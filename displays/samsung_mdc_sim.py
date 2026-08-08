"""
Samsung MDC Display — Simulator

Full Samsung MDC (Multiple Display Control) binary protocol simulator with:
  - Multiple displays addressed by Set ID (the ``set_ids`` config, default a
    single display), each with its own state
  - Power on/off, volume, audio mute, input source switching
  - Picture settings: brightness, contrast, backlight (Manual Lamp), picture
    mode, color tone — each get/set with read-back
  - Status query returning power/volume/mute/input at once
  - Serial-number query (used by the discovery probe)
  - Proper MDC frame format with header (0xAA) and checksum

Request frame (controller -> display):
    [0xAA] [CMD] [ID] [LEN] [DATA...] [CHECKSUM]

Response frame (display -> controller):
    [0xAA] [0xFF] [ID] [LEN] [ACK/NAK] [r-CMD] [VALUES...] [CHECKSUM]

Every response carries 0xFF in the command position (not the original command),
an ACK (0x41 'A') or NAK (0x4E 'N') byte, then the echoed command and its
values. Checksum = sum of all bytes after the 0xAA header, masked to 0xFF.

The lowest Set ID is the "primary" display and is backed by the simulator's
``state`` so the auto-generated UI controls drive it; any additional Set IDs
are backed by an internal per-display map (wire-only, for exercising a driver's
multi-display roster). A request to a Set ID that isn't present gets no reply,
modelling an absent display.
"""

from openavc.simulator.tcp_simulator import TCPSimulator


# MDC command bytes (must match the driver)
CMD_STATUS = 0x00
CMD_SERIAL = 0x0B
CMD_POWER = 0x11
CMD_VOLUME = 0x12
CMD_MUTE = 0x13
CMD_INPUT = 0x14
CMD_CONTRAST = 0x24
CMD_BRIGHTNESS = 0x25
CMD_COLOR_TONE = 0x3E
CMD_BACKLIGHT = 0x58
CMD_PICTURE_MODE = 0x71

# Input source codes (full Samsung source set — must match the driver)
INPUT_MAP = {
    "hdmi1": 0x21,
    "hdmi2": 0x23,
    "hdmi3": 0x31,
    "hdmi4": 0x33,
    "dp1": 0x25,
    "dp2": 0x26,
    "dp3": 0x27,
    "dvi": 0x18,
    "pc": 0x14,
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

# Picture mode + color tone name<->byte maps (must match the driver's).
PICTURE_MODE_NAMES = {
    0x00: "dynamic",
    0x01: "standard",
    0x02: "movie",
    0x03: "custom_tv",
    0x04: "natural",
    0x05: "calibration_tv",
    0x10: "entertain",
    0x11: "internet",
    0x12: "text",
    0x13: "custom",
    0x14: "advertisement",
    0x15: "information",
    0x16: "calibration",
    0x20: "shop_mall_video",
    0x21: "shop_mall_text",
    0x22: "office_school_video",
    0x23: "office_school_text",
    0x24: "terminal_station_video",
    0x25: "terminal_station_text",
    0x26: "video_wall_video",
    0x27: "video_wall_text",
    0x30: "hdr_plus",
    0x50: "off",
}
PICTURE_MODE_BYTES = {v: k for k, v in PICTURE_MODE_NAMES.items()}

COLOR_TONE_NAMES = {
    0x00: "cool2",
    0x01: "cool1",
    0x02: "normal",
    0x03: "warm1",
    0x04: "warm2",
    0x50: "off",
}
COLOR_TONE_BYTES = {v: k for k, v in COLOR_TONE_NAMES.items()}

DEFAULT_DISPLAY = {
    "power": "off",
    "volume": 30,
    "mute": False,
    "input": "hdmi1",
    "brightness": 50,
    "contrast": 70,
    "backlight": 80,
    "picture_mode": "standard",
    "color_tone": "normal",
}


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
        "initial_state": dict(DEFAULT_DISPLAY),
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
            {"type": "power", "key": "power"},
            {
                "type": "select",
                "key": "input",
                "label": "Input Source",
                "options": [
                    "hdmi1", "hdmi2", "hdmi3", "hdmi4",
                    "dp1", "dvi", "pc", "hdbaset",
                    "magic_info_s", "url_launcher",
                ],
                "labels": {
                    "hdmi1": "HDMI 1",
                    "hdmi2": "HDMI 2",
                    "hdmi3": "HDMI 3",
                    "hdmi4": "HDMI 4",
                    "dp1": "DisplayPort",
                    "dvi": "DVI",
                    "pc": "PC (analog RGB)",
                    "hdbaset": "HDBaseT",
                    "magic_info_s": "MagicInfo S",
                    "url_launcher": "URL Launcher",
                },
            },
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 100},
            {"type": "toggle", "key": "mute", "label": "Audio Mute"},
            {"type": "slider", "key": "brightness", "label": "Brightness", "min": 0, "max": 100},
            {"type": "slider", "key": "contrast", "label": "Contrast", "min": 0, "max": 100},
            {"type": "slider", "key": "backlight", "label": "Backlight", "min": 0, "max": 100},
            {
                "type": "select",
                "key": "picture_mode",
                "label": "Picture Mode",
                "options": [
                    "standard", "dynamic", "movie", "natural",
                    "calibration", "information", "advertisement",
                    "video_wall_video",
                ],
            },
            {
                "type": "select",
                "key": "color_tone",
                "label": "Color Tone",
                "options": ["cool2", "cool1", "normal", "warm1", "warm2", "off"],
            },
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        present = self._parse_set_ids(self.config.get("set_ids", "1"))
        self._present_ids = present
        self._primary = present[0]
        # Non-primary displays live in an internal map; the primary is backed
        # by self.state so the UI controls it.
        self._displays: dict[int, dict] = {
            sid: dict(DEFAULT_DISPLAY) for sid in present if sid != self._primary
        }

    @staticmethod
    def _parse_set_ids(raw) -> list[int]:
        ids: list[int] = []
        for part in str(raw).replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                continue
            if 0 <= n <= 254 and n not in ids:
                ids.append(n)
        return sorted(ids) or [1]

    # ── Per-display state access ──

    def _get(self, set_id: int, field: str):
        if set_id == self._primary:
            return self.state.get(field, DEFAULT_DISPLAY.get(field))
        return self._displays.get(set_id, {}).get(field, DEFAULT_DISPLAY.get(field))

    def _put(self, set_id: int, field: str, value) -> None:
        if set_id == self._primary:
            self.set_state(field, value)
        else:
            self._displays.setdefault(set_id, dict(DEFAULT_DISPLAY))[field] = value

    def _build_ack(
        self, rcmd: int, display_id: int, data: bytes = b"", ack: bool = True
    ) -> bytes:
        """Build an MDC response frame.

        Format: [0xAA] [0xFF] [ID] [LEN] [ACK/NAK] [r-CMD] [DATA...] [CHECKSUM]
        The byte after the header is always 0xFF (response marker), followed by
        ACK (0x41 'A') or NAK (0x4E 'N'), then the echoed command and its data.
        LEN counts the ACK/NAK, r-CMD, and data bytes. Checksum covers every
        byte after the header.
        """
        body = bytes([0x41 if ack else 0x4E, rcmd]) + data
        payload = bytes([0xFF, display_id, len(body)]) + body
        cs = _checksum(payload)
        return bytes([0xAA]) + payload + bytes([cs])

    def handle_command(self, data: bytes) -> bytes | None:
        """Parse incoming MDC binary frames and return ACK responses.

        The driver sends: [0xAA] [CMD] [ID] [LEN] [DATA...] [CS]
        We may see multiple frames from one read.
        """
        response_buf = b""
        buf = data

        while len(buf) >= 4:
            start = buf.find(0xAA)
            if start == -1:
                break
            buf = buf[start:]
            if len(buf) < 4:
                break

            cmd = buf[1]
            display_id = buf[2]
            data_len = buf[3]
            total_len = 4 + data_len + 1  # header + cmd + id + len + data + checksum
            if len(buf) < total_len:
                break

            payload = buf[4 : 4 + data_len]
            buf = buf[total_len:]

            resp = self._process_command(cmd, display_id, payload)
            if resp:
                response_buf += resp

        return response_buf if response_buf else None

    def _process_command(
        self, cmd: int, display_id: int, payload: bytes
    ) -> bytes | None:
        """Process a single MDC command and return the ACK response, or None
        if the addressed display isn't present (an absent display is silent)."""
        if display_id not in self._present_ids:
            return None

        # ── Serial number (0x0B) — used by the discovery probe ──
        if cmd == CMD_SERIAL:
            return self._build_ack(
                CMD_SERIAL, display_id, b"SIMULATED0000001"
            )

        # ── Status query (0x00) ──
        if cmd == CMD_STATUS:
            return self._build_ack(
                CMD_STATUS,
                display_id,
                bytes(
                    [
                        0x01 if self._get(display_id, "power") == "on" else 0x00,
                        int(self._get(display_id, "volume")),
                        0x01 if self._get(display_id, "mute") else 0x00,
                        INPUT_MAP.get(self._get(display_id, "input"), 0x21),
                        0x01,  # picture aspect
                        0x00,  # legacy timer byte
                        0x00,  # legacy timer byte
                    ]
                ),
            )

        # ── Power (0x11) ──
        if cmd == CMD_POWER:
            if payload:
                self._put(display_id, "power", "on" if payload[0] == 0x01 else "off")
            byte = 0x01 if self._get(display_id, "power") == "on" else 0x00
            return self._build_ack(CMD_POWER, display_id, bytes([byte]))

        # ── Volume (0x12) ──
        if cmd == CMD_VOLUME:
            if payload:
                self._put(display_id, "volume", max(0, min(100, payload[0])))
            return self._build_ack(
                CMD_VOLUME, display_id, bytes([int(self._get(display_id, "volume"))])
            )

        # ── Mute (0x13) ──
        if cmd == CMD_MUTE:
            if payload:
                self._put(display_id, "mute", payload[0] == 0x01)
            byte = 0x01 if self._get(display_id, "mute") else 0x00
            return self._build_ack(CMD_MUTE, display_id, bytes([byte]))

        # ── Input (0x14) ──
        if cmd == CMD_INPUT:
            if payload:
                name = INPUT_REVERSE.get(payload[0])
                if name:
                    self._put(display_id, "input", name)
                return self._build_ack(CMD_INPUT, display_id, bytes([payload[0]]))
            code = INPUT_MAP.get(self._get(display_id, "input"), 0x21)
            return self._build_ack(CMD_INPUT, display_id, bytes([code]))

        # ── Contrast / Brightness / Backlight (0-100 ints) ──
        if cmd in (CMD_CONTRAST, CMD_BRIGHTNESS, CMD_BACKLIGHT):
            field = {
                CMD_CONTRAST: "contrast",
                CMD_BRIGHTNESS: "brightness",
                CMD_BACKLIGHT: "backlight",
            }[cmd]
            if payload:
                self._put(display_id, field, max(0, min(100, payload[0])))
            return self._build_ack(
                cmd, display_id, bytes([int(self._get(display_id, field))])
            )

        # ── Color tone (0x3E) ──
        if cmd == CMD_COLOR_TONE:
            if payload:
                name = COLOR_TONE_NAMES.get(payload[0])
                if name:
                    self._put(display_id, "color_tone", name)
                return self._build_ack(CMD_COLOR_TONE, display_id, bytes([payload[0]]))
            byte = COLOR_TONE_BYTES.get(self._get(display_id, "color_tone"), 0x02)
            return self._build_ack(CMD_COLOR_TONE, display_id, bytes([byte]))

        # ── Picture mode (0x71) ──
        if cmd == CMD_PICTURE_MODE:
            if payload:
                name = PICTURE_MODE_NAMES.get(payload[0])
                if name:
                    self._put(display_id, "picture_mode", name)
                return self._build_ack(CMD_PICTURE_MODE, display_id, bytes([payload[0]]))
            byte = PICTURE_MODE_BYTES.get(self._get(display_id, "picture_mode"), 0x01)
            return self._build_ack(CMD_PICTURE_MODE, display_id, bytes([byte]))

        return None
