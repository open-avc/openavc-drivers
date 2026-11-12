"""SMP 352 SIS simulator for lecture capture and commissioning."""

import asyncio
import re
import time
from contextvars import ContextVar

from openavc.simulator.tcp_simulator import TCPSimulator

PARTS = {"SMP 352": "60-1634-11", "SMP 352 3G-SDI": "60-1634-12"}
INPUTS = (
    "",
    "Lectern laptop",
    "Guest laptop",
    "Analog camera",
    "Room camera",
    "SDI camera",
)
LAYOUTS = ("Lectern + Slides", "Side by Side", "Slides Only", "Camera Only")
STATES = ("Stopped", "Recording", "Paused")


def clock(seconds):
    minutes, sec = divmod(max(0, int(seconds)), 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02}:{minute:02}:{sec:02}"


class Smp352Simulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "extron_smp_352",
        "name": "Extron SMP 352 recorder",
        "category": "streaming",
        "transport": "tcp",
        "default_port": 22023,
        "delimiter": "\r",
        "initial_state": {
            "model": "SMP 352",
            "record_state": "Stopped",
            "mode": "Composite",
            "camera_signal": True,
            "minutes_free": 180,
            "rtmp_reachable": True,
            "secondary": False,
            "markers": 0,
        },
        "controls": [
            {
                "type": "select",
                "key": "model",
                "label": "Recorder model (reconnect after changing)",
                "options": ["SMP 352", "SMP 352 3G-SDI"],
            },
            {
                "type": "select",
                "key": "record_state",
                "label": "Front panel recording state",
                "options": ["Stopped", "Recording", "Paused"],
            },
            {
                "type": "select",
                "key": "mode",
                "label": "Encoding mode",
                "options": ["Composite", "Dual channel"],
            },
            {"type": "toggle", "key": "camera_signal", "label": "Camera input signal"},
            {
                "type": "slider",
                "key": "minutes_free",
                "label": "Internal recording minutes available",
                "min": 0,
                "max": 240,
                "step": 1,
            },
            {
                "type": "toggle",
                "key": "secondary",
                "label": "Also record to front USB (composite mode)",
            },
            {
                "type": "toggle",
                "key": "rtmp_reachable",
                "label": "Primary RTMP destination reachable",
            },
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "Recorder stops answering.",
                "behavior": "no_response",
            }
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.config.get("model"):
            self.set_state("model", self.config["model"])
        self.names = dict(enumerate(INPUTS))
        self.layouts = {
            n: LAYOUTS[n - 1] if n <= len(LAYOUTS) else f"Layout {n}"
            for n in range(1, 17)
        }
        self.sources = [1, 4]
        self.layout = 1
        self._layout_report = "8Rpr01"
        self._used_seconds = 0
        self.streams = {n: {"enabled": False, "rtmp_enabled": False} for n in (1, 2, 3)}
        self._session = ContextVar("smp_client", default=None)
        self._verbose = set()
        self._timer = None
        self._snapshot = {}
        self._last_record = "Stopped"
        self._started = time.monotonic()
        self._duration = 0

    @staticmethod
    def _line(value):
        return (value + "\r\n").encode("latin-1")

    async def on_client_connected(self, client_id):
        self._session.set(client_id)
        if self._timer is None:
            self._snapshot = self._reports()
            self._timer = asyncio.create_task(self._notifications())
        model = self.get_state("model")
        return self._line(
            f"© Copyright 2022, Extron Electronics, {model}, V3.09, {PARTS.get(model, '60-0000-00')}"
        )

    async def _notifications(self):
        while True:
            await asyncio.sleep(0.15)
            self._tick()
            reports = self._reports()
            self._verbose.intersection_update(self._clients)
            changed = [
                line for key, line in reports.items() if self._snapshot.get(key) != line
            ]
            self._snapshot = reports
            if changed:
                for client in tuple(self._verbose):
                    await self.push_to(
                        client, b"".join(self._line(line) for line in changed)
                    )

    def _reports(self):
        reports = {
            "layout": self._layout_report,
            "record": f"RcdrY{STATES.index(self.get_state('record_state'))}",
            "mode": f"Encm1*{int(self._dual())}",
        }
        for channel, slot in enumerate(self.sources, 1):
            reports[channel] = f"In{slot:02}*{channel:02}"
        for slot in self._outputs():
            reports[("enabled", slot)] = (
                f"Strc{slot}*{int(self.streams[slot]['enabled'])}"
            )
            reports[("rtmp", slot)] = (
                f"RtmpE{slot}*{int(self.streams[slot]['rtmp_enabled'])}"
            )
            reports[("live", slot)] = f"RtmpS1*{slot}*{self._live(slot)}"
        return reports

    async def stop(self):
        if self._timer:
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)
            self._timer = None
        self._verbose.clear()
        await super().stop()

    def _tick(self):
        now = time.monotonic()
        state = self.get_state("record_state")
        if self._last_record == "Recording":
            self._duration += now - self._started
            self._used_seconds += now - self._started
        if state == "Stopped":
            self._duration = 0
        self._last_record, self._started = state, now

    def _dual(self):
        return self.get_state("mode") == "Dual channel"

    def _outputs(self):
        return (1, 2, 3) if self._dual() else (1, 3)

    def _last_input(self):
        return 5 if self.get_state("model") == "SMP 352 3G-SDI" else 4

    def _signal(self, slot):
        return slot < 3 or self.get_state("camera_signal")

    def _live(self, slot):
        return int(
            self.streams[slot]["rtmp_enabled"] and self.get_state("rtmp_reachable")
        )

    def _input_report(self):
        result = []
        for channel, slot in zip(("A", "B"), self.sources):
            name = self.names[slot]
            present = self._signal(slot)
            result.append(
                f"Ch{channel}{slot if present else ''}*{name}*"
                + ("1920x1080*60*1" if present else "n/a*n/a*0")
            )
        return ",".join(result)

    def _internal_remaining(self):
        return max(
            0, int(self.get_state("minutes_free")) * 60 - int(self._used_seconds)
        )

    def _time_report(self):
        active = self.get_state("record_state") != "Stopped"
        result = "internal " + clock(self._internal_remaining() if active else 0)
        if self._secondary():
            result += "*usbfront " + clock(
                max(0, 7200 - self._duration) if active else 0
            )
        return result

    def _secondary(self):
        return not self._dual() and self.get_state("secondary")

    def _information(self, cmd):
        part = PARTS.get(self.get_state("model"), "60-0000-00")
        remaining = self._internal_remaining()
        replies = {
            "N": part,
            "1I": self.get_state("model"),
            "Q": "3.09",
            "35I": clock(self._duration),
            "36I": self._time_report(),
            "37I": "internal*usbfront" if self._secondary() else "internal",
            "32I": f"ChA{self.sources[0]}*ChB{self.sources[1]}",
            "42I": self._input_report(),
            "49I": f"1*{self.layouts[1]},{self.layout}*{self.layouts[self.layout]}"
            if not self._dual()
            else "E14",
            "55I": f"Internal*1000*2000*1000*{clock(remaining)}*1",
            "39I": "[name:free space],[level:warning]"
            if remaining < 900
            else "None active",
        }
        return replies.get(cmd)

    def _record(self, value):
        if value == "":
            return str(STATES.index(self.get_state("record_state")))
        if value == "1" and self._internal_remaining() <= 0:
            return "E22"
        if value == "2" and self.get_state("record_state") == "Stopped":
            return "E14"
        self.set_state("record_state", STATES[int(value)])
        self._tick()
        return f"RcdrY{value}"

    def _marker(self):
        if self.get_state("record_state") != "Recording":
            return "E14"
        self.set_state("markers", self.get_state("markers") + 1)
        return "RcdrB"

    def _source(self, n, channel):
        if not 1 <= n <= self._last_input() or channel != (1 if n <= 2 else 2):
            return "E12"
        self.sources[channel - 1] = n
        return f"In{n:02}*{channel:02}"

    def _recall(self, op, n):
        if self._dual() or n not in self.layouts:
            return "E14"
        self.layout = n
        if op == "7":
            self.sources = [1, 4]
        self._layout_report = f"{op}Rpr{n:02}"
        return self._layout_report

    def _stream(self, cmd):
        m = re.fullmatch(r"(E?)([123])(?:\*([01]))?(STRC|RTMP)", cmd)
        if not m:
            return "E10"
        rtmp, n, value, kind = m[1] == "E", int(m[2]), m[3], m[4]
        if n not in self._outputs() or (kind == "RTMP") != rtmp:
            return "E14"
        prop, tag = ("rtmp_enabled", "RtmpE") if rtmp else ("enabled", "Strc")
        if value is None:
            return str(int(self.streams[n][prop]))
        self.streams[n][prop] = value == "1"
        return f"{tag}{n}*{value}"

    def _extended(self, cmd):
        if cmd == "1CV":
            self._verbose.add(self._session.get())
            return "Vrb1"
        if cmd == "1ENCM":
            return str(int(self._dual()))
        if cmd == "BRCDR":
            return self._marker()
        if m := re.fullmatch(r"Y([012]?)RCDR", cmd):
            return self._record(m[1])
        return self._extended_query(cmd)

    def _extended_query(self, cmd):
        info = {"CI": "192.0.2.35", "N3STRC": "Confidence Feed"}
        if cmd in info:
            return info[cmd]
        if m := re.fullmatch(r"([1-5])NI", cmd):
            return self.names[int(m[1])] if int(m[1]) <= self._last_input() else "E12"
        if m := re.fullmatch(r"7\*(\d+)PNAM", cmd):
            return self.layouts.get(int(m[1]), "E13")
        if m := re.fullmatch(r"I([124])HDCP", cmd):
            return "2" if self._signal(int(m[1])) else "0"
        if m := re.fullmatch(r"S([12])\*([123])RTMP", cmd):
            return str(self._live(int(m[2])) if m[1] == "1" else 0)
        return self._stream(cmd)

    def handle_command(self, data):
        cmd = data.decode("latin-1").strip("\r\n").upper()
        self._tick()
        if cmd.startswith("\x1b"):
            reply = self._extended(cmd[1:])
        elif m := re.fullmatch(r"([1-5])\*([12])!", cmd):
            reply = self._source(int(m[1]), int(m[2]))
        elif m := re.fullmatch(r"([78])\*(\d+)\.", cmd):
            reply = self._recall(m[1], int(m[2]))
        else:
            reply = self._information(cmd) or "E10"
        return self._line(reply)
