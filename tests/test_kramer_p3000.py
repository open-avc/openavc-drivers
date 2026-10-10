"""Self-contained contract test for kramer_p3000's matrix size.

A Protocol 3000 matrix answers #INFO-IO? with its input and output counts
(Protocol 3000 3.0 master, "INFO-IO?"; the VS-88H2 manual writes the reply
without the "?"). The driver asks on connect, sizes its Input and Output
rosters from the answer, and the platform saves the counts into the
Input Count and Output Count settings (learned_from).

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
    assert str(INFO["version"]) == "1.7.0"
    assert str(INFO["min_platform_version"]) == "0.37.0"


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
