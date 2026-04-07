"""
Samsung MDC Display — Simulator

Full Samsung MDC (Multiple Display Control) binary protocol simulator with:
  - Power on/off with state tracking
  - Volume control (0-100)
  - Audio mute on/off
  - Input source switching (HDMI1, HDMI2, DP1, DVI1, VGA1, URL Launcher)
  - Status query returning all state at once
  - Proper MDC frame format with header (0xAA) and checksum

Frame format (request and response):
    [0xAA] [CMD] [ID] [LEN] [DATA...] [CHECKSUM]
    Checksum = sum of bytes after header (CMD+ID+LEN+DATA) & 0xFF

Response echoes the command byte back in the same frame format.
The driver's frame parser strips 0xAA and checksum, returning
[CMD, ID, LEN, DATA...] for processing.
"""

from simulator.tcp_simulator import TCPSimulator


# MDC command bytes (must match the driver)
CMD_STATUS = 0x00
CMD_POWER = 0x11
CMD_VOLUME = 0x12
CMD_MUTE = 0x13
CMD_INPUT = 0x14

# Input source codes
INPUT_MAP = {
    "hdmi1": 0x21,
    "hdmi2": 0x23,
    "dp1": 0x25,
    "dvi1": 0x18,
    "vga1": 0x14,
    "url_launcher": 0x63,
}
INPUT_REVERSE = {v: k for k, v in INPUT_MAP.items()}


def _checksum(data: bytes) -> int:
    """MDC checksum: sum of all bytes & 0xFF."""
    return sum(data) & 0xFF


class SamsungMdcSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "samsung_mdc",
        "name": "Samsung MDC Display Simulator",
        "category": "display",
        "transport": "tcp",
        "default_port": 1515,
        "initial_state": {
            "power": "off",
            "volume": 30,
            "mute": False,
            "input": "hdmi1",
        },
        "delays": {
            "command_response": 0.03,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Display stops responding to commands",
                "behavior": "no_response",
            },
            "corrupt_response": {
                "description": "Corrupted data on the wire",
                "behavior": "corrupt_response",
            },
        },
        "controls": [
            {
                "type": "power",
                "key": "power",
            },
            {
                "type": "select",
                "key": "input",
                "label": "Input Source",
                "options": ["hdmi1", "hdmi2", "dp1", "dvi1", "vga1", "url_launcher"],
                "labels": {
                    "hdmi1": "HDMI 1",
                    "hdmi2": "HDMI 2",
                    "dp1": "DisplayPort",
                    "dvi1": "DVI",
                    "vga1": "VGA",
                    "url_launcher": "URL Launcher",
                },
            },
            {
                "type": "slider",
                "key": "volume",
                "label": "Volume",
                "min": 0,
                "max": 100,
            },
            {
                "type": "toggle",
                "key": "mute",
                "label": "Audio Mute",
            },
        ],
    }

    def _build_ack(self, cmd: int, display_id: int, data: bytes = b"") -> bytes:
        """Build an MDC response frame.

        Format: [0xAA] [CMD] [ID] [LEN] [DATA...] [CHECKSUM]
        Checksum covers CMD+ID+LEN+DATA (everything after header).
        """
        payload = bytes([cmd, display_id, len(data)]) + data
        cs = _checksum(payload)
        return bytes([0xAA]) + payload + bytes([cs])

    def handle_command(self, data: bytes) -> bytes | None:
        """Parse incoming MDC binary frames and return ACK responses.

        The driver sends: [0xAA] [CMD] [ID] [LEN] [DATA...] [CS]
        We need to parse potentially multiple frames from one read.
        """
        response_buf = b""
        buf = data

        while len(buf) >= 4:
            # Find start marker
            start = -1
            for i in range(len(buf)):
                if buf[i] == 0xAA:
                    start = i
                    break
            if start == -1:
                break
            buf = buf[start:]

            if len(buf) < 4:
                break

            # Parse frame header
            cmd = buf[1]
            display_id = buf[2]
            data_len = buf[3]
            total_len = 4 + data_len + 1  # header + cmd + id + len + data + checksum

            if len(buf) < total_len:
                break

            # Extract payload (data bytes between length and checksum)
            payload = buf[4 : 4 + data_len]
            buf = buf[total_len:]

            # Process the command
            resp = self._process_command(cmd, display_id, payload)
            if resp:
                response_buf += resp

        return response_buf if response_buf else None

    def _process_command(
        self, cmd: int, display_id: int, payload: bytes
    ) -> bytes | None:
        """Process a single MDC command and return the ACK response."""

        # ── Status query (0x00) ──
        if cmd == CMD_STATUS:
            power_byte = 0x01 if self.state.get("power") == "on" else 0x00
            volume_byte = self.state.get("volume", 30)
            mute_byte = 0x01 if self.state.get("mute") else 0x00
            input_code = INPUT_MAP.get(self.state.get("input", "hdmi1"), 0x21)
            return self._build_ack(
                CMD_STATUS,
                display_id,
                bytes([power_byte, volume_byte, mute_byte, input_code]),
            )

        # ── Power (0x11) ──
        elif cmd == CMD_POWER:
            if len(payload) == 0:
                # Query
                power_byte = 0x01 if self.state.get("power") == "on" else 0x00
                return self._build_ack(CMD_POWER, display_id, bytes([power_byte]))
            else:
                # Set
                new_power = "on" if payload[0] == 0x01 else "off"
                self.set_state("power", new_power)
                power_byte = 0x01 if new_power == "on" else 0x00
                return self._build_ack(CMD_POWER, display_id, bytes([power_byte]))

        # ── Volume (0x12) ──
        elif cmd == CMD_VOLUME:
            if len(payload) == 0:
                # Query
                vol = self.state.get("volume", 30)
                return self._build_ack(CMD_VOLUME, display_id, bytes([vol]))
            else:
                # Set
                level = max(0, min(100, payload[0]))
                self.set_state("volume", level)
                return self._build_ack(CMD_VOLUME, display_id, bytes([level]))

        # ── Mute (0x13) ──
        elif cmd == CMD_MUTE:
            if len(payload) == 0:
                # Query
                mute_byte = 0x01 if self.state.get("mute") else 0x00
                return self._build_ack(CMD_MUTE, display_id, bytes([mute_byte]))
            else:
                # Set
                new_mute = payload[0] == 0x01
                self.set_state("mute", new_mute)
                mute_byte = 0x01 if new_mute else 0x00
                return self._build_ack(CMD_MUTE, display_id, bytes([mute_byte]))

        # ── Input (0x14) ──
        elif cmd == CMD_INPUT:
            if len(payload) == 0:
                # Query
                input_code = INPUT_MAP.get(self.state.get("input", "hdmi1"), 0x21)
                return self._build_ack(CMD_INPUT, display_id, bytes([input_code]))
            else:
                # Set
                input_name = INPUT_REVERSE.get(payload[0])
                if input_name:
                    self.set_state("input", input_name)
                    return self._build_ack(
                        CMD_INPUT, display_id, bytes([payload[0]])
                    )
                else:
                    # Unknown input code — still ACK with what was sent
                    return self._build_ack(
                        CMD_INPUT, display_id, bytes([payload[0]])
                    )

        return None
