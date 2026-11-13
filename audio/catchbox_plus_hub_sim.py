"""Catchbox Plus Hub API v2.1 UDP simulator. Each client owns change subscriptions."""

from __future__ import annotations

import json

from openavc.simulator.udp_simulator import UDPSimulator


def _reply(path, value, error=0):
    for part in reversed(path):
        value = {part: value}
    return json.dumps({**value, "error": error}, separators=(",", ":")).encode()


def _address(tree):
    path = []
    while isinstance(tree, dict) and len(tree) == 1:
        key, tree = next(iter(tree.items()))
        path.append(key)
    if isinstance(tree, dict) or not path:
        raise ValueError("Expected one API address")
    return tuple(path), tree


class CatchboxPlusHubSimulator(UDPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "catchbox_plus_hub",
        "name": "Catchbox Plus Hub Simulator",
        "category": "audio",
        "transport": "udp",
        "default_port": 39030,
        "initial_state": {
            "device_type": "quad",
            "name": "Meeting Hub",
            "firmware_info": "2.1",
            "serial": "SIMCB1001",
            "mic1_link_state": 1,
            "mic2_link_state": 3,
            "mic3_link_state": 0,
            "mic4_link_state": 2,
        },
        "controls": [
            {
                "type": "select",
                "key": "device_type",
                "label": "Hub Size",
                "options": ["dual", "quad"],
            },
            {
                "type": "select",
                "key": "mic1_link_state",
                "label": "Mic 1 Link (0 disconnected, 1 connected, 2 pairing, 3 charging)",
                "options": [0, 1, 2, 3],
            },
            {
                "type": "slider",
                "key": "tx1_battery",
                "label": "Mic 1 Battery (%)",
                "min": 0,
                "max": 100,
            },
            {"type": "toggle", "key": "mic1_mute", "label": "Mic 1 Input Muted"},
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._subscriptions = {}
        self._peer = None
        self._addresses = {
            ("rx", "device", k): k
            for k in ("device_type", "name", "firmware_info", "serial")
        }
        self._writable = set()
        self._slot_for = {}
        for output in ("usb", "3pin", "rca"):
            path = ("rx", "audio", "output", "mixout", output, "mute")
            self._bind(path, f"{output}_mute", 0, writable=True)
        for slot in range(1, 5):
            self._channel(slot)

    def _bind(self, path, key, value, writable=False):
        self._addresses[path] = key
        self._state.setdefault(key, value)
        if writable:
            self._writable.add(path)

    def _channel(self, slot):
        mic, tx = f"mic{slot}", f"tx{slot}"
        paths = {
            ("rx", "device", mic + "_link_state"): (mic + "_link_state", 0),
            ("rx", "audio", "input", mic, "mute"): (mic + "_mute", 0),
        }
        for leaf, value in {
            "battery": 80,
            "rssi": 55,
            "serial": f"SIM{slot:05}",
            "name": f"Mic{slot}",
        }.items():
            paths[(tx, "device", leaf)] = (tx + "_" + leaf, value)
        for path, (key, value) in paths.items():
            self._bind(path, key, value, writable=path[-1] == "mute")
            self._slot_for[path] = slot

    async def dispatch_datagram(self, data, addr):
        self._peer = addr
        try:
            await super().dispatch_datagram(data, addr)
        finally:
            self._peer = None

    def handle_command(self, data):
        try:
            body = json.loads(data)
            if "subscribe" in body:
                return self._subscription(body)
            path, value = _address(body)
            return self._access(path, value)
        except (AttributeError, IndexError, ValueError, KeyError, TypeError):
            return b'{"error":425}'

    def _subscription(self, body):
        entry = body["subscribe"][0].copy()
        control = entry.pop("#")
        path, _ = _address(entry)
        if path not in self._addresses or control["period_ms"] != 0:
            return _reply(path, None, 430)
        clients = self._subscriptions.setdefault(path, set())
        if control["enable"]:
            clients.add(self._peer)
        else:
            clients.discard(self._peer)
        return json.dumps({**body, "error": 0}).encode()

    def _access(self, path, value):
        key = self._addresses.get(path)
        slot = self._slot_for.get(path, 0)
        if key is None or slot > self._fitted_channels():
            return _reply(path, None, 430)
        if path[0].startswith("tx") and not self._linked(slot):
            return _reply(path, None, 415)
        if value is not None:
            return self._write(path, key, value)
        return _reply(path, self._wire_value(path, key))

    def _fitted_channels(self):
        return 2 if self.get_state("device_type") == "dual" else 4

    def _linked(self, slot):
        return self.get_state(f"mic{slot}_link_state") in (1, 3)

    def _write(self, path, key, value):
        if path not in self._writable or value not in (0, 1):
            return _reply(path, value, 405)
        self.set_state(key, value)
        return _reply(path, self._wire_value(path, key))

    def _wire_value(self, path, key):
        value = self.get_state(key)
        return int(value) if path in self._writable else value

    def set_state(self, key, value):
        changed = self.get_state(key) != value
        super().set_state(key, value)
        if changed:
            self._notify(key)

    def _notify(self, key):
        for path, clients in self._subscriptions.items():
            if self._addresses.get(path) != key:
                continue
            frame = self._access(path, None)
            for peer in clients:
                self.push_datagram(frame, peer)
