"""Shared stand-ins for the parts of the platform ``BaseDriver`` lifecycle
that every fake-based driver test models identically.

Most driver tests in this repo run their driver against a hand-written
``_FakeBaseDriver`` so the community CI stays self-contained (no ``openavc``
install). Those fakes are deliberately per-driver — each one wires that
driver's own transport, ``connect()`` ceremony and child registry, and that
part is not duplication worth erasing. A measured census (2026-07-25) put the
genuinely-identical share at ~13% of the fake bulk; this module holds exactly
that share, verified byte-identical across the files it was lifted from:

  * the liveness watchdog (``_health_loop`` / ``_force_disconnect`` /
    ``_health_enabled`` / ``_liveness_probe``, plus the dominant
    ``_start_health_loop`` / ``_stop_health_loop`` pair),
  * ``_transport_kwargs`` and ``_create_frame_parser``,
  * the fault stash (``_stash_fault`` / ``_stash_transport_error``),
  * the no-op polling hooks.

Inherit it and override anything that differs — a subclass method always wins,
so a fake that models a hook differently keeps its own version. In particular
several fakes deliberately override ``_start_health_loop`` to raise, as a
tripwire for a driver growing a liveness probe the fake doesn't model; that
override is intentional and must stay.

**This module must never import ``server`` or ``simulator``, and must never
install stubs at import time.** ``conftest.py`` brackets ``sys.modules`` per
test module, but only for the ``server`` / ``simulator`` / ``websockets``
roots — this helper is cached like any other module, so a top-level stub
install here would fire once, get rolled back after the first importer, and
starve every module after it. Stubs stay in each test module, installed by a
function that module calls at its own load.
"""

from __future__ import annotations

import asyncio


class LifecycleFake:
    """The identical core of the per-file ``_FakeBaseDriver`` copies."""

    # Watchdog knobs — mirror the platform's BaseDriver class attributes.
    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the device stopped answering keep-alive probes."
    )

    # -- transport construction hooks --

    def _transport_kwargs(self, transport_type, kwargs):
        return kwargs

    def _create_frame_parser(self):
        return None

    # -- polling (the platform's real loop is out of scope for the fakes) --

    async def start_polling(self, interval) -> None:
        pass

    async def stop_polling(self) -> None:
        pass

    # -- fault stash --

    def _stash_fault(self, code, message="") -> None:
        self.stashed_fault = (code, message)

    def _stash_transport_error(self) -> None:
        pass

    # -- liveness watchdog --

    async def _liveness_probe(self) -> None:
        raise NotImplementedError

    def _health_enabled(self) -> bool:
        return type(self)._liveness_probe is not LifecycleFake._liveness_probe

    def _start_health_loop(self) -> None:
        if self._health_task is None or self._health_task.done():
            self._health_failures = 0
            self._health_task = asyncio.ensure_future(self._health_loop())

    def _stop_health_loop(self) -> None:
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
        self._health_task = None

    def _force_disconnect(self, code="no_response", message="") -> None:
        self._health_task = None
        self._stash_fault(code, message)
        self._handle_transport_disconnect()

    async def _health_loop(self) -> None:
        interval = float(self.HEALTH_INTERVAL_S)
        timeout = float(self.HEALTH_TIMEOUT_S)
        max_failures = max(int(self.HEALTH_MAX_FAILURES), 1)
        try:
            while self.transport is not None and getattr(
                    self.transport, "connected", False):
                await asyncio.sleep(interval)
                if not (self.transport is not None and getattr(
                        self.transport, "connected", False)):
                    return
                try:
                    await asyncio.wait_for(self._liveness_probe(), timeout)
                    self._health_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._health_failures += 1
                    if self._health_failures >= max_failures:
                        self._force_disconnect(
                            "no_response", self.HEALTH_FAULT_MESSAGE)
                        return
        except asyncio.CancelledError:
            return
