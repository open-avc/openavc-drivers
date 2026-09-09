"""
Bose Professional ControlSpace — Serial Control Protocol driver for the
ESP, EX and CSP processors.

Controls a ControlSpace EX-1280C / EX-12AEC / EX-440C / EX-1280, an
ESP-880 / 1240 / 4120 / 1600, an ESP-00 (Series II, ESP-00, ESP-88) or a
CSP-428 / CSP-1248 through the ControlSpace Serial Control Protocol on TCP
port 10055 (serial-over-Ethernet). The processor's RS-232 port carries the
same bytes (38,400 baud on an ESP-00, 115,200 on the others), so the driver
offers both transports.

Why Python (rules.md Principle 9):
    The control surface is the integrator's design, not the driver's: every
    signal processing module placed in ControlSpace Designer is addressed by
    the label it was given there, and the parameters it carries depend on
    the module type (a Gain has two, a Matrix Mixer has inputs x outputs x 2
    plus a mute per channel). So the driver takes a declared module table
    and builds one child entity per row with a schema chosen by the row's
    type — dynamic per-child schemas, which YAML rosters cannot express. On
    top of that: a subscribe handshake with a capability check and a polled
    fallback, ACK / NAK bytes that are not line-terminated and carry an
    error code the user should see, hexadecimal levels on the system and
    device commands beside decimal dB on the module commands, and the Get
    Signal Level array that fans one reply into a channel of meters each.

Push, with a read-back after every write (Principle 2):
    ``SUB "<GET command>"`` makes the processor send the value at once and
    again whenever it changes (section 9), so a subscribe doubles as the
    read. On connect the driver asks ``SUB`` (no argument); a processor that
    answers ``SUB yes`` gets every declared control, group and the
    parameter-set query subscribed, and the poll cycle (default 60 s,
    "Resync interval") renews them, which is both the resync and the re-arm
    after a reboot. A processor that does not answer is polled instead, one
    GET per control each cycle. Because the document says a change made by
    serial command is not itself notified (section 6, "Automatic
    notification"), the driver follows every Set with the matching Get, as
    the document itself recommends, so a panel always shows what the
    processor accepted.

Liveness:
    A subscribed session can sit silent for hours. The watchdog sends ``GS``
    and awaits its ``S n`` reply; two misses drop the link with a typed
    ``no_response`` fault.

Acknowledgements:
    Module commands answer with a bare ACK (0x06) or ``NAK nn``; system and
    device commands answer nothing at all. The frame parser delivers an ACK
    or NAK as its own frame whether or not a carriage return follows it,
    and a NAK's two-digit code becomes the error the user sees ("Invalid
    module name" points at a label that does not match the design).

Not this driver: the PowerMatch / PowerShare amplifiers (same grammar, plus
standby, fault and alarm commands and their own module tables), the MSA12X
loudspeaker and the WP / EP / EX Dante endpoints (a different ASCII grammar
over UDP 49494). Each is its own roadmap row.

Source (Bose Professional, manufacturer documents):
    ControlSpace Serial Control Protocol v5.13, revision date June 5, 2024.
    https://assets.boseprofessional.com/m/4998082f60dfee56/original/ControlSpace-Serial-Protocol-v5-13.pdf
    CSP Processors Serial Control Protocol Guide v1.0 (CSP-428 / CSP-1248).
    https://assets.boseprofessional.com/m/48b4f11e8a4922b9/original/ug_csp_control_serial.pdf
"""


import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import CallableFrameParser
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ── Wire constants (sections 2 and 3) ───────────────────────────────────────

ACK = 0x06
NAK = 0x15
DEFAULT_PORT = 10055
DEFAULT_POLL_INTERVAL = 60
DEFAULT_METER_INTERVAL_S = 1.0
MIN_METER_INTERVAL_S = 0.2
ACK_TIMEOUT_S = 2.0
QUERY_TIMEOUT_S = 2.0
SUB_TIMEOUT_S = 2.0

NAK_CODES = {
    "01": "Invalid module name: no module in the loaded design has that label, "
          "or two modules share it",
    "02": "Illegal index: the index or number of indices is wrong for that module",
    "03": "Value out of range for that parameter",
    "99": "Unknown error",
}

# Levels on the module commands (section 6): -60.5 to +12.0 dB in 0.5 dB steps.
LEVEL_MIN = -60.5
LEVEL_MAX = 12.0
# Levels on the system / device commands (sections 4 and 5): 0h = -60 dB to
# 90h = +12 dB in 0.5 dB steps; FFh = -60.5 dB / off.
HEX_LEVEL_MAX = 0x90
HEX_LEVEL_OFF = 0xFF
GROUP_MAX = 0x40
PARAMETER_SET_MAX = 0xFF
ROOM_COMBINE_MAX = 6
ROOM_MAX = 6


# ── Frame parser ────────────────────────────────────────────────────────────

_NAK_RE = re.compile(rb"\x15 ?(\d{0,2})")


def parse_controlspace_stream(buf: bytes) -> tuple[bytes | None, bytes]:
    """CallableFrameParser function: one CR (or LF) terminated line per
    message, with a bare ACK byte and a ``NAK nn`` delivered as their own
    frames whether or not a line ending follows them (section 3 shows both
    forms). The returned buffer is what the parser keeps."""
    if not buf:
        return None, buf
    first = buf[0]
    if first in (0x0D, 0x0A):
        return b"", buf[1:]
    if first == ACK:
        return buf[:1], buf[1:]
    if first == NAK:
        m = _NAK_RE.match(buf)
        end = m.end()
        digits = m.group(1)
        if end < len(buf):
            if buf[end] in (0x0D, 0x0A):
                return buf[:end], buf[end + 1:]
            return buf[:end], buf[end:]
        if len(digits) < 2:
            return None, buf          # the code may still be on its way
        return buf[:end], buf[end:]
    for i, b in enumerate(buf):
        if b in (0x0D, 0x0A):
            return buf[:i], buf[i + 1:]
        if b == ACK and i > 0:
            # An acknowledgement riding on an unterminated line: the line
            # ends here and the ACK is the next frame.
            return buf[:i], buf[i:]
    return None, buf


# ── Value formats ───────────────────────────────────────────────────────────

FMT_LEVEL = "level"      # (-)NN.N dB
FMT_ONOFF = "onoff"      # O / F, written as O / F / T
FMT_LOGIC = "logic"      # O / F, written as O / F / T / P
FMT_INT = "int"          # a whole number
FMT_NUMBER = "number"    # a decimal number
FMT_ENUM = "enum"        # one of a list of tokens
FMT_STRING = "string"    # read-only text
FMT_ROUTING = "routing"  # Standard Mixer routing mask (8 hex digits), read-only
FMT_METER = "meter"      # a Get Signal Level channel, read-only

VALUE_FORMATS = (FMT_LEVEL, FMT_ONOFF, FMT_LOGIC, FMT_INT, FMT_NUMBER, FMT_STRING)
FORMAT_ALIASES = {
    "db": FMT_LEVEL, "gain": FMT_LEVEL, "volume": FMT_LEVEL,
    "bool": FMT_ONOFF, "boolean": FMT_ONOFF, "mute": FMT_ONOFF, "switch": FMT_ONOFF,
    "pulse": FMT_LOGIC,
    "integer": FMT_INT,
    "float": FMT_NUMBER, "decimal": FMT_NUMBER,
    "str": FMT_STRING, "text": FMT_STRING,
}


def format_number(value: float) -> str:
    """A number as the protocol writes it: no units, no trailing ``.0``."""
    v = float(value)
    if v == int(v):
        return str(int(v))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def parse_onoff(text: str) -> bool | None:
    t = text.strip().strip('"').upper()
    if t == "O":
        return True
    if t == "F":
        return False
    return None


def coerce_onoff(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "on", "yes", "y", "o", "mute", "muted"):
        return True
    if s in ("0", "false", "off", "no", "n", "f", "unmute", "unmuted"):
        return False
    raise ValueError(f"{value!r} is not an on/off value")


def hex_level_to_db(raw: int) -> float:
    """System / device command level: 0h = -60 dB ... 90h = +12 dB in 0.5 dB
    steps; FFh = -60.5 dB (off)."""
    if raw >= HEX_LEVEL_OFF:
        return LEVEL_MIN
    return round(raw / 2.0 - 60.0, 1)


def db_to_hex_level(db: float) -> str:
    db = float(db)
    if db <= LEVEL_MIN:
        return format(HEX_LEVEL_OFF, "x")
    raw = int(round((min(db, LEVEL_MAX) + 60.0) * 2))
    return format(max(0, min(HEX_LEVEL_MAX, raw)), "x")


def meter_to_db(raw: int, floor_db: float = -60.0) -> float:
    """Get Signal Level value: 0.5 dB steps up from the slot's floor (-60 dBFS
    for inputs and digital outputs, -35 dBu for a fixed-I/O analog output,
    -36 dBu on an ESP-00 output; section 5.4)."""
    return round(floor_db + raw / 2.0, 1)


def routing_mask_to_outputs(mask_hex: str, outputs: int) -> dict[int, bool]:
    """Standard Mixer routing A: eight hex digits, the first digit's most
    significant bit is output 1 (section 6.1.40's worked example)."""
    value = int(mask_hex.strip(), 16)
    return {o: bool(value & (1 << (32 - o))) for o in range(1, outputs + 1)}


def outputs_to_routing_mask(outputs_on: dict[int, bool]) -> str:
    value = 0
    for o, on in outputs_on.items():
        if on and 1 <= o <= 32:
            value |= 1 << (32 - o)
    return format(value, "08X")


# ── Control definitions ─────────────────────────────────────────────────────

FILTER_TYPES = [
    ("But6", "Butterworth 6 dB/oct"), ("But12", "Butterworth 12 dB/oct"),
    ("But18", "Butterworth 18 dB/oct"), ("But24", "Butterworth 24 dB/oct"),
    ("But36", "Butterworth 36 dB/oct"), ("But48", "Butterworth 48 dB/oct"),
    ("Bes12", "Bessel 12 dB/oct"), ("Bes18", "Bessel 18 dB/oct"),
    ("Bes24", "Bessel 24 dB/oct"), ("Bes36", "Bessel 36 dB/oct"),
    ("Bes48", "Bessel 48 dB/oct"), ("Lin12", "Linkwitz-Riley 12 dB/oct"),
    ("Lin24", "Linkwitz-Riley 24 dB/oct"), ("Lin36", "Linkwitz-Riley 36 dB/oct"),
    ("Lin48", "Linkwitz-Riley 48 dB/oct"),
]
PEQ_TYPES = [("B", "Band"), ("HS", "High Shelf"), ("LS", "Low Shelf"),
             ("HC", "High Cut (Low Pass)"), ("LC", "Low Cut (High Pass)"), ("N", "Notch")]
SPEAKER_PEQ_TYPES = [("B", "Band"), ("HS", "High Shelf"), ("LS", "Low Shelf"), ("N", "Notch")]
DETECTOR_LRMS = [("L", "Left"), ("R", "Right"), ("M", "Mix"), ("S", "Sidechain")]
GEQ_BANDS = ["20 Hz", "25 Hz", "31.5 Hz", "40 Hz", "50 Hz", "63 Hz", "80 Hz", "100 Hz",
             "125 Hz", "160 Hz", "200 Hz", "250 Hz", "315 Hz", "400 Hz", "500 Hz", "630 Hz",
             "800 Hz", "1 kHz", "1.25 kHz", "1.6 kHz", "2 kHz", "2.5 kHz", "3.15 kHz", "4 kHz",
             "5 kHz", "6.3 kHz", "8 kHz", "10 kHz", "12.5 kHz", "16 kHz", "20 kHz"]


@dataclass
class ControlDef:
    prop: str
    label: str
    idx: tuple[str, ...]
    fmt: str
    writable: bool = True
    subscribe: bool = True
    write_idx: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str | None = None
    values: list[tuple[str, str]] = field(default_factory=list)
    fanout: int = 0         # FMT_ROUTING: outputs the mask covers
    help: str = ""

    @property
    def read_path(self) -> str:
        return ">".join(self.idx)

    @property
    def write_path(self) -> str:
        return ">".join(self.write_idx or self.idx)

    def schema(self) -> dict[str, Any]:
        d: dict[str, Any] = {"label": self.label}
        if self.fmt == FMT_LEVEL:
            d.update({"type": "number", "unit": self.unit or "dB",
                      "min": LEVEL_MIN if self.min is None else self.min,
                      "max": LEVEL_MAX if self.max is None else self.max,
                      "step": 0.5 if self.step is None else self.step})
        elif self.fmt in (FMT_ONOFF, FMT_LOGIC):
            d["type"] = "boolean"
        elif self.fmt == FMT_INT:
            d["type"] = "integer"
            if self.min is not None:
                d["min"] = self.min
            if self.max is not None:
                d["max"] = self.max
            if self.unit:
                d["unit"] = self.unit
        elif self.fmt == FMT_NUMBER:
            d["type"] = "number"
            for k in ("min", "max", "step", "unit"):
                v = getattr(self, k)
                if v is not None:
                    d[k] = v
        elif self.fmt == FMT_ENUM:
            d["type"] = "enum"
            d["values"] = [{"value": v, "label": lbl} for v, lbl in self.values]
        elif self.fmt == FMT_METER:
            d.update({"type": "number", "unit": self.unit or "dB", "cloud_priority": "low"})
            if self.min is not None:
                d["min"] = self.min
            if self.max is not None:
                d["max"] = self.max
        else:
            d["type"] = "string"
        d["control"] = bool(self.writable)
        if self.help:
            d["help"] = self.help
        return d


def _idx(*parts: Any) -> tuple[str, ...]:
    return tuple(str(p) for p in parts)


def L(prop: str, label: str, *idx: Any, lo: float = LEVEL_MIN, hi: float = LEVEL_MAX,
      step: float = 0.5, unit: str = "dB") -> ControlDef:
    return ControlDef(prop, label, _idx(*idx), FMT_LEVEL, min=lo, max=hi, step=step, unit=unit)


def B(prop: str, label: str, *idx: Any) -> ControlDef:
    return ControlDef(prop, label, _idx(*idx), FMT_ONOFF)


def P(prop: str, label: str, *idx: Any) -> ControlDef:
    return ControlDef(prop, label, _idx(*idx), FMT_LOGIC)


def Int(prop: str, label: str, *idx: Any, lo: int | None = None, hi: int | None = None,
      unit: str | None = None) -> ControlDef:
    return ControlDef(prop, label, _idx(*idx), FMT_INT, min=lo, max=hi, unit=unit)


def N(prop: str, label: str, *idx: Any, lo: float | None = None, hi: float | None = None,
      step: float | None = None, unit: str | None = None) -> ControlDef:
    return ControlDef(prop, label, _idx(*idx), FMT_NUMBER, min=lo, max=hi, step=step, unit=unit)


def E(prop: str, label: str, *idx: Any, values: list[tuple[str, str]]) -> ControlDef:
    return ControlDef(prop, label, _idx(*idx), FMT_ENUM, values=list(values))


def S(prop: str, label: str, *idx: Any) -> ControlDef:
    return ControlDef(prop, label, _idx(*idx), FMT_STRING, writable=False)


def RO(ctl: ControlDef) -> ControlDef:
    ctl.writable = False
    return ctl


# ── Module types (section 6.1 and the CSP guide) ────────────────────────────

def build_input(size: str) -> list[ControlDef]:
    return [
        E("type", "Type", 1, values=[("M", "Mic"), ("L", "Line")]),
        E("gain", "Gain (dB)", 2, values=[(g, f"{g} dB") for g in
                                          ("0", "14", "24", "32", "42", "44", "48", "54", "64")]),
        L("level", "Level", 3),
        B("mute", "Mute", 4),
        B("phantom", "Phantom Power", 5),
    ]


def build_output(size: str) -> list[ControlDef]:
    return [L("level", "Level", 1), B("mute", "Mute", 2), B("polarity", "Polarity", 3)]


def build_gain(size: str) -> list[ControlDef]:
    return [L("level", "Level", 1), B("mute", "Mute", 2)]


def build_usb(size: str) -> list[ControlDef]:
    n = _parse_count(size, default=2, maximum=2)
    out: list[ControlDef] = []
    for c in range(1, n + 1):
        out += [L(f"ch_{c}_level", f"Channel {c} Level", c, 1),
                B(f"ch_{c}_mute", f"Channel {c} Mute", c, 2)]
    return out


def build_pstn_input(size: str) -> list[ControlDef]:
    return [
        S("call_status", "Call Status", 0, 1),
        S("caller_id", "Caller ID", 0, 2),
        L("ring_level", "Ring Level", 0, 3, lo=-30, hi=10, step=1),
        L("dtmf_level", "DTMF Level", 0, 4, lo=-20, hi=10, step=1),
        Int("auto_answer", "Auto Answer (rings, 0 = off)", 0, 6, lo=0, hi=8),
        Int("country_code", "Country Code", 0, 7, lo=0, hi=254),
        RO(B("call_active", "Call Active", 0, 8)),
        B("manual_hook", "Manual Hook", 0, 9),
        L("level", "Level", 1, 1),
        B("mute", "Mute", 1, 2),
    ]


def build_voip_input(size: str) -> list[ControlDef]:
    return [
        S("account_status", "Account Status", 0, 0),
        S("call_status", "Call Status", 0, 1),
        S("caller_id", "Caller ID", 0, 2),
        RO(B("call_active", "Call Active", 0, 6)),
        Int("auto_answer", "Auto Answer (rings, 0 = off)", 0, 7, lo=0, hi=8),
        L("level", "Level", 1, 1),
        B("mute", "Mute", 1, 2),
    ]


CALL_ACTIONS = {"dial_key": "1", "make_call": "2", "end_call": "3", "answer_call": "4",
                "transfer_call": "5"}
CALL_MODULE_TYPES = ("pstn_input", "voip_input")


def build_aec(size: str) -> list[ControlDef]:
    n = _parse_count(size, default=12, maximum=12)
    out: list[ControlDef] = []
    for c in range(1, n + 1):
        out += [
            B(f"ch_{c}_internal_mute", f"Input {c} Internal Mute", c, 5),
            B(f"ch_{c}_aec_enable", f"Input {c} AEC Enable", c, 6),
            E(f"ch_{c}_nlp", f"Input {c} NLP Control", c, 7,
              values=[("1", "Light"), ("2", "Medium"), ("3", "Strong")]),
            B(f"ch_{c}_cn_enable", f"Input {c} Comfort Noise", c, 8),
            Int(f"ch_{c}_nr_level", f"Input {c} Noise Reduction", c, 9, lo=0, hi=32, unit="dB"),
            RO(Int(f"ch_{c}_reference", f"Input {c} Reference", c, 10, lo=1, hi=4)),
        ]
    return out


def build_agc(size: str) -> list[ControlDef]:
    n = _parse_count(size, default=4, maximum=32)
    out = [N("max_total_gain", "Max Total Gain", 0, 1, lo=0, hi=60, step=1, unit="dB")]
    for i in range(1, n + 1):
        p = f"in_{i}_"
        lbl = f"Input {i} "
        out += [
            N(p + "activity_threshold", lbl + "Activity Threshold", i, 1, lo=-70, hi=0, step=1, unit="dB"),
            N(p + "target_min", lbl + "Target Level Minimum", i, 2, lo=-40, hi=24, step=1, unit="dB"),
            N(p + "target_max", lbl + "Target Level Maximum", i, 3, lo=-40, hi=24, step=1, unit="dB"),
            N(p + "cut_rate", lbl + "Cut Rate", i, 4, lo=0, hi=9000, step=0.1, unit="dB/s"),
            N(p + "cut_range", lbl + "Cut Range", i, 5, lo=0, hi=30, step=1, unit="dB"),
            N(p + "cut_hold", lbl + "Cut Hold", i, 6, lo=0, hi=60, step=1, unit="s"),
            N(p + "boost_rate", lbl + "Boost Rate", i, 7, lo=0, hi=9000, step=0.1, unit="dB/s"),
            N(p + "boost_range", lbl + "Boost Range", i, 8, lo=0, hi=30, step=1, unit="dB"),
            N(p + "boost_hold", lbl + "Boost Hold", i, 9, lo=0, hi=60, step=1, unit="s"),
            B(p + "bypass", lbl + "Bypass", i, 10),
        ]
    return out


def build_agc_legacy(size: str) -> list[ControlDef]:
    return [
        E("detector", "Detector (stereo)", 1, values=[("L", "Left"), ("R", "Right"), ("M", "Mix")]),
        N("threshold", "Threshold", 2, lo=-40, hi=0, step=0.5, unit="dBFS"),
        B("bypass", "Bypass", 6),
    ]


def build_array_eq(size: str) -> list[ControlDef]:
    return [
        Int("center_frequency", "Center Frequency", 1, 1, lo=100, hi=4000, unit="Hz"),
        N("tilt", "Tilt", 1, 2, lo=0, hi=10, step=0.1),
        N("gain", "Gain", 1, 3, lo=-12, hi=2, step=0.1, unit="dB"),
        B("bypass", "Bypass", 1, 5),
        B("advanced", "Advanced", 1, 6),
        Int("modules", "RoomMatch Modules", 1, 7, lo=2, hi=8),
        Int("vertical_angle", "Vertical Coverage Angle", 1, 8, lo=20, hi=100, unit="deg"),
    ]


def build_amm_gain_sharing(size: str) -> list[ControlDef]:
    n = _parse_count(size, default=8, maximum=32)
    out = [
        L("gain", "Output Gain", 0, 1),
        B("mute", "Output Mute", 0, 2),
        N("slope", "Slope", 0, 3, lo=0.01, hi=2.0, step=0.01),
        N("attack", "Attack", 0, 4, lo=0.5, hi=100, step=0.5, unit="ms"),
        N("hold", "Hold", 0, 5, lo=0, hi=1000, step=1, unit="ms"),
        N("decay", "Decay", 0, 6, lo=5, hi=50000, step=5, unit="ms"),
        N("input_rms_avg", "Input RMS Averaging", 0, 7, lo=1, hi=500, step=1, unit="ms"),
        N("output_rms_avg", "Output RMS Averaging", 0, 8, lo=1, hi=500, step=1, unit="ms"),
        B("bypass_all", "Bypass All", 0, 9),
    ]
    for i in range(1, n + 1):
        out += [
            L(f"in_{i}_gain", f"Input {i} Gain", i, 1),
            B(f"in_{i}_mute", f"Input {i} Mute", i, 2),
            Int(f"in_{i}_priority", f"Input {i} Priority (1 = highest)", i, 3, lo=1, hi=5),
            B(f"in_{i}_bypass", f"Input {i} Bypass", i, 4),
            Int(f"in_{i}_mute_group", f"Input {i} Mute Group (0 = none)", i, 5, lo=0, hi=31),
        ]
    return out


def build_amm_gated_legacy(size: str) -> list[ControlDef]:
    n = _parse_count(size, default=8, maximum=8)
    out = [
        L("gain", "Output Gain", 0, 1, hi=0.0),
        B("nom", "NOM", 0, 2),
        B("mute", "Output Mute", 0, 3),
        Int("nom_limit", "NOM Limit", 0, 4, lo=1, hi=8),
    ]
    for i in range(1, n + 1):
        p, lbl = f"in_{i}_", f"Input {i} "
        out += [
            B(p + "priority", lbl + "Priority", i, 1),
            L(p + "gain", lbl + "Gain", i, 2, hi=0.0),
            E(p + "detection", lbl + "Detection", i, 3,
              values=[("1", "Threshold"), ("2", "Last On"), ("3", "Push To Talk"), ("4", "Bypass")]),
            N(p + "threshold", lbl + "Threshold", i, 4, lo=-80, hi=0, step=0.5, unit="dB"),
            N(p + "gate_depth", lbl + "Gate Depth", i, 5, lo=-70, hi=0, step=0.5, unit="dB"),
            Int(p + "hold", lbl + "Hold", i, 6, lo=1, hi=50000, unit="ms"),
            N(p + "ducking_depth", lbl + "Ducking Depth", i, 7, lo=-60, hi=0, step=0.5, unit="dB"),
            Int(p + "decay", lbl + "Decay", i, 8, lo=5, hi=50000, unit="ms"),
            Int(p + "high_pass", lbl + "High Pass", i, 10, lo=20, hi=1000, unit="Hz"),
            Int(p + "low_pass", lbl + "Low Pass", i, 11, lo=1000, hi=20000, unit="Hz"),
            Int(p + "rms_avg", lbl + "RMS Averaging", i, 12, lo=1, hi=500, unit="ms"),
            N(p + "attack", lbl + "Attack", i, 14, lo=0.5, hi=100, step=0.5, unit="ms"),
            B(p + "push_to_talk", lbl + "Push To Talk", i, 15),
            B(p + "mute", lbl + "Mute", i, 16),
        ]
    return out


def build_amm_gated(size: str) -> list[ControlDef]:
    n = _parse_count(size, default=8, maximum=32)
    out = [
        L("gain", "Output Gain", 0, 1),
        B("mute", "Output Mute", 0, 2),
        B("nom_enable", "NOM Enable", 0, 3),
        Int("nom_limit", "NOM Limit", 0, 4, lo=1, hi=32),
        ControlDef("designated_mic", "Designated Mic (number, None or LMH)", _idx(0, 5), FMT_STRING,
                   writable=True),
        N("ats_attack", "ATS Attack", 0, 8, lo=100, hi=10000, step=1, unit="ms"),
        N("ats_release", "ATS Release", 0, 9, lo=10, hi=1000, step=1, unit="ms"),
        N("ats_margin", "ATS Margin", 0, 10, lo=-18, hi=18, step=1, unit="dB"),
        E("ats_source", "ATS Source", 0, 11, values=[("I", "Mic Mix"), ("A", "Ambient Input")]),
        N("ats_sensitivity", "ATS Sensitivity", 0, 12, lo=-20, hi=20, step=0.5, unit="dB"),
        N("ats_lpf", "ATS LPF Frequency", 0, 13, lo=20, hi=20000, step=0.1, unit="Hz"),
        N("ats_hpf", "ATS HPF Frequency", 0, 14, lo=20, hi=20000, step=0.1, unit="Hz"),
        E("ats_slope", "ATS Slope", 0, 15, values=[("6.0", "6 dB/oct"), ("12.0", "12 dB/oct")]),
        B("ats_filter_enable", "ATS Filter Enable", 0, 16),
        B("direct_outputs", "Direct Outputs", 0, 17),
    ]
    for i in range(1, n + 1):
        p, lbl = f"in_{i}_", f"Input {i} "
        out += [
            Int(p + "priority", lbl + "Priority (1 = highest)", i, 1, lo=1, hi=5),
            L(p + "gain", lbl + "Gain", i, 2),
            B(p + "mute", lbl + "Mute", i, 3),
            ControlDef(p + "mute_group", lbl + "Mute Group (number or None)", _idx(i, 4), FMT_STRING,
                       writable=True),
            N(p + "manual_threshold", lbl + "Manual Threshold", i, 5, lo=-80, hi=0, step=0.5, unit="dB"),
            B(p + "auto_threshold", lbl + "Auto Threshold", i, 6),
            B(p + "bypass", lbl + "Bypass", i, 7),
            E(p + "direct_output", lbl + "Direct Output", i, 8, values=[("R", "Pre"), ("S", "Post")]),
            N(p + "low_pass", lbl + "Low Pass", i, 9, lo=20, hi=20000, step=0.1, unit="Hz"),
            N(p + "high_pass", lbl + "High Pass", i, 10, lo=20, hi=20000, step=0.1, unit="Hz"),
            N(p + "rms_avg", lbl + "RMS Averaging", i, 11, lo=1, hi=1000, step=1, unit="ms"),
            N(p + "ducker_depth", lbl + "Ducker Depth", i, 12, lo=-60, hi=0, step=0.5, unit="dB"),
            N(p + "gate_depth", lbl + "Gate Depth", i, 13, lo=-70, hi=0, step=0.5, unit="dB"),
            N(p + "gate_attack", lbl + "Gate Attack", i, 14, lo=0.5, hi=500, step=0.5, unit="ms"),
            N(p + "gate_hold", lbl + "Gate Hold", i, 15, lo=1, hi=50000, step=1, unit="ms"),
            N(p + "gate_decay", lbl + "Gate Decay", i, 16, lo=1, hi=50000, step=1, unit="ms"),
            B(p + "nom_gain", lbl + "NOM Gain", i, 17),
        ]
    return out


def build_compressor(size: str) -> list[ControlDef]:
    return [
        E("detect_input", "Detect Input", 1, values=DETECTOR_LRMS),
        N("threshold", "Threshold", 2, lo=-40, hi=0, step=0.5, unit="dBFS"),
        N("ratio", "Ratio", 3, lo=1, hi=20, step=0.1),
        N("attack", "Attack", 4, lo=0.5, hi=100, step=0.5, unit="ms"),
        N("release", "Release", 5, lo=1, hi=1000, step=0.5, unit="ms"),
        B("bypass", "Bypass", 6),
    ]


def build_conference_room_router(size: str) -> list[ControlDef]:
    far_ends = _parse_count(size, default=2, maximum=8)
    out = [
        RO(Int("far_end_inputs", "Far End Inputs", 0, 1, lo=0, hi=8)),
        RO(Int("pre_aec_mic_inputs", "Pre-AEC Mic Inputs", 0, 2, lo=0, hi=32)),
        RO(Int("overhead_outputs", "Overhead Outputs", 0, 3, lo=0, hi=16)),
        RO(B("room_combine_member", "Room Combine Member", 0, 4)),
        RO(Int("rc_room_number", "Room Combine Room Number", 0, 5, lo=0, hi=6)),
        S("rc_room_name", "Room Combine Room Name", 0, 6),
        S("room_combine_state", "Room Combine State", 0, 7),
        RO(N("stereo_mono_attenuation", "Stereo to Mono Attenuation", 0, 8, unit="dB")),
        RO(E("matrix_mode", "Matrix Mode", 0, 9, values=[("N", "Normal"), ("A", "Advanced")])),
        L("master_volume", "Master Volume", 1, 1),
        B("master_mute", "Master Mute", 1, 2),
        L("mic_mix_level", "Mic Mix Level", 1, 3),
        B("mic_mix_mute", "Mic Mix Mute", 1, 4),
        L("non_mic_mix_level", "Non-Mic Mix Level", 1, 5),
        B("non_mic_mix_mute", "Non-Mic Mix Mute", 1, 6),
        L("pre_aec_mic_mix_level", "Pre-AEC Mic Mix Level", 1, 7),
        B("pre_aec_mic_mix_mute", "Pre-AEC Mic Mix Mute", 1, 8),
        Int("rc_group_number", "Room Combine Group Number", 1, 9, lo=1, hi=6),
        L("program_level", "Program Level", 2, 1),
        B("program_mute", "Program Mute", 2, 2),
    ]
    for k in range(1, far_ends + 1):
        out += [L(f"far_end_{k}_level", f"Far End {k} Level", 2, 2 * k + 1),
                B(f"far_end_{k}_mute", f"Far End {k} Mute", 2, 2 * k + 2)]
    return out


def _crossover_section(prefix: str, label: str, section: int, mid: bool) -> list[ControlDef]:
    if mid:
        return [
            E(prefix + "hpf_type", label + " HPF Type", section, 1, values=FILTER_TYPES),
            Int(prefix + "hpf_frequency", label + " HPF Frequency", section, 2, lo=20, hi=20000, unit="Hz"),
            E(prefix + "lpf_type", label + " LPF Type", section, 3, values=FILTER_TYPES),
            Int(prefix + "lpf_frequency", label + " LPF Frequency", section, 4, lo=20, hi=20000, unit="Hz"),
            B(prefix + "polarity", label + " Polarity", section, 6),
            B(prefix + "mute", label + " Mute", section, 7),
        ]
    return [
        E(prefix + "type", label + " Type", section, 1, values=FILTER_TYPES),
        Int(prefix + "frequency", label + " Frequency", section, 2, lo=20, hi=20000, unit="Hz"),
        B(prefix + "polarity", label + " Polarity", section, 4),
        B(prefix + "mute", label + " Mute", section, 5),
    ]


def build_crossover(size: str) -> list[ControlDef]:
    ways = _parse_count(size, default=2, maximum=4)
    if ways < 2:
        raise ValueError("a crossover is 2, 3 or 4 way")
    layouts = {
        2: [("low_", "Low", False), ("high_", "High", False)],
        3: [("low_", "Low", False), ("mid_", "Mid", True), ("high_", "High", False)],
        4: [("low_", "Low", False), ("lo_mid_", "Lo Mid", True), ("hi_mid_", "Hi Mid", True),
            ("high_", "High", False)],
    }
    out: list[ControlDef] = []
    for section, (prefix, label, mid) in enumerate(layouts[ways], 1):
        out += _crossover_section(prefix, label, section, mid)
    return out


def build_delay(size: str) -> list[ControlDef]:
    taps = _parse_count(size, default=1, maximum=8)
    out: list[ControlDef] = []
    for t in range(1, taps + 1):
        out += [Int(f"tap_{t}_delay", f"Tap {t} Delay (48 samples = 1 ms)", t, 1, lo=0, hi=144000,
                  unit="samples"),
                B(f"tap_{t}_bypass", f"Tap {t} Bypass", t, 2)]
    return out


def build_ducker(size: str) -> list[ControlDef]:
    return [
        N("threshold", "Threshold", 2, lo=-40, hi=0, step=0.5, unit="dBFS"),
        N("range", "Range", 3, lo=-60, hi=0, step=0.5, unit="dB"),
        N("attack", "Attack", 4, lo=0.5, hi=100, step=0.5, unit="ms"),
        Int("hold", "Hold", 5, lo=0, hi=1000, unit="ms"),
        Int("decay", "Decay", 6, lo=5, hi=50000, unit="ms"),
        B("bypass", "Bypass", 7),
        B("engage", "Engage Ducker (logic)", 8),
    ]


def build_gate(size: str) -> list[ControlDef]:
    return [
        E("detector", "Detector", 1, values=DETECTOR_LRMS),
        N("threshold", "Threshold", 2, lo=-40, hi=0, step=0.5, unit="dBFS"),
        N("range", "Range", 3, lo=-70, hi=0, step=0.5, unit="dB"),
        N("attack", "Attack", 4, lo=0.5, hi=100, step=0.5, unit="ms"),
        Int("hold", "Hold", 5, lo=0, hi=1000, unit="ms"),
        Int("decay", "Decay", 6, lo=5, hi=50000, unit="ms"),
        B("bypass", "Bypass", 7),
    ]


def build_gpo(size: str) -> list[ControlDef]:
    pins = _parse_count(size, default=8, maximum=8)
    return [B(f"pin_{p}", f"Pin {p}", p) for p in range(1, pins + 1)]


def build_graphic_eq(size: str) -> list[ControlDef]:
    out = [N(f"band_{b}", f"{GEQ_BANDS[b - 1]}", b, lo=-15, hi=15, step=0.1, unit="dB")
           for b in range(1, 32)]
    out.append(B("bypass_all", "Bypass All", 32))
    return out


def build_logic_pins(size: str) -> list[ControlDef]:
    pins = _parse_count(size, default=8, maximum=16)
    return [P(f"pin_{p}", f"Pin {p}", p, 1) for p in range(1, pins + 1)]


def build_logic_block(size: str) -> list[ControlDef]:
    ins, outs = _parse_matrix(size, default=(4, 4), maximum=16)
    out = [RO(B(f"input_{i}", f"Input {i}", 1, i)) for i in range(1, ins + 1)]
    out += [RO(B(f"output_{o}", f"Output {o}", 2, o)) for o in range(1, outs + 1)]
    return out


def build_matrix_mixer(size: str) -> list[ControlDef]:
    ins, outs = _parse_matrix(size, default=(8, 8), maximum=32)
    out: list[ControlDef] = []
    for i in range(1, ins + 1):
        for o in range(1, outs + 1):
            xp = (i - 1) * outs + o
            out += [B(f"xp_{i}_{o}", f"In {i} to Out {o}", 1, xp),
                    L(f"xp_{i}_{o}_level", f"In {i} to Out {o} Level", 2, xp, hi=0.0)]
    out += [B(f"input_{i}_mute", f"Input {i} Mute", 3, i) for i in range(1, ins + 1)]
    out += [B(f"output_{o}_mute", f"Output {o} Mute", 4, o) for o in range(1, outs + 1)]
    return out


def build_parametric_eq(size: str) -> list[ControlDef]:
    bands = _parse_count(size, default=5, maximum=16)
    out: list[ControlDef] = []
    for b in range(1, bands + 1):
        p, lbl = f"band_{b}_", f"Band {b} "
        out += [
            Int(p + "frequency", lbl + "Frequency", b, 1, lo=20, hi=20000, unit="Hz"),
            N(p + "q", lbl + "Q", b, 2, lo=0.1, hi=10, step=0.1),
            N(p + "gain", lbl + "Gain", b, 3, lo=-20, hi=20, step=0.1, unit="dB"),
            E(p + "slope", lbl + "Slope", b, 4, values=[("0", "0 dB/oct"), ("-6", "-6 dB/oct"),
                                                        ("-12", "-12 dB/oct")]),
            E(p + "type", lbl + "Type", b, 5, values=PEQ_TYPES),
            B(p + "bypass", lbl + "Bypass", b, 6),
        ]
    return out


def build_peak_rms_limiter(size: str) -> list[ControlDef]:
    return [
        E("detect_input", "Detect Input", 1, values=DETECTOR_LRMS),
        N("peak_threshold", "Peak Threshold", 2, lo=-40, hi=0, step=0.5, unit="dBFS"),
        B("bypass", "Bypass", 6),
        N("rms_threshold", "RMS Threshold", 7, lo=-40, hi=0, step=0.5, unit="dBFS"),
        Int("rms_attack", "RMS Attack", 8, lo=500, hi=10000, unit="ms"),
        Int("rms_release", "RMS Release", 9, lo=500, hi=10000, unit="ms"),
    ]


def build_router(size: str) -> list[ControlDef]:
    outs = _parse_count(size, default=8, maximum=32)
    return [Int(f"output_{o}_source", f"Output {o} Source (0 = off)", o, lo=0, hi=32)
            for o in range(1, outs + 1)]


def build_signal_generator(size: str) -> list[ControlDef]:
    kind = (size or "sine").strip().lower()
    if kind in ("sine", "tone"):
        return [Int("frequency", "Frequency", 1, 1, lo=20, hi=20000, unit="Hz"),
                L("gain", "Gain", 1, 2), B("mute", "Mute", 1, 3)]
    if kind == "white":
        return [L("gain", "Gain", 2, 1), B("mute", "Mute", 2, 2)]
    if kind == "pink":
        return [L("gain", "Gain", 3, 1), B("mute", "Mute", 3, 2)]
    if kind == "noise":
        return [
            E("noise_type", "Noise Type", 1, values=[("2", "White"), ("3", "Pink")]),
            L("white_gain", "White Noise Gain", 2, 1), B("white_mute", "White Noise Mute", 2, 2),
            L("pink_gain", "Pink Noise Gain", 3, 1), B("pink_mute", "Pink Noise Mute", 3, 2),
        ]
    if kind == "sweep":
        return [
            L("gain", "Gain", 4, 1),
            E("speed", "Speed", 4, 2, values=[("S", "Slow"), ("F", "Fast")]),
            B("repeat", "Repeat", 4, 3),
            B("running", "Start / Stop", 4, 4),
        ]
    raise ValueError("a signal generator's size is its kind: sine, white, pink, noise or sweep")


def build_source_selector(size: str) -> list[ControlDef]:
    n = _parse_count(size, default=16, maximum=16)
    return [Int("source", "Source", 1, lo=1, hi=n)]


def build_speaker_peq(size: str) -> list[ControlDef]:
    out = [
        N("gain", "EQ Gain", 0, 3, lo=-15, hi=15, step=0.5, unit="dB"),
        Int("align_delay", "Alignment Delay", 0, 4, lo=0, hi=480, unit="samples"),
        E("high_type", "High Pass Type", 0, 5, values=FILTER_TYPES),
        Int("high_frequency", "High Pass Frequency", 0, 6, lo=20, hi=20000, unit="Hz"),
        E("low_type", "Low Pass Type", 0, 7, values=FILTER_TYPES),
        Int("low_frequency", "Low Pass Frequency", 0, 8, lo=20, hi=20000, unit="Hz"),
        B("low_bypass", "Low Pass Bypass", 0, 9),
        B("high_bypass", "High Pass Bypass", 0, 10),
        B("polarity", "Polarity", 0, 11),
    ]
    for b in range(1, 10):
        p, lbl = f"band_{b}_", f"Band {b} "
        out += [
            Int(p + "frequency", lbl + "Frequency", b, 1, lo=20, hi=20000, unit="Hz"),
            N(p + "q", lbl + "Q", b, 2, lo=0.1, hi=10, step=0.1),
            N(p + "gain", lbl + "Gain", b, 3, lo=-20, hi=20, step=0.1, unit="dB"),
            E(p + "type", lbl + "Type", b, 5, values=SPEAKER_PEQ_TYPES),
            B(p + "bypass", lbl + "Bypass", b, 6),
        ]
    return out


def build_standard_mixer(size: str) -> list[ControlDef]:
    ins, outs = _parse_matrix(size, default=(4, 4), maximum=32)
    out: list[ControlDef] = []
    for i in range(1, ins + 1):
        out += [L(f"input_{i}_level", f"Input {i} Level", 1, 2 * i - 1),
                B(f"input_{i}_mute", f"Input {i} Mute", 1, 2 * i)]
    for o in range(1, outs + 1):
        out += [L(f"output_{o}_level", f"Output {o} Level", 2, 2 * o - 1),
                B(f"output_{o}_mute", f"Output {o} Mute", 2, 2 * o)]
    for i in range(1, ins + 1):
        # Routing A (one mask per input) is what is read; routing B (one
        # cross-point) is what is written.
        out.append(ControlDef(f"input_{i}_routing", f"Input {i} Routing (hex mask)", _idx(3, i),
                              FMT_ROUTING, writable=False, fanout=outs))
        for o in range(1, outs + 1):
            out.append(ControlDef(f"xp_{i}_{o}", f"In {i} to Out {o}", _idx(4, f"({i},{o})"),
                                  FMT_ONOFF, subscribe=False))
    return out


def build_tone_eq(size: str) -> list[ControlDef]:
    return [
        N("low_gain", "Low Gain", 1, lo=-15, hi=15, step=0.1, unit="dB"),
        B("low_bypass", "Low Bypass", 2),
        N("mid_gain", "Mid Gain", 3, lo=-15, hi=15, step=0.1, unit="dB"),
        B("mid_bypass", "Mid Bypass", 4),
        N("high_gain", "High Gain", 5, lo=-15, hi=15, step=0.1, unit="dB"),
        B("high_bypass", "High Bypass", 6),
    ]


def build_bypass_only(size: str) -> list[ControlDef]:
    return [B("bypass", "Bypass", 1)]


def build_standard_room_combiner(size: str) -> list[ControlDef]:
    return [
        Int("bgm_source", "BGM Source (0 = none)", 1, 1, lo=0, hi=32),
        L("bgm_gain", "BGM Gain", 1, 2),
        B("bgm_mute", "BGM Mute", 1, 3),
        L("main_input_gain", "Main Input Gain", 1, 4),
        B("main_input_mute", "Main Input Mute", 1, 5),
        L("main_output_gain", "Main Output Gain", 1, 6),
        B("main_output_mute", "Main Output Mute", 1, 7),
    ]


def build_pfs(size: str) -> list[ControlDef]:
    filters = _parse_count(size, default=16, maximum=16)
    out = [
        B("bypass_detection", "Bypass Dynamic Filter Detection", 0, 2),
        Int("release_time", "Dynamic Filter Release Time", 1, 1, lo=1, hi=43200, unit="s"),
    ]
    for k in range(1, filters + 1):
        f, p, lbl = k + 1, f"filter_{k}_", f"Filter {k} "
        out += [
            N(p + "gain", lbl + "Gain", f, 1, lo=-24, hi=0, step=0.1, unit="dB"),
            Int(p + "frequency", lbl + "Center Frequency", f, 2, lo=20, hi=20000, unit="Hz"),
            N(p + "q", lbl + "Q", f, 3, lo=0.1, hi=14.4, step=0.1),
            B(p + "bypass", lbl + "Bypass", f, 4),
            B(p + "static", lbl + "Static", 18, k),
        ]
    out.append(N("system_gain", "System Gain", 19, 0, lo=0, hi=12, step=0.5, unit="dB"))
    for i, (prop, label) in enumerate([("bypass_all_filters", "Bypass All Filters"),
                                       ("unbypass_all_filters", "Un-bypass All Filters"),
                                       ("set_all_static", "Set All Filters Static"),
                                       ("set_all_dynamic", "Set All Filters Dynamic"),
                                       ("clear_dynamic_filters", "Clear All Dynamic Filters")]):
        out.append(ControlDef(prop, label, _idx(20, i), FMT_ONOFF, subscribe=False))
    return out


def build_surround_input(size: str) -> list[ControlDef]:
    out = [
        E("input_source", "Input Source", 1, values=[("O", "Optical"), ("C", "Coaxial")]),
        S("output_format", "Output Format", 2),
        RO(E("room_type", "Room Type", 3, values=[("S", "Small"), ("L", "Large"), ("N", "None")])),
    ]
    for i, (prop, label) in enumerate([("left_front", "Left Front"), ("right_front", "Right Front"),
                                       ("left_surround", "Left Surround"),
                                       ("right_surround", "Right Surround"), ("center", "Center"),
                                       ("lfe", "LFE (Sub)"), ("back_left", "Back Surround Left"),
                                       ("back_right", "Back Surround Right")], 4):
        out.append(L(prop + "_level", label + " Level", i))
    return out


def build_parameter_set_list(size: str) -> list[ControlDef]:
    return [ControlDef("selection", "Selection", _idx(2), FMT_INT, write_idx=_idx(1), min=1)]


def build_listening_area_av(size: str) -> list[ControlDef]:
    return [E("auto_volume", "AutoVolume", 1, values=[("1", "Off"), ("2", "On")])]


def build_custom(size: str) -> list[ControlDef]:
    """One parameter: ``size`` is "<index path> <format> [<min> <max>]",
    e.g. "3>(1,1) onoff" for a Conference Room Router cross-point or
    "5>4>3 onoff" for a gate inside a Logic block."""
    parts = str(size or "").split()
    if len(parts) < 2:
        raise ValueError("a custom row's Size is the index path and the value format, "
                         "e.g. '3>(1,1) onoff' or '0>1 level'")
    idx = tuple(p.strip() for p in parts[0].split(">") if p.strip())
    if not idx:
        raise ValueError("a custom row needs an index path such as 1 or 0>3")
    token = parts[1].lower()
    fmt = FORMAT_ALIASES.get(token, token)
    if fmt not in VALUE_FORMATS:
        raise ValueError(f"unknown value format {parts[1]!r} (use one of {', '.join(VALUE_FORMATS)})")
    lo = hi = None
    if len(parts) >= 4:
        try:
            lo, hi = float(parts[2]), float(parts[3])
        except ValueError as exc:
            raise ValueError("a custom row's range is two numbers, e.g. '2 level -60.5 0'") from exc
    ctl = ControlDef("value", "Value", idx, fmt, writable=fmt != FMT_STRING,
                     min=lo, max=hi)
    if fmt == FMT_LEVEL:
        ctl.step, ctl.unit = 0.5, "dB"
    return [ctl]


SIGNAL_LEVEL_TYPE = "signal_level"


def build_signal_level(size: str) -> list[ControlDef]:
    """Not module controls: the row is a Get Signal Level slot. ``size`` is
    "<slot>[,<parameter>] [<channels>] [<floor dB>]"; the meter channels are
    registered from the first reply when the count is left out."""
    spec = parse_signal_level_size(size)
    return [_meter_control(c, spec.floor_db) for c in range(1, spec.channels + 1)]


@dataclass
class SignalLevelSpec:
    slot: str
    param: str | None
    channels: int
    floor_db: float

    @property
    def query(self) -> str:
        return f"GL {self.slot},{self.param}" if self.param else f"GL {self.slot}"


_HEX_RE = re.compile(r"^[0-9a-fA-F]{1,2}$")


def parse_signal_level_size(size: Any) -> SignalLevelSpec:
    parts = str(size or "").split()
    if not parts:
        raise ValueError("a signal level row's Size is the slot index, e.g. '1' or '8,3' "
                         "(Get Signal Level indices), then optionally the channel count and "
                         "the dB floor, e.g. '2 4 -35'")
    slot_part = parts[0]
    slot, _, param = slot_part.partition(",")
    if not _HEX_RE.match(slot) or (param and not _HEX_RE.match(param)):
        raise ValueError(f"slot {slot_part!r} is not a hexadecimal slot index such as 1, 8,3 or A")
    channels = 0
    floor_db = -60.0
    rest = parts[1:]
    if rest and re.match(r"^\d{1,2}$", rest[0]):
        channels = int(rest[0])
        rest = rest[1:]
        if not 1 <= channels <= 64:
            raise ValueError("a signal level row covers 1..64 channels")
    if rest:
        try:
            floor_db = float(rest[0])
        except ValueError as exc:
            raise ValueError(f"floor {rest[0]!r} is not a dB value (-60, -35 or -36)") from exc
        rest = rest[1:]
    if rest:
        raise ValueError(f"unexpected text in a signal level row's Size: {' '.join(rest)!r}")
    return SignalLevelSpec(slot.lower(), param.lower() or None, channels, floor_db)


def _meter_control(channel: int, floor_db: float) -> ControlDef:
    unit = "dBu" if floor_db > -50 else "dBFS"
    return ControlDef(f"level_{channel}", f"Channel {channel} Level", (), FMT_METER,
                      writable=False, subscribe=False, min=floor_db, unit=unit)


MODULE_TYPES: dict[str, dict[str, Any]] = {
    "input": {"label": "Input (analog / Mic-Line channel)", "build": build_input},
    "output": {"label": "Output / ESPLink / AmpLink / Dante channel", "build": build_output},
    "gain": {"label": "Gain (also PSTN / VoIP Output, CSP Listening Area Gain)",
             "build": build_gain},
    "source_selector": {"label": "Source Selector (also CSP Listening Area Selector)",
                        "build": build_source_selector},
    "router": {"label": "Router", "build": build_router},
    "standard_mixer": {"label": "Standard Mixer (inputs x outputs)", "build": build_standard_mixer},
    "matrix_mixer": {"label": "Matrix Mixer (inputs x outputs)", "build": build_matrix_mixer},
    "amm_gain_sharing": {"label": "AMM - Gain Sharing (EX / 1U ESP)", "build": build_amm_gain_sharing},
    "amm_gated": {"label": "AMM - Gated, Enhanced (EX)", "build": build_amm_gated},
    "amm_gated_legacy": {"label": "AMM - Gated, Legacy (ESP)", "build": build_amm_gated_legacy},
    "aec": {"label": "Acoustic Echo Canceller (EX)", "build": build_aec},
    "agc": {"label": "AGC, Enhanced (EX)", "build": build_agc},
    "agc_legacy": {"label": "AGC, Legacy", "build": build_agc_legacy},
    "conference_room_router": {"label": "Conference Room Router (EX)",
                               "build": build_conference_room_router},
    "standard_room_combiner": {"label": "Standard Room Combiner",
                               "build": build_standard_room_combiner},
    "parametric_eq": {"label": "Parametric EQ", "build": build_parametric_eq},
    "graphic_eq": {"label": "1/3 Octave Graphic EQ", "build": build_graphic_eq},
    "tone_eq": {"label": "Tone Control EQ", "build": build_tone_eq},
    "speaker_peq": {"label": "Speaker Parametric EQ", "build": build_speaker_peq},
    "array_eq": {"label": "Array EQ", "build": build_array_eq},
    "crossover": {"label": "Crossover (2, 3 or 4 way)", "build": build_crossover},
    "compressor": {"label": "Compressor / Limiter", "build": build_compressor},
    "peak_rms_limiter": {"label": "Peak / RMS Limiter", "build": build_peak_rms_limiter},
    "gate": {"label": "Gate", "build": build_gate},
    "ducker": {"label": "Ducker", "build": build_ducker},
    "delay": {"label": "Delay (taps)", "build": build_delay},
    "smartbass": {"label": "SmartBass (EX / 1U ESP)", "build": build_bypass_only},
    "dynamic_eq": {"label": "Dynamic EQ (EX / 1U ESP)", "build": build_bypass_only},
    "pfs": {"label": "Predictive Feedback Suppression (EX / 1U ESP)", "build": build_pfs},
    "signal_generator": {"label": "Signal Generator (sine, white, pink, noise, sweep)",
                         "build": build_signal_generator},
    "gpo": {"label": "GPO", "build": build_gpo},
    "logic_input": {"label": "Logic Input (EX)", "build": build_logic_pins},
    "logic_output": {"label": "Logic Output (EX)", "build": build_logic_pins},
    "logic_block": {"label": "Logic Processing block (EX)", "build": build_logic_block},
    "usb_input": {"label": "USB Input (EX)", "build": build_usb},
    "usb_output": {"label": "USB Output (EX)", "build": build_usb},
    "pstn_input": {"label": "PSTN Input (EX)", "build": build_pstn_input},
    "voip_input": {"label": "VoIP Input (EX)", "build": build_voip_input},
    "surround_input": {"label": "Surround Input (ESP-00)", "build": build_surround_input},
    "parameter_set_list": {"label": "Parameter Set List", "build": build_parameter_set_list},
    "listening_area_av": {"label": "CSP Listening Area AutoVolume", "build": build_listening_area_av},
    SIGNAL_LEVEL_TYPE: {"label": "Signal Level meters (a Get Signal Level slot)",
                        "build": build_signal_level},
    "custom": {"label": "Custom (one parameter by index)", "build": build_custom},
}


def _parse_count(size: Any, default: int, maximum: int) -> int:
    s = str(size or "").strip().lower()
    if not s:
        return default
    m = re.match(r"^(\d+)$", s)
    if not m:
        raise ValueError(f"size {size!r} must be a count (e.g. 8)")
    n = int(m.group(1))
    if not 1 <= n <= maximum:
        raise ValueError(f"size {n} is outside 1..{maximum}")
    return n


def _parse_matrix(size: Any, default: tuple[int, int], maximum: int) -> tuple[int, int]:
    s = str(size or "").strip().lower()
    if not s:
        return default
    m = re.match(r"^(\d+)\s*x\s*(\d+)$", s)
    if not m:
        raise ValueError(f"size {size!r} must be inputs x outputs (e.g. 8x4)")
    ins, outs = int(m.group(1)), int(m.group(2))
    if not (1 <= ins <= maximum and 1 <= outs <= maximum):
        raise ValueError(f"size {size!r} is outside 1..{maximum} on a side")
    return ins, outs


# ── The declared tables ─────────────────────────────────────────────────────

MODULE_CHILD_TYPE = "module"
GROUP_CHILD_TYPE = "group"
ROOM_COMBINE_CHILD_TYPE = "room_combine"

MODULE_COLUMNS: dict[str, dict[str, Any]] = {
    "name": {
        "type": "string", "label": "Module Label", "required": True,
        "help": "The module's label exactly as ControlSpace Designer shows it "
                "(Main Volume, Input 1, AmpLink-Ch 3). Labels must be unique on "
                "the processor. Becomes the child entity's id.",
    },
    "type": {
        "type": "enum", "label": "Module Type", "required": True,
        "values": [{"value": k, "label": v["label"]} for k, v in MODULE_TYPES.items()],
        "help": "The module type placed in the design. Decides which controls "
                "are exposed.",
    },
    "size": {
        "type": "string", "label": "Size",
        "help": "Inputs for an AMM or AGC (8), inputs x outputs for a mixer or "
                "logic block (8x4), bands for a PEQ (5), taps for a delay (4), "
                "pins for logic and GPO, ways for a crossover (3), far-end "
                "inputs for a Conference Room Router, the kind for a signal "
                "generator (sine, white, pink, noise, sweep). Signal Level: the "
                "GL slot then optionally channels and floor ('1', '8,3 12', "
                "'2 4 -35'). Custom: index path and format ('3>(1,1) onoff'). "
                "Blank = the default.",
    },
    "device": {
        "type": "string", "label": "On Device",
        "help": "Leave blank for the processor you are connected to. An ESP "
                "network can control a module on another ESP: enter that "
                "device's label from ControlSpace Designer.",
    },
}

GROUP_COLUMNS: dict[str, dict[str, Any]] = {
    "number": {"type": "integer", "label": "Group Number", "required": True, "min": 1, "max": 64,
               "help": "The Group number in ControlSpace Designer (1-64)."},
    "name": {"type": "string", "label": "Name", "required": True,
             "help": "What the group controls (Lobby Volume). Shown on the child entity."},
    "kind": {"type": "enum", "label": "Kind", "required": True,
             "values": [{"value": "level", "label": "Volume group (level + mute)"},
                        {"value": "selector", "label": "Source selector group"}],
             "help": "A group of gains, inputs or outputs carries a master level and "
                     "mute; a group of source selectors carries the selected channel."},
}

PARAMETER_SET_COLUMNS: dict[str, dict[str, Any]] = {
    "number": {"type": "integer", "label": "Parameter Set", "required": True, "min": 1, "max": 255,
               "help": "The Parameter Set number in ControlSpace Designer (1-255)."},
    "name": {"type": "string", "label": "Name", "required": True,
             "help": "Shown in the recall picker and as the current parameter set's name."},
}

DEFAULT_MODULES: list[dict[str, str]] = [
    {"name": "Main Volume", "type": "gain", "size": "", "device": ""},
    {"name": "Input 1", "type": "input", "size": "", "device": ""},
    {"name": "Output 1", "type": "output", "size": "", "device": ""},
    {"name": "Selector 1", "type": "source_selector", "size": "4", "device": ""},
]
DEFAULT_GROUPS: list[dict[str, Any]] = []
DEFAULT_PARAMETER_SETS: list[dict[str, Any]] = []


@dataclass
class ModuleDef:
    cid: str
    name: str
    type_id: str
    size: str
    device: str
    controls: dict[str, ControlDef]
    signal_level: SignalLevelSpec | None = None

    @property
    def type_label(self) -> str:
        return MODULE_TYPES[self.type_id]["label"]

    def by_read_path(self) -> dict[tuple[str, ...], ControlDef]:
        return {c.idx: c for c in self.controls.values() if c.idx}


@dataclass
class GroupDef:
    number: int
    name: str
    kind: str

    @property
    def controls(self) -> dict[str, ControlDef]:
        if self.kind == "selector":
            return {"source": Int("source", "Source", lo=1, hi=32)}
        return {"level_db": L("level_db", "Master Level", lo=LEVEL_MIN, hi=LEVEL_MAX),
                "mute": B("mute", "Master Mute")}


def safe_child_id(name: str) -> str:
    cid = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name).strip()).strip("_")
    return cid[:120] or "module"


def parse_modules_config(rows: Any) -> tuple[list[ModuleDef], list[str]]:
    """Expand the table rows into modules; a bad row is reported, not fatal."""
    modules: list[ModuleDef] = []
    problems: list[str] = []
    seen: set[str] = set()
    if isinstance(rows, str):
        rows = _rows_from_text(rows)
    if not isinstance(rows, list):
        return modules, ["the module list is not a list of rows"]
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            problems.append(f"row {n}: not a table row")
            continue
        name = str(row.get("name") or "").strip()
        type_id = str(row.get("type") or "").strip().lower()
        size = str(row.get("size") or "").strip()
        device = str(row.get("device") or "").strip()
        try:
            if not name:
                raise ValueError("no module label")
            if '"' in name or '"' in device:
                raise ValueError("a label cannot contain a double quote")
            if type_id not in MODULE_TYPES:
                raise ValueError(f"unknown module type {type_id!r}")
            controls = MODULE_TYPES[type_id]["build"](size)
            spec = parse_signal_level_size(size) if type_id == SIGNAL_LEVEL_TYPE else None
        except ValueError as exc:
            problems.append(f"row {n} ({name or '?'}): {exc}")
            continue
        cid = safe_child_id(name)
        if cid in seen:
            problems.append(f"row {n} ({name}): duplicates another row's label")
            continue
        seen.add(cid)
        modules.append(ModuleDef(cid, name, type_id, size, device,
                                 {c.prop: c for c in controls}, spec))
    return modules, problems


def _rows_from_text(text: str) -> list[dict[str, str]]:
    """``name | type | size`` per line, for a config written by hand."""
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        rows.append({"name": parts[0], "type": parts[1],
                     "size": parts[2] if len(parts) > 2 else "",
                     "device": parts[3] if len(parts) > 3 else ""})
    return rows


def parse_groups_config(rows: Any) -> tuple[list[GroupDef], list[str]]:
    groups: list[GroupDef] = []
    problems: list[str] = []
    seen: set[int] = set()
    if not isinstance(rows, list):
        return groups, ["the group list is not a list of rows"] if rows else []
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            problems.append(f"group row {n}: not a table row")
            continue
        try:
            number = int(row.get("number"))
        except (TypeError, ValueError):
            problems.append(f"group row {n}: the group number is not a number")
            continue
        name = str(row.get("name") or "").strip() or f"Group {number}"
        kind = str(row.get("kind") or "level").strip().lower()
        if not 1 <= number <= GROUP_MAX:
            problems.append(f"group row {n} ({name}): group number {number} is outside 1..64")
            continue
        if kind not in ("level", "selector"):
            problems.append(f"group row {n} ({name}): kind must be level or selector")
            continue
        if number in seen:
            problems.append(f"group row {n} ({name}): duplicates group {number}")
            continue
        seen.add(number)
        groups.append(GroupDef(number, name, kind))
    return groups, problems


def parse_parameter_sets_config(rows: Any) -> tuple[dict[int, str], list[str]]:
    names: dict[int, str] = {}
    problems: list[str] = []
    if not isinstance(rows, list):
        return names, ["the parameter set list is not a list of rows"] if rows else []
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            problems.append(f"parameter set row {n}: not a table row")
            continue
        try:
            number = int(row.get("number"))
        except (TypeError, ValueError):
            problems.append(f"parameter set row {n}: the number is not a number")
            continue
        if not 1 <= number <= PARAMETER_SET_MAX:
            problems.append(f"parameter set row {n}: {number} is outside 1..255")
            continue
        names[number] = str(row.get("name") or "").strip() or f"Parameter Set {number}"
    return names, problems


_MODULE_SUMMARY_SCHEMA: dict[str, dict[str, Any]] = {
    "name": {"type": "string", "label": "Module Label"},
    "module_type": {"type": "string", "label": "Module Type"},
    "device": {"type": "string", "label": "On Device"},
    "responding": {"type": "boolean", "label": "Responding"},
}
_GROUP_SUMMARY_SCHEMA: dict[str, dict[str, Any]] = {
    "name": {"type": "string", "label": "Name"},
    "kind": {"type": "string", "label": "Kind"},
    "responding": {"type": "boolean", "label": "Responding"},
}


def _room_pairs() -> list[tuple[int, int]]:
    return [(a, b) for a in range(1, ROOM_MAX + 1) for b in range(a + 1, ROOM_MAX + 1)]


_ROOM_COMBINE_SCHEMA: dict[str, dict[str, Any]] = {
    "joined": {"type": "string", "label": "Joined Rooms",
               "help": "As the processor reports it: rooms in one bracket are joined, "
                       "e.g. [2,4,5][1,3]."},
    "responding": {"type": "boolean", "label": "Responding"},
    **{f"joined_{a}_{b}": {"type": "boolean", "label": f"Rooms {a} and {b} Joined", "control": True}
       for a, b in _room_pairs()},
}

CHILD_TYPES: dict[str, dict[str, Any]] = {
    MODULE_CHILD_TYPE: {
        "label": "Module",
        "label_plural": "Modules",
        "dynamic": True,
        "id_format": {"type": "string", "max_length": 128},
        "state_variables": dict(_MODULE_SUMMARY_SCHEMA),
        "summary_fields": ["module_type", "device", "responding"],
        "label_field": "name",
    },
    GROUP_CHILD_TYPE: {
        "label": "Group",
        "label_plural": "Groups",
        "dynamic": True,
        "id_format": {"type": "integer", "min": 1, "max": GROUP_MAX},
        "state_variables": dict(_GROUP_SUMMARY_SCHEMA),
        "summary_fields": ["kind", "responding"],
        "label_field": "name",
    },
    ROOM_COMBINE_CHILD_TYPE: {
        "label": "Room Combine Group",
        "label_plural": "Room Combine Groups",
        "id_format": {"type": "integer", "min": 1, "max": ROOM_COMBINE_MAX},
        "state_variables": dict(_ROOM_COMBINE_SCHEMA),
        "summary_fields": ["joined", "responding"],
    },
}


# ── Protocol text ───────────────────────────────────────────────────────────

def module_ref(name: str, device: str = "") -> str:
    if device:
        return f'@ "{device}" "{name}"'
    return f'"{name}"'


def sa_line(name: str, path: str, value: str, device: str = "") -> str:
    return f"SA {module_ref(name, device)}>{path}={value}"


def ga_line(name: str, path: str, device: str = "") -> str:
    return f"GA {module_ref(name, device)}>{path}"


def ma_line(name: str, index: str, parameter: str | None, device: str = "") -> str:
    base = f"MA {module_ref(name, device)}>{index}"
    return base if parameter is None else f'{base}="{parameter}"'


def sub_line(get_text: str) -> str:
    return f'SUB "{get_text}"'


def uns_line(get_text: str) -> str:
    return f'UNS "{get_text}"'


_GA_RE = re.compile(r'^GA\s*"([^"]*)"((?:>[^>=]*)+)=(.*)$')
_SUB_RE = re.compile(r'^(SUB|UNS)\s*"(.*)",\s*(yes|no)\s*$', re.IGNORECASE)
_SUB_SUPPORT_RE = re.compile(r"^SUB\s+(yes|no)\s*$", re.IGNORECASE)
_S_RE = re.compile(r"^S\s*([0-9a-fA-F]+)$")
_GG_RE = re.compile(r"^GG\s*([0-9a-fA-F]+),([0-9a-fA-F]+)$")
_GN_RE = re.compile(r"^GN\s*([0-9a-fA-F]+),([MUmu])$")
_GRC_RE = re.compile(r"^GRC\s*(.*)$")
_GV_RE = re.compile(r"^GV\s*([0-9a-fA-F]+),([0-9a-fA-F]+),([0-9a-fA-F]+)$")
_GM_RE = re.compile(r"^GM\s*([0-9a-fA-F]+),([0-9a-fA-F]+),([MUmu])$")
_GL_RE = re.compile(r"^GL\s*([0-9a-fA-F]+)(?:,([0-9a-fA-F]+))?\s*\[([^\]]*)\]$")
_IP_RE = re.compile(r"^IP\s+(\d{1,3}(?:\.\d{1,3}){3})$")
_NP_RE = re.compile(r"^NP\s*([TMGtmg]),(.+)$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


@dataclass
class ModuleReply:
    name: str
    idx: tuple[str, ...]
    value: str


def parse_ga_reply(text: str) -> ModuleReply | None:
    m = _GA_RE.match(text)
    if not m:
        return None
    idx = tuple(p.strip() for p in m.group(2).split(">") if p.strip())
    return ModuleReply(m.group(1), idx, m.group(3).strip())


def parse_grc_joined(text: str) -> tuple[str, list[set[int]]] | None:
    """``GRC n,[2,4,5][1,3]`` -> ("n", [{2,4,5}, {1,3}]). A pair reply
    (``GRC n,a,b,s``) is not a joined-rooms report and returns None."""
    m = _GRC_RE.match(text)
    if not m:
        return None
    body = m.group(1).strip()
    ref, sep, rest = body.partition(",")
    if not sep:
        return ref.strip().strip('"'), []
    if "[" not in rest:
        return None
    groups = [set(int(x) for x in re.findall(r"\d+", grp)) for grp in re.findall(r"\[([^\]]*)\]", rest)]
    return ref.strip().strip('"'), [g for g in groups if g]


def decode_value(ctl: ControlDef, text: str) -> Any:
    t = text.strip()
    if ctl.fmt in (FMT_ONOFF, FMT_LOGIC):
        return parse_onoff(t)
    if ctl.fmt == FMT_LEVEL or ctl.fmt == FMT_NUMBER:
        return float(t)
    if ctl.fmt == FMT_INT:
        return int(float(t))
    if ctl.fmt == FMT_ENUM:
        return t.strip('"')
    if ctl.fmt == FMT_ROUTING:
        return t.upper()
    return t.strip('"')


def encode_value(ctl: ControlDef, value: Any) -> str:
    """A user's value for a Set, in the control's wire form."""
    if ctl.fmt in (FMT_ONOFF, FMT_LOGIC):
        return "O" if coerce_onoff(value) else "F"
    if ctl.fmt in (FMT_LEVEL, FMT_NUMBER):
        v = float(str(value).strip())
        if ctl.min is not None and v < ctl.min:
            raise ValueError(f"{ctl.label}: {v} is below the minimum {ctl.min}")
        if ctl.max is not None and v > ctl.max:
            raise ValueError(f"{ctl.label}: {v} is above the maximum {ctl.max}")
        return format_number(v)
    if ctl.fmt == FMT_INT:
        v = int(float(str(value).strip()))
        if ctl.min is not None and v < ctl.min:
            raise ValueError(f"{ctl.label}: {v} is below the minimum {int(ctl.min)}")
        if ctl.max is not None and v > ctl.max:
            raise ValueError(f"{ctl.label}: {v} is above the maximum {int(ctl.max)}")
        return str(v)
    if ctl.fmt == FMT_ENUM:
        s = str(value).strip()
        for wire, label in ctl.values:
            if s == wire or s.lower() == label.lower():
                return wire
        raise ValueError(f"{ctl.label}: {s!r} is not one of "
                         f"{', '.join(w for w, _ in ctl.values)}")
    if ctl.fmt == FMT_STRING:
        s = str(value).strip()
        if '"' in s:
            raise ValueError(f"{ctl.label}: a value cannot contain a double quote")
        return s
    raise ValueError(f"{ctl.label} cannot be set")


# ── Command surface ─────────────────────────────────────────────────────────

def _module_param() -> dict[str, Any]:
    return {"type": "child_id", "child_type": MODULE_CHILD_TYPE, "required": True,
            "label": "Module",
            "help": "One of the modules declared on the device page."}


def _control_param(help: str = "") -> dict[str, Any]:
    p: dict[str, Any] = {"type": "string", "required": True, "label": "Control",
                         "options_from": {"param": "module", "source": "child_schema"}}
    if help:
        p["help"] = help
    return p


def _group_param() -> dict[str, Any]:
    return {"type": "child_id", "child_type": GROUP_CHILD_TYPE, "required": True,
            "label": "Group", "help": "One of the groups declared on the device page."}


def _rc_group_param() -> dict[str, Any]:
    return {"type": "child_id", "child_type": ROOM_COMBINE_CHILD_TYPE, "required": True,
            "label": "Room Combine Group"}


def _room_param(label: str) -> dict[str, Any]:
    return {"type": "integer", "required": True, "label": label, "min": 1, "max": ROOM_MAX,
            "help": "The room number in the Room Combine Group configuration."}


def _hex_param(label: str, help: str) -> dict[str, Any]:
    return {"type": "string", "required": True, "label": label,
            "pattern": r"^\s*[0-9a-fA-F]{1,2}\s*$", "help": help}


def _level_param() -> dict[str, Any]:
    return {"type": "number", "required": True, "label": "Level (dB)", "unit": "dB",
            "min": LEVEL_MIN, "max": LEVEL_MAX, "decimals": 1,
            "help": "-60 to +12 dB in 0.5 dB steps; -60.5 is off."}


def _step_param() -> dict[str, Any]:
    return {"type": "number", "required": True, "label": "Amount (dB)", "unit": "dB",
            "default": 1.0, "min": -72, "max": 72, "decimals": 1,
            "help": "Positive raises, negative lowers, in 0.5 dB steps."}


def _module_name_param() -> dict[str, Any]:
    return {"type": "string", "required": True, "label": "Module Label",
            "pattern": r'^[^"]{1,64}$',
            "help": "The label from ControlSpace Designer, without quotes."}


def _index_param() -> dict[str, Any]:
    return {"type": "string", "required": True, "label": "Index Path",
            "pattern": r"^[0-9(),>\s]{1,32}$",
            "help": "The indices separated by >, e.g. 1, 0>3 or 4>(2,5)."}


def _device_param() -> dict[str, Any]:
    return {"type": "string", "label": "On Device", "pattern": r'^[^"]{0,64}$',
            "help": "Blank for this processor; another ESP's label to reach a module on it."}


def _number_param() -> dict[str, Any]:
    return {"type": "string", "required": True, "label": "Number or SIP address",
            "pattern": r'^[^"]{1,64}$',
            "help": "A telephone number, an extension, or sip:user@host for VoIP."}


COMMANDS: dict[str, dict[str, Any]] = {
    "recall_parameter_set": {
        "label": "Recall Parameter Set",
        "params": {"number": {"type": "integer", "required": True, "label": "Parameter Set",
                              "min": 1, "max": PARAMETER_SET_MAX,
                              "options_state": "parameter_set_options",
                              "help": "The Parameter Set number (1-255). Names come from the "
                                      "Parameter Sets table on the device page."}},
        "help": "Recall a Parameter Set. Sent to the RTC/Main processor it reaches "
                "every device in the design.",
    },
    "set_control": {
        "label": "Set Control",
        "params": {
            "module": _module_param(),
            "control": _control_param(),
            "value": {"type": "string", "required": True, "label": "Value",
                      "type_from": {"param": "control"},
                      "help": "dB for a level, on/off for a mute or bypass, a number "
                              "for a source or a frequency, a type name for a filter."},
        },
        "help": "Set any control on a declared module.",
    },
    "toggle_control": {
        "label": "Toggle Control",
        "params": {"module": _module_param(),
                   "control": _control_param("A mute, bypass, polarity, cross-point or other on/off control.")},
        "help": "Flip an on/off control. The processor toggles it, so no current value is needed.",
    },
    "pulse_control": {
        "label": "Pulse Logic Control",
        "params": {"module": _module_param(),
                   "control": _control_param("A logic input or output pin.")},
        "help": "Momentarily press a logic pin: on, then back off.",
    },
    "step_level": {
        "label": "Step Level (dB)",
        "params": {"module": _module_param(),
                   "control": _control_param("A level control."),
                   "amount": _step_param()},
        "help": "Raise or lower a level by a number of dB from its current value.",
    },
    "make_call": {
        "label": "Make Call",
        "params": {"module": _module_param(), "number": _number_param()},
        "help": "Dial on a PSTN or VoIP input module (EX).",
    },
    "answer_call": {
        "label": "Answer Call",
        "params": {"module": _module_param()},
        "help": "Answer the incoming call on a PSTN or VoIP input module (EX).",
    },
    "end_call": {
        "label": "End Call",
        "params": {"module": _module_param()},
        "help": "Hang up on a PSTN or VoIP input module (EX).",
    },
    "dial_key": {
        "label": "Dial Key (DTMF)",
        "params": {"module": _module_param(),
                   "key": {"type": "string", "required": True, "label": "Key",
                           "pattern": r"^[0-9#*!]$",
                           "help": "0-9, # or *; ! is a hook flash (PSTN). During an active call only."}},
        "help": "Press a key during a call on a PSTN or VoIP input module (EX).",
    },
    "transfer_call": {
        "label": "Transfer Call",
        "params": {"module": _module_param(), "number": _number_param()},
        "help": "Transfer the active call on a VoIP input module (EX).",
    },
    "set_group_level": {
        "label": "Set Group Level",
        "params": {"group": _group_param(), "level": _level_param()},
        "help": "Set the master level of a volume group.",
    },
    "step_group_level": {
        "label": "Step Group Level (dB)",
        "params": {"group": _group_param(), "amount": _step_param()},
        "help": "Raise or lower a volume group's master level; the processor does the arithmetic.",
    },
    "set_group_mute": {
        "label": "Set Group Mute",
        "params": {"group": _group_param(),
                   "mute": {"type": "boolean", "required": True, "label": "Mute"}},
        "help": "Mute or unmute a volume group.",
    },
    "toggle_group_mute": {
        "label": "Toggle Group Mute",
        "params": {"group": _group_param()},
        "help": "Flip a volume group's mute; the processor toggles it.",
    },
    "set_group_source": {
        "label": "Set Group Source",
        "params": {"group": _group_param(),
                   "channel": {"type": "integer", "required": True, "label": "Source",
                               "min": 1, "max": 32}},
        "help": "Select the input on a source selector group.",
    },
    "join_rooms": {
        "label": "Join Rooms",
        "params": {"group": _rc_group_param(), "room_a": _room_param("Room A"),
                   "room_b": _room_param("Room B")},
        "help": "Join two rooms of a Room Combine Group (EX).",
    },
    "split_rooms": {
        "label": "Split Rooms",
        "params": {"group": _rc_group_param(), "room_a": _room_param("Room A"),
                   "room_b": _room_param("Room B")},
        "help": "Split two rooms of a Room Combine Group (EX).",
    },
    "set_io_level": {
        "label": "Set Input/Output Level by Slot",
        "params": {"slot": _hex_param("Slot", "The slot index from Table 1 of the protocol document, "
                                              "in hexadecimal (1-B)."),
                   "channel": _hex_param("Channel", "The channel within the slot, hexadecimal (1-8, "
                                                    "or up to 40 on a Dante slot)."),
                   "level": _level_param()},
        "help": "Set a physical input or output level without naming its module. "
                "Ignored by the processor while that channel is muted.",
    },
    "step_io_level": {
        "label": "Step Input/Output Level by Slot",
        "params": {"slot": _hex_param("Slot", "Hexadecimal slot index (Table 1)."),
                   "channel": _hex_param("Channel", "Hexadecimal channel within the slot."),
                   "amount": _step_param()},
        "help": "Raise or lower a physical channel's level; ignored while it is muted.",
    },
    "set_io_mute": {
        "label": "Set Input/Output Mute by Slot",
        "params": {"slot": _hex_param("Slot", "Hexadecimal slot index (Table 1)."),
                   "channel": _hex_param("Channel", "Hexadecimal channel within the slot."),
                   "state": {"type": "enum", "required": True, "label": "State",
                             "values": [{"value": "M", "label": "Mute"}, {"value": "U", "label": "Unmute"},
                                        {"value": "T", "label": "Toggle"}]}},
        "help": "Mute, unmute or toggle a physical input or output channel.",
    },
    "set_module_parameter": {
        "label": "Set Module Parameter (raw)",
        "params": {"module_name": _module_name_param(), "index": _index_param(),
                   "value": {"type": "string", "required": True, "label": "Value",
                             "help": "Exactly as the protocol document writes it: -3.5, O, But24."},
                   "device": _device_param()},
        "help": "Send any SA command, to a module that is not in the Modules table too.",
    },
    "query_module_parameter": {
        "label": "Query Module Parameter (raw)",
        "params": {"module_name": _module_name_param(), "index": _index_param(),
                   "device": _device_param()},
        "help": "Send a GA command and return the processor's reply.",
    },
    "invoke_module_action": {
        "label": "Invoke Module Action (raw)",
        "params": {"module_name": _module_name_param(),
                   "index": {"type": "integer", "required": True, "label": "Action Index",
                             "min": 1, "max": 9},
                   "parameter": {"type": "string", "label": "Parameter", "pattern": r'^[^"]{0,64}$',
                                 "help": "Leave blank for an action that takes none (End Call)."},
                   "device": _device_param()},
        "help": "Send an MA command.",
    },
    "set_ip_address": {
        "label": "Set IP Address (applies after reboot)",
        "params": {"address": {"type": "string", "required": True, "label": "IP Address",
                               "pattern": r"^\d{1,3}(\.\d{1,3}){3}$"}},
        "help": "Change the processor's IP address. Takes effect after a reboot; this "
                "device's own address must then be changed to match.",
    },
    "set_network_parameter": {
        "label": "Set Network Parameter (applies after reboot)",
        "params": {"parameter": {"type": "enum", "required": True, "label": "Parameter",
                                 "values": [{"value": "T", "label": "Addressing (DHCP or static)"},
                                            {"value": "M", "label": "Subnet mask"},
                                            {"value": "G", "label": "Default gateway"}]},
                   "value": {"type": "string", "required": True, "label": "Value",
                             "pattern": r"^([DdSs]|\d{1,3}(\.\d{1,3}){3})$",
                             "help": "D (DHCP) or S (static) for addressing; a dotted address otherwise."}},
        "help": "Change the subnet mask, gateway or addressing mode. Takes effect after a reboot.",
    },
    "reset_network_defaults": {
        "label": "Reset Network Settings to Defaults",
        "help": "Return every network setting to the factory default (an ESP-00 becomes "
                "192.168.0.16; the others go to DHCP). Takes effect after a reboot.",
    },
    "reboot": {
        "label": "Reboot Processor",
        "help": "Restart the processor. Audio stops until it is back, unsaved settings "
                "revert to the flashed design, and this connection drops until it returns.",
    },
    "resync": {
        "label": "Resync from Processor",
        "help": "Re-subscribe to (or re-read) every declared control so each reports "
                "its current value.",
    },
}


class BoseControlSpaceDriver(BaseDriver):
    """Bose Professional ControlSpace — Serial Control Protocol over TCP 10055 or RS-232."""

    DRIVER_INFO = {
        "id": "bose_controlspace",
        "name": "Bose Professional ControlSpace (ESP / EX / CSP)",
        "manufacturer": "Bose Professional",
        "category": "audio",
        "version": "1.0.0",
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls Bose Professional ControlSpace EX, ESP and CSP processors "
            "through the ControlSpace Serial Control Protocol on TCP port 10055 "
            "or the RS-232 port. Declare the modules from your ControlSpace "
            "Designer design (gains, inputs, outputs, selectors, routers, "
            "mixers, automatic mic mixers, EQs, dynamics, delays, logic, "
            "telephone and VoIP lines, signal-level meters, or any parameter "
            "by index) and each becomes a child entity whose controls panels "
            "bind to. Groups carry master level, mute and source; parameter "
            "sets recall by number or name; room combine groups join and "
            "split. Every control is subscribed on a processor that supports "
            "subscriptions, so changes made from a CC-16 or CC-64 wall "
            "controller or from Designer appear at once."
        ),
        "source_url": "https://assets.boseprofessional.com/m/4998082f60dfee56/original/ControlSpace-Serial-Protocol-v5-13.pdf",
        "tags": ["dsp", "bose", "controlspace", "esp", "ex", "csp", "install-audio",
                 "conference", "room-combine", "dante"],
        "verified": False,
        "simulated": True,
        "protocols": ["bose-controlspace-serial"],
        "ports": [10055],
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "discovery": {
            # ``IP`` with no argument asks the processor for its own address
            # (section 5.5) and changes nothing; every ControlSpace, PowerMatch
            # and PowerShare answers it on 10055.
            "tcp_probe": {
                "port": 10055,
                "send_ascii": "IP\r",
                "expect_regex": r"^IP \d{1,3}(\.\d{1,3}){3}",
                "timeout_ms": 1500,
            },
            "port_open": [10055],
            # Bose Corporation's registered prefixes (IEEE registry, read
            # through the Wireshark manuf list). Bose Professional's own
            # block (48:5E:0E:C0/28) is a 28-bit assignment the hint format
            # cannot carry.
            "oui": ["00:0c:8a", "04:52:c7", "08:df:1f", "28:11:a5", "2c:41:a1", "48:22:1d",
                    "4c:87:5d", "60:ab:d2", "68:f2:1f", "78:2b:64", "ac:bf:71", "bc:87:fa",
                    "c8:7b:23", "e4:58:bc"],
            "manufacturer_alias": ["bose", "bose professional", "bose corporation", "controlspace"],
        },
        "compatible_models": [
            {
                "manufacturer": "Bose Professional",
                "models": ["EX-1280C", "EX-12AEC", "EX-440C", "EX-1280"],
                "confidence": "untested",
                "notes": (
                    "Serial-over-Ethernet on TCP 10055 (the port can be changed or "
                    "disabled in Designer's device properties), or RS-232 at 115,200 "
                    "baud. AEC, VoIP, PSTN, USB, logic, conference room router and room "
                    "combine are EX-only. Built from the v5.13 protocol document and the "
                    "simulator; not yet run against a processor."
                ),
            },
            {
                "manufacturer": "Bose Professional",
                "models": ["ESP-880", "ESP-880A", "ESP-880AD", "ESP-1240", "ESP-1240A",
                           "ESP-1240AD", "ESP-4120", "ESP-1600"],
                "confidence": "untested",
                "notes": (
                    "TCP 10055 (changeable in Designer) or RS-232 at 115,200 baud. "
                    "Gain-sharing AMM, SmartBass, Dynamic EQ and PFS are available; "
                    "the EX-only modules are not."
                ),
            },
            {
                "manufacturer": "Bose Professional",
                "models": ["ESP-00 Series II", "ESP-00", "ESP-88"],
                "confidence": "untested",
                "notes": (
                    "Fixed port 10055 (8 connections) or RS-232 at 38,400 baud. Card-"
                    "based: Surround Input and CobraNet cards exist only here (CobraNet "
                    "cards have no serial control). Whether this generation answers the "
                    "SUB subscription command is not stated; without it the driver polls "
                    "every control each Resync interval, so lower that interval."
                ),
            },
            {
                "manufacturer": "Bose Professional",
                "models": ["CSP-428", "CSP-1248"],
                "confidence": "partial",
                "notes": (
                    "Software 2.2 or later, static IP, TCP 10055. The CSP guide documents "
                    "the module commands for listening areas only: declare '<Area> Gain' "
                    "as a Gain, '<Area> Selector' as a Source Selector and '<Area> AV' as "
                    "CSP Listening Area AutoVolume. Groups, parameter sets and the "
                    "slot commands are not documented for the CSP."
                ),
            },
        ],
        "help": {
            "overview": (
                "ControlSpace control over the Serial Control Protocol (TCP 10055, "
                "no login). Declare each module you want to control in the Modules "
                "table on the device page: its label from ControlSpace Designer and "
                "its type. Every control of every module is subscribed, so panels "
                "update the moment a value changes anywhere. Drive them with Set / "
                "Toggle / Step Control (pick the module, then the control), recall "
                "parameter sets, set group levels and mutes, join and split rooms, "
                "and use Set Module Parameter for anything the module types do not "
                "cover."
            ),
            "setup": (
                "STEP 1 - Read the labels.\n"
                "In ControlSpace Designer, every module has a label (Main Volume, "
                "Input 1, AmpLink-Ch 3). Labels must be unique on the processor: two "
                "modules with the same label answer neither. Prefix a label with # "
                "in Designer to have the processor announce changes made from a wall "
                "controller even without subscriptions.\n\n"
                "STEP 2 - Add the device.\n"
                "Enter the processor's IP address and port 10055 (or pick Direct "
                "serial and the RS-232 port: 115,200 baud, 38,400 on an ESP-00). "
                "Then add one row per module in the Modules table: the label, the "
                "module type, and a size where the type needs one (8 inputs, 8x4, "
                "5 bands). Add volume and selector groups by number, and parameter "
                "sets by number and name.\n\n"
                "STEP 3 - Test.\n"
                "Run Test Connection / Verify Modules. Every module that answers is "
                "listed; one the processor rejects has a label that does not match "
                "the loaded design.\n\n"
                "Signal level meters are off by default. Add a Signal Level row per "
                "slot and turn on 'Poll signal levels' to read them at the chosen "
                "rate."
            ),
            "connection": (
                "Port 10055 needs no login. Going online with ControlSpace Designer "
                "closes every third-party connection; the driver reconnects on its "
                "own once the design is loaded. If nothing answers, check that the "
                "serial-over-Ethernet port is enabled in the device properties."
            ),
        },
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "modules": DEFAULT_MODULES,
            "groups": DEFAULT_GROUPS,
            "parameter_sets": DEFAULT_PARAMETER_SETS,
            "room_combine_groups": 0,
            "enable_meters": False,
            "meter_interval_s": DEFAULT_METER_INTERVAL_S,
            "poll_interval": DEFAULT_POLL_INTERVAL,
            "inter_command_delay": 0,
            "baudrate": 115200,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": DEFAULT_PORT, "label": "Port",
                     "min": 1, "max": 65535,
                     "description": "The serial-over-Ethernet port. 10055 unless changed in "
                                    "ControlSpace Designer's device properties (1U ESP and EX)."},
            "baudrate": {"type": "integer", "default": 115200, "label": "Baud Rate (serial)",
                         "description": "115,200 for EX and 1U ESP processors; 38,400 for an ESP-00."},
            "modules": {
                "type": "table", "label": "Modules", "row_label": "module",
                "columns": MODULE_COLUMNS,
                "help": "One row per module to control or watch. Each becomes a child "
                        "entity named after the row.",
            },
            "groups": {
                "type": "table", "label": "Groups", "row_label": "group",
                "columns": GROUP_COLUMNS,
                "help": "Groups programmed in ControlSpace Designer: a volume group "
                        "carries a master level and mute, a selector group a source.",
            },
            "parameter_sets": {
                "type": "table", "label": "Parameter Sets", "row_label": "parameter set",
                "columns": PARAMETER_SET_COLUMNS,
                "help": "Names for the parameter sets you recall. The protocol knows "
                        "only numbers; the names feed the recall picker and the "
                        "current parameter set's name.",
            },
            "room_combine_groups": {
                "type": "integer", "default": 0, "min": 0, "max": ROOM_COMBINE_MAX,
                "label": "Room Combine Groups (EX)",
                "help": "How many Room Combine Groups the design has (0-6). Each "
                        "becomes a child entity that reports which rooms are joined.",
            },
            "enable_meters": {
                "type": "boolean", "default": False, "label": "Poll signal levels",
                "help": "Read every Signal Level row at the rate below. Off keeps the "
                        "link quiet; meters then read as unknown.",
            },
            "meter_interval_s": {
                "type": "number", "default": DEFAULT_METER_INTERVAL_S,
                "min": MIN_METER_INTERVAL_S, "max": 60, "label": "Signal level rate (s)",
                "advanced": True,
                "help": "Seconds between Get Signal Level reads (0.2 = five a second).",
            },
            "poll_interval": {
                "type": "integer", "default": DEFAULT_POLL_INTERVAL, "min": 0, "max": 3600,
                "label": "Resync interval (s)", "advanced": True,
                "help": "How often every subscription is renewed and the room combine "
                        "state re-read. On a processor without subscription support "
                        "this is how often every control is read, so set it to a few "
                        "seconds there. 0 turns it off.",
            },
            "inter_command_delay": {
                "type": "number", "default": 0, "min": 0, "max": 1, "label": "Inter-command delay (s)",
                "advanced": True,
            },
        },
        "child_entity_types": CHILD_TYPES,
        "state_variables": {
            "parameter_set": {"type": "integer", "label": "Last Parameter Set", "min": 0,
                              "max": PARAMETER_SET_MAX, "control": True,
                              "help": "The last parameter set recalled; 0 after power-up."},
            "parameter_set_name": {"type": "string", "label": "Last Parameter Set Name"},
            "parameter_set_options": {"type": "string", "label": "Parameter Set List",
                                      "help": "The Parameter Sets table as a picker list."},
            "push_supported": {"type": "boolean", "label": "Subscriptions Supported",
                               "help": "True when the processor answered SUB with yes; "
                                       "otherwise every control is polled each resync."},
            "ip_address": {"type": "string", "label": "IP Address"},
            "subnet_mask": {"type": "string", "label": "Subnet Mask"},
            "gateway": {"type": "string", "label": "Default Gateway"},
            "addressing": {"type": "enum", "values": ["dhcp", "static"], "label": "Addressing"},
            "modules_declared": {"type": "integer", "label": "Modules Declared", "min": 0},
            "modules_responding": {"type": "integer", "label": "Modules Responding", "min": 0},
            "config_problems": {"type": "string", "label": "Table Problems"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "commands": COMMANDS,
        "quick_actions": ["recall_parameter_set", "resync"],
        "actions": [
            {"id": "recall_parameter_set", "kind": "command", "icon": "bookmark"},
            {"id": "resync", "kind": "command", "icon": "refresh-cw"},
            {"id": "reboot", "kind": "command", "icon": "power",
             "confirm": "Reboot the processor? Audio stops until it is back, unsaved "
                        "settings revert to the flashed design, and this connection "
                        "drops until it returns."},
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection / Verify Modules",
                "icon": "search",
                "availability": "always",
                "confirm": (
                    "Opens a second session to the processor and asks every declared "
                    "module and group for a value. Nothing on the processor changes."
                ),
            },
        ],
    }

    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the processor stopped answering (no reply to GS)."
    )
    PROBE_TIMEOUT_S = 3.0
    ACK_TIMEOUT_S = ACK_TIMEOUT_S
    QUERY_TIMEOUT_S = QUERY_TIMEOUT_S
    SUB_TIMEOUT_S = SUB_TIMEOUT_S

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        self._problems: list[str] = []
        modules, problems = parse_modules_config(config.get("modules", DEFAULT_MODULES))
        self._modules = modules
        self._problems.extend(problems)
        groups, problems = parse_groups_config(config.get("groups", DEFAULT_GROUPS))
        self._groups = groups
        self._problems.extend(problems)
        names, problems = parse_parameter_sets_config(config.get("parameter_sets", DEFAULT_PARAMETER_SETS))
        self._parameter_set_names = names
        self._problems.extend(problems)
        try:
            self._rc_groups = max(0, min(ROOM_COMBINE_MAX, int(config.get("room_combine_groups", 0) or 0)))
        except (TypeError, ValueError):
            self._rc_groups = 0
            self._problems.append("Room Combine Groups is not a number")
        if not modules and not groups and not self._rc_groups and not self._problems:
            self._problems.append(
                "No modules declared yet: add one row per module in the Modules table "
                "on the device page"
            )
        self._by_cid: dict[str, ModuleDef] = {m.cid: m for m in modules}
        # (module label, index path) -> (cid, prop): where an inbound GA lands.
        self._route: dict[tuple[str, tuple[str, ...]], tuple[str, str]] = {}
        self._route_folded: dict[tuple[str, tuple[str, ...]], tuple[str, str]] = {}
        for m in modules:
            for path, ctl in m.by_read_path().items():
                self._route[(m.name, path)] = (m.cid, ctl.prop)
                self._route_folded[(m.name.lower(), path)] = (m.cid, ctl.prop)
        self._group_by_number: dict[int, GroupDef] = {g.number: g for g in groups}
        self._meter_slots: dict[tuple[str, str | None], ModuleDef] = {
            (m.signal_level.slot, m.signal_level.param): m for m in modules if m.signal_level
        }
        self._waiters: list[tuple[Callable[[bytes], bool], asyncio.Future[bytes]]] = []
        self._responding: set[str] = set()
        self._push_supported = False
        self._send_lock = asyncio.Lock()
        self._meter_task: asyncio.Task | None = None
        super().__init__(device_id, config, state, events)
        for problem in self._problems:
            log.warning(f"[{self.device_id}] Tables: {problem}")

    # ── Transport ──

    def _transport_kwargs(self, transport_type: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        kwargs["delimiter"] = None
        return kwargs

    def _create_frame_parser(self) -> CallableFrameParser:
        return CallableFrameParser(parse_controlspace_stream)

    # ── Lifecycle ──

    async def _initial_sync(self) -> None:
        self.set_state("modules_declared", len(self._modules))
        self.set_state("config_problems", "; ".join(self._problems))
        self.set_state("parameter_set_options", json.dumps(
            [{"value": n, "label": f"{n}: {name}"} for n, name in sorted(self._parameter_set_names.items())]
        ))
        self._responding.clear()
        self.set_state("modules_responding", 0)
        self._register_children()
        await self._detect_push_support()
        await self._read_network()
        await self._subscribe_all()
        await self._read_room_combine()
        self._start_meter_loop()

    async def poll(self) -> None:
        """Renew every subscription (a subscribe answers with the current
        value, so this is the resync and the re-arm after a reboot), or read
        every control on a processor without subscriptions; then re-read
        the room combine state, which has no subscription."""
        await self._subscribe_all()
        await self._read_room_combine()

    async def _close_session(self) -> None:
        self._stop_meter_loop()
        for _, fut in self._waiters:
            if not fut.done():
                fut.cancel()
        self._waiters.clear()

    def _register_children(self) -> None:
        for m in self._modules:
            schema = dict(_MODULE_SUMMARY_SCHEMA)
            for ctl in m.controls.values():
                schema[ctl.prop] = ctl.schema()
            try:
                self.register_child(
                    MODULE_CHILD_TYPE, m.cid, schema=schema,
                    initial_state={"name": m.name, "module_type": m.type_label,
                                   "device": m.device, "responding": False},
                )
            except (ValueError, TypeError) as exc:
                log.warning(f"[{self.device_id}] Could not register module {m.name!r}: {exc}")
        for g in self._groups:
            schema = dict(_GROUP_SUMMARY_SCHEMA)
            for ctl in g.controls.values():
                schema[ctl.prop] = ctl.schema()
            try:
                self.register_child(
                    GROUP_CHILD_TYPE, g.number, schema=schema,
                    initial_state={"name": g.name, "kind": g.kind, "responding": False},
                )
            except (ValueError, TypeError) as exc:
                log.warning(f"[{self.device_id}] Could not register group {g.number}: {exc}")
        for n in range(1, self._rc_groups + 1):
            try:
                self.register_child(ROOM_COMBINE_CHILD_TYPE, n, initial_state={"responding": False})
            except (ValueError, TypeError) as exc:
                log.warning(f"[{self.device_id}] Could not register room combine group {n}: {exc}")

    async def refresh_children(self) -> dict[str, Any]:
        self._register_children()
        await self._subscribe_all()
        await self._read_room_combine()
        return {"modules": len(self._modules), "responding": len(self._responding),
                "groups": len(self._groups), "room_combine_groups": self._rc_groups}

    # ── Sending ──

    async def _send_line(self, text: str) -> None:
        """One CR-terminated line. The transport itself paces sends by the
        device's inter_command_delay."""
        if not self.transport:
            raise ConnectionError("Not connected")
        await self.transport.send((text + "\r").encode("ascii", errors="replace"))

    def _wait_for(self, pred: Callable[[bytes], bool]) -> asyncio.Future[bytes]:
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._waiters.append((pred, fut))
        return fut

    def _drop_waiter(self, fut: asyncio.Future[bytes]) -> None:
        self._waiters = [(p, f) for p, f in self._waiters if f is not fut]

    @staticmethod
    def _is_ack_or_nak(frame: bytes) -> bool:
        return bool(frame) and frame[0] in (ACK, NAK)

    @staticmethod
    def _nak_message(frame: bytes) -> str:
        code = frame[1:].decode("ascii", errors="replace").strip()
        return NAK_CODES.get(code, f"the processor rejected the command (NAK {code or '?'})")

    async def _module_write(self, line: str) -> None:
        """Send a module command (SA / MA) and await its ACK or NAK. A NAK
        becomes the error the user sees; silence is tolerated (the document
        promises an acknowledgement, but nothing else depends on it)."""
        async with self._send_lock:
            fut = self._wait_for(self._is_ack_or_nak)
            try:
                await self._send_line(line)
                frame = await asyncio.wait_for(fut, self.ACK_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.debug(f"[{self.device_id}] No acknowledgement for {line!r}")
                return
            finally:
                self._drop_waiter(fut)
        if frame[0] == NAK:
            message = self._nak_message(frame)
            self.set_state("last_error", f"{line}: {message}")
            raise ValueError(message)

    async def _query(self, line: str, pred: Callable[[bytes], bool],
                     timeout: float | None = None) -> bytes | None:
        """Send a query and await the reply that satisfies ``pred`` (or a
        NAK). Returns None on silence. The reply also routes to state on its
        own through on_data_received."""
        async with self._send_lock:
            fut = self._wait_for(lambda f: pred(f) or (bool(f) and f[0] == NAK))
            try:
                await self._send_line(line)
                frame = await asyncio.wait_for(fut, self.QUERY_TIMEOUT_S if timeout is None else timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                self._drop_waiter(fut)
        if frame and frame[0] == NAK:
            raise ValueError(self._nak_message(frame))
        return frame

    async def _fire(self, line: str) -> None:
        """A system or device command: the processor answers nothing."""
        async with self._send_lock:
            await self._send_line(line)

    # ── Subscriptions and reads ──

    async def _detect_push_support(self) -> None:
        reply = None
        try:
            reply = await self._query("SUB", lambda f: bool(_SUB_SUPPORT_RE.match(f.decode("ascii", "replace"))))
        except ValueError:
            reply = None
        supported = bool(reply) and reply.decode("ascii", "replace").strip().lower().endswith("yes")
        self._push_supported = supported
        self.set_state("push_supported", supported)
        if not supported:
            log.info(f"[{self.device_id}] The processor did not answer SUB yes: polling every "
                     f"control each resync interval instead of subscribing")

    async def _read_network(self) -> None:
        """Identity reads, fire-and-forget: the replies route to state as they
        arrive, and an ESP-00 (no NP T) simply leaves that one unknown."""
        for line in ("IP", "NP T", "NP M", "NP G"):
            await self._fire(line)

    def _get_texts(self) -> list[tuple[str, str, str | None]]:
        """Every subscribable GET as (module cid or '', GET text, prop)."""
        out: list[tuple[str, str, str | None]] = []
        for m in self._modules:
            if m.signal_level is not None:
                continue
            for ctl in m.controls.values():
                if ctl.subscribe and ctl.idx:
                    out.append((m.cid, ga_line(m.name, ctl.read_path, m.device), ctl.prop))
        return out

    async def _subscribe_all(self) -> None:
        if not self._push_supported:
            await self._read_all()
            return
        for cid, text, _ in self._get_texts():
            await self._sub(text, cid)
        for g in self._groups:
            await self._sub(f"GG {g.number:x}", None)
            if g.kind == "level":
                await self._sub(f"GN {g.number:x}", None)
        await self._sub("GS", None)

    async def _sub(self, get_text: str, cid: str | None) -> bool:
        def matched(frame: bytes) -> bool:
            m = _SUB_RE.match(frame.decode("ascii", "replace"))
            return bool(m) and m.group(2) == get_text
        try:
            reply = await self._query(sub_line(get_text), matched, self.SUB_TIMEOUT_S)
        except ValueError as exc:
            log.warning(f"[{self.device_id}] Subscribe refused for {get_text}: {exc}")
            return False
        if reply is None:
            log.debug(f"[{self.device_id}] No answer to SUB {get_text!r}")
            return False
        ok = reply.decode("ascii", "replace").strip().lower().endswith("yes")
        if not ok:
            log.warning(f"[{self.device_id}] Subscription not accepted for {get_text}")
        return ok

    async def _read_all(self) -> None:
        """Polling mode: one GET per control. A module whose first control
        stays silent is skipped for the rest of the cycle (a wrong label costs
        one timeout, not one per control)."""
        skipped: set[str] = set()
        for cid, text, _ in self._get_texts():
            if cid in skipped:
                continue
            try:
                reply = await self._query(text, lambda f, t=text: self._ga_matches(f, t))
            except ValueError as exc:
                log.warning(f"[{self.device_id}] {text}: {exc}")
                skipped.add(cid)
                continue
            if reply is None and cid not in self._responding:
                skipped.add(cid)
        for g in self._groups:
            await self._query(f"GG {g.number:x}", lambda f, n=g.number: self._group_reply(f, "GG", n))
            if g.kind == "level":
                await self._query(f"GN {g.number:x}", lambda f, n=g.number: self._group_reply(f, "GN", n))
        await self._query("GS", lambda f: bool(_S_RE.match(f.decode("ascii", "replace"))))

    @staticmethod
    def _ga_matches(frame: bytes, ga_text: str) -> bool:
        sent = parse_ga_reply(ga_text + "=")
        got = parse_ga_reply(frame.decode("ascii", "replace"))
        return bool(sent and got and sent.name.lower() == got.name.lower() and sent.idx == got.idx)

    @staticmethod
    def _group_reply(frame: bytes, kind: str, number: int) -> bool:
        m = (_GG_RE if kind == "GG" else _GN_RE).match(frame.decode("ascii", "replace"))
        return bool(m) and int(m.group(1), 16) == number

    async def _read_room_combine(self) -> None:
        for n in range(1, self._rc_groups + 1):
            try:
                await self._query(f"GRC {n}", lambda f, n=n: self._grc_matches(f, n))
            except ValueError as exc:
                log.warning(f"[{self.device_id}] GRC {n}: {exc}")

    @staticmethod
    def _grc_matches(frame: bytes, number: int) -> bool:
        parsed = parse_grc_joined(frame.decode("ascii", "replace"))
        return bool(parsed) and parsed[0] == str(number)

    async def _read_back(self, m: ModuleDef, ctl: ControlDef) -> None:
        """The document's own advice: follow a Set with a Get. Routing goes
        through the mask (routing A) that reports the whole input."""
        if ctl.fmt == FMT_ONOFF and not ctl.subscribe and m.type_id == "standard_mixer":
            i = ctl.prop.split("_")[1]
            mask = m.controls.get(f"input_{i}_routing")
            if mask is not None:
                await self._query(ga_line(m.name, mask.read_path, m.device),
                                  lambda f, t=ga_line(m.name, mask.read_path, m.device): self._ga_matches(f, t))
            return
        if not ctl.subscribe and ctl.fmt == FMT_ONOFF:
            return  # a write-only action (PFS GUI parameters)
        if not ctl.idx:
            return
        text = ga_line(m.name, ctl.read_path, m.device)
        await self._query(text, lambda f, t=text: self._ga_matches(f, t))

    # ── Meters ──

    def _meters_enabled(self) -> bool:
        return bool(self.config.get("enable_meters", False)) and bool(self._meter_slots)

    def _meter_interval(self) -> float:
        try:
            v = float(self.config.get("meter_interval_s", DEFAULT_METER_INTERVAL_S))
        except (TypeError, ValueError):
            v = DEFAULT_METER_INTERVAL_S
        return max(MIN_METER_INTERVAL_S, v)

    def _start_meter_loop(self) -> None:
        self._stop_meter_loop()
        if not self._meters_enabled():
            return
        self._meter_task = asyncio.ensure_future(self._meter_loop())

    def _stop_meter_loop(self) -> None:
        task = self._meter_task
        self._meter_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _meter_loop(self) -> None:
        try:
            while self._link_alive():
                await self.read_meters()
                await asyncio.sleep(self._meter_interval())
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug(f"[{self.device_id}] Meter loop stopped", exc_info=True)

    async def read_meters(self) -> int:
        """One Get Signal Level per declared slot; returns how many answered."""
        answered = 0
        for (slot, param), m in list(self._meter_slots.items()):
            spec = m.signal_level
            if spec is None:
                continue
            try:
                reply = await self._query(spec.query, lambda f, s=slot, p=param: self._gl_matches(f, s, p))
            except ValueError as exc:
                log.warning(f"[{self.device_id}] {spec.query}: {exc}")
                continue
            if reply is not None:
                answered += 1
        return answered

    @staticmethod
    def _gl_matches(frame: bytes, slot: str, param: str | None) -> bool:
        m = _GL_RE.match(frame.decode("ascii", "replace"))
        if not m:
            return False
        got_param = m.group(2).lower() if m.group(2) else None
        return m.group(1).lower() == slot and got_param == param

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        if not data:
            return
        if data[0] in (ACK, NAK):
            if not self._dispatch_waiters(data) and data[0] == NAK:
                self.set_state("last_error", self._nak_message(data))
            return
        text = data.decode("ascii", errors="replace").strip()
        if not text:
            return
        self._dispatch_waiters(data)
        self._route_line(text)

    def _dispatch_waiters(self, frame: bytes) -> bool:
        for pred, fut in list(self._waiters):
            if fut.done():
                self._drop_waiter(fut)
                continue
            try:
                hit = pred(frame)
            except Exception:
                hit = False
            if hit:
                fut.set_result(frame)
                self._drop_waiter(fut)
                return True
        return False

    def _route_line(self, text: str) -> None:
        reply = parse_ga_reply(text)
        if reply is not None:
            self._apply_module_reply(reply)
            return
        m = _S_RE.match(text)
        if m:
            n = int(m.group(1), 16)
            self.set_states({"parameter_set": n,
                             "parameter_set_name": self._parameter_set_names.get(n, "" if n == 0 else f"Parameter Set {n}")})
            return
        m = _GG_RE.match(text)
        if m:
            self._apply_group_value(int(m.group(1), 16), int(m.group(2), 16))
            return
        m = _GN_RE.match(text)
        if m:
            self._apply_group_mute(int(m.group(1), 16), m.group(2).upper() == "M")
            return
        parsed = parse_grc_joined(text)
        if parsed is not None:
            self._apply_room_combine(parsed[0], parsed[1])
            return
        m = _GL_RE.match(text)
        if m:
            self._apply_signal_levels(m.group(1).lower(), m.group(2).lower() if m.group(2) else None,
                                      m.group(3))
            return
        m = _IP_RE.match(text)
        if m:
            self.set_state("ip_address", m.group(1))
            return
        m = _NP_RE.match(text)
        if m:
            key, value = m.group(1).upper(), m.group(2).strip()
            if key == "T":
                self.set_state("addressing", "dhcp" if value.upper().startswith("D") else "static")
            elif key == "M" and _IPV4_RE.match(value):
                self.set_state("subnet_mask", value)
            elif key == "G" and _IPV4_RE.match(value):
                self.set_state("gateway", value)
            return
        if _SUB_RE.match(text) or _SUB_SUPPORT_RE.match(text) or _GV_RE.match(text) or _GM_RE.match(text):
            return
        if text.strip().lower() == "ready":
            log.info(f"[{self.device_id}] The processor reports Ready (rebooted); the next resync re-arms it")
            return
        log.debug(f"[{self.device_id}] Unhandled line: {text!r}")

    def _apply_module_reply(self, reply: ModuleReply) -> None:
        route = self._route.get((reply.name, reply.idx)) or self._route_folded.get((reply.name.lower(), reply.idx))
        if route is None:
            log.debug(f"[{self.device_id}] GA for an undeclared control {reply.name!r} {'>'.join(reply.idx)}")
            return
        cid, prop = route
        m = self._by_cid.get(cid)
        if m is None:
            return
        ctl = m.controls[prop]
        try:
            value = decode_value(ctl, reply.value)
        except ValueError:
            log.warning(f"[{self.device_id}] Could not read {reply.value!r} as {ctl.label} on {m.name}")
            return
        updates: dict[str, Any] = {}
        if ctl.fmt == FMT_ROUTING:
            updates[prop] = value
            i = prop.split("_")[1]
            try:
                for o, on in routing_mask_to_outputs(value, ctl.fanout).items():
                    if f"xp_{i}_{o}" in m.controls:
                        updates[f"xp_{i}_{o}"] = on
            except ValueError:
                log.warning(f"[{self.device_id}] Routing mask {value!r} on {m.name} is not hexadecimal")
        elif value is None:
            return
        else:
            updates[prop] = value
        self._mark_responding(cid, updates)
        try:
            self.set_child_state_batch(MODULE_CHILD_TYPE, cid, updates)
        except ValueError as exc:
            log.warning(f"[{self.device_id}] Could not store {cid}.{prop}: {exc}")

    def _mark_responding(self, cid: str, updates: dict[str, Any]) -> None:
        if cid not in self._responding:
            self._responding.add(cid)
            updates["responding"] = True
            self.set_state("modules_responding", len(self._responding))

    def _apply_group_value(self, number: int, raw: int) -> None:
        g = self._group_by_number.get(number)
        if g is None:
            return
        if g.kind == "selector":
            updates: dict[str, Any] = {"source": raw}
        else:
            updates = {"level_db": hex_level_to_db(raw)}
        updates["responding"] = True
        self.set_child_state_batch(GROUP_CHILD_TYPE, number, updates)

    def _apply_group_mute(self, number: int, muted: bool) -> None:
        g = self._group_by_number.get(number)
        if g is None or g.kind != "level":
            return
        self.set_child_state_batch(GROUP_CHILD_TYPE, number, {"mute": muted, "responding": True})

    def _apply_room_combine(self, ref: str, joined: list[set[int]]) -> None:
        try:
            n = int(ref)
        except ValueError:
            return
        if not 1 <= n <= self._rc_groups:
            return
        ordered = sorted((sorted(grp) for grp in joined), key=lambda g: g[0])
        updates: dict[str, Any] = {
            "joined": "".join("[" + ",".join(str(r) for r in grp) + "]" for grp in ordered),
            "responding": True,
        }
        for a, b in _room_pairs():
            updates[f"joined_{a}_{b}"] = any(a in grp and b in grp for grp in joined)
        self.set_child_state_batch(ROOM_COMBINE_CHILD_TYPE, n, updates)

    def _apply_signal_levels(self, slot: str, param: str | None, body: str) -> None:
        m = self._meter_slots.get((slot, param))
        if m is None or m.signal_level is None:
            return
        try:
            raws = [int(tok.strip(), 16) for tok in body.split(",") if tok.strip()]
        except ValueError:
            log.warning(f"[{self.device_id}] Signal levels for slot {slot} are not hexadecimal: {body!r}")
            return
        if not raws:
            return
        if len(m.controls) < len(raws):
            # The channel count was learned from the reply: give the child
            # its full meter set.
            m.controls = {c.prop: c for c in
                          [_meter_control(c, m.signal_level.floor_db) for c in range(1, len(raws) + 1)]}
            m.signal_level.channels = len(raws)
            self.deregister_child(MODULE_CHILD_TYPE, m.cid)
            schema = dict(_MODULE_SUMMARY_SCHEMA)
            for ctl in m.controls.values():
                schema[ctl.prop] = ctl.schema()
            self.register_child(MODULE_CHILD_TYPE, m.cid, schema=schema,
                                initial_state={"name": m.name, "module_type": m.type_label,
                                               "device": m.device, "responding": False})
        floor = m.signal_level.floor_db
        updates: dict[str, Any] = {f"level_{c}": meter_to_db(raw, floor)
                                   for c, raw in enumerate(raws, 1) if f"level_{c}" in m.controls}
        self._mark_responding(m.cid, updates)
        self.set_child_state_batch(MODULE_CHILD_TYPE, m.cid, updates)

    # ── Liveness ──

    async def _liveness_probe(self) -> None:
        """``GS`` answers ``S n`` on every processor; silence twice drops the link."""
        reply = await self._query("GS", lambda f: bool(_S_RE.match(f.decode("ascii", "replace"))),
                                  self.PROBE_TIMEOUT_S)
        if reply is None:
            raise TimeoutError("no reply to GS")

    # ── Commands ──

    def _lookup(self, params: dict[str, Any]) -> tuple[ModuleDef, ControlDef]:
        cid = str(params.get("module") or "").strip()
        m = self._by_cid.get(cid) or self._by_cid.get(safe_child_id(cid))
        if m is None:
            raise ValueError(f"'{cid}' is not one of the declared modules")
        name = str(params.get("control") or "").strip()
        ctl = m.controls.get(name)
        if ctl is None:
            lowered = name.lower()
            for c in m.controls.values():
                if c.label.lower() == lowered or c.prop.lower() == lowered:
                    ctl = c
                    break
        if ctl is None:
            raise ValueError(f"{m.name} has no control named '{name}'")
        return m, ctl

    def _lookup_module(self, params: dict[str, Any]) -> ModuleDef:
        cid = str(params.get("module") or "").strip()
        m = self._by_cid.get(cid) or self._by_cid.get(safe_child_id(cid))
        if m is None:
            raise ValueError(f"'{cid}' is not one of the declared modules")
        return m

    def _lookup_group(self, params: dict[str, Any]) -> GroupDef:
        try:
            number = int(params.get("group"))
        except (TypeError, ValueError) as exc:
            raise ValueError("pick one of the declared groups") from exc
        g = self._group_by_number.get(number)
        if g is None:
            raise ValueError(f"group {number} is not in the Groups table")
        return g

    def _current(self, m: ModuleDef, prop: str) -> Any:
        return self.get_child_state(MODULE_CHILD_TYPE, m.cid).get(prop)

    async def _set_module_control(self, m: ModuleDef, ctl: ControlDef, wire: str) -> None:
        if not ctl.writable:
            raise ValueError(f"{ctl.label} on {m.name} is read-only")
        await self._module_write(sa_line(m.name, ctl.write_path, wire, m.device))
        await self._read_back(m, ctl)

    async def _call_action(self, params: dict[str, Any], action: str, parameter: str | None) -> None:
        m = self._lookup_module(params)
        if m.type_id not in CALL_MODULE_TYPES:
            raise ValueError(f"{m.name} is not a PSTN or VoIP input module")
        if action == "transfer_call" and m.type_id != "voip_input":
            raise ValueError("only a VoIP line can transfer a call")
        await self._module_write(ma_line(m.name, CALL_ACTIONS[action], parameter, m.device))
        # The call status and Call Active flag are read-only reports; ask for
        # them so a panel sees the new state without waiting for a push.
        for prop in ("call_status", "call_active", "caller_id"):
            ctl = m.controls.get(prop)
            if ctl is not None:
                await self._read_back(m, ctl)

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if command == "recall_parameter_set":
            n = int(params["number"])
            if not 1 <= n <= PARAMETER_SET_MAX:
                raise ValueError("a parameter set is 1..255")
            await self._fire(f"SS {n:x}")
            await self._query("GS", lambda f: bool(_S_RE.match(f.decode("ascii", "replace"))))
            return None
        if command == "set_control":
            m, ctl = self._lookup(params)
            await self._set_module_control(m, ctl, encode_value(ctl, params.get("value")))
            return None
        if command == "toggle_control":
            m, ctl = self._lookup(params)
            if ctl.fmt not in (FMT_ONOFF, FMT_LOGIC):
                raise ValueError(f"{ctl.label} on {m.name} is not an on/off control")
            await self._set_module_control(m, ctl, "T")
            return None
        if command == "pulse_control":
            m, ctl = self._lookup(params)
            if ctl.fmt != FMT_LOGIC:
                raise ValueError(f"{ctl.label} on {m.name} is not a logic pin")
            await self._set_module_control(m, ctl, "P")
            return None
        if command == "step_level":
            m, ctl = self._lookup(params)
            if ctl.fmt != FMT_LEVEL:
                raise ValueError(f"{ctl.label} on {m.name} is not a level")
            current = self._current(m, ctl.prop)
            if current is None:
                raise ValueError(f"{m.name} {ctl.label} has not reported a value yet")
            lo = LEVEL_MIN if ctl.min is None else ctl.min
            hi = LEVEL_MAX if ctl.max is None else ctl.max
            step = ctl.step or 0.5
            target = max(lo, min(hi, float(current) + float(params.get("amount", 1.0))))
            target = round(round(target / step) * step, 3)
            await self._set_module_control(m, ctl, format_number(target))
            return None
        if command == "make_call":
            await self._call_action(params, command, str(params["number"]).strip())
            return None
        if command == "answer_call":
            await self._call_action(params, command, None)
            return None
        if command == "end_call":
            await self._call_action(params, command, None)
            return None
        if command == "dial_key":
            await self._call_action(params, command, str(params["key"]).strip())
            return None
        if command == "transfer_call":
            await self._call_action(params, command, str(params["number"]).strip())
            return None
        if command == "set_group_level":
            g = self._lookup_group(params)
            if g.kind != "level":
                raise ValueError(f"{g.name} is a selector group; use Set Group Source")
            await self._fire(f"SG {g.number:x},{db_to_hex_level(float(params['level']))}")
            await self._query(f"GG {g.number:x}", lambda f, n=g.number: self._group_reply(f, "GG", n))
            return None
        if command == "step_group_level":
            g = self._lookup_group(params)
            if g.kind != "level":
                raise ValueError(f"{g.name} is a selector group")
            amount = float(params.get("amount", 1.0))
            steps = int(round(abs(amount) * 2))
            if steps == 0:
                return None
            await self._fire(f"SH {g.number:x},{1 if amount > 0 else 0},{steps:x}")
            await self._query(f"GG {g.number:x}", lambda f, n=g.number: self._group_reply(f, "GG", n))
            return None
        if command == "set_group_mute":
            g = self._lookup_group(params)
            if g.kind != "level":
                raise ValueError(f"{g.name} is a selector group")
            await self._fire(f"SN {g.number:x},{'M' if coerce_onoff(params.get('mute')) else 'U'}")
            await self._query(f"GN {g.number:x}", lambda f, n=g.number: self._group_reply(f, "GN", n))
            return None
        if command == "toggle_group_mute":
            g = self._lookup_group(params)
            if g.kind != "level":
                raise ValueError(f"{g.name} is a selector group")
            await self._fire(f"SN {g.number:x},T")
            await self._query(f"GN {g.number:x}", lambda f, n=g.number: self._group_reply(f, "GN", n))
            return None
        if command == "set_group_source":
            g = self._lookup_group(params)
            if g.kind != "selector":
                raise ValueError(f"{g.name} is a volume group; use Set Group Level")
            channel = int(params["channel"])
            if not 1 <= channel <= 32:
                raise ValueError("a source selector group takes channels 1..32")
            await self._fire(f"SG {g.number:x},{channel:x}")
            await self._query(f"GG {g.number:x}", lambda f, n=g.number: self._group_reply(f, "GG", n))
            return None
        if command in ("join_rooms", "split_rooms"):
            n = int(params["group"])
            if not 1 <= n <= self._rc_groups:
                raise ValueError(f"room combine group {n} is not declared (Room Combine Groups is {self._rc_groups})")
            a, b = int(params["room_a"]), int(params["room_b"])
            if a == b:
                raise ValueError("pick two different rooms")
            await self._fire(f"SRC {n},{a},{b},{'J' if command == 'join_rooms' else 'S'}")
            await self._query(f"GRC {n}", lambda f, n=n: self._grc_matches(f, n))
            return None
        if command == "set_io_level":
            slot, ch = self._slot_channel(params)
            await self._fire(f"SV {slot},{ch},{db_to_hex_level(float(params['level']))}")
            return await self._io_read_back(f"GV {slot},{ch}", _GV_RE)
        if command == "step_io_level":
            slot, ch = self._slot_channel(params)
            amount = float(params.get("amount", 1.0))
            steps = int(round(abs(amount) * 2))
            if steps == 0:
                return None
            await self._fire(f"SI {slot},{ch},{1 if amount > 0 else 0},{steps:x}")
            return await self._io_read_back(f"GV {slot},{ch}", _GV_RE)
        if command == "set_io_mute":
            slot, ch = self._slot_channel(params)
            state = str(params["state"]).strip().upper()[:1]
            if state not in ("M", "U", "T"):
                raise ValueError("state is M, U or T")
            await self._fire(f"SM {slot},{ch},{state}")
            return await self._io_read_back(f"GM {slot},{ch}", _GM_RE)
        if command == "set_module_parameter":
            name, path, device = self._raw_module_params(params)
            value = str(params.get("value", "")).strip()
            if '"' in value:
                raise ValueError("a value cannot contain a double quote")
            await self._module_write(sa_line(name, path, value, device))
            text = ga_line(name, path, device)
            await self._query(text, lambda f, t=text: self._ga_matches(f, t))
            return None
        if command == "query_module_parameter":
            name, path, device = self._raw_module_params(params)
            text = ga_line(name, path, device)
            reply = await self._query(text, lambda f, t=text: self._ga_matches(f, t))
            if reply is None:
                raise ValueError(f"no reply to {text}")
            parsed = parse_ga_reply(reply.decode("ascii", "replace"))
            return parsed.value if parsed else reply.decode("ascii", "replace")
        if command == "invoke_module_action":
            name, _, device = self._raw_module_params({**params, "index": str(params.get("index", 1))})
            parameter = str(params.get("parameter") or "").strip() or None
            await self._module_write(ma_line(name, str(int(params["index"])), parameter, device))
            return None
        if command == "set_ip_address":
            address = str(params["address"]).strip()
            if not _IPV4_RE.match(address):
                raise ValueError("enter a dotted IPv4 address")
            await self._fire(f"IP {address}")
            return None
        if command == "set_network_parameter":
            key = str(params["parameter"]).strip().upper()[:1]
            value = str(params["value"]).strip()
            if key == "T":
                value = value.upper()[:1]
                if value not in ("D", "S"):
                    raise ValueError("addressing is D (DHCP) or S (static)")
            elif key in ("M", "G"):
                if not _IPV4_RE.match(value):
                    raise ValueError("enter a dotted IPv4 address")
            else:
                raise ValueError("parameter is T, M or G")
            await self._fire(f"NP {key},{value}")
            return None
        if command == "reset_network_defaults":
            await self._fire("NP F")
            return None
        if command == "reboot":
            await self._fire("RESET")
            return None
        if command == "resync":
            await self._subscribe_all()
            await self._read_room_combine()
            return None
        raise ValueError(f"Unknown command: {command}")

    async def _io_read_back(self, query: str, pattern: re.Pattern[str]) -> dict[str, Any]:
        """The slot commands have no child to land on: the reply is the
        command's result (level in dB, or the mute state)."""
        reply = await self._query(query, lambda f: bool(pattern.match(f.decode("ascii", "replace"))))
        if reply is None:
            return {"reply": None}
        text = reply.decode("ascii", "replace").strip()
        m = pattern.match(text)
        if pattern is _GV_RE and m:
            return {"reply": text, "level_db": hex_level_to_db(int(m.group(3), 16))}
        if pattern is _GM_RE and m:
            return {"reply": text, "muted": m.group(3).upper() == "M"}
        return {"reply": text}

    @staticmethod
    def _slot_channel(params: dict[str, Any]) -> tuple[str, str]:
        slot = str(params.get("slot", "")).strip().lower()
        ch = str(params.get("channel", "")).strip().lower()
        if not _HEX_RE.match(slot) or not _HEX_RE.match(ch):
            raise ValueError("slot and channel are hexadecimal (1-B, 1-40)")
        return slot, ch

    @staticmethod
    def _raw_module_params(params: dict[str, Any]) -> tuple[str, str, str]:
        name = str(params.get("module_name", "")).strip()
        path = re.sub(r"\s+", "", str(params.get("index", "")))
        device = str(params.get("device") or "").strip()
        if not name or '"' in name or '"' in device:
            raise ValueError("enter the module label without quotes")
        if not path or not re.match(r"^[0-9(),>]+$", path):
            raise ValueError("the index path is digits separated by >, e.g. 1 or 0>3 or 4>(2,5)")
        return name, path, device

    # ── Test Connection / Verify Modules (setup wizard) ──

    async def run_setup_action(self, action_id: str, params: dict[str, Any], progress: Any) -> dict[str, Any]:
        """Open a session of its own (a processor takes 8 to 32 clients), ask
        for subscription support, the last parameter set, and the first
        control of every declared module and each group, and report which
        answered. A NAK names the reason (a label that is not in the loaded
        design is the commissioning failure this protocol can show)."""
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", DEFAULT_PORT))
        if not host:
            raise ValueError("No IP address configured yet.")
        if self._problems:
            await progress("Tables have problems: " + "; ".join(self._problems), 5)
        await progress(f"Connecting to {host}:{port}", 10)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10.0)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach {host}:{port}: check the address, and that serial-over-Ethernet "
                f"is enabled on the processor ({exc})"
            ) from exc
        session = _WizardSession(reader, writer, self.PROBE_TIMEOUT_S)
        answered: list[str] = []
        silent: list[str] = []
        rejected: list[str] = []
        try:
            sub = await session.ask_or_none("SUB", lambda t: bool(_SUB_SUPPORT_RE.match(t)))
            push = bool(sub) and sub.strip().lower().endswith("yes")
            gs = await session.ask_or_none("GS", lambda t: bool(_S_RE.match(t)))
            ip = await session.ask_or_none("IP", lambda t: bool(_IP_RE.match(t)))
            probes = [m for m in self._modules if m.signal_level is None and any(c.idx for c in m.controls.values())]
            await progress(f"Asking {len(probes)} module(s) and {len(self._groups)} group(s) for a value", 30)
            for i, m in enumerate(probes):
                ctl = next(c for c in m.controls.values() if c.idx)
                text = ga_line(m.name, ctl.read_path, m.device)
                try:
                    reply = await session.ask(text, lambda t, x=text: self._ga_matches(t.encode(), x))
                except ValueError as exc:
                    rejected.append(f"{m.name} ({exc})")
                    continue
                (answered if reply else silent).append(m.name)
                if i % 5 == 4:
                    await progress(f"{i + 1} of {len(probes)} modules asked", 30 + int(50 * (i + 1) / len(probes)))
            for m in (x for x in self._modules if x.signal_level is not None):
                spec = m.signal_level
                reply = await session.ask(spec.query, lambda t, s=spec: bool(_GL_RE.match(t)) and self._gl_matches(t.encode(), s.slot, s.param))
                (answered if reply else silent).append(f"{m.name} ({spec.query})")
            for g in self._groups:
                reply = await session.ask(f"GG {g.number:x}", lambda t, n=g.number: self._group_reply(t.encode(), "GG", n))
                (answered if reply else silent).append(f"{g.name} (group {g.number})")
        finally:
            await session.close()
        total = len(answered) + len(silent) + len(rejected)
        if gs is None and ip is None and total and not answered:
            message = (f"Connected to {host}:{port}, but nothing answered: check that this is a "
                       f"ControlSpace processor and that serial-over-Ethernet is enabled.")
        elif total == 0:
            message = (f"Connected to {host}:{port}"
                       + (f", address {ip.split()[1]}" if ip else "")
                       + ". No modules or groups are declared yet, so nothing was verified.")
        elif not silent and not rejected:
            message = f"All {total} module(s) and group(s) answered."
        else:
            parts = [f"{len(answered)} of {total} answered."]
            if rejected:
                parts.append("Rejected: " + "; ".join(rejected) + ".")
            if silent:
                parts.append("Silent: " + ", ".join(silent) + ".")
            message = " ".join(parts)
        if total and not push:
            message += " The processor did not answer SUB yes, so controls will be polled each resync interval."
        await progress(message, 100)
        return {
            "ok": bool(total) and not silent and not rejected,
            "message": message,
            "answered": answered,
            "silent": silent,
            "rejected": rejected,
            "push_supported": push,
            "parameter_set": int(_S_RE.match(gs).group(1), 16) if gs and _S_RE.match(gs) else None,
            "ip_address": ip.split()[1] if ip else None,
            "problems": list(self._problems),
        }


class _WizardSession:
    """A throwaway socket for Test Connection, framed by the driver's parser."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout: float) -> None:
        self.reader = reader
        self.writer = writer
        self.timeout = timeout
        self.buf = b""

    async def ask(self, line: str, matched: Callable[[str], bool]) -> str | None:
        """Send a line and wait for a frame ``matched`` accepts; a NAK raises,
        silence returns None. Unrelated frames are dropped."""
        self.writer.write((line + "\r").encode("ascii", "replace"))
        await self.writer.drain()
        deadline = asyncio.get_running_loop().time() + self.timeout
        while True:
            while True:
                frame, self.buf = parse_controlspace_stream(self.buf)
                if frame is None:
                    break
                if not frame:
                    if not self.buf:
                        break
                    continue
                if frame[0] == NAK:
                    raise ValueError(BoseControlSpaceDriver._nak_message(frame))
                if frame[0] == ACK:
                    continue
                text = frame.decode("ascii", "replace").strip()
                if matched(text):
                    return text
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                chunk = await asyncio.wait_for(self.reader.read(4096), remaining)
            except asyncio.TimeoutError:
                return None
            if not chunk:
                return None
            self.buf += chunk

    async def ask_or_none(self, line: str, matched: Callable[[str], bool]) -> str | None:
        """``ask`` for a query the processor may refuse (an ESP-00 and SUB)."""
        try:
            return await self.ask(line, matched)
        except ValueError:
            return None

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
