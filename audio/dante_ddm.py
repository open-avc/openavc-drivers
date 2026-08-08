"""
OpenAVC Dante DDM/Director Driver.

Controls Dante audio routing via the Audinate Managed API (GraphQL).
Requires a Dante Domain Manager (on-premise) or Dante Director Professional
(cloud) instance — the driver connects to the management server, not to
individual Dante devices.

Capabilities:
  - Discover all Dante devices and their Tx/Rx channels; each Dante device
    is modeled as a child entity whose Rx channels carry their live
    subscription (routing) as child state.
  - Route audio: subscribe any Rx channel to a Tx channel (with device and
    channel pickers driven by the discovered topology).
  - Unroute audio: clear a subscription on an Rx channel.
  - "Refresh from Device" re-reads the whole domain and reconciles the child
    roster.

The Managed API is GraphQL over HTTPS. Authentication is via API key,
generated in the DDM/Director web UI.

Reference:
  - Audinate Managed API: https://www.getdante.com/products/network-management/dante-managed-api/
  - Bitfocus Companion DDM module (MIT, full GraphQL schema):
    https://github.com/bitfocus/companion-module-audinate-dante-ddm
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

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
query Domain($domainIDInput: ID!) {
  domain(id: $domainIDInput) {
    id
    name
    devices {
      id
      name
      txChannels {
        id
        index
        name
      }
      rxChannels {
        id
        index
        name
        subscribedDevice
        subscribedChannel
        status
        summary
      }
    }
  }
}
"""

_MUTATION_SUBSCRIBE = """
mutation DeviceRxChannelsSubscriptionSet($input: DeviceRxChannelsSubscriptionSetInput!) {
  DeviceRxChannelsSubscriptionSet(input: $input) {
    ok
  }
}
"""


def _safe_id(name: str) -> str:
    """Sanitize a Dante device name into a child local-id (the platform
    requires ``[A-Za-z0-9_-]``). The real name is kept in the child's ``name``
    state var and the driver's sid->name map."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "device"


def _rx_prop(index: int) -> str:
    """Child state-var key for an Rx channel by its 1-based index."""
    return f"rx{index}"


class DanteDDMDriver(BaseDriver):
    """Dante DDM/Director driver via the Audinate Managed API (GraphQL)."""

    DRIVER_INFO = {
        "id": "dante_ddm",
        "name": "Dante DDM / Director",
        "manufacturer": "Audinate",
        "category": "audio",
        "version": "1.7.2",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls Dante audio routing via the Audinate Managed API. "
            "Requires Dante Domain Manager or Dante Director Professional. "
            "Each Dante device is a child entity; route/unroute any Rx channel "
            "to a Tx channel and monitor live subscription status."
        ),
        "source_url": "https://www.getdante.com/products/network-management/dante-managed-api/",
        "tags": ["dante", "audio-routing", "graphql"],
        "verified": False,
        "simulated": True,
        "ports": [443],
        "discovery": {
            # DDM / Dante Director is a management *server*, not a
            # discovered endpoint — the user supplies the URL and API
            # token. The manufacturer aliases let peer Audinate drivers
            # (future Dante Connect / Dante AV entries) narrow when an
            # endpoint reports Audinate.
            "manufacturer_alias": ["audinate", "dante"],
        },
        "compatible_models": [
            {
                "manufacturer": "Audinate",
                "models": [
                    "Dante Domain Manager",
                    "Dante Director Professional",
                ],
                "confidence": "untested",
            },
        ],
        "transport": "http",
        "help": {
            "overview": (
                "This driver connects to a Dante Domain Manager (DDM) or "
                "Dante Director Professional instance to control audio routing "
                "across all Dante devices on the network. It does NOT connect "
                "to individual Dante devices — the DDM/Director acts as the "
                "central management point.\n\n"
                "Every Dante device in the domain appears as a child entity. "
                "Each device's Rx channels show what they're currently "
                "subscribed to, so you can see and drive the whole routing "
                "matrix from one device."
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
                "6. On connect, the devices and channels are discovered. Use "
                "the 'Refresh from Device' button (or the 'refresh' command) "
                "after adding gear to the domain."
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
            "ssl": {
                "type": "boolean",
                "default": True,
                "label": "Use HTTPS",
                "description": "Disable for on-premise DDM running plain HTTP.",
            },
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
            "tx_channel_names": {
                "type": "string",
                "label": "Tx Channel Names",
                "help": (
                    "JSON list of every Tx channel name across the domain. "
                    "Populates the Route command's Transmitter Channel picker."
                ),
            },
            "last_error": {
                "type": "string",
                "label": "Last Error",
            },
        },
        "child_entity_types": {
            "device": {
                "label": "Dante Device",
                "label_plural": "Dante Devices",
                "dynamic": True,
                "id_format": {"type": "string", "max_length": 128},
                # The per-child schema (published at register_child(schema=…))
                # adds one rx<index> prop per Rx channel; the type-level schema
                # carries the shared summary fields.
                "state_variables": {
                    "name": {"type": "string", "label": "Device Name", "cloud_priority": "low"},
                    "rx_channels": {"type": "integer", "label": "Rx Channels", "cloud_priority": "low"},
                    "tx_channels": {"type": "integer", "label": "Tx Channels", "cloud_priority": "low"},
                    "subscribed": {"type": "integer", "label": "Active Subscriptions", "cloud_priority": "low"},
                },
                "summary_fields": ["name", "subscribed", "rx_channels"],
                "label_field": "name",
            },
        },
        "quick_actions": ["refresh"],
        "actions": [
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
        ],
        "commands": {
            "route": {
                "label": "Route Audio",
                "params": {
                    "rx_device": {
                        "type": "child_id",
                        "child_type": "device",
                        "required": True,
                        "label": "Receiver Device",
                        "help": "The Dante device receiving audio.",
                    },
                    "rx_channel": {
                        "type": "string",
                        "required": True,
                        "label": "Receiver Channel",
                        "options_from": {"param": "rx_device", "source": "child_schema"},
                        "help": (
                            "Rx channel on the receiver. Pick the receiver "
                            "above to list its channels, or type a channel "
                            "name or index."
                        ),
                    },
                    "tx_device": {
                        "type": "child_id",
                        "child_type": "device",
                        "required": True,
                        "label": "Transmitter Device",
                        "help": "The Dante device sending audio.",
                    },
                    "tx_channel": {
                        "type": "string",
                        "required": True,
                        "label": "Transmitter Channel",
                        "options_state": "tx_channel_names",
                        "help": (
                            "Tx channel name on the transmitter. Every Tx "
                            "channel name in the domain is offered; type one "
                            "if it isn't listed."
                        ),
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
                        "type": "child_id",
                        "child_type": "device",
                        "required": True,
                        "label": "Receiver Device",
                        "help": "The Dante device to unroute.",
                    },
                    "rx_channel": {
                        "type": "string",
                        "required": True,
                        "label": "Receiver Channel",
                        "options_from": {"param": "rx_device", "source": "child_schema"},
                        "help": "Rx channel to clear (name, index, or picker).",
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
        self._domain_id: str = ""
        # Cached device data: {device_name: {id, name, txChannels, rxChannels}}
        self._devices: dict[str, dict[str, Any]] = {}
        # Child roster maps: child local-id (sanitized) <-> real device name,
        # and the last per-child schema (reconcile guard, qsc pattern).
        self._device_sid_to_name: dict[str, str] = {}
        self._device_schemas: dict[str, dict[str, Any]] = {}

    async def _pre_connect(self) -> None:
        # Validate config before any session is built — a missing field is a
        # configuration problem, not a connection failure.
        if not self.config.get("host", "").rstrip("/"):
            raise ConnectionError("DDM/Director URL is required")
        if not self.config.get("api_key", ""):
            raise ConnectionError("API key is required")
        if not self.config.get("domain_name", ""):
            raise ConnectionError("Domain name is required")

    async def _create_transport(self, transport_type: str) -> None:
        """Driver-owned session: GraphQL over HTTPS.

        No platform transport — the httpx client is the connection, so
        ``self.transport`` stays None and _link_alive()/_close_session()
        report and retire the client instead.
        """
        host = self.config.get("host", "").rstrip("/")
        port = self.config.get("port", 443)
        use_ssl = self.config.get("ssl", True)
        verify_ssl = self.config.get("verify_ssl", True)
        self._api_key = self.config.get("api_key", "")
        self._domain_name = self.config.get("domain_name", "")

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
                "Authorization": self._api_key,
                "Content-Type": "application/json",
            },
        )

    async def _post_connect(self) -> None:
        # Verify the DDM answers and resolve the domain name to its ID before
        # `connected` is declared. A raise aborts the attempt (the platform
        # retires the client via _close_session).
        host = self.config.get("host", "").rstrip("/")
        try:
            result = await self._graphql(_QUERY_DOMAINS)
            domains = result.get("data", {}).get("domains", [])

            # Find the domain by name and get its ID
            matched = None
            for d in domains:
                if d["name"] == self._domain_name:
                    matched = d
                    break

            if not matched:
                available = ", ".join(d["name"] for d in domains) if domains else "none"
                raise ConnectionError(
                    f"Domain '{self._domain_name}' not found. "
                    f"Available domains: {available}"
                )

            self._domain_id = matched["id"]

            log.info(
                f"[{self.device_id}] Connected to DDM/Director at {host}, "
                f"domain: {self._domain_name}"
            )
        except ConnectionError:
            raise
        except Exception as e:
            raise ConnectionError(
                f"Failed to connect to DDM/Director at {host}: {e}"
            )
        self.set_state("domain_name", self._domain_name)
        self.set_state("last_error", None)

    async def _initial_sync(self) -> None:
        # Initial device discovery seeds the child roster before the first
        # poll cycle runs.
        await self._refresh_devices()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()
        self._devices.clear()

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Execute a Dante routing command."""
        params = params or {}

        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match command:
            case "route":
                rx_device = self._device_name_for(params.get("rx_device", ""))
                rx_channel = params.get("rx_channel", "")
                tx_device = self._device_name_for(params.get("tx_device", ""))
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
                rx_device = self._device_name_for(params.get("rx_device", ""))
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

    async def refresh_children(self) -> dict[str, Any]:
        """Re-query the domain and reconcile the device child roster.

        Backs the IDE "Refresh from Device" button for this device.
        """
        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self._refresh_devices()
        return {"devices": len(self._devices)}

    async def poll(self) -> None:
        """Periodically refresh device and subscription status.

        Transport errors propagate so the BaseDriver watchdog can flip
        device.<id>.connected to False.
        """
        if not self._client:
            return

        try:
            await self._refresh_devices()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise ConnectionError(
                f"DDM at {self._base_url} not responding: {exc}"
            ) from exc

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
                # Worded for the shared connection-fault classifier -> auth_failed.
                self.set_state("last_error", "Authentication failed — check API key")
                raise ConnectionError("Authentication failed — check the API key")

            if resp.status_code == 403:
                self.set_state("last_error", "Access denied — check API key permissions")
                raise ConnectionError("Access denied — the API key lacks permission")

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
        """Query all devices and channels from the managed domain, then
        reconcile the device child roster and republish the picker lists."""
        try:
            result = await self._graphql(
                _QUERY_DEVICES, {"domainIDInput": self._domain_id}
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
                    if rx.get("subscribedDevice"):
                        subscription_count += 1

            self._reconcile_device_children(devices)

            self.set_state("device_count", len(self._devices))
            self.set_state("subscription_count", subscription_count)
            self.set_state("last_error", None)

            log.info(
                f"[{self.device_id}] Refreshed: {len(self._devices)} devices, "
                f"{subscription_count} active subscriptions"
            )

        except (httpx.TimeoutException, httpx.ConnectError):
            # Let transport errors propagate so poll()/connect() can react.
            raise
        except Exception:
            log.exception(f"[{self.device_id}] Refresh error")

    # --- Child entities (Dante devices) ---

    @staticmethod
    def _subscription_value(rx: dict[str, Any]) -> str:
        """Render one Rx channel's live subscription as a display string.

        "" when unrouted; "<TxDevice>: <TxChannel>" when subscribed; the raw
        status is appended when it isn't a clean connection (so a broken route
        is visible without inventing a status enum)."""
        dev = str(rx.get("subscribedDevice") or "")
        if not dev:
            return ""
        ch = str(rx.get("subscribedChannel") or "")
        val = f"{dev}: {ch}" if ch else dev
        status = str(rx.get("status") or "")
        if status and status.strip().lower() != "connected":
            val = f"{val} ({status})"
        return val

    def _build_device_child(
        self, dev: dict[str, Any]
    ) -> tuple[str, dict[str, dict[str, Any]], dict[str, Any], int]:
        """Return (sid, schema, initial_state, subscribed_count) for a device."""
        name = str(dev.get("name", ""))
        sid = _safe_id(name)
        rx_channels = dev.get("rxChannels", []) or []
        tx_channels = dev.get("txChannels", []) or []

        schema: dict[str, dict[str, Any]] = {
            "name": {"type": "string", "label": "Device Name", "cloud_priority": "low"},
            "rx_channels": {"type": "integer", "label": "Rx Channels", "cloud_priority": "low"},
            "tx_channels": {"type": "integer", "label": "Tx Channels", "cloud_priority": "low"},
            "subscribed": {"type": "integer", "label": "Active Subscriptions", "cloud_priority": "low"},
        }
        initial: dict[str, Any] = {
            "name": name,
            "rx_channels": len(rx_channels),
            "tx_channels": len(tx_channels),
        }

        subscribed = 0
        for rx in rx_channels:
            idx = rx.get("index")
            if idx is None:
                continue
            prop = _rx_prop(int(idx))
            label = str(rx.get("name") or f"Rx {idx}")
            # `control: true` opts this prop into the rx_channel picker cascade;
            # `high` cloud priority because a live route is the operational state.
            schema[prop] = {
                "type": "string",
                "label": label,
                "control": True,
                "cloud_priority": "high",
            }
            value = self._subscription_value(rx)
            initial[prop] = value
            if value:
                subscribed += 1

        initial["subscribed"] = subscribed
        return sid, schema, initial, subscribed

    def _reconcile_device_children(self, devices: list[dict[str, Any]]) -> None:
        """Register a child per Dante device (re-registering only when its
        channel schema changed), deregister devices no longer present, and
        publish the Tx-channel picker list."""
        seen: set[str] = set()
        tx_names: set[str] = set()

        for dev in devices:
            if not isinstance(dev, dict) or not dev.get("name"):
                continue
            sid, schema, initial, _ = self._build_device_child(dev)
            seen.add(sid)
            self._device_sid_to_name[sid] = str(dev.get("name", ""))
            for tx in dev.get("txChannels", []) or []:
                nm = tx.get("name")
                if nm:
                    tx_names.add(str(nm))

            prev = self._device_schemas.get(sid)
            if (
                prev is not None
                and prev == schema
                and self.is_child_registered("device", sid)
            ):
                # Same channel set — just refresh values, keep the child.
                try:
                    self.set_child_state_batch("device", sid, initial)
                except ValueError:
                    pass
            else:
                if self.is_child_registered("device", sid):
                    self.deregister_child("device", sid)
                self.register_child(
                    "device", sid, schema=schema, initial_state=initial
                )
            self._device_schemas[sid] = schema

        # Drop children for devices that left the domain.
        for sid in list(self._device_sid_to_name):
            if sid not in seen:
                self.deregister_child("device", sid)
                self._device_sid_to_name.pop(sid, None)
                self._device_schemas.pop(sid, None)

        self.set_state("tx_channel_names", json.dumps(sorted(tx_names)))

    def _device_name_for(self, value: Any) -> str:
        """Resolve a child local-id (from the device picker) back to the real
        Dante device name; fall back to treating the value as a literal name."""
        s = str(value)
        return self._device_sid_to_name.get(s, s)

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
        """Resolve a channel picker value / name / index to a numeric index."""
        s = str(channel).strip()
        # The rx_channel picker hands back the schema prop key, e.g. "rx3".
        m = re.match(r"^rx(\d+)$", s, re.IGNORECASE)
        if m:
            return int(m.group(1))
        # A bare index.
        try:
            return int(s)
        except ValueError:
            pass
        # Match by channel name (case-insensitive).
        lower = s.lower()
        for ch in device.get("rxChannels", []):
            if str(ch.get("name", "")).lower() == lower:
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
                "subscriptions": [
                    {
                        "rxChannelIndex": rx_idx,
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

            ok = result.get("data", {}).get(
                "DeviceRxChannelsSubscriptionSet", {}
            ).get("ok", False)
            if not ok:
                log.warning(f"[{self.device_id}] Route mutation returned ok=false")
                self.set_state("last_error", "Route mutation rejected")
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
                "subscriptions": [
                    {
                        "rxChannelIndex": rx_idx,
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

            ok = result.get("data", {}).get(
                "DeviceRxChannelsSubscriptionSet", {}
            ).get("ok", False)
            if not ok:
                log.warning(f"[{self.device_id}] Unroute mutation returned ok=false")
                self.set_state("last_error", "Unroute mutation rejected")
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
