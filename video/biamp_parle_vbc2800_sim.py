"""
Biamp Parlé VBC 2800 — Simulator.

Serves the bar's Network API on a plain socket (the bar itself serves it over
SSH as user ``control``; the simulator stands in for the session after the
login). Commands are ``<subject> <verb> <attribute> [value]`` ended by a
carriage return; replies end with CR LF.

As Biamp's "Parlé VBC 2800 Network API Control" article describes it:
DEVICE get serialNumber / version / modelName and reboot; InputSource and
ALSSource input; AnalogInput gain, level, minLevel, maxLevel and mute;
MicrophoneALSInput level, minLevel, maxLevel and mute; Camera streaming,
autoframing, tiling, peopleCount, pan / tilt / zoom with their min and max,
presetSave / presetRecall (0 to 2), backlightCompensation, flip and mirror;
USBIn / USBOut connected, streaming, level (0 to 100, an increment answering
the new level), mute, and USBOut hook. A level set outside its min and max is
refused; an increment or decrement stops at them; a new min or max pulls the
level inside. Tiling and autoframing turn each other off. With either on,
pan, tilt and zoom are accepted and change nothing.

Each value is answered in the form the article's table shows: analog and
microphone mutes as ``false`` / ``true``, the camera's and USB's switches as
``0`` / ``1``, levels in dB with one decimal, USB levels as whole numbers.

Subscriptions: ``<subject> subscribe <attribute> <name> [rate ms]`` answers
``! "publishToken":"<name>" "value":<value> +OK`` (one line, the article's
example) and then a ``!`` line on every change, no faster than the rate
rounded up to 100 ms; 50 per session; ``unsubscribe <name>`` (the article) and
``<subject> unsubscribe <attribute> <name>`` (the command calculator) both end
one. Subscriptions belong to the newest session.

Not in the article, so modelled on the Tesira Text Protocol's conventions: an
error is a line starting ``-ERR``, and the session echoes each command back
before its answer (Biamp's own control file expects the echo; set the device
config's ``echo`` to false to turn it off). ``refuse_subscriptions`` (a list
of "Subject attribute" strings) makes those subscriptions answer an error, as
an older firmware might. ``max_zoom`` sets the zoom ceiling (default 5.0, the
article's example). A reboot closes every session.

Driver side: ``video/biamp_parle_vbc2800.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

CRLF = "\r\n"
PUMP_TICK_S = 0.05
MAX_SUBSCRIPTIONS = 50

# (subject, attribute) -> (state key, form, writable). Forms: "dbool" true /
# false, "ibool" 1 / 0, "db" one decimal, "int", "zoom" one decimal.
ATTRS: dict[tuple[str, str], tuple[str, str, bool]] = {
    ("InputSource", "input"): ("input_source", "int", True),
    ("ALSSource", "input"): ("als_source", "int", True),
    ("AnalogInput", "gain"): ("analog_gain", "db", True),
    ("AnalogInput", "level"): ("analog_level", "db", True),
    ("AnalogInput", "minLevel"): ("analog_min_level", "db", True),
    ("AnalogInput", "maxLevel"): ("analog_max_level", "db", True),
    ("AnalogInput", "mute"): ("analog_mute", "dbool", True),
    ("MicrophoneALSInput", "level"): ("mic_als_level", "db", True),
    ("MicrophoneALSInput", "minLevel"): ("mic_als_min_level", "db", True),
    ("MicrophoneALSInput", "maxLevel"): ("mic_als_max_level", "db", True),
    ("MicrophoneALSInput", "mute"): ("mic_als_mute", "dbool", True),
    ("Camera", "streaming"): ("camera_streaming", "ibool", False),
    ("Camera", "autoframing"): ("autoframing", "ibool", True),
    ("Camera", "tiling"): ("tiling", "ibool", True),
    ("Camera", "peopleCount"): ("people_count", "int", False),
    ("Camera", "pan"): ("pan", "int", True),
    ("Camera", "tilt"): ("tilt", "int", True),
    ("Camera", "zoom"): ("zoom", "zoom", True),
    ("Camera", "backlightCompensation"): ("backlight_compensation", "ibool", True),
    ("Camera", "flip"): ("flip", "ibool", True),
    ("Camera", "mirror"): ("mirror", "ibool", True),
    ("USBIn", "connected"): ("usb_in_connected", "ibool", False),
    ("USBIn", "streaming"): ("usb_in_streaming", "ibool", False),
    ("USBIn", "level"): ("usb_in_level", "int", True),
    ("USBIn", "mute"): ("usb_in_mute", "ibool", True),
    ("USBOut", "connected"): ("usb_out_connected", "ibool", False),
    ("USBOut", "streaming"): ("usb_out_streaming", "ibool", False),
    ("USBOut", "level"): ("usb_out_level", "int", True),
    ("USBOut", "mute"): ("usb_out_mute", "ibool", True),
    ("USBOut", "hook"): ("hook", "ibool", False),
}

# Fixed read-only values: (subject, attribute) -> wire value.
FIXED = {
    ("Camera", "minPan"): "-100",
    ("Camera", "maxPan"): "100",
    ("Camera", "minTilt"): "-100",
    ("Camera", "maxTilt"): "100",
    ("Camera", "minZoom"): "1.0",
    ("USBIn", "minLevel"): "0.0",
    ("USBIn", "maxLevel"): "100.0",
    ("USBOut", "minLevel"): "0.0",
    ("USBOut", "maxLevel"): "100.0",
}

SUBJECTS = sorted({s for s, _ in ATTRS} | {"DEVICE"})
LEVEL_RANGE = (-100.0, 12.0)


class ParleSimError(Exception):
    """An error line: -ERR <reason>."""


class BiampParleVBC2800Simulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "biamp_parle_vbc2800",
        "name": "Biamp Parlé VBC 2800 Simulator",
        "category": "video",
        "transport": "tcp",
        "default_port": 22,
        "delimiter": "\r",
        "initial_state": {
            "serial_number": "31248700049",
            "firmware_version": "1.6.0",
            "model_name": "VBC 2800",
            "input_source": 2,
            "als_source": 0,
            "analog_gain": 0.0,
            "analog_level": 0.0,
            "analog_min_level": -100.0,
            "analog_max_level": 0.0,
            "analog_mute": False,
            "mic_als_level": 0.0,
            "mic_als_min_level": -100.0,
            "mic_als_max_level": 0.0,
            "mic_als_mute": False,
            "camera_streaming": True,
            "autoframing": False,
            "tiling": False,
            "people_count": 2,
            "pan": 0,
            "tilt": 0,
            "zoom": 1.0,
            "backlight_compensation": False,
            "flip": False,
            "mirror": False,
            "usb_in_connected": True,
            "usb_in_streaming": True,
            "usb_in_level": 75,
            "usb_in_mute": False,
            "usb_out_connected": True,
            "usb_out_streaming": True,
            "usb_out_level": 75,
            "usb_out_mute": False,
            "hook": False,
        },
        "controls": [
            {"type": "group", "label": "Camera", "controls": [
                {"type": "toggle", "key": "camera_streaming", "label": "Streaming"},
                {"type": "toggle", "key": "autoframing", "label": "Autoframing"},
                {"type": "toggle", "key": "tiling", "label": "Tiling"},
                {"type": "slider", "key": "people_count", "label": "People Count",
                 "min": 0, "max": 20, "step": 1},
                {"type": "slider", "key": "pan", "label": "Pan",
                 "min": -100, "max": 100, "step": 1},
                {"type": "slider", "key": "tilt", "label": "Tilt",
                 "min": -100, "max": 100, "step": 1},
                {"type": "slider", "key": "zoom", "label": "Zoom",
                 "min": 1, "max": 5, "step": 0.1},
                {"type": "toggle", "key": "backlight_compensation",
                 "label": "Backlight Compensation"},
                {"type": "toggle", "key": "flip", "label": "Flip"},
                {"type": "toggle", "key": "mirror", "label": "Mirror"},
            ]},
            {"type": "group", "label": "USB", "controls": [
                {"type": "toggle", "key": "usb_in_connected", "label": "USB In Connected"},
                {"type": "toggle", "key": "usb_in_streaming", "label": "USB In Streaming"},
                {"type": "slider", "key": "usb_in_level", "label": "USB In Level",
                 "min": 0, "max": 100, "step": 1},
                {"type": "toggle", "key": "usb_in_mute", "label": "USB In Mute"},
                {"type": "toggle", "key": "usb_out_connected", "label": "USB Out Connected"},
                {"type": "toggle", "key": "usb_out_streaming", "label": "USB Out Streaming"},
                {"type": "slider", "key": "usb_out_level", "label": "USB Out Level",
                 "min": 0, "max": 100, "step": 1},
                {"type": "toggle", "key": "usb_out_mute", "label": "USB Out Mute"},
                {"type": "toggle", "key": "hook", "label": "Off Hook"},
            ]},
            {"type": "group", "label": "Analog and ALS", "controls": [
                {"type": "slider", "key": "input_source",
                 "label": "Input Source (0 USB, 1 Analog, 2 Mixed)",
                 "min": 0, "max": 2, "step": 1},
                {"type": "slider", "key": "als_source",
                 "label": "ALS Source (0 Off, 1 Far, 2 Near, 3 Both)",
                 "min": 0, "max": 3, "step": 1},
                {"type": "slider", "key": "analog_level", "label": "Analog Level (dB)",
                 "min": -100, "max": 12, "step": 0.5},
                {"type": "toggle", "key": "analog_mute", "label": "Analog Mute"},
                {"type": "slider", "key": "mic_als_level",
                 "label": "Microphone ALS Level (dB)",
                 "min": -100, "max": 12, "step": 0.5},
                {"type": "toggle", "key": "mic_als_mute", "label": "Microphone ALS Mute"},
            ]},
        ],
        # The platform's own name for it from 0.37.0, declared here so a 0.36.0
        # Simulator UI offers it too.
        "error_modes": {
            "communication_timeout": {
                "behavior": "no_response",
                "description": "Device stops responding",
            },
        },
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Three camera positions: (pan, tilt, zoom).
        self._presets: list[tuple[int, int, float]] = [(0, 0, 1.0)] * 3
        # client id -> subscription name -> subscription
        self._subs: dict[str, dict[str, dict[str, Any]]] = {}
        self._pumps: dict[str, asyncio.Task] = {}

    # ── Configuration ──

    def _echo(self) -> bool:
        return (self.config or {}).get("echo", True) is not False

    def _max_zoom(self) -> float:
        try:
            return float((self.config or {}).get("max_zoom", 5.0))
        except (TypeError, ValueError):
            return 5.0

    def _refused(self, subject: str, attr: str) -> bool:
        refuse = (self.config or {}).get("refuse_subscriptions") or []
        return f"{subject} {attr}".lower() in {str(r).lower() for r in refuse}

    # ── Sessions ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        self._subs[client_id] = {}
        self._pumps[client_id] = asyncio.ensure_future(self._pump(client_id))
        return None

    async def stop(self) -> None:
        for task in self._pumps.values():
            task.cancel()
        self._pumps.clear()
        await super().stop()

    def _latest_client_id(self) -> str | None:
        if not self._clients:
            return None
        return next(reversed(self._clients))

    # ── Dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", errors="replace").strip()
        if not line:
            return None
        try:
            answer = self._dispatch(line.split())
        except ParleSimError as exc:
            answer = f"-ERR {exc}"
        out = (line + CRLF if self._echo() else "") + answer + CRLF
        return out.encode("utf-8")

    @staticmethod
    def _canon(name: str, choices: list[str]) -> str | None:
        # The article's own examples write some attributes in lower case
        # ("decrement minlevel"), so names are matched without case.
        low = name.lower()
        for choice in choices:
            if choice.lower() == low:
                return choice
        return None

    def _dispatch(self, words: list[str]) -> str:
        if len(words) >= 2 and words[0].lower() == "unsubscribe":
            return self._unsubscribe(words[1])
        if len(words) < 2:
            raise ParleSimError("invalid command")
        subject = self._canon(words[0], SUBJECTS)
        if subject is None:
            raise ParleSimError(f"address not found: {words[0]}")
        verb = words[1].lower()
        if subject == "DEVICE":
            return self._device(verb, words[2:])
        if len(words) < 3:
            raise ParleSimError("invalid command")
        attrs = [a for s, a in ATTRS if s == subject]
        attrs += [a for s, a in FIXED if s == subject]
        attrs += ["presetSave", "presetRecall"] if subject == "Camera" else []
        if subject == "Camera":
            attrs.append("maxZoom")
        attr = self._canon(words[2], attrs)
        if attr is None:
            raise ParleSimError(f"unknown attribute: {words[2]}")
        args = words[3:]
        if verb == "get":
            return f'+OK "value":{self._read(subject, attr)}'
        if verb == "subscribe":
            return self._subscribe(subject, attr, args)
        if verb == "unsubscribe":
            if not args:
                raise ParleSimError("invalid command")
            return self._unsubscribe(args[-1])
        if verb == "set":
            if not args:
                raise ParleSimError("missing value")
            return self._set(subject, attr, args[0])
        if verb in ("increment", "decrement"):
            if not args:
                raise ParleSimError("missing value")
            return self._step(subject, attr, args[0], 1 if verb == "increment" else -1)
        if verb == "toggle":
            return self._toggle(subject, attr)
        raise ParleSimError(f"unknown command: {words[1]}")

    def _device(self, verb: str, args: list[str]) -> str:
        if verb == "reboot":
            asyncio.ensure_future(self._reboot())
            return "+OK"
        if verb != "get" or not args:
            raise ParleSimError("invalid command")
        key = self._canon(args[0], ["serialNumber", "version", "modelName"])
        if key is None:
            raise ParleSimError(f"unknown attribute: {args[0]}")
        state_key = {"serialNumber": "serial_number", "version": "firmware_version",
                     "modelName": "model_name"}[key]
        return f'+OK "value":{json.dumps(str(self.state.get(state_key, "")))}'

    async def _reboot(self) -> None:
        await asyncio.sleep(0.3)
        for writer in list(self._clients.values()):
            try:
                writer.close()
            except Exception:
                pass

    # ── Values ──

    def _form(self, key: str, form: str) -> str:
        value = self.state.get(key)
        if form == "dbool":
            return "true" if value else "false"
        if form == "ibool":
            return "1" if value else "0"
        if form in ("db", "zoom"):
            return f"{float(value or 0):.1f}"
        return str(int(round(float(value or 0))))

    def _read(self, subject: str, attr: str) -> str:
        if (subject, attr) in FIXED:
            return FIXED[(subject, attr)]
        if (subject, attr) == ("Camera", "maxZoom"):
            return f"{self._max_zoom():.1f}"
        if attr in ("presetSave", "presetRecall"):
            raise ParleSimError("not readable")
        key, form, _w = ATTRS[(subject, attr)]
        return self._form(key, form)

    @staticmethod
    def _number(text: str) -> float:
        try:
            number = float(text)
        except ValueError:
            raise ParleSimError(f"invalid value: {text}") from None
        if math.isnan(number) or math.isinf(number):
            raise ParleSimError(f"invalid value: {text}")
        return number

    @staticmethod
    def _bool(text: str) -> bool:
        low = text.lower()
        if low in ("true", "1"):
            return True
        if low in ("false", "0"):
            return False
        raise ParleSimError(f"invalid value: {text}")

    def _limits(self, subject: str) -> tuple[str, str, str]:
        prefix = "analog" if subject == "AnalogInput" else "mic_als"
        return f"{prefix}_level", f"{prefix}_min_level", f"{prefix}_max_level"

    def _camera_moves(self) -> bool:
        return not (self.state.get("autoframing") or self.state.get("tiling"))

    def _set(self, subject: str, attr: str, raw: str) -> str:
        if attr == "presetSave":
            idx = self._preset_index(raw)
            st = self.state
            self._presets[idx] = (int(st["pan"]), int(st["tilt"]), float(st["zoom"]))
            return "+OK"
        if attr == "presetRecall":
            idx = self._preset_index(raw)
            if self._camera_moves():
                pan, tilt, zoom = self._presets[idx]
                self._write("pan", pan)
                self._write("tilt", tilt)
                self._write("zoom", zoom)
            return "+OK"
        if (subject, attr) not in ATTRS:
            raise ParleSimError(f"{attr} is read-only")
        key, form, writable = ATTRS[(subject, attr)]
        if not writable:
            raise ParleSimError(f"{attr} is read-only")
        if form in ("dbool", "ibool"):
            self._write_switch(key, self._bool(raw))
            return "+OK"
        number = self._number(raw)
        if attr == "input":
            top = 2 if subject == "InputSource" else 3
            if number not in range(top + 1):
                raise ParleSimError("bounds error")
            self._write(key, int(number))
        elif attr == "gain":
            if not 0.0 <= number <= 24.0:
                raise ParleSimError("bounds error")
            self._write(key, number)
        elif subject in ("AnalogInput", "MicrophoneALSInput"):
            self._set_db(subject, attr, number)
        elif subject in ("USBIn", "USBOut"):
            if not 0 <= number <= 100:
                raise ParleSimError("bounds error")
            self._write(key, int(round(number)))
        elif attr in ("pan", "tilt"):
            if not -100 <= number <= 100:
                raise ParleSimError("bounds error")
            if self._camera_moves():
                self._write(key, int(round(number)))
        elif attr == "zoom":
            if not 1.0 <= number <= self._max_zoom():
                raise ParleSimError("bounds error")
            if self._camera_moves():
                self._write(key, round(number, 1))
        return "+OK"

    def _preset_index(self, raw: str) -> int:
        number = self._number(raw)
        if number not in (0, 1, 2):
            raise ParleSimError("bounds error")
        return int(number)

    def _set_db(self, subject: str, attr: str, number: float) -> None:
        level_key, min_key, max_key = self._limits(subject)
        low, high = LEVEL_RANGE
        if attr == "level":
            if not self.state[min_key] <= number <= self.state[max_key]:
                raise ParleSimError("bounds error")
            self._write(level_key, number)
            return
        if not low <= number <= high:
            raise ParleSimError("bounds error")
        if attr == "minLevel":
            if number > self.state[max_key]:
                raise ParleSimError("bounds error")
            self._write(min_key, number)
        else:
            if number < self.state[min_key]:
                raise ParleSimError("bounds error")
            self._write(max_key, number)
        # "Adjusting the minLevel or maxLevel commands will adjust the current
        # level if it becomes outside the new limits."
        level = self.state[level_key]
        clamped = max(self.state[min_key], min(self.state[max_key], level))
        if clamped != level:
            self._write(level_key, clamped)

    def _step(self, subject: str, attr: str, raw: str, sign: int) -> str:
        amount = self._number(raw) * sign
        if subject in ("AnalogInput", "MicrophoneALSInput") and attr in (
            "level", "minLevel", "maxLevel",
        ):
            level_key, min_key, max_key = self._limits(subject)
            low, high = LEVEL_RANGE
            if attr == "level":
                lo, hi = self.state[min_key], self.state[max_key]
                self._write(level_key, max(lo, min(hi, self.state[level_key] + amount)))
            else:
                key = min_key if attr == "minLevel" else max_key
                self._set_db(subject, attr, max(low, min(high, self.state[key] + amount)))
            return "+OK"
        if subject in ("USBIn", "USBOut") and attr == "level":
            key = ATTRS[(subject, attr)][0]
            new = int(max(0, min(100, round(self.state[key] + amount))))
            self._write(key, new)
            # The article's example answers the new level.
            return f'+OK "value":{new}'
        if subject == "Camera" and attr in ("pan", "tilt"):
            if self._camera_moves():
                new = int(max(-100, min(100, round(self.state[attr] + amount))))
                self._write(attr, new)
            return "+OK"
        if subject == "Camera" and attr == "zoom":
            if self._camera_moves():
                new = round(max(1.0, min(self._max_zoom(), self.state["zoom"] + amount)), 1)
                self._write("zoom", new)
            return "+OK"
        raise ParleSimError(f"{attr} cannot be stepped")

    def _toggle(self, subject: str, attr: str) -> str:
        entry = ATTRS.get((subject, attr))
        if entry is None or entry[1] not in ("dbool", "ibool") or not entry[2]:
            raise ParleSimError(f"{attr} cannot be toggled")
        self._write_switch(entry[0], not self.state.get(entry[0]))
        return "+OK"

    def _write_switch(self, key: str, value: bool) -> None:
        # "Tiling and Autoframing functions are mutually exclusive."
        if value and key == "autoframing":
            self._write("tiling", False)
        elif value and key == "tiling":
            self._write("autoframing", False)
        self._write(key, value)

    # ── Changes and subscriptions ──

    def _write(self, key: str, value: Any) -> None:
        if self.state.get(key) == value:
            return
        super().set_state(key, value)
        self._mark(key)

    def set_state(self, key: str, value: Any) -> None:
        # A Simulator UI change: announce it like a front-panel change.
        changed = self.state.get(key) != value
        super().set_state(key, value)
        if changed:
            self._mark(key)

    def _mark(self, key: str) -> None:
        for subs in self._subs.values():
            for sub in subs.values():
                if sub["key"] == key:
                    sub["dirty"] = True

    def _subscribe(self, subject: str, attr: str, args: list[str]) -> str:
        if not args:
            raise ParleSimError("missing subscription name")
        if (subject, attr) not in ATTRS or self._refused(subject, attr):
            raise ParleSimError(f"cannot subscribe to {attr}")
        client = self._latest_client_id()
        if client is None:
            raise ParleSimError("no session")
        subs = self._subs.setdefault(client, {})
        name = args[0]
        if name in subs:
            raise ParleSimError(f"subscription {name} already exists")
        if len(subs) >= MAX_SUBSCRIPTIONS:
            raise ParleSimError("too many subscriptions")
        rate_s = 0.0
        if len(args) > 1:
            # "rounded up to the nearest 100 ms"
            rate_s = math.ceil(max(0.0, self._number(args[1])) / 100.0) / 10.0
        key, form, _w = ATTRS[(subject, attr)]
        subs[name] = {
            "key": key, "form": form, "rate": max(rate_s, 0.1),
            "dirty": False, "last_sent": time.monotonic(),
        }
        return f'! "publishToken":"{name}" "value":{self._form(key, form)} +OK'

    def _unsubscribe(self, name: str) -> str:
        client = self._latest_client_id()
        subs = self._subs.get(client or "", {})
        if subs.pop(name, None) is None:
            raise ParleSimError(f"no subscription {name}")
        return "+OK"

    async def _pump(self, client_id: str) -> None:
        try:
            while client_id in self._clients:
                await asyncio.sleep(PUMP_TICK_S)
                now = time.monotonic()
                for name, sub in list(self._subs.get(client_id, {}).items()):
                    if not sub["dirty"] or now - sub["last_sent"] < sub["rate"]:
                        continue
                    sub["dirty"] = False
                    sub["last_sent"] = now
                    value = self._form(sub["key"], sub["form"])
                    await self.push_to(
                        client_id,
                        f'! "publishToken":"{name}" "value":{value}{CRLF}'.encode(),
                    )
        except asyncio.CancelledError:
            return
        finally:
            self._subs.pop(client_id, None)
            self._pumps.pop(client_id, None)
