"""Catchbox Plus Hub, public API 2.1 over the platform UDP transport.

Python reconciles the reported dual/quad roster, joins two differently addressed
sources (Hub link state and transmitter readings), rejects late telemetry after
a link drop and releases change subscriptions for the session's UDP endpoint.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError

HUB = ("rx", "device")
TYPES = {"dual": 2, "quad": 4}
LINKS = ("Disconnected", "Connected", "Pairing", "Charging")
MIX = ("usb", "rca", "3pin")
TX_FIELDS = {
    "name": "name",
    "serial": "serial_number",
    "battery": "battery_percent",
    "rssi": "rssi",
}
IDENTITY = {
    "name": "hub_name",
    "firmware_info": "firmware_version",
    "serial": "serial_number",
}
ERRORS = {
    405: "Value outside device range",
    410: "Invalid value",
    415: "Transmitter has no RF link",
    425: "Malformed command",
    430: "Feature unavailable",
}


def packet(path, value=None):
    for key in reversed(path):
        value = {key: value}
    return value


def leaves(tree, path=()):
    for key, value in tree.items():
        address = (*path, key)
        if isinstance(value, dict):
            yield from leaves(value, address)
        else:
            yield address, value


def mic_mute(slot):
    return ("rx", "audio", "input", f"mic{slot}", "mute")


def link(slot):
    return (*HUB, f"mic{slot}_link_state")


def mix(output):
    return ("rx", "audio", "output", "mixout", output, "mute")


def radio(slot, key):
    return (f"tx{slot}", "device", key)


HUB_IDENTITY = {(*HUB, wire): prop for wire, prop in IDENTITY.items()}
MIX_MUTES = {mix(output): f"mix_{output}_mute" for output in MIX}


def valid_transmitter_value(key, value):
    if key == "battery":
        return type(value) is int and 0 <= value <= 100
    if key == "rssi":
        return type(value) is int
    return isinstance(value, str)


class CatchboxPlusHubDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "catchbox_plus_hub",
        "name": "Catchbox Plus Hub",
        "manufacturer": "Catchbox",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": "Discover the Hub and its fitted microphone channels. Control input and mix output "
        "mute and monitor link, charging, battery and RF signal for each microphone.",
        "source_url": "https://docs.catchbox.com/v2.1/command-list/",
        "verified": False,
        "simulated": True,
        "compatible_models": [
            {
                "manufacturer": "Catchbox",
                "models": ["Plus Hub CBPLRX003", "Plus Hub CBPLRX004"],
                "confidence": "untested",
                "notes": "Network-enabled dual and quad Hubs. CBPLRX003 firmware 2.0/2.1 and "
                "CBPLRX004 firmware 2.1.",
            }
        ],
        "transport": "udp",
        "ports": [39030],
        "default_config": {
            "host": "",
            "port": 39030,
            "poll_interval": 30,
            "inter_command_delay": 0.025,
        },
        "config_schema": {
            "host": {"type": "string", "label": "Device Address", "required": True},
            "port": {
                "type": "integer",
                "label": "Control Port",
                "default": 39030,
                "min": 1,
                "max": 65535,
            },
            "poll_interval": {
                "type": "integer",
                "label": "Recheck Interval (seconds)",
                "min": 5,
                "max": 300,
                "default": 30,
            },
        },
        "min_platform_version": "0.37.2",
        "tags": ["wireless-microphone", "battery", "conferencing", "catchbox"],
        "protocols": ["catchbox"],
        "help": {
            "overview": "The receiver reports whether it has two or four microphone channels. Each "
            "channel has a stable slot, input mute and link status. Live transmitter "
            "readings disappear when the radio link drops. Charging is a distinct Hub "
            "report, not inferred from battery percentage.",
            "setup": "Use a network-enabled Hub with the supported 2.x firmware. Discover it on the "
            "local subnet or enter the Hub IPv4 address and allow UDP port 39030. The Hub does "
            "not require an API login. Pair transmitters at the Hub before use.",
            "connection": "Every connection identifies the Hub and reads each channel. Change "
            "subscriptions supply immediate feedback. A periodic refresh recovers lost UDP "
            "updates. RF signal uses the value reported by Catchbox, which does not "
            "specify a unit.",
        },
        "discovery": {
            "mdns": [{"service": "_catchbox._udp"}],
            "manufacturer_alias": ["catchbox"],
            "udp_probe": {
                "port": 39030,
                "send_ascii": '{"rx":{"device":{"device_type":null}}}',
                "expect_regex": '"device_type"\\s*:\\s*"(?:dual|quad)"',
                "extract_manufacturer": "Catchbox",
            },
        },
        "state_variables": {
            "hub_name": {"type": "string", "label": "Hub Name"},
            "firmware_version": {"type": "string", "label": "Firmware"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "channel_count": {
                "type": "integer",
                "label": "Microphone Channels",
                "min": 2,
                "max": 4,
            },
            "mix_usb_mute": {
                "type": "boolean",
                "label": "USB Mix Output Muted",
                "control": True,
                "cloud_priority": "high",
            },
            "mix_rca_mute": {
                "type": "boolean",
                "label": "RCA Mix Output Muted",
                "control": True,
                "cloud_priority": "high",
            },
            "mix_3pin_mute": {
                "type": "boolean",
                "label": "3-pin Mix Output Muted",
                "control": True,
                "cloud_priority": "high",
            },
        },
        "child_entity_types": {
            "channel": {
                "label": "Microphone Channel",
                "label_plural": "Microphone Channels",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "label_field": "name",
                "summary_fields": ["link_state", "mute", "battery_percent"],
                "state_variables": {
                    "name": {"type": "string", "label": "Transmitter Name"},
                    "serial_number": {
                        "type": "string",
                        "label": "Transmitter Serial Number",
                    },
                    "link_state": {
                        "type": "enum",
                        "label": "Wireless Link",
                        "values": ["Disconnected", "Connected", "Pairing", "Charging"],
                        "cloud_priority": "high",
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Hub Input Muted",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "battery_percent": {
                        "type": "integer",
                        "label": "Battery",
                        "min": 0,
                        "max": 100,
                        "unit": "%",
                        "cloud_priority": "high",
                    },
                    "rssi": {
                        "type": "integer",
                        "label": "RF Signal (device units)",
                        "cloud_priority": "low",
                    },
                },
            }
        },
        "commands": {
            "read_firmware": {
                "label": "Read Firmware",
                "query_for": "firmware_version",
            },
            "read_channels": {
                "label": "Read Channel Count",
                "query_for": "channel_count",
            },
            "read_mic_link": {
                "label": "Read Wireless Link",
                "query_for": "link_state",
                "params": {
                    "channel": {
                        "type": "child_id",
                        "label": "Microphone Channel",
                        "child_type": "channel",
                        "required": True,
                    }
                },
            },
            "read_mic_battery": {
                "label": "Read Battery",
                "query_for": "battery_percent",
                "params": {
                    "channel": {
                        "type": "child_id",
                        "label": "Microphone Channel",
                        "child_type": "channel",
                        "required": True,
                    }
                },
            },
            "read_mic_mute": {
                "label": "Read Microphone Mute",
                "query_for": "mute",
                "params": {
                    "channel": {
                        "type": "child_id",
                        "label": "Microphone Channel",
                        "child_type": "channel",
                        "required": True,
                    }
                },
            },
            "mute_mic": {
                "label": "Mute Microphone",
                "sets": {"mute": True},
                "params": {
                    "channel": {
                        "type": "child_id",
                        "label": "Microphone Channel",
                        "child_type": "channel",
                        "required": True,
                    }
                },
            },
            "unmute_mic": {
                "label": "Unmute Microphone",
                "sets": {"mute": False},
                "params": {
                    "channel": {
                        "type": "child_id",
                        "label": "Microphone Channel",
                        "child_type": "channel",
                        "required": True,
                    }
                },
            },
            "mute_usb": {
                "label": "Mute USB Mix Output",
                "sets": {"mix_usb_mute": True},
            },
            "unmute_usb": {
                "label": "Unmute USB Mix Output",
                "sets": {"mix_usb_mute": False},
            },
            "read_usb_mute": {
                "label": "Read USB Mix Output Mute",
                "query_for": "mix_usb_mute",
            },
            "mute_rca": {
                "label": "Mute RCA Mix Output",
                "sets": {"mix_rca_mute": True},
            },
            "unmute_rca": {
                "label": "Unmute RCA Mix Output",
                "sets": {"mix_rca_mute": False},
            },
            "read_rca_mute": {
                "label": "Read RCA Mix Output Mute",
                "query_for": "mix_rca_mute",
            },
            "mute_3pin": {
                "label": "Mute 3-pin Mix Output",
                "sets": {"mix_3pin_mute": True},
            },
            "unmute_3pin": {
                "label": "Unmute 3-pin Mix Output",
                "sets": {"mix_3pin_mute": False},
            },
            "read_3pin_mute": {
                "label": "Read 3-pin Mix Output Mute",
                "query_for": "mix_3pin_mute",
            },
        },
        "actions": [
            {"id": "mute_mic", "icon": "mic-off"},
            {"id": "unmute_mic", "icon": "mic"},
            {"id": "mute_usb", "icon": "volume-x"},
            {"id": "unmute_usb", "icon": "volume-2"},
        ],
        "quick_actions": ["mute_mic", "unmute_mic"],
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._waiters = {}
        self._subscriptions = set()
        self._renew_at = 0.0
        self._links = {}
        self._requests = asyncio.Lock()
        self._radio_tasks = {}

    async def _send(self, body):
        if not self.transport or not self.transport.connected:
            raise ConnectionError("Hub connection is closed.")
        await self.transport.send(json.dumps(body, separators=(",", ":")).encode())

    async def _request(self, address, value=None):
        # Errors without a returned address cannot identify a caller. Only
        # fulfill a read/write with a response carrying its exact address.
        async with self._requests:
            result = asyncio.get_running_loop().create_future()
            self._waiters[address] = result
            try:
                await self._send(packet(address, value))
                return await asyncio.wait_for(result, 2)
            finally:
                self._waiters.pop(address, None)

    async def _post_connect(self):
        try:
            shape = await self._request((*HUB, "device_type"))
            version = await self._request((*HUB, "firmware_info"))
        except asyncio.TimeoutError as exc:
            raise ConnectionFaultError(
                "No Catchbox Hub identity reply on UDP 39030.", code="no_response"
            ) from exc
        if shape not in TYPES or not re.match(r"^2\.(?:0|1)(?:\D|$)", str(version)):
            raise ConnectionFaultError(
                f"Unsupported Catchbox Hub type or firmware: {shape}, {version}.",
                code="invalid_config",
            )

    async def _initial_sync(self):
        await self.poll()

    async def _subscribe(self, address):
        if address in self._subscriptions:
            return
        await self._send(
            {"subscribe": [{"#": {"enable": True, "period_ms": 0}, **packet(address)}]}
        )
        self._subscriptions.add(address)

    async def _read_observed(self, address):
        await self._subscribe(address)
        await self._send(packet(address))

    async def _refresh_radio(self, slot):
        for key in TX_FIELDS:
            if self._links.get(slot) not in ("Connected", "Charging"):
                return
            if key == "serial":
                await self._send(packet(radio(slot, key)))
            else:
                await self._read_observed(radio(slot, key))

    def _queue_radio(self, slot):
        task = self._radio_tasks.get(slot)
        if task and not task.done():
            return

        task = asyncio.create_task(self._refresh_radio(slot))
        self._bg_tasks.add(task)
        task.add_done_callback(self._radio_finished)
        self._radio_tasks[slot] = task

    def _radio_finished(self, task):
        self._bg_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def poll(self):
        # A Hub can reboot between successful heartbeat probes. Re-arm
        # on-change subscriptions periodically even on the same UDP socket.
        if time.monotonic() >= self._renew_at:
            self._subscriptions.clear()
            self._renew_at = time.monotonic() + 120
        await self._send(packet((*HUB, "device_type")))
        await self._read_observed((*HUB, "name"))
        await self._send(packet((*HUB, "serial")))
        for output in MIX:
            await self._read_observed(mix(output))
        for slot in self.list_children("channel"):
            await self._read_observed(link(slot))
            await self._read_observed(mic_mute(slot))
            await self._refresh_radio(slot)

    async def _liveness_probe(self):
        await self._request((*HUB, "firmware_info"))

    def _roster(self, shape):
        count = TYPES.get(shape) if isinstance(shape, str) else None
        if not count:
            return
        self.set_state("channel_count", count)
        for slot in set(self.list_children("channel")) - set(range(1, count + 1)):
            self.deregister_child("channel", slot)
            self._links.pop(slot, None)
        for slot in range(1, count + 1):
            self.register_child("channel", slot)

    def _link(self, slot, code):
        if type(code) is not int or not 0 <= code < len(LINKS):
            return
        status = LINKS[code]
        previous = self._links.get(slot)
        self._links[slot] = status
        updates = {"link_state": status}
        if status not in ("Connected", "Charging"):
            updates.update(dict.fromkeys(TX_FIELDS.values()))
        self.set_child_state_batch("channel", slot, updates)
        if previous != status and status in ("Connected", "Charging"):
            self._queue_radio(slot)

    def _radio_value(self, slot, key, value):
        if self._links.get(slot) not in ("Connected", "Charging"):
            return
        if key in TX_FIELDS and valid_transmitter_value(key, value):
            self.set_child_state("channel", slot, TX_FIELDS[key], value)

    def _report(self, path, value):
        if path == (*HUB, "device_type"):
            self._roster(value)
        elif path in HUB_IDENTITY and isinstance(value, str):
            self.set_state(HUB_IDENTITY[path], value)
        elif path in MIX_MUTES and value in (0, 1):
            self.set_state(MIX_MUTES[path], bool(value))
        else:
            self._channel_report(path, value)

    def _channel_report(self, path, value):
        for slot in self.list_children("channel"):
            if path == link(slot):
                self._link(slot, value)
            elif path == mic_mute(slot) and value in (0, 1):
                self.set_child_state("channel", slot, "mute", bool(value))
            elif path[:2] == (f"tx{slot}", "device") and len(path) == 3:
                self._radio_value(slot, path[-1], value)

    def _accept(self, path, value, error):
        if error == 415:
            self._forget_unreachable_radio(path)
        elif error == 0 and value is not None:
            self._report(path, value)
        self._complete_request(path, value, error)

    def _forget_unreachable_radio(self, path):
        if len(path) != 3 or path[1] != "device":
            return
        match = re.fullmatch(r"tx([1-4])", path[0])
        if match and self.is_child_registered("channel", int(match[1])):
            self.set_child_state_batch(
                "channel", int(match[1]), dict.fromkeys(TX_FIELDS.values())
            )

    def _complete_request(self, path, value, error):
        waiting = self._waiters.get(path)
        if waiting is None or waiting.done():
            return
        if error:
            waiting.set_exception(
                CommandRefusedError(ERRORS.get(error, f"Hub error {error}"))
            )
        elif value is not None:
            waiting.set_result(value)

    async def on_data_received(self, data):
        try:
            body = json.loads(data)
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(body, dict) or type(body.get("error")) is not int:
            return
        for path, value in leaves(body):
            if path[0] not in ("error", "subscribe"):
                self._accept(path, value, body["error"])

    async def refresh_children(self):
        await self._request((*HUB, "device_type"))
        await self.poll()
        return {"channel": len(self.list_children("channel"))}

    async def send_command(self, command, params=None):
        if command not in self.DRIVER_INFO["commands"]:
            raise ValueError(f"Unknown command {command}")
        if command == "read_firmware":
            return await self._request((*HUB, "firmware_info"))
        if command == "read_channels":
            return TYPES[await self._request((*HUB, "device_type"))]
        if "mic" in command:
            return await self._microphone_command(
                command, (params or {}).get("channel")
            )
        output = next(key for key in MIX if key in command)
        return await self._mute_command(command, mix(output))

    async def _microphone_command(self, command, slot):
        slot = int(slot)
        if not self.is_child_registered("channel", slot):
            raise ValueError("Choose a microphone channel fitted to this Hub.")
        if command == "read_mic_link":
            return LINKS[await self._request(link(slot))]
        if command == "read_mic_battery":
            if self._links.get(slot) not in ("Connected", "Charging"):
                raise CommandRefusedError("This channel has no connected transmitter.")
            return await self._request(radio(slot, "battery"))
        return await self._mute_command(command, mic_mute(slot))

    async def _mute_command(self, command, address):
        if command.startswith("read_"):
            return bool(await self._request(address))
        expected = int(command.startswith("mute_"))
        await self._request(address, expected)
        actual = await self._request(address)
        if actual != expected:
            raise CommandRefusedError(
                "Hub mute readback differs from the requested value."
            )
        return bool(actual)

    async def disconnect(self):
        try:
            if self.transport and self.transport.connected:
                for address in tuple(self._subscriptions):
                    await self._send(
                        {
                            "subscribe": [
                                {
                                    "#": {"enable": False, "period_ms": 0},
                                    **packet(address),
                                }
                            ]
                        }
                    )
        finally:
            await super().disconnect()

    async def _close_session(self):
        for task in self._radio_tasks.values():
            task.cancel()
        self._radio_tasks.clear()
        for future in self._waiters.values():
            if not future.done():
                future.cancel()
        self._subscriptions.clear()
        self._links.clear()
        self.set_states(dict.fromkeys(self.DRIVER_INFO["state_variables"]))
        for slot in self.list_children("channel"):
            fields = self.DRIVER_INFO["child_entity_types"]["channel"][
                "state_variables"
            ]
            self.set_child_state_batch("channel", slot, dict.fromkeys(fields))
        await super()._close_session()
