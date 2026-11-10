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
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
# The platform's driver-contract rules run here from a generated copy under
# scripts/_vendor/ (see scripts/vendor_platform_contract.py; CI keeps that
# copy in sync with the platform repo). Pin scripts/ on sys.path so the
# package resolves the same way whether this file runs as a script or is
# imported by the tests.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from catalog_contract import ContractReport  # noqa: E402
from catalog_discovery import (  # noqa: E402
    declared_companion,
    validate_discovery_block,
    with_discovery_requires,
)
from discovery_claims import (  # noqa: E402
    validate_locator_disambiguation,
    validate_no_signal_collisions,
)
from _vendor.python_info import (  # noqa: E402
    ExtractError,
    driver_info_ast,
)
from _vendor.spec import (  # noqa: E402
    CATEGORIES,
    CONFIDENCE_LEVELS,
    DRIVER_ID_PATTERN,
    PYTHON_ONLY_TRANSPORTS,
    SEMVER_PATTERN,
    TAG_PATTERN,
    URL_PATTERN,
    YAML_TRANSPORTS,
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

GENERATOR_VERSION = "1.1.0"   # 1.1.0 adds the per-entry `files` hash map
SCHEMA_VERSION = "1"

# Categories, transports, and confidence levels come straight from the
# platform's vendored contract tables, so the catalog can't drift from the
# platform. DRIVER_TRANSPORTS is the YAML (.avcdriver) transports plus the
# Python-driver-only ones ("ssh"/"mqtt" need driver code the declarative
# runtime doesn't model, so the YAML schema intentionally omits them).
DRIVER_CATEGORIES = CATEGORIES
DRIVER_TRANSPORTS = (*YAML_TRANSPORTS, *PYTHON_ONLY_TRANSPORTS)
CONFIDENCE_VALUES = CONFIDENCE_LEVELS

DRIVER_DIRS = (
    "audio", "cameras", "displays", "lighting", "power",
    "projectors", "streaming", "switchers", "utility", "video",
)

# Shape patterns from the vendored contract tables (URL_PATTERN encodes
# its case-insensitivity in character classes).
SEMVER_RE = re.compile(SEMVER_PATTERN)
ID_RE = re.compile(DRIVER_ID_PATTERN)
TAG_RE = re.compile(TAG_PATTERN)
URL_RE = re.compile(URL_PATTERN)


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


def _is_port(p: Any) -> bool:
    """A port number: an integer (not a bool) from 1 to 65535."""
    return isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535


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
    # Repo-relative path -> SHA-256 of every file installing this driver
    # fetches (the driver plus any companion). The platform hashes what it
    # downloads and compares before writing anything to driver_repo/, so a
    # file that changed without the catalog being rebuilt is refused.
    files: dict[str, str] = Field(default_factory=dict)

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
            if not _is_port(p):
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
#
# Reading a Python driver's DRIVER_INFO out of the source is the platform's,
# under scripts/_vendor/python_info.py: the standalone checker (python -m
# openavc.drivers.check) and openavc.simulator.validate read it the same way, so what
# the catalog checks and what an author is told before publishing cannot
# diverge. What stays here is the catalog's own stricter pass — index fields
# must be static literals, because they ship in index.json.


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
        return _literal_dict(node, file=file)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [ast_to_python(item, file=file) for item in node.elts]
    if _is_negated_number(node):
        return -node.operand.value
    raise ExtractError(
        f"{file.name}: DRIVER_INFO must contain only literal data "
        f"(no calls, comprehensions, or f-strings). "
        f"Offending node: {type(node).__name__}"
    )


def _literal_dict(node: ast.Dict, *, file: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in zip(node.keys, node.values):
        if not _is_string_key(k):
            raise ExtractError(
                f"{file.name}: dict keys must be string literals "
                f"(found non-string key in DRIVER_INFO)"
            )
        result[k.value] = ast_to_python(v, file=file)
    return result


def _is_string_key(key: ast.expr | None) -> bool:
    """A dict key written as a string literal (a ``**spread`` has no key node)."""
    return isinstance(key, ast.Constant) and isinstance(key.value, str)


def _is_negated_number(node: Any) -> bool:
    """A minus sign on a numeric constant: how a negative number is written."""
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    )


def extract_python_driver_info(filepath: Path) -> dict[str, Any]:
    """Locate `DRIVER_INFO = {...}` inside a class body and extract index fields.

    Only keys in INDEX_FIELDS are extracted — runtime-only keys
    (`state_variables`, `commands`, `default_config`, `config_schema`, etc.)
    may contain non-literal expressions and are skipped. Index fields MUST be
    literal data; non-literal values there raise ExtractError.
    """
    # FINDING the dict is the platform's rule, so it comes from the vendored
    # reader rather than a second walk here: a second copy goes stale, and a
    # driver `python -m openavc.drivers.check` accepts (the annotated
    # `DRIVER_INFO: dict[str, Any] = {...}`, for one) would be refused here
    # for having no DRIVER_INFO at all. What stays here is the catalog's own
    # stricter pass over the fields it publishes.
    return _extract_index_fields(driver_info_ast(filepath), file=filepath)


def _extract_index_fields(node: ast.Dict, *, file: Path) -> dict[str, Any]:
    """Pull INDEX_FIELDS out of a DRIVER_INFO dict literal.

    ``discovery`` is one of them: it ships in index.json, so on-device
    discovery can match a driver before it is installed, and the discovery
    checks read a Python driver's block from here. Any other key is a
    runtime field, left for the runtime to parse.
    """
    result: dict[str, Any] = {}
    for k, v in zip(node.keys, node.values):
        if not _is_string_key(k):
            raise ExtractError(
                f"{file.name}: DRIVER_INFO keys must be string literals"
            )
        if k.value in INDEX_FIELDS:
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


# A Python driver's companions sit beside it, named for it: its discovery
# probe and its simulator. They are not drivers, so the walk skips them, and an
# install of the driver fetches them (``_installed_file_set``), in this order.
PYTHON_COMPANION_SUFFIXES = ("_discovery.py", "_sim.py")


def collect_drivers(repo_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Walk driver dirs and extract raw metadata. Does not validate."""
    raw: list[tuple[Path, dict[str, Any]]] = []
    for filepath in _category_files(repo_root):
        reader = _driver_reader(filepath)
        if reader is not None:
            raw.append((filepath, reader(filepath)))
    return raw


def _category_files(repo_root: Path) -> Iterator[Path]:
    """Every entry in the category directories, a directory at a time, sorted."""
    for dir_name in DRIVER_DIRS:
        dir_path = repo_root / dir_name
        if dir_path.exists():
            yield from sorted(dir_path.iterdir())


def _driver_reader(filepath: Path) -> Callable[[Path], dict[str, Any]] | None:
    """How to read a file in a category directory, or None when it is not a driver."""
    if filepath.suffix == ".avcdriver":
        return extract_yaml_driver_info
    if filepath.suffix == ".py" and not filepath.name.endswith(PYTHON_COMPANION_SUFFIXES):
        return extract_python_driver_info
    return None


# --- JSON Schema ------------------------------------------------------------
#
# The published schemas check each driver file's shape on top of the build's
# own checks. Validation is optional unless --check-json-schema asks for it:
# then a schema that cannot run, or a driver it refuses, fails the run at
# once. Otherwise the reason is a warning, and a refused driver fails the run
# last, once every other check has passed, so nothing is reported twice.


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
        # jsonschema exposes .message; jsonschema_rs raises plain ValueError
        # for things it cannot even represent (a non-string mapping key, for
        # instance, which YAML produces from a bare "on:" or "off:"). Reading
        # .message unconditionally turned that into an AttributeError
        # traceback, hiding the driver and the reason.
        detail = getattr(e, "message", None) or str(e)
        errors.append(f"{filepath}: JSON Schema validation error: {detail}")
    except ValueError as e:
        errors.append(f"{filepath}: JSON Schema validation error: {e}")
    return errors


class _SchemaValidators(NamedTuple):
    """A validator per driver format; None where that format goes unchecked.

    .avcdriver (YAML) drivers use avcdriver.schema.json as published; .py
    (Python) drivers use pythondriver.schema.json, the platform-generated
    variant that also allows the Python-only features (ssh/mqtt transports,
    kind "setup" actions).
    """

    yaml: "JsonSchemaValidator | None" = None
    python: "JsonSchemaValidator | None" = None

    def for_file(self, filepath: Path) -> "JsonSchemaValidator | None":
        return self.python if filepath.suffix == ".py" else self.yaml


def _validate_all_json_schemas(
    raw: list[tuple[Path, dict[str, Any]]], validators: _SchemaValidators
) -> list[str]:
    """Validate each driver_info against the JSON Schema for its format,
    skipping a format that has none. Returns a list of errors."""
    errors: list[str] = []
    for filepath, driver_info in raw:
        validator = validators.for_file(filepath)
        if validator is not None:
            errors.extend(_validate_json_schema(filepath.as_posix(), driver_info, validator))
    return errors


def _schema_unavailable(json_schema_path: Path) -> str | None:
    """Why schema validation cannot run at all, or None when it can.

    The two reasons need different messages: a missing schema file is a repo
    problem, a missing jsonschema-rs is an environment one. Reporting the
    first when the cause is the second sends you to inspect a path where the
    file is sitting right there, which is exactly what happened, for 18 test
    failures.
    """
    # Read at call time: the tests stand in for an environment without the
    # package by setting this module's name to None.
    if jsonschema_validator_for is None:
        return (
            "jsonschema-rs is not installed, so JSON Schema validation cannot "
            "run (pip install -r requirements-dev.txt)"
        )
    if not json_schema_path.is_file():
        return f"JSON Schema file not found at {json_schema_path}"
    return None


def _cannot_validate(reason: str, *, required: bool, skipping: str) -> bool:
    """Say why some schema validation cannot run; True when that fails the run."""
    if required:
        # Asked for and cannot run: a failure rather than a downgrade.
        print(f"ERROR: {reason}", file=sys.stderr)
        return True
    print(f"WARNING: {reason}, skipping {skipping}.", file=sys.stderr)
    return False


def _schema_validators(json_schema_path: Path, *, required: bool) -> _SchemaValidators | None:
    """The schema validators this run has; None when it must stop (said why).

    .avcdriver files validate against ``json_schema_path``; .py drivers
    against ``pythondriver.schema.json`` beside it, the platform-generated
    variant that also allows the Python-only features (ssh/mqtt transports,
    kind "setup" actions). Both are vendored from the platform repo side by
    side.
    """
    reason = _schema_unavailable(json_schema_path)
    if reason is not None:
        failed = _cannot_validate(reason, required=required, skipping="schema validation")
        return None if failed else _SchemaValidators()
    yaml_validator = jsonschema_validator_for(
        json.loads(json_schema_path.read_text(encoding="utf-8"))
    )
    python_schema_path = json_schema_path.with_name("pythondriver.schema.json")
    if python_schema_path.is_file():
        return _SchemaValidators(yaml_validator, jsonschema_validator_for(
            json.loads(python_schema_path.read_text(encoding="utf-8"))
        ))
    if _cannot_validate(
        f"JSON Schema file not found at {python_schema_path}",
        required=required, skipping="schema validation for Python drivers",
    ):
        return None
    return _SchemaValidators(yaml_validator)


def _report_schema_errors(errors: list[str]) -> bool:
    """Print the schema's refusals, if any; True when there were some."""
    if not errors:
        return False
    print(
        f"\nFAILED: {len(errors)} JSON Schema validation error(s):\n",
        file=sys.stderr,
    )
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    return True


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


class CatalogHashError(RuntimeError):
    """A file cannot be hashed into the catalog as it currently sits on disk."""


def _sha256_file(path: Path) -> str:
    """SHA-256 of a driver file, as the platform will see it.

    This hash is a promise about bytes we do not control: the installer
    downloads the file from raw.githubusercontent, which serves the git blob,
    and refuses the install when the SHA-256 does not match. So the only
    correct answer here is the hash of the blob - and hashing the working copy
    only gives that when the working copy IS the blob.

    On Windows with core.autocrlf that is not free. A checkout rewrites LF to
    CRLF, so a catalog generated there records checksums no download can ever
    match, for every driver at once, and because the check is fail-closed the
    result is not a warning: nothing in the catalog can be installed. It
    happened on 2026-08-09 and CI caught it, which is luck rather than design
    - the local --check compares the working copy against itself and is
    perfectly happy.

    .gitattributes pins these files to LF so a checkout matches the blob on
    every OS. This guard is what notices when that has been bypassed: a stale
    clone made before those rules landed, a file written by a tool that forced
    CRLF, an archive extracted with conversion. Refusing here costs a rerun;
    not refusing ships a catalog that installs nothing.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_hashable_bytes(path: Path) -> None:
    """Refuse to hash a file whose bytes cannot be the bytes GitHub serves.

    Scoped to a real checkout by its caller: "the working copy must equal the
    blob" is only meaningful where there IS a blob. Test fixtures built in a
    temp directory have none, and on Windows they are written CRLF by the same
    text-mode default that caused the original bug - enforcing there would fail
    locally and pass in CI, which is the worst shape a check can have.
    """
    if b"\r\n" in path.read_bytes():
        raise CatalogHashError(
            f"{path}: file contains CRLF line endings, so its checksum would "
            f"not match the bytes GitHub serves and the platform would refuse "
            f"to install it.\n"
            f"    Renormalize this checkout, then rebuild:\n"
            f"        git add --renormalize .\n"
            f"        git rm -r --cached . && git reset --hard\n"
            f"        python scripts/build_index.py"
        )


def _installed_file_set(filepath: Path, raw: dict[str, Any]) -> list[Path]:
    """Every file the platform's install path fetches for this driver.

    Deliberately the *installed* set, not "every file related to the driver":
    the platform verifies each file it downloads against this map, so listing
    something it never fetches would look like a truncated install. That means:

    - the driver file itself, always;
    - a YAML driver's declared ``discovery.python.file`` companion (required —
      install hard-fails without it);
    - a Python driver's convention-named ``_discovery.py`` / ``_sim.py``
      siblings, which the platform fetches best-effort by name, and only when
      they actually exist here.

    A YAML driver's ``_sim.py`` is intentionally absent: the platform never
    installs one (YAML drivers simulate from their inline ``simulator:``
    block), so it has no bytes to check it against.
    """
    if filepath.suffix == ".avcdriver":
        return [filepath, *_declared_companion_file(filepath, raw)]
    return [filepath, *_convention_companions(filepath)]


def _declared_companion_file(filepath: Path, raw: dict[str, Any]) -> list[Path]:
    """A YAML driver's declared discovery companion, when that file exists."""
    discovery = raw.get("discovery")
    relpath = declared_companion(discovery.get("python")) if isinstance(discovery, dict) else None
    if relpath is None:
        return []
    companion = (filepath.parent / relpath).resolve()
    return [companion] if companion.is_file() else []


def _convention_companions(filepath: Path) -> list[Path]:
    """A Python driver's companions, by their names, when those files exist."""
    candidates = (
        filepath.parent / f"{filepath.stem}{suffix}" for suffix in PYTHON_COMPANION_SUFFIXES
    )
    return [companion for companion in candidates if companion.is_file()]


def _artifact_hashes(
    filepath: Path, raw: dict[str, Any], repo_root: Path
) -> dict[str, str]:
    """Repo-relative path -> SHA-256 for every file an install of this driver
    fetches. Consumed by the platform installer to check the bytes it got are
    the bytes this catalog was built from."""
    # In a real checkout the bytes on disk must already equal the blob, since
    # that is what the installer downloads and hashes. (.git is a directory in
    # a clone and a file in a linked worktree, so test for existence.)
    guard = (repo_root / ".git").exists()
    files = _installed_file_set(filepath, raw)
    if guard:
        for f in files:
            _assert_hashable_bytes(f)
    return {
        f.relative_to(repo_root).as_posix(): _sha256_file(f)
        for f in files
    }


def build_entry(filepath: Path, raw: dict[str, Any], repo_root: Path) -> DriverEntry:
    subset = {k: v for k, v in raw.items() if k in INDEX_FIELDS}
    discovery = subset.get("discovery")
    if isinstance(discovery, dict):
        subset["discovery"] = with_discovery_requires(discovery)
    subset["file"] = filepath.relative_to(repo_root).as_posix()
    subset["format"] = "avcdriver" if filepath.suffix == ".avcdriver" else "python"
    subset["files"] = _artifact_hashes(filepath, raw, repo_root)
    return DriverEntry(**subset)


# --- Cross-driver validation ------------------------------------------------


def cross_validate(entries: list[DriverEntry], manufacturers: set[str]) -> list[str]:
    """The checks that need every entry at once, each run over the whole catalog in turn."""
    return [
        *_duplicate_ids(entries),
        *_unknown_manufacturers(entries, manufacturers),
        *_unknown_model_manufacturers(entries, manufacturers),
        *_unresolved_replacements(entries),
        *_overlapping_full_support(entries),
    ]


def _duplicate_ids(entries: list[DriverEntry]) -> Iterator[str]:
    """Each driver whose id an earlier one already has, named with the latest of them."""
    seen_ids: dict[str, str] = {}
    for e in entries:
        if e.id in seen_ids:
            yield f"duplicate driver id {e.id!r}: {seen_ids[e.id]} vs {e.file}"
        seen_ids[e.id] = e.file


def _unknown_manufacturers(entries: list[DriverEntry], manufacturers: set[str]) -> Iterator[str]:
    for e in entries:
        if e.manufacturer not in manufacturers:
            yield (
                f"{e.file}: manufacturer {e.manufacturer!r} not in manufacturers.json. "
                f"Add it there first if it's legitimately new."
            )


def _unknown_model_manufacturers(
    entries: list[DriverEntry], manufacturers: set[str]
) -> Iterator[str]:
    for e in entries:
        for cm in e.compatible_models:
            if cm.manufacturer not in manufacturers:
                yield (
                    f"{e.file}: compatible_models manufacturer {cm.manufacturer!r} "
                    f"not in manufacturers.json"
                )


def _unresolved_replacements(entries: list[DriverEntry]) -> Iterator[str]:
    ids = {e.id for e in entries}
    for e in entries:
        if e.replacement_id and e.replacement_id not in ids:
            yield f"{e.file}: replacement_id {e.replacement_id!r} does not match any driver"


def _overlapping_full_support(entries: list[DriverEntry]) -> Iterator[str]:
    """Each full claim on a (manufacturer, model) the latest earlier full claim
    already made, unless both carry notes justifying the overlap."""
    seen_full: dict[tuple[str, str], _FullClaim] = {}
    full_claims = (
        (e, cm, model) for e, cm, model in _model_claims(entries) if cm.confidence == "full"
    )
    for e, cm, model in full_claims:
        key = (cm.manufacturer, model)
        prior = seen_full.get(key)
        if prior is not None and not _justified(prior, cm):
            yield (
                f"{e.file}: claims full support for "
                f"{cm.manufacturer} {model}, but {prior.file} already does. "
                f"Add a `notes:` field on both entries justifying the overlap."
            )
        seen_full[key] = _FullClaim(e.file, bool(cm.notes))


class _FullClaim(NamedTuple):
    file: str
    has_notes: bool


def _justified(prior: _FullClaim, cm: CompatibleModelsEntry) -> bool:
    """Both claims on the model carry notes explaining why they overlap."""
    return bool(prior.has_notes and cm.notes)


def _model_claims(
    entries: list[DriverEntry],
) -> Iterator[tuple[DriverEntry, CompatibleModelsEntry, str]]:
    """Every (driver, compatible_models entry, model) the catalog lists, in file order."""
    for e in entries:
        for cm in e.compatible_models:
            for model in cm.models:
                yield e, cm, model


# --- Devices catalog --------------------------------------------------------


CONFIDENCE_ORDER = {"full": 0, "partial": 1, "untested": 2}


def build_devices(
    entries: list[DriverEntry],
    devices_extra: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reverse-index `compatible_models` into a device catalog."""
    devices = _devices_from_drivers(entries)
    _merge_devices_extra(devices, devices_extra)
    return sorted(devices.values(), key=lambda d: (d["manufacturer"].lower(), d["model"].lower()))


def _devices_from_drivers(entries: list[DriverEntry]) -> dict[tuple[str, str], dict[str, Any]]:
    """Each listed (manufacturer, model) with the drivers that list it.

    The first driver to list a model gives the device its category; its
    drivers are ordered by confidence, then id.
    """
    devices: dict[tuple[str, str], dict[str, Any]] = {}
    for e, cm, model in _model_claims(entries):
        entry = devices.setdefault(
            (cm.manufacturer, model),
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
    for entry in devices.values():
        entry["drivers"].sort(key=lambda d: (CONFIDENCE_ORDER.get(d["confidence"], 9), d["id"]))
    return devices


def _merge_devices_extra(
    devices: dict[tuple[str, str], dict[str, Any]], devices_extra: list[dict[str, Any]]
) -> None:
    """Add devices-extra.json's entries: catalog gaps no driver produces."""
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


# --- Output ----------------------------------------------------------------


# Left out of an entry at these values, as None fields are: the catalog
# carries what a driver says, not the model's defaults.
_OMITTED_WHEN_EMPTY = ("ports", "protocols", "tags", "compatible_models")
_OMITTED_WHEN_FALSE = ("simulated", "verified", "deprecated")


def _entry_dict(entry: DriverEntry) -> dict[str, Any]:
    """Tidy dict for JSON output: drop None and default-empty fields."""
    d = entry.model_dump(exclude_none=True)
    return {key: value for key, value in d.items() if not _omitted(key, value)}


def _omitted(key: str, value: Any) -> bool:
    if key in _OMITTED_WHEN_EMPTY:
        return value == []
    return key in _OMITTED_WHEN_FALSE and value is False


# No `generated_at` timestamp: the catalog is a deterministic function of the
# driver files, so re-running the build produces byte-identical output. Whoever
# changes a driver rebuilds and commits the catalog in the same change, and
# `--check` is the CI gate on that; a wall-clock timestamp would make every run
# a spurious diff — the exact churn that turned unrelated category shards into
# merge conflicts. Don't reintroduce it. `generator_version` / `schema_version`
# carry the provenance.
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


# --- Freshness -------------------------------------------------------------
#
# The catalog is generated from the driver files, and the platform installs
# from it: index.json carries a SHA-256 for every file an install fetches, and
# a mismatch is refused rather than warned about. So a driver edit that lands
# without a regenerated catalog does not merely look out of date — it makes
# that driver uninstallable, silently, until somebody notices.
#
# That is not hypothetical: four drivers shipped that way for a day. Nothing
# caught it, because validation checked the *drivers* and never asked whether
# the committed catalog still matched them.
#
# So --check rebuilds into a temporary directory and compares. Regenerating is
# one command, and the failure names it.


def _catalog_snapshot(root: Path) -> dict[str, str]:
    """Every generated catalog file under ``root``, keyed by relative path."""
    paths = [root / "index.json", root / "devices.json"]
    for cat in DRIVER_CATEGORIES:
        paths.append(root / "index" / f"{cat}.json")
        paths.append(root / "devices" / f"{cat}.json")
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in paths
        if path.is_file()
    }


def stale_catalog_files(
    repo_root: Path,
    entries: list[DriverEntry],
    devices: list[dict[str, Any]],
) -> list[str]:
    """Committed catalog files a fresh build would not reproduce.

    Empty when the catalog still matches the driver files it comes from.
    """
    with tempfile.TemporaryDirectory() as tmp:
        write_outputs(Path(tmp), entries, devices)
        fresh = _catalog_snapshot(Path(tmp))
    committed = _catalog_snapshot(repo_root)
    return sorted(name for name in fresh if committed.get(name) != fresh[name])


def _report_stale(stale: list[str]) -> None:
    """Name the catalog files a fresh build would change, and the one fix."""
    # stderr is unbuffered and stdout is not, so without this the failure
    # prints above the validation line that precedes it.
    sys.stdout.flush()
    print(
        f"\nFAILED: the catalog is out of date — {len(stale)} generated "
        f"file(s) no longer match the drivers they come from:\n",
        file=sys.stderr,
    )
    for name in stale:
        print(f"  - {name}", file=sys.stderr)
    print(
        "\nThe catalog is what the platform installs from, and it carries "
        "a checksum\nfor every driver file. A checksum that no longer "
        "matches is refused, not\nwarned about, so a driver whose entry is "
        "stale cannot be installed at all.\n\nRegenerate and commit the "
        "result alongside your driver change:\n\n    python "
        "scripts/build_index.py\n",
        file=sys.stderr,
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


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
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
    if args.json_schema_file is None:
        args.json_schema_file = args.root.resolve() / "avcdriver.schema.json"
    return args


class _BuildInputs(NamedTuple):
    manufacturers: set[str]
    devices_extra: list[dict[str, Any]]
    raw: list[tuple[Path, dict[str, Any]]]


def _load_inputs(repo_root: Path) -> _BuildInputs | None:
    """Everything the build reads besides the schema, in this order; None once
    one of them cannot be read (said why)."""
    try:
        manufacturers = _load_manufacturers(repo_root)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None
    devices_extra = _load_devices_extra(repo_root)
    try:
        raw = collect_drivers(repo_root)
    except ExtractError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return None
    if not raw:
        print(f"ERROR: no driver files found under {repo_root}", file=sys.stderr)
        return None
    return _BuildInputs(manufacturers, devices_extra, raw)


def _entry_or_errors(
    filepath: Path, data: dict[str, Any], repo_root: Path, rel: str
) -> tuple[DriverEntry | None, list[str]]:
    """One driver's catalog entry, or the lines saying why it has none."""
    try:
        return build_entry(filepath, data, repo_root), []
    except ValidationError as e:
        return None, _format_validation_errors(rel, e)
    except Exception as e:
        return None, [f"{rel}: {e}"]


def _check_drivers(
    inputs: _BuildInputs, repo_root: Path, contract: ContractReport
) -> tuple[list[DriverEntry], list[str]]:
    """Every driver's entry and every check's errors, in the order reported.

    Per driver: its entry, the platform's rules for its format, its discovery
    block. Then the checks that need the whole catalog: the entries against
    each other, then the discovery claims.
    """
    entries: list[DriverEntry] = []
    errors: list[str] = []
    discovery_per_driver: list[tuple[str, str, dict[str, Any]]] = []
    for filepath, data in inputs.raw:
        rel = filepath.relative_to(repo_root).as_posix()
        entry, entry_errors = _entry_or_errors(filepath, data, repo_root, rel)
        if entry is not None:
            entries.append(entry)
        errors.extend(entry_errors)
        errors.extend(contract.check(filepath, rel, data))
        disc_errors, normalized = validate_discovery_block(
            rel, data, yaml_dir=filepath.parent,
        )
        errors.extend(disc_errors)
        if normalized:
            discovery_per_driver.append((str(data.get("id") or ""), rel, normalized))

    if entries:
        errors.extend(cross_validate(entries, inputs.manufacturers))
    errors.extend(validate_no_signal_collisions(discovery_per_driver))
    errors.extend(validate_locator_disambiguation(discovery_per_driver))
    return entries, errors


def _report_errors(errors: list[str]) -> None:
    print(f"\nFAILED: {len(errors)} validation error(s):\n", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)


def _devices_or_none(
    entries: list[DriverEntry], devices_extra: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """The devices catalog, or None when it cannot be built (said why)."""
    try:
        return build_devices(entries, devices_extra)
    except ValueError as e:
        print(f"ERROR building devices catalog: {e}", file=sys.stderr)
        return None


def _finish(
    args: argparse.Namespace,
    repo_root: Path,
    entries: list[DriverEntry],
    devices: list[dict[str, Any]],
) -> int:
    """Write the catalog, or under --check say whether the committed one is current.

    --check-json-schema alone is the narrower flag: validate the driver files
    against the schema and stop. Only --check speaks for the whole repo, so
    only it asks whether the committed catalog still matches the drivers.
    """
    if args.check_json_schema and not args.check:
        return 0
    if args.check:
        stale = stale_catalog_files(repo_root, entries, devices)
        if stale:
            _report_stale(stale)
            return 1
        print("Catalog is up to date with the driver files.")
        return 0
    write_outputs(repo_root, entries, devices)
    print(
        f"Wrote index.json, devices.json, and "
        f"{len(DRIVER_CATEGORIES) * 2} category shard files."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root: Path = args.root.resolve()
    validators = _schema_validators(args.json_schema_file, required=args.check_json_schema)
    if validators is None:
        return 1
    inputs = _load_inputs(repo_root)
    if inputs is None:
        return 1

    schema_errors = _validate_all_json_schemas(inputs.raw, validators)
    if args.check_json_schema and _report_schema_errors(schema_errors):
        return 1

    contract = ContractReport()
    entries, errors = _check_drivers(inputs, repo_root, contract)
    # Before any error exit, so what the checks could not decide is visible
    # whether or not the run fails.
    contract.print_notes()
    if errors:
        _report_errors(errors)
        return 1

    devices = _devices_or_none(entries, inputs.devices_extra)
    if devices is None:
        return 1

    # Without --check-json-schema, a driver only the schema refuses fails the
    # run here, last, so it is not reported beside the errors above.
    if _report_schema_errors(schema_errors):
        return 1

    print(f"Validated {len(entries)} driver(s), {len(devices)} device(s).")
    return _finish(args, repo_root, entries, devices)


if __name__ == "__main__":
    sys.exit(main())
