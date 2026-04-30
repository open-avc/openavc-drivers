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
    "audio", "cameras", "displays", "lighting",
    "projectors", "switchers", "utility", "video",
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
    """Pull only INDEX_FIELDS out of a DRIVER_INFO dict literal."""
    result: dict[str, Any] = {}
    for k, v in zip(node.keys, node.values):
        if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
            raise ExtractError(
                f"{file.name}: DRIVER_INFO keys must be string literals"
            )
        if k.value not in INDEX_FIELDS:
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
    """Walk driver dirs and extract raw metadata. Does not validate."""
    raw: list[tuple[Path, dict[str, Any]]] = []
    for dir_name in DRIVER_DIRS:
        dir_path = repo_root / dir_name
        if not dir_path.exists():
            continue
        for filepath in sorted(dir_path.iterdir()):
            if filepath.suffix == ".avcdriver":
                raw.append((filepath, extract_yaml_driver_info(filepath)))
            elif filepath.suffix == ".py" and not filepath.name.endswith("_sim.py"):
                raw.append((filepath, extract_python_driver_info(filepath)))
    return raw


# --- Index field selection --------------------------------------------------

# The driver file may carry many fields the runtime needs (transport config,
# commands, state_variables, default_config, config_schema, discovery, etc.).
# Only these go into index.json.
INDEX_FIELDS = frozenset({
    "id", "name", "manufacturer", "category", "version", "author",
    "transport", "description", "source_url",
    "ports", "protocols", "simulated", "verified", "min_platform_version",
    "tags", "help", "deprecated", "replacement_id", "compatible_models",
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
    for filepath, data in raw:
        rel = filepath.relative_to(repo_root).as_posix()
        try:
            entries.append(build_entry(filepath, data, repo_root))
        except ValidationError as e:
            errors.extend(_format_validation_errors(rel, e))
        except Exception as e:
            errors.append(f"{rel}: {e}")

    if entries:
        errors.extend(cross_validate(entries, manufacturers))

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
