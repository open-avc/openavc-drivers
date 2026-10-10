"""Self-contained contract test for gefen_uhd600a's matrix size.

The routing dump "s" answers one pair per output (manuals, "s"): four on the
EXT-UHD600A-44, eight on the -88, and both are square. The driver reads the
size from it, sizes the Input roster from it, and the platform saves the
output letters and input count into the Output IDs and Input Count settings
(learned_from).

Runs with PyYAML only, like the rest of the community CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

DRIVER_PATH = (
    Path(__file__).resolve().parent.parent / "switchers" / "gefen_uhd600a.avcdriver"
)
INFO = yaml.safe_load(DRIVER_PATH.read_text(encoding="utf-8"))


def _first_match(line: str) -> dict:
    """The rule that handles ``line``: first match wins, as at runtime."""
    for rule in INFO["responses"]:
        if "match" in rule and re.search(rule["match"], line):
            return rule
    raise AssertionError(f"no rule matches {line!r}")


def test_version_and_floor():
    assert str(INFO["version"]) == "1.2.0"
    assert str(INFO["min_platform_version"]) == "0.37.0"


def test_the_44_dump_says_four_by_four_and_routes():
    rule = _first_match("S A 1 B 2 C 3 D X")
    assert rule["set"] == {"matrix_output_ids": "A,B,C,D", "matrix_inputs": "4"}
    assert [c["id"] for c in rule["child_set"]] == ["A", "B", "C", "D"]


def test_the_88_dump_says_eight_by_eight_and_routes():
    rule = _first_match("S A 1 B 2 C 3 D X E 0 F X G 1 H 0")
    assert rule["set"] == {
        "matrix_output_ids": "A,B,C,D,E,F,G,H", "matrix_inputs": "8"}
    assert [c["id"] for c in rule["child_set"]] == list("ABCDEFGH")


def test_a_route_echo_does_not_say_the_size():
    rule = _first_match("R A 1 B 1 C 1")
    assert "set" not in rule


def test_the_settings_fill_in_and_the_inputs_follow():
    schema = INFO["config_schema"]
    assert schema["output_ids"]["learned_from"] == "matrix_output_ids"
    assert schema["input_count"]["learned_from"] == "matrix_inputs"
    inst = INFO["child_entity_types"]["input"]["instances"]
    assert inst["count_from_state"] == "matrix_inputs"
