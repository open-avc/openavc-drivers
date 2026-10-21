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

Two halves, like ``test_commands_that_erase_ask_first.py``. A ``.avcdriver``
is read with PyYAML, so that half runs everywhere. A Python driver's blocks
are read from its class, loaded on the real platform: a reader that skips
what it cannot evaluate (a module constant, a helper call) looks identical to
coverage, so every Python driver must load, and that half runs in CI's
platform job and skips without a platform. The load is shared with the other
checks that read each class (``_platform_probe.python_drivers``).
"""

from __future__ import annotations

import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _platform_probe import (  # noqa: E402
    CATEGORIES,
    REPO_ROOT,
    driver_info,
    python_drivers,
)


def _disagreements(name: str, info: dict) -> list[str]:
    defaults = info.get("default_config") or {}
    schema = info.get("config_schema") or {}
    if not isinstance(defaults, dict) or not isinstance(schema, dict):
        return [f"{name}: default_config/config_schema not mappings"]
    out = []
    for key, spec in schema.items():
        if not isinstance(spec, dict) or "default" not in spec:
            continue
        if key not in defaults:
            continue  # a schema-only default contradicts nothing
        if defaults[key] != spec["default"]:
            out.append(
                f"{name} [{key}]: default_config={defaults[key]!r} "
                f"config_schema default={spec['default']!r}")
    return out


_REMEDY = (
    "These drivers declare two different defaults for the same field, so the "
    "Add Device form shows one value and an install writes the other:\n  ")


def test_no_yaml_driver_declares_two_different_defaults_for_one_field():
    files = [f for c in CATEGORIES for f in sorted((REPO_ROOT / c).glob("*.avcdriver"))]
    assert files, "found no .avcdriver files"
    found: list[str] = []
    for path in files:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict), f"{path.name} is not a mapping"
        found.extend(_disagreements(path.name, loaded))
    assert not found, _REMEDY + "\n  ".join(found)


def test_no_python_driver_declares_two_different_defaults_for_one_field():
    found: list[str] = []
    broken: list[str] = []
    for path, module in python_drivers():
        if isinstance(module, Exception):
            broken.append(f"{path.name}: {module!r}")
            continue
        info = driver_info(module)
        if info is None:
            broken.append(f"{path.name}: no class with DRIVER_INFO")
            continue
        found.extend(_disagreements(path.name, info))
    assert not broken, "Could not read: " + "; ".join(broken)
    assert not found, _REMEDY + "\n  ".join(found)
