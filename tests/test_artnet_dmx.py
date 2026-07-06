"""Tests for the Art-Net DMX driver's declared surface.

``artnet_dmx.py`` imports ``server.*`` at module load, so the whole module
skips when the openavc platform isn't importable (this repo's isolated CI);
it runs in the workspace where openavc is installed alongside.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    _driver_mod = _load_module(
        "_artnet_dmx_driver", REPO_ROOT / "lighting" / "artnet_dmx.py"
    )
except ModuleNotFoundError:
    pytest.skip(
        "artnet_dmx test requires the openavc platform "
        "(run from the workspace with openavc installed)",
        allow_module_level=True,
    )

ArtNetDmxDriver = next(
    obj
    for obj in vars(_driver_mod).values()
    if isinstance(obj, type)
    and getattr(obj, "DRIVER_INFO", {}).get("id") == "artnet_dmx"
)


def test_actions_reference_real_commands():
    """Every quick action promotes an existing command (a dangling id
    would silently drop the button — the resolver skips it)."""
    info = ArtNetDmxDriver.DRIVER_INFO
    commands = info["commands"]
    actions = info["actions"]
    assert [a["id"] for a in actions] == ["blackout", "full_on"]
    for entry in actions:
        assert entry["id"] in commands, entry["id"]
        assert entry["kind"] == "command"
    # full_on is the destructive one — it must carry a confirm
    full_on = next(a for a in actions if a["id"] == "full_on")
    assert full_on.get("confirm")
