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

Multi-display (Set ID) model:
    MDC is designed to address several daisy-chained displays over one
    connection, each carrying a unique Set ID in the request's ID byte; every
    response echoes the Set ID that answered. This driver models each Set ID as
    a ``display`` child entity, so a video wall or signage chain surfaces one
    controllable unit per display (power / volume / mute / input plus the
    picture settings brightness, contrast, backlight, picture mode and color
    tone) instead of collapsing to a single device. The roster is declared in
    the ``display_ids`` config (the Set IDs an installer assigned when
    commissioning the chain); a single display is simply a chain of one.

    Per-display picture values live as writable child props (set by a command
    that names the display, read back on the next poll) rather than
    device_settings — a device setting's flat state_key can't address a child.
    Same reasoning as the RackLink PDU's per-outlet state.
"""

from __future__ import annotations

from typing import Any, Optional

from server.drivers.base import BaseDriver
from server.transport.binary_helpers import checksum_sum
from server.transport.frame_parsers import CallableFrameParser, FrameParser
from server.utils.logger import get_logger

log = get_logger(__name__)

# MDC command bytes
CMD_STATUS = 0x00
CMD_POWER = 0x11
CMD_VOLUME = 0x12
CMD_MUTE = 0x13
CMD_INPUT = 0x14
CMD_CONTRAST = 0x24
CMD_BRIGHTNESS = 0x25
CMD_COLOR_TONE = 0x3E
CMD_BACKLIGHT = 0x58  # "Manual Lamp" — panel backlight level 0-100
CMD_PICTURE_MODE = 0x71

# Response framing: every response uses 0xFF in the command position, followed
# by an ACK ('A') or NAK ('N') byte before the echoed command.
RESPONSE_CMD = 0xFF
ACK = 0x41  # 'A'
NAK = 0x4E  # 'N'

# Valid Set ID range on the wire (the ID byte). 0xFF is reserved for broadcast
# and is not an addressable unit.
SET_ID_MIN = 0
SET_ID_MAX = 254

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

# Picture Mode (cmd 0x71). Full read-back map (every documented mode byte ->
# a friendly name) so a polled value always resolves. The settable subset is
# the modes common across the signage / commercial lines — the display NAKs any
# mode a given model doesn't offer.
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
PICTURE_MODE_SET = {
    "standard": 0x01,
    "dynamic": 0x00,
    "movie": 0x02,
    "natural": 0x04,
    "calibration": 0x16,
    "information": 0x15,
    "advertisement": 0x14,
    "video_wall_video": 0x26,
}

# Color Tone (cmd 0x3E) — the Cool/Normal/Warm preset shown as "Color Tone" in
# the on-screen menu. A closed set, so it round-trips as an enum both ways.
# (The absolute Color Temperature Kelvin value, cmd 0x3F, has model-varying
# ranges; the preset is model-consistent and matches the menu wording.)
COLOR_TONE_NAMES = {
    0x00: "cool2",
    0x01: "cool1",
    0x02: "normal",
    0x03: "warm1",
    0x04: "warm2",
    0x50: "off",
}
COLOR_TONE_SET = {v: k for k, v in COLOR_TONE_NAMES.items()}


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


def _child_state_vars() -> dict[str, dict[str, Any]]:
    """State variables for one ``display`` child entity.

    Power / volume / mute / input are the operationally-hot values (high cloud
    tier); the picture settings change rarely (low tier).
    """
    return {
        "power": {
            "type": "enum",
            "values": ["off", "on"],
            "label": "Power",
            "cloud_priority": "high",
        },
        "volume": {"type": "integer", "label": "Volume", "cloud_priority": "high"},
        "mute": {"type": "boolean", "label": "Mute", "cloud_priority": "high"},
        "input": {
            "type": "enum",
            "values": list(INPUT_MAP.keys()),
            "label": "Input Source",
            "cloud_priority": "high",
        },
        "brightness": {
            "type": "integer",
            "label": "Brightness",
            "cloud_priority": "low",
        },
        "contrast": {"type": "integer", "label": "Contrast", "cloud_priority": "low"},
        "backlight": {"type": "integer", "label": "Backlight", "cloud_priority": "low"},
        "picture_mode": {
            "type": "string",
            "label": "Picture Mode",
            "cloud_priority": "low",
        },
        "color_tone": {
            "type": "enum",
            "values": list(COLOR_TONE_SET.keys()),
            "label": "Color Tone",
            "cloud_priority": "low",
        },
    }


class SamsungMDCDriver(BaseDriver):
    """Samsung MDC binary protocol driver for commercial displays."""

    DRIVER_INFO = {
        "id": "samsung_mdc",
        "name": "Samsung MDC Display",
        "manufacturer": "Samsung",
        "category": "display",
        "version": "1.5.1",
        "author": "OpenAVC",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "description": (
            "Controls Samsung commercial displays via the MDC (Multiple "
            "Display Control) binary protocol over TCP. Each Set ID on the "
            "chain is a display child entity with power, volume, mute, input, "
            "and picture settings (brightness, contrast, backlight, picture "
            "mode, color tone)."
        ),
        "source_url": "https://github.com/vgavro/samsung-mdc",
        "tags": ["display", "signage", "mdc", "video-wall"],
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
                "Controls Samsung commercial displays using the MDC binary "
                "protocol. Covers Smart Signage, The Wall, LED, and SMART "
                "Signage Platform series. One connection can address several "
                "displays daisy-chained by Set ID — each appears as a display "
                "child entity."
            ),
            "setup": (
                "1. Connect the display (or the chain's master) to the "
                "network.\n"
                "2. Enable MDC / network control in the display's settings.\n"
                "3. Default port is 1515.\n"
                "4. In the display's menu, give each display a unique Set ID "
                "(ID). List those IDs in 'Display Set IDs' — e.g. 1 for a "
                "single display, or 1,2,3,4 for a four-panel wall.\n"
                "5. Route commands to a display by picking it from the Display "
                "dropdown; use All Displays On / Off to drive the whole chain "
                "at once."
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
            "display_ids": "1",
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 1515, "label": "Port"},
            "display_ids": {
                "type": "string",
                "default": "1",
                "label": "Display Set IDs",
                "description": (
                    "Comma-separated MDC Set IDs on the chain (0-254). One ID "
                    "for a single display; list every ID for a daisy-chained "
                    "wall, e.g. 1,2,3,4. Each becomes a display child entity."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "display_count": {"type": "integer", "label": "Displays"},
        },
        "child_entity_types": {
            "display": {
                "label": "Display",
                "label_plural": "Displays",
                "id_format": {
                    "type": "integer",
                    "min": SET_ID_MIN,
                    "max": SET_ID_MAX,
                    "pad_width": 3,
                },
                "state_variables": _child_state_vars(),
                "summary_fields": ["power", "input", "volume"],
                "label_field": "label",
            },
        },
        "quick_actions": ["all_on", "all_off", "refresh"],
        "actions": [
            {"id": "all_on", "kind": "command", "icon": "power"},
            {"id": "all_off", "kind": "command", "icon": "power-off"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
        ],
        "commands": {
            "power_on": {
                "label": "Power On",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                },
                "help": "Turn on a display.",
            },
            "power_off": {
                "label": "Power Off",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                },
                "help": "Turn off a display (standby).",
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Volume level 0-100",
                    },
                },
                "help": "Set a display's speaker volume.",
            },
            "mute_on": {
                "label": "Mute On",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                },
                "help": "Mute a display's audio.",
            },
            "mute_off": {
                "label": "Mute Off",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                },
                "help": "Unmute a display's audio.",
            },
            "set_input": {
                "label": "Set Input",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "input": {
                        "type": "enum",
                        "values": list(INPUT_MAP.keys()),
                        "required": True,
                        "help": "Input source to switch to",
                    },
                },
                "help": "Switch a display's input source.",
            },
            "set_brightness": {
                "label": "Set Brightness",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Brightness 0-100",
                    },
                },
                "help": "Set a display's picture brightness (0-100).",
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Contrast 0-100",
                    },
                },
                "help": "Set a display's picture contrast (0-100).",
            },
            "set_backlight": {
                "label": "Set Backlight",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Backlight 0-100",
                    },
                },
                "help": (
                    "Set a display's backlight / panel brightness (0-100). "
                    "Some models only accept this when Eco / auto-brightness "
                    "is off."
                ),
            },
            "set_picture_mode": {
                "label": "Set Picture Mode",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "mode": {
                        "type": "enum",
                        "values": list(PICTURE_MODE_SET.keys()),
                        "required": True,
                        "help": "Picture preset",
                    },
                },
                "help": (
                    "Set a display's picture mode. Not every model offers "
                    "every preset — the display rejects an unsupported one."
                ),
            },
            "set_color_tone": {
                "label": "Set Color Tone",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "tone": {
                        "type": "enum",
                        "values": list(COLOR_TONE_SET.keys()),
                        "required": True,
                        "help": "Color tone preset (cool to warm)",
                    },
                },
                "help": "Set a display's color tone preset (Cool 2 through Warm 2).",
            },
            "all_on": {
                "label": "All Displays On",
                "params": {},
                "help": "Turn on every display on the chain.",
            },
            "all_off": {
                "label": "All Displays Off",
                "params": {},
                "help": "Turn off (standby) every display on the chain.",
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Re-query every display's status.",
            },
        },
    }

    # ── Roster ──

    def _parse_display_ids(self) -> list[int]:
        """Set IDs declared in config, de-duplicated and range-checked.

        Accepts commas or semicolons; ignores blanks and out-of-range values.
        Falls back to [1] so a misconfigured device still exposes one display.
        """
        raw = str(self.config.get("display_ids", "1"))
        ids: list[int] = []
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                continue
            if SET_ID_MIN <= n <= SET_ID_MAX and n not in ids:
                ids.append(n)
        return ids or [1]

    def _reconcile_displays(self) -> None:
        """Register a ``display`` child per configured Set ID; drop any child
        whose Set ID is no longer configured."""
        want = self._parse_display_ids()
        current = set(self.list_children("display"))
        for set_id in want:
            self.register_child("display", set_id)  # idempotent
        for set_id in current - set(want):
            self.deregister_child("display", set_id)
        self.set_state("display_count", len(want))

    # ── Framing hooks (BaseDriver builds the transport from these) ──

    def _create_frame_parser(self) -> Optional[FrameParser]:
        """Use callable parser for MDC binary framing."""
        return CallableFrameParser(_parse_mdc_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        """MDC uses binary framing, not delimiters."""
        return None

    async def _initial_sync(self) -> None:
        """Register the display roster and query status."""
        self._reconcile_displays()
        await self.poll()

    async def refresh_children(self) -> dict[str, Any]:
        """Re-sync the display roster from config and re-read every display's
        live state — backs the IDE 'Refresh from Device' button.

        The roster is declared in ``display_ids`` (an MDC bus reports no device
        list to enumerate), so this reconciles against the current config and
        re-polls, rather than discovering new units."""
        self._reconcile_displays()
        await self.poll()
        return {"displays": len(self.list_children("display"))}

    def _coerce_child_ids(self, command: str, params: dict[str, Any]) -> None:
        """Coerce any child_id-typed param to a bare int (the IDE child picker
        hands back a zero-padded string)."""
        cmd_def = self.DRIVER_INFO["commands"].get(command, {})
        for pname, pdef in cmd_def.get("params", {}).items():
            if pdef.get("type") == "child_id" and pname in params and params[pname] != "":
                try:
                    params[pname] = int(params[pname])
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"{command}: parameter {pname!r} must be an integer "
                        f"Set ID, got {params[pname]!r}"
                    ) from e

    async def _send_to(self, display: Any, cmd: int, data: bytes = b"") -> None:
        """Send one MDC frame to a display's Set ID."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        set_id = int(display)
        await self.transport.send(_build_mdc_frame(cmd, set_id, data))

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a named command to a display (or the whole chain)."""
        params = params or {}
        self._coerce_child_ids(command, params)

        match command:
            case "power_on":
                await self._send_to(params["display"], CMD_POWER, bytes([1]))
            case "power_off":
                await self._send_to(params["display"], CMD_POWER, bytes([0]))
            case "set_volume":
                level = max(0, min(100, int(params.get("level", 0))))
                await self._send_to(params["display"], CMD_VOLUME, bytes([level]))
            case "mute_on":
                await self._send_to(params["display"], CMD_MUTE, bytes([1]))
            case "mute_off":
                await self._send_to(params["display"], CMD_MUTE, bytes([0]))
            case "set_input":
                code = INPUT_MAP.get(params.get("input", ""))
                if code is None:
                    log.warning(f"[{self.device_id}] Unknown input: {params.get('input')}")
                    return
                await self._send_to(params["display"], CMD_INPUT, bytes([code]))
            case "set_brightness":
                level = max(0, min(100, int(params.get("level", 0))))
                await self._send_to(params["display"], CMD_BRIGHTNESS, bytes([level]))
            case "set_contrast":
                level = max(0, min(100, int(params.get("level", 0))))
                await self._send_to(params["display"], CMD_CONTRAST, bytes([level]))
            case "set_backlight":
                level = max(0, min(100, int(params.get("level", 0))))
                await self._send_to(params["display"], CMD_BACKLIGHT, bytes([level]))
            case "set_picture_mode":
                byte = PICTURE_MODE_SET.get(params.get("mode", ""))
                if byte is None:
                    log.warning(f"[{self.device_id}] Unknown picture mode: {params.get('mode')}")
                    return
                await self._send_to(params["display"], CMD_PICTURE_MODE, bytes([byte]))
            case "set_color_tone":
                byte = COLOR_TONE_SET.get(params.get("tone", ""))
                if byte is None:
                    log.warning(f"[{self.device_id}] Unknown color tone: {params.get('tone')}")
                    return
                await self._send_to(params["display"], CMD_COLOR_TONE, bytes([byte]))
            case "all_on":
                for set_id in self.list_children("display"):
                    await self._send_to(set_id, CMD_POWER, bytes([1]))
            case "all_off":
                for set_id in self.list_children("display"):
                    await self._send_to(set_id, CMD_POWER, bytes([0]))
            case "refresh":
                await self.poll()
            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

        log.debug(f"[{self.device_id}] Sent command: {command} {params}")

    async def on_data_received(self, data: bytes) -> None:
        """Parse an MDC response frame and update the answering display.

        The frame parser has stripped the 0xAA header and trailing checksum,
        so ``data`` is the response body::

            [0xFF] [set_id] [len] [ACK/NAK] [r-CMD] [values...]

        The Set ID (data[1]) names which display answered; values are read by
        the echoed command (r-CMD), not the leading 0xFF marker.
        """
        # Need at least: response marker, id, len, ack/nak, r-cmd
        if len(data) < 5 or data[0] != RESPONSE_CMD:
            return

        set_id = data[1]
        ack = data[3]
        rcmd = data[4]
        values = data[5:]

        if ack == NAK:
            log.warning(
                f"[{self.device_id}] Display {set_id} returned NAK for "
                f"command 0x{rcmd:02x}"
            )
            return
        if ack != ACK:
            return
        if not self.is_child_registered("display", set_id):
            return  # a Set ID we don't track (not in the configured roster)

        updates = self._parse_values(rcmd, values)
        if updates:
            self.set_child_state_batch("display", set_id, updates)

    def _parse_values(self, rcmd: int, values: bytes) -> dict[str, Any]:
        """Map a response's echoed command + value bytes to child props."""
        if rcmd == CMD_STATUS and len(values) >= 3:
            # Status values: [power, volume, mute, input, aspect, ...timers].
            updates: dict[str, Any] = {
                "power": "on" if values[0] else "off",
                "volume": values[1],
                "mute": bool(values[2]),
            }
            if len(values) >= 4:
                updates["input"] = INPUT_REVERSE.get(
                    values[3], f"unknown_{values[3]:02x}"
                )
            return updates
        if not values:
            return {}
        if rcmd == CMD_POWER:
            return {"power": "on" if values[0] else "off"}
        if rcmd == CMD_VOLUME:
            return {"volume": values[0]}
        if rcmd == CMD_MUTE:
            return {"mute": bool(values[0])}
        if rcmd == CMD_INPUT:
            return {"input": INPUT_REVERSE.get(values[0], f"unknown_{values[0]:02x}")}
        if rcmd == CMD_CONTRAST:
            return {"contrast": values[0]}
        if rcmd == CMD_BRIGHTNESS:
            return {"brightness": values[0]}
        if rcmd == CMD_BACKLIGHT:
            return {"backlight": values[0]}
        if rcmd == CMD_PICTURE_MODE:
            return {"picture_mode": PICTURE_MODE_NAMES.get(values[0], f"mode_{values[0]:02x}")}
        if rcmd == CMD_COLOR_TONE:
            name = COLOR_TONE_NAMES.get(values[0])
            return {"color_tone": name} if name else {}
        return {}

    async def poll(self) -> None:
        """Query every configured display's status and picture settings."""
        if not self.transport or not self.transport.connected:
            return
        try:
            for set_id in self.list_children("display"):
                for cmd in (
                    CMD_STATUS,
                    CMD_CONTRAST,
                    CMD_BRIGHTNESS,
                    CMD_BACKLIGHT,
                    CMD_COLOR_TONE,
                    CMD_PICTURE_MODE,
                ):
                    await self._send_to(set_id, cmd)
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")
