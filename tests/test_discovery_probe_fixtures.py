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


# ---------------------------------------------------------------------------
# One captured reply, one driver
# ---------------------------------------------------------------------------
#
# Drivers whose probes ask a device the same thing (same port, same bytes
# sent, same TLS) all receive that device's one reply, so a capture is
# evidence against every one of them. When another driver's matcher also
# hits a driver's capture, a scan of that device offers both drivers, and the
# second one is usually wrong (a sibling product's reply that the probe was
# too loose to tell apart). A capture is only checked against probes that
# send what it was captured in reply to; a probe with a ``then:`` step or a
# ``cert_subject`` is not judged as the other side, since this mirror cannot
# replay either.

# Driver pairs whose probes are meant to match the same device: a scan offers
# both and the integrator picks. Each entry quotes the driver that says so.
INTENDED_OVERLAPS = {
    # panasonic_display: "NTCONTROL is shared with Panasonic projectors, so a
    # projector scan may surface both this driver and panasonic_pt".
    frozenset({"panasonic_display", "panasonic_pt"}),
}


def _send_bytes(probe: dict) -> bytes:
    """The bytes a probe sends (mirrors openavc/discovery/hints)."""
    if probe.get("send_hex"):
        return bytes.fromhex(probe["send_hex"].replace(" ", "").replace(":", ""))
    if probe.get("send_ascii"):
        return probe["send_ascii"].encode("utf-8")
    return b""


def _exchange(kind: str, probe: dict) -> tuple:
    return (kind, probe.get("port"), _send_bytes(probe), bool(probe.get("tls")))


def _same_exchange() -> dict[tuple, list[tuple[str, dict]]]:
    groups: dict[tuple, list[tuple[str, dict]]] = {}
    for driver_id, kind, probe, _aliases in _PROBE_SPECS:
        groups.setdefault(_exchange(kind, probe), []).append((driver_id, probe))
    return {key: drivers for key, drivers in groups.items() if len(drivers) > 1}


_SHARED = _same_exchange()
_SHARED_WITH_FIXTURE = [
    s for s in _WITH_FIXTURE if _exchange(s[1], s[2]) in _SHARED
]


@pytest.mark.parametrize(
    ("driver_id", "kind", "probe"),
    [
        pytest.param(driver_id, kind, probe, id=f"{driver_id}-{kind}")
        for driver_id, kind, probe, _aliases in _SHARED_WITH_FIXTURE
    ],
)
def test_a_captured_reply_matches_no_other_driver_asking_the_same_thing(driver_id, kind, probe):
    fixture = _fixture_for(driver_id)
    payload = fixture.read_bytes()
    also = [
        other
        for other, other_probe in _SHARED[_exchange(kind, probe)]
        if other != driver_id
        and not other_probe.get("then")
        and not other_probe.get("cert_subject")
        and frozenset({driver_id, other}) not in INTENDED_OVERLAPS
        and _matches(payload, other_probe)
    ]

    assert not also, (
        f"{driver_id}: the captured reply {fixture.name!r} also matches the {kind} of "
        f"{also}, which send the same bytes to port {probe.get('port')}. A scan of this "
        "device would offer those drivers too. Make each probe expect something only "
        "its own device says, or, if both drivers are meant to be offered, add the "
        "pair to INTENDED_OVERLAPS with the driver's own words for why."
    )


@pytest.mark.parametrize(
    "pair", [pytest.param(p, id="+".join(sorted(p))) for p in INTENDED_OVERLAPS],
)
def test_an_intended_overlap_names_two_drivers_asking_the_same_thing(pair):
    assert any(
        pair <= {driver_id for driver_id, _probe in drivers} for drivers in _SHARED.values()
    ), (
        f"INTENDED_OVERLAPS lists {sorted(pair)}, but those drivers no longer declare "
        "probes that send the same bytes to the same port. Remove the entry."
    )


# Replies captured from a device whose own driver identifies it some other way
# (SSDP, an OUI), so it has no fixture above to be replayed. Each one is put
# through every probe that sends what it was captured in reply to, and only
# its own driver may match: any other is a driver a scan of that device would
# wrongly offer.
FOREIGN_REPLIES = [
    {
        "device": "Audio-Technica ATDM-0604a, fw 01.03.01, captured 2026-10-05",
        "owner": "at_atdm_0604a",
        "kind": "tcp_probe",
        "port": 17300,
        "sent": b"g_smart_mix O 0000 00 NC 0 \r",
        "reply": b"g_smart_mix 0000 42 NC 0,1,30,0,0,20,10 \r",
    },
]


@pytest.mark.parametrize(
    "capture", FOREIGN_REPLIES, ids=[c["owner"] for c in FOREIGN_REPLIES],
)
def test_a_reply_from_a_device_found_another_way_matches_only_its_own_driver(capture):
    asking = [
        (driver_id, probe)
        for driver_id, kind, probe, _aliases in _PROBE_SPECS
        if kind == capture["kind"]
        and probe.get("port") == capture["port"]
        and _send_bytes(probe) == capture["sent"]
        and not probe.get("tls")
    ]
    assert asking, (
        f"No driver's {capture['kind']} sends {capture['sent']!r} to port "
        f"{capture['port']} any more, so the {capture['device']} capture tests "
        "nothing. Remove the entry."
    )
    also = [
        driver_id
        for driver_id, probe in asking
        if driver_id != capture["owner"]
        and not probe.get("then")
        and not probe.get("cert_subject")
        and _matches(capture["reply"], probe)
    ]
    assert not also, (
        f"The {capture['device']} answered {capture['reply']!r}, which matches the "
        f"{capture['kind']} of {also}. A scan of that device would offer them. Make "
        "each probe expect something only its own device says."
    )
