"""WolfVision UHD Visualizers over WolfProt 1.86d.

Python handles binary framing, per-connection login, and the camera's feature
bitmap. Classroom feedback is read after writes and on a short polling cycle.
The Visualizer command table does not define Cynap's control subscriptions.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlunsplit

from openavc.drivers.base import (
    BaseDriver,
    CommandRefusedError,
    ConnectionFaultError,
    WakeAddressError,
)
from openavc.transport.frame_parsers import CallableFrameParser

MODEL = 0x4016
FEATURES = 0x4021
LOGIN = 0x400B
MODELS = ("VZ-8.UHD", "VZ-3neo.UHD", "VZ-8neo.UHD")
SOURCES = ("Live camera", "Internal memory", "USB memory", "HDMI 1", "HDMI 2")
STANDBY = ("Standby", "Deep with Wake-on-LAN", "Deep", "HDMI pass-through")
RECORDING = ("Idle", "Recording", "Paused", "Syncing")

# Command, encoding and optional feature bit (byte index, mask). Feature bits
# are printed most-significant first in the Visualizer GET Features table.
READS = {
    "device_name": (0x4001, "text", None),
    "serial_number": (0x4002, "serial", None),
    "firmware": (0x4003, "text", None),
    "mac_address": (0x430C, "text", None),
    "power": (0x4100, "bool", (0, 0x04)),
    "lamp": (0x4104, "bool", (0, 0x02)),
    "frozen": (0x4105, "bool", (1, 0x04)),
    "source": (0x4106, SOURCES, None),
    "hdmi_1_present": (0x4107, "bool", (0, 0x01)),
    "hdmi_2_present": (0x4108, "bool", (7, 0x40)),
    "autofocus": (0x420C, "bool", (0, 0x08)),
    "zoom": (0x420F, "lens", (0, 0x80)),
    "focus": (0x4210, "lens", (0, 0x10)),
    "brightness": (0x4200, "brightness", (0, 0x40)),
    "recording_status": (0x4115, RECORDING, (7, 0x04)),
    "streaming": (0x4113, "bool", (7, 0x20)),
    "stream_enabled": (0x4408, "bool", (7, 0x20)),
    "privacy_mode": (0x4433, "bool", None),
    "security_mode": (0x441A, "bool", None),
    "standby_mode": (0x4410, STANDBY, None),
}
IDENTITY = ("device_name", "serial_number", "firmware", "mac_address")
ERRORS = {
    1: "timed out",
    2: "unsupported command",
    3: "invalid parameter",
    4: "invalid message length",
    5: "queue full",
    7: "access denied",
    8: "moderator login required",
    9: "camera busy",
    11: "camera in standby",
}


class WolfProtError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS.get(code, f"camera error {code}"))


def message(code, payload=b"", *, write=False):
    size = len(payload)
    width = 2 if size > 255 else 1
    header = 0x08 | int(write) | (4 if width == 2 else 0)
    return (
        bytes([header])
        + code.to_bytes(2, "big")
        + size.to_bytes(width, "big")
        + payload
    )


def _length_field(flags, extended_flags):
    if extended_flags & 1:
        return 4
    return 2 if flags & 4 else 1


def _command_end(data):
    if len(data) < 2:
        return None
    flags = data[0]
    if flags & 0x78 != 0x08:
        raise ValueError("Unrecognized WolfProt header")
    if not flags & 2:
        return 3
    if data[1] & 0xFE:
        raise ValueError("Unrecognized extended header")
    return 4


def frame_parts(data):
    """Return the payload offset and frame length when enough header has arrived."""
    start = _command_end(data)
    if start is None:
        return None
    flags = data[0]
    if flags & 0x80:
        return start, start + 1
    width = _length_field(flags, data[1] if flags & 2 else 0)
    offset = start + width
    if len(data) < offset:
        return None
    length = int.from_bytes(data[start:offset], "big")
    if length > 65535:  # No picture data is requested.
        raise ValueError("Unexpected camera payload size")
    return offset, offset + length


def split_frame(buffer):
    try:
        parts = frame_parts(buffer)
    except ValueError:
        return None, buffer[1:]
    if parts is None or len(buffer) < parts[1]:
        return None, buffer
    size = parts[1]
    return buffer[:size], buffer[size:]


def _lens_percent(data):
    value = int.from_bytes(data, "little")
    return round(100 * value / 4095) if len(data) == 2 and value <= 4095 else None


def _brightness(data):
    value = int.from_bytes(data, "little", signed=True)
    return value if len(data) == 4 and -10 <= value <= 10 else None


DECODERS = {
    "text": lambda data: data.rstrip(b"\0").decode("utf-8", errors="replace"),
    "lens": _lens_percent,
    "bool": {b"\x00": False, b"\x01": True}.get,
    "serial": lambda data: (
        str(int.from_bytes(data, "little")) if len(data) == 4 else None
    ),
    "brightness": _brightness,
}


def unpack_value(kind, data):
    if isinstance(kind, tuple):
        return kind[data[0]] if len(data) == 1 and data[0] < len(kind) else None
    return DECODERS[kind](data)


def bounded(value, minimum, maximum):
    if isinstance(value, bool) or not str(value).strip().lstrip("-").isdigit():
        raise CommandRefusedError("Enter a whole number.")
    number = int(value)
    if not minimum <= number <= maximum:
        raise CommandRefusedError(f"Choose a value from {minimum} to {maximum}.")
    return number


class WolfVisionVisualizer(BaseDriver):
    DRIVER_INFO = {
        "id": "wolfvision_visualizer",
        "name": "WolfVision UHD Visualizer",
        "manufacturer": "WolfVision",
        "category": "camera",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": "Control WolfVision UHD document cameras over Ethernet. Detect the model, select "
        "supported sources and presets, and read back image, recording and privacy status.",
        "tags": ["document-camera", "visualizer", "education", "lectern", "wolfprot"],
        "source_url": "https://wolfvision.com/api_wolfprot/Visualizer_commands_1.86d.html",
        "verified": False,
        "simulated": True,
        "transport": "tcp",
        "ports": [50915, 50917],
        "min_platform_version": "0.37.1",
        "compatible_models": [
            {
                "manufacturer": "WolfVision",
                "models": ["VZ-8.UHD", "VZ-3neo.UHD", "VZ-8neo.UHD"],
                "confidence": "untested",
                "notes": "WolfProt Visualizer 1.86d. Sources and preset slots are read from the "
                "connected camera.",
            }
        ],
        "discovery": {
            "tcp_probe": {
                "port": 50915,
                "send_hex": "08401600",
                "expect_regex": "^\\x08\\x40\\x16\\x01[\\x00-\\x02]$",
                "timeout_ms": 2000,
            },
            "oui": ["00:0c:92"],
            "manufacturer_alias": ["wolfvision", "wolfvision gmbh"],
        },
        "help": {
            "overview": "Control the lectern document camera, frame a page, recall presets and check "
            "whether the image is live or frozen. Camera controls and the optional RTSP "
            "preview work directly over the room network.",
            "setup": "1. Power the camera on and connect its LAN port. Choose it in Network Discovery or "
            "enter its IP address or hostname. OpenAVC reads the model, source choices and "
            "preset slots.\n"
            "2. If Moderator password required is enabled under Advanced Settings > Security, "
            "enter the moderator password. Automatic uses TLS on port "
            "50917.\n"
            "3. For remote power, choose Standby or Deep + WoL under Advanced Settings. Deep + "
            "WoL needs a local Wake-on-LAN path.\n"
            "4. To use Video Panel preview, enable streaming on the camera. Privacy mode and "
            "security mode disable preview. Freeze and recording are unavailable in security "
            "mode.",
        },
        "default_config": {
            "host": "",
            "connection_security": "Automatic",
            "port": 50915,
            "verify_ssl": False,
            "password": "",
            "mac_address": "",
            "poll_interval": 3,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "label": "Camera IP address or hostname",
                "required": True,
            },
            "connection_security": {
                "type": "enum",
                "label": "Control connection",
                "values": ["Automatic", "TLS", "Plain TCP"],
                "default": "Automatic",
                "description": "Automatic uses TLS when a moderator password is entered. Choose TLS to encrypt every connection.",
            },
            "port": {
                "type": "integer",
                "label": "Control port",
                "default": 50915,
                "min": 1,
                "max": 65535,
                "description": "Leave at 50915 for automatic selection of the standard TCP "
                "or TLS port.",
            },
            "verify_ssl": {
                "type": "boolean",
                "label": "Verify TLS certificate",
                "default": False,
                "description": "Enable if the camera certificate is trusted by the "
                "OpenAVC server.",
            },
            "password": {
                "type": "password",
                "label": "Moderator password",
                "default": "",
                "description": "Leave blank when moderator authentication is disabled "
                "on the camera.",
            },
            "mac_address": {
                "type": "string",
                "label": "Wake-on-LAN MAC address",
                "default": "",
                "learned_from": "mac_address",
                "description": "Learned when the camera first connects. Enter the "
                "wired MAC address to commission a sleeping camera.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Feedback interval (seconds)",
                "default": 3,
                "min": 1,
                "max": 120,
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Detected model"},
            "device_name": {"type": "string", "label": "Camera name"},
            "serial_number": {"type": "string", "label": "Serial number"},
            "firmware": {"type": "string", "label": "Firmware version"},
            "mac_address": {"type": "string", "label": "Wired MAC address"},
            "source_options": {"type": "string", "label": "Available sources"},
            "preset_options": {"type": "string", "label": "Recallable presets"},
            "store_options": {"type": "string", "label": "Writable presets"},
            "preview_url": {"type": "string", "label": "Preview URL"},
            "preview_format": {"type": "string", "label": "Preview format"},
            "power": {
                "type": "boolean",
                "label": "Camera on",
                "control": True,
                "cloud_priority": "high",
            },
            "frozen": {
                "type": "boolean",
                "label": "Image frozen",
                "control": True,
                "cloud_priority": "high",
            },
            "lamp": {
                "type": "boolean",
                "label": "Lamp on",
                "control": True,
                "cloud_priority": "low",
            },
            "autofocus": {
                "type": "boolean",
                "label": "Continuous autofocus",
                "control": True,
                "cloud_priority": "low",
            },
            "hdmi_1_present": {
                "type": "boolean",
                "label": "HDMI 1 connected",
                "control": False,
                "cloud_priority": "low",
            },
            "hdmi_2_present": {
                "type": "boolean",
                "label": "HDMI 2 connected",
                "control": False,
                "cloud_priority": "low",
            },
            "streaming": {
                "type": "boolean",
                "label": "Stream active",
                "control": False,
                "cloud_priority": "low",
            },
            "stream_enabled": {
                "type": "boolean",
                "label": "Streaming enabled",
                "control": False,
                "cloud_priority": "low",
            },
            "privacy_mode": {
                "type": "boolean",
                "label": "Privacy mode",
                "control": False,
                "cloud_priority": "low",
            },
            "security_mode": {
                "type": "boolean",
                "label": "Security mode",
                "control": False,
                "cloud_priority": "low",
            },
            "zoom": {
                "type": "integer",
                "label": "Zoom (%)",
                "min": 0,
                "max": 100,
                "control": True,
            },
            "focus": {
                "type": "integer",
                "label": "Focus position (%)",
                "min": 0,
                "max": 100,
                "control": True,
            },
            "brightness": {
                "type": "integer",
                "label": "Brightness",
                "min": -10,
                "max": 10,
                "control": True,
            },
            "source": {
                "type": "enum",
                "label": "Selected source",
                "values": [
                    "Live camera",
                    "Internal memory",
                    "USB memory",
                    "HDMI 1",
                    "HDMI 2",
                ],
            },
            "recording_status": {
                "type": "enum",
                "label": "Recording status",
                "values": ["Idle", "Recording", "Paused", "Syncing"],
            },
            "standby_mode": {
                "type": "enum",
                "label": "Standby mode",
                "values": [
                    "Standby",
                    "Deep with Wake-on-LAN",
                    "Deep",
                    "HDMI pass-through",
                ],
            },
            "last_error": {"type": "string", "label": "Last camera error"},
        },
        "commands": {
            "read_power": {
                "label": "Read camera power",
                "help": "Read camera power from the camera.",
                "params": {},
                "query_for": "power",
            },
            "read_frozen": {
                "label": "Read image freeze",
                "help": "Read image freeze from the camera.",
                "params": {},
                "query_for": "frozen",
            },
            "read_lamp": {
                "label": "Read lamp",
                "help": "Read lamp from the camera.",
                "params": {},
                "query_for": "lamp",
            },
            "read_zoom": {
                "label": "Read zoom position",
                "help": "Read zoom position from the camera.",
                "params": {},
                "query_for": "zoom",
            },
            "read_focus": {
                "label": "Read focus position",
                "help": "Read focus position from the camera.",
                "params": {},
                "query_for": "focus",
            },
            "read_autofocus": {
                "label": "Read continuous autofocus",
                "help": "Read continuous autofocus from the camera.",
                "params": {},
                "query_for": "autofocus",
            },
            "read_brightness": {
                "label": "Read brightness",
                "help": "Read brightness from the camera.",
                "params": {},
                "query_for": "brightness",
            },
            "read_source": {
                "label": "Read selected source",
                "help": "Read selected source from the camera.",
                "params": {},
                "query_for": "source",
            },
            "read_recording_status": {
                "label": "Read recording status",
                "help": "Read recording status from the camera.",
                "params": {},
                "query_for": "recording_status",
            },
            "read_hdmi_1_present": {
                "label": "Read HDMI 1 connection",
                "help": "Read HDMI 1 connection from the camera.",
                "params": {},
                "query_for": "hdmi_1_present",
            },
            "read_hdmi_2_present": {
                "label": "Read HDMI 2 connection",
                "help": "Read HDMI 2 connection from the camera.",
                "params": {},
                "query_for": "hdmi_2_present",
            },
            "read_security_mode": {
                "label": "Read security mode",
                "help": "Read security mode from the camera.",
                "params": {},
                "query_for": "security_mode",
            },
            "read_privacy_mode": {
                "label": "Read privacy mode",
                "help": "Read privacy mode from the camera.",
                "params": {},
                "query_for": "privacy_mode",
            },
            "read_streaming": {
                "label": "Read stream activity",
                "help": "Read stream activity from the camera.",
                "params": {},
                "query_for": "streaming",
            },
            "read_stream_enabled": {
                "label": "Read stream service",
                "help": "Read stream service from the camera.",
                "params": {},
                "query_for": "stream_enabled",
            },
            "read_standby_mode": {
                "label": "Read standby mode",
                "help": "Read standby mode from the camera.",
                "params": {},
                "query_for": "standby_mode",
            },
            "power_on": {
                "label": "Power on",
                "help": "Turn the camera on. An offline camera in Deep with Wake-on-LAN can "
                "wake on the same local network.",
                "params": {},
                "available_offline": True,
                "sets": {"power": True},
            },
            "standby": {
                "label": "Standby",
                "help": "Put the camera into its configured standby mode. Deep standby "
                "without Wake-on-LAN requires someone to wake the camera locally.",
                "params": {},
                "sets": {"power": False},
            },
            "freeze": {
                "label": "Freeze image",
                "help": "Hold the image while the presenter changes the page on the working "
                "surface.",
                "params": {},
                "sets": {"frozen": True},
            },
            "go_live": {
                "label": "Return to live image",
                "help": "Release the frozen image so the output follows the selected source "
                "again.",
                "params": {},
                "sets": {"frozen": False},
            },
            "set_lamp": {
                "label": "Set lamp",
                "help": "Turn the light over the working surface on or off.",
                "params": {
                    "enabled": {"required": True, "type": "boolean", "label": "Enabled"}
                },
                "sets": {"lamp": "{enabled}"},
            },
            "set_autofocus": {
                "label": "Set continuous autofocus",
                "help": "Enable or disable continuous focusing as the presenter moves "
                "an object.",
                "params": {
                    "enabled": {"required": True, "type": "boolean", "label": "Enabled"}
                },
                "sets": {"autofocus": "{enabled}"},
            },
            "set_frozen": {
                "label": "Set image freeze",
                "help": "Freeze or release the displayed image.",
                "params": {
                    "enabled": {"required": True, "type": "boolean", "label": "Enabled"}
                },
                "sets": {"frozen": "{enabled}"},
            },
            "focus_once": {
                "label": "Focus once",
                "help": "Focus once on the full image. The camera then turns continuous "
                "autofocus off.",
                "params": {},
                "sets": {"autofocus": False},
            },
            "zoom_in": {
                "label": "Zoom in",
                "help": "Move ten percentage points from the current camera zoom toward "
                "telephoto.",
                "params": {},
            },
            "zoom_out": {
                "label": "Zoom out",
                "help": "Move ten percentage points from the current camera zoom toward wide "
                "angle.",
                "params": {},
            },
            "set_zoom": {
                "label": "Set zoom",
                "help": "Position the zoom lens from wide (0%) to telephoto (100%).",
                "params": {
                    "value": {
                        "required": True,
                        "type": "integer",
                        "label": "Zoom",
                        "min": 0,
                        "max": 100,
                    }
                },
                "sets": {"zoom": "{value}"},
            },
            "set_focus": {
                "label": "Set focus position",
                "help": "Set the focus position as a percentage of the lens travel.",
                "params": {
                    "value": {
                        "required": True,
                        "type": "integer",
                        "label": "Focus position",
                        "min": 0,
                        "max": 100,
                    }
                },
                "sets": {"focus": "{value}"},
            },
            "set_brightness": {
                "label": "Set brightness",
                "help": "Adjust image brightness from −10 to +10. Zero is the normal "
                "setting.",
                "params": {
                    "value": {
                        "required": True,
                        "type": "integer",
                        "label": "Brightness",
                        "min": -10,
                        "max": 10,
                    }
                },
                "sets": {"brightness": "{value}"},
            },
            "select_source": {
                "label": "Select source",
                "help": "Show the live camera, a connected HDMI input or a supported "
                "memory source.",
                "params": {
                    "source": {
                        "required": True,
                        "type": "string",
                        "options_state": "source_options",
                    }
                },
                "sets": {"source": "{source}"},
            },
            "recall_preset": {
                "label": "Recall preset",
                "help": "Apply the stored camera settings in the selected slot.",
                "params": {
                    "preset": {
                        "required": True,
                        "type": "integer",
                        "min": 1,
                        "max": 8,
                        "options_state": "preset_options",
                    }
                },
            },
            "store_preset": {
                "label": "Store preset",
                "help": "Replace the settings in this camera preset with the current "
                "settings.",
                "params": {
                    "preset": {
                        "required": True,
                        "type": "integer",
                        "min": 1,
                        "max": 3,
                        "options_state": "store_options",
                    }
                },
                "confirm": "Replace the settings in this camera preset?",
            },
            "start_recording": {
                "label": "Start recording",
                "help": "Start recording the presentation on the Visualizer.",
                "params": {},
                "sets": {"recording_status": "Recording"},
            },
            "pause_recording": {
                "label": "Pause recording",
                "help": "Pause the recording on the Visualizer.",
                "params": {},
                "sets": {"recording_status": "Paused"},
            },
            "stop_recording": {
                "label": "Stop recording",
                "help": "Finish the current recording and let the camera save it.",
                "params": {},
                "sets": {"recording_status": "Idle"},
            },
            "refresh": {
                "label": "Refresh camera status",
                "help": "Read the camera identity, available sources and presets, and current "
                "settings again.",
                "params": {},
            },
        },
        "quick_actions": [
            "freeze",
            "go_live",
            "zoom_in",
            "zoom_out",
            "focus_once",
            "recall_preset",
        ],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        security = self.config.get("connection_security", "Automatic")
        secure = security == "TLS" or (
            security == "Automatic" and bool(self.config.get("password"))
        )
        port = self.config.get("port", 50915)
        if port in (50915, 50917):
            port = 50917 if secure else 50915
        self.config = dict(self.config, ssl=secure, port=port)
        self._io = asyncio.Lock()
        self._pending = None
        self._session = False
        self._features = b""
        self._sources = []
        self._presets = []
        self._unsupported = set()
        self._reported = {}
        self._polls = 0

    def _resolve_delimiter(self):
        return None

    def _create_frame_parser(self):
        return CallableFrameParser(split_frame)

    async def _exchange(self, code, data=b"", *, write=False):
        if not self._session or not self.transport:
            raise ConnectionError("The camera is offline.")
        future = asyncio.get_running_loop().create_future()
        self._pending = (code, write, future)
        try:
            await self.transport.send(message(code, data, write=write))
            return await asyncio.wait_for(future, 4)
        except asyncio.TimeoutError as exc:
            # A late reply has no transaction number. Start a clean session.
            self._session = False
            self._force_disconnect(
                "no_response", "The camera stopped answering WolfProt commands."
            )
            raise ConnectionError("The camera did not answer.") from exc
        finally:
            self._pending = None

    async def on_data_received(self, data):
        start = 1 + int(bool(data[0] & 2))
        code = int.from_bytes(data[start : start + 2], "big")
        if not self._pending or self._pending[:2] != (code, bool(data[0] & 1)):
            return
        future = self._pending[2]
        if future.done():
            return
        if data[0] & 0x80:
            future.set_exception(WolfProtError(data[-1]))
        else:
            offset, _ = frame_parts(data)
            future.set_result(data[offset:])

    async def _on_disconnect_cleanup(self):
        self._session = False
        if self._pending and not self._pending[2].done():
            self._pending[2].set_exception(ConnectionError("The camera disconnected."))
        self.set_state("preview_url", "")
        await super()._on_disconnect_cleanup()

    async def _post_connect(self):
        async with self._io:
            self._session = True
            self._reported.clear()
            self._unsupported.clear()
            self._features = b""
            self.set_states(dict.fromkeys(self.DRIVER_INFO["state_variables"]))
            await self._identify()
            await self._login()
            self._polls = 0

    async def _identify(self):
        try:
            model = await self._exchange(MODEL)
            if len(model) != 1 or model[0] >= len(MODELS):
                raise ValueError
            features = await self._exchange(FEATURES)
            if len(features) != 8:
                raise ValueError
        except (ValueError, WolfProtError) as exc:
            raise ConnectionFaultError(
                "Check the camera address. This driver supports VZ-8.UHD, VZ-3neo.UHD and VZ-8neo.UHD.",
                code="invalid_config",
            ) from exc
        self.set_state("model", MODELS[model[0]])
        self._load_features(features)

    def _load_features(self, features):
        self._features = features
        self._sources = [
            name
            for name, bit in zip(
                SOURCES, (None, (6, 0x20), (6, 0x10), (0, 1), (7, 0x40))
            )
            if self._supports(bit)
        ]
        self._presets = [n for n in range(1, 9) if features[2] & (0x100 >> n)]
        self.set_state("source_options", json.dumps(self._sources))
        self.set_state(
            "preset_options",
            json.dumps(
                [{"value": str(n), "label": f"Preset {n}"} for n in self._presets]
            ),
        )
        self.set_state(
            "store_options",
            json.dumps(
                [
                    {"value": str(n), "label": f"Preset {n}"}
                    for n in self._presets
                    if n <= 3
                ]
            ),
        )

    async def _login(self):
        level = await self._exchange(LOGIN)
        if level in (b"\x01", b"\x02", b"\x03"):
            return
        password = self.config.get("password") or ""
        if not password:
            raise ConnectionFaultError(
                "The camera requires a moderator password.",
                code="auth_failed",
            )
        await self._authenticate(password)
        if await self._exchange(LOGIN) not in (b"\x01", b"\x02", b"\x03"):
            raise ConnectionFaultError(
                "The camera did not grant moderator access.", code="auth_failed"
            )

    async def _authenticate(self, password):
        try:
            await self._exchange(LOGIN, b"\x01" + password.encode("utf-8"), write=True)
        except WolfProtError as exc:
            if exc.code in (7, 8):
                raise ConnectionFaultError(
                    "The camera rejected the moderator password.", code="auth_failed"
                ) from None
            raise ConnectionError(f"Camera login failed ({exc}).") from exc

    def _supports(self, bit):
        return bit is None or bool(self._features and self._features[bit[0]] & bit[1])

    async def _read(self, key):
        code, kind, feature = READS[key]
        if not self._supports(feature) or key in self._unsupported:
            return None
        try:
            data = await self._exchange(code)
        except WolfProtError as exc:
            if exc.code == 2:
                self._unsupported.add(key)
            self._reported.pop(key, None)
            self.set_state(key, None)
            return None
        value = unpack_value(kind, data)
        self._reported[key] = value
        self.set_state(key, value)
        return value

    def _preview(self):
        r = self._reported
        enabled = (
            r.get("stream_enabled") is True
            and r.get("power") is True
            and r.get("privacy_mode") is False
            and r.get("security_mode") is False
        )
        host = str(self.config.get("host", "")).strip("[]")
        if ":" in host:
            host = f"[{host}]"
        self.set_state(
            "preview_url",
            urlunsplit(("rtsp", host, "/stream", "", "")) if host and enabled else "",
        )
        self.set_state("preview_format", "rtsp" if enabled else "")

    async def _sync(self, inventory=False):
        if inventory:
            for key in IDENTITY:
                await self._read(key)
        await self._read("power")
        for key in ("standby_mode", "security_mode", "privacy_mode", "stream_enabled"):
            await self._read(key)
        self._preview()
        if self._reported.get("power") is False:
            return
        for key in (
            "frozen",
            "lamp",
            "source",
            "autofocus",
            "zoom",
            "focus",
            "brightness",
            "hdmi_1_present",
            "hdmi_2_present",
            "recording_status",
            "streaming",
        ):
            await self._read(key)

    async def _initial_sync(self):
        await self.poll()

    async def poll(self):
        async with self._io:
            if self._session:
                await self._sync(inventory=self._polls % 20 == 0)
                self._polls += 1

    async def _write(self, code, payload=b"", *, feedback=()):
        await self._exchange(code, payload, write=True)
        for key in feedback:
            await self._read(key)
        return True

    async def _write_value(self, key, payload):
        code, _, feature = READS[key]
        self._require(feature, key)
        if key in self._unsupported:
            raise CommandRefusedError(f"The camera does not support {key}.")
        return await self._write(code, payload, feedback=(key,))

    def _require(self, feature, label):
        if not self._supports(feature):
            raise CommandRefusedError(f"This camera does not support {label}.")

    async def _lens(self, key, value):
        percent = bounded(value, 0, 100)
        return await self._write_value(
            key, round(percent * 4095 / 100).to_bytes(2, "little")
        )

    async def _zoom_step(self, delta):
        current = await self._read("zoom")
        if current is None:
            raise CommandRefusedError("The camera did not report its zoom position.")
        return await self._lens("zoom", max(0, min(100, current + delta)))

    async def _preset(self, number, store):
        slot = bounded(number, 1, 3 if store else 8)
        if slot not in self._presets:
            raise CommandRefusedError(f"Preset {slot} is unavailable on this camera.")
        await self._write(0x4112 if store else 0x4111, bytes([slot - 1]))
        if not store:
            await self._sync()
        return True

    async def _power(self, on):
        self._require((0, 4), "power control")
        await self._write(0x4100, bytes([on]))
        # Deep standby may drop the connection as soon as the camera acknowledges.
        if on or self._reported.get("standby_mode") not in STANDBY[1:3]:
            await self._read("power")
        self._preview()
        return True

    async def _boolean(self, key, enabled):
        if not isinstance(enabled, bool):
            raise CommandRefusedError("Choose on or off.")
        return await self._write_value(key, bytes([enabled]))

    async def _once(self):
        self._require((4, 8), "one-push autofocus")
        return await self._write(0x420D, b"\x00", feedback=("autofocus", "focus"))

    async def _select(self, source):
        if source not in self._sources:
            raise CommandRefusedError("Choose a source reported by this camera.")
        return await self._write_value("source", bytes([SOURCES.index(source)]))

    async def _record(self, code):
        self._require((7, 4), "recording")
        return await self._write(code, feedback=("recording_status",))

    async def _refresh(self):
        await self._identify()
        await self._sync(inventory=True)
        return True

    async def _command(self, command, params):
        if command.startswith("read_") and command in self.DRIVER_INFO["commands"]:
            value = await self._read(command[5:])
            if value is None:
                raise CommandRefusedError(
                    "This camera cannot report that value at present."
                )
            return value
        actions = {
            "power_on": lambda: self._power(True),
            "standby": lambda: self._power(False),
            "freeze": lambda: self._boolean("frozen", True),
            "go_live": lambda: self._boolean("frozen", False),
            "set_frozen": lambda: self._boolean("frozen", params["enabled"]),
            "set_lamp": lambda: self._boolean("lamp", params["enabled"]),
            "set_autofocus": lambda: self._boolean("autofocus", params["enabled"]),
            "focus_once": self._once,
            "zoom_in": lambda: self._zoom_step(10),
            "zoom_out": lambda: self._zoom_step(-10),
            "set_zoom": lambda: self._lens("zoom", params["value"]),
            "set_focus": lambda: self._lens("focus", params["value"]),
            "set_brightness": lambda: self._write_value(
                "brightness",
                bounded(params["value"], -10, 10).to_bytes(4, "little", signed=True),
            ),
            "select_source": lambda: self._select(params["source"]),
            "recall_preset": lambda: self._preset(params["preset"], False),
            "store_preset": lambda: self._preset(params["preset"], True),
            "start_recording": lambda: self._record(0x4109),
            "pause_recording": lambda: self._record(0x410A),
            "stop_recording": lambda: self._record(0x410B),
            "refresh": self._refresh,
        }
        if command not in actions:
            raise CommandRefusedError("Unknown Visualizer command.")
        return await actions[command]()

    async def _wake(self):
        mac = self.config.get("mac_address") or self.get_state("mac_address")
        if not mac:
            raise WakeAddressError(
                "Power on the camera locally once or enter its wired MAC address in Edit Device."
            )
        await self.wake_on_lan(mac)
        return True

    def _online(self):
        return self._session and self.transport and self.transport.connected

    async def send_command(self, command, params=None):
        if command == "power_on" and not self._online():
            return await self._wake()
        async with self._io:
            try:
                result = await self._command(command, params or {})
            except WolfProtError as exc:
                label = self.DRIVER_INFO["commands"][command]["label"]
                detail = f"{label} was refused by the camera ({exc})."
                if self._reported.get("security_mode"):
                    detail += (
                        " Security mode restricts image capture, freeze and recording."
                    )
                self.set_state("last_error", detail)
                raise CommandRefusedError(detail) from None
            self.set_state("last_error", "")
            return result
