"""A command that erases, resets or deletes asks before it is sent by hand.

``confirm`` on the command (platform 0.36.0): the device page, its Quick Action
button, the Driver Builder's Live Test and a device audit ask first; macros,
triggers and panel buttons send it without asking. These are the catalog's
factory resets, the commands that delete something stored on the device (a
user, a key, a preset, a file), the ones that put a group of tuned settings back
to factory values, and the ones that stop or reroute a whole running system.

Reads each driver from its source (PyYAML, and the vendored Python-driver
reader), like the rest of the community CI. A driver whose commands are built
in code (the Chazy and Darwin controllers, Bose ControlSpace, the Netgear
switch) pins its own in its own test file, which loads the class.
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
    # Factory resets, and erasing the device's own configuration.
    ("switchers/avproedge_acmx.avcdriver", "factory_reset"),
    ("switchers/atlona_ome_ps62.avcdriver", "factory_reset"),
    ("video/novastar_h_series.avcdriver", "factory_reset"),
    ("switchers/atlona_ome_ms.py", "factory_reset"),
    ("cameras/aver_ptz.py", "factory_reset"),
    ("displays/viewsonic_cde.avcdriver", "restore_default"),
    ("switchers/extron_nav.py", "factory_reset"),
    ("switchers/extron_nav.py", "reset_device_name"),
    ("streaming/brightsign_player.py", "factory_reset"),
    ("audio/sennheiser_tcc2.avcdriver", "restore"),
    # Users, keys, pairings and registry values.
    ("audio/soundcorehero.py", "users_delete"),
    ("audio/soundcorehero.py", "users_create_api_key"),
    ("audio/soundcorehero.py", "users_remove_api_key"),
    ("streaming/brightsign_player.py", "delete_registry_key"),
    ("displays/lg_webos.py", "clear_pairing"),
    ("audio/turtle_bt_wallplate.avcdriver", "bt_clear_paired"),
    # Something stored on the device, deleted.
    ("cameras/axis_vapix.py", "preset_delete"),
    ("cameras/onvif_camera.py", "preset_delete"),
    ("cameras/panasonic_awhe.avcdriver", "preset_delete"),
    ("cameras/ptzoptics.py", "reset_preset"),
    ("cameras/sony_visca.py", "preset_reset"),
    ("cameras/sony_visca.py", "ptz_trace_delete"),
    ("cameras/visca_ip.py", "preset_reset"),
    ("switchers/atlona_uhd_pro3.avcdriver", "clear_preset"),
    ("video/novastar_h_series.avcdriver", "clear_preset"),
    ("streaming/epiphan_pearl.py", "delete_publisher"),
    ("video/vmix.py", "remove_input"),
    ("audio/soundcorehero.py", "player_remove"),
    ("audio/soundcorehero.py", "zone_remove"),
    ("audio/soundcorehero.py", "speaker_remove"),
    ("audio/soundcorehero.py", "input_remove"),
    ("audio/soundcorehero.py", "playlist_remove"),
    ("audio/soundcorehero.py", "playlist_remove_item"),
    ("audio/soundcorehero.py", "announcement_remove"),
    ("audio/soundcorehero.py", "preset_remove"),
    ("audio/soundcorehero.py", "action_remove"),
    ("audio/soundcorehero.py", "action_group_remove"),
    ("audio/soundcorehero.py", "project_remove"),
    ("audio/soundcorehero.py", "software_update_remove"),
    ("audio/soundcorehero.py", "file_remove"),
    # A group of tuned settings put back to factory values.
    ("displays/benq_display.py", "picture_reset"),
    ("displays/benq_display.py", "sound_reset"),
    ("cameras/aver_ptz.py", "image_defaults"),
    ("switchers/lightware_lw3.avcdriver", "reset_edid"),
    ("audio/turtle_bt_wallplate.avcdriver", "eq_clear"),
    ("audio/sennheiser_ewdx.avcdriver", "restore_channel_audio"),
    ("projectors/benq_projector.avcdriver", "lens_reset_center"),
    # Stops or reroutes the whole running system, or erases recorded readings.
    ("power/wattbox_ip.py", "reset_all"),
    ("switchers/extron_nav.py", "clear_av_ties"),
    ("switchers/extron_nav.py", "clear_usb_ties"),
    ("switchers/atlona_uhd_pro3.avcdriver", "route_reset"),
    ("switchers/atlona_ome_ps62.avcdriver", "route_reset"),
    ("video/novastar_h_series.avcdriver", "clear_screen"),
    ("video/qlab.py", "reset"),
    ("power/apc_rack_pdu.py", "reset_peak_power"),
    ("power/apc_rack_pdu.py", "reset_energy"),
    ("power/apc_rack_pdu.py", "reset_outlet_energy"),
    ("power/apc_rack_pdu.py", "reset_outlet_peak_load"),
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
