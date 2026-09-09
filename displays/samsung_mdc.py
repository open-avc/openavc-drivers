"""
OpenAVC Samsung MDC (Multiple Display Control) Driver.

Controls Samsung commercial displays via TCP using the MDC binary protocol.
Default port: 1515.

Request frame (host -> display):
    [0xAA] [CMD] [ID] [LEN] [DATA...] [CHECKSUM]

Response frame (display -> host):
    [0xAA] [0xFF] [ID] [LEN] [ACK/NAK] [r-CMD] [VALUES...] [CHECKSUM]

In a response the byte after the 0xAA header is always 0xFF (not the command).
ACK is 0x41 ('A') and NAK is 0x4E ('N'); r-CMD echoes the command being
answered, and the value bytes follow it. The checksum is the sum of every byte
after the header, masked to 0xFF. The frame parser strips the 0xAA header and
the trailing checksum before handing the body to on_data_received().

Multi-display (Set ID) model:
    MDC is designed to address several daisy-chained displays over one
    connection, each carrying a unique Set ID in the request's ID byte; every
    response echoes the Set ID that answered. This driver models each Set ID as
    a ``display`` child entity, so a video wall or signage chain surfaces one
    controllable unit per display (power / volume / mute / input plus the
    picture settings brightness, contrast, backlight, picture mode and color
    tone) instead of collapsing to a single device. The roster is declared in
    the ``display_ids`` config (the Set IDs an installer assigned when
    commissioning the chain); a single display is simply a chain of one.

    Per-display picture values live as writable child props (set by a command
    that names the display, read back on the next poll) rather than
    device_settings — a device setting's flat state_key can't address a child.
    Same reasoning as the RackLink PDU's per-outlet state.

Why every request is correlated and awaited (measured on a DM75E):
    The display answers whichever TCP connection opened LAST, whoever asked.
    A socket that was serving fine goes silent the moment anything else opens
    port 1515 — a second controller, a discovery probe, a vendor tool — and it
    is never handed back: no FIN, no RST, just silence, so the transport stays
    "connected" and the driver would poll a dead socket forever. Only a
    reconnect restores service. That has two consequences this driver is built
    around:

      * Liveness is a protocol question, not a transport one. ``poll()`` awaits
        each reply and raises on silence, and ``_liveness_probe`` gives the
        BaseDriver watchdog the same signal when polling is switched off, so a
        deaf socket is torn down and reconnected instead of going stale.
      * A reply cannot be assumed to answer the request that preceded it. While
        this driver holds the newest socket it also receives another
        controller's replies, and the display re-delivers undelivered frames
        from before a reconnect. Every request therefore waits on its own
        (Set ID, command) waiter, and an uncorrelated frame updates state but
        satisfies nobody's wait.

    A GET the display NAKs is a model capability gap, not an error: MDC's
    command set is nominally shared but 9 of 27 commands probed on the DM75E
    are unsupported (colour tone among them). The first NAK marks that command
    unsupported for that display and drops it from the poll, so an unsupported
    setting costs one frame per connection instead of one warning every cycle.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from openavc.core.connection_fault import CHILD_NOT_RESPONDING
from openavc.drivers.base import BaseDriver
from openavc.transport.binary_helpers import checksum_sum
from openavc.transport.frame_parsers import CallableFrameParser, FrameParser
from openavc.utils.logger import get_logger

log = get_logger(__name__)

# MDC command bytes
CMD_STATUS = 0x00
CMD_POWER = 0x11
CMD_VOLUME = 0x12
CMD_MUTE = 0x13
CMD_INPUT = 0x14
CMD_CONTRAST = 0x24
CMD_BRIGHTNESS = 0x25
CMD_COLOR_TONE = 0x3E
CMD_BACKLIGHT = 0x58  # "Manual Lamp" — panel backlight level 0-100
CMD_PICTURE_MODE = 0x71
CMD_SHARPNESS = 0x26
CMD_SW_VERSION = 0x0E
CMD_MODEL_NAME = 0x8A

# Response framing: every response uses 0xFF in the command position, followed
# by an ACK ('A') or NAK ('N') byte before the echoed command.
RESPONSE_CMD = 0xFF
ACK = 0x41  # 'A'
NAK = 0x4E  # 'N'

# How long one request waits for its echoed reply, unless the driver's
# REPLY_TIMEOUT_S class attribute overrides it. The DM75E answers in 9-23 ms on
# a quiet link; a second is slack for a busy chain, and the caller (poll /
# liveness / a command) decides what a timeout means.
REPLY_TIMEOUT_S = 1.0

# Setting volume on a DM75E clears mute, and the display then refuses a mute
# command for about two seconds (measured: refused at every gap up to 1.5s,
# accepted from 2.0s). A mute issued inside that window is NAKed, so a macro
# that sets a level and then mutes would fail on the second step. The driver
# waits out the remainder instead, and re-reads mute after a volume change
# rather than assuming the clear happened.
VOLUME_MUTE_GUARD_S = 2.2

# The commands poll() reads back, in order. STATUS fans out power/volume/mute/
# input; the rest are one value each. A command the display NAKs is dropped
# from this list for that Set ID until the next connect.
POLL_COMMANDS = (
    CMD_STATUS,
    CMD_CONTRAST,
    CMD_BRIGHTNESS,
    CMD_BACKLIGHT,
    CMD_SHARPNESS,
    CMD_COLOR_TONE,
    CMD_PICTURE_MODE,
)

# Valid Set ID range on the wire (the ID byte). 0xFF is reserved for broadcast
# and is not an addressable unit.
SET_ID_MIN = 0
SET_ID_MAX = 254

# MDC input source codes (full Samsung source set). Ordered with the inputs an
# integrator switches most often first; the rest cover PC-mode variants and the
# display's internal/platform sources so status read-back always resolves.
INPUT_MAP = {
    "hdmi1": 0x21,
    "hdmi2": 0x23,
    "hdmi3": 0x31,
    "hdmi4": 0x33,
    "dp1": 0x25,
    "dp2": 0x26,
    "dp3": 0x27,
    "dvi": 0x18,
    "pc": 0x14,  # analog RGB (D-sub / "PC")
    "hdbaset": 0x55,
    "component": 0x08,
    "av": 0x0C,
    "av2": 0x0D,
    "s_video": 0x04,
    "scart1": 0x0E,
    "bnc": 0x1E,
    "rf_tv": 0x30,
    "tv_dtv": 0x40,
    "hdmi1_pc": 0x22,
    "hdmi2_pc": 0x24,
    "hdmi3_pc": 0x32,
    "hdmi4_pc": 0x34,
    "dvi_video": 0x1F,
    "magic_info": 0x20,
    "magic_info_s": 0x60,
    "url_launcher": 0x63,
    "web_browser": 0x65,
    "internal_usb": 0x62,
    "widi": 0x61,
    "iwb": 0x64,
    "remote_workspace": 0x66,
    "ocm": 0x56,
    "plug_in_mode": 0x50,
    "none": 0x00,
}
INPUT_REVERSE = {v: k for k, v in INPUT_MAP.items()}

# Picture Mode (cmd 0x71). Full read-back map (every documented mode byte ->
# a friendly name) so a polled value always resolves. The settable subset is
# the modes common across the signage / commercial lines — the display NAKs any
# mode a given model doesn't offer.
PICTURE_MODE_NAMES = {
    0x00: "dynamic",
    0x01: "standard",
    0x02: "movie",
    0x03: "custom_tv",
    0x04: "natural",
    0x05: "calibration_tv",
    0x10: "entertain",
    0x11: "internet",
    0x12: "text",
    0x13: "custom",
    0x14: "advertisement",
    0x15: "information",
    0x16: "calibration",
    0x20: "shop_mall_video",
    0x21: "shop_mall_text",
    0x22: "office_school_video",
    0x23: "office_school_text",
    0x24: "terminal_station_video",
    0x25: "terminal_station_text",
    0x26: "video_wall_video",
    0x27: "video_wall_text",
    0x30: "hdr_plus",
    0x50: "off",
}
# Settable modes = every documented mode except ``off`` (0x50), which is a
# read-back state the display refuses as a target. Which of these a given model
# accepts varies wildly — a DM75E takes ``calibration`` and the eight signage
# modes (0x20-0x27) and NAKs all twelve TV-style modes — so the driver offers
# the documented set and surfaces the display's refusal instead of guessing a
# subset. An earlier hand-picked list of eight was wrong for signage: six of
# its entries NAK on a DM75E and seven of the modes that work were missing.
PICTURE_MODE_SET = {
    name: byte for byte, name in PICTURE_MODE_NAMES.items() if name != "off"
}

# Color Tone (cmd 0x3E) — the Cool/Normal/Warm preset shown as "Color Tone" in
# the on-screen menu. A closed set, so it round-trips as an enum both ways.
# (The absolute Color Temperature Kelvin value, cmd 0x3F, has model-varying
# ranges; the preset is model-consistent and matches the menu wording.)
COLOR_TONE_NAMES = {
    0x00: "cool2",
    0x01: "cool1",
    0x02: "normal",
    0x03: "warm1",
    0x04: "warm2",
    0x50: "off",
}
COLOR_TONE_SET = {v: k for k, v in COLOR_TONE_NAMES.items()}

# What to call a command in a message an integrator reads.
_COMMAND_NAMES = {
    CMD_STATUS: "status",
    CMD_POWER: "power",
    CMD_VOLUME: "volume",
    CMD_MUTE: "mute",
    CMD_INPUT: "input source",
    CMD_CONTRAST: "contrast",
    CMD_BRIGHTNESS: "brightness",
    CMD_SHARPNESS: "sharpness",
    CMD_COLOR_TONE: "colour tone",
    CMD_BACKLIGHT: "backlight",
    CMD_PICTURE_MODE: "picture mode",
}


def _build_mdc_frame(cmd: int, display_id: int, data: bytes = b"") -> bytes:
    """Build a Samsung MDC frame with header and checksum."""
    frame = bytes([cmd, display_id, len(data)]) + data
    cs = checksum_sum(frame)
    return bytes([0xAA]) + frame + bytes([cs])


def _parse_mdc_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """
    Parse a Samsung MDC frame from a byte buffer.

    Returns (frame_bytes, remaining_buffer) or (None, buffer) if incomplete.
    A valid frame is returned WITHOUT the 0xAA header and checksum — just
    the cmd, id, length, and data portion.
    """
    while True:
        # Find the start marker
        start = buffer.find(0xAA)
        if start == -1:
            return None, b""  # No valid data, discard
        if start > 0:
            buffer = buffer[start:]  # Skip garbage before header

        # Need at least: header(1) + cmd(1) + id(1) + len(1) = 4 bytes
        if len(buffer) < 4:
            return None, buffer

        data_len = buffer[3]
        total_len = 4 + data_len + 1  # header + cmd + id + len + data + checksum

        if len(buffer) < total_len:
            return None, buffer

        frame = buffer[1 : total_len - 1]  # Exclude header and checksum
        # Verify the checksum before trusting the framing. 0xAA is a legal
        # data and checksum byte, so a resync after a truncated read can land
        # mid-frame and every later frame would be shifted; an unverified
        # parser turns that into plausible-looking garbage written to state
        # (a volume of 170, an input that decodes to nothing). A frame whose
        # checksum disagrees is dropped one byte at a time until the framing
        # lines up again.
        if (sum(frame) & 0xFF) != buffer[total_len - 1]:
            log.debug(
                f"MDC checksum mismatch, resyncing: {buffer[:total_len].hex()}"
            )
            buffer = buffer[1:]
            continue

        remaining = buffer[total_len:]
        return frame, remaining


def _lookup(table: dict[str, int], name: str, what: str) -> int:
    """Resolve a named enum value, refusing an unknown one.

    Returning quietly on an unrecognised name (what this used to do) makes a
    typo in a macro indistinguishable from a working step.
    """
    code = table.get(name)
    if code is None:
        raise ValueError(
            f"Unknown {what}: {name!r}. Expected one of: "
            f"{', '.join(sorted(table))}"
        )
    return code


def _child_state_vars() -> dict[str, dict[str, Any]]:
    """State variables for one ``display`` child entity.

    Power / volume / mute / input are the operationally-hot values (high cloud
    tier); the picture settings change rarely (low tier).
    """
    return {
        "power": {
            "type": "enum",
            "values": ["off", "on"],
            "label": "Power",
            "cloud_priority": "high",
        },
        "volume": {"type": "integer", "label": "Volume", "cloud_priority": "high"},
        "mute": {"type": "boolean", "label": "Mute", "cloud_priority": "high"},
        "input": {
            "type": "enum",
            "values": list(INPUT_MAP.keys()),
            "label": "Input Source",
            "cloud_priority": "high",
        },
        "brightness": {
            "type": "integer",
            "label": "Brightness",
            "cloud_priority": "low",
        },
        "contrast": {"type": "integer", "label": "Contrast", "cloud_priority": "low"},
        "backlight": {"type": "integer", "label": "Backlight", "cloud_priority": "low"},
        "sharpness": {"type": "integer", "label": "Sharpness", "cloud_priority": "low"},
        "picture_mode": {
            "type": "string",
            "label": "Picture Mode",
            "cloud_priority": "low",
        },
        "color_tone": {
            "type": "enum",
            "values": list(COLOR_TONE_SET.keys()),
            "label": "Color Tone",
            "cloud_priority": "low",
        },
    }


class SamsungMDCDriver(BaseDriver):
    """Samsung MDC binary protocol driver for commercial displays."""

    # A Samsung display goes silent without closing the socket the moment
    # anything else opens port 1515, so the link has to be proved at the
    # protocol level. 30s between probes keeps a display that is merely busy
    # (an input change stops answering for a few seconds) from tripping it,
    # and two consecutive misses is ~60s before the reconnect — comfortably
    # longer than the ~20-30s a DM75E spends unreachable while powering on.
    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    #: Per-request reply deadline. A class attribute so a slow chain can be
    #: given more room without touching the watchdog's own budget.
    REPLY_TIMEOUT_S = REPLY_TIMEOUT_S
    #: How long after a volume change this display refuses a mute command.
    VOLUME_MUTE_GUARD_S = VOLUME_MUTE_GUARD_S
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the display stopped answering. A Samsung display "
        "serves only the connection that opened most recently. Another "
        "controller, a discovery scan, or a vendor tool has taken it over. "
        "The connection is being rebuilt."
    )

    DRIVER_INFO = {
        "id": "samsung_mdc",
        "name": "Samsung MDC Display",
        "manufacturer": "Samsung",
        "category": "display",
        "version": "1.6.0",
        "author": "OpenAVC",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "description": (
            "Controls Samsung commercial displays via the MDC (Multiple "
            "Display Control) binary protocol over TCP. Each Set ID on the "
            "chain is a display child entity with power, volume, mute, input, "
            "and picture settings (brightness, contrast, backlight, picture "
            "mode, color tone)."
        ),
        "source_url": "https://github.com/vgavro/samsung-mdc",
        "tags": ["display", "signage", "mdc", "video-wall"],
        "verified": True,
        "simulated": True,
        "protocols": ["samsung_mdc"],
        "ports": [1515],
        "compatible_models": [
            {
                "manufacturer": "Samsung",
                "models": ["DM75E"],
                "confidence": "full",
                "notes": (
                    "Verified on hardware (firmware T-GFSLE2AKUC-1037.2): "
                    "power, volume, mute, input, contrast, brightness, "
                    "backlight, sharpness and picture mode all round-trip. "
                    "Colour tone (0x3E) is not implemented on this model and "
                    "is dropped from the poll on its first NAK; colour "
                    "temperature (0x3F) answers instead. Picture mode accepts "
                    "calibration and the signage modes 0x20-0x27 only."
                ),
            },
            {
                "manufacturer": "Samsung",
                "models": [
                    "Smart Signage series",
                    "The Wall series",
                    "LED Commercial series",
                    "SMART Signage Platform",
                ],
                "confidence": "partial",
                "notes": (
                    "Same MDC command set as the verified DM75E, but which "
                    "commands and which picture modes a model implements "
                    "varies; unsupported ones are reported rather than "
                    "silently ignored."
                ),
            },
        ],
        "transport": "tcp",
        "help": {
            "overview": (
                "Controls Samsung commercial displays using the MDC binary "
                "protocol. Covers Smart Signage, The Wall, LED, and SMART "
                "Signage Platform series. One connection can address several "
                "displays daisy-chained by Set ID. Each appears as a display "
                "child entity."
            ),
            "setup": (
                "1. Connect the display (or the chain's master) to the "
                "network.\n"
                "2. Enable MDC / network control in the display's settings.\n"
                "3. Default port is 1515.\n"
                "4. In the display's menu, give each display a unique Set ID "
                "(ID). List those IDs in 'Display Set IDs'. Use 1 for a "
                "single display, or 1,2,3,4 for a four-panel wall.\n"
                "5. Route commands to a display by picking it from the Display "
                "dropdown; use All Displays On / Off to drive the whole chain "
                "at once."
            ),
        },
        "discovery": {
            # Samsung MDC is binary on TCP/1515. Get-Serial-Number
            # (AA 0B 01 00 0C) elicits a fixed-prefix ACK starting with
            # AA FF on any MDC-speaking display, regardless of model.
            "tcp_probe": {
                "port": 1515,
                "send_hex": "AA0B01000C",
                "expect_hex": "AAFF",
                "extract_manufacturer": "Samsung",
            },
            "oui": [
                "00:07:ab",
                "00:e0:64",
                "14:49:e0",
                "34:c3:d2",
                "64:b5:c6",
                "8c:71:f8",
                "b4:79:a7",
                "d0:03:4b",
            ],
        },
        "default_config": {
            "host": "",
            "port": 1515,
            "display_ids": "1",
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 1515, "label": "Port"},
            "display_ids": {
                "type": "string",
                "default": "1",
                "label": "Display Set IDs",
                "description": (
                    "Comma-separated MDC Set IDs on the chain (0-254). One ID "
                    "for a single display; list every ID for a daisy-chained "
                    "wall, e.g. 1,2,3,4. Each becomes a display child entity."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often to re-read every display. Set 0 to stop "
                    "polling; the keep-alive watchdog still runs, because a "
                    "Samsung display stops answering without closing the "
                    "connection whenever another controller opens port 1515."
                ),
            },
        },
        "state_variables": {
            "display_count": {"type": "integer", "label": "Displays"},
            "model": {
                "type": "string",
                "label": "Model",
                "help": "Model name reported by the chain's first display.",
            },
            "firmware": {
                "type": "string",
                "label": "Firmware",
                "help": "Software version reported by the chain's first display.",
            },
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "help": (
                    "The most recent thing a display refused: a command it "
                    "does not implement, or a Set ID that never answered. "
                    "Clears on the next clean poll."
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
                    "pad_width": 3,
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
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                },
                "help": "Turn on a display.",
            },
            "power_off": {
                "label": "Power Off",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                },
                "help": "Turn off a display (standby).",
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Volume level 0-100",
                    },
                },
                "help": "Set a display's speaker volume.",
            },
            "mute_on": {
                "label": "Mute On",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                },
                "help": "Mute a display's audio.",
            },
            "mute_off": {
                "label": "Mute Off",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                },
                "help": "Unmute a display's audio.",
            },
            "set_input": {
                "label": "Set Input",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "input": {
                        "type": "enum",
                        "values": list(INPUT_MAP.keys()),
                        "required": True,
                        "help": "Input source to switch to",
                    },
                },
                "help": "Switch a display's input source.",
            },
            "set_brightness": {
                "label": "Set Brightness",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Brightness 0-100",
                    },
                },
                "help": "Set a display's picture brightness (0-100).",
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Contrast 0-100",
                    },
                },
                "help": "Set a display's picture contrast (0-100).",
            },
            "set_backlight": {
                "label": "Set Backlight",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Backlight 0-100",
                    },
                },
                "help": (
                    "Set a display's backlight / panel brightness (0-100). "
                    "Some models only accept this when Eco / auto-brightness "
                    "is off."
                ),
            },
            "set_sharpness": {
                "label": "Set Sharpness",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Sharpness 0-100",
                    },
                },
                "help": "Set a display's picture sharpness (0-100).",
            },
            "set_picture_mode": {
                "label": "Set Picture Mode",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "mode": {
                        "type": "enum",
                        "values": list(PICTURE_MODE_SET.keys()),
                        "required": True,
                        "help": "Picture preset",
                    },
                },
                "help": (
                    "Set a display's picture mode. Models differ sharply in "
                    "which presets they offer. Signage panels typically take "
                    "the shop, office, terminal and video-wall modes and "
                    "refuse the TV-style ones. A refused mode is reported in "
                    "Last Error."
                ),
            },
            "set_color_tone": {
                "label": "Set Color Tone",
                "params": {
                    "display": {
                        "type": "child_id",
                        "child_type": "display",
                        "required": True,
                        "label": "Display",
                    },
                    "tone": {
                        "type": "enum",
                        "values": list(COLOR_TONE_SET.keys()),
                        "required": True,
                        "help": "Color tone preset (cool to warm)",
                    },
                },
                "help": "Set a display's color tone preset (Cool 2 through Warm 2).",
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

    def __init__(self, device_id, config, state, events) -> None:
        super().__init__(device_id, config, state, events)
        # Waiters keyed by (set_id, echoed command). A list per key: two polls
        # can never overlap, but a command issued while a poll is in flight
        # can, and the display answers both.
        self._waiters: dict[tuple[int, int], list[asyncio.Future]] = {}
        # (set_id, command) pairs this display NAKed. Reset on every connect,
        # because the roster or the panel behind a Set ID may have changed.
        self._unsupported: set[tuple[int, int]] = set()
        # When this driver last set volume on a Set ID (loop clock), for the
        # mute guard above. Best effort: a volume change from the IR remote or
        # another controller starts the same window and cannot be seen here,
        # so a NAK is still reported rather than assumed away.
        self._volume_set_at: dict[int, float] = {}

    # ── Roster ──

    def _parse_display_ids(self) -> list[int]:
        """Set IDs declared in config, de-duplicated and range-checked.

        Accepts commas or semicolons; ignores blanks and out-of-range values.
        Falls back to [1] so a misconfigured device still exposes one display.
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
        """Register a ``display`` child per configured Set ID; drop any child
        whose Set ID is no longer configured."""
        want = self._parse_display_ids()
        current = set(self.list_children("display"))
        for set_id in want:
            self.register_child("display", set_id)  # idempotent
        for set_id in current - set(want):
            self.deregister_child("display", set_id)
        self.set_state("display_count", len(want))

    # ── Framing hooks (BaseDriver builds the transport from these) ──

    def _create_frame_parser(self) -> Optional[FrameParser]:
        """Use callable parser for MDC binary framing."""
        return CallableFrameParser(_parse_mdc_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        """MDC uses binary framing, not delimiters."""
        return None

    async def _initial_sync(self) -> None:
        """Register the roster, read identity, then take a first reading.

        The unsupported-command set is cleared here rather than in __init__:
        what a Set ID answers is a fact about the panel currently behind it,
        and a reconnect is the one moment that panel may have changed.
        """
        self._unsupported.clear()
        self._reconcile_displays()
        await self._read_identity()
        await self.poll()

    async def _read_identity(self) -> None:
        """Read model and firmware from the first display, best effort.

        Identity is a nicety, not a reason to fail a connect: a display that
        NAKs either command (the DM75E NAKs serial number, for instance) or a
        chain whose first Set ID is absent should still come up.
        """
        set_id = next(iter(self.list_children("display")), None)
        if set_id is None:
            return
        for cmd, prop in ((CMD_MODEL_NAME, "model"), (CMD_SW_VERSION, "firmware")):
            try:
                ack, values = await self._request(set_id, cmd)
            except (TimeoutError, ConnectionError):
                return
            if ack == ACK and values:
                # MDC pads these fields with NULs; keep the printable run.
                text = "".join(
                    chr(b) for b in values if 32 <= b < 127
                ).strip()
                if text:
                    self.set_state(prop, text)

    async def refresh_children(self) -> dict[str, Any]:
        """Re-sync the display roster from config and re-read every display's
        live state — backs the IDE 'Refresh from Device' button.

        The roster is declared in ``display_ids`` (an MDC bus reports no device
        list to enumerate), so this reconciles against the current config and
        re-polls, rather than discovering new units."""
        self._reconcile_displays()
        await self.poll()
        return {"displays": len(self.list_children("display"))}

    def _coerce_child_ids(self, command: str, params: dict[str, Any]) -> None:
        """Coerce any child_id-typed param to a bare int (the IDE child picker
        hands back a zero-padded string)."""
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

    async def _send_to(self, display: Any, cmd: int, data: bytes = b"") -> None:
        """Send one MDC frame to a display's Set ID, without waiting."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        set_id = int(display)
        await self.transport.send(_build_mdc_frame(cmd, set_id, data))

    async def _request(
        self,
        display: Any,
        cmd: int,
        data: bytes = b"",
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        """Send one frame and await the display's echoed reply.

        Returns ``(ack, values)`` — ACK or NAK, whichever came back; both are
        proof the link is alive, which is what the poll and the watchdog need.
        Raises TimeoutError when nothing answers, which is the deaf-socket
        signal the missed-poll watchdog and _liveness_probe both act on.

        Waiting on (Set ID, command) rather than "the next frame" is what makes
        this safe while another controller holds a conversation with the same
        display: its replies still arrive here and still update state, but they
        satisfy their own key, not ours.
        """
        set_id = int(display)
        key = (set_id, cmd)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, []).append(fut)
        try:
            await self._send_to(set_id, cmd, data)
            return await asyncio.wait_for(
                fut, self.REPLY_TIMEOUT_S if timeout is None else timeout
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Display {set_id} did not answer command 0x{cmd:02X}"
            ) from exc
        finally:
            waiters = self._waiters.get(key)
            if waiters and fut in waiters:
                waiters.remove(fut)
            if waiters is not None and not waiters:
                self._waiters.pop(key, None)

    def _resolve_waiter(self, set_id: int, rcmd: int, ack: int, values: bytes) -> bool:
        """Hand a reply to the oldest waiter for that (Set ID, command).

        Returns True when somebody was waiting. The caller then owns what the
        reply means, so on_data_received leaves state alone — that keeps a
        polled value to a single batched write per display instead of one
        write per frame, and keeps NAK handling in the one place that knows
        what was asked.
        """
        waiters = self._waiters.get((set_id, rcmd))
        if not waiters:
            return False
        for fut in list(waiters):
            if not fut.done():
                fut.set_result((ack, values))
                waiters.remove(fut)
                return True
        return False

    # ── Liveness ──

    async def _liveness_probe(self) -> None:
        """Ask the first display for its power state and await the answer.

        BaseDriver's watchdog wraps this in HEALTH_TIMEOUT_S and force-drops
        the transport after HEALTH_MAX_FAILURES misses, so the platform
        reconnects — the only thing that restores service once a Samsung
        display has handed itself to a newer connection. poll() raising on
        silence covers the same ground, but only while polling is switched on;
        this keeps a poll_interval of 0 honest.
        """
        set_id = next(iter(self.list_children("display")), None)
        if set_id is None:
            return
        await self._request(set_id, CMD_POWER)

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a named command to a display (or the whole chain)."""
        params = params or {}
        self._coerce_child_ids(command, params)

        def level_of(name: str = "level") -> bytes:
            return bytes([max(0, min(100, int(params.get(name, 0))))])

        match command:
            case "power_on":
                await self._command_to(params["display"], CMD_POWER, bytes([1]))
            case "power_off":
                await self._command_to(params["display"], CMD_POWER, bytes([0]))
            case "set_volume":
                await self._set_volume(params["display"], level_of())
            case "mute_on":
                await self._set_mute(params["display"], 1)
            case "mute_off":
                await self._set_mute(params["display"], 0)
            case "set_input":
                code = _lookup(INPUT_MAP, params.get("input", ""), "input source")
                await self._command_to(params["display"], CMD_INPUT, bytes([code]))
            case "set_brightness":
                await self._command_to(params["display"], CMD_BRIGHTNESS, level_of())
            case "set_contrast":
                await self._command_to(params["display"], CMD_CONTRAST, level_of())
            case "set_backlight":
                await self._command_to(params["display"], CMD_BACKLIGHT, level_of())
            case "set_sharpness":
                await self._command_to(params["display"], CMD_SHARPNESS, level_of())
            case "set_picture_mode":
                byte = _lookup(PICTURE_MODE_SET, params.get("mode", ""), "picture mode")
                await self._command_to(
                    params["display"], CMD_PICTURE_MODE, bytes([byte])
                )
            case "set_color_tone":
                byte = _lookup(COLOR_TONE_SET, params.get("tone", ""), "colour tone")
                await self._command_to(
                    params["display"], CMD_COLOR_TONE, bytes([byte])
                )
            case "all_on":
                await self._drive_chain(1)
            case "all_off":
                await self._drive_chain(0)
            case "refresh":
                await self.poll()
            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

        log.debug(f"[{self.device_id}] Sent command: {command} {params}")

    async def _command_to(self, display: Any, cmd: int, data: bytes = b"") -> None:
        """Send a command and hold the caller until the display answers.

        A NAK is raised rather than logged. MDC's command set is nominally
        shared but sparsely implemented — a DM75E refuses colour tone outright
        and takes only nine of the twenty-two documented picture modes — so
        "the display will not do that" is the single most likely outcome of a
        correct-looking command, and swallowing it left the IDE, a macro and a
        panel button all reporting success while nothing moved.
        """
        set_id = int(display)
        ack, _values = await self._request(set_id, cmd, data)
        if ack == NAK:
            what = _COMMAND_NAMES.get(cmd, "command 0x%02X" % cmd)
            detail = " = %d" % data[0] if len(data) == 1 else ""
            msg = f"Display {set_id} does not support {what}{detail}."
            self.set_state(self.LAST_ERROR_PROPERTY, msg)
            # Deliberately NOT recorded as an unsupported command. A refused
            # SET is usually about the value, not the command: a DM75E has one
            # HDMI port and refuses hdmi2, and refuses any mute inside the
            # post-volume window. Retiring the command on that evidence would
            # stop polling something the display answers perfectly well.
            raise ValueError(msg)

        # The ACK echoes the value the display actually applied, and this
        # waiter consumed the frame, so on_data_received will not see it.
        # Applying it here is what makes a panel button light up on the press
        # instead of on the next poll.
        updates = self._parse_values(cmd, _values)
        if updates and self.is_child_registered("display", set_id):
            self.set_child_state_batch("display", set_id, updates)

    async def _set_volume(self, display: Any, level: bytes) -> None:
        """Set volume, then re-read mute, which the display clears as a
        side effect of the level change."""
        set_id = int(display)
        await self._command_to(set_id, CMD_VOLUME, level)
        self._volume_set_at[set_id] = asyncio.get_running_loop().time()
        # Read it rather than assume it: the value is queryable, and a driver
        # that synthesises state from what it sent diverges the moment a
        # remote or another controller acts.
        try:
            ack, values = await self._request(set_id, CMD_MUTE)
        except TimeoutError:
            return
        if ack == ACK and values and self.is_child_registered("display", set_id):
            self.set_child_state("display", set_id, "mute", bool(values[0]))

    async def _set_mute(self, display: Any, value: int) -> None:
        """Mute or unmute, waiting out the display's post-volume refusal."""
        set_id = int(display)
        set_at = self._volume_set_at.get(set_id)
        if set_at is not None:
            elapsed = asyncio.get_running_loop().time() - set_at
            remaining = self.VOLUME_MUTE_GUARD_S - elapsed
            if remaining > 0:
                log.debug(
                    f"[{self.device_id}] Holding mute for display {set_id} "
                    f"{remaining:.1f}s: the display refuses it just after a "
                    f"volume change"
                )
                await asyncio.sleep(remaining)
        await self._command_to(set_id, CMD_MUTE, bytes([value]))

    async def _drive_chain(self, value: int) -> None:
        """Power every display on the chain, reporting whatever refused.

        One display refusing (or missing) must not strand the rest of a video
        wall half-powered, so every Set ID is attempted before anything raises.
        """
        failures: list[str] = []
        for set_id in self.list_children("display"):
            try:
                await self._command_to(set_id, CMD_POWER, bytes([value]))
            except (ValueError, TimeoutError) as exc:
                failures.append(f"display {set_id}: {exc}")
        if failures:
            msg = "; ".join(failures)
            self.set_state(self.LAST_ERROR_PROPERTY, msg)
            raise ValueError(msg)

    async def on_data_received(self, data: bytes) -> None:
        """Parse an MDC response frame and update the answering display.

        The frame parser has stripped the 0xAA header and trailing checksum,
        so ``data`` is the response body::

            [0xFF] [set_id] [len] [ACK/NAK] [r-CMD] [values...]

        The Set ID (data[1]) names which display answered; values are read by
        the echoed command (r-CMD), not the leading 0xFF marker.
        """
        # Need at least: response marker, id, len, ack/nak, r-cmd
        if len(data) < 5 or data[0] != RESPONSE_CMD:
            return

        set_id = data[1]
        ack = data[3]
        rcmd = data[4]
        values = data[5:]

        if ack not in (ACK, NAK):
            return

        # Whoever asked owns the answer. Anything left over is a frame this
        # driver did not ask for: the display re-delivering replies queued
        # before a reconnect, or — while this driver holds the newest socket —
        # another controller's reply arriving here instead of at the
        # controller that asked. Both still describe the display accurately,
        # so they are worth applying; neither may satisfy a wait.
        if self._resolve_waiter(set_id, rcmd, ack, values):
            return

        if ack == NAK:
            log.debug(
                f"[{self.device_id}] Unsolicited NAK from display {set_id} "
                f"for command 0x{rcmd:02x}"
            )
            return
        if not self.is_child_registered("display", set_id):
            return  # a Set ID we don't track (not in the configured roster)

        updates = self._parse_values(rcmd, values)
        if updates:
            self.set_child_state_batch("display", set_id, updates)

    def _parse_values(self, rcmd: int, values: bytes) -> dict[str, Any]:
        """Map a response's echoed command + value bytes to child props."""
        if rcmd == CMD_STATUS and len(values) >= 3:
            # Status values: [power, volume, mute, input, aspect, ...timers].
            updates: dict[str, Any] = {
                "power": "on" if values[0] else "off",
                "volume": values[1],
                "mute": bool(values[2]),
            }
            if len(values) >= 4:
                updates["input"] = INPUT_REVERSE.get(
                    values[3], f"unknown_{values[3]:02x}"
                )
            return updates
        if not values:
            return {}
        if rcmd == CMD_POWER:
            return {"power": "on" if values[0] else "off"}
        if rcmd == CMD_VOLUME:
            return {"volume": values[0]}
        if rcmd == CMD_MUTE:
            return {"mute": bool(values[0])}
        if rcmd == CMD_INPUT:
            return {"input": INPUT_REVERSE.get(values[0], f"unknown_{values[0]:02x}")}
        if rcmd == CMD_CONTRAST:
            return {"contrast": values[0]}
        if rcmd == CMD_BRIGHTNESS:
            return {"brightness": values[0]}
        if rcmd == CMD_BACKLIGHT:
            return {"backlight": values[0]}
        if rcmd == CMD_SHARPNESS:
            return {"sharpness": values[0]}
        if rcmd == CMD_PICTURE_MODE:
            return {"picture_mode": PICTURE_MODE_NAMES.get(values[0], f"mode_{values[0]:02x}")}
        if rcmd == CMD_COLOR_TONE:
            name = COLOR_TONE_NAMES.get(values[0])
            return {"color_tone": name} if name else {}
        return {}

    async def poll(self) -> None:
        """Read every configured display, awaiting each reply.

        Awaiting is what makes this a liveness signal: a fire-and-forget send
        succeeds against a socket the display has stopped serving, so the
        missed-poll watchdog could never fire and the device stayed "connected"
        with frozen values. Raising when the whole chain is silent hands that
        judgement to the platform, which reconnects — the only thing that gets
        service back.

        A single Set ID that does not answer is NOT that: it is a roster entry
        with no panel behind it (someone listed 1,2 for one display), so it is
        marked not_responding on its own child and the poll carries on. The
        link is only condemned when nothing at all answered.
        """
        if not self.transport or not self.transport.connected:
            return

        roster = list(self.list_children("display"))
        if not roster:
            return

        any_reply = False
        silent: list[int] = []
        refused: list[str] = []

        for set_id in roster:
            updates: dict[str, Any] = {}
            answered = False
            for cmd in POLL_COMMANDS:
                if (set_id, cmd) in self._unsupported:
                    continue
                try:
                    ack, values = await self._request(set_id, cmd)
                except TimeoutError:
                    # Stop asking this display for the rest of the cycle;
                    # seven one-second waits per absent Set ID would stall
                    # every other display behind it.
                    break
                except ConnectionError:
                    log.warning(f"[{self.device_id}] Poll failed: not connected")
                    return
                answered = True
                any_reply = True
                if ack == NAK:
                    self._unsupported.add((set_id, cmd))
                    name = _COMMAND_NAMES.get(cmd, f"0x{cmd:02X}")
                    refused.append(f"display {set_id}: {name}")
                    log.info(
                        f"[{self.device_id}] Display {set_id} does not "
                        f"implement {name}; dropping it from the poll"
                    )
                    continue
                updates.update(self._parse_values(cmd, values))

            if answered:
                self.set_child_state_batch(
                    "display", set_id, {**updates, **self.child_fault()}
                )
            else:
                silent.append(set_id)
                self.set_child_state_batch(
                    "display",
                    set_id,
                    self.child_fault(
                        CHILD_NOT_RESPONDING,
                        f"No reply from Set ID {set_id}. Check that a display "
                        f"on the chain is set to this ID.",
                    ),
                )

        if not any_reply:
            raise ConnectionError(
                f"[{self.device_id}] No display answered "
                f"({len(roster)} Set ID(s) polled)"
            )

        if silent:
            self.set_state(
                self.LAST_ERROR_PROPERTY,
                f"No reply from Set ID(s) {', '.join(str(i) for i in silent)}.",
            )
        elif refused:
            self.set_state(
                self.LAST_ERROR_PROPERTY,
                f"Not supported by this model: {'; '.join(refused)}.",
            )
