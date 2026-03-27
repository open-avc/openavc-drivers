"""
OpenAVC BirdDog Encoder/Decoder Driver.

Controls BirdDog NDI encoders and decoders via the REST API (port 8080).

Supported models: Mini, Flex (Encode/Decode), 4K HDMI, 4K SDI, Studio NDI,
PLAY, Quad, and newer converters with the same API.

Encoders convert HDMI/SDI input into an NDI source on the network.
Decoders receive an NDI source and output it to HDMI/SDI.
Some models (Mini, Studio, 4K, Quad) can switch between encode and decode mode.

REST API (port 8080, HTTP, JSON, no authentication):
  GET  /about           — Device info (hostname, firmware, format)
  GET  /List            — Available NDI sources on the network (JSON object)
  GET  /connectTo       — Current decode source {"sourceName": "..."}
  POST /connectTo       — Select decode source  {"sourceName": "SOURCE_NAME"}
  GET  /operationmode   — Current mode (text: "Encode" or "Decode")
  GET  /refresh         — Trigger NDI source list refresh
  GET  /reboot          — Reboot device
  GET  /restart         — Restart video engine

WebSocket (port 6790):
  Real-time status updates. Used for monitoring; not required for control.

Reference:
  - BirdDog REST API: https://birddog.tv/AV/API/index.html
  - Bitfocus Companion module: github.com/bitfocus/companion-module-birddog-converters
"""

from __future__ import annotations

from typing import Any

import httpx

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)


class BirdDogCodecDriver(BaseDriver):
    """BirdDog NDI encoder/decoder driver via REST API."""

    DRIVER_INFO = {
        "id": "birddog_codec",
        "name": "BirdDog NDI Encoder/Decoder",
        "manufacturer": "BirdDog",
        "category": "video",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls BirdDog NDI encoders and decoders via REST API. "
            "Select NDI sources on decoders, monitor input/output status, "
            "reboot, and refresh source lists."
        ),
        "transport": "http",
        "help": {
            "overview": (
                "Controls BirdDog NDI encoders and decoders — Mini, Flex, "
                "4K HDMI, 4K SDI, Studio NDI, PLAY, Quad, and similar models.\n\n"
                "For decoders: select which NDI source to display, cycle "
                "through available sources, and monitor the active source.\n\n"
                "For encoders: monitor input status and NDI source name.\n\n"
                "No external software, SDK, or runtime required."
            ),
            "setup": (
                "1. Enter the device's IP address (find it via BirdDog Central "
                "or your network's DHCP table).\n"
                "2. Default port is 8080.\n"
                "3. No authentication is required.\n"
                "4. For decoders, use 'select_source' with the full NDI source "
                "name (e.g., 'BIRDDOG-P200 (Camera)').\n"
                "5. Use 'refresh_sources' to update the available source list."
            ),
        },
        "default_config": {
            "host": "",
            "port": 8080,
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 8080, "label": "REST API Port"},
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 0,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "hostname": {"type": "string", "label": "Hostname"},
            "firmware": {"type": "string", "label": "Firmware Version"},
            "model": {"type": "string", "label": "Model"},
            "operation_mode": {
                "type": "enum",
                "values": ["Encode", "Decode"],
                "label": "Operation Mode",
            },
            "decode_source": {
                "type": "string",
                "label": "Current NDI Source",
            },
            "source_count": {
                "type": "integer",
                "label": "Available NDI Sources",
            },
        },
        "commands": {
            "select_source": {
                "label": "Select NDI Source",
                "params": {
                    "source_name": {
                        "type": "string",
                        "required": True,
                        "label": "NDI Source Name",
                        "help": (
                            "Full NDI source name as it appears on the network "
                            "(e.g., 'BIRDDOG-P200 (Camera)'). Use 'refresh_sources' "
                            "to see available sources in the log."
                        ),
                    },
                },
                "help": "Switch the decoder to receive the specified NDI source.",
            },
            "next_source": {
                "label": "Next NDI Source",
                "params": {},
                "help": "Switch to the next available NDI source in the list.",
            },
            "previous_source": {
                "label": "Previous NDI Source",
                "params": {},
                "help": "Switch to the previous available NDI source in the list.",
            },
            "refresh_sources": {
                "label": "Refresh NDI Sources",
                "params": {},
                "help": "Trigger a refresh of the available NDI source list.",
            },
            "reboot": {
                "label": "Reboot Device",
                "params": {},
            },
            "restart_video": {
                "label": "Restart Video",
                "params": {},
                "help": "Restart the video engine without a full reboot.",
            },
        },
        "device_settings": {
            "ndi_name": {
                "type": "string",
                "label": "NDI Source Name",
                "help": (
                    "The name other devices use to subscribe to this NDI source. "
                    "Only applies when device is in Encode mode. Must be unique "
                    "across all NDI devices on the network."
                ),
                "state_key": "hostname",
                "default": "BIRDDOG",
                "setup": True,
                "unique": True,
            },
            "hostname": {
                "type": "string",
                "label": "Device Hostname",
                "help": (
                    "The network hostname of this encoder/decoder. Shown in "
                    "BirdDog Central and mDNS/DNS-SD discovery."
                ),
                "state_key": "hostname",
                "default": "BIRDDOG",
                "setup": True,
                "unique": True,
            },
            "operation_mode": {
                "type": "enum",
                "label": "Operation Mode",
                "help": (
                    "Switch between Encode (HDMI/SDI in, NDI out) and Decode "
                    "(NDI in, HDMI/SDI out) mode. Only supported on dual-mode "
                    "models (Mini, Studio, 4K, Quad)."
                ),
                "values": ["Encode", "Decode"],
                "state_key": "operation_mode",
                "default": "Encode",
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
        # Cached NDI source list: [source_name, ...]
        self._sources: list[str] = []

    async def connect(self) -> None:
        """Connect to the BirdDog encoder/decoder."""
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
            if not about:
                raise ConnectionError("No response from device")

            # Handle both current and legacy firmware
            hostname = about.get("HostName") or about.get("MyHostName", "")
            if not hostname:
                raise ConnectionError("Unexpected response from device")

            self.set_state("hostname", hostname)
            self.set_state("model", about.get("Format", ""))
            self.set_state("firmware", about.get("FirmwareVersion", ""))
            log.info(
                f"[{self.device_id}] Connected to BirdDog "
                f"{about.get('Format', '')} at {host}:{port} ({hostname})"
            )
        except ConnectionError:
            if self._client:
                await self._client.aclose()
                self._client = None
            raise
        except Exception as e:
            if self._client:
                await self._client.aclose()
                self._client = None
            raise ConnectionError(
                f"Failed to connect to BirdDog at {host}:{port}: {e}"
            )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")

        # Query operation mode, source list, and current source
        await self._refresh_state()

        # Start polling
        poll_interval = self.config.get("poll_interval", 5)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        await self.stop_polling()
        if self._client:
            await self._client.aclose()
            self._client = None
        self._sources.clear()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Execute a command on the encoder/decoder."""
        params = params or {}

        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match command:
            case "select_source":
                source_name = params.get("source_name", "")
                if not source_name:
                    log.warning(f"[{self.device_id}] select_source requires source_name")
                    return

                await self._api_post("connectTo", {"sourceName": source_name})
                self.set_state("decode_source", source_name)
                log.info(f"[{self.device_id}] Selected source: {source_name}")

            case "next_source":
                await self._cycle_source(1)

            case "previous_source":
                await self._cycle_source(-1)

            case "refresh_sources":
                await self._api_get("refresh")
                await self._update_source_list()
                log.info(
                    f"[{self.device_id}] Refreshed sources: "
                    f"{len(self._sources)} available"
                )

            case "reboot":
                await self._api_get("reboot")
                log.info(f"[{self.device_id}] Rebooting device")

            case "restart_video":
                await self._api_get("restart")
                log.info(f"[{self.device_id}] Restarting video engine")

            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting to the encoder/decoder via REST API."""
        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match key:
            case "ndi_name":
                await self._api_post("encodesetup", {"NDIName": str(value)})
                log.info(f"[{self.device_id}] Set NDI name to '{value}'")

            case "hostname":
                await self._api_post("about", {"HostName": str(value)})
                self.set_state("hostname", str(value))
                log.info(f"[{self.device_id}] Set hostname to '{value}'")

            case "operation_mode":
                # Note: not all models support mode switching
                await self._api_post("operationmode", {"mode": str(value)})
                self.set_state("operation_mode", str(value))
                log.info(f"[{self.device_id}] Set operation mode to '{value}'")

            case _:
                raise ValueError(f"Unknown device setting: {key}")

    async def poll(self) -> None:
        """Query device status."""
        if not self._client:
            return

        try:
            await self._refresh_state()
        except (httpx.ConnectError, httpx.TimeoutException):
            log.warning(f"[{self.device_id}] Poll failed — device not responding")
        except Exception:
            log.exception(f"[{self.device_id}] Poll error")

    # --- Internal helpers ---

    async def _refresh_state(self) -> None:
        """Query operation mode, current source, and source list."""
        # Operation mode
        mode_resp = await self._api_get_text("operationmode")
        if mode_resp:
            mode = mode_resp.strip().strip('"')
            self.set_state("operation_mode", mode)

        # Current decode source
        connect = await self._api_get("connectTo")
        if connect and "sourceName" in connect:
            old_source = self.get_state("decode_source")
            new_source = connect["sourceName"]
            self.set_state("decode_source", new_source)
            if new_source != old_source and new_source:
                log.info(f"[{self.device_id}] Current source: {new_source}")

        # Source list
        await self._update_source_list()

    async def _update_source_list(self) -> None:
        """Query and cache the available NDI source list."""
        sources = await self._api_get("List")
        if sources and isinstance(sources, dict):
            # The /List endpoint returns {"SourceName1": "addr", "SourceName2": "addr", ...}
            self._sources = list(sources.keys())
            self.set_state("source_count", len(self._sources))

    async def _cycle_source(self, direction: int) -> None:
        """Cycle to the next or previous NDI source."""
        if not self._sources:
            await self._update_source_list()

        if not self._sources:
            log.warning(f"[{self.device_id}] No NDI sources available")
            return

        current = self.get_state("decode_source") or ""
        try:
            idx = self._sources.index(current)
            new_idx = (idx + direction) % len(self._sources)
        except ValueError:
            new_idx = 0

        new_source = self._sources[new_idx]
        await self._api_post("connectTo", {"sourceName": new_source})
        self.set_state("decode_source", new_source)
        log.info(f"[{self.device_id}] Switched to source: {new_source}")

    async def _api_get(self, endpoint: str) -> dict | None:
        """Send a GET request, return parsed JSON."""
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

    async def _api_get_text(self, endpoint: str) -> str | None:
        """Send a GET request, return raw text (for endpoints that return plain text)."""
        if not self._client:
            return None
        try:
            resp = await self._client.get(f"/{endpoint}")
            if resp.status_code == 200:
                return resp.text
            return None
        except (httpx.TimeoutException, httpx.ConnectError):
            return None
        except Exception as e:
            log.warning(f"[{self.device_id}] GET /{endpoint} error: {e}")
            return None

    async def _api_post(self, endpoint: str, body: dict) -> dict | None:
        """Send a POST request with JSON body."""
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
