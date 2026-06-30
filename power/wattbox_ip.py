"""
OpenAVC WattBox IP-Controlled PDU Driver.

Controls SnapAV WattBox IP-controlled power distribution units over the
WattBox Integration Protocol — a text-based, line-oriented Telnet protocol
on TCP port 23, requiring username/password authentication.

Models covered (any WattBox running firmware that exposes the v1.7
Integration Protocol — IP / IPV / IPVM series, 300/700/800):
    WB-300-IP-3, WB-300-IP-MV-3
    WB-700-IPV-12, WB-700-IPV-12B, WB-700-IPVM-IN-NM
    WB-800-IPVM-12, WB-800-IPVM-18, WB-800-IPVM-IN-NM
    WB-800VS-IPVM-12, WB-800VS-IPVM-18

Protocol summary:
    Connect (TCP 23) → server sends "Please Login to Continue\\n" then
    "Username: " → client replies "<user>\\n" → "Password: " →
    "<pass>\\n" → "Successfully Logged In!\\n" → command mode. On bad
    credentials the server re-sends the login prompt instead.
    Commands are ASCII text, '\\n'-terminated, prefixed by '?', '!', '#',
    or '~'. Source: SnapAV WattBox Integration Protocol v1.7 (rev 20190520).

Why Python: prompt-driven login whose RESULT must be awaited (so a bad
credential surfaces as an auth fault, not a silent half-connected session)
plus an awaited liveness watchdog — v1.7 documents no push and no protocol
keepalive, so a silently-vanished unit on raw TCP is otherwise undetectable.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

# WattBox outlet counts top out at 18 (WB-800-IPVM-18 / WB-800VS-IPVM-18).
# Outlets are child entities reconciled from ?OutletCount, so an 18-outlet
# unit and a 12-outlet unit each surface exactly the outlets they have.
MAX_OUTLETS = 18

OUTLET_MODES = {"enabled": 0, "disabled": 1, "reset_only": 2}

# Login handshake + liveness watchdog timing.
LOGIN_TIMEOUT_S = 5.0
KEEPALIVE_INTERVAL_S = 30.0
KEEPALIVE_TIMEOUT_S = 5.0
KEEPALIVE_MAX_FAILURES = 2


def _build_state_vars() -> dict[str, dict[str, Any]]:
    # Per-outlet values (state / name / power / current / voltage) are
    # modelled as child entities (see CHILD_ENTITY_TYPES), not flat
    # outlet_N_* keys, so the surface scales to the unit's real outlet
    # count. Everything below is device-level.
    return {
        "model": {"type": "string", "label": "Model"},
        "hostname": {"type": "string", "label": "Hostname"},
        "serial": {"type": "string", "label": "Serial Number"},
        "firmware": {"type": "string", "label": "Firmware Version"},
        "outlet_count": {"type": "integer", "label": "Outlet Count", "min": 0},
        "voltage": {"type": "number", "label": "System Voltage (V)"},
        "current": {"type": "number", "label": "System Current (A)"},
        "power_watts": {"type": "number", "label": "System Power (W)"},
        "safe_voltage": {"type": "boolean", "label": "Safe Voltage"},
        "auto_reboot": {"type": "boolean", "label": "Auto Reboot Enabled"},
        "ups_connected": {"type": "boolean", "label": "UPS Connected"},
        "ups_battery_charge": {"type": "integer", "label": "UPS Battery Charge (%)"},
        "ups_battery_load": {"type": "integer", "label": "UPS Battery Load (%)"},
        "ups_battery_health": {
            "type": "enum",
            "values": ["Good", "Bad"],
            "label": "UPS Battery Health",
        },
        "ups_power_lost": {"type": "boolean", "label": "UPS Power Lost"},
        "ups_battery_runtime": {
            "type": "integer",
            "label": "UPS Battery Runtime (min)",
        },
        "ups_alarm_enabled": {"type": "boolean", "label": "UPS Alarm Enabled"},
        "ups_alarm_muted": {"type": "boolean", "label": "UPS Alarm Muted"},
    }


# Each outlet is an addressable child of the one PDU. State lands at
# device.<id>.outlet.<NN>.<prop>. `state` is the operationally-meaningful
# live value (high cloud tier); `name` and the per-outlet metering change
# rarely / are telemetry (low tier). Device-side outlet names live here as
# child state set by the rename command — not as device_settings (a
# setting's flat state_key can't point into a child key).
CHILD_ENTITY_TYPES = {
    "outlet": {
        "label": "Outlet",
        "label_plural": "Outlets",
        "id_format": {"type": "integer", "min": 1, "max": MAX_OUTLETS, "pad_width": 2},
        "state_variables": {
            "state": {"type": "boolean", "label": "Powered", "cloud_priority": "high"},
            "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
            "power": {"type": "number", "label": "Power (W)", "cloud_priority": "low"},
            "current": {"type": "number", "label": "Current (A)", "cloud_priority": "low"},
            "voltage": {"type": "number", "label": "Voltage (V)", "cloud_priority": "low"},
        },
        "summary_fields": ["name", "state", "power"],
        "label_field": "name",
    },
}


class WattBoxIPDriver(BaseDriver):
    """SnapAV WattBox IP-Controlled PDU driver."""

    DRIVER_INFO = {
        "id": "wattbox_ip",
        "name": "WattBox IP-Controlled PDU",
        "manufacturer": "WattBox",
        "category": "power",
        "version": "1.3.0",
        "author": "OpenAVC",
        "description": (
            "Controls SnapAV WattBox IP-controlled power distribution units "
            "over the WattBox Integration Protocol (Telnet, port 23). "
            "Outlets are addressable children with per-outlet state, name, "
            "and power metering. Supports on/off/toggle/reset, per-outlet "
            "naming, auto-reboot configuration, system + per-outlet power "
            "metering, and UPS status reporting on units with a connected UPS."
        ),
        "source_url": "https://www.snapav.com/wcsstore/ExtendedSitesCatalogAssetStore/attachments/documents/PowerManagement/SupportDocuments/SnapAV_Wattbox_API_V1.7.pdf",
        "tags": ["pdu", "power", "outlet-control", "ups", "telnet"],
        "verified": False,
        "simulated": True,
        "protocols": ["wattbox-integration-v1.7"],
        "ports": [23],
        "transport": "tcp",
        "discovery": {
            # SnapAV / Snap One OUIs — shared across WattBox, Araknis,
            # Pakedge, Wirepath, and Binary product lines. WattBox's
            # `/wattbox_info.xml` HTTP endpoint is a near-perfect
            # fingerprint (returns hostname / hardware_version /
            # serial_number XML) but lives on TCP 80, which is in
            # the disallowed port_open set; a declarative tcp_probe on
            # 80 is deferred until we have a real captured response to
            # verify against. WattBox does not expose a polled SNMP
            # MIB (only outbound traps).
            #   Refs:
            #     SnapAV_Wattbox_API_V2.2.pdf (Telnet 23 / HTTP API)
            #     WattBox.WB10.API.v3.0.pdf (XML endpoint)
            "oui": ["d4:6a:91", "14:3f:c3"],
            "manufacturer_alias": [
                "wattbox", "snapav", "snap one", "snap-av", "wirepath",
            ],
        },
        "compatible_models": [
            {
                "manufacturer": "WattBox",
                "models": [
                    "WB-300-IP-3",
                    "WB-300-IP-MV-3",
                    "WB-700-IPV-12",
                    "WB-700-IPV-12B",
                    "WB-700-IPVM-IN-NM",
                    "WB-800-IPVM-12",
                    "WB-800-IPVM-18",
                    "WB-800-IPVM-IN-NM",
                    "WB-800VS-IPVM-12",
                    "WB-800VS-IPVM-18",
                ],
                "confidence": "untested",
                "notes": (
                    "All current IP / IPV / IPVM WattBoxes share the v1.7 "
                    "Integration Protocol. Outlets are reconciled from the "
                    "unit's reported outlet count, so 3 / 12 / 18-outlet "
                    "models each surface exactly their outlets as children."
                ),
            }
        ],
        "help": {
            "overview": (
                "WattBox is the SnapAV / Snap One family of IP-controlled "
                "power distribution units commonly installed in commercial "
                "AV racks. This driver speaks the v1.7 Integration Protocol, "
                "letting macros switch outlets on / off / reset, rename "
                "outlets, configure auto-reboot, and read live power / UPS "
                "telemetry. Use this driver instead of OvrC for local "
                "OpenAVC control — OvrC is a separate cloud product."
            ),
            "setup": (
                "1. Connect the WattBox to your network via Ethernet.\n"
                "2. Find the WattBox's IP from the front panel (where "
                "applicable) or from the WattBox web interface.\n"
                "3. In the WattBox web interface, ensure the integration "
                "protocol is enabled and confirm the admin username and "
                "password.\n"
                "4. Enter the IP, port 23, and credentials in the device "
                "config in OpenAVC.\n"
                "5. Outlets are 1-indexed in commands. Use Reset All Outlets "
                "to power-cycle the whole unit."
            ),
        },
        "default_config": {
            "host": "",
            "port": 23,
            "username": "wattbox",
            "password": "wattbox",
            "poll_interval": 30,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 23,
                "label": "TCP Port",
                "description": (
                    "WattBox Integration Protocol port — always 23 (Telnet)."
                ),
            },
            "username": {
                "type": "string",
                "required": True,
                "default": "wattbox",
                "label": "Username",
            },
            "password": {
                "type": "string",
                "required": True,
                "default": "wattbox",
                "label": "Password",
                "secret": True,
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often to refresh outlet states + power readings. "
                    "Set to 0 to disable polling."
                ),
            },
        },
        "state_variables": _build_state_vars(),
        "child_entity_types": CHILD_ENTITY_TYPES,
        "device_settings": {
            # Auto-reboot is the one genuine device-level setting WattBox
            # both accepts (!AutoReboot=) and reports back (?AutoReboot),
            # so it gets the editable-field + offline pending-queue
            # treatment. Per-outlet power-on delay and outlet mode are
            # SET-only in v1.7 (no ?OutletPowerOnDelay / ?OutletMode read
            # verb), so they stay transient commands — without read-back
            # they aren't device settings.
            "auto_reboot": {
                "type": "boolean",
                "label": "Auto Reboot",
                "help": (
                    "When enabled, the WattBox automatically power-cycles "
                    "the outlets tied to a monitored host if that host stops "
                    "responding. Configure the monitored hosts and timeouts "
                    "in the WattBox web UI."
                ),
                "state_key": "auto_reboot",
                "default": True,
                "setup": False,
            },
        },
        "quick_actions": [
            "reset_all",
            "reboot_device",
            "refresh",
        ],
        "actions": [
            {
                "id": "reset_all",
                "kind": "command",
                "icon": "rotate-ccw",
                "confirm": (
                    "Reset (power-cycle) every outlet on the WattBox. "
                    "Connected gear briefly loses power. Continue?"
                ),
            },
            {
                "id": "reboot_device",
                "kind": "command",
                "icon": "power",
                "confirm": (
                    "Reboot the WattBox itself. The connection drops until "
                    "the unit is back online. Continue?"
                ),
            },
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
        ],
        "commands": {
            "outlet_on": {
                "label": "Turn Outlet On",
                "params": {
                    "outlet": {
                        "type": "child_id",
                        "child_type": "outlet",
                        "required": True,
                        "label": "Outlet",
                    },
                },
                "help": "Turn a specific outlet on.",
            },
            "outlet_off": {
                "label": "Turn Outlet Off",
                "params": {
                    "outlet": {
                        "type": "child_id",
                        "child_type": "outlet",
                        "required": True,
                        "label": "Outlet",
                    },
                },
                "help": "Turn a specific outlet off.",
            },
            "outlet_toggle": {
                "label": "Toggle Outlet",
                "params": {
                    "outlet": {
                        "type": "child_id",
                        "child_type": "outlet",
                        "required": True,
                        "label": "Outlet",
                    },
                },
                "help": "Toggle a specific outlet.",
            },
            "outlet_reset": {
                "label": "Reset Outlet",
                "params": {
                    "outlet": {
                        "type": "child_id",
                        "child_type": "outlet",
                        "required": True,
                        "label": "Outlet",
                    },
                    "delay": {
                        "type": "integer",
                        "required": False,
                        "min": 1,
                        "max": 600,
                        "help": (
                            "Optional reset delay in seconds (1–600). "
                            "Overrides the configured power-on delay."
                        ),
                    },
                },
                "help": "Reset (power-cycle) a specific outlet.",
            },
            "reset_all": {
                "label": "Reset All Outlets",
                "params": {},
                "help": (
                    "Reset (power-cycle) every outlet on the WattBox at once."
                ),
            },
            "set_outlet_name": {
                "label": "Rename Outlet",
                "params": {
                    "outlet": {
                        "type": "child_id",
                        "child_type": "outlet",
                        "required": True,
                        "label": "Outlet",
                    },
                    "name": {"type": "string", "required": True},
                },
                "help": "Rename an outlet so it shows the right label in the WattBox UI.",
            },
            "set_outlet_power_on_delay": {
                "label": "Set Outlet Power-On Delay",
                "params": {
                    "outlet": {
                        "type": "child_id",
                        "child_type": "outlet",
                        "required": True,
                        "label": "Outlet",
                    },
                    "delay": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 600,
                    },
                },
                "help": (
                    "Set the staggered power-on delay (1–600 seconds) for "
                    "an outlet so connected gear comes up in sequence. The "
                    "WattBox does not report this value back, so it stays a "
                    "transient command, not a read-back setting."
                ),
            },
            "set_outlet_mode": {
                "label": "Set Outlet Mode",
                "params": {
                    "outlet": {
                        "type": "child_id",
                        "child_type": "outlet",
                        "required": True,
                        "label": "Outlet",
                    },
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["enabled", "disabled", "reset_only"],
                    },
                },
                "help": (
                    "Set how an outlet reacts to switching commands: "
                    "enabled (default), disabled (locked off), or "
                    "reset_only (rejects on / off, accepts reset). The "
                    "WattBox does not report this value back, so it stays a "
                    "transient command, not a read-back setting."
                ),
            },
            "set_auto_reboot": {
                "label": "Set Auto-Reboot",
                "params": {
                    "enabled": {"type": "boolean", "required": True},
                },
                "help": (
                    "Enable or disable the WattBox's auto-reboot feature, "
                    "which power-cycles outlets when monitored hosts time "
                    "out. (Also editable as a device setting.)"
                ),
            },
            "reboot_device": {
                "label": "Reboot WattBox",
                "params": {},
                "help": (
                    "Reboot the WattBox itself. The connection drops until "
                    "the unit is back online."
                ),
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Re-poll all status from the WattBox.",
            },
        },
    }

    # ── Lifecycle ──

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state,
        events,
    ):
        self._line_buffer = b""
        self._authenticated = False
        # Login handshake state machine: "user" → "pass" → "result" →
        # "done". Drives the prompt-driven login from on_data so the result
        # can be awaited.
        self._auth_stage = "user"
        self._auth_user = ""
        self._auth_pass = ""
        self._login_future: asyncio.Future[bool] | None = None
        # Awaited request/response correlation, keyed by the response field
        # name. The text protocol has no message id, but only the liveness
        # probe ever awaits a reply (regular polls are fire-and-forget), so
        # a single outstanding future per field name is sufficient.
        self._pending: dict[str, asyncio.Future[str]] = {}
        # Liveness watchdog (qsc_qrc / racklink pattern): WattBox v1.7 has
        # no push and no protocol keepalive, so a silently-dropped unit on
        # raw TCP needs an awaited probe to be detected.
        self._health_task: asyncio.Task | None = None
        self._health_failures = 0
        super().__init__(device_id, config, state, events)

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 23))
        self._auth_user = self.config.get("username", "wattbox") or ""
        self._auth_pass = self.config.get("password", "wattbox") or ""

        # Reset the auth state machine for this (re)connection.
        self._authenticated = False
        self._auth_stage = "user"
        self._line_buffer = b""
        self._login_future = asyncio.get_running_loop().create_future()

        # Open a raw TCP connection — we parse the login banner / username /
        # password prompts ourselves and drive the handshake reactively.
        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=None,
            timeout=5.0,
            name=self.device_id,
        )

        # AWAIT the login result. The banner / prompts arrive through the
        # normal data path; _drive_auth sends the credentials in order and
        # resolves _login_future True (saw "Successfully Logged In") or
        # False (the WattBox re-prompted = bad credentials). Resolving it
        # here lets a bad credential surface as a real auth fault — the
        # "authentication failed"-worded ConnectionError below is what the
        # shared connection-fault classifier maps to offline_reason=
        # auth_failed — instead of a silent half-connected session.
        try:
            ok = await asyncio.wait_for(self._login_future, LOGIN_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            await self._safe_close()
            raise ConnectionError(
                f"WattBox at {host}:{port} did not complete the login "
                f"handshake within {LOGIN_TIMEOUT_S:.0f}s"
            ) from e
        except (ConnectionError, OSError) as e:
            await self._safe_close()
            raise ConnectionError(
                f"WattBox login send failed for {host}:{port}: {e}"
            ) from e

        if not ok:
            await self._safe_close()
            raise ConnectionError(
                f"WattBox authentication failed for {host}:{port} — check "
                f"the username and password."
            )
        self._authenticated = True

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to WattBox at {host}:{port}")

        # Two-pass initial status sweep: the first pass learns
        # outlet_count, the second pass uses that count to query
        # per-outlet metering. Polling after that is single-pass.
        try:
            await self.poll()
            await asyncio.sleep(0.3)
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

        # Liveness watchdog: no push / keepalive in the protocol, so an
        # awaited probe is the only way to notice a silently-dropped unit.
        self._start_health_loop()

        poll_interval = int(self.config.get("poll_interval", 30))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self._login_future is not None and not self._login_future.done():
            self._login_future.cancel()
        self._login_future = None
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self._authenticated = False
        self._auth_stage = "user"
        self._line_buffer = b""
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    # ── Request/response correlation + liveness watchdog ──

    def _prime_response(self, key: str) -> asyncio.Future[str]:
        """Register interest in the next response carrying field ``key``.

        Call this BEFORE sending the query so a fast reply can't arrive
        before the future exists. Only the liveness probe awaits a reply;
        regular polls are fire-and-forget, so one outstanding future per
        field name is sufficient.
        """
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        return fut

    async def _await_response(
        self, fut: asyncio.Future[str], key: str, timeout: float
    ) -> str:
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            if self._pending.get(key) is fut:
                self._pending.pop(key, None)

    def _resolve_pending(self, key: str, value: str) -> None:
        fut = self._pending.get(key)
        if fut is not None and not fut.done():
            fut.set_result(value)

    def _start_health_loop(self) -> None:
        if self._health_task is None or self._health_task.done():
            self._health_failures = 0
            self._health_task = asyncio.ensure_future(self._health_loop())

    def _stop_health_loop(self) -> None:
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
        self._health_task = None

    async def _health_loop(self) -> None:
        try:
            while self.transport and self.transport.connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                if not (self.transport and self.transport.connected):
                    return
                try:
                    await self._probe_once()
                    self._health_failures = 0
                except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                    self._health_failures += 1
                    log.warning(
                        f"[{self.device_id}] Liveness probe failed "
                        f"({self._health_failures}/{KEEPALIVE_MAX_FAILURES}): {exc}"
                    )
                    if self._health_failures >= KEEPALIVE_MAX_FAILURES:
                        log.warning(
                            f"[{self.device_id}] WattBox unresponsive — forcing reconnect"
                        )
                        self._force_disconnect()
                        return
        except asyncio.CancelledError:
            return

    async def _probe_once(self) -> None:
        """Send a cheap query and await its reply as a liveness probe."""
        fut = self._prime_response("Firmware")
        await self._send("?Firmware")
        await self._await_response(fut, "Firmware", KEEPALIVE_TIMEOUT_S)

    def _force_disconnect(self) -> None:
        # Null our own task ref first so the disconnect handler doesn't
        # cancel the still-running loop out from under us.
        self._health_task = None
        self._handle_transport_disconnect()

    async def _safe_close(self) -> None:
        if self.transport:
            try:
                await self.transport.close()
            except Exception:  # noqa: BLE001
                pass
            self.transport = None

    # ── Sending ──

    async def _send(self, line: str) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send((line + "\n").encode("utf-8"))

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "outlet_on":
            await self._send(f"!OutletSet={self._outlet_id(params)},ON")
        elif command == "outlet_off":
            await self._send(f"!OutletSet={self._outlet_id(params)},OFF")
        elif command == "outlet_toggle":
            await self._send(f"!OutletSet={self._outlet_id(params)},TOGGLE")
        elif command == "outlet_reset":
            outlet = self._outlet_id(params)
            delay = params.get("delay")
            if delay is not None:
                await self._send(f"!OutletSet={outlet},RESET,{int(delay)}")
            else:
                await self._send(f"!OutletSet={outlet},RESET")
        elif command == "reset_all":
            # Outlet 0 + RESET = reset every outlet (v1.7).
            await self._send("!OutletSet=0,RESET")
        elif command == "set_outlet_name":
            outlet = self._outlet_id(params)
            name = str(params["name"])
            await self._send(f"!OutletNameSet={outlet},{name}")
        elif command == "set_outlet_power_on_delay":
            outlet = self._outlet_id(params)
            delay = int(params["delay"])
            await self._send(f"!OutletPowerOnDelaySet={outlet},{delay}")
        elif command == "set_outlet_mode":
            outlet = self._outlet_id(params)
            mode = str(params["mode"])
            mode_num = OUTLET_MODES.get(mode)
            if mode_num is None:
                raise ValueError(f"Unknown outlet mode: {mode}")
            await self._send(f"!OutletModeSet={outlet},{mode_num}")
        elif command == "set_auto_reboot":
            enabled = bool(params["enabled"])
            await self._send(f"!AutoReboot={1 if enabled else 0}")
        elif command == "reboot_device":
            await self._send("!Reboot")
        elif command == "refresh":
            await self.poll()
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    @staticmethod
    def _outlet_id(params: dict[str, Any]) -> int:
        # child_id pickers can hand back a zero-padded string ("05"); coerce.
        return int(params["outlet"])

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting to the WattBox.

        Only auto-reboot is a real read-back setting (?AutoReboot reports
        it). The offline pending queue + read-back are handled by the
        platform via the ``device_settings`` declaration above.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if key == "auto_reboot":
            enabled = (
                value if isinstance(value, bool)
                else str(value).strip().lower() in ("1", "true", "on", "yes")
            )
            await self._send(f"!AutoReboot={1 if enabled else 0}")
            log.info(
                f"[{self.device_id}] Set auto-reboot "
                f"{'on' if enabled else 'off'}"
            )
        else:
            raise ValueError(f"Unknown device setting: {key}")

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return
        try:
            for query in (
                "?Firmware",
                "?Hostname",
                "?Model",
                "?Serial",
                "?OutletCount",
                "?OutletStatus",
                "?OutletName",
                "?PowerStatus",
                "?AutoReboot",
                "?UPSConnection",
            ):
                await self._send(query)
            # Per-outlet metering only after we've learned the count
            # from a prior ?OutletCount response. Skipping on the first
            # poll round avoids spurious #Error responses for outlets
            # past the device's actual count.
            count = self.get_state("outlet_count") or 0
            for n in range(1, int(count) + 1):
                await self._send(f"?OutletPowerStatus={n}")
            # UPS status only meaningful when a UPS is attached.
            if self.get_state("ups_connected"):
                await self._send("?UPSStatus")
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")

    # ── Parsing ──

    async def on_data_received(self, data: bytes) -> None:
        self._line_buffer += data
        if not self._authenticated:
            # Still logging in — drive the prompt-driven handshake and don't
            # parse the buffer as command lines yet.
            await self._drive_auth()
            return
        while b"\n" in self._line_buffer:
            line_bytes, self._line_buffer = self._line_buffer.split(b"\n", 1)
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                self._handle_line(line)

    async def _drive_auth(self) -> None:
        """Advance the login handshake from buffered banner / prompt bytes.

        The prompts ("Username: " / "Password: ") arrive without a trailing
        newline, so we scan the raw buffer for them rather than splitting on
        '\\n'. Each stage consumes the buffer it acted on. Only a login
        re-prompt SEEN IN THE "result" STAGE (after the password was sent)
        counts as a rejection — the initial banner is expected in "user".
        """
        text = self._line_buffer.decode("utf-8", errors="replace")
        if self._auth_stage == "user":
            if "Username:" in text or "Please Login" in text:
                self._auth_stage = "pass"
                self._line_buffer = b""
                await self._send(self._auth_user)
        elif self._auth_stage == "pass":
            if "Password:" in text:
                self._auth_stage = "result"
                self._line_buffer = b""
                await self._send(self._auth_pass)
        elif self._auth_stage == "result":
            if "Successfully" in text:
                self._auth_stage = "done"
                self._authenticated = True
                self._line_buffer = b""
                self._resolve_login(True)
            elif (
                "Username:" in text
                or "Please Login" in text
                or "#Error" in text
            ):
                self._auth_stage = "done"
                self._line_buffer = b""
                self._resolve_login(False)

    def _resolve_login(self, ok: bool) -> None:
        fut = self._login_future
        if fut is not None and not fut.done():
            fut.set_result(ok)

    def _handle_line(self, line: str) -> None:
        # Banner / login prompts (e.g. on a mid-session reconnect) — drop.
        if line.startswith("Please Login") or line.startswith("Username:"):
            return
        if line.startswith("Password:") or line.startswith("Successfully"):
            return
        if line.startswith("OK"):
            return
        if line.startswith("#"):
            log.warning(f"[{self.device_id}] Device error: {line}")
            return

        # Both ? (poll responses) and ~ (unsolicited push) carry state.
        if line[0] in ("?", "~") and "=" in line:
            key, _, value = line[1:].partition("=")
            self._dispatch(key.strip(), value.strip())
            return

    def _dispatch(self, key: str, value: str) -> None:
        # Resolve any awaiter (the liveness probe) waiting on this field.
        self._resolve_pending(key, value)

        if key == "Firmware":
            self.set_state("firmware", value)
        elif key == "Hostname":
            self.set_state("hostname", value)
        elif key == "Serial":
            self.set_state("serial", value)
        elif key == "Model":
            self.set_state("model", value)
        elif key == "OutletCount":
            self._dispatch_outlet_count(value)
        elif key == "OutletStatus":
            for i, raw in enumerate(value.split(","), start=1):
                state_val = _safe_int(raw)
                if state_val is not None and 1 <= i <= MAX_OUTLETS:
                    self.register_child("outlet", i)  # idempotent
                    self.set_child_state("outlet", i, "state", state_val == 1)
        elif key == "OutletName":
            for i, name in enumerate(_split_brace_list(value), start=1):
                if 1 <= i <= MAX_OUTLETS:
                    self.register_child("outlet", i)  # idempotent
                    self.set_child_state("outlet", i, "name", name)
        elif key == "OutletPowerStatus":
            parts = [p.strip() for p in value.split(",")]
            if len(parts) >= 4:
                idx = _safe_int(parts[0])
                watts = _safe_float(parts[1])
                amps = _safe_float(parts[2])
                volts = _safe_float(parts[3])
                if idx is not None and 1 <= idx <= MAX_OUTLETS:
                    self.register_child("outlet", idx)  # idempotent
                    updates: dict[str, Any] = {}
                    if watts is not None:
                        updates["power"] = watts
                    if amps is not None:
                        updates["current"] = amps
                    if volts is not None:
                        updates["voltage"] = volts
                    if updates:
                        self.set_child_state_batch("outlet", idx, updates)
        elif key == "PowerStatus":
            parts = [p.strip() for p in value.split(",")]
            if len(parts) >= 4:
                amps = _safe_float(parts[0])
                watts = _safe_float(parts[1])
                volts = _safe_float(parts[2])
                safe = _safe_int(parts[3])
                updates = {}
                if amps is not None:
                    updates["current"] = amps
                if watts is not None:
                    updates["power_watts"] = watts
                if volts is not None:
                    updates["voltage"] = volts
                if safe is not None:
                    updates["safe_voltage"] = safe == 1
                if updates:
                    self.set_states(updates)
        elif key == "AutoReboot":
            n = _safe_int(value)
            if n is not None:
                self.set_state("auto_reboot", n == 1)
        elif key == "UPSConnection":
            n = _safe_int(value)
            if n is not None:
                self.set_state("ups_connected", n == 1)
        elif key == "UPSStatus":
            self._dispatch_ups_status(value)
        elif key.startswith("Outlet") and "," in value:
            # Some firmware pushes per-outlet state changes as
            # ~Outlet=N,STATE — route that into child state too.
            parts = value.split(",")
            idx = _safe_int(parts[0])
            state_val = _safe_int(parts[1]) if len(parts) > 1 else None
            if (
                idx is not None
                and state_val is not None
                and 1 <= idx <= MAX_OUTLETS
            ):
                self.register_child("outlet", idx)  # idempotent
                self.set_child_state("outlet", idx, "state", state_val == 1)

    def _dispatch_outlet_count(self, value: str) -> None:
        """Reconcile the outlet child roster from the reported count.

        Register outlets 1..count; drop any outlet beyond it (idempotent if
        already absent), so reconnecting to a different unit, or a unit that
        reports fewer outlets, surfaces exactly the outlets it has.
        """
        count = _safe_int(value)
        if count is None:
            return
        count = max(0, min(MAX_OUTLETS, count))
        for n in range(1, MAX_OUTLETS + 1):
            if n <= count:
                self.register_child("outlet", n)  # idempotent
            else:
                self.deregister_child("outlet", n)  # idempotent if absent
        self.set_state("outlet_count", count)

    def _dispatch_ups_status(self, value: str) -> None:
        # Charge%, Load%, Health, PowerLost, Runtime, AlarmEnabled, AlarmMuted
        parts = [p.strip() for p in value.split(",")]
        if len(parts) < 7:
            return
        charge = _safe_int(parts[0])
        load = _safe_int(parts[1])
        health = parts[2] if parts[2] in ("Good", "Bad") else "Good"
        power_lost = parts[3].lower() == "true"
        runtime = _safe_int(parts[4])
        alarm_enabled = parts[5].lower() == "true"
        alarm_muted = parts[6].lower() == "true"

        updates: dict[str, Any] = {
            "ups_battery_health": health,
            "ups_power_lost": power_lost,
            "ups_alarm_enabled": alarm_enabled,
            "ups_alarm_muted": alarm_muted,
        }
        if charge is not None:
            updates["ups_battery_charge"] = charge
        if load is not None:
            updates["ups_battery_load"] = load
        if runtime is not None:
            updates["ups_battery_runtime"] = runtime
        self.set_states(updates)


def _safe_int(v: str) -> int | None:
    try:
        return int(v.strip())
    except (ValueError, AttributeError):
        return None


def _safe_float(v: str) -> float | None:
    try:
        return float(v.strip())
    except (ValueError, AttributeError):
        return None


def _split_brace_list(value: str) -> list[str]:
    """Parse `{Outlet 1},{Outlet 2},...` into ['Outlet 1', 'Outlet 2', ...]."""
    return re.findall(r"\{([^}]*)\}", value)
