"""N2612S and N3312D training encoders. The two control protocols share one allocated
loopback port in simulation. Physical serial equipment uses a separate peer.
Changing a cable in Auto mode selects the last connected source and delivers
the documented HControl event to known subscribers.
"""

from __future__ import annotations
import asyncio
from contextvars import ContextVar
import hmac
import json
from openavc.simulator.self_signed_tls import build_optional_tls, remove_cert_files
from openavc.simulator.tcp_simulator import TCPSimulator

CLIENT = ContextVar("n2612s_client", default="")
AUTH_PHASE = ContextVar("n2612s_password_prefix", default=None)
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
    "MUTE",
    "VMUTE",
    "HDMIINPUT",
    "HDMI2INPUT",
    "INPUTRES",
    "HDMI2INPUTRES",
    "HDMIAUDIO",
    "SAMPLE",
    "LINEGAIN",
    "PLAYMODE",
    "PLAYLIST",
    "ENCMODE",
    "H26XMODE",
    "H26XPROFILE",
    "H26XSHUTDOWN",
    "STREAMURL",
    "KVMEnableStatus",
    "USB20EnableStatus",
    "BAUD",
    "SNUMB",
    "SPAR",
    "SP2S",
)
NETWORK = {
    "SVSI_NETSTATS": "N2612S-SIM001",
    "NAME": "Lectern encoder",
    "MAC": "02:00:00:26:12:01",
    "IP": "192.0.2.12",
    "SWVER": "2.0.13",
    "CHASSISID": "02:00:00:00:00:01",
    "SYSNAME": "AV switch",
    "PORTID": "Gi1/0/12",
    "PORTDESCR": "Lectern",
}
ROOT = "/encoder/systemStatus/"
CABLES = ("HDMIINPUT", "HDMI2INPUT")
TIMINGS = ("INPUTRES", "HDMI2INPUTRES")
NOTICES = {
    key: ROOT + f"hdmi{n}{suffix}"
    for n in (1, 2)
    for key, suffix in (
        (CABLES[n - 1], "CableStatusChanged"),
        (TIMINGS[n - 1], "ResolutionChanged"),
    )
}
HREAD = {ROOT + f"hdmi{n}Status": CABLES[n - 1] for n in (1, 2)}
HREAD.update({ROOT + f"hdmi{n}Resolution": TIMINGS[n - 1] for n in (1, 2)})
HREAD.update({ROOT + f"hdcp{n}Status": f"hdcp{n}Status" for n in (1, 2)})
HREAD.update(
    {
        "/encoder/streamSetup/mwcStream/txEnable": "mwc_tx",
        "/encoder/streamSetup/h.26xStream/txEnable": "h26x_tx",
        "/encoder/streamSetup/mwcStream/previewImage": "image_enabled",
    }
)
FIXED = {
    "idOn": ("ID", "0"),
    "mute": ("MUTE", "1"),
    "unmute": ("MUTE", "0"),
    "vmute": ("VMUTE", "1"),
    "vunmute": ("VMUTE", "0"),
    "live": ("PLAYMODE", "live"),
    "txenable": ("mwc_tx", True),
    "txdisable": ("mwc_tx", False),
    "setSettings:H26xTxEnable": ("h26x_tx", True),
    "setSettings:H26xTxDisable": ("h26x_tx", False),
}
PARAMS = {
    "hdmiAudio": "HDMIAUDIO",
    "setStream": "STREAM",
    "H26xMode": "H26XMODE",
    "H26xProfile": "H26XPROFILE",
}
ENABLE = {"kvmEnable": "KVMEnableStatus", "usbEnable": "USB20EnableStatus"}


RECORDER_STATE = {
    "USBRecPortStatus": "1",
    "USBStatus": "1",
    "USBFree": "32 GB",
    "AVHEnable": "0",
    "recording_simulated": False,
    "record_basename": "Lecture",
}
RECORDER_CONTROLS = [
    {
        "type": "select",
        "key": "USBRecPortStatus",
        "label": "USB recording port",
        "options": ["0", "1"],
    },
    {
        "type": "select",
        "key": "USBStatus",
        "label": "Recording drive (0 absent, 1 present)",
        "options": ["0", "1"],
    },
    {
        "type": "select",
        "key": "USBFree",
        "label": "Drive free space",
        "options": ["32 GB", "512 MB", "0 MB"],
    },
    {
        "type": "select",
        "key": "AVHEnable",
        "label": "Dante AV-H enabled",
        "options": ["0", "1"],
    },
]


def hc_line(verb, path, **values):
    return (verb + " " + json.dumps({"path": path, **values}) + "\n").encode()


class AMXN2612SSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "amx_svsi_n2612s",
        "name": "AMX SVSI N2612S Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 50002,
        "delimiter": "\r",
        "initial_state": {
            "SVSI_TXGEN2": "N2612S-SIM001",
            "NAME": "Lectern encoder",
            "MAC": "02:00:00:26:12:01",
            "IP": "192.0.2.12",
            "SWVER": "2.0.13",
            "ID": "1",
            "VSRC": "auto",
            "VSTS": "1",
            "STREAM": "55",
            "MUTE": "0",
            "VMUTE": "0",
            "HDMIINPUT": "connected",
            "HDMI2INPUT": "disconnected",
            "INPUTRES": "1920x1080p60",
            "HDMI2INPUTRES": "disconnect",
            "HDMIAUDIO": "on",
            "SAMPLE": "48 kHz",
            "LINEGAIN": "0",
            "PLAYMODE": "live",
            "PLAYLIST": "1",
            "ENCMODE": "2",
            "H26XMODE": "H264",
            "H26XPROFILE": "Custom",
            "H26XSHUTDOWN": "0",
            "STREAMURL": "rtsp://192.0.2.12:8554/live",
            "KVMEnableStatus": "0",
            "USB20EnableStatus": "0",
            "BAUD": "9600",
            "SNUMB": "8",
            "SPAR": "none",
            "SP2S": "1",
            "mwc_tx": True,
            "h26x_tx": True,
            "image_enabled": False,
            "hdcp1Status": "Disabled",
            "hdcp2Status": "Disabled",
        },
        "controls": [
            {
                "type": "select",
                "key": "HDMIINPUT",
                "label": "HDMI 1 cable",
                "options": ["connected", "disconnected"],
            },
            {
                "type": "select",
                "key": "INPUTRES",
                "label": "HDMI 1 input timing",
                "options": ["1920x1080p60", "3840x2160p60", "disconnect"],
            },
            {
                "type": "select",
                "key": "HDMI2INPUT",
                "label": "HDMI 2 cable",
                "options": ["connected", "disconnected"],
            },
            {
                "type": "select",
                "key": "HDMI2INPUTRES",
                "label": "HDMI 2 input timing",
                "options": ["1920x1080p60", "3840x2160p60", "disconnect"],
            },
            {
                "type": "select",
                "key": "VSRC",
                "label": "Source selection",
                "options": ["auto", "hdmi1", "hdmi2"],
            },
            {
                "type": "select",
                "key": "STREAM",
                "label": "Encoder stream number",
                "options": ["12", "55", "100"],
            },
            {
                "type": "select",
                "key": "ENCMODE",
                "label": "Encoding mode",
                "options": ["1", "2"],
            },
            {"type": "toggle", "key": "mwc_tx", "label": "MWC transmit"},
            {"type": "toggle", "key": "h26x_tx", "label": "H.26x transmit"},
            {"type": "toggle", "key": "image_enabled", "label": "Web snapshot"},
        ],
        "error_modes": {
            "locked_settings": {
                "label": "Refuse setting changes",
                "description": "Continue reporting the old input, stream "
                "and encoding settings.",
            },
            "refuse_serial": {
                "label": "Refuse serial setup",
                "description": "Keep the current line settings.",
            },
            "communication_timeout": {
                "label": "Silence Direct Control",
                "description": "Keep the control connection open "
                "without answering requests.",
            },
        },
    }

    def __init__(self, device_id, config=None):
        config = config or {}
        model = config.get("encoder_model", "Auto")
        self._recorder = model in ("N3312D", "N3312D-C")
        self.SIMULATOR_INFO = {
            **type(self).SIMULATOR_INFO,
            "tls": bool(config.get("ssl")),
        }
        self._hcontrol_enabled = not self._recorder and not config.get("ssl")
        self._status_fields = STATUS
        if self._recorder:
            self._configure_recorder(model)
        super().__init__(device_id, config)
        self._certs = None
        self._direct = None
        self._subscriptions = {}
        self._notices = set()
        self._last_connected = 1

    def _configure_recorder(self, model):
        info = self.SIMULATOR_INFO
        info["name"] = f"AMX SVSI {model} Simulator"
        omit = {"H26XSHUTDOWN", "KVMEnableStatus", "USB20EnableStatus"}
        seed = {k: v for k, v in info["initial_state"].items() if k not in omit}
        seed.update(RECORDER_STATE)
        seed.update(SVSI_TXGEN2=model + "15601300001", SWVER="1.5.9")
        info["initial_state"] = seed
        info["controls"] = [
            c
            for c in info["controls"]
            if c["key"] not in ("mwc_tx", "h26x_tx", "image_enabled")
        ] + RECORDER_CONTROLS
        info["error_modes"] = {
            **info["error_modes"],
            "protected_source": {
                "label": "HDCP-protected recording source",
                "description": "The encoder accepts a Start command but cannot record the protected source.",
            },
        }
        self._status_fields = tuple(k for k in STATUS if k not in omit) + (
            "USBRecPortStatus",
            "USBStatus",
            "USBFree",
            "AVHEnable",
        )

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
            return self._packet(NETWORK)
        self._direct_command(wire)
        return self._packet(dict.fromkeys(self._status_fields, ""))

    def _packet(self, fields):
        lines = []
        for key, default in fields.items():
            sep = "" if key == "SVSI_TXGEN2" and not self._recorder else ":"
            lines.append(key + sep + str(self.state.get(key, default)))
        return ("\r".join(lines) + "\r").encode()

    def _direct_command(self, wire):
        if "locked_settings" in self.active_errors:
            return
        if self._recorder and wire in ("startRecord", "stopRecord"):
            self._record(wire)
            return
        if wire in FIXED:
            self.set_state(*FIXED[wire])
            return
        cmd, _, value = wire.partition(":")
        self._parameter_command(cmd, value)

    def _record(self, wire):
        if wire == "stopRecord":
            self.set_state("recording_simulated", False)
            return
        if (
            self.get_state("USBRecPortStatus") == "1"
            and self.get_state("USBStatus") == "1"
        ):
            full = self.get_state("USBFree", "").partition(" ")[0] == "0"
            self.set_state(
                "recording_simulated",
                not full and "protected_source" not in self.active_errors,
            )

    def _parameter_command(self, cmd, value):
        if cmd == "vidsrc" and value in ("auto", "hdmi1", "hdmi2"):
            self.set_state("VSRC", value)
        elif cmd == "local" and value in tuple(map(str, range(1, 9))):
            self.set_state("PLAYMODE", "local")
            self.set_state("PLAYLIST", value)
        elif cmd == "setSettings":
            self._setting(value)
        elif cmd == "serSet":
            self._serial(value)

    def _serial(self, value):
        fields = value.split(",")
        if len(fields) == 4 and "refuse_serial" not in self.active_errors:
            for key, val in zip(("BAUD", "SNUMB", "SPAR", "SP2S"), fields):
                self.set_state(key, val)

    def _setting(self, wire):
        key, _, val = wire.partition(":")
        if key == "setStream" and self._dante_routing():
            return
        if self._recorder and self._record_setting(key, val):
            return
        self._stream_setting(key, val)

    def _dante_routing(self):
        return (
            self.config.get("routing_owner") == "Dante AV"
            or self.get_state("AVHEnable") == "1"
        )

    def _stream_setting(self, key, val):
        if key in PARAMS:
            self.set_state(PARAMS[key], val)
        elif key in ENABLE:
            self.set_state(ENABLE[key], "1" if val == "on" else "0")
        elif key == "encodeMode" and val in ("SingleStream", "MultiStream"):
            self.set_state("ENCMODE", "2" if val == "MultiStream" else "1")
        elif key == "Preview":
            self.set_state("image_enabled", val == "on")

    def _record_setting(self, key, val):
        if key == "usbEnable":
            self.set_state("USBRecPortStatus", "1" if val == "on" else "0")
            return True
        if key == "recordBaseName":
            self.set_state("record_basename", val)
            return True
        return False

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

    def set_state(self, key, value):
        old = self.get_state(key)
        super().set_state(key, value)
        if old == value:
            return
        if key in ("USBStatus", "USBRecPortStatus") and value == "0":
            self.set_state("recording_simulated", False)
        self._update_input(key, value)

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
        selected = {"hdmi1": 1, "hdmi2": 2, "auto": self._last_connected}.get(mode)
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
