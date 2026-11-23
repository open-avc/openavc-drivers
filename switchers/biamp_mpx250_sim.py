"""MPX 250 SSH shell simulator, carried over local TCP after sign-in.

JSON replies follow Biamp's public MPX 250 API Reference Guide. The guide
does not specify event printouts: this simulator sends the documented event
name alone as a synthetic refresh hint, not a captured hardware payload.
The driver reads port/config reports to establish state after that hint.
SSH negotiation and passwords are the platform transport's responsibility;
this simulator does not claim to qualify a physical MPX 250 SSH server.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

CLIENT: ContextVar[str] = ContextVar("mpx250_sim_client", default="")
EVENT_NAMES = ["EVENT_US_PORT_CONNECTION", "EVENT_SYSTEM_STATE_CHANGE",
               "EVENT_POWER_INFO", "EVENT_USB_PORT_CHANGE", "EVENT_DISPLAY_PORT_CHANGE"]
CONFIG_DEFAULTS = {
    "autoswitchconnect": "on", "autoswitchdisconnect": "active", "defaultport": 0,
    "lockactive": 0, "buttonemptydisabled": 0, "buttonoffdefaultport": 0,
    "disableleds": 0, "hdmiceccontrol": 0, "mtrbuttoncycle": 0,
    "mtrmodememory": 0, "mtrkeepmodeinactive": 0, "mtrmanualinherit": 0,
    "sharenetwork": 0,
}


class BiampMPX250Simulator(TCPSimulator):
    SIMULATOR_INFO = {
        "push_state": True,
        "driver_id": "biamp_mpx250",
        "name": "Biamp EasyConnect MPX 250 Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 22,
        "delimiter": "\r",
        "initial_state": {
            "active": 1, "mode": "BYOD", "firmware": "1.3.1.0",
            "input_1": True, "input_2": True, "input_3": False, "input_4": False,
            "rear_5": True, "rear_6": False, "rear_7": True, "rear_8": False,
        },
        "controls": [
            {"type": "slider", "key": "active", "label": "Selected Input (0 None)", "min": 0, "max": 4, "step": 1},
            {"type": "select", "key": "mode", "label": "Meeting Mode", "options": ["BYOD", "UC"]},
            {"type": "toggle", "key": "input_1", "label": "USB-C 1 Connected"},
            {"type": "toggle", "key": "input_2", "label": "USB-C 2 Connected"},
            {"type": "toggle", "key": "input_3", "label": "USB-C 3 Connected"},
            {"type": "toggle", "key": "input_4", "label": "USB-C 4 Connected"},
            {"type": "toggle", "key": "rear_5", "label": "Rear Port 5 Connected"},
            {"type": "toggle", "key": "rear_6", "label": "Rear Port 6 Connected"},
            {"type": "toggle", "key": "rear_7", "label": "Rear Port 7 Connected"},
            {"type": "toggle", "key": "rear_8", "label": "Rear Port 8 Connected"},
            {"type": "toggle", "key": "lockactive", "label": "Lock Active Input"},
        ],
        "error_modes": {
            "communication_timeout": {"behavior": "no_response", "description": "Stop answering requests"},
        },
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._revision = 0
        self._subscriptions: dict[str, set[str]] = {}
        self._pumps: set[asyncio.Task] = set()
        for key, value in CONFIG_DEFAULTS.items():
            self.set_state(key, value)

    def set_state(self, key: str, value: Any) -> None:
        changed = self.get_state(key) != value
        super().set_state(key, value)
        if changed:
            self._revision = getattr(self, "_revision", 0) + 1

    async def on_client_connected(self, client_id: str) -> bytes:
        CLIENT.set(client_id)
        self._subscriptions[client_id] = set()
        task = asyncio.create_task(self._push_changes(client_id))
        self._pumps.add(task)
        task.add_done_callback(self._pumps.discard)
        return b"MPX 250 simulator\r\n> "

    async def _push_changes(self, client_id: str) -> None:
        revision = self._revision
        try:
            while client_id in self._clients:
                await asyncio.sleep(0.1)
                subscribed = self._subscriptions.get(client_id, set())
                if revision != self._revision and subscribed:
                    # Synthetic name-only notification; see module docstring.
                    event = sorted(subscribed)[0]
                    await self.push_to(client_id, (event + "\r\n> ").encode())
                revision = self._revision
        finally:
            self._subscriptions.pop(client_id, None)

    async def stop(self) -> None:
        for task in self._pumps:
            task.cancel()
        if self._pumps:
            await asyncio.gather(*self._pumps, return_exceptions=True)
        self._pumps.clear()
        await super().stop()

    def _port_report(self) -> dict:
        state = self.state
        return {"active": int(state["active"]), "mode": state["mode"],
                "con_in": [int(bool(state[f"input_{i}"])) for i in range(1, 5)],
                "con_out": [int(bool(state[f"rear_{i}"])) for i in range(5, 9)]}

    def _config_report(self) -> dict:
        return {key: self.get_state(key) for key in CONFIG_DEFAULTS}

    def _system_report(self) -> dict:
        return {"sn": "MPX250-SIMULATOR", "hwId": "SIMULATOR000000",
                "product": "O-PC-4", "firmwareversion": self.get_state("firmware")}

    def _network_report(self) -> dict:
        if self.config.get("wifi"):
            return {"status": "UP", "interface": "WiFi", "ip": "192.0.2.25",
                    "ssid": "Simulation", "wifirssi": -55}
        return {"status": "UP", "interface": "Ethernet", "ip": "192.0.2.25",
                "subnet": "255.255.255.0", "gw": "192.0.2.1"}

    def _port_set(self, values: dict) -> dict:
        if self.config.get("refuse_writes"):
            return self._port_report()
        if "active" in values and not self.get_state("lockactive"):
            active = values["active"]
            if type(active) is int and 1 <= active <= 4:
                self.set_state("active", active)
        if values.get("mode") in ("UC", "BYOD"):
            self.set_state("mode", values["mode"])
        return self._port_report()

    def _config_set(self, values: dict) -> dict:
        if not self.config.get("refuse_writes"):
            for key, value in values.items():
                if key in CONFIG_DEFAULTS:
                    self.set_state(key, value)
        return self._config_report()

    def _events(self, action: str, values: dict) -> dict:
        subscribed = self._subscriptions.setdefault(CLIENT.get(), set())
        wanted = set(values.get("events", [])).intersection(EVENT_NAMES)
        if action == "subscribe":
            subscribed.update(wanted)
        if action == "unsubscribe":
            subscribed.difference_update(wanted)
        result = {"subscriptions": sorted(subscribed)}
        if action == "list":
            result["events"] = EVENT_NAMES
        return result

    def _dispatch(self, command: str, values: dict) -> dict | None:
        readers = {"system": self._system_report, "port get": self._port_report,
                   "config get": self._config_report, "network get": self._network_report}
        if command in readers:
            return readers[command]()
        writers = {"port set": self._port_set, "config set": self._config_set}
        if command in writers:
            return writers[command](values)
        if command in ("event list", "event subscribe", "event unsubscribe"):
            return self._events(command.split()[1], values)
        if command in ("reboot soft", "reboot hard"):
            for writer in self._clients.values():
                writer.close()
        return None

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("utf-8", errors="replace").strip()
        command, brace, body = text.partition("{")
        try:
            values = json.loads(brace + body) if brace else {}
        except ValueError:
            return None  # No documented error wire format to fabricate.
        response = self._dispatch(command.strip(), values)
        if response is None:
            return None
        echo = text + "\r\n" if self.config.get("echo", True) else ""
        indent = 2 if self.config.get("pretty_json") else None
        return (echo + json.dumps(response, indent=indent) + "\r\n> ").encode()
