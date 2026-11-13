"""LUNA-U ASCII simulator with independent zone mixers and example scenes."""

from __future__ import annotations

import re
from openavc.simulator.tcp_simulator import TCPSimulator

BANKS = {"ROUTE": "MIXER", "MIXER": "MIXER", "VOLUME": "VOLUME", "MUTE": "VOLUME"}
TARGET = re.compile(r"ZONE>([1-9]|[12][0-9]|3[0-2])>(MIXER|VOLUME)>1")
ROUTE_CONTROL = [
    "Mixed",
    "Off",
    *(f"Input {i}" for i in range(1, 17)),
    *(f"Scene {i}" for i in range(1, 9)),
]
SILENT = (-90,) * 16


def saved_scene(index):
    # The simulator's eight commissioned example scenes, each a two-input mix.
    levels = list(SILENT)
    levels[index - 1] = -6
    levels[index + 7] = -12
    return tuple(levels)


def mix_for(route):
    if route == -1:
        return (-10, -20, *SILENT[2:])
    if route > 16:
        return saved_scene(route - 16)
    return tuple(0 if i == route else -90 for i in range(1, 17))


def route_for(levels):
    for candidate in range(25):
        if levels == mix_for(candidate):
            return candidate
    return -1


def whole(value, low, high):
    if not re.fullmatch(r"-?[0-9]{1,2}", value):
        return None
    parsed = int(value)
    return parsed if low <= parsed <= high else None


def edit_mix(current, value):
    result = list(current)
    seen = set()
    for item in value.split("^"):
        index, _, raw = item.partition(">")
        slot, level = whole(index, 1, 16), whole(raw, -90, 0)
        if None in (slot, level) or slot in seen:
            return None
        result[slot - 1] = level
        seen.add(slot)
    return tuple(result)


REQUEST = re.compile(
    rb"#\|(LUNA_U(?:>[0-9]+)?)\|([^|]*)\|(SET_REQ|GET_REQ)\^([A-Z_0-9>]+)\^"
    rb"(ROUTE|VOLUME|MUTE|MIXER)\|([^|]*)\|U\|"
)


class AudacLunaUSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "audac_luna_u",
        "name": "AUDAC LUNA-U Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 5001,
        "delimiter": "\r\n",
        "initial_state": {
            "zone_1_route": "Mixed",
            "zone_1_volume": -24,
            "zone_1_mute": False,
        },
        "controls": [
            {
                "key": "zone_1_route",
                "label": "Zone 1 Source or Mix",
                "type": "select",
                "options": ROUTE_CONTROL,
            },
            {
                "key": "zone_1_volume",
                "label": "Zone 1 Volume (dB)",
                "type": "slider",
                "min": -90,
                "max": 0,
                "step": 1,
            },
            {"key": "zone_1_mute", "label": "Zone 1 Mute", "type": "toggle"},
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._levels = {1: mix_for(-1)}
        self.address = 1

    def set_state(self, key, value):
        match = re.fullmatch(r"zone_(\d+)_route", key)
        if match and value in ROUTE_CONTROL:
            self._levels[int(match[1])] = mix_for(ROUTE_CONTROL.index(value) - 1)
        super().set_state(key, value)

    def _mix(self, zone):
        return self._levels.get(zone, SILENT)

    def _read(self, zone, key):
        if key == "MIXER":
            return "^".join(f"{i}>{v}" for i, v in enumerate(self._mix(zone), 1))
        if key == "ROUTE":
            return str(route_for(self._mix(zone)))
        if key == "MUTE":
            return "TRUE" if self.get_state(f"zone_{zone}_mute", False) else "FALSE"
        return str(self.get_state(f"zone_{zone}_volume", -24))

    def _edit(self, zone, value):
        levels = edit_mix(self._mix(zone), value)
        if levels is None:
            return False
        self._levels[zone] = levels
        super().set_state(f"zone_{zone}_route", ROUTE_CONTROL[route_for(levels) + 1])
        return True

    def _write(self, zone, key, value):
        if key == "MIXER":
            return self._edit(zone, value)
        values = {"TRUE": True, "FALSE": False}
        if key == "MUTE":
            valid = values.get(value)
        elif key == "VOLUME":
            valid = whole(value, -90, 0)
        else:
            route = whole(value, 0, 24)
            valid = ROUTE_CONTROL[route + 1] if route is not None else None
        if valid is None:
            return False
        self.set_state(f"zone_{zone}_{key.lower()}", valid)
        return True

    def _value(self, kind, target, key, arg):
        if target == "ALL_ZONES":
            if kind != "GET_REQ" or key == "MIXER":
                return None
            return "^".join(self._read(i, key) for i in range(1, 33))
        match = TARGET.fullmatch(target)
        if not match or match[2] != BANKS[key]:
            return None
        zone = int(match[1])
        if kind == "SET_REQ" and not self._write(zone, key, arg):
            return None
        return self._read(zone, key)

    def handle_command(self, data):
        match = REQUEST.fullmatch(data.rstrip(b"\r\n"))
        if not match:
            return None
        dest, source, kind, target, key, arg = (
            part.decode("ascii") for part in match.groups()
        )
        if dest not in ("LUNA_U", "LUNA_U>0", f"LUNA_U>{self.address}"):
            return None
        if kind == "GET_REQ" and arg:
            return None
        value = self._value(kind, target, key, arg)
        if value is None:
            return None
        return f"#|{source}|LUNA_U>{self.address}|GET_RSP^{target}^{key}|{value}|U|\r\n".encode(
            "ascii"
        )
