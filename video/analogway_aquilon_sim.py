"""Aquilon AWJ simulator from the manufacturer's public 6.2 programmer guide.

The protocol framing, documented properties, bank mapping and write/update
sequence follow that guide. Sparse rosters, names, instantaneous TAKE and memory
contents are illustrative. They are not captures or hardware timing evidence.
Each TCP connection has independent subscriptions, initially empty.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import re

from openavc.simulator.tcp_simulator import TCPSimulator

_CLIENT = ContextVar("aquilon_client", default="direct")
ROOT = "DeviceObject/"
MODEL = ROOT + "system/$device/@items/1/@props/dev"


class AnalogwayAquilonSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "analogway_aquilon",
        "name": "Analog Way Aquilon",
        "category": "video",
        "transport": "tcp",
        "default_port": 10606,
        "delimiter": "\x04",
        "initial_state": {
            "input_8_available": True,
            "transition_up": False,
            "alternate_configuration": False,
        },
        "controls": [
            {
                "type": "toggle",
                "key": "input_8_available",
                "label": "Input 8 Available",
            },
            {"type": "toggle", "key": "transition_up", "label": "Screen 1 T-Bar Up"},
            {
                "type": "toggle",
                "key": "alternate_configuration",
                "label": "Alternate Configuration (fewer objects)",
            },
        ],
        "error_modes": {
            "communication_timeout": {
                "behavior": "no_response",
                "description": "Aquilon stops answering",
            },
            "write_rejected": {
                "behavior": "custom",
                "description": "Aquilon refuses control writes",
            },
            "ignore_writes": {
                "behavior": "custom",
                "description": "Control writes leave reported state unchanged",
            },
            "transitioning": {
                "behavior": "custom",
                "description": "Screen 1 is between Program and Preview",
            },
        },
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._subscriptions = {}
        self._pending = {}
        self._push_jobs = set()
        self._seed()

    def _seed(self):
        self.set_state(MODEL, self.config.get("model", "NLC_CMINI"))
        self.set_state(
            ROOT + "system/$device/@items/1/serial/@props/serialNumber", "SIM0001"
        )
        self.set_state(
            ROOT + "system/$device/@items/1/version/@props/updater", "6.2.79"
        )
        for sid, name in (
            ("S1", "Main Screen"),
            ("S3", "Stage Screen"),
            ("A2", "Confidence"),
        ):
            self._seed_screen(sid, name)
        for slot in (2, 7):
            base = (
                ROOT
                + f"preconfig/backgrounds/$screen/@items/S1/$backgroundSet/@items/{slot}/$output/@items/3"
            )
            self.set_state(base + "/control/@props/content", "STILL_12")
            self.set_state(base + "/status/@props/isContentValid", True)
        self.set_state(
            ROOT + "preconfig/resources/new/$screen/@items/S2/control/@props/mode",
            "DISABLED",
        )
        for number, name in ((2, "Presentation"), (8, "Camera")):
            self._seed_input(number, name)
        for branch in ("masterPresetBank", "presetBank"):
            for slot, name in ((1, "Welcome"), (7, "Presentation"), (42, "")):
                self.set_state(
                    ROOT + f"{branch}/$bank/@items/{slot}/control/@props/label", name
                )
        for mode in ("PROGRAM", "PREVIEW"):
            self.set_state(
                ROOT
                + f"masterPresetBank/status/lastUsed/$presetMode/@items/{mode}/@props/memoryId",
                1,
            )
        for suffix, value in (
            ("status/@props/isUsed", True),
            ("status/@props/isAvailable", True),
            ("control/@props/source", 1),
            ("control/@props/label", "Holding Slide"),
        ):
            self.set_state(ROOT + "$still/@items/12/" + suffix, value)
        self.set_state(
            ROOT + "$still/library/$bank/@items/1/status/@props/capability", "4K"
        )
        self._seed_multiviewer()

    def _seed_screen(self, sid, name):
        branch = "$screen" if sid.startswith("S") else "$auxiliary"
        base = ROOT + f"{branch}/@items/{sid}"
        self.set_state(base + "/control/@props/label", name)
        self.set_state(
            ROOT + f"$screenAuxGroup/@items/{sid}/status/@props/transition", "AT_DOWN"
        )
        if sid.startswith("S"):
            self.set_state(
                ROOT
                + f"preconfig/resources/new/$screen/@items/{sid}/control/@props/mode",
                "FREESTYLE",
            )
        for layer in ("1", "3", "NATIVE"):
            self.set_state(base + f"/$layer/@items/{layer}/control/@props/freeze", [])
            if sid.startswith("S") and layer != "NATIVE":
                self.set_state(
                    ROOT
                    + f"preconfig/resources/new/$screen/@items/{sid}/$layer/@items/{layer}/control/@props/capability",
                    "4K",
                )
            for bank, source in (("A", "LIVE_2"), ("B", "LIVE_8")):
                self.set_state(
                    base
                    + f"/$preset/@items/{bank}/$layer/@items/{layer}/source/@props/inputNum",
                    source if layer != "NATIVE" else "NONE",
                )
        for bank in ("A", "B"):
            self.set_state(
                ROOT
                + f"presetBank/status/presetId/{branch}/@items/{sid}/$preset/@items/{bank}/@props/id",
                1,
            )

    def _seed_input(self, number, name):
        base = ROOT + f"$input/@items/IN_{number}"
        for suffix, value in (
            ("control/@props/label", name),
            ("control/@props/freeze", False),
            ("status/@props/isEnabled", True),
            ("status/@props/isAvailable", True),
            ("status/@props/capability", "4K"),
        ):
            self.set_state(base + "/" + suffix, value)

    def _seed_multiviewer(self):
        base = ROOT + "$monitoring/@items/2"
        self.set_state(base + "/status/@props/isEnabled", True)
        self.set_state(base + "/control/@props/label", "Operator Monitor")
        for widget, source in ((0, "PROGRAM_S1"), (5, "PREVIEW_S1")):
            prefix = base + f"/layout/$widget/@items/{widget}"
            self.set_state(prefix + "/status/@props/isEnabled", True)
            self.set_state(prefix + "/control/@props/source", source)

    async def on_client_connected(self, client_id):
        # This hook runs inside the connection task. ContextVar preserves the
        # correct client even when another client's response is delayed.
        _CLIENT.set(client_id)
        live = set(self._clients)
        self._subscriptions = {
            key: value for key, value in self._subscriptions.items() if key in live
        }
        self._pending = {
            key: value for key, value in self._pending.items() if key in live
        }
        self._subscriptions[client_id] = []
        self._pending[client_id] = {}

    async def stop(self):
        for job in self._push_jobs:
            job.cancel()
        await asyncio.gather(*self._push_jobs, return_exceptions=True)
        self._push_jobs.clear()
        self._subscriptions.clear()
        self._pending.clear()
        await super().stop()

    def set_state(self, key, value):
        old = self.get_state(key)
        super().set_state(key, value)
        if key == "input_8_available":
            self.set_state(ROOT + "$input/@items/IN_8/status/@props/isAvailable", value)
        elif key == "transition_up":
            self.set_state(
                ROOT + "$screenAuxGroup/@items/S1/status/@props/transition",
                "AT_UP" if value else "AT_DOWN",
            )
        if old == value:
            return
        if key.startswith(ROOT) and self.running:
            self._announce(key, value)

    def _announce(self, path, value):
        for client, subscriptions in self._subscriptions.items():
            if client in self._clients and any(
                path.startswith(prefix) for prefix in subscriptions
            ):
                job = asyncio.create_task(self.push_to(client, _reply(path, value)))
                self._push_jobs.add(job)
                job.add_done_callback(self._push_jobs.discard)

    def _visible(self):
        values = {
            key: value for key, value in self.state.items() if key.startswith(ROOT)
        }
        if self.get_state("alternate_configuration"):
            omitted = ("/@items/S3/", "/@items/IN_8/", "/$bank/@items/42/")
            values = {
                key: value
                for key, value in values.items()
                if not any(part in key for part in omitted)
            }
        if "transitioning" in self.active_errors:
            values[ROOT + "$screenAuxGroup/@items/S1/status/@props/transition"] = (
                "GOING_UP"
            )
        return values

    def handle_command(self, data):
        try:
            message = json.loads(data.rstrip(b"\x04"))
        except (ValueError, UnicodeDecodeError):
            return _error("E09", "Invalid JSON")
        if not isinstance(message, dict) or not isinstance(message.get("path"), str):
            return _error("E10", "Missing path")
        op, path = message.get("op"), message["path"]
        if op == "get":
            return self._read(path)
        if op != "replace":
            return _error("E11", "Unknown operation")
        if "value" not in message:
            return _error("E10", "Missing value")
        return self._write(path, message["value"])

    def _read(self, path):
        if path == "Subscriptions":
            return _reply(path, self._subscriptions.get(_CLIENT.get(), []))
        values = self._visible()
        if path in values:
            return _reply(path, values[path])
        descendants = {
            key[len(path) + 1 :]: value
            for key, value in values.items()
            if key.startswith(path + "/")
        }
        if not descendants:
            return _error("E12", "Unexpected path " + path)
        tree = {}
        for suffix, value in descendants.items():
            branch = tree
            parts = suffix.split("/")
            for part in parts[:-1]:
                branch = branch.setdefault(part, {})
            branch[parts[-1]] = value
        return _reply(path, tree)

    def _write(self, path, value):
        if path == "Subscriptions":
            return self._subscribe(value)
        if "write_rejected" in self.active_errors:
            return _error("E13", "Control write refused")
        if "ignore_writes" in self.active_errors:
            return None
        if path == ROOT + "$screenAuxGroup/control/@props/xUpdate":
            return self._apply_pending(value)
        if path.endswith("/control/@props/xTake"):
            return self._take(path, value)
        if "/control/load/" in path:
            return self._recall(path, value)
        return self._property_write(path, value)

    def _subscribe(self, value):
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            return _error("E13", "Subscriptions must be a string array")
        self._subscriptions[_CLIENT.get()] = list(value)
        return _reply("Subscriptions", value)

    def _property_write(self, path, value):
        if path not in self._visible():
            return _error("E12", "Unexpected path " + path)
        if path.endswith("/source/@props/inputNum"):
            if not isinstance(value, str) or not re.fullmatch(
                r"(?:LIVE|STILL|SCREEN|NATIVE)_[1-9]\d*|COLOR|NONE", value
            ):
                return _error("E13", "Invalid source")
            self._pending.setdefault(_CLIENT.get(), {})[path] = value
            return None
        if "/layout/$widget/" in path and path.endswith("/control/@props/source"):
            self.set_state(path, value)
            return None
        return _error("E13", "Property is read-only")

    def _apply_pending(self, value):
        if value is not True:
            return _error("E13", "Expected true")
        pending = self._pending.pop(_CLIENT.get(), {})
        for path, source in pending.items():
            self.set_state(path, source)
        return None

    def _take(self, path, value):
        status = path.replace("/control/@props/xTake", "/status/@props/transition")
        if value is not True or status not in self._visible():
            return _error("E13", "Screen cannot TAKE")
        before = self.get_state(status)
        self.set_state(status, "AT_UP" if before == "AT_DOWN" else "AT_DOWN")
        return None

    def _recall(self, path, value):
        if value is not True:
            return _error("E13", "Expected true")
        patterns = [
            (
                r"DeviceObject/masterPresetBank/control/load/\$slot/@items/(\d+)/\$preset/@items/(PROGRAM|PREVIEW)/@props/xRequest",
                self._master,
            ),
            (
                r"DeviceObject/presetBank/control/load/\$slot/@items/(\d+)/(\$screen|\$auxiliary)/@items/([SA]\d+)/\$preset/@items/(PROGRAM|PREVIEW)/@props/xRequest",
                self._screen_preset,
            ),
            (
                r"DeviceObject/monitoringBank/control/load/\$slot/@items/(\d+)/\$output/@items/(\d+)/@props/xRequest",
                self._multiviewer,
            ),
        ]
        for pattern, handler in patterns:
            match = re.fullmatch(pattern, path)
            if match:
                return handler(*match.groups())
        return _error("E12", "Unexpected path " + path)

    def _master(self, slot, target):
        label = ROOT + f"masterPresetBank/$bank/@items/{slot}/control/@props/label"
        if label not in self._visible():
            return _error("E13", "Memory is not stored")
        for branch, sid in (("$screen", "S1"), ("$screen", "S3"), ("$auxiliary", "A2")):
            if ROOT + f"{branch}/@items/{sid}/control/@props/label" in self._visible():
                self._screen_preset(slot, branch, sid, target)
        self.set_state(
            ROOT
            + f"masterPresetBank/status/lastUsed/$presetMode/@items/{target}/@props/memoryId",
            int(slot),
        )
        return None

    def _screen_preset(self, slot, branch, sid, target):
        label = ROOT + f"presetBank/$bank/@items/{slot}/control/@props/label"
        if label not in self._visible():
            return _error("E13", "Memory is not stored")
        transition = self.get_state(
            ROOT + f"$screenAuxGroup/@items/{sid}/status/@props/transition"
        )
        bank = "A" if transition == "AT_DOWN" else "B"
        if target == "PREVIEW":
            bank = "B" if bank == "A" else "A"
        base = (
            ROOT
            + f"{branch}/@items/{sid}/$preset/@items/{bank}/$layer/@items/1/source/@props/inputNum"
        )
        self.set_state(base, "STILL_12" if slot == "7" else "LIVE_2")
        self.set_state(
            ROOT
            + f"presetBank/status/presetId/{branch}/@items/{sid}/$preset/@items/{bank}/@props/id",
            int(slot),
        )
        return None

    def _multiviewer(self, slot, mid):
        path = (
            ROOT
            + f"$monitoring/@items/{mid}/layout/$widget/@items/0/control/@props/source"
        )
        if path not in self._visible():
            return _error("E13", "Multiviewer is disabled")
        self.set_state(path, "PREVIEW_S1" if slot == "7" else "PROGRAM_S1")
        return None


def _reply(path, value):
    return (
        json.dumps({"path": path, "value": value}, separators=(",", ":")) + "\x04"
    ).encode()


def _error(code, message):
    return (json.dumps({"error": {"code": code, "message": message}}) + "\x04").encode()
