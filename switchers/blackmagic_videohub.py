"""
OpenAVC Blackmagic Videohub Driver.

Controls Blackmagic Design Videohub routers over the Videohub Ethernet
Protocol — a text-based, block-oriented protocol on TCP port 9990.

Models covered (any unit running Videohub Server v4.9.x or later):
    Blackmagic Videohub 12G              10x10, 20x20, 40x40
    Blackmagic Videohub 80x80 12G        80x80
    Smart Videohub CleanSwitch 12x12     12x12
    Smart Videohub                       16x16, 20x20
    Universal Videohub                   modular up to 72x144
    Videohub Master Control Pro          control surface
    Videohub Smart Control Pro           control surface

Protocol: text, lines terminated by LF. Server pushes blocks of the form:

    HEADER:
    <line>
    <line>
    <blank line terminates block>

Clients send the same shape to request changes. Server replies ACK / NAK
to each client request. Source: Videohub Ethernet Protocol v2.3, Nov 2023.
"""

from __future__ import annotations

from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

# Number of ports we predeclare state variables for. Devices wider than this
# still route correctly; their additional ports surface as undeclared keys.
MAX_DECLARED_PORTS = 16
MAX_DECLARED_MONITOR_PORTS = 4

LOCK_MAP = {"U": "unlocked", "O": "owned", "L": "locked"}


def _build_state_vars() -> dict[str, dict[str, Any]]:
    """Generate the state_variables dict for declared port range."""
    out: dict[str, dict[str, Any]] = {
        "model": {"type": "string", "label": "Model"},
        "device_present": {"type": "boolean", "label": "Device Present"},
        "video_inputs": {"type": "integer", "label": "Video Inputs"},
        "video_outputs": {"type": "integer", "label": "Video Outputs"},
        "monitoring_outputs": {"type": "integer", "label": "Monitoring Outputs"},
        "protocol_version": {"type": "string", "label": "Protocol Version"},
    }
    for n in range(1, MAX_DECLARED_PORTS + 1):
        out[f"in_{n}_label"] = {"type": "string", "label": f"Input {n} Label"}
        out[f"out_{n}_label"] = {"type": "string", "label": f"Output {n} Label"}
        out[f"out_{n}_input"] = {"type": "integer", "label": f"Output {n} Source"}
        out[f"out_{n}_lock"] = {
            "type": "enum",
            "values": ["unlocked", "owned", "locked"],
            "label": f"Output {n} Lock",
        }
    for n in range(1, MAX_DECLARED_MONITOR_PORTS + 1):
        out[f"mon_{n}_input"] = {
            "type": "integer",
            "label": f"Monitor Output {n} Source",
        }
    return out


class BlackmagicVideohubDriver(BaseDriver):
    """Blackmagic Videohub Ethernet Protocol driver."""

    DRIVER_INFO = {
        "id": "blackmagic_videohub",
        "name": "Blackmagic Videohub",
        "manufacturer": "Blackmagic Design",
        "category": "switcher",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Blackmagic Design Videohub routers over the Videohub "
            "Ethernet Protocol (TCP 9990). Supports video output routing, "
            "monitoring output routing, input/output label editing, and "
            "output locking across the entire Videohub product line."
        ),
        "source_url": "https://documents.blackmagicdesign.com/DeveloperManuals/VideohubEthernetProtocol.pdf",
        "tags": ["matrix-switcher", "sdi", "12g", "videohub", "broadcast"],
        "verified": False,
        "simulated": True,
        "protocols": ["videohub"],
        "ports": [9990],
        "transport": "tcp",
        "discovery": {
            "ports": [9990],
            "mdns_service": "_blackmagic._tcp.local.",
        },
        "compatible_models": [
            {
                "manufacturer": "Blackmagic Design",
                "models": [
                    "Blackmagic Videohub 12G 10x10",
                    "Blackmagic Videohub 12G 20x20",
                    "Blackmagic Videohub 12G 40x40",
                    "Blackmagic Videohub 80x80 12G",
                    "Smart Videohub 12G CleanSwitch 12x12",
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
                    "Protocol. Pre-declared state variables cover the first "
                    "16 video ports + 4 monitoring outputs; routing on wider "
                    "frames still works, additional ports surface as raw keys."
                ),
            }
        ],
        "help": {
            "overview": (
                "Blackmagic Videohub is a family of SDI matrix routers used "
                "in broadcast, post-production, and live event facilities. "
                "This driver speaks the Videohub Ethernet Protocol, used by "
                "every Videohub server-class device since 2010. It exposes "
                "video routing, monitoring routing, lock state, and label "
                "editing as macro-callable commands."
            ),
            "setup": (
                "1. Connect the Videohub to your network via Ethernet.\n"
                "2. Find the unit's IP address from the front panel "
                "(Smart Videohub) or from Blackmagic Videohub Setup.\n"
                "3. The Videohub Ethernet Protocol on TCP 9990 is always "
                "enabled — no auth, no enable step.\n"
                "4. Enter the IP address in the device config; leave port "
                "at 9990.\n"
                "5. Outputs and inputs are 1-indexed in commands.\n"
                "6. Routing 'output X to input Y' sends VIDEO OUTPUT "
                "ROUTING with the protocol's 0-indexed values."
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
                    "needed; raise above 0 only if you want a periodic sanity "
                    "check via PING."
                ),
            },
        },
        "state_variables": _build_state_vars(),
        "commands": {
            "route": {
                "label": "Route Output",
                "params": {
                    "output": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "help": "Destination output (1-indexed).",
                    },
                    "input": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "help": "Source input (1-indexed).",
                    },
                },
                "help": "Route a video input to a video output.",
            },
            "route_monitoring": {
                "label": "Route Monitor Output",
                "params": {
                    "output": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "help": "Monitor output (1-indexed).",
                    },
                    "input": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "help": "Source input (1-indexed).",
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
                    "input": {"type": "integer", "required": True, "min": 1},
                    "label": {"type": "string", "required": True},
                },
                "help": "Set the friendly label shown on the front panel for an input.",
            },
            "set_output_label": {
                "label": "Rename Output",
                "params": {
                    "output": {"type": "integer", "required": True, "min": 1},
                    "label": {"type": "string", "required": True},
                },
                "help": "Set the friendly label shown on the front panel for an output.",
            },
            "lock_output": {
                "label": "Lock Output",
                "params": {
                    "output": {"type": "integer", "required": True, "min": 1},
                },
                "help": (
                    "Acquire a lock on an output so other clients cannot "
                    "change its routing or label."
                ),
            },
            "unlock_output": {
                "label": "Unlock Output",
                "params": {
                    "output": {"type": "integer", "required": True, "min": 1},
                },
                "help": "Release a lock previously acquired by this client.",
            },
            "force_unlock_output": {
                "label": "Force Unlock Output",
                "params": {
                    "output": {"type": "integer", "required": True, "min": 1},
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
        super().__init__(device_id, config, state, events)

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = self.config.get("port", 9990)

        # Raw transport (no delimiter framing) — we need to keep blank lines,
        # which the delimiter parser would silently drop.
        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=None,
            timeout=5.0,
            name=self.device_id,
        )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(
            f"[{self.device_id}] Connected to Videohub at {host}:{port}"
        )

        poll_interval = self.config.get("poll_interval", 0)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self.set_state("connected", False)
        self._line_buffer = b""
        self._current_block = None
        self._block_lines = []
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

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

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

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
                "VIDEO OUTPUT ROUTING:",
                "VIDEO OUTPUT LOCKS:",
                "VIDEO MONITORING OUTPUT ROUTING:",
            ):
                await self._send_query(header)
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")

    # ── Parsing ──

    async def on_data_received(self, data: bytes) -> None:
        """Accumulate, split into lines (preserving blank lines), dispatch."""
        self._line_buffer += data
        # Use splitlines(keepends=False) but only on completed lines.
        # We need to keep the trailing partial line in the buffer.
        # Walk byte-by-byte: each \n closes a line.
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
                if line == "NAK":
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
                    self.set_state("video_inputs", _safe_int(v))
                elif k == "Video outputs":
                    self.set_state("video_outputs", _safe_int(v))
                elif k == "Video monitoring outputs":
                    self.set_state("monitoring_outputs", _safe_int(v))

        elif block == "INPUT LABELS":
            self._apply_label_block(lines, "in")

        elif block == "OUTPUT LABELS":
            self._apply_label_block(lines, "out")

        elif block == "VIDEO OUTPUT ROUTING":
            self._apply_routing_block(lines, "out")

        elif block == "VIDEO MONITORING OUTPUT ROUTING":
            self._apply_routing_block(lines, "mon")

        elif block == "VIDEO OUTPUT LOCKS":
            self._apply_lock_block(lines, "out")

        # Other blocks (SERIAL PORT *, MONITORING OUTPUT LABELS, etc.) are
        # ignored — their state is not exposed in this driver's surface.

    def _apply_label_block(self, lines: list[str], prefix: str) -> None:
        for ln in lines:
            n, label = _split_index_value(ln)
            if n is None:
                continue
            self.set_state(f"{prefix}_{n + 1}_label", label)

    def _apply_routing_block(self, lines: list[str], prefix: str) -> None:
        for ln in lines:
            n, val = _split_index_value(ln)
            if n is None:
                continue
            src = _safe_int(val)
            if src is None:
                continue
            self.set_state(f"{prefix}_{n + 1}_input", src + 1)

    def _apply_lock_block(self, lines: list[str], prefix: str) -> None:
        for ln in lines:
            n, val = _split_index_value(ln)
            if n is None:
                continue
            state_value = LOCK_MAP.get(val.strip(), "unlocked")
            self.set_state(f"{prefix}_{n + 1}_lock", state_value)


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
