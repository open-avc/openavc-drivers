"""
OpenAVC Sharp PN-series commercial display driver (pre-merger Sharp
RS-232C / LAN protocol).

Controls legacy Sharp commercial large-format displays and AQUOS BOARD
interactive displays (PN-L / PN-E / PN-R and related PN series) over
TCP (DATA PORT, default 10008) or direct RS-232. These are the
pre-merger Sharp models with Sharp's own 4+4 ASCII command protocol —
post-merger Sharp/NEC large-format displays (current Exx8 etc.) speak
NEC External Control instead and are covered by ``sharp_nec_display``.

Why Python: read replies are BARE VALUES with no command echo
(``VOLM????`` answers just ``30``), so pattern-based YAML response
dispatch cannot attribute replies — the driver serializes one awaited
request at a time and routes each reply by the command that asked.
``WAIT`` interim responses, the ``Login:`` / ``Password:`` prompt
handshake (prompts arrive without a line ending), and the
``LOCKED`` / standby restrictions are handled in the same state
machine.

Wire protocol (ASCII, CR terminated; responses CRLF)::

    Command:  C1 C2 C3 C4 P1 P2 P3 P4 <CR>   (4-char command +
                                               4-char parameter field)
    Response: OK / ERR / WAIT / LOCKED, or the bare value for a read
    Read:     parameter "????" returns the current value ("R" commands)

  - Numeric parameters are zero-padded to 4 chars ("0030"); negative
    values use a three-digit numeral ("-005").
  - "WAIT" is returned by slow commands (POWR, INPS, WIDE, MWIN, ...);
    the real response follows. The driver extends its response window
    when WAIT arrives.
  - On LAN the monitor always prompts ``Login:`` / ``Password:`` after
    connect (send blank lines when no credentials are set); RS-232C
    has no login.
  - Replies on an RS-232C daisy chain carry a trailing ID suffix
    ("OK 001"); the driver strips it from ack lines. Full multi-monitor
    chain control (stateful IDSL/IDLK targeting) is not modeled yet —
    this driver addresses the directly-connected monitor.

Push vs poll: POLL-ONLY. The protocol is strictly request/response —
no notification mechanism is documented — so the driver polls the "R"
commands and pulls the model name and serial number once on connect.

Power model (matters for automation):
  - POWR reads 0 standby / 1 on / 2 input-signal waiting.
  - STANDBY MODE (STBM) decides whether a standby display stays
    controllable: STANDARD keeps RS-232C/LAN control alive in standby;
    LOW POWER shuts control down until the display is woken at the
    panel. The driver exposes STBM as a device setting and the help
    steers integrators to STANDARD. No Wake-on-LAN exists in this
    protocol.

Protocol reference: Sharp PN-L603B/PN-L703B Operation Manual,
"Controlling the Monitor with a PC (LAN)" (command table pp. 50-56);
corroborated against the PN-E603/E703 manual (RS-232C chapter: baud,
daisy-chain ID grammar) and Sharp's AQUOS BOARD RS-232C technical
bulletin #14167 (service-port baud fixed at 38400).
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)

# ── Wire tables (PN-L603B operation manual command table) ──────────────────

# INPS input-mode codes. The code space is stable across the PN family —
# a model without a terminal answers ERR for its code.
INPUT_CODES = {
    9: "hdmi1_av",
    10: "hdmi1_pc",
    12: "hdmi2_av",
    13: "hdmi2_pc",
    17: "hdmi3_av",
    18: "hdmi3_pc",
    14: "displayport",
    1: "dvi",
    2: "dsub1_rgb",
    3: "dsub1_component",
    4: "dsub1_video",
    16: "dsub2",
}
INPUT_TOKENS = {v: k for k, v in INPUT_CODES.items()}
INPUT_LABELS = [
    {"value": "hdmi1_pc", "label": "HDMI 1 (PC)"},
    {"value": "hdmi1_av", "label": "HDMI 1 (AV)"},
    {"value": "hdmi2_pc", "label": "HDMI 2 (PC)"},
    {"value": "hdmi2_av", "label": "HDMI 2 (AV)"},
    {"value": "hdmi3_pc", "label": "HDMI 3 (PC)"},
    {"value": "hdmi3_av", "label": "HDMI 3 (AV)"},
    {"value": "displayport", "label": "DisplayPort"},
    {"value": "dvi", "label": "DVI"},
    {"value": "dsub1_rgb", "label": "D-SUB 1 (RGB)"},
    {"value": "dsub1_component", "label": "D-SUB 1 (Component)"},
    {"value": "dsub1_video", "label": "D-SUB 1 (Video)"},
    {"value": "dsub2", "label": "D-SUB 2"},
]

POWER_STATES = {0: "standby", 1: "on", 2: "signal_waiting"}
PIP_MODES = {0: "off", 1: "pip", 2: "pbyp", 3: "pbyp2"}
PIP_MODE_TOKENS = {v: k for k, v in PIP_MODES.items()}
PIP_SOUNDS = {1: "main", 2: "sub"}
STANDBY_MODES = {0: "standard", 1: "low_power"}
STANDBY_MODE_TOKENS = {v: k for k, v in STANDBY_MODES.items()}
ADJUSTMENT_LOCKS = {0: "off", 1: "on1", 2: "on2"}
ADJUSTMENT_LOCK_TOKENS = {v: k for k, v in ADJUSTMENT_LOCKS.items()}
OSD_MODES = {0: "on1", 1: "off", 2: "on2"}
OSD_MODE_TOKENS = {v: k for k, v in OSD_MODES.items()}
TEMP_STATUSES = {
    0: "normal",
    1: "abnormal_standby",
    2: "abnormal",
    3: "abnormal_dimmed",
    4: "sensor_fault",
}
STANDBY_CAUSES = {
    0: "none",
    1: "power_button",
    2: "main_switch",
    3: "lan",
    4: "no_signal",
    6: "thermal",
    8: "schedule",
    20: "no_operation",
}

# Read command -> (state key, kind). Kind decides parsing:
#   int    plain integer (handles negatives)
#   map    integer looked up in a code map
#   bool   integer 0/1 -> False/True
#   text   raw string
_READS: dict[str, tuple[str, str, dict | None]] = {
    "POWR": ("power", "map", POWER_STATES),
    "INPS": ("input", "map", INPUT_CODES),
    "VOLM": ("volume", "int", None),
    "MUTE": ("mute", "bool", None),
    "VLMP": ("brightness", "int", None),
    "CONT": ("contrast", "int", None),
    "BLVL": ("black_level", "int", None),
    "TINT": ("tint", "int", None),
    "COLR": ("color", "int", None),
    "SHRP": ("sharpness", "int", None),
    "AUTR": ("treble", "int", None),
    "AUBS": ("bass", "int", None),
    "AUBL": ("balance", "int", None),
    "WIDE": ("screen_size", "int", None),
    "MWIN": ("pip_mode", "map", PIP_MODES),
    "MWIP": ("pip_source", "map", INPUT_CODES),
    "MWAD": ("pip_sound", "map", PIP_SOUNDS),
    "TPEN": ("touch_enabled", "bool", None),
    "STBM": ("standby_mode", "map", STANDBY_MODES),
    "ALCK": ("adjustment_lock", "map", ADJUSTMENT_LOCKS),
    "LOSD": ("osd_display", "map", OSD_MODES),
    # OFLD: wire 0 = LED ON, 1 = OFF (inverted).
    "OFLD": ("led_enabled", "led", None),
    "DSTA": ("temp_status", "map", TEMP_STATUSES),
    "ERRT": ("temperature_c", "int", None),
    "STCA": ("last_standby_cause", "map", STANDBY_CAUSES),
    "INF1": ("model_name", "text", None),
    "SRNO": ("serial_number", "text", None),
    "PXCK": ("input_resolution", "text", None),
    "RESO": ("input_resolution", "text", None),
}

# The poll cycle. PXCK answers on PC inputs, RESO on AV inputs — the
# other returns ERR, which is ignored. Models without a function (touch,
# PIP) answer ERR the same way.
_POLL_READS = [
    "POWR", "INPS", "VOLM", "MUTE", "VLMP",
    "CONT", "BLVL", "TINT", "COLR", "SHRP",
    "AUTR", "AUBS", "AUBL", "WIDE",
    "MWIN", "MWIP", "MWAD", "TPEN",
    "STBM", "ALCK", "LOSD", "OFLD",
    "DSTA", "ERRT", "STCA",
    "PXCK", "RESO",
]

# Chain-ID reply suffix ("OK 001", "30 001"). No polled value legitimately
# ends in whitespace + exactly three digits, so the strip is unconditional.
_ID_SUFFIX_RE = re.compile(r"\s\d{3}$")
_PROMPT_TIMEOUT = 10.0
_REPLY_TIMEOUT = 3.0
_WAIT_EXTENSION = 15.0
_AUTH_MAX_BUFFER = 4096


def _num(label: str, minimum: int, maximum: int) -> dict:
    return {
        "type": "integer", "required": True, "label": label,
        "min": minimum, "max": maximum,
    }


class SharpPnDisplayDriver(BaseDriver):
    """Sharp PN-series (pre-merger) RS-232C / LAN protocol driver."""

    DRIVER_INFO = {
        "id": "sharp_pn_display",
        "name": "Sharp PN Display (AQUOS BOARD)",
        "manufacturer": "Sharp",
        "category": "display",
        "version": "1.0.2",
        "author": "OpenAVC",
        "description": (
            "Controls pre-merger Sharp PN-series commercial displays and "
            "AQUOS BOARD interactive displays (PN-L, PN-E, PN-R and related "
            "series) using Sharp's 4-character RS-232C/LAN command protocol "
            "on TCP port 10008 or direct RS-232. Power, input select, "
            "volume, mute, brightness and picture values, audio EQ, screen "
            "size, PIP/PbyP, touch enable, control locks, temperature "
            "monitoring, and standby-mode configuration. Polls for status. "
            "Post-merger Sharp/NEC displays are covered by the Sharp NEC "
            "Display driver instead."
        ),
        "source_url": "https://business.sharpusa.com/portals/0/downloads/Manuals/PN_L603B_L703B_Operation_Manual.pdf",
        "tags": ["display", "sharp", "aquos-board", "pn-series", "signage", "rs232", "lan-control"],
        "verified": False,
        "simulated": True,
        "ports": [10008],
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "delimiter": "\r\n",
        "compatible_models": [
            {
                "manufacturer": "Sharp",
                "models": [
                    "PN-L603B", "PN-L703B", "PN-L603W", "PN-L703W",
                    "PN-E603", "PN-E703", "PN-R603",
                ],
                "confidence": "untested",
                "notes": (
                    "Every listed model's operation manual documents this "
                    "command table. Other pre-merger Sharp PN-series "
                    "monitors and AQUOS BOARD models publish the same "
                    "protocol and are expected to work; input terminals and "
                    "optional functions (touch, PIP) vary by model, and an "
                    "unsupported command answers a reject the driver "
                    "ignores. Current post-merger Sharp/NEC large-format "
                    "displays use NEC External Control instead — add those "
                    "with the Sharp NEC Display driver."
                ),
            },
        ],
        "help": {
            "overview": (
                "Pre-merger Sharp PN-series commercial displays and AQUOS "
                "BOARD interactive displays. Power, input, audio, picture "
                "controls, PIP, touch enable, control locks, and "
                "temperature monitoring. Polls every 30 seconds. Keep "
                "STANDBY MODE on STANDARD so a standby display stays "
                "controllable over the network."
            ),
            "setup": (
                "1. Network control: connect the display's LAN port and set "
                "RS-232C/LAN SELECT to LAN in the SETUP menu. Add the "
                "display's IP address here (TCP data port, default 10008). "
                "If a username/password is set in the display's SECURITY "
                "settings, enter the same values here; leave blank "
                "otherwise.\n"
                "2. Keep STANDBY MODE set to STANDARD (Monitor menu). LOW "
                "POWER standby shuts down LAN/RS-232C control until the "
                "display is woken at the panel.\n"
                "3. Serial control: connect the RS-232C input terminal "
                "(straight cable) and set RS-232C/LAN SELECT to RS-232C. "
                "Default 38400 8N1 — match the display's BAUD RATE "
                "setting. AQUOS BOARD models use a 3.5mm service jack "
                "fixed at 38400.\n"
                "4. RS-232C and LAN control cannot be used simultaneously."
            ),
            "connection": (
                "A display in LOW POWER standby does not answer on LAN or "
                "RS-232C until woken at the panel — set STANDBY MODE to "
                "STANDARD (the Standby Mode device setting) so power_on "
                "keeps working. The LAN session requires a login; blank "
                "credentials are sent when none are configured."
            ),
        },
        "discovery": {
            # Banner fingerprint: the monitor prompts "Login:" immediately
            # after a TCP connect on the data port — no probe bytes needed.
            "tcp_probe": {
                "port": 10008,
                "expect": "Login:",
            },
            "manufacturer_alias": ["sharp", "sharp corporation"],
        },
        "default_config": {
            "host": "",
            "port": 10008,
            "username": "",
            "password": "",
            "poll_interval": 30,
            "inter_command_delay": 0.1,
            "baudrate": 38400,
            "parity": "N",
            "bytesize": 8,
            "stopbits": 1,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "Display IP address. LAN control uses the display's DATA PORT (default 10008).",
            },
            "port": {
                "type": "integer", "default": 10008, "label": "TCP Port",
                "description": "The display's DATA PORT setting. Factory default 10008.",
            },
            "username": {
                "type": "string", "default": "", "label": "Username",
                "description": "Username from the display's SECURITY settings. Leave blank if none is set.",
            },
            "password": {
                "type": "string", "default": "", "label": "Password",
                "secret": True,
                "description": "Password from the display's SECURITY settings. Leave blank if none is set.",
            },
            "poll_interval": {
                "type": "integer", "default": 30, "min": 5,
                "label": "Poll Interval (sec)",
            },
            "inter_command_delay": {
                "type": "number", "default": 0.1,
                "label": "Inter-Command Delay (sec)",
                "description": "The protocol requires 100 ms between a response and the next command.",
            },
            "baudrate": {
                "type": "integer", "default": 38400, "label": "Baud Rate (serial)",
                "description": "Match the display's BAUD RATE setting (9600/19200/38400; default 38400).",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum", "values": ["on", "standby", "signal_waiting"],
                "label": "Power", "control": True,
                "help": "on = picture up; standby = off; signal_waiting = on with no input signal.",
            },
            "input": {
                "type": "string", "label": "Input", "control": True,
                "help": "Active input (hdmi1_pc, hdmi1_av, hdmi2_pc, hdmi2_av, hdmi3_pc, hdmi3_av, displayport, dvi, dsub1_rgb, dsub1_component, dsub1_video, dsub2). Unknown codes read back as input_<code>.",
            },
            "volume": {
                "type": "integer", "label": "Volume", "control": True,
                "min": 0, "max": 31, "step": 1,
                "help": "Speaker volume, 0-31.",
            },
            "mute": {
                "type": "boolean", "label": "Mute", "control": True,
                "help": "Audio mute state.",
            },
            "brightness": {
                "type": "integer", "label": "Brightness", "control": True,
                "min": 0, "max": 31, "step": 1,
                "help": "Backlight brightness (the PICTURE menu's BRIGHT value), 0-31.",
            },
            "contrast": {
                "type": "integer", "label": "Contrast", "min": 0, "max": 60, "step": 1,
                "help": "Picture contrast, 0-60.",
            },
            "black_level": {
                "type": "integer", "label": "Black Level", "min": 0, "max": 60, "step": 1,
                "help": "Picture black level, 0-60.",
            },
            "tint": {
                "type": "integer", "label": "Tint", "min": 0, "max": 60, "step": 1,
                "help": "Picture tint, 0-60.",
            },
            "color": {
                "type": "integer", "label": "Colors", "min": 0, "max": 60, "step": 1,
                "help": "Color intensity, 0-60.",
            },
            "sharpness": {
                "type": "integer", "label": "Sharpness", "min": 0, "max": 24, "step": 1,
                "help": "Picture sharpness, 0-24.",
            },
            "treble": {
                "type": "integer", "label": "Treble", "min": -5, "max": 5, "step": 1,
                "help": "Treble level, -5 to +5.",
            },
            "bass": {
                "type": "integer", "label": "Bass", "min": -5, "max": 5, "step": 1,
                "help": "Bass level, -5 to +5.",
            },
            "balance": {
                "type": "integer", "label": "Balance", "min": -10, "max": 10, "step": 1,
                "help": "Audio balance, -10 (left) to +10 (right).",
            },
            "screen_size": {
                "type": "integer", "label": "Screen Size", "min": 1, "max": 5,
                "help": "WIDE mode number. PC input: 1 Wide, 2 Normal, 3 Dot by Dot, 4 Zoom 1, 5 Zoom 2. AV input: 1 Wide, 2 Zoom 1, 3 Zoom 2, 4 Normal, 5 Dot by Dot.",
            },
            "pip_mode": {
                "type": "enum", "values": ["off", "pip", "pbyp", "pbyp2"],
                "label": "PIP Mode",
                "help": "Picture-in-picture / picture-by-picture mode (model-dependent).",
            },
            "pip_source": {
                "type": "string", "label": "PIP Source",
                "help": "Source shown in the PIP window (same tokens as Input).",
            },
            "pip_sound": {
                "type": "enum", "values": ["main", "sub"],
                "label": "PIP Sound",
                "help": "Which picture's audio plays while PIP is active.",
            },
            "touch_enabled": {
                "type": "boolean", "label": "Touch Enabled",
                "help": "Whether touch operation is enabled (AQUOS BOARD / touch models with the panel connected).",
            },
            "standby_mode": {
                "type": "enum", "values": ["standard", "low_power"],
                "label": "Standby Mode",
                "help": "STANDARD keeps LAN/RS-232C control alive in standby. LOW POWER shuts control down until the display is woken at the panel.",
            },
            "adjustment_lock": {
                "type": "enum", "values": ["off", "on1", "on2"],
                "label": "Adjustment Lock",
                "help": "Front-button / remote adjustment lock. ON1 allows power operations; ON2 locks power too.",
            },
            "osd_display": {
                "type": "enum", "values": ["on1", "off", "on2"],
                "label": "OSD Display",
                "help": "On-screen display visibility mode.",
            },
            "led_enabled": {
                "type": "boolean", "label": "Power LED",
                "help": "Whether the power LED lights.",
            },
            "temp_status": {
                "type": "enum",
                "values": ["normal", "abnormal_standby", "abnormal", "abnormal_dimmed", "sensor_fault"],
                "label": "Temperature Status",
                "help": "Internal temperature monitor: normal; abnormal_standby = overheated into standby; abnormal = overheat recorded; abnormal_dimmed = backlight dimmed to cool; sensor_fault = sensor abnormality.",
            },
            "temperature_c": {
                "type": "integer", "label": "Temperature (C)",
                "help": "Internal temperature sensor reading, degrees Celsius. 126 indicates a sensor abnormality.",
            },
            "last_standby_cause": {
                "type": "enum",
                "values": ["none", "power_button", "main_switch", "lan", "no_signal", "thermal", "schedule", "no_operation"],
                "label": "Last Standby Cause",
                "help": "Why the display last entered standby — useful when diagnosing a display found off.",
            },
            "input_resolution": {
                "type": "string", "label": "Input Resolution",
                "help": "Current input resolution (pixel count on PC inputs, signal format on AV inputs).",
            },
            "model_name": {
                "type": "string", "label": "Model",
                "help": "Model name reported by the display.",
            },
            "serial_number": {
                "type": "string", "label": "Serial Number",
                "help": "Serial number reported by the display.",
            },
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "help": "Turn the display on. Works from standby only while STANDBY MODE is STANDARD (LOW POWER standby shuts control down).",
                "params": {},
            },
            "power_off": {
                "label": "Power Off (Standby)",
                "help": "Put the display in standby.",
                "params": {},
            },
            "set_input": {
                "label": "Select Input",
                "help": "Select an input. Terminals a given model lacks (or that are disabled in INPUT SELECT) answer a reject the driver ignores.",
                "params": {
                    "input": {
                        "type": "enum", "required": True, "label": "Input",
                        "values": INPUT_LABELS,
                    },
                },
            },
            "input_toggle": {
                "label": "Toggle Input",
                "help": "Step to the next available input.",
                "params": {},
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {"level": _num("Volume (0-31)", 0, 31)},
            },
            "mute_on": {"label": "Mute On", "params": {}},
            "mute_off": {"label": "Mute Off", "params": {}},
            "set_brightness": {
                "label": "Set Brightness",
                "params": {"level": _num("Brightness (0-31)", 0, 31)},
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {"level": _num("Contrast (0-60)", 0, 60)},
            },
            "set_black_level": {
                "label": "Set Black Level",
                "params": {"level": _num("Black Level (0-60)", 0, 60)},
            },
            "set_tint": {
                "label": "Set Tint",
                "params": {"level": _num("Tint (0-60)", 0, 60)},
            },
            "set_color": {
                "label": "Set Colors",
                "params": {"level": _num("Colors (0-60)", 0, 60)},
            },
            "set_sharpness": {
                "label": "Set Sharpness",
                "params": {"level": _num("Sharpness (0-24)", 0, 24)},
            },
            "set_treble": {
                "label": "Set Treble",
                "params": {"level": _num("Treble (-5 to +5)", -5, 5)},
            },
            "set_bass": {
                "label": "Set Bass",
                "params": {"level": _num("Bass (-5 to +5)", -5, 5)},
            },
            "set_balance": {
                "label": "Set Balance",
                "params": {"level": _num("Balance (-10 to +10)", -10, 10)},
            },
            "set_screen_size": {
                "label": "Set Screen Size",
                "help": "WIDE mode. The number's meaning differs between PC and AV inputs — see the labels.",
                "params": {
                    "size": {
                        "type": "enum", "required": True, "label": "Screen Size",
                        "values": [
                            {"value": "1", "label": "1 - Wide"},
                            {"value": "2", "label": "2 - Normal (PC) / Zoom 1 (AV)"},
                            {"value": "3", "label": "3 - Dot by Dot (PC) / Zoom 2 (AV)"},
                            {"value": "4", "label": "4 - Zoom 1 (PC) / Normal (AV)"},
                            {"value": "5", "label": "5 - Zoom 2 (PC) / Dot by Dot (AV)"},
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
                            {"value": "pbyp", "label": "PbyP"},
                            {"value": "pbyp2", "label": "PbyP2"},
                        ],
                    },
                },
            },
            "set_pip_source": {
                "label": "Set PIP Source",
                "params": {
                    "input": {
                        "type": "enum", "required": True, "label": "PIP Source",
                        "values": INPUT_LABELS,
                    },
                },
            },
            "set_pip_size": {
                "label": "Set PIP Size",
                "params": {"size": _num("PIP Size (1-64)", 1, 64)},
            },
            "set_pip_sound": {
                "label": "Set PIP Sound",
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
            "screen_motion": {
                "label": "Set Screen Motion",
                "help": "Burn-in reduction pattern (OTHERS menu).",
                "params": {
                    "pattern": {
                        "type": "enum", "required": True, "label": "Pattern",
                        "values": [
                            {"value": "0", "label": "Off"},
                            {"value": "1", "label": "Pattern 1"},
                            {"value": "2", "label": "Pattern 2"},
                            {"value": "3", "label": "Pattern 3"},
                            {"value": "4", "label": "Pattern 4"},
                        ],
                    },
                },
            },
            "raw_command": {
                "label": "Send Raw Command",
                "help": "Send any command from your model's RS-232C/LAN command table: the 4-character command plus its parameter field (e.g. VOLM + 0015, or a read with ????). The reply is applied to state when the command is a known read.",
                "params": {
                    "command": {
                        "type": "string", "required": True, "label": "Command (4 characters)",
                        "pattern": "^[0-9A-Za-z]{4}$",
                    },
                    "parameter": {
                        "type": "string", "required": False, "label": "Parameter",
                        "pattern": "^[0-9A-Za-z?+, -]{0,10}$",
                    },
                },
            },
        },
        "device_settings": {
            "brightness": {
                "type": "integer", "min": 0, "max": 31,
                "label": "Brightness",
                "help": "Backlight brightness, 0-31. Persisted on the display.",
                "state_key": "brightness", "default": 20, "setup": False,
            },
            "standby_mode": {
                "type": "enum",
                "values": [
                    {"value": "standard", "label": "Standard (control stays on)"},
                    {"value": "low_power", "label": "Low Power (control dies in standby)"},
                ],
                "label": "Standby Mode",
                "help": "Keep on Standard so the display can be powered on remotely from standby.",
                "state_key": "standby_mode", "default": "standard", "setup": False,
            },
            "adjustment_lock": {
                "type": "enum",
                "values": [
                    {"value": "off", "label": "Off"},
                    {"value": "on1", "label": "On 1 (power still allowed)"},
                    {"value": "on2", "label": "On 2 (power locked too)"},
                ],
                "label": "Adjustment Lock",
                "help": "Lock the display's own buttons/remote against adjustment.",
                "state_key": "adjustment_lock", "default": "off", "setup": False,
            },
            "osd_display": {
                "type": "enum",
                "values": [
                    {"value": "on1", "label": "On 1"},
                    {"value": "off", "label": "Off"},
                    {"value": "on2", "label": "On 2"},
                ],
                "label": "OSD Display",
                "help": "On-screen display visibility.",
                "state_key": "osd_display", "default": "on1", "setup": False,
            },
            "led_enabled": {
                "type": "boolean",
                "label": "Power LED",
                "help": "Light the power LED.",
                "state_key": "led_enabled", "default": True, "setup": False,
            },
            "touch": {
                "type": "boolean",
                "label": "Touch Operation",
                "help": "Enable touch operation (touch models with the panel connected; not changeable in standby).",
                "state_key": "touch_enabled", "default": True, "setup": False,
            },
        },
        "quick_actions": ["power_on", "power_off"],
        "protocols": ["sharp_pn_rs232c_lan"],
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any):
        super().__init__(device_id, config, state, events)
        self._io_lock = asyncio.Lock()
        self._reply_queue: deque[str] = deque()
        self._reply_event = asyncio.Event()
        # Login handshake plumbing (TCP only; prompts arrive without a
        # line ending, so the frame parser is bypassed for the duration).
        self._auth_mode = False
        self._auth_buffer = bytearray()
        self._auth_event = asyncio.Event()
        self._saved_frame_parser = None

    # ── Connect / login ────────────────────────────────────────────────────

    def _transport_type(self) -> str:
        return self.config.get("transport") or self.DRIVER_INFO.get("transport", "tcp")

    async def _pre_connect(self) -> None:
        # Arm the auth capture BEFORE the transport exists so the Login:
        # banner can't slip past (it arrives immediately on connect).
        if self._transport_type() == "tcp":
            self._auth_mode = True
            self._auth_buffer = bytearray()
            self._auth_event = asyncio.Event()

    async def _close_session(self) -> None:
        # A failed or torn-down attempt must not leave the auth capture armed.
        self._auth_mode = False

    async def _post_connect(self) -> None:
        if self._auth_mode and self.transport is not None:
            # Prompts like "Login:" carry no line ending — drop the frame
            # parser for the handshake so partial lines are visible, and
            # flush anything it already buffered into the auth buffer.
            saved = getattr(self.transport, "_frame_parser", None)
            if hasattr(self.transport, "_frame_parser"):
                if saved is not None and hasattr(saved, "_buffer"):
                    pending = bytes(saved._buffer)
                    if pending:
                        self._auth_buffer.extend(pending)
                        self._auth_event.set()
                        saved._buffer = b""
                self.transport._frame_parser = None
            self._saved_frame_parser = saved
            try:
                await self._perform_login()
            finally:
                self._auth_mode = False
                if self._saved_frame_parser is not None and hasattr(self.transport, "_frame_parser"):
                    self._saved_frame_parser.reset()
                    self.transport._frame_parser = self._saved_frame_parser
                self._saved_frame_parser = None

        self._reply_queue.clear()
        # Identity. A model that rejects either answers ERR, leaving the
        # state unset.
        for cmd in ("INF1", "SRNO"):
            line = await self._request(cmd, "????")
            if line is not None:
                self._apply_read(cmd, line)

    async def _perform_login(self) -> None:
        """LAN sessions always prompt Login:/Password: — send the
        configured credentials, or blank lines when none are set."""
        username = str(self.config.get("username", "") or "")
        password = str(self.config.get("password", "") or "")

        await self._auth_expect(r"Login:")
        await self.transport.send((username + "\r\n").encode("ascii"))
        await self._auth_expect(r"Password:")
        await self.transport.send((password + "\r\n").encode("ascii"))
        # Success is the "OK" line; a re-prompt means the credentials were
        # rejected.
        outcome = await self._auth_expect(r"OK|Login:|Password:|incorrect")
        if outcome != "OK":
            raise ConnectionError(
                f"[{self.device_id}] Authentication failed — the display "
                "rejected the username/password (check its SECURITY settings)"
            )

    async def _auth_expect(self, pattern: str) -> str:
        """Wait until `pattern` appears in the auth buffer; return the match."""
        regex = re.compile(pattern)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PROMPT_TIMEOUT
        while True:
            text = bytes(self._auth_buffer).decode("ascii", errors="replace")
            m = regex.search(text)
            if m:
                del self._auth_buffer[:m.end()]
                return m.group(0)
            if len(self._auth_buffer) > _AUTH_MAX_BUFFER:
                raise ConnectionError(f"[{self.device_id}] Login handshake flooded")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ConnectionError(
                    f"[{self.device_id}] No response to login handshake "
                    f"(waiting for {pattern!r})"
                )
            self._auth_event.clear()
            try:
                await asyncio.wait_for(self._auth_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                continue

    # ── Request/response (single in flight, replies carry no echo) ─────────

    async def _request(self, cmd: str, param: str, timeout: float = _REPLY_TIMEOUT) -> str | None:
        """Send one command and await its response line.

        Returns the final line (acks included), or None on timeout. WAIT
        interim responses extend the window. Serialized: the protocol is
        strictly one command / one response.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._io_lock:
            self._reply_queue.clear()
            self._reply_event.clear()
            await self.transport.send(f"{cmd}{param}\r".encode("ascii"))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                while self._reply_queue:
                    line = self._reply_queue.popleft()
                    if line == "WAIT":
                        deadline = max(deadline, loop.time() + _WAIT_EXTENSION)
                        continue
                    return line
                remaining = deadline - loop.time()
                if remaining <= 0:
                    log.warning(f"[{self.device_id}] No response to {cmd}{param}")
                    return None
                self._reply_event.clear()
                try:
                    await asyncio.wait_for(self._reply_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue

    async def on_data_received(self, data: bytes) -> None:
        if self._auth_mode:
            self._auth_buffer.extend(data)
            self._auth_event.set()
            return
        text = data.decode("ascii", errors="replace").strip()
        if not text:
            return
        # An RS-232C daisy chain suffixes replies with the responder's ID
        # ("OK 001", "30 001"); this driver addresses the directly-connected
        # monitor, so the suffix is stripped wherever it appears.
        text = _ID_SUFFIX_RE.sub("", text)
        self._reply_queue.append(text)
        self._reply_event.set()

    # ── Value formatting / parsing ─────────────────────────────────────────

    @staticmethod
    def _fmt(value: int) -> str:
        """4-char parameter field: zero-padded, negatives as 3-digit numeral."""
        if value < 0:
            return f"-{abs(value):03d}"
        return f"{value:04d}"

    def _apply_read(self, cmd: str, line: str) -> None:
        if line in ("ERR", "LOCKED"):
            return
        entry = _READS.get(cmd)
        if entry is None:
            return
        state_key, kind, mapping = entry
        text = line.strip()
        if kind == "text":
            if text and text != "OK":
                self.set_state(state_key, text)
            return
        try:
            value = int(text)
        except ValueError:
            return
        if kind == "int":
            self.set_state(state_key, value)
        elif kind == "bool":
            self.set_state(state_key, value == 1)
        elif kind == "led":
            self.set_state(state_key, value == 0)
        elif kind == "map":
            if cmd in ("INPS", "MWIP"):
                self.set_state(state_key, mapping.get(value, f"input_{value}"))
            else:
                mapped = mapping.get(value)
                if mapped is not None:
                    self.set_state(state_key, mapped)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def poll(self) -> None:
        for cmd in _POLL_READS:
            line = await self._request(cmd, "????")
            if line is not None:
                self._apply_read(cmd, line)

    # ── Commands ───────────────────────────────────────────────────────────

    async def _write(self, cmd: str, param: str, timeout: float = _REPLY_TIMEOUT) -> bool:
        line = await self._request(cmd, param, timeout=timeout)
        if line == "OK":
            return True
        if line == "LOCKED":
            log.warning(f"[{self.device_id}] Control is locked on the display (operation lock)")
        elif line == "ERR":
            log.debug(f"[{self.device_id}] Display rejected {cmd}{param}")
        return False

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        if command == "power_on":
            # Power-on can take POWER ON DELAY (up to 60 s) — give it room.
            return await self._write("POWR", "0001", timeout=12.0)
        if command == "power_off":
            return await self._write("POWR", "0000", timeout=12.0)
        if command == "input_toggle":
            return await self._write("INPS", "0000", timeout=12.0)
        if command in ("set_input", "set_pip_source"):
            token = str(params["input"])
            code = INPUT_TOKENS.get(token)
            if code is None:
                raise ValueError(f"Unknown input '{token}'")
            cmd = "INPS" if command == "set_input" else "MWIP"
            return await self._write(cmd, self._fmt(code), timeout=12.0)
        if command == "mute_on":
            return await self._write("MUTE", "0001")
        if command == "mute_off":
            return await self._write("MUTE", "0000")

        level_sets = {
            "set_volume": "VOLM",
            "set_brightness": "VLMP",
            "set_contrast": "CONT",
            "set_black_level": "BLVL",
            "set_tint": "TINT",
            "set_color": "COLR",
            "set_sharpness": "SHRP",
            "set_treble": "AUTR",
            "set_bass": "AUBS",
            "set_balance": "AUBL",
        }
        if command in level_sets:
            return await self._write(level_sets[command], self._fmt(int(params["level"])))

        if command == "set_screen_size":
            return await self._write("WIDE", self._fmt(int(params["size"])), timeout=12.0)
        if command == "set_pip_mode":
            return await self._write("MWIN", self._fmt(PIP_MODE_TOKENS[str(params["mode"])]), timeout=12.0)
        if command == "set_pip_size":
            return await self._write("MPSZ", self._fmt(int(params["size"])))
        if command == "set_pip_sound":
            code = 1 if str(params["from_window"]) == "main" else 2
            return await self._write("MWAD", self._fmt(code))
        if command == "screen_motion":
            return await self._write("SCSV", self._fmt(int(params["pattern"])))

        if command == "raw_command":
            cmd = str(params["command"]).upper()
            param = str(params.get("parameter") or "????")
            if len(param) < 4:
                param = param.rjust(4)
            line = await self._request(cmd, param, timeout=12.0)
            if line is not None and cmd in _READS:
                self._apply_read(cmd, line)
            return line

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    # ── Device settings ────────────────────────────────────────────────────

    async def set_device_setting(self, key: str, value: Any) -> Any:
        writers = {
            "brightness": lambda v: ("VLMP", self._fmt(int(v))),
            "standby_mode": lambda v: ("STBM", self._fmt(STANDBY_MODE_TOKENS[str(v)])),
            "adjustment_lock": lambda v: ("ALCK", self._fmt(ADJUSTMENT_LOCK_TOKENS[str(v)])),
            "osd_display": lambda v: ("LOSD", self._fmt(OSD_MODE_TOKENS[str(v)])),
            # Wire is inverted: 0 = LED on.
            "led_enabled": lambda v: ("OFLD", self._fmt(0 if v else 1)),
            "touch": lambda v: ("TPEN", self._fmt(1 if v else 0)),
        }
        writer = writers.get(key)
        if writer is None:
            raise ValueError(f"Unknown device setting '{key}'")
        cmd, param = writer(value)
        await self._write(cmd, param)
        # Read straight back so the settings editor reflects the device.
        line = await self._request(cmd, "????")
        if line is not None:
            self._apply_read(cmd, line)
        return True
