"""
OpenAVC Sony VISCA-over-IP PTZ Camera Driver.

Dedicated driver for Sony SRG / BRC / EVI PTZ cameras. Uses the standard
Sony VISCA-over-IP wrapping (UDP port 52381, 8-byte header + VISCA
payload, Control RESET handshake on connect) — same wire format the
generic ``visca_ip`` driver implements — but adds the Sony-specific
command surface that the generic deliberately leaves out:

  * Picture profile selection (STD / HIGH SAT / FL LIGHT / MOVIE / STILL /
    CINEMA / PRO / ITU709 / B&W) via ``CAM_PictureEffect`` (8x 01 7E 01 3D)
  * R/B gain direct + reset/up/down (8x 01 04 03 / 04 / 43 / 44)
  * Color matrix R-G, R-B, G-R, G-B, B-R, B-G (8x 01 7E 01 7A..7F) —
    BRC-X400/X401 only
  * Defog (BRC-X400/X401), Visibility Enhancer, Low Light Basis Brightness,
    Chroma Suppress
  * PRESET MODE selection (BRC-X400/X401): MODE1, MODE2, TRACE, plus the
    matching PTZ TRACE record/play/delete commands
  * Tally light (on/off + level), IR Correction, IR Cut Filter (ICR) +
    Auto ICR, AF Mode (Normal / Interval / Zoom Trigger)
  * AE Speed, Min/Max Shutter direct, High Sensitivity mode
  * OSD display on/off (per output: SDI / HDMI), Spotlight, Backlight,
    Exp Comp on/off + level, Flicker Cancel

The wire-format helpers (``_wrap`` / ``_unwrap`` / 4-nibble encode/decode)
are duplicated from ``visca_ip`` rather than imported because community
drivers are loaded as standalone modules — there is no package import path
between two driver files in the same directory.

Push vs poll
------------
Sony's VISCA over IP spec is purely request/response. There is no
subscription mechanism in the published Command List, so the driver polls
the camera's PTZ + camera-setting state at the configured interval
(default 5 s).

Liveness (never-offline fix)
----------------------------
UDP is connectionless, so an unplugged camera does not drop a socket — the
inquiry just stops getting answered. The power inquiry (answered by every
VISCA camera) doubles as a liveness probe: it gates connect()
(``_post_connect`` raises a no_response-worded ConnectionError if the camera
answers nothing) and leads the poll cycle (``poll()`` raises the same when
the camera goes silent). Previously the swallowed UDP timeout left an
unplugged camera shown ONLINE forever.

Why Python (Principle 9)
------------------------
Same as ``visca_ip``: binary framing with a 32-bit sequence counter,
multiple payload-type kinds, and the on-connect RESET handshake — none
of which ``ConfigurableDriver`` exposes through YAML.

Source: Sony "Color Video Camera VISCA Command List" (SRG-X400 / BRC-X400 /
SRG-X120 lineup, Software Version 2.00). PDF cached at
``driver-roadmap/reference-docs/sony-visca-over-ip.pdf``.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ── VISCA-over-IP wire constants ──

PAYLOAD_VISCA_COMMAND = 0x0100
PAYLOAD_VISCA_INQUIRY = 0x0110
PAYLOAD_VISCA_REPLY = 0x0111
PAYLOAD_CONTROL_CMD = 0x0200
PAYLOAD_CONTROL_REPLY = 0x0201

CONTROL_RESET = b"\x01"

# Liveness probe. CAM_PowerInq is answered by every VISCA camera, so it
# doubles as the "are you still there?" probe. The probe gates connect()
# (so a dead camera surfaces offline, not phantom-connected) and rides the
# poll cycle (so an unplugged camera flips offline mid-session). The
# "is not responding" wording is what the shared connection-fault classifier
# maps to offline_reason=no_response.
POWER_INQUIRY = b"\x81\x09\x04\x00\xff"
PROBE_TIMEOUT_S = 1.5
PROBE_RETRIES = 3  # connect-time tolerance for a single dropped UDP datagram
RESET_TIMEOUT_S = 2.0  # Control RESET ACK wait (best-effort; not a liveness gate)


def _wrap(payload: bytes, payload_type: int, sequence: int) -> bytes:
    return struct.pack(">HHI", payload_type, len(payload), sequence) + payload


def _unwrap(packet: bytes) -> tuple[int, int, bytes] | None:
    if len(packet) < 9:
        return None
    payload_type, payload_length, sequence = struct.unpack(">HHI", packet[:8])
    payload = packet[8:]
    if len(payload) != payload_length:
        return None
    return payload_type, sequence, payload


def _encode_4nibble(value: int) -> bytes:
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
    v = (
        ((data[0] & 0x0F) << 12)
        | ((data[1] & 0x0F) << 8)
        | ((data[2] & 0x0F) << 4)
        | (data[3] & 0x0F)
    )
    if signed and v >= 0x8000:
        v -= 0x10000
    return v


def _encode_2nibble(value: int) -> bytes:
    """Encode a byte (0..255) into two 4-bit-per-byte VISCA nibbles."""
    v = value & 0xFF
    return bytes([(v >> 4) & 0x0F, v & 0x0F])


def _decode_2nibble(data: bytes) -> int:
    return ((data[0] & 0x0F) << 4) | (data[1] & 0x0F)


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

_PICTURE_PROFILE_TO_BYTE = {
    "std":       0x02,
    "off":       0x03,
    "high_sat":  0x04,
    "fl_light":  0x05,
    "movie":     0x06,
    "still":     0x07,
    "cinema":    0x08,
    "pro":       0x09,
    "itu709":    0x0A,
    "bw":        0x0B,
}
_BYTE_TO_PICTURE_PROFILE = {v: k for k, v in _PICTURE_PROFILE_TO_BYTE.items()}

_PRESET_MODE_TO_BYTE = {
    "mode1": 0x00,
    "mode2": 0x01,
    "trace": 0x10,
}
_BYTE_TO_PRESET_MODE = {v: k for k, v in _PRESET_MODE_TO_BYTE.items()}

_AF_MODE_TO_BYTE = {
    "normal":        0x00,
    "interval":      0x01,
    "zoom_trigger":  0x02,
}
_BYTE_TO_AF_MODE = {v: k for k, v in _AF_MODE_TO_BYTE.items()}

_TALLY_LEVEL_TO_BYTE = {
    "off":      0x00,
    "on_low":   0x04,
    "on_high":  0x05,
}
_BYTE_TO_TALLY_LEVEL = {v: k for k, v in _TALLY_LEVEL_TO_BYTE.items()}

_PTZ_TRACE_STATUS = {
    0x00: "none",
    0x01: "recording",
    0x02: "preparing",
    0x03: "ready",
    0x04: "playing",
    0x05: "deleting",
}

_COLOR_MATRIX_OPS = {
    "rg": 0x7A,
    "rb": 0x7B,
    "gr": 0x7C,
    "gb": 0x7D,
    "br": 0x7E,
    "bg": 0x7F,
}


# Color-matrix value range: 00 (-99) - 63 (0) - C6 (+99). Driver accepts
# signed -99..+99 and converts to/from the byte form.
def _color_matrix_to_byte(value: int) -> int:
    return max(0, min(0xC6, int(value) + 0x63))


def _color_matrix_from_byte(byte: int) -> int:
    return max(-99, min(99, int(byte) - 0x63))


class SonyVISCADriver(BaseDriver):
    """Sony SRG / BRC / EVI VISCA-over-IP PTZ camera driver."""

    DRIVER_INFO = {
        "id": "sony_visca",
        "name": "Sony VISCA-IP PTZ Camera",
        "manufacturer": "Sony",
        "category": "camera",
        "version": "1.3.2",
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Dedicated Sony SRG / BRC / EVI VISCA-over-IP driver (UDP port "
            "52381). Adds Sony picture profiles, R/B gain, color matrix, "
            "defog, visibility enhancer, low-light, chroma suppress, "
            "PRESET MODE + PTZ TRACE (BRC-X400/X401), tally, IR correction, "
            "ICR, AF mode, AE speed, min/max shutter, high sensitivity, and "
            "OSD on/off — on top of the standard PTZ + AE/WB command set."
        ),
        "source_url": (
            "https://shop.ccisolutions.com/StoreFront/jsp/pdf/"
            "SON-SRGX400_visca_commandList.pdf"
        ),
        "tags": ["visca", "visca-ip", "ptz", "camera", "sony"],
        "verified": False,
        "simulated": True,
        "protocols": ["visca-ip"],
        "ports": [52381],
        "transport": "udp",
        "discovery": {
            # Sony PTZ cameras come up via the cross-vendor visca_ip
            # companion (TCP/10500 CAM_VersionInq) — that probe lifts
            # vendor_code 0x0020 to manufacturer="Sony" which the
            # manufacturer_alias hint here narrows to this driver.
            # Skip oui — Sony has dozens of OUI blocks across consumer /
            # pro / mobile / appliance gear; declaring them surfaces
            # every Sony PlayStation as a "possible PTZ." Hostnames are
            # the SRG / BRC / EVI families.
            "manufacturer_alias": ["sony"],
            "hostname": ["^SRG-", "^BRC-", "^EVI-"],
        },
        "compatible_models": [
            {
                "manufacturer": "Sony",
                "models": [
                    "SRG-X400 / X402 / 201M2",
                    "SRG-X120 / HD1M2",
                    "BRC-X400 / X401",
                    "BRC-H800 / H900",
                ],
                "confidence": "untested",
                "notes": (
                    "BRC-X400 / X401 expose the full feature set (picture "
                    "profiles, color matrix, defog, PRESET MODE, PTZ TRACE, "
                    "tally). SRG-X400/X402/201M2 and SRG-X120/HD1M2 share "
                    "the picture profile / R-B gain / chroma suppress / IR "
                    "correction surface but lack PRESET MODE, PTZ TRACE, "
                    "tally, color matrix and defog (commands return error "
                    "harmlessly). BRC-H800 / H900 are older but speak the "
                    "same Command List wire format."
                ),
            },
        ],
        "help": {
            "overview": (
                "Sony PTZ camera control over VISCA-IP (UDP 52381). "
                "Includes Sony's own picture profiles, color matrix, "
                "PRESET MODE / PTZ TRACE (BRC-X400/X401), tally, OSD, AF "
                "mode, AE speed, IR correction, ICR, and shutter limits."
            ),
            "setup": (
                "1. Set the camera to a static IP and put it on a routable "
                "VLAN to the OpenAVC server.\n"
                "2. Enable VISCA over IP in the camera's network menu. The "
                "default port 52381 should not need to change.\n"
                "3. Confirm the camera is set to UDP — not TCP. Sony BRC/SRG "
                "cameras default to UDP; this driver speaks UDP only.\n"
                "4. VISCA over IP supports up to 5 simultaneous controllers "
                "per camera, so OpenAVC can co-exist with hardware joysticks "
                "or other control surfaces.\n"
                "5. Picture-profile / color-matrix / defog / PRESET MODE "
                "commands only take effect on the BRC-X400/X401 — on smaller "
                "SRG models the camera silently rejects them."
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
                    "polling entirely (commands still work)."
                ),
            },
            "inter_command_delay": {
                "type": "number",
                "default": 0.05,
                "min": 0.0,
                "label": "Inter-command Delay (sec)",
                "description": (
                    "Minimum delay between outbound packets. 50 ms keeps "
                    "fast joystick input from outpacing the camera's "
                    "command buffer."
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
                "max": 31424,
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
            "spotlight": {
                "type": "boolean",
                "label": "Spotlight Compensation",
            },
            "picture_profile": {
                "type": "enum",
                "values": list(_PICTURE_PROFILE_TO_BYTE.keys()),
                "label": "Picture Profile",
            },
            "rgain": {
                "type": "integer",
                "label": "Red Gain (0-255, 128=neutral)",
                "min": 0,
                "max": 255,
            },
            "bgain": {
                "type": "integer",
                "label": "Blue Gain (0-255, 128=neutral)",
                "min": 0,
                "max": 255,
            },
            "chroma_suppress": {
                "type": "integer",
                "label": "Chroma Suppress (0=Off,1=Weak,2=Mid,3=Strong)",
                "min": 0,
                "max": 3,
            },
            "visibility_enhancer": {
                "type": "boolean",
                "label": "Visibility Enhancer",
            },
            "defog": {
                "type": "boolean",
                "label": "Defog (BRC-X400/X401)",
            },
            "defog_level": {
                "type": "integer",
                "label": "Defog Level (1-3)",
                "min": 1,
                "max": 3,
            },
            "low_light": {
                "type": "boolean",
                "label": "Low Light Basis",
            },
            "low_light_level": {
                "type": "integer",
                "label": "Low Light Basis Brightness (4-10)",
                "min": 4,
                "max": 10,
            },
            "ae_speed": {
                "type": "integer",
                "label": "AE Convergence Speed (1=fast..30=slow)",
                "min": 1,
                "max": 48,
            },
            "min_shutter": {
                "type": "integer",
                "label": "Min Shutter (raw)",
                "min": 0,
                "max": 255,
            },
            "max_shutter": {
                "type": "integer",
                "label": "Max Shutter (raw)",
                "min": 0,
                "max": 255,
            },
            "high_sensitivity": {
                "type": "boolean",
                "label": "High Sensitivity Mode",
            },
            "exp_comp": {
                "type": "boolean",
                "label": "Exposure Compensation Enabled",
            },
            "exp_comp_level": {
                "type": "integer",
                "label": "Exposure Compensation Level (0-14, 7=center)",
                "min": 0,
                "max": 14,
            },
            "flicker_cancel": {
                "type": "boolean",
                "label": "Flicker Cancel",
            },
            "ir_correction": {
                "type": "enum",
                "values": ["standard", "ir_light"],
                "label": "IR Correction",
            },
            "ir_cut_filter": {
                "type": "enum",
                "values": ["day", "night"],
                "label": "IR Cut Filter (ICR)",
            },
            "auto_icr": {
                "type": "boolean",
                "label": "Auto ICR",
            },
            "af_mode": {
                "type": "enum",
                "values": list(_AF_MODE_TO_BYTE.keys()),
                "label": "AF Mode",
            },
            "preset_mode": {
                "type": "enum",
                "values": list(_PRESET_MODE_TO_BYTE.keys()),
                "label": "PRESET MODE (BRC-X400/X401)",
            },
            "ptz_trace_status": {
                "type": "enum",
                "values": list(_PTZ_TRACE_STATUS.values()),
                "label": "PTZ TRACE Status",
            },
            "tally_level": {
                "type": "enum",
                "values": list(_TALLY_LEVEL_TO_BYTE.keys()),
                "label": "Tally Lamp",
            },
        },
        # The picture / image-quality surface is device-side configuration the
        # camera persists and reports back (every entry's state_key is
        # populated by a poll() inquiry), so it gets the editable-field +
        # offline pending-queue treatment of device settings on top of the
        # transient set_* commands (same dual surface as crestron_nvx). Live
        # operational state (PTZ/zoom/focus position, power, presets) and
        # production-driven tally are NOT settings and stay commands.
        "device_settings": {
            "picture_profile": {
                "type": "enum",
                "label": "Picture Profile",
                "help": (
                    "Sony picture-look preset. BRC-X400/X401 expose the full "
                    "list; smaller SRG models accept a subset."
                ),
                "values": list(_PICTURE_PROFILE_TO_BYTE.keys()),
                "state_key": "picture_profile",
                "default": "std",
                "setup": False,
            },
            "ae_mode": {
                "type": "enum",
                "label": "Auto-Exposure Mode",
                "help": "How the camera meters exposure.",
                "values": ["full_auto", "manual", "shutter", "iris", "bright"],
                "state_key": "ae_mode",
                "default": "full_auto",
                "setup": False,
            },
            "ae_speed": {
                "type": "integer",
                "label": "AE Convergence Speed",
                "help": "1 = fast convergence, higher = slower / smoother.",
                "min": 1,
                "max": 48,
                "state_key": "ae_speed",
                "default": 1,
                "setup": False,
            },
            "wb_mode": {
                "type": "enum",
                "label": "White Balance Mode",
                "help": (
                    "auto1 / auto2 track the scene; indoor / outdoor lock a "
                    "colour temperature; one_push samples once; manual uses "
                    "fixed R/B gains."
                ),
                "values": list(_WB_MODE_TO_BYTE.keys()),
                "state_key": "wb_mode",
                "default": "auto1",
                "setup": False,
            },
            "rgain": {
                "type": "integer",
                "label": "Red Gain",
                "help": "Manual-WB red gain. 0 = -128, 128 = neutral, 255 = +127.",
                "min": 0,
                "max": 255,
                "state_key": "rgain",
                "default": 128,
                "setup": False,
            },
            "bgain": {
                "type": "integer",
                "label": "Blue Gain",
                "help": "Manual-WB blue gain. 0 = -128, 128 = neutral, 255 = +127.",
                "min": 0,
                "max": 255,
                "state_key": "bgain",
                "default": 128,
                "setup": False,
            },
            "chroma_suppress": {
                "type": "integer",
                "label": "Chroma Suppress",
                "help": "0 = Off, 1 = Weak, 2 = Mid, 3 = Strong.",
                "min": 0,
                "max": 3,
                "state_key": "chroma_suppress",
                "default": 0,
                "setup": False,
            },
            "backlight": {
                "type": "boolean",
                "label": "Backlight Compensation",
                "help": "Brightens a subject lit from behind.",
                "state_key": "backlight",
                "default": False,
                "setup": False,
            },
            "spotlight": {
                "type": "boolean",
                "label": "Spotlight Compensation",
                "help": "Tames a bright spotlight on the subject.",
                "state_key": "spotlight",
                "default": False,
                "setup": False,
            },
            "visibility_enhancer": {
                "type": "boolean",
                "label": "Visibility Enhancer",
                "help": "Lifts shadow detail in high-contrast scenes.",
                "state_key": "visibility_enhancer",
                "default": False,
                "setup": False,
            },
            "defog": {
                "type": "boolean",
                "label": "Defog (BRC-X400/X401)",
                "help": "Haze reduction. BRC-X400/X401 only.",
                "state_key": "defog",
                "default": False,
                "setup": False,
            },
            "defog_level": {
                "type": "integer",
                "label": "Defog Level (BRC-X400/X401)",
                "help": "1-3, applied when Defog is on.",
                "min": 1,
                "max": 3,
                "state_key": "defog_level",
                "default": 2,
                "setup": False,
            },
            "low_light": {
                "type": "boolean",
                "label": "Low Light Basis",
                "help": "Low-light brightness mode.",
                "state_key": "low_light",
                "default": False,
                "setup": False,
            },
            "low_light_level": {
                "type": "integer",
                "label": "Low Light Basis Brightness",
                "help": "4-10, applied when Low Light Basis is on.",
                "min": 4,
                "max": 10,
                "state_key": "low_light_level",
                "default": 4,
                "setup": False,
            },
            "high_sensitivity": {
                "type": "boolean",
                "label": "High Sensitivity Mode",
                "help": "Extended gain for very dark rooms.",
                "state_key": "high_sensitivity",
                "default": False,
                "setup": False,
            },
            "min_shutter": {
                "type": "integer",
                "label": "Min Shutter (raw)",
                "help": "Lower shutter-speed limit (raw 0-255).",
                "min": 0,
                "max": 255,
                "state_key": "min_shutter",
                "default": 0,
                "setup": False,
            },
            "max_shutter": {
                "type": "integer",
                "label": "Max Shutter (raw)",
                "help": "Upper shutter-speed limit (raw 0-255).",
                "min": 0,
                "max": 255,
                "state_key": "max_shutter",
                "default": 0,
                "setup": False,
            },
            "exp_comp": {
                "type": "boolean",
                "label": "Exposure Compensation Enabled",
                "help": "Enables the exposure-compensation offset.",
                "state_key": "exp_comp",
                "default": False,
                "setup": False,
            },
            "exp_comp_level": {
                "type": "integer",
                "label": "Exposure Compensation Level",
                "help": "0 = -7 stops, 7 = center, 14 = +7 stops.",
                "min": 0,
                "max": 14,
                "state_key": "exp_comp_level",
                "default": 7,
                "setup": False,
            },
            "flicker_cancel": {
                "type": "boolean",
                "label": "Flicker Cancel",
                "help": "Cancels banding from mains-frequency lighting.",
                "state_key": "flicker_cancel",
                "default": False,
                "setup": False,
            },
            "ir_correction": {
                "type": "enum",
                "label": "IR Correction",
                "help": "Focus correction for IR-heavy lighting.",
                "values": ["standard", "ir_light"],
                "state_key": "ir_correction",
                "default": "standard",
                "setup": False,
            },
            "ir_cut_filter": {
                "type": "enum",
                "label": "IR Cut Filter (ICR)",
                "help": "day = filter in (colour), night = filter out (mono/IR).",
                "values": ["day", "night"],
                "state_key": "ir_cut_filter",
                "default": "day",
                "setup": False,
            },
            "auto_icr": {
                "type": "boolean",
                "label": "Auto ICR",
                "help": "Camera switches the IR cut filter automatically by light level.",
                "state_key": "auto_icr",
                "default": False,
                "setup": False,
            },
            "af_mode": {
                "type": "enum",
                "label": "AF Mode",
                "help": "Autofocus behaviour.",
                "values": list(_AF_MODE_TO_BYTE.keys()),
                "state_key": "af_mode",
                "default": "normal",
                "setup": False,
            },
            "preset_mode": {
                "type": "enum",
                "label": "PRESET MODE (BRC-X400/X401)",
                "help": (
                    "What a preset stores. mode1 = PTZ/F + camera settings, "
                    "mode2 = PTZ/F only, trace = PTZ paths. BRC-X400/X401 only."
                ),
                "values": list(_PRESET_MODE_TO_BYTE.keys()),
                "state_key": "preset_mode",
                "default": "mode1",
                "setup": False,
            },
        },
        # Parameterless commissioning actions surfaced as one-tap buttons.
        "quick_actions": ["pt_home", "wb_one_push_trigger", "focus_one_push"],
        "commands": {
            # ── Power ──
            "power_on":  {"label": "Power On",  "params": {}},
            "power_off": {"label": "Power Off (Standby)", "params": {}},

            # ── Pan/Tilt continuous drive ──
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

            # ── Zoom ──
            "zoom_tele": {
                "label": "Zoom Tele (In)",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7}},
                "help": "Variable-speed zoom in. Send 'zoom_stop' to halt.",
            },
            "zoom_wide": {
                "label": "Zoom Wide (Out)",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7}},
                "help": "Variable-speed zoom out. Send 'zoom_stop' to halt.",
            },
            "zoom_stop": {"label": "Zoom Stop", "params": {}},
            "zoom_direct": {
                "label": "Zoom Direct",
                "params": {"position": {"type": "integer", "required": True, "min": 0, "max": 31424}},
                "help": "Optical positions run 0-16384 (0x4000); Clear Image Zoom / digital extends to 31424 (0x7AC0) on models that support it.",
            },

            # ── Focus ──
            "focus_far": {
                "label": "Focus Far",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7}},
            },
            "focus_near": {
                "label": "Focus Near",
                "params": {"speed": {"type": "integer", "min": 0, "max": 7}},
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
            },

            # ── Presets (0-99) ──
            "preset_recall": {
                "label": "Preset Recall",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 99}},
            },
            "preset_set": {
                "label": "Preset Save",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 99}},
            },
            "preset_reset": {
                "label": "Preset Reset (Erase)",
                "params": {"number": {"type": "integer", "required": True, "min": 0, "max": 99}},
            },

            # ── PRESET MODE (BRC-X400/X401) ──
            "set_preset_mode": {
                "label": "Set Preset Mode (BRC-X400/X401)",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": list(_PRESET_MODE_TO_BYTE.keys()),
                    },
                },
                "help": (
                    "MODE1 saves PT/Z/F + camera settings, MODE2 saves only "
                    "PT/Z/F, TRACE saves PTZ paths."
                ),
            },

            # ── PTZ TRACE (BRC-X400/X401, slots 1..16) ──
            "ptz_trace_record_start": {
                "label": "PTZ TRACE Record: Start",
                "params": {"slot": {"type": "integer", "required": True, "min": 1, "max": 16}},
            },
            "ptz_trace_record_stop": {
                "label": "PTZ TRACE Record: Stop",
                "params": {},
            },
            "ptz_trace_play_prepare": {
                "label": "PTZ TRACE Play: Prepare",
                "params": {"slot": {"type": "integer", "required": True, "min": 1, "max": 16}},
            },
            "ptz_trace_play_start": {
                "label": "PTZ TRACE Play: Start",
                "params": {},
            },
            "ptz_trace_delete": {
                "label": "PTZ TRACE Delete",
                "params": {"slot": {"type": "integer", "required": True, "min": 1, "max": 16}},
            },

            # ── Exposure ──
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
            "set_ae_speed": {
                "label": "Set AE Convergence Speed",
                "params": {
                    "speed": {"type": "integer", "required": True, "min": 1, "max": 48},
                },
                "help": "1 = fast convergence, higher = slower / smoother.",
            },
            "set_backlight": {
                "label": "Set Backlight Compensation",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "set_spotlight": {
                "label": "Set Spotlight Compensation",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "set_visibility_enhancer": {
                "label": "Set Visibility Enhancer",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "set_low_light": {
                "label": "Set Low Light Basis",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "set_low_light_level": {
                "label": "Set Low Light Basis Brightness",
                "params": {"level": {"type": "integer", "required": True, "min": 4, "max": 10}},
            },
            "set_high_sensitivity": {
                "label": "Set High Sensitivity Mode",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "set_min_shutter": {
                "label": "Set Min Shutter (raw value)",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 255}},
            },
            "set_max_shutter": {
                "label": "Set Max Shutter (raw value)",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 255}},
            },
            "set_exp_comp": {
                "label": "Set Exposure Compensation On/Off",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "set_exp_comp_level": {
                "label": "Set Exposure Compensation Level",
                "params": {"level": {"type": "integer", "required": True, "min": 0, "max": 14}},
                "help": "0 = -7 stops, 7 = center, 14 = +7 stops.",
            },
            "set_flicker_cancel": {
                "label": "Set Flicker Cancel",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },

            # ── White balance ──
            "set_wb_mode": {
                "label": "Set White Balance Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": list(_WB_MODE_TO_BYTE.keys()),
                    },
                },
            },
            "wb_one_push_trigger": {
                "label": "One-Push WB Trigger",
                "params": {},
            },
            "rgain_reset": {"label": "R Gain: Reset", "params": {}},
            "rgain_up":    {"label": "R Gain: Up",    "params": {}},
            "rgain_down":  {"label": "R Gain: Down",  "params": {}},
            "rgain_direct": {
                "label": "R Gain: Direct",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 255}},
                "help": "0 = -128, 128 = neutral, 255 = +127.",
            },
            "bgain_reset": {"label": "B Gain: Reset", "params": {}},
            "bgain_up":    {"label": "B Gain: Up",    "params": {}},
            "bgain_down":  {"label": "B Gain: Down",  "params": {}},
            "bgain_direct": {
                "label": "B Gain: Direct",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 255}},
            },
            "set_chroma_suppress": {
                "label": "Set Chroma Suppress",
                "params": {"level": {"type": "integer", "required": True, "min": 0, "max": 3}},
                "help": "0=Off, 1=Weak, 2=Mid, 3=Strong.",
            },

            # ── Picture / color ──
            "set_picture_profile": {
                "label": "Set Picture Profile",
                "params": {
                    "profile": {
                        "type": "enum",
                        "required": True,
                        "values": list(_PICTURE_PROFILE_TO_BYTE.keys()),
                    },
                },
                "help": (
                    "off / std / high_sat / fl_light / movie / still / "
                    "cinema / pro / itu709 / bw."
                ),
            },
            "set_color_matrix": {
                "label": "Set Color Matrix Coefficient (BRC-X400/X401)",
                "params": {
                    "axis": {
                        "type": "enum",
                        "required": True,
                        "values": list(_COLOR_MATRIX_OPS.keys()),
                    },
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": -99,
                        "max": 99,
                    },
                },
                "help": (
                    "axis is one of rg / rb / gr / gb / br / bg. value -99..+99."
                ),
            },
            "set_defog": {
                "label": "Set Defog (BRC-X400/X401)",
                "params": {
                    "enabled": {"type": "boolean", "required": True},
                    "level": {"type": "integer", "min": 1, "max": 3},
                },
            },

            # ── Tally ──
            "set_tally_onoff": {
                "label": "Set Tally On/Off",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "set_tally_level": {
                "label": "Set Tally Level",
                "params": {
                    "level": {
                        "type": "enum",
                        "required": True,
                        "values": list(_TALLY_LEVEL_TO_BYTE.keys()),
                    },
                },
            },

            # ── IR / ICR / AF ──
            "set_ir_correction": {
                "label": "Set IR Correction",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["standard", "ir_light"],
                    },
                },
            },
            "set_ir_cut_filter": {
                "label": "Set IR Cut Filter (ICR)",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["day", "night"],
                    },
                },
            },
            "set_auto_icr": {
                "label": "Set Auto ICR",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "set_af_mode": {
                "label": "Set AF Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": list(_AF_MODE_TO_BYTE.keys()),
                    },
                },
            },

            # ── OSD / Menu ──
            "set_osd": {
                "label": "Set OSD On/Off",
                "params": {
                    "output": {
                        "type": "enum",
                        "required": True,
                        "values": ["sdi", "hdmi"],
                    },
                    "enabled": {"type": "boolean", "required": True},
                },
                "help": (
                    "Show or hide the camera's on-screen menu on the named "
                    "video output."
                ),
            },
            "menu_enter": {
                "label": "Menu: Enter",
                "params": {},
                "help": "Confirm the current menu selection.",
            },
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sequence = 0
        self._inquiry_lock: asyncio.Lock | None = None
        self._inquiry_future: asyncio.Future[bytes] | None = None
        self._control_reply_future: asyncio.Future[None] | None = None

    async def _post_connect(self) -> None:
        """Session setup + liveness gate, run by BaseDriver.connect() after the
        UDP socket opens and before the device is marked connected.

        A raise here aborts the connect cleanly (BaseDriver stashes the
        transport error, closes the socket, re-raises). That is how a dead
        camera surfaces offline with ``no_response`` instead of a phantom-
        connected session, and how every reconnect attempt against a still-dead
        camera keeps that reason instead of flapping back online — UDP is
        connectionless, so the socket "opening" proves nothing.
        """
        self._inquiry_lock = asyncio.Lock()
        self._sequence = 0

        # Best-effort RESET (some cameras don't ACK it); the liveness gate
        # below is the real reachability check.
        try:
            await self._send_control_reset()
        except (ConnectionError, OSError, asyncio.TimeoutError):
            log.warning(
                f"[{self.device_id}] Sony VISCA-IP RESET handshake did not "
                "complete; continuing optimistically"
            )

        # Liveness gate: the camera must answer the power inquiry. poll()
        # raises a no_response-worded ConnectionError when it answers nothing;
        # retry to ride out a single dropped UDP datagram at connect, then
        # propagate so a truly dead camera is reported offline.
        last_exc: Exception | None = None
        for _ in range(max(1, PROBE_RETRIES)):
            try:
                await self.poll()
                return
            except (ConnectionError, OSError) as e:
                last_exc = e
        host = self.config.get("host", "?")
        port = self.config.get("port", "?")
        raise ConnectionError(
            f"Camera at {host}:{port} is not responding to VISCA inquiries"
        ) from last_exc

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
        unwrapped = _unwrap(data)
        if unwrapped is None:
            log.debug(f"[{self.device_id}] dropped malformed packet: {data.hex()}")
            return
        payload_type, _seq, payload = unwrapped

        if payload_type == PAYLOAD_CONTROL_REPLY:
            if (
                self._control_reply_future
                and not self._control_reply_future.done()
            ):
                self._control_reply_future.set_result(None)
            return

        if payload_type != PAYLOAD_VISCA_REPLY:
            return

        if len(payload) < 2 or payload[0] != 0x90:
            return
        second = payload[1] & 0xF0
        if second == 0x40:
            return
        if second == 0x60:
            err = payload[2] if len(payload) > 2 else 0
            log.debug(
                f"[{self.device_id}] VISCA error {err:#04x} "
                f"(packet {payload.hex()})"
            )
            if self._inquiry_future and not self._inquiry_future.done():
                self._inquiry_future.set_result(b"")
            return
        if second == 0x50:
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
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        seq = self._next_seq()
        await self.transport.send(_wrap(visca, PAYLOAD_VISCA_COMMAND, seq))

    async def _send_visca_inquiry(self, visca: bytes) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        seq = self._next_seq()
        await self.transport.send(_wrap(visca, PAYLOAD_VISCA_INQUIRY, seq))

    async def _send_control_reset(self, timeout: float | None = None) -> None:
        if timeout is None:
            timeout = RESET_TIMEOUT_S
        loop = asyncio.get_running_loop()
        self._control_reply_future = loop.create_future()
        try:
            await self.transport.send(_wrap(CONTROL_RESET, PAYLOAD_CONTROL_CMD, 0))
            await asyncio.wait_for(self._control_reply_future, timeout=timeout)
            self._sequence = 0
            log.debug(f"[{self.device_id}] Sony VISCA-IP RESET handshake complete")
        finally:
            self._control_reply_future = None

    async def _inquire(self, payload: bytes, timeout: float = 1.5) -> bytes | None:
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
                pos = max(0, min(0x7AC0, int(params["position"])))
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

            case "set_preset_mode":
                mode = str(params["mode"])
                b = _PRESET_MODE_TO_BYTE.get(mode)
                if b is None:
                    raise ValueError(f"Unknown preset mode: {mode}")
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x04, 0x3D, b, 0xFF])
                )
                self.set_state("preset_mode", mode)

            case "ptz_trace_record_start":
                slot = max(1, min(16, int(params["slot"]))) - 1
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x04, 0x20, 0x00, slot, 0x02, 0xFF])
                )
            case "ptz_trace_record_stop":
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x04, 0x20, 0x00, 0x00, 0x03, 0xFF])
                )
            case "ptz_trace_play_prepare":
                slot = max(1, min(16, int(params["slot"]))) - 1
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x04, 0x20, 0x01, slot, 0x01, 0xFF])
                )
            case "ptz_trace_play_start":
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x04, 0x20, 0x01, 0x00, 0x02, 0xFF])
                )
            case "ptz_trace_delete":
                slot = max(1, min(16, int(params["slot"]))) - 1
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x04, 0x20, 0x02, slot, 0x00, 0xFF])
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
            case "set_ae_speed":
                spd = max(1, min(48, int(params["speed"])))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x5D, spd, 0xFF])
                )
                self.set_state("ae_speed", spd)
            case "set_backlight":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x04\x33" + (b"\x02" if on else b"\x03") + b"\xff"
                )
                self.set_state("backlight", on)
            case "set_spotlight":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x04\x3A" + (b"\x02" if on else b"\x03") + b"\xff"
                )
                self.set_state("spotlight", on)
            case "set_visibility_enhancer":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x04\x3D" + (b"\x06" if on else b"\x03") + b"\xff"
                )
                self.set_state("visibility_enhancer", on)
            case "set_low_light":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x05\x39" + (b"\x02" if on else b"\x03") + b"\xff"
                )
                self.set_state("low_light", on)
            case "set_low_light_level":
                lvl = max(4, min(10, int(params["level"])))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x05, 0x49, lvl, 0xFF])
                )
                self.set_state("low_light_level", lvl)
            case "set_high_sensitivity":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x04\x5E" + (b"\x02" if on else b"\x03") + b"\xff"
                )
                self.set_state("high_sensitivity", on)
            case "set_min_shutter":
                v = max(0, min(0xFF, int(params["value"])))
                await self._send_visca(
                    b"\x81\x01\x05\x2A\x01" + _encode_2nibble(v) + b"\xff"
                )
                self.set_state("min_shutter", v)
            case "set_max_shutter":
                v = max(0, min(0xFF, int(params["value"])))
                await self._send_visca(
                    b"\x81\x01\x05\x2A\x00" + _encode_2nibble(v) + b"\xff"
                )
                self.set_state("max_shutter", v)
            case "set_exp_comp":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x04\x3E" + (b"\x02" if on else b"\x03") + b"\xff"
                )
                self.set_state("exp_comp", on)
            case "set_exp_comp_level":
                lvl = max(0, min(0x0E, int(params["level"])))
                await self._send_visca(
                    b"\x81\x01\x04\x4E\x00\x00" + _encode_2nibble(lvl) + b"\xff"
                )
                self.set_state("exp_comp_level", lvl)
            case "set_flicker_cancel":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x04\x32" + (b"\x02" if on else b"\x03") + b"\xff"
                )
                self.set_state("flicker_cancel", on)

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

            case "rgain_reset":
                await self._send_visca(b"\x81\x01\x04\x03\x00\xff")
            case "rgain_up":
                await self._send_visca(b"\x81\x01\x04\x03\x02\xff")
            case "rgain_down":
                await self._send_visca(b"\x81\x01\x04\x03\x03\xff")
            case "rgain_direct":
                v = max(0, min(0xFF, int(params["value"])))
                await self._send_visca(
                    b"\x81\x01\x04\x43\x00\x00" + _encode_2nibble(v) + b"\xff"
                )
                self.set_state("rgain", v)
            case "bgain_reset":
                await self._send_visca(b"\x81\x01\x04\x04\x00\xff")
            case "bgain_up":
                await self._send_visca(b"\x81\x01\x04\x04\x02\xff")
            case "bgain_down":
                await self._send_visca(b"\x81\x01\x04\x04\x03\xff")
            case "bgain_direct":
                v = max(0, min(0xFF, int(params["value"])))
                await self._send_visca(
                    b"\x81\x01\x04\x44\x00\x00" + _encode_2nibble(v) + b"\xff"
                )
                self.set_state("bgain", v)
            case "set_chroma_suppress":
                lvl = max(0, min(3, int(params["level"])))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x5F, lvl, 0xFF])
                )
                self.set_state("chroma_suppress", lvl)

            case "set_picture_profile":
                profile = str(params["profile"])
                b = _PICTURE_PROFILE_TO_BYTE.get(profile)
                if b is None:
                    raise ValueError(f"Unknown picture profile: {profile}")
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x01, 0x3D, b, 0xFF])
                )
                self.set_state("picture_profile", profile)
            case "set_color_matrix":
                axis = str(params["axis"])
                op = _COLOR_MATRIX_OPS.get(axis)
                if op is None:
                    raise ValueError(f"Unknown color matrix axis: {axis}")
                v = _color_matrix_to_byte(int(params["value"]))
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x01, op]) + _encode_2nibble(v) + b"\xff"
                )
            case "set_defog":
                on = bool(params["enabled"])
                lvl = max(1, min(3, int(params.get("level", 2))))
                p = 0x02 if on else 0x03
                # When On, q encodes level (Sony spec: 0=Min..3=Max; UI 1..3
                # maps to byte 1..3).
                q = lvl if on else 0
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x37, p, q, 0xFF])
                )
                self.set_state("defog", on)
                if on:
                    self.set_state("defog_level", lvl)

            case "set_tally_onoff":
                on = bool(params["enabled"])
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x01, 0x0A, 0x00, 0x02 if on else 0x03, 0xFF])
                )
            case "set_tally_level":
                level = str(params["level"])
                b = _TALLY_LEVEL_TO_BYTE.get(level)
                if b is None:
                    raise ValueError(f"Unknown tally level: {level}")
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x01, 0x0A, 0x01, b, 0xFF])
                )
                self.set_state("tally_level", level)

            case "set_ir_correction":
                mode = str(params["mode"])
                b = 0x01 if mode == "ir_light" else 0x00
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x11, b, 0xFF])
                )
                self.set_state("ir_correction", mode)
            case "set_ir_cut_filter":
                mode = str(params["mode"])
                b = 0x02 if mode == "night" else 0x03
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x01, b, 0xFF])
                )
                self.set_state("ir_cut_filter", mode)
            case "set_auto_icr":
                on = bool(params["enabled"])
                await self._send_visca(
                    b"\x81\x01\x04\x51" + (b"\x02" if on else b"\x03") + b"\xff"
                )
                self.set_state("auto_icr", on)
            case "set_af_mode":
                mode = str(params["mode"])
                b = _AF_MODE_TO_BYTE.get(mode)
                if b is None:
                    raise ValueError(f"Unknown AF mode: {mode}")
                await self._send_visca(
                    bytes([0x81, 0x01, 0x04, 0x57, b, 0xFF])
                )
                self.set_state("af_mode", mode)

            case "set_osd":
                output = str(params["output"])
                on = bool(params["enabled"])
                p = 0x00 if output == "sdi" else 0x01
                q = 0x02 if on else 0x03
                await self._send_visca(
                    bytes([0x81, 0x01, 0x7E, 0x04, 0x76, p, q, 0xFF])
                )
            case "menu_enter":
                # Sony's MENU ENTER from the BRC-X400 spec is `81 01 7E 01 02
                # 00 01 FF` (close to a "select" / "OK" press). The simulator
                # treats it as a no-op ack.
                await self._send_visca(
                    b"\x81\x01\x7E\x01\x02\x00\x01\xff"
                )

            case _:
                raise ValueError(f"Unknown command: {command}")

    # ── Device settings ──

    # setting key -> (command, param name). The picture surface routes to the
    # existing transient commands so byte-building lives in one place; the
    # platform handles the offline pending queue and read-back comes from
    # poll(). defog is compound (set_defog takes enabled + level together) and
    # handled specially below.
    _DS_COMMANDS = {
        "picture_profile": ("set_picture_profile", "profile"),
        "ae_mode": ("set_ae_mode", "mode"),
        "ae_speed": ("set_ae_speed", "speed"),
        "wb_mode": ("set_wb_mode", "mode"),
        "rgain": ("rgain_direct", "value"),
        "bgain": ("bgain_direct", "value"),
        "chroma_suppress": ("set_chroma_suppress", "level"),
        "backlight": ("set_backlight", "enabled"),
        "spotlight": ("set_spotlight", "enabled"),
        "visibility_enhancer": ("set_visibility_enhancer", "enabled"),
        "low_light": ("set_low_light", "enabled"),
        "low_light_level": ("set_low_light_level", "level"),
        "high_sensitivity": ("set_high_sensitivity", "enabled"),
        "min_shutter": ("set_min_shutter", "value"),
        "max_shutter": ("set_max_shutter", "value"),
        "exp_comp": ("set_exp_comp", "enabled"),
        "exp_comp_level": ("set_exp_comp_level", "level"),
        "flicker_cancel": ("set_flicker_cancel", "enabled"),
        "ir_correction": ("set_ir_correction", "mode"),
        "ir_cut_filter": ("set_ir_cut_filter", "mode"),
        "auto_icr": ("set_auto_icr", "enabled"),
        "af_mode": ("set_af_mode", "mode"),
        "preset_mode": ("set_preset_mode", "mode"),
    }

    @staticmethod
    def _to_bool(value: Any) -> bool:
        return (
            value if isinstance(value, bool)
            else str(value).strip().lower() in ("1", "true", "on", "yes")
        )

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a picture/image device setting by routing to the matching
        transient command, coercing the value by the declared setting type."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        # defog is compound: set_defog needs the on/off AND the level together,
        # so writing either side re-sends both, reading the sibling from state.
        if key == "defog":
            await self.send_command("set_defog", {
                "enabled": self._to_bool(value),
                "level": int(self.get_state("defog_level") or 2),
            })
            return
        if key == "defog_level":
            await self.send_command("set_defog", {
                "enabled": bool(self.get_state("defog")),
                "level": int(value),
            })
            return

        spec = self._DS_COMMANDS.get(key)
        if spec is None:
            raise ValueError(f"Unknown device setting: {key}")
        command, param = spec
        setting_def = self.DRIVER_INFO["device_settings"][key]
        stype = setting_def.get("type")
        if stype == "boolean":
            coerced: Any = self._to_bool(value)
        elif stype == "integer":
            coerced = int(value)
        else:
            coerced = str(value)
        await self.send_command(command, {param: coerced})

    # ── Polling ──

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return

        # Power doubles as the liveness probe: every VISCA camera answers
        # CAM_PowerInq, so a total non-answer means the camera vanished
        # (unplugged, powered off, wrong IP, UDP black hole). Raise so the
        # missed-poll watchdog flips the device offline with no_response
        # wording — previously the swallowed UDP timeout left an unplugged
        # camera shown ONLINE forever.
        reply = await self._inquire(POWER_INQUIRY, timeout=PROBE_TIMEOUT_S)
        if reply is None:
            host = self.config.get("host", "?")
            port = self.config.get("port", "?")
            raise ConnectionError(
                f"Camera at {host}:{port} is not responding to VISCA inquiries"
            )
        if len(reply) == 4:
            self.set_state("power", "on" if reply[2] == 0x02 else "standby")

        # The camera is reachable; individual inquiry timeouts below are benign
        # (a model may not support a given inquiry) and don't trip the watchdog.

        # Pan/Tilt position
        reply = await self._inquire(b"\x81\x09\x06\x12\xff")
        if reply and len(reply) == 11:
            self.set_state("pan_position", _decode_4nibble(reply[2:6], signed=True))
            self.set_state("tilt_position", _decode_4nibble(reply[6:10], signed=True))

        # Zoom position
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

        # R Gain (5-byte data form: 90 50 00 00 0p 0p FF)
        reply = await self._inquire(b"\x81\x09\x04\x43\xff")
        if reply and len(reply) == 7:
            self.set_state("rgain", _decode_2nibble(reply[4:6]))

        # B Gain
        reply = await self._inquire(b"\x81\x09\x04\x44\xff")
        if reply and len(reply) == 7:
            self.set_state("bgain", _decode_2nibble(reply[4:6]))

        # Backlight
        reply = await self._inquire(b"\x81\x09\x04\x33\xff")
        if reply and len(reply) == 4:
            self.set_state("backlight", reply[2] == 0x02)

        # Spotlight
        reply = await self._inquire(b"\x81\x09\x04\x3A\xff")
        if reply and len(reply) == 4:
            self.set_state("spotlight", reply[2] == 0x02)

        # Visibility Enhancer (p: 6=On, 3=Off)
        reply = await self._inquire(b"\x81\x09\x04\x3D\xff")
        if reply and len(reply) == 4:
            self.set_state("visibility_enhancer", reply[2] == 0x06)

        # Low Light Basis on/off + level
        reply = await self._inquire(b"\x81\x09\x05\x39\xff")
        if reply and len(reply) == 4:
            self.set_state("low_light", reply[2] == 0x02)
        reply = await self._inquire(b"\x81\x09\x05\x49\xff")
        if reply and len(reply) == 4:
            self.set_state("low_light_level", reply[2])

        # Chroma Suppress
        reply = await self._inquire(b"\x81\x09\x04\x5F\xff")
        if reply and len(reply) == 4:
            self.set_state("chroma_suppress", reply[2])

        # Defog (BRC-X400/X401: y0 50 0p 0q FF)
        reply = await self._inquire(b"\x81\x09\x04\x37\xff")
        if reply and len(reply) == 5:
            self.set_state("defog", reply[2] == 0x02)
            if reply[2] == 0x02 and reply[3]:
                self.set_state("defog_level", reply[3])

        # Picture Profile (CAM_PictureEffect, 7E 01 3D inquiry → y0 50 0p)
        reply = await self._inquire(b"\x81\x09\x7E\x01\x3D\xff")
        if reply and len(reply) == 4:
            profile = _BYTE_TO_PICTURE_PROFILE.get(reply[2])
            if profile:
                self.set_state("picture_profile", profile)

        # AE Speed
        reply = await self._inquire(b"\x81\x09\x04\x5D\xff")
        if reply and len(reply) == 4:
            self.set_state("ae_speed", reply[2])

        # Min/Max Shutter
        reply = await self._inquire(b"\x81\x09\x05\x2A\x00\xff")
        if reply and len(reply) == 5:
            self.set_state("max_shutter", _decode_2nibble(reply[2:4]))
        reply = await self._inquire(b"\x81\x09\x05\x2A\x01\xff")
        if reply and len(reply) == 5:
            self.set_state("min_shutter", _decode_2nibble(reply[2:4]))

        # High Sensitivity
        reply = await self._inquire(b"\x81\x09\x04\x5E\xff")
        if reply and len(reply) == 4:
            self.set_state("high_sensitivity", reply[2] == 0x02)

        # Exp Comp
        reply = await self._inquire(b"\x81\x09\x04\x3E\xff")
        if reply and len(reply) == 4:
            self.set_state("exp_comp", reply[2] == 0x02)
        reply = await self._inquire(b"\x81\x09\x04\x4E\xff")
        if reply and len(reply) == 7:
            self.set_state("exp_comp_level", _decode_2nibble(reply[4:6]))

        # Flicker Cancel
        reply = await self._inquire(b"\x81\x09\x04\x32\xff")
        if reply and len(reply) == 4:
            self.set_state("flicker_cancel", reply[2] == 0x02)

        # IR Correction
        reply = await self._inquire(b"\x81\x09\x04\x11\xff")
        if reply and len(reply) == 4:
            self.set_state(
                "ir_correction",
                "ir_light" if reply[2] == 0x01 else "standard",
            )

        # IR Cut Filter / Auto ICR
        reply = await self._inquire(b"\x81\x09\x04\x01\xff")
        if reply and len(reply) == 4:
            self.set_state(
                "ir_cut_filter", "night" if reply[2] == 0x02 else "day"
            )
        reply = await self._inquire(b"\x81\x09\x04\x51\xff")
        if reply and len(reply) == 4:
            self.set_state("auto_icr", reply[2] == 0x02)

        # AF Mode
        reply = await self._inquire(b"\x81\x09\x04\x57\xff")
        if reply and len(reply) == 4:
            mode = _BYTE_TO_AF_MODE.get(reply[2])
            if mode:
                self.set_state("af_mode", mode)

        # Preset Mode (BRC-X400/X401)
        reply = await self._inquire(b"\x81\x09\x7E\x04\x3D\xff")
        if reply and len(reply) == 4:
            mode = _BYTE_TO_PRESET_MODE.get(reply[2])
            if mode:
                self.set_state("preset_mode", mode)

        # PTZ TRACE Status
        reply = await self._inquire(b"\x81\x09\x7E\x04\x20\x03\xff")
        if reply and len(reply) == 4:
            status = _PTZ_TRACE_STATUS.get(reply[2])
            if status:
                self.set_state("ptz_trace_status", status)

        # Tally On/Off
        reply = await self._inquire(b"\x81\x09\x7E\x01\x0A\xff")
        if reply and len(reply) == 4:
            level = _BYTE_TO_TALLY_LEVEL.get(reply[2])
            if level:
                self.set_state("tally_level", level)

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
