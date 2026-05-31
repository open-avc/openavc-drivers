"""Cross-driver disambiguation for the three TurtleAV controllers.

darwin_control, chazy_control, and chazy_control_pro all default to the same
hostname (controller.local) on telnet 23, so the thing that tells them apart on
an UNINSTALLED scan is the declarative ``tcp_probe`` banner matcher each ships
in the catalog. This loads those matchers straight from the built index.json
(the artifact discovery actually consumes) and asserts each one claims only its
own controller's welcome banner, never a sibling's. A regression here is the
exact "an uninstalled Darwin shows up as a Chazy" bug this fingerprinting was
added to prevent.

Pure stdlib (json + re), no openavc/server import, so it runs in the community
repo's isolated CI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "index.json"

# Leading Telnet IAC negotiation the controllers send before the banner, in its
# own TCP segment. The probe runner accumulates past it, so the matcher has to
# still hit with this noise prepended.
IAC = bytes([0xFF, 0xFB, 0x03, 0xFF, 0xFB, 0x01, 0xFF, 0xFD, 0x00]).decode("latin-1")


def _banner(model: str, fw: str) -> str:
    return (
        IAC
        + "\r\n================================\r\n"
        + f"Welcome To {model} Terminal Control System\r\n"
        + f"FW Version: {fw}\r\n"
        + "CONTROLLER> "
    )


# label -> (owning driver id, banner). One banner per controller variant.
BANNERS: dict[str, tuple[str, str]] = {
    "darwin_fw1": ("darwin_control", _banner("Controller(h)", "1.50.02")),
    "darwin_fw2": ("darwin_control", _banner("DARWIN CONTROL", "2.03.19")),
    "chazy_pro": ("chazy_control_pro", _banner("TAV-CHAZY-CLTPRO", "1.10.11")),
    "chazy_std": ("chazy_control", _banner("CHAZY CONTROL", "1.00.17")),
}

TAV_DRIVERS = ("darwin_control", "chazy_control", "chazy_control_pro")


def _load_tcp_probes() -> dict[str, dict]:
    data = json.loads(INDEX.read_text(encoding="utf-8"))
    probes: dict[str, dict] = {}
    for d in data["drivers"]:
        if d["id"] in TAV_DRIVERS:
            probes[d["id"]] = (d.get("discovery") or {}).get("tcp_probe")
    return probes


_PROBES = _load_tcp_probes()


def _matches(banner: str, probe: dict) -> bool:
    """Minimal mirror of the runner's matcher: exactly one of expect /
    expect_regex (these probes don't use expect_hex)."""
    if probe.get("expect") is not None:
        return probe["expect"] in banner
    if probe.get("expect_regex") is not None:
        return re.search(probe["expect_regex"], banner) is not None
    raise AssertionError("tcp_probe declares neither expect nor expect_regex")


def test_all_three_declare_a_port23_tcp_probe():
    # Identity must live in the declarative (catalog-runnable) probe, not only
    # in an install-gated python companion.
    for did in TAV_DRIVERS:
        probe = _PROBES.get(did)
        assert probe, f"{did} must declare a discovery.tcp_probe"
        assert probe.get("port") == 23, f"{did} tcp_probe must target telnet 23"


def test_each_banner_claimed_by_exactly_its_owner():
    for label, (owner, banner) in BANNERS.items():
        claimants = [d for d in TAV_DRIVERS if _matches(banner, _PROBES[d])]
        assert claimants == [owner], (
            f"{label} banner must be claimed only by {owner!r}; got {claimants!r} "
            f"-- a sibling probe matching this banner is the mislabel bug"
        )


def test_no_banner_goes_unclaimed():
    # Every known controller variant must be positively identified by exactly
    # one driver (the flip side of mutual exclusivity).
    for label, (_owner, banner) in BANNERS.items():
        assert any(_matches(banner, _PROBES[d]) for d in TAV_DRIVERS), (
            f"{label} banner is claimed by no driver -- it would scan as unknown"
        )
