"""
Shure Networked Audio — Simulator.

Implements the Shure command strings protocol from the per-model pages
(MXA910 / MXA920 / MXA925 / MXA320 / MXA902 / P300 / SCM820 / ANI). Messages
are ``< ... >`` framed over TCP port 2202; ``>`` is the delimiter.

Plays the model the device's Model setting names, or, with Model on Detect
automatically, the one the project's Channel Count suggests (an MXA920 for
9, an ANI4IN for 4, else a P300). Each model has its own channel numbers
(an MXA902's are 9-12) and answers only the properties its page documents;
anything else gets ``< REP ERR >``.

Answers GET with REP, applies SET and echoes the resulting REP (as real
hardware does: the REP a controller sees for its own change is the same REP
the device pushes to other controllers). An index-0 GET fans out one REP per
channel. An MXA920 / MXA925 starts with automatic coverage on, as from the
factory: channels 1-8 are then coverage areas (``CA_MUTE`` / ``CA_GAIN`` /
``AUTOMIX_GATE_OUT_CA``), ``AUDIO_MUTE`` / ``AUDIO_GAIN_HI_RES`` / channel
names answer for the automix output 09 only, and the plain meter carries the
automix output alone. Metering and talker-position subscriptions reply with
an immediate frame. A change made in the Simulator UI goes out as the REP the
device would push.
"""

from __future__ import annotations

import asyncio
import logging

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


DEFAULT_CHANNEL_COUNT = 8
_MODEL_FOR_COUNT = {9: "MXA920", 4: "ANI4IN"}

# Channel numbers per model, the AEC reference input included where the page
# meters it (MXA920 / 925 / 910: 10 is not metered by METER_RATE; MXA320: 6
# is the last value of its row).
_CHANNELS = {
    "MXA910": list(range(1, 10)),
    "MXA920": list(range(1, 10)),
    "MXA925": list(range(1, 10)),
    "MXA320": list(range(1, 6)),
    "MXA902": [9, 10, 11, 12],
    "ANI4IN": list(range(1, 5)),
    "ANI4OUT": list(range(1, 5)),
}
_METER_EXTRA = {"MXA320": [6]}
_COVERAGE = {"MXA920", "MXA925"}
_AUTOMIX = {"MXA910": 9, "MXA920": 9, "MXA925": 9, "MXA320": 5, "MXA902": 9}
_MXA = {"MXA910", "MXA920", "MXA925", "MXA320", "MXA902"}

# Unindexed settings: property -> (models, kind, factory value).
_DEVICE_SETTINGS = {
    "AUTO_COVERAGE": (_COVERAGE, "title", True),
    "VAB": (_COVERAGE, "int", 0),
    "ARRAY_HEIGHT": (_COVERAGE, "int", 274),
    # The MXA925 page: MXA920 from firmware 6.6.
    "LOUDSPEAKER_NULL": (_COVERAGE, "onoff", False),
    "EQ_CONTOUR": (_MXA, "onoff", False),
    "BYPASS_ALL_EQ": ({"MXA910", "MXA920", "MXA925", "MXA320"}, "onoff", False),
    "BYPASS_IMX": ({"MXA910", "MXA920", "MXA925", "MXA320"}, "onoff", False),
    "MUTE_CONTROL_FUNC": ({"MXA320"}, "word", "LOCAL"),
    "AUTOFOCUS": ({"MXA920", "MXA902"}, "onoff", True),
    "SPEAKER": ({"MXA902"}, "onoff", True),
    "LED_STATE_MUTED": (_MXA, "word", "ON"),
    "LED_STATE_UNMUTED": (_MXA, "word", "ON"),
    "LED_COLOR_MUTED": (_MXA, "word", "RED"),
    "LED_COLOR_UNMUTED": (_MXA, "word", "GREEN"),
}
_LED_WORDS = {
    "LED_STATE_MUTED": {"ON", "OFF", "FLASHING"},
    "LED_STATE_UNMUTED": {"ON", "OFF", "FLASHING"},
    "LED_COLOR_MUTED": {
        "RED", "ORANGE", "GOLD", "YELLOW", "YELLOWGREEN", "GREEN", "TURQUOISE",
        "POWDERBLUE", "CYAN", "SKYBLUE", "BLUE", "PURPLE", "LIGHTPURPLE",
        "VIOLET", "ORCHID", "PINK", "WHITE"},
    "MUTE_CONTROL_FUNC": {"LOGIC", "DISABLED", "LOCAL"},
}
_LED_WORDS["LED_COLOR_UNMUTED"] = _LED_WORDS["LED_COLOR_MUTED"]
_GATING = {
    "MXA920": ["Off", "Low", "Medium", "High"],
    "MXA925": ["Off", "Low", "Medium", "High"],
    "MXA320": ["ON", "OFF"],
    "MXA902": ["ON", "OFF"],
}
_POLAR = {"TOROID", "OMNI", "CARDIOID", "SUPER", "HYPER", "BIDIRECTION"}

# Wire gain 0-1400 = -110..+30 dB; 1100 = 0 dB (unity).
_GAIN_WIRE_DEFAULT = 1100
_GAIN_WIRE_MIN = 0
_GAIN_WIRE_MAX = 1400


class ShureNetworkSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "shure_network",
        "name": "Shure Networked Audio Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 2202,
        "delimiter": ">",
        "initial_state": {
            "device_name": "MXA920-SIM",
            "model": "MXA920",
            "mute": False,
            "auto_coverage": True,
            "speaker": True,
            "device_state": "0",
            "led_brightness": 2,
            "firmware": "4.6.11",
        },
        "controls": [
            {"type": "toggle", "key": "mute", "label": "Device Mute"},
            {"type": "toggle", "key": "auto_coverage",
             "label": "Automatic Coverage (MXA920 / MXA925)"},
            {"type": "toggle", "key": "speaker", "label": "Loudspeaker (MXA902)"},
            {"type": "select", "key": "device_state",
             "label": "Error State (MXA925 / MXA320)",
             "options": ["0", "1", "2", "3"]},
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "led_brightness", "label": "LED Brightness"},
            {"type": "indicator", "key": "device_name", "label": "Device Name"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
        ],
        "delays": {"command_response": 0.01},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        self._handling = True
        super().__init__(device_id, config)
        cfg = self.config
        self._count = int(cfg.get("channel_count", DEFAULT_CHANNEL_COUNT))
        self._device_name = str(
            self.state.get("device_name", "MXA920-SIM"))
        # Play the model the device's Model setting names; with it on Detect
        # automatically, the one its Channel Count suggests (an MXA920 for 9,
        # an ANI4IN for 4), else a P300, whose model leaves the count alone.
        self._model = str(
            cfg.get("model") or _MODEL_FOR_COUNT.get(self._count, "P300"))
        self.set_state("model", self._model)
        self._firmware = str(self.state.get("firmware", "4.6.11"))
        self._mute = bool(self.state.get("mute", False))
        self._brightness = int(self.state.get("led_brightness", 2))
        self._preset = 1
        self._meter_ms = 0
        self._ca_meter_ms = 0
        self._talker_ms = 0
        self._ids = list(_CHANNELS.get(self._model,
                                       range(1, self._count + 1)))
        # Device settings this model's page documents, at factory values.
        self._settings: dict[str, object] = {
            prop: default
            for prop, (models, _kind, default) in _DEVICE_SETTINGS.items()
            if self._model in models
        }
        if "AUTO_COVERAGE" in self._settings:
            self._settings["AUTO_COVERAGE"] = bool(
                self.state.get("auto_coverage", True))
        if "SPEAKER" in self._settings:
            self._settings["SPEAKER"] = bool(self.state.get("speaker", True))
        self._device_state = str(self.state.get("device_state", "0"))
        # Each page's default: Low on the MXA920 / MXA925, ON on the others.
        self._gating = ({"MXA920": "Low", "MXA925": "Low"}.get(self._model, "ON")
                        if self._model in _GATING else None)
        self._noise_filter = False
        self._compressor = {9: True, 10: True}
        self._delay = 0
        # Per-channel values.
        self._ch_mute = {n: False for n in self._ids}
        self._ch_gain = {n: _GAIN_WIRE_DEFAULT for n in self._ids}
        self._ch_name = {n: f"Channel {n}" for n in self._ids}
        self._ch_gate = {n: False for n in self._ids}
        self._ch_clip = {n: False for n in self._ids}
        self._polar = {n: "CARDIOID" for n in range(1, 5)}
        self._angle = {n: (n - 1) * 90 for n in range(1, 5)}
        # Coverage areas (MXA920 / MXA925), their own settings.
        self._ca_mute = {n: False for n in range(1, 9)}
        self._ca_gain = {n: _GAIN_WIRE_DEFAULT for n in range(1, 9)}
        self._ca_gate = {n: n == 1 for n in range(1, 9)}
        self._handling = False

    # ── Model helpers ──

    def _auto(self) -> bool:
        return bool(self._settings.get("AUTO_COVERAGE", False))

    def _channel_ok(self, idx: int) -> bool:
        """The ordinary channel properties answer this index right now."""
        if idx not in self._ids:
            return False
        # With automatic coverage on, 1-8 are coverage areas.
        return not (self._auto() and idx <= 8)

    def _has(self, models) -> bool:
        return self._model in models

    # ── Simulator UI ──

    def set_state(self, key: str, value) -> None:
        old = self.state.get(key)
        super().set_state(key, value)
        if getattr(self, "_handling", True) or old == value:
            return
        frames = self._apply_ui_change(key, value)
        if frames:
            asyncio.ensure_future(self.push(frames))

    def _apply_ui_change(self, key: str, value) -> bytes:
        """A Simulator UI change becomes the REP the device would push."""
        if key == "mute":
            self._mute = bool(value)
            return self._mute_rep()
        if key == "auto_coverage" and "AUTO_COVERAGE" in self._settings:
            self._settings["AUTO_COVERAGE"] = bool(value)
            return self._setting_rep("AUTO_COVERAGE")
        if key == "speaker" and "SPEAKER" in self._settings:
            self._settings["SPEAKER"] = bool(value)
            return self._setting_rep("SPEAKER")
        if key == "device_state" and self._has({"MXA925", "MXA320"}):
            self._device_state = str(value)
            return self._frame(f"REP DEVICE_STATE {self._device_state}")
        return b""

    # ── Per-frame handling ──

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("ascii", errors="replace").strip()
        # Strip framing: leading "<", trailing delimiter ">".
        text = text.lstrip("<").rstrip(">").strip()
        if not text:
            return None

        parts = text.split()
        verb = parts[0].upper() if parts else ""

        self._handling = True
        try:
            if verb == "GET":
                return self._handle_get(parts)
            if verb == "SET":
                return self._handle_set(parts)
            return None
        finally:
            self._handling = False

    # ── GET ──

    def _handle_get(self, parts: list[str]) -> bytes | None:
        # < GET PROP >  or  < GET index PROP >
        if len(parts) >= 3 and parts[1].isdigit():
            idx = int(parts[1])
            prop = parts[2].upper()
            return self._get_channel(idx, prop)
        if len(parts) >= 2:
            prop = parts[1].upper()
            return self._get_device(prop)
        return None

    def _get_device(self, prop: str) -> bytes | None:
        if prop == "DEVICE_ID":
            return self._frame(f"REP DEVICE_ID {{{self._device_name:<31}}}")
        if prop == "MODEL":
            if self._model == "SCM820":
                # Its page documents no MODEL.
                return self._frame("REP ERR")
            # 32-character space-padded, as every MXA / ANI / P300 reports it.
            return self._frame(f"REP MODEL {{{self._model:<32}}}")
        if prop == "DEVICE_AUDIO_MUTE":
            return self._mute_rep()
        if prop == "FW_VER":
            return self._frame(f"REP FW_VER {{{self._firmware}}}")
        if prop == "SERIAL_NUM":
            return self._frame(f"REP SERIAL_NUM {{{'SIM' + self._model:<32}}}")
        if prop == "ENCRYPTION":
            return self._frame("REP ENCRYPTION OFF")
        if prop == "PRESET":
            return self._frame(f"REP PRESET {self._preset:02d}")
        if prop == "LED_BRIGHTNESS":
            return self._frame(f"REP LED_BRIGHTNESS {self._brightness}")
        if prop in self._settings:
            return self._setting_rep(prop)
        if prop == "DEVICE_STATE" and self._has({"MXA925", "MXA320"}):
            return self._frame(f"REP DEVICE_STATE {self._device_state}")
        if prop == "RJ45_MODE" and self._has({"MXA925"}):
            return self._frame("REP RJ45_MODE SWITCHED")
        if prop == "ALL":
            out = (
                self._get_device("DEVICE_ID")
                + self._mute_rep()
                + self._get_device("FW_VER")
                + self._get_device("LED_BRIGHTNESS")
                + self._fanout("CHAN_NAME")
                + self._fanout("AUDIO_MUTE")
            )
            return out
        return self._frame("REP ERR")

    def _get_channel(self, idx: int, prop: str) -> bytes | None:
        if prop.startswith("CA_") or prop == "AUTOMIX_GATE_OUT_CA":
            return self._get_area(idx, prop)
        if idx == 0:
            # Broadcast query fans out one REP per channel that answers it.
            return self._fanout(prop) or self._frame("REP ERR")
        if prop == "SPEECH_GATING" and self._gating is not None:
            if idx != _AUTOMIX[self._model]:
                return self._frame("REP ERR")
            return self._frame(f"REP {idx:02d} SPEECH_GATING {self._gating}")
        if prop == "NOISE_FILTER" and self._has({"MXA920", "MXA902"}):
            if idx != 9:
                return self._frame("REP ERR")
            return self._onoff_rep(idx, "NOISE_FILTER", self._noise_filter)
        if prop == "COMPRESSOR" and self._has({"MXA902"}) and idx in (9, 10):
            return self._onoff_rep(idx, "COMPRESSOR", self._compressor[idx])
        if prop == "DELAY" and self._has({"MXA902"}) and idx == 10:
            return self._frame(f"REP 10 DELAY {self._delay:04d}")
        rep = self._channel_rep(idx, prop)
        return rep or self._frame("REP ERR")

    def _channel_rep(self, idx: int, prop: str) -> bytes:
        """One channel's REP, or b"" when this index does not answer it."""
        if prop == "AUDIO_OUT_CLIP_INDICATOR" and self._has(_MXA):
            # With automatic coverage on, the automix output 09 only.
            if not self._channel_ok(idx):
                return b""
            return self._onoff_rep(idx, prop, self._ch_clip[idx])
        if prop == "AUTOMIX_GATE_OUT_EXT_SIG":
            # Per lobe, in either coverage mode.
            if not self._has({"MXA910", "MXA920", "MXA925", "MXA320"}):
                return b""
            if idx not in self._ids or idx == _AUTOMIX[self._model]:
                return b""
            return self._onoff_rep(idx, prop, self._ch_gate[idx])
        if self._model == "MXA320" and idx in self._polar:
            if prop == "POLAR_PATTERN":
                return self._frame(f"REP {idx} POLAR_PATTERN {self._polar[idx]}")
            if prop == "LOBE_ANGLE":
                return self._frame(f"REP {idx} LOBE_ANGLE {self._angle[idx]}")
        if not self._channel_ok(idx):
            return b""
        if prop == "AUDIO_MUTE":
            return self._ch_mute_rep(idx)
        if prop == "CHAN_NAME":
            return self._ch_name_rep(idx)
        if prop == "AUDIO_GAIN_HI_RES":
            return self._ch_gain_rep(idx)
        return b""

    def _get_area(self, idx: int, prop: str) -> bytes:
        if self._model not in _COVERAGE or not self._auto():
            return self._frame("REP ERR")
        areas = range(1, 9) if idx == 0 else [idx]
        if any(n not in self._ca_mute for n in areas):
            return self._frame("REP ERR")
        if prop == "CA_GAIN" and idx == 0:
            return self._frame("REP ERR")
        out = b""
        for n in areas:
            if prop == "CA_MUTE":
                out += self._frame(
                    f"REP {n:02d} CA_MUTE {'On' if self._ca_mute[n] else 'Off'}")
            elif prop == "CA_GAIN":
                out += self._frame(f"REP {n:02d} CA_GAIN {self._ca_gain[n]}")
            elif prop == "AUTOMIX_GATE_OUT_CA":
                out += self._frame(
                    f"REP {n:02d} AUTOMIX_GATE_OUT_CA "
                    f"{'On' if self._ca_gate[n] else 'Off'}")
            else:
                return self._frame("REP ERR")
        return out

    def _fanout(self, prop: str) -> bytes:
        prop = prop.upper()
        out = b""
        for n in self._ids:
            out += self._channel_rep(n, prop)
        return out

    # ── SET ──

    def _handle_set(self, parts: list[str]) -> bytes | None:
        # < SET PROP val >  or  < SET index PROP val... >
        if len(parts) >= 3 and parts[1].isdigit():
            return self._set_channel(int(parts[1]), parts[2].upper(), parts[3:])
        if len(parts) >= 2:
            return self._set_device(parts[1].upper(), parts[2:])
        return None

    def _set_device(self, prop: str, args: list[str]) -> bytes | None:
        arg = args[0] if args else ""
        if prop == "DEVICE_AUDIO_MUTE":
            new = _toggle(arg, self._mute)
            if new is None:
                return self._frame("REP ERR")
            self._mute = new
            self.set_state("mute", self._mute)
            return self._mute_rep()
        if prop == "LED_BRIGHTNESS":
            try:
                self._brightness = int(arg)
            except ValueError:
                return self._frame("REP ERR")
            self.set_state("led_brightness", self._brightness)
            return self._frame(f"REP LED_BRIGHTNESS {self._brightness}")
        if prop == "FLASH":
            return self._frame(f"REP FLASH {arg.upper()}")
        if prop == "PRESET":
            try:
                self._preset = int(arg)
            except ValueError:
                return self._frame("REP ERR")
            return self._frame(f"REP PRESET {self._preset:02d}")
        if prop == "REBOOT":
            # The page: no acknowledgement.
            return None
        if prop == "DEFAULT_SETTINGS":
            return self._frame("REP DEFAULT_SETTINGS 00")
        if prop in self._settings:
            return self._set_setting(prop, arg)
        if prop in ("METER_RATE", "METER_RATE_IN"):
            if prop == "METER_RATE_IN" and self._model != "P300":
                return self._frame("REP ERR")
            try:
                self._meter_ms = int(arg)
            except ValueError:
                return self._frame("REP ERR")
            out = self._frame(f"REP {prop} {self._meter_ms}")
            if self._meter_ms > 0:
                out += self._sample_frame()
            return out
        if prop == "CA_METER_RATE":
            if self._model not in _COVERAGE or not self._auto():
                return self._frame("REP ERR")
            try:
                self._ca_meter_ms = int(arg)
            except ValueError:
                return self._frame("REP ERR")
            out = self._frame(f"REP CA_METER_RATE {self._ca_meter_ms}")
            if self._ca_meter_ms > 0:
                vals = " ".join(f"{20 + n:03d}" for n in range(1, 9))
                out += self._frame(f"SAMPLE_CA {vals}")
            return out
        if prop == "TALKER_POSITION_RATE" and self._model in _COVERAGE:
            try:
                rate = int(arg)
            except ValueError:
                return self._frame("REP ERR")
            if 0 < rate < 100 or rate > 99999:
                rate = self._talker_ms  # an invalid rate reports the current one
            self._talker_ms = rate
            out = self._frame(f"REP TALKER_POSITION_RATE {rate:05d}")
            if rate > 0:
                area = 1 if self._auto() else 0
                out += self._frame(
                    f"SAMPLE TALKER_POSITIONS 1 {area} -152 305 140 0 "
                    f"2 {area} 91 -61 152 0")
            return out
        if prop == "METER_RATE_OUT" and self._model == "P300":
            # Acknowledged; this sim doesn't stream output meters.
            try:
                return self._frame(f"REP METER_RATE_OUT {int(arg)}")
            except ValueError:
                return self._frame("REP ERR")
        if prop.startswith("LED_STATE_") or prop.startswith("LED_COLOR_"):
            # A model without these settings still echoes, as before.
            return self._frame(f"REP {prop} {arg.upper()}")
        return self._frame("REP ERR")

    def _set_setting(self, prop: str, arg: str) -> bytes:
        _models, kind, _default = _DEVICE_SETTINGS[prop]
        if kind in ("onoff", "title"):
            new = _toggle(arg, bool(self._settings[prop]), allow_toggle=False)
            if new is None:
                return self._frame("REP ERR")
            self._settings[prop] = new
            if prop == "AUTO_COVERAGE":
                self.set_state("auto_coverage", new)
            elif prop == "SPEAKER":
                self.set_state("speaker", new)
        elif kind == "int":
            try:
                self._settings[prop] = int(arg)
            except ValueError:
                return self._frame("REP ERR")
        else:
            word = arg.upper()
            if word not in _LED_WORDS.get(prop, {word}):
                return self._frame("REP ERR")
            self._settings[prop] = word
        return self._setting_rep(prop)

    def _set_channel(self, idx: int, prop: str, args: list[str]) -> bytes | None:
        arg = args[0] if args else ""
        if prop in ("CA_MUTE", "CA_GAIN"):
            if (self._model not in _COVERAGE or not self._auto()
                    or idx not in self._ca_mute):
                return self._frame("REP ERR")
            if prop == "CA_MUTE":
                new = _toggle(arg, self._ca_mute[idx])
                if new is None:
                    return self._frame("REP ERR")
                self._ca_mute[idx] = new
                return self._frame(
                    f"REP {idx:02d} CA_MUTE {'On' if new else 'Off'}")
            wire = _apply_gain(self._ca_gain[idx], args)
            if wire is None:
                return self._frame("REP ERR")
            self._ca_gain[idx] = wire
            return self._frame(f"REP {idx:02d} CA_GAIN {wire}")
        if prop == "SPEECH_GATING" and self._gating is not None:
            words = {w.upper(): w for w in _GATING[self._model]}
            if idx != _AUTOMIX[self._model] or arg.upper() not in words:
                return self._frame("REP ERR")
            self._gating = words[arg.upper()]
            return self._frame(f"REP {idx:02d} SPEECH_GATING {self._gating}")
        if prop == "NOISE_FILTER" and self._has({"MXA920", "MXA902"}) and idx == 9:
            new = _toggle(arg, self._noise_filter, allow_toggle=False)
            if new is None:
                return self._frame("REP ERR")
            self._noise_filter = new
            return self._onoff_rep(idx, prop, new)
        if prop == "COMPRESSOR" and self._has({"MXA902"}) and idx in (9, 10):
            new = _toggle(arg, self._compressor[idx])
            if new is None:
                return self._frame("REP ERR")
            self._compressor[idx] = new
            return self._onoff_rep(idx, prop, new)
        if prop == "DELAY" and self._has({"MXA902"}) and idx == 10:
            try:
                delay = int(arg)
            except ValueError:
                return self._frame("REP ERR")
            if not 0 <= delay <= 160:
                return self._frame("REP ERR")
            self._delay = delay
            return self._frame(f"REP 10 DELAY {delay:04d}")
        if self._model == "MXA320" and idx in self._polar:
            if prop == "POLAR_PATTERN":
                if arg.upper() not in _POLAR:
                    return self._frame("REP ERR")
                self._polar[idx] = arg.upper()
                return self._frame(f"REP {idx} POLAR_PATTERN {self._polar[idx]}")
            if prop == "LOBE_ANGLE":
                try:
                    angle = int(arg)
                except ValueError:
                    return self._frame("REP ERR")
                if angle % 15 or not 0 <= angle <= 345:
                    return self._frame("REP ERR")
                self._angle[idx] = angle
                return self._frame(f"REP {idx} LOBE_ANGLE {angle}")
        if not self._channel_ok(idx):
            return self._frame("REP ERR")
        if prop == "AUDIO_MUTE":
            new = _toggle(arg, self._ch_mute[idx])
            if new is None:
                return self._frame("REP ERR")
            self._ch_mute[idx] = new
            return self._ch_mute_rep(idx)
        if prop == "AUDIO_GAIN_HI_RES":
            wire = _apply_gain(self._ch_gain[idx], args)
            if wire is None:
                return self._frame("REP ERR")
            self._ch_gain[idx] = wire
            return self._ch_gain_rep(idx)
        return self._frame("REP ERR")

    # ── REP builders ──

    def _mute_rep(self) -> bytes:
        return self._frame(
            f"REP DEVICE_AUDIO_MUTE {'ON' if self._mute else 'OFF'}")

    def _setting_rep(self, prop: str) -> bytes:
        _models, kind, _default = _DEVICE_SETTINGS[prop]
        value = self._settings[prop]
        if kind == "onoff":
            word = "ON" if value else "OFF"
        elif kind == "title":
            word = "On" if value else "Off"
        elif kind == "int":
            word = str(value)
        else:
            word = str(value)
        return self._frame(f"REP {prop} {word}")

    def _onoff_rep(self, idx: int, prop: str, value: bool) -> bytes:
        return self._frame(f"REP {idx:02d} {prop} {'ON' if value else 'OFF'}")

    def _ch_mute_rep(self, idx: int) -> bytes:
        return self._frame(
            f"REP {idx:02d} AUDIO_MUTE {'ON' if self._ch_mute[idx] else 'OFF'}")

    def _ch_name_rep(self, idx: int) -> bytes:
        # 31-char space-padded, brace-wrapped — as real hardware reports.
        return self._frame(f"REP {idx:02d} CHAN_NAME {{{self._ch_name[idx]:<31}}}")

    def _ch_gain_rep(self, idx: int) -> bytes:
        return self._frame(f"REP {idx:02d} AUDIO_GAIN_HI_RES {self._ch_gain[idx]}")

    def _sample_frame(self) -> bytes:
        # One value per metered channel (000-060). Deterministic ramp so
        # tests can assert the positional fan-out into channel meter props.
        # With automatic coverage on, the automix output alone.
        if self._model in _COVERAGE and self._auto():
            return self._frame("SAMPLE 019")
        metered = self._ids + _METER_EXTRA.get(self._model, [])
        vals = " ".join(f"{min(60, 10 + n):03d}" for n in metered)
        return self._frame(f"SAMPLE {vals}")

    def _frame(self, body: str) -> bytes:
        return f"< {body} >".encode("ascii")


def _toggle(arg: str, current: bool, allow_toggle: bool = True) -> bool | None:
    word = arg.upper()
    if word == "ON":
        return True
    if word == "OFF":
        return False
    if word == "TOGGLE" and allow_toggle:
        return not current
    return None


def _apply_gain(current: int, args: list[str]) -> int | None:
    if not args:
        return None
    first = args[0].lower()
    try:
        if first in ("inc", "dec") and len(args) >= 2:
            step = int(args[1])
            new = current + step if first == "inc" else current - step
        else:
            new = int(args[0])
    except ValueError:
        return None
    return max(_GAIN_WIRE_MIN, min(_GAIN_WIRE_MAX, new))
