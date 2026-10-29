"""
OpenAVC Promethean ActivPanel 10 driver (RS232 control codes, serial or IP).

Controls the Promethean ActivPanel 10 and ActivPanel 10 Premium interactive
displays with Promethean's RS232 control codes, sent over the panel's LAN
port (TCP 5000, "RS232 over IP") or its RS-232 port (9600 8N1).

Why Python: every command and reply is a binary frame, ``F6 <command>
<type> <data...> <checksum> 6F``, with a one-byte additive checksum and no
length field. The identity and network replies carry several data bytes,
and the checksum can itself be 0x6F (Set Contrast 65 is ``F6 34 04 41 6F
6F``), so neither delimiter nor fixed-length framing can split the stream:
a frame ends at the first 0x6F whose preceding byte is the checksum of
everything before it, which takes code.

Push vs poll: POLL. The control codes are request/response only; nothing
in Promethean's documents describes the panel sending anything it was not
asked for. Gets are answered with the same command and type bytes as the
question, so each reply is matched to its question by those two bytes.

Login: none. The control-codes article lists "Logon Credentials: No" for
RS232 over IP, and the panel's settings have no control password.

Power model (Promethean's notes, article 2752):
  - 01 on, 02 standby, 03 sleep.
  - Over the network, Standby and Power On are not supported: the panel's
    network interface is powered off in Sleep, and the article marks Set
    Standby "[Serial only]" and Power On "Only supported with RS232 over
    Serial ... To wake the panel from sleep, WOL should be used instead."
    So Power Off over the network is Sleep, and Power On is a Wake-on-LAN
    magic packet (the panel's Settings > Power > Wake on LAN must be on)
    to the MAC the panel reports, plus the Power On frame whenever the
    panel is reachable. Over serial all three power commands work.

Model check: the ActivPanel 9 speaks the same frames on the same port,
and its own article (2586) warns that its power-status query can lock the
panel's power up. This driver asks the model number at connect and
refuses to run against an ActivPanel 9, LE or LX, which have their own
command tables.

Protocol reference: Promethean support article 2752, "RS232 Control Codes
for ActivPanel 10" (3 October 2025).
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import CallableFrameParser, FrameParser
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import WakeAddressError
except ImportError:  # OpenAVC before 0.37.1 has no typed wake refusal
    WakeAddressError = ValueError

try:
    from openavc.core.connection_fault import ConnectionFaultError
except ImportError:  # pragma: no cover - every supported platform has it
    ConnectionFaultError = None

log = get_logger(__name__)

HEAD = 0xF6
END = 0x6F
#: The longest frame the documents show is the 13-byte firmware reply; a
#: model or serial number may run longer. A buffer this long with no valid
#: end is noise, and the parser drops its first byte to resync.
MAX_FRAME = 64

#: How long a get waits for its reply.
REPLY_TIMEOUT_S = 1.5


# ── Framing (module level so tests/protocol_math can reach it) ───────────────

def checksum(data: bytes) -> int:
    """The control codes' checksum: the sum of every byte before it, mod 256.

    Article 2752: "CS = F6+[Command]+[Type]+[Data]"; the firmware-version
    example (``F6 03 01 33 2E 32 2E 30 2E 31 34 7E 6F``) shows the sum runs
    over every data byte when the data is longer than one.
    """
    return sum(data) & 0xFF


def build_frame(body: bytes) -> bytes:
    """Frame a command body (command, type, data...) for the wire."""
    head = bytes([HEAD]) + bytes(body)
    return head + bytes([checksum(head), END])


def _frame_end(buffer: bytes, start: int) -> int:
    """Index of the end code of a valid frame whose head is at ``start``, or -1.

    The end is the first 0x6F whose preceding byte is the checksum of every
    byte from the head up to it. The shortest frame is six bytes (head,
    command, type, data, checksum, end).
    """
    total = 0
    for i in range(start, min(len(buffer), start + MAX_FRAME)):
        if i - start >= 5 and buffer[i] == END and buffer[i - 1] == (total - buffer[i - 1]) & 0xFF:
            return i
        total += buffer[i]
    return -1


def parse_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Split one frame off the front of ``buffer`` (the CallableFrameParser contract).

    Returns ``(frame, rest)`` with the frame whole (head to end code), or
    ``(None, buffer)`` to wait for more bytes, or ``(None, shorter)`` to drop
    bytes no frame can use: anything before a head byte, a frame whose
    checksum failed (once a later head byte starts a whole frame), or a head
    byte with no valid end within MAX_FRAME bytes.
    """
    start = buffer.find(bytes([HEAD]))
    if start < 0:
        return None, b""
    if start:
        buffer = buffer[start:]
    end = _frame_end(buffer, 0)
    if end >= 0:
        return buffer[: end + 1], buffer[end + 1:]
    if len(buffer) >= MAX_FRAME:
        return None, buffer[1:]
    # No end yet: either the frame is still arriving, or it was corrupt and
    # a whole frame already follows it. Skip to that one rather than wait.
    later = buffer.find(bytes([HEAD]), 1)
    while later > 0:
        if _frame_end(buffer, later) >= 0:
            return None, buffer[later:]
        later = buffer.find(bytes([HEAD]), later + 1)
    return None, buffer


def frame_parts(frame: bytes) -> tuple[int, int, bytes] | None:
    """(command, type, data) of a whole frame, or None if it is not one."""
    if len(frame) < 6 or frame[0] != HEAD or frame[-1] != END:
        return None
    if frame[-2] != checksum(frame[:-2]):
        return None
    return frame[1], frame[2], bytes(frame[3:-2])


# ── Wire tables (article 2752) ──────────────────────────────────────────────

POWER_STATES = {0x01: "on", 0x02: "standby", 0x03: "sleep"}

INPUT_CODES = {
    "usb_c_1": 0x13,
    "usb_c_2": 0x0C,
    "hdmi_1": 0x09,
    "hdmi_2": 0x0A,
    "displayport": 0x14,
    "ops": 0x12,
}
INPUT_NAMES = {code: name for name, code in INPUT_CODES.items()}

TIMER_CODES = {
    "3_min": 0x00, "10_min": 0x01, "30_min": 0x02, "1_hour": 0x03,
    "4_hours": 0x04, "8_hours": 0x05, "12_hours": 0x06, "never": 0x07,
}
TIMER_NAMES = {code: name for name, code in TIMER_CODES.items()}

BACKLIGHT_MODES = {"eco": 0x00, "standard": 0x01, "theater": 0x02}
BACKLIGHT_MODE_NAMES = {code: name for name, code in BACKLIGHT_MODES.items()}

# Front-panel buttons: index byte of commands 0x54 (set) / 0x55 (get).
PANEL_BUTTONS = {
    "power_button": 0x01,
    "source_button": 0x02,
    "volume_button": 0x03,
    "flame_button": 0x04,
    "freeze_button": 0x05,
    "touch_button": 0x06,
}

# Picture levels: (get type, set type) under command 0x34, all 0-100.
PICTURE_LEVELS = {
    "contrast": (0x03, 0x04),
    "brightness": (0x05, 0x06),
    "sharpness": (0x07, 0x08),
    "backlight": (0x0B, 0x0C),
}

# Gets that read one data byte, by (command, type).
GET_POWER = (0x01, 0x02)
GET_MUTE = (0x02, 0x02)
GET_VOLUME = (0x0C, 0x02)
GET_INPUT = (0x30, 0x02)
GET_OPS_INSTALLED = (0x31, 0x02)
GET_OPS_POWER = (0x31, 0x03)
GET_FREEZE = (0x32, 0x02)
GET_BLANK = (0x35, 0x02)
GET_TOUCH_LOCK = (0x36, 0x02)
GET_REMOTE_LOCK = (0x37, 0x02)
GET_STANDBY_TIMER = (0x39, 0x04)
GET_SLEEP_TIMER = (0x3A, 0x04)
GET_AUTO_BRIGHTNESS = (0x34, 0x0F)
GET_BACKLIGHT_MODE = (0x34, 0x12)

# Identity, text replies (hexadecimal ASCII).
GET_SCALER_FW = (0x03, 0x01)
GET_TOUCH_FW = (0x03, 0x02)
GET_SERIAL = (0x03, 0x04)
GET_MODEL = (0x03, 0x05)
GET_MCU_FW = (0x03, 0x07)
_TEXT_GETS = {
    GET_SCALER_FW: "firmware_version",
    GET_TOUCH_FW: "touch_firmware_version",
    GET_SERIAL: "serial_number",
    GET_MODEL: "model",
    GET_MCU_FW: "mcu_firmware_version",
}

GET_POWER_ON_TIME = (0x3D, 0x01)

# Network (the panel's own interface: the one this driver reaches over IP).
GET_NETWORK_MODE = (0x20, 0x02)
GET_MAC = (0x20, 0x09)
_ADDRESS_GETS = {
    # mode byte -> (ip, gateway, mask) gets; 00 = DHCP, 01 = static.
    0x00: ((0x20, 0x0B), (0x20, 0x0C), (0x20, 0x0D)),
    0x01: ((0x20, 0x04), (0x20, 0x06), (0x20, 0x08)),
}
_ADDRESS_KEYS = {}
for _ip, _gw, _mask in _ADDRESS_GETS.values():
    _ADDRESS_KEYS[_ip] = "ip_address"
    _ADDRESS_KEYS[_gw] = "gateway"
    _ADDRESS_KEYS[_mask] = "subnet_mask"

# Everything polled while the panel is on, after the power read.
_POLL_GETS = [
    GET_INPUT, GET_VOLUME, GET_MUTE, GET_FREEZE, GET_BLANK,
    GET_TOUCH_LOCK, GET_REMOTE_LOCK, GET_OPS_INSTALLED, GET_OPS_POWER,
    *((0x34, get_type) for get_type, _set_type in PICTURE_LEVELS.values()),
    GET_AUTO_BRIGHTNESS, GET_BACKLIGHT_MODE,
    GET_STANDBY_TIMER, GET_SLEEP_TIMER,
    *((0x55, index) for index in PANEL_BUTTONS.values()),
]

# Camera (ActivPanel 10 Premium with the Promethean ePTZ 4K camera): whole
# command bodies as the article gives them.
CAMERA_BODIES = {
    "camera_on": bytes.fromhex("80 01 81 0A 11 55 01 FF"),
    "camera_off": bytes.fromhex("80 01 81 0A 11 55 00 FF"),
    "camera_reset": bytes.fromhex("80 01 81 0B 01 AA FF"),
}

#: Model numbers this driver refuses, with the panel they name.
_OTHER_PANELS = (
    ("AP9", "an ActivPanel 9"),
    ("APLE", "an ActivPanel LE"),
    ("APLX", "an ActivPanel LX"),
)


def _text(data: bytes) -> str:
    return "".join(chr(b) for b in data if 32 <= b < 127).strip()


class PrometheanActivPanel10Driver(BaseDriver):
    """Promethean ActivPanel 10 / 10 Premium RS232 control codes driver."""

    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = (
        "The panel stopped answering. A panel in Sleep turns its network "
        "off; Power On wakes it over Wake-on-LAN."
    )

    DRIVER_INFO = {
        "id": "promethean_activpanel10",
        "name": "Promethean ActivPanel 10",
        "manufacturer": "Promethean",
        "category": "display",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls the Promethean ActivPanel 10 and ActivPanel 10 Premium "
            "interactive displays over the network (TCP port 5000) or RS-232. "
            "Power, sleep and Wake-on-LAN, input select, volume and mute, "
            "freeze, blank, touch and remote locks, OPS computer power, "
            "picture levels, backlight mode, standby and sleep timers, "
            "front-panel button locks and the ePTZ camera. Polls for status."
        ),
        "source_url": "https://support.prometheanworld.com/s/article/2752",
        "tags": ["display", "promethean", "activpanel", "ifp", "interactive-display", "rs232", "lan-control", "education"],
        "verified": False,
        "simulated": True,
        "ports": [5000],
        "min_platform_version": "0.37.0",
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "compatible_models": [
            {
                "manufacturer": "Promethean",
                "models": ["AP10-A65", "AP10-A75", "AP10-A86"],
                "confidence": "untested",
                "notes": "ActivPanel 10. Network control on the panel's own LAN port, TCP 5000, or RS-232 at 9600 8N1.",
            },
            {
                "manufacturer": "Promethean",
                "models": ["AP10-B55", "AP10-B65", "AP10-B75", "AP10-B86"],
                "confidence": "untested",
                "notes": "ActivPanel 10 Premium. Same control codes; the camera commands are for the Promethean ePTZ 4K camera in the Premium's camera mount.",
            },
        ],
        "help": {
            "overview": (
                "Promethean ActivPanel 10 and 10 Premium interactive displays. "
                "Power, input, volume, mute, freeze, blank, touch and remote "
                "locks, the OPS computer's power, picture levels and timers. "
                "Polls every 15 seconds. Over the network, Power Off puts the "
                "panel to Sleep, which turns its network off: it shows offline "
                "until Power On wakes it with Wake-on-LAN."
            ),
            "setup": (
                "1. Plug a network cable into the panel's own LAN port (not "
                "the OPS computer's), and give the panel a static IP address "
                "or a DHCP reservation. The address is under Settings > "
                "System > Network.\n"
                "2. OpenAVC must be on the same subnet as the panel: its "
                "network control does not cross subnets.\n"
                "3. Turn on Settings > Power > Wake on LAN, so Power On can "
                "wake a sleeping panel.\n"
                "4. Network control uses TCP port 5000 and needs no username "
                "or password.\n"
                "5. Serial control instead: a straight RS-232 cable to the "
                "panel's RS232 port, 9600 baud, 8 data bits, no parity, 1 "
                "stop bit. Standby and Power On work over serial without "
                "Wake-on-LAN."
            ),
            "connection": (
                "A panel in Sleep does not answer on the network. Power On "
                "wakes it with Wake-on-LAN (Settings > Power > Wake on LAN on, "
                "and the panel's MAC address known), and the connection comes "
                "back on its own. Check that OpenAVC is on the panel's subnet "
                "and that the cable is in the panel's LAN port, not the OPS."
            ),
        },
        "discovery": {
            # Get Model Number on TCP 5000 answers F6 03 05 <model text> CS 6F.
            # The model text is what tells an ActivPanel 10 from an
            # ActivPanel 9 on the same port. A sleeping panel has its network
            # off and is not found.
            "tcp_probe": {
                "port": 5000,
                "send_hex": "F6 03 05 00 FE 6F",
                "expect_regex": "\\xf6\\x03\\x05[ -~]*(?:AP10|ActivPanel ?10)",
                "extract": {
                    # The model text, up to the checksum byte and the end code ('o').
                    "model": {"regex": "\\xf6\\x03\\x05((?:AP10|ActivPanel ?10)[ -~]*?)[\\x00-\\xff]o", "group": 1},
                },
                "timeout_ms": 2000,
            },
            "oui": ["6c:dc:6a"],
            "manufacturer_alias": ["promethean", "promethean limited"],
        },
        "default_config": {
            "host": "",
            "port": 5000,
            "poll_interval": 15,
            "baudrate": 9600,
            "parity": "N",
            "bytesize": 8,
            "stopbits": 1,
            "mac_address": "",
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "The panel's IP address, under Settings > System > Network.",
            },
            "port": {
                "type": "integer", "default": 5000, "label": "TCP Port",
                "description": "5000 for ActivPanel network control (fixed on the panel).",
            },
            "poll_interval": {
                "type": "integer", "default": 15, "min": 5,
                "label": "Poll Interval (sec)",
            },
            "baudrate": {
                "type": "integer", "default": 9600, "label": "Baud Rate (serial)",
                "description": "9600 for the ActivPanel 10's RS-232 port.",
            },
            "mac_address": {
                "type": "string", "default": "", "label": "MAC Address (for Wake-on-LAN)",
                "learned_from": "mac_address",
                "description": "Filled in from the panel when it connects, so Power On can wake it from Sleep after a restart. Fill it in yourself (Settings > System > Network on the panel) only for a panel that has never connected.",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum", "values": ["on", "standby", "sleep"],
                "label": "Power", "control": True,
                "help": "on, standby, or sleep (network off; only reported over serial, since a sleeping panel is offline on the network).",
            },
            "input": {
                "type": "enum",
                "values": ["usb_c_1", "usb_c_2", "hdmi_1", "hdmi_2", "displayport", "ops", "other"],
                "label": "Input", "control": True,
                "help": "Active input. 'other' is a code this driver does not know.",
            },
            "volume": {
                "type": "integer", "label": "Volume", "control": True,
                "min": 0, "max": 100, "step": 1,
                "help": "Speaker volume, 0-100.",
            },
            "mute": {"type": "boolean", "label": "Mute", "control": True, "help": "Audio mute."},
            "freeze": {"type": "boolean", "label": "Freeze", "control": True, "help": "Picture frozen."},
            "blank": {"type": "boolean", "label": "Blank", "control": True, "help": "Screen blanked."},
            "touch_lock": {
                "type": "boolean", "label": "Touch Lock", "control": True,
                "help": "Touch input locked.",
            },
            "remote_lock": {
                "type": "boolean", "label": "Remote Control Lock",
                "help": "The panel's IR remote locked out.",
            },
            "ops_installed": {
                "type": "boolean", "label": "OPS Installed",
                "help": "An OPS computer is in the panel's OPS slot.",
            },
            "ops_power": {
                "type": "boolean", "label": "OPS Power", "control": True,
                "help": "The OPS computer is on.",
            },
            "contrast": {
                "type": "integer", "label": "Contrast", "min": 0, "max": 100, "step": 1,
                "help": "Picture contrast, 0-100.",
            },
            "brightness": {
                "type": "integer", "label": "Brightness", "min": 0, "max": 100, "step": 1,
                "help": "Picture brightness, 0-100.",
            },
            "sharpness": {
                "type": "integer", "label": "Sharpness", "min": 0, "max": 100, "step": 1,
                "help": "Picture sharpness, 0-100.",
            },
            "backlight": {
                "type": "integer", "label": "Backlight", "control": True,
                "min": 0, "max": 100, "step": 1,
                "help": "Backlight level, 0-100.",
            },
            "auto_brightness": {
                "type": "boolean", "label": "Automatic Brightness",
                "help": "The panel sets its backlight from its ambient light sensor.",
            },
            "backlight_mode": {
                "type": "enum", "values": ["eco", "standard", "theater"],
                "label": "Backlight Mode",
                "help": "The panel's backlight mode (Eco, Standard or Theater).",
            },
            "standby_timer": {
                "type": "enum",
                "values": ["3_min", "10_min", "30_min", "1_hour", "4_hours", "8_hours", "12_hours", "never"],
                "label": "Standby Timer",
                "help": "Idle time before the panel goes to standby.",
            },
            "sleep_timer": {
                "type": "enum",
                "values": ["3_min", "10_min", "30_min", "1_hour", "4_hours", "8_hours", "12_hours", "never"],
                "label": "Sleep Timer",
                "help": "Idle time before the panel goes to sleep (network off).",
            },
            "power_button_enabled": {"type": "boolean", "label": "Power Button Enabled", "help": "The front-panel power button works."},
            "source_button_enabled": {"type": "boolean", "label": "Source Button Enabled", "help": "The front-panel source button works."},
            "volume_button_enabled": {"type": "boolean", "label": "Volume Buttons Enabled", "help": "The front-panel volume buttons work."},
            "flame_button_enabled": {"type": "boolean", "label": "Flame Button Enabled", "help": "The front-panel flame (menu) button works."},
            "freeze_button_enabled": {"type": "boolean", "label": "Freeze Button Enabled", "help": "The front-panel freeze button works."},
            "touch_button_enabled": {"type": "boolean", "label": "Touch Button Enabled", "help": "The front-panel touch on/off button works."},
            "model": {"type": "string", "label": "Model", "help": "Model number the panel reports."},
            "serial_number": {"type": "string", "label": "Serial Number", "help": "Serial number the panel reports."},
            "firmware_version": {"type": "string", "label": "Firmware Version", "help": "Scaler (main board) firmware version."},
            "mcu_firmware_version": {"type": "string", "label": "MCU Firmware Version", "help": "MCU firmware version."},
            "touch_firmware_version": {"type": "string", "label": "Touch Firmware Version", "help": "Touch (bezel) firmware version."},
            "power_on_time": {
                "type": "string", "label": "Power On Time",
                "help": "The six values the panel reports for its power-on time (year, month, day, hour, minute, second), as yy-mm-dd hh:mm:ss.",
            },
            "network_mode": {
                "type": "enum", "values": ["dhcp", "static"],
                "label": "Network Mode", "help": "How the panel gets its IP address.",
            },
            "ip_address": {"type": "string", "label": "IP Address", "help": "The panel's IP address."},
            "subnet_mask": {"type": "string", "label": "Subnet Mask", "help": "The panel's subnet mask."},
            "gateway": {"type": "string", "label": "Gateway", "help": "The panel's default gateway."},
            "mac_address": {
                "type": "string", "label": "MAC Address",
                "help": "The panel's network MAC address; Power On sends its Wake-on-LAN packet here.",
            },
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "help": "Turns the panel on. Sends a Wake-on-LAN packet to the panel's MAC address, which wakes a sleeping panel, and the Power On command as well while the panel is reachable. Runs while the panel is offline.",
                "available_offline": True,
                "sets": {"power": "on"},
                "params": {},
            },
            "power_off": {
                "label": "Power Off",
                "help": "Turns the panel off: Standby over a serial connection, Sleep over the network. A sleeping panel turns its network off and shows offline until Power On wakes it.",
                "params": {},
            },
            "standby": {
                "label": "Standby",
                "help": "Puts the panel in standby. Works over a serial connection only; over the network use Power Off (Sleep).",
                "params": {},
            },
            "sleep": {
                "label": "Sleep",
                "help": "Puts the panel to sleep, which turns its network off. Power On wakes it with Wake-on-LAN.",
                "params": {},
            },
            "set_input": {
                "label": "Select Input",
                "help": "Switches the panel to an input.",
                "sets": {"input": "{input}"},
                "params": {
                    "input": {
                        "type": "enum", "required": True, "label": "Input",
                        "values": [
                            {"value": "hdmi_1", "label": "HDMI 1"},
                            {"value": "hdmi_2", "label": "HDMI 2"},
                            {"value": "usb_c_1", "label": "USB-C 1"},
                            {"value": "usb_c_2", "label": "USB-C 2"},
                            {"value": "displayport", "label": "DisplayPort"},
                            {"value": "ops", "label": "OPS"},
                        ],
                    },
                },
            },
            "set_volume": {
                "label": "Set Volume",
                "help": "Sets the speaker volume.",
                "sets": {"volume": "{level}"},
                "params": {"level": {"type": "integer", "required": True, "label": "Volume (0-100)", "min": 0, "max": 100}},
            },
            "volume_up": {"label": "Volume Up", "help": "Raises the volume one step.", "params": {}},
            "volume_down": {"label": "Volume Down", "help": "Lowers the volume one step.", "params": {}},
            "mute_on": {"label": "Mute On", "help": "Mutes the panel's audio.", "sets": {"mute": True}, "params": {}},
            "mute_off": {"label": "Mute Off", "help": "Unmutes the panel's audio.", "sets": {"mute": False}, "params": {}},
            "freeze_on": {"label": "Freeze On", "help": "Freezes the picture.", "sets": {"freeze": True}, "params": {}},
            "freeze_off": {"label": "Freeze Off", "help": "Unfreezes the picture.", "sets": {"freeze": False}, "params": {}},
            "blank_on": {"label": "Blank On", "help": "Blanks the screen.", "sets": {"blank": True}, "params": {}},
            "blank_off": {"label": "Blank Off", "help": "Shows the picture again.", "sets": {"blank": False}, "params": {}},
            "touch_lock_on": {"label": "Touch Lock On", "help": "Locks touch input on the screen.", "sets": {"touch_lock": True}, "params": {}},
            "touch_lock_off": {"label": "Touch Lock Off", "help": "Unlocks touch input.", "sets": {"touch_lock": False}, "params": {}},
            "remote_lock_on": {"label": "Remote Lock On", "help": "Locks out the panel's IR remote.", "sets": {"remote_lock": True}, "params": {}},
            "remote_lock_off": {"label": "Remote Lock Off", "help": "Lets the panel's IR remote work again.", "sets": {"remote_lock": False}, "params": {}},
            "ops_power_on": {"label": "OPS Power On", "help": "Turns the OPS computer on.", "sets": {"ops_power": True}, "params": {}},
            "ops_power_off": {"label": "OPS Power Off", "help": "Turns the OPS computer off.", "sets": {"ops_power": False}, "params": {}},
            "set_backlight": {
                "label": "Set Backlight",
                "help": "Sets the backlight level.",
                "sets": {"backlight": "{level}"},
                "params": {"level": {"type": "integer", "required": True, "label": "Backlight (0-100)", "min": 0, "max": 100}},
            },
            "set_brightness": {
                "label": "Set Brightness",
                "help": "Sets the picture brightness.",
                "sets": {"brightness": "{level}"},
                "params": {"level": {"type": "integer", "required": True, "label": "Brightness (0-100)", "min": 0, "max": 100}},
            },
            "set_contrast": {
                "label": "Set Contrast",
                "help": "Sets the picture contrast.",
                "sets": {"contrast": "{level}"},
                "params": {"level": {"type": "integer", "required": True, "label": "Contrast (0-100)", "min": 0, "max": 100}},
            },
            "set_sharpness": {
                "label": "Set Sharpness",
                "help": "Sets the picture sharpness.",
                "sets": {"sharpness": "{level}"},
                "params": {"level": {"type": "integer", "required": True, "label": "Sharpness (0-100)", "min": 0, "max": 100}},
            },
            "enable_all_buttons": {
                "label": "Enable All Front Buttons",
                "help": "Lets every front-panel button work.",
                "params": {},
            },
            "disable_all_buttons": {
                "label": "Disable All Front Buttons",
                "help": "Stops every front-panel button working. The IR remote's buttons still work.",
                "params": {},
            },
            "camera_on": {
                "label": "Camera On",
                "help": "Turns on the Promethean ePTZ 4K camera (ActivPanel 10 Premium).",
                "params": {},
            },
            "camera_off": {
                "label": "Camera Off",
                "help": "Turns off the Promethean ePTZ 4K camera (ActivPanel 10 Premium).",
                "params": {},
            },
            "camera_reset": {
                "label": "Reset Camera",
                "help": "Resets the Promethean ePTZ 4K camera (ActivPanel 10 Premium).",
                "confirm": "Resets the panel's ePTZ camera.",
                "params": {},
            },
            "picture_reset": {
                "label": "Picture Reset",
                "help": "Puts the picture settings back to their factory values.",
                "confirm": "Puts every picture setting back to its factory value.",
                "params": {},
            },
            "factory_reset": {
                "label": "Factory Reset",
                "help": "Returns the panel to its factory settings.",
                "confirm": "Erases the panel's settings and returns it to factory defaults.",
                "params": {},
            },
            "send_raw": {
                "label": "Send Raw Command",
                "help": "Sends one control code. Give the command, type and data bytes in hex (for example 32 01 01 for Freeze On); the driver adds the head, checksum and end bytes.",
                "params": {
                    "body": {
                        "type": "string", "required": True, "label": "Command bytes (hex)",
                        "pattern": "^\\s*[0-9A-Fa-f]{2}(?:\\s*[0-9A-Fa-f]{2}){2,30}\\s*$",
                    },
                },
            },
        },
        "device_settings": {
            "contrast": {
                "type": "integer", "min": 0, "max": 100, "label": "Contrast",
                "help": "Picture contrast, 0-100.",
                "state_key": "contrast", "default": 50, "setup": False,
            },
            "brightness": {
                "type": "integer", "min": 0, "max": 100, "label": "Brightness",
                "help": "Picture brightness, 0-100.",
                "state_key": "brightness", "default": 50, "setup": False,
            },
            "sharpness": {
                "type": "integer", "min": 0, "max": 100, "label": "Sharpness",
                "help": "Picture sharpness, 0-100.",
                "state_key": "sharpness", "default": 50, "setup": False,
            },
            "backlight": {
                "type": "integer", "min": 0, "max": 100, "label": "Backlight",
                "help": "Backlight level, 0-100.",
                "state_key": "backlight", "default": 80, "setup": False,
            },
            "auto_brightness": {
                "type": "boolean", "label": "Automatic Brightness",
                "help": "Let the panel set its backlight from its ambient light sensor.",
                "state_key": "auto_brightness", "default": True, "setup": False,
            },
            "backlight_mode": {
                "type": "enum",
                "values": [
                    {"value": "eco", "label": "Eco"},
                    {"value": "standard", "label": "Standard"},
                    {"value": "theater", "label": "Theater"},
                ],
                "label": "Backlight Mode",
                "help": "The panel's backlight mode.",
                "state_key": "backlight_mode", "default": "eco", "setup": False,
            },
            "standby_timer": {
                "type": "enum",
                "values": [
                    {"value": "3_min", "label": "3 minutes"},
                    {"value": "10_min", "label": "10 minutes"},
                    {"value": "30_min", "label": "30 minutes"},
                    {"value": "1_hour", "label": "1 hour"},
                    {"value": "4_hours", "label": "4 hours"},
                    {"value": "8_hours", "label": "8 hours"},
                    {"value": "12_hours", "label": "12 hours"},
                    {"value": "never", "label": "Never"},
                ],
                "label": "Standby Timer",
                "help": "Idle time before the panel goes to standby. Promethean notes that Never, or running the panel more than 20 hours a day, may shorten its life.",
                "state_key": "standby_timer", "default": "3_min", "setup": False,
            },
            "sleep_timer": {
                "type": "enum",
                "values": [
                    {"value": "3_min", "label": "3 minutes"},
                    {"value": "10_min", "label": "10 minutes"},
                    {"value": "30_min", "label": "30 minutes"},
                    {"value": "1_hour", "label": "1 hour"},
                    {"value": "4_hours", "label": "4 hours"},
                    {"value": "8_hours", "label": "8 hours"},
                    {"value": "12_hours", "label": "12 hours"},
                    {"value": "never", "label": "Never"},
                ],
                "label": "Sleep Timer",
                "help": "Idle time before the panel goes to sleep, which turns its network off. Promethean notes that Never, or running the panel more than 20 hours a day, may shorten its life.",
                "state_key": "sleep_timer", "default": "3_min", "setup": False,
            },
            "power_button": {
                "type": "boolean", "label": "Power Button",
                "help": "The front-panel power button works. The IR remote's power button works either way.",
                "state_key": "power_button_enabled", "default": True, "setup": False,
            },
            "source_button": {
                "type": "boolean", "label": "Source Button",
                "help": "The front-panel source button works.",
                "state_key": "source_button_enabled", "default": True, "setup": False,
            },
            "volume_button": {
                "type": "boolean", "label": "Volume Buttons",
                "help": "The front-panel volume buttons work.",
                "state_key": "volume_button_enabled", "default": True, "setup": False,
            },
            "flame_button": {
                "type": "boolean", "label": "Flame Button",
                "help": "The front-panel flame (menu) button works.",
                "state_key": "flame_button_enabled", "default": True, "setup": False,
            },
            "freeze_button": {
                "type": "boolean", "label": "Freeze Button",
                "help": "The front-panel freeze button works.",
                "state_key": "freeze_button_enabled", "default": True, "setup": False,
            },
            "touch_button": {
                "type": "boolean", "label": "Touch Button",
                "help": "The front-panel touch on/off button works.",
                "state_key": "touch_button_enabled", "default": True, "setup": False,
            },
        },
        "quick_actions": ["power_on", "power_off", "mute_on", "mute_off"],
        "protocols": ["promethean_rs232"],
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any):
        super().__init__(device_id, config, state, events)
        self._waiters: dict[tuple[int, int], list[asyncio.Future]] = {}
        # Gets this panel left unanswered while it was on, this connection,
        # counted so one slow reply does not drop a value for good.
        self._misses: dict[tuple[int, int], int] = {}
        self._model_checked = False

    # ── Framing hooks ────────────────────────────────────────────────────────

    def _create_frame_parser(self) -> Optional[FrameParser]:
        return CallableFrameParser(parse_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        return None

    # ── Send / request ───────────────────────────────────────────────────────

    def _over_network(self) -> bool:
        return self._effective_transport() != "serial"

    async def _send(self, body: bytes) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(build_frame(body))

    async def _request(self, key: tuple[int, int], timeout: float | None = None) -> bytes:
        """Send a get and wait for the reply carrying the same command and type."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, []).append(fut)
        try:
            await self._send(bytes([key[0], key[1], 0x00]))
            return await asyncio.wait_for(fut, timeout or REPLY_TIMEOUT_S)
        finally:
            waiters = self._waiters.get(key, [])
            if fut in waiters:
                waiters.remove(fut)

    async def _read(self, key: tuple[int, int]) -> bytes | None:
        """A get whose silence is not a fault: None when it goes unanswered."""
        try:
            return await self._request(key)
        except asyncio.TimeoutError:
            return None

    async def _set(self, body: bytes, read_back: tuple[int, int] | None = None) -> bool:
        """Send a set, then read the value back so state follows the panel."""
        await self._send(body)
        if read_back is not None:
            await self._read(read_back)
        return True

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _initial_sync(self) -> None:
        self._misses.clear()
        self._model_checked = False
        await self._check_model()
        await self._read_identity()
        if not self.config.get("poll_interval", 0) > 0:
            await self.poll()

    async def _check_model(self) -> None:
        """Ask the model, and refuse a panel this driver is not for.

        An ActivPanel 9 answers the same frames on the same port, and its
        power-status query can lock the panel's power up (Promethean article
        2586), so the model is settled before the first power poll whenever
        the panel answers it.
        """
        model = await self._read(GET_MODEL)
        if model is None:
            log.info(f"[{self.device_id}] The panel did not report its model")
            return
        self._model_checked = True
        text = _text(model)
        for prefix, name in _OTHER_PANELS:
            if text.upper().startswith(prefix):
                message = (
                    f"This panel reports model {text}, which is {name}. This "
                    f"driver is for the ActivPanel 10: choose the driver for "
                    f"your panel."
                )
                if ConnectionFaultError is not None:
                    raise ConnectionFaultError(message, code="invalid_config")
                raise ConnectionError(message)

    async def _read_identity(self) -> None:
        """Serial number, firmware, power-on time and network: read once a
        connection, and again the first time the panel is on if a panel in
        standby left them unanswered."""
        for key in (GET_SERIAL, GET_SCALER_FW, GET_MCU_FW, GET_TOUCH_FW, GET_POWER_ON_TIME):
            await self._read(key)
        mode = await self._read(GET_NETWORK_MODE)
        if mode:
            for key in _ADDRESS_GETS.get(mode[0], ()):
                await self._read(key)
        await self._read(GET_MAC)

    async def poll(self) -> None:
        if not self._model_checked:
            await self._check_model()
        # Power first: the one question a panel answers in every state the
        # connection survives. Its silence means the link is gone.
        was_on = self.get_state("power") == "on"
        await self._request(GET_POWER)
        if self.get_state("power") != "on":
            return
        if not was_on and not self.get_state("serial_number"):
            await self._read_identity()
        for key in _POLL_GETS:
            if self._misses.get(key, 0) >= 2:
                continue
            if await self._read(key) is None:
                self._misses[key] = self._misses.get(key, 0) + 1
                if self._misses[key] == 2:
                    log.info(
                        f"[{self.device_id}] No reply to get {key[0]:02X} {key[1]:02X} "
                        f"twice; not asked again until the panel next comes on"
                    )
            else:
                self._misses.pop(key, None)

    async def _liveness_probe(self) -> None:
        await self._request(GET_POWER)

    # ── Receive ──────────────────────────────────────────────────────────────

    async def on_data_received(self, data: bytes) -> None:
        parts = frame_parts(data)
        if parts is None:
            return
        command, ftype, payload = parts
        key = (command, ftype)
        self._apply_reply(key, payload)
        # Every waiter on this question gets the answer: a poll and a
        # command's read-back can ask the same get at once.
        for fut in self._waiters.get(key, []):
            if not fut.done():
                fut.set_result(payload)

    def _apply_reply(self, key: tuple[int, int], data: bytes) -> None:
        if not data:
            return
        value = data[0]
        if key == GET_POWER:
            state = POWER_STATES.get(value)
            if state:
                if state == "on" and self.get_state("power") != "on":
                    # Ask everything again: a panel coming on may answer
                    # what it skipped in standby.
                    self._misses.clear()
                self.set_state("power", state)
        elif key == GET_INPUT:
            self.set_state("input", INPUT_NAMES.get(value, "other"))
        elif key == GET_VOLUME:
            self.set_state("volume", value)
        elif key == GET_MUTE:
            self.set_state("mute", value == 0x01)
        elif key == GET_FREEZE:
            self.set_state("freeze", value == 0x01)
        elif key == GET_BLANK:
            self.set_state("blank", value == 0x01)
        elif key == GET_TOUCH_LOCK:
            # The article: "00 = Touch Lock On, 01 = Touch Lock Off".
            self.set_state("touch_lock", value == 0x00)
        elif key == GET_REMOTE_LOCK:
            self.set_state("remote_lock", value == 0x01)
        elif key == GET_OPS_INSTALLED:
            self.set_state("ops_installed", value == 0x01)
        elif key == GET_OPS_POWER:
            self.set_state("ops_power", value == 0x01)
        elif key == GET_AUTO_BRIGHTNESS:
            self.set_state("auto_brightness", value == 0x01)
        elif key == GET_BACKLIGHT_MODE:
            name = BACKLIGHT_MODE_NAMES.get(value)
            if name:
                self.set_state("backlight_mode", name)
        elif key == GET_STANDBY_TIMER:
            name = TIMER_NAMES.get(value)
            if name:
                self.set_state("standby_timer", name)
        elif key == GET_SLEEP_TIMER:
            name = TIMER_NAMES.get(value)
            if name:
                self.set_state("sleep_timer", name)
        elif key[0] == 0x34 and key[1] in {g for g, _s in PICTURE_LEVELS.values()}:
            for name, (get_type, _set_type) in PICTURE_LEVELS.items():
                if get_type == key[1]:
                    self.set_state(name, value)
        elif key[0] == 0x55 and key[1] in PANEL_BUTTONS.values():
            for name, index in PANEL_BUTTONS.items():
                if index == key[1] and value in (0x01, 0x02):
                    self.set_state(f"{name}_enabled", value == 0x01)
        elif key in _TEXT_GETS:
            text = _text(data)
            if text:
                self.set_state(_TEXT_GETS[key], text)
        elif key == GET_POWER_ON_TIME and len(data) >= 6:
            yy, mo, dd, hh, mi, ss = data[:6]
            self.set_state("power_on_time", f"{yy:02d}-{mo:02d}-{dd:02d} {hh:02d}:{mi:02d}:{ss:02d}")
        elif key == GET_NETWORK_MODE:
            if value in (0x00, 0x01):
                self.set_state("network_mode", "static" if value == 0x01 else "dhcp")
        elif key in _ADDRESS_KEYS and len(data) >= 4:
            self.set_state(_ADDRESS_KEYS[key], ".".join(str(b) for b in data[:4]))
        elif key == GET_MAC and len(data) >= 6:
            mac = ":".join(f"{b:02x}" for b in data[:6])
            if mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
                self.set_state("mac_address", mac)

    # ── Commands ─────────────────────────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        if command == "power_on":
            return await self._power_on()
        if command == "power_off":
            if self._over_network():
                return await self._sleep()
            return await self._set(bytes([0x01, 0x01, 0x00]), GET_POWER)
        if command == "standby":
            if self._over_network():
                raise ValueError(
                    "Standby works over a serial connection only. Over the "
                    "network, use Power Off, which puts the panel to Sleep."
                )
            return await self._set(bytes([0x01, 0x01, 0x00]), GET_POWER)
        if command == "sleep":
            return await self._sleep()

        simple = {
            "volume_up": (bytes([0x0C, 0x00, 0x01]), None),
            "volume_down": (bytes([0x0C, 0x00, 0x00]), None),
            "mute_on": (bytes([0x02, 0x00, 0x01]), GET_MUTE),
            "mute_off": (bytes([0x02, 0x00, 0x00]), GET_MUTE),
            "freeze_on": (bytes([0x32, 0x01, 0x01]), GET_FREEZE),
            "freeze_off": (bytes([0x32, 0x01, 0x02]), GET_FREEZE),
            "blank_on": (bytes([0x35, 0x01, 0x00]), GET_BLANK),
            "blank_off": (bytes([0x35, 0x01, 0x01]), GET_BLANK),
            "touch_lock_on": (bytes([0x36, 0x01, 0x00]), GET_TOUCH_LOCK),
            "touch_lock_off": (bytes([0x36, 0x01, 0x01]), GET_TOUCH_LOCK),
            "remote_lock_on": (bytes([0x37, 0x01, 0x00]), GET_REMOTE_LOCK),
            "remote_lock_off": (bytes([0x37, 0x01, 0x01]), GET_REMOTE_LOCK),
            "ops_power_on": (bytes([0x31, 0x01, 0x01]), None),
            "ops_power_off": (bytes([0x31, 0x01, 0x02]), None),
            "enable_all_buttons": (bytes([0x54, 0x00, 0x01]), None),
            "disable_all_buttons": (bytes([0x54, 0x00, 0x02]), None),
            "picture_reset": (bytes([0x34, 0x0D, 0x00]), None),
            "factory_reset": (bytes([0x03, 0x06, 0x00]), None),
        }
        # Volume steps, the OPS (it takes a while to boot or shut down) and
        # the resets are left to the next poll rather than read back at once.
        if command in simple:
            body, read_back = simple[command]
            return await self._set(body, read_back)

        if command in CAMERA_BODIES:
            await self._send(CAMERA_BODIES[command])
            return True

        if command == "set_input":
            name = str(params["input"])
            if name not in INPUT_CODES:
                raise ValueError(f"Unknown input '{name}'")
            return await self._set(bytes([0x30, 0x01, INPUT_CODES[name]]), GET_INPUT)

        if command == "set_volume":
            return await self._set(bytes([0x0C, 0x01, self._level(params["level"])]), GET_VOLUME)

        levels = {
            "set_contrast": "contrast",
            "set_brightness": "brightness",
            "set_sharpness": "sharpness",
            "set_backlight": "backlight",
        }
        if command in levels:
            get_type, set_type = PICTURE_LEVELS[levels[command]]
            return await self._set(bytes([0x34, set_type, self._level(params["level"])]), (0x34, get_type))

        if command == "send_raw":
            body = bytes.fromhex(str(params["body"]))
            if len(body) < 3:
                raise ValueError("Give at least the command, type and data bytes")
            await self._send(body)
            return True

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    @staticmethod
    def _level(value: Any) -> int:
        level = int(value)
        if not 0 <= level <= 100:
            raise ValueError("Level must be 0-100")
        return level

    async def _sleep(self) -> bool:
        if self._over_network() and not self._wake_mac():
            log.warning(
                f"[{self.device_id}] Putting the panel to sleep with no MAC "
                f"address known: Power On cannot wake it over the network"
            )
        # No read-back: over the network the panel turns its network off.
        await self._send(bytes([0x01, 0x05, 0x00]))
        return True

    # ── Device settings ──────────────────────────────────────────────────────

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key in PICTURE_LEVELS:
            get_type, set_type = PICTURE_LEVELS[key]
            return await self._set(bytes([0x34, set_type, self._level(value)]), (0x34, get_type))
        if key == "auto_brightness":
            return await self._set(bytes([0x34, 0x0E, 0x01 if value else 0x00]), GET_AUTO_BRIGHTNESS)
        if key == "backlight_mode":
            return await self._set(bytes([0x34, 0x13, BACKLIGHT_MODES[str(value)]]), GET_BACKLIGHT_MODE)
        if key == "standby_timer":
            return await self._set(bytes([0x39, 0x03, TIMER_CODES[str(value)]]), GET_STANDBY_TIMER)
        if key == "sleep_timer":
            return await self._set(bytes([0x3A, 0x03, TIMER_CODES[str(value)]]), GET_SLEEP_TIMER)
        if key in PANEL_BUTTONS:
            index = PANEL_BUTTONS[key]
            return await self._set(bytes([0x54, index, 0x01 if value else 0x02]), (0x55, index))
        raise ValueError(f"Unknown device setting '{key}'")

    # ── Power On: Wake-on-LAN, then the protocol ─────────────────────────────

    def _wake_mac(self) -> str:
        return (
            str(self.get_state("mac_address") or "").strip()
            or str(self.config.get("mac_address") or "").strip()
        )

    async def _power_on(self) -> bool:
        """Wake a sleeping panel and turn a reachable one on.

        Declared ``available_offline``, so this runs whether or not the panel
        answers. A known MAC gets the Wake-on-LAN magic packet; a live
        connection also gets the Power On frame (which the article supports
        over serial; over the network it is sent too, and Wake-on-LAN is what
        wakes the panel).
        """
        if not self._over_network():
            # Serial: the Power On frame wakes the panel from standby and sleep.
            return await self._set(bytes([0x01, 0x01, 0x01]))
        mac = self._wake_mac()
        woke = False
        not_a_mac = False
        if mac:
            try:
                await self.wake_on_lan(mac)
                woke = True
            except ValueError as exc:
                not_a_mac = True
                log.warning(f"[{self.device_id}] Wake-on-LAN skipped: {exc}")
        if self.transport and self.transport.connected:
            await self._set(bytes([0x01, 0x01, 0x01]))
            return True
        if not_a_mac:
            raise WakeAddressError(
                f"The panel cannot be woken: '{mac}' is not a MAC address "
                f"(six pairs of hex digits, like AA:BB:CC:DD:EE:FF). Correct "
                f"it under Edit Device."
            )
        if not woke:
            raise WakeAddressError(
                "The panel is offline and no MAC address is known, so it "
                "cannot be woken. The MAC is learned automatically on the "
                "first connection; until then, enter it under Edit Device "
                "(the panel shows it under Settings > System > Network), or "
                "turn the panel on at the panel."
            )
        return True
