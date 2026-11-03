"""
WattBox IP-Controlled PDU — Simulator.

Implements the WattBox Integration Protocol v2.4 server side, as a
WB-250-IPW-2 on firmware 2.9.0.2 answered it:

  - Sends "Please Login to Continue" / "Username: " / "Password: " /
    "Successfully Logged In!" handshake when a client connects; a refused
    login (`reject_auth`) answers "Invalid Login" and prompts again.
  - Accepts ?Query and !Set commands, replies in matching shape
    (?Field=value\\n for queries, OK\\n for sets, #Error\\n for anything the
    unit does not answer).
  - Sends "~OutletStatus=..." to every client when an outlet changes.
  - Models a configurable model (`model`, default WB-800-IPVM-12) and outlet
    count (`outlets`). A WB-150 / WB-250 has 2 outlets, no power metering
    and no UPS link, so those queries answer #Error, as the v2.4 document
    says and the WB-250-IPW-2 did.
"""

from __future__ import annotations

import asyncio
import logging
import re

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


DEFAULT_MODEL = "WB-800-IPVM-12"
DEFAULT_OUTLETS = 12

# Sent the moment a client connects, before any command. Partial line (no
# trailing newline on the "Username: " prompt) on purpose — mirrors the real
# WattBox banner.
CONNECT_BANNER = b"Please Login to Continue\nUsername: "

# The WB-250-IPW-2 sent "OK" about 45 ms before the "~OutletStatus" line.
PUSH_AFTER_S = 0.05

MODE_RESET_ONLY = 2


def _measures_power(model: str) -> bool:
    # v2.4: ?PowerStatus and ?OutletPowerStatus "Not supported on WB150/250";
    # the WattBox comparison sheet gives those two series no UPS link either.
    return not model.upper().startswith(("WB-150", "WB-250"))


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
            "model": DEFAULT_MODEL,
            "hostname": "WattBox",
            "service_tag": "ST191500681E8422",
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
        model = str(self.config.get("model") or DEFAULT_MODEL)
        if model != DEFAULT_MODEL:
            self.set_state("model", model)
        self._metered = _measures_power(model)
        default_outlets = DEFAULT_OUTLETS if self._metered else 2
        n_outlets = int(self.config.get("outlets", default_outlets))
        self._n_outlets = n_outlets

        self._outlet_state: list[bool] = [True] * n_outlets
        self._outlet_name: list[str] = [
            f"Outlet {i + 1}" for i in range(n_outlets)
        ]
        self._outlet_mode: list[int] = [0] * n_outlets  # enabled
        self._outlet_power_on_delay: list[int] = [1] * n_outlets

        self._auto_reboot = True
        self._ups_connected = False

        # Pending outlet-status pushes, kept so they are not collected.
        self._tasks: set[asyncio.Task] = set()

    # ── Connect: send banner + Username prompt ──

    def connect_banner(self) -> bytes:
        """The banner sent immediately on connect (sync helper for harnesses
        that deliver server-initiated data outside the request/response path).
        """
        return CONNECT_BANNER

    async def on_client_connected(self, client_id: str) -> bytes | None:
        return CONNECT_BANNER

    async def authenticate_client(self, reader, writer, client_id) -> bool:
        """A username, then a password, then commands, on each connection.

        Read from the socket here, not in handle_command: the base drops an
        empty line before handle_command sees it, so a blank password would
        never be answered; and sign-in belongs to the connection, so a driver
        that reconnects is asked again. A refused login (`reject_auth`)
        answers "Invalid Login" and the username prompt again, as the unit
        does, and waits for the next attempt.
        """
        async def emit(data: bytes) -> None:
            writer.write(data)
            await writer.drain()
            self.log_protocol("out", data, client_id)

        async def credential() -> bool:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=30.0)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                return False
            return bool(raw)

        while True:
            if not await credential():
                return False
            await emit(b"Password: ")
            if not await credential():
                return False
            if self.config.get("reject_auth"):
                await emit(b"Invalid Login\nUsername: ")
                continue
            await emit(b"Successfully Logged In!\n")
            return True

    # ── Per-line handling ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", errors="replace").strip()

        if not line:
            return None

        # Authenticated; parse command.
        if line.startswith("?"):
            return self._handle_query(line[1:])
        if line == "!Exit":
            return b"OK\n"
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

    def _outlet_status(self, prefix: str) -> bytes:
        states = ",".join("1" if s else "0" for s in self._outlet_state)
        return f"{prefix}OutletStatus={states}\n".encode()

    def _handle_simple_query(self, key: str) -> bytes:
        if key == "Firmware":
            return f"?Firmware={self.state.get('firmware', '1.0.0.0')}\n".encode()
        if key == "Hostname":
            return f"?Hostname={self.state.get('hostname', 'WattBox')}\n".encode()
        if key == "ServiceTag":
            tag = self.state.get("service_tag", "ST191500681E8422")
            return f"?ServiceTag={tag}\n".encode()
        if key == "Model":
            return f"?Model={self.state.get('model', DEFAULT_MODEL)}\n".encode()
        if key == "OutletCount":
            return f"?OutletCount={self._n_outlets}\n".encode()
        if key == "OutletStatus":
            return self._outlet_status("?")
        if key == "OutletName":
            names = ",".join(f"{{{n}}}" for n in self._outlet_name)
            return f"?OutletName={names}\n".encode()
        if key == "AutoReboot":
            return f"?AutoReboot={1 if self._auto_reboot else 0}\n".encode()
        if not self._metered:
            # ?PowerStatus, ?UPSConnection, ?UPSStatus (and ?Serial below).
            return b"#Error\n"
        if key == "PowerStatus":
            # current(A), power(W), voltage(V), safe(0/1)
            return b"?PowerStatus=2.50,275.00,120.00,1\n"
        if key == "UPSConnection":
            return f"?UPSConnection={1 if self._ups_connected else 0}\n".encode()
        if key == "UPSStatus":
            if not self._ups_connected:
                return b"#Error\n"
            return b"?UPSStatus=85,15,Good,False,42,True,False\n"
        # ?Serial: v2.4 replaced it with ?ServiceTag, and firmware 2.9.0.2
        # answers it #Error.
        return b"#Error\n"

    def _handle_param_query(self, key: str, arg: str) -> bytes:
        if key == "OutletPowerStatus" and self._metered:
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
            return b"OK\n" if body == "Reboot" else b"#Error\n"
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
            # The WB-250-IPW-2 (firmware 2.9.0.2) keeps only the text before
            # the first space: "Bench Test" -> "Bench", "{Bench Test}" ->
            # "{Bench", "Comma, here" -> "Comma,".
            raw = value.split(",", 1)[1]
            self._outlet_name[n - 1] = raw.split(" ", 1)[0]
            return b"OK\n"
        if key == "OutletNameSetAll":
            # Kept whole, spaces and commas included; a shorter list leaves
            # the outlets after it as they were.
            names = re.findall(r"\{([^}]*)\}", value)
            if not names:
                return b"#Error\n"
            for i, name in enumerate(names[: self._n_outlets]):
                self._outlet_name[i] = name
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
        if action not in ("ON", "OFF", "TOGGLE", "RESET"):
            return b"#Error\n"

        if action == "RESET":
            outlets = range(self._n_outlets) if n == 0 else [n - 1]
            try:
                delay = int(parts[2]) if len(parts) > 2 else None
            except ValueError:
                return b"#Error\n"
            for i in outlets:
                self._outlet_state[i] = False
            self._announce()
            back_after = delay or max(self._outlet_power_on_delay[i] for i in outlets)
            self._announce(after=back_after, turn_on=list(outlets))
            return b"OK\n"

        i = n - 1
        if self._outlet_mode[i] == MODE_RESET_ONLY:
            # Measured: OK, and the outlet stays as it was.
            return b"OK\n"
        before = self._outlet_state[i]
        if action == "ON":
            self._outlet_state[i] = True
        elif action == "OFF":
            self._outlet_state[i] = False
        else:
            self._outlet_state[i] = not before
        if self._outlet_state[i] != before:
            self._announce()
        return b"OK\n"

    # ── Push ──

    def _announce(self, after: float = 0.0, turn_on: list[int] | None = None) -> None:
        """Send ~OutletStatus to every client a moment after the reply, as
        the unit does; with turn_on, power those outlets back first (the end
        of a reset)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _send() -> None:
            await asyncio.sleep(max(after, PUSH_AFTER_S))
            if turn_on:
                for i in turn_on:
                    self._outlet_state[i] = True
            await self.push(self._outlet_status("~"))

        task = loop.create_task(_send())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
