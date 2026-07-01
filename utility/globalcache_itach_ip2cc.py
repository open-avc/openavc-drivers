"""Global Cache iTach IP2CC — Ethernet to contact-closure (relay) controller.

The iTach IP2CC exposes three dry-contact relay outputs over the network. AV
integrators wire them to anything that takes a contact-closure trigger:
motorized screens and lifts, projector triggers, shades, door strikes, or the
"trigger in" of other gear. Each relay can be latched (held closed/open) or
pulsed (closed briefly, then released) from macros, triggers, and panel buttons.

Protocol: Global Cache Unified TCP API (iTach API v1.5).
  - TCP port 4998, line-based. Requests and responses are single lines
    terminated by a carriage return (0x0D). Commands used here:
      getversion            -> "<version>"          e.g. 710-1008-05
      getdevices            -> "device,<m>,<p> <TYPE>"... then "endlistdevices"
                               (the IP2CC reports "device,1,3 RELAY")
      getstate,1:<n>        -> "state,1:<n>,<0|1>"   (1 = closed, 0 = open)
      setstate,1:<n>,<0|1>  -> command echo (see below)
  - Discovery: AMX DDP beacon on 239.255.250.250:9131
    (Make=GlobalCache, Model=iTachIP2CC), 4998 getdevices probe, OUI 00:0C:1E.

Hardware-verified quirks (differ from the published spec, confirmed against a
live IP2CC firmware 710-1008-05):
  - ``setstate`` does NOT reply ``state,1:n,v`` as the spec claims. It echoes
    the command verbatim, e.g. ``setstate,1:1,1``. This driver treats either
    form as a state report, and reconciles from a follow-up ``getstate`` poll.
  - An unknown command returns ``ERR_<module:connector>,<code>`` (e.g.
    ``ERR_0:0,001``), not ``unknowncommand,<code>``.
  - Relay state is NOT retained across a power cycle: every relay returns to
    open (0) on power up, so the driver re-reads all relay states on connect.

License: MIT.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# Unified TCP command port (line-based, CR-terminated).
COMMAND_PORT = 4998
CR = b"\r"

# The IP2CC's relay module is address 1; relays are connectors 1..3 (1:1..1:3).
RELAY_MODULE = 1
RELAY_COUNT = 3

# A bare iTach firmware/version string, e.g. "710-1008-05".
_VERSION_RE = re.compile(r"^\d{3}-\d{4}-\d{2,}$")
# A relay state report: "state,1:2,0" (getstate) or "setstate,1:2,1" (the
# echo the IP2CC returns to a set). Captures module, connector, and value.
_STATE_RE = re.compile(r"^(?:set)?state,(\d+):(\d+),([01])$")
# One AMX DDP beacon field: <-Key=Value>
_BEACON_FIELD_RE = re.compile(r"<-([^=>]+)=([^>]*)>")


# ---------------------------------------------------------------------------
# Pure protocol helpers (module-level so the driver test can exercise them
# against byte-exact captures without instantiating the driver).
# ---------------------------------------------------------------------------


def parse_version(line: str | bytes) -> str:
    """Return the firmware version from a ``getversion`` response, or "".

    The iTach answers ``getversion`` with a bare version string (no prefix),
    e.g. ``710-1008-05``.
    """
    text = line.decode("ascii", "replace") if isinstance(line, bytes) else line
    text = text.strip().strip("\r\n")
    return text if _VERSION_RE.match(text) else ""


def parse_getdevices(data: str | bytes) -> list[dict[str, Any]]:
    """Parse a ``getdevices`` response into a list of module descriptors.

    Each ``device,<module>,<ports> <TYPE>`` line becomes
    ``{"module": int, "ports": int, "type": str}``. The trailing
    ``endlistdevices`` line is ignored. For an IP2CC this yields the ETHERNET
    module and a ``{"module": 1, "ports": 3, "type": "RELAY"}`` entry.
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


def parse_state_line(line: str | bytes) -> dict[str, Any]:
    """Parse a relay state line into ``{module, connector, relay, closed}``.

    Accepts both the ``getstate`` reply (``state,1:2,0``) and the ``setstate``
    command echo (``setstate,1:2,1``) the IP2CC actually returns. ``relay`` is
    the connector number (1-based); ``closed`` is True when contacts are closed
    (value 1). Returns ``{}`` for any non-state line.
    """
    text = line.decode("ascii", "replace") if isinstance(line, bytes) else line
    m = _STATE_RE.match(text.strip().strip("\r\n"))
    if not m:
        return {}
    module, connector, value = int(m.group(1)), int(m.group(2)), m.group(3)
    return {
        "module": module,
        "connector": connector,
        "relay": connector,
        "closed": value == "1",
    }


def format_setstate(relay: int, closed: bool) -> bytes:
    """Build a ``setstate,1:<relay>,<0|1>`` request (CR-terminated)."""
    value = 1 if closed else 0
    return f"setstate,{RELAY_MODULE}:{relay},{value}".encode("ascii") + CR


def format_getstate(relay: int) -> bytes:
    """Build a ``getstate,1:<relay>`` request (CR-terminated)."""
    return f"getstate,{RELAY_MODULE}:{relay}".encode("ascii") + CR


def parse_amx_beacon(data: str | bytes) -> dict[str, str]:
    """Parse an AMX DDP beacon (``AMXB<-Key=Value>...``) into a field dict.

    e.g. ``{"UUID": "GlobalCache_000C1E075EF1", "Make": "GlobalCache",
    "Model": "iTachIP2CC", "Revision": "710-1008-05", ...}``.
    """
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return {k: v for k, v in _BEACON_FIELD_RE.findall(text)}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class GlobalCacheItachIP2CCDriver(BaseDriver):
    """Global Cache iTach IP2CC contact-closure (relay) controller."""

    # Note: literal port number below (not the COMMAND_PORT constant) — the
    # catalog builder statically extracts DRIVER_INFO and only reads literals.
    DRIVER_INFO = {
        "id": "globalcache_itach_ip2cc",
        "name": "Global Cache iTach IP2CC Contact Closure (Relay)",
        "manufacturer": "Global Cache",
        "category": "utility",
        "version": "1.0.0",
        "author": "OpenAVC",
        "transport": "tcp",
        "description": (
            "Network-controlled three-relay contact closure. Latch or pulse "
            "each dry-contact relay to trigger screens, lifts, shades, door "
            "strikes, or the trigger input of other AV gear."
        ),
        "source_url": "https://www.globalcache.com/products/itach/ip2-family/",
        "ports": [4998],
        "protocols": ["global-cache-unified-tcp"],
        "simulated": True,
        "verified": True,
        "min_platform_version": "0.19.0",
        # Search-friendly: integrators look for what they need ("relay",
        # "contact closure", "trigger"), not the box name.
        "tags": [
            "relay", "relays", "contact-closure", "dry-contact", "trigger",
            "gpo", "switch", "global-cache", "globalcache", "itach", "ip2cc",
        ],
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
            "relay_1": {
                "type": "bool",
                "label": "Relay 1",
                "help": "True when relay 1 contacts are closed.",
            },
            "relay_2": {
                "type": "bool",
                "label": "Relay 2",
                "help": "True when relay 2 contacts are closed.",
            },
            "relay_3": {
                "type": "bool",
                "label": "Relay 3",
                "help": "True when relay 3 contacts are closed.",
            },
        },
        "commands": {
            "close": {
                "label": "Close Relay",
                "params": {
                    "relay": {
                        "type": "enum",
                        "values": ["1", "2", "3"],
                        "required": True,
                        "label": "Relay",
                    },
                },
                "help": "Close (activate) the relay's contacts and hold.",
            },
            "open": {
                "label": "Open Relay",
                "params": {
                    "relay": {
                        "type": "enum",
                        "values": ["1", "2", "3"],
                        "required": True,
                        "label": "Relay",
                    },
                },
                "help": "Open (deactivate) the relay's contacts and hold.",
            },
            "pulse": {
                "label": "Pulse Relay (Momentary)",
                "params": {
                    "relay": {
                        "type": "enum",
                        "values": ["1", "2", "3"],
                        "required": True,
                        "label": "Relay",
                    },
                    "duration_ms": {
                        "type": "integer",
                        "min": 50,
                        "max": 10000,
                        "default": 500,
                        "label": "Duration (ms)",
                    },
                },
                "help": (
                    "Close the relay momentarily, then open it. Use for "
                    "screen up/down, lifts, and other momentary triggers."
                ),
            },
            "toggle": {
                "label": "Toggle Relay",
                "params": {
                    "relay": {
                        "type": "enum",
                        "values": ["1", "2", "3"],
                        "required": True,
                        "label": "Relay",
                    },
                },
                "help": "Flip the relay between closed and open.",
            },
            "refresh": {
                "label": "Refresh",
                "params": {},
                "help": "Re-read the firmware version and all relay states.",
            },
        },
        "discovery": {
            "amx_ddp": [
                {"make": "GlobalCache", "model_pattern": "iTachIP2CC"},
            ],
            # getdevices identifies a relay unit positively (an IP2SL answers
            # the same probe with SERIAL, so match on RELAY, not endlistdevices).
            "tcp_probe": {
                "port": 4998,
                "send_ascii": "getdevices\r",
                "expect": "RELAY",
            },
            "oui": ["00:0C:1E"],
            "manufacturer_alias": ["global cache", "globalcache"],
        },
        "help": {
            "overview": (
                "The iTach IP2CC provides three network-controlled dry-contact "
                "relays. Use the Close/Open commands to latch a relay, or Pulse "
                "for a momentary trigger (screens, lifts, shades, door strikes)."
            ),
            "setup": (
                "Set the iTach to a static IP or DHCP reservation. No login is "
                "required for the control API. Wire each load to the matching "
                "relay's terminals (relays are dry contacts, 5-16V DC @ 300mA)."
            ),
            "connection": (
                "Command API on TCP 4998. Relay states are not retained across "
                "a power cycle: all relays return to open on power up."
            ),
        },
    }

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state: Any,
        events: Any,
    ) -> None:
        super().__init__(device_id, config, state, events)
        # Last-known relay states (connector -> closed), updated from every
        # state line the unit reports. Seeds toggle and gives snappy UI.
        self._relay_state: dict[int, bool] = {}

    # --- Liveness + relay-state polling (over the auto 4998 TCP transport) ---

    async def poll(self) -> None:
        """Refresh identity + all relay states; propagate transport errors.

        Sends getversion + a getstate for each relay over the persistent 4998
        connection; responses arrive via on_data_received and update state. A
        dead transport makes ``send`` raise, which the watchdog counts toward
        going offline.
        """
        if self.transport is None:
            raise ConnectionError("iTach command transport not connected")
        await self.transport.send(b"getversion" + CR)
        for relay in range(1, RELAY_COUNT + 1):
            await self.transport.send(format_getstate(relay))

    async def on_data_received(self, data: bytes) -> None:
        """Route a response line (identity or relay state) to the state store.

        The TCP transport frames on CR and delivers each line with the
        delimiter already stripped, so ``data`` is normally a single line;
        the CR split is defensive in case unframed bytes ever arrive.
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
            parsed = parse_state_line(line)
            if parsed:
                self._apply_relay_state(parsed["relay"], parsed["closed"])

    def _apply_relay_state(self, relay: int, closed: bool) -> None:
        """Record a relay's state locally and publish it to the state store."""
        self._relay_state[relay] = closed
        self.set_state(f"relay_{relay}", closed)

    # --- Commands ---

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Execute a relay command (close / open / pulse / toggle / refresh)."""
        params = params or {}

        if command == "refresh":
            await self.poll()
            return {"status": "ok"}

        if command in ("close", "open", "pulse", "toggle"):
            relay = self._coerce_relay(params)
            if command == "close":
                await self._set_relay(relay, True)
            elif command == "open":
                await self._set_relay(relay, False)
            elif command == "toggle":
                await self._set_relay(relay, not self._relay_state.get(relay, False))
            else:  # pulse
                duration_ms = self._coerce_duration(params)
                await self._set_relay(relay, True)
                await asyncio.sleep(duration_ms / 1000.0)
                await self._set_relay(relay, False)
            return {"status": "ok", "relay": relay}

        raise ValueError(f"Unknown command: {command}")

    async def _set_relay(self, relay: int, closed: bool) -> None:
        """Send setstate for one relay and optimistically publish its state.

        The unit echoes the command (``setstate,1:n,v``); on_data_received also
        reconciles from that echo and from the next getstate poll.
        """
        if self.transport is None:
            raise ConnectionError("iTach command transport not connected")
        await self.transport.send(format_setstate(relay, closed))
        self._apply_relay_state(relay, closed)
        log.info(
            "[%s] Relay %d %s", self.device_id, relay,
            "closed" if closed else "opened",
        )

    @staticmethod
    def _coerce_relay(params: dict[str, Any]) -> int:
        """Validate the ``relay`` param to an int in 1..RELAY_COUNT."""
        try:
            relay = int(params.get("relay"))
        except (TypeError, ValueError):
            raise ValueError(f"relay must be one of 1-{RELAY_COUNT}") from None
        if not 1 <= relay <= RELAY_COUNT:
            raise ValueError(f"relay must be one of 1-{RELAY_COUNT}, got {relay}")
        return relay

    @staticmethod
    def _coerce_duration(params: dict[str, Any]) -> int:
        """Clamp the pulse ``duration_ms`` param to the declared 50..10000 ms."""
        try:
            duration = int(params.get("duration_ms", 500))
        except (TypeError, ValueError):
            duration = 500
        return max(50, min(10000, duration))
