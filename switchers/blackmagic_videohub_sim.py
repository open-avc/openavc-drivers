"""
Blackmagic Videohub — Simulator.

Implements the Videohub Ethernet Protocol v2.3 server side:

  - Sends initial status dump (preamble, device, labels, routing, locks)
    on client connect.
  - Accepts request blocks (VIDEO OUTPUT ROUTING, INPUT LABELS,
    OUTPUT LABELS, VIDEO OUTPUT LOCKS) and PING.
  - Replies ACK / NAK and pushes the resulting status block back.

Simulates a 16x16 Smart Videohub by default (no serial ports). Set the
``inputs`` / ``outputs`` / ``monitors`` config keys to emulate a wider frame
(e.g. a 40x40 with 2 monitoring outputs) — useful for exercising the driver's
device-sized child roster.
"""

from __future__ import annotations

import asyncio
import logging
import re

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


DEFAULT_INPUTS = 16
DEFAULT_OUTPUTS = 16
DEFAULT_MONITORS = 0


class BlackmagicVideohubSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "blackmagic_videohub",
        "name": "Blackmagic Videohub Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 9990,
        # No delimiter — we handle line/block parsing in handle_command and
        # override _read_messages to preserve blank lines.
        "initial_state": {
            "model": "Blackmagic Smart Videohub",
            "video_inputs": DEFAULT_INPUTS,
            "video_outputs": DEFAULT_OUTPUTS,
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "video_inputs", "label": "Inputs"},
            {"type": "indicator", "key": "video_outputs", "label": "Outputs"},
        ],
        "delays": {
            "command_response": 0.01,
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        n_in = int(self.config.get("inputs", DEFAULT_INPUTS))
        n_out = int(self.config.get("outputs", DEFAULT_OUTPUTS))
        n_mon = int(self.config.get("monitors", DEFAULT_MONITORS))
        self._n_inputs = n_in
        self._n_outputs = n_out
        self._n_monitors = n_mon
        self.set_state("video_inputs", n_in)
        self.set_state("video_outputs", n_out)
        self.set_state("monitoring_outputs", n_mon)

        self._input_labels: list[str] = [f"Input {i + 1}" for i in range(n_in)]
        self._output_labels: list[str] = [f"Output {i + 1}" for i in range(n_out)]
        self._monitor_labels: list[str] = [f"Monitor {i + 1}" for i in range(n_mon)]
        # Identity routing: output i ← input i (clamped to inputs available).
        self._routing: list[int] = [
            min(i, n_in - 1) for i in range(n_out)
        ]
        self._monitor_routing: list[int] = [
            min(i, n_in - 1) for i in range(n_mon)
        ]
        # All outputs unlocked.
        self._locks: list[str] = ["U"] * n_out

        # Per-client block-reassembly buffer (keyed by client id).
        self._client_blocks: dict[str, dict] = {}
        # Per-client owner identity for lock state — single global "owner"
        # is fine for sim purposes.
        self._owner_client_id: str | None = None

    # ── Read each line, blank lines included ──

    async def _read_messages(self, reader: asyncio.StreamReader):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30.0)
        except asyncio.TimeoutError:
            return []
        if not raw:
            return None
        # Strip trailing \n and optional preceding \r, but preserve empty
        # lines as b"" so the block parser sees them.
        line = raw.rstrip(b"\r\n")
        return [line]

    # ── Greeting: full state dump on connect ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        self._client_blocks[client_id] = {"header": None, "lines": []}
        return self._render_initial_dump().encode("utf-8")

    def _render_initial_dump(self) -> str:
        out: list[str] = []
        out.append("PROTOCOL PREAMBLE:")
        out.append("Version: 2.3")
        out.append("")
        out.append("VIDEOHUB DEVICE:")
        out.append("Device present: true")
        out.append(f"Model name: {self.state.get('model', 'Blackmagic Smart Videohub')}")
        out.append(f"Video inputs: {self._n_inputs}")
        out.append("Video processing units: 0")
        out.append(f"Video outputs: {self._n_outputs}")
        out.append(f"Video monitoring outputs: {self._n_monitors}")
        out.append("Serial ports: 0")
        out.append("")
        out.append(self._render_input_labels())
        out.append(self._render_output_labels())
        if self._n_monitors:
            out.append(self._render_monitor_labels())
        out.append(self._render_routing())
        if self._n_monitors:
            out.append(self._render_monitor_routing())
        out.append(self._render_locks())
        return "\n".join(out) + "\n"

    def _render_input_labels(self) -> str:
        body = "INPUT LABELS:\n"
        body += "\n".join(f"{i} {label}" for i, label in enumerate(self._input_labels))
        body += "\n"
        return body

    def _render_output_labels(self) -> str:
        body = "OUTPUT LABELS:\n"
        body += "\n".join(f"{i} {label}" for i, label in enumerate(self._output_labels))
        body += "\n"
        return body

    def _render_monitor_labels(self) -> str:
        body = "MONITORING OUTPUT LABELS:\n"
        body += "\n".join(
            f"{i} {label}" for i, label in enumerate(self._monitor_labels)
        )
        body += "\n"
        return body

    def _render_routing(self) -> str:
        body = "VIDEO OUTPUT ROUTING:\n"
        body += "\n".join(f"{i} {src}" for i, src in enumerate(self._routing))
        body += "\n"
        return body

    def _render_monitor_routing(self) -> str:
        body = "VIDEO MONITORING OUTPUT ROUTING:\n"
        body += "\n".join(
            f"{i} {src}" for i, src in enumerate(self._monitor_routing)
        )
        body += "\n"
        return body

    def _render_locks(self) -> str:
        body = "VIDEO OUTPUT LOCKS:\n"
        body += "\n".join(f"{i} {lk}" for i, lk in enumerate(self._locks))
        body += "\n"
        return body

    # ── Per-line handling ──

    def handle_command(self, data: bytes) -> bytes | None:
        """Each call gets one line (b'' for blank). We treat the most-recent
        client to send data as the active block owner; multi-client interleave
        is rare in practice and not worth tracking precisely in a sim."""
        line = data.decode("utf-8", errors="replace")
        # We don't get client_id from the framework's call signature, so
        # use a single shared block buffer across all clients. This is
        # sufficient for the test harness which uses one client.
        block = self._client_blocks.setdefault(
            "_shared", {"header": None, "lines": []}
        )

        if line == "":
            # Block terminator — process and respond.
            return self._finalize_block(block)

        # Header lines end with ":" (and are uppercase per spec).
        if line.endswith(":") and line == line.upper():
            block["header"] = line[:-1]
            block["lines"] = []
            return None

        # Inside a block: accumulate.
        if block["header"] is not None:
            block["lines"].append(line)
        return None

    def _finalize_block(self, block: dict) -> bytes:
        header = block["header"]
        lines = block["lines"]
        block["header"] = None
        block["lines"] = []

        if header is None:
            return b""

        # PING — server replies ACK with no payload.
        if header == "PING":
            return b"ACK\n\n"

        # Empty body == status dump request.
        if not lines:
            payload = self._render_block_for(header)
            if payload is None:
                return b"NAK\n\n"
            return ("ACK\n\n" + payload + "\n").encode("utf-8")

        # Body present — apply the change.
        if header == "VIDEO OUTPUT ROUTING":
            applied = self._apply_routing_block(lines)
            if not applied:
                return b"NAK\n\n"
            return ("ACK\n\n" + self._render_routing() + "\n").encode("utf-8")

        if header == "INPUT LABELS":
            applied = self._apply_label_block(lines, self._input_labels, self._n_inputs)
            if not applied:
                return b"NAK\n\n"
            return ("ACK\n\n" + self._render_input_labels() + "\n").encode("utf-8")

        if header == "OUTPUT LABELS":
            applied = self._apply_label_block(lines, self._output_labels, self._n_outputs)
            if not applied:
                return b"NAK\n\n"
            return ("ACK\n\n" + self._render_output_labels() + "\n").encode("utf-8")

        if header == "VIDEO OUTPUT LOCKS":
            applied = self._apply_lock_block(lines)
            if not applied:
                return b"NAK\n\n"
            return ("ACK\n\n" + self._render_locks() + "\n").encode("utf-8")

        if header == "MONITORING OUTPUT LABELS":
            applied = self._apply_label_block(
                lines, self._monitor_labels, self._n_monitors
            )
            if not applied:
                return b"NAK\n\n"
            return ("ACK\n\n" + self._render_monitor_labels() + "\n").encode("utf-8")

        if header == "VIDEO MONITORING OUTPUT ROUTING":
            applied = self._apply_monitor_routing_block(lines)
            if not applied:
                return b"NAK\n\n"
            return (
                "ACK\n\n" + self._render_monitor_routing() + "\n"
            ).encode("utf-8")

        # Unknown header.
        return b"NAK\n\n"

    def _render_block_for(self, header: str) -> str | None:
        if header == "VIDEOHUB DEVICE":
            # Return the device block alone.
            out = "VIDEOHUB DEVICE:\n"
            out += "Device present: true\n"
            out += f"Model name: {self.state.get('model', 'Blackmagic Smart Videohub')}\n"
            out += f"Video inputs: {self._n_inputs}\n"
            out += "Video processing units: 0\n"
            out += f"Video outputs: {self._n_outputs}\n"
            out += f"Video monitoring outputs: {self._n_monitors}\n"
            out += "Serial ports: 0\n"
            return out
        if header == "INPUT LABELS":
            return self._render_input_labels()
        if header == "OUTPUT LABELS":
            return self._render_output_labels()
        if header == "VIDEO OUTPUT ROUTING":
            return self._render_routing()
        if header == "VIDEO OUTPUT LOCKS":
            return self._render_locks()
        if header == "MONITORING OUTPUT LABELS":
            return self._render_monitor_labels()
        if header == "VIDEO MONITORING OUTPUT ROUTING":
            return self._render_monitor_routing()
        return None

    def _apply_routing_block(self, lines: list[str]) -> bool:
        applied = False
        for line in lines:
            m = re.match(r"^(\d+) (\d+)$", line)
            if not m:
                return False
            out_idx = int(m.group(1))
            in_idx = int(m.group(2))
            if not (0 <= out_idx < self._n_outputs):
                return False
            if not (0 <= in_idx < self._n_inputs):
                return False
            self._routing[out_idx] = in_idx
            applied = True
        return applied

    def _apply_monitor_routing_block(self, lines: list[str]) -> bool:
        applied = False
        for line in lines:
            m = re.match(r"^(\d+) (\d+)$", line)
            if not m:
                return False
            out_idx = int(m.group(1))
            in_idx = int(m.group(2))
            if not (0 <= out_idx < self._n_monitors):
                return False
            if not (0 <= in_idx < self._n_inputs):
                return False
            self._monitor_routing[out_idx] = in_idx
            applied = True
        return applied

    def _apply_label_block(
        self, lines: list[str], target: list[str], size: int
    ) -> bool:
        applied = False
        for line in lines:
            parts = line.split(" ", 1)
            if len(parts) < 1:
                return False
            try:
                idx = int(parts[0])
            except ValueError:
                return False
            label = parts[1] if len(parts) > 1 else ""
            if not (0 <= idx < size):
                return False
            target[idx] = label
            applied = True
        return applied

    def _apply_lock_block(self, lines: list[str]) -> bool:
        applied = False
        for line in lines:
            m = re.match(r"^(\d+) ([UOFL])$", line)
            if not m:
                return False
            idx = int(m.group(1))
            code = m.group(2)
            if not (0 <= idx < self._n_outputs):
                return False
            # 'O' = lock to current client (just record as locked owned).
            # 'L' = lock to a different client (rare from a client send;
            #       accept as locked).
            # 'U' = unlock if we own (or anyone — sim is lenient).
            # 'F' = force unlock — server replies with U.
            if code == "F":
                self._locks[idx] = "U"
            else:
                self._locks[idx] = code
            applied = True
        return applied
