"""Every driver declares its defaults twice; the two must not disagree.

``DRIVER_INFO["default_config"]`` and ``config_schema[field]["default"]`` are
read by different callers:

* ``get_driver_default_config`` reads ``default_config``, so THAT block is what
  a one-click install from discovery writes into the project, and what the
  runtime resolves under saved device config.
* ``config_schema`` drives the Add / Edit Device form.

Nothing in the platform checks that they match, and on 2026-08-27 two drivers
were shipping out of step. ``avproedge_mxnet_switch`` had its schema moved to
telnet on port 23 while ``default_config`` stayed on ssh port 22, so the form
advertised one connection and every install made a different one -- invisible
until a device would not connect. ``lea_connect`` declared a string enum
(``"2"``/``"4"``/``"8"``) whose ``default_config`` value was the integer ``4``,
matching no option, so the dropdown came up unselected.

Corpus-wide platform-contract sweep: names no product, ships no captured
fixture, asserts a property that holds for every driver.

ONE test rather than one per driver, on purpose. A per-file parametrization
skips whatever it cannot read, and the first version of this file passed with
62 of 94 drivers silently skipped -- which looks identical to coverage.

The guard against that is NOT a count. The second version asserted a floor of
80 inspected drivers, which was simply the number reachable on the machine it
was written on. CI installs fewer optional packages, reached 79, and went red
for a reason that had nothing to do with any driver. A corpus count is a
property of the environment, not of the corpus.

So the guard is the KIND of failure instead:

* Every ``.avcdriver`` must be inspected. YAML parses with no imports, so that
  floor is deterministic in every environment and cannot drift.
* A Python driver that will not import is reported and tolerated ONLY when the
  cause is a missing module -- that is what a lean environment looks like.
* Anything else -- no DRIVER_INFO found, a malformed block, an exception that
  is not an import error -- fails, because that is the corpus being wrong
  rather than the environment being small.
"""

from __future__ import annotations

import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _platform_stubs import StubBaseDriver, install_stubs, load_module  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
CATEGORIES = ["projectors", "displays", "switchers", "audio", "cameras",
              "video", "lighting", "power", "streaming", "utility"]


def _driver_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for cat in CATEGORIES:
        d = REPO / cat
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix == ".avcdriver":
                out.append(f)
            elif (f.suffix == ".py"
                  and not f.name.endswith(("_sim.py", "_discovery.py"))):
                out.append(f)
    return out


def _driver_info(path: pathlib.Path) -> dict | None:
    if path.suffix == ".avcdriver":
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    module = load_module(path.stem, path)
    best = None
    for value in vars(module).values():
        if not isinstance(value, type):
            continue
        # The stub base class is in every driver module's namespace and carries
        # its own DRIVER_INFO, so taking the first match finds the BASE and
        # inspects nothing. Take the driver's own subclass instead.
        if value is StubBaseDriver or not issubclass(value, StubBaseDriver):
            continue
        info = getattr(value, "DRIVER_INFO", None)
        if isinstance(info, dict) and info.get("id"):
            best = info
    return best


def test_no_driver_declares_two_different_defaults_for_one_field():
    install_stubs(base_driver=StubBaseDriver)

    inspected: list[str] = []
    missing_dependency: list[str] = []   # tolerated: a lean environment
    broken: list[str] = []               # never tolerated: the corpus is wrong
    disagreements: list[str] = []

    for path in _driver_files():
        try:
            info = _driver_info(path)
        except ImportError as exc:
            # The stubs do not provide every platform transport, and optional
            # third-party packages are absent from some jobs. That is the
            # environment being small, not a driver being wrong.
            # (ModuleNotFoundError is a subclass of ImportError.)
            missing_dependency.append(f"{path.name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            broken.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        if info is None:
            broken.append(f"{path.name}: no DRIVER_INFO found")
            continue

        defaults = info.get("default_config") or {}
        schema = info.get("config_schema") or {}
        if not isinstance(defaults, dict) or not isinstance(schema, dict):
            broken.append(
                f"{path.name}: default_config/config_schema not mappings")
            continue

        inspected.append(path.name)
        for key, spec in schema.items():
            if not isinstance(spec, dict) or "default" not in spec:
                continue
            if key not in defaults:
                continue  # a schema-only default contradicts nothing
            if defaults[key] != spec["default"]:
                disagreements.append(
                    f"{path.name} [{key}]: default_config={defaults[key]!r} "
                    f"config_schema default={spec['default']!r}")

    assert not disagreements, (
        "These drivers declare two different defaults for the same field, so "
        "the Add Device form shows one value and an install writes the "
        "other:\n  " + "\n  ".join(disagreements))

    assert not broken, (
        "These drivers could not be read for a reason that is NOT a missing "
        "dependency, so the sweep skipped them without saying so:\n  "
        + "\n  ".join(broken))

    # YAML parses with no imports at all, so this floor holds everywhere. It is
    # what stops the sweep quietly inspecting nothing.
    yaml_total = sum(1 for p in _driver_files() if p.suffix == ".avcdriver")
    yaml_seen = sum(1 for name in inspected if name.endswith(".avcdriver"))
    assert yaml_seen == yaml_total, (
        f"Only {yaml_seen} of {yaml_total} .avcdriver files were inspected. "
        f"YAML needs no imports, so the sweep is not reaching the corpus.")
    assert inspected, "inspected nothing at all"
