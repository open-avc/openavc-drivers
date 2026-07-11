"""MD push-notice replay tests for the Audio-Technica ATDM family.

The ATDM mixers multicast `MD <notice> ...` frames (push: block). These tests
replay captured/spec-derived frames against each driver's declared response
rules with a minimal reimplementation of the platform's set-shorthand matcher
(first match wins; `$N` capture refs; an optional group that didn't match
skips its mapping; values coerce by the state variable's declared type) —
keeping the repo CI self-contained per the driver-test policy.

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


def coerce(raw: str, var_type: str):
    if var_type == "integer":
        return int(raw)
    if var_type in ("float", "number"):
        return float(raw)
    if var_type == "boolean":
        return raw.lower() in ("1", "true", "yes", "on")
    return raw


def dispatch(driver: dict, frame: str) -> dict:
    """Apply the first matching response rule; return {state: value}."""
    state_vars = driver.get("state_variables", {})
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
        return out
    return {}


# ── at_atdm_0604a ──────────────────────────────────────────────────────────

CASES_0604A = [
    # Captured live on hardware (device ID 42, AT-LINK on):
    ("MD output_mute_notice 0000 42 NC 0,0 \r", {"output_1_mute": False}),
    ("MD input_gain_level_notice 0000 42 NC 0,0,0,400,0 \r",
     {"input_1_level": 400.0, "input_1_mute": False}),
    # Spec-derived:
    ("MD output_mute_notice 0000 00 NC 10,1 \r", {"output_st_mute": True}),
    ("MD output_level_notice 0000 00 NC 1,275 \r", {"output_2_level": 275.0}),
    ("MD input_gain_level_notice 0000 00 NC 10,40,40,511,1 \r",
     {"input_st_level": 511.0, "input_st_mute": True}),
    # Omitted-parameter forms (spec: "some parameters can be omitted"):
    ("MD input_gain_level_notice 0000 00 NC 3,,,123, \r",
     {"input_4_level": 123.0}),
    ("MD input_gain_level_notice 0000 00 NC 5,,,,1 \r",
     {"input_6_mute": True}),
    ("MD open_channel_notice 0000 00 NC 5,1 \r", {"input_6_gate_open": True}),
    ("MD open_channel_notice 0000 00 NC 0,0 \r", {"input_1_gate_open": False}),
    ("MD recall_preset_notice 0000 00 NC 6 \r", {"last_recalled_preset": 6}),
    # 24-value meter frame: 12 post-fader mapped, AEC + gainshare ignored.
    ("MD level_meter_notice 0000 42 NC "
     "61,60,59,58,57,56,55,54,53,52,51,50,"
     "60,60,60,60,60,60,15,15,15,15,15,15 \r",
     {"input_1_meter": 61, "input_2_meter": 60, "input_3_meter": 59,
      "input_4_meter": 58, "input_5_meter": 57, "input_6_meter": 56,
      "input_st_l_meter": 55, "input_st_r_meter": 54,
      "output_1_meter": 53, "output_2_meter": 52,
      "output_st_l_meter": 51, "output_st_r_meter": 50}),
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
    # open_channel: all six inputs in ONE frame on this generation
    ("MD open_channel_notice 0000 00 NC 1,0,1,0,1,0 \r",
     {"input_1_gate_open": True, "input_2_gate_open": False,
      "input_3_gate_open": True, "input_4_gate_open": False,
      "input_5_gate_open": True, "input_6_gate_open": False}),
    # input mute is push-only on this protocol (no get/set commands)
    ("MD input_gain_level_notice 0000 00 NC 0,,,123,1 \r",
     {"input_1_level": 123.0, "input_1_mute": True}),
    ("MD input_gain_level_notice 0000 00 NC 10,20,20,411,0 \r",
     {"input_st_level": 411.0, "input_st_mute": False}),
    ("MD output_level_notice 0000 00 NC 10,300 \r", {"output_st_level": 300.0}),
    ("MD output_mute_notice 0000 00 NC 1,1 \r", {"output_2_mute": True}),
    ("MD recall_preset_notice 0000 00 NC 3 \r", {"last_recalled_preset": 3}),
    # bank 0 = "Not select" on this generation: must not write state
    ("MD recall_preset_notice 0000 00 NC 0 \r", {}),
    # 44-value meter frame: 12 post-fader mapped; pre-fader/AEC/gain-share/
    # dynamics blocks ignored
    ("MD level_meter_notice 0000 00 NC "
     "61,60,59,58,57,56,55,54,53,52,51,50,"
     "40,40,40,40,40,40,40,40,60,60,60,60,"
     "15,15,15,15,15,15,15,15,1,1,1,1,1,1,1,1,1,1,1,1 \r",
     {"input_1_meter": 61, "input_6_meter": 56, "input_st_l_meter": 55,
      "input_st_r_meter": 54, "output_1_meter": 53, "output_2_meter": 52,
      "output_st_l_meter": 51, "output_st_r_meter": 50,
      "input_2_meter": 60, "input_3_meter": 59, "input_4_meter": 58,
      "input_5_meter": 57}),
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
    for n in list(range(1, 7)) + ["st"]:
        var = driver["state_variables"][f"input_{n}_mute"]
        assert not var.get("control"), f"input_{n}_mute must be read-only"


# ── at_atdm_1012 (0604a generation, wider channel model; spec-derived) ─────

CASES_1012 = [
    ("MD input_gain_level_notice 0000 00 NC 9,40,40,511,1 \r",
     {"input_10_level": 511.0, "input_10_mute": True}),
    # spec's own example frame: ST2 input is deliberately unmapped
    ("MD input_gain_level_notice 0000 00 NC 11,40,40,511,1 \r", {}),
    ("MD output_level_notice 0000 00 NC 7,275 \r", {"output_8_level": 275.0}),
    # spec's own example frames: ST outputs deliberately unmapped
    ("MD output_level_notice 0000 00 NC 9,511 \r", {}),
    ("MD output_mute_notice 0000 00 NC 9,1 \r", {}),
    ("MD output_mute_notice 0000 00 NC 0,1 \r", {"output_1_mute": True}),
    # three-param gate notice (ch, SmartMix group 1-4, status)
    ("MD open_channel_notice 0000 00 NC 9,4,1 \r", {"input_10_gate_open": True}),
    ("MD open_channel_notice 0000 00 NC 0,1,0 \r", {"input_1_gate_open": False}),
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
    assert result["input_1_meter"] == 60
    assert result["input_10_meter"] == 51
    assert result["input_st1_meter"] == 50
    assert result["input_st2_meter"] == 49
    assert result["output_1_meter"] == 48
    assert result["output_8_meter"] == 41
    assert result["output_st1_meter"] == 40
    assert result["output_st2_meter"] == 39
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
