"""
OpenAVC Sennheiser SpeechLine Multi-Channel Receiver DW driver.

Controls and monitors the SL MCR 2 DW and SL MCR 4 DW (the SpeechLine Digital
Wireless multi-channel receivers) over Sennheiser Sound Control (SSC) version
1: JSON objects over UDP port 45, one object per datagram.

Protocol summary (Sennheiser TI 1094 v4.3, chapters 4-5 and 10-12):
    Get     : ``{"rx1":{"name":null}}``        -> ``{"rx1":{"name":"rx1"}}``
    Set     : ``{"audio":{"rx1":{"gain":6}}}`` -> ``{"audio":{"rx1":{"gain":6}}}``
    Error   : ``{"osc":{"error":[{"audio":{"rx1":{"gain":[406,{"desc":..}]}}}]}}``
    Ping    : ``{"osc":{"ping":null}}``        -> the same back
    - Every address is a path of JSON keys; a null argument reads it, any
      other value writes it, and the reply carries the value now in force.
    - ``rx<n>`` is receiver channel n, ``mates/tx<n>`` the transmitter
      linked to it, ``audio/rx<n>`` its audio settings and ``m/rx<n>`` its
      meters (n = 1-2 on the SL MCR 2 DW, 1-4 on the SL MCR 4 DW).

State model:
    - Device-level state carries identity, names, network, the Dante and
      analog output gains, the receiver's state and warnings, and standby.
    - ``channel`` child entities (1..N, N read from the product name the
      receiver reports; the Channel Count setting is the fallback before the
      first connection) fold ``rx<n>``, ``audio/rx<n>``, ``m/rx<n>`` and
      ``mates/tx<n>`` together: name, mute, gain, EQ, low cut, mute mode,
      link state, pairing, walk test, RF quality, warnings, the linked
      transmitter's type, capsule, battery, charging, locks and LED, and the
      opt-in meters. The names follow ``sennheiser_ewdx`` where the concept
      matches (name, mute, gain, identifying, level_dbfs, tx_linked,
      no_link, tx_type, tx_capsule, tx_led, tx_battery_type,
      tx_battery_minutes, tx_battery_percent, tx_low_battery), so one panel
      template serves both receiver families.

Push vs poll: push. SSC subscriptions (``/osc/state/subscribe``) make the
receiver send every value change itself. A subscription lasts a lifetime the
client asks for (10 s by default) and at most 1000 notifications, then ends
with error 310; it is renewed by sending the same request again. The driver
subscribes in small groups, one address tree per request (the only form an
SSC server must accept), with a lifetime of three poll intervals and at least
60 s, and the poll renews them. A 310 resubscribes the group it ended. The
transmitter's values are subscribed only while a transmitter is linked to the
channel, the way the document's pre-conditions describe them. Values that
cannot be subscribed (identity, MAC address, Dante version, standby, the
transmitter's switch label) are read on connect and on a new link.

Python (not YAML) because the document's own bundled replies repeat a key at
one level (``{"rx1":{"mute_mode":"ON"},"rx1":{"mute_state":false},
"mates":{..},"mates":{..}}``). A JSON reader keeps only the last value of a
repeated key, so a YAML ``json: true`` rule would lose all but one value of
every such notification; this driver merges the repeats. One of the
document's subscription-end examples is not valid JSON at all, so a reply
that cannot be parsed is still checked for an error 310. The subscription
groups, their renewal and the per-channel transmitter link also have no
declarative form.

Standby: ``/device/standby`` sends the receiver into standby, which only a
Wake-on-LAN packet ends. The driver keeps the device online while it knows
the receiver is in standby (the keep-alive ping is not sent and the poll asks
only for the standby state), and Wake is ``available_offline`` so it also
wakes a receiver the driver has lost. The MAC address is read from the
receiver and saved into the MAC Address setting.

Liveness: an awaited ``/osc/ping`` (the protocol's own keep-alive, §11.14).

Source: https://assets.sennheiser.com/download/assets/TI_1094_v4.3_Sennheiser_Sound_Control_Protocol_SL_DW_EN.pdf/b2a3c92ce7bb11f088db768ee8e97aea
(Sennheiser, "Sennheiser Sound Control Protocol (SSC) - Developer's guide
for SpeechLine Digital Wireless", TI 1094 v4.3, SL MCR DW firmware 4.1.4).
No hardware was available for this build: every claim here is the
document's, verified in the simulator only.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError, WakeAddressError
except ImportError:  # OpenAVC before 0.37.1 has neither typed refusal
    CommandRefusedError = RuntimeError
    WakeAddressError = None

log = get_logger(__name__)


DEFAULT_CHANNEL_COUNT = 4

# Channel, Dante and analog output gains: -24..+12 dB in 6 dB steps (§11.90,
# §11.102, §11.103). The receiver refuses a value off the range (406).
_GAIN_MIN = -24
_GAIN_MAX = 12
_GAIN_STEP = 6

# A subscription lives this long unless the poll renews it first: three poll
# intervals, never under a minute (§5.1 default is 10 s).
_MIN_LIFETIME_S = 60
# A group ended by a 310 is not asked for again sooner than this.
_RESUBSCRIBE_GAP_S = 2.0
# The whole set is not resubscribed more often than this.
_FULL_RESUBSCRIBE_GAP_S = 5.0
# How long connect waits for the product name before sizing the roster.
_IDENTITY_WAIT_S = 2.0
# An error reply naming an address the driver wrote this recently is the
# answer to that write, so it is shown as Last Error.
_WRITE_ERROR_WINDOW_S = 10.0
# After a restart is announced or commanded, an answer this soon is still the
# receiver on its way down, so it does not yet count as the receiver back.
_RESTART_GRACE_S = 5.0
# After Wake, the standby state is asked for this often, for this long.
_WAKE_CHECK_EVERY_S = 5.0
_WAKE_CHECK_FOR_S = 120.0
_DEFAULT_METER_MS = 500

_MAC_FIELD_LABEL = "MAC Address (for Wake-on-LAN)"

_PRODUCT_RE = re.compile(r"^SLDW(\d+)CH$")
_RX_RE = re.compile(r"^rx(\d+)$")
_TX_RE = re.compile(r"^tx(\d+)$")
_TERMINATED_RE = re.compile(r"\[\s*310\b")

# Wire word -> state value, and back.
_LINK_STATES = {
    "NOT_CONNECTED": "not_connected",
    "CONNECTED": "connected",
    "PAIRING": "pairing",
    "FWU_OTA": "transmitter_update",
    "FWU_OTA_CONFIRMATION": "transmitter_update_confirmation",
    "TRANSMITTER_UPDATE_FAILED": "transmitter_update_failed",
    "WALKTEST": "walktest",
}
_MUTE_MODES = {
    "OFF": "off",
    "ON": "on",
    "PUSH_TO_TALK": "push_to_talk",
    "PUSH_TO_MUTE": "push_to_mute",
    "ROOM_MUTE": "room_mute",
}
_EQ_PRESETS = {
    "OFF": "off",
    "FEMALE_SPEECH": "female_speech",
    "MALE_SPEECH": "male_speech",
    "MEDIA": "media",
    "CUSTOM": "custom",
}
_SYNC_INFO = {
    "NOT_SYNCED": "not_synced",
    "MASTER": "master",
    "FOLLOWER": "follower",
    "UNSYNCED_FOLLOWER": "unsynced_follower",
}
_SENSITIVITIES = {
    "AUTOMATIC": "automatic",
    "LEVEL1_0DB": "0db",
    "LEVEL2_6DB": "-6db",
    "LEVEL3_12DB": "-12db",
    "LEVEL4_18DB": "-18db",
    "LEVEL5_24DB": "-24db",
    "LEVEL6_30DB": "-30db",
}
_AUTO_POWER_OFF = {"OFF": "off", "10MIN": "10min", "20MIN": "20min", "30MIN": "30min"}
_TX_TYPES = {
    "HANDHELD": "handheld",
    "BODYPACK": "bodypack",
    "TABLE-STAND": "table_stand",
    "BOUNDARY": "boundary",
}
_BATTERY_TYPES = {"BATTERY": "battery", "RECHARGEABLE": "rechargeable"}
_DEVICE_STATES = {
    "NORMAL": "normal",
    "UPDATE": "update",
    "UPDATE_FAILED": "update_failed",
    "ERROR": "error",
}
_NETWORK_MODES = {"CONFIG1": "single_cable", "CONFIG2": "redundant", "CONFIG3": "split"}
# §11.51: a warning that is not a fault of the receiver.
_DEVICE_NOTICES = {"NONE", "", "SETTINGS_DEFAULTED"}


def _invert(table: dict[str, str]) -> dict[str, str]:
    return {v: k for k, v in table.items()}


_MUTE_MODES_WIRE = _invert(_MUTE_MODES)
_EQ_PRESETS_WIRE = _invert(_EQ_PRESETS)
_SENSITIVITIES_WIRE = _invert(_SENSITIVITIES)
_AUTO_POWER_OFF_WIRE = _invert(_AUTO_POWER_OFF)
_NETWORK_MODES_WIRE = _invert(_NETWORK_MODES)

# The transmitter's values that only mean something while it is linked; the
# rest of mates/tx<n> are the channel's transmitter settings.
_LINKED_TX_KEYS = (
    "acoustic", "bat_bars", "bat_charging", "bat_cycles", "bat_gauge",
    "bat_health", "bat_lifetime", "bat_type", "device_type",
    "gooseneck_state", "warnings", "switch1",
)
_LINKED_TX_CLEARED: dict[str, Any] = {
    "tx_capsule": "", "tx_type": None, "tx_battery_type": None,
    "tx_battery_percent": None, "tx_battery_minutes": None,
    "tx_battery_bars": None, "tx_battery_health_percent": None,
    "tx_battery_cycles": None, "tx_charging": None,
    "tx_gooseneck_attached": None, "tx_switch_label": "",
    "tx_warning": "", "tx_low_battery": False,
}


# ── JSON helpers ──

def _merge(into: dict[str, Any], key: str, value: Any) -> None:
    """Put ``value`` under ``key``, merging an object into one already there."""
    old = into.get(key)
    if isinstance(old, dict) and isinstance(value, dict):
        for k, v in value.items():
            _merge(old, k, v)
    else:
        into[key] = value


def _merge_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that keeps every value of a repeated key.

    TI 1094's bundled replies repeat a key at one level; each repeat holds
    another address under it, so the objects are merged rather than the last
    one kept. A space inside a key is dropped: the document's examples carry
    ``"mdns "`` and ``"bat_ lifetime"``, and no SSC address holds one.
    """
    out: dict[str, Any] = {}
    for key, value in pairs:
        _merge(out, str(key).replace(" ", ""), value)
    return out


def _put(tree: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = tree
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def _tree(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    _put(tree, path, value)
    return tree


def _leaves(obj: Any, prefix: tuple[str, ...] = ()):
    """Every (address, value) in a reply. An empty object is a container
    (an ``/osc/schema`` answer), not a value."""
    if not isinstance(obj, dict):
        return
    for key, value in obj.items():
        path = prefix + (key,)
        if isinstance(value, dict):
            yield from _leaves(value, path)
        else:
            yield path, value


def _error_leaves(obj: Any, prefix: tuple[str, ...] = ()):
    """Every (address, code, text) in an ``/osc/error`` tree.

    An error is ``[code]`` or ``[code, {"desc": .., "reason": ..}]`` at the
    failed address; inside a 210's ``failed_addresses`` the code may stand
    alone (§5.2).
    """
    if isinstance(obj, list):
        if obj and isinstance(obj[0], int) and not isinstance(obj[0], bool):
            info = obj[1] if len(obj) > 1 and isinstance(obj[1], dict) else {}
            desc = str(info.get("desc") or "").strip()
            reason = str(info.get("reason") or "").strip()
            text = f"{desc} ({reason})" if desc and reason else (desc or reason)
            yield prefix, obj[0], text
            return
        for item in obj:
            yield from _error_leaves(item, prefix)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "failed_addresses":
                yield from _error_leaves(value, prefix)
            else:
                yield from _error_leaves(value, prefix + (key,))
        return
    if isinstance(obj, int) and not isinstance(obj, bool) and prefix:
        yield prefix, obj, ""


def _first(value: Any) -> Any:
    """The first entry of a per-interface list, or the value itself."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _mapped(table: dict[str, str], value: Any) -> str | None:
    return table.get(str(value).strip().upper()) if value is not None else None


def _snap_gain(db: int) -> int:
    """A gain the receiver accepts: on the range, on a 6 dB step."""
    db = max(_GAIN_MIN, min(_GAIN_MAX, int(db)))
    return int(round(db / _GAIN_STEP)) * _GAIN_STEP


def _channel_count_for_product(product: str) -> int | None:
    m = _PRODUCT_RE.match(product.strip().upper())
    if not m:
        return None
    count = int(m.group(1))
    return count if count in (2, 4) else None


def _readable(path: tuple[str, ...]) -> str:
    return "/" + "/".join(path)


# ── Subscription groups ──

def _device_group() -> list[tuple[str, ...]]:
    return [
        ("device", "name"), ("device", "dante", "name"),
        ("device", "location"), ("device", "position"), ("device", "system"),
        ("device", "identification", "visual"), ("device", "led", "brightness"),
        ("device", "network", "interface_mapping"),
        ("device", "network", "ipv4", "ipaddr"), ("device", "network", "mdns"),
        ("device", "state"), ("device", "warnings"),
        ("audio", "out1", "mixer", "gain"), ("audio", "dante", "mixer", "gain"),
        ("audio", "out2", "network", "ipv4", "ipaddr"),
    ]


def _rx_group(n: int) -> list[tuple[str, ...]]:
    rx = f"rx{n}"
    return [
        (rx, "name"), (rx, "state"), (rx, "identification", "visual"),
        (rx, "pair", "enable"), (rx, "rf_quality"), (rx, "walktest"),
        (rx, "mute_mode"), (rx, "mute_state"),
        (rx, "master_follower", "sync_info"), (rx, "rfpi"),
        (rx, "last_paired_ipei"), (rx, "warnings"),
        ("mates", f"tx{n}", "active"),
    ]


def _rx_progress_group(n: int) -> list[tuple[str, ...]]:
    # Each has a pre-condition (pairing, a transmitter update), so they are
    # kept apart from the values every channel always has, and subscribed
    # again when the channel enters that state.
    rx = f"rx{n}"
    return [(rx, "pair", "progress"), (rx, "update", "progress")]


# Link states whose progress the receiver reports.
_PROGRESS_STATES = {"pairing", "transmitter_update"}


def _settings_group(n: int) -> list[tuple[str, ...]]:
    tx = f"tx{n}"
    return [
        ("audio", f"rx{n}", "gain"), ("audio", f"rx{n}", "equalizer", "preset"),
        ("audio", f"rx{n}", "low_cut"),
        ("mates", tx, "agc"), ("mates", tx, "led_active"),
        ("mates", tx, "auto_power_off"), ("mates", tx, "power_lock"),
        ("mates", tx, "pairing_button_lock"),
    ]


def _tx_group(n: int) -> list[tuple[str, ...]]:
    tx = f"tx{n}"
    return [
        ("mates", tx, key)
        for key in ("acoustic", "bat_gauge", "bat_type", "device_type", "warnings")
    ]


def _tx_extra_group(n: int) -> list[tuple[str, ...]]:
    # Only some transmitters have these (a rechargeable battery, a table
    # stand), so a refusal of one costs nothing else.
    tx = f"tx{n}"
    return [
        ("mates", tx, key)
        for key in ("bat_bars", "bat_charging", "bat_cycles", "bat_health",
                    "bat_lifetime", "gooseneck_state")
    ]


def _meter_group(count: int) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = [("m", "mixer", "level")]
    for n in range(1, count + 1):
        paths += [("m", f"rx{n}", "level"), ("m", f"rx{n}", "channel_level")]
    return paths


class SennheiserSlMcrDwDriver(BaseDriver):
    """Sennheiser SpeechLine SL MCR 2 DW / SL MCR 4 DW receiver driver."""

    DRIVER_INFO = {
        "id": "sennheiser_sl_mcr_dw",
        "name": "Sennheiser SpeechLine Multi-Channel Receiver DW",
        "manufacturer": "Sennheiser",
        "category": "audio",
        "version": "1.0.0",
        # learned_from (channel count, MAC address) landed in 0.37.0.
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": (
            "Controls and monitors the Sennheiser SpeechLine Digital Wireless "
            "SL MCR 2 DW and SL MCR 4 DW multi-channel receivers over "
            "Sennheiser Sound Control (SSC) on UDP port 45. Each receiver "
            "channel is a child entity with mute, gain, EQ preset, low cut, "
            "mute mode, link state, pairing, walk test, RF quality and "
            "warnings, plus the linked transmitter's type, capsule, battery, "
            "charging, locks and LED, and opt-in audio meters. Every change "
            "is pushed through SSC subscriptions. Standby and "
            "Wake-on-LAN wake, Dante and analog output gain, names, LED "
            "brightness and restart are covered."
        ),
        "source_url": (
            "https://assets.sennheiser.com/download/assets/"
            "TI_1094_v4.3_Sennheiser_Sound_Control_Protocol_SL_DW_EN.pdf/"
            "b2a3c92ce7bb11f088db768ee8e97aea"
        ),
        "tags": ["wireless", "microphone", "receiver", "speechline", "ssc", "dante"],
        "verified": False,
        "simulated": True,
        "protocols": ["sennheiser_ssc"],
        "ports": [45],
        "transport": "udp",
        "discovery": {
            # Only the SL MCR DW reports an SLDW product with a channel
            # count after it ("SLDW4CH"); the SL Rack Receiver DW reports a
            # bare "SLDW", and other SSC devices their own names.
            "udp_probe": {
                "port": 45,
                "send_ascii": '{"device":{"identity":{"product":null}}}',
                "expect_regex": r'"product"\s*:\s*"SLDW[^"]+"',
                "extract_manufacturer": "Sennheiser",
                "extract": {
                    "model": {"regex": r'"product"\s*:\s*"(SLDW[^"]+)"'},
                },
            },
            "oui": ["00:1b:66"],   # Sennheiser electronic GmbH & Co. KG
            "manufacturer_alias": ["sennheiser"],
        },
        "compatible_models": [
            {
                "manufacturer": "Sennheiser",
                "models": ["SL MCR 2 DW", "SL MCR 4 DW"],
                "confidence": "untested",
                "notes": (
                    "Both share one command set. The SL MCR 4 DW has four "
                    "channels, the SL MCR 2 DW two; the count is read from "
                    "the receiver's product name, so Channel Count only "
                    "matters before the first connection. The SL Rack "
                    "Receiver DW and the CHG 2N / 4N chargers use different "
                    "SSC commands and are not covered."
                ),
            }
        ],
        "help": {
            "overview": (
                "The SpeechLine Multi-Channel Receiver DW is controlled with "
                "Sennheiser Sound Control (SSC) on UDP port 45. Each receiver "
                "channel is a child entity with its name, mute, gain, EQ "
                "preset, low cut and mute mode, its link state, pairing, walk "
                "test and RF quality, and the linked transmitter's type, "
                "capsule, battery, charging state, locks and LED. Device-level "
                "state covers the names, network, Dante and analog output "
                "gain, LED brightness, warnings and standby.\n\n"
                "State is push-driven: every value is subscribed, so each "
                "change appears as it happens, including changes made in "
                "Sennheiser Control Cockpit or on a transmitter. Audio meters "
                "stream only when Meter Interval is set or Meters On is sent."
            ),
            "setup": (
                "1. Give the receiver a static IP address or a DHCP "
                "reservation.\n"
                "2. In OpenAVC, enter that IP. Port 45 is fixed.\n"
                "3. Set Channel Count to 4 for an SL MCR 4 DW or 2 for an SL "
                "MCR 2 DW. It is corrected from the receiver's product name on "
                "the first connection.\n"
                "4. To see live audio meters on a panel, set Meter Interval "
                "to a value like 500 ms (0 leaves metering off).\n"
                "5. Standby can only be left by Wake-on-LAN. The MAC address "
                "Wake uses is filled in when the receiver first connects."
            ),
        },
        "default_config": {
            "host": "",
            "port": 45,
            "channel_count": 4,
            "poll_interval": 30,
            "meter_interval_ms": 0,
            "mac_address": "",
            # The document states no minimum gap between messages; this is
            # the platform's default 50 ms. Connect sends about twenty
            # subscription requests one after another.
            "inter_command_delay": 0.05,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "The receiver's IP address.",
            },
            "port": {
                "type": "integer",
                "default": 45,
                "label": "Port",
                "description": "Sennheiser Sound Control port, always 45.",
                "advanced": True,
            },
            "channel_count": {
                "type": "integer",
                "default": 4,
                "min": 2,
                "max": 4,
                "label": "Channel Count",
                "learned_from": "channel_count_reported",
                "description": (
                    "4 for an SL MCR 4 DW, 2 for an SL MCR 2 DW. On the "
                    "first connection, the channel count is read from the "
                    "receiver's product name and saved here."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 5,
                "max": 600,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often the subscriptions are renewed. Changes arrive "
                    "as they happen, so this can be long."
                ),
                "advanced": True,
            },
            "meter_interval_ms": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "max": 10000,
                "label": "Meter Interval (ms)",
                "description": (
                    "How often the audio meters update, in milliseconds (0 = "
                    "off). Metering streams continuously, so leave it off "
                    "unless a panel shows live meters."
                ),
            },
            "mac_address": {
                "type": "string",
                "default": "",
                "label": "MAC Address (for Wake-on-LAN)",
                "learned_from": "mac_address",
                "description": (
                    "Filled in from the receiver when it connects, so Wake "
                    "can still bring it out of standby after a restart. Fill "
                    "it in yourself only for a receiver that has never "
                    "connected."
                ),
            },
            "inter_command_delay": {
                "type": "number",
                "default": 0.05,
                "min": 0,
                "label": "Inter-Command Delay (sec)",
                "description": (
                    "Pause between messages sent to the receiver. Raise it if "
                    "some channels never show their status after connecting."
                ),
                "advanced": True,
            },
        },
        "state_variables": {
            "model": {
                "type": "string", "label": "Model",
                "help": "The receiver's product name (SLDW4CH for an SL MCR 4 "
                        "DW).",
            },
            "channel_count_reported": {
                "type": "integer", "label": "Channels", "min": 2, "max": 4,
                "help": "Channel count read from the receiver's product name.",
            },
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "interface_version": {
                "type": "string", "label": "SSC Interface Version",
            },
            "device_name": {
                "type": "string", "label": "Device Name",
                "help": "The receiver's name, as shown in Control Cockpit and in "
                        "device discovery.",
            },
            "dante_name": {"type": "string", "label": "Dante Name"},
            "location": {"type": "string", "label": "Location"},
            "position": {"type": "string", "label": "Position"},
            "system_name": {"type": "string", "label": "System Name"},
            "identifying": {
                "type": "boolean", "label": "Identifying",
                "help": "The receiver's LEDs are flashing to identify it.",
            },
            "led_brightness": {
                "type": "integer", "label": "LED Brightness", "min": 0, "max": 5,
                "step": 1, "control": True,
            },
            "standby": {
                "type": "boolean", "label": "Standby", "cloud_priority": "high",
                "help": "The receiver is in standby. Send Wake to end it.",
            },
            "device_state": {
                "type": "enum", "values": ["normal", "update", "update_failed", "error"],
                "label": "Receiver State", "cloud_priority": "high",
                "help": "normal; update (a firmware update is running); "
                        "update_failed or error (see the receiver's error log).",
            },
            "update_progress": {
                "type": "integer", "label": "Update Progress", "min": 0, "max": 100,
                "unit": "%",
                "help": "Progress of a running firmware update.",
            },
            "device_warning": {
                "type": "string", "label": "Receiver Warning",
                "help": "The receiver's own warning (HW_FAILURE, "
                        "MIXERDSP_FAILURE, SETTINGS_SAVE_FAILED, UPDATE_FAILED "
                        "or SETTINGS_DEFAULTED); empty when there is none.",
            },
            "device_fault": {
                "type": "boolean", "label": "Receiver Fault", "cloud_priority": "high",
                "help": "The receiver's warning is a hardware, DSP, settings or "
                        "update failure.",
            },
            "network_mode": {
                "type": "enum", "values": ["single_cable", "redundant", "split"],
                "label": "Network Mode",
                "help": "single_cable (control and Dante on one cable, the "
                        "factory default), redundant (Dante audio redundancy) "
                        "or split (control and Dante on separate ports).",
            },
            "ip_address": {"type": "string", "label": "IP Address"},
            "mac_address": {
                "type": "string", "label": "MAC Address",
                "help": "The receiver's network MAC address. The Wake-on-LAN "
                        "packet from Wake goes to this address.",
            },
            "mdns_enabled": {"type": "boolean", "label": "mDNS"},
            "analog_output_gain": {
                "type": "integer", "label": "Analog Output Gain",
                "min": -24, "max": 12, "step": 6, "unit": "dB",
                "help": "Gain of the analog output (read only).",
            },
            "dante_output_gain": {
                "type": "integer", "label": "Dante Output Gain",
                "min": -24, "max": 12, "step": 6, "unit": "dB", "control": True,
            },
            "dante_firmware_version": {"type": "string", "label": "Dante Firmware"},
            "dante_ip_primary": {"type": "string", "label": "Dante Primary IP"},
            "dante_ip_secondary": {"type": "string", "label": "Dante Secondary IP"},
            "metering": {
                "type": "boolean", "label": "Metering",
                "help": "The receiver is streaming audio meters.",
            },
            "mix_level_dbfs": {
                "type": "number", "label": "Mix Level", "min": -60, "max": 0,
                "unit": "dBFS", "cloud_priority": "low",
                "help": "Level of the receiver's mixed output (-60 to 0).",
            },
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": "The error returned for the last change the receiver did "
                        "not accept.",
            },
        },
        "child_entity_types": {
            "channel": {
                "label": "Channel",
                "label_plural": "Channels",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "state_variables": {
                    # ── Receiver channel ──
                    "name": {
                        "type": "string", "label": "Channel Name",
                        "help": "Up to eight characters, shown on a transmitter with a "
                                "display.",
                    },
                    "mute": {
                        "type": "boolean", "label": "Mute", "control": True,
                        "cloud_priority": "high",
                        "help": "The linked transmitter's mute, set from here or by its "
                                "mute switch.",
                    },
                    "mute_mode": {
                        "type": "enum",
                        "values": ["off", "on", "push_to_talk", "push_to_mute", "room_mute"],
                        "label": "Mute Mode", "control": True,
                        "help": "What the transmitter's mute switch does: off "
                                "(disabled), on (mute switch), push_to_talk, "
                                "push_to_mute or room_mute (location-based mute).",
                    },
                    "gain": {
                        "type": "integer", "label": "Gain",
                        "min": -24, "max": 12, "step": 6, "unit": "dB",
                        "control": True,
                        "help": "Channel gain, -24 to +12 dB in 6 dB steps.",
                    },
                    "eq_preset": {
                        "type": "enum",
                        "values": ["off", "female_speech", "male_speech", "media", "custom"],
                        "label": "EQ Preset", "control": True,
                    },
                    "low_cut": {
                        "type": "boolean", "label": "Low Cut", "control": True,
                    },
                    "link_state": {
                        "type": "enum",
                        "values": [
                            "not_connected", "connected", "pairing",
                            "transmitter_update", "transmitter_update_confirmation",
                            "transmitter_update_failed", "walktest",
                        ],
                        "label": "Link State", "cloud_priority": "high",
                        "help": "not_connected (no transmitter, no audio); connected; "
                                "pairing; transmitter_update (updating the "
                                "transmitter's firmware); "
                                "transmitter_update_confirmation (an update is pending: "
                                "Approve or Reject it); transmitter_update_failed; "
                                "walktest.",
                    },
                    "identifying": {
                        "type": "boolean", "label": "Identifying",
                        "help": "The channel's LED is flashing to show which channel "
                                "this is.",
                    },
                    "pairing": {
                        "type": "boolean", "label": "Pairing",
                        "help": "The channel is in pairing mode.",
                    },
                    "pair_countdown": {
                        "type": "integer", "label": "Pairing Time Left", "min": 0,
                        "max": 120, "unit": "s",
                        "help": "Seconds left to pair a transmitter (from 120).",
                    },
                    "walktest": {
                        "type": "boolean", "label": "Walk Test",
                        "help": "The channel is in walk-test mode.",
                    },
                    "rf_quality_percent": {
                        "type": "integer", "label": "RF Quality", "min": 0, "max": 100,
                        "unit": "%", "cloud_priority": "low",
                    },
                    "sync_info": {
                        "type": "enum",
                        "values": ["not_synced", "master", "follower", "unsynced_follower"],
                        "label": "Sync",
                        "help": "How the channel is synchronized to the system: "
                                "not_synced, master, follower, or unsynced_follower "
                                "(it lost its master).",
                    },
                    "rfpi": {
                        "type": "string", "label": "RFPI",
                        "help": "The channel's radio identity.",
                    },
                    "last_paired_ipei": {
                        "type": "string", "label": "Last Paired Transmitter",
                        "help": "Radio identity (IPEI) of the last transmitter linked "
                                "to this channel.",
                    },
                    "receiver_warning": {
                        "type": "string", "label": "Channel Warning",
                        "help": "The channel's warnings (HW Failure, Bad Link, No "
                                "Link, No FW Image, TX OTA FWU failed); empty when "
                                "there are none.",
                    },
                    "bad_link": {
                        "type": "boolean", "label": "Bad Link", "cloud_priority": "high",
                        "help": "The radio link to the transmitter is poor.",
                    },
                    "tx_update_progress": {
                        "type": "integer", "label": "Transmitter Update Progress",
                        "min": 0, "max": 100, "unit": "%",
                    },
                    # ── Transmitter settings (kept by the channel) ──
                    "tx_sensitivity": {
                        "type": "enum",
                        "values": ["automatic", "0db", "-6db", "-12db", "-18db",
                                   "-24db", "-30db"],
                        "label": "Transmitter Sensitivity", "control": True,
                        "help": "Automatic gain control, or a fixed sensitivity.",
                    },
                    "tx_led": {
                        "type": "boolean", "label": "Transmitter LED", "control": True,
                        "help": "The LED of a handheld or bodypack transmitter is on.",
                    },
                    "tx_auto_power_off": {
                        "type": "enum", "values": ["off", "10min", "20min", "30min"],
                        "label": "Transmitter Auto Power Off",
                        "help": "The transmitter switches off after this long without "
                                "a link.",
                    },
                    "tx_power_lock": {
                        "type": "boolean", "label": "Power Button Locked",
                    },
                    "tx_pairing_lock": {
                        "type": "boolean", "label": "Pairing Button Locked",
                    },
                    # ── The linked transmitter ──
                    "tx_linked": {
                        "type": "boolean", "label": "Transmitter Linked",
                        "cloud_priority": "high",
                        "help": "A transmitter has a link to this channel.",
                    },
                    "no_link": {
                        "type": "boolean", "label": "No Link", "cloud_priority": "high",
                        "help": "No transmitter is linked.",
                    },
                    "tx_type": {
                        "type": "enum",
                        "values": ["handheld", "bodypack", "table_stand", "boundary"],
                        "label": "Transmitter Type",
                    },
                    "tx_capsule": {
                        "type": "string", "label": "Capsule",
                        "help": "The microphone or input of the linked transmitter "
                                "(MD42, MME865, Line and so on); INCOMPATIBLE for an "
                                "unsupported capsule, empty when none is connected.",
                    },
                    "tx_switch_label": {
                        "type": "string", "label": "Switch Label",
                        "help": "The function of the transmitter's switch.",
                    },
                    "tx_battery_type": {
                        "type": "enum", "values": ["battery", "rechargeable"],
                        "label": "Battery Type",
                    },
                    "tx_battery_percent": {
                        "type": "integer", "label": "Battery Level", "min": 0, "max": 100,
                        "unit": "%", "cloud_priority": "high",
                    },
                    "tx_battery_minutes": {
                        "type": "integer", "label": "Battery Runtime", "unit": "min",
                        "cloud_priority": "high",
                        "help": "Estimated runtime left on a rechargeable battery; "
                                "unknown for a while after power-on.",
                    },
                    "tx_battery_bars": {
                        "type": "integer", "label": "Battery Bars", "min": 0, "max": 5,
                        "help": "Battery bars as shown in Control Cockpit "
                                "(rechargeable batteries).",
                    },
                    "tx_battery_health_percent": {
                        "type": "integer", "label": "Battery Health", "min": 0,
                        "max": 100, "unit": "%",
                    },
                    "tx_battery_cycles": {
                        "type": "integer", "label": "Battery Cycles",
                    },
                    "tx_charging": {
                        "type": "boolean", "label": "Charging",
                        "help": "A table-stand or boundary microphone's battery is "
                                "charging.",
                    },
                    "tx_gooseneck_attached": {
                        "type": "boolean", "label": "Gooseneck Attached",
                        "help": "A table-stand transmitter has its gooseneck attached.",
                    },
                    "tx_low_battery": {
                        "type": "boolean", "label": "Low Battery", "cloud_priority": "high",
                        "help": "The transmitter's warning is Low Bat.",
                    },
                    "tx_warning": {
                        "type": "string", "label": "Transmitter Warning",
                        "help": "The transmitter's warnings; empty when there are none.",
                    },
                    # ── Meters (opt-in: Meter Interval, or Meters On) ──
                    "input_level_dbfs": {
                        "type": "number", "label": "Input Level", "min": -60, "max": 0,
                        "unit": "dBFS", "cloud_priority": "low",
                        "help": "Level arriving from the transmitter (-60 to 0).",
                    },
                    "level_dbfs": {
                        "type": "number", "label": "Audio Level", "min": -60, "max": 0,
                        "unit": "dBFS", "cloud_priority": "low",
                        "help": "Level after the channel gain (-60 to 0).",
                    },
                },
                "summary_fields": ["name", "mute", "tx_linked", "tx_battery_percent",
                                   "no_link", "bad_link"],
                "label_field": "name",
            },
        },
        "device_settings": {
            "device_name": {
                "type": "string", "label": "Device Name",
                "help": "Up to 29 characters: letters, digits, '-' and '_', starting "
                        "with a letter and ending with a letter or digit.",
                "state_key": "device_name", "default": "",
                "regex": r"^[A-Za-z](?:[A-Za-z0-9_-]{0,27}[A-Za-z0-9])?$",
                "setup": False,
            },
            "dante_name": {
                "type": "string", "label": "Dante Name",
                "help": "The name the receiver has on the Dante network, up to 16 "
                        "characters.",
                "state_key": "dante_name", "default": "",
                "regex": r"^.{1,16}$",
                "setup": False,
            },
            "location": {
                "type": "string", "label": "Location",
                "state_key": "location", "default": "", "setup": False,
            },
            "position": {
                "type": "string", "label": "Position",
                "state_key": "position", "default": "", "setup": False,
            },
            "system_name": {
                "type": "string", "label": "System Name",
                "state_key": "system_name", "default": "", "setup": False,
            },
            "led_brightness": {
                "type": "integer", "label": "LED Brightness",
                "help": "0 (off) to 5.",
                "state_key": "led_brightness", "default": 5, "min": 0, "max": 5,
                "setup": False,
            },
            "mdns_enabled": {
                "type": "boolean", "label": "mDNS",
                "help": "mDNS announcements from the receiver are on.",
                "state_key": "mdns_enabled", "default": True, "setup": False,
            },
            "dante_output_gain": {
                "type": "integer", "label": "Dante Output Gain",
                "help": "-24 to +12 dB, in 6 dB steps; a value between steps is "
                        "rounded to the nearest one.",
                "state_key": "dante_output_gain", "default": 0, "min": -24, "max": 12,
                "setup": False,
            },
        },
        "commands": {
            "identify": {
                "label": "Identify Receiver",
                "help": "Flash the receiver's LEDs to show which unit it is.",
            },
            "identify_off": {
                "label": "Stop Identifying",
                "help": "Stop flashing the receiver's LEDs.",
            },
            "standby": {
                "label": "Standby",
                "help": "Put the receiver into standby. Until Wake is sent it does "
                        "not respond, and the device stays shown as connected "
                        "with Standby on.",
            },
            "wake": {
                "label": "Wake",
                "help": "Bring the receiver out of standby with a Wake-on-LAN "
                        "packet to its MAC address (filled in on the first "
                        "connection, or entered under Edit Device). Runs while "
                        "the receiver is offline.",
                "available_offline": True,
            },
            "restart": {
                "label": "Restart Receiver",
                "help": "Restart the receiver. Audio stops until it is back.",
                "confirm": "Restarts the receiver. Every channel's audio stops "
                           "until it is back.",
                # The document gives no boot time; not yet measured on a unit.
                "restarts_device_for": 90,
            },
            "factory_reset": {
                "label": "Factory Reset",
                "help": "Return every setting to its factory default and restart "
                        "the receiver.",
                "confirm": "Returns every setting on the receiver to its factory "
                           "default and restarts it. Names, audio settings and "
                           "transmitter settings are lost.",
                "restarts_device_for": 90,
            },
            "restore_audio_defaults": {
                "label": "Restore Audio Defaults",
                "help": "Reset the receiver's audio effects and custom EQ to their "
                        "defaults.",
                "confirm": "Resets the receiver's audio effects and custom EQ "
                           "settings to their defaults.",
            },
            "set_network_mode": {
                "label": "Set Network Mode",
                "params": {
                    "mode": {
                        "type": "enum", "values": ["single_cable", "redundant", "split"],
                        "required": True, "label": "Network Mode",
                        "help": "single_cable (factory default), redundant (Dante "
                                "audio redundancy) or split (control and Dante on "
                                "separate ports).",
                    },
                },
                "help": "Change how the receiver's network ports are used. The "
                        "receiver restarts.",
                "confirm": "Changes how the receiver's network ports are used and "
                           "restarts it. With the wrong mode for the cabling it "
                           "can drop off the control network.",
                "restarts_device_for": 90,
            },
            "meters_on": {
                "label": "Meters On",
                "params": {
                    "interval_ms": {
                        "type": "integer", "label": "Interval (ms)", "required": False,
                        "min": 100, "max": 10000, "unit": "ms",
                        "help": "How often the meters update. Leave it empty to use "
                                "Meter Interval, or 500 ms when that is 0.",
                    },
                },
                "help": "Start the audio meters until Meters Off or the next "
                        "reconnect.",
            },
            "meters_off": {
                "label": "Meters Off",
                "help": "Stop the audio meters.",
            },
            "set_channel_mute": {
                "label": "Set Channel Mute",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "mute": {"type": "boolean", "required": True, "label": "Mute"},
                },
                "help": "Mute or unmute the channel's transmitter.",
            },
            "toggle_channel_mute": {
                "label": "Toggle Channel Mute",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "Mute the channel's transmitter if it is unmuted, and the "
                        "other way round.",
            },
            "set_channel_gain": {
                "label": "Set Channel Gain",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "gain_db": {
                        "type": "integer", "required": True, "label": "Gain",
                        "min": -24, "max": 12, "unit": "dB",
                        "help": "-24 to +12 dB in 6 dB steps; a value between steps "
                                "is rounded to the nearest one.",
                    },
                },
                "help": "Set the channel gain.",
            },
            "step_channel_gain": {
                "label": "Nudge Channel Gain",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "delta_db": {
                        "type": "integer", "required": True, "label": "Change",
                        "min": -36, "max": 36, "unit": "dB",
                        "help": "Gain change in dB, in 6 dB steps (6 or -6 for one "
                                "step).",
                    },
                },
                "help": "Raise or lower the channel gain, stopping at -24 and +12 dB.",
            },
            "set_channel_eq": {
                "label": "Set Channel EQ Preset",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "preset": {
                        "type": "enum",
                        "values": ["off", "female_speech", "male_speech", "media", "custom"],
                        "required": True, "label": "Preset",
                    },
                },
                "help": "Choose the channel's equalizer preset.",
            },
            "set_channel_low_cut": {
                "label": "Set Channel Low Cut",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "enabled": {"type": "boolean", "required": True, "label": "Low Cut"},
                },
                "help": "Turn the channel's low-cut filter on or off.",
            },
            "set_channel_mute_mode": {
                "label": "Set Mute Mode",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "mode": {
                        "type": "enum",
                        "values": ["off", "on", "push_to_talk", "push_to_mute", "room_mute"],
                        "required": True, "label": "Mute Mode",
                    },
                },
                "help": "Choose what the transmitter's mute switch does.",
            },
            "restore_channel_audio": {
                "label": "Restore Channel Audio Defaults",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "Reset the channel's audio settings to their defaults.",
                "confirm": "Resets this channel's audio settings to their defaults.",
            },
            "set_channel_name": {
                "label": "Set Channel Name",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "name": {
                        "type": "string", "required": True, "label": "Name",
                        "pattern": r"^[A-Za-z](?:[A-Za-z0-9_-]{0,6}[A-Za-z0-9])?$",
                        "help": "Up to 8 characters: letters, digits, '-' and '_', "
                                "starting with a letter and ending with a letter or "
                                "digit.",
                    },
                },
                "help": "Rename the channel.",
            },
            "identify_channel": {
                "label": "Identify Channel",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "Flash the channel's LED to show which channel it is. Not "
                        "available while the channel is pairing or in walk test.",
            },
            "identify_channel_off": {
                "label": "Stop Identifying Channel",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "Stop flashing the channel's LED.",
            },
            "start_pairing": {
                "label": "Start Pairing",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "Put the channel in pairing mode for up to two minutes; pair "
                        "a transmitter to it in that time. Pairing Time Left counts "
                        "down.",
            },
            "stop_pairing": {
                "label": "Stop Pairing",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "Leave pairing mode.",
            },
            "walktest_on": {
                "label": "Start Walk Test",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "Start the walk test: move around the room with the "
                        "transmitter and watch RF Quality. The channel must have a "
                        "linked transmitter.",
            },
            "walktest_off": {
                "label": "Stop Walk Test",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "End the walk test.",
            },
            "set_tx_sensitivity": {
                "label": "Set Transmitter Sensitivity",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "sensitivity": {
                        "type": "enum",
                        "values": ["automatic", "0db", "-6db", "-12db", "-18db",
                                   "-24db", "-30db"],
                        "required": True, "label": "Sensitivity",
                    },
                },
                "help": "Set the audio sensitivity of the channel's transmitter: "
                        "automatic gain control, or a fixed level.",
            },
            "set_tx_auto_power_off": {
                "label": "Set Transmitter Auto Power Off",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "timeout": {
                        "type": "enum", "values": ["off", "10min", "20min", "30min"],
                        "required": True, "label": "Switch Off After",
                    },
                },
                "help": "Choose how long the transmitter stays on without a link.",
            },
            "set_tx_power_lock": {
                "label": "Set Power Button Lock",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "locked": {"type": "boolean", "required": True, "label": "Locked"},
                },
                "help": "Lock or unlock the transmitter's power button.",
            },
            "set_tx_pairing_lock": {
                "label": "Set Pairing Button Lock",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "locked": {"type": "boolean", "required": True, "label": "Locked"},
                },
                "help": "Lock or unlock the transmitter's pairing button.",
            },
            "set_tx_led": {
                "label": "Set Transmitter LED",
                "params": {
                    "channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"},
                    "enabled": {"type": "boolean", "required": True, "label": "LED On"},
                },
                "help": "Turn the LED of a handheld or bodypack transmitter on or off.",
            },
            "power_down_tx": {
                "label": "Switch Off Transmitter",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "Switch the channel's transmitter off.",
                "confirm": "Switches the transmitter off. It has to be switched on "
                           "again at the transmitter.",
            },
            "approve_tx_update": {
                "label": "Approve Transmitter Update",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "Approve the pending firmware update for the channel's "
                        "transmitter (Link State: "
                        "transmitter_update_confirmation).",
            },
            "reject_tx_update": {
                "label": "Reject Transmitter Update",
                "params": {"channel": {"type": "child_id", "child_type": "channel", "required": True,
                            "label": "Channel"}},
                "help": "Decline the pending firmware update for the channel's "
                        "transmitter.",
                "confirm": "Declines the transmitter's firmware update. The "
                           "transmitter is removed from the receiver's pairing list "
                           "and has to be paired again.",
            },
            "send_raw": {
                "label": "Send Raw SSC",
                "params": {
                    "message": {
                        "type": "string", "required": True, "label": "SSC Message",
                        "help": 'One SSC JSON message, e.g. {"device":{"name":null}}.',
                    },
                },
                "help": "Send one SSC message, for anything the other commands do "
                        "not cover (network addresses, for instance). Values in "
                        "the reply update state as usual.",
            },
        },
        "quick_actions": ["identify", "identify_off"],
        "actions": [
            {"id": "identify", "kind": "command", "icon": "sun"},
        ],
    }

    HEALTH_FAULT_MESSAGE = "No reply from the receiver to keep-alive pings."

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        count = int(config.get("channel_count", DEFAULT_CHANNEL_COUNT) or DEFAULT_CHANNEL_COUNT)
        self._channel_count = 4 if count > 2 else 2
        poll = int(config.get("poll_interval", 30) or 30)
        self._lifetime = max(_MIN_LIFETIME_S, 3 * poll)
        self._config_meter_ms = max(0, int(config.get("meter_interval_ms", 0) or 0))
        self._probe_fut: asyncio.Future[None] | None = None
        self._product_fut: asyncio.Future[None] | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._reset_session()
        super().__init__(device_id, config, state, events)

    def _reset_session(self) -> None:
        """Per-connection protocol state."""
        self._topology_ready = False
        self._tx_active: dict[int, bool] = {}
        self._resync_pending = False
        self._resync_not_before = 0.0
        self._last_full_sub = 0.0
        self._last_group_sub: dict[str, float] = {}
        self._resub_wanted: set[str] = set()
        self._resub_task: asyncio.Task | None = None
        self._recent_writes: dict[tuple[str, ...], float] = {}
        self._meters_on = self._config_meter_ms > 0
        self._meter_s = (self._config_meter_ms or _DEFAULT_METER_MS) / 1000.0
        self._meter_pending: dict[tuple[int | None, str], Any] = {}
        self._meter_flush: asyncio.TimerHandle | None = None
        self._wake_task: asyncio.Task | None = None

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        if not self.config.get("host", ""):
            raise ConnectionError(f"[{self.device_id}] Receiver host is required")

    async def _initial_sync(self) -> None:
        self._reset_session()
        self.set_state("metering", self._meters_on)
        try:
            # Size the roster from the product name before subscribing, so a
            # two-channel unit is never asked about rx3 and rx4.
            loop = asyncio.get_running_loop()
            self._product_fut = loop.create_future()
            await self._get(("device", "identity", "product"))
            try:
                await asyncio.wait_for(asyncio.shield(self._product_fut), _IDENTITY_WAIT_S)
            except asyncio.TimeoutError:
                # Nothing answered: subscribe anyway, and again as soon as
                # the receiver answers anything.
                self._mark_resync()
            self._register_topology()
            await self._read_identity()
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial sync failed")

    async def _close_session(self) -> None:
        for fut in (self._probe_fut, self._product_fut):
            if fut is not None and not fut.done():
                fut.set_exception(ConnectionError("connection closed"))
                fut.exception()  # seen, so it is not logged as lost
        self._probe_fut = None
        self._product_fut = None
        if self._meter_flush is not None:
            self._meter_flush.cancel()
            self._meter_flush = None
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def _read_identity(self) -> None:
        """The values that cannot be subscribed, read once per connection."""
        for path in (
            ("device", "identity", "version"),
            ("device", "identity", "serial"),
            ("device", "network", "ether", "macs"),
            ("interface", "version"),
            ("audio", "out2", "identity", "version"),
        ):
            await self._get(path)

    # ── Topology ──

    def _register_topology(self) -> None:
        """Register the channel roster; idempotent, and a placeholder label
        never overrides one the user set in the project."""
        for n in range(1, self._channel_count + 1):
            initial = None
            project = self._project_child_entities.get("channel", {}).get(str(n))
            if not (project and project.get("label")):
                initial = {"label": f"Channel {n}"}
            self.register_child("channel", n, initial_state=initial)
        for existing in list(self.list_children("channel")):
            if not (1 <= int(existing) <= self._channel_count):
                self.deregister_child("channel", existing)
        self._topology_ready = True

    async def refresh_children(self) -> dict[str, Any]:
        """IDE 'Refresh from Device': re-register and re-read everything."""
        if not (self.transport and self.transport.connected):
            raise ConnectionError(f"[{self.device_id}] Not connected")
        self._register_topology()
        self._last_full_sub = 0.0
        await self._subscribe_all()
        return {"channel": self._channel_count}

    async def _resize_roster(self, count: int) -> None:
        """The product name said how many channels the receiver has."""
        old = self._channel_count
        self._channel_count = count
        self._register_topology()
        try:
            for n in range(old + 1, count + 1):
                for name in self._channel_groups(n):
                    await self._subscribe_group(name)
            if self._meters_on:
                await self._subscribe_group("meters")
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Roster resync failed")

    # ── Subscriptions ──

    def _channel_groups(self, n: int) -> list[str]:
        names = [f"rx{n}", f"rx{n}_progress", f"settings{n}"]
        if self._tx_active.get(n):
            names += [f"tx{n}", f"tx{n}_extra"]
        return names

    def _active_groups(self) -> list[str]:
        names = ["device", "device_update"]
        for n in range(1, self._channel_count + 1):
            names += self._channel_groups(n)
        if self._meters_on:
            names.append("meters")
        return names

    def _group_paths(self, name: str) -> list[tuple[str, ...]]:
        if name == "device":
            return _device_group()
        if name == "device_update":
            # Pre-condition: a firmware update is running.
            return [("device", "update", "progress")]
        if name == "meters":
            return _meter_group(self._channel_count)
        m = re.match(r"^(rx|settings|tx)(\d+)(_progress|_extra)?$", name)
        if not m:
            return []
        kind, n, suffix = m.group(1), int(m.group(2)), m.group(3) or ""
        if kind == "rx":
            return _rx_progress_group(n) if suffix else _rx_group(n)
        if kind == "settings":
            return _settings_group(n)
        return _tx_extra_group(n) if suffix else _tx_group(n)

    def _group_of(self, path: tuple[str, ...]) -> str | None:
        """The group an address is subscribed in, wanted now or not."""
        names = ["device", "device_update", "meters"]
        for n in range(1, 5):
            names += [f"rx{n}", f"rx{n}_progress", f"settings{n}", f"tx{n}", f"tx{n}_extra"]
        for name in names:
            if path in self._group_paths(name):
                return name
        return None

    def _mark_resync(self, after: float = 0.0) -> None:
        """Subscribe everything anew at the receiver's next answer."""
        self._resync_pending = True
        self._resync_not_before = asyncio.get_running_loop().time() + after

    def _maybe_resync(self) -> None:
        if not self._resync_pending or self.get_state("standby"):
            return
        if asyncio.get_running_loop().time() < self._resync_not_before:
            return
        self._resync_pending = False
        self._last_full_sub = 0.0
        self._spawn(self._subscribe_all())

    def _subscribe_message(self, name: str, *, cancel: bool = False) -> dict[str, Any]:
        tree: dict[str, Any] = {"#": {"cancel": True} if cancel else {"lifetime": self._lifetime}}
        for path in self._group_paths(name):
            _put(tree, path, None)
        return {"osc": {"state": {"subscribe": [tree]}}}

    async def _subscribe_group(self, name: str) -> None:
        self._last_group_sub[name] = asyncio.get_running_loop().time()
        await self._send_json(self._subscribe_message(name))

    async def _subscribe_quietly(self, name: str) -> None:
        try:
            await self._subscribe_group(name)
        except (ConnectionError, OSError):
            pass

    async def _subscribe_all(self) -> None:
        """Subscribe (or renew) every group the receiver should report."""
        loop = asyncio.get_running_loop()
        if loop.time() - self._last_full_sub < _FULL_RESUBSCRIBE_GAP_S:
            return
        self._last_full_sub = loop.time()
        for name in self._active_groups():
            await self._subscribe_group(name)

    def _resubscribe_soon(self, names: set[str]) -> None:
        """Resubscribe groups a 310 ended, no group sooner than the gap."""
        self._resub_wanted |= names
        if self._resub_task is None or self._resub_task.done():
            self._resub_task = self._spawn(self._resubscribe_loop())

    async def _resubscribe_loop(self) -> None:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(0.2)  # one 310 often names several groups
        while self._resub_wanted:
            if self.get_state("standby"):
                self._resub_wanted.clear()
                return
            now = loop.time()
            due = {
                n for n in self._resub_wanted
                if now - self._last_group_sub.get(n, 0.0) >= _RESUBSCRIBE_GAP_S
            }
            active = set(self._active_groups())
            for name in sorted(due):
                self._resub_wanted.discard(name)
                if name in active:
                    try:
                        await self._subscribe_group(name)
                    except (ConnectionError, OSError):
                        self._resub_wanted.clear()
                        return
            if self._resub_wanted:
                await asyncio.sleep(_RESUBSCRIBE_GAP_S)

    # ── Polling and liveness ──

    async def poll(self) -> None:
        if self.get_state("standby"):
            # Asleep: ask only whether it is still asleep (another client
            # may have woken it).
            await self._get(("device", "standby"))
            return
        self._last_full_sub = 0.0
        await self._subscribe_all()
        await self._get(("device", "standby"))

    async def _liveness_probe(self) -> None:
        """Send ``/osc/ping`` and await its echo.

        A receiver the driver knows is in standby answers nothing until Wake,
        and that is not a fault, so no ping is sent then.
        """
        if self.get_state("standby"):
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._probe_fut = fut
        try:
            await self._send_json({"osc": {"ping": None}})
            await fut
        finally:
            self._probe_fut = None

    # ── Sending ──

    async def _send_json(self, message: dict[str, Any]) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        text = json.dumps(message, separators=(",", ":"))
        await self.transport.send(text.encode("utf-8"))

    async def _get(self, path: tuple[str, ...]) -> None:
        await self._send_json(_tree(path, None))

    async def _set(self, path: tuple[str, ...], value: Any) -> None:
        self._recent_writes[path] = asyncio.get_running_loop().time()
        await self._send_json(_tree(path, value))

    # ── Commands ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "wake":
            return await self._wake()
        if command == "identify":
            await self._set(("device", "identification", "visual"), True)
        elif command == "identify_off":
            await self._set(("device", "identification", "visual"), False)
        elif command == "standby":
            await self._set(("device", "standby"), True)
        elif command == "restart":
            await self._set(("device", "restart"), True)
            self._mark_resync(_RESTART_GRACE_S)
        elif command == "factory_reset":
            await self._set(("device", "restore"), "FACTORY_DEFAULTS")
            self._mark_resync(_RESTART_GRACE_S)
        elif command == "restore_audio_defaults":
            await self._set(("device", "restore"), "AUDIO_DEFAULTS")
        elif command == "set_network_mode":
            await self._set(("device", "network", "interface_mapping"),
                            _NETWORK_MODES_WIRE[str(params["mode"])])
            self._mark_resync(_RESTART_GRACE_S)
        elif command == "meters_on":
            ms = _as_int(params.get("interval_ms")) or self._config_meter_ms or _DEFAULT_METER_MS
            self._meter_s = max(100, ms) / 1000.0
            self._meters_on = True
            self.set_state("metering", True)
            await self._subscribe_group("meters")
        elif command == "meters_off":
            await self._meters_off()
        elif command == "send_raw":
            try:
                message = json.loads(str(params["message"]))
            except ValueError:
                raise CommandRefusedError("The SSC message is not valid JSON.") from None
            if not isinstance(message, dict):
                raise CommandRefusedError("An SSC message must be one JSON object.")
            now = asyncio.get_running_loop().time()
            for path, value in _leaves(message):
                if value is not None:
                    self._recent_writes[path] = now
            await self._send_json(message)
        else:
            await self._channel_command(command, params)

    async def _channel_command(self, command: str, params: dict[str, Any]) -> None:
        if "channel" not in params:
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            return
        n = int(params["channel"])
        rx, tx, audio = f"rx{n}", f"tx{n}", ("audio", f"rx{n}")

        if command == "set_channel_mute":
            await self._set((rx, "mute_state"), bool(params["mute"]))
        elif command == "toggle_channel_mute":
            current = self.get_child_state("channel", n).get("mute")
            await self._set((rx, "mute_state"), not bool(current))
        elif command == "set_channel_gain":
            await self._set(audio + ("gain",), _snap_gain(int(params["gain_db"])))
        elif command == "step_channel_gain":
            current = _as_int(self.get_child_state("channel", n).get("gain"))
            if current is None:
                raise CommandRefusedError(
                    f"Channel {n}'s gain is not known yet, so it cannot be nudged.")
            await self._set(audio + ("gain",), _snap_gain(current + int(params["delta_db"])))
        elif command == "set_channel_eq":
            await self._set(audio + ("equalizer", "preset"),
                            _EQ_PRESETS_WIRE[str(params["preset"])])
        elif command == "set_channel_low_cut":
            await self._set(audio + ("low_cut",), bool(params["enabled"]))
        elif command == "set_channel_mute_mode":
            await self._set((rx, "mute_mode"), _MUTE_MODES_WIRE[str(params["mode"])])
        elif command == "restore_channel_audio":
            await self._set(audio + ("restore",), True)
        elif command == "set_channel_name":
            await self._set((rx, "name"), str(params["name"]))
        elif command == "identify_channel":
            await self._set((rx, "identification", "visual"), True)
        elif command == "identify_channel_off":
            await self._set((rx, "identification", "visual"), False)
        elif command == "start_pairing":
            await self._set((rx, "pair", "enable"), True)
        elif command == "stop_pairing":
            await self._set((rx, "pair", "enable"), False)
        elif command == "walktest_on":
            await self._set((rx, "walktest"), True)
        elif command == "walktest_off":
            await self._set((rx, "walktest"), False)
        elif command == "set_tx_sensitivity":
            await self._set(("mates", tx, "agc"),
                            _SENSITIVITIES_WIRE[str(params["sensitivity"])])
        elif command == "set_tx_auto_power_off":
            await self._set(("mates", tx, "auto_power_off"),
                            _AUTO_POWER_OFF_WIRE[str(params["timeout"])])
        elif command == "set_tx_power_lock":
            await self._set(("mates", tx, "power_lock"), bool(params["locked"]))
        elif command == "set_tx_pairing_lock":
            await self._set(("mates", tx, "pairing_button_lock"), bool(params["locked"]))
        elif command == "set_tx_led":
            await self._set(("mates", tx, "led_active"), bool(params["enabled"]))
        elif command == "power_down_tx":
            await self._set(("mates", tx, "power_down"), True)
        elif command == "approve_tx_update":
            await self._set((rx, "update", "confirmation"), True)
        elif command == "reject_tx_update":
            await self._set((rx, "update", "confirmation"), False)
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def _meters_off(self) -> None:
        self._meters_on = False
        self.set_state("metering", False)
        if self._meter_flush is not None:
            self._meter_flush.cancel()
            self._meter_flush = None
        self._meter_pending.clear()
        # §5.1 "cancel"; if the receiver ignores it, the meters stop when the
        # subscription's lifetime runs out, and their values are ignored.
        await self._send_json(self._subscribe_message("meters", cancel=True))

    async def _wake(self) -> bool:
        """Wake-on-LAN to the receiver's MAC: the one it reported on this or
        an earlier connection, else the MAC Address setting.

        Declared ``available_offline``. On a live connection the standby state
        is then asked for every few seconds until the receiver answers, so the
        subscriptions come back as soon as it does.
        """
        mac = (
            str(self.get_state("mac_address") or "").strip()
            or str(self.config.get("mac_address") or "").strip()
        )
        if not mac:
            raise self._wake_refusal("")
        try:
            await self.wake_on_lan(mac)
        except ValueError:
            raise self._wake_refusal(mac) from None
        if self.transport and self.transport.connected:
            if self._wake_task is None or self._wake_task.done():
                self._wake_task = self._spawn(self._check_awake())
        return True

    @staticmethod
    def _wake_refusal(mac: str) -> Exception:
        if WakeAddressError is not None:
            return WakeAddressError(field_label=_MAC_FIELD_LABEL, value=mac, reported=True)
        if mac:
            return ValueError(
                f"The receiver cannot be woken: '{mac}' is not a MAC address "
                f"(six pairs of hex digits, like 00:1B:66:C3:51:CD). Correct it "
                f"under Edit Device."
            )
        return ValueError(
            "The receiver cannot be woken yet: its MAC address is not known. It "
            "is filled in when the receiver connects; until then, enter it "
            "under Edit Device."
        )

    async def _check_awake(self) -> None:
        loop = asyncio.get_running_loop()
        until = loop.time() + _WAKE_CHECK_FOR_S
        while loop.time() < until and self.get_state("standby") is not False:
            try:
                await self._get(("device", "standby"))
            except (ConnectionError, OSError):
                return
            await asyncio.sleep(_WAKE_CHECK_EVERY_S)

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "device_name":
            await self._set(("device", "name"), str(value))
        elif key == "dante_name":
            await self._set(("device", "dante", "name"), str(value))
        elif key == "location":
            await self._set(("device", "location"), str(value))
        elif key == "position":
            await self._set(("device", "position"), str(value))
        elif key == "system_name":
            await self._set(("device", "system"), str(value))
        elif key == "led_brightness":
            await self._set(("device", "led", "brightness"), max(0, min(5, int(value))))
        elif key == "mdns_enabled":
            await self._set(("device", "network", "mdns"), bool(value))
        elif key == "dante_output_gain":
            await self._set(("audio", "dante", "mixer", "gain"), _snap_gain(int(value)))
        else:
            raise ValueError(f"[{self.device_id}] Unknown device setting: {key}")

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            message = json.loads(text, object_pairs_hook=_merge_pairs)
        except ValueError:
            # One of the document's subscription-end examples is not valid
            # JSON; an ended subscription must still be renewed.
            if _TERMINATED_RE.search(text):
                self._last_full_sub = 0.0
                self._spawn(self._subscribe_all())
            else:
                log.debug(f"[{self.device_id}] Unreadable reply: {text[:120]!r}")
            return
        if not isinstance(message, dict):
            return

        # The receiver answers again after a connect it did not hear, or
        # after a restart: everything is subscribed anew.
        self._maybe_resync()

        osc = message.pop("osc", None)
        if isinstance(osc, dict):
            self._apply_osc(osc)

        device: dict[str, Any] = {}
        channels: dict[int, dict[str, Any]] = {}
        for path, value in _leaves(message):
            self._apply_leaf(path, value, device, channels)
        for key, value in device.items():
            self.set_state(key, value)
        for n, updates in channels.items():
            if updates and 1 <= n <= self._channel_count:
                self.set_child_state_batch("channel", n, updates)

    def _apply_osc(self, osc: dict[str, Any]) -> None:
        fut = self._probe_fut
        if "ping" in osc or "error" in osc:
            # Any answer proves the link, an error included.
            if fut is not None and not fut.done():
                fut.set_result(None)
        state = osc.get("state")
        if isinstance(state, dict) and state.get("close") is True:
            # §11.12: the receiver is about to close (a restart); its
            # subscriptions go with it.
            log.info(f"[{self.device_id}] Receiver announced it is closing")
            self._mark_resync(_RESTART_GRACE_S)
        if "error" in osc:
            self._apply_errors(osc["error"])

    def _apply_errors(self, errors: Any) -> None:
        ended: set[str] = set()
        unplaced_end = False
        now = asyncio.get_running_loop().time()
        active = set(self._active_groups())
        for path, code, text in _error_leaves(errors):
            if code == 310:
                group = self._group_of(path)
                if group is None:
                    unplaced_end = True
                elif group in active:
                    ended.add(group)
                # else: a group no longer wanted (meters off, a link gone).
                continue
            if code == 400 and not path:
                # §12: a message the receiver could not read at all.
                self.set_state("last_error",
                               "A message could not be read by the receiver (400 Bad Request)")
                log.warning(f"[{self.device_id}] Receiver could not read a message")
                continue
            asked = self._recent_writes.get(path)
            if asked is not None and now - asked <= _WRITE_ERROR_WINDOW_S:
                self._recent_writes.pop(path, None)
                detail = f"{code} {text}".strip()
                self.set_state("last_error",
                               f"{_readable(path)} was not accepted: {detail}")
                log.warning(f"[{self.device_id}] Refused {_readable(path)}: {detail}")
            else:
                # A subscription or read the receiver cannot answer in its
                # current state (an address with a pre-condition).
                log.debug(f"[{self.device_id}] {_readable(path)}: error {code} {text}")
        if unplaced_end:
            self._last_full_sub = 0.0
            self._spawn(self._subscribe_all())
        elif ended:
            self._resubscribe_soon(ended)

    def _apply_leaf(
        self,
        path: tuple[str, ...],
        value: Any,
        device: dict[str, Any],
        channels: dict[int, dict[str, Any]],
    ) -> None:
        head, rest = path[0], path[1:]
        if head == "device":
            self._apply_device(rest, value, device)
        elif head == "audio":
            self._apply_audio(rest, value, device, channels)
        elif head == "interface" and rest == ("version",):
            device["interface_version"] = _text(value)
        elif head == "m":
            self._apply_meter(rest, value)
        elif head == "mates" and rest:
            m = _TX_RE.match(rest[0])
            if m:
                n = int(m.group(1))
                self._apply_mate(n, rest[1:], value, channels.setdefault(n, {}))
        else:
            m = _RX_RE.match(head)
            if m:
                n = int(m.group(1))
                self._apply_rx(n, rest, value, channels.setdefault(n, {}))

    # ── Device-level values ──

    def _apply_device(self, rest: tuple[str, ...], value: Any, out: dict[str, Any]) -> None:
        if rest == ("name",):
            out["device_name"] = _text(value)
        elif rest == ("dante", "name"):
            out["dante_name"] = _text(value)
        elif rest == ("location",):
            out["location"] = _text(value)
        elif rest == ("position",):
            out["position"] = _text(value)
        elif rest == ("system",):
            out["system_name"] = _text(value)
        elif rest == ("identification", "visual"):
            out["identifying"] = _as_bool(value)
        elif rest == ("led", "brightness"):
            out["led_brightness"] = _as_int(value)
        elif rest == ("network", "interface_mapping"):
            out["network_mode"] = _mapped(_NETWORK_MODES, value)
        elif rest == ("network", "ipv4", "ipaddr"):
            out["ip_address"] = _text(_first(value))
        elif rest == ("network", "ether", "macs"):
            mac = _text(_first(value))
            if mac:
                out["mac_address"] = mac
        elif rest == ("network", "mdns"):
            out["mdns_enabled"] = _as_bool(value)
        elif rest == ("state",):
            device_state = _mapped(_DEVICE_STATES, value)
            out["device_state"] = device_state
            if device_state == "update" and self.get_state("device_state") != "update":
                self._spawn(self._subscribe_quietly("device_update"))
        elif rest == ("update", "progress"):
            out["update_progress"] = _as_int(value)
        elif rest == ("warnings",):
            warning = _text(value)
            out["device_warning"] = "" if warning.upper() == "NONE" else warning
            out["device_fault"] = not {w.strip().upper() for w in warning.split(",")} <= _DEVICE_NOTICES
        elif rest == ("identity", "product"):
            self._apply_product(_text(value), out)
        elif rest == ("identity", "version"):
            out["firmware_version"] = _text(value)
        elif rest == ("identity", "serial"):
            out["serial_number"] = _text(value)
        elif rest == ("standby",):
            self._apply_standby(_as_bool(value), out)

    def _apply_product(self, product: str, out: dict[str, Any]) -> None:
        out["model"] = product
        count = _channel_count_for_product(product)
        if count is not None:
            out["channel_count_reported"] = count
            if count != self._channel_count:
                if self._topology_ready:
                    self._spawn(self._resize_roster(count))
                else:
                    self._channel_count = count
        fut = self._product_fut
        if fut is not None and not fut.done():
            fut.set_result(None)

    def _apply_standby(self, standby: bool | None, out: dict[str, Any]) -> None:
        if standby is None:
            return
        was = self.get_state("standby")
        out["standby"] = standby
        if standby:
            self._resub_wanted.clear()
            if self._meter_flush is not None:
                self._meter_flush.cancel()
                self._meter_flush = None
            self._meter_pending.clear()
        elif was:
            # Awake again: its subscriptions did not survive standby.
            log.info(f"[{self.device_id}] Receiver left standby")
            self._last_full_sub = 0.0
            self._spawn(self._subscribe_all())

    def _apply_audio(
        self,
        rest: tuple[str, ...],
        value: Any,
        device: dict[str, Any],
        channels: dict[int, dict[str, Any]],
    ) -> None:
        if rest == ("out1", "mixer", "gain"):
            device["analog_output_gain"] = _as_int(value)
        elif rest == ("dante", "mixer", "gain"):
            device["dante_output_gain"] = _as_int(value)
        elif rest == ("out2", "identity", "version"):
            device["dante_firmware_version"] = _text(value)
        elif rest == ("out2", "network", "ipv4", "ipaddr"):
            addrs = value if isinstance(value, list) else [value]
            device["dante_ip_primary"] = _text(addrs[0]) if len(addrs) > 0 else ""
            device["dante_ip_secondary"] = _text(addrs[1]) if len(addrs) > 1 else ""
        elif rest:
            m = _RX_RE.match(rest[0])
            if not m:
                return
            out = channels.setdefault(int(m.group(1)), {})
            key = rest[1:]
            if key == ("gain",):
                out["gain"] = _as_int(value)
            elif key == ("equalizer", "preset"):
                out["eq_preset"] = _mapped(_EQ_PRESETS, value)
            elif key == ("low_cut",):
                out["low_cut"] = _as_bool(value)

    # ── Channel values ──

    def _apply_rx(
        self, n: int, rest: tuple[str, ...], value: Any, out: dict[str, Any]
    ) -> None:
        if rest == ("name",):
            out["name"] = _text(value)
        elif rest == ("state",):
            link_state = _mapped(_LINK_STATES, value)
            out["link_state"] = link_state
            if (link_state in _PROGRESS_STATES and n <= self._channel_count
                    and link_state != self.get_child_state("channel", n).get("link_state")):
                self._spawn(self._subscribe_quietly(f"rx{n}_progress"))
        elif rest == ("identification", "visual"):
            out["identifying"] = _as_bool(value)
        elif rest == ("pair", "enable"):
            out["pairing"] = _as_bool(value)
        elif rest == ("pair", "progress"):
            out["pair_countdown"] = _as_int(value)
        elif rest == ("update", "progress"):
            out["tx_update_progress"] = _as_int(value)
        elif rest == ("rf_quality",):
            out["rf_quality_percent"] = _as_int(value)
        elif rest == ("walktest",):
            out["walktest"] = _as_bool(value)
        elif rest == ("mute_mode",):
            out["mute_mode"] = _mapped(_MUTE_MODES, value)
        elif rest == ("mute_state",):
            out["mute"] = _as_bool(value)
        elif rest == ("master_follower", "sync_info"):
            out["sync_info"] = _mapped(_SYNC_INFO, value)
        elif rest == ("rfpi",):
            out["rfpi"] = _text(value)
        elif rest == ("last_paired_ipei",):
            out["last_paired_ipei"] = _text(value)
        elif rest == ("warnings",):
            warnings = _text(value)
            out["receiver_warning"] = warnings
            out["bad_link"] = "bad link" in warnings.lower()

    def _apply_mate(
        self, n: int, rest: tuple[str, ...], value: Any, out: dict[str, Any]
    ) -> None:
        if not rest:
            return
        key = rest[0]
        if key == "active":
            self._apply_link(n, _as_bool(value), out)
            return
        # The channel's transmitter settings, linked or not.
        if key == "agc":
            out["tx_sensitivity"] = _mapped(_SENSITIVITIES, value)
            return
        if key == "led_active":
            out["tx_led"] = _as_bool(value)
            return
        if key == "auto_power_off":
            out["tx_auto_power_off"] = _mapped(_AUTO_POWER_OFF, value)
            return
        if key == "power_lock":
            out["tx_power_lock"] = _as_bool(value)
            return
        if key == "pairing_button_lock":
            out["tx_pairing_lock"] = _as_bool(value)
            return
        if key not in _LINKED_TX_KEYS or not self._tx_active.get(n):
            return
        # The linked transmitter's own values.
        if key == "acoustic":
            out["tx_capsule"] = _text(value)
        elif key == "bat_gauge":
            out["tx_battery_percent"] = _as_int(value)
        elif key == "bat_lifetime":
            seconds = _as_int(value)
            out["tx_battery_minutes"] = None if seconds is None else seconds // 60
        elif key == "bat_bars":
            out["tx_battery_bars"] = _as_int(value)
        elif key == "bat_health":
            out["tx_battery_health_percent"] = _as_int(value)
        elif key == "bat_cycles":
            out["tx_battery_cycles"] = _as_int(value)
        elif key == "bat_charging":
            out["tx_charging"] = _as_bool(value)
        elif key == "bat_type":
            out["tx_battery_type"] = _mapped(_BATTERY_TYPES, value)
        elif key == "device_type":
            out["tx_type"] = _mapped(_TX_TYPES, value)
        elif key == "gooseneck_state":
            out["tx_gooseneck_attached"] = _as_bool(value)
        elif key == "switch1" and rest[1:] == ("label",):
            out["tx_switch_label"] = _text(value)
        elif key == "warnings":
            warnings = _text(value)
            out["tx_warning"] = warnings
            out["tx_low_battery"] = "low bat" in warnings.lower()

    def _apply_link(self, n: int, active: bool | None, out: dict[str, Any]) -> None:
        if active is None:
            return
        was = self._tx_active.get(n)
        self._tx_active[n] = active
        out["tx_linked"] = active
        out["no_link"] = not active
        if not active:
            out.update(_LINKED_TX_CLEARED)
        elif not was and n <= self._channel_count:
            # A new link: the transmitter's values are subscribed now.
            self._spawn(self._link_up(n))

    async def _link_up(self, n: int) -> None:
        try:
            await self._subscribe_group(f"tx{n}")
            await self._subscribe_group(f"tx{n}_extra")
            await self._get(("mates", f"tx{n}", "switch1", "label"))
        except (ConnectionError, OSError):
            pass

    # ── Meters ──

    def _apply_meter(self, rest: tuple[str, ...], value: Any) -> None:
        if not self._meters_on or self.get_state("standby"):
            return
        level = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        if rest == ("mixer", "level"):
            self._queue_meter(None, "mix_level_dbfs", level)
            return
        if len(rest) != 2:
            return
        m = _RX_RE.match(rest[0])
        if not m or not (1 <= int(m.group(1)) <= self._channel_count):
            return
        key = {"level": "input_level_dbfs", "channel_level": "level_dbfs"}.get(rest[1])
        if key:
            self._queue_meter(int(m.group(1)), key, level)

    def _queue_meter(self, n: int | None, key: str, value: Any) -> None:
        """Meters update at most once per Meter Interval: the receiver sends a
        notification on every change, and no rate limit is documented for
        its subscriptions."""
        self._meter_pending[(n, key)] = value
        if self._meter_flush is None:
            self._meter_flush = asyncio.get_running_loop().call_later(
                self._meter_s, self._flush_meters)

    def _flush_meters(self) -> None:
        self._meter_flush = None
        pending, self._meter_pending = self._meter_pending, {}
        by_channel: dict[int, dict[str, Any]] = {}
        for (n, key), value in pending.items():
            if n is None:
                self.set_state(key, value)
            else:
                by_channel.setdefault(n, {})[key] = value
        for n, updates in by_channel.items():
            self.set_child_state_batch("channel", n, updates)
