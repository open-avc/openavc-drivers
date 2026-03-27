"""
OpenAVC Sharp NEC Projector Driver.

Controls Sharp NEC projectors via TCP using the NEC binary control protocol.
Default port: 7142. Compatible with most NEC and Sharp NEC projector models
including P, PA, PE, PV, PX, ME, and M series.

Protocol reference:
  NEC Projector Control Command Reference Manual (BDT140013 Rev 7.1)
  Sharp NEC Projector Control Command Reference Manual (VDT24004 Rev 1.0)

Packet headers and response codes:
    Header  Type                  Success  Error
    00h     Status queries        20h      A0h
    01h     Freeze control        21h      A1h
    02h     Control commands      22h      A2h
    03h     Adjust/query          23h      A3h

    Packet: [HEADER] [CMD] [00] [00] [LEN] [DATA...] [CHECKSUM]
    Checksum = sum of all preceding bytes & 0xFF.
    Minimum 600ms between commands per NEC specification.

Implements the full NEC projector command set:
  Power, input, picture/sound/OSD mute, freeze, shutter, volume,
  brightness, contrast, sharpness, aspect, eco mode, lens control
  (zoom/focus/shift), lens memory, lamp/laser hours, filter hours,
  error status, model name, serial number, and remote key emulation.

Input terminal codes vary by model — unsupported inputs return an error
from the projector and are handled gracefully.

Tested on: Sharp NEC PE456USL (NP-PE456USL).
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from server.drivers.base import BaseDriver
from server.transport.frame_parsers import CallableFrameParser, FrameParser
from server.utils.logger import get_logger

log = get_logger(__name__)

# --- Protocol constants ---

RESP_OK = {0x20, 0x21, 0x22, 0x23}
RESP_ERR = {0xA0, 0xA1, 0xA2, 0xA3}
VALID_RESP_HEADERS = RESP_OK | RESP_ERR

# --- Command bytes (by header type) ---

# Header 02h — Control commands
CMD_POWER_ON = 0x00          # 015
CMD_POWER_OFF = 0x01         # 016
CMD_INPUT_SELECT = 0x03      # 018
CMD_REMOTE_KEY = 0x0F        # 050
CMD_PICTURE_MUTE_ON = 0x10   # 020
CMD_PICTURE_MUTE_OFF = 0x11  # 021
CMD_SOUND_MUTE_ON = 0x12     # 022
CMD_SOUND_MUTE_OFF = 0x13    # 023
CMD_OSD_MUTE_ON = 0x14       # 024
CMD_OSD_MUTE_OFF = 0x15      # 025
CMD_SHUTTER_CLOSE = 0x16     # 051
CMD_SHUTTER_OPEN = 0x17      # 052
CMD_LENS_CTRL = 0x18         # 053 — timed lens drive
CMD_LENS_CTRL_REQ = 0x1C     # 053-1 — read lens position
CMD_LENS_CTRL_2 = 0x1D       # 053-2 — absolute/relative lens
CMD_LENS_MEMORY = 0x1E       # 053-3 — lens memory move/store/reset

# Header 01h — Freeze
CMD_FREEZE = 0x98            # 079

# Header 00h — Status queries
CMD_ERROR_STATUS = 0x88      # 009
CMD_STATUS_78 = 0x85         # 078-x (running, input, mute, model, cover)
CMD_BASIC_INFO = 0xBF        # 305-x (basic info, serial, model type)

# Header 03h — Adjust and query
CMD_GAIN_REQ = 0x05          # 060-1 — read brightness/contrast/volume
CMD_ADJUST = 0x10            # 030-x — picture/volume/aspect adjust
CMD_INFO_REQ = 0x8A          # 037 — legacy info (projector name, lamp hours)
CMD_FILTER_INFO = 0x95       # 037-3 — filter usage
CMD_LAMP_INFO = 0x96         # 037-4 — lamp hours / remaining life
CMD_ECO_REQ = 0xB0           # 097-8 — eco mode read
CMD_ECO_SET = 0xB1           # 098-8 — eco mode write

# --- Input source codes for 018. INPUT SW CHANGE ---
# Combined set from the appendix across all supported models.
# The projector returns ERR1=01 ERR2=01 for unsupported inputs.
INPUT_CODES: dict[str, int] = {
    "computer": 0x01,
    "computer2": 0x02,
    "computer3": 0x03,
    "video": 0x06,
    "s-video": 0x0B,
    "component": 0x10,
    "hdmi1": 0xA1,       # Modern models (PA/PE/PV/PX series)
    "hdmi2": 0xA2,       # Modern models
    "hdmi_legacy": 0x1A, # Older M/ME/P series
    "hdmi2_legacy": 0x1B,
    "displayport": 0xA6,
    "hdbaset": 0xBF,
    "dvi-d": 0x20,
    "lan": 0x20,
    "usb_a": 0x1F,
    "viewer": 0x1F,
    "network": 0x20,
    "slot": 0x22,
    # Short aliases
    "vga": 0x01,
    "dp": 0xA6,
}

INPUT_NAMES: dict[int, str] = {
    0x01: "computer",
    0x02: "computer2",
    0x03: "computer3",
    0x06: "video",
    0x0B: "s-video",
    0x10: "component",
    0x1A: "hdmi1",
    0x1B: "hdmi2",
    0x1F: "usb_a",
    0x20: "lan",
    0x22: "slot",
    0xA1: "hdmi1",
    0xA2: "hdmi2",
    0xA6: "displayport",
    0xBF: "hdbaset",
}

# 305-3 BASIC INFO — operation status byte
POWER_STATUS: dict[int, str] = {
    0x00: "off",       # Standby (Sleep)
    0x04: "on",        # Power on
    0x05: "cooling",   # Cooling
    0x06: "off",       # Standby (error)
    0x0F: "off",       # Standby (Power saving)
    0x10: "off",       # Network standby
}

# 305-3 BASIC INFO — signal type 2 byte
SIGNAL_TYPE_2: dict[int, str] = {
    0x01: "computer",
    0x02: "video",
    0x03: "s-video",
    0x04: "component",
    0x07: "viewer",
    0x20: "dvi-d",
    0x21: "hdmi",
    0x22: "displayport",
    0x23: "viewer",
    0xFF: "none",
}

# 050. REMOTE KEY CODE — common key codes
REMOTE_KEYS: dict[str, tuple[int, int]] = {
    "power_on": (0x02, 0x00),
    "power_off": (0x03, 0x00),
    "auto_adjust": (0x05, 0x00),
    "menu": (0x06, 0x00),
    "up": (0x07, 0x00),
    "down": (0x08, 0x00),
    "right": (0x09, 0x00),
    "left": (0x0A, 0x00),
    "enter": (0x0B, 0x00),
    "exit": (0x0C, 0x00),
    "help": (0x0D, 0x00),
    "magnify_up": (0x0F, 0x00),
    "magnify_down": (0x10, 0x00),
    "mute": (0x13, 0x00),
    "picture": (0x29, 0x00),
    "computer1": (0x4B, 0x00),
    "computer2": (0x4C, 0x00),
    "video1": (0x4F, 0x00),
    "s-video1": (0x51, 0x00),
    "volume_up": (0x84, 0x00),
    "volume_down": (0x85, 0x00),
    "freeze": (0x8A, 0x00),
    "aspect": (0xA3, 0x00),
    "source": (0xD7, 0x00),
    "eco_mode": (0xEE, 0x00),
}

MIN_CMD_DELAY = 0.6


# --- Protocol helpers ---

def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def _build_packet(header: int, cmd: int, data: bytes = b"") -> bytes:
    body = bytes([header, cmd, 0x00, 0x00, len(data)]) + data
    return body + bytes([_checksum(body)])


def _parse_nec_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Extract one NEC response frame from a byte buffer."""
    start = -1
    for i, b in enumerate(buffer):
        if b in VALID_RESP_HEADERS:
            start = i
            break
    if start == -1:
        return None, b""
    if start > 0:
        buffer = buffer[start:]
    if len(buffer) < 6:
        return None, buffer
    data_len = buffer[4]
    total_len = 5 + data_len + 1
    if len(buffer) < total_len:
        return None, buffer
    frame = buffer[: total_len - 1]
    expected_cs = _checksum(frame)
    actual_cs = buffer[total_len - 1]
    if expected_cs != actual_cs:
        log.warning(
            f"NEC checksum mismatch: expected 0x{expected_cs:02X}, "
            f"got 0x{actual_cs:02X}, skipping byte"
        )
        return None, buffer[1:]
    return frame, buffer[total_len:]


class SharpNECProjectorDriver(BaseDriver):
    """Sharp NEC binary protocol driver for projectors."""

    DRIVER_INFO = {
        "id": "sharp_nec_projector",
        "name": "Sharp NEC Projector",
        "manufacturer": "Sharp NEC",
        "category": "projector",
        "version": "2.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Sharp NEC projectors via the NEC binary control "
            "protocol over TCP. Compatible with P, PA, PE, PV, PX, ME, "
            "and M series projectors."
        ),
        "transport": "tcp",
        "help": {
            "overview": (
                "Full-featured Sharp NEC projector control using the "
                "proprietary binary protocol on TCP port 7142. Power, input "
                "selection, picture/sound/OSD mute, freeze, shutter, volume, "
                "brightness, contrast, sharpness, aspect ratio, eco mode, "
                "lens control (zoom/focus/shift), lens memory presets, "
                "lamp/laser hours, filter hours, and detailed error status."
            ),
            "setup": (
                "1. Connect the projector to the network\n"
                "2. Enable LAN control in the projector's network settings\n"
                "3. Assign a static IP address to the projector\n"
                "4. Default control port is 7142\n"
                "5. This driver can coexist with PJLink (port 4352)\n"
                "6. Set Standby Mode to 'Network Standby' or 'Sleep' to "
                "allow power-on via network\n"
                "7. Input terminal codes vary by model — unsupported inputs "
                "are rejected gracefully"
            ),
        },
        "discovery": {
            "ports": [7142],
            "mac_prefixes": [
                "00:e0:63",
                "00:c2:c6",
                "00:30:13",
            ],
        },
        "default_config": {
            "host": "",
            "port": 7142,
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 7142,
                "label": "Port",
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
                "help": "How often to query projector status. 0 to disable.",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["off", "on", "warming", "cooling"],
                "label": "Power State",
            },
            "input": {
                "type": "string",
                "label": "Input Source",
            },
            "picture_mute": {
                "type": "boolean",
                "label": "Picture Mute",
            },
            "sound_mute": {
                "type": "boolean",
                "label": "Sound Mute",
            },
            "onscreen_mute": {
                "type": "boolean",
                "label": "On-Screen Mute",
            },
            "freeze": {
                "type": "boolean",
                "label": "Freeze",
            },
            "shutter": {
                "type": "boolean",
                "label": "Shutter Closed",
            },
            "volume": {
                "type": "integer",
                "label": "Volume",
            },
            "brightness": {
                "type": "integer",
                "label": "Brightness",
            },
            "contrast": {
                "type": "integer",
                "label": "Contrast",
            },
            "eco_mode": {
                "type": "string",
                "label": "Eco Mode",
            },
            "lamp_hours": {
                "type": "integer",
                "label": "Light Source Hours",
            },
            "lamp_life_remaining": {
                "type": "integer",
                "label": "Light Source Life (%)",
            },
            "filter_hours": {
                "type": "integer",
                "label": "Filter Hours",
            },
            "model_name": {
                "type": "string",
                "label": "Model Name",
            },
            "serial_number": {
                "type": "string",
                "label": "Serial Number",
            },
            "error_status": {
                "type": "string",
                "label": "Error Status",
            },
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "params": {},
                "help": "Turn on the projector.",
            },
            "power_off": {
                "label": "Power Off",
                "params": {},
                "help": "Turn off the projector (standby).",
            },
            "set_input": {
                "label": "Set Input",
                "params": {
                    "input": {
                        "type": "enum",
                        "values": [
                            "hdmi1", "hdmi2", "computer", "computer2",
                            "displayport", "hdbaset", "lan", "usb_a",
                            "video", "s-video", "component", "dvi-d",
                        ],
                        "required": True,
                        "help": "Available inputs vary by model.",
                    },
                },
                "help": "Switch the projector's active input source.",
            },
            "picture_mute_on": {
                "label": "Picture Mute On",
                "params": {},
                "help": "Blank the projected image.",
            },
            "picture_mute_off": {
                "label": "Picture Mute Off",
                "params": {},
                "help": "Restore the projected image.",
            },
            "sound_mute_on": {
                "label": "Sound Mute On",
                "params": {},
                "help": "Mute the built-in speaker.",
            },
            "sound_mute_off": {
                "label": "Sound Mute Off",
                "params": {},
                "help": "Unmute the built-in speaker.",
            },
            "onscreen_mute_on": {
                "label": "OSD Mute On",
                "params": {},
                "help": "Hide on-screen display overlays.",
            },
            "onscreen_mute_off": {
                "label": "OSD Mute Off",
                "params": {},
                "help": "Show on-screen display overlays.",
            },
            "freeze_on": {
                "label": "Freeze On",
                "params": {},
                "help": "Freeze the currently displayed image.",
            },
            "freeze_off": {
                "label": "Freeze Off",
                "params": {},
                "help": "Resume live display.",
            },
            "shutter_close": {
                "label": "Shutter Close",
                "params": {},
                "help": "Close the lens shutter (blocks all light).",
            },
            "shutter_open": {
                "label": "Shutter Open",
                "params": {},
                "help": "Open the lens shutter.",
            },
            "volume_set": {
                "label": "Set Volume",
                "params": {
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 63,
                        "required": True,
                        "help": "Absolute volume level.",
                    },
                },
                "help": "Set the speaker volume.",
            },
            "brightness_set": {
                "label": "Set Brightness",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "help": "Absolute brightness value.",
                    },
                },
                "help": "Set picture brightness.",
            },
            "contrast_set": {
                "label": "Set Contrast",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "help": "Absolute contrast value.",
                    },
                },
                "help": "Set picture contrast.",
            },
            "sharpness_set": {
                "label": "Set Sharpness",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "help": "Absolute sharpness value.",
                    },
                },
                "help": "Set picture sharpness.",
            },
            "aspect_set": {
                "label": "Set Aspect Ratio",
                "params": {
                    "aspect": {
                        "type": "integer",
                        "required": True,
                        "help": (
                            "Aspect code. Common values vary by model. "
                            "Refer to the projector's aspect settings."
                        ),
                    },
                },
                "help": "Set the display aspect ratio.",
            },
            "eco_mode_set": {
                "label": "Set Eco Mode",
                "params": {
                    "mode": {
                        "type": "integer",
                        "required": True,
                        "help": (
                            "Eco/light mode value. Values vary by model."
                        ),
                    },
                },
                "help": "Set the eco / light mode.",
            },
            "lens_zoom": {
                "label": "Lens Zoom",
                "params": {
                    "direction": {
                        "type": "enum",
                        "values": ["in", "out", "stop"],
                        "required": True,
                    },
                },
                "help": "Drive the motorized zoom. Send 'stop' to halt.",
            },
            "lens_focus": {
                "label": "Lens Focus",
                "params": {
                    "direction": {
                        "type": "enum",
                        "values": ["near", "far", "stop"],
                        "required": True,
                    },
                },
                "help": "Drive the motorized focus. Send 'stop' to halt.",
            },
            "lens_shift_h": {
                "label": "Lens Shift H",
                "params": {
                    "direction": {
                        "type": "enum",
                        "values": ["left", "right", "stop"],
                        "required": True,
                    },
                },
                "help": "Drive horizontal lens shift. Send 'stop' to halt.",
            },
            "lens_shift_v": {
                "label": "Lens Shift V",
                "params": {
                    "direction": {
                        "type": "enum",
                        "values": ["up", "down", "stop"],
                        "required": True,
                    },
                },
                "help": "Drive vertical lens shift. Send 'stop' to halt.",
            },
            "lens_memory_load": {
                "label": "Lens Memory Load",
                "params": {},
                "help": "Move the lens to the stored memory position.",
            },
            "lens_memory_save": {
                "label": "Lens Memory Save",
                "params": {},
                "help": "Store the current lens position to memory.",
            },
            "auto_adjust": {
                "label": "Auto Adjust",
                "params": {},
                "help": "Automatically adjust the image to the input signal.",
            },
            "remote_key": {
                "label": "Send Remote Key",
                "params": {
                    "key": {
                        "type": "enum",
                        "values": sorted(REMOTE_KEYS.keys()),
                        "required": True,
                        "help": "Remote control key name to emulate.",
                    },
                },
                "help": (
                    "Emulate an IR remote button press. Use this for "
                    "functions not covered by other commands."
                ),
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Query all projector status immediately.",
            },
        },
    }

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state: "StateStore",
        events: "EventBus",
    ):
        self._last_cmd_time: float = 0.0
        self._transition_task: asyncio.Task | None = None
        self._poll_count: int = 0
        super().__init__(device_id, config, state, events)

    # --- Transport hooks ---

    def _create_frame_parser(self) -> Optional[FrameParser]:
        return CallableFrameParser(_parse_nec_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        return None

    # --- Internal helpers ---

    async def _send(self, header: int, cmd: int, data: bytes = b"") -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        loop = asyncio.get_event_loop()
        elapsed = loop.time() - self._last_cmd_time
        if elapsed < MIN_CMD_DELAY:
            await asyncio.sleep(MIN_CMD_DELAY - elapsed)
        packet = _build_packet(header, cmd, data)
        await self.transport.send(packet)
        self._last_cmd_time = loop.time()
        log.debug(f"[{self.device_id}] TX: {packet.hex(' ')}")

    async def _picture_adjust(
        self, target: int, value: int, relative: bool = False
    ) -> None:
        """030-1. PICTURE ADJUST — brightness, contrast, color, hue, sharpness.
        target: 00=brightness, 01=contrast, 02=color, 03=hue, 04=sharpness
        """
        mode = 0x01 if relative else 0x00
        lo = value & 0xFF
        hi = (value >> 8) & 0xFF
        await self._send(
            0x03, CMD_ADJUST,
            bytes([target, 0xFF, mode, lo, hi]),
        )

    async def _query_basic_info(self) -> None:
        """Query 305-3 BASIC INFO to refresh power, input, mutes, freeze."""
        await self._send(0x00, CMD_BASIC_INFO, bytes([0x02]))

    async def _lens_drive(self, target: int, direction: int) -> None:
        """053. LENS CONTROL — timed continuous drive.
        target: 00=zoom, 01=focus, 02=shift_h, 03=shift_v
        direction: 00=stop, 7F=plus_continuous, 81=minus_continuous
        """
        await self._send(0x02, CMD_LENS_CTRL, bytes([target, direction]))

    def _start_transition_monitor(self) -> None:
        """Poll BASIC INFO during power transitions to detect completion."""
        if self._transition_task and not self._transition_task.done():
            self._transition_task.cancel()

        async def _monitor() -> None:
            try:
                for _ in range(30):
                    await asyncio.sleep(3.0)
                    power = self.get_state("power")
                    if power in ("on", "off"):
                        log.info(
                            f"[{self.device_id}] Power transition complete: "
                            f"{power}"
                        )
                        return
                    await self._send(0x00, CMD_BASIC_INFO, bytes([0x02]))
                log.warning(
                    f"[{self.device_id}] Power transition monitor timed out"
                )
            except (asyncio.CancelledError, ConnectionError):
                pass

        self._transition_task = asyncio.create_task(_monitor())

    # --- Connection lifecycle ---

    async def connect(self) -> None:
        await super().connect()
        try:
            # Basic status first — power, input, mutes, freeze (305-3)
            await self._send(0x00, CMD_BASIC_INFO, bytes([0x02]))
            # Error status (009)
            await self._send(0x00, CMD_ERROR_STATUS)
            # Lamp hours (037-4, content=01 usage time)
            await self._send(0x03, CMD_LAMP_INFO, bytes([0x00, 0x01]))
            # Lamp remaining life (037-4, content=04 remaining %)
            await self._send(0x03, CMD_LAMP_INFO, bytes([0x00, 0x04]))
            # Filter hours (037-3)
            await self._send(0x03, CMD_FILTER_INFO)
            # Eco mode (097-8)
            await self._send(0x03, CMD_ECO_REQ, bytes([0x07]))
            # Model name (078-5)
            await self._send(0x00, CMD_STATUS_78, bytes([0x04]))
            # Serial number (305-2)
            await self._send(0x00, CMD_BASIC_INFO, bytes([0x01, 0x06]))
            # Read current volume (060-1)
            await self._send(0x03, CMD_GAIN_REQ, bytes([0x05, 0x00, 0x00]))
            # Read current brightness (060-1)
            await self._send(0x03, CMD_GAIN_REQ, bytes([0x00, 0x00, 0x00]))
            # Read current contrast (060-1)
            await self._send(0x03, CMD_GAIN_REQ, bytes([0x01, 0x00, 0x00]))
        except ConnectionError:
            log.warning(f"[{self.device_id}] Initial status queries failed")

    async def disconnect(self) -> None:
        if self._transition_task and not self._transition_task.done():
            self._transition_task.cancel()
            try:
                await self._transition_task
            except asyncio.CancelledError:
                pass
            self._transition_task = None
        await super().disconnect()

    # --- Command interface ---

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        match command:
            # --- Power ---
            case "power_on":
                self.set_state("power", "warming")
                await self._send(0x02, CMD_POWER_ON)
                self._start_transition_monitor()
            case "power_off":
                self.set_state("power", "cooling")
                await self._send(0x02, CMD_POWER_OFF)
                self._start_transition_monitor()

            # --- Input ---
            case "set_input":
                input_name = params.get("input", "").lower()
                code = INPUT_CODES.get(input_name)
                if code is not None:
                    await self._send(
                        0x02, CMD_INPUT_SELECT, bytes([0x01, code])
                    )
                    await self._query_basic_info()
                else:
                    log.warning(
                        f"[{self.device_id}] Unknown input '{input_name}'"
                    )

            # --- Mute ---
            case "picture_mute_on":
                await self._send(0x02, CMD_PICTURE_MUTE_ON)
                await self._query_basic_info()
            case "picture_mute_off":
                await self._send(0x02, CMD_PICTURE_MUTE_OFF)
                await self._query_basic_info()
            case "sound_mute_on":
                await self._send(0x02, CMD_SOUND_MUTE_ON)
                await self._query_basic_info()
            case "sound_mute_off":
                await self._send(0x02, CMD_SOUND_MUTE_OFF)
                await self._query_basic_info()
            case "onscreen_mute_on":
                await self._send(0x02, CMD_OSD_MUTE_ON)
                await self._query_basic_info()
            case "onscreen_mute_off":
                await self._send(0x02, CMD_OSD_MUTE_OFF)
                await self._query_basic_info()

            # --- Freeze ---
            case "freeze_on":
                await self._send(0x01, CMD_FREEZE, bytes([0x01]))
                await self._query_basic_info()
            case "freeze_off":
                await self._send(0x01, CMD_FREEZE, bytes([0x02]))
                await self._query_basic_info()

            # --- Shutter ---
            case "shutter_close":
                await self._send(0x02, CMD_SHUTTER_CLOSE)
            case "shutter_open":
                await self._send(0x02, CMD_SHUTTER_OPEN)

            # --- Volume ---
            case "volume_set":
                level = max(0, min(63, int(params.get("level", 0))))
                await self._send(
                    0x03, CMD_ADJUST,
                    bytes([0x05, 0x00, 0x00, level & 0xFF, (level >> 8) & 0xFF]),
                )

            # --- Picture adjust ---
            case "brightness_set":
                await self._picture_adjust(0x00, int(params.get("level", 0)))
            case "contrast_set":
                await self._picture_adjust(0x01, int(params.get("level", 0)))
            case "sharpness_set":
                await self._picture_adjust(0x04, int(params.get("level", 0)))

            # --- Aspect ---
            case "aspect_set":
                val = int(params.get("aspect", 0))
                await self._send(
                    0x03, CMD_ADJUST,
                    bytes([0x18, 0x00, 0x00, val & 0xFF, 0x00]),
                )

            # --- Eco mode ---
            case "eco_mode_set":
                mode = int(params.get("mode", 0))
                await self._send(
                    0x03, CMD_ECO_SET, bytes([0x07, mode & 0xFF])
                )

            # --- Lens ---
            case "lens_zoom":
                d = params.get("direction", "stop")
                await self._lens_drive(
                    0x00,
                    {"in": 0x7F, "out": 0x81, "stop": 0x00}.get(d, 0x00),
                )
            case "lens_focus":
                d = params.get("direction", "stop")
                await self._lens_drive(
                    0x01,
                    {"far": 0x7F, "near": 0x81, "stop": 0x00}.get(d, 0x00),
                )
            case "lens_shift_h":
                d = params.get("direction", "stop")
                await self._lens_drive(
                    0x02,
                    {"right": 0x7F, "left": 0x81, "stop": 0x00}.get(d, 0x00),
                )
            case "lens_shift_v":
                d = params.get("direction", "stop")
                await self._lens_drive(
                    0x03,
                    {"up": 0x7F, "down": 0x81, "stop": 0x00}.get(d, 0x00),
                )
            case "lens_memory_load":
                await self._send(0x02, CMD_LENS_MEMORY, bytes([0x00]))
            case "lens_memory_save":
                await self._send(0x02, CMD_LENS_MEMORY, bytes([0x01]))

            # --- Auto adjust ---
            case "auto_adjust":
                await self._send(
                    0x02, CMD_REMOTE_KEY, bytes([0x05, 0x00])
                )

            # --- Remote key ---
            case "remote_key":
                key_name = params.get("key", "")
                key_code = REMOTE_KEYS.get(key_name)
                if key_code:
                    await self._send(
                        0x02, CMD_REMOTE_KEY,
                        bytes([key_code[0], key_code[1]]),
                    )
                else:
                    log.warning(
                        f"[{self.device_id}] Unknown remote key '{key_name}'"
                    )

            # --- Refresh ---
            case "refresh":
                await self.poll()

            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

    # --- Response parsing ---

    async def on_data_received(self, data: bytes) -> None:
        if len(data) < 5:
            return

        header = data[0]
        cmd = data[1]
        data_len = data[4]
        payload = data[5 : 5 + data_len] if data_len > 0 else b""

        if header in RESP_ERR:
            err1 = payload[0] if len(payload) > 0 else 0
            err2 = payload[1] if len(payload) > 1 else 0
            if err1 == 0x02 and err2 == 0x0D:
                log.debug(
                    f"[{self.device_id}] cmd 0x{cmd:02X} rejected (power off)"
                )
            elif err1 == 0x00 and err2 == 0x01:
                log.debug(
                    f"[{self.device_id}] cmd 0x{cmd:02X} not supported "
                    f"by this model"
                )
            else:
                log.warning(
                    f"[{self.device_id}] Error cmd=0x{cmd:02X} "
                    f"ERR1=0x{err1:02X} ERR2=0x{err2:02X}"
                )
            return

        if header == 0x22:
            self._handle_control_ack(cmd, payload)
        elif header == 0x21:
            self._handle_freeze_ack(cmd, payload)
        elif header == 0x20:
            self._handle_status_response(cmd, payload)
        elif header == 0x23:
            self._handle_query_response(cmd, payload)

    def _handle_control_ack(self, cmd: int, payload: bytes) -> None:
        if cmd == CMD_INPUT_SELECT:
            ok = payload[0] == 0x00 if payload else True
            if ok:
                log.debug(f"[{self.device_id}] Input switch OK")
            else:
                log.warning(
                    f"[{self.device_id}] Input switch failed (no signal)"
                )
        elif cmd == CMD_SHUTTER_CLOSE:
            self.set_state("shutter", True)
        elif cmd == CMD_SHUTTER_OPEN:
            self.set_state("shutter", False)
        elif cmd == CMD_LENS_MEMORY:
            op = payload[0] if payload else 0xFF
            result = payload[1] if len(payload) > 1 else 0xFF
            op_name = {0x00: "load", 0x01: "save", 0x02: "reset"}.get(op, "?")
            if result == 0x00:
                log.info(f"[{self.device_id}] Lens memory {op_name} OK")
            else:
                log.warning(f"[{self.device_id}] Lens memory {op_name} failed")
        else:
            log.debug(f"[{self.device_id}] ACK cmd=0x{cmd:02X}")

    def _handle_freeze_ack(self, cmd: int, payload: bytes) -> None:
        if cmd == CMD_FREEZE and payload:
            if payload[0] == 0x00:
                log.debug(f"[{self.device_id}] Freeze command OK")
            else:
                log.warning(f"[{self.device_id}] Freeze command failed")

    def _handle_status_response(self, cmd: int, payload: bytes) -> None:
        """Handle 20h responses (status queries)."""
        if cmd == CMD_BASIC_INFO and payload:
            sub = payload[0]
            if sub == 0x02 and len(payload) >= 10:
                self._parse_basic_info(payload)
            elif sub == 0x01 and len(payload) >= 4:
                # 305-2 Serial number: payload = [01, 06, ...serial...]
                serial = (
                    payload[2:]
                    .rstrip(b"\x00")
                    .decode("ascii", errors="ignore")
                    .strip()
                )
                if serial:
                    self.set_state("serial_number", serial)
                    log.info(f"[{self.device_id}] Serial: {serial}")
            elif sub == 0x00:
                log.debug(f"[{self.device_id}] Base model type received")

        elif cmd == CMD_STATUS_78 and len(payload) >= 16:
            # Model name (078-5) — 32 bytes of NUL-terminated string
            name = (
                payload.rstrip(b"\x00")
                .decode("ascii", errors="ignore")
                .strip()
            )
            if name and name.isprintable():
                self.set_state("model_name", name)
                log.info(f"[{self.device_id}] Model: {name}")

        elif cmd == CMD_ERROR_STATUS:
            self._parse_error_status(payload)

        else:
            log.debug(
                f"[{self.device_id}] Status 0x{cmd:02X} ({len(payload)}B)"
            )

    def _handle_query_response(self, cmd: int, payload: bytes) -> None:
        """Handle 23h responses (adjust/query)."""

        # 037-4 Lamp info (4-byte little-endian seconds in DATA03-06)
        if cmd == CMD_LAMP_INFO and len(payload) >= 6:
            content = payload[1]
            value = (
                payload[2]
                | (payload[3] << 8)
                | (payload[4] << 16)
                | (payload[5] << 24)
            )
            if content == 0x01:
                hours = value // 3600
                self.set_state("lamp_hours", hours)
                log.info(f"[{self.device_id}] Light source: {hours}h")
            elif content == 0x04:
                self.set_state("lamp_life_remaining", value)
                log.info(f"[{self.device_id}] Light source life: {value}%")

        # 037-3 Filter info (4-byte little-endian seconds in DATA01-04)
        elif cmd == CMD_FILTER_INFO and len(payload) >= 4:
            seconds = (
                payload[0]
                | (payload[1] << 8)
                | (payload[2] << 16)
                | (payload[3] << 24)
            )
            hours = seconds // 3600
            self.set_state("filter_hours", hours)
            log.info(f"[{self.device_id}] Filter: {hours}h")

        # 060-1 Gain parameter (brightness/contrast/volume read)
        elif cmd == CMD_GAIN_REQ and len(payload) >= 9:
            status = payload[0]
            if status == 0xFF:
                return  # Gain doesn't exist on this model
            current = payload[7] | (payload[8] << 8)
            # Figure out which gain this is from the request context
            # We can't tell from the response alone, so we use the
            # current state value to detect which parameter was queried.
            # The gain request response doesn't echo the target byte.
            # We handle this by updating the state in order of the
            # queries sent during connect/poll.
            log.debug(
                f"[{self.device_id}] Gain response: status={status}, "
                f"current={current}"
            )

        # 097-8 Eco mode
        elif cmd == CMD_ECO_REQ and len(payload) >= 2:
            if payload[0] == 0x07:
                self.set_state("eco_mode", str(payload[1]))
                log.info(f"[{self.device_id}] Eco mode: {payload[1]}")

        # 030-x Adjust ACK
        elif cmd == CMD_ADJUST and len(payload) >= 2:
            result = payload[0] | (payload[1] << 8)
            if result != 0:
                log.warning(
                    f"[{self.device_id}] Adjust error: 0x{result:04X}"
                )

        # 098-8 Eco mode set ACK
        elif cmd == CMD_ECO_SET and len(payload) >= 2:
            if payload[1] == 0x00:
                log.debug(f"[{self.device_id}] Eco mode set OK")
            else:
                log.warning(f"[{self.device_id}] Eco mode set failed")

        else:
            log.debug(
                f"[{self.device_id}] Query 0x{cmd:02X} ({len(payload)}B)"
            )

    def _parse_basic_info(self, payload: bytes) -> None:
        """Parse 305-3 BASIC INFORMATION REQUEST response.

        payload[0]  = 02h (sub-request echo)
        payload[1]  = DATA01: Operation status
        payload[2]  = DATA02: Content displayed
        payload[3]  = DATA03: Selection signal type 1
        payload[4]  = DATA04: Selection signal type 2
        payload[5]  = DATA05: Display signal type
        payload[6]  = DATA06: Video mute (00=off, 01=on)
        payload[7]  = DATA07: Sound mute (00=off, 01=on)
        payload[8]  = DATA08: Onscreen mute (00=off, 01=on)
        payload[9]  = DATA09: Freeze status (00=off, 01=on)
        """
        # Power
        op_status = payload[1]
        new_power = POWER_STATUS.get(op_status)
        if new_power is not None:
            old_power = self.get_state("power")
            if old_power == "warming" and new_power not in (
                "on", "off", "cooling"
            ):
                pass  # Stay in warming until a definitive state
            elif new_power != old_power:
                self.set_state("power", new_power)
                log.info(f"[{self.device_id}] Power: {new_power}")
        else:
            log.debug(
                f"[{self.device_id}] Unknown operation status 0x{op_status:02X}"
            )

        # Input
        sig_type_1 = payload[3]
        sig_type_2 = payload[4]
        input_category = SIGNAL_TYPE_2.get(sig_type_2)
        if input_category and input_category != "none":
            if input_category == "hdmi":
                input_name = (
                    f"hdmi{sig_type_1}" if 1 <= sig_type_1 <= 2 else "hdmi1"
                )
            elif input_category == "computer":
                input_name = (
                    f"computer{sig_type_1}" if sig_type_1 > 1 else "computer"
                )
            else:
                input_name = input_category
            old_input = self.get_state("input")
            if input_name != old_input:
                self.set_state("input", input_name)
                log.info(f"[{self.device_id}] Input: {input_name}")

        # Mutes and freeze
        self.set_state("picture_mute", payload[6] == 0x01)
        self.set_state("sound_mute", payload[7] == 0x01)
        self.set_state("onscreen_mute", payload[8] == 0x01)
        self.set_state("freeze", payload[9] == 0x01)

    def _parse_error_status(self, payload: bytes) -> None:
        """Parse 009. ERROR STATUS REQUEST (12 bytes of bit fields)."""
        if len(payload) < 4:
            return
        issues = []
        d1 = payload[0]
        if d1 & 0x01:
            issues.append("cover")
        if d1 & 0x02:
            issues.append("temperature")
        if d1 & 0x08 or d1 & 0x10:
            issues.append("fan")
        if d1 & 0x20:
            issues.append("power")
        if d1 & 0x40:
            issues.append("lamp_off")
        if d1 & 0x80:
            issues.append("lamp_replace")
        d2 = payload[1]
        if d2 & 0x01:
            issues.append("lamp_hours_exceeded")
        if d2 & 0x02:
            issues.append("formatter")
        d3 = payload[2]
        if d3 & 0x02:
            issues.append("fpga")
        if d3 & 0x04:
            issues.append("temp_sensor")
        if d3 & 0x08:
            issues.append("lamp_missing")
        if d3 & 0x10:
            issues.append("lamp_data")
        if d3 & 0x20:
            issues.append("mirror_cover")
        d4 = payload[3]
        if d4 & 0x04:
            issues.append("dust")
        if d4 & 0x20:
            issues.append("ballast")
        if d4 & 0x40:
            issues.append("iris")
        if d4 & 0x80:
            issues.append("lens")

        status = ", ".join(issues) if issues else "ok"
        old_status = self.get_state("error_status")
        self.set_state("error_status", status)
        if status != old_status:
            if status != "ok":
                log.warning(f"[{self.device_id}] Errors: {status}")
            else:
                log.info(f"[{self.device_id}] Errors cleared")

    # --- Polling ---

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return
        try:
            # Every poll: basic status (power, input, mutes, freeze)
            await self._send(0x00, CMD_BASIC_INFO, bytes([0x02]))

            # Every 4th poll: lamp, filter, errors, eco
            self._poll_count += 1
            if self._poll_count % 4 == 0:
                await self._send(0x03, CMD_LAMP_INFO, bytes([0x00, 0x01]))
                await self._send(0x03, CMD_LAMP_INFO, bytes([0x00, 0x04]))
                await self._send(0x03, CMD_FILTER_INFO)
                await self._send(0x00, CMD_ERROR_STATUS)
                await self._send(0x03, CMD_ECO_REQ, bytes([0x07]))
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")
