"""
Art-Net DMX — Receiver Simulator.

Stands up a UDP listener on the Art-Net port (6454 by default) and
decodes incoming ArtDmx packets (OpCode 0x5000) into a per-universe
512-byte channel buffer that tests can inspect.

The receiver is fire-and-forget — Art-Net controllers don't expect
responses to ArtDmx packets, so this simulator never replies to the
controller. The simulator's value is in exposing what the controller
actually sent.

State exposed (settable / inspectable from tests):
  - ``last_universe`` — Port-Address of the most recently received frame.
  - ``last_sequence`` — sequence byte of the last frame.
  - ``last_channel_count`` — DMX payload length of the last frame.
  - ``frames_received`` — running count of valid frames seen.
  - ``last_packet_hex`` — short hex preview of the last packet for
    debugging.
  - ``buffers`` — dict of universe -> bytes(512). Tests usually
    reach into ``sim.universes`` directly to assert channel values.

Driver side: ``lighting/artnet_dmx.py``.
"""

from __future__ import annotations

import logging

from simulator.udp_simulator import UDPSimulator

logger = logging.getLogger(__name__)


_HEADER_ID = b"Art-Net\x00"
_OP_DMX_LE = (0x5000).to_bytes(2, "little")
_HEADER_LEN = 18  # 8 + 2 + 2 + 1 + 1 + 1 + 1 + 2


class ArtnetDmxSimulator(UDPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "artnet_dmx",
        "name": "Art-Net DMX Receiver Simulator",
        "category": "lighting",
        "transport": "udp",
        "default_port": 6454,
        "initial_state": {
            "last_universe": 0,
            "last_sequence": 0,
            "last_channel_count": 0,
            "frames_received": 0,
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
                "key": "last_sequence",
                "label": "Last Sequence",
            },
            {
                "type": "indicator",
                "key": "frames_received",
                "label": "Frames Received",
            },
        ],
        "delays": {"command_response": 0.0},
        "error_modes": {
            "drop_packets": {
                "description": "Stop accepting incoming ArtDmx",
                "set_state": {"force_drop": True},
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Public for tests — universe (int) -> bytes(512).
        self.universes: dict[int, bytes] = {}

    def handle_command(self, data: bytes) -> bytes | None:
        # Art-Net is unidirectional from controller to node; never
        # respond. We just decode and update inspection state.
        if self.state.get("force_drop"):
            return None
        self._decode_packet(data)
        return None

    def _decode_packet(self, data: bytes) -> None:
        if len(data) < _HEADER_LEN:
            logger.debug(
                "%s: short packet (%d bytes), ignoring",
                self.name, len(data),
            )
            return
        if data[:8] != _HEADER_ID:
            logger.debug(
                "%s: not an Art-Net packet (header mismatch)",
                self.name,
            )
            return
        if data[8:10] != _OP_DMX_LE:
            # Could be ArtPoll / ArtSync / etc. — not in scope here.
            return

        # Bytes 10-11: ProtVer (big-endian); 12: sequence;
        # 13: physical; 14: SubUni; 15: Net;
        # 16-17: length (big-endian).
        sequence = data[12]
        sub_uni = data[14]
        net = data[15]
        length = int.from_bytes(data[16:18], "big")

        if length < 2 or length > 512 or len(data) < _HEADER_LEN + length:
            logger.debug(
                "%s: bad ArtDmx length field %d (data %d bytes)",
                self.name, length, len(data),
            )
            return

        universe = (net << 8) | sub_uni
        payload = data[_HEADER_LEN : _HEADER_LEN + length]

        # Pad / store as a full 512-byte buffer so callers can index
        # any channel without bounds-checking the original payload.
        if length < 512:
            buf = bytes(payload) + b"\x00" * (512 - length)
        else:
            buf = bytes(payload)
        self.universes[universe] = buf

        self.set_state("last_universe", universe)
        self.set_state("last_sequence", sequence)
        self.set_state("last_channel_count", length)
        self.set_state(
            "frames_received",
            int(self.state.get("frames_received", 0)) + 1,
        )
        self.set_state(
            "last_packet_hex",
            data[:24].hex(),
        )
