"""Self-contained contract test for the viewsonic_cde .avcdriver (2.0.0).

The 1.x driver was Python for one reason: the Wake-on-LAN packet a display
in network-dead standby needs, which YAML could not send. The platform's
``udp:`` command block now sends it, so 2.0.0 is the declarative rewrite of
the same LFD RS-232 & LAN protocol. Every command, state variable and
device setting kept its name and its tokens, which is what this file pins.

Two mirrors of the runtime, without importing the platform (the community CI
runs with PyYAML only):

- the send side: ``command_prefix`` + the command's ``send`` with the
  driver's ``{param}`` / ``{param:spec}`` substitution and the enum
  ``map:`` translation, asserted byte for byte against the spec's worked
  examples (brightness 76, an ID-05 get, the 'A'/'a' backlight pair);
- the receive side: every reply through the declared ``responses`` first
  match wins on the CR-stripped frame (``ConfigurableDriver.on_data_received``),
  mappings applied group by group with their ``map`` and the state
  variable's declared type, exactly as ``compiled_protocol`` compiles them.

Replies are built from the LFD RS-232 & LAN Protocol Specification v3.3.2
tables: the 9-byte set/get grammar, the packed Get-Input reply, the 32-byte
NUL-padded info replies, the negative thermal form, and the Smart Hub's
6-byte sub-fields.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

DRIVER_PATH = Path(__file__).resolve().parent.parent / "displays" / "viewsonic_cde.avcdriver"
INFO = yaml.safe_load(DRIVER_PATH.read_text(encoding="utf-8"))
STATE_VARS = INFO["state_variables"]
CONFIG = {"monitor_id": 1, "host": "10.0.0.50", "port": 5000}

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([^{}]*))?\}")


# ── Runtime mirrors ─────────────────────────────────────────────────────────


def _substitute(template: str, values: dict) -> str:
    """Mirror compiled_protocol.safe_substitute for the specs this driver uses."""
    def repl(m):
        name, spec = m.group(1), m.group(2)
        if name not in values:
            return m.group(0)
        v = values[name]
        if not spec:
            return str(v)
        try:
            return format(v, spec)
        except (ValueError, TypeError):
            return format(int(v), spec)
    return _PLACEHOLDER.sub(repl, template)


def _wire(command: str, params: dict | None = None, config: dict | None = None) -> bytes:
    """Mirror ConfigurableDriver.build_wire: map: translation, then prefix + send + suffix."""
    cmd = INFO["commands"][command]
    params = dict(params or {})
    for name, pdef in (cmd.get("params") or {}).items():
        value_map = pdef.get("map")
        if value_map and name in params and str(params[name]) in value_map:
            params[name] = value_map[str(params[name])]
    raw = INFO["command_prefix"] + cmd["send"] + INFO["command_suffix"]
    text = _substitute(raw, {**(config or CONFIG), **params})
    return text.encode("ascii")


def _coerce(value, key):
    var_type = STATE_VARS.get(key, {}).get("type", "string")
    if var_type == "integer":
        return int(value)
    if var_type == "number":
        return float(value)
    if var_type == "boolean":
        return str(value).lower() in ("true", "1", "on", "yes")
    return str(value)


def _apply(reply: bytes, config: dict | None = None) -> tuple[str | None, dict]:
    """First response rule matching the CR-stripped reply, and what it writes."""
    text = reply.decode("utf-8", errors="replace").strip()
    for resp in INFO["responses"]:
        pattern = _substitute(resp["match"], config or CONFIG)
        m = re.search(pattern, text)
        if not m:
            continue
        out: dict = {}
        if "set" in resp:
            for key, expr in resp["set"].items():
                raw = m.group(int(expr[1:])) if str(expr).startswith("$") else expr
                out[key] = _coerce(raw, key)
        for mp in resp.get("mappings", []):
            raw = m.group(mp["group"])
            if raw is None:
                continue
            value_map = mp.get("map")
            if value_map and raw in value_map:
                raw = value_map[raw]
            out[mp["state"]] = _coerce(raw, mp["state"])
        return resp["match"], out
    return None, {}


def _reply(code: str, value: str, mid: int = 1) -> bytes:
    body = f"{mid:02d}r{code}{value}"
    return bytes([0x30 + len(body) + 1]) + body.encode() + b"\r"


def _reply32(code: str, value: str, mid: int = 1) -> bytes:
    payload = value.encode()[:26]
    return f"2{mid:02d}r{code}".encode() + payload + b"\x00" * (26 - len(payload)) + b"\r"


# ── Metadata ────────────────────────────────────────────────────────────────


def test_metadata_shape():
    assert INFO["id"] == "viewsonic_cde"
    assert INFO["version"] == "2.0.0"
    assert INFO["transport"] == "tcp"
    assert INFO["transports"] == ["tcp", "serial"]
    assert INFO["delimiter"] == "\r"
    assert INFO["command_prefix"] == "8{monitor_id:02d}"
    assert INFO["command_suffix"] == "\r"
    # The udp: block is a 0.34.0 field, and the floor says so.
    assert INFO["min_platform_version"] == "0.34.0"
    for cid in INFO["quick_actions"]:
        assert cid in INFO["commands"], cid
    for key, setting in INFO["device_settings"].items():
        assert setting["state_key"] in STATE_VARS, key
    assert "actions" not in INFO, "the kind:setup wake is gone; Power On is the wake"


def test_power_on_is_the_wake_on_lan_and_runs_offline():
    cmd = INFO["commands"]["power_on"]
    assert cmd["available_offline"] is True
    assert cmd["udp"] == {"magic_packet": "mac_address"}
    assert "send" not in cmd
    # The MAC can come from the display (state) or be typed in (config).
    assert "mac_address" in STATE_VARS
    assert "mac_address" in INFO["config_schema"]
    assert cmd["sets"] == {"power": "on"}
    # The protocol's own Set Power stays reachable.
    assert INFO["commands"]["power_on_lan"]["send"] == "s!001"


def test_setting_enum_labels_are_the_state_tokens():
    # The platform confirms a queued setting write by matching the state
    # variable's value against the option's label, so the two must agree.
    for key, setting in INFO["device_settings"].items():
        if setting["type"] != "enum":
            continue
        tokens = set(STATE_VARS[setting["state_key"]]["values"])
        labels = {opt["label"] for opt in setting["values"]}
        assert labels == tokens, key


# ── Send side: the spec's worked examples ───────────────────────────────────


def test_packet_build_matches_spec_examples():
    # Set brightness 76 on ID 01: '8' '01' 's' '$' '076' CR
    assert _wire("set_brightness", {"level": 76}) == b"801s$076\r"
    # A get addressed to ID 05 (RS-232 chain).
    assert _wire("power_on_lan", config={**CONFIG, "monitor_id": 5}) == b"805s!001\r"
    # The backlight-level pair rides its own command type: 'A' set (raw command).
    assert _wire("raw_command", {"cmd_type": "A", "code": "B", "value": "080"}) == b"801AB080\r"
    assert _wire("raw_command", {"cmd_type": "a", "code": "B", "value": "000"}) == b"801aB000\r"


def test_enum_params_map_tokens_to_wire_codes():
    assert _wire("set_source", {"source": "hdmi2"}) == b'801s"014\r'
    assert _wire("set_pip_input", {"source": "android"}) == b"801s700A\r"
    assert _wire("set_color_mode", {"mode": "warm"}) == b"801s)001\r"
    assert _wire("set_tiling_mode", {"mode": "on"}) == b"801sP001\r"
    assert _wire("set_tiling_layout", {"horizontal": 3, "vertical": 2}) == b"801sR032\r"
    assert _wire("set_tiling_position", {"position": 7}) == b"801sS007\r"
    assert _wire("nav_key", {"key": "enter"}) == b"801sA004\r"
    assert _wire("set_osd_language", {"language": "spanish"}) == b"801s2002\r"
    assert _wire("input_cycle") == b'801s"00Z\r'
    assert _wire("volume_up") == b"801s5901\r"
    assert _wire("backlight_off") == b"801s(000\r"
    assert _wire("restore_default") == b"801s~000\r"


def test_every_source_token_has_a_wire_code_and_reads_back():
    source_map = INFO["commands"]["set_source"]["params"]["source"]["map"]
    values = {opt["value"] for opt in INFO["commands"]["set_source"]["params"]["source"]["values"]}
    assert set(source_map) == values
    # Every documented code's two-character suffix decodes back to its token.
    for token, code in source_map.items():
        _, out = _apply(_reply("j", "1" + code[1:]))
        assert out == {"signal_detected": True, "source": token}, token


def test_device_setting_writes_carry_the_frame_and_a_read_back():
    ds = INFO["device_settings"]
    assert _substitute(ds["backlight"]["write"]["send"], {**CONFIG, "value": 55}) == "801AB055\r801aB000\r"
    assert _substitute(ds["power_lock"]["write"]["send"], {**CONFIG, "value": "001"}) == "801s4001\r801go000\r"
    assert _substitute(ds["remote_control_mode"]["write"]["send"], {**CONFIG, "value": "002"}) == "801sB002\r801gn000\r"
    assert _substitute(ds["touch"]["write"]["send"], {**CONFIG, "value": True}) == "801s=103\r801g=003\r"
    assert _substitute(ds["touch"]["write"]["send"], {**CONFIG, "value": False}) == "801s=003\r801g=003\r"


def test_polls_and_on_connect_are_framed_for_the_configured_monitor_id():
    queries = INFO["polling"]["queries"]
    assert len(queries) == 26
    assert _substitute(queries[0]["send"], {"monitor_id": 5}) == "805gl000\r"
    assert {q["query_for"] for q in queries} <= set(STATE_VARS)
    assert [_substitute(q, CONFIG) for q in INFO["on_connect"]] == [
        "801g4000\r", "801g5000\r", "801g6000\r", "801g7000\r", "801g8000\r",
    ]


# ── Receive side ────────────────────────────────────────────────────────────


def test_power_and_binary_replies():
    assert _apply(_reply("l", "001"))[1] == {"power": "on"}
    assert _apply(_reply("l", "000"))[1] == {"power": "standby"}
    assert _apply(_reply("g", "001"))[1] == {"mute": True}
    assert _apply(_reply("h", "000"))[1] == {"backlight_on": False}
    assert _apply(_reply("i", "001"))[1] == {"freeze": True}
    assert _apply(_reply("v", "001"))[1] == {"tiling_mode": True}
    assert _apply(_reply("w", "000"))[1] == {"tiling_compensation": False}


def test_numeric_replies_including_the_backlight_pair():
    assert _apply(_reply("f", "063"))[1] == {"volume": 63}
    assert _apply(_reply("b", "076"))[1] == {"brightness": 76}
    assert _apply(_reply("B", "080"))[1] == {"backlight": 80}
    assert _apply(_reply("a", "050"))[1] == {"contrast": 50}
    assert _apply(_reply("y", "007"))[1] == {"tiling_position": 7}


def test_input_reply_signal_packing_and_unknown_code():
    assert _apply(_reply("j", "104"))[1] == {"signal_detected": True, "source": "hdmi1"}
    assert _apply(_reply("j", "014"))[1] == {"signal_detected": False, "source": "hdmi2"}
    # A code this driver does not know reads back as its suffix.
    assert _apply(_reply("j", "1ZZ"))[1] == {"signal_detected": True, "source": "ZZ"}
    assert _apply(_reply("u", "029"))[1] == {"pip_input": "dp2"}


def test_locks_are_not_inverted_and_rcu_pip_modes_decode():
    assert _apply(_reply("o", "001"))[1] == {"power_lock": "locked"}
    assert _apply(_reply("p", "000"))[1] == {"button_lock": "unlocked"}
    assert _apply(_reply("q", "001"))[1] == {"menu_lock": "locked"}
    assert _apply(_reply("n", "002"))[1] == {"remote_control_mode": "passthrough"}
    assert _apply(_reply("t", "002"))[1] == {"pip_mode": "pbp"}


def test_function_on_off_reply_routes_by_function_id():
    assert _apply(_reply("=", "103"))[1] == {"touch_enabled": True}
    assert _apply(_reply("=", "003"))[1] == {"touch_enabled": False}
    assert _apply(_reply("=", "101"))[1] == {"backlight_on": True}
    assert _apply(_reply("=", "002"))[1] == {"freeze": False}


def test_tiling_layout_spells_out_h_by_v():
    assert _apply(_reply("x", "033"))[1] == {"tiling_layout": "3x3"}
    assert _apply(_reply("x", "019"))[1] == {"tiling_layout": "1x9"}
    layout_map = next(
        mp["map"] for r in INFO["responses"] for mp in r.get("mappings", [])
        if mp["state"] == "tiling_layout"
    )
    assert len(layout_map) == 81


def test_thermal_negative_encoding():
    assert _apply(_reply("0", "042"))[1] == {"thermal_c": 42}
    assert _apply(_reply("0", "-05"))[1] == {"thermal_c": -5}


def test_identity_and_info_replies_strip_nul_padding():
    assert _apply(_reply32("4", "CDE5530"))[1] == {"device_name": "CDE5530"}
    assert _apply(_reply32("5", "040ec2123456"))[1] == {"mac_address": "040ec2123456"}
    assert _apply(_reply32("6", "192.168.1.50"))[1] == {"ip_address": "192.168.1.50"}
    assert _apply(_reply32("7", "ABC180212345"))[1] == {"serial_number": "ABC180212345"}
    assert _apply(_reply32("8", "3.02.001"))[1] == {"firmware_version": "3.02.001"}
    assert _apply(_reply32("1", "001234"))[1] == {"operation_hours": 1234}
    # An all-zero MAC is not one; the twelve hex digits are what the rule takes.
    assert _apply(_reply32("5", ""))[1] == {}


def test_smart_hub_fields_in_any_order_and_any_subset():
    _, out = _apply(_reply32(":", "A-05.0B030.0C00080D00001"))
    assert out == {"amb_temperature_c": -5.0, "amb_humidity": 30.0, "amb_light": 80, "amb_presence": True}
    _, out = _apply(_reply32(":", "D00000C00012"))
    assert out == {"amb_light": 12, "amb_presence": False}
    _, out = _apply(_reply32(":", "A023.5"))
    assert out == {"amb_temperature_c": 23.5}


def test_monitor_id_addressing_and_noise_match_nothing():
    # A reply for another display on the chain matches no rule for ID 01.
    assert _apply(_reply("l", "001", mid=2)) == (None, {})
    # ... and matches once the driver is configured for that ID.
    assert _apply(_reply("l", "001", mid=2), {**CONFIG, "monitor_id": 2})[1] == {"power": "on"}
    # Acks, rejects, the link test and IR pass-through carry no state.
    for frame in (b"501+\r", b"501-\r", _reply("z", "000"), b"801p\x01\x02\r"):
        assert _apply(frame) == (None, {}), frame


@pytest.mark.parametrize("command", sorted(INFO["commands"]))
def test_every_command_sends_one_way(command):
    cmd = INFO["commands"][command]
    assert ("send" in cmd) != ("udp" in cmd), command
