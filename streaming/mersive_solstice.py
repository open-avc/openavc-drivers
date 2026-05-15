"""
OpenAVC Mersive Solstice Driver.

Controls Mersive Solstice Pods (and Solstice Windows Software hosts) over the
public OpenControl REST API. Solstice is a wireless presentation / BYOD
collaboration endpoint — clients screen-share to the Pod and the Pod composes
their content onto a connected display.

Three URL families are used:
  GET  /api/stats                — runtime snapshot (post count, connected users,
                                   bandwidth, display name, version, hardware)
  GET  /api/config               — hierarchical configuration (used here only
                                   for the runtime-relevant subset: screen key,
                                   bulletin/emergency banners, power management)
  POST /api/config               — write configuration values
  GET  /api/control/<verb>       — runtime commands (clear / boot / reboot /
                                   restart / resetkey / suspend / wake)
  GET  /api/serial-passthru/send — RS-232 pass-thru to a USB-to-serial adapter
                                   on the Pod's USB port (chained display
                                   control)

Auth: optional. When the Pod has an admin password set it must be supplied on
every call — query string ``?password=...`` for GET, JSON field ``password``
for POST. Unset passwords mean any client on the network can drive the Pod.

Push vs poll: poll-only. The OpenControl protocol exposes no subscriptions,
events, or unsolicited messages. Mersive recommends polling no more than once
every 10 seconds; the default poll interval here is 30 s and the minimum the
config UI accepts is 10 s.

Install-time configuration (network/wifi/AP settings, EAP credentials, NTP,
splash-screen images, calendar Exchange auth, RSS feeds, VLANs, proxy
servers) is intentionally not exposed. Those belong in the Solstice Dashboard
and rarely change at runtime in commercial AV deployments.

Source: https://documentation.mersive.com/content/pdf/opencontrolapiguide.pdf
        (OpenControl API 1.0, May 2023)
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import httpx

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)


# OpenControl licenseStatus codes → human-readable strings
_LICENSE_STATUS = {0: "No license", 1: "Error", 2: "OK", 3: "Expired"}


class SolsticeDriver(BaseDriver):
    """Mersive Solstice Pod driver via OpenControl REST API."""

    DRIVER_INFO = {
        "id": "mersive_solstice",
        "name": "Mersive Solstice Pod",
        "manufacturer": "Mersive",
        "category": "streaming",
        "version": "1.3.0",
        "author": "OpenAVC",
        "description": (
            "Controls Mersive Solstice Pods (and Solstice Windows Software) "
            "over the OpenControl REST API. Boot users, clear posts, suspend "
            "and wake the display, reset the on-screen key, push bulletin or "
            "emergency banners, and monitor connected users and post counts."
        ),
        "source_url": "https://documentation.mersive.com/content/pdf/opencontrolapiguide.pdf",
        "tags": [
            "wireless-presentation",
            "byod",
            "byom",
            "collaboration",
            "huddle-room",
        ],
        "verified": False,
        "simulated": True,
        "transport": "http",
        "ports": [80, 443],
        "compatible_models": [
            {
                "manufacturer": "Mersive",
                "models": [
                    "Solstice Pod Gen3",
                    "Solstice Pod Gen2i",
                    "Solstice Pod Gen2",
                    "Solstice Pod Gen1",
                    "Solstice Windows Software",
                ],
                "confidence": "untested",
                "notes": (
                    "Requires Solstice Enterprise Edition license — Standard "
                    "Edition Pods do not expose the OpenControl API. Some "
                    "commands are Pod-only (reboot, hardware variant, HDMI "
                    "input force-off); Windows Software hosts ignore them. "
                    "Power-management / suspend / wake are Pod-only."
                ),
            },
        ],
        "help": {
            "overview": (
                "Mersive Solstice is a wireless presentation system. Users "
                "share screens, video, and media from laptops, phones, and "
                "tablets to a Solstice Pod that drives the room display.\n\n"
                "OpenAVC controls the Pod's runtime: clear posts, boot all "
                "users at end-of-meeting, sleep / wake the display, reset "
                "the on-screen connection key, and push bulletin or "
                "emergency banners. Live state includes connected users, "
                "active posts, bandwidth, and the current screen key.\n\n"
                "Install-time configuration (network setup, calendar "
                "integration, splash-screen images) stays in the Solstice "
                "Dashboard. RS-232 pass-thru is exposed for chained-display "
                "control via a USB-to-serial adapter on the Pod."
            ),
            "setup": (
                "1. Confirm the Pod has Solstice Enterprise Edition. The "
                "OpenControl API is gated to Enterprise hosts.\n"
                "2. In the Solstice Dashboard, set an admin password "
                "(strongly recommended — without one, any device on the "
                "network can drive this Pod).\n"
                "3. Enter the Pod's IP address and admin password here. "
                "Default port is 80 (HTTP). Tick Use HTTPS for port 443; "
                "Solstice ships with a self-signed certificate which the "
                "driver does not verify.\n"
                "4. Mersive recommends polling no more than once every 10 "
                "seconds. The default interval is 30 s; the minimum is 10 s."
            ),
        },
        "default_config": {
            "host": "",
            "port": 80,
            "use_https": False,
            "admin_password": "",
            "poll_interval": 30,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "help": "IP address of the Solstice Pod or Windows Software host.",
            },
            "port": {
                "type": "integer",
                "default": 80,
                "min": 1,
                "max": 65535,
                "label": "Port",
                "help": "OpenControl port. 80 for HTTP, 443 for HTTPS.",
            },
            "use_https": {
                "type": "boolean",
                "default": False,
                "label": "Use HTTPS",
                "help": (
                    "Use HTTPS instead of HTTP. Solstice Pods ship with a "
                    "self-signed certificate; certificate validation is "
                    "disabled to allow connection."
                ),
            },
            "admin_password": {
                "type": "password",
                "default": "",
                "label": "Admin Password",
                "help": (
                    "Solstice host admin password. Leave blank if no "
                    "password is set on the Pod (not recommended in "
                    "production)."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 10,
                "label": "Poll Interval (sec)",
                "help": (
                    "Mersive recommends polling no more than once every 10 "
                    "seconds. Set to 0 to disable polling."
                ),
            },
        },
        "state_variables": {
            "display_id": {
                "type": "string",
                "label": "Display ID",
            },
            "display_name": {
                "type": "string",
                "label": "Display Name",
            },
            "server_version": {
                "type": "string",
                "label": "Software Version",
            },
            "product_variant": {
                "type": "string",
                "label": "Hardware Variant",
            },
            "current_post_count": {
                "type": "integer",
                "label": "Active Posts",
            },
            "current_bandwidth_mbps": {
                "type": "integer",
                "label": "Bandwidth (Mbps)",
            },
            "connected_users": {
                "type": "integer",
                "label": "Connected Users",
            },
            "in_session": {
                "type": "boolean",
                "label": "In Session",
            },
            "live_source_count": {
                "type": "integer",
                "label": "Live Sources",
            },
            "session_key": {
                "type": "string",
                "label": "Screen Key",
            },
            "screen_key_enabled": {
                "type": "boolean",
                "label": "Screen Key Required",
            },
            "moderator_disabled": {
                "type": "boolean",
                "label": "Moderator Mode Disabled",
            },
            "bulletin_enabled": {
                "type": "boolean",
                "label": "Bulletin Banner Active",
            },
            "bulletin_text": {
                "type": "string",
                "label": "Bulletin Text",
            },
            "emergency_enabled": {
                "type": "boolean",
                "label": "Emergency Broadcast Active",
            },
            "emergency_text": {
                "type": "string",
                "label": "Emergency Text",
            },
            "power_management_enabled": {
                "type": "boolean",
                "label": "Power Management Enabled",
            },
            "license_status": {
                "type": "string",
                "label": "License Status",
            },
        },
        "commands": {
            "clear": {
                "label": "Clear Posts",
                "params": {},
                "help": "Clears all shared content from the display.",
            },
            "boot_users": {
                "label": "Boot All Users",
                "params": {},
                "help": (
                    "Disconnects every connected client and clears the "
                    "display. Returns the Pod to the splash screen. "
                    "Useful at end-of-meeting."
                ),
            },
            "reboot": {
                "label": "Reboot Pod",
                "params": {},
                "help": "Reboots the Pod hardware. Pod-only.",
            },
            "restart": {
                "label": "Restart Solstice",
                "params": {},
                "help": (
                    "Restarts the Solstice software without rebooting the "
                    "host."
                ),
            },
            "reset_screen_key": {
                "label": "Reset Screen Key",
                "params": {},
                "help": (
                    "Generates a new random on-screen connection key. The "
                    "old key is immediately invalid; clients must read the "
                    "new key from the display to reconnect."
                ),
            },
            "suspend": {
                "label": "Suspend Display",
                "params": {},
                "help": (
                    "Puts the Pod in standby and stops the HDMI output. "
                    "Ignored if the Pod is in active use, an emergency "
                    "broadcast is showing, or a meeting is scheduled "
                    "within 15 minutes. Pod-only."
                ),
            },
            "wake": {
                "label": "Wake Display",
                "params": {},
                "help": "Wakes the Pod from standby and resumes HDMI output. Pod-only.",
            },
            "set_display_name": {
                "label": "Set Display Name",
                "params": {
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "Display Name",
                        "help": (
                            "Name shown on the splash screen and in the "
                            "Solstice client discovery list."
                        ),
                    },
                },
            },
            "set_bulletin": {
                "label": "Show Bulletin Banner",
                "params": {
                    "text": {
                        "type": "string",
                        "required": True,
                        "label": "Bulletin Text",
                    },
                },
                "help": (
                    "Shows a scrolling text banner across the top of the "
                    "Solstice display. Useful for room announcements."
                ),
            },
            "clear_bulletin": {
                "label": "Clear Bulletin Banner",
                "params": {},
            },
            "set_emergency": {
                "label": "Show Emergency Broadcast",
                "params": {
                    "text": {
                        "type": "string",
                        "required": True,
                        "label": "Emergency Text",
                    },
                },
                "help": (
                    "Overlays an emergency broadcast on top of all shared "
                    "content. Use for evacuation, lockdown, or other "
                    "life-safety messaging."
                ),
            },
            "clear_emergency": {
                "label": "Clear Emergency Broadcast",
                "params": {},
            },
            "serial_passthru": {
                "label": "RS-232 Pass-Thru",
                "params": {
                    "data": {
                        "type": "string",
                        "required": True,
                        "label": "Data",
                        "help": (
                            "Raw bytes to send out the Pod's USB-to-serial "
                            "adapter to a connected display. Use '+' or "
                            "'%20' for spaces and '%XX' for any other "
                            "non-alphanumeric byte (the value is passed "
                            "through to the URL query string verbatim)."
                        ),
                    },
                },
                "help": (
                    "Sends RS-232 bytes to a display chained off the Pod's "
                    "USB port via a USB-to-serial adapter. Pod-only and "
                    "depends on adapter compatibility."
                ),
            },
        },
        "device_settings": {
            "screen_key_enabled": {
                "type": "boolean",
                "label": "Screen Key Required",
                "help": (
                    "When on, clients must enter the on-screen key before "
                    "connecting to the Pod."
                ),
                "state_key": "screen_key_enabled",
                "default": True,
            },
            "power_management_enabled": {
                "type": "boolean",
                "label": "Power Management",
                "help": (
                    "When on, the Pod follows its configured power schedule "
                    "(sleeps the display after the configured idle delay). "
                    "Schedule itself is configured in the Solstice Dashboard."
                ),
                "state_key": "power_management_enabled",
                "default": False,
            },
        },
        "discovery": {
            # Solstice Pods do not advertise via mDNS, SSDP, or any
            # vendor-specific Bonjour service (Mersive Network Requirements
            # documents UDP 5353 only when Miracast OEN or AirPlay is
            # enabled, and those are non-exclusive third-party services).
            # The only OpenAVC-relevant signals are the open TCP ports
            # the Pod listens on for SDS coordination + Miracast control;
            # match by those + hostname pattern. No Mersive-registered
            # IEEE OUI exists — Pods run on Intel NUC-class hardware so
            # OUI matching would false-positive across the office.
            #   Network Requirements: documentation.mersive.com/en/network-requirements.html
            #   SDS Guide: documentation.mersive.com/content/pdf/solsticediscoveryserviceguide.pdf
            "port_open": [53200, 53201, 53202, 7236],
            "hostname": ["^solstice", "^Pod-"],
            "manufacturer_alias": ["mersive", "solstice"],
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._base_url: str = ""
        self._admin_password: str = ""

    # ── Lifecycle ──

    async def connect(self) -> None:
        """Open an HTTP session and verify the Pod is reachable."""
        host = (self.config.get("host") or "").strip()
        port = int(self.config.get("port") or 80)
        use_https = bool(self.config.get("use_https", False))
        self._admin_password = str(self.config.get("admin_password") or "")
        scheme = "https" if use_https else "http"
        self._base_url = f"{scheme}://{host}:{port}"

        # Solstice Pods ship with self-signed certificates. Disable verification
        # so HTTPS works out of the box.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=10.0,
            verify=False,
        )

        try:
            stats = await self._api_get("/api/stats")
        except Exception as exc:
            await self._client.aclose()
            self._client = None
            raise ConnectionError(
                f"Failed to reach Solstice at {host}:{port}: {exc}"
            ) from exc

        if not isinstance(stats, dict) or "m_displayId" not in stats:
            await self._client.aclose()
            self._client = None
            raise ConnectionError(
                f"Unexpected response from Solstice at {host}:{port} "
                "(missing m_displayId — check the host runs Solstice "
                "Enterprise Edition)"
            )

        log.info(
            "[%s] Connected to Solstice %s at %s:%d (%s)",
            self.device_id,
            stats.get("m_productVariant", "") or "host",
            host,
            port,
            stats.get("m_displayName", "") or "unnamed",
        )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")

        await self._refresh_state()

        poll_interval = int(self.config.get("poll_interval") or 30)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        """Close the HTTP session and stop polling."""
        await self.stop_polling()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info("[%s] Disconnected", self.device_id)

    # ── Driver API ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Execute a Solstice command."""
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match command:
            case "clear":
                await self._api_get("/api/control/clear")
            case "boot_users":
                await self._api_get("/api/control/boot")
            case "reboot":
                await self._api_get("/api/control/reboot")
            case "restart":
                await self._api_get("/api/control/restart")
            case "reset_screen_key":
                await self._api_get("/api/control/resetkey")
            case "suspend":
                await self._api_get("/api/control/suspend")
            case "wake":
                await self._api_get("/api/control/wake")
            case "set_display_name":
                name = str(params.get("name", "")).strip()
                if not name:
                    log.warning(
                        "[%s] set_display_name requires a non-empty name",
                        self.device_id,
                    )
                    return
                await self._api_post(
                    "/api/config",
                    {"m_displayInformation": {"m_displayName": name}},
                )
                self.set_state("display_name", name)
            case "set_bulletin":
                text = str(params.get("text", ""))
                await self._api_post(
                    "/api/config",
                    {
                        "m_networkCuration": {
                            "bulletinEnabled": True,
                            "bulletinText": text,
                        }
                    },
                )
                self.set_state("bulletin_enabled", True)
                self.set_state("bulletin_text", text)
            case "clear_bulletin":
                await self._api_post(
                    "/api/config",
                    {"m_networkCuration": {"bulletinEnabled": False}},
                )
                self.set_state("bulletin_enabled", False)
            case "set_emergency":
                text = str(params.get("text", ""))
                await self._api_post(
                    "/api/config",
                    {
                        "m_networkCuration": {
                            "emergencyEnabled": True,
                            "emergencyText": text,
                        }
                    },
                )
                self.set_state("emergency_enabled", True)
                self.set_state("emergency_text", text)
            case "clear_emergency":
                await self._api_post(
                    "/api/config",
                    {"m_networkCuration": {"emergencyEnabled": False}},
                )
                self.set_state("emergency_enabled", False)
            case "serial_passthru":
                data = str(params.get("data", ""))
                if not data:
                    log.warning(
                        "[%s] serial_passthru requires data",
                        self.device_id,
                    )
                    return
                # Per the OpenControl spec the user pre-encodes spaces as '+' or
                # '%20' and other special bytes as '%XX'. Pass through verbatim
                # in the query string.
                await self._api_get(f"/api/serial-passthru/send?data={data}")
            case _:
                log.warning("[%s] Unknown command: %s", self.device_id, command)

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a Solstice device setting through the config API."""
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match key:
            case "screen_key_enabled":
                await self._api_post(
                    "/api/config",
                    {"m_authenticationCuration": {"screenKeyEnabled": bool(value)}},
                )
                self.set_state("screen_key_enabled", bool(value))
            case "power_management_enabled":
                await self._api_post(
                    "/api/config",
                    {"m_powerManagementCuration": {"enabled": bool(value)}},
                )
                self.set_state("power_management_enabled", bool(value))
            case _:
                raise ValueError(f"Unknown device setting: {key}")

    async def poll(self) -> None:
        """Refresh state from /api/stats and /api/config.

        Transport errors propagate so the BaseDriver watchdog can flip
        device.<id>.connected to False.
        """
        if self._client is None:
            return
        try:
            await self._refresh_state()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise ConnectionError(
                f"Solstice at {self._base_url} not responding: {exc}"
            ) from exc
        except Exception:
            log.exception("[%s] Poll error", self.device_id)

    # ── Internal helpers ──

    async def _refresh_state(self) -> None:
        """Pull /api/stats and /api/config and flatten into state vars."""
        stats = await self._api_get("/api/stats")
        if isinstance(stats, dict):
            self.set_state("display_id", str(stats.get("m_displayId", "")))
            self.set_state("server_version", str(stats.get("m_serverVersion", "")))
            self.set_state("product_variant", str(stats.get("m_productVariant", "")))

            display_info = stats.get("m_displayInformation")
            if isinstance(display_info, dict):
                self.set_state(
                    "display_name", str(display_info.get("m_displayName", ""))
                )

            statistics = stats.get("m_statistics")
            if isinstance(statistics, dict):
                connected = _to_int(statistics.get("m_connectedUsers"))
                self.set_state(
                    "current_post_count",
                    _to_int(statistics.get("m_currentPostCount")),
                )
                self.set_state(
                    "current_bandwidth_mbps",
                    _to_int(statistics.get("m_currentBandwidth")),
                )
                self.set_state("connected_users", connected)
                self.set_state(
                    "live_source_count",
                    _to_int(statistics.get("m_currentLiveSourceCount")),
                )
                self.set_state("in_session", connected > 0)

        config = await self._api_get("/api/config")
        if isinstance(config, dict):
            auth = config.get("m_authenticationCuration")
            if isinstance(auth, dict):
                self.set_state("session_key", str(auth.get("sessionKey", "")))
                self.set_state(
                    "screen_key_enabled", bool(auth.get("screenKeyEnabled", False))
                )
                self.set_state(
                    "moderator_disabled",
                    bool(auth.get("moderatorApprovalDisabled", False)),
                )

            net = config.get("m_networkCuration")
            if isinstance(net, dict):
                self.set_state(
                    "bulletin_enabled", bool(net.get("bulletinEnabled", False))
                )
                self.set_state("bulletin_text", str(net.get("bulletinText", "")))
                self.set_state(
                    "emergency_enabled", bool(net.get("emergencyEnabled", False))
                )
                self.set_state("emergency_text", str(net.get("emergencyText", "")))

            pm = config.get("m_powerManagementCuration")
            if isinstance(pm, dict):
                self.set_state(
                    "power_management_enabled", bool(pm.get("enabled", False))
                )

            lic = config.get("m_licenseCuration")
            if isinstance(lic, dict):
                code = _to_int(lic.get("licenseStatus"))
                self.set_state(
                    "license_status", _LICENSE_STATUS.get(code, "Unknown")
                )

    async def _api_get(self, path: str) -> Any:
        """GET path, returning parsed JSON when possible, otherwise raw text."""
        if self._client is None:
            return None
        full_path = self._with_password_query(path)
        resp = await self._client.get(full_path)
        resp.raise_for_status()
        text = resp.text
        if not text:
            return None
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            return text

    async def _api_post(self, path: str, payload: dict[str, Any]) -> Any:
        """POST a JSON body to path, injecting password when configured."""
        if self._client is None:
            return None
        body = dict(payload)
        if self._admin_password:
            body["password"] = self._admin_password
        resp = await self._client.post(path, json=body)
        resp.raise_for_status()
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            return resp.text

    def _with_password_query(self, path: str) -> str:
        """Append ``?password=...`` (URL-encoded) when an admin password is set."""
        if not self._admin_password:
            return path
        encoded = urllib.parse.quote(self._admin_password, safe="")
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}password={encoded}"


def _to_int(value: Any) -> int:
    """Coerce a JSON value to int, defaulting to 0 on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
