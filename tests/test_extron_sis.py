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
    assert str(INFO["version"]) == "1.8.1"


def test_the_probe_sends_nothing():
    assert not any(key.startswith("send") for key in PROBE), PROBE


def test_the_banner_identifies_the_device():
    assert re.search(PROBE["expect_regex"], BANNER)
    model = re.search(PROBE["extract"]["model"]["regex"], BANNER)
    assert model and model.group(1).startswith("DXP 84 HD 4K Plus")
    firmware = re.search(PROBE["extract"]["firmware"]["regex"], BANNER)
    assert firmware and firmware.group(1) == "1.06"
