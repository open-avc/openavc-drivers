"""Extron IN1806/IN1808 SIS simulator. Each client's verbose mode is independent."""

from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar
from openavc.simulator.tcp_simulator import TCPSimulator

ESC = "\x1b"
MODELS = {
    "IN1806": "60-1663-01",
    "IN1808": "60-1615-01",
    "IN1808 IPCP SA": "60-1615-02",
    "IN1808 IPCP MA 70": "60-1615-03",
    "IN1808 IPCP Q SA": "60-1615-92",
    "IN1808 IPCP Q MA 70": "60-1615-93",
}
MODES = ("Full power", "Standby", "Standby with TP power")
AUTOS = ("Manual", "User priority", "Last connected")
MUTES = ("Visible", "Black video", "Video and sync off")
ROUTE = {"%": ("video_input", "Vid"), "$": ("audio_input", "Aud")}
FRONT = {"video_input": ("Vid", "%"), "audio_input": ("Aud", "$")}
INITIAL_NAMES = (
    "Lectern PC",
    "Laptop",
    "Document camera",
    "Wireless presentation",
    "HDMI 5",
    "HDMI 6",
    "Wall plate",
    "Stage input",
)
PRESETS = {2: "Document camera", 7: "Wide image"}
GROUP_LIMITS = {1: (120, -800), 3: (120, -800), 5: (120, -800), 9: (0, -600)}
SESSION = ContextVar("in1808_client", default=None)


def line(text):
    return (text + "\r\n").encode("latin-1")


class ExtronIN1808Simulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "extron_in1808",
        "name": "Extron IN1806 / IN1808",
        "transport": "tcp",
        "default_port": 22023,
        "delimiter": "\r",
        "initial_state": {
            "model": "IN1808 IPCP Q MA 70",
            "firmware": "2.00",
            "video_input": 1,
            "audio_input": 1,
            "loop_input": 1,
            "input_2_signal": True,
            "frozen": False,
            "power_mode": "Full power",
            "auto_switch": "Manual",
            "room_volume": -20.0,
            "room_muted": False,
        },
        "controls": [
            {
                "type": "select",
                "key": "model",
                "label": "Scaler model (reconnect after changing)",
                "options": [
                    "IN1806",
                    "IN1808",
                    "IN1808 IPCP SA",
                    "IN1808 IPCP MA 70",
                    "IN1808 IPCP Q SA",
                    "IN1808 IPCP Q MA 70",
                ],
            },
            {"type": "toggle", "key": "input_2_signal", "label": "Laptop video signal"},
            {
                "type": "slider",
                "key": "video_input",
                "label": "Main video input",
                "min": 1,
                "max": 8,
                "step": 1,
            },
            {"type": "toggle", "key": "frozen", "label": "Freeze picture"},
            {"type": "toggle", "key": "room_muted", "label": "Room master mute"},
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "Scaler stops answering.",
                "behavior": "no_response",
            }
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mode = {}
        self._tasks = set()
        self._names = {}
        self._groups = {
            g: (-200 if g in GROUP_LIMITS else 0) for g in (1, 2, 3, 4, 5, 6, 9, 10)
        }
        self._serial_line = "9600,N,8,1"
        self._dsp = {}
        self._blanks = {i: 0 for i in (1, 2, 3)}
        self._presets = dict(PRESETS)
        if self.config.get("model") in MODELS:
            super().set_state("model", self.config["model"])

    @property
    def size(self):
        return 6 if self.get_state("model") == "IN1806" else 8

    async def on_client_connected(self, client_id):
        SESSION.set(client_id)
        self._mode = {c: mode for c, mode in self._mode.items() if c in self._clients}
        self._mode[client_id] = 0
        model = self.get_state("model")
        return line(
            f"(c) Copyright 2022, Extron Electronics, {model}, V2.00, {MODELS[model]}"
        )

    async def stop(self):
        await super().stop()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._mode.clear()

    def _response(self, tag, bare=None):
        if bare is None or self._mode.get(SESSION.get(), 0) in (2, 3):
            return tag
        return bare

    async def _broadcast(self, text, exclude=None):
        for client in tuple(self._clients):
            if client != exclude and self._mode.get(client, 0) in (1, 3):
                await self.push_to(client, line(text))

    def _emit(self, text, exclude=None):
        if self._clients:
            task = asyncio.create_task(self._broadcast(text, exclude))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def handle_command(self, data):
        cmd = data.decode("latin-1").rstrip("\r\n")
        result = self._fixed(cmd)
        if result is None:
            result = self._command(cmd)
        return line(str(result))

    def _fixed(self, cmd):
        model = self.get_state("model")
        if cmd == ESC + "3CV":
            self._mode[SESSION.get()] = 3
            return "Vrb3"
        if cmd == "1I":
            return self._response("Inf01*" + model, model)
        if cmd == "N":
            return self._response("Pno" + MODELS[model], MODELS[model])
        if cmd == "Q":
            fw = self.get_state("firmware")
            return self._response("Ver01*" + fw, fw)
        if cmd == ESC + "MD":
            return f"Pmd{self.get_state('uart_base', 2000)}"
        if cmd == ESC + "O2LRPT":
            return f"LrptO2*{self.get_state('insertion_mode', 1)}"
        if cmd == ESC + "0LS":
            return self._signal_report()
        return None

    def _command(self, cmd):
        for pattern, handler in self._commands():
            match = re.fullmatch(pattern, cmd)
            if match:
                return handler(*match.groups())
        return "E10"

    def _commands(self):
        return (
            (ESC + r"9(?:\*(\d+,[NOEMS],[78],[12]))?CP", self._serial),
            (r"(\d+)\*1([!%$])", self._route),
            (r"1([%$])", self._route_read),
            (ESC + r"(\d*)LOUT", self._loop),
            (ESC + r"([IO])(\d+)(VNAM|ANAM)", self._name),
            (r"(\d+)\*\\", self._format),
            (ESC + r"([IO])(\d+)HDCP", self._hdcp),
            (r"(\d+)\*([0-2]?)B", self._blank),
            (r"1(?:\*([01]))?F", self._freeze),
            (ESC + r"([0-2]?)(AUSW|PSAV)", self._mode_command),
            (ESC + r"([DL])(\d+)(?:\*([+-]?\d+))?GRPM", self._group),
            (ESC + r"([GM])(60\d{3})(?:\*([+-]?\d+))?AU", self._audio),
            (ESC + r"2\*(\d+)PNAM", self._preset_name),
            (r"2\*(\d+)\.", self._recall),
        )

    def _serial(self, settings):
        if settings:
            self._serial_line = settings
        return self._response(f"Cpn9 Ccp{self._serial_line}", self._serial_line)

    def _route(self, raw, plane):
        n = int(raw)
        if n not in range(1, self.size + 1) and (n, plane) != (9, "$"):
            return "E12"
        if plane == "!":
            super().set_state("video_input", n)
            super().set_state("audio_input", n)
            report = f"In{n:02}*1 All"
        else:
            key, tag = ROUTE[plane]
            super().set_state(key, n)
            report = f"In{n:02}*1 {tag}"
        self._emit(report, SESSION.get())
        return report

    def _route_read(self, plane):
        key, tag = ROUTE[plane]
        n = self.get_state(key)
        return self._response(f"In{n:02}*1 {tag}", f"{n:02}")

    def _loop(self, raw):
        if raw:
            if int(raw) not in range(1, self.size + 1):
                return "E12"
            super().set_state("loop_input", int(raw))
            self._emit(f"Lout{int(raw):02}", SESSION.get())
        n = self.get_state("loop_input")
        return self._response(f"Lout{n:02}", f"{n:02}")

    def _name(self, side, raw, what):
        n = int(raw)
        if side == "O":
            default = {1: "Projection", 2: "Overflow", 3: "Lectern monitor"}.get(n)
        elif what == "ANAM" and n == 9:
            default = "Auxiliary program audio"
        else:
            default = INITIAL_NAMES[n - 1] if 1 <= n <= self.size else None
        if default is None:
            return "E12"
        txt = self._names.get((side, n, what), default)
        return self._response(f"{what.title()}{side}{n:02}*{txt}", txt)

    def _signal(self, n):
        return bool(self.get_state(f"input_{n}_signal", n <= 3))

    def _signal_report(self):
        bits = "*".join(str(int(self._signal(n))) for n in range(1, self.size + 1))
        return self._response("IN00 " + bits, bits)

    def _format(self, raw):
        n = int(raw)
        if not 1 <= n <= self.size:
            return "E12"
        fmt = (3 if n == 1 else 2) if self._signal(n) else 0
        return self._response(f"Vtyp{n:02}*{fmt}", str(fmt))

    def _hdcp(self, side, raw):
        n = int(raw)
        if side == "I":
            code = 2 if self._signal(n) else 0
        else:
            code = self.get_state(f"output_{n}_hdcp", 1)
        return self._response(f"Hdcp{side} {n:02}*{code}", str(code))

    def _blank(self, raw, val):
        n = int(raw)
        if n not in self._blanks:
            return "E12"
        if val:
            self._blanks[n] = int(val)
            self._emit(f"Vmt{n}*{val}", SESSION.get())
        return self._response(f"Vmt{n}*{self._blanks[n]}", str(self._blanks[n]))

    def _freeze(self, val):
        if val is not None:
            super().set_state("frozen", val == "1")
            self._emit(f"Frz1*{val}", SESSION.get())
        current = int(self.get_state("frozen"))
        return self._response(f"Frz1*{current}", str(current))

    def _mode_command(self, val, word):
        key, prefix, choices = {
            "PSAV": ("power_mode", "Psav", MODES),
            "AUSW": ("auto_switch", "Ausw", AUTOS),
        }[word]
        if val:
            super().set_state(key, choices[int(val)])
            self._emit(prefix + val, SESSION.get())
        index = choices.index(self.get_state(key))
        return self._response(f"{prefix}{index}", str(index))

    def _group(self, op, raw, val):
        g = int(raw)
        if g not in self._groups:
            return "E12"
        if op == "L":
            return self._limits(g)
        if val is not None:
            hi, lo = GROUP_LIMITS.get(g, (1, 0))
            if not lo <= int(val) <= hi:
                return "E13"
            self._groups[g] = int(val)
            self._room_setting(g, int(val))
            self._emit(f"GrpmD{g}*{val}", SESSION.get())
        return self._response(f"GrpmD{g}*{self._groups[g]}", str(self._groups[g]))

    def _limits(self, g):
        if g not in GROUP_LIMITS:
            return "E13"
        hi, lo = GROUP_LIMITS[g]
        return self._response(f"GrpmL{g}*{hi}*{lo}", f"{hi}*{lo}")

    def _room_setting(self, g, value):
        if g == 9:
            super().set_state("room_volume", value / 10)
        elif g == 10:
            super().set_state("room_muted", bool(value))

    def _audio(self, op, raw, val):
        oid = int(raw)
        model = self.get_state("model")
        channels = 12 if model.endswith("SA") else 11 if model.endswith("70") else 10
        if not 60000 <= oid < 60000 + channels:
            return "E12"
        if val is not None:
            self._dsp[op, oid] = int(val)
            self._emit(f"Ds{op}{oid}*{val}", SESSION.get())
        return f"Ds{op}{oid}*{self._dsp.get((op, oid), 0)}"

    def _preset_name(self, raw):
        n = int(raw)
        name = self._presets.get(n, "[unassigned]")
        return self._response(f"Pnam2*{n:03},{name}", name)

    def _recall(self, raw):
        n = int(raw)
        if n not in self._presets:
            return "E11"
        self._emit(f"2Rpr{n:03}", SESSION.get())
        return f"2Rpr{n:03}"

    def set_state(self, key, value):
        if key == "model" and value not in MODELS:
            return
        if key == "video_input" and int(value) not in range(1, self.size + 1):
            return
        super().set_state(key, value)
        text = self._front_report(key, value)
        if text:
            self._emit(text)

    def _front_report(self, key, value):
        if key in FRONT:
            plane, _ = FRONT[key]
            return f"In{int(value):02}*1 {plane}"
        if key in ("room_volume", "room_muted"):
            g, v = (
                (9, round(float(value) * 10))
                if key == "room_volume"
                else (10, int(value))
            )
            self._groups[g] = v
            return f"GrpmD{g}*{v}"
        if key.startswith("input_") and key.endswith("_signal"):
            return "IN00 " + "*".join(
                str(int(self._signal(n))) for n in range(1, self.size + 1)
            )
        if key == "frozen":
            return f"Frz1*{int(value)}"
        return None


SIMULATOR_CLASS = ExtronIN1808Simulator
