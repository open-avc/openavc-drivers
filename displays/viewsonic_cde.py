"""
OpenAVC ViewSonic commercial display driver (LFD RS-232 & LAN protocol).

Controls ViewSonic large-format commercial displays (CDE series) over
TCP port 5000 or direct RS-232. The whole ViewSonic LFD line publishes
one protocol grammar ("LFD RS-232 & LAN Protocol Specification");
per-model support varies and an unsupported command answers a '-'
reject, which the driver ignores.

Why Python: the protocol itself is plain framed ASCII, but waking a
display whose standby mode has shut the LAN port down needs a
Wake-on-LAN magic packet (UDP port 9) from an offline-capable setup
action — a YAML driver has no way to send it. Everything else here
fits the declarative format; if a platform WOL/offline-wake primitive
lands, this driver converts to YAML.

Wire protocol (ASCII framing, CR terminated)::

    Set:    [len][ID two digits]['s'][code][value 3 bytes]<CR>
    Get:    [len][ID two digits]['g'][code]['000']<CR>
    Ack:    [len][ID]['+' or '-']<CR>                (set result)
    Reply:  [len][ID]['r'][code][value...]<CR>       (get answer)
    IR:     [len][ID]['p'][key MSB][key LSB]<CR>     (RCU pass-through)

  - The length byte is the ASCII digit of the byte count excluding CR
    ('8' for the standard 9-byte packet). Info replies (device name,
    MAC, IP, serial, firmware, operation hours, smart hub) use the
    fixed "32-byte format": the value is NUL-padded, so the driver
    parses positionally and strips the padding without trusting the
    length byte.
  - The backlight level rides its own command-type pair inherited from
    the color-calibration command set: set type 'A' / get type 'a',
    command code 'B' (the type char distinguishes it from the
    Remote-Control-mode set, which is type 's' code 'B').
  - Monitor ID is 01 for LAN control; RS-232 daisy chains address IDs
    1-98 (99 broadcasts, unused here), so the ID is a config field and
    frames for other IDs are dropped.

Push vs poll: HYBRID. Since protocol 3.2.1 the display auto-replies —
it sends updated power / input / brightness / backlight / volume /
mute frames unsolicited when a user changes them locally. The spec
does not pin the auto-reply frame type, so the driver parses any
'r'-type frame whenever it arrives, which covers the documented data
set; polling refreshes everything else (and covers models predating
auto-reply).

Power model (matters for automation):
  - "001" on, "000" standby. Whether LAN control keeps working in
    standby depends on the display's standby/network setting (see the
    model's user guide) — the spec warns power-on via LAN "may work
    only under specific modes". The "Wake Display (Wake-on-LAN)" setup
    action is the robust wake path: the spec defines WOL by MAC on UDP
    port 9, and the driver learns the MAC on every connection.

No state is fabricated for set-only functions (color mode, picture
size, bass/treble/balance, surround, OSD language have no Get in the
spec) — they ship as commands without read-back.

Protocol reference: ViewSonic "LFD RS-232 & LAN Protocol
Specification" rev 3.3.2 (Dec 2020). Current CDE models publish the
same tables per-model at manuals.viewsonic.com.
"""

from __future__ import annotations

import asyncio
import re
import socket
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# ── Wire tables (LFD RS-232 & LAN Protocol Specification v3.3.2) ────────────

# Set-function command codes (command type 's' unless noted).
SET_POWER = "!"            # 000 standby / 001 on
SET_INPUT = '"'            # three-char source code; 00Z = cycle
SET_CONTRAST = "#"         # 000-100
SET_BRIGHTNESS = "$"       # 000-100; 900 down / 901 up
SET_SHARPNESS = "%"        # 000-100
SET_COLOR = "&"            # 000-100
SET_TINT = "'"             # 000-100
SET_BACKLIGHT_ON = "("     # 000 off / 001 on (screen blank)
SET_COLOR_MODE = ")"       # 000 normal / 001 warm / 002 cold / 003 personal
SET_FREEZE = "*"           # 000 off / 001 on
SET_SURROUND = "-"         # 000 off / 001 on
SET_BASS = "."             # 000-100 (no get)
SET_TREBLE = "/"           # 000-100 (no get)
SET_BALANCE = "0"          # 000-100, 050 central (no get)
SET_PICTURE_SIZE = "1"     # 000 full / 001 normal / 002 real
SET_OSD_LANGUAGE = "2"     # 000 english / 001 french / 002 spanish
SET_POWER_LOCK = "4"       # 000 unlock / 001 lock
SET_VOLUME = "5"           # 000-100; 900 down / 901 up
SET_MUTE = "6"             # 000 off / 001 on
SET_PIP_INPUT = "7"        # source code
SET_BUTTON_LOCK = "8"      # 000 unlock / 001 lock
SET_PIP_MODE = "9"         # 000 off / 001 pip / 002 pbp
SET_PIP_SOUND = ":"        # 000 main / 001 sub
SET_PIP_POSITION = ";"     # 000 up / 001 down / 002 left / 003 right
SET_FUNCTION_ONOFF = "="   # [1/0][function id 01 backlight / 02 freeze / 03 touch]
SET_MENU_LOCK = ">"        # 000 unlock / 001 lock
SET_NUMBER = "@"           # 000-009
SET_KEYPAD = "A"           # nav keys 000-007
SET_RCU_MODE = "B"         # 000 disable / 001 enable / 002 pass-through
SET_TILING_MODE = "P"      # 000 off / 001 on
SET_TILING_COMP = "Q"      # 000 off / 001 on (bezel compensation)
SET_TILING_HV = "R"        # "0" + H digit + V digit
SET_TILING_POSITION = "S"  # 001-025
SET_HOT_KEY = "X"          # 001-999 (model-defined customized hot keys)
SET_RESTORE_DEFAULT = "~"  # 000

# The backlight level's dedicated command-type pair (code 'B').
TYPE_SET_BACKLIGHT = "A"
TYPE_GET_BACKLIGHT = "a"
BACKLIGHT_CODE = "B"

# Get-function command codes (command type 'g' unless noted above).
GET_THERMAL = "0"          # 000-100 degC, -01..-99 below zero
GET_OPERATION_HOURS = "1"  # 32-byte reply, six-digit integer
GET_DEVICE_NAME = "4"      # 32-byte reply
GET_MAC = "5"              # 32-byte reply, 12 hex chars
GET_IP = "6"               # 32-byte reply, dotted quad
GET_SERIAL = "7"           # 32-byte reply
GET_FW = "8"               # 32-byte reply
GET_SMART_HUB = ":"        # 32-byte reply, fixed 6-byte sub-fields
GET_FUNCTION_ONOFF = "="   # send 001/002/003; reply [1/0][function id]
GET_CONTRAST = "a"
GET_BRIGHTNESS = "b"
GET_SHARPNESS = "c"
GET_COLOR = "d"
GET_TINT = "e"
GET_VOLUME = "f"
GET_MUTE = "g"
GET_BACKLIGHT_ON = "h"
GET_FREEZE = "i"
GET_INPUT = "j"            # digit 1 = signal detect, digits 2-3 = source
GET_POWER = "l"            # 001 on / 000 standby
GET_RCU_MODE = "n"
GET_POWER_LOCK = "o"
GET_BUTTON_LOCK = "p"
GET_MENU_LOCK = "q"
GET_PIP_MODE = "t"
GET_PIP_INPUT = "u"
GET_TILING_MODE = "v"
GET_TILING_COMP = "w"
GET_TILING_HV = "x"
GET_TILING_POSITION = "y"  # 000 off / 001-025
GET_ACK = "z"              # communication-link test, replies 000

# Source codes (set '"' / PIP input '7'). The Get-Input reply carries the
# code's last two chars after the signal digit, so a suffix map decodes it.
INPUT_CODES = {
    "004": "hdmi1",
    "014": "hdmi2",
    "024": "hdmi3",
    "034": "hdmi4",
    "009": "dp1",
    "029": "dp2",
    "019": "typec1",
    "039": "typec2",
    "007": "ops",
    "00A": "android",
    "006": "vga1",
    "016": "vga2",
    "026": "vga3",
    "005": "dvi",
    "008": "internal",
    "003": "ypbpr",
    "001": "av",
    "002": "svideo",
    "000": "tv",
}
INPUT_TOKENS = {v: k for k, v in INPUT_CODES.items()}
INPUT_BY_SUFFIX = {code[1:]: token for code, token in INPUT_CODES.items()}
INPUT_LABELS = [
    {"value": "hdmi1", "label": "HDMI 1"},
    {"value": "hdmi2", "label": "HDMI 2"},
    {"value": "hdmi3", "label": "HDMI 3"},
    {"value": "hdmi4", "label": "HDMI 4"},
    {"value": "dp1", "label": "DisplayPort 1"},
    {"value": "dp2", "label": "DisplayPort 2"},
    {"value": "typec1", "label": "USB-C 1"},
    {"value": "typec2", "label": "USB-C 2"},
    {"value": "ops", "label": "Slot-in PC (OPS/HDBT)"},
    {"value": "android", "label": "Embedded Android"},
    {"value": "vga1", "label": "VGA 1"},
    {"value": "vga2", "label": "VGA 2"},
    {"value": "vga3", "label": "VGA 3"},
    {"value": "dvi", "label": "DVI"},
    {"value": "internal", "label": "Internal Memory"},
    {"value": "ypbpr", "label": "YPbPr Component"},
    {"value": "av", "label": "AV"},
    {"value": "svideo", "label": "S-Video"},
    {"value": "tv", "label": "TV Tuner"},
]

RCU_MODES = {"000": "disabled", "001": "enabled", "002": "passthrough"}
RCU_MODE_TOKENS = {v: k for k, v in RCU_MODES.items()}

PIP_MODES = {"000": "off", "001": "pip", "002": "pbp"}
PIP_MODE_TOKENS = {v: k for k, v in PIP_MODES.items()}

COLOR_MODES = {"normal": "000", "warm": "001", "cold": "002", "personal": "003"}
PICTURE_SIZES = {"full": "000", "normal": "001", "real": "002"}
OSD_LANGUAGES = {"english": "000", "french": "001", "spanish": "002"}
PIP_SOUNDS = {"main": "000", "sub": "001"}
PIP_POSITIONS = {"up": "000", "down": "001", "left": "002", "right": "003"}
NAV_KEYS = {
    "up": "000", "down": "001", "left": "002", "right": "003",
    "enter": "004", "input": "005", "menu": "006", "exit": "007",
}

# Function On_Off ids ('=' set/get) -> the boolean state each one feeds.
FUNCTION_IDS = {"01": "backlight_on", "02": "freeze", "03": "touch_enabled"}

# Get code -> state key for plain 0-100 numeric replies.
_NUMERIC_GETS = {
    GET_VOLUME: "volume",
    GET_BRIGHTNESS: "brightness",
    GET_CONTRAST: "contrast",
    GET_SHARPNESS: "sharpness",
    GET_COLOR: "color",
    GET_TINT: "tint",
    BACKLIGHT_CODE: "backlight",
}

# Get code -> state key for 32-byte NUL-padded text replies.
_TEXT_GETS = {
    GET_DEVICE_NAME: "device_name",
    GET_IP: "ip_address",
    GET_SERIAL: "serial_number",
    GET_FW: "firmware_version",
}

# Get code -> state key for locked/unlocked replies (000 unlock / 001 lock).
_LOCK_GETS = {
    GET_POWER_LOCK: "power_lock",
    GET_BUTTON_LOCK: "button_lock",
    GET_MENU_LOCK: "menu_lock",
}

# The poll cycle: every get with a state variable behind it. Entries are
# (command type, code, value); models that lack a function answer '-',
# which the driver ignores.
_POLL_GETS = [
    ("g", GET_POWER, "000"),
    ("g", GET_INPUT, "000"),
    ("g", GET_VOLUME, "000"),
    ("g", GET_MUTE, "000"),
    ("g", GET_BRIGHTNESS, "000"),
    (TYPE_GET_BACKLIGHT, BACKLIGHT_CODE, "000"),
    ("g", GET_CONTRAST, "000"),
    ("g", GET_SHARPNESS, "000"),
    ("g", GET_COLOR, "000"),
    ("g", GET_TINT, "000"),
    ("g", GET_BACKLIGHT_ON, "000"),
    ("g", GET_FREEZE, "000"),
    ("g", GET_FUNCTION_ONOFF, "003"),   # touch on/off
    ("g", GET_POWER_LOCK, "000"),
    ("g", GET_BUTTON_LOCK, "000"),
    ("g", GET_MENU_LOCK, "000"),
    ("g", GET_RCU_MODE, "000"),
    ("g", GET_THERMAL, "000"),
    ("g", GET_PIP_MODE, "000"),
    ("g", GET_PIP_INPUT, "000"),
    ("g", GET_TILING_MODE, "000"),
    ("g", GET_TILING_COMP, "000"),
    ("g", GET_TILING_HV, "000"),
    ("g", GET_TILING_POSITION, "000"),
    ("g", GET_SMART_HUB, "000"),
    ("g", GET_OPERATION_HOURS, "000"),
]

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


def _num(label: str, minimum: int = 0, maximum: int = 100) -> dict:
    return {
        "type": "integer", "required": True, "label": label,
        "min": minimum, "max": maximum,
    }


def _onoff(label: str) -> dict:
    return {
        "type": "enum", "required": True, "label": label,
        "values": [
            {"value": "on", "label": "On"},
            {"value": "off", "label": "Off"},
        ],
    }


class ViewSonicCdeDriver(BaseDriver):
    """ViewSonic CDE commercial display LFD RS-232 & LAN protocol driver."""

    DRIVER_INFO = {
        "id": "viewsonic_cde",
        "name": "ViewSonic Commercial Display",
        "manufacturer": "ViewSonic",
        "category": "display",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls ViewSonic CDE-series commercial displays (and other "
            "ViewSonic large-format displays publishing the LFD RS-232 & "
            "LAN protocol) on TCP port 5000 or direct RS-232. Power, input "
            "select, volume, mute, brightness, backlight, picture values, "
            "freeze and screen blank, PIP, video-wall tiling, keypad "
            "navigation, control locks, ambient sensor readings, and "
            "Wake-on-LAN wake-up for displays whose standby closes the "
            "network port. Listens for the display's own status pushes and "
            "polls for the rest."
        ),
        "source_url": "https://viewsonicvsa.freshdesk.com/support/solutions/articles/43000470279-viewsonic-lfd-rs-232-lan-protocol",
        "tags": ["display", "viewsonic", "cde", "signage", "lfd", "rs232", "lan-control"],
        "verified": False,
        "simulated": True,
        "ports": [5000],
        "min_platform_version": "0.23.0",
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "delimiter": "\r",
        "compatible_models": [
            {
                "manufacturer": "ViewSonic",
                "models": [
                    "CDE4330", "CDE5530", "CDE6530", "CDE7530", "CDE8630", "CDE9830",
                    "CDE4320", "CDE5520", "CDE6520", "CDE7520", "CDE8620",
                    "CDE7512",
                ],
                "confidence": "untested",
                "notes": (
                    "The CDE30 and CDE20 series publish this protocol in their "
                    "per-model RS-232 documentation. Every ViewSonic large-"
                    "format display on the brand-wide LFD RS-232 & LAN "
                    "protocol — including ViewBoard IFP interactive displays "
                    "and NMP media players — is expected to work. Function "
                    "support varies by model (PIP and tiling are model-"
                    "dependent); an unsupported command answers a reject the "
                    "driver ignores."
                ),
            },
        ],
        "help": {
            "overview": (
                "ViewSonic commercial displays speaking the LFD RS-232 & LAN "
                "protocol. Power, input, audio, picture controls, screen "
                "blank and freeze, PIP, video-wall tiling, and control "
                "locks. The display pushes power / input / brightness / "
                "backlight / volume / mute changes on its own; everything "
                "else is polled every 30 seconds."
            ),
            "setup": (
                "1. Network control: connect the display's LAN port, note "
                "the IP address from the network settings, and add it here "
                "(TCP port 5000, no login). Monitor ID stays 1 for LAN "
                "control.\n"
                "2. Check the display's standby / network-standby setting: "
                "on many models a display in standby closes the network "
                "port. Enable the mode that keeps the network alive, or "
                "rely on the Wake Display (Wake-on-LAN) action.\n"
                "3. Serial control: connect the RS-232 port with a "
                "crossover (null-modem) cable, 9600 8N1 (fixed). On a "
                "daisy chain, set each display's Monitor ID in its OSD and "
                "match it here."
            ),
            "connection": (
                "A display whose standby mode shuts the network down does "
                "not answer on TCP 5000. Wake it with the Wake Display "
                "(Wake-on-LAN) action — the display's MAC is learned "
                "automatically on every connection."
            ),
        },
        "discovery": {
            # Active fingerprint: the documented communication-link test
            # (Get-ACK) on TCP 5000 answers the framed reply '801rz000' —
            # protocol-unique grammar. A display in network-dead standby
            # won't probe; the OUI / alias hints still surface it.
            "tcp_probe": {
                "port": 5000,
                "send_ascii": "801gz000\r",
                "expect": "01rz",
            },
            "oui": ["00:0b:14", "04:0e:c2"],
            "manufacturer_alias": ["viewsonic", "viewsonic corporation"],
        },
        "default_config": {
            "host": "",
            "port": 5000,
            "monitor_id": 1,
            "poll_interval": 30,
            "inter_command_delay": 0.05,
            "baudrate": 9600,
            "parity": "N",
            "bytesize": 8,
            "stopbits": 1,
            "mac_address": "",
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "Display IP address. LAN control uses TCP port 5000.",
            },
            "port": {
                "type": "integer", "default": 5000, "label": "TCP Port",
                "description": "Default 5000 for ViewSonic LAN control (fixed on the display).",
            },
            "monitor_id": {
                "type": "integer", "default": 1, "min": 1, "max": 98,
                "label": "Monitor ID",
                "description": "Leave at 1 for LAN control. RS-232 daisy chains address IDs 1-98 (set per display in the OSD).",
            },
            "poll_interval": {
                "type": "integer", "default": 30, "min": 5,
                "label": "Poll Interval (sec)",
            },
            "inter_command_delay": {
                "type": "number", "default": 0.05,
                "label": "Inter-Command Delay (sec)",
            },
            "baudrate": {
                "type": "integer", "default": 9600, "label": "Baud Rate (serial)",
                "description": "ViewSonic LFD serial control is fixed at 9600 8N1.",
            },
            "mac_address": {
                "type": "string", "default": "", "label": "MAC Address (for Wake-on-LAN)",
                "description": "Optional. Learned automatically on every connection; fill in manually to wake a display that has never connected.",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum", "values": ["on", "standby"],
                "label": "Power", "control": True,
                "help": "on = picture up; standby = off. Whether LAN control survives standby depends on the display's standby/network setting — the Wake Display action covers the rest.",
            },
            "source": {
                "type": "string", "label": "Source", "control": True,
                "help": "Active input (hdmi1-4, dp1/dp2, typec1/typec2, ops, android, vga1-3, dvi, internal, ypbpr, av, svideo, tv). Unknown codes read back as source_<code>.",
            },
            "signal_detected": {
                "type": "boolean", "label": "Signal Present",
                "help": "Whether the current input has a detected video signal (reported with every input read).",
            },
            "volume": {
                "type": "integer", "label": "Volume", "control": True,
                "min": 0, "max": 100, "step": 1,
                "help": "Speaker volume, 0-100.",
            },
            "mute": {
                "type": "boolean", "label": "Mute", "control": True,
                "help": "Audio mute state.",
            },
            "brightness": {
                "type": "integer", "label": "Brightness", "control": True,
                "min": 0, "max": 100, "step": 1,
                "help": "Picture brightness, 0-100. On Android-based models the embedded source is governed by Backlight instead.",
            },
            "backlight": {
                "type": "integer", "label": "Backlight",
                "min": 0, "max": 100, "step": 1,
                "help": "Backlight level, 0-100.",
            },
            "contrast": {
                "type": "integer", "label": "Contrast", "min": 0, "max": 100, "step": 1,
                "help": "Picture contrast, 0-100.",
            },
            "sharpness": {
                "type": "integer", "label": "Sharpness", "min": 0, "max": 100, "step": 1,
                "help": "Picture sharpness, 0-100.",
            },
            "color": {
                "type": "integer", "label": "Color", "min": 0, "max": 100, "step": 1,
                "help": "Color saturation, 0-100.",
            },
            "tint": {
                "type": "integer", "label": "Tint", "min": 0, "max": 100, "step": 1,
                "help": "Picture tint, 0-100.",
            },
            "backlight_on": {
                "type": "boolean", "label": "Backlight On",
                "help": "False while the screen is blanked (backlight off) with the display still on and controllable.",
            },
            "freeze": {
                "type": "boolean", "label": "Freeze",
                "help": "Whether the picture is frozen.",
            },
            "touch_enabled": {
                "type": "boolean", "label": "Touch Enabled",
                "help": "Whether the touch screen is enabled (touch-capable models only).",
            },
            "power_lock": {
                "type": "enum", "values": ["locked", "unlocked"],
                "label": "Power Key Lock",
                "help": "Whether the display's power key is locked. RS-232/LAN power control keeps working while locked.",
            },
            "button_lock": {
                "type": "enum", "values": ["locked", "unlocked"],
                "label": "Button Lock",
                "help": "Whether the front-panel buttons and remote are locked (power key excepted).",
            },
            "menu_lock": {
                "type": "enum", "values": ["locked", "unlocked"],
                "label": "Menu Lock",
                "help": "Whether the OSD menu key is locked.",
            },
            "remote_control_mode": {
                "type": "enum", "values": ["disabled", "enabled", "passthrough"],
                "label": "Remote Control Mode",
                "help": "IR remote handling: enabled, disabled, or passed through to a device on the RS-232 port.",
            },
            "pip_mode": {
                "type": "enum", "values": ["off", "pip", "pbp"],
                "label": "PIP Mode",
                "help": "Picture-in-picture / picture-by-picture mode (model-dependent).",
            },
            "pip_input": {
                "type": "string", "label": "PIP Input",
                "help": "Source shown in the PIP window (model-dependent).",
            },
            "tiling_mode": {
                "type": "boolean", "label": "Tiling Mode",
                "help": "Video-wall tiling on/off (video-wall models only).",
            },
            "tiling_compensation": {
                "type": "boolean", "label": "Tiling Bezel Compensation",
                "help": "Video-wall bezel-width compensation on/off.",
            },
            "tiling_layout": {
                "type": "string", "label": "Tiling Layout",
                "help": "Video-wall layout as <horizontal>x<vertical> monitors (e.g. 3x3).",
            },
            "tiling_position": {
                "type": "integer", "label": "Tiling Position",
                "help": "This display's position in the video wall (1-25; 0 while tiling is off).",
            },
            "thermal_c": {
                "type": "integer", "label": "Temperature (C)",
                "help": "Internal temperature reported by the display, degrees Celsius.",
            },
            "amb_temperature_c": {
                "type": "number", "label": "Ambient Temperature (C)",
                "help": "Ambient temperature from the Smart Hub sensor accessory (models with a sensor hub only).",
            },
            "amb_humidity": {
                "type": "number", "label": "Ambient Humidity (%)",
                "help": "Ambient relative humidity from the Smart Hub sensor accessory.",
            },
            "amb_light": {
                "type": "integer", "label": "Ambient Light",
                "help": "Ambient light level from the Smart Hub sensor accessory.",
            },
            "amb_presence": {
                "type": "boolean", "label": "Presence Detected",
                "help": "PIR presence detection from the Smart Hub sensor accessory — usable as an occupancy trigger.",
            },
            "operation_hours": {
                "type": "integer", "label": "Operation Hours",
                "help": "Accumulated operation hours reported by the display.",
            },
            "device_name": {
                "type": "string", "label": "Device Name",
                "help": "Device name reported by the display.",
            },
            "mac_address": {
                "type": "string", "label": "MAC Address",
                "help": "Network MAC address reported by the display; used by the Wake Display action.",
            },
            "ip_address": {
                "type": "string", "label": "Reported IP Address",
                "help": "IP address the display reports for itself.",
            },
            "serial_number": {
                "type": "string", "label": "Serial Number",
                "help": "Serial number reported by the display.",
            },
            "firmware_version": {
                "type": "string", "label": "Firmware Version",
                "help": "Firmware version reported by the display.",
            },
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "help": "Turn the display on. Works over LAN only if the display's standby mode keeps the network alive — otherwise use the Wake Display (Wake-on-LAN) action.",
                "params": {},
            },
            "power_off": {
                "label": "Power Off (Standby)",
                "help": "Put the display in standby. Depending on the model's standby setting this may close the LAN control port — the Wake Display action wakes it back up.",
                "params": {},
            },
            "set_source": {
                "label": "Select Source",
                "help": "Select an input. Sources a given model lacks answer a reject the driver ignores.",
                "params": {
                    "source": {
                        "type": "enum", "required": True, "label": "Source",
                        "values": INPUT_LABELS,
                    },
                },
            },
            "input_cycle": {
                "label": "Cycle Input",
                "help": "Step to the next input in the display's own cycle list.",
                "params": {},
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {"level": _num("Volume (0-100)")},
            },
            "volume_up": {"label": "Volume Up", "params": {}},
            "volume_down": {"label": "Volume Down", "params": {}},
            "mute_on": {"label": "Mute On", "params": {}},
            "mute_off": {"label": "Mute Off", "params": {}},
            "set_brightness": {
                "label": "Set Brightness",
                "params": {"level": _num("Brightness (0-100)")},
            },
            "brightness_up": {"label": "Brightness Up", "params": {}},
            "brightness_down": {"label": "Brightness Down", "params": {}},
            "set_contrast": {
                "label": "Set Contrast",
                "params": {"level": _num("Contrast (0-100)")},
            },
            "set_sharpness": {
                "label": "Set Sharpness",
                "params": {"level": _num("Sharpness (0-100)")},
            },
            "set_color": {
                "label": "Set Color",
                "params": {"level": _num("Color (0-100)")},
            },
            "set_tint": {
                "label": "Set Tint",
                "params": {"level": _num("Tint (0-100)")},
            },
            "backlight_off": {
                "label": "Screen Blank (Backlight Off)",
                "help": "Blank the picture by switching the backlight off. The display stays on and controllable — the right 'off' for a short pause.",
                "params": {},
            },
            "backlight_on": {
                "label": "Screen Unblank (Backlight On)",
                "params": {},
            },
            "freeze_on": {"label": "Freeze Picture", "params": {}},
            "freeze_off": {"label": "Unfreeze Picture", "params": {}},
            "set_color_mode": {
                "label": "Set Color Mode",
                "help": "Color temperature preset. Set-only: the protocol has no color-mode read-back.",
                "params": {
                    "mode": {
                        "type": "enum", "required": True, "label": "Color Mode",
                        "values": [
                            {"value": "normal", "label": "Normal"},
                            {"value": "warm", "label": "Warm"},
                            {"value": "cold", "label": "Cold"},
                            {"value": "personal", "label": "Personal"},
                        ],
                    },
                },
            },
            "set_picture_size": {
                "label": "Set Picture Size",
                "help": "Aspect handling. Set-only: the protocol has no picture-size read-back.",
                "params": {
                    "size": {
                        "type": "enum", "required": True, "label": "Picture Size",
                        "values": [
                            {"value": "full", "label": "Full (16:9)"},
                            {"value": "normal", "label": "Normal (4:3)"},
                            {"value": "real", "label": "Real (1:1)"},
                        ],
                    },
                },
            },
            "set_bass": {
                "label": "Set Bass",
                "help": "Set-only: the protocol has no bass read-back.",
                "params": {"level": _num("Bass (0-100)")},
            },
            "set_treble": {
                "label": "Set Treble",
                "help": "Set-only: the protocol has no treble read-back.",
                "params": {"level": _num("Treble (0-100)")},
            },
            "set_balance": {
                "label": "Set Balance",
                "help": "50 is centered. Set-only: the protocol has no balance read-back.",
                "params": {"level": _num("Balance (0-100)")},
            },
            "surround_on": {
                "label": "Surround Sound On",
                "help": "Set-only: the protocol has no surround read-back.",
                "params": {},
            },
            "surround_off": {"label": "Surround Sound Off", "params": {}},
            "set_osd_language": {
                "label": "Set OSD Language",
                "help": "Set-only; some models extend the list beyond these three documented languages.",
                "params": {
                    "language": {
                        "type": "enum", "required": True, "label": "Language",
                        "values": [
                            {"value": "english", "label": "English"},
                            {"value": "french", "label": "French"},
                            {"value": "spanish", "label": "Spanish"},
                        ],
                    },
                },
            },
            "set_pip_mode": {
                "label": "Set PIP Mode",
                "params": {
                    "mode": {
                        "type": "enum", "required": True, "label": "PIP Mode",
                        "values": [
                            {"value": "off", "label": "Off"},
                            {"value": "pip", "label": "PIP"},
                            {"value": "pbp", "label": "PBP"},
                        ],
                    },
                },
            },
            "set_pip_input": {
                "label": "Set PIP Input",
                "params": {
                    "source": {
                        "type": "enum", "required": True, "label": "PIP Source",
                        "values": INPUT_LABELS,
                    },
                },
            },
            "set_pip_sound": {
                "label": "Set PIP Sound",
                "help": "Which picture's audio plays. Set-only: no read-back.",
                "params": {
                    "from_window": {
                        "type": "enum", "required": True, "label": "Sound From",
                        "values": [
                            {"value": "main", "label": "Main Picture"},
                            {"value": "sub", "label": "Sub Picture"},
                        ],
                    },
                },
            },
            "set_pip_position": {
                "label": "Set PIP Position",
                "help": "Set-only: no read-back.",
                "params": {
                    "position": {
                        "type": "enum", "required": True, "label": "Position",
                        "values": [
                            {"value": "up", "label": "Up"},
                            {"value": "down", "label": "Down"},
                            {"value": "left", "label": "Left"},
                            {"value": "right", "label": "Right"},
                        ],
                    },
                },
            },
            "set_tiling_mode": {
                "label": "Set Tiling Mode",
                "help": "Video-wall tiling on/off (video-wall models only).",
                "params": {"mode": _onoff("Tiling")},
            },
            "set_tiling_compensation": {
                "label": "Set Tiling Bezel Compensation",
                "params": {"mode": _onoff("Bezel Compensation")},
            },
            "set_tiling_layout": {
                "label": "Set Tiling Layout",
                "help": "Video-wall size in monitors, 1-9 each way.",
                "params": {
                    "horizontal": _num("Horizontal Monitors (1-9)", 1, 9),
                    "vertical": _num("Vertical Monitors (1-9)", 1, 9),
                },
            },
            "set_tiling_position": {
                "label": "Set Tiling Position",
                "help": "This display's position in the video wall (1-25, row-major from top left).",
                "params": {"position": _num("Position (1-25)", 1, 25)},
            },
            "nav_key": {
                "label": "Press Navigation Key",
                "help": "Send a virtual keypad press for OSD navigation.",
                "params": {
                    "key": {
                        "type": "enum", "required": True, "label": "Key",
                        "values": [
                            {"value": "up", "label": "Up"},
                            {"value": "down", "label": "Down"},
                            {"value": "left", "label": "Left"},
                            {"value": "right", "label": "Right"},
                            {"value": "enter", "label": "Enter"},
                            {"value": "input", "label": "Input"},
                            {"value": "menu", "label": "Menu"},
                            {"value": "exit", "label": "Exit"},
                        ],
                    },
                },
            },
            "press_number": {
                "label": "Press Number Key",
                "params": {"number": _num("Number (0-9)", 0, 9)},
            },
            "custom_hot_key": {
                "label": "Trigger Custom Hot Key",
                "help": "Fire a model-defined customized hot key (e.g. 1 opens the MVBA app on supporting models).",
                "params": {"key": _num("Hot Key (1-999)", 1, 999)},
            },
            "restore_default": {
                "label": "Restore Factory Defaults",
                "help": "Recover the display to factory settings. This wipes the display's own configuration — use with care.",
                "params": {},
            },
            "raw_command": {
                "label": "Send Raw Command",
                "help": "Send any set/get from the LFD protocol tables: pick the type ('A'/'a' are the backlight-level pair), give the one-character command code and the three-character value field (e.g. type s, code $, value 076).",
                "params": {
                    "cmd_type": {
                        "type": "enum", "required": True, "label": "Type",
                        "values": [
                            {"value": "s", "label": "Set"},
                            {"value": "g", "label": "Get"},
                            {"value": "A", "label": "Set (backlight pair)"},
                            {"value": "a", "label": "Get (backlight pair)"},
                        ],
                    },
                    "code": {
                        "type": "string", "required": True, "label": "Command Code",
                        "pattern": "^[\\x20-\\x7e]$",
                    },
                    "value": {
                        "type": "string", "required": False, "label": "Value",
                        "pattern": "^[0-9A-Za-z]{3}$",
                    },
                },
            },
        },
        "device_settings": {
            "backlight": {
                "type": "integer", "min": 0, "max": 100,
                "label": "Backlight",
                "help": "Backlight level, 0-100. Persisted on the display.",
                "state_key": "backlight", "default": 100, "setup": False,
            },
            "power_lock": {
                "type": "enum",
                "values": [
                    {"value": "unlocked", "label": "Unlocked"},
                    {"value": "locked", "label": "Locked"},
                ],
                "label": "Power Key Lock",
                "help": "Lock the power key on the panel and remote. RS-232/LAN power control keeps working.",
                "state_key": "power_lock", "default": "unlocked", "setup": False,
            },
            "button_lock": {
                "type": "enum",
                "values": [
                    {"value": "unlocked", "label": "Unlocked"},
                    {"value": "locked", "label": "Locked"},
                ],
                "label": "Button Lock",
                "help": "Lock the front-panel buttons and remote (power key excepted).",
                "state_key": "button_lock", "default": "unlocked", "setup": False,
            },
            "menu_lock": {
                "type": "enum",
                "values": [
                    {"value": "unlocked", "label": "Unlocked"},
                    {"value": "locked", "label": "Locked"},
                ],
                "label": "Menu Lock",
                "help": "Lock the OSD menu key.",
                "state_key": "menu_lock", "default": "unlocked", "setup": False,
            },
            "remote_control_mode": {
                "type": "enum",
                "values": [
                    {"value": "enabled", "label": "Enabled"},
                    {"value": "disabled", "label": "Disabled"},
                    {"value": "passthrough", "label": "Pass Through"},
                ],
                "label": "Remote Control Mode",
                "help": "Pass Through forwards IR remote keys to the RS-232 port instead of acting on them.",
                "state_key": "remote_control_mode", "default": "enabled", "setup": False,
            },
            "touch": {
                "type": "boolean",
                "label": "Touch Screen",
                "help": "Enable or disable the touch screen (touch-capable models only).",
                "state_key": "touch_enabled", "default": True, "setup": False,
            },
        },
        "quick_actions": ["power_on", "power_off"],
        "actions": [
            {
                "id": "wake_display",
                "kind": "setup",
                "label": "Wake Display (Wake-on-LAN)",
                "icon": "power",
                "availability": "offline",
                "params": {
                    "mac": {
                        "type": "string", "required": False,
                        "label": "MAC Address",
                        "description": "Leave blank to use the MAC learned on the last connection (or the mac_address config field).",
                    },
                },
            },
        ],
        "protocols": ["viewsonic_lfd_rs232_lan"],
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any):
        super().__init__(device_id, config, state, events)
        self._monitor_id = int(config.get("monitor_id", 1) or 1)

    # ── Packet build / send ────────────────────────────────────────────────

    def _packet(self, cmd_type: str, code: str, value: str = "000") -> bytes:
        """Render one framed packet: length, ID, type, code, value, CR."""
        body = (
            f"{self._monitor_id:02d}"
            + cmd_type
            + code
            + value
        ).encode("ascii")
        # Length byte: ASCII digit of the byte count excluding CR, itself
        # included ('8' for the standard 9-byte packet).
        return bytes([0x30 + len(body) + 1]) + body + b"\r"

    async def _send(self, cmd_type: str, code: str, value: str = "000") -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(self._packet(cmd_type, code, value))

    async def _set(self, code: str, value: str) -> bool:
        await self._send("s", code, value)
        return True

    async def _get(self, code: str, value: str = "000") -> None:
        await self._send("g", code, value)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def _post_connect(self) -> None:
        # Identity + the MAC the Wake Display action needs. Fire-and-forget:
        # replies land in on_data_received; a model that rejects one of
        # these answers '-' and the state simply stays unset.
        await self._get(GET_DEVICE_NAME)
        await self._get(GET_MAC)
        await self._get(GET_IP)
        await self._get(GET_SERIAL)
        await self._get(GET_FW)

    async def poll(self) -> None:
        for cmd_type, code, value in _POLL_GETS:
            await self._send(cmd_type, code, value)

    # ── Commands ───────────────────────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        simple = {
            "power_on": (SET_POWER, "001"),
            "power_off": (SET_POWER, "000"),
            "input_cycle": (SET_INPUT, "00Z"),
            "volume_up": (SET_VOLUME, "901"),
            "volume_down": (SET_VOLUME, "900"),
            "mute_on": (SET_MUTE, "001"),
            "mute_off": (SET_MUTE, "000"),
            "brightness_up": (SET_BRIGHTNESS, "901"),
            "brightness_down": (SET_BRIGHTNESS, "900"),
            "backlight_on": (SET_BACKLIGHT_ON, "001"),
            "backlight_off": (SET_BACKLIGHT_ON, "000"),
            "freeze_on": (SET_FREEZE, "001"),
            "freeze_off": (SET_FREEZE, "000"),
            "surround_on": (SET_SURROUND, "001"),
            "surround_off": (SET_SURROUND, "000"),
            "restore_default": (SET_RESTORE_DEFAULT, "000"),
        }
        if command in simple:
            code, value = simple[command]
            return await self._set(code, value)

        level_sets = {
            "set_volume": SET_VOLUME,
            "set_brightness": SET_BRIGHTNESS,
            "set_contrast": SET_CONTRAST,
            "set_sharpness": SET_SHARPNESS,
            "set_color": SET_COLOR,
            "set_tint": SET_TINT,
            "set_bass": SET_BASS,
            "set_treble": SET_TREBLE,
            "set_balance": SET_BALANCE,
        }
        if command in level_sets:
            level = int(params["level"])
            return await self._set(level_sets[command], f"{level:03d}")

        if command == "set_backlight":
            level = int(params["level"])
            await self._send(TYPE_SET_BACKLIGHT, BACKLIGHT_CODE, f"{level:03d}")
            return True

        if command in ("set_source", "set_pip_input"):
            token = str(params["source"])
            code3 = INPUT_TOKENS.get(token)
            if code3 is None:
                raise ValueError(f"Unknown source '{token}'")
            code = SET_INPUT if command == "set_source" else SET_PIP_INPUT
            return await self._set(code, code3)

        if command == "set_color_mode":
            return await self._set(SET_COLOR_MODE, COLOR_MODES[str(params["mode"])])

        if command == "set_picture_size":
            return await self._set(SET_PICTURE_SIZE, PICTURE_SIZES[str(params["size"])])

        if command == "set_osd_language":
            return await self._set(SET_OSD_LANGUAGE, OSD_LANGUAGES[str(params["language"])])

        if command == "set_pip_mode":
            return await self._set(SET_PIP_MODE, PIP_MODE_TOKENS[str(params["mode"])])

        if command == "set_pip_sound":
            return await self._set(SET_PIP_SOUND, PIP_SOUNDS[str(params["from_window"])])

        if command == "set_pip_position":
            return await self._set(SET_PIP_POSITION, PIP_POSITIONS[str(params["position"])])

        if command == "set_tiling_mode":
            return await self._set(SET_TILING_MODE, "001" if str(params["mode"]) == "on" else "000")

        if command == "set_tiling_compensation":
            return await self._set(SET_TILING_COMP, "001" if str(params["mode"]) == "on" else "000")

        if command == "set_tiling_layout":
            h = int(params["horizontal"])
            v = int(params["vertical"])
            if not (1 <= h <= 9 and 1 <= v <= 9):
                raise ValueError("Tiling layout is 1-9 monitors each way")
            return await self._set(SET_TILING_HV, f"0{h}{v}")

        if command == "set_tiling_position":
            return await self._set(SET_TILING_POSITION, f"{int(params['position']):03d}")

        if command == "nav_key":
            key = str(params["key"])
            if key not in NAV_KEYS:
                raise ValueError(f"Unknown navigation key '{key}'")
            return await self._set(SET_KEYPAD, NAV_KEYS[key])

        if command == "press_number":
            return await self._set(SET_NUMBER, f"{int(params['number']):03d}")

        if command == "custom_hot_key":
            return await self._set(SET_HOT_KEY, f"{int(params['key']):03d}")

        if command == "raw_command":
            cmd_type = str(params["cmd_type"])
            code = str(params["code"])
            value = str(params.get("value") or "000")
            await self._send(cmd_type, code, value)
            return True

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    # ── Device settings ────────────────────────────────────────────────────

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "backlight":
            await self._send(TYPE_SET_BACKLIGHT, BACKLIGHT_CODE, f"{int(value):03d}")
            await self._send(TYPE_GET_BACKLIGHT, BACKLIGHT_CODE, "000")
            return True

        if key == "touch":
            await self._set(SET_FUNCTION_ONOFF, "103" if value else "003")
            await self._get(GET_FUNCTION_ONOFF, "003")
            return True

        locks = {
            "power_lock": (SET_POWER_LOCK, GET_POWER_LOCK),
            "button_lock": (SET_BUTTON_LOCK, GET_BUTTON_LOCK),
            "menu_lock": (SET_MENU_LOCK, GET_MENU_LOCK),
        }
        if key in locks:
            set_code, get_code = locks[key]
            await self._set(set_code, "001" if str(value) == "locked" else "000")
            await self._get(get_code)
            return True

        if key == "remote_control_mode":
            await self._set(SET_RCU_MODE, RCU_MODE_TOKENS[str(value)])
            await self._get(GET_RCU_MODE)
            return True

        raise ValueError(f"Unknown device setting '{key}'")

    # ── Receive path ───────────────────────────────────────────────────────

    async def on_data_received(self, data: bytes) -> None:
        # One CR-stripped frame: len, ID(2), type, [code, value...].
        if len(data) < 4:
            return
        frame_id = data[1:3]
        if frame_id != f"{self._monitor_id:02d}".encode("ascii"):
            return  # another monitor on an RS-232 chain
        ftype = data[3:4]
        if ftype == b"+":
            return  # set accepted; pushes / polling refresh state
        if ftype == b"-":
            log.debug(f"[{self.device_id}] Command rejected by display")
            return
        if ftype == b"p":
            # IR pass-through key event (RCU mode "passthrough"); key codes
            # are for a downstream device, not display state.
            log.debug(f"[{self.device_id}] IR pass-through key: {data[4:6]!r}")
            return
        if ftype != b"r" or len(data) < 5:
            return
        code = data[4:5].decode("ascii", errors="replace")
        payload = data[5:].rstrip(b"\x00")
        self._apply_reply(code, payload)

    def _apply_reply(self, code: str, payload: bytes) -> None:
        text = payload.decode("ascii", errors="replace").strip()

        if code in _TEXT_GETS:
            if text:
                self.set_state(_TEXT_GETS[code], text)
            return

        if code == GET_MAC:
            hexstr = text.lower().replace(":", "").replace("-", "")
            if len(hexstr) == 12 and all(c in "0123456789abcdef" for c in hexstr):
                mac = ":".join(hexstr[i:i + 2] for i in range(0, 12, 2))
                if mac != "00:00:00:00:00:00":
                    self.set_state("mac_address", mac)
            return

        if code == GET_OPERATION_HOURS:
            if text.isdigit():
                self.set_state("operation_hours", int(text))
            return

        if code == GET_SMART_HUB:
            self._apply_smart_hub(text)
            return

        if code == GET_INPUT:
            if len(text) == 3:
                self.set_state("signal_detected", text[0] == "1")
                suffix = text[1:]
                self.set_state("source", INPUT_BY_SUFFIX.get(suffix, f"source_{suffix}"))
            return

        if code == GET_PIP_INPUT:
            if text in INPUT_CODES:
                self.set_state("pip_input", INPUT_CODES[text])
            elif len(text) == 3:
                self.set_state("pip_input", INPUT_BY_SUFFIX.get(text[1:], f"source_{text[1:]}"))
            return

        if code == GET_FUNCTION_ONOFF:
            # Reply is [1/0 on/off][function id]: 103 = touch on.
            state_key = FUNCTION_IDS.get(text[1:3]) if len(text) == 3 else None
            if state_key:
                self.set_state(state_key, text[0] == "1")
            return

        if code == GET_THERMAL:
            try:
                self.set_state("thermal_c", int(text))
            except ValueError:
                pass
            return

        if code == GET_TILING_HV:
            if len(text) == 3 and text[1].isdigit() and text[2].isdigit():
                self.set_state("tiling_layout", f"{text[1]}x{text[2]}")
            return

        if code == GET_ACK:
            return  # communication-link test answer

        if not text.isdigit():
            return

        if code in _NUMERIC_GETS:
            self.set_state(_NUMERIC_GETS[code], int(text))
        elif code == GET_POWER:
            self.set_state("power", "on" if text == "001" else "standby")
        elif code == GET_MUTE:
            self.set_state("mute", text == "001")
        elif code == GET_BACKLIGHT_ON:
            self.set_state("backlight_on", text == "001")
        elif code == GET_FREEZE:
            self.set_state("freeze", text == "001")
        elif code in _LOCK_GETS:
            self.set_state(_LOCK_GETS[code], "locked" if text == "001" else "unlocked")
        elif code == GET_RCU_MODE:
            mode = RCU_MODES.get(text)
            if mode:
                self.set_state("remote_control_mode", mode)
        elif code == GET_PIP_MODE:
            mode = PIP_MODES.get(text)
            if mode:
                self.set_state("pip_mode", mode)
        elif code == GET_TILING_MODE:
            self.set_state("tiling_mode", text == "001")
        elif code == GET_TILING_COMP:
            self.set_state("tiling_compensation", text == "001")
        elif code == GET_TILING_POSITION:
            self.set_state("tiling_position", int(text))

    def _apply_smart_hub(self, text: str) -> None:
        # Fixed 6-byte sub-fields, marker + 5 chars: A-05.0 B030.0 C00080
        # D00001 (any subset, in any order).
        i = 0
        while i + 6 <= len(text):
            marker, value = text[i], text[i + 1:i + 6]
            i += 6
            try:
                if marker == "A":
                    self.set_state("amb_temperature_c", float(value))
                elif marker == "B":
                    self.set_state("amb_humidity", float(value))
                elif marker == "C":
                    self.set_state("amb_light", int(value))
                elif marker == "D":
                    self.set_state("amb_presence", int(value) == 1)
            except ValueError:
                continue

    # ── Wake-on-LAN setup action ───────────────────────────────────────────

    async def run_setup_action(self, action_id, params, progress):
        if action_id != "wake_display":
            raise ValueError(f"Unknown setup action '{action_id}'")

        mac = (
            str(params.get("mac") or "").strip()
            or str(self.get_state("mac_address") or "").strip()
            or str(self.config.get("mac_address") or "").strip()
        )
        if not mac:
            raise ValueError(
                "No MAC address available. The MAC is learned automatically "
                "on the first connection; enter it here or in the device's "
                "mac_address config field."
            )
        if not _MAC_RE.match(mac):
            raise ValueError(f"'{mac}' is not a valid MAC address (aa:bb:cc:dd:ee:ff)")

        await progress(f"Sending Wake-on-LAN magic packet to {mac}", pct=20)
        mac_bytes = bytes(int(part, 16) for part in re.split(r"[:-]", mac))
        # The LFD spec's 126-byte WOL frame: 16 MAC repeats plus a 24-byte
        # zero tail, on UDP port 9. Standard WOL listeners ignore the tail.
        magic = b"\xff" * 6 + mac_bytes * 16 + b"\x00" * 24
        loop = asyncio.get_running_loop()

        def _send_wol() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(magic, ("255.255.255.255", 9))
                # Also aim at the display's own address in case broadcast
                # is filtered between subnets.
                host = self.config.get("host") or ""
                if host:
                    try:
                        sock.sendto(magic, (host, 9))
                    except OSError:
                        pass

        await loop.run_in_executor(None, _send_wol)
        await progress(
            "Magic packet sent. The display takes a little while to wake; "
            "reconnecting.",
            pct=70,
        )
        try:
            await self.request_reconnect()
        except Exception:
            # Still waking — the normal auto-reconnect loop keeps retrying.
            pass
        await progress(
            "Done. If the display stays offline, check its standby / "
            "network settings allow Wake-on-LAN.",
            pct=100,
        )
        return {"mac": mac}
