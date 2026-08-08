"""Self-contained contract test for the sanyo_serial_projector .avcdriver.

Replays representative EIKI EK-307W "Basic Serial Command" replies through the
driver's declared ``responses:`` patterns and asserts they classify and extract
the way the runtime would. The runtime applies responses first-match-wins on
the CR-stripped reply (verified against ``ConfigurableDriver.on_data_received``
in openavc/drivers/configurable.py: walk ``responses`` in order, stop at the
first whose ``match`` searches the text, apply its ``set``/``mappings``). This
test mirrors that contract without importing the platform, so the community CI
runs it with only PyYAML installed.

Byte-exact where captured: the model reply ``Coral-EK-307W`` is the string the
bench EK-307W returns to CR5. The other replies are built from the EIKI / Sanyo
"Basic Serial Command" RS-232 command list (power-status codes, input names,
temperature/lamp formats); the driver's response patterns were hardware-
validated against the same unit.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

DRIVER_PATH = (
    Path(__file__).resolve().parent.parent
    / "projectors"
    / "sanyo_serial_projector.avcdriver"
)
INFO = yaml.safe_load(DRIVER_PATH.read_text(encoding="utf-8"))
STATE_VARS = INFO.get("state_variables", {})


def _coerce(value, key):
    """Mirror ConfigurableDriver._coerce_value for the types this driver uses."""
    var_type = STATE_VARS.get(key, {}).get("type", "string")
    if var_type == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if var_type == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if var_type == "boolean":
        return str(value).lower() in ("true", "1", "on", "yes")
    return str(value)


def _apply(reply: bytes):
    """Frame + classify a device reply exactly as the runtime does.

    Returns ``(matched_pattern, {state_key: value})`` for the first response
    whose pattern matches the CR-stripped reply, or ``(None, {})`` if none do
    (e.g. the ACK / "?" replies the driver intentionally ignores).
    """
    text = reply.decode("utf-8", errors="replace").strip()
    for resp in INFO.get("responses", []):
        pattern = resp.get("match") or resp.get("pattern")
        if not pattern:
            continue
        m = re.search(pattern, text)
        if not m:
            continue
        out: dict = {}
        if resp.get("mappings"):
            for mp in resp["mappings"]:
                key = mp.get("state")
                if not key:
                    continue
                if "value" in mp:
                    out[key] = _coerce(mp["value"], key)
                    continue
                raw = m.group(mp.get("group", 0))
                value_map = mp.get("map")
                if value_map and raw in value_map:
                    raw = value_map[raw]
                out[key] = _coerce(raw, key)
        elif "set" in resp:
            for key, expr in resp["set"].items():
                if isinstance(expr, str) and expr.startswith("$"):
                    out[key] = _coerce(m.group(int(expr[1:])), key)
                else:
                    out[key] = expr
        return pattern, out
    return None, {}


# ── Metadata ──

def test_metadata():
    assert INFO["id"] == "sanyo_serial_projector"
    assert INFO["transport"] == "serial"
    assert INFO["delimiter"] == "\r"
    assert INFO["simulated"] is True
    assert INFO["version"]  # bumped alongside the simulator section


# ── Command contract ──

def test_power_uses_immediate_off_not_confirm():
    cmds = INFO["commands"]
    assert cmds["power_on"]["send"] == "C00\r"
    # C01 = immediate power-off (single press, no on-screen confirmation). The
    # C02 "normal" off needs a front-panel confirm, so the driver must NOT wire
    # it to any command -- it is reachable only through the raw escape hatch.
    assert cmds["power_off"]["send"] == "C01\r"
    assert not any(c.get("send", "").startswith("C02") for c in cmds.values())


def test_every_command_is_cr_terminated():
    for name, c in INFO["commands"].items():
        send = c.get("send", "")
        assert send.endswith("\r"), f"{name} send {send!r} is not CR-terminated"


def test_status_queries_and_polling():
    cmds = INFO["commands"]
    assert cmds["query_power"]["send"] == "CR0\r"
    assert cmds["query_input"]["send"] == "CR1\r"
    assert cmds["query_lamp_hours"]["send"] == "CR3\r"
    assert cmds["query_temperatures"]["send"] == "CR6\r"
    assert cmds["query_model"]["send"] == "CR5\r"
    assert INFO["polling"]["queries"] == ["CR0\r", "CR1\r", "CR3\r", "CR6\r"]
    assert "CR5\r" in INFO["on_connect"]


def test_raw_escape_hatch_appends_cr():
    assert INFO["commands"]["send_command"]["send"] == "{raw}\r"


# ── Response parsing (replay representative device replies) ──

def test_power_status_codes():
    cases = {
        b"00\r": "On",
        b"80\r": "Standby",
        b"40\r": "Warming Up",
        b"20\r": "Cooling",
        b"04\r": "Power Mgmt Ready",
        b"88\r": "Recovering (Temp Fault)",
    }
    for reply, expected in cases.items():
        _pattern, out = _apply(reply)
        assert out == {"power": expected}, (reply, out)


def test_lamp_hours_parsed_as_int():
    _pattern, out = _apply(b"1240\r")
    assert out == {"lamp_hours": 1240}


def test_temperatures_space_separated():
    # The temperature pattern accepts digits, dots and spaces. The validated
    # EK-307W reply is space-separated (the manufacturer doc's "_" is
    # placeholder notation, not the wire byte).
    _pattern, out = _apply(b"31.5 29.0 30.2\r")
    assert out == {"temperatures": "31.5 29.0 30.2"}


def test_model_reply_is_byte_exact_capture():
    _pattern, out = _apply(b"Coral-EK-307W\r")
    assert out == {"model": "Coral-EK-307W"}


def test_input_names():
    for name in ("VGA 1", "HDMI 1", "Network", "Memory Viewer"):
        _pattern, out = _apply((name + "\r").encode())
        assert out == {"input": name}, name


# ── Disambiguation (ordering of mutually-overlapping patterns) ──

def test_model_wins_over_input_by_ordering():
    # "Coral-EK-307W" matches BOTH the model pattern and the generic
    # letter-leading input pattern. The driver lists model first, so
    # first-match-wins resolves it to model. Guard that ordering by proving the
    # input pattern (declared last) would otherwise have claimed it.
    _pattern, out = _apply(b"Coral-EK-307W\r")
    assert "model" in out and "input" not in out
    input_resp = INFO["responses"][-1]
    assert re.search(input_resp["match"], "Coral-EK-307W")


def test_two_digit_is_power_not_lamp_hours():
    # A 2-digit reply is power; lamp hours need 4-6 digits. The power pattern is
    # declared first and matches exactly two digits, so there is no collision.
    _pattern, out = _apply(b"00\r")
    assert set(out) == {"power"}


def test_ack_and_nak_are_ignored():
    # Functional commands ACK with 0x06; unknown commands reply "?". Neither
    # matches a response pattern, so the driver records no state from them.
    for reply in (b"\x06\r", b"?\r"):
        pattern, out = _apply(reply)
        assert pattern is None and out == {}


# ── Simulator round-trip (sim initial_state parses back through the driver) ──

def test_simulator_initial_state_round_trips():
    sim = INFO["simulator"]["initial_state"]
    # Every status read the simulator answers must parse cleanly through the
    # driver's own response patterns -- catches a sim default the driver can't
    # read back (lamp hours with too few digits, an unmapped power code, ...).
    assert _apply((str(sim["power"]) + "\r").encode())[1] == {"power": "Standby"}
    assert _apply((str(sim["input"]) + "\r").encode())[1] == {"input": sim["input"]}
    assert _apply((str(sim["lamp_hours"]) + "\r").encode())[1] == {"lamp_hours": sim["lamp_hours"]}
    assert _apply((str(sim["temperatures"]) + "\r").encode())[1] == {"temperatures": sim["temperatures"]}
    assert _apply((str(sim["model"]) + "\r").encode())[1] == {"model": sim["model"]}
