"""
OpenAVC Crestron DM NVX Driver.

Controls Crestron DM NVX AV-over-IP encoders/decoders via the REST API (HTTPS).
Covers DM-NVX-35x, DM-NVX-36x, DM-NVX-E30, DM-NVX-D30, and similar models.

API reference: https://sdkcon78221.crestron.com/sdk/DM_NVX_REST_API/
Authentication: Cookie-based with XSRF token (or disabled on many AV VLANs).
Protocol: HTTPS REST (JSON), default port 443.

The NVX API uses a "CresNext" JSON format where all objects are nested under
a "Device" root key:
    GET  /Device/DeviceSpecific        -> device mode, status, video/audio source
    POST /Device/DeviceSpecific        -> set video source, audio source, etc.
    GET  /Device/AudioVideoInputOutput -> AV I/O status (resolution, sync detect)
    GET  /Device/StreamReceive         -> stream receive config (multicast address)
    POST /Device/StreamReceive         -> route a stream to this decoder
    GET  /Device/StreamTransmit        -> stream transmit config
    POST /Device/StreamTransmit        -> configure encoder stream settings
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)


class CrestronNVXDriver(BaseDriver):
    """Crestron DM NVX AV-over-IP encoder/decoder driver."""

    DRIVER_INFO = {
        "id": "crestron_nvx",
        "name": "Crestron DM NVX",
        "manufacturer": "Crestron",
        "category": "display",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Crestron DM NVX AV-over-IP encoders and decoders via "
            "the REST API. Supports device status, video/audio source selection, "
            "stream routing, and AV I/O monitoring."
        ),
        "transport": "http",
        "default_config": {
            "host": "",
            "port": 443,
            "ssl": True,
            "verify_ssl": False,
            "username": "admin",
            "password": "",
            "auth_enabled": True,
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 443, "label": "Port"},
            "username": {
                "type": "string",
                "default": "admin",
                "label": "Username",
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "Password",
                "secret": True,
            },
            "auth_enabled": {
                "type": "boolean",
                "default": True,
                "label": "Authentication Enabled",
                "description": "Disable if the NVX has auth turned off (common on isolated AV VLANs)",
            },
            "poll_interval": {
                "type": "integer",
                "default": 10,
                "min": 0,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "device_mode": {
                "type": "enum",
                "values": ["Transmitter", "Receiver"],
                "label": "Device Mode",
            },
            "device_ready": {"type": "boolean", "label": "Device Ready"},
            "video_source": {"type": "string", "label": "Video Source"},
            "audio_source": {"type": "string", "label": "Audio Source"},
            "active_video_source": {"type": "string", "label": "Active Video Source"},
            "active_audio_source": {"type": "string", "label": "Active Audio Source"},
            "stream_multicast": {"type": "string", "label": "Stream Multicast Address"},
            "horizontal_resolution": {"type": "integer", "label": "Horizontal Resolution"},
            "vertical_resolution": {"type": "integer", "label": "Vertical Resolution"},
            "sync_detected": {"type": "boolean", "label": "Sync Detected"},
            "firmware": {"type": "string", "label": "Firmware Version"},
        },
        "device_settings": {
            "device_name": {
                "type": "string",
                "label": "Device Name",
                "help": (
                    "The friendly name shown in the NVX web UI and Crestron "
                    "Toolbox. Helps identify this endpoint on the network."
                ),
                "state_key": "firmware",
                "default": "DM-NVX",
                "setup": True,
                "unique": True,
            },
            "led_enable": {
                "type": "boolean",
                "label": "Front Panel LEDs",
                "help": (
                    "Enable or disable the front panel LED indicators. "
                    "Disable for a cleaner look in visible installations."
                ),
                "state_key": "device_ready",
                "default": True,
                "setup": False,
            },
        },
        "commands": {
            "set_video_source": {
                "label": "Set Video Source",
                "params": {
                    "source": {
                        "type": "enum",
                        "values": ["None", "Input1", "Input2", "Stream"],
                        "required": True,
                        "label": "Video Source",
                    },
                },
            },
            "set_audio_source": {
                "label": "Set Audio Source",
                "params": {
                    "source": {
                        "type": "enum",
                        "values": [
                            "Automatic", "Input1", "Input2", "Analog",
                            "PrimaryAudio", "SecondaryAudio",
                        ],
                        "required": True,
                        "label": "Audio Source",
                    },
                },
            },
            "route_stream": {
                "label": "Route Stream (Decoder)",
                "params": {
                    "multicast_address": {
                        "type": "string",
                        "required": True,
                        "label": "Multicast Address",
                        "description": "Multicast address of the encoder stream (e.g., 239.x.x.x)",
                    },
                },
            },
            "set_stream_url": {
                "label": "Set Stream URL (Decoder)",
                "params": {
                    "url": {
                        "type": "string",
                        "required": True,
                        "label": "Stream URL",
                        "description": "Full stream URL to receive",
                    },
                },
            },
            "enable_leds": {
                "label": "Enable Front Panel LEDs",
                "params": {},
            },
            "disable_leds": {
                "label": "Disable Front Panel LEDs",
                "params": {},
            },
            "reboot": {
                "label": "Reboot Device",
                "params": {},
            },
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._xsrf_token: str = ""
        self._base_url: str = ""

    async def connect(self) -> None:
        """Connect to the NVX device via HTTPS."""
        host = self.config.get("host", "")
        port = self.config.get("port", 443)
        use_ssl = self.config.get("ssl", True)
        verify_ssl = self.config.get("verify_ssl", False)

        scheme = "https" if use_ssl else "http"
        self._base_url = f"{scheme}://{host}:{port}"

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            verify=verify_ssl,
            timeout=10.0,
        )

        # Authenticate if enabled
        if self.config.get("auth_enabled", True):
            try:
                await self._authenticate()
            except Exception as e:
                log.warning(f"[{self.device_id}] Auth failed: {e}")
                await self._client.aclose()
                self._client = None
                raise ConnectionError(f"Authentication failed: {e}")

        # Verify connection by fetching device info
        try:
            resp = await self._api_get("/Device/DeviceSpecific")
            if resp and "Device" in resp:
                self._connected = True
                self.set_state("connected", True)
                await self.events.emit(f"device.connected.{self.device_id}")
                log.info(f"[{self.device_id}] Connected to NVX at {host}:{port}")

                # Parse initial state
                self._parse_device_specific(resp)

                # Start polling
                poll_interval = self.config.get("poll_interval", 10)
                if poll_interval > 0:
                    await self.start_polling(poll_interval)
            else:
                raise ConnectionError("Unexpected response from device")
        except httpx.RequestError as e:
            if self._client:
                await self._client.aclose()
                self._client = None
            raise ConnectionError(f"Failed to connect: {e}")

    async def disconnect(self) -> None:
        """Disconnect from the NVX device."""
        await self.stop_polling()

        # Logout if authenticated
        if self._client and self.config.get("auth_enabled", True):
            try:
                await self._client.get("/logout")
            except Exception:
                pass

        if self._client:
            await self._client.aclose()
            self._client = None

        self._connected = False
        self._xsrf_token = ""
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a named command to the NVX device."""
        params = params or {}

        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match command:
            case "set_video_source":
                source = params.get("source", "Input1")
                await self._api_post("/Device/DeviceSpecific", {
                    "Device": {
                        "DeviceSpecific": {
                            "VideoSource": source,
                        }
                    }
                })

            case "set_audio_source":
                source = params.get("source", "Automatic")
                await self._api_post("/Device/DeviceSpecific", {
                    "Device": {
                        "DeviceSpecific": {
                            "AudioSource": source,
                        }
                    }
                })

            case "route_stream":
                multicast = params.get("multicast_address", "")
                await self._api_post("/Device/StreamReceive", {
                    "Device": {
                        "StreamReceive": {
                            "MulticastAddress": multicast,
                        }
                    }
                })

            case "set_stream_url":
                url = params.get("url", "")
                await self._api_post("/Device/StreamReceive", {
                    "Device": {
                        "StreamReceive": {
                            "StreamUrl": url,
                        }
                    }
                })

            case "enable_leds":
                await self._api_post("/Device/DeviceSpecific", {
                    "Device": {
                        "DeviceSpecific": {
                            "LedsEnabled": True,
                        }
                    }
                })

            case "disable_leds":
                await self._api_post("/Device/DeviceSpecific", {
                    "Device": {
                        "DeviceSpecific": {
                            "LedsEnabled": False,
                        }
                    }
                })

            case "reboot":
                await self._api_post("/Device/DeviceOperations", {
                    "Device": {
                        "DeviceOperations": {
                            "Reboot": True,
                        }
                    }
                })

            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

        log.debug(f"[{self.device_id}] Sent command: {command} {params}")

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting to the NVX via REST API."""
        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match key:
            case "device_name":
                await self._api_post("/Device/DeviceSpecific", {
                    "Device": {
                        "DeviceSpecific": {
                            "DeviceName": str(value),
                        }
                    }
                })
                log.info(f"[{self.device_id}] Set device name to '{value}'")

            case "led_enable":
                enabled = value if isinstance(value, bool) else str(value).lower() == "true"
                await self._api_post("/Device/DeviceSpecific", {
                    "Device": {
                        "DeviceSpecific": {
                            "LedsEnabled": enabled,
                        }
                    }
                })
                log.info(f"[{self.device_id}] Set LEDs {'enabled' if enabled else 'disabled'}")

            case _:
                raise ValueError(f"Unknown device setting: {key}")

    async def poll(self) -> None:
        """Query device status."""
        if not self._client:
            return

        try:
            # Get device-specific info (mode, sources, firmware)
            resp = await self._api_get("/Device/DeviceSpecific")
            if resp:
                self._parse_device_specific(resp)

            # Small delay between requests
            await asyncio.sleep(0.2)

            # Get AV I/O info (resolution, sync)
            resp = await self._api_get("/Device/AudioVideoInputOutput")
            if resp:
                self._parse_av_io(resp)

            await asyncio.sleep(0.2)

            # Get stream receive info (multicast address)
            resp = await self._api_get("/Device/StreamReceive")
            if resp:
                self._parse_stream_receive(resp)

        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")
        except Exception:
            log.exception(f"[{self.device_id}] Poll error")

    # --- Authentication ---

    async def _authenticate(self) -> None:
        """
        Perform the Crestron NVX cookie-based authentication flow.

        1. GET /userlogin.html → get TRACKID cookie
        2. POST /userlogin.html with credentials → get auth cookies + XSRF token
        """
        username = self.config.get("username", "admin")
        password = self.config.get("password", "")

        # Step 1: Get the login page and TRACKID cookie
        resp = await self._client.get("/userlogin.html")

        # Step 2: POST credentials
        resp = await self._client.post(
            "/userlogin.html",
            data={"login": username, "passwd": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if resp.status_code not in (200, 302):
            raise ConnectionError(f"Login failed with status {resp.status_code}")

        # Extract XSRF token from response headers
        self._xsrf_token = resp.headers.get("X-CREST-XSRF-TOKEN", "")

        log.info(f"[{self.device_id}] Authenticated successfully")

    # --- API Helpers ---

    async def _api_get(self, path: str) -> dict | None:
        """Send a GET request and return parsed JSON."""
        try:
            headers = {"Referer": self._base_url}
            log.info(f"[{self.device_id}] GET {path}")
            resp = await self._client.get(path, headers=headers)
            log.info(f"[{self.device_id}] GET {path} -> {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            log.warning(f"[{self.device_id}] GET {path} failed: HTTP {e.response.status_code}")
            # Re-authenticate on 401
            if e.response.status_code == 401 and self.config.get("auth_enabled", True):
                try:
                    await self._authenticate()
                    resp = await self._client.get(path, headers=headers)
                    resp.raise_for_status()
                    return resp.json()
                except Exception:
                    log.warning(f"[{self.device_id}] Re-auth failed")
            return None
        except Exception as e:
            log.warning(f"[{self.device_id}] GET {path} error: {e}")
            return None

    async def _api_post(self, path: str, body: dict) -> dict | None:
        """Send a POST request with JSON body."""
        try:
            headers = {
                "Referer": self._base_url,
                "Content-Type": "application/json",
            }
            if self._xsrf_token:
                headers["X-CREST-XSRF-TOKEN"] = self._xsrf_token

            log.info(f"[{self.device_id}] POST {path}")
            resp = await self._client.post(path, json=body, headers=headers)
            log.info(f"[{self.device_id}] POST {path} -> {resp.status_code}")
            resp.raise_for_status()
            return resp.json() if resp.text else None
        except httpx.HTTPStatusError as e:
            log.warning(f"[{self.device_id}] POST {path} failed: HTTP {e.response.status_code}")
            # Re-authenticate on 401
            if e.response.status_code == 401 and self.config.get("auth_enabled", True):
                try:
                    await self._authenticate()
                    if self._xsrf_token:
                        headers["X-CREST-XSRF-TOKEN"] = self._xsrf_token
                    resp = await self._client.post(path, json=body, headers=headers)
                    resp.raise_for_status()
                    return resp.json() if resp.text else None
                except Exception:
                    log.warning(f"[{self.device_id}] Re-auth + POST failed")
            return None
        except Exception as e:
            log.warning(f"[{self.device_id}] POST {path} error: {e}")
            return None

    # --- Response Parsing ---

    def _parse_device_specific(self, data: dict) -> None:
        """Parse /Device/DeviceSpecific response into state."""
        ds = data.get("Device", {}).get("DeviceSpecific", {})
        if not ds:
            return

        if "DeviceMode" in ds:
            self.set_state("device_mode", ds["DeviceMode"])
        if "DeviceReady" in ds:
            self.set_state("device_ready", ds["DeviceReady"])
        if "VideoSource" in ds:
            self.set_state("video_source", ds["VideoSource"])
        if "AudioSource" in ds:
            self.set_state("audio_source", ds["AudioSource"])
        if "ActiveVideoSource" in ds:
            self.set_state("active_video_source", ds["ActiveVideoSource"])
        if "ActiveAudioSource" in ds:
            self.set_state("active_audio_source", ds["ActiveAudioSource"])
        if "Version" in ds:
            self.set_state("firmware", ds["Version"])

    def _parse_av_io(self, data: dict) -> None:
        """Parse /Device/AudioVideoInputOutput response into state."""
        avio = data.get("Device", {}).get("AudioVideoInputOutput", {})
        if not avio:
            return

        # Input resolution and sync
        inputs = avio.get("Inputs", [])
        if inputs and len(inputs) > 0:
            inp = inputs[0]  # Primary input
            if "HorizontalResolution" in inp:
                self.set_state("horizontal_resolution", inp["HorizontalResolution"])
            if "VerticalResolution" in inp:
                self.set_state("vertical_resolution", inp["VerticalResolution"])
            if "SyncDetected" in inp:
                self.set_state("sync_detected", inp["SyncDetected"])

    def _parse_stream_receive(self, data: dict) -> None:
        """Parse /Device/StreamReceive response into state."""
        sr = data.get("Device", {}).get("StreamReceive", {})
        if not sr:
            return

        if "MulticastAddress" in sr:
            self.set_state("stream_multicast", sr["MulticastAddress"])
