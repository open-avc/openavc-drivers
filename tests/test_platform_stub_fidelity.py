"""Pins ``tests/_platform_stubs.py`` against the real OpenAVC platform.

Every driver test in this repo runs against stand-ins, because the community CI
installs ``requirements-dev.txt`` and has no ``openavc`` package. That makes the
stand-ins load-bearing in a way that is easy to miss: a hand-written stub cannot
disagree with the person who wrote it, so a green suite confirms the author's
belief about the platform, not the platform. Three separate divergences shipped
that way and none of them failed a test:

  * five copies of ``CallableFrameParser`` discarded the buffer a parse function
    returned with no message -- the behaviour the platform had already stopped
    having -- so a driver's resync path was tested against a parser that wedges;
  * one ``set_state`` wrote the bare property name, without the
    ``device.<id>.`` prefix the real ``BaseDriver`` applies, and nothing noticed
    because every read went back through the same stub;
  * four copies of the typed connection fault put the code on ``.code``, an
    attribute the platform does not have (it reads ``.fault_code``), so nine
    assertions across three files were checking the stub.

This module is the check those needed. It has two halves:

  **Shape.** For every stubbed class, every attribute the stub defines is
  resolved on the real class and its signature compared exactly -- parameter
  names, order, kind (positional / keyword-only), and whether each has a
  default. Constants are compared by value. A stub attribute the real class
  does not have is a failure: it is invented surface a driver could come to
  depend on.

  **Behaviour.** Shape is not enough for the divergences that actually shipped:
  ``set_state(key, value)`` has the same signature whether or not it namespaces
  the key. So the classes where semantics is what bites are driven side by side
  against the real ones -- identical byte sequences through both frame parsers,
  identical calls through both drivers -- and the resulting state compared.

**Coverage is the number that matters, not the pass.** A comparison that
silently skips a method is a comparison that passes, so nothing is skipped
quietly: every stub attribute lands in exactly one of *compared*,
*allowlisted* (with a written reason, and the allowlist is checked for stale
entries), or *unreachable* -- and unreachable is a failure. The per-class
counts print with ``-s``.

**Running it.** The platform is found through ``OPENAVC_PLATFORM_ROOT``, or
beside this repo in the workspace, or wherever ``openavc`` is already
importable. Absent, the whole module skips -- a contributor with only this repo
cloned still gets a green run. A run that means to provide the platform says so
with ``OPENAVC_REQUIRE_PLATFORM=1``, and then a missing platform fails instead
of quietly turning this file into nothing. That is the mirror of openavc's own
``tests/test_driver_round_trip_parity.py``, which sweeps this repo's drivers
under ``OPENAVC_REQUIRE_DRIVER_CORPUS`` -- the same carve-out, pointing the
other way.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import _platform_stubs as stubs
from _platform_probe import (
    PLATFORM_ROOT_ENV,
    REQUIRE_PLATFORM_ENV,
    platform_on_path,
    platform_required as _platform_required,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Locating the platform ───────────────────────────────────────────────────
#
# The search itself, the sys.path restore and the stub-leak cleanup live in
# ``_platform_probe`` -- ``test_doc_python_examples.py`` needs exactly the same
# resolution, and two copies of "where is the platform" is how the two would
# come to disagree about whether a run counts as pinned.


def _import_platform() -> tuple[dict, str]:
    """Import the real platform, or return the reason it could not be.

    The classes imported below stay live through this module's own references;
    conftest drops the ``openavc`` entries from ``sys.modules``
    once this module is collected, exactly as it does for every other module's
    stubs.
    """
    with platform_on_path():
        try:
            from openavc.core.connection_fault import (    # noqa: PLC0415
                _DRIVER_FAULT_CODES, ConnectionFaultError,
            )
            from openavc.core.event_bus import EventBus    # noqa: PLC0415
            from openavc.core.state_store import StateStore  # noqa: PLC0415
            from openavc.discovery.companion import ProbeContext  # noqa: PLC0415
            from openavc.drivers.base import (             # noqa: PLC0415
                BaseDriver, CommandParamError, DeviceSettingValueError,
                UndeclaredStateError,
            )
            from openavc.transport.frame_parsers import (  # noqa: PLC0415
                DEFAULT_MAX_BUFFER, CallableFrameParser, DelimiterFrameParser,
                FrameParser,
            )
            from openavc.simulator.base import BaseSimulator      # noqa: PLC0415
            from openavc.simulator.http_simulator import HTTPSimulator  # noqa: PLC0415
            from openavc.simulator.tcp_simulator import TCPSimulator  # noqa: PLC0415
            from openavc.simulator.udp_simulator import UDPSimulator  # noqa: PLC0415
        except Exception as exc:                          # noqa: BLE001
            return {}, f"the openavc platform is not importable ({exc})"

    return {
        "BaseDriver": BaseDriver,
        "CommandParamError": CommandParamError,
        "DeviceSettingValueError": DeviceSettingValueError,
        "UndeclaredStateError": UndeclaredStateError,
        "ConnectionFaultError": ConnectionFaultError,
        "DRIVER_FAULT_CODES": _DRIVER_FAULT_CODES,
        "StateStore": StateStore,
        "EventBus": EventBus,
        "ProbeContext": ProbeContext,
        "FrameParser": FrameParser,
        "CallableFrameParser": CallableFrameParser,
        "DelimiterFrameParser": DelimiterFrameParser,
        "DEFAULT_MAX_BUFFER": DEFAULT_MAX_BUFFER,
        "BaseSimulator": BaseSimulator,
        "TCPSimulator": TCPSimulator,
        "HTTPSimulator": HTTPSimulator,
        "UDPSimulator": UDPSimulator,
    }, ""


PLATFORM, _MISSING_REASON = _import_platform()

if _MISSING_REASON:
    if _platform_required():
        raise AssertionError(
            f"{REQUIRE_PLATFORM_ENV}=1 promised the openavc platform, but "
            f"{_MISSING_REASON}. Point {PLATFORM_ROOT_ENV} at the checkout, or "
            f"unset {REQUIRE_PLATFORM_ENV} to state that this run does not "
            f"pin the stubs."
        )
    pytest.skip(
        f"stub fidelity needs the openavc platform: {_MISSING_REASON}. Set "
        f"{PLATFORM_ROOT_ENV} to its checkout, or {REQUIRE_PLATFORM_ENV}=1 to "
        f"make its absence a failure.",
        allow_module_level=True,
    )


# ── What is pinned, and what is deliberately not ────────────────────────────

#: stub class -> the platform class it stands in for.
PAIRS: list[tuple[type, str]] = [
    (stubs.StubBaseDriver, "BaseDriver"),
    (stubs.StubState, "StateStore"),
    (stubs.StubEvents, "EventBus"),
    (stubs.FrameParser, "FrameParser"),
    (stubs.CallableFrameParser, "CallableFrameParser"),
    (stubs.DelimiterFrameParser, "DelimiterFrameParser"),
    (stubs.StubBaseSimulator, "BaseSimulator"),
    (stubs.StubTCPSimulator, "TCPSimulator"),
    (stubs.StubHTTPSimulator, "HTTPSimulator"),
    (stubs.StubUDPSimulator, "UDPSimulator"),
    (stubs.ConnectionFaultError, "ConnectionFaultError"),
    (stubs.CommandParamError, "CommandParamError"),
    (stubs.DeviceSettingValueError, "DeviceSettingValueError"),
    (stubs.UndeclaredStateError, "UndeclaredStateError"),
    (stubs.StubProbeContext, "ProbeContext"),
]

#: (stub class name, attribute) -> why it cannot be compared to the platform.
#: Every entry is checked for staleness: an attribute that no longer exists on
#: the stub fails, so this list cannot quietly outlive its reason.
#: Empty on purpose, and worth keeping empty: every class attribute the stubs
#: define currently resolves on the platform. The harness-only extras the
#: stubs carry (StubState.writes, StubEvents.payloads, StubTCPSimulator
#: .push_targets) are instance attributes set in __init__, so they are not
#: class surface and a driver cannot mistake them for platform API.
UNCOMPARABLE: dict[tuple[str, str], str] = {}

#: Attributes every class carries that say nothing about the platform.
_BORING = {
    "__module__", "__qualname__", "__doc__", "__dict__", "__weakref__",
    "__annotations__", "__firstlineno__", "__static_attributes__",
    "__abstractmethods__", "_abc_impl", "__parameters__", "__slots__",
    "__type_params__", "__orig_bases__",
}


def _stub_attrs(cls: type) -> list[str]:
    """Attributes the stub class itself defines, worth comparing."""
    return sorted(n for n in vars(cls) if n not in _BORING)


def _unwrap(obj):
    if isinstance(obj, (staticmethod, classmethod)):
        return obj.__func__
    if isinstance(obj, property):
        return obj.fget
    return obj


def _describe(sig: inspect.Signature) -> list[str]:
    """A signature as a comparable list: name, kind, and has-a-default."""
    return [
        f"{p.name}:{p.kind.name}"
        f"{'=' if p.default is not inspect.Parameter.empty else ''}"
        for p in sig.parameters.values()
    ]


def _compare_attr(stub_cls: type, real_cls: type, name: str) -> str | None:
    """Return a description of the divergence, or None when they agree."""
    stub_raw = vars(stub_cls)[name]

    real_raw = None
    for klass in real_cls.__mro__:
        if name in vars(klass):
            real_raw = vars(klass)[name]
            break
    if real_raw is None:
        return (
            f"{stub_cls.__name__}.{name} does not exist on "
            f"{real_cls.__module__}.{real_cls.__name__} — the stub invents "
            f"surface the platform does not have, so a driver can depend on "
            f"something that is not there in the field"
        )

    stub_obj, real_obj = _unwrap(stub_raw), _unwrap(real_raw)

    stub_is_prop = isinstance(stub_raw, property)
    real_is_prop = isinstance(real_raw, property)
    if stub_is_prop != real_is_prop:
        return (
            f"{stub_cls.__name__}.{name} is "
            f"{'a property' if stub_is_prop else 'not a property'} but the "
            f"platform's is {'a property' if real_is_prop else 'not'}"
        )

    if callable(stub_obj) and callable(real_obj):
        stub_async = inspect.iscoroutinefunction(stub_obj)
        real_async = inspect.iscoroutinefunction(real_obj)
        if stub_async != real_async:
            return (
                f"{stub_cls.__name__}.{name} is "
                f"{'async' if stub_async else 'sync'} but the platform's is "
                f"{'async' if real_async else 'sync'} — awaiting it in a test "
                f"proves nothing about the real call"
            )
        try:
            stub_sig = inspect.signature(stub_obj)
            real_sig = inspect.signature(real_obj)
        except (TypeError, ValueError):
            return None
        stub_params, real_params = _describe(stub_sig), _describe(real_sig)
        if stub_params != real_params:
            return (
                f"{stub_cls.__name__}.{name}{stub_sig} does not match "
                f"{real_cls.__name__}.{name}{real_sig}\n"
                f"        stub:     {stub_params}\n"
                f"        platform: {real_params}"
            )
        return None

    # Not callable: a class constant. Compare by value -- a drifted
    # _PLATFORM_STATE_PROPS or _CHILD_RESERVED_PROPS is a silent behaviour
    # change on both sides of the check.
    if stub_raw != real_raw:
        return (
            f"{stub_cls.__name__}.{name} = {stub_raw!r} but the platform's is "
            f"{real_raw!r}"
        )
    return None


# ── Half one: shape ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "stub_cls,real_name", PAIRS, ids=[p[0].__name__ for p in PAIRS]
)
def test_stub_matches_platform_signature(stub_cls, real_name):
    """Every attribute the stub defines resolves on the platform class and
    matches it exactly."""
    real_cls = PLATFORM[real_name]
    problems = []
    for attr in _stub_attrs(stub_cls):
        if (stub_cls.__name__, attr) in UNCOMPARABLE:
            continue
        divergence = _compare_attr(stub_cls, real_cls, attr)
        if divergence:
            problems.append(divergence)
    assert not problems, (
        f"{stub_cls.__name__} has drifted from "
        f"{real_cls.__module__}.{real_cls.__name__}:\n  - "
        + "\n  - ".join(problems)
        + "\n\nThe stub is what every driver test in this repo runs against. "
        "Fix tests/_platform_stubs.py to match the platform, not the other "
        "way round."
    )


def test_every_stub_attribute_is_accounted_for(capsys):
    """No attribute is skipped quietly.

    The failure this guards against is not a red run, it is a green one: a
    comparison that silently drops half its methods reports the same "passed"
    as a comparison that reached all of them. So each attribute has to land in
    compared or allowlisted, and the counts are printed.
    """
    compared = 0
    allowlisted = 0
    unreachable: list[str] = []
    report: list[str] = []

    for stub_cls, real_name in PAIRS:
        real_cls = PLATFORM[real_name]
        attrs = _stub_attrs(stub_cls)
        here_compared, here_allowed = [], []
        for attr in attrs:
            if (stub_cls.__name__, attr) in UNCOMPARABLE:
                here_allowed.append(attr)
                continue
            if not any(attr in vars(k) for k in real_cls.__mro__):
                unreachable.append(f"{stub_cls.__name__}.{attr}")
                continue
            here_compared.append(attr)
        compared += len(here_compared)
        allowlisted += len(here_allowed)
        real_public = {
            n for k in real_cls.__mro__ for n in vars(k)
            if not n.startswith("__") and n not in _BORING
        }
        not_stubbed = len(real_public - set(attrs))
        report.append(
            f"  {stub_cls.__name__:<22} -> {real_name:<22} "
            f"{len(here_compared):>3} compared, {len(here_allowed)} allowlisted"
            f"  (platform has {not_stubbed} more the stub does not model)"
        )

    with capsys.disabled():
        print("\nStub fidelity coverage:")
        print("\n".join(report))
        print(f"  TOTAL: {compared} attributes compared against the platform, "
              f"{allowlisted} allowlisted, {len(unreachable)} unreachable")

    assert not unreachable, (
        "these stub attributes could not be compared to the platform at all, "
        "so nothing pins them:\n  - " + "\n  - ".join(unreachable)
        + "\nEither the platform lost the attribute (fix the stub) or the "
          "stub invented it (delete it, or add it to UNCOMPARABLE with the "
          "reason)."
    )

    # A floor, so a refactor that guts the comparison fails instead of
    # reporting a clean run over nothing.
    assert compared >= 70, (
        f"only {compared} attributes were compared against the platform; this "
        f"has been reaching 76. A drop that large means the comparison "
        f"stopped finding things, not that the stubs got smaller."
    )


def test_uncomparable_allowlist_has_no_stale_entries():
    """An allowlist entry whose attribute is gone is a claim about nothing."""
    by_name = {cls.__name__: cls for cls, _ in PAIRS}
    stale = [
        f"{cls_name}.{attr}"
        for (cls_name, attr) in UNCOMPARABLE
        if cls_name not in by_name or attr not in vars(by_name[cls_name])
    ]
    assert not stale, (
        "UNCOMPARABLE names attributes the stubs no longer define: "
        + ", ".join(stale)
        + ". Delete the entries — an allowlist that outlives its reason hides "
          "the next real divergence."
    )


def test_fault_codes_match_the_platform():
    """The stub rejects exactly the codes the platform rejects.

    A stub that accepted any string would let a driver raise a typo'd code,
    pass its own test, and misclassify the device forever in the field.
    """
    assert stubs.DRIVER_FAULT_CODES == set(PLATFORM["DRIVER_FAULT_CODES"])


def test_shared_constants_match():
    assert stubs.DEFAULT_MAX_BUFFER == PLATFORM["DEFAULT_MAX_BUFFER"]
    assert stubs.STRICT_DRIVER_STATE_ENV == "OPENAVC_STRICT_DRIVER_STATE"


# ── Half two: behaviour ─────────────────────────────────────────────────────
#
# Signature parity cannot see the divergences that actually shipped:
# set_state(key, value) looks identical whether or not it namespaces, and a
# frame parser that throws away the returned buffer has the same feed(data).
# So these drive both sides and compare what came out.


#: Parse functions covering the shapes a real driver writes. Each is
#: (name, parse_fn, [byte chunks to feed]) and every one is fed to the stub
#: and the platform parser identically.
def _parse_stx_etx(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Framed \\x02...\\x03 with a trailing checksum byte; garbage before the
    STX is dropped, a bad checksum resyncs past the frame."""
    start = buffer.find(b"\x02")
    if start < 0:
        return None, b""            # nothing usable at all
    if start > 0:
        return None, buffer[start:]  # drop leading garbage
    end = buffer.find(b"\x03", 1)
    if end < 0 or len(buffer) < end + 2:
        return None, buffer          # wait for more
    body = buffer[1:end]
    checksum = buffer[end + 1]
    if checksum != (sum(body) & 0xFF):
        return None, buffer[end + 2:]  # corrupt frame: skip it
    return body, buffer[end + 2:]


def _parse_line(buffer: bytes) -> tuple[bytes | None, bytes]:
    """The commonest shape: a delimiter, buffer returned unchanged when short."""
    if b"\r" not in buffer:
        return None, buffer
    msg, _, rest = buffer.partition(b"\r")
    return msg, rest


def _parse_never_consumes(buffer: bytes) -> tuple[bytes | None, bytes]:
    """A buggy parser: returns a message without consuming. Both sides must
    stop rather than spin the event loop."""
    if not buffer:
        return None, buffer
    return b"X", buffer


def _frame(body: bytes) -> bytes:
    return b"\x02" + body + b"\x03" + bytes([sum(body) & 0xFF])


PARSER_CASES = [
    (
        "corrupt frame then good frames",
        _parse_stx_etx,
        [b"\x02BAD\x03\x00" + _frame(b"OK1"), _frame(b"OK2"), _frame(b"OK3")],
    ),
    (
        "leading garbage before the start marker",
        _parse_stx_etx,
        [b"\xff\xff\xff" + _frame(b"HELLO")],
    ),
    (
        "frame split across three feeds",
        _parse_stx_etx,
        [b"\x02PA", b"RT\x03", bytes([sum(b"PART") & 0xFF])],
    ),
    (
        "several frames in one chunk",
        _parse_stx_etx,
        [_frame(b"A") + _frame(b"B") + _frame(b"C")],
    ),
    ("no usable bytes at all", _parse_stx_etx, [b"\xde\xad\xbe\xef"]),
    ("delimiter framing", _parse_line, [b"ONE\rTWO\r", b"THR", b"EE\r"]),
    ("empty feed", _parse_line, [b"", b"A\r"]),
    ("non-consuming parse function", _parse_never_consumes, [b"junk"]),
]


@pytest.mark.parametrize(
    "label,parse_fn,chunks", PARSER_CASES, ids=[c[0] for c in PARSER_CASES]
)
def test_callable_frame_parser_behaves_identically(label, parse_fn, chunks):
    """Identical bytes through both parsers produce identical messages and
    leave identical buffers.

    Reverting the stub to the pre-2026-07-30 semantics (drop the returned
    buffer unless a message came back) fails the first two cases: the corrupt
    frame wedges the stream and the following good frames never arrive.
    """
    stub_parser = stubs.CallableFrameParser(parse_fn)
    real_parser = PLATFORM["CallableFrameParser"](parse_fn)

    for index, chunk in enumerate(chunks):
        stub_out = stub_parser.feed(chunk)
        real_out = real_parser.feed(chunk)
        assert stub_out == real_out, (
            f"{label}: feed #{index} ({chunk!r}) — the stub delivered "
            f"{stub_out!r}, the platform delivered {real_out!r}. Every driver "
            f"test in this repo runs against the stub."
        )
        assert stub_parser._buffer == real_parser._buffer, (
            f"{label}: feed #{index} ({chunk!r}) left the stub holding "
            f"{stub_parser._buffer!r} and the platform holding "
            f"{real_parser._buffer!r} — the next feed will diverge even "
            f"though this one matched."
        )

    stub_parser.reset()
    real_parser.reset()
    assert stub_parser._buffer == real_parser._buffer == b""


def test_callable_frame_parser_overflow_clears_and_names_the_device():
    """The 64 KB guard fires at the same point on both sides, and the warning
    can be attributed to a device."""
    def _hoard(buffer: bytes) -> tuple[bytes | None, bytes]:
        return None, buffer

    stub_parser = stubs.CallableFrameParser(_hoard)
    real_parser = PLATFORM["CallableFrameParser"](_hoard)
    stub_parser.device_label = real_parser.device_label = "projector_1"

    payload = b"x" * (stubs.DEFAULT_MAX_BUFFER + 2)
    assert stub_parser.feed(payload) == real_parser.feed(payload) == []
    assert stub_parser._buffer == real_parser._buffer == b""
    assert stub_parser._where() == real_parser._where() == "[projector_1] "


# -- BaseDriver behaviour --

_DRIVER_INFO = {
    "id": "acme_widget",
    "name": "Acme Widget",
    "transport": "tcp",
    "state_variables": {
        "power": {"type": "boolean"},
        "volume": {"type": "integer", "min": 0, "max": 100},
    },
    "child_entity_types": {
        "zone": {
            "label": "Zone",
            "id_format": {"type": "integer", "min": 1, "max": 8, "pad_width": 2},
            "state_variables": {
                "level": {"type": "integer", "min": -80, "max": 12},
                "mute": {"type": "boolean"},
            },
        },
        "block": {
            "label": "Block",
            "id_format": {"type": "string"},
            "dynamic": True,
            "state_variables": {},
        },
    },
}


def _make_pair(monkeypatch, strict: bool = False):
    """One stub driver and one real driver, same DRIVER_INFO, fresh stores."""
    monkeypatch.setenv(stubs.STRICT_DRIVER_STATE_ENV, "1" if strict else "")

    stub_state = stubs.StubState()
    stub_driver = type("StubAcme", (stubs.StubBaseDriver,),
                       {"DRIVER_INFO": _DRIVER_INFO})(
        "widget_1", {}, stub_state, stubs.StubEvents())

    real_store = PLATFORM["StateStore"]()
    real_cls = type("RealAcme", (PLATFORM["BaseDriver"],), {
        "DRIVER_INFO": _DRIVER_INFO,
        "send_command": lambda self, command, params=None: None,
    })
    real_driver = real_cls("widget_1", {}, real_store, PLATFORM["EventBus"]())
    return stub_driver, stub_state, real_driver, real_store


def test_construction_creates_the_same_keys(monkeypatch):
    """Declared variables get a key at construction and no value: a driver
    reading one before its first poll gets None, because nothing has reported.

    Both halves have to match. A stub that skipped the keys entirely would let
    a driver depend on the key being absent, which on a real system it never
    is; a stub that invented typed defaults -- which this one did, mirroring
    the platform before it stopped -- would hide a driver reading a number the
    hardware never sent."""
    _, stub_state, _, real_store = _make_pair(monkeypatch)
    assert stub_state.data == real_store.snapshot() == {
        "device.widget_1.power": None,
        "device.widget_1.volume": None,
        "device.widget_1.connected": False,
    }


def test_set_state_lands_on_the_same_key(monkeypatch):
    """The divergence that shipped: a stub whose set_state wrote the bare
    property name, with no ``device.<id>.`` prefix. Every read went back
    through the same stub, so the whole file agreed with itself."""
    stub_driver, stub_state, real_driver, real_store = _make_pair(monkeypatch)

    stub_driver.set_state("power", True)
    real_driver.set_state("power", True)

    assert "device.widget_1.power" in stub_state.data
    assert real_store.get("device.widget_1.power") is True
    assert stub_state.data["device.widget_1.power"] is True
    assert stub_driver.get_state("power") == real_driver.get_state("power")


def test_set_states_batch_lands_on_the_same_keys(monkeypatch):
    stub_driver, stub_state, real_driver, real_store = _make_pair(monkeypatch)
    updates = {"power": False, "volume": 42}

    stub_driver.set_states(updates)
    real_driver.set_states(updates)

    for key, value in {"device.widget_1.power": False,
                       "device.widget_1.volume": 42}.items():
        assert stub_state.data[key] == value
        assert real_store.get(key) == value


def test_delete_state_removes_the_same_key(monkeypatch):
    stub_driver, stub_state, real_driver, real_store = _make_pair(monkeypatch)
    stub_driver.set_state("power", True)
    real_driver.set_state("power", True)

    stub_driver.delete_state("power")
    real_driver.delete_state("power")

    assert "device.widget_1.power" not in stub_state.data
    assert real_store.get("device.widget_1.power") is None


def test_redact_in_log_accepts_the_same_values(monkeypatch):
    """A driver that registers a session token must run under both.

    The platform routes the value into a process-wide registry the transport
    formatter reads; the stub only records it. What has to agree is the
    contract a driver sees: the call exists, takes one string, returns None,
    and ignores a value too short to be a credential.
    """
    stub_driver, _, real_driver, _ = _make_pair(monkeypatch)

    assert stub_driver.redact_in_log("sess-4d91c07e") is None
    assert real_driver.redact_in_log("sess-4d91c07e") is None
    assert stub_driver.redact_in_log("ok") is None
    assert real_driver.redact_in_log("ok") is None

    assert stub_driver.redacted_secrets == {"sess-4d91c07e"}

    # Reach the registry through the very function the real driver just called.
    # Neither a plain `import openavc.utils.log_redaction` nor a sys.modules
    # lookup works here: conftest installs and rolls back stub `server.*`
    # entries around every test, so `openavc.drivers.base` is no longer in
    # sys.modules and a fresh import would re-execute the module and hand back
    # a second, empty singleton. The class object still holds its own globals.
    registry = PLATFORM["BaseDriver"].redact_in_log.__globals__[
        "get_secret_registry"
    ]()
    try:
        assert registry.secrets_for("widget_1") == {"sess-4d91c07e"}
    finally:
        registry.forget("widget_1")


def test_undeclared_state_raises_in_strict_mode_on_both(monkeypatch):
    """Strict mode is the external author's gate. If the stub did not carry it,
    turning it on in this repo -- where driver authors actually run tests --
    would do nothing."""
    stub_driver, _, real_driver, _ = _make_pair(monkeypatch, strict=True)

    with pytest.raises(ValueError) as stub_exc:
        stub_driver.set_state("undeclared_thing", 1)
    with pytest.raises(ValueError) as real_exc:
        real_driver.set_state("undeclared_thing", 1)

    assert type(stub_exc.value).__name__ == type(real_exc.value).__name__
    for exc in (stub_exc, real_exc):
        assert "undeclared_thing" in str(exc.value)
        assert "state_variables" in str(exc.value)

    # And the batch form fails whole, not half-applied, on both.
    with pytest.raises(ValueError):
        stub_driver.set_states({"power": True, "nope": 1})
    with pytest.raises(ValueError):
        real_driver.set_states({"power": True, "nope": 1})


def test_undeclared_state_warns_but_writes_when_not_strict(monkeypatch, caplog):
    """Not strict: the value is live on both sides, and both say so once."""
    stub_driver, stub_state, real_driver, real_store = _make_pair(monkeypatch)

    with caplog.at_level("WARNING"):
        stub_driver.set_state("undeclared_thing", 7)
        stub_driver.set_state("undeclared_thing", 8)
        real_driver.set_state("undeclared_thing", 7)
        real_driver.set_state("undeclared_thing", 8)

    assert stub_state.data["device.widget_1.undeclared_thing"] == 8
    assert real_store.get("device.widget_1.undeclared_thing") == 8
    warnings = [r for r in caplog.records if "undeclared_thing" in r.getMessage()]
    # Once per key per driver, not once per write.
    assert len(warnings) == 2, [r.getMessage() for r in warnings]


def test_connected_is_exempt_from_the_undeclared_check(monkeypatch):
    """The platform writes it on the driver's behalf, so no driver declares
    it. A stub that raised here would fail every connect test."""
    stub_driver, _, real_driver, _ = _make_pair(monkeypatch, strict=True)
    stub_driver.set_state("connected", True)
    real_driver.set_state("connected", True)


def test_child_registration_writes_the_same_keys(monkeypatch):
    """Padded ids, the four injected platform props, and the rest unreported.

    ``mute`` is the one that matters here: it was declared, it was not supplied
    and nobody has read it, so it holds nothing on both sides. The four
    platform props DO say something -- they describe the child rather than
    report from it -- and a stub that let them fall through to "unreported"
    would diverge on all four."""
    stub_driver, stub_state, real_driver, real_store = _make_pair(monkeypatch)

    stub_driver.register_child("zone", 3, {"level": -6})
    real_driver.register_child("zone", 3, {"level": -6})

    expected = {
        "device.widget_1.zone.03.level": -6,
        "device.widget_1.zone.03.mute": None,
        "device.widget_1.zone.03.online": True,
        "device.widget_1.zone.03.label": "",
        "device.widget_1.zone.03.offline_reason": "",
        "device.widget_1.zone.03.offline_detail": "",
    }
    for key, value in expected.items():
        assert stub_state.data[key] == value, key
        assert real_store.get(key) == value, key
    assert stub_driver.format_child_id("zone", 3) == \
        real_driver.format_child_id("zone", 3) == "03"


def test_child_write_to_an_unregistered_child_is_skipped_on_both(monkeypatch):
    stub_driver, stub_state, real_driver, real_store = _make_pair(monkeypatch)

    stub_driver.set_child_state("zone", 5, "level", -3)
    real_driver.set_child_state("zone", 5, "level", -3)

    assert "device.widget_1.zone.05.level" not in stub_state.data
    assert real_store.get("device.widget_1.zone.05.level") is None


def test_undeclared_child_property_raises_on_both(monkeypatch):
    stub_driver, _, real_driver, _ = _make_pair(monkeypatch)
    stub_driver.register_child("zone", 1)
    real_driver.register_child("zone", 1)

    with pytest.raises(ValueError, match="not declared"):
        stub_driver.set_child_state("zone", 1, "not_a_prop", 1)
    with pytest.raises(ValueError, match="not declared"):
        real_driver.set_child_state("zone", 1, "not_a_prop", 1)


def test_child_id_range_is_enforced_on_both(monkeypatch):
    stub_driver, _, real_driver, _ = _make_pair(monkeypatch)
    for driver in (stub_driver, real_driver):
        with pytest.raises(ValueError):
            driver.register_child("zone", 99)      # > id_format.max
        with pytest.raises(ValueError):
            driver.register_child("zone", 0)       # < id_format.min
        with pytest.raises(TypeError):
            driver.register_child("zone", "three")  # wrong id kind
        with pytest.raises(ValueError):
            driver.register_child("nope", 1)       # undeclared child type


def test_dynamic_child_schema_behaves_the_same(monkeypatch):
    stub_driver, stub_state, real_driver, real_store = _make_pair(monkeypatch)
    schema = {"gain": {"type": "number", "min": -100, "max": 20}}

    stub_driver.register_child("block", "Pgm_Gain", schema=schema)
    real_driver.register_child("block", "Pgm_Gain", schema=schema)

    # A dynamic child's declared prop is no different: registered is not read,
    # so it holds nothing on both sides. This used to be -100.0 -- the declared
    # min, drawn on a fader as fully attenuated -- and before that a stub-only 0.
    assert stub_state.data["device.widget_1.block.Pgm_Gain.gain"] is None
    assert real_store.get("device.widget_1.block.Pgm_Gain.gain") is None
    assert stub_state.has("device.widget_1.block.Pgm_Gain.gain")
    assert real_store.has("device.widget_1.block.Pgm_Gain.gain")
    assert stub_driver.get_child_schema("block", "Pgm_Gain") == \
        real_driver.get_child_schema("block", "Pgm_Gain")
    assert stub_driver.is_child_type_dynamic("block") is \
        real_driver.is_child_type_dynamic("block") is True

    # A per-child schema on a static type is rejected by both.
    for driver in (stub_driver, real_driver):
        with pytest.raises(ValueError, match="dynamic"):
            driver.register_child("zone", 2, schema=schema)


def test_deregister_removes_the_same_keys(monkeypatch):
    stub_driver, stub_state, real_driver, real_store = _make_pair(monkeypatch)
    stub_driver.register_child("zone", 4)
    real_driver.register_child("zone", 4)

    stub_driver.deregister_child("zone", 4)
    real_driver.deregister_child("zone", 4)

    assert not [k for k in stub_state.data if ".zone.04." in k]
    assert not [k for k in real_store.snapshot() if ".zone.04." in k]
    assert stub_driver.list_children("zone") == real_driver.list_children("zone")


def test_children_batch_skips_unregistered_and_applies_the_rest(monkeypatch):
    stub_driver, stub_state, real_driver, real_store = _make_pair(monkeypatch)
    for driver in (stub_driver, real_driver):
        driver.register_child("zone", 1)
        driver.register_child("zone", 2)
        driver.set_children_state_batch([
            ("zone", 1, {"level": -10}),
            ("zone", 7, {"level": -20}),   # never registered
            ("zone", 2, {"level": -30}),
        ])

    assert stub_state.data["device.widget_1.zone.01.level"] == -10
    assert stub_state.data["device.widget_1.zone.02.level"] == -30
    assert "device.widget_1.zone.07.level" not in stub_state.data
    assert real_store.get("device.widget_1.zone.01.level") == -10
    assert real_store.get("device.widget_1.zone.02.level") == -30
    assert real_store.get("device.widget_1.zone.07.level") is None


def test_get_child_entity_types_agrees(monkeypatch):
    stub_driver, _, real_driver, _ = _make_pair(monkeypatch)
    assert stub_driver.get_child_entity_types() == \
        real_driver.get_child_entity_types()


# -- ConnectionFaultError behaviour --

def test_connection_fault_error_matches_the_platform():
    """The divergence that shipped: three stubs put the code on ``.code``, the
    attribute the platform's classifier does not read, and one accepted it
    positionally with a default. Nine assertions were checking the stub."""
    real_cls = PLATFORM["ConnectionFaultError"]

    stub_exc = stubs.ConnectionFaultError("nope", code="auth_failed")
    real_exc = real_cls("nope", code="auth_failed")

    assert stub_exc.fault_code == real_exc.fault_code == "auth_failed"
    assert not hasattr(real_exc, "code"), (
        "the platform's typed fault has no .code attribute; a stub that "
        "provided one would let a test assert on something the classifier "
        "never reads"
    )
    assert not hasattr(stub_exc, "code")
    assert isinstance(stub_exc, ConnectionError)
    assert isinstance(real_exc, ConnectionError)
    assert str(stub_exc) == str(real_exc) == "nope"

    # An unknown code fails at construction on both, so a typo cannot survive
    # a driver's own test suite.
    for cls in (stubs.ConnectionFaultError, real_cls):
        with pytest.raises(ValueError, match="Unknown connection-fault code"):
            cls("x", code="totally_bogus")
        with pytest.raises(TypeError):
            cls("x", "auth_failed")     # code is keyword-only
        with pytest.raises(TypeError):
            cls("x")                    # code is required


# -- Simulator behaviour --

def test_simulator_state_is_a_read_only_copy():
    """Mutating ``self.state[...]`` in simulator code is a bug the platform
    swallows; a stub that returned the live dict would hide it."""
    info = {"driver_id": "acme_widget", "initial_state": {"power": "off"}}
    stub_sim = type("StubSim", (stubs.StubBaseSimulator,),
                    {"SIMULATOR_INFO": info})("sim1")
    real_sim = type("RealSim", (PLATFORM["TCPSimulator"],), {
        "SIMULATOR_INFO": info,
        "handle_command": lambda self, data: None,
    })("sim1")

    for sim in (stub_sim, real_sim):
        sim.state["power"] = "on"          # writing the copy changes nothing
        assert sim.get_state("power") == "off"
        sim.set_state("power", "on")
        assert sim.state["power"] == "on"
    assert stub_sim.driver_id == real_sim.driver_id == "acme_widget"


def test_simulator_unknown_error_mode_is_ignored_not_raised():
    info = {"driver_id": "acme_widget", "initial_state": {},
            "error_modes": {"no_reply": {"behavior": "no_response"}}}
    stub_sim = type("StubSim", (stubs.StubBaseSimulator,),
                    {"SIMULATOR_INFO": info})("sim1")
    real_sim = type("RealSim", (PLATFORM["TCPSimulator"],), {
        "SIMULATOR_INFO": info,
        "handle_command": lambda self, data: None,
    })("sim1")

    for sim in (stub_sim, real_sim):
        sim.inject_error("not_a_mode")          # warns, does not raise
        assert sim.active_errors == set()
        sim.inject_error("no_reply")
        assert sim.active_errors == {"no_reply"}
        assert sim.has_error_behavior("no_response") is True
        sim.clear_error("no_reply")
        assert sim.has_error_behavior("no_response") is False
