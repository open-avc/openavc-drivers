"""A command whose name says it erases something declares ``confirm``.

``confirm`` (platform 0.36.0) makes a command ask before it is sent by hand:
the device page, its Quick Action button, the Driver Builder's Live Test and a
device audit. A factory reset that sends on one click is how a unit gets wiped
by mistake, so every command whose id or label says factory, erase, wipe,
delete or remove, resets or clears "all", or resets or restores defaults
declares one: the sentence to ask, saying what is lost, or ``confirm: false``
when nothing is.

A few names match and erase nothing (a recall of a factory scene, taking a
member out of a group). Those are listed below with the reason, rather than
given a ``confirm: false`` that would raise the driver's platform floor for no
change in behaviour.

Two halves. The first reads every driver from its source, like the rest of
this repo's CI. The second imports every Python driver against the real
platform, because some build their commands in code and the source shows an
empty table (the Chazy and Darwin controllers, whose factory resets went out
unasked that way); it runs in the platform job and skips without a platform.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _platform_probe import (  # noqa: E402
    REQUIRE_PLATFORM_ENV,
    platform_on_path,
    platform_required,
)
from _vendor.python_info import extract_python_driver_info_full  # noqa: E402

CATEGORIES = (
    "projectors", "displays", "switchers", "audio", "cameras", "video",
    "lighting", "power", "streaming", "utility",
)

_ERASES = re.compile(
    r"factory|erase|wipe|delete|\bremove\b"
    r"|\b(?:reset|clear)\s+all\b"
    r"|\b(?:reset|restore)\b.*\bdefaults?\b",
    re.IGNORECASE,
)

# (driver id, command) -> why it sends without asking.
_LEAVES_A_GROUP = "takes a member out of a group or list; nothing stored is deleted"
SENDS_WITHOUT_ASKING: dict[tuple[str, str], str] = {
    ("chazy_control_pro", "group_del_dec"): _LEAVES_A_GROUP,
    ("soundcorehero", "player_remove_zones"): _LEAVES_A_GROUP,
    ("soundcorehero", "player_remove_playlist"): _LEAVES_A_GROUP,
    ("soundcorehero", "zone_remove_players"): _LEAVES_A_GROUP,
    ("soundcorehero", "zone_remove_inputs"): _LEAVES_A_GROUP,
    ("soundcorehero", "zone_remove_speakers"): _LEAVES_A_GROUP,
    ("soundcorehero", "speaker_remove_zone"): _LEAVES_A_GROUP,
    ("soundcorehero", "speaker_remove_channel"): _LEAVES_A_GROUP,
    ("soundcorehero", "input_remove_channels"): _LEAVES_A_GROUP,
    ("soundcorehero", "input_remove_zones"): _LEAVES_A_GROUP,
    ("soundcorehero", "dso_remove_channel"): _LEAVES_A_GROUP,
    ("vmix", "input_bus_off"): "stops sending one input's audio to a bus, a routing change",
    ("axis_vapix", "overlay_remove"): "takes off an overlay that Add Overlay puts back from the same fields",
    ("behringer_x32", "clear_solo"): "releases the solos, which only change what the engineer monitors",
    ("vaddio_roboshot", "ccu_scene_recall_factory"): "recalls one of Vaddio's factory image scenes; nothing stored is lost",
}


def _driver_files() -> list[Path]:
    files = []
    for category in CATEGORIES:
        for path in sorted((REPO_ROOT / category).glob("*")):
            if path.suffix == ".avcdriver":
                files.append(path)
            elif (
                path.suffix == ".py"
                and not path.stem.startswith("_")
                and not path.stem.endswith(("_sim", "_discovery"))
            ):
                files.append(path)
    return files


def _unasked(driver_id: str, commands: object) -> list[str]:
    """Commands whose name says they erase something and that declare no confirm."""
    if not isinstance(commands, dict):
        return []
    found = []
    for command, definition in commands.items():
        if not isinstance(command, str) or not isinstance(definition, dict):
            continue
        label = definition.get("label")
        name = f"{command.replace('_', ' ')} {label if isinstance(label, str) else ''}"
        if not _ERASES.search(name) or "confirm" in definition:
            continue
        if (driver_id, command) in SENDS_WITHOUT_ASKING:
            continue
        found.append(f"{driver_id}.{command} ({label})")
    return found


_REMEDY = (
    "Declare confirm on each: the sentence to ask, saying what is lost "
    "(\"Erases every preset and returns the unit to DHCP.\"), or confirm: false "
    "if it erases nothing. A judged exception that needs no driver change goes "
    "in SENDS_WITHOUT_ASKING with the reason."
)


def test_every_command_that_erases_asks_first_read_from_source():
    unasked: list[str] = []
    seen: set[tuple[str, str]] = set()
    for path in _driver_files():
        if path.suffix == ".avcdriver":
            info = yaml.safe_load(path.read_text(encoding="utf-8"))
        else:
            info, _ = extract_python_driver_info_full(path)
        driver_id = info.get("id", path.stem)
        commands = info.get("commands")
        if isinstance(commands, dict):
            seen.update((driver_id, c) for c in commands)
        unasked.extend(_unasked(driver_id, commands))
    assert not unasked, f"{len(unasked)} send unasked: {unasked}. {_REMEDY}"

    readable = {driver_id for driver_id, _ in seen}
    stale = [key for key in SENDS_WITHOUT_ASKING if key[0] in readable and key not in seen]
    assert not stale, f"SENDS_WITHOUT_ASKING names commands that no longer exist: {stale}"


def _load(path: Path):
    name = f"_erase_sweep_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _driver_info(module) -> dict | None:
    for value in vars(module).values():
        info = getattr(value, "DRIVER_INFO", None)
        if isinstance(value, type) and isinstance(info, dict) and value.__module__ == module.__name__:
            return info
    return None


def test_every_command_that_erases_asks_first_with_the_class_loaded():
    with platform_on_path() as root:
        try:
            import openavc.drivers.base  # noqa: F401
        except ModuleNotFoundError:
            if platform_required():
                raise AssertionError(
                    f"{REQUIRE_PLATFORM_ENV}=1 promised the openavc platform, but it is "
                    "not importable, so no Python driver's built command table was read."
                ) from None
            pytest.skip(
                "Needs the openavc platform to load each Python driver; the "
                "commands a driver builds in code were NOT checked."
            )

        unasked: list[str] = []
        seen: set[tuple[str, str]] = set()
        failed: list[str] = []
        for path in _driver_files():
            if path.suffix != ".py":
                continue
            try:
                info = _driver_info(_load(path))
            except Exception as exc:  # noqa: BLE001 - name the driver, whatever broke
                failed.append(f"{path.name}: {exc!r}")
                continue
            if info is None:
                failed.append(f"{path.name}: no class with DRIVER_INFO")
                continue
            driver_id = info.get("id", path.stem)
            commands = info.get("commands")
            if isinstance(commands, dict):
                seen.update((driver_id, c) for c in commands)
            unasked.extend(_unasked(driver_id, commands))

    assert not failed, f"Could not load: {failed}"
    assert not unasked, f"{len(unasked)} send unasked: {unasked}. {_REMEDY}"
    python_ids = {driver_id for driver_id, _ in seen}
    stale = [key for key in SENDS_WITHOUT_ASKING if key[0] in python_ids and key not in seen]
    assert not stale, f"SENDS_WITHOUT_ASKING names commands that no longer exist: {stale}"
