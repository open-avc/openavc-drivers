"""AMX N2625 wallplate decoder training simulator. Direct Control and HControl
share one allocated loopback port. A simulated display is a separate serial
peer. Cable, incoming signal, and output timing are independently controllable.
"""

from __future__ import annotations
import asyncio
from contextvars import ContextVar
from copy import deepcopy
import hmac
import json
from openavc.simulator.self_signed_tls import build_optional_tls, remove_cert_files
from openavc.simulator.tcp_simulator import TCPSimulator

CLIENT = ContextVar("n2625wp_client", default="")

AUTH_PHASE = ContextVar("n2625wp_password_prefix", default=None)

HROOT = "/decoder/systemStatus/"

NOTICES = {
    "hdmiOutStatus": "hdmiOutCableStatusChanged",
    "hdmiOutResolution": "hdmiOutResolutionChanged",
}
STATUS = (
    "SVSI_RXGEN2",
    "NAME",
    "MAC",
    "IP",
    "SWVER",
    "ID",
    "STREAM",
    "STREAMAUDIO",
    "MUTE",
    "LINEOUTVOL_L",
    "LINEOUTVOL_R",
    "SCALERBYPASS",
    "MODE",
    "HDMIOFF",
    "HDMISTATUS",
    "INPUTRES",
    "PLAYMODE",
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
    "BAUD",
    "SNUMB",
    "SPAR",
    "SP2S",
    "LIVEAUDIOLP",
    "HDMIAUDIO",
    "FRAMEHOLD",
    "VIDOFFNOSTRM",
    "wallStretch",
)
NETWORK = {
    "SVSI_NETSTATS": "",
    "NAME": "",
    "MAC": "",
    "IP": "",
    "SWVER": "",
    "CHASSISID": "02:00:00:00:00:01",
    "SYSNAME": "AV access switch",
    "PORTID": "Gi1/0/25",
    "PORTDESCR": "Display wallplate",
}
FIXED = {
    "unmute": {"MUTE": "0"},
    "mute": {"MUTE": "1"},
    "vunmute": {"video_muted": False},
    "vmute": {"video_muted": True},
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
HREAD = {
    HROOT + "hdmiOutStatus": "HDMISTATUS",
    HROOT + "hdmiOutResolution": "hdmiOutResolution",
    HROOT + "hdcpStatus": "hdcpStatus",
    "/decoder/hdmiOut/videoMute": "video_muted",
    "/decoder/streamSetup/mwcStream/previewImage": "image_enabled",
}


def hc_line(verb, path, **values):
    return (verb + " " + json.dumps({"path": path, **values}) + "\n").encode()


class AMXN2625WPSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "amx_svsi_n2625_wp",
        "name": "AMX SVSI N2625 Wallplate Decoder Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 50002,
        "delimiter": "\r",
        "initial_state": {
            "SVSI_RXGEN2": "N2625WP15601300001",
            "NAME": "Training room wallplate",
            "MAC": "02:00:00:00:26:25",
            "IP": "192.0.2.25",
            "SWVER": "1.0.0",
            "ID": "1",
            "STREAM": "12",
            "STREAMAUDIO": "0",
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
            "LIVEAUDIOLP": "off",
            "HDMIAUDIO": "auto",
            "FRAMEHOLD": "off",
            "VIDOFFNOSTRM": "off",
            "wallStretch": "0",
            "hdmiOutResolution": "1920x1080p60",
            "hdcpStatus": "None",
            "video_muted": False,
            "image_enabled": False,
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
            {
                "type": "select",
                "key": "PLAYMODE",
                "label": "Playback",
                "options": ["live", "local"],
            },
            {
                "type": "select",
                "key": "LIVEAUDIOLP",
                "label": "Stream audio in local playback",
                "options": ["on", "off"],
            },
            {
                "type": "select",
                "key": "KVMMainIP",
                "label": "KVM encoder address",
                "options": ["192.0.2.12", "192.0.2.15"],
            },
            {
                "type": "select",
                "key": "USB20MainIP",
                "label": "USB encoder address",
                "options": ["192.0.2.12", "192.0.2.15"],
            },
            {"type": "toggle", "key": "image_enabled", "label": "Web preview enabled"},
            {"type": "toggle", "key": "video_muted", "label": "Video muted"},
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
            "locked_settings": {
                "label": "Refuse settings or playback changes",
                "description": "Keep reporting the previous settings.",
            },
            "missing_routes": {
                "label": "Omit routing fields",
                "description": "Return a short status packet without video, audio or host "
                "addresses.",
            },
        },
    }

    def __init__(self, device_id, config=None):
        config = config or {}
        self.SIMULATOR_INFO = deepcopy(type(self).SIMULATOR_INFO)
        self.SIMULATOR_INFO["initial_state"]["SVSI_RXGEN2"] = (
            config.get("simulation_variant", "NMX-DEC-N2625D-WP-NA") + "15601300202"
        )
        self.SIMULATOR_INFO["tls"] = bool(config.get("ssl"))
        super().__init__(device_id, config)
        self._certs = self._direct = None
        self._subscriptions = {}
        self._notices = set()

    def _packet(self, fields):
        return (
            "\r".join(
                f"{key}:{self.state.get(key, default)}"
                for key, default in fields.items()
            )
            + "\r"
        ).encode()

    def _status_packet(self):
        fields = dict.fromkeys(STATUS, "")
        if "missing_routes" in self.active_errors:
            for key in ("STREAM", "STREAMAUDIO", "KVMMainIP", "USB20MainIP"):
                fields.pop(key)
        return self._packet(fields)

    def _changes(self, wire):
        if "locked_settings" in self.active_errors:
            return {}
        if wire in FIXED:
            return FIXED[wire]
        cmd, _, value = wire.partition(":")
        if cmd in ("set", "seta") and self._route_locked():
            return {}
        if cmd in POINTS:
            return {POINTS[cmd]: value}
        if cmd == "setSettings":
            return self._setting(value)
        return self._port_or_playback(cmd, value)

    def _port_or_playback(self, cmd, value):
        if cmd == "serSet":
            return self._serial(value)
        if cmd == "KVMUSB20MainIP" and "locked_routes" in self.active_errors:
            return {}
        return {
            "local": {"PLAYMODE": "local", "PLAYLIST": value},
            "modeset": {"MODE": value + ".mode"},
            "KVMUSB20MainIP": {"KVMMainIP": value, "USB20MainIP": value},
            "lovol": {"LINEOUTVOL_L": value, "LINEOUTVOL_R": value},
        }.get(cmd, {})

    def _serial(self, value):
        fields = value.split(",")
        if len(fields) != 4 or "refuse_serial" in self.active_errors:
            return {}
        return dict(zip(("BAUD", "SNUMB", "SPAR", "SP2S"), fields))

    def _route_locked(self):
        return (
            self.config.get("routing_owner") == "Dante AV-A"
            or "locked_routes" in self.active_errors
        )

    def _setting(self, wire):
        name, _, value = wire.partition(":")
        if name == "VideoWallHorVer":
            return dict(
                zip(
                    ("wallVerMons", "wallHorMons", "wallMonPosV", "wallMonPosH"),
                    value.split(","),
                )
            )
        if name in ENABLE:
            return {ENABLE[name]: "1" if value == "on" else "0"}
        if name == "Preview":
            return {"image_enabled": value == "on"}
        fields = {"hdmiAudio": "HDMIAUDIO", "disableHDMILostStream": "VIDOFFNOSTRM"}
        return {fields[name]: value} if name in fields else {}

    def _hcontrol(self, wire):
        verb, body = wire.split(" ", 1)
        path = json.loads(body).get("path", "")
        if verb == "subscribeevent" and path.removeprefix(HROOT) in NOTICES.values():
            self._subscriptions.setdefault(CLIENT.get(), set()).add(path)
            return hc_line("@subscribeevent", path)
        if verb == "get" and path in HREAD:
            key = HREAD[path]
            value = self.get_state(key)
            if key == "HDMISTATUS":
                value = int(value == "connected")
            return hc_line("@get", path, value=value)
        return None

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
            return self._hcontrol(wire)
        client = CLIENT.get()
        if self._direct in self._clients and client != self._direct:
            self._clients[client].close()
            return None
        self._direct = client
        if wire == "getNetStatus":
            return self._packet({**NETWORK, "SVSI_NETSTATS": self.state["SVSI_RXGEN2"]})
        for key, value in self._changes(wire).items():
            self.set_state(key, value)
        return self._status_packet()

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
        event = HROOT + NOTICES[prop]
        data = hc_line("event", event, arguments={"data": self._hdmi_value(prop)})
        for client, events in self._subscriptions.items():
            if event in events and client in self._clients:
                task = asyncio.create_task(self.push_to(client, data))
                self._notices.add(task)
                task.add_done_callback(self._notices.discard)
