"""
OpenAVC Dante DDM/Director Driver.

Controls Dante audio routing via the Audinate Managed API (GraphQL).
Requires a Dante Domain Manager (on-premise) or Dante Director Professional
(cloud) instance — the driver connects to the management server, not to
individual Dante devices.

Capabilities:
  - Discover all Dante devices and their Tx/Rx channels
  - Route audio: subscribe any Rx channel to a Tx channel
  - Unroute audio: clear a subscription on an Rx channel
  - Query subscription status (active, failed, format mismatch, etc.)

The Managed API is GraphQL over HTTPS. Authentication is via API key,
generated in the DDM/Director web UI.

Reference:
  - Audinate Managed API: https://www.getdante.com/products/network-management/dante-managed-api/
  - Bitfocus Companion DDM module (MIT, full GraphQL schema):
    https://github.com/bitfocus/companion-module-audinate-dante-ddm
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# --- GraphQL queries and mutations ---

_QUERY_DOMAINS = """
query {
  domains {
    id
    name
  }
}
"""

_QUERY_DEVICES = """
query($domainName: String!) {
  domain(name: $domainName) {
    id
    name
    devices {
      id
      name
      manufacturer
      productModelId
      firmwareVersion
      txChannels {
        index
        name
      }
      rxChannels {
        index
        name
        subscribedDevice
        subscribedChannel
        status
      }
    }
  }
}
"""

_MUTATION_SUBSCRIBE = """
mutation($input: DeviceRxChannelsSubscriptionSetInput!) {
  DeviceRxChannelsSubscriptionSet(input: $input) {
    deviceId
    rxChannels {
      index
      subscribedDevice
      subscribedChannel
      status
    }
  }
}
"""

# Subscription status codes from the Dante Managed API
_STATUS_LABELS = {
    "SUBSCRIBED": "active",
    "RESOLVED": "active",
    "SUBSCRIBE_SELF": "active",
    "UNRESOLVED": "failed",
    "REJECTED_FORMAT": "format_mismatch",
    "REJECTED_BANDWIDTH": "bandwidth",
    "REJECTED_LATENCY": "latency",
    "REJECTED_CHANNEL_COUNT": "channel_limit",
    "NO_CONNECTION": "no_connection",
    "CHANNEL_FORMAT_CHANGED": "format_changed",
    "IDLE": "idle",
    "UNSUBSCRIBED": "unsubscribed",
}


class DanteDDMDriver(BaseDriver):
    """Dante DDM/Director driver via the Audinate Managed API (GraphQL)."""

    DRIVER_INFO = {
        "id": "dante_ddm",
        "name": "Dante DDM / Director",
        "manufacturer": "Audinate",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Dante audio routing via the Audinate Managed API. "
            "Requires Dante Domain Manager or Dante Director Professional. "
            "Discover devices, route/unroute audio channels, monitor subscriptions."
        ),
        "transport": "http",
        "help": {
            "overview": (
                "This driver connects to a Dante Domain Manager (DDM) or "
                "Dante Director Professional instance to control audio routing "
                "across all Dante devices on the network. It does NOT connect "
                "to individual Dante devices — the DDM/Director acts as the "
                "central management point.\n\n"
                "You can route any transmit (Tx) channel to any receive (Rx) "
                "channel, clear routes, and monitor subscription status."
            ),
            "setup": (
                "1. You need Dante Domain Manager (on-premise) or Dante "
                "Director Professional (cloud) — the Standard tier does not "
                "include API access.\n"
                "2. Generate an API key in the DDM/Director web UI.\n"
                "3. Enter the DDM/Director URL (e.g., https://ddm.local or "
                "the Director cloud URL).\n"
                "4. Enter the API key.\n"
                "5. Enter the Dante domain name to manage (shown in DDM/Director).\n"
                "6. Use the 'refresh' command to discover devices and channels."
            ),
        },
        "default_config": {
            "host": "",
            "port": 443,
            "ssl": True,
            "verify_ssl": True,
            "api_key": "",
            "domain_name": "",
            "poll_interval": 30,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "DDM/Director URL",
                "description": (
                    "Hostname or IP of the Dante Domain Manager, or the "
                    "Dante Director cloud URL."
                ),
            },
            "port": {"type": "integer", "default": 443, "label": "Port"},
            "api_key": {
                "type": "string",
                "required": True,
                "label": "API Key",
                "secret": True,
                "description": "Generated in the DDM/Director web UI.",
            },
            "domain_name": {
                "type": "string",
                "required": True,
                "label": "Domain Name",
                "description": "The Dante domain to manage (as shown in DDM/Director).",
            },
            "verify_ssl": {
                "type": "boolean",
                "default": True,
                "label": "Verify SSL",
                "description": "Disable for self-signed certificates on local DDM.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": "How often to refresh device and subscription status. 0 to disable.",
            },
        },
        "state_variables": {
            "device_count": {
                "type": "integer",
                "label": "Device Count",
            },
            "subscription_count": {
                "type": "integer",
                "label": "Active Subscriptions",
            },
            "domain_name": {
                "type": "string",
                "label": "Domain Name",
            },
            "last_error": {
                "type": "string",
                "label": "Last Error",
            },
        },
        "commands": {
            "route": {
                "label": "Route Audio",
                "params": {
                    "rx_device": {
                        "type": "string",
                        "required": True,
                        "label": "Receiver Device",
                        "help": "Name of the Dante device receiving audio.",
                    },
                    "rx_channel": {
                        "type": "string",
                        "required": True,
                        "label": "Receiver Channel",
                        "help": "Name or index of the Rx channel on the receiver.",
                    },
                    "tx_device": {
                        "type": "string",
                        "required": True,
                        "label": "Transmitter Device",
                        "help": "Name of the Dante device sending audio.",
                    },
                    "tx_channel": {
                        "type": "string",
                        "required": True,
                        "label": "Transmitter Channel",
                        "help": "Name or index of the Tx channel on the transmitter.",
                    },
                },
                "help": (
                    "Route a Dante Tx channel to an Rx channel. "
                    "Use device names and channel names as shown in Dante Controller."
                ),
            },
            "unroute": {
                "label": "Unroute Audio",
                "params": {
                    "rx_device": {
                        "type": "string",
                        "required": True,
                        "label": "Receiver Device",
                        "help": "Name of the Dante device to unroute.",
                    },
                    "rx_channel": {
                        "type": "string",
                        "required": True,
                        "label": "Receiver Channel",
                        "help": "Name or index of the Rx channel to clear.",
                    },
                },
                "help": "Clear the subscription on an Rx channel (stop receiving audio).",
            },
            "refresh": {
                "label": "Refresh Devices",
                "params": {},
                "help": "Re-query all Dante devices, channels, and subscription status from DDM/Director.",
            },
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._base_url: str = ""
        self._api_key: str = ""
        self._domain_name: str = ""
        # Cached device data: {device_name: {id, name, manufacturer, txChannels, rxChannels}}
        self._devices: dict[str, dict[str, Any]] = {}

    async def connect(self) -> None:
        """Connect to the DDM/Director GraphQL API."""
        host = self.config.get("host", "").rstrip("/")
        port = self.config.get("port", 443)
        use_ssl = self.config.get("ssl", True)
        verify_ssl = self.config.get("verify_ssl", True)
        self._api_key = self.config.get("api_key", "")
        self._domain_name = self.config.get("domain_name", "")

        if not host:
            raise ConnectionError("DDM/Director URL is required")
        if not self._api_key:
            raise ConnectionError("API key is required")
        if not self._domain_name:
            raise ConnectionError("Domain name is required")

        scheme = "https" if use_ssl else "http"
        # If the host already includes a scheme, use it as-is
        if host.startswith("http://") or host.startswith("https://"):
            self._base_url = host
        else:
            self._base_url = f"{scheme}://{host}:{port}"

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            verify=verify_ssl,
            timeout=15.0,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

        # Verify connection by querying domains
        try:
            result = await self._graphql(_QUERY_DOMAINS)
            domains = result.get("data", {}).get("domains", [])
            domain_names = [d["name"] for d in domains]

            if self._domain_name not in domain_names:
                available = ", ".join(domain_names) if domain_names else "none"
                raise ConnectionError(
                    f"Domain '{self._domain_name}' not found. "
                    f"Available domains: {available}"
                )

            log.info(
                f"[{self.device_id}] Connected to DDM/Director at {host}, "
                f"domain: {self._domain_name}"
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
                f"Failed to connect to DDM/Director at {host}: {e}"
            )

        self._connected = True
        self.set_state("connected", True)
        self.set_state("domain_name", self._domain_name)
        self.set_state("last_error", None)
        await self.events.emit(f"device.connected.{self.device_id}")

        # Initial device discovery
        await self._refresh_devices()

        # Start polling
        poll_interval = self.config.get("poll_interval", 30)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        """Disconnect from the DDM/Director."""
        await self.stop_polling()
        if self._client:
            await self._client.aclose()
            self._client = None
        self._devices.clear()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected from DDM/Director")

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Execute a Dante routing command."""
        params = params or {}

        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match command:
            case "route":
                rx_device = params.get("rx_device", "")
                rx_channel = params.get("rx_channel", "")
                tx_device = params.get("tx_device", "")
                tx_channel = params.get("tx_channel", "")

                if not all([rx_device, rx_channel, tx_device, tx_channel]):
                    log.warning(
                        f"[{self.device_id}] Route requires rx_device, "
                        f"rx_channel, tx_device, tx_channel"
                    )
                    return

                await self._set_subscription(
                    rx_device, rx_channel, tx_device, tx_channel
                )

            case "unroute":
                rx_device = params.get("rx_device", "")
                rx_channel = params.get("rx_channel", "")

                if not all([rx_device, rx_channel]):
                    log.warning(
                        f"[{self.device_id}] Unroute requires rx_device, rx_channel"
                    )
                    return

                await self._clear_subscription(rx_device, rx_channel)

            case "refresh":
                await self._refresh_devices()

            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def poll(self) -> None:
        """Periodically refresh device and subscription status."""
        if not self._client:
            return

        try:
            await self._refresh_devices()
        except Exception:
            log.exception(f"[{self.device_id}] Poll error")

    # --- Internal helpers ---

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        """Send a GraphQL request and return the parsed response."""
        if not self._client:
            raise ConnectionError("Not connected")

        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            resp = await self._client.post("/graphql", json=payload)

            if resp.status_code == 401:
                self.set_state("last_error", "Authentication failed — check API key")
                raise ConnectionError("Authentication failed — check API key")

            if resp.status_code == 403:
                self.set_state("last_error", "Access denied — check API permissions")
                raise ConnectionError("Access denied — check API permissions")

            resp.raise_for_status()
            result = resp.json()

            # Check for GraphQL-level errors
            if "errors" in result:
                error_msg = result["errors"][0].get("message", "Unknown GraphQL error")
                log.warning(f"[{self.device_id}] GraphQL error: {error_msg}")
                self.set_state("last_error", error_msg)

            return result

        except httpx.TimeoutException:
            log.warning(f"[{self.device_id}] GraphQL request timeout")
            self.set_state("last_error", "Request timeout")
            raise
        except httpx.ConnectError:
            log.warning(f"[{self.device_id}] GraphQL connection error")
            self.set_state("last_error", "Connection failed")
            raise

    async def _refresh_devices(self) -> None:
        """Query all devices and channels from the managed domain."""
        try:
            result = await self._graphql(
                _QUERY_DEVICES, {"domainName": self._domain_name}
            )

            domain = result.get("data", {}).get("domain")
            if not domain:
                log.warning(
                    f"[{self.device_id}] Domain '{self._domain_name}' "
                    f"returned no data"
                )
                return

            devices = domain.get("devices", [])
            self._devices.clear()

            subscription_count = 0
            for dev in devices:
                name = dev.get("name", "")
                self._devices[name] = dev

                # Count active subscriptions
                for rx in dev.get("rxChannels", []):
                    status = rx.get("status", "")
                    if status in ("SUBSCRIBED", "RESOLVED", "SUBSCRIBE_SELF"):
                        subscription_count += 1

            self.set_state("device_count", len(self._devices))
            self.set_state("subscription_count", subscription_count)
            self.set_state("last_error", None)

            log.info(
                f"[{self.device_id}] Refreshed: {len(self._devices)} devices, "
                f"{subscription_count} active subscriptions"
            )

        except (httpx.TimeoutException, httpx.ConnectError):
            log.warning(f"[{self.device_id}] Refresh failed — connection issue")
        except Exception:
            log.exception(f"[{self.device_id}] Refresh error")

    def _find_device(self, device_name: str) -> dict | None:
        """Look up a device by name (case-insensitive)."""
        # Exact match first
        if device_name in self._devices:
            return self._devices[device_name]
        # Case-insensitive fallback
        lower = device_name.lower()
        for name, dev in self._devices.items():
            if name.lower() == lower:
                return dev
        return None

    def _find_rx_channel_index(self, device: dict, channel: str) -> int | None:
        """Resolve a channel name or index string to a numeric index."""
        # Try as integer index
        try:
            return int(channel)
        except ValueError:
            pass

        # Match by name (case-insensitive)
        lower = channel.lower()
        for ch in device.get("rxChannels", []):
            if ch.get("name", "").lower() == lower:
                return ch["index"]
        return None

    async def _set_subscription(
        self,
        rx_device_name: str,
        rx_channel: str,
        tx_device_name: str,
        tx_channel: str,
    ) -> None:
        """Route a Tx channel to an Rx channel."""
        rx_dev = self._find_device(rx_device_name)
        if not rx_dev:
            log.warning(
                f"[{self.device_id}] Receiver device '{rx_device_name}' not found. "
                f"Run 'refresh' to update device list."
            )
            self.set_state("last_error", f"Device not found: {rx_device_name}")
            return

        rx_idx = self._find_rx_channel_index(rx_dev, rx_channel)
        if rx_idx is None:
            rx_names = [
                ch.get("name", str(ch["index"]))
                for ch in rx_dev.get("rxChannels", [])
            ]
            log.warning(
                f"[{self.device_id}] Rx channel '{rx_channel}' not found on "
                f"'{rx_device_name}'. Available: {rx_names}"
            )
            self.set_state("last_error", f"Rx channel not found: {rx_channel}")
            return

        variables = {
            "input": {
                "deviceId": rx_dev["id"],
                "rxChannels": [
                    {
                        "index": rx_idx,
                        "subscribedDevice": tx_device_name,
                        "subscribedChannel": tx_channel,
                    }
                ],
            }
        }

        try:
            result = await self._graphql(_MUTATION_SUBSCRIBE, variables)

            if "errors" in result:
                error_msg = result["errors"][0].get("message", "Unknown error")
                log.warning(
                    f"[{self.device_id}] Route failed: {error_msg}"
                )
                self.set_state("last_error", f"Route failed: {error_msg}")
                return

            log.info(
                f"[{self.device_id}] Routed: {tx_device_name}/{tx_channel} -> "
                f"{rx_device_name}/{rx_channel}"
            )
            self.set_state("last_error", None)

            # Refresh to get updated subscription status
            await self._refresh_devices()

        except Exception as e:
            log.warning(f"[{self.device_id}] Route error: {e}")
            self.set_state("last_error", f"Route error: {e}")

    async def _clear_subscription(
        self, rx_device_name: str, rx_channel: str
    ) -> None:
        """Clear (unsubscribe) an Rx channel."""
        rx_dev = self._find_device(rx_device_name)
        if not rx_dev:
            log.warning(
                f"[{self.device_id}] Receiver device '{rx_device_name}' not found. "
                f"Run 'refresh' to update device list."
            )
            self.set_state("last_error", f"Device not found: {rx_device_name}")
            return

        rx_idx = self._find_rx_channel_index(rx_dev, rx_channel)
        if rx_idx is None:
            log.warning(
                f"[{self.device_id}] Rx channel '{rx_channel}' not found on "
                f"'{rx_device_name}'."
            )
            self.set_state("last_error", f"Rx channel not found: {rx_channel}")
            return

        # Clear subscription by setting empty device/channel
        variables = {
            "input": {
                "deviceId": rx_dev["id"],
                "rxChannels": [
                    {
                        "index": rx_idx,
                        "subscribedDevice": "",
                        "subscribedChannel": "",
                    }
                ],
            }
        }

        try:
            result = await self._graphql(_MUTATION_SUBSCRIBE, variables)

            if "errors" in result:
                error_msg = result["errors"][0].get("message", "Unknown error")
                log.warning(
                    f"[{self.device_id}] Unroute failed: {error_msg}"
                )
                self.set_state("last_error", f"Unroute failed: {error_msg}")
                return

            log.info(
                f"[{self.device_id}] Unrouted: {rx_device_name}/{rx_channel}"
            )
            self.set_state("last_error", None)

            # Refresh to get updated status
            await self._refresh_devices()

        except Exception as e:
            log.warning(f"[{self.device_id}] Unroute error: {e}")
            self.set_state("last_error", f"Unroute error: {e}")
