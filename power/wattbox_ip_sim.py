"""
WattBox IP-Controlled PDU — Simulator.

Implements the WattBox Integration Protocol v1.7 server side:

  - Sends "Please Login to Continue" / "Username: " / "Password: " /
    "Successfully Logged In!" handshake when a client connects.
  - Accepts ?Query and !Set commands, replies in matching shape
    (?Field=value\\n for queries, OK\\n for sets, #Error\\n for parse
    failures).
  - Models a configurable outlet count (default 12), per-outlet state +
    name + power telemetry, system metering, and a UPS that defaults
    to "not connected".
"""

from __future__ import annotations

import logging

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


DEFAULT_OUTLETS = 12

# Sent the moment a client connects, before any command. Partial line (no
# trailing newline on the "Username: " prompt) on purpose — mirrors the real
# WattBox banner.
CONNECT_BANNER = b"Please Login to Continue\nUsername: "


class WattBoxIPSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "wattbox_ip",
        "name": "WattBox IP-Controlled PDU Simulator",
        "category": "power",
        "transport": "tcp",
        "default_port": 23,
        # Lines end in \n; TCPSimulator's flexible line mode handles \r\n,
        # \r, or \n indistinguishably and strips the terminator before
        # calling handle_command().
        "delimiter": "\n",
        "initial_state": {
            "model": "WB-700-IPV-12",
            "hostname": "Wattbox",
            "serial": "12345678",
            "firmware": "1.0.0.0",
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "hostname", "label": "Hostname"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
        ],
        "delays": {
            "command_response": 0.005,
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        n_outlets = int(self.config.get("outlets", DEFAULT_OUTLETS))
        self._n_outlets = n_outlets

        self._outlet_state: list[bool] = [True] * n_outlets
        self._outlet_name: list[str] = [
            f"Outlet {i + 1}" for i in range(n_outlets)
        ]
        self._outlet_mode: list[int] = [0] * n_outlets  # enabled
        self._outlet_power_on_delay: list[int] = [1] * n_outlets

        self._auto_reboot = True
        self._ups_connected = False

        # Track per-client auth state so we know whether to expect a
        # username, then a password, then commands.
        self._auth_state: dict[str, int] = {}

    # ── Connect: send banner + Username prompt ──

    def connect_banner(self) -> bytes:
        """The banner sent immediately on connect (sync helper for harnesses
        that deliver server-initiated data outside the request/response path).
        """
        return CONNECT_BANNER

    async def on_client_connected(self, client_id: str) -> bytes | None:
        self._auth_state[client_id] = 0  # 0 = waiting for username
        return CONNECT_BANNER

    # ── Per-line handling ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", errors="replace").strip()

        # We don't get client_id from the framework signature, so use a
        # single shared auth slot. The test harness uses one client.
        cid = "_shared"
        state = self._auth_state.setdefault(cid, 0)

        if state == 0:
            # Username arrived. Reply with the password prompt.
            self._auth_state[cid] = 1
            return b"Password: "
        if state == 1:
            # Password arrived. On a real unit, bad credentials get the login
            # prompt again instead of a success banner; `reject_auth` models
            # that so the driver's auth-fault path can be tested.
            if self.config.get("reject_auth"):
                self._auth_state[cid] = 0
                return CONNECT_BANNER
            self._auth_state[cid] = 2
            return b"Successfully Logged In!\n"

        if not line:
            return None

        # Authenticated; parse command.
        if line.startswith("?"):
            return self._handle_query(line[1:])
        if line.startswith("!"):
            return self._handle_set(line[1:])
        return b"#Error\n"

    # ── Queries ──

    def _handle_query(self, body: str) -> bytes:
        # Some queries take parameters: e.g. ?OutletPowerStatus=N
        if "=" in body:
            key, _, arg = body.partition("=")
            return self._handle_param_query(key, arg)
        return self._handle_simple_query(body)

    def _handle_simple_query(self, key: str) -> bytes:
        if key == "Firmware":
            return f"?Firmware={self.state.get('firmware', '1.0.0.0')}\n".encode()
        if key == "Hostname":
            return f"?Hostname={self.state.get('hostname', 'Wattbox')}\n".encode()
        if key == "Serial":
            return f"?Serial={self.state.get('serial', '12345678')}\n".encode()
        if key == "Model":
            return f"?Model={self.state.get('model', 'WB-700-IPV-12')}\n".encode()
        if key == "OutletCount":
            return f"?OutletCount={self._n_outlets}\n".encode()
        if key == "OutletStatus":
            states = ",".join("1" if s else "0" for s in self._outlet_state)
            return f"?OutletStatus={states}\n".encode()
        if key == "OutletName":
            names = ",".join(f"{{{n}}}" for n in self._outlet_name)
            return f"?OutletName={names}\n".encode()
        if key == "PowerStatus":
            # current(A), power(W), voltage(V), safe(0/1)
            return b"?PowerStatus=2.50,275.00,120.00,1\n"
        if key == "AutoReboot":
            return f"?AutoReboot={1 if self._auto_reboot else 0}\n".encode()
        if key == "UPSConnection":
            return f"?UPSConnection={1 if self._ups_connected else 0}\n".encode()
        if key == "UPSStatus":
            if not self._ups_connected:
                return b"#Error\n"
            return b"?UPSStatus=85,15,Good,False,42,True,False\n"
        return b"#Error\n"

    def _handle_param_query(self, key: str, arg: str) -> bytes:
        if key == "OutletPowerStatus":
            try:
                n = int(arg)
            except ValueError:
                return b"#Error\n"
            if not (1 <= n <= self._n_outlets):
                return b"#Error\n"
            on = self._outlet_state[n - 1]
            watts = round(50.0 + n * 5.0, 2) if on else 0.0
            amps = round(watts / 120.0, 2) if on else 0.0
            return f"?OutletPowerStatus={n},{watts:.2f},{amps:.2f},120.00\n".encode()
        return b"#Error\n"

    # ── Sets ──

    def _handle_set(self, body: str) -> bytes:
        if "=" not in body:
            return b"#Error\n"
        key, _, value = body.partition("=")
        parts = [p.strip() for p in value.split(",")]

        if key == "OutletSet":
            return self._handle_outlet_set(parts)
        if key == "OutletNameSet":
            if len(parts) < 2:
                return b"#Error\n"
            try:
                n = int(parts[0])
            except ValueError:
                return b"#Error\n"
            if not (1 <= n <= self._n_outlets):
                return b"#Error\n"
            # Name may contain commas/spaces; everything after the first
            # comma is the new name.
            idx = body.index(",", body.index("="))
            self._outlet_name[n - 1] = body[idx + 1 :]
            return b"OK\n"
        if key == "OutletPowerOnDelaySet":
            if len(parts) != 2:
                return b"#Error\n"
            try:
                n, delay = int(parts[0]), int(parts[1])
            except ValueError:
                return b"#Error\n"
            if not (1 <= n <= self._n_outlets) or not (1 <= delay <= 600):
                return b"#Error\n"
            self._outlet_power_on_delay[n - 1] = delay
            return b"OK\n"
        if key == "OutletModeSet":
            if len(parts) != 2:
                return b"#Error\n"
            try:
                n, mode = int(parts[0]), int(parts[1])
            except ValueError:
                return b"#Error\n"
            if not (1 <= n <= self._n_outlets) or mode not in (0, 1, 2):
                return b"#Error\n"
            self._outlet_mode[n - 1] = mode
            return b"OK\n"
        if key == "AutoReboot":
            if value not in ("0", "1"):
                return b"#Error\n"
            self._auto_reboot = value == "1"
            return b"OK\n"
        if key == "Reboot":
            # Spec: respond OK then drop. Sim just acks; the harness can
            # disconnect explicitly if it wants to model the drop.
            return b"OK\n"
        return b"#Error\n"

    def _handle_outlet_set(self, parts: list[str]) -> bytes:
        if len(parts) < 2:
            return b"#Error\n"
        try:
            n = int(parts[0])
        except ValueError:
            return b"#Error\n"
        action = parts[1].upper()
        # Outlet 0 with RESET = reset every outlet. Other 0 outlet uses
        # are invalid.
        if n == 0 and action != "RESET":
            return b"#Error\n"
        if n != 0 and not (1 <= n <= self._n_outlets):
            return b"#Error\n"

        if action == "ON":
            self._outlet_state[n - 1] = True
        elif action == "OFF":
            self._outlet_state[n - 1] = False
        elif action == "TOGGLE":
            self._outlet_state[n - 1] = not self._outlet_state[n - 1]
        elif action == "RESET":
            # RESET = brief off then on. Sim keeps state at "on" since
            # the cycle is too short to model meaningfully without the
            # client polling at sub-second intervals.
            if n == 0:
                self._outlet_state = [True] * self._n_outlets
            else:
                self._outlet_state[n - 1] = True
        else:
            return b"#Error\n"

        return b"OK\n"
