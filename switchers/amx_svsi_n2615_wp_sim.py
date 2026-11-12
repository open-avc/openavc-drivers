"""AMX N2615 wallplate training simulator. The two protocols share an assigned
loopback port. Auto switches to the last connector plugged in. USB-C timing
changes without a cable transition require a Direct Control status read.
"""

from __future__ import annotations
import asyncio
from contextvars import ContextVar
import hmac
import json
from openavc.simulator.self_signed_tls import build_optional_tls, remove_cert_files
from openavc.simulator.tcp_simulator import TCPSimulator

CLIENT = ContextVar("n2615_client", default="")
AUTH_PHASE = ContextVar("n2615_password_prefix", default=None)
CABLES = ("HDMIINPUT", "USBINPUT")
TIMINGS = ("INPUTRES", "USBINPUTRES")
ROOT = "/encoder/systemStatus/"
NOTICES = {
    "HDMIINPUT": ROOT + "hdmiCableStatusChanged",
    "INPUTRES": ROOT + "hdmiResolutionChanged",
    "USBINPUT": ROOT + "usbCCableStatusChanged",
}
HREAD = {
    "/encoder/streamSetup/mwcStream/txEnable": "mwc_tx",
    "/encoder/streamSetup/mwcStream/previewImage": "image_enabled",
}
NETWORK = {
    "SVSI_NETSTATS": "",
    "IP": "",
    "MAC": "",
    "SWVER": "",
    "NAME": "",
    "CHASSISID": "02:00:00:00:00:01",
    "SYSNAME": "AV switch",
    "PORTID": "Gi1/0/15",
    "PORTDESCR": "Lectern wallplate",
}
FIXED = {
    "idOn": ("ID", "0"),
    "mute": ("MUTE", "1"),
    "unmute": ("MUTE", "0"),
    "vmute": ("VMUTE", "1"),
    "vunmute": ("VMUTE", "0"),
    "live": ("PLAYMODE", "live"),
    "txenable": ("mwc_tx", True),
    "txdisable": ("mwc_tx", False),
}
STATUS = (
    "SVSI_TXGEN2",
    "NAME",
    "MAC",
    "IP",
    "SWVER",
    "ID",
    "VSRC",
    "VSTS",
    "STREAM",
    "PLAYMODE",
    "PLAYLIST",
    "MUTE",
    "VMUTE",
    "HDMIINPUT",
    "INPUTRES",
    "USBINPUT",
    "USBINPUTRES",
    "HDMIAUDIO",
    "SAMPLE",
    "KVMEnableStatus",
    "USB20EnableStatus",
)


def hc_line(verb, path, **values):
    return (verb + " " + json.dumps({"path": path, **values}) + "\n").encode()


class AMXN2615WPSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "amx_svsi_n2615_wp",
        "name": "AMX SVSI N2615 Wallplate Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 50002,
        "delimiter": "\r",
        "initial_state": {
            "SVSI_TXGEN2": "N2615WP15601300202",
            "NAME": "Lectern wallplate",
            "MAC": "02:00:00:26:15:01",
            "IP": "192.0.2.15",
            "SWVER": "1.0.0",
            "ID": "1",
            "VSRC": "auto",
            "VSTS": "1",
            "STREAM": "55",
            "PLAYMODE": "live",
            "PLAYLIST": "0",
            "MUTE": "0",
            "VMUTE": "0",
            "HDMIINPUT": "connected",
            "INPUTRES": "1920x1080p60",
            "USBINPUT": "disconnected",
            "USBINPUTRES": "disconnect",
            "HDMIAUDIO": "on",
            "SAMPLE": "48k",
            "KVMEnableStatus": "0",
            "USB20EnableStatus": "0",
            "mwc_tx": True,
            "image_enabled": False,
        },
        "controls": [
            {
                "type": "select",
                "key": "HDMIINPUT",
                "label": "HDMI cable",
                "options": ["connected", "disconnected"],
            },
            {
                "type": "select",
                "key": "INPUTRES",
                "label": "HDMI timing",
                "options": ["1920x1080p60", "3840x2160p60", "disconnect"],
            },
            {
                "type": "select",
                "key": "USBINPUT",
                "label": "USB-C cable",
                "options": ["connected", "disconnected"],
            },
            {
                "type": "select",
                "key": "USBINPUTRES",
                "label": "USB-C timing",
                "options": ["1920x1080p60", "3840x2160p60", "disconnect"],
            },
            {
                "type": "select",
                "key": "VSRC",
                "label": "Input switching policy",
                "options": ["auto", "hdmi", "usbc"],
            },
            {
                "type": "select",
                "key": "PLAYMODE",
                "label": "Playback",
                "options": ["live", "local"],
            },
            {
                "type": "select",
                "key": "STREAM",
                "label": "SVSI stream",
                "options": ["12", "55", "100"],
            },
            {"type": "toggle", "key": "mwc_tx", "label": "MWC transmit"},
            {"type": "toggle", "key": "image_enabled", "label": "Web snapshot"},
        ],
        "error_modes": {
            "locked_settings": {
                "label": "Refuse setting changes",
                "description": "Reply with the actual, unchanged wallplate settings.",
            },
            "communication_timeout": {
                "label": "Silence Direct Control",
                "description": "Leave the connection open without answering.",
            },
        },
    }

    def __init__(self, device_id, config=None):
        config = config or {}
        model = config.get("simulation_variant", "NMX-ENC-N2615D-WP-NA")
        self.SIMULATOR_INFO = {
            **type(self).SIMULATOR_INFO,
            "tls": bool(config.get("ssl")),
        }
        self.SIMULATOR_INFO["initial_state"] = {
            **self.SIMULATOR_INFO["initial_state"],
            "SVSI_TXGEN2": model + "15601300202",
        }
        super().__init__(device_id, config)
        self._hcontrol_enabled = not config.get("ssl")
        self._certs = self._direct = None
        self._subscriptions = {}
        self._notices = set()
        self._last_connected = 1

    def _direct_command(self, wire):
        if "locked_settings" in self.active_errors:
            return
        if wire in FIXED:
            self.set_state(*FIXED[wire])
            return
        cmd, _, value = wire.partition(":")
        if cmd == "vidsrc" and value in ("hdmi", "usbc", "auto"):
            self.set_state("VSRC", value)
        elif cmd == "local" and value in tuple("12345678"):
            self.set_state("PLAYLIST", str(int(value) - 1))
            self.set_state("PLAYMODE", "local")
        elif cmd == "setSettings":
            self._setting(value)

    def _setting(self, setting):
        key, _, val = setting.partition(":")
        if key == "setStream":
            self._set_stream(val)
            return
        if val not in ("on", "off"):
            return
        if key == "hdmiAudio":
            self.set_state("HDMIAUDIO", val)
        elif key == "Preview":
            self.set_state("image_enabled", val == "on")
        elif key in ("kvmEnable", "usbEnable"):
            field = "KVMEnableStatus" if key == "kvmEnable" else "USB20EnableStatus"
            self.set_state(field, "1" if val == "on" else "0")

    def _set_stream(self, value):
        if self.config.get("routing_owner") == "Dante AV-A":
            return
        if not value.isascii() or not value.isdecimal() or len(value) > 5:
            return
        if 1 <= int(value) <= 32511:
            self.set_state("STREAM", value)

    def set_state(self, key, value):
        old = self.get_state(key)
        super().set_state(key, value)
        if old != value:
            self._update_input(key, value)

    async def start(self, port):
        if not self.SIMULATOR_INFO["tls"]:
            return await super().start(port)
        ctx, self._certs = build_optional_tls(
            self.SIMULATOR_INFO, self.config, self.name
        )
        if ctx is None:
            raise RuntimeError("Unable to create the simulation certificate.")
        try:
            self._server = await asyncio.start_server(
                self._handle_client, "127.0.0.1", port, ssl=ctx
            )
        except BaseException:
            remove_cert_files(self._certs)
            raise
        self._port = self._server.sockets[0].getsockname()[1]
        self._running = True

    async def stop(self):
        for task in self._notices:
            task.cancel()
        await asyncio.gather(*self._notices, return_exceptions=True)
        try:
            await super().stop()
        finally:
            remove_cert_files(self._certs)
            self._certs = None
            self._subscriptions.clear()

    async def on_client_connected(self, client_id):
        CLIENT.set(client_id)
        AUTH_PHASE.set(None)
        self._subscriptions = {
            cid: events
            for cid, events in self._subscriptions.items()
            if cid in self._clients
        }

    async def _read_messages(self, reader, **kwargs):
        lines = await super()._read_messages(reader, **kwargs)
        if not lines or not self.SIMULATOR_INFO["tls"]:
            return lines
        # The password is a prefix for each individual command. Strip it
        # before the simulator's traffic log and command dispatch.
        return [line for line in lines if self._permitted_command(line)]

    def _permitted_command(self, line):
        authorized = AUTH_PHASE.get()
        if authorized is not None:
            AUTH_PHASE.set(None)
            return authorized
        password = (self.config.get("password") or "").encode()
        AUTH_PHASE.set(
            bool(password) and hmac.compare_digest(line.rstrip(b"\r\n"), password)
        )
        return False

    def handle_command(self, data):
        wire = data.decode("ascii", "replace").strip()
        if self._hcontrol_enabled and wire.startswith(("get {", "subscribeevent {")):
            return self._hcontrol(wire)
        client = CLIENT.get()
        if self._direct in self._clients and self._direct != client:
            self._clients[client].close()
            return None
        self._direct = client
        if "communication_timeout" in self.active_errors:
            return None
        if wire == "getNetStatus":
            return self._packet({**NETWORK, "SVSI_NETSTATS": self.state["SVSI_TXGEN2"]})
        self._direct_command(wire)
        return self._packet(dict.fromkeys(STATUS, ""))

    def _packet(self, fields):
        lines = []
        for key, default in fields.items():
            sep = "" if key == "SVSI_TXGEN2" else ":"
            lines.append(key + sep + str(self.state.get(key, default)))
        return ("\r".join(lines) + "\r").encode()

    def _hcontrol(self, wire):
        verb, body = wire.split(" ", 1)
        path = json.loads(body).get("path")
        if verb == "subscribeevent" and path in NOTICES.values():
            self._subscriptions.setdefault(CLIENT.get(), set()).add(path)
            return hc_line("@subscribeevent", path)
        if verb == "get" and path in HREAD:
            return hc_line("@get", path, value=self._reading(HREAD[path]))
        return None

    def _reading(self, key):
        value = self.get_state(key)
        return int(value == "connected") if key in CABLES else value

    def _update_input(self, key, value):
        if key in CABLES:
            self._cable_change(CABLES.index(key) + 1, value)
        elif key == "VSRC":
            self._select_input()
        if key in NOTICES and self._running:
            self._notify(key)

    def _cable_change(self, n, value):
        timing = TIMINGS[n - 1]
        if value == "connected":
            self._last_connected = n
            self._signal_present(timing)
        else:
            self.set_state(timing, "disconnect")
            self._select_remaining_cable(3 - n)
        self._select_input()

    def _signal_present(self, timing):
        if self.get_state(timing) == "disconnect":
            self.set_state(timing, "1920x1080p60")

    def _select_remaining_cable(self, other):
        if self.get_state(CABLES[other - 1]) == "connected":
            self._last_connected = other

    def _select_input(self):
        mode = self.get_state("VSRC")
        selected = {"hdmi": 1, "usbc": 2, "auto": self._last_connected}.get(mode)
        if selected:
            super().set_state("VSTS", str(selected))

    def _notify(self, key):
        event = NOTICES[key]
        data = hc_line("event", event, arguments={"data": self._reading(key)})
        for cid, events in self._subscriptions.items():
            if event in events and cid in self._clients:
                task = asyncio.create_task(self.push_to(cid, data))
                self._notices.add(task)
                task.add_done_callback(self._notices.discard)
