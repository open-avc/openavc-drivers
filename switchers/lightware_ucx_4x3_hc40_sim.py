"""UCX-4x3-HC40 LW3 simulator. Changes to subscribed nodes produce CHG messages.
Simulates switcher functions and serial configuration on the control connection.
The two raw RS-232 sockets belong to the equipment; no fixed listeners are
started in simulation. Use a downstream peer to test a bridged project.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from math import ceil
import re
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

CLIENT = ContextVar("ucx_client", default="direct")
OUTPUTS = 3
SERIALS = 2
PROPERTY_SPECS = {
    "/V1/MANAGEMENT/UID/PACKAGE.Version": ("firmware", "str", False),
    "/V1/MANAGEMENT/LABEL.DeviceLabel": ("device_label", "str", True),
    "/V1/EDID.EdidStatus": ("edid_status", "str", False),
    "/V1/MEDIA/VIDEO/XP/O1.ConnectedSource": ("output.1.source", "source", False),
    "/V1/MEDIA/VIDEO/XP/O1.Mute": ("output.1.mute", "bool", True),
    "/V1/MEDIA/VIDEO/XP/O1.Lock": ("output.1.lock", "bool", True),
    "/V1/MEDIA/VIDEO/O1.HdcpMode": ("output.1.hdcp_mode", "str", True),
    "/V1/MEDIA/VIDEO/O1.Output5VMode": ("output.1.output_5v", "str", True),
    "/V1/MEDIA/VIDEO/O1.OutputSignalType": ("output.1.signal_type", "str", True),
    "/V1/MEDIA/VIDEO/O1.EmbeddedAudioMute": (
        "output.1.embedded_audio_mute",
        "bool",
        True,
    ),
    "/V1/MEDIA/VIDEO/XP/O2.ConnectedSource": ("output.2.source", "source", False),
    "/V1/MEDIA/VIDEO/XP/O2.Mute": ("output.2.mute", "bool", True),
    "/V1/MEDIA/VIDEO/XP/O2.Lock": ("output.2.lock", "bool", True),
    "/V1/MEDIA/VIDEO/O2.HdcpMode": ("output.2.hdcp_mode", "str", True),
    "/V1/MEDIA/VIDEO/O2.Output5VMode": ("output.2.output_5v", "str", True),
    "/V1/MEDIA/VIDEO/O2.OutputSignalType": ("output.2.signal_type", "str", True),
    "/V1/MEDIA/VIDEO/O2.EmbeddedAudioMute": (
        "output.2.embedded_audio_mute",
        "bool",
        True,
    ),
    "/V1/MEDIA/VIDEO/XP/O3.ConnectedSource": ("output.3.source", "source", False),
    "/V1/MEDIA/VIDEO/XP/O3.Mute": ("output.3.mute", "bool", True),
    "/V1/MEDIA/VIDEO/XP/O3.Lock": ("output.3.lock", "bool", True),
    "/V1/MEDIA/VIDEO/O3.HdcpMode": ("output.3.hdcp_mode", "str", True),
    "/V1/MEDIA/VIDEO/O3.Output5VMode": ("output.3.output_5v", "str", True),
    "/V1/MEDIA/VIDEO/O3.OutputSignalType": ("output.3.signal_type", "str", True),
    "/V1/MEDIA/VIDEO/O3.EmbeddedAudioMute": (
        "output.3.embedded_audio_mute",
        "bool",
        True,
    ),
    "/V1/MEDIA/VIDEO/I1.SignalPresent": ("input.1.signal", "bool", False),
    "/V1/MEDIA/VIDEO/I1/HDCP.AllowedHdcpVersion": ("input.1.hdcp_version", "str", True),
    "/V1/MEDIA/VIDEO/I2.SignalPresent": ("input.2.signal", "bool", False),
    "/V1/MEDIA/VIDEO/I2/HDCP.AllowedHdcpVersion": ("input.2.hdcp_version", "str", True),
    "/V1/MEDIA/VIDEO/I3.SignalPresent": ("input.3.signal", "bool", False),
    "/V1/MEDIA/VIDEO/I3/HDCP.AllowedHdcpVersion": ("input.3.hdcp_version", "str", True),
    "/V1/MEDIA/VIDEO/I4.SignalPresent": ("input.4.signal", "bool", False),
    "/V1/MEDIA/VIDEO/I4/HDCP.AllowedHdcpVersion": ("input.4.hdcp_version", "str", True),
    "/V1/MEDIA/AUDIO/O4.VolumePercent": ("audio_volume_percent", "float", True),
    "/V1/MEDIA/AUDIO/O4.VolumedB": ("audio_volume_db", "float", True),
    "/V1/MEDIA/AUDIO/O4.Balance": ("audio_balance", "int", True),
    "/V1/MEDIA/AUDIO/O4.Mute": ("audio_mute", "bool", True),
    "/V1/MEDIA/AUDIO/XP/O4.ConnectedSource": ("audio_source", "source", False),
    "/V1/MEDIA/AUDIO/XP/O4.Mute": ("audio_crosspoint_mute", "bool", True),
    "/V1/MEDIA/SERIAL/P1.Baudrate": ("serial_1_baudrate", "str", True),
    "/V1/MEDIA/SERIAL/P1.Parity": ("serial_1_parity", "str", True),
    "/V1/MEDIA/SERIAL/P1.StopBits": ("serial_1_stopbits", "str", True),
    "/V1/MEDIA/SERIAL/P1.DataBits": ("serial_1_databits", "int", False),
    "/V1/MANAGEMENT/NETWORK/SERVICES/UART1.Port": ("serial_1_tcp_port", "int", False),
    "/V1/MEDIA/SERIAL/P2.Baudrate": ("serial_2_baudrate", "str", True),
    "/V1/MEDIA/SERIAL/P2.Parity": ("serial_2_parity", "str", True),
    "/V1/MEDIA/SERIAL/P2.StopBits": ("serial_2_stopbits", "str", True),
    "/V1/MEDIA/SERIAL/P2.DataBits": ("serial_2_databits", "int", False),
    "/V1/MANAGEMENT/NETWORK/SERVICES/UART2.Port": ("serial_2_tcp_port", "int", False),
    "/V1/MEDIA/USB/XP/H1.ConnectedSource": ("usb_host", "str", False),
    "/V1/MEDIA/USB/AUTOSELECT/H1.Policy": ("usb_policy", "str", True),
    "/V1/MEDIA/USB/AUTOSELECT/H1.VideoFollowPort": ("usb_follow_output", "str", True),
}


def _word(value: Any, kind: str) -> str:
    if kind == "bool":
        return "true" if value else "false"
    if kind == "source":
        return "I" + str(value) if value else "0"
    return "".join("\\" + c if c in "\\{}#%()\r\n\t" else c for c in str(value))


def _typed(value: Any, kind: str) -> Any:
    if kind in ("int", "source"):
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "bool":
        return value is True or str(value).lower() in ("true", "1")
    return value


class LightwareTaurusSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "lightware_ucx_4x3_hc40",
        "name": "Lightware UCX-4x3-HC40 Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 6107,
        "delimiter": "\r\n",
        "initial_state": {
            "firmware": "2.22.9b1",
            "device_label": "UCX-4x3-HC40",
            "edid_status": "F47:E1;F47:E2;F47:E3;F47:E4",
            "output.1.source": 1,
            "output.1.mute": False,
            "output.1.lock": False,
            "output.1.hdcp_mode": "Auto",
            "output.1.output_5v": "On",
            "output.1.signal_type": "Auto",
            "output.1.embedded_audio_mute": False,
            "output.2.source": 2,
            "output.2.mute": False,
            "output.2.lock": False,
            "output.2.hdcp_mode": "Auto",
            "output.2.output_5v": "On",
            "output.2.signal_type": "Auto",
            "output.2.embedded_audio_mute": False,
            "output.3.source": 3,
            "output.3.mute": False,
            "output.3.lock": False,
            "output.3.hdcp_mode": "Auto",
            "output.3.output_5v": "On",
            "output.3.signal_type": "Auto",
            "output.3.embedded_audio_mute": False,
            "input.1.signal": True,
            "input.1.hdcp_version": "HDCP 2.2",
            "input.2.signal": True,
            "input.2.hdcp_version": "HDCP 2.2",
            "input.3.signal": True,
            "input.3.hdcp_version": "HDCP 2.2",
            "input.4.signal": True,
            "input.4.hdcp_version": "HDCP 2.2",
            "audio_volume_percent": 75.0,
            "audio_volume_db": -9.75,
            "audio_balance": 0,
            "audio_mute": False,
            "audio_source": 1,
            "audio_crosspoint_mute": False,
            "serial_1_baudrate": "9600",
            "serial_1_parity": "None",
            "serial_1_stopbits": "1",
            "serial_1_databits": 8,
            "serial_1_tcp_port": 8001,
            "serial_2_baudrate": "9600",
            "serial_2_parity": "None",
            "serial_2_stopbits": "1",
            "serial_2_databits": 8,
            "serial_2_tcp_port": 8002,
            "usb_host": "U1",
            "usb_policy": "Off",
            "usb_follow_output": "O1",
        },
        "controls": [
            {
                "type": "select",
                "key": "output.1.source",
                "label": "HDMI Output 1 Input (0 disconnects)",
                "options": ["0", "1", "2", "3", "4", "5"],
            },
            {
                "type": "select",
                "key": "output.2.source",
                "label": "HDMI Output 2 Input (0 disconnects)",
                "options": ["0", "1", "2", "3", "4", "5"],
            },
            {
                "type": "select",
                "key": "output.3.source",
                "label": "HDMI Output 3 Input (0 disconnects)",
                "options": ["0", "1", "2", "3", "4", "5"],
            },
            {
                "type": "toggle",
                "key": "input.1.signal",
                "label": "USB-C Input 1 Signal",
            },
            {
                "type": "toggle",
                "key": "input.2.signal",
                "label": "USB-C Input 2 Signal",
            },
            {"type": "toggle", "key": "input.3.signal", "label": "HDMI Input 3 Signal"},
            {"type": "toggle", "key": "input.4.signal", "label": "HDMI Input 4 Signal"},
            {"type": "toggle", "key": "audio_mute", "label": "Analog Audio Mute"},
            {
                "type": "slider",
                "key": "audio_volume_percent",
                "label": "Analog Volume (%)",
                "min": 0,
                "max": 100,
            },
            {
                "type": "select",
                "key": "usb_host",
                "label": "Computer Using USB Hub",
                "options": ["0", "U1", "U2", "U3", "U4"],
            },
        ],
        "error_modes": {
            "refuse_serial_write": {
                "label": "Refuse Serial Setting Changes",
                "description": "Reject serial writes while continuing to answer reads.",
            },
            "communication_timeout": {
                "label": "Stop Answering",
                "description": "Stop replying to requests.",
                "behavior": "no_response",
            },
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._properties = PROPERTY_SPECS
        self._by_key = {spec[0]: (p, spec) for p, spec in self._properties.items()}
        self._subscriptions: dict[str, set[str]] = {}
        self._notice_tasks: set[asyncio.Task] = set()

    async def on_client_connected(self, client_id: str) -> None:
        CLIENT.set(client_id)
        self._subscriptions = {
            cid: nodes
            for cid, nodes in self._subscriptions.items()
            if cid in self._clients
        }
        self._subscriptions[client_id] = set()

    async def stop(self) -> None:
        for task in self._notice_tasks:
            task.cancel()
        await asyncio.gather(*self._notice_tasks, return_exceptions=True)
        self._notice_tasks.clear()
        self._subscriptions.clear()
        await super().stop()

    def set_state(self, key: str, value: Any) -> None:
        old = self.get_state(key)
        spec = getattr(self, "_by_key", {}).get(key)
        if spec:
            value = _typed(value, spec[1][1])
        if key == "audio_volume_db":
            value = -95.62 if value <= -95.62 else min(0, ceil(value / 0.375) * 0.375)
        elif key == "audio_volume_percent":
            value = max(0, min(100, ceil(value)))
        super().set_state(key, value)
        if old != value and spec:
            self._queue_notice(spec, value)

    def _queue_notice(self, spec: tuple, value: Any) -> None:
        if not self._running:
            return
        task = asyncio.create_task(self._push_change(spec[0], _word(value, spec[1][1])))
        self._notice_tasks.add(task)
        task.add_done_callback(self._notice_tasks.discard)

    async def _push_change(self, path: str, value: str) -> None:
        node = path.rsplit(".", 1)[0]
        data = f"CHG {path}={value}\r\n".encode("ascii")
        for cid in tuple(self._clients):
            if node in self._subscriptions.get(cid, set()):
                await self.push_to(cid, data)

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("ascii", "replace").strip()
        signed = re.fullmatch(r"([0-9A-Fa-f]{4})#(.*)", text)
        command = signed[2] if signed else text
        response = self._dispatch(command)
        if signed:
            response = "{" + signed[1] + "\r\n" + response + "}\r\n"
        return response.encode("ascii")

    def _dispatch(self, text: str) -> str:
        verb, _, rest = text.partition(" ")
        if verb in ("OPEN", "CLOSE"):
            subscriptions = self._subscriptions.setdefault(CLIENT.get(), set())
            if verb == "OPEN":
                subscriptions.add(rest)
            else:
                subscriptions.discard(rest)
            return f"{'o-' if verb == 'OPEN' else 'c-'} {rest}\r\n"
        if verb == "GET":
            return self._read(rest)
        if verb == "SET":
            return self._set(rest)
        if verb == "CALL":
            return self._call(rest)
        return f"nE {rest} %E001:Invalid command\r\n"

    def _read(self, path: str) -> str:
        spec = self._properties.get(path)
        if not spec:
            return f"pE {path} %E002:Not exist\r\n"
        key, kind, writable = spec
        prefix = "pw" if writable else "pr"
        return f"{prefix} {path}={_word(self.get_state(key), kind)}\r\n"

    def _set(self, text: str) -> str:
        path, _, word = text.partition("=")
        spec = self._properties.get(path)
        if not spec or not spec[2]:
            return f"pE {path} %E002:Not writable\r\n"
        if "refuse_serial_write" in self.active_errors and "/SERIAL/" in path:
            return f"pE {path} %E003:Write refused\r\n"
        key, kind, _ = spec
        value = re.sub(r"\\(.)", r"\1", word)
        if kind == "bool":
            value = word == "true"
        self.set_state(key, value)
        return self._read(path)

    def _call(self, text: str) -> str:
        match = re.fullmatch(r"([^:]+):([^()]+)\((.*)\)", text)
        if not match:
            return f"mE {text} %E001:Invalid method\r\n"
        node, method, args = match.groups()
        if node.endswith("/XP"):
            applied = self._route(node, method, args)
        elif "/CEC" in node:
            applied = method in ("send", "sendUserControlClick")
        elif node == "/V1/EDID":
            applied = self._edid(method, args)
        elif "/MEDIA/AUDIO/" in node:
            applied = self._step(node, method, args)
        else:
            applied = (node, method) in (
                ("/V1/MANAGEMENT/UI", "identifyMe"),
                ("/V1/SYS", "restart"),
            )
        if not applied:
            return f"mF {node}:{method} %E003:Invalid value\r\n"
        return f"mO {node}:{method}=\r\n"

    def _route(self, node: str, method: str, args: str) -> bool:
        if node == "/V1/MEDIA/USB/XP":
            return self._route_usb(method, args)
        if method == "switchAll":
            return self._route_all(node, args)
        match = re.fullmatch(r"(I[1-5]|0):O([1-4])", args)
        if not match or method != "switch":
            return False
        return self._switch_outputs(
            node, int(match[1].removeprefix("I")), [int(match[2])]
        )

    def _route_usb(self, method: str, args: str) -> bool:
        match = re.fullmatch(r"(U[1-4]|0):H1", args)
        if method != "switch" or not match:
            return False
        self.set_state("usb_host", match[1])
        return True

    def _route_all(self, node: str, source: str) -> bool:
        if node != "/V1/MEDIA/VIDEO/XP" or not re.fullmatch(r"I[1-5]|0", source):
            return False
        return self._switch_outputs(
            node, int(source.removeprefix("I")), list(range(1, OUTPUTS + 1))
        )

    def _switch_outputs(self, node: str, source: int, ports: list[int]) -> bool:
        if not self._allows_route(node, source, ports):
            return False
        for port in ports:
            path = f"{node}/O{port}.ConnectedSource"
            self.set_state(self._properties[path][0], source)
        return True

    def _allows_route(self, node: str, source: int, ports: list[int]) -> bool:
        if node == "/V1/MEDIA/AUDIO/XP":
            return ports == [4] and source <= 4
        if node != "/V1/MEDIA/VIDEO/XP" or max(ports) > OUTPUTS:
            return False
        return self._can_route(source, ports)

    def _can_route(self, source: int, ports: list[int]) -> bool:
        remaining = [i for i in range(1, OUTPUTS + 1) if i not in ports]
        if any(self.get_state(f"output.{i}.lock") for i in ports):
            return False
        incompatible = {1: 5, 5: 1}.get(source)
        return incompatible is None or not any(
            self.get_state(f"output.{i}.source") == incompatible for i in remaining
        )

    def _step(self, node: str, method: str, args: str) -> bool:
        prop = {"stepVolumedB": "VolumedB", "stepVolumePercent": "VolumePercent"}.get(
            method
        )
        spec = self._properties.get(f"{node}.{prop}")
        if not spec:
            return False
        minimum, maximum = (-95.62, 0) if prop == "VolumedB" else (0, 100)
        self.set_state(
            spec[0],
            max(minimum, min(maximum, float(self.get_state(spec[0])) + float(args))),
        )
        return True

    def _edid(self, method: str, args: str) -> bool:
        current = dict(
            part.split(":", 1)[::-1]
            for part in self.get_state("edid_status").split(";")
        )
        if method == "switch" and ":" in args:
            if not self._edid_switch(current, args):
                return False
        elif method in ("switchAll", "reset"):
            source = {"switchAll": args, "reset": "F47"}[method]
            current = dict.fromkeys(current, source)
        elif method != "copy":
            return False
        self.set_state("edid_status", ";".join(f"{v}:{k}" for k, v in current.items()))
        return True

    def _edid_switch(self, current: dict, args: str) -> bool:
        source, target = args.split(":", 1)
        if target not in current:
            return False
        current[target] = source
        return True
