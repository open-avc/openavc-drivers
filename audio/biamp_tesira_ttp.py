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
    set of blocks, and each block type exposes a different control set (a
    Level block vs a Matrix Mixer vs a Dialer). ConfigurableDriver has no
    per-instance child roster with per-child dynamic schemas. So — following
    the qsc_qrc reference — each declared block is registered as a dynamic
    `block` child at connect, with a per-child schema built from the block
    type + channels. Panel UIs bind to child properties
    (device.<id>.block.<tag>.<control>), and one set of child-scoped commands
    (set_control / step_control / ... with a block picker + control cascade)
    drives every block type.

Why subscriptions, not polling (rules.md Principle 2):
    Tesira natively pushes state-change notifications via the `subscribe`
    command. The driver enables a session subscription per declared
    (block, attribute, channel) on connect (and again after every reconnect
    — Tesira subscriptions are session-scoped). It also issues a one-shot
    `get` for each subscribed attribute so panel UIs reflect current values
    immediately even if no change happens to fire a push.

Liveness watchdog:
    A push-subscription session with poll_interval=0 sends nothing once
    subscribed, so a link that dies without a FIN (cable pull, DSP power
    loss) is invisible to the transport — the device would stay shown
    online forever. The BaseDriver health loop probes `DEVICE get version`
    every HEALTH_INTERVAL_S and awaits the reply through the pending-GET
    FIFO; consecutive misses force a reconnect with a typed `no_response`
    fault. A subscription push does NOT satisfy the probe (only a reply
    that consumes the probe's FIFO entry does).

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

from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import DelimiterFrameParser
from openavc.utils.logger import get_logger

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

# Block-type vocabulary the parser accepts (canonical types + hand-typed
# aliases below). The canonical types back the table editor's Block Type
# dropdown; aliases stay parseable for a legacy string config.
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

# Aliases the grammar accepts (hand-typed convenience) mapped to the canonical
# block type. The table editor only offers the canonical values; a legacy
# string may still carry an alias, so parsing normalizes both.
_BLOCK_TYPE_ALIASES = {
    "fader": "level",
    "source_selector": "source_select",
    "mixer": "matrix_mixer",
    "tone_generator": "generator",
    "voip_receive": "voip_rx",
    "meter": "audio_meter",
}

# Columns for the `blocks` `type: table` config field. The integrator declares
# one row per Tesira block on the device page; the enum offers the canonical
# block types (aliases above stay parseable for legacy string configs).
# Declared once and reused in config_schema so the device-page table editor
# renders the right widgets.
BLOCK_COLUMNS = {
    "tag": {
        "type": "string", "label": "Instance Tag", "required": True,
        "help": "The block's Instance Tag from your Tesira design "
                "(case-sensitive, must match exactly).",
    },
    "type": {
        "type": "enum", "label": "Block Type", "required": True,
        "values": [
            {"value": "mute", "label": "Mute"},
            {"value": "level", "label": "Level / Fader"},
            {"value": "source_select", "label": "Source Selector"},
            {"value": "matrix_mixer", "label": "Matrix Mixer (NxM)"},
            {"value": "automixer", "label": "Automixer"},
            {"value": "router", "label": "Router"},
            {"value": "logic", "label": "Logic State"},
            {"value": "logic_meter", "label": "Logic Meter"},
            {"value": "aec", "label": "AEC Processor"},
            {"value": "room_combiner", "label": "Room Combiner"},
            {"value": "audio_meter", "label": "Audio Meter"},
            {"value": "signal_present", "label": "Signal Present"},
            {"value": "generator", "label": "Tone Generator"},
            {"value": "voip_rx", "label": "VoIP Receive"},
            {"value": "dialer", "label": "VoIP Dialer"},
            {"value": "preset", "label": "Preset"},
        ],
        "help": "The Tesira block class — determines the controls exposed.",
    },
    "channels": {
        "type": "string", "label": "Channels / NxM",
        "help": "Channel spec for per-channel blocks (1, 1-4, or 1,3,5); "
                "inputs x outputs for a matrix mixer (e.g. 8x4); blank = "
                "channel 1.",
    },
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


def _build_block(tag: str, type_token: str, spec: str) -> dict[str, Any]:
    """Expand one (tag, type, channels/NxM) triple into a block dict.

    Shared by the row-list config path and the legacy-string converter so both
    apply the same alias normalization, channel/matrix parsing, and defaults.

    Returns {"tag", "type", "channels": [...], "extra": {...}}. For
    matrix_mixer, "extra" carries {"inputs", "outputs"} and "channels" is
    empty. For source_select / voip_rx / dialer / generator / preset,
    "channels" is empty. For mute/level/etc., "channels" is the channel index
    list (e.g. [1,2,3,4] for "1-4"). An unknown type yields type="unknown" so
    the driver logs a warning rather than crashing on a typo.
    """
    type_token = (type_token or "").strip().lower()
    if type_token not in BLOCK_TYPES:
        log.warning(
            f"Tesira blocks parse: unknown block type {type_token!r} for "
            f"tag {tag!r}; valid types: {sorted(BLOCK_TYPES)}"
        )
        return {"tag": tag, "type": "unknown", "raw_type": type_token,
                "channels": [], "extra": {}}

    canonical_type = _BLOCK_TYPE_ALIASES.get(type_token, type_token)
    block: dict[str, Any] = {
        "tag": tag,
        "type": canonical_type,
        "channels": [],
        "extra": {},
    }

    if canonical_type == "matrix_mixer":
        inputs, outputs = _parse_matrix_spec(spec)
        if inputs == 0 or outputs == 0:
            # Default to 4x4 if the NxM spec is missing / malformed.
            inputs, outputs = 4, 4
            log.warning(
                f"Tesira blocks parse: matrix_mixer {tag!r} missing "
                f"NxM spec, defaulting to 4x4"
            )
        block["extra"] = {"inputs": inputs, "outputs": outputs}
    elif canonical_type in ("source_select", "voip_rx", "dialer", "generator", "preset"):
        # No channel spec needed.
        pass
    else:
        channels = _parse_channel_spec(spec)
        if not channels:
            # Default to single channel 1 if the channel spec is omitted.
            channels = [1]
        block["channels"] = channels

    return block


def _blocks_text_to_rows(text: str) -> list[dict[str, Any]]:
    """One-shot converter: the legacy `<TAG> <TYPE> [CHANNELS|NxM]` textarea
    -> table rows.

    The block list used to be a `type: text` field the integrator hand-typed
    one-per-line; it is now a `type: table`. A project saved before the table
    editor stores a string here — convert it (reusing the old line grammar) so
    it still loads and can be re-authored in the row editor without hand
    migration. ``#``/``;`` comment/blank lines and inline comments are dropped;
    a line missing a type is dropped with a warning.
    """
    rows: list[dict[str, Any]] = []
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

        row: dict[str, Any] = {"tag": parts[0], "type": parts[1].lower()}
        if len(parts) >= 3:
            row["channels"] = parts[2]
        rows.append(row)
    return rows


def parse_blocks_config(value: Any) -> list[dict[str, Any]]:
    """Parse the `blocks` config into a list of block dicts.

    Accepts the `type: table` row list (``[{"tag", "type", "channels"}, ...]``)
    and, for a project saved before the table editor shipped, a legacy
    `<TAG> <TYPE> [CHANNELS|NxM]` textarea string (converted to rows first).
    Each row is expanded by :func:`_build_block` (same normalization for both
    paths). A row with no tag is skipped.
    """
    if isinstance(value, str):
        value = _blocks_text_to_rows(value)
    blocks: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return blocks
    for row in value:
        if not isinstance(row, dict):
            continue
        tag = str(row.get("tag", "")).strip()
        if not tag:
            log.warning("Tesira blocks parse: row with no instance tag, skipping")
            continue
        type_token = str(row.get("type", "") or "")
        channels = row.get("channels", "")
        spec = "" if channels is None else str(channels).strip()
        blocks.append(_build_block(tag, type_token, spec))
    return blocks


# ── Child entities, state, schema, subscriptions, commands ──

# One dynamic child entity type: every declared Tesira block becomes a
# "block" child whose per-instance schema (the controls it exposes) is
# published at register_child(schema=...). This follows the qsc_qrc
# reference — the roster and each child's control set are known only from
# the user's declared block list, so a per-child dynamic schema is the
# right shape. (Contrast a config-SIZED static roster where every child
# shares one schema; a Tesira level block and a matrix mixer expose
# entirely different controls.)
BLOCK_CHILD_TYPE = "block"

# Summary fields shared by every block child. For a dynamic child type the
# per-child schema supplied at register_child(schema=...) is the child's full
# schema (the type-level state_variables don't merge in), so these are also
# spliced into each block's schema at registration — see _register_blocks.
_BLOCK_SUMMARY_SCHEMA = {
    "name": {"type": "string", "label": "Instance Tag"},
    "block_type": {"type": "string", "label": "Block Type"},
    "channels": {"type": "string", "label": "Channels"},
}

BLOCK_CHILD_TYPES = {
    BLOCK_CHILD_TYPE: {
        "label": "DSP Block",
        "label_plural": "DSP Blocks",
        "dynamic": True,
        "id_format": {"type": "string", "max_length": 128},
        "state_variables": dict(_BLOCK_SUMMARY_SCHEMA),
        "summary_fields": ["block_type", "channels"],
        "label_field": "name",
    },
}


def system_state_variables() -> dict[str, dict[str, Any]]:
    """Device-level state vars present on every instance. Per-block/per-channel
    values live on child entities now (device.<id>.block.<tag>.<control>),
    not flat keys."""
    return {
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


# ── Per-child var-def helpers ──
#
# cloud_priority follows the console convention (mute + on/off high, levels
# and meters low). `control: true` marks the props a panel control binds to
# and the ones the set_control / step_control pickers offer.

def _level(label: str) -> dict[str, Any]:
    return {"type": "number", "label": label, "min": -100, "max": 12,
            "control": True, "cloud_priority": "low"}


def _mute(label: str) -> dict[str, Any]:
    return {"type": "boolean", "label": label, "control": True,
            "cloud_priority": "high"}


def _ctl_bool(label: str) -> dict[str, Any]:
    return {"type": "boolean", "label": label, "control": True,
            "cloud_priority": "high"}


def _ctl_int(label: str) -> dict[str, Any]:
    return {"type": "integer", "label": label, "min": 0, "control": True,
            "cloud_priority": "low"}


def _ctl_num(label: str) -> dict[str, Any]:
    # A settable number with no documented device-independent range. No
    # min/max on purpose — a bound narrower than the device's true range
    # would block valid commands at the dispatch gate (first-class G3 rule).
    return {"type": "number", "label": label, "control": True,
            "cloud_priority": "low"}


def _meter(label: str) -> dict[str, Any]:
    # Read-only measured value (meter, gain reduction) — not a control.
    return {"type": "number", "label": label, "cloud_priority": "low"}


def _ro_bool(label: str) -> dict[str, Any]:
    return {"type": "boolean", "label": label, "cloud_priority": "low"}


def _channels_label(block: dict[str, Any]) -> str:
    """Human summary of a block's channel span for the child summary row."""
    if block["type"] == "matrix_mixer":
        extra = block.get("extra", {})
        return f"{extra.get('inputs', 0)}x{extra.get('outputs', 0)}"
    chans = block.get("channels") or []
    if not chans:
        return ""
    if len(chans) > 1 and chans == list(range(chans[0], chans[-1] + 1)):
        return f"{chans[0]}-{chans[-1]}"
    return ",".join(str(c) for c in chans)


def _expand_block(block: dict[str, Any]) -> tuple[
    dict[str, dict[str, Any]], dict[str, tuple[str, Any]], list[dict[str, Any]]
]:
    """Expand one parsed block into (schema, wire, subs).

    - schema: {prop: var-def} — the child's per-instance dynamic schema.
    - wire:   {prop: (attribute, index)} — maps a child prop back to the TTP
              attribute + index for set / toggle / step / get.
    - subs:   [{prop, attribute, index, type_hint, rate_ms}] — the props to
              push-subscribe on connect. Every settable prop is in `schema`
              and `wire`; only the subscribe set is gated (a very large
              crosspoint grid isn't auto-subscribed, but stays settable).
    """
    bt = block["type"]
    chans = block["channels"]
    extra = block["extra"]
    schema: dict[str, dict[str, Any]] = {}
    wire: dict[str, tuple[str, Any]] = {}
    subs: list[dict[str, Any]] = []

    def add(prop, var_def, attribute, index, type_hint,
            subscribe=True, rate_ms=DEFAULT_SUBSCRIBE_RATE_MS):
        schema[prop] = var_def
        wire[prop] = (attribute, index)
        if subscribe:
            subs.append({"prop": prop, "attribute": attribute, "index": index,
                         "type_hint": type_hint, "rate_ms": rate_ms})

    if bt == "mute":
        for ch in chans:
            add(f"mute_{ch}", _mute(f"Mute Ch {ch}"), "mute", ch, "boolean")
    elif bt == "level":
        for ch in chans:
            add(f"level_{ch}", _level(f"Level Ch {ch} (dB)"), "level", ch, "number")
            add(f"mute_{ch}", _mute(f"Mute Ch {ch}"), "mute", ch, "boolean")
    elif bt == "source_select":
        add("source", _ctl_int("Selected Source"), "sourceSelection", None, "integer")
        add("output_level", _level("Output Level (dB)"), "outputLevel", None, "number")
        add("output_mute", _mute("Output Mute"), "outputMute", None, "boolean")
    elif bt == "matrix_mixer":
        inputs = extra.get("inputs", 4)
        outputs = extra.get("outputs", 4)
        for i in range(1, inputs + 1):
            add(f"input_mute_{i}", _mute(f"Input {i} Mute"), "inputMute", i, "boolean")
        for o in range(1, outputs + 1):
            add(f"output_level_{o}", _level(f"Output {o} Level (dB)"),
                "outputLevel", o, "number")
            add(f"output_mute_{o}", _mute(f"Output {o} Mute"),
                "outputMute", o, "boolean")
        # Crosspoints always appear in the schema (settable via set_control),
        # but a big grid isn't auto-subscribed to avoid flooding the wire —
        # the integrator can runtime-subscribe the crosspoints they care about.
        sub_xpoints = inputs * outputs <= 64
        for i in range(1, inputs + 1):
            for o in range(1, outputs + 1):
                add(f"xpoint_{i}_{o}", _ctl_bool(f"XP {i}->{o} On"),
                    "crosspointLevelState", (i, o), "boolean",
                    subscribe=sub_xpoints)
                add(f"xpoint_level_{i}_{o}", _level(f"XP {i}->{o} Level (dB)"),
                    "crosspointLevel", (i, o), "number", subscribe=sub_xpoints)
    elif bt == "automixer":
        for ch in chans:
            add(f"channel_level_{ch}", _level(f"Ch {ch} Level (dB)"),
                "channelLevel", ch, "number")
            add(f"channel_mute_{ch}", _mute(f"Ch {ch} Mute"),
                "channelMute", ch, "boolean")
            add(f"gain_reduction_{ch}", _meter(f"Ch {ch} Gain Reduction (dB)"),
                "gainReduction", ch, "number")
        add("output_level", _level("Output Level (dB)"), "outputLevel", None, "number")
        add("output_mute", _mute("Output Mute"), "outputMute", None, "boolean")
    elif bt == "router":
        for o in chans:
            add(f"output_{o}", _ctl_int(f"Output {o} Source"), "output", o, "integer")
    elif bt == "logic":
        for ch in chans:
            add(f"state_{ch}", _ctl_bool(f"Logic State {ch}"), "state", ch, "boolean")
    elif bt == "logic_meter":
        for ch in chans:
            add(f"state_{ch}", _ro_bool(f"Logic Meter {ch}"), "state", ch, "boolean")
    elif bt == "aec":
        for ch in chans:
            add(f"level_{ch}", _level(f"AEC Ch {ch} Level (dB)"), "level", ch, "number")
            add(f"mute_{ch}", _mute(f"AEC Ch {ch} Mute"), "mute", ch, "boolean")
            add(f"erc_state_{ch}", _ro_bool(f"AEC Ch {ch} ERC Active"),
                "ercState", ch, "boolean")
    elif bt == "room_combiner":
        for w in chans:
            add(f"group_{w}", _ctl_int(f"Wall {w} Group"), "group", w, "integer")
            add(f"combine_{w}", _ctl_bool(f"Wall {w} Combined"), "combine", w, "boolean")
    elif bt == "audio_meter":
        for ch in chans:
            add(f"meter_{ch}", _meter(f"Meter Ch {ch} (dB)"), "level", ch, "number",
                rate_ms=METER_SUBSCRIBE_RATE_MS)
    elif bt == "signal_present":
        for ch in chans:
            add(f"signal_present_{ch}", _ro_bool(f"Signal Present Ch {ch}"),
                "signalPresent", ch, "boolean")
            add(f"signal_level_{ch}", _meter(f"Signal Level Ch {ch} (dB)"),
                "signalLevel", ch, "number", rate_ms=METER_SUBSCRIBE_RATE_MS)
    elif bt == "generator":
        add("amplitude", _ctl_num("Generator Amplitude (dB)"), "amplitude", None, "number")
        add("frequency", _ctl_int("Generator Frequency (Hz)"), "frequency", None, "integer")
        add("state", _ctl_bool("Generator On"), "generatorEnable", None, "boolean")
    elif bt == "voip_rx":
        add("call_state", {"type": "string", "label": "Call State"},
            "callState", None, "string")
        add("mute", _mute("Receive Mute"), "mute", None, "boolean")
    # dialer: no state — commands only (still registered as a child so the
    #         dialer commands' block picker can target it).
    # preset: no state and no per-block commands — recall_preset is
    #         device-level; a bare preset label registers no child.

    return schema, wire, subs


# ── Command surface (static — child-scoped, not per-block-named) ──

def _block_param() -> dict[str, Any]:
    return {"type": "child_id", "child_type": BLOCK_CHILD_TYPE, "required": True,
            "label": "Block",
            "help": "Pick one of the DSP blocks you declared in the block list."}


def _control_param(help: str | None = None) -> dict[str, Any]:
    # Cascades off the sibling `block` child_id param: picking a block
    # populates this with that block's controls (its dynamic schema's
    # control:true props). Stays free-text-forgiving for anything the
    # picker hasn't loaded yet.
    p: dict[str, Any] = {
        "type": "string", "required": True, "label": "Control",
        "options_from": {"param": "block", "source": "child_schema"},
    }
    if help:
        p["help"] = help
    return p


def _line_param() -> dict[str, Any]:
    return {"type": "integer", "required": False, "label": "Line",
            "default": 1, "min": 1}


def build_commands() -> dict[str, dict[str, Any]]:
    """The driver's static command surface: generic TTP escape hatches plus
    child-scoped commands that pick a declared block and one of its controls.

    The command set is the same for every instance — the per-block detail
    lives in the child roster + each child's schema, not in per-block command
    names (that was the pre-3.0.0 flat model)."""
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
            "help": "Add a runtime subscription. The value surfaces under a flat "
                    "state var named after the token; scripts read it via "
                    "state.get('device.<id>.<token>'). Use the block children for "
                    "declared blocks — this is for ad-hoc attributes.",
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

    # Child-scoped commands — pick a declared block, then one of its controls.
    cmds.update({
        "set_control": {
            "label": "Set Control",
            "params": {
                "block": _block_param(),
                "control": _control_param(
                    "Pick the block above to list its controls, or type one "
                    "(e.g. level_1, mute_2, xpoint_1_3, source, output_mute)."),
                # The Value field follows the picked control's type (a dB
                # spinner for a level, Yes/No for a mute, a source number, ...).
                "value": {"type": "string", "required": True, "label": "Value",
                          "type_from": {"param": "control"},
                          "help": "Numbers and true/false are auto-typed."},
            },
            "help": "Set any control on a declared block — level, mute, "
                    "crosspoint, source, group, generator amplitude, etc.",
        },
        "toggle_control": {
            "label": "Toggle Control",
            "params": {
                "block": _block_param(),
                "control": _control_param(
                    "A boolean control — a mute, crosspoint on/off, or logic state."),
            },
            "help": "Toggle a boolean control on a declared block.",
        },
        "step_control": {
            "label": "Step Control (± dB)",
            "params": {
                "block": _block_param(),
                "control": _control_param("A level control, e.g. level_1."),
                "amount": {"type": "number", "required": True, "label": "Amount (dB)",
                           "default": 1.0,
                           "help": "Positive increments, negative decrements."},
            },
            "help": "Nudge a level control up or down by a relative amount.",
        },
        "ramp_level": {
            "label": "Ramp Level (dB over sec)",
            "params": {
                "block": _block_param(),
                "control": _control_param("A level control to ramp, e.g. level_1."),
                "target_db": {"type": "number", "required": True, "label": "Target (dB)",
                              "min": -100, "max": 12},
                "duration_s": {"type": "number", "required": True, "label": "Duration (sec)",
                               "min": 0, "default": 2.0},
            },
            "help": "Glide a level control to a target dB over a duration "
                    "(Tesira 'set rampLevel').",
        },
        "dial": {
            "label": "Dialer: Dial",
            "params": {
                "block": _block_param(),
                "number": {"type": "string", "required": True, "label": "Number"},
                "line": _line_param(),
            },
            "help": "Dial a number on a VoIP / POTS dialer block.",
        },
        "hangup": {
            "label": "Dialer: Hang Up",
            "params": {"block": _block_param(), "line": _line_param()},
        },
        "answer": {
            "label": "Dialer: Answer",
            "params": {"block": _block_param(), "line": _line_param()},
        },
        "dtmf": {
            "label": "Dialer: Send DTMF",
            "params": {
                "block": _block_param(),
                "digits": {"type": "string", "required": True, "label": "Digits"},
                "line": _line_param(),
            },
        },
    })
    return cmds


# ── The driver class ──

# Default block roster seeded into the block table so a new device isn't
# empty. Covers a typical conferencing room: mic mute/level, program level +
# mute, source select. The integrator edits these rows on the device page.
DEFAULT_BLOCKS = [
    {"tag": "Mute1", "type": "mute", "channels": "1-4"},
    {"tag": "Level1", "type": "level", "channels": "1-4"},
    {"tag": "PgmMute", "type": "mute", "channels": "1"},
    {"tag": "PgmLvl", "type": "level", "channels": "1"},
    {"tag": "PgmSrc", "type": "source_select"},
]


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
        "version": "3.1.2",
        # The connection lifecycle hooks this driver overrides landed in
        # 0.24.0 (supersedes the table-editor 0.23.0 requirement).
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls Biamp Tesira and TesiraFORTÉ DSPs over the Tesira "
            "Text Protocol on TCP port 23 (Telnet). Supports Mute, Level, "
            "Source Selector, Matrix Mixer (crosspoints), Auto Mixer, "
            "Router, Logic, AEC, Room Combiner, Audio Meter, Signal "
            "Present Meter, Tone Generator, Preset recall, and basic VoIP "
            "/ Dialer surfaces. Subscribes to push updates instead of "
            "polling. Declare your Tesira blocks once in the device "
            "config — each block becomes a child entity whose per-channel "
            "controls panel UIs bind to directly."
        ),
        "source_url": "https://support.biamp.com/Tesira/Control/Tesira_Text_Protocol",
        "tags": ["dsp", "tesira", "ttp", "ceiling-mic", "aec", "conferencing"],
        "verified": False,
        "simulated": True,
        "protocols": ["biamp_tesira"],
        "ports": [23],
        "transport": "tcp",
        "discovery": {
            # Tesira's TCP/23 banner read is preceded by Telnet IAC
            # negotiation that the declarative tcp_probe runner can't
            # filter, and the serial-number multi-exchange needs a
            # banner read before the query write. The companion handles
            # both.
            "python": "./biamp_tesira_ttp_discovery.py",
            "oui": ["78:45:01"],
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
                "want to monitor or control in the 'DSP Block List' table "
                "on the device page; each block becomes a child entity whose per-channel "
                "controls (levels, mutes, crosspoints, sources) panel "
                "elements bind to. Drive them with the Set / Toggle / Step "
                "Control commands: pick the block, then its control."
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
                "table on the device page.\n"
                "Add a row per block: its Instance Tag, its Block Type "
                "(from the dropdown), and — for per-channel blocks or a "
                "matrix mixer — the Channels / NxM cell:\n"
                "    • Channels can be '1', '1-4' (range), or '1,3,5' (list)\n"
                "    • For a Matrix Mixer, use INPUTS x OUTPUTS (e.g. 8x4 = "
                "8 inputs, 4 outputs)\n"
                "    • Leave Channels blank and channel 1 is assumed\n"
                "A new device starts with an example conferencing-room "
                "roster (mic mute/level, program level + mute, source "
                "select) — edit those rows to match your Tesira design, "
                "add rows for AEC / Room Combiner / Matrix Mixer as needed, "
                "and remove the ones you don't use.\n"
                "\n"
                "STEP 5 — Save.\n"
                "Within seconds the device should show 'Connected'. Each "
                "block you declared appears as a child entity — e.g. the "
                "'Level1' block exposes a 'level_3' control for its third "
                "channel. Bind those child controls to UI elements, or drive "
                "them with the Set / Toggle / Step Control commands (pick "
                "the block, then its control) in macros and panel buttons.\n"
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
            "blocks": DEFAULT_BLOCKS,
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
                "type": "table",
                "label": "DSP Block List",
                "row_label": "block",
                "columns": BLOCK_COLUMNS,
                "help": (
                    "Declare the Tesira blocks you want to monitor or "
                    "control — one row each: the Instance Tag, its block "
                    "type, and (for per-channel blocks or a matrix mixer) "
                    "the channels. To find Instance Tags, right-click a "
                    "block in Tesira software and choose Properties. Tags "
                    "are case-sensitive."
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
        "quick_actions": ["recall_preset", "recall_preset_by_name"],
        "actions": [
            {"id": "recall_preset", "kind": "command", "icon": "play"},
            {"id": "recall_preset_by_name", "kind": "command", "icon": "bookmark"},
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection / Verify Blocks",
                "icon": "search",
                "availability": "always",
            },
        ],
        # Every declared Tesira block is registered as a dynamic "block"
        # child at connect; its controls are published as a per-child schema.
        "child_entity_types": BLOCK_CHILD_TYPES,
        # Device-level state vars and the (static) command surface. Both are
        # class-level and instance-independent now — per-block detail lives in
        # the child roster, not in per-instance state_variables / commands.
        "state_variables": system_state_variables(),
        "commands": build_commands(),
    }

    HEALTH_FAULT_MESSAGE = (
        "Connected, but the DSP stopped answering (no reply to "
        "DEVICE get version)."
    )

    # ── Lifecycle ──

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state: Any,
        events: Any,
    ) -> None:
        # Expand the user's declared block list into a dynamic child roster.
        # Each block becomes a "block" child (registered at connect) with a
        # per-instance schema; the command surface stays static (class-level),
        # so DRIVER_INFO is not rebuilt per instance. The config value is the
        # `type: table` row list (or a legacy string for a project saved before
        # the table editor — parse_blocks_config handles both).
        self._blocks: list[dict[str, Any]] = parse_blocks_config(
            config.get("blocks", DEFAULT_BLOCKS))

        # child_id -> per-child dynamic schema published at register_child().
        self._block_schemas: dict[str, dict[str, dict[str, Any]]] = {}
        # child_id -> summary state ({name, block_type, channels}).
        self._block_meta: dict[str, dict[str, Any]] = {}
        # child_id -> original instance tag (child_id is the sanitized form).
        self._tag_by_child: dict[str, str] = {}
        # (child_id, prop) -> (attribute, index) for command resolution.
        self._prop_wire: dict[tuple[str, str], tuple[str, Any]] = {}
        # publishToken -> (child_id, prop) so pushes / initial GETs route to
        # the right child property.
        self._route_by_key: dict[str, tuple[str, str]] = {}
        # Subscriptions to (re)register on every connect. Each carries the
        # wire pieces plus its child route.
        self._subscriptions: list[dict[str, Any]] = []
        self._sub_by_token: dict[str, dict[str, Any]] = {}

        for blk in self._blocks:
            if blk["type"] == "unknown":
                continue
            cid = _safe_token(blk["tag"])
            if cid in self._block_schemas:
                log.warning(
                    f"Tesira: two blocks sanitize to the same child id "
                    f"{cid!r}; keeping the first, skipping {blk['tag']!r}"
                )
                continue
            schema, wire, subs = _expand_block(blk)
            self._block_schemas[cid] = schema
            self._block_meta[cid] = {
                "name": blk["tag"],
                "block_type": blk["type"],
                "channels": _channels_label(blk),
            }
            self._tag_by_child[cid] = blk["tag"]
            for prop, w in wire.items():
                self._prop_wire[(cid, prop)] = w
            for s in subs:
                token = f"{cid}_{s['prop']}"
                sub = {
                    "tag": blk["tag"],
                    "attribute": s["attribute"],
                    "index": s["index"],
                    "token": token,
                    "child_id": cid,
                    "prop": s["prop"],
                    "rate_ms": s["rate_ms"],
                    "type_hint": s["type_hint"],
                }
                self._subscriptions.append(sub)
                self._sub_by_token[token] = sub
                self._route_by_key[token] = (cid, s["prop"])

        super().__init__(device_id, config, state, events)

        # Device-level (non-child) state keys — GET replies for these route
        # to flat state, everything else routes to a child property.
        self._system_state_vars = set(
            type(self).DRIVER_INFO["state_variables"].keys()
        )

        # Outstanding "get" queue: when send_command issues a get, we
        # remember the (state_key, type_hint) so the next +OK "value":...
        # response routes to that state var. FIFO.
        self._pending_gets: list[tuple[str, str]] = []
        # Lock around modifying _pending_gets and sending sequenced
        # request/response commands so concurrent get_attribute calls
        # don't interleave.
        self._get_lock = asyncio.Lock()

        # Liveness-probe correlation: the probe's _pending_gets entry
        # (matched by identity) and the future the health loop awaits.
        self._probe_entry: tuple[str, str] | None = None
        self._probe_fut: asyncio.Future[None] | None = None

        # Saved frame parser used during the IAC handshake (we drop the
        # parser to raw mode for the handshake then restore the
        # \r\n-delimiter parser before normal operation).
        self._saved_frame_parser: Any = None

        # Welcome banner detection during connect()
        self._auth_buffer = bytearray()
        self._auth_event = asyncio.Event()
        self._auth_mode = False

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ConnectionError(f"[{self.device_id}] No host configured")
        # Arm the banner capture BEFORE the transport exists — IAC bytes and
        # the welcome banner arrive immediately on connect.
        self._auth_buffer = bytearray()
        self._auth_event = asyncio.Event()
        self._auth_mode = True

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # Open in raw mode (delimiter=None) — IAC bytes arrive in arbitrary
        # frames and we need to see them byte-for-byte.
        kwargs["delimiter"] = None
        return kwargs

    async def _post_connect(self) -> None:
        # Run the IAC + welcome-banner handshake.
        await self._run_iac_handshake(timeout=10.0)

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

    async def _initial_sync(self) -> None:
        # Settle the session: turn off verbose so we don't get echoes,
        # then probe for serial / firmware (best-effort, ignore errors).
        try:
            await self._send_line("SESSION set verbose false")
            await self._send_line("SESSION set aliasUsage true")
            await self._send_get('DEVICE get serialNumber', "serial_number", "string")
            await self._send_get('DEVICE get version', "firmware_version", "string")
            await self._send_get('DEVICE get hostname', "device_id_str", "string")
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial session setup failed")

        # Register the declared blocks as children before subscribing so
        # push updates and initial GETs have somewhere to land. Idempotent
        # on reconnect (same config → same schema).
        self._register_blocks()

        # Re-subscribe to every declared block. This also runs after
        # reconnect — Tesira subscriptions are session-scoped.
        await self._subscribe_all()

        # Initial GET for every subscribed attribute so panel UIs reflect
        # current values immediately.
        await self._initial_get_all()

    async def _close_session(self) -> None:
        # Runs on every teardown path: disarm the banner capture and start
        # the next session from a clean GET queue — a session that died with
        # unanswered GETs leaves stale entries at the head of the FIFO that
        # would eat the next session's first replies and mis-route values.
        self._auth_mode = False
        self._auth_buffer = bytearray()
        self._pending_gets.clear()
        self._probe_entry = None
        if self._probe_fut is not None and not self._probe_fut.done():
            self._probe_fut.cancel()
        self._probe_fut = None

    async def disconnect(self) -> None:
        if self.transport:
            try:
                # Polite quit so the device frees its session slot promptly.
                await self._send_line("SESSION quit")
            except (ConnectionError, OSError):
                pass
        await super().disconnect()

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

    @staticmethod
    def _strip_iac(buf: bytearray) -> tuple[bytearray, bytes]:
        """Split a raw Telnet buffer into (cleaned bytes, IAC replies owed).

        An IAC sequence is always 3 bytes: IAC + (DO/DONT/WILL/WONT) +
        option. We owe WONT to every DO and DONT to every WILL, telling the
        device "we're a dumb pipe" per RFC 854/855. IAC IAC is an escaped
        0xFF data byte and is left alone. An incomplete sequence at the end
        of the buffer is kept for the next pass.
        """
        i = 0
        cleaned = bytearray()
        replies: list[bytes] = []
        while i < len(buf):
            b = buf[i]
            if b != IAC:
                cleaned.append(b)
                i += 1
                continue
            # We hit an IAC. Need at least 3 bytes total.
            if i + 2 >= len(buf):
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
        return cleaned, b"".join(replies)

    async def _process_iac_buffer(self) -> None:
        """Strip IAC sequences from the auth buffer, sending replies inline."""
        if self.transport is None:
            return

        cleaned, payload = self._strip_iac(self._auth_buffer)
        self._auth_buffer = cleaned

        # Send all replies coalesced
        if payload:
            try:
                await self.transport.send(payload)
                log.debug(
                    f"[{self.device_id}] IAC negotiation: sent "
                    f"{len(payload) // 3} option replies"
                )
            except (ConnectionError, OSError) as e:
                log.warning(f"[{self.device_id}] IAC reply send failed: {e}")

    # ── Sending ──

    async def _send_line(self, line: str) -> None:
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send((line + "\n").encode("utf-8"))

    async def _send_get(self, line: str, state_key: str, type_hint: str) -> tuple[str, str]:
        """Queue the pending-GET entry, then send the query.

        The entry must be queued BEFORE the send — the reply can arrive the
        moment the send awaits, and a reply that finds the queue one entry
        short routes every subsequent value into the wrong state var. On a
        send failure the entry is removed so the queue stays in sync.
        """
        entry = (state_key, type_hint)
        self._pending_gets.append(entry)
        try:
            await self._send_line(line)
        except BaseException:
            try:
                self._pending_gets.remove(entry)
            except ValueError:
                pass
            raise
        return entry

    def _register_blocks(self) -> None:
        """Register every declared block as a dynamic child with its schema.

        Safe to re-run on reconnect — register_child is an idempotent no-op
        for an already-registered id with the same schema (config can't change
        without a full driver reload), so control values survive a reconnect
        and the fresh subscriptions / GETs repopulate them."""
        for cid, schema in self._block_schemas.items():
            # A dynamic child's schema= is its FULL schema — splice in the
            # shared summary fields the summary/label rows reference.
            full_schema = {**_BLOCK_SUMMARY_SCHEMA, **schema}
            try:
                self.register_child(
                    BLOCK_CHILD_TYPE, cid, schema=full_schema,
                    initial_state=dict(self._block_meta[cid]),
                )
            except (ValueError, TypeError) as exc:
                log.warning(
                    f"[{self.device_id}] Could not register block child "
                    f"{cid!r}: {exc}"
                )

    def _route_value(self, key: str, coerced: Any) -> None:
        """Route a resolved value (from a push or a GET reply) to its target:
        a child property if `key` is a declared subscription token, a flat
        device-level state var if it's a system var, else a raw flat key (an
        ad-hoc runtime subscription)."""
        route = self._route_by_key.get(key)
        if route is not None:
            cid, prop = route
            if self.is_child_registered(BLOCK_CHILD_TYPE, cid):
                try:
                    self.set_child_state_batch(BLOCK_CHILD_TYPE, cid, {prop: coerced})
                except ValueError:
                    pass
            return
        if key in self._system_state_vars:
            self.set_state(key, coerced)
            return
        # Ad-hoc runtime-subscribe token — surface raw so scripts can read it.
        self.state.set(f"device.{self.device_id}.{key}", coerced)

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
                await self._send_get(cmd, sub["token"], sub["type_hint"])
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
            # queue forever on an error response. A -ERR reply to the
            # liveness probe still proves the device answered.
            if self._pending_gets:
                self._resolve_probe(self._pending_gets.pop(0))
            return

        # Successful GET response: +OK "value":...
        m = OK_VALUE_RE.match(line)
        if m:
            value_str = m.group(1).strip()
            if self._pending_gets:
                entry = self._pending_gets.pop(0)
                state_key, type_hint = entry
                coerced = self._coerce_response_value(value_str, type_hint)
                if coerced is not None:
                    # state_key is a child token for initial GETs, or a
                    # device-level var for the metadata / get_attribute path.
                    self._route_value(state_key, coerced)
                # Also surface in last_query_result for visibility from macros
                self.set_state("last_query_result", value_str)
                self._resolve_probe(entry)
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
                self._resolve_probe(self._pending_gets.pop(0))
            self.set_state("last_query_result", value_str)
            return

        # Plain +OK ack — no payload
        if line == "+OK" or line.startswith("+OK"):
            return

        # Anything else: stash for visibility, log debug.
        log.debug(f"[{self.device_id}] Unhandled line: {line!r}")

    def _handle_push(self, token: str, value_str: str) -> None:
        sub = self._sub_by_token.get(token)
        # An unrecognized token (e.g. a runtime subscribe_attribute) has no
        # child route — _route_value falls back to a raw flat state var so
        # the value is still observable.
        type_hint = sub["type_hint"] if sub else "string"

        # Array values: "value":[1.0 2.0 3.0]
        if value_str.startswith("["):
            self._handle_array_push(token, value_str, type_hint)
            return

        coerced = self._coerce_response_value(value_str, type_hint)
        if coerced is None:
            return
        self._route_value(token, coerced)

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

        # Child-scoped commands — pick a declared block + one of its controls.
        if command == "set_control":
            return await self._cmd_child_set(
                params["block"], params["control"], params["value"])
        if command == "toggle_control":
            return await self._cmd_child_toggle(params["block"], params["control"])
        if command == "step_control":
            return await self._cmd_child_step(
                params["block"], params["control"], params["amount"])
        if command == "ramp_level":
            return await self._cmd_ramp_level(
                params["block"], params["control"],
                params["target_db"], params["duration_s"])
        if command in ("dial", "hangup", "answer", "dtmf"):
            return await self._cmd_dialer(command, params)

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

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
            await self._send_get(" ".join(parts), "last_query_result", "string")
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

    # ── Child-scoped command dispatch ──

    def _resolve_wire(
        self, block: Any, control: Any,
    ) -> tuple[str | None, tuple[str, Any] | None]:
        """Resolve (block child_id, control prop) → (instance tag, (attribute,
        index)). Returns (None, None) if either is unknown."""
        cid = str(block)
        tag = self._tag_by_child.get(cid)
        wire = self._prop_wire.get((cid, str(control)))
        return tag, wire

    def _warn_unresolved(self, block: Any, control: Any) -> None:
        log.warning(
            f"[{self.device_id}] Unknown block/control: "
            f"block={block!r} control={control!r}"
        )

    async def _send_wire(
        self, tag: str, verb: str, attr: str, index: Any, value: Any = None,
    ) -> bool:
        parts = [tag, verb, attr]
        parts.extend(self._format_index(index))
        if value is not None:
            parts.append(self._format_value(value))
        await self._send_line(" ".join(parts))
        return True

    async def _cmd_child_set(self, block: Any, control: Any, value: Any) -> Any:
        tag, wire = self._resolve_wire(block, control)
        if tag is None or wire is None:
            self._warn_unresolved(block, control)
            return None
        attr, index = wire
        return await self._send_wire(tag, "set", attr, index, value)

    async def _cmd_child_toggle(self, block: Any, control: Any) -> Any:
        tag, wire = self._resolve_wire(block, control)
        if tag is None or wire is None:
            self._warn_unresolved(block, control)
            return None
        attr, index = wire
        return await self._send_wire(tag, "toggle", attr, index)

    async def _cmd_child_step(self, block: Any, control: Any, amount: Any) -> Any:
        tag, wire = self._resolve_wire(block, control)
        if tag is None or wire is None:
            self._warn_unresolved(block, control)
            return None
        attr, index = wire
        amt = float(amount)
        verb = "increment" if amt >= 0 else "decrement"
        return await self._send_wire(tag, verb, attr, index, abs(amt))

    async def _cmd_ramp_level(
        self, block: Any, control: Any, target_db: Any, duration_s: Any,
    ) -> Any:
        tag, wire = self._resolve_wire(block, control)
        if tag is None or wire is None:
            self._warn_unresolved(block, control)
            return None
        _, index = wire
        # Tesira level ramp: <TAG> set rampLevel <ch> <dB> <seconds>.
        parts = [tag, "set", "rampLevel", *self._format_index(index),
                 str(float(target_db)), str(float(duration_s))]
        await self._send_line(" ".join(parts))
        return True

    async def _cmd_dialer(self, command: str, params: dict[str, Any]) -> Any:
        tag = self._tag_by_child.get(str(params["block"]))
        if tag is None:
            self._warn_unresolved(params["block"], command)
            return None
        line = int(params.get("line", 1) or 1)
        if command == "dial":
            num = str(params["number"]).replace('"', '\\"')
            await self._send_line(f'{tag} dial {line} "{num}"')
        elif command == "hangup":
            await self._send_line(f"{tag} end {line}")
        elif command == "answer":
            await self._send_line(f"{tag} answer {line}")
        elif command == "dtmf":
            digits = str(params["digits"]).replace('"', '\\"')
            await self._send_line(f'{tag} dtmf {line} "{digits}"')
        return True

    # ── Liveness watchdog (BaseDriver health loop) ──

    async def _liveness_probe(self) -> None:
        """Send `DEVICE get version` and await its reply.

        Tesira pushes are DSP->client only and the device never closes an
        idle session, so a link that dies without a FIN is invisible to the
        transport. The reply routes through the normal _pending_gets FIFO;
        the probe's entry is remembered by identity and any reply that
        consumes it (+OK or -ERR — either proves the device answered)
        resolves the awaited future. The value read-back keeps
        firmware_version fresh as a side effect.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        async with self._get_lock:
            # The probe state and FIFO entry are registered BEFORE the send —
            # the reply can arrive while the send awaits (see _send_get).
            entry = ("firmware_version", "string")
            self._probe_entry = entry
            self._probe_fut = fut
            self._pending_gets.append(entry)
            try:
                await self._send_line("DEVICE get version")
            except BaseException:
                try:
                    self._pending_gets.remove(entry)
                except ValueError:
                    pass
                self._probe_entry = None
                self._probe_fut = None
                raise
        await fut

    def _resolve_probe(self, entry: tuple[str, str]) -> None:
        """Resolve the health loop's awaited future when the probe's FIFO
        entry (matched by identity) is consumed by a reply."""
        if entry is not self._probe_entry:
            return
        fut = self._probe_fut
        self._probe_entry = None
        self._probe_fut = None
        if fut is not None and not fut.done():
            fut.set_result(None)

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
                await self._send_get(
                    self._build_get_command(sub), sub["token"], sub["type_hint"]
                )
            except (ConnectionError, OSError):
                return

    # ── Setup wizard: Test Connection / Verify Blocks ──

    @classmethod
    async def _setup_wait_banner(
        cls, reader: Any, writer: Any, buf: bytearray, timeout: float = 10.0,
    ) -> None:
        """Answer IAC negotiation and wait for the TTP welcome banner.

        Leaves any post-banner bytes in `buf` for the line reader.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            cleaned, payload = cls._strip_iac(buf)
            buf[:] = cleaned
            if payload:
                writer.write(payload)
                await writer.drain()
            idx = buf.find(WELCOME_BANNER)
            if idx >= 0:
                del buf[: idx + len(WELCOME_BANNER)]
                while buf[:1] in (b"\r", b"\n"):
                    del buf[:1]
                return
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise ConnectionError(
                    "Connected, but never saw the Tesira welcome banner — "
                    "is this host really a Tesira?"
                )
            try:
                chunk = await asyncio.wait_for(
                    reader.read(256), min(remaining, 2.0)
                )
            except asyncio.TimeoutError:
                continue
            if not chunk:
                raise ConnectionError(
                    "The device closed the connection during Telnet "
                    "negotiation"
                )
            buf.extend(chunk)

    @staticmethod
    async def _setup_read_reply(
        reader: Any, buf: bytearray, timeout: float = 5.0,
    ) -> str:
        """Read lines until a +OK / -ERR reply arrives (skipping any pushes
        or blank lines), using `buf` as the carry-over buffer."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            nl = buf.find(b"\n")
            if nl >= 0:
                line = (
                    bytes(buf[:nl])
                    .decode("utf-8", errors="replace")
                    .strip()
                )
                del buf[: nl + 1]
                if line.startswith("+OK") or line.startswith("-ERR"):
                    return line
                continue
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise ConnectionError(
                    "Timed out waiting for a reply from the DSP"
                )
            chunk = await asyncio.wait_for(reader.read(256), remaining)
            if not chunk:
                raise ConnectionError("The device closed the connection")
            buf.extend(chunk)

    async def run_setup_action(
        self, action_id: str, params: dict[str, Any], progress: Any,
    ) -> dict[str, Any]:
        """Test Connection / Verify Blocks — out-of-band diagnostic.

        Opens its own Telnet session (works offline and alongside a live
        connection — Tesira allows multiple TTP sessions), confirms the
        welcome banner and firmware, then GETs the first subscribed
        attribute of every declared block: a typo'd instance tag is the #1
        commissioning failure on Tesira, and this surfaces exactly which
        tags the DSP design doesn't recognize.
        """
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 23))
        if not host:
            raise ValueError("No host configured — set the device's host first.")

        await progress(f"Connecting to {host}:{port}", 10)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), 10.0
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach {host}:{port} — check the address and "
                f"that Telnet is enabled in Tesira's Network Settings "
                f"({exc})"
            ) from exc

        firmware = ""
        checked: list[str] = []
        ok: list[str] = []
        failed: list[str] = []
        try:
            await progress("Waiting for the TTP welcome banner", 25)
            buf = bytearray()
            await self._setup_wait_banner(reader, writer, buf)

            writer.write(b"SESSION set verbose false\n")
            await writer.drain()
            await self._setup_read_reply(reader, buf)

            await progress("Checking firmware", 40)
            writer.write(b"DEVICE get version\n")
            await writer.drain()
            reply = await self._setup_read_reply(reader, buf)
            m = OK_VALUE_RE.match(reply)
            if m:
                firmware = m.group(1).strip().strip('"')

            seen: set[str] = set()
            total = max(len(self._blocks), 1)
            for i, blk in enumerate(self._blocks):
                tag = blk["tag"]
                if tag in seen:
                    continue
                seen.add(tag)
                sub = next(
                    (s for s in self._subscriptions if s["tag"] == tag), None
                )
                if sub is None:
                    # Command-only block (e.g. dialer) — nothing to GET.
                    continue
                await progress(
                    f"Verifying block {tag}",
                    40 + int(55 * (i + 1) / total),
                )
                checked.append(tag)
                writer.write(
                    (self._build_get_command(sub) + "\n").encode("utf-8")
                )
                await writer.drain()
                reply = await self._setup_read_reply(reader, buf)
                (ok if reply.startswith("+OK") else failed).append(tag)

            writer.write(b"SESSION quit\n")
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        if failed:
            await progress(
                f"{len(failed)} block(s) not answering: {', '.join(failed)} "
                f"— check the instance tags against the Tesira design",
                100,
            )
        else:
            await progress(
                f"Connected (firmware {firmware or 'unknown'}); all "
                f"{len(checked)} declared blocks answered",
                100,
            )
        return {
            "firmware": firmware,
            "blocks_checked": len(checked),
            "blocks_ok": ok,
            "blocks_failed": failed,
        }
