"""A command that erases or resets the device asks before it is sent by hand.

``confirm`` on the command (platform 0.36.0): the device page, its Quick Action
button, the Driver Builder's Live Test and a device audit ask first; macros,
triggers and panel buttons send it without asking. These are the catalog's
factory resets and the user commands that delete an account or replace a key.

Reads each driver from its source (PyYAML, and the vendored Python-driver
reader), like the rest of the community CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _vendor.python_info import extract_python_driver_info_full  # noqa: E402

ASKS_FIRST = [
    ("switchers/avproedge_acmx.avcdriver", "factory_reset"),
    ("switchers/atlona_ome_ps62.avcdriver", "factory_reset"),
    ("video/novastar_h_series.avcdriver", "factory_reset"),
    ("switchers/atlona_ome_ms.py", "factory_reset"),
    ("cameras/aver_ptz.py", "factory_reset"),
    ("audio/soundcorehero.py", "users_delete"),
    ("audio/soundcorehero.py", "users_create_api_key"),
]


def _info(rel: str) -> dict:
    path = REPO_ROOT / rel
    if path.suffix == ".avcdriver":
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    info, _ = extract_python_driver_info_full(path)
    return info


@pytest.mark.parametrize("rel, command", ASKS_FIRST)
def test_the_command_asks_first_in_its_own_words(rel, command):
    info = _info(rel)
    confirm = info["commands"][command].get("confirm")
    assert isinstance(confirm, str) and confirm.strip(), (rel, command)
    assert "—" not in confirm
    assert str(info["min_platform_version"]) == "0.36.0"


def test_the_acmx_reset_help_has_no_em_dash():
    info = _info("switchers/avproedge_acmx.avcdriver")
    assert "—" not in info["commands"]["factory_reset"]["help"]
