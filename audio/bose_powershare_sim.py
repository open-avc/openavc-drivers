"""
Bose Professional PowerShare PS404D / PS604D — Serial Control Protocol
simulator.

A PowerShare amplifier on TCP 10055 as the ControlSpace Serial Control
Protocol v5.15 describes it: CR-terminated ASCII; system and device commands
(SS / GS, SG / GG / SH, SN / GN, SV / GV / SI, SM / GM, GL, IP, NP, RESET,
SY / GY, GH / CH) that answer only when they are queries, in hexadecimal;
module commands (SA / GA) on the amplifier's fixed modules (Matrix 1,
PEQ-9band n, SpeakerPEQ n, Band Pass n, Limiter n, Delay n) and on the
renamable Input and Amp Output modules, answered with a bare ACK (0x06) or
``NAK nn``; several module commands on one line separated by semicolons; and
SUB / UNS, which send a value at once and again on every change.

What the document says, modelled on purpose:

- A Set Volume or Increment / Decrement on a muted channel is ignored (5.1,
  5.2).
- Entering or leaving standby is not immediate (5.8): GY reports the new
  state ``standby_transition_s`` after the command (sim config, default 1 s).
- A change made by serial command is not notified back to the connection
  that made it (section 6, "Automatic notification"); changes from anywhere
  else (the Simulator UI, the test hooks) go to every subscriber.
  ``echo_own_changes: true`` (sim config) notifies the writer too.
- PowerMatch-only commands (GC, GF / SF, GR / SR, CF) and the Input module's
  analog sensitivity are not answered on a PowerShare: GC and its siblings
  draw silence, the sensitivity index a NAK 02.
- The matrix answers the cross-points (Input # - 1) x 8 + Output # for
  inputs and outputs 1-4 (6.2.4), and NAK 02 for any other index.

The Input and Amp Output labels are the device config's ``input_n_label`` /
``output_n_label`` (blank: ``In n`` / ``Out n``, which stand in for whatever
the design calls them). ``push_supported: false`` makes the amplifier ignore
``SUB``. ``alarm_log`` (a list of "time, date, description" strings) seeds
the alarm history.

Driver side: ``audio/bose_powershare.py``; its codec and fixed labels are
imported from it so the two cannot drift.
"""


import asyncio
import importlib.util
import logging
import random
import re
import sys
from pathlib import Path
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


def _load_driver_module():
    name = "openavc_sim_bose_powershare_driver"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("bose_powershare.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_d = _load_driver_module()

ACK, NAK = _d.ACK, _d.NAK
CH = _d.CHANNELS
_HEX = r"([0-9a-fA-F]+)"
_SA_RE = re.compile(r'^SA\s*"([^"]*)"((?:>[^>=]*)+)=(.*)$')
_GA_RE = re.compile(r'^GA\s*"([^"]*)"((?:>[^>=]*)+)$')
_SUB_RE = re.compile(r'^(SUB|UNS)\s*"(.*)"$')

DEFAULT_ALARM_LOG = [
    "09:14:07, 11/03/2026, Output 2 Limiting",
    "09:14:09, 11/03/2026, Output 2 Limiting Cleared",
]

# parameter Index 2 -> (kind, low, high or choices) for the stored modules.
_PEQ = {"1": ("num", 20, 20000), "2": ("num", 0.1, 10.0), "3": ("num", -20.0, 20.0),
        "4": ("choice", ["0", "-6", "-12"]), "5": ("choice", _d.INPUT_PEQ_TYPES), "6": ("onoff",)}
_SPEQ = {"1": ("num", 200, 20000), "2": ("num", 0.1, 10.0), "3": ("num", -20.0, 20.0),
         "5": ("choice", _d.SPEAKER_PEQ_TYPES), "6": ("onoff",)}
_FILTERS = [t for t, _ in _d.FILTER_TYPES]
_BP = {"1": ("choice", _FILTERS), "2": ("num", 20, 20000), "3": ("choice", _FILTERS),
       "4": ("num", 20, 20000), "8": ("onoff",), "9": ("onoff",)}
_LIM = {"1": ("num", 0.5, 142.0), "2": ("num", 0.5, 142.0), "3": ("num", 500, 10000),
        "4": ("num", 500, 10000)}


class BosePowerShareSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "bose_powershare",
        "name": "Bose PowerShare Amplifier Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": _d.DEFAULT_PORT,
        "delimiter": "\r",
        "initial_state": {
            "model": "PS604D",
            "power": True,
            "push_supported": True,
            "subscriptions": 0,
            "parameter_set": 0,
            "alarm_count": 2,
            "output_1_level_db": 0.0,
            "output_1_mute": False,
            "input_1_mute": False,
            "input_1_to_output_1": True,
            "raise_alarm": False,
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "toggle", "key": "power", "label": "Power (off = standby)"},
            {"type": "toggle", "key": "push_supported", "label": "Answers SUB (subscriptions)"},
            {"type": "indicator", "key": "subscriptions", "label": "Subscriptions"},
            {"type": "indicator", "key": "parameter_set", "label": "Last Parameter Set"},
            # Front-of-house changes, as a ControlCenter zone controller or
            # Designer would make them: subscribers hear about each.
            {"type": "slider", "key": "output_1_level_db", "label": "Output 1 Level (dB)",
             "min": -60.5, "max": 0, "step": 0.5},
            {"type": "toggle", "key": "output_1_mute", "label": "Output 1 Mute"},
            {"type": "toggle", "key": "input_1_mute", "label": "Input 1 Mute"},
            {"type": "toggle", "key": "input_1_to_output_1", "label": "Matrix: Input 1 to Output 1"},
            {"type": "indicator", "key": "alarm_count", "label": "Alarm Log Entries"},
            {"type": "toggle", "key": "raise_alarm", "label": "Add an alarm to the log"},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        self._in_labels = {c: (str(cfg.get(f"input_{c}_label") or "").strip() or f"In {c}")
                           for c in range(1, CH + 1)}
        self._out_labels = {c: (str(cfg.get(f"output_{c}_label") or "").strip() or f"Out {c}")
                            for c in range(1, CH + 1)}
        groups, _ = _d.parse_groups_config(cfg.get("groups") or [])
        self._groups = set(groups)
        self._group_level = {n: 0x78 for n in self._groups}
        self._group_mute = {n: False for n in self._groups}
        self._standby = False
        self._standby_timer: asyncio.TimerHandle | None = None
        try:
            self._transition_s = float(cfg.get("standby_transition_s", 1.0))
        except (TypeError, ValueError):
            self._transition_s = 1.0
        self._parameter_set = 0
        self._out_level = {c: 0x78 for c in range(1, CH + 1)}
        self._mute = {(s, c): False for s in (_d.INPUT_SLOT, _d.OUTPUT_SLOT) for c in range(1, CH + 1)}
        self._source = {c: False for c in range(1, CH + 1)}           # False = analog
        self._polarity = {c: False for c in range(1, CH + 1)}
        self._xp_on = {(i, o): i == o for i in range(1, CH + 1) for o in range(1, CH + 1)}
        self._xp_level = {(i, o): 0.0 for i in range(1, CH + 1) for o in range(1, CH + 1)}
        self._mx_in_mute = {c: False for c in range(1, CH + 1)}
        self._mx_out_mute = {c: False for c in range(1, CH + 1)}
        # Stored module parameters: (label lowered, index path) -> wire text.
        self._params: dict[tuple[str, tuple[str, ...]], str] = {}
        for c in range(1, CH + 1):
            for b in range(1, 10):
                for k, v in {"1": "1000", "2": "1", "3": "0", "4": "0", "5": "B", "6": "F"}.items():
                    self._params[(_d.peq_label(c).lower(), (str(b), k))] = v
                for k, v in {"1": "1000", "2": "1", "3": "0", "5": "B", "6": "F"}.items():
                    self._params[(_d.speaker_peq_label(c).lower(), (str(b), k))] = v
            for k, v in {"1": "But24", "2": "20", "3": "But24", "4": "20000", "8": "O", "9": "O"}.items():
                self._params[(_d.band_pass_label(c).lower(), ("1", k))] = v
            for k, v in {"1": "71", "2": "50", "3": "1000", "4": "1000"}.items():
                self._params[(_d.limiter_label(c).lower(), ("0", k))] = v
            self._params[(_d.delay_label(c).lower(), ("1", "1"))] = "0"
            self._params[(_d.delay_label(c).lower(), ("1", "3"))] = "F"
        self._meters = {_d.GL_INPUTS: [0x50] * CH, _d.GL_OUTPUTS: [0x40] * CH}
        self._network = {"ip": "192.168.0.170", "mask": "255.255.255.0", "gateway": "192.168.0.1",
                         "addressing": "S"}
        self._pending_network: dict[str, str] = {}
        log = cfg.get("alarm_log")
        self._alarms: list[str] = [str(x) for x in log] if isinstance(log, list) else list(DEFAULT_ALARM_LOG)
        self._subs: set[str] = set()
        self._push_ok = bool(cfg.get("push_supported", True))
        self._echo_own = bool(cfg.get("echo_own_changes", False))
        super().set_state("push_supported", self._push_ok)
        super().set_state("alarm_count", len(self._alarms))

    # ── Test hooks ──

    @property
    def standby(self) -> bool:
        return self._standby

    @property
    def subscription_count(self) -> int:
        return len(self._subs)

    def is_subscribed(self, get_text: str) -> bool:
        return _norm(get_text) in self._subs

    def output_level_raw(self, c: int) -> int:
        return self._out_level[c]

    def muted(self, slot: str, c: int) -> bool:
        return self._mute[(slot, c)]

    def crosspoint(self, i: int, o: int) -> tuple[bool, float]:
        return self._xp_on[(i, o)], self._xp_level[(i, o)]

    def param(self, label: str, path: str) -> str | None:
        return self._params.get((label.lower(), tuple(path.split(">"))))

    def source_is_dante(self, c: int) -> bool:
        return self._source[c]

    def polarity_inverted(self, c: int) -> bool:
        return self._polarity[c]

    @property
    def parameter_set(self) -> int:
        return self._parameter_set

    @property
    def alarms(self) -> list[str]:
        return list(self._alarms)

    def set_output_level(self, c: int, db: float) -> None:
        """A level moved at a zone controller; subscribers hear it."""
        self._out_level[c] = int(_d.db_to_hex_level(db), 16)
        self._changed(f"GV {_d.OUTPUT_SLOT},{c:x}", own=False)
        if c == 1:
            super().set_state("output_1_level_db", _d.hex_level_to_db(self._out_level[c]))

    def set_mute(self, slot: str, c: int, muted: bool) -> None:
        self._mute[(slot, c)] = muted
        self._changed(f"GM {slot},{c:x}", own=False)

    def set_crosspoint(self, i: int, o: int, on: bool) -> None:
        self._xp_on[(i, o)] = on
        self._changed(_d.ga_line(_d.MATRIX_LABEL, f"1>{_d.crosspoint_index(i, o)}"), own=False)

    def set_parameter_set(self, n: int) -> None:
        self._parameter_set = n
        super().set_state("parameter_set", n)
        self._changed("GS", own=False)

    def set_standby(self, standby: bool) -> None:
        """Auto-standby, or a parameter set carrying the Standby property."""
        self._standby = standby
        super().set_state("power", not standby)

    def add_alarm(self, entry: str) -> None:
        self._alarms.append(entry)
        super().set_state("alarm_count", len(self._alarms))

    # ── Framing ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("ascii", errors="replace").strip("\r\n")
        if not line.strip():
            return None
        out = bytearray()
        for cmd in _split_commands(line):
            reply = self._dispatch(cmd.strip())
            if reply:
                out += reply
        return bytes(out) if out else None

    def _dispatch(self, cmd: str) -> bytes:
        if not cmd:
            return b""
        upper = cmd.upper()
        m = _SA_RE.match(cmd)
        if m:
            return self._set_module(m.group(1), _split_path(m.group(2)), m.group(3).strip())
        m = _GA_RE.match(cmd)
        if m:
            return self._get_module(m.group(1), _split_path(m.group(2)))
        if upper == "SUB":
            return b"SUB yes\r" if self._push_ok else b""
        m = _SUB_RE.match(cmd)
        if m:
            return self._subscribe(m.group(1).upper(), m.group(2))
        # Standby (5.8).
        m = re.match(r"^SY\s*([SNsn])$", cmd)
        if m:
            self._begin_standby(m.group(1).upper() == "S")
            return b""
        if upper == "GY":
            return f"GY {'S' if self._standby else 'N'}\r".encode()
        # Parameter sets (4.1).
        m = re.match(rf"^SS\s*{_HEX}$", cmd)
        if m:
            n = int(m.group(1), 16)
            if 1 <= n <= _d.PARAMETER_SET_MAX:
                self._parameter_set = n
                super().set_state("parameter_set", n)
                self._changed("GS", own=True)
            return b""
        if upper == "GS":
            return f"S {self._parameter_set:x}\r".encode()
        # Groups (4.2-4.4): amplifier levels stop at 78h.
        m = re.match(rf"^SG\s*{_HEX},{_HEX}$", cmd)
        if m:
            n, v = int(m.group(1), 16), int(m.group(2), 16)
            if n in self._groups:
                self._group_level[n] = v if v == 0xFF else max(0, min(_d.HEX_LEVEL_MAX, v))
                self._changed(f"GG {n:x}", own=True)
            return b""
        m = re.match(rf"^GG\s*{_HEX}$", cmd)
        if m:
            return self._gg(int(m.group(1), 16))
        m = re.match(rf"^SH\s*{_HEX},([01]),{_HEX}$", cmd)
        if m:
            n, up, steps = int(m.group(1), 16), m.group(2) == "1", int(m.group(3), 16)
            if n in self._groups:
                cur = self._group_level[n]
                cur = 0 if cur == 0xFF else cur
                self._group_level[n] = max(0, min(_d.HEX_LEVEL_MAX, cur + (steps if up else -steps)))
                self._changed(f"GG {n:x}", own=True)
            return b""
        m = re.match(rf"^SN\s*{_HEX},([MUTmut])$", cmd)
        if m:
            n, s = int(m.group(1), 16), m.group(2).upper()
            if n in self._groups:
                self._group_mute[n] = {"M": True, "U": False}.get(s, not self._group_mute[n])
                self._changed(f"GN {n:x}", own=True)
            return b""
        m = re.match(rf"^GN\s*{_HEX}$", cmd)
        if m:
            return self._gn(int(m.group(1), 16))
        # Slot volume and mute (5.1-5.3): volume on outputs only.
        m = re.match(rf"^SV\s*{_HEX},{_HEX},{_HEX}$", cmd)
        if m:
            slot, c = m.group(1).lower(), int(m.group(2), 16)
            if slot == _d.OUTPUT_SLOT and 1 <= c <= CH and not self._mute[(slot, c)]:
                v = int(m.group(3), 16)
                self._out_level[c] = v if v == 0xFF else max(0, min(_d.HEX_LEVEL_MAX, v))
                self._mirror_output_1(c)
                self._changed(f"GV {slot},{c:x}", own=True)
            return b""
        m = re.match(rf"^GV\s*{_HEX},{_HEX}$", cmd)
        if m:
            return self._gv(m.group(1).lower(), int(m.group(2), 16))
        m = re.match(rf"^SI\s*{_HEX},{_HEX},([01]),{_HEX}$", cmd)
        if m:
            slot, c = m.group(1).lower(), int(m.group(2), 16)
            if slot == _d.OUTPUT_SLOT and 1 <= c <= CH and not self._mute[(slot, c)]:
                cur = self._out_level[c]
                cur = 0 if cur == 0xFF else cur
                delta = int(m.group(4), 16) * (1 if m.group(3) == "1" else -1)
                self._out_level[c] = max(0, min(_d.HEX_LEVEL_MAX, cur + delta))
                self._mirror_output_1(c)
                self._changed(f"GV {slot},{c:x}", own=True)
            return b""
        m = re.match(rf"^SM\s*{_HEX},{_HEX},([MUTmut])$", cmd)
        if m:
            key = (m.group(1).lower(), int(m.group(2), 16))
            if key in self._mute:
                s = m.group(3).upper()
                self._mute[key] = {"M": True, "U": False}.get(s, not self._mute[key])
                self._mirror_mutes(key)
                self._changed(f"GM {key[0]},{key[1]:x}", own=True)
            return b""
        m = re.match(rf"^GM\s*{_HEX},{_HEX}$", cmd)
        if m:
            return self._gm(m.group(1).lower(), int(m.group(2), 16))
        m = re.match(rf"^GL\s*{_HEX}$", cmd)
        if m:
            levels = self._meters.get(m.group(1).lower())
            if levels is None:
                return b""
            for i, v in enumerate(levels):
                levels[i] = max(0, min(0x78, v + random.randint(-4, 4)))
            return f"GL {m.group(1).lower()} [{','.join(format(v, 'x') for v in levels)}]\r".encode()
        # Alarm log (5.13).
        if upper == "GH":
            return ("GH [" + "\r".join(self._alarms) + "]\r").encode()
        if upper == "CH":
            self._alarms.clear()
            super().set_state("alarm_count", 0)
            return bytes([ACK]) + b"\r"
        # Network (5.5-5.7).
        if upper == "IP":
            return f"IP {self._network['ip']}\r".encode()
        m = re.match(r"^IP\s+(\d{1,3}(?:\.\d{1,3}){3})$", cmd)
        if m:
            self._pending_network["ip"] = m.group(1)   # 5.5: takes effect after a reboot
            return b""
        m = re.match(r"^NP\s*([TMGtmg])$", cmd)
        if m:
            p = m.group(1).upper()
            v = {"T": self._network["addressing"], "M": self._network["mask"],
                 "G": self._network["gateway"]}[p]
            return f"NP {p},{v}\r".encode()
        m = re.match(r"^NP\s*([TMGtmg]),(.+)$", cmd)
        if m:
            p, v = m.group(1).upper(), m.group(2).strip()
            # 5.6: every network change takes effect after a reboot.
            if p == "T" and v.upper()[:1] in ("D", "S"):
                self._pending_network["addressing"] = v.upper()[:1]
            elif p == "M":
                self._pending_network["mask"] = v
            elif p == "G":
                self._pending_network["gateway"] = v
            return b""
        if upper == "NP F":
            self._pending_network = {"ip": "169.254.0.1", "mask": "255.255.0.0", "gateway": "0.0.0.0",
                                     "addressing": "D"}
            return b""
        if upper == "RESET":
            self._network.update(self._pending_network)
            self._pending_network = {}
            self._parameter_set = 0
            super().set_state("parameter_set", 0)
            self._subs.clear()
            super().set_state("subscriptions", 0)
            return b""
        if upper[:2] in ("SA", "GA", "MA"):
            return _nak("99")
        # GC, GF / SF, GR / SR, CF (PowerMatch only) and anything else: silence.
        return b""

    # ── Standby ──

    def _begin_standby(self, standby: bool) -> None:
        if self._standby_timer is not None:
            self._standby_timer.cancel()
            self._standby_timer = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.set_standby(standby)
            return
        if self._transition_s <= 0:
            self.set_standby(standby)
            return
        self._standby_timer = loop.call_later(self._transition_s, self.set_standby, standby)

    # ── Module commands ──

    def _set_module(self, label: str, path: tuple[str, ...], raw: str) -> bytes:
        name = label.lower()
        value = raw.strip().strip('"')
        if name == _d.MATRIX_LABEL.lower():
            return self._set_matrix(path, value)
        for c in range(1, CH + 1):
            if name == self._in_labels[c].lower():
                return self._set_input(c, path, value)
            if name == self._out_labels[c].lower():
                return self._set_output(c, path, value)
        spec = self._spec(name, path)
        if spec is None:
            return _nak("01") if not self._known_label(name) else _nak("02")
        try:
            wire = _check(spec, value, self._params.get((name, path)))
        except ValueError:
            return _nak("03")
        if self._params.get((name, path)) != wire:
            self._params[(name, path)] = wire
            self._changed(self._ga_text(name, path), own=True)
        return bytes([ACK])

    def _get_module(self, label: str, path: tuple[str, ...]) -> bytes:
        text = self._value(label.lower(), path)
        if text is None:
            return _nak("01") if not self._known_label(label.lower()) else _nak("02")
        return f'GA "{self._display(label)}">{">".join(path)}={text}\r'.encode()

    def _value(self, name: str, path: tuple[str, ...]) -> str | None:
        if name == _d.MATRIX_LABEL.lower():
            return self._matrix_value(path)
        for c in range(1, CH + 1):
            if name == self._in_labels[c].lower():
                if path == ("2",):
                    return _onoff(self._mute[(_d.INPUT_SLOT, c)])
                if path == ("3",):
                    return _onoff(self._source[c])
                return None
            if name == self._out_labels[c].lower():
                if path == ("1",):
                    return _d.format_number(_d.hex_level_to_db(self._out_level[c]))
                if path == ("2",):
                    return _onoff(self._mute[(_d.OUTPUT_SLOT, c)])
                if path == ("3",):
                    return _onoff(self._polarity[c])
                return None
        if self._spec(name, path) is None:
            return None
        return self._params.get((name, path))

    def _spec(self, name: str, path: tuple[str, ...]) -> tuple | None:
        for c in range(1, CH + 1):
            if name == _d.peq_label(c).lower() and len(path) == 2 and path[0] in [str(b) for b in range(1, 10)]:
                return _PEQ.get(path[1])
            if name == _d.speaker_peq_label(c).lower() and len(path) == 2 and path[0] in [str(b) for b in range(1, 10)]:
                return _SPEQ.get(path[1])
            if name == _d.band_pass_label(c).lower() and len(path) == 2 and path[0] == "1":
                return _BP.get(path[1])
            if name == _d.limiter_label(c).lower() and len(path) == 2 and path[0] == "0":
                return _LIM.get(path[1])
            if name == _d.delay_label(c).lower() and len(path) == 2 and path[0] == "1":
                if path[1] == "1":
                    return ("int", 0, _d.DELAY_MAX_SAMPLES)
                if path[1] == "3":
                    return ("onoff",)
                return None
        return None

    def _known_label(self, name: str) -> bool:
        fixed = {_d.MATRIX_LABEL.lower()}
        for c in range(1, CH + 1):
            fixed |= {_d.peq_label(c).lower(), _d.speaker_peq_label(c).lower(),
                      _d.band_pass_label(c).lower(), _d.limiter_label(c).lower(),
                      _d.delay_label(c).lower(), self._in_labels[c].lower(),
                      self._out_labels[c].lower()}
        return name in fixed

    def _display(self, label: str) -> str:
        name = label.lower()
        for c in range(1, CH + 1):
            if name == self._in_labels[c].lower():
                return self._in_labels[c]
            if name == self._out_labels[c].lower():
                return self._out_labels[c]
        for c in range(1, CH + 1):
            for fixed in (_d.peq_label(c), _d.speaker_peq_label(c), _d.band_pass_label(c),
                          _d.limiter_label(c), _d.delay_label(c)):
                if name == fixed.lower():
                    return fixed
        return _d.MATRIX_LABEL if name == _d.MATRIX_LABEL.lower() else label

    def _ga_text(self, name: str, path: tuple[str, ...]) -> str:
        return _d.ga_line(self._display(name), ">".join(path))

    def _set_input(self, c: int, path: tuple[str, ...], value: str) -> bytes:
        if path == ("3",):
            new = _toggle(value, self._source[c])
            if new is None:
                return _nak("03")
            if new != self._source[c]:
                self._source[c] = new
                self._changed(self._ga_text(self._in_labels[c].lower(), path), own=True)
            return bytes([ACK])
        if path == ("2",):
            new = _toggle(value, self._mute[(_d.INPUT_SLOT, c)])
            if new is None:
                return _nak("03")
            self._mute[(_d.INPUT_SLOT, c)] = new
            self._mirror_mutes((_d.INPUT_SLOT, c))
            self._changed(f"GM {_d.INPUT_SLOT},{c:x}", own=True)
            return bytes([ACK])
        # Index 1, Analog Sensitivity, is PowerMatch only (6.2.1).
        return _nak("02")

    def _set_output(self, c: int, path: tuple[str, ...], value: str) -> bytes:
        if path == ("1",):
            try:
                db = float(value)
            except ValueError:
                return _nak("03")
            if not _d.LEVEL_MIN <= db <= _d.LEVEL_MAX:
                return _nak("03")
            self._out_level[c] = int(_d.db_to_hex_level(db), 16)
            self._mirror_output_1(c)
            self._changed(f"GV {_d.OUTPUT_SLOT},{c:x}", own=True)
            return bytes([ACK])
        if path in (("2",), ("3",)):
            store = self._mute if path == ("2",) else None
            current = self._mute[(_d.OUTPUT_SLOT, c)] if store is not None else self._polarity[c]
            new = _toggle(value, current)
            if new is None:
                return _nak("03")
            if path == ("2",):
                self._mute[(_d.OUTPUT_SLOT, c)] = new
                self._mirror_mutes((_d.OUTPUT_SLOT, c))
                self._changed(f"GM {_d.OUTPUT_SLOT},{c:x}", own=True)
            elif new != self._polarity[c]:
                self._polarity[c] = new
                self._changed(self._ga_text(self._out_labels[c].lower(), path), own=True)
            return bytes([ACK])
        return _nak("02")

    def _set_matrix(self, path: tuple[str, ...], value: str) -> bytes:
        if len(path) != 2 or not path[1].isdigit():
            return _nak("02")
        kind, n = path[0], int(path[1])
        if kind in ("1", "2"):
            xp = _d.crosspoint_of(n)
            if xp is None:
                return _nak("02")
            if kind == "1":
                new = _toggle(value, self._xp_on[xp])
                if new is None:
                    return _nak("03")
                changed = new != self._xp_on[xp]
                self._xp_on[xp] = new
                if xp == (1, 1):
                    super().set_state("input_1_to_output_1", new)
            else:
                try:
                    db = float(value)
                except ValueError:
                    return _nak("03")
                if not _d.LEVEL_MIN <= db <= _d.LEVEL_MAX:
                    return _nak("03")
                changed = db != self._xp_level[xp]
                self._xp_level[xp] = db
        elif kind in ("3", "4"):
            if not 1 <= n <= CH:
                return _nak("02")
            store = self._mx_in_mute if kind == "3" else self._mx_out_mute
            new = _toggle(value, store[n])
            if new is None:
                return _nak("03")
            changed = new != store[n]
            store[n] = new
        else:
            return _nak("02")
        if changed:
            self._changed(_d.ga_line(_d.MATRIX_LABEL, ">".join(path)), own=True)
        return bytes([ACK])

    def _matrix_value(self, path: tuple[str, ...]) -> str | None:
        if len(path) != 2 or not path[1].isdigit():
            return None
        kind, n = path[0], int(path[1])
        if kind in ("1", "2"):
            xp = _d.crosspoint_of(n)
            if xp is None:
                return None
            return _onoff(self._xp_on[xp]) if kind == "1" else _d.format_number(self._xp_level[xp])
        if kind in ("3", "4") and 1 <= n <= CH:
            return _onoff((self._mx_in_mute if kind == "3" else self._mx_out_mute)[n])
        return None

    # ── System / device replies ──

    def _gg(self, n: int) -> bytes:
        return f"GG {n:x},{self._group_level[n]:x}\r".encode() if n in self._groups else b""

    def _gn(self, n: int) -> bytes:
        return f"GN {n:x},{'M' if self._group_mute[n] else 'U'}\r".encode() if n in self._groups else b""

    def _gv(self, slot: str, c: int) -> bytes:
        if slot != _d.OUTPUT_SLOT or not 1 <= c <= CH:
            return b""
        return f"GV {slot},{c:x},{self._out_level[c]:x}\r".encode()

    def _gm(self, slot: str, c: int) -> bytes:
        if (slot, c) not in self._mute:
            return b""
        return f"GM {slot},{c:x},{'M' if self._mute[(slot, c)] else 'U'}\r".encode()

    # ── Subscriptions and pushes ──

    def _subscribe(self, verb: str, get_text: str) -> bytes:
        if not self._push_ok:
            return b""
        value = self._answer(get_text)
        if value is None:
            return f'{verb} "{get_text}",no\r'.encode()
        if verb == "SUB":
            self._subs.add(_norm(get_text))
            super().set_state("subscriptions", len(self._subs))
            return f'SUB "{get_text}",yes\r'.encode() + value
        self._subs.discard(_norm(get_text))
        super().set_state("subscriptions", len(self._subs))
        return f'UNS "{get_text}",yes\r'.encode()

    def _answer(self, get_text: str) -> bytes | None:
        """The current value a GET produces, or None for a GET section 9 does
        not subscribe (GY among them)."""
        text = get_text.strip()
        m = _GA_RE.match(text)
        if m:
            reply = self._get_module(m.group(1), _split_path(m.group(2)))
            return reply if reply and reply[0] != NAK else None
        if text.upper() == "GS":
            return f"S {self._parameter_set:x}\r".encode()
        m = re.match(rf"^GG\s*{_HEX}$", text)
        if m:
            return self._gg(int(m.group(1), 16)) or None
        m = re.match(rf"^GN\s*{_HEX}$", text)
        if m:
            return self._gn(int(m.group(1), 16)) or None
        m = re.match(rf"^GV\s*{_HEX},{_HEX}$", text)
        if m:
            return self._gv(m.group(1).lower(), int(m.group(2), 16)) or None
        m = re.match(rf"^GM\s*{_HEX},{_HEX}$", text)
        if m:
            return self._gm(m.group(1).lower(), int(m.group(2), 16)) or None
        return None

    def _changed(self, get_text: str, own: bool) -> None:
        """Push the GET's current value to subscribers. ``own`` marks a change
        made by serial command, which the document says is not notified."""
        if own and not self._echo_own:
            return
        if _norm(get_text) not in self._subs:
            return
        value = self._answer(get_text)
        if not value:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._push_later(value))

    async def _push_later(self, data: bytes) -> None:
        await asyncio.sleep(0)  # let the reply to the command go out first
        await self.push(data)

    # ── Simulator UI bridge ──

    def _mirror_output_1(self, c: int) -> None:
        if c == 1:
            super().set_state("output_1_level_db", _d.hex_level_to_db(self._out_level[1]))

    def _mirror_mutes(self, key: tuple[str, int]) -> None:
        if key == (_d.OUTPUT_SLOT, 1):
            super().set_state("output_1_mute", self._mute[key])
        elif key == (_d.INPUT_SLOT, 1):
            super().set_state("input_1_mute", self._mute[key])

    def set_state(self, key: str, value: Any) -> None:
        if key == "output_1_level_db":
            self.set_output_level(1, float(value))
            return
        if key == "output_1_mute":
            super().set_state(key, bool(value))
            self.set_mute(_d.OUTPUT_SLOT, 1, bool(value))
            return
        if key == "input_1_mute":
            super().set_state(key, bool(value))
            self.set_mute(_d.INPUT_SLOT, 1, bool(value))
            return
        if key == "input_1_to_output_1":
            super().set_state(key, bool(value))
            self.set_crosspoint(1, 1, bool(value))
            return
        if key == "power":
            self.set_standby(not bool(value))
            return
        if key == "raise_alarm":
            if value:
                self.add_alarm("10:21:40, 11/04/2026, Output 3 Limiting")
            super().set_state(key, False)
            return
        if key == "push_supported":
            self._push_ok = bool(value)
        super().set_state(key, value)

    async def on_client_connected(self, client_id: str) -> bytes | None:
        return None

    async def stop(self) -> None:
        if self._standby_timer is not None:
            self._standby_timer.cancel()
            self._standby_timer = None
        await super().stop()


def _onoff(value: bool) -> str:
    return "O" if value else "F"


def _toggle(value: str, current: bool) -> bool | None:
    u = value.strip().upper()
    if u == "O":
        return True
    if u == "F":
        return False
    if u == "T":
        return not current
    return None


def _check(spec: tuple, value: str, current: str | None) -> str:
    """The wire text the amplifier would store for a module write, or
    ValueError (NAK 03)."""
    kind = spec[0]
    if kind == "onoff":
        new = _toggle(value, (current or "F") == "O")
        if new is None:
            raise ValueError(value)
        return _onoff(new)
    if kind == "choice":
        for choice in spec[1]:
            if value.lower() == choice.lower():
                return choice
        raise ValueError(value)
    v = float(value)
    if not spec[1] <= v <= spec[2]:
        raise ValueError(value)
    if kind == "int":
        return str(int(v))
    return _d.format_number(v)


def _nak(code: str) -> bytes:
    return bytes([NAK]) + code.encode()


def _norm(get_text: str) -> str:
    return re.sub(r"\s+", " ", get_text.strip()).lower()


def _split_path(text: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in text.split(">") if p.strip())


def _split_commands(line: str) -> list[str]:
    """Several module commands on one line are separated by semicolons
    (section 3), which may also appear inside a quoted label."""
    out: list[str] = []
    cur: list[str] = []
    in_quote = False
    for ch in line:
        if ch == '"':
            in_quote = not in_quote
        if ch == ";" and not in_quote:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out
