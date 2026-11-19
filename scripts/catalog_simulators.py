"""Which catalog drivers are simulated, worked out from the files, and the
check that two simulator classes never name one driver id.

OpenAVC's simulator generates a simulator for every ``.avcdriver``, and serves
a Python driver from the one class in its ``_sim.py`` files that names the
driver's id; installing a Python driver fetches the ``<driver>_sim.py`` beside
it. So a catalog entry is ``simulated`` when it is a YAML driver, or a Python
driver whose ``<driver>_sim.py`` has a class naming the driver's id and no
other class naming it. A ``simulated`` value written in the driver file is
not used.

Every simulator is read from its source through the platform's own reader
(``_vendor.simulator_info.simulator_claims``), never imported: the catalog
build runs without the platform. ``tests/test_simulators_load_on_the_platform.py``
imports each one the way the simulator does and holds this module to it.

Two classes naming one driver id, which the simulator refuses (neither is
used), are refused here across the catalog's simulator files. Two in one
file are the contract check's (``check_simulator_file``, run on each Python
driver's simulator); this module names the ones in two files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _vendor.python_source import SourceDecodeError
from _vendor.simulator_info import (
    SimulatorClaim,
    claim_conflicts,
    companion_simulator,
    simulator_claims,
)


def is_simulated(filepath: Path, raw: dict[str, Any]) -> bool:
    """Whether the simulator serves this driver, as its catalog entry says."""
    if filepath.suffix == ".avcdriver":
        return True
    sim_path = companion_simulator(filepath)
    if not sim_path.is_file():
        return False
    driver_id = raw.get("id")
    claims = _claims(sim_path)
    if driver_id in claim_conflicts(_named(claims)):
        return False
    return any(claim.driver_id == driver_id for claim in claims)


def simulator_errors(sim_files: list[Path], repo_root: Path) -> list[str]:
    """What the catalog refuses in its simulator files: a driver id classes
    in two of them name, and a Python driver's simulator whose id is written
    in code, so the catalog cannot tell which driver it simulates."""
    claims = {path: _claims(path) for path in sim_files}
    errors = _across_files(claims, repo_root)
    for path, found in claims.items():
        errors.extend(_unread_ids(path, found, repo_root))
    return errors


def _claims(sim_path: Path) -> list[SimulatorClaim]:
    try:
        return simulator_claims(sim_path)
    except SourceDecodeError:
        return []  # the contract check names a file the import cannot read


def _named(claims: list[SimulatorClaim]) -> list[tuple[str, str | None]]:
    return [(claim.class_name, claim.driver_id) for claim in claims]


def _across_files(claims: dict[Path, list[SimulatorClaim]], repo_root: Path) -> list[str]:
    """Each driver id classes in two or more of the files name."""
    files_by_id: dict[str, set[Path]] = {}
    for path, found in claims.items():
        for claim in found:
            if claim.driver_id is not None:
                files_by_id.setdefault(claim.driver_id, set()).add(path)
    named = [
        (f"{claim.class_name} in {_rel(path, repo_root)}", claim.driver_id)
        for path, found in claims.items()
        for claim in found
        if len(files_by_id.get(claim.driver_id or "", ())) > 1
    ]
    return list(claim_conflicts(named).values())


def _unread_ids(path: Path, found: list[SimulatorClaim], repo_root: Path) -> list[str]:
    if not any(claim.driver_id is None for claim in found):
        return []
    driver = path.with_name(path.name.removesuffix("_sim.py") + ".py")
    if not driver.is_file():
        return []
    return [
        f"{_rel(path, repo_root)}: SIMULATOR_INFO's driver_id is built in code, so the "
        "catalog cannot tell which driver it simulates. Write the driver's id as text."
    ]


def _rel(path: Path, repo_root: Path) -> str:
    return path.relative_to(repo_root).as_posix()
