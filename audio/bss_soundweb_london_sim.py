"""
BSS Soundweb London — Direct Inject simulator.

A Soundweb London unit on TCP 1023 as the Interface Kit (rev 2.7) describes
it and as a BLU-100 on firmware 86.4.2 behaved on the bench: STX / ETX frames
with escaped reserved bytes and an XOR checksum, DI_SETSV writes, DI_SUBSCRIBESV
answered at once with the current value and thereafter on every change,
meters streamed at the subscribed rate, percent set / subscribe / bump,
string state variables (Appendix F), and venue / parameter preset recalls.

What the unit showed and this models, beyond the document:

- No ACK and no NAK over Ethernet. A frame with a bad checksum, or one cut
  short by the next STX, is dropped without a word.
- A frame to node 0 or to the unit's own node reaches it; every reply carries
  the unit's own node, never 0. A frame for any other node gets silence.
- Subscriptions belong to the session (the TCP connection) that made them.
- A change is passed on to every other subscribed session, never to the one
  that made it, in the form it was made: a value write as DI_SETSV, a percent
  write as DI_SETSVPERCENT, a bump as DI_SETSVPERCENT carrying only the bump
  amount, a string write as DI_SETSTRINGSV. A change made at the unit (the
  Simulator UI standing in for Architect or a wall panel) reaches every
  subscribed session.
- Gains run from -80 to +10 dB; a write beyond either end is clamped. Percent
  of travel is linear in the raw word between the ends.
- Meters report dB x 10000 and never below -80 dB; the input card's gain is
  one count per 6 dB step; a source selector's input numbering starts at 1.

A state variable that is not in the loaded design never answers.

The design it holds is the driver's own object table: the device config's
``controls`` rows (or the driver's defaults when a device has none), so the
simulator addresses exactly what the driver addresses. The unit's node is the
device config's ``node_address``, or 0x0001 when that is blank (a unit always
has one).

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

STX = _d.STX
DI_SETSV, DI_SUBSCRIBESV, DI_UNSUBSCRIBESV = _d.DI_SETSV, _d.DI_SUBSCRIBESV, _d.DI_UNSUBSCRIBESV
DI_VENUE_PRESET_RECALL, DI_PARAM_PRESET_RECALL = _d.DI_VENUE_PRESET_RECALL, _d.DI_PARAM_PRESET_RECALL
DI_SETSVPERCENT, DI_SUBSCRIBESVPERCENT = _d.DI_SETSVPERCENT, _d.DI_SUBSCRIBESVPERCENT
DI_UNSUBSCRIBESVPERCENT, DI_BUMPSVPERCENT, DI_SETSTRINGSV = (
    _d.DI_UNSUBSCRIBESVPERCENT, _d.DI_BUMPSVPERCENT, _d.DI_SETSTRINGSV,
)
FMT_GAIN, FMT_METER, FMT_BOOL, FMT_INT, FMT_STRING = (
    _d.FMT_GAIN, _d.FMT_METER, _d.FMT_BOOL, _d.FMT_INT, _d.FMT_STRING,
)
FMT_INPUT_GAIN = _d.FMT_INPUT_GAIN
PERCENT_SCALE = _d.PERCENT_SCALE
DEFAULT_UNIT_NODE = 0x0001

Key = tuple[int, int, int]          # (virtual device, object, state variable)

# The session a frame came from when the framework named no client (a test
# driving handle_command directly): one anonymous session.
LOCAL = "local"
# The unit itself (Architect, a wall panel): its changes reach every session.
UNIT = "unit"

GAIN_RAW_MIN = _d.gain_db_to_raw(_d.GAIN_DB_MIN)
GAIN_RAW_MAX = _d.gain_db_to_raw(_d.GAIN_DB_MAX)
INPUT_GAIN_STEPS = _d.INPUT_GAIN_MAX_DB // _d.INPUT_GAIN_STEP_DB

# Seed values per format, in the control's own units.
_SEED = {
    FMT_GAIN: 0.0, FMT_METER: -80.0, FMT_INPUT_GAIN: 0, FMT_BOOL: False, FMT_INT: 1,
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
            "node_address": "0x0001",
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
             "min": -80, "max": 10, "step": 0.5},
            {"type": "toggle", "key": "program_mute", "label": "Program Mute"},
            {"type": "toggle", "key": "mic_1_mute", "label": "Mics: Input 1 Mute"},
            {"type": "toggle", "key": "mic_2_mute", "label": "Mics: Input 2 Mute"},
            {"type": "slider", "key": "source", "label": "Source", "min": 0, "max": 8, "step": 1},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._delimiter = None
        self._line_mode = False

        cfg = self.config or {}
        try:
            configured = _d.parse_node_address(cfg.get("node_address", ""))
        except ValueError:
            configured = 0
        self._node = configured or DEFAULT_UNIT_NODE
        rows = cfg.get("controls") or _d.DEFAULT_CONTROLS
        objects, problems = _d.parse_controls_config(rows, configured)
        for p in problems:
            logger.warning("%s: object list: %s", self.name, p)
        # Rows with a full address for another node describe another unit.
        self._objects = [o for o in objects if o.node in (0, self._node)]
        # The design: every addressable SV with its format and current value.
        self._fmt: dict[Key, str] = {}
        self._values: dict[Key, Any] = {}
        for o in self._objects:
            for ctl in o.controls.values():
                key = (o.vd, o.obj, ctl.sv)
                self._fmt[key] = ctl.fmt
                self._values[key] = "" if ctl.fmt == FMT_STRING else _SEED.get(ctl.fmt, 0)
        # Per session: key -> rate_ms (0 = on change), and percent subscriptions.
        self._subs: dict[str, dict[Key, int]] = {}
        self._percent_subs: dict[str, set[Key]] = {}
        self._meter_tasks: dict[tuple[str, Key], asyncio.Task] = {}
        self._task_sessions: dict[asyncio.Task, str] = {}
        self._rx: dict[str, bytearray] = {}
        # Simulator-UI keys -> (object name, control prop) for the default table.
        self._ui_map: dict[str, tuple[str, str]] = {
            "program_gain_db": ("Program", "gain"),
            "program_mute": ("Program", "mute"),
            "mic_1_mute": ("Mics", "input_1_mute"),
            "mic_2_mute": ("Mics", "input_2_mute"),
            "source": ("Source", "source"),
        }
        super().set_state("node_address", f"0x{self._node:04X}")

    # ── Sessions ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        # Called from the client's own task, which also runs handle_command.
        try:
            self._task_sessions[asyncio.current_task()] = client_id
        except RuntimeError:
            pass
        return None

    def _session(self) -> str:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return LOCAL
        return self._task_sessions.get(task, LOCAL)

    def _live_sessions(self) -> set[str]:
        sessions = set(self._subs) | set(self._percent_subs)
        clients = getattr(self, "_clients", None)
        if clients is not None:
            gone = {s for s in sessions if s not in (LOCAL,) and s not in clients}
            for s in gone:
                self._drop_session(s)
            sessions -= gone
        return sessions

    def _drop_session(self, session: str) -> None:
        self._subs.pop(session, None)
        self._percent_subs.pop(session, None)
        for sk in [sk for sk in self._meter_tasks if sk[0] == session]:
            self._stop_meter(*sk)
        self.set_state("subscriptions", self.subscription_count)

    # ── Lookups ──

    def _find(self, name: str, prop: str) -> Key | None:
        for o in self._objects:
            if o.name == name or o.cid == name:
                ctl = o.controls.get(prop)
                if ctl is not None:
                    return (o.vd, o.obj, ctl.sv)
        return None

    def value_of(self, name: str, prop: str) -> Any:
        """Test / UI hook: the current value of a control in its own units."""
        key = self._find(name, prop)
        return None if key is None else self._values.get(key)

    def is_subscribed(self, name: str, prop: str) -> bool:
        key = self._find(name, prop)
        return key is not None and any(key in subs for subs in self._subs.values())

    @property
    def subscription_count(self) -> int:
        return (sum(len(s) for s in self._subs.values())
                + sum(len(s) for s in self._percent_subs.values()))

    def _node_ok(self, node: int) -> bool:
        return node in (0, self._node)

    # ── Framing ──

    def handle_command(self, data: bytes) -> bytes | None:
        session = self._session()
        rx = self._rx.setdefault(session, bytearray())
        rx.extend(data)
        out = bytearray()
        while True:
            frame, rest = _d.parse_di_stream(bytes(rx))
            rx[:] = rest
            if frame is None:
                break
            if frame == b"":
                if not rest:
                    break
                continue
            if frame[0] != STX:
                continue  # a stray ACK / NAK byte: ignored, as on the unit
            body = _d.decode_frame(frame)
            if body is None:
                continue  # bad checksum: dropped without a NAK, as on the unit
            msg = _d.parse_body(body)
            if msg is None:
                continue
            reply = self._dispatch(session, msg)
            if reply:
                out += reply
        return bytes(out) if out else None

    def _dispatch(self, session: str, msg: Any) -> bytes:
        if msg.cmd == DI_VENUE_PRESET_RECALL:
            self.set_state("last_venue_preset", int(msg.raw))
            return b""
        if msg.cmd == DI_PARAM_PRESET_RECALL:
            self.set_state("last_parameter_preset", int(msg.raw))
            return b""
        if not self._node_ok(msg.node):
            return b""
        key = (msg.vd, msg.obj, msg.sv)
        fmt = self._fmt.get(key)
        if fmt is None:
            return b""  # not in the loaded design: silence, as on the unit
        if msg.cmd == DI_SETSV:
            if fmt == FMT_STRING:
                return b""
            self._apply(key, self._from_raw(fmt, msg.raw), origin=session,
                        relay=lambda: self._value_frame(key))
            return b""
        if msg.cmd == DI_SETSTRINGSV:
            if fmt != FMT_STRING:
                return b""
            self._apply(key, (msg.text or "")[: _d.MAX_STRING_SV_LEN], origin=session,
                        relay=lambda: self._value_frame(key))
            return b""
        if msg.cmd == DI_SUBSCRIBESV:
            rate = max(0, int(msg.raw))
            self._subs.setdefault(session, {})[key] = rate
            self.set_state("subscriptions", self.subscription_count)
            if fmt == FMT_METER and rate > 0:
                self._start_meter(session, key, rate)
            return self._value_frame(key)
        if msg.cmd == DI_UNSUBSCRIBESV:
            self._subs.get(session, {}).pop(key, None)
            self._stop_meter(session, key)
            self.set_state("subscriptions", self.subscription_count)
            return b""
        if msg.cmd == DI_SETSVPERCENT:
            pct = msg.raw / PERCENT_SCALE
            self._apply(key, self._from_percent(fmt, pct), origin=session,
                        relay=lambda: self._percent_word(key, msg.raw))
            return b""
        if msg.cmd == DI_BUMPSVPERCENT:
            pct = self._to_percent(fmt, self._values[key]) + msg.raw / PERCENT_SCALE
            # The unit passes a bump on as a percent message carrying only the
            # amount it moved, not where the control ended up.
            self._apply(key, self._from_percent(fmt, max(0.0, min(100.0, pct))), origin=session,
                        relay=lambda: self._percent_word(key, msg.raw))
            return b""
        if msg.cmd == DI_SUBSCRIBESVPERCENT:
            self._percent_subs.setdefault(session, set()).add(key)
            self.set_state("subscriptions", self.subscription_count)
            return self._percent_frame(key)
        if msg.cmd == DI_UNSUBSCRIBESVPERCENT:
            self._percent_subs.get(session, set()).discard(key)
            self.set_state("subscriptions", self.subscription_count)
            return b""
        return b""

    # ── Values ──

    def _from_raw(self, fmt: str, raw: int) -> Any:
        if fmt == FMT_GAIN:
            raw = max(GAIN_RAW_MIN, min(GAIN_RAW_MAX, raw))
        elif fmt == FMT_INPUT_GAIN:
            raw = max(0, min(INPUT_GAIN_STEPS, raw))
        return _d.raw_to_value(fmt, raw)

    def _raw(self, key: Key) -> int:
        fmt = self._fmt[key]
        value = self._values[key]
        if fmt == FMT_GAIN:
            return max(GAIN_RAW_MIN, min(GAIN_RAW_MAX, _d.gain_db_to_raw(value)))
        return _d.value_to_raw(fmt, value)

    def _apply(self, key: Key, value: Any, origin: str, relay: Any = None) -> None:
        fmt = self._fmt[key]
        if fmt == FMT_BOOL:
            value = bool(value)
        elif fmt in (FMT_INT, FMT_INPUT_GAIN):
            value = int(value)
        elif fmt == FMT_STRING:
            value = str(value)
        else:
            value = float(value)
        changed = self._values.get(key) != value
        self._values[key] = value
        self._mirror_to_ui(key, value)
        if changed:
            self._notify(key, origin, relay)

    def _value_frame(self, key: Key) -> bytes:
        vd, obj, sv = key
        fmt = self._fmt[key]
        value = self._values[key]
        if fmt == FMT_STRING:
            return _d.build_set_string(self._node, vd, obj, sv, str(value))
        return _d.build_set(self._node, vd, obj, sv, self._raw(key))

    def _percent_word(self, key: Key, word: int) -> bytes:
        vd, obj, sv = key
        return _d.build_addressed(DI_SETSVPERCENT, self._node, vd, obj, sv, word)

    def _percent_frame(self, key: Key) -> bytes:
        pct = self._to_percent(self._fmt[key], self._values[key])
        return self._percent_word(key, round(pct * PERCENT_SCALE))

    def _to_percent(self, fmt: str, value: Any) -> float:
        if fmt == FMT_GAIN:
            raw = max(GAIN_RAW_MIN, min(GAIN_RAW_MAX, _d.gain_db_to_raw(value)))
            return (raw - GAIN_RAW_MIN) / (GAIN_RAW_MAX - GAIN_RAW_MIN) * 100.0
        if fmt == FMT_INPUT_GAIN:
            return int(value) / _d.INPUT_GAIN_STEP_DB / INPUT_GAIN_STEPS * 100.0
        if fmt == FMT_BOOL:
            return 100.0 if value else 0.0
        if fmt in (FMT_STRING, FMT_METER):
            return 0.0
        return max(0.0, min(100.0, float(value)))

    def _from_percent(self, fmt: str, pct: float) -> Any:
        pct = max(0.0, min(100.0, pct))
        if fmt == FMT_GAIN:
            raw = round(GAIN_RAW_MIN + pct / 100.0 * (GAIN_RAW_MAX - GAIN_RAW_MIN))
            return _d.raw_to_value(FMT_GAIN, raw)
        if fmt == FMT_INPUT_GAIN:
            return round(pct / 100.0 * INPUT_GAIN_STEPS) * _d.INPUT_GAIN_STEP_DB
        if fmt == FMT_BOOL:
            return pct >= 50.0
        if fmt == FMT_INT:
            return int(round(pct))
        if fmt == FMT_STRING:
            return ""
        return pct

    def _notify(self, key: Key, origin: str, relay: Any = None) -> None:
        """Pass a change on to every subscribed session except the one that
        made it, in the form it was made; a change at the unit reaches every
        session, as a value or (to a percent subscriber) a percent."""
        for session in self._live_sessions():
            if session == origin:
                continue
            raw_sub = key in self._subs.get(session, {})
            pct_sub = key in self._percent_subs.get(session, set())
            if not (raw_sub or pct_sub):
                continue
            if relay is not None:
                frame = relay()
            elif raw_sub:
                frame = self._value_frame(key)
            else:
                frame = self._percent_frame(key)
            self._schedule_push(session, frame)

    def _schedule_push(self, session: str, data: bytes) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._push_later(session, data))

    async def _push_later(self, session: str, data: bytes) -> None:
        await asyncio.sleep(0)  # let the reply to the sender go out first
        await self._push_session(session, data)

    async def _push_session(self, session: str, data: bytes) -> None:
        if session == LOCAL or not hasattr(self, "push_to"):
            await self.push(data)
        else:
            await self.push_to(session, data)

    # ── Meters ──

    def _start_meter(self, session: str, key: Key, rate_ms: int) -> None:
        self._stop_meter(session, key)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._meter_tasks[(session, key)] = loop.create_task(self._meter_loop(session, key, rate_ms))

    def _stop_meter(self, session: str, key: Key) -> None:
        task = self._meter_tasks.pop((session, key), None)
        if task is not None and not task.done():
            task.cancel()

    async def _meter_loop(self, session: str, key: Key, rate_ms: int) -> None:
        try:
            while self._running and key in self._subs.get(session, {}):
                await asyncio.sleep(rate_ms / 1000.0)
                self._wander_meter(key)
                await self._push_session(session, self._value_frame(key))
        except asyncio.CancelledError:
            return

    def _wander_meter(self, key: Key) -> None:
        level = float(self._values.get(key, _d.METER_DB_MIN))
        level = max(_d.METER_DB_MIN, min(0.0, level + random.uniform(-3.0, 3.0)))
        self._values[key] = round(level, 1)

    async def tick_meters(self) -> int:
        """Test hook: push one frame for every subscribed meter now."""
        n = 0
        for session, subs in list(self._subs.items()):
            for key, rate in list(subs.items()):
                if self._fmt.get(key) == FMT_METER and rate > 0:
                    self._wander_meter(key)
                    await self._push_session(session, self._value_frame(key))
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
                self._apply(dkey, value, origin=UNIT)
                return
        super().set_state(key, value)

    def set_value(self, name: str, prop: str, value: Any) -> bool:
        """Test hook: a change made at the unit (Architect, a wall panel);
        every subscribed session hears about it."""
        key = self._find(name, prop)
        if key is None:
            return False
        self._apply(key, value, origin=UNIT)
        return True

    def write_from_other_session(self, name: str, prop: str, *, percent: float | None = None,
                                 bump: float | None = None) -> bool:
        """Test hook: another controller on its own connection sets a control
        by percent, or bumps it; the unit tells the other sessions."""
        key = self._find(name, prop)
        if key is None:
            return False
        fmt = self._fmt[key]
        if percent is not None:
            word = round(percent * PERCENT_SCALE)
            self._apply(key, self._from_percent(fmt, percent), origin="other",
                        relay=lambda: self._percent_word(key, word))
        elif bump is not None:
            word = round(bump * PERCENT_SCALE)
            pct = self._to_percent(fmt, self._values[key]) + bump
            self._apply(key, self._from_percent(fmt, pct), origin="other",
                        relay=lambda: self._percent_word(key, word))
        return True

    async def stop(self) -> None:
        for sk in list(self._meter_tasks):
            self._stop_meter(*sk)
        await super().stop()
