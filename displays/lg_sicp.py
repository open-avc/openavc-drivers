"""
OpenAVC LG SICP (Serial Interface Communication Protocol) driver.

Controls LG commercial displays (webOS Signage, LED, and OLED commercial
series) over TCP port 9761, or RS-232 (9600 8N1) behind an IP-to-serial
bridge. Protocol reference: LG SM-series RS-232C/IP control manual.

Why Python (converted from .avcdriver): two YAML gaps block this protocol.
(1) Every SICP numeric travels as two hex digits (00-64 = 0-100) and YAML
response coercion is base-10 only, so volume/brightness/contrast could
never read back — the original driver's volume was broken on real
hardware. (2) Input and picture mode are code<->label enums (90 = HDMI 1)
that a YAML param or device-setting write can't map. Python decodes hex,
publishes {value, label} picker options, and models each Set ID as a
``display`` child entity.

Wire protocol::

    Transmission:     [Cmd1][Cmd2] [Set ID] [Data]\\r     (ASCII)
    Acknowledgement:  [Cmd2] [Set ID] OK|NG[Data]x

  - The Set ID travels as HEX: OSD IDs 1..1000 are sent as 01..3E8 (the
    manual's "1 to 1000 (01H~3E8H)"). Every ack echoes the answering
    display's Set ID — that's what routes state to the right child when
    several displays are daisy-chained on RS-232 behind one socket.
  - Broadcast Set ID 00 addresses every display but suppresses all acks,
    so the chain-wide all_on / all_off loop the roster instead (each
    display confirms).
  - Data values are two hex digits. Mute is inverted on the wire
    (ke 00 = muted, 01 = unmuted).
  - An OK ack echoes the applied value for both sets and FF reads, so a
    successful set updates child state immediately; polling covers
    out-of-band changes (IR remote, front panel, webOS menu).
  - The ack does NOT echo Cmd1, so two commands sharing Cmd2 (kg contrast
    vs mg backlight both ack "g ...") are ambiguous by reply alone. A
    FIFO of in-flight (command, set_id) pairs correlates each ack back to
    the command that caused it; unanswered older entries (display off,
    absent Set ID) are pruned as later acks arrive. A display that
    answers after its entry was pruned can mis-route one colliding-Cmd2
    reply; the next poll corrects it.
  - A dx (picture mode) ack STARTS with the terminator character
    ("x 01 OK01x"), so naive split-on-'x' framing loses its Cmd2. The
    frame parser regex-scans the stream for complete acks instead. (An
    ASCII payload containing a literal lowercase 'x' — conceivable only
    in a serial-number string — would truncate at that character.)

Push vs poll: the protocol is strictly request/response — no
subscriptions or unsolicited notifications are documented — so polling
is correct. While a display reports power "off", only the power query is
sent each cycle (per the manual, most commands are only answered/valid
when the panel is fully on); the full surface refreshes on the cycle
after it comes back on.

Liveness: not armed here — a dropped TCP socket surfaces offline via the
transport, matching the samsung_mdc decision. Arming the platform
``_liveness_probe`` hook (a ka FF probe) is queued for the batch
liveness-arming pass.
"""

from __future__ import annotations

import json
import re
from collections import deque
from typing import Any, Optional

from server.drivers.base import BaseDriver
from server.transport.frame_parsers import CallableFrameParser, FrameParser
from server.utils.logger import get_logger

log = get_logger(__name__)

# Valid Set ID range (OSD "Set ID"; hex 01..3E8 on the wire). 00 is the
# no-ack broadcast address, not an addressable unit.
SET_ID_MIN = 1
SET_ID_MAX = 1000

# Select Input (xb) codes, per the SM-series manual. The DTV variant is the
# normal AV signal path; the (PC) variants treat the same connector as a PC
# source. Other codes exist on other LG lines (e.g. 92 = HDMI 3 on models
# that have one) — set_input forgives any two-hex-digit code and read-back
# renders unknown codes as "Input 0xNN".
INPUT_CODES = {
    "90": "HDMI 1",
    "A0": "HDMI 1 (PC)",
    "91": "HDMI 2 / OPS",
    "A1": "HDMI 2 / OPS (PC)",
    "C0": "DisplayPort",
    "D0": "DisplayPort (PC)",
    "70": "DVI-D (PC)",
    "80": "DVI-D (DTV)",
    "60": "RGB",
    "40": "Component",
}

# Picture Mode (dx) codes, per the SM-series manual. Not every model offers
# every mode — the display NGs an unsupported one. Unknown codes on other
# lines read back as "Mode 0xNN" and can be set by typing the raw code.
PICTURE_MODE_CODES = {
    "00": "Vivid",
    "01": "Standard",
    "02": "Cinema",
    "03": "Sports",
    "04": "Game",
    "05": "Expert 1",
    "06": "Expert 2",
    "08": "APS",
    "09": "Photos",
    "11": "Calibration",
}

# Aspect Ratio (kc). The named modes are settable from the dropdown; the
# Cinema Zoom band (10..1F) reads back as "Cinema Zoom N" and is reachable
# via raw_command for the rare rig that needs it.
ASPECT_SET = {
    "4:3": "01",
    "16:9": "02",
    "Zoom": "04",
    "Set by Program": "06",
    "Just Scan": "09",
}
ASPECT_NAMES = {v: k for k, v in ASPECT_SET.items()}

# Energy Saving (jq).
ENERGY_SAVING_SET = {
    "Off": "00",
    "Minimum": "01",
    "Medium": "02",
    "Maximum": "03",
    "Automatic": "04",
    "Screen Off": "05",
}
ENERGY_SAVING_NAMES = {v: k for k, v in ENERGY_SAVING_SET.items()}


def _build_sicp_command(cmd: str, set_id: int, data: str) -> bytes:
    """Render one SICP line: two command chars, hex Set ID, data, CR."""
    return f"{cmd} {format(set_id, '02X')} {data}\r".encode("ascii")


# One complete ack anywhere in the stream: Cmd2, hex Set ID, OK/NG, then
# everything up to the 'x' terminator. Scanning (rather than splitting on
# 'x') keeps the leading Cmd2 of a dx/fx ack, which is itself an 'x'.
_ACK_RE = re.compile(rb"([a-z]) ([0-9A-Fa-f]{2,3}) (OK|NG)([^x]*)x")


def _parse_sicp_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Frame parser: pop the next complete ack off the buffer.

    Returns (frame, remaining) or (None, buffer) when no complete ack has
    arrived yet. Garbage before a match (line echo, partial noise) is
    discarded with the match; an unmatched tail is kept for the next read
    but bounded so a non-SICP peer can't grow the buffer forever.
    """
    m = _ACK_RE.search(buffer)
    if m is None:
        if len(buffer) > 4096:
            buffer = buffer[-64:]
        return None, buffer
    return m.group(0), buffer[m.end():]


def _hex_or_none(data: str) -> Optional[int]:
    try:
        return int(data, 16)
    except ValueError:
        return None


def _norm_label(s: str) -> str:
    """Normalize a label for forgiving lookup ("HDMI 1" == "hdmi1")."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


_INPUT_BY_LABEL = {_norm_label(v): k for k, v in INPUT_CODES.items()}
_PICTURE_MODE_BY_LABEL = {_norm_label(v): k for k, v in PICTURE_MODE_CODES.items()}


def _child_state_vars() -> dict[str, dict[str, Any]]:
    """State variables for one ``display`` child entity.

    Power / input / volume / mute / screen-off / signal are the
    operationally-hot values (high cloud tier); picture settings and the
    health block change rarely (low tier).
    """
    return {
        "power": {
            "type": "enum",
            "values": ["off", "on"],
            "label": "Power",
            "cloud_priority": "high",
        },
        "input": {"type": "string", "label": "Input Source", "cloud_priority": "high"},
        "volume": {"type": "integer", "label": "Volume", "cloud_priority": "high"},
        "mute": {"type": "boolean", "label": "Audio Mute", "cloud_priority": "high"},
        "screen_off": {
            "type": "boolean",
            "label": "Screen Off",
            "help": "True when the picture is blanked (panel stays powered).",
            "cloud_priority": "high",
        },
        "signal": {
            "type": "enum",
            "values": ["unknown", "none", "present"],
            "label": "Input Signal",
            "help": "Whether the active input has a live signal (sv check).",
            "cloud_priority": "high",
        },
        "brightness": {"type": "integer", "label": "Brightness", "cloud_priority": "low"},
        "contrast": {"type": "integer", "label": "Contrast", "cloud_priority": "low"},
        "sharpness": {"type": "integer", "label": "Sharpness", "cloud_priority": "low"},
        "color": {"type": "integer", "label": "Color", "cloud_priority": "low"},
        "tint": {
            "type": "integer",
            "label": "Tint",
            "help": "0 = red 50, 100 = green 50 (50 is neutral).",
            "cloud_priority": "low",
        },
        "color_temperature": {
            "type": "integer",
            "label": "Color Temperature",
            "help": "0 = warm 50, 100 = cool 50 (50 is neutral).",
            "cloud_priority": "low",
        },
        "backlight": {"type": "integer", "label": "Backlight", "cloud_priority": "low"},
        "picture_mode": {"type": "string", "label": "Picture Mode", "cloud_priority": "low"},
        "aspect_ratio": {"type": "string", "label": "Aspect Ratio", "cloud_priority": "low"},
        "energy_saving": {"type": "string", "label": "Energy Saving", "cloud_priority": "low"},
        "key_lock": {
            "type": "boolean",
            "label": "Remote/Key Lock",
            "help": "True when the IR remote and local keys are locked.",
            "cloud_priority": "low",
        },
        "temperature": {
            "type": "integer",
            "label": "Temperature (C)",
            "help": "Panel's internal temperature in degrees Celsius.",
            "cloud_priority": "low",
        },
        "usage_hours": {
            "type": "integer",
            "label": "Usage Hours",
            "help": "Total panel-on time reported by the display.",
            "cloud_priority": "low",
        },
        "serial_number": {"type": "string", "label": "Serial Number", "cloud_priority": "low"},
        "software_version": {
            "type": "string",
            "label": "Software Version",
            "cloud_priority": "low",
        },
    }


def _display_param() -> dict[str, Any]:
    return {
        "type": "child_id",
        "child_type": "display",
        "required": True,
        "label": "Display",
    }


def _level_param(label: str, maximum: int = 100) -> dict[str, Any]:
    return {
        "type": "integer",
        "min": 0,
        "max": maximum,
        "required": True,
        "label": label,
        "help": f"{label} 0-{maximum}",
    }


class LGSICPDriver(BaseDriver):
    """LG SICP text protocol driver for commercial displays."""

    DRIVER_INFO = {
        "id": "lg_sicp",
        "name": "LG SICP Display",
        "manufacturer": "LG",
        "category": "display",
        "version": "2.0.2",
        "author": "OpenAVC",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "description": (
            "Controls LG commercial displays via SICP over TCP (port 9761). "
            "Each Set ID on an RS-232 daisy chain is a display child entity "
            "with power, input, volume, mute, screen-off, signal status, "
            "picture settings, and panel health. Covers webOS Signage, LED, "
            "and OLED commercial series."
        ),
        "source_url": "https://aca.im/driver_docs/LG/SM_models.pdf",
        "tags": ["display", "signage", "sicp", "video-wall"],
        "verified": False,
        "simulated": True,
        "protocols": ["lg_sicp"],
        "ports": [9761],
        "compatible_models": [
            {
                "manufacturer": "LG",
                "models": [
                    "webOS Signage displays",
                    "LED Commercial series",
                    "OLED Commercial series",
                ],
                "confidence": "untested",
            },
        ],
        "transport": "tcp",
        "help": {
            "overview": (
                "Controls LG commercial displays using the SICP protocol. "
                "One connection can address several displays daisy-chained "
                "over RS-232 by Set ID — each appears as a display child "
                "entity with power, input, volume, mute, screen blank, "
                "signal status, picture settings, and panel health."
            ),
            "setup": (
                "1. Connect the display (or the chain's first display) to "
                "the network.\n"
                "2. Default control port is 9761.\n"
                "3. In each display's OSD (Settings > General), give it a "
                "unique Set ID. List those IDs in 'Display Set IDs' — e.g. "
                "1 for a single display, or 1,2,3,4 for a daisy-chained "
                "video wall. Enter the IDs exactly as the OSD shows them "
                "(decimal); the driver handles the protocol's hex encoding.\n"
                "4. Route commands to a display by picking it from the "
                "Display dropdown; use All Displays On / Off to drive the "
                "whole chain at once."
            ),
        },
        "discovery": {
            # Active probe: a read-only power query to the factory-default
            # Set ID 1 on LG's dedicated SICP port. A display commissioned
            # with a different Set ID stays silent (the hints below still
            # surface it as a candidate).
            "tcp_probe": {
                "port": 9761,
                "send_ascii": "ka 01 FF\r",
                "expect": "a 01 OK",
                "extract_manufacturer": "LG",
            },
            "oui": [
                "00:05:c9",
                "00:e0:91",
                "10:68:3f",
                "2c:54:cf",
                "34:4d:f7",
                "38:8c:50",
                "58:a2:b5",
                "64:99:5d",
                "a8:23:fe",
                "bc:f1:71",
            ],
            "port_open": [9761],
            "manufacturer_alias": ["lg", "lg electronics"],
        },
        "default_config": {
            "host": "",
            "port": 9761,
            "display_ids": "1",
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 9761, "label": "Port"},
            "display_ids": {
                "type": "string",
                "default": "1",
                "label": "Display Set IDs",
                "description": (
                    "Comma-separated Set IDs as shown in each display's OSD "
                    "(1-1000). One ID for a single display; list every ID "
                    "for a daisy-chained wall, e.g. 1,2,3,4. Each becomes a "
                    "display child entity."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "display_count": {"type": "integer", "label": "Displays"},
            "input_options": {
                "type": "string",
                "label": "Input Options (picker)",
                "help": (
                    "JSON list of {value, label} input codes — backs the "
                    "Set Input dropdown."
                ),
            },
            "picture_mode_options": {
                "type": "string",
                "label": "Picture Mode Options (picker)",
                "help": (
                    "JSON list of {value, label} picture-mode codes — backs "
                    "the Set Picture Mode dropdown."
                ),
            },
        },
        "child_entity_types": {
            "display": {
                "label": "Display",
                "label_plural": "Displays",
                "id_format": {
                    "type": "integer",
                    "min": SET_ID_MIN,
                    "max": SET_ID_MAX,
                    "pad_width": 4,
                },
                "state_variables": _child_state_vars(),
                "summary_fields": ["power", "input", "volume"],
                "label_field": "label",
            },
        },
        "quick_actions": ["all_on", "all_off", "refresh"],
        "actions": [
            {"id": "all_on", "kind": "command", "icon": "power"},
            {"id": "all_off", "kind": "command", "icon": "power-off"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
        ],
        "commands": {
            "power_on": {
                "label": "Power On",
                "params": {"display": _display_param()},
                "help": "Turn on a display.",
            },
            "power_off": {
                "label": "Power Off",
                "params": {"display": _display_param()},
                "help": "Turn off a display (standby).",
            },
            "set_input": {
                "label": "Set Input",
                "params": {
                    "display": _display_param(),
                    "input": {
                        "type": "string",
                        "required": True,
                        "options_state": "input_options",
                        "help": (
                            "Input name (e.g. HDMI 1, DisplayPort) or raw "
                            "SICP code (e.g. 90). The dropdown lists the "
                            "documented inputs; you can still type any "
                            "two-hex-digit code your model supports."
                        ),
                    },
                },
                "help": "Switch a display's input source.",
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "display": _display_param(),
                    "level": _level_param("Volume"),
                },
                "help": "Set a display's speaker volume (0-100).",
            },
            "mute_on": {
                "label": "Mute On",
                "params": {"display": _display_param()},
                "help": "Mute a display's audio.",
            },
            "mute_off": {
                "label": "Mute Off",
                "params": {"display": _display_param()},
                "help": "Unmute a display's audio.",
            },
            "screen_off": {
                "label": "Screen Off (blank)",
                "params": {"display": _display_param()},
                "help": (
                    "Blank a display's picture (panel stays powered — "
                    "useful during presentations)."
                ),
            },
            "screen_on": {
                "label": "Screen On",
                "params": {"display": _display_param()},
                "help": "Restore a display's picture from blank.",
            },
            "set_brightness": {
                "label": "Set Brightness",
                "params": {
                    "display": _display_param(),
                    "level": _level_param("Brightness"),
                },
                "help": "Set a display's picture brightness (0-100).",
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {
                    "display": _display_param(),
                    "level": _level_param("Contrast"),
                },
                "help": "Set a display's picture contrast (0-100).",
            },
            "set_sharpness": {
                "label": "Set Sharpness",
                "params": {
                    "display": _display_param(),
                    "level": _level_param("Sharpness", maximum=50),
                },
                "help": "Set a display's picture sharpness (0-50).",
            },
            "set_color": {
                "label": "Set Color",
                "params": {
                    "display": _display_param(),
                    "level": _level_param("Color"),
                },
                "help": "Set a display's color saturation (0-100).",
            },
            "set_tint": {
                "label": "Set Tint",
                "params": {
                    "display": _display_param(),
                    "level": _level_param("Tint"),
                },
                "help": "Set a display's tint (0 = red 50, 100 = green 50).",
            },
            "set_color_temperature": {
                "label": "Set Color Temperature",
                "params": {
                    "display": _display_param(),
                    "level": _level_param("Color Temperature"),
                },
                "help": (
                    "Set a display's color temperature "
                    "(0 = warm 50, 100 = cool 50)."
                ),
            },
            "set_backlight": {
                "label": "Set Backlight",
                "params": {
                    "display": _display_param(),
                    "level": _level_param("Backlight"),
                },
                "help": "Set a display's backlight level (0-100).",
            },
            "set_picture_mode": {
                "label": "Set Picture Mode",
                "params": {
                    "display": _display_param(),
                    "mode": {
                        "type": "string",
                        "required": True,
                        "options_state": "picture_mode_options",
                        "help": (
                            "Picture mode name (e.g. Standard, Cinema) or "
                            "raw dx code (e.g. 01). Not every model offers "
                            "every mode — the display rejects an "
                            "unsupported one."
                        ),
                    },
                },
                "help": "Set a display's picture mode preset.",
            },
            "set_aspect_ratio": {
                "label": "Set Aspect Ratio",
                "params": {
                    "display": _display_param(),
                    "aspect": {
                        "type": "enum",
                        "values": list(ASPECT_SET.keys()),
                        "required": True,
                        "help": (
                            "Aspect ratio. Available modes depend on the "
                            "input signal; Cinema Zoom steps are reachable "
                            "via raw_command (kc, codes 10-1F)."
                        ),
                    },
                },
                "help": "Set a display's aspect ratio.",
            },
            "set_energy_saving": {
                "label": "Set Energy Saving",
                "params": {
                    "display": _display_param(),
                    "mode": {
                        "type": "enum",
                        "values": list(ENERGY_SAVING_SET.keys()),
                        "required": True,
                        "help": "Energy Saving level (Screen Off blanks the panel).",
                    },
                },
                "help": "Set a display's Energy Saving mode.",
            },
            "remote_lock_on": {
                "label": "Remote/Key Lock On",
                "params": {"display": _display_param()},
                "help": (
                    "Lock the IR remote and local keys (the power key still "
                    "works while the display is off)."
                ),
            },
            "remote_lock_off": {
                "label": "Remote/Key Lock Off",
                "params": {"display": _display_param()},
                "help": "Unlock the IR remote and local keys.",
            },
            "send_key": {
                "label": "Send IR Key",
                "params": {
                    "display": _display_param(),
                    "code": {
                        "type": "string",
                        "required": True,
                        "pattern": "^[0-9A-Fa-f]{2}$",
                        "help": (
                            "IR key code as two hex digits (mc command) — "
                            "e.g. 08 power, 44 OK, 7C home. See the manual's "
                            "IR codes table."
                        ),
                    },
                },
                "help": "Send an IR remote key code to a display (menu navigation).",
            },
            "raw_command": {
                "label": "Raw SICP Command",
                "params": {
                    "display": _display_param(),
                    "command": {
                        "type": "string",
                        "required": True,
                        "pattern": "^[a-z]{2}$",
                        "help": "Two-character SICP command, e.g. kc, dd, fe.",
                    },
                    "data": {
                        "type": "string",
                        "required": True,
                        "help": (
                            "Hex data field(s), space-separated when the "
                            "command takes several — e.g. FF, 01, or "
                            "'f1 ff ff'."
                        ),
                    },
                },
                "help": (
                    "Send any SICP command from the manual (timers, tile "
                    "mode, white balance, ...). The ack updates state when "
                    "the command is one the driver tracks."
                ),
            },
            "all_on": {
                "label": "All Displays On",
                "params": {},
                "help": "Turn on every display on the chain.",
            },
            "all_off": {
                "label": "All Displays Off",
                "params": {},
                "help": "Turn off (standby) every display on the chain.",
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Re-query every display's status.",
            },
        },
    }

    # Queries whose acks carry per-display state, keyed by the full command
    # (Cmd1+Cmd2). The correlation queue maps an ack's bare Cmd2 back here.
    _HOT_QUERIES = ("xb", "kf", "ke", "kd")
    _FULL_QUERIES = (
        "kg", "kh", "kk", "ki", "kj", "xu", "mg", "dx", "kc", "jq", "km",
        "dn", "dl",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # In-flight (command, set_id) pairs awaiting an ack, oldest first.
        self._pending: deque[tuple[str, int]] = deque(maxlen=256)

    # ── Roster ──

    def _parse_display_ids(self) -> list[int]:
        """Set IDs declared in config, de-duplicated and range-checked.

        Accepts commas or semicolons; ignores blanks and out-of-range
        values. Falls back to [1] so a misconfigured device still exposes
        one display.
        """
        raw = str(self.config.get("display_ids", "1"))
        ids: list[int] = []
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                continue
            if SET_ID_MIN <= n <= SET_ID_MAX and n not in ids:
                ids.append(n)
        return ids or [1]

    def _reconcile_displays(self) -> None:
        """Register a ``display`` child per configured Set ID; drop any
        child whose Set ID is no longer configured."""
        want = self._parse_display_ids()
        current = set(self.list_children("display"))
        for set_id in want:
            self.register_child("display", set_id)  # idempotent
        for set_id in current - set(want):
            self.deregister_child("display", set_id)
        self.set_state("display_count", len(want))

    def _publish_options(self) -> None:
        """Publish the {value, label} picker lists behind set_input /
        set_picture_mode (options_state)."""
        self.set_state(
            "input_options",
            json.dumps([{"value": c, "label": n} for c, n in INPUT_CODES.items()]),
        )
        self.set_state(
            "picture_mode_options",
            json.dumps(
                [{"value": c, "label": n} for c, n in PICTURE_MODE_CODES.items()]
            ),
        )

    # ── Framing hooks (BaseDriver builds the transport from these) ──

    def _create_frame_parser(self) -> Optional[FrameParser]:
        """Regex ack scanner (see module docstring — 'x' is both the frame
        terminator and a valid leading Cmd2)."""
        return CallableFrameParser(_parse_sicp_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        return None

    # ── Lifecycle ──

    async def _initial_sync(self) -> None:
        """Register the display roster and read initial state."""
        self._pending.clear()
        self._reconcile_displays()
        self._publish_options()
        await self._identify_displays()
        await self.poll()

    async def _identify_displays(self) -> None:
        """One-time identity reads (serial number, software version)."""
        for set_id in self.list_children("display"):
            await self._send_to("fy", set_id, "FF")
            await self._send_to("fz", set_id, "FF")

    async def refresh_children(self) -> dict[str, Any]:
        """Re-sync the display roster from config and re-read every
        display's live state — backs the IDE 'Refresh from Device' button.

        The roster is declared in ``display_ids`` (an RS-232 chain reports
        no device list to enumerate), so this reconciles against the
        current config and re-polls rather than discovering new units."""
        self._reconcile_displays()
        await self._identify_displays()
        await self.poll()
        return {"displays": len(self.list_children("display"))}

    # ── Sending ──

    def _coerce_child_ids(self, command: str, params: dict[str, Any]) -> None:
        """Coerce any child_id-typed param to a bare int (the IDE child
        picker hands back a zero-padded string)."""
        cmd_def = self.DRIVER_INFO["commands"].get(command, {})
        for pname, pdef in cmd_def.get("params", {}).items():
            if pdef.get("type") == "child_id" and pname in params and params[pname] != "":
                try:
                    params[pname] = int(params[pname])
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"{command}: parameter {pname!r} must be an integer "
                        f"Set ID, got {params[pname]!r}"
                    ) from e

    async def _send_to(self, cmd: str, display: Any, data: str) -> None:
        """Send one SICP command to a display's Set ID and queue it for
        ack correlation."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        set_id = int(display)
        self._pending.append((cmd, set_id))
        await self.transport.send(_build_sicp_command(cmd, set_id, data))

    @staticmethod
    def _resolve_code(
        value: Any, codes: dict[str, str], by_label: dict[str, str]
    ) -> Optional[str]:
        """Resolve a picker/free-text value to a wire code: an exact code,
        any two-hex-digit code (forgiving, for model-specific extras), or a
        normalized label."""
        v = str(value).strip()
        if v.upper() in codes:
            return v.upper()
        if re.fullmatch(r"[0-9A-Fa-f]{2}", v):
            return v.upper()
        return by_label.get(_norm_label(v))

    async def _send_level(self, display: Any, cmd: str, level: Any, maximum: int) -> None:
        value = max(0, min(maximum, int(level)))
        await self._send_to(cmd, display, format(value, "02X"))

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a named command to a display (or the whole chain)."""
        params = params or {}
        self._coerce_child_ids(command, params)
        display = params.get("display")

        match command:
            case "power_on":
                await self._send_to("ka", display, "01")
            case "power_off":
                await self._send_to("ka", display, "00")
            case "set_input":
                code = self._resolve_code(
                    params.get("input", ""), INPUT_CODES, _INPUT_BY_LABEL
                )
                if code is None:
                    log.warning(
                        f"[{self.device_id}] Unknown input: {params.get('input')}"
                    )
                    return
                await self._send_to("xb", display, code)
            case "set_volume":
                await self._send_level(display, "kf", params.get("level", 0), 100)
            case "mute_on":
                await self._send_to("ke", display, "00")  # inverted on the wire
            case "mute_off":
                await self._send_to("ke", display, "01")
            case "screen_off":
                await self._send_to("kd", display, "01")
            case "screen_on":
                await self._send_to("kd", display, "00")
            case "set_brightness":
                await self._send_level(display, "kh", params.get("level", 0), 100)
            case "set_contrast":
                await self._send_level(display, "kg", params.get("level", 0), 100)
            case "set_sharpness":
                await self._send_level(display, "kk", params.get("level", 0), 50)
            case "set_color":
                await self._send_level(display, "ki", params.get("level", 0), 100)
            case "set_tint":
                await self._send_level(display, "kj", params.get("level", 0), 100)
            case "set_color_temperature":
                await self._send_level(display, "xu", params.get("level", 0), 100)
            case "set_backlight":
                await self._send_level(display, "mg", params.get("level", 0), 100)
            case "set_picture_mode":
                code = self._resolve_code(
                    params.get("mode", ""), PICTURE_MODE_CODES, _PICTURE_MODE_BY_LABEL
                )
                if code is None:
                    log.warning(
                        f"[{self.device_id}] Unknown picture mode: "
                        f"{params.get('mode')}"
                    )
                    return
                await self._send_to("dx", display, code)
            case "set_aspect_ratio":
                code = ASPECT_SET.get(str(params.get("aspect", "")))
                if code is None:
                    log.warning(
                        f"[{self.device_id}] Unknown aspect ratio: "
                        f"{params.get('aspect')}"
                    )
                    return
                await self._send_to("kc", display, code)
            case "set_energy_saving":
                code = ENERGY_SAVING_SET.get(str(params.get("mode", "")))
                if code is None:
                    log.warning(
                        f"[{self.device_id}] Unknown energy saving mode: "
                        f"{params.get('mode')}"
                    )
                    return
                await self._send_to("jq", display, code)
            case "remote_lock_on":
                await self._send_to("km", display, "01")
            case "remote_lock_off":
                await self._send_to("km", display, "00")
            case "send_key":
                code = str(params.get("code", "")).strip()
                if not re.fullmatch(r"[0-9A-Fa-f]{2}", code):
                    log.warning(f"[{self.device_id}] Bad IR key code: {code!r}")
                    return
                await self._send_to("mc", display, code.upper())
            case "raw_command":
                cmd = str(params.get("command", "")).strip().lower()
                if not re.fullmatch(r"[a-z]{2}", cmd):
                    log.warning(f"[{self.device_id}] Bad raw command: {cmd!r}")
                    return
                await self._send_to(cmd, display, str(params.get("data", "")).strip())
            case "all_on":
                for set_id in self.list_children("display"):
                    await self._send_to("ka", set_id, "01")
            case "all_off":
                for set_id in self.list_children("display"):
                    await self._send_to("ka", set_id, "00")
            case "refresh":
                await self.poll()
            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

        log.debug(f"[{self.device_id}] Sent command: {command} {params}")

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        """Correlate one ack frame to its in-flight command and update the
        answering display's child state."""
        try:
            text = data.decode("ascii", errors="replace")
        except Exception:
            return
        m = re.fullmatch(r"([a-z]) ([0-9A-Fa-f]{2,3}) (OK|NG)([^x]*)x", text)
        if not m:
            return
        cmd2, sid_hex, status, payload = m.groups()
        set_id = int(sid_hex, 16)

        cmd = self._match_pending(cmd2, set_id)
        if cmd is None:
            log.debug(
                f"[{self.device_id}] Unmatched ack '{text.strip()}' — ignoring"
            )
            return
        if status == "NG":
            # Unsupported command/value on this model (or in this mode) —
            # normal per the manual; leave the prop unpopulated.
            log.debug(
                f"[{self.device_id}] Display {set_id} NG for {cmd} "
                f"(data {payload!r})"
            )
            return

        updates = self._parse_ack(cmd, payload)
        if updates and self.is_child_registered("display", set_id):
            self.set_child_state_batch("display", set_id, updates)

    def _match_pending(self, cmd2: str, set_id: int) -> Optional[str]:
        """Pop the oldest in-flight command this ack answers.

        Acks preserve send order on the shared bus, so anything queued
        before the match went unanswered (display off, absent Set ID) and
        is pruned with it. An ack that matches nothing leaves the queue
        untouched — a stray frame must not break correlation for the
        commands still in flight.
        """
        for i, (cmd, sid) in enumerate(self._pending):
            if cmd[1] == cmd2 and sid == set_id:
                for _ in range(i + 1):
                    self._pending.popleft()
                return cmd
        return None

    def _parse_ack(self, cmd: str, data: str) -> dict[str, Any]:
        """Map an OK ack's echoed data to child props, keyed by the full
        command that elicited it (the ack alone can't distinguish kg from
        mg — both echo Cmd2 'g')."""
        data = data.strip()
        match cmd:
            case "ka":
                if data in ("00", "01"):
                    return {"power": "on" if data == "01" else "off"}
                return {}
            case "xb":
                code = data.upper()
                return {"input": INPUT_CODES.get(code, f"Input 0x{code}")}
            case "kf":
                value = _hex_or_none(data)
                return {"volume": value} if value is not None else {}
            case "ke":
                if data in ("00", "01"):
                    return {"mute": data == "00"}  # inverted: 00 = muted
                return {}
            case "kd":
                if data in ("00", "01"):
                    return {"screen_off": data == "01"}
                return {}
            case "kg" | "kh" | "kk" | "ki" | "kj" | "xu" | "mg":
                prop = {
                    "kg": "contrast",
                    "kh": "brightness",
                    "kk": "sharpness",
                    "ki": "color",
                    "kj": "tint",
                    "xu": "color_temperature",
                    "mg": "backlight",
                }[cmd]
                value = _hex_or_none(data)
                return {prop: value} if value is not None else {}
            case "dx":
                code = data.upper()
                return {
                    "picture_mode": PICTURE_MODE_CODES.get(code, f"Mode 0x{code}")
                }
            case "kc":
                code = data.upper()
                if code in ASPECT_NAMES:
                    return {"aspect_ratio": ASPECT_NAMES[code]}
                value = _hex_or_none(code)
                if value is not None and 0x10 <= value <= 0x1F:
                    return {"aspect_ratio": f"Cinema Zoom {value - 0x0F}"}
                return {"aspect_ratio": f"Aspect 0x{code}"} if code else {}
            case "jq":
                code = data.upper()
                return {
                    "energy_saving": ENERGY_SAVING_NAMES.get(code, f"Mode 0x{code}")
                }
            case "km":
                if data in ("00", "01"):
                    return {"key_lock": data == "01"}
                return {}
            case "dn":
                value = _hex_or_none(data)
                return {"temperature": value} if value is not None else {}
            case "dl":
                value = _hex_or_none(data)
                return {"usage_hours": value} if value is not None else {}
            case "fy":
                return {"serial_number": data} if data else {}
            case "fz":
                return {"software_version": data} if data else {}
            case "sv":
                # Signal check ack: 02 + 01 (signal) / 00 (none).
                if data.upper().startswith("02") and len(data) >= 4:
                    return {"signal": "present" if data[2:4] == "01" else "none"}
                return {}
        return {}

    # ── Polling ──

    async def poll(self) -> None:
        """Query every configured display.

        The power query always goes out; the rest of the surface is only
        queried while the display last reported "on" (per the manual, most
        commands are only answered when the panel is fully powered), so a
        display in standby costs one frame per cycle and the full state
        refreshes on the cycle after it comes back on.
        """
        if not self.transport or not self.transport.connected:
            return
        try:
            for set_id in self.list_children("display"):
                await self._send_to("ka", set_id, "FF")
                if self.get_child_state("display", set_id).get("power") != "on":
                    continue
                for cmd in self._HOT_QUERIES:
                    await self._send_to(cmd, set_id, "FF")
                await self._send_to("sv", set_id, "02 FF")
                for cmd in self._FULL_QUERIES:
                    await self._send_to(cmd, set_id, "FF")
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")
