"""Every Python example in this repo's guides must parse, and every platform
reference in it must resolve.

``AGENTS.md`` and ``docs/writing-simulators.md`` teach by example, and an agent
or a contributor copies an example verbatim. The declarative half of that — that
a guide's YAML uses real contract fields — is now pinned by the generated
schemas the guides point at rather than by prose. The code half is pinned here,
and by nothing else. Three things went wrong before it existed, and all three
shipped:

* **A helper that does not exist.** A binary-protocol example imported
  ``crc16`` from ``binary_helpers`` for years; the function is ``crc16_ccitt``
  and ``crc16`` never existed. That fails loudly at import, which is the
  *good* case.
* **An async call that is never awaited.** ``writing-simulators.md``'s Level 3
  state-machine example called ``self._schedule_warmup()`` — an ``async def``
  — without awaiting it, so the warm-up it demonstrates never fires. Python
  says nothing at parse time; the coroutine is created and dropped. An author
  copying it gets code that runs, raises nothing, and does not work.
* **An example contradicting the guide's own rule.** One page said "Do not
  override ``connect()``" and listed a hook per lifecycle stage; another told
  a controller author to override ``connect()``, and a third showed them
  building the transport by hand. That is not a style disagreement — the
  platform owns transport construction, so following it produces a driver that
  cannot connect.

So this file asserts three things about every ```python fence:

* **It parses** (``ast.parse``, after the documented elision below).
* **Every platform reference resolves** — ``from server.… / simulator.…
  import X`` against the real module, ``self.<attr>`` against the base class
  the document is about, ``self.transport.<attr>`` against the transport
  classes. This closes both directions: a helper a guide invents, and a method
  the platform renames out from under a guide.
* **No coroutine is dropped, and no example overrides ``connect()`` to append
  a step.**

**The resolution half needs the real platform**, which this repo deliberately
does not install — so it skips when there is none and fails when a CI job
promised one, exactly like ``test_platform_stub_fidelity.py`` (they share
``_platform_probe``). The parse / coroutine / connect checks are stdlib-only
and always run, including in the contributor-shaped job that installs nothing
but ``requirements-dev.txt``.

Three limits, recorded so nobody re-derives them:

* **Resolution is by name, not by signature.** A guide can still pass the
  wrong arguments to a real method.
* **``self.<attr>`` cannot tell platform surface from the example's own
  helper**, so every helper an example invents is listed in
  ``EXAMPLE_HELPERS`` with what it belongs to. That list is the price, and it
  is also the point: adding an entry is a deliberate "mine, not the
  platform's" decision, and a platform rename makes a real reference fall out
  of the platform set and *not* be in the list.
* **The async check only sees a block-local definition.** A dropped call to a
  coroutine defined elsewhere is invisible to it. Widening it would mean
  resolving every attribute call against the platform and asking whether it is
  a coroutine function, which flags every legitimately fire-and-forget call.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from functools import lru_cache
from pathlib import Path

import pytest

from _platform_probe import (
    PLATFORM_ROOT_ENV,
    REQUIRE_PLATFORM_ENV,
    platform_on_path,
    platform_required,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Which base class each guide's ``self`` refers to. A guide about writing
# simulators and a guide about writing drivers both say ``self.set_state``,
# and they mean different classes.
DRIVER_DOCS = ("AGENTS.md",)
SIMULATOR_DOCS = ("docs/writing-simulators.md",)
BOTH_DOCS = ("docs/contributing-drivers.md",)

ALL_DOCS = DRIVER_DOCS + SIMULATOR_DOCS + BOTH_DOCS

# Names an example calls on ``self`` that belong to the example's own driver or
# simulator, not to the platform. Each entry says what it is: an entry here is
# a claim that the platform does NOT own the name, and a wrong claim hides a
# rename.
EXAMPLE_HELPERS = {
    "CMD_INPUT": "A protocol constant the example driver defines on its class.",
    "CMD_POWER": "Same — the example's own protocol constant.",
    "CMD_STATUS": "Same — the example's own protocol constant.",
    "_schedule_transition": "The simulator example's timed-transition helper, "
                            "modelled on the PJLink reference simulator.",
    "_warmup_time": "The simulator example's own configured warm-up duration.",
}

# Calls that consume a coroutine without awaiting it here.
_TASK_FACTORIES = {
    "create_task", "ensure_future", "gather", "wait_for", "shield", "run",
    "run_until_complete", "wait", "as_completed", "run_coroutine_threadsafe",
}

_FENCE = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)
_BARE_ELLIPSIS = re.compile(r"^\s*\.\.\.\s*$")


def _normalize(block: str) -> str:
    """Drop elision markers, but never a legitimate ``...`` body.

    A bare ``...`` line inside a dict or class body means "and the rest goes
    here"; dropping it is what lets the surrounding literal parse. The same
    token directly under a ``def …:`` is the *body* — a real, valid statement
    — and dropping that one would break a block that was fine. The previous
    non-blank line settles which is which.
    """
    out: list[str] = []
    previous = ""
    for line in block.splitlines():
        if _BARE_ELLIPSIS.match(line) and not previous.rstrip().endswith(":"):
            continue
        out.append(line)
        if line.strip():
            previous = line
    return "\n".join(out)


@lru_cache(maxsize=None)
def _doc_text(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.exists(), f"{rel} moved — update the document lists"
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _blocks(rel: str) -> tuple[tuple[int, str], ...]:
    return tuple(
        (n, _normalize(body))
        for n, body in enumerate(_FENCE.findall(_doc_text(rel)), 1)
    )


@lru_cache(maxsize=None)
def _parsed(rel: str) -> tuple[tuple[int, ast.Module], ...]:
    out = []
    for n, src in _blocks(rel):
        try:
            out.append((n, ast.parse(src)))
        except SyntaxError:
            continue
    return tuple(out)


# ── Platform surfaces (resolution half) ─────────────────────────────────────

def _self_assigned_attrs(cls: type) -> set[str]:
    """Instance attributes a class assigns to ``self`` in its own source.

    ``dir(cls)`` sees class-level names only, so ``self.config`` and
    ``self.transport`` — set in ``__init__`` — would read as unresolvable
    without this.
    """
    found: set[str] = set()
    try:
        tree = ast.parse(inspect.getsource(cls))
    except (OSError, TypeError, SyntaxError):
        return found
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            found.add(node.attr)
    return found


def _surface(classes) -> frozenset[str]:
    names: set[str] = set()
    for cls in classes:
        names |= set(dir(cls)) | _self_assigned_attrs(cls)
    return frozenset(names)


@lru_cache(maxsize=1)
def _platform() -> tuple[dict, str]:
    """The platform classes the guides refer to, or the reason they are absent."""
    with platform_on_path():
        try:
            from server.drivers.base import BaseDriver          # noqa: PLC0415
            from server.transport.http_client import HTTPClientTransport  # noqa: PLC0415, E501
            from server.transport.mqtt import MQTTTransport     # noqa: PLC0415
            from server.transport.osc import OSCTransport       # noqa: PLC0415
            from server.transport.serial_transport import SerialTransport  # noqa: PLC0415, E501
            from server.transport.ssh import SSHTransport       # noqa: PLC0415
            from server.transport.tcp import TCPTransport       # noqa: PLC0415
            from server.transport.udp import UDPTransport       # noqa: PLC0415
            from simulator.base import BaseSimulator            # noqa: PLC0415
            from simulator.http_simulator import HTTPSimulator  # noqa: PLC0415
            from simulator.mqtt_simulator import MQTTSimulator  # noqa: PLC0415
            from simulator.osc_simulator import OSCSimulator    # noqa: PLC0415
            from simulator.tcp_simulator import TCPSimulator    # noqa: PLC0415
            from simulator.udp_simulator import UDPSimulator    # noqa: PLC0415
            from simulator.websocket_simulator import WebSocketSimulator  # noqa: PLC0415, E501
        except Exception as exc:                                # noqa: BLE001
            return {}, f"the openavc platform is not importable ({exc})"

    drivers = _surface([BaseDriver])
    simulators = _surface([
        BaseSimulator, TCPSimulator, HTTPSimulator, UDPSimulator,
        OSCSimulator, MQTTSimulator, WebSocketSimulator,
    ])
    transports = _surface([
        TCPTransport, SerialTransport, UDPTransport, HTTPClientTransport,
        OSCTransport, MQTTTransport, SSHTransport,
    ])
    return {
        "driver": drivers,
        "simulator": simulators,
        "both": drivers | simulators,
        "transport": transports,
    }, ""


PLATFORM, _MISSING_REASON = _platform()

if _MISSING_REASON and platform_required():
    raise AssertionError(
        f"{REQUIRE_PLATFORM_ENV}=1 promised the openavc platform, but "
        f"{_MISSING_REASON}. Point {PLATFORM_ROOT_ENV} at the checkout, or "
        f"unset {REQUIRE_PLATFORM_ENV} to state that this run does not pin "
        f"the guides against the platform."
    )

needs_platform = pytest.mark.skipif(
    bool(_MISSING_REASON),
    reason=(
        f"resolving the guides' platform references needs the openavc "
        f"platform: {_MISSING_REASON}. Set {PLATFORM_ROOT_ENV} to its "
        f"checkout, or {REQUIRE_PLATFORM_ENV}=1 to make its absence a failure."
    ),
)


def _surface_for(rel: str) -> frozenset[str]:
    if rel in DRIVER_DOCS:
        return PLATFORM["driver"]
    if rel in SIMULATOR_DOCS:
        return PLATFORM["simulator"]
    return PLATFORM["both"]


# ── AST extractors ──────────────────────────────────────────────────────────

def _import_targets(tree: ast.Module) -> list[tuple[str, str]]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module.split(".")[0] not in ("server", "simulator"):
            continue
        out.extend((node.module, alias.name) for alias in node.names)
    return out


def _block_local_names(tree: ast.Module) -> set[str]:
    local: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local.add(node.name)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            local.add(node.attr)
    return local


def _self_reads(tree: ast.Module) -> set[str]:
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }


def _transport_reads(tree: ast.Module) -> set[str]:
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
        and node.value.attr == "transport"
    }


def _dropped_coroutines(tree: ast.Module) -> list[str]:
    """Calls to a block-local ``async def`` that nothing consumes.

    A coroutine is consumed when it is awaited, handed to a task factory, or
    returned / stored for a caller to await. Anything else evaluates the call
    into a coroutine object that is thrown away — silent at runtime beyond a
    "never awaited" warning nobody sees in a doc example.
    """
    coroutines = {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }
    if not coroutines:
        return []

    consumed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Await):
            consumed.add(id(node.value))
        elif isinstance(node, (ast.Return, ast.Assign, ast.AnnAssign)):
            value = getattr(node, "value", None)
            if value is not None:
                consumed.add(id(value))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else ""
            )
            if name in _TASK_FACTORIES:
                consumed.update(id(arg) for arg in node.args)

    dropped = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in consumed:
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else ""
        )
        if called in coroutines:
            dropped.append(called)
    return dropped


def _connect_overrides_calling_super(tree: ast.Module) -> bool:
    """``async def connect`` that calls ``await super().connect()``.

    A driver that replaces ``connect()`` wholesale (no ``super()`` call) is a
    different, legitimate thing — a transport-less driver has no lifecycle to
    run. But an override that runs the whole platform lifecycle and then bolts
    a step onto the end is exactly what ``_pre_connect`` / ``_post_connect`` /
    ``_initial_sync`` exist to replace, and it is not cosmetic:
    ``super().connect()`` marks the device connected and starts polling, so
    anything after it races the first poll.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "connect":
            continue
        for inner in ast.walk(node):
            func = getattr(inner, "func", None)
            if (
                isinstance(inner, ast.Call)
                and isinstance(func, ast.Attribute)
                and func.attr == "connect"
                and isinstance(func.value, ast.Call)
                and isinstance(func.value.func, ast.Name)
                and func.value.func.id == "super"
            ):
                return True
    return False


# ── Checks that need no platform ────────────────────────────────────────────

@pytest.mark.parametrize("rel", ALL_DOCS)
def test_every_python_example_parses(rel: str) -> None:
    broken = []
    for n, src in _blocks(rel):
        try:
            ast.parse(src)
        except SyntaxError as exc:
            first = next((ln for ln in src.splitlines() if ln.strip()), "")
            broken.append(f"block {n} ({first.strip()!r}): {exc.msg} line {exc.lineno}")
    assert not broken, (
        f"```python examples in {rel} are not valid Python: {broken}. A "
        f"contributor copies an example verbatim, so one that does not parse "
        f"is one they cannot run. A deliberate elision is a bare '...' line, "
        f"which this check drops before parsing — unless it is a function "
        f"body, where it is real."
    )


@pytest.mark.parametrize("rel", ALL_DOCS)
def test_no_example_drops_a_coroutine(rel: str) -> None:
    dropped = {}
    for n, tree in _parsed(rel):
        for name in _dropped_coroutines(tree):
            dropped.setdefault(name, []).append(n)
    assert not dropped, (
        f"in {rel}, these async helpers are called without await and without "
        f"being handed to a task factory, so the coroutine is created and "
        f"thrown away: {dropped}. The example runs, raises nothing, and does "
        f"not do what the surrounding text says it does."
    )


@pytest.mark.parametrize("rel", ALL_DOCS)
def test_no_example_overrides_connect_to_append_a_step(rel: str) -> None:
    offenders = [n for n, tree in _parsed(rel) if _connect_overrides_calling_super(tree)]
    assert not offenders, (
        f"```python blocks {offenders} in {rel} override connect() and call "
        f"super().connect(), which §3.4 tells authors not to do. Anything "
        f"after super().connect() runs after the device is declared connected "
        f"and polling has started — use _pre_connect / _post_connect / "
        f"_initial_sync for the stage you need."
    )


# ── Checks that need the platform ───────────────────────────────────────────

@needs_platform
@pytest.mark.parametrize("rel", ALL_DOCS)
def test_every_platform_import_resolves(rel: str) -> None:
    missing = []
    with platform_on_path():
        for n, tree in _parsed(rel):
            for module, name in _import_targets(tree):
                try:
                    mod = importlib.import_module(module)
                except ImportError:
                    missing.append(f"block {n}: no module {module!r}")
                    continue
                if not hasattr(mod, name):
                    missing.append(f"block {n}: {module}.{name} does not exist")
    assert not missing, (
        f"{rel} imports platform names that are not there: {missing}. Either "
        f"the name was renamed and the guide has to follow, or the example "
        f"invented it."
    )


@needs_platform
@pytest.mark.parametrize("rel", ALL_DOCS)
def test_every_self_reference_resolves(rel: str) -> None:
    surface = _surface_for(rel)
    unresolved = {}
    for n, tree in _parsed(rel):
        local = _block_local_names(tree)
        for attr in sorted(_self_reads(tree)):
            if attr in local or attr in surface or attr in EXAMPLE_HELPERS:
                continue
            unresolved.setdefault(attr, []).append(n)
    assert not unresolved, (
        f"{rel} calls these on self, and the platform base class does not "
        f"have them: {unresolved}. If the platform renamed the method, fix "
        f"the guide; if the example owns the name, add it to EXAMPLE_HELPERS "
        f"with what it is."
    )


@needs_platform
@pytest.mark.parametrize("rel", ALL_DOCS)
def test_every_transport_reference_resolves(rel: str) -> None:
    unresolved = {}
    for n, tree in _parsed(rel):
        for attr in sorted(_transport_reads(tree)):
            if attr not in PLATFORM["transport"]:
                unresolved.setdefault(attr, []).append(n)
    assert not unresolved, (
        f"{rel} calls these on self.transport and no platform transport class "
        f"offers them: {unresolved}."
    )


@needs_platform
def test_the_opt_out_list_holds_nothing_stale() -> None:
    """EXAMPLE_HELPERS may only shrink.

    An entry naming real platform surface is a wrong claim about who owns the
    name, and it would silently absorb a real reference. An entry no guide
    writes any more is dead weight that hides the next rename.
    """
    both = PLATFORM["both"]
    became_platform = sorted(n for n in EXAMPLE_HELPERS if n in both)
    assert not became_platform, (
        f"now real platform surface — delete from EXAMPLE_HELPERS: "
        f"{became_platform}"
    )
    used: set[str] = set()
    for rel in ALL_DOCS:
        for _, tree in _parsed(rel):
            used |= _self_reads(tree)
    unused = sorted(set(EXAMPLE_HELPERS) - used)
    assert not unused, (
        f"EXAMPLE_HELPERS names helpers no guide writes any more: {unused}"
    )


# ── Vacuity guards ──────────────────────────────────────────────────────────

def test_the_sweep_still_reaches_the_documents() -> None:
    """Guard the extractor.

    Every assertion above passes vacuously if the fence sweep stops finding
    anything, and a formatting change could do that silently — a renamed fence
    language, a switch to indented blocks. These are floors on what it must
    still see, per document, so one guide going quiet cannot hide behind the
    others.
    """
    # AGENTS.md's floor is low on purpose. It used to carry the whole Python
    # driver API inline; that reference now lives in the platform's own guide
    # and its generated schema, and what is left here is the worked binary
    # driver in §9.2 plus the test-writing snippets in §8.1. Those are still
    # copied verbatim, which is what this file is for.
    floors = {
        "AGENTS.md": 3,
        "docs/writing-simulators.md": 5,
        "docs/contributing-drivers.md": 2,
    }
    for rel, floor in floors.items():
        blocks = _blocks(rel)
        assert len(blocks) >= floor, (
            f"only {len(blocks)} ```python fences found in {rel} (expected at "
            f"least {floor})"
        )
        assert len(_parsed(rel)) == len(blocks), f"a fence in {rel} stopped parsing"


@needs_platform
def test_the_resolution_sweep_reaches_something() -> None:
    """The resolution half is the part that can quietly check nothing.

    An empty platform surface makes every reference "unresolvable" (loud) —
    but an empty *reference* set makes every check pass (silent). Floor both.
    """
    assert len(PLATFORM["driver"]) > 50, "the BaseDriver surface walk collapsed"
    assert len(PLATFORM["simulator"]) > 50, "the simulator surface walk collapsed"
    assert len(PLATFORM["transport"]) > 50, "the transport surface walk collapsed"

    # Four, and each one is load-bearing: ``BaseDriver`` and
    # ``CallableFrameParser`` in AGENTS.md's worked binary driver, and the TCP
    # and HTTP simulator bases in ``writing-simulators.md``. It was higher when
    # AGENTS.md carried the whole Python driver API inline; that reference moved
    # to the platform's own guide, and with it most of the import lines. The
    # floor is set at what is actually there so a sweep that stops finding
    # anything still fails, which is all this guard was ever for.
    imports = sum(len(_import_targets(t)) for rel in ALL_DOCS for _, t in _parsed(rel))
    assert imports >= 4, f"the import sweep found only {imports} platform imports"

    reads: set[str] = set()
    transport: set[str] = set()
    for rel in ALL_DOCS:
        for _, tree in _parsed(rel):
            reads |= _self_reads(tree)
            transport |= _transport_reads(tree)
    assert len(reads) >= 16, f"the self sweep found only {len(reads)} attributes"
    assert transport, "no guide calls anything on self.transport any more"


def test_the_detectors_can_actually_fire() -> None:
    """Prove the two behavioural detectors on both shapes.

    The coroutine check is the one with no *current* instance to point at once
    the guide is fixed, so a green run says nothing about whether it still
    works. Drive it directly instead of trusting it.
    """
    dropped = ast.parse(
        "async def _schedule_warmup(self):\n    pass\n"
        "def handle_command(self, data):\n    self._schedule_warmup()\n"
    )
    assert _dropped_coroutines(dropped) == ["_schedule_warmup"]

    for consumed in (
        "async def w(self):\n    pass\nasync def g(self):\n    await self.w()\n",
        "async def w(self):\n    pass\ndef g(self):\n    asyncio.ensure_future(self.w())\n",
        "async def w(self):\n    pass\ndef g(self):\n    self._t = asyncio.create_task(self.w())\n",
    ):
        assert _dropped_coroutines(ast.parse(consumed)) == [], consumed

    assert _connect_overrides_calling_super(
        ast.parse("async def connect(self):\n    await super().connect()\n    await self.sync()\n")
    )
    assert not _connect_overrides_calling_super(
        ast.parse("async def connect(self):\n    self._connected = True\n")
    )
    assert not _connect_overrides_calling_super(
        ast.parse("async def _initial_sync(self):\n    await self.sync()\n")
    )


def test_the_elision_rule_keeps_a_real_body() -> None:
    """``...`` means two different things and the rule has to tell them apart.

    Inside a dict it is "and the rest goes here" and must go, or the literal
    will not parse. Under a ``def`` it is the body — a real statement — and
    dropping it turns a valid block into a syntax error, which would then read
    as a broken example.
    """
    elided = 'SIMULATOR_INFO = {\n    ...\n    "delimiter": "\\r",\n}\n'
    assert "..." not in _normalize(elided)
    ast.parse(_normalize(elided))

    body = "def __init__(self, device_id: str, config: dict):\n    ...\n"
    assert "..." in _normalize(body)
    ast.parse(_normalize(body))


def test_the_code_fences_are_balanced() -> None:
    """An odd number of ``` markers inverts every fence after the break.

    Found by an outside audit: the http_listener push example had lost its
    opening fence, so the back half of AGENTS.md was inside-out — YAML examples
    reading as prose and prose reading as YAML. That matters twice over. A
    contributor reads a mangled page, and the ``_FENCE`` sweep above walks
    exactly these fences, so its coverage silently moves to the wrong half of
    the document while every assertion in this file still passes.

    Checked for every author-facing guide in this repo, not just AGENTS.md:
    both broken files had the same defect in the same example, which is what a
    single-document check would have missed.

    Counting is the whole check. It cannot say a fence opens in a sensible
    place, only that they pair up — the failure that actually happened.
    """
    broken = {}
    for rel in ALL_DOCS:
        path = REPO_ROOT / rel
        assert path.exists(), f"{rel} moved — update ALL_DOCS"
        fences = [
            n
            for n, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            )
            if line.startswith("```")
        ]
        if len(fences) % 2:
            broken[rel] = (len(fences), fences[-6:])
    assert not broken, (
        "odd code-fence count — one block is unterminated and every fence "
        f"after it is inverted: {broken}"
    )
