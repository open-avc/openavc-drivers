"""
OpenAVC AVer PTZ310/PTZ330 Pro-AV Camera Driver.

AVer's Pro-AV PTZ cameras expose two parallel control surfaces:

  * VISCA-over-IP (UDP port 52381, Sony 8-byte header) — power, absolute
    pan/tilt/zoom/focus positioning, position inquiries, fast jog with
    per-command speed bytes. Sony's wire format, AVer's command subset.
  * HTTP CGI (TCP port 80, ``/storks?cmd=<command>``) — full image-quality
    surface (shutter, iris, gain, gain limit, white balance, color
    temperature, saturation, contrast, sharpness, noise filter, mirror/
    flip), AVer-specific AI features (SmartShoot, SmartFraming), bulk
    status query (``get_sys_stat``), RTMP streaming control, factory
    reset, and reboot.

The generic ``visca_ip`` driver covers the universal Sony VISCA subset
across the AVer fleet, but it cannot reach the HTTP-only image-quality
controls and AI feature toggles. This driver adds them for the PTZ310 /
PTZ310N / PTZ310W / PTZ330 / PTZ330N / PTZ330W families, where AVer's
HTTP CGI surface is publicly documented and stable.

Other AVer cameras (CAM5xx, VC520, VB342+, CAM570, etc.) speak the same
VISCA subset but their HTTP surface either differs or is undocumented;
those continue to use ``visca_ip`` until AVer ships matching CGI guides.

Push vs poll
------------
Both transports are request/response. AVer publishes no push/subscription
mechanism for either VISCA-IP or the HTTP CGI; the driver polls.

Why Python
----------
Two simultaneous transports (HTTP for image-quality + AI, VISCA-over-UDP
for PTZ + power + inquiries) from one driver. ``ConfigurableDriver``
exposes one transport per driver today, so this is the same multi-
transport shape used by the shipped ``birddog_ptz`` driver.

Sources (cached in ``driver-roadmap/reference-docs/``)
------------------------------------------------------
* AVer VISCA Specification 3.3 — ``aver-visca-3.3.pdf``
* PTZ310/330 HTTP CGI Commands (Updated 6/2/2022) —
  ``aver-ptz310-330-cgi.pdf``
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any
from urllib.parse import quote

import httpx

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)


# ── VISCA-over-IP wire constants ──

_PT_VISCA_PORT = 52381

PAYLOAD_VISCA_COMMAND = 0x0100
PAYLOAD_VISCA_INQUIRY = 0x0110
PAYLOAD_VISCA_REPLY = 0x0111
PAYLOAD_CONTROL_CMD = 0x0200
PAYLOAD_CONTROL_REPLY = 0x0201

CONTROL_RESET = b"\x01"


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
    return bytes([
        (v >> 12) & 0x0F,
        (v >> 8) & 0x0F,
        (v >> 4) & 0x0F,
        v & 0x0F,
    ])


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


# AVer expmode integer values (HTTP get_sys_stat / set_expmode)
# 0=Full Auto, 1=Shutter Priority, 2=Iris Priority, 3=Manual.
_EXP_MODE_TO_INT = {
    "full_auto": 0,
    "shutter_priority": 1,
    "iris_priority": 2,
    "manual": 3,
}
_INT_TO_EXP_MODE = {v: k for k, v in _EXP_MODE_TO_INT.items()}

# AVer wb modes (HTTP set_wb)
# 0=Auto, 2=Indoor, 3=Outdoor, 4=One Push, 5=Manual.
# (Value 1 is reserved/unused per the CGI doc.)
_WB_MODE_TO_INT = {
    "auto": 0,
    "indoor": 2,
    "outdoor": 3,
    "one_push": 4,
    "manual": 5,
}
_INT_TO_WB_MODE = {v: k for k, v in _WB_MODE_TO_INT.items()}

# AVer mirror_flip (HTTP mirror_flip)
# 0=Off, 1=Mirror, 2=Flip, 3=Both.
_MIRROR_FLIP_TO_INT = {
    "off": 0,
    "mirror": 1,
    "flip": 2,
    "both": 3,
}
_INT_TO_MIRROR_FLIP = {v: k for k, v in _MIRROR_FLIP_TO_INT.items()}

# AVer noise filter (HTTP set_nf)
_NF_TO_INT = {"off": 0, "low": 1, "medium": 2, "high": 3}
_INT_TO_NF = {v: k for k, v in _NF_TO_INT.items()}

# AVer back light compensation (HTTP set_blc)
_BLC_TO_INT = {"off": 0, "low": 1, "high": 2}
_INT_TO_BLC = {v: k for k, v in _BLC_TO_INT.items()}

# Power frequency (HTTP set_freq)
_FREQ_TO_INT = {"50hz": 1, "60hz": 2, "auto": 3}
_INT_TO_FREQ = {v: k for k, v in _FREQ_TO_INT.items()}


class AVerPTZDriver(BaseDriver):
    """AVer PTZ310/PTZ330 driver (VISCA-IP for motion + HTTP CGI for image / AI)."""

    DRIVER_INFO = {
        "id": "aver_ptz",
        "name": "AVer Pro-AV PTZ Camera (PTZ310/330)",
        "manufacturer": "AVer",
        "category": "camera",
        "version": "1.1.1",
        "author": "OpenAVC",
        "description": (
            "AVer Pro-AV PTZ310 / PTZ330 family. Combines VISCA-over-IP "
            "(UDP 52381) for power, PTZ motion, and position inquiries "
            "with AVer's HTTP CGI (port 80, /storks) for image quality "
            "(shutter, iris, gain, white balance, color temperature, "
            "saturation, contrast, sharpness), AI features (SmartShoot, "
            "SmartFraming), bulk status query, RTMP streaming, mirror/"
            "flip, and reboot. For other AVer models without a documented "
            "HTTP CGI guide, prefer the generic 'visca_ip' driver."
        ),
        "source_url": (
            "https://www.averusa.com/pro-av/downloads/control-codes/"
            "PTZ310330_CGI_Commands.pdf"
        ),
        "tags": ["ptz", "camera", "aver", "visca", "visca-ip", "broadcast"],
        "verified": False,
        "simulated": True,
        "protocols": ["visca-ip", "aver-cgi"],
        "ports": [80, 52381],
        "transport": "http",
        "discovery": {
            # AVer Pro-AV PTZ cameras advertise ONVIF when the camera
            # menu's network feature is enabled. The AVerMedia OUI also
            # surfaces them as a soft candidate when ONVIF is off.
            "onvif": {"manufacturer": "AVer"},
            "oui_prefixes": ["00:18:1a"],
        },
        "compatible_models": [
            {
                "manufacturer": "AVer",
                "models": [
                    "PTZ310",
                    "PTZ310N",
                    "PTZ310W",
                    "PTZ330",
                    "PTZ330N",
                    "PTZ330W",
                ],
                "confidence": "untested",
                "notes": (
                    "Verified against AVer's VISCA Specification 3.3 (April "
                    "2025) and the PTZ310/330 HTTP CGI Commands document "
                    "(June 2022). Both transports use the same camera IP. "
                    "Reboot and Factory Reset require Basic auth on PTZ-S310/"
                    "S330 SKUs; configure a username/password on those models."
                ),
            },
        ],
        "help": {
            "overview": (
                "AVer PTZ310 / PTZ330 broadcast PTZ cameras. Common in "
                "houses of worship, lecture capture, courtroom recording, "
                "and corporate AV. Image quality (gain, iris, shutter, "
                "white balance, color temperature, sharpness, mirror/flip) "
                "and AVer's SmartShoot / SmartFraming AI features are "
                "controlled over HTTP. Power, pan/tilt/zoom motion, and "
                "position inquiries use VISCA-over-IP."
            ),
            "setup": (
                "1. Set the camera to a static IP and place it on a "
                "routable VLAN to the OpenAVC server.\n"
                "2. The driver opens TWO connections: VISCA-over-IP on "
                "UDP 52381 and HTTP on TCP 80. Both must be reachable.\n"
                "3. Most CGI commands require no authentication. The "
                "Reboot and Factory Reset commands require Basic auth "
                "and only work on the PTZ-S310 / PTZ-S330 SKUs — fill in "
                "the username/password fields if you need them.\n"
                "4. VISCA-over-IP supports multiple controllers per "
                "camera, so OpenAVC can co-exist with a hardware "
                "joystick or other control surface."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "visca_port": 52381,
            "username": "",
            "password": "",
            "pan_speed": 12,
            "tilt_speed": 10,
            "poll_interval": 5,
            "inter_command_delay": 0.05,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "default": 80,
                "label": "HTTP Port",
            },
            "visca_port": {
                "type": "integer",
                "default": 52381,
                "label": "VISCA-over-IP Port (UDP)",
            },
            "username": {
                "type": "string",
                "default": "",
                "label": "Username (Reboot / Factory Reset only)",
                "description": (
                    "Required only for the Reboot and Factory Reset "
                    "commands on PTZ-S310 / PTZ-S330 SKUs. Leave blank "
                    "otherwise."
                ),
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "Password",
                "secret": True,
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
                "max": 20,
                "label": "Default Tilt Speed (1-20)",
            },
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often to re-query camera state. Set to 0 to "
                    "disable polling entirely."
                ),
            },
            "inter_command_delay": {
                "type": "number",
                "default": 0.05,
                "min": 0.0,
                "label": "Inter-command Delay (sec)",
            },
        },
        "state_variables": {
            # VISCA-polled
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
            # HTTP-controlled (image / AI)
            "exposure_mode": {
                "type": "enum",
                "values": [
                    "full_auto", "shutter_priority", "iris_priority", "manual",
                ],
                "label": "Exposure Mode",
            },
            "exposure_value": {
                "type": "integer",
                "label": "Exposure Compensation (-4..4)",
                "min": -4,
                "max": 4,
            },
            "shutter_value": {
                "type": "integer",
                "label": "Shutter Index (0..15)",
                "min": 0,
                "max": 15,
            },
            "iris_value": {
                "type": "integer",
                "label": "Iris Index (0..13)",
                "min": 0,
                "max": 13,
            },
            "gain_value": {
                "type": "integer",
                "label": "Gain Index (0..16, *3 dB)",
                "min": 0,
                "max": 16,
            },
            "gain_limit": {
                "type": "integer",
                "label": "Gain Limit Index (0..8, 24..48 dB)",
                "min": 0,
                "max": 8,
            },
            "wb_mode": {
                "type": "enum",
                "values": ["auto", "indoor", "outdoor", "one_push", "manual"],
                "label": "White Balance Mode",
            },
            "color_temperature": {
                "type": "integer",
                "label": "Color Temperature (K)",
                "min": 2500,
                "max": 10000,
            },
            "saturation": {
                "type": "integer",
                "label": "Saturation (0..10)",
                "min": 0,
                "max": 10,
            },
            "contrast": {
                "type": "integer",
                "label": "Contrast (0..4)",
                "min": 0,
                "max": 4,
            },
            "sharpness": {
                "type": "integer",
                "label": "Sharpness (0..3)",
                "min": 0,
                "max": 3,
            },
            "noise_filter": {
                "type": "enum",
                "values": ["off", "low", "medium", "high"],
                "label": "Noise Filter",
            },
            "back_light": {
                "type": "enum",
                "values": ["off", "low", "high"],
                "label": "Back Light Compensation",
            },
            "mirror_flip": {
                "type": "enum",
                "values": ["off", "mirror", "flip", "both"],
                "label": "Mirror / Flip",
            },
            "smart_shoot": {
                "type": "boolean",
                "label": "SmartShoot",
            },
            "smart_framing": {
                "type": "boolean",
                "label": "SmartFraming",
            },
            "slow_shutter": {
                "type": "boolean",
                "label": "Slow Shutter",
            },
            "pt_slow": {
                "type": "boolean",
                "label": "PTZ Slow Mode",
            },
            "power_frequency": {
                "type": "enum",
                "values": ["50hz", "60hz", "auto"],
                "label": "Power Frequency",
            },
            # Identity (from get_sys_stat)
            "model_name": {"type": "string", "label": "Model"},
            "fw_version": {"type": "string", "label": "Firmware Version"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "mac_address": {"type": "string", "label": "MAC Address"},
        },
        "commands": {
            # ── Power (VISCA) ──
            "power_on": {"label": "Power On", "params": {}},
            "power_off": {
                "label": "Power Off (Standby)",
                "params": {},
            },

            # ── Pan/Tilt continuous (VISCA, with per-command speed) ──
            "pt_up":         {"label": "Pan/Tilt Up",         "params": {}},
            "pt_down":       {"label": "Pan/Tilt Down",       "params": {}},
            "pt_left":       {"label": "Pan/Tilt Left",       "params": {}},
            "pt_right":      {"label": "Pan/Tilt Right",      "params": {}},
            "pt_up_left":    {"label": "Pan/Tilt Up-Left",    "params": {}},
            "pt_up_right":   {"label": "Pan/Tilt Up-Right",   "params": {}},
            "pt_down_left":  {"label": "Pan/Tilt Down-Left",  "params": {}},
            "pt_down_right": {"label": "Pan/Tilt Down-Right", "params": {}},
            "pt_stop":       {"label": "Pan/Tilt Stop",       "params": {}},
            "pt_home":       {"label": "Pan/Tilt Home",       "params": {}},
            "pt_absolute": {
                "label": "Pan/Tilt Absolute",
                "params": {
                    "pan":  {"type": "integer", "required": True, "min": -32768, "max": 32767},
                    "tilt": {"type": "integer", "required": True, "min": -32768, "max": 32767},
                },
            },

            # ── Zoom (VISCA) ──
            "zoom_tele": {"label": "Zoom In (Tele)", "params": {}},
            "zoom_wide": {"label": "Zoom Out (Wide)", "params": {}},
            "zoom_stop": {"label": "Zoom Stop", "params": {}},
            "zoom_direct": {
                "label": "Zoom Direct",
                "params": {
                    "position": {"type": "integer", "required": True, "min": 0, "max": 16384},
                },
            },

            # ── Focus (VISCA) ──
            "focus_far":  {"label": "Focus Far",  "params": {}},
            "focus_near": {"label": "Focus Near", "params": {}},
            "focus_stop": {"label": "Focus Stop", "params": {}},
            "focus_mode_auto":   {"label": "Focus Mode: Auto",   "params": {}},
            "focus_mode_manual": {"label": "Focus Mode: Manual", "params": {}},
            "focus_one_push": {
                "label": "Focus One-Push Trigger",
                "params": {},
                "help": "Trigger one-shot autofocus from manual mode.",
            },

            # ── Presets (HTTP) — AVer supports 1-255 ──
            "preset_recall": {
                "label": "Preset Recall",
                "params": {
                    "number": {"type": "integer", "required": True, "min": 1, "max": 255},
                },
            },
            "preset_save": {
                "label": "Preset Save",
                "params": {
                    "number": {"type": "integer", "required": True, "min": 1, "max": 255},
                },
            },

            # ── Image / exposure (HTTP) ──
            "set_exposure_mode": {
                "label": "Set Exposure Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": [
                            "full_auto", "shutter_priority",
                            "iris_priority", "manual",
                        ],
                    },
                },
            },
            "set_exposure_value": {
                "label": "Set Exposure Compensation (-4..4)",
                "params": {
                    "value": {"type": "integer", "required": True, "min": -4, "max": 4},
                },
            },
            "set_shutter": {
                "label": "Set Shutter Index (0..15)",
                "params": {
                    "value": {"type": "integer", "required": True, "min": 0, "max": 15},
                },
                "help": (
                    "Index into the camera's shutter table. Lower index = "
                    "longer shutter (1/32k..1/1)."
                ),
            },
            "set_iris": {
                "label": "Set Iris Index (0..13)",
                "params": {
                    "value": {"type": "integer", "required": True, "min": 0, "max": 13},
                },
                "help": "Index into F-stop table (F1.6..F14).",
            },
            "set_gain": {
                "label": "Set Gain Index (0..16, *3 dB)",
                "params": {
                    "value": {"type": "integer", "required": True, "min": 0, "max": 16},
                },
            },
            "set_gain_limit": {
                "label": "Set Gain Limit Index (0..8, 24..48 dB)",
                "params": {
                    "value": {"type": "integer", "required": True, "min": 0, "max": 8},
                },
            },
            "set_slow_shutter": {
                "label": "Slow Shutter On/Off",
                "params": {
                    "enabled": {"type": "boolean", "required": True},
                },
            },
            "set_back_light": {
                "label": "Set Back Light Compensation",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["off", "low", "high"],
                    },
                },
            },

            # ── White balance (HTTP) ──
            "set_wb_mode": {
                "label": "Set White Balance Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["auto", "indoor", "outdoor", "one_push", "manual"],
                    },
                },
            },
            "set_color_temperature": {
                "label": "Set Color Temperature (2500..10000 K)",
                "params": {
                    "value": {"type": "integer", "required": True, "min": 2500, "max": 10000},
                },
            },
            "wb_one_push_trigger": {
                "label": "One-Push White Balance Trigger",
                "params": {},
            },

            # ── Picture (HTTP) ──
            "set_saturation": {
                "label": "Set Saturation (0..10)",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 10}},
            },
            "set_contrast": {
                "label": "Set Contrast (0..4)",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 4}},
            },
            "set_sharpness": {
                "label": "Set Sharpness (0..3)",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 3}},
            },
            "set_noise_filter": {
                "label": "Set Noise Filter",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["off", "low", "medium", "high"],
                    },
                },
            },
            "set_mirror_flip": {
                "label": "Set Mirror / Flip",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["off", "mirror", "flip", "both"],
                    },
                },
            },
            "set_power_frequency": {
                "label": "Set Power Frequency",
                "params": {
                    "value": {
                        "type": "enum",
                        "required": True,
                        "values": ["50hz", "60hz", "auto"],
                    },
                },
            },
            "image_defaults": {
                "label": "Reset Image to Defaults",
                "params": {},
            },

            # ── Speeds & misc (HTTP) ──
            "set_pan_speed": {
                "label": "Set Default Pan Speed (1..24)",
                "params": {"value": {"type": "integer", "required": True, "min": 1, "max": 24}},
                "help": "Persists on the camera; affects HTTP movement commands.",
            },
            "set_tilt_speed": {
                "label": "Set Default Tilt Speed (1..24)",
                "params": {"value": {"type": "integer", "required": True, "min": 1, "max": 24}},
            },
            "set_zoom_speed": {
                "label": "Set Default Zoom Speed (0..6)",
                "params": {"value": {"type": "integer", "required": True, "min": 0, "max": 6}},
            },
            "set_preset_speed": {
                "label": "Set Preset Recall Speed",
                "params": {
                    "value": {"type": "integer", "required": True, "min": 1, "max": 6},
                },
                "help": "Maps 1..6 to 5/25/50/100/150/200 deg/sec on the camera.",
            },
            "set_pt_slow": {
                "label": "PTZ Slow Mode On/Off",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "freeze": {"label": "Freeze Image", "params": {}},

            # ── AI / Smart features (HTTP) ──
            "smart_shoot": {
                "label": "SmartShoot On/Off",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },
            "smart_framing": {
                "label": "SmartFraming On/Off",
                "params": {"enabled": {"type": "boolean", "required": True}},
            },

            # ── Streaming (HTTP) ──
            "rtmp_start": {
                "label": "Start RTMP Stream",
                "params": {
                    "server": {"type": "string", "required": True, "label": "RTMP Server URL"},
                    "key":    {"type": "string", "required": True, "label": "Stream Key"},
                },
            },
            "rtmp_stop": {"label": "Stop RTMP Stream", "params": {}},

            # ── System (HTTP, Basic auth) ──
            "reboot": {
                "label": "Reboot Camera",
                "params": {},
                "help": (
                    "Requires Basic auth (PTZ-S310/S330 only). Provide "
                    "username/password in the device config."
                ),
            },
            "factory_reset": {
                "label": "Factory Reset",
                "params": {},
                "help": "Requires Basic auth (PTZ-S310/S330 only).",
            },
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._http: httpx.AsyncClient | None = None
        self._base_url: str = ""
        self._visca_transport: asyncio.DatagramTransport | None = None
        self._visca_protocol: _ViscaUDPProtocol | None = None
        self._sequence: int = 0
        self._inquiry_lock: asyncio.Lock | None = None
        self._inquiry_future: asyncio.Future[bytes] | None = None
        self._control_reply_future: asyncio.Future[None] | None = None

    # ── Connect / disconnect ──

    async def connect(self) -> None:
        host = self.config.get("host", "")
        if not host:
            raise ConnectionError(f"[{self.device_id}] host is required")
        port = int(self.config.get("port", 80))
        visca_port = int(self.config.get("visca_port", _PT_VISCA_PORT))

        self._base_url = f"http://{host}:{port}"
        self._inquiry_lock = asyncio.Lock()
        self._sequence = 0

        username = self.config.get("username", "") or ""
        password = self.config.get("password", "") or ""
        auth = (username, password) if username else None

        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=5.0,
            auth=auth,
        )

        # VISCA UDP socket
        try:
            loop = asyncio.get_running_loop()
            self._visca_protocol = _ViscaUDPProtocol(self._on_visca_data)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: self._visca_protocol,
                remote_addr=(host, visca_port),
            )
            self._visca_transport = transport
        except Exception as e:
            log.warning(
                f"[{self.device_id}] VISCA UDP open failed: {e}"
            )

        # Sync VISCA sequence — Control RESET. Some firmware doesn't reply;
        # don't fail connect if it times out.
        try:
            await self._send_control_reset()
        except (ConnectionError, OSError, asyncio.TimeoutError):
            log.debug(f"[{self.device_id}] VISCA RESET not ack'd; continuing")

        # HTTP probe via get_sys_stat — confirms HTTP reachability and
        # populates identity state.
        ok = await self._http_get_sys_stat()
        if not ok:
            await self.disconnect()
            raise ConnectionError(
                f"[{self.device_id}] HTTP CGI probe failed at {self._base_url}"
            )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")

        try:
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

        poll_interval = int(self.config.get("poll_interval", 5))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self._inquiry_future and not self._inquiry_future.done():
            self._inquiry_future.cancel()
        self._inquiry_future = None
        if self._control_reply_future and not self._control_reply_future.done():
            self._control_reply_future.cancel()
        self._control_reply_future = None
        if self._visca_transport:
            self._visca_transport.close()
            self._visca_transport = None
        self._visca_protocol = None
        if self._http:
            await self._http.aclose()
            self._http = None
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")

    # ── VISCA UDP datagram callback ──

    def _on_visca_data(self, data: bytes) -> None:
        unwrapped = _unwrap(data)
        if unwrapped is None:
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
            return  # ACK
        if second == 0x60:
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

    # ── VISCA send / inquire ──

    def _next_seq(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        return self._sequence

    def _send_visca(self, visca: bytes) -> None:
        if not self._visca_transport:
            log.warning(f"[{self.device_id}] VISCA transport unavailable")
            return
        seq = self._next_seq()
        self._visca_transport.sendto(_wrap(visca, PAYLOAD_VISCA_COMMAND, seq))

    def _send_visca_inquiry(self, visca: bytes) -> None:
        if not self._visca_transport:
            return
        seq = self._next_seq()
        self._visca_transport.sendto(_wrap(visca, PAYLOAD_VISCA_INQUIRY, seq))

    async def _send_control_reset(self, timeout: float = 2.0) -> None:
        if not self._visca_transport:
            return
        loop = asyncio.get_running_loop()
        self._control_reply_future = loop.create_future()
        try:
            self._visca_transport.sendto(
                _wrap(CONTROL_RESET, PAYLOAD_CONTROL_CMD, 0)
            )
            await asyncio.wait_for(self._control_reply_future, timeout=timeout)
            self._sequence = 0
        finally:
            self._control_reply_future = None

    async def _inquire(self, payload: bytes, timeout: float = 1.5) -> bytes | None:
        if not self._inquiry_lock or not self._visca_transport:
            return None
        async with self._inquiry_lock:
            loop = asyncio.get_running_loop()
            self._inquiry_future = loop.create_future()
            try:
                self._send_visca_inquiry(payload)
                try:
                    reply = await asyncio.wait_for(
                        self._inquiry_future, timeout=timeout
                    )
                except asyncio.TimeoutError:
                    return None
                return reply or None
            finally:
                self._inquiry_future = None

    # ── HTTP helpers ──

    async def _cgi(self, cmd: str) -> str | None:
        """Fire a /storks?cmd=<cmd> GET. Returns response text or None."""
        if not self._http:
            return None
        try:
            resp = await self._http.get(f"/storks?cmd={cmd}")
            if resp.status_code == 200:
                return resp.text
            log.debug(
                f"[{self.device_id}] CGI '{cmd}' status={resp.status_code}"
            )
            return None
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as e:
            log.warning(f"[{self.device_id}] CGI '{cmd}' error: {e}")
            return None

    async def _cgi_post(self, cmd: str) -> str | None:
        if not self._http:
            return None
        try:
            resp = await self._http.post(f"/storks?cmd={cmd}")
            if resp.status_code == 200:
                return resp.text
            return None
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
            return None

    async def _http_get_sys_stat(self) -> bool:
        text = await self._cgi("get_sys_stat")
        if text is None:
            return False
        for pair in text.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "model_name":
                self.set_state("model_name", v)
            elif k == "fw_ver":
                self.set_state("fw_version", v)
            elif k == "sn":
                self.set_state("serial_number", v)
            elif k == "mac":
                self.set_state("mac_address", v)
        return True

    # ── Commands ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}
        if not self._http:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        # ── VISCA: power, motion, position, focus mode ──
        match command:
            case "power_on":
                self._send_visca(b"\x81\x01\x04\x00\x02\xff")
                return
            case "power_off":
                self._send_visca(b"\x81\x01\x04\x00\x03\xff")
                return
            case "pt_home":
                self._send_visca(b"\x81\x01\x06\x04\xff")
                return
            case "pt_stop":
                pan_speed = self._cfg_pan_speed()
                tilt_speed = self._cfg_tilt_speed()
                self._send_visca(
                    b"\x81\x01\x06\x01"
                    + bytes([pan_speed, tilt_speed, 0x03, 0x03])
                    + b"\xff"
                )
                return
            case "pt_absolute":
                pan = int(params["pan"])
                tilt = int(params["tilt"])
                pan_speed = self._cfg_pan_speed()
                tilt_speed = self._cfg_tilt_speed()
                self._send_visca(
                    b"\x81\x01\x06\x02"
                    + bytes([pan_speed, tilt_speed])
                    + _encode_4nibble(pan)
                    + _encode_4nibble(tilt)
                    + b"\xff"
                )
                return
            case "zoom_tele":
                self._send_visca(b"\x81\x01\x04\x07\x02\xff")
                return
            case "zoom_wide":
                self._send_visca(b"\x81\x01\x04\x07\x03\xff")
                return
            case "zoom_stop":
                self._send_visca(b"\x81\x01\x04\x07\x00\xff")
                return
            case "zoom_direct":
                pos = max(0, min(0x4000, int(params["position"])))
                self._send_visca(
                    b"\x81\x01\x04\x47" + _encode_4nibble(pos) + b"\xff"
                )
                return
            case "focus_far":
                self._send_visca(b"\x81\x01\x04\x08\x02\xff")
                return
            case "focus_near":
                self._send_visca(b"\x81\x01\x04\x08\x03\xff")
                return
            case "focus_stop":
                self._send_visca(b"\x81\x01\x04\x08\x00\xff")
                return
            case "focus_mode_auto":
                self._send_visca(b"\x81\x01\x04\x38\x02\xff")
                return
            case "focus_mode_manual":
                self._send_visca(b"\x81\x01\x04\x38\x03\xff")
                return
            case "focus_one_push":
                self._send_visca(b"\x81\x01\x04\x18\x01\xff")
                return

        # ── VISCA: continuous PT drive with per-command speed ──
        pt_dirs = {
            "pt_up":         (0x03, 0x01),
            "pt_down":       (0x03, 0x02),
            "pt_left":       (0x01, 0x03),
            "pt_right":      (0x02, 0x03),
            "pt_up_left":    (0x01, 0x01),
            "pt_up_right":   (0x02, 0x01),
            "pt_down_left":  (0x01, 0x02),
            "pt_down_right": (0x02, 0x02),
        }
        if command in pt_dirs:
            pan_dir, tilt_dir = pt_dirs[command]
            pan_speed = self._cfg_pan_speed()
            tilt_speed = self._cfg_tilt_speed()
            self._send_visca(
                b"\x81\x01\x06\x01"
                + bytes([pan_speed, tilt_speed, pan_dir, tilt_dir])
                + b"\xff"
            )
            return

        # ── HTTP CGI ──
        match command:
            case "preset_recall":
                num = max(1, min(255, int(params["number"])))
                await self._cgi(f"load_preset:{num}")
            case "preset_save":
                num = max(1, min(255, int(params["number"])))
                await self._cgi(f"set_preset:{num}")

            case "set_exposure_mode":
                mode = str(params["mode"])
                if mode not in _EXP_MODE_TO_INT:
                    raise ValueError(f"Unknown exposure mode: {mode}")
                await self._cgi(f"set_expmode:{_EXP_MODE_TO_INT[mode]}")
                self.set_state("exposure_mode", mode)
            case "set_exposure_value":
                v = max(-4, min(4, int(params["value"])))
                await self._cgi(f"set_expval:{v}")
                self.set_state("exposure_value", v)
            case "set_shutter":
                v = max(0, min(15, int(params["value"])))
                await self._cgi(f"set_shutter:{v}")
                self.set_state("shutter_value", v)
            case "set_iris":
                v = max(0, min(13, int(params["value"])))
                await self._cgi(f"set_iris:{v}")
                self.set_state("iris_value", v)
            case "set_gain":
                v = max(0, min(16, int(params["value"])))
                await self._cgi(f"set_gain:{v}")
                self.set_state("gain_value", v)
            case "set_gain_limit":
                v = max(0, min(8, int(params["value"])))
                await self._cgi(f"set_gain_limit:{v}")
                self.set_state("gain_limit", v)
            case "set_slow_shutter":
                on = bool(params["enabled"])
                await self._cgi(f"set_slow_shutter:{'on' if on else 'off'}")
                self.set_state("slow_shutter", on)
            case "set_back_light":
                mode = str(params["mode"])
                if mode not in _BLC_TO_INT:
                    raise ValueError(f"Unknown back light mode: {mode}")
                await self._cgi(f"set_blc:{_BLC_TO_INT[mode]}")
                self.set_state("back_light", mode)

            case "set_wb_mode":
                mode = str(params["mode"])
                if mode not in _WB_MODE_TO_INT:
                    raise ValueError(f"Unknown WB mode: {mode}")
                await self._cgi(f"set_wb:{_WB_MODE_TO_INT[mode]}")
                self.set_state("wb_mode", mode)
            case "set_color_temperature":
                v = max(2500, min(10000, int(params["value"])))
                await self._cgi(f"set_colortemp:{v}")
                self.set_state("color_temperature", v)
            case "wb_one_push_trigger":
                await self._cgi("set_one_push_wb")

            case "set_saturation":
                v = max(0, min(10, int(params["value"])))
                await self._cgi(f"set_saturation:{v}")
                self.set_state("saturation", v)
            case "set_contrast":
                v = max(0, min(4, int(params["value"])))
                await self._cgi(f"set_contrast:{v}")
                self.set_state("contrast", v)
            case "set_sharpness":
                v = max(0, min(3, int(params["value"])))
                await self._cgi(f"set_sharpness:{v}")
                self.set_state("sharpness", v)
            case "set_noise_filter":
                mode = str(params["mode"])
                if mode not in _NF_TO_INT:
                    raise ValueError(f"Unknown noise filter mode: {mode}")
                await self._cgi(f"set_nf:{_NF_TO_INT[mode]}")
                self.set_state("noise_filter", mode)
            case "set_mirror_flip":
                mode = str(params["mode"])
                if mode not in _MIRROR_FLIP_TO_INT:
                    raise ValueError(f"Unknown mirror/flip mode: {mode}")
                await self._cgi(f"mirror_flip:{_MIRROR_FLIP_TO_INT[mode]}")
                self.set_state("mirror_flip", mode)
            case "set_power_frequency":
                value = str(params["value"])
                if value not in _FREQ_TO_INT:
                    raise ValueError(f"Unknown power frequency: {value}")
                await self._cgi(f"set_freq:{_FREQ_TO_INT[value]}")
                self.set_state("power_frequency", value)
            case "image_defaults":
                await self._cgi("set_img_default")

            case "set_pan_speed":
                v = max(1, min(24, int(params["value"])))
                await self._cgi(f"set_pan_speed:{v}")
            case "set_tilt_speed":
                v = max(1, min(24, int(params["value"])))
                await self._cgi(f"set_tilt_speed:{v}")
            case "set_zoom_speed":
                v = max(0, min(6, int(params["value"])))
                await self._cgi(f"set_zoom_speed:{v}")
            case "set_preset_speed":
                v = max(1, min(6, int(params["value"])))
                await self._cgi(f"set_preset_speed:{v}")
            case "set_pt_slow":
                on = bool(params["enabled"])
                await self._cgi(f"set_pt_slow:{'on' if on else 'off'}")
                self.set_state("pt_slow", on)
            case "freeze":
                await self._cgi("set_freeze")

            case "smart_shoot":
                on = bool(params["enabled"])
                await self._cgi(f"set_smartshot:{1 if on else 0}")
                self.set_state("smart_shoot", on)
            case "smart_framing":
                on = bool(params["enabled"])
                await self._cgi(f"set_smartframing:{1 if on else 0}")
                self.set_state("smart_framing", on)

            case "rtmp_start":
                server = quote(str(params["server"]), safe="")
                key = quote(str(params["key"]), safe="")
                await self._cgi(
                    f"set_rtmp_start(server=\"{server}\",key=\"{key}\")"
                )
            case "rtmp_stop":
                await self._cgi("set_rtmp_stop")

            case "reboot":
                await self._cgi("sys_reboot")
            case "factory_reset":
                await self._cgi_post("set_factory_default")

            case _:
                raise ValueError(f"Unknown command: {command}")

    # ── Polling ──

    async def poll(self) -> None:
        if not self._http:
            return

        # VISCA: power + position
        if self._visca_transport:
            reply = await self._inquire(b"\x81\x09\x04\x00\xff")
            if reply and len(reply) == 4:
                self.set_state("power", "on" if reply[2] == 0x02 else "standby")

            reply = await self._inquire(b"\x81\x09\x06\x12\xff")
            if reply and len(reply) == 11:
                self.set_state("pan_position", _decode_4nibble(reply[2:6], signed=True))
                self.set_state("tilt_position", _decode_4nibble(reply[6:10], signed=True))

            reply = await self._inquire(b"\x81\x09\x04\x47\xff")
            if reply and len(reply) == 7:
                self.set_state("zoom_position", _decode_4nibble(reply[2:6]))

        # HTTP get_sys_stat — identity and a handful of state keys
        text = await self._cgi("get_sys_stat")
        if text:
            for pair in text.split(";"):
                pair = pair.strip()
                if "=" not in pair:
                    continue
                k, v = pair.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "model_name":
                    self.set_state("model_name", v)
                elif k == "fw_ver":
                    self.set_state("fw_version", v)
                elif k == "sn":
                    self.set_state("serial_number", v)
                elif k == "mac":
                    self.set_state("mac_address", v)
                elif k == "expmode" and v.isdigit():
                    mode = _INT_TO_EXP_MODE.get(int(v))
                    if mode:
                        self.set_state("exposure_mode", mode)
                elif k == "wb" and v.isdigit():
                    mode = _INT_TO_WB_MODE.get(int(v))
                    if mode:
                        self.set_state("wb_mode", mode)
                elif k == "colortemp" and v.isdigit():
                    self.set_state("color_temperature", int(v))
                elif k == "saturation" and v.isdigit():
                    self.set_state("saturation", int(v))
                elif k == "contrast" and v.isdigit():
                    self.set_state("contrast", int(v))
                elif k == "sharpness" and v.isdigit():
                    self.set_state("sharpness", int(v))
                elif k == "expval" and v.lstrip("-").isdigit():
                    self.set_state("exposure_value", int(v))
                elif k == "shutter" and v.isdigit():
                    self.set_state("shutter_value", int(v))
                elif k == "iris" and v.isdigit():
                    self.set_state("iris_value", int(v))
                elif k == "gain" and v.isdigit():
                    self.set_state("gain_value", int(v))
                elif k == "gain_limit" and v.isdigit():
                    self.set_state("gain_limit", int(v))
                elif k == "nf" and v.isdigit():
                    mode = _INT_TO_NF.get(int(v))
                    if mode:
                        self.set_state("noise_filter", mode)
                elif k == "blc" and v.isdigit():
                    mode = _INT_TO_BLC.get(int(v))
                    if mode:
                        self.set_state("back_light", mode)
                elif k == "mirror_flip" and v.isdigit():
                    mode = _INT_TO_MIRROR_FLIP.get(int(v))
                    if mode:
                        self.set_state("mirror_flip", mode)
                elif k == "freq" and v.isdigit():
                    mode = _INT_TO_FREQ.get(int(v))
                    if mode:
                        self.set_state("power_frequency", mode)
                elif k == "smartshot" and v.isdigit():
                    self.set_state("smart_shoot", v == "1")
                elif k == "smartframing" and v.isdigit():
                    self.set_state("smart_framing", v == "1")
                elif k == "slow_shutter":
                    self.set_state("slow_shutter", v == "on")
                elif k == "pt_slow":
                    self.set_state("pt_slow", v == "on")

    # ── Helpers ──

    def _cfg_pan_speed(self) -> int:
        return max(1, min(24, int(self.config.get("pan_speed", 12))))

    def _cfg_tilt_speed(self) -> int:
        return max(1, min(20, int(self.config.get("tilt_speed", 10))))


class _ViscaUDPProtocol(asyncio.DatagramProtocol):
    """Minimal asyncio datagram protocol that forwards each datagram to a callback."""

    def __init__(self, on_data) -> None:
        self._on_data = on_data

    def datagram_received(self, data, addr) -> None:  # noqa: D401
        try:
            self._on_data(data)
        except Exception:
            log.exception("VISCA datagram handler error")
