"""
Symetrix Composer Control Protocol — Simulator.

Implements the v7.0 Composer Control Protocol on TCP port 48631:

  - ASCII commands terminated by ``\\r``.
  - Responses: ``ACK``, ``NAK``, value lines, or ``#N=V`` push lines.
  - Tracks per-client subscription state (PU 1 / PU 0). When push is
    enabled the sim emits ``#N=V`` lines for every CS / CSQ / CC
    change so the driver under test sees its own writes mirrored
    through the push channel — that's how a real Symetrix unit
    behaves.

Driver side: ``audio/symetrix_composer.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


DEFAULT_NUM_CONTROLLERS = 64

PUSH_CMD_RE = re.compile(r"^PU\s+([01])(?:\s|$)")
CS_CMD_RE = re.compile(r"^(CS|CSQ)\s+(\d+)\s+(\d+)$")
CC_CMD_RE = re.compile(r"^CC\s+(\d+)\s+([01])\s+(\d+)$")
GS_CMD_RE = re.compile(r"^GS\s+(\d+)$")
LP_CMD_RE = re.compile(r"^LP\s+(\d+)$")
FU_CMD_RE = re.compile(r"^FU(?:\s+(\d+))?$")
GPR_CMD_RE = re.compile(r"^GPR$")
V_CMD_RE = re.compile(r"^V$")
RI_CMD_RE = re.compile(r"^RI$")
PUR_CMD_RE = re.compile(r"^PUR$")
QUIT_CMD_RE = re.compile(r"^Q!$")


def _fmt_push(number: int, value: int) -> bytes:
    return f"#{number:05d}={value:05d}\r".encode("ascii")


class SymetrixComposerSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "symetrix_composer",
        "name": "Symetrix Composer DSP Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 48631,
        "delimiter": "\r",
        "initial_state": {
            "model": "Radius NX 12x8",
            "firmware": "8.5.0",
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        n = int(self.config.get("controllers", DEFAULT_NUM_CONTROLLERS))
        self._n_controllers = max(1, min(10000, n))
        # Sparse store keyed by controller number (1..10000)
        self._controllers: dict[int, int] = {}
        # Pre-populate first num_controllers with deterministic values
        # so PUR has something to push.
        for i in range(1, self._n_controllers + 1):
            self._controllers[i] = (i * 1024) % 65536

        self._firmware = self.config.get("firmware", "8.5.0 (3.6.4)")
        self._ip_address = self.config.get(
            "ip_address", "192.168.1.42"
        )
        self._last_preset = 0

        # Per-client push state.
        self._push_enabled: dict[str, bool] = {}

    # ── Connection ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        self._push_enabled[client_id] = False
        return None

    # ── Command dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        # The framework strips the \r terminator. Drop empty lines.
        line = data.decode("ascii", errors="replace").strip()
        if not line:
            return None
        client_id = self._latest_client_id()
        if client_id is None:
            return None

        if QUIT_CMD_RE.match(line):
            # Q!: caller wants to close the session. ACK and let the
            # client tear down on its end.
            return b"ACK\r"

        if V_CMD_RE.match(line):
            return f"{self._firmware}\r".encode("ascii")

        if RI_CMD_RE.match(line):
            return f"{self._ip_address}\r".encode("ascii")

        if GPR_CMD_RE.match(line):
            return f"{self._last_preset:04d}\r".encode("ascii")

        if FU_CMD_RE.match(line):
            return b"ACK\r"

        m = PUSH_CMD_RE.match(line)
        if m:
            self._push_enabled[client_id] = m.group(1) == "1"
            return b"ACK\r"

        if PUR_CMD_RE.match(line):
            # Push every controller's current value back. In real
            # hardware this only fires push-enabled controllers; the
            # sim treats every tracked controller as push-enabled.
            asyncio.create_task(self._push_refresh(client_id))
            return None

        m = CS_CMD_RE.match(line)
        if m:
            verb, number_str, value_str = m.group(1), m.group(2), m.group(3)
            number = int(number_str)
            value = max(0, min(65535, int(value_str)))
            if verb == "CSQ":
                # CSQ always ACKs — no existence check.
                self._set_controller(number, value)
                asyncio.create_task(self._maybe_push(client_id, number))
                return b"ACK\r"
            if 1 <= number <= self._n_controllers:
                self._set_controller(number, value)
                asyncio.create_task(self._maybe_push(client_id, number))
                return b"ACK\r"
            return b"NAK\r"

        m = CC_CMD_RE.match(line)
        if m:
            number = int(m.group(1))
            inc = m.group(2) == "1"
            amount = int(m.group(3))
            if 1 <= number <= self._n_controllers:
                current = self._controllers.get(number, 0)
                if inc:
                    new = min(65535, current + amount)
                else:
                    new = max(0, current - amount)
                self._set_controller(number, new)
                asyncio.create_task(self._maybe_push(client_id, number))
                return b"ACK\r"
            return b"NAK\r"

        m = GS_CMD_RE.match(line)
        if m:
            number = int(m.group(1))
            if 1 <= number <= self._n_controllers:
                value = self._controllers.get(number, 0)
                return f"{value:05d}\r".encode("ascii")
            return b"NAK\r"

        m = LP_CMD_RE.match(line)
        if m:
            preset = int(m.group(1))
            if 1 <= preset <= 1000:
                self._last_preset = preset
                return b"ACK\r"
            return b"NAK\r"

        return b"NAK\r"

    def _latest_client_id(self) -> str | None:
        if not self._clients:
            return None
        return next(reversed(self._clients))

    def _set_controller(self, number: int, value: int) -> None:
        self._controllers[number] = value

    # ── Push helpers ──

    async def _maybe_push(self, client_id: str, number: int) -> None:
        if not self._push_enabled.get(client_id):
            return
        await asyncio.sleep(0)
        value = self._controllers.get(number, 0)
        await self.push_to(client_id, _fmt_push(number, value))

    async def _push_refresh(self, client_id: str) -> None:
        if not self._push_enabled.get(client_id):
            return
        await asyncio.sleep(0)
        # Push everything we know about, sorted by number for
        # determinism.
        for number in sorted(self._controllers):
            value = self._controllers[number]
            await self.push_to(client_id, _fmt_push(number, value))

    # ── Test hooks (not part of the protocol) ──

    def trigger_external_change(self, number: int, value: int) -> None:
        """Force a controller change from outside the wire — e.g. an
        operator turned a knob on the front panel — and push to every
        push-enabled subscriber.

        Used by the round-trip test to verify the driver reacts to
        unsolicited state changes.
        """
        value = max(0, min(65535, int(value)))
        self._controllers[number] = value
        for client_id, enabled in list(self._push_enabled.items()):
            if enabled:
                asyncio.create_task(
                    self.push_to(client_id, _fmt_push(number, value))
                )
