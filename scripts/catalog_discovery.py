"""The catalog's checks of a driver's ``discovery:`` block.

``validate_discovery_block`` checks one driver's block the way the platform's
discovery parser reads it (``parse_driver_discovery`` in
``openavc/discovery/hints.py``), but collects every problem where the parser
stops at the first, and also requires a declared ``python:`` companion to ship
beside the driver. It returns the block's normalised form for the
cross-driver checks: ``validate_no_signal_collisions`` (no two drivers claim
the same fingerprint) and ``validate_locator_disambiguation`` (drivers sharing
a hostname pattern each carry a fingerprint that runs before install).
``with_discovery_requires`` is the ``requires:`` stamp a catalog entry is
written with.

The platform's parser is the authority and this is a hand copy of it. A new
discovery key lands there first; here it needs its place in the known keys,
its check, and a gate in ``_DISCOVERY_FEATURE_GATES`` when an older parser
would misread it rather than refuse it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any


# --- Discovery block validation ---------------------------------------------
#
# Mirrors ``parse_driver_discovery`` in the platform's hints.py.

# Ports too generic to use as a hint — every web / admin / SSH device on
# the network would match. AV-specific ports are fine. Mirrors
# ``DISALLOWED_OPEN_PORTS`` in the platform. 8000 / 8080 / 8443 / 8888
# are admin-UI alternates with the same false-positive class as 80/443.
_DISALLOWED_OPEN_PORTS = frozenset({22, 80, 443, 8000, 8080, 8443, 8888})

_MAX_PROBE_TIMEOUT_MS = 10000

_KNOWN_DISCOVERY_KEYS: frozenset[str] = frozenset({
    "requires",
    "mdns", "ssdp", "amx_ddp",
    "tcp_probe", "udp_probe", "python",
    "oui", "hostname", "port_open", "manufacturer_alias", "snmp_pen",
})

_KNOWN_PROBE_KEYS: frozenset[str] = frozenset({
    "port", "send_hex", "send_ascii",
    "expect", "expect_regex", "expect_hex",
    "cross_vendor", "timeout_ms", "tls", "cert_subject",
    "extract", "extract_manufacturer", "then", "reply_port",
})

# Keys inside a probe's `then:` follow-up step.
_KNOWN_FOLLOW_UP_KEYS: frozenset[str] = frozenset({
    "send_hex", "send_ascii", "expect_silence",
    "expect", "expect_regex", "expect_hex", "timeout_ms",
})


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]

def _validate_extract_block(file: str, where: str, raw: Any) -> list[str]:
    """Return validation errors for an ``extract:`` block."""
    errors: list[str] = []
    if raw is None:
        return errors
    if not isinstance(raw, dict):
        errors.append(
            f"{file}: discovery.{where}.extract must be a mapping of field "
            "name to literal string or {regex, group} mapping"
        )
        return errors
    for name, spec in raw.items():
        if not isinstance(name, str) or not name:
            errors.append(
                f"{file}: discovery.{where}.extract field names must be "
                "non-empty strings"
            )
            continue
        if isinstance(spec, str):
            continue
        if isinstance(spec, dict):
            pat = spec.get("regex")
            if not isinstance(pat, str) or not pat:
                errors.append(
                    f"{file}: discovery.{where}.extract.{name} mapping requires "
                    "a non-empty 'regex' string"
                )
            else:
                try:
                    re.compile(pat)
                except re.error as exc:
                    errors.append(
                        f"{file}: discovery.{where}.extract.{name}.regex failed "
                        f"to compile: {exc}"
                    )
            grp = spec.get("group", 1)
            if not isinstance(grp, int) or isinstance(grp, bool) or grp < 0:
                errors.append(
                    f"{file}: discovery.{where}.extract.{name}.group must be a "
                    "non-negative integer"
                )
            continue
        errors.append(
            f"{file}: discovery.{where}.extract.{name} must be a literal "
            "string or a {regex, group} mapping"
        )
    return errors


def _validate_probe_block(file: str, kind: str, raw: Any) -> list[str]:
    """Return validation errors for a ``tcp_probe:`` / ``udp_probe:`` block."""
    where = f"{kind}_probe"
    errors: list[str] = []
    if not isinstance(raw, dict):
        errors.append(f"{file}: discovery.{where} must be a mapping")
        return errors

    unknown = set(raw.keys()) - _KNOWN_PROBE_KEYS
    if unknown:
        errors.append(
            f"{file}: discovery.{where} has unknown keys: {sorted(unknown)}"
        )

    port = raw.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        errors.append(
            f"{file}: discovery.{where}.port must be an integer in [1, 65535]"
        )

    has_send_hex = "send_hex" in raw and raw["send_hex"] is not None
    has_send_ascii = "send_ascii" in raw and raw["send_ascii"] is not None
    if has_send_hex and has_send_ascii:
        errors.append(
            f"{file}: discovery.{where} declares both send_hex and "
            "send_ascii — pick one"
        )
    if has_send_hex:
        if not isinstance(raw["send_hex"], str):
            errors.append(f"{file}: discovery.{where}.send_hex must be a string")
        else:
            try:
                bytes.fromhex(raw["send_hex"].replace(" ", "").replace(":", ""))
            except ValueError as exc:
                errors.append(
                    f"{file}: discovery.{where}.send_hex is not valid hex: {exc}"
                )
    if has_send_ascii and not isinstance(raw["send_ascii"], str):
        errors.append(f"{file}: discovery.{where}.send_ascii must be a string")

    has_send = has_send_hex or has_send_ascii
    if kind == "udp" and not has_send:
        errors.append(
            f"{file}: discovery.{where} must declare send_ascii or send_hex "
            "(UDP probes need a query payload)"
        )

    declared_matchers = [
        k for k in ("expect", "expect_regex", "expect_hex")
        if k in raw and raw[k] is not None
    ]
    has_expect = bool(declared_matchers)
    if len(declared_matchers) > 1:
        errors.append(
            f"{file}: discovery.{where} declares multiple matchers "
            f"({', '.join(declared_matchers)}) — pick exactly one of "
            "expect, expect_regex, or expect_hex"
        )

    if "expect" in raw and raw["expect"] is not None:
        c = raw["expect"]
        if not isinstance(c, str) or not c:
            errors.append(
                f"{file}: discovery.{where}.expect must be a non-empty string"
            )
    if "expect_regex" in raw and raw["expect_regex"] is not None:
        r = raw["expect_regex"]
        if not isinstance(r, str) or not r:
            errors.append(
                f"{file}: discovery.{where}.expect_regex must be a non-empty string"
            )
        else:
            try:
                re.compile(r)
            except re.error as exc:
                errors.append(
                    f"{file}: discovery.{where}.expect_regex failed to compile: {exc}"
                )
    if "expect_hex" in raw and raw["expect_hex"] is not None:
        s = raw["expect_hex"]
        if not isinstance(s, str):
            errors.append(f"{file}: discovery.{where}.expect_hex must be a string")
        else:
            try:
                bytes.fromhex(s.replace(" ", "").replace(":", ""))
            except ValueError as exc:
                errors.append(
                    f"{file}: discovery.{where}.expect_hex is not valid hex: {exc}"
                )

    if kind == "udp" and not has_expect:
        errors.append(
            f"{file}: discovery.{where} needs exactly one of "
            "expect, expect_regex, expect_hex"
        )
    if kind == "tcp" and has_send and not has_expect:
        errors.append(
            f"{file}: discovery.{where} sends bytes but declares no matcher "
            "— add exactly one of expect, expect_regex, or expect_hex"
        )

    timeout_ms = raw.get("timeout_ms")
    if timeout_ms is not None:
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 1:
            errors.append(
                f"{file}: discovery.{where}.timeout_ms must be a positive integer"
            )
        elif timeout_ms > _MAX_PROBE_TIMEOUT_MS:
            errors.append(
                f"{file}: discovery.{where}.timeout_ms exceeds the max of "
                f"{_MAX_PROBE_TIMEOUT_MS} ms"
            )

    if "cross_vendor" in raw and not isinstance(raw["cross_vendor"], bool):
        errors.append(f"{file}: discovery.{where}.cross_vendor must be a bool")

    if "tls" in raw:
        if not isinstance(raw["tls"], bool):
            errors.append(f"{file}: discovery.{where}.tls must be a bool")
        elif raw["tls"] and kind != "tcp":
            errors.append(
                f"{file}: discovery.{where}.tls is only valid on a tcp_probe"
            )

    if raw.get("reply_port") is not None:
        reply_port = raw["reply_port"]
        if kind != "udp":
            errors.append(
                f"{file}: discovery.{where}.reply_port is only valid on a "
                "udp_probe (a tcp_probe's answer comes back on its own connection)"
            )
        elif (
            not isinstance(reply_port, int) or isinstance(reply_port, bool)
            or reply_port < 1 or reply_port > 65535
        ):
            errors.append(
                f"{file}: discovery.{where}.reply_port must be an integer in "
                "[1, 65535]"
            )

    errors.extend(_validate_extract_block(file, where, raw.get("extract")))

    if "extract_manufacturer" in raw and raw["extract_manufacturer"] is not None:
        mfg = raw["extract_manufacturer"]
        if not isinstance(mfg, str) or not mfg:
            errors.append(
                f"{file}: discovery.{where}.extract_manufacturer must be a "
                "non-empty string"
            )
        elif (
            isinstance(raw.get("extract"), dict)
            and "manufacturer" in raw["extract"]
        ):
            errors.append(
                f"{file}: discovery.{where}.extract_manufacturer collides with "
                "extract.manufacturer — pick one"
            )

    errors.extend(_validate_follow_up_block(file, where, kind, raw.get("then")))

    return errors


def _validate_follow_up_block(
    file: Any, where: str, kind: str, raw: Any,
) -> list[str]:
    """Return validation errors for a probe's ``then:`` follow-up step.

    Mirrors openavc/discovery/hints.py ``_parse_follow_up``. The runtime is the
    authority; this copy exists so the catalog refuses a block the platform
    would reject at load rather than shipping a driver that silently never
    matches.
    """
    if raw is None:
        return []
    at = f"discovery.{where}.then"
    if kind != "tcp":
        return [f"{file}: {at} is only valid on a tcp_probe"]
    if not isinstance(raw, dict):
        return [f"{file}: {at} must be a mapping"]

    errors: list[str] = []
    unknown = set(raw.keys()) - _KNOWN_FOLLOW_UP_KEYS
    if unknown:
        errors.append(f"{file}: {at} has unknown keys: {sorted(unknown)}")

    has_send = any(raw.get(k) is not None for k in ("send_hex", "send_ascii"))
    if not has_send:
        errors.append(
            f"{file}: {at} must declare send_ascii or send_hex "
            "(a follow-up step has to ask something)"
        )
    if raw.get("send_hex") is not None and raw.get("send_ascii") is not None:
        errors.append(f"{file}: {at} declares both send_hex and send_ascii — pick one")

    silence = raw.get("expect_silence")
    if silence is not None and not isinstance(silence, bool):
        errors.append(f"{file}: {at}.expect_silence must be a bool")
    has_match = any(
        raw.get(k) is not None for k in ("expect", "expect_regex", "expect_hex")
    )
    if silence and has_match:
        errors.append(
            f"{file}: {at} declares both expect_silence and a matcher — a step "
            "that expects no answer cannot also match one"
        )
    if not silence and not has_match:
        errors.append(
            f"{file}: {at} declares no expectation — add expect_silence: true, "
            "or expect / expect_regex / expect_hex"
        )

    timeout = raw.get("timeout_ms")
    if timeout is not None:
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
            errors.append(f"{file}: {at}.timeout_ms must be a positive integer")
        elif timeout > _MAX_PROBE_TIMEOUT_MS:
            errors.append(
                f"{file}: {at}.timeout_ms must not exceed {_MAX_PROBE_TIMEOUT_MS} ms"
            )

    return errors


def _validate_python_block(
    file: str, raw: Any, *, yaml_dir: Path | None = None,
) -> list[str]:
    """Return validation errors for the ``python:`` field.

    When ``yaml_dir`` is provided, also verifies the declared companion
    file exists alongside the YAML — required so a community PR can't
    ship a YAML with ``python: ./missing.py`` past CI. Without the
    sibling file the engine would auto-register two ``SignalRule``
    records under ``custom_<id>_companion_(udp|tcp)`` that never get
    any evidence to match.
    """
    errors: list[str] = []
    file_path: str | None = None
    if isinstance(raw, str):
        if not raw:
            errors.append(
                f"{file}: discovery.python path must be a non-empty string"
            )
        else:
            file_path = raw
    elif not isinstance(raw, dict):
        errors.append(
            f"{file}: discovery.python must be a string path or "
            "{file, cross_vendor} mapping"
        )
        return errors
    else:
        unknown = set(raw.keys()) - {"file", "cross_vendor"}
        if unknown:
            errors.append(
                f"{file}: discovery.python has unknown keys: {sorted(unknown)}"
            )
        candidate = raw.get("file")
        if not isinstance(candidate, str) or not candidate:
            errors.append(
                f"{file}: discovery.python.file must be a non-empty string"
            )
        else:
            file_path = candidate
        if "cross_vendor" in raw and not isinstance(raw["cross_vendor"], bool):
            errors.append(f"{file}: discovery.python.cross_vendor must be a bool")

    # Companion existence check — only when the parser has somewhere to
    # look (yaml_dir set). Skipped for purely-structural validation (the
    # platform's parser path runs without a yaml_dir).
    if file_path and yaml_dir is not None:
        companion_path = (yaml_dir / file_path).resolve()
        if not companion_path.is_file():
            errors.append(
                f"{file}: discovery.python.file={file_path!r} but no "
                f"such file exists at {companion_path} — drivers must "
                "ship the sibling _discovery.py companion alongside "
                "the .avcdriver"
            )
    return errors


def _validate_mdns_entry(file: str, raw: Any) -> tuple[list[str], dict[str, Any] | None]:
    """Validate one mDNS fingerprint entry. Returns (errors, normalized)."""
    if isinstance(raw, str):
        if not raw:
            return ([f"{file}: discovery.mdns service must be a non-empty string"], None)
        return ([], {"service": raw, "txt": {}})
    if not isinstance(raw, dict):
        return (
            [f"{file}: discovery.mdns entries must be strings or "
             "{service, txt, cross_vendor} mappings"],
            None,
        )
    errors: list[str] = []
    unknown = set(raw.keys()) - {"service", "txt", "cross_vendor"}
    if unknown:
        errors.append(
            f"{file}: discovery.mdns entry has unknown keys: {sorted(unknown)}"
        )
    service = raw.get("service")
    if not isinstance(service, str) or not service:
        errors.append(f"{file}: discovery.mdns.service must be a non-empty string")
        return (errors, None)
    txt_raw = raw.get("txt") or {}
    if not isinstance(txt_raw, dict):
        errors.append(f"{file}: discovery.mdns.txt must be a mapping")
        txt_raw = {}
    if "cross_vendor" in raw and not isinstance(raw["cross_vendor"], bool):
        errors.append(f"{file}: discovery.mdns.cross_vendor must be a bool")
    return (
        errors,
        {"service": service, "txt": {str(k): str(v) for k, v in txt_raw.items()}},
    )


_SSDP_FILTER_KEYS = ("model", "manufacturer", "friendly_name")


def _validate_ssdp_entry(file: str, raw: Any) -> tuple[list[str], dict[str, Any] | None]:
    """Validate one SSDP fingerprint entry.

    Returns (errors, normalized {device_type, fields}) where ``fields`` is
    the optional device-description filter (model / manufacturer /
    friendly_name) that lets several drivers share one device-type URN —
    mirrors ``_parse_ssdp_entry`` in the platform's hints.py.
    """
    if isinstance(raw, str):
        if not raw:
            return ([f"{file}: discovery.ssdp device_type must be a non-empty string"], None)
        return ([], {"device_type": raw, "fields": {}})
    if not isinstance(raw, dict):
        return (
            [f"{file}: discovery.ssdp entries must be strings or "
             "{device_type, model, manufacturer, friendly_name, cross_vendor} mappings"],
            None,
        )
    errors: list[str] = []
    unknown = set(raw.keys()) - {"device_type", "cross_vendor", *_SSDP_FILTER_KEYS}
    if unknown:
        errors.append(
            f"{file}: discovery.ssdp entry has unknown keys: {sorted(unknown)}"
        )
    dt = raw.get("device_type")
    if not isinstance(dt, str) or not dt:
        errors.append(f"{file}: discovery.ssdp.device_type must be a non-empty string")
        return (errors, None)
    fields: dict[str, str] = {}
    for key in _SSDP_FILTER_KEYS:
        if raw.get(key) is None:
            continue
        if not isinstance(raw[key], str) or not raw[key]:
            errors.append(f"{file}: discovery.ssdp.{key} must be a non-empty string")
            continue
        fields[key] = raw[key]
    if "cross_vendor" in raw and not isinstance(raw["cross_vendor"], bool):
        errors.append(f"{file}: discovery.ssdp.cross_vendor must be a bool")
    return (errors, {"device_type": dt, "fields": fields})


def _validate_amx_ddp_entry(file: str, raw: Any) -> tuple[list[str], dict[str, str] | None]:
    """Validate one AMX-DDP fingerprint entry."""
    if not isinstance(raw, dict):
        return ([f"{file}: discovery.amx_ddp entries must be mappings"], None)
    errors: list[str] = []
    unknown = set(raw.keys()) - {"make", "model_pattern", "cross_vendor"}
    if unknown:
        errors.append(
            f"{file}: discovery.amx_ddp entry has unknown keys: {sorted(unknown)}"
        )
    make = raw.get("make")
    if not isinstance(make, str) or not make:
        errors.append(f"{file}: discovery.amx_ddp.make is required")
        return (errors, None)
    model_pattern = raw.get("model_pattern", "*")
    if not isinstance(model_pattern, str):
        errors.append(f"{file}: discovery.amx_ddp.model_pattern must be a string")
        model_pattern = "*"
    if "cross_vendor" in raw and not isinstance(raw["cross_vendor"], bool):
        errors.append(f"{file}: discovery.amx_ddp.cross_vendor must be a bool")
    return (errors, {"make": make, "model_pattern": str(model_pattern)})


def validate_discovery_block(
    file: str, raw: dict[str, Any], *, yaml_dir: Path | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Return (errors, normalized_discovery) for one driver.

    Mirrors ``parse_driver_discovery`` in the platform. Drivers whose IDs
    start with ``generic_`` are exempt. ``yaml_dir`` is the directory the
    YAML lives in — when provided, used to verify a declared
    ``python:`` companion .py exists alongside the YAML.
    """
    errors: list[str] = []
    normalized: dict[str, Any] = {}
    driver_id = str(raw.get("id") or "")
    if any(driver_id.startswith(p) for p in ("generic_",)):
        return errors, normalized

    discovery = raw.get("discovery") or {}
    if not isinstance(discovery, dict):
        errors.append(f"{file}: discovery: must be a mapping")
        return errors, normalized

    unknown = set(discovery.keys()) - _KNOWN_DISCOVERY_KEYS
    if unknown:
        errors.append(
            f"{file}: discovery has unknown keys: {sorted(unknown)}. "
            f"Known keys: {sorted(_KNOWN_DISCOVERY_KEYS)}"
        )

    # ``requires`` is normally stamped by this script at emission time
    # (see _DISCOVERY_FEATURE_GATES); a hand-authored value is accepted
    # but must be a parseable version string, or every platform would
    # skip the block conservatively.
    if "requires" in discovery:
        req = discovery["requires"]
        if not isinstance(req, str) or _version_tuple(req) is None:
            errors.append(
                f"{file}: discovery.requires must be a version string "
                f"like \"0.23.0\", got {req!r}"
            )

    # --- Fingerprints -----------------------------------------------------

    normalized_mdns: list[dict[str, Any]] = []
    if "mdns" in discovery:
        for entry in _as_list(discovery["mdns"]):
            entry_errors, entry_norm = _validate_mdns_entry(file, entry)
            errors.extend(entry_errors)
            if entry_norm is not None:
                normalized_mdns.append(entry_norm)
    normalized["mdns"] = normalized_mdns

    normalized_ssdp: list[dict[str, Any]] = []
    if "ssdp" in discovery:
        for entry in _as_list(discovery["ssdp"]):
            entry_errors, dt = _validate_ssdp_entry(file, entry)
            errors.extend(entry_errors)
            if dt is not None:
                normalized_ssdp.append(dt)
    normalized["ssdp"] = normalized_ssdp

    normalized_amx: list[dict[str, str]] = []
    if "amx_ddp" in discovery:
        for entry in _as_list(discovery["amx_ddp"]):
            entry_errors, entry_norm = _validate_amx_ddp_entry(file, entry)
            errors.extend(entry_errors)
            if entry_norm is not None:
                normalized_amx.append(entry_norm)
    normalized["amx_ddp"] = normalized_amx

    has_tcp_probe = False
    has_udp_probe = False
    if "tcp_probe" in discovery:
        errors.extend(_validate_probe_block(file, "tcp", discovery["tcp_probe"]))
        has_tcp_probe = True
    if "udp_probe" in discovery:
        errors.extend(_validate_probe_block(file, "udp", discovery["udp_probe"]))
        has_udp_probe = True
    normalized["has_tcp_probe"] = has_tcp_probe
    normalized["has_udp_probe"] = has_udp_probe

    has_python = False
    if "python" in discovery:
        errors.extend(_validate_python_block(
            file, discovery["python"], yaml_dir=yaml_dir,
        ))
        has_python = True
    normalized["has_python"] = has_python

    # --- Hints ------------------------------------------------------------

    if "snmp_pen" in discovery:
        pen = discovery["snmp_pen"]
        if not isinstance(pen, int) or isinstance(pen, bool) or pen < 1:
            errors.append(f"{file}: discovery.snmp_pen must be a positive integer")

    raw_oui = discovery.get("oui") or []
    if not isinstance(raw_oui, list):
        errors.append(f"{file}: discovery.oui must be a list")
        raw_oui = []
    for prefix in raw_oui:
        if not isinstance(prefix, str) or not prefix:
            errors.append(
                f"{file}: discovery.oui entries must be non-empty strings, "
                f"got {prefix!r}"
            )

    raw_host = discovery.get("hostname") or []
    if not isinstance(raw_host, list):
        errors.append(f"{file}: discovery.hostname must be a list")
        raw_host = []
    for pattern in raw_host:
        if not isinstance(pattern, str) or not pattern:
            errors.append(
                f"{file}: discovery.hostname entries must be non-empty strings, "
                f"got {pattern!r}"
            )
            continue
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            errors.append(
                f"{file}: discovery.hostname entry {pattern!r} failed to "
                f"compile: {exc}"
            )
    normalized["hostname"] = [p for p in raw_host if isinstance(p, str) and p]

    raw_ports = discovery.get("port_open") or []
    if not isinstance(raw_ports, list):
        errors.append(f"{file}: discovery.port_open must be a list")
        raw_ports = []
    for port in raw_ports:
        if not isinstance(port, int) or isinstance(port, bool):
            errors.append(
                f"{file}: discovery.port_open entries must be integers, got {port!r}"
            )
            continue
        if port < 1 or port > 65535:
            errors.append(
                f"{file}: discovery.port_open entry {port} out of range [1, 65535]"
            )
            continue
        if port in _DISALLOWED_OPEN_PORTS:
            errors.append(
                f"{file}: discovery.port_open entry {port} is too generic — "
                f"would match every web/SSH device. "
                f"Disallowed: {sorted(_DISALLOWED_OPEN_PORTS)}"
            )

    raw_aliases = discovery.get("manufacturer_alias") or []
    if not isinstance(raw_aliases, list):
        errors.append(f"{file}: discovery.manufacturer_alias must be a list")
        raw_aliases = []
    for alias in raw_aliases:
        if not isinstance(alias, str):
            errors.append(
                f"{file}: discovery.manufacturer_alias entries must be strings, "
                f"got {alias!r}"
            )
            continue
        if not alias.strip():
            errors.append(
                f"{file}: discovery.manufacturer_alias entries must be non-empty"
            )

    has_any_signal = (
        bool(normalized_mdns)
        or bool(normalized_ssdp)
        or bool(normalized_amx)
        or has_tcp_probe
        or has_udp_probe
        or has_python
        or "snmp_pen" in discovery
        or bool(raw_oui)
        or bool(raw_host)
        or bool(raw_ports)
        or bool(raw_aliases)
    )
    if not has_any_signal:
        sys.stderr.write(
            f"warning: {file}: discovery block declares no fingerprints or "
            "hints; this driver will never participate in matching.\n"
        )
    return errors, normalized


def validate_no_signal_collisions(
    per_driver: list[tuple[str, str, dict[str, Any]]],
) -> list[str]:
    """Cross-driver: refuse two drivers claiming the same fingerprint."""
    errors: list[str] = []
    # (kind, source_id) -> list[(driver_id, file, txt_filter)]
    bucket: dict[tuple[str, str], list[tuple[str, str, frozenset]]] = {}

    def claim(kind: str, source_id: str, driver_id: str, file: str, txt_filter: dict[str, str] | None) -> None:
        key = (kind, source_id)
        existing = bucket.setdefault(key, [])
        filter_set = frozenset((k.lower(), str(v)) for k, v in (txt_filter or {}).items())
        for prior_driver, prior_file, prior_filter in existing:
            if prior_driver == driver_id:
                return  # Same driver re-claim is harmless.
            if prior_filter == filter_set:
                errors.append(
                    f"Signal collision: {kind}:{source_id} claimed by "
                    f"both {prior_driver!r} ({prior_file}) and "
                    f"{driver_id!r} ({file})"
                )
                return
            if not prior_filter and filter_set:
                errors.append(
                    f"Signal collision: {kind}:{source_id} — {prior_driver!r} "
                    f"({prior_file}) claims it without a TXT filter, which "
                    f"would shadow {driver_id!r}'s filtered claim ({file})"
                )
                return
            if prior_filter and not filter_set:
                errors.append(
                    f"Signal collision: {kind}:{source_id} — {driver_id!r} "
                    f"({file}) claims it without a TXT filter, which would "
                    f"shadow {prior_driver!r}'s filtered claim ({prior_file})"
                )
                return
        existing.append((driver_id, file, filter_set))

    for driver_id, file, normalized in per_driver:
        for entry in normalized.get("mdns", []):
            service_norm = entry["service"].lower().rstrip(".") + "."
            claim("mdns", service_norm, driver_id, file, entry.get("txt"))
        for entry in normalized.get("ssdp", []):
            claim("ssdp", entry["device_type"], driver_id, file, entry.get("fields"))
        for amx in normalized.get("amx_ddp", []):
            claim("amx_ddp", f"{amx['make']}/{amx['model_pattern']}",
                  driver_id, file, None)
        if normalized.get("has_tcp_probe"):
            claim("probe", f"custom_{driver_id}_tcp", driver_id, file, None)
        if normalized.get("has_udp_probe"):
            claim("broadcast", f"custom_{driver_id}_udp", driver_id, file, None)
        if normalized.get("has_python"):
            claim("broadcast", f"custom_{driver_id}_companion_udp",
                  driver_id, file, None)
            claim("probe", f"custom_{driver_id}_companion_tcp",
                  driver_id, file, None)

    return errors


# Pre-existing shared-hostname drivers that predate this check and can't be
# given a declarative disambiguator without hardware we don't have. Grandfathered
# so the rule is a hard error for NEW occurrences while these stay listed here
# for cleanup. birddog_ptz / birddog_codec both default to the
# "^birddog-" hostname with only soft signals (oui + NDI, deliberately not an
# mdns claim since _ndi._tcp is cross-vendor); telling a PTZ from a codec
# pre-install needs a probe of the BirdDog HTTP /about API, which needs a
# capture from real gear. Remove an id here once it carries a real fingerprint.
_KNOWN_LOCATOR_AMBIGUITY: frozenset[str] = frozenset({
    "birddog_ptz", "birddog_codec",
})


def validate_locator_disambiguation(
    per_driver: list[tuple[str, str, dict[str, Any]]],
) -> list[str]:
    """Cross-driver: a hostname pattern shared by 2+ drivers needs each of
    them to carry a *declarative* pre-install fingerprint.

    ``hostname`` (and ``port_open``) are soft locator hints: they narrow the
    candidate set but never identify on their own. When several drivers claim
    the same hostname pattern, the thing that tells them apart has to run
    before any of them is installed — a declarative ``tcp_probe`` /
    ``udp_probe`` / ``mdns`` / ``ssdp`` / ``amx_ddp`` fingerprint, which the
    engine evaluates straight from the catalog. A ``python:`` companion does
    NOT count: companions load only from on-disk ``*_discovery.py`` files, so
    they run only after the driver is installed. A driver that leans on a
    companion as its sole strong signal while sharing a hostname with a
    sibling gets mislabeled as that sibling on any scan where it isn't
    installed (the exact TurtleAV Darwin-vs-Chazy failure this check exists
    to prevent). Discovery's job is to identify gear you have NOT installed
    yet, so the disambiguator must be declarative.
    """
    errors: list[str] = []
    # hostname pattern (lowercased) -> {driver_id: (file, normalized)}.
    # Keyed by driver_id so a driver listing the same pattern twice counts
    # once, and a pattern claimed by a single driver never trips the check.
    by_host: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
    for driver_id, file, norm in per_driver:
        for pattern in norm.get("hostname", []):
            by_host.setdefault(pattern.lower(), {})[driver_id] = (file, norm)

    for pattern, claimants in sorted(by_host.items()):
        if len(claimants) < 2:
            continue
        for driver_id, (file, norm) in sorted(claimants.items()):
            has_declarative_fingerprint = (
                norm.get("has_tcp_probe")
                or norm.get("has_udp_probe")
                or bool(norm.get("mdns"))
                or bool(norm.get("ssdp"))
                or bool(norm.get("amx_ddp"))
            )
            if has_declarative_fingerprint:
                continue
            if driver_id in _KNOWN_LOCATOR_AMBIGUITY:
                continue  # grandfathered pre-existing case
            others = sorted(d for d in claimants if d != driver_id)
            errors.append(
                f"Locator ambiguity: {driver_id!r} ({file}) shares the "
                f"hostname pattern {pattern!r} with {others} but declares no "
                f"pre-install fingerprint (tcp_probe / udp_probe / mdns / "
                f"ssdp / amx_ddp). An uninstalled device matching that "
                f"hostname can't be told apart from its siblings; a python: "
                f"companion only runs once the driver is installed, so it "
                f"can't disambiguate first. Add a declarative fingerprint "
                f"(e.g. a tcp_probe matching this driver's banner token)."
            )
    return errors


def _version_tuple(version: str) -> tuple[int, ...] | None:
    """Parse ``"0.23.0"`` into a comparable tuple (suffixes after ``-``/``+``
    ignored). ``None`` when the string doesn't lead with dotted integers."""
    core = re.split(r"[-+ ]", version.strip(), maxsplit=1)[0]
    try:
        return tuple(int(p) for p in core.split("."))
    except ValueError:
        return None


# Platform version whose parser first UNDERSTANDS each discovery feature
# below. This matters only for features an older parser silently mis-reads
# rather than rejects — e.g. SSDP description filters: pre-0.23.0 parsers
# ignore the filter fields, collapsing distinct filtered claims into
# colliding unfiltered ones that (pre-0.23.0) abort the whole catalog fold
# to installed-only. The emitted catalog entry gains ``requires: <version>``
# — a top-level discovery key those parsers reject — so they skip just this
# driver's hints and the rest of the catalog stays live. Platforms >= 0.23.0
# honor ``requires`` directly and skip blocks gated on a newer version than
# they are. Add a (predicate, version) pair here whenever a new discovery
# feature extends the meaning of an existing key.
def _ssdp_has_description_filters(discovery: dict[str, Any]) -> bool:
    for entry in _as_list(discovery.get("ssdp")):
        if isinstance(entry, dict) and any(
            entry.get(k) is not None for k in _SSDP_FILTER_KEYS
        ):
            return True
    return False


def _probe_has_follow_up(discovery: dict[str, Any]) -> bool:
    probe = discovery.get("tcp_probe")
    return isinstance(probe, dict) and probe.get("then") is not None


def _udp_probe_has_reply_port(discovery: dict[str, Any]) -> bool:
    probe = discovery.get("udp_probe")
    return isinstance(probe, dict) and probe.get("reply_port") is not None


_DISCOVERY_FEATURE_GATES: tuple[tuple[Any, str], ...] = (
    (_ssdp_has_description_filters, "0.23.0"),
    # A `then:` step needs the 0.32.0 probe runner. An older platform rejects
    # the `requires` key and skips this driver's hints rather than mis-reading
    # the probe as a plain one-shot — which would match every SQ as well.
    (_probe_has_follow_up, "0.32.0"),
    # A `reply_port` needs the 0.37.2 probe runner. Without the gate an older
    # parser refuses the unknown key, and with it this driver's whole block,
    # as a malformed one; with it, the block is skipped as written for a newer
    # platform. Either way an older platform uses none of this driver's hints.
    (_udp_probe_has_reply_port, "0.37.2"),
)


def with_discovery_requires(discovery: dict[str, Any]) -> dict[str, Any]:
    """Return the catalog-emission form of a discovery block.

    Stamps/raises ``requires`` to the newest platform version any used
    feature gate demands (keeping a hand-authored ``requires`` when it is
    already newer). Blocks that use no gated features emit unchanged so
    every platform keeps reading them.
    """
    needed: str | None = discovery.get("requires")
    for predicate, version in _DISCOVERY_FEATURE_GATES:
        if not predicate(discovery):
            continue
        if needed is None or (
            (_version_tuple(version) or ())
            > (_version_tuple(needed) or ())
        ):
            needed = version
    if needed is None or needed == discovery.get("requires"):
        return discovery
    return {"requires": needed, **{
        k: v for k, v in discovery.items() if k != "requires"
    }}
