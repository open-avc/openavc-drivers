"""Discovery matching for the NETGEAR M4250/M4350 driver.

Replays the byte-exact SSDP/UPnP rootDesc.xml captured from a real
M4250-40G8XF-PoE+ (2026-06-07) through the platform's discovery matcher and
confirms the driver surfaces as a ``possible`` match — via the SSDP rootDesc
manufacturer (mined into the ``netgear`` manufacturer_alias) and via the
base-MAC OUI ``28:94:01``. Also confirms a generic gateway advertising the same
UPnP device type but no NETGEAR manufacturer is NOT misidentified as the switch.

Drives the real platform discovery code, so it needs ``openavc`` importable; in
the community repo's isolated CI it skips cleanly. The lightweight check that the
hints are declared at all lives in ``test_netgear_m4250_m4350.py`` (self-contained).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOTDESC_PATH = (REPO_ROOT / "tests" / "fixtures" / "netgear_m4250"
                 / "discovery" / "upnp-rootDesc.xml")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    from openavc.discovery.hints import build_signal_index, parse_driver_discovery
    from openavc.discovery.result import DeviceState
    from openavc.discovery.ssdp_scanner import SSDPResult, _parse_upnp_xml
    from openavc.discovery.tier_matcher import (
        TierMatcher,
        evidence_oui,
        extract_vendor_strings,
    )
    _driver_mod = _load_module(
        "_netgear_driver_disc", REPO_ROOT / "utility" / "netgear_m4250_m4350.py")
except ModuleNotFoundError:
    pytest.skip(
        "NETGEAR discovery test requires the openavc platform "
        "(run from the workspace with openavc installed)",
        allow_module_level=True,
    )

INFO = _driver_mod.NetgearM4250M4350Driver.DRIVER_INFO
ROOTDESC = ROOTDESC_PATH.read_text(encoding="utf-8")

# The switch responds to SSDP with the generic gateway device type; the NETGEAR
# identity lives only in the rootDesc.xml manufacturer.
GENERIC_ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"


def _matcher() -> "TierMatcher":
    hint = parse_driver_discovery(INFO)
    return TierMatcher(build_signal_index([hint]))


def test_real_rootdesc_manufacturer_surfaces_driver():
    result = SSDPResult(ip="169.254.100.100", st=GENERIC_ST)
    _parse_upnp_xml(result, ROOTDESC)
    # The byte-exact rootDesc parses to the NETGEAR identity.
    assert result.manufacturer == "NETGEAR"
    assert result.model_name == "M4250-40G8XF-PoE+"

    evs = result.to_evidence_records()
    evidence_log = [*evs, *extract_vendor_strings(evs)]
    match = _matcher().match(evidence_log)

    assert match.state == DeviceState.POSSIBLE
    assert "netgear_m4250_m4350" in match.candidates


def test_legacy_ui_probe_identifies_outright():
    # The 49151 management UI returns "<TITLE>NETGEAR M4250-...</TITLE>"; the
    # tcp_probe matches that and identifies the switch outright (strong signal),
    # rather than the soft OUI "possible". Byte-exact title from a live GET /.
    from openavc.discovery.probe_runner import _matches
    from openavc.discovery.tier_matcher import evidence_active_probe

    hint = parse_driver_discovery(INFO)
    assert hint.tcp_probe is not None
    assert hint.tcp_probe.port == 49151

    real = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
            b"<TITLE>NETGEAR M4250-40G8XF-PoE+</TITLE>")
    assert _matches(real, hint.tcp_probe.response_match)
    assert not _matches(b"<title>network</title>", hint.tcp_probe.response_match)

    matcher = TierMatcher(build_signal_index([hint]))
    ev = evidence_active_probe(
        hint.tcp_probe.probe_id, response={"text": "matched"}, port=49151)
    match = matcher.match([ev])
    assert match.state == DeviceState.IDENTIFIED
    assert match.driver_id == "netgear_m4250_m4350"


def test_base_mac_oui_surfaces_driver():
    match = _matcher().match(
        [evidence_oui("28:94:01:7F:D8:F4", vendor="NETGEAR")])
    assert match.state == DeviceState.POSSIBLE
    assert "netgear_m4250_m4350" in match.candidates


def test_generic_gateway_without_manufacturer_is_not_misidentified():
    # A non-NETGEAR router advertising the same UPnP device type, with no
    # NETGEAR manufacturer, must NOT be claimed by this driver — which is why
    # the device type is deliberately not declared as an ssdp: fingerprint.
    evs = SSDPResult(ip="192.0.2.50", st=GENERIC_ST).to_evidence_records()
    match = _matcher().match([*evs, *extract_vendor_strings(evs)])
    assert match.state == DeviceState.UNKNOWN
