"""Driver-owned result shapes for the exact historical compatibility inputs.

These synthetic values pin each mapping's field names/types. They do not replace
handler execution, platform-loader identity or installed-upgrade qualification.
"""
import hashlib
import json
from pathlib import Path

import pytest

from scripts._vendor.setup_compatibility import SetupCompatibility

ROOT = Path(__file__).resolve().parents[1]
SOURCES = json.loads((ROOT / "setup-compatibility/manifest.json").read_text())["sources"]
MAPPINGS = [entry for entry in SOURCES if entry["actions"]]

RESULTS = {
    "biamp_tesira_ttp": {"firmware": "", "blocks_checked": 0, "blocks_ok": [], "blocks_failed": []},
    "bose_controlspace": {"ok": True, "message": "Answered", "answered": ["Input"], "silent": [],
                          "rejected": [], "problems": ["Invalid configured row"], "push_supported": False,
                          "parameter_set": None, "ip_address": None},
    "bss_soundweb_london": {"ok": False, "message": "No answer", "answered": [], "silent": ["Input"],
                           "problems": [], "unit_node": "", "nodes_seen": []},
    "qsc_qrc": {"reachable": True, "platform": "?", "design": "?", "component_count": 0,
                "components": [], "hint": "No component answer"},
    "vaddio_conferenceshot_av": {"success": False, "message": "Accepted change; port did not answer"},
    "panasonic_display": {"reachable": True, "protocol": "protocol2", "auth_enabled": False,
                          "auth_ok": False, "saved": False, "message": "No authentication required"},
    "sony_bravia": {"reachable": True, "auth_ok": True, "saved": False, "model": "", "message": "Accepted"},
    "philips_hue": {"paired": True, "bridge_name": "Fixture", "bridge_id": "000001",
                    "app_key": "invented-generated-key", "saved": False},
    "apc_rack_pdu": {"success": False, "message": "No reply"},
    "barco_pulse": {"model": None, "serial": "000001", "firmware": None, "state": None, "authenticated": False},
    "panasonic_pt": {"reachable": True, "auth_enabled": True, "auth_ok": True, "saved": True, "message": "Accepted"},
    "sony_vpl": {"reachable": True, "auth_enabled": False, "auth_ok": False, "saved": False, "message": "Disabled"},
    "mersive_solstice": {"reachable": True, "auth_ok": True, "saved": False, "display_name": "Fixture", "message": "Accepted"},
    "avproedge_mxnet_10g": {"success": True, "message": "Identification reported"},
    "avproedge_mxnet_1g": {"success": True, "message": "Identification reported"},
    "blackmagic_videohub": {"success": True, "message": "Identification reported"},
    "crestron_nvx": {"provisioned": True, "username": "operator"},
    "tvone_coriomatrix": {"success": True, "message": "Logged in"},
    "netgear_m4250_m4350": {"ssh_enabled": True, "transport": "ssh", "port": 22},
    "snmp_v2c": {"success": False, "message": "Configured OID did not answer"},
    "poly_studio": {"reachable": True, "auth_ok": True, "saved": False, "message": "Accepted"},
    "tvone_coriomaster": {"success": True, "message": "Logged in"},
}


def test_current_catalog_python_sources_have_exact_coverage_inputs():
    inputs = {(entry['path'], entry['source_sha256'], entry['driver_id'], entry['driver_version'])
              for entry in SOURCES}
    catalog = json.loads((ROOT / 'index.json').read_text())['drivers']
    missing = []
    for entry in catalog:
        path = entry['file']
        if path.endswith('.py'):
            digest = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            if (path, digest, entry['id'], entry['version']) not in inputs:
                missing.append(path)
    assert not missing, f'Qualify and retain current source coverage inputs: {missing}'


def descriptor(entry):
    mapping, = entry["actions"]
    return SetupCompatibility.parse({"schema": "setup-compatibility/v1",
        "driver_id": entry["driver_id"], "driver_version": entry["driver_version"],
        "source_sha256": entry["source_sha256"], "declaration_sha256": "1" * 64,
        "action_sha256": "2" * 64, **mapping})


def test_every_maintained_mapping_has_a_driver_owned_result_case():
    assert {item["driver_id"] for item in MAPPINGS} == set(RESULTS)


@pytest.mark.parametrize("entry", MAPPINGS, ids=lambda entry: entry["driver_id"])
def test_result_fields_match_the_exact_declared_mapping(entry):
    selected = descriptor(entry)
    raw = RESULTS[entry["driver_id"]]
    assert selected.read_roles(raw)
    for field in selected.diagnostics:
        present, _value = field.read(raw)
        assert present
    if selected.generated_secret:
        assert raw[selected.generated_secret] == "invented-generated-key"


def test_provisioning_claims_and_pairing_keys_have_distinct_roles():
    mappings = {entry["driver_id"]: descriptor(entry) for entry in MAPPINGS}
    for name in ("vaddio_conferenceshot_av", "crestron_nvx", "netgear_m4250_m4350"):
        assert mappings[name].profile == "provisioning-claim/v1"
        assert mappings[name].generated_secret is None
    assert mappings["qsc_qrc"].profile == "inventory-check/v1"
    assert mappings["barco_pulse"].profile == "session-check/v1"
    pairing = mappings["philips_hue"]
    assert pairing.generated_secret == "app_key"
    assert "app_key" not in {field.key for field in pairing.diagnostics}
