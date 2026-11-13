"""MuxLab 500532 ASCII control simulator. Manual 94-001072-A, pp. 26–41.

Preset names are deliberately untagged. Changes made through the simulator UI
are discovered on the next status poll, as on a device with no subscription.
"""

from __future__ import annotations

import copy
import re

from openavc.simulator.tcp_simulator import TCPSimulator

VIDEO = ("USB-C 1", "USB-C 2", "HDMI 1", "HDMI 2")
USB = ("USB-C 1", "USB-C 2", "USB host 3", "USB host 4")
OUTPUT = ("HDMI out 1", "HDMI/HDBT out 2")
CONNECTOR = ("HDMI out 1", "HDMI out 2", "HDBT out 2")
AUDIO = (*OUTPUT, "Line out", "Dante out")
MODE = ("Follow video", "Manual switch", "Auto switch")
LAYOUT = (
    "Auto",
    "Single",
    "Dual-LR",
    "Dual-TB",
    "PIP-LT",
    "PIP-LB",
    "PIP-RT",
    "PIP-RB",
    "Triple-L",
    "Triple-R",
    "Triple-T",
    "Triple-B",
    "Quad-L",
    "Quad-R",
    "Quad-T",
    "Quad-B",
    "Quad-S",
    "Copy other output",
    "Custom Layout 1",
    "Custom Layout 2",
    "Custom Layout 3",
    "Custom Layout 4",
    "Custom Layout 5",
)
RX = ("HDBT in", "HDMI in")
FIRMWARE = "MCU 1.1.0  Web 1.1.0  FPGA 1.1.0  RX 1.18.02"
DIVIDER = "=" * 80


def onoff(value):
    return "on" if value else "off"


class MuxLab500532Simulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "muxlab_500532",
        "name": "MuxLab 500532 Presentation Switcher Simulator",
        "transport": "tcp",
        "category": "switcher",
        "default_port": 8000,
        "delimiter": "\r\n",
        "initial_state": {
            "power": True,
            "video_1": "USB-C 1",
            "video_2": "HDMI 1",
            "usb_host": "USB host 3",
            "usb_mode": "Manual switch",
            "receiver_ready": True,
            "usb_follow_output": "Output 1",
            "usb_1_power": True,
            "receiver_input": "HDBT in",
            "input_1_connected": True,
            "master_volume": 50,
            "master_mute": False,
            "temperature": 46,
        },
        "controls": [
            {
                "key": "usb_follow_output",
                "label": "Video Output USB Follows",
                "type": "select",
                "options": ["Output 1", "Output 2"],
            },
            {
                "key": "usb_1_power",
                "label": "USB-C 1 Host 5 V Present",
                "type": "toggle",
            },
            {"key": "power", "label": "Power", "type": "toggle"},
            {
                "key": "video_1",
                "label": "Output 1 Front-Panel Source",
                "type": "select",
                "options": ["USB-C 1", "USB-C 2", "HDMI 1", "HDMI 2"],
            },
            {
                "key": "video_2",
                "label": "Output 2 Front-Panel Source",
                "type": "select",
                "options": ["USB-C 1", "USB-C 2", "HDMI 1", "HDMI 2"],
            },
            {
                "key": "usb_host",
                "label": "Current USB Host",
                "type": "select",
                "options": ["USB-C 1", "USB-C 2", "USB host 3", "USB host 4"],
            },
            {
                "key": "usb_mode",
                "label": "USB Mode",
                "type": "select",
                "options": ["Follow video", "Manual switch", "Auto switch"],
            },
            {
                "key": "input_1_connected",
                "label": "USB-C 1 Cable Connected",
                "type": "toggle",
            },
            {"key": "receiver_ready", "label": "Receiver Connected", "type": "toggle"},
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._names = [
            "Lecture",
            "Guest Laptop",
            "Side by Side",
            "Discussion",
            "Seminar",
            "Workshop",
            "Conference",
            "",
        ]
        self._presets = {}
        self._layouts = ["Single", "Single"]
        self._windows = [[1], [3]]
        self._levels = [50] * 4
        self._mutes = [False] * 4
        self._flags = {
            k: [int(k == "enable")] * 3 for k in ("enable", "avmute", "freeze")
        }
        for n in range(1, 9):
            self._save(n)

    def set_state(self, key, value):
        super().set_state(key, value)
        if key in ("video_1", "video_2"):
            self._source(int(key[-1]) - 1, [VIDEO.index(value) + 1])
        if (
            key in ("usb_mode", "usb_follow_output")
            and self.get_state("usb_mode") == "Follow video"
        ):
            self._follow()

    def _source(self, out, sources):
        self._windows[out] = sources
        super().set_state(f"video_{out + 1}", VIDEO[sources[0] - 1])
        if self.get_state("usb_mode") == "Follow video":
            self._follow()

    def _follow(self):
        super().set_state(
            "usb_host",
            USB[self._windows[int(self.get_state("usb_follow_output")[-1]) - 1][0] - 1],
        )

    def _save(self, idx):
        keys = (
            "usb_mode",
            "usb_host",
            "receiver_input",
            "master_volume",
            "master_mute",
            "video_1",
            "video_2",
        )
        config = {k: self.get_state(k) for k in keys}
        self._presets[idx] = copy.deepcopy(
            (
                config,
                self._windows,
                self._layouts,
                self._levels,
                self._mutes,
                self._flags,
            )
        )

    def _recall(self, idx):
        config, self._windows, self._layouts, self._levels, self._mutes, self._flags = (
            copy.deepcopy(self._presets[idx])
        )
        for key, value in config.items():
            super().set_state(key, value)

    def _status(self):
        lines = [
            DIVIDER,
            "Status Info",
            FIRMWARE,
            "Input Cable Resolution ColorSpace ColorDepth HDCP EDID",
        ]
        for idx, name in enumerate(VIDEO):
            ok = idx != 0 or self.get_state("input_1_connected")
            lines.append(
                f"{name} Connected 1920x1080p60 RGB 8bit 1.4 AUTO"
                if ok
                else f"{name} Disconnected -- -- -- -- AUTO"
            )
        lines.append(
            "Output Cable Resolution ColorSpace ColorDepth HDCP Freeze AVMute Turn_on/off Source"
        )
        for idx, name in enumerate(CONNECTOR):
            flags = " ".join(
                onoff(self._flags[key][idx]) for key in ("freeze", "avmute", "enable")
            )
            ports = "/".join(map(str, self._windows[min(idx, 1)]))
            lines.append(
                f"{name} Connected 3840x2160p60Hz RGB 8bit 2.2 {flags} {ports}"
            )
        lines.extend(
            [
                "Power Key Beep IR UsbcAccessNetwork Baud FanSpeed Temp(C) Uptime(Day:Hour:Min:Sec)",
                f"On On Off On All USB-C inputs 115200 Auto {self.get_state('temperature')} 000:00:13:04",
                DIVIDER,
            ]
        )
        return "\r\n".join(lines)

    def handle_command(self, data):
        command = data.decode("ascii", errors="replace").strip()
        try:
            result = self._command(command)
        except (ValueError, IndexError, KeyError):
            result = "Error,invalid parameter!"
        return (result + "\r\n").encode("ascii", errors="replace")

    def _command(self, command):
        basic = {
            "get model": "500532",
            "get power": "power " + onoff(self.get_state("power")),
        }
        if command in basic:
            return basic[command]
        if command in ("power on", "power off"):
            self.set_state("power", command == "power on")
            return (
                "Power on\r\nSystem Initializing...\r\nInitialization Finished!\r\n"
                + FIRMWARE
                if self.get_state("power")
                else "Power off"
            )
        if not self.get_state("power"):
            return "Error,in standby!"
        if command == "status":
            return self._status()
        if command.startswith("get host5v "):
            idx = int(command.split()[-1])
            power = self.get_state("usb_1_power") if idx == 1 else True
            return f"{USB[idx - 1]} {onoff(power)}"
        group = command.split()[1]
        handler = {
            "preset": self._preset,
            "usb": self._usb,
            "output": self._output,
            "master": self._master,
            "rx": self._receiver,
        }.get(group)
        return handler(command) if handler else "Invalid command"

    def _preset(self, command):
        m = re.fullmatch(r"(get|set) preset ([1-8]) name(?: (.{1,16}))?", command)
        if m:
            idx = int(m[2]) - 1
            if m[1] == "get":
                return self._names[idx]
            self._names[idx] = m[3]
            return f"Set preset {idx + 1} name {m[3]}"
        op, idx = command.split()[2:4]
        {"save": self._save, "recall": self._recall}[op](int(idx))
        return f"Set preset {idx} {op}"

    def _usb(self, command):
        if command == "get usb switch":
            return self.get_state("usb_host")
        if command == "get usb switch mode":
            return self.get_state("usb_mode")
        idx = int(command.split()[-1]) - 1
        if "manual" in command:
            if self.get_state("usb_mode") != "Manual switch":
                return "Error,select manual mode!"
            self.set_state("usb_host", USB[idx])
            return "Set " + USB[idx]
        self.set_state("usb_mode", MODE[idx])
        return "Set USB switch mode " + MODE[idx].lower()

    def _receiver(self, command):
        if not self.get_state("receiver_ready"):
            return "Error,RX not ready!"
        if command == "get rx input":
            return self.get_state("receiver_input")
        value = RX[int(command.split()[-1]) - 1]
        self.set_state("receiver_input", value)
        return "Set RX " + value

    def _master(self, command):
        verb, _, _, prop, *args = command.split()
        key = {"vol": "master_volume", "mute": "master_mute"}[prop]
        if verb == "set":
            self.set_state(key, int(args[0]) if prop == "vol" else args[0] == "1")
        value = self.get_state(key)
        return (
            f"Master volume {value}" if prop == "vol" else "Master mute " + onoff(value)
        )

    def _output(self, command):
        action, _, which, field, *args = command.split()
        idx = int(which) - 1
        if field == "audio":
            return self._audio(idx, action, args)
        if field in self._flags:
            self._flags[field][idx] = int(args[0])
            return f"Set {CONNECTOR[idx]} {field} {onoff(int(args[0]))}"
        if field == "display":
            return self._display(idx, action, args)
        if action == "set":
            self._source(idx, [int(i) for i in args])
        sources = " ".join(VIDEO[n - 1] for n in self._windows[idx])
        return (
            "Set " if action == "set" else ""
        ) + f"{OUTPUT[idx]} video source {sources}"

    def _display(self, idx, action, args):
        if action == "set":
            self._layouts[idx] = LAYOUT[int(args[0]) - 1]
            if self._layouts[idx] == "Single":
                self._source(idx, self._windows[idx][:1])
        return (
            "Set " if action == "set" else ""
        ) + f"{OUTPUT[idx]} display {self._layouts[idx]}"

    def _audio(self, idx, action, args):
        prop = args[0]
        if action == "set":
            if prop == "vol":
                self._levels[idx] = int(args[1])
            else:
                self._mutes[idx] = args[1] == "1"
        value = (
            "volume " + str(self._levels[idx])
            if prop == "vol"
            else "mute " + onoff(self._mutes[idx])
        )
        return f"{AUDIO[idx]} {value}"
