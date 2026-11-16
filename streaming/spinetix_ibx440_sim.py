"""Direct RPC simulator for the SpinetiX iBX440, including sparse display replies."""

import base64
import json
import time

from openavc.simulator.http_simulator import HTTPSimulator


class SpinetixIBX440Simulator(HTTPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "spinetix_ibx440", "name": "SpinetiX iBX440 Simulator",
        "category": "streaming", "transport": "http", "default_port": 443,
        "initial_state": {
            "systems": True, "allow_variables": True, "output_2_connected": True,
            "output_1_power": "unknown", "room_mode": "Welcome", "temperature": 38,
            "project_handle": "", "restart_pending": False,
        },
        "controls": [
            {"type": "toggle", "key": "systems", "label": "SYSTEMS Feature Set"},
            {"type": "toggle", "key": "allow_variables", "label": "Allow Shared Variables"},
            {"type": "toggle", "key": "output_2_connected", "label": "HDMI 2 Cable Connected"},
            {"type": "select", "key": "output_1_power", "label": "HDMI 1 Display Power",
             "options": ["on", "off", "unknown", "gone"]},
            {"type": "select", "key": "room_mode", "label": "Room Mode",
             "options": ["Welcome", "Event", "Closed"]},
            {"type": "slider", "key": "temperature", "label": "Player Temperature", "min": 20, "max": 85},
        ],
    }

    def __init__(self, device_id, config=None):
        settings = dict(config or {})
        settings.setdefault("tls", settings.get("ssl", True))
        super().__init__(device_id, settings)
        self.variables = {"Room Mode": "Welcome", "Lobby/Message": "Welcome to the lobby"}
        self.timestamps = {name: "0" for name in self.variables}
        self._last_room_mode = "Welcome"
        self._handles = 0

    def _info(self, flags):
        info = {"name": "Lobby Signage", "model": self.config.get("model", "iBX440"),
                "serial": "SIM440", "mode": "normal",
                "firmware": {"version": "4.9.4", "status": "normal"}}
        ports, displays = self._outputs()
        extra = {
            "videoConnectors": ports, "display": list(reversed(displays)),
            "license": {"features": [{"name": "_SYSTEMS", "valid": self.state["systems"]}]},
            "temperature": {"C": self.state["temperature"], "alarm": "none"},
            "storage": [{"type": "content", "free": "10832", "unit": "Mbytes"}],
            "reason": "rpc", "errors": [],
        }
        info.update({key: item for key, item in extra.items() if flags.get(key) or flags.get("all")})
        return info

    def _outputs(self):
        ports = []
        displays = []
        for index in range(1, 5):
            key = f"card0-HDMI-A-{index}"
            connected = index == 1 or (index == 2 and self.state["output_2_connected"])
            ports.append({"id": key, "type": "HDMI-A", "name": f"HDMI {index}",
                          "status": "connected" if connected else "disconnected", "enabled": True})
            if connected:
                displays.append({"id": key, "type": "HDMI", "power_save": "off",
                                 "power": self.state["output_1_power"] if index == 1 else "on",
                                 "mode": {"width": 3840 if index == 1 else 1920,
                                          "height": 2160 if index == 1 else 1080,
                                          "refresh": 59.94, "colorspace": "YCbCr 4:2:0"}})
        return ports, displays

    def _variable_result(self, method, params):
        if not self.state["systems"] or not self.state["allow_variables"]:
            raise ValueError("Web Storage is unavailable.")
        self._sync_room_mode()
        if method == "webstorage_list":
            return list(self.variables)
        operation = {"webstorage_get": self._value_row, "webstorage_cmpxchg": self._compare_and_set}
        apply = operation[method]
        return [apply(item) for item in reversed(params[0])]

    def _sync_room_mode(self):
        room_mode = self.state["room_mode"]
        if room_mode != self._last_room_mode:
            self.variables["Room Mode"] = self._last_room_mode = room_mode
            self.timestamps["Room Mode"] = str(int(time.time() * 1000))

    def _value_row(self, name):
        if name not in self.variables:
            return {"name": name, "success": False}
        return {"name": name, "success": True, "value": self.variables[name],
                "timestamp": self.timestamps.get(name, "0")}

    def _compare_and_set(self, change):
        name = change["name"]
        if self.variables.get(name) != change["expect"]:
            return {"name": name, "success": False}
        value = change["value"]
        self.variables[name] = value
        self.timestamps[name] = str(int(time.time() * 1000))
        if name == "Room Mode":
            self.set_state("room_mode", value)
            self._last_room_mode = value
        return self._value_row(name)

    def _dispatch(self, method, params):
        if method == "get_info":
            return self._info(params[0] if params else {})
        if method.startswith("webstorage_"):
            return self._variable_result(method, params)
        if method == "restart":
            return self._restart()
        if method == "add_pull_action" and params[0]["type"] == "publish":
            return self._queue_project()
        raise ValueError("No such method.")

    def _restart(self):
        accepted = not self.state["restart_pending"]
        self.set_state("restart_pending", True)
        return {"success": accepted, "reason": "" if accepted else "Already scheduled"}

    def _queue_project(self):
        self._handles += 1
        handle = str(self._handles)
        self.set_state("project_handle", handle)
        return handle

    def handle_request(self, method, path, headers, body):
        if path != "/rpc" or method != "POST":
            return 404, {}, {}
        if not self._authorized(headers):
            return 401, {}, {"WWW-Authenticate": 'Basic realm="SpinetiX"'}
        request = {}
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Expected an RPC object.")
            request = payload
            result = self._dispatch(request["method"], request["params"])
        except (TypeError, KeyError, IndexError, ValueError) as exc:
            return 200, {"id": request.get("id"),
                         "result": None, "error": str(exc)}, {}
        return 200, {"id": request["id"], "result": result, "error": None}, {}

    def _authorized(self, headers):
        if not self.config.get("password"):
            return True
        login = self.config.get("username", "admin") + ":" + self.config["password"]
        expected = "Basic " + base64.b64encode(login.encode()).decode()
        return headers.get("Authorization", headers.get("authorization")) == expected
