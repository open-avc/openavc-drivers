"""
OpenAVC sACN / E1.31 DMX Driver.

Streams DMX512-A lighting data to sACN (Streaming ACN, ANSI E1.31)
receivers over UDP port 5568. Implements the E1.31 Data Packet
(Root vector VECTOR_ROOT_E131_DATA, Framing vector
VECTOR_E131_DATA_PACKET) carrying a DMP Set-Property message — the
workhorse packet for driving fixtures, dimmers, moving heads, LED
strips, pixel tape, and architectural lighting gear that speaks sACN.

sACN is the IETF-multicast sibling of Art-Net (see ``artnet_dmx``):
same job (DMX-over-IP), different wire format. Where Art-Net is a
proprietary-but-published Artistic Licence protocol carried by
broadcast/unicast, sACN is an open ANSI standard that natively uses
per-universe multicast (239.255.<hi>.<lo>) so each universe lands
only on the receivers that joined its group. Many gateways speak
both; pick this driver when the install standardized on sACN, or
when per-source priority arbitration (an sACN feature Art-Net lacks)
matters.

Models covered:
    Generic. ANSI E1.31 is a public ESTA standard. Products from
    ETC, MA Lighting, Pathway Connectivity, Enttec, Elation, Chauvet,
    ADJ, Color Kinetics, Madrix, and essentially every modern
    sACN-to-DMX gateway or network-native fixture accept it. One
    driver, the whole ecosystem.

Push vs poll:
    The driver is a *transmitter* — sACN is a push protocol from the
    controller's side. Per E1.31 6.6.2 the source sends a data
    packet whenever the DMX values change, then (after three
    identical packets) suppresses redundant traffic down to a single
    keep-alive packet every 800-1000 ms so a receiver's 2.5 s
    network-data-loss timeout never fires on a static look. The
    driver maintains a 512-byte channel buffer per universe in
    memory, sends on every change, and schedules the keep-alive
    re-send. On a clean disconnect it sends three packets with the
    Stream_Terminated option bit set (E1.31 6.2.6 / 12.2) so
    receivers release the source immediately instead of waiting out
    the timeout.

Why Python (not YAML):
    The E1.31 Data Packet is a 638-byte binary structure with three
    nested ACN PDUs, each carrying a 16-bit flags-and-length field
    (0x7 nibble + 12-bit PDU length), a 16-byte source CID, an
    incrementing sequence byte, big-endian universe/priority fields,
    and a 512-byte DMX buffer that persists across commands. The
    multicast destination is *derived from the universe*
    (239.255.<hi>.<lo>), so it changes when the universe changes.
    ConfigurableDriver's UDP path sends static byte strings; it
    cannot maintain mutable per-universe DMX state, increment a
    sequence counter, recompute the destination, or schedule the
    keep-alive / termination sends. Python is the right shape here,
    exactly as with ``artnet_dmx``.

Out of scope (documented, mirrors artnet_dmx skipping ArtPoll):
    E1.31 Universe Discovery advertisement (the periodic 64214
    enumeration for passive network monitors — it does not affect
    controllability of fixtures/gateways), Universe Synchronization
    packets, and the receiver side. This driver stays focused on the
    DMX transmission path that installs actually need.

Sources:
    ANSI E1.31-2018, Entertainment Technology — Lightweight
    streaming protocol for transport of DMX512 using ACN (ESTA):
        https://tsp.esta.org/tsp/documents/docs/E1-31-2018.pdf
    Packet layout: Table 4-1 (Data Packet), Section 5 (Root Layer),
    Section 6 (Framing Layer), Section 7 (DMP Layer), Section 9
    (multicast addressing), Appendix A (defined parameters).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.transport.udp import UDPTransport
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ── E1.31 constants (ANSI E1.31-2018, Appendix A + Section 4) ──
ACN_SDT_MULTICAST_PORT = 5568          # standard sACN UDP port
E131_ROOT_PREAMBLE_SIZE = 0x0010       # octets 0-1
E131_ROOT_POSTAMBLE_SIZE = 0x0000      # octets 2-3
# ACN Packet Identifier, octets 4-15: "ASC-E1.17" + three NULs.
E131_ACN_PID = b"ASC-E1.17\x00\x00\x00"
VECTOR_ROOT_E131_DATA = 0x00000004     # Root Layer vector (data)
VECTOR_E131_DATA_PACKET = 0x00000002   # Framing Layer vector (data)
VECTOR_DMP_SET_PROPERTY = 0x02         # DMP Layer vector
DMP_ADDRESS_DATA_TYPE = 0xA1           # Address Type & Data Type (octet 118)
PDU_FLAGS = 0x7                        # top nibble of every flags/length field

# DMX / universe parameters.
DMX_UNIVERSE_SIZE = 512
DMX_START_CODE = 0x00                  # Null START code (dimmer data)
MIN_CHANNEL = 1
MAX_CHANNEL = 512
MIN_VALUE = 0
MAX_VALUE = 255

# E1.31 6.2.7 / 9.3.1: data universes are 1..63999. 0 and 64000-65535
# are reserved (64214 is the discovery universe).
UNIVERSE_MIN = 1
UNIVERSE_MAX = 63999

# E1.31 6.2.3: priority is 0..200, default 100. Higher wins in a
# multi-source (HTP/priority) merge on the receiver.
PRIORITY_MIN = 0
PRIORITY_MAX = 200
PRIORITY_DEFAULT = 100

# E1.31 6.2.6 Options bits (octet 112).
OPT_PREVIEW_DATA = 0x80                # bit 7
OPT_STREAM_TERMINATED = 0x40           # bit 6
OPT_FORCE_SYNCHRONIZATION = 0x20       # bit 5

# Source Name field (octets 44-107) is 64 bytes, UTF-8, NUL-terminated.
SOURCE_NAME_SIZE = 64

# Keep-alive cadence — E1.31 6.6.2 says 800..1000 ms.
KEEPALIVE_INTERVAL_SEC = 0.9
# E1.31 6.2.6 / 12.2: send three Stream_Terminated packets on shutdown.
TERMINATION_PACKET_COUNT = 3

# Stable namespace for deriving a per-device CID (see _derive_cid).
_CID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_DNS


def _flags_and_length(pdu_length: int) -> bytes:
    """Encode an ACN flags-and-length field: 0x7 in the top nibble,
    the PDU length in the low 12 bits, big-endian (E1.31 5.4)."""
    return ((PDU_FLAGS << 12) | (pdu_length & 0x0FFF)).to_bytes(2, "big")


def universe_to_multicast(universe: int) -> str:
    """Map a universe number to its IPv4 multicast group (E1.31 9.3.1,
    Table 9-1): 239.255.<universe-hi>.<universe-lo>."""
    return f"239.255.{(universe >> 8) & 0xFF}.{universe & 0xFF}"


def build_e131_data_packet(
    cid: bytes,
    source_name: str,
    priority: int,
    sequence: int,
    universe: int,
    dmx_data: bytes,
    *,
    sync_address: int = 0,
    options: int = 0,
    start_code: int = DMX_START_CODE,
) -> bytes:
    """Build an E1.31 Data Packet (ANSI E1.31-2018, Table 4-1).

    Args:
        cid: 16-byte sender Component Identifier (RFC 4122 UUID bytes).
        source_name: user-assigned source label; truncated to 63 bytes
            of UTF-8 and NUL-padded to the 64-byte field.
        priority: 0..200 data priority (6.2.3).
        sequence: 0..255 sequence byte (wraps; 6.2.5 / 6.7.2).
        universe: 1..63999 universe number (6.2.7).
        dmx_data: the DMX slot values; padded/truncated to 512 by the
            caller. Sent verbatim after the START code.
        sync_address: synchronization universe, 0 = unsynchronized.
        options: Options-field byte (6.2.6); e.g. OPT_STREAM_TERMINATED.
        start_code: DMX START code prepended to the property values
            (0x00 = dimmer data).

    Returns:
        The encoded packet, ready for the wire. All fields big-endian.
    """
    if not len(cid) == 16:
        raise ValueError(f"CID must be 16 bytes, got {len(cid)}")
    if not 0 <= priority <= 255:
        raise ValueError(f"priority byte out of range: {priority}")

    property_values = bytes([start_code & 0xFF]) + dmx_data
    property_value_count = len(property_values)  # 1 + slots (max 513)

    # PDU lengths, each measured from its own flags/length octet to the
    # last property value (E1.31 5.4, 6.1, 7.1). Derived, not hard-coded,
    # so a short payload still frames correctly.
    dmp_pdu_len = 11 + len(dmx_data)          # from octet 115
    framing_pdu_len = 77 + dmp_pdu_len        # from octet 38
    root_pdu_len = 22 + framing_pdu_len       # from octet 16

    name_field = source_name.encode("utf-8")[: SOURCE_NAME_SIZE - 1].ljust(
        SOURCE_NAME_SIZE, b"\x00"
    )

    return (
        # ── Root Layer (octets 0-37) ──
        E131_ROOT_PREAMBLE_SIZE.to_bytes(2, "big")
        + E131_ROOT_POSTAMBLE_SIZE.to_bytes(2, "big")
        + E131_ACN_PID
        + _flags_and_length(root_pdu_len)
        + VECTOR_ROOT_E131_DATA.to_bytes(4, "big")
        + cid
        # ── E1.31 Framing Layer (octets 38-114) ──
        + _flags_and_length(framing_pdu_len)
        + VECTOR_E131_DATA_PACKET.to_bytes(4, "big")
        + name_field
        + bytes([priority & 0xFF])
        + (sync_address & 0xFFFF).to_bytes(2, "big")
        + bytes([sequence & 0xFF])
        + bytes([options & 0xFF])
        + (universe & 0xFFFF).to_bytes(2, "big")
        # ── DMP Layer (octets 115-637) ──
        + _flags_and_length(dmp_pdu_len)
        + bytes([VECTOR_DMP_SET_PROPERTY])
        + bytes([DMP_ADDRESS_DATA_TYPE])
        + (0x0000).to_bytes(2, "big")           # First Property Address
        + (0x0001).to_bytes(2, "big")           # Address Increment
        + property_value_count.to_bytes(2, "big")
        + property_values
    )


def _derive_cid(device_id: str) -> bytes:
    """Derive a stable 16-byte CID from the device id.

    A source is tracked by its CID; deriving it deterministically means
    a server restart looks like the *same* source to receivers (no
    duplicate-source arbitration churn) without persisting anything."""
    return uuid.uuid5(_CID_NAMESPACE, f"openavc-sacn-{device_id}").bytes


class SacnE131Driver(BaseDriver):
    """sACN / E1.31 DMX controller / transmitter."""

    DRIVER_INFO = {
        "id": "sacn_e131",
        "name": "sACN / E1.31 DMX (Generic)",
        "manufacturer": "Generic",
        "category": "lighting",
        "version": "1.0.2",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Streams DMX512 lighting data over sACN (Streaming ACN, "
            "ANSI E1.31) to any sACN-compatible node, gateway, "
            "fixture, or controller. Uses per-universe multicast "
            "(239.255.x.x, UDP 5568) by default, or unicast to a "
            "specific node. Maintains a 512-byte channel buffer per "
            "universe in memory; commands set channels, blackout, or "
            "full-on; per-source priority tunes multi-source merging; "
            "a keep-alive re-send holds static looks alive and a "
            "graceful stream-termination releases the universe on "
            "disconnect."
        ),
        "source_url": "https://tsp.esta.org/tsp/documents/docs/E1-31-2018.pdf",
        "tags": [
            "sacn",
            "e131",
            "e1-31",
            "dmx",
            "dmx512",
            "lighting",
            "generic",
            "etc",
            "ma",
            "pathway",
            "enttec",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["sacn", "e131"],
        "ports": [5568],
        "transport": "udp",
        "discovery": {
            # sACN has no request/response discovery to probe: sources
            # push, receivers passively join multicast groups, and the
            # Universe Discovery advertisement is a one-way source
            # broadcast on universe 64214 (a passive-listener concern,
            # not a probe). So there is no active fingerprint to send.
            # Most sACN gateways also speak Art-Net and are surfaced by
            # the artnet_dmx ArtPoll probe; the alias lets a scan that
            # already identified an sACN-capable device offer this
            # driver. (ANSI E1.31-2018 Section 12.)
            "manufacturer_alias": ["sacn", "e131", "e1.31", "streaming acn"],
        },
        "compatible_models": [
            {
                "manufacturer": "Generic",
                "models": [
                    "sACN / E1.31 Node / Gateway / Fixture (any conforming vendor)"
                ],
                "confidence": "untested",
                "notes": (
                    "sACN is the ANSI E1.31 public standard maintained by ESTA. "
                    "This driver sends E1.31 Data Packets and works with any "
                    "conforming sACN receiver. Brand entries below name major "
                    "node / gateway / fixture vendors confirmed by their "
                    "published product literature."
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
                    "ETC's Response and Net3 gateway families translate sACN (and "
                    "Art-Net) to physical DMX outputs. ETC's Eos console is also "
                    "covered by the dedicated etc_eos OSC driver, which is the "
                    "right choice when controlling cues directly rather than "
                    "driving fixtures."
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
                    "Pathway Pathport gateways translate sACN and Art-Net to DMX. "
                    "Common in pro AV and theatrical installs; sACN is Pathway's "
                    "preferred network protocol."
                ),
            },
            {
                "manufacturer": "Enttec",
                "models": [
                    "ODE Mk3",
                    "DIN-ODE Mk3",
                    "S-Play Mini",
                    "Storm 24",
                ],
                "confidence": "untested",
                "notes": (
                    "Enttec ODE Mk3 and S-Play families accept sACN and convert "
                    "to DMX. The DIN-ODE fits a DIN rail in install racks."
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
                "notes": "Elation E-Node series sACN / Art-Net to DMX gateways.",
            },
            {
                "manufacturer": "Chauvet",
                "models": [
                    "Net-X II",
                    "DataStream 4",
                ],
                "confidence": "untested",
                "notes": (
                    "Chauvet Professional network nodes for entertainment "
                    "lighting; sACN and Art-Net capable."
                ),
            },
            {
                "manufacturer": "Color Kinetics",
                "models": [
                    "Data Enabler Pro",
                    "Antumbra Touch",
                    "PDS-150e / PDS-200e",
                ],
                "confidence": "untested",
                "notes": (
                    "Color Kinetics (Signify) architectural LED gateways. The "
                    "controller side speaks sACN to drive Color Kinetics fixtures "
                    "via PDS power/data supplies and Antumbra controllers."
                ),
            },
            {
                "manufacturer": "MA Lighting",
                "models": [
                    "grandMA3 onPC",
                    "grandMA3 NPU",
                    "grandMA2 NPU (legacy)",
                ],
                "confidence": "untested",
                "notes": (
                    "MA Lighting Network Processing Units translate console "
                    "output to sACN and Art-Net for downstream gateways."
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
                    "Madrix is primarily an sACN / Art-Net source for LED "
                    "pixel-mapping, but its Aura and Plexus hardware bridges "
                    "accept sACN input and convert to DMX or play back shows."
                ),
            },
        ],
        "help": {
            "overview": (
                "sACN (Streaming ACN, ANSI E1.31) is the open ANSI "
                "standard for DMX512 over IP. Controllers send E1.31 "
                "Data Packets to nodes (sACN-to-DMX gateways) or "
                "directly to network-native fixtures. Each universe "
                "has its own multicast group (239.255.<hi>.<lo>), so "
                "traffic reaches only the receivers that joined it. "
                "The driver keeps a 512-channel buffer per universe, "
                "re-sends on a ~900 ms keep-alive cadence so a static "
                "look never times out, and stamps a per-source "
                "priority for multi-source merging."
            ),
            "setup": (
                "1. Put the sACN node/gateway on the same VLAN as the "
                "OpenAVC server and confirm its universe number "
                "(1..63999).\n"
                "2. Leave Addressing on 'Multicast' (the sACN default) "
                "unless the node is configured for unicast — multicast "
                "is derived automatically as 239.255.<hi>.<lo> from the "
                "universe, so nothing else to enter. For 'Unicast', "
                "fill in the node's IP as the Target Host.\n"
                "3. Set the Priority if the universe has more than one "
                "source (higher wins; default 100). Set a Source Name "
                "so the universe is identifiable on the network.\n"
                "4. Run set_channel / blackout / full_on. The driver "
                "starts sending and re-broadcasting automatically; no "
                "separate start is needed. On disconnect it sends a "
                "graceful stream-termination so receivers release the "
                "universe immediately."
            ),
        },
        "default_config": {
            "mode": "multicast",
            "host": "",
            "port": 5568,
            "universe": 1,
            "priority": 100,
            "source_name": "OpenAVC",
            "keepalive": True,
        },
        "config_schema": {
            "mode": {
                "type": "enum",
                "values": ["multicast", "unicast"],
                "default": "multicast",
                "label": "Addressing",
                "description": (
                    "Multicast (sACN default) derives the destination "
                    "239.255.<hi>.<lo> from the universe. Unicast sends "
                    "to the Target Host below."
                ),
            },
            "host": {
                "type": "string",
                "required": False,
                "label": "Target Host (unicast)",
                "description": (
                    "IP address of the sACN node — used only when "
                    "Addressing is Unicast. Leave blank for multicast."
                ),
            },
            "port": {
                "type": "integer",
                "default": 5568,
                "label": "UDP Port",
                "description": "sACN standard is 5568.",
            },
            "universe": {
                "type": "integer",
                "default": 1,
                "min": 1,
                "max": 63999,
                "label": "Universe",
                "description": (
                    "E1.31 universe number (1..63999). In multicast "
                    "mode this selects the 239.255.<hi>.<lo> group."
                ),
            },
            "priority": {
                "type": "integer",
                "default": 100,
                "min": 0,
                "max": 200,
                "label": "Priority",
                "description": (
                    "Per-source data priority (0..200, default 100). "
                    "When multiple sources drive one universe, the "
                    "receiver honors the highest priority."
                ),
            },
            "source_name": {
                "type": "string",
                "default": "OpenAVC",
                "label": "Source Name",
                "description": (
                    "Human-readable label carried in every packet so "
                    "the universe is identifiable on the network "
                    "(up to 63 characters)."
                ),
            },
            "keepalive": {
                "type": "boolean",
                "default": True,
                "label": "Keep-alive Re-send",
                "description": (
                    "Re-send the current buffer every ~900 ms per "
                    "E1.31 6.6.2 so a static look doesn't hit the "
                    "receiver's 2.5 s data-loss timeout. Disable for "
                    "one-shot updates."
                ),
            },
        },
        "state_variables": {
            "target_universe": {
                "type": "integer",
                "label": "Universe",
            },
            "destination": {
                "type": "string",
                "label": "Destination",
            },
            "priority": {
                "type": "integer",
                "label": "Priority",
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
                    "Sets one DMX channel and sends the updated buffer."
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
                            "``128,64,255,0``. Written to consecutive "
                            "channels starting at ``start_channel``."
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
                    "Set every channel in the buffer to 255. Useful "
                    "for fixture testing."
                ),
            },
            "set_universe": {
                "label": "Change Universe at Runtime",
                "params": {
                    "universe": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 63999,
                    },
                },
                "help": (
                    "Switch the target universe without reconnecting. "
                    "In multicast mode the destination group follows "
                    "the new universe; the buffer carries over."
                ),
            },
            "set_priority": {
                "label": "Set Priority",
                "params": {
                    "priority": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 200,
                        "label": "Priority (0..200)",
                    },
                },
                "help": (
                    "Change this source's E1.31 priority at runtime "
                    "and re-send. Higher priority wins on receivers "
                    "that merge multiple sources."
                ),
            },
            "send_now": {
                "label": "Send Current Buffer",
                "params": {},
                "help": (
                    "Force an immediate send of the current buffer, "
                    "independent of the keep-alive schedule."
                ),
            },
        },
        # Quick Action strip: the two whole-universe moves. Blackout is
        # panic-safe so it fires without a confirm; full-on drives every
        # fixture to maximum, so it asks first.
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
        # E1.31 6.7.2 sequence numbering spans the full 0..255 byte and
        # wraps; unlike Art-Net, 0 is a valid sequence value here.
        self._sequence = 0
        self._universe = 1
        self._priority = PRIORITY_DEFAULT
        self._mode = "multicast"
        self._host = ""
        self._port = ACN_SDT_MULTICAST_PORT
        self._source_name = "OpenAVC"
        self._cid = _derive_cid(device_id)
        self._keepalive_task: asyncio.Task | None = None
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        # Read the sending parameters up front so a bad combination is
        # caught before the socket opens.
        self._mode = str(self.config.get("mode", "multicast")).lower()
        self._host = str(self.config.get("host", "")).strip()
        self._port = int(self.config.get("port", ACN_SDT_MULTICAST_PORT))
        self._universe = self._clamp_universe(
            int(self.config.get("universe", 1))
        )
        self._priority = max(
            PRIORITY_MIN,
            min(PRIORITY_MAX, int(self.config.get("priority", PRIORITY_DEFAULT))),
        )
        self._source_name = str(self.config.get("source_name", "OpenAVC"))

        if self._mode == "unicast" and not self._host:
            raise ConnectionError(
                "sACN unicast mode requires a Target Host"
            )

    async def _create_transport(self, transport_type: str) -> None:
        # Built here rather than by the platform ladder: the destination
        # is computed per send (multicast group derived from the universe,
        # or the unicast host), so the socket has no fixed default target
        # and must open with broadcast sending allowed.
        self.transport = UDPTransport(
            on_disconnect=self._handle_transport_disconnect,
            name=self.device_id,
        )
        await self.transport.open(allow_broadcast=True)
        log.info(
            f"[{self.device_id}] sACN source bound "
            f"(universe {self._universe}, {self._destination_str()}, "
            f"priority {self._priority})"
        )

    async def _initial_sync(self) -> None:
        self.set_state("target_universe", self._universe)
        self.set_state("priority", self._priority)
        self.set_state("destination", self._destination_str())
        self.set_state("packets_sent", 0)
        self.set_state("blackout", True)  # buffer starts all-zero
        if self.config.get("keepalive", True):
            self._keepalive_task = asyncio.create_task(self._keepalive())

    async def _close_session(self) -> None:
        # Runs on every teardown path — stop the keep-alive re-send.
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

    async def disconnect(self) -> None:
        # Stop the keep-alive first so a routine re-send can't follow the
        # goodbye, then send the graceful stream termination while the
        # socket is still open (E1.31 6.2.6 / 12.2): three
        # Stream_Terminated packets so receivers release the universe now
        # instead of waiting out the 2.5 s data-loss timeout.
        await self._stop_keepalive()
        if self._connected and self.transport:
            await self._send_termination()
        await super().disconnect()

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
            self.set_state("last_value", values[-1] if values else 0)
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
            if not UNIVERSE_MIN <= new_u <= UNIVERSE_MAX:
                log.warning(
                    f"[{self.device_id}] Universe {new_u} out of range"
                )
                return
            self._universe = new_u
            self.set_state("target_universe", new_u)
            self.set_state("destination", self._destination_str())
            await self._send_buffer()
            return

        if command == "set_priority":
            new_p = int(params.get("priority", PRIORITY_DEFAULT))
            if not PRIORITY_MIN <= new_p <= PRIORITY_MAX:
                log.warning(
                    f"[{self.device_id}] Priority {new_p} out of range"
                )
                return
            self._priority = new_p
            self.set_state("priority", new_p)
            await self._send_buffer()
            return

        if command == "send_now":
            await self._send_buffer()
            return

        log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def _send_buffer(self, options: int = 0) -> None:
        if not self.transport or not self._connected:
            return
        packet = build_e131_data_packet(
            cid=self._cid,
            source_name=self._source_name,
            priority=self._priority,
            sequence=self._sequence,
            universe=self._universe,
            dmx_data=bytes(self._buffer),
            options=options,
        )
        host, port = self._destination()
        try:
            await self.transport.send_to(packet, host, port)
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] E1.31 send failed")
            return
        self._sequence = (self._sequence + 1) & 0xFF
        try:
            current = int(self.get_state("packets_sent") or 0)
        except (TypeError, ValueError):
            current = 0
        self.set_state("packets_sent", current + 1)

    async def _send_termination(self) -> None:
        """Send TERMINATION_PACKET_COUNT packets with the
        Stream_Terminated option bit set (E1.31 6.2.6 / 12.2)."""
        host, port = self._destination()
        for _ in range(TERMINATION_PACKET_COUNT):
            packet = build_e131_data_packet(
                cid=self._cid,
                source_name=self._source_name,
                priority=self._priority,
                sequence=self._sequence,
                universe=self._universe,
                dmx_data=bytes(self._buffer),
                options=OPT_STREAM_TERMINATED,
            )
            try:
                await self.transport.send_to(packet, host, port)
            except (ConnectionError, OSError):
                return
            self._sequence = (self._sequence + 1) & 0xFF

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
                f"[{self.device_id}] sACN keep-alive task crashed"
            )

    # ── Helpers ──

    @staticmethod
    def _clamp_universe(universe: int) -> int:
        return max(UNIVERSE_MIN, min(UNIVERSE_MAX, universe))

    def _destination(self) -> tuple[str, int]:
        """Resolve the (host, port) for the current universe/mode."""
        if self._mode == "unicast":
            return (self._host, self._port)
        return (universe_to_multicast(self._universe), self._port)

    def _destination_str(self) -> str:
        host, port = self._destination()
        return f"{host}:{port}"
