"""CALICO PRO CLI simulator from tvONE document 16005.1.

Models API 1.6.0 reply framing, a per-connection login and subscriptions,
modular ports, canvas-assigned windows, sparse presets and a configured media
queue. The example wall is illustrative, not a claim about factory designs or
the maximum capacity of either chassis. No driver implementation is imported.
UI controls act like external device changes and emit the documented events.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
import re

from openavc.simulator.tcp_simulator import TCPSimulator


_CATEGORIES = {
    "WINDOW",
    "CANVAS",
    "LAYOUT",
    "OUTPUTS_SYNC",
    "OUTPUT",
    "INPUT",
    "HDMI",
    "STBD",
    "PRESET",
    "MEDIA_PLAYER",
    "MODULE",
    "MODULE_CORE_TEMPERATURE",
}
_UI_PROPERTIES = {
    "window_1_source": "Window1.Input",
    "canvas_1_volume": "Canvas1.AudioVolume",
    "canvas_1_blackout": "Canvas1.CutToBlack",
    "input_1_signal": "Slot1.In1.HDMI",
    "output_1_sink": "Slot3.Out1.HDMI",
    "system_alert": "System.Alerts",
    "unit_name": "System.UnitDescription",
}
_BOOLEAN_PROPERTIES = {
    "mute",
    "hflip",
    "vflip",
    "scftb",
    "sccrossfade",
    "cuttoblack",
    "audiomute",
    "audioenable",
    "ecomode",
}
_RANGES = {
    "width": (0, 16383),
    "height": (0, 16383),
    "centerx": (-8192, 8191),
    "centery": (-8192, 8191),
    "zorder": (0, 15),
    "ftb": (0, 256),
    "brightness": (-30, 30),
    "contrast": (30, 130),
    "audiovolume": (0, 100),
}
_INPUT_EVENTS = {
    "Status",
    "MeasuredResolution",
    "SetResolution",
    "CanFramelockTo",
    "HDCPRequired",
    "HDCPStatus",
    "HDMI",
    "ColourSampling",
    "ColourRangeActive",
    "Audio",
}
_WRITABLE = (
    _BOOLEAN_PROPERTIES
    | set(_RANGES)
    | {
        "input",
        "canvas",
        "rotation",
        "audiosource",
        "audiomode",
        "audiofollowwindow",
        "playmode",
        "unitdescription",
    }
)


class TvoneCalicoProSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "tvone_calico_pro",
        "name": "tvONE CALICO PRO Simulator",
        "category": "video",
        "transport": "tcp",
        "default_port": 10001,
        "delimiter": "\r\n",
        "initial_state": {
            "window_1_source": "Slot1.In1",
            "canvas_1_volume": 75,
            "canvas_1_blackout": False,
            "input_1_signal": "Found",
            "output_1_sink": "Found",
            "active_preset": 1,
            "system_alert": "OK",
            "unit_name": "CALICO Demo Wall",
            "media_status": "Idle",
            "media_index": 1,
        },
        "controls": [
            {
                "type": "select",
                "key": "window_1_source",
                "label": "Presenter Window Source",
                "options": ["Slot1.In1", "Slot1.In2", "Slot2.In1"],
            },
            {
                "type": "slider",
                "key": "canvas_1_volume",
                "label": "Main Wall Audio",
                "min": 0,
                "max": 100,
            },
            {
                "type": "toggle",
                "key": "canvas_1_blackout",
                "label": "Main Wall Blackout",
            },
            {
                "type": "select",
                "key": "input_1_signal",
                "label": "Presenter Signal",
                "options": ["Found", "Not_Found"],
            },
            {
                "type": "select",
                "key": "output_1_sink",
                "label": "Left Display",
                "options": ["Found", "Not_Found"],
            },
            {
                "type": "select",
                "key": "active_preset",
                "label": "Preset",
                "options": [1, 57],
            },
            {
                "type": "select",
                "key": "system_alert",
                "label": "System Alert",
                "options": ["OK", "Error_OTF", "Error_FF"],
            },
            {"type": "indicator", "key": "media_status", "label": "Media Playback"},
            {"type": "indicator", "key": "media_index", "label": "Media Queue Item"},
        ],
        "error_modes": {
            "communication_timeout": {
                "behavior": "no_response",
                "description": "Device stops responding",
            },
            "refuse_writes": {
                "behavior": "custom",
                "description": "Refuse setting changes",
            },
            "ignore_writes": {
                "behavior": "custom",
                "description": "Acknowledge changes without applying them",
            },
        },
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._objects = _example_wall(self.config.get("model", "C7-PRO-2200"))
        self._session = contextvars.ContextVar("calico_sim_client", default=None)
        self._subscriptions = {}
        self._push_tasks = set()
        self.command_log = []
        self.saved_count = 0

    async def on_client_connected(self, client_id):
        return (
            b"// ===================\r\n// CALICO PRO - 2U\r\n"
            b"// ===================\r\n// Command Interface Ready\r\n"
            b"Please login. Use 'login(username,password)'\r\n"
        )

    async def authenticate_client(self, reader, writer, client_id):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\n"), 10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return False
        command = raw.decode("utf-8", errors="replace").strip()
        match = re.fullmatch(r"Login\(([^,()]*),([^,()]*)\)", command, re.I)
        good = match is not None and self._credentials_match(match[1], match[2])
        echo = "Login(********,********)"
        if not good:
            await self.push_to(client_id, _failed(echo, "Login failed"))
            return False
        self._subscriptions = {
            key: value
            for key, value in self._subscriptions.items()
            if key in self._clients
        }
        self._subscriptions[client_id] = set()
        self._session.set(client_id)
        await self.push_to(
            client_id, _reply([f"!Info : User {match[1]} Logged In"], echo)
        )
        return True

    def _credentials_match(self, user, password):
        if not user or not password or "invalid" in (user.lower(), password.lower()):
            return False
        return all(
            not self.config.get(key) or self.config[key] == value
            for key, value in (("username", user), ("password", password))
        )

    async def stop(self):
        for task in list(self._push_tasks):
            task.cancel()
        await asyncio.gather(*self._push_tasks, return_exceptions=True)
        self._push_tasks.clear()
        self._subscriptions.clear()
        await super().stop()

    def handle_command(self, data):
        command = data.decode("utf-8", errors="replace").strip()
        self.command_log.append(command)
        if self._session.get() not in self._subscriptions:
            return _failed(command, "Please login")
        try:
            return self._dispatch(command)
        except (KeyError, ValueError, IndexError, StopIteration):
            return _failed(command, "Invalid property, method or value")

    def _dispatch(self, command):
        if command.lower().startswith(("addevents(", "removeevents(", "listevents(")):
            return self._events_command(command)
        if command.lower() == "preset.presetlist()":
            return self._preset_list(command)
        if command.endswith("()"):
            return self._method(command)
        if "=" in command:
            path, value = (part.strip() for part in command.split("=", 1))
            return self._write(path, value, command)
        key = _canonical(command)
        if key in self._objects:
            return _reply(
                [
                    f"{command}.{prop} = {value}"
                    for prop, value in self._objects[key].items()
                ],
                command,
            )
        parent, prop = self._property_address(key)
        return _reply([f"{command} = {self._objects[parent][prop]}"], command)

    def _events_command(self, command):
        match = re.fullmatch(
            r"(AddEvents|RemoveEvents|ListEvents)\((.*?)\)", command, re.I
        )
        if match is None:
            raise ValueError("Unknown event method")
        subscribed = self._subscriptions[self._session.get()]
        action, category = match[1].lower(), match[2].upper()
        if action == "listevents":
            return _reply(sorted(subscribed), command)
        if category not in _CATEGORIES:
            raise ValueError("Unknown event category")
        if action == "addevents":
            subscribed.add(category)
        else:
            subscribed.discard(category)
        return _reply([], command)

    def _preset_list(self, command):
        lines = []
        for key, props in self._objects.items():
            match = re.fullmatch(r"stbd(\d+)", key)
            if match:
                lines.append(
                    f"Routing.Preset.PresetList[{match[1]}]={props['Name']},{props['Canvas']},{props['Duration']}"
                )
        return _reply(lines, command)

    def _property_address(self, key):
        parent, prop = key.rsplit(".", 1)
        spelling = next(name for name in self._objects[parent] if name.lower() == prop)
        return parent, spelling

    def _write(self, path, value, command):
        parent, prop = self._property_address(_canonical(path))
        if prop.lower() not in _WRITABLE:
            return _failed(command, "Property is read only")
        self._validate_write(prop.lower(), value)
        if "refuse_writes" in self.active_errors:
            return _failed(command, "Setting change refused")
        if "ignore_writes" not in self.active_errors:
            self.change_property(path, value)
        return _reply([], command)

    def _validate_write(self, prop, value):
        self._validate_numeric(prop, value)
        self._validate_choice(prop, value)
        if prop in ("input", "audiosource", "canvas"):
            if _canonical(value) not in self._objects:
                raise ValueError("Unknown object")

    @staticmethod
    def _validate_numeric(prop, value):
        if prop in _RANGES:
            low, high = _RANGES[prop]
            if not low <= int(value) <= high:
                raise ValueError("Outside range")
        if prop == "rotation" and not math.isfinite(float(value)):
            raise ValueError("Non-finite rotation")

    @staticmethod
    def _validate_choice(prop, value):
        if prop in _BOOLEAN_PROPERTIES and value not in ("On", "Off"):
            raise ValueError("Expected On or Off")
        choices = {
            "audiomode": ("FromSource", "FollowWindow"),
            "playmode": ("Single", "Repeat"),
        }
        if prop in choices and value not in choices[prop]:
            raise ValueError("Unknown mode")

    def _method(self, command):
        path, method = command[:-2].rsplit(".", 1)
        key = _canonical(path)
        if key == "system" and method.lower() in (
            "saveallsettings",
            "savesystemsettings",
        ):
            self.saved_count += 1
        elif re.fullmatch(r"stbd\d+", key) and method.lower() == "take":
            self._take(int(re.search(r"\d+", key)[0]))
        elif key.endswith(".activequeue"):
            self._media_method(key, method)
        else:
            raise ValueError("Unknown method")
        return _reply([], command)

    def _take(self, number):
        preset = self._objects[f"stbd{number}"]
        canvas = preset["Canvas"]
        self.emit_event("PRESET", "TAKE", number, canvas, preset["Name"])
        self.change_property(canvas + ".StbdCurrent", f"Stbd{number}")
        self.change_property(
            "Window1.Input", "Slot1.In2" if number == 57 else "Slot1.In1"
        )
        for key, properties in self._objects.items():
            if key.startswith("stbd"):
                properties["IsCurrent"] = "Yes" if key == f"stbd{number}" else "No"
                self.emit_event(
                    "STBD", "ISCURRENT_CHANGED", key, properties["IsCurrent"]
                )
        super().set_state("active_preset", number)
        self.emit_event("PRESET", "COMPLETE", number, canvas, preset["Name"])

    def _media_method(self, key, method):
        properties = self._objects[key]
        action = method.lower()
        states = {"play": "Playing", "pause": "Paused", "stop": "Idle"}
        if action in states:
            properties["Status"] = states[action]
            if action == "stop":
                properties["CurrentIndex"] = "1"
        elif action in ("skipforward", "skipbackward"):
            self._skip_media(properties, 1 if action == "skipforward" else -1)
        else:
            raise ValueError("Unknown playback action")
        super().set_state("media_status", properties["Status"])
        super().set_state("media_index", int(properties["CurrentIndex"]))
        self.emit_event(
            "MEDIA_PLAYER",
            "STATUS_UPDATE",
            key.removesuffix(".activequeue"),
            properties["Status"],
            properties["CurrentIndex"],
        )

    @staticmethod
    def _skip_media(properties, direction):
        index = int(properties["CurrentIndex"]) + direction
        if properties["PlayMode"] == "Repeat":
            index = (index - 1) % 3 + 1
        if not 1 <= index <= 3:
            raise ValueError("End of queue")
        properties["CurrentIndex"] = str(index)

    def set_state(self, key, value):
        if key == "active_preset":
            self._take(int(value))
        elif key in _UI_PROPERTIES:
            wire = ("On" if value else "Off") if isinstance(value, bool) else str(value)
            self.change_property(_UI_PROPERTIES[key], wire)
        else:
            super().set_state(key, value)

    def change_property(self, path, value):
        """An external wall/configuration change; query and event use one value."""
        parent, prop = self._property_address(_canonical(path))
        self._objects[parent][prop] = str(value)
        self._sync_ui(parent, prop, value)
        if prop == "Canvas" and parent.startswith("window"):
            self._rebuild_window_lists()
        self._emit_property(parent, prop, value)

    def _sync_ui(self, parent, prop, value):
        for key, path in _UI_PROPERTIES.items():
            if _canonical(path) != parent + "." + prop.lower():
                continue
            initial = self.SIMULATOR_INFO["initial_state"][key]
            if isinstance(initial, bool):
                value = str(value) == "On"
            elif isinstance(initial, int):
                value = int(value)
            super().set_state(key, value)

    def _rebuild_window_lists(self):
        for name, properties in self._objects.items():
            if not re.fullmatch(r"canvas\d+", name):
                continue
            windows = [
                key.title()
                for key, props in self._objects.items()
                if key.startswith("window") and props.get("Canvas", "").lower() == name
            ]
            properties["WindowList"] = ",".join(windows)

    def _emit_property(self, parent, prop, value):
        if parent.startswith("window") and prop == "Input":
            self.emit_event("WINDOW", "INPUT", parent, value)
        elif parent.startswith("canvas"):
            self._emit_canvas(parent, prop, value)
        elif ".in" in parent:
            self._emit_input(parent, prop, value)
        elif ".out" in parent:
            self._emit_output(parent, prop, value)
        elif parent.startswith("layout"):
            self.emit_event("LAYOUT", "PROPERTY_CHANGED", parent, prop, value)

    def _emit_input(self, parent, prop, value):
        if prop in _INPUT_EVENTS:
            self.emit_event("INPUT", "STATUS_GROUP", parent, prop, value)

    def _emit_canvas(self, parent, prop, value):
        if prop == "StbdCurrent":
            self.emit_event("CANVAS", "STBDCURRENT_CHANGED", parent, value)
        elif prop in {
            "AudioMute",
            "AudioMode",
            "AudioFollowWindow",
            "AudioSource",
            "AudioVolume",
        }:
            self.emit_event("CANVAS", "PROPERTY_CHANGED", parent, prop, value)

    def _emit_output(self, parent, prop, value):
        if prop in {"AudioEnable", "AudioMute", "CutToBlack"}:
            self.emit_event("OUTPUT", "PROPERTY_CHANGED", parent, prop, value)
        elif prop in {"HDCPActive", "HDMI", "FramelockStatus", "Genlock"}:
            self.emit_event("OUTPUT", "STATUS_GROUP", parent, prop, value)

    def emit_event(self, category, event, *args):
        if self.has_error_behavior("no_response"):
            return
        data = (
            "!Event " + ",".join(map(str, (category, event, *args))) + "\r\n"
        ).encode()
        for client_id, subscriptions in self._subscriptions.items():
            if category in subscriptions and client_id in self._clients:
                task = asyncio.create_task(self.push_to(client_id, data))
                self._push_tasks.add(task)
                task.add_done_callback(self._push_tasks.discard)


def _reply(lines, echo):
    return ("\r\n".join([*lines, "!Done " + echo]) + "\r\n").encode()


def _failed(echo, message):
    return f"!Error : {message}\r\n!Failed {echo}\r\n".encode()


def _canonical(path):
    value = path.lower()
    for prefix in ("routing.", "slots.", "windows.", "canvases.", "layouts.", "stbds."):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value


def _system_objects(model):
    return {
        "device": {
            "ModelNumber": model,
            "SerialNumber": "SIM-CALICO-001",
            "SoftwareVersion": "1.6.2.5",
        },
        "system": {
            "APIVersion": "1.6.0",
            "Status": "Serving",
            "Alerts": "OK",
            "UnitDescription": "CALICO Demo Wall",
        },
        "system.sensors": {
            "BPTemperature_degC": "28",
            "BPFPGACore_degC": "42",
            "PSU1_degC": "31",
            "PSU2_degC": "30",
        },
        "system.temperaturecontrol": {
            "FanInlet0_RPM": "3939",
            "FanInlet1_RPM": "3976",
            "FanInlet2_RPM": "0",
        },
        "outputssync": {
            "LockMode": "Synclock",
            "LockSource": "NULL",
            "LockStatus": "Locked",
            "LockAccuracy": "1024",
        },
    }


def _example_wall(model):
    objects = _system_objects(model)
    objects.update(
        {
            "slots": {
                f"Slot{n}": "<...>" if n <= 3 else "NO CARD" for n in range(1, 13)
            },
            "slot1": {"Cardtype": "HDMI_4K 4-in", "In1": "<...>", "In2": "<...>"},
            "slot2": {"Cardtype": "MEDIA_4KIN", "Status": "READY", "In1": "<...>"},
            "slot3": {"Cardtype": "HDMI_4K 2-out", "Out1": "<...>", "Out2": "<...>"},
            "canvases": {"Canvas1": "<...>", "Canvas2": "<...>"},
            "layouts": {"Layout1": "<...>", "Layout2": "<...>", "OutputsSync": "<...>"},
            "stbd1": {
                "Name": "Presentation",
                "Canvas": "Canvas1",
                "Duration": "0",
                "IsCurrent": "Yes",
            },
            "stbd57": {
                "Name": "Two sources",
                "Canvas": "Canvas1",
                "Duration": "1000",
                "IsCurrent": "No",
            },
        }
    )
    for number in (1, 2):
        objects[f"slot1.in{number}"] = _input("Presenter" if number == 1 else "Guest")
        objects[f"slot3.out{number}"] = _output(number)
        objects[f"canvas{number}"] = _canvas(number)
        objects[f"layout{number}"] = {
            "Alias": f"Display Layout {number}",
            "Canvas": f"Canvas{number}",
            "OutputList": f"Slot3.Out{number}",
            "FrameRate": "60",
            "Lock": "Yes",
            "StbdActive": "No",
        }
        objects[f"window{number}"] = _window(number)
    objects["slot2.in1"] = {
        "Alias": "Signage Player",
        "Status": "OK",
        "MeasuredResolution": "1920x1080p60",
        "ActiveQueue": "<...>",
    }
    objects["slot2.in1.activequeue"] = {
        "Status": "Idle",
        "CurrentIndex": "1",
        "PlayMode": "Single",
    }
    # A free window is intentionally absent from both canvases' WindowList.
    objects["window256"] = {"Alias": "NULL", "Canvas": "NULL", "Input": "Slot1.In1"}
    return objects


def _input(name):
    return {
        "Alias": name,
        "HDMI": "Found",
        "MeasuredResolution": "1920x1080p60",
        "HDCPStatus": "None",
        "HDCPRequired": "Off",
        "Audio": "Found",
        "ColourRangeActive": "SDR_YCCL_709",
        "CutToBlack": "Off",
        "Brightness": "0",
        "Contrast": "100",
    }


def _output(number):
    return {
        "Alias": "Left Display" if number == 1 else "Right Display",
        "HDMI": "Found",
        "Resolution": "1920x1080p60",
        "Layout": f"Layout{number}",
        "HDCPActive": "Off",
        "HDCPStatus": "None",
        "FramelockStatus": "Locked",
        "CutToBlack": "Off",
        "AudioEnable": "On",
        "AudioMute": "Off",
        "EcoMode": "Off",
    }


def _canvas(number):
    return {
        "Alias": "Main Wall" if number == 1 else "Confidence Wall",
        "WindowList": f"Window{number}",
        "LayoutList": f"Layout{number}",
        "StbdCurrent": "Stbd1" if number == 1 else "NULL",
        "AudioFollowWindow": str(number),
        "AudioMute": "Off",
        "AudioSource": "Slot1.In1",
        "AudioMode": "FromSource",
        "AudioVolume": "75",
        "CutToBlack": "Off",
    }


def _window(number):
    return {
        "Alias": "Presenter Window" if number == 1 else "Confidence Window",
        "Input": f"Slot1.In{number}",
        "Canvas": f"Canvas{number}",
        "Mute": "Off",
        "Width": "1920",
        "Height": "1080",
        "CenterX": "0",
        "CenterY": "0",
        "Zorder": str(number),
        "Rotation": "0.00",
        "FTB": "0",
        "HFlip": "Off",
        "VFlip": "Off",
        "SCFTB": "Off",
        "SCCrossFade": "Off",
    }
