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
from typing import TYPE_CHECKING
import argparse
import ast
import copy
import json
import re
import sys
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
try:
    from jsonschema_rs import validator_for as jsonschema_validator_for
except ModuleNotFoundError:
    jsonschema_validator_for = None

# We want to import these for type checks, but `JsonSchemaValidationError`
# is needed at runtime so we redefine it as BaseException.
#
# This ensures that the script can run with or without jsonschema_rs, but we
# still get proper types when it's available.
if TYPE_CHECKING:
    from jsonschema_rs import (
        Validator as JsonSchemaValidator,
        ValidationError as JsonSchemaValidationError,
    )
else:
    JsonSchemaValidator = object
    JsonSchemaValidationError = BaseException

# --- Constants ---------------------------------------------------------------

GENERATOR_VERSION = "1.0.0"
SCHEMA_VERSION = "1"

DRIVER_CATEGORIES = (
    "projector", "display", "switcher", "audio", "camera",
    "video", "streaming", "lighting", "power", "utility",
)
# "ssh" and "mqtt" are Python-driver-only transports (the platform SSH CLI and
# MQTT pub/sub transports). Declarative .avcdriver/YAML drivers can't drive
# either, so the YAML schema (avcdriver.schema.json) intentionally omits them.
DRIVER_TRANSPORTS = ("tcp", "udp", "http", "osc", "serial", "ssh", "mqtt")
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
    # Optional troubleshooting hint shown on the device's offline banner.
    connection: str | None = Field(default=None, min_length=1)


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
    "requires",
    "mdns", "ssdp", "amx_ddp",
    "tcp_probe", "udp_probe", "python",
    "oui", "hostname", "port_open", "manufacturer_alias", "snmp_pen",
})

_KNOWN_PROBE_KEYS: frozenset[str] = frozenset({
    "port", "send_hex", "send_ascii",
    "expect", "expect_regex", "expect_hex",
    "cross_vendor", "timeout_ms", "tls",
    "extract", "extract_manufacturer",
})


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _validate_json_schema(
    filepath: str,
    driver_info: dict[str, Any],
    validator: JsonSchemaValidator
) -> list[str]:
    """Validate driver_info against the JSON Schema.

    Returns a list of errors.
    """
    errors: list[str] = []
    try:
        validator.validate(driver_info)
    except JsonSchemaValidationError as e:
        errors.append(f"{filepath}: JSON Schema validation error: {e.message}")
    return errors


def _python_driver_schema(base_schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the avcdriver schema that also allows Python-only transports.

    avcdriver.schema.json describes the YAML (.avcdriver) format, whose
    transport enum deliberately omits ssh and mqtt: a declarative driver can't
    drive an SSH CLI session or MQTT pub/sub. Python drivers can, and the
    runtime allows it, so they are validated against the same schema with ssh
    and mqtt added to the transport enum. Everything else stays identical, so a
    Python driver's metadata is checked just as strictly as a YAML driver's.
    """
    schema = copy.deepcopy(base_schema)
    transport = schema.get("properties", {}).get("transport", {})
    enum = transport.get("enum")
    if isinstance(enum, list):
        transport["enum"] = [*enum, *(t for t in ("ssh", "mqtt") if t not in enum)]
    return schema


def _validate_all_json_schemas(
    raw: list[tuple[Path, dict[str, Any]]],
    yaml_validator: JsonSchemaValidator,
    python_validator: JsonSchemaValidator,
) -> list[str]:
    """Validate each driver_info against the JSON Schema for its format.

    .avcdriver (YAML) drivers use the schema as published; .py (Python) drivers
    use the variant from _python_driver_schema that also allows the ssh
    transport.

    Returns a list of errors.
    """
    errors: list[str] = []
    for filepath, driver_info in raw:
        validator = python_validator if filepath.suffix == ".py" else yaml_validator
        errors.extend(_validate_json_schema(filepath.as_posix(), driver_info, validator))
    return errors


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


# Catastrophic-backtracking detection. Mirrors the platform runtime validator
# (server/drivers/driver_loader.py); kept self-contained (stdlib only) because
# this script runs in CI without an openavc install.
_NESTED_QUANT_RE = re.compile(r"\((?:\\.|\[[^\]]*\]|[^()\[\]\\*+?])[*+]\)[*+]")
_ALT_QUANT_RE = re.compile(r"\(([^()]*\|[^()]*)\)[*+]")


def _regex_backtracking_reason(pattern: str) -> str | None:
    """Return why a regex risks catastrophic backtracking, or None if it looks safe."""
    if _NESTED_QUANT_RE.search(pattern):
        return "nested quantifier"
    for m in _ALT_QUANT_RE.finditer(pattern):
        alts = m.group(1).split("|")
        if len(set(alts)) != len(alts):
            return "duplicate alternatives under a quantifier"
        for a in alts:
            for b in alts:
                if a and a != b and b.startswith(a):
                    return "overlapping alternatives under a quantifier"
    return None


def _validate_auth_block(file: str, data: dict[str, Any]) -> list[str]:
    """Validate a driver's optional `auth:` login handshake.

    Mirrors the runtime rules in driver_loader.validate_driver_definition:
    telnet_login only, tcp/serial only, both prompts required, and all four
    prompt/pattern regexes compile and don't risk catastrophic backtracking
    (they run synchronously on raw pre-auth device bytes).
    """
    errors: list[str] = []
    auth = data.get("auth")
    if auth is None:
        return errors
    if not isinstance(auth, dict):
        errors.append(f"{file}: auth must be a mapping")
        return errors

    atype = auth.get("type", "telnet_login")
    if atype != "telnet_login":
        errors.append(f"{file}: auth.type '{atype}' is unsupported (only telnet_login)")

    transport = data.get("transport", "")
    if transport and transport not in ("tcp", "serial"):
        errors.append(
            f"{file}: auth login handshake is only valid on tcp/serial "
            f"transports, not '{transport}'"
        )

    for required in ("username_prompt", "password_prompt"):
        if not auth.get(required):
            errors.append(f"{file}: auth missing required '{required}'")

    for key in ("username_prompt", "password_prompt", "success_pattern", "failure_pattern"):
        pat = auth.get(key)
        if not pat:
            continue
        if not isinstance(pat, str):
            errors.append(f"{file}: auth.{key} must be a string")
            continue
        try:
            re.compile(pat)
        except re.error as exc:
            errors.append(f"{file}: auth.{key} failed to compile: {exc}")
            continue
        reason = _regex_backtracking_reason(pat)
        if reason:
            errors.append(
                f"{file}: auth.{key} has a {reason} that can cause catastrophic "
                f"backtracking against pre-auth device bytes"
            )
    return errors


def _validate_liveness_block(file: str, data: dict[str, Any]) -> list[str]:
    """Validate a driver's optional `liveness:` dead-link watchdog.

    Mirrors the runtime rules in driver_loader.validate_driver_definition:
    tcp/serial/udp/osc only, `send` required, `expect` compiles without
    catastrophic backtracking, interval >= 1 / timeout >= 0.1 /
    max_failures >= 1, and `args` only on osc.
    """
    errors: list[str] = []
    liveness = data.get("liveness")
    if liveness is None:
        return errors
    if not isinstance(liveness, dict):
        errors.append(f"{file}: liveness must be a mapping")
        return errors

    transport = data.get("transport", "")
    if transport and transport not in ("tcp", "serial", "udp", "osc"):
        errors.append(
            f"{file}: liveness is not supported on transport '{transport}' "
            f"(only tcp/serial/udp/osc)"
        )

    send = liveness.get("send")
    if not isinstance(send, str) or not send:
        errors.append(f"{file}: liveness missing required 'send'")

    expect = liveness.get("expect")
    if expect is not None:
        if not isinstance(expect, str) or not expect:
            errors.append(f"{file}: liveness.expect must be a regex string")
        else:
            try:
                re.compile(expect)
            except re.error as exc:
                errors.append(f"{file}: liveness.expect failed to compile: {exc}")
            else:
                reason = _regex_backtracking_reason(expect)
                if reason:
                    errors.append(
                        f"{file}: liveness.expect has a {reason} that can cause "
                        f"catastrophic backtracking against raw device bytes"
                    )

    for key, minimum in (("interval", 1.0), ("timeout", 0.1)):
        value = liveness.get(key)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < minimum
        ):
            errors.append(f"{file}: liveness.{key} must be a number >= {minimum}")

    max_failures = liveness.get("max_failures")
    if max_failures is not None and (
        not isinstance(max_failures, int)
        or isinstance(max_failures, bool)
        or max_failures < 1
    ):
        errors.append(f"{file}: liveness.max_failures must be an integer >= 1")

    if liveness.get("args") is not None:
        if transport != "osc":
            errors.append(f"{file}: liveness.args is only valid on osc")
        elif not isinstance(liveness["args"], list):
            errors.append(f"{file}: liveness.args must be a list")
    return errors


def _validate_push_block(file: str, data: dict[str, Any]) -> list[str]:
    """Validate a driver's optional `push:` block (device-initiated
    notifications, platform >= 0.23.0).

    Mirrors the runtime rules in driver_loader.validate_driver_definition:
    type is multicast (group/port) or sse (path/idle_timeout, http transport
    only); {config_field} templates must reference fields declared in
    config_schema or default_config. Catching this at catalog-build time
    keeps a driver that would fail to load out of the index.
    """
    errors: list[str] = []
    push = data.get("push")
    if push is None:
        return errors
    if not isinstance(push, dict):
        errors.append(f"{file}: push must be a mapping")
        return errors

    config_fields: set[str] = set()
    for src_key in ("config_schema", "default_config"):
        src = data.get(src_key)
        if isinstance(src, dict):
            config_fields.update(src)

    known_keys_by_type = {
        "multicast": {"type", "group", "port"},
        "sse": {"type", "path", "idle_timeout"},
    }
    ptype = push.get("type")
    if ptype in ("tcp_listener", "http_listener"):
        errors.append(
            f"{file}: push type '{ptype}' is not supported yet "
            f"(only 'multicast' and 'sse')"
        )
    elif ptype not in known_keys_by_type:
        errors.append(
            f"{file}: push missing or unknown 'type' (supported: multicast, sse)"
        )
    known_keys = known_keys_by_type.get(
        ptype, {"type", "group", "port", "path", "idle_timeout"}
    )
    unknown = set(push) - known_keys
    if unknown:
        errors.append(
            f"{file}: push has unknown key(s): {', '.join(sorted(unknown))}"
        )

    def check_template(where: str, value: str) -> None:
        fields = re.findall(r"\{(\w+)\}", value)
        if not fields:
            errors.append(
                f"{file}: push.{where} {value!r} has braces but no "
                f"{{config_field}} token"
            )
        for field in fields:
            if field not in config_fields:
                errors.append(
                    f"{file}: push.{where} references config field '{field}' "
                    f"not declared in config_schema or default_config"
                )

    if ptype == "multicast":
        group = push.get("group")
        if group is None:
            errors.append(f"{file}: push missing 'group'")
        elif isinstance(group, str) and "{" in group:
            check_template("group", group)
        else:
            ok = False
            if isinstance(group, str):
                parts = group.split(".")
                if len(parts) == 4 and all(
                    p.isdigit() and int(p) <= 255 for p in parts
                ):
                    ok = 224 <= int(parts[0]) <= 239
            if not ok:
                errors.append(
                    f"{file}: push.group {group!r} must be an IPv4 multicast "
                    f"address (224.0.0.0 - 239.255.255.255) or a "
                    f"{{config_field}} template"
                )

        port = push.get("port")
        if port is None:
            errors.append(f"{file}: push missing 'port'")
        elif isinstance(port, str) and "{" in port:
            check_template("port", port)
        elif isinstance(port, bool) or not isinstance(port, int) or not (0 < port < 65536):
            errors.append(
                f"{file}: push.port must be an integer 1-65535 or a "
                f"{{config_field}} template"
            )

    elif ptype == "sse":
        if data.get("transport") != "http":
            errors.append(
                f"{file}: push type 'sse' requires the http transport"
            )
        raw_path = push.get("path")
        paths = (
            [raw_path] if isinstance(raw_path, str)
            else raw_path if isinstance(raw_path, list) else None
        )
        if raw_path is None or paths == []:
            errors.append(
                f"{file}: push missing 'path' (an event-stream URL path, "
                f"or a list of them)"
            )
        elif paths is None:
            errors.append(
                f"{file}: push.path must be a string or a list of strings"
            )
        else:
            for p in paths:
                if not isinstance(p, str) or not p.strip():
                    errors.append(
                        f"{file}: push.path entry {p!r} must be a "
                        f"non-empty string"
                    )
                elif "{" in p:
                    check_template("path", p)
                elif not p.startswith("/"):
                    errors.append(
                        f"{file}: push.path {p!r} must start with '/' or "
                        f"be a {{config_field}} template"
                    )
        idle = push.get("idle_timeout")
        if idle is not None and (
            isinstance(idle, bool)
            or not isinstance(idle, (int, float))
            or idle <= 0
        ):
            errors.append(
                f"{file}: push.idle_timeout must be a positive number of "
                f"seconds"
            )
    return errors


def _validate_child_entity_types(file: str, data: dict[str, Any]) -> list[str]:
    """Validate a driver's optional `child_entity_types:` mapping.

    Mirrors the runtime rules in driver_loader.validate_driver_definition: the
    block must be a mapping, and each child type name becomes a state-key
    segment (device.<id>.<child_type>.<local_id>.<prop>) that also feeds the
    fnmatch dispatch routing per-child state changes — so a name must be a
    non-empty string with no dots and no glob metacharacters (* ? [). Without
    this, a malformed child type only fails when an instance loads the driver
    at runtime, not at catalog-build time.
    """
    errors: list[str] = []
    child_types = data.get("child_entity_types", {})
    if child_types and not isinstance(child_types, dict):
        errors.append(f"{file}: child_entity_types must be a mapping")
        return errors
    if not isinstance(child_types, dict):
        return errors
    valid_var_types = {"string", "integer", "number", "boolean", "enum", "float"}
    for child_type, type_def in child_types.items():
        if not isinstance(child_type, str) or not child_type:
            errors.append(
                f"{file}: child_entity_types type name {child_type!r} must be a "
                f"non-empty string"
            )
            continue
        if "." in child_type:
            errors.append(
                f"{file}: child_entity_types type name '{child_type}' must not "
                f"contain dots (used as a state-key separator)"
            )
        bad = [c for c in "*?[" if c in child_type]
        if bad:
            errors.append(
                f"{file}: child_entity_types type name '{child_type}' must not "
                f"contain glob metacharacters ({', '.join(bad)}) — they break "
                f"state-change dispatch"
            )
        # Schema depth (mirrors driver_loader): a malformed id_format used to
        # fail at connect() on the instance, and an invalid cloud_priority
        # silently fell to the default relay tier.
        where = f"{file}: child_entity_types.{child_type}"
        if not isinstance(type_def, dict):
            errors.append(f"{where} must be a mapping")
            continue
        id_format = type_def.get("id_format")
        if id_format is not None and not isinstance(id_format, dict):
            errors.append(f"{where}.id_format must be a mapping")
        elif isinstance(id_format, dict):
            id_type = id_format.get("type", "integer")
            if id_type not in ("integer", "string"):
                errors.append(
                    f"{where}.id_format: unknown type '{id_type}' "
                    f"(expected 'integer' or 'string')"
                )
            mn, mx = id_format.get("min"), id_format.get("max")
            for label, val in (("min", mn), ("max", mx)):
                if val is not None and (
                    isinstance(val, bool) or not isinstance(val, int)
                ):
                    errors.append(
                        f"{where}.id_format: {label} must be an integer (got {val!r})"
                    )
            if isinstance(mn, int) and isinstance(mx, int) and mn > mx:
                errors.append(
                    f"{where}.id_format: min ({mn}) is greater than max ({mx})"
                )
            pad = id_format.get("pad_width")
            if pad is not None and (
                isinstance(pad, bool) or not isinstance(pad, int) or pad < 1
            ):
                errors.append(
                    f"{where}.id_format: pad_width must be a positive integer "
                    f"(got {pad!r})"
                )
        child_vars = type_def.get("state_variables")
        if child_vars is not None and not isinstance(child_vars, dict):
            errors.append(f"{where}.state_variables must be a mapping")
        elif isinstance(child_vars, dict):
            for var_name, var_def in child_vars.items():
                if not isinstance(var_def, dict):
                    errors.append(
                        f"{where}.state_variables.{var_name} must be a mapping"
                    )
                    continue
                vt = var_def.get("type", "")
                if vt and vt not in valid_var_types:
                    errors.append(
                        f"{where}.state_variables.{var_name}: unknown type '{vt}'"
                    )
                cp = var_def.get("cloud_priority")
                if cp is not None and cp not in ("low", "high"):
                    errors.append(
                        f"{where}.state_variables.{var_name}: cloud_priority "
                        f"must be 'low' or 'high' (got {cp!r})"
                    )
                cunit = var_def.get("unit")
                if cunit is not None and not isinstance(cunit, str):
                    errors.append(
                        f"{where}.state_variables.{var_name}: unit must be "
                        f"a string (got {cunit!r})"
                    )
                cctl = var_def.get("control")
                if cctl is not None and not isinstance(cctl, bool):
                    errors.append(
                        f"{where}.state_variables.{var_name}: control must "
                        f"be true or false (got {cctl!r})"
                    )
        # Declarative roster (mirrors driver_loader): exactly one source;
        # count sane vs id_format; *_from names a declared config field.
        instances = type_def.get("instances")
        if instances is not None:
            if not isinstance(instances, dict):
                errors.append(f"{where}.instances must be a mapping")
                continue
            config_fields: set[str] = set()
            for src in ("config_schema", "default_config"):
                block = data.get(src)
                if isinstance(block, dict):
                    config_fields.update(block.keys())
            id_fmt = type_def.get("id_format")
            id_fmt = id_fmt if isinstance(id_fmt, dict) else {}
            id_type = id_fmt.get("type", "integer")
            sources = [
                k for k in ("count", "count_from", "ids_from") if k in instances
            ]
            if len(sources) != 1:
                errors.append(
                    f"{where}.instances: declare exactly one of 'count', "
                    f"'count_from', 'ids_from'"
                )
            elif sources[0] == "count":
                count = instances["count"]
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    errors.append(
                        f"{where}.instances: count must be an integer >= 1 "
                        f"(got {count!r})"
                    )
                else:
                    mx = id_fmt.get("max")
                    if isinstance(mx, int) and not isinstance(mx, bool) and count > mx:
                        errors.append(
                            f"{where}.instances: count ({count}) exceeds "
                            f"id_format.max ({mx})"
                        )
                if id_type == "string":
                    errors.append(
                        f"{where}.instances: 'count' requires integer ids "
                        f"(id_format.type is 'string' — use 'ids_from')"
                    )
            else:
                src_key = sources[0]
                field = instances[src_key]
                if not isinstance(field, str) or not field:
                    errors.append(
                        f"{where}.instances: {src_key} must name a config field"
                    )
                elif field not in config_fields:
                    errors.append(
                        f"{where}.instances: {src_key} '{field}' is not a "
                        f"declared config field (config_schema / default_config)"
                    )
                if src_key == "count_from" and id_type == "string":
                    errors.append(
                        f"{where}.instances: 'count_from' requires integer ids "
                        f"(id_format.type is 'string' — use 'ids_from')"
                    )
            label = instances.get("label")
            if label is not None and not isinstance(label, str):
                errors.append(f"{where}.instances: label must be a string")
    return errors


def _validate_child_routing(file: str, data: dict[str, Any]) -> list[str]:
    """Validate `child_set:` response routing and `each_child:` query
    templates (mirrors driver_loader.validate_driver_definition). A bad
    entry silently never writes child state / never polls — catch it at
    catalog-build time.
    """
    errors: list[str] = []
    child_types = data.get("child_entity_types")
    child_types = child_types if isinstance(child_types, dict) else {}

    responses = data.get("responses")
    responses = responses if isinstance(responses, list) else []
    for i, resp in enumerate(responses):
        if not isinstance(resp, dict):
            continue
        # Optional per-rule throttle (platform >= 0.23.0) — mirrors the
        # runtime loader: a non-positive/non-numeric value is rejected.
        throttle = resp.get("throttle")
        if throttle is not None and (
            isinstance(throttle, bool)
            or not isinstance(throttle, (int, float))
            or throttle <= 0
        ):
            errors.append(
                f"{file}: Response {i}: throttle must be a positive number "
                f"of seconds"
            )
        # Optional require: scope on json rules (platform >= 0.23.0) —
        # mirrors the runtime loader.
        require = resp.get("require")
        if require is not None:
            if not resp.get("json"):
                errors.append(
                    f"{file}: Response {i}: require only applies to "
                    f"json: true responses"
                )
            if isinstance(require, str):
                if not require.strip():
                    errors.append(
                        f"{file}: Response {i}: require must name a JSON key"
                    )
            elif isinstance(require, list):
                if not require or not all(
                    isinstance(k, str) and k.strip() for k in require
                ):
                    errors.append(
                        f"{file}: Response {i}: require list entries must "
                        f"be non-empty JSON key names"
                    )
            else:
                errors.append(
                    f"{file}: Response {i}: require must be a JSON key name "
                    f"or a list of them"
                )
        child_set = resp.get("child_set")
        if child_set is None:
            continue
        if "address" in resp:
            errors.append(
                f"{file}: Response {i}: child_set is not supported on OSC "
                f"responses"
            )
            continue
        if resp.get("json"):
            errors.append(
                f"{file}: Response {i}: child_set is not supported on json "
                f"responses"
            )
            continue
        if not isinstance(child_set, list) or not child_set:
            errors.append(
                f"{file}: Response {i}: child_set must be a non-empty list"
            )
            continue
        pattern = resp.get("pattern", "") or resp.get("match", "")
        ngroups = None
        if isinstance(pattern, str) and pattern:
            try:
                ngroups = re.compile(pattern).groups
            except re.error:
                ngroups = None  # {config} placeholders substitute at runtime

        def _check_ref(where: str, ref: str) -> None:
            try:
                group = int(ref[1:])
            except ValueError:
                errors.append(
                    f"{where}: {ref!r} is not a numeric capture ref ($1, $2, ...)"
                )
                return
            if group < 1:
                errors.append(f"{where}: capture ref must be $1 or higher")
            elif ngroups is not None and group > ngroups:
                errors.append(
                    f"{where}: capture ref ${group} exceeds the pattern's "
                    f"{ngroups} group(s)"
                )

        for j, entry in enumerate(child_set):
            where = f"{file}: Response {i}: child_set[{j}]"
            if not isinstance(entry, dict):
                errors.append(f"{where}: must be a mapping")
                continue
            ctype = entry.get("type")
            if not isinstance(ctype, str) or ctype not in child_types:
                errors.append(
                    f"{where}: type {ctype!r} is not a declared child_entity_type"
                )
                continue
            tdef = child_types.get(ctype)
            tdef = tdef if isinstance(tdef, dict) else {}
            cvars = tdef.get("state_variables")
            cvars = cvars if isinstance(cvars, dict) else {}
            cid = entry.get("id")
            id_fmt = tdef.get("id_format")
            id_fmt = id_fmt if isinstance(id_fmt, dict) else {}
            id_type = id_fmt.get("type", "integer")
            if cid is None:
                errors.append(
                    f"{where}: missing 'id' (a capture ref like $1, a "
                    f"literal, or {{group, map}})"
                )
            elif isinstance(cid, dict):
                # Long form {group, map}: wire-id translation on a capture
                # ref (mirrors driver_loader.py).
                gref = cid.get("group")
                if isinstance(gref, str) and gref.startswith("$"):
                    _check_ref(f"{where}: id group", gref)
                elif isinstance(gref, int) and not isinstance(gref, bool):
                    _check_ref(f"{where}: id group", f"${gref}")
                else:
                    errors.append(
                        f"{where}: id group must be a capture ref "
                        f"(1, 2, ... or '$1')"
                    )
                id_map = cid.get("map")
                if id_map is not None:
                    if not isinstance(id_map, dict) or not id_map:
                        errors.append(
                            f"{where}: id map must be a non-empty mapping "
                            f"of wire id -> local child id"
                        )
                    else:
                        for mk, mv in id_map.items():
                            if isinstance(mv, bool) or not isinstance(
                                mk, (str, int)
                            ) or not isinstance(mv, (str, int)):
                                errors.append(
                                    f"{where}: id map entries must be "
                                    f"scalar wire id -> local id pairs"
                                )
                                break
                            if id_type == "integer":
                                try:
                                    int(str(mv).strip())
                                except ValueError:
                                    errors.append(
                                        f"{where}: id map value {mv!r} is "
                                        f"not an integer ({ctype} declares "
                                        f"integer ids)"
                                    )
                                    break
            elif isinstance(cid, str) and cid.startswith("$"):
                _check_ref(f"{where}: id", cid)
            state_map = entry.get("state")
            if not isinstance(state_map, dict) or not state_map:
                errors.append(
                    f"{where}: missing 'state' mapping (prop -> $N or literal)"
                )
                continue
            for prop, expr in state_map.items():
                if prop not in cvars:
                    errors.append(
                        f"{where}: state prop '{prop}' is not declared in "
                        f"child_entity_types.{ctype}.state_variables"
                    )
                if isinstance(expr, str) and expr.startswith("$"):
                    _check_ref(f"{where}: state '{prop}'", expr)

    def _check_each_child(name: str, entries: Any, allow_osc_dict: bool) -> None:
        if not isinstance(entries, list):
            return
        for i, q in enumerate(entries):
            if not isinstance(q, dict):
                continue
            if "each_child" not in q:
                if allow_osc_dict and "address" in q:
                    continue
                errors.append(
                    f"{file}: {name}[{i}]: mapping entries must be "
                    f"{{each_child, send}}"
                    + (" or {address, args}" if allow_osc_dict else "")
                )
                continue
            ec = q.get("each_child")
            if not isinstance(ec, str) or ec not in child_types:
                errors.append(
                    f"{file}: {name}[{i}]: each_child type {ec!r} is not a "
                    f"declared child_entity_type"
                )
            else:
                tdef = child_types.get(ec)
                inst = tdef.get("instances") if isinstance(tdef, dict) else None
                if not isinstance(inst, dict):
                    errors.append(
                        f"{file}: {name}[{i}]: each_child type '{ec}' declares "
                        f"no instances: block — nothing would ever be polled"
                    )
            send = q.get("send")
            if not isinstance(send, str) or not send:
                errors.append(f"{file}: {name}[{i}]: missing 'send' template")
            elif "{child_id}" not in send:
                errors.append(
                    f"{file}: {name}[{i}]: 'send' must contain {{child_id}}"
                )

    polling = data.get("polling")
    if isinstance(polling, dict):
        _check_each_child("polling.queries", polling.get("queries"), False)
    _check_each_child("on_connect", data.get("on_connect"), True)
    return errors


def _validate_command_framing(file: str, data: dict[str, Any]) -> list[str]:
    """Validate the optional send-side command framing fields.

    Mirrors driver_loader.validate_driver_definition: `command_prefix` /
    `command_suffix` wrap every byte-stream command and must be strings when
    present (a list or number would break the send path).
    """
    errors: list[str] = []
    for key in ("command_prefix", "command_suffix"):
        val = data.get(key)
        if val is not None and not isinstance(val, str):
            errors.append(f"{file}: {key} must be a string")
    return errors


def _validate_device_settings(file: str, data: dict[str, Any]) -> list[str]:
    """Validate a YAML driver's optional `device_settings:` mapping.

    Mirrors the runtime rules in driver_loader.validate_driver_definition:
    every setting needs a `write:` block (ConfigurableDriver raises
    NotImplementedError without one) and a `state_key` that names a declared
    state variable — a typo'd state_key used to pass CI and just show
    "(not set)" forever in the IDE while writes silently fired.
    """
    errors: list[str] = []
    settings = data.get("device_settings")
    if settings is None:
        return errors
    if not isinstance(settings, dict):
        errors.append(f"{file}: device_settings must be a mapping")
        return errors
    declared = data.get("state_variables")
    declared = declared if isinstance(declared, dict) else {}
    valid_types = {"string", "integer", "number", "boolean", "enum", "float"}
    for name, sdef in settings.items():
        where = f"{file}: device_settings.{name}"
        if not isinstance(sdef, dict):
            errors.append(f"{where} must be a mapping")
            continue
        stype = sdef.get("type", "")
        if stype and stype not in valid_types:
            errors.append(f"{where}: unknown type '{stype}'")
        state_key = sdef.get("state_key", name)
        if state_key not in declared:
            errors.append(
                f"{where}: state_key '{state_key}' is not a declared state "
                f"variable — the setting would never read back"
            )
        mn, mx = sdef.get("min"), sdef.get("max")
        if (
            isinstance(mn, (int, float)) and isinstance(mx, (int, float))
            and not isinstance(mn, bool) and not isinstance(mx, bool)
            and mn > mx
        ):
            errors.append(f"{where}: min ({mn}) is greater than max ({mx})")
        if not isinstance(sdef.get("write"), dict):
            errors.append(
                f"{where}: missing 'write' block (send / path / address) — a "
                f"device setting must be writable"
            )
    return errors


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
# so the rule is a hard error for NEW occurrences while these stay tracked for
# cleanup (openavc-backlog.md). birddog_ptz / birddog_codec both default to the
# "^birddog-" hostname with only soft signals (oui + NDI, deliberately not an
# mdns claim since _ndi._tcp is cross-vendor); telling a PTZ from a codec
# pre-install needs a probe of the BirdDog HTTP /about API, which needs a
# capture from real gear. Remove an id here once it carries a real fingerprint.
_KNOWN_LOCATOR_AMBIGUITY: frozenset[str] = frozenset({
    "birddog_ptz", "birddog_codec",
})


def _validate_locator_disambiguation(
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
                continue  # grandfathered pre-existing case (tracked in backlog)
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


_DISCOVERY_FEATURE_GATES: tuple[tuple[Any, str], ...] = (
    (_ssdp_has_description_filters, "0.23.0"),
)


def _with_discovery_requires(discovery: dict[str, Any]) -> dict[str, Any]:
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


def build_entry(filepath: Path, raw: dict[str, Any], repo_root: Path) -> DriverEntry:
    subset = {k: v for k, v in raw.items() if k in INDEX_FIELDS}
    discovery = subset.get("discovery")
    if isinstance(discovery, dict):
        subset["discovery"] = _with_discovery_requires(discovery)
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


# No `generated_at` timestamp: the catalog is a deterministic function of the
# driver files, so re-running the build produces byte-identical output. CI
# rebuilds and commits the catalog on merge to main (contributors never commit
# it), and a wall-clock timestamp would make every run a spurious diff — the
# exact churn that turned unrelated category shards into merge conflicts. Don't
# reintroduce it. `generator_version` / `schema_version` carry the provenance.
def _meta_block(total_key: str, total: int, **extra: Any) -> dict[str, Any]:
    block = {
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
    sorted_entries = sorted(
        entries, key=lambda e: (e.manufacturer.lower(), e.name.lower())
    )
    entry_dicts = [_entry_dict(e) for e in sorted_entries]

    # index.json
    _write_json(
        repo_root / "index.json",
        {
            "_meta": _meta_block("total_drivers", len(entry_dicts)),
            "drivers": entry_dicts,
        },
    )

    # devices.json
    _write_json(
        repo_root / "devices.json",
        {
            "_meta": _meta_block("total_devices", len(devices)),
            "devices": devices,
        },
    )

    # Per-category driver shards
    for cat in DRIVER_CATEGORIES:
        shard = [d for d in entry_dicts if d["category"] == cat]
        _write_json(
            repo_root / "index" / f"{cat}.json",
            {
                "_meta": _meta_block("total_drivers", len(shard), category=cat),
                "drivers": shard,
            },
        )

    # Per-category device shards
    for cat in DRIVER_CATEGORIES:
        shard = [d for d in devices if d.get("category") == cat]
        _write_json(
            repo_root / "devices" / f"{cat}.json",
            {
                "_meta": _meta_block("total_devices", len(shard), category=cat),
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


def _validate_frame_parser_block(file: str, data: dict[str, Any]) -> list[str]:
    """Validate the optional frame_parser block against the runtime's limits.

    Mirrors server/drivers/driver_loader.validate_driver_definition: the runtime
    LengthPrefixFrameParser only accepts header_size in {1, 2, 4} and
    FixedLengthFrameParser needs a positive length. An out-of-range value would
    crash connect() and wedge the device in a reconnect loop, so reject it at
    catalog-build time. Stdlib-only so the drivers repo CI stays self-contained.
    """
    errors: list[str] = []
    fp = data.get("frame_parser")
    if fp is None:
        return errors
    if not isinstance(fp, dict):
        return [f"{file}: frame_parser must be a mapping"]
    fp_type = fp.get("type", "")
    if fp_type == "length_prefix":
        header_size = fp.get("header_size", 2)
        if header_size not in (1, 2, 4):
            errors.append(
                f"{file}: frame_parser.header_size must be 1, 2, or 4 (got {header_size!r})"
            )
        offset = fp.get("header_offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int):
            errors.append(
                f"{file}: frame_parser.header_offset must be an integer (got {offset!r})"
            )
        for extra_key in ("length_offset", "header_extra"):
            extra_val = fp.get(extra_key, 0)
            if isinstance(extra_val, bool) or not isinstance(extra_val, int) or extra_val < 0:
                errors.append(
                    f"{file}: frame_parser.{extra_key} must be a non-negative "
                    f"integer (got {extra_val!r})"
                )
        endian = fp.get("length_endian", "big")
        if endian not in ("big", "little"):
            errors.append(
                f"{file}: frame_parser.length_endian must be 'big' or 'little' "
                f"(got {endian!r})"
            )
    elif fp_type == "fixed_length":
        length = fp.get("length", 1)
        if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            errors.append(
                f"{file}: frame_parser.length must be a positive integer (got {length!r})"
            )
    elif fp_type:
        errors.append(
            f"{file}: frame_parser.type '{fp_type}' is not "
            f"'length_prefix' or 'fixed_length'"
        )
    else:
        errors.append(f"{file}: frame_parser is missing 'type'")
    return errors


def _validate_send_frame_block(file: str, data: dict[str, Any]) -> list[str]:
    """Validate the optional send_frame block (send-side packet framing).

    Mirrors server/drivers/driver_loader.validate_driver_definition: only
    length_prefix is supported, length_size must be a positive int, the byte
    fields must be strings. Stdlib-only so the drivers repo CI stays
    self-contained.
    """
    errors: list[str] = []
    sf = data.get("send_frame")
    if sf is None:
        return errors
    if not isinstance(sf, dict):
        return [f"{file}: send_frame must be a mapping"]
    sf_type = sf.get("type", "length_prefix")
    if sf_type != "length_prefix":
        return [f"{file}: send_frame.type '{sf_type}' is not 'length_prefix'"]
    length_size = sf.get("length_size", 4)
    if isinstance(length_size, bool) or not isinstance(length_size, int) or length_size < 1:
        errors.append(
            f"{file}: send_frame.length_size must be a positive integer "
            f"(got {length_size!r})"
        )
    endian = sf.get("length_endian", "big")
    if endian not in ("big", "little"):
        errors.append(
            f"{file}: send_frame.length_endian must be 'big' or 'little' (got {endian!r})"
        )
    for byte_key in ("header", "after_length"):
        byte_val = sf.get(byte_key)
        if byte_val is not None and not isinstance(byte_val, str):
            errors.append(f"{file}: send_frame.{byte_key} must be a string (got {byte_val!r})")
    return errors


_ACTION_KINDS = ("command", "setup")
_ACTION_AVAILABILITIES = ("online", "offline", "always")
_VISIBLE_WHEN_OPERATORS = frozenset({
    "eq", "ne", "gt", "lt", "gte", "lte", "truthy", "falsy",
    "equals", "not_equals", "==", "!=", ">", "<", ">=", "<=",
})


def _validate_visible_when(where: str, vw: Any) -> list[str]:
    if vw is None:
        return []
    if not isinstance(vw, dict):
        return [f"{where}: 'visible_when' must be a mapping"]

    def _validate_condition(cwhere: str, cond: Any) -> list[str]:
        if not isinstance(cond, dict):
            return [f"{cwhere}: condition must be a mapping"]
        errs: list[str] = []
        if not isinstance(cond.get("key"), str) or not cond.get("key"):
            errs.append(f"{cwhere}: condition missing 'key' (state key string)")
        op = cond.get("operator", "eq")
        if op not in _VISIBLE_WHEN_OPERATORS:
            errs.append(f"{cwhere}: unknown operator '{op}'")
        return errs

    errors: list[str] = []
    if "any" in vw or "all" in vw:
        for group_key in ("any", "all"):
            if group_key not in vw:
                continue
            group = vw[group_key]
            if not isinstance(group, list) or not group:
                errors.append(
                    f"{where}: visible_when.{group_key} must be a non-empty list"
                )
                continue
            for j, cond in enumerate(group):
                errors.extend(
                    _validate_condition(f"{where}: visible_when.{group_key}[{j}]", cond)
                )
    else:
        errors.extend(_validate_condition(f"{where}: visible_when", vw))
    return errors


def _validate_actions_block(file: str, data: dict[str, Any]) -> list[str]:
    """Validate the optional actions / quick_actions blocks (Quick Action strip).

    Mirrors server/drivers/actions.validate_actions in the platform; kept
    stdlib-only here so the drivers repo CI stays self-contained.
    """
    errors: list[str] = []
    commands = data.get("commands")
    command_ids = set(commands.keys()) if isinstance(commands, dict) else set()

    quick = data.get("quick_actions")
    if quick is not None:
        if not isinstance(quick, list):
            errors.append(f"{file}: quick_actions must be a list of command ids")
        else:
            for i, cid in enumerate(quick):
                if not isinstance(cid, str) or not cid:
                    errors.append(
                        f"{file}: quick_actions[{i}] must be a non-empty command id"
                    )
                elif command_ids and cid not in command_ids:
                    errors.append(
                        f"{file}: quick_actions[{i}] '{cid}' is not a declared command"
                    )

    actions = data.get("actions")
    if actions is not None:
        if not isinstance(actions, list):
            errors.append(f"{file}: actions must be a list")
            return errors
        seen: set[str] = set()
        for i, entry in enumerate(actions):
            where = f"{file}: actions[{i}]"
            if not isinstance(entry, dict):
                errors.append(f"{where}: must be a mapping")
                continue
            action_id = entry.get("id")
            if not isinstance(action_id, str) or not action_id:
                errors.append(f"{where}: missing required 'id' (non-empty string)")
            else:
                if action_id in seen:
                    errors.append(f"{where}: duplicate action id '{action_id}'")
                seen.add(action_id)
            kind = entry.get("kind", "command")
            if kind not in _ACTION_KINDS:
                errors.append(
                    f"{where}: unknown kind '{kind}' (expected {list(_ACTION_KINDS)})"
                )
            elif kind == "setup" and file.endswith(".avcdriver"):
                # A .avcdriver is interpreted by ConfigurableDriver, which has no
                # run_setup_action handler — kind:"setup" can't do anything there.
                errors.append(
                    f"{where}: kind 'setup' requires a Python driver "
                    f"(a run_setup_action handler); .avcdriver drivers support "
                    f"kind 'command' only"
                )
            if entry.get("label") is not None and not isinstance(entry.get("label"), str):
                errors.append(f"{where}: 'label' must be a string")
            if entry.get("icon") is not None and not isinstance(entry.get("icon"), str):
                errors.append(f"{where}: 'icon' must be a string (lucide icon name)")
            avail = entry.get("availability")
            if avail is not None and avail not in _ACTION_AVAILABILITIES:
                errors.append(
                    f"{where}: 'availability' must be one of {list(_ACTION_AVAILABILITIES)}"
                )
            confirm = entry.get("confirm")
            if confirm is not None and not isinstance(confirm, (bool, str)):
                errors.append(f"{where}: 'confirm' must be a boolean or a message string")
            params = entry.get("params")
            if params is not None and not isinstance(params, dict):
                errors.append(f"{where}: 'params' must be a mapping")
            errors.extend(_validate_visible_when(where, entry.get("visible_when")))
            if kind == "command" and isinstance(action_id, str) and action_id:
                command_id = entry.get("command")
                if command_id is not None and not isinstance(command_id, str):
                    errors.append(f"{where}: 'command' must be a string")
                else:
                    target = command_id or action_id
                    if command_ids and target not in command_ids:
                        errors.append(
                            f"{where}: command '{target}' is not a declared command"
                        )
    return errors


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
        "--check-json-schema",
        action="store_true",
        help="Validate driver files against the JSON Schema",
    )
    parser.add_argument(
        "--json-schema-file",
        type=Path,
        default=None,
        help="Path to JSON Schema file (default: avcdriver.schema.json in repo root)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root (default: parent of scripts/)",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.root.resolve()
    json_schema_path = args.json_schema_file or (repo_root / "avcdriver.schema.json")
    if jsonschema_validator_for is not None and json_schema_path.is_file():
        json_schema = json.loads(json_schema_path.read_text(encoding="utf-8"))
        json_validator = jsonschema_validator_for(json_schema)
        python_json_validator = jsonschema_validator_for(
            _python_driver_schema(json_schema)
        )
    else:
        if args.check_json_schema:
            # If the user explicitly requested JSON Schema validation,
            # treat a missing schema file as an error.
            print(
                f"ERROR: JSON Schema file not found at {json_schema_path}",
                file=sys.stderr,
            )
            return 1
        print(
            f"WARNING: JSON Schema file not found at {json_schema_path}, skipping schema validation.",
            file=sys.stderr,
        )
        json_schema = None
        json_validator = None
        python_json_validator = None

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

    def _print_schema_errors(errors: list[str]) -> None:
        """Print JSON Schema validation errors, if any."""
        if not errors:
            return
        print(
            f"\nFAILED: {len(errors)} JSON Schema validation error(s):\n",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)

    if json_schema is not None:
        assert json_validator is not None
        assert python_json_validator is not None
        schema_errors = _validate_all_json_schemas(
            raw, json_validator, python_json_validator
        )
        if args.check_json_schema and schema_errors:
            # If the user requested JSON Schema validation,
            # report any schema errors and exit with failure.
            _print_schema_errors(schema_errors)
            return 1
    else:
        schema_errors = []

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

        errors.extend(_validate_auth_block(rel, data))
        errors.extend(_validate_liveness_block(rel, data))
        errors.extend(_validate_push_block(rel, data))
        errors.extend(_validate_frame_parser_block(rel, data))
        errors.extend(_validate_send_frame_block(rel, data))
        errors.extend(_validate_actions_block(rel, data))
        errors.extend(_validate_child_entity_types(rel, data))
        errors.extend(_validate_child_routing(rel, data))
        errors.extend(_validate_device_settings(rel, data))
        errors.extend(_validate_command_framing(rel, data))

        disc_errors, normalized = _validate_discovery_block(
            rel, data, yaml_dir=filepath.parent,
        )
        errors.extend(disc_errors)
        if normalized:
            discovery_per_driver.append((str(data.get("id") or ""), rel, normalized))

    if entries:
        errors.extend(cross_validate(entries, manufacturers))
    errors.extend(_validate_no_signal_collisions(discovery_per_driver))
    errors.extend(_validate_locator_disambiguation(discovery_per_driver))

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

    if len(schema_errors) > 0:
        # If we got here, the schema validation ran with errors not caught by
        # the above checks (and the `--check-json-schema` flag was not set).
        # Report those errors and exit with failure. This is done as the
        # final step to avoid duplicated error reports.
        _print_schema_errors(schema_errors)
        return 1

    print(f"Validated {len(entries)} driver(s), {len(devices)} device(s).")

    if args.check or args.check_json_schema:
        return 0

    write_outputs(repo_root, entries, devices)
    print(
        f"Wrote index.json, devices.json, and "
        f"{len(DRIVER_CATEGORIES) * 2} category shard files."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
