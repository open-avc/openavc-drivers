"""
OpenAVC Art-Net DMX Driver.

Sends DMX512 lighting data to an Art-Net node (or broadcast across a
subnet) over UDP port 6454. Implements the ArtDmx packet (OpCode
0x5000) from the Art-Net 4 specification — that's the workhorse
packet for controlling fixtures, dimmers, moving heads, LED strips,
and architectural lighting controllers that speak Art-Net.

Models covered:
    Generic. Art-Net is an open standard published by Artistic
    Licence Holdings; thousands of products from ETC, MA Lighting,
    Chamsys, Pathway Connectivity, Enttec, ADJ, Chauvet, Elation,
    Madrix, GrandMA gateways, Pharos, Color Kinetics, and almost
    every Art-Net-to-DMX gateway speak it. One driver, the entire
    ecosystem.

Push vs poll:
    The driver is a *transmitter* — Art-Net is fundamentally a
    push protocol from the controller's side. Per the spec, an
    active controller should re-transmit the current state every
    800-1000 ms even if nothing has changed (recommended keep-alive
    so the receiving node doesn't time out and drop the universe).
    The driver maintains a 512-byte channel buffer per universe in
    memory, sends an ArtDmx whenever a command modifies it, AND
    schedules a periodic refresh send on the keep-alive cadence.

Why Python (not YAML):
    ArtDmx is a binary packet with an embedded sequence counter,
    a 15-bit Port-Address split into separate Net (top 7 bits) and
    SubUni (low 8 bits) fields, big-endian length encoding for the
    DMX payload, and most importantly — a 512-byte buffer that
    persists across commands. ConfigurableDriver's UDP path can
    send static byte strings, but it cannot maintain mutable
    per-universe DMX state across calls, increment a sequence
    counter, or schedule periodic re-broadcast for keep-alive.
    Python is the right shape here.

Sources:
    Art-Net 4 Protocol Release V1.4, Document Revision 1.4dp
    23/10/2025 (Artistic Licence Holdings):
        https://art-net.org.uk/downloads/art-net.pdf
    Wikipedia overview (corroborates header and packet layout):
        https://en.wikipedia.org/wiki/Art-Net
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.udp import UDPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)


# Art-Net constants from the spec.
ARTNET_PORT = 6454              # 0x1936 in spec, "Art-Net" UDP port
ARTNET_PROTOCOL_VERSION = 14    # 0x000E, current spec value
OP_DMX = 0x5000                 # ArtDmx packet OpCode
ARTNET_HEADER_ID = b"Art-Net\x00"

# DMX universe parameters.
DMX_UNIVERSE_SIZE = 512         # Standard DMX512 universe channel count
MIN_CHANNEL = 1                 # Channels are 1-indexed in user-facing API
MAX_CHANNEL = 512
MIN_VALUE = 0
MAX_VALUE = 255

# Keep-alive cadence — spec recommends 800 to 1000 ms.
KEEPALIVE_INTERVAL_SEC = 0.9


def build_artdmx_packet(
    sequence: int,
    universe: int,
    dmx_data: bytes,
    physical_port: int = 0,
) -> bytes:
    """Build an ArtDmx (OpCode 0x5000) packet.

    Args:
        sequence: 1..255 incrementing counter (0 disables ordering).
        universe: 0..32767 (15-bit Port-Address). Split into Net
            (top 7 bits) + SubUni (low 8 bits) over the wire.
        dmx_data: The DMX channel payload, 2..512 bytes (must be
            even per spec). Will be passed through verbatim — the
            caller is responsible for any zero-padding to 512.
        physical_port: Informational only; defaults to 0.

    Returns:
        The encoded packet bytes ready to drop on the wire.
    """
    if not 0 <= universe <= 0x7FFF:
        raise ValueError(f"universe out of range (0..32767): {universe}")
    length = len(dmx_data)
    if length < 2 or length > DMX_UNIVERSE_SIZE or length % 2 != 0:
        raise ValueError(
            f"DMX payload must be 2..512 bytes, even: got {length}"
        )

    net = (universe >> 8) & 0x7F
    sub_uni = universe & 0xFF

    return (
        ARTNET_HEADER_ID
        + (OP_DMX).to_bytes(2, "little")              # OpCode (LE)
        + (ARTNET_PROTOCOL_VERSION).to_bytes(2, "big")  # ProtVer (BE)
        + bytes([sequence & 0xFF, physical_port & 0xFF])
        + bytes([sub_uni, net])
        + length.to_bytes(2, "big")                    # Length (BE)
        + dmx_data
    )


class ArtNetDMXDriver(BaseDriver):
    """Art-Net DMX controller / transmitter."""

    DRIVER_INFO = {
        "id": "artnet_dmx",
        "name": "Art-Net DMX (Generic)",
        "manufacturer": "Generic",
        "category": "lighting",
        "version": "1.4.1",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "author": "OpenAVC",
        "description": (
            "Sends DMX512 lighting data over Art-Net (UDP 6454) to "
            "any Art-Net-compatible node, gateway, fixture, or "
            "controller. Maintains a 512-byte channel buffer per "
            "universe in memory; commands set channels, blackout, "
            "or full-on; a periodic keep-alive re-broadcast keeps "
            "long-running shows alive on receivers that time out "
            "idle universes."
        ),
        "source_url": "https://art-net.org.uk/downloads/art-net.pdf",
        "tags": [
            "artnet",
            "dmx",
            "dmx512",
            "lighting",
            "generic",
            "etc",
            "ma",
            "chamsys",
            "enttec",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["artnet"],
        "ports": [6454],
        "transport": "udp",
        "discovery": {
            # Art-Net 4 spec is fully public — every conforming node answers
            # an ArtPoll broadcast on UDP 6454 with an ArtPollReply that
            # carries ShortName (offset 26, 18 bytes) and LongName (offset
            # 44, 64 bytes) NUL-padded ASCII fields. cross_vendor = true so
            # vendor-specific Art-Net drivers (e.g. dedicated ETC / MA /
            # Pathport drivers added later) can claim the device by
            # manufacturer_alias. Probe is silent: TalkToMe=0x00 means
            # reply-to-poll only, no diagnostics, no VLC.
            #   Art-Net 4 spec: https://art-net.org.uk/downloads/art-net.pdf
            #   ArtNode reference (MIT): github.com/tobiasebsen/ArtNode
            #   go-artnet (MIT): github.com/jsimonetti/go-artnet
            "udp_probe": {
                "port": 6454,
                # ArtPoll: "Art-Net\0" + OpPoll(0x2000 LE) + ProtVer(0x000E)
                # + TalkToMe(0x00) + Priority(0x10 = DpLow).
                "send_hex": "41 72 74 2D 4E 65 74 00 00 20 00 0E 00 10",
                # "Art-Net\0" + OpPollReply(0x2100 LE).
                "expect_hex": "41 72 74 2D 4E 65 74 00 00 21",
                "extract": {
                    # ShortName: 18-byte NUL-padded ASCII at offset 26.
                    "shortname": {
                        "regex": r"^[\s\S]{26}([\x20-\x7E]{1,17})\x00",
                        "group": 1,
                    },
                    # LongName: 64-byte NUL-padded ASCII at offset 44.
                    "longname": {
                        "regex": r"^[\s\S]{44}([\x20-\x7E]{1,63})\x00",
                        "group": 1,
                    },
                },
                "timeout_ms": 1500,
                "cross_vendor": True,
            },
            "manufacturer_alias": ["artnet", "art-net"],
        },
        "compatible_models": [
            {
                "manufacturer": "Generic",
                "models": ["Art-Net Node / Gateway / Fixture (any conforming vendor)"],
                "confidence": "untested",
                "notes": (
                    "Art-Net is an open published standard maintained by Artistic "
                    "Licence. This driver sends ArtDmx and works with any conforming "
                    "Art-Net receiver. Brand-specific entries below name the major "
                    "node / gateway / fixture vendors confirmed by their published "
                    "product literature."
                ),
            },
            {
                "manufacturer": "ETC",
                "models": [
                    "Response Mk2 Gateway",
                    "Net3 Gateway",
                    "Net3 Show Control Gateway",
                    "Net3 4-port Gateway",
                ],
                "confidence": "untested",
                "notes": (
                    "ETC's Response and Net3 gateway families translate Art-Net (and "
                    "sACN) to physical DMX outputs. ETC's Eos console is also covered "
                    "by the dedicated `etc_eos` OSC driver, which is the right choice "
                    "when controlling cues directly rather than driving fixtures."
                ),
            },
            {
                "manufacturer": "Pathway Connectivity",
                "models": [
                    "Pathport Octo",
                    "Pathport Quattro",
                    "Pathport Q42",
                    "VIA 12 / VIA 24",
                ],
                "confidence": "untested",
                "notes": (
                    "Pathway Pathport gateways translate Art-Net and sACN to DMX. "
                    "Common in pro AV and theatrical installs."
                ),
            },
            {
                "manufacturer": "Enttec",
                "models": [
                    "ODE Mk2 / Mk3",
                    "DIN-ODE",
                    "S-Play Mini",
                    "DMX Storm 8",
                    "Storm 24",
                ],
                "confidence": "untested",
                "notes": (
                    "Enttec ODE family converts Art-Net to DMX. ODE Mk3 also speaks "
                    "sACN. The smaller-footprint DIN-ODE fits a DIN rail in install racks."
                ),
            },
            {
                "manufacturer": "Elation",
                "models": [
                    "E-Node 2",
                    "E-Node 4",
                    "E-Node 8",
                ],
                "confidence": "untested",
                "notes": (
                    "Elation E-Node series Art-Net / sACN to DMX gateways."
                ),
            },
            {
                "manufacturer": "Chauvet",
                "models": [
                    "Net-X II",
                    "DataStream 4",
                ],
                "confidence": "untested",
                "notes": (
                    "Chauvet Professional network nodes for entertainment lighting."
                ),
            },
            {
                "manufacturer": "ADJ",
                "models": [
                    "Net4",
                    "Net8",
                ],
                "confidence": "untested",
                "notes": (
                    "American DJ network DMX gateways."
                ),
            },
            {
                "manufacturer": "Color Kinetics",
                "models": [
                    "Data Enabler Pro",
                    "Antumbra Touch",
                    "iColor Player",
                    "PDS-150e / PDS-200e",
                ],
                "confidence": "untested",
                "notes": (
                    "Color Kinetics (Signify) architectural LED gateways. The "
                    "controller side speaks Art-Net to drive Color Kinetics fixtures "
                    "via PDS power/data supplies and Antumbra controllers."
                ),
            },
            {
                "manufacturer": "MA Lighting",
                "models": [
                    "grandMA3 onPC",
                    "grandMA3 NPU",
                    "MA NPU (legacy)",
                    "grandMA2 NPU (legacy)",
                ],
                "confidence": "untested",
                "notes": (
                    "MA Lighting Network Processing Units translate console output to "
                    "Art-Net and sACN for downstream gateways. Useful for tracking "
                    "backup or for sending Art-Net into an MA console for visualizer "
                    "/ pre-program workflows."
                ),
            },
            {
                "manufacturer": "Madrix",
                "models": [
                    "Madrix 5 Software",
                    "Madrix Aura Ethernet-DMX Bridge",
                    "Madrix Plexus",
                ],
                "confidence": "untested",
                "notes": (
                    "Madrix is primarily an Art-Net source for LED video pixel-mapping, "
                    "but its Aura and Plexus hardware bridges accept Art-Net input and "
                    "convert to DMX or playback stored shows."
                ),
            },
        ],
        "help": {
            "overview": (
                "Art-Net is the de-facto Ethernet protocol for "
                "DMX512 lighting. Controllers send ArtDmx packets "
                "to nodes (Art-Net to DMX gateways), or directly "
                "to network-native fixtures. The driver maintains "
                "a 512-channel buffer for each universe it controls "
                "and re-broadcasts on a 900 ms keep-alive cadence "
                "so the receiver doesn't drop the universe."
            ),
            "setup": (
                "1. Set the target node's IP to a static address "
                "and put it on the same VLAN as the OpenAVC server. "
                "Art-Net traditionally uses the 2.x.x.x or 10.x.x.x "
                "range, but any reachable IP works.\n"
                "2. Confirm the node's universe number — Art-Net "
                "calls it the Port-Address (0..32767). Most "
                "consoles and fixtures expose it as a Net + Subnet "
                "+ Universe trio (7 + 4 + 4 bits); enter the "
                "combined 15-bit value here. Universe 0 is a "
                "common default.\n"
                "3. To broadcast (one transmit reaches all nodes "
                "on the subnet), use the broadcast IP for your "
                "subnet. Unicast to a node's IP is preferred per "
                "the Art-Net 4 spec.\n"
                "4. Run the ``set_channel`` or ``blackout`` "
                "command. The driver starts re-broadcasting "
                "automatically; no separate ``start`` is needed."
            ),
        },
        "default_config": {
            "host": "",
            "port": 6454,
            "universe": 0,
            "keepalive": True,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "Target Host / Broadcast",
                "description": (
                    "IP address of the Art-Net node, or a subnet "
                    "broadcast address (e.g. 192.168.1.255). "
                    "Unicast is preferred per the spec."
                ),
            },
            "port": {
                "type": "integer",
                "default": 6454,
                "label": "UDP Port",
                "description": "Art-Net standard is 6454.",
            },
            "universe": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "max": 32767,
                "label": "Universe (Port-Address)",
                "description": (
                    "15-bit Port-Address (0..32767). The driver "
                    "splits this into Net (top 7 bits) and SubUni "
                    "(low 8 bits) on the wire."
                ),
            },
            "keepalive": {
                "type": "boolean",
                "default": True,
                "label": "Keep-alive Re-broadcast",
                "description": (
                    "Re-broadcast the current channel buffer every "
                    "~900 ms per the Art-Net 4 spec recommendation. "
                    "Disable for one-shot updates."
                ),
            },
        },
        "state_variables": {
            "target_universe": {
                "type": "integer",
                "label": "Universe",
            },
            "last_channel": {
                "type": "integer",
                "label": "Last Channel Set",
            },
            "last_value": {
                "type": "integer",
                "label": "Last Value Sent",
            },
            "packets_sent": {
                "type": "integer",
                "label": "Packets Sent (Lifetime)",
            },
            "blackout": {
                "type": "boolean",
                "label": "Blackout Active",
            },
        },
        "commands": {
            "set_channel": {
                "label": "Set Channel",
                "params": {
                    "channel": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 512,
                        "label": "Channel (1..512)",
                    },
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 255,
                        "label": "Value (0..255)",
                    },
                },
                "help": (
                    "Sets one DMX channel and broadcasts the "
                    "updated buffer."
                ),
            },
            "set_channels": {
                "label": "Set Multiple Channels (CSV)",
                "params": {
                    "start_channel": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 512,
                        "label": "Starting Channel",
                    },
                    "values": {
                        "type": "string",
                        "required": True,
                        "label": "Values (comma-separated 0..255)",
                        "help": (
                            "Comma-separated values, e.g. "
                            "``128,64,255,0``. Written to "
                            "consecutive channels starting at "
                            "``start_channel``."
                        ),
                    },
                },
            },
            "blackout": {
                "label": "Blackout (All Channels 0)",
                "params": {},
            },
            "full_on": {
                "label": "Full On (All Channels 255)",
                "params": {},
                "help": (
                    "Set every channel in the buffer to 255. "
                    "Useful for fixture testing."
                ),
            },
            "set_universe": {
                "label": "Change Universe at Runtime",
                "params": {
                    "universe": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 32767,
                    },
                },
                "help": (
                    "Switch the target universe without "
                    "reconnecting. The buffer follows the new "
                    "universe (channels carry over)."
                ),
            },
            "send_now": {
                "label": "Send Current Buffer",
                "params": {},
                "help": (
                    "Force an immediate ArtDmx send of the "
                    "current buffer, independent of the keep-alive "
                    "schedule."
                ),
            },
        },
        # Quick Action strip: the two whole-universe moves. Blackout is
        # the panic-safe action so it fires without a confirm; full-on
        # drives every fixture to maximum, so it asks first.
        "actions": [
            {"id": "blackout", "kind": "command", "icon": "moon"},
            {
                "id": "full_on", "kind": "command", "icon": "sun",
                "confirm": (
                    "Drive all 512 channels to 255? Every fixture on "
                    "this universe goes to full."
                ),
            },
        ],
    }

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state,
        events,
    ) -> None:
        self._buffer = bytearray(DMX_UNIVERSE_SIZE)
        self._sequence = 1
        self._universe = 0
        self._keepalive_task: asyncio.Task | None = None
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        # Built here rather than by the platform ladder: the target may be
        # a subnet broadcast address, so the socket must open with
        # allow_broadcast — and Art-Net is send-only from our side, so
        # there is no inbound data callback to wire up.
        host = self.config.get("host", "")
        port = int(self.config.get("port", ARTNET_PORT))
        self._universe = int(self.config.get("universe", 0))

        self.transport = UDPTransport(
            host=host,
            port=port,
            on_disconnect=self._handle_transport_disconnect,
            name=self.device_id,
        )
        await self.transport.open(allow_broadcast=True)
        log.info(
            f"[{self.device_id}] Art-Net controller bound for "
            f"{host}:{port} (universe {self._universe})"
        )

    async def _initial_sync(self) -> None:
        self.set_state("target_universe", self._universe)
        self.set_state("packets_sent", 0)
        self.set_state("blackout", True)  # buffer starts all-zero
        if self.config.get("keepalive", True):
            self._keepalive_task = asyncio.create_task(self._keepalive())

    async def _close_session(self) -> None:
        # Runs on every teardown path — stop the keep-alive re-broadcast.
        # Tolerates being called when the task was never started.
        await self._stop_keepalive()

    async def _stop_keepalive(self) -> None:
        task = self._keepalive_task
        self._keepalive_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ── Sending ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "set_channel":
            channel = int(params.get("channel", 0))
            value = int(params.get("value", 0))
            if not MIN_CHANNEL <= channel <= MAX_CHANNEL:
                log.warning(
                    f"[{self.device_id}] Channel {channel} out of range"
                )
                return
            self._buffer[channel - 1] = max(MIN_VALUE, min(MAX_VALUE, value))
            self.set_state("last_channel", channel)
            self.set_state("last_value", value)
            self.set_state("blackout", False)
            await self._send_buffer()
            return

        if command == "set_channels":
            start = int(params.get("start_channel", 1))
            raw = str(params.get("values", "")).strip()
            if not MIN_CHANNEL <= start <= MAX_CHANNEL:
                log.warning(
                    f"[{self.device_id}] Start channel {start} out of range"
                )
                return
            try:
                values = [
                    max(MIN_VALUE, min(MAX_VALUE, int(p.strip())))
                    for p in raw.split(",")
                    if p.strip()
                ]
            except ValueError:
                log.warning(
                    f"[{self.device_id}] Bad value list: {raw!r}"
                )
                return
            end = min(MAX_CHANNEL, start - 1 + len(values))
            for i, v in enumerate(values):
                idx = start - 1 + i
                if idx >= DMX_UNIVERSE_SIZE:
                    break
                self._buffer[idx] = v
            self.set_state("last_channel", end)
            self.set_state(
                "last_value", values[-1] if values else 0
            )
            self.set_state("blackout", False)
            await self._send_buffer()
            return

        if command == "blackout":
            for i in range(DMX_UNIVERSE_SIZE):
                self._buffer[i] = 0
            self.set_state("last_channel", 0)
            self.set_state("last_value", 0)
            self.set_state("blackout", True)
            await self._send_buffer()
            return

        if command == "full_on":
            for i in range(DMX_UNIVERSE_SIZE):
                self._buffer[i] = MAX_VALUE
            self.set_state("last_channel", MAX_CHANNEL)
            self.set_state("last_value", MAX_VALUE)
            self.set_state("blackout", False)
            await self._send_buffer()
            return

        if command == "set_universe":
            new_u = int(params.get("universe", 0))
            if not 0 <= new_u <= 32767:
                log.warning(
                    f"[{self.device_id}] Universe {new_u} out of range"
                )
                return
            self._universe = new_u
            self.set_state("target_universe", new_u)
            await self._send_buffer()
            return

        if command == "send_now":
            await self._send_buffer()
            return

        log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def _send_buffer(self) -> None:
        if not self.transport or not self._connected:
            return
        packet = build_artdmx_packet(
            sequence=self._sequence,
            universe=self._universe,
            dmx_data=bytes(self._buffer),
        )
        try:
            await self.transport.send(packet)
        except (ConnectionError, OSError):
            log.warning(
                f"[{self.device_id}] ArtDmx send failed"
            )
            return
        # Sequence wraps 1..255 — 0 means "ordering disabled".
        self._sequence = self._sequence + 1 if self._sequence < 255 else 1
        try:
            current = int(self.get_state("packets_sent") or 0)
        except (TypeError, ValueError):
            current = 0
        self.set_state("packets_sent", current + 1)

    async def _keepalive(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL_SEC)
                if not self._connected:
                    return
                await self._send_buffer()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception(
                f"[{self.device_id}] Art-Net keep-alive task crashed"
            )
