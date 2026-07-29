"""AGENTS.md must describe the contract the vendored registry declares.

``AGENTS.md`` is what an AI agent reads before writing a driver for this
repository, and it is a hand-written description of the same .avcdriver
contract ``scripts/_vendor/spec.py`` carries. Nothing else pins the two
together: the vendored copy is byte-compared against the platform by CI, the
JSON schemas are generated, but the guide is prose and prose drifts silently.
When it does, an agent writes exactly what the guide describes and produces a
driver the catalog refuses — or one that loads and quietly does nothing.

So this is a floor, not a spec check. It asserts the two things that go wrong:

* **A field nobody documented.** Every field name in the registry appears
  somewhere in the guide, so a new contract field has to be written up when it
  arrives (or say here why it is deliberately left to another document).
* **A documented field that no longer exists.** Every key the guide presents as
  a driver field resolves in the registry — the ones it names in prose
  (``options_state: <key>`` in a bullet) and the ones its YAML examples use.
  An example is the most dangerous place for a stale key, because it gets
  copied verbatim.

Deliberately NOT generating the prose. Generated documentation reads like a
schema dump, so nobody reads it, and the judgement that makes this guide
useful — when to reach for a field, what it pairs with, what it costs — cannot
come out of a registry. The floor buys the mechanical half only.

The walk below duplicates the platform's own registry walk rather than
importing it, for the same reason ``scripts/_vendor/`` exists at all: this
repo's CI installs no ``openavc``. It is ~30 lines and the guard at the bottom
fails if it stops reaching every depth.

Three limits, recorded so nobody re-derives them:

* **"Documented" here means "mentioned".** A field named once in an unrelated
  sentence passes. This catches the field nobody wrote up, which is the
  recurring failure; it cannot judge whether the writing is any good.
* **The prose sweep only sees key-shaped mentions** — a name in backticks
  followed by a colon, which is how this guide introduces a field. A stale
  field mentioned only as bare prose is invisible to it. Widening the pattern
  to every backticked token drowns the signal in command names and state keys.
* **The YAML sweep only walks fences it can root** as a driver definition. A
  fragment rooted deeper is skipped, and the skip count is asserted so the
  coverage cannot quietly fall to nothing.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _vendor import spec  # noqa: E402

DOC_PATH = REPO_ROOT / "AGENTS.md"

# Contract fields the guide deliberately does not describe. Each entry says
# why, so a wrong one is visible rather than inherited. Keep this short: a
# field a contributor can set but cannot read about is a documentation bug,
# not an exemption.
UNDOCUMENTED = {
    "inline_protocol": "Built-in generic drivers only — it turns on the "
                       "no-code editor on a device page. A driver submitted "
                       "here ships its protocol in the driver file, so a "
                       "contributor never sets it.",
}

# Key-shaped tokens the guide writes that the registry has no field for. Each
# entry says what the name actually belongs to; without them the prose sweep
# reports every device-config key and Python attribute the guide mentions.
# Most are not driver-file keys at all. The exception is a key inside a block
# the registry deliberately leaves open — the simulator's state-machine
# transitions are checked by the platform's simulator validator, not by the
# registry — and that entry says so, because an entry here is a claim about
# the contract's shape and a wrong one hides a real cut field.
NOT_A_CONTRACT_KEY = {
    "connected": "A Python driver attribute (self.connected), not a "
                 "definition key.",
    "reject": "A simulator state-machine transition key. The registry models "
              "that a machine has transitions, not what one contains; the "
              "platform's simulator validator checks their shape.",
    "power": "A state variable name, used in the example that explains how "
             "the simulator infers a command's effect from its name.",
    "tcp_keepalive": "A default_config key that turns on OS-level socket "
                     "keepalive.",
}

_FENCE = re.compile(r"^```yaml\n(.*?)^```", re.DOTALL | re.MULTILINE)

# A field the guide introduces, written the way it writes one:
# `name:` or `name: value` inside backticks.
_KEY_IN_TICKS = re.compile(r"`([a-z][a-z0-9_]*):(?:\s|`)")

_CHILD_NODE_KEYS = ("extra", "prop_names", "items")
_COMBINATOR_KEYS = ("one_of", "any_of", "all_of")


def _walk_registry(node: object, names: set) -> None:
    """Collect field names from one registry node, depth-first.

    Names come only from the keys of a ``fields`` dict; every other key in a
    node is registry metadata ('type', 'doc', 'enum', ...). Mirrors the
    platform's own walk, including the raw-fragment branch, so the two agree
    on what the contract declares.
    """
    if not isinstance(node, dict):
        return
    fields = node.get("fields")
    if isinstance(fields, dict):
        for name, child in fields.items():
            names.add(name)
            _walk_registry(child, names)
    for key in _CHILD_NODE_KEYS:
        _walk_registry(node.get(key), names)
    for key in _COMBINATOR_KEYS:
        for branch in node.get(key, ()):
            _walk_registry(branch, names)
    raw = node.get("raw")
    if isinstance(raw, dict):
        for fragment in (raw, raw.get("if"), raw.get("then")):
            if isinstance(fragment, dict):
                properties = fragment.get("properties")
                if isinstance(properties, dict):
                    names.update(properties)


@lru_cache(maxsize=1)
def _contract_field_names() -> frozenset:
    names: set = set()
    for name, node in spec.FIELDS.items():
        names.add(name)
        _walk_registry(node, names)
    for node in spec.DEFS.values():
        _walk_registry(node, names)
    return frozenset(names)


@lru_cache(maxsize=1)
def _doc_text() -> str:
    text = DOC_PATH.read_text(encoding="utf-8")
    assert len(text) > 50_000, "AGENTS.md went missing — the path is wrong"
    return text


def _documents(name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", _doc_text()) is not None


@lru_cache(maxsize=1)
def _keys_named_in_prose() -> frozenset:
    return frozenset(_KEY_IN_TICKS.findall(_doc_text()))


def _resolve(node: object) -> dict:
    """Follow a registry $ref into DEFS."""
    for _ in range(10):
        if not (isinstance(node, dict) and "ref" in node):
            break
        node = spec.DEFS.get(node["ref"], {})
    return node if isinstance(node, dict) else {}


def _walk_example(value: object, node: object, path: str, unknown: list, seen: set) -> None:
    """Walk one YAML example beside the registry, flagging keys it cannot place.

    Only descends where the registry says the keys are fixed. An open map
    (``commands``, ``state_variables``, ``default_config`` …) holds
    author-chosen names, so its keys are skipped and only its values are
    followed into the value node the registry declares for them.
    """
    node = _resolve(node)
    if isinstance(value, dict):
        fields = node.get("fields")
        extra = node.get("extra")
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            if isinstance(fields, dict):
                if key in fields:
                    seen.add(key)
                    _walk_example(child, fields[key], f"{path}.{key}", unknown, seen)
                elif isinstance(extra, dict):
                    _walk_example(child, extra, f"{path}.*", unknown, seen)
                else:
                    unknown.append(f"{path}.{key}".lstrip("."))
            elif isinstance(extra, dict):
                _walk_example(child, extra, f"{path}.*", unknown, seen)
    elif isinstance(value, list):
        items = node.get("items")
        if isinstance(items, dict):
            for item in value:
                _walk_example(item, items, f"{path}[]", unknown, seen)


@lru_cache(maxsize=1)
def _example_sweep():
    """(keys the examples use that the contract has no place for, keys
    checked, fences rooted, fences skipped)."""
    unknown: list = []
    seen: set = set()
    rooted = skipped = 0
    root = {"fields": spec.FIELDS}
    for body in _FENCE.findall(_doc_text()):
        try:
            example = yaml.safe_load(body)
        except yaml.YAMLError:
            skipped += 1
            continue
        if not isinstance(example, dict) or not set(example) & set(spec.FIELDS):
            skipped += 1  # not a driver definition (a fragment, a project snippet)
            continue
        rooted += 1
        _walk_example(example, root, "", unknown, seen)
    return tuple(sorted(set(unknown))), frozenset(seen), rooted, skipped


def test_every_contract_field_is_documented():
    missing = sorted(
        name
        for name in _contract_field_names()
        if name not in UNDOCUMENTED and not _documents(name)
    )
    assert not missing, (
        "declared in the driver contract but never mentioned in AGENTS.md: "
        f"{missing}. Write the field up, or — if the guide deliberately "
        "leaves it to another document — add it to UNDOCUMENTED with the "
        "reason."
    )


def test_every_key_the_guide_names_is_a_real_contract_field():
    stale = sorted(
        key
        for key in _keys_named_in_prose()
        if key not in NOT_A_CONTRACT_KEY and key not in _contract_field_names()
    )
    assert not stale, (
        "AGENTS.md introduces these as driver fields, but the contract does "
        f"not declare them: {stale}. If the field was cut from the platform, "
        "cut it from the guide too; if the name belongs to something else (a "
        "config key, a Python attribute), add it to NOT_A_CONTRACT_KEY with "
        "what it is."
    )


def test_every_yaml_example_uses_only_contract_keys():
    unknown, _, _, _ = _example_sweep()
    assert not unknown, (
        f"YAML examples in AGENTS.md use keys the contract has no place for: "
        f"{list(unknown)}. An agent copies an example verbatim, so a key here "
        f"that the catalog refuses ships as a rejected submission."
    )


def test_the_opt_out_lists_hold_nothing_stale():
    """Both lists may only shrink.

    Documenting a field makes its UNDOCUMENTED entry untrue, and a list that
    keeps untrue entries is how exemptions become the place fields get parked.
    Failing the moment an entry stops being true means it has to go in the
    same change that makes it stale.
    """
    now_documented = sorted(name for name in UNDOCUMENTED if _documents(name))
    assert not now_documented, (
        f"now described in AGENTS.md — delete from UNDOCUMENTED: {now_documented}"
    )
    names = _contract_field_names()
    unknown_exemption = sorted(set(UNDOCUMENTED) - names)
    assert not unknown_exemption, (
        "UNDOCUMENTED names fields the contract does not declare (renamed or "
        f"removed?): {unknown_exemption}"
    )
    became_real = sorted(key for key in NOT_A_CONTRACT_KEY if key in names)
    assert not became_real, (
        f"now real contract fields — delete from NOT_A_CONTRACT_KEY: {became_real}"
    )
    unused = sorted(set(NOT_A_CONTRACT_KEY) - _keys_named_in_prose())
    assert not unused, (
        f"NOT_A_CONTRACT_KEY names tokens AGENTS.md no longer writes: {unused}"
    )


def test_the_sweeps_still_reach_the_document():
    """Guard the extractors themselves.

    Every assertion above passes vacuously if a sweep stops finding anything,
    and a formatting change to the guide could do that silently — a renamed
    fence language, a switch from backticks to bold. These are floors on what
    each sweep must still see.
    """
    assert len(_contract_field_names()) > 150, "the registry walk collapsed"

    prose = _keys_named_in_prose()
    assert len(prose) > 30, f"the prose sweep found only {len(prose)} keys"
    for name in ("transport", "options_state", "child_entity_types"):
        assert name in prose or _documents(name), f"AGENTS.md stopped naming {name}"

    unknown, checked, rooted, skipped = _example_sweep()
    assert rooted > 20, f"only {rooted} YAML examples rooted as driver definitions"
    assert len(checked) > 50, f"the example sweep placed only {len(checked)} keys"
    # Fences it cannot root are fine and expected (fragments, project files);
    # this only pins that rooting has not silently become the rare case.
    assert skipped < rooted, f"{skipped} fences skipped vs {rooted} rooted"
