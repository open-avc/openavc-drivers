"""Pins for the catalog's discovery checks: every sentence, in order.

Each case is a driver's ``discovery:`` block (or a small catalog of them) run
through the same steps the catalog build takes: the per-driver block check,
then the cross-driver claims (signal collisions, locator ambiguity) over the
drivers whose blocks produced a normalised form, and the ``requires`` stamp an
entry is written with. What is recorded is the result: the errors in order,
the normalised form, anything written to stderr, the stamped block.

The expected results live in ``fixtures/build_index_discovery_pins.json``.
They record what the checks say today, right or wrong; a sentence that should
change is changed on purpose, and the fixture regenerated with

    python tests/test_build_index_discovery_pins.py --regen
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

from build_index import (  # noqa: E402
    _validate_discovery_block as discovery_block_errors,
    _validate_locator_disambiguation as locator_errors,
    _validate_no_signal_collisions as collision_errors,
    _with_discovery_requires as with_discovery_requires,
)

FIXTURE = HERE / "fixtures" / "build_index_discovery_pins.json"
FILE = "audio/acme_widget.avcdriver"


def d(discovery: Any = None, *, driver_id: Any = "acme_widget", **extra: Any) -> dict:
    """A driver dict carrying ``discovery`` (left out when it is ``...``)."""
    raw: dict[str, Any] = {"id": driver_id, **extra}
    if discovery is not ...:
        raw["discovery"] = discovery
    return raw


def tcp(**probe: Any) -> dict:
    return d({"tcp_probe": probe})


def udp(**probe: Any) -> dict:
    return d({"udp_probe": probe})


def then(**step: Any) -> dict:
    return tcp(port=5000, send_ascii="Q", expect="A", then=step)


def extract(spec: Any) -> dict:
    return tcp(port=5000, extract=spec)


# --- One driver's block ------------------------------------------------------
#
# (name, driver dict, whether the block is checked with a yaml_dir holding a
# companion file called ``present_discovery.py``)

BLOCK_CASES: list[tuple[str, dict, bool]] = [
    # Which drivers are checked at all, and the block's own shape.
    ("generic_exempt", d({"bogus": 1}, driver_id="generic_tcp"), False),
    ("generic_prefix_only_at_start", d({"bogus": 1}, driver_id="acme_generic_x"), False),
    ("no_block", d(...), False),
    ("block_none", d(None), False),
    ("block_empty", d({}), False),
    ("block_list", d([{"mdns": "_x._tcp"}]), False),
    ("block_string", d("mdns"), False),
    ("block_false", d(False), False),
    ("id_missing", d({"oui": ["00:11:22"]}, driver_id=None), False),
    ("id_number", d({"oui": ["00:11:22"]}, driver_id=7), False),
    ("unknown_keys", d({"mdsn": "_x._tcp", "zeta": 1, "oui": ["00:11:22"]}), False),
    # requires
    ("requires_valid", d({"requires": "0.23.0", "oui": ["00:11:22"]}), False),
    ("requires_suffix", d({"requires": "0.23.0-rc1", "oui": ["00:11:22"]}), False),
    ("requires_word", d({"requires": "latest", "oui": ["00:11:22"]}), False),
    ("requires_number", d({"requires": 23, "oui": ["00:11:22"]}), False),
    ("requires_empty", d({"requires": "", "oui": ["00:11:22"]}), False),
    ("requires_only", d({"requires": "0.23.0"}), False),
    # mDNS
    ("mdns_string", d({"mdns": "_acme._tcp"}), False),
    ("mdns_string_upper", d({"mdns": "_ACME._tcp"}), False),
    ("mdns_empty_string", d({"mdns": ""}), False),
    ("mdns_list_mixed", d({"mdns": ["_a._tcp", {"service": "_b._tcp"}, 5, ""]}), False),
    ("mdns_mapping_txt", d({"mdns": {"service": "_a._tcp", "txt": {"Model": "X1", "n": 2}}}), False),
    ("mdns_mapping_txt_none", d({"mdns": {"service": "_a._tcp", "txt": None}}), False),
    ("mdns_mapping_no_service", d({"mdns": {"txt": {"a": "b"}}}), False),
    ("mdns_mapping_service_empty", d({"mdns": {"service": ""}}), False),
    ("mdns_mapping_service_number", d({"mdns": {"service": 5}}), False),
    ("mdns_mapping_txt_list", d({"mdns": {"service": "_a._tcp", "txt": ["a"]}}), False),
    ("mdns_mapping_unknown", d({"mdns": {"service": "_a._tcp", "name": "x", "a": 1}}), False),
    ("mdns_mapping_unknown_no_service", d({"mdns": {"name": "x"}}), False),
    ("mdns_cross_vendor_ok", d({"mdns": {"service": "_a._tcp", "cross_vendor": True}}), False),
    ("mdns_cross_vendor_bad", d({"mdns": {"service": "_a._tcp", "cross_vendor": "yes"}}), False),
    ("mdns_cross_vendor_none", d({"mdns": {"service": "_a._tcp", "cross_vendor": None}}), False),
    ("mdns_none", d({"mdns": None}), False),
    ("mdns_empty_list", d({"mdns": []}), False),
    ("mdns_number", d({"mdns": 7}), False),
    # SSDP
    ("ssdp_string", d({"ssdp": "urn:acme:device:Widget:1"}), False),
    ("ssdp_empty_string", d({"ssdp": ""}), False),
    ("ssdp_mapping_filters", d({"ssdp": {
        "device_type": "urn:x", "model": "W1", "manufacturer": "Acme",
        "friendly_name": "Widget",
    }}), False),
    ("ssdp_filter_none_skipped", d({"ssdp": {"device_type": "urn:x", "model": None}}), False),
    ("ssdp_filter_empty", d({"ssdp": {"device_type": "urn:x", "model": "", "manufacturer": "A"}}), False),
    ("ssdp_filter_number", d({"ssdp": {"device_type": "urn:x", "friendly_name": 5}}), False),
    ("ssdp_no_device_type", d({"ssdp": {"model": "W1"}}), False),
    ("ssdp_device_type_number", d({"ssdp": {"device_type": 3}}), False),
    ("ssdp_unknown", d({"ssdp": {"device_type": "urn:x", "serial": "1"}}), False),
    ("ssdp_cross_vendor_bad", d({"ssdp": {"device_type": "urn:x", "cross_vendor": 1}}), False),
    ("ssdp_cross_vendor_ok", d({"ssdp": {"device_type": "urn:x", "cross_vendor": False}}), False),
    ("ssdp_list_mixed", d({"ssdp": ["urn:a", {"device_type": "urn:b", "model": "M"}, [], ""]}), False),
    ("ssdp_number", d({"ssdp": 2}), False),
    # AMX DDP
    ("amx_ok_default_pattern", d({"amx_ddp": {"make": "Acme"}}), False),
    ("amx_ok_pattern", d({"amx_ddp": {"make": "Acme", "model_pattern": "W*"}}), False),
    ("amx_string", d({"amx_ddp": "Acme"}), False),
    ("amx_no_make", d({"amx_ddp": {"model_pattern": "W*"}}), False),
    ("amx_make_empty", d({"amx_ddp": {"make": ""}}), False),
    ("amx_pattern_number", d({"amx_ddp": {"make": "Acme", "model_pattern": 5}}), False),
    ("amx_pattern_none", d({"amx_ddp": {"make": "Acme", "model_pattern": None}}), False),
    ("amx_unknown", d({"amx_ddp": {"make": "Acme", "sku": "1"}}), False),
    ("amx_cross_vendor_bad", d({"amx_ddp": {"make": "Acme", "cross_vendor": "no"}}), False),
    ("amx_list", d({"amx_ddp": [{"make": "A"}, {"make": "B", "model_pattern": "X"}, 3]}), False),
    # Probes: the block, its port and its keys.
    ("tcp_probe_string", d({"tcp_probe": "5000"}), False),
    ("tcp_probe_none", d({"tcp_probe": None}), False),
    ("udp_probe_list", d({"udp_probe": [1]}), False),
    ("tcp_banner_ok", tcp(port=5000), False),
    ("tcp_port_missing", tcp(), False),
    ("tcp_port_zero", tcp(port=0), False),
    ("tcp_port_high", tcp(port=65536), False),
    ("tcp_port_max", tcp(port=65535), False),
    ("tcp_port_bool", tcp(port=True), False),
    ("tcp_port_string", tcp(port="5000"), False),
    ("tcp_unknown_keys", tcp(port=5000, sned="x", expect_text="y"), False),
    # Probes: what they send.
    ("tcp_send_ascii_ok", tcp(port=5000, send_ascii="Q\r", expect="A"), False),
    ("tcp_send_hex_ok_spaced", tcp(port=5000, send_hex="AB cd:01", expect="A"), False),
    ("tcp_send_both", tcp(port=5000, send_hex="AB", send_ascii="Q", expect="A"), False),
    ("tcp_send_both_bad", tcp(port=5000, send_hex=5, send_ascii=6, expect="A"), False),
    ("tcp_send_hex_number", tcp(port=5000, send_hex=12, expect="A"), False),
    ("tcp_send_hex_bad", tcp(port=5000, send_hex="zz", expect="A"), False),
    ("tcp_send_hex_odd", tcp(port=5000, send_hex="abc", expect="A"), False),
    ("tcp_send_ascii_number", tcp(port=5000, send_ascii=5, expect="A"), False),
    ("tcp_send_none_values", tcp(port=5000, send_hex=None, send_ascii=None), False),
    ("tcp_send_empty_ascii", tcp(port=5000, send_ascii="", expect="A"), False),
    ("tcp_send_no_matcher", tcp(port=5000, send_ascii="Q"), False),
    ("tcp_send_hex_no_matcher", tcp(port=5000, send_hex="00"), False),
    ("udp_no_send", udp(port=5000, expect="A"), False),
    ("udp_no_send_no_matcher", udp(port=5000), False),
    ("udp_ok", udp(port=5000, send_ascii="Q", expect="A"), False),
    # Probes: what they match.
    ("tcp_matchers_two", tcp(port=5000, expect="A", expect_regex="B"), False),
    ("tcp_matchers_three", tcp(port=5000, expect="A", expect_regex="(", expect_hex="zz"), False),
    ("tcp_matcher_none_values", tcp(port=5000, send_ascii="Q", expect=None, expect_hex=None), False),
    ("tcp_expect_empty", tcp(port=5000, expect=""), False),
    ("tcp_expect_number", tcp(port=5000, expect=5), False),
    ("tcp_expect_regex_ok", tcp(port=5000, expect_regex="^AB.*$"), False),
    ("tcp_expect_regex_empty", tcp(port=5000, expect_regex=""), False),
    ("tcp_expect_regex_number", tcp(port=5000, expect_regex=1), False),
    ("tcp_expect_regex_bad", tcp(port=5000, expect_regex="(unclosed"), False),
    ("tcp_expect_hex_ok", tcp(port=5000, expect_hex="0A:0b 0c"), False),
    ("tcp_expect_hex_number", tcp(port=5000, expect_hex=10), False),
    ("tcp_expect_hex_bad", tcp(port=5000, expect_hex="0g"), False),
    ("tcp_expect_hex_empty", tcp(port=5000, send_ascii="Q", expect_hex=""), False),
    ("udp_expect_bad_is_still_a_matcher", udp(port=5000, send_ascii="Q", expect=""), False),
    ("udp_matchers_two", udp(port=5000, send_hex="00", expect="A", expect_hex="00"), False),
    # Probes: timeout, cross_vendor, tls, reply_port.
    ("tcp_timeout_ok", tcp(port=5000, timeout_ms=10000), False),
    ("tcp_timeout_over", tcp(port=5000, timeout_ms=10001), False),
    ("tcp_timeout_zero", tcp(port=5000, timeout_ms=0), False),
    ("tcp_timeout_negative", tcp(port=5000, timeout_ms=-5), False),
    ("tcp_timeout_bool", tcp(port=5000, timeout_ms=True), False),
    ("tcp_timeout_float", tcp(port=5000, timeout_ms=100.0), False),
    ("tcp_timeout_none", tcp(port=5000, timeout_ms=None), False),
    ("tcp_cross_vendor_ok", tcp(port=5000, cross_vendor=True), False),
    ("tcp_cross_vendor_bad", tcp(port=5000, cross_vendor="true"), False),
    ("tcp_cross_vendor_none", tcp(port=5000, cross_vendor=None), False),
    ("tcp_tls_true", tcp(port=443, tls=True, expect="A"), False),
    ("tcp_tls_false", tcp(port=443, tls=False), False),
    ("tcp_tls_bad", tcp(port=443, tls="yes"), False),
    ("tcp_tls_none", tcp(port=443, tls=None), False),
    ("udp_tls_true", udp(port=5000, send_ascii="Q", expect="A", tls=True), False),
    ("udp_tls_false", udp(port=5000, send_ascii="Q", expect="A", tls=False), False),
    ("udp_tls_bad", udp(port=5000, send_ascii="Q", expect="A", tls=1), False),
    ("tcp_cert_subject_unchecked", tcp(port=443, cert_subject="(", expect="x"), False),
    ("tcp_reply_port", tcp(port=5000, reply_port=40002), False),
    ("tcp_reply_port_none", tcp(port=5000, reply_port=None), False),
    ("udp_reply_port_ok", udp(port=5000, send_ascii="Q", expect="A", reply_port=40002), False),
    ("udp_reply_port_zero", udp(port=5000, send_ascii="Q", expect="A", reply_port=0), False),
    ("udp_reply_port_high", udp(port=5000, send_ascii="Q", expect="A", reply_port=70000), False),
    ("udp_reply_port_bool", udp(port=5000, send_ascii="Q", expect="A", reply_port=False), False),
    ("udp_reply_port_string", udp(port=5000, send_ascii="Q", expect="A", reply_port="1"), False),
    # Probes: extract and extract_manufacturer.
    ("extract_none", extract(None), False),
    ("extract_list", extract(["model"]), False),
    ("extract_string", extract("model"), False),
    ("extract_literal", extract({"model": "W1"}), False),
    ("extract_regex_ok", extract({"model": {"regex": "m=(\\w+)", "group": 1}}), False),
    ("extract_regex_default_group", extract({"model": {"regex": "m=(\\w+)"}}), False),
    ("extract_regex_missing", extract({"model": {"group": 1}}), False),
    ("extract_regex_empty", extract({"model": {"regex": ""}}), False),
    ("extract_regex_number", extract({"model": {"regex": 5}}), False),
    ("extract_regex_bad", extract({"model": {"regex": "(", "group": 1}}), False),
    ("extract_group_negative", extract({"model": {"regex": "(a)", "group": -1}}), False),
    ("extract_group_zero", extract({"model": {"regex": "(a)", "group": 0}}), False),
    ("extract_group_bool", extract({"model": {"regex": "(a)", "group": True}}), False),
    ("extract_group_string", extract({"model": {"regex": "(a)", "group": "1"}}), False),
    ("extract_regex_bad_and_group_bad", extract({"model": {"regex": "[", "group": "x"}}), False),
    ("extract_spec_number", extract({"model": 5}), False),
    ("extract_spec_list", extract({"model": ["a"]}), False),
    ("extract_name_empty", extract({"": "x", "model": "W"}), False),
    ("extract_name_number", extract({5: "x", "serial": {"regex": "("}}), False),
    ("extract_many", extract({"model": "W", "serial": {"regex": "s=(\\d+)"}, "fw": 1}), False),
    ("extract_mfg_ok", tcp(port=5000, extract_manufacturer="Acme"), False),
    ("extract_mfg_empty", tcp(port=5000, extract_manufacturer=""), False),
    ("extract_mfg_number", tcp(port=5000, extract_manufacturer=5), False),
    ("extract_mfg_none", tcp(port=5000, extract_manufacturer=None), False),
    ("extract_mfg_collides", tcp(port=5000, extract_manufacturer="Acme",
                                 extract={"manufacturer": "Other"}), False),
    ("extract_mfg_with_other_extract", tcp(port=5000, extract_manufacturer="Acme",
                                           extract={"model": "W"}), False),
    ("extract_mfg_with_list_extract", tcp(port=5000, extract_manufacturer="Acme",
                                          extract=["manufacturer"]), False),
    ("extract_mfg_bad_and_collides", tcp(port=5000, extract_manufacturer="",
                                         extract={"manufacturer": "x"}), False),
    # A probe's `then:` step.
    ("then_silence_ok", then(send_hex="B0 63", expect_silence=True), False),
    ("then_match_ok", then(send_ascii="Q2", expect="B"), False),
    ("then_none", tcp(port=5000, send_ascii="Q", expect="A", then=None), False),
    ("then_on_udp", udp(port=5000, send_ascii="Q", expect="A", then={"send_ascii": "x"}), False),
    ("then_on_udp_not_mapping", udp(port=5000, send_ascii="Q", expect="A", then=5), False),
    ("then_string", tcp(port=5000, send_ascii="Q", expect="A", then="x"), False),
    ("then_empty", then(), False),
    ("then_unknown", then(send_ascii="x", expect="y", wait=1, port=2), False),
    ("then_both_sends", then(send_ascii="x", send_hex="00", expect="y"), False),
    ("then_send_none", then(send_ascii=None, expect="y"), False),
    ("then_silence_bad", then(send_ascii="x", expect_silence="yes"), False),
    ("then_silence_number_with_match", then(send_ascii="x", expect_silence=1, expect="y"), False),
    ("then_silence_zero_with_match", then(send_ascii="x", expect_silence=0, expect="y"), False),
    ("then_silence_and_match", then(send_ascii="x", expect_silence=True, expect_regex="y"), False),
    ("then_silence_false_no_match", then(send_ascii="x", expect_silence=False), False),
    ("then_silence_false_match", then(send_ascii="x", expect_silence=False, expect_hex="00"), False),
    ("then_silence_none_match", then(send_ascii="x", expect_silence=None, expect="y"), False),
    ("then_contents_unchecked", then(send_hex="zz", expect_regex="(", expect="b"), False),
    ("then_timeout_ok", then(send_ascii="x", expect="y", timeout_ms=10000), False),
    ("then_timeout_over", then(send_ascii="x", expect="y", timeout_ms=10001), False),
    ("then_timeout_zero", then(send_ascii="x", expect="y", timeout_ms=0), False),
    ("then_timeout_bool", then(send_ascii="x", expect="y", timeout_ms=False), False),
    ("then_timeout_true", then(send_ascii="x", expect="y", timeout_ms=True), False),
    ("then_timeout_string", then(send_ascii="x", expect="y", timeout_ms="5"), False),
    ("then_everything_wrong", then(bogus=1, expect_silence="x", timeout_ms=0), False),
    # A probe with every kind of problem at once (the order they are reported in).
    ("tcp_everything_wrong", tcp(
        port="x", bogus=1, send_hex="zz", send_ascii=5, expect="", expect_regex="(",
        expect_hex=4, timeout_ms=0, cross_vendor="x", tls="x", reply_port=9,
        extract={"model": {"regex": "("}}, extract_manufacturer="", then=4,
    ), False),
    ("udp_everything_wrong", udp(
        port=0, bogus=1, tls=True, reply_port="x", timeout_ms=99999,
        cross_vendor=0, then={"send_ascii": "x"}, extract=[1],
    ), False),
    # python:
    ("python_string", d({"python": "./acme_discovery.py"}), False),
    ("python_string_empty", d({"python": ""}), False),
    ("python_mapping_ok", d({"python": {"file": "./acme_discovery.py", "cross_vendor": True}}), False),
    ("python_mapping_unknown", d({"python": {"file": "x.py", "module": "y"}}), False),
    ("python_mapping_no_file", d({"python": {"cross_vendor": False}}), False),
    ("python_mapping_file_empty", d({"python": {"file": ""}}), False),
    ("python_mapping_file_number", d({"python": {"file": 3}}), False),
    ("python_mapping_cross_vendor_bad", d({"python": {"file": "x.py", "cross_vendor": "y"}}), False),
    ("python_number", d({"python": 5}), False),
    ("python_none", d({"python": None}), False),
    ("python_list", d({"python": ["x.py"]}), False),
    ("python_string_missing_companion", d({"python": "./missing_discovery.py"}), True),
    ("python_string_present_companion", d({"python": "./present_discovery.py"}), True),
    ("python_mapping_missing_companion", d({"python": {"file": "missing.py"}}), True),
    ("python_mapping_present_companion", d({"python": {"file": "present_discovery.py"}}), True),
    ("python_empty_with_dir", d({"python": ""}), True),
    ("python_mapping_bad_with_dir", d({"python": {"file": 4, "x": 1}}), True),
    ("python_companion_is_a_directory", d({"python": "."}), True),
    # Hints: snmp_pen, oui, hostname, port_open, manufacturer_alias.
    ("snmp_ok", d({"snmp_pen": 1234}), False),
    ("snmp_zero", d({"snmp_pen": 0}), False),
    ("snmp_bool", d({"snmp_pen": True}), False),
    ("snmp_string", d({"snmp_pen": "1234"}), False),
    ("snmp_none", d({"snmp_pen": None}), False),
    ("oui_ok", d({"oui": ["00:11:22", "AABBCC"]}), False),
    ("oui_string", d({"oui": "00:11:22"}), False),
    ("oui_entries_bad", d({"oui": ["", 5, None, "00:11:22"]}), False),
    ("oui_empty_list", d({"oui": []}), False),
    ("oui_none", d({"oui": None}), False),
    ("hostname_ok", d({"hostname": ["^acme-", "widget\\d+"]}), False),
    ("hostname_string", d({"hostname": "^acme-"}), False),
    ("hostname_entries_bad", d({"hostname": ["", 5, "(", "^ok"]}), False),
    ("hostname_mapping", d({"hostname": {"a": 1}}), False),
    ("port_open_ok", d({"port_open": [5000, 1, 65535]}), False),
    ("port_open_int", d({"port_open": 5000}), False),
    ("port_open_entries_bad", d({"port_open": ["5000", True, 0, 65536, 80, 8888, 5001]}), False),
    ("port_open_each_disallowed", d({"port_open": [22, 80, 443, 8000, 8080, 8443]}), False),
    ("alias_ok", d({"manufacturer_alias": ["Acme Corp", "ACME"]}), False),
    ("alias_string", d({"manufacturer_alias": "Acme"}), False),
    ("alias_entries_bad", d({"manufacturer_alias": [5, " ", "", "ok"]}), False),
    # The signal-less warning, and each signal that silences it on its own.
    ("signal_only_mdns", d({"mdns": "_a._tcp"}), False),
    ("signal_only_ssdp", d({"ssdp": "urn:a"}), False),
    ("signal_only_amx", d({"amx_ddp": {"make": "A"}}), False),
    ("signal_only_tcp_probe", tcp(port=5000), False),
    ("signal_only_bad_tcp_probe", d({"tcp_probe": 5}), False),
    ("signal_only_udp_probe", udp(port=5000, send_ascii="Q", expect="A"), False),
    ("signal_only_python", d({"python": "x.py"}), False),
    ("signal_only_bad_python", d({"python": 5}), False),
    ("signal_only_snmp", d({"snmp_pen": 99}), False),
    ("signal_only_bad_snmp", d({"snmp_pen": "x"}), False),
    ("signal_only_oui", d({"oui": ["00:11:22"]}), False),
    ("signal_only_hostname", d({"hostname": ["^a"]}), False),
    ("signal_only_bad_hostname", d({"hostname": [5]}), False),
    ("signal_only_port_open", d({"port_open": [5000]}), False),
    ("signal_only_bad_port_open", d({"port_open": ["x"]}), False),
    ("signal_only_alias", d({"manufacturer_alias": ["Acme"]}), False),
    ("signal_bad_mdns_entry_only", d({"mdns": [5]}), False),
    ("signal_bad_ssdp_entry_only", d({"ssdp": [{"model": "x"}]}), False),
    ("signal_bad_amx_entry_only", d({"amx_ddp": ["x"]}), False),
    ("signal_bad_lists_only", d({"oui": "x", "hostname": "y", "port_open": 1,
                                  "manufacturer_alias": "z"}), False),
    # A full, valid block in the order the corpus writes it.
    ("full_valid", d({
        "requires": "0.37.2",
        "mdns": [{"service": "_acme._tcp", "txt": {"model": "W"}}],
        "ssdp": [{"device_type": "urn:acme", "model": "W"}],
        "amx_ddp": [{"make": "Acme", "model_pattern": "W*"}],
        "tcp_probe": {"port": 5000, "send_ascii": "Q", "expect": "A",
                      "then": {"send_ascii": "Q2", "expect_silence": True}},
        "udp_probe": {"port": 5001, "send_hex": "00", "expect_hex": "01",
                      "reply_port": 5002},
        "python": "./acme_discovery.py",
        "snmp_pen": 1234, "oui": ["00:11:22"], "hostname": ["^acme"],
        "port_open": [5000], "manufacturer_alias": ["Acme Corp"],
    }), False),
]


# --- Several drivers: the cross-driver claims --------------------------------
#
# (name, [driver dicts]) -- each driver's block is checked first, as the
# catalog build does, and the claims run over what that produced.

def mdns(service: Any, txt: Any = None, *, driver_id: str) -> dict:
    entry: Any = service if txt is None else {"service": service, "txt": txt}
    return d({"mdns": entry}, driver_id=driver_id)


def ssdp(device_type: str, *, driver_id: str, **fields: Any) -> dict:
    entry: Any = {"device_type": device_type, **fields} if fields else device_type
    return d({"ssdp": entry}, driver_id=driver_id)


def host(*patterns: str, driver_id: str, **more: Any) -> dict:
    return d({"hostname": list(patterns), **more}, driver_id=driver_id)


CATALOG_CASES: list[tuple[str, list[dict]]] = [
    ("mdns_same_service", [mdns("_a._tcp", driver_id="acme_a"), mdns("_a._tcp", driver_id="acme_b")]),
    ("mdns_service_normalised", [mdns("_A._TCP", driver_id="acme_a"), mdns("_a._tcp.", driver_id="acme_b")]),
    ("mdns_same_driver_twice", [d({"mdns": ["_a._tcp", "_a._tcp"]}, driver_id="acme_a")]),
    ("mdns_same_id_two_files", [mdns("_a._tcp", driver_id="acme_a"), mdns("_a._tcp", driver_id="acme_a")]),
    ("mdns_same_id_reclaims_after_another", [
        mdns("_a._tcp", {"model": "W"}, driver_id="acme_a"),
        mdns("_a._tcp", {"model": "X"}, driver_id="acme_b"),
        mdns("_a._tcp", {"model": "X"}, driver_id="acme_a"),
    ]),
    ("mdns_unfiltered_then_filtered", [
        mdns("_a._tcp", driver_id="acme_a"), mdns("_a._tcp", {"model": "W"}, driver_id="acme_b"),
    ]),
    ("mdns_filtered_then_unfiltered", [
        mdns("_a._tcp", {"model": "W"}, driver_id="acme_a"), mdns("_a._tcp", driver_id="acme_b"),
    ]),
    ("mdns_different_filters", [
        mdns("_a._tcp", {"model": "W"}, driver_id="acme_a"),
        mdns("_a._tcp", {"model": "X"}, driver_id="acme_b"),
    ]),
    ("mdns_same_filter_key_case", [
        mdns("_a._tcp", {"Model": "W"}, driver_id="acme_a"),
        mdns("_a._tcp", {"model": "W"}, driver_id="acme_b"),
    ]),
    ("mdns_same_filter_value_case", [
        mdns("_a._tcp", {"model": "W"}, driver_id="acme_a"),
        mdns("_a._tcp", {"model": "w"}, driver_id="acme_b"),
    ]),
    ("mdns_three_way", [
        mdns("_a._tcp", driver_id="acme_a"),
        mdns("_a._tcp", {"model": "W"}, driver_id="acme_b"),
        mdns("_a._tcp", {"model": "W"}, driver_id="acme_c"),
        mdns("_a._tcp", driver_id="acme_d"),
    ]),
    ("mdns_three_way_filtered_first", [
        mdns("_a._tcp", {"model": "W"}, driver_id="acme_a"),
        mdns("_a._tcp", {"model": "X"}, driver_id="acme_b"),
        mdns("_a._tcp", driver_id="acme_c"),
        mdns("_a._tcp", {"model": "X"}, driver_id="acme_d"),
    ]),
    ("ssdp_same_type", [ssdp("urn:a", driver_id="acme_a"), ssdp("urn:a", driver_id="acme_b")]),
    ("ssdp_type_case_matters", [ssdp("urn:A", driver_id="acme_a"), ssdp("urn:a", driver_id="acme_b")]),
    ("ssdp_filtered_apart", [
        ssdp("urn:a", driver_id="acme_a", model="W1"), ssdp("urn:a", driver_id="acme_b", model="W2"),
    ]),
    ("ssdp_unfiltered_shadows", [
        ssdp("urn:a", driver_id="acme_a"), ssdp("urn:a", driver_id="acme_b", manufacturer="Acme"),
    ]),
    ("ssdp_filtered_then_unfiltered", [
        ssdp("urn:a", driver_id="acme_a", friendly_name="W"), ssdp("urn:a", driver_id="acme_b"),
    ]),
    ("ssdp_same_filters", [
        ssdp("urn:a", driver_id="acme_a", model="W", manufacturer="A"),
        ssdp("urn:a", driver_id="acme_b", manufacturer="A", model="W"),
    ]),
    ("amx_same", [
        d({"amx_ddp": {"make": "Acme"}}, driver_id="acme_a"),
        d({"amx_ddp": {"make": "Acme", "model_pattern": "*"}}, driver_id="acme_b"),
    ]),
    ("amx_patterns_apart", [
        d({"amx_ddp": {"make": "Acme", "model_pattern": "W*"}}, driver_id="acme_a"),
        d({"amx_ddp": {"make": "Acme", "model_pattern": "X*"}}, driver_id="acme_b"),
    ]),
    ("probes_and_python_never_collide", [
        d({"tcp_probe": {"port": 1}, "udp_probe": {"port": 2, "send_ascii": "q", "expect": "a"},
           "python": "x.py"}, driver_id="acme_a"),
        d({"tcp_probe": {"port": 1}, "udp_probe": {"port": 2, "send_ascii": "q", "expect": "a"},
           "python": "x.py"}, driver_id="acme_b"),
    ]),
    ("probes_same_id", [
        d({"tcp_probe": {"port": 1}}, driver_id="acme_a"),
        d({"tcp_probe": {"port": 1}, "python": "y.py"}, driver_id="acme_a"),
    ]),
    ("mixed_kinds_same_source", [
        d({"mdns": "urn:a", "ssdp": "urn:a"}, driver_id="acme_a"),
        d({"ssdp": "urn:a"}, driver_id="acme_b"),
    ]),
    ("generic_and_bad_blocks_skipped", [
        mdns("_a._tcp", driver_id="generic_tcp"),
        d([1], driver_id="acme_x"),
        mdns("_a._tcp", driver_id="acme_a"),
    ]),
    ("missing_ids", [
        d({"mdns": "_a._tcp"}, driver_id=None),
        d({"mdns": "_a._tcp"}, driver_id=""),
        d({"mdns": "_a._tcp"}, driver_id="acme_a"),
    ]),
    # Locator ambiguity: a hostname shared by drivers with no declarative fingerprint.
    ("host_shared_no_fingerprints", [host("^acme-", driver_id="acme_b"), host("^acme-", driver_id="acme_a")]),
    ("host_shared_one_probe", [
        host("^acme-", driver_id="acme_a", tcp_probe={"port": 1}), host("^acme-", driver_id="acme_b"),
    ]),
    ("host_shared_case_insensitive", [host("^ACME-", driver_id="acme_a"), host("^acme-", driver_id="acme_b")]),
    ("host_single_driver_twice", [host("^acme-", "^acme-", driver_id="acme_a")]),
    ("host_same_id_two_files", [host("^acme-", driver_id="acme_a"), host("^acme-", driver_id="acme_a")]),
    ("host_grandfathered", [host("^birddog-", driver_id="birddog_ptz"), host("^birddog-", driver_id="birddog_codec")]),
    ("host_grandfathered_and_new", [
        host("^birddog-", driver_id="birddog_ptz"), host("^birddog-", driver_id="birddog_codec"),
        host("^birddog-", driver_id="birddog_new"),
    ]),
    ("host_each_fingerprint_counts", [
        host("^a-", driver_id="acme_tcp", tcp_probe={"port": 1}),
        host("^a-", driver_id="acme_udp", udp_probe={"port": 1, "send_ascii": "q", "expect": "a"}),
        host("^a-", driver_id="acme_mdns", mdns="_a._tcp"),
        host("^a-", driver_id="acme_ssdp", ssdp="urn:a"),
        host("^a-", driver_id="acme_amx", amx_ddp={"make": "A"}),
        host("^a-", driver_id="acme_python", python="x.py"),
        host("^a-", driver_id="acme_soft", oui=["00:11:22"], port_open=[5000]),
    ]),
    ("host_bad_fingerprints_do_not_count", [
        host("^a-", driver_id="acme_one", mdns=[5], ssdp=[{"model": "x"}], amx_ddp=["x"]),
        host("^a-", driver_id="acme_two"),
    ]),
    ("host_bad_probe_still_counts", [
        host("^a-", driver_id="acme_one", tcp_probe="x"), host("^a-", driver_id="acme_two"),
    ]),
    ("host_several_patterns_sorted", [
        host("^zeta", "^alpha", driver_id="acme_z"), host("^alpha", "^zeta", driver_id="acme_a"),
        host("^zeta", driver_id="acme_m"),
    ]),
    ("host_bad_entries_skipped", [host("", "^x", driver_id="acme_a"), host("", driver_id="acme_b")]),
    ("host_and_mdns_collide_together", [
        host("^a-", driver_id="acme_a", mdns="_a._tcp"), host("^a-", driver_id="acme_b", mdns="_a._tcp"),
        host("^a-", driver_id="acme_c"),
    ]),
]


# --- The `requires` stamp ----------------------------------------------------

STAMP_CASES: list[tuple[str, Any]] = [
    ("plain", {"mdns": "_a._tcp"}),
    ("empty", {}),
    ("ssdp_filter", {"ssdp": [{"device_type": "urn:a", "model": "W"}]}),
    ("ssdp_filter_none", {"ssdp": [{"device_type": "urn:a", "model": None}]}),
    ("ssdp_filter_empty_string", {"ssdp": [{"device_type": "urn:a", "model": ""}]}),
    ("ssdp_filter_false", {"ssdp": [{"device_type": "urn:a", "friendly_name": False}]}),
    ("ssdp_string", {"ssdp": "urn:a"}),
    ("ssdp_single_mapping", {"ssdp": {"device_type": "urn:a", "friendly_name": "W"}}),
    ("ssdp_mixed_list", {"ssdp": ["urn:a", {"device_type": "urn:b"}, {"device_type": "urn:c", "manufacturer": "A"}]}),
    ("follow_up", {"tcp_probe": {"port": 1, "then": {"send_ascii": "x"}}}),
    ("follow_up_none", {"tcp_probe": {"port": 1, "then": None}}),
    ("follow_up_on_udp", {"udp_probe": {"port": 1, "then": {"send_ascii": "x"}}}),
    ("tcp_probe_not_mapping", {"tcp_probe": "x"}),
    ("reply_port", {"udp_probe": {"port": 1, "reply_port": 2}}),
    ("reply_port_none", {"udp_probe": {"port": 1, "reply_port": None}}),
    ("reply_port_on_tcp", {"tcp_probe": {"port": 1, "reply_port": 2}}),
    ("udp_probe_not_mapping", {"udp_probe": [1]}),
    ("all_three", {
        "ssdp": [{"device_type": "urn:a", "model": "W"}],
        "tcp_probe": {"port": 1, "then": {"send_ascii": "x"}},
        "udp_probe": {"port": 1, "reply_port": 2},
    }),
    ("filter_and_follow_up", {"tcp_probe": {"port": 1, "then": {}}, "ssdp": {"device_type": "u", "model": "m"}}),
    ("hand_newer_kept", {"requires": "0.40.0", "tcp_probe": {"port": 1, "then": {}}}),
    ("hand_equal_kept", {"requires": "0.32.0", "tcp_probe": {"port": 1, "then": {}}}),
    ("hand_older_raised", {"mdns": "_a._tcp", "requires": "0.1.0", "udp_probe": {"reply_port": 2}}),
    ("hand_unparsable_replaced", {"requires": "latest", "tcp_probe": {"then": {}}}),
    ("hand_suffix_compared", {"requires": "0.32.0-rc1", "tcp_probe": {"then": {}}}),
    ("hand_newer_suffix", {"requires": "0.33.0+x", "tcp_probe": {"then": {}}}),
    ("hand_only", {"requires": "0.1.0"}),
    ("hand_number", {"requires": 5}),
    ("hand_none_plain", {"requires": None, "mdns": "x"}),
    ("hand_none_gated", {"requires": None, "tcp_probe": {"then": {}}}),
]


# --- Running a case ----------------------------------------------------------


def _norm_text(text: str, root: Path | None) -> str:
    if root is None:
        return text
    # The longer form first: on macOS the resolved path is the plain one with
    # /private in front of it.
    for form in sorted({str(root), str(root.resolve())}, key=len, reverse=True):
        text = text.replace(form, "<dir>")
    return text


def _run_block(raw: dict, with_dir: bool) -> dict:
    stderr = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) if with_dir else None
        if root is not None:
            (root / "present_discovery.py").write_text("# companion\n", encoding="utf-8")
        with contextlib.redirect_stderr(stderr):
            errors, normalized = discovery_block_errors(FILE, raw, yaml_dir=root)
        return {
            "errors": [_norm_text(e, root) for e in errors],
            "normalized": repr(normalized),
            "stderr": stderr.getvalue(),
        }


def _run_catalog(drivers: list[dict]) -> dict:
    stderr = io.StringIO()
    errors: list[str] = []
    per_driver: list[tuple[str, str, dict]] = []
    with contextlib.redirect_stderr(stderr):
        for i, raw in enumerate(drivers):
            rel = f"audio/driver_{i}.avcdriver"
            block_errors, normalized = discovery_block_errors(rel, raw, yaml_dir=None)
            errors.extend(block_errors)
            if normalized:
                per_driver.append((str(raw.get("id") or ""), rel, normalized))
        collisions = collision_errors(per_driver)
        ambiguity = locator_errors(per_driver)
    return {
        "errors": errors,
        "collisions": collisions,
        "locator": ambiguity,
        "stderr": stderr.getvalue(),
    }


def _run_stamp(discovery: Any) -> dict:
    before = repr(discovery)
    result = with_discovery_requires(discovery)
    return {
        "result": repr(result),
        "same_object": result is discovery,
        "input_unchanged": repr(discovery) == before,
    }


def _observe() -> dict:
    return {
        "block": {name: _run_block(raw, with_dir) for name, raw, with_dir in BLOCK_CASES},
        "catalog": {name: _run_catalog(drivers) for name, drivers in CATALOG_CASES},
        "stamp": {name: _run_stamp(disc) for name, disc in STAMP_CASES},
    }


def _expected() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_case_names_are_unique() -> None:
    for cases in (BLOCK_CASES, CATALOG_CASES, STAMP_CASES):
        names = [case[0] for case in cases]
        assert len(names) == len(set(names))


def test_fixture_covers_exactly_the_cases() -> None:
    expected = _expected()
    assert sorted(expected["block"]) == sorted(n for n, _, _ in BLOCK_CASES)
    assert sorted(expected["catalog"]) == sorted(n for n, _ in CATALOG_CASES)
    assert sorted(expected["stamp"]) == sorted(n for n, _ in STAMP_CASES)


@pytest.mark.parametrize(("name", "raw", "with_dir"), BLOCK_CASES, ids=[c[0] for c in BLOCK_CASES])
def test_block(name: str, raw: dict, with_dir: bool) -> None:
    assert _run_block(raw, with_dir) == _expected()["block"][name]


@pytest.mark.parametrize(("name", "drivers"), CATALOG_CASES, ids=[c[0] for c in CATALOG_CASES])
def test_catalog(name: str, drivers: list[dict]) -> None:
    assert _run_catalog(drivers) == _expected()["catalog"][name]


@pytest.mark.parametrize(("name", "discovery"), STAMP_CASES, ids=[c[0] for c in STAMP_CASES])
def test_stamp(name: str, discovery: Any) -> None:
    assert _run_stamp(discovery) == _expected()["stamp"][name]


if __name__ == "__main__":
    if sys.argv[1:] != ["--regen"]:
        sys.exit("usage: python tests/test_build_index_discovery_pins.py --regen")
    FIXTURE.write_text(json.dumps(_observe(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE}")
