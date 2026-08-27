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

Deliberately ONE test rather than one per driver. A per-file parametrization
skips whatever it cannot read, and the first version of this file passed with
62 of 94 drivers silently skipped -- which looks identical to coverage. The
floor assertion below is what makes an unreadable corpus fail instead.
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

# The corpus only grows. If a future refactor makes drivers unreadable here,
# this is what turns the run red instead of quietly inspecting nothing.
#
# 80, not 94: about fourteen Python drivers import platform modules the test
# stubs do not provide (openavc.transport.udp and friends), so they cannot be
# read in this job at all. That is a real coverage gap, named here rather than
# hidden -- the failure message lists every driver it could not read, so the
# gap stays visible instead of quietly becoming the norm. Raise this number
# when the stub set grows to cover them.
MINIMUM_DRIVERS_INSPECTED = 80


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
        # its own empty DRIVER_INFO, so taking the first match finds the BASE
        # and inspects nothing. Take the driver's own subclass instead.
        if value is StubBaseDriver or not issubclass(value, StubBaseDriver):
            continue
        info = getattr(value, "DRIVER_INFO", None)
        if isinstance(info, dict) and info.get("id"):
            best = info
    return best


def test_no_driver_declares_two_different_defaults_for_one_field():
    install_stubs(base_driver=StubBaseDriver)

    inspected: list[str] = []
    unreadable: list[str] = []
    disagreements: list[str] = []

    for path in _driver_files():
        try:
            info = _driver_info(path)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            unreadable.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        if info is None:
            unreadable.append(f"{path.name}: no DRIVER_INFO found")
            continue

        defaults = info.get("default_config") or {}
        schema = info.get("config_schema") or {}
        if not isinstance(defaults, dict) or not isinstance(schema, dict):
            unreadable.append(f"{path.name}: default_config/config_schema not mappings")
            continue

        inspected.append(path.name)
        for key, spec in schema.items():
            if not isinstance(spec, dict) or "default" not in spec:
                continue
            if key not in defaults:
                continue  # schema-only default contradicts nothing
            if defaults[key] != spec["default"]:
                disagreements.append(
                    f"{path.name} [{key}]: default_config={defaults[key]!r} "
                    f"config_schema default={spec['default']!r}")

    assert not disagreements, (
        "These drivers declare two different defaults for the same field, so "
        "the Add Device form shows one value and an install writes the "
        "other:\n  " + "\n  ".join(disagreements))

    assert len(inspected) >= MINIMUM_DRIVERS_INSPECTED, (
        f"Only inspected {len(inspected)} drivers, expected at least "
        f"{MINIMUM_DRIVERS_INSPECTED} -- this sweep is not covering the "
        f"corpus, which passes for free.\nUnreadable:\n  "
        + "\n  ".join(unreadable))
