#!/usr/bin/env python3
"""Vendor the OpenAVC platform's driver-contract modules into scripts/_vendor/.

The platform repo (github.com/open-avc/openavc) owns the driver contract:
the validation rules that decide whether a driver definition is valid, the
constant tables those rules check against, and the fixture corpus that pins
every rejection message. This repo runs the same rules in CI via generated
copies under scripts/_vendor/, so a driver that passes catalog review is
exactly a driver the platform will load — the verdicts cannot drift.

Two modes:

  python scripts/vendor_platform_contract.py [--openavc PATH]
      Regenerate scripts/_vendor/ from a local platform checkout
      (default: the sibling ../openavc directory).

  python scripts/vendor_platform_contract.py --check
      Fetch the platform's current files from GitHub, apply the same
      transform, and fail if any committed copy differs. CI runs this on
      every push and pull request, so a contract change on the platform
      side hard-fails this repo's CI until the copies are regenerated.

The transform is deterministic: line endings are normalized to LF, the
platform's absolute imports are rewritten to package-relative ones, and a
generated-file banner is prepended to the Python modules. Fixture JSON
files are copied byte-for-byte (after line-ending normalization).
"""

from __future__ import annotations

import argparse
import difflib
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "scripts" / "_vendor"
RAW_BASE = "https://raw.githubusercontent.com/open-avc/openavc/main/"

# (platform path, target path relative to this repo's root, import
# rewrites applied exactly once each). Most copies land in scripts/_vendor/;
# the published JSON Schemas land at the repo root, where editors and the
# contributing guide have always pointed. The platform generates the
# schemas from its driver-contract registry (python -m
# server.drivers.contract_gen); this repo byte-copies them.
FILES: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "server/drivers/spec.py",
        "scripts/_vendor/spec.py",
        (),
    ),
    (
        "server/utils/regex_safety.py",
        "scripts/_vendor/regex_safety.py",
        (),
    ),
    (
        "server/drivers/avcdriver_semantic.py",
        "scripts/_vendor/avcdriver_semantic.py",
        (
            ("from server.drivers.spec import (", "from .spec import ("),
            (
                "from server.utils.regex_safety import",
                "from .regex_safety import",
            ),
        ),
    ),
    (
        "tests/fixtures/driver_validation_cases.json",
        "scripts/_vendor/driver_validation_cases.json",
        (),
    ),
    (
        "tests/fixtures/driver_validation_messages.json",
        "scripts/_vendor/driver_validation_messages.json",
        (),
    ),
    (
        "server/drivers/avcdriver.schema.json",
        "avcdriver.schema.json",
        (),
    ),
    (
        "server/drivers/pythondriver.schema.json",
        "pythondriver.schema.json",
        (),
    ),
)

INIT_CONTENT = '''\
"""Generated copies of the OpenAVC platform's driver-contract modules.

Do not edit these files here. The platform repo
(github.com/open-avc/openavc) is the source of truth; regenerate with
``python scripts/vendor_platform_contract.py``.
"""
'''


def _banner(source_path: str) -> str:
    return (
        "# GENERATED FILE - DO NOT EDIT.\n"
        "# Vendored from the OpenAVC platform repo "
        "(github.com/open-avc/openavc),\n"
        f"# source: {source_path}\n"
        "# The platform copy is the source of truth for the driver "
        "contract; CI\n"
        "# fails when this copy drifts from it. To update, run:\n"
        "#     python scripts/vendor_platform_contract.py\n"
        "\n"
    )


def transform(source_path: str, raw: bytes) -> str:
    """Produce the vendored file content for one platform source file."""
    text = raw.decode("utf-8").replace("\r\n", "\n")
    rewrites = next(rw for p, _, rw in FILES if p == source_path)
    for old, new in rewrites:
        count = text.count(old)
        if count != 1:
            raise SystemExit(
                f"ERROR: expected exactly one occurrence of {old!r} in "
                f"{source_path}, found {count} — the platform module's "
                f"import layout changed; update FILES in {__file__}"
            )
        text = text.replace(old, new)
    if source_path.endswith(".py"):
        text = _banner(source_path) + text
    return text


def _read_local(openavc: Path, source_path: str) -> bytes:
    path = openavc / source_path
    if not path.is_file():
        raise SystemExit(
            f"ERROR: {path} not found — pass --openavc pointing at an "
            f"OpenAVC platform checkout"
        )
    return path.read_bytes()


def _fetch_upstream(source_path: str) -> bytes:
    url = RAW_BASE + source_path
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        raise SystemExit(f"ERROR: could not fetch {url}: {exc}")


def _expected_files(read) -> dict[str, str]:
    expected = {"scripts/_vendor/__init__.py": INIT_CONTENT}
    for source_path, target_rel, _ in FILES:
        expected[target_rel] = transform(source_path, read(source_path))
    return expected


def sync(openavc: Path) -> int:
    expected = _expected_files(lambda p: _read_local(openavc, p))
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    changed = 0
    for target_rel, content in expected.items():
        target = REPO_ROOT / target_rel
        current = (
            target.read_bytes().decode("utf-8").replace("\r\n", "\n")
            if target.is_file()
            else None
        )
        if current == content:
            print(f"  unchanged  {target_rel}")
            continue
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        print(f"  wrote      {target_rel}")
        changed += 1
    print(f"{changed} file(s) updated from {openavc}")
    return 0


def check() -> int:
    expected = _expected_files(_fetch_upstream)
    problems: list[str] = []
    for target_rel, content in expected.items():
        target = REPO_ROOT / target_rel
        if not target.is_file():
            problems.append(f"{target_rel}: missing")
            continue
        committed = target.read_bytes().decode("utf-8").replace("\r\n", "\n")
        if committed != content:
            diff = "".join(
                difflib.unified_diff(
                    content.splitlines(keepends=True),
                    committed.splitlines(keepends=True),
                    fromfile=f"platform:{target_rel}",
                    tofile=f"vendored:{target_rel}",
                )
            )
            problems.append(
                f"{target_rel}: differs from the platform copy\n{diff}"
            )
    if problems:
        print(
            "FAILED: the vendored platform-contract files are out of sync "
            "with the OpenAVC platform repo.\n"
            "The platform (github.com/open-avc/openavc) is the source of "
            "truth for the\ndriver contract. Regenerate the vendored "
            "copies and commit the result:\n"
            "    python scripts/vendor_platform_contract.py\n",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"vendored platform contract is in sync ({len(expected)} files).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument(
        "--openavc",
        type=Path,
        default=REPO_ROOT.parent / "openavc",
        help="Path to a local OpenAVC platform checkout (sync mode; "
        "default: the sibling ../openavc)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed copies match the platform's current "
        "files on GitHub (used in CI); writes nothing",
    )
    args = parser.parse_args(argv)
    if args.check:
        return check()
    return sync(args.openavc.resolve())


if __name__ == "__main__":
    sys.exit(main())
