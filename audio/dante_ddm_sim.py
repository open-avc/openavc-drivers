"""
Dante DDM / Director — Simulator

Simulates a Dante Domain Manager GraphQL API on port 443. Returns fake
Dante devices with Tx/Rx channels and handles routing mutations.

Driver: dante_ddm
Transport: http
"""

import json

from simulator.http_simulator import HTTPSimulator

# Domain and device IDs
_DOMAIN_ID = "d-001"
_DOMAIN_NAME = "OpenAVC Domain"

# Pre-built device data
_DEVICES = [
    {
        "id": "dev-001",
        "name": "Tesira-1",
        "txChannels": [
            {"id": f"tx-001-{i}", "index": i, "name": f"Output {i}"}
            for i in range(1, 9)
        ],
        "rxChannels": [
            {
                "id": f"rx-001-{i}",
                "index": i,
                "name": f"Input {i}",
                "subscribedDevice": "MXA920-1" if i <= 2 else "",
                "subscribedChannel": f"Channel {i}" if i <= 2 else "",
                "status": "Connected" if i <= 2 else "Unresolved",
                "summary": "OK" if i <= 2 else "",
            }
            for i in range(1, 9)
        ],
    },
    {
        "id": "dev-002",
        "name": "MXA920-1",
        "txChannels": [
            {"id": f"tx-002-{i}", "index": i, "name": f"Channel {i}"}
            for i in range(1, 5)
        ],
        "rxChannels": [],
    },
    {
        "id": "dev-003",
        "name": "AMP-1",
        "txChannels": [],
        "rxChannels": [
            {
                "id": f"rx-003-{i}",
                "index": i,
                "name": f"Input {i}",
                "subscribedDevice": "",
                "subscribedChannel": "",
                "status": "Unresolved",
                "summary": "",
            }
            for i in range(1, 5)
        ],
    },
]


def _count_subscriptions(devices: list[dict]) -> int:
    """Count active subscriptions across all devices."""
    count = 0
    for dev in devices:
        for rx in dev.get("rxChannels", []):
            if rx.get("subscribedDevice"):
                count += 1
    return count


class DanteDdmSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "dante_ddm",
        "name": "Dante DDM / Director Simulator",
        "category": "audio",
        "transport": "http",
        "default_port": 443,
        "initial_state": {
            "device_count": 3,
            "subscription_count": 2,
            "domain_name": _DOMAIN_NAME,
            "last_error": "",
        },
        "delays": {
            "command_response": 0.05,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "DDM stops responding to requests",
                "behavior": "no_response",
            },
            "auth_failure": {
                "description": "API key rejected (401 Unauthorized)",
            },
        },
        "controls": [
            {
                "type": "indicator",
                "key": "device_count",
                "label": "Devices",
            },
            {
                "type": "indicator",
                "key": "subscription_count",
                "label": "Active Subscriptions",
            },
            {
                "type": "indicator",
                "key": "domain_name",
                "label": "Domain",
            },
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Deep copy of device data so mutations are isolated per instance
        self._devices = json.loads(json.dumps(_DEVICES))

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        # Auth failure error mode returns 401 for all requests
        if "auth_failure" in self.active_errors:
            return 401, {"error": "Unauthorized"}

        if path != "/graphql" or method != "POST":
            return 404, {"error": "Not Found"}

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return 400, {"error": "Invalid JSON"}

        query = payload.get("query", "")
        variables = payload.get("variables", {})

        # Determine which query/mutation this is
        if "domains" in query and "domain(" not in query:
            return self._handle_list_domains()

        if "domain(" in query and "devices" in query:
            return self._handle_get_domain_devices(variables)

        if "DeviceRxChannelsSubscriptionSet" in query:
            return self._handle_subscription_set(variables)

        return 400, {"errors": [{"message": "Unknown query"}]}

    def _handle_list_domains(self) -> tuple[int, dict]:
        domain_name = self.get_state("domain_name", _DOMAIN_NAME)
        return 200, {
            "data": {
                "domains": [
                    {"id": _DOMAIN_ID, "name": domain_name},
                ]
            }
        }

    def _handle_get_domain_devices(self, variables: dict) -> tuple[int, dict]:
        domain_id = variables.get("domainIDInput", "")
        if domain_id != _DOMAIN_ID:
            return 200, {
                "errors": [{"message": f"Domain '{domain_id}' not found"}]
            }

        domain_name = self.get_state("domain_name", _DOMAIN_NAME)
        return 200, {
            "data": {
                "domain": {
                    "id": _DOMAIN_ID,
                    "name": domain_name,
                    "devices": self._devices,
                }
            }
        }

    def _handle_subscription_set(self, variables: dict) -> tuple[int, dict]:
        inp = variables.get("input", {})
        device_id = inp.get("deviceId", "")
        subscriptions = inp.get("subscriptions", [])

        # Find the target device
        target_dev = None
        for dev in self._devices:
            if dev["id"] == device_id:
                target_dev = dev
                break

        if not target_dev:
            return 200, {
                "errors": [{"message": f"Device '{device_id}' not found"}]
            }

        # Apply subscription changes
        for sub in subscriptions:
            rx_index = sub.get("rxChannelIndex")
            tx_device = sub.get("subscribedDevice", "")
            tx_channel = sub.get("subscribedChannel", "")

            for rx in target_dev.get("rxChannels", []):
                if rx["index"] == rx_index:
                    rx["subscribedDevice"] = tx_device
                    rx["subscribedChannel"] = tx_channel
                    if tx_device:
                        rx["status"] = "Connected"
                        rx["summary"] = "OK"
                    else:
                        rx["status"] = "Unresolved"
                        rx["summary"] = ""
                    break

        # Update state counts
        sub_count = _count_subscriptions(self._devices)
        self.set_state("subscription_count", sub_count)

        return 200, {
            "data": {
                "DeviceRxChannelsSubscriptionSet": {
                    "ok": True,
                }
            }
        }
