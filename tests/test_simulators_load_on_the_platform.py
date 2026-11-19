"""Every simulator in the catalog loads the way OpenAVC's simulator loads it,
and each catalog entry's ``simulated`` says what that load serves.

The catalog build works ``simulated`` out from each simulator's source
(``scripts/catalog_simulators.py``), because it runs without the platform.
This imports every ``_sim.py`` through the simulator's own loader
(``openavc.simulator.simulator_file``) and holds the catalog to it:

* every file imports, and no driver id is refused (two classes naming it);
* the classes the source reading names are the classes the import serves;
* an entry is ``simulated`` exactly when the simulator serves it: a YAML
  driver from its definition, a Python driver from its ``<driver>_sim.py``.

Needs the real platform, and skips without it (fails when
``OPENAVC_REQUIRE_PLATFORM`` promised one).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from _platform_probe import REPO_ROOT, platform_on_path, platform_required
from catalog_files import DRIVER_DIRS, simulator_files

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _vendor.simulator_info import claim_conflicts, simulator_claims  # noqa: E402


def _simulator_files() -> list[Path]:
    return simulator_files(REPO_ROOT)


def _platform():
    """The simulator's loader and its manager, from the real platform."""
    with platform_on_path():
        try:
            from openavc.simulator import simulator_file
            from openavc.simulator.engine import SimulatorManager
        except ModuleNotFoundError as exc:
            if platform_required():
                raise AssertionError(
                    f"OPENAVC_REQUIRE_PLATFORM=1 promised a platform with the simulator's "
                    f"loader, but importing it failed: {exc}"
                ) from None
            pytest.skip(f"Needs the openavc platform's simulator loader ({exc}). NOT checked: "
                        "that every simulator loads and the catalog's simulated flags.")
        return simulator_file, SimulatorManager


@pytest.fixture(scope="module")
def loaded():
    simulator_file, _ = _platform()
    return {path: simulator_file.load_simulator_file(path) for path in _simulator_files()}


def test_every_simulator_loads_and_serves_every_id_it_names() -> None:
    simulator_file, _ = _platform()
    folder = simulator_file.load_simulator_folder(_simulator_files())
    assert folder.refused == {}
    assert len(folder.classes) == len(_simulator_files())


def test_the_source_reading_names_the_classes_the_import_serves(loaded) -> None:
    differ = {}
    for path, found in loaded.items():
        claims = simulator_claims(path)
        refused = claim_conflicts((c.class_name, c.driver_id) for c in claims)
        read = {c.driver_id: c.class_name for c in claims if c.driver_id not in refused}
        served = {driver_id: cls.__name__ for driver_id, cls in found.classes.items()}
        if read != served:
            differ[path.relative_to(REPO_ROOT).as_posix()] = {"read": read, "served": served}
    assert differ == {}


def test_each_entry_is_simulated_exactly_when_the_simulator_serves_it(loaded) -> None:
    _, manager_class = _platform()
    manager = manager_class()
    manager.discover([str(REPO_ROOT / folder) for folder in DRIVER_DIRS])
    index = json.loads((REPO_ROOT / "index.json").read_text(encoding="utf-8"))
    wrong = {}
    for entry in index["drivers"]:
        if entry["format"] == "avcdriver":
            serves = entry["id"] in manager._available
        else:
            sim_path = (REPO_ROOT / entry["file"]).with_name(Path(entry["file"]).stem + "_sim.py")
            serves = sim_path in loaded and entry["id"] in loaded[sim_path].classes
        if bool(entry.get("simulated")) != serves:
            wrong[entry["id"]] = {"catalog": bool(entry.get("simulated")), "simulator": serves}
    assert wrong == {}
