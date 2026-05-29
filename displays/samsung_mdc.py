"""
OpenAVC Samsung MDC (Multiple Display Control) Driver.

Controls Samsung commercial displays via TCP using the MDC binary protocol.
Default port: 1515.

Request frame (host -> display):
    [0xAA] [CMD] [ID] [LEN] [DATA...] [CHECKSUM]

Response frame (display -> host):
    [0xAA] [0xFF] [ID] [LEN] [ACK/NAK] [r-CMD] [VALUES...] [CHECKSUM]

In a response the byte after the 0xAA header is always 0xFF (not the command).
ACK is 0x41 ('A') and NAK is 0x4E ('N'); r-CMD echoes the command being
answered, and the value bytes follow it. The checksum is the sum of every byte
after the header, masked to 0xFF. The frame parser strips the 0xAA header and
the trailing checksum before handing the body to on_data_received().
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

# Response framing: every response uses 0xFF in the command position, followed
# by an ACK ('A') or NAK ('N') byte before the echoed command.
RESPONSE_CMD = 0xFF
ACK = 0x41  # 'A'
NAK = 0x4E  # 'N'

# MDC input source codes (full Samsung source set). Ordered with the inputs an
# integrator switches most often first; the rest cover PC-mode variants and the
# display's internal/platform sources so status read-back always resolves.
INPUT_MAP = {
    "hdmi1": 0x21,
    "hdmi2": 0x23,
    "hdmi3": 0x31,
    "hdmi4": 0x33,
    "dp1": 0x25,
    "dp2": 0x26,
    "dp3": 0x27,
    "dvi": 0x18,
    "pc": 0x14,  # analog RGB (D-sub / "PC")
    "hdbaset": 0x55,
    "component": 0x08,
    "av": 0x0C,
    "av2": 0x0D,
    "s_video": 0x04,
    "scart1": 0x0E,
    "bnc": 0x1E,
    "rf_tv": 0x30,
    "tv_dtv": 0x40,
    "hdmi1_pc": 0x22,
    "hdmi2_pc": 0x24,
    "hdmi3_pc": 0x32,
    "hdmi4_pc": 0x34,
    "dvi_video": 0x1F,
    "magic_info": 0x20,
    "magic_info_s": 0x60,
    "url_launcher": 0x63,
    "web_browser": 0x65,
    "internal_usb": 0x62,
    "widi": 0x61,
    "iwb": 0x64,
    "remote_workspace": 0x66,
    "ocm": 0x56,
    "plug_in_mode": 0x50,
    "none": 0x00,
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
        "version": "1.4.0",
        "author": "OpenAVC",
        "description": (
            "Controls Samsung commercial displays via the MDC (Multiple "
            "Display Control) binary protocol over TCP."
        ),
        "source_url": "https://github.com/vgavro/samsung-mdc",
        "tags": ["display", "signage", "mdc"],
        "verified": False,
        "simulated": True,
        "protocols": ["samsung_mdc"],
        "ports": [1515],
        "compatible_models": [
            {
                "manufacturer": "Samsung",
                "models": [
                    "Smart Signage series",
                    "The Wall series",
                    "LED Commercial series",
                    "SMART Signage Platform",
                ],
                "confidence": "untested",
            },
        ],
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
            # Samsung MDC is binary on TCP/1515. Get-Serial-Number
            # (AA 0B 01 00 0C) elicits a fixed-prefix ACK starting with
            # AA FF on any MDC-speaking display, regardless of model.
            "tcp_probe": {
                "port": 1515,
                "send_hex": "AA0B01000C",
                "expect_hex": "AAFF",
                "extract_manufacturer": "Samsung",
            },
            "oui": [
                "00:07:ab",
                "00:e0:64",
                "14:49:e0",
                "34:c3:d2",
                "64:b5:c6",
                "8c:71:f8",
                "b4:79:a7",
                "d0:03:4b",
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

    async def connect(self) -> None:
        """Connect and immediately query status."""
        await super().connect()
        await self.poll()

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
        """Parse MDC response frames and update state.

        The frame parser has already stripped the 0xAA header and trailing
        checksum, so ``data`` is the response body::

            [0xFF] [display_id] [len] [ACK/NAK] [r-CMD] [values...]

        Values are read by the echoed command (r-CMD), not by the leading
        byte — that is always 0xFF on a response.
        """
        # Need at least: response marker, id, len, ack/nak, r-cmd
        if len(data) < 5 or data[0] != RESPONSE_CMD:
            return

        ack = data[3]
        rcmd = data[4]
        values = data[5:]

        if ack == NAK:
            log.warning(f"[{self.device_id}] Display returned NAK for command 0x{rcmd:02x}")
            return
        if ack != ACK:
            return

        if rcmd == CMD_POWER and values:
            self.set_state("power", "on" if values[0] else "off")
        elif rcmd == CMD_VOLUME and values:
            self.set_state("volume", values[0])
        elif rcmd == CMD_MUTE and values:
            self.set_state("mute", bool(values[0]))
        elif rcmd == CMD_INPUT and values:
            self.set_state("input", INPUT_REVERSE.get(values[0], f"unknown_{values[0]:02x}"))
        elif rcmd == CMD_STATUS and len(values) >= 3:
            # Status values: [power, volume, mute, input, aspect, n_time, f_time]
            self.set_state("power", "on" if values[0] else "off")
            self.set_state("volume", values[1])
            self.set_state("mute", bool(values[2]))
            if len(values) >= 4:
                self.set_state("input", INPUT_REVERSE.get(values[3], f"unknown_{values[3]:02x}"))

    async def poll(self) -> None:
        """Query display status."""
        if not self.transport or not self.transport.connected:
            return
        try:
            frame = _build_mdc_frame(CMD_STATUS, self._display_id)
            await self.transport.send(frame)
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")
