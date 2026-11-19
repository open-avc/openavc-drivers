"""Which files are the catalog's drivers: the one rule every reader asks.

A driver is a ``.avcdriver`` file, or a ``.py`` file that is not a Python
driver's companion (its ``<id>_discovery.py`` probe or its ``<id>_sim.py``
simulator), sitting directly in one of the ten category folders. A file in a
sub-folder, in a new folder or at the repository root is not in the catalog,
and the build refuses it (``files_outside_folders`` lists the candidates).

The build, this repository's tests and the platform's corpus sweeps all read
the library through these functions, so a new category folder is one line in
``CATEGORY_FOLDERS``. Standard library only: a reader in another repository
loads this file by its path without the build's dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path

# The contract's categories (``spec.CATEGORIES``) and the folder each one's
# drivers live in. tests/test_build_index.py holds the keys to the contract.
CATEGORY_FOLDERS: dict[str, str] = {
    "audio": "audio",
    "camera": "cameras",
    "display": "displays",
    "lighting": "lighting",
    "power": "power",
    "projector": "projectors",
    "streaming": "streaming",
    "switcher": "switchers",
    "utility": "utility",
    "video": "video",
}

DRIVER_DIRS: tuple[str, ...] = tuple(sorted(CATEGORY_FOLDERS.values()))

# A Python driver's companions sit beside it, named for it: its discovery
# probe and its simulator. They are not drivers, and an install of the driver
# fetches them, in this order.
PYTHON_COMPANION_SUFFIXES = ("_discovery.py", "_sim.py")

# The repository's own tooling: test fixtures and the vendored platform rules
# are not contributions, whatever they declare.
TOOLING_DIRS = frozenset(("tests", "scripts"))


def category_files(repo_root: Path) -> list[Path]:
    """Every file directly in a category folder, a folder at a time, sorted."""
    files: list[Path] = []
    for dir_name in DRIVER_DIRS:
        dir_path = repo_root / dir_name
        if dir_path.is_dir():
            files.extend(sorted(path for path in dir_path.iterdir() if path.is_file()))
    return files


def is_driver_file(path: Path) -> bool:
    """A ``.avcdriver`` file, or a ``.py`` file that is not a companion."""
    if path.suffix == ".avcdriver":
        return True
    return path.suffix == ".py" and not path.name.endswith(PYTHON_COMPANION_SUFFIXES)


def driver_files(repo_root: Path) -> list[Path]:
    """The catalog's driver files, in the order the build reads them."""
    return [path for path in category_files(repo_root) if is_driver_file(path)]


def simulator_files(repo_root: Path) -> list[Path]:
    """Every simulator in the category folders."""
    return [path for path in category_files(repo_root) if path.name.endswith("_sim.py")]


def files_outside_folders(repo_root: Path) -> list[Path]:
    """Every file shaped like a driver that is not directly in a category folder.

    The walk leaves out hidden folders, ``__pycache__`` and the repository's
    tooling. A ``.py`` file here is only a candidate: the build reads it, and
    one that declares no ``DRIVER_INFO`` is not a driver.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        here = Path(dirpath)
        dirnames[:] = _folders_to_walk(here, repo_root, dirnames)
        if not _is_category_folder(here, repo_root):
            found.extend(here / name for name in sorted(filenames) if is_driver_file(here / name))
    return found


def _folders_to_walk(here: Path, repo_root: Path, names: list[str]) -> list[str]:
    """The sub-folders of ``here`` worth walking: not hidden, not a cache, not tooling."""
    tooling = TOOLING_DIRS if here == repo_root else frozenset()
    return sorted(
        name for name in names
        if not name.startswith(".") and name != "__pycache__" and name not in tooling
    )


def _is_category_folder(path: Path, repo_root: Path) -> bool:
    return path.parent == repo_root and path.name in DRIVER_DIRS


def folder_for(category: object) -> str | None:
    """The folder a driver of this category belongs in, or None."""
    return CATEGORY_FOLDERS.get(category) if isinstance(category, str) else None
