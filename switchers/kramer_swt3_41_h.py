"""Kramer SWT3-41-H Protocol 3000 switcher and rear RS-232 gateway.

The gateway is a separate native TCP stream. Python checks the bound driver's
serial format against live COM-ROUTE and UART replies without reconfiguring
an installation.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from openavc.drivers.base import BaseDriver

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # Earlier supported releases report refusals as RuntimeError.
    CommandRefusedError = RuntimeError


MODES = ("Manual", "Priority", "Last connected")
PARITIES = ("N", "O", "E", "M", "S")


def _enabled(value: str) -> bool:
    return value != "0"


# Kramer sends the same format for a query response and for a change notification.
FLAT_REPORTS = {
    "MODEL": (r"(.+)", "model", str),
    "VERSION": (r"(.+)", "firmware", str),
    "VMUTE": (r"1,([01])", "video_muted", _enabled),
    "AUD-MUTE": (r"(?:1,1,1,|1,)([01])", "audio_muted", _enabled),
    "AUD-LVL": (r"1,1,(-?\d+(?:\.\d+)?)", "volume", float),
    "AV-SW-MODE": (r"1,1,([012])", "switch_mode", lambda n: MODES[int(n)]),
}
CHILD_REPORTS = {
    "ROUTE": (r"1,(1),([1-4])", "output", "source", int),
    "DISPLAY": (r"(1),([012])", "output", "sink", _enabled),
    "SIGNAL": (r"([1-4]),([01])", "input", "signal", _enabled),
    "HDCP-STAT": (r"0,([1-4]),([01])", "input", "hdcp", _enabled),
}
SIMPLE_COMMANDS = {
    "read_firmware": ("VERSION?", ""),
    "mute_video": ("VMUTE 1,1", "1,"),
    "unmute_video": ("VMUTE 1,0", "1,"),
    "volume_up": ("AUD-LVL 1,1,++", "1,1,"),
    "volume_down": ("AUD-LVL 1,1,--", "1,1,"),
}


class KramerSwt3Driver(BaseDriver):
    DRIVER_INFO = {
        "id": "kramer_swt3_41_h",
        "name": "Kramer SWT3-41-H",
        "manufacturer": "Kramer",
        "category": "switcher",
        "version": "1.0.0",
        "author": "OpenAVC",
        "min_platform_version": "0.27.0",
        "source_url": "https://cdn.kramerav.com/web/downloads/manuals/kramer-swt3-21-h__swt3-41-h_um_rev_7.pdf",
        "description": "Four-input HDMI auto switcher with analog audio output and a bidirectional RS-232 gateway for a display or projector. Checks the gateway port, reply setting and serial format.",
        "tags": [
            "hdmi",
            "presentation",
            "auto-switcher",
            "p3000",
            "serial-bridge",
            "rs232",
        ],
        "protocols": ["kramer_p3000"],
        "ports": [5000],
        "verified": False,
        "simulated": True,
        "compatible_models": [
            {
                "manufacturer": "Kramer",
                "models": ["SWT3-41-H"],
                "confidence": "untested",
                "notes": "Switcher feedback is based on the manual and earlier captures from firmware 1.0.65739. The new driver and serial gateway have not been qualified on physical equipment.",
            }
        ],
        "transport": "tcp",
        "delimiter": "\r\n",
        "bridge": {
            "ports": [
                {
                    "id": "serial:1",
                    "kind": "serial",
                    "label": "Rear RS-232 (TCP 5001)",
                    "passthrough_port": 5001,
                }
            ]
        },
        "default_config": {
            "host": "",
            "port": 5000,
            "poll_interval": 30,
            "inter_command_delay": 0.1,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "Switcher IP Address",
            },
            "port": {
                "type": "integer",
                "default": 5000,
                "min": 1,
                "max": 65535,
                "label": "Switcher Control Port",
                "description": "Protocol 3000 control connection, usually 5000. The serial gateway uses a separate TCP connection on 5001.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 0,
                "label": "Status Refresh (seconds)",
                "description": "Recheck switcher and gateway readings. Set to 0 to keep only the initial readings, change notifications and manual refresh.",
            },
        },
        "help": {
            "overview": "Select any of four HDMI inputs, follow signal and display status, control output volume and muting, and connect one serial device through the rear RS-232 terminal.",
            "setup": "1. Add this exact switcher with its IP address and Protocol 3000 control port (normally 5000). Select the input-switching policy under Device Settings. Automatic selection may change the route after a manual selection.\n2. Wire the rear three-pin RS-232 terminal to the display or projector: TX to RX, RX to TX, ground to ground. Use one downstream driver for that connector.\n3. In the Kramer web interface open Control Gateway → Serial Ports. Set Tunneling to Gateway. Match the attached equipment’s baud rate, data bits, parity and stop bits, then save. In Advanced Properties choose TCP, IP Port 5001 and enable replies to new clients. Save. Switching to Gateway repurposes the connector if it was controlling the switcher or running Display ON commands.\n4. In OpenAVC add the display or projector with its own serial driver. Select Through a bridge, this Kramer and Rear RS-232 (TCP 5001). Enter the same serial settings. Read Serial Gateway Setup here, then request status from the attached device and confirm its reply.\n5. This bridge uses TCP 5001. If this project already uses another tunnel port, coordinate the change in the Kramer web interface with its other users before binding a downstream device. The driver reads the settings and leaves them in place.",
            "connection": "Allow the switcher control port and TCP 5001 between the OpenAVC server and the Kramer. OpenAVC can open the network socket even when Serial Gateway Setup reports a mismatch. Correct that setting before sending display commands. A returned display status checks the RS-232 wiring. After a change in Kramer, reconnect the attached device to check its settings again.",
        },
        "discovery": {
            "tcp_probe": {
                "port": 5000,
                "send_ascii": "#MODEL?\r\n",
                "expect_regex": "(?i)~\\d+@MODEL\\s+SWT3-41-H(?:\\s|$)",
                "extract_manufacturer": "Kramer",
                "extract": {
                    "model": {"regex": "(?i)~\\d+@MODEL\\s+(SWT3-41-H)(?:\\s|$)"}
                },
            },
            "oui": ["00:1d:56"],
        },
        "state_variables": {
            "model": {"type": "string", "label": "Switcher Model"},
            "firmware": {"type": "string", "label": "Firmware Version"},
            "volume": {
                "type": "number",
                "label": "Audio Output Level",
                "unit": "dB",
                "min": -100,
                "max": 15,
                "control": True,
            },
            "video_muted": {
                "type": "boolean",
                "label": "HDMI Video Muted",
                "control": True,
            },
            "audio_muted": {
                "type": "boolean",
                "label": "Audio Output Muted",
                "control": True,
            },
            "switch_mode": {
                "type": "enum",
                "label": "Input Selection",
                "values": ["Manual", "Priority", "Last connected"],
            },
            "gateway_protocol": {"type": "string", "label": "Serial Gateway Protocol"},
            "gateway_port": {
                "type": "integer",
                "label": "Serial Gateway TCP/UDP Port",
                "help": "The number reported by the switcher. This bridge uses TCP 5001.",
            },
            "gateway_replies": {
                "type": "boolean",
                "label": "Serial Replies to New Clients",
            },
            "serial_format": {
                "type": "string",
                "label": "Reported Serial Format",
                "help": "Baud, data bits, parity and stop bits reported by the switcher.",
            },
            "gateway_status": {
                "type": "string",
                "label": "Serial Gateway Setup",
                "cloud_priority": "high",
                "help": "Checks the reported tunnel settings and compares the serial format when a downstream device is bound. Verify wiring by requesting a reply from that device.",
            },
        },
        "child_entity_types": {
            "input": {
                "label": "HDMI Input",
                "label_plural": "HDMI Inputs",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "instances": {"count": 4, "label": "HDMI {id}"},
                "state_variables": {
                    "signal": {
                        "type": "boolean",
                        "label": "Signal Present",
                        "cloud_priority": "high",
                    },
                    "hdcp": {"type": "boolean", "label": "HDCP Detected"},
                },
                "summary_fields": ["signal"],
            },
            "output": {
                "label": "HDMI Output",
                "label_plural": "HDMI Outputs",
                "id_format": {"type": "integer", "min": 1, "max": 1},
                "instances": {"count": 1, "label": "HDMI Out"},
                "state_variables": {
                    "source": {
                        "type": "integer",
                        "label": "Selected HDMI Input",
                        "min": 1,
                        "max": 4,
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "sink": {"type": "boolean", "label": "Display Connected"},
                },
                "summary_fields": ["source", "sink"],
            },
        },
        "routing": {
            "destination_child_type": "output",
            "command": "route",
            "planes": [{"label": "Video", "route_property": "source"}],
        },
        "commands": {
            "route": {
                "label": "Select HDMI Input",
                "help": "Select the HDMI source for the output. Automatic switching can select another source when signals change.",
                "params": {
                    "input": {
                        "type": "enum",
                        "label": "HDMI Input",
                        "required": True,
                        "values": [
                            {"value": 1, "label": "HDMI 1"},
                            {"value": 2, "label": "HDMI 2"},
                            {"value": 3, "label": "HDMI 3"},
                            {"value": 4, "label": "HDMI 4"},
                        ],
                    },
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "label": "Output",
                        "default": 1,
                    },
                },
                "sets": {"source": "{input}"},
            },
            "set_volume": {
                "label": "Set Audio Output Level",
                "help": "Set the output level in increments of 0.1 dB.",
                "params": {
                    "level": {
                        "type": "number",
                        "label": "Volume (dB)",
                        "required": True,
                        "min": -100,
                        "max": 15,
                    }
                },
                "sets": {"volume": "{level}"},
            },
            "volume_up": {
                "label": "Raise Volume 1 dB",
                "help": "Raise the current output level by one dB.",
            },
            "volume_down": {
                "label": "Lower Volume 1 dB",
                "help": "Lower the current output level by one dB.",
            },
            "mute_video": {
                "label": "Mute HDMI Video",
                "help": "Disable the HDMI output video.",
                "sets": {"video_muted": True},
            },
            "unmute_video": {
                "label": "Unmute HDMI Video",
                "help": "Enable the HDMI output video.",
                "sets": {"video_muted": False},
            },
            "mute_audio": {
                "label": "Mute Output Audio",
                "help": "Mute the output audio.",
                "sets": {"audio_muted": True},
            },
            "unmute_audio": {
                "label": "Unmute Output Audio",
                "help": "Restore the output audio.",
                "sets": {"audio_muted": False},
            },
            "read_gateway": {
                "label": "Check Serial Gateway",
                "help": "Read the tunnel port, reply setting and line format, then compare them to the selected bridge configuration.",
                "query_for": "gateway_status",
            },
            "read_firmware": {
                "label": "Read Firmware Version",
                "help": "Read the running firmware version.",
                "query_for": "firmware",
            },
        },
        "actions": [
            {"id": "route"},
            {"id": "set_volume"},
            {"id": "mute_video"},
            {"id": "unmute_video"},
            {"id": "read_gateway"},
        ],
        "device_settings": {
            "switch_mode": {
                "type": "enum",
                "label": "Automatic Input Selection",
                "values": ["Manual", "Priority", "Last connected"],
                "default": "Manual",
                "state_key": "switch_mode",
                "setup": False,
                "help": "Manual keeps the selected input. Priority follows the switcher’s configured priorities. Last connected selects the newest source.",
            }
        },
    }

    HEALTH_INTERVAL_S = 30
    HEALTH_TIMEOUT_S = 5
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The switcher stopped answering Protocol 3000 requests."

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._wire_lock = asyncio.Lock()
        self._gateway_lock = asyncio.Lock()
        self._pending = None
        self._bound_settings = None
        self._mute_address = None
        self.register_child("output", 1, {"label": "HDMI Out"})
        for n in range(1, 5):
            self.register_child("input", n, {"label": f"HDMI {n}"})

    async def _post_connect(self) -> None:
        self._mute_address = None
        self.set_state("gateway_status", "Serial gateway settings have not been read.")
        model = await self._ask("MODEL?")
        if model.upper() != "SWT3-41-H":
            raise CommandRefusedError(
                f"This driver requires SWT3-41-H. The switcher reported {model}."
            )

    async def _initial_sync(self) -> None:
        await self.poll()

    async def _close_session(self) -> None:
        if self._pending:
            future = self._pending[2]
            if not future.done():
                future.set_exception(
                    ConnectionError("The Kramer control connection closed.")
                )

    async def _ask(self, wire: str, address: str = "") -> str:
        # P3K has no transaction IDs. Match the verb and address and keep just
        # one request outstanding; unrelated front-panel updates still publish.
        verb = wire.partition(" ")[0].rstrip("?")
        async with self._wire_lock:
            if self.transport is None:
                raise ConnectionError("The Kramer control connection is offline.")
            future = asyncio.get_running_loop().create_future()
            self._pending = (verb, address, future)
            try:
                await self.transport.send(("#" + wire + "\r").encode("ascii"))
                return await asyncio.wait_for(future, 2.5)
            finally:
                self._pending = None
                future.cancel()

    async def on_data_received(self, data: bytes) -> None:
        for line in data.decode("ascii", "replace").splitlines():
            match = re.fullmatch(r"~\d+@([\w-]+)\??\s+(.+)", line.strip())
            if match:
                verb, args = match.groups()
                self._receive(verb, args.strip())

    def _receive(self, verb: str, args: str) -> None:
        error = re.search(r"(?:^|\s)ERR\s+(\d+)$", args, re.I)
        if not error and not self._publish(verb, args):
            return
        if self._pending is None:
            return
        wanted, address, future = self._pending
        if verb != wanted or future.done():
            return
        if error:
            future.set_exception(
                CommandRefusedError(f"Kramer refused {verb} (ERR {error[1]}).")
            )
        elif args.startswith(address):
            future.set_result(args)

    def _publish(self, verb: str, args: str) -> bool:
        if verb in FLAT_REPORTS:
            pattern, key, convert = FLAT_REPORTS[verb]
            match = re.fullmatch(pattern, args)
            if match:
                self.set_state(key, convert(match[1]))
            return match is not None
        if verb in CHILD_REPORTS:
            return self._publish_child(verb, args)
        return verb in ("COM-ROUTE", "UART")

    def _publish_child(self, verb: str, args: str) -> bool:
        pattern, kind, key, convert = CHILD_REPORTS[verb]
        match = re.fullmatch(pattern, args)
        if match:
            self.set_child_state(kind, int(match[1]), key, convert(match[2]))
        return match is not None

    async def _read_mute(self) -> None:
        # The manual gives the short form. Firmware 1.0.65739 also reports
        # an output/stage/mute-type address in its on-device HELP and readback.
        addresses = (self._mute_address,) if self._mute_address else ("1", "1,1,1")
        for address in addresses:
            try:
                reply = await self._ask("AUD-MUTE? " + address, address + ",")
            except (CommandRefusedError, TimeoutError):
                continue
            if reply in (address + ",0", address + ",1"):
                self._mute_address = address
                return
        self._mute_address = None
        raise CommandRefusedError(
            "The switcher did not confirm an output audio mute reading."
        )

    async def poll(self) -> None:
        queries = [
            ("VERSION?", ""),
            ("ROUTE? 1,1", "1,1,"),
            ("VMUTE? 1", "1,"),
            ("AUD-LVL? 1,1", "1,1,"),
            ("DISPLAY? 1", "1,"),
            ("AV-SW-MODE? 1,1", "1,1,"),
        ]
        for n in range(1, 5):
            queries.extend(
                [(f"SIGNAL? {n}", f"{n},"), (f"HDCP-STAT? 0,{n}", f"0,{n},")]
            )
        for query, address in queries:
            try:
                await self._ask(query, address)
            except CommandRefusedError:
                continue
        try:
            await self._read_mute()
        except CommandRefusedError:
            pass  # Optional mute syntax differs by firmware; switching stays available.
        await self._check_gateway()

    async def refresh_children(self) -> None:
        await self.poll()

    async def _liveness_probe(self) -> None:
        await self._ask("MODEL?")

    async def set_device_setting(self, name: str, value: Any) -> None:
        if name != "switch_mode" or value not in MODES:
            raise CommandRefusedError("Choose a listed input selection policy.")
        wanted = f"1,1,{MODES.index(value)}"
        await self._ask("AV-SW-MODE " + wanted, "1,1,")
        if await self._ask("AV-SW-MODE? 1,1", "1,1,") != wanted:
            raise CommandRefusedError(
                "The switcher did not confirm the input selection policy."
            )

    async def _mute_audio(self, mute: bool) -> str:
        if not self._mute_address:
            await self._read_mute()
        address = self._mute_address + ","
        return await self._ask("AUD-MUTE " + address + str(int(mute)), address)

    async def _route_input(self, values: dict) -> str:
        source = str(values.get("input", ""))
        if source not in ("1", "2", "3", "4") or str(values.get("output", 1)) != "1":
            raise CommandRefusedError("Choose HDMI input 1–4 and HDMI Out.")
        return await self._ask("ROUTE 1,1," + source, "1,1,")

    async def _set_volume(self, values: dict) -> str:
        try:
            level = float(values["level"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandRefusedError("Enter an output level in dB.") from exc
        if not -100 <= level <= 15 or round(level, 1) != level:
            raise CommandRefusedError("Choose -100 to +15 dB in increments of 0.1 dB.")
        return await self._ask(f"AUD-LVL 1,1,{level:g}", "1,1,")

    async def send_command(self, command: str, params: dict | None = None) -> Any:
        values = params or {}
        if command in SIMPLE_COMMANDS:
            return await self._ask(*SIMPLE_COMMANDS[command])
        if command == "read_gateway":
            return await self._check_gateway()
        if command in ("mute_audio", "unmute_audio"):
            return await self._mute_audio(command == "mute_audio")
        if command == "route":
            return await self._route_input(values)
        if command == "set_volume":
            return await self._set_volume(values)
        raise CommandRefusedError("Unknown SWT3-41-H command.")

    async def prepare_bridge_port(self, port_id: str, params: dict[str, Any]) -> None:
        if port_id != "serial:1":
            raise CommandRefusedError("Choose the rear RS-232 connector.")
        async with self._gateway_lock:
            self._bound_settings = dict(params)
            await self._read_gateway(raise_on_failure=True)

    async def _check_gateway(self) -> str:
        async with self._gateway_lock:
            return await self._read_gateway(raise_on_failure=False)

    async def _read_gateway(self, *, raise_on_failure: bool) -> str:
        try:
            protocol, port, replies = await self._read_tunnel()
            line = await self._read_line()
            if (protocol, port, replies) != ("TCP", 5001, True):
                raise CommandRefusedError(
                    f"Kramer reports {protocol} {port}, replies {'on' if replies else 'off'}. "
                    "In Control Gateway select Gateway, TCP 5001 and enable replies to new clients."
                )
            self._check_bound_format(line)
            status = f"TCP 5001, replies enabled, {line}. Request status from the attached device."
        except (CommandRefusedError, ConnectionError, TimeoutError, ValueError) as exc:
            detail = str(exc) or "No response to the gateway check."
            status = f"Check Serial Ports in the Kramer web interface. {detail}"
            self.set_state("gateway_status", status)
            if raise_on_failure:
                raise CommandRefusedError(status) from exc
            return status
        self.set_state("gateway_status", status)
        return status

    async def _read_tunnel(self) -> tuple[str, int, bool]:
        try:
            value = await self._ask("COM-ROUTE? 1", "1,")
            return self._record_route(value)
        except (CommandRefusedError, ConnectionError, TimeoutError):
            for key in ("gateway_protocol", "gateway_port", "gateway_replies"):
                self.set_state(key, None)
            raise

    async def _read_line(self) -> str:
        try:
            return self._record_uart(await self._ask("UART? 1", "1,"))
        except (CommandRefusedError, ConnectionError, TimeoutError):
            self.set_state("serial_format", None)
            raise

    def _record_route(self, value: str) -> tuple[str, int, bool]:
        match = re.fullmatch(r"1,(0|1|TCP|UDP),(\d{1,5}),([01]),(\d+)", value, re.I)
        if not match or not 1 <= int(match[2]) <= 65535:
            raise CommandRefusedError(
                "The switcher did not report a valid tunnel. Check Gateway mode."
            )
        protocol = "TCP" if match[1].upper() in ("0", "TCP") else "UDP"
        port, replies = int(match[2]), match[3] == "1"
        self.set_state("gateway_protocol", protocol)
        self.set_state("gateway_port", port)
        self.set_state("gateway_replies", replies)
        return protocol, port, replies

    def _record_uart(self, value: str) -> str:
        match = re.fullmatch(
            r"1,(\d+),([5-8]),([0-4]),(1|1\.5|2)(?:,([01])(?:,[01])?)?", value
        )
        if not match or match[5] == "1":
            raise CommandRefusedError(
                "The switcher did not report a valid RS-232 format."
            )
        line = f"{int(match[1])} {match[2]}{PARITIES[int(match[3])]}{match[4]}"
        self.set_state("serial_format", line)
        return line

    def _check_bound_format(self, line: str) -> None:
        params = self._bound_settings
        if params is None:
            return
        if any(params.get(k, False) for k in ("rtscts", "dsrdtr", "xonxoff")):
            raise CommandRefusedError(
                "Use a downstream device with flow control disabled on this three-wire connector."
            )
        stop = float(params.get("stopbits", 1))
        requested = f"{params.get('baudrate', 9600)} {params.get('bytesize', 8)}{str(params.get('parity', 'N')).upper()}{stop:g}"
        if line != requested:
            raise CommandRefusedError(
                f"Kramer reports {line}. The downstream device requests {requested}. Match their serial settings."
            )
