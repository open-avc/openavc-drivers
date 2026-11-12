"""Kramer SWT3-41-H control simulator. A separate peer can represent serial equipment on TCP 5001."""

from __future__ import annotations

import asyncio
import re

from openavc.simulator.tcp_simulator import TCPSimulator


class KramerSwt3Simulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "kramer_swt3_41_h",
        "name": "Kramer SWT3-41-H Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 5000,
        "delimiter": "\r",
        "initial_state": {
            "model": "SWT3-41-H",
            "firmware": "1.0.65739",
            "source": 1,
            "volume": 0.0,
            "video_muted": 0,
            "audio_muted": 0,
            "switch_mode": 0,
            "sink": 1,
            "signal_1": 1,
            "signal_2": 0,
            "signal_3": 1,
            "signal_4": 0,
            "hdcp_1": 0,
            "hdcp_2": 0,
            "hdcp_3": 0,
            "hdcp_4": 0,
            "gateway_active": True,
            "gateway_protocol": "TCP",
            "gateway_port": 5001,
            "gateway_replies": True,
            "baud": 9600,
            "bits": 8,
            "parity": 0,
            "stops": 1,
            "mute_address": "1,1,1",
        },
        "controls": [
            {
                "type": "select",
                "key": "source",
                "label": "Front Panel HDMI Input",
                "options": ["1", "2", "3", "4"],
            },
            {"type": "toggle", "key": "signal_1", "label": "HDMI 1 Signal"},
            {"type": "toggle", "key": "sink", "label": "Display Connected"},
            {
                "type": "toggle",
                "key": "gateway_active",
                "label": "Serial Gateway Enabled",
            },
            {
                "type": "select",
                "key": "gateway_protocol",
                "label": "Serial Tunnel Protocol",
                "options": ["TCP", "UDP"],
            },
            {
                "type": "select",
                "key": "gateway_port",
                "label": "Serial Tunnel Port",
                "options": ["5001", "5501"],
            },
            {
                "type": "toggle",
                "key": "gateway_replies",
                "label": "Send Replies to New Clients",
            },
            {
                "type": "select",
                "key": "baud",
                "label": "Serial Baud Rate",
                "options": ["9600", "19200", "38400", "57600", "115200"],
            },
            {
                "type": "select",
                "key": "mute_address",
                "label": "Firmware Audio Mute Address",
                "options": ["1", "1,1,1"],
            },
        ],
    }
    _REPORTS = {
        "source": "ROUTE 1,1,",
        "video_muted": "VMUTE 1,",
        "switch_mode": "AV-SW-MODE 1,1,",
        "sink": "DISPLAY 1,",
    }
    _REPORTS.update({f"signal_{n}": f"SIGNAL {n}," for n in range(1, 5)})
    _REPORTS.update({f"hdcp_{n}": f"HDCP-STAT 0,{n}," for n in range(1, 5)})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._notifications = set()

    @staticmethod
    def _frame(body: str) -> bytes:
        return ("~01@" + body + "\r\n").encode("ascii")

    def set_state(self, key, value):
        old = self.get_state(key)
        super().set_state(key, value)
        if old == value or not self.running:
            return
        if key in self._REPORTS:
            data = self._frame(self._REPORTS[key] + str(int(value)))
            task = asyncio.create_task(self.push(data))
            self._notifications.add(task)
            task.add_done_callback(self._notifications.discard)

    async def stop(self):
        for task in self._notifications:
            task.cancel()
        await asyncio.gather(*self._notifications, return_exceptions=True)
        await super().stop()

    def handle_command(self, data: bytes) -> bytes:
        command = data.decode("ascii", "replace").strip()
        if command == "#":
            return self._frame(" OK ")
        verb, _, args = command.removeprefix("#").partition(" ")
        name = verb.rstrip("?")
        result = (
            self._read(name, args) if verb.endswith("?") else self._write(name, args)
        )
        return self._frame(name + " " + result)

    def _read(self, name, args):
        if name in ("MODEL", "VERSION"):
            return self.get_state("model" if name == "MODEL" else "firmware")
        if name in ("COM-ROUTE", "UART") and args == "1":
            return self._serial_read(name)
        return self._read_media(name, args)

    def _read_media(self, name, args):
        readbacks = {
            ("ROUTE", "1,1"): "source",
            ("DISPLAY", "1"): "sink",
            ("AV-SW-MODE", "1,1"): "switch_mode",
            ("VMUTE", "1"): "video_muted",
            ("AUD-LVL", "1,1"): "volume",
            ("AUD-MUTE", self.get_state("mute_address")): "audio_muted",
        }
        for port in range(1, 5):
            readbacks[("SIGNAL", str(port))] = f"signal_{port}"
            readbacks[("HDCP-STAT", f"0,{port}")] = f"hdcp_{port}"
        key = readbacks.get((name, args))
        if not key:
            return "ERR 002"
        value = self.get_state(key)
        return args + "," + (str(float(value)) if key == "volume" else str(int(value)))

    def _serial_read(self, name):
        if name == "UART":
            return "1,{},{},{},{},0,0".format(
                *(self.get_state(k) for k in ("baud", "bits", "parity", "stops"))
            )
        if not self.get_state("gateway_active"):
            return "ERR 003"
        protocol = int(self.get_state("gateway_protocol") == "UDP")
        replies = int(self.get_state("gateway_replies"))
        return f"1,{protocol},{self.get_state('gateway_port')},{replies},0"

    def _write(self, name, args):
        changes = {
            "ROUTE": ("source", "1,1", [1, 2, 3, 4]),
            "AV-SW-MODE": ("switch_mode", "1,1", [0, 1, 2]),
            "VMUTE": ("video_muted", "1", [0, 1]),
            "AUD-MUTE": ("audio_muted", self.get_state("mute_address"), [0, 1]),
        }
        addr, _, value = args.rpartition(",")
        if name == "AUD-LVL" and addr == "1,1":
            return self._volume(value)
        if name not in changes:
            return "ERR 002"
        key, expected, choices = changes[name]
        if addr != expected or value not in tuple(str(n) for n in choices):
            return "ERR 003"
        super().set_state(key, int(value))
        return args

    def _volume(self, value):
        if value in ("++", "--"):
            step = 1 if value == "++" else -1
            level = max(-100, min(15, float(self.get_state("volume")) + step))
        elif re.fullmatch(r"-?\d+(?:\.\d+)?", value) and -100 <= float(value) <= 15:
            level = float(value)
        else:
            return "ERR 003"
        super().set_state("volume", level)
        return f"1,1,{level}"
