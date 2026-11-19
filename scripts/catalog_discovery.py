"""What only the catalog asks of one driver's ``discovery:`` block, and the stamp it is written with.

Whether a block is valid is the platform's to say, and the catalog says it in
the platform's words: its discovery reader (``read_discovery_block``) is
vendored with the driver contract (``scripts/_vendor/discovery_block.py``) and
judges every block through the contract check (``catalog_contract``): a YAML
definition's walk reads it, and so does a Python driver's verdict. A new
discovery key lands there, and reaches the catalog with the next vendoring.

On top of that sit the catalog's own rules, here:

* ``validate_discovery_block`` requires a declared ``python:`` companion to
  ship beside the driver, prints the reader's warnings, and returns the
  block's normalised form, which ``discovery_claims`` reads to check the
  drivers against each other.
* ``with_discovery_requires`` is the ``requires:`` stamp a catalog entry is
  written with, so an older platform skips a block it would misread
  (``_DISCOVERY_FEATURE_GATES``: a gate per feature that changes the meaning
  of a key an older parser already reads).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from _vendor.discovery_block import read_discovery_block
from _vendor.discovery_hint import DiscoveryHint
from _vendor.version_rule import read_version


# --- One driver's block --------------------------------------------------------


def validate_discovery_block(
    file: str, raw: dict[str, Any], *, yaml_dir: Path | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Return (the catalog's own errors, normalized_discovery) for one driver.

    The block's own problems are the contract check's to report; this adds
    what only publishing needs: a declared ``python:`` companion has to exist
    beside the driver (checked when ``yaml_dir``, the driver's directory, is
    given), or the engine would register two companion signal rules that never
    get any evidence to match. The reader's warnings are printed here.

    The normalised form is what the cross-driver checks read: each
    fingerprint's well-formed entries, whether a tcp_probe, udp_probe and
    python companion are declared (a malformed one still claims its signal,
    and is refused by the contract check), and the hostname patterns. A
    template, or a block that is not a mapping, has none.
    """
    read = read_discovery_block(raw)
    for warning in read.warnings:
        sys.stderr.write(f"warning: {file}: {warning}\n")
    if read.hint is None:
        return [], {}
    return _companion_errors(file, raw, yaml_dir), _normalized(read.hint, raw.get("discovery") or {})


def _companion_errors(file: str, raw: dict[str, Any], yaml_dir: Path | None) -> list[str]:
    discovery = raw.get("discovery")
    file_path = declared_companion(discovery.get("python")) if isinstance(discovery, dict) else None
    if not file_path or yaml_dir is None:
        return []
    companion_path = (yaml_dir / file_path).resolve()
    if companion_path.is_file():
        return []
    return [
        f"{file}: discovery.python.file={file_path!r} but no "
        f"such file exists at {companion_path}; drivers must "
        "ship the sibling _discovery.py companion alongside "
        "the .avcdriver"
    ]


def declared_companion(raw: Any) -> str | None:
    """The companion file a ``python:`` declaration names, or None if it names none.

    A non-empty string is the path; a mapping names it under ``file``. The
    catalog build fetches the same file when it lists what an install of a
    YAML driver downloads (``build_index._installed_file_set``).
    """
    candidate = raw.get("file") if isinstance(raw, dict) else raw
    return candidate if isinstance(candidate, str) and candidate else None


def _normalized(hint: DiscoveryHint, discovery: dict[str, Any]) -> dict[str, Any]:
    """The claims ``discovery_claims`` checks across drivers: each entry as
    the platform's reader made of it, each probe and companion as declared."""
    return {
        "mdns": [{"service": fp.service, "txt": dict(fp.txt)} for fp in hint.mdns],
        "ssdp": [{"device_type": fp.device_type, "fields": dict(fp.fields)} for fp in hint.ssdp],
        "amx_ddp": [{"make": fp.make, "model_pattern": fp.model_pattern} for fp in hint.amx_ddp],
        "has_tcp_probe": "tcp_probe" in discovery,
        "has_udp_probe": "udp_probe" in discovery,
        "has_python": "python" in discovery,
        "hostname": list(hint.hostname),
    }


# --- The `requires` stamp ----------------------------------------------------------
#
# Platform version whose parser first UNDERSTANDS each discovery feature
# below. This matters only for features an older parser silently mis-reads
# rather than rejects — e.g. SSDP description filters: pre-0.23.0 parsers
# ignore the filter fields, collapsing distinct filtered claims into colliding
# unfiltered ones that (pre-0.23.0) abort the whole catalog fold to
# installed-only. The emitted catalog entry gains ``requires: <version>`` — a
# top-level discovery key those parsers reject — so they skip just this
# driver's hints and the rest of the catalog stays live. Platforms >= 0.23.0
# honor ``requires`` directly and skip blocks gated on a newer version than
# they are. Add a (predicate, version) pair here whenever a new discovery
# feature extends the meaning of an existing key.

_SSDP_FILTER_KEYS = ("model", "manufacturer", "friendly_name")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _ssdp_has_description_filters(discovery: dict[str, Any]) -> bool:
    return any(
        isinstance(entry, dict) and any(entry.get(key) is not None for key in _SSDP_FILTER_KEYS)
        for entry in _as_list(discovery.get("ssdp"))
    )


def _probe_declares(discovery: dict[str, Any], kind: str, key: str) -> bool:
    probe = discovery.get(f"{kind}_probe")
    return isinstance(probe, dict) and probe.get(key) is not None


def _probe_has_follow_up(discovery: dict[str, Any]) -> bool:
    return _probe_declares(discovery, "tcp", "then")


def _udp_probe_has_reply_port(discovery: dict[str, Any]) -> bool:
    return _probe_declares(discovery, "udp", "reply_port")


_DISCOVERY_FEATURE_GATES: tuple[tuple[Any, str], ...] = (
    (_ssdp_has_description_filters, "0.23.0"),
    # A `then:` step needs the 0.32.0 probe runner. An older platform rejects
    # the `requires` key and skips this driver's hints rather than mis-reading
    # the probe as a plain one-shot — which would match every SQ as well.
    (_probe_has_follow_up, "0.32.0"),
    # A `reply_port` needs the 0.37.2 probe runner. Without the gate an older
    # parser refuses the unknown key, and with it this driver's whole block,
    # as a malformed one; with it, the block is skipped as written for a newer
    # platform. Either way an older platform uses none of this driver's hints.
    (_udp_probe_has_reply_port, "0.37.2"),
)


def with_discovery_requires(discovery: dict[str, Any]) -> dict[str, Any]:
    """Return the catalog-emission form of a discovery block.

    Stamps/raises ``requires`` to the newest platform version any used
    feature gate demands (keeping a hand-authored ``requires`` when it is
    already newer). Blocks that use no gated features emit unchanged so
    every platform keeps reading them.
    """
    declared = discovery.get("requires")
    needed = declared
    for predicate, version in _DISCOVERY_FEATURE_GATES:
        if predicate(discovery) and _newer(version, needed):
            needed = version
    if needed is None or needed == declared:
        return discovery
    return {"requires": needed, **{
        k: v for k, v in discovery.items() if k != "requires"
    }}


def _newer(version: str, than: Any) -> bool:
    """Whether a gate's ``version`` is newer than the ``requires`` so far
    (none yet counts as older; so does one that is not a version), read by
    the platform's version rule, as its ``requires:`` gate reads it: a
    release candidate is older than its release."""
    if than is None:
        return True
    so_far = read_version(than)
    return so_far is None or read_version(version) > so_far
