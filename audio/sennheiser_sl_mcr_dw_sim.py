"""
Sennheiser SpeechLine Multi-Channel Receiver DW simulator.

Plays an SL MCR 4 DW (or 2 DW) from Sennheiser TI 1094 v4.3: SSC JSON over
UDP, one object per datagram. A null argument reads an address and any other
value writes it; the reply carries the value now in force. An unknown address
answers 404, a read-only one or a value off its range 406, in the
``{"osc":{"error":[..]}}`` shape of §11.4.

Subscriptions follow §5 and §11.11: the request is echoed with the addresses
that were accepted (refused ones answer an error), an initial notification
follows, and every change is notified until the lifetime (10 s unless the
request sets ``"#":{"lifetime":N}``) or 1000 notifications run out, when a
310 ends it. Renewing is the same request again; ``"#":{"cancel":true}``
ends it. Notifications are written the way the document's own examples
write a bundle, one top-level key per address with the key repeated
(``{"rx1":{"mute_mode":"ON"},"rx1":{"mute_state":false}}``), so the
driver's merge of repeated keys is what is exercised.

The channel count follows the device config (``channel_count``) and the
product name follows the count (``SLDW4CH`` / ``SLDW2CH``); rx3 and rx4 answer
404 on a two-channel unit. Channel 1 has a rechargeable handheld linked,
channel 2 a table-stand with its gooseneck, channel 4 a boundary microphone
on primary cells, and channel 3 nothing. The transmitter's own values answer
406 while it is not linked, and the rechargeable-only and table-stand-only
values answer 406 on a transmitter without them.

Standby (``/device/standby`` true) silences the receiver and drops its
subscriptions until a Wake-on-LAN magic packet for its MAC arrives. A
restart, a factory reset and a network-mode change send
``{"osc":{"state":{"close":true}}}`` to every subscriber, go silent for three
seconds and drop the subscriptions. Pairing counts down from 120 s; Simulator
UI changes (the ``ch<n>_*`` keys and the device keys) are notified to
subscribers like a change made on the receiver. Meters stream every 200 ms
while subscribed.

Deliberate fidelity gaps: the receiver's boot time is not known, so three
seconds stands in for it; network addresses and the time are not writable;
``/osc/schema`` and ``/osc/limits`` are not answered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any, Callable

from openavc.simulator.udp_simulator import UDPSimulator

logger = logging.getLogger(__name__)


DEFAULT_CHANNEL_COUNT = 4
_MAC = "00:1b:66:c3:51:cd"
_DEFAULT_LIFETIME = 10
_MAX_NOTIFICATIONS = 1000
_RESTART_SILENCE_S = 3.0
_WAKE_DELAY_S = 2.0
_METER_EVERY_S = 0.2
_PAIR_SECONDS = 120

_LINK_STATES = ["NOT_CONNECTED", "CONNECTED", "PAIRING", "FWU_OTA",
                "FWU_OTA_CONFIRMATION", "TRANSMITTER_UPDATE_FAILED", "WALKTEST"]
_MUTE_MODES = ["OFF", "ON", "PUSH_TO_TALK", "PUSH_TO_MUTE", "ROOM_MUTE"]
_EQ_PRESETS = ["OFF", "FEMALE_SPEECH", "MALE_SPEECH", "MEDIA", "CUSTOM"]
_AGC = ["AUTOMATIC", "LEVEL1_0DB", "LEVEL2_6DB", "LEVEL3_12DB", "LEVEL4_18DB",
        "LEVEL5_24DB", "LEVEL6_30DB"]
_AUTO_POWER_OFF = ["OFF", "10MIN", "20MIN", "30MIN"]
_NETWORK_MODES = ["CONFIG1", "CONFIG2", "CONFIG3"]
_DEVICE_STATES = ["NORMAL", "UPDATE", "UPDATE_FAILED", "ERROR"]
_DEVICE_WARNINGS = ["NONE", "HW_FAILURE", "MIXERDSP_FAILURE", "SETTINGS_SAVE_FAILED",
                    "UPDATE_FAILED", "SETTINGS_DEFAULTED"]
_TX_TYPES = ["HANDHELD", "BODYPACK", "TABLE-STAND", "BOUNDARY"]
_NAME_29 = re.compile(r"^[A-Za-z](?:[A-Za-z0-9_-]{0,27}[A-Za-z0-9])?$")
_NAME_8 = re.compile(r"^[A-Za-z](?:[A-Za-z0-9_-]{0,6}[A-Za-z0-9])?$")

Path = tuple[str, ...]


# ── JSON helpers (the driver's own shapes, written independently) ──

def _merge_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        old = out.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            stack = [(old, value)]
            while stack:
                dst, src = stack.pop()
                for k, v in src.items():
                    if isinstance(dst.get(k), dict) and isinstance(v, dict):
                        stack.append((dst[k], v))
                    else:
                        dst[k] = v
        else:
            out[key] = value
    return out


def _leaves(obj: Any, prefix: Path = ()):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, dict):
                yield from _leaves(value, prefix + (key,))
            else:
                yield prefix + (key,), value


def _put(tree: dict[str, Any], path: Path, value: Any) -> None:
    node = tree
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def _nested(items: list[tuple[Path, Any]]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for path, value in items:
        _put(tree, path, value)
    return tree


def _encode(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def _split(items: list[tuple[Path, Any]]) -> bytes:
    """One top-level key per address, repeated: TI 1094's bundled form."""
    parts = []
    for path, value in items:
        inner = json.dumps(value, separators=(",", ":"))
        for key in reversed(path[1:]):
            inner = "{" + json.dumps(key) + ":" + inner + "}"
        parts.append(json.dumps(path[0]) + ":" + inner)
    return ("{" + ",".join(parts) + "}").encode("utf-8")


def _error(failures: list[tuple[Path, int, str, str]]) -> bytes:
    tree: dict[str, Any] = {}
    for path, code, desc, reason in failures:
        info: dict[str, str] = {"desc": desc}
        if reason:
            info["reason"] = reason
        _put(tree, path, [code, info])
    return _encode({"osc": {"error": [tree]}})


def _magic_mac(data: bytes) -> str | None:
    """The MAC a Wake-on-LAN magic packet wakes, or None."""
    if len(data) < 102 or data[:6] != b"\xff" * 6:
        return None
    mac = data[6:12]
    if data[6:102] != mac * 16:
        return None
    return ":".join(f"{b:02x}" for b in mac)


class _Method:
    __slots__ = ("write", "sub", "check", "requires")

    def __init__(self, write: bool, sub: bool,
                 check: Callable[[Any], bool] | None = None,
                 requires: Callable[[], bool] | None = None):
        self.write = write
        self.sub = sub
        self.check = check
        self.requires = requires


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _one_of(options: list[str]) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, str) and v in options


def _gain(v: Any) -> bool:
    return (isinstance(v, int) and not isinstance(v, bool)
            and -24 <= v <= 12 and v % 6 == 0)


def _int_range(lo: int, hi: int) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


class SennheiserSlMcrDwSimulator(UDPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "sennheiser_sl_mcr_dw",
        "name": "Sennheiser SpeechLine Multi-Channel Receiver DW Simulator",
        "category": "audio",
        "transport": "udp",
        "default_port": 45,
        "initial_state": {
            "device_name": "SLDW4CH",
            "standby": False,
            "identifying": False,
            "led_brightness": 5,
            "device_state": "NORMAL",
            "device_warning": "NONE",
            "ch1_linked": True, "ch1_mute": False, "ch1_gain": 0,
            "ch1_battery": 67, "ch1_low_battery": False, "ch1_rf_quality": 92,
            "ch1_bad_link": False, "ch1_update_waiting": False,
            "ch2_linked": True, "ch2_mute": False, "ch2_gain": 0,
            "ch2_battery": 88, "ch2_low_battery": False, "ch2_rf_quality": 85,
            "ch2_bad_link": False, "ch2_update_waiting": False,
            "ch3_linked": False, "ch3_mute": False, "ch3_gain": 0,
            "ch3_battery": 0, "ch3_low_battery": False, "ch3_rf_quality": 0,
            "ch3_bad_link": False, "ch3_update_waiting": False,
            "ch4_linked": True, "ch4_mute": False, "ch4_gain": 0,
            "ch4_battery": 45, "ch4_low_battery": False, "ch4_rf_quality": 78,
            "ch4_bad_link": False, "ch4_update_waiting": False,
        },
        "controls": [
            {"type": "group", "label": "Receiver", "controls": [
                {"type": "indicator", "key": "device_name", "label": "Device Name"},
                {"type": "indicator", "key": "standby", "label": "Standby"},
                {"type": "indicator", "key": "identifying", "label": "Identifying"},
                {"type": "slider", "key": "led_brightness", "min": 0, "max": 5,
                 "label": "LED Brightness"},
                {"type": "select", "key": "device_state", "options": _DEVICE_STATES,
                 "label": "Receiver State"},
                {"type": "select", "key": "device_warning", "options": _DEVICE_WARNINGS,
                 "label": "Receiver Warning"},
            ]},
            {"type": "group", "label": "Channel 1", "controls": [
                {"type": "toggle", "key": "ch1_linked", "label": "Transmitter Linked"},
                {"type": "toggle", "key": "ch1_mute", "label": "Mute"},
                {"type": "slider", "key": "ch1_gain", "min": -24, "max": 12, "step": 6,
                 "unit": "dB", "label": "Gain"},
                {"type": "slider", "key": "ch1_battery", "min": 0, "max": 100,
                 "unit": "%", "label": "Battery"},
                {"type": "toggle", "key": "ch1_low_battery", "label": "Low Battery"},
                {"type": "slider", "key": "ch1_rf_quality", "min": 0, "max": 100,
                 "unit": "%", "label": "RF Quality"},
                {"type": "toggle", "key": "ch1_bad_link", "label": "Bad Link"},
                {"type": "toggle", "key": "ch1_update_waiting",
                 "label": "Transmitter Update Waiting"},
            ]},
            {"type": "group", "label": "Channel 2", "controls": [
                {"type": "toggle", "key": "ch2_linked", "label": "Transmitter Linked"},
                {"type": "toggle", "key": "ch2_mute", "label": "Mute"},
                {"type": "slider", "key": "ch2_gain", "min": -24, "max": 12, "step": 6,
                 "unit": "dB", "label": "Gain"},
                {"type": "slider", "key": "ch2_battery", "min": 0, "max": 100,
                 "unit": "%", "label": "Battery"},
                {"type": "toggle", "key": "ch2_low_battery", "label": "Low Battery"},
                {"type": "toggle", "key": "ch2_bad_link", "label": "Bad Link"},
            ]},
            {"type": "group", "label": "Channel 3", "controls": [
                {"type": "toggle", "key": "ch3_linked", "label": "Transmitter Linked"},
                {"type": "toggle", "key": "ch3_mute", "label": "Mute"},
            ]},
            {"type": "group", "label": "Channel 4", "controls": [
                {"type": "toggle", "key": "ch4_linked", "label": "Transmitter Linked"},
                {"type": "toggle", "key": "ch4_mute", "label": "Mute"},
                {"type": "slider", "key": "ch4_battery", "min": 0, "max": 100,
                 "unit": "%", "label": "Battery"},
            ]},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        count = int(self.config.get("channel_count", DEFAULT_CHANNEL_COUNT) or 4)
        self._count = 4 if count > 2 else 2
        self._handling = True
        self._values: dict[Path, Any] = {}
        self._methods: dict[Path, _Method] = {}
        # addr -> {path: [expires_at, notifications_sent]}
        self._subs: dict[tuple[str, int], dict[Path, list]] = {}
        self._standby = False
        self._down_until = 0.0
        self._tasks: list[asyncio.Task] = []
        self._ota_tasks: dict[int, asyncio.Task] = {}
        self._build_methods()
        self._seed()
        self._sync_ui()
        self._handling = False

    # ── The receiver's addresses ──

    def _linked(self, n: int) -> Callable[[], bool]:
        return lambda: bool(self._values.get(("mates", f"tx{n}", "active")))

    def _rechargeable(self, n: int) -> Callable[[], bool]:
        return lambda: (self._linked(n)()
                        and self._values.get(("mates", f"tx{n}", "bat_type")) == "RECHARGEABLE")

    def _table_stand(self, n: int) -> Callable[[], bool]:
        return lambda: (self._linked(n)()
                        and self._values.get(("mates", f"tx{n}", "device_type")) == "TABLE-STAND")

    def _charges(self, n: int) -> Callable[[], bool]:
        return lambda: (self._rechargeable(n)()
                        and self._values.get(("mates", f"tx{n}", "device_type"))
                        in ("TABLE-STAND", "BOUNDARY"))

    def _build_methods(self) -> None:
        m = self._methods
        RO, RW = False, True
        m[("interface", "version")] = _Method(RO, False)
        for key, check in (
            ("name", lambda v: _is_str(v) and bool(_NAME_29.match(v))),
            ("location", _is_str), ("position", _is_str), ("system", _is_str),
        ):
            m[("device", key)] = _Method(RW, True, check)
        m[("device", "dante", "name")] = _Method(RW, True, lambda v: _is_str(v) and 0 < len(v) <= 16)
        m[("device", "language")] = _Method(RW, False, _is_str)
        for key in ("product", "version", "serial", "vendor"):
            m[("device", "identity", key)] = _Method(RO, False)
        m[("device", "identification", "visual")] = _Method(
            RW, True, _is_bool, lambda: self._values[("device", "state")] != "UPDATE")
        m[("device", "led", "brightness")] = _Method(RW, True, _int_range(0, 5))
        m[("device", "network", "ether", "interfaces")] = _Method(RO, False)
        m[("device", "network", "ether", "macs")] = _Method(RO, False)
        m[("device", "network", "interface_mapping")] = _Method(RW, True, _one_of(_NETWORK_MODES))
        for key in ("interfaces", "auto", "gateway"):
            m[("device", "network", "ipv4", key)] = _Method(RO, False)
        for key in ("ipaddr", "netmask"):
            m[("device", "network", "ipv4", key)] = _Method(RO, True)
        m[("device", "network", "ipv6", "ipaddr")] = _Method(RO, True)
        m[("device", "network", "mdns")] = _Method(RW, True, _is_bool)
        m[("device", "state")] = _Method(RO, True)
        m[("device", "update", "progress")] = _Method(
            RO, True, None, lambda: self._values[("device", "state")] == "UPDATE")
        m[("device", "restart")] = _Method(RW, False, _is_bool)
        m[("device", "standby")] = _Method(RW, False, _is_bool)
        m[("device", "restore")] = _Method(RW, False, _one_of(["FACTORY_DEFAULTS", "AUDIO_DEFAULTS"]))
        m[("device", "warnings")] = _Method(RO, True)
        m[("audio", "out1", "desc")] = _Method(RO, False)
        m[("audio", "out1", "label")] = _Method(RO, False)
        m[("audio", "out1", "mixer", "gain")] = _Method(RO, True)
        m[("audio", "out2", "identity", "version")] = _Method(RO, False)
        m[("audio", "out2", "network", "ether", "macs")] = _Method(RO, False)
        m[("audio", "out2", "network", "ipv4", "ipaddr")] = _Method(RO, True)
        m[("audio", "dante", "mixer", "gain")] = _Method(RW, True, _gain)
        m[("m", "mixer", "level")] = _Method(RO, True)
        for n in range(1, self._count + 1):
            rx, tx = f"rx{n}", f"tx{n}"
            m[(rx, "state")] = _Method(RO, True)
            m[(rx, "identification", "visual")] = _Method(
                RW, True, _is_bool,
                lambda n=n: self._values[(f"rx{n}", "state")] not in ("PAIRING", "WALKTEST"))
            m[(rx, "update", "confirmation")] = _Method(
                RW, True, _is_bool,
                lambda n=n: self._values[(f"rx{n}", "state")] == "FWU_OTA_CONFIRMATION")
            m[(rx, "update", "progress")] = _Method(
                RO, True, None, lambda n=n: self._values[(f"rx{n}", "state")] == "FWU_OTA")
            m[(rx, "pair", "enable")] = _Method(
                RW, True, _is_bool,
                lambda n=n: (self._values[(f"rx{n}", "state")] != "WALKTEST"
                             and not self._values[(f"rx{n}", "identification", "visual")]))
            m[(rx, "pair", "progress")] = _Method(
                RO, True, None, lambda n=n: self._values[(f"rx{n}", "state")] == "PAIRING")
            m[(rx, "rf_quality")] = _Method(RO, True)
            m[(rx, "walktest")] = _Method(
                RW, True, _is_bool,
                lambda n=n: self._values[(f"rx{n}", "state")] in ("CONNECTED", "WALKTEST"))
            m[(rx, "mates")] = _Method(RO, True)
            m[(rx, "mute_mode")] = _Method(RW, True, _one_of(_MUTE_MODES))
            m[(rx, "mute_state")] = _Method(RW, True, _is_bool)
            m[(rx, "name")] = _Method(RW, True, lambda v: _is_str(v) and bool(_NAME_8.match(v)))
            m[(rx, "master_follower", "sync_info")] = _Method(RO, True)
            m[(rx, "rfpi")] = _Method(RO, True)
            m[(rx, "last_paired_ipei")] = _Method(RO, True)
            m[(rx, "warnings")] = _Method(RO, True)
            mates = ("mates", tx)
            m[mates + ("acoustic",)] = _Method(RO, True)
            m[mates + ("active",)] = _Method(RO, True)
            m[mates + ("agc",)] = _Method(RW, True, _one_of(_AGC))
            for key in ("bat_bars", "bat_cycles", "bat_health", "bat_lifetime"):
                m[mates + (key,)] = _Method(RO, True, None, self._rechargeable(n))
            m[mates + ("bat_charging",)] = _Method(RO, True, None, self._charges(n))
            for key in ("bat_gauge", "bat_type", "device_type", "warnings"):
                m[mates + (key,)] = _Method(RO, True, None, self._linked(n))
            m[mates + ("gooseneck_state",)] = _Method(RO, True, None, self._table_stand(n))
            m[mates + ("led_active",)] = _Method(RW, True, _is_bool)
            m[mates + ("switch1", "label")] = _Method(RO, False, None, self._linked(n))
            m[mates + ("power_down",)] = _Method(RW, False, _is_bool, self._linked(n))
            m[mates + ("auto_power_off",)] = _Method(RW, True, _one_of(_AUTO_POWER_OFF))
            m[mates + ("power_lock",)] = _Method(RW, True, _is_bool)
            m[mates + ("pairing_button_lock",)] = _Method(RW, True, _is_bool)
            m[("audio", rx, "gain")] = _Method(RW, True, _gain)
            m[("audio", rx, "equalizer", "preset")] = _Method(RW, True, _one_of(_EQ_PRESETS))
            m[("audio", rx, "low_cut")] = _Method(RW, True, _is_bool)
            m[("audio", rx, "restore")] = _Method(RW, False, _is_bool)
            m[("m", rx, "level")] = _Method(RO, True)
            m[("m", rx, "channel_level")] = _Method(RO, True)

    def _seed(self, factory: bool = False) -> None:
        v = self._values
        product = f"SLDW{self._count}CH"
        if not factory:
            v.update({
                ("interface", "version"): "1.3",
                ("device", "identity", "product"): product,
                ("device", "identity", "version"): "4.1.4",
                ("device", "identity", "serial"): "1050000515",
                ("device", "identity", "vendor"): "Sennheiser electronic GmbH & Co. KG",
                ("device", "network", "ether", "interfaces"): ["eth0"],
                ("device", "network", "ether", "macs"): [_MAC],
                ("device", "network", "ipv4", "interfaces"): [1],
                ("device", "network", "ipv4", "auto"): [True],
                ("device", "network", "ipv4", "ipaddr"): ["192.168.2.5"],
                ("device", "network", "ipv4", "netmask"): ["255.255.255.0"],
                ("device", "network", "ipv4", "gateway"): ["192.168.2.1"],
                ("device", "network", "ipv6", "ipaddr"): ["fe80::21b:66ff:fec3:51cd%eth0"],
                ("device", "state"): "NORMAL",
                ("device", "update", "progress"): 0,
                ("device", "warnings"): "NONE",
                ("audio", "out1", "desc"): "Analog audio output",
                ("audio", "out1", "label"): "Analog Out",
                ("audio", "out1", "mixer", "gain"): 0,
                ("audio", "out2", "identity", "version"): "4.2.1",
                ("audio", "out2", "network", "ether", "macs"): ["00:1b:66:c3:51:cb", "00:00:00:00:00:00"],
                ("audio", "out2", "network", "ipv4", "ipaddr"): ["169.254.242.172", "0.0.0.0"],
                ("m", "mixer", "level"): -60,
            })
        v.update({
            ("device", "name"): product,
            ("device", "dante", "name"): f"SLMCR{self._count}-c351cb",
            ("device", "location"): "Meeting Room A",
            ("device", "position"): "",
            ("device", "system"): "System",
            ("device", "language"): "en_GB",
            ("device", "identification", "visual"): False,
            ("device", "led", "brightness"): 5,
            ("device", "network", "interface_mapping"): "CONFIG1",
            ("device", "network", "mdns"): True,
            ("device", "standby"): False,
            ("device", "restart"): False,
            ("device", "restore"): "",
            ("audio", "dante", "mixer", "gain"): 0,
        })
        transmitters = {
            1: ("HANDHELD", "MD42", "RECHARGEABLE", 67, False),
            2: ("TABLE-STAND", "GOOSENECK", "RECHARGEABLE", 88, True),
            4: ("BOUNDARY", "BOUNDARY", "BATTERY", 45, False),
        }
        for n in range(1, self._count + 1):
            rx, tx = f"rx{n}", ("mates", f"tx{n}")
            linked = n in transmitters and not factory
            v.update({
                (rx, "name"): f"rx{n}",
                (rx, "state"): "CONNECTED" if linked else "NOT_CONNECTED",
                (rx, "identification", "visual"): False,
                (rx, "update", "confirmation"): False,
                (rx, "update", "progress"): 0,
                (rx, "pair", "enable"): False,
                (rx, "pair", "progress"): 0,
                (rx, "rf_quality"): 90 if linked else 0,
                (rx, "walktest"): False,
                (rx, "mates"): [f"mates/tx{n}"] if linked else [],
                (rx, "mute_mode"): "ON",
                (rx, "mute_state"): False,
                (rx, "master_follower", "sync_info"): "MASTER" if n == 1 else "FOLLOWER",
                (rx, "rfpi"): f"028112cf2{n}",
                (rx, "last_paired_ipei"): f"028110a93{n}" if linked else "",
                (rx, "warnings"): [] if linked else ["No Link"],
                ("audio", rx, "gain"): 0,
                ("audio", rx, "equalizer", "preset"): "OFF",
                ("audio", rx, "low_cut"): False,
                ("audio", rx, "restore"): False,
                ("m", rx, "level"): -60,
                ("m", rx, "channel_level"): -60,
                tx + ("agc",): "AUTOMATIC",
                tx + ("led_active",): True,
                tx + ("auto_power_off",): "OFF",
                tx + ("power_lock",): False,
                tx + ("pairing_button_lock",): False,
                tx + ("power_down",): False,
            })
            kind, capsule, battery, gauge, goose = transmitters.get(
                n, ("HANDHELD", "", "RECHARGEABLE", 0, False))
            self._set_transmitter(n, linked, kind, capsule, battery, gauge, goose)

    def _set_transmitter(self, n: int, linked: bool, kind: str = "HANDHELD",
                         capsule: str = "MD42", battery: str = "RECHARGEABLE",
                         gauge: int = 80, goose: bool = False) -> None:
        tx = ("mates", f"tx{n}")
        self._values.update({
            tx + ("active",): linked,
            tx + ("acoustic",): capsule if linked else "",
            tx + ("device_type",): kind,
            tx + ("bat_type",): battery,
            tx + ("bat_gauge",): gauge,
            tx + ("bat_lifetime",): gauge * 540,
            tx + ("bat_bars",): max(1, gauge // 20) if gauge else 0,
            tx + ("bat_health",): 100,
            tx + ("bat_cycles",): 12,
            tx + ("bat_charging",): False,
            tx + ("gooseneck_state",): goose,
            tx + ("warnings",): [],
            tx + ("switch1", "label"): "Mute",
        })

    # ── Simulator UI ──

    def _sync_ui(self) -> None:
        """Mirror the model into the Simulator UI keys, without notifying."""
        handling, self._handling = self._handling, True
        try:
            v = self._values
            self.set_state("device_name", v[("device", "name")])
            self.set_state("standby", self._standby)
            self.set_state("identifying", v[("device", "identification", "visual")])
            self.set_state("led_brightness", v[("device", "led", "brightness")])
            self.set_state("device_state", v[("device", "state")])
            self.set_state("device_warning", v[("device", "warnings")])
            for n in range(1, self._count + 1):
                tx = ("mates", f"tx{n}")
                self.set_state(f"ch{n}_linked", bool(v[tx + ("active",)]))
                self.set_state(f"ch{n}_mute", bool(v[(f"rx{n}", "mute_state")]))
                self.set_state(f"ch{n}_gain", v[("audio", f"rx{n}", "gain")])
                self.set_state(f"ch{n}_battery", v[tx + ("bat_gauge",)])
                self.set_state(f"ch{n}_rf_quality", v[(f"rx{n}", "rf_quality")])
        finally:
            self._handling = handling

    def set_state(self, key: str, value) -> None:
        old = self.state.get(key)
        super().set_state(key, value)
        # The base class seeds initial_state before this subclass has set up.
        if getattr(self, "_handling", True) or old == value:
            return
        changed = self._apply_ui(key, value)
        self._sync_ui()
        self._notify(changed)

    def _apply_ui(self, key: str, value: Any) -> list[Path]:
        """A Simulator UI change becomes the change the receiver reports."""
        v = self._values
        if key == "led_brightness":
            v[("device", "led", "brightness")] = int(value)
            return [("device", "led", "brightness")]
        if key == "device_state":
            v[("device", "state")] = str(value)
            return [("device", "state")]
        if key == "device_warning":
            v[("device", "warnings")] = str(value)
            return [("device", "warnings")]
        m = re.match(r"^ch(\d+)_(\w+)$", key)
        if not m or not (1 <= int(m.group(1)) <= self._count):
            return []
        n, field = int(m.group(1)), m.group(2)
        rx, tx = f"rx{n}", ("mates", f"tx{n}")
        if field == "linked":
            return self._link(n, bool(value))
        if field == "mute":
            v[(rx, "mute_state")] = bool(value)
            return [(rx, "mute_state")]
        if field == "gain":
            v[("audio", rx, "gain")] = max(-24, min(12, int(value) // 6 * 6))
            return [("audio", rx, "gain")]
        if field == "rf_quality":
            v[(rx, "rf_quality")] = int(value)
            return [(rx, "rf_quality")]
        if field == "bad_link":
            v[(rx, "warnings")] = ["Bad Link"] if value else []
            return [(rx, "warnings")]
        if not v[tx + ("active",)]:
            return []
        if field == "battery":
            v[tx + ("bat_gauge",)] = int(value)
            v[tx + ("bat_lifetime",)] = int(value) * 540
            return [tx + ("bat_gauge",), tx + ("bat_lifetime",)]
        if field == "low_battery":
            v[tx + ("warnings",)] = ["Low Bat"] if value else []
            return [tx + ("warnings",)]
        if field == "update_waiting" and value:
            v[(rx, "state")] = "FWU_OTA_CONFIRMATION"
            return [(rx, "state")]
        return []

    def _link(self, n: int, linked: bool) -> list[Path]:
        rx, tx = f"rx{n}", ("mates", f"tx{n}")
        was = self._values[tx + ("active",)]
        if linked == was:
            return []
        if linked:
            kind = self._values[tx + ("device_type",)]
            self._set_transmitter(n, True, kind, "MD42" if kind == "HANDHELD" else kind,
                                  self._values[tx + ("bat_type",)], 80)
        else:
            self._values[tx + ("active",)] = False
            self._values[tx + ("acoustic",)] = ""
        self._values[(rx, "state")] = "CONNECTED" if linked else "NOT_CONNECTED"
        self._values[(rx, "pair", "enable")] = False
        self._values[(rx, "pair", "progress")] = 0
        self._values[(rx, "mates")] = [f"mates/tx{n}"] if linked else []
        self._values[(rx, "warnings")] = [] if linked else ["No Link"]
        self._values[(rx, "rf_quality")] = 90 if linked else 0
        if linked:
            self._values[(rx, "last_paired_ipei")] = f"028110a93{n}"
        changed = [tx + ("active",), tx + ("acoustic",), (rx, "state"), (rx, "pair", "enable"),
                   (rx, "mates"), (rx, "warnings"), (rx, "rf_quality"), (rx, "last_paired_ipei")]
        if linked:
            changed += [tx + (k,) for k in ("device_type", "bat_type", "bat_gauge", "bat_lifetime",
                                            "bat_bars", "bat_health", "bat_cycles", "warnings")]
        return changed

    # ── Lifecycle ──

    async def start(self, port: int) -> None:
        await super().start(port)
        self._tasks = [
            asyncio.ensure_future(self._housekeeping()),
            asyncio.ensure_future(self._meters()),
        ]

    async def stop(self) -> None:
        for task in self._tasks + list(self._ota_tasks.values()):
            task.cancel()
        self._tasks = []
        self._ota_tasks.clear()
        await super().stop()

    def _down(self) -> bool:
        return self._standby or time.monotonic() < self._down_until

    # ── Datagrams ──

    def handle_command(self, data: bytes) -> bytes | None:
        """The receiver's first reply to one datagram (no subscriptions)."""
        replies = self._handle(data, None)
        return replies[0] if replies else None

    async def dispatch_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._standby:
            if _magic_mac(data) == _MAC:
                asyncio.ensure_future(self._wake())
            return
        if self._down():
            return
        for reply in self._handle(data, addr):
            self.send_datagram(reply, addr)

    def _handle(self, data: bytes, addr: tuple[str, int] | None) -> list[bytes]:
        if _magic_mac(data) is not None:
            return []
        try:
            message = json.loads(data.decode("utf-8"), object_pairs_hook=_merge_pairs)
        except (UnicodeDecodeError, ValueError):
            return [_encode({"osc": {"error": [400, {"desc": "Bad Request"}]}})]
        if not isinstance(message, dict):
            return [_encode({"osc": {"error": [400, {"desc": "Bad Request"}]}})]

        replies: list[bytes] = []
        osc = message.pop("osc", None)
        if isinstance(osc, dict):
            replies += self._handle_osc(osc, addr)
        if not message:
            return replies

        answers: list[tuple[Path, Any]] = []
        failures: list[tuple[Path, int, str, str]] = []
        changed: list[Path] = []
        after: list[Callable[[], None]] = []
        for path, value in _leaves(message):
            method = self._methods.get(path)
            if method is None:
                failures.append((path, 404, "Not found", ""))
                continue
            if value is None:
                if method.requires and not method.requires():
                    failures.append((path, 406, "Not accepted", "not available"))
                else:
                    answers.append((path, self._values.get(path)))
                continue
            if not method.write:
                failures.append((path, 406, "Not accepted", "command is read-only"))
                continue
            if method.check and not method.check(value):
                failures.append((path, 406, "Not accepted", "invalid value"))
                continue
            if method.requires and not method.requires():
                failures.append((path, 406, "Not accepted", "not available"))
                continue
            changed += self._write(path, value, after)
            answers.append((path, self._values.get(path)))
        if answers:
            replies.append(_encode(_nested(answers)))
        if failures:
            replies.append(_error(failures))
        if changed:
            self._sync_ui()
            self._notify(changed)
        for action in after:
            action()
        return replies

    def _handle_osc(self, osc: dict[str, Any], addr: tuple[str, int] | None) -> list[bytes]:
        replies: list[bytes] = []
        if "ping" in osc:
            replies.append(_encode({"osc": {"ping": osc["ping"]}}))
        if "version" in osc:
            replies.append(_encode({"osc": {"version": "1.2"}}))
        state = osc.get("state")
        if isinstance(state, dict) and "subscribe" in state:
            replies += self._subscribe(state["subscribe"], addr)
        return replies

    # ── Writes and their side effects ──

    def _write(self, path: Path, value: Any, after: list[Callable[[], None]]) -> list[Path]:
        v = self._values
        v[path] = value
        changed = [path]
        head = path[0]
        if path == ("device", "standby") and value:
            after.append(self._enter_standby)
        elif path == ("device", "restart") and value:
            v[path] = True
            after.append(self._restart)
        elif path == ("device", "restore"):
            if value == "FACTORY_DEFAULTS":
                after.append(lambda: self._restart(factory=True))
            else:
                changed += self._audio_defaults()
        elif path == ("device", "network", "interface_mapping"):
            after.append(self._restart)
        elif head.startswith("rx") and len(path) >= 2:
            n = int(head[2:])
            rx = head
            if path[1:] == ("pair", "enable"):
                if value:
                    v[(rx, "state")] = "PAIRING"
                    v[(rx, "pair", "progress")] = _PAIR_SECONDS
                else:
                    v[(rx, "state")] = "CONNECTED" if v[("mates", f"tx{n}", "active")] else "NOT_CONNECTED"
                    v[(rx, "pair", "progress")] = 0
                changed += [(rx, "state"), (rx, "pair", "progress")]
            elif path[1:] == ("walktest",):
                v[(rx, "state")] = "WALKTEST" if value else "CONNECTED"
                changed.append((rx, "state"))
            elif path[1:] == ("update", "confirmation"):
                if value:
                    v[(rx, "state")] = "FWU_OTA"
                    v[(rx, "update", "progress")] = 0
                    changed += [(rx, "state"), (rx, "update", "progress")]
                    self._ota_tasks[n] = asyncio.ensure_future(self._transmitter_update(n))
                else:
                    changed += self._link(n, False)
        elif path[:1] == ("mates",) and path[2:] == ("power_down",) and value:
            n = int(path[1][2:])
            v[path] = False
            changed = self._link(n, False)
        elif path[:1] == ("audio",) and path[2:] == ("restore",) and value:
            changed += self._channel_audio_defaults(path[1])
        return changed

    def _channel_audio_defaults(self, rx: str) -> list[Path]:
        paths = [("audio", rx, "gain"), ("audio", rx, "equalizer", "preset"), ("audio", rx, "low_cut")]
        self._values.update({paths[0]: 0, paths[1]: "OFF", paths[2]: False})
        return paths

    def _audio_defaults(self) -> list[Path]:
        changed: list[Path] = []
        for n in range(1, self._count + 1):
            changed += self._channel_audio_defaults(f"rx{n}")
        return changed

    async def _transmitter_update(self, n: int) -> None:
        rx = f"rx{n}"
        try:
            for progress in range(10, 101, 10):
                await asyncio.sleep(0.5)
                self._values[(rx, "update", "progress")] = progress
                self._notify([(rx, "update", "progress")])
            self._values[(rx, "state")] = "CONNECTED"
            self._notify([(rx, "state")])
        finally:
            self._ota_tasks.pop(n, None)

    def _announce_close(self) -> None:
        for addr in list(self._subs):
            self.push_datagram(_encode({"osc": {"state": {"close": True}}}), addr)

    def _enter_standby(self) -> None:
        self._subs.clear()
        self._standby = True
        self._sync_ui()

    async def _wake(self) -> None:
        await asyncio.sleep(_WAKE_DELAY_S)
        self._standby = False
        self._values[("device", "standby")] = False
        self._sync_ui()

    def _restart(self, factory: bool = False) -> None:
        self._announce_close()
        self._subs.clear()
        if factory:
            self._seed(factory=True)
            self._values[("device", "warnings")] = "SETTINGS_DEFAULTED"
        self._values[("device", "restart")] = False
        self._values[("device", "restore")] = ""
        self._down_until = time.monotonic() + _RESTART_SILENCE_S
        self._sync_ui()

    # ── Subscriptions ──

    def _subscribe(self, request: Any, addr: tuple[str, int] | None) -> list[bytes]:
        if not isinstance(request, list):
            return [_encode({"osc": {"error": [{"osc": {"state": {"subscribe": [400, {"desc": "Bad Request"}]}}}]}})]
        if len(request) != 1 or not isinstance(request[0], dict):
            # §5.3: one address tree per request is all this server takes.
            return [_encode({"osc": {"error": [{"osc": {"state": {"subscribe": [414, {"desc": "Request Too Complex"}]}}}]}})]
        tree = dict(request[0])
        params = tree.pop("#", None) or {}
        lifetime = params.get("lifetime", _DEFAULT_LIFETIME)
        if not isinstance(lifetime, int) or isinstance(lifetime, bool) or lifetime <= 0:
            lifetime = _DEFAULT_LIFETIME
        cancel = params.get("cancel") is True
        accepted: list[Path] = []
        failures: list[tuple[Path, int, str, str]] = []
        for path, _ in _leaves(tree):
            method = self._methods.get(path)
            if method is None:
                failures.append((path, 404, "Not found", ""))
            elif not method.sub:
                failures.append((path, 406, "Not accepted", "not subscribable"))
            elif method.requires and not method.requires():
                failures.append((path, 406, "Not accepted", "not available"))
            else:
                accepted.append(path)

        echo: dict[str, Any] = {"#": {"cancel": True} if cancel else {"lifetime": lifetime}}
        for path in accepted:
            _put(echo, path, None)
        replies = [_encode({"osc": {"state": {"subscribe": [echo]}}})]
        if failures:
            replies.append(_error(failures))
        if addr is None:
            return replies
        subs = self._subs.setdefault(addr, {})
        if cancel:
            for path in accepted:
                subs.pop(path, None)
            return replies
        expires = time.monotonic() + lifetime
        for path in accepted:
            subs[path] = [expires, 1]
        if accepted:
            replies.append(_split([(p, self._values.get(p)) for p in accepted]))
        return replies

    def _notify(self, changed: list[Path]) -> None:
        if not changed or self._down():
            return
        seen: set[Path] = set()
        unique = [p for p in changed if not (p in seen or seen.add(p))]
        for addr, subs in list(self._subs.items()):
            items = [(p, self._values.get(p)) for p in unique if p in subs]
            if not items:
                continue
            self.push_datagram(_split(items), addr)
            ended = []
            for path, _ in items:
                subs[path][1] += 1
                if subs[path][1] >= _MAX_NOTIFICATIONS:
                    ended.append(path)
            if ended:
                self._terminate(addr, ended)

    def _terminate(self, addr: tuple[str, int], paths: list[Path]) -> None:
        subs = self._subs.get(addr, {})
        for path in paths:
            subs.pop(path, None)
        # The document's examples end a bundle in the same repeated-key form.
        body = _split([(p, [310]) for p in paths]).decode("utf-8")
        self.push_datagram(('{"osc":{"error":[' + body + "]}}").encode("utf-8"), addr)

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self._down():
                continue
            now = time.monotonic()
            for addr, subs in list(self._subs.items()):
                expired = [p for p, (expires, _) in subs.items() if expires <= now]
                if expired:
                    self._terminate(addr, expired)
                if not subs:
                    self._subs.pop(addr, None)
            changed: list[Path] = []
            for n in range(1, self._count + 1):
                rx = f"rx{n}"
                if self._values[(rx, "state")] == "PAIRING":
                    left = int(self._values[(rx, "pair", "progress")]) - 1
                    self._values[(rx, "pair", "progress")] = max(0, left)
                    changed.append((rx, "pair", "progress"))
                    if left <= 0:
                        self._values[(rx, "pair", "enable")] = False
                        self._values[(rx, "state")] = (
                            "CONNECTED" if self._values[("mates", f"tx{n}", "active")]
                            else "NOT_CONNECTED")
                        changed += [(rx, "pair", "enable"), (rx, "state")]
            self._notify(changed)

    async def _meters(self) -> None:
        while True:
            await asyncio.sleep(_METER_EVERY_S)
            if self._down() or not any(
                p[0] == "m" for subs in self._subs.values() for p in subs
            ):
                continue
            changed: list[Path] = []
            mix = -60
            for n in range(1, self._count + 1):
                rx = f"rx{n}"
                live = (self._values[("mates", f"tx{n}", "active")]
                        and not self._values[(rx, "mute_state")])
                level = random.randint(-34, -14) if live else -60
                post = max(-60, min(0, level + int(self._values[("audio", rx, "gain")])))
                if not live:
                    post = -60
                self._values[("m", rx, "level")] = level
                self._values[("m", rx, "channel_level")] = post
                mix = max(mix, post)
                changed += [("m", rx, "level"), ("m", rx, "channel_level")]
            self._values[("m", "mixer", "level")] = mix
            changed.append(("m", "mixer", "level"))
            self._notify(changed)
