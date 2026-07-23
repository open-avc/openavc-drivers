"""
OpenAVC Blackmagic Videohub Driver.

Controls Blackmagic Design Videohub routers over the Videohub Ethernet
Protocol — a text-based, block-oriented protocol on TCP port 9990.

Models covered (any unit running Videohub Server v4.9.x or later):
    Blackmagic Videohub 12G              10x10, 20x20, 40x40
    Blackmagic Videohub 80x80 12G        80x80
    Smart Videohub CleanSwitch 12x12     12x12
    Smart Videohub                       16x16, 20x20
    Universal Videohub                   modular up to 72x144 / 288
    Videohub Master Control Pro          control surface
    Videohub Smart Control Pro           control surface

Protocol: text, lines terminated by LF. Server pushes blocks of the form:

    HEADER:
    <line>
    <line>
    <blank line terminates block>

Clients send the same shape to request changes. Server replies ACK / NAK
to each client request. Source: Videohub Ethernet Protocol v2.3, Nov 2023.

Design:
  * **Child entities** — inputs, outputs, and monitoring outputs are modeled
    as addressable child entities, sized from the VIDEOHUB DEVICE block the
    unit reports on connect. This replaces the earlier flat, hard-capped
    ``in_16_label`` / ``out_16_input`` / ``mon_4_input`` state keys, which
    silently truncated on frames wider than 16×16 (a 40×40 or 80×80 unit
    routed correctly but its ports past 16 landed as undeclared state keys
    the UI couldn't render). Each port's device-reported label lives in the
    child's ``name`` prop; routed source and lock state live on the output/
    monitor children.
  * **Push + liveness** — the Videohub server pushes every state change
    unsolicited, so the driver does not poll by default (``poll_interval`` 0).
    Because a silent link drop (cable pull, frozen unit) produces no TCP FIN,
    a PING/ACK watchdog force-reconnects after a few missed replies so the
    device flips offline instead of appearing online forever.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# Largest current Videohub is the Universal Videohub 288 (288×288); monitoring
# output counts are far smaller but share the bound for simplicity. Children
# are registered to the count the unit actually reports, not this ceiling.
PORT_MAX = 288

LOCK_MAP = {"U": "unlocked", "O": "owned", "L": "locked"}

# Liveness: push protocol with no polling, so a silent drop is otherwise
# invisible. The BaseDriver watchdog (see _liveness_probe) sends PING on the
# default HEALTH_* cadence (30 s interval, 5 s reply deadline, 2 misses).


def _input_state_vars() -> dict[str, dict[str, Any]]:
    return {
        "name": {"type": "string", "label": "Label", "cloud_priority": "low"},
    }


def _output_state_vars() -> dict[str, dict[str, Any]]:
    return {
        "input": {"type": "integer", "label": "Source", "cloud_priority": "high"},
        "lock": {
            "type": "enum",
            "values": ["unlocked", "owned", "locked"],
            "label": "Lock",
            "cloud_priority": "high",
        },
        "name": {"type": "string", "label": "Label", "cloud_priority": "low"},
    }


def _monitor_state_vars() -> dict[str, dict[str, Any]]:
    return {
        "input": {"type": "integer", "label": "Source", "cloud_priority": "high"},
        "name": {"type": "string", "label": "Label", "cloud_priority": "low"},
    }


class BlackmagicVideohubDriver(BaseDriver):
    """Blackmagic Videohub Ethernet Protocol driver."""

    DRIVER_INFO = {
        "id": "blackmagic_videohub",
        "name": "Blackmagic Videohub",
        "manufacturer": "Blackmagic Design",
        "category": "switcher",
        "version": "1.3.2",
        "author": "OpenAVC",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "description": (
            "Controls Blackmagic Design Videohub routers over the Videohub "
            "Ethernet Protocol (TCP 9990). Video and monitoring outputs, "
            "inputs, routing, output locks, and labels are modeled as child "
            "entities sized to the connected frame — from a 12×12 Smart "
            "Videohub to a 288×288 Universal Videohub."
        ),
        "source_url": "https://documents.blackmagicdesign.com/DeveloperManuals/VideohubEthernetProtocol.pdf",
        "tags": ["matrix-switcher", "sdi", "12g", "videohub", "broadcast"],
        "verified": False,
        "simulated": True,
        "protocols": ["videohub"],
        "ports": [9990],
        "transport": "tcp",
        "discovery": {
            # Videohub routers advertise on `_blackmagic._tcp.local.` via
            # mDNS. The active tcp_probe is the reliable identifier: the
            # server sends its PROTOCOL PREAMBLE + VIDEOHUB DEVICE block the
            # moment a client connects (no request needed), so a connect-only
            # probe reads the banner and pulls the model name straight out.
            "mdns": "_blackmagic._tcp.local.",
            "port_open": [9990],
            "manufacturer_alias": [
                "blackmagic", "blackmagic design", "bmd",
            ],
            "tcp_probe": {
                "port": 9990,
                "expect_regex": r"PROTOCOL PREAMBLE|VIDEOHUB DEVICE",
                "extract_manufacturer": "Blackmagic Design",
                "extract": {
                    "model": {"regex": r"Model name:\s*(.+)"},
                },
            },
        },
        "compatible_models": [
            {
                "manufacturer": "Blackmagic Design",
                "models": [
                    "Smart Videohub CleanSwitch 12x12",
                ],
                "confidence": "full",
                "notes": (
                    "Confirmed against real hardware (firmware 9.0) via a "
                    "community test report: video routing, input/output "
                    "labels, output locks, and unsolicited state updates all "
                    "work. This model has no monitoring outputs, so monitoring "
                    "routing does not apply to it."
                ),
            },
            {
                "manufacturer": "Blackmagic Design",
                "models": [
                    "Blackmagic Videohub 12G 10x10",
                    "Blackmagic Videohub 12G 20x20",
                    "Blackmagic Videohub 12G 40x40",
                    "Blackmagic Videohub 80x80 12G",
                    "Smart Videohub 16x16",
                    "Smart Videohub 20x20",
                    "Universal Videohub 72",
                    "Universal Videohub 288",
                    "Videohub Master Control Pro",
                    "Videohub Smart Control Pro",
                ],
                "confidence": "untested",
                "notes": (
                    "All current Videohub products share the v2.3 Ethernet "
                    "Protocol. Inputs, outputs, and monitoring outputs are "
                    "registered as child entities sized to the frame the unit "
                    "reports, so wide routers (40×40, 80×80, Universal 288) "
                    "expose every port."
                ),
            },
        ],
        "help": {
            "overview": (
                "Blackmagic Videohub is a family of SDI matrix routers used "
                "in broadcast, post-production, and live event facilities. "
                "This driver speaks the Videohub Ethernet Protocol, used by "
                "every Videohub server-class device since 2010. Inputs, "
                "outputs, and monitoring outputs appear as child entities "
                "with their labels, routed source, and lock state."
            ),
            "setup": (
                "1. Connect the Videohub to your network via Ethernet.\n"
                "2. Find the unit's IP address from the front panel "
                "(Smart Videohub) or from Blackmagic Videohub Setup.\n"
                "3. The Videohub Ethernet Protocol on TCP 9990 is always "
                "enabled — no auth, no enable step.\n"
                "4. Enter the IP address in the device config; leave port "
                "at 9990.\n"
                "5. Outputs and inputs are 1-indexed. Pick them from the "
                "input/output dropdowns once the unit connects and its "
                "children are discovered."
            ),
        },
        "default_config": {
            "host": "",
            "port": 9990,
            "poll_interval": 0,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 9990,
                "label": "TCP Port",
                "description": "Videohub Ethernet Protocol port — always 9990.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "Set to 0 to disable polling. The Videohub server pushes "
                    "every state change unsolicited, so polling is rarely "
                    "needed; a PING watchdog handles liveness. Raise above 0 "
                    "only if you want a periodic full-state resync."
                ),
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "device_present": {"type": "boolean", "label": "Device Present"},
            "video_inputs": {"type": "integer", "label": "Video Inputs"},
            "video_outputs": {"type": "integer", "label": "Video Outputs"},
            "monitoring_outputs": {"type": "integer", "label": "Monitoring Outputs"},
            "protocol_version": {"type": "string", "label": "Protocol Version"},
        },
        "child_entity_types": {
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "integer", "min": 1, "max": PORT_MAX, "pad_width": 3},
                "state_variables": _input_state_vars(),
                "summary_fields": ["name"],
                "label_field": "name",
            },
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "integer", "min": 1, "max": PORT_MAX, "pad_width": 3},
                "state_variables": _output_state_vars(),
                "summary_fields": ["name", "input", "lock"],
                "label_field": "name",
            },
            "monitor": {
                "label": "Monitor Output",
                "label_plural": "Monitor Outputs",
                "id_format": {"type": "integer", "min": 1, "max": PORT_MAX, "pad_width": 3},
                "state_variables": _monitor_state_vars(),
                "summary_fields": ["name", "input"],
                "label_field": "name",
            },
        },
        "quick_actions": ["refresh"],
        "actions": [
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection",
                "icon": "search",
                "availability": "always",
            },
        ],
        "commands": {
            "route": {
                "label": "Route Output",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                        "help": "Destination video output.",
                    },
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                        "help": "Source video input.",
                    },
                },
                "help": "Route a video input to a video output.",
            },
            "route_monitoring": {
                "label": "Route Monitor Output",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "monitor",
                        "required": True,
                        "label": "Monitor Output",
                        "help": "Monitoring output.",
                    },
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                        "help": "Source video input.",
                    },
                },
                "help": (
                    "Route a video input to a monitoring output. Only works "
                    "on Videohubs that have monitoring outputs (Smart "
                    "Videohub family)."
                ),
            },
            "set_input_label": {
                "label": "Rename Input",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "label": {"type": "string", "required": True},
                },
                "help": "Set the friendly label shown on the front panel for an input.",
            },
            "set_output_label": {
                "label": "Rename Output",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "label": {"type": "string", "required": True},
                },
                "help": "Set the friendly label shown on the front panel for an output.",
            },
            "lock_output": {
                "label": "Lock Output",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                },
                "help": (
                    "Acquire a lock on an output so other clients cannot "
                    "change its routing or label."
                ),
            },
            "unlock_output": {
                "label": "Unlock Output",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                },
                "help": "Release a lock previously acquired by this client.",
            },
            "force_unlock_output": {
                "label": "Force Unlock Output",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                },
                "help": (
                    "Override a lock held by another client. Use sparingly — "
                    "you may step on someone else's session."
                ),
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Request a full state dump from the Videohub.",
            },
        },
    }

    # BaseDriver liveness watchdog fault wording (cadence/misses use the
    # HEALTH_* defaults: 30 s interval, 5 s deadline, 2 misses).
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the Videohub stopped answering PING keep-alive probes."
    )

    # ── Lifecycle ──

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state,
        events,
    ):
        self._line_buffer = b""
        self._current_block: str | None = None
        self._block_lines: list[str] = []
        self._ping_waiter: asyncio.Future | None = None
        super().__init__(device_id, config, state, events)

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # Raw transport (no delimiter framing) — we need to keep blank lines,
        # which the delimiter parser would silently drop.
        kwargs["delimiter"] = None
        return kwargs

    async def _initial_sync(self) -> None:
        # The server dumps full state on connect; a nudge poll covers the rare
        # case it does not, and (re)primes the child roster after a reconnect.
        await self.poll()

    async def _close_session(self) -> None:
        self._line_buffer = b""
        self._current_block = None
        self._block_lines = []

    # ── Sending ──

    async def _send_block(self, header: str, lines: list[str]) -> None:
        """Send a block: header line + content lines + blank line."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        body = header + "\n"
        for line in lines:
            body += line + "\n"
        body += "\n"
        await self.transport.send(body.encode("utf-8"))

    async def _send_query(self, header: str) -> None:
        """Send a header + blank line to request a status dump."""
        await self._send_block(header, [])

    def _coerce_child_ids(self, command: str, params: dict[str, Any]) -> None:
        """Coerce any child_id-typed param to a bare int for the wire format
        (the IDE hands us a zero-padded string from the child picker)."""
        cmd_def = self.DRIVER_INFO["commands"].get(command, {})
        for pname, pdef in cmd_def.get("params", {}).items():
            if pdef.get("type") == "child_id" and pname in params and params[pname] != "":
                try:
                    params[pname] = int(params[pname])
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"{command}: parameter {pname!r} must be an integer "
                        f"id, got {params[pname]!r}"
                    ) from e

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}
        self._coerce_child_ids(command, params)

        if command == "route":
            output = int(params["output"])
            in_idx = int(params["input"])
            await self._send_block(
                "VIDEO OUTPUT ROUTING:",
                [f"{output - 1} {in_idx - 1}"],
            )
        elif command == "route_monitoring":
            output = int(params["output"])
            in_idx = int(params["input"])
            await self._send_block(
                "VIDEO MONITORING OUTPUT ROUTING:",
                [f"{output - 1} {in_idx - 1}"],
            )
        elif command == "set_input_label":
            n = int(params["input"])
            label = str(params["label"])
            await self._send_block("INPUT LABELS:", [f"{n - 1} {label}"])
        elif command == "set_output_label":
            n = int(params["output"])
            label = str(params["label"])
            await self._send_block("OUTPUT LABELS:", [f"{n - 1} {label}"])
        elif command == "lock_output":
            n = int(params["output"])
            await self._send_block("VIDEO OUTPUT LOCKS:", [f"{n - 1} O"])
        elif command == "unlock_output":
            n = int(params["output"])
            await self._send_block("VIDEO OUTPUT LOCKS:", [f"{n - 1} U"])
        elif command == "force_unlock_output":
            n = int(params["output"])
            await self._send_block("VIDEO OUTPUT LOCKS:", [f"{n - 1} F"])
        elif command == "refresh":
            await self.poll()
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return
        try:
            for header in (
                "VIDEOHUB DEVICE:",
                "INPUT LABELS:",
                "OUTPUT LABELS:",
                "MONITORING OUTPUT LABELS:",
                "VIDEO OUTPUT ROUTING:",
                "VIDEO OUTPUT LOCKS:",
                "VIDEO MONITORING OUTPUT ROUTING:",
            ):
                await self._send_query(header)
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")

    # ── Child refresh (IDE "Refresh from Device") ──

    async def refresh_children(self) -> dict[str, Any]:
        """Re-request the full state dump and let it reconcile the roster.

        The Videohub answers a status query within a few hundred ms; give the
        async dump a beat to arrive, then report the discovered counts.
        """
        await self.poll()
        await asyncio.sleep(0.75)
        return {
            "inputs": len(self.list_children("input")),
            "outputs": len(self.list_children("output")),
            "monitors": len(self.list_children("monitor")),
        }

    # ── Liveness probe (PING/ACK, BaseDriver watchdog hook) ──

    async def _liveness_probe(self) -> None:
        """Send a PING block and await the server's ACK.

        PING has no correlation id; any ACK (even one prompted by another
        request) proves the link is alive, which is all we need here. The
        reply deadline is enforced by the BaseDriver loop (HEALTH_TIMEOUT_S
        wraps this coroutine); after consecutive misses it tears the
        transport down with a typed ``no_response`` fault so the platform
        reconnects and the device card shows the real cause.
        """
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._ping_waiter = fut
        try:
            await self._send_block("PING:", [])
            await fut
        finally:
            if self._ping_waiter is fut:
                self._ping_waiter = None

    # ── Parsing ──

    async def on_data_received(self, data: bytes) -> None:
        """Accumulate, split into lines (preserving blank lines), dispatch."""
        self._line_buffer += data
        while b"\n" in self._line_buffer:
            line_bytes, self._line_buffer = self._line_buffer.split(b"\n", 1)
            # Strip trailing \r (Videohub may send \r\n on some platforms).
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]
            line = line_bytes.decode("utf-8", errors="replace")
            self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        """Dispatch a single line into the block parser."""
        # ACK / NAK arrive as standalone lines outside a block.
        if line in ("ACK", "NAK"):
            if self._current_block is None:
                if line == "ACK":
                    self._resolve_ping()
                else:
                    log.warning(f"[{self.device_id}] Server returned NAK")
                return
            # ACK/NAK that arrives mid-block is a protocol oddity; flush.
            self._finalize_block()
            return

        if line == "":
            # Blank line terminates the current block, if any.
            self._finalize_block()
            return

        # Header lines end with ":" and are uppercase.
        if line.endswith(":") and line == line.upper():
            # New block — finalize any previous (defensive; should already be done).
            if self._current_block is not None:
                self._finalize_block()
            self._current_block = line[:-1]  # strip trailing ":"
            self._block_lines = []
            return

        # Inside a block — accumulate.
        if self._current_block is not None:
            self._block_lines.append(line)

    def _resolve_ping(self) -> None:
        waiter = self._ping_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(True)

    def _finalize_block(self) -> None:
        if self._current_block is None:
            return
        block = self._current_block
        lines = self._block_lines
        self._current_block = None
        self._block_lines = []

        if block == "PROTOCOL PREAMBLE":
            for ln in lines:
                if ln.startswith("Version:"):
                    self.set_state("protocol_version", ln.split(":", 1)[1].strip())

        elif block == "VIDEOHUB DEVICE":
            self._apply_device_block(lines)

        elif block == "INPUT LABELS":
            self._apply_child_block(lines, "input", "name", _as_label)

        elif block == "OUTPUT LABELS":
            self._apply_child_block(lines, "output", "name", _as_label)

        elif block == "MONITORING OUTPUT LABELS":
            self._apply_child_block(lines, "monitor", "name", _as_label)

        elif block == "VIDEO OUTPUT ROUTING":
            self._apply_child_block(lines, "output", "input", _as_one_indexed)

        elif block == "VIDEO MONITORING OUTPUT ROUTING":
            self._apply_child_block(lines, "monitor", "input", _as_one_indexed)

        elif block == "VIDEO OUTPUT LOCKS":
            self._apply_child_block(lines, "output", "lock", _as_lock)

        # Other blocks (SERIAL PORT *, FRAME LABELS, etc.) are ignored —
        # their state is not exposed in this driver's surface.

    def _apply_device_block(self, lines: list[str]) -> None:
        counts: dict[str, int] = {}
        for ln in lines:
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            k, v = k.strip(), v.strip()
            if k == "Device present":
                self.set_state("device_present", v == "true")
            elif k == "Model name":
                self.set_state("model", v)
            elif k == "Video inputs":
                counts["input"] = _safe_int(v) or 0
                self.set_state("video_inputs", counts["input"])
            elif k == "Video outputs":
                counts["output"] = _safe_int(v) or 0
                self.set_state("video_outputs", counts["output"])
            elif k == "Video monitoring outputs":
                counts["monitor"] = _safe_int(v) or 0
                self.set_state("monitoring_outputs", counts["monitor"])
        for ctype, count in counts.items():
            self._reconcile_ports(ctype, count)

    def _reconcile_ports(self, child_type: str, count: int) -> None:
        """Register children 1..count for a port type, deregistering any the
        unit no longer reports (a frame swap or module change)."""
        want = set(range(1, count + 1))
        current = set(self.list_children(child_type))
        for cid in sorted(want - current):
            self.register_child(child_type, cid)
        for cid in current - want:
            self.deregister_child(child_type, cid)

    def _apply_child_block(
        self, lines: list[str], child_type: str, prop: str, transform
    ) -> None:
        """Apply an 'index value' block to one child prop across many children
        in a single atomic transaction. Registers a child on demand if a block
        arrives before the VIDEOHUB DEVICE reconcile (defensive — the device
        sends DEVICE first in its dump)."""
        updates: list[tuple[str, int, dict[str, Any]]] = []
        for ln in lines:
            n, raw = _split_index_value(ln)
            if n is None:
                continue
            value = transform(raw)
            if value is None:
                continue
            cid = n + 1
            if cid > PORT_MAX:
                continue
            if not self.is_child_registered(child_type, cid):
                self.register_child(child_type, cid)
            updates.append((child_type, cid, {prop: value}))
        if updates:
            self.set_children_state_batch(updates)

    # ── Setup action ──

    async def run_setup_action(
        self, action_id: str, params: dict[str, Any], progress: Any
    ) -> dict[str, Any]:
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 9990))
        if not host:
            raise ValueError("No IP address configured")

        await progress(f"Connecting to {host}:{port}…", 20)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach a Videohub on {host}:{port} ({exc}). Check "
                f"the IP address and that the unit is powered on."
            ) from exc

        await progress("Reading device banner…", 60)
        banner = b""
        try:
            deadline = asyncio.get_event_loop().time() + 4.0
            # The server pushes PROTOCOL PREAMBLE + VIDEOHUB DEVICE on connect;
            # read until we have the device block or time out.
            while asyncio.get_event_loop().time() < deadline:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                banner += chunk
                if b"Video outputs:" in banner:
                    break
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass

        text = banner.decode("utf-8", errors="replace")
        if "VIDEOHUB DEVICE" not in text and "PROTOCOL PREAMBLE" not in text:
            raise ConnectionError(
                f"{host}:{port} answered but did not send a Videohub banner — "
                f"is this a Videohub on port 9990?"
            )

        model = _grep(text, r"Model name:\s*(.+)") or "Videohub"
        n_in = _grep(text, r"Video inputs:\s*(\d+)")
        n_out = _grep(text, r"Video outputs:\s*(\d+)")
        n_mon = _grep(text, r"Video monitoring outputs:\s*(\d+)")
        size = ""
        if n_in and n_out:
            size = f" — {n_in}×{n_out}"
            if n_mon and n_mon != "0":
                size += f", {n_mon} monitoring"
        await progress("Connected.", 100)
        return {
            "success": True,
            "message": f"Reached {model}{size} on {host}:{port}.",
        }


def _safe_int(v: str) -> int | None:
    try:
        return int(v.strip())
    except (ValueError, AttributeError):
        return None


def _split_index_value(line: str) -> tuple[int | None, str]:
    """Split a 'N value' line into (index, value). Returns (None, '') on parse failure."""
    parts = line.split(" ", 1)
    if not parts:
        return None, ""
    n = _safe_int(parts[0])
    if n is None:
        return None, ""
    value = parts[1] if len(parts) > 1 else ""
    return n, value


def _as_label(raw: str) -> str:
    return raw


def _as_one_indexed(raw: str) -> int | None:
    n = _safe_int(raw)
    return None if n is None else n + 1


def _as_lock(raw: str) -> str:
    return LOCK_MAP.get(raw.strip(), "unlocked")


def _grep(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else None
