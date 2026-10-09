"""A YAML driver's poll fits inside its poll interval.

Every line of a poll waits ``inter_command_delay`` after it (or the
platform's 50 ms default over TCP or serial when none is set), so a poll of N
lines takes N times that. When that is longer than the poll interval the
device answers polls more than half the time and every reading refreshes less
than half as often as the interval says. The platform's driver validator warns
about it (the vendored copy under scripts/_vendor/); this test makes the
warning a gate for the catalog, at the default config and at the largest
child roster each driver accepts.

A driver that cannot meet it yet is listed below with the reason. An entry
whose driver no longer warns fails the test, so the list only shrinks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _vendor.avcdriver_semantic import validate_driver_warnings  # noqa: E402

CATEGORIES = (
    "projectors", "displays", "switchers", "audio", "cameras", "video",
    "lighting", "power", "streaming", "utility",
)

# driver id -> why its poll can outlast its interval today.
POLL_OUTLASTS_INTERVAL: dict[str, str] = {
    "atlona_omnistream": (
        "polls 8 values per output at the 500 ms gap the protocol asks for; "
        "at its 8 outputs that is 33.5 s against a 30 s interval"
    ),
    "extron_sis": (
        "its rosters accept the 128 inputs and outputs the id format allows; "
        "from 20x20 a poll at the 100 ms SIS gap outlasts the 10 s interval"
    ),
    "kramer_p3000": (
        "its rosters accept the 128 inputs and outputs the id format allows; "
        "from 20x20 a poll at its 100 ms gap outlasts the 10 s interval"
    ),
    "lightware_lw3": (
        "its rosters accept up to 128x128; from 75x75 a poll at the 50 ms "
        "default gap outlasts the 30 s interval"
    ),
}


def _yaml_drivers() -> list[Path]:
    return [
        path
        for category in CATEGORIES
        for path in sorted((REPO_ROOT / category).glob("*.avcdriver"))
    ]


def _poll_warnings(path: Path) -> list[str]:
    definition = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [w for w in validate_driver_warnings(definition) if "poll sends" in w]


@pytest.mark.parametrize("path", _yaml_drivers(), ids=lambda p: p.stem)
def test_each_poll_fits_its_interval(path: Path) -> None:
    warnings = _poll_warnings(path)
    if path.stem in POLL_OUTLASTS_INTERVAL:
        assert warnings, (
            f"{path.stem} no longer outlasts its poll interval; remove it from "
            f"POLL_OUTLASTS_INTERVAL"
        )
        return
    assert warnings == [], f"{path.stem}: {warnings}"


def test_every_listed_driver_exists() -> None:
    known = {path.stem for path in _yaml_drivers()}
    assert set(POLL_OUTLASTS_INTERVAL) <= known, sorted(
        set(POLL_OUTLASTS_INTERVAL) - known
    )
