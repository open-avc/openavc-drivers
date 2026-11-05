"""
Bose Professional PowerMatch PM8500N / PM8250N / PM4500N / PM4250N — Serial
Control Protocol simulator.

One networked PowerMatch amplifier on TCP 10055, per the ControlSpace Serial
Control Protocol v5.15: CR-terminated ASCII; system and device commands (SS /
GS, SG / GG / SH, SN / GN, SV / GV / SI, SM / GM, GL, IP, NP, RESET, SY / GY,
GC, SF / GF, CF, SR / GR, GH / CH) that answer only when they are queries, in
hexadecimal; module commands (SA / GA) on the amplifier's modules (Matrix 1,
SigGen 1, PEQ-5band A-H, SpeakerPEQ n, Band Pass n, Limiter n, Delay n and
the renamable Input modules), answered with a bare ACK (0x06) or ``NAK nn``;
several module commands on one line separated by semicolons; and SUB / UNS,
which send a value at once and again on every change.

What the document says, modelled on purpose:

- Eight channels, or four (sim config ``channels``, the driver's own
  setting): inputs A-D on slot 1 and E-H on slot 3, outputs 1-4 on slot 2
  and 5-8 on slot 4, channels 1-4 on each (Table 2). A four-channel unit has
  slots 1 and 2 only. GL 1 and GL 2 answer every input and every output.
  Volume is on the outputs only; a Set Volume or Increment / Decrement on a
  muted output is ignored (5.1, 5.2).
- GC answers each channel's output configuration (sim config
  ``output_modes``, default the document's own example, ``IN,IN,BL,BL,Q7,
  Q7,Q7,Q7``; ``IN,IN,BL,BL`` on four channels).
- Entering or leaving standby is not immediate (5.8): GY reports the new
  state ``standby_transition_s`` after the command (sim config, default 1 s).
- A change made by serial command is not notified back to the connection
  that made it (section 6, "Automatic notification"); changes from anywhere
  else (the Simulator UI, the test hooks) go to every subscriber.
  ``echo_own_changes: true`` (sim config) notifies the writer too.
- The matrix answers the cross-points (Input # - 1) x 8 + Output # for every
  input and the signal generator (input 9) into every output, NAK 02 for any
  other index; it has no input or output mutes (6.2.4: "Only PowerShare and
  PowerShareX amplifiers have Input and Output mutes on the matrix"). The
  input PEQ has five bands with a slope; limiter thresholds run 0.5 to 71 V
  peak and 50 V RMS on an independent output and to 142 V on any other
  (6.2.9), NAK 03 beyond; the delay runs 0 to 144000 samples with bypass at
  2 and align delay (0 to 480) at 3.
- ``SF O`` / ``SR O`` turn on unsolicited ``GF f`` and ``GR c,s,t,x``
  lines (5.10, 5.12); off, nothing is sent. Limiting and an input clip are
  reported only by notification and only the first time until ``CF``
  clears the alarms; a limit or clip still going on is then reported again.
  ``CF`` also resets the fault output, which a fault still present sets
  again at once (5.11).
- A network change takes effect at RESET (5.6); RESET also returns the
  parameter set to 0 and drops the subscriptions.

Not in the document, so chosen here and listed for the bench: the Input
modules' labels (sim config ``input_a_label`` .. ``input_h_label``, default
"In A" .. "In H" as the examples write them); that the Input module's mute
is the slot mute; the array EQ's label (sim config ``array_eq_label``:
``peq``, the default, answers it as band 6 of "PEQ-5band X" as 6.2.6's prose
says; ``array`` as "Array EQ X" as its examples write); whether a cleared
limit or clip is reported (``GR c,W,L,C``; the sim reports it when the
Simulator UI's toggle goes off); and the alarm log's lines.

Driver side: ``audio/bose_powermatch.py``; its codec, tables and fixed labels
are imported from it so the two cannot drift.
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
    name = "openavc_sim_bose_powermatch_driver"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("bose_powermatch.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_d = _load_driver_module()

ACK, NAK = _d.ACK, _d.NAK
SIGGEN = _d.SIGGEN_INPUT
_HEX = r"([0-9a-fA-F]+)"
_SA_RE = re.compile(r'^SA\s*"([^"]*)"((?:>[^>=]*)+)=(.*)$')
_GA_RE = re.compile(r'^GA\s*"([^"]*)"((?:>[^>=]*)+)$')
_SUB_RE = re.compile(r'^(SUB|UNS)\s*"(.*)"$')

DEFAULT_MODES = {8: "IN,IN,BL,BL,Q7,Q7,Q7,Q7", 4: "IN,IN,BL,BL"}
DEFAULT_ALARM_LOG = [
    "10:02:41, 11/05/2026, Ch 3 Limiting",
    "10:02:44, 11/05/2026, Ch 3 Limiting Cleared",
]


def _num(lo: float | None, hi: float | None, kind: str = "num") -> tuple:
    return (kind, lo, hi)


_ONOFF = ("onoff",)
_PEQ = {"1": _num(20, 20000), "2": _num(0.1, 10.0), "3": _num(-20.0, 20.0),
        "4": ("choice", ["0", "-6", "-12"]), "5": ("choice", _d.INPUT_PEQ_TYPES), "6": _ONOFF}
_ARRAY = {"1": _num(100, 4000), "2": _num(0.0, 10.0), "3": _num(-12.0, 2.0), "4": ("array",),
          "6": _ONOFF}
_SPEQ = {"1": _num(200, 20000), "2": _num(0.1, 10.0), "3": _num(-20.0, 20.0),
         "5": ("choice", _d.SPEAKER_PEQ_TYPES), "6": _ONOFF}
_SPEQ0 = {"3": _num(-15.0, 15.0), "4": _ONOFF}
_FILTERS = [t for t, _ in _d.FILTER_TYPES]
_BP = {"1": ("choice", _FILTERS), "2": _num(20, 20000), "3": ("choice", _FILTERS),
       "4": _num(20, 20000), "8": _ONOFF, "9": _ONOFF}
_DELAY = {"1": _num(0, _d.DELAY_MAX_SAMPLES, "int"), "2": _ONOFF, "3": _num(0, _d.ALIGN_MAX_SAMPLES, "int")}
_SIGGEN = {"1": _num(-60.5, 12.0), "2": _ONOFF, "3": _ONOFF, "4": _num(1, 4, "int"),
           "5": _num(20, 20000, "int"), "6": _num(20, 20000, "int"), "7": _num(20, 20000, "int"),
           "8": _num(1, 5, "int")}
_INPUT = {"1": ("choice", ["0", "4", "12", "24"]), "2": _ONOFF, "3": _ONOFF}


class BosePowerMatchSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "bose_powermatch",
        "name": "Bose PowerMatch Amplifier Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 10055,
        "delimiter": "\r",
        "initial_state": {
            "channels": 8,
            "power": True,
            "push_supported": True,
            "subscriptions": 0,
            "alarm_reports": False,
            "parameter_set": 0,
            "alarm_count": 2,
            "output_1_level_db": 0.0,
            "output_1_mute": False,
            "input_a_mute": False,
            "input_a_to_output_1": True,
            "fault": False,
            "output_1_open_load": False,
            "output_1_limiting": False,
            "input_a_clip": False,
        },
        "controls": [
            {"type": "indicator", "key": "channels", "label": "Channels"},
            {"type": "toggle", "key": "power", "label": "Power (off = standby)"},
            {"type": "toggle", "key": "push_supported", "label": "Answers SUB (subscriptions)"},
            {"type": "indicator", "key": "subscriptions", "label": "Subscriptions"},
            {"type": "indicator", "key": "alarm_reports", "label": "Alarm reports on (SR O)"},
            {"type": "indicator", "key": "parameter_set", "label": "Last Parameter Set"},
            {"type": "indicator", "key": "alarm_count", "label": "Alarm log entries"},
            # Changes made elsewhere in the system: subscribers hear about each.
            {"type": "slider", "key": "output_1_level_db", "label": "Output 1 Level (dB)",
             "min": -60.5, "max": 0, "step": 0.5},
            {"type": "toggle", "key": "output_1_mute", "label": "Output 1 Mute"},
            {"type": "toggle", "key": "input_a_mute", "label": "Input A Mute"},
            {"type": "toggle", "key": "input_a_to_output_1", "label": "Matrix: Input A to Output 1"},
            {"type": "toggle", "key": "fault", "label": "Fault output"},
            {"type": "toggle", "key": "output_1_open_load", "label": "Output 1 open load (fault)"},
            {"type": "toggle", "key": "output_1_limiting", "label": "Output 1 limiting"},
            {"type": "toggle", "key": "input_a_clip", "label": "Input A clip"},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        self._n = _d.configured_channels(cfg)
        super().set_state("channels", self._n)
        modes = str(cfg.get("output_modes") or DEFAULT_MODES[self._n]).upper().replace(" ", "")
        codes = [c for c in modes.split(",") if c]
        if len(codes) != self._n or any(c not in _d.OUTPUT_MODES for c in codes):
            codes = DEFAULT_MODES[self._n].split(",")
        self._modes = codes
        self._in_labels = {n: (str(cfg.get(f"input_{_d.input_letter(n).lower()}_label") or "").strip()
                               or f"In {_d.input_letter(n)}")
                           for n in range(1, self._n + 1)}
        self._array_style = 1 if str(cfg.get("array_eq_label") or "").strip().lower() == "array" else 0
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
        n = self._n
        self._out_level = {c: 0x78 for c in range(1, n + 1)}
        self._out_mute = {c: False for c in range(1, n + 1)}
        self._in_mute = {c: False for c in range(1, n + 1)}
        inputs = list(range(1, n + 1)) + [SIGGEN]
        self._xp_on = {(i, o): i == o for i in inputs for o in range(1, n + 1)}
        self._xp_level = {(i, o): 0.0 for i in inputs for o in range(1, n + 1)}
        # Stored module parameters: (label lowered, index path) -> wire text.
        self._params: dict[tuple[str, tuple[str, ...]], str] = {}
        for c in range(1, n + 1):
            peq = _d.peq_label(c).lower()
            for b in range(1, _d.INPUT_PEQ_BANDS + 1):
                for k, v in {"1": "1000", "2": "1", "3": "0", "4": "0", "5": "B", "6": "F"}.items():
                    self._params[(peq, (str(b), k))] = v
            for k, v in {"1": "500", "2": "0", "3": "0", "4": "4.080", "6": "O"}.items():
                self._params[(self._array_label(c).lower(), (_d.ARRAY_EQ_BAND, k))] = v
            for b in range(1, _d.SPEAKER_PEQ_BANDS + 1):
                for k, v in {"1": "1000", "2": "1", "3": "0", "5": "B", "6": "F"}.items():
                    self._params[(_d.speaker_peq_label(c).lower(), (str(b), k))] = v
            self._params[(_d.speaker_peq_label(c).lower(), ("0", "3"))] = "0"
            self._params[(_d.speaker_peq_label(c).lower(), ("0", "4"))] = "F"
            for k, v in {"1": "But24", "2": "20", "3": "But24", "4": "20000", "8": "O", "9": "O"}.items():
                self._params[(_d.band_pass_label(c).lower(), ("1", k))] = v
            mono = self._modes[c - 1] == "IN"
            peak = _d.MONO_PEAK_MAX_V if mono else _d.BRIDGED_MAX_V
            rms = _d.MONO_RMS_MAX_V if mono else _d.BRIDGED_MAX_V
            for k, v in {"1": peak, "2": rms, "3": 1000, "4": 1000, "5": 0}.items():
                self._params[(_d.limiter_label(c).lower(), ("0", k))] = _d.format_number(v)
            for k, v in {"1": "0", "2": "F", "3": "0"}.items():
                self._params[(_d.delay_label(c).lower(), ("1", k))] = v
            label = self._in_labels[c].lower()
            self._params[(label, ("1",))] = "4"
            self._params[(label, ("3",))] = "F"
        for k, v in {"1": "-20", "2": "F", "3": "O", "4": "1", "5": "1000", "6": "20", "7": "20000",
                     "8": "3"}.items():
            self._params[(_d.SIGGEN_LABEL.lower(), (_d.SIGGEN_INDEX, k))] = v
        self._meters = {_d.GL_INPUTS: [0x50] * n, _d.GL_OUTPUTS: [0x40] * n}
        self._network = {"ip": str(cfg.get("ip_address") or "192.168.0.172"), "mask": "255.255.255.0",
                         "gateway": "192.168.0.1", "addressing": "S"}
        self._pending_network: dict[str, str] = {}
        log = cfg.get("alarm_log")
        self._alarms: list[str] = [str(x) for x in log] if isinstance(log, list) else list(DEFAULT_ALARM_LOG)
        super().set_state("alarm_count", len(self._alarms))
        # 5.10 / 5.12: unsolicited fault and alarm lines, off at power-up.
        self._sf_on = False
        self._sr_on = False
        # The fault output, and whether its cause is still present.
        self._fault = False
        self._fault_condition = False
        # A channel's standing alarm (severity, type), as GR c answers it.
        self._standing = {c: ("N", "N") for c in range(1, n + 1)}
        # Limiting (outputs) and input clips going on, and whether each has
        # been reported since the last CF.
        self._limiting = {c: False for c in range(1, n + 1)}
        self._clipping = {c: False for c in range(1, n + 1)}
        self._limit_reported = {c: False for c in range(1, n + 1)}
        self._clip_reported = {c: False for c in range(1, n + 1)}
        self._subs: set[str] = set()
        self._push_ok = bool(cfg.get("push_supported", True))
        self._echo_own = bool(cfg.get("echo_own_changes", False))
        super().set_state("push_supported", self._push_ok)

    def _array_label(self, c: int) -> str:
        return _d.array_eq_labels(c)[self._array_style]

    # ── Test hooks ──

    @property
    def channels(self) -> int:
        return self._n

    @property
    def standby(self) -> bool:
        return self._standby

    @property
    def subscription_count(self) -> int:
        return len(self._subs)

    @property
    def alarm_reports(self) -> tuple[bool, bool]:
        return self._sf_on, self._sr_on

    @property
    def alarms(self) -> list[str]:
        return list(self._alarms)

    @property
    def network(self) -> dict[str, str]:
        return dict(self._network)

    def is_subscribed(self, get_text: str) -> bool:
        return _norm(get_text) in self._subs

    def output_level_raw(self, c: int) -> int:
        return self._out_level[c]

    def output_muted(self, c: int) -> bool:
        return self._out_mute[c]

    def input_muted(self, c: int) -> bool:
        return self._in_mute[c]

    def crosspoint(self, i: int, o: int) -> tuple[bool, float]:
        return self._xp_on[(i, o)], self._xp_level[(i, o)]

    def param(self, label: str, path: str) -> str | None:
        return self._params.get((label.lower(), tuple(path.split(">"))))

    @property
    def parameter_set(self) -> int:
        return self._parameter_set

    def set_output_level(self, c: int, db: float) -> None:
        """A level moved elsewhere in the system; subscribers hear it."""
        self._out_level[c] = int(_d.db_to_hex_level(db), 16)
        slot, ch = _d.output_slot(c)
        self._changed(f"GV {slot},{ch:x}", own=False)
        if c == 1:
            super().set_state("output_1_level_db", _d.hex_level_to_db(self._out_level[c]))

    def set_output_mute(self, c: int, muted: bool) -> None:
        self._out_mute[c] = muted
        slot, ch = _d.output_slot(c)
        self._changed(f"GM {slot},{ch:x}", own=False)
        if c == 1:
            super().set_state("output_1_mute", muted)

    def set_input_mute(self, c: int, muted: bool) -> None:
        self._in_mute[c] = muted
        slot, ch = _d.input_slot(c)
        self._changed(f"GM {slot},{ch:x}", own=False)
        self._changed(_d.ga_line(self._in_labels[c], "2"), own=False)
        if c == 1:
            super().set_state("input_a_mute", muted)

    def set_crosspoint(self, i: int, o: int, on: bool) -> None:
        self._xp_on[(i, o)] = on
        self._changed(_d.ga_line(_d.MATRIX_LABEL, f"1>{_d.crosspoint_index(i, o)}"), own=False)
        if (i, o) == (1, 1):
            super().set_state("input_a_to_output_1", on)

    def set_param(self, label: str, path: str, value: str) -> None:
        """A module parameter changed elsewhere (Designer, a zone control)."""
        key = (label.lower(), tuple(path.split(">")))
        self._params[key] = value
        self._changed(_d.ga_line(self._display(key[0]), path), own=False)

    def set_parameter_set(self, n: int) -> None:
        self._parameter_set = n
        super().set_state("parameter_set", n)
        self._changed("GS", own=False)

    def set_standby(self, standby: bool) -> None:
        """Auto-standby, or a parameter set carrying the Standby property."""
        self._standby = standby
        super().set_state("power", not standby)

    def set_fault(self, fault: bool) -> None:
        """The cause of the fault output appears or goes."""
        self._fault_condition = fault
        self._set_fault_output(fault)

    def _set_fault_output(self, fault: bool) -> None:
        changed = fault != self._fault
        self._fault = fault
        super().set_state("fault", fault)
        if changed and self._sf_on:
            self._push_text(f"GF {'F' if fault else 'C'}")

    def set_standing_alarm(self, c: int, severity: str, kind: str) -> None:
        """A load or protection alarm appears on (or, severity N, leaves) a
        channel; with SR O it is reported."""
        before = self._standing[c]
        self._standing[c] = (severity, kind)
        if not self._sr_on:
            return
        if severity == "N":
            if before != ("N", "N"):
                self._push_text(f"GR {c},{before[0]},{before[1]},C")
        else:
            self._push_text(f"GR {c},{severity},{kind},S")

    def report_limiting(self, c: int, on: bool = True) -> None:
        self._limiting[c] = on
        self._one_shot(c, "L", on, self._limit_reported)

    def report_clip(self, c: int, on: bool = True) -> None:
        self._clipping[c] = on
        self._one_shot(c, "C", on, self._clip_reported)

    def report_system(self, kind: str, on: bool = True) -> None:
        """A non-channel alarm (A = AC mains loss, D = digital audio loss)."""
        if self._sr_on:
            self._push_text(f"GR 0,S,{kind},{'S' if on else 'C'}")

    def _one_shot(self, c: int, kind: str, on: bool, latch: dict[int, bool]) -> None:
        """5.12: "Only the first instance of limiting or clipping for a given
        channel is reported"."""
        if on:
            if self._sr_on and not latch[c]:
                latch[c] = True
                self._push_text(f"GR {c},W,{kind},S")
            return
        if latch[c]:
            latch[c] = False
            if self._sr_on:
                self._push_text(f"GR {c},W,{kind},C")

    def _clear_alarms(self) -> None:
        """5.11 CF: active alarms cleared and the fault output reset; what is
        still present is set (and, with SR O, reported) again."""
        self._limit_reported = {c: False for c in self._limit_reported}
        self._clip_reported = {c: False for c in self._clip_reported}
        self._set_fault_output(False)
        if self._fault_condition:
            self._set_fault_output(True)
        for c in range(1, self._n + 1):
            if self._limiting[c]:
                self._one_shot(c, "L", True, self._limit_reported)
            if self._clipping[c]:
                self._one_shot(c, "C", True, self._clip_reported)

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

    def _slot(self, slot: str, c: int) -> tuple[str, int] | None:
        """A slot and channel as ("input" or "output", n) on this unit."""
        where = _d.channel_of_slot(slot, c)
        if where is None or where[1] > self._n:
            return None
        return where

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
        # Output configuration (5.9).
        if upper == "GC":
            return f"GC {','.join(self._modes)}\r".encode()
        # Fault and alarm reporting (5.10-5.12).
        m = re.match(r"^S([FR])\s*([OFof])$", cmd)
        if m:
            on = m.group(2).upper() == "O"
            if m.group(1) == "F":
                self._sf_on = on
            else:
                self._sr_on = on
                super().set_state("alarm_reports", on)
            return b""
        if upper == "GF":
            return f"GF {'F' if self._fault else 'C'}\r".encode()
        if upper == "CF":
            self._clear_alarms()
            return bytes([ACK]) + b"\r"
        m = re.match(rf"^GR\s*{_HEX}$", cmd)
        if m:
            c = int(m.group(1), 16)
            if c not in self._standing:
                return b""
            s, t = self._standing[c]
            return f"GR {c:x},{s},{t}\r".encode()
        # Parameter sets (4.1): 1-FFh.
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
        # Groups (4.2-4.4): 1-40h, amplifier levels stop at 78h.
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
        # Slot volume and mute (5.1-5.3): volume on the output slots only.
        m = re.match(rf"^SV\s*{_HEX},{_HEX},{_HEX}$", cmd)
        if m:
            slot = m.group(1).lower()
            where = self._slot(slot, int(m.group(2), 16))
            if where and where[0] == "output" and not self._out_mute[where[1]]:
                v = int(m.group(3), 16)
                self._out_level[where[1]] = v if v == 0xFF else max(0, min(_d.HEX_LEVEL_MAX, v))
                self._mirror_output_1(where[1])
                self._changed(f"GV {slot},{int(m.group(2), 16):x}", own=True)
            return b""
        m = re.match(rf"^GV\s*{_HEX},{_HEX}$", cmd)
        if m:
            return self._gv(m.group(1).lower(), int(m.group(2), 16))
        m = re.match(rf"^SI\s*{_HEX},{_HEX},([01]),{_HEX}$", cmd)
        if m:
            slot = m.group(1).lower()
            where = self._slot(slot, int(m.group(2), 16))
            if where and where[0] == "output" and not self._out_mute[where[1]]:
                c = where[1]
                cur = self._out_level[c]
                cur = 0 if cur == 0xFF else cur
                delta = int(m.group(4), 16) * (1 if m.group(3) == "1" else -1)
                self._out_level[c] = max(0, min(_d.HEX_LEVEL_MAX, cur + delta))
                self._mirror_output_1(c)
                self._changed(f"GV {slot},{int(m.group(2), 16):x}", own=True)
            return b""
        m = re.match(rf"^SM\s*{_HEX},{_HEX},([MUTmut])$", cmd)
        if m:
            slot, ch = m.group(1).lower(), int(m.group(2), 16)
            where = self._slot(slot, ch)
            if where:
                store = self._out_mute if where[0] == "output" else self._in_mute
                c = where[1]
                store[c] = {"M": True, "U": False}.get(m.group(3).upper(), not store[c])
                if c == 1:
                    super().set_state("output_1_mute" if where[0] == "output" else "input_a_mute", store[c])
                self._changed(f"GM {slot},{ch:x}", own=True)
                if where[0] == "input":
                    self._changed(_d.ga_line(self._in_labels[c], "2"), own=True)
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
            self._sf_on = self._sr_on = False
            super().set_state("alarm_reports", False)
            return b""
        if upper[:2] in ("SA", "GA", "MA"):
            return _nak("99")
        # Anything else: silence.
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

    def _input_of_label(self, name: str) -> int | None:
        for c, label in self._in_labels.items():
            if label.lower() == name:
                return c
        return None

    def _set_module(self, label: str, path: tuple[str, ...], raw: str) -> bytes:
        name = label.lower()
        value = raw.strip().strip('"')
        if name == _d.MATRIX_LABEL.lower():
            return self._set_matrix(path, value)
        spec = self._spec(name, path)
        if spec is None:
            return _nak("01") if not self._known_label(name) else _nak("02")
        c = self._input_of_label(name)
        if c is not None and path == ("2",):
            new = _toggle(value, self._in_mute[c])
            if new is None:
                return _nak("03")
            if new != self._in_mute[c]:
                self._in_mute[c] = new
                slot, ch = _d.input_slot(c)
                self._changed(f"GM {slot},{ch:x}", own=True)
                self._changed(self._ga_text(name, path), own=True)
            return bytes([ACK])
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
        return f'GA "{self._display(label.lower())}">{">".join(path)}={text}\r'.encode()

    def _value(self, name: str, path: tuple[str, ...]) -> str | None:
        if name == _d.MATRIX_LABEL.lower():
            return self._matrix_value(path)
        if self._spec(name, path) is None:
            return None
        c = self._input_of_label(name)
        if c is not None and path == ("2",):
            return _onoff(self._in_mute[c])
        return self._params.get((name, path))

    def _spec(self, name: str, path: tuple[str, ...]) -> tuple | None:
        c = self._input_of_label(name)
        if c is not None:
            return _INPUT.get(path[0]) if len(path) == 1 else None
        if len(path) != 2:
            return None
        i1, i2 = path
        if name == _d.SIGGEN_LABEL.lower():
            return _SIGGEN.get(i2) if i1 == _d.SIGGEN_INDEX else None
        for c in range(1, self._n + 1):
            if name == _d.peq_label(c).lower() or name == self._array_label(c).lower():
                if i1 == _d.ARRAY_EQ_BAND and name == self._array_label(c).lower():
                    return _ARRAY.get(i2)
                if name == _d.peq_label(c).lower():
                    return _PEQ.get(i2) if i1 in [str(b) for b in range(1, _d.INPUT_PEQ_BANDS + 1)] else None
                return None
            if name == _d.speaker_peq_label(c).lower():
                if i1 == "0":
                    return _SPEQ0.get(i2)
                return _SPEQ.get(i2) if i1 in [str(b) for b in range(1, _d.SPEAKER_PEQ_BANDS + 1)] else None
            if name == _d.band_pass_label(c).lower():
                return _BP.get(i2) if i1 == "1" else None
            if name == _d.limiter_label(c).lower():
                return self._limiter_spec(c, i2) if i1 == "0" else None
            if name == _d.delay_label(c).lower():
                return _DELAY.get(i2) if i1 == "1" else None
        return None

    def _limiter_spec(self, c: int, index2: str) -> tuple | None:
        mono = self._modes[c - 1] == "IN"
        peak = _d.MONO_PEAK_MAX_V if mono else _d.BRIDGED_MAX_V
        rms = _d.MONO_RMS_MAX_V if mono else _d.BRIDGED_MAX_V
        return {"1": _num(0.5, peak), "2": _num(0.5, rms), "3": _num(500, 10000, "int"),
                "4": _num(500, 10000, "int"), "5": _num(0, 4, "int")}.get(index2)

    def _fixed_labels(self) -> list[str]:
        labels = [_d.MATRIX_LABEL, _d.SIGGEN_LABEL]
        for c in range(1, self._n + 1):
            labels += [_d.peq_label(c), self._array_label(c), _d.speaker_peq_label(c),
                       _d.band_pass_label(c), _d.limiter_label(c), _d.delay_label(c),
                       self._in_labels[c]]
        return labels

    def _known_label(self, name: str) -> bool:
        return name in {label.lower() for label in self._fixed_labels()}

    def _display(self, name: str) -> str:
        for label in self._fixed_labels():
            if label.lower() == name:
                return label
        return name

    def _ga_text(self, name: str, path: tuple[str, ...]) -> str:
        return _d.ga_line(self._display(name), ">".join(path))

    def _set_matrix(self, path: tuple[str, ...], value: str) -> bytes:
        if len(path) != 2 or not path[1].isdigit() or path[0] not in ("1", "2"):
            return _nak("02")
        xp = _d.crosspoint_of(int(path[1]))
        if xp is None or xp not in self._xp_on:
            return _nak("02")
        if path[0] == "1":
            new = _toggle(value, self._xp_on[xp])
            if new is None:
                return _nak("03")
            changed = new != self._xp_on[xp]
            self._xp_on[xp] = new
            if xp == (1, 1):
                super().set_state("input_a_to_output_1", new)
        else:
            try:
                db = float(value)
            except ValueError:
                return _nak("03")
            if not _d.LEVEL_MIN <= db <= _d.LEVEL_MAX:
                return _nak("03")
            changed = db != self._xp_level[xp]
            self._xp_level[xp] = db
        if changed:
            self._changed(_d.ga_line(_d.MATRIX_LABEL, ">".join(path)), own=True)
        return bytes([ACK])

    def _matrix_value(self, path: tuple[str, ...]) -> str | None:
        if len(path) != 2 or not path[1].isdigit() or path[0] not in ("1", "2"):
            return None
        xp = _d.crosspoint_of(int(path[1]))
        if xp is None or xp not in self._xp_on:
            return None
        return _onoff(self._xp_on[xp]) if path[0] == "1" else _d.format_number(self._xp_level[xp])

    # ── System / device replies ──

    def _gg(self, n: int) -> bytes:
        return f"GG {n:x},{self._group_level[n]:x}\r".encode() if n in self._groups else b""

    def _gn(self, n: int) -> bytes:
        return f"GN {n:x},{'M' if self._group_mute[n] else 'U'}\r".encode() if n in self._groups else b""

    def _gv(self, slot: str, ch: int) -> bytes:
        where = self._slot(slot, ch)
        if not where or where[0] != "output":
            return b""
        return f"GV {slot},{ch:x},{self._out_level[where[1]]:x}\r".encode()

    def _gm(self, slot: str, ch: int) -> bytes:
        where = self._slot(slot, ch)
        if not where:
            return b""
        store = self._out_mute if where[0] == "output" else self._in_mute
        return f"GM {slot},{ch:x},{'M' if store[where[1]] else 'U'}\r".encode()

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
        not subscribe (GY, GC, GF and GR among them)."""
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
        if value:
            self._push_bytes(value)

    def _push_text(self, line: str) -> None:
        self._push_bytes((line + "\r").encode())

    def _push_bytes(self, data: bytes) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._push_later(data))

    async def _push_later(self, data: bytes) -> None:
        await asyncio.sleep(0)  # let the reply to the command go out first
        await self.push(data)

    # ── Simulator UI bridge ──

    def _mirror_output_1(self, c: int) -> None:
        if c == 1:
            super().set_state("output_1_level_db", _d.hex_level_to_db(self._out_level[1]))

    def set_state(self, key: str, value: Any) -> None:
        if key == "output_1_level_db":
            self.set_output_level(1, float(value))
            return
        if key == "output_1_mute":
            self.set_output_mute(1, bool(value))
            return
        if key == "input_a_mute":
            self.set_input_mute(1, bool(value))
            return
        if key == "input_a_to_output_1":
            self.set_crosspoint(1, 1, bool(value))
            return
        if key == "power":
            self.set_standby(not bool(value))
            return
        if key == "fault":
            self.set_fault(bool(value))
            return
        if key == "output_1_open_load":
            super().set_state(key, bool(value))
            if value:
                self.set_standing_alarm(1, "F", "O")
            else:
                self.set_standing_alarm(1, "N", "N")
            return
        if key == "output_1_limiting":
            super().set_state(key, bool(value))
            self.report_limiting(1, bool(value))
            return
        if key == "input_a_clip":
            super().set_state(key, bool(value))
            self.report_clip(1, bool(value))
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
    if kind == "array":
        try:
            return _d.encode_array(value)
        except Exception as exc:
            raise ValueError(value) from exc
    v = float(value)
    lo, hi = spec[1], spec[2]
    if lo is not None and hi is not None and not lo <= v <= hi:
        raise ValueError(value)
    if kind == "int":
        if v != int(v):
            raise ValueError(value)
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
