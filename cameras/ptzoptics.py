"""
OpenAVC PTZOptics Camera Driver.

Controls PTZOptics PTZ cameras over the VISCA-over-IP protocol on TCP
port 5678. PTZOptics ships raw VISCA — each command is the bare
``81 ... FF`` packet sent directly over TCP, with no Sony-style
8-byte VISCA-over-IP wrapper. Responses come back the same way:
``90 4y FF`` (ACK), ``90 5y FF`` (Completion), ``90 5y <data> FF``
(Completion with payload, used for inquiry replies), or
``90 6y <code> FF`` (error).

Polling: VISCA is purely request/response; there's no subscription /
notification mechanism, so the driver polls camera state at the
configured interval (default 5 s). Pan / tilt / zoom / focus position
inquiries plus AE / WB / focus-mode / backlight / flip / picture-flip /
LR-reverse / preset-speed state are queried each cycle. Commands
themselves are fire-and-forget — we don't wait for ACK / Completion
because the camera processes movement in <100 ms and the driver should
not block joystick-rate inputs.

Source: https://ptzoptics.com/wp-content/uploads/2020/11/PTZOptics-VISCA-over-IP-Rev-1_2-8-20.pdf
"""

from __future__ import annotations

import asyncio
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# Direction nibble pairs for Pan/Tilt drive (`81 01 06 01 VV WW <p> <t> FF`)
_PT_DIR = {
    "pt_up":         (0x03, 0x01),
    "pt_down":       (0x03, 0x02),
    "pt_left":       (0x01, 0x03),
    "pt_right":      (0x02, 0x03),
    "pt_up_left":    (0x01, 0x01),
    "pt_up_right":   (0x02, 0x01),
    "pt_down_left":  (0x01, 0x02),
    "pt_down_right": (0x02, 0x02),
    "pt_stop":       (0x03, 0x03),
}

# AE mode byte values
_AE_MODE_TO_BYTE = {
    "full_auto": 0x00,
    "manual":    0x03,
    "shutter":   0x0A,
    "iris":      0x0B,
    "bright":    0x0D,
}
_BYTE_TO_AE_MODE = {v: k for k, v in _AE_MODE_TO_BYTE.items()}

# WB mode byte values
_WB_MODE_TO_BYTE = {
    "auto":         0x00,
    "indoor":       0x01,
    "outdoor":      0x02,
    "one_push":     0x03,
    "manual":       0x05,
    "color_temp":   0x20,
}
_BYTE_TO_WB_MODE = {v: k for k, v in _WB_MODE_TO_BYTE.items()}

# Flip (single-command) byte values
_FLIP_TO_BYTE = {"off": 0x00, "h": 0x01, "v": 0x02, "hv": 0x03}
_BYTE_TO_FLIP = {v: k for k, v in _FLIP_TO_BYTE.items()}

# Tally light state byte values per `81 0A 02 02 0p FF` — p=1 flashing,
# p=2 on (always lit), p=3 off (back to normal).
_TALLY_TO_BYTE = {"flashing": 0x01, "on": 0x02, "off": 0x03}


def _encode_4nibble(val: int) -> bytes:
    """Encode a 16-bit value as four bytes (each high nibble 0, low nibble data)."""
    if val < 0:
        val = (val + 0x10000) & 0xFFFF
    val &= 0xFFFF
    return bytes([
        (val >> 12) & 0x0F,
        (val >> 8) & 0x0F,
        (val >> 4) & 0x0F,
        val & 0x0F,
    ])


def _decode_4nibble(data: bytes, signed: bool = False) -> int:
    """Decode four 4-nibble bytes into a 16-bit value."""
    val = (
        ((data[0] & 0x0F) << 12)
        | ((data[1] & 0x0F) << 8)
        | ((data[2] & 0x0F) << 4)
        | (data[3] & 0x0F)
    )
    if signed and (val & 0x8000):
        val -= 0x10000
    return val


class PTZOpticsDriver(BaseDriver):
    """PTZOptics camera driver via raw VISCA-over-IP (TCP 5678)."""

    DRIVER_INFO = {
        "id": "ptzoptics",
        "name": "PTZOptics Camera",
        "manufacturer": "PTZOptics",
        "category": "camera",
        "version": "1.3.2",
        "author": "OpenAVC",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "description": (
            "Controls PTZOptics PTZ cameras over the VISCA-over-IP "
            "protocol on TCP port 5678. Pan, tilt, zoom, focus, "
            "preset recall / save / reset, tally light, image flip, "
            "auto-exposure mode, white balance mode, and backlight. "
            "Polled state covers pan / tilt / zoom / focus position, "
            "focus mode, AE mode, WB mode, backlight, and flip "
            "settings."
        ),
        "source_url": (
            "https://ptzoptics.com/wp-content/uploads/2020/11/"
            "PTZOptics-VISCA-over-IP-Rev-1_2-8-20.pdf"
        ),
        "tags": ["ptz", "camera", "visca", "ndi", "sdi"],
        "verified": False,
        "simulated": True,
        "protocols": ["visca"],
        "ports": [5678],
        "transport": "tcp",
        "discovery": {
            # PTZOptics cameras advertise via ONVIF when ONVIF is enabled
            # in the camera menu — picked up by the generic onvif_camera
            # anchor. The hints below catch the camera even when ONVIF
            # is disabled.
            "port_open": [5678],
            "hostname": ["^PTZOptics", "^ptz-"],
            "manufacturer_alias": ["ptzoptics", "ptz optics", "ptz-optics"],
        },
        "compatible_models": [
            {
                "manufacturer": "PTZOptics",
                "models": [
                    "Move 4K",
                    "Move SE",
                    "Move PT4K",
                    "Link 4K",
                    "Link SDI",
                    "Studio Pro",
                    "EDU SDI",
                    "EDU NDI",
                    "30X-NDI",
                    "30X-SDI",
                    "20X-NDI",
                    "20X-SDI",
                    "12X-USB",
                    "VL-ZCAM",
                ],
                "confidence": "untested",
                "notes": (
                    "All PTZOptics cameras share the same VISCA-over-IP "
                    "command set on TCP 5678. Older models may not "
                    "support every command (the camera ignores unknown "
                    "ones). 12X-USB needs PTZOptics' IP/serial bridge "
                    "for network control."
                ),
            }
        ],
        "help": {
            "overview": (
                "PTZOptics cameras are widely used in classrooms, "
                "houses of worship, and conference rooms. This driver "
                "speaks raw VISCA over TCP 5678 (no Sony VISCA-over-IP "
                "header), which is what PTZOptics cameras actually "
                "expose — UDP 1259 also works but TCP gives reliable "
                "delivery for absolute-position commands.\n\n"
                "VISCA is request/response only. The driver polls the "
                "camera every few seconds for pan / tilt / zoom / focus "
                "position and exposure / WB state. Joystick-style "
                "movement uses pt_up / pt_down / etc. with pt_stop to "
                "halt; absolute moves use pt_absolute with explicit "
                "pan and tilt positions."
            ),
            "setup": (
                "1. Find the camera's IP via the OSD menu, the "
                "PTZOptics utility, or your router's DHCP table.\n"
                "2. Confirm VISCA-over-IP is enabled in the camera's "
                "web UI (Settings → Network or Settings → VISCA). "
                "Most cameras ship with it on by default.\n"
                "3. Enter the IP in OpenAVC. Port 5678 is fixed.\n"
                "4. After connecting, pan_position / tilt_position / "
                "zoom_position will populate automatically.\n"
                "5. Save up to 128 presets (numbered 0–127). 'set "
                "preset speed' applies to subsequent recalls."
            ),
        },
        "default_config": {
            "host": "",
            "port": 5678,
            "poll_interval": 5,
            "pan_speed": 12,
            "tilt_speed": 10,
            "inter_command_delay": 0.05,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 5678,
                "label": "TCP Port",
                "description": "PTZOptics VISCA-over-IP port — always 5678.",
            },
            "pan_speed": {
                "type": "integer",
                "default": 12,
                "min": 1,
                "max": 24,
                "label": "Default Pan Speed (1–24)",
            },
            "tilt_speed": {
                "type": "integer",
                "default": 10,
                "min": 1,
                "max": 20,
                "label": "Default Tilt Speed (1–20)",
            },
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often to re-query the camera for state. Set "
                    "to 0 to disable polling."
                ),
            },
            "inter_command_delay": {
                "type": "number",
                "default": 0.05,
                "min": 0.0,
                "label": "Inter-command Delay (sec)",
                "description": (
                    "Minimum delay between commands. PTZOptics is "
                    "tolerant; 50 ms keeps fast joystick input from "
                    "overwhelming the command buffer."
                ),
            },
        },
        "state_variables": {
            "pan_position": {
                "type": "integer",
                "label": "Pan Position",
                "min": -32768,
                "max": 32767,
            },
            "tilt_position": {
                "type": "integer",
                "label": "Tilt Position",
                "min": -32768,
                "max": 32767,
            },
            "zoom_position": {
                "type": "integer",
                "label": "Zoom Position",
                "min": 0,
                "max": 16384,
            },
            "focus_position": {
                "type": "integer",
                "label": "Focus Position",
                "min": 0,
                "max": 65535,
            },
            "focus_mode": {
                "type": "enum",
                "values": ["auto", "manual"],
                "label": "Focus Mode",
            },
            "ae_mode": {
                "type": "enum",
                "values": ["full_auto", "manual", "shutter", "iris", "bright"],
                "label": "Auto-Exposure Mode",
            },
            "wb_mode": {
                "type": "enum",
                "values": [
                    "auto", "indoor", "outdoor", "one_push", "manual", "color_temp"
                ],
                "label": "White Balance Mode",
            },
            "backlight": {
                "type": "boolean",
                "label": "Backlight Compensation",
            },
            "flip": {
                "type": "enum",
                "values": ["off", "h", "v", "hv"],
                "label": "Image Flip",
            },
            "lr_reverse": {
                "type": "boolean",
                "label": "L/R Reverse (Horizontal Flip)",
            },
            "picture_flip": {
                "type": "boolean",
                "label": "Picture Flip (Vertical)",
            },
        },
        # Exposure mode, white-balance mode, backlight, and the three image-
        # orientation toggles are device-side configuration the camera persists
        # and reports back (the poll() AE/WB/backlight/flip/lr_reverse/
        # picture_flip inquiries are the read-back), so they get the editable-
        # field + offline pending-queue treatment of device settings on top of
        # the transient set_* commands (same dual surface as crestron_nvx). PTZ
        # / zoom / focus position and presets are live operational state, not
        # settings.
        "device_settings": {
            "ae_mode": {
                "type": "enum",
                "label": "Auto-Exposure Mode",
                "help": (
                    "How the camera meters exposure. full_auto lets the camera "
                    "choose; manual / shutter / iris / bright fix one or more "
                    "parameters."
                ),
                "values": ["full_auto", "manual", "shutter", "iris", "bright"],
                "state_key": "ae_mode",
                "default": "full_auto",
                "setup": False,
            },
            "wb_mode": {
                "type": "enum",
                "label": "White Balance Mode",
                "help": (
                    "White-balance behavior. auto tracks the scene; indoor / "
                    "outdoor lock a colour temperature; one_push samples once; "
                    "color_temp / manual use fixed values."
                ),
                "values": [
                    "auto", "indoor", "outdoor", "one_push", "manual", "color_temp",
                ],
                "state_key": "wb_mode",
                "default": "auto",
                "setup": False,
            },
            "backlight": {
                "type": "boolean",
                "label": "Backlight Compensation",
                "help": (
                    "Brightens a subject lit from behind. Persisted on the "
                    "camera."
                ),
                "state_key": "backlight",
                "default": False,
                "setup": False,
            },
            "flip": {
                "type": "enum",
                "label": "Image Flip",
                "help": (
                    "Single-command image orientation — off, horizontal, "
                    "vertical, or both. Set once at install for the mount "
                    "orientation."
                ),
                "values": ["off", "h", "v", "hv"],
                "state_key": "flip",
                "default": "off",
                "setup": False,
            },
            "lr_reverse": {
                "type": "boolean",
                "label": "L/R Reverse (Horizontal Flip)",
                "help": "Mirror the image horizontally. Persisted on the camera.",
                "state_key": "lr_reverse",
                "default": False,
                "setup": False,
            },
            "picture_flip": {
                "type": "boolean",
                "label": "Picture Flip (Vertical)",
                "help": (
                    "Flip the image vertically — use when ceiling-mounting. "
                    "Persisted on the camera."
                ),
                "state_key": "picture_flip",
                "default": False,
                "setup": False,
            },
        },
        # Parameterless commissioning actions surfaced as one-tap buttons on the
        # device card.
        "quick_actions": ["pt_home", "wb_one_push_trigger", "focus_one_push"],
        "commands": {
            # Power
            "power_on": {
                "label": "Power On",
                "params": {},
                "help": "Wake the camera from standby.",
            },
            "power_off": {
                "label": "Power Off",
                "params": {},
                "help": "Put the camera into standby.",
            },
            # Pan/Tilt drive
            "pt_up": {
                "label": "Pan/Tilt Up",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Start tilting up. Send 'pt_stop' to halt.",
            },
            "pt_down": {
                "label": "Pan/Tilt Down",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Start tilting down. Send 'pt_stop' to halt.",
            },
            "pt_left": {
                "label": "Pan/Tilt Left",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Start panning left. Send 'pt_stop' to halt.",
            },
            "pt_right": {
                "label": "Pan/Tilt Right",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": "Start panning right. Send 'pt_stop' to halt.",
            },
            "pt_up_left": {
                "label": "Pan/Tilt Up-Left",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
            },
            "pt_up_right": {
                "label": "Pan/Tilt Up-Right",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
            },
            "pt_down_left": {
                "label": "Pan/Tilt Down-Left",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
            },
            "pt_down_right": {
                "label": "Pan/Tilt Down-Right",
                "params": {
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
            },
            "pt_stop": {
                "label": "Pan/Tilt Stop",
                "params": {},
                "help": "Halt continuous pan/tilt movement.",
            },
            "pt_home": {
                "label": "Pan/Tilt Home",
                "params": {},
                "help": "Move to the home position (typically pan=0, tilt=0).",
            },
            "pt_reset": {
                "label": "Pan/Tilt Reset",
                "params": {},
                "help": "Re-initialize the pan/tilt motors. Camera may briefly cycle.",
            },
            "pt_absolute": {
                "label": "Pan/Tilt Absolute",
                "params": {
                    "pan": {
                        "type": "integer",
                        "required": True,
                        "min": -32768,
                        "max": 32767,
                    },
                    "tilt": {
                        "type": "integer",
                        "required": True,
                        "min": -32768,
                        "max": 32767,
                    },
                    "pan_speed": {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 20},
                },
                "help": (
                    "Move to a specific pan / tilt position. Position "
                    "values are 16-bit signed; range varies by model "
                    "(typically ±2,448 horizontal, ±1,296 vertical for "
                    "Move-series cameras)."
                ),
            },
            # Zoom
            "zoom_in": {
                "label": "Zoom In",
                "params": {
                    "speed": {"type": "integer", "min": 0, "max": 7},
                },
                "help": (
                    "Start zooming in (telephoto). Optional speed 0–7 "
                    "(default standard speed). Send 'zoom_stop' to halt."
                ),
            },
            "zoom_out": {
                "label": "Zoom Out",
                "params": {
                    "speed": {"type": "integer", "min": 0, "max": 7},
                },
                "help": "Start zooming out (wide).",
            },
            "zoom_stop": {
                "label": "Zoom Stop",
                "params": {},
            },
            "zoom_direct": {
                "label": "Zoom Direct",
                "params": {
                    "position": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 16384,
                    },
                },
                "help": (
                    "Move zoom to a specific position. 0 = full wide, "
                    "16384 (0x4000) = full telephoto."
                ),
            },
            # Focus
            "focus_auto": {
                "label": "Auto Focus",
                "params": {},
            },
            "focus_manual": {
                "label": "Manual Focus",
                "params": {},
            },
            "focus_far": {
                "label": "Focus Far",
                "params": {
                    "speed": {"type": "integer", "min": 0, "max": 7},
                },
                "help": "Start focusing toward infinity. 'focus_stop' to halt.",
            },
            "focus_near": {
                "label": "Focus Near",
                "params": {
                    "speed": {"type": "integer", "min": 0, "max": 7},
                },
                "help": "Start focusing closer. 'focus_stop' to halt.",
            },
            "focus_stop": {
                "label": "Focus Stop",
                "params": {},
            },
            "focus_one_push": {
                "label": "One-Push Auto Focus",
                "params": {},
                "help": "Trigger one-shot AF without leaving manual mode.",
            },
            "focus_lock": {
                "label": "Focus Lock",
                "params": {},
                "help": "Lock current focus state, ignoring AF triggers.",
            },
            "focus_unlock": {
                "label": "Focus Unlock",
                "params": {},
            },
            "focus_recalibrate": {
                "label": "Re-Calibrate Focus",
                "params": {},
                "help": "Re-train focus capabilities. Camera may take several seconds.",
            },
            # Presets
            "recall_preset": {
                "label": "Recall Preset",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 127,
                    },
                },
                "help": "Move to a saved preset position.",
            },
            "save_preset": {
                "label": "Save Preset",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 127,
                    },
                },
                "help": "Save the current pan / tilt / zoom / focus to a preset.",
            },
            "reset_preset": {
                "label": "Delete Preset",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 127,
                    },
                },
                "help": "Erase a saved preset.",
            },
            "set_preset_speed": {
                "label": "Set Preset Recall Speed",
                "params": {
                    "speed": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 24,
                    },
                },
                "help": "Set the speed (1–24) used for subsequent preset recalls.",
            },
            # Image / display
            "set_flip": {
                "label": "Set Image Flip",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["off", "h", "v", "hv"],
                    },
                },
                "help": "Single-command image flip — off, horizontal, vertical, or both.",
            },
            "set_lr_reverse": {
                "label": "Set L/R Reverse",
                "params": {
                    "enabled": {"type": "boolean", "required": True},
                },
                "help": "Toggle horizontal mirror only.",
            },
            "set_picture_flip": {
                "label": "Set Picture Flip",
                "params": {
                    "enabled": {"type": "boolean", "required": True},
                },
                "help": "Toggle vertical flip only (use when ceiling-mounting).",
            },
            "set_tally": {
                "label": "Set Tally Light",
                "params": {
                    "state": {
                        "type": "enum",
                        "required": True,
                        "values": ["off", "on", "flashing"],
                    },
                },
                "help": (
                    "Control the front tally LED. 'on' = always lit, "
                    "'flashing' = blinking, 'off' = normal (returns "
                    "control to camera firmware)."
                ),
            },
            # Exposure
            "set_ae_mode": {
                "label": "Set Exposure Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": [
                            "full_auto", "manual", "shutter", "iris", "bright"
                        ],
                    },
                },
            },
            "set_backlight": {
                "label": "Set Backlight Compensation",
                "params": {
                    "enabled": {"type": "boolean", "required": True},
                },
            },
            # White balance
            "set_wb_mode": {
                "label": "Set White Balance Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": [
                            "auto", "indoor", "outdoor",
                            "one_push", "manual", "color_temp",
                        ],
                    },
                },
            },
            "wb_one_push_trigger": {
                "label": "One-Push WB Trigger",
                "params": {},
                "help": "Trigger one-shot white-balance calibration (use in 'one_push' WB mode).",
            },
            # Misc
            "save_settings": {
                "label": "Save Settings",
                "params": {},
                "help": "Save current camera settings as the new defaults.",
            },
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._inquiry_lock: asyncio.Lock | None = None
        self._inquiry_future: asyncio.Future[bytes] | None = None

    def _resolve_delimiter(self) -> bytes:
        # Every VISCA packet ends with 0xFF — frame on it.
        return b"\xff"

    async def _initial_sync(self) -> None:
        self._inquiry_lock = asyncio.Lock()
        # Initial poll so state populates before the first poll cycle.
        try:
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

    async def _close_session(self) -> None:
        # Drop any pending inquiry future so callers don't hang.
        if self._inquiry_future and not self._inquiry_future.done():
            self._inquiry_future.cancel()
        self._inquiry_future = None

    # ── Receive ──

    async def on_data_received(self, data: bytes) -> None:
        # Frame parser strips the trailing 0xFF, so `data` is the
        # VISCA packet body (e.g. b"\x90\x50\x02" for an inquiry reply).
        if len(data) < 2 or data[0] != 0x90:
            return
        second = data[1] & 0xF0
        if second == 0x40:
            # ACK — fire-and-forget; nothing to do.
            return
        if second == 0x60:
            # Error reply
            err = data[2] if len(data) > 2 else 0
            log.warning(
                f"[{self.device_id}] VISCA error {err:#04x} "
                f"(packet {data.hex()})"
            )
            if self._inquiry_future and not self._inquiry_future.done():
                self._inquiry_future.set_result(b"")
            return
        if second == 0x50:
            # Completion or inquiry reply.
            if len(data) > 2 and self._inquiry_future and not self._inquiry_future.done():
                # Reply with payload — hand to the waiting inquiry.
                self._inquiry_future.set_result(data)
            # else: empty completion for an action command — ignore.

    # ── Send / inquire ──

    async def _send_visca(self, payload: bytes) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(payload)

    async def _inquire(self, payload: bytes, timeout: float = 1.5) -> bytes | None:
        """Send an inquiry and wait for its `90 5y <data> FF` reply."""
        if not self._inquiry_lock:
            return None
        async with self._inquiry_lock:
            loop = asyncio.get_running_loop()
            self._inquiry_future = loop.create_future()
            try:
                await self._send_visca(payload)
                try:
                    reply = await asyncio.wait_for(
                        self._inquiry_future, timeout=timeout
                    )
                except asyncio.TimeoutError:
                    return None
                return reply or None
            except (ConnectionError, OSError):
                return None
            finally:
                self._inquiry_future = None

    # ── Commands ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        # Pan/Tilt drive (continuous movement)
        if command in _PT_DIR:
            pan_dir, tilt_dir = _PT_DIR[command]
            pan_speed = self._pan_speed(params)
            tilt_speed = self._tilt_speed(params)
            payload = (
                b"\x81\x01\x06\x01"
                + bytes([pan_speed, tilt_speed, pan_dir, tilt_dir])
                + b"\xff"
            )
            await self._send_visca(payload)
            return

        match command:
            case "power_on":
                await self._send_visca(b"\x81\x01\x04\x00\x02\xff")
            case "power_off":
                await self._send_visca(b"\x81\x01\x04\x00\x03\xff")

            case "pt_home":
                await self._send_visca(b"\x81\x01\x06\x04\xff")
            case "pt_reset":
                await self._send_visca(b"\x81\x01\x06\x05\xff")

            case "pt_absolute":
                pan = int(params["pan"])
                tilt = int(params["tilt"])
                pan_speed = self._pan_speed(params)
                tilt_speed = self._tilt_speed(params)
                payload = (
                    b"\x81\x01\x06\x02"
                    + bytes([pan_speed, tilt_speed])
                    + _encode_4nibble(pan)
                    + _encode_4nibble(tilt)
                    + b"\xff"
                )
                await self._send_visca(payload)

            case "zoom_in":
                speed = params.get("speed")
                if speed is None:
                    await self._send_visca(b"\x81\x01\x04\x07\x02\xff")
                else:
                    s = max(0, min(7, int(speed)))
                    await self._send_visca(
                        bytes([0x81, 0x01, 0x04, 0x07, 0x20 | s, 0xFF])
                    )
            case "zoom_out":
                speed = params.get("speed")
                if speed is None:
                    await self._send_visca(b"\x81\x01\x04\x07\x03\xff")
                else:
                    s = max(0, min(7, int(speed)))
                    await self._send_visca(
                        bytes([0x81, 0x01, 0x04, 0x07, 0x30 | s, 0xFF])
                    )
            case "zoom_stop":
                await self._send_visca(b"\x81\x01\x04\x07\x00\xff")
            case "zoom_direct":
                pos = max(0, min(0x4000, int(params["position"])))
                await self._send_visca(
                    b"\x81\x01\x04\x47" + _encode_4nibble(pos) + b"\xff"
                )

            case "focus_auto":
                await self._send_visca(b"\x81\x01\x04\x38\x02\xff")
            case "focus_manual":
                await self._send_visca(b"\x81\x01\x04\x38\x03\xff")
            case "focus_far":
                speed = params.get("speed")
                if speed is None:
                    await self._send_visca(b"\x81\x01\x04\x08\x02\xff")
                else:
                    s = max(0, min(7, int(speed)))
                    await self._send_visca(
                        bytes([0x81, 0x01, 0x04, 0x08, 0x20 | s, 0xFF])
                    )
            case "focus_near":
                speed = params.get("speed")
                if speed is None:
                    await self._send_visca(b"\x81\x01\x04\x08\x03\xff")
                else:
                    s = max(0, min(7, int(speed)))
                    await self._send_visca(
                        bytes([0x81, 0x01, 0x04, 0x08, 0x30 | s, 0xFF])
                    )
            case "focus_stop":
                await self._send_visca(b"\x81\x01\x04\x08\x00\xff")
            case "focus_one_push":
                await self._send_visca(b"\x81\x01\x04\x18\x01\xff")
            case "focus_lock":
                await self._send_visca(b"\x81\x0a\x04\x68\x02\xff")
            case "focus_unlock":
                await self._send_visca(b"\x81\x0a\x04\x68\x03\xff")
            case "focus_recalibrate":
                await self._send_visca(b"\x81\x0a\x01\x03\x10\xff")

            case "recall_preset":
                num = max(0, min(127, int(params["number"])))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x3F, 0x02, num, 0xFF])
                )
            case "save_preset":
                num = max(0, min(127, int(params["number"])))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x3F, 0x01, num, 0xFF])
                )
            case "reset_preset":
                num = max(0, min(127, int(params["number"])))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x3F, 0x00, num, 0xFF])
                )
            case "set_preset_speed":
                speed = max(1, min(24, int(params["speed"])))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x06, 0x01, speed, 0xFF])
                )

            case "set_flip":
                mode = str(params["mode"])
                b = _FLIP_TO_BYTE.get(mode, 0x00)
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0xA4, b, 0xFF])
                )
                self.set_state("flip", mode)
            case "set_lr_reverse":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x04\x61" + (b"\x02" if on else b"\x03") + b"\xff"
                )
                self.set_state("lr_reverse", on)
            case "set_picture_flip":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x04\x66" + (b"\x02" if on else b"\x03") + b"\xff"
                )
                self.set_state("picture_flip", on)

            case "set_tally":
                state = str(params["state"])
                p = _TALLY_TO_BYTE.get(state, 0x03)
                await self._send_visca(
                    bytes([0x81, 0x0A, 0x02, 0x02, p, 0xFF])
                )

            case "set_ae_mode":
                mode = str(params["mode"])
                b = _AE_MODE_TO_BYTE.get(mode)
                if b is None:
                    raise ValueError(f"Unknown AE mode: {mode}")
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x39, b, 0xFF])
                )
                self.set_state("ae_mode", mode)
            case "set_backlight":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x04\x33" + (b"\x02" if on else b"\x03") + b"\xff"
                )
                self.set_state("backlight", on)

            case "set_wb_mode":
                mode = str(params["mode"])
                b = _WB_MODE_TO_BYTE.get(mode)
                if b is None:
                    raise ValueError(f"Unknown WB mode: {mode}")
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x35, b, 0xFF])
                )
                self.set_state("wb_mode", mode)
            case "wb_one_push_trigger":
                await self._send_visca(b"\x81\x01\x04\x10\x05\xff")

            case "save_settings":
                await self._send_visca(b"\x81\x01\x04\xa5\x10\xff")

            case _:
                raise ValueError(f"Unknown command: {command}")

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting. Routes to the matching transient command so
        the byte-building lives in one place; the platform handles the offline
        pending queue and the read-back comes from poll()."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if key == "ae_mode":
            await self.send_command("set_ae_mode", {"mode": str(value)})
        elif key == "wb_mode":
            await self.send_command("set_wb_mode", {"mode": str(value)})
        elif key == "flip":
            await self.send_command("set_flip", {"mode": str(value)})
        elif key in ("backlight", "lr_reverse", "picture_flip"):
            on = (
                value if isinstance(value, bool)
                else str(value).strip().lower() in ("1", "true", "on", "yes")
            )
            await self.send_command(f"set_{key}", {"enabled": on})
        else:
            raise ValueError(f"Unknown device setting: {key}")

    # ── Polling ──

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return

        # Pan/Tilt position — `81 09 06 12 FF` → `90 50 0w 0w 0w 0w 0z 0z 0z 0z FF`
        reply = await self._inquire(b"\x81\x09\x06\x12\xff")
        if reply and len(reply) == 10:
            self.set_state("pan_position", _decode_4nibble(reply[2:6], signed=True))
            self.set_state("tilt_position", _decode_4nibble(reply[6:10], signed=True))

        # Zoom — `81 09 04 47 FF` → `90 50 0p 0q 0r 0s FF`
        reply = await self._inquire(b"\x81\x09\x04\x47\xff")
        if reply and len(reply) == 6:
            self.set_state("zoom_position", _decode_4nibble(reply[2:6]))

        # Focus position — `81 09 04 48 FF`
        reply = await self._inquire(b"\x81\x09\x04\x48\xff")
        if reply and len(reply) == 6:
            self.set_state("focus_position", _decode_4nibble(reply[2:6]))

        # Focus mode — `81 09 04 38 FF` → `90 50 02|03 FF`
        reply = await self._inquire(b"\x81\x09\x04\x38\xff")
        if reply and len(reply) == 3:
            self.set_state("focus_mode", "auto" if reply[2] == 0x02 else "manual")

        # AE mode — `81 09 04 39 FF`
        reply = await self._inquire(b"\x81\x09\x04\x39\xff")
        if reply and len(reply) == 3:
            mode = _BYTE_TO_AE_MODE.get(reply[2])
            if mode:
                self.set_state("ae_mode", mode)

        # WB mode — `81 09 04 35 FF`
        reply = await self._inquire(b"\x81\x09\x04\x35\xff")
        if reply and len(reply) == 3:
            mode = _BYTE_TO_WB_MODE.get(reply[2])
            if mode:
                self.set_state("wb_mode", mode)

        # Backlight — `81 09 04 33 FF`
        reply = await self._inquire(b"\x81\x09\x04\x33\xff")
        if reply and len(reply) == 3:
            self.set_state("backlight", reply[2] == 0x02)

        # Flip — `81 09 04 A4 FF`
        reply = await self._inquire(b"\x81\x09\x04\xa4\xff")
        if reply and len(reply) == 3:
            mode = _BYTE_TO_FLIP.get(reply[2])
            if mode:
                self.set_state("flip", mode)

        # L/R reverse — `81 09 04 61 FF`
        reply = await self._inquire(b"\x81\x09\x04\x61\xff")
        if reply and len(reply) == 3:
            self.set_state("lr_reverse", reply[2] == 0x02)

        # Picture flip — `81 09 04 66 FF`
        reply = await self._inquire(b"\x81\x09\x04\x66\xff")
        if reply and len(reply) == 3:
            self.set_state("picture_flip", reply[2] == 0x02)

    # ── Helpers ──

    def _pan_speed(self, params: dict[str, Any]) -> int:
        speed = params.get("pan_speed")
        if speed is None:
            speed = self.config.get("pan_speed", 12)
        return max(1, min(24, int(speed)))

    def _tilt_speed(self, params: dict[str, Any]) -> int:
        speed = params.get("tilt_speed")
        if speed is None:
            speed = self.config.get("tilt_speed", 10)
        return max(1, min(20, int(speed)))
