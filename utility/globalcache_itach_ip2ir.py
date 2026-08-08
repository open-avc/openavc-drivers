"""Global Cache iTach IP2IR — Ethernet to infrared (IR) bridge.

The iTach IP2IR emits infrared through three IR emitter ports. In OpenAVC it's a
**bridge**: IR devices (a TV, cable box, AVR) bind to one of its ports and emit
their code-set through it. The platform speaks a vendor-neutral IR code
(Pronto hex); this driver converts each code to the iTach's ``sendir`` wire
format at emit, and learned codes back to Pronto.

Protocol: Global Cache Unified TCP API (iTach API v1.5).
  - TCP port 4998, line-based, carriage-return terminated (0x0D).
      getversion                          -> "<version>"   e.g. 710-1005-05
      getdevices                          -> "device,<m>,<p> <TYPE>"... + endlistdevices
                                             (the IP2IR reports "device,1,3 IR")
      get_IR,1:<n>                         -> "IR,1:<n>,<mode>"
      set_IR,1:<n>,<mode>                  -> "IR,1:<n>,<mode>"  (mode echo)
      sendir,<conn>,<id>,<freq>,<repeat>,<offset>,<on1>,<off1>,...
                                          -> "completeir,<conn>,<id>"  on success
      get_IRL                             -> "IR Learner Enabled", then each learned
                                             button as an (uncompressed) sendir line
      stop_IRL                            -> "IR Learner Disabled"
  - Discovery: AMX DDP beacon on 239.255.250.250:9131
    (Make=GlobalCache, Model=iTachIP2IR), a 4998 getdevices probe (reports "IR"),
    OUI 00:0C:1E.

Hardware-verified against a live IP2IR (firmware 710-1005-05):
  - ``sendir`` is answered by ``completeir,<conn>,<id>`` (no command echo, unlike
    the IP2CC). Bad pulse data -> ``ERR_<conn>,010``; bad connector -> ``ERR_0:0,002``.
  - The learner replies on connector ``2:1`` (an internal learner address), NOT
    the emit port. Because a learned code is stored as Pronto (which drops the
    connector/id/repeat), the retarget is free: emit always sets the connector
    from the bound port.
  - The learner is disabled by any command on the unit, so learning runs on a
    dedicated socket and this driver pauses its own poll for the session.

License: MIT.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.transport.ir_codec import IRCode, build_pronto, parse_pronto
from openavc.utils.logger import get_logger

log = get_logger(__name__)

# Unified TCP command port (line-based, CR-terminated).
COMMAND_PORT = 4998
CR = b"\r"

# The IP2IR's IR module is address 1; emitters are connectors 1..3 (1:1..1:3).
IR_MODULE = 1
IR_CONNECTOR_COUNT = 3

# sendir IDs cycle 1..65535 (0 is avoided so a completeir id is always truthy).
_MAX_IR_ID = 65535

# How long an emit waits for its completeir before giving up.
_EMIT_TIMEOUT = 5.0

_VERSION_RE = re.compile(r"^\d{3}-\d{4}-\d{2,}$")
_BEACON_FIELD_RE = re.compile(r"<-([^=>]+)=([^>]*)>")
_COMPLETEIR_RE = re.compile(r"^completeir,(\d+:\d+),(\d+)$")
_ERR_RE = re.compile(r"^ERR_(\d+:\d+),(\d+)$")
_IR_MODE_RE = re.compile(r"^IR,(\d+:\d+),(\w+)$")


# ---------------------------------------------------------------------------
# Pure protocol helpers (module-level so the driver test can exercise them
# against byte-exact captures without instantiating the driver, and without an
# openavc install for the wire layer). The Pronto <-> structure step lives in
# the platform's ir_codec; the sendir wire layer below is self-contained.
# ---------------------------------------------------------------------------


def parse_version(line: str | bytes) -> str:
    """Return the firmware version from a ``getversion`` response, or ""."""
    text = line.decode("ascii", "replace") if isinstance(line, bytes) else line
    text = text.strip().strip("\r\n")
    return text if _VERSION_RE.match(text) else ""


def parse_getdevices(data: str | bytes) -> list[dict[str, Any]]:
    """Parse a ``getdevices`` response into module descriptors.

    Each ``device,<module>,<ports> <TYPE>`` line becomes
    ``{"module", "ports", "type"}``. For an IP2IR this yields the ETHERNET
    module and ``{"module": 1, "ports": 3, "type": "IR"}``.
    """
    text = data.decode("ascii", "replace") if isinstance(data, bytes) else data
    modules: list[dict[str, Any]] = []
    for raw in text.replace("\n", "\r").split("\r"):
        line = raw.strip()
        if not line.startswith("device,"):
            continue
        try:
            _kw, module, rest = line.split(",", 2)
            ports_str, _sep, dtype = rest.partition(" ")
            modules.append(
                {"module": int(module), "ports": int(ports_str), "type": dtype.strip()}
            )
        except (ValueError, IndexError):
            log.debug("Unparseable getdevices line: %r", line)
    return modules


def parse_amx_beacon(data: str | bytes) -> dict[str, str]:
    """Parse an AMX DDP beacon (``AMXB<-Key=Value>...``) into a field dict."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return {k: v for k, v in _BEACON_FIELD_RE.findall(text)}


def parse_ir_mode(line: str | bytes) -> dict[str, str]:
    """Parse an ``IR,1:1,IR`` connector-mode line into ``{connector, mode}``,
    or ``{}`` for any other line."""
    text = line.decode("ascii", "replace") if isinstance(line, bytes) else line
    m = _IR_MODE_RE.match(text.strip().strip("\r\n"))
    if not m:
        return {}
    return {"connector": m.group(1), "mode": m.group(2)}


def parse_completeir(line: str | bytes) -> tuple[str, int] | None:
    """Return ``(connector, id)`` from a ``completeir`` line, else None."""
    text = line.decode("ascii", "replace") if isinstance(line, bytes) else line
    m = _COMPLETEIR_RE.match(text.strip().strip("\r\n"))
    return (m.group(1), int(m.group(2))) if m else None


def parse_err(line: str | bytes) -> tuple[str, str] | None:
    """Return ``(connector, code)`` from an ``ERR_<conn>,<code>`` line, else None."""
    text = line.decode("ascii", "replace") if isinstance(line, bytes) else line
    m = _ERR_RE.match(text.strip().strip("\r\n"))
    return (m.group(1), m.group(2)) if m else None


def port_to_connector(port_id: str, module: int = IR_MODULE) -> str:
    """Map a bridge port id (``ir:2``) to an iTach connector (``1:2``)."""
    conn = port_id.rsplit(":", 1)[-1]
    return f"{module}:{conn}"


def decompress_sendir_data(tokens: list[str]) -> list[int]:
    """Expand the on/off values of a ``sendir`` data section to a flat int list.

    Handles both the plain (all-numeric) form the learner returns and the
    Global Cache compressed form: the first up-to-15 unique ``on,off`` pairs are
    assigned the letters A-O as they occur, and a recurring pair is written as
    its letter (letters may sit inline in a comma-delimited token, e.g.
    ``4,5A8,9ABB``). We always emit uncompressed, so this is only used on import.
    """
    stream = ",".join(tokens)
    values: list[int] = []
    pairs: list[tuple[int, int]] = []
    pending: int | None = None
    num = ""

    def emit(n: int) -> None:
        nonlocal pending
        if pending is None:
            pending = n
        else:
            pair = (pending, n)
            values.append(pending)
            values.append(n)
            pending = None
            if len(pairs) < 15 and pair not in pairs:
                pairs.append(pair)

    for ch in stream:
        if ch.isdigit():
            num += ch
            continue
        if num:
            emit(int(num))
            num = ""
        if ch == ",":
            continue
        if "A" <= ch <= "O":
            if pending is not None:
                raise ValueError("compressed sendir: letter reference mid-pair")
            idx = ord(ch) - ord("A")
            if idx >= len(pairs):
                raise ValueError(f"compressed sendir references unknown pair {ch!r}")
            on, off = pairs[idx]
            values.append(on)
            values.append(off)
            continue
        if ch in " \r\n":
            continue
        raise ValueError(f"unexpected character {ch!r} in sendir data")
    if num:
        emit(int(num))
    if pending is not None:
        raise ValueError("sendir data has an odd number of values")
    return values


def build_sendir(
    connector: str, ir_id: int, freq: int, repeat: int, offset: int,
    bursts: list[int],
) -> bytes:
    """Build an (uncompressed) ``sendir`` request (CR-terminated).

    ``connector`` is the iTach form (``1:1``); ``offset`` is 1-based and odd
    (the position where the repeat body begins).
    """
    parts = ["sendir", connector, str(ir_id), str(freq), str(repeat), str(offset)]
    parts.extend(str(b) for b in bursts)
    return ",".join(parts).encode("ascii") + CR


def parse_sendir(text: str | bytes) -> dict[str, Any]:
    """Parse a ``sendir`` line into its fields + decompressed bursts.

    Tolerates a stray leading newline (the learner sometimes prefixes one) and
    the trailing CR. Returns ``{connector, id, freq, repeat, offset, bursts}``.
    """
    s = text.decode("ascii", "replace") if isinstance(text, bytes) else text
    s = s.strip().strip("\r\n").strip()
    parts = s.split(",")
    if len(parts) < 7 or parts[0] != "sendir":
        raise ValueError(f"not a sendir line: {s[:40]!r}")
    return {
        "connector": parts[1],
        "id": int(parts[2]),
        "freq": int(parts[3]),
        "repeat": int(parts[4]),
        "offset": int(parts[5]),
        "bursts": decompress_sendir_data(parts[6:]),
    }


def sendir_to_pronto(text: str | bytes) -> str:
    """Convert a captured ``sendir`` line to vendor-neutral Pronto hex.

    Drops the iTach-specific connector / id / repeat (Pronto has no home for
    them); the emit path re-supplies the connector from the bound port and the
    repeat from the stored per-command value.
    """
    d = parse_sendir(text)
    code = IRCode(
        frequency=d["freq"],
        bursts=tuple(d["bursts"]),
        repeat_offset=d["offset"] - 1,
    )
    return build_pronto(code)


def pronto_to_sendir(pronto: str, connector: str, ir_id: int, repeat: int) -> bytes:
    """Convert a stored Pronto code to a ``sendir`` request for ``connector``."""
    code = parse_pronto(pronto)
    offset = code.repeat_offset + 1  # 0-based even -> 1-based odd
    return build_sendir(connector, ir_id, code.frequency, repeat, offset, list(code.bursts))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class GlobalCacheItachIP2IRDriver(BaseDriver):
    """Global Cache iTach IP2IR — three-port network IR bridge."""

    # Note: literal port number below (not the COMMAND_PORT constant) — the
    # catalog builder statically extracts DRIVER_INFO and only reads literals.
    DRIVER_INFO = {
        "id": "globalcache_itach_ip2ir",
        "name": "Global Cache iTach IP2IR (IR Bridge)",
        "manufacturer": "Global Cache",
        "category": "utility",
        "version": "1.0.2",
        "author": "OpenAVC",
        "transport": "tcp",
        "description": (
            "Network IR bridge with three emitter ports. Bind IR devices (TVs, "
            "cable boxes, AVRs) to a port and control them with a learned or "
            "database code-set. Learn codes straight from the original remote."
        ),
        "source_url": "https://www.globalcache.com/products/itach/ip2-family/",
        "ports": [4998],
        "protocols": ["global-cache-unified-tcp"],
        "simulated": True,
        "verified": True,
        "min_platform_version": "0.25.0",
        # Search-friendly: integrators look for what they need.
        "tags": [
            "ir", "infrared", "ir-bridge", "ir-blaster", "blaster", "remote",
            "global-cache", "globalcache", "itach", "ip2ir",
        ],
        "default_config": {
            "host": "",
            "port": 4998,
            "poll_interval": 20,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 4998, "label": "Command Port"},
            "poll_interval": {
                "type": "integer", "default": 20, "label": "Poll Interval (s)",
            },
        },
        "state_variables": {
            "firmware_version": {"type": "string", "label": "Firmware"},
        },
        # No device-level commands: an IR device bound to a port carries the
        # code-set commands. The bridge only emits/learns on behalf of those.
        "commands": {},
        # Three IR emitter ports. IR ports route commands at send time
        # (bridge_emit) — they are not transparent byte pipes, so they have no
        # passthrough_port.
        "bridge": {
            "ports": [
                {"id": "ir:1", "kind": "ir", "label": "IR Out 1"},
                {"id": "ir:2", "kind": "ir", "label": "IR Out 2"},
                {"id": "ir:3", "kind": "ir", "label": "IR Out 3"},
            ],
        },
        "discovery": {
            "amx_ddp": [
                {"make": "GlobalCache", "model_pattern": "iTachIP2IR"},
            ],
            # getdevices identifies an IR unit positively: an IP2SL answers the
            # same probe with SERIAL and an IP2CC with RELAY, so match on IR.
            "tcp_probe": {
                "port": 4998,
                "send_ascii": "getdevices\r",
                "expect": "IR",
            },
            "oui": ["00:0C:1E"],
            "manufacturer_alias": ["global cache", "globalcache"],
        },
        "help": {
            "overview": (
                "The iTach IP2IR is a network IR bridge with three emitter "
                "ports. Add it, then add an IR Device (or a community IR "
                "driver) and bind it to one of the bridge's IR ports. Build the "
                "device's code-set by learning from its remote, pasting Pronto "
                "hex, or searching a code database."
            ),
            "setup": (
                "Set the iTach to a static IP or DHCP reservation. No login is "
                "required for the control API. Plug an IR emitter into a port "
                "and stick it over the target device's IR receiver window."
            ),
            "connection": (
                "Command API on TCP 4998. Learning uses a second connection and "
                "pauses polling for the session (the learner is disabled by any "
                "other command on the unit)."
            ),
        },
    }

    def __init__(
        self, device_id: str, config: dict[str, Any], state: Any, events: Any,
    ) -> None:
        super().__init__(device_id, config, state, events)
        self._ir_id = 0
        # In-flight emits: sendir id -> (connector, future resolved by the
        # matching completeir, or failed by an ERR_ on that connector).
        self._pending: dict[int, tuple[str, asyncio.Future]] = {}
        # Learn session (dedicated socket + a queue the reader task feeds).
        self._learning = False
        self._learn_reader: asyncio.StreamReader | None = None
        self._learn_writer: asyncio.StreamWriter | None = None
        self._learn_task: asyncio.Task | None = None
        self._learn_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    # --- Commands ---

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """The IP2IR bridge has no device-level commands of its own — downstream
        IR devices carry the code-set and emit through it via ``bridge_emit``.
        This satisfies the BaseDriver abstract method; ``refresh`` re-reads the
        firmware for a manual liveness check.
        """
        if command == "refresh":
            await self.poll()
            return {"status": "ok"}
        raise ValueError(f"Unknown command: {command}")

    # --- Liveness poll (over the auto 4998 TCP transport) ---

    async def poll(self) -> None:
        """Query firmware for liveness; propagate transport errors to the watchdog."""
        if self.transport is None:
            raise ConnectionError("iTach IR command transport not connected")
        await self.transport.send(b"getversion" + CR)

    async def on_data_received(self, data: bytes) -> None:
        """Route a response line: version, completeir (resolve an emit), ERR_
        (fail an emit), or a connector-mode echo.

        The TCP transport frames on CR and delivers each line delimiter-stripped;
        the CR split is defensive for any unframed bytes.
        """
        text = data.decode("ascii", "replace").replace("\n", "\r")
        for chunk in text.split("\r"):
            line = chunk.strip()
            if not line:
                continue

            version = parse_version(line)
            if version:
                self.set_state("firmware_version", version)
                continue

            done = parse_completeir(line)
            if done is not None:
                _connector, ir_id = done
                entry = self._pending.pop(ir_id, None)
                if entry is not None and not entry[1].done():
                    entry[1].set_result(line)
                continue

            err = parse_err(line)
            if err is not None:
                self._fail_emit(err[0], f"IR emit rejected: {line}")
                continue

            # IR,1:n,<mode> mode echoes are informational; ignore.

    def _fail_emit(self, connector: str, message: str) -> None:
        """Fail the oldest in-flight emit on ``connector`` (an ERR_ carries the
        connector, not the sendir id, so match by connector / arrival order)."""
        for ir_id, (conn, fut) in list(self._pending.items()):
            if conn == connector and not fut.done():
                fut.set_exception(ConnectionError(message))
                self._pending.pop(ir_id, None)
                return
        log.warning("[%s] %s (no matching in-flight emit)", self.device_id, message)

    def _next_id(self) -> int:
        self._ir_id = (self._ir_id % _MAX_IR_ID) + 1
        return self._ir_id

    # --- Bridge emit (vendor-neutral capability) ---

    async def bridge_emit(
        self, port_id: str, kind: str, payload: dict[str, Any]
    ) -> Any:
        """Emit an IR code out ``port_id`` and wait for the iTach's completeir.

        ``payload`` is the platform's neutral IR payload ``{pronto, repeat}``.
        Converts it to a ``sendir`` for the connector the port maps to, sends it
        on the persistent command socket, and resolves when the matching
        completeir arrives (or raises on an ERR_ / timeout).
        """
        if kind != "ir":
            raise ValueError(f"IP2IR bridge cannot emit kind {kind!r}")
        if self._learning:
            raise ConnectionError("Cannot emit while an IR learn session is active")
        if self.transport is None or not getattr(self.transport, "connected", False):
            raise ConnectionError("iTach IR bridge not connected")

        pronto = payload.get("pronto", "")
        if not pronto:
            raise ValueError("IR payload has no 'pronto' code")
        try:
            repeat = int(payload.get("repeat", 1) or 1)
        except (TypeError, ValueError):
            repeat = 1
        connector = port_to_connector(port_id)
        ir_id = self._next_id()
        request = pronto_to_sendir(pronto, connector, ir_id, max(1, repeat))

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[ir_id] = (connector, fut)
        try:
            await self.transport.send(request)
            line = await asyncio.wait_for(fut, timeout=_EMIT_TIMEOUT)
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"IR bridge did not confirm the emit on {connector} (no completeir)"
            ) from None
        finally:
            self._pending.pop(ir_id, None)
        return {"status": "ok", "connector": connector, "response": line}

    async def prepare_bridge_port(
        self, port_id: str, params: dict[str, Any]
    ) -> None:
        """Put the emitter port in IR mode before a downstream IR device uses it.

        Best-effort: a connector left in SENSOR / LED mode can't emit, so push
        IR mode when a device binds. A failure here never blocks the downstream
        (the base contract), and the mode echo is swallowed by on_data_received.
        """
        if self.transport is None or not getattr(self.transport, "connected", False):
            return
        connector = port_to_connector(port_id)
        with suppress(Exception):
            await self.transport.send(f"set_IR,{connector},IR".encode("ascii") + CR)

    # --- Learn capability (vendor-neutral) ---

    async def bridge_import_code(self, wire: str) -> str:
        """Convert a typed ``sendir`` string to Pronto for storage.

        Raises ValueError (surfaced as a 400) if it isn't a valid sendir line.
        """
        return sendir_to_pronto(wire)

    @property
    def can_learn(self) -> bool:
        return True

    async def bridge_learn_start(self) -> None:
        """Open a dedicated socket, enable the learner, and pause the main poll.

        A second connection is used because the learner streams captures only to
        the connection that started it, and any command on the unit disables the
        learner — so the periodic poll is paused for the session too.
        """
        if self._learning:
            return
        host = self.config.get("host", "")
        port = int(self.config.get("port", COMMAND_PORT) or COMMAND_PORT)
        # Pause the liveness poll so it doesn't disable the learner mid-session.
        await self.stop_polling()
        self._learn_reader, self._learn_writer = await asyncio.open_connection(
            host, port
        )
        self._learning = True
        self._learn_queue = asyncio.Queue()
        self._learn_writer.write(b"get_IRL" + CR)
        await self._learn_writer.drain()
        self._learn_task = asyncio.create_task(self._learn_loop())

    async def _learn_loop(self) -> None:
        """Read the dedicated socket, converting each captured sendir to Pronto
        and pushing events onto the learn queue."""
        reader = self._learn_reader
        if reader is None:
            return
        try:
            while True:
                chunk = await reader.readuntil(CR)
                line = chunk.decode("ascii", "replace").strip()
                if not line:
                    continue
                if line == "IR Learner Enabled" or line == "IR Learner Disabled":
                    continue
                if line == "IR Learner Unavailable":
                    await self._learn_queue.put((
                        "error",
                        "IR learner unavailable (a connector is in LED mode)",
                    ))
                    continue
                if line.startswith("sendir"):
                    try:
                        pronto = sendir_to_pronto(line)
                    except Exception as exc:
                        log.warning(
                            "[%s] could not convert learned code: %s",
                            self.device_id, exc,
                        )
                        continue
                    await self._learn_queue.put(("captured", pronto))
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            await self._learn_queue.put(("closed", None))

    async def bridge_learn_poll(self, timeout: float) -> str | None:
        """Return the next captured code as Pronto, or None on timeout."""
        try:
            kind, value = await asyncio.wait_for(
                self._learn_queue.get(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None
        if kind == "captured":
            return value
        if kind == "error":
            raise ConnectionError(value)
        if kind == "closed":
            raise ConnectionError("IR learn connection closed")
        return None

    async def bridge_learn_stop(self) -> None:
        """Disable the learner, close the dedicated socket, resume the poll.

        Safe to call twice.
        """
        if not self._learning:
            return
        self._learning = False
        if self._learn_task is not None:
            self._learn_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._learn_task
            self._learn_task = None
        writer = self._learn_writer
        self._learn_reader = None
        self._learn_writer = None
        if writer is not None:
            with suppress(Exception):
                writer.write(b"stop_IRL" + CR)
                await writer.drain()
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()
        # Resume the liveness poll.
        interval = self.config.get("poll_interval", 0)
        if interval and interval > 0:
            await self.start_polling(interval)

    async def disconnect(self) -> None:
        """Tear down any learn session + fail in-flight emits, then disconnect."""
        await self.bridge_learn_stop()
        for ir_id, (_conn, fut) in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(ConnectionError("IR bridge disconnected"))
            self._pending.pop(ir_id, None)
        await super().disconnect()
