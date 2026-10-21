"""Locating the real OpenAVC platform, for the tests that need it.

The catalog and contract checks in this suite read driver files and need
nothing but ``requirements-dev.txt``; CI's first job proves it by installing
nothing else. The checks that load a Python driver run it on the real
platform instead (CI's second job clones it): the connect/disconnect run, the
commands loaded from each class, the defaults read from each class, the
guides' examples and the protocol-math tests. They all need to find a real
checkout, skip cleanly when there isn't one, and go loud when a CI job
*promised* one and failed to provide it — otherwise a job that meant to check
something passes while checking nothing.

Entry points:

* :func:`platform_required` — did the caller promise a platform
  (``OPENAVC_REQUIRE_PLATFORM``)? If so, "not found" is a failure, not a skip.
* :func:`platform_on_path` — a context manager that puts a located checkout on
  ``sys.path`` for the duration and takes it back off afterwards. **Restoring
  matters more than it looks.** Other modules in this suite skip when the
  platform is not importable; an entry left on ``sys.path`` silently un-skips
  them depending on collection order, which is how a "green" run once started
  including 27 tests nobody meant to run there.
* :func:`load_driver` — import one driver file against the real platform, or
  skip (fail when promised) without one.
* :func:`python_drivers` — every Python driver in the catalog, loaded once per
  run and shared by the checks that read each class, so a second check costs
  no second load.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import os
import pathlib
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

REQUIRE_PLATFORM_ENV = "OPENAVC_REQUIRE_PLATFORM"
PLATFORM_ROOT_ENV = "OPENAVC_PLATFORM_ROOT"

REPO_ROOT = Path(__file__).resolve().parent.parent

_TRUE = {"1", "true", "yes", "on"}


def platform_required() -> bool:
    """True when a CI job promised the platform, so absence must fail."""
    return os.environ.get(REQUIRE_PLATFORM_ENV, "").strip().lower() in _TRUE


def candidate_roots() -> list[Path]:
    """Where the openavc checkout might be, most explicit first."""
    roots: list[Path] = []
    configured = os.environ.get(PLATFORM_ROOT_ENV, "").strip()
    if configured:
        roots.append(Path(configured))
    # Beside this repo in the workspace, including a task worktree that shares
    # this one's suffix (openavc-drivers-wt-foo -> openavc-wt-foo).
    name = REPO_ROOT.name
    workspace = REPO_ROOT.parent
    roots.append(workspace / name.replace("openavc-drivers", "openavc", 1))
    roots.append(workspace / "openavc")
    return roots


def platform_root() -> Path | None:
    """The first candidate that actually holds a platform, or None."""
    for root in candidate_roots():
        if (root / "openavc" / "drivers" / "base.py").exists():
            return root
    return None


@contextlib.contextmanager
def platform_on_path():
    """Put a located platform checkout on ``sys.path`` for this block.

    Yields the root it found, or None. ``sys.path`` is always restored, and
    any ``openavc`` entry in ``sys.modules`` with no file behind it (a
    namespace left by a failed import) is dropped first, so the import below
    resolves against the real package. One root covers the simulator too.
    """
    path_before = list(sys.path)
    root = platform_root()
    if root is not None and str(root) not in sys.path:
        sys.path.insert(0, str(root))

    leaked = [
        name
        for name in sys.modules
        if name.split(".", 1)[0] == "openavc"
        and getattr(sys.modules[name], "__file__", None) is None
    ]
    for name in leaked:
        del sys.modules[name]

    # Importing the platform resolves its data directory and opens a log there.
    # Point it at a temp dir: the default would drop an untracked folder into a
    # contributor's checkout just for running the suite.
    os.environ.setdefault(
        "OPENAVC_DATA_DIR",
        str(pathlib.Path(tempfile.gettempdir()) / "openavc-drivers-tests"),
    )
    try:
        yield root
    finally:
        sys.path[:] = path_before


CATEGORIES = (
    "projectors", "displays", "switchers", "audio", "cameras", "video",
    "lighting", "power", "streaming", "utility",
)


def python_driver_files() -> list[Path]:
    """Every Python driver in the catalog's folders (not simulators or companions)."""
    files = []
    for category in CATEGORIES:
        for path in sorted((REPO_ROOT / category).glob("*.py")):
            if not path.stem.startswith("_") and not path.stem.endswith(("_sim", "_discovery")):
                files.append(path)
    return files


def _import_file(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _require_platform(what: str) -> None:
    """Import the platform, or skip (fail when a job promised it)."""
    try:
        import openavc.drivers.base  # noqa: F401
    except ModuleNotFoundError:
        if platform_required():
            raise AssertionError(
                f"{REQUIRE_PLATFORM_ENV}=1 promised the openavc platform, but it is "
                f"not importable. Not checked: {what}."
            ) from None
        pytest.skip(f"Needs the openavc platform. NOT checked: {what}.",
                    allow_module_level=True)


def load_driver(relative_path: str) -> ModuleType:
    """Import one file of this repo (``"audio/toa_9000m2.py"``) on the real platform."""
    path = REPO_ROOT / relative_path
    with platform_on_path():
        _require_platform(path.name)
        return _import_file(path, f"_driver_{path.stem}")


def driver_info(module: ModuleType) -> dict | None:
    """The DRIVER_INFO of the driver class the module itself defines."""
    for value in vars(module).values():
        info = getattr(value, "DRIVER_INFO", None)
        if (isinstance(value, type) and isinstance(info, dict)
                and value.__module__ == module.__name__):
            return info
    return None


@functools.lru_cache(maxsize=None)
def python_drivers() -> tuple[tuple[Path, ModuleType | Exception], ...]:
    """Every Python driver loaded once on the real platform: (path, module or why not)."""
    with platform_on_path():
        _require_platform("every Python driver's class")
        loaded = []
        for path in python_driver_files():
            try:
                loaded.append((path, _import_file(path, f"_driver_{path.stem}")))
            except Exception as exc:  # noqa: BLE001 - each caller names the driver
                loaded.append((path, exc))
        return tuple(loaded)
