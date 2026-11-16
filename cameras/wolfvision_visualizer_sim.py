"""WolfVision Visualizer simulator. Each TCP client has its own login and receive buffer."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from openavc.simulator.tcp_simulator import TCPSimulator

MODELS = ("VZ-8.UHD", "VZ-3neo.UHD", "VZ-8neo.UHD")
SOURCES = ("Live camera", "Internal memory", "USB memory", "HDMI 1", "HDMI 2")
RECORDING = ("Idle", "Recording", "Paused", "Syncing")
STANDBY = ("Standby", "Deep with Wake-on-LAN", "Deep", "HDMI pass-through")
BOOLEANS = {
    0x4100: "power",
    0x4104: "lamp",
    0x4105: "frozen",
    0x4107: "hdmi_1_present",
    0x4108: "hdmi_2_present",
    0x420C: "autofocus",
    0x4113: "streaming",
    0x4408: "stream_enabled",
    0x441A: "security_mode",
    0x4433: "privacy_mode",
}
TEXT = {0x4001: "device_name", 0x4003: "firmware", 0x430C: "mac_address"}
LENS = {0x420F: "zoom", 0x4210: "focus"}
ENUMS = {
    0x4410: ("standby_mode", STANDBY),
    0x4106: ("source", SOURCES),
    0x4115: ("recording_status", RECORDING),
}
PRESET_KEYS = ("source", "lamp", "zoom", "focus", "brightness", "autofocus")


@dataclass
class Session:
    level: int = 0
    buffer: bytearray = field(default_factory=bytearray)


CLIENT = ContextVar("wolfvision_visualizer_session", default=None)


class Refused(Exception):
    def __init__(self, code):
        self.code = code


class WolfVisionVisualizerSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "wolfvision_visualizer",
        "name": "WolfVision UHD Visualizer",
        "transport": "tcp",
        "default_port": 50915,
        "initial_state": {
            "model": "VZ-8.UHD",
            "device_name": "Lectern Visualizer",
            "serial_number": "123456",
            "firmware": "V1.86d",
            "mac_address": "00:0C:92:12:34:56",
            "standby_mode": "Standby",
            "power": True,
            "lamp": True,
            "autofocus": True,
            "frozen": False,
            "source": "Live camera",
            "zoom": 0,
            "focus": 50,
            "brightness": 0,
            "hdmi_1_present": True,
            "hdmi_2_present": False,
            "stream_enabled": True,
            "streaming": False,
            "security_mode": False,
            "privacy_mode": False,
            "recording_status": "Idle",
        },
        "controls": [
            {
                "type": "select",
                "key": "model",
                "label": "Camera model (reconnect to detect)",
                "options": ["VZ-8.UHD", "VZ-3neo.UHD", "VZ-8neo.UHD"],
            },
            {"type": "toggle", "key": "frozen", "label": "Freeze at the camera"},
            {"type": "toggle", "key": "lamp", "label": "Lamp"},
            {"type": "toggle", "key": "security_mode", "label": "Security mode"},
            {"type": "toggle", "key": "privacy_mode", "label": "Privacy mode"},
            {"type": "toggle", "key": "stream_enabled", "label": "Streaming enabled"},
            {
                "type": "select",
                "key": "recording_status",
                "label": "Recorder",
                "options": ["Idle", "Recording", "Paused", "Syncing"],
            },
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._line_mode = False
        self._delimiter = None
        self._presets = {}
        self._lens_raw = {}
        self.set_state("model", self.config.get("model", self.get_state("model")))

    async def on_client_connected(self, client_id):
        CLIENT.set(Session(level=0 if self.config.get("password") else 1))

    def _model_code(self):
        try:
            return MODELS.index(self.get_state("model"))
        except ValueError:
            return 4  # An out-of-family Visualizer for discovery checks.

    def _features(self):
        mask = bytearray.fromhex("DF 04 E0 00 08 00 30 24")
        if self._model_code() == 0:
            mask[7] |= 0x40
        return bytes(mask)

    def _read_lens(self, key):
        raw = self._lens_raw.get(key)
        percent = self.get_state(key)
        if raw is None or round(raw * 100 / 4095) != percent:
            raw = round(percent * 4095 / 100)
        return raw.to_bytes(2, "little")

    def _read_value(self, code, session):
        fixed = {
            0x400B: lambda: bytes([session.level]),
            0x4016: lambda: bytes([self._model_code()]),
            0x4021: self._features,
            0x4002: lambda: int(self.get_state("serial_number")).to_bytes(4, "little"),
            0x4200: lambda: self.get_state("brightness").to_bytes(
                4, "little", signed=True
            ),
        }
        if code in fixed:
            return fixed[code]()
        return self._read_setting(code)

    def _read_setting(self, code):
        if code in TEXT:
            return self.get_state(TEXT[code]).encode()
        if code in LENS:
            return self._read_lens(LENS[code])
        if code in ENUMS:
            key, choices = ENUMS[code]
            return bytes([choices.index(self.get_state(key))])
        return self._read_boolean(code)

    def _read_boolean(self, code):
        if code not in BOOLEANS:
            raise Refused(2)
        if code == 0x4108 and self._model_code() != 0:
            raise Refused(2)
        return bytes([bool(self.get_state(BOOLEANS[code]))])

    def _login(self, data, session):
        if (
            data[:1] != b"\x01"
            or data[1:] != str(self.config.get("password", "")).encode()
        ):
            raise Refused(7)
        session.level = 1

    def _set_lens(self, key, data):
        raw = int.from_bytes(data, "little")
        if len(data) != 2 or raw > 4095:
            raise Refused(3)
        self._lens_raw[key] = raw
        self.set_state(key, round(100 * raw / 4095))

    def _set_brightness(self, data):
        value = int.from_bytes(data, "little", signed=True)
        if len(data) != 4 or not -10 <= value <= 10:
            raise Refused(3)
        self.set_state("brightness", value)

    def _set_focus_once(self, data):
        self._index(data, 10)
        self.set_state("autofocus", False)

    def _set_source(self, data):
        limit = 5 if self._model_code() == 0 else 4
        self.set_state("source", SOURCES[self._index(data, limit)])

    def _set_value(self, code, data):
        if code in (0x4100, 0x4104, 0x4105, 0x420C):
            self.set_state(BOOLEANS[code], bool(self._index(data, 2)))
            return
        if code in LENS:
            return self._set_lens(LENS[code], data)
        setters = {
            0x4106: self._set_source,
            0x420D: self._set_focus_once,
            0x4200: self._set_brightness,
        }
        if code not in setters:
            raise Refused(2)
        setters[code](data)

    @staticmethod
    def _index(data, limit):
        if len(data) != 1 or data[0] >= limit:
            raise Refused(3)
        return data[0]

    def _preset(self, code, data):
        slot = self._index(data, 3)
        if code == 0x4112:
            self._presets[slot] = {k: self.get_state(k) for k in PRESET_KEYS}
        else:
            for key, value in self._presets.get(slot, {"zoom": slot * 25}).items():
                self.set_state(key, value)

    def _check_access(self, code, session):
        if not session.level:
            raise Refused(8)
        self._check_mode(code)

    def _check_mode(self, code):
        if not self.get_state("power") and code != 0x4100:
            raise Refused(11)
        if self.get_state("security_mode") and code in (0x4105, 0x4109, 0x410A, 0x410B):
            raise Refused(7)

    def _write_value(self, code, data, session):
        if code == 0x400B:
            return self._login(data, session)
        self._check_access(code, session)
        if code in (0x4111, 0x4112):
            return self._preset(code, data)
        if code in (0x4109, 0x410A, 0x410B):
            if data:
                raise Refused(4)
            self.set_state(
                "recording_status",
                {0x4109: "Recording", 0x410A: "Paused", 0x410B: "Idle"}[code],
            )
            return
        self._set_value(code, data)

    def _answer(self, frame, width, session):
        code = int.from_bytes(frame[1:3], "big")
        data = frame[3 + width :]
        try:
            if frame[0] & 1:
                self._write_value(code, data, session)
                result = b""
            else:
                if data:
                    raise Refused(4)
                result = self._read_value(code, session)
            return (
                bytes([8 | (frame[0] & 1)]) + frame[1:3] + bytes([len(result)]) + result
            )
        except Refused as exc:
            return bytes([0x88 | (frame[0] & 1)]) + frame[1:3] + bytes([exc.code])

    def handle_command(self, data):
        session = CLIENT.get()
        if session is None:
            session = Session(level=0 if self.config.get("password") else 1)
            CLIENT.set(session)
        buffer = session.buffer
        buffer.extend(data)
        replies = bytearray()
        while len(buffer) >= 4:
            if buffer[0] not in (8, 9, 12, 13):
                del buffer[0]
                continue
            width = 2 if buffer[0] & 4 else 1
            if len(buffer) < 3 + width:
                break
            size = 3 + width + int.from_bytes(buffer[3 : 3 + width], "big")
            if len(buffer) < size:
                break
            replies.extend(self._answer(bytes(buffer[:size]), width, session))
            del buffer[:size]
        return bytes(replies) or None
