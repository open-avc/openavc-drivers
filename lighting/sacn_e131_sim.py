"""
sACN / E1.31 DMX — Receiver Simulator.

Stands up a UDP listener on the sACN port (5568 by default) and
decodes incoming E1.31 Data Packets (ANSI E1.31-2018, Table 4-1) into
a per-universe 512-byte channel buffer that tests can inspect.

Like the Art-Net receiver, this is fire-and-forget — sACN sources do
not expect a response to a data packet, so the simulator never replies
to the controller. Its value is exposing what the source actually
sent: universe, priority, sequence, source name, DMX values, and
whether the source signalled a graceful stream termination.

State exposed (settable / inspectable from tests):
  - ``last_universe`` — universe of the most recently received frame.
  - ``last_priority`` — priority byte (E1.31 6.2.3) of the last frame.
  - ``last_sequence`` — sequence byte of the last frame.
  - ``last_source_name`` — decoded Source Name field.
  - ``last_options`` — Options byte of the last frame.
  - ``last_channel_count`` — DMX slot count (property values minus the
    START code) of the last frame.
  - ``frames_received`` — running count of valid data frames seen.
  - ``stream_terminated`` — True once a Stream_Terminated packet
    (Options bit 6) has been received.
  - ``last_packet_hex`` — short hex preview of the last packet.

Tests usually reach into ``sim.universes`` directly (universe -> the
512-byte buffer) to assert individual channel values.

Driver side: ``lighting/sacn_e131.py``.
"""

from __future__ import annotations

import logging

from openavc.simulator.udp_simulator import UDPSimulator

logger = logging.getLogger(__name__)


_ACN_PID = b"ASC-E1.17\x00\x00\x00"
_VECTOR_ROOT_E131_DATA = (0x00000004).to_bytes(4, "big")
_VECTOR_E131_DATA_PACKET = (0x00000002).to_bytes(4, "big")
_VECTOR_DMP_SET_PROPERTY = 0x02
_OPT_STREAM_TERMINATED = 0x40
# Fixed header length up to (and excluding) the first property value.
_HEADER_LEN = 126


class SacnE131Simulator(UDPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "sacn_e131",
        "name": "sACN / E1.31 DMX Receiver Simulator",
        "category": "lighting",
        "transport": "udp",
        "default_port": 5568,
        "initial_state": {
            "last_universe": 0,
            "last_priority": 0,
            "last_sequence": 0,
            "last_source_name": "",
            "last_options": 0,
            "last_channel_count": 0,
            "frames_received": 0,
            "stream_terminated": False,
            "last_packet_hex": "",
        },
        "controls": [
            {
                "type": "indicator",
                "key": "last_universe",
                "label": "Last Universe",
            },
            {
                "type": "indicator",
                "key": "last_priority",
                "label": "Last Priority",
            },
            {
                "type": "indicator",
                "key": "frames_received",
                "label": "Frames Received",
            },
            {
                "type": "indicator",
                "key": "stream_terminated",
                "label": "Stream Terminated",
            },
        ],
        "delays": {"command_response": 0.0},
        "error_modes": {
            "drop_packets": {
                "description": "Stop accepting incoming E1.31 data",
                "set_state": {"force_drop": True},
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Public for tests — universe (int) -> bytes(512).
        self.universes: dict[int, bytes] = {}

    def handle_command(self, data: bytes) -> bytes | None:
        # sACN is unidirectional from source to receiver; never respond.
        if self.state.get("force_drop"):
            return None
        self._decode_packet(data)
        return None

    def _decode_packet(self, data: bytes) -> None:
        if len(data) < _HEADER_LEN + 1:
            logger.debug(
                "%s: short packet (%d bytes), ignoring",
                self.name, len(data),
            )
            return
        # Root Layer validation.
        if data[0:2] != b"\x00\x10":  # Preamble Size 0x0010
            return
        if data[4:16] != _ACN_PID:
            logger.debug("%s: not an E1.31 packet (ACN PID mismatch)", self.name)
            return
        if data[18:22] != _VECTOR_ROOT_E131_DATA:
            # Could be VECTOR_ROOT_E131_EXTENDED (sync/discovery) — not in scope.
            return
        # Framing Layer validation.
        if data[40:44] != _VECTOR_E131_DATA_PACKET:
            return
        # DMP Layer validation.
        if data[117] != _VECTOR_DMP_SET_PROPERTY:
            return

        source_name = data[44:108].split(b"\x00", 1)[0].decode(
            "utf-8", errors="replace"
        )
        priority = data[108]
        sequence = data[111]
        options = data[112]
        universe = int.from_bytes(data[113:115], "big")
        property_value_count = int.from_bytes(data[123:125], "big")

        if property_value_count < 1:
            return
        slot_count = property_value_count - 1  # subtract the START code
        # Property values start at octet 125 (START code), slots follow.
        available = len(data) - _HEADER_LEN
        slot_count = min(slot_count, available, 512)
        slots = data[_HEADER_LEN : _HEADER_LEN + slot_count]

        # Pad / store as a full 512-byte buffer so callers can index any
        # channel without bounds-checking the original payload.
        buf = bytes(slots) + b"\x00" * (512 - len(slots))
        self.universes[universe] = buf

        self.set_state("last_universe", universe)
        self.set_state("last_priority", priority)
        self.set_state("last_sequence", sequence)
        self.set_state("last_source_name", source_name)
        self.set_state("last_options", options)
        self.set_state("last_channel_count", slot_count)
        self.set_state(
            "frames_received",
            int(self.state.get("frames_received", 0)) + 1,
        )
        if options & _OPT_STREAM_TERMINATED:
            self.set_state("stream_terminated", True)
        self.set_state("last_packet_hex", data[:24].hex())
