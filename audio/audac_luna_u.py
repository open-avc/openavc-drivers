"""AUDAC LUNA-U ASCII v1.6.3 over the unit's dedicated TCP control socket.

Mixer slot numbers are relative to each zone. Only a ROUTE report identifies
a route or scene. Indexed MIXER reports describe the faders independently.
Python expires individual readings at disconnect and assembles partial mixer
updates. The ASCII manual defines requests and replies, so poll for local edits.
"""

from __future__ import annotations

import asyncio
import re
import zlib

from openavc.drivers.base import BaseDriver, CommandRefusedError

TIMEOUT = 2.0
BLOCK = {"ROUTE": "MIXER", "MIXER": "MIXER", "MUTE": "VOLUME", "VOLUME": "VOLUME"}
PROP = {"ROUTE": "route", "MUTE": "mute", "VOLUME": "volume"}
READ = {
    "read_zone_route": "ROUTE",
    "read_zone_volume": "VOLUME",
    "read_zone_mute": "MUTE",
    "read_zone_mix": "MIXER",
}
SIMPLE_WRITES = {
    "route_zone": ("ROUTE", "source", 0, 24),
    "set_zone_volume": ("VOLUME", "level", -90, 0),
}
BOOL = {"TRUE": True, "FALSE": False}
FADERS = tuple(f"mix_{i}" for i in range(1, 17))
SINGLE = re.compile(r"ZONE>([1-9][0-9]?)>(VOLUME|MIXER)>1")
NUMBER = re.compile(r"-?[0-9]{1,2}")


def number(text, low, high):
    if NUMBER.fullmatch(str(text)):
        value = int(text)
        if low <= value <= high:
            return value
    return None


def required(params, name, low, high):
    value = number(params.get(name), low, high)
    if value is None:
        raise CommandRefusedError(f"Choose a whole {name} value from {low} to {high}.")
    return value


def configured_zones(raw):
    zones = []
    for item in str(raw).split(","):
        zone = number(item.strip(), 1, 32)
        if zone is None:
            raise ValueError(
                "Enter commissioned zone numbers from 1 to 32, separated by commas."
            )
        if zone not in zones:
            zones.append(zone)
    return zones


def route_label(route):
    if route == -1:
        return "Mixed"
    if route == 0:
        return "Off"
    if route <= 16:
        return f"Mapped input {route}"
    return f"Mixer scene {route - 16}"


def mix_levels(payload):
    """Parse the whole update before applying any part of it."""
    result = {}
    for pair in payload.split("^"):
        idx, _, raw = pair.partition(">")
        slot, level = number(idx, 1, 16), number(raw, -90, 0)
        if None in (slot, level) or slot in result:
            return {}
        result[slot] = level
    return result


def mix_detail(levels):
    if len(levels) != 16:
        return None
    active = [f"Input {i}: {levels[i]} dB" for i in sorted(levels) if levels[i] > -90]
    return ", ".join(active) or "All mixer inputs at -90 dB"


def crc16_arc(payload):
    value = 0
    for byte in payload:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return value


def checksum_ok(frame, crc):
    if crc == "U":
        return True
    payload = frame[1 : -(len(crc) + 1)].encode("ascii")
    if len(crc) == 4:
        return crc == f"{crc16_arc(payload):04X}"
    return crc == f"{zlib.crc32(payload):08X}" if len(crc) == 8 else False


REPLY = re.compile(
    r"#\|\|LUNA_U>([0-9]{1,9})\|GET_RSP\^([A-Z_0-9>]+)\^(MIXER|ROUTE|VOLUME|MUTE)\|"
    r"([A-Z0-9>^\-]*)\|(U|[0-9A-F]{4}|[0-9A-F]{8})\|"
)


def unpack(data):
    frame = data.decode("ascii", errors="replace").rstrip("\r\n")
    match = REPLY.fullmatch(frame)
    if match and checksum_ok(frame, match[5]):
        return match[1], match[2], match[3], match[4]
    return None


def reading(key, value):
    if key == "MIXER":
        return mix_levels(value) or None
    if key == "MUTE":
        return BOOL.get(value)
    return number(value, -90, 0) if key == "VOLUME" else number(value, -1, 24)


def reports(target, key, payload):
    if target == "ALL_ZONES" and key != "MIXER":
        items = payload.split("^")
        return enumerate(items, 1) if len(items) <= 32 else ()
    match = SINGLE.fullmatch(target)
    if match and match[2] == BLOCK[key] and int(match[1]) <= 32:
        return ((int(match[1]), payload),)
    return ()


class AudacLunaUDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "audac_luna_u",
        "name": "AUDAC LUNA-U",
        "manufacturer": "AUDAC",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "min_platform_version": "0.37.1",
        "description": "Control the LUNA-U zone matrix, individual mixer inputs, saved mixing scenes, master volume "
        "and mute. Each configured zone reports its route and actual mix levels.",
        "source_url": "https://audac.eu/eu/products/d/luna-u---unified-audio-matrix-processor/command-manual/pdf/command-manual.pdf",
        "tags": ["audio", "matrix", "mixer", "dsp", "zones"],
        "verified": False,
        "simulated": True,
        "ports": [5001],
        "compatible_models": [
            {
                "manufacturer": "AUDAC",
                "models": ["LUNA-U"],
                "confidence": "untested",
                "notes": "Exact-model ASCII v1.6.3 documentation and simulator. Physical verification pending.",
            }
        ],
        "transport": "tcp",
        "help": {
            "overview": "Bind a Matrix to Zone Routing, or bind individual zone controls to the source, mute "
            "and master fader. A custom mix reads Mixed. Its contributing mixer inputs and their "
            "levels appear under Mix Detail. Muting a zone leaves its route intact.",
            "setup": "Commission the input mappings and saved mixer scenes in AUDAC Touch. Enter the processor "
            "IP and the zone numbers used in this installation, such as 1,3,8. Keep those numbers "
            "when assigning project labels to zones. Inputs 1–16 are slots in each individual zone’s "
            "mapping. The same slot can carry different physical sources in different zones. The "
            "final eight choices address saved mixer scenes. Configure those scenes on the processor "
            "before recalling one.",
            "connection": "Uses the local ASCII control port, TCP 5001, with the LUNA-U v1.6.3 command set. "
            "The processor identifies its control address on connect. The driver reads route, "
            "master volume, mute and mixer levels again after reconnection. A missing report "
            "leaves its reading unknown.",
        },
        "default_config": {"host": "", "port": 5001, "zones": "1", "poll_interval": 5},
        "config_schema": {
            "host": {"type": "string", "label": "Processor Address", "required": True},
            "port": {
                "type": "integer",
                "label": "TCP Control Port",
                "default": 5001,
                "min": 1,
                "max": 65535,
            },
            "zones": {
                "type": "string",
                "label": "Commissioned Zone Numbers",
                "default": "1",
                "help": "Comma-separated zone IDs from 1 to 32 as configured in AUDAC Touch. "
                "For example, 1,3,8.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Feedback Refresh (seconds)",
                "default": 5,
                "min": 2,
                "max": 300,
                "advanced": True,
            },
        },
        "discovery": {
            "manufacturer_alias": ["AUDAC"],
            "port_open": [5001],
            "tcp_probe": {
                "port": 5001,
                "send_ascii": "#|LUNA_U||GET_REQ^ALL_ZONES^ROUTE||U|\r\n",
                "expect_regex": "#\\|\\|LUNA_U>[0-9]+\\|GET_RSP\\^(?:ALL_ZONES|ZONE>[0-9]+>MIXER>1)\\^ROUTE\\|",
                "extract": {"model": {"regex": "#\\|\\|(LUNA_U)>[0-9]+\\|"}},
                "timeout_ms": 2200,
            },
        },
        "state_variables": {
            "control_address": {"type": "integer", "label": "Reported LUNA-U Address"}
        },
        "child_entity_types": {
            "zone": {
                "label": "Zone",
                "label_plural": "Zones",
                "id_format": {"type": "integer", "min": 1, "max": 32},
                "instances": {"ids_from": "zones", "label": "Zone {id}"},
                "summary_fields": ["route_label", "volume", "mute"],
                "state_variables": {
                    "route": {
                        "type": "integer",
                        "label": "Selected Route",
                        "min": -1,
                        "max": 24,
                        "control": True,
                        "help": "-1 is a custom mix, 0 is Off, 1–16 "
                        "select mapped inputs and 17–24 "
                        "select mixer scenes.",
                    },
                    "matrix_source": {
                        "type": "string",
                        "label": "Matrix Source",
                        "help": "Mapped route ID, Off, or Mixed. Use this feedback for a Matrix control.",
                    },
                    "route_label": {
                        "type": "string",
                        "label": "Current Source or Mix",
                        "cloud_priority": "high",
                    },
                    "volume": {
                        "type": "integer",
                        "label": "Zone Volume",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Zone Mute",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "mix_detail": {
                        "type": "string",
                        "label": "Mix Detail",
                        "help": "Mapped inputs with faders above "
                        "-90 dB. Available when all 16 "
                        "fader positions have been "
                        "reported. This describes the "
                        "mix upstream of the zone master "
                        "and mute.",
                    },
                    "mix_1": {
                        "type": "integer",
                        "label": "Mapped Input 1 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_2": {
                        "type": "integer",
                        "label": "Mapped Input 2 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_3": {
                        "type": "integer",
                        "label": "Mapped Input 3 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_4": {
                        "type": "integer",
                        "label": "Mapped Input 4 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_5": {
                        "type": "integer",
                        "label": "Mapped Input 5 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_6": {
                        "type": "integer",
                        "label": "Mapped Input 6 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_7": {
                        "type": "integer",
                        "label": "Mapped Input 7 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_8": {
                        "type": "integer",
                        "label": "Mapped Input 8 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_9": {
                        "type": "integer",
                        "label": "Mapped Input 9 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_10": {
                        "type": "integer",
                        "label": "Mapped Input 10 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_11": {
                        "type": "integer",
                        "label": "Mapped Input 11 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_12": {
                        "type": "integer",
                        "label": "Mapped Input 12 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_13": {
                        "type": "integer",
                        "label": "Mapped Input 13 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_14": {
                        "type": "integer",
                        "label": "Mapped Input 14 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_15": {
                        "type": "integer",
                        "label": "Mapped Input 15 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "mix_16": {
                        "type": "integer",
                        "label": "Mapped Input 16 Mix Level",
                        "min": -90,
                        "max": 0,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                },
            }
        },
        "commands": {
            "route_zone": {
                "label": "Select Zone Source or Scene",
                "help": "Select Off, one mapped input, or one of the eight saved mixer scenes. "
                "Mixed is a reported state.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    },
                    "source": {
                        "type": "enum",
                        "label": "Route",
                        "values": [
                            {"value": "Off", "label": "Off"},
                            {"value": "1", "label": "Mapped input 1"},
                            {"value": "2", "label": "Mapped input 2"},
                            {"value": "3", "label": "Mapped input 3"},
                            {"value": "4", "label": "Mapped input 4"},
                            {"value": "5", "label": "Mapped input 5"},
                            {"value": "6", "label": "Mapped input 6"},
                            {"value": "7", "label": "Mapped input 7"},
                            {"value": "8", "label": "Mapped input 8"},
                            {"value": "9", "label": "Mapped input 9"},
                            {"value": "10", "label": "Mapped input 10"},
                            {"value": "11", "label": "Mapped input 11"},
                            {"value": "12", "label": "Mapped input 12"},
                            {"value": "13", "label": "Mapped input 13"},
                            {"value": "14", "label": "Mapped input 14"},
                            {"value": "15", "label": "Mapped input 15"},
                            {"value": "16", "label": "Mapped input 16"},
                            {"value": "17", "label": "Mixer scene 1"},
                            {"value": "18", "label": "Mixer scene 2"},
                            {"value": "19", "label": "Mixer scene 3"},
                            {"value": "20", "label": "Mixer scene 4"},
                            {"value": "21", "label": "Mixer scene 5"},
                            {"value": "22", "label": "Mixer scene 6"},
                            {"value": "23", "label": "Mixer scene 7"},
                            {"value": "24", "label": "Mixer scene 8"},
                        ],
                        "required": True,
                        "pattern": "(?:Off|[1-9]|1[0-9]|2[0-4])",
                    },
                },
                "sets": {"matrix_source": "{source}"},
            },
            "set_zone_volume": {
                "label": "Set Zone Volume",
                "help": "Set the master level for this zone, keeping its mix and mute.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    },
                    "level": {
                        "type": "integer",
                        "label": "Level (dB)",
                        "min": -90,
                        "max": 0,
                        "required": True,
                    },
                },
                "sets": {"volume": "{level}"},
            },
            "mute_zone": {
                "label": "Mute Zone",
                "help": "Silence this zone without losing its mix.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    }
                },
                "sets": {"mute": True},
            },
            "unmute_zone": {
                "label": "Unmute Zone",
                "help": "Restore audio using the existing mix and master level.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    }
                },
                "sets": {"mute": False},
            },
            "set_mix_level": {
                "label": "Set Mixer Input Level",
                "help": "Change one mapped input in the zone mix. The processor reports "
                "whether the result matches a source, a scene or a custom mix.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    },
                    "input": {
                        "type": "integer",
                        "label": "Mapped Input",
                        "min": 1,
                        "max": 16,
                        "required": True,
                    },
                    "level": {
                        "type": "integer",
                        "label": "Level (dB)",
                        "min": -90,
                        "max": 0,
                        "required": True,
                    },
                },
            },
            "read_zone_route": {
                "label": "Read Zone Route",
                "help": "Read the zone’s current route from the processor.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    }
                },
                "query_for": "route",
            },
            "read_zone_volume": {
                "label": "Read Zone Volume",
                "help": "Read the zone’s current volume from the processor.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    }
                },
                "query_for": "volume",
            },
            "read_zone_mute": {
                "label": "Read Zone Mute",
                "help": "Read the zone’s current mute from the processor.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    }
                },
                "query_for": "mute",
            },
            "read_zone_mix": {
                "label": "Read Zone Mix Levels",
                "help": "Read the zone’s current mix levels from the processor.",
                "params": {
                    "zone": {
                        "type": "child_id",
                        "label": "Zone",
                        "child_type": "zone",
                        "required": True,
                    }
                },
                "query_for": "mix_detail",
            },
        },
        "routing": {
            "planes": [
                {
                    "label": "Zone Routing",
                    "destination_child_type": "zone",
                    "route_property": "matrix_source",
                    "command": "route_zone",
                    "source_param": "source",
                    "destination_param": "zone",
                }
            ]
        },
        "quick_actions": ["route_zone", "set_zone_volume", "mute_zone", "unmute_zone"],
        "actions": [
            {"id": "route_zone", "icon": "list"},
            {"id": "set_zone_volume", "icon": "volume-2"},
            {"id": "mute_zone", "icon": "volume-x"},
            {"id": "unmute_zone", "icon": "volume-2"},
            {"id": "set_mix_level", "icon": "sliders"},
        ],
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._zones = []
        self._mix = {}
        self._address = None
        self._receiving = False
        self._identity = None
        self._waiting = {}
        self._command_lock = asyncio.Lock()

    def _transport_kwargs(self, transport_type, kwargs):
        kwargs.update(delimiter=b"\r\n", frame_parser=None)
        return kwargs

    def _forget_mix(self, zone):
        self._mix[zone] = {}
        self.set_child_state_batch("zone", zone, dict.fromkeys((*FADERS, "mix_detail")))

    def _clear(self, zone, key):
        if key == "MIXER":
            self._forget_mix(zone)
        elif key == "ROUTE":
            self.set_child_state_batch(
                "zone",
                zone,
                {"route": None, "route_label": None, "matrix_source": None},
            )
        else:
            self.set_child_state("zone", zone, PROP[key], None)

    async def _close_session(self):
        self._receiving = False
        self._address = None
        self.set_state("control_address", None)
        if self._identity and not self._identity.done():
            self._identity.cancel()
        for pending in self._waiting.values():
            if not pending.done():
                pending.set_exception(
                    ConnectionError("LUNA-U disconnected during readback.")
                )
        self._waiting.clear()
        empty = dict.fromkeys(
            self.DRIVER_INFO["child_entity_types"]["zone"]["state_variables"]
        )
        for zone in self.list_children("zone"):
            self.set_child_state_batch("zone", zone, empty)
        self._mix.clear()
        await super()._close_session()

    async def _send(self, target, key, value=None):
        dest = f"LUNA_U>{self._address}" if self._address is not None else "LUNA_U"
        kind = "GET_REQ" if value is None else "SET_REQ"
        frame = (
            f"#|{dest}||{kind}^{target}^{key}|{'' if value is None else value}|U|\r\n"
        )
        await self.transport.send(frame.encode("ascii"))

    def on_data_received(self, data):
        message = unpack(data) if self._receiving else None
        if not message:
            return
        address, target, key, payload = message
        if self._address is not None and address != self._address:
            return
        for zone, item in reports(target, key, payload):
            value = reading(key, item)
            if value is not None:
                self._accept(address, zone, key, value)

    def _accept(self, address, zone, key, value):
        if self._address is None:
            self._address = address
            self.set_state("control_address", int(address))
            if self._identity and not self._identity.done():
                self._identity.set_result(True)
        if zone not in self._zones:
            return
        self._publish(zone, key, value)
        waiting = self._waiting.get((zone, key))
        if waiting and not waiting.done():
            waiting.set_result(value)

    def _publish(self, zone, key, value):
        if key == "MIXER":
            levels = self._mix.setdefault(zone, {})
            levels.update(value)
            updates = {f"mix_{idx}": level for idx, level in value.items()}
            updates["mix_detail"] = mix_detail(levels)
        elif key == "ROUTE":
            previous = self.get_child_state("zone", zone).get("route")
            if previous is not None and value != previous:
                self._forget_mix(zone)
            label = route_label(value)
            # The panel treats numeric zero as unknown/unrouted. Keep Off explicit.
            updates = {
                "route": value,
                "route_label": label,
                "matrix_source": str(value) if value > 0 else label,
            }
        else:
            updates = {PROP[key]: value}
        self.set_child_state_batch("zone", zone, {**updates, **self.child_fault()})

    async def _post_connect(self):
        self._zones = configured_zones(self.config.get("zones", "1"))
        for removed in set(self.list_children("zone")) - set(self._zones):
            self.deregister_child("zone", removed)
        for zone in self._zones:
            self.register_child("zone", zone)
        self._identity = asyncio.get_running_loop().create_future()
        self._receiving = True
        await self._send("ALL_ZONES", "ROUTE")
        # A direct query also works when the installation omits grouped replies.
        try:
            await asyncio.wait_for(asyncio.shield(self._identity), TIMEOUT / 2)
        except TimeoutError:
            await self._send(f"ZONE>{self._zones[0]}>MIXER>1", "ROUTE")
        try:
            await asyncio.wait_for(self._identity, TIMEOUT)
        except TimeoutError as exc:
            raise ConnectionError(
                "No LUNA-U zone report on the ASCII control port."
            ) from exc

    async def _initial_sync(self):
        await self.poll()

    async def _request(self, zone, key, value=None):
        identity = (zone, key)
        future = asyncio.get_running_loop().create_future()
        self._waiting[identity] = future
        try:
            await self._send(f"ZONE>{zone}>{BLOCK[key]}>1", key, value)
            return await asyncio.wait_for(future, TIMEOUT)
        finally:
            if self._waiting.get(identity) is future:
                self._waiting.pop(identity)

    async def _poll_one(self, zone, key):
        try:
            await self._request(zone, key)
        except TimeoutError:
            self._clear(zone, key)

    async def _refresh_field(self, key):
        # Several zones can be read together. The reply names both zone and field.
        # Bound the burst so even a 32-zone installation is a considerate client.
        for start in range(0, len(self._zones), 8):
            await asyncio.gather(
                *(self._poll_one(z, key) for z in self._zones[start : start + 8])
            )

    async def poll(self):
        async with self._command_lock:
            for key in ("ROUTE", "VOLUME", "MUTE", "MIXER"):
                await self._refresh_field(key)
            for zone in self._zones:
                values = self.get_child_state("zone", zone)
                if all(
                    values.get(field) is None
                    for field in ("route", "volume", "mute", *FADERS)
                ):
                    self.set_child_state_batch(
                        "zone",
                        zone,
                        self.child_fault(
                            "not_responding",
                            "No zone feedback. Check the commissioned zone number.",
                        ),
                    )

    async def refresh_children(self):
        await self.poll()
        return {"zones": len(self._zones)}

    async def _read(self, zone, key):
        try:
            return await self._request(zone, key)
        except TimeoutError as exc:
            self._clear(zone, key)
            raise CommandRefusedError(
                f"Zone {zone} did not report {key.lower()}."
            ) from exc

    async def _write(self, zone, key, argument, expected):
        try:
            actual = await self._request(zone, key, argument)
        except TimeoutError:
            # An invalid SET_REQ is silent. Read the device before confirming.
            actual = await self._read(zone, key)
        if key == "MIXER":
            slot, level = expected
            if actual.get(slot) != level:
                actual = await self._read(zone, key)
            confirmed = actual.get(slot) == level
        else:
            confirmed = actual == expected
        if not confirmed:
            raise CommandRefusedError(
                f"Zone {zone} did not confirm the requested {key.lower()}."
            )

    async def send_command(self, command, params=None):
        params = params or {}
        zone = required(params, "zone", 1, 32)
        if zone not in self._zones:
            raise CommandRefusedError("Choose one of the commissioned zones.")
        async with self._command_lock:
            await self._operate(command, zone, params)
        return True

    async def _set_scalar(self, command, zone, params):
        key, name, low, high = SIMPLE_WRITES[command]
        value = (
            0
            if key == "ROUTE" and params.get(name) == "Off"
            else required(params, name, low, high)
        )
        await self._write(zone, key, value, value)
        if key == "ROUTE":
            await self._poll_one(zone, "MIXER")

    async def _operate(self, command, zone, params):
        if command in READ:
            await self._read(zone, READ[command])
        elif command in SIMPLE_WRITES:
            await self._set_scalar(command, zone, params)
        elif command in ("mute_zone", "unmute_zone"):
            enabled = command == "mute_zone"
            await self._write(zone, "MUTE", "TRUE" if enabled else "FALSE", enabled)
        elif command == "set_mix_level":
            slot = required(params, "input", 1, 16)
            level = required(params, "level", -90, 0)
            await self._write(zone, "MIXER", f"{slot}>{level}", (slot, level))
            await self._poll_one(zone, "ROUTE")
            await self._poll_one(zone, "MIXER")
        else:
            raise CommandRefusedError("Unknown zone command.")
