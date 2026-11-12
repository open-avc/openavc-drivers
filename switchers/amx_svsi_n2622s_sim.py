"""AMX N2422A / N2622S / N3322D Direct Control simulator with N2622S HControl. The two protocol
connections share an allocated loopback port in simulation. A second Direct
Control client cannot take ownership on that port. The ordinary serial
connector is represented by its line settings, without opening a fixed port.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import hmac
import json

from openavc.simulator.self_signed_tls import build_optional_tls, remove_cert_files
from openavc.simulator.tcp_simulator import TCPSimulator

CLIENT = ContextVar("n2622s_client", default="")
AUTH_PHASE = ContextVar("n2622s_password_prefix", default=None)
HROOT = "/decoder/systemStatus/"
NOTICES = {
    "hdmiOutStatus": "hdmiOutCableStatusChanged",
    "hdmiOutResolution": "hdmiOutResolutionChanged",
}
STATUS = {
    "SVSI_RXGEN2": "N2622S-SIM0001",
    "NAME": "Training room decoder",
    "MAC": "02:00:00:00:26:22",
    "IP": "192.0.2.22",
    "SWVER": "2.0.13",
    "ID": "1",
    "STREAM": "12",
    "STREAMAUDIO": "0",
    "VMUTE": "0",
    "MUTE": "0",
    "LINEOUTVOL_L": "65",
    "LINEOUTVOL_R": "65",
    "SCALERBYPASS": "no",
    "MODE": "auto.mode",
    "HDMIOFF": "off",
    "HDMISTATUS": "connected",
    "INPUTRES": "1920x1080p60",
    "PLAYMODE": "live",
    "PLAYLIST": "1",
    "STREAMURL": "rtp://234.1.0.102:5500",
    "RCVMODE": "1",
    "AUDIOSTREAM": "1",
    "USB20EnableStatus": "0",
    "KVMEnableStatus": "0",
    "KVMMainIP": "192.0.2.12",
    "USB20MainIP": "192.0.2.12",
    "wallEnable": "0",
    "wallHorMons": "1",
    "wallVerMons": "1",
    "wallMonPosH": "1",
    "wallMonPosV": "1",
    "BAUD": "9600",
    "SNUMB": "8",
    "SPAR": "none",
    "SP2S": "1",
}
N24_STATUS = {
    "SVSI_RXGEN2": "N2422A-SIM0001",
    "AES67": "0",
    "AESRX": "239.255.10.11:5004",
    "NAME": "Training room N2400 decoder",
    "MAC": "02:00:00:00:24:22",
    "IP": "192.0.2.22",
    "SWVER": "simulated",
    "STREAM": "12",
    "STREAMAUDIO": "0",
    "PLAYMODE": "live",
    "PLAYLIST": "1",
    "USB": "1",
    "KVM": "1",
    "DVIOFF": "off",
    "DVISTATUS": "connected",
    "SCALERBYPASS": "no",
    "MODE": "auto.mode",
    "INPUTRES": "1920x1080p60",
    "HDMIAUDIO": "auto",
    "FRAMEHOLD": "off",
    "VIDOFFNOSTRM": "off",
    "LIVEAUDIOLP": "off",
    "MUTE": "0",
    "VMUTE": "0",
    "LINEOUTVOL_L": "65",
    "LINEOUTVOL_R": "65",
    "BAUD": "9600",
    "SNUMB": "8",
    "SPAR": "none",
    "SP2S": "1",
}
N26_FIELDS = {
    "PLAYLIST",
    "USB20EnableStatus",
    "KVMEnableStatus",
    "KVMMainIP",
    "USB20MainIP",
    "wallEnable",
    "wallHorMons",
    "wallVerMons",
    "wallMonPosH",
    "wallMonPosV",
}
N33_FIELDS = {
    "HDMIAUDIO": "auto",
    "LIVEAUDIOLP": "off",
    "SAMPLE": "48000",
    "AVHENABLE": "0",
    "FRAMEHOLD": "off",
    "VIDOFFNOSTRM": "off",
    "AUDIOSTREAM": "svsi",
}
N33_CONTROLS = [
    {"key": "AVHENABLE", "label": "Dante AV-H owns routing", "options": ["0", "1"]},
    {
        "key": "RCVMODE",
        "label": "Receive mode (1 SVSI, 2 H.26x)",
        "options": ["1", "2"],
    },
    {
        "key": "STREAMURL",
        "label": "External H.26x video route",
        "options": ["rtp://234.1.0.102:5500", "rtsp://192.0.2.30/live"],
    },
    {"key": "PLAYMODE", "label": "Playback mode", "options": ["live", "local"]},
    {
        "key": "AUDIOSTREAM",
        "label": "Audio input type",
        "options": ["svsi", "dante", "H.26x"],
    },
]
NETWORK = {
    "SVSI_NETSTATS": "N2622S-SIM0001",
    "NAME": STATUS["NAME"],
    "MAC": STATUS["MAC"],
    "IP": STATUS["IP"],
    "SWVER": STATUS["SWVER"],
    "CHASSISID": "02:00:00:00:00:01",
    "SYSNAME": "AV access switch",
    "PORTID": "Gi1/0/8",
    "PORTDESCR": "Training room",
}
FIXED = {
    "unmute": {"MUTE": "0"},
    "mute": {"MUTE": "1"},
    "vunmute": {"VMUTE": "0"},
    "vmute": {"VMUTE": "1"},
    "hdmiOn": {"HDMIOFF": "off"},
    "hdmiOff": {"HDMIOFF": "on"},
    "scalerenable": {"SCALERBYPASS": "no"},
    "scalerdisable": {"SCALERBYPASS": "yes"},
    "idOn": {"ID": "0"},
    "live": {"PLAYMODE": "live"},
}
POINTS = {
    "lovolleft": "LINEOUTVOL_L",
    "lovolright": "LINEOUTVOL_R",
    "set": "STREAM",
    "seta": "STREAMAUDIO",
}
ENABLE = {
    "wallEnable": "wallEnable",
    "kvmEnable": "KVMEnableStatus",
    "usbEnable": "USB20EnableStatus",
}


def hc_line(verb, path, **data):
    return (verb + " " + json.dumps({"path": HROOT + path, **data}) + "\n").encode()


class AMXN2622SSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "amx_svsi_n2622s",
        "name": "AMX SVSI N2422A / N2622S / N3322D Decoder Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 50002,
        "delimiter": "\r",
        "initial_state": {
            "HDMISTATUS": "connected",
            "hdmiOutResolution": "1920x1080p60",
            "hdcpStatus": "None",
            "STREAM": "12",
            "STREAMAUDIO": "0",
            "INPUTRES": "1920x1080p60",
            "PLAYMODE": "live",
            "MUTE": "0",
            "VMUTE": "0",
        },
        "controls": [
            {
                "type": "select",
                "key": "HDMISTATUS",
                "label": "Display cable",
                "options": ["connected", "disconnected"],
            },
            {
                "type": "select",
                "key": "hdmiOutResolution",
                "label": "Output resolution",
                "options": ["1920x1080p60", "3840x2160p60", "None"],
            },
            {
                "type": "select",
                "key": "hdcpStatus",
                "label": "HDCP output",
                "options": ["None", "HDCP 2.2"],
            },
            {
                "type": "select",
                "key": "STREAM",
                "label": "External video route",
                "options": ["12", "20", "55"],
            },
            {
                "type": "select",
                "key": "STREAMAUDIO",
                "label": "External audio route (0 follows video)",
                "options": ["0", "12", "20", "55"],
            },
            {
                "type": "select",
                "key": "INPUTRES",
                "label": "Incoming signal",
                "options": ["1920x1080p60", "3840x2160p60", "None"],
            },
        ],
        "error_modes": {
            "locked_routes": {
                "label": "Refuse stream changes",
                "description": "Keep reporting the existing streams.",
            },
            "refuse_serial": {
                "label": "Refuse serial setup",
                "description": "Return the old line settings after serSet.",
            },
            "communication_timeout": {
                "label": "Stop responding",
                "description": "Leave the control connection silent.",
                "behavior": "no_response",
            },
        },
    }

    def __init__(self, device_id, config=None):
        model = (config or {}).get("decoder_model")
        self._n33 = model == "N3322D"
        self._n24 = model == "N2422A"
        self._hcontrol_enabled = model in (None, "Auto", "N2622S")
        self._fields = dict(STATUS)
        self.SIMULATOR_INFO = deepcopy(type(self).SIMULATOR_INFO)
        if self._n24:
            self._n24_profile()
        elif self._n33:
            self._n33_profile(config)
        super().__init__(device_id, config)
        self.SIMULATOR_INFO = {
            **self.SIMULATOR_INFO,
            "tls": bool(self.config.get("ssl")),
        }
        streams = {row["name"]: row["stream"] for row in self.config.get("sources", [])}
        self._keyboard_streams = (
            {
                row["address"]: streams.get(row["name"])
                for row in self.config.get("usb_sources", [])
            }
            if self._n24
            else {}
        )
        self._certs = None
        self._direct = None
        self._subscriptions = {}
        self._notices = set()
        for key, value in self._fields.items():
            self.state.setdefault(key, value)

    def _n24_profile(self):
        self._fields = dict(N24_STATUS)
        info = self.SIMULATOR_INFO
        info["controls"] = [c for c in info["controls"] if c["key"] in self._fields]
        for key, label, options in (
            ("DVISTATUS", "Display cable", ["connected", "disconnected"]),
            ("PLAYMODE", "Playback", ["live", "local"]),
            ("AES67", "AES67 audio selected", ["0", "1"]),
            ("USB", "Keyboard and mouse USB enabled", ["0", "1"]),
            ("KVM", "KVM enabled", ["0", "1"]),
        ):
            info["controls"].append(
                {"type": "select", "key": key, "label": label, "options": options}
            )
        info["initial_state"] = dict(self._fields)

    def _n33_profile(self, config):
        self._fields = {k: v for k, v in STATUS.items() if k not in N26_FIELDS}
        self._fields.update(N33_FIELDS)
        self._fields.update(
            {
                "SVSI_RXGEN2": "N3322D-SIM0001",
                "SWVER": "simulated",
                "MAC": "02:00:00:00:33:22",
                "AVHENABLE": "1" if config.get("routing_owner") == "Dante AV" else "0",
            }
        )
        info = self.SIMULATOR_INFO
        hcontrol = {"hdmiOutResolution", "hdcpStatus"}
        info["controls"] = [c for c in info["controls"] if c["key"] not in hcontrol]
        info["controls"].extend({"type": "select", **c} for c in N33_CONTROLS)
        info["initial_state"] = dict(self._fields)

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
        if not self.SIMULATOR_INFO["tls"] and wire.startswith(
            ("get {", "subscribeevent {")
        ):
            return self._hcontrol(wire) if self._hcontrol_enabled else None
        client = CLIENT.get()
        if self._direct in self._clients and client != self._direct:
            self._clients[client].close()
            return None
        self._direct = client
        if wire == "getNetStatus":
            return self._network_packet()
        for key, value in self._changes(wire).items():
            if key in self._fields:
                self.set_state(key, value)
        return self._packet(self._fields)

    def _network_packet(self):
        header = "SVSI_RXGEN2" if self._n24 else "SVSI_NETSTATS"
        fields = dict(NETWORK)
        del fields["SVSI_NETSTATS"]
        return self._packet({header: self._fields["SVSI_RXGEN2"], **fields})

    def _switch_keyboard(self, target):
        address, _, video = target.partition(",")
        if self.get_state("USB") == "0" or self._route_locked():
            return
        self.set_state("connected_keyboard_encoder", address)
        if video != "0":
            stream = self._keyboard_streams.get(address)
            if stream is not None:
                self.set_state("STREAM", str(stream))

    def _packet(self, fields):
        return "".join(
            f"{key}:{self.state.get(key, default)}\r" for key, default in fields.items()
        ).encode()

    def _changes(self, wire):
        if self._n24:
            override = self._n24_changes(wire)
            if override is not None:
                return override
        return self._common_changes(wire)

    def _n24_changes(self, wire):
        if wire.startswith("KVMMasterIP:"):
            self._switch_keyboard(wire.partition(":")[2])
            return {}
        if wire.startswith("setSettings:"):
            return {}
        return {"hdmiOn": {"DVIOFF": "off"}, "hdmiOff": {"DVIOFF": "on"}}.get(wire)

    def _common_changes(self, wire):
        if wire in FIXED:
            return FIXED[wire]
        cmd, _, value = wire.partition(":")
        if cmd in POINTS:
            if cmd in ("set", "seta") and self._route_locked():
                return {}
            return {POINTS[cmd]: value}
        if cmd == "setSettings":
            return self._setting(value)
        if cmd == "serSet":
            fields = value.split(",")
            return (
                {}
                if len(fields) != 4 or "refuse_serial" in self.active_errors
                else dict(zip(("BAUD", "SNUMB", "SPAR", "SP2S"), fields))
            )
        updates = {
            "local": {"PLAYMODE": "local", "PLAYLIST": value},
            "modeset": {"MODE": value + ".mode"},
            "KVMUSB20MainIP": {"KVMMainIP": value, "USB20MainIP": value},
            "lovol": {"LINEOUTVOL_L": value, "LINEOUTVOL_R": value},
        }
        return updates.get(cmd, {})

    def _route_locked(self):
        return (
            "locked_routes" in self.active_errors
            or self.get_state("AVHENABLE") == "1"
            or (not self._n24 and self.config.get("routing_owner") == "Dante AV")
        )

    def _setting(self, wire):
        name, _, value = wire.partition(":")
        if self._n33:
            return self._n33_setting(name, value)
        if name == "streamURL":
            return {"STREAMURL": value}
        if name == "VideoWallHorVer":
            return dict(
                zip(
                    ("wallVerMons", "wallHorMons", "wallMonPosV", "wallMonPosH"),
                    value.split(","),
                )
            )
        if name in ENABLE and value in ("on", "off"):
            return {ENABLE[name]: "1" if value == "on" else "0"}
        if name == "ReceiveSrc" and value in ("MWC", "H26X"):
            return {"RCVMODE": "1" if value == "MWC" else "2"}
        return {}

    def _n33_setting(self, name, value):
        if name in {"ReceiveSrc", "streamURL", "audioStream"} and self._route_locked():
            return {}
        modes = {"SVSI": "1", "H26X": "2"}
        if name == "ReceiveSrc" and value in modes:
            return {"RCVMODE": modes[value]}
        field = {
            "streamURL": "STREAMURL",
            "audioStream": "AUDIOSTREAM",
            "hdmiAudio": "HDMIAUDIO",
            "holdlastframe": "FRAMEHOLD",
            "disableHDMILostStream": "VIDOFFNOSTRM",
        }.get(name)
        return {field: value} if field else {}

    def _hcontrol(self, wire):
        verb, body = wire.split(" ", 1)
        path = json.loads(body).get("path", "")
        if not path.startswith(HROOT):
            return None
        prop = path[len(HROOT) :]
        if verb == "subscribeevent" and prop in NOTICES.values():
            self._subscriptions.setdefault(CLIENT.get(), set()).add(prop)
            return hc_line("@subscribeevent", prop)
        if verb == "get" and prop in (
            "hdmiOutStatus",
            "hdmiOutResolution",
            "hdcpStatus",
        ):
            return hc_line("@get", prop, value=self._hdmi_value(prop))
        return None

    def _hdmi_value(self, prop):
        if prop == "hdmiOutStatus":
            return int(self.get_state("HDMISTATUS") == "connected")
        return self.get_state(prop)

    def set_state(self, key, value):
        old = self.get_state(key)
        super().set_state(key, value)
        prop = "hdmiOutStatus" if key == "HDMISTATUS" else key
        if not self._running or value == old:
            return
        if prop not in NOTICES:
            return
        event = NOTICES[prop]
        data = hc_line("event", event, arguments={"data": self._hdmi_value(prop)})
        for client, events in self._subscriptions.items():
            if event in events and client in self._clients:
                task = asyncio.create_task(self.push_to(client, data))
                self._notices.add(task)
                task.add_done_callback(self._notices.discard)
