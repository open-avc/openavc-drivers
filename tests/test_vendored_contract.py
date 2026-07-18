"""Tests for the vendored platform-contract copies under scripts/_vendor/.

The vendored modules are generated from the OpenAVC platform repo by
scripts/vendor_platform_contract.py, and CI separately verifies they match
the platform's current files byte-for-byte. These tests prove the copies
behave identically in place: the platform's rejection corpus — one
synthetic invalid definition per validation rule, with the exact error
messages the platform records for each — replays against the vendored
validator and must reproduce those messages exactly.

One carve-out: the platform loader also validates the ``discovery:`` block
through its discovery engine, which is not vendored here (the catalog runs
its own deep discovery checks in scripts/build_index.py). Those loader-side
messages all carry the ``discovery: `` prefix and are filtered from the
expectations before comparing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT_DIR = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from _vendor.avcdriver_semantic import validate_driver_definition  # noqa: E402

VENDOR_DIR = SCRIPT_DIR / "_vendor"

_DISCOVERY_PREFIX = "discovery: "


def _load(name: str):
    return json.loads((VENDOR_DIR / name).read_text(encoding="utf-8"))


def _shared_rule_messages(messages: list[str]) -> list[str]:
    """Drop the loader-side discovery-engine messages from an expectation."""
    return [m for m in messages if not m.startswith(_DISCOVERY_PREFIX)]


def test_replay_matches_platform_messages() -> None:
    cases = _load("driver_validation_cases.json")
    expected = _load("driver_validation_messages.json")
    assert sorted(cases) == sorted(expected), (
        "case and message fixtures disagree — regenerate scripts/_vendor/ "
        "(python scripts/vendor_platform_contract.py)"
    )
    for name in sorted(cases):
        actual = validate_driver_definition(cases[name])
        stray = [m for m in actual if m.startswith(_DISCOVERY_PREFIX)]
        assert stray == [], (
            f"'{name}': the shared rules emitted discovery-engine messages; "
            f"the filter in this test is no longer sound: {stray}"
        )
        want = _shared_rule_messages(expected[name])
        assert actual == want, (
            f"vendored validator disagrees with the platform for '{name}':\n"
            f"  platform: {want}\n"
            f"  vendored: {actual}"
        )


def test_every_case_is_rejected() -> None:
    """Each case must trip a shared rule — or, for the loader-only discovery
    cases, at least a platform-side rule — so no corpus entry goes dead."""
    cases = _load("driver_validation_cases.json")
    expected = _load("driver_validation_messages.json")
    dead = [
        name
        for name in sorted(cases)
        if not validate_driver_definition(cases[name]) and not expected[name]
    ]
    assert dead == [], f"cases no rule rejects anywhere: {dead}"


def test_minimal_definition_validates_clean() -> None:
    minimal = {"id": "acme_widget", "name": "Acme Widget", "transport": "tcp"}
    assert validate_driver_definition(minimal) == []
