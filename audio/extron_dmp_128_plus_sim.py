"""DMP 128 Plus SIS simulator with a sparse commissioned room design."""

import asyncio
from contextvars import ContextVar
import re

from openavc.simulator.tcp_simulator import TCPSimulator


PARTS = {
    "DMP 128 Plus": "60-1511-01",
    "DMP 128 Plus AT": "60-1511-10",
    "DMP 128 Plus C": "60-1512-01",
    "DMP 128 Plus C AT": "60-1512-10",
    "DMP 128 Plus C V": "60-1513-01",
    "DMP 128 Plus C V AT": "60-1513-10",
}

# A controller can discover groups, but cannot read their friendly designer names.
DESIGN = {2: (6, [60001, 60000]), 9: (12, [40100]), 45: (21, [60000, 40000])}
NAMES = {
    "I": {1: "Lectern Mic", 2: "Wireless Mic"},
    "O": {1: "Main Left", 2: "Main Right"},
    "G": {1: "Presentation", 17: "Room Combine", 32: "Lecture Capture"},
}


class Dmp128PlusSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "extron_dmp_128_plus",
        "name": "Extron DMP 128 Plus simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 22023,
        "delimiter": "\r",
        "initial_state": {
            "model": "DMP 128 Plus C AT",
            "firmware": "1.14",
            "part_number": "60-1512-10",
            "room_level": -18.0,
            "mic_muted": False,
            "mic_meter": -28.0,
            "output_meter": -13.5,
            "lower_limit": -50.0,
            "upper_limit": -5.0,
        },
        "controls": [
            {
                "type": "select",
                "key": "model",
                "label": "Processor model (reconnect after changing)",
                "options": [
                    "DMP 128 Plus C AT",
                    "DMP 128 Plus",
                    "DMP 128 Plus AT",
                    "DMP 128 Plus C",
                    "DMP 128 Plus C V",
                    "DMP 128 Plus C V AT",
                ],
            },
            {
                "type": "slider",
                "key": "room_level",
                "label": "Room level (dB)",
                "min": -50,
                "max": -5,
                "step": 0.1,
            },
            {"type": "toggle", "key": "mic_muted", "label": "Lectern mute"},
            {
                "type": "slider",
                "key": "mic_meter",
                "label": "Lectern signal (dBFS)",
                "min": -150,
                "max": 0,
                "step": 0.1,
            },
            {
                "type": "slider",
                "key": "output_meter",
                "label": "Main output signal (dBFS)",
                "min": -150,
                "max": 0,
                "step": 0.1,
            },
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "DSP stops answering.",
                "behavior": "no_response",
            }
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.design = dict(DESIGN)
        self.names = {key: dict(names) for key, names in NAMES.items()}
        self._session = ContextVar("dmp_session", default=None)
        self._sessions = {}
        self._offline = {"verbose": 0, "group": 0}
        self._stream = None
        if self.config.get("model"):
            self.set_state("model", self.config["model"])

    async def on_client_connected(self, client_id):
        session = {"verbose": 0, "group": 0}
        self._sessions[client_id] = session
        self._session.set(session)
        if self._stream is None:
            self._stream = asyncio.create_task(self._meter_loop())
        return None

    async def _meter_loop(self):
        while True:
            await asyncio.sleep(0.5)
            for client, session in tuple(self._sessions.items()):
                if client not in self._clients:
                    self._sessions.pop(client, None)
                    continue
                slot = session["group"]
                if slot in self.design and session["verbose"] == 3:
                    values = [self._meter(oid) for oid in sorted(self.design[slot][1])]
                    await self.push_to(
                        client, self._line(f"GrpmV{slot}*" + "*".join(map(str, values)))
                    )

    async def stop(self):
        if self._stream is not None:
            self._stream.cancel()
            await asyncio.gather(self._stream, return_exceptions=True)
            self._stream = None
        self._sessions.clear()
        await super().stop()

    @staticmethod
    def _line(value):
        return (value + "\r\n").encode("latin-1")

    def _meter(self, oid):
        key = {40000: "mic_meter", 60000: "output_meter"}.get(oid)
        return round(-10 * self.get_state(key, -150))

    def handle_command(self, data):
        command = data.decode("latin-1").strip("\r\n").upper()
        session = self._session.get() or self._offline
        if command.startswith("\x1b"):
            answer = self._extended(command[1:], session)
        elif command.endswith("."):
            answer = self._preset(command[:-1])
        else:
            model = self.get_state("model")
            answer = {
                "1I": model,
                "N": PARTS.get(model, "60-0000-00"),
                "Q": self.get_state("firmware"),
            }.get(command, "E10")
        return self._line(answer)

    def _extended(self, cmd, session):
        if cmd == "3CV":
            session["verbose"] = 3
            return "Vrb3"
        if cmd.endswith("GRPU"):
            return self._subscribe(cmd, session)
        if cmd.endswith("GRPM"):
            return self._group(cmd)
        if cmd.endswith("AU"):
            return self._block(cmd)
        match = re.fullmatch(r"(\d+)N([IOG])", cmd)
        if match:
            return self._name(int(match[1]), match[2], session["verbose"])
        return "E10"

    def _name(self, slot, family, verbose):
        default = {"I": f"Input {slot}", "O": f"Output {slot}", "G": ""}[family]
        name = self.names[family].get(slot, default)
        return f"Nm{family.lower()}{slot:02},{name}" if verbose >= 2 else name

    def _subscribe(self, cmd, session):
        match = re.fullmatch(r"G(\d*)GRPU", cmd)
        if not match:
            return "E10"
        if match[1]:
            slot = int(match[1])
            if slot and self.design.get(slot, (0,))[0] != 21:
                return "E13"
            session["group"] = slot
        return f"GrpuG{session['group']:02}"

    def _group(self, cmd):
        match = re.fullmatch(r"([POLD])(\d+)(?:\*(-?\d+))?GRPM", cmd)
        if not match:
            return "E10"
        op, slot, value = match[1], int(match[2]), match[3]
        if slot not in self.design:
            return "E13"
        kind, members = self.design[slot]
        prefix = f"Grpm{op}{slot}*"
        if op == "P":
            return prefix + str(kind)
        if op == "O":
            return prefix + "*".join(map(str, members))
        if op == "L":
            return (
                prefix
                + f"{round(self.get_state('upper_limit') * 10)}*{round(self.get_state('lower_limit') * 10)}"
            )
        return self._group_value(kind, value, prefix)

    def _group_value(self, kind, value, prefix):
        if kind == 6:
            if value is not None:
                level = int(value) / 10
                if (
                    not self.get_state("lower_limit")
                    <= level
                    <= self.get_state("upper_limit")
                ):
                    return "E13"
                self.set_state("room_level", level)
            return prefix + str(round(self.get_state("room_level") * 10))
        if kind != 12:
            return "E14"
        if value is not None:
            if value not in ("0", "1"):
                return "E13"
            self.set_state("mic_muted", value == "1")
        return prefix + str(int(self.get_state("mic_muted")))

    def _block(self, cmd):
        match = re.fullmatch(r"([GMV])(\d+)(?:\*(-?\d+))?AU", cmd)
        if not match:
            return "E10"
        op, oid, value = match[1], int(match[2]), match[3]
        if op == "V":
            return f"1*{self._meter(oid):04}"
        base = 40100 if 40100 <= oid < 40112 else 60000
        if not base <= oid < base + (12 if base == 40100 else 8):
            return "E13"
        key = f"{op}{oid}"
        if value is not None:
            self.set_state(key, int(value))
        return f"Ds{op}{oid}*{self.get_state(key, 0)}"

    def _preset(self, raw):
        if not raw.isdigit() or int(raw) not in self.names["G"]:
            return "E13"
        slot = int(raw)
        self.set_state("room_level", -30.0 if slot == 17 else -12.0)
        self.set_state("mic_muted", False)
        return f"Rpr{slot:02}"
