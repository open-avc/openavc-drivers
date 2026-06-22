"""Global Cache iTach IP2SL — RS-232 serial bridge.

The iTach IP2SL is an Ethernet-to-serial bridge: it exposes a single RS-232
port that other devices connect *through*. In OpenAVC it is a "bridge" device —
a downstream serial device binds to the iTach's ``serial:1`` port, and the
platform routes that device's bytes through the iTach's transparent TCP
pass-through (port 4999) while this driver pushes the downstream's line settings
(baud / parity) to the hardware via the Unified TCP command API on port 4998.

Protocol: Global Cache Unified TCP API v1.1.2.
  - Port 4998: line-based command API. Requests and responses are single lines
    terminated by a carriage return (0x0D). Commands used here:
      getversion          -> "<version>"            e.g. 710-1009-05
      getdevices          -> "device,<m>,<p> <TYPE>"... then "endlistdevices"
      get_SERIAL,1:1      -> "SERIAL,1:1,<baud>,<flow>,<parity>"
      set_SERIAL,1:1,...  -> echoes the SERIAL line
  - Port 4999: transparent serial pass-through (raw bytes, not interpreted).
  - Discovery: AMX DDP beacon on 239.255.250.250:9131
    (Make=GlobalCache, Model=iTachIP2SL), 4998 getdevices probe, OUI 00:0C:1E.

The IP2SL serial config (set_SERIAL) persists in the unit's NVRAM, so the baud
push only has to happen when a downstream binds or changes its params.

License: MIT.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# Command API port (line-based, CR-terminated). The transparent serial
# pass-through lives on 4999 and is declared as the bridge port's
# passthrough_port; downstream devices reach it through the TCP transport.
COMMAND_PORT = 4998
PASSTHROUGH_PORT = 4999
CR = b"\r"

# A bare iTach firmware/version string, e.g. "710-1009-05" (IP2SL line).
_VERSION_RE = re.compile(r"^\d{3}-\d{4}-\d{2,}$")
# One AMX DDP beacon field: <-Key=Value>
_BEACON_FIELD_RE = re.compile(r"<-([^=>]+)=([^>]*)>")

# OpenAVC parity letter (project serial config) -> Global Cache parity token.
_PARITY_TO_GC = {"N": "PARITY_NO", "E": "PARITY_EVEN", "O": "PARITY_ODD"}


# ---------------------------------------------------------------------------
# Pure protocol helpers (module-level so the driver test can exercise them
# against byte-exact captures without instantiating the driver).
# ---------------------------------------------------------------------------


def parse_version(line: str | bytes) -> str:
    """Return the firmware version from a ``getversion`` response, or "".

    The iTach answers ``getversion`` with a bare version string (no ``version,``
    prefix), e.g. ``710-1009-05``.
    """
    text = line.decode("ascii", "replace") if isinstance(line, bytes) else line
    text = text.strip().strip("\r\n")
    return text if _VERSION_RE.match(text) else ""


def parse_getdevices(data: str | bytes) -> list[dict[str, Any]]:
    """Parse a ``getdevices`` response into a list of module descriptors.

    Each ``device,<module>,<ports> <TYPE>`` line becomes
    ``{"module": int, "ports": int, "type": str}``. The trailing
    ``endlistdevices`` line is ignored.
    """
    text = data.decode("ascii", "replace") if isinstance(data, bytes) else data
    modules: list[dict[str, Any]] = []
    for raw in text.replace("\n", "\r").split("\r"):
        line = raw.strip()
        if not line.startswith("device,"):
            continue
        # device,<module>,<ports> <TYPE>
        try:
            _kw, module, rest = line.split(",", 2)
            ports_str, _sep, dtype = rest.partition(" ")
            modules.append(
                {"module": int(module), "ports": int(ports_str), "type": dtype.strip()}
            )
        except (ValueError, IndexError):
            log.debug("Unparseable getdevices line: %r", line)
    return modules


def parse_serial_response(line: str | bytes) -> dict[str, Any]:
    """Parse a ``SERIAL,1:1,<baud>,<flow>,<parity>`` response into a dict.

    Returns ``{}`` for a non-SERIAL line. The IP2SL response has no stopbits
    field (8N1 is fixed); newer products may append more fields, which are
    preserved under ``extra``.
    """
    text = line.decode("ascii", "replace") if isinstance(line, bytes) else line
    parts = text.strip().strip("\r\n").split(",")
    if not parts or parts[0] != "SERIAL" or len(parts) < 5:
        return {}
    result: dict[str, Any] = {
        "port": parts[1],
        "baudrate": int(parts[2]) if parts[2].isdigit() else parts[2],
        "flow": parts[3],
        "parity": parts[4],
    }
    if len(parts) > 5:
        result["extra"] = parts[5:]
    return result


def format_set_serial(modaddr: str, baudrate: int, flow: str, parity: str) -> bytes:
    """Build a ``set_SERIAL,<modaddr>,<baud>,<flow>,<parity>`` request (CR-terminated)."""
    return f"set_SERIAL,{modaddr},{baudrate},{flow},{parity}".encode("ascii") + CR


def bridge_port_to_modaddr(port_id: str) -> str:
    """Map a bridge port id (``serial:1``) to a Global Cache ``module:port``.

    On the IP2SL the serial line is module 1; ``serial:<n>`` maps to ``1:<n>``.
    """
    kind, _sep, num = port_id.partition(":")
    num = num or "1"
    if kind != "serial":
        raise ValueError(f"Unsupported bridge port kind for iTach IP2SL: {port_id!r}")
    return f"1:{num}"


def openavc_serial_to_gc(params: dict[str, Any]) -> tuple[int, str, str]:
    """Map OpenAVC serial connection params to Global Cache set_SERIAL args.

    Returns ``(baudrate, flow_token, parity_token)``. OpenAVC stores baud as
    ``baudrate`` and parity as a single letter (N/E/O); the iTach IP2SL fixes
    8 data bits / 1 stop bit, so only baud / flow / parity are sent. Hardware
    flow control is off unless the project sets ``flow_control: "hardware"``
    (or ``rtscts: true``).
    """
    try:
        baudrate = int(params.get("baudrate", 9600))
    except (TypeError, ValueError):
        baudrate = 9600
    parity_letter = str(params.get("parity", "N")).upper()[:1]
    parity = _PARITY_TO_GC.get(parity_letter, "PARITY_NO")
    flow_raw = str(params.get("flow_control", "")).lower()
    hardware = flow_raw in ("hardware", "rtscts") or bool(params.get("rtscts"))
    flow = "FLOW_HARDWARE" if hardware else "FLOW_NONE"
    return baudrate, flow, parity


def parse_amx_beacon(data: str | bytes) -> dict[str, str]:
    """Parse an AMX DDP beacon (``AMXB<-Key=Value>...``) into a field dict.

    e.g. ``{"UUID": "GlobalCache_000C1E075C67", "Make": "GlobalCache",
    "Model": "iTachIP2SL", "Revision": "710-1009-05", ...}``.
    """
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return {k: v for k, v in _BEACON_FIELD_RE.findall(text)}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class GlobalCacheItachIP2SLDriver(BaseDriver):
    """Global Cache iTach IP2SL serial bridge."""

    # Note: literal port numbers below (not the COMMAND_PORT/PASSTHROUGH_PORT
    # constants) — the catalog builder statically extracts DRIVER_INFO and only
    # reads literals. The runtime methods use the named constants.
    DRIVER_INFO = {
        "id": "globalcache_itach_ip2sl",
        "name": "Global Cache iTach IP2SL Serial Bridge",
        "manufacturer": "Global Cache",
        "category": "utility",
        "version": "1.0.0",
        "author": "OpenAVC",
        "transport": "tcp",
        "description": (
            "Ethernet-to-RS-232 bridge. Connect serial devices through the "
            "iTach: bind a serial device to this bridge's port and OpenAVC "
            "routes it over the transparent pass-through while pushing the "
            "right baud and parity to the hardware."
        ),
        "source_url": "https://www.globalcache.com/products/itach/ip2-family/",
        "ports": [4998, 4999],
        "protocols": ["global-cache-unified-tcp"],
        "simulated": False,
        "verified": True,
        "min_platform_version": "0.19.0",
        # Search-friendly: integrators look for what they need ("RS232",
        # "serial", "bridge"), not the box name.
        "tags": [
            "serial", "rs232", "rs-232", "bridge", "gateway", "ethernet-to-serial",
            "global-cache", "globalcache", "itach", "ip2sl",
        ],
        # Bridge declaration: one transparent RS-232 pass-through port. The
        # platform rewrites a bound device's transport to tcp -> this host /
        # passthrough_port and calls prepare_bridge_port() to push line settings.
        "bridge": {
            "ports": [
                {
                    "id": "serial:1",
                    "kind": "serial",
                    "passthrough_port": 4999,
                    "label": "RS-232 Port 1",
                },
            ],
        },
        "default_config": {
            "host": "",
            "port": 4998,
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 4998, "label": "Command Port"},
            "poll_interval": {
                "type": "integer", "default": 15, "label": "Poll Interval (s)",
            },
        },
        "state_variables": {
            "firmware_version": {"type": "string", "label": "Firmware"},
            "serial_port_1": {
                "type": "string",
                "label": "RS-232 Port 1 Settings",
                "help": "Current line settings reported by the iTach (baud, flow, parity).",
            },
        },
        "discovery": {
            "amx_ddp": [
                {"make": "GlobalCache", "model_pattern": "iTachIP2SL"},
            ],
            "tcp_probe": {
                "port": 4998,
                "send_ascii": "getdevices\r",
                "expect": "endlistdevices",
            },
            "oui": ["00:0C:1E"],
            "manufacturer_alias": ["global cache", "globalcache"],
        },
        "help": {
            "overview": (
                "The iTach IP2SL is an Ethernet-to-RS-232 bridge. Add it as a "
                "device, then on any serial device choose 'Through a bridge' in "
                "its Connection settings and pick this iTach's RS-232 port."
            ),
            "setup": (
                "Set the iTach to a static IP or DHCP reservation. No login is "
                "required for the control API. The serial line settings are "
                "pushed automatically from the bound device's connection."
            ),
            "connection": (
                "Command API on TCP 4998, transparent serial pass-through on "
                "TCP 4999. The bridge holds the line settings in NVRAM."
            ),
        },
        "commands": {},  # populated by _build_commands() at import
    }

    # --- Liveness polling (over the auto-created 4998 TCP transport) ---

    async def poll(self) -> None:
        """Refresh identity + serial settings; propagate transport errors.

        Sends getversion + get_SERIAL over the persistent 4998 connection;
        responses arrive via on_data_received and update state. A dead transport
        makes ``send`` raise, which the watchdog counts toward going offline.
        """
        if self.transport is None:
            raise ConnectionError("iTach command transport not connected")
        await self.transport.send(b"getversion" + CR)
        await self.transport.send(b"get_SERIAL,1:1" + CR)

    async def on_data_received(self, data: bytes) -> None:
        """Route a CR-framed response line to state."""
        line = data.decode("ascii", "replace").strip()
        if not line:
            return
        version = parse_version(line)
        if version:
            self.set_state("firmware_version", version)
            return
        if line.startswith("SERIAL,"):
            parsed = parse_serial_response(line)
            if parsed:
                self.set_state(
                    "serial_port_1",
                    f"{parsed['baudrate']},{parsed['flow']},{parsed['parity']}",
                )

    # --- Bridge: push line settings before a downstream connects ---

    async def prepare_bridge_port(
        self, port_id: str, params: dict[str, Any]
    ) -> None:
        """Push the bound device's baud/parity to the iTach via set_SERIAL.

        Uses a dedicated short-lived 4998 connection (the API allows several)
        so the push is confirmed from the echo independently of the polling
        transport's timing. Best-effort by contract: failures are logged, never
        raised, so a bridge hiccup can't strand the downstream device offline.
        """
        try:
            modaddr = bridge_port_to_modaddr(port_id)
        except ValueError as e:
            log.warning("[%s] %s", self.device_id, e)
            return
        host = self.config.get("host", "")
        cmd_port = int(self.config.get("port", COMMAND_PORT) or COMMAND_PORT)
        baudrate, flow, parity = openavc_serial_to_gc(params)
        request = format_set_serial(modaddr, baudrate, flow, parity)

        echo = await self._command(host, cmd_port, request)
        parsed = parse_serial_response(echo)
        if parsed.get("baudrate") == baudrate:
            self.set_state(
                "serial_port_1", f"{baudrate},{flow},{parity}",
            )
            log.info(
                "[%s] Bridge port %s set to %s,%s,%s",
                self.device_id, port_id, baudrate, flow, parity,
            )
        else:
            log.warning(
                "[%s] set_SERIAL for %s not confirmed (sent %r, got %r)",
                self.device_id, port_id, request, echo,
            )

    @staticmethod
    async def _command(
        host: str, port: int, request: bytes, *, timeout: float = 4.0
    ) -> str:
        """Open a short-lived 4998 connection, send one CR-terminated request,
        return the first CR-terminated response line as text.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        try:
            writer.write(request)
            await writer.drain()
            raw = await asyncio.wait_for(reader.readuntil(CR), timeout=timeout)
            return raw.decode("ascii", "replace").strip()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Standalone commands. ``refresh`` re-queries identity + serial config."""
        if command == "refresh":
            await self.poll()
            return {"status": "ok"}
        raise ValueError(f"Unknown command: {command}")


def _build_commands() -> dict[str, dict[str, Any]]:
    return {
        "refresh": {
            "label": "Refresh",
            "params": {},
            "help": "Re-read the iTach firmware version and serial settings.",
        },
    }


GlobalCacheItachIP2SLDriver.DRIVER_INFO["commands"] = _build_commands()
