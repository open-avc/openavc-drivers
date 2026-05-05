"""
Biamp Tesira Text Protocol (TTP) Driver.

Controls Biamp Tesira and TesiraFORTÉ DSPs over the Tesira Text Protocol on
TCP port 23 (Telnet). Comprehensive coverage of the block types AV
integrators commonly wire into a Tesira design: Mute, Level, Source
Selector, Standard / Matrix Mixer (crosspoints), Auto Mixer, Router, Logic
and Logic Meter, AEC, Room Combiner, Audio Meter (Peak/RMS), Signal Present
Meter, Tone / Audio Generator, Preset recall, and basic VoIP Receive +
Dialer surfaces.

Why Python (rules.md Principle 9):
    Tesira's instance-tag/channel topology is declared by the integrator in
    Tesira software, not fixed by the driver. Each install has a different
    set of blocks. ConfigurableDriver loads `state_variables` and `commands`
    statically from the YAML at class-creation time — there's no per-instance
    expansion hook. This driver follows the Symetrix Composer precedent of
    rebuilding `state_variables` and `commands` in __init__ from a user
    `blocks` config, so panel UIs can bind directly to per-block / per-channel
    state keys.

Why subscriptions, not polling (rules.md Principle 2):
    Tesira natively pushes state-change notifications via the `subscribe`
    command. The driver enables a session subscription per declared
    (block, attribute, channel) on connect (and again after every reconnect
    — Tesira subscriptions are session-scoped). It also issues a one-shot
    `get` for each subscribed attribute so panel UIs reflect current values
    immediately even if no change happens to fire a push.

Telnet IAC handshake:
    Tesira sends Telnet IAC option negotiation bytes (RFC 854/855) the moment
    the TCP socket opens. Per Biamp's wiki ("Telnet session negotiation in
    Tesira"), the controller must reply WONT to every DO and DONT to every
    WILL, then wait for the "Welcome to the Tesira Text Protocol Server"
    banner before sending TTP commands. The driver does this in raw mode
    (no frame parser), then swaps the transport to a `\\r\\n` delimiter
    parser for normal command/response framing — same pattern that
    ConfigurableDriver uses for its declarative auth handshake.

Sources:
    - https://support.biamp.com/Tesira/Control/Tesira_Text_Protocol
    - https://support.biamp.com/Tesira/Control/Telnet_session_negotiation_in_Tesira
    - https://support.biamp.com/Tesira/Control/Tesira_Text_Protocol/Tesira_DSP_blocks_that_support_subscriptions
    - https://support.biamp.com/Tesira/Control/Tesira_network_ports_and_protocols
    - Bitfocus Companion module (real-hardware reference for IAC handling
      and publishToken format):
      https://github.com/bitfocus/companion-module-biamp-tesira

Out of scope (deferred, tracked):
    - SSH transport (port 22). Tesira's raw TCP-on-22 is real SSH and our
      TCPTransport is raw bytes. Telnet (port 23) is the recommended path
      when System Security is disabled (default), and is what every reference
      implementation uses. SSH support is tracked in driver-roadmap/rules.md
      "Drivers shipped Python pending platform-transport extension".
    - System Security login prompt (optional `Login:` prompt before banner
      when System Security is enabled). Default OFF on Tesira; document
      "leave System Security disabled" in setup help.
    - VoIP call control beyond CallState observation (call placement,
      hold, transfer, etc.). The current driver exposes basic Dialer
      commands (dial, hangup, answer, dtmf) but doesn't model the full
      call state machine.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.frame_parsers import DelimiterFrameParser
from server.utils.logger import get_logger

log = get_logger(__name__)


# ── Telnet IAC constants (RFC 854 / 855) ──
IAC = 0xFF
DO = 0xFD
DONT = 0xFE
WILL = 0xFB
WONT = 0xFC

WELCOME_BANNER = b"Welcome to the Tesira Text Protocol Server"
LINE_DELIMITER = b"\r\n"

# Default subscription rate in milliseconds. 250ms matches Companion's default
# and balances responsiveness against bandwidth on AEC / meter blocks.
DEFAULT_SUBSCRIBE_RATE_MS = 250

# Higher-rate throttle for fast-moving values (audio meters). Subscribing at
# 25-50ms on a meter block fills the wire; 500ms is enough for a UI bargraph.
METER_SUBSCRIBE_RATE_MS = 500

# Inter-command delay default. Tesira's parser tolerates rapid bursts but
# subscribe commands need to sequence on connect; small delay keeps ordering
# clean.
DEFAULT_INTER_COMMAND_DELAY = 0.05

# Block-type vocabulary the textarea grammar accepts. Keys are user-visible
# type tokens (case-insensitive on parse). Values describe how to expand the
# block into state vars, subscriptions, and commands.
BLOCK_TYPES = {
    "mute",
    "level",
    "fader",  # alias for level
    "source_select",
    "source_selector",  # alias
    "matrix_mixer",
    "mixer",  # alias for matrix_mixer
    "automixer",
    "router",
    "logic",
    "logic_meter",
    "aec",
    "room_combiner",
    "audio_meter",
    "meter",  # alias for audio_meter
    "signal_present",
    "generator",
    "tone_generator",  # alias
    "voip_rx",
    "voip_receive",  # alias
    "dialer",
    "preset",  # bare preset label (no state surface, just convenience commands)
}


def _safe_token(s: str) -> str:
    """Sanitize a string for use as a state key segment.

    Tesira instance tags can technically contain any printable character.
    Most installations stick to alphanumeric + underscore, but we strip
    anything else defensively so state keys don't collide with our format
    or break the transport layer.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def _parse_channel_spec(spec: str | None) -> list[int]:
    """Parse a channel spec like '4', '1-4', '1,2,5', or '' into a list.

    Returns an empty list for missing/empty specs (callers that don't take
    a channel index treat empty == [None] or single-channel sentinel).
    """
    if not spec or not spec.strip():
        return []
    spec = spec.strip()
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(part))
            except ValueError:
                # Not an integer — could be a bare channel count e.g. "4"
                # already handled above, or trash; skip.
                continue
    # Dedupe while preserving order
    seen: set[int] = set()
    result = []
    for n in out:
        if n not in seen:
            seen.add(n)
            result.append(n)
    return result


def _parse_matrix_spec(spec: str | None) -> tuple[int, int]:
    """Parse '8x4' -> (8, 4). Returns (0, 0) for invalid input."""
    if not spec:
        return 0, 0
    m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", spec)
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2))


def parse_blocks_config(text: str) -> list[dict[str, Any]]:
    """Parse the user-supplied `blocks` textarea into a list of block dicts.

    Format: one block per line, whitespace-separated.
        <INSTANCE_TAG> <BLOCK_TYPE> [CHANNELS|MATRIX]

    Comments start with '#' or ';'. Blank lines are ignored.

    Returns a list of dicts:
        {"tag": "Mute1", "type": "mute", "channels": [1, 2, 3, 4], "extra": {}}

    For matrix_mixer, "extra" carries {"inputs": 8, "outputs": 4} and
    "channels" is empty. For source_select / dialer / generator, "channels"
    is empty. For mute/level/etc., "channels" is the list of channel indexes
    (e.g. [1,2,3,4] for "1-4").

    Unknown block types are returned with type="unknown" so the driver can
    log a warning rather than crashing on a typo.
    """
    blocks: list[dict[str, Any]] = []
    if not text:
        return blocks

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        # Strip inline comments after the body (only those preceded by
        # whitespace, so '#' inside an instance tag stays put — Tesira
        # tags don't usually contain '#' but be defensive).
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
            log.warning(f"Tesira blocks parse: line too short, skipping: {raw_line!r}")
            continue

        tag = parts[0]
        type_token = parts[1].lower()
        spec = parts[2] if len(parts) >= 3 else ""

        if type_token not in BLOCK_TYPES:
            log.warning(
                f"Tesira blocks parse: unknown block type {type_token!r} on "
                f"line {raw_line!r}; valid types: {sorted(BLOCK_TYPES)}"
            )
            blocks.append({
                "tag": tag,
                "type": "unknown",
                "raw_type": type_token,
                "channels": [],
                "extra": {},
            })
            continue

        # Normalize aliases to canonical names
        canonical_type = {
            "fader": "level",
            "source_selector": "source_select",
            "mixer": "matrix_mixer",
            "tone_generator": "generator",
            "voip_receive": "voip_rx",
            "meter": "audio_meter",
        }.get(type_token, type_token)

        block: dict[str, Any] = {
            "tag": tag,
            "type": canonical_type,
            "channels": [],
            "extra": {},
        }

        if canonical_type == "matrix_mixer":
            inputs, outputs = _parse_matrix_spec(spec)
            if inputs == 0 or outputs == 0:
                # Default to 4x4 if user forgot to specify
                inputs, outputs = 4, 4
                log.warning(
                    f"Tesira blocks parse: matrix_mixer {tag!r} missing "
                    f"NxM spec, defaulting to 4x4"
                )
            block["extra"] = {"inputs": inputs, "outputs": outputs}
        elif canonical_type in ("source_select", "voip_rx", "dialer", "generator", "preset"):
            # No channel spec needed
            pass
        else:
            channels = _parse_channel_spec(spec)
            if not channels:
                # Default to single channel 1 if user omitted the channel spec
                channels = [1]
            block["channels"] = channels

        blocks.append(block)

    return blocks


# ── State variable / subscription / command expansion ──

def _state_var(block: dict[str, Any], suffix: str) -> str:
    """Build a state-var key from a block + attribute suffix."""
    return f"{_safe_token(block['tag'])}_{suffix}"


def expand_state_variables(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build a state_variables dict for the driver, given parsed blocks.

    Always includes a small set of system-level state vars (firmware,
    serial, last_preset, last_query_result, last_raw_response). Then
    appends per-block state vars based on type.
    """
    out: dict[str, dict[str, Any]] = {
        "firmware_version": {"type": "string", "label": "Firmware Version"},
        "serial_number": {"type": "string", "label": "Serial Number"},
        "device_id_str": {"type": "string", "label": "Device ID"},
        "last_preset": {"type": "string", "label": "Last Recalled Preset"},
        "last_query_result": {
            "type": "string",
            "label": "Last GET Result",
            "help": "Result of the last get_attribute / send_raw query.",
        },
        "last_raw_response": {
            "type": "string",
            "label": "Last Raw Response",
            "help": "Most recent line received from the DSP (debug aid).",
        },
        "last_error": {"type": "string", "label": "Last DSP Error"},
    }

    for blk in blocks:
        bt = blk["type"]
        chans = blk["channels"]
        extra = blk["extra"]

        if bt == "mute":
            for ch in chans:
                out[_state_var(blk, f"mute_{ch}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} Mute Ch {ch}",
                }
        elif bt == "level":
            for ch in chans:
                out[_state_var(blk, f"level_{ch}")] = {
                    "type": "number",
                    "label": f"{blk['tag']} Level Ch {ch} (dB)",
                    "min": -100,
                    "max": 12,
                }
                out[_state_var(blk, f"mute_{ch}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} Mute Ch {ch}",
                }
        elif bt == "source_select":
            out[_state_var(blk, "source")] = {
                "type": "integer",
                "label": f"{blk['tag']} Selected Source",
                "min": 0,
                "max": 64,
            }
            out[_state_var(blk, "output_level")] = {
                "type": "number",
                "label": f"{blk['tag']} Output Level (dB)",
                "min": -100,
                "max": 12,
            }
            out[_state_var(blk, "output_mute")] = {
                "type": "boolean",
                "label": f"{blk['tag']} Output Mute",
            }
        elif bt == "matrix_mixer":
            inputs = extra.get("inputs", 4)
            outputs = extra.get("outputs", 4)
            for i in range(1, inputs + 1):
                out[_state_var(blk, f"input_mute_{i}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} Input {i} Mute",
                }
            for o in range(1, outputs + 1):
                out[_state_var(blk, f"output_level_{o}")] = {
                    "type": "number",
                    "label": f"{blk['tag']} Output {o} Level (dB)",
                    "min": -100,
                    "max": 12,
                }
                out[_state_var(blk, f"output_mute_{o}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} Output {o} Mute",
                }
            for i in range(1, inputs + 1):
                for o in range(1, outputs + 1):
                    out[_state_var(blk, f"xpoint_{i}_{o}")] = {
                        "type": "boolean",
                        "label": f"{blk['tag']} XP {i}->{o} On",
                    }
                    out[_state_var(blk, f"xpoint_level_{i}_{o}")] = {
                        "type": "number",
                        "label": f"{blk['tag']} XP {i}->{o} Level (dB)",
                        "min": -100,
                        "max": 12,
                    }
        elif bt == "automixer":
            for ch in chans:
                out[_state_var(blk, f"channel_level_{ch}")] = {
                    "type": "number",
                    "label": f"{blk['tag']} Ch {ch} Level (dB)",
                    "min": -100,
                    "max": 12,
                }
                out[_state_var(blk, f"channel_mute_{ch}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} Ch {ch} Mute",
                }
                out[_state_var(blk, f"gain_reduction_{ch}")] = {
                    "type": "number",
                    "label": f"{blk['tag']} Ch {ch} Gain Reduction (dB)",
                }
            out[_state_var(blk, "output_level")] = {
                "type": "number",
                "label": f"{blk['tag']} Output Level (dB)",
            }
            out[_state_var(blk, "output_mute")] = {
                "type": "boolean",
                "label": f"{blk['tag']} Output Mute",
            }
        elif bt == "router":
            for o in chans:
                out[_state_var(blk, f"output_{o}")] = {
                    "type": "integer",
                    "label": f"{blk['tag']} Output {o} Source",
                    "min": 0,
                    "max": 256,
                }
        elif bt == "logic":
            for ch in chans:
                out[_state_var(blk, f"state_{ch}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} Logic State {ch}",
                }
        elif bt == "logic_meter":
            for ch in chans:
                out[_state_var(blk, f"state_{ch}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} Logic Meter {ch}",
                }
        elif bt == "aec":
            for ch in chans:
                out[_state_var(blk, f"level_{ch}")] = {
                    "type": "number",
                    "label": f"{blk['tag']} AEC Ch {ch} Level (dB)",
                }
                out[_state_var(blk, f"mute_{ch}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} AEC Ch {ch} Mute",
                }
                out[_state_var(blk, f"erc_state_{ch}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} AEC Ch {ch} ERC Active",
                }
        elif bt == "room_combiner":
            for w in chans:
                out[_state_var(blk, f"group_{w}")] = {
                    "type": "integer",
                    "label": f"{blk['tag']} Wall {w} Group",
                    "min": 0,
                    "max": 32,
                }
                out[_state_var(blk, f"combine_{w}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} Wall {w} Combined",
                }
        elif bt == "audio_meter":
            for ch in chans:
                out[_state_var(blk, f"meter_{ch}")] = {
                    "type": "number",
                    "label": f"{blk['tag']} Meter Ch {ch} (dB)",
                }
        elif bt == "signal_present":
            for ch in chans:
                out[_state_var(blk, f"signal_present_{ch}")] = {
                    "type": "boolean",
                    "label": f"{blk['tag']} Signal Present Ch {ch}",
                }
                out[_state_var(blk, f"signal_level_{ch}")] = {
                    "type": "number",
                    "label": f"{blk['tag']} Signal Level Ch {ch} (dB)",
                }
        elif bt == "generator":
            out[_state_var(blk, "amplitude")] = {
                "type": "number",
                "label": f"{blk['tag']} Generator Amplitude (dB)",
            }
            out[_state_var(blk, "frequency")] = {
                "type": "integer",
                "label": f"{blk['tag']} Generator Frequency (Hz)",
            }
            out[_state_var(blk, "state")] = {
                "type": "boolean",
                "label": f"{blk['tag']} Generator On",
            }
        elif bt == "voip_rx":
            out[_state_var(blk, "call_state")] = {
                "type": "string",
                "label": f"{blk['tag']} Call State",
            }
            out[_state_var(blk, "mute")] = {
                "type": "boolean",
                "label": f"{blk['tag']} Receive Mute",
            }
        # dialer: no state — exposes commands only
        # preset: no state — convenience commands only

    return out


def expand_subscriptions(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the list of subscriptions to register on connect.

    Each entry is a dict with the wire-level pieces we need:
        {
            "tag": "Mute1",           # instance tag
            "attribute": "mute",      # TTP attribute name (case-sensitive)
            "index": 1,               # channel/index, or None for indexless
            "token": "Mute1_mute_1",  # publishToken, also used as state-var key
            "rate_ms": 250,           # subscribe rate
            "type_hint": "boolean",   # how to coerce push values
        }

    The token doubles as the state-var name so the push handler doesn't
    need a separate token-to-state map.
    """
    subs: list[dict[str, Any]] = []

    def add(tag: str, attr: str, idx: int | None, state_key: str,
            type_hint: str, rate_ms: int = DEFAULT_SUBSCRIBE_RATE_MS) -> None:
        subs.append({
            "tag": tag,
            "attribute": attr,
            "index": idx,
            "token": state_key,
            "rate_ms": rate_ms,
            "type_hint": type_hint,
        })

    for blk in blocks:
        bt = blk["type"]
        tag = blk["tag"]
        chans = blk["channels"]
        extra = blk["extra"]

        if bt == "mute":
            for ch in chans:
                add(tag, "mute", ch, _state_var(blk, f"mute_{ch}"), "boolean")
        elif bt == "level":
            for ch in chans:
                add(tag, "level", ch, _state_var(blk, f"level_{ch}"), "number")
                add(tag, "mute", ch, _state_var(blk, f"mute_{ch}"), "boolean")
        elif bt == "source_select":
            add(tag, "sourceSelection", None, _state_var(blk, "source"), "integer")
            add(tag, "outputLevel", None, _state_var(blk, "output_level"), "number")
            add(tag, "outputMute", None, _state_var(blk, "output_mute"), "boolean")
        elif bt == "matrix_mixer":
            inputs = extra.get("inputs", 4)
            outputs = extra.get("outputs", 4)
            for i in range(1, inputs + 1):
                add(tag, "inputMute", i, _state_var(blk, f"input_mute_{i}"), "boolean")
            for o in range(1, outputs + 1):
                add(tag, "outputLevel", o, _state_var(blk, f"output_level_{o}"), "number")
                add(tag, "outputMute", o, _state_var(blk, f"output_mute_{o}"), "boolean")
            # Crosspoint state grid — two-index attribute. The subscribe
            # command format is "<TAG> subscribe crosspointLevelState <i>
            # <o> "<token>" <rate>". We pass the index as a (i, o) tuple
            # in the subscription dict; _build_subscribe_command renders
            # it as space-separated indexes, and _push_subscribers in the
            # simulator matches on the same tuple. Skip subscription on
            # very large matrices to avoid overwhelming the wire — the
            # set/get commands still work; the user can subscribe to
            # specific crosspoints at runtime via subscribe_attribute if
            # needed.
            if inputs * outputs <= 64:
                for i in range(1, inputs + 1):
                    for o in range(1, outputs + 1):
                        add(tag, "crosspointLevelState", (i, o),
                            _state_var(blk, f"xpoint_{i}_{o}"), "boolean",
                            rate_ms=DEFAULT_SUBSCRIBE_RATE_MS)
                        add(tag, "crosspointLevel", (i, o),
                            _state_var(blk, f"xpoint_level_{i}_{o}"), "number",
                            rate_ms=DEFAULT_SUBSCRIBE_RATE_MS)
        elif bt == "automixer":
            for ch in chans:
                add(tag, "channelLevel", ch, _state_var(blk, f"channel_level_{ch}"), "number")
                add(tag, "channelMute", ch, _state_var(blk, f"channel_mute_{ch}"), "boolean")
                add(tag, "gainReduction", ch, _state_var(blk, f"gain_reduction_{ch}"), "number")
            add(tag, "outputLevel", None, _state_var(blk, "output_level"), "number")
            add(tag, "outputMute", None, _state_var(blk, "output_mute"), "boolean")
        elif bt == "router":
            for o in chans:
                add(tag, "output", o, _state_var(blk, f"output_{o}"), "integer")
        elif bt == "logic":
            for ch in chans:
                add(tag, "state", ch, _state_var(blk, f"state_{ch}"), "boolean")
        elif bt == "logic_meter":
            for ch in chans:
                add(tag, "state", ch, _state_var(blk, f"state_{ch}"), "boolean")
        elif bt == "aec":
            for ch in chans:
                add(tag, "level", ch, _state_var(blk, f"level_{ch}"), "number")
                add(tag, "mute", ch, _state_var(blk, f"mute_{ch}"), "boolean")
                add(tag, "ercState", ch, _state_var(blk, f"erc_state_{ch}"), "boolean")
        elif bt == "room_combiner":
            for w in chans:
                add(tag, "group", w, _state_var(blk, f"group_{w}"), "integer")
                add(tag, "combine", w, _state_var(blk, f"combine_{w}"), "boolean")
        elif bt == "audio_meter":
            for ch in chans:
                add(tag, "level", ch, _state_var(blk, f"meter_{ch}"), "number",
                    rate_ms=METER_SUBSCRIBE_RATE_MS)
        elif bt == "signal_present":
            for ch in chans:
                add(tag, "signalPresent", ch, _state_var(blk, f"signal_present_{ch}"), "boolean")
                add(tag, "signalLevel", ch, _state_var(blk, f"signal_level_{ch}"), "number",
                    rate_ms=METER_SUBSCRIBE_RATE_MS)
        elif bt == "generator":
            add(tag, "amplitude", None, _state_var(blk, "amplitude"), "number")
            add(tag, "frequency", None, _state_var(blk, "frequency"), "integer")
            add(tag, "generatorEnable", None, _state_var(blk, "state"), "boolean")
        elif bt == "voip_rx":
            add(tag, "callState", None, _state_var(blk, "call_state"), "string")
            add(tag, "mute", None, _state_var(blk, "mute"), "boolean")

    return subs


def expand_commands(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build the per-instance commands dict.

    Always includes the generic escape hatches (set_attribute, get_attribute,
    increment_attribute, decrement_attribute, recall_preset, send_raw, etc).
    Per-block convenience commands are added for each declared block so the
    Programmer IDE Macros editor surfaces them by name.
    """
    cmds: dict[str, dict[str, Any]] = {
        # Generic escape hatches
        "set_attribute": {
            "label": "Set Attribute (raw)",
            "params": {
                "tag": {"type": "string", "required": True, "label": "Instance Tag"},
                "attribute": {"type": "string", "required": True, "label": "Attribute"},
                "index": {"type": "string", "required": False, "label": "Index", "default": ""},
                "value": {"type": "string", "required": True, "label": "Value"},
            },
            "help": "Send raw '<TAG> set <attr> [<index>] <value>'. "
                    "For Set commands the value is sent literally — for booleans "
                    "use 'true'/'false', for numbers send the digits.",
        },
        "get_attribute": {
            "label": "Get Attribute (raw)",
            "params": {
                "tag": {"type": "string", "required": True, "label": "Instance Tag"},
                "attribute": {"type": "string", "required": True, "label": "Attribute"},
                "index": {"type": "string", "required": False, "label": "Index", "default": ""},
            },
            "help": "Query a raw attribute. Result lands in last_query_result state var.",
        },
        "toggle_attribute": {
            "label": "Toggle Attribute (raw)",
            "params": {
                "tag": {"type": "string", "required": True, "label": "Instance Tag"},
                "attribute": {"type": "string", "required": True, "label": "Attribute"},
                "index": {"type": "string", "required": False, "label": "Index", "default": ""},
            },
            "help": "Toggle a boolean attribute (mute, etc).",
        },
        "increment_attribute": {
            "label": "Increment Attribute (raw)",
            "params": {
                "tag": {"type": "string", "required": True, "label": "Instance Tag"},
                "attribute": {"type": "string", "required": True, "label": "Attribute"},
                "index": {"type": "string", "required": False, "label": "Index", "default": ""},
                "amount": {"type": "number", "required": True, "label": "Amount", "default": 1.0},
            },
        },
        "decrement_attribute": {
            "label": "Decrement Attribute (raw)",
            "params": {
                "tag": {"type": "string", "required": True, "label": "Instance Tag"},
                "attribute": {"type": "string", "required": True, "label": "Attribute"},
                "index": {"type": "string", "required": False, "label": "Index", "default": ""},
                "amount": {"type": "number", "required": True, "label": "Amount", "default": 1.0},
            },
        },
        "recall_preset": {
            "label": "Recall Preset by ID",
            "params": {
                "preset_id": {"type": "integer", "required": True, "label": "Preset ID",
                              "min": 1001, "max": 9999,
                              "help": "Preset ID as shown in Tesira's Preset Manager."},
            },
        },
        "recall_preset_by_name": {
            "label": "Recall Preset by Name",
            "params": {
                "name": {"type": "string", "required": True, "label": "Preset Name"},
            },
        },
        "save_preset": {
            "label": "Save Current State as Preset",
            "params": {
                "preset_id": {"type": "integer", "required": True, "label": "Preset ID",
                              "min": 1001, "max": 9999},
            },
            "help": "Saves the current DSP state to the specified preset slot.",
        },
        "send_raw": {
            "label": "Send Raw TTP Command",
            "params": {
                "command": {"type": "string", "required": True, "label": "Command line"},
            },
            "help": "Sends the literal text as a TTP command line. Response goes "
                    "to last_raw_response. Use sparingly — covers anything the "
                    "typed commands don't.",
        },
        "subscribe_attribute": {
            "label": "Subscribe to Attribute (runtime)",
            "params": {
                "tag": {"type": "string", "required": True, "label": "Instance Tag"},
                "attribute": {"type": "string", "required": True, "label": "Attribute"},
                "index": {"type": "string", "required": False, "label": "Index", "default": ""},
                "token": {"type": "string", "required": True, "label": "Publish Token"},
                "rate_ms": {"type": "integer", "required": False, "label": "Rate (ms)",
                            "default": 250, "min": 50, "max": 60000},
            },
            "help": "Add a runtime subscription. The token doubles as the state "
                    "var name; if it's not declared in your blocks list, scripts "
                    "can read it via state.get('device.<id>.<token>').",
        },
        "unsubscribe_attribute": {
            "label": "Unsubscribe from Attribute (runtime)",
            "params": {
                "tag": {"type": "string", "required": True, "label": "Instance Tag"},
                "attribute": {"type": "string", "required": True, "label": "Attribute"},
                "index": {"type": "string", "required": False, "label": "Index", "default": ""},
                "token": {"type": "string", "required": True, "label": "Publish Token"},
            },
        },
        "session_quit": {
            "label": "Disconnect Session",
            "params": {},
            "help": "Sends 'SESSION quit' to politely free the TCP slot on the DSP.",
        },
    }

    # Per-block convenience commands.
    for blk in blocks:
        bt = blk["type"]
        tag = blk["tag"]
        chans = blk["channels"]
        extra = blk["extra"]
        safe = _safe_token(tag)

        if bt == "mute":
            cmds[f"{safe}_mute_set"] = {
                "label": f"{tag}: Set Mute",
                "params": {
                    "channel": {"type": "integer", "required": True, "label": "Channel",
                                "min": min(chans) if chans else 1,
                                "max": max(chans) if chans else 1},
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
            cmds[f"{safe}_mute_toggle"] = {
                "label": f"{tag}: Toggle Mute",
                "params": {
                    "channel": {"type": "integer", "required": True, "label": "Channel",
                                "min": min(chans) if chans else 1,
                                "max": max(chans) if chans else 1},
                },
            }
        elif bt == "level":
            ch_min = min(chans) if chans else 1
            ch_max = max(chans) if chans else 1
            cmds[f"{safe}_level_set"] = {
                "label": f"{tag}: Set Level (dB)",
                "params": {
                    "channel": {"type": "integer", "required": True, "label": "Channel",
                                "min": ch_min, "max": ch_max},
                    "level_db": {"type": "number", "required": True, "label": "Level (dB)",
                                 "min": -100, "max": 12},
                },
            }
            cmds[f"{safe}_level_step"] = {
                "label": f"{tag}: Step Level",
                "params": {
                    "channel": {"type": "integer", "required": True, "label": "Channel",
                                "min": ch_min, "max": ch_max},
                    "amount_db": {"type": "number", "required": True, "label": "Amount (dB)",
                                  "default": 1.0,
                                  "help": "Positive = increment, negative = decrement."},
                },
            }
            cmds[f"{safe}_level_ramp"] = {
                "label": f"{tag}: Ramp Level",
                "params": {
                    "channel": {"type": "integer", "required": True, "label": "Channel",
                                "min": ch_min, "max": ch_max},
                    "target_db": {"type": "number", "required": True, "label": "Target (dB)",
                                  "min": -100, "max": 12},
                    "duration_s": {"type": "number", "required": True, "label": "Duration (sec)",
                                   "default": 1.0, "min": 0.0},
                },
                "help": "Smoothly ramps the level to the target over the specified duration.",
            }
            cmds[f"{safe}_mute_set"] = {
                "label": f"{tag}: Set Mute",
                "params": {
                    "channel": {"type": "integer", "required": True, "label": "Channel",
                                "min": ch_min, "max": ch_max},
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
            cmds[f"{safe}_mute_toggle"] = {
                "label": f"{tag}: Toggle Mute",
                "params": {
                    "channel": {"type": "integer", "required": True, "label": "Channel",
                                "min": ch_min, "max": ch_max},
                },
            }
        elif bt == "source_select":
            cmds[f"{safe}_select_source"] = {
                "label": f"{tag}: Select Source",
                "params": {
                    "source": {"type": "integer", "required": True, "label": "Source Index",
                               "min": 0, "max": 64,
                               "help": "0 = no source selected; 1..N = pick source N."},
                },
            }
            cmds[f"{safe}_set_output_level"] = {
                "label": f"{tag}: Set Output Level (dB)",
                "params": {
                    "level_db": {"type": "number", "required": True, "label": "Level (dB)",
                                 "min": -100, "max": 12},
                },
            }
            cmds[f"{safe}_output_mute_set"] = {
                "label": f"{tag}: Set Output Mute",
                "params": {
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
        elif bt == "matrix_mixer":
            inputs = extra.get("inputs", 4)
            outputs = extra.get("outputs", 4)
            cmds[f"{safe}_xpoint_set"] = {
                "label": f"{tag}: Set Crosspoint On/Off",
                "params": {
                    "input_n": {"type": "integer", "required": True, "label": "Input",
                                "min": 1, "max": inputs},
                    "output_n": {"type": "integer", "required": True, "label": "Output",
                                 "min": 1, "max": outputs},
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
            cmds[f"{safe}_xpoint_level"] = {
                "label": f"{tag}: Set Crosspoint Level (dB)",
                "params": {
                    "input_n": {"type": "integer", "required": True, "label": "Input",
                                "min": 1, "max": inputs},
                    "output_n": {"type": "integer", "required": True, "label": "Output",
                                 "min": 1, "max": outputs},
                    "level_db": {"type": "number", "required": True, "label": "Level (dB)",
                                 "min": -100, "max": 12},
                },
            }
            cmds[f"{safe}_output_level_set"] = {
                "label": f"{tag}: Set Output Level (dB)",
                "params": {
                    "output_n": {"type": "integer", "required": True, "label": "Output",
                                 "min": 1, "max": outputs},
                    "level_db": {"type": "number", "required": True, "label": "Level (dB)",
                                 "min": -100, "max": 12},
                },
            }
            cmds[f"{safe}_output_mute_set"] = {
                "label": f"{tag}: Set Output Mute",
                "params": {
                    "output_n": {"type": "integer", "required": True, "label": "Output",
                                 "min": 1, "max": outputs},
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
        elif bt == "router":
            cmds[f"{safe}_route"] = {
                "label": f"{tag}: Route Source to Output",
                "params": {
                    "output_n": {"type": "integer", "required": True, "label": "Output",
                                 "min": 1, "max": (max(chans) if chans else 8)},
                    "source": {"type": "integer", "required": True, "label": "Source",
                               "min": 0, "max": 256,
                               "help": "0 = disconnect; otherwise pick source N."},
                },
            }
        elif bt == "automixer":
            ch_max = max(chans) if chans else 1
            cmds[f"{safe}_channel_mute_set"] = {
                "label": f"{tag}: Set Channel Mute",
                "params": {
                    "channel": {"type": "integer", "required": True, "label": "Channel",
                                "min": 1, "max": ch_max},
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
            cmds[f"{safe}_channel_level_set"] = {
                "label": f"{tag}: Set Channel Level (dB)",
                "params": {
                    "channel": {"type": "integer", "required": True, "label": "Channel",
                                "min": 1, "max": ch_max},
                    "level_db": {"type": "number", "required": True, "label": "Level (dB)",
                                 "min": -100, "max": 12},
                },
            }
        elif bt == "room_combiner":
            wall_max = max(chans) if chans else 1
            cmds[f"{safe}_set_group"] = {
                "label": f"{tag}: Assign Wall to Group",
                "params": {
                    "wall": {"type": "integer", "required": True, "label": "Wall",
                             "min": 1, "max": wall_max},
                    "group": {"type": "integer", "required": True, "label": "Group",
                              "min": 0, "max": 32,
                              "help": "0 = isolated; matching group numbers combine."},
                },
            }
            cmds[f"{safe}_set_combine"] = {
                "label": f"{tag}: Set Wall Combine",
                "params": {
                    "wall": {"type": "integer", "required": True, "label": "Wall",
                             "min": 1, "max": wall_max},
                    "value": {"type": "enum", "required": True, "label": "Combine",
                              "values": ["true", "false"]},
                },
            }
        elif bt == "logic":
            ch_max = max(chans) if chans else 1
            cmds[f"{safe}_set_state"] = {
                "label": f"{tag}: Set Logic State",
                "params": {
                    "channel": {"type": "integer", "required": True, "label": "Channel",
                                "min": 1, "max": ch_max},
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
        elif bt == "generator":
            cmds[f"{safe}_set_amplitude"] = {
                "label": f"{tag}: Set Generator Amplitude (dB)",
                "params": {
                    "level_db": {"type": "number", "required": True, "label": "Amplitude (dB)",
                                 "min": -100, "max": 0},
                },
            }
            cmds[f"{safe}_set_frequency"] = {
                "label": f"{tag}: Set Generator Frequency (Hz)",
                "params": {
                    "hz": {"type": "integer", "required": True, "label": "Frequency (Hz)",
                           "min": 20, "max": 20000},
                },
            }
            cmds[f"{safe}_enable"] = {
                "label": f"{tag}: Enable Generator",
                "params": {
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }
        elif bt == "dialer":
            cmds[f"{safe}_dial"] = {
                "label": f"{tag}: Dial Number",
                "params": {
                    "number": {"type": "string", "required": True, "label": "Phone Number"},
                    "line": {"type": "integer", "required": False, "label": "Line",
                             "default": 1, "min": 1, "max": 8},
                },
            }
            cmds[f"{safe}_hangup"] = {
                "label": f"{tag}: Hang Up",
                "params": {
                    "line": {"type": "integer", "required": False, "label": "Line",
                             "default": 1, "min": 1, "max": 8},
                },
            }
            cmds[f"{safe}_answer"] = {
                "label": f"{tag}: Answer Call",
                "params": {
                    "line": {"type": "integer", "required": False, "label": "Line",
                             "default": 1, "min": 1, "max": 8},
                },
            }
            cmds[f"{safe}_dtmf"] = {
                "label": f"{tag}: Send DTMF",
                "params": {
                    "digits": {"type": "string", "required": True, "label": "Digits",
                               "help": "Digits 0-9 plus *, #, A-D."},
                    "line": {"type": "integer", "required": False, "label": "Line",
                             "default": 1, "min": 1, "max": 8},
                },
            }
        elif bt == "voip_rx":
            cmds[f"{safe}_set_mute"] = {
                "label": f"{tag}: Set Receive Mute",
                "params": {
                    "value": {"type": "enum", "required": True, "label": "State",
                              "values": ["true", "false"]},
                },
            }

    return cmds


# ── The driver class ──

# Default block list seeded into the textarea so the Add Device dialog
# isn't empty. Covers a typical conferencing room: mic mute/level for AEC
# inputs, program level + mute, source select, recall preset.
DEFAULT_BLOCKS_TEXT = """\
# Tesira DSP Block List — one block per line.
# Format:  <INSTANCE_TAG>  <TYPE>  [CHANNELS|NxM]
# Lines starting with '#' or ';' are comments and are ignored.
#
# To find Instance Tags: in Tesira software, right-click any control
# block in your design and choose "Properties" — the Instance Tag is
# the user-assigned label at the top of the dialog. Tags are
# case-sensitive and must match exactly.
#
# CHANNELS:
#   "1"     single channel
#   "1-4"   range, four channels
#   "1,3,5" specific channels
#   (omit)  defaults to channel 1
#
# Block types (CHANNELS unless noted):
#   mute              mute control block
#   level             level/fader block (also creates per-channel mute)
#   source_select     source selector block (no channel)
#   matrix_mixer NxM  standard or matrix mixer; NxM is INPUTS x OUTPUTS,
#                     e.g. "8x4" for 8 inputs into 4 outputs
#   automixer         gain-sharing auto mixer (one channel per mic)
#   router            audio router (one channel per output)
#   logic             logic state block (input/output bool)
#   logic_meter       logic meter block
#   aec               AEC processor (one channel per AEC input)
#   room_combiner     room combiner block (one channel per wall/partition)
#   audio_meter       peak/RMS meter (read-only, dB)
#   signal_present    signal-present indicator (per channel)
#   generator         tone generator (no channel)
#   voip_rx           VoIP receive block (no channel)
#   dialer            VoIP dialer block (no channel; commands only)
#
# Replace the example blocks below with the Instance Tags from YOUR
# Tesira design. Delete the lines you don't need; you can add more
# blocks later by editing the device.

# Example: typical conference room
Mute1   mute    1-4         # 4 microphone mute channels
Level1  level   1-4         # 4 microphone level channels (with per-channel mute)
PgmMute mute    1           # program-out master mute
PgmLvl  level   1           # program-out master level
PgmSrc  source_select       # program source selector
"""


# Subscription-push response regex.
# Format: ! "publishToken":"NAME" "value":VALUE
# VALUE may be a scalar (-12.5, true, "string") or an array ([1.0 2.0 3.0]).
PUSH_LINE_RE = re.compile(
    r'^!\s+"publishToken":"([^"]+)"\s+"value":(.*)$'
)

# +OK response patterns
OK_VALUE_RE = re.compile(r'^\+OK\s+"value":(.*)$')
OK_LIST_RE = re.compile(r'^\+OK\s+"list":(.*)$')
ERR_RE = re.compile(r'^-ERR\s+(.*)$')


class BiampTesiraTTPDriver(BaseDriver):
    """Biamp Tesira Text Protocol (TTP) driver — comprehensive coverage."""

    DRIVER_INFO = {
        "id": "biamp_tesira_ttp",
        "name": "Biamp Tesira TTP",
        "manufacturer": "Biamp",
        "category": "audio",
        "version": "2.1.0",
        "min_platform_version": "0.11.0",
        "author": "OpenAVC",
        "description": (
            "Controls Biamp Tesira and TesiraFORTÉ DSPs over the Tesira "
            "Text Protocol on TCP port 23 (Telnet). Supports Mute, Level, "
            "Source Selector, Matrix Mixer (crosspoints), Auto Mixer, "
            "Router, Logic, AEC, Room Combiner, Audio Meter, Signal "
            "Present Meter, Tone Generator, Preset recall, and basic VoIP "
            "/ Dialer surfaces. Subscribes to push updates instead of "
            "polling. Declare your Tesira blocks once in the device "
            "config — the driver expands per-block / per-channel state "
            "variables and commands automatically."
        ),
        "source_url": "https://support.biamp.com/Tesira/Control/Tesira_Text_Protocol",
        "tags": ["dsp", "tesira", "ttp", "ceiling-mic", "aec", "conferencing"],
        "verified": False,
        "simulated": True,
        "protocols": ["biamp_tesira"],
        "ports": [23],
        "transport": "tcp",
        "discovery": {
            "active_probes": ["tesira_ttp"],
            "oui_prefixes": ["00:90:5e"],
        },
        "compatible_models": [
            {
                "manufacturer": "Biamp",
                "models": [
                    "Tesira SERVER",
                    "Tesira SERVER-IO",
                    "TesiraFORTÉ AVB",
                    "TesiraFORTÉ CI",
                    "TesiraFORTÉ DAN",
                    "TesiraFORTÉ TI",
                    "TesiraFORTÉ VI",
                    "TesiraFORTÉ VT",
                    "TesiraFORTÉ X 400",
                    "TesiraFORTÉ X 800",
                    "TesiraFORTÉ X 1600",
                    "TesiraLUX",
                    "Tesira EX-MOD",
                    "Tesira EX-IN",
                    "Tesira EX-OUT",
                    "Tesira EX-AEC",
                ],
                "confidence": "untested",
                "notes": (
                    "Every Tesira / TesiraFORTÉ DSP that runs Tesira "
                    "software shares the same TTP protocol. Driver covers "
                    "the block types AV integrators commonly use for room "
                    "control. Setup: in Tesira's System Manager → Network "
                    "Settings, enable Discovery and Telnet (both default "
                    "OFF). Leave System Security disabled for the "
                    "no-credentials path used by this driver."
                ),
            },
        ],
        "help": {
            "overview": (
                "Comprehensive Biamp Tesira / TesiraFORTÉ control over TTP "
                "(Telnet, port 23). The driver subscribes to per-block, "
                "per-channel state changes — UI bindings update in "
                "real-time without polling. Declare every Tesira block you "
                "want to monitor or control in the 'DSP Block List' field "
                "below; the driver expands one state variable per "
                "(block, attribute, channel) and surfaces typed commands "
                "named after each block."
            ),
            "setup": (
                "STEP 1 — Enable network control on the DSP.\n"
                "Open Tesira software, connect to the unit, and go to "
                "Tools → Device Maintenance → Network Settings. Tick "
                "'Discovery Service' and 'Telnet' (both default OFF). "
                "Leave 'System Security' OFF too — this driver expects an "
                "unauthenticated Telnet session, which matches the standard "
                "deployment when the DSP is on a private AV VLAN. Apply.\n"
                "\n"
                "STEP 2 — Find your Instance Tags.\n"
                "Open the Tesira design (.tmf file) for this DSP. Each "
                "control block (Mute, Level, Source Selector, Mixer, AEC, "
                "Room Combiner, etc.) has a user-assigned 'Instance Tag' — "
                "right-click any block → Properties → top of the dialog. "
                "Tags are case-sensitive: 'Level1' and 'level1' are NOT "
                "the same. For multi-channel blocks (e.g. a 4-mic Mute), "
                "note how many channels are wired up — that's the channel "
                "count you'll declare below.\n"
                "\n"
                "STEP 3 — Enter the device IP address above.\n"
                "\n"
                "STEP 4 — Declare your blocks in the 'DSP Block List' "
                "field below.\n"
                "Format: one block per line, whitespace-separated\n"
                "    <INSTANCE_TAG> <TYPE> [CHANNELS|NxM]\n"
                "\n"
                "Examples (typical conferencing room):\n"
                "    Mute1     mute        1-4         # 4 mic mute channels\n"
                "    Level1    level       1-4         # 4 mic level channels\n"
                "    PgmMute   mute        1           # program-out mute\n"
                "    PgmLvl    level       1           # program-out level\n"
                "    PgmSrc    source_select           # source selector\n"
                "    AEC1      aec         1-2         # 2-channel AEC\n"
                "    Combiner1 room_combiner 1-4       # 4-wall combiner\n"
                "    PgmMix    matrix_mixer 8x4        # 8 inputs × 4 outputs\n"
                "\n"
                "Notes on the syntax:\n"
                "    • CHANNELS can be '1', '1-4' (range), or '1,3,5' (list)\n"
                "    • NxM for matrix_mixer is INPUTS × OUTPUTS (e.g. 8x4 = "
                "8 inputs, 4 outputs)\n"
                "    • If you omit CHANNELS, channel 1 is assumed\n"
                "    • Lines starting with '#' or ';' are comments — keep "
                "them as a reference or delete them\n"
                "    • The pre-filled textarea has a complete syntax guide\n"
                "\n"
                "STEP 5 — Save.\n"
                "Within seconds the device should show 'Connected'. Open "
                "Variables → Devices and you'll see one state variable per "
                "(block, attribute, channel) — e.g. 'Level1_level_3' for "
                "the third channel of the Level1 block. Bind those state "
                "keys to UI elements, or use the auto-named per-block "
                "commands (e.g. 'Level1: Set Level (dB)') in macros and "
                "panel buttons.\n"
                "\n"
                "Troubleshooting:\n"
                "    • 'Connection lost' loop on port 22 — that's SSH, not "
                "      supported. Use port 23 (Telnet).\n"
                "    • Device shows Connected but no state variables — "
                "      check that Telnet is enabled in System Manager AND "
                "      that your Instance Tags match exactly (case "
                "      matters).\n"
                "    • Some channels don't update — verify the channel "
                "      count in your block list matches the actual wired "
                "      channel count in the Tesira design.\n"
                "    • Need a block type the driver doesn't handle? Use "
                "      the generic 'Send Raw TTP Command' / 'Set "
                "      Attribute' commands — anything Tesira's command "
                "      string calculator generates will work."
            ),
        },
        "default_config": {
            "host": "",
            "port": 23,
            "blocks": DEFAULT_BLOCKS_TEXT,
            "subscribe_rate_ms": DEFAULT_SUBSCRIBE_RATE_MS,
            "inter_command_delay": DEFAULT_INTER_COMMAND_DELAY,
            "poll_interval": 0,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 23,
                "label": "Telnet Port",
                "min": 1,
                "max": 65535,
                "description": (
                    "Tesira's Telnet control port. Always 23 for stock "
                    "Tesira firmware. SSH (port 22) is not supported by "
                    "this driver — leave Telnet enabled in System Manager."
                ),
            },
            "blocks": {
                "type": "text",
                "label": "DSP Block List",
                "default": DEFAULT_BLOCKS_TEXT,
                "description": (
                    "List the Tesira blocks you want to monitor or "
                    "control — one per line. Format: "
                    "<INSTANCE_TAG> <TYPE> [CHANNELS|NxM]. The pre-filled "
                    "textarea has a full syntax guide; see Setup above "
                    "for a step-by-step walkthrough including how to find "
                    "Instance Tags in Tesira software."
                ),
            },
            "subscribe_rate_ms": {
                "type": "integer",
                "default": DEFAULT_SUBSCRIBE_RATE_MS,
                "min": 50,
                "max": 60000,
                "label": "Subscribe Rate (ms)",
                "description": (
                    "Minimum interval between push updates per attribute. "
                    "250 ms is responsive without flooding the wire. "
                    "Audio meters use a longer rate automatically."
                ),
            },
            "inter_command_delay": {
                "type": "number",
                "default": DEFAULT_INTER_COMMAND_DELAY,
                "min": 0,
                "label": "Inter-Command Delay (sec)",
                "description": (
                    "Delay between sequential outgoing commands. Tesira's "
                    "parser tolerates rapid bursts but a small delay keeps "
                    "subscribe ordering clean on connect."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "label": "Backstop Poll Interval (sec)",
                "description": (
                    "Refresh poll. Subscriptions are the source of truth — "
                    "leave at 0 unless you've seen state drift."
                ),
            },
        },
        # state_variables and commands are populated per-instance in __init__
        "state_variables": {},
        "commands": {},
    }

    # ── Lifecycle ──

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state: Any,
        events: Any,
    ) -> None:
        # Per-instance DRIVER_INFO so state_variables / commands reflect the
        # blocks declared in the user's config. Mirrors the Symetrix
        # Composer driver pattern.
        blocks_text = str(config.get("blocks", DEFAULT_BLOCKS_TEXT))
        self._blocks: list[dict[str, Any]] = parse_blocks_config(blocks_text)
        self._subscriptions: list[dict[str, Any]] = expand_subscriptions(self._blocks)
        # Map publishToken → subscription dict (for fast lookup on push parse)
        self._sub_by_token: dict[str, dict[str, Any]] = {
            s["token"]: s for s in self._subscriptions
        }

        self.DRIVER_INFO = {
            **type(self).DRIVER_INFO,
            "state_variables": expand_state_variables(self._blocks),
            "commands": expand_commands(self._blocks),
        }

        super().__init__(device_id, config, state, events)

        # Outstanding "get" queue: when send_command issues a get, we
        # remember the (state_key, type_hint) so the next +OK "value":...
        # response routes to that state var. FIFO.
        self._pending_gets: list[tuple[str, str]] = []
        # Lock around modifying _pending_gets and sending sequenced
        # request/response commands so concurrent get_attribute calls
        # don't interleave.
        self._get_lock = asyncio.Lock()

        # Saved frame parser used during the IAC handshake (we drop the
        # parser to raw mode for the handshake then restore the
        # \r\n-delimiter parser before normal operation).
        self._saved_frame_parser: Any = None

        # Welcome banner detection during connect()
        self._auth_buffer = bytearray()
        self._auth_event = asyncio.Event()
        self._auth_mode = False

    async def connect(self) -> None:
        """Open the TCP connection, run the IAC handshake, subscribe."""
        from server.transport.tcp import TCPTransport
        from server.system_config import get_system_config

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 23))
        if not host:
            raise ConnectionError(f"[{self.device_id}] No host configured")

        delay = float(self.config.get("inter_command_delay", DEFAULT_INTER_COMMAND_DELAY))
        control_ip = get_system_config().get("network", "control_interface")

        self._auth_buffer = bytearray()
        self._auth_event = asyncio.Event()
        self._auth_mode = True

        # Open in raw mode (delimiter=None) — IAC bytes arrive in arbitrary
        # frames and we need to see them byte-for-byte.
        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=None,
            inter_command_delay=delay,
            name=self.device_id,
            local_addr=(control_ip, 0) if control_ip else None,
        )

        # Run the IAC + welcome-banner handshake.
        try:
            await self._run_iac_handshake(timeout=10.0)
        except Exception:
            self._auth_mode = False
            if self.transport:
                try:
                    await self.transport.close()
                except Exception:
                    pass
                self.transport = None
            raise

        # Swap the transport's frame parser to \r\n delimiter mode for
        # normal command/response framing. Mirrors the parser-swap pattern
        # in ConfigurableDriver._perform_auth_handshake.
        new_parser = DelimiterFrameParser(LINE_DELIMITER)
        # Push any banner-trailing bytes through the new parser so the
        # first real command's response isn't lost.
        leftover = bytes(self._auth_buffer)
        self._auth_buffer = bytearray()
        self._auth_mode = False
        if hasattr(self.transport, "_frame_parser"):
            self.transport._frame_parser = new_parser
        # If anything came in past the welcome banner, feed it through the
        # parser now. Most of the time this is empty.
        if leftover:
            for msg in new_parser.feed(leftover):
                await self.on_data_received(msg)

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to Tesira at {host}:{port}")

        # Settle the session: turn off verbose so we don't get echoes,
        # then probe for serial / firmware (best-effort, ignore errors).
        try:
            await self._send_line("SESSION set verbose false")
            await self._send_line("SESSION set aliasUsage true")
            await self._send_line('DEVICE get serialNumber')
            self._pending_gets.append(("serial_number", "string"))
            await self._send_line('DEVICE get version')
            self._pending_gets.append(("firmware_version", "string"))
            await self._send_line('DEVICE get hostname')
            self._pending_gets.append(("device_id_str", "string"))
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial session setup failed")

        # Re-subscribe to every declared block. This also runs after
        # reconnect — Tesira subscriptions are session-scoped.
        await self._subscribe_all()

        # Initial GET for every subscribed attribute so panel UIs reflect
        # current values immediately.
        await self._initial_get_all()

        poll_interval = int(self.config.get("poll_interval", 0))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            try:
                # Polite quit so the device frees its session slot promptly.
                await self._send_line("SESSION quit")
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

    # ── IAC handshake ──

    async def _run_iac_handshake(self, timeout: float) -> None:
        """Wait for the welcome banner while replying WONT/DONT to IAC.

        Tesira sends one or more IAC option negotiation sequences as soon
        as the TCP socket opens. Per RFC 854/855 we reply WONT to every DO
        and DONT to every WILL, telling the device "we're a dumb pipe."
        Once the device has finished negotiation it sends the welcome
        banner; we wait for that, then return.
        """
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            # Process any pending IAC sequences in the buffer
            await self._process_iac_buffer()

            # Decode current buffer and look for the welcome banner
            text = bytes(self._auth_buffer).decode("utf-8", errors="replace")
            if WELCOME_BANNER.decode() in text:
                log.info(f"[{self.device_id}] Welcome banner received")
                # Trim everything up to and including the banner — we don't
                # want it to feed into the response parser later.
                idx = self._auth_buffer.find(WELCOME_BANNER)
                if idx >= 0:
                    after = self._auth_buffer[idx + len(WELCOME_BANNER):]
                    # The banner is followed by \r\n which we also discard
                    while after.startswith(b"\r") or after.startswith(b"\n"):
                        after = after[1:]
                    self._auth_buffer = bytearray(after)
                return

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                # Some firmware variants don't emit the canonical banner
                # but DO accept commands once IAC negotiation is settled.
                # If we have ANY content beyond IAC bytes, log a warning
                # and proceed.
                if self._auth_buffer:
                    log.warning(
                        f"[{self.device_id}] Welcome banner timeout; "
                        f"proceeding with whatever's in the buffer "
                        f"({len(self._auth_buffer)} bytes)"
                    )
                    return
                raise ConnectionError(
                    f"[{self.device_id}] Timeout waiting for Tesira welcome banner"
                )

            try:
                await asyncio.wait_for(self._auth_event.wait(), timeout=min(remaining, 0.5))
            except asyncio.TimeoutError:
                # Periodic re-check; loop again
                pass
            self._auth_event.clear()

    async def _process_iac_buffer(self) -> None:
        """Strip IAC sequences from the auth buffer, sending replies inline."""
        if self.transport is None:
            return

        # Walk the buffer looking for IAC sequences. An IAC sequence is
        # always 3 bytes: IAC + (DO/DONT/WILL/WONT) + option. IAC IAC means
        # an escaped 0xFF in the data stream; we leave that alone.
        # Build up a "clean" buffer with IAC sequences removed.
        i = 0
        cleaned = bytearray()
        replies: list[bytes] = []
        buf = self._auth_buffer
        while i < len(buf):
            b = buf[i]
            if b != IAC:
                cleaned.append(b)
                i += 1
                continue
            # We hit an IAC. Need at least 3 bytes total.
            if i + 2 >= len(buf):
                # Incomplete sequence at end of buffer — keep the partial
                # bytes for the next pass.
                cleaned.extend(buf[i:])
                break
            cmd = buf[i + 1]
            opt = buf[i + 2]
            if cmd == IAC:
                # Escaped 0xFF — emit a single 0xFF, advance by 2
                cleaned.append(IAC)
                i += 2
                continue
            if cmd == DO:
                replies.append(bytes([IAC, WONT, opt]))
            elif cmd == WILL:
                replies.append(bytes([IAC, DONT, opt]))
            # DONT/WONT — no reply needed
            i += 3

        self._auth_buffer = cleaned

        # Send all replies coalesced
        if replies:
            payload = b"".join(replies)
            try:
                await self.transport.send(payload)
                log.debug(
                    f"[{self.device_id}] IAC negotiation: sent "
                    f"{len(replies)} option replies"
                )
            except (ConnectionError, OSError) as e:
                log.warning(f"[{self.device_id}] IAC reply send failed: {e}")

    # ── Sending ──

    async def _send_line(self, line: str) -> None:
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send((line + "\n").encode("utf-8"))

    async def _subscribe_all(self) -> None:
        """Send a subscribe command for every declared subscription."""
        if not self._subscriptions:
            return
        rate_default = int(self.config.get("subscribe_rate_ms", DEFAULT_SUBSCRIBE_RATE_MS))
        for sub in self._subscriptions:
            cmd = self._build_subscribe_command(sub, rate_default)
            try:
                await self._send_line(cmd)
            except (ConnectionError, OSError):
                log.warning(f"[{self.device_id}] Subscribe send failed: {cmd}")
                return
        log.info(
            f"[{self.device_id}] Subscribed to {len(self._subscriptions)} "
            f"attributes across {len(self._blocks)} blocks"
        )

    async def _initial_get_all(self) -> None:
        """Issue a one-shot GET for every subscribed attribute."""
        if not self._subscriptions:
            return
        for sub in self._subscriptions:
            cmd = self._build_get_command(sub)
            try:
                await self._send_line(cmd)
                self._pending_gets.append((sub["token"], sub["type_hint"]))
            except (ConnectionError, OSError):
                log.warning(f"[{self.device_id}] Initial GET send failed: {cmd}")
                return

    @staticmethod
    def _format_index(idx: Any) -> list[str]:
        """Render a TTP index spec as a list of string tokens.

        Indexless attributes -> []. Single-index -> ["<n>"]. Two-index
        attributes (crosspointLevel, crosspointLevelState) -> ["<i>","<j>"].
        Tuples cover both two-index attrs and any future N-dimensional ones.
        """
        if idx is None or idx == "":
            return []
        if isinstance(idx, (tuple, list)):
            return [str(x) for x in idx]
        return [str(idx)]

    @classmethod
    def _build_subscribe_command(cls, sub: dict[str, Any], rate_default: int) -> str:
        rate = int(sub.get("rate_ms") or rate_default)
        parts = [sub["tag"], "subscribe", sub["attribute"]]
        parts.extend(cls._format_index(sub["index"]))
        parts.append(f'"{sub["token"]}"')
        parts.append(str(rate))
        return " ".join(parts)

    @classmethod
    def _build_get_command(cls, sub: dict[str, Any]) -> str:
        parts = [sub["tag"], "get", sub["attribute"]]
        parts.extend(cls._format_index(sub["index"]))
        return " ".join(parts)

    @classmethod
    def _build_unsubscribe_command(cls, tag: str, attr: str, idx: Any, token: str) -> str:
        parts = [tag, "unsubscribe", attr]
        parts.extend(cls._format_index(idx))
        parts.append(f'"{token}"')
        return " ".join(parts)

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        # During the IAC handshake, capture all bytes raw and let the
        # handshake state machine decide what to do.
        if self._auth_mode:
            self._auth_buffer.extend(data)
            self._auth_event.set()
            await self._process_iac_buffer()
            return

        # Otherwise the transport's frame parser hands us one stripped
        # line per call.
        line = data.decode("utf-8", errors="replace").rstrip("\r\n").strip()
        if not line:
            return

        self.set_state("last_raw_response", line)
        self._handle_response_line(line)

    def _handle_response_line(self, line: str) -> None:
        """Dispatch a single response line."""
        # Subscription push — `! "publishToken":"NAME" "value":VALUE`
        m = PUSH_LINE_RE.match(line)
        if m:
            token = m.group(1)
            value_str = m.group(2).strip()
            self._handle_push(token, value_str)
            return

        # Error
        m = ERR_RE.match(line)
        if m:
            self.set_state("last_error", m.group(1))
            log.debug(f"[{self.device_id}] DSP error: {m.group(1)}")
            # Drop the oldest pending-get so we don't pin the head of the
            # queue forever on an error response.
            if self._pending_gets:
                self._pending_gets.pop(0)
            return

        # Successful GET response: +OK "value":...
        m = OK_VALUE_RE.match(line)
        if m:
            value_str = m.group(1).strip()
            if self._pending_gets:
                state_key, type_hint = self._pending_gets.pop(0)
                coerced = self._coerce_response_value(value_str, type_hint)
                if coerced is not None:
                    self.set_state(state_key, coerced)
                # Also surface in last_query_result for visibility from macros
                self.set_state("last_query_result", value_str)
            else:
                # Unsolicited +OK "value":... — typically the immediate
                # echo from a subscribe (Tesira sends initial value after
                # subscribe), use it as the latest known value if a
                # publishToken match is buried in the line. Companion
                # parses these via the same regex; we already do above.
                self.set_state("last_query_result", value_str)
            return

        # Successful GET list response
        m = OK_LIST_RE.match(line)
        if m:
            value_str = m.group(1).strip()
            if self._pending_gets:
                self._pending_gets.pop(0)
            self.set_state("last_query_result", value_str)
            return

        # Plain +OK ack — no payload
        if line == "+OK" or line.startswith("+OK"):
            return

        # Anything else: stash for visibility, log debug.
        log.debug(f"[{self.device_id}] Unhandled line: {line!r}")

    def _handle_push(self, token: str, value_str: str) -> None:
        sub = self._sub_by_token.get(token)
        # If we don't recognize the token (e.g. user added a runtime
        # subscribe via subscribe_attribute), still surface the value
        # under that token name as a state var so scripts can read it.
        type_hint = sub["type_hint"] if sub else "string"

        # Array values: "value":[1.0 2.0 3.0]
        if value_str.startswith("["):
            self._handle_array_push(token, value_str, type_hint)
            return

        coerced = self._coerce_response_value(value_str, type_hint)
        if coerced is None:
            return
        if sub is None:
            # Register a synthetic state var for unknown tokens so the
            # value is observable. set_state validates against
            # DRIVER_INFO state_variables — bypass by writing directly.
            self.state.set(f"device.{self.device_id}.{token}", coerced)
        else:
            self.set_state(token, coerced)

    def _handle_array_push(self, token: str, value_str: str, type_hint: str) -> None:
        """Handle 'value':[a b c] — fan out to <token>_1, <token>_2, ..."""
        # Strip surrounding [] and split on whitespace, honoring quoted
        # strings (Tesira sometimes emits string arrays).
        body = value_str.strip()
        if body.startswith("[") and body.endswith("]"):
            body = body[1:-1]
        parts = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', body)
        elements = [(q or u) for q, u in parts]

        for i, elem in enumerate(elements, start=1):
            coerced = self._coerce_response_value(elem, type_hint)
            if coerced is None:
                continue
            key = f"{token}_{i}"
            self.state.set(f"device.{self.device_id}.{key}", coerced)

    @staticmethod
    def _coerce_response_value(raw: str, type_hint: str) -> Any:
        """Convert a raw TTP response value to the declared state type.

        TTP scalars look like:
            -12.500000           number
            true | false         boolean
            "some string"        quoted string
            42                   integer

        type_hint is one of: number, integer, boolean, string.
        Returns None for unparseable input (caller skips state update).
        """
        if raw is None:
            return None
        s = raw.strip()
        # Strip a leading +OK if it slipped through
        if s.startswith("+OK"):
            s = s[3:].strip()
        # Strip surrounding quotes
        if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
            s = s[1:-1].replace('\\"', '"')

        if type_hint == "boolean":
            low = s.lower()
            if low in ("true", "1", "on"):
                return True
            if low in ("false", "0", "off"):
                return False
            return None
        if type_hint == "integer":
            try:
                return int(float(s))  # tolerate "1.000000"
            except (ValueError, TypeError):
                return None
        if type_hint == "number":
            try:
                return float(s)
            except (ValueError, TypeError):
                return None
        # string / fallback
        return s

    # ── Command dispatch ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        # Generic escape-hatch commands
        if command == "set_attribute":
            return await self._cmd_set_attribute(
                params["tag"], params["attribute"],
                params.get("index", ""), params["value"],
            )
        if command == "get_attribute":
            return await self._cmd_get_attribute(
                params["tag"], params["attribute"], params.get("index", ""),
            )
        if command == "toggle_attribute":
            return await self._send_attribute_action(
                "toggle", params["tag"], params["attribute"],
                params.get("index", ""), None,
            )
        if command == "increment_attribute":
            return await self._send_attribute_action(
                "increment", params["tag"], params["attribute"],
                params.get("index", ""), params["amount"],
            )
        if command == "decrement_attribute":
            return await self._send_attribute_action(
                "decrement", params["tag"], params["attribute"],
                params.get("index", ""), params["amount"],
            )
        if command == "recall_preset":
            await self._send_line(f"DEVICE recallPreset {int(params['preset_id'])}")
            self.set_state("last_preset", str(params["preset_id"]))
            return True
        if command == "recall_preset_by_name":
            name = str(params["name"]).replace('"', '\\"')
            await self._send_line(f'DEVICE recallPresetByName "{name}"')
            self.set_state("last_preset", str(params["name"]))
            return True
        if command == "save_preset":
            await self._send_line(f"DEVICE savePreset {int(params['preset_id'])}")
            return True
        if command == "send_raw":
            await self._send_line(str(params["command"]))
            return True
        if command == "subscribe_attribute":
            return await self._cmd_subscribe(
                params["tag"], params["attribute"],
                params.get("index", ""), params["token"],
                int(params.get("rate_ms", DEFAULT_SUBSCRIBE_RATE_MS)),
            )
        if command == "unsubscribe_attribute":
            return await self._cmd_unsubscribe(
                params["tag"], params["attribute"],
                params.get("index", ""), params["token"],
            )
        if command == "session_quit":
            await self._send_line("SESSION quit")
            return True

        # Per-block commands — name format is <safe_tag>_<action>[_<modifier>]
        return await self._dispatch_per_block_command(command, params)

    async def _send_attribute_action(
        self, action: str, tag: str, attr: str,
        index: Any, amount: Any,
    ) -> bool:
        parts = [tag, action, attr]
        if index not in (None, ""):
            parts.append(str(index))
        if amount is not None:
            parts.append(self._format_value(amount))
        await self._send_line(" ".join(parts))
        return True

    async def _cmd_set_attribute(
        self, tag: str, attr: str, index: Any, value: Any,
    ) -> bool:
        parts = [tag, "set", attr]
        if index not in (None, ""):
            parts.append(str(index))
        parts.append(self._format_value(value))
        await self._send_line(" ".join(parts))
        return True

    async def _cmd_get_attribute(
        self, tag: str, attr: str, index: Any,
    ) -> bool:
        parts = [tag, "get", attr]
        if index not in (None, ""):
            parts.append(str(index))
        async with self._get_lock:
            await self._send_line(" ".join(parts))
            self._pending_gets.append(("last_query_result", "string"))
        return True

    async def _cmd_subscribe(
        self, tag: str, attr: str, index: Any, token: str, rate_ms: int,
    ) -> bool:
        sub = {
            "tag": tag,
            "attribute": attr,
            "index": index if index not in (None, "") else None,
            "token": token,
            "rate_ms": rate_ms,
            "type_hint": "string",  # caller is on their own for typing
        }
        cmd = self._build_subscribe_command(sub, rate_ms)
        await self._send_line(cmd)
        # Track for reconnect re-subscribe and for token lookup
        # (replace any existing entry with the same token).
        self._subscriptions = [s for s in self._subscriptions if s["token"] != token]
        self._subscriptions.append(sub)
        self._sub_by_token[token] = sub
        return True

    async def _cmd_unsubscribe(
        self, tag: str, attr: str, index: Any, token: str,
    ) -> bool:
        idx = index if index not in (None, "") else None
        cmd = self._build_unsubscribe_command(tag, attr, idx, token)
        await self._send_line(cmd)
        self._subscriptions = [s for s in self._subscriptions if s["token"] != token]
        self._sub_by_token.pop(token, None)
        return True

    @staticmethod
    def _format_value(value: Any) -> str:
        """Format a Python value as a TTP wire token.

        Booleans render as 'true'/'false' (lowercase), numbers stay as
        their string repr, strings get quoted to handle whitespace.
        """
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        s = str(value)
        # Already-typed bool strings pass through
        if s.lower() in ("true", "false"):
            return s.lower()
        # Quote strings that contain whitespace or special chars
        if re.match(r"^-?\d+(\.\d+)?$", s):
            return s
        if " " in s or "\t" in s or '"' in s:
            escaped = s.replace('"', '\\"')
            return f'"{escaped}"'
        return s

    # ── Per-block command dispatch ──

    async def _dispatch_per_block_command(
        self, command: str, params: dict[str, Any],
    ) -> Any:
        """Match a per-block command name to the right TTP wire command."""
        # Find the block whose safe-tag is a prefix
        for blk in self._blocks:
            safe = _safe_token(blk["tag"])
            if not command.startswith(safe + "_"):
                continue
            action = command[len(safe) + 1:]
            return await self._dispatch_block_action(blk, action, params)
        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    async def _dispatch_block_action(
        self, blk: dict[str, Any], action: str, params: dict[str, Any],
    ) -> Any:
        bt = blk["type"]
        tag = blk["tag"]

        if bt in ("mute",) and action == "mute_set":
            return await self._cmd_set_attribute(
                tag, "mute", params["channel"], params["value"]
            )
        if bt in ("mute",) and action == "mute_toggle":
            return await self._send_attribute_action(
                "toggle", tag, "mute", params["channel"], None
            )
        if bt == "level" and action == "level_set":
            return await self._cmd_set_attribute(
                tag, "level", params["channel"], params["level_db"]
            )
        if bt == "level" and action == "level_step":
            amt = float(params["amount_db"])
            verb = "increment" if amt >= 0 else "decrement"
            return await self._send_attribute_action(
                verb, tag, "level", params["channel"], abs(amt)
            )
        if bt == "level" and action == "level_ramp":
            # Tesira level ramp: <TAG> set rampLevel <ch> <dB> <seconds>
            # (Some firmware variants use 'ramp' as the attribute name; the
            # canonical TTP command is set rampLevel.)
            ch = int(params["channel"])
            target = float(params["target_db"])
            duration = float(params["duration_s"])
            await self._send_line(
                f"{tag} set rampLevel {ch} {target} {duration}"
            )
            return True
        if bt == "level" and action == "mute_set":
            return await self._cmd_set_attribute(
                tag, "mute", params["channel"], params["value"]
            )
        if bt == "level" and action == "mute_toggle":
            return await self._send_attribute_action(
                "toggle", tag, "mute", params["channel"], None
            )
        if bt == "source_select" and action == "select_source":
            return await self._cmd_set_attribute(
                tag, "sourceSelection", None, params["source"]
            )
        if bt == "source_select" and action == "set_output_level":
            return await self._cmd_set_attribute(
                tag, "outputLevel", None, params["level_db"]
            )
        if bt == "source_select" and action == "output_mute_set":
            return await self._cmd_set_attribute(
                tag, "outputMute", None, params["value"]
            )
        if bt == "matrix_mixer" and action == "xpoint_set":
            i = int(params["input_n"])
            o = int(params["output_n"])
            val = self._format_value(params["value"])
            await self._send_line(
                f"{tag} set crosspointLevelState {i} {o} {val}"
            )
            return True
        if bt == "matrix_mixer" and action == "xpoint_level":
            i = int(params["input_n"])
            o = int(params["output_n"])
            db = float(params["level_db"])
            await self._send_line(
                f"{tag} set crosspointLevel {i} {o} {db}"
            )
            return True
        if bt == "matrix_mixer" and action == "output_level_set":
            return await self._cmd_set_attribute(
                tag, "outputLevel", params["output_n"], params["level_db"]
            )
        if bt == "matrix_mixer" and action == "output_mute_set":
            return await self._cmd_set_attribute(
                tag, "outputMute", params["output_n"], params["value"]
            )
        if bt == "router" and action == "route":
            return await self._cmd_set_attribute(
                tag, "output", params["output_n"], params["source"]
            )
        if bt == "automixer" and action == "channel_mute_set":
            return await self._cmd_set_attribute(
                tag, "channelMute", params["channel"], params["value"]
            )
        if bt == "automixer" and action == "channel_level_set":
            return await self._cmd_set_attribute(
                tag, "channelLevel", params["channel"], params["level_db"]
            )
        if bt == "room_combiner" and action == "set_group":
            return await self._cmd_set_attribute(
                tag, "group", params["wall"], params["group"]
            )
        if bt == "room_combiner" and action == "set_combine":
            return await self._cmd_set_attribute(
                tag, "combine", params["wall"], params["value"]
            )
        if bt == "logic" and action == "set_state":
            return await self._cmd_set_attribute(
                tag, "state", params["channel"], params["value"]
            )
        if bt == "generator" and action == "set_amplitude":
            return await self._cmd_set_attribute(
                tag, "amplitude", None, params["level_db"]
            )
        if bt == "generator" and action == "set_frequency":
            return await self._cmd_set_attribute(
                tag, "frequency", None, params["hz"]
            )
        if bt == "generator" and action == "enable":
            return await self._cmd_set_attribute(
                tag, "generatorEnable", None, params["value"]
            )
        if bt == "dialer" and action == "dial":
            num = str(params["number"]).replace('"', '\\"')
            line = int(params.get("line", 1))
            await self._send_line(f'{tag} dial {line} "{num}"')
            return True
        if bt == "dialer" and action == "hangup":
            line = int(params.get("line", 1))
            await self._send_line(f"{tag} end {line}")
            return True
        if bt == "dialer" and action == "answer":
            line = int(params.get("line", 1))
            await self._send_line(f"{tag} answer {line}")
            return True
        if bt == "dialer" and action == "dtmf":
            digits = str(params["digits"]).replace('"', '\\"')
            line = int(params.get("line", 1))
            await self._send_line(f'{tag} dtmf {line} "{digits}"')
            return True
        if bt == "voip_rx" and action == "set_mute":
            return await self._cmd_set_attribute(
                tag, "mute", None, params["value"]
            )

        log.warning(
            f"[{self.device_id}] Unhandled per-block action: "
            f"block={tag} type={bt} action={action}"
        )
        return None

    # ── Polling backstop (only fires if poll_interval > 0) ──

    async def poll(self) -> None:
        """Re-issue GET on every subscribed attribute as a refresh backstop.

        Subscriptions are reliable on Tesira but a periodic refresh
        catches any missed pushes (network blips, device restarts where
        the session survived). Default config has poll_interval=0 so this
        doesn't run unless the integrator opts in.
        """
        if not self.transport or not self.transport.connected:
            return
        for sub in self._subscriptions:
            try:
                await self._send_line(self._build_get_command(sub))
                self._pending_gets.append((sub["token"], sub["type_hint"]))
            except (ConnectionError, OSError):
                return
