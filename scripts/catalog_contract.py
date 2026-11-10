"""The platform's own rules for one driver file, held to the publishing bar.

Every driver in the catalog is checked by the vendored copy of the platform's
driver contract (``scripts/_vendor/``, kept in step by
``scripts/vendor_platform_contract.py``), so the catalog's verdict on a file
is the platform loader's verdict, in the same words a contributor sees from
``python -m openavc.drivers.check`` and in the server log. On top of those
rules sits the one that only publishing needs: a driver that uses anything
gated must declare ``min_platform_version`` (``_with_catalog_platform_floor``).

The two formats are checked differently because the platform reads them
differently. A YAML definition runs the platform's whole definition check,
strictly, plus its warnings. A Python driver runs the checks that can be
decided from its source text: unknown keys and structure over the WHOLE
``DRIVER_INFO``, never just the catalog's index fields, because a misspelled
key in a runtime block otherwise ships inert.

What a check could not decide is reported, never silent: values the source
reader could not evaluate, cross-references whose target set is computed at
runtime, and the platform's warnings. ``ContractReport`` keeps those for the
whole catalog and prints them once, before the build's errors. A new kind of
note is a list on the report, the place a check fills it, and its line in
``print_notes``; nothing outside this module changes.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _vendor.avcdriver_semantic import (
    platform_version_errors,
    unknown_key_errors,
    validate_driver_definition,
    validate_driver_warnings,
)
from _vendor.python_info import (
    ExtractError,
    extract_python_driver_info_full,
    python_driver_info_issues,
    python_driver_reference_skips,
)


@dataclass
class ContractReport:
    """The platform's verdicts on each driver, and what they left undecided."""

    # The platform's warnings for a YAML driver: the list the Driver Builder
    # and `python -m openavc.drivers.check` show. Printed, never a failure.
    warnings: list[str] = field(default_factory=list)
    # Places a Python driver's DRIVER_INFO could not be read (a value built
    # from a constant, a call or a comprehension): exactly the spots the
    # unknown-key check could not cover.
    unevaluated: list[str] = field(default_factory=list)
    # Cross-references whose TARGET SET is computed: a driver merging its
    # commands in from a module constant has a real target the reader cannot
    # see, so "not in the visible set" would report a working driver as
    # broken. They are skipped, and named, because an unexplained skip and a
    # clean pass look identical from the outside.
    reference_skips: list[str] = field(default_factory=list)

    def check(self, filepath: Path, rel: str, data: dict[str, Any]) -> list[str]:
        """The errors in one driver file; its notes and warnings are kept here."""
        if filepath.suffix == ".avcdriver":
            return self._check_yaml(rel, data)
        return self._check_python(filepath, rel)

    def _check_yaml(self, rel: str, data: dict[str, Any]) -> list[str]:
        # strict: publishing is an authoring gate. A key the contract doesn't
        # declare is a typo, and a typo'd section silently does nothing once
        # the driver is installed.
        errors = [
            f"{rel}: {err}"
            for err in _with_catalog_platform_floor(
                data, validate_driver_definition(data, strict=True)
            )
        ]
        self.warnings.extend(f"{rel}: {w}" for w in validate_driver_warnings(data))
        return errors

    def _check_python(self, filepath: Path, rel: str) -> list[str]:
        # Unknown keys and STRUCTURE, never the cross-reference rules that
        # validate_driver_definition also runs: a Python driver may populate
        # commands and state at runtime, so those false-positive here, while
        # whether the contract declares a key and whether an entry is shaped
        # the way the runtime reads it are decidable without the class. The
        # two checks that need the loaded class are left unanswered; nothing
        # here imports a driver.
        try:
            full_info, opaque_spots = extract_python_driver_info_full(filepath)
        except ExtractError as e:
            return [str(e)]
        errors = [f"{rel}: {err}" for err in unknown_key_errors(full_info)]
        errors += [
            f"{rel}: {err}"
            for err in _with_catalog_platform_floor(
                full_info, python_driver_info_issues(full_info), "python"
            )
        ]
        self.unevaluated.extend(f"{rel}: {spot}" for spot in opaque_spots)
        self.reference_skips.extend(
            f"{rel}: {skip}" for skip in python_driver_reference_skips(full_info)
        )
        return errors

    def print_notes(self) -> None:
        """Say what the checks could not decide: notes to stdout, warnings to stderr."""
        if self.unevaluated:
            # Not a failure: a driver is allowed to compute a value. Printed
            # so the unknown-key check's coverage is visible instead of
            # implied.
            print(
                f"Note: {len(self.unevaluated)} computed value(s) in "
                f"{_files_named(self.unevaluated)} Python driver(s) could not be "
                f"read; keys nested under them are unchecked."
            )
        if self.reference_skips:
            print(
                f"Note: {len(self.reference_skips)} cross-reference group(s) in "
                f"{_files_named(self.reference_skips)} Python driver(s) were not "
                f"checked; the set they resolve against is computed at runtime."
            )
        for warning in self.warnings:
            print(f"warning: {warning}", file=sys.stderr)


def _files_named(lines: list[str]) -> int:
    """How many driver files a list of ``<file>: <detail>`` lines names."""
    return len({line.split(":", 1)[0] for line in lines})


def _with_catalog_platform_floor(
    info: dict[str, Any], errors: list[str], tier: str = "yaml"
) -> list[str]:
    """Add the one min_platform_version rule that is publishing-only.

    The platform's own rules (which this repo runs verbatim) already reject a
    ``min_platform_version`` lower than the fields the driver uses. They stop
    short of requiring the field at all, because a driver written for one job
    on one server has no install gate to satisfy — it is running on the
    platform it was written against.

    A published driver does have one. Omitting the field there means every
    OpenAVC instance is offered the driver, including the releases that will
    read the file, ignore the fields they do not recognise, and run it wrong.
    So the catalog asks the same rule the stricter way, and drops the message
    the platform already produced rather than saying it twice.

    ``tier`` is the driver's format ("yaml" or "python"): a few floors bind a
    YAML driver only, because an older release's YAML loader refuses the
    value while its Python loader runs the driver without it.
    """
    extra = [
        err
        for err in platform_version_errors(
            info, require_declaration=True, tier=tier
        )
        if err not in errors
    ]
    return list(errors) + extra
