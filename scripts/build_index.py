#!/usr/bin/env python3
"""Build openavc-drivers/index.json and devices.json from per-driver metadata.

Walks the driver directories, extracts metadata from each .avcdriver (YAML) and
.py (Python AST) file, validates against a strict schema, then writes:

  index.json             (driver catalog, monolithic)
  devices.json           (reverse-indexed device catalog, monolithic)
  index/<category>.json  (per-category driver shards, ready for sharded mode)
  devices/<category>.json (per-category device shards)

The driver file is the single source of truth. index.json is a build product:
DO NOT edit it by hand. Run this script after editing any driver file.

Usage:
  python scripts/build_index.py            # Build all outputs
  python scripts/build_index.py --check    # Validate only (used in CI)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


# --- Constants ---------------------------------------------------------------

GENERATOR_VERSION = "1.0.0"
SCHEMA_VERSION = "1"

DRIVER_CATEGORIES = (
    "projector", "display", "switcher", "audio", "camera",
    "video", "streaming", "lighting", "power", "utility",
)
DRIVER_TRANSPORTS = ("tcp", "udp", "http", "osc", "serial")
CONFIDENCE_VALUES = ("full", "partial", "untested")

DRIVER_DIRS = (
    "audio", "cameras", "displays", "lighting", "power",
    "projectors", "streaming", "switchers", "utility", "video",
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[\-+][\w.\-]+)?$")
ID_RE = re.compile(r"^[a-z0-9_]+$")
TAG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


# --- Pydantic models ---------------------------------------------------------


class HelpBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overview: str = Field(min_length=1)
    setup: str = Field(min_length=1)


class CompatibleModelsEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manufacturer: str
    models: list[str]
    confidence: str
    notes: str | None = None

    @field_validator("confidence")
    @classmethod
    def _confidence(cls, v: str) -> str:
        if v not in CONFIDENCE_VALUES:
            raise ValueError(f"must be one of {CONFIDENCE_VALUES}")
        return v

    @field_validator("models")
    @classmethod
    def _models(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("models list cannot be empty")
        for m in v:
            if not isinstance(m, str) or not m.strip():
                raise ValueError(f"each model must be a non-empty string, got {m!r}")
        return v


class DriverEntry(BaseModel):
    """Validated driver-index entry. Fields not listed here are dropped."""

    model_config = ConfigDict(extra="ignore")

    # Required (build-script enforced)
    id: str
    name: str
    manufacturer: str
    category: str
    version: str
    author: str
    transport: str
    description: str
    source_url: str

    # Identity (set by collector, derived from filesystem)
    file: str
    format: str  # "avcdriver" | "python"

    # Optional
    ports: list[int] = Field(default_factory=list)
    protocols: list[str] = Field(default_factory=list)
    simulated: bool = False
    verified: bool = False
    min_platform_version: str | None = None
    tags: list[str] = Field(default_factory=list)
    help: HelpBlock | None = None
    deprecated: bool = False
    replacement_id: str | None = None
    compatible_models: list[CompatibleModelsEntry] = Field(default_factory=list)
    # Phase 6 deterministic discovery hints. Carried through to index.json
    # so on-device discovery can match catalog entries before they're
    # installed — that's discovery's whole job.
    discovery: dict[str, Any] | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.fullmatch(v):
            raise ValueError("id must be lowercase alphanumeric with underscores")
        return v

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        if v not in DRIVER_CATEGORIES:
            raise ValueError(f"must be one of {DRIVER_CATEGORIES}")
        return v

    @field_validator("transport")
    @classmethod
    def _transport(cls, v: str) -> str:
        if v not in DRIVER_TRANSPORTS:
            raise ValueError(f"must be one of {DRIVER_TRANSPORTS}")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError("must be valid semver (e.g. 1.0.0)")
        return v

    @field_validator("min_platform_version")
    @classmethod
    def _min_platform_version(cls, v: str | None) -> str | None:
        if v is not None and not SEMVER_RE.match(v):
            raise ValueError("must be valid semver if present")
        return v

    @field_validator("source_url")
    @classmethod
    def _source_url(cls, v: str) -> str:
        if not URL_RE.match(v):
            raise ValueError("must start with http:// or https://")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        for t in v:
            if not TAG_RE.fullmatch(t):
                raise ValueError(
                    f"tag {t!r} must be lowercase, hyphen-separated, alphanumeric "
                    "(e.g. 'ndi', 'ceiling-mic')"
                )
        return v

    @field_validator("ports")
    @classmethod
    def _ports(cls, v: list[int]) -> list[int]:
        for p in v:
            if not isinstance(p, int) or isinstance(p, bool) or p < 1 or p > 65535:
                raise ValueError(f"port must be an integer 1–65535, got {p!r}")
        return v

    @field_validator("protocols")
    @classmethod
    def _protocols(cls, v: list[str]) -> list[str]:
        for p in v:
            if not isinstance(p, str) or not p.strip():
                raise ValueError(f"each protocol must be a non-empty string, got {p!r}")
        return v

    @model_validator(mode="after")
    def _deprecation_pair(self) -> "DriverEntry":
        if self.deprecated and not self.replacement_id:
            raise ValueError("deprecated drivers must set replacement_id")
        if self.replacement_id and not self.deprecated:
            raise ValueError("replacement_id only valid when deprecated=true")
        return self


# --- AST extraction (Python drivers) ----------------------------------------


class ExtractError(Exception):
    """Raised when a driver file's metadata cannot be extracted."""


def ast_to_python(node: Any, *, file: Path) -> Any:
    """Convert an AST node into a plain Python value.

    Accepts: literal scalars, dicts (with string keys), lists, tuples, and
    unary-negated numeric constants. Rejects everything else — function calls,
    comprehensions, f-strings, name references — because driver metadata must
    be static literal data.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        result: dict[str, Any] = {}
        for k, v in zip(node.keys, node.values):
            if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
                raise ExtractError(
                    f"{file.name}: dict keys must be string literals "
                    f"(found non-string key in DRIVER_INFO)"
                )
            result[k.value] = ast_to_python(v, file=file)
        return result
    if isinstance(node, (ast.List, ast.Tuple)):
        return [ast_to_python(item, file=file) for item in node.elts]
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return -node.operand.value
    raise ExtractError(
        f"{file.name}: DRIVER_INFO must contain only literal data "
        f"(no calls, comprehensions, or f-strings). "
        f"Offending node: {type(node).__name__}"
    )


def extract_python_driver_info(filepath: Path) -> dict[str, Any]:
    """Locate `DRIVER_INFO = {...}` inside a class body and extract index fields.

    Only keys in INDEX_FIELDS are extracted — runtime-only keys
    (`state_variables`, `commands`, `default_config`, `config_schema`, etc.)
    may contain non-literal expressions and are skipped. Index fields MUST be
    literal data; non-literal values there raise ExtractError.
    """
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise ExtractError(f"{filepath.name}: Python syntax error — {e}")

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if (
                    isinstance(item, ast.Assign)
                    and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)
                    and item.targets[0].id == "DRIVER_INFO"
                    and isinstance(item.value, ast.Dict)
                ):
                    return _extract_index_fields(item.value, file=filepath)
    raise ExtractError(
        f"{filepath.name}: no DRIVER_INFO class attribute found. "
        "Python drivers must define `DRIVER_INFO = {...}` inside a class body."
    )


def _extract_index_fields(node: ast.Dict, *, file: Path) -> dict[str, Any]:
    """Pull INDEX_FIELDS plus the ``discovery`` block out of a DRIVER_INFO dict literal.

    ``discovery`` is not part of INDEX_FIELDS (it does not ship in
    index.json) but the discovery validator needs it; AST extraction
    keeps it as a literal so cross-driver collision checks can run.
    """
    result: dict[str, Any] = {}
    extras = {"discovery"}
    for k, v in zip(node.keys, node.values):
        if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
            raise ExtractError(
                f"{file.name}: DRIVER_INFO keys must be string literals"
            )
        if k.value not in INDEX_FIELDS and k.value not in extras:
            continue  # Runtime field — leave alone, runtime parses it
        result[k.value] = ast_to_python(v, file=file)
    return result


# --- YAML extraction --------------------------------------------------------


def extract_yaml_driver_info(filepath: Path) -> dict[str, Any]:
    """Read top-level keys from a .avcdriver YAML file."""
    try:
        data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ExtractError(f"{filepath.name}: invalid YAML — {e}")
    if not isinstance(data, dict):
        raise ExtractError(f"{filepath.name}: top-level YAML must be a mapping")
    return data


# --- Collection -------------------------------------------------------------


def collect_drivers(repo_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Walk driver dirs and extract raw metadata. Does not validate.

    ``*_sim.py`` (Python simulator companions) and ``*_discovery.py``
    (Phase 9.7 discovery companions) are sibling helper files, not
    drivers — skipped here.
    """
    raw: list[tuple[Path, dict[str, Any]]] = []
    for dir_name in DRIVER_DIRS:
        dir_path = repo_root / dir_name
        if not dir_path.exists():
            continue
        for filepath in sorted(dir_path.iterdir()):
            if filepath.suffix == ".avcdriver":
                raw.append((filepath, extract_yaml_driver_info(filepath)))
            elif filepath.suffix == ".py" and not (
                filepath.name.endswith("_sim.py")
                or filepath.name.endswith("_discovery.py")
            ):
                raw.append((filepath, extract_python_driver_info(filepath)))
    return raw


# --- Discovery block validation ---------------------------------------------

# Phase 9 dropped the broadcast / active-probe allow-lists: the platform
# accepts unknown probe IDs as silent no-ops at runtime and driver-
# declared probes carry the wire format directly via ``udp_broadcast_probe:``
# / ``tcp_active_probe:``. CI no longer has a registry to gate against.

# Ports too generic to use as a Tier 4 soft enrichment signal — every web /
# admin / SSH device on the network would match. AV-specific ports are fine.
# Mirrors `DISALLOWED_OPEN_PORTS` in the platform's hints.py.
_DISALLOWED_OPEN_PORTS = frozenset({22, 80, 443})

# Phase 9: ports owned by built-in handlers — drivers declaring a
# ``udp_broadcast_probe`` / ``tcp_active_probe`` cannot collide on them.
# Mirrors `DISALLOWED_UDP_BROADCAST_PROBE_PORTS` /
# `DISALLOWED_TCP_ACTIVE_PROBE_PORTS` in the platform's hints.py.
_DISALLOWED_UDP_BROADCAST_PROBE_PORTS = frozenset({
    1900, 3702, 4352, 5353, 9131, 41794,
})
_DISALLOWED_TCP_ACTIVE_PROBE_PORTS = frozenset({
    23, 1515, 1688, 1710, 4352, 10500, 49280,
})
_MAX_PROBE_TIMEOUT_MS = 10000


def _validate_send_block(file: str, kind: str, raw: Any) -> list[str]:
    """Return validation errors for a probe ``send:`` block."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        errors.append(
            f"{file}: discovery.{kind}.send must be a mapping with exactly "
            "one of 'hex' or 'ascii'"
        )
        return errors
    has_hex = "hex" in raw and raw["hex"] is not None
    has_ascii = "ascii" in raw and raw["ascii"] is not None
    if has_hex and has_ascii:
        errors.append(
            f"{file}: discovery.{kind}.send must declare exactly one of "
            "'hex' or 'ascii', not both"
        )
    if not has_hex and not has_ascii:
        errors.append(
            f"{file}: discovery.{kind}.send must declare one of 'hex' or 'ascii'"
        )
    if has_hex:
        h = raw["hex"]
        if not isinstance(h, str):
            errors.append(f"{file}: discovery.{kind}.send.hex must be a string")
        else:
            try:
                bytes.fromhex(h.replace(" ", "").replace(":", ""))
            except ValueError as exc:
                errors.append(
                    f"{file}: discovery.{kind}.send.hex is not valid hex: {exc}"
                )
    if has_ascii and not isinstance(raw["ascii"], str):
        errors.append(f"{file}: discovery.{kind}.send.ascii must be a string")
    return errors


def _validate_response_match_block(file: str, kind: str, raw: Any) -> list[str]:
    """Return validation errors for a ``response_match:`` block."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        errors.append(
            f"{file}: discovery.{kind}.response_match must be a mapping (at "
            "least one of starts_with_hex, contains, regex)"
        )
        return errors
    have_any = False
    if "starts_with_hex" in raw and raw["starts_with_hex"] is not None:
        s = raw["starts_with_hex"]
        if not isinstance(s, str):
            errors.append(
                f"{file}: discovery.{kind}.response_match.starts_with_hex "
                "must be a string"
            )
        else:
            have_any = True
            try:
                bytes.fromhex(s.replace(" ", "").replace(":", ""))
            except ValueError as exc:
                errors.append(
                    f"{file}: discovery.{kind}.response_match.starts_with_hex "
                    f"is not valid hex: {exc}"
                )
    if "contains" in raw and raw["contains"] is not None:
        c = raw["contains"]
        if not isinstance(c, str) or not c:
            errors.append(
                f"{file}: discovery.{kind}.response_match.contains must be a "
                "non-empty string"
            )
        else:
            have_any = True
    if "regex" in raw and raw["regex"] is not None:
        r = raw["regex"]
        if not isinstance(r, str) or not r:
            errors.append(
                f"{file}: discovery.{kind}.response_match.regex must be a "
                "non-empty string"
            )
        else:
            have_any = True
            try:
                re.compile(r)
            except re.error as exc:
                errors.append(
                    f"{file}: discovery.{kind}.response_match.regex failed "
                    f"to compile: {exc}"
                )
    if not have_any:
        errors.append(
            f"{file}: discovery.{kind}.response_match needs at least one of "
            "starts_with_hex, contains, regex"
        )
    return errors


def _validate_extract_block(file: str, kind: str, raw: Any) -> list[str]:
    """Return validation errors for an ``extract:`` block."""
    errors: list[str] = []
    if raw is None:
        return errors
    if not isinstance(raw, dict):
        errors.append(
            f"{file}: discovery.{kind}.extract must be a mapping of field "
            "name to literal string or {regex, group} mapping"
        )
        return errors
    for name, spec in raw.items():
        if not isinstance(name, str) or not name:
            errors.append(
                f"{file}: discovery.{kind}.extract field names must be "
                "non-empty strings"
            )
            continue
        if isinstance(spec, str):
            continue
        if isinstance(spec, dict):
            pat = spec.get("regex")
            if not isinstance(pat, str) or not pat:
                errors.append(
                    f"{file}: discovery.{kind}.extract.{name} mapping requires "
                    "a non-empty 'regex' string"
                )
            else:
                try:
                    re.compile(pat)
                except re.error as exc:
                    errors.append(
                        f"{file}: discovery.{kind}.extract.{name}.regex failed "
                        f"to compile: {exc}"
                    )
            grp = spec.get("group", 1)
            if not isinstance(grp, int) or isinstance(grp, bool) or grp < 0:
                errors.append(
                    f"{file}: discovery.{kind}.extract.{name}.group must be a "
                    "non-negative integer"
                )
            continue
        errors.append(
            f"{file}: discovery.{kind}.extract.{name} must be a literal "
            "string or a {regex, group} mapping"
        )
    return errors


def _validate_custom_probe_block(
    file: str,
    kind: str,                      # "udp_broadcast_probe" | "tcp_active_probe"
    raw: Any,
    disallowed_ports: frozenset[int],
) -> list[str]:
    """Return validation errors for one custom probe block."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        errors.append(f"{file}: discovery.{kind} must be a mapping")
        return errors

    port = raw.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        errors.append(
            f"{file}: discovery.{kind}.port must be an integer in [1, 65535]"
        )
    elif port in disallowed_ports:
        errors.append(
            f"{file}: discovery.{kind}.port {port} is reserved for a built-in "
            f"handler. Use the named opt-in instead. "
            f"Disallowed: {sorted(disallowed_ports)}"
        )

    errors.extend(_validate_send_block(file, kind, raw.get("send")))
    errors.extend(_validate_response_match_block(file, kind, raw.get("response_match")))
    errors.extend(_validate_extract_block(file, kind, raw.get("extract")))

    timeout_ms = raw.get("timeout_ms")
    if timeout_ms is not None:
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 1:
            errors.append(
                f"{file}: discovery.{kind}.timeout_ms must be a positive integer"
            )
        elif timeout_ms > _MAX_PROBE_TIMEOUT_MS:
            errors.append(
                f"{file}: discovery.{kind}.timeout_ms exceeds the max of "
                f"{_MAX_PROBE_TIMEOUT_MS} ms"
            )

    if "generic" in raw and not isinstance(raw["generic"], bool):
        errors.append(f"{file}: discovery.{kind}.generic must be a bool")

    return errors


def _validate_discovery_block(file: str, raw: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Return (errors, normalized_discovery) for one driver.

    Mirrors ``parse_driver_discovery`` in the platform. Drivers whose IDs
    start with ``generic_`` are exempt from the strong-signal requirement.
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

    manual_only = bool(discovery.get("manual_only", False))
    normalized["manual_only"] = manual_only

    mdns = discovery.get("mdns_services") or []
    if not isinstance(mdns, list):
        errors.append(f"{file}: discovery.mdns_services must be a list")
        mdns = []
    normalized_mdns: list[dict[str, Any]] = []
    for entry in mdns:
        if isinstance(entry, str):
            normalized_mdns.append({"service": entry, "txt_match": {}})
        elif isinstance(entry, dict) and isinstance(entry.get("service"), str):
            normalized_mdns.append({
                "service": entry["service"],
                "txt_match": {str(k): str(v) for k, v in (entry.get("txt_match") or {}).items()},
            })
        else:
            errors.append(
                f"{file}: discovery.mdns_services entries must be strings or "
                f"{{service, txt_match}} mappings"
            )
    normalized["mdns_services"] = normalized_mdns

    ssdp = discovery.get("ssdp_device_types") or []
    if not isinstance(ssdp, list) or not all(isinstance(s, str) for s in ssdp):
        errors.append(f"{file}: discovery.ssdp_device_types must be a list of strings")
        ssdp = []
    normalized["ssdp_device_types"] = list(ssdp)

    amx = discovery.get("amx_ddp")
    if amx is not None:
        if not isinstance(amx, dict) or not isinstance(amx.get("make"), str) or not amx["make"]:
            errors.append(f"{file}: discovery.amx_ddp.make is required")
            amx = None
        else:
            amx = {"make": amx["make"], "model_pattern": str(amx.get("model_pattern", "*"))}
    normalized["amx_ddp"] = amx

    broadcast: list[tuple[str, str | None]] = []
    # ONVIF is the only remaining built-in Tier 2 named opt-in. PJLink
    # Class 2 + Crestron CIP discovery now ship as ``_discovery.py``
    # companions on their respective drivers (Phase 9.7).
    if "onvif" in discovery:
        onvif_block = discovery["onvif"]
        if onvif_block is True:
            broadcast.append(("onvif", None))
        elif isinstance(onvif_block, dict):
            mfg = onvif_block.get("manufacturer")
            broadcast.append(("onvif", str(mfg) if mfg else None))
        elif onvif_block is not False and onvif_block is not None:
            errors.append(f"{file}: discovery.onvif must be a bool or {{manufacturer: ...}} mapping")
    normalized["broadcast"] = broadcast

    active: list[str] = []
    raw_probes = discovery.get("active_probes") or []
    if not isinstance(raw_probes, list):
        errors.append(f"{file}: discovery.active_probes must be a list")
        raw_probes = []
    for entry in raw_probes:
        if isinstance(entry, str):
            probe_id = entry
        elif isinstance(entry, dict) and isinstance(entry.get("probe"), str):
            probe_id = entry["probe"]
        else:
            errors.append(f"{file}: discovery.active_probes entry malformed")
            continue
        active.append(probe_id)
    normalized["active_probes"] = active

    # Phase 9: driver-declared probe blocks. Both optional.
    has_udp_probe = False
    has_tcp_probe = False
    if "udp_broadcast_probe" in discovery:
        errors.extend(_validate_custom_probe_block(
            file,
            "udp_broadcast_probe",
            discovery["udp_broadcast_probe"],
            _DISALLOWED_UDP_BROADCAST_PROBE_PORTS,
        ))
        has_udp_probe = True
    if "tcp_active_probe" in discovery:
        errors.extend(_validate_custom_probe_block(
            file,
            "tcp_active_probe",
            discovery["tcp_active_probe"],
            _DISALLOWED_TCP_ACTIVE_PROBE_PORTS,
        ))
        has_tcp_probe = True
    normalized["has_udp_broadcast_probe"] = has_udp_probe
    normalized["has_tcp_active_probe"] = has_tcp_probe

    # Phase 9.7: companion declaration. Mirrors parse_driver_discovery.
    has_companion = False
    companion_generic = False
    if "companion" in discovery:
        comp_block = discovery["companion"]
        if not isinstance(comp_block, dict):
            errors.append(
                f"{file}: discovery.companion must be a mapping "
                f"(use ``companion: {{generic: bool}}``)"
            )
        else:
            unknown = set(comp_block.keys()) - {"generic"}
            if unknown:
                errors.append(
                    f"{file}: discovery.companion has unknown keys: "
                    f"{sorted(unknown)}. Only ``generic`` is supported."
                )
            generic_raw = comp_block.get("generic", False)
            if not isinstance(generic_raw, bool):
                errors.append(
                    f"{file}: discovery.companion.generic must be a bool"
                )
            else:
                has_companion = True
                companion_generic = generic_raw
    normalized["has_companion"] = has_companion
    normalized["companion_generic"] = companion_generic

    if "snmp_pen" in discovery:
        pen = discovery["snmp_pen"]
        if not isinstance(pen, int) or isinstance(pen, bool) or pen < 1:
            errors.append(f"{file}: discovery.snmp_pen must be a positive integer")

    raw_ports = discovery.get("open_ports") or []
    if not isinstance(raw_ports, list):
        errors.append(f"{file}: discovery.open_ports must be a list")
        raw_ports = []
    for port in raw_ports:
        if not isinstance(port, int) or isinstance(port, bool):
            errors.append(
                f"{file}: discovery.open_ports entries must be integers, got {port!r}"
            )
            continue
        if port < 1 or port > 65535:
            errors.append(
                f"{file}: discovery.open_ports entry {port} out of range [1, 65535]"
            )
            continue
        if port in _DISALLOWED_OPEN_PORTS:
            errors.append(
                f"{file}: discovery.open_ports entry {port} is disallowed "
                f"(too generic — would match every web/SSH device). "
                f"Disallowed: {sorted(_DISALLOWED_OPEN_PORTS)}"
            )

    # oui_prefixes: list of non-empty strings. Mirrors platform's
    # parse_driver_discovery validation in hints.py.
    raw_oui = discovery.get("oui_prefixes") or []
    if not isinstance(raw_oui, list):
        errors.append(f"{file}: discovery.oui_prefixes must be a list")
        raw_oui = []
    for prefix in raw_oui:
        if not isinstance(prefix, str) or not prefix:
            errors.append(
                f"{file}: discovery.oui_prefixes entries must be non-empty "
                f"strings, got {prefix!r}"
            )

    # hostname_patterns: list of non-empty strings, each must compile as
    # a regex (platform's SignalIndex.add_rule compiles with re.IGNORECASE
    # and raises ValueError on failure — mirror that here so a bad regex
    # fails CI, not the platform at load time).
    raw_host = discovery.get("hostname_patterns") or []
    if not isinstance(raw_host, list):
        errors.append(f"{file}: discovery.hostname_patterns must be a list")
        raw_host = []
    for pattern in raw_host:
        if not isinstance(pattern, str) or not pattern:
            errors.append(
                f"{file}: discovery.hostname_patterns entries must be "
                f"non-empty strings, got {pattern!r}"
            )
            continue
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            errors.append(
                f"{file}: discovery.hostname_patterns entry {pattern!r} "
                f"failed to compile: {exc}"
            )

    # Phase 8.6: vendor_aliases are manufacturer/make strings the driver
    # claims when a strong-tier probe response carries that field.
    raw_aliases = discovery.get("vendor_aliases") or []
    if not isinstance(raw_aliases, list):
        errors.append(f"{file}: discovery.vendor_aliases must be a list")
        raw_aliases = []
    for alias in raw_aliases:
        if not isinstance(alias, str):
            errors.append(
                f"{file}: discovery.vendor_aliases entries must be strings, "
                f"got {alias!r}"
            )
            continue
        if not alias.strip():
            errors.append(
                f"{file}: discovery.vendor_aliases entries must be non-empty"
            )

    # Phase 8 Task 8.3: any combination of strong + soft signals is valid.
    # Declaring no signals at all is a no-op (the matcher silently ignores
    # the driver) but no longer a hard error. We surface it as a warning
    # via stderr so contributors notice, while letting CI succeed.
    has_any_signal = (
        bool(normalized_mdns)
        or bool(ssdp)
        or amx is not None
        or bool(broadcast)
        or bool(active)
        or has_udp_probe
        or has_tcp_probe
        or has_companion
        or "snmp_pen" in discovery
        or bool(discovery.get("oui_prefixes"))
        or bool(discovery.get("hostname_patterns"))
        or bool(raw_ports)
        or bool(raw_aliases)
    )
    if not has_any_signal and not manual_only:
        sys.stderr.write(
            f"warning: {file}: discovery block declares no signals "
            "(strong or soft); this driver will never participate in "
            "matching. Add oui_prefixes, hostname_patterns, open_ports, "
            "vendor_aliases, or a Tier 1/2/3 signal — or set "
            "manual_only: true.\n"
        )
    return errors, normalized


def _validate_no_signal_collisions(
    per_driver: list[tuple[str, str, dict[str, Any]]],
) -> list[str]:
    """Cross-driver: refuse two drivers claiming the same strong signal."""
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
        if normalized.get("manual_only"):
            continue
        for entry in normalized.get("mdns_services", []):
            claim("mdns", entry["service"].lower().rstrip(".") + ".",
                  driver_id, file, entry.get("txt_match"))
        for st in normalized.get("ssdp_device_types", []):
            claim("ssdp", st, driver_id, file, None)
        amx = normalized.get("amx_ddp")
        if amx:
            claim("amx_ddp", f"{amx['make']}/{amx['model_pattern']}",
                  driver_id, file, None)
        for probe_id, mfg in normalized.get("broadcast", []):
            claim("broadcast", probe_id, driver_id, file,
                  {"manufacturer": mfg} if mfg else None)
        for probe_id in normalized.get("active_probes", []):
            claim("probe", probe_id, driver_id, file, None)

    return errors


# --- Index field selection --------------------------------------------------

# The driver file may carry many fields the runtime needs (transport config,
# commands, state_variables, default_config, config_schema, discovery, etc.).
# Only these go into index.json.
INDEX_FIELDS = frozenset({
    "id", "name", "manufacturer", "category", "version", "author",
    "transport", "description", "source_url",
    "ports", "protocols", "simulated", "verified", "min_platform_version",
    "tags", "help", "deprecated", "replacement_id", "compatible_models",
    "discovery",
})


def build_entry(filepath: Path, raw: dict[str, Any], repo_root: Path) -> DriverEntry:
    subset = {k: v for k, v in raw.items() if k in INDEX_FIELDS}
    subset["file"] = filepath.relative_to(repo_root).as_posix()
    subset["format"] = "avcdriver" if filepath.suffix == ".avcdriver" else "python"
    return DriverEntry(**subset)


# --- Cross-driver validation ------------------------------------------------


def cross_validate(entries: list[DriverEntry], manufacturers: set[str]) -> list[str]:
    errors: list[str] = []

    # ID uniqueness
    seen_ids: dict[str, str] = {}
    for e in entries:
        if e.id in seen_ids:
            errors.append(
                f"duplicate driver id {e.id!r}: {seen_ids[e.id]} vs {e.file}"
            )
        seen_ids[e.id] = e.file

    # Driver-level manufacturer in canonical list
    for e in entries:
        if e.manufacturer not in manufacturers:
            errors.append(
                f"{e.file}: manufacturer {e.manufacturer!r} not in manufacturers.json. "
                f"Add it there first if it's legitimately new."
            )

    # compatible_models manufacturer in canonical list
    for e in entries:
        for cm in e.compatible_models:
            if cm.manufacturer not in manufacturers:
                errors.append(
                    f"{e.file}: compatible_models manufacturer {cm.manufacturer!r} "
                    f"not in manufacturers.json"
                )

    # replacement_id must resolve
    ids = {e.id for e in entries}
    for e in entries:
        if e.replacement_id and e.replacement_id not in ids:
            errors.append(
                f"{e.file}: replacement_id {e.replacement_id!r} does not match any driver"
            )

    # No two drivers claim full for the same (manufacturer, model) without notes
    seen_full: dict[tuple[str, str], tuple[str, bool]] = {}
    for e in entries:
        for cm in e.compatible_models:
            if cm.confidence != "full":
                continue
            for model in cm.models:
                key = (cm.manufacturer, model)
                if key in seen_full:
                    prior_file, prior_has_notes = seen_full[key]
                    if not (prior_has_notes and cm.notes):
                        errors.append(
                            f"{e.file}: claims full support for "
                            f"{cm.manufacturer} {model}, but {prior_file} already does. "
                            f"Add a `notes:` field on both entries justifying the overlap."
                        )
                seen_full[key] = (e.file, bool(cm.notes))

    return errors


# --- Devices catalog --------------------------------------------------------


CONFIDENCE_ORDER = {"full": 0, "partial": 1, "untested": 2}


def build_devices(
    entries: list[DriverEntry],
    devices_extra: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reverse-index `compatible_models` into a device catalog."""
    devices: dict[tuple[str, str], dict[str, Any]] = {}

    for e in entries:
        for cm in e.compatible_models:
            for model in cm.models:
                key = (cm.manufacturer, model)
                entry = devices.setdefault(
                    key,
                    {
                        "manufacturer": cm.manufacturer,
                        "model": model,
                        "category": e.category,
                        "drivers": [],
                    },
                )
                drv: dict[str, Any] = {"id": e.id, "confidence": cm.confidence}
                if cm.notes:
                    drv["notes"] = cm.notes
                entry["drivers"].append(drv)

    # Stable per-device driver order: confidence first, then id
    for entry in devices.values():
        entry["drivers"].sort(key=lambda d: (CONFIDENCE_ORDER.get(d["confidence"], 9), d["id"]))

    # Merge devices-extra (catalog gaps not produced by any driver)
    for extra in devices_extra:
        mfr = extra.get("manufacturer", "")
        model = extra.get("model", "")
        key = (mfr, model)
        if key in devices:
            raise ValueError(
                f"devices-extra.json: ({mfr}, {model}) is also produced from a "
                f"driver's compatible_models. Move the entry into the driver."
            )
        devices[key] = extra

    return sorted(devices.values(), key=lambda d: (d["manufacturer"].lower(), d["model"].lower()))


# --- Output ----------------------------------------------------------------


def _entry_dict(entry: DriverEntry) -> dict[str, Any]:
    """Tidy dict for JSON output: drop None and default-empty fields."""
    d = entry.model_dump(exclude_none=True)
    for key in ("ports", "protocols", "tags", "compatible_models"):
        if d.get(key) == []:
            d.pop(key)
    for key in ("simulated", "verified", "deprecated"):
        if d.get(key) is False:
            d.pop(key, None)
    return d


def _meta_block(now: str, total_key: str, total: int, **extra: Any) -> dict[str, Any]:
    block = {
        "generated_at": now,
        "generator_version": GENERATOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        total_key: total,
        "shards": None,
    }
    block.update(extra)
    return block


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def write_outputs(
    repo_root: Path,
    entries: list[DriverEntry],
    devices: list[dict[str, Any]],
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    sorted_entries = sorted(
        entries, key=lambda e: (e.manufacturer.lower(), e.name.lower())
    )
    entry_dicts = [_entry_dict(e) for e in sorted_entries]

    # index.json
    _write_json(
        repo_root / "index.json",
        {
            "_meta": _meta_block(now, "total_drivers", len(entry_dicts)),
            "drivers": entry_dicts,
        },
    )

    # devices.json
    _write_json(
        repo_root / "devices.json",
        {
            "_meta": _meta_block(now, "total_devices", len(devices)),
            "devices": devices,
        },
    )

    # Per-category driver shards
    for cat in DRIVER_CATEGORIES:
        shard = [d for d in entry_dicts if d["category"] == cat]
        _write_json(
            repo_root / "index" / f"{cat}.json",
            {
                "_meta": _meta_block(
                    now, "total_drivers", len(shard), category=cat
                ),
                "drivers": shard,
            },
        )

    # Per-category device shards
    for cat in DRIVER_CATEGORIES:
        shard = [d for d in devices if d.get("category") == cat]
        _write_json(
            repo_root / "devices" / f"{cat}.json",
            {
                "_meta": _meta_block(
                    now, "total_devices", len(shard), category=cat
                ),
                "devices": shard,
            },
        )


# --- Main ------------------------------------------------------------------


def _load_manufacturers(repo_root: Path) -> set[str]:
    f = repo_root / "manufacturers.json"
    if not f.exists():
        raise FileNotFoundError(f"{f} not found — required for cross-driver validation")
    data = json.loads(f.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("manufacturers.json must be a JSON array of strings")
    return set(data)


def _load_devices_extra(repo_root: Path) -> list[dict[str, Any]]:
    f = repo_root / "devices-extra.json"
    if not f.exists():
        return []
    data = json.loads(f.read_text(encoding="utf-8"))
    return data.get("devices", []) if isinstance(data, dict) else []


def _format_validation_errors(file: str, exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "<root>"
        out.append(f"{file}: {loc}: {err['msg']}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate only — do not write outputs (used in CI)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root (default: parent of scripts/)",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.root.resolve()

    try:
        manufacturers = _load_manufacturers(repo_root)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    devices_extra = _load_devices_extra(repo_root)

    try:
        raw = collect_drivers(repo_root)
    except ExtractError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not raw:
        print(f"ERROR: no driver files found under {repo_root}", file=sys.stderr)
        return 1

    entries: list[DriverEntry] = []
    errors: list[str] = []
    discovery_per_driver: list[tuple[str, str, dict[str, Any]]] = []
    for filepath, data in raw:
        rel = filepath.relative_to(repo_root).as_posix()
        try:
            entries.append(build_entry(filepath, data, repo_root))
        except ValidationError as e:
            errors.extend(_format_validation_errors(rel, e))
        except Exception as e:
            errors.append(f"{rel}: {e}")

        disc_errors, normalized = _validate_discovery_block(rel, data)
        errors.extend(disc_errors)
        if normalized:
            discovery_per_driver.append((str(data.get("id") or ""), rel, normalized))

    if entries:
        errors.extend(cross_validate(entries, manufacturers))
    errors.extend(_validate_no_signal_collisions(discovery_per_driver))

    if errors:
        print(f"\nFAILED: {len(errors)} validation error(s):\n", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    try:
        devices = build_devices(entries, devices_extra)
    except ValueError as e:
        print(f"ERROR building devices catalog: {e}", file=sys.stderr)
        return 1

    print(f"Validated {len(entries)} driver(s), {len(devices)} device(s).")

    if args.check:
        return 0

    write_outputs(repo_root, entries, devices)
    print(
        f"Wrote index.json, devices.json, and "
        f"{len(DRIVER_CATEGORIES) * 2} category shard files."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
