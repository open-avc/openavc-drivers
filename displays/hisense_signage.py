"""
OpenAVC Hisense commercial signage display driver.

Controls Hisense commercial displays that speak Hisense's signage command set:
the DM series (55OP90FE) over RS-232 and the 54DS012FE LED display over its
IP Control service (TCP, port 8000 by default) or RS-232.

Protocol sources (Hisense, both public on hisense-b2b.com):
  * "55OP90FE User Manual" (2026-05-12), chapter 8 "RS232": the data
    parameters (9600 8N1), the frame format and the full command table.
  * "54DS User Manual" (10 August 2026), "Serial Protocol": the IP Control
    service (default port 8000, 5000-12000) carrying the same frames, and
    Wake-on-LAN.

Frames: ``DD FF <length:2> <command:4> <monitor ID> <data...> <checksum>
BB CC`` to the display and ``AB AB ... CD CD`` back. The length counts the
command, the monitor ID, the data and the checksum (the 54DS note leaves the
ID out; both manuals' examples count it), and the checksum is the XOR of the
length, command, ID and data bytes.

Over TCP the 54DS manual says the command "is directly sent in ASCII format":
the frame's hex digits as text (``DDFF0007C12600000101E0BBCC``), which its
screenshot of the test tool shows sent with "Send As Hex" off. So the driver
sends hex text over TCP and the bytes themselves over RS-232, and reads a
reply in either form on either connection.

Why Python: the checksum is an XOR over each command's own value byte (the
volume, the input, the brightness), and the platform's declarative send
framing computes a length but no checksum; replies are binary frames whose
data must be split by position, and the end marker cannot frame them (Power
On's data is BB BB).

Push vs poll: POLL. The manuals describe request and reply only, with one
exception: Power On sent in standby is answered at once and again when the
display has started, and the driver reads the status again on that second
answer.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import CallableFrameParser, FrameParser
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import WakeAddressError
except ImportError:  # OpenAVC before 0.37.1 has no typed wake refusal
    WakeAddressError = ValueError

log = get_logger(__name__)

SEND_HEAD = b"\xdd\xff"
SEND_END = b"\xbb\xcc"
REPLY_HEAD = b"\xab\xab"
REPLY_END = b"\xcd\xcd"
#: The longest documented reply is the 32-byte device ID (length 0x26).
MAX_LENGTH = 0x60
REPLY_TIMEOUT_S = 2.0
#: Every this many polls, the settings and identity are read again.
SETTINGS_EVERY = 6


# ── Framing (module level so tests/protocol_math can reach it) ───────────────

def checksum(data: bytes) -> int:
    """XOR of every byte given (the length, command, monitor ID and data)."""
    value = 0
    for b in data:
        value ^= b
    return value


def build_frame(command: bytes, monitor_id: int, data: bytes = b"") -> bytes:
    """A frame to the display: head, length, command, ID, data, checksum, end."""
    body = bytes(command) + bytes([monitor_id & 0xFF]) + bytes(data)
    length = (len(body) + 1).to_bytes(2, "big")
    return SEND_HEAD + length + body + bytes([checksum(length + body)]) + SEND_END


def as_text(frame: bytes) -> bytes:
    """The frame as the 54DS's IP Control takes it: its hex digits as text."""
    return frame.hex().upper().encode("ascii")


def _split_binary(buffer: bytes) -> tuple[bytes | None, bytes]:
    start = buffer.find(REPLY_HEAD)
    if start < 0:
        return None, buffer[-1:] if buffer.endswith(REPLY_HEAD[:1]) else b""
    buffer = buffer[start:]
    if len(buffer) < 4:
        return None, buffer
    length = int.from_bytes(buffer[2:4], "big")
    if not 6 <= length <= MAX_LENGTH:
        return None, buffer[2:]
    total = 4 + length + 2
    if len(buffer) < total:
        return None, buffer
    frame = buffer[:total]
    if frame[-2:] != REPLY_END or frame[-3] != checksum(frame[2:-3]):
        return None, buffer[2:]
    return frame, buffer[total:]


_TEXT_HEAD = re.compile(rb"AB[ \t]*AB", re.IGNORECASE)


def parse_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Split one reply off the front of ``buffer`` (the CallableFrameParser contract).

    A reply is a binary frame (``AB AB ... CD CD``) or the same frame written
    as hex text, spaces allowed; a text reply is returned decoded to bytes.
    Returns ``(frame, rest)``, ``(None, buffer)`` to wait for more, or
    ``(None, shorter)`` to drop what no frame can use.
    """
    binary = buffer.find(REPLY_HEAD)
    text = _TEXT_HEAD.search(buffer)
    if text is not None and (binary < 0 or text.start() < binary):
        tail = buffer[text.start():]
        digits = bytearray()
        used = 0
        for ch in tail:
            used += 1
            if chr(ch) in "0123456789abcdefABCDEF":
                digits.append(ch)
            elif ch not in (0x20, 0x0D, 0x0A, 0x09):
                break
            if len(digits) >= 8 and len(digits) % 2 == 0:
                raw = bytes.fromhex(digits.decode())
                frame, _rest = _split_binary(raw)
                if frame is not None and len(frame) * 2 == len(digits):
                    return frame, tail[used:]
        if len(digits) > 2 * (4 + MAX_LENGTH + 2):
            return None, tail[2:]
        return None, buffer[text.start():]
    return _split_binary(buffer)


def frame_parts(frame: bytes) -> tuple[bytes, int, bytes] | None:
    """(command, monitor ID, data) of a whole reply, or None."""
    if len(frame) < 10 or frame[:2] != REPLY_HEAD or frame[-2:] != REPLY_END:
        return None
    if frame[-3] != checksum(frame[2:-3]):
        return None
    return bytes(frame[4:8]), frame[8], bytes(frame[9:-3])


# ── Wire tables (55OP90FE User Manual, chapter 8) ────────────────────────────

def _cmd(text: str) -> bytes:
    return bytes.fromhex(text)


POWER = _cmd("C1150000")
SCREEN = _cmd("C1310000")
REBOOT = _cmd("C11E0000")
AC_POWER_ON_SET = _cmd("C1FF0009")
INPUT_SET = _cmd("C1080000")
ROTATION = _cmd("C1350000")
MUTE = _cmd("C1260000")
VOLUME = _cmd("C1270000")
BACKLIGHT_SET = _cmd("C1320000")
AUTO_BACKLIGHT = _cmd("C1340000")
BRIGHTNESS_SET = _cmd("C1360000")
CONTRAST = _cmd("C1370000")
SHARPNESS = _cmd("C1380000")
NOISE_REDUCTION = _cmd("C13A0000")
SCALING = _cmd("C13B0000")
PICTURE_MODE_SET = _cmd("C10F0600")
SOUND_MODE_SET = _cmd("C1FF0003")
EYE_SET = _cmd("C1FF001E")
POWER_ON_DELAY = _cmd("C13C0000")
USB_SET = _cmd("C1FF000E")
STATUS = _cmd("C1280000")
SCREEN_GET = _cmd("C1320001")
VERSION_GET = _cmd("C11B0000")
BACKLIGHT_GET = _cmd("C13E0024")
BRIGHTNESS_GET = _cmd("C1360001")
NETWORK_GET = _cmd("C1FF0016")
SOUND_MODE_GET = _cmd("C1FF0002")
AC_POWER_ON_GET = _cmd("C1FF0008")
IP_GET = _cmd("C11B2000")
TEMPERATURE_GET = _cmd("C1FE0000")
PICTURE_MODE_GET = _cmd("C16D0000")
USB_GET = _cmd("C16E0000")
EYE_GET = _cmd("C1FF001D")
SERIAL_GET = _cmd("C1FF000B")
MAC_GET = _cmd("C16C0000")
BRAND_GET = _cmd("C1FE0001")
MODEL_GET = _cmd("C1FE0002")
REMOTE_KEY = _cmd("C1170000")
OPEN_SETTINGS = _cmd("C1410000")
OPEN_HOME = _cmd("C1FF001A")
OPEN_CMS = _cmd("C1FF0013")

#: Input select codes (the Input commands).
INPUT_CODES = {"hdmi_1": 0x0E, "hdmi_2": 0x0F, "hdmi_3": 0x12, "hdbt": 0x2A}
#: The source as Query TV Status reports it (two bytes), a different table.
STATUS_SOURCES = {b"\x05\x05": "hdmi_1", b"\x05\x04": "hdmi_2", b"\x05\x0b": "hdmi_3", b"\x05\x2a": "hdbt"}
PICTURE_MODES = {
    "standard": 0x00, "bright": 0x01, "soft": 0x02, "movie": 0x03, "text": 0x04,
    "game": 0x05, "natural": 0x12,
}
#: Query Picture Mode also answers 06 (AI) and 07 (user), which cannot be set.
PICTURE_MODE_NAMES = {**{v: k for k, v in PICTURE_MODES.items()}, 0x06: "ai", 0x07: "user"}
SOUND_MODES = {
    "standard": 0x00, "music": 0x01, "news": 0x02, "movie": 0x08, "sports": 0x10,
    "custom": 0x20, "voice": 0x30, "meeting": 0x40,
}
SOUND_MODE_NAMES = {v: k for k, v in SOUND_MODES.items()}
POWER_ON_MODES = {"direct": 0x00, "last": 0x01, "standby": 0x02}
POWER_ON_MODE_NAMES = {v: k for k, v in POWER_ON_MODES.items()}
BACKLIGHT_MODE_NAMES = {
    0x01: "bright", 0x02: "soft", 0x03: "auto", 0x04: "stereo_frequency",
    0x05: "comfort_frequency", 0x06: "custom",
}
NOISE_LEVELS = {"off": 0x00, "low": 0x01, "medium": 0x02, "high": 0x03, "auto": 0x04}
SCALING_MODES = {"full": 0x00, "16_9": 0x01, "4_3": 0x02}
REMOTE_KEYS = {
    "menu": 0x0000, "up": 0x0001, "down": 0x0002, "left": 0x0003, "right": 0x0004,
    "ok": 0x0005, "return": 0x0006, "source": 0x0007,
}


def _ascii(data: bytes) -> str:
    return data.replace(b"\x00", b"").decode("ascii", errors="replace").strip()


class HisenseSignageDriver(BaseDriver):
    """Hisense commercial signage displays (DM series, 54DS) over RS-232 or IP Control."""

    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The display stopped answering status queries."

    DRIVER_INFO = {
        "id": "hisense_signage",
        "name": "Hisense Commercial Signage Display",
        "manufacturer": "Hisense",
        "category": "display",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Hisense commercial signage displays with Hisense's "
            "signage command set: the DM series (55OP90FE outdoor) over "
            "RS-232 and the 54DS012FE LED display over its IP Control "
            "service or RS-232. Power with Wake-on-LAN, screen on and off, "
            "input, volume and mute, picture and sound modes, brightness and "
            "backlight, rotation, the remote's keys, and status: signal, "
            "temperature, model, serial number and network."
        ),
        "source_url": "https://www.hisense-b2b.com/en/Attachment/DownloadFile?downloadId=1046",
        "tags": ["display", "hisense", "digital-signage", "outdoor", "led", "rs232", "lan-control"],
        "verified": False,
        "simulated": True,
        "ports": [8000],
        "min_platform_version": "0.37.0",
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "discovery": {
            # Query TV Status for monitor ID 1 as the IP Control takes it (hex
            # text, 54DS manual), matched on the reply's head, length and
            # command in either form. A display with another Serial Port ID,
            # or with the IP Control app stopped, does not answer it.
            "tcp_probe": {
                "port": 8000,
                "send_ascii": "DDFF0006C128000001EEBBCC",
                "expect_regex": r"(?i)AB ?AB ?00 ?0C ?C1 ?28|\xab\xab\x00\x0c\xc1\x28",
                "timeout_ms": 3000,
            },
        },
        "compatible_models": [
            {
                "manufacturer": "Hisense",
                "models": ["55OP90FE"],
                "confidence": "untested",
                "notes": (
                    "DM series outdoor display. RS-232 at 9600 8N1 (the RS232 IN "
                    "port; an RS232 converter cable is an option in the box). "
                    "Its manual names an IP Control service but documents no "
                    "port for it."
                ),
            },
            {
                "manufacturer": "Hisense",
                "models": ["54DS012FE"],
                "confidence": "untested",
                "notes": (
                    "54-inch LED signage display. IP Control on TCP 8000 by "
                    "default, once the IP Control app on the display is started, "
                    "or RS-232."
                ),
            },
        ],
        "help": {
            "overview": (
                "Hisense commercial signage displays. Power (with Wake-on-LAN "
                "over the network), screen on and off, input, volume and mute, "
                "picture and sound modes, brightness, backlight and rotation, "
                "the remote's keys, and the display's status: signal, "
                "temperature, model, serial number and network. Polls every 10 "
                "seconds."
            ),
            "setup": (
                "1. Over the network (54DS012FE): on the display, open Apps > "
                "IP Control, check the port (8000 unless it was changed) and "
                "press Start. Give the display a fixed IP address and enter it "
                "with the port.\n"
                "2. Over RS-232 (the DM series, or a 54DS): connect to the "
                "display's RS232 port at 9600 baud, 8 data bits, no parity, 1 "
                "stop bit, and choose the serial connection.\n"
                "3. Monitor ID: the display's Serial Port ID (Settings > Signal "
                "Manager > Serial Port ID on the DM series, Settings > System > "
                "Serial Port ID Setting on the 54DS). 1 unless it was changed.\n"
                "4. To turn the display on over the network from standby, turn "
                "on Wake-on-LAN on the display. The MAC address is read from the "
                "display when it first connects.\n"
                "5. The display's Remote Control Lock turns off IP Control: "
                "leave it off."
            ),
        },
        "default_config": {
            "host": "",
            "port": 8000,
            "baudrate": 9600,
            "parity": "N",
            "bytesize": 8,
            "stopbits": 1,
            "monitor_id": 1,
            "mac_address": "",
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address",
                     "description": "The display's IP address (network control)."},
            "port": {"type": "integer", "default": 8000, "label": "TCP Port",
                     "description": "The port shown in the display's IP Control app: 8000 unless it was changed."},
            "baudrate": {"type": "integer", "default": 9600, "label": "Baud Rate (serial)",
                         "description": "9600, as the display ships."},
            "monitor_id": {"type": "integer", "default": 1, "min": 0, "max": 255,
                           "label": "Monitor ID",
                           "description": "The display's Serial Port ID."},
            "mac_address": {"type": "string", "default": "", "label": "MAC Address",
                            "learned_from": "mac_address",
                            "description": "For Wake-on-LAN. Read from the display when it connects."},
            "poll_interval": {"type": "integer", "default": 10, "min": 1,
                              "label": "Poll Interval (sec)"},
        },
        "state_variables": {
            "power": {"type": "enum", "values": ["on", "off"], "label": "Power",
                      "control": True, "cloud_priority": "high"},
            "screen_on": {"type": "boolean", "label": "Screen On",
                          "help": "False while Screen Off has the picture blanked.",
                          "control": True, "cloud_priority": "high"},
            "input": {"type": "enum", "values": ["hdmi_1", "hdmi_2", "hdmi_3", "hdbt", ""],
                      "label": "Input", "control": True, "cloud_priority": "high"},
            "volume": {"type": "integer", "label": "Volume", "min": 0, "max": 100,
                       "control": True},
            "mute": {"type": "boolean", "label": "Mute", "control": True,
                     "cloud_priority": "high"},
            "signal": {"type": "boolean", "label": "Signal",
                       "help": "The current input has a signal.", "cloud_priority": "high"},
            "temperature": {"type": "integer", "label": "Temperature", "unit": "°C",
                            "cloud_priority": "low"},
            "brightness": {"type": "integer", "label": "Brightness", "min": 0, "max": 100},
            "backlight_mode": {"type": "enum", "label": "Backlight Mode",
                               "values": ["bright", "soft", "auto", "stereo_frequency",
                                          "comfort_frequency", "custom", ""]},
            "backlight": {"type": "integer", "label": "Backlight",
                          "help": "The custom backlight level, while Backlight Mode is custom."},
            "picture_mode": {"type": "enum", "label": "Picture Mode",
                             "values": ["standard", "bright", "soft", "movie", "text", "game",
                                        "natural", "ai", "user", ""]},
            "sound_mode": {"type": "enum", "label": "Sound Mode",
                           "values": ["standard", "music", "news", "movie", "sports", "custom",
                                      "voice", "meeting", ""]},
            "eye_protection": {"type": "boolean", "label": "Eye Protection"},
            "usb_enabled": {"type": "boolean", "label": "USB Enabled",
                            "help": "False while the USB ports are locked."},
            "power_on_mode": {"type": "enum", "label": "Power-On Mode",
                              "values": ["direct", "last", "standby", ""],
                              "help": "What the display does when its power is restored."},
            "network_connected": {"type": "boolean", "label": "Network Connected",
                                  "cloud_priority": "low"},
            "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
            "brand": {"type": "string", "label": "Brand", "cloud_priority": "low"},
            "serial_number": {"type": "string", "label": "Serial Number", "cloud_priority": "low"},
            "software_version": {"type": "string", "label": "Software Version",
                                 "cloud_priority": "low"},
            "mac_address": {"type": "string", "label": "MAC Address", "cloud_priority": "low"},
            "ip_address": {"type": "string", "label": "IP Address", "cloud_priority": "low"},
        },
        "device_settings": {
            "picture_mode": {
                "type": "enum", "label": "Picture Mode", "state_key": "picture_mode",
                "values": ["standard", "bright", "soft", "movie", "text", "game", "natural"],
                "default": "standard", "setup": False,
            },
            "sound_mode": {
                "type": "enum", "label": "Sound Mode", "state_key": "sound_mode",
                "values": ["standard", "music", "news", "movie", "sports", "custom", "voice", "meeting"],
                "default": "standard", "setup": False,
            },
            "brightness": {
                "type": "integer", "label": "Brightness", "state_key": "brightness",
                "min": 0, "max": 100, "default": 50, "setup": False,
            },
            "eye_protection": {
                "type": "boolean", "label": "Eye Protection", "state_key": "eye_protection",
                "default": False, "setup": False,
            },
            "usb_enabled": {
                "type": "boolean", "label": "USB Enabled", "state_key": "usb_enabled",
                "default": True, "setup": False,
                "help": "Off locks the display's USB ports.",
            },
            "power_on_mode": {
                "type": "enum", "label": "Power-On Mode", "state_key": "power_on_mode",
                "values": ["direct", "last", "standby"], "default": "last", "setup": False,
                "help": "What the display does when its power is restored: turn on, return to how it was, or stay in standby.",
            },
        },
        "quick_actions": ["power_on", "power_off", "set_input", "mute_on", "mute_off"],
        "commands": {
            "power_on": {
                "label": "Power On", "params": {}, "available_offline": True,
                "help": "Turns the display on. Over the network a display in standby is woken with Wake-on-LAN first.",
            },
            "power_off": {"label": "Power Off", "params": {},
                          "help": "Puts the display in standby."},
            "screen_on": {"label": "Screen On", "params": {},
                          "help": "Turns the picture back on after Screen Off."},
            "screen_off": {"label": "Screen Off", "params": {},
                           "help": "Turns the picture off while the display stays on."},
            "set_input": {
                "label": "Select Input",
                "params": {"input": {"type": "enum", "required": True, "label": "Input",
                                     "values": ["hdmi_1", "hdmi_2", "hdmi_3", "hdbt"]}},
                "help": "Switches the display to the input.",
            },
            "mute_on": {"label": "Mute", "params": {}, "help": "Mutes the display's sound."},
            "mute_off": {"label": "Unmute", "params": {}, "help": "Unmutes the display's sound."},
            "set_volume": {
                "label": "Set Volume",
                "params": {"level": {"type": "integer", "required": True, "min": 0, "max": 100,
                                     "label": "Volume"}},
                "help": "Sets the display's volume.",
            },
            "set_backlight": {
                "label": "Set Backlight",
                "params": {"level": {"type": "integer", "required": True, "min": 0, "max": 100,
                                     "label": "Backlight"}},
                "help": "Sets a custom backlight level (Backlight Mode becomes custom).",
            },
            "set_auto_backlight": {
                "label": "Set Auto Backlight",
                "params": {"enabled": {"type": "boolean", "required": True, "label": "Auto"}},
                "help": "Turns automatic backlight adjustment on or off.",
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {"level": {"type": "integer", "required": True, "min": 0, "max": 100,
                                     "label": "Contrast"}},
                "help": "Sets the contrast. Only on HDMI, DP, VGA, PC and DVI inputs.",
            },
            "set_sharpness": {
                "label": "Set Sharpness",
                "params": {"level": {"type": "integer", "required": True, "min": 0, "max": 100,
                                     "label": "Sharpness"}},
                "help": "Sets the sharpness. Only on HDMI, DP, VGA, PC and DVI inputs.",
            },
            "set_noise_reduction": {
                "label": "Set Noise Reduction",
                "params": {"level": {"type": "enum", "required": True, "label": "Noise Reduction",
                                     "values": ["off", "low", "medium", "high", "auto"]}},
                "help": "Sets the picture's noise reduction.",
            },
            "set_scaling": {
                "label": "Set Image Scaling",
                "params": {"mode": {"type": "enum", "required": True, "label": "Scaling",
                                    "values": ["full", "16_9", "4_3"]}},
                "help": "Sets how the picture fills the screen.",
            },
            "set_rotation": {
                "label": "Set Rotation",
                "params": {"degrees": {"type": "enum", "required": True, "label": "Rotation",
                                       "values": ["0", "90"]}},
                "help": "Sets the screen's rotation. It takes effect when the display next restarts.",
            },
            "set_power_on_delay": {
                "label": "Set Power-On Delay",
                "params": {"seconds": {"type": "integer", "required": True, "min": 0, "max": 255,
                                       "label": "Delay (s)",
                                       "help": "0 turns the delay off; otherwise 2 to 255 seconds."}},
                "help": "Delays the display turning on, so a row of displays does not start all at once.",
            },
            "remote_key": {
                "label": "Send Remote Key",
                "params": {"key": {"type": "enum", "required": True, "label": "Key",
                                   "values": ["menu", "up", "down", "left", "right", "ok", "return", "source"]}},
                "help": "Presses a key on the display's remote control.",
            },
            "open_home": {"label": "Open Home Screen", "params": {},
                          "help": "Opens the display's home screen."},
            "open_settings": {"label": "Open Settings", "params": {},
                              "help": "Opens the display's settings menu."},
            "open_cms": {"label": "Open CMS", "params": {},
                         "help": "Opens the display's content management (signage) app."},
            "reboot": {"label": "Restart Display", "params": {},
                       "help": "Restarts the display. The picture drops until it is back."},
        },
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any):
        super().__init__(device_id, config, state, events)
        self._waiters: dict[bytes, list[asyncio.Future]] = {}
        self._cycle = 0
        self._identity_read = False

    # ── Transport ────────────────────────────────────────────────────────────

    def _transport_type(self) -> str:
        # Read here rather than through the platform's helper, which is newer
        # than this driver's floor.
        chosen = self.config.get("transport") or self.DRIVER_INFO["transport"]
        return "serial" if str(chosen).lower() == "serial" else "tcp"

    def _over_network(self) -> bool:
        return self._transport_type() == "tcp"

    def _create_frame_parser(self) -> Optional[FrameParser]:
        return CallableFrameParser(parse_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        return None

    def _monitor_id(self) -> int:
        try:
            return int(self.config.get("monitor_id", 1)) & 0xFF
        except (TypeError, ValueError):
            return 1

    async def _send(self, command: bytes, data: bytes = b"") -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        frame = build_frame(command, self._monitor_id(), data)
        await self.transport.send(as_text(frame) if self._over_network() else frame)

    async def _request(self, command: bytes, data: bytes = b"",
                       timeout: float = REPLY_TIMEOUT_S) -> bytes:
        """Send a frame and wait for the reply carrying the same command."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(command, []).append(fut)
        try:
            await self._send(command, data)
            return await asyncio.wait_for(fut, timeout)
        finally:
            waiters = self._waiters.get(command, [])
            if fut in waiters:
                waiters.remove(fut)

    async def _read(self, command: bytes) -> bytes | None:
        """A query whose silence is not a fault (a display in standby may not answer it)."""
        try:
            return await self._request(command)
        except asyncio.TimeoutError:
            return None

    async def _set(self, command: bytes, data: bytes, read_back: bytes | None = None) -> bool:
        """Send a set and wait for its acknowledgement, then read the value back."""
        try:
            await self._request(command, data)
        except asyncio.TimeoutError:
            raise RuntimeError("The display did not acknowledge the command.") from None
        if read_back is not None:
            await self._read(read_back)
        return True

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _initial_sync(self) -> None:
        self._cycle = 0
        self._identity_read = False
        try:
            await self.poll()
        except asyncio.TimeoutError:
            # The display answers only frames carrying its own Serial Port ID.
            raise ConnectionError(
                f"The display did not answer a status query for Monitor ID "
                f"{self._monitor_id()}. Check that the Monitor ID matches the "
                f"display's Serial Port ID."
            ) from None

    async def _read_identity(self) -> None:
        for command in (BRAND_GET, MODEL_GET, SERIAL_GET, VERSION_GET, MAC_GET, IP_GET):
            await self._read(command)
        self._identity_read = bool(self.get_state("model") or self.get_state("serial_number"))

    async def _read_settings(self) -> None:
        for command in (PICTURE_MODE_GET, SOUND_MODE_GET, BACKLIGHT_GET, EYE_GET, USB_GET,
                        AC_POWER_ON_GET, NETWORK_GET):
            await self._read(command)

    async def poll(self) -> None:
        # Query TV Status first: it answers in standby too, and carries power,
        # input, volume, mute and signal. Its silence means the link is gone.
        await self._request(STATUS)
        self._cycle += 1
        if self.get_state("power") != "on":
            return
        for command in (SCREEN_GET, BRIGHTNESS_GET, TEMPERATURE_GET):
            await self._read(command)
        if not self._identity_read:
            await self._read_identity()
        if self._cycle == 1 or self._cycle % SETTINGS_EVERY == 0:
            await self._read_settings()

    async def _liveness_probe(self) -> None:
        await self._request(STATUS)

    # ── Receive ──────────────────────────────────────────────────────────────

    async def on_data_received(self, data: bytes) -> None:
        # Nothing here awaits: each message's handler runs as a task of its
        # own, and one that paused could let the next message in first.
        parts = frame_parts(data)
        if parts is None:
            return
        command, _monitor, payload = parts
        self._apply_reply(command, payload)
        waiters = self._waiters.get(command) or []
        if waiters:
            for fut in waiters:
                if not fut.done():
                    fut.set_result(payload)
        elif command == POWER and payload == b"\xbb\xbb":
            # Power On in standby is answered twice: at once, and again when
            # the display has started. The second answer is the one to read.
            task = asyncio.ensure_future(self._read(STATUS))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _apply_reply(self, command: bytes, data: bytes) -> None:
        if command == STATUS and len(data) >= 6:
            self.set_state("volume", data[0])
            self.set_state("input", STATUS_SOURCES.get(bytes(data[1:3]), ""))
            self.set_state("power", "off" if data[3] == 0xFF else "on")
            self.set_state("mute", data[4] == 0x01)
            self.set_state("signal", data[5] == 0x01)
        elif command == SCREEN_GET and data:
            self.set_state("screen_on", data[0] == 0x01)
        elif command == SCREEN and data:
            self.set_state("screen_on", data[0] == 0x01)
        elif command == BRIGHTNESS_GET and data:
            self.set_state("brightness", data[0])
        elif command == TEMPERATURE_GET and data:
            self.set_state("temperature", data[0])
        elif command == BACKLIGHT_GET and data:
            self.set_state("backlight_mode", BACKLIGHT_MODE_NAMES.get(data[0], ""))
            if data[0] == 0x06 and len(data) >= 2:
                self.set_state("backlight", data[1])
        elif command == PICTURE_MODE_GET and data:
            self.set_state("picture_mode", PICTURE_MODE_NAMES.get(data[0], ""))
        elif command == SOUND_MODE_GET and data:
            self.set_state("sound_mode", SOUND_MODE_NAMES.get(data[0], ""))
        elif command == EYE_GET and data:
            self.set_state("eye_protection", data[0] == 0x01)
        elif command == USB_GET and data:
            self.set_state("usb_enabled", data[0] == 0x01)
        elif command == AC_POWER_ON_GET and data:
            self.set_state("power_on_mode", POWER_ON_MODE_NAMES.get(data[0], ""))
        elif command == NETWORK_GET and data:
            self.set_state("network_connected", data[0] == 0x01)
        elif command == BRAND_GET and data:
            self.set_state("brand", _ascii(data))
        elif command == MODEL_GET and data:
            self.set_state("model", _ascii(data))
        elif command == SERIAL_GET and data:
            self.set_state("serial_number", _ascii(data))
        elif command == VERSION_GET and len(data) >= 3:
            self.set_state("software_version", f"{data[0]:02X}{data[1]:02X}{data[2]:02X}")
        elif command == MAC_GET and len(data) >= 6:
            self.set_state("mac_address", ":".join(f"{b:02X}" for b in data[:6]))
        elif command == IP_GET and len(data) >= 4:
            self.set_state("ip_address", ".".join(str(b) for b in data[:4]))

    # ── Commands ─────────────────────────────────────────────────────────────

    @staticmethod
    def _level(value: Any, top: int = 100) -> int:
        level = int(value)
        if not 0 <= level <= top:
            raise ValueError(f"Level must be 0-{top}")
        return level

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if command == "power_on":
            return await self._power_on()
        if command == "power_off":
            return await self._set(POWER, b"\xaa\xaa", STATUS)
        if command in ("screen_on", "screen_off"):
            return await self._set(SCREEN, b"\x01" if command == "screen_on" else b"\x00", SCREEN_GET)
        if command == "set_input":
            code = INPUT_CODES.get(str(params.get("input", "")))
            if code is None:
                raise ValueError(f"Input must be one of {', '.join(INPUT_CODES)}")
            return await self._set(INPUT_SET, bytes([code]), STATUS)
        if command in ("mute_on", "mute_off"):
            return await self._set(MUTE, b"\x01" if command == "mute_on" else b"\x00", STATUS)
        if command == "set_volume":
            return await self._set(VOLUME, bytes([self._level(params["level"])]), STATUS)
        if command == "set_backlight":
            return await self._set(BACKLIGHT_SET, bytes([0x06, self._level(params["level"])]),
                                   BACKLIGHT_GET)
        if command == "set_auto_backlight":
            on = params.get("enabled") in (True, "true", "True", 1, "1", "on")
            return await self._set(AUTO_BACKLIGHT, b"\x01" if on else b"\x00", BACKLIGHT_GET)
        if command == "set_contrast":
            return await self._set(CONTRAST, bytes([self._level(params["level"])]))
        if command == "set_sharpness":
            return await self._set(SHARPNESS, bytes([self._level(params["level"])]))
        if command == "set_noise_reduction":
            level = NOISE_LEVELS.get(str(params.get("level", "")))
            if level is None:
                raise ValueError(f"Noise reduction must be one of {', '.join(NOISE_LEVELS)}")
            return await self._set(NOISE_REDUCTION, bytes([level]))
        if command == "set_scaling":
            mode = SCALING_MODES.get(str(params.get("mode", "")))
            if mode is None:
                raise ValueError(f"Scaling must be one of {', '.join(SCALING_MODES)}")
            return await self._set(SCALING, bytes([mode]))
        if command == "set_rotation":
            degrees = str(params.get("degrees", ""))
            if degrees not in ("0", "90"):
                raise ValueError("Rotation must be 0 or 90")
            return await self._set(ROTATION, b"\x00" if degrees == "0" else b"\x01")
        if command == "set_power_on_delay":
            seconds = self._level(params["seconds"], 255)
            if seconds == 1:
                raise ValueError("The delay is 0 (off) or 2 to 255 seconds")
            return await self._set(POWER_ON_DELAY, bytes([seconds]))
        if command == "remote_key":
            key = REMOTE_KEYS.get(str(params.get("key", "")))
            if key is None:
                raise ValueError(f"Key must be one of {', '.join(REMOTE_KEYS)}")
            return await self._set(REMOTE_KEY, key.to_bytes(2, "big"))
        if command == "open_home":
            return await self._set(OPEN_HOME, b"")
        if command == "open_settings":
            return await self._set(OPEN_SETTINGS, b"")
        if command == "open_cms":
            return await self._set(OPEN_CMS, b"")
        if command == "reboot":
            # The manual shows an acknowledgement; a display that restarts at
            # once may cut it off, which is not a refusal.
            try:
                await self._request(REBOOT)
            except asyncio.TimeoutError:
                log.info(f"[{self.device_id}] No acknowledgement to Restart Display")
            return True
        raise ValueError(f"Unknown command: {command}")

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "picture_mode":
            mode = PICTURE_MODES.get(str(value))
            if mode is None:
                raise ValueError(f"Picture mode must be one of {', '.join(PICTURE_MODES)}")
            return await self._set(PICTURE_MODE_SET, bytes([mode]), PICTURE_MODE_GET)
        if key == "sound_mode":
            mode = SOUND_MODES.get(str(value))
            if mode is None:
                raise ValueError(f"Sound mode must be one of {', '.join(SOUND_MODES)}")
            return await self._set(SOUND_MODE_SET, bytes([mode]), SOUND_MODE_GET)
        if key == "brightness":
            return await self._set(BRIGHTNESS_SET, bytes([self._level(value)]), BRIGHTNESS_GET)
        if key == "eye_protection":
            return await self._set(EYE_SET, b"\x01" if value in (True, "true", 1) else b"\x00", EYE_GET)
        if key == "usb_enabled":
            return await self._set(USB_SET, b"\x01" if value in (True, "true", 1) else b"\x00", USB_GET)
        if key == "power_on_mode":
            mode = POWER_ON_MODES.get(str(value))
            if mode is None:
                raise ValueError(f"Power-on mode must be one of {', '.join(POWER_ON_MODES)}")
            return await self._set(AC_POWER_ON_SET, bytes([mode]), AC_POWER_ON_GET)
        raise ValueError(f"Unknown device setting '{key}'")

    # ── Power On: Wake-on-LAN, then the protocol ─────────────────────────────

    def _wake_mac(self) -> str:
        return (
            str(self.get_state("mac_address") or "").strip()
            or str(self.config.get("mac_address") or "").strip()
        )

    async def _power_on(self) -> bool:
        """Wake a display in standby and turn a reachable one on.

        Declared ``available_offline``. Over RS-232 the Power On frame is
        enough (the manual: a display in standby answers it). Over the network
        the 54DS manual wakes the display with Wake-on-LAN, so a known MAC gets
        the magic packet, and a live connection gets the frame as well.
        """
        connected = bool(self.transport and self.transport.connected)
        if not self._over_network():
            if not connected:
                raise ConnectionError(f"[{self.device_id}] Not connected")
            return await self._set(POWER, b"\xbb\xbb")
        mac = self._wake_mac()
        woke = False
        if mac:
            try:
                await self.wake_on_lan(mac)
                woke = True
            except ValueError as exc:
                if not connected:
                    raise WakeAddressError(
                        f"The display cannot be woken: '{mac}' is not a MAC address "
                        f"(six pairs of hex digits, like AA:BB:CC:DD:EE:FF)."
                    ) from exc
        if connected:
            return await self._set(POWER, b"\xbb\xbb")
        if not woke:
            raise WakeAddressError(
                "The display is offline and no MAC address is known, so it cannot "
                "be woken. The MAC is read from the display on its first connection; "
                "until then, enter it under Edit Device, or turn the display on at "
                "the display."
            )
        return True
