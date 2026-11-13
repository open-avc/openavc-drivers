"""Contemporary Research 232-ATSC 4K, tuner side of the May 2026 manual.

The cable map reproduces the manufacturer's four-station LC example. The
simulated air map additionally exercises analog and one-part virtual channels.
"""

from __future__ import annotations

import asyncio
import re

from openavc.simulator.tcp_simulator import TCPSimulator

MAPS = {
    "Cable": (
        ("D15-1", "98-1", "CNBC"),
        ("D15-2", "98-2", "NTD"),
        ("D16-1", "99-1", "CNN"),
        ("D16-2", "99-2", "Fox"),
    ),
    "Air": (("D6-2", "6-2", "WLOCAL"), ("D1032", "27-1", "RADIO"), ("A32", "32", "")),
}
RESOLUTION = {
    "1080i": "0",
    "720p": "1",
    "480p": "2",
    "480i": "3",
    "1080p": "4",
    "No signal": "N",
}
OUTPUTS = ("1080i", "720p", "480p", "480i", "1080p", "2160p60", "Auto", "2160p30")
REPORTS = {
    "output_setting": "SS",
    "channel": "ST",
    "source": "ST",
    "reception": "ST",
    "power": "ST",
    "volume": "SV",
    "mute": "SV",
    "captions": "SQ",
}
SETS = {
    "P0": ("power", False),
    "P1": ("power", True),
    "VX": ("mute", False),
    "VM": ("mute", True),
    "Q0=0": ("captions", False),
    "Q0=1": ("captions", True),
}


def tune_number(row):
    return row[0][1:] + ("-0" if row[0].startswith("A") else "")


class ContemporaryResearchTunerSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "contemporary_research_232_atsc_4k",
        "name": "Contemporary Research 232-ATSC 4K Simulator",
        "category": "video",
        "transport": "tcp",
        "default_port": 23,
        "delimiter": "\r",
        "initial_state": {
            "source": "Cable",
            "channel": "15-1",
            "reception": "1080i",
            "power": True,
            "volume": 55,
            "mute": False,
            "captions": False,
            "empty_memory": False,
            "output_setting": "Auto",
        },
        "controls": [
            {
                "type": "select",
                "key": "source",
                "label": "Channel Memory",
                "options": ["Air", "Cable"],
            },
            {
                "type": "select",
                "key": "reception",
                "label": "RF Reception",
                "options": ["No signal", "480i", "480p", "720p", "1080i", "1080p"],
            },
            {"type": "toggle", "key": "empty_memory", "label": "Empty Channel Memory"},
            {"type": "toggle", "key": "power", "label": "Power On"},
            {"type": "toggle", "key": "mute", "label": "Audio Muted"},
            {"type": "toggle", "key": "captions", "label": "Closed Captions On"},
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._unit = int(self.config.get("unit_number", 1))
        self._previous = self.get_state("channel")
        self._push_tasks = set()
        self._digits = ""

    def _map(self):
        return () if self.get_state("empty_memory") else MAPS[self.get_state("source")]

    def _listing(self):
        rows = self._map()
        source = self.get_state("source")
        lines = [
            f"Start of Channel List- {source}:{len(rows)}",
            *(":".join(r) for r in rows),
            "End of Channel List",
        ]
        return "\r\n".join(lines)

    def _tuner_report(self):
        number = self.get_state("channel")
        major, sep, sub = number.partition("-")
        thousands, base = divmod(int(major), 1000)
        minor = f"{int(sub):03d}" if sep else f"F{thousands:02d}"
        source = self.get_state("source")[0]
        return f"T{self._power()}{base:03d}U0{source}{RESOLUTION[self.get_state('reception')]}{minor}x0"

    def _power(self):
        return "U" if self.get_state("power") else "M"

    def _report(self, code):
        if code == "ST":
            return self._tuner_report()
        if code == "SV":
            return self._audio_report()
        if code == "SS":
            return self._output_report()
        return f"Q{int(self.get_state('captions'))}13029011xx"

    def _audio_report(self):
        level = self.get_state("volume")
        mute = "M" if self.get_state("mute") else "U"
        return f"V{self._power()}{round(level * 63 / 100):02d}{mute}x{level:03d}"

    def _output_report(self):
        selected = OUTPUTS.index(self.get_state("output_setting"))
        actual = 4 if selected == 6 else selected
        return f"Sxx00842{actual}{selected}xxxx"

    def _wire(self, text):
        return (text + "\r\n").encode()

    def _addressed(self, code):
        return self._wire(f"<{self._unit}{self._report(code)}")

    def set_state(self, key, value):
        # UI/front-panel changes generate unsolicited status on the open link.
        if key == "source" and value != self.get_state(key):
            super().set_state("channel", tune_number(MAPS[value][0]))
        if key == "channel":
            self._previous = self.get_state("channel")
        super().set_state(key, value)
        if self._clients and key in REPORTS:
            task = asyncio.create_task(self.push(self._addressed(REPORTS[key])))
            self._push_tasks.add(task)
            task.add_done_callback(self._push_tasks.discard)

    async def stop(self):
        if self._push_tasks:
            await asyncio.gather(*self._push_tasks)
        await super().stop()

    def handle_command(self, data):
        match = re.fullmatch(rb">([1-9]?)([A-Z0-9=:-]+)", data.strip())
        if not match or int(match[1] or b"1") != self._unit:
            return None
        return self._command(match[2].decode())

    def _command(self, command):
        if command == "ID":
            return self._wire(f"<{self._unit}ID=232-ATSC 4K V1.0")
        if command == "LC":
            return self._wire(self._listing())
        if command in ("ST", "SV", "SQ", "SS"):
            return self._addressed(command)
        update = SETS.get(command) or self._change(command)
        if update:
            key, value = update
            if key == "channel":
                self._previous = self.get_state("channel")
            super().set_state(key, value)
            return self._addressed(REPORTS[key])
        return None

    def _change(self, command):
        if command.startswith("KK="):
            return self._remote_key(command)
        level = re.fullmatch(r"VH=([0-9]{1,3})", command)
        if level and int(level[1]) <= 100:
            return "volume", int(level[1])
        tune = re.fullmatch(r"TC=([0-9]{1,4}|[0-9]{1,3}[-:][0-9]{1,3})", command)
        if tune:
            return "channel", tune[1].replace(":", "-")
        return self._step(command)

    def _remote_key(self, command):
        digit = re.fullmatch(r"KK=1([0-9])", command)
        if digit:
            self._digits = (self._digits + digit[1])[-5:]
            return None
        if command == "KK=21" and self._digits:
            number, self._digits = self._digits, ""
            return "channel", number
        output = re.fullmatch(r"KK=(14[1-8])", command)
        if output:
            return "output_setting", OUTPUTS[int(output[1]) - 141]
        return None

    def _step(self, command):
        if command == "TP":
            return "channel", self._previous
        if command in ("TU", "TD") and self._map():
            channels = [tune_number(row) for row in self._map()]
            current = self.get_state("channel")
            index = channels.index(current) if current in channels else -1
            return "channel", channels[
                (index + (1 if command == "TU" else -1)) % len(channels)
            ]
        return None
