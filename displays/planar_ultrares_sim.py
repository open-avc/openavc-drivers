"""Simulator for the Planar UltraRes L and X ASCII control interface."""

import json
import re

from openavc.simulator.tcp_simulator import TCPSimulator


LAYOUTS = ("SINGLE", "DUAL", "TRIPLE", "QUAD", "PIP")
SOURCES = ("HDMI.1", "HDMI.2", "HDMI.3", "HDMI.4", "DP")
ZONES = ("ZONE.1", "ZONE.2", "ZONE.3", "ZONE.4")
LEVELS = {"AUDIO.VOLUME": ("volume", 0), "BACKLIGHT.INTENSITY": ("intensity", 1)}
REQUEST = re.compile(r"([\w.]+)(?:\(([\w.]+)\))?([?=]?)(.*)")
READ_ONLY = {
    "MODEL.ID": "model",
    "SERIAL.NUMBER": "serial_number",
    "POWER.DOWN.MODE": "standby_mode",
    "SYSTEM.STATE": "system_state",
}
PRESETS = {
    1: ("Presentation", "SINGLE", ("HDMI.1", "HDMI.2", "HDMI.3", "HDMI.4")),
    34: ("Four sources", "QUAD", ("DP", "HDMI.2", "HDMI.3", "HDMI.4")),
    875: ("Speaker and slides", "PIP", ("HDMI.3", "DP", "HDMI.2", "HDMI.4")),
}


class PlanarUltraResSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "planar_ultrares",
        "name": "Planar UltraRes L / X Simulator",
        "category": "display",
        "transport": "tcp",
        "default_port": 57,
        "delimiter": "\r",
        "initial_state": {
            "model": "URL136-MFT",
            "serial_number": "SIM-PLANAR-001",
            "firmware": "1.0.6",
            "power": True,
            "system_state": "ON",
            "standby_mode": "NETWORKED.STANDBY.MODE",
            "volume": 40,
            "muted": False,
            "audio_zone": 1,
            "intensity": 75,
            "layout": "Single",
            "preset_count": 3,
            "preset_options": "[]",
            "source_options": "[]",
            "source_1": "HDMI.1",
            "source_2": "HDMI.2",
            "source_3": "HDMI.3",
            "source_4": "HDMI.4",
            "hdmi1_signal": "3840x2160p 60Hz",
            "hdmi2_signal": "1920x1080p 60Hz",
            "hdmi3_signal": "Searching",
            "dp_signal": "3840x2160p 60Hz",
            "ops_signal": "1920x1080p 60Hz",
            "hdmi4_signal": "No Signal",
        },
        "controls": [
            {"type": "toggle", "key": "power", "label": "Display powered on"},
            {
                "type": "select",
                "key": "model",
                "label": "Model (reconnect after changing)",
                "options": [
                    "URL109-MFT",
                    "URL122-MFT",
                    "URL136-MFT",
                    "URL136-T",
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
            },
            {
                "type": "select",
                "key": "layout",
                "label": "Front-panel layout",
                "options": ["Single", "Dual", "Triple", "Quad", "Picture in Picture"],
            },
            {
                "type": "select",
                "key": "hdmi1_signal",
                "label": "HDMI 1 signal",
                "options": [
                    "3840x2160p 60Hz",
                    "1920x1080p 60Hz",
                    "Searching",
                    "No Signal",
                ],
            },
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "Display stops replying.",
                "behavior": "no_response",
            }
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.presets = dict(PRESETS)
        self._cursor = 0
        if self.config.get("model"):
            self.set_state("model", self.config["model"])

    def handle_command(self, data):
        request = data.decode("ascii", "replace").strip().upper()
        match = REQUEST.fullmatch(request)
        if not match:
            return b"!ERR 1\r"
        word, modifier, op, operand = match.groups()
        key = word + ("(" + modifier + ")" if modifier else "")
        try:
            answer = self._command(word, modifier, op, operand)
        except (ValueError, KeyError):
            answer = "!ERR 5"
        return (key + answer + "\r").encode("utf-8")

    def _command(self, word, modifier, op, operand):
        if word == "DISPLAY.POWER":
            return self._switch("power", op, operand)
        if (word, modifier, op) == ("BUILD.INFO", "PKG.VERSION", "?"):
            return ":" + json.dumps(self.state["firmware"])
        if word.startswith("PRESET."):
            return self._preset(word, modifier, op)
        if not self.state["power"]:
            return "^NAK"
        if word in READ_ONLY:
            return (
                ":" + json.dumps(self.state[READ_ONLY[word]]) if op == "?" else "!ERR 6"
            )
        return self._picture(word, modifier, op, operand)

    def _picture(self, word, modifier, op, operand):
        if word in LEVELS:
            return self._level(word, op, operand)
        if word == "AUDIO.MUTE":
            return self._switch("muted", op, operand)
        if word == "MULTI.VIEW":
            return self._layout(op, operand)
        if word == "AUDIO.ZONE":
            return self._audio_zone(op, operand)
        return self._zone(word, modifier, op, operand)

    def _audio_zone(self, op, operand):
        if op == "=":
            self.set_state("audio_zone", ZONES.index(operand) + 1)
        return ":" + ZONES[self.state["audio_zone"] - 1]

    def _level(self, word, op, operand):
        key, low = LEVELS[word]
        if op == "=" and not low <= int(operand) <= 100:
            return "!ERR 5"
        return self._value(key, op, operand, int)

    def _zone(self, word, modifier, op, operand):
        if word not in ("SOURCE.SELECT", "SOURCE.MESSAGE"):
            return "!ERR 3"
        if modifier not in ZONES:
            return "!ERR 4"
        zone = ZONES.index(modifier) + 1
        if word == "SOURCE.MESSAGE":
            return (
                ":"
                + json.dumps(
                    self.state[
                        self.state[f"source_{zone}"].replace(".", "").lower()
                        + "_signal"
                    ]
                )
                if op == "?"
                else "!ERR 6"
            )
        if op == "=":
            sources = SOURCES + (
                ("OPS",) if self.state["model"].startswith("URX") else ()
            )
            if operand not in sources:
                return "!ERR 5"
        return self._value(f"source_{zone}", op, operand, str)

    def _layout(self, op, operand):
        names = ("Single", "Dual", "Triple", "Quad", "Picture in Picture")
        if op == "=":
            self.set_state("layout", names[LAYOUTS.index(operand)])
        return ":" + LAYOUTS[names.index(self.state["layout"])]

    def _value(self, key, op, operand, convert):
        if op == "=":
            self.set_state(key, convert(operand))
        return ":" + str(self.state[key]) if op in ("=", "?") else "!ERR 6"

    def _switch(self, key, op, operand):
        if op == "=":
            self.set_state(key, {"ON": True, "OFF": False}[operand])
        return ":ON" if self.state[key] else ":OFF"

    def _preset(self, word, modifier, op):
        if (word, op) == ("PRESET.COUNT", "?"):
            return ":" + str(len(self.presets))
        if (word, op) == ("PRESET.LIST", "?"):
            return self._preset_page(modifier)
        slot = int(modifier or 0)
        if (word, op) == ("PRESET.FULL", "?"):
            return ":YES" if slot in self.presets else ":NO"
        if slot not in self.presets:
            return "^NAK"
        if (word, op) == ("PRESET.NAME", "?"):
            return ":" + json.dumps(self.presets[slot][0], ensure_ascii=False)
        if (word, op) == ("PRESET.RECALL", ""):
            return self._recall(slot)
        return "!ERR 6"

    def _preset_page(self, modifier):
        if modifier == "FIRST":
            self._cursor = 0
        elif modifier != "NEXT":
            return "!ERR 4"
        slots = sorted(self.presets)[self._cursor : self._cursor + 64]
        self._cursor += len(slots)
        return ":" + " ".join(map(str, slots))

    def _recall(self, slot):
        if not self.state["power"]:
            return "^NAK"
        _, layout, sources = self.presets[slot]
        self._layout("=", layout)
        for zone, source in enumerate(sources, 1):
            self.set_state(f"source_{zone}", source)
        return "@ACK"
