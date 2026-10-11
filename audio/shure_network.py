"""
OpenAVC Shure Networked Audio Driver.

Controls Shure networked microphones, interfaces, and processors over the
Shure command strings protocol on TCP port 2202. Covers the MXA910 / MXA920 /
MXA925 / MXA710 ceiling and linear arrays, the MXA320 table array, the MXA902
ceiling array with loudspeaker, the ANI networked audio interfaces, the P300
IntelliMix processor, and the SCM820 automixer.

Protocol summary (all models share one grammar):
    Command : ``< [GET|SET] [index] PROPERTY [value] >``
    Reply   : ``< REP [index] PROPERTY value >``  (``< REP ERR >`` on error)
    - The angle brackets frame every message; ``>`` is the delimiter. Spaces
      separate fields. ``index`` is the channel; index 0 addresses all
      channels and, on a GET, fans one query out into one REP per channel.
    - String values (device / channel names, firmware) are brace-wrapped and
      space-padded to a fixed width (``{Podium Mic}``); the parser strips both.
    - The device sends a REP whenever a value changes, from any controller,
      its own web application or Designer (the MXA910 page says so for every
      parameter), so control is push-driven: the driver reads everything on
      connect and then mirrors the reports.

Per-channel state is modelled with child entities, numbered as the device
numbers them. Every model exposes the same per-channel surface through the
shared grammar (mute, digital gain, read-only name, metering), though the
channels mean different things per model. Where the model fixes its channel
numbers (the command-strings pages' channel number assignments), the model
the device reports sets the roster: MXA910 / MXA920 / MXA925 channels 1-8 and
the automix output 9; MXA320 microphones 1-4 and the automix output 5; MXA902
the automix output 9, the loudspeaker output 10 and its Dante inputs 11-12;
ANI4IN / ANI4OUT 1-4. The P300 (gapped channel map), SCM820 (no MODEL), ANI22
and MXA710 keep a configured count. The reported model is saved into the
Model setting (``learned_from``), which also lets a project built or
simulated before the device exists show that model's channels and controls.
The commands and device settings on the device page are narrowed to the ones
that model's page documents.

Automatic coverage (MXA920, MXA925; on from the factory): while it is on,
channels 1-8 are coverage areas and take ``CA_MUTE`` / ``CA_GAIN`` (the
``AUDIO_MUTE`` / ``AUDIO_GAIN_HI_RES`` pair accepts only the automix output
09), channel names are reported for 09 only, the plain ``SAMPLE`` meter
carries the automix output alone, and the areas meter through
``CA_METER_RATE`` / ``SAMPLE_CA``. While it is off, 1-8 are steerable lobes
on the ordinary channel properties. The driver reads ``AUTO_COVERAGE`` on
connect, follows its reports, and addresses channels 1-8 the way the current
mode needs.

Python (not YAML) because several things the YAML runtime can't express carry
their weight here: one ``SAMPLE`` frame fans a row of levels out into N
per-channel meters, and a talker-position frame carries several positions of
six numbers each; channels 1-8 change property names with a device setting
(automatic coverage); and the roster and the offered commands follow the
model the device reports.

Source: https://www.shure.com/en-US/docs/commandstrings/ (per-model command
strings pages: MXA925, MXA920, MXA910, MXA320, MXA902, P300, SCM820, ANI4IN).
"""

from __future__ import annotations

import asyncio
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)


DEFAULT_CHANNEL_COUNT = 8

# The models the Model setting lists, as the device reports them (prefix
# match: an MXA920 may report MXA920-S, an ANI22 ANI22-XLR).
_MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("MXA910", "MXA910 ceiling array"),
    ("MXA920", "MXA920 ceiling array"),
    ("MXA925", "MXA925 ceiling array"),
    ("MXA710", "MXA710 linear array"),
    ("MXA320", "MXA320 table array"),
    ("MXA902", "MXA902 ceiling array with loudspeaker"),
    ("ANI4IN", "ANI4IN audio network interface"),
    ("ANI4OUT", "ANI4OUT audio network interface"),
    ("ANI22", "ANI22 audio network interface"),
    ("P300", "P300 IntelliMix audio conferencing processor"),
    ("SCM820", "SCM820 automatic mixer"),
)


def _lobe_channels(word: str) -> dict[int, str]:
    channels = {n: f"{word} {n}" for n in range(1, 9)}
    channels[9] = "Automix Output"
    return channels


# Channel numbers and their names, per the model's channel number
# assignments. The AEC reference input (MXA910 / 920 / 925: 10, MXA320: 6) is
# not a channel a room controller mutes or levels, so it is not modelled.
_CHANNEL_MAPS: dict[str, dict[int, str]] = {
    "MXA910": _lobe_channels("Channel"),
    "MXA920": _lobe_channels("Lobe"),
    "MXA925": _lobe_channels("Lobe"),
    "MXA320": {1: "Microphone 1", 2: "Microphone 2", 3: "Microphone 3",
               4: "Microphone 4", 5: "Automix Output"},
    "MXA902": {9: "Automix Output", 10: "Loudspeaker Output",
               11: "Dante Input 1", 12: "Dante Input 2"},
    "ANI4IN": {n: f"Channel {n}" for n in range(1, 5)},
    "ANI4OUT": {n: f"Channel {n}" for n in range(1, 5)},
}

# Models with automatic coverage (channels 1-8 are coverage areas while on).
_COVERAGE_MODELS = frozenset({"MXA920", "MXA925"})
_COVERAGE_AREAS = range(1, 9)

# The automix output channel, which the automixer settings are indexed by.
_AUTOMIX_CHANNEL = {"MXA910": 9, "MXA920": 9, "MXA925": 9, "MXA320": 5,
                    "MXA902": 9}

# Which models' pages document each model-specific property.
_MXA_ALL = frozenset({"MXA910", "MXA920", "MXA925", "MXA320", "MXA902"})
_GATE_MODELS = frozenset({"MXA910", "MXA920", "MXA925", "MXA320"})
_TALKER_MODELS = _COVERAGE_MODELS
_DEVICE_STATE_MODELS = frozenset({"MXA925", "MXA320"})


def _family_of(model: str) -> str:
    """The listed model a reported (or configured) model belongs to, or ""."""
    upper = (model or "").strip().upper()
    for value, _label in _MODEL_CHOICES:
        if upper.startswith(value):
            return value
    return ""


def _channel_count_for_model(model: str) -> int | None:
    """The channel count the reported MODEL implies, or None to keep the
    configured one."""
    channels = _CHANNEL_MAPS.get(_family_of(model))
    return len(channels) if channels else None


# Digital gain (AUDIO_GAIN_HI_RES, CA_GAIN) is carried on the wire as an
# integer 0-1400 that maps linearly to -110.0 .. +30.0 dB in 0.1 dB steps
# (1100 = 0 dB).
_GAIN_WIRE_MIN = 0
_GAIN_WIRE_MAX = 1400
_GAIN_DB_MIN = -110.0
_GAIN_DB_MAX = 30.0

# LED brightness: 0 = disabled, then 1-5 = 20/40/60/80/100% on every MXA
# page and the MXA910 from v3.0. Older MXA910 / P300 / ANI firmware only
# honor 0-2; those devices answer a benign ``REP ERR`` to 3-5, so the widest
# documented range is the right bound.
_LED_BRIGHTNESS_MAX = 5

# A meter SAMPLE value is 000-060, representing -60 .. 0 dBFS.
_METER_DBFS_OFFSET = 60

# How long a connect read waits for the model or the coverage mode before
# going on with what it already assumes.
_REPLY_WAIT_S = 2.0

_LED_COLORS = [
    "RED", "ORANGE", "GOLD", "YELLOW", "YELLOWGREEN", "GREEN", "TURQUOISE",
    "POWDERBLUE", "CYAN", "SKYBLUE", "BLUE", "PURPLE", "LIGHTPURPLE",
    "VIOLET", "ORCHID", "PINK", "WHITE",
]
_LED_STATES = ["ON", "OFF", "FLASHING"]
_POLAR_PATTERNS = ["TOROID", "OMNI", "CARDIOID", "SUPER", "HYPER", "BIDIRECTION"]
_DEVICE_STATES = {
    "0": "no_error",
    "1": "clearable_error",
    "2": "non_clearable_error",
    "3": "fatal_error",
}
_RJ45_MODES = ["switched", "split", "bimodal", "redundant"]
_MUTE_CONTROL_FUNCS = ["logic", "disabled", "local"]

# Speech gating: four levels on the MXA920 / MXA925, on or off on the MXA320
# and MXA902. The device's own words, per page.
_GATING_WORDS = {
    "MXA920": {"off": "Off", "low": "Low", "medium": "Medium", "high": "High"},
    "MXA925": {"off": "Off", "low": "Low", "medium": "Medium", "high": "High"},
    "MXA320": {"on": "ON", "off": "OFF"},
    "MXA902": {"on": "ON", "off": "OFF"},
}
_GATING_LABELS = {"off": "Off", "low": "Low", "medium": "Medium",
                  "high": "High", "on": "On"}

# Device settings: wire property, channel index ("automix" = the model's
# automix output, an int = that channel, None = not indexed), how the value
# goes on the wire, and the models whose pages document it (None = every
# model).
_SETTING_WIRE: dict[str, tuple[str, Any, str, frozenset | None]] = {
    "led_brightness": ("LED_BRIGHTNESS", None, "int", None),
    "led_state_muted": ("LED_STATE_MUTED", None, "upper", _MXA_ALL),
    "led_state_unmuted": ("LED_STATE_UNMUTED", None, "upper", _MXA_ALL),
    "led_color_muted": ("LED_COLOR_MUTED", None, "upper", _MXA_ALL),
    "led_color_unmuted": ("LED_COLOR_UNMUTED", None, "upper", _MXA_ALL),
    "auto_coverage": ("AUTO_COVERAGE", None, "title_onoff", _COVERAGE_MODELS),
    "speech_gating": ("SPEECH_GATING", "automix", "gating",
                      frozenset(_GATING_WORDS)),
    "noise_filter": ("NOISE_FILTER", "automix", "onoff",
                     frozenset({"MXA920", "MXA902"})),
    "vab": ("VAB", None, "int", _COVERAGE_MODELS),
    # The MXA925 page: "MXA920: 6.6 and newer" (the MXA920 page itself does
    # not list it yet). Older firmware answers REP ERR.
    "loudspeaker_null": ("LOUDSPEAKER_NULL", None, "onoff", _COVERAGE_MODELS),
    "array_height": ("ARRAY_HEIGHT", None, "int", _COVERAGE_MODELS),
    "eq_contour": ("EQ_CONTOUR", None, "onoff", _MXA_ALL),
    "bypass_all_eq": ("BYPASS_ALL_EQ", None, "onoff",
                      frozenset({"MXA910", "MXA920", "MXA925", "MXA320"})),
    "bypass_imx": ("BYPASS_IMX", None, "onoff",
                   frozenset({"MXA910", "MXA920", "MXA925", "MXA320"})),
    "mute_control_func": ("MUTE_CONTROL_FUNC", None, "upper",
                          frozenset({"MXA320"})),
    "autofocus": ("AUTOFOCUS", None, "onoff", frozenset({"MXA920", "MXA902"})),
    "speaker_delay_ms": ("DELAY", 10, "delay", frozenset({"MXA902"})),
    "compressor_automix": ("COMPRESSOR", 9, "onoff", frozenset({"MXA902"})),
    "compressor_speaker": ("COMPRESSOR", 10, "onoff", frozenset({"MXA902"})),
}

# Commands only some models' pages document. Every other command is offered
# on every model.
_COMMAND_MODELS: dict[str, frozenset] = {
    "speaker_on": frozenset({"MXA902"}),
    "speaker_off": frozenset({"MXA902"}),
    "set_polar_pattern": frozenset({"MXA320"}),
    "set_lobe_angle": frozenset({"MXA320"}),
}

# Unindexed reports read straight into state.
_DEVICE_FLAGS = {
    "ENCRYPTION": "encryption",
    "LOUDSPEAKER_NULL": "loudspeaker_null",
    "AUTOFOCUS": "autofocus",
    "EQ_CONTOUR": "eq_contour",
    "BYPASS_ALL_EQ": "bypass_all_eq",
    "BYPASS_IMX": "bypass_imx",
    "SPEAKER": "speaker",
}
_DEVICE_INTS = {
    "LED_BRIGHTNESS": "led_brightness",
    "VAB": "vab",
    "ARRAY_HEIGHT": "array_height",
    "PRESET": "preset",
}
_DEVICE_WORDS = {
    "LED_STATE_MUTED": ("led_state_muted", [s.lower() for s in _LED_STATES]),
    "LED_STATE_UNMUTED": ("led_state_unmuted", [s.lower() for s in _LED_STATES]),
    "LED_COLOR_MUTED": ("led_color_muted", [c.lower() for c in _LED_COLORS]),
    "LED_COLOR_UNMUTED": ("led_color_unmuted", [c.lower() for c in _LED_COLORS]),
    "MUTE_CONTROL_FUNC": ("mute_control_func", _MUTE_CONTROL_FUNCS),
    "RJ45_MODE": ("rj45_mode", _RJ45_MODES),
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _db_to_wire(db: float) -> int:
    """dB (-110..+30) -> wire units (0..1400), clamped."""
    wire = round((_clamp(db, _GAIN_DB_MIN, _GAIN_DB_MAX) + 110.0) * 10.0)
    return int(_clamp(wire, _GAIN_WIRE_MIN, _GAIN_WIRE_MAX))


def _wire_to_db(wire: int) -> float:
    """Wire units (0..1400) -> dB (-110..+30), rounded to 0.1 dB."""
    return round(wire / 10.0 - 110.0, 1)


def _strip_string_value(raw: str) -> str:
    """Unwrap a brace-wrapped, space-padded string value.

    Names arrive as ``{Podium Mic}`` padded to a fixed width; firmware as
    ``{4.6.11}``. Take the brace contents when present, else the raw token,
    and trim the padding.
    """
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        return raw[start + 1:end].strip()
    return raw


def _on(word: str) -> bool | None:
    """ON / OFF in any case (CA_MUTE and AUTO_COVERAGE answer On / Off)."""
    upper = word.strip().upper()
    if upper == "ON":
        return True
    if upper == "OFF":
        return False
    return None


def _int(word: str) -> int | None:
    try:
        return int(word.strip())
    except ValueError:
        return None


class ShureNetworkDriver(BaseDriver):
    """Shure networked audio (MXA / ANI / P300 / SCM820) driver."""

    DRIVER_INFO = {
        "id": "shure_network",
        "name": "Shure Networked Devices",
        "manufacturer": "Shure",
        "category": "audio",
        "version": "2.2.0",
        # learned_from on Channel Count and Model landed in 0.37.0.
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": (
            "Controls Shure networked audio devices over the command strings "
            "protocol on TCP port 2202: MXA910 / MXA920 / MXA925 / MXA710 "
            "ceiling and linear arrays, the MXA320 table array, the MXA902 "
            "ceiling array with loudspeaker, ANI interfaces, the P300 "
            "IntelliMix processor and the SCM820 automixer. Each audio "
            "channel or coverage area is a child entity with mute, gain, "
            "name, gate status and live metering; device mute, presets, the "
            "status LED, automatic coverage, talker positions and the "
            "MXA902's loudspeaker are controlled directly. State pushes live "
            "as it changes on the device or another controller."
        ),
        "source_url": "https://www.shure.com/en-US/docs/commandstrings",
        "tags": [
            "ceiling-mic", "microphone", "automixer", "intellimix", "dsp",
            "camera-tracking",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["shure_dcs"],
        "ports": [2202],
        "transport": "tcp",
        "discovery": {
            # Shure Web Device Discovery / Designer enumerate devices over
            # standard mDNS (`_http._tcp`) plus hostname/TXT filters — there
            # is no registered `_shure._tcp` service type. We do an active
            # query on TCP/2202 plus OUI / hostname / manufacturer-alias
            # hints. MODEL answers on the MXA, ANI and P300 documents. The
            # wireless systems (Axient Digital, ULX-D, SLX-D, QLX-D, MXW neXt)
            # answer on the same port with the same grammar and have their
            # own drivers, so this probe declines their model names: a
            # receiver or access point must never be offered the
            # conferencing driver. A bare `< REP ERR >` is not matched: the
            # SCM820 answers MODEL that way, but so does every Shure device
            # whose document has no MODEL (QLX-D, MXW neXt), so the reply
            # identifies nothing. An SCM820 is found by its OUI and port.
            #   Common IP ports: content-files.shure.com/FileRepository/common-ip-ports-v2.pdf
            "tcp_probe": {
                "port": 2202,
                "send_ascii": "< GET MODEL >\r\n",
                "expect_regex": (
                    "<\\s*REP\\s+MODEL\\s+\\{\\s*(?!AD4|AD6|ULXD|SLXD|QLXD|AXT|P10T|MXW)"
                ),
                "extract_manufacturer": "Shure",
                "extract": {
                    "model": {"regex": "<\\s*REP\\s+MODEL\\s+\\{\\s*([^{}>\\s]+)"},
                },
            },
            "oui": [
                "00:0e:dd",   # Shure Incorporated (legacy MA-L)
                "d8:34:ee",   # Shure Incorporated (current MXA / AD blocks)
            ],
            "hostname": ["^MXA", "^ANI", "^P300", "^IMX"],
            "port_open": [2202],
            "manufacturer_alias": ["shure", "shure incorporated"],
        },
        "compatible_models": [
            {
                "manufacturer": "Shure",
                "models": [
                    "MXA910", "MXA920", "MXA925", "MXA710", "MXA320",
                    "MXA902", "ANI22", "ANI4IN", "ANI4OUT", "P300 IntelliMix",
                    "SCM820",
                ],
                "confidence": "untested",
                "notes": (
                    "All speak the same command strings grammar. The MXA910, "
                    "MXA920, MXA925, MXA320, MXA902 and ANI4IN / ANI4OUT "
                    "report their model, which sets their channels: 1-8 plus "
                    "the automix output 9 on the ceiling arrays (coverage "
                    "areas while automatic coverage is on, lobes while it is "
                    "off), microphones 1-4 plus the automix output 5 on the "
                    "MXA320, and the automix output 9, loudspeaker output 10 "
                    "and Dante inputs 11-12 on the MXA902. Set Channel Count "
                    "yourself for the others: the SCM820 has 8 inputs plus "
                    "mixes, and the P300 has a large input/output matrix. "
                    "Device mute and presets are MXA / P300 / ANI; the "
                    "SCM820 mutes per channel and has no presets. Properties "
                    "a model lacks answer a benign REP ERR."
                ),
            },
        ],
        "help": {
            "overview": (
                "Shure networked microphones, interfaces, and processors "
                "speak the command strings protocol on TCP port 2202. This "
                "driver models each audio channel as a child entity carrying "
                "its mute, gain (in dB), name, gate status and live level, "
                "and controls device mute, presets, the status LED and the "
                "model's own settings directly.\n\nState is push-driven: "
                "after connecting, the driver reads every channel from the "
                "device, then mirrors the reports the device sends whenever "
                "anything changes, including changes made in Shure Designer "
                "or the device's web application.\n\nOn an MXA920 or MXA925 "
                "with automatic coverage on (the factory setting), channels "
                "1-8 are the coverage areas; with it off they are the "
                "steerable lobes. Channel 9 is the automix output either "
                "way.\n\nChannel names are read-only over third-party "
                "control. Set them in Shure Designer, and they appear here "
                "as the channel labels."
            ),
            "setup": (
                "1. The device's command strings interface on TCP 2202 is on "
                "by default (Designer or the web application: Settings > "
                "Services).\n2. Give the device a static IP or a DHCP "
                "reservation. Use the Shure Control address, not the audio "
                "network one.\n3. In OpenAVC, enter the IP. Port 2202 is "
                "fixed.\n4. Leave Model on Detect automatically: the device "
                "reports its model when it connects, which sets its channels "
                "and the controls offered here. Pick it yourself to build or "
                "simulate the project before the device is on the "
                "network.\n5. For a P300, SCM820, ANI22 or MXA710, set "
                "Channel Count to match the device (8 for an SCM820).\n6. To "
                "see live level meters, set Meter Interval to a value like "
                "500 ms (0 disables metering). For camera tracking on an "
                "MXA920 or MXA925, set Talker Position Interval."
            ),
        },
        "default_config": {
            "host": "",
            "port": 2202,
            "model": "",
            "channel_count": 8,
            "poll_interval": 10,
            "meter_interval_ms": 0,
            "talker_position_interval_ms": 0,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "default": 2202,
                "label": "Port",
                "description": "Shure command strings port — always 2202.",
            },
            "model": {
                "type": "enum",
                "default": "",
                "label": "Model",
                "learned_from": "model_family",
                "values": [
                    {"value": "", "label": "Detect automatically"},
                    {"value": "MXA910", "label": "MXA910 ceiling array"},
                    {"value": "MXA920", "label": "MXA920 ceiling array"},
                    {"value": "MXA925", "label": "MXA925 ceiling array"},
                    {"value": "MXA710", "label": "MXA710 linear array"},
                    {"value": "MXA320", "label": "MXA320 table array"},
                    {
                        "value": "MXA902",
                        "label": "MXA902 ceiling array with loudspeaker",
                    },
                    {
                        "value": "ANI4IN",
                        "label": "ANI4IN audio network interface",
                    },
                    {
                        "value": "ANI4OUT",
                        "label": "ANI4OUT audio network interface",
                    },
                    {
                        "value": "ANI22",
                        "label": "ANI22 audio network interface",
                    },
                    {
                        "value": "P300",
                        "label": "P300 IntelliMix audio conferencing processor",
                    },
                    {"value": "SCM820", "label": "SCM820 automatic mixer"},
                ],
                "description": (
                    "Sets the channels and the controls offered. The device "
                    "reports its model when it connects, and that is saved "
                    "here; pick it yourself to build or simulate the project "
                    "first."
                ),
            },
            "channel_count": {
                "type": "integer",
                "default": 8,
                "min": 0,
                "label": "Channel Count",
                "learned_from": "channel_count_reported",
                "description": (
                    "Number of audio channels to model, for a model whose "
                    "channels the driver does not know (P300, SCM820: 8, "
                    "ANI22, MXA710). The MXA arrays and the ANI4IN / ANI4OUT "
                    "set their own channels."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 10,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often to refresh device-level state. Channel state "
                    "is push-driven, so this can be gentle."
                ),
            },
            "meter_interval_ms": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "max": 99999,
                "label": "Meter Interval (ms)",
                "description": (
                    "Level-meter update rate in milliseconds (0 = off, valid "
                    "range 100-99999). Metering streams continuously, so "
                    "leave it off unless a panel shows live levels."
                ),
            },
            "talker_position_interval_ms": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "max": 99999,
                "label": "Talker Position Interval (ms)",
                "description": (
                    "MXA920 / MXA925: how often the microphone reports where "
                    "the active talkers are, for camera tracking (100-99999 "
                    "ms). The microphone keeps this setting. 0 leaves the "
                    "microphone's own setting alone."
                ),
            },
        },
        "state_variables": {
            "device_name": {
                "type": "string",
                "label": "Device Name",
                "help": "The device's friendly name (the DEVICE_ID property).",
            },
            "mute": {
                "type": "boolean",
                "label": "Device Mute",
                "help": (
                    "Device mute (DEVICE_AUDIO_MUTE). MXA / P300 / ANI; the "
                    "SCM820 mutes per channel instead. On an MXA902 it mutes "
                    "the microphone only, never the loudspeaker."
                ),
                "cloud_priority": "high",
                "control": True,
            },
            "audio_mute": {
                "type": "boolean",
                "label": "Audio Mute",
                "help": "Mirror of Device Mute, for templates that bind audio_mute.",
            },
            "led_brightness": {
                "type": "integer",
                "label": "LED Brightness",
                "min": 0,
                "max": 5,
                "help": (
                    "Status LED brightness: 0 = off; 1-5 = 20/40/60/80/100%. "
                    "Older MXA910 / P300 / ANI firmware only honors 0-2."
                ),
            },
            "led_state_muted": {
                "type": "enum",
                "label": "LED When Muted",
                "values": ["on", "off", "flashing"],
            },
            "led_state_unmuted": {
                "type": "enum",
                "label": "LED When Unmuted",
                "values": ["on", "off", "flashing"],
            },
            "led_color_muted": {
                "type": "enum",
                "label": "LED Color When Muted",
                "values": [
                    "red", "orange", "gold", "yellow", "yellowgreen", "green",
                    "turquoise", "powderblue", "cyan", "skyblue", "blue",
                    "purple", "lightpurple", "violet", "orchid", "pink",
                    "white",
                ],
            },
            "led_color_unmuted": {
                "type": "enum",
                "label": "LED Color When Unmuted",
                "values": [
                    "red", "orange", "gold", "yellow", "yellowgreen", "green",
                    "turquoise", "powderblue", "cyan", "skyblue", "blue",
                    "purple", "lightpurple", "violet", "orchid", "pink",
                    "white",
                ],
            },
            "firmware": {
                "type": "string",
                "label": "Firmware Version",
                "help": "Firmware version reported by the device (FW_VER).",
            },
            "serial_number": {"type": "string", "label": "Serial Number"},
            "model": {
                "type": "string",
                "label": "Model",
                "help": (
                    "Model reported by the device (MODEL). The SCM820 does "
                    "not report one."
                ),
            },
            "model_family": {
                "type": "string",
                "label": "Listed Model",
                "help": (
                    "The Model setting's entry the reported model belongs "
                    "to; blank for a model the setting does not list."
                ),
            },
            "channel_count_reported": {
                "type": "integer",
                "label": "Channels",
                "help": (
                    "Channel count the reported model implies; blank for a "
                    "model that does not fix one."
                ),
            },
            "encryption": {"type": "boolean", "label": "Audio Encryption"},
            "device_state": {
                "type": "enum",
                "label": "Device Error State",
                "values": [
                    "no_error", "clearable_error", "non_clearable_error",
                    "fatal_error",
                ],
                "help": (
                    "MXA925 / MXA320: no_error, a clearable error, a "
                    "non-clearable error, or a non-clearable fatal error."
                ),
                "cloud_priority": "high",
            },
            "rj45_mode": {
                "type": "enum",
                "label": "Network Port Mode",
                "values": ["switched", "split", "bimodal", "redundant"],
                "help": "MXA925: how its two network ports are configured.",
            },
            "preset": {
                "type": "integer",
                "label": "Preset",
                "help": "The preset the device reports as loaded (1-10).",
            },
            "auto_coverage": {
                "type": "boolean",
                "label": "Automatic Coverage",
                "help": (
                    "MXA920 / MXA925. On: channels 1-8 are coverage areas. "
                    "Off: they are steerable lobes."
                ),
            },
            "speech_gating": {
                "type": "enum",
                "label": "Speech Gating Threshold",
                "values": ["off", "low", "medium", "high", "on"],
            },
            "noise_filter": {"type": "boolean", "label": "Enhanced Noise Filtering"},
            "vab": {
                "type": "integer",
                "label": "Virtual Acoustic Boundary",
                "min": 0,
                "max": 20,
                "help": "0 = off. Works while automatic coverage is on.",
            },
            "loudspeaker_null": {"type": "boolean", "label": "Advanced Loudspeaker Nulling"},
            "array_height": {
                "type": "integer",
                "label": "Array Height",
                "min": 122,
                "max": 914,
                "unit": "cm",
            },
            "eq_contour": {"type": "boolean", "label": "EQ Contour"},
            "bypass_all_eq": {"type": "boolean", "label": "Bypass All EQ"},
            "bypass_imx": {"type": "boolean", "label": "Bypass IntelliMix"},
            "mute_control_func": {
                "type": "enum",
                "label": "Mute Control",
                "values": ["logic", "disabled", "local"],
            },
            "autofocus": {"type": "boolean", "label": "Autofocus"},
            "speaker": {
                "type": "boolean",
                "label": "Loudspeaker",
                "help": "MXA902: the built-in loudspeaker is on.",
                "cloud_priority": "high",
                "control": True,
            },
            "speaker_delay_ms": {
                "type": "integer",
                "label": "Loudspeaker Delay",
                "min": 0,
                "max": 160,
                "unit": "ms",
                "help": "MXA902: 0 = off.",
            },
            "compressor_automix": {"type": "boolean", "label": "Automix Compressor"},
            "compressor_speaker": {"type": "boolean", "label": "Loudspeaker Compressor"},
            "talker_x": {
                "type": "integer",
                "label": "Talker X",
                "min": -1524,
                "max": 1524,
                "unit": "cm",
                "help": (
                    "Where the latest talker-position report put the first "
                    "talker, from the microphone's center: X along the LED "
                    "bar on a square array (45 degrees to it on a round "
                    "one)."
                ),
                "cloud_priority": "low",
            },
            "talker_y": {
                "type": "integer",
                "label": "Talker Y",
                "min": -1524,
                "max": 1524,
                "unit": "cm",
                "cloud_priority": "low",
            },
            "talker_z": {
                "type": "integer",
                "label": "Talker Z",
                "min": 0,
                "max": 914,
                "unit": "cm",
                "help": "Distance down from the microphone.",
                "cloud_priority": "low",
            },
            "talker_lobe": {
                "type": "integer",
                "label": "Talker Lobe",
                "cloud_priority": "low",
            },
            "talker_coverage_area": {
                "type": "integer",
                "label": "Talker Coverage Area",
                "help": "0 while automatic coverage is off.",
                "cloud_priority": "low",
            },
            "talker_count": {
                "type": "integer",
                "label": "Talkers Reported",
                "help": "How many talker positions the latest report carried.",
                "cloud_priority": "low",
            },
        },
        # One `channel` type covers every model's per-channel surface. Mute
        # relays at high cloud priority (a hot-mic'd or muted podium is
        # operationally significant); gain, name, gate, clip and the
        # continuous meter are low. Local ids are the device's own channel
        # numbers (channel 0 is the "all channels" broadcast/query index, not
        # an addressable entity).
        "child_entity_types": {
            "channel": {
                "label": "Channel",
                "label_plural": "Channels",
                "id_format": {"type": "integer", "min": 1, "pad_width": 2},
                "state_variables": {
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "cloud_priority": "high",
                        "control": True,
                    },
                    "gain_db": {
                        "type": "number",
                        "label": "Gain",
                        "min": -110.0,
                        "max": 30.0,
                        "step": 0.1,
                        "unit": "dB",
                        "cloud_priority": "low",
                        "control": True,
                    },
                    # CHAN_NAME is GET/REP only: names are set in Shure
                    # Designer, not over third-party control.
                    "name": {
                        "type": "string",
                        "label": "Name",
                        "cloud_priority": "low",
                    },
                    # Live level from the SAMPLE stream; only populated
                    # while metering is enabled (meter_interval_ms > 0).
                    "meter": {
                        "type": "number",
                        "label": "Level",
                        "min": -60,
                        "max": 0,
                        "unit": "dBFS",
                        "cloud_priority": "low",
                    },
                    "gate": {
                        "type": "boolean",
                        "label": "Gate Open",
                        "help": (
                            "The automixer has this channel's gate open: "
                            "someone is talking in this coverage area or "
                            "lobe. Reported on every change, for camera "
                            "switching."
                        ),
                        "cloud_priority": "low",
                    },
                    "clip": {
                        "type": "boolean",
                        "label": "Clipping",
                        "cloud_priority": "low",
                    },
                    "polar_pattern": {
                        "type": "enum",
                        "label": "Polar Pattern",
                        "values": [
                            "toroid", "omni", "cardioid", "super", "hyper",
                            "bidirection",
                        ],
                        "help": "MXA320 microphones 1-4.",
                        "cloud_priority": "low",
                    },
                    "lobe_angle": {
                        "type": "integer",
                        "label": "Lobe Angle",
                        "min": 0,
                        "max": 345,
                        "unit": "°",
                        "help": "MXA320 microphones 1-4, in 15-degree steps.",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["mute", "gain_db"],
                "label_field": "name",
            },
        },
        "device_settings": {
            "led_brightness": {
                "type": "integer",
                "label": "LED Brightness",
                "help": (
                    "Status LED brightness: 0 = off, 1-5 = dim..full (older "
                    "firmware caps at 2)."
                ),
                "state_key": "led_brightness",
                "default": 2,
                "min": 0,
                "max": 5,
                "setup": False,
            },
            "led_state_muted": {
                "type": "enum",
                "label": "LED When Muted",
                "values": [
                    {"value": "on", "label": "On"},
                    {"value": "off", "label": "Off"},
                    {"value": "flashing", "label": "Flashing"},
                ],
                "state_key": "led_state_muted",
                "default": "on",
                "setup": False,
            },
            "led_state_unmuted": {
                "type": "enum",
                "label": "LED When Unmuted",
                "values": [
                    {"value": "on", "label": "On"},
                    {"value": "off", "label": "Off"},
                    {"value": "flashing", "label": "Flashing"},
                ],
                "state_key": "led_state_unmuted",
                "default": "on",
                "setup": False,
            },
            "led_color_muted": {
                "type": "enum",
                "label": "LED Color When Muted",
                "values": [
                    {"value": "red", "label": "Red"},
                    {"value": "orange", "label": "Orange"},
                    {"value": "gold", "label": "Gold"},
                    {"value": "yellow", "label": "Yellow"},
                    {"value": "yellowgreen", "label": "Yellowgreen"},
                    {"value": "green", "label": "Green"},
                    {"value": "turquoise", "label": "Turquoise"},
                    {"value": "powderblue", "label": "Powderblue"},
                    {"value": "cyan", "label": "Cyan"},
                    {"value": "skyblue", "label": "Skyblue"},
                    {"value": "blue", "label": "Blue"},
                    {"value": "purple", "label": "Purple"},
                    {"value": "lightpurple", "label": "Lightpurple"},
                    {"value": "violet", "label": "Violet"},
                    {"value": "orchid", "label": "Orchid"},
                    {"value": "pink", "label": "Pink"},
                    {"value": "white", "label": "White"},
                ],
                "state_key": "led_color_muted",
                "default": "red",
                "setup": False,
            },
            "led_color_unmuted": {
                "type": "enum",
                "label": "LED Color When Unmuted",
                "values": [
                    {"value": "red", "label": "Red"},
                    {"value": "orange", "label": "Orange"},
                    {"value": "gold", "label": "Gold"},
                    {"value": "yellow", "label": "Yellow"},
                    {"value": "yellowgreen", "label": "Yellowgreen"},
                    {"value": "green", "label": "Green"},
                    {"value": "turquoise", "label": "Turquoise"},
                    {"value": "powderblue", "label": "Powderblue"},
                    {"value": "cyan", "label": "Cyan"},
                    {"value": "skyblue", "label": "Skyblue"},
                    {"value": "blue", "label": "Blue"},
                    {"value": "purple", "label": "Purple"},
                    {"value": "lightpurple", "label": "Lightpurple"},
                    {"value": "violet", "label": "Violet"},
                    {"value": "orchid", "label": "Orchid"},
                    {"value": "pink", "label": "Pink"},
                    {"value": "white", "label": "White"},
                ],
                "state_key": "led_color_unmuted",
                "default": "green",
                "setup": False,
            },
            "auto_coverage": {
                "type": "boolean",
                "label": "Automatic Coverage",
                "help": (
                    "On: channels 1-8 are coverage areas, drawn in Designer. "
                    "Off: they are steerable lobes, each with its own Dante "
                    "output."
                ),
                "default": True,
                "setup": False,
                "state_key": "auto_coverage",
            },
            "speech_gating": {
                "type": "enum",
                "label": "Speech Gating Threshold",
                "help": "How readily the automixer opens a channel for speech.",
                "values": [
                    {"value": "off", "label": "Off"},
                    {"value": "low", "label": "Low"},
                    {"value": "medium", "label": "Medium"},
                    {"value": "high", "label": "High"},
                    {"value": "on", "label": "On"},
                ],
                "state_key": "speech_gating",
                "default": "low",
                "setup": False,
            },
            "noise_filter": {
                "type": "boolean",
                "label": "Enhanced Noise Filtering",
                "help": "Filters non-speech noise in the automixer.",
                "default": False,
                "setup": False,
                "state_key": "noise_filter",
            },
            "vab": {
                "type": "integer",
                "label": "Virtual Acoustic Boundary",
                "help": (
                    "Strength from 0 (off) to 20. Works while automatic "
                    "coverage is on."
                ),
                "state_key": "vab",
                "default": 0,
                "min": 0,
                "max": 20,
                "setup": False,
            },
            "loudspeaker_null": {
                "type": "boolean",
                "label": "Advanced Loudspeaker Nulling",
                "help": (
                    "Rejects audio from the room's loudspeakers. Works while "
                    "automatic coverage is on; MXA920 from firmware 6.6."
                ),
                "default": False,
                "setup": False,
                "state_key": "loudspeaker_null",
            },
            "array_height": {
                "type": "integer",
                "label": "Array Height (cm)",
                "help": (
                    "The microphone's height above the floor, 122-914 cm "
                    "(4-30 ft)."
                ),
                "state_key": "array_height",
                "default": 274,
                "min": 122,
                "max": 914,
                "setup": False,
            },
            "eq_contour": {
                "type": "boolean",
                "label": "EQ Contour",
                "help": "The voice EQ contour.",
                "default": False,
                "setup": False,
                "state_key": "eq_contour",
            },
            "bypass_all_eq": {
                "type": "boolean",
                "label": "Bypass All EQ",
                "help": (
                    "Bypasses every EQ filter. On an MXA920 / MXA925, works "
                    "while automatic coverage is off."
                ),
                "default": False,
                "setup": False,
                "state_key": "bypass_all_eq",
            },
            "bypass_imx": {
                "type": "boolean",
                "label": "Bypass IntelliMix",
                "help": (
                    "Bypasses every IntelliMix DSP block. On an MXA920 / "
                    "MXA925, works while automatic coverage is off."
                ),
                "default": False,
                "setup": False,
                "state_key": "bypass_imx",
            },
            "mute_control_func": {
                "type": "enum",
                "label": "Mute Control",
                "help": "What the MXA320's mute button controls.",
                "values": [
                    {"value": "logic", "label": "Logic"},
                    {"value": "disabled", "label": "Disabled"},
                    {"value": "local", "label": "Local"},
                ],
                "state_key": "mute_control_func",
                "default": "local",
                "setup": False,
            },
            "autofocus": {
                "type": "boolean",
                "label": "Autofocus",
                "help": "Fine-tunes the lobes as talkers move.",
                "default": True,
                "setup": False,
                "state_key": "autofocus",
            },
            "speaker_delay_ms": {
                "type": "integer",
                "label": "Loudspeaker Delay (ms)",
                "help": (
                    "Delay on the loudspeaker output, 1-160 ms; 0 turns the "
                    "delay off."
                ),
                "state_key": "speaker_delay_ms",
                "default": 0,
                "min": 0,
                "max": 160,
                "setup": False,
            },
            "compressor_automix": {
                "type": "boolean",
                "label": "Automix Compressor",
                "help": "The compressor on the automix output.",
                "default": False,
                "setup": False,
                "state_key": "compressor_automix",
            },
            "compressor_speaker": {
                "type": "boolean",
                "label": "Loudspeaker Compressor",
                "help": "The compressor on the loudspeaker output.",
                "default": False,
                "setup": False,
                "state_key": "compressor_speaker",
            },
        },
        "commands": {
            "mute_on": {
                "label": "Mute",
                "help": "Mutes the device's microphone output.",
                "sets": {"mute": True},
            },
            "mute_off": {
                "label": "Unmute",
                "help": "Unmutes the device's microphone output.",
                "sets": {"mute": False},
            },
            "mute_toggle": {
                "label": "Toggle Mute",
                "help": "Mutes the device if it is unmuted, and the other way round.",
            },
            "set_channel_mute": {
                "label": "Set Channel Mute",
                "params": {
                    "channel": {
                        "type": "child_id",
                        "child_type": "channel",
                        "required": True,
                        "label": "Channel",
                    },
                    "mute": {"type": "boolean", "required": True},
                },
                "help": "Mutes or unmutes one channel or coverage area.",
                "sets": {"mute": "{mute}"},
            },
            "toggle_channel_mute": {
                "label": "Toggle Channel Mute",
                "params": {
                    "channel": {
                        "type": "child_id",
                        "child_type": "channel",
                        "required": True,
                        "label": "Channel",
                    },
                },
                "help": "Flips one channel's or coverage area's mute.",
            },
            "set_channel_gain": {
                "label": "Set Channel Gain",
                "params": {
                    "channel": {
                        "type": "child_id",
                        "child_type": "channel",
                        "required": True,
                        "label": "Channel",
                    },
                    "gain_db": {
                        "type": "number",
                        "required": True,
                        "min": -110.0,
                        "max": 30.0,
                        "help": "Digital gain in dB (-110 to +30).",
                    },
                },
                "help": "Sets one channel's or coverage area's gain, in dB.",
                "sets": {"gain_db": "{gain_db}"},
            },
            "step_channel_gain": {
                "label": "Nudge Channel Gain",
                "params": {
                    "channel": {
                        "type": "child_id",
                        "child_type": "channel",
                        "required": True,
                        "label": "Channel",
                    },
                    "delta_db": {
                        "type": "number",
                        "required": True,
                        "help": "Gain change in dB (positive or negative).",
                    },
                },
                "help": "Raises or lowers one channel's gain by an amount.",
            },
            "speaker_on": {
                "label": "Loudspeaker On",
                "help": "Turns the MXA902's loudspeaker on.",
                "sets": {"speaker": True},
            },
            "speaker_off": {
                "label": "Loudspeaker Off",
                "help": "Turns the MXA902's loudspeaker off.",
                "sets": {"speaker": False},
            },
            "set_polar_pattern": {
                "label": "Set Polar Pattern",
                "params": {
                    "channel": {
                        "type": "child_id",
                        "child_type": "channel",
                        "required": True,
                        "label": "Channel",
                    },
                    "pattern": {
                        "type": "enum",
                        "required": True,
                        "label": "Polar Pattern",
                        "values": [
                            {"value": "toroid", "label": "Toroid"},
                            {"value": "omni", "label": "Omnidirectional"},
                            {"value": "cardioid", "label": "Cardioid"},
                            {"value": "super", "label": "Supercardioid"},
                            {"value": "hyper", "label": "Hypercardioid"},
                            {
                                "value": "bidirection",
                                "label": "Bidirectional",
                            },
                        ],
                    },
                },
                "help": "Sets an MXA320 microphone's (1-4) pickup pattern.",
                "sets": {"polar_pattern": "{pattern}"},
            },
            "set_lobe_angle": {
                "label": "Set Lobe Angle",
                "params": {
                    "channel": {
                        "type": "child_id",
                        "child_type": "channel",
                        "required": True,
                        "label": "Channel",
                    },
                    "angle": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 345,
                        "label": "Angle (degrees)",
                        "help": "0 to 345 in 15-degree steps.",
                    },
                },
                "help": "Points an MXA320 microphone's (1-4) lobe.",
                "sets": {"lobe_angle": "{angle}"},
            },
            "set_led_brightness": {
                "label": "Set LED Brightness",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 5,
                        "label": "Brightness (0=off .. 5=full)",
                    },
                },
                "help": "Sets the status LED's brightness; 0 turns it off.",
                "sets": {"led_brightness": "{level}"},
            },
            "set_led_state": {
                "label": "Set LED State",
                "params": {
                    "which": {
                        "type": "enum",
                        "values": ["MUTED", "UNMUTED"],
                        "required": True,
                        "label": "Mute State to Configure",
                    },
                    "state": {
                        "type": "enum",
                        "values": ["ON", "OFF", "FLASHING"],
                        "required": True,
                        "label": "LED State",
                    },
                },
                "help": (
                    "Sets whether the LED is on, off or flashing while muted "
                    "or unmuted."
                ),
            },
            "set_led_color": {
                "label": "Set LED Color",
                "params": {
                    "which": {
                        "type": "enum",
                        "values": ["MUTED", "UNMUTED"],
                        "required": True,
                        "label": "Mute State to Configure",
                    },
                    "color": {
                        "type": "enum",
                        "values": [
                            "RED", "ORANGE", "GOLD", "YELLOW", "YELLOWGREEN",
                            "GREEN", "TURQUOISE", "POWDERBLUE", "CYAN",
                            "SKYBLUE", "BLUE", "PURPLE", "LIGHTPURPLE",
                            "VIOLET", "ORCHID", "PINK", "WHITE",
                        ],
                        "required": True,
                        "label": "LED Color",
                    },
                },
                "help": "Sets the LED's color while muted or unmuted.",
            },
            "flash_on": {
                "label": "Flash LEDs (Identify)",
                "help": (
                    "Flashes the device's lights so it can be found; stops "
                    "by itself after about 30 seconds."
                ),
            },
            "flash_off": {"label": "Stop Flashing", "help": "Stops the identify flash."},
            "recall_preset": {
                "label": "Recall Preset",
                "params": {
                    "preset": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 10,
                        "label": "Preset Number",
                        "description": (
                            "Presets 1-10 (MXA, P300, ANI; the SCM820 has no "
                            "presets)."
                        ),
                    },
                },
                "help": "Loads one of the device's saved presets.",
                "sets": {"preset": "{preset}"},
            },
            "reboot": {
                "label": "Reboot",
                "help": (
                    "Restarts the device. It drops off the network while it "
                    "starts up."
                ),
                # Unmeasured (no hardware): round the real gap up on the
                # bench.
                "restarts_device_for": 120,
            },
            "restore_defaults": {
                "label": "Restore Default Settings",
                "help": "Returns the device's settings to their factory defaults.",
                "confirm": (
                    "Returns every setting on the device to its factory "
                    "default: coverage, gains, mutes, DSP and LED settings."
                ),
            },
            "raw_command": {
                "label": "Raw Command",
                "params": {
                    "command": {
                        "type": "string",
                        "required": True,
                        "label": "Command String",
                        "help": "A raw command string, e.g. < GET SERIAL_NUM >.",
                    },
                },
                "help": (
                    "Sends a raw command string for surfaces this driver "
                    "does not model (lobe steering, coverage area positions, "
                    "PEQ filters, the MXA902 signal generator, P300 "
                    "outputs)."
                ),
            },
        },
        "quick_actions": [
            "mute_on", "mute_off", "speaker_on", "speaker_off", "flash_on",
            "recall_preset",
        ],
        "actions": [
            {"id": "mute_on", "kind": "command", "icon": "mic-off"},
            {"id": "mute_off", "kind": "command", "icon": "mic"},
            {"id": "speaker_on", "kind": "command", "icon": "volume-2"},
            {"id": "speaker_off", "kind": "command", "icon": "volume-x"},
            {"id": "flash_on", "kind": "command", "icon": "sun"},
            {"id": "recall_preset", "kind": "command", "icon": "bookmark"},
        ],
    }

    # The device pushes REP lines only for changes made elsewhere, so an idle,
    # untouched system exchanges no traffic and a silently dropped link (no
    # FIN) is invisible until the next command fails. Probe DEVICE_ID (present
    # on every covered model) as a heartbeat — a mute push or a meter sample
    # can't satisfy a DEVICE_ID probe, so it truly proves the link.
    HEALTH_FAULT_MESSAGE = (
        "The device stopped answering DEVICE_ID probes."
    )

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        # Resolved when a DEVICE_ID REP arrives (see _liveness_probe).
        self._probe_fut = None
        # (property, future) while a connect read waits for one answer (see
        # _ask).
        self._wait: tuple[str, asyncio.Future] | None = None
        self._channel_count = max(0, int(config.get("channel_count",
                                                     DEFAULT_CHANNEL_COUNT)))
        self._meter_ms = max(0, int(config.get("meter_interval_ms", 0)))
        self._talker_ms = max(
            0, int(config.get("talker_position_interval_ms", 0)))
        # The model picked in the settings, so a project built or simulated
        # before the device is reachable gets that model's channels and
        # controls. The device's own report replaces it on connect.
        self._family = _family_of(str(config.get("model") or ""))
        # Automatic coverage as the device last reported it (None = not yet).
        self._auto_coverage: bool | None = None
        super().__init__(device_id, config, state, events)
        self._narrow()

    # ── Model ──

    def _narrow(self) -> None:
        """Offer only the commands and settings this model's page documents.
        An unidentified model is offered the ones every model has."""
        base = type(self).DRIVER_INFO
        family = self._family
        commands = {
            cid: cdef for cid, cdef in base["commands"].items()
            if _offered(_COMMAND_MODELS.get(cid), family)
        }
        settings: dict[str, Any] = {}
        for key, sdef in base["device_settings"].items():
            if not _offered(_SETTING_WIRE[key][3], family):
                continue
            if key == "speech_gating":
                sdef = {
                    **sdef,
                    "values": [{"value": v, "label": _GATING_LABELS[v]}
                               for v in _GATING_WORDS[family]],
                    "default": "low" if "low" in _GATING_WORDS[family] else "on",
                }
            settings[key] = sdef
        self.DRIVER_INFO = {
            **base,
            "commands": commands,
            "device_settings": settings,
            "quick_actions": [q for q in base["quick_actions"] if q in commands],
            "actions": [a for a in base["actions"] if a["id"] in commands],
        }

    def _has(self, models: frozenset | None) -> bool:
        return _offered(models, self._family)

    def _coverage(self) -> bool:
        return self._family in _COVERAGE_MODELS

    def _auto_on(self) -> bool:
        # Automatic coverage is on from the factory; until the device says
        # otherwise, address the coverage areas.
        return self._coverage() and self._auto_coverage is not False

    def _ca_addressed(self, ch: int) -> bool:
        """Channel 1-8 of an MXA920 / MXA925 is a coverage area right now."""
        return self._auto_on() and ch in _COVERAGE_AREAS

    def _channel_map(self) -> dict[int, str] | None:
        return _CHANNEL_MAPS.get(self._family)

    def _channel_ids(self) -> list[int]:
        channels = self._channel_map()
        if channels is not None:
            return sorted(channels)
        return list(range(1, self._channel_count + 1))

    def _placeholder(self, ch: int) -> str:
        if self._ca_addressed(ch):
            return f"Coverage Area {ch}"
        channels = self._channel_map()
        if channels is not None and ch in channels:
            return channels[ch]
        return f"Channel {ch}"

    def _automix(self) -> int | None:
        return _AUTOMIX_CHANNEL.get(self._family)

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        if not self.config.get("host", ""):
            raise ConnectionError(f"[{self.device_id}] Shure host is required")

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # Every reply ends at its closing ">" — frame on that instead of the
        # platform's CR default.
        kwargs["delimiter"] = b">"
        return kwargs

    async def _initial_sync(self) -> None:
        # Seed the roster and every device / channel value; channels stay
        # fresh via push from here on (polling only refreshes device-level
        # state).
        self._auto_coverage = None
        self._register_topology()
        await self._sync_all()

    async def _close_session(self) -> None:
        # Runs on every teardown path: unblock a heartbeat probe still
        # waiting on a dead link.
        fut = self._probe_fut
        if fut is not None and not fut.done():
            fut.set_exception(ConnectionError("connection closed"))
        self._probe_fut = None
        if self._wait is not None and not self._wait[1].done():
            self._wait[1].set_exception(ConnectionError("connection closed"))
        self._wait = None

    # ── Topology ──

    def _project_label(self, ch: int) -> str:
        project = self._project_child_entities.get("channel", {}).get(f"{ch:02d}")
        return (project or {}).get("label", "") if project else ""

    def _register_topology(self) -> None:
        """Register the roster (the model's channel numbers, else the
        configured count). Idempotent. A placeholder label never overrides
        one the user set in the project, and follows the coverage mode; the
        device's CHAN_NAME then displays via label_field."""
        ids = self._channel_ids()
        existing = set(self.list_children("channel"))
        for n in ids:
            placeholder = None if self._project_label(n) else self._placeholder(n)
            if n in existing:
                if placeholder is not None:
                    self.set_child_state("channel", n, "label", placeholder)
            else:
                initial = {"label": placeholder} if placeholder else None
                self.register_child("channel", n, initial_state=initial)
        # Reconcile a shrunk or renumbered roster.
        for old in list(self.list_children("channel")):
            if int(old) not in ids:
                self.deregister_child("channel", old)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        tasks = getattr(self, "_bg_tasks", None)
        if tasks is None:
            tasks = self._bg_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def refresh_children(self) -> dict[str, Any]:
        """IDE 'Refresh from Device': re-register and re-read everything."""
        if not (self.transport and self.transport.connected):
            raise ConnectionError(f"[{self.device_id}] Not connected")
        self._register_topology()
        await self._sync_all()
        return {"channel": len(self._channel_ids())}

    async def _sync_all(self) -> None:
        """Read device + channel state from the device on connect / refresh.
        The model is asked first and waited for: it decides the channels and
        what else there is to read."""
        try:
            await self._ask("MODEL")
            for prop in ("DEVICE_ID", "DEVICE_AUDIO_MUTE", "FW_VER",
                         "SERIAL_NUM", "ENCRYPTION", "PRESET",
                         "LED_BRIGHTNESS"):
                await self._send(f"< GET {prop} >")
            await self._sync_model()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial sync failed")

    async def _ask(self, prop: str) -> None:
        """GET a property and wait for its answer (or REP ERR) before going
        on. Only one is outstanding, so the next REP ERR is its answer."""
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._wait = (prop, fut)
        try:
            await self._send(f"< GET {prop} >")
            await asyncio.wait_for(fut, _REPLY_WAIT_S)
        except asyncio.TimeoutError:
            log.info(f"[{self.device_id}] No answer to GET {prop}")
        finally:
            self._wait = None

    def _waiting_for(self, prop: str) -> bool:
        return self._wait is not None and self._wait[0] == prop

    def _answered(self) -> None:
        if self._wait is not None and not self._wait[1].done():
            self._wait[1].set_result(None)

    async def _sync_model(self) -> None:
        """Read what this model has beyond the common properties, then the
        channels and the subscriptions."""
        if self._coverage():
            # Channels 1-8 are read the way the mode needs.
            await self._ask("AUTO_COVERAGE")
        for key, (prop, index, _kind, models) in _SETTING_WIRE.items():
            if key in ("led_brightness", "auto_coverage"):
                continue
            if models is None or not self._has(models):
                continue
            idx = self._setting_index(index)
            if index is not None and idx is None:
                continue
            if idx is None:
                await self._send(f"< GET {prop} >")
            else:
                await self._send(f"< GET {idx} {prop} >")
        if self._has(frozenset({"MXA902"})):
            await self._send("< GET SPEAKER >")
        if self._has(_DEVICE_STATE_MODELS):
            await self._send("< GET DEVICE_STATE >")
        if self._has(frozenset({"MXA925"})):
            await self._send("< GET RJ45_MODE >")
        await self._sync_channels()
        await self._subscribe()

    async def _sync_channels(self, only: list[int] | None = None) -> None:
        ids = [n for n in self._channel_ids() if only is None or n in only]
        if not ids:
            return
        areas = [n for n in ids if self._ca_addressed(n)]
        plain = [n for n in ids if n not in areas]
        if areas:
            await self._send("< GET 0 CA_MUTE >")
            for n in areas:
                await self._send(f"< GET {n} CA_GAIN >")
            await self._send("< GET 0 AUTOMIX_GATE_OUT_CA >")
        if plain:
            # Names and mutes fan out from the index-0 query; gain is
            # per-channel (index 0 is not valid for a gain query on the
            # MXA910). With automatic coverage on, index 0 reports the
            # automix output alone.
            await self._send("< GET 0 CHAN_NAME >")
            await self._send("< GET 0 AUDIO_MUTE >")
            for n in plain:
                await self._send(f"< GET {n} AUDIO_GAIN_HI_RES >")
            if self._has(_GATE_MODELS) and not self._auto_on():
                await self._send("< GET 0 AUTOMIX_GATE_OUT_EXT_SIG >")
        if self._has(_MXA_ALL):
            await self._send("< GET 0 AUDIO_OUT_CLIP_INDICATOR >")
        if self._has(frozenset({"MXA320"})) and only is None:
            await self._send("< GET 0 POLAR_PATTERN >")
            await self._send("< GET 0 LOBE_ANGLE >")

    async def _subscribe(self) -> None:
        # Metering (opt-in). The command name differs across models (the
        # P300 uses METER_RATE_IN); a model this driver knows gets its own,
        # anything else both, and the unsupported one answers REP ERR.
        if self._meter_ms > 0:
            names = ["METER_RATE"]
            if not self._has(_MXA_ALL):
                names.append("METER_RATE_IN")
            if self._coverage() and self._auto_coverage is True:
                # Not yet known: the AUTO_COVERAGE report arms it.
                names.append("CA_METER_RATE")
            for name in names:
                await self._send(f"< SET {name} {self._meter_ms} >")
        if self._talker_ms > 0 and self._has(_TALKER_MODELS):
            await self._send(f"< SET TALKER_POSITION_RATE {self._talker_ms} >")

    def _set_family(self, family: str) -> None:
        """The device reported a different model than the one assumed."""
        self._family = family
        self._auto_coverage = None
        self._narrow()
        self._register_topology()

    async def _resync_model(self) -> None:
        try:
            await self._sync_model()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Model resync failed")

    async def _apply_coverage(self) -> None:
        """Automatic coverage changed: channels 1-8 are now the other kind."""
        self._register_topology()
        for n in _COVERAGE_AREAS:
            if n in self._channel_ids():
                # A lobe's name and gate do not belong to a coverage area,
                # and the other way round; the new mode's reads refill them.
                self.set_child_state("channel", n, "name", None)
                self.set_child_state("channel", n, "gate", None)
        try:
            await self._sync_channels(only=list(_COVERAGE_AREAS) + [9])
            if self._meter_ms > 0 and self._auto_on():
                await self._send(f"< SET CA_METER_RATE {self._meter_ms} >")
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Coverage resync failed")

    # ── Sending ──

    async def _send(self, message: str) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(message.encode("ascii"))

    async def _liveness_probe(self) -> None:
        """Send `< GET DEVICE_ID >` and await its REP (correlated by param).

        A mute push or a meter SAMPLE does not resolve the future — only a
        DEVICE_ID REP does — so the probe genuinely proves the link is alive.
        """
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._probe_fut = fut
        try:
            await self._send("< GET DEVICE_ID >")
            await fut
        finally:
            self._probe_fut = None

    # ── Commands ──

    def _mute_prop(self, ch: int) -> str:
        return "CA_MUTE" if self._ca_addressed(ch) else "AUDIO_MUTE"

    def _gain_prop(self, ch: int) -> str:
        return "CA_GAIN" if self._ca_addressed(ch) else "AUDIO_GAIN_HI_RES"

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "mute_on":
            await self._send("< SET DEVICE_AUDIO_MUTE ON >")
        elif command == "mute_off":
            await self._send("< SET DEVICE_AUDIO_MUTE OFF >")
        elif command == "mute_toggle":
            await self._send("< SET DEVICE_AUDIO_MUTE TOGGLE >")
        elif command == "set_channel_mute":
            ch = int(params["channel"])
            state = "ON" if params["mute"] else "OFF"
            await self._send(f"< SET {ch} {self._mute_prop(ch)} {state} >")
        elif command == "toggle_channel_mute":
            ch = int(params["channel"])
            await self._send(f"< SET {ch} {self._mute_prop(ch)} TOGGLE >")
        elif command == "set_channel_gain":
            ch = int(params["channel"])
            wire = _db_to_wire(float(params["gain_db"]))
            await self._send(f"< SET {ch} {self._gain_prop(ch)} {wire} >")
        elif command == "step_channel_gain":
            ch = int(params["channel"])
            delta = float(params["delta_db"])
            step = abs(round(delta * 10))
            verb = "inc" if delta >= 0 else "dec"
            await self._send(f"< SET {ch} {self._gain_prop(ch)} {verb} {step} >")
        elif command == "speaker_on":
            await self._send("< SET SPEAKER ON >")
        elif command == "speaker_off":
            await self._send("< SET SPEAKER OFF >")
        elif command == "set_polar_pattern":
            ch = int(params["channel"])
            pattern = str(params["pattern"]).upper()
            if pattern not in _POLAR_PATTERNS:
                raise ValueError(f"Unknown polar pattern: {params['pattern']}")
            await self._send(f"< SET {ch} POLAR_PATTERN {pattern} >")
        elif command == "set_lobe_angle":
            ch = int(params["channel"])
            angle = int(params["angle"])
            if angle % 15 or not 0 <= angle <= 345:
                raise ValueError(
                    f"Lobe angle {angle} is not 0-345 in 15-degree steps")
            await self._send(f"< SET {ch} LOBE_ANGLE {angle} >")
        elif command == "set_led_brightness":
            level = int(params["level"])
            await self._send(f"< SET LED_BRIGHTNESS {level} >")
        elif command == "set_led_state":
            await self._send(
                f"< SET LED_STATE_{params['which']} {params['state']} >"
            )
        elif command == "set_led_color":
            await self._send(
                f"< SET LED_COLOR_{params['which']} {params['color']} >"
            )
        elif command == "flash_on":
            await self._send("< SET FLASH ON >")
        elif command == "flash_off":
            await self._send("< SET FLASH OFF >")
        elif command == "recall_preset":
            await self._send(f"< SET PRESET {int(params['preset'])} >")
        elif command == "reboot":
            await self._send("< SET REBOOT >")
        elif command == "restore_defaults":
            await self._send("< SET DEFAULT_SETTINGS >")
        elif command == "raw_command":
            await self._send(str(params["command"]))
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    def _setting_index(self, index: Any) -> int | None:
        if index == "automix":
            return self._automix()
        return index

    async def set_device_setting(self, key: str, value: Any) -> Any:
        spec = _SETTING_WIRE.get(key)
        if spec is None:
            raise ValueError(f"[{self.device_id}] Unknown device setting: {key}")
        prop, index, kind, _models = spec
        if kind == "int":
            wire = str(int(value))
        elif kind == "delay":
            wire = f"{int(value):04d}"
        elif kind == "onoff":
            wire = "ON" if _as_bool(value) else "OFF"
        elif kind == "title_onoff":
            wire = "On" if _as_bool(value) else "Off"
        elif kind == "gating":
            words = _GATING_WORDS.get(self._family, {})
            wire = words.get(str(value).lower(), "")
            if not wire:
                raise ValueError(
                    f"[{self.device_id}] Speech gating {value!r} is not one "
                    f"of this model's levels")
        else:
            wire = str(value).upper()
        idx = self._setting_index(index)
        if index is not None and idx is None:
            raise ValueError(
                f"[{self.device_id}] {key} needs a model with an automix output")
        if idx is None:
            await self._send(f"< SET {prop} {wire} >")
        else:
            await self._send(f"< SET {idx} {prop} {wire} >")

    async def poll(self) -> None:
        # Device-level heartbeat/refresh; channels are push-driven.
        await self._send("< GET DEVICE_AUDIO_MUTE >")
        await self._send("< GET LED_BRIGHTNESS >")
        if self._has(_DEVICE_STATE_MODELS):
            await self._send("< GET DEVICE_STATE >")

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        # The transport frames on ">", delivering one message body (delimiter
        # stripped) per call. Split defensively in case a chunk carries more
        # than one ">"; _handle_frame normalizes the leading "<" and any CR/LF
        # that rode along from the prior line.
        for part in data.split(b">"):
            self._handle_frame(part)

    def _handle_frame(self, raw: bytes) -> None:
        text = raw.decode("ascii", errors="replace").strip()
        text = text.lstrip("<").strip()
        if not text:
            return
        tokens = text.split()
        head = tokens[0].upper()
        if head in ("SAMPLE", "SAMPLE_IN"):
            if len(tokens) > 1 and tokens[1].upper().startswith("TALKER_POS"):
                # TALKER_POSITIONS (the array-group example spells it
                # TALKER_POSTIONS).
                self._apply_talkers(tokens[2:])
            else:
                self._apply_sample(tokens[1:])
            return
        if head == "SAMPLE_CA":
            self._apply_area_sample(tokens[1:])
            return
        if head != "REP" or len(tokens) < 2:
            # SAMPLE_OUT / _PRECOMP / _AECREF / _AGC are other meters.
            return
        if tokens[1].upper() == "ERR":
            # A property this device does not have; the answer to an
            # outstanding _ask (the SCM820 has no MODEL).
            self._answered()
            return
        if tokens[1].isdigit() and len(tokens) >= 3:
            self._handle_channel_rep(int(tokens[1]), tokens[2].upper(),
                                     text.split(None, 3)[3] if len(tokens) > 3 else "")
        else:
            self._handle_device_rep(tokens[1].upper(),
                                    text.split(None, 2)[2] if len(tokens) > 2 else "")

    def _handle_device_rep(self, prop: str, value: str) -> None:
        if prop == "DEVICE_ID":
            self.set_state("device_name", _strip_string_value(value))
            fut = self._probe_fut
            if fut is not None and not fut.done():
                fut.set_result(None)
            return
        if prop == "DEVICE_AUDIO_MUTE":
            muted = _on(value)
            if muted is not None:
                self.set_state("mute", muted)
                self.set_state("audio_mute", muted)
            return
        if prop == "FW_VER":
            self.set_state("firmware", _strip_string_value(value))
            return
        if prop == "SERIAL_NUM":
            self.set_state("serial_number", _strip_string_value(value))
            return
        if prop == "MODEL":
            self._on_model(_strip_string_value(value))
            return
        if prop == "AUTO_COVERAGE":
            on = _on(value)
            if on is not None:
                self._on_auto_coverage(on)
            return
        if prop == "DEVICE_STATE":
            code = _DEVICE_STATES.get(value.strip())
            if code is not None:
                self.set_state("device_state", code)
            return
        if prop in _DEVICE_FLAGS:
            on = _on(value)
            if on is not None:
                self.set_state(_DEVICE_FLAGS[prop], on)
            return
        if prop in _DEVICE_INTS:
            number = _int(value)
            if number is not None:
                self.set_state(_DEVICE_INTS[prop], number)
            return
        if prop in _DEVICE_WORDS:
            key, allowed = _DEVICE_WORDS[prop]
            word = value.strip().lower()
            if word in allowed:
                self.set_state(key, word)
            return

    def _handle_channel_rep(self, idx: int, prop: str, value: str) -> None:
        if prop in ("AUDIO_MUTE", "CA_MUTE"):
            muted = _on(value)
            if muted is not None:
                self._set_channel(idx, "mute", muted)
        elif prop in ("AUDIO_GAIN_HI_RES", "CA_GAIN"):
            wire = _int(value)
            if wire is not None:
                self._set_channel(idx, "gain_db", _wire_to_db(wire))
        elif prop == "CHAN_NAME":
            self._set_channel(idx, "name", _strip_string_value(value))
        elif prop == "AUDIO_OUT_CLIP_INDICATOR":
            clip = _on(value)
            if clip is not None:
                self._set_channel(idx, "clip", clip)
        elif prop == "AUTOMIX_GATE_OUT_CA":
            gate = _on(value)
            if gate is not None:
                self._set_channel(idx, "gate", gate)
        elif prop == "AUTOMIX_GATE_OUT_EXT_SIG":
            # Per lobe. With automatic coverage on, channels 1-8 are coverage
            # areas, whose gate is AUTOMIX_GATE_OUT_CA.
            gate = _on(value)
            if gate is not None and not self._ca_addressed(idx):
                self._set_channel(idx, "gate", gate)
        elif prop == "POLAR_PATTERN":
            word = value.strip().lower()
            if word.upper() in _POLAR_PATTERNS:
                self._set_channel(idx, "polar_pattern", word)
        elif prop == "LOBE_ANGLE":
            angle = _int(value)
            if angle is not None:
                self._set_channel(idx, "lobe_angle", angle)
        elif prop == "SPEECH_GATING":
            word = value.strip().lower()
            if idx == self._automix() and word in _GATING_LABELS:
                self.set_state("speech_gating", word)
        elif prop == "NOISE_FILTER":
            on = _on(value)
            if idx == self._automix() and on is not None:
                self.set_state("noise_filter", on)
        elif prop == "COMPRESSOR":
            on = _on(value)
            key = {9: "compressor_automix", 10: "compressor_speaker"}.get(idx)
            if key and on is not None and self._family == "MXA902":
                self.set_state(key, on)
        elif prop == "DELAY":
            delay = _int(value)
            if idx == 10 and delay is not None and self._family == "MXA902":
                self.set_state("speaker_delay_ms", delay)

    def _on_model(self, model: str) -> None:
        self.set_state("model", model)
        family = _family_of(model)
        if family:
            self.set_state("model_family", family)
        count = _channel_count_for_model(model)
        if count is not None:
            self.set_state("channel_count_reported", count)
        if family != self._family:
            self._set_family(family)
            if not self._waiting_for("MODEL"):
                # Reported outside a connect read: read what it has.
                self._spawn(self._resync_model())
        if self._waiting_for("MODEL"):
            self._answered()

    def _on_auto_coverage(self, on: bool) -> None:
        self.set_state("auto_coverage", on)
        if self._waiting_for("AUTO_COVERAGE"):
            # The connect read: its channel reads follow in this mode.
            self._auto_coverage = on
            self._register_topology()
            if on:
                # Coverage areas have no names on the wire; a lobe name kept
                # from before would label the wrong thing.
                for n in _COVERAGE_AREAS:
                    if n in self._channel_ids():
                        self.set_child_state("channel", n, "name", None)
            self._answered()
            return
        if not self._coverage() or on == self._auto_coverage:
            return
        self._auto_coverage = on
        self._spawn(self._apply_coverage())

    def _set_channel(self, idx: int, prop: str, value: Any) -> None:
        # Index 0 is the "all channels" broadcast — a fan-out never REPs it as
        # its own line, but guard anyway.
        if idx in self._channel_ids():
            self.set_child_state("channel", idx, prop, value)

    def _apply_sample(self, tokens: list[str]) -> None:
        """Map a SAMPLE row onto channel meter props.

        Values are ``000-060`` per channel, in the device's channel order,
        representing -60..0 dBFS. With automatic coverage on, an MXA920 /
        MXA925 meters the automix output alone (its coverage areas come as
        SAMPLE_CA). Otherwise the row follows the model's channel numbers
        (an MXA902's four values are 9-12); a value past the modelled
        channels (an AEC reference input) is dropped. A single value from a
        device the driver cannot place is skipped rather than mislabelled.
        """
        values = [_int(t) for t in tokens]
        if not values:
            return
        if self._auto_on():
            if len(values) == 1 and values[0] is not None:
                self._set_channel(9, "meter",
                                  float(values[0] - _METER_DBFS_OFFSET))
            return
        ids = self._channel_ids()
        if len(values) == 1 and len(ids) != 1:
            return
        for ch, raw in zip(ids, values):
            if raw is not None:
                self._set_channel(ch, "meter", float(raw - _METER_DBFS_OFFSET))

    def _apply_area_sample(self, tokens: list[str]) -> None:
        """SAMPLE_CA: one level per coverage area, areas 1-8 in order."""
        if not self._auto_on():
            return
        for ch, tok in zip(_COVERAGE_AREAS, tokens):
            raw = _int(tok)
            if raw is not None:
                self._set_channel(ch, "meter", float(raw - _METER_DBFS_OFFSET))

    def _apply_talkers(self, tokens: list[str]) -> None:
        """SAMPLE TALKER_POSITIONS: sets of lobe, coverage area, X, Y, Z and
        a reserved 0, one set per talker heard in the reporting period. The
        first talker's position goes to the talker_* state."""
        numbers = [_int(t) for t in tokens]
        if not numbers or any(n is None for n in numbers):
            return
        # Six numbers per talker; the page's array-group example leaves the
        # reserved field off.
        size = 6 if len(numbers) % 6 == 0 else 5 if len(numbers) % 5 == 0 else 0
        if not size:
            return
        lobe, area, x, y, z = numbers[:5]
        self.set_state("talker_lobe", lobe)
        self.set_state("talker_coverage_area", area)
        self.set_state("talker_x", x)
        self.set_state("talker_y", y)
        self.set_state("talker_z", z)
        self.set_state("talker_count", len(numbers) // size)


def _offered(models: frozenset | None, family: str) -> bool:
    return models is None or family in models


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)
