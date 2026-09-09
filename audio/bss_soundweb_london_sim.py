"""
BSS Soundweb London — Direct Inject simulator.

A Soundweb London unit on TCP 1023 as the Interface Kit (rev 2.7) describes
it: STX / ETX frames with escaped reserved bytes and an XOR checksum, ACK for
every well-formed frame and NAK for a bad one, DI_SETSV writes, DI_SUBSCRIBESV
answered at once with the current value and thereafter on every change,
meters streamed at the subscribed rate, percent set / subscribe / bump, string
state variables (Appendix F), and venue / parameter preset recalls.

The design it holds is the driver's own object table: the device config's
``controls`` rows (or the driver's defaults when a device has none), so the
simulator addresses exactly what the driver addresses. Frames for another node
are acknowledged and otherwise ignored, and a state variable that is not in
the loaded design never answers — the two silences a wrong address produces on
real hardware, and what the driver's Test Connection reports.

Two behaviours the document states and this models on purpose:

- A DI_BUMPSVPERCENT changes the value but sends no subscription update
  (Harman help centre); the driver re-subscribes to read it back.
- Subscriptions live until unsubscribed or the unit reboots; a reconnecting
  driver starts with none.

Driver side: ``audio/bss_soundweb_london.py`` — the codec and the object
tables are imported from it so the two cannot drift.
"""


import asyncio
import importlib.util
import logging
import random
import sys
from pathlib import Path
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


def _load_driver_module():
    name = "openavc_sim_bss_soundweb_london_driver"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("bss_soundweb_london.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_d = _load_driver_module()

STX, ETX, ACK, NAK = _d.STX, _d.ETX, _d.ACK, _d.NAK
DI_SETSV, DI_SUBSCRIBESV, DI_UNSUBSCRIBESV = _d.DI_SETSV, _d.DI_SUBSCRIBESV, _d.DI_UNSUBSCRIBESV
DI_VENUE_PRESET_RECALL, DI_PARAM_PRESET_RECALL = _d.DI_VENUE_PRESET_RECALL, _d.DI_PARAM_PRESET_RECALL
DI_SETSVPERCENT, DI_SUBSCRIBESVPERCENT = _d.DI_SETSVPERCENT, _d.DI_SUBSCRIBESVPERCENT
DI_UNSUBSCRIBESVPERCENT, DI_BUMPSVPERCENT, DI_SETSTRINGSV = (
    _d.DI_UNSUBSCRIBESVPERCENT, _d.DI_BUMPSVPERCENT, _d.DI_SETSTRINGSV,
)
FMT_GAIN, FMT_METER, FMT_BOOL, FMT_INT, FMT_STRING = (
    _d.FMT_GAIN, _d.FMT_METER, _d.FMT_BOOL, _d.FMT_INT, _d.FMT_STRING,
)
PERCENT_SCALE = _d.PERCENT_SCALE

Key = tuple[int, int, int, int]

# Seed values per format, in the control's own units.
_SEED = {
    FMT_GAIN: 0.0, FMT_METER: -60.0, FMT_BOOL: False, FMT_INT: 1,
    _d.FMT_SCALAR: 0.0, _d.FMT_PERCENT: 50.0, _d.FMT_DELAY: 0.0,
    _d.FMT_FREQ: 1000.0, _d.FMT_SPEED: 100.0,
}


class BSSSoundwebLondonSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "bss_soundweb_london",
        "name": "BSS Soundweb London Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 1023,
        # Binary protocol: no line delimiter.
        "delimiter": None,
        "initial_state": {
            "model": "BLU-100",
            "node_address": "0x0000",
            "program_gain_db": 0.0,
            "program_mute": False,
            "mic_1_mute": False,
            "mic_2_mute": False,
            "source": 1,
            "last_venue_preset": 0,
            "last_parameter_preset": 0,
            "subscriptions": 0,
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "node_address", "label": "Node Address"},
            {"type": "indicator", "key": "subscriptions", "label": "Subscriptions"},
            {"type": "indicator", "key": "last_venue_preset", "label": "Last Venue Preset"},
            {"type": "indicator", "key": "last_parameter_preset", "label": "Last Parameter Preset"},
            # The default object table's front-panel-like controls: moving one
            # here pushes a DI_SETSV to every subscriber, as a wall controller
            # or Architect would.
            {"type": "slider", "key": "program_gain_db", "label": "Program Gain (dB)",
             "min": -100, "max": 10, "step": 0.5},
            {"type": "toggle", "key": "program_mute", "label": "Program Mute"},
            {"type": "toggle", "key": "mic_1_mute", "label": "Mics: Input 1 Mute"},
            {"type": "toggle", "key": "mic_2_mute", "label": "Mics: Input 2 Mute"},
            {"type": "slider", "key": "source", "label": "Source", "min": 1, "max": 8, "step": 1},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._delimiter = None
        self._line_mode = False

        cfg = self.config or {}
        try:
            self._node = _d.parse_node_address(cfg.get("node_address", ""))
        except ValueError:
            self._node = 0
        rows = cfg.get("controls") or _d.DEFAULT_CONTROLS
        objects, problems = _d.parse_controls_config(rows, self._node)
        for p in problems:
            logger.warning("%s: object list: %s", self.name, p)
        self._objects = objects
        # The design: every addressable SV with its format and current value.
        self._fmt: dict[Key, str] = {}
        self._values: dict[Key, Any] = {}
        self._labels: dict[Key, str] = {}
        for o in objects:
            for ctl in o.controls.values():
                key = o.key(ctl)
                self._fmt[key] = ctl.fmt
                self._labels[key] = f"{o.name}.{ctl.prop}"
                self._values[key] = "" if ctl.fmt == FMT_STRING else _SEED.get(ctl.fmt, 0)
        # Subscriptions: key -> rate_ms (0 = on change). Percent subscriptions
        # are kept apart because they answer in a different unit.
        self._subs: dict[Key, int] = {}
        self._percent_subs: set[Key] = set()
        self._meter_tasks: dict[Key, asyncio.Task] = {}
        self._rx = bytearray()
        # Simulator-UI keys -> (object name, control prop) for the default table.
        self._ui_map: dict[str, tuple[str, str]] = {
            "program_gain_db": ("Program", "gain"),
            "program_mute": ("Program", "mute"),
            "mic_1_mute": ("Mics", "input_1_mute"),
            "mic_2_mute": ("Mics", "input_2_mute"),
            "source": ("Source", "source"),
        }
        self.set_state("node_address", f"0x{self._node:04X}")

    # ── Lookups ──

    def _find(self, name: str, prop: str) -> Key | None:
        for o in self._objects:
            if o.name == name or o.cid == name:
                ctl = o.controls.get(prop)
                if ctl is not None:
                    return o.key(ctl)
        return None

    def value_of(self, name: str, prop: str) -> Any:
        """Test / UI hook: the current value of a control in its own units."""
        key = self._find(name, prop)
        return None if key is None else self._values.get(key)

    def is_subscribed(self, name: str, prop: str) -> bool:
        key = self._find(name, prop)
        return key is not None and key in self._subs

    @property
    def subscription_count(self) -> int:
        return len(self._subs) + len(self._percent_subs)

    def _node_ok(self, node: int) -> bool:
        return node == 0 or node == self._node

    # ── Framing ──

    def handle_command(self, data: bytes) -> bytes | None:
        self._rx.extend(data)
        out = bytearray()
        while True:
            frame, rest = _d.parse_di_stream(bytes(self._rx))
            self._rx = bytearray(rest)
            if frame is None:
                break
            if frame == b"":
                if not rest:
                    break
                continue
            if frame in (bytes([ACK]), bytes([NAK])):
                continue  # the controller acknowledging our notification
            body = _d.decode_frame(frame)
            if body is None:
                out.append(NAK)
                continue
            out.append(ACK)
            msg = _d.parse_body(body)
            if msg is None:
                continue
            reply = self._dispatch(msg)
            if reply:
                out += reply
        return bytes(out) if out else None

    def _dispatch(self, msg: Any) -> bytes:
        if msg.cmd == DI_VENUE_PRESET_RECALL:
            self.set_state("last_venue_preset", int(msg.raw))
            return b""
        if msg.cmd == DI_PARAM_PRESET_RECALL:
            self.set_state("last_parameter_preset", int(msg.raw))
            return b""
        if not self._node_ok(msg.node):
            return b""
        key = msg.key
        fmt = self._fmt.get(key)
        if fmt is None:
            return b""  # not in the loaded design: silence, as on the unit
        if msg.cmd == DI_SETSV:
            if fmt == FMT_STRING:
                return b""
            self._apply(key, _d.raw_to_value(fmt, msg.raw), notify=True)
            return b""
        if msg.cmd == DI_SETSTRINGSV:
            if fmt != FMT_STRING:
                return b""
            self._apply(key, (msg.text or "")[: _d.MAX_STRING_SV_LEN], notify=True)
            return b""
        if msg.cmd == DI_SUBSCRIBESV:
            rate = max(0, int(msg.raw))
            self._subs[key] = rate
            self.set_state("subscriptions", self.subscription_count)
            if fmt == FMT_METER and rate > 0:
                self._start_meter(key, rate)
            return self._value_frame(key)
        if msg.cmd == DI_UNSUBSCRIBESV:
            self._subs.pop(key, None)
            self._stop_meter(key)
            self.set_state("subscriptions", self.subscription_count)
            return b""
        if msg.cmd == DI_SETSVPERCENT:
            self._apply(key, self._from_percent(fmt, msg.raw / PERCENT_SCALE), notify=True)
            return b""
        if msg.cmd == DI_BUMPSVPERCENT:
            pct = self._to_percent(fmt, self._values[key]) + msg.raw / PERCENT_SCALE
            self._apply(key, self._from_percent(fmt, max(0.0, min(100.0, pct))), notify=False)
            return b""
        if msg.cmd == DI_SUBSCRIBESVPERCENT:
            self._percent_subs.add(key)
            self.set_state("subscriptions", self.subscription_count)
            return self._percent_frame(key)
        if msg.cmd == DI_UNSUBSCRIBESVPERCENT:
            self._percent_subs.discard(key)
            self.set_state("subscriptions", self.subscription_count)
            return b""
        return b""

    # ── Values ──

    def _apply(self, key: Key, value: Any, notify: bool) -> None:
        fmt = self._fmt[key]
        if fmt == FMT_BOOL:
            value = bool(value)
        elif fmt == FMT_INT:
            value = int(value)
        elif fmt == FMT_STRING:
            value = str(value)
        else:
            value = float(value)
        changed = self._values.get(key) != value
        self._values[key] = value
        self._mirror_to_ui(key, value)
        if changed and notify:
            self._notify(key)

    def _value_frame(self, key: Key) -> bytes:
        node, vd, obj, sv = key
        fmt = self._fmt[key]
        value = self._values[key]
        if fmt == FMT_STRING:
            return _d.build_set_string(node, vd, obj, sv, str(value))
        return _d.build_set(node, vd, obj, sv, _d.value_to_raw(fmt, value))

    def _percent_frame(self, key: Key) -> bytes:
        node, vd, obj, sv = key
        pct = self._to_percent(self._fmt[key], self._values[key])
        return _d.build_addressed(DI_SETSVPERCENT, node, vd, obj, sv, round(pct * PERCENT_SCALE))

    def _to_percent(self, fmt: str, value: Any) -> float:
        if fmt in (FMT_GAIN, FMT_METER):
            return (float(value) - _d.GAIN_DB_MIN) / (_d.GAIN_DB_MAX - _d.GAIN_DB_MIN) * 100.0
        if fmt == FMT_BOOL:
            return 100.0 if value else 0.0
        if fmt == FMT_STRING:
            return 0.0
        return max(0.0, min(100.0, float(value)))

    def _from_percent(self, fmt: str, pct: float) -> Any:
        pct = max(0.0, min(100.0, pct))
        if fmt in (FMT_GAIN, FMT_METER):
            return round(_d.GAIN_DB_MIN + pct / 100.0 * (_d.GAIN_DB_MAX - _d.GAIN_DB_MIN), 2)
        if fmt == FMT_BOOL:
            return pct >= 50.0
        if fmt == FMT_INT:
            return int(round(pct))
        if fmt == FMT_STRING:
            return ""
        return pct

    def _notify(self, key: Key) -> None:
        frames = b""
        if key in self._subs:
            frames += self._value_frame(key)
        if key in self._percent_subs:
            frames += self._percent_frame(key)
        if frames:
            self._schedule_push(frames)

    def _schedule_push(self, data: bytes) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._push_later(data))

    async def _push_later(self, data: bytes) -> None:
        await asyncio.sleep(0)  # let the ACK / reply go out first
        await self.push(data)

    # ── Meters ──

    def _start_meter(self, key: Key, rate_ms: int) -> None:
        self._stop_meter(key)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._meter_tasks[key] = loop.create_task(self._meter_loop(key, rate_ms))

    def _stop_meter(self, key: Key) -> None:
        task = self._meter_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _meter_loop(self, key: Key, rate_ms: int) -> None:
        try:
            while self._running and key in self._subs:
                await asyncio.sleep(rate_ms / 1000.0)
                self._wander_meter(key)
                await self.push(self._value_frame(key))
        except asyncio.CancelledError:
            return

    def _wander_meter(self, key: Key) -> None:
        level = float(self._values.get(key, -60.0))
        level = max(-100.0, min(0.0, level + random.uniform(-3.0, 3.0)))
        self._values[key] = round(level, 1)

    async def tick_meters(self) -> int:
        """Test hook: push one frame for every subscribed meter now."""
        n = 0
        for key, rate in list(self._subs.items()):
            if self._fmt.get(key) == FMT_METER and rate > 0:
                self._wander_meter(key)
                await self.push(self._value_frame(key))
                n += 1
        return n

    # ── Simulator UI bridge ──

    def _mirror_to_ui(self, key: Key, value: Any) -> None:
        for ui_key, (name, prop) in self._ui_map.items():
            if self._find(name, prop) == key:
                super().set_state(ui_key, value)

    def set_state(self, key: str, value: Any) -> None:
        target = getattr(self, "_ui_map", {}).get(key)
        if target is not None:
            dkey = self._find(*target)
            if dkey is not None:
                super().set_state(key, value)
                self._apply(dkey, value, notify=True)
                return
        super().set_state(key, value)

    def set_value(self, name: str, prop: str, value: Any) -> bool:
        """Test hook: a change made at the unit (Architect, a wall panel);
        subscribers hear about it."""
        key = self._find(name, prop)
        if key is None:
            return False
        self._apply(key, value, notify=True)
        return True

    async def stop(self) -> None:
        for key in list(self._meter_tasks):
            self._stop_meter(key)
        await super().stop()
