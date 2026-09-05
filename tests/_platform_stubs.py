"""Shared stand-ins for the OpenAVC platform, for driver and simulator tests.

A driver imports ``openavc.*`` (and its simulator imports ``openavc.simulator.*``) at
module load. This repo's CI installs ``requirements-dev.txt`` and nothing else,
so there is no ``openavc`` package for those imports to resolve against. Every
test here therefore puts lightweight stand-ins into ``sys.modules`` before
loading the driver, and ``conftest.py`` rolls them back once the module is
collected.

This module is where those stand-ins live, so a test writes::

    from _platform_stubs import StubBaseDriver, StubState, install_stubs

    install_stubs()
    DRV = load_driver("acme_widget", REPO_ROOT / "devices" / "acme_widget.py")

instead of ninety lines of hand-written platform.

**Why this is shared rather than copied per test, and why it is pinned.**
A hand-written stub cannot disagree with the person who wrote it: it *is* the
author's belief about the platform, so a test that passes against it has
confirmed the belief, not the platform. That is not hypothetical here. Before
this module existed, five separate tests replicated a frame parser that
discarded the buffer a parse function returned -- a behaviour the platform had
already stopped having, and two of those stubs documented it as a contract to
work around. A sixth wrote device state without the ``device.<id>.`` prefix the
real ``BaseDriver`` applies, and nothing noticed because every read went back
through the same stub. Four more stood in the typed connection fault with the
wrong attribute name, and their assertions checked an attribute the platform
does not have.

So ``tests/test_platform_stub_fidelity.py`` pins this module against the real
platform whenever the ``openavc`` checkout is present: it signature-compares
every stubbed method against the real class, and replays behaviour where the
shape alone would not catch a divergence. It skips when the platform is absent
-- a contributor with only this repo cloned still gets a green run -- and fails
loudly when a run promised the platform and did not provide it. Change anything
here and run that test with the platform present.

**Never import ``openavc`` from this module, and never install
stubs at import time.** ``conftest.py`` brackets ``sys.modules`` per test
module, but this helper is cached like any other module: a top-level install
here would fire once, be rolled back after the first importer, and starve every
test module after it. Stubs are installed by ``install_stubs()``, which each
test module calls at its own load. (Same rule as ``_lifecycle_fake.py``, which
holds the lifecycle half of the same job.)
"""

from __future__ import annotations

import fnmatch
import importlib.util
import logging
import os
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable

__all__ = [
    "STRICT_DRIVER_STATE_ENV",
    "CommandParamError",
    "CHILD_FAULT_CODES",
    "CHILD_RESERVED_PROPS",
    "CHILD_RESERVED_PROP_SCHEMA",
    "CHILD_NOT_RESPONDING",
    "CHILD_SERVICE_FAULT",
    "ConnectionFaultError",
    "DeviceSettingValueError",
    "UndeclaredStateError",
    "StubState",
    "StubEvents",
    "StubProbeContext",
    "FrameParser",
    "CallableFrameParser",
    "DelimiterFrameParser",
    "StubBaseDriver",
    "StubBaseSimulator",
    "StubTCPSimulator",
    "StubHTTPSimulator",
    "StubUDPSimulator",
    "strict_driver_state",
    "install_stubs",
    "stub_modules",
    "load_module",
]

log = logging.getLogger("openavc_drivers.stubs")


# ── Strict driver state ─────────────────────────────────────────────────────
#
# The platform reports a write to a state variable the driver never declared in
# DRIVER_INFO["state_variables"]: a warning at runtime, a raise under this
# environment variable. openavc's own conftest sets it so the platform suite
# runs strict; this repo's conftest sets it too, because this is where driver
# authors actually run tests -- without it, strict mode is a no-op in the one
# place the author would see it. Read per call, never cached at import: the
# flag exists to be switched around a single driver.

STRICT_DRIVER_STATE_ENV = "OPENAVC_STRICT_DRIVER_STATE"

_TRUE = {"1", "true", "yes", "on"}


def strict_driver_state() -> bool:
    """True when undeclared state writes should raise instead of warn."""
    return os.environ.get(STRICT_DRIVER_STATE_ENV, "").strip().lower() in _TRUE


# ── Exceptions ──────────────────────────────────────────────────────────────

class CommandParamError(ValueError):
    """A command parameter failed the platform's dispatch-gate validation."""


class DeviceSettingValueError(ValueError):
    """A device-setting write failed validation."""


class UndeclaredStateError(ValueError):
    """A driver wrote state it does not declare, under strict mode."""


# The codes a driver is allowed to raise. Kept in step with the platform's
# _DRIVER_FAULT_CODES -- the real ConnectionFaultError rejects anything else at
# construction, so a stub that accepted every string would let a typo'd code
# pass its own test and misclassify forever in the field.
DRIVER_FAULT_CODES = frozenset({
    "auth_failed",
    "connection_refused",
    "unreachable",
    "host_key_rejected",
    "no_response",
    "client_missing",
    "invalid_config",
    "transport_disconnected",
})


# The child-entity fault vocabulary, kept in step with the platform's
# CHILD_FAULT_CODES. A sub-unit gets more than a boolean: `not_responding` is
# absence (go find it), `service_fault` is present-but-wedged (power-cycle it).
# What the platform injects on every child, on top of whatever the type
# declares. Mirrors openavc/drivers/spec.py CHILD_RESERVED_PROP_SCHEMA; the
# fidelity job compares both the names and the resulting effective schema.
CHILD_RESERVED_PROP_SCHEMA: dict[str, dict[str, str]] = {
    "online": {"type": "boolean", "label": "Online"},
    "label": {"type": "string", "label": "Label"},
    "offline_reason": {"type": "string", "label": "Fault"},
    "offline_detail": {"type": "string", "label": "Fault Detail"},
}

CHILD_RESERVED_PROPS: tuple[str, ...] = tuple(CHILD_RESERVED_PROP_SCHEMA)

CHILD_NOT_RESPONDING = "not_responding"
CHILD_SERVICE_FAULT = "service_fault"

CHILD_FAULT_CODES = frozenset({
    CHILD_NOT_RESPONDING,
    CHILD_SERVICE_FAULT,
})

CHILD_FAULT_MESSAGES = {
    CHILD_NOT_RESPONDING: (
        "Not answering. Check that it has power and a network connection."
    ),
    CHILD_SERVICE_FAULT: (
        "Reachable, but not running. Power-cycle it, or restart it from the "
        "controller."
    ),
}


def default_child_fault_message(code: str) -> str:
    """Stand-in for the platform's child-fault sentence lookup."""
    return CHILD_FAULT_MESSAGES.get(code, "")


def is_child_fault_code(code: str) -> bool:
    """Stand-in for the platform's child-fault code check."""
    return code in CHILD_FAULT_CODES


class ConnectionFaultError(ConnectionError):
    """Stand-in for the platform's typed connection fault.

    Three things here are load-bearing and were each got wrong by a
    hand-written copy: it derives from ``ConnectionError`` (not ``Exception``),
    ``code`` is keyword-only and required (not a positional with a default),
    and the code lands on ``.fault_code`` -- the attribute the platform's
    classifier reads to produce ``offline_reason``. An unknown code raises at
    construction, exactly as the platform does.
    """

    def __init__(self, message: str = "", *, code: str):
        if code not in DRIVER_FAULT_CODES:
            raise ValueError(
                f"Unknown connection-fault code {code!r}. Valid codes: "
                f"{', '.join(sorted(DRIVER_FAULT_CODES))}"
            )
        super().__init__(message)
        self.fault_code = code


# ── State store and event bus ───────────────────────────────────────────────

class StubState:
    """Stand-in for ``openavc.core.state_store.StateStore``.

    Values live in ``.data`` so a test can assert on the flat key space the
    platform really writes -- ``device.<id>.<property>`` for a device variable,
    ``device.<id>.<child_type>.<padded_id>.<property>`` for a child's.
    """

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        #: Every (key, value, source) write in order, for tests that care about
        #: write ordering or the source attribution the platform stamps.
        self.writes: list[tuple[str, Any, str]] = []

    def set(self, key: str, value: Any, source: str = "system") -> None:
        self.data[key] = value
        self.writes.append((key, value, source))

    def set_batch(self, updates: dict[str, Any], source: str = "system") -> None:
        self.data.update(updates)
        for key, value in updates.items():
            self.writes.append((key, value, source))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.data

    def delete(self, key: str, source: str = "system") -> None:
        self.data.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        return dict(self.data)

    def get_namespace(self, prefix: str) -> dict[str, Any]:
        dotted = prefix + "."
        return {
            k[len(dotted):]: v
            for k, v in self.data.items()
            if k.startswith(dotted)
        }

    def get_matching(self, pattern: str) -> dict[str, Any]:
        return {
            k: v for k, v in self.data.items() if fnmatch.fnmatchcase(k, pattern)
        }


class StubEvents:
    """Stand-in for ``openavc.core.event_bus.EventBus``.

    ``emit`` records the event name in ``.emitted`` so a test can assert the
    canonical ``device.connected.<id>`` / ``device.disconnected.<id>``
    lifecycle without wiring a subscriber.
    """

    def __init__(self) -> None:
        self.emitted: list[str] = []
        self.payloads: list[tuple[str, dict[str, Any] | None]] = []

    async def emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        self.emitted.append(event)
        self.payloads.append((event, payload))


class StubProbeContext:
    """Stand-in for ``openavc.discovery.companion.ProbeContext``.

    A discovery companion annotates its probe function with this type, so the
    module needs *something* importable under that name; companions that use
    the context's methods stand in their own recording version instead.
    """


# ── Frame parsers ───────────────────────────────────────────────────────────

DEFAULT_MAX_BUFFER = 65536


class FrameParser:
    """Stand-in for the ``openavc.transport.frame_parsers.FrameParser`` base."""

    #: Stamped by whichever transport owns the parser, so an overflow warning
    #: can name the device whose traffic was dropped.
    device_label: str = ""

    def _where(self) -> str:
        return f"[{self.device_label}] " if self.device_label else ""

    def feed(self, data: bytes) -> list[bytes]:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


class DelimiterFrameParser(FrameParser):
    """Splits on a delimiter; messages come back with the delimiter stripped
    and empty messages (consecutive delimiters) skipped."""

    def __init__(self, delimiter: bytes = b"\r",
                 max_buffer: int = DEFAULT_MAX_BUFFER) -> None:
        if not delimiter:
            raise ValueError("Delimiter must not be empty")
        self._delimiter = delimiter
        self._buffer = b""
        self._max_buffer = max_buffer

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        messages: list[bytes] = []
        while self._delimiter in self._buffer:
            msg, self._buffer = self._buffer.split(self._delimiter, 1)
            if msg:
                messages.append(msg)
        if len(self._buffer) > self._max_buffer:
            log.warning(
                "%sDelimiter parser buffer overflow (%d bytes), clearing",
                self._where(), len(self._buffer),
            )
            self._buffer = b""
        return messages

    def reset(self) -> None:
        self._buffer = b""


class CallableFrameParser(FrameParser):
    """Wraps a driver's ``parse(buffer) -> (message | None, remaining)``.

    **The returned buffer is authoritative on BOTH branches.** Returning a
    shorter buffer with no message is how a binary protocol drops leading
    garbage or resyncs past a frame whose checksum failed; the parser keeps
    that shorter buffer and tries again. A parser that discarded it would wedge
    the stream after one corrupt frame until the ``max_buffer`` guard fired --
    which is what five hand-written copies of this class modelled, and why the
    fidelity test replays byte sequences through this class and the platform's
    side by side rather than only comparing their signatures.

    The loop stops when the parse function makes no progress: no message
    produced and no bytes consumed.
    """

    def __init__(
        self,
        parse_fn: Callable[[bytes], tuple[bytes | None, bytes]],
        max_buffer: int = DEFAULT_MAX_BUFFER,
    ) -> None:
        self._parse_fn = parse_fn
        self._buffer = b""
        self._max_buffer = max_buffer

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        messages: list[bytes] = []
        try:
            while True:
                before = len(self._buffer)
                msg, remaining = self._parse_fn(self._buffer)
                self._buffer = remaining
                if msg is None:
                    if len(remaining) >= before:
                        # Waiting for more data, or unable to progress at all.
                        # Also the hang guard for this branch.
                        break
                    # Bytes dropped without a message: garbage discarded or a
                    # corrupt frame skipped. The buffer strictly shrank, so
                    # retrying terminates.
                    continue
                messages.append(msg)
                if len(remaining) >= before:
                    log.warning(
                        "%sCustom frame parser made no forward progress "
                        "(buffer not consumed); stopping to avoid a hang",
                        self._where(),
                    )
                    break
        except Exception:
            log.exception(
                "%sError in custom frame parser function, clearing buffer",
                self._where(),
            )
            self._buffer = b""
        if len(self._buffer) > self._max_buffer:
            log.warning(
                "%sCallable parser buffer overflow (%d bytes), clearing",
                self._where(), len(self._buffer),
            )
            self._buffer = b""
        return messages

    def reset(self) -> None:
        self._buffer = b""


# ── BaseDriver ──────────────────────────────────────────────────────────────

_CHILD_STRING_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CHILD_STRING_ID_MAX_LEN = 128


class StubBaseDriver:
    """Stand-in for the state-owning half of ``openavc.drivers.base.BaseDriver``.

    What it models, faithfully, is everything a driver's *code* does to state:
    the ``device.<id>.`` namespace, the undeclared-state posture, and the whole
    child-entity registry (id validation and padding, the skip-with-a-warning
    on an unregistered child, the raise on a property the child type never
    declared). Those are the semantics a driver test is really asserting, and
    each of them has been got wrong by a hand-written copy.

    What it does not model is the connection lifecycle -- ``connect()``, the
    transport ladder, polling. Those are per-driver by design and belong in the
    test's own ``_FakeBaseDriver``, whose shared core is
    ``_lifecycle_fake.LifecycleFake``. Inherit from both when a driver needs
    both::

        class _FakeBaseDriver(StubBaseDriver, LifecycleFake):
            ...
    """

    DRIVER_INFO: dict[str, Any] = {}

    #: State the platform writes on a driver's behalf, exempt from the
    #: undeclared-state check because a driver never declares it.
    _PLATFORM_STATE_PROPS: frozenset[str] = frozenset({"connected"})

    _CHILD_RESERVED_PROPS: tuple[str, ...] = CHILD_RESERVED_PROPS
    _CHILD_RESERVED_PROP_SCHEMA: dict[str, dict[str, Any]] = (
        CHILD_RESERVED_PROP_SCHEMA
    )

    def __init__(self, device_id: str, config: dict[str, Any],
                 state: Any, events: Any) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._children: dict[str, dict[Any, int]] = {}
        self._child_schemas: dict[tuple[str, Any], dict[str, Any]] = {}
        self._child_register_seq = 0
        self._project_child_entities: dict[str, dict[str, dict[str, Any]]] = {}
        self._undeclared_state_seen: set[str] = set()
        # Runtime secrets the driver handed to redact_in_log(). The platform
        # keeps these in a process-wide registry the transport formatter reads;
        # here they are just recorded so a test can assert on them.
        self.redacted_secrets: set[str] = set()
        self._init_state_variables()

    def _init_state_variables(self) -> None:
        """Create a key for every declared state variable, plus ``connected``.

        The platform does this in ``__init__``, so the keys exist from
        construction and each holds ``None`` until the device reports. A stub
        that skipped it would let a driver depend on the key being absent,
        which on a real system it never is.

        ``connected`` is the exception and is not a reading: a driver being
        constructed is definitely not connected yet.
        """
        for prop_name, prop_info in self.DRIVER_INFO.get(
                "state_variables", {}).items():
            self.set_state(prop_name, self._default_for_var_def(prop_info))
        self.set_state("connected", False)

    # -- device state --

    def set_state(self, property_name: str, value: Any) -> None:
        var_def = self.DRIVER_INFO.get("state_variables", {}).get(property_name)
        if var_def is None:
            self._check_undeclared_state(property_name)
        self.state.set(
            f"device.{self.device_id}.{property_name}",
            value,
            source=f"device.{self.device_id}",
        )

    def set_states(self, updates: dict[str, Any]) -> None:
        declared = self.DRIVER_INFO.get("state_variables", {})
        # Check every key before writing any, so strict mode fails the batch
        # whole rather than half-applied.
        for key in updates:
            if key not in declared:
                self._check_undeclared_state(key)
        self.state.set_batch(
            {f"device.{self.device_id}.{k}": v for k, v in updates.items()},
            source=f"device.{self.device_id}",
        )

    def get_state(self, property_name: str) -> Any:
        return self.state.get(f"device.{self.device_id}.{property_name}")

    def delete_state(self, property_name: str) -> None:
        self.state.delete(
            f"device.{self.device_id}.{property_name}",
            source=f"device.{self.device_id}",
        )

    def redact_in_log(self, value: str) -> None:
        """Mask a runtime secret (a session token) in this device's log.

        The platform routes this into a process-wide redaction registry that the
        transport's TX/RX formatter reads. There is no log here, so the stub
        just records the value -- enough that a driver calling it under test
        does not blow up, and that a test can assert it was called.
        """
        if isinstance(value, str) and len(value) >= 4:
            self.redacted_secrets.add(value)

    def _check_undeclared_state(self, property_name: str) -> None:
        """Report a write to a state variable the driver never declared.

        Raises under strict mode -- which this repo's conftest turns on, so a
        driver's own test suite fails on it. Warns once per key otherwise,
        matching what someone tailing the server log sees during bring-up.
        """
        if property_name in self._PLATFORM_STATE_PROPS:
            return
        driver_id = self.DRIVER_INFO.get("id", "?")
        summary = (
            f"[{self.device_id}] driver '{driver_id}' wrote state "
            f"'{property_name}', which it does not declare in "
            f'DRIVER_INFO["state_variables"]'
        )
        if strict_driver_state():
            raise UndeclaredStateError(
                f"{summary}. Declare it, or stop writing it. (Reported as a "
                f"warning when {STRICT_DRIVER_STATE_ENV} is not set.)"
            )
        if property_name in self._undeclared_state_seen:
            return
        self._undeclared_state_seen.add(property_name)
        log.warning(
            f"{summary} — the value is live, but nothing knows its type and "
            f"no binding picker will offer it"
        )

    # -- child entities --

    def set_project_child_entities(
        self, child_entities: dict[str, dict[str, dict[str, Any]]] | None
    ) -> None:
        self._project_child_entities = dict(child_entities or {})

    def _child_type_def(self, child_type: str) -> dict[str, Any]:
        types = self.DRIVER_INFO.get("child_entity_types", {})
        if child_type not in types:
            raise ValueError(
                f"Driver {self.DRIVER_INFO.get('id', '?')} did not declare "
                f"child_entity_types[{child_type!r}]"
            )
        return types[child_type]

    def _effective_child_schema(
        self, child_type: str, local_id: int | str | None = None,
    ) -> dict[str, dict[str, Any]]:
        type_def = self._child_type_def(child_type)
        declared: dict[str, dict[str, Any]] | None = None
        if type_def.get("dynamic") and local_id is not None:
            declared = self._child_schemas.get((child_type, local_id))
        declared = dict(declared if declared is not None
                        else type_def.get("state_variables", {}))
        for _prop, _def in CHILD_RESERVED_PROP_SCHEMA.items():
            declared.setdefault(_prop, dict(_def))
        return declared

    def _format_child_id(self, child_type: str, local_id: int | str) -> str:
        type_def = self._child_type_def(child_type)
        id_format = type_def.get("id_format", {})
        id_kind = id_format.get("type", "integer")

        if id_kind == "string":
            if not isinstance(local_id, str):
                raise TypeError(
                    f"Child {child_type} local_id must be str (id_format.type "
                    f"is 'string'), got {type(local_id).__name__}: {local_id!r}"
                )
            if not _CHILD_STRING_ID_RE.match(local_id):
                raise ValueError(
                    f"Child {child_type} local_id {local_id!r} is not a valid "
                    f"string id (allowed characters: letters, digits, '_', '-')"
                )
            max_len = id_format.get("max_length", _CHILD_STRING_ID_MAX_LEN)
            if max_len and len(local_id) > max_len:
                raise ValueError(
                    f"Child {child_type} local_id {local_id!r} exceeds "
                    f"id_format.max_length {max_len}"
                )
            return local_id

        if id_kind != "integer":
            raise ValueError(
                f"Child type {child_type!r} id_format.type {id_kind!r} not "
                f"supported (only 'integer' and 'string' are supported)"
            )
        # bool is an int subclass; reject it so register_child(t, True) does
        # not silently land at id 1.
        if not isinstance(local_id, int) or isinstance(local_id, bool):
            raise TypeError(
                f"Child {child_type} local_id must be int, got "
                f"{type(local_id).__name__}: {local_id!r}"
            )
        min_id = id_format.get("min", 1)
        max_id = id_format.get("max")
        if local_id < min_id:
            raise ValueError(
                f"Child {child_type} local_id {local_id} < min {min_id}"
            )
        if max_id is not None and local_id > max_id:
            raise ValueError(
                f"Child {child_type} local_id {local_id} > max {max_id}"
            )
        pad = id_format.get("pad_width", 0)
        return f"{local_id:0{pad}d}" if pad else str(local_id)

    def _child_state_key(
        self, child_type: str, local_id: int | str, prop: str
    ) -> str:
        padded = self._format_child_id(child_type, local_id)
        return f"device.{self.device_id}.{child_type}.{padded}.{prop}"

    def _child_state_prefix(self, child_type: str, local_id: int | str) -> str:
        padded = self._format_child_id(child_type, local_id)
        return f"device.{self.device_id}.{child_type}.{padded}"

    def _validate_child_prop(
        self, child_type: str, local_id: int | str, prop: str
    ) -> None:
        schema = self._effective_child_schema(child_type, local_id)
        if prop not in schema:
            raise ValueError(
                f"Child {child_type} property {prop!r} not declared in "
                f"child_entity_types[{child_type!r}].state_variables"
                + (" (or this child's dynamic schema)"
                   if self._child_type_def(child_type).get("dynamic") else "")
            )

    @staticmethod
    def _default_for_var_def(var_def: dict[str, Any]) -> Any:
        """What a declared state variable holds before the device has spoken:
        nothing.

        The key is created; the value is not invented. A driver that has not
        heard from its hardware has no reading, and the platform's own
        consumers already read ``None`` that way -- a monitor tile draws "--"
        rather than 0, an alert rule matches no threshold, and a macro guard
        makes no decision.

        It used to hand back a typed value here (a numeric at its declared
        ``min``, an enum at its first value, a string at ``""``), which nothing
        downstream could tell from a reading: a projector nobody had reached
        published 0 lamp hours against a true 450, and a monitor declaring a
        minimum above zero fired on it.

        ``var_def`` is unused and stays in the signature deliberately -- it is
        what a caller has in hand, and the platform-stub fidelity job compares
        this signature against the real ``BaseDriver``.

        The value-producing form of the rule still exists on the platform, as
        ``compiled_protocol.state_var_default``, for the simulator and the
        driver validator. Nothing in this repo's suite needs it.
        """
        return None

    def register_child(
        self,
        child_type: str,
        local_id: int | str,
        initial_state: dict[str, Any] | None = None,
        schema: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._format_child_id(child_type, local_id)   # validates id range

        type_def = self._child_type_def(child_type)
        if schema is not None:
            if not type_def.get("dynamic"):
                raise ValueError(
                    f"Child type {child_type!r} is not declared "
                    f"`dynamic: true`; a per-child schema is only allowed for "
                    f"dynamic child types"
                )
            if not isinstance(schema, dict):
                raise TypeError(
                    f"Child {child_type} schema must be a dict, got "
                    f"{type(schema).__name__}"
                )
            for prop, var_def in schema.items():
                if not isinstance(var_def, dict):
                    raise TypeError(
                        f"Child {child_type} schema property {prop!r} must map "
                        f"to a var-def dict, got {type(var_def).__name__}"
                    )

        bucket = self._children.setdefault(child_type, {})
        if local_id in bucket:
            # Idempotent, so a poll loop can call this opportunistically. A
            # *different* schema under the same id is the signature of a
            # sanitized-id collision and is worth saying out loud.
            if schema is not None and dict(schema) != self._child_schemas.get(
                (child_type, local_id)
            ):
                log.warning(
                    f"[{self.device_id}] register_child({child_type!r}, "
                    f"{local_id!r}) ignored: id already registered with a "
                    f"different schema — likely an id collision after "
                    f"sanitization, or a schema change without "
                    f"deregister_child() first"
                )
            return

        self._child_register_seq += 1
        bucket[local_id] = self._child_register_seq

        if schema is not None:
            self._child_schemas[(child_type, local_id)] = dict(schema)

        eff_schema = self._effective_child_schema(child_type, local_id)
        overrides = dict(initial_state or {})

        for prop in overrides:
            if prop not in eff_schema:
                # Roll the registration back so a fixed retry can succeed.
                del bucket[local_id]
                if not bucket:
                    del self._children[child_type]
                self._child_schemas.pop((child_type, local_id), None)
                raise ValueError(
                    f"Child {child_type} initial_state property {prop!r} "
                    f"not declared in child_entity_types[{child_type!r}]"
                    f".state_variables"
                    + (" (or this child's dynamic schema)"
                       if type_def.get("dynamic") else "")
                )

        padded = self._format_child_id(child_type, local_id)
        project_entry = self._project_child_entities.get(child_type, {}).get(padded)
        project_label = project_entry.get("label", "") if project_entry else ""

        updates: dict[str, Any] = {}
        for prop, var_def in eff_schema.items():
            if prop == "online":
                value = overrides.get("online", True)
            elif prop == "label":
                value = overrides.get("label", project_label)
            elif prop in ("offline_reason", "offline_detail"):
                # The platform's own arms. These describe the child rather than
                # report from it, so they say something rather than falling
                # through to "nothing reported" -- a child claims no fault
                # until one is asserted.
                value = overrides.get(prop, "")
            elif prop in overrides:
                value = overrides[prop]
            else:
                value = self._default_for_var_def(var_def)
            updates[self._child_state_key(child_type, local_id, prop)] = value

        self.state.set_batch(updates, source=f"device.{self.device_id}")

    def deregister_child(self, child_type: str, local_id: int | str) -> None:
        bucket = self._children.get(child_type)
        if bucket is None or local_id not in bucket:
            return
        prefix_dot = self._child_state_prefix(child_type, local_id) + "."
        for key in [k for k in self.state.snapshot() if k.startswith(prefix_dot)]:
            self.state.delete(key, source=f"device.{self.device_id}")
        del bucket[local_id]
        if not bucket:
            del self._children[child_type]
        self._child_schemas.pop((child_type, local_id), None)

    def list_children(self, child_type: str) -> list[int | str]:
        return list(self._children.get(child_type, {}).keys())

    def is_child_registered(self, child_type: str, local_id: int | str) -> bool:
        return local_id in self._children.get(child_type, {})

    def set_child_state(
        self, child_type: str, local_id: int | str, prop: str, value: Any
    ) -> None:
        if not self.is_child_registered(child_type, local_id):
            log.warning(
                f"[{self.device_id}] set_child_state for unregistered child "
                f"{child_type}/{local_id} (prop {prop!r}) skipped — call "
                f"register_child first"
            )
            return
        self._validate_child_prop(child_type, local_id, prop)
        self.state.set(
            self._child_state_key(child_type, local_id, prop),
            value,
            source=f"device.{self.device_id}",
        )

    @staticmethod
    def child_fault(code: str = "", message: str = "") -> dict[str, Any]:
        """Stand-in for the platform's child-fault fragment builder.

        Returns the three reserved keys to merge into whatever the driver is
        already writing for that child. `online` goes down with any code --
        the coupling is load-bearing, so a stub that left it alone would let a
        driver ship a wedged endpoint drawing green.
        """
        if not code:
            return {"online": True, "offline_reason": "", "offline_detail": ""}
        if not is_child_fault_code(code):
            raise ValueError(
                f"{code!r} is not a child fault code (expected one of "
                f"{', '.join(sorted(CHILD_FAULT_CODES))})"
            )
        return {
            "online": False,
            "offline_reason": code,
            "offline_detail": message or default_child_fault_message(code),
        }

    def set_child_state_batch(
        self, child_type: str, local_id: int | str, updates: dict[str, Any]
    ) -> None:
        if not self.is_child_registered(child_type, local_id):
            log.warning(
                f"[{self.device_id}] set_child_state_batch for unregistered "
                f"child {child_type}/{local_id} skipped — call register_child "
                f"first"
            )
            return
        for prop in updates:
            self._validate_child_prop(child_type, local_id, prop)
        self.state.set_batch(
            {
                self._child_state_key(child_type, local_id, prop): v
                for prop, v in updates.items()
            },
            source=f"device.{self.device_id}",
        )

    def set_children_state_batch(
        self, updates: list[tuple[str, int | str, dict[str, Any]]]
    ) -> None:
        live: list[tuple[str, int | str, dict[str, Any]]] = []
        for child_type, local_id, child_updates in updates:
            if not self.is_child_registered(child_type, local_id):
                log.warning(
                    f"[{self.device_id}] set_children_state_batch entry for "
                    f"unregistered child {child_type}/{local_id} skipped — "
                    f"call register_child first"
                )
                continue
            live.append((child_type, local_id, child_updates))
        for child_type, local_id, child_updates in live:
            for prop in child_updates:
                self._validate_child_prop(child_type, local_id, prop)
        namespaced: dict[str, Any] = {}
        for child_type, local_id, child_updates in live:
            for prop, value in child_updates.items():
                namespaced[self._child_state_key(child_type, local_id, prop)] = value
        if namespaced:
            self.state.set_batch(namespaced, source=f"device.{self.device_id}")

    def get_child_entity_types(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for ctype, definition in self.DRIVER_INFO.get(
                "child_entity_types", {}).items():
            merged = dict(definition)
            merged["state_variables"] = self._effective_child_schema(ctype)
            result[ctype] = merged
        return result

    def get_child_schema(
        self, child_type: str, local_id: int | str,
    ) -> dict[str, dict[str, Any]]:
        return self._effective_child_schema(child_type, local_id)

    def is_child_type_dynamic(self, child_type: str) -> bool:
        return bool(
            self.DRIVER_INFO.get("child_entity_types", {})
            .get(child_type, {})
            .get("dynamic")
        )

    def format_child_id(self, child_type: str, local_id: int | str) -> str:
        return self._format_child_id(child_type, local_id)

    def get_child_state(
        self, child_type: str, local_id: int | str,
    ) -> dict[str, Any]:
        if not self.is_child_registered(child_type, local_id):
            return {}
        return self.state.get_namespace(
            self._child_state_prefix(child_type, local_id)
        )

    async def refresh_children(self) -> Any:
        raise NotImplementedError(
            f"Driver {self.DRIVER_INFO.get('id', '?')} does not implement "
            f"refresh_children"
        )

    async def poll_children(
        self,
        child_type: str,
        fetch: Callable[[list[int]], Awaitable[dict[int, dict[str, Any]]]],
        batch_size: int = 50,
        inter_batch_delay: float = 0.1,
    ) -> None:
        """Paginated poll: fetch in batches, apply the whole poll atomically.

        Results for a child that was deregistered (or deregistered and
        re-registered, which resets its state) mid-poll are dropped, so a
        concurrent refresh cannot be clobbered by a stale write.
        """
        import asyncio

        ids = self.list_children(child_type)
        if not ids:
            return
        start_bucket = self._children.get(child_type, {})
        epochs = {lid: start_bucket.get(lid) for lid in ids}

        collected: dict[Any, dict[str, Any]] = {}
        for index in range(0, len(ids), batch_size):
            batch = ids[index:index + batch_size]
            if index and inter_batch_delay:
                await asyncio.sleep(inter_batch_delay)
            collected.update(await fetch(batch) or {})

        live_bucket = self._children.get(child_type, {})
        updates = [
            (child_type, lid, props)
            for lid, props in collected.items()
            if lid in live_bucket and live_bucket.get(lid) == epochs.get(lid)
        ]
        if updates:
            self.set_children_state_batch(updates)


# ── Simulators ──────────────────────────────────────────────────────────────

class StubBaseSimulator:
    """Stand-in for ``openavc.simulator.base.BaseSimulator``.

    ``state`` is a read-only copy, as it is on the real base -- simulator code
    that mutates ``self.state[...]`` directly is a bug the real class also
    swallows silently, so the copy is what makes the test see it.
    """

    SIMULATOR_INFO: dict[str, Any] = {}

    def __init__(self, device_id: str, config: dict | None = None):
        self.device_id = device_id
        self.config = config or {}
        self._state: dict[str, Any] = dict(
            self.SIMULATOR_INFO.get("initial_state", {})
        )
        self._error_modes: dict[str, dict] = dict(
            self.SIMULATOR_INFO.get("error_modes", {})
        )
        self._active_errors: set[str] = set()
        self._protocol_log: list[dict] = []

    @property
    def driver_id(self) -> str:
        return self.SIMULATOR_INFO.get("driver_id", "")

    @property
    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def set_state(self, key: str, value: Any) -> None:
        self._state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    @property
    def active_errors(self) -> set[str]:
        return set(self._active_errors)

    @property
    def available_errors(self) -> dict[str, dict]:
        return dict(self._error_modes)

    def inject_error(self, mode: str) -> None:
        # Unknown mode warns and returns; the real base does not raise, and a
        # stub that did would fail a test the platform would let through.
        if mode not in self._error_modes:
            log.warning("Unknown error mode '%s' on %s", mode, self.device_id)
            return
        self._active_errors.add(mode)
        for key, value in self._error_modes[mode].get("set_state", {}).items():
            self.set_state(key, value)

    def clear_error(self, mode: str) -> None:
        self._active_errors.discard(mode)

    def clear_all_errors(self) -> None:
        self._active_errors.clear()

    def has_error_behavior(self, behavior: str) -> bool:
        return any(
            self._error_modes.get(mode, {}).get("behavior") == behavior
            for mode in self._active_errors
        )

    def log_protocol(self, direction: str, data: bytes | str,
                     client_id: str = "") -> None:
        self._protocol_log.append(
            {"direction": direction, "data": data, "client_id": client_id}
        )

    def get_protocol_log(self, limit: int = 100) -> list[dict]:
        return self._protocol_log[-limit:]

    def clear_protocol_log(self) -> None:
        self._protocol_log.clear()


class StubTCPSimulator(StubBaseSimulator):
    """Stand-in for ``openavc.simulator.tcp_simulator.TCPSimulator``.

    ``push`` fans out to whatever the test registered in ``push_targets`` --
    the real class writes to every connected socket, and a test's fake
    transport stands in for one.
    """

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self.push_targets: list[Any] = []

    async def on_client_connected(self, client_id: str) -> bytes | None:
        return None

    def handle_command(self, data: bytes) -> bytes | None:
        return None

    async def push(self, data: bytes) -> None:
        for target in list(self.push_targets):
            result = target.deliver(data)
            if hasattr(result, "__await__"):
                await result


class StubHTTPSimulator(StubBaseSimulator):
    """Stand-in for ``openavc.simulator.http_simulator.HTTPSimulator``."""

    def handle_request(self, method: str, path: str, headers: dict[str, str],
                       body: str) -> Any:
        raise NotImplementedError


class StubUDPSimulator(StubBaseSimulator):
    """Stand-in for ``openavc.simulator.udp_simulator.UDPSimulator``."""

    def handle_command(self, data: bytes) -> bytes | None:
        return None


# ── sys.modules installation ────────────────────────────────────────────────

#: The stub module tree, as {dotted name: {attribute: value}}. A package entry
#: (one that has submodules) gets an empty ``__path__`` so ``import a.b``
#: resolves through it.
def _default_tree() -> dict[str, dict[str, Any]]:
    return {
        "openavc": {},
        "openavc.drivers": {},
        "openavc.drivers.base": {
            "BaseDriver": StubBaseDriver,
            "CommandParamError": CommandParamError,
            "DeviceSettingValueError": DeviceSettingValueError,
            "UndeclaredStateError": UndeclaredStateError,
            "ConnectionFaultError": ConnectionFaultError,
        },
        "openavc.core": {},
        "openavc.core.connection_fault": {
            "CHILD_FAULT_CODES": CHILD_FAULT_CODES,
            "CHILD_NOT_RESPONDING": CHILD_NOT_RESPONDING,
            "CHILD_SERVICE_FAULT": CHILD_SERVICE_FAULT,
            "ConnectionFaultError": ConnectionFaultError,
            "default_child_fault_message": default_child_fault_message,
            "is_child_fault_code": is_child_fault_code,
        },
        "openavc.transport": {},
        "openavc.transport.frame_parsers": {
            "FrameParser": FrameParser,
            "CallableFrameParser": CallableFrameParser,
            "DelimiterFrameParser": DelimiterFrameParser,
            "DEFAULT_MAX_BUFFER": DEFAULT_MAX_BUFFER,
        },
        "openavc.discovery": {},
        "openavc.discovery.companion": {"ProbeContext": StubProbeContext},
        "openavc.utils": {},
        "openavc.utils.logger": {"get_logger": _get_logger},
        "openavc.simulator": {},
        "openavc.simulator.base": {"BaseSimulator": StubBaseSimulator},
        "openavc.simulator.tcp_simulator": {"TCPSimulator": StubTCPSimulator},
        "openavc.simulator.http_simulator": {"HTTPSimulator": StubHTTPSimulator},
        "openavc.simulator.udp_simulator": {"UDPSimulator": StubUDPSimulator},
    }


def _get_logger(name: str = "openavc") -> logging.Logger:
    """Stand-in for ``openavc.utils.logger.get_logger``.

    A real ``logging.Logger``, not a swallow-everything object, so a test can
    assert on the warnings a driver emits with ``caplog``.
    """
    return logging.getLogger(name)


def stub_modules(
    overrides: dict[str, dict[str, Any]] | None = None,
    *,
    base_driver: type | None = None,
) -> dict[str, ModuleType]:
    """Build the stub module tree without touching ``sys.modules``.

    Use this when a test needs to re-install the same module objects for each
    test's *runtime* (a driver that imports a transport lazily inside a method
    resolves it through ``sys.modules`` at call time, after conftest has rolled
    the collection-time stubs back)::

        _STUBS = stub_modules(base_driver=_FakeBaseDriver)

        @pytest.fixture(autouse=True)
        def _reinstall(monkeypatch):
            for name, mod in _STUBS.items():
                monkeypatch.setitem(sys.modules, name, mod)

    ``overrides`` maps a dotted module name to the attributes to set on it,
    merged over the defaults; naming a module the defaults do not carry creates
    it (``{"openavc.transport.ir_codec": {"IRCode": _FakeIRCode}}``).
    ``base_driver`` is shorthand for the commonest override of all.
    """
    tree = _default_tree()
    if base_driver is not None:
        tree["openavc.drivers.base"]["BaseDriver"] = base_driver
    for name, attrs in (overrides or {}).items():
        tree.setdefault(name, {}).update(attrs)

    # A module is a package if anything else in the tree lives beneath it.
    names = set(tree)
    modules: dict[str, ModuleType] = {}
    for name in sorted(names):
        module = ModuleType(name)
        if any(other.startswith(name + ".") for other in names):
            module.__path__ = []  # type: ignore[attr-defined]
        for attr, value in tree[name].items():
            setattr(module, attr, value)
        modules[name] = module
    return modules


def install_stubs(
    overrides: dict[str, dict[str, Any]] | None = None,
    *,
    base_driver: type | None = None,
) -> dict[str, ModuleType]:
    """Install the platform stand-ins into ``sys.modules`` and return them.

    Call this from a test module before it loads its driver. ``conftest.py``
    removes what this adds once the module is collected, so the stubs cannot
    leak into a later test module that wants the real platform.

    See :func:`stub_modules` for the ``overrides`` / ``base_driver`` arguments.
    """
    modules = stub_modules(overrides, base_driver=base_driver)
    sys.modules.update(modules)
    return modules


def load_module(name: str, path: Path | str) -> ModuleType:
    """Import a driver or simulator file directly, under ``name``.

    Call :func:`install_stubs` first -- the file's ``openavc.*`` imports run
    while this executes.
    """
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:            # pragma: no cover
        raise ImportError(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
