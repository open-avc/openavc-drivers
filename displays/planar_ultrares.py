"""Planar UltraRes L and X multi-source displays.

Python enumerates multi-page, sparse preset slots and their names and builds
the input roster from the model's own report. L and X share the control words.
X reference: https://www.planar.com/media/tghnvl0f/020-1397-00b_ultrares-x-series_rs232-user-manual-wm.pdf
"""

from __future__ import annotations

import asyncio
import json
import re

from openavc.core.connection_fault import ConnectionFaultError
from openavc.drivers.base import BaseDriver

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:
    CommandRefusedError = RuntimeError

INPUTS = {1: "HDMI.1", 2: "HDMI.2", 3: "HDMI.3", 4: "HDMI.4", 5: "DP", 6: "OPS"}
INPUT_LABELS = {
    1: "HDMI 1",
    2: "HDMI 2",
    3: "HDMI 3",
    4: "HDMI 4",
    5: "DisplayPort",
    6: "OPS",
}
INPUT_IDS = {word: number for number, word in INPUTS.items()}
LAYOUTS = {
    "SINGLE": "Single",
    "DUAL": "Dual",
    "TRIPLE": "Triple",
    "QUAD": "Quad",
    "PIP": "Picture in Picture",
}
MODEL = re.compile(
    r"URL(?:109|122)(?:-MFT)?|URL136(?:-MFT|-T)?|URX(?:75|85|100)(?:-ERO(?:-T)?)?", re.I
)
REPLY = re.compile(
    r"([\w.]+(?:\([\w.]+\))?)\s*(:|=|@ACK|\^NAK|!ERR)\s*(.*)", re.I | re.S
)
ZONES = range(1, 5)


def integer(value, low, high):
    if not re.fullmatch(r"\d+", str(value)) or not low <= int(value) <= high:
        raise CommandRefusedError(f"Choose a whole number from {low} to {high}.")
    return int(value)


def switch(value):
    try:
        return {"ON": True, "OFF": False, "1": True, "0": False}[value.upper()]
    except KeyError as exc:
        raise CommandRefusedError(
            "The display returned an unrecognized power or mute reading."
        ) from exc


def unquote(value):
    if value.startswith('"') and value.endswith('"'):
        return re.sub(r'\\(["\\])', r"\1", value[1:-1])
    return value


class PlanarUltraResDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "planar_ultrares",
        "name": "Planar UltraRes L / X",
        "manufacturer": "Planar",
        "category": "display",
        "version": "1.0.0",
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "source_url": "https://www.planar.com/media/iuant51m/planar-ultrares-l-series-rs232-user-manual.pdf",
        "description": "Route sources to four screen zones, switch multi-view layouts, follow audio and recall "
        "presets by their names. Detects the X-series OPS input.",
        "tags": ["video-wall", "multiview", "microled", "rs232"],
        "protocols": ["planar_ultrares"],
        "ports": [57],
        "simulated": True,
        "verified": False,
        "compatible_models": [
            {
                "manufacturer": "Planar",
                "models": ["URL109-MFT", "URL122-MFT", "URL136-MFT", "URL136-T"],
                "confidence": "untested",
                "notes": "UltraRes L LED displays. Four HDMI sources and DisplayPort. "
                "Manufacturer documentation and simulation.",
            },
            {
                "manufacturer": "Planar",
                "models": [
                    "URX75",
                    "URX75-ERO",
                    "URX75-ERO-T",
                    "URX85",
                    "URX85-ERO",
                    "URX85-ERO-T",
                    "URX100",
                    "URX100-ERO",
                    "URX100-ERO-T",
                ],
                "confidence": "untested",
                "notes": "UltraRes X LCD displays. Adds the OPS source. Manufacturer "
                "documentation and simulation.",
            },
        ],
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "delimiter": "\r",
        "default_config": {
            "host": "",
            "port": 57,
            "baudrate": 19200,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "poll_interval": 15,
            "model": "URL136-MFT",
        },
        "config_schema": {
            "host": {"type": "string", "label": "Display IP address", "required": True},
            "port": {
                "type": "integer",
                "label": "TCP control port",
                "default": 57,
                "min": 1,
                "max": 65535,
            },
            "model": {
                "type": "string",
                "label": "Model for simulation",
                "default": "URL136-MFT",
                "learned_from": "model",
                "description": "For offline design, enter the model printed on the "
                "display. Live control reads the model from the display.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status refresh (seconds)",
                "default": 15,
                "min": 0,
                "description": "Tracks front-panel and remote changes. Set to 0 "
                "for manual refresh.",
            },
        },
        "help": {
            "overview": "Select the source and inspect the signal for each of four zones, choose a "
            "multi-source layout, select which zone supplies audio, and recall presets using "
            "names read from the display.",
            "setup": "On the display, select Networked Standby or Fast Startup under Power Down Mode so "
            "OpenAVC can wake it. For Ethernet, enable Network Commands and enter the display IP "
            "address (TCP 57). For RS-232, use a straight-through cable and select the local "
            "serial port or Through a bridge. Set the controller or bridge to 19200 baud, 8 data "
            "bits, no parity, one stop bit and no flow control.",
            "connection": "Power on once to identify the model and populate its source list. The X series "
            "includes OPS for an installed slot computer. Choose a layout, route each zone, "
            "and check its Signal reading. Refresh Saved Presets after adding or renaming "
            "presets on the display.",
        },
        "discovery": {
            "tcp_probe": {
                "port": 57,
                "send_ascii": "MODEL.ID?\r",
                "expect_regex": '(?i)MODEL\\.ID\\s*[:=]\\s*"?((?:URL(?:109|122)(?:-MFT)?|URL136(?:-MFT|-T)?|URX(?:75|85|100)(?:-ERO(?:-T)?)?))"?(?:[\\r\\n]|$)',
                "extract_manufacturer": "Planar",
                "extract": {
                    "model": {
                        "regex": '(?i)MODEL\\.ID\\s*[:=]\\s*"?((?:URL(?:109|122)(?:-MFT)?|URL136(?:-MFT|-T)?|URX(?:75|85|100)(?:-ERO(?:-T)?)?))"?(?:[\\r\\n]|$)'
                    }
                },
            }
        },
        "state_variables": {
            "power": {
                "type": "boolean",
                "label": "Power",
                "control": True,
                "cloud_priority": "high",
            },
            "model": {"type": "string", "label": "Reported model"},
            "serial_number": {"type": "string", "label": "Serial number"},
            "firmware": {"type": "string", "label": "Firmware version"},
            "system_state": {
                "type": "string",
                "label": "Display status",
                "cloud_priority": "high",
            },
            "standby_mode": {"type": "string", "label": "Power down mode"},
            "layout": {
                "type": "enum",
                "label": "Layout",
                "values": ["Single", "Dual", "Triple", "Quad", "Picture in Picture"],
                "control": True,
            },
            "audio_zone": {
                "type": "integer",
                "label": "Audio zone",
                "min": 1,
                "max": 4,
                "step": 1,
                "control": True,
            },
            "volume": {
                "type": "integer",
                "label": "Volume",
                "min": 0,
                "max": 100,
                "step": 1,
                "control": True,
            },
            "muted": {"type": "boolean", "label": "Muted", "control": True},
            "intensity": {
                "type": "integer",
                "label": "Panel intensity",
                "min": 1,
                "max": 100,
                "step": 1,
                "control": True,
            },
            "preset_count": {"type": "integer", "label": "Saved presets"},
            "preset_options": {"type": "string", "label": "Saved preset choices"},
        },
        "child_entity_types": {
            "input": {
                "label": "Source",
                "label_plural": "Sources",
                "id_format": {"type": "integer", "min": 1, "max": 6},
                "state_variables": {},
            },
            "zone": {
                "label": "Zone",
                "label_plural": "Screen zones",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "instances": {"count": 4, "label": "Zone {id}"},
                "state_variables": {
                    "source": {
                        "type": "integer",
                        "label": "Selected input",
                        "min": 1,
                        "max": 6,
                        "step": 1,
                        "control": True,
                    },
                    "signal": {
                        "type": "string",
                        "label": "Signal",
                        "cloud_priority": "high",
                        "help": "Resolution and frame rate, "
                        "Searching, or No Signal, as "
                        "reported by the display.",
                    },
                },
                "summary_fields": ["source", "signal"],
            },
        },
        "routing": {
            "source_child_type": "input",
            "destination_child_type": "zone",
            "source_param": "input",
            "destination_param": "zone",
            "command": "route",
            "planes": [{"label": "Video", "route_property": "source"}],
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "help": "Wake the display. Its power and picture status show when it is "
                "ready.",
                "sets": {"power": True},
            },
            "power_off": {
                "label": "Enter Standby",
                "help": "Turn off the picture and put the display in its configured standby "
                "mode.",
                "sets": {"power": False},
                "confirm": "Put the display in standby?",
            },
            "set_layout": {
                "label": "Select Layout",
                "help": "Choose how many sources to show on screen.",
                "params": {
                    "layout": {
                        "type": "enum",
                        "label": "Layout",
                        "values": [
                            "Single",
                            "Dual",
                            "Triple",
                            "Quad",
                            "Picture in Picture",
                        ],
                        "required": True,
                    }
                },
                "sets": {"layout": "{layout}"},
            },
            "route": {
                "label": "Route Source to Zone",
                "help": "Show the selected input in the chosen zone. Select a layout that shows "
                "that zone to see its picture.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Source",
                        "child_type": "input",
                        "required": True,
                    },
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    },
                },
                "sets": {"source": "{input}"},
            },
            "select_audio": {
                "label": "Listen to Zone",
                "help": "Play the audio from the selected zone.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    }
                },
                "sets": {"audio_zone": "{zone}"},
            },
            "recall_preset": {
                "label": "Recall Saved Preset",
                "help": "Apply a setup saved on the display and read back the resulting "
                "layout, sources, audio and intensity.",
                "params": {
                    "preset": {
                        "type": "string",
                        "label": "Saved preset",
                        "options_state": "preset_options",
                        "required": True,
                    }
                },
            },
            "refresh_presets": {
                "label": "Refresh Saved Presets",
                "help": "Read the saved slot numbers and names again after editing "
                "presets on the display.",
                "query_for": "preset_options",
            },
            "read_status": {
                "label": "Read Display Status",
                "help": "Check power and refresh the layout, audio and zone readings when "
                "the display is on.",
                "query_for": "power",
            },
            "mute": {
                "label": "Mute Audio",
                "help": "Silence display audio.",
                "sets": {"muted": True},
            },
            "unmute": {
                "label": "Unmute Audio",
                "help": "Restore display audio at the current level.",
                "sets": {"muted": False},
            },
            "set_volume": {
                "label": "Set Volume",
                "help": "Set the display audio level from 0 to 100.",
                "params": {
                    "level": {
                        "type": "integer",
                        "label": "Volume",
                        "min": 0,
                        "max": 100,
                        "required": True,
                    }
                },
                "sets": {"volume": "{level}"},
            },
            "set_intensity": {
                "label": "Set Panel Intensity",
                "help": "Adjust light output across the whole display from 1 to 100.",
                "params": {
                    "level": {
                        "type": "integer",
                        "label": "Panel Intensity",
                        "min": 1,
                        "max": 100,
                        "required": True,
                    }
                },
                "sets": {"intensity": "{level}"},
            },
        },
        "actions": [
            {"id": "set_layout"},
            {"id": "route"},
            {"id": "recall_preset"},
            {"id": "select_audio"},
            {"id": "set_volume"},
            {"id": "set_intensity"},
            {"id": "refresh_presets"},
        ],
        "quick_actions": ["power_on", "set_layout", "route", "recall_preset"],
        "device_settings": {
            "intensity": {
                "type": "integer",
                "label": "Panel intensity",
                "min": 1,
                "max": 100,
                "state_key": "intensity",
                "help": "Sets the whole display’s light output. A preset may recall "
                "another intensity.",
                "setup": False,
            }
        },
    }

    HEALTH_INTERVAL_S = 30
    HEALTH_TIMEOUT_S = 5
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The display stopped answering its power query."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wire = asyncio.Lock()
        self._preset_lock = asyncio.Lock()
        self._pending = None
        self._model = None
        self._presets_loaded = False

    def set_project_child_entities(self, child_entities):
        super().set_project_child_entities(child_entities)
        self._register_zones()
        self._sync_inputs(self.config.get("model", "").upper())

    def _register_zones(self):
        for zone in ZONES:
            self.register_child("zone", zone)
            if not self.get_child_state("zone", zone).get("label"):
                self.set_child_state("zone", zone, "label", f"Zone {zone}")

    def _sync_inputs(self, model):
        ports = set(range(1, 7 if model.startswith("URX") else 6))
        for port in set(self.list_children("input")) - ports:
            self.deregister_child("input", port)
        for port in sorted(ports):
            self.register_child("input", port)
            if not self.get_child_state("input", port).get("label"):
                self.set_child_state("input", port, "label", INPUT_LABELS[port])

    async def _ask(self, key, operation="?"):
        async with self._wire:
            if self.transport is None:
                raise ConnectionError("The display control connection is offline.")
            future = asyncio.get_running_loop().create_future()
            self._pending = (key, operation, future)
            try:
                await self.transport.send((key + operation + "\r").encode("ascii"))
                return await asyncio.wait_for(future, 2.5)
            finally:
                self._pending = None
                future.cancel()

    async def on_data_received(self, data):
        if not self._pending:
            return
        key, operation, future = self._pending
        match = REPLY.fullmatch(data.decode("utf-8", "replace").strip())
        if not match or future.done() or match[1].upper() != key:
            return
        marker, value = match[2].upper(), match[3].strip()
        if marker in ("!ERR", "^NAK") or re.fullmatch(r"ERR \d+", value, re.I):
            future.set_exception(
                CommandRefusedError(f"Planar refused {key} ({marker} {value}).")
            )
        elif (marker == "@ACK") == (operation == ""):
            future.set_result(unquote(value))

    async def _close_session(self):
        if self._pending and not self._pending[2].done():
            self._pending[2].set_exception(
                ConnectionError("The display connection closed.")
            )

    async def _post_connect(self):
        self._register_zones()
        self._model = None
        self._presets_loaded = False
        self.set_state("preset_options", "[]")
        if await self._power() and await self._ready():
            await self._identify()

    async def _initial_sync(self):
        self.set_state("firmware", await self._ask("BUILD.INFO(PKG.VERSION)"))
        await self.poll()

    async def _power(self):
        on = switch(await self._ask("DISPLAY.POWER"))
        self.set_state("power", on)
        if not on:
            self.set_state("system_state", "Standby")
            for key in ("layout", "volume", "muted", "audio_zone", "intensity"):
                self.set_state(key, None)
            for zone in ZONES:
                self.set_child_state("zone", zone, "signal", None)
                self.set_child_state("zone", zone, "source", None)
        return on

    async def _identify(self):
        model = (await self._ask("MODEL.ID")).upper()
        self.set_state("model", model)
        if not MODEL.fullmatch(model):
            self._model = None
            raise ConnectionFaultError(
                f"The display reports {model}. Expected a Planar UltraRes L or X model.",
                code="invalid_config",
            )
        self._model = model
        self._sync_inputs(model)
        self.set_state("serial_number", await self._ask("SERIAL.NUMBER"))

    async def _liveness_probe(self):
        await self._power()

    async def _ready(self):
        state = (await self._ask("SYSTEM.STATE")).upper()
        self.set_state("system_state", state.replace(".", " ").title())
        return state in ("ON", "BACKLIGHT.OFF")

    async def _read_room(self):
        layout = await self._ask("MULTI.VIEW")
        self.set_state("layout", LAYOUTS.get(layout))
        self.set_state("muted", switch(await self._ask("AUDIO.MUTE")))
        self.set_state("volume", integer(await self._ask("AUDIO.VOLUME"), 0, 100))
        self.set_state(
            "intensity", integer(await self._ask("BACKLIGHT.INTENSITY"), 0, 100)
        )
        audio = await self._ask("AUDIO.ZONE")
        self.set_state("audio_zone", integer(audio.removeprefix("ZONE."), 1, 4))
        for zone in ZONES:
            await self._read_zone(zone)

    async def _read_zone(self, zone):
        source = await self._ask(f"SOURCE.SELECT(ZONE.{zone})")
        self.set_child_state("zone", zone, "source", INPUT_IDS.get(source))
        signal = await self._ask(f"SOURCE.MESSAGE(ZONE.{zone})")
        self.set_child_state("zone", zone, "signal", signal)

    async def poll(self):
        if not await self._power() or not await self._ready():
            return
        if not self._model:
            await self._identify()
        await self._read_room()
        self.set_state(
            "standby_mode",
            (await self._ask("POWER.DOWN.MODE")).replace(".", " ").title(),
        )
        if not self._presets_loaded:
            await self._refresh_presets()

    async def refresh_children(self):
        await self.poll()

    async def _preset_ids(self, count):
        slots = []
        modifier = "FIRST"
        while len(slots) < count:
            page = await self._ask(f"PRESET.LIST({modifier})")
            ids = [integer(word, 1, 1000) for word in page.split()]
            combined = slots + ids
            if not ids or len(set(combined)) != len(combined) or len(combined) > count:
                raise CommandRefusedError(
                    "The saved preset list changed. Refresh Saved Presets again."
                )
            slots = combined
            modifier = "NEXT"
        return slots

    async def _refresh_presets(self):
        async with self._preset_lock:
            count = integer(await self._ask("PRESET.COUNT"), 0, 100)
            options = []
            for slot in await self._preset_ids(count):
                name = await self._ask(f"PRESET.NAME({slot})")
                options.append(
                    {
                        "value": str(slot),
                        "label": f"{slot} · {name or 'Unnamed preset'}",
                    }
                )
            self.set_state("preset_count", count)
            self.set_state("preset_options", json.dumps(options, ensure_ascii=False))
            self._presets_loaded = True

    async def _set(self, key, value):
        await self._ask(key, "=" + value)
        actual = await self._ask(key)
        if actual != value:
            raise CommandRefusedError(
                f"The display reported {actual} after setting {key} to {value}."
            )
        return actual

    async def _route(self, params):
        zone = integer(params.get("zone"), 1, 4)
        source = integer(
            params.get("input"), 1, 6 if self._model.startswith("URX") else 5
        )
        key = f"SOURCE.SELECT(ZONE.{zone})"
        await self._set(key, INPUTS[source])
        await self._read_zone(zone)

    async def _recall(self, params):
        slot = integer(params.get("preset"), 1, 1000)
        if (await self._ask(f"PRESET.FULL({slot})")).upper() not in ("YES", "1"):
            raise CommandRefusedError(f"Preset {slot} is empty. Refresh Saved Presets.")
        await self._ask(f"PRESET.RECALL({slot})", "")
        await self._read_room()

    async def _set_room_control(self, command, params):
        if command == "set_layout":
            words = {label: word for word, label in LAYOUTS.items()}
            if params.get("layout") not in words:
                raise CommandRefusedError("Choose a listed layout.")
            await self._set("MULTI.VIEW", words[params["layout"]])
            await self._read_room()
        elif command == "select_audio":
            zone = integer(params.get("zone"), 1, 4)
            actual = await self._set("AUDIO.ZONE", f"ZONE.{zone}")
            self.set_state("audio_zone", int(actual.removeprefix("ZONE.")))
        elif command in ("mute", "unmute"):
            self.set_state(
                "muted",
                switch(
                    await self._set("AUDIO.MUTE", "ON" if command == "mute" else "OFF")
                ),
            )
        else:
            key, state, minimum = {
                "set_volume": ("AUDIO.VOLUME", "volume", 0),
                "set_intensity": ("BACKLIGHT.INTENSITY", "intensity", 1),
            }[command]
            value = integer(params.get("level"), minimum, 100)
            self.set_state(state, int(await self._set(key, str(value))))

    async def _change_power(self, command):
        wanted = "ON" if command == "power_on" else "OFF"
        await self._ask("DISPLAY.POWER", "=" + wanted)
        await self.poll()

    async def _operate(self, command, params):
        if not await self._power():
            raise CommandRefusedError(
                "Power on the display before changing its picture or audio."
            )
        if not self._model:
            await self._identify()
        if command == "route":
            return await self._route(params)
        if command == "recall_preset":
            return await self._recall(params)
        return await self._set_room_control(command, params)

    async def send_command(self, command, params=None):
        if command not in self.DRIVER_INFO["commands"]:
            raise CommandRefusedError("Unknown UltraRes command.")
        if command == "read_status":
            return await self.poll()
        if command == "refresh_presets":
            return await self._refresh_presets()
        if command in ("power_on", "power_off"):
            return await self._change_power(command)
        return await self._operate(command, params or {})

    async def set_device_setting(self, name, value):
        if name != "intensity":
            raise CommandRefusedError("Unknown UltraRes display setting.")
        await self.send_command("set_intensity", {"level": value})
