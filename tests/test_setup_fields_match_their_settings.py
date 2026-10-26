"""A setup action's field is required when the setting it fills is required.

A setup action (``kind: "setup"``) often asks for the value a device setting
needs, under the setting's own name, then tests it against the device and
offers to save it: a key, a password, an auth code. When that setting is
``required: true`` in ``config_schema``, a blank is never the right answer, so
the action's field declares ``required: true`` as well. The device page's
setup dialog keeps Start disabled until a required field is filled in;
without it, a blank value runs the whole test, and on a device that locks out
after a few failed logins it spends one of those attempts to learn nothing.

Corpus-wide platform-contract sweep: names no product, ships no captured
fixture, asserts a property that holds for every driver. Setup actions exist
only on Python drivers, so this reads each class loaded on the real platform
(``_platform_probe.python_drivers``): it runs in CI's platform job and skips
without a platform.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _platform_probe import driver_info, python_drivers  # noqa: E402


def _looser_than_their_setting(name: str, info: dict) -> list[str]:
    schema = info.get("config_schema") or {}
    if not isinstance(schema, dict):
        return []
    out = []
    for action in info.get("actions") or []:
        if not isinstance(action, dict) or action.get("kind") != "setup":
            continue
        params = action.get("params") or {}
        if not isinstance(params, dict):
            continue
        for key, param in params.items():
            setting = schema.get(key)
            if not isinstance(param, dict) or not isinstance(setting, dict):
                continue
            if setting.get("required") and not param.get("required"):
                out.append(f"{name} [{action.get('id')}.{key}]")
    return out


def test_a_setup_field_is_required_when_its_setting_is():
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
        found.extend(_looser_than_their_setting(path.name, info))
    assert not broken, "Could not read: " + "; ".join(broken)
    assert not found, (
        "These setup actions ask for a setting the driver marks required, but "
        "let the field be left blank, so the setup dialog runs with nothing in "
        "it. Add \"required\": True to the field:\n  " + "\n  ".join(found)
    )
