"""The catalog's checksums must describe the bytes GitHub will serve.

Every driver entry in index.json carries a SHA-256 per installed file. The
platform downloads each file from raw.githubusercontent - which serves the git
blob - and refuses the install when the hash does not match. So a checksum
taken from anything other than the blob makes that driver uninstallable, and
because the check is fail-closed the failure is silent from the catalog's side:
the build is green, CI is green, and the install just says no.

That is not hypothetical. On 2026-08-09 the catalog was regenerated on Windows,
where core.autocrlf rewrites LF to CRLF on checkout, and every one of the 93
drivers got a checksum no download could match. .gitattributes now pins these
files to LF so a checkout equals the blob on every OS, and build_index.py
refuses to hash a file containing CRLF.

This test states the invariant independently of the generator, because the
generator is the thing most likely to be edited. It needs no `openavc` install
and no network - it reads the repo it lives in.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CATEGORY_DIRS = (
    "projectors", "displays", "switchers", "audio", "cameras",
    "video", "lighting", "power", "streaming", "utility",
)


def _driver_files() -> list[Path]:
    out: list[Path] = []
    for name in CATEGORY_DIRS:
        d = REPO / name
        if not d.is_dir():
            continue
        out.extend(sorted(p for p in d.rglob("*.avcdriver") if p.is_file()))
        out.extend(sorted(p for p in d.rglob("*.py") if p.is_file()))
    return out


def test_no_driver_file_has_crlf_line_endings() -> None:
    """A CRLF driver file hashes to something no download can match."""
    offenders = [
        p.relative_to(REPO).as_posix()
        for p in _driver_files()
        if b"\r\n" in p.read_bytes()
    ]
    assert not offenders, (
        "These files have CRLF line endings, so their catalog checksums would "
        "not match the bytes GitHub serves and the platform would refuse to "
        "install them:\n  "
        + "\n  ".join(offenders)
        + "\n\nRenormalize the checkout:\n"
        "    git add --renormalize .\n"
        "    git rm -r --cached . && git reset --hard\n"
        "    python scripts/build_index.py"
    )


def test_catalog_checksums_match_the_files_on_disk() -> None:
    """Every hash in index.json is the hash of the file it names.

    With the CRLF invariant above holding, the working copy is the blob, so
    this is also the hash the installer will compute after downloading.
    """
    index = json.loads((REPO / "index.json").read_text(encoding="utf-8"))
    mismatches: list[str] = []
    checked = 0
    for driver in index["drivers"]:
        for rel, expected in (driver.get("files") or {}).items():
            path = REPO / rel
            if not path.is_file():
                mismatches.append(f"{rel}: listed in the catalog but missing")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            checked += 1
            if actual != expected:
                mismatches.append(
                    f"{rel}: catalog says {expected[:12]}..., file is {actual[:12]}..."
                )
    assert checked, "no file checksums found in index.json - has the format changed?"
    assert not mismatches, (
        "The catalog does not describe these files. Regenerate it with "
        "`python scripts/build_index.py` and commit the result:\n  "
        + "\n  ".join(mismatches)
    )


@pytest.mark.parametrize("name", ["*.avcdriver", "*.py", "*.json"])
def test_gitattributes_pins_the_hashed_file_types_to_lf(name: str) -> None:
    """The invariant above is only free because .gitattributes enforces it.

    Without these rules a Windows checkout silently reintroduces CRLF, and the
    next catalog rebuild bakes it into every checksum.
    """
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert f"{name} text eol=lf" in attrs, (
        f"'{name} text eol=lf' is missing from .gitattributes. Removing it "
        f"lets a checkout differ from the blob, which is how the catalog's "
        f"checksums went wrong on 2026-08-09."
    )
