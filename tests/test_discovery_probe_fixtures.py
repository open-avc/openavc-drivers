"""Replay captured probe responses through each driver's declared matcher.

A driver's ``tcp_probe`` / ``udp_probe`` declares how discovery fingerprints
the device: a port, bytes to send, an ``expect`` / ``expect_hex`` /
``expect_regex`` matcher, and optional ``extract`` rules. This test confirms,
for every driver that ships a captured response under
``tests/fixtures/discovery/<id>.bin`` (or ``.txt``), that the declared matcher
actually hits the capture and each extract rule pulls a value.

The declarations are read from the built ``index.json`` by the platform's own
discovery reader, and each reply is judged by the platform's own matcher
(``reply_matches``, ``extract_values``). Both are vendored with the driver
contract under ``scripts/_vendor/``, so this runs in this repo's isolated CI
(no ``openavc`` install) and a capture that passes here is a reply a scan
matches. The probe *engine* itself is tested generically, with synthetic
devices, in the openavc platform repo — this file validates the *drivers*.

Soft contract: a driver may declare a probe without a fixture (no hardware to
capture from); it's skipped rather than failed. Capture a response to add
coverage — no test-code change needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _vendor.discovery_block import read_discovery_block  # noqa: E402
from _vendor.discovery_hint import (  # noqa: E402
    CustomProbeSpec,
    extract_values,
    reply_matches,
)

INDEX = REPO_ROOT / "index.json"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "discovery"

PROBE_KINDS = ("tcp_probe", "udp_probe")


def _fixture_for(driver_id: str) -> Path | None:
    for ext in (".bin", ".txt"):
        candidate = FIXTURE_DIR / f"{driver_id}{ext}"
        if candidate.exists():
            return candidate
    return None


def _index_entries() -> list[dict]:
    try:
        index = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [entry for entry in index.get("drivers") or [] if isinstance(entry, dict)]


def _declared_probes(entry: dict) -> list[str]:
    """The probe kinds an entry's ``discovery:`` block declares."""
    disc = entry.get("discovery")
    if not isinstance(disc, dict):
        return []
    return [kind for kind in PROBE_KINDS if isinstance(disc.get(kind), dict) and disc[kind]]


def _collect():
    """(driver_id, kind, probe, manufacturer aliases) for every declared probe
    the platform's reader reads, and the (driver_id, kind) of any it does not."""
    read_ok, unread = [], []
    for entry in _index_entries():
        kinds = _declared_probes(entry)
        if not kinds:
            continue
        hint = read_discovery_block(entry).hint
        for kind in kinds:
            probe = getattr(hint, kind) if hint is not None else None
            if probe is None:
                unread.append((entry.get("id"), kind))
            else:
                read_ok.append((entry.get("id"), kind, probe, hint.manufacturer_alias))
    return read_ok, unread


_PROBE_SPECS, _UNREAD = _collect()
_WITH_FIXTURE = [s for s in _PROBE_SPECS if _fixture_for(s[0]) is not None]


def test_some_probe_fixtures_are_present():
    """Guard so an accidental fixture-dir wipe surfaces as a failure rather than
    silently turning the replay below into a no-op."""
    assert _WITH_FIXTURE, (
        "No probe fixtures found under tests/fixtures/discovery/. If you removed "
        "them on purpose, remove this test too; otherwise restore the captures."
    )


def test_every_declared_probe_is_read():
    """A probe the platform's reader cannot read would drop out of every replay
    below without a word, so it fails here instead."""
    assert not _UNREAD, (
        f"The platform's discovery reader reads no probe from {_UNREAD}. Run "
        "python scripts/build_index.py, which names what is wrong with the block."
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

    assert reply_matches(payload, probe.response_match), (
        f"{driver_id}: declared {kind} matcher did not hit the captured fixture "
        f"{_fixture_for(driver_id).name!r}."
    )

    reserved, extracted = extract_values(payload, probe.extract)
    pulled = {**reserved, **extracted}
    for rule in probe.extract:
        assert rule.field_name in pulled, (
            f"{driver_id}: extract '{rule.field_name}' regex did not match the fixture."
        )
        assert pulled[rule.field_name], (
            f"{driver_id}: extract '{rule.field_name}' produced an empty value."
        )

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
# ``cert_subject`` is not judged as the other side, since a capture holds
# neither the second reply nor the certificate.

# Driver pairs whose probes are meant to match the same device: a scan offers
# both and the integrator picks. Each entry quotes the driver that says so.
INTENDED_OVERLAPS = {
    # panasonic_display: "NTCONTROL is shared with Panasonic projectors, so a
    # projector scan may surface both this driver and panasonic_pt".
    frozenset({"panasonic_display", "panasonic_pt"}),
    # extron_dtp_crosspoint: "extron_sis's probe reads any Extron banner and
    # also matches; the two tie on driver id and this one sorts first, so a
    # scan offers it first."
    frozenset({"extron_dtp_crosspoint", "extron_sis"}),
}


def _exchange(kind: str, probe: CustomProbeSpec) -> tuple:
    return (kind, probe.port, probe.send, probe.tls)


def _judged_on_the_reply_alone(probe: CustomProbeSpec) -> bool:
    return probe.follow_up is None and probe.cert_subject is None


def _same_exchange() -> dict[tuple, list[tuple[str, CustomProbeSpec]]]:
    groups: dict[tuple, list[tuple[str, CustomProbeSpec]]] = {}
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
        and _judged_on_the_reply_alone(other_probe)
        and frozenset({driver_id, other}) not in INTENDED_OVERLAPS
        and reply_matches(payload, other_probe.response_match)
    ]

    assert not also, (
        f"{driver_id}: the captured reply {fixture.name!r} also matches the {kind} of "
        f"{also}, which send the same bytes to port {probe.port}. A scan of this "
        "device would offer those drivers too. Make each probe expect something only "
        "its own device says, or, if both drivers are meant to be offered, add the "
        "pair to INTENDED_OVERLAPS with the driver's own words for why."
    )


@pytest.mark.parametrize(
    # Sorted: a set's order differs between processes, and pytest-xdist
    # refuses a run whose workers collect the parameters in different orders.
    "pair", [pytest.param(p, id="+".join(sorted(p))) for p in sorted(INTENDED_OVERLAPS, key=sorted)],
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
        if _exchange(kind, probe) == (capture["kind"], capture["port"], capture["sent"], False)
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
        and _judged_on_the_reply_alone(probe)
        and reply_matches(capture["reply"], probe.response_match)
    ]
    assert not also, (
        f"The {capture['device']} answered {capture['reply']!r}, which matches the "
        f"{capture['kind']} of {also}. A scan of that device would offer them. Make "
        "each probe expect something only its own device says."
    )
