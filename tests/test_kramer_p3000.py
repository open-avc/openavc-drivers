"""Self-contained contract test for kramer_p3000's matrix size.

A Protocol 3000 matrix answers #INFO-IO? with its input and output counts
(Protocol 3000 3.0 master, "INFO-IO?"; the VS-88H2 manual writes the reply
without the "?"). The driver asks on connect, sizes its Input and Output
rosters from the answer, and the platform saves the counts into the
Input Count and Output Count settings (learned_from).

Also the replies an SWT3-41-H (firmware 1.0.65739) sent, byte for byte:
what each one sets, and that its error lines set nothing. And RS-232 at
the documented 115200 8N1 (Protocol 3000 3.0 master, "Protocol 3000
Syntax").

Runs with PyYAML only, like the rest of the community CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

DRIVER_PATH = (
    Path(__file__).resolve().parent.parent / "switchers" / "kramer_p3000.avcdriver"
)
INFO = yaml.safe_load(DRIVER_PATH.read_text(encoding="utf-8"))


def _size_rule():
    rules = [r for r in INFO["responses"]
             if r.get("set", {}).get("matrix_outputs") == "$2"]
    assert len(rules) == 1
    return re.compile(rules[0]["match"])


def test_version_and_floor():
    assert str(INFO["version"]) == "1.8.0"
    assert str(INFO["min_platform_version"]) == "0.37.0"


def test_rs232_runs_at_the_documented_rate():
    assert INFO["transport"] == "tcp"
    assert INFO["transports"] == ["tcp", "serial"]
    assert INFO["default_config"]["baudrate"] == 115200
    assert INFO["config_schema"]["baudrate"]["default"] == 115200
    # 8N1 is the platform's serial default, so the driver names only the rate.
    for key in ("parity", "bytesize", "stopbits"):
        assert key not in INFO["default_config"]


def test_the_size_is_asked_on_connect():
    assert "#INFO-IO?\r" in INFO["on_connect"]


def test_both_documented_reply_forms_size_the_matrix():
    rule = _size_rule()
    for reply, size in (("~01@INFO-IO? IN 8,OUT 8", ("8", "8")),
                        ("~01@INFO-IO IN 8,OUT 4", ("8", "4")),
                        ("~12@INFO-IO IN 16, OUT 16", ("16", "16"))):
        m = rule.match(reply)
        assert m and m.groups() == size, reply
    schema = INFO["config_schema"]
    assert schema["input_count"]["learned_from"] == "matrix_inputs"
    assert schema["output_count"]["learned_from"] == "matrix_outputs"
    types = INFO["child_entity_types"]
    assert types["output"]["instances"]["count_from_state"] == "matrix_outputs"
    assert types["input"]["instances"]["count_from_state"] == "matrix_inputs"


def test_a_single_output_device_keeps_its_flat_surface():
    rule = _size_rule()
    assert not rule.match("~01@INFO-IO IN 4,OUT 1")
    assert not rule.match("~01@INFO-IO ERR 002")


def _first_rule(reply):
    for rule in INFO["responses"]:
        m = re.match(rule["match"], reply)
        if m:
            return rule, m
    return None, None


# Measured on an SWT3-41-H, firmware 1.0.65739 (ROUTE dialect only).
SWT3_READ = (
    ("~01@MODEL SWT3-41-H", {"model": "SWT3-41-H"}),
    ("~01@VERSION 1.0.65739", {"firmware": "1.0.65739"}),
    ("~01@ROUTE 1,1,2", {"input": "2"}),
    ("~01@VMUTE 1,1", {"video_mute": "true"}),
    ("~01@SIGNAL 1,0", {"signal_present": "false"}),
    ("~01@AUD-LVL 1,1,0.0", {"volume": "0.0"}),
    ("~01@AUD-LVL 1,1,-12.5", {"volume": "-12.5"}),
)

SWT3_ERRORS = (
    "~01@ OK ",
    "~01@INFO-IO? ERR 002",
    "~01@VID? 1 ERR 002",
    "~01@VID? * ERR 002",
    "~01@AUD? 1 ERR 002",
    "~01@AV 3>1 ERR 002",
    "~01@VID 4>1 ERR 002",
    "~01@AV 2>* ERR 002",
    "~01@ROUTE 1 ERR 001",
    "~01@ROUTE 1 ERR 031",
    "~01@ROUTE ERR 003",
    "~01@MUTE? 0 ERR 002",
    "~01@AUD-LVL ERR 003",
    "~01@VMUTE 1 ERR 005",
)


def test_an_swt3_reply_sets_what_it_reports():
    for reply, expected in SWT3_READ:
        rule, m = _first_rule(reply)
        assert rule is not None, reply
        got = {k: (m.expand(v.replace("$", "\\")) if "$" in v else v)
               for k, v in rule["set"].items()}
        assert got == expected, reply


def test_an_swt3_error_line_sets_nothing():
    for reply in SWT3_ERRORS:
        rule, _ = _first_rule(reply)
        assert rule is None, reply


def test_volume_is_a_number_on_both_surfaces():
    assert INFO["state_variables"]["volume"]["type"] == "number"
    out = INFO["child_entity_types"]["output"]["state_variables"]
    assert out["volume"]["type"] == "number"
    assert INFO["commands"]["set_volume"]["params"]["level"]["type"] == "number"
