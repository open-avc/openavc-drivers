"""
OpenAVC BenQ commercial display driver (RS232 & LAN protocol).

Controls BenQ interactive flat panels (BenQ Boards: RM / RP / RE / CP /
CM series) and BenQ Smart Signage (IL / ST / SL / SM / BH series) over
TCP port 4660 or direct RS-232. All of them publish the same "RS232 &
LAN Protocol" command grammar; per-model support varies and an
unsupported command answers a '-' reject, which the driver ignores.

Why Python: the protocol is a fixed-position framed packet, not a
pattern-shaped text line. Set/get command codes above 0x80 (picture
mode, saturation, hue, backlight, DCR, color temperature, power save,
WOL, ...) are raw high-bit bytes that all decode to the same U+FFFD
replacement character in text matching, so YAML response rules cannot
tell their replies apart; the model/serial and network queries carry
NUL-padded binary value blocks; and waking a standby display needs a
Wake-on-LAN magic packet from a setup action. Position-parsing bytes in
Python keeps every read unambiguous.

Wire protocol (ASCII framing, CR terminated)::

    Set:    [len][ID two digits]['s'][code][value...]<CR>
    Get:    [len][ID two digits]['g'][code][value...]<CR>
    Ack:    [len][ID]['+' or '-']<CR>              (set result)
    Reply:  [len][ID]['r'][code][value...]<CR>     (get answer)

  - The length byte is 0x30 + total bytes excluding CR ('8' for the
    common 8-byte packet, ':' for 10, 'D' for 20).
  - Values are ASCII digits (three for most functions, five for the
    operation-time counter). The model-info and network queries instead
    carry a binary selector byte plus NUL padding, and answer ASCII/hex
    payloads.
  - Monitor ID is "01" for LAN control (fixed on current firmware);
    RS-232 daisy chains on older signage can address IDs 1-98, so the
    ID is a config field.

Push vs poll: POLL-ONLY. The protocol is strictly request/response —
no notification mechanism is documented anywhere in the family's
guides — so the driver polls the documented Get functions and pulls
model name, firmware, serial number, and the network MAC once on
connect.

Power model (matters for automation):
  - "001" power on, "000" screen off (backlight off + mute — Android
    stays up, so LAN control KEEPS WORKING; the right "off" for
    automated spaces), "002" standby (Android off — the LAN control
    port goes dead until the panel is woken), "003" reboot.
  - Per the manual, LAN commands only work while the display is on or
    screen-off. A display put into standby is unreachable over LAN;
    the "Wake Display (Wake-on-LAN)" setup action sends a WOL magic
    packet (the display's WOL setting must be on) using the MAC learned
    on the last connection (or the mac_address config field).

Protocol reference: BenQ "RM6503/RM7503/RM8603/RM8603T RS232 & LAN
Protocol Installation Guide" (2022); the signage-wide "Generic
RS232/LAN Protocol Installation Guide" (2019) documents the same
grammar with baud 9600.
"""

from __future__ import annotations

import asyncio
import re
import socket
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# ── Wire tables (RM6503 RS232 & LAN guide; generic signage guide) ──────────

# Set-function command codes.
SET_POWER = 0x21
SET_SOURCE = 0x22
SET_CONTRAST = 0x23
SET_BRIGHTNESS = 0x24
SET_SHARPNESS = 0x25
SET_PICTURE_RESET = 0x26
SET_ASPECT = 0x31
SET_SOUND_MODE = 0x33
SET_VOLUME = 0x35
SET_MUTE = 0x36
SET_TREBLE = 0x37
SET_BASS = 0x38
SET_BALANCE = 0x39
SET_SOUND_RESET = 0x3B
SET_REMOTE = 0x40
SET_IR_LOCK = 0x42
SET_KEYPAD_LOCK = 0x45
SET_PICTURE_MODE = 0x81
SET_SATURATION = 0x82
SET_HUE = 0x83
SET_BACKLIGHT = 0x84
SET_DCR = 0x85
SET_COLOR_TEMP = 0x86
SET_POWER_SAVE = 0xA9
SET_SWITCH_ON = 0xAB
SET_WOL = 0xF0

# Get-function command codes -> the state each reply feeds.
GET_MODEL_INFO = 0x20     # binary selector payload (model / fw / serial)
GET_SIGNAL = 0x22
GET_TREBLE = 0x37
GET_BASS = 0x38
GET_BALANCE = 0x39
GET_CONTRAST = 0x61
GET_BRIGHTNESS = 0x62
GET_SHARPNESS = 0x63
GET_SOUND_MODE = 0x65
GET_VOLUME = 0x66
GET_MUTE = 0x67
GET_IR_LOCK = 0x68
GET_SOURCE = 0x6A
GET_POWER = 0x6C
GET_KEYPAD_LOCK = 0x73
GET_OPERATION_TIME = 0x76
GET_ASPECT = 0x77
GET_PICTURE_MODE = 0xB1
GET_SATURATION = 0xB2
GET_HUE = 0xB3
GET_BACKLIGHT = 0xB4
GET_DCR = 0xB5
GET_COLOR_TEMP = 0xB6
GET_POWER_SAVE = 0xD9
GET_SWITCH_ON = 0xDA
GET_WOL = 0xF0
GET_NETWORK = 0xE1        # binary selector payload (MAC address)

# Video Source codes (set 0x22 / get 0x6A). Older signage may report
# additional codes; unknown ones read back as "source_<code>".
SOURCE_CODES = {
    "000": "vga",
    "001": "hdmi",
    "002": "hdmi1",
    "021": "hdmi2",
    "007": "displayport",
    "051": "typec",
    "101": "android",
    "102": "ops",
    "107": "ezwrite",
    "108": "wifi",
}
SOURCE_TOKENS = {v: k for k, v in SOURCE_CODES.items()}
SOURCE_LABELS = [
    {"value": "hdmi", "label": "HDMI"},
    {"value": "hdmi1", "label": "HDMI 1"},
    {"value": "hdmi2", "label": "HDMI 2"},
    {"value": "displayport", "label": "DisplayPort"},
    {"value": "typec", "label": "USB-C"},
    {"value": "vga", "label": "VGA"},
    {"value": "android", "label": "Android"},
    {"value": "ops", "label": "OPS Slot PC"},
    {"value": "ezwrite", "label": "EZWrite Whiteboard"},
    {"value": "wifi", "label": "Wireless Presentation"},
]

POWER_CODES = {"000": "screen_off", "001": "on", "002": "standby"}

PICTURE_MODES = {
    "standard": "000", "bright": "001", "soft": "002", "eco": "003",
    "custom1": "005", "custom2": "006", "custom3": "007",
}
PICTURE_MODE_NAMES = {v: k for k, v in PICTURE_MODES.items()}
PICTURE_MODE_LABELS = [
    {"value": "standard", "label": "Standard"},
    {"value": "bright", "label": "Bright"},
    {"value": "soft", "label": "Soft"},
    {"value": "eco", "label": "ECO"},
    {"value": "custom1", "label": "Custom 1"},
    {"value": "custom2", "label": "Custom 2"},
    {"value": "custom3", "label": "Custom 3"},
]

SOUND_MODES = {
    "movie": "000", "standard": "001", "custom": "002",
    "class": "003", "meeting": "004",
}
SOUND_MODE_NAMES = {v: k for k, v in SOUND_MODES.items()}

COLOR_TEMPS = {"cool": "000", "normal": "001", "warm": "002"}
COLOR_TEMP_NAMES = {v: k for k, v in COLOR_TEMPS.items()}

ASPECTS = {"16:9": "000", "ptp": "002"}
ASPECT_NAMES = {v: k for k, v in ASPECTS.items()}

POWER_SAVE_MODES = {"off": "000", "low": "001", "high": "002"}
POWER_SAVE_NAMES = {v: k for k, v in POWER_SAVE_MODES.items()}

SWITCH_ON_MODES = {"power_off": "000", "force_on": "001", "last_status": "002"}
SWITCH_ON_NAMES = {v: k for k, v in SWITCH_ON_MODES.items()}

# Remote-control virtual key codes (set 0x40).
REMOTE_KEYS = {
    "vol_up": "000", "vol_down": "001",
    "up": "010", "down": "011", "left": "012", "right": "013",
    "ok": "014", "menu": "020", "exit": "022",
    "blank": "031", "freeze": "032",
}

# 0-100 numeric state fed by simple three-digit get replies.
_NUMERIC_GETS = {
    GET_CONTRAST: "contrast",
    GET_BRIGHTNESS: "brightness",
    GET_SHARPNESS: "sharpness",
    GET_SATURATION: "saturation",
    GET_HUE: "hue",
    GET_BACKLIGHT: "backlight",
    GET_VOLUME: "volume",
    GET_TREBLE: "treble",
    GET_BASS: "bass",
    GET_BALANCE: "balance",
}

# The poll cycle: every documented three-digit get, cheapest first.
_POLL_GETS = [
    GET_POWER, GET_SOURCE, GET_SIGNAL, GET_VOLUME, GET_MUTE,
    GET_CONTRAST, GET_BRIGHTNESS, GET_SHARPNESS, GET_BACKLIGHT,
    GET_SATURATION, GET_HUE, GET_PICTURE_MODE, GET_COLOR_TEMP, GET_DCR,
    GET_SOUND_MODE, GET_TREBLE, GET_BASS, GET_BALANCE, GET_ASPECT,
    GET_IR_LOCK, GET_KEYPAD_LOCK, GET_POWER_SAVE, GET_SWITCH_ON,
    GET_WOL,
]

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


def _num(value: str, minimum: int = 0, maximum: int = 100) -> dict:
    return {
        "type": "integer", "required": True, "label": value,
        "min": minimum, "max": maximum,
    }


class BenqDisplayDriver(BaseDriver):
    """BenQ Board / Smart Signage RS232 & LAN protocol driver."""

    DRIVER_INFO = {
        "id": "benq_display",
        "name": "BenQ Display",
        "manufacturer": "BenQ",
        "category": "display",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls BenQ interactive flat panels (BenQ Boards: RM, RP, RE, "
            "CP, CM series) and BenQ Smart Signage using BenQ's RS232 & LAN "
            "protocol on TCP port 4660 or direct RS-232. Power and screen "
            "off, source select, volume, mute and audio EQ, picture values "
            "and modes, backlight, color temperature, aspect, remote-key "
            "navigation, IR and keypad locks, power save, and Wake-on-LAN "
            "wake-up for displays in standby. Polls for status."
        ),
        "source_url": "https://esupportdownload.benq.com/esupport/PUBLIC%20DISPLAY%20PRODUCT/Control%20Protocols/RM6503/RS232%20&%20LAN%20Command%20List_2_Others.pdf",
        "tags": ["display", "benq", "benq-board", "ifp", "signage", "rs232", "lan-control"],
        "verified": False,
        "simulated": True,
        "ports": [4660],
        "min_platform_version": "0.23.0",
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "delimiter": "\r",
        "compatible_models": [
            {
                "manufacturer": "BenQ",
                "models": [
                    "RM6503", "RM7503", "RM8603",
                    "RP6503", "RP7503", "RP8603",
                    "RP6502", "RP7502", "RP8602",
                    "RP553K", "RP653K",
                    "CP5505", "CP7505", "CM5505", "CM7505",
                    "RP552", "RP552H", "RP653", "RP703", "RP750", "RP750K", "RP840G",
                    "IL430",
                ],
                "confidence": "untested",
                "notes": (
                    "Every listed model has a published BenQ RS232 & LAN (or "
                    "RS232 command list) guide with this grammar. Other BenQ "
                    "Boards and Smart Signage (ST / SL / SM / BH series) "
                    "publish the same Generic RS232/LAN protocol and are "
                    "expected to work. Function support varies by model; an "
                    "unsupported command answers a reject the driver ignores. "
                    "Current BenQ Boards fix the serial baud at 115200; older "
                    "RP panels and Smart Signage use 9600."
                ),
            },
        ],
        "help": {
            "overview": (
                "BenQ Boards (interactive flat panels) and BenQ Smart Signage "
                "speaking BenQ's RS232 & LAN protocol. Power, source, audio, "
                "picture controls, backlight, locks, and power-save settings. "
                "Polls every 30 seconds. Use Screen Off (not Standby) as the "
                "automated 'off' for a space: it blanks the backlight while "
                "network control stays available. Standby shuts Android down "
                "and the LAN control port with it — a standby display is "
                "woken with the Wake Display (Wake-on-LAN) action or from "
                "the panel itself."
            ),
            "setup": (
                "1. Network control: connect the display's LAN port, note the "
                "IP address from the network settings, and add it here (TCP "
                "port 4660). Monitor ID stays 1 for LAN control.\n"
                "2. Turn the display's Wake-on-LAN setting on so a standby "
                "display can be woken remotely.\n"
                "3. Serial control: connect the RS-232 port. Current BenQ "
                "Boards run 115200 8N1 (fixed); older RP panels and Smart "
                "Signage run 9600 8N1 — set Baud Rate to match your model.\n"
                "4. LAN commands only work while the display is on or screen-"
                "off (Android running). After Standby, use the Wake Display "
                "action, then reconnect."
            ),
            "connection": (
                "A display in standby (Android off) does not answer on the "
                "LAN control port. Wake it with the Wake Display "
                "(Wake-on-LAN) action, or use Screen Off instead of Standby "
                "so control stays available."
            ),
        },
        "discovery": {
            # Active fingerprint: the power get on TCP 4660 answers the
            # framed reply '801rl<value>' — protocol-unique grammar. A
            # display in standby has the port closed and won't probe; the
            # OUI / alias hints still surface it from ARP sweeps.
            "tcp_probe": {
                "port": 4660,
                "send_ascii": "801gl000\r",
                "expect": "01r",
            },
            "oui": ["80:65:e9"],
            "manufacturer_alias": ["benq", "benq corporation"],
        },
        "default_config": {
            "host": "",
            "port": 4660,
            "monitor_id": 1,
            "poll_interval": 30,
            "inter_command_delay": 0.05,
            "baudrate": 115200,
            "parity": "N",
            "bytesize": 8,
            "stopbits": 1,
            "mac_address": "",
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "Display IP address. LAN control uses TCP port 4660.",
            },
            "port": {
                "type": "integer", "default": 4660, "label": "TCP Port",
                "description": "Default 4660 for BenQ LAN control",
            },
            "monitor_id": {
                "type": "integer", "default": 1, "min": 1, "max": 98,
                "label": "Monitor ID",
                "description": "Leave at 1 for LAN control. RS-232 daisy chains on signage can address IDs 1-98.",
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
                "type": "integer", "default": 115200, "label": "Baud Rate (serial)",
                "description": "115200 for current BenQ Boards; 9600 for older RP panels and Smart Signage.",
            },
            "mac_address": {
                "type": "string", "default": "", "label": "MAC Address (for Wake-on-LAN)",
                "description": "Optional. Learned automatically on the first connection; fill in manually to wake a display that has never connected.",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum", "values": ["on", "screen_off", "standby"],
                "label": "Power", "control": True,
                "help": "on = picture up; screen_off = backlight off + mute with control still available; standby = Android off (LAN control port dead until woken).",
            },
            "source": {
                "type": "string", "label": "Source", "control": True,
                "help": "Active input (hdmi, hdmi1, hdmi2, displayport, typec, vga, android, ops, ezwrite, wifi). Unknown codes read back as source_<code>.",
            },
            "signal_stable": {
                "type": "boolean", "label": "Signal Present",
                "help": "Whether the current input has a stable video signal.",
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
            "saturation": {
                "type": "integer", "label": "Saturation", "min": 0, "max": 100, "step": 1,
                "help": "Color saturation, 0-100.",
            },
            "hue": {
                "type": "integer", "label": "Hue", "min": 0, "max": 100, "step": 1,
                "help": "Picture hue, 0-100.",
            },
            "backlight": {
                "type": "integer", "label": "Backlight", "control": True,
                "min": 0, "max": 100, "step": 1,
                "help": "Backlight level, 0-100.",
            },
            "picture_mode": {
                "type": "enum",
                "values": ["standard", "bright", "soft", "eco", "custom1", "custom2", "custom3"],
                "label": "Picture Mode",
                "help": "Active picture mode.",
            },
            "color_temp": {
                "type": "enum", "values": ["cool", "normal", "warm"],
                "label": "Color Temperature",
                "help": "Color temperature preset.",
            },
            "dcr": {
                "type": "boolean", "label": "Dynamic Contrast (DCR)",
                "help": "Dynamic contrast ratio on/off.",
            },
            "aspect": {
                "type": "enum", "values": ["16:9", "ptp"],
                "label": "Aspect Ratio",
                "help": "16:9 or PTP (point-to-point / 1:1 pixel mapping).",
            },
            "sound_mode": {
                "type": "enum",
                "values": ["movie", "standard", "custom", "class", "meeting"],
                "label": "Sound Mode",
                "help": "Active sound mode preset.",
            },
            "treble": {
                "type": "integer", "label": "Treble", "min": 0, "max": 100, "step": 1,
                "help": "Treble level, 0-100.",
            },
            "bass": {
                "type": "integer", "label": "Bass", "min": 0, "max": 100, "step": 1,
                "help": "Bass level, 0-100.",
            },
            "balance": {
                "type": "integer", "label": "Balance", "min": 0, "max": 100, "step": 1,
                "help": "Audio balance, 0-100 (50 = centered).",
            },
            "ir_lock": {
                "type": "enum", "values": ["locked", "unlocked"],
                "label": "IR Remote Lock",
                "help": "Whether the display's IR remote receiver is locked out.",
            },
            "keypad_lock": {
                "type": "enum", "values": ["locked", "unlocked"],
                "label": "Keypad Lock",
                "help": "Whether the display's physical keypad is locked out.",
            },
            "power_save": {
                "type": "enum", "values": ["off", "low", "high"],
                "label": "Power Save Mode",
                "help": "Power save mode reported by the display.",
            },
            "switch_on_status": {
                "type": "enum", "values": ["power_off", "force_on", "last_status"],
                "label": "Switch-On Behavior",
                "help": "What the display does when mains power is applied.",
            },
            "wol_enabled": {
                "type": "boolean", "label": "Wake-on-LAN Enabled",
                "help": "Whether the display's Wake-on-LAN setting is on. Required to wake a standby display remotely.",
            },
            "operation_hours": {
                "type": "integer", "label": "Operation Time",
                "help": "Cumulative operation counter reported by the display (typically hours).",
            },
            "model_name": {
                "type": "string", "label": "Model",
                "help": "Model name reported by the display.",
            },
            "serial_number": {
                "type": "string", "label": "Serial Number",
                "help": "Serial number reported by the display.",
            },
            "firmware_version": {
                "type": "string", "label": "Firmware Version",
                "help": "Scaler firmware version reported by the display.",
            },
            "mac_address": {
                "type": "string", "label": "MAC Address",
                "help": "Network MAC address reported by the display; used by the Wake Display action.",
            },
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "help": "Turn the picture on. Works while the display is reachable (on or screen-off). A display in standby is unreachable over LAN — use the Wake Display (Wake-on-LAN) action instead.",
                "params": {},
            },
            "screen_off": {
                "label": "Screen Off",
                "help": "Backlight off + mute. Network control stays available — the recommended 'off' for automated spaces.",
                "params": {},
            },
            "standby": {
                "label": "Standby (Android Off)",
                "help": "Full standby: Android shuts down and the LAN control port goes dead. Wake with the Wake Display action or at the panel.",
                "params": {},
            },
            "reboot": {
                "label": "Reboot",
                "help": "Reboot the display. It drops offline until it restarts.",
                "params": {},
            },
            "set_source": {
                "label": "Select Source",
                "help": "Select an input. Sources a given model lacks answer a reject the driver ignores.",
                "params": {
                    "source": {
                        "type": "enum", "required": True, "label": "Source",
                        "values": SOURCE_LABELS,
                    },
                },
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {"level": _num("Volume (0-100)")},
            },
            "mute_on": {"label": "Mute On", "params": {}},
            "mute_off": {"label": "Mute Off", "params": {}},
            "volume_up": {"label": "Volume Up", "params": {}},
            "volume_down": {"label": "Volume Down", "params": {}},
            "set_contrast": {
                "label": "Set Contrast",
                "params": {"level": _num("Contrast (0-100)")},
            },
            "set_brightness": {
                "label": "Set Brightness",
                "params": {"level": _num("Brightness (0-100)")},
            },
            "set_sharpness": {
                "label": "Set Sharpness",
                "params": {"level": _num("Sharpness (0-100)")},
            },
            "set_saturation": {
                "label": "Set Saturation",
                "params": {"level": _num("Saturation (0-100)")},
            },
            "set_hue": {
                "label": "Set Hue",
                "params": {"level": _num("Hue (0-100)")},
            },
            "set_backlight": {
                "label": "Set Backlight",
                "params": {"level": _num("Backlight (0-100)")},
            },
            "set_treble": {
                "label": "Set Treble",
                "params": {"level": _num("Treble (0-100)")},
            },
            "set_bass": {
                "label": "Set Bass",
                "params": {"level": _num("Bass (0-100)")},
            },
            "set_balance": {
                "label": "Set Balance",
                "params": {"level": _num("Balance (0-100)")},
            },
            "set_picture_mode": {
                "label": "Set Picture Mode",
                "params": {
                    "mode": {
                        "type": "enum", "required": True, "label": "Picture Mode",
                        "values": PICTURE_MODE_LABELS,
                    },
                },
            },
            "set_sound_mode": {
                "label": "Set Sound Mode",
                "params": {
                    "mode": {
                        "type": "enum", "required": True, "label": "Sound Mode",
                        "values": [
                            {"value": "standard", "label": "Standard"},
                            {"value": "movie", "label": "Movie"},
                            {"value": "custom", "label": "Custom"},
                            {"value": "class", "label": "Class"},
                            {"value": "meeting", "label": "Meeting"},
                        ],
                    },
                },
            },
            "set_color_temp": {
                "label": "Set Color Temperature",
                "params": {
                    "temp": {
                        "type": "enum", "required": True, "label": "Color Temperature",
                        "values": [
                            {"value": "cool", "label": "Cool"},
                            {"value": "normal", "label": "Normal"},
                            {"value": "warm", "label": "Warm"},
                        ],
                    },
                },
            },
            "set_aspect": {
                "label": "Set Aspect Ratio",
                "params": {
                    "aspect": {
                        "type": "enum", "required": True, "label": "Aspect Ratio",
                        "values": [
                            {"value": "16:9", "label": "16:9"},
                            {"value": "ptp", "label": "PTP (1:1 Pixel)"},
                        ],
                    },
                },
            },
            "remote_key": {
                "label": "Press Remote Key",
                "help": "Send a virtual remote-control key press.",
                "params": {
                    "key": {
                        "type": "enum", "required": True, "label": "Key",
                        "values": [
                            {"value": "up", "label": "Up"},
                            {"value": "down", "label": "Down"},
                            {"value": "left", "label": "Left"},
                            {"value": "right", "label": "Right"},
                            {"value": "ok", "label": "OK"},
                            {"value": "menu", "label": "Menu"},
                            {"value": "exit", "label": "Exit"},
                            {"value": "vol_up", "label": "Volume Up"},
                            {"value": "vol_down", "label": "Volume Down"},
                        ],
                    },
                },
            },
            "blank_toggle": {
                "label": "Blank (Toggle)",
                "help": "Virtual remote key that toggles picture blank. The protocol has no blank read-back, so no state is tracked.",
                "params": {},
            },
            "freeze_toggle": {
                "label": "Freeze (Toggle)",
                "help": "Virtual remote key that toggles image freeze. The protocol has no freeze read-back, so no state is tracked.",
                "params": {},
            },
            "picture_reset": {
                "label": "Reset Picture Settings",
                "params": {},
            },
            "sound_reset": {
                "label": "Reset Sound Settings",
                "params": {},
            },
            "raw_command": {
                "label": "Send Raw Command",
                "help": "Send any set/get from your model's RS232 & LAN guide: pick the type, give the command code as two hex digits (e.g. 24 for brightness) and the ASCII value field (e.g. 076).",
                "params": {
                    "cmd_type": {
                        "type": "enum", "required": True, "label": "Type",
                        "values": [
                            {"value": "s", "label": "Set"},
                            {"value": "g", "label": "Get"},
                        ],
                    },
                    "code": {
                        "type": "string", "required": True, "label": "Command Code (hex)",
                        "pattern": "^[0-9A-Fa-f]{2}$",
                    },
                    "value": {
                        "type": "string", "required": False, "label": "Value",
                        "pattern": "^[0-9]{3,5}$",
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
            "picture_mode": {
                "type": "enum",
                "values": PICTURE_MODE_LABELS,
                "label": "Picture Mode",
                "help": "Persisted picture mode preset.",
                "state_key": "picture_mode", "default": "standard", "setup": False,
            },
            "color_temp": {
                "type": "enum",
                "values": [
                    {"value": "cool", "label": "Cool"},
                    {"value": "normal", "label": "Normal"},
                    {"value": "warm", "label": "Warm"},
                ],
                "label": "Color Temperature",
                "help": "Persisted color temperature preset.",
                "state_key": "color_temp", "default": "normal", "setup": False,
            },
            "sound_mode": {
                "type": "enum",
                "values": [
                    {"value": "standard", "label": "Standard"},
                    {"value": "movie", "label": "Movie"},
                    {"value": "custom", "label": "Custom"},
                    {"value": "class", "label": "Class"},
                    {"value": "meeting", "label": "Meeting"},
                ],
                "label": "Sound Mode",
                "help": "Persisted sound mode preset.",
                "state_key": "sound_mode", "default": "standard", "setup": False,
            },
            "power_save": {
                "type": "enum",
                "values": [
                    {"value": "off", "label": "Off"},
                    {"value": "low", "label": "Low"},
                    {"value": "high", "label": "High"},
                ],
                "label": "Power Save Mode",
                "help": "Display power-save mode.",
                "state_key": "power_save", "default": "off", "setup": False,
            },
            "switch_on_status": {
                "type": "enum",
                "values": [
                    {"value": "last_status", "label": "Last Status"},
                    {"value": "force_on", "label": "Force On"},
                    {"value": "power_off", "label": "Power Off"},
                ],
                "label": "Switch-On Behavior",
                "help": "State the display enters when mains power is applied.",
                "state_key": "switch_on_status", "default": "last_status", "setup": False,
            },
            "wol": {
                "type": "boolean",
                "label": "Wake-on-LAN",
                "help": "Keep on so a standby display can be woken with the Wake Display action.",
                "state_key": "wol_enabled", "default": True, "setup": False,
            },
            "ir_lock": {
                "type": "enum",
                "values": [
                    {"value": "unlocked", "label": "Unlocked"},
                    {"value": "locked", "label": "Locked"},
                ],
                "label": "IR Remote Lock",
                "help": "Lock out the display's IR remote receiver.",
                "state_key": "ir_lock", "default": "unlocked", "setup": False,
            },
            "keypad_lock": {
                "type": "enum",
                "values": [
                    {"value": "unlocked", "label": "Unlocked"},
                    {"value": "locked", "label": "Locked"},
                ],
                "label": "Keypad Lock",
                "help": "Lock out the display's physical keypad.",
                "state_key": "keypad_lock", "default": "unlocked", "setup": False,
            },
        },
        "quick_actions": ["power_on", "screen_off"],
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
        "protocols": ["benq_rs232_lan"],
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any):
        super().__init__(device_id, config, state, events)
        self._monitor_id = int(config.get("monitor_id", 1) or 1)

    # ── Packet build / send ────────────────────────────────────────────────

    def _packet(self, cmd_type: str, code: int, value: bytes = b"000") -> bytes:
        """Render one framed packet: length, ID, type, code, value, CR."""
        body = (
            f"{self._monitor_id:02d}".encode("ascii")
            + cmd_type.encode("ascii")
            + bytes([code])
            + value
        )
        # Length byte counts every byte except the CR, itself included.
        return bytes([0x30 + len(body) + 1]) + body + b"\r"

    async def _send(self, cmd_type: str, code: int, value: bytes = b"000") -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(self._packet(cmd_type, code, value))

    async def _set(self, code: int, value: str) -> bool:
        await self._send("s", code, value.encode("ascii"))
        return True

    async def _get(self, code: int, value: bytes = b"000") -> None:
        await self._send("g", code, value)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def _post_connect(self) -> None:
        # Identity + the MAC the Wake Display action needs. Fire-and-forget:
        # replies land in on_data_received; a model that rejects one of these
        # answers '-' and the state simply stays unset.
        await self._get(GET_MODEL_INFO, b"\x02" + b"\x00" * 14)  # model name
        await self._get(GET_MODEL_INFO, b"\x04" + b"\x00" * 14)  # scaler firmware
        await self._get(GET_MODEL_INFO, b"\x06" + b"\x00" * 14)  # serial number
        await self._get(GET_NETWORK, b"\x06" + b"\x00" * 8)      # MAC address

    async def poll(self) -> None:
        for code in _POLL_GETS:
            await self._get(code)
        # Operation time is a five-digit value field (packet length 10).
        await self._get(GET_OPERATION_TIME, b"00000")

    # ── Commands ───────────────────────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        simple = {
            "power_on": (SET_POWER, "001"),
            "screen_off": (SET_POWER, "000"),
            "standby": (SET_POWER, "002"),
            "reboot": (SET_POWER, "003"),
            "mute_on": (SET_MUTE, "001"),
            "mute_off": (SET_MUTE, "000"),
            "volume_up": (SET_REMOTE, REMOTE_KEYS["vol_up"]),
            "volume_down": (SET_REMOTE, REMOTE_KEYS["vol_down"]),
            "blank_toggle": (SET_REMOTE, REMOTE_KEYS["blank"]),
            "freeze_toggle": (SET_REMOTE, REMOTE_KEYS["freeze"]),
            "picture_reset": (SET_PICTURE_RESET, "000"),
            "sound_reset": (SET_SOUND_RESET, "000"),
        }
        if command in simple:
            code, value = simple[command]
            return await self._set(code, value)

        level_sets = {
            "set_volume": SET_VOLUME,
            "set_contrast": SET_CONTRAST,
            "set_brightness": SET_BRIGHTNESS,
            "set_sharpness": SET_SHARPNESS,
            "set_saturation": SET_SATURATION,
            "set_hue": SET_HUE,
            "set_backlight": SET_BACKLIGHT,
            "set_treble": SET_TREBLE,
            "set_bass": SET_BASS,
            "set_balance": SET_BALANCE,
        }
        if command in level_sets:
            level = int(params["level"])
            return await self._set(level_sets[command], f"{level:03d}")

        if command == "set_source":
            token = str(params["source"])
            code3 = SOURCE_TOKENS.get(token)
            if code3 is None:
                raise ValueError(f"Unknown source '{token}'")
            return await self._set(SET_SOURCE, code3)

        if command == "set_picture_mode":
            return await self._set(SET_PICTURE_MODE, PICTURE_MODES[str(params["mode"])])

        if command == "set_sound_mode":
            return await self._set(SET_SOUND_MODE, SOUND_MODES[str(params["mode"])])

        if command == "set_color_temp":
            return await self._set(SET_COLOR_TEMP, COLOR_TEMPS[str(params["temp"])])

        if command == "set_aspect":
            return await self._set(SET_ASPECT, ASPECTS[str(params["aspect"])])

        if command == "remote_key":
            key = str(params["key"])
            if key not in REMOTE_KEYS:
                raise ValueError(f"Unknown remote key '{key}'")
            return await self._set(SET_REMOTE, REMOTE_KEYS[key])

        if command == "raw_command":
            cmd_type = str(params["cmd_type"])
            code = int(str(params["code"]), 16)
            value = str(params.get("value") or "000")
            await self._send(cmd_type, code, value.encode("ascii"))
            return True

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    # ── Device settings ────────────────────────────────────────────────────

    async def set_device_setting(self, key: str, value: Any) -> Any:
        writers = {
            "backlight": lambda v: (SET_BACKLIGHT, f"{int(v):03d}", GET_BACKLIGHT),
            "picture_mode": lambda v: (SET_PICTURE_MODE, PICTURE_MODES[str(v)], GET_PICTURE_MODE),
            "color_temp": lambda v: (SET_COLOR_TEMP, COLOR_TEMPS[str(v)], GET_COLOR_TEMP),
            "sound_mode": lambda v: (SET_SOUND_MODE, SOUND_MODES[str(v)], GET_SOUND_MODE),
            "power_save": lambda v: (SET_POWER_SAVE, POWER_SAVE_MODES[str(v)], GET_POWER_SAVE),
            "switch_on_status": lambda v: (SET_SWITCH_ON, SWITCH_ON_MODES[str(v)], GET_SWITCH_ON),
            "wol": lambda v: (SET_WOL, "001" if v else "000", GET_WOL),
            # 000 = Disable(d) = locked on the wire.
            "ir_lock": lambda v: (SET_IR_LOCK, "000" if str(v) == "locked" else "001", GET_IR_LOCK),
            "keypad_lock": lambda v: (SET_KEYPAD_LOCK, "000" if str(v) == "locked" else "001", GET_KEYPAD_LOCK),
        }
        writer = writers.get(key)
        if writer is None:
            raise ValueError(f"Unknown device setting '{key}'")
        set_code, wire_value, get_code = writer(value)
        await self._set(set_code, wire_value)
        # Read straight back so the settings editor reflects the device.
        await self._get(get_code)
        return True

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
            return  # set accepted; polling / explicit read-back refreshes state
        if ftype == b"-":
            log.debug(f"[{self.device_id}] Command rejected by display")
            return
        if ftype != b"r" or len(data) < 6:
            return
        code = data[4]
        payload = data[5:]
        self._apply_reply(code, payload)

    def _apply_reply(self, code: int, payload: bytes) -> None:
        if code == GET_MODEL_INFO and payload:
            sub = payload[0]
            text = payload[1:].rstrip(b"\x00").decode("ascii", errors="replace").strip()
            key = {0x02: "model_name", 0x04: "firmware_version", 0x06: "serial_number"}.get(sub)
            if key and text:
                self.set_state(key, text)
            return

        if code == GET_NETWORK and payload:
            sub = payload[0]
            if sub in (0x06, 0x07) and len(payload) >= 7:
                mac = ":".join(f"{b:02x}" for b in payload[1:7])
                if mac != "00:00:00:00:00:00":
                    self.set_state("mac_address", mac)
            return

        text = payload.decode("ascii", errors="replace")
        if not text.isdigit():
            return

        if code in _NUMERIC_GETS:
            self.set_state(_NUMERIC_GETS[code], int(text))
        elif code == GET_POWER:
            state = POWER_CODES.get(text)
            if state:
                self.set_state("power", state)
        elif code == GET_SOURCE:
            self.set_state("source", SOURCE_CODES.get(text, f"source_{text}"))
        elif code == GET_SIGNAL:
            self.set_state("signal_stable", text == "001")
        elif code == GET_MUTE:
            self.set_state("mute", text == "001")
        elif code == GET_SOUND_MODE:
            name = SOUND_MODE_NAMES.get(text)
            if name:
                self.set_state("sound_mode", name)
        elif code == GET_ASPECT:
            name = ASPECT_NAMES.get(text)
            if name:
                self.set_state("aspect", name)
        elif code == GET_PICTURE_MODE:
            name = PICTURE_MODE_NAMES.get(text)
            if name:
                self.set_state("picture_mode", name)
        elif code == GET_COLOR_TEMP:
            name = COLOR_TEMP_NAMES.get(text)
            if name:
                self.set_state("color_temp", name)
        elif code == GET_DCR:
            self.set_state("dcr", text == "001")
        elif code == GET_IR_LOCK:
            self.set_state("ir_lock", "locked" if text == "000" else "unlocked")
        elif code == GET_KEYPAD_LOCK:
            self.set_state("keypad_lock", "locked" if text == "000" else "unlocked")
        elif code == GET_POWER_SAVE:
            name = POWER_SAVE_NAMES.get(text)
            if name:
                self.set_state("power_save", name)
        elif code == GET_SWITCH_ON:
            name = SWITCH_ON_NAMES.get(text)
            if name:
                self.set_state("switch_on_status", name)
        elif code == GET_WOL:
            self.set_state("wol_enabled", text == "001")
        elif code == GET_OPERATION_TIME:
            self.set_state("operation_hours", int(text))

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
        magic = b"\xff" * 6 + mac_bytes * 16
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
            "Magic packet sent. The display takes a little while to boot; "
            "reconnecting.",
            pct=70,
        )
        try:
            await self.request_reconnect()
        except Exception:
            # Still booting — the normal auto-reconnect loop keeps retrying.
            pass
        await progress("Done. If the display stays offline, verify its Wake-on-LAN setting is on.", pct=100)
        return {"mac": mac}
