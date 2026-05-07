"""
OpenAVC Generic VISCA-over-IP PTZ Camera Driver.

Controls Sony-specification VISCA-over-IP PTZ cameras via UDP port 52381.
This is the canonical wire format documented in Sony's BRC/SRG VISCA
Command List and adopted by Sony, AVer, Marshall, Lumens, and many other
pro PTZ cameras as their network control protocol.

Wire format
-----------
Each datagram is an 8-byte VISCA-over-IP header followed by 1-16 bytes
of VISCA payload:

  Bytes 0-1: payload_type (big-endian)
    0x0100 = VISCA command
    0x0110 = VISCA inquiry
    0x0111 = VISCA reply (peripheral -> controller)
    0x0120 = VISCA device setting command
    0x0200 = Control command
    0x0201 = Control reply
  Bytes 2-3: payload_length (big-endian, 1..16)
  Bytes 4-7: sequence_number (big-endian, 32-bit)
  Bytes 8+ : VISCA packet (e.g. b"\\x81\\x01\\x04\\x00\\x02\\xff" for power on)

VISCA address bytes are locked: controller=0, peripheral=1, so commands
always start with 0x81 (b"\\x81") and replies always with 0x90 (b"\\x90").

On connect the driver sends a Control RESET (payload_type 0x0200, payload
0x01) to clear the camera's sequence-number state, then waits for the
0x0201 ACK reply before issuing further commands.

Push vs poll
------------
VISCA inquiry/reply is request/response. There is no subscription or
push mechanism in the Sony spec (see "Communication Method of VISCA
over IP" in the command list). Driver polls the camera's pan/tilt/zoom/
focus/AE/WB/power state at the configured interval (default 5 s).

Differences from `ptzoptics`
----------------------------
PTZOptics uses raw VISCA over TCP on port 5678 with no IP wrapper. AVer,
Sony SRG/BRC, Marshall, and Lumens use this wrapped UDP variant. The
VISCA *command* bytes are identical between the two — what differs is
the framing (raw TCP stream vs. wrapped UDP datagram), the transport
(TCP vs. UDP), and the port (5678 vs. 52381). Use the dedicated
`ptzoptics` driver for PTZOptics cameras and this driver for everything
else that speaks Sony-spec VISCA-over-IP.

Source: Sony "Color Video Camera VISCA Command List" R5915531-equivalent,
covering BRC-X400/X401, SRG-X400/X402/201M2, SRG-X120/HD1M2 (Software
Version 2.00). PDF: cached at
``driver-roadmap/reference-docs/sony-visca-over-ip.pdf``.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)


# ── VISCA-over-IP wire constants ──

PAYLOAD_VISCA_COMMAND = 0x0100
PAYLOAD_VISCA_INQUIRY = 0x0110
PAYLOAD_VISCA_REPLY = 0x0111
PAYLOAD_VISCA_DEVSET = 0x0120
PAYLOAD_CONTROL_CMD = 0x0200
PAYLOAD_CONTROL_REPLY = 0x0201

CONTROL_RESET = b"\x01"


def _wrap(payload: bytes, payload_type: int, sequence: int) -> bytes:
    """Wrap a VISCA payload in the 8-byte VISCA-over-IP header."""
    return struct.pack(">HHI", payload_type, len(payload), sequence) + payload


def _unwrap(packet: bytes) -> tuple[int, int, bytes] | None:
    """Strip the 8-byte VISCA-over-IP header. Returns (type, seq, payload).

    Returns None if the packet is too short or the declared length doesn't match.
    """
    if len(packet) < 9:
        return None
    payload_type, payload_length, sequence = struct.unpack(">HHI", packet[:8])
    payload = packet[8:]
    if len(payload) != payload_length:
        return None
    return payload_type, sequence, payload


# ── VISCA byte helpers (shared shape with ptzoptics) ──

def _encode_4nibble(value: int) -> bytes:
    """Encode an int into four 4-bit-per-byte VISCA position nibbles, MSB first.

    A signed 16-bit position becomes 4 bytes of low-nibble data — this is the
    classic VISCA position encoding used for pan/tilt/zoom/focus targets.
    """
    v = value & 0xFFFF
    return bytes(
        [
            (v >> 12) & 0x0F,
            (v >> 8) & 0x0F,
            (v >> 4) & 0x0F,
            v & 0x0F,
        ]
    )


def _decode_4nibble(data: bytes, signed: bool = False) -> int:
    """Decode four VISCA position nibbles into an integer."""
    v = (
        ((data[0] & 0x0F) << 12)
        | ((data[1] & 0x0F) << 8)
        | ((data[2] & 0x0F) << 4)
        | (data[3] & 0x0F)
    )
    if signed and v >= 0x8000:
        v -= 0x10000
    return v


# ── Pan/Tilt direction nibble pairs ──

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

_AE_MODE_TO_BYTE = {
    "full_auto": 0x00,
    "manual":    0x03,
    "shutter":   0x0A,
    "iris":      0x0B,
    "bright":    0x0D,
}
_BYTE_TO_AE_MODE = {v: k for k, v in _AE_MODE_TO_BYTE.items()}

_WB_MODE_TO_BYTE = {
    "auto1":    0x00,
    "indoor":   0x01,
    "outdoor":  0x02,
    "one_push": 0x03,
    "auto2":    0x04,
    "manual":   0x05,
}
_BYTE_TO_WB_MODE = {v: k for k, v in _WB_MODE_TO_BYTE.items()}


class VISCAIPDriver(BaseDriver):
    """Sony-spec VISCA-over-IP PTZ camera driver."""

    DRIVER_INFO = {
        "id": "visca_ip",
        "name": "Generic VISCA-IP PTZ Camera",
        "manufacturer": "Generic",
        "category": "camera",
        "version": "1.2.0",
        "author": "OpenAVC",
        "min_platform_version": "0.10.3",
        "description": (
            "Generic Sony-specification VISCA-over-IP driver (UDP port "
            "52381). Pan/tilt/zoom/focus, presets, auto-exposure, white "
            "balance, backlight, power. Covers Sony SRG/BRC, AVer, "
            "Marshall, Lumens and many other pro PTZ cameras that adopt "
            "Sony's wire format. PTZOptics uses a different framing on "
            "TCP/5678 -- prefer the dedicated `ptzoptics` driver for "
            "those."
        ),
        "source_url": (
            "https://shop.ccisolutions.com/StoreFront/jsp/pdf/"
            "SON-SRGX400_visca_commandList.pdf"
        ),
        "tags": ["visca", "visca-ip", "ptz", "camera", "generic", "sony"],
        "verified": False,
        "simulated": True,
        "protocols": ["visca-ip"],
        "ports": [52381],
        "transport": "udp",
        "discovery": {
            # Generic VISCA-over-IP fallback. Brand-specific drivers
            # (sony_visca, ptzoptics, panasonic_awhe, etc.) claim ONVIF
            # or vendor-specific signals — this driver intentionally
            # stays manual_only as the catch-all for cameras that
            # speak Sony's wire format but don't have a dedicated
            # driver. Vendor aliases match the broad family.
            "manual_only": True,
            "vendor_aliases": [
                "sony", "aver", "marshall", "lumens", "visca",
            ],
        },
        "compatible_models": [
            {
                "manufacturer": "Generic",
                "models": ["Any Sony-spec VISCA-over-IP PTZ camera (UDP 52381)"],
                "confidence": "untested",
                "notes": (
                    "Sony's VISCA-over-IP wire format is the de-facto cross-vendor "
                    "standard for pro PTZ cameras. Brand-specific drivers expose "
                    "more (vendor-specific image presets, preset-name strings, "
                    "OSD/menu, lens controls) and should be preferred when "
                    "available."
                ),
            },
            {
                "manufacturer": "Sony",
                "models": [
                    "EVI-H100V / H100S",
                ],
                "confidence": "untested",
                "notes": (
                    "Older Sony EVI cameras that speak Sony-spec VISCA-IP but "
                    "predate the BRC/SRG picture-profile / PRESET MODE / PTZ "
                    "TRACE / tally surface. For SRG-X400/X402/201M2, "
                    "SRG-X120/HD1M2, and BRC-X400/X401/H800/H900 use the "
                    "dedicated `sony_visca` driver instead -- it adds picture "
                    "profiles, R/B gain, color matrix, defog, PRESET MODE, "
                    "PTZ TRACE, tally, IR correction, AF mode, AE speed, "
                    "min/max shutter, high sensitivity, and OSD."
                ),
            },
            {
                "manufacturer": "AVer",
                "models": [
                    "CAM520 Pro / Pro2 / Pro3",
                    "CAM530",
                    "CAM540 / 550",
                    "CAM570",
                    "PTC500S",
                    "TR313 / TR315 / TR320 / TR530",
                    "VC520 Pro / Pro2 / Pro3",
                    "VB342+ / VB342 Pro / VB350",
                    "FONE700",
                ],
                "confidence": "untested",
                "notes": (
                    "AVer's published 'VISCA over IP' protocol guide uses UDP "
                    "52381 with the Sony 8-byte header (verified against AVer's "
                    "VISCA Specification 3.3). For the PTZ310 / PTZ330 family "
                    "use the dedicated `aver_ptz` driver instead -- it adds "
                    "AVer's HTTP CGI surface (image quality, SmartShoot, "
                    "SmartFraming, RTMP, bulk status) on top of the VISCA-IP "
                    "wire format. Other AVer cameras don't have a comparably "
                    "documented HTTP CGI guide and continue here."
                ),
            },
            {
                "manufacturer": "Marshall",
                "models": [
                    "CV620-NDI / CV620-IP / CV620-BI",
                    "CV730-NDI / CV730-IP",
                    "CV730-BHN / BHN-7",
                ],
                "confidence": "untested",
                "notes": (
                    "Marshall's CV-series broadcast PTZ cameras list VISCA over "
                    "IP as a control method in their installation manuals."
                ),
            },
        ],
        "help": {
            "overview": (
                "Generic Sony-specification VISCA-over-IP PTZ control on UDP "
                "port 52381. Supports the universal pan/tilt/zoom/focus + "
                "preset + AE/WB command surface. Where a brand-specific "
                "driver exists in the catalog, that driver covers more and "
                "is preferred."
            ),
            "setup": (
                "1. Set the camera to a static IP and place it on a routable "
                "VLAN to the OpenAVC server.\n"
                "2. Enable VISCA over IP on the camera (often labeled "
                "'VISCA over IP' or 'IP CONTROL' in the network menu). The "
                "default port 52381 should not need to change.\n"
                "3. Confirm the camera is set to UDP — not TCP. Some cameras "
                "let you choose; this driver speaks UDP only.\n"
                "4. Note: VISCA over IP supports up to 5 simultaneous "
                "controllers per camera, so multiple control surfaces "
                "(OpenAVC + a hardware joystick + a Bitfocus Companion "
                "page, etc.) can co-exist."
            ),
        },
        "default_config": {
            "host": "",
            "port": 52381,
            "pan_speed": 12,
            "tilt_speed": 10,
            "poll_interval": 5,
            "inter_command_delay": 0.05,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "default": 52381,
                "label": "UDP Port",
                "description": (
                    "VISCA-over-IP standard port. Don't change unless your "
                    "camera was deliberately moved off it."
                ),
            },
            "pan_speed": {
                "type": "integer",
                "default": 12,
                "min": 1,
                "max": 24,
                "label": "Default Pan Speed (1-24)",
            },
            "tilt_speed": {
                "type": "integer",
                "default": 10,
                "min": 1,
                "max": 23,
                "label": "Default Tilt Speed (1-23)",
            },
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often to re-query camera state. Set to 0 to disable "
                    "polling entirely (PTZ commands still work)."
                ),
            },
            "inter_command_delay": {
                "type": "number",
                "default": 0.05,
                "min": 0.0,
                "label": "Inter-command Delay (sec)",
                "description": (
                    "Minimum delay between outbound packets. 50 ms keeps fast "
                    "joystick input from outpacing the camera's command buffer."
                ),
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["on", "standby"],
                "label": "Power State",
            },
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
                "values": ["auto1", "indoor", "outdoor", "one_push", "auto2", "manual"],
                "label": "White Balance Mode",
            },
            "backlight": {
                "type": "boolean",
                "label": "Backlight Compensation",
            },
        },
        "commands": {
            "power_on":  {"label": "Power On",  "params": {}},
            "power_off": {"label": "Power Off (Standby)", "params": {}},

            # PT continuous drive
            **{
                cmd: {
                    "label": cmd.replace("pt_", "Pan/Tilt ").replace("_", " ").title(),
                    "params": {
                        "pan_speed":  {"type": "integer", "min": 1, "max": 24},
                        "tilt_speed": {"type": "integer", "min": 1, "max": 23},
                    },
                    "help": "Continuous-movement command. Send 'pt_stop' to halt.",
                }
                for cmd in _PT_DIR
            },

            "pt_home":  {"label": "Pan/Tilt Home",  "params": {}},
            "pt_reset": {"label": "Pan/Tilt Reset", "params": {}},
            "pt_absolute": {
                "label": "Pan/Tilt Absolute",
                "params": {
                    "pan":  {"type": "integer", "required": True, "min": -32768, "max": 32767},
                    "tilt": {"type": "integer", "required": True, "min": -32768, "max": 32767},
                    "pan_speed":  {"type": "integer", "min": 1, "max": 24},
                    "tilt_speed": {"type": "integer", "min": 1, "max": 23},
                },
            },

            # Zoom
            "zoom_tele": {
                "label": "Zoom Tele (In)",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7}},
                "help": "Variable-speed zoom in. Send 'zoom_stop' to halt. Omit speed for standard.",
            },
            "zoom_wide": {
                "label": "Zoom Wide (Out)",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7}},
                "help": "Variable-speed zoom out. Send 'zoom_stop' to halt.",
            },
            "zoom_stop": {"label": "Zoom Stop", "params": {}},
            "zoom_direct": {
                "label": "Zoom Direct",
                "params": {"position": {"type": "integer", "required": True, "min": 0, "max": 16384}},
            },

            # Focus
            "focus_far": {
                "label": "Focus Far",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7}},
                "help": "Variable-speed focus farther. Send 'focus_stop' to halt.",
            },
            "focus_near": {
                "label": "Focus Near",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7}},
                "help": "Variable-speed focus closer. Send 'focus_stop' to halt.",
            },
            "focus_stop": {"label": "Focus Stop", "params": {}},
            "focus_direct": {
                "label": "Focus Direct",
                "params": {"position": {"type": "integer", "required": True, "min": 0, "max": 65535}},
            },
            "focus_mode_auto":   {"label": "Focus Mode: Auto",   "params": {}},
            "focus_mode_manual": {"label": "Focus Mode: Manual", "params": {}},
            "focus_one_push": {
                "label": "Focus One-Push Trigger",
                "params": {},
                "help": "Trigger one-shot autofocus from manual mode.",
            },

            # Presets (0-99)
            "preset_recall": {
                "label": "Preset Recall",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 99}},
            },
            "preset_set": {
                "label": "Preset Save",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 99}},
                "help": "Store the current PTZ position into the given preset slot.",
            },
            "preset_reset": {
                "label": "Preset Reset (Erase)",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 99}},
            },

            # Exposure
            "set_ae_mode": {
                "label": "Set Auto-Exposure Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["full_auto", "manual", "shutter", "iris", "bright"],
                    },
                },
            },
            "set_backlight": {
                "label": "Set Backlight Compensation",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },

            # White balance
            "set_wb_mode": {
                "label": "Set White Balance Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["auto1", "indoor", "outdoor", "one_push", "auto2", "manual"],
                    },
                },
            },
            "wb_one_push_trigger": {
                "label": "One-Push WB Trigger",
                "params": {},
                "help": "Trigger one-shot white balance (use in 'one_push' WB mode).",
            },
        },
    }

    # Polling cadence is honored by BaseDriver via `poll_interval` config.
    # The driver implements `poll()` below.

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sequence = 0
        self._inquiry_lock: asyncio.Lock | None = None
        self._inquiry_future: asyncio.Future[bytes] | None = None
        self._control_reply_future: asyncio.Future[None] | None = None

    async def connect(self) -> None:
        await super().connect()
        self._inquiry_lock = asyncio.Lock()
        self._sequence = 0

        # Send Control RESET to sync the camera's sequence-number state. The
        # camera replies with 0x0201 ACK (payload 0x01) when accepted.
        try:
            await self._send_control_reset()
        except (ConnectionError, OSError, asyncio.TimeoutError):
            log.warning(
                f"[{self.device_id}] VISCA-IP RESET handshake did not "
                "complete; continuing optimistically"
            )

        # Initial poll so state populates immediately.
        try:
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

    async def disconnect(self) -> None:
        if self._inquiry_future and not self._inquiry_future.done():
            self._inquiry_future.cancel()
        self._inquiry_future = None
        if self._control_reply_future and not self._control_reply_future.done():
            self._control_reply_future.cancel()
        self._control_reply_future = None
        await super().disconnect()

    # ── Receive ──

    async def on_data_received(self, data: bytes) -> None:
        """Strip the VISCA-over-IP header and dispatch by payload type."""
        unwrapped = _unwrap(data)
        if unwrapped is None:
            log.debug(f"[{self.device_id}] dropped malformed packet: {data.hex()}")
            return
        payload_type, _seq, payload = unwrapped

        if payload_type == PAYLOAD_CONTROL_REPLY:
            # Camera ACK for our RESET (or error).
            if (
                self._control_reply_future
                and not self._control_reply_future.done()
            ):
                self._control_reply_future.set_result(None)
            return

        if payload_type != PAYLOAD_VISCA_REPLY:
            log.debug(
                f"[{self.device_id}] ignoring non-reply payload type "
                f"{payload_type:#06x}"
            )
            return

        if len(payload) < 2 or payload[0] != 0x90:
            return
        second = payload[1] & 0xF0
        if second == 0x40:
            # ACK — fire and forget.
            return
        if second == 0x60:
            err = payload[2] if len(payload) > 2 else 0
            log.warning(
                f"[{self.device_id}] VISCA error {err:#04x} "
                f"(packet {payload.hex()})"
            )
            if self._inquiry_future and not self._inquiry_future.done():
                self._inquiry_future.set_result(b"")
            return
        if second == 0x50:
            # Completion / inquiry reply.
            if (
                len(payload) > 2
                and self._inquiry_future
                and not self._inquiry_future.done()
            ):
                self._inquiry_future.set_result(payload)

    # ── Send / inquire ──

    def _next_seq(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        return self._sequence

    async def _send_visca(self, visca: bytes) -> None:
        """Wrap a VISCA command/inquiry payload and send."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        # Inquiries are 0x0110, commands are 0x0100. We can detect by the
        # second byte: 0x09 = inquiry, 0x01 / 0x0a = command. Simpler: callers
        # use _inquire() for inquiries which sets payload type explicitly.
        seq = self._next_seq()
        await self.transport.send(_wrap(visca, PAYLOAD_VISCA_COMMAND, seq))

    async def _send_visca_inquiry(self, visca: bytes) -> None:
        """Wrap a VISCA inquiry payload (0x0110) and send."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        seq = self._next_seq()
        await self.transport.send(_wrap(visca, PAYLOAD_VISCA_INQUIRY, seq))

    async def _send_control_reset(self, timeout: float = 2.0) -> None:
        """Send the Control RESET packet (payload_type=0x0200, payload=0x01).

        Per Sony's VISCA over IP spec, this clears the camera's sequence-number
        tracking; the camera replies with 0x0201 ACK (payload 0x01).
        """
        loop = asyncio.get_running_loop()
        self._control_reply_future = loop.create_future()
        try:
            await self.transport.send(_wrap(CONTROL_RESET, PAYLOAD_CONTROL_CMD, 0))
            await asyncio.wait_for(self._control_reply_future, timeout=timeout)
            self._sequence = 0  # Spec: subsequent commands start from 1.
            log.debug(f"[{self.device_id}] VISCA-IP RESET handshake complete")
        finally:
            self._control_reply_future = None

    async def _inquire(self, payload: bytes, timeout: float = 1.5) -> bytes | None:
        """Send an inquiry, wait for `90 5y <data> FF` reply, return payload."""
        if not self._inquiry_lock:
            return None
        async with self._inquiry_lock:
            loop = asyncio.get_running_loop()
            self._inquiry_future = loop.create_future()
            try:
                await self._send_visca_inquiry(payload)
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

        # Pan/Tilt continuous drive
        if command in _PT_DIR:
            pan_dir, tilt_dir = _PT_DIR[command]
            pan_speed = self._pan_speed(params)
            tilt_speed = self._tilt_speed(params)
            await self._send_visca(
                b"\x81\x01\x06\x01"
                + bytes([pan_speed, tilt_speed, pan_dir, tilt_dir])
                + b"\xff"
            )
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
                await self._send_visca(
                    b"\x81\x01\x06\x02"
                    + bytes([pan_speed, tilt_speed])
                    + _encode_4nibble(pan)
                    + _encode_4nibble(tilt)
                    + b"\xff"
                )

            case "zoom_tele":
                speed = params.get("speed")
                if speed is None:
                    await self._send_visca(b"\x81\x01\x04\x07\x02\xff")
                else:
                    s = max(0, min(7, int(speed)))
                    await self._send_visca(
                        bytes([0x81, 0x01, 0x04, 0x07, 0x20 | s, 0xFF])
                    )
            case "zoom_wide":
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
            case "focus_direct":
                pos = max(0, min(0xFFFF, int(params["position"])))
                await self._send_visca(
                    b"\x81\x01\x04\x48" + _encode_4nibble(pos) + b"\xff"
                )
            case "focus_mode_auto":
                await self._send_visca(b"\x81\x01\x04\x38\x02\xff")
            case "focus_mode_manual":
                await self._send_visca(b"\x81\x01\x04\x38\x03\xff")
            case "focus_one_push":
                await self._send_visca(b"\x81\x01\x04\x18\x01\xff")

            case "preset_recall":
                num = max(0, min(99, int(params["number"])))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x3F, 0x02, num, 0xFF])
                )
            case "preset_set":
                num = max(0, min(99, int(params["number"])))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x3F, 0x01, num, 0xFF])
                )
            case "preset_reset":
                num = max(0, min(99, int(params["number"])))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x3F, 0x00, num, 0xFF])
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

            case _:
                raise ValueError(f"Unknown command: {command}")

    # ── Polling ──

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return

        # Power — `81 09 04 00 FF` -> `90 50 02|03 FF`
        reply = await self._inquire(b"\x81\x09\x04\x00\xff")
        if reply and len(reply) == 4:
            self.set_state("power", "on" if reply[2] == 0x02 else "standby")

        # Pan/Tilt position
        reply = await self._inquire(b"\x81\x09\x06\x12\xff")
        if reply and len(reply) == 11:
            self.set_state("pan_position", _decode_4nibble(reply[2:6], signed=True))
            self.set_state("tilt_position", _decode_4nibble(reply[6:10], signed=True))

        # Zoom
        reply = await self._inquire(b"\x81\x09\x04\x47\xff")
        if reply and len(reply) == 7:
            self.set_state("zoom_position", _decode_4nibble(reply[2:6]))

        # Focus position
        reply = await self._inquire(b"\x81\x09\x04\x48\xff")
        if reply and len(reply) == 7:
            self.set_state("focus_position", _decode_4nibble(reply[2:6]))

        # Focus mode
        reply = await self._inquire(b"\x81\x09\x04\x38\xff")
        if reply and len(reply) == 4:
            self.set_state("focus_mode", "auto" if reply[2] == 0x02 else "manual")

        # AE mode
        reply = await self._inquire(b"\x81\x09\x04\x39\xff")
        if reply and len(reply) == 4:
            mode = _BYTE_TO_AE_MODE.get(reply[2])
            if mode:
                self.set_state("ae_mode", mode)

        # WB mode
        reply = await self._inquire(b"\x81\x09\x04\x35\xff")
        if reply and len(reply) == 4:
            mode = _BYTE_TO_WB_MODE.get(reply[2])
            if mode:
                self.set_state("wb_mode", mode)

        # Backlight
        reply = await self._inquire(b"\x81\x09\x04\x33\xff")
        if reply and len(reply) == 4:
            self.set_state("backlight", reply[2] == 0x02)

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
        return max(1, min(23, int(speed)))
