"""Replay captured probe responses through each driver's declared matcher.

A driver's ``tcp_probe`` / ``udp_probe`` declares how discovery fingerprints
the device: a port, bytes to send, an ``expect`` / ``expect_hex`` /
``expect_regex`` matcher, and optional ``extract`` rules. This test confirms,
for every driver that ships a captured response under
``tests/fixtures/discovery/<id>.bin`` (or ``.txt``), that the declared matcher
actually hits the capture and each extract rule pulls a value.

Self-contained: it reads the declarations from the built ``index.json`` and
mirrors the matcher/extract semantics of openavc's
``openavc/discovery/probe_runner`` in a few lines of stdlib, so it runs in this
repo's isolated CI (no ``openavc`` install). The probe *engine* itself is tested
generically, with synthetic devices, in the openavc platform repo — this file
validates the *drivers*.

Soft contract: a driver may declare a probe without a fixture (no hardware to
capture from); it's skipped rather than failed. Capture a response to add
coverage — no test-code change needed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "index.json"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "discovery"

# Mirrors openavc/discovery/hints.RESERVED_EXTRACT_KEYS — values that feed the
# manufacturer-alias narrowing path.
RESERVED_EXTRACT_KEYS = {"manufacturer", "make"}


def _fixture_for(driver_id: str) -> Path | None:
    for ext in (".bin", ".txt"):
        candidate = FIXTURE_DIR / f"{driver_id}{ext}"
        if candidate.exists():
            return candidate
    return None


def _matches(payload: bytes, probe: dict) -> bool:
    """Mirror of probe_runner._matches for the declared matcher kinds.

    All declared matchers AND together. ``expect_hex`` is a byte prefix;
    ``expect`` is a substring (bytes first, then latin-1 text); ``expect_regex``
    searches the latin-1 text.
    """
    expect_hex = probe.get("expect_hex")
    if expect_hex:
        prefix = bytes.fromhex(expect_hex.replace(" ", "").replace(":", ""))
        if not payload.startswith(prefix):
            return False
    expect = probe.get("expect")
    if expect:
        if expect.encode("utf-8") not in payload and expect not in payload.decode(
            "latin-1", "replace"
        ):
            return False
    expect_regex = probe.get("expect_regex")
    if expect_regex:
        if not re.search(expect_regex, payload.decode("latin-1", "replace")):
            return False
    return True


def _extract_fields(probe: dict) -> dict[str, object]:
    """field_name -> spec (static str or {regex, group}), incl. the
    ``extract_manufacturer`` sugar that maps to the reserved ``manufacturer``."""
    fields: dict[str, object] = dict(probe.get("extract") or {})
    mfg = probe.get("extract_manufacturer")
    if mfg:
        fields["manufacturer"] = mfg
    return fields


def _collect():
    """(driver_id, kind, probe_block, lowercased aliases) for every declared probe."""
    try:
        index = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for entry in index.get("drivers") or []:
        if not isinstance(entry, dict):
            continue
        disc = entry.get("discovery") or {}
        if not isinstance(disc, dict):
            continue
        aliases = [str(a).strip().lower() for a in disc.get("manufacturer_alias", [])]
        for kind in ("tcp_probe", "udp_probe"):
            probe = disc.get(kind)
            if isinstance(probe, dict) and probe:
                out.append((entry.get("id"), kind, probe, aliases))
    return out


_PROBE_SPECS = _collect()
_WITH_FIXTURE = [s for s in _PROBE_SPECS if _fixture_for(s[0]) is not None]


def test_some_probe_fixtures_are_present():
    """Guard so an accidental fixture-dir wipe surfaces as a failure rather than
    silently turning the replay below into a no-op."""
    assert _WITH_FIXTURE, (
        "No probe fixtures found under tests/fixtures/discovery/. If you removed "
        "them on purpose, remove this test too; otherwise restore the captures."
    )


@pytest.mark.parametrize(
    ("driver_id", "kind", "probe", "aliases"),
    [
        pytest.param(driver_id, kind, probe, aliases, id=f"{driver_id}-{kind}")
        for driver_id, kind, probe, aliases in _WITH_FIXTURE
    ],
)
def test_fixture_matches_declared_probe(driver_id, kind, probe, aliases):
    payload = _fixture_for(driver_id).read_bytes()

    assert _matches(payload, probe), (
        f"{driver_id}: declared {kind} matcher did not hit the captured fixture "
        f"{_fixture_for(driver_id).name!r}."
    )

    text = payload.decode("latin-1", "replace")
    reserved: dict[str, str] = {}
    for name, spec in _extract_fields(probe).items():
        if isinstance(spec, str):
            value = spec
        elif isinstance(spec, dict) and spec.get("regex"):
            m = re.search(spec["regex"], text)
            assert m, f"{driver_id}: extract '{name}' regex did not match the fixture."
            value = m.group(spec.get("group", 1))
        else:
            continue
        assert value, f"{driver_id}: extract '{name}' produced an empty value."
        if name in RESERVED_EXTRACT_KEYS:
            reserved[name] = value

    # Cross-vendor narrowing contract: an extracted manufacturer/make must
    # appear in the driver's declared manufacturer_alias, or peer-driver
    # narrowing can't pick this vendor.
    vendor = reserved.get("manufacturer") or reserved.get("make")
    if aliases and vendor:
        assert vendor.strip().lower() in set(aliases), (
            f"{driver_id}: extracted vendor {vendor!r} is not in manufacturer_alias "
            f"{aliases}; cross-vendor narrowing won't fire."
        )
