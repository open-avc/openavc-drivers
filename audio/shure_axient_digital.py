"""
OpenAVC Shure Axient Digital receiver driver.

Controls and monitors Shure Axient Digital rack receivers (AD4D two-channel,
AD4Q four-channel) over Shure's ASCII command strings on TCP port 2202: the
same ``< GET / SET / REP >`` grammar the conferencing driver (``shure_network``)
speaks, with the wireless property set on top.

Protocol summary (Shure "Axient Digital Command Strings"):
    Command : ``< [GET|SET] [x] PROPERTY [value...] >``
    Reply   : ``< REP [x] PROPERTY value... >``   (``< REP ERR >`` on a refusal)
    Meter   : ``< SAMPLE x ALL ... >``           (only while METER_RATE > 0)
    - ``>`` ends every frame; ``x`` is the channel (1-4; 0 fans a GET out to
      every channel). Slot properties carry a slot index after the property.
    - String values are brace-wrapped and space-padded to a fixed width
      (``{Lead Vox                       }``); the parser strips both.
    - Every non-metered property is REPorted when it changes, from any
      source (front panel, Wireless Workbench, another controller), so the
      driver seeds state on connect and then mirrors pushes. Metered values
      (audio level, RSSI, channel quality) only travel in SAMPLE frames at the
      opt-in METER_RATE.

State model:
    - ``channel`` child entities (1..N, N from the receiver's MODEL: AD4D 2,
      AD4Q 4; the config field is the offline fallback) carry the audio and
      RF surface, the decoded transmitter's side-channel telemetry (battery,
      power, lock, mute switch, talk switch) and the opt-in meters. The
      per-channel names follow ``sennheiser_ewdx`` where the concept matches
      (mute, gain, frequency_khz, no_link, tx_linked, tx_battery_percent,
      tx_battery_minutes, tx_low_battery, rf_peak, af_peak, level_dbfs,
      rssi_dbm) so one panel template serves both receiver families.
    - ``slot`` child entities (``<channel>-<slot>``, 8 per channel) are the
      transmitter registration slots. A slot is a place a transmitter CAN
      be, so the roster reports presence: an EMPTY slot is ``not_fitted``, a
      LINKED.INACTIVE ShowLink transmitter is ``not_responding``, and only a
      LINKED.ACTIVE ADX transmitter accepts the remote-control writes (RF
      mute, power mode, gain offset, polarity, input pad, name).

Python (not YAML) because the wire values need arithmetic (every level,
gain, offset and temperature is reported with an offset the driver removes,
and 255 / 65533..65535 are sentinels that must become "unknown" plus a
state, not a number), a SAMPLE frame's layout depends on the channel's
Quadversity and frequency-diversity modes and fans one line out into up to
twenty keys, and the two-level roster (channels, then eight slots each, with
presence read off SLOT_STATUS) is registered from the receiver's own MODEL.

Push vs poll: push. The receiver reports every change itself; the 30 s poll
only re-reads the device-level values as a liveness baseline, and an awaited
``< GET MODEL >`` probe catches a link that died without a FIN.

Source: https://www.shure.com/en-US/docs/commandstrings/AD4  (Shure,
"Axient Digital Command Strings"; read 2026-09-13). No hardware was
available for this build: every claim below is the document's, verified in
the simulator only.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from openavc.core.connection_fault import (
    CHILD_NOT_FITTED,
    CHILD_NOT_RESPONDING,
)
from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)


DEFAULT_CHANNEL_COUNT = 4
MAX_CHANNELS = 4
SLOTS_PER_CHANNEL = 8

# AUDIO_GAIN travels as 000-060 with an offset of 18: the actual gain is
# -18..+42 dB in 1 dB steps.
_GAIN_OFFSET = 18
_GAIN_DB_MIN = -18
_GAIN_DB_MAX = 42

# Audio levels (AUDIO_LEVEL_PEAK / _RMS) and RSSI are 000-120, actual = raw - 120.
_LEVEL_OFFSET = 120
# TX_OFFSET / SLOT_OFFSET: 000-033, actual = raw - 12 (-12..+21 dB).
_OFFSET_OFFSET = 12
_OFFSET_DB_MIN = -12
_OFFSET_DB_MAX = 21
# TX_BATT_TEMP_C: actual = raw - 40.
_TEMP_OFFSET = 40

# 3-character "unknown" sentinel and the 5-character battery-minute sentinels.
_UNKNOWN_3 = 255
_MINS_COMM_WARNING = 65533
_MINS_CALCULATING = 65534
_MINS_UNKNOWN = 65535

# Bit 6 of an RSSI LED bitmap is the red overload / RF pad LED; bit 8 of the
# audio LED bitmap is the red OL LED.
_RSSI_OVERLOAD_BIT = 0x20
_AUDIO_OVERLOAD_BIT = 0x80

# The character set Shure allows in a DEVICE_ID / CHAN_NAME / SLOT_TX_DEVICE_ID
# SET (1-8 characters).
_NAME_PATTERN = r"^[A-Za-z0-9 !\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`~]{1,8}$"

_FD_MODES = {"OFF": "off", "FD-C": "combining", "FD-S": "selection"}
_SLOT_STATUS = {
    "EMPTY": "empty",
    "STANDARD": "standard",
    "LINKED.INACTIVE": "linked_inactive",
    "LINKED.ACTIVE": "linked_active",
}
_TX_MODELS = ["AD1", "AD2", "ADX1", "ADX1M", "ADX2", "ADX2FD"]


def _strip_string_value(raw: str) -> str:
    """Unwrap a brace-wrapped, space-padded string value."""
    raw = raw.strip()
    m = re.search(r"\{(.*)\}", raw, re.S)
    if m:
        return m.group(1).strip()
    return raw


def _int3(token: str) -> int | None:
    """A 3-character numeric value; 255 is the device's "unknown"."""
    try:
        value = int(token)
    except ValueError:
        return None
    return None if value == _UNKNOWN_3 else value


def _minutes(token: str) -> tuple[int | None, str]:
    """A 5-character battery-minutes value and the state it encodes."""
    try:
        value = int(token)
    except ValueError:
        return None, "unknown"
    if value == _MINS_COMM_WARNING:
        return None, "communication_warning"
    if value == _MINS_CALCULATING:
        return None, "calculating"
    if value >= _MINS_UNKNOWN:
        return None, "unknown"
    return value, "ok"


def _cycles(token: str) -> int | None:
    try:
        value = int(token)
    except ValueError:
        return None
    return None if value >= _MINS_UNKNOWN else value


def _offset_db(token: str) -> int | None:
    raw = _int3(token)
    return None if raw is None else raw - _OFFSET_OFFSET


def _input_pad(token: str) -> bool | None:
    """000 = pad ON (-12 dB), 012 = pad OFF, 255 = not applicable."""
    raw = _int3(token)
    return None if raw is None else raw == 0


def _level(token: str) -> float | None:
    raw = _int3(token)
    return None if raw is None else float(raw - _LEVEL_OFFSET)


def _group_channel(text: str) -> tuple[str, str]:
    """``{6,100     }`` -> ("6", "100"); the ``--,--`` wildcard -> ("", "")."""
    body = _strip_string_value(text)
    if "," not in body:
        return "", ""
    group, _, chan = body.partition(",")
    group, chan = group.strip(), chan.strip()
    if group.startswith("-") or chan.startswith("-"):
        return "", ""
    return group, chan


def _channel_count_for_model(model: str) -> int | None:
    """The channel count the receiver's MODEL string implies."""
    upper = model.upper()
    if upper.startswith("AD4Q"):
        return 4
    if upper.startswith("AD4D"):
        return 2
    return None


def slot_id(channel: int, slot: int) -> str:
    return f"{channel}-{slot}"


class ShureAxientDigitalDriver(BaseDriver):
    """Shure Axient Digital AD4D / AD4Q receiver driver."""

    DRIVER_INFO = {
        "id": "shure_axient_digital",
        "name": "Shure Axient Digital Receivers",
        "manufacturer": "Shure",
        "category": "audio",
        "version": "1.0.0",
        "min_platform_version": "0.33.0",
        "author": "OpenAVC",
        "description": (
            "Controls and monitors Shure Axient Digital AD4D and AD4Q wireless "
            "receivers over Shure's command strings on TCP port 2202. Each "
            "receiver channel is a child entity with mute, gain, frequency and "
            "group/channel preset, interference and encryption warnings, the "
            "decoded transmitter's battery, RF power, lock and mute switch, "
            "and opt-in audio and RF meters. Each of a channel's eight "
            "transmitter slots is a child entity; a linked ADX transmitter's "
            "RF mute, power mode, gain offset, polarity, input pad and name "
            "are remote-controllable. State pushes live as it changes."
        ),
        "source_url": "https://www.shure.com/en-US/docs/commandstrings/AD4",
        "tags": ["wireless", "microphone", "receiver", "axient", "rf"],
        "verified": False,
        "simulated": True,
        "protocols": ["shure_command_strings"],
        "ports": [2202],
        "transport": "tcp",
        "discovery": {
            # Every Axient Digital receiver answers MODEL on 2202 with an
            # AD4-prefixed name. The conferencing driver probes the same port
            # with DEVICE_ID, which every Shure device answers; this probe is
            # the specific one, so a scan offers this driver for a receiver.
            "tcp_probe": {
                "port": 2202,
                "send_ascii": "< GET MODEL >\r\n",
                "expect_regex": r"<\s*REP\s+MODEL\s+\{\s*AD4",
                "extract_manufacturer": "Shure",
                "extract": {
                    "model": {
                        "regex": r"<\s*REP\s+MODEL\s+\{\s*(AD4[^}\s]*)",
                    },
                },
            },
            "oui": [
                "00:0e:dd",   # Shure Incorporated
                "d8:34:ee",   # Shure Incorporated
            ],
            "hostname": ["^AD4"],
            "port_open": [2202],
            "manufacturer_alias": ["shure", "shure incorporated"],
        },
        "compatible_models": [
            {
                "manufacturer": "Shure",
                "models": ["AD4D", "AD4Q"],
                "confidence": "untested",
                "notes": (
                    "Both share one command set. The AD4Q is four channels, "
                    "the AD4D two; the driver reads the count from the "
                    "receiver's model name, so Channel Count only matters "
                    "before the first connection. Quadversity mode (AD4Q "
                    "only) and FD-C frequency diversity change the meter "
                    "layout and are handled automatically."
                ),
            }
        ],
        "help": {
            "overview": (
                "Shure Axient Digital receivers speak Shure's ASCII command "
                "strings on TCP port 2202. This driver models each receiver "
                "channel as a child entity carrying its mute, gain, frequency "
                "and preset, its warnings (interference, encryption mismatch, "
                "unregistered transmitter), the received transmitter's "
                "battery and settings, and opt-in level and RF meters. Each "
                "channel's eight transmitter slots are child entities too, and "
                "a linked ADX transmitter can be adjusted from here: RF mute, "
                "power mode, gain offset, polarity, input pad and name.\n\n"
                "State is push-driven: after connecting, the driver reads "
                "everything once, then mirrors the reports the receiver sends "
                "whenever anything changes, including changes made on the "
                "front panel or in Wireless Workbench. Meters stream only "
                "when Meter Interval is set or a channel's meters are turned "
                "on."
            ),
            "setup": (
                "1. Give the receiver's Shure Control network interface a "
                "static IP or a DHCP reservation.\n"
                "2. In OpenAVC, enter that IP. Port 2202 is fixed.\n"
                "3. Set Channel Count to 4 for an AD4Q or 2 for an AD4D. The "
                "receiver's model name corrects it on the first connection.\n"
                "4. To see live audio and RF meters on a panel, set Meter "
                "Interval to a value like 500 ms (0 leaves metering off; the "
                "receiver streams a SAMPLE frame per channel at that rate)."
            ),
        },
        "default_config": {
            "host": "",
            "port": 2202,
            "channel_count": 4,
            "poll_interval": 30,
            "meter_interval_ms": 0,
            "low_battery_bars": 1,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "The receiver's Shure Control interface address.",
            },
            "port": {
                "type": "integer",
                "default": 2202,
                "label": "Port",
                "description": "Shure command-strings port, always 2202.",
                "advanced": True,
            },
            "channel_count": {
                "type": "integer",
                "default": 4,
                "min": 1,
                "max": 4,
                "label": "Channel Count",
                "description": (
                    "4 for an AD4Q, 2 for an AD4D. Only used until the "
                    "receiver reports its model, which sets the real count."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often to re-read the device-level values. Everything "
                    "else is pushed by the receiver, so this can be gentle."
                ),
                "advanced": True,
            },
            "meter_interval_ms": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "max": 65535,
                "label": "Meter Interval (ms)",
                "description": (
                    "Level and RF meter update rate in milliseconds (0 = off; "
                    "the receiver accepts 100 to 65535). Metering streams a "
                    "frame per channel continuously, so leave it off unless a "
                    "panel shows live meters."
                ),
            },
            "low_battery_bars": {
                "type": "integer",
                "default": 1,
                "min": 0,
                "max": 5,
                "label": "Low Battery Bars",
                "description": (
                    "Low Battery is raised when a transmitter's battery bars "
                    "are at or below this (0 never raises it)."
                ),
                "advanced": True,
            },
        },
        "state_variables": {
            "device_name": {
                "type": "string", "label": "Device Name",
                "help": "The receiver's device ID, as Wireless Workbench shows "
                        "it.",
            },
            "model": {
                "type": "string", "label": "Model",
                "help": "The model name the receiver reports (AD4D-A, "
                        "AD4Q-B and so on).",
            },
            "firmware": {"type": "string", "label": "Firmware Version"},
            "selftest_failed": {
                "type": "boolean", "label": "Self Test Failed",
                "cloud_priority": "high",
                "help": "The receiver marked its firmware version with an "
                        "asterisk: the last firmware update did not complete.",
            },
            "rf_band": {
                "type": "string", "label": "RF Band",
                "help": "The receiver's tuning band (G55, K54, X55 and so on).",
            },
            "encryption_enabled": {
                "type": "boolean", "label": "Encryption",
                "cloud_priority": "high",
                "help": "AES-256 encryption is on (set on the receiver or in "
                        "Wireless Workbench; not writable over control).",
            },
            "quadversity": {
                "type": "boolean", "label": "Quadversity",
                "help": "The AD4Q is in Quadversity mode (four antennas per "
                        "channel, two channels).",
            },
            "transmission_mode": {
                "type": "enum", "values": ["standard", "high_density"],
                "label": "Transmission Mode",
            },
            "channel_count_reported": {
                "type": "integer", "label": "Channels", "min": 2, "max": 4,
                "help": "Channel count read from the receiver's model name.",
            },
            "identifying": {
                "type": "boolean", "label": "Identifying",
                "help": "The receiver's front panel is flashing to identify "
                        "it.",
            },
            "ip_address": {
                "type": "string", "label": "Control IP Address",
                "help": "The Shure Control interface address the receiver "
                        "reports.",
            },
            "mac_address": {
                "type": "string", "label": "Control MAC Address",
            },
        },
        "child_entity_types": {
            "channel": {
                "label": "Channel",
                "label_plural": "Channels",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "state_variables": {
                    "name": {
                        "type": "string", "label": "Channel Name",
                        "help": "The receiver's channel name (CHAN_NAME); the receiver pads "
                                "it to 31 characters and accepts 8 in a write.",
                    },
                    "mute": {
                        "type": "boolean", "label": "Mute", "control": True,
                        "cloud_priority": "high",
                        "help": "Audio output mute for this channel (AUDIO_MUTE).",
                    },
                    "gain": {
                        "type": "integer", "label": "Gain",
                        "min": -18, "max": 42, "step": 1, "unit": "dB",
                        "control": True,
                        "help": "Channel audio gain in dB (-18 to +42).",
                    },
                    "frequency_khz": {
                        "type": "integer", "label": "Frequency", "unit": "kHz",
                        "control": True,
                        "help": "Carrier frequency in kHz. Setting it clears the group/channel "
                                "preset.",
                    },
                    "preset_bank": {
                        "type": "string", "label": "Group",
                        "help": "The frequency group of the active group/channel preset; "
                                "empty when the frequency was set directly.",
                    },
                    "preset_channel": {
                        "type": "string", "label": "Preset Channel",
                        "help": "The channel number of the active group/channel preset; "
                                "empty when the frequency was set directly.",
                    },
                    "fd_mode": {
                        "type": "enum", "values": ["off", "combining", "selection"],
                        "label": "Frequency Diversity",
                        "help": "off, combining (FD-C, both carriers combined) or selection "
                                "(FD-S). An FD-C channel has a second frequency.",
                    },
                    "frequency2_khz": {
                        "type": "integer", "label": "Frequency 2", "unit": "kHz",
                        "help": "The second carrier of an FD-C channel, in kHz.",
                    },
                    "preset_bank2": {"type": "string", "label": "Group 2"},
                    "preset_channel2": {"type": "string", "label": "Preset Channel 2"},
                    "aes256_error": {
                        "type": "boolean", "label": "AES-256 Error", "cloud_priority": "high",
                        "help": "The receiver detected a transmitter whose encryption does "
                                "not match, so no audio passes.",
                    },
                    "interference": {
                        "type": "boolean", "label": "Interference", "cloud_priority": "high",
                        "help": "The receiver detected interference on this channel's "
                                "frequency.",
                    },
                    "interference2": {
                        "type": "boolean", "label": "Interference 2",
                        "help": "Interference on the second carrier of an FD-C channel.",
                    },
                    "unregistered_tx": {
                        "type": "boolean", "label": "Unregistered Transmitter",
                        "cloud_priority": "high",
                        "help": "A transmitter that is not registered to this channel is "
                                "being received.",
                    },
                    "identifying": {
                        "type": "boolean", "label": "Identifying",
                        "help": "The channel's display is flashing to show which channel this "
                                "is.",
                    },
                    "metering": {
                        "type": "boolean", "label": "Metering",
                        "help": "SAMPLE frames are streaming for this channel.",
                    },
                    "meter_rate_ms": {
                        "type": "integer", "label": "Meter Rate", "unit": "ms",
                        "help": "Interval between SAMPLE frames; 0 when metering is off.",
                    },
                    # ── The decoded transmitter (side channel) ──
                    "tx_linked": {
                        "type": "boolean", "label": "Transmitter Linked",
                        "cloud_priority": "high",
                        "help": "A transmitter is being received on this channel.",
                    },
                    "no_link": {
                        "type": "boolean", "label": "No Link", "cloud_priority": "high",
                        "help": "No transmitter is being received (the inverse of "
                                "Transmitter Linked, named as the EW-DX driver names it).",
                    },
                    "tx_model": {
                        "type": "string", "label": "Transmitter Model",
                        "help": "AD1, AD2, ADX1, ADX1M, ADX2 or ADX2FD; empty when no "
                                "transmitter is received.",
                    },
                    "tx_name": {
                        "type": "string", "label": "Transmitter Name",
                        "help": "The received transmitter's device ID.",
                    },
                    "tx_battery_type": {
                        "type": "string", "label": "Battery Type",
                        "help": "LION, ALKA, NIMH or LITH; empty when unknown.",
                    },
                    "tx_battery_bars": {
                        "type": "integer", "label": "Battery Bars", "min": 0, "max": 5,
                        "help": "Battery level as the receiver's five-bar indicator shows it.",
                    },
                    "tx_battery_percent": {
                        "type": "integer", "label": "Battery Level", "min": 0, "max": 100,
                        "unit": "%", "cloud_priority": "high",
                        "help": "Charge of a rechargeable transmitter battery; not reported "
                                "for disposable cells.",
                    },
                    "tx_battery_minutes": {
                        "type": "integer", "label": "Battery Runtime", "unit": "min",
                        "cloud_priority": "high",
                        "help": "Estimated runtime remaining; unknown while the transmitter "
                                "is calculating it.",
                    },
                    "tx_battery_state": {
                        "type": "enum",
                        "values": ["ok", "calculating", "communication_warning", "unknown"],
                        "label": "Battery Runtime State",
                        "help": "Why the runtime is or is not known: calculating (just "
                                "powered on), communication_warning (check the battery "
                                "contacts) or unknown (no transmitter, or a disposable cell).",
                    },
                    "tx_battery_health_percent": {
                        "type": "integer", "label": "Battery Health", "min": 0, "max": 100,
                        "unit": "%",
                        "help": "Health of a rechargeable transmitter battery.",
                    },
                    "tx_battery_cycles": {
                        "type": "integer", "label": "Battery Cycles",
                        "help": "Charge cycles of a rechargeable transmitter battery.",
                    },
                    "tx_battery_temp_c": {
                        "type": "integer", "label": "Battery Temperature", "unit": "°C",
                    },
                    "tx_low_battery": {
                        "type": "boolean", "label": "Low Battery", "cloud_priority": "high",
                        "help": "The transmitter's battery bars are at or below the Low "
                                "Battery Bars threshold in the device settings.",
                    },
                    "tx_input_pad": {
                        "type": "boolean", "label": "Transmitter Input Pad",
                        "help": "The -12 dB input pad is engaged (AD1 / ADX1 only).",
                    },
                    "tx_offset_db": {
                        "type": "integer", "label": "Transmitter Gain Offset",
                        "min": -12, "max": 21, "unit": "dB",
                    },
                    "tx_polarity": {
                        "type": "enum", "values": ["positive", "negative"],
                        "label": "Transmitter Polarity",
                    },
                    "tx_power_mw": {
                        "type": "integer", "label": "Transmitter RF Power", "unit": "mW",
                        "help": "The transmitter's decoded RF power level (2, 10, 20, 35, 40 "
                                "or 50 mW).",
                    },
                    "tx_lock": {
                        "type": "enum", "values": ["none", "power", "menu", "all"],
                        "label": "Transmitter Lock",
                        "help": "Which transmitter controls are locked.",
                    },
                    "tx_mute": {
                        "type": "boolean", "label": "Transmitter Mute", "cloud_priority": "high",
                        "help": "The transmitter's power switch is in the mute position "
                                "(audio muted at the transmitter, RF still on).",
                    },
                    "tx_talk_switch": {
                        "type": "boolean", "label": "Talk Switch",
                        "help": "The transmitter's talk switch is pressed.",
                    },
                    # ── Meters (opt-in: Meter Interval, or the meter commands) ──
                    "chan_quality": {
                        "type": "integer", "label": "Channel Quality", "min": 0, "max": 5,
                        "cloud_priority": "low",
                        "help": "The receiver's five-segment channel quality meter.",
                    },
                    "level_peak_dbfs": {
                        "type": "number", "label": "Audio Peak Level", "min": -120, "max": 0,
                        "unit": "dBFS", "cloud_priority": "low",
                    },
                    "level_dbfs": {
                        "type": "number", "label": "Audio Level", "min": -120, "max": 0,
                        "unit": "dBFS", "cloud_priority": "low",
                        "help": "RMS audio level.",
                    },
                    "antenna_status": {
                        "type": "string", "label": "Antenna Status", "cloud_priority": "low",
                        "help": "One letter per antenna (A B, or A B C D in Quadversity): "
                                "B blue (active), R red, X off.",
                    },
                    "rssi_dbm": {
                        "type": "number", "label": "RF Signal Strength", "min": -120, "max": 0,
                        "unit": "dBm", "cloud_priority": "low",
                        "help": "The strongest antenna's RSSI.",
                    },
                    "rssi_a_dbm": {
                        "type": "number", "label": "RSSI A", "min": -120, "max": 0,
                        "unit": "dBm", "cloud_priority": "low",
                    },
                    "rssi_b_dbm": {
                        "type": "number", "label": "RSSI B", "min": -120, "max": 0,
                        "unit": "dBm", "cloud_priority": "low",
                    },
                    "rssi_c_dbm": {
                        "type": "number", "label": "RSSI C", "min": -120, "max": 0,
                        "unit": "dBm", "cloud_priority": "low",
                        "help": "Quadversity only.",
                    },
                    "rssi_d_dbm": {
                        "type": "number", "label": "RSSI D", "min": -120, "max": 0,
                        "unit": "dBm", "cloud_priority": "low",
                        "help": "Quadversity only.",
                    },
                    "antenna_status2": {
                        "type": "string", "label": "Antenna Status 2", "cloud_priority": "low",
                        "help": "The second RF section of an FD-C channel.",
                    },
                    "rssi2_a_dbm": {
                        "type": "number", "label": "RSSI 2A", "min": -120, "max": 0,
                        "unit": "dBm", "cloud_priority": "low",
                    },
                    "rssi2_b_dbm": {
                        "type": "number", "label": "RSSI 2B", "min": -120, "max": 0,
                        "unit": "dBm", "cloud_priority": "low",
                    },
                    "rssi2_c_dbm": {
                        "type": "number", "label": "RSSI 2C", "min": -120, "max": 0,
                        "unit": "dBm", "cloud_priority": "low",
                    },
                    "rssi2_d_dbm": {
                        "type": "number", "label": "RSSI 2D", "min": -120, "max": 0,
                        "unit": "dBm", "cloud_priority": "low",
                    },
                    "rf_peak": {
                        "type": "boolean", "label": "RF Peak", "cloud_priority": "high",
                        "help": "An antenna's red overload / RF pad LED is lit (from the "
                                "meter feed).",
                    },
                    "af_peak": {
                        "type": "boolean", "label": "AF Peak", "cloud_priority": "high",
                        "help": "The channel's red audio overload LED is lit (from the meter "
                                "feed).",
                    },
                },
                "summary_fields": ["mute", "tx_battery_bars", "tx_linked"],
                "label_field": "name",
            },
            "slot": {
                "label": "Transmitter Slot",
                "label_plural": "Transmitter Slots",
                "id_format": {"type": "string", "max_length": 4},
                # The ids are slots, so presence is reported: an empty slot is
                # not_fitted until SLOT_STATUS says something is registered in it.
                "instances": {
                    "ids": [
                        "1-1", "1-2", "1-3", "1-4", "1-5", "1-6", "1-7", "1-8",
                        "2-1", "2-2", "2-3", "2-4", "2-5", "2-6", "2-7", "2-8",
                        "3-1", "3-2", "3-3", "3-4", "3-5", "3-6", "3-7", "3-8",
                        "4-1", "4-2", "4-3", "4-4", "4-5", "4-6", "4-7", "4-8",
                    ],
                    "presence": "reported",
                },
                "state_variables": {
                    "channel": {"type": "integer", "label": "Channel", "min": 1, "max": 4},
                    "slot_number": {"type": "integer", "label": "Slot", "min": 1, "max": 8},
                    "status": {
                        "type": "enum",
                        "values": ["empty", "standard", "linked_inactive", "linked_active"],
                        "label": "Slot Status", "cloud_priority": "high",
                        "help": "empty; standard (an AD transmitter is registered); "
                                "linked_inactive (an ADX transmitter is linked but off or "
                                "out of range); linked_active (an ADX transmitter is linked "
                                "and can be controlled from here).",
                    },
                    "tx_model": {"type": "string", "label": "Transmitter Model"},
                    "tx_name": {
                        "type": "string", "label": "Transmitter Name",
                        "help": "The registered transmitter's device ID.",
                    },
                    "battery_type": {"type": "string", "label": "Battery Type"},
                    "battery_bars": {
                        "type": "integer", "label": "Battery Bars", "min": 0, "max": 5,
                    },
                    "battery_percent": {
                        "type": "integer", "label": "Battery Level", "min": 0, "max": 100,
                        "unit": "%", "cloud_priority": "high",
                    },
                    "battery_minutes": {
                        "type": "integer", "label": "Battery Runtime", "unit": "min",
                        "cloud_priority": "high",
                    },
                    "battery_state": {
                        "type": "enum",
                        "values": ["ok", "calculating", "communication_warning", "unknown"],
                        "label": "Battery Runtime State",
                    },
                    "battery_health_percent": {
                        "type": "integer", "label": "Battery Health", "min": 0, "max": 100,
                        "unit": "%",
                    },
                    "battery_cycles": {"type": "integer", "label": "Battery Cycles"},
                    "input_pad": {
                        "type": "boolean", "label": "Input Pad", "control": True,
                        "help": "The -12 dB input pad (ADX1 only).",
                    },
                    "offset_db": {
                        "type": "integer", "label": "Gain Offset",
                        "min": -12, "max": 21, "step": 1, "unit": "dB",
                        "control": True,
                    },
                    "polarity": {
                        "type": "enum", "values": ["positive", "negative"], "label": "Polarity",
                        "control": True,
                        "help": "ADX1 and ADX1M only.",
                    },
                    "rf_muted": {
                        "type": "boolean", "label": "RF Mute", "control": True,
                        "cloud_priority": "high",
                        "help": "The transmitter's RF output is muted.",
                    },
                    "rf_power_mw": {
                        "type": "integer", "label": "RF Power", "unit": "mW",
                        "help": "The actual transmit power; set it through RF Power Mode.",
                    },
                    "rf_power_mode": {
                        "type": "enum", "values": ["low", "normal", "high"],
                        "label": "RF Power Mode", "control": True,
                        "help": "Some bands and modes do not allow high; the receiver refuses "
                                "it.",
                    },
                    "showlink_quality": {
                        "type": "integer", "label": "ShowLink Quality", "min": 1, "max": 5,
                        "cloud_priority": "low",
                        "help": "Quality of the ShowLink control link to the transmitter.",
                    },
                },
                "summary_fields": ["status", "tx_model", "battery_bars"],
                "label_field": "tx_name",
            },
        },
        "device_settings": {
            "device_name": {
                "type": "string",
                "label": "Device Name",
                "help": "1 to 8 characters: letters, digits, space and "
                        "punctuation.",
                "state_key": "device_name",
                "default": "",
                "regex": r"^[A-Za-z0-9 !\"#$%&'()*+,\-./:;<=>?@\[\\]^_`~]{1,8}$",
                "setup": False,
            },
        },
        "commands": {
            "identify": {
                "label": "Identify Receiver",
                "help": "Flash the receiver's front panel; it stops by itself.",
            },
            "identify_off": {"label": "Stop Identifying"},
            "set_channel_mute": {
                "label": "Set Channel Mute",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"},
                    "mute": {"type": "boolean", "required": True},
                },
            },
            "toggle_channel_mute": {
                "label": "Toggle Channel Mute",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"}},
            },
            "set_channel_gain": {
                "label": "Set Channel Gain",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"},
                    "gain_db": {
                        "type": "integer", "required": True,
                        "min": -18, "max": 42, "unit": "dB",
                        "help": "Gain in dB, -18 to +42 in 1 dB steps.",
                    },
                },
            },
            "step_channel_gain": {
                "label": "Nudge Channel Gain",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"},
                    "delta_db": {
                        "type": "integer", "required": True,
                        "min": -60, "max": 60, "unit": "dB",
                        "help": "Gain change in dB (positive or negative).",
                    },
                },
            },
            "set_channel_name": {
                "label": "Set Channel Name",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"},
                    "name": {
                        "type": "string", "required": True,
                        "pattern": r"^[A-Za-z0-9 !\"#$%&'()*+,\-./:;<=>?@\[\\]^_`~]{1,8}$",
                        "help": "1 to 8 characters: letters, digits, space and "
                                "punctuation.",
                    },
                },
            },
            "set_channel_frequency": {
                "label": "Set Channel Frequency",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"},
                    "frequency_khz": {
                        "type": "integer", "required": True,
                        "min": 470000, "max": 960000, "unit": "kHz",
                        "help": "Carrier frequency in kHz; the receiver refuses "
                                "one outside its band or off its step. Clears "
                                "the group/channel preset.",
                    },
                },
            },
            "set_channel_preset": {
                "label": "Set Channel Group/Channel",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"},
                    "group": {
                        "type": "string", "required": True, "label": "Group",
                        "pattern": r"^[0-9A-Za-z]{1,3}$",
                        "help": "A frequency group from the receiver's band "
                                "and transmission mode.",
                    },
                    "preset": {
                        "type": "string", "required": True,
                        "label": "Channel Number",
                        "pattern": r"^[0-9]{1,3}$",
                        "help": "A channel within that group.",
                    },
                },
                "help": "Tune to a group/channel preset; the receiver reports "
                        "the resulting frequency.",
            },
            "set_channel_frequency2": {
                "label": "Set Channel Frequency 2 (FD-C)",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"},
                    "frequency_khz": {
                        "type": "integer", "required": True,
                        "min": 470000, "max": 960000, "unit": "kHz",
                        "help": "The second carrier of an FD-C channel.",
                    },
                },
            },
            "set_channel_preset2": {
                "label": "Set Channel Group/Channel 2 (FD-C)",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"},
                    "group": {
                        "type": "string", "required": True, "label": "Group",
                        "pattern": r"^[0-9A-Za-z]{1,3}$",
                    },
                    "preset": {
                        "type": "string", "required": True,
                        "label": "Channel Number",
                        "pattern": r"^[0-9]{1,3}$",
                    },
                },
            },
            "identify_channel": {
                "label": "Identify Channel",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"}},
                "help": "Flash the channel's display; it stops by itself.",
            },
            "identify_channel_off": {
                "label": "Stop Identifying Channel",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"}},
            },
            "channel_meters_on": {
                "label": "Channel Meters On",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"},
                    "rate_ms": {
                        "type": "integer", "required": False, "default": 500,
                        "min": 100, "max": 65535, "unit": "ms",
                        "help": "Interval between meter frames.",
                    },
                },
                "help": "Start the channel's audio and RF meter feed.",
            },
            "channel_meters_off": {
                "label": "Channel Meters Off",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                                "label": "Channel"}},
            },
            "set_slot_rf_mute": {
                "label": "Set Transmitter RF Mute",
                "params": {
                    "slot": {"type": "child_id", "child_type": "slot", "required": True,
                             "label": "Transmitter Slot",
                             "help": "Only a slot whose transmitter is linked and active "
                                     "accepts a remote change; the receiver refuses the rest."},
                    "muted": {"type": "boolean", "required": True},
                },
                "help": "Mute or unmute a linked ADX transmitter's RF output.",
            },
            "set_slot_rf_power": {
                "label": "Set Transmitter RF Power",
                "params": {
                    "slot": {"type": "child_id", "child_type": "slot", "required": True,
                             "label": "Transmitter Slot",
                             "help": "Only a slot whose transmitter is linked and active "
                                     "accepts a remote change; the receiver refuses the rest."},
                    "mode": {
                        "type": "enum", "values": ["low", "normal", "high"],
                        "required": True, "label": "Power Mode",
                    },
                },
            },
            "set_slot_offset": {
                "label": "Set Transmitter Gain Offset",
                "params": {
                    "slot": {"type": "child_id", "child_type": "slot", "required": True,
                             "label": "Transmitter Slot",
                             "help": "Only a slot whose transmitter is linked and active "
                                     "accepts a remote change; the receiver refuses the rest."},
                    "offset_db": {
                        "type": "integer", "required": True,
                        "min": -12, "max": 21,
                        "unit": "dB",
                    },
                },
            },
            "step_slot_offset": {
                "label": "Nudge Transmitter Gain Offset",
                "params": {
                    "slot": {"type": "child_id", "child_type": "slot", "required": True,
                             "label": "Transmitter Slot",
                             "help": "Only a slot whose transmitter is linked and active "
                                     "accepts a remote change; the receiver refuses the rest."},
                    "delta_db": {
                        "type": "integer", "required": True,
                        "min": -33, "max": 33, "unit": "dB",
                    },
                },
            },
            "set_slot_polarity": {
                "label": "Set Transmitter Polarity",
                "params": {
                    "slot": {"type": "child_id", "child_type": "slot", "required": True,
                             "label": "Transmitter Slot",
                             "help": "Only a slot whose transmitter is linked and active "
                                     "accepts a remote change; the receiver refuses the rest."},
                    "polarity": {
                        "type": "enum", "values": ["positive", "negative"],
                        "required": True,
                    },
                },
                "help": "ADX1 and ADX1M only.",
            },
            "set_slot_input_pad": {
                "label": "Set Transmitter Input Pad",
                "params": {
                    "slot": {"type": "child_id", "child_type": "slot", "required": True,
                             "label": "Transmitter Slot",
                             "help": "Only a slot whose transmitter is linked and active "
                                     "accepts a remote change; the receiver refuses the rest."},
                    "enabled": {"type": "boolean", "required": True},
                },
                "help": "Engage or release the -12 dB input pad (ADX1 only).",
            },
            "set_slot_tx_name": {
                "label": "Set Transmitter Name",
                "params": {
                    "slot": {"type": "child_id", "child_type": "slot", "required": True,
                             "label": "Transmitter Slot",
                             "help": "Only a slot whose transmitter is linked and active "
                                     "accepts a remote change; the receiver refuses the rest."},
                    "name": {
                        "type": "string", "required": True,
                        "pattern": r"^[A-Za-z0-9 !\"#$%&'()*+,\-./:;<=>?@\[\\]^_`~]{1,8}$",
                        "help": "1 to 8 characters: letters, digits, space and "
                                "punctuation.",
                    },
                },
            },
            "raw_command": {
                "label": "Raw Command",
                "params": {
                    "command": {
                        "type": "string", "required": True,
                        "label": "Command String",
                        "help": "A raw command string, e.g. < GET 1 RSSI 0 >.",
                    },
                },
                "help": "Send a raw command string for anything this driver "
                        "does not model (network settings, for instance).",
            },
        },
        "quick_actions": ["identify", "identify_off"],
        "actions": [
            {"id": "identify", "kind": "command", "icon": "sun"},
        ],
    }

    HEALTH_FAULT_MESSAGE = (
        "Connected, but the receiver stopped answering MODEL probes."
    )

    # `< REP x PROP value... >` after the frame's "<" and ">" are stripped.
    _RE_REP = re.compile(r"^REP\s+(?:(\d+)\s+)?([A-Z0-9_]+)\s*(.*)$", re.S)
    _RE_SAMPLE = re.compile(r"^SAMPLE\s+(\d+)\s+ALL\s+(.*)$", re.S)
    _RE_ANTENNA = re.compile(r"^[XRB]{1,4}$")

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._probe_fut: asyncio.Future[None] | None = None
        count = int(config.get("channel_count", DEFAULT_CHANNEL_COUNT))
        self._channel_count = max(1, min(MAX_CHANNELS, count))
        self._meter_ms = max(0, int(config.get("meter_interval_ms", 0)))
        self._low_bars = max(0, min(5, int(config.get("low_battery_bars", 1))))
        # Battery bars per channel, for the low-battery derivation.
        self._bars: dict[int, int | None] = {}
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        if not self.config.get("host", ""):
            raise ConnectionError(f"[{self.device_id}] Receiver host is required")

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # Every reply ends at its closing ">"; frame on that, not on CR.
        kwargs["delimiter"] = b">"
        return kwargs

    async def _initial_sync(self) -> None:
        self._register_topology()
        await self._sync_all()

    async def _close_session(self) -> None:
        fut = self._probe_fut
        if fut is not None and not fut.done():
            fut.set_exception(ConnectionError("connection closed"))
        self._probe_fut = None

    # ── Topology ──

    def _register_topology(self) -> None:
        """Register the channel roster and its slots; idempotent, and a
        placeholder label never overrides one the user set in the project."""
        for n in range(1, self._channel_count + 1):
            initial = None
            project = self._project_child_entities.get("channel", {}).get(str(n))
            if not (project and project.get("label")):
                initial = {"label": f"Channel {n}"}
            self.register_child("channel", n, initial_state=initial)
            for s in range(1, SLOTS_PER_CHANNEL + 1):
                sid = slot_id(n, s)
                seed: dict[str, Any] = {"channel": n, "slot_number": s}
                project = self._project_child_entities.get("slot", {}).get(sid)
                if not (project and project.get("label")):
                    seed["label"] = f"Channel {n} Slot {s}"
                self.register_child("slot", sid, initial_state=seed)
        for existing in list(self.list_children("channel")):
            if not (1 <= int(existing) <= self._channel_count):
                self.deregister_child("channel", existing)
        for existing in list(self.list_children("slot")):
            chan = int(str(existing).split("-", 1)[0])
            if not (1 <= chan <= self._channel_count):
                self.deregister_child("slot", existing)

    async def refresh_children(self) -> dict[str, Any]:
        """IDE 'Refresh from Device': re-register and re-read everything."""
        if not (self.transport and self.transport.connected):
            raise ConnectionError(f"[{self.device_id}] Not connected")
        self._register_topology()
        await self._sync_all()
        return {
            "channel": self._channel_count,
            "slot": self._channel_count * SLOTS_PER_CHANNEL,
        }

    async def _resize_roster(self, count: int) -> None:
        """The receiver's MODEL said how many channels it has."""
        self.set_state("channel_count_reported", count)
        if count == self._channel_count:
            return
        old = self._channel_count
        self._channel_count = count
        self._register_topology()
        try:
            for n in range(old + 1, count + 1):
                await self._send(f"< GET {n} ALL >")
                await self._query_slots(n)
                if self._meter_ms > 0:
                    await self._send(f"< SET {n} METER_RATE {self._meter_ms:05d} >")
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Roster resync failed")

    # ── Sync ──

    async def _sync_all(self) -> None:
        """Seed device, channel and slot state on connect / refresh."""
        try:
            # GET 0 ALL dumps every device and channel property, including
            # the metered ones, in one round trip. The slot properties are
            # asked for explicitly (slot index 0 = every slot).
            await self._send("< GET 0 ALL >")
            await self._send("< GET NET_SETTINGS SC >")
            for n in range(1, self._channel_count + 1):
                await self._query_slots(n)
            if self._meter_ms > 0:
                for n in range(1, self._channel_count + 1):
                    await self._send(f"< SET {n} METER_RATE {self._meter_ms:05d} >")
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial sync failed")

    async def _query_slots(self, channel: int) -> None:
        for prop in ("SLOT_STATUS", "SLOT_TX_MODEL", "SLOT_TX_DEVICE_ID",
                     "SLOT_BATT_TYPE", "SLOT_BATT_BARS",
                     "SLOT_BATT_CHARGE_PERCENT", "SLOT_BATT_MINS",
                     "SLOT_BATT_HEALTH_PERCENT", "SLOT_BATT_CYCLE_COUNT",
                     "SLOT_INPUT_PAD", "SLOT_OFFSET", "SLOT_POLARITY",
                     "SLOT_RF_OUTPUT", "SLOT_RF_POWER", "SLOT_RF_POWER_MODE",
                     "SLOT_SHOWLINK_STATUS"):
            await self._send(f"< GET {channel} {prop} 0 >")

    async def poll(self) -> None:
        # The receiver pushes changes; this is the device-level baseline.
        for prop in ("ENCRYPTION_MODE", "QUADVERSITY_MODE",
                     "TRANSMISSION_MODE", "RF_BAND", "FW_VER"):
            await self._send(f"< GET {prop} >")

    # ── Sending ──

    async def _send(self, message: str) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(message.encode("ascii"))

    async def _liveness_probe(self) -> None:
        """Send `< GET MODEL >` and await its REP. A channel push or a meter
        frame cannot resolve it, so it proves the link is alive."""
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._probe_fut = fut
        try:
            await self._send("< GET MODEL >")
            await fut
        finally:
            self._probe_fut = None

    # ── Commands ──

    @staticmethod
    def _slot_addr(params: dict[str, Any]) -> tuple[int, int]:
        chan, _, slot = str(params["slot"]).partition("-")
        return int(chan), int(slot)

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "identify":
            await self._send("< SET FLASH ON >")
        elif command == "identify_off":
            await self._send("< SET FLASH OFF >")
        elif command == "set_channel_mute":
            ch = int(params["channel"])
            await self._send(
                f"< SET {ch} AUDIO_MUTE {'ON' if params['mute'] else 'OFF'} >")
        elif command == "toggle_channel_mute":
            ch = int(params["channel"])
            await self._send(f"< SET {ch} AUDIO_MUTE TOGGLE >")
        elif command == "set_channel_gain":
            ch = int(params["channel"])
            wire = int(params["gain_db"]) + _GAIN_OFFSET
            await self._send(f"< SET {ch} AUDIO_GAIN {wire} >")
        elif command == "step_channel_gain":
            ch = int(params["channel"])
            delta = int(params["delta_db"])
            verb = "INC" if delta >= 0 else "DEC"
            await self._send(f"< SET {ch} AUDIO_GAIN {verb} {abs(delta)} >")
        elif command == "set_channel_name":
            ch = int(params["channel"])
            await self._send(f"< SET {ch} CHAN_NAME {{{params['name']}}} >")
        elif command == "set_channel_frequency":
            ch = int(params["channel"])
            await self._send(
                f"< SET {ch} FREQUENCY {int(params['frequency_khz'])} >")
        elif command == "set_channel_frequency2":
            ch = int(params["channel"])
            await self._send(
                f"< SET {ch} FREQUENCY2 {int(params['frequency_khz'])} >")
        elif command == "set_channel_preset":
            ch = int(params["channel"])
            await self._send(
                f"< SET {ch} GROUP_CHANNEL {{{params['group']},{params['preset']}}} >")
        elif command == "set_channel_preset2":
            ch = int(params["channel"])
            await self._send(
                f"< SET {ch} GROUP_CHANNEL2 {{{params['group']},{params['preset']}}} >")
        elif command == "identify_channel":
            ch = int(params["channel"])
            await self._send(f"< SET {ch} FLASH ON >")
        elif command == "identify_channel_off":
            ch = int(params["channel"])
            await self._send(f"< SET {ch} FLASH OFF >")
        elif command == "channel_meters_on":
            ch = int(params["channel"])
            rate = int(params.get("rate_ms") or 500)
            await self._send(f"< SET {ch} METER_RATE {rate:05d} >")
        elif command == "channel_meters_off":
            ch = int(params["channel"])
            await self._send(f"< SET {ch} METER_RATE 00000 >")
        elif command == "set_slot_rf_mute":
            ch, slot = self._slot_addr(params)
            value = "RF_MUTE" if params["muted"] else "RF_ON"
            await self._send(f"< SET {ch} SLOT_RF_OUTPUT {slot} {value} >")
        elif command == "set_slot_rf_power":
            ch, slot = self._slot_addr(params)
            await self._send(
                f"< SET {ch} SLOT_RF_POWER_MODE {slot} {str(params['mode']).upper()} >")
        elif command == "set_slot_offset":
            ch, slot = self._slot_addr(params)
            wire = int(params["offset_db"]) + _OFFSET_OFFSET
            await self._send(f"< SET {ch} SLOT_OFFSET {slot} {wire} >")
        elif command == "step_slot_offset":
            ch, slot = self._slot_addr(params)
            delta = int(params["delta_db"])
            verb = "INC" if delta >= 0 else "DEC"
            await self._send(
                f"< SET {ch} SLOT_OFFSET {slot} {verb} {abs(delta)} >")
        elif command == "set_slot_polarity":
            ch, slot = self._slot_addr(params)
            await self._send(
                f"< SET {ch} SLOT_POLARITY {slot} {str(params['polarity']).upper()} >")
        elif command == "set_slot_input_pad":
            ch, slot = self._slot_addr(params)
            wire = 0 if params["enabled"] else _OFFSET_OFFSET
            await self._send(f"< SET {ch} SLOT_INPUT_PAD {slot} {wire} >")
        elif command == "set_slot_tx_name":
            ch, slot = self._slot_addr(params)
            await self._send(
                f"< SET {ch} SLOT_TX_DEVICE_ID {slot} {{{params['name']}}} >")
        elif command == "raw_command":
            await self._send(str(params["command"]))
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "device_name":
            await self._send(f"< SET DEVICE_ID {{{value}}} >")
            return
        raise ValueError(f"[{self.device_id}] Unknown device setting: {key}")

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        # The transport frames on ">", delivering one message per call; split
        # defensively in case a chunk carries more than one.
        for part in data.split(b">"):
            self._handle_frame(part)

    def _handle_frame(self, raw: bytes) -> None:
        text = raw.decode("ascii", errors="replace").strip()
        text = text.lstrip("<").strip()
        if not text:
            return
        if text.upper().startswith("REP ERR"):
            log.debug(f"[{self.device_id}] Receiver refused a command (REP ERR)")
            return

        m = self._RE_SAMPLE.match(text)
        if m:
            self._apply_sample(int(m.group(1)), m.group(2))
            return

        m = self._RE_REP.match(text)
        if not m:
            return
        index, prop, rest = m.group(1), m.group(2).upper(), m.group(3).strip()
        if index is None:
            self._apply_device(prop, rest)
        else:
            self._apply_channel(int(index), prop, rest)

    # ── Device-level reports ──

    def _apply_device(self, prop: str, rest: str) -> None:
        if prop == "MODEL":
            model = _strip_string_value(rest)
            self.set_state("model", model)
            fut = self._probe_fut
            if fut is not None and not fut.done():
                fut.set_result(None)
            count = _channel_count_for_model(model)
            if count is not None:
                self._spawn(self._resize_roster(count))
        elif prop == "DEVICE_ID":
            self.set_state("device_name", _strip_string_value(rest))
        elif prop == "FW_VER":
            version = _strip_string_value(rest)
            self.set_state("selftest_failed", version.endswith("*"))
            self.set_state("firmware", version.rstrip("*"))
        elif prop == "RF_BAND":
            self.set_state("rf_band", _strip_string_value(rest))
        elif prop == "ENCRYPTION_MODE":
            self.set_state("encryption_enabled", rest.upper() == "ON")
        elif prop == "QUADVERSITY_MODE":
            self.set_state("quadversity", rest.upper() == "ON")
        elif prop == "TRANSMISSION_MODE":
            self.set_state("transmission_mode", rest.strip().lower())
        elif prop == "FLASH":
            self.set_state("identifying", rest.upper() == "ON")
        elif prop == "NET_SETTINGS":
            tokens = rest.split()
            if len(tokens) >= 6 and tokens[0].upper() == "SC":
                ip = ".".join(str(int(o)) for o in tokens[2].split(".")
                              if o.isdigit())
                self.set_state("ip_address", ip)
                self.set_state("mac_address", tokens[5].upper())

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        tasks = getattr(self, "_bg_tasks", None)
        if tasks is None:
            tasks = self._bg_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    # ── Channel-level reports ──

    def _apply_channel(self, ch: int, prop: str, rest: str) -> None:
        if not (1 <= ch <= self._channel_count):
            return
        if prop.startswith("SLOT_"):
            self._apply_slot(ch, prop, rest)
            return
        tokens = rest.split()
        first = tokens[0] if tokens else ""
        upper = first.upper()
        updates: dict[str, Any] = {}

        if prop == "AUDIO_MUTE":
            updates["mute"] = upper == "ON"
        elif prop == "AUDIO_GAIN":
            raw = _int3(first)
            if raw is not None:
                updates["gain"] = raw - _GAIN_OFFSET
        elif prop == "CHAN_NAME":
            updates["name"] = _strip_string_value(rest)
        elif prop == "FREQUENCY":
            if first.isdigit():
                updates["frequency_khz"] = int(first)
        elif prop == "FREQUENCY2":
            if first.isdigit():
                updates["frequency2_khz"] = int(first)
        elif prop == "GROUP_CHANNEL":
            updates["preset_bank"], updates["preset_channel"] = _group_channel(rest)
        elif prop == "GROUP_CHANNEL2":
            updates["preset_bank2"], updates["preset_channel2"] = _group_channel(rest)
        elif prop == "FD_MODE":
            updates["fd_mode"] = _FD_MODES.get(upper, "off")
        elif prop == "ENCRYPTION_STATUS":
            updates["aes256_error"] = upper == "ERROR"
        elif prop == "INTERFERENCE_STATUS":
            updates["interference"] = upper == "DETECTED"
        elif prop == "INTERFERENCE_STATUS2":
            updates["interference2"] = upper == "DETECTED"
        elif prop == "UNREGISTERED_TX_STATUS":
            updates["unregistered_tx"] = upper == "ERROR"
        elif prop == "FLASH":
            updates["identifying"] = upper == "ON"
        elif prop == "METER_RATE":
            if first.isdigit():
                rate = int(first)
                updates["meter_rate_ms"] = rate
                updates["metering"] = rate > 0
        # ── Transmitter side channel ──
        elif prop == "TX_MODEL":
            linked = upper not in ("", "UNKNOWN")
            updates["tx_model"] = first if linked else ""
            updates["tx_linked"] = linked
            updates["no_link"] = not linked
        elif prop == "TX_DEVICE_ID":
            updates["tx_name"] = _strip_string_value(rest)
        elif prop == "TX_BATT_TYPE":
            updates["tx_battery_type"] = "" if upper == "UNKN" else upper
        elif prop == "TX_BATT_BARS":
            bars = _int3(first)
            self._bars[ch] = bars
            updates["tx_battery_bars"] = bars
            updates["tx_low_battery"] = (
                bars is not None and self._low_bars > 0 and bars <= self._low_bars
            )
        elif prop == "TX_BATT_CHARGE_PERCENT":
            updates["tx_battery_percent"] = _int3(first)
        elif prop == "TX_BATT_MINS":
            updates["tx_battery_minutes"], updates["tx_battery_state"] = _minutes(first)
        elif prop == "TX_BATT_HEALTH_PERCENT":
            updates["tx_battery_health_percent"] = _int3(first)
        elif prop == "TX_BATT_CYCLE_COUNT":
            updates["tx_battery_cycles"] = _cycles(first)
        elif prop == "TX_BATT_TEMP_C":
            raw = _int3(first)
            updates["tx_battery_temp_c"] = None if raw is None else raw - _TEMP_OFFSET
        elif prop == "TX_INPUT_PAD":
            updates["tx_input_pad"] = _input_pad(first)
        elif prop == "TX_OFFSET":
            updates["tx_offset_db"] = _offset_db(first)
        elif prop == "TX_POLARITY":
            updates["tx_polarity"] = (
                first.lower() if upper in ("POSITIVE", "NEGATIVE") else None)
        elif prop == "TX_POWER_LEVEL":
            updates["tx_power_mw"] = _int3(first)
        elif prop == "TX_LOCK":
            updates["tx_lock"] = (
                first.lower() if upper in ("NONE", "POWER", "MENU", "ALL") else None)
        elif prop == "TX_MUTE_MODE_STATUS":
            updates["tx_mute"] = (
                None if upper not in ("ON", "MUTE") else upper == "MUTE")
        elif prop == "TX_TALK_SWITCH":
            updates["tx_talk_switch"] = (
                None if upper not in ("ON", "OFF") else upper == "ON")
        # ── Metered properties answered by a GET (or a GET ALL dump) ──
        elif prop == "AUDIO_LEVEL_PEAK":
            updates["level_peak_dbfs"] = _level(first)
        elif prop == "AUDIO_LEVEL_RMS":
            updates["level_dbfs"] = _level(first)
        elif prop == "CHAN_QUALITY":
            updates["chan_quality"] = _int3(first)
        elif prop == "ANTENNA_STATUS":
            updates["antenna_status"] = upper
        elif prop == "AUDIO_LED_BITMAP":
            if first.isdigit():
                updates["af_peak"] = bool(int(first) & _AUDIO_OVERLOAD_BIT)
        elif prop == "RSSI":
            if len(tokens) >= 2 and tokens[0].isdigit():
                key = self._rssi_key(int(tokens[0]))
                if key:
                    updates[key] = _level(tokens[1])
        elif prop == "RSSI_LED_BITMAP":
            # Per-antenna; a GET answers one line per antenna, so a single
            # line can only raise the flag, never clear it (SAMPLE does both).
            if len(tokens) >= 2 and tokens[1].isdigit():
                if int(tokens[1]) & _RSSI_OVERLOAD_BIT:
                    updates["rf_peak"] = True
        else:
            return

        if updates:
            self.set_child_state_batch("channel", ch, updates)
            if "rssi_a_dbm" in updates or "rssi_b_dbm" in updates \
                    or "rssi_c_dbm" in updates or "rssi_d_dbm" in updates:
                self._update_best_rssi(ch)

    @staticmethod
    def _rssi_key(antenna: int, section: int = 1) -> str | None:
        letters = {1: "a", 2: "b", 3: "c", 4: "d"}
        letter = letters.get(antenna)
        if letter is None:
            return None
        return f"rssi_{letter}_dbm" if section == 1 else f"rssi2_{letter}_dbm"

    def _update_best_rssi(self, ch: int) -> None:
        current = self.get_child_state("channel", ch)
        values = [
            current.get(k) for k in ("rssi_a_dbm", "rssi_b_dbm",
                                     "rssi_c_dbm", "rssi_d_dbm")
        ]
        values = [v for v in values if isinstance(v, (int, float))]
        self.set_child_state("channel", ch, "rssi_dbm",
                             max(values) if values else None)

    # ── Slot reports ──

    def _apply_slot(self, ch: int, prop: str, rest: str) -> None:
        tokens = rest.split()
        if len(tokens) < 2 or not tokens[0].isdigit():
            return
        slot = int(tokens[0])
        if not (1 <= slot <= SLOTS_PER_CHANNEL):
            return
        sid = slot_id(ch, slot)
        value_text = rest.split(None, 1)[1] if len(rest.split(None, 1)) > 1 else ""
        first = tokens[1]
        upper = first.upper()
        updates: dict[str, Any] = {}

        if prop == "SLOT_STATUS":
            status = _SLOT_STATUS.get(upper)
            if status is None:
                return
            updates["status"] = status
            if status == "empty":
                updates.update(self.child_fault(
                    CHILD_NOT_FITTED, "No transmitter is registered in this slot"))
                updates.update({
                    "tx_model": "", "tx_name": "", "battery_type": "",
                    "battery_bars": None, "battery_percent": None,
                    "battery_minutes": None, "battery_state": "unknown",
                    "battery_health_percent": None, "battery_cycles": None,
                    "input_pad": None, "offset_db": None, "polarity": None,
                    "rf_muted": None, "rf_power_mw": None,
                    "rf_power_mode": None, "showlink_quality": None,
                })
            elif status == "linked_inactive":
                updates.update(self.child_fault(
                    CHILD_NOT_RESPONDING,
                    "The linked transmitter is off or out of range"))
            else:
                updates.update(self.child_fault())
        elif prop == "SLOT_TX_MODEL":
            updates["tx_model"] = "" if upper == "UNKNOWN" else first
        elif prop == "SLOT_TX_DEVICE_ID":
            updates["tx_name"] = _strip_string_value(value_text)
        elif prop == "SLOT_BATT_TYPE":
            updates["battery_type"] = "" if upper == "UNKN" else upper
        elif prop == "SLOT_BATT_BARS":
            updates["battery_bars"] = _int3(first)
        elif prop == "SLOT_BATT_CHARGE_PERCENT":
            updates["battery_percent"] = _int3(first)
        elif prop == "SLOT_BATT_MINS":
            updates["battery_minutes"], updates["battery_state"] = _minutes(first)
        elif prop == "SLOT_BATT_HEALTH_PERCENT":
            updates["battery_health_percent"] = _int3(first)
        elif prop == "SLOT_BATT_CYCLE_COUNT":
            updates["battery_cycles"] = _cycles(first)
        elif prop == "SLOT_INPUT_PAD":
            updates["input_pad"] = _input_pad(first)
        elif prop == "SLOT_OFFSET":
            updates["offset_db"] = _offset_db(first)
        elif prop == "SLOT_POLARITY":
            updates["polarity"] = (
                first.lower() if upper in ("POSITIVE", "NEGATIVE") else None)
        elif prop == "SLOT_RF_OUTPUT":
            updates["rf_muted"] = (
                None if upper not in ("RF_ON", "RF_MUTE") else upper == "RF_MUTE")
        elif prop == "SLOT_RF_POWER":
            updates["rf_power_mw"] = _int3(first)
        elif prop == "SLOT_RF_POWER_MODE":
            updates["rf_power_mode"] = (
                first.lower() if upper in ("LOW", "NORMAL", "HIGH") else None)
        elif prop == "SLOT_SHOWLINK_STATUS":
            updates["showlink_quality"] = _int3(first)
        else:
            return
        self.set_child_state_batch("slot", sid, updates)

    # ── SAMPLE frames ──

    def _apply_sample(self, ch: int, body: str) -> None:
        """Fan a SAMPLE frame out into the channel's meter keys.

        ``qual audBitmap audPeak audRms`` come first, then one RF section per
        carrier: an antenna-status token (one letter per antenna: two, or
        four in Quadversity) followed by an LED bitmap and an RSSI per
        antenna. An FD-C channel carries two sections.
        """
        if not (1 <= ch <= self._channel_count):
            return
        tokens = body.split()
        if len(tokens) < 5:
            return
        updates: dict[str, Any] = {
            "chan_quality": _int3(tokens[0]),
            "level_peak_dbfs": _level(tokens[2]),
            "level_dbfs": _level(tokens[3]),
        }
        if tokens[1].isdigit():
            updates["af_peak"] = bool(int(tokens[1]) & _AUDIO_OVERLOAD_BIT)

        rf_peak = False
        section = 0
        i = 4
        while i < len(tokens) and section < 2:
            status = tokens[i].upper()
            if not self._RE_ANTENNA.match(status):
                break
            section += 1
            count = len(status)
            updates["antenna_status" if section == 1 else "antenna_status2"] = status
            i += 1
            for antenna in range(1, count + 1):
                if i + 1 >= len(tokens):
                    break
                bitmap, rssi = tokens[i], tokens[i + 1]
                i += 2
                if bitmap.isdigit() and int(bitmap) & _RSSI_OVERLOAD_BIT:
                    rf_peak = True
                key = self._rssi_key(antenna, section)
                if key:
                    updates[key] = _level(rssi)
        if section:
            updates["rf_peak"] = rf_peak
        self.set_child_state_batch("channel", ch, updates)
        self._update_best_rssi(ch)
