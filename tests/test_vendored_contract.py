"""Tests for the vendored platform-contract copies under scripts/_vendor/.

The vendored modules are generated from the OpenAVC platform repo by
scripts/vendor_platform_contract.py, and CI separately verifies they match
the platform's current files byte-for-byte. These tests prove the copies
behave identically in place: the platform's rejection corpus — one
synthetic invalid definition per validation rule, with the exact error
messages the platform records for each — replays against the vendored
validator and must reproduce those messages exactly.

The corpus is what an *authoring* gate refuses, so the replay validates
with ``strict=True`` — the same bar this repo's catalog build holds a
submitted driver to (scripts/catalog_contract.py). Strict adds one rule: keys the contract does not
declare. The platform's runtime loader softens that one to a warning, so a
driver written for a newer platform still runs; publishing does not get
that latitude, because a misspelled key silently does nothing.

One carve-out: the platform loader also validates the ``discovery:`` block
through its discovery engine, which is not vendored here (the catalog runs
its own deep discovery checks, in scripts/catalog_discovery.py and
scripts/discovery_claims.py). Those loader-side
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
        actual = validate_driver_definition(cases[name], strict=True)
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
        if not validate_driver_definition(cases[name], strict=True) and not expected[name]
    ]
    assert dead == [], f"cases no rule rejects anywhere: {dead}"


def test_minimal_definition_validates_clean() -> None:
    minimal = {"id": "acme_widget", "name": "Acme Widget", "transport": "tcp"}
    assert validate_driver_definition(minimal) == []


def test_the_catalog_refuses_an_empty_section_the_platform_reads_as_absent() -> None:
    """The platform reads a top-level section left empty as absent; older
    releases refuse it or fail the poll, so a catalog driver leaves it out."""
    from catalog_contract import ContractReport

    data = {
        "id": "acme_widget", "name": "Acme Widget", "transport": "tcp",
        "commands": {"noop": {"send": "N\r"}}, "polling": None,
    }
    assert validate_driver_definition(data, strict=True) == []
    errors = ContractReport().check(Path("audio/acme_widget.avcdriver"), "audio/acme_widget.avcdriver", data)
    assert errors == [
        "audio/acme_widget.avcdriver: polling: is empty; leave it out (older OpenAVC releases "
        "refuse a driver with an empty section)"
    ]


def test_the_catalog_refuses_an_empty_simulator_setting() -> None:
    """A simulator setting left empty reads as absent on the platform; older
    releases do not read it so, so a catalog driver leaves it out."""
    from catalog_contract import ContractReport

    data = {
        "id": "acme_widget", "name": "Acme Widget", "transport": "tcp",
        "commands": {"noop": {"send": "N\r"}}, "simulator": {"initial_state": None},
    }
    assert validate_driver_definition(data, strict=True) == []
    errors = ContractReport().check(Path("audio/acme_widget.avcdriver"), "audio/acme_widget.avcdriver", data)
    assert errors == [
        "audio/acme_widget.avcdriver: simulator.initial_state: is empty; leave it out (older "
        "OpenAVC releases do not read an empty simulator setting as left out)"
    ]


_PYTHON_DRIVER = """\
class AcmeWidgetDriver:
    DRIVER_INFO = {
        "id": "acme_widget",
        "name": "Acme Widget",
        "transport": "tcp",
        "commands": {"power_on": {"label": "Power On"}},
    }
"""


def test_the_catalog_checks_a_python_drivers_simulator(tmp_path) -> None:
    """A Python driver's companion simulator is checked with it: a value of
    the wrong kind and an empty setting fail the build, an unknown key warns."""
    from catalog_contract import ContractReport

    driver = tmp_path / "acme_widget.py"
    driver.write_text(_PYTHON_DRIVER, encoding="utf-8")
    (tmp_path / "acme_widget_sim.py").write_text(
        "class AcmeWidgetSimulator:\n"
        "    SIMULATOR_INFO = {\n"
        '        "driver_id": "acme_widget", "name": "Acme Widget Simulator",\n'
        '        "initial_state": {}, "delays": None, "default_port": "23",\n'
        '        "errors": {},\n'
        "    }\n",
        encoding="utf-8",
    )
    report = ContractReport()
    errors = report.check(driver, "audio/acme_widget.py", {})
    assert errors == [
        "audio/acme_widget_sim.py: SIMULATOR_INFO.delays: is empty; leave it out (older "
        "OpenAVC releases do not read an empty simulator setting as left out)",
        "audio/acme_widget_sim.py: SIMULATOR_INFO.default_port: must be an integer",
    ]
    assert report.warnings == [
        "audio/acme_widget_sim.py: SIMULATOR_INFO: unknown key 'errors' (did you mean "
        "'error_modes'?); the simulator does not read it",
    ]
