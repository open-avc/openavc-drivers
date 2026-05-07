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
    "<pass>\\n" → "Successfully Logged In!\\n" → command mode.
    Commands are ASCII text, '\\n'-terminated, prefixed by '?', '!', '#',
    or '~'. Source: SnapAV WattBox Integration Protocol v1.7 (rev 20190520).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

MAX_DECLARED_OUTLETS = 12

OUTLET_ACTIONS = {
    "off": "OFF",
    "on": "ON",
    "toggle": "TOGGLE",
    "reset": "RESET",
}

OUTLET_MODES = {"enabled": 0, "disabled": 1, "reset_only": 2}


def _build_state_vars() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
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
    for n in range(1, MAX_DECLARED_OUTLETS + 1):
        out[f"outlet_{n}_state"] = {
            "type": "boolean",
            "label": f"Outlet {n} State",
        }
        out[f"outlet_{n}_name"] = {
            "type": "string",
            "label": f"Outlet {n} Name",
        }
        out[f"outlet_{n}_power"] = {
            "type": "number",
            "label": f"Outlet {n} Power (W)",
        }
        out[f"outlet_{n}_current"] = {
            "type": "number",
            "label": f"Outlet {n} Current (A)",
        }
        out[f"outlet_{n}_voltage"] = {
            "type": "number",
            "label": f"Outlet {n} Voltage (V)",
        }
    return out


class WattBoxIPDriver(BaseDriver):
    """SnapAV WattBox IP-Controlled PDU driver."""

    DRIVER_INFO = {
        "id": "wattbox_ip",
        "name": "WattBox IP-Controlled PDU",
        "manufacturer": "WattBox",
        "category": "power",
        "version": "1.2.0",
        "author": "OpenAVC",
        "description": (
            "Controls SnapAV WattBox IP-controlled power distribution units "
            "over the WattBox Integration Protocol (Telnet, port 23). "
            "Supports outlet on/off/toggle/reset, per-outlet naming, "
            "auto-reboot configuration, system + per-outlet power metering, "
            "and UPS status reporting on units with a connected UPS."
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
            # the disallowed open_ports set; declarative probe could
            # use tcp_active_probe on 80 — deferred until we have a
            # real captured response to verify against. WattBox does
            # not expose a polled SNMP MIB (only outbound traps).
            #   Refs:
            #     SnapAV_Wattbox_API_V2.2.pdf (Telnet 23 / HTTP API)
            #     WattBox.WB10.API.v3.0.pdf (XML endpoint)
            "oui_prefixes": ["d4:6a:91", "14:3f:c3"],
            "vendor_aliases": [
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
                    "Integration Protocol. Pre-declared state variables "
                    "cover 12 outlets; 18-outlet models route correctly, "
                    "with outlets 13–18 surfacing as raw state keys."
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
                "5. Outlets are 1-indexed in commands. Outlet 0 with the "
                "reset action resets every outlet."
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
        "commands": {
            "outlet_on": {
                "label": "Turn Outlet On",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "help": "Outlet number (1-indexed).",
                    },
                },
                "help": "Turn a specific outlet on.",
            },
            "outlet_off": {
                "label": "Turn Outlet Off",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                    },
                },
                "help": "Turn a specific outlet off.",
            },
            "outlet_toggle": {
                "label": "Toggle Outlet",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                    },
                },
                "help": "Toggle a specific outlet.",
            },
            "outlet_reset": {
                "label": "Reset Outlet",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "help": (
                            "Outlet number. Use 0 to reset every outlet."
                        ),
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
                "help": (
                    "Reset (cycle) a specific outlet, or every outlet when "
                    "set to 0."
                ),
            },
            "set_outlet_name": {
                "label": "Rename Outlet",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                    },
                    "name": {"type": "string", "required": True},
                },
                "help": "Rename an outlet so it shows the right label in the WattBox UI.",
            },
            "set_outlet_power_on_delay": {
                "label": "Set Outlet Power-On Delay",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
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
                    "an outlet so connected gear comes up in sequence."
                ),
            },
            "set_outlet_mode": {
                "label": "Set Outlet Mode",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
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
                    "reset_only (rejects on / off, accepts reset)."
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
                    "out."
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
        super().__init__(device_id, config, state, events)

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 23))
        username = self.config.get("username", "wattbox")
        password = self.config.get("password", "wattbox")

        # Open a raw TCP connection — we need control over the read loop
        # during the login banner / username / password handshake, so we
        # use raw mode and parse lines ourselves.
        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=None,
            timeout=5.0,
            name=self.device_id,
        )

        # Best-effort auth handshake. Real WattBoxes send the prompts in a
        # predictable order; we don't strictly wait for each prompt because
        # banner timing varies between firmware versions. A short pause
        # gives the server time to flush "Please Login to Continue" +
        # "Username: " before we send our credentials.
        try:
            await asyncio.sleep(0.2)
            await self.transport.send(f"{username}\n".encode("utf-8"))
            await asyncio.sleep(0.2)
            await self.transport.send(f"{password}\n".encode("utf-8"))
        except (ConnectionError, OSError) as e:
            await self.transport.close()
            self.transport = None
            raise ConnectionError(
                f"WattBox auth handshake failed for {host}:{port}: {e}"
            ) from e

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

        poll_interval = int(self.config.get("poll_interval", 30))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self._authenticated = False
        self._line_buffer = b""
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

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
            outlet = int(params["outlet"])
            await self._send(f"!OutletSet={outlet},ON")
        elif command == "outlet_off":
            outlet = int(params["outlet"])
            await self._send(f"!OutletSet={outlet},OFF")
        elif command == "outlet_toggle":
            outlet = int(params["outlet"])
            await self._send(f"!OutletSet={outlet},TOGGLE")
        elif command == "outlet_reset":
            outlet = int(params["outlet"])
            delay = params.get("delay")
            if delay is not None:
                await self._send(f"!OutletSet={outlet},RESET,{int(delay)}")
            else:
                await self._send(f"!OutletSet={outlet},RESET")
        elif command == "set_outlet_name":
            outlet = int(params["outlet"])
            name = str(params["name"])
            await self._send(f"!OutletNameSet={outlet},{name}")
        elif command == "set_outlet_power_on_delay":
            outlet = int(params["outlet"])
            delay = int(params["delay"])
            await self._send(f"!OutletPowerOnDelaySet={outlet},{delay}")
        elif command == "set_outlet_mode":
            outlet = int(params["outlet"])
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
        while b"\n" in self._line_buffer:
            line_bytes, self._line_buffer = self._line_buffer.split(b"\n", 1)
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        # Banner / login prompts — drop. The auth handshake is fire-and-forget.
        if line.startswith("Please Login") or line.startswith("Username:"):
            return
        if line.startswith("Password:") or line.startswith("Successfully"):
            self._authenticated = True
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
        if key == "Firmware":
            self.set_state("firmware", value)
        elif key == "Hostname":
            self.set_state("hostname", value)
        elif key == "Serial":
            self.set_state("serial", value)
        elif key == "Model":
            self.set_state("model", value)
        elif key == "OutletCount":
            n = _safe_int(value)
            if n is not None:
                self.set_state("outlet_count", n)
        elif key == "OutletStatus":
            for i, raw in enumerate(value.split(","), start=1):
                state_val = _safe_int(raw)
                if state_val is not None:
                    self.set_state(f"outlet_{i}_state", state_val == 1)
        elif key == "OutletName":
            for i, name in enumerate(_split_brace_list(value), start=1):
                self.set_state(f"outlet_{i}_name", name)
        elif key == "OutletPowerStatus":
            parts = [p.strip() for p in value.split(",")]
            if len(parts) >= 4:
                idx = _safe_int(parts[0])
                watts = _safe_float(parts[1])
                amps = _safe_float(parts[2])
                volts = _safe_float(parts[3])
                if idx is not None:
                    if watts is not None:
                        self.set_state(f"outlet_{idx}_power", watts)
                    if amps is not None:
                        self.set_state(f"outlet_{idx}_current", amps)
                    if volts is not None:
                        self.set_state(f"outlet_{idx}_voltage", volts)
        elif key == "PowerStatus":
            parts = [p.strip() for p in value.split(",")]
            if len(parts) >= 4:
                amps = _safe_float(parts[0])
                watts = _safe_float(parts[1])
                volts = _safe_float(parts[2])
                safe = _safe_int(parts[3])
                updates: dict[str, Any] = {}
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
            # ~Outlet=N,STATE — accept that shape too.
            parts = value.split(",")
            idx = _safe_int(parts[0])
            state_val = _safe_int(parts[1]) if len(parts) > 1 else None
            if idx is not None and state_val is not None:
                self.set_state(f"outlet_{idx}_state", state_val == 1)

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
