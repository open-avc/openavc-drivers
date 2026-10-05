"""A model a driver has been run against on real hardware says so in the catalog.

``verified`` is maintainer-controlled: it goes to true once at least one model in
``compatible_models`` has been confirmed end to end on real hardware, and that
model's entry carries ``full`` or ``partial`` (``contributing-drivers.md``,
"Reporting Test Results"). A Device Audit shows "Untested on this model" beside
any model left at ``untested``, so a unit the driver was built and checked
against read as untested in front of the manufacturer who made it.

Reads each driver from its source with the vendored reader, like the rest of
this repo's CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _vendor.python_info import extract_python_driver_info_full  # noqa: E402

CATEGORIES = (
    "projectors", "displays", "switchers", "audio", "cameras", "video",
    "lighting", "power", "streaming", "utility",
)

# (driver file, model) -> the unit it was run against.
RUN_ON_HARDWARE = {
    ("switchers/chazy_control_pro.py", "Chazy Control Pro (TAV-CHAZY-CLTPRO)"): "the bench controller, FW 1.10.11",
    ("switchers/darwin_control.py", "Darwin Control (CTL100AL)"): "the bench controller, FW 1.50.02",
    ("switchers/avproedge_mxnet_1g.py", "AC-MXNET-CBOX-B"): "the bench control box, firmware 4.32",
}


def _info(path: Path) -> dict:
    if path.suffix == ".avcdriver":
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    info, _ = extract_python_driver_info_full(path)
    return info


def _driver_files() -> list[Path]:
    files = []
    for category in CATEGORIES:
        for path in sorted((REPO_ROOT / category).glob("*")):
            if path.suffix == ".avcdriver" or (
                path.suffix == ".py"
                and not path.stem.startswith("_")
                and not path.stem.endswith(("_sim", "_discovery"))
            ):
                files.append(path)
    return files


@pytest.mark.parametrize("rel, model", sorted(RUN_ON_HARDWARE))
def test_a_model_run_on_hardware_is_not_untested(rel, model):
    info = _info(REPO_ROOT / rel)
    assert info["verified"] is True, rel
    entries = [m for m in info["compatible_models"] if model in m["models"]]
    assert len(entries) == 1, (rel, model)
    assert entries[0]["confidence"] in ("full", "partial"), (rel, model)


def test_a_verified_driver_names_a_model_it_was_confirmed_on():
    unsupported = []
    for path in _driver_files():
        info = _info(path)
        models = info.get("compatible_models")
        if info.get("verified") is not True or not isinstance(models, list) or not models:
            continue
        if not any(isinstance(m, dict) and m.get("confidence") in ("full", "partial") for m in models):
            unsupported.append(info.get("id", path.stem))
    assert not unsupported, (
        f"verified: true with every model untested: {unsupported}. Give the model it was "
        "confirmed on its own entry at full or partial."
    )
