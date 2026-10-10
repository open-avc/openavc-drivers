"""Self-contained contract test for extron_sis's discovery probe.

An Extron SIS device sends its copyright banner the moment a Telnet socket
opens (DXP HD 4K PLUS guide, "Copyright Information"), so the probe sends
nothing and reads the banner. It used to send Esc 3CV, which sets verbose
mode 3: a write typed into every port-23 device a scan reached.

Runs with PyYAML only, like the rest of the community CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

DRIVER_PATH = (
    Path(__file__).resolve().parent.parent / "switchers" / "extron_sis.avcdriver"
)
INFO = yaml.safe_load(DRIVER_PATH.read_text(encoding="utf-8"))
PROBE = INFO["discovery"]["tcp_probe"]

# The guide's banner, behind the Telnet option bytes a server sends first.
BANNER = (
    "\xff\xfb\x01\xff\xfb\x03(c) Copyright 2018, Extron Electronics, "
    "DXP 84 HD 4K Plus, V1.06, 60-1495-01\r\nMon, 12 Feb 2018 11:27:33\r\n"
)


def test_version_bumped():
    assert str(INFO["version"]) == "1.9.0"
    assert str(INFO["min_platform_version"]) == "0.37.0"


def test_the_probe_sends_nothing():
    assert not any(key.startswith("send") for key in PROBE), PROBE


def test_the_banner_identifies_the_device():
    assert re.search(PROBE["expect_regex"], BANNER)
    model = re.search(PROBE["extract"]["model"]["regex"], BANNER)
    assert model and model.group(1).startswith("DXP 84 HD 4K Plus")
    firmware = re.search(PROBE["extract"]["firmware"]["regex"], BANNER)
    assert firmware and firmware.group(1) == "1.06"


def _size_rule():
    rules = [r for r in INFO["responses"]
             if r.get("set", {}).get("matrix_outputs") == "$2"]
    assert len(rules) == 1
    return re.compile(rules[0]["match"])


def test_a_matrix_reports_its_size_and_the_counts_fill_in():
    """The Information request I answers "V<in>X<out> A<in>X<audio outs>"
    (DXP HD 4K PLUS guide, "General information"), tagged Info00* in
    verbose mode 3. The video half sizes the rosters and is saved into the
    count settings (learned_from)."""
    rule = _size_rule()
    for reply, size in (("V8X4 A8X2", ("8", "4")),
                        ("Info00*V8X8 A8X2", ("8", "8")),
                        ("V32X32 A32X2", ("32", "32"))):
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
    # A one-output size, and a DSC scaler's signal information, never match.
    assert not rule.match("V8X1 A8X1")
    assert not rule.match("Vid1 Typ6 Std0 Blk0 Hrt031.5 Vrt060.0")
