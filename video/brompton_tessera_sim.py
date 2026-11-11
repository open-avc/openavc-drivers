"""Tessera HTTP simulator from Brompton's IP Control API 3.5.2 revision 2.

GET and PUT return the requested endpoint under its leaf name (pp. 69-71),
including the actual new value on PUT. Errors carry response-code. Project
rosters occupy the documented dynamic tree (p. 7); no driver tables are imported.
Illustrative project names, temperatures, timings and instantaneous transitions
are simulator choices, not hardware captures. Model selection changes identity;
no claim about undisclosed model-specific feature differences is implied.
"""

from __future__ import annotations

import json
import math
from urllib.parse import urlsplit

from openavc.simulator.http_simulator import HTTPSimulator


class BromptonTesseraSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "brompton_tessera",
        "name": "Brompton Tessera LED Wall",
        "category": "video",
        "transport": "http",
        "default_port": 80,
        "initial_state": {"alternate_project": False},
        "controls": [
            {"type": "toggle", "key": "override/blackout/enabled", "label": "Blackout"},
            {"type": "toggle", "key": "override/freeze/enabled", "label": "Freeze"},
            {
                "type": "slider",
                "key": "output/global-colour/brightness",
                "label": "Brightness (nits)",
                "min": 0,
                "max": 10000,
                "step": 50,
            },
            {
                "type": "slider",
                "key": "devices/statistics/error-count",
                "label": "Devices Reporting Errors",
                "min": 0,
                "max": 10,
                "step": 1,
            },
            {
                "type": "toggle",
                "key": "output/network/failover/state/is-partner-present",
                "label": "Redundancy Partner Present",
            },
            {
                "type": "slider",
                "key": "system/temperature/cpu",
                "label": "CPU Temperature (°C)",
                "min": 20,
                "max": 100,
                "step": 1,
            },
            {
                "type": "toggle",
                "key": "alternate_project",
                "label": "Load Alternate Project (different rosters)",
            },
        ],
        "error_modes": {
            "communication_timeout": {
                "behavior": "no_response",
                "description": "Processor stops answering",
            },
            "no_project": {
                "behavior": "custom",
                "description": "No project loaded; system identity still answers",
            },
            "access_denied": {
                "behavior": "custom",
                "description": "Writes are refused by the processor",
            },
            "ignore_writes": {
                "behavior": "custom",
                "description": "Return unchanged actual values after writes",
            },
            "health_unsupported": {
                "behavior": "custom",
                "description": "Temperature and fan branches report Not supported",
            },
        },
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        for address, value in INITIAL_VALUES.items():
            self.set_state(address, value)
        self.set_state("system/processor-type", self.config.get("model", "sx40"))
        self._seed_project()

    def _seed_project(self):
        for number, name in ((1, "Main Wall"), (9, "Stage Wings")):
            for suffix, value in GROUP_VALUES.items():
                self.set_state(f"groups/items/{number}/{suffix}", value)
            self.set_state(f"groups/items/{number}/name", name)
        for number, name in ((1, "Welcome"), (7, "Presentation"), (42, "Evening")):
            self.set_state(f"presets/items/{number}/name", name)
            self.set_state(f"presets/items/{number}/status", number == 1)
        for number, name in ((2, "Holding Card"), (9, 'Sponsor "A"')):
            self.set_state(
                f"override/test-pattern/frame-store/frames/{number}/name", name
            )
        for port in ("hdmi", "sdi"):
            self.set_state(f"input/ports/{port}/1/meta-data/resolution/width", 1920)
            self.set_state(f"input/ports/{port}/1/meta-data/resolution/height", 1080)
            self.set_state(f"input/ports/{port}/1/meta-data/refresh-rate", 60.0)

    def handle_request(self, method, path, headers, body):
        address = urlsplit(path).path.strip("/")
        if address == "":
            return 200, "Tessera processor simulator"
        if not address.startswith("api/"):
            return _error("Path not found")
        address = address[4:]
        refusal = self._refusal(address, method)
        if refusal:
            return _error(refusal)
        if method == "PUT":
            return self._handle_write(address, body)
        if method not in ("GET", "HEAD"):
            return _error("Bad operation")
        return self._answer(address)

    def _refusal(self, address, method):
        if "no_project" in self.active_errors and not address.startswith("system/"):
            return "No project loaded"
        if "health_unsupported" in self.active_errors and address.startswith(
            ("system/temperature", "system/fan")
        ):
            return "Not supported"
        if "access_denied" in self.active_errors and method == "PUT":
            return "Access denied"
        return None

    def _visible_values(self):
        values = {key: value for key, value in self.state.items() if "/" in key}
        if not self.state["alternate_project"]:
            return values
        omitted = (
            "groups/items/9/",
            "presets/items/42/",
            "override/test-pattern/frame-store/frames/9/",
        )
        values = {
            key: value for key, value in values.items() if not key.startswith(omitted)
        }
        values["project/name"] = "Conference"
        values["groups/items/1/name"] = "Conference Wall"
        values["presets/items/7/name"] = "Conference Presentation"
        return values

    def _answer(self, address):
        values = self._visible_values()
        if address in values:
            return 200, {address.rsplit("/", 1)[-1]: values[address]}
        descendants = {
            key[len(address) + 1 :]: value
            for key, value in values.items()
            if key.startswith(address + "/")
        }
        if not descendants:
            return _error("Path not found")
        tree = {}
        for suffix, value in descendants.items():
            branch = tree
            parts = suffix.split("/")
            for part in parts[:-1]:
                branch = branch.setdefault(part, {})
            branch[parts[-1]] = value
        return 200, {address.rsplit("/", 1)[-1]: tree}

    def _handle_write(self, address, body):
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return _error("Bad input parameter type")
        if not isinstance(payload, dict) or "data" not in payload:
            return _error("Missing input parameter")
        writes = _expanded_write(address, payload["data"])
        values = self._visible_values()
        refusal = _check_writes(writes, values)
        if refusal:
            return _error(refusal)
        if not self._selection_exists(writes, values):
            return _error("Object not found")
        if "ignore_writes" not in self.active_errors:
            self._apply(writes)
        return self._answer(address)

    def _selection_exists(self, writes, values):
        preset = writes.get("presets/active/number")
        if preset is not None and f"presets/items/{preset}/name" not in values:
            return False
        pattern = writes.get("override/test-pattern/type")
        if (
            type(pattern) is int
            and f"override/test-pattern/frame-store/frames/{pattern}/name" not in values
        ):
            return False
        source_type = writes.get(
            "input/active/source/port-type", self.state["input/active/source/port-type"]
        )
        source_number = writes.get(
            "input/active/source/port-number",
            self.state["input/active/source/port-number"],
        )
        return (
            f"input/ports/{source_type}/{source_number}/meta-data/refresh-rate"
            in values
        )

    def _apply(self, writes):
        for address, value in writes.items():
            self.set_state(address, value)
        preset = writes.get("presets/active/number")
        if preset is not None:
            self.set_state(
                "presets/active/name",
                self._visible_values()[f"presets/items/{preset}/name"],
            )
            for number in (1, 7, 42):
                self.set_state(f"presets/items/{number}/status", number == preset)


def _expanded_write(address, value):
    if not isinstance(value, dict):
        return {address: value}
    result = {}
    for key, child in value.items():
        result.update(_expanded_write(address + "/" + key, child))
    return result


def _check_writes(writes, values):
    for target, value in writes.items():
        if target not in values:
            return "Object not found"
        if not _writable_value(target, value):
            return "Bad input parameter value"
    return None


def _writable_value(address, value):
    if address in VALUE_RULES:
        return VALUE_RULES[address](value)
    if address in BOOLEAN_WRITES or address.startswith(
        "output/network/failover/settings/modes/"
    ):
        return type(value) is bool
    if address.startswith("groups/items/"):
        return _group_value("/".join(address.split("/")[3:]), value)
    if address.startswith("output/global-colour/"):
        return _colour_value(address[len("output/global-colour/") :], value)
    return False


def _pattern_value(value):
    return (type(value) is int and 1 <= value <= 50) or value in PATTERNS


def _group_value(field, value):
    if field in ("global-colour-override", "global-gains-override"):
        return type(value) is bool
    return _colour_value(field, value)


def _colour_value(field, value):
    bounds = {
        "brightness": (0, 10000),
        "colour-temperature": (2000, 11000),
        "gamma": (0.2, 4),
        "gains/intensity": (0, 100),
    }
    if field not in bounds or not _number_in(value, *bounds[field]):
        return False
    return field not in ("brightness", "colour-temperature") or type(value) is int


def _number_in(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _error(code):
    return 200, {"response-code": code}


PATTERNS = (
    "brompton",
    "red",
    "green",
    "blue",
    "cyan",
    "magenta",
    "yellow",
    "white",
    "black",
    "grid",
    "scrolling-grid",
    "checkerboard",
    "scrolling-checkerboard",
    "colour-bars",
    "scrolling-colour-bars",
    "gradient",
    "scrolling-gradient",
    "strobe",
    "smpte-bars",
    "scrolling-smpte-bars",
    "custom-colour",
    "custom",
    "forty-five-degree-grid",
    "scrolling-forty-five-degree-grid",
    "custom-gradient",
    "scrolling-custom-gradient",
)
BOOLEAN_WRITES = {
    "override/blackout/enabled",
    "override/freeze/enabled",
    "override/test-pattern/enabled",
    "output/network/failover/settings/enabled",
}
GROUP_VALUES = {
    "brightness": 800,
    "colour-temperature": 6500,
    "gamma": 2.2,
    "gains/intensity": 100.0,
    "brightness-limit/enabled": True,
    "brightness-limit/value": 1800,
    "global-colour-override": False,
    "global-gains-override": False,
}
INITIAL_VALUES = {
    "system/processor-type": "sx40",
    "system/processor-name": "Main LED Processor",
    "system/serial-number": "SIM-SX40-0001",
    "system/software-version": "3.5.2",
    "system/uptime": "01d 02h 03m 04s",
    "project/name": "Auditorium",
    "system/temperature/ambient": 27.0,
    "system/temperature/cpu": 43.5,
    "system/temperature/fpga": 46.1,
    "system/temperature/gpu": 40.0,
    "system/temperature/main": 32.0,
    "system/temperature/psu": 35.0,
    "system/fan/case/one/speed": 1500.0,
    "system/fan/case/one/status": True,
    "system/fan/case/two/speed": 1500.0,
    "system/fan/case/two/status": True,
    "system/fan/fpga/speed": 1800.0,
    "system/fan/fpga/status": True,
    "input/active/source/port-type": "hdmi",
    "input/active/source/port-number": 1,
    "output/global-colour/brightness": 1000,
    "output/global-colour/colour-temperature": 6500,
    "output/global-colour/gamma": 2.2,
    "output/global-colour/gains/intensity": 100.0,
    "output/global-colour/brightness-limit/enabled": True,
    "output/global-colour/brightness-limit/value": 1800,
    "override/blackout/enabled": False,
    "override/blackout/fade-time": 0.0,
    "override/freeze/enabled": False,
    "override/test-pattern/enabled": False,
    "override/test-pattern/type": "smpte-bars",
    "presets/active/number": 1,
    "presets/active/name": "Welcome",
    "devices/statistics/associated-count": 48,
    "devices/statistics/online-count": 48,
    "devices/statistics/error-count": 0,
    "output/network/failover/settings/enabled": True,
    "output/network/failover/settings/role": "primary",
    "output/network/failover/settings/modes/on-button-press": False,
    "output/network/failover/settings/modes/on-partner-fail": True,
    "output/network/failover/settings/modes/on-partner-video-fail": True,
    "output/network/failover/settings/modes/prefer-primary": True,
    "output/network/failover/state/is-active": False,
    "output/network/failover/state/is-partner-present": True,
    "output/network/failover/state/partner-name": "Backup LED Processor",
    "output/network/failover/state/partner-serial": "SIM-SX40-0002",
    "output/network/failover/state/partner-absence-duration": "00d 00h 00m 00s",
    "output/network/failover/state/partner-video-absence-duration": "00d 00h 00m 00s",
    "output/network/cable-redundancy/loops/1/state": "loop-found: A1 -> B1",
}

VALUE_RULES = {
    "override/test-pattern/type": _pattern_value,
    "input/active/source/port-type": lambda value: value in ("hdmi", "sdi"),
    "input/active/source/port-number": lambda value: (
        type(value) is int and value in (1, 2)
    ),
    "presets/active/number": lambda value: type(value) is int and 1 <= value <= 128,
    "override/blackout/fade-time": lambda value: _number_in(value, 0, 10),
}
