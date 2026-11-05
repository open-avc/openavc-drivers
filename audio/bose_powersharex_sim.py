"""
Bose Professional PowerShareX PSX1204D / PSX2404D / PSX4804D — Serial Control
Protocol simulator.

One PowerShareX amplifier as its PSXProxy port presents it, per the
ControlSpace Serial Control Protocol v5.15: CR-terminated ASCII; system and
device commands (SS / GS, SG / GG / SH, SN / GN, SV / GV / SI, SM / GM, GL,
IP, SY / GY, SF / GF, SR / GR) that answer only when they are queries, in
hexadecimal; module commands (SA / GA) on the amplifier's fixed modules
(Matrix 1, PEQ-8band n, SpeakerPEQ n, Band Pass n, Array EQ n, Limiter n,
Delay n, SigGen 1, Backup Strategy 1, the Alarm/Fault Panel), answered with
a bare ACK (0x06) or ``NAK nn``; several module commands on one line
separated by semicolons; and SUB / UNS, which send a value at once and again
on every change.

What the document says, modelled on purpose:

- Volume and mute are on slot 2 (the outputs) only (5.1-5.3); a Set Volume
  or Increment / Decrement on a muted output is ignored.
- Entering or leaving standby is not immediate (5.8): GY reports the new
  state ``standby_transition_s`` after the command (sim config, default 1 s).
- A change made by serial command is not notified back to the connection
  that made it (section 6, "Automatic notification"); changes from anywhere
  else (the Simulator UI, the test hooks) go to every subscriber.
  ``echo_own_changes: true`` (sim config) notifies the writer too.
- The matrix answers the cross-points (Input # - 1) x 4 + Output # (6.2.4),
  NAK 02 for any other index. The input PEQ has eight bands and no slope
  (NAK 02 on Index 2 = 4). Limiter thresholds run 10 V to the model's and
  output mode's maximum (Appendix B; sim config ``model`` and
  ``output_mode``), NAK 03 above it.
- ``SF O`` / ``SR O`` turn on unsolicited ``GF f`` and ``GR c,s,t,x``
  lines (5.10, 5.12); off, nothing is sent. Limiting and an input clip are
  reported only by notification and only the first time until cleared.
- Commands a PowerShareX does not support (RESET, the IP set, GH / CH, CF,
  GC, NP) draw silence. The Input module (6.2.1) is not there.

Not in the document, so chosen here and listed for the bench: whether a
cleared limit or clip is reported (``GR c,W,L,C``; the sim reports it when
the Simulator UI's toggle goes off and re-arms the one-shot); what the
Alarm/Fault Panel is called (sim config ``alarm_panel_label``, default the
prose's "Alarm/Fault Panel"; the examples write "Alarm / Fault Panel"); the
array EQ's tilt and gain ranges (the PowerShareX table gives none: the sim
takes any number); and that ``IP`` answers the amplifier's own address.

Driver side: ``audio/bose_powersharex.py``; its codec, tables and fixed
labels are imported from it so the two cannot drift.
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
    name = "openavc_sim_bose_powersharex_driver"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("bose_powersharex.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_d = _load_driver_module()

ACK, NAK = _d.ACK, _d.NAK
CH = _d.CHANNELS
OUT = _d.OUTPUT_SLOT
_HEX = r"([0-9a-fA-F]+)"
_SA_RE = re.compile(r'^SA\s*"([^"]*)"((?:>[^>=]*)+)=(.*)$')
_GA_RE = re.compile(r'^GA\s*"([^"]*)"((?:>[^>=]*)+)$')
_SUB_RE = re.compile(r'^(SUB|UNS)\s*"(.*)"$')

# Appendix B, PowerShareX Limiter Settings: (peak, RMS, clip) maximum in V.
LIMITER_MAX = {
    "PSX1204D": {"mono_lo_z": (70, 50, 80), "mono_70v": (100, 71, 100), "mono_100v": (142, 100, 142),
                 "bridge_lo_z": (140, 100, 160), "bridge_70v": (100, 71, 100),
                 "bridge_100v": (142, 100, 142)},
    "PSX2404D": {"mono_lo_z": (100, 70, 100), "mono_70v": (100, 71, 100), "mono_100v": (142, 100, 142),
                 "bridge_lo_z": (200, 140, 200), "bridge_70v": (100, 71, 100),
                 "bridge_100v": (142, 100, 142)},
    "PSX4804D": {"mono_lo_z": (140, 100, 140), "mono_70v": (100, 71, 100), "mono_100v": (142, 100, 142),
                 "bridge_lo_z": (280, 200, 280), "bridge_70v": (100, 71, 100),
                 "bridge_100v": (142, 100, 142)},
}


def _num(lo: float | None, hi: float | None, kind: str = "num") -> tuple:
    return (kind, lo, hi)


_ONOFF = ("onoff",)
_PEQ = {"1": _num(20, 20000), "2": _num(0.1, 10.0), "3": _num(-15.0, 15.0),
        "5": ("choice", _d.INPUT_PEQ_TYPES), "6": _ONOFF}
_SPEQ = {"1": _num(200, 20000), "2": _num(0.1, 10.0), "3": _num(-15.0, 15.0),
         "5": ("choice", _d.SPEAKER_PEQ_TYPES), "6": _ONOFF}
_SPEQ0 = {"3": _num(-15.0, 15.0), "4": _ONOFF}
_FILTERS = [t for t, _ in _d.FILTER_TYPES]
_BP = {"1": ("choice", _FILTERS), "2": _num(20, 20000), "3": ("choice", _FILTERS),
       "4": _num(20, 20000), "8": _ONOFF, "9": _ONOFF}
_ARRAY = {"1": _num(100, 4000), "2": _num(None, None), "3": _num(None, None), "5": _ONOFF}
_DELAY = {"1": _num(0, _d.DELAY_MAX_SAMPLES, "int"), "2": _ONOFF, "3": _num(0, _d.ALIGN_MAX_SAMPLES, "int")}
_SIGGEN = {"1": _num(-60.5, 12.0), "2": _ONOFF, "3": _ONOFF, "4": _ONOFF, "5": _ONOFF, "6": _ONOFF}
_BACKUP = {"1": ("choice", ["F", "S", "P"]), "2": _num(0, 8, "int"), "3": _num(-50.0, 15.0),
           "4": _num(0, 8, "int"), "5": _num(-50.0, 15.0), "6": _num(0, 8, "int"),
           "7": _num(-50.0, 15.0), "8": _num(0, 8, "int"), "9": _num(-50.0, 15.0),
           "10": _num(20, 22000), "11": _num(-50.0, 14.0), "12": _num(-50.0, 14.0), "13": _ONOFF}
_PANEL = {"1": _ONOFF, "2": _num(20, 22000), "3": _num(0.1, 40.0), "4": _ONOFF,
          "5": _num(20, 22000), "6": _num(0.1, 40.0), "7": _num(0.1, 40.0), "10": _ONOFF,
          "11": _num(20, 22000), "12": _num(1.0, 500.0), "13": _num(1.0, 500.0), "16": _ONOFF,
          "17": _num(1.0, 500.0), "18": _num(1.0, 500.0)}


class BosePowerShareXSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "bose_powersharex",
        "name": "Bose PowerShareX Amplifier Simulator",
        "category": "audio",
        "transport": "tcp",
        # The port is the one ControlSpace Designer gives the amplifier on its
        # PSXProxy; the document has no default.
        "default_port": 0,
        "delimiter": "\r",
        "initial_state": {
            "model": "PSX2404D",
            "power": True,
            "push_supported": True,
            "subscriptions": 0,
            "alarm_reports": False,
            "parameter_set": 0,
            "output_1_level_db": 0.0,
            "output_1_mute": False,
            "input_1_to_output_1": True,
            "fault": False,
            "output_1_open_load": False,
            "output_1_limiting": False,
            "input_1_clip": False,
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "toggle", "key": "power", "label": "Power (off = standby)"},
            {"type": "toggle", "key": "push_supported", "label": "Answers SUB (subscriptions)"},
            {"type": "indicator", "key": "subscriptions", "label": "Subscriptions"},
            {"type": "indicator", "key": "alarm_reports", "label": "Alarm reports on (SR O)"},
            {"type": "indicator", "key": "parameter_set", "label": "Last Parameter Set"},
            # Changes made elsewhere in the system: subscribers hear about each.
            {"type": "slider", "key": "output_1_level_db", "label": "Output 1 Level (dB)",
             "min": -60.5, "max": 0, "step": 0.5},
            {"type": "toggle", "key": "output_1_mute", "label": "Output 1 Mute"},
            {"type": "toggle", "key": "input_1_to_output_1", "label": "Matrix: Input 1 to Output 1"},
            {"type": "toggle", "key": "fault", "label": "Fault output"},
            {"type": "toggle", "key": "output_1_open_load", "label": "Output 1 open load (fault)"},
            {"type": "toggle", "key": "output_1_limiting", "label": "Output 1 limiting"},
            {"type": "toggle", "key": "input_1_clip", "label": "Input 1 clip"},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        model = str(cfg.get("model") or "PSX2404D").strip().upper()
        self._model = model if model in LIMITER_MAX else "PSX2404D"
        mode = str(cfg.get("output_mode") or "mono_lo_z").strip().lower()
        self._limits = LIMITER_MAX[self._model].get(mode, LIMITER_MAX[self._model]["mono_lo_z"])
        super().set_state("model", self._model)
        self._panel_label = str(cfg.get("alarm_panel_label") or _d.ALARM_PANEL_LABELS[0])
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
        self._mute = {c: False for c in range(1, CH + 1)}
        self._xp_on = {(i, o): i == o for i in range(1, CH + 1) for o in range(1, CH + 1)}
        self._xp_level = {(i, o): 0.0 for i in range(1, CH + 1) for o in range(1, CH + 1)}
        self._mx_in_mute = {c: False for c in range(1, CH + 1)}
        self._mx_out_mute = {c: False for c in range(1, CH + 1)}
        # Stored module parameters: (label lowered, index path) -> wire text.
        self._params: dict[tuple[str, tuple[str, ...]], str] = {}
        peak, rms, clip = self._limits
        for c in range(1, CH + 1):
            for b in range(1, _d.INPUT_PEQ_BANDS + 1):
                for k, v in {"1": "1000", "2": "1", "3": "0", "5": "B", "6": "F"}.items():
                    self._params[(_d.peq_label(c).lower(), (str(b), k))] = v
            for b in range(1, _d.SPEAKER_PEQ_BANDS + 1):
                for k, v in {"1": "1000", "2": "1", "3": "0", "5": "B", "6": "F"}.items():
                    self._params[(_d.speaker_peq_label(c).lower(), (str(b), k))] = v
            self._params[(_d.speaker_peq_label(c).lower(), ("0", "3"))] = "0"
            self._params[(_d.speaker_peq_label(c).lower(), ("0", "4"))] = "F"
            for k, v in {"1": "But24", "2": "20", "3": "But24", "4": "20000", "8": "O", "9": "O"}.items():
                self._params[(_d.band_pass_label(c).lower(), ("1", k))] = v
            for k, v in {"1": "500", "2": "0", "3": "0", "5": "O"}.items():
                self._params[(_d.array_eq_label(c).lower(), ("1", k))] = v
            for k, v in {"1": peak, "2": rms, "3": 1000, "4": 1000, "9": 1, "10": 100, "13": clip,
                         "14": "O", "15": "O"}.items():
                self._params[(_d.limiter_label(c).lower(), ("0", k))] = (
                    v if isinstance(v, str) else _d.format_number(v))
            for k, v in {"1": "0", "2": "F", "3": "0"}.items():
                self._params[(_d.delay_label(c).lower(), ("1", k))] = v
            # Backup strategy: Force, analog n first, Dante n behind it.
            for k, v in {"1": "F", "2": str(c), "3": "-40", "4": str(c + 4), "5": "-40", "6": "0",
                         "7": "-40", "8": "0", "9": "-40", "10": "20000", "11": "-10", "12": "-30",
                         "13": "F"}.items():
                self._params[(_d.BACKUP_LABEL.lower(), (str(c), k))] = v
            for k, v in {"1": "F", "2": "20000", "3": "1", "4": "F", "5": "20000", "6": "2", "7": "0.5",
                         "10": "F", "11": "20000", "12": "16", "13": "4", "16": "F", "17": "16",
                         "18": "4"}.items():
                self._params[(self._panel_label.lower(), (str(c), k))] = v
        for k, v in {"1": "-20", "2": "F", "3": "F", "4": "F", "5": "F", "6": "F"}.items():
            self._params[(_d.SIGGEN_LABEL.lower(), (_d.SIGGEN_INDEX, k))] = v
        self._meters = {_d.GL_ANALOG: [0x50] * CH, _d.GL_OUTPUTS: [0x40] * CH, _d.GL_DANTE: [0x48] * CH}
        self._ip = str(cfg.get("ip_address") or "192.168.0.171")
        # 5.10 / 5.12: unsolicited fault and alarm lines, off at power-up.
        self._sf_on = False
        self._sr_on = False
        self._fault = False
        # A channel's standing alarm (severity, type), as GR c answers it.
        self._standing = {c: ("N", "N") for c in range(1, CH + 1)}
        # One-shot reports of limiting (outputs) and input clip.
        self._limit_reported = {c: False for c in range(1, CH + 1)}
        self._clip_reported = {c: False for c in range(1, CH + 1)}
        self._subs: set[str] = set()
        self._push_ok = bool(cfg.get("push_supported", True))
        self._echo_own = bool(cfg.get("echo_own_changes", False))
        super().set_state("push_supported", self._push_ok)

    # ── Test hooks ──

    @property
    def standby(self) -> bool:
        return self._standby

    @property
    def subscription_count(self) -> int:
        return len(self._subs)

    @property
    def alarm_reports(self) -> tuple[bool, bool]:
        return self._sf_on, self._sr_on

    def is_subscribed(self, get_text: str) -> bool:
        return _norm(get_text) in self._subs

    def output_level_raw(self, c: int) -> int:
        return self._out_level[c]

    def muted(self, c: int) -> bool:
        return self._mute[c]

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
        self._changed(f"GV {OUT},{c:x}", own=False)
        if c == 1:
            super().set_state("output_1_level_db", _d.hex_level_to_db(self._out_level[c]))

    def set_mute(self, c: int, muted: bool) -> None:
        self._mute[c] = muted
        self._changed(f"GM {OUT},{c:x}", own=False)
        if c == 1:
            super().set_state("output_1_mute", muted)

    def set_crosspoint(self, i: int, o: int, on: bool) -> None:
        self._xp_on[(i, o)] = on
        self._changed(_d.ga_line(_d.MATRIX_LABEL, f"1>{_d.crosspoint_index(i, o)}"), own=False)

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
        self._fault = fault
        super().set_state("fault", fault)
        if self._sf_on:
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
        self._one_shot(c, "L", on, self._limit_reported)

    def report_clip(self, c: int, on: bool = True) -> None:
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
        # Fault and alarm reporting (5.10, 5.12).
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
        m = re.match(rf"^GR\s*{_HEX}$", cmd)
        if m:
            c = int(m.group(1), 16)
            if c not in self._standing:
                return b""
            s, t = self._standing[c]
            return f"GR {c:x},{s},{t}\r".encode()
        # Parameter sets (4.1): 1-40h on a PowerShareX.
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
        # Groups (4.2-4.4): 1-3Fh, amplifier levels stop at 78h.
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
        # Slot volume and mute (5.1-5.3): slot 2, the outputs, only.
        m = re.match(rf"^SV\s*{_HEX},{_HEX},{_HEX}$", cmd)
        if m:
            slot, c = m.group(1).lower(), int(m.group(2), 16)
            if slot == OUT and 1 <= c <= CH and not self._mute[c]:
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
            if slot == OUT and 1 <= c <= CH and not self._mute[c]:
                cur = self._out_level[c]
                cur = 0 if cur == 0xFF else cur
                delta = int(m.group(4), 16) * (1 if m.group(3) == "1" else -1)
                self._out_level[c] = max(0, min(_d.HEX_LEVEL_MAX, cur + delta))
                self._mirror_output_1(c)
                self._changed(f"GV {slot},{c:x}", own=True)
            return b""
        m = re.match(rf"^SM\s*{_HEX},{_HEX},([MUTmut])$", cmd)
        if m:
            slot, c = m.group(1).lower(), int(m.group(2), 16)
            if slot == OUT and 1 <= c <= CH:
                s = m.group(3).upper()
                self._mute[c] = {"M": True, "U": False}.get(s, not self._mute[c])
                if c == 1:
                    super().set_state("output_1_mute", self._mute[c])
                self._changed(f"GM {slot},{c:x}", own=True)
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
        # 5.5: the IP query only; "Set not supported on PowerShareX".
        if upper == "IP":
            return f"IP {self._ip}\r".encode()
        if upper[:2] in ("SA", "GA", "MA"):
            return _nak("99")
        # RESET, IP set, GH / CH, CF, GC, NP and anything else: silence.
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
        return f'GA "{self._display(label.lower())}">{">".join(path)}={text}\r'.encode()

    def _value(self, name: str, path: tuple[str, ...]) -> str | None:
        if name == _d.MATRIX_LABEL.lower():
            return self._matrix_value(path)
        if self._spec(name, path) is None:
            return None
        return self._params.get((name, path))

    def _spec(self, name: str, path: tuple[str, ...]) -> tuple | None:
        if len(path) != 2:
            return None
        i1, i2 = path
        channels = [str(c) for c in range(1, CH + 1)]
        if name == _d.SIGGEN_LABEL.lower():
            return _SIGGEN.get(i2) if i1 == _d.SIGGEN_INDEX else None
        if name == _d.BACKUP_LABEL.lower():
            return _BACKUP.get(i2) if i1 in channels else None
        if name == self._panel_label.lower():
            return _PANEL.get(i2) if i1 in channels else None
        for c in range(1, CH + 1):
            if name == _d.peq_label(c).lower():
                return _PEQ.get(i2) if i1 in [str(b) for b in range(1, _d.INPUT_PEQ_BANDS + 1)] else None
            if name == _d.speaker_peq_label(c).lower():
                if i1 == "0":
                    return _SPEQ0.get(i2)
                return _SPEQ.get(i2) if i1 in [str(b) for b in range(1, _d.SPEAKER_PEQ_BANDS + 1)] else None
            if name == _d.band_pass_label(c).lower():
                return _BP.get(i2) if i1 == "1" else None
            if name == _d.array_eq_label(c).lower():
                return _ARRAY.get(i2) if i1 == "1" else None
            if name == _d.limiter_label(c).lower():
                return self._limiter_spec(i2) if i1 == "0" else None
            if name == _d.delay_label(c).lower():
                return _DELAY.get(i2) if i1 == "1" else None
        return None

    def _limiter_spec(self, index2: str) -> tuple | None:
        peak, rms, clip = self._limits
        return {"1": _num(10, peak), "2": _num(10, rms), "3": _num(500, 10000), "4": _num(500, 10000),
                "9": _num(0.3, 2000.0), "10": _num(3.0, 8000.0), "13": _num(10, clip),
                "14": _ONOFF, "15": _ONOFF}.get(index2)

    def _fixed_labels(self) -> list[str]:
        labels = [_d.MATRIX_LABEL, _d.SIGGEN_LABEL, _d.BACKUP_LABEL, self._panel_label]
        for c in range(1, CH + 1):
            labels += [_d.peq_label(c), _d.speaker_peq_label(c), _d.band_pass_label(c),
                       _d.array_eq_label(c), _d.limiter_label(c), _d.delay_label(c)]
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
        if slot != OUT or not 1 <= c <= CH:
            return b""
        return f"GV {slot},{c:x},{self._out_level[c]:x}\r".encode()

    def _gm(self, slot: str, c: int) -> bytes:
        if slot != OUT or not 1 <= c <= CH:
            return b""
        return f"GM {slot},{c:x},{'M' if self._mute[c] else 'U'}\r".encode()

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
        not subscribe (GY, GF and GR among them)."""
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
            self.set_mute(1, bool(value))
            return
        if key == "input_1_to_output_1":
            super().set_state(key, bool(value))
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
        if key == "input_1_clip":
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
