"""MD push-notice replay tests for the Audio-Technica ATDM family.

The ATDM mixers multicast `MD <notice> ...` frames (push: block). These tests
replay captured/spec-derived frames against each driver's declared response
rules with a minimal reimplementation of the platform's response matcher
(first match wins; `$N` capture refs; an optional group that didn't match
skips its mapping; values coerce by the state variable's declared type) —
keeping the repo CI self-contained per the driver-test policy.

The family models its channels as child entities, so a matched rule routes its
captures two ways, and `dispatch` returns both in one flat dict:
  - device-level `set:`  -> `"last_recalled_preset"`
  - `child_set:` entries -> `"<child_type>.<local_id>.<prop>"`, e.g.
    `"input.1.mute"`. A child id resolves from a literal, a `$N` capture, or
    `{group, map}` (the wire ids are 0-based, with 10 for the ST channels);
    a wire id the map doesn't cover writes nothing, as does an id outside the
    type's declared instances.

Frame provenance:
  - at_atdm_0604a: `MD output_mute_notice 0000 42 NC 0,0` and the full-form
    input_gain_level_notice were captured on real hardware (fw 01.03.01);
    the rest are built from IP Control Protocol Specifications Ver 1.0.
  - at_atdm_0604 / at_atdm_1012: spec-derived (Ver 1.5 / Ver 1.0 documents).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_driver(name: str) -> dict:
    with open(REPO_ROOT / "audio" / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def coerce(raw, var_type: str):
    if var_type == "integer":
        return int(raw)
    if var_type in ("float", "number"):
        return float(raw)
    if var_type == "boolean":
        return str(raw).lower() in ("1", "true", "yes", "on")
    return raw


def _registered_ids(type_def: dict) -> set[int]:
    """Local ids the platform would have registered for a child type — the
    runtime skips a child_set write addressed to an unregistered child."""
    instances = type_def.get("instances") or {}
    if isinstance(instances.get("count"), int):
        return set(range(1, instances["count"] + 1))
    id_format = type_def.get("id_format") or {}
    return set(range(id_format.get("min", 1), id_format.get("max", 1) + 1))


def _resolve_child_id(entry: dict, match: re.Match) -> int | None:
    """Resolve a child_set entry's `id` — literal, `$N`, or {group, map}."""
    cid = entry.get("id")
    if isinstance(cid, dict):
        group = cid.get("group")
        if isinstance(group, str) and group.startswith("$"):
            group = group[1:]
        raw = match.group(int(group))
        if raw is None:
            return None
        id_map = cid.get("map")
        if isinstance(id_map, dict) and id_map:
            # A wire id outside the map isn't a channel this driver models.
            raw = {str(k): v for k, v in id_map.items()}.get(str(raw).strip())
            if raw is None:
                return None
    elif isinstance(cid, str) and cid.startswith("$"):
        raw = match.group(int(cid[1:]))
        if raw is None:
            return None
    else:
        raw = cid
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def dispatch(driver: dict, frame: str) -> dict:
    """Apply the first matching response rule; return {state_key: value},
    with child-entity writes keyed `"<type>.<id>.<prop>"`."""
    state_vars = driver.get("state_variables", {})
    child_types = driver.get("child_entity_types") or {}
    for resp in driver.get("responses", []):
        pattern = resp.get("match") or resp.get("pattern")
        if not pattern:
            continue
        m = re.search(pattern, frame)
        if not m:
            continue
        out = {}
        for state_key, expr in (resp.get("set") or {}).items():
            var_type = state_vars.get(state_key, {}).get("type", "string")
            if isinstance(expr, str) and expr.startswith("$"):
                raw = m.group(int(expr[1:]))
                if raw is None:  # optional group absent -> mapping skipped
                    continue
                out[state_key] = coerce(raw, var_type)
            else:
                out[state_key] = coerce(str(expr), var_type)
        for entry in resp.get("child_set") or []:
            type_def = child_types.get(entry.get("type"))
            if not isinstance(type_def, dict):
                continue
            local_id = _resolve_child_id(entry, m)
            if local_id is None or local_id not in _registered_ids(type_def):
                continue
            child_vars = type_def.get("state_variables") or {}
            for prop, expr in (entry.get("state") or {}).items():
                var_type = (child_vars.get(prop) or {}).get("type", "string")
                if isinstance(expr, dict):
                    var_type = expr.get("type", var_type)
                    if "group" in expr:
                        raw = m.group(int(expr["group"]))
                        if raw is None:
                            continue
                    elif "value" in expr:
                        raw = expr["value"]
                    else:
                        continue
                    value_map = {
                        str(k): v for k, v in (expr.get("map") or {}).items()
                    }
                    raw = value_map.get(str(raw), raw)
                elif isinstance(expr, str) and expr.startswith("$"):
                    raw = m.group(int(expr[1:]))
                    if raw is None:  # optional group absent -> mapping skipped
                        continue
                else:
                    raw = expr
                out[f"{entry['type']}.{local_id}.{prop}"] = coerce(raw, var_type)
        return out
    return {}


# ── at_atdm_0604a ──────────────────────────────────────────────────────────

CASES_0604A = [
    # Captured live on hardware (device ID 42, AT-LINK on):
    ("MD output_mute_notice 0000 42 NC 0,0 \r", {"output.1.mute": False}),
    ("MD input_gain_level_notice 0000 42 NC 0,0,0,400,0 \r",
     {"input.1.gain": 0, "input.1.level": 400.0, "input.1.mute": False}),
    # Spec-derived:
    ("MD output_mute_notice 0000 00 NC 10,1 \r", {"stereo_output.1.mute": True}),
    ("MD output_level_notice 0000 00 NC 1,275 \r", {"output.2.level": 275.0}),
    ("MD input_gain_level_notice 0000 00 NC 10,40,40,511,1 \r",
     {"stereo_input.1.gain": 40, "stereo_input.1.level": 511.0,
      "stereo_input.1.mute": True}),
    # Omitted-parameter forms (spec: "some parameters can be omitted"):
    ("MD input_gain_level_notice 0000 00 NC 3,,,123, \r",
     {"input.4.level": 123.0}),
    ("MD input_gain_level_notice 0000 00 NC 5,,,,1 \r",
     {"input.6.mute": True}),
    ("MD open_channel_notice 0000 00 NC 5,1 \r", {"input.6.gate_open": True}),
    ("MD open_channel_notice 0000 00 NC 0,0 \r", {"input.1.gate_open": False}),
    ("MD recall_preset_notice 0000 00 NC 6 \r", {"last_recalled_preset": 6}),
    # 24-value meter frame: 12 post-fader mapped, AEC + gainshare ignored.
    ("MD level_meter_notice 0000 42 NC "
     "61,60,59,58,57,56,55,54,53,52,51,50,"
     "60,60,60,60,60,60,15,15,15,15,15,15 \r",
     {"input.1.meter": 61, "input.2.meter": 60, "input.3.meter": 59,
      "input.4.meter": 58, "input.5.meter": 57, "input.6.meter": 56,
      "stereo_input.1.meter_l": 55, "stereo_input.1.meter_r": 54,
      "output.1.meter": 53, "output.2.meter": 52,
      "stereo_output.1.meter_l": 51, "stereo_output.1.meter_r": 50}),
]


@pytest.mark.parametrize("frame, expected", CASES_0604A)
def test_at_atdm_0604a_notice(frame, expected):
    driver = load_driver("at_atdm_0604a.avcdriver")
    result = dispatch(driver, frame)
    for key, want in expected.items():
        assert result.get(key) == want, (frame, key, result)
    # nothing unexpected written
    assert set(result) == set(expected), (frame, result)


def test_at_atdm_0604a_meter_rule_declares_throttle():
    driver = load_driver("at_atdm_0604a.avcdriver")
    meter_rules = [
        r for r in driver["responses"]
        if "level_meter_notice" in (r.get("match") or "")
    ]
    assert meter_rules and all(r.get("throttle", 0) >= 0.5 for r in meter_rules)


def test_at_atdm_0604a_push_block():
    driver = load_driver("at_atdm_0604a.avcdriver")
    assert driver["push"]["type"] == "multicast"
    assert driver["default_config"]["notification_group"] == "239.0.0.100"
    assert driver["default_config"]["notification_port"] == 17000
    # arming write: 19 fields -> 18 separators, fields 7/8 populated
    arming = [c for c in driver.get("on_connect", []) if "s_network" in c]
    assert len(arming) == 1
    params = arming[0].split("NC ", 1)[1].rsplit("\\r", 1)[0].strip()
    fields = params.split(",")
    assert len(fields) == 19, fields
    assert fields[6] == "1"
    assert fields[7] == "{meters_flag}"
    assert all(f == "" for f in fields[:6] + fields[8:])


# ── at_atdm_0604 (old protocol generation; spec-derived) ───────────────────

CASES_0604 = [
    # open_channel: all six inputs in ONE frame on this generation (Table 5-3),
    # unlike the per-channel (channel, status) notice of the newer generation
    ("MD open_channel_notice 0000 00 NC 1,0,1,0,1,0 \r",
     {"input.1.gate_open": True, "input.2.gate_open": False,
      "input.3.gate_open": True, "input.4.gate_open": False,
      "input.5.gate_open": True, "input.6.gate_open": False}),
    # input mute is push-only on this protocol (no get/set commands)
    ("MD input_gain_level_notice 0000 00 NC 0,,,123,1 \r",
     {"input.1.level": 123.0, "input.1.mute": True}),
    ("MD input_gain_level_notice 0000 00 NC 10,20,20,411,0 \r",
     {"stereo_input.1.gain": 20, "stereo_input.1.level": 411.0,
      "stereo_input.1.mute": False}),
    ("MD output_level_notice 0000 00 NC 10,300 \r",
     {"stereo_output.1.level": 300.0}),
    ("MD output_mute_notice 0000 00 NC 1,1 \r", {"output.2.mute": True}),
    ("MD recall_preset_notice 0000 00 NC 3 \r", {"last_recalled_preset": 3}),
    # bank 0 = "Not select" on this generation: must not write state
    ("MD recall_preset_notice 0000 00 NC 0 \r", {}),
    # 44-value meter frame: 12 post-fader mapped; pre-fader/AEC/gain-share/
    # dynamics blocks ignored
    ("MD level_meter_notice 0000 00 NC "
     "61,60,59,58,57,56,55,54,53,52,51,50,"
     "40,40,40,40,40,40,40,40,60,60,60,60,"
     "15,15,15,15,15,15,15,15,1,1,1,1,1,1,1,1,1,1,1,1 \r",
     {"input.1.meter": 61, "input.6.meter": 56, "stereo_input.1.meter_l": 55,
      "stereo_input.1.meter_r": 54, "output.1.meter": 53, "output.2.meter": 52,
      "stereo_output.1.meter_l": 51, "stereo_output.1.meter_r": 50,
      "input.2.meter": 60, "input.3.meter": 59, "input.4.meter": 58,
      "input.5.meter": 57}),
]


@pytest.mark.parametrize("frame, expected", CASES_0604)
def test_at_atdm_0604_notice(frame, expected):
    driver = load_driver("at_atdm_0604.avcdriver")
    result = dispatch(driver, frame)
    for key, want in expected.items():
        assert result.get(key) == want, (frame, key, result)
    assert set(result) == set(expected), (frame, result)


def test_at_atdm_0604_push_defaults_and_readonly_mutes():
    driver = load_driver("at_atdm_0604.avcdriver")
    assert driver["push"]["type"] == "multicast"
    assert driver["default_config"]["notification_group"] == "225.0.0.100"
    # the push-only input mutes must not claim panel control
    for ctype in ("input", "stereo_input"):
        var = driver["child_entity_types"][ctype]["state_variables"]["mute"]
        assert not var.get("control"), f"{ctype} mute must be read-only"


# ── at_atdm_1012 (0604a generation, wider channel model; spec-derived) ─────

CASES_1012 = [
    ("MD input_gain_level_notice 0000 00 NC 9,40,40,511,1 \r",
     {"input.10.gain": 40, "input.10.level": 511.0, "input.10.mute": True}),
    # spec's own example frame: wire 11 = ST2 (both ST inputs are modelled)
    ("MD input_gain_level_notice 0000 00 NC 11,40,40,511,1 \r",
     {"stereo_input.2.gain": 40, "stereo_input.2.level": 511.0,
      "stereo_input.2.mute": True}),
    ("MD output_level_notice 0000 00 NC 7,275 \r", {"output.8.level": 275.0}),
    # spec's own example frames: wire 9 = ST2 output
    ("MD output_level_notice 0000 00 NC 9,511 \r",
     {"stereo_output.2.level": 511.0}),
    ("MD output_mute_notice 0000 00 NC 9,1 \r", {"stereo_output.2.mute": True}),
    ("MD output_mute_notice 0000 00 NC 0,1 \r", {"output.1.mute": True}),
    # three-param gate notice (ch, SmartMix group 1-4, status)
    ("MD open_channel_notice 0000 00 NC 9,4,1 \r", {"input.10.gate_open": True}),
    ("MD open_channel_notice 0000 00 NC 0,1,0 \r", {"input.1.gate_open": False}),
    ("MD recall_preset_notice 0000 00 NC 8 \r", {"last_recalled_preset": 8}),
]


@pytest.mark.parametrize("frame, expected", CASES_1012)
def test_at_atdm_1012_notice(frame, expected):
    driver = load_driver("at_atdm_1012.avcdriver")
    result = dispatch(driver, frame)
    for key, want in expected.items():
        assert result.get(key) == want, (frame, key, result)
    assert set(result) == set(expected), (frame, result)


def test_at_atdm_1012_meter_frame_maps_22_postfader_channels():
    driver = load_driver("at_atdm_1012.avcdriver")
    values = [str(60 - i) for i in range(22)] + ["60"] * 10 + ["15"] * 10
    frame = "MD level_meter_notice 0000 00 NC " + ",".join(values) + " \r"
    result = dispatch(driver, frame)
    assert result["input.1.meter"] == 60
    assert result["input.10.meter"] == 51
    assert result["stereo_input.1.meter"] == 50
    assert result["stereo_input.2.meter"] == 49
    assert result["output.1.meter"] == 48
    assert result["output.8.meter"] == 41
    assert result["stereo_output.1.meter"] == 40
    assert result["stereo_output.2.meter"] == 39
    assert len(result) == 22


@pytest.mark.parametrize(
    "name, group",
    [("at_atdm_0604.avcdriver", "225.0.0.100"),
     ("at_atdm_1012.avcdriver", "225.0.0.100"),
     ("at_atdm_0604a.avcdriver", "239.0.0.100")],
)
def test_family_arming_write_and_throttle(name, group):
    driver = load_driver(name)
    assert driver["default_config"]["notification_group"] == group
    arming = [c for c in driver.get("on_connect", []) if "s_network" in c]
    assert len(arming) == 1
    fields = arming[0].split("NC ", 1)[1].rsplit("\\r", 1)[0].strip().split(",")
    assert len(fields) == 19 and fields[6] == "1" and fields[7] == "{meters_flag}"
    meter_rules = [
        r for r in driver["responses"]
        if "level_meter_notice" in (r.get("match") or "")
    ]
    assert meter_rules and all(r.get("throttle", 0) >= 0.5 for r in meter_rules)


@pytest.mark.parametrize(
    "name",
    ["at_atdm_0604a.avcdriver", "at_atdm_0604.avcdriver", "at_atdm_1012.avcdriver"],
)
def test_family_meters_commands_and_quick_actions(name):
    """meters_on/meters_off write ONLY field 8 (Audio Level Notification) of
    the 19-field s_network layout, and both are promoted as quick actions."""
    driver = load_driver(name)
    for cmd, flag in (("meters_on", "1"), ("meters_off", "0")):
        send = driver["commands"][cmd]["send"]
        fields = send.split("NC ", 1)[1].rsplit("\\r", 1)[0].strip().split(",")
        assert len(fields) == 19, (name, cmd, fields)
        assert fields[7] == flag, (name, cmd, fields)
        assert all(f == "" for i, f in enumerate(fields) if i != 7), (name, cmd)
    action_ids = [a["id"] for a in driver.get("actions", [])]
    assert "meters_on" in action_ids and "meters_off" in action_ids
