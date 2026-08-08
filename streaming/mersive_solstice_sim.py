"""
Mersive Solstice — Simulator

Emulates the OpenControl REST API on port 80. Implements the runtime
endpoints driven by the mersive_solstice driver: /api/stats (read-only),
/api/config (read + nested-write), /api/control/<verb> (clear/boot/reboot/
restart/resetkey/suspend/wake), and /api/serial-passthru/send.

Auth is enforced when the simulator is started with an admin_password set
in initial_state — GET requests must include ?password=..., POST requests
must include a top-level "password" field. Unset password = open access
(matching the real Pod's behavior).

Driver: mersive_solstice
Transport: http
"""

from __future__ import annotations

import json
import random
import string
from typing import Any
from urllib.parse import parse_qs, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator


def _random_screen_key() -> str:
    """Generate a 4-digit screen key in the same style as a real Solstice Pod."""
    return "".join(random.choices(string.digits, k=4))


def _deep_update(target: dict, src: dict) -> None:
    """Recursively merge src into target — matches Solstice's nested POST semantics."""
    for key, value in src.items():
        if (
            key in target
            and isinstance(target[key], dict)
            and isinstance(value, dict)
        ):
            _deep_update(target[key], value)
        else:
            target[key] = value


class MersiveSolsticeSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "mersive_solstice",
        "name": "Mersive Solstice Simulator",
        "category": "streaming",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            # Identity
            "display_id": "00000000-1111-2222-3333-444455556666",
            "display_name": "Solstice Sim Room",
            "server_version": "6.1.1",
            "product_name": "Solstice",
            "product_variant": "Pod Gen3",
            "product_hardware_version": 3,
            # Stats
            "current_post_count": 0,
            "current_bandwidth_mbps": 0,
            "connected_users": 0,
            "time_since_last_connection_ms": 5_000,
            "live_source_count": 0,
            # Auth / session
            "session_key": "1234",
            "screen_key_enabled": True,
            "moderator_disabled": False,
            "admin_password": "",
            # Banners + power
            "bulletin_enabled": False,
            "bulletin_text": "",
            "emergency_enabled": False,
            "emergency_text": "",
            "power_management_enabled": False,
            # License
            "license_status": 2,  # 2 = OK
            "feature_line": "permanent",
            # Pod runtime
            "suspended": False,
        },
        "delays": {
            "command_response": 0.05,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Pod stops responding to HTTP requests",
                "behavior": "no_response",
            },
            "license_expired": {
                "description": "License has expired",
                "set_state": {"license_status": 3},
            },
            "no_license": {
                "description": "No Enterprise license installed",
                "set_state": {"license_status": 0},
            },
        },
        "controls": [
            {"type": "indicator", "key": "display_name", "label": "Display Name"},
            {"type": "indicator", "key": "session_key", "label": "Screen Key"},
            {"type": "indicator", "key": "connected_users", "label": "Users"},
            {"type": "indicator", "key": "current_post_count", "label": "Posts"},
            {
                "type": "number",
                "key": "connected_users",
                "label": "Simulate Users",
                "min": 0,
                "max": 16,
            },
            {
                "type": "number",
                "key": "current_post_count",
                "label": "Simulate Posts",
                "min": 0,
                "max": 32,
            },
            {
                "type": "toggle",
                "key": "screen_key_enabled",
                "label": "Screen Key Required",
            },
            {
                "type": "toggle",
                "key": "power_management_enabled",
                "label": "Power Management",
            },
            {"type": "toggle", "key": "suspended", "label": "Suspended"},
            {
                "type": "toggle",
                "key": "bulletin_enabled",
                "label": "Bulletin Banner",
            },
            {
                "type": "toggle",
                "key": "emergency_enabled",
                "label": "Emergency Broadcast",
            },
        ],
    }

    # ── Request handler ──

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        parts = urlsplit(path)
        clean_path = parts.path.rstrip("/")
        query = parse_qs(parts.query)

        # Parse JSON body for POST. Solstice tolerates an empty body on POST.
        body_data: dict = {}
        if body:
            try:
                body_data = json.loads(body)
                if not isinstance(body_data, dict):
                    body_data = {}
            except (json.JSONDecodeError, ValueError):
                body_data = {}

        # ── Auth gate ──
        admin_password = str(self.get_state("admin_password", "") or "")
        if admin_password:
            if method == "GET":
                supplied = query.get("password", [""])[0]
            else:
                supplied = str(body_data.get("password", "") or "")
            if supplied != admin_password:
                return 401, {"error": "unauthorized"}

        # Strip the password key from POST bodies before processing.
        body_data.pop("password", None)

        # ── /api/stats ──
        if clean_path == "/api/stats" and method == "GET":
            return 200, self._stats_payload()

        # ── /api/config ──
        if clean_path == "/api/config":
            if method == "GET":
                return 200, self._config_payload()
            if method == "POST":
                self._apply_config_post(body_data)
                return 200, {"rebootRequired": False, "restartRequired": False}
            return 405, {"error": "method not allowed"}

        # ── /api/control/<verb> ──
        if clean_path.startswith("/api/control/") and method == "GET":
            verb = clean_path.removeprefix("/api/control/")
            return self._handle_control(verb)

        # ── /api/serial-passthru/send ──
        if clean_path == "/api/serial-passthru/send" and method == "GET":
            data = query.get("data", [""])[0]
            self.log_protocol("in", f"serial-passthru: {data!r}")
            return 200, {"status": "ok"}

        # ── /api/version/* ──
        if clean_path == "/api/version/currentversion" and method == "GET":
            return 200, {"currentVersion": self.get_state("server_version", "6.1.1")}
        if clean_path == "/api/version/updateavailable" and method == "GET":
            return 200, {"isUpdateAvailable": False}

        return 404, {"error": "not found"}

    # ── Payload builders ──

    def _stats_payload(self) -> dict[str, Any]:
        return {
            "m_displayId": self.get_state("display_id", ""),
            "m_serverVersion": self.get_state("server_version", ""),
            "m_productName": self.get_state("product_name", "Solstice"),
            "m_productVariant": self.get_state("product_variant", ""),
            "m_productHardwareVersion": int(
                self.get_state("product_hardware_version", 0) or 0
            ),
            "m_displayInformation": {
                "m_displayName": self.get_state("display_name", ""),
            },
            "m_statistics": {
                "m_currentPostCount": int(
                    self.get_state("current_post_count", 0) or 0
                ),
                "m_currentBandwidth": int(
                    self.get_state("current_bandwidth_mbps", 0) or 0
                ),
                "m_connectedUsers": int(self.get_state("connected_users", 0) or 0),
                "m_timeSinceLastConnectionInitialize": int(
                    self.get_state("time_since_last_connection_ms", 0) or 0
                ),
                "m_currentLiveSourceCount": int(
                    self.get_state("live_source_count", 0) or 0
                ),
            },
        }

    def _config_payload(self) -> dict[str, Any]:
        return {
            "m_displayId": self.get_state("display_id", ""),
            "m_serverVersion": self.get_state("server_version", ""),
            "m_productName": self.get_state("product_name", "Solstice"),
            "m_productVariant": self.get_state("product_variant", ""),
            "m_displayInformation": {
                "m_displayName": self.get_state("display_name", ""),
                "m_ipv4": "127.0.0.1",
                "m_hostName": "solstice-sim",
                "m_port": 53100,
            },
            "m_authenticationCuration": {
                "sessionKey": self.get_state("session_key", ""),
                "screenKeyEnabled": bool(self.get_state("screen_key_enabled", False)),
                "moderatorApprovalDisabled": bool(
                    self.get_state("moderator_disabled", False)
                ),
                "authenticationMode": 1
                if self.get_state("screen_key_enabled", False)
                else 0,
            },
            "m_networkCuration": {
                "bulletinEnabled": bool(self.get_state("bulletin_enabled", False)),
                "bulletinText": self.get_state("bulletin_text", ""),
                "emergencyEnabled": bool(self.get_state("emergency_enabled", False)),
                "emergencyText": self.get_state("emergency_text", ""),
                "discoveryBroadcastEnabled": True,
                "maximumConnections": 4,
                "maximumLicensedConnections": 4,
            },
            "m_powerManagementCuration": {
                "enabled": bool(self.get_state("power_management_enabled", False)),
                "weekdaysAllDay": False,
                "weekdaysBegin": "08:00",
                "weekdaysEnd": "18:00",
                "weekdaysDelayMinutes": 15,
                "weekendAllDay": True,
                "weekendBegin": "00:00",
                "weekendEnd": "23:59",
                "weekendDelayMinutes": 15,
            },
            "m_licenseCuration": {
                "licenseStatus": int(self.get_state("license_status", 2) or 0),
                "fulfillmentType": "PUBLISHER ACTIVATION",
                "enabled": True,
                "featureLine": self.get_state("feature_line", "permanent"),
                "numDaysToExpiration": 999_999_999,
                "maxUsers": "Unlimited",
            },
            "m_userGroupCuration": {
                "adminPassword": "unknown",
                "passwordValidationEnabled": False,
            },
            "m_systemCuration": {
                "autoDateTime": True,
                "ntpServer": "",
                "timeZone": "America/New_York",
                "scheduledRestartEnabled": False,
                "scheduledRestartTime": "03:00",
            },
        }

    # ── Mutation helpers ──

    def _apply_config_post(self, body: dict[str, Any]) -> None:
        """Translate a Solstice nested POST body into simulator state changes."""
        display_info = body.get("m_displayInformation")
        if isinstance(display_info, dict) and "m_displayName" in display_info:
            self.set_state("display_name", str(display_info["m_displayName"]))

        auth = body.get("m_authenticationCuration")
        if isinstance(auth, dict):
            if "screenKeyEnabled" in auth:
                self.set_state("screen_key_enabled", bool(auth["screenKeyEnabled"]))
            if "moderatorApprovalDisabled" in auth:
                self.set_state(
                    "moderator_disabled", bool(auth["moderatorApprovalDisabled"])
                )

        net = body.get("m_networkCuration")
        if isinstance(net, dict):
            if "bulletinEnabled" in net:
                self.set_state("bulletin_enabled", bool(net["bulletinEnabled"]))
            if "bulletinText" in net:
                self.set_state("bulletin_text", str(net["bulletinText"]))
            if "emergencyEnabled" in net:
                self.set_state("emergency_enabled", bool(net["emergencyEnabled"]))
            if "emergencyText" in net:
                self.set_state("emergency_text", str(net["emergencyText"]))

        pm = body.get("m_powerManagementCuration")
        if isinstance(pm, dict) and "enabled" in pm:
            self.set_state("power_management_enabled", bool(pm["enabled"]))

        ugroup = body.get("m_userGroupCuration")
        if isinstance(ugroup, dict) and "adminPassword" in ugroup:
            self.set_state("admin_password", str(ugroup["adminPassword"] or ""))

    def _handle_control(self, verb: str) -> tuple[int, dict | str]:
        if verb == "clear":
            self.set_state("current_post_count", 0)
            return 200, {"status": "ok"}
        if verb == "boot":
            self.set_state("current_post_count", 0)
            self.set_state("connected_users", 0)
            self.set_state("live_source_count", 0)
            return 200, {"status": "ok"}
        if verb == "reboot":
            # Pretend the device is rebooting — clear runtime state. Real Pods
            # take ~60 s to come back; we don't simulate the downtime.
            self.set_state("current_post_count", 0)
            self.set_state("connected_users", 0)
            self.set_state("suspended", False)
            return 200, {"status": "ok"}
        if verb == "restart":
            self.set_state("current_post_count", 0)
            self.set_state("connected_users", 0)
            return 200, {"status": "ok"}
        if verb == "resetkey":
            self.set_state("session_key", _random_screen_key())
            return 200, {"status": "ok"}
        if verb == "suspend":
            # Real Pods refuse to suspend when actively in use; mirror that here.
            users = int(self.get_state("connected_users", 0) or 0)
            if users > 0 or bool(self.get_state("emergency_enabled", False)):
                return 200, {"status": "ignored"}
            self.set_state("suspended", True)
            return 200, {"status": "ok"}
        if verb == "wake":
            self.set_state("suspended", False)
            return 200, {"status": "ok"}
        return 404, {"error": f"unknown control verb: {verb}"}
