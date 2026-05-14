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
    # Discovery declaration (fingerprints + hints, new schema). Carried
    # through to index.json so on-device discovery can match catalog
    # entries before they're installed — that's discovery's whole job.
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
    (Python discovery companions) are sibling helper files, not
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
#
# Mirrors ``parse_driver_discovery`` in the platform's hints.py. Schema
# reference: ``discovery-rewrite-plan.md`` (workspace root).

# Ports too generic to use as a hint — every web / admin / SSH device on
# the network would match. AV-specific ports are fine. Mirrors
# ``DISALLOWED_OPEN_PORTS`` in the platform. 8000 / 8080 / 8443 / 8888
# are admin-UI alternates with the same false-positive class as 80/443.
_DISALLOWED_OPEN_PORTS = frozenset({22, 80, 443, 8000, 8080, 8443, 8888})

_MAX_PROBE_TIMEOUT_MS = 10000

_KNOWN_DISCOVERY_KEYS: frozenset[str] = frozenset({
    "mdns", "ssdp", "amx_ddp",
    "tcp_probe", "udp_probe", "python",
    "oui", "hostname", "port_open", "manufacturer_alias", "snmp_pen",
})

_KNOWN_PROBE_KEYS: frozenset[str] = frozenset({
    "port", "send_hex", "send_ascii",
    "expect", "expect_regex", "expect_hex",
    "cross_vendor", "timeout_ms",
    "extract", "extract_manufacturer",
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


def _validate_ssdp_entry(file: str, raw: Any) -> tuple[list[str], str | None]:
    """Validate one SSDP fingerprint entry. Returns (errors, normalized device_type)."""
    if isinstance(raw, str):
        if not raw:
            return ([f"{file}: discovery.ssdp device_type must be a non-empty string"], None)
        return ([], raw)
    if not isinstance(raw, dict):
        return (
            [f"{file}: discovery.ssdp entries must be strings or "
             "{device_type, cross_vendor} mappings"],
            None,
        )
    errors: list[str] = []
    unknown = set(raw.keys()) - {"device_type", "cross_vendor"}
    if unknown:
        errors.append(
            f"{file}: discovery.ssdp entry has unknown keys: {sorted(unknown)}"
        )
    dt = raw.get("device_type")
    if not isinstance(dt, str) or not dt:
        errors.append(f"{file}: discovery.ssdp.device_type must be a non-empty string")
        return (errors, None)
    if "cross_vendor" in raw and not isinstance(raw["cross_vendor"], bool):
        errors.append(f"{file}: discovery.ssdp.cross_vendor must be a bool")
    return (errors, dt)


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


def _validate_discovery_block(
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

    # --- Fingerprints -----------------------------------------------------

    normalized_mdns: list[dict[str, Any]] = []
    if "mdns" in discovery:
        for entry in _as_list(discovery["mdns"]):
            entry_errors, entry_norm = _validate_mdns_entry(file, entry)
            errors.extend(entry_errors)
            if entry_norm is not None:
                normalized_mdns.append(entry_norm)
    normalized["mdns"] = normalized_mdns

    normalized_ssdp: list[str] = []
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


def _validate_no_signal_collisions(
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
        for dt in normalized.get("ssdp", []):
            claim("ssdp", dt, driver_id, file, None)
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

        # The runtime sources poll cadence from `default_config.poll_interval`
        # only — a top-level `polling.interval` has never been honored and was
        # stripped from the fleet on 2026-05-13 (backlog §19). Reject it here
        # so future contributions can't reintroduce a field that does nothing.
        polling = data.get("polling")
        if isinstance(polling, dict) and "interval" in polling:
            errors.append(
                f"{rel}: top-level `polling.interval` is inert — the runtime "
                f"reads only `default_config.poll_interval`. Remove the "
                f"`interval:` key from the `polling:` block; set the cadence "
                f"via `default_config.poll_interval` instead."
            )

        disc_errors, normalized = _validate_discovery_block(
            rel, data, yaml_dir=filepath.parent,
        )
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
