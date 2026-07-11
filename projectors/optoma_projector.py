"""
OpenAVC Optoma projector driver (Optoma RS232 / LAN "~XX" protocol).

Controls Optoma projectors over Optoma's own ASCII command protocol —
"RS232 by Telnet" on TCP port 23, or direct RS-232 — covering the
installation and large-venue laser lines (ZU/ZK series, ProScene) and
the long tail of Optoma business/home models that publish the same
command table. This is the brand-specific surface beyond PJLink:
display modes, shutter, motorized lens (shift/zoom/focus/memories),
keystone, light-source hours, and fault push notifications.

Why Python and not YAML:
    Replies carry no command echo — a write answers a bare ``P`` (pass)
    or ``F`` (fail) and a read answers ``Ok<value>`` (older firmware:
    ``OK<value>``) with nothing that says which query it answers, so
    pattern-based YAML response dispatch cannot attribute them. The
    driver serializes one awaited request at a time and routes each
    reply by the query that asked (the sharp_pn_display pattern). The
    same receive path must also divert the unsolicited ``INFOn`` status
    lines a projector emits on power transitions and faults, and strip
    Telnet IAC negotiation / NUL padding bytes some LAN modules emit.

Push vs poll: HYBRID. Projectors push unsolicited ``INFOn`` lines
("System Automatically Send" in every Optoma protocol document) on
power transitions (0 standby / 1 warming / 2 cooling / 24 ready) and
faults (lamp/LD fail, fan lock, over-temperature, ...); the driver maps
these live. Everything else (input, mutes, picture values, hours) is
strictly request/response and is polled.

Wire protocol (ASCII, commands and replies CR-terminated)::

    Write:  ~XXnnn v<CR>   ->  P | F
    Read:   ~XXnnn 1<CR>   ->  Ok<value> | OK<value> | F
    Push:   INFO<n><CR>        (unsolicited)

    XX = two-digit projector ID (00 addresses any/all projectors,
    the factory default target).

Documented constraints honored here:
  - >= 200 ms between commands over Telnet (inter_command_delay 0.2);
  - power-on/off feedback can lag 6-10 s (long ack window on power
    commands);
  - < 26 bytes per command line (every command in this driver is
    well under).

Family variance (one driver, per-model subsets — an unsupported
command answers ``F``, which the driver logs at debug and ignores):
  - Volume range is 0-10 on most models, 0-15 on some laser
    generations.
  - The ``~XX121`` input read codes 9-14 drift between generations
    (HDMI3 vs Wireless vs Component ...); the stable codes (0-8,
    15, 16) cover the common terminals and unknown codes read back
    as ``input_<n>``.
  - AV mute is ``~XX02``; models with a mechanical shutter also
    expose it as ``~XX325`` (both provided).
  - The INFO fault-code list grew over time (older lamp units emit
    3-11; laser units add 12-24); unknown codes read back as
    ``fault_<n>``.

Standby-power caveat (matters for automation): LAN control only works
in standby when the projector's Standby Power Mode is set to
"Communication" / "Active" (menu wording varies by model). The eco
"0.5W" standby shuts the network module down — the projector then
drops off the network entirely until woken at the panel or by RS-232.

Protocol reference: Optoma "RS232 Protocol Function List" (full
command table with the INFO code list and lens/shutter commands):
https://www.optomaeurope.com/ContentStorage/Documents/869372d2-6854-4dd6-b26e-2f246a5f6ea8.pdf
Cross-checked against the ZU510T-generation install-laser table
(https://optoma.de/uploads/RS232/ZU510T-RS232--.pdf) and a
current large-venue table (19200 baud generation); the Telnet
transport (TCP 23, payload/pacing limits) is documented in Optoma's
user manuals ("Using RS232 command by Telnet").
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# ── Wire tables (Optoma RS232 Protocol Function List) ───────────────────────

# ~XX12 write codes (input select).
INPUT_WRITE_CODES = {
    "hdmi1": 1,
    "hdmi2": 15,
    "hdmi3": 16,
    "dvi_d": 2,
    "dvi_a": 3,
    "bnc": 4,
    "vga1": 5,
    "vga2": 6,
    "svideo": 9,
    "video": 10,
    "wireless": 11,
    "component": 14,
    "flash_drive": 17,
    "network_display": 18,
    "usb_display": 19,
    "displayport": 20,
    "hdbaset": 21,
    "sdi_3g": 22,
    "multimedia": 23,
    "smart_tv": 24,
}

# ~XX121 read codes (current source). Codes 9-14 drift between
# generations; unknown codes surface as input_<n>.
INPUT_READ_NAMES = {
    0: "no_signal",
    1: "dvi_d",
    2: "vga1",
    3: "vga2",
    4: "svideo",
    5: "video",
    6: "bnc",
    7: "hdmi1",
    8: "hdmi2",
    9: "hdmi3",
    10: "wireless",
    11: "component",
    12: "flash_drive",
    13: "network_display",
    14: "usb_display",
    15: "displayport",
    16: "hdbaset",
    17: "multimedia",
    18: "sdi_3g",
    20: "smart_tv",
}

INPUT_LABELS = [
    {"value": "hdmi1", "label": "HDMI 1"},
    {"value": "hdmi2", "label": "HDMI 2"},
    {"value": "hdmi3", "label": "HDMI 3"},
    {"value": "displayport", "label": "DisplayPort"},
    {"value": "hdbaset", "label": "HDBaseT"},
    {"value": "vga1", "label": "VGA 1"},
    {"value": "vga2", "label": "VGA 2"},
    {"value": "dvi_d", "label": "DVI-D"},
    {"value": "dvi_a", "label": "DVI-A"},
    {"value": "bnc", "label": "BNC"},
    {"value": "component", "label": "Component"},
    {"value": "sdi_3g", "label": "3G-SDI"},
    {"value": "video", "label": "Video"},
    {"value": "svideo", "label": "S-Video"},
    {"value": "wireless", "label": "Wireless"},
    {"value": "network_display", "label": "Network Display"},
    {"value": "usb_display", "label": "USB Display"},
    {"value": "flash_drive", "label": "Flash Drive"},
    {"value": "multimedia", "label": "Multimedia"},
    {"value": "smart_tv", "label": "Smart TV"},
]

# ~XX20 write codes / ~XX123 read codes (display mode). The write and
# read spaces agree except DICOM SIM (write 13, reads back 10).
DISPLAY_MODE_WRITE_CODES = {
    "presentation": 1,
    "bright": 2,
    "movie": 3,
    "srgb": 4,
    "user": 5,
    "dicom_sim": 13,
    "blending": 19,
}
DISPLAY_MODE_READ_NAMES = {
    0: "none",
    1: "presentation",
    2: "bright",
    3: "movie",
    4: "srgb",
    5: "user",
    10: "dicom_sim",
    19: "blending",
}
DISPLAY_MODE_LABELS = [
    {"value": "presentation", "label": "Presentation"},
    {"value": "bright", "label": "Bright"},
    {"value": "movie", "label": "Movie"},
    {"value": "srgb", "label": "sRGB"},
    {"value": "user", "label": "User"},
    {"value": "dicom_sim", "label": "DICOM SIM."},
    {"value": "blending", "label": "Blending"},
]

# ~XX60 write codes / ~XX127 read codes (aspect ratio).
ASPECT_WRITE_CODES = {"4_3": 1, "16_9": 2, "16_10": 3, "auto": 7}
ASPECT_READ_NAMES = {0: "none", 1: "4_3", 2: "16_9", 3: "16_10", 7: "auto"}
ASPECT_LABELS = [
    {"value": "auto", "label": "Auto"},
    {"value": "4_3", "label": "4:3"},
    {"value": "16_9", "label": "16:9"},
    {"value": "16_10", "label": "16:10"},
]

# ~XX84 lens shift directions (3-6; 1/2 are the shift lock).
LENS_SHIFT_CODES = {"up": 3, "down": 4, "left": 5, "right": 6}

# ~XX140 remote-key codes (the subset stable across every generation's
# table; Exit and Source drift between documents and are left out).
REMOTE_KEY_CODES = {
    "menu": 20,
    "up": 10,
    "down": 14,
    "left": 11,
    "right": 13,
    "enter": 12,
}

# ~XX151 model-name read: older units answer a resolution-class code.
MODEL_CLASS_NAMES = {
    1: "Optoma SVGA",
    2: "Optoma XGA",
    3: "Optoma WXGA",
    4: "Optoma 1080p",
    5: "Optoma WUXGA",
}

# INFOn power transitions ("System Automatically Send").
INFO_POWER_STATES = {
    0: "standby",
    1: "warming_up",
    2: "cooling_down",
    24: "on",
}

# INFOn fault codes. 3-11 are the classic lamp-unit list; 12-24 were
# added by later (laser) generations.
INFO_FAULTS = {
    3: "signal_out_of_range",
    4: "light_source_fail",
    5: "thermal_switch_error",
    6: "fan_lock",
    7: "over_temperature",
    8: "light_source_life_low",
    9: "cover_open",
    10: "light_source_ignite_fail",
    11: "format_board_power_fail",
    12: "color_wheel_stop",
    13: "over_temperature",
    14: "fan_lock",
    15: "fan_lock",
    16: "fan_lock",
    17: "fan_lock",
    18: "fan_lock",
    19: "lan_restarted",
    20: "light_source_degraded",
    21: "ld_over_temperature",
    22: "ld_over_temperature",
    23: "high_ambient_temperature",
}

# Read command -> (state key, kind). Keyed by (command digits, value
# digits) exactly as they appear on the wire after the projector ID.
#   power    Ok0/Ok1 -> standby/on
#   input    code -> INPUT_READ_NAMES
#   mode     code -> DISPLAY_MODE_READ_NAMES
#   aspect   code -> ASPECT_READ_NAMES
#   bool     0/1 -> False/True
#   int      plain integer
#   hours    sum of every integer in the reply (models report either a
#            single total or "normal/eco" counters; each runtime hour
#            lands in exactly one counter, so the sum is total hours)
#   model    class code -> MODEL_CLASS_NAMES, or the raw string
#   text     raw string
_READS: dict[tuple[str, str], tuple[str, str]] = {
    ("124", "1"): ("power_state", "power"),
    ("121", "1"): ("input", "input"),
    ("355", "1"): ("av_mute", "bool"),
    ("356", "1"): ("audio_mute", "bool"),
    ("123", "1"): ("display_mode", "mode"),
    ("127", "1"): ("aspect_ratio", "aspect"),
    ("125", "1"): ("brightness", "int"),
    ("126", "1"): ("contrast", "int"),
    ("108", "1"): ("light_source_hours", "hours"),
    ("150", "4"): ("source_resolution", "text"),
    ("151", "1"): ("model_name", "model"),
    ("122", "1"): ("firmware_version", "text"),
    ("353", "1"): ("serial_number", "text"),
}

# Poll cycle. Power and hours answer in standby (with Standby Power
# Mode on Communication); the rest only make sense with the lamp on.
_POLL_ALWAYS = [("124", "1"), ("108", "1")]
_POLL_WHEN_ON = [
    ("121", "1"), ("355", "1"), ("356", "1"), ("123", "1"),
    ("127", "1"), ("125", "1"), ("126", "1"), ("150", "4"),
]

_REPLY_TIMEOUT = 5.0
# Optoma documents a 6-10 s feedback delay on power commands.
_POWER_TIMEOUT = 12.0

# Telnet IAC negotiation sequences some LAN modules emit at connect:
# IAC WILL/WONT/DO/DONT <opt> (3 bytes), or IAC <cmd> (2 bytes).
_IAC_RE = re.compile(rb"\xff[\xfb-\xfe].|\xff.")


class OptomaProjectorDriver(BaseDriver):
    """Optoma "~XX" RS232/LAN protocol driver."""

    DRIVER_INFO = {
        "id": "optoma_projector",
        "name": "Optoma Projector",
        "manufacturer": "Optoma",
        "category": "projector",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Optoma projectors over Optoma's RS232/LAN command "
            "protocol (Telnet port 23 or direct RS-232). Power, input "
            "select, A/V mute, shutter, audio, display modes, aspect, "
            "brightness/contrast, keystone, motorized lens with lens "
            "memories, remote-key navigation, light-source hours, and "
            "live fault notifications. Covers the ZU/ZK installation and "
            "large-venue laser series and other Optoma models sharing "
            "the published command table — the full brand feature set "
            "beyond what generic PJLink offers."
        ),
        "source_url": "https://www.optomaeurope.com/ContentStorage/Documents/869372d2-6854-4dd6-b26e-2f246a5f6ea8.pdf",
        "tags": [
            "projector", "optoma", "laser", "install", "proscene",
            "rs232", "telnet", "lan-control",
        ],
        "verified": False,
        "simulated": True,
        "ports": [23],
        "min_platform_version": "0.23.0",
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "delimiter": "\r",
        "protocols": ["optoma_rs232"],
        "compatible_models": [
            {
                "manufacturer": "Optoma",
                "models": [
                    "ZU506Te / ZU606TSTe / ZU610T / ZU510T",
                    "ZU660e / ZU720T / ZU720TST / ZU725TST",
                    "ZK507 / ZK750 / ZK1050",
                    "ZU860 / ZU1050 / ZU1300 (ProScene large venue)",
                    "EH / WU / X / W business series",
                    "UHD home cinema series",
                ],
                "confidence": "untested",
                "notes": (
                    "Optoma publishes the same command grammar across its "
                    "range; each model's RS232 Protocol Function List "
                    "documents its supported subset, and a command a model "
                    "lacks answers F (ignored by the driver). Lens "
                    "shift/zoom/focus and lens memories need a motorized "
                    "lens (ZK/ZU install and large-venue models); the "
                    "shutter commands need a mechanical shutter. Volume "
                    "range is 0-10 on most models, 0-15 on some."
                ),
            },
        ],
        "help": {
            "overview": (
                "Optoma's own control protocol — the same ~XX command set "
                "over the LAN (Telnet) or RS-232. Compared to PJLink it "
                "adds display modes, shutter, motorized lens control with "
                "position memories, keystone, remote-key navigation, "
                "light-source hours, and live fault reporting (fan, "
                "thermal, light source). The projector pushes power and "
                "fault transitions as they happen; other state is polled."
            ),
            "setup": (
                "1. Connect the projector's LAN port and give it a static "
                "IP (or a DHCP reservation).\n"
                "2. In the projector's Network / Communications menu make "
                "sure Telnet control is enabled (on most models it is on "
                "by default alongside the web control page).\n"
                "3. Set Standby Power Mode to 'Communication' (some menus "
                "say 'Active' or 'Network Standby') so the projector "
                "still answers — and can be powered on — while in "
                "standby. The eco '0.5W' standby powers the network "
                "module down completely.\n"
                "4. Serial control instead: connect the RS-232 port "
                "(9600 8N1 on most models; newer large-venue units "
                "default to 19200 — match the projector's Serial Port "
                "Baud Rate setting).\n"
                "5. Leave Projector ID at 0 unless you have assigned "
                "IDs on an RS-232 chain; ID 0 addresses any projector."
            ),
            "connection": (
                "A projector in eco (0.5W) standby does not answer on the "
                "network at all — set Standby Power Mode to Communication "
                "so power_on works remotely. Power on/off feedback can "
                "take 6-10 seconds; the driver waits accordingly."
            ),
        },
        "discovery": {
            # Active fingerprint: the Optoma power read on the Telnet
            # control port. An Optoma unit answers Ok0/Ok1; other gear
            # on port 23 answers prompts/banners that never match.
            "tcp_probe": {
                "port": 23,
                "send_ascii": "~00124 1\r",
                "expect_regex": r"(?i)\bOk[01]\b",
                "extract_manufacturer": "Optoma",
            },
            # PJLink scans report the make (Optoma projectors answer
            # PJLink too); surface this brand driver as the candidate.
            "manufacturer_alias": ["optoma", "optoma corporation"],
        },
        "default_config": {
            "host": "",
            "port": 23,
            "projector_id": 0,
            "poll_interval": 15,
            "inter_command_delay": 0.2,
            "baudrate": 9600,
            "parity": "N",
            "bytesize": 8,
            "stopbits": 1,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "Projector IP address (LAN control uses Telnet).",
            },
            "port": {
                "type": "integer", "default": 23, "label": "TCP Port",
                "description": "Telnet control port. Optoma fixed default 23.",
            },
            "projector_id": {
                "type": "integer", "default": 0, "min": 0, "max": 99,
                "label": "Projector ID",
                "description": (
                    "The projector's assigned ID (Setup menu). 0 addresses "
                    "any projector and is right for LAN control and "
                    "one-to-one serial."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 15, "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "Power and fault changes arrive as push notifications; "
                    "polling refreshes input, mutes, and picture state. "
                    "Set to 0 to disable."
                ),
            },
            "inter_command_delay": {
                "type": "number", "default": 0.2, "min": 0,
                "label": "Inter-Command Delay (sec)",
                "description": (
                    "Optoma requires at least 200 ms between commands over "
                    "Telnet."
                ),
            },
            "baudrate": {
                "type": "integer", "default": 9600,
                "label": "Baud Rate (serial)",
                "description": (
                    "Match the projector's Serial Port Baud Rate setting "
                    "(9600 on most models; newer large-venue units default "
                    "to 19200)."
                ),
            },
        },
        "state_variables": {
            "power_state": {
                "type": "enum",
                "values": ["standby", "warming_up", "on", "cooling_down"],
                "label": "Power", "control": True,
                "help": (
                    "warming_up / cooling_down come live from the "
                    "projector's INFO notifications during transitions."
                ),
            },
            "input": {
                "type": "string", "label": "Input", "control": True,
                "help": (
                    "Active input (hdmi1, hdmi2, displayport, hdbaset, "
                    "vga1, ...) or no_signal. Unknown codes read back as "
                    "input_<n>; on some generations codes 9-14 label "
                    "differently (see the driver notes)."
                ),
            },
            "av_mute": {
                "type": "boolean", "label": "A/V Mute", "control": True,
                "help": "Picture (and audio) blanked.",
            },
            "audio_mute": {
                "type": "boolean", "label": "Audio Mute", "control": True,
                "help": "Audio mute state (models with audio).",
            },
            "display_mode": {
                "type": "string", "label": "Display Mode",
                "help": (
                    "Active picture preset (presentation, bright, movie, "
                    "srgb, user, dicom_sim, blending). Unknown codes read "
                    "back as mode_<n>."
                ),
            },
            "aspect_ratio": {
                "type": "string", "label": "Aspect Ratio",
                "help": (
                    "Active aspect (auto, 4_3, 16_9, 16_10). Unknown codes "
                    "read back as aspect_<n>."
                ),
            },
            "brightness": {
                "type": "integer", "label": "Brightness", "control": True,
                "min": 0, "max": 100, "step": 1,
                "help": "Picture brightness, 0-100.",
            },
            "contrast": {
                "type": "integer", "label": "Contrast",
                "min": 0, "max": 100, "step": 1,
                "help": "Picture contrast, 0-100.",
            },
            "light_source_hours": {
                "type": "integer", "label": "Light Source Hours",
                "help": (
                    "Total lamp / laser runtime hours. Models that report "
                    "per-mode counters (Normal/Eco) are summed."
                ),
            },
            "source_resolution": {
                "type": "string", "label": "Source Resolution",
                "help": "Resolution of the active source, as reported.",
            },
            "fault": {
                "type": "string", "label": "Fault",
                "help": (
                    "Last fault pushed by the projector (fan_lock, "
                    "over_temperature, light_source_fail, ...). 'none' "
                    "when healthy; cleared when the projector reports "
                    "ready. Unknown codes surface as fault_<n>."
                ),
            },
            "model_name": {
                "type": "string", "label": "Model",
                "help": (
                    "Model as reported. Older units answer a resolution "
                    "class (e.g. Optoma WUXGA) rather than a model name."
                ),
            },
            "firmware_version": {
                "type": "string", "label": "Firmware Version",
            },
            "serial_number": {
                "type": "string", "label": "Serial Number",
            },
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "help": (
                    "Turn the projector on. From standby this needs "
                    "Standby Power Mode set to Communication (eco standby "
                    "shuts LAN control down). Feedback can lag 6-10 s."
                ),
                "params": {},
            },
            "power_off": {
                "label": "Power Off (Standby)",
                "params": {},
            },
            "set_input": {
                "label": "Select Input",
                "help": (
                    "Select an input. Terminals a model lacks answer F, "
                    "which is ignored."
                ),
                "params": {
                    "input": {
                        "type": "enum", "required": True, "label": "Input",
                        "values": INPUT_LABELS,
                    },
                },
            },
            "av_mute_on": {
                "label": "A/V Mute On",
                "help": "Blank the picture (and audio).",
                "params": {},
            },
            "av_mute_off": {"label": "A/V Mute Off", "params": {}},
            "shutter_close": {
                "label": "Shutter Close",
                "help": (
                    "Close the mechanical shutter (models with one; other "
                    "models answer F — use A/V Mute instead)."
                ),
                "params": {},
            },
            "shutter_open": {"label": "Shutter Open", "params": {}},
            "audio_mute_on": {"label": "Audio Mute On", "params": {}},
            "audio_mute_off": {"label": "Audio Mute Off", "params": {}},
            "set_volume": {
                "label": "Set Volume",
                "help": (
                    "Speaker/line volume. 0-10 on most models, 0-15 on "
                    "some; values past a model's range answer F. There is "
                    "no volume read-back in the protocol."
                ),
                "params": {
                    "level": {
                        "type": "integer", "required": True,
                        "label": "Volume (0-15)", "min": 0, "max": 15,
                    },
                },
            },
            "freeze_on": {"label": "Freeze On", "params": {}},
            "freeze_off": {"label": "Freeze Off", "params": {}},
            "set_display_mode": {
                "label": "Set Display Mode",
                "params": {
                    "mode": {
                        "type": "enum", "required": True, "label": "Mode",
                        "values": DISPLAY_MODE_LABELS,
                    },
                },
            },
            "set_aspect": {
                "label": "Set Aspect Ratio",
                "params": {
                    "aspect": {
                        "type": "enum", "required": True, "label": "Aspect",
                        "values": ASPECT_LABELS,
                    },
                },
            },
            "set_brightness": {
                "label": "Set Brightness",
                "params": {
                    "level": {
                        "type": "integer", "required": True,
                        "label": "Brightness (0-100)", "min": 0, "max": 100,
                    },
                },
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {
                    "level": {
                        "type": "integer", "required": True,
                        "label": "Contrast (0-100)", "min": 0, "max": 100,
                    },
                },
            },
            "set_v_keystone": {
                "label": "Set V Keystone",
                "help": "Vertical keystone correction (range is model-dependent).",
                "params": {
                    "value": {
                        "type": "integer", "required": True,
                        "label": "V Keystone (-40 to 40)",
                        "min": -40, "max": 40,
                    },
                },
            },
            "set_h_keystone": {
                "label": "Set H Keystone",
                "params": {
                    "value": {
                        "type": "integer", "required": True,
                        "label": "H Keystone (-40 to 40)",
                        "min": -40, "max": 40,
                    },
                },
            },
            "lens_shift": {
                "label": "Lens Shift",
                "help": "Nudge the motorized lens (models with one).",
                "params": {
                    "direction": {
                        "type": "enum", "required": True, "label": "Direction",
                        "values": [
                            {"value": "up", "label": "Up"},
                            {"value": "down", "label": "Down"},
                            {"value": "left", "label": "Left"},
                            {"value": "right", "label": "Right"},
                        ],
                    },
                },
            },
            "lens_zoom": {
                "label": "Lens Zoom",
                "params": {
                    "direction": {
                        "type": "enum", "required": True, "label": "Direction",
                        "values": [
                            {"value": "in", "label": "Zoom In (+)"},
                            {"value": "out", "label": "Zoom Out (-)"},
                        ],
                    },
                },
            },
            "lens_focus": {
                "label": "Lens Focus",
                "params": {
                    "direction": {
                        "type": "enum", "required": True, "label": "Direction",
                        "values": [
                            {"value": "near", "label": "Focus +"},
                            {"value": "far", "label": "Focus -"},
                        ],
                    },
                },
            },
            "lens_memory_apply": {
                "label": "Apply Lens Memory",
                "help": "Recall a saved lens position (models with lens memories).",
                "params": {
                    "slot": {
                        "type": "integer", "required": True,
                        "label": "Memory (1-10)", "min": 1, "max": 10,
                    },
                },
            },
            "lens_memory_save": {
                "label": "Save Lens Memory",
                "params": {
                    "slot": {
                        "type": "integer", "required": True,
                        "label": "Memory (1-10)", "min": 1, "max": 10,
                    },
                },
            },
            "lens_calibrate": {
                "label": "Calibrate Lens",
                "help": "Run the lens calibration cycle.",
                "params": {},
            },
            "send_key": {
                "label": "Send Remote Key",
                "help": (
                    "Emulate a remote-control key press for menu "
                    "navigation."
                ),
                "params": {
                    "key": {
                        "type": "enum", "required": True, "label": "Key",
                        "values": [
                            {"value": "menu", "label": "Menu"},
                            {"value": "up", "label": "Up"},
                            {"value": "down", "label": "Down"},
                            {"value": "left", "label": "Left"},
                            {"value": "right", "label": "Right"},
                            {"value": "enter", "label": "Enter"},
                        ],
                    },
                },
            },
            "resync": {
                "label": "Re-Sync",
                "help": "Re-synchronize to the current source.",
                "params": {},
            },
            "raw_command": {
                "label": "Send Raw Command",
                "help": (
                    "Send any command from your model's RS232 Protocol "
                    "Function List as a full line, e.g. ~0021 50 or a "
                    "read like ~00125 1 (the reply is applied to state "
                    "when it is a read this driver knows). The projector "
                    "ID in the line is sent as typed."
                ),
                "params": {
                    "command": {
                        "type": "string", "required": True, "label": "Command",
                        "pattern": r"^~\d{2,5}( .+)?$",
                    },
                },
            },
            "refresh": {"label": "Refresh Status", "params": {}},
        },
        # Values the projector persists and reads back — editable field +
        # offline pending queue on top of the transient set_* commands.
        "device_settings": {
            "display_mode": {
                "type": "enum", "values": DISPLAY_MODE_LABELS,
                "label": "Display Mode",
                "help": (
                    "Picture preset. Presets a model lacks answer F on "
                    "the projector."
                ),
                "state_key": "display_mode", "default": "presentation",
                "setup": False,
            },
            "aspect_ratio": {
                "type": "enum", "values": ASPECT_LABELS,
                "label": "Aspect Ratio",
                "state_key": "aspect_ratio", "default": "auto",
                "setup": False,
            },
            "brightness": {
                "type": "integer", "min": 0, "max": 100,
                "label": "Brightness",
                "state_key": "brightness", "default": 50, "setup": False,
            },
            "contrast": {
                "type": "integer", "min": 0, "max": 100,
                "label": "Contrast",
                "state_key": "contrast", "default": 50, "setup": False,
            },
        },
        "quick_actions": ["power_on", "power_off", "av_mute_on", "av_mute_off"],
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any):
        super().__init__(device_id, config, state, events)
        self._io_lock = asyncio.Lock()
        self._reply_queue: deque[str] = deque()
        self._reply_event = asyncio.Event()

    # ── Wire helpers ─────────────────────────────────────────────────────────

    def _id_field(self) -> str:
        try:
            pid = int(self.config.get("projector_id", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        return f"{max(0, min(99, pid)):02d}"

    def _line(self, command: str, value: str) -> str:
        return f"~{self._id_field()}{command} {value}"

    # ── Request/response (single in flight, replies carry no echo) ─────────

    async def _request(
        self, command: str, value: str, timeout: float = _REPLY_TIMEOUT
    ) -> str | None:
        """Send one command line and await its reply (P / F / Ok<n>).

        Returns None on timeout. Serialized: the protocol is strictly
        one command / one reply, with unsolicited INFO lines diverted
        before they reach the reply queue.
        """
        return await self._request_line(self._line(command, value), timeout)

    async def _request_line(self, line: str, timeout: float) -> str | None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._io_lock:
            self._reply_queue.clear()
            self._reply_event.clear()
            await self.transport.send((line + "\r").encode("ascii"))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                if self._reply_queue:
                    return self._reply_queue.popleft()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    log.warning(f"[{self.device_id}] No response to {line}")
                    return None
                self._reply_event.clear()
                try:
                    await asyncio.wait_for(
                        self._reply_event.wait(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    continue

    async def on_data_received(self, data: bytes) -> None:
        # Telnet LAN modules can inject IAC negotiation and NUL padding.
        data = _IAC_RE.sub(b"", data).replace(b"\x00", b"")
        text = data.decode("ascii", errors="replace").strip()
        if not text:
            return
        # Unsolicited status push — never a reply to a request.
        if text.upper().startswith("INFO"):
            self._handle_info(text)
            return
        # Some Telnet servers echo the typed command line back.
        if text.startswith("~"):
            return
        self._reply_queue.append(text)
        self._reply_event.set()

    def _handle_info(self, text: str) -> None:
        try:
            code = int(text[4:].strip())
        except ValueError:
            log.debug(f"[{self.device_id}] Unparseable INFO push: {text!r}")
            return
        power = INFO_POWER_STATES.get(code)
        if power is not None:
            self.set_state("power_state", power)
            if code == 24:
                self.set_state("fault", "none")
            log.info(f"[{self.device_id}] Projector reports {power} (INFO{code})")
            return
        fault = INFO_FAULTS.get(code, f"fault_{code}")
        self.set_state("fault", fault)
        log.warning(f"[{self.device_id}] Projector fault: {fault} (INFO{code})")

    # ── Reply parsing ────────────────────────────────────────────────────────

    def _apply_read(self, key: tuple[str, str], reply: str) -> None:
        if not reply or reply[:2].upper() != "OK":
            # F = read rejected/unsupported; anything else is noise.
            if reply and reply != "F":
                log.debug(f"[{self.device_id}] Unexpected read reply: {reply!r}")
            return
        entry = _READS.get(key)
        if entry is None:
            return
        state_key, kind = entry
        text = reply[2:].strip()
        if kind == "text":
            if text:
                self.set_state(state_key, text)
            return
        if kind == "hours":
            numbers = re.findall(r"\d+", text)
            if numbers:
                self.set_state(state_key, sum(int(n) for n in numbers))
            return
        if kind == "model":
            if text.isdigit() and int(text) in MODEL_CLASS_NAMES:
                self.set_state(state_key, MODEL_CLASS_NAMES[int(text)])
            elif text:
                self.set_state(state_key, text)
            return
        try:
            value = int(text)
        except ValueError:
            log.debug(
                f"[{self.device_id}] Non-numeric reply for {state_key}: {text!r}"
            )
            return
        if kind == "int":
            self.set_state(state_key, value)
        elif kind == "bool":
            self.set_state(state_key, value == 1)
        elif kind == "power":
            self.set_state(state_key, "on" if value == 1 else "standby")
        elif kind == "input":
            self.set_state(
                state_key, INPUT_READ_NAMES.get(value, f"input_{value}")
            )
        elif kind == "mode":
            self.set_state(
                state_key, DISPLAY_MODE_READ_NAMES.get(value, f"mode_{value}")
            )
        elif kind == "aspect":
            self.set_state(
                state_key, ASPECT_READ_NAMES.get(value, f"aspect_{value}")
            )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _post_connect(self) -> None:
        self._reply_queue.clear()
        # Identity — one-time reads; a model that rejects one answers F,
        # leaving the state unset.
        for cmd, value in (("151", "1"), ("122", "1"), ("353", "1")):
            reply = await self._request(cmd, value)
            if reply is not None:
                self._apply_read((cmd, value), reply)

    async def poll(self) -> None:
        for cmd, value in _POLL_ALWAYS:
            reply = await self._request(cmd, value)
            if reply is not None:
                self._apply_read((cmd, value), reply)
        if self.get_state("power_state") == "on":
            for cmd, value in _POLL_WHEN_ON:
                reply = await self._request(cmd, value)
                if reply is not None:
                    self._apply_read((cmd, value), reply)

    # ── Commands ─────────────────────────────────────────────────────────────

    async def _write(
        self, command: str, value: str, timeout: float = _REPLY_TIMEOUT
    ) -> bool:
        reply = await self._request(command, value, timeout=timeout)
        if reply == "P":
            return True
        if reply == "F":
            log.debug(
                f"[{self.device_id}] Projector rejected ~XX{command} {value}"
            )
        return False

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "power_on":
            return await self._write("00", "1", timeout=_POWER_TIMEOUT)
        if command == "power_off":
            return await self._write("00", "0", timeout=_POWER_TIMEOUT)
        if command == "set_input":
            token = str(params["input"])
            code = INPUT_WRITE_CODES.get(token)
            if code is None:
                raise ValueError(f"Unknown input '{token}'")
            return await self._write("12", str(code), timeout=_POWER_TIMEOUT)
        if command == "av_mute_on":
            return await self._write("02", "1")
        if command == "av_mute_off":
            return await self._write("02", "0")
        if command == "shutter_close":
            return await self._write("325", "1")
        if command == "shutter_open":
            return await self._write("325", "0")
        if command == "audio_mute_on":
            return await self._write("80", "1")
        if command == "audio_mute_off":
            return await self._write("80", "0")
        if command == "set_volume":
            return await self._write("81", str(int(params["level"])))
        if command == "freeze_on":
            return await self._write("04", "1")
        if command == "freeze_off":
            return await self._write("04", "0")
        if command == "set_display_mode":
            token = str(params["mode"])
            code = DISPLAY_MODE_WRITE_CODES.get(token)
            if code is None:
                raise ValueError(f"Unknown display mode '{token}'")
            return await self._write("20", str(code))
        if command == "set_aspect":
            token = str(params["aspect"])
            code = ASPECT_WRITE_CODES.get(token)
            if code is None:
                raise ValueError(f"Unknown aspect '{token}'")
            return await self._write("60", str(code))
        if command == "set_brightness":
            return await self._write("21", str(int(params["level"])))
        if command == "set_contrast":
            return await self._write("22", str(int(params["level"])))
        if command == "set_v_keystone":
            return await self._write("66", str(int(params["value"])))
        if command == "set_h_keystone":
            return await self._write("65", str(int(params["value"])))
        if command == "lens_shift":
            code = LENS_SHIFT_CODES.get(str(params["direction"]))
            if code is None:
                raise ValueError("Unknown lens shift direction")
            return await self._write("84", str(code))
        if command == "lens_zoom":
            code = 1 if str(params["direction"]) == "in" else 2
            return await self._write("307", str(code))
        if command == "lens_focus":
            code = 1 if str(params["direction"]) == "near" else 2
            return await self._write("308", str(code))
        if command == "lens_memory_apply":
            return await self._write("359", str(int(params["slot"])))
        if command == "lens_memory_save":
            return await self._write("360", str(int(params["slot"])))
        if command == "lens_calibrate":
            return await self._write("525", "1", timeout=_POWER_TIMEOUT)
        if command == "send_key":
            code = REMOTE_KEY_CODES.get(str(params["key"]))
            if code is None:
                raise ValueError(f"Unknown key '{params.get('key')}'")
            return await self._write("140", str(code))
        if command == "resync":
            return await self._write("01", "1")
        if command == "raw_command":
            line = str(params["command"]).strip()
            if not line.startswith("~"):
                raise ValueError("Raw command must start with ~")
            reply = await self._request_line(line, timeout=_POWER_TIMEOUT)
            if reply is not None:
                m = re.match(r"^~\d{2}(\d{1,3})\s+(\d+)$", line)
                if m and (m.group(1), m.group(2)) in _READS:
                    self._apply_read((m.group(1), m.group(2)), reply)
            return reply
        if command == "refresh":
            await self.poll()
            return None

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    # ── Device settings ──────────────────────────────────────────────────────

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "display_mode":
            await self.send_command("set_display_mode", {"mode": str(value)})
            read = ("123", "1")
        elif key == "aspect_ratio":
            await self.send_command("set_aspect", {"aspect": str(value)})
            read = ("127", "1")
        elif key == "brightness":
            await self.send_command("set_brightness", {"level": int(value)})
            read = ("125", "1")
        elif key == "contrast":
            await self.send_command("set_contrast", {"level": int(value)})
            read = ("126", "1")
        else:
            raise ValueError(f"Unknown device setting '{key}'")
        # Read straight back so the settings editor reflects the device.
        reply = await self._request(*read)
        if reply is not None:
            self._apply_read(read, reply)
        return True
