"""The catalog's checks of one driver's ``discovery:`` block, and the stamp it is written with.

``validate_discovery_block`` checks one driver's block the way the platform's
discovery parser reads it (``parse_driver_discovery`` in
``openavc/discovery/hints.py``), but collects every problem where the parser
stops at the first, and also requires a declared ``python:`` companion to ship
beside the driver. It returns the block's normalised form, which
``discovery_claims`` reads to check the drivers against each other.
``with_discovery_requires`` is the ``requires:`` stamp a catalog entry is
written with.

The platform's parser is the authority and this is a hand copy of it. A new
discovery key lands there first; here it needs its place in the known keys,
its check, and a gate in ``_DISCOVERY_FEATURE_GATES`` when an older parser
would misread it rather than refuse it. A new fingerprint kind is its key in
``_KNOWN_DISCOVERY_KEYS``, an entry check and its row in
``_FINGERPRINT_ENTRIES`` here, and its claim in ``discovery_claims``.
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

_SSDP_FILTER_KEYS = ("model", "manufacturer", "friendly_name")
_KNOWN_MDNS_KEYS = frozenset({"service", "txt", "cross_vendor"})
_KNOWN_SSDP_KEYS = frozenset({"device_type", "cross_vendor", *_SSDP_FILTER_KEYS})
_KNOWN_AMX_DDP_KEYS = frozenset({"make", "model_pattern", "cross_vendor"})
_KNOWN_PYTHON_KEYS = frozenset({"file", "cross_vendor"})

# Template drivers carry no discovery of their own.
_EXEMPT_ID_PREFIXES = ("generic_",)


_SEND_KEYS = ("send_hex", "send_ascii")
_MATCHER_KEYS = ("expect", "expect_regex", "expect_hex")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# --- The shapes a value is checked against ----------------------------------
#
# ``at`` is the dotted place a message names (``discovery.tcp_probe.port``).
# A key written with a null value counts as not declared, the way the
# platform's parser reads it.


def _is_int(value: Any) -> bool:
    """An integer that is not a bool (YAML's ``true`` is an int to Python)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_port(value: Any) -> bool:
    return _is_int(value) and 1 <= value <= 65535


def _declared(raw: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    """The ``keys`` set to something other than null, in ``keys`` order."""
    return [key for key in keys if raw.get(key) is not None]


def _unknown_keys(file: str, at: str, raw: dict[str, Any], known: frozenset[str]) -> list[str]:
    unknown = set(raw.keys()) - known
    if not unknown:
        return []
    return [f"{file}: {at} has unknown keys: {sorted(unknown)}"]


def _not_bool(file: str, at: str, raw: dict[str, Any], key: str) -> list[str]:
    """A present ``key`` that is not a bool (null included)."""
    if key in raw and not isinstance(raw[key], bool):
        return [f"{file}: {at}.{key} must be a bool"]
    return []


def _text_errors(file: str, at: str, value: Any) -> list[str]:
    if isinstance(value, str) and value:
        return []
    return [f"{file}: {at} must be a non-empty string"]


def _hex_errors(file: str, at: str, value: Any) -> list[str]:
    if not isinstance(value, str):
        return [f"{file}: {at} must be a string"]
    try:
        bytes.fromhex(value.replace(" ", "").replace(":", ""))
    except ValueError as exc:
        return [f"{file}: {at} is not valid hex: {exc}"]
    return []


def _compile_errors(file: str, at: str, pattern: str, flags: int = 0) -> list[str]:
    try:
        re.compile(pattern, flags)
    except re.error as exc:
        return [f"{file}: {at} failed to compile: {exc}"]
    return []


def _regex_errors(file: str, at: str, value: Any) -> list[str]:
    return _text_errors(file, at, value) or _compile_errors(file, at, value)


def _timeout_errors(file: str, at: str, value: Any, *, too_long: str) -> list[str]:
    """A ``timeout_ms`` is a positive integer no longer than the probe runner waits.

    ``too_long`` ends the sentence for one over the limit: a probe and its
    ``then:`` step have always worded it differently.
    """
    if value is None:
        return []
    if not _is_int(value) or value < 1:
        return [f"{file}: {at}.timeout_ms must be a positive integer"]
    if value > _MAX_PROBE_TIMEOUT_MS:
        return [f"{file}: {at}.timeout_ms {too_long}"]
    return []


# --- A probe: tcp_probe / udp_probe ------------------------------------------


def _validate_probe_block(file: str, kind: str, raw: Any) -> list[str]:
    """Return validation errors for a ``tcp_probe:`` / ``udp_probe:`` block."""
    where = f"{kind}_probe"
    at = f"discovery.{where}"
    if not isinstance(raw, dict):
        return [f"{file}: {at} must be a mapping"]

    errors = _unknown_keys(file, at, raw, _KNOWN_PROBE_KEYS)
    if not _is_port(raw.get("port")):
        errors.append(f"{file}: {at}.port must be an integer in [1, 65535]")
    errors += _probe_send_errors(file, at, kind, raw)
    errors += _probe_matcher_errors(file, at, kind, raw)
    errors += _timeout_errors(
        file, at, raw.get("timeout_ms"),
        too_long=f"exceeds the max of {_MAX_PROBE_TIMEOUT_MS} ms",
    )
    errors += _not_bool(file, at, raw, "cross_vendor")
    errors += _tls_errors(file, at, kind, raw)
    errors += _reply_port_errors(file, at, kind, raw.get("reply_port"))
    errors += _validate_extract_block(file, f"{at}.extract", raw.get("extract"))
    errors += _extract_manufacturer_errors(file, at, raw)
    errors += _validate_follow_up_block(file, where, kind, raw.get("then"))
    return errors


def _probe_send_errors(file: str, at: str, kind: str, raw: dict[str, Any]) -> list[str]:
    """One of send_hex or send_ascii, well formed; a UDP probe has to send one."""
    sends = _declared(raw, _SEND_KEYS)
    errors: list[str] = []
    if len(sends) == len(_SEND_KEYS):
        errors.append(f"{file}: {at} declares both send_hex and send_ascii — pick one")
    if "send_hex" in sends:
        errors += _hex_errors(file, f"{at}.send_hex", raw["send_hex"])
    if "send_ascii" in sends and not isinstance(raw["send_ascii"], str):
        errors.append(f"{file}: {at}.send_ascii must be a string")
    if kind == "udp" and not sends:
        errors.append(
            f"{file}: {at} must declare send_ascii or send_hex "
            "(UDP probes need a query payload)"
        )
    return errors


_MATCHER_CHECKS = {
    "expect": _text_errors,
    "expect_regex": _regex_errors,
    "expect_hex": _hex_errors,
}


# What a probe that needs a matcher and has none is told, by kind.
_NO_MATCHER = {
    "udp": "needs exactly one of expect, expect_regex, expect_hex",
    "tcp": (
        "sends bytes but declares no matcher "
        "— add exactly one of expect, expect_regex, or expect_hex"
    ),
}


def _probe_matcher_errors(file: str, at: str, kind: str, raw: dict[str, Any]) -> list[str]:
    """At most one matcher, well formed. A UDP probe needs one, and so does a TCP
    probe that sends something; a TCP probe that only reads a banner does not."""
    matchers = _declared(raw, _MATCHER_KEYS)
    errors: list[str] = []
    if len(matchers) > 1:
        errors.append(
            f"{file}: {at} declares multiple matchers "
            f"({', '.join(matchers)}) — pick exactly one of "
            "expect, expect_regex, or expect_hex"
        )
    for key in matchers:
        errors += _MATCHER_CHECKS[key](file, f"{at}.{key}", raw[key])
    needs_one = kind == "udp" or bool(_declared(raw, _SEND_KEYS))
    if needs_one and not matchers:
        errors.append(f"{file}: {at} {_NO_MATCHER[kind]}")
    return errors


def _tls_errors(file: str, at: str, kind: str, raw: dict[str, Any]) -> list[str]:
    errors = _not_bool(file, at, raw, "tls")
    if raw.get("tls") is True and kind != "tcp":
        errors.append(f"{file}: {at}.tls is only valid on a tcp_probe")
    return errors


def _reply_port_errors(file: str, at: str, kind: str, reply_port: Any) -> list[str]:
    if reply_port is None:
        return []
    if kind != "udp":
        return [
            f"{file}: {at}.reply_port is only valid on a "
            "udp_probe (a tcp_probe's answer comes back on its own connection)"
        ]
    if not _is_port(reply_port):
        return [f"{file}: {at}.reply_port must be an integer in [1, 65535]"]
    return []


def _validate_extract_block(file: str, at: str, raw: Any) -> list[str]:
    """Return validation errors for an ``extract:`` block at ``at``."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [
            f"{file}: {at} must be a mapping of field "
            "name to literal string or {regex, group} mapping"
        ]
    errors: list[str] = []
    for name, spec in raw.items():
        errors += _extract_field_errors(file, at, name, spec)
    return errors


def _extract_field_errors(file: str, at: str, name: Any, spec: Any) -> list[str]:
    """One ``extract:`` field: a literal value, or a ``{regex, group}`` capture."""
    if not isinstance(name, str) or not name:
        return [f"{file}: {at} field names must be non-empty strings"]
    if isinstance(spec, str):
        return []
    if isinstance(spec, dict):
        return _extract_capture_errors(file, f"{at}.{name}", spec)
    return [
        f"{file}: {at}.{name} must be a literal "
        "string or a {regex, group} mapping"
    ]


def _extract_capture_errors(file: str, at: str, spec: dict[str, Any]) -> list[str]:
    """A ``{regex, group}`` capture: a pattern that compiles, and a group number."""
    pattern = spec.get("regex")
    if isinstance(pattern, str) and pattern:
        errors = _compile_errors(file, f"{at}.regex", pattern)
    else:
        errors = [f"{file}: {at} mapping requires a non-empty 'regex' string"]
    group = spec.get("group", 1)
    if not _is_int(group) or group < 0:
        errors.append(f"{file}: {at}.group must be a non-negative integer")
    return errors


def _extract_manufacturer_errors(file: str, at: str, raw: dict[str, Any]) -> list[str]:
    """``extract_manufacturer`` is shorthand for ``extract.manufacturer``, so not both."""
    mfg = raw.get("extract_manufacturer")
    if mfg is None:
        return []
    errors = _text_errors(file, f"{at}.extract_manufacturer", mfg)
    if errors:
        return errors
    extract = raw.get("extract")
    if isinstance(extract, dict) and "manufacturer" in extract:
        return [
            f"{file}: {at}.extract_manufacturer collides with "
            "extract.manufacturer — pick one"
        ]
    return []


def _validate_follow_up_block(
    file: Any, where: str, kind: str, raw: Any,
) -> list[str]:
    """Return validation errors for a probe's ``then:`` follow-up step.

    Mirrors openavc/discovery/hints.py ``_parse_follow_up``. The runtime is the
    authority; this copy exists so the catalog refuses a block the platform
    would reject at load rather than shipping a driver that silently never
    matches. Unlike the probe's own checks, these do not look inside what the
    step sends or matches.
    """
    if raw is None:
        return []
    at = f"discovery.{where}.then"
    if kind != "tcp":
        return [f"{file}: {at} is only valid on a tcp_probe"]
    if not isinstance(raw, dict):
        return [f"{file}: {at} must be a mapping"]

    errors = _unknown_keys(file, at, raw, _KNOWN_FOLLOW_UP_KEYS)
    sends = _declared(raw, _SEND_KEYS)
    if not sends:
        errors.append(
            f"{file}: {at} must declare send_ascii or send_hex "
            "(a follow-up step has to ask something)"
        )
    if len(sends) == len(_SEND_KEYS):
        errors.append(f"{file}: {at} declares both send_hex and send_ascii — pick one")
    errors += _follow_up_expectation_errors(file, at, raw)
    errors += _timeout_errors(
        file, at, raw.get("timeout_ms"),
        too_long=f"must not exceed {_MAX_PROBE_TIMEOUT_MS} ms",
    )
    return errors


def _follow_up_expectation_errors(file: str, at: str, raw: dict[str, Any]) -> list[str]:
    """A ``then:`` step expects silence or a match, never both and never neither."""
    silence = raw.get("expect_silence")
    errors: list[str] = []
    if silence is not None and not isinstance(silence, bool):
        errors.append(f"{file}: {at}.expect_silence must be a bool")
    has_match = bool(_declared(raw, _MATCHER_KEYS))
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
    return errors


# --- python: ------------------------------------------------------------------


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
    errors = _python_declaration_errors(file, raw)
    file_path = declared_companion(raw)
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


def declared_companion(raw: Any) -> str | None:
    """The companion file a ``python:`` declaration names, or None if it names none.

    A non-empty string is the path; a mapping names it under ``file``. The
    catalog build fetches the same file when it lists what an install of a
    YAML driver downloads (``build_index._installed_file_set``).
    """
    candidate = raw.get("file") if isinstance(raw, dict) else raw
    return candidate if isinstance(candidate, str) and candidate else None


def _python_declaration_errors(file: str, raw: Any) -> list[str]:
    """What is wrong with a ``python:`` declaration."""
    if isinstance(raw, str):
        return [] if raw else [f"{file}: discovery.python path must be a non-empty string"]
    if not isinstance(raw, dict):
        return [
            f"{file}: discovery.python must be a string path or "
            "{file, cross_vendor} mapping"
        ]
    return [
        *_unknown_keys(file, "discovery.python", raw, _KNOWN_PYTHON_KEYS),
        *_text_errors(file, "discovery.python.file", raw.get("file")),
        *_not_bool(file, "discovery.python", raw, "cross_vendor"),
    ]


# --- Fingerprints: mdns, ssdp, amx_ddp ------------------------------------------
#
# Each entry check returns (errors, normalised entry); the entry is None when
# it is too broken to claim anything, so the cross-driver checks never see it.


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
    errors = _unknown_keys(file, "discovery.mdns entry", raw, _KNOWN_MDNS_KEYS)
    service = raw.get("service")
    service_errors = _text_errors(file, "discovery.mdns.service", service)
    if service_errors:
        return (errors + service_errors, None)
    txt_raw = raw.get("txt") or {}
    if not isinstance(txt_raw, dict):
        errors.append(f"{file}: discovery.mdns.txt must be a mapping")
        txt_raw = {}
    errors += _not_bool(file, "discovery.mdns", raw, "cross_vendor")
    return (
        errors,
        {"service": service, "txt": {str(k): str(v) for k, v in txt_raw.items()}},
    )


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
    errors = _unknown_keys(file, "discovery.ssdp entry", raw, _KNOWN_SSDP_KEYS)
    dt = raw.get("device_type")
    dt_errors = _text_errors(file, "discovery.ssdp.device_type", dt)
    if dt_errors:
        return (errors + dt_errors, None)
    filter_errors, fields = _ssdp_filters(file, raw)
    errors += filter_errors
    errors += _not_bool(file, "discovery.ssdp", raw, "cross_vendor")
    return (errors, {"device_type": dt, "fields": fields})


def _ssdp_filters(file: str, raw: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """The device-description filters an SSDP entry declares, and the well-formed ones."""
    errors: list[str] = []
    fields: dict[str, str] = {}
    for key in _declared(raw, _SSDP_FILTER_KEYS):
        key_errors = _text_errors(file, f"discovery.ssdp.{key}", raw[key])
        errors += key_errors
        if not key_errors:
            fields[key] = raw[key]
    return errors, fields


def _validate_amx_ddp_entry(file: str, raw: Any) -> tuple[list[str], dict[str, str] | None]:
    """Validate one AMX-DDP fingerprint entry."""
    if not isinstance(raw, dict):
        return ([f"{file}: discovery.amx_ddp entries must be mappings"], None)
    errors = _unknown_keys(file, "discovery.amx_ddp entry", raw, _KNOWN_AMX_DDP_KEYS)
    make = raw.get("make")
    if not isinstance(make, str) or not make:
        errors.append(f"{file}: discovery.amx_ddp.make is required")
        return (errors, None)
    model_pattern = raw.get("model_pattern", "*")
    if not isinstance(model_pattern, str):
        errors.append(f"{file}: discovery.amx_ddp.model_pattern must be a string")
        model_pattern = "*"
    errors += _not_bool(file, "discovery.amx_ddp", raw, "cross_vendor")
    return (errors, {"make": make, "model_pattern": str(model_pattern)})


_FINGERPRINT_ENTRIES = (
    ("mdns", _validate_mdns_entry),
    ("ssdp", _validate_ssdp_entry),
    ("amx_ddp", _validate_amx_ddp_entry),
)


def _fingerprint_entries(
    file: str, discovery: dict[str, Any], key: str, check: Any,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Check every entry under one fingerprint key (one entry or a list of them)."""
    errors: list[str] = []
    entries: list[dict[str, Any]] = []
    for entry in _as_list(discovery.get(key)):
        entry_errors, normalized = check(file, entry)
        errors += entry_errors
        if normalized is not None:
            entries.append(normalized)
    return errors, entries


# --- Hints: snmp_pen, oui, hostname, port_open, manufacturer_alias ---------------
#
# Each list hint checks its entries one at a time and reports the first thing
# wrong with an entry.


def _snmp_pen_errors(file: str, discovery: dict[str, Any]) -> list[str]:
    if "snmp_pen" not in discovery:
        return []
    pen = discovery["snmp_pen"]
    if _is_int(pen) and pen >= 1:
        return []
    return [f"{file}: discovery.snmp_pen must be a positive integer"]


def _oui_entry_errors(file: str, prefix: Any) -> list[str]:
    if isinstance(prefix, str) and prefix:
        return []
    return [f"{file}: discovery.oui entries must be non-empty strings, got {prefix!r}"]


def _hostname_entry_errors(file: str, pattern: Any) -> list[str]:
    if not isinstance(pattern, str) or not pattern:
        return [
            f"{file}: discovery.hostname entries must be non-empty strings, "
            f"got {pattern!r}"
        ]
    return _compile_errors(file, f"discovery.hostname entry {pattern!r}", pattern, re.IGNORECASE)


def _port_open_entry_errors(file: str, port: Any) -> list[str]:
    if not _is_int(port):
        return [f"{file}: discovery.port_open entries must be integers, got {port!r}"]
    if not _is_port(port):
        return [f"{file}: discovery.port_open entry {port} out of range [1, 65535]"]
    if port in _DISALLOWED_OPEN_PORTS:
        return [
            f"{file}: discovery.port_open entry {port} is too generic — "
            f"would match every web/SSH device. "
            f"Disallowed: {sorted(_DISALLOWED_OPEN_PORTS)}"
        ]
    return []


def _alias_entry_errors(file: str, alias: Any) -> list[str]:
    if not isinstance(alias, str):
        return [f"{file}: discovery.manufacturer_alias entries must be strings, got {alias!r}"]
    if not alias.strip():
        return [f"{file}: discovery.manufacturer_alias entries must be non-empty"]
    return []


_HINT_LISTS = (
    ("oui", _oui_entry_errors),
    ("hostname", _hostname_entry_errors),
    ("port_open", _port_open_entry_errors),
    ("manufacturer_alias", _alias_entry_errors),
)


def _hint_list(
    file: str, discovery: dict[str, Any], key: str, check: Any,
) -> tuple[list[str], list[Any]]:
    """Check one list hint: errors, and the entries as declared (none when it is
    not a list)."""
    entries = discovery.get(key) or []
    if not isinstance(entries, list):
        return [f"{file}: discovery.{key} must be a list"], []
    errors: list[str] = []
    for entry in entries:
        errors += check(file, entry)
    return errors, entries


# --- One driver's block -----------------------------------------------------------


def validate_discovery_block(
    file: str, raw: dict[str, Any], *, yaml_dir: Path | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Return (errors, normalized_discovery) for one driver.

    Mirrors ``parse_driver_discovery`` in the platform. Drivers whose IDs
    start with ``generic_`` are exempt. ``yaml_dir`` is the directory the
    YAML lives in — when provided, used to verify a declared
    ``python:`` companion .py exists alongside the YAML.

    The normalised form is what the cross-driver checks read: each
    fingerprint's well-formed entries, whether a tcp_probe, udp_probe and
    python companion are declared, and the hostname patterns.
    """
    driver_id = str(raw.get("id") or "")
    if driver_id.startswith(_EXEMPT_ID_PREFIXES):
        return [], {}

    discovery = raw.get("discovery") or {}
    if not isinstance(discovery, dict):
        return [f"{file}: discovery: must be a mapping"], {}

    errors = _top_level_errors(file, discovery)
    fingerprint_errors, normalized = _fingerprints(file, discovery, yaml_dir)
    hint_errors, hints = _hints(file, discovery)
    errors += fingerprint_errors + hint_errors
    declares_a_signal = _declares_a_signal(discovery, normalized, hints)
    normalized["hostname"] = [p for p in hints["hostname"] if isinstance(p, str) and p]

    if not declares_a_signal:
        sys.stderr.write(
            f"warning: {file}: discovery block declares no fingerprints or "
            "hints; this driver will never participate in matching.\n"
        )
    return errors, normalized


def _fingerprints(
    file: str, discovery: dict[str, Any], yaml_dir: Path | None,
) -> tuple[list[str], dict[str, Any]]:
    """The fingerprints: mdns, ssdp and amx_ddp entries, the two probes and the
    python companion. Returns their errors and the start of the normalised form."""
    errors: list[str] = []
    normalized: dict[str, Any] = {}
    for key, check in _FINGERPRINT_ENTRIES:
        entry_errors, normalized[key] = _fingerprint_entries(file, discovery, key, check)
        errors += entry_errors
    for kind in ("tcp", "udp"):
        declared = f"{kind}_probe" in discovery
        if declared:
            errors += _validate_probe_block(file, kind, discovery[f"{kind}_probe"])
        normalized[f"has_{kind}_probe"] = declared
    normalized["has_python"] = "python" in discovery
    if normalized["has_python"]:
        errors += _validate_python_block(file, discovery["python"], yaml_dir=yaml_dir)
    return errors, normalized


def _hints(file: str, discovery: dict[str, Any]) -> tuple[list[str], dict[str, list[Any]]]:
    """The hints: snmp_pen and the four list hints. Returns their errors and each
    list hint's entries as declared."""
    errors = _snmp_pen_errors(file, discovery)
    hints: dict[str, list[Any]] = {}
    for key, check in _HINT_LISTS:
        hint_errors, hints[key] = _hint_list(file, discovery, key, check)
        errors += hint_errors
    return errors, hints


def _top_level_errors(file: str, discovery: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    unknown = set(discovery.keys()) - _KNOWN_DISCOVERY_KEYS
    if unknown:
        errors.append(
            f"{file}: discovery has unknown keys: {sorted(unknown)}. "
            f"Known keys: {sorted(_KNOWN_DISCOVERY_KEYS)}"
        )
    # ``requires`` is normally stamped by the catalog build at emission time
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
    return errors


def _declares_a_signal(
    discovery: dict[str, Any], fingerprints: dict[str, Any], hints: dict[str, list[Any]],
) -> bool:
    """Whether anything in the block can take part in matching.

    ``fingerprints`` is what ``_fingerprints`` recorded, every key of it: an
    entry kind counts only with a well-formed entry, a probe or a companion as
    soon as it is declared. So do ``snmp_pen`` and a list hint.
    """
    return (
        any(fingerprints.values())
        or "snmp_pen" in discovery
        or any(hints.values())
    )


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
    return any(
        isinstance(entry, dict) and bool(_declared(entry, _SSDP_FILTER_KEYS))
        for entry in _as_list(discovery.get("ssdp"))
    )


def _probe_declares(discovery: dict[str, Any], kind: str, key: str) -> bool:
    probe = discovery.get(f"{kind}_probe")
    return isinstance(probe, dict) and bool(_declared(probe, (key,)))


def _probe_has_follow_up(discovery: dict[str, Any]) -> bool:
    return _probe_declares(discovery, "tcp", "then")


def _udp_probe_has_reply_port(discovery: dict[str, Any]) -> bool:
    return _probe_declares(discovery, "udp", "reply_port")


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
    declared = discovery.get("requires")
    needed = declared
    for predicate, version in _DISCOVERY_FEATURE_GATES:
        if predicate(discovery) and _newer(version, needed):
            needed = version
    if needed is None or needed == declared:
        return discovery
    return {"requires": needed, **{
        k: v for k, v in discovery.items() if k != "requires"
    }}


def _newer(version: str, than: str | None) -> bool:
    """Whether a gate's ``version`` is newer than the ``requires`` so far
    (none yet counts as older; so does one that is not a version)."""
    if than is None:
        return True
    return (_version_tuple(version) or ()) > (_version_tuple(than) or ())
