"""
Shure Axient Digital receiver simulator.

Plays an AD4D / AD4Q from Shure's "Axient Digital Command Strings": ``< GET ``
/ ``SET`` / ``REP >`` frames over TCP 2202 with ``>`` as the delimiter, one
``REP`` per property in a ``GET x ALL`` dump, index 0 fanning a GET out to
every channel, ``INC`` / ``DEC`` on the gain and the slot offset, the
FREQUENCY / GROUP_CHANNEL pair reporting each other, the SAMPLE frame whose
layout follows Quadversity and the channel's frequency-diversity mode, and the
eight transmitter slots per channel with the SET refusals a slot that is not
LINKED.ACTIVE answers.

The channel count follows the device config (``channel_count``: 4 for an
AD4Q, 2 for an AD4D) and the MODEL string follows the count. Channel 2 runs in
FD-C mode so the two-section SAMPLE layout and FREQUENCY2 are exercised;
channel 1 is receiving an ADX1 (slot 2, LINKED.ACTIVE), slot 1 holds an AD2
(STANDARD) and slot 3 an ADX2 that is off (LINKED.INACTIVE); the other slots
are EMPTY.

Simulator UI keys (``ch<n>_*``, ``slot_<n>-<s>_status``, the device keys) are
mirrored to the wire: a value changed in the UI is pushed as the REP the
receiver would send, so the driver's push path can be watched. Deliberate
fidelity gaps: any frequency in the band is accepted (the receiver enforces
its tuning step), a HIGH power mode is always allowed, and NET_SETTINGS
writes are acknowledged but change nothing.
"""

from __future__ import annotations

import asyncio
import logging

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


DEFAULT_CHANNEL_COUNT = 4
SLOTS_PER_CHANNEL = 8

_GAIN_OFFSET = 18
_LEVEL_OFFSET = 120
_OFFSET_OFFSET = 12
_TEMP_OFFSET = 40

_POWER_BY_MODE = {"LOW": 2, "NORMAL": 10, "HIGH": 40}

# A tiny group/channel table for the simulated band so a preset maps to a
# frequency the way the receiver's does.
_GROUP_TABLE = {
    ("1", "1"): 606025, ("1", "2"): 608350, ("1", "3"): 611975,
    ("6", "6"): 614650, ("6", "100"): 652875,
}


def _pad(text: str, width: int) -> str:
    return "{" + text.ljust(width)[:width] + "}"


def _new_transmitter(model: str, name: str, **over) -> dict:
    tx = {
        "model": model, "name": name, "batt_type": "LION", "bars": 4,
        "percent": 88, "mins": 125, "health": 97, "cycles": 19, "temp_c": 22,
        "pad": False, "offset": 0, "polarity": "POSITIVE", "power": 10,
        "lock": "NONE", "mute_mode": "ON", "talk": "OFF",
    }
    tx.update(over)
    return tx


def _new_slot(status: str = "EMPTY", model: str = "", name: str = "", **over) -> dict:
    slot = {
        "status": status, "model": model, "name": name, "batt_type": "LION",
        "bars": 4, "percent": 87, "mins": 360, "health": 97, "cycles": 13,
        "pad": False, "offset": 0, "polarity": "POSITIVE", "rf_output": "RF_ON",
        "rf_mode": "NORMAL", "showlink": 5,
    }
    slot.update(over)
    return slot


class ShureAxientDigitalSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "shure_axient_digital",
        "name": "Shure Axient Digital Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 2202,
        "delimiter": ">",
        "initial_state": {
            "device_name": "AD4Q-SIM",
            "model": "AD4Q-A",
            "firmware": "2.0.15.2",
            "rf_band": "G55",
            "encryption": False,
            "quadversity": False,
            "transmission_mode": "STANDARD",
            "identifying": False,
            "ch1_mute": False, "ch1_gain_db": 12, "ch1_frequency_khz": 606025,
            "ch1_tx_linked": True, "ch1_battery_bars": 4,
            "ch1_battery_percent": 88, "ch1_battery_minutes": 125,
            "ch1_interference": False, "ch1_encryption_error": False,
            "ch1_tx_mute": False, "ch1_talk_switch": False,
            "ch2_mute": False, "ch2_gain_db": 0, "ch2_frequency_khz": 578350,
            "ch2_tx_linked": False, "ch2_battery_bars": 0,
            "ch2_battery_percent": 0, "ch2_battery_minutes": 0,
            "ch2_interference": False, "ch2_encryption_error": False,
            "ch2_tx_mute": False, "ch2_talk_switch": False,
            "ch3_mute": False, "ch3_gain_db": 0, "ch3_frequency_khz": 611975,
            "ch3_tx_linked": False, "ch3_battery_bars": 0,
            "ch3_battery_percent": 0, "ch3_battery_minutes": 0,
            "ch3_interference": False, "ch3_encryption_error": False,
            "ch3_tx_mute": False, "ch3_talk_switch": False,
            "ch4_mute": False, "ch4_gain_db": 0, "ch4_frequency_khz": 608350,
            "ch4_tx_linked": False, "ch4_battery_bars": 0,
            "ch4_battery_percent": 0, "ch4_battery_minutes": 0,
            "ch4_interference": False, "ch4_encryption_error": False,
            "ch4_tx_mute": False, "ch4_talk_switch": False,
            "slot_1-1_status": "STANDARD",
            "slot_1-2_status": "LINKED.ACTIVE",
            "slot_1-3_status": "LINKED.INACTIVE",
        },
        "controls": [
            {"type": "group", "label": "Receiver", "controls": [
                {"type": "indicator", "key": "model", "label": "Model"},
                {"type": "indicator", "key": "device_name", "label": "Device Name"},
                {"type": "toggle", "key": "encryption", "label": "Encryption"},
                {"type": "toggle", "key": "quadversity", "label": "Quadversity"},
                {"type": "indicator", "key": "identifying", "label": "Identifying"},
            ]},
            {"type": "group", "label": "Channel 1", "controls": [
                {"type": "toggle", "key": "ch1_mute", "label": "Mute"},
                {"type": "slider", "key": "ch1_gain_db", "min": -18, "max": 42,
                 "unit": "dB", "label": "Gain"},
                {"type": "indicator", "key": "ch1_frequency_khz", "label": "Frequency (kHz)"},
                {"type": "toggle", "key": "ch1_tx_linked", "label": "Transmitter Linked"},
                {"type": "slider", "key": "ch1_battery_bars", "min": 0, "max": 5,
                 "label": "Battery Bars"},
                {"type": "slider", "key": "ch1_battery_percent", "min": 0, "max": 100,
                 "unit": "%", "label": "Battery %"},
                {"type": "slider", "key": "ch1_battery_minutes", "min": 0, "max": 900,
                 "unit": "min", "label": "Battery Minutes"},
                {"type": "toggle", "key": "ch1_interference", "label": "Interference"},
                {"type": "toggle", "key": "ch1_encryption_error", "label": "Encryption Error"},
                {"type": "toggle", "key": "ch1_tx_mute", "label": "TX Mute Switch"},
                {"type": "toggle", "key": "ch1_talk_switch", "label": "Talk Switch"},
            ]},
            {"type": "group", "label": "Channel 2", "controls": [
                {"type": "toggle", "key": "ch2_mute", "label": "Mute"},
                {"type": "slider", "key": "ch2_gain_db", "min": -18, "max": 42,
                 "unit": "dB", "label": "Gain"},
                {"type": "toggle", "key": "ch2_tx_linked", "label": "Transmitter Linked"},
                {"type": "slider", "key": "ch2_battery_bars", "min": 0, "max": 5,
                 "label": "Battery Bars"},
                {"type": "toggle", "key": "ch2_interference", "label": "Interference"},
            ]},
            {"type": "group", "label": "Slots (channel 1)", "controls": [
                {"type": "select", "key": "slot_1-1_status",
                 "options": ["EMPTY", "STANDARD", "LINKED.INACTIVE", "LINKED.ACTIVE"],
                 "label": "Slot 1"},
                {"type": "select", "key": "slot_1-2_status",
                 "options": ["EMPTY", "STANDARD", "LINKED.INACTIVE", "LINKED.ACTIVE"],
                 "label": "Slot 2"},
                {"type": "select", "key": "slot_1-3_status",
                 "options": ["EMPTY", "STANDARD", "LINKED.INACTIVE", "LINKED.ACTIVE"],
                 "label": "Slot 3"},
            ]},
        ],
        "delays": {"command_response": 0.01},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config
        count = int(cfg.get("channel_count", DEFAULT_CHANNEL_COUNT))
        self._count = max(1, min(4, count))
        model = "AD4Q-A" if self._count > 2 else "AD4D-A"
        self._handling = False
        self._meter_tasks: dict[int, asyncio.Task] = {}
        with self._quiet():
            self.set_state("model", model)
        # Per-channel protocol state, 1-based.
        self._ch: dict[int, dict] = {}
        for n in range(1, self._count + 1):
            self._ch[n] = {
                "mute": bool(self.state.get(f"ch{n}_mute", False)),
                "gain": int(self.state.get(f"ch{n}_gain_db", 0)) + _GAIN_OFFSET,
                "name": f"Channel {n}",
                "freq": int(self.state.get(f"ch{n}_frequency_khz", 606025)),
                "group": ("1", "1") if n != 2 else ("--", "--"),
                "fd": "FD-C" if n == 2 else "OFF",
                "freq2": 578850, "group2": ("--", "--"),
                "enc_error": False, "interference": False,
                "interference2": False, "unregistered": False,
                "flash": False, "meter_rate": 0,
                "tx": None,
                # Metered values: quality, audio bitmap, peak, rms and one
                # (bitmap, rssi) per antenna per RF section.
                "qual": 5, "aud_bitmap": 31, "peak": 102, "rms": 98,
                "rssi": [86, 65, 70, 72], "rssi2": [82, 60, 66, 68],
            }
        if self.state.get("ch1_tx_linked", True) and 1 in self._ch:
            self._ch[1]["tx"] = _new_transmitter("ADX1", "LeadVox")
        # Transmitter slots, keyed (channel, slot).
        self._slots: dict[tuple[int, int], dict] = {}
        for n in range(1, self._count + 1):
            for s in range(1, SLOTS_PER_CHANNEL + 1):
                self._slots[(n, s)] = _new_slot()
        if 1 in self._ch:
            self._slots[(1, 1)] = _new_slot("STANDARD", "AD2", "AD2")
            self._slots[(1, 2)] = _new_slot("LINKED.ACTIVE", "ADX1", "LeadVox")
            self._slots[(1, 3)] = _new_slot("LINKED.INACTIVE", "ADX2", "Guest")

    # ── Simulator UI mirroring ──

    class _Quiet:
        def __init__(self, sim):
            self.sim = sim

        def __enter__(self):
            self.sim._handling = True

        def __exit__(self, *exc):
            self.sim._handling = False

    def _quiet(self):
        return self._Quiet(self)

    def set_state(self, key: str, value) -> None:
        old = self.state.get(key)
        super().set_state(key, value)
        # The base class seeds initial_state before this subclass has set up:
        # nothing is on the wire yet, so there is nothing to mirror.
        if getattr(self, "_handling", True) or old == value:
            return
        frames = self._apply_ui_change(key, value)
        if frames:
            asyncio.ensure_future(self.push(frames))

    def _apply_ui_change(self, key: str, value) -> bytes:
        """A Simulator UI change becomes the REP the receiver would push."""
        if key == "encryption":
            return self._frame(f"REP ENCRYPTION_MODE {'ON' if value else 'OFF'}")
        if key == "quadversity":
            return self._frame(f"REP QUADVERSITY_MODE {'ON' if value else 'OFF'}")
        if key == "device_name":
            return self._frame(f"REP DEVICE_ID {_pad(str(value), 31)}")
        if key.startswith("slot_"):
            addr, _, _ = key[5:].partition("_")
            c, _, s = addr.partition("-")
            slot = self._slots.get((int(c), int(s)))
            if slot is None:
                return b""
            slot["status"] = str(value)
            if slot["status"] == "EMPTY":
                slot["model"], slot["name"] = "", ""
            elif not slot["model"]:
                slot["model"], slot["name"] = "ADX1", f"Tx{c}{s}"
            return self._slot_rep(int(c), int(s), "SLOT_STATUS")
        if not key.startswith("ch"):
            return b""
        n_text, _, field = key[2:].partition("_")
        if not n_text.isdigit() or int(n_text) not in self._ch:
            return b""
        n = int(n_text)
        ch = self._ch[n]
        if field == "mute":
            ch["mute"] = bool(value)
            return self._ch_rep(n, "AUDIO_MUTE")
        if field == "gain_db":
            ch["gain"] = max(0, min(60, int(value) + _GAIN_OFFSET))
            return self._ch_rep(n, "AUDIO_GAIN")
        if field == "frequency_khz":
            ch["freq"] = int(value)
            ch["group"] = ("--", "--")
            return self._ch_rep(n, "GROUP_CHANNEL") + self._ch_rep(n, "FREQUENCY")
        if field == "interference":
            ch["interference"] = bool(value)
            return self._ch_rep(n, "INTERFERENCE_STATUS")
        if field == "encryption_error":
            ch["enc_error"] = bool(value)
            return self._ch_rep(n, "ENCRYPTION_STATUS")
        if field == "tx_linked":
            if value:
                ch["tx"] = _new_transmitter(
                    "AD2", f"Tx{n}",
                    bars=int(self.state.get(f"ch{n}_battery_bars", 4)),
                    percent=int(self.state.get(f"ch{n}_battery_percent", 88)),
                    mins=int(self.state.get(f"ch{n}_battery_minutes", 125)),
                )
            else:
                ch["tx"] = None
            return self._tx_dump(n)
        if ch["tx"] is None:
            return b""
        tx = ch["tx"]
        if field == "battery_bars":
            tx["bars"] = int(value)
            return self._ch_rep(n, "TX_BATT_BARS")
        if field == "battery_percent":
            tx["percent"] = int(value)
            return self._ch_rep(n, "TX_BATT_CHARGE_PERCENT")
        if field == "battery_minutes":
            tx["mins"] = int(value)
            return self._ch_rep(n, "TX_BATT_MINS")
        if field == "tx_mute":
            tx["mute_mode"] = "MUTE" if value else "ON"
            return self._ch_rep(n, "TX_MUTE_MODE_STATUS")
        if field == "talk_switch":
            tx["talk"] = "ON" if value else "OFF"
            return self._ch_rep(n, "TX_TALK_SWITCH")
        return b""

    def _mirror(self, key: str, value) -> None:
        with self._quiet():
            self.set_state(key, value)

    # ── Lifecycle ──

    async def stop(self) -> None:
        for task in self._meter_tasks.values():
            task.cancel()
        self._meter_tasks.clear()
        await super().stop()

    # ── Per-frame handling ──

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("ascii", errors="replace").strip()
        text = text.lstrip("<").rstrip(">").strip()
        if not text:
            return None
        parts = text.split()
        verb = parts[0].upper()
        with self._quiet():
            if verb == "GET":
                return self._handle_get(parts[1:])
            if verb == "SET":
                return self._handle_set(parts[1:], text)
        return None

    # ── GET ──

    def _handle_get(self, parts: list[str]) -> bytes | None:
        if not parts:
            return self._frame("REP ERR")
        if parts[0].isdigit():
            idx = int(parts[0])
            if len(parts) < 2:
                return self._frame("REP ERR")
            prop = parts[1].upper()
            args = parts[2:]
            if idx == 0:
                out = b""
                for n in range(1, self._count + 1):
                    out += self._get_channel(n, prop, args) or b""
                return out or self._frame("REP ERR")
            return self._get_channel(idx, prop, args)
        return self._get_device(parts[0].upper(), parts[1:])

    def _get_device(self, prop: str, args: list[str]) -> bytes:
        if prop == "ALL":
            return self._device_dump() + b"".join(
                self._channel_dump(n) for n in range(1, self._count + 1))
        if prop == "NET_SETTINGS":
            iface = args[0].upper() if args else "SC"
            if iface not in ("SC", "D1", "D2"):
                return self._frame("REP ERR")
            return self._frame(
                f"REP NET_SETTINGS {iface} AUTO 192.168.001.025 255.255.255.000 "
                f"000.000.000.000 00:0E:DD:45:60:EB")
        rep = self._device_rep(prop)
        return rep if rep else self._frame("REP ERR")

    def _device_rep(self, prop: str) -> bytes:
        st = self.state
        if prop == "DEVICE_ID":
            return self._frame(f"REP DEVICE_ID {_pad(str(st.get('device_name', '')), 31)}")
        if prop == "MODEL":
            return self._frame(f"REP MODEL {_pad(str(st.get('model', '')), 32)}")
        if prop == "FW_VER":
            return self._frame(f"REP FW_VER {_pad(str(st.get('firmware', '')), 24)}")
        if prop == "RF_BAND":
            return self._frame(f"REP RF_BAND {_pad(str(st.get('rf_band', '')), 8)}")
        if prop == "ENCRYPTION_MODE":
            return self._frame(
                f"REP ENCRYPTION_MODE {'ON' if st.get('encryption') else 'OFF'}")
        if prop == "QUADVERSITY_MODE":
            return self._frame(
                f"REP QUADVERSITY_MODE {'ON' if self._quad() else 'OFF'}")
        if prop == "TRANSMISSION_MODE":
            return self._frame(
                f"REP TRANSMISSION_MODE {st.get('transmission_mode', 'STANDARD')}")
        if prop == "FLASH":
            return self._frame(f"REP FLASH {'ON' if st.get('identifying') else 'OFF'}")
        return b""

    def _device_dump(self) -> bytes:
        return b"".join(self._device_rep(p) for p in (
            "DEVICE_ID", "MODEL", "FW_VER", "RF_BAND", "ENCRYPTION_MODE",
            "QUADVERSITY_MODE", "TRANSMISSION_MODE"))

    def _quad(self) -> bool:
        return bool(self.state.get("quadversity")) and self._count > 2

    def _get_channel(self, n: int, prop: str, args: list[str]) -> bytes | None:
        if n not in self._ch:
            return self._frame("REP ERR")
        if prop == "ALL":
            return self._device_dump() + self._channel_dump(n)
        if prop.startswith("SLOT_"):
            if not args or not args[0].isdigit():
                return self._frame("REP ERR")
            s = int(args[0])
            if s == 0:
                return b"".join(self._slot_rep(n, k, prop)
                                for k in range(1, SLOTS_PER_CHANNEL + 1))
            if not (1 <= s <= SLOTS_PER_CHANNEL):
                return self._frame("REP ERR")
            return self._slot_rep(n, s, prop) or self._frame("REP ERR")
        if prop in ("RSSI", "RSSI_LED_BITMAP"):
            antenna = int(args[0]) if args and args[0].isdigit() else 0
            return self._antenna_reps(n, prop, antenna)
        rep = self._ch_rep(n, prop)
        return rep if rep else self._frame("REP ERR")

    def _channel_dump(self, n: int) -> bytes:
        props = ["METER_RATE", "CHAN_NAME", "AUDIO_MUTE", "AUDIO_GAIN",
                 "FD_MODE", "FREQUENCY", "GROUP_CHANNEL"]
        if self._ch[n]["fd"] == "FD-C":
            props += ["FREQUENCY2", "GROUP_CHANNEL2", "INTERFERENCE_STATUS2"]
        props += ["ENCRYPTION_STATUS", "INTERFERENCE_STATUS",
                  "UNREGISTERED_TX_STATUS", "FLASH",
                  "CHAN_QUALITY", "AUDIO_LED_BITMAP", "AUDIO_LEVEL_PEAK",
                  "AUDIO_LEVEL_RMS", "ANTENNA_STATUS"]
        out = b"".join(self._ch_rep(n, p) for p in props)
        out += self._antenna_reps(n, "RSSI_LED_BITMAP", 0)
        out += self._antenna_reps(n, "RSSI", 0)
        out += self._tx_dump(n)
        return out

    _TX_PROPS = ("TX_MODEL", "TX_DEVICE_ID", "TX_BATT_TYPE", "TX_BATT_BARS",
                 "TX_BATT_CHARGE_PERCENT", "TX_BATT_MINS",
                 "TX_BATT_HEALTH_PERCENT", "TX_BATT_CYCLE_COUNT",
                 "TX_BATT_TEMP_C", "TX_INPUT_PAD", "TX_OFFSET", "TX_POLARITY",
                 "TX_POWER_LEVEL", "TX_LOCK", "TX_MUTE_MODE_STATUS",
                 "TX_TALK_SWITCH")

    def _tx_dump(self, n: int) -> bytes:
        return b"".join(self._ch_rep(n, p) for p in self._TX_PROPS)

    def _antenna_count(self) -> int:
        return 4 if self._quad() else 2

    def _antenna_reps(self, n: int, prop: str, antenna: int) -> bytes:
        ch = self._ch[n]
        count = self._antenna_count()
        indexes = range(1, count + 1) if antenna == 0 else [antenna]
        out = b""
        for a in indexes:
            if not (1 <= a <= count):
                return self._frame("REP ERR")
            if prop == "RSSI":
                out += self._frame(f"REP {n} RSSI {a} {ch['rssi'][a - 1]:03d}")
            else:
                out += self._frame(f"REP {n} RSSI_LED_BITMAP {a} {self._rssi_bitmap(ch['rssi'][a - 1]):02d}")
        return out

    @staticmethod
    def _rssi_bitmap(rssi_raw: int) -> int:
        # Five amber LEDs light with signal; the sixth (0x20) is overload.
        lit = max(0, min(5, (rssi_raw - 40) // 12))
        bitmap = (1 << lit) - 1
        if rssi_raw >= 110:
            bitmap |= 0x20
        return bitmap

    def _ch_rep(self, n: int, prop: str) -> bytes:
        ch = self._ch[n]
        tx = ch["tx"]
        if prop == "AUDIO_MUTE":
            return self._frame(f"REP {n} AUDIO_MUTE {'ON' if ch['mute'] else 'OFF'}")
        if prop == "AUDIO_GAIN":
            return self._frame(f"REP {n} AUDIO_GAIN {ch['gain']:03d}")
        if prop == "CHAN_NAME":
            return self._frame(f"REP {n} CHAN_NAME {_pad(ch['name'], 31)}")
        if prop == "FREQUENCY":
            return self._frame(f"REP {n} FREQUENCY {ch['freq']:07d}")
        if prop == "FREQUENCY2":
            return self._frame(f"REP {n} FREQUENCY2 {ch['freq2']:07d}")
        if prop == "GROUP_CHANNEL":
            return self._frame(f"REP {n} GROUP_CHANNEL {_pad(','.join(ch['group']), 10)}")
        if prop == "GROUP_CHANNEL2":
            return self._frame(f"REP {n} GROUP_CHANNEL2 {_pad(','.join(ch['group2']), 10)}")
        if prop == "FD_MODE":
            return self._frame(f"REP {n} FD_MODE {ch['fd']}")
        if prop == "ENCRYPTION_STATUS":
            return self._frame(f"REP {n} ENCRYPTION_STATUS {'ERROR' if ch['enc_error'] else 'OK'}")
        if prop == "INTERFERENCE_STATUS":
            return self._frame(f"REP {n} INTERFERENCE_STATUS {'DETECTED' if ch['interference'] else 'NONE'}")
        if prop == "INTERFERENCE_STATUS2":
            return self._frame(f"REP {n} INTERFERENCE_STATUS2 {'DETECTED' if ch['interference2'] else 'NONE'}")
        if prop == "UNREGISTERED_TX_STATUS":
            return self._frame(f"REP {n} UNREGISTERED_TX_STATUS {'ERROR' if ch['unregistered'] else 'OK'}")
        if prop == "FLASH":
            return self._frame(f"REP {n} FLASH {'ON' if ch['flash'] else 'OFF'}")
        if prop == "METER_RATE":
            return self._frame(f"REP {n} METER_RATE {ch['meter_rate']:05d}")
        if prop == "CHAN_QUALITY":
            return self._frame(f"REP {n} CHAN_QUALITY {ch['qual']:03d}")
        if prop == "AUDIO_LED_BITMAP":
            return self._frame(f"REP {n} AUDIO_LED_BITMAP {ch['aud_bitmap']:03d}")
        if prop == "AUDIO_LEVEL_PEAK":
            return self._frame(f"REP {n} AUDIO_LEVEL_PEAK {ch['peak']:03d}")
        if prop == "AUDIO_LEVEL_RMS":
            return self._frame(f"REP {n} AUDIO_LEVEL_RMS {ch['rms']:03d}")
        if prop == "ANTENNA_STATUS":
            return self._frame(f"REP {n} ANTENNA_STATUS {self._antenna_status(n)}")
        # ── Side channel ──
        if prop == "TX_MODEL":
            return self._frame(f"REP {n} TX_MODEL {tx['model'] if tx else 'UNKNOWN'}")
        if prop == "TX_DEVICE_ID":
            return self._frame(f"REP {n} TX_DEVICE_ID {_pad(tx['name'] if tx else '', 31)}")
        if prop == "TX_BATT_TYPE":
            return self._frame(f"REP {n} TX_BATT_TYPE {tx['batt_type'] if tx else 'UNKN'}")
        if prop == "TX_BATT_BARS":
            return self._frame(f"REP {n} TX_BATT_BARS {tx['bars'] if tx else 255:03d}")
        if prop == "TX_BATT_CHARGE_PERCENT":
            return self._frame(f"REP {n} TX_BATT_CHARGE_PERCENT {tx['percent'] if tx else 255:03d}")
        if prop == "TX_BATT_MINS":
            return self._frame(f"REP {n} TX_BATT_MINS {tx['mins'] if tx else 65535:05d}")
        if prop == "TX_BATT_HEALTH_PERCENT":
            return self._frame(f"REP {n} TX_BATT_HEALTH_PERCENT {tx['health'] if tx else 255:03d}")
        if prop == "TX_BATT_CYCLE_COUNT":
            return self._frame(f"REP {n} TX_BATT_CYCLE_COUNT {tx['cycles'] if tx else 65535:05d}")
        if prop == "TX_BATT_TEMP_C":
            return self._frame(f"REP {n} TX_BATT_TEMP_C {tx['temp_c'] + _TEMP_OFFSET if tx else 255:03d}")
        if prop == "TX_INPUT_PAD":
            if tx and tx["model"] in ("AD1", "ADX1"):
                return self._frame(f"REP {n} TX_INPUT_PAD {0 if tx['pad'] else 12:03d}")
            return self._frame(f"REP {n} TX_INPUT_PAD 255")
        if prop == "TX_OFFSET":
            return self._frame(f"REP {n} TX_OFFSET {tx['offset'] + _OFFSET_OFFSET if tx else 255:03d}")
        if prop == "TX_POLARITY":
            return self._frame(f"REP {n} TX_POLARITY {tx['polarity'] if tx else 'UNKNOWN'}")
        if prop == "TX_POWER_LEVEL":
            return self._frame(f"REP {n} TX_POWER_LEVEL {tx['power'] if tx else 255:03d}")
        if prop == "TX_LOCK":
            return self._frame(f"REP {n} TX_LOCK {tx['lock'] if tx else 'UNKNOWN'}")
        if prop == "TX_MUTE_MODE_STATUS":
            return self._frame(f"REP {n} TX_MUTE_MODE_STATUS {tx['mute_mode'] if tx else 'UNKNOWN'}")
        if prop == "TX_TALK_SWITCH":
            return self._frame(f"REP {n} TX_TALK_SWITCH {tx['talk'] if tx else 'UNKNOWN'}")
        return b""

    def _antenna_status(self, n: int, section: int = 1) -> str:
        ch = self._ch[n]
        values = ch["rssi"] if section == 1 else ch["rssi2"]
        letters = []
        for a in range(self._antenna_count()):
            raw = values[a]
            letters.append("X" if raw < 45 else ("R" if raw < 60 else "B"))
        return "".join(letters)

    # ── Slots ──

    def _slot_rep(self, n: int, s: int, prop: str) -> bytes:
        slot = self._slots.get((n, s))
        if slot is None:
            return b""
        active = slot["status"] == "LINKED.ACTIVE"
        known = slot["status"] in ("STANDARD", "LINKED.INACTIVE", "LINKED.ACTIVE")
        head = f"REP {n} {prop} {s}"
        if prop == "SLOT_STATUS":
            return self._frame(f"{head} {slot['status']}")
        if prop == "SLOT_TX_MODEL":
            return self._frame(f"{head} {slot['model'] if known else 'UNKNOWN'}")
        if prop == "SLOT_TX_DEVICE_ID":
            return self._frame(f"{head} {_pad(slot['name'] if known else '', 31)}")
        if prop == "SLOT_BATT_TYPE":
            return self._frame(f"{head} {slot['batt_type'] if active else 'UNKN'}")
        if prop == "SLOT_BATT_BARS":
            return self._frame(f"{head} {slot['bars'] if active else 255:03d}")
        if prop == "SLOT_BATT_CHARGE_PERCENT":
            return self._frame(f"{head} {slot['percent'] if active else 255:03d}")
        if prop == "SLOT_BATT_MINS":
            return self._frame(f"{head} {slot['mins'] if active else 65535:05d}")
        if prop == "SLOT_BATT_HEALTH_PERCENT":
            return self._frame(f"{head} {slot['health'] if active else 255:03d}")
        if prop == "SLOT_BATT_CYCLE_COUNT":
            return self._frame(f"{head} {slot['cycles'] if active else 65535:05d}")
        if prop == "SLOT_INPUT_PAD":
            if active and slot["model"] == "ADX1":
                return self._frame(f"{head} {0 if slot['pad'] else 12:03d}")
            return self._frame(f"{head} 255")
        if prop == "SLOT_OFFSET":
            return self._frame(f"{head} {slot['offset'] + _OFFSET_OFFSET if active else 255:03d}")
        if prop == "SLOT_POLARITY":
            if active and slot["model"] in ("ADX1", "ADX1M"):
                return self._frame(f"{head} {slot['polarity']}")
            return self._frame(f"{head} UNKNOWN")
        if prop == "SLOT_RF_OUTPUT":
            return self._frame(f"{head} {slot['rf_output'] if active else 'UNKNOWN'}")
        if prop == "SLOT_RF_POWER":
            return self._frame(f"{head} {_POWER_BY_MODE[slot['rf_mode']] if active else 255:03d}")
        if prop == "SLOT_RF_POWER_MODE":
            return self._frame(f"{head} {slot['rf_mode'] if active else 'UNKNOWN'}")
        if prop == "SLOT_SHOWLINK_STATUS":
            return self._frame(f"{head} {slot['showlink'] if active else 255:03d}")
        return b""

    # ── SET ──

    def _handle_set(self, parts: list[str], text: str) -> bytes | None:
        if not parts:
            return self._frame("REP ERR")
        if parts[0].isdigit():
            n = int(parts[0])
            if len(parts) < 2 or n not in self._ch:
                return self._frame("REP ERR")
            prop = parts[1].upper()
            if prop.startswith("SLOT_"):
                return self._set_slot(n, prop, parts[2:], text)
            return self._set_channel(n, prop, parts[2:], text)
        return self._set_device(parts[0].upper(), parts[1:], text)

    @staticmethod
    def _brace_value(text: str) -> str | None:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            return None
        return text[start + 1:end].strip()

    def _set_device(self, prop: str, args: list[str], text: str) -> bytes:
        if prop == "DEVICE_ID":
            name = self._brace_value(text)
            if name is None or not (1 <= len(name) <= 8):
                return self._frame("REP ERR")
            self._mirror("device_name", name)
            return self._device_rep("DEVICE_ID")
        if prop == "FLASH":
            state = args[0].upper() if args else ""
            if state not in ("ON", "OFF"):
                return self._frame("REP ERR")
            self._mirror("identifying", state == "ON")
            return self._frame(f"REP FLASH {state}")
        if prop == "NET_SETTINGS":
            return self._frame("REP ERR") if len(args) < 5 else b""
        return self._frame("REP ERR")

    def _set_channel(self, n: int, prop: str, args: list[str], text: str) -> bytes:
        ch = self._ch[n]
        first = args[0].upper() if args else ""
        if prop == "AUDIO_MUTE":
            if first == "ON":
                ch["mute"] = True
            elif first == "OFF":
                ch["mute"] = False
            elif first == "TOGGLE":
                ch["mute"] = not ch["mute"]
            else:
                return self._frame("REP ERR")
            self._mirror(f"ch{n}_mute", ch["mute"])
            return self._ch_rep(n, "AUDIO_MUTE")
        if prop == "AUDIO_GAIN":
            new = self._apply_step(ch["gain"], args, 0, 60)
            if new is None:
                return self._frame("REP ERR")
            ch["gain"] = new
            self._mirror(f"ch{n}_gain_db", new - _GAIN_OFFSET)
            return self._ch_rep(n, "AUDIO_GAIN")
        if prop == "CHAN_NAME":
            name = self._brace_value(text)
            if name is None or not (1 <= len(name) <= 8):
                return self._frame("REP ERR")
            ch["name"] = name
            return self._ch_rep(n, "CHAN_NAME")
        if prop in ("FREQUENCY", "FREQUENCY2"):
            if not first.isdigit() or not (470000 <= int(first) <= 960000):
                return self._frame("REP ERR")
            if prop == "FREQUENCY2" and ch["fd"] != "FD-C":
                return self._frame("REP ERR")
            key, gkey = ("freq", "group") if prop == "FREQUENCY" else ("freq2", "group2")
            ch[key] = int(first)
            ch[gkey] = ("--", "--")
            if prop == "FREQUENCY":
                self._mirror(f"ch{n}_frequency_khz", ch["freq"])
            gprop = "GROUP_CHANNEL" if prop == "FREQUENCY" else "GROUP_CHANNEL2"
            return self._ch_rep(n, gprop) + self._ch_rep(n, prop)
        if prop in ("GROUP_CHANNEL", "GROUP_CHANNEL2"):
            value = self._brace_value(text)
            if value is None or "," not in value:
                return self._frame("REP ERR")
            group, _, chan = value.partition(",")
            freq = _GROUP_TABLE.get((group.strip(), chan.strip()))
            if freq is None:
                return self._frame("REP ERR")
            if prop == "GROUP_CHANNEL2" and ch["fd"] != "FD-C":
                return self._frame("REP ERR")
            key, gkey = ("freq", "group") if prop == "GROUP_CHANNEL" else ("freq2", "group2")
            ch[key] = freq
            ch[gkey] = (group.strip(), chan.strip())
            if prop == "GROUP_CHANNEL":
                self._mirror(f"ch{n}_frequency_khz", freq)
            fprop = "FREQUENCY" if prop == "GROUP_CHANNEL" else "FREQUENCY2"
            return self._ch_rep(n, fprop) + self._ch_rep(n, prop)
        if prop == "FLASH":
            if first not in ("ON", "OFF"):
                return self._frame("REP ERR")
            ch["flash"] = first == "ON"
            return self._ch_rep(n, "FLASH")
        if prop == "METER_RATE":
            if not first.isdigit():
                return self._frame("REP ERR")
            rate = int(first)
            if rate != 0 and not (100 <= rate <= 65535):
                return self._frame("REP ERR")
            ch["meter_rate"] = rate
            self._set_meter_task(n, rate)
            out = self._ch_rep(n, "METER_RATE")
            if rate:
                out += self._sample_frame(n)
            return out
        return self._frame("REP ERR")

    @staticmethod
    def _apply_step(current: int, args: list[str], lo: int, hi: int) -> int | None:
        if not args:
            return None
        first = args[0].upper()
        try:
            if first in ("INC", "DEC") and len(args) >= 2:
                step = int(args[1])
                new = current + step if first == "INC" else current - step
            else:
                new = int(args[0])
        except ValueError:
            return None
        return max(lo, min(hi, new))

    def _set_slot(self, n: int, prop: str, args: list[str], text: str) -> bytes:
        if not args or not args[0].isdigit():
            return self._frame("REP ERR")
        s = int(args[0])
        slot = self._slots.get((n, s))
        if slot is None or slot["status"] != "LINKED.ACTIVE":
            return self._frame("REP ERR")
        rest = args[1:]
        first = rest[0].upper() if rest else ""
        if prop == "SLOT_TX_DEVICE_ID":
            name = self._brace_value(text)
            if name is None or not (1 <= len(name) <= 8):
                return self._frame("REP ERR")
            slot["name"] = name
            return self._slot_rep(n, s, prop)
        if prop == "SLOT_INPUT_PAD":
            if slot["model"] != "ADX1" or first not in ("0", "12"):
                return self._frame("REP ERR")
            slot["pad"] = first == "0"
            return self._slot_rep(n, s, prop)
        if prop == "SLOT_OFFSET":
            new = self._apply_step(slot["offset"] + _OFFSET_OFFSET, rest, 0, 33)
            if new is None:
                return self._frame("REP ERR")
            slot["offset"] = new - _OFFSET_OFFSET
            return self._slot_rep(n, s, prop)
        if prop == "SLOT_POLARITY":
            if slot["model"] not in ("ADX1", "ADX1M") or first not in ("POSITIVE", "NEGATIVE"):
                return self._frame("REP ERR")
            slot["polarity"] = first
            return self._slot_rep(n, s, prop)
        if prop == "SLOT_RF_OUTPUT":
            if first not in ("RF_ON", "RF_MUTE"):
                return self._frame("REP ERR")
            slot["rf_output"] = first
            return self._slot_rep(n, s, prop)
        if prop == "SLOT_RF_POWER_MODE":
            if first not in _POWER_BY_MODE:
                return self._frame("REP ERR")
            slot["rf_mode"] = first
            return self._slot_rep(n, s, prop) + self._slot_rep(n, s, "SLOT_RF_POWER")
        # SLOT_RF_POWER, SLOT_BATT_*, SLOT_STATUS and the rest are read-only.
        return self._frame("REP ERR")

    # ── Metering ──

    def _sample_frame(self, n: int) -> bytes:
        ch = self._ch[n]
        count = self._antenna_count()
        fields = [f"{ch['qual']:03d}", f"{ch['aud_bitmap']:03d}",
                  f"{ch['peak']:03d}", f"{ch['rms']:03d}"]
        sections = ["rssi", "rssi2"] if ch["fd"] == "FD-C" else ["rssi"]
        for i, key in enumerate(sections, start=1):
            fields.append(self._antenna_status(n, i))
            for a in range(count):
                raw = ch[key][a]
                fields.append(f"{self._rssi_bitmap(raw):02d}")
                fields.append(f"{raw:03d}")
        return self._frame(f"SAMPLE {n} ALL {' '.join(fields)}")

    def _set_meter_task(self, n: int, rate_ms: int) -> None:
        task = self._meter_tasks.pop(n, None)
        if task is not None:
            task.cancel()
        if rate_ms <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._meter_tasks[n] = loop.create_task(self._meter_loop(n, rate_ms))

    async def _meter_loop(self, n: int, rate_ms: int) -> None:
        try:
            while True:
                await asyncio.sleep(rate_ms / 1000.0)
                if self._ch[n]["meter_rate"] != rate_ms:
                    return
                await self.push(self._sample_frame(n))
        except asyncio.CancelledError:
            return

    # ── Framing ──

    @staticmethod
    def _frame(body: str) -> bytes:
        return f"< {body} >".encode("ascii")
