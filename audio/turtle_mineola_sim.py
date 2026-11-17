"""Selectable Mineola XLR bridge sizes. Larger status tables are simulated
extensions of the two-channel table printed in all four manufacturer's guides.
"""

import copy
import re

from openavc.simulator.tcp_simulator import TCPSimulator

INPUT_DB = ("+24dBu", "+14dBu", "+4dBu", "0dBV", "-18dBV", "-35dBV")
OUTPUT_DB = ("+20dBu", "+14dBu", "+4dBu", "0dBV", "-18dBV")
BANKS = ("Flat", "Custom1", "Custom2")
DEFAULTS = {
    "gain": "0",
    "mute": "Off",
    "phantom power": "Off",
    "sensitivity": "0dBV",
    "reference": "0dBV",
    "vol": "50",
    "delay": "0",
    "eq preset": "Flat",
}
DEFAULT_FILTER = ("1", "1000", "0", "1.4", "off")
FILTER = re.compile(r"([1-8]) typ ([1-5]) frq ([\d.]+) val (-?[\d.]+) q ([\d.]+)")
FIELDS = re.compile(r"(phantom power|gain|vol|mute|sensitivity|delay|eq preset) (.+)")


def table_row(kind, n, values):
    name = values["name"]
    order = (
        ("phantom power", "sensitivity", "gain", "mute")
        if kind == "input"
        else ("reference", "vol", "mute", "delay")
    )
    return f"{n:02d}  {name}  " + "  ".join(str(values[field]) for field in order)


def decode_value(prop, value):
    choices = {"sensitivity": INPUT_DB, "reference": OUTPUT_DB, "eq preset": BANKS}
    if prop in choices:
        index = int(value)
        if not 1 <= index <= len(choices[prop]):
            raise ValueError("Unknown selection.")
        return choices[prop][index - 1]
    if prop in ("phantom power", "mute"):
        return {"on": "On", "off": "Off"}[value]
    low, high = {"gain": (-12, 12), "vol": (0, 100), "delay": (0, 50)}[prop]
    if not low <= float(value) <= high:
        raise ValueError("Value outside range.")
    return value


def eq_table(name, filters):
    lines = [f"Dante {name} EQ"]
    for band, (typ, hz, gain, q, enabled) in enumerate(filters, 1):
        lines.append(
            f"{band}: Type: {typ}, Frequency: {hz}Hz, Value: {gain}dB, Q: {q} {enabled}"
        )
    return "\r\n".join(lines)


class TurtleMineolaSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "turtle_mineola",
        "name": "Turtle AV Mineola Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 8000,
        "delimiter": "\r\n",
        "initial_state": {
            "model_size": "2x2",
            "input_1_mute": False,
            "output_1_volume": 50,
        },
        "controls": [
            {
                "key": "model_size",
                "label": "Bridge Size",
                "type": "select",
                "options": ["2x2", "4x4", "8x8", "16x16"],
            },
            {"key": "input_1_mute", "label": "Input 1 Mute", "type": "toggle"},
            {
                "key": "output_1_volume",
                "label": "Output 1 Volume",
                "type": "slider",
                "min": 0,
                "max": 100,
                "step": 1,
            },
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self.channels = {}
        self.equalizers = {}
        self.scenes = {}
        self.master = {"vol": "50", "mute": "off"}
        configured = str((config or {}).get("channel_count", "2"))
        if configured in ("2", "4", "8", "16"):
            self.set_state("model_size", f"{configured}x{configured}")

    @property
    def size(self):
        return int(self.get_state("model_size", "2x2").split("x")[0])

    def channel(self, kind, n):
        label = ("XLR IN" if kind == "input" else "XLR OUT") + str(n)
        return self.channels.setdefault((kind, n), {**DEFAULTS, "name": label})

    def set_state(self, key, value):
        super().set_state(key, value)
        if key == "input_1_mute":
            self.channel("input", 1)["mute"] = "On" if value else "Off"
        if key == "output_1_volume":
            self.channel("output", 1)["vol"] = str(value)
        if key == "model_size":
            self.master.pop("member", None)

    def _status(self):
        lines = [
            "=" * 60,
            "Status Info XLR22 Audio Bridge",
            "Web v1.00.03 MCU v2.0.0 DEP v1.3.3.5_20250226",
            "Power Standby Mode Auto_Standby Baud",
            "On Sleep 10min 115200",
        ]
        headers = {
            "input": "Input Name Phantom_Power Sensitivity Gain(dB) Mute",
            "output": "Output Name Gain Volume Mute Delay (ms)",
        }
        for kind, header in headers.items():
            lines.append(header)
            lines.extend(
                table_row(kind, n, self.channel(kind, n))
                for n in range(1, self.size + 1)
            )
        return "\r\n".join(
            [*lines, "TCP/IP Telnet MAC (PRIMARY) MAC (SECONDARY)", "=" * 60]
        )

    def _filters(self, kind, n):
        bank = self.channel(kind, n)["eq preset"]
        return self.equalizers.setdefault((kind, n, bank), [DEFAULT_FILTER] * 8)

    def _eq_write(self, kind, n, command):
        filters = self._filters(kind, n)
        if match := re.fullmatch(r"([1-8]) (on|off)", command):
            index = int(match[1]) - 1
            filters[index] = (*filters[index][:4], match[2])
        elif match := FILTER.fullmatch(command):
            index = int(match[1]) - 1
            filters[index] = (*match.groups()[1:], filters[index][-1])
        else:
            return None
        return f"{self.channel(kind, n)['name']} EQ {index + 1}"

    def _write_field(self, kind, n, match):
        prop, value = match.groups()
        if kind == "output" and prop == "gain":
            prop = "reference"
        parsed = decode_value(prop, value)
        self.channel(kind, n)[prop] = parsed
        if (kind, n, prop) == ("output", 1, "vol"):
            self.set_state("output_1_volume", int(parsed))
        if (kind, n, prop) == ("input", 1, "mute"):
            self.set_state("input_1_mute", parsed == "On")
        return f"{self.channel(kind, n)['name']} {prop}: {parsed}"

    def _channel_command(self, match):
        verb, kind, raw, cmd = match.groups()
        n = int(raw)
        if not 1 <= n <= self.size:
            return None
        if verb == "get":
            return self._get_channel(kind, n, cmd)
        if fields := FIELDS.fullmatch(cmd):
            return self._write_field(kind, n, fields)
        if cmd.startswith("eq "):
            return self._eq_write(kind, n, cmd[3:])
        return None

    def _get_channel(self, kind, n, cmd):
        values = self.channel(kind, n)
        if cmd == "eq setting":
            return eq_table(values["name"], self._filters(kind, n))
        if cmd == "eq preset":
            return f"{values['name']} PEQ: {values['eq preset']}"
        return None

    def _mask(self):
        return self.master.get("member", "1" * self.size)

    def _master(self, match):
        verb, key, value = match.groups()
        if verb == "get":
            return self._master_feedback(key)
        if key == "member":
            return self._set_mask(value)
        self.master[key] = str(decode_value(key, value)).lower()
        for n, enabled in enumerate(self._mask(), 1):
            if enabled == "1":
                self._write_field("output", n, FIELDS.fullmatch(f"{key} {value}"))
        return self._master_feedback(key)

    def _master_feedback(self, key):
        if key == "member":
            return self._mask()
        return f"Master {'volume' if key == 'vol' else key}: {self.master[key]}"

    def _set_mask(self, value):
        digits = re.fullmatch(rf"<([01]{{{self.size}}})>", value or "")
        if not digits:
            return None
        self.master["member"] = digits[1]
        return f"Set master member: {digits[1]}"

    def _scene(self, match):
        action, slot = match.groups()
        if action == "save":
            self.scenes[slot] = copy.deepcopy(
                (self.channels, self.master, self.equalizers)
            )
            return f"Save to preset {slot}"
        if action == "clear":
            self.scenes.pop(slot, None)
            return f"Clear preset {slot}"
        if slot not in self.scenes:
            return None
        self.channels, self.master, self.equalizers = copy.deepcopy(self.scenes[slot])
        self.set_state("input_1_mute", self.channel("input", 1)["mute"] == "On")
        self.set_state("output_1_volume", int(self.channel("output", 1)["vol"]))
        return f"Recall preset {slot}"

    def _reply(self, cmd):
        if cmd == "get status":
            return self._status()
        if cmd == "get type":
            return "XLR22"
        if match := re.fullmatch(r"(get|set) (input|output) (\d+) (.+)", cmd):
            return self._channel_command(match)
        if match := re.fullmatch(r"(get|set) master (member|mute|vol)(?: (.+))?", cmd):
            return self._master(match)
        if match := re.fullmatch(r"set preset (save|recall|clear) ([1-5])", cmd):
            return self._scene(match)
        return None

    def handle_command(self, data):
        try:
            reply = self._reply(data.decode("ascii").strip().lower())
        except (ValueError, KeyError):
            reply = None
        return (reply + "\r\n").encode("utf-8") if reply else None
