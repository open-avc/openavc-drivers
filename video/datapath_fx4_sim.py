"""Datapath Fx4 REST simulator, based on the manufacturer's published API.

Models the documented JSON .cgx reads and POST acknowledgments/refusals, zero-based
ports, optional POST password, source fallback priority 2/0/1, independent output
controls, read/modify/write transforms and explicit SaveSettings. No driver imports.

Choices not established by the manual: illustrative serial/firmware and timing
values, instantaneous changes, no-signal timings answered with a refusal, and
English errors without undocumented numeric codes. All three models use the shared
REST surface; this does not establish hardware-specific audio/HDR support. Toggle
an input's signal or an output control in the simulator to model external changes.
Unsupported-audio, ignored-write, bad-password and health-refusal modes are synthetic
failure cases. Only SaveSettings increments Explicit Saves; no power-cycle claim.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator


class DatapathFx4Simulator(HTTPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "datapath_fx4",
        "name": "Datapath Fx4 Video Wall Controller Simulator",
        "category": "video",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "preferred_input": "auto",
            "input_1_present": True,
            "input_2_present": True,
            "input_3_present": True,
            "temperature": 34.312,
            "supply_good": True,
            "save_count": 0,
        },
        "controls": [
            {
                "type": "select",
                "key": "preferred_input",
                "label": "Preferred Source",
                "options": ["auto", "1", "2", "3"],
            },
            {"type": "toggle", "key": "input_1_present", "label": "Input 1 Signal Present"},
            {"type": "toggle", "key": "input_2_present", "label": "Input 2 Signal Present"},
            {"type": "toggle", "key": "input_3_present", "label": "Input 3 Signal Present"},
            {
                "type": "slider",
                "key": "temperature",
                "label": "Temperature (°C)",
                "min": 10,
                "max": 90,
                "step": 1,
            },
            {"type": "toggle", "key": "supply_good", "label": "Power Supplies Good"},
            {"type": "indicator", "key": "save_count", "label": "Explicit Saves"},
            {"type": "toggle", "key": "OutputEnable:0:Enable", "label": "Output 1 Video"},
            {"type": "toggle", "key": "OutputAudioEnable:0:Enable", "label": "Output 1 Audio"},
            {
                "type": "select",
                "key": "OutputImageSource:0:Source",
                "label": "Output 1 Image",
                "options": [
                    "InputCapture",
                    "Colourbars",
                    "Greyscale",
                    "MovingColourbars",
                    "MovingGreyscale",
                    "FlatColour",
                ],
            },
            {"type": "toggle", "key": "OutputEnable:1:Enable", "label": "Output 2 Video"},
            {"type": "toggle", "key": "OutputAudioEnable:1:Enable", "label": "Output 2 Audio"},
            {
                "type": "select",
                "key": "OutputImageSource:1:Source",
                "label": "Output 2 Image",
                "options": [
                    "InputCapture",
                    "Colourbars",
                    "Greyscale",
                    "MovingColourbars",
                    "MovingGreyscale",
                    "FlatColour",
                ],
            },
            {"type": "toggle", "key": "OutputEnable:2:Enable", "label": "Output 3 Video"},
            {"type": "toggle", "key": "OutputAudioEnable:2:Enable", "label": "Output 3 Audio"},
            {
                "type": "select",
                "key": "OutputImageSource:2:Source",
                "label": "Output 3 Image",
                "options": [
                    "InputCapture",
                    "Colourbars",
                    "Greyscale",
                    "MovingColourbars",
                    "MovingGreyscale",
                    "FlatColour",
                ],
            },
            {"type": "toggle", "key": "OutputEnable:3:Enable", "label": "Output 4 Video"},
            {"type": "toggle", "key": "OutputAudioEnable:3:Enable", "label": "Output 4 Audio"},
            {
                "type": "select",
                "key": "OutputImageSource:3:Source",
                "label": "Output 4 Image",
                "options": [
                    "InputCapture",
                    "Colourbars",
                    "Greyscale",
                    "MovingColourbars",
                    "MovingGreyscale",
                    "FlatColour",
                ],
            },
        ],
        "error_modes": {
            "communication_timeout": {"behavior": "no_response", "description": "Controller stops answering"},
            "password_changed": {
                "behavior": "custom",
                "description": "Reject writes with invalid password; reads still " "work",
            },
            "ignore_writes": {
                "behavior": "custom",
                "description": "Acknowledge writes without applying them",
            },
            "audio_unsupported": {
                "behavior": "custom",
                "description": "Audio API unavailable; other controls remain " "usable",
            },
            "health_read_refused": {
                "behavior": "custom",
                "description": "Refuse the health query inside HTTP 200",
            },
        },
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        for endpoint, fields in RESOURCES.items():
            for index in _indices(endpoint):
                self._seed_resource(endpoint, index, fields)

    def _seed_resource(self, endpoint, index, fields):
        for field, default in fields.items():
            self.set_state(_key(endpoint, index, field), default)

    def handle_request(self, method, path, headers, body):
        parsed = urlsplit(path)
        if parsed.path == "/":
            return 200, "Datapath Fx4 simulator"
        endpoint = parsed.path.removeprefix("/").removesuffix(".cgx")
        try:
            data = json.loads(body) if body else {}
            if not isinstance(data, dict):
                return _refused("Expected a JSON object")
            index = _index(endpoint, method, parse_qs(parsed.query), data)
        except (ValueError, TypeError):
            return _refused("Invalid input or output")
        if endpoint == "OutputAudioEnable" and "audio_unsupported" in self.active_errors:
            return _refused("API not supported")
        if method == "POST":
            return self._post(endpoint, index, data)
        if method not in ("GET", "HEAD"):
            return 405, "Method not allowed"
        return self._get(endpoint, index)

    def _get(self, endpoint, index):
        if endpoint in ("ActiveInput", "PreferredInput"):
            preferred = self.state["preferred_input"]
            value = None if preferred == "auto" else int(preferred) - 1
            return 200, {"Input": self._active_input() if endpoint == "ActiveInput" else value}
        if endpoint == "SystemHealth":
            return self._health()
        if endpoint == "InputCurrentTimings" and not self.state[f"input_{index + 1}_present"]:
            return _refused("No input signal")
        if endpoint not in RESOURCES:
            return 404, {"Success": False, "ErrorMessage": "API not supported"}
        return 200, {field: self.state[_key(endpoint, index, field)] for field in RESOURCES[endpoint]}

    def _health(self):
        if "health_read_refused" in self.active_errors:
            return _refused("Health read refused")
        return 200, {
            "PowerSupply": {"IO": self.state["supply_good"], "1.8V": True, "1.2V": True},
            "Temperature": {"Average": self.state["temperature"], "Minimum": 23, "Maximum": 39},
        }

    def _active_input(self):
        preferred = self.state["preferred_input"]
        order = ([int(preferred) - 1] if preferred != "auto" else []) + [2, 0, 1]
        return next((index for index in order if self.state[f"input_{index + 1}_present"]), None)

    def _post(self, endpoint, index, data):
        if not self._password_valid(data):
            return _refused("Invalid password")
        if endpoint == "SaveSettings":
            self.set_state("save_count", self.state["save_count"] + 1)
            return 200, {"Success": True}
        if endpoint == "PreferredInput":
            return self._prefer(data)
        if endpoint not in WRITABLE:
            return _refused("API not writable")
        fields = WRITABLE[endpoint]
        if not _valid_fields(fields, data):
            return _refused("Invalid value")
        if "ignore_writes" not in self.active_errors:
            self._apply_write(endpoint, index, data, fields)
        return 200, {"Success": True}

    def _apply_write(self, endpoint, index, data, fields):
        for field in fields:
            self.set_state(_key(endpoint, index, field), data[field])
        if endpoint == "OutputTimingSource":
            self.set_state(_key("OutputCurrentTimingSource", index, "Source"), data["Source"])

    def _password_valid(self, data):
        if "password_changed" in self.active_errors or data.get("Password") == "invalid":
            return False
        password = self.config.get("password", "")
        return not password or data.get("Password") == password

    def _prefer(self, data):
        if "Input" not in data or not _allowed(data["Input"], (None, 0, 1, 2)):
            return _refused("Invalid input")
        if "ignore_writes" not in self.active_errors:
            value = "auto" if data["Input"] is None else str(data["Input"] + 1)
            self.set_state("preferred_input", value)
        return 200, {"Success": True}


def _indices(endpoint):
    if endpoint == "OutputNoSignal":
        return (None,)
    if endpoint.startswith("Input"):
        return range(3)
    if endpoint.startswith("Output"):
        return range(4)
    return (None,)


def _key(endpoint, index, field):
    return f"{endpoint}:{index}:{field}"


def _refused(message):
    return 200, {"Success": False, "ErrorMessage": message}


def _index(endpoint, method, query, body):
    if endpoint not in RESOURCES or endpoint == "OutputNoSignal":
        return None
    kind = "Input" if endpoint.startswith("Input") else "Output" if endpoint.startswith("Output") else None
    if not kind:
        return None
    raw = body.get(kind) if method == "POST" else query.get(kind.lower(), [None])[0]
    index = int(raw)
    if isinstance(raw, bool) or index not in range(3 if kind == "Input" else 4):
        raise ValueError("Invalid port")
    return index


def _valid_fields(fields, data):
    return all(field in data and _allowed(data[field], choices) for field, choices in fields.items())


def _allowed(value, choices):
    if choices == "text":
        return isinstance(value, str)
    return any(type(value) is type(option) and value == option for option in choices)


TIMINGS = {
    "HorAddrTime": 1920,
    "VerAddrTime": 1080,
    "HorFrequency": 67432,
    "VerFrequency": 59939,
    "PixelClock": 148351478,
    "Flags": 0,
    "ColourSpace": "RGB 709",
    "ColourRange": "Full",
    "Subsampling": "4:4:4",
}
RESOURCES = {
    "SerialNumber": {"SerialNumber": "SIM-FX4-0001"},
    "SystemInfo": {"FirmwareVersion": 66048, "ConfigVersion": 256, "HardwareVersion": 1, "FPGAVersion": 5},
    "FriendlyName": {"FriendlyName": "Demonstration Video Wall"},
    "NetworkSettings": {
        "MACAddress": "02:00:5E:00:53:04",
        "DHCP": True,
        "IPAddress": "127.0.0.1",
        "Gateway": "127.0.0.1",
        "Subnet": "255.0.0.0",
    },
    "Smoothness": {"Smoothness": 0},
    "OutputNoSignal": {"NoSignal": False},
    "InputCurrentTimings": TIMINGS,
    "InputHDCPEnable": {"Enable": True},
    "InputHDCPStatus": {"Status": "Unencrypted"},
    "InputHDCPExtStatus": {"Status": ""},
    "InputHDRStatus": {"Status": "No"},
    "InputHDRVersion": {"Status": "None"},
    "OutputCurrentTimings": TIMINGS,
    "OutputEnable": {"Enable": True},
    "OutputAudioEnable": {"Enable": True},
    "OutputTransform": {"Rotation": 0, "Flip": "None"},
    "OutputImageSource": {"Source": "InputCapture"},
    "OutputTimingSource": {"Source": "Monitor"},
    "OutputCurrentTimingSource": {"Source": "Monitor"},
    "OutputGenlockSource": {"GenlockSource": "Input"},
    "OutputHDCPStatus": {"Status": "Unencrypted"},
    "OutputHDCPExtStatus": {"Status": ""},
    "OutputCrop": {"X0": 0.0, "Y0": 0.0, "X1": 1.0, "Y1": 1.0},
}
WRITABLE = {
    "FriendlyName": {"FriendlyName": "text"},
    "Smoothness": {"Smoothness": (0, 1)},
    "OutputNoSignal": {"NoSignal": (True, False)},
    "InputHDCPEnable": {"Enable": (True, False)},
    "OutputEnable": {"Enable": (True, False)},
    "OutputAudioEnable": {"Enable": (True, False)},
    "OutputTransform": {"Rotation": (0, 90, 180, 270), "Flip": ("None", "Horizontal", "Vertical")},
    "OutputImageSource": {
        "Source": (
            "InputCapture",
            "Colourbars",
            "Greyscale",
            "MovingColourbars",
            "MovingGreyscale",
            "FlatColour",
        )
    },
    "OutputTimingSource": {"Source": ("Monitor", "Default")},
    "OutputGenlockSource": {"GenlockSource": ("None", "Sync", "Input", "Input0", "Input1", "Input2")},
}
