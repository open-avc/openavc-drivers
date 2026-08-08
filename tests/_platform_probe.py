"""Locating the real OpenAVC platform, for the tests that need it.

Most of this suite runs against the stand-ins in ``_platform_stubs.py``, with
no ``openavc`` package installed — that is deliberate, and CI proves it by
installing nothing but ``requirements-dev.txt``. A couple of tests are the
exception: they exist precisely to ask the platform whether what this repo
believes about it is true (``test_platform_stub_fidelity.py`` for the stubs,
``test_doc_python_examples.py`` for the guides). Both need to find a real
checkout, both must skip cleanly when there isn't one, and both must go loud
when a CI job *promised* one and failed to provide it — otherwise a job that
meant to check something passes while checking nothing.

That resolution was written once, for the fidelity test. This module is it,
lifted out so the second caller shares it rather than growing a second copy —
the same reason ``_platform_stubs.py`` exists.

Two entry points:

* :func:`platform_required` — did the caller promise a platform
  (``OPENAVC_REQUIRE_PLATFORM``)? If so, "not found" is a failure, not a skip.
* :func:`platform_on_path` — a context manager that puts a located checkout on
  ``sys.path`` for the duration and takes it back off afterwards. **Restoring
  matters more than it looks.** Other modules in this suite skip when the
  platform is not importable; an entry left on ``sys.path`` silently un-skips
  them depending on collection order, which is how a "green" run once started
  including 27 tests nobody meant to run there.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import sys
import tempfile
from pathlib import Path

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
    any partially-stubbed ``openavc`` entries another module leaked into
    ``sys.modules`` are dropped first, so the import below can never resolve
    against a stand-in. One root covers the simulator too now.
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
        str(pathlib.Path(tempfile.gettempdir()) / "openavc-stub-fidelity"),
    )
    try:
        yield root
    finally:
        sys.path[:] = path_before
