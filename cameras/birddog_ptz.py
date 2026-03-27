"""
OpenAVC BirdDog PTZ Camera Driver.

Controls BirdDog PTZ cameras via the REST API (port 8080) for configuration
and VISCA-over-UDP (port 52381) for real-time PTZ movement.

Supported models: P100, P110, P120, P200, P240, P400, P4K, A200, A300,
X1, X1 Ultra, MAKI Ultra, and newer models with the same API.

REST API (port 8080, HTTP, JSON):
  GET/POST /about              — Device info (hostname, firmware, model, format)
  GET/POST /birddogptzsetup    — PTZ config (pan/tilt speed, preset speed)
  POST     /recall             — Recall PTZ preset  {"Preset": "Preset-1"}
  POST     /save               — Save PTZ preset    {"Preset": "Preset-1"}
  GET/POST /birddogexpsetup    — Exposure settings (mode, gain, iris, shutter)
  GET/POST /birddogwbsetup     — White balance settings (mode, red/blue gain)
  GET/POST /birddogpicsetup    — Picture settings (brightness, contrast, etc.)
  GET/POST /encodesetup        — NDI encode settings (bandwidth, format, tally)
  GET/POST /analogaudiosetup   — Audio settings (gain, output select)
  GET/POST /tally              — Tally state (program/preview)
  GET/POST /NDIDisServer       — NDI discovery server config

VISCA-over-UDP (port 52381):
  Used for real-time pan/tilt/zoom movement commands. BirdDog cameras
  implement the VISCA-over-IP protocol (Sony standard) with the same
  packet format used by all VISCA-over-IP devices.

No authentication required. No external SDK or runtime needed.

Reference:
  - BirdDog REST API: https://birddog.tv/AV/API/index.html
  - Bitfocus Companion module: github.com/bitfocus/companion-module-birddog-ptz
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import httpx

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# VISCA-over-IP constants
_VISCA_PORT = 52381
_VISCA_HEADER = b"\x01\x10"  # VISCA command type

# VISCA command bytes
_VISCA_MSG_CAM = b"\x81\x01\x04"
_VISCA_CAM_POWER_ON = b"\x00\x02"
_VISCA_CAM_POWER_OFF = b"\x00\x03"
_VISCA_END = b"\xFF"

# Pan/Tilt commands: 81 01 06 01 VV WW XX XX YY YY FF
# VV=pan speed (01-18), WW=tilt speed (01-14)
# XX XX = pan direction, YY YY = tilt direction
_VISCA_PT_PREFIX = b"\x81\x01\x06\x01"
_VISCA_PT_STOP = b"\x03\x03"
_VISCA_PT_UP = b"\x03\x01"
_VISCA_PT_DOWN = b"\x03\x02"
_VISCA_PT_LEFT = b"\x01\x03"
_VISCA_PT_RIGHT = b"\x02\x03"
_VISCA_PT_UP_LEFT = b"\x01\x01"
_VISCA_PT_UP_RIGHT = b"\x02\x01"
_VISCA_PT_DOWN_LEFT = b"\x01\x02"
_VISCA_PT_DOWN_RIGHT = b"\x02\x02"

# Zoom commands: 81 01 04 07 XX FF
_VISCA_ZOOM_STOP = b"\x81\x01\x04\x07\x00\xFF"
_VISCA_ZOOM_TELE = b"\x81\x01\x04\x07\x02\xFF"  # Zoom in
_VISCA_ZOOM_WIDE = b"\x81\x01\x04\x07\x03\xFF"  # Zoom out

# Focus commands
_VISCA_FOCUS_AUTO = b"\x81\x01\x04\x38\x02\xFF"
_VISCA_FOCUS_MANUAL = b"\x81\x01\x04\x38\x03\xFF"
_VISCA_FOCUS_FAR = b"\x81\x01\x04\x08\x02\xFF"
_VISCA_FOCUS_NEAR = b"\x81\x01\x04\x08\x03\xFF"
_VISCA_FOCUS_STOP = b"\x81\x01\x04\x08\x00\xFF"
_VISCA_FOCUS_ONE_PUSH = b"\x81\x01\x04\x18\x01\xFF"

# Home position
_VISCA_PT_HOME = b"\x81\x01\x06\x04\xFF"


def _build_visca_ip_packet(payload: bytes, counter: int) -> bytes:
    """Wrap a VISCA payload in a VISCA-over-IP packet."""
    header = _VISCA_HEADER
    length = struct.pack(">H", len(payload))
    seq = struct.pack(">I", counter & 0xFFFFFFFF)
    return header + length + seq + payload


def _build_pt_command(
    direction: bytes, pan_speed: int = 8, tilt_speed: int = 8
) -> bytes:
    """Build a VISCA pan/tilt continuous movement command."""
    ps = max(1, min(24, pan_speed)).to_bytes(1, "big")
    ts = max(1, min(20, tilt_speed)).to_bytes(1, "big")
    return _VISCA_PT_PREFIX + ps + ts + direction + _VISCA_END


class BirdDogPTZDriver(BaseDriver):
    """BirdDog PTZ camera driver via REST API + VISCA-over-UDP."""

    DRIVER_INFO = {
        "id": "birddog_ptz",
        "name": "BirdDog PTZ Camera",
        "manufacturer": "BirdDog",
        "category": "camera",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls BirdDog PTZ cameras via REST API and VISCA. "
            "Pan, tilt, zoom, focus, presets, exposure, white balance, "
            "tally, and NDI encode settings."
        ),
        "transport": "http",
        "help": {
            "overview": (
                "Controls any BirdDog PTZ camera — P100, P200, P240, P400, "
                "A200, A300, X1, MAKI, and newer models. Uses the built-in "
                "REST API for configuration and VISCA-over-UDP for real-time "
                "PTZ movement.\n\n"
                "No external software, SDK, or runtime required."
            ),
            "setup": (
                "1. Enter the camera's IP address (find it via the camera's "
                "on-screen display or BirdDog Central).\n"
                "2. Default REST port is 8080, VISCA port is 52381.\n"
                "3. No authentication is required.\n"
                "4. Use 'recall_preset' and 'save_preset' with preset "
                "numbers (1-255 depending on model)."
            ),
        },
        "default_config": {
            "host": "",
            "port": 8080,
            "poll_interval": 5,
            "pan_speed": 8,
            "tilt_speed": 8,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 8080, "label": "REST API Port"},
            "pan_speed": {
                "type": "integer",
                "default": 8,
                "min": 1,
                "max": 24,
                "label": "Default Pan Speed",
            },
            "tilt_speed": {
                "type": "integer",
                "default": 8,
                "min": 1,
                "max": 20,
                "label": "Default Tilt Speed",
            },
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 0,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "hostname": {"type": "string", "label": "Hostname"},
            "model": {"type": "string", "label": "Model"},
            "firmware": {"type": "string", "label": "Firmware Version"},
            "video_format": {"type": "string", "label": "Video Format"},
            "ndi_name": {"type": "string", "label": "NDI Source Name"},
            "tally_mode": {"type": "string", "label": "Tally Mode"},
            "exposure_mode": {"type": "string", "label": "Exposure Mode"},
            "wb_mode": {"type": "string", "label": "White Balance Mode"},
        },
        "commands": {
            "pt_up": {
                "label": "Pan/Tilt Up",
                "params": {},
                "help": "Start tilting up. Send 'pt_stop' to stop.",
            },
            "pt_down": {
                "label": "Pan/Tilt Down",
                "params": {},
                "help": "Start tilting down. Send 'pt_stop' to stop.",
            },
            "pt_left": {
                "label": "Pan/Tilt Left",
                "params": {},
                "help": "Start panning left. Send 'pt_stop' to stop.",
            },
            "pt_right": {
                "label": "Pan/Tilt Right",
                "params": {},
                "help": "Start panning right. Send 'pt_stop' to stop.",
            },
            "pt_up_left": {
                "label": "Pan/Tilt Up-Left",
                "params": {},
            },
            "pt_up_right": {
                "label": "Pan/Tilt Up-Right",
                "params": {},
            },
            "pt_down_left": {
                "label": "Pan/Tilt Down-Left",
                "params": {},
            },
            "pt_down_right": {
                "label": "Pan/Tilt Down-Right",
                "params": {},
            },
            "pt_stop": {
                "label": "Pan/Tilt Stop",
                "params": {},
                "help": "Stop all pan/tilt movement.",
            },
            "pt_home": {
                "label": "Pan/Tilt Home",
                "params": {},
                "help": "Move to the home position.",
            },
            "zoom_in": {
                "label": "Zoom In",
                "params": {},
                "help": "Start zooming in (telephoto). Send 'zoom_stop' to stop.",
            },
            "zoom_out": {
                "label": "Zoom Out",
                "params": {},
                "help": "Start zooming out (wide). Send 'zoom_stop' to stop.",
            },
            "zoom_stop": {
                "label": "Zoom Stop",
                "params": {},
            },
            "focus_auto": {
                "label": "Auto Focus",
                "params": {},
            },
            "focus_manual": {
                "label": "Manual Focus",
                "params": {},
            },
            "focus_near": {
                "label": "Focus Near",
                "params": {},
                "help": "Start focusing nearer. Send 'focus_stop' to stop.",
            },
            "focus_far": {
                "label": "Focus Far",
                "params": {},
                "help": "Start focusing farther. Send 'focus_stop' to stop.",
            },
            "focus_stop": {
                "label": "Focus Stop",
                "params": {},
            },
            "focus_one_push": {
                "label": "One-Push Auto Focus",
                "params": {},
                "help": "Trigger a single auto-focus operation.",
            },
            "recall_preset": {
                "label": "Recall Preset",
                "params": {
                    "preset": {
                        "type": "integer",
                        "min": 1,
                        "max": 255,
                        "required": True,
                        "label": "Preset Number",
                    },
                },
                "help": "Move the camera to a saved preset position.",
            },
            "save_preset": {
                "label": "Save Preset",
                "params": {
                    "preset": {
                        "type": "integer",
                        "min": 1,
                        "max": 255,
                        "required": True,
                        "label": "Preset Number",
                    },
                },
                "help": "Save the current camera position to a preset.",
            },
            "set_exposure_mode": {
                "label": "Set Exposure Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "values": [
                            "FULL AUTO",
                            "MANUAL",
                            "SHUTTER Pri",
                            "IRIS Pri",
                            "BRIGHT",
                        ],
                        "required": True,
                        "label": "Exposure Mode",
                    },
                },
            },
            "set_wb_mode": {
                "label": "Set White Balance Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "values": [
                            "AUTO",
                            "INDOOR",
                            "OUTDOOR",
                            "ONE PUSH",
                            "MANUAL",
                        ],
                        "required": True,
                        "label": "White Balance Mode",
                    },
                },
            },
            "set_tally": {
                "label": "Set Tally",
                "params": {
                    "state": {
                        "type": "enum",
                        "values": ["Off", "Program", "Preview"],
                        "required": True,
                        "label": "Tally State",
                    },
                },
                "help": "Set the camera's tally light state.",
            },
            "power_on": {
                "label": "Power On",
                "params": {},
            },
            "standby": {
                "label": "Standby",
                "params": {},
            },
        },
        "device_settings": {
            "ndi_name": {
                "type": "string",
                "label": "NDI Source Name",
                "help": (
                    "The name other devices use to subscribe to this NDI source "
                    "on the network. Must be unique across all NDI devices."
                ),
                "state_key": "ndi_name",
                "default": "BIRDDOG",
                "setup": True,
                "unique": True,
            },
            "hostname": {
                "type": "string",
                "label": "Device Hostname",
                "help": (
                    "The network hostname of this camera. Shown in BirdDog Central "
                    "and mDNS/DNS-SD discovery."
                ),
                "state_key": "hostname",
                "default": "BIRDDOG",
                "setup": True,
                "unique": True,
            },
            "tally_mode": {
                "type": "enum",
                "label": "Tally Mode",
                "help": (
                    "Controls how the camera responds to tally signals. "
                    "'Program' lights red, 'Preview' lights green."
                ),
                "values": ["Off", "Program", "Preview"],
                "state_key": "tally_mode",
                "default": "Off",
                "setup": False,
            },
            "video_format": {
                "type": "enum",
                "label": "Video Format",
                "help": (
                    "The output video resolution and frame rate. Changing this "
                    "may briefly interrupt the video stream."
                ),
                "values": [
                    "1080p60", "1080p59.94", "1080p50",
                    "1080p30", "1080p29.97", "1080p25",
                    "1080i60", "1080i59.94", "1080i50",
                    "720p60", "720p59.94", "720p50",
                ],
                "state_key": "video_format",
                "default": "1080p59.94",
                "setup": False,
            },
        },
        "discovery": {
            "ports": [8080],
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._base_url: str = ""
        self._visca_transport: asyncio.DatagramTransport | None = None
        self._visca_counter: int = 0

    async def connect(self) -> None:
        """Connect to the BirdDog camera."""
        host = self.config.get("host", "")
        port = self.config.get("port", 8080)
        self._base_url = f"http://{host}:{port}"

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=5.0,
        )

        # Verify connection
        try:
            about = await self._api_get("about")
            if not about or "HostName" not in about:
                raise ConnectionError("Unexpected response from device")

            self.set_state("hostname", about.get("HostName", ""))
            self.set_state("model", about.get("Format", ""))
            self.set_state("firmware", about.get("FirmwareVersion", ""))
            log.info(
                f"[{self.device_id}] Connected to BirdDog "
                f"{about.get('Format', '')} at {host}:{port} "
                f"({about.get('HostName', '')})"
            )
        except Exception as e:
            if self._client:
                await self._client.aclose()
                self._client = None
            raise ConnectionError(
                f"Failed to connect to BirdDog at {host}:{port}: {e}"
            )

        # Open VISCA UDP socket
        try:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                remote_addr=(host, _VISCA_PORT),
            )
            self._visca_transport = transport
            log.info(f"[{self.device_id}] VISCA UDP connected to {host}:{_VISCA_PORT}")
        except Exception as e:
            log.warning(f"[{self.device_id}] VISCA UDP failed: {e} — PTZ commands unavailable")

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")

        # Initial poll
        await self.poll()

        # Start polling
        poll_interval = self.config.get("poll_interval", 5)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        """Disconnect from the camera."""
        await self.stop_polling()
        if self._visca_transport:
            self._visca_transport.close()
            self._visca_transport = None
        if self._client:
            await self._client.aclose()
            self._client = None
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Execute a camera command."""
        params = params or {}

        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        # VISCA PTZ commands
        pt_directions = {
            "pt_up": _VISCA_PT_UP,
            "pt_down": _VISCA_PT_DOWN,
            "pt_left": _VISCA_PT_LEFT,
            "pt_right": _VISCA_PT_RIGHT,
            "pt_up_left": _VISCA_PT_UP_LEFT,
            "pt_up_right": _VISCA_PT_UP_RIGHT,
            "pt_down_left": _VISCA_PT_DOWN_LEFT,
            "pt_down_right": _VISCA_PT_DOWN_RIGHT,
            "pt_stop": _VISCA_PT_STOP,
        }

        if command in pt_directions:
            pan_speed = self.config.get("pan_speed", 8)
            tilt_speed = self.config.get("tilt_speed", 8)
            payload = _build_pt_command(
                pt_directions[command], pan_speed, tilt_speed
            )
            self._send_visca(payload)
            return

        match command:
            case "pt_home":
                self._send_visca(_VISCA_PT_HOME)

            case "zoom_in":
                self._send_visca(_VISCA_ZOOM_TELE)
            case "zoom_out":
                self._send_visca(_VISCA_ZOOM_WIDE)
            case "zoom_stop":
                self._send_visca(_VISCA_ZOOM_STOP)

            case "focus_auto":
                self._send_visca(_VISCA_FOCUS_AUTO)
            case "focus_manual":
                self._send_visca(_VISCA_FOCUS_MANUAL)
            case "focus_near":
                self._send_visca(_VISCA_FOCUS_NEAR)
            case "focus_far":
                self._send_visca(_VISCA_FOCUS_FAR)
            case "focus_stop":
                self._send_visca(_VISCA_FOCUS_STOP)
            case "focus_one_push":
                self._send_visca(_VISCA_FOCUS_ONE_PUSH)

            case "power_on":
                self._send_visca(
                    _VISCA_MSG_CAM + _VISCA_CAM_POWER_ON + _VISCA_END
                )
            case "standby":
                self._send_visca(
                    _VISCA_MSG_CAM + _VISCA_CAM_POWER_OFF + _VISCA_END
                )

            case "recall_preset":
                preset = int(params.get("preset", 1))
                await self._api_post("recall", {"Preset": f"Preset-{preset}"})
                log.info(f"[{self.device_id}] Recalled preset {preset}")

            case "save_preset":
                preset = int(params.get("preset", 1))
                await self._api_post("save", {"Preset": f"Preset-{preset}"})
                log.info(f"[{self.device_id}] Saved preset {preset}")

            case "set_exposure_mode":
                mode = params.get("mode", "FULL AUTO")
                await self._api_post("birddogexpsetup", {"ExpMode": mode})
                self.set_state("exposure_mode", mode)

            case "set_wb_mode":
                mode = params.get("mode", "AUTO")
                await self._api_post("birddogwbsetup", {"WBMode": mode})
                self.set_state("wb_mode", mode)

            case "set_tally":
                state = params.get("state", "Off")
                await self._api_post("tally", {"tally_state": state})

            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting to the camera via the REST API."""
        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match key:
            case "ndi_name":
                await self._api_post("encodesetup", {"NDIName": str(value)})
                self.set_state("ndi_name", str(value))
                log.info(f"[{self.device_id}] Set NDI name to '{value}'")

            case "hostname":
                await self._api_post("about", {"HostName": str(value)})
                self.set_state("hostname", str(value))
                log.info(f"[{self.device_id}] Set hostname to '{value}'")

            case "tally_mode":
                await self._api_post("tally", {"tally_state": str(value)})
                self.set_state("tally_mode", str(value))
                log.info(f"[{self.device_id}] Set tally mode to '{value}'")

            case "video_format":
                await self._api_post("encodesetup", {"VideoFormat": str(value)})
                self.set_state("video_format", str(value))
                log.info(f"[{self.device_id}] Set video format to '{value}'")

            case _:
                raise ValueError(f"Unknown device setting: {key}")

    async def poll(self) -> None:
        """Query camera status."""
        if not self._client:
            return

        try:
            # Device info
            about = await self._api_get("about")
            if about:
                self.set_state("hostname", about.get("HostName", ""))
                self.set_state("model", about.get("Format", ""))
                self.set_state("firmware", about.get("FirmwareVersion", ""))

            # Encode settings (includes NDI name, video format, tally mode)
            encode = await self._api_get("encodesetup")
            if encode:
                self.set_state("ndi_name", encode.get("NDIName", ""))
                self.set_state("video_format", encode.get("VideoFormat", ""))
                self.set_state("tally_mode", encode.get("TallyMode", ""))

            # Exposure
            exp = await self._api_get("birddogexpsetup")
            if exp:
                self.set_state("exposure_mode", exp.get("ExpMode", ""))

            # White balance
            wb = await self._api_get("birddogwbsetup")
            if wb:
                self.set_state("wb_mode", wb.get("WBMode", ""))

        except (httpx.ConnectError, httpx.TimeoutException):
            log.warning(f"[{self.device_id}] Poll failed — camera not responding")
        except Exception:
            log.exception(f"[{self.device_id}] Poll error")

    # --- Internal helpers ---

    def _send_visca(self, payload: bytes) -> None:
        """Send a VISCA command over UDP."""
        if not self._visca_transport:
            log.warning(f"[{self.device_id}] VISCA not available")
            return

        self._visca_counter += 1
        packet = _build_visca_ip_packet(payload, self._visca_counter)
        self._visca_transport.sendto(packet)

    async def _api_get(self, endpoint: str) -> dict | None:
        """Send a GET request to the BirdDog REST API."""
        if not self._client:
            return None
        try:
            resp = await self._client.get(f"/{endpoint}")
            if resp.status_code == 200:
                return resp.json()
            return None
        except (httpx.TimeoutException, httpx.ConnectError):
            return None
        except Exception as e:
            log.warning(f"[{self.device_id}] GET /{endpoint} error: {e}")
            return None

    async def _api_post(self, endpoint: str, body: dict) -> dict | None:
        """Send a POST request to the BirdDog REST API."""
        if not self._client:
            return None
        try:
            resp = await self._client.post(
                f"/{endpoint}",
                json=body,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200 and resp.text:
                return resp.json()
            return None
        except (httpx.TimeoutException, httpx.ConnectError):
            return None
        except Exception as e:
            log.warning(f"[{self.device_id}] POST /{endpoint} error: {e}")
            return None
