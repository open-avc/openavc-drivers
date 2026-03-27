"""
OpenAVC Samsung MDC (Multiple Display Control) Driver.

Controls Samsung commercial displays via TCP using the MDC binary protocol.
Default port: 1515. Each frame has a header (0xAA), command byte, display ID,
data length, data bytes, and a checksum (sum of bytes after header, masked 0xFF).

Frame format:
    [0xAA] [CMD] [ID] [LEN] [DATA...] [CHECKSUM]

This driver validates the frame parser + checksum infrastructure from Phase 3.
"""

from __future__ import annotations

from typing import Any, Optional

from server.drivers.base import BaseDriver
from server.transport.binary_helpers import checksum_sum
from server.transport.frame_parsers import CallableFrameParser, FrameParser
from server.utils.logger import get_logger

log = get_logger(__name__)

# MDC command bytes
CMD_POWER = 0x11
CMD_VOLUME = 0x12
CMD_MUTE = 0x13
CMD_INPUT = 0x14
CMD_STATUS = 0x00

# MDC input source codes
INPUT_MAP = {
    "hdmi1": 0x21,
    "hdmi2": 0x23,
    "dp1": 0x25,
    "dvi1": 0x18,
    "vga1": 0x14,
    "url_launcher": 0x63,
}
INPUT_REVERSE = {v: k for k, v in INPUT_MAP.items()}


def _build_mdc_frame(cmd: int, display_id: int, data: bytes = b"") -> bytes:
    """Build a Samsung MDC frame with header and checksum."""
    frame = bytes([cmd, display_id, len(data)]) + data
    cs = checksum_sum(frame)
    return bytes([0xAA]) + frame + bytes([cs])


def _parse_mdc_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """
    Parse a Samsung MDC frame from a byte buffer.

    Returns (frame_bytes, remaining_buffer) or (None, buffer) if incomplete.
    A valid frame is returned WITHOUT the 0xAA header and checksum — just
    the cmd, id, length, and data portion.
    """
    # Find the start marker
    start = buffer.find(0xAA)
    if start == -1:
        return None, b""  # No valid data, discard
    if start > 0:
        buffer = buffer[start:]  # Skip garbage before header

    # Need at least: header(1) + cmd(1) + id(1) + len(1) = 4 bytes
    if len(buffer) < 4:
        return None, buffer

    data_len = buffer[3]
    total_len = 4 + data_len + 1  # header + cmd + id + len + data + checksum

    if len(buffer) < total_len:
        return None, buffer

    frame = buffer[1 : total_len - 1]  # Exclude header and checksum
    remaining = buffer[total_len:]
    return frame, remaining


class SamsungMDCDriver(BaseDriver):
    """Samsung MDC binary protocol driver for commercial displays."""

    DRIVER_INFO = {
        "id": "samsung_mdc",
        "name": "Samsung MDC Display",
        "manufacturer": "Samsung",
        "category": "display",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Samsung commercial displays via the MDC (Multiple "
            "Display Control) binary protocol over TCP."
        ),
        "transport": "tcp",
        "help": {
            "overview": (
                "Controls Samsung commercial displays using the MDC binary protocol. "
                "Covers Smart Signage, The Wall, LED, and SMART Signage Platform series."
            ),
            "setup": (
                "1. Connect the display to the network\n"
                "2. Enable MDC protocol in the display's network settings\n"
                "3. Default port is 1515\n"
                "4. Set the Display ID to match the display's configuration (default 1)"
            ),
        },
        "discovery": {
            "ports": [1515],
            "mac_prefixes": [
                "00:07:ab",  # Samsung Electronics
                "00:e0:64",  # Samsung Electronics
                "14:49:e0",  # Samsung Electronics
                "34:c3:d2",  # Samsung Electronics
                "64:b5:c6",  # Samsung Electronics
                "8c:71:f8",  # Samsung Electronics
                "b4:79:a7",  # Samsung Electronics
                "d0:03:4b",  # Samsung Electronics
            ],
        },
        "default_config": {
            "host": "",
            "port": 1515,
            "display_id": 1,
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 1515, "label": "Port"},
            "display_id": {
                "type": "integer",
                "default": 1,
                "min": 0,
                "max": 254,
                "label": "Display ID",
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["off", "on"],
                "label": "Power State",
            },
            "volume": {"type": "integer", "label": "Volume"},
            "mute": {"type": "boolean", "label": "Mute"},
            "input": {
                "type": "enum",
                "values": list(INPUT_MAP.keys()),
                "label": "Input Source",
            },
        },
        "commands": {
            "power_on": {"label": "Power On", "params": {}, "help": "Turn on the display."},
            "power_off": {"label": "Power Off", "params": {}, "help": "Turn off the display (standby)."},
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Volume level 0-100",
                    },
                },
                "help": "Set the display speaker volume.",
            },
            "mute_on": {"label": "Mute On", "params": {}, "help": "Mute the display audio."},
            "mute_off": {"label": "Mute Off", "params": {}, "help": "Unmute the display audio."},
            "set_input": {
                "label": "Set Input",
                "params": {
                    "input": {
                        "type": "enum",
                        "values": list(INPUT_MAP.keys()),
                        "required": True,
                        "help": "Input source to switch to",
                    },
                },
                "help": "Switch the display input source.",
            },
        },
    }

    @property
    def _display_id(self) -> int:
        return self.config.get("display_id", 1)

    def _create_frame_parser(self) -> Optional[FrameParser]:
        """Use callable parser for MDC binary framing."""
        return CallableFrameParser(_parse_mdc_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        """MDC uses binary framing, not delimiters."""
        return None

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a named command to the display."""
        params = params or {}

        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match command:
            case "power_on":
                frame = _build_mdc_frame(CMD_POWER, self._display_id, bytes([1]))
                await self.transport.send(frame)
            case "power_off":
                frame = _build_mdc_frame(CMD_POWER, self._display_id, bytes([0]))
                await self.transport.send(frame)
            case "set_volume":
                level = int(params.get("level", 0))
                level = max(0, min(100, level))
                frame = _build_mdc_frame(CMD_VOLUME, self._display_id, bytes([level]))
                await self.transport.send(frame)
            case "mute_on":
                frame = _build_mdc_frame(CMD_MUTE, self._display_id, bytes([1]))
                await self.transport.send(frame)
            case "mute_off":
                frame = _build_mdc_frame(CMD_MUTE, self._display_id, bytes([0]))
                await self.transport.send(frame)
            case "set_input":
                input_name = params.get("input", "")
                input_code = INPUT_MAP.get(input_name)
                if input_code is not None:
                    frame = _build_mdc_frame(CMD_INPUT, self._display_id, bytes([input_code]))
                    await self.transport.send(frame)
                else:
                    log.warning(f"[{self.device_id}] Unknown input: {input_name}")
            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

        log.debug(f"[{self.device_id}] Sent command: {command} {params}")

    async def on_data_received(self, data: bytes) -> None:
        """Parse MDC response frames and update state."""
        if len(data) < 3:
            return

        cmd = data[0]
        # data[1] = display_id (ACK responses use 0xFF + cmd format)
        payload = data[3:] if len(data) > 3 else b""

        # Check for ACK (0xFF prefix in response)
        ack_cmd = data[0]

        if ack_cmd == CMD_POWER and payload:
            self.set_state("power", "on" if payload[0] else "off")
        elif ack_cmd == CMD_VOLUME and payload:
            self.set_state("volume", payload[0])
        elif ack_cmd == CMD_MUTE and payload:
            self.set_state("mute", bool(payload[0]))
        elif ack_cmd == CMD_INPUT and payload:
            input_name = INPUT_REVERSE.get(payload[0], f"unknown_{payload[0]:02x}")
            self.set_state("input", input_name)
        elif ack_cmd == CMD_STATUS and len(payload) >= 3:
            # Status response: [power, volume, mute, input, ...]
            self.set_state("power", "on" if payload[0] else "off")
            self.set_state("volume", payload[1])
            self.set_state("mute", bool(payload[2]))

    async def poll(self) -> None:
        """Query display status."""
        if not self.transport or not self.transport.connected:
            return
        try:
            frame = _build_mdc_frame(CMD_STATUS, self._display_id)
            await self.transport.send(frame)
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")
