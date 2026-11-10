"""The catalog's discovery checks across drivers.

Two rules need the whole catalog rather than one driver's block:
``validate_no_signal_collisions`` refuses two drivers claiming the same
fingerprint, and ``validate_locator_disambiguation`` requires drivers that
share a hostname pattern to each carry a fingerprint that runs before install.

Both read the normalised form ``catalog_discovery.validate_discovery_block``
returns for each driver: ``mdns`` ({service, txt}), ``ssdp`` ({device_type,
fields}) and ``amx_ddp`` ({make, model_pattern}) as lists of the well-formed
entries, ``has_tcp_probe``, ``has_udp_probe`` and ``has_python`` as
declared-or-not, and ``hostname`` as the well-formed patterns. A new
fingerprint kind is a new key there, its claim in ``_signal_claims`` here, and
a place in ``_DECLARATIVE_FINGERPRINTS`` when the engine evaluates it straight
from the catalog, before the driver is installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


# --- Across the catalog: fingerprint claims ---------------------------------------
#
# ``per_driver`` is (driver id, file, normalised block) for every driver whose
# block produced a normalised form, in catalog order.

# One claim on a signal: the driver, its file, and the filter it narrows the
# signal with (TXT pairs or description fields, keys lower-cased).
_Claim = tuple[str, str, frozenset]


def _signal_claims(
    driver_id: str, normalized: dict[str, Any],
) -> Iterator[tuple[str, str, dict[str, str] | None]]:
    """Every signal one driver's block claims, as (kind, source id, filter).

    The kinds and source ids are the ones the platform registers its signal
    rules under, so two claims here meet exactly where they would there.
    """
    for entry in normalized.get("mdns", []):
        yield "mdns", entry["service"].lower().rstrip(".") + ".", entry.get("txt")
    for entry in normalized.get("ssdp", []):
        yield "ssdp", entry["device_type"], entry.get("fields")
    for amx in normalized.get("amx_ddp", []):
        yield "amx_ddp", f"{amx['make']}/{amx['model_pattern']}", None
    if normalized.get("has_tcp_probe"):
        yield "probe", f"custom_{driver_id}_tcp", None
    if normalized.get("has_udp_probe"):
        yield "broadcast", f"custom_{driver_id}_udp", None
    if normalized.get("has_python"):
        yield "broadcast", f"custom_{driver_id}_companion_udp", None
        yield "probe", f"custom_{driver_id}_companion_tcp", None


def _claim_conflict(signal: str, prior: _Claim, claim: _Claim) -> str | None:
    """Why ``claim`` cannot share ``signal`` with an earlier ``prior``, or None.

    The same filter twice is a collision; an unfiltered claim beside a
    filtered one would shadow it, whichever came first.
    """
    prior_driver, prior_file, prior_filter = prior
    driver_id, file, filter_set = claim
    if prior_filter == filter_set:
        return (
            f"Signal collision: {signal} claimed by "
            f"both {prior_driver!r} ({prior_file}) and "
            f"{driver_id!r} ({file})"
        )
    if not prior_filter:
        return (
            f"Signal collision: {signal} — {prior_driver!r} "
            f"({prior_file}) claims it without a TXT filter, which "
            f"would shadow {driver_id!r}'s filtered claim ({file})"
        )
    if not filter_set:
        return (
            f"Signal collision: {signal} — {driver_id!r} "
            f"({file}) claims it without a TXT filter, which would "
            f"shadow {prior_driver!r}'s filtered claim ({prior_file})"
        )
    return None


def _claim(holders: list[_Claim], signal: str, claim: _Claim) -> str | None:
    """Add ``claim`` to a signal's ``holders``, or say why it cannot join them.

    A driver claiming a signal it already holds changes nothing. A claim that
    conflicts with an earlier holder is refused and not recorded, so a later
    claim is judged against the earlier holders only.
    """
    for prior in holders:
        if prior[0] == claim[0]:
            return None
        problem = _claim_conflict(signal, prior, claim)
        if problem:
            return problem
    holders.append(claim)
    return None


def validate_no_signal_collisions(
    per_driver: list[tuple[str, str, dict[str, Any]]],
) -> list[str]:
    """Cross-driver: refuse two drivers claiming the same fingerprint."""
    errors: list[str] = []
    holders: dict[tuple[str, str], list[_Claim]] = {}
    for driver_id, file, normalized in per_driver:
        for kind, source_id, txt_filter in _signal_claims(driver_id, normalized):
            filter_set = frozenset((k.lower(), str(v)) for k, v in (txt_filter or {}).items())
            problem = _claim(
                holders.setdefault((kind, source_id), []),
                f"{kind}:{source_id}",
                (driver_id, file, filter_set),
            )
            if problem:
                errors.append(problem)
    return errors


# --- Across the catalog: drivers that share a hostname ----------------------------

# Pre-existing shared-hostname drivers that predate this check and can't be
# given a declarative disambiguator without hardware we don't have. Grandfathered
# so the rule is a hard error for NEW occurrences while these stay listed here
# for cleanup. birddog_ptz / birddog_codec both default to the
# "^birddog-" hostname with only soft signals (oui + NDI, deliberately not an
# mdns claim since _ndi._tcp is cross-vendor); telling a PTZ from a codec
# pre-install needs a probe of the BirdDog HTTP /about API, which needs a
# capture from real gear. Remove an id here once it carries a real fingerprint.
_KNOWN_LOCATOR_AMBIGUITY: frozenset[str] = frozenset({
    "birddog_ptz", "birddog_codec",
})

# The fingerprints the engine evaluates straight from the catalog, before
# anything is installed. A python: companion is not one of them.
_DECLARATIVE_FINGERPRINTS = ("has_tcp_probe", "has_udp_probe", "mdns", "ssdp", "amx_ddp")


def _hostname_claimants(
    per_driver: list[tuple[str, str, dict[str, Any]]],
) -> dict[str, dict[str, tuple[str, dict[str, Any]]]]:
    """Hostname pattern (lower-cased) -> {driver id: (file, normalised block)}.

    Keyed by driver id so a driver listing the same pattern twice counts once,
    and a pattern claimed by a single driver never trips the check.
    """
    by_host: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
    for driver_id, file, norm in per_driver:
        for pattern in norm.get("hostname", []):
            by_host.setdefault(pattern.lower(), {})[driver_id] = (file, norm)
    return by_host


def _undistinguished(
    per_driver: list[tuple[str, str, dict[str, Any]]],
) -> Iterator[tuple[str, str, str, list[str]]]:
    """(pattern, driver id, file, the others sharing it) for every driver that
    shares a hostname pattern and has nothing to tell it from them before install.
    In pattern order, then driver order."""
    shared = [
        (pattern, claimants)
        for pattern, claimants in sorted(_hostname_claimants(per_driver).items())
        if len(claimants) > 1
    ]
    for pattern, claimants in shared:
        for driver_id, (file, norm) in sorted(claimants.items()):
            if _needs_a_fingerprint(driver_id, norm):
                yield pattern, driver_id, file, sorted(d for d in claimants if d != driver_id)


def _needs_a_fingerprint(driver_id: str, norm: dict[str, Any]) -> bool:
    """No declarative fingerprint, and not one of the grandfathered drivers."""
    if any(norm.get(key) for key in _DECLARATIVE_FINGERPRINTS):
        return False
    return driver_id not in _KNOWN_LOCATOR_AMBIGUITY


def validate_locator_disambiguation(
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
    return [
        f"Locator ambiguity: {driver_id!r} ({file}) shares the "
        f"hostname pattern {pattern!r} with {others} but declares no "
        f"pre-install fingerprint (tcp_probe / udp_probe / mdns / "
        f"ssdp / amx_ddp). An uninstalled device matching that "
        f"hostname can't be told apart from its siblings; a python: "
        f"companion only runs once the driver is installed, so it "
        f"can't disambiguate first. Add a declarative fingerprint "
        f"(e.g. a tcp_probe matching this driver's banner token)."
        for pattern, driver_id, file, others in _undistinguished(per_driver)
    ]
