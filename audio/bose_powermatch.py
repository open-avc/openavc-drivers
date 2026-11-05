"""
Bose Professional PowerMatch PM8500N / PM8250N / PM4500N / PM4250N —
ControlSpace Serial Control Protocol driver.

Controls a networked PowerMatch amplifier through the ControlSpace Serial
Control Protocol on its own address at TCP 10055 (fixed; up to 32
connections, section 2). A PM8xxxN has eight inputs (A-H) and eight output
channels, a PM4xxxN four of each. The protocol reaches them on pseudo slots
(Table 2): inputs A-D on slot 1 and E-H on slot 3, outputs 1-4 on slot 2 and
5-8 on slot 4, channels 1-4 on each; Get Signal Level reads all eight inputs
on slot 1 and all eight outputs on slot 2. ``GC`` reports each channel's
output configuration, and how many channels the amplifier has.

What it controls:
    Standby (``SY`` / ``GY``), each output's level and mute and each input's
    mute by slot (``SV`` / ``GV`` / ``SI`` / ``SM`` / ``GM``: a PowerMatch has
    volume on its outputs only), the "Matrix 1" mixer (every input and the
    signal generator into every output, on / off and level), each output's
    polarity (the speaker EQ, a fixed label), limiter (with its link group),
    delay and align delay, the signal generator (level, mute, type, tone and
    sweep), the input, array and speaker EQs and the band-pass filter by
    command, each input's analog / digital source and analog sensitivity (the
    Input module, whose label the integrator types), parameter sets, level
    groups, Get Signal Level for the inputs (dBFS) and outputs (dBV max), the
    output configuration (``GC``), the fault output and alarm reports (``GF``
    / ``SF``, ``GR`` / ``SR``, ``CF``), the alarm log (``GH`` / ``CH``) and the
    network settings.

Why Python:
    Every system and device command carries hexadecimal numbers (a level of
    5Bh is -14.5 dB, parameter set 11 is ``b``, the signal-level arrays are
    hex), and a YAML driver reads a captured number in base 10 only. On top
    of that: the module commands' ACK / NAK bytes are not line-terminated and
    a NAK's code is the error the user should see; the subscribe handshake
    needs a capability check with a polled fallback; one Get Signal Level
    reply fans out into eight children; eight channels sit on four slots;
    unsolicited alarm lines carry a channel, a severity, a type and a
    condition that go to different children and, when one clears, a read of
    what is left; the alarm log arrives as a bracketed block over several
    lines; the output configuration sizes the roster; and a matrix
    cross-point's index is computed from its input and output.

Push, with a read-back after every write:
    ``SUB "<GET command>"`` makes the amplifier send the value at once and
    again whenever it changes (section 9: parameter set, group levels and
    mutes, slot levels and mutes, any module parameter). On connect the
    driver asks ``SUB`` alone; an amplifier answering ``SUB yes`` gets every
    output level and mute, every input mute, the matrix, polarity, the
    signal generator, each labelled input's source, the parameter set and
    the groups subscribed, renewed every Resync interval; a value it will not
    subscribe is read every poll instead. One that does not answer ``SUB
    yes`` is read every Poll interval (the matrix levels every Resync
    interval), and ``push_supported`` says which. ``SF O`` and ``SR O`` turn
    on the amplifier's own fault and alarm reports (5.10, 5.12; "not retained
    on power down", so they are sent again every Resync interval). Limiting
    and clipping arrive only that way, once each until the alarms are cleared
    (``CF``, Clear Alarms). Standby, the fault output and each channel's
    alarm status are read every poll as well. The output configuration,
    limiter, delay and input sensitivity are read at connect and every Resync
    interval, and after a write. The document recommends following every Set
    with a Get, and the driver does.

Liveness:
    ``GY`` answers ``GY N`` or ``GY S`` in either power state. A link quiet
    for the probe interval is asked; two misses drop it with a typed
    ``no_response`` fault.

Not this driver: the PowerShare PS404D / PS604D (``bose_powershare``), the
PowerShareX (``bose_powersharex``, through a proxy port on an EX or ESP) and
the ControlSpace processors (``bose_controlspace``).

Source (Bose Professional, manufacturer document):
    ControlSpace Serial Control Protocol v5.15, revision date May 27, 2025
    (published as a zip holding the PDF). Sections 2, 3, 4, 5 (Table 2 and
    the GL indices for the PowerMatch slots; 5.8-5.13 standby,
    configuration, fault, clear, alarm reporting and the alarm log), 6.2 (the
    PowerMatch module tables) and 9.
    https://assets.boseprofessional.com/m/4b14907043cbedb3/original/ControlSpace-Serial-Protocol-v5-15.zip
"""


import asyncio
import json
import re
from typing import Any, Callable

from openavc.drivers.base import BaseDriver, CommandParamError
from openavc.transport.frame_parsers import CallableFrameParser
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ── Wire constants (sections 2, 3 and 5) ────────────────────────────────────

ACK = 0x06
NAK = 0x15
PORT = 10055
# A PM8xxxN has eight inputs and eight outputs, a PM4xxxN four of each (5.9:
# "1-8 ... (1-4 for PM4250N/PM4500N)").
MAX_CHANNELS = 8
CHANNEL_CHOICES = (8, 4)
# Table 2: PM8xxxN slot 1 In A-D, 2 Out 1-4, 3 In E-H, 4 Out 5-8; a PM4xxxN
# has slots 1 and 2 only. Channel numbers run 1-4 on each slot (5.1-5.3: "1 -
# 4 (for PM, PS, and PSX)").
INPUT_SLOTS = ("1", "3")
OUTPUT_SLOTS = ("2", "4")
SLOT_WIDTH = 4
# 5.4 note and the GL indices: "For 8ch PowerMatch and PowerShare devices the
# Get Signal Level (GL) command only uses Slot 1 (for Inputs) and Slot 2 (for
# Outputs) for all 8 channels."
GL_INPUTS = "1"
GL_OUTPUTS = "2"

DEFAULT_POLL_INTERVAL = 10
DEFAULT_RESYNC_INTERVAL = 60
DEFAULT_METER_INTERVAL_S = 1.0
MIN_METER_INTERVAL_S = 0.2
ACK_TIMEOUT_S = 2.0
QUERY_TIMEOUT_S = 2.0
SUB_TIMEOUT_S = 2.0
ALARM_LOG_TIMEOUT_S = 5.0
# Section 5.8: "entering or exiting standby is not immediate", so the power
# state is read again this long after a standby command as well as at once.
STANDBY_SETTLE_S = 5.0

NAK_CODES = {
    "01": "Invalid module name: no module in the amplifier has that label, "
          "or two modules share it",
    "02": "Illegal index: the index or number of indices is wrong for that module",
    "03": "Value out of range for that parameter",
    "99": "Unknown error",
}

# Amplifier outputs have no gain: levels run -60 dB (0h) to 0 dB (78h) in
# 0.5 dB steps, FFh = -60.5 dB / off (sections 4.2 and 5.1); the matrix
# module levels -60.5 to 0.0 dB (6.2.4).
LEVEL_MIN = -60.5
LEVEL_MAX = 0.0
HEX_LEVEL_MAX = 0x78
HEX_LEVEL_OFF = 0xFF
# 4.2-4.4: "n is the Group number, 1 - 40h (1-64 decimal)"; 4.1: "1 - FFh
# (1-255 decimal)" (the PowerShareX ranges are the smaller ones).
GROUP_MAX = 0x40
PARAMETER_SET_MAX = 0xFF

# Fixed module labels (section 6.2): "Matrix 1", "Limiter 1" to "Limiter 8",
# "Delay 1" to "Delay 8", "SpeakerPEQ 1" to "SpeakerPEQ 8", "Band Pass 1" to
# "Band Pass 8" and "PEQ-5band A" to "PEQ-5band H" are stated for the
# PowerMatch; "SigGen 1" is the signal generator's label in every example.
# A label the amplifier refuses (NAK 01) is reported in config_problems and
# on the hardware checklist. Only the Input and Amp Output modules can be
# renamed (section 6), and the document gives neither a default.
MATRIX_LABEL = "Matrix 1"
# 6.2.4: "Index 2 = (Input # -1) x 8 + Output #", Cross Point(9,8): the
# signal generator is matrix input 9 (6.2.2: "it appears as input 9"), so
# cross-point 65 is the signal generator into output 1.
MATRIX_STRIDE = 8
SIGGEN_INPUT = 9
SIGGEN_LABEL = "SigGen 1"
SIGGEN_INDEX = "5"          # 6.2.2: "Index 1 should always be set to 5"
# 6.2.10, PowerMatch: Index 1 = 1; Index 2 = 1 delay time (0 to 144000
# samples, 3 s), 2 bypass, 3 align delay (0 to 480 samples).
DELAY_MAX_SAMPLES = 144000
ALIGN_MAX_SAMPLES = 480
# The document's own conversion (6.2.10 examples): samples = seconds x 48000.
SAMPLE_RATE_HZ = 48000
# 6.2.6: "the Array EQ module is actually addressed as band 6 of the Input
# Parametric EQ module, hence Index 1 = 6", and "Module labels are "PEQ-5band
# A" through to "PEQ-5band H""; its examples write "Array EQ A" with the same
# indices. The driver tries the first and, refused, the second.
ARRAY_EQ_BAND = "6"


def input_letter(n: int) -> str:
    """Input n's letter: 1 is A, 8 is H (Table 2, the PEQ labels)."""
    return "ABCDEFGH"[int(n) - 1]


def input_slot(n: int) -> tuple[str, int]:
    """Input n (1-8) as (slot, channel): A-D are slot 1, E-H slot 3."""
    n = int(n)
    return INPUT_SLOTS[(n - 1) // SLOT_WIDTH], (n - 1) % SLOT_WIDTH + 1


def output_slot(n: int) -> tuple[str, int]:
    """Output n (1-8) as (slot, channel): 1-4 are slot 2, 5-8 slot 4."""
    n = int(n)
    return OUTPUT_SLOTS[(n - 1) // SLOT_WIDTH], (n - 1) % SLOT_WIDTH + 1


def channel_of_slot(slot: str, channel: int) -> tuple[str, int] | None:
    """A slot and channel as ("input" or "output", n), or None."""
    slot = slot.strip().lower()
    if not 1 <= channel <= SLOT_WIDTH:
        return None
    if slot in INPUT_SLOTS:
        return "input", INPUT_SLOTS.index(slot) * SLOT_WIDTH + channel
    if slot in OUTPUT_SLOTS:
        return "output", OUTPUT_SLOTS.index(slot) * SLOT_WIDTH + channel
    return None


def peq_label(n: int) -> str:
    """6.2.5: "Module labels for PowerMatch are "PEQ5band A" through to
    "PEQ-5band H" per the channel" (every example has the hyphen)."""
    return f"PEQ-5band {input_letter(n)}"


def array_eq_labels(n: int) -> tuple[str, str]:
    return peq_label(n), f"Array EQ {input_letter(n)}"


def speaker_peq_label(channel: int) -> str:
    return f"SpeakerPEQ {channel}"


def band_pass_label(channel: int) -> str:
    return f"Band Pass {channel}"


def limiter_label(channel: int) -> str:
    return f"Limiter {channel}"


def delay_label(channel: int) -> str:
    return f"Delay {channel}"


# ── Frame parser ────────────────────────────────────────────────────────────

_NAK_RE = re.compile(rb"\x15 ?(\d{0,2})")


def parse_powermatch_stream(buf: bytes) -> tuple[bytes | None, bytes]:
    """CallableFrameParser function: one CR (or LF) terminated line per
    message, with a bare ACK byte and a ``NAK nn`` delivered as their own
    frames whether or not a line ending follows them (section 3 says only
    "ACK (ASCII 0x06)"; 5.11 prints ``<ACK><CR>``). The returned buffer is
    what the parser keeps."""
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


# ── Value codec ─────────────────────────────────────────────────────────────

def format_number(value: float) -> str:
    """A number as the protocol writes it: no units, no trailing ``.0``."""
    v = float(value)
    if v == int(v):
        return str(int(v))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def parse_onoff(text: str) -> bool | None:
    """O = On, F = Off (every O,F,T parameter in section 6). One example
    prints a digit zero where the table's O belongs (6.2.4 ``GA"Matrix
    1">3>2=0``, "the matrix input mute for Channel 2 is on"), so a 0 also
    reads as On; the hardware checklist asks which the amplifier sends."""
    t = text.strip().strip('"').upper()
    if t in ("O", "0"):
        return True
    if t == "F":
        return False
    return None


def coerce_onoff(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "on", "yes", "y", "o", "mute", "muted", "inverted", "bypass", "bypassed",
             "enabled"):
        return True
    if s in ("0", "false", "off", "no", "n", "f", "unmute", "unmuted", "normal", "disabled"):
        return False
    raise CommandParamError(f"{value!r} is not an on/off value")


def hex_level_to_db(raw: int) -> float:
    """System / device command level (sections 4.2, 5.1): 0h = -60 dB, 0.5 dB
    a step, so 78h = 0 dB, the amplifier's ceiling; FFh = -60.5 dB (off)."""
    if raw >= HEX_LEVEL_OFF:
        return LEVEL_MIN
    return round(raw / 2.0 - 60.0, 1)


def db_to_hex_level(db: float) -> str:
    db = float(db)
    if db <= LEVEL_MIN:
        return format(HEX_LEVEL_OFF, "x")
    raw = int(round((min(db, LEVEL_MAX) + 60.0) * 2))
    return format(max(0, min(HEX_LEVEL_MAX, raw)), "x")


def meter_to_db(raw: int) -> float:
    """Get Signal Level (5.4): inputs read dBFS and outputs dBV max, both
    -60.0 to 0.0 in 0.5 dB steps from 0h. On a PowerMatch 0 dBV is 71 V on a
    mono channel and 141 V on a bridged or quad one."""
    return round(-60.0 + raw / 2.0, 1)


def crosspoint_index(input_no: int, output_no: int) -> int:
    """6.2.4: "Index 2 = (Input # -1) x 8 + Output #"; input 9 is the signal
    generator."""
    return (int(input_no) - 1) * MATRIX_STRIDE + int(output_no)


def crosspoint_of(index: int) -> tuple[int, int] | None:
    """The (input, output) a matrix Index 2 names: inputs 1-9 (9 the signal
    generator), outputs 1-8, so Cross Point(9,8) = 72 is the last."""
    i, o = divmod(int(index) - 1, MATRIX_STRIDE)
    i, o = i + 1, o + 1
    if 1 <= i <= SIGGEN_INPUT and 1 <= o <= MAX_CHANNELS:
        return i, o
    return None


def samples_to_ms(samples: int) -> float:
    return round(int(samples) * 1000.0 / SAMPLE_RATE_HZ, 3)


def ms_to_samples(ms: float) -> int:
    return int(round(float(ms) * SAMPLE_RATE_HZ / 1000.0))


# 5.9 Get Configuration: "IN= Independent (Mono), BL = Bridged (LoZ), B7 =
# Bridged (70v), B1 = Bridged (100v), PA = Parallel, QL = Quad (LoZ), Q7 =
# Quad (70v), Q1 = Quad (100v)".
OUTPUT_MODES = {"IN": "independent", "BL": "bridged_lo_z", "B7": "bridged_70v",
                "B1": "bridged_100v", "PA": "parallel", "QL": "quad_lo_z", "Q7": "quad_70v",
                "Q1": "quad_100v"}
_GC_RE = re.compile(r"^GC\s*([A-Za-z0-9]{2}(?:\s*,\s*[A-Za-z0-9]{2})*)\s*$")


def parse_configuration(text: str) -> list[str] | None:
    """``GC IN,IN,BL,BL,Q7,Q7,Q7,Q7`` as one mode per channel (eight on a
    PM8xxxN, four on a PM4xxxN); None when the line is not a GC reply. A
    code the document does not list reads as "unknown"."""
    m = _GC_RE.match(text.strip())
    if not m:
        return None
    codes = [c.strip().upper() for c in m.group(1).split(",")]
    return [OUTPUT_MODES.get(c, "unknown") for c in codes]


# 6.2.9, PowerMatch: Peak Threshold "0.5 to 71.0 V (or 142.0 for bridged)",
# RMS Threshold "0.5 to 50.0 V (or 142.0 for bridged)". The lower ceilings
# are applied only to an output the amplifier reports as Independent, the
# one mode the document ties them to; any other mode is bounded at 142 V and
# the amplifier judges the rest.
MONO_PEAK_MAX_V = 71.0
MONO_RMS_MAX_V = 50.0
BRIDGED_MAX_V = 142.0

# 6.2.1 Input module (PowerMatch): Index 1 Analog Sensitivity "0, 4, 12, 24
# dBu for PM only"; 2 Mute; 3 Source "O=On, F=Analog" (On is the digital
# input: "Select Digital input source").
SENSITIVITIES_DBU = (0, 4, 12, 24)
INPUT_SENSITIVITY_INDEX = "1"
INPUT_MUTE_INDEX = "2"
INPUT_SOURCE_INDEX = "3"

# 6.2.2, PowerMatch (Index 1 = 5): 1 Level -60.5 to 12.0 dB, 2 Repeat, 3 Mute,
# 4 Type (1 Pink Noise, 2 White Noise, 3 Tone, 4 Sweep), 5 Tone Freq, 6 Start
# Freq, 7 End Freq (20 to 20000 Hz), 8 Duration (1 to 5 s).
SIGGEN_TYPES = {"1": "pink_noise", "2": "white_noise", "3": "tone", "4": "sweep"}
SIGGEN_TYPE_WIRE = {v: k for k, v in SIGGEN_TYPES.items()}
SIGGEN_LEVEL_MAX = 12.0

# 5.12 Alarm Reporting. Severity: "W=Warning, F=Fault, S= System" (and
# N=No Alarm in a query's reply). Type, notification: "N=No Alarm, O=Open,
# S=Short, A=ACLoss, D=Digital Audio Loss, I=I-Share Jumper Missing,
# L=Limiting, C=Clip (Input), P=Protection, Z = Other"; a query's reply uses
# N, O, S, I and Z. Condition: "S=Set, C=Clear (not used for all
# alarms/faults)".
ALARM_SEVERITY = {"N": "none", "W": "warning", "F": "fault", "S": "system"}
ALARM_TYPE = {"N": "none", "O": "open", "S": "short", "A": "ac_loss", "D": "digital_audio_loss",
              "I": "ishare_jumper_missing", "L": "limiting", "C": "clip", "P": "protection",
              "Z": "other"}
ALARM_WORDS = {"none": "no alarm", "open": "open load", "short": "short circuit",
               "ac_loss": "AC mains loss", "digital_audio_loss": "digital audio loss",
               "ishare_jumper_missing": "I-Share jumper missing", "limiting": "limiting",
               "clip": "input clip", "protection": "protection", "other": "other alarm"}


# ── Replies ─────────────────────────────────────────────────────────────────

_GA_RE = re.compile(r'^GA\s*"([^"]*)"((?:>[^>=]*)+)=(.*)$')
_SUB_RE = re.compile(r'^(SUB|UNS)\s*"(.*)",\s*(yes|no)\s*$', re.IGNORECASE)
_SUB_SUPPORT_RE = re.compile(r"^SUB\s+(yes|no)\s*$", re.IGNORECASE)
_S_RE = re.compile(r"^S\s*([0-9a-fA-F]+)$")
_GG_RE = re.compile(r"^GG\s*([0-9a-fA-F]+),([0-9a-fA-F]+)$")
_GN_RE = re.compile(r"^GN\s*([0-9a-fA-F]+),([MUmu])$")
_GV_RE = re.compile(r"^GV\s*([0-9a-fA-F]+),([0-9a-fA-F]+),([0-9a-fA-F]+)$")
_GM_RE = re.compile(r"^GM\s*([0-9a-fA-F]+),([0-9a-fA-F]+),([MUmu])$")
_GL_RE = re.compile(r"^GL\s*([0-9a-fA-F]+)(?:,([0-9a-fA-F]+))?\s*\[([^\]]*)\]$")
_GY_RE = re.compile(r"^GY\s*([SNsn])$")
_GF_RE = re.compile(r"^GF\s*([FCfc])$")
# A query's reply ``GR c,s,t`` and a notification ``GR c,s,t,x`` (5.12).
_GR_STATUS_RE = re.compile(r"^GR\s*([0-9a-fA-F]+),\s*([A-Za-z]),\s*([A-Za-z])$")
_GR_EVENT_RE = re.compile(r"^GR\s*([0-9a-fA-F]+),\s*([A-Za-z]),\s*([A-Za-z]),\s*([SCsc])$")
_GH_START_RE = re.compile(r"^GH\s*\[(.*)$")
_IP_RE = re.compile(r"^IP\s+(\d{1,3}(?:\.\d{1,3}){3})$")
_NP_RE = re.compile(r"^NP\s*([TMGtmg]),(.+)$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def parse_ga_reply(text: str) -> tuple[str, tuple[str, ...], str] | None:
    """``GA "Label">i1>i2=value`` (the document also prints the response form
    with a ``>`` before the ``=``) as (label, index path, value)."""
    m = _GA_RE.match(text.strip())
    if not m:
        return None
    idx = tuple(p.strip() for p in m.group(2).split(">") if p.strip())
    return m.group(1), idx, m.group(3).strip()


def module_ref(name: str) -> str:
    return f'"{name}"'


def sa_line(name: str, path: str, value: str) -> str:
    return f"SA {module_ref(name)}>{path}={value}"


def ga_line(name: str, path: str) -> str:
    return f"GA {module_ref(name)}>{path}"


def sub_line(get_text: str) -> str:
    return f'SUB "{get_text}"'


def same_get(a: str, b: str) -> bool:
    """Whether two GET texts name the same value: an echo in a SUB reply is
    compared without regard to spacing or case."""
    def norm(t: str) -> str:
        return re.sub(r"\s+", " ", t.strip()).lower()
    return norm(a) == norm(b)


# ── Module parameter tables (section 6.2, PowerMatch) ───────────────────────

FILTER_TYPES = [
    ("But6", "Butterworth 6 dB/oct"), ("But12", "Butterworth 12 dB/oct"),
    ("But18", "Butterworth 18 dB/oct"), ("But24", "Butterworth 24 dB/oct"),
    ("But36", "Butterworth 36 dB/oct"), ("But48", "Butterworth 48 dB/oct"),
    ("Bes12", "Bessel 12 dB/oct"), ("Bes18", "Bessel 18 dB/oct"),
    ("Bes24", "Bessel 24 dB/oct"), ("Bes36", "Bessel 36 dB/oct"),
    ("Bes48", "Bessel 48 dB/oct"),
    ("Lin12", "Linkwitz-Riley 12 dB/oct"), ("Lin24", "Linkwitz-Riley 24 dB/oct"),
    ("Lin36", "Linkwitz-Riley 36 dB/oct"), ("Lin48", "Linkwitz-Riley 48 dB/oct"),
]
INPUT_PEQ_TYPES = ["B", "HS", "LS", "HC", "LC", "N"]
SPEAKER_PEQ_TYPES = ["B", "HS", "LS", "N"]
INPUT_PEQ_BANDS = 5         # 6.2.5: "1-5 for PowerMatch"
SPEAKER_PEQ_BANDS = 9       # 6.2.8: "9 bands of parametric EQ"

# parameter -> (Index 2, kind, min, max). kind: number, onoff, choice, array.
INPUT_PEQ_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "frequency": ("1", "number", 20, 20000),
    "q": ("2", "number", 0.1, 10.0),
    "gain": ("3", "number", -20.0, 20.0),
    "slope": ("4", "choice", ["0", "-6", "-12"], None),
    "type": ("5", "choice", INPUT_PEQ_TYPES, None),
    "bypass": ("6", "onoff", None, None),
}
SPEAKER_PEQ_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "frequency": ("1", "number", 200, 20000),
    "q": ("2", "number", 0.1, 10.0),
    "gain": ("3", "number", -20.0, 20.0),
    "type": ("5", "choice", SPEAKER_PEQ_TYPES, None),
    "bypass": ("6", "onoff", None, None),
}
# 6.2.8 Gain/Polarity (Index 1 = 0): Index 2 = 3 Gain "-15.0B to +15.0dB,
# 0.5dB step (PM and PSX only)", 4 Polarity O,F,T.
SPEAKER_GAIN_PATH = "0>3"
SPEAKER_POLARITY_PATH = "0>4"
BAND_PASS_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "hpf_type": ("1", "choice", [t for t, _ in FILTER_TYPES], None),
    "hpf_frequency": ("2", "number", 20, 20000),
    "lpf_type": ("3", "choice", [t for t, _ in FILTER_TYPES], None),
    "lpf_frequency": ("4", "number", 20, 20000),
    "hpf_bypass": ("8", "onoff", None, None),
    "lpf_bypass": ("9", "onoff", None, None),
}
# 6.2.6, PowerMatch (Index 1 = 6): 1 Center Freq 100 to 4000 Hz, 2 Tilt 0.0
# to 10.0, 3 Gain -12.0 to +2.0 dB, 4 Array "M.VVV, M=2 to 8, VVV=20 to 100, 5
# step" (modules and vertical coverage: ``4.080`` is four modules at 80
# degrees), 5 Reserved, 6 Bypass.
ARRAY_EQ_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "center_frequency": ("1", "number", 100, 4000),
    "tilt": ("2", "number", 0.0, 10.0),
    "gain": ("3", "number", -12.0, 2.0),
    "array": ("4", "array", None, None),
    "bypass": ("6", "onoff", None, None),
}
# 6.2.9, PowerMatch, Index 1 always 0: (Index 2, state variable, kind, min,
# max, step). Thresholds are bounded at the bridged ceiling here and at the
# mono one for an Independent output (MONO_*_MAX_V above).
LIMITER_PARAMS: dict[str, tuple[str, str, str, float, float, float]] = {
    "peak_threshold": ("1", "limiter_peak_v", "number", 0.5, BRIDGED_MAX_V, 0.5),
    "rms_threshold": ("2", "limiter_rms_v", "number", 0.5, BRIDGED_MAX_V, 0.5),
    "rms_attack": ("3", "limiter_rms_attack_ms", "int", 500, 10000, 100),
    "rms_release": ("4", "limiter_rms_release_ms", "int", 500, 10000, 100),
    "link_group": ("5", "limiter_link_group", "int", 0, 4, 1),
}
# 6.2.2, PowerMatch, Index 1 = 5: the parameters the signal generator command
# sets besides level, mute and type, which have commands of their own.
SIGGEN_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "repeat": ("2", "onoff", None, None),
    "tone_frequency": ("5", "int", 20, 20000),
    "start_frequency": ("6", "int", 20, 20000),
    "end_frequency": ("7", "int", 20, 20000),
    "duration": ("8", "int", 1, 5),
}

_ARRAY_RE = re.compile(r"^\s*([2-8])\.(\d{2,3})\s*$")


def encode_array(value: Any) -> str:
    """6.2.6 Array "M.VVV": M modules (2 to 8) and VVV degrees of vertical
    coverage (20 to 100 in steps of 5), written ``4.080``."""
    m = _ARRAY_RE.match(str(value))
    if not m:
        raise CommandParamError("array is M.VVV: 2 to 8 modules, then 20 to 100 degrees "
                                "(4.080 is four modules at 80 degrees)")
    degrees = int(m.group(2))
    if not 20 <= degrees <= 100 or degrees % 5:
        raise CommandParamError("the array's coverage is 20 to 100 degrees in steps of 5")
    return f"{m.group(1)}.{degrees:03d}"


def encode_table_value(parameter: str, spec: tuple, value: Any) -> str:
    """A value for one of the module parameters, checked against the
    document's range."""
    kind, lo, hi = spec[1], spec[2], spec[3]
    if kind == "onoff":
        return "O" if coerce_onoff(value) else "F"
    if kind == "choice":
        text = str(value).strip()
        if text.endswith(".0"):
            text = text[:-2]
        for choice in lo:
            if text.lower() == choice.lower():
                return choice
        raise CommandParamError(f"{parameter} is one of {', '.join(lo)}")
    if kind == "array":
        return encode_array(value)
    try:
        v = float(str(value).strip())
    except ValueError as exc:
        raise CommandParamError(f"{parameter} takes a number") from exc
    if not lo <= v <= hi:
        raise CommandParamError(f"{parameter} is {format_number(lo)} to {format_number(hi)}")
    if kind == "int":
        return str(int(round(v)))
    return format_number(v)


# ── Children ────────────────────────────────────────────────────────────────

INPUT_CHILD_TYPE = "input"
OUTPUT_CHILD_TYPE = "output"
GROUP_CHILD_TYPE = "group"


def crosspoint_prop(input_no: int) -> str:
    """The output child's state variable for a matrix input: ``input_1`` to
    ``input_8``, and ``signal_generator`` for input 9."""
    return "signal_generator" if int(input_no) == SIGGEN_INPUT else f"input_{int(input_no)}"


def read_input_labels(config: dict[str, Any]) -> tuple[dict[int, str], list[str]]:
    """The typed Input module labels, by input; blank = not set."""
    labels: dict[int, str] = {}
    problems: list[str] = []
    for n in range(1, MAX_CHANNELS + 1):
        text = str(config.get(f"input_{input_letter(n).lower()}_label") or "").strip()
        if not text:
            continue
        if '"' in text:
            problems.append(f"Input {input_letter(n)} label: a label cannot contain a double quote")
            continue
        labels[n] = text
    return labels, problems


def configured_channels(config: dict[str, Any]) -> int:
    try:
        n = int(str(config.get("channels", MAX_CHANNELS)).strip())
    except (TypeError, ValueError):
        return MAX_CHANNELS
    return n if n in CHANNEL_CHOICES else MAX_CHANNELS


def parse_groups_config(rows: Any) -> tuple[dict[int, str], list[str]]:
    groups: dict[int, str] = {}
    problems: list[str] = []
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
        if not 1 <= number <= GROUP_MAX:
            problems.append(f"group row {n} ({name}): group number {number} is outside 1..64")
            continue
        if number in groups:
            problems.append(f"group row {n} ({name}): duplicates group {number}")
            continue
        groups[number] = name
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


class BosePowerMatchDriver(BaseDriver):
    """Bose Professional PowerMatch PM8500N / PM8250N / PM4500N / PM4250N
    over TCP 10055."""

    DRIVER_INFO = {
        "id": "bose_powermatch",
        "name": "Bose Professional PowerMatch (PM8500N / PM8250N / PM4500N / PM4250N)",
        "manufacturer": "Bose Professional",
        "category": "audio",
        "version": "1.0.0",
        # BaseDriver.local_address(), which the Test Connection socket binds
        # to, needs 0.37.0 (set by hand: the contract check cannot see it).
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": (
            "Controls Bose Professional PowerMatch PM8500N, PM8250N, PM4500N and PM4250N "
            "amplifiers through the ControlSpace Serial Control Protocol on TCP port 10055: "
            "standby, each output's level, mute and polarity, each input's mute and source, "
            "the matrix with its signal generator, limiters, delays, EQs, parameter sets, "
            "volume groups, signal levels, each channel's output configuration, and the "
            "amplifier's fault and alarm reports and alarm log. Values are subscribed, so "
            "changes made elsewhere appear at once."
        ),
        "source_url": "https://assets.boseprofessional.com/m/4b14907043cbedb3/original/ControlSpace-Serial-Protocol-v5-15.zip",
        "tags": ["amplifier", "bose", "powermatch", "install-audio", "dante", "controlspace"],
        "verified": False,
        "simulated": True,
        "protocols": ["bose-controlspace-serial"],
        "ports": [10055],
        "transport": "tcp",
        "discovery": {
            # GC (Get Configuration, 5.9) is "PowerMatch Only" and changes
            # nothing: a PowerShare, a PowerShareX proxy and a ControlSpace
            # processor leave it unanswered, and bose_powershare's probe
            # declines a unit that answers it.
            "tcp_probe": {
                "port": 10055,
                "send_ascii": "GC\r",
                "expect_regex": r"^GC ?(IN|BL|B7|B1|PA|QL|Q7|Q1)(,(IN|BL|B7|B1|PA|QL|Q7|Q1)){3}",
                "timeout_ms": 1500,
            },
            "port_open": [10055],
            # Bose Corporation's registered prefixes (IEEE registry), the
            # same list bose_controlspace and bose_powershare carry.
            "oui": ["00:0c:8a", "04:52:c7", "08:df:1f", "28:11:a5", "2c:41:a1", "48:22:1d",
                    "4c:87:5d", "60:ab:d2", "68:f2:1f", "78:2b:64", "ac:bf:71", "bc:87:fa",
                    "c8:7b:23", "e4:58:bc"],
            "manufacturer_alias": ["bose", "bose professional", "bose corporation", "powermatch"],
        },
        "compatible_models": [
            {
                "manufacturer": "Bose Professional",
                "models": ["PM8500N", "PM8250N"],
                "confidence": "untested",
                "notes": (
                    "Networked PowerMatch amplifiers with eight inputs and eight output "
                    "channels, TCP 10055 (fixed, up to 32 connections). The PM8500N is sold "
                    "while stock lasts; the PM8250N is discontinued. Built from the "
                    "ControlSpace Serial Control Protocol v5.15 and the simulator; not yet "
                    "run against an amplifier."
                ),
            },
            {
                "manufacturer": "Bose Professional",
                "models": ["PM4500N", "PM4250N"],
                "confidence": "untested",
                "notes": (
                    "Networked PowerMatch amplifiers with four inputs and four output "
                    "channels (discontinued). Built from the ControlSpace Serial Control "
                    "Protocol v5.15 and the simulator; not yet run against an amplifier."
                ),
            },
        ],
        "help": {
            "overview": (
                "PowerMatch control over the ControlSpace Serial Control Protocol (TCP 10055, "
                "no login). Each input and output is a child entity: output level, mute and "
                "polarity, input mute and source, the matrix cross-points into each output, "
                "the output configuration, limiter and delay, alarms and signal levels. "
                "Standby, the fault output, the alarm log, the signal generator, parameter "
                "sets and volume groups are on the device. Values are subscribed where the "
                "amplifier supports it, so panels follow changes made anywhere."
            ),
            "setup": (
                "STEP 1 - Address.\n"
                "Enter the amplifier's IP address (its front panel or ControlSpace Designer "
                "shows it). The port is always 10055.\n\n"
                "STEP 2 - Channels.\n"
                "Pick 8 for a PM8500N or PM8250N, 4 for a PM4500N or PM4250N. The amplifier "
                "reports its channel count when it connects, and the setting follows it.\n\n"
                "STEP 3 - Input labels (optional).\n"
                "To choose each input's analog or digital source and its analog sensitivity, "
                "type that input's Input module label exactly as ControlSpace Designer shows "
                "it. Everything else works without labels.\n\n"
                "STEP 4 - Groups and parameter sets (optional).\n"
                "Add the volume groups and parameter sets from your design by number and name "
                "if you want to recall them by name or bind group levels.\n\n"
                "STEP 5 - Test.\n"
                "Run Test Connection. It reports whether the amplifier answered, how its "
                "outputs are configured, whether it supports subscriptions, and whether each "
                "module and typed label answered."
            ),
            "connection": (
                "TCP 10055, no login; the amplifier takes up to 32 connections, shared with "
                "ControlSpace Remote. Going online with ControlSpace Designer closes the "
                "connection; the driver reconnects on its own."
            ),
        },
        "default_config": {
            "host": "",
            "port": 10055,
            "channels": "8",
            "input_a_label": "",
            "input_b_label": "",
            "input_c_label": "",
            "input_d_label": "",
            "input_e_label": "",
            "input_f_label": "",
            "input_g_label": "",
            "input_h_label": "",
            "groups": [],
            "parameter_sets": [],
            "alarm_notifications": True,
            "enable_meters": False,
            "meter_interval_s": 1.0,
            "poll_interval": 10,
            "resync_interval": 60,
            "inter_command_delay": 0,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address",
                     "help": "The amplifier's IP address, from its front panel or ControlSpace "
                             "Designer."},
            "port": {"type": "integer", "default": 10055, "label": "Port", "min": 1,
                     "max": 65535, "advanced": True,
                     "help": "Fixed at 10055 on PowerMatch amplifiers."},
            "channels": {
                "type": "enum",
                "default": "8",
                "label": "Channels",
                "values": [
                    {"value": "8", "label": "8 (PM8500N / PM8250N)"},
                    {"value": "4", "label": "4 (PM4500N / PM4250N)"},
                ],
                "learned_from": "channel_count",
                "help": "The amplifier reports this when it connects, and it is saved here. "
                        "Pick one to build or simulate a project before the amplifier is "
                        "reachable.",
            },
            "input_a_label": {"type": "string", "default": "", "label": "Input A Label",
                                "help": "The Input module's label in ControlSpace Designer. "
                                        "Needed only for the analog / digital source and the "
                                        "analog sensitivity."},
            "input_b_label": {"type": "string", "default": "", "label": "Input B Label"},
            "input_c_label": {"type": "string", "default": "", "label": "Input C Label"},
            "input_d_label": {"type": "string", "default": "", "label": "Input D Label"},
            "input_e_label": {"type": "string", "default": "", "label": "Input E Label"},
            "input_f_label": {"type": "string", "default": "", "label": "Input F Label"},
            "input_g_label": {"type": "string", "default": "", "label": "Input G Label"},
            "input_h_label": {"type": "string", "default": "", "label": "Input H Label"},
            "groups": {
                "type": "table", "label": "Volume Groups", "row_label": "group",
                "columns": {
                    "number": {
                        "type": "integer",
                        "label": "Group Number",
                        "required": True,
                        "min": 1,
                        "max": 64,
                        "help": "The volume Group number in ControlSpace Designer (1-64).",
                    },
                    "name": {
                        "type": "string",
                        "label": "Name",
                        "required": True,
                        "help": (
                            "What the group controls (Lobby Speakers). Shown on the child entity."
                        ),
                    },
                },
                "help": "Volume groups from your ControlSpace Designer design that include "
                        "this amplifier's outputs. Each becomes a child with a master level "
                        "and mute.",
            },
            "parameter_sets": {
                "type": "table", "label": "Parameter Sets", "row_label": "parameter set",
                "columns": {
                    "number": {
                        "type": "integer",
                        "label": "Parameter Set",
                        "required": True,
                        "min": 1,
                        "max": 255,
                        "help": "The Parameter Set number in ControlSpace Designer (1-255).",
                    },
                    "name": {
                        "type": "string",
                        "label": "Name",
                        "required": True,
                        "help": (
                            "Shown in the recall picker and as the current parameter set's name."
                        ),
                    },
                },
                "help": "Names for the parameter sets you recall. The protocol knows only "
                        "numbers; the names feed the recall picker.",
            },
            "alarm_notifications": {
                "type": "boolean", "default": True, "label": "Alarm reports",
                "advanced": True,
                "help": "Turn on the amplifier's own fault and alarm reports, the only way it "
                        "reports limiting and clipping. The reports go to every system "
                        "connected to the amplifier.",
            },
            "enable_meters": {
                "type": "boolean", "default": False, "label": "Poll signal levels",
                "help": "Read every input's and output's signal level at the rate below. "
                        "Off keeps the link quiet; levels then read as unknown.",
            },
            "meter_interval_s": {
                "type": "number", "default": 1.0, "min": 0.2,
                "max": 60, "label": "Signal level rate (s)", "advanced": True,
                "help": "Seconds between signal level reads (0.2 = five a second).",
            },
            "poll_interval": {
                "type": "integer", "default": 10, "min": 0, "max": 3600,
                "label": "Poll interval (s)", "advanced": True,
                "help": "How often standby, the fault output and each channel's alarm status "
                        "are read. On an amplifier without subscription support every level, "
                        "mute and cross-point is read this often too.",
            },
            "resync_interval": {
                "type": "integer", "default": 60, "min": 10, "max": 3600,
                "label": "Resync interval (s)", "advanced": True,
                "help": "How often the subscriptions and alarm reports are renewed and the "
                        "output configuration, limiter, delay and input sensitivity read again.",
            },
            "inter_command_delay": {
                "type": "number", "default": 0, "min": 0, "max": 1,
                "label": "Inter-command delay (s)", "advanced": True,
            },
        },
        "child_entity_types": {
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "integer", "min": 1, "max": 8},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "source": {
                        "type": "enum",
                        "values": ["analog", "digital"],
                        "label": "Source",
                        "control": True,
                        "help": "The analog or the digital input. Read only when the input's "
                                "label is typed in the device settings.",
                    },
                    "sensitivity_dbu": {
                        "type": "integer",
                        "label": "Analog Sensitivity (dBu)",
                        "min": 0,
                        "max": 24,
                        "unit": "dBu",
                        "help": "0, 4, 12 or 24 dBu. Read only when the input's label is typed "
                                "in the device settings.",
                    },
                    "clipping": {
                        "type": "boolean",
                        "label": "Input Clip Reported",
                        "cloud_priority": "high",
                        "help": (
                            "The amplifier reported this input clipping. It reports only the "
                            "first clip until its alarms are cleared, so this stays on until "
                            "Clear Alarms runs. Needs Alarm reports on."
                        ),
                    },
                    "signal_level": {
                        "type": "number",
                        "label": "Signal Level (dBFS)",
                        "min": -60.0,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "cloud_priority": "low",
                        "help": "Read only while Poll signal levels is on.",
                    },
                },
                "summary_fields": ["mute", "source"],
                "label_field": "name",
            },
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "integer", "min": 1, "max": 8},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "level_db": {
                        "type": "number",
                        "label": "Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                        "help": (
                            "-60.5 is off. The amplifier ignores a level change while the output "
                            "is muted."
                        ),
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "polarity_inverted": {
                        "type": "boolean",
                        "label": "Polarity Inverted",
                        "control": True,
                        "help": "The speaker EQ's polarity for this output.",
                    },
                    "output_mode": {
                        "type": "enum",
                        "values": ["independent", "bridged_lo_z", "bridged_70v", "bridged_100v",
                                   "parallel", "quad_lo_z", "quad_70v", "quad_100v", "unknown"],
                        "label": "Output Configuration",
                        "help": "How the amplifier's design configures this channel: "
                                "independent (mono), bridged, parallel or quad, and the load.",
                    },
                    "alarm_severity": {
                        "type": "enum",
                        "values": ["none", "warning", "fault", "system"],
                        "label": "Alarm Severity",
                        "cloud_priority": "high",
                        "help": "The most severe alarm or fault the amplifier reports on this "
                                "channel.",
                    },
                    "alarm": {
                        "type": "enum",
                        "values": ["none", "open", "short", "ac_loss", "digital_audio_loss",
                                   "ishare_jumper_missing", "protection", "other"],
                        "label": "Alarm",
                        "cloud_priority": "high",
                        "help": "What that alarm is: an open or shorted load, a missing "
                                "I-Share jumper, protection, or another alarm.",
                    },
                    "limiting": {
                        "type": "boolean",
                        "label": "Limiting Reported",
                        "cloud_priority": "high",
                        "help": (
                            "The amplifier reported this output limiting. It reports only the "
                            "first time until its alarms are cleared, so this stays on until "
                            "Clear Alarms runs. Needs Alarm reports on."
                        ),
                    },
                    "signal_level": {
                        "type": "number",
                        "label": "Signal Level (dBV max)",
                        "min": -60.0,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "cloud_priority": "low",
                        "help": (
                            "0 dBV is the channel's full output: 71 V on an independent "
                            "channel, 141 V bridged or quad. Read only while Poll signal levels "
                            "is on."
                        ),
                    },
                    "input_1": {
                        "type": "boolean",
                        "label": "From Input A",
                        "control": True,
                        "help": "Matrix 1 cross-point: input A into this output.",
                    },
                    "input_1_level": {
                        "type": "number",
                        "label": "From Input A Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "input_2": {
                        "type": "boolean",
                        "label": "From Input B",
                        "control": True,
                        "help": "Matrix 1 cross-point: input B into this output.",
                    },
                    "input_2_level": {
                        "type": "number",
                        "label": "From Input B Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "input_3": {
                        "type": "boolean",
                        "label": "From Input C",
                        "control": True,
                        "help": "Matrix 1 cross-point: input C into this output.",
                    },
                    "input_3_level": {
                        "type": "number",
                        "label": "From Input C Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "input_4": {
                        "type": "boolean",
                        "label": "From Input D",
                        "control": True,
                        "help": "Matrix 1 cross-point: input D into this output.",
                    },
                    "input_4_level": {
                        "type": "number",
                        "label": "From Input D Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "input_5": {
                        "type": "boolean",
                        "label": "From Input E",
                        "control": True,
                        "help": "Matrix 1 cross-point: input E into this output.",
                    },
                    "input_5_level": {
                        "type": "number",
                        "label": "From Input E Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "input_6": {
                        "type": "boolean",
                        "label": "From Input F",
                        "control": True,
                        "help": "Matrix 1 cross-point: input F into this output.",
                    },
                    "input_6_level": {
                        "type": "number",
                        "label": "From Input F Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "input_7": {
                        "type": "boolean",
                        "label": "From Input G",
                        "control": True,
                        "help": "Matrix 1 cross-point: input G into this output.",
                    },
                    "input_7_level": {
                        "type": "number",
                        "label": "From Input G Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "input_8": {
                        "type": "boolean",
                        "label": "From Input H",
                        "control": True,
                        "help": "Matrix 1 cross-point: input H into this output.",
                    },
                    "input_8_level": {
                        "type": "number",
                        "label": "From Input H Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "signal_generator": {
                        "type": "boolean",
                        "label": "From Signal Generator",
                        "control": True,
                        "help": "Matrix 1 cross-point: the signal generator into this output.",
                    },
                    "signal_generator_level": {
                        "type": "number",
                        "label": "From Signal Generator Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "limiter_peak_v": {
                        "type": "number",
                        "label": "Limiter Peak Threshold (V)",
                        "min": 0.5,
                        "max": 142.0,
                        "step": 0.5,
                        "unit": "V",
                        "help": "0.5 to 71 V on an independent output, up to 142 V bridged.",
                    },
                    "limiter_rms_v": {
                        "type": "number",
                        "label": "Limiter RMS Threshold (V)",
                        "min": 0.5,
                        "max": 142.0,
                        "step": 0.5,
                        "unit": "V",
                        "help": "0.5 to 50 V on an independent output, up to 142 V bridged.",
                    },
                    "limiter_rms_attack_ms": {
                        "type": "integer",
                        "label": "Limiter RMS Attack (ms)",
                        "min": 500,
                        "max": 10000,
                        "step": 100,
                        "unit": "ms",
                    },
                    "limiter_rms_release_ms": {
                        "type": "integer",
                        "label": "Limiter RMS Release (ms)",
                        "min": 500,
                        "max": 10000,
                        "step": 100,
                        "unit": "ms",
                    },
                    "limiter_link_group": {
                        "type": "integer",
                        "label": "Limiter Link Group",
                        "min": 0,
                        "max": 4,
                        "help": "0 is no link group; outputs in the same group (1-4) limit "
                                "together.",
                    },
                    "delay_ms": {
                        "type": "number",
                        "label": "Delay (ms)",
                        "min": 0.0,
                        "max": 3000.0,
                        "unit": "ms",
                        "help": "0 to 3000 ms (144000 samples at 48 kHz).",
                    },
                    "delay_bypass": {"type": "boolean", "label": "Delay Bypass"},
                    "align_delay_ms": {
                        "type": "number",
                        "label": "Align Delay (ms)",
                        "min": 0.0,
                        "max": 10.0,
                        "unit": "ms",
                        "help": "0 to 10 ms (480 samples at 48 kHz).",
                    },
                },
                "summary_fields": ["level_db", "mute", "alarm"],
                "label_field": "name",
            },
            "group": {
                "label": "Group",
                "label_plural": "Groups",
                "id_format": {"type": "integer", "min": 1, "max": 64},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "level_db": {
                        "type": "number",
                        "label": "Master Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                    },
                    "mute": {"type": "boolean", "label": "Master Mute", "control": True},
                    "responding": {"type": "boolean", "label": "Responding"},
                },
                "summary_fields": ["level_db", "mute"],
                "label_field": "name",
            },
        },
        "state_variables": {
            "power": {"type": "boolean", "label": "Power", "control": True, "cloud_priority": "high",
                      "help": "On when the amplifier reports Normal; off in standby."},
            "fault": {"type": "boolean", "label": "Fault Output", "cloud_priority": "high",
                      "help": "On while the amplifier's fault output is in its fault state."},
            "ac_mains_loss": {"type": "boolean", "label": "AC Mains Loss Reported",
                              "cloud_priority": "high",
                              "help": "The amplifier reported losing AC mains, until it reports "
                                      "it cleared. Needs Alarm reports on."},
            "digital_audio_loss": {"type": "boolean", "label": "Digital Audio Loss Reported",
                                   "cloud_priority": "high",
                                   "help": "The amplifier reported losing digital audio, until it "
                                           "reports it cleared. Needs Alarm reports on."},
            "last_alarm": {"type": "string", "label": "Last Alarm Report",
                           "help": "The amplifier's most recent alarm report, in words."},
            "alarm_count": {"type": "integer", "label": "Alarm Log Entries", "min": 0,
                            "cloud_priority": "high",
                            "help": "How many alarms the amplifier's alarm history holds."},
            "alarm_log": {"type": "string", "label": "Alarm Log",
                          "help": "The amplifier's alarm history, one entry per alarm, as of "
                                  "the last read."},
            "channel_count": {"type": "integer", "label": "Channels", "min": 4, "max": 8,
                              "help": "How many output channels the amplifier reports (8 on a "
                                      "PM8xxxN, 4 on a PM4xxxN)."},
            "parameter_set": {"type": "integer", "label": "Last Parameter Set", "min": 0,
                              "max": 255, "control": True,
                              "help": "The last parameter set recalled; 0 after power-up."},
            "parameter_set_name": {"type": "string", "label": "Last Parameter Set Name"},
            "parameter_set_options": {"type": "string", "label": "Parameter Set List",
                                      "help": "The Parameter Sets table as a picker list."},
            "signal_generator_level_db": {"type": "number", "label": "Signal Generator Level (dB)",
                                          "min": -60.5, "max": 12.0, "step": 0.5, "unit": "dB",
                                          "control": True},
            "signal_generator_mute": {"type": "boolean", "label": "Signal Generator Mute",
                                      "control": True},
            "signal_generator_type": {"type": "enum", "label": "Signal Generator Type",
                                      "values": ["pink_noise", "white_noise", "tone", "sweep"],
                                      "control": True},
            "push_supported": {"type": "boolean", "label": "Subscriptions Supported",
                               "help": "True when the amplifier answered SUB with yes; otherwise "
                                       "every value is read each poll."},
            "ip_address": {"type": "string", "label": "IP Address",
                           "help": "The address the amplifier reports for itself."},
            "subnet_mask": {"type": "string", "label": "Subnet Mask"},
            "gateway": {"type": "string", "label": "Default Gateway"},
            "addressing": {"type": "enum", "values": ["dhcp", "static"], "label": "Addressing"},
            "config_problems": {"type": "string", "label": "Problems",
                                "help": "A module or typed label the amplifier refused, or a "
                                        "setting that could not be read."},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "commands": {
            "power_on": {
                "label": "Power On (Normal)",
                "params": {},
                "sets": {"power": True},
                "help": (
                    "Takes the amplifier out of standby so the outputs pass audio again. It takes "
                    "a few seconds."
                ),
            },
            "standby": {
                "label": "Standby",
                "params": {},
                "sets": {"power": False},
                "help": (
                    "Puts the amplifier into standby: the outputs go silent and it keeps "
                    "answering on the network."
                ),
            },
            "set_output_level": {
                "label": "Set Output Level",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "level": {
                        "type": "number",
                        "required": True,
                        "label": "Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "unit": "dB",
                        "help": "-60.5 (off) to 0 dB in 0.5 dB steps.",
                    },
                },
                "sets": {"level_db": "{level}"},
                "help": (
                    "Sets an output's level. The amplifier ignores a level change while the output "
                    "is muted, so this is refused then: unmute first."
                ),
            },
            "step_output_level": {
                "label": "Step Output Level",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "amount": {
                        "type": "number",
                        "required": True,
                        "label": "Amount (dB)",
                        "default": 1.0,
                        "min": -60.0,
                        "max": 60.0,
                        "help": "Positive raises, negative lowers, in 0.5 dB steps.",
                    },
                },
                "help": (
                    "Raises or lowers an output's level by the amount, stopping at 0 dB and off. "
                    "Refused while the output is muted, which the amplifier would ignore."
                ),
            },
            "set_output_mute": {
                "label": "Set Output Mute",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "mute": {"type": "boolean", "required": True, "label": "Mute"},
                },
                "sets": {"mute": "{mute}"},
                "help": "Mutes or unmutes an output channel.",
            },
            "toggle_output_mute": {
                "label": "Toggle Output Mute",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                },
                "help": "Flips an output channel's mute.",
            },
            "set_output_polarity": {
                "label": "Set Output Polarity",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "inverted": {"type": "boolean", "required": True, "label": "Inverted"},
                },
                "sets": {"polarity_inverted": "{inverted}"},
                "help": "Inverts an output's polarity, or puts it back to normal.",
            },
            "set_input_mute": {
                "label": "Set Input Mute",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "mute": {"type": "boolean", "required": True, "label": "Mute"},
                },
                "sets": {"mute": "{mute}"},
                "help": "Mutes or unmutes an input, silencing it in every output.",
            },
            "toggle_input_mute": {
                "label": "Toggle Input Mute",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                },
                "help": "Flips an input's mute.",
            },
            "set_input_source": {
                "label": "Set Input Source",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "source": {
                        "type": "enum",
                        "required": True,
                        "label": "Source",
                        "values": ["analog", "digital"],
                    },
                },
                "sets": {"source": "{source}"},
                "help": (
                    "Switches an input between its analog and digital input. Needs that input's "
                    "label typed in the device settings."
                ),
            },
            "set_input_sensitivity": {
                "label": "Set Input Sensitivity",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "sensitivity": {
                        "type": "enum",
                        "required": True,
                        "label": "Sensitivity (dBu)",
                        "values": ["0", "4", "12", "24"],
                    },
                },
                "help": (
                    "Sets an analog input's sensitivity: the input level that drives the "
                    "amplifier to full output. Needs that input's label typed in the device "
                    "settings."
                ),
            },
            "set_crosspoint": {
                "label": "Set Cross-Point",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "on": {"type": "boolean", "required": True, "label": "On"},
                },
                "help": "Turns an input's feed into an output on or off in Matrix 1.",
            },
            "toggle_crosspoint": {
                "label": "Toggle Cross-Point",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                },
                "help": "Flips an input's feed into an output in Matrix 1.",
            },
            "set_crosspoint_level": {
                "label": "Set Cross-Point Level",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "level": {
                        "type": "number",
                        "required": True,
                        "label": "Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "unit": "dB",
                        "help": "-60.5 to 0 dB in 0.5 dB steps.",
                    },
                },
                "help": "Sets how much of an input feeds an output in Matrix 1.",
            },
            "set_signal_generator_route": {
                "label": "Set Signal Generator to Output",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "on": {"type": "boolean", "required": True, "label": "On"},
                },
                "sets": {"signal_generator": "{on}"},
                "help": "Turns the signal generator's feed into an output on or off in Matrix 1.",
            },
            "set_signal_generator_route_level": {
                "label": "Set Signal Generator to Output Level",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "level": {
                        "type": "number",
                        "required": True,
                        "label": "Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "unit": "dB",
                        "help": "-60.5 to 0 dB in 0.5 dB steps.",
                    },
                },
                "sets": {"signal_generator_level": "{level}"},
                "help": "Sets how much of the signal generator feeds an output in Matrix 1.",
            },
            "set_signal_generator_level": {
                "label": "Set Signal Generator Level",
                "params": {
                    "level": {
                        "type": "number",
                        "required": True,
                        "label": "Level (dB)",
                        "min": -60.5,
                        "max": 12.0,
                        "unit": "dB",
                        "help": "-60.5 to +12 dB in 0.5 dB steps.",
                    },
                },
                "sets": {"signal_generator_level_db": "{level}"},
                "help": "Sets the signal generator's level.",
            },
            "set_signal_generator_mute": {
                "label": "Set Signal Generator Mute",
                "params": {"mute": {"type": "boolean", "required": True, "label": "Mute"}},
                "sets": {"signal_generator_mute": "{mute}"},
                "help": "Mutes or unmutes the signal generator. Unmuted, it plays into every "
                        "output its cross-point is on for.",
            },
            "set_signal_generator_type": {
                "label": "Set Signal Generator Type",
                "params": {
                    "type": {
                        "type": "enum",
                        "required": True,
                        "label": "Type",
                        "values": [
                            {"value": "pink_noise", "label": "Pink Noise"},
                            {"value": "white_noise", "label": "White Noise"},
                            {"value": "tone", "label": "Tone"},
                            {"value": "sweep", "label": "Sweep"},
                        ],
                    },
                },
                "sets": {"signal_generator_type": "{type}"},
                "help": "Chooses what the signal generator plays.",
            },
            "set_signal_generator": {
                "label": "Set Signal Generator Parameter",
                "params": {
                    "parameter": {
                        "type": "enum",
                        "required": True,
                        "label": "Parameter",
                        "values": [
                            {"value": "tone_frequency", "label": "Tone Frequency (Hz)"},
                            {"value": "start_frequency", "label": "Sweep Start Frequency (Hz)"},
                            {"value": "end_frequency", "label": "Sweep End Frequency (Hz)"},
                            {"value": "duration", "label": "Sweep Duration (s)"},
                            {"value": "repeat", "label": "Repeat Sweep (1 = on, 0 = off)"},
                        ],
                    },
                    "value": {
                        "type": "number",
                        "required": True,
                        "label": "Value",
                        "min": 0,
                        "max": 20000,
                        "help": "Frequencies 20 to 20000 Hz; duration 1 to 5 s; 1 or 0 to "
                                "repeat or not.",
                    },
                },
                "help": "Changes the tone's frequency or the sweep's range, length and repeat, "
                        "and returns the value read back.",
            },
            "set_output_limiter": {
                "label": "Set Output Limiter",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "parameter": {
                        "type": "enum",
                        "required": True,
                        "label": "Parameter",
                        "values": [
                            {"value": "peak_threshold", "label": "Peak Threshold (V)"},
                            {"value": "rms_threshold", "label": "RMS Threshold (V)"},
                            {"value": "rms_attack", "label": "RMS Attack (ms)"},
                            {"value": "rms_release", "label": "RMS Release (ms)"},
                            {"value": "link_group", "label": "Link Group (0 = none, 1-4)"},
                        ],
                    },
                    "value": {
                        "type": "number",
                        "required": True,
                        "label": "Value",
                        "min": 0,
                        "max": 10000,
                        "help": (
                            "Peak threshold 0.5 to 71 V and RMS 0.5 to 50 V on an independent "
                            "output, up to 142 V bridged, in 0.5 V steps; RMS attack and release "
                            "500 to 10,000 ms; link group 0 to 4."
                        ),
                    },
                },
                "help": (
                    "Changes one setting of an output's limiter, which protects the loudspeakers. "
                    "A threshold above an independent output's ceiling is refused."
                ),
            },
            "set_output_delay": {
                "label": "Set Output Delay",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "delay_ms": {
                        "type": "number",
                        "required": True,
                        "label": "Delay (ms)",
                        "min": 0,
                        "max": 3000,
                        "unit": "ms",
                        "help": "0 to 3000 ms, sent as samples at 48 kHz.",
                    },
                },
                "help": "Sets an output's delay.",
            },
            "set_output_delay_bypass": {
                "label": "Set Output Delay Bypass",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "bypass": {"type": "boolean", "required": True, "label": "Bypass"},
                },
                "sets": {"delay_bypass": "{bypass}"},
                "help": "Bypasses an output's delay, or puts it back in.",
            },
            "set_output_align_delay": {
                "label": "Set Output Align Delay",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "delay_ms": {
                        "type": "number",
                        "required": True,
                        "label": "Align Delay (ms)",
                        "min": 0,
                        "max": 10,
                        "unit": "ms",
                        "help": "0 to 10 ms, sent as samples at 48 kHz.",
                    },
                },
                "help": "Sets an output's alignment delay, which lines up drivers in one "
                        "loudspeaker.",
            },
            "set_input_eq": {
                "label": "Set Input EQ Band",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "band": {
                        "type": "integer",
                        "required": True,
                        "label": "Band",
                        "min": 1,
                        "max": 5,
                    },
                    "parameter": {
                        "type": "enum",
                        "required": True,
                        "label": "Parameter",
                        "values": [
                            {"value": "frequency", "label": "Frequency (Hz)"},
                            {"value": "q", "label": "Q"},
                            {"value": "gain", "label": "Gain (dB)"},
                            {"value": "slope", "label": "Slope (0, -6, -12 dB/oct)"},
                            {"value": "type", "label": "Type (B, HS, LS, HC, LC, N)"},
                            {"value": "bypass", "label": "Bypass (on / off)"},
                        ],
                    },
                    "value": {"type": "string", "required": True, "label": "Value",
                              "help": "Frequency 20 to 20000 Hz; Q 0.1 to 10; gain -20 to +20 "
                                      "dB; slope 0, -6 or -12; type B (band), HS / LS (shelves), "
                                      "HC / LC (cuts) or N (notch); bypass on or off."},
                },
                "help": (
                    "Changes one band of an input's five-band parametric EQ, and returns the "
                    "value read back."
                ),
            },
            "set_array_eq": {
                "label": "Set Array EQ",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "parameter": {
                        "type": "enum",
                        "required": True,
                        "label": "Parameter",
                        "values": [
                            {"value": "center_frequency", "label": "Center Frequency (Hz)"},
                            {"value": "tilt", "label": "Tilt"},
                            {"value": "gain", "label": "Gain (dB)"},
                            {"value": "array", "label": "Array (modules.coverage, e.g. 4.080)"},
                            {"value": "bypass", "label": "Bypass (on / off)"},
                        ],
                    },
                    "value": {"type": "string", "required": True, "label": "Value",
                              "help": "Center frequency 100 to 4000 Hz; tilt 0 to 10; gain -12 "
                                      "to +2 dB; array as modules and coverage (4.080 is four "
                                      "modules at 80 degrees); bypass on or off."},
                },
                "help": (
                    "Changes the array EQ on an input's channel, for a Bose line array, and "
                    "returns the value read back."
                ),
            },
            "set_speaker_eq": {
                "label": "Set Speaker EQ Band",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "band": {
                        "type": "integer",
                        "required": True,
                        "label": "Band",
                        "min": 1,
                        "max": 9,
                    },
                    "parameter": {
                        "type": "enum",
                        "required": True,
                        "label": "Parameter",
                        "values": [
                            {"value": "frequency", "label": "Frequency (Hz)"},
                            {"value": "q", "label": "Q"},
                            {"value": "gain", "label": "Gain (dB)"},
                            {"value": "type", "label": "Type (B, HS, LS, N)"},
                            {"value": "bypass", "label": "Bypass (on / off)"},
                        ],
                    },
                    "value": {"type": "string", "required": True, "label": "Value",
                              "help": "Frequency 200 to 20000 Hz; Q 0.1 to 10; gain -20 to +20 "
                                      "dB; type B, HS, LS or N; bypass on or off."},
                },
                "help": (
                    "Changes one band of an output's nine-band speaker EQ, and returns the value "
                    "read back."
                ),
            },
            "set_speaker_eq_gain": {
                "label": "Set Speaker EQ Gain",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "gain": {
                        "type": "number",
                        "required": True,
                        "label": "Gain (dB)",
                        "min": -15,
                        "max": 15,
                        "unit": "dB",
                        "help": "-15 to +15 dB in 0.5 dB steps.",
                    },
                },
                "help": "Sets the overall gain of an output's speaker EQ, and returns the value "
                        "read back.",
            },
            "set_band_pass": {
                "label": "Set Band Pass",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "parameter": {
                        "type": "enum",
                        "required": True,
                        "label": "Parameter",
                        "values": [
                            {"value": "hpf_type", "label": "High-Pass Type"},
                            {"value": "hpf_frequency", "label": "High-Pass Frequency (Hz)"},
                            {"value": "hpf_bypass", "label": "High-Pass Bypass (on / off)"},
                            {"value": "lpf_type", "label": "Low-Pass Type"},
                            {"value": "lpf_frequency", "label": "Low-Pass Frequency (Hz)"},
                            {"value": "lpf_bypass", "label": "Low-Pass Bypass (on / off)"},
                        ],
                    },
                    "value": {"type": "string", "required": True, "label": "Value",
                              "help": "A type such as But24, Bes12 or Lin48 (Butterworth, Bessel "
                                      "or Linkwitz-Riley and the slope); a frequency 20 to 20000 "
                                      "Hz; bypass on or off."},
                },
                "help": (
                    "Changes an output's high-pass or low-pass filter, and returns the value read "
                    "back."
                ),
            },
            "recall_parameter_set": {
                "label": "Recall Parameter Set",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "label": "Parameter Set",
                        "min": 1,
                        "max": 255,
                        "options_state": "parameter_set_options",
                        "help": "The Parameter Set number (1-255). Names come from the "
                                "Parameter Sets table.",
                    },
                },
                "sets": {"parameter_set": "{number}"},
                "help": "Recalls a parameter set from the amplifier's design.",
            },
            "set_group_level": {
                "label": "Set Group Level",
                "params": {
                    "group": {
                        "type": "child_id",
                        "child_type": "group",
                        "required": True,
                        "label": "Group",
                    },
                    "level": {
                        "type": "number",
                        "required": True,
                        "label": "Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "unit": "dB",
                        "help": "-60.5 (off) to 0 dB in 0.5 dB steps.",
                    },
                },
                "sets": {"level_db": "{level}"},
                "help": "Sets a volume group's master level.",
            },
            "step_group_level": {
                "label": "Step Group Level",
                "params": {
                    "group": {
                        "type": "child_id",
                        "child_type": "group",
                        "required": True,
                        "label": "Group",
                    },
                    "amount": {
                        "type": "number",
                        "required": True,
                        "label": "Amount (dB)",
                        "default": 1.0,
                        "min": -60.0,
                        "max": 60.0,
                        "help": "Positive raises, negative lowers, in 0.5 dB steps.",
                    },
                },
                "help": "Raises or lowers a volume group's master level by the amount.",
            },
            "set_group_mute": {
                "label": "Set Group Mute",
                "params": {
                    "group": {
                        "type": "child_id",
                        "child_type": "group",
                        "required": True,
                        "label": "Group",
                    },
                    "mute": {"type": "boolean", "required": True, "label": "Mute"},
                },
                "sets": {"mute": "{mute}"},
                "help": "Mutes or unmutes a volume group.",
            },
            "toggle_group_mute": {
                "label": "Toggle Group Mute",
                "params": {
                    "group": {
                        "type": "child_id",
                        "child_type": "group",
                        "required": True,
                        "label": "Group",
                    },
                },
                "help": "Flips a volume group's mute.",
            },
            "read_alarm_status": {
                "label": "Read Alarm Status",
                "params": {},
                "help": "Reads the fault output and each channel's alarm status now.",
            },
            "clear_alarms": {
                "label": "Clear Alarms",
                "params": {},
                "help": (
                    "Clears the amplifier's active alarms and resets its fault output. Any "
                    "condition still present is reported again at once, and the next limiting "
                    "or input clip is reported again."
                ),
            },
            "get_alarm_log": {
                "label": "Read Alarm Log",
                "params": {},
                "help": (
                    "Reads the amplifier's alarm history: one line per alarm with its time, date "
                    "and description. The times are real only if the amplifier has not lost power "
                    "since ControlSpace Designer last connected."
                ),
            },
            "clear_alarm_log": {
                "label": "Clear Alarm Log",
                "params": {},
                "help": "Empties the amplifier's alarm history.",
            },
            "set_module_parameter": {
                "label": "Set Module Parameter",
                "params": {
                    "module_name": {
                        "type": "string",
                        "required": True,
                        "label": "Module Label",
                        "pattern": "^[^\"]{1,64}$",
                        "help": (
                            "The module's label as ControlSpace Designer shows it (Matrix 1, "
                            "Limiter 2, PEQ-5band C, SpeakerPEQ 4)."
                        ),
                    },
                    "index": {
                        "type": "string",
                        "required": True,
                        "label": "Index",
                        "pattern": "^\\s*\\d+(\\s*>\\s*\\d+){0,2}\\s*$",
                        "help": (
                            "Index 1 and, where the module has them, Index 2: 1, 0>2, 3>5 (section "
                            "6.2 of the protocol document)."
                        ),
                    },
                    "value": {
                        "type": "string",
                        "required": True,
                        "label": "Value",
                        "pattern": "^[^\"]{1,32}$",
                        "help": "As the protocol writes it: -6.5, O (on), F (off), T (toggle), "
                                "But24.",
                    },
                },
                "help": "Sets any module parameter by label and index (SA), then reads it back.",
            },
            "query_module_parameter": {
                "label": "Query Module Parameter",
                "params": {
                    "module_name": {
                        "type": "string",
                        "required": True,
                        "label": "Module Label",
                        "pattern": "^[^\"]{1,64}$",
                        "help": (
                            "The module's label as ControlSpace Designer shows it (Matrix 1, "
                            "Limiter 2, PEQ-5band C, SpeakerPEQ 4)."
                        ),
                    },
                    "index": {
                        "type": "string",
                        "required": True,
                        "label": "Index",
                        "pattern": "^\\s*\\d+(\\s*>\\s*\\d+){0,2}\\s*$",
                        "help": (
                            "Index 1 and, where the module has them, Index 2: 1, 0>2, 3>5 (section "
                            "6.2 of the protocol document)."
                        ),
                    },
                },
                "help": "Reads any module parameter by label and index (GA) and returns its value.",
            },
            "set_ip_address": {
                "label": "Set IP Address",
                "params": {
                    "address": {
                        "type": "string",
                        "required": True,
                        "label": "IP Address",
                        "pattern": "^\\d{1,3}(\\.\\d{1,3}){3}$",
                    },
                },
                "help": (
                    "Changes the amplifier's IP address. It takes effect at the next reboot, and "
                    "this device's address in OpenAVC must change with it."
                ),
            },
            "set_network_parameter": {
                "label": "Set Network Parameter",
                "params": {
                    "parameter": {
                        "type": "enum",
                        "required": True,
                        "label": "Parameter",
                        "values": [
                            {"value": "T", "label": "Addressing (D = DHCP, S = static)"},
                            {"value": "M", "label": "Subnet Mask"},
                            {"value": "G", "label": "Default Gateway"},
                        ],
                    },
                    "value": {"type": "string", "required": True, "label": "Value"},
                },
                "help": (
                    "Changes the addressing mode, subnet mask or gateway. Takes effect at the next "
                    "reboot."
                ),
            },
            "reset_network_defaults": {
                "label": "Reset Network Defaults",
                "params": {},
                "confirm": (
                    "Returns every network setting to its factory default at the next reboot, so "
                    "the amplifier may come back on a different address."
                ),
                "help": (
                    "Returns the amplifier's network settings to their defaults at the next "
                    "reboot."
                ),
            },
            "reboot": {
                "label": "Reboot",
                "params": {},
                "confirm": (
                    "Reboots the amplifier. Audio stops until it is back, settings changed since "
                    "the design was last sent revert to it, and this connection drops until then."
                ),
                "help": "Restarts the amplifier, the same as a power cycle.",
            },
            "resync": {
                "label": "Resync",
                "params": {},
                "help": "Reads every value again and renews the subscriptions and alarm reports.",
            },
        },
        "quick_actions": ["power_on", "standby", "recall_parameter_set"],
        "actions": [
            {"id": "power_on", "kind": "command", "icon": "power"},
            {"id": "standby", "kind": "command", "icon": "moon"},
            {"id": "recall_parameter_set", "kind": "command", "icon": "bookmark"},
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection",
                "icon": "search",
                "availability": "always",
                "confirm": (
                    "Asks the amplifier for its standby state, its output configuration, its "
                    "subscription support, its fixed modules, each typed input label and each "
                    "group, on the driver's own connection (or on one of its own when the driver "
                    "is not connected). Nothing on the amplifier changes."
                ),
            },
        ],
    }

    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The amplifier stopped answering (no reply to GY)."
    PROBE_TIMEOUT_S = 3.0
    ACK_TIMEOUT_S = ACK_TIMEOUT_S
    QUERY_TIMEOUT_S = QUERY_TIMEOUT_S
    SUB_TIMEOUT_S = SUB_TIMEOUT_S
    ALARM_LOG_TIMEOUT_S = ALARM_LOG_TIMEOUT_S
    STANDBY_SETTLE_S = STANDBY_SETTLE_S

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        self._problems: list[str] = []
        self._input_labels, problems = read_input_labels(config)
        self._problems.extend(problems)
        self._groups, problems = parse_groups_config(config.get("groups") or [])
        self._problems.extend(problems)
        self._parameter_set_names, problems = parse_parameter_sets_config(config.get("parameter_sets") or [])
        self._problems.extend(problems)
        # How many channels the amplifier has: the Channels setting until it
        # reports its configuration (GC).
        self._n = configured_channels(config)
        # A standing problem that is not a refused read: the configuration
        # unanswered.
        self._standing: dict[str, str] = {}
        # Module reads the amplifier refused on this connection: label (or
        # GET text) -> why.
        self._refused: dict[str, str] = {}
        # Which of the two Array EQ labels answered: 0 = "PEQ-5band X"
        # (6.2.6's prose), 1 = "Array EQ X" (its examples).
        self._array_eq_style = 0
        # (module label lowered, index path) -> (child type or "", channel, prop, kind)
        self._route: dict[tuple[str, tuple[str, ...]], tuple[str, int, str, str]] = {}
        mx = MATRIX_LABEL.lower()
        for o in range(1, MAX_CHANNELS + 1):
            for i in list(range(1, MAX_CHANNELS + 1)) + [SIGGEN_INPUT]:
                idx = str(crosspoint_index(i, o))
                prop = crosspoint_prop(i)
                self._route[(mx, ("1", idx))] = (OUTPUT_CHILD_TYPE, o, prop, "onoff")
                self._route[(mx, ("2", idx))] = (OUTPUT_CHILD_TYPE, o, f"{prop}_level", "number")
            for index2, prop, kind, *_rest in LIMITER_PARAMS.values():
                self._route[(limiter_label(o).lower(), ("0", index2))] = (OUTPUT_CHILD_TYPE, o, prop, kind)
            self._route[(delay_label(o).lower(), ("1", "1"))] = (OUTPUT_CHILD_TYPE, o, "delay_ms", "samples")
            self._route[(delay_label(o).lower(), ("1", "2"))] = (OUTPUT_CHILD_TYPE, o, "delay_bypass", "onoff")
            self._route[(delay_label(o).lower(), ("1", "3"))] = (OUTPUT_CHILD_TYPE, o, "align_delay_ms", "samples")
            self._route[(speaker_peq_label(o).lower(), ("0", "4"))] = (
                OUTPUT_CHILD_TYPE, o, "polarity_inverted", "onoff")
        for n, label in self._input_labels.items():
            key = label.lower()
            self._route[(key, (INPUT_SENSITIVITY_INDEX,))] = (INPUT_CHILD_TYPE, n, "sensitivity_dbu", "int")
            self._route[(key, (INPUT_MUTE_INDEX,))] = (INPUT_CHILD_TYPE, n, "mute", "onoff")
            self._route[(key, (INPUT_SOURCE_INDEX,))] = (INPUT_CHILD_TYPE, n, "source", "source")
        sg = SIGGEN_LABEL.lower()
        self._route[(sg, (SIGGEN_INDEX, "1"))] = ("", 0, "signal_generator_level_db", "number")
        self._route[(sg, (SIGGEN_INDEX, "3"))] = ("", 0, "signal_generator_mute", "onoff")
        self._route[(sg, (SIGGEN_INDEX, "4"))] = ("", 0, "signal_generator_type", "siggen_type")
        self._waiters: list[tuple[Callable[[bytes], bool], asyncio.Future[bytes]]] = []
        self._push_supported = False
        # Live values a subscribing amplifier would not subscribe, read every
        # poll instead.
        self._polled_gets: list[str] = []
        # Set when the fault query drew silence: the status reads then wait
        # for the next resync rather than costing a timeout every poll.
        self._status_silent = False
        # The alarm log block (5.13) while it arrives, and who waits for it.
        self._gh_lines: list[str] | None = None
        self._gh_waiter: asyncio.Future[list[str]] | None = None
        self._send_lock = asyncio.Lock()
        # When the amplifier last sent anything (event-loop time).
        self._last_rx: float | None = None
        self._last_resync: float | None = None
        self._meter_task: asyncio.Task | None = None
        self._standby_task: asyncio.Task | None = None
        self._side_tasks: set[asyncio.Task] = set()
        super().__init__(device_id, config, state, events)
        for problem in self._problems:
            log.warning(f"[{self.device_id}] Settings: {problem}")

    # ── Transport ──

    def _transport_kwargs(self, transport_type: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        kwargs["delimiter"] = None
        return kwargs

    def _create_frame_parser(self) -> CallableFrameParser:
        return CallableFrameParser(parse_powermatch_stream)

    # ── Lifecycle ──

    async def _initial_sync(self) -> None:
        self.set_state("parameter_set_options", json.dumps(
            [{"value": n, "label": f"{n}: {name}"} for n, name in sorted(self._parameter_set_names.items())]
        ))
        self._refused.clear()
        self._standing.clear()
        self._status_silent = False
        self._gh_lines = None
        self.set_states({"ac_mains_loss": False, "digital_audio_loss": False})
        self._register_children()
        await self._read_power()
        await self._read_configuration()
        self._publish_problems()
        await self._detect_push_support()
        for line in ("IP", "NP T", "NP M", "NP G"):
            await self._read_network(line)
        await self._resync()
        await self._read_alarm_log_quietly()
        self._start_meter_loop()

    async def poll(self) -> None:
        """Standby every poll (it has no subscription), then the fault output
        and alarm status; every live value too on an amplifier without
        subscriptions; the subscriptions and alarm reports renewed and the
        configuration, limiter, delay and sensitivity read every Resync
        interval. Stops at the first silence and lets the platform's checks
        judge it."""
        if await self._read_power() is None:
            return
        due = self._last_resync is None or (
            asyncio.get_running_loop().time() - self._last_resync >= self._resync_interval()
        )
        if due:
            await self._read_configuration()
            await self._resync()
        else:
            if not self._push_supported:
                await self._read_live(levels=False)
            elif self._polled_gets:
                # Values the amplifier would not subscribe.
                await self._read_list(self._polled_gets)
            await self._read_status()
        self._publish_problems()

    async def _close_session(self) -> None:
        self._stop_meter_loop()
        task = self._standby_task
        self._standby_task = None
        if task is not None and not task.done():
            task.cancel()
        for t in list(self._side_tasks):
            if not t.done():
                t.cancel()
        self._side_tasks.clear()
        for _, fut in self._waiters:
            if not fut.done():
                fut.cancel()
        self._waiters.clear()
        waiter = self._gh_waiter
        self._gh_waiter = None
        self._gh_lines = None
        if waiter is not None and not waiter.done():
            waiter.cancel()

    def _register_children(self) -> None:
        """Inputs and outputs 1..n for the channel count in force, and the
        groups from the table. A roster that shrank (an amplifier reporting
        four channels where eight were set) drops the rest."""
        wanted = list(range(1, self._n + 1))
        for ctype in (INPUT_CHILD_TYPE, OUTPUT_CHILD_TYPE):
            for local in self.list_children(ctype):
                if local not in wanted:
                    self.deregister_child(ctype, local)
        for n in wanted:
            if not self.is_child_registered(INPUT_CHILD_TYPE, n):
                self.register_child(INPUT_CHILD_TYPE, n,
                                    initial_state={"name": f"Input {input_letter(n)}", "clipping": False})
            if not self.is_child_registered(OUTPUT_CHILD_TYPE, n):
                self.register_child(OUTPUT_CHILD_TYPE, n,
                                    initial_state={"name": f"Output {n}", "limiting": False})
        for number, name in self._groups.items():
            if not self.is_child_registered(GROUP_CHILD_TYPE, number):
                self.register_child(GROUP_CHILD_TYPE, number,
                                    initial_state={"name": name, "responding": False})

    async def refresh_children(self) -> dict[str, Any]:
        await self._read_configuration()
        self._register_children()
        await self._resync()
        return {"inputs": self._n, "outputs": self._n, "groups": len(self._groups)}

    def _resync_interval(self) -> float:
        try:
            return max(10.0, float(self.config.get("resync_interval", DEFAULT_RESYNC_INTERVAL)))
        except (TypeError, ValueError):
            return float(DEFAULT_RESYNC_INTERVAL)

    def _publish_problems(self) -> None:
        """Setting problems, standing conditions and every module the
        amplifier refused, written again on every poll while they hold."""
        refused = [f"{label}: {why}" for label, why in sorted(self._refused.items())]
        self.set_state("config_problems", "; ".join(
            self._problems + list(self._standing.values()) + refused))

    # ── Sending ──

    async def _send_line(self, text: str) -> None:
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
    def _nak_message(frame: bytes) -> str:
        code = frame[1:].decode("ascii", errors="replace").strip()
        return NAK_CODES.get(code, f"the amplifier rejected the command (NAK {code or '?'})")

    @staticmethod
    def _nak_code(frame: bytes) -> str:
        return frame[1:].decode("ascii", errors="replace").strip()

    async def _acked(self, line: str) -> None:
        """A command answered with ACK or NAK (SA, CF, CH): await it. A NAK
        becomes the error the user sees; silence is tolerated, and the
        read-back that follows shows what the amplifier holds."""
        async with self._send_lock:
            fut = self._wait_for(lambda f: bool(f) and f[0] in (ACK, NAK))
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
            raise _Refused(message, self._nak_code(frame))

    async def _query(self, line: str, pred: Callable[[bytes], bool],
                     timeout: float | None = None) -> bytes | None:
        """Send a query and await the reply ``pred`` accepts (or a NAK, which
        raises ``_Refused``). None on silence. The reply also routes to state
        through on_data_received."""
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
            raise _Refused(self._nak_message(frame), self._nak_code(frame))
        return frame

    async def _fire(self, line: str) -> None:
        """A system or device command: the amplifier answers nothing."""
        async with self._send_lock:
            await self._send_line(line)

    # ── Reads and subscriptions ──

    @staticmethod
    def _text(frame: bytes) -> str:
        return frame.decode("ascii", "replace").strip()

    async def _read_power(self) -> bytes | None:
        return await self._query("GY", lambda f: bool(_GY_RE.match(self._text(f))))

    async def _read_configuration(self) -> list[str] | None:
        """``GC`` (5.9): each channel's output configuration, which also says
        how many channels the amplifier has. Routed in _route_line; silence
        keeps the Channels setting and is reported."""
        try:
            reply = await self._query("GC", lambda f: parse_configuration(self._text(f)) is not None)
        except ValueError as exc:
            reply = None
            log.warning(f"[{self.device_id}] GC: {exc}")
        if reply is None:
            self._standing["configuration"] = (
                f"the amplifier did not report its output configuration (GC); "
                f"{self._n} channels are taken from the Channels setting")
            return None
        self._standing.pop("configuration", None)
        return parse_configuration(self._text(reply))

    def _apply_configuration(self, modes: list[str]) -> None:
        count = len(modes)
        if count in CHANNEL_CHOICES:
            self.set_state("channel_count", count)
            if count != self._n:
                log.info(f"[{self.device_id}] The amplifier reports {count} channels "
                         f"(the Channels setting says {self._n})")
                self._n = count
                self._register_children()
        for n, mode in enumerate(modes[:self._n], 1):
            self.set_child_state_batch(OUTPUT_CHILD_TYPE, n, {"output_mode": mode})

    async def _read_network(self, line: str) -> None:
        def matched(frame: bytes) -> bool:
            text = self._text(frame)
            if line == "IP":
                return bool(_IP_RE.match(text))
            m = _NP_RE.match(text)
            return bool(m) and m.group(1).upper() == line[-1]
        try:
            await self._query(line, matched)
        except ValueError as exc:
            log.debug(f"[{self.device_id}] {line}: {exc}")

    async def _detect_push_support(self) -> None:
        try:
            reply = await self._query("SUB", lambda f: bool(_SUB_SUPPORT_RE.match(self._text(f))))
        except ValueError:
            reply = None
        supported = bool(reply) and self._text(reply).lower().endswith("yes")
        self._push_supported = supported
        self.set_state("push_supported", supported)
        if not supported:
            log.info(f"[{self.device_id}] The amplifier did not answer SUB yes: reading every "
                     f"value each poll instead of subscribing")

    def _matrix_inputs(self) -> list[int]:
        return list(range(1, self._n + 1)) + [SIGGEN_INPUT]

    def _live_gets(self, levels: bool = True) -> list[str]:
        """Every subscribable value the driver follows. Without ``levels``
        the 72 cross-point levels of an eight-channel matrix are left out
        (a polled amplifier reads them every Resync interval)."""
        out: list[str] = ["GS"]
        for n in range(1, self._n + 1):
            slot, ch = output_slot(n)
            out.append(f"GV {slot},{ch:x}")
            out.append(f"GM {slot},{ch:x}")
        for n in range(1, self._n + 1):
            slot, ch = input_slot(n)
            out.append(f"GM {slot},{ch:x}")
        for o in range(1, self._n + 1):
            for i in self._matrix_inputs():
                idx = crosspoint_index(i, o)
                out.append(ga_line(MATRIX_LABEL, f"1>{idx}"))
                if levels:
                    out.append(ga_line(MATRIX_LABEL, f"2>{idx}"))
        for o in range(1, self._n + 1):
            out.append(ga_line(speaker_peq_label(o), SPEAKER_POLARITY_PATH))
        for index2 in ("1", "3", "4"):
            out.append(ga_line(SIGGEN_LABEL, f"{SIGGEN_INDEX}>{index2}"))
        for n, label in sorted(self._input_labels.items()):
            if n <= self._n:
                out.append(ga_line(label, INPUT_SOURCE_INDEX))
        for number in sorted(self._groups):
            out.append(f"GG {number:x}")
            out.append(f"GN {number:x}")
        return out

    def _level_gets(self) -> list[str]:
        return [ga_line(MATRIX_LABEL, f"2>{crosspoint_index(i, o)}")
                for o in range(1, self._n + 1) for i in self._matrix_inputs()]

    def _setting_gets(self) -> list[str]:
        """Values read at connect and every Resync interval, not subscribed:
        they change only when someone tunes the system."""
        out: list[str] = []
        for o in range(1, self._n + 1):
            for index2, *_ in LIMITER_PARAMS.values():
                out.append(ga_line(limiter_label(o), f"0>{index2}"))
            for index2 in ("1", "2", "3"):
                out.append(ga_line(delay_label(o), f"1>{index2}"))
        for n, label in sorted(self._input_labels.items()):
            if n <= self._n:
                out.append(ga_line(label, INPUT_SENSITIVITY_INDEX))
        return out

    async def _resync(self) -> None:
        self._last_resync = asyncio.get_running_loop().time()
        self._status_silent = False
        if self.config.get("alarm_notifications", True):
            # 5.10, 5.12: not retained on power down, so armed again here.
            await self._fire("SF O")
            await self._fire("SR O")
        if self._push_supported:
            self._polled_gets = []
            for text in self._live_gets():
                if self._is_refused(text) or await self._sub(text):
                    continue
                # Not subscribed: read it now (a refusal names its reason)
                # and with the poll from here on.
                if await self._read_one(text):
                    self._polled_gets.append(text)
        else:
            await self._read_live(levels=True)
        await self._read_list(self._setting_gets())
        await self._read_status()
        self._publish_problems()

    def _is_refused(self, get_text: str) -> bool:
        if get_text in self._refused:
            return True
        parsed = parse_ga_reply(get_text + "=")
        return bool(parsed) and parsed[0] in self._refused

    def _note_refusal(self, get_text: str, exc: Exception) -> None:
        """A module read the amplifier refused: an unknown label (NAK 01)
        names the module, any other refusal names the read. Either is
        skipped for the rest of the connection and written to
        config_problems until then (Resync asks again)."""
        parsed = parse_ga_reply(get_text + "=")
        if not parsed:
            log.warning(f"[{self.device_id}] {get_text}: {exc}")
            return
        if getattr(exc, "code", "") == "01":
            typed = [n for n, label in self._input_labels.items() if label == parsed[0]]
            if typed:
                self._refused[parsed[0]] = (
                    f"Input {input_letter(typed[0])} label was not accepted ({exc}). Check it "
                    f"against ControlSpace Designer")
            else:
                self._refused[parsed[0]] = f"no module with this label ({exc})"
        else:
            self._refused[get_text] = f"refused ({exc})"
        log.warning(f"[{self.device_id}] {get_text}: {exc}")

    async def _sub(self, get_text: str) -> bool:
        def matched(frame: bytes) -> bool:
            m = _SUB_RE.match(self._text(frame))
            return bool(m) and same_get(m.group(2), get_text)
        try:
            reply = await self._query(sub_line(get_text), matched, self.SUB_TIMEOUT_S)
        except ValueError as exc:
            self._note_refusal(get_text, exc)
            return False
        if reply is None:
            log.debug(f"[{self.device_id}] No answer to SUB {get_text!r}")
            return False
        ok = self._text(reply).lower().endswith("yes")
        if not ok:
            log.debug(f"[{self.device_id}] Subscription not accepted for {get_text}; reading it instead")
        return ok

    async def _read_live(self, levels: bool) -> None:
        texts = self._live_gets(levels=levels)
        await self._read_list([t for t in texts if not self._is_refused(t)])

    async def _read_list(self, texts: list[str]) -> None:
        for text in texts:
            await self._read_one(text)

    async def _read_one(self, text: str) -> bool:
        """Read one value; whether the amplifier answered it. A refusal is
        noted (and skipped from then on)."""
        if self._is_refused(text):
            return False
        try:
            reply = await self._query(text, lambda f, t=text: self._reply_to(f, t))
        except ValueError as exc:
            self._note_refusal(text, exc)
            return False
        return reply is not None

    async def _read_status(self) -> dict[str, Any] | None:
        """The fault output (5.10) and each channel's alarm status (5.12).
        Silence on the fault query parks these until the next resync."""
        if self._status_silent:
            return None
        try:
            gf = await self._query("GF", lambda f: bool(_GF_RE.match(self._text(f))))
        except ValueError as exc:
            log.warning(f"[{self.device_id}] GF: {exc}")
            gf = None
        if gf is None:
            self._status_silent = True
            return None
        channels: dict[int, str | None] = {}
        for c in range(1, self._n + 1):
            try:
                reply = await self._query(f"GR {c:x}", lambda f, n=c: self._gr_reply(f, n))
            except ValueError as exc:
                log.warning(f"[{self.device_id}] GR {c}: {exc}")
                reply = None
            channels[c] = self._text(reply) if reply else None
        return {"fault": self._text(gf), "channels": channels}

    @staticmethod
    def _gr_reply(frame: bytes, channel: int) -> bool:
        m = _GR_STATUS_RE.match(frame.decode("ascii", "replace").strip())
        return bool(m) and int(m.group(1), 16) == channel

    @staticmethod
    def _reply_to(frame: bytes, get_text: str) -> bool:
        """Whether a frame answers the GET it was asked with."""
        text = frame.decode("ascii", "replace").strip()
        if get_text.startswith("GA"):
            sent = parse_ga_reply(get_text + "=")
            got = parse_ga_reply(text)
            return bool(sent and got and sent[0].lower() == got[0].lower() and sent[1] == got[1])
        if get_text == "GS":
            return bool(_S_RE.match(text))
        head = get_text.split()[0]
        args = get_text[len(head):].strip().lower().split(",")
        pattern = {"GV": _GV_RE, "GM": _GM_RE, "GG": _GG_RE, "GN": _GN_RE}.get(head)
        if pattern is None:
            return False
        m = pattern.match(text)
        if not m:
            return False
        return [g.lower().lstrip("0") or "0" for g in m.groups()[:len(args)]] == \
            [a.lstrip("0") or "0" for a in args]

    async def _read_back_power_later(self) -> None:
        try:
            await asyncio.sleep(self.STANDBY_SETTLE_S)
            if self._link_alive():
                await self._read_power()
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug(f"[{self.device_id}] Standby read-back failed", exc_info=True)

    async def _reread_alarm(self, channel: int) -> None:
        """A channel's alarm cleared: read what is left on it."""
        try:
            await self._query(f"GR {channel:x}", lambda f, n=channel: self._gr_reply(f, n))
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug(f"[{self.device_id}] Alarm re-read failed", exc_info=True)

    async def _read_alarm_log(self) -> list[str]:
        """``GH`` answers a bracketed block, one alarm per line (5.13)."""
        async with self._send_lock:
            fut: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()
            self._gh_waiter = fut
            nak = self._wait_for(lambda f: bool(f) and f[0] == NAK)
            try:
                await self._send_line("GH")
                done, _ = await asyncio.wait({fut, nak}, timeout=self.ALARM_LOG_TIMEOUT_S,
                                             return_when=asyncio.FIRST_COMPLETED)
            finally:
                self._drop_waiter(nak)
                if self._gh_waiter is fut:
                    self._gh_waiter = None
            if nak in done:
                raise ValueError(self._nak_message(nak.result()))
            if fut not in done:
                self._gh_lines = None
                raise TimeoutError("no complete reply to GH")
            return fut.result()

    async def _read_alarm_log_quietly(self) -> None:
        try:
            await self._read_alarm_log()
        except (ValueError, TimeoutError) as exc:
            log.info(f"[{self.device_id}] Alarm log not read: {exc}")

    # ── Meters ──

    def _meters_enabled(self) -> bool:
        return bool(self.config.get("enable_meters", False))

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
        """Get Signal Level for the inputs and the outputs; how many
        answered."""
        answered = 0
        for slot in (GL_INPUTS, GL_OUTPUTS):
            try:
                reply = await self._query(f"GL {slot}", lambda f, s=slot: self._gl_reply(f, s))
            except ValueError as exc:
                log.warning(f"[{self.device_id}] GL {slot}: {exc}")
                continue
            if reply is not None:
                answered += 1
        return answered

    @staticmethod
    def _gl_reply(frame: bytes, slot: str) -> bool:
        m = _GL_RE.match(frame.decode("ascii", "replace").strip())
        return bool(m) and m.group(1).lower() == slot and not m.group(2)

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        # Nothing here awaits before the frame is applied: each frame runs as
        # a task of its own, and a pause would let the next one apply first.
        if not data:
            return
        self._last_rx = asyncio.get_running_loop().time()
        if data[0] in (ACK, NAK):
            if not self._dispatch_waiters(data) and data[0] == NAK:
                self.set_state("last_error", self._nak_message(data))
            return
        text = data.decode("ascii", errors="replace").strip()
        if not text:
            return
        if self._gh_lines is not None and not self._is_known_reply(text):
            self._collect_alarm_line(text)
            return
        m = _GH_START_RE.match(text)
        if m:
            self._gh_lines = []
            self._collect_alarm_line(m.group(1))
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

    @staticmethod
    def _is_known_reply(text: str) -> bool:
        """A reply or pushed value that can arrive in the middle of the alarm
        log block (subscriptions and alarm reports go out on the same
        connection)."""
        return bool(parse_ga_reply(text) or _S_RE.match(text) or _GG_RE.match(text)
                    or _GN_RE.match(text) or _GV_RE.match(text) or _GM_RE.match(text)
                    or _GL_RE.match(text) or _GY_RE.match(text) or _GF_RE.match(text)
                    or _GR_STATUS_RE.match(text) or _GR_EVENT_RE.match(text)
                    or parse_configuration(text) is not None or _SUB_RE.match(text)
                    or _SUB_SUPPORT_RE.match(text) or _IP_RE.match(text) or _NP_RE.match(text))

    def _collect_alarm_line(self, text: str) -> None:
        lines = self._gh_lines
        if lines is None:
            return
        body = text.strip()
        done = body.endswith("]")
        if done:
            body = body[:-1].strip()
        if body:
            lines.append(body)
        if not done:
            return
        self._gh_lines = None
        self.set_states({"alarm_count": len(lines), "alarm_log": " | ".join(lines)})
        waiter = self._gh_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(lines)

    def _route_line(self, text: str) -> None:
        reply = parse_ga_reply(text)
        if reply is not None:
            self._apply_module_reply(*reply)
            return
        m = _GY_RE.match(text)
        if m:
            self.set_state("power", m.group(1).upper() == "N")
            return
        m = _GR_EVENT_RE.match(text)
        if m:
            self._apply_alarm_event(int(m.group(1), 16), m.group(2).upper(), m.group(3).upper(),
                                    m.group(4).upper())
            return
        m = _GR_STATUS_RE.match(text)
        if m:
            self._apply_alarm_status(int(m.group(1), 16), m.group(2).upper(), m.group(3).upper())
            return
        m = _GF_RE.match(text)
        if m:
            self.set_state("fault", m.group(1).upper() == "F")
            return
        modes = parse_configuration(text)
        if modes is not None:
            self._apply_configuration(modes)
            return
        m = _GV_RE.match(text)
        if m:
            where = channel_of_slot(m.group(1), int(m.group(2), 16))
            if where and where[0] == "output" and where[1] <= self._n:
                self.set_child_state_batch(OUTPUT_CHILD_TYPE, where[1],
                                           {"level_db": hex_level_to_db(int(m.group(3), 16))})
            return
        m = _GM_RE.match(text)
        if m:
            where = channel_of_slot(m.group(1), int(m.group(2), 16))
            if where and where[1] <= self._n:
                ctype = OUTPUT_CHILD_TYPE if where[0] == "output" else INPUT_CHILD_TYPE
                self.set_child_state_batch(ctype, where[1], {"mute": m.group(3).upper() == "M"})
            return
        m = _S_RE.match(text)
        if m:
            n = int(m.group(1), 16)
            self.set_states({"parameter_set": n,
                             "parameter_set_name": self._parameter_set_names.get(
                                 n, "" if n == 0 else f"Parameter Set {n}")})
            return
        m = _GG_RE.match(text)
        if m:
            n = int(m.group(1), 16)
            if n in self._groups:
                self.set_child_state_batch(GROUP_CHILD_TYPE, n, {
                    "level_db": hex_level_to_db(int(m.group(2), 16)), "responding": True})
            return
        m = _GN_RE.match(text)
        if m:
            n = int(m.group(1), 16)
            if n in self._groups:
                self.set_child_state_batch(GROUP_CHILD_TYPE, n, {
                    "mute": m.group(2).upper() == "M", "responding": True})
            return
        m = _GL_RE.match(text)
        if m and not m.group(2):
            self._apply_signal_levels(m.group(1).lower(), m.group(3))
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
        if _SUB_RE.match(text) or _SUB_SUPPORT_RE.match(text):
            return
        log.debug(f"[{self.device_id}] Unhandled line: {text!r}")

    def _apply_alarm_status(self, channel: int, severity: str, kind: str) -> None:
        """A query's reply ``GR c,s,t``: the channel's most severe standing
        alarm."""
        if not 1 <= channel <= self._n:
            return
        sev = ALARM_SEVERITY.get(severity, "none")
        alarm = ALARM_TYPE.get(kind, "other")
        if alarm in ("limiting", "clip"):
            alarm = "other"
        if sev == "none":
            alarm = "none"
        self.set_child_state_batch(OUTPUT_CHILD_TYPE, channel, {"alarm_severity": sev, "alarm": alarm})

    def _apply_alarm_event(self, channel: int, severity: str, kind: str, condition: str) -> None:
        """A notification ``GR c,s,t,x`` (5.12). Limiting goes to the output,
        an input clip to the input; channel 0 is the amplifier's own (AC
        mains, digital audio); any other alarm is the channel's standing
        state, and its clear reads what is left on the channel."""
        sev = ALARM_SEVERITY.get(severity, "none")
        alarm = ALARM_TYPE.get(kind, "other")
        cleared = condition == "C"
        where = "Amplifier" if channel == 0 else f"Channel {channel}"
        words = ALARM_WORDS.get(alarm, alarm)
        self.set_state("last_alarm", f"{where}: {words} ({sev}){' cleared' if cleared else ''}")
        if channel == 0 or alarm in ("ac_loss", "digital_audio_loss"):
            if alarm == "ac_loss":
                self.set_state("ac_mains_loss", not cleared)
            elif alarm == "digital_audio_loss":
                self.set_state("digital_audio_loss", not cleared)
            return
        if not 1 <= channel <= self._n:
            return
        if alarm == "limiting":
            self.set_child_state_batch(OUTPUT_CHILD_TYPE, channel, {"limiting": not cleared})
            return
        if alarm == "clip":
            self.set_child_state_batch(INPUT_CHILD_TYPE, channel, {"clipping": not cleared})
            return
        if cleared:
            self._spawn(self._reread_alarm(channel))
            return
        self.set_child_state_batch(OUTPUT_CHILD_TYPE, channel, {
            "alarm_severity": sev, "alarm": "none" if alarm == "none" else alarm})

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    def _apply_module_reply(self, label: str, idx: tuple[str, ...], raw: str) -> None:
        route = self._route.get((label.lower(), idx))
        if route is None:
            log.debug(f"[{self.device_id}] GA for a value the driver does not follow: "
                      f"{label!r} {'>'.join(idx)}")
            return
        ctype, channel, prop, kind = route
        value: Any
        try:
            if kind == "onoff":
                value = parse_onoff(raw)
            elif kind == "source":
                on = parse_onoff(raw)
                value = None if on is None else ("digital" if on else "analog")
            elif kind == "siggen_type":
                value = SIGGEN_TYPES.get(str(int(float(raw))))
            elif kind == "samples":
                value = samples_to_ms(int(float(raw)))
            elif kind == "int":
                value = int(round(float(raw)))
            else:
                value = round(float(raw), 3)
        except ValueError:
            log.warning(f"[{self.device_id}] Could not read {raw!r} for {label} {'>'.join(idx)}")
            return
        if value is None:
            return
        if not ctype:
            self.set_state(prop, value)
            return
        if 1 <= channel <= self._n:
            self.set_child_state_batch(ctype, channel, {prop: value})

    def _apply_signal_levels(self, slot: str, body: str) -> None:
        if slot == GL_INPUTS:
            ctype = INPUT_CHILD_TYPE
        elif slot == GL_OUTPUTS:
            ctype = OUTPUT_CHILD_TYPE
        else:
            return
        try:
            raws = [int(tok.strip(), 16) for tok in body.split(",") if tok.strip()]
        except ValueError:
            log.warning(f"[{self.device_id}] Signal levels for slot {slot} are not hexadecimal: {body!r}")
            return
        for c, raw in enumerate(raws[:self._n], 1):
            self.set_child_state_batch(ctype, c, {"signal_level": meter_to_db(raw)})

    # ── Liveness ──

    async def _liveness_probe(self) -> None:
        """``GY`` answers in either power state. Anything received within the
        last interval already proves the link, so only a quiet link is asked
        (otherwise the probe can wait behind queries holding the send lock
        through their own timeouts and count as a miss)."""
        last = self._last_rx
        if last is not None and asyncio.get_running_loop().time() - last < self.HEALTH_INTERVAL_S:
            return
        reply = await self._query("GY", lambda f: bool(_GY_RE.match(self._text(f))), self.PROBE_TIMEOUT_S)
        if reply is None:
            raise TimeoutError("no reply to GY")

    # ── Commands ──

    def _channel(self, params: dict[str, Any], key: str) -> int:
        what = "an input, 1" if key == "input" else "an output, 1"
        try:
            c = int(str(params.get(key, "")).strip())
        except ValueError as exc:
            raise CommandParamError(f"{key} is {what} to {self._n}") from exc
        if not 1 <= c <= self._n:
            raise CommandParamError(f"{key} is {what} to {self._n}")
        return c

    def _group(self, params: dict[str, Any]) -> int:
        try:
            n = int(str(params.get("group", "")).strip())
        except ValueError as exc:
            raise CommandParamError("group is a group number") from exc
        if n not in self._groups:
            raise CommandParamError(f"group {n} is not in the Volume Groups table")
        return n

    def _input_label(self, n: int, what: str) -> str:
        label = self._input_labels.get(n)
        if not label:
            raise CommandParamError(f"Type Input {input_letter(n)}'s label in the device settings "
                                    f"to set its {what}")
        return label

    def _output_muted(self, c: int) -> bool:
        return self.get_child_state(OUTPUT_CHILD_TYPE, c).get("mute") is True

    async def _ga_read(self, label: str, path: str) -> str | None:
        """Read one module parameter; its value (also routed to state)."""
        text = ga_line(label, path)
        reply = await self._query(text, lambda f, t=text: self._reply_to(f, t))
        if reply is None:
            return None
        parsed = parse_ga_reply(self._text(reply))
        return parsed[2] if parsed else None

    async def _module_set(self, label: str, path: str, value: str) -> str | None:
        await self._acked(sa_line(label, path, value))
        return await self._ga_read(label, path)

    async def _array_eq_set(self, n: int, index2: str, value: str) -> str | None:
        """6.2.6 gives the array EQ two labels. Use the one that answered;
        when the amplifier refuses one as an unknown label or index (NAK 01
        or 02), try the other, and keep whichever it took."""
        labels = array_eq_labels(n)
        order = [self._array_eq_style, 1 - self._array_eq_style]
        last: Exception | None = None
        for style in order:
            try:
                result = await self._module_set(labels[style], f"{ARRAY_EQ_BAND}>{index2}", value)
            except _Refused as exc:
                if exc.code not in ("01", "02"):
                    raise
                last = exc
                continue
            self._array_eq_style = style
            return result
        raise ValueError(f"the amplifier refused the array EQ as '{labels[0]}' and as "
                         f"'{labels[1]}' ({last})")

    async def _slot_read(self, text: str) -> None:
        await self._query(text, lambda f, t=text: self._reply_to(f, t))

    @staticmethod
    def _onoff_param(params: dict[str, Any], key: str) -> str:
        return "O" if coerce_onoff(params.get(key)) else "F"

    @staticmethod
    def _matrix_level(params: dict[str, Any]) -> str:
        level = float(params["level"])
        if not LEVEL_MIN <= level <= LEVEL_MAX:
            raise CommandParamError("a cross-point level is -60.5 to 0 dB")
        return format_number(round(level * 2) / 2)

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if command in ("power_on", "standby"):
            await self._fire("SY N" if command == "power_on" else "SY S")
            await self._read_power()
            self._schedule_power_read()
            return None
        if command in ("set_output_level", "step_output_level"):
            c = self._channel(params, "output")
            if self._output_muted(c):
                raise CommandParamError(
                    f"Output {c} is muted, and the amplifier ignores a level change while an "
                    f"output is muted. Unmute it first.")
            slot, ch = output_slot(c)
            if command == "set_output_level":
                await self._fire(f"SV {slot},{ch:x},{db_to_hex_level(float(params['level']))}")
            else:
                amount = float(params.get("amount", 1.0))
                steps = int(round(abs(amount) * 2))
                if steps == 0:
                    return None
                await self._fire(f"SI {slot},{ch:x},{1 if amount > 0 else 0},{steps:x}")
            await self._slot_read(f"GV {slot},{ch:x}")
            return None
        if command in ("set_output_mute", "toggle_output_mute", "set_input_mute", "toggle_input_mute"):
            key = "output" if "output" in command else "input"
            c = self._channel(params, key)
            slot, ch = output_slot(c) if key == "output" else input_slot(c)
            state = "T" if command.startswith("toggle") else ("M" if coerce_onoff(params.get("mute")) else "U")
            await self._fire(f"SM {slot},{ch:x},{state}")
            await self._slot_read(f"GM {slot},{ch:x}")
            return None
        if command == "set_output_polarity":
            c = self._channel(params, "output")
            await self._module_set(speaker_peq_label(c), SPEAKER_POLARITY_PATH, self._onoff_param(params, "inverted"))
            return None
        if command == "set_input_source":
            c = self._channel(params, "input")
            label = self._input_label(c, "source")
            source = str(params.get("source", "")).strip().lower()
            if source not in ("analog", "digital"):
                raise CommandParamError("source is analog or digital")
            # 6.2.1 Source: O = On (the digital input), F = Analog.
            await self._module_set(label, INPUT_SOURCE_INDEX, "O" if source == "digital" else "F")
            return None
        if command == "set_input_sensitivity":
            c = self._channel(params, "input")
            label = self._input_label(c, "sensitivity")
            try:
                dbu = int(float(str(params.get("sensitivity", "")).strip()))
            except ValueError as exc:
                raise CommandParamError("sensitivity is 0, 4, 12 or 24 dBu") from exc
            if dbu not in SENSITIVITIES_DBU:
                raise CommandParamError("sensitivity is 0, 4, 12 or 24 dBu")
            await self._module_set(label, INPUT_SENSITIVITY_INDEX, str(dbu))
            return None
        if command in ("set_crosspoint", "toggle_crosspoint", "set_crosspoint_level"):
            i = self._channel(params, "input")
            o = self._channel(params, "output")
            idx = crosspoint_index(i, o)
            if command == "set_crosspoint_level":
                await self._module_set(MATRIX_LABEL, f"2>{idx}", self._matrix_level(params))
            else:
                value = "T" if command == "toggle_crosspoint" else self._onoff_param(params, "on")
                await self._module_set(MATRIX_LABEL, f"1>{idx}", value)
            return None
        if command in ("set_signal_generator_route", "set_signal_generator_route_level"):
            o = self._channel(params, "output")
            idx = crosspoint_index(SIGGEN_INPUT, o)
            if command == "set_signal_generator_route":
                await self._module_set(MATRIX_LABEL, f"1>{idx}", self._onoff_param(params, "on"))
            else:
                await self._module_set(MATRIX_LABEL, f"2>{idx}", self._matrix_level(params))
            return None
        if command == "set_signal_generator_level":
            level = float(params["level"])
            if not LEVEL_MIN <= level <= SIGGEN_LEVEL_MAX:
                raise CommandParamError("the signal generator level is -60.5 to +12 dB")
            await self._module_set(SIGGEN_LABEL, f"{SIGGEN_INDEX}>1", format_number(round(level * 2) / 2))
            return None
        if command == "set_signal_generator_mute":
            await self._module_set(SIGGEN_LABEL, f"{SIGGEN_INDEX}>3", self._onoff_param(params, "mute"))
            return None
        if command == "set_signal_generator_type":
            name = str(params.get("type", "")).strip().lower()
            wire = SIGGEN_TYPE_WIRE.get(name) or (name if name in SIGGEN_TYPES else None)
            if wire is None:
                raise CommandParamError("type is pink_noise, white_noise, tone or sweep")
            await self._module_set(SIGGEN_LABEL, f"{SIGGEN_INDEX}>4", wire)
            return None
        if command == "set_signal_generator":
            name = str(params.get("parameter", "")).strip().lower()
            spec = SIGGEN_PARAMS.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(SIGGEN_PARAMS))
            wire = encode_table_value(name, (name,) + spec[1:], params.get("value"))
            return await self._module_set(SIGGEN_LABEL, f"{SIGGEN_INDEX}>{spec[0]}", wire)
        if command == "set_output_limiter":
            c = self._channel(params, "output")
            name = str(params.get("parameter", "")).strip()
            spec = LIMITER_PARAMS.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(LIMITER_PARAMS))
            index2, _, _kind, lo, hi, step = spec
            value = float(params["value"])
            mode = self.get_child_state(OUTPUT_CHILD_TYPE, c).get("output_mode")
            if mode == "independent" and name in ("peak_threshold", "rms_threshold"):
                hi = MONO_PEAK_MAX_V if name == "peak_threshold" else MONO_RMS_MAX_V
                if not lo <= value <= hi:
                    raise CommandParamError(
                        f"Output {c} is independent (mono), where the {name.replace('_', ' ')} is "
                        f"{format_number(lo)} to {format_number(hi)} V")
            if not lo <= value <= hi:
                raise CommandParamError(f"{name} is {format_number(lo)} to {format_number(hi)}")
            wire = format_number(round(round(value / step) * step, 3))
            await self._module_set(limiter_label(c), f"0>{index2}", wire)
            return None
        if command in ("set_output_delay", "set_output_align_delay"):
            c = self._channel(params, "output")
            samples = ms_to_samples(float(params["delay_ms"]))
            ceiling, path, what = ((DELAY_MAX_SAMPLES, "1>1", "delay is 0 to 3000 ms")
                                   if command == "set_output_delay"
                                   else (ALIGN_MAX_SAMPLES, "1>3", "align delay is 0 to 10 ms"))
            if not 0 <= samples <= ceiling:
                raise CommandParamError(what)
            await self._module_set(delay_label(c), path, str(samples))
            return None
        if command == "set_output_delay_bypass":
            c = self._channel(params, "output")
            await self._module_set(delay_label(c), "1>2", self._onoff_param(params, "bypass"))
            return None
        if command in ("set_input_eq", "set_speaker_eq"):
            if command == "set_input_eq":
                c = self._channel(params, "input")
                table, label, bands = INPUT_PEQ_PARAMS, peq_label(c), INPUT_PEQ_BANDS
            else:
                c = self._channel(params, "output")
                table, label, bands = SPEAKER_PEQ_PARAMS, speaker_peq_label(c), SPEAKER_PEQ_BANDS
            band = int(params["band"])
            if not 1 <= band <= bands:
                raise CommandParamError(f"band is 1 to {bands}")
            name = str(params.get("parameter", "")).strip().lower()
            spec = table.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(table))
            wire = encode_table_value(name, (name,) + spec[1:], params.get("value"))
            return await self._module_set(label, f"{band}>{spec[0]}", wire)
        if command == "set_array_eq":
            c = self._channel(params, "input")
            name = str(params.get("parameter", "")).strip().lower()
            spec = ARRAY_EQ_PARAMS.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(ARRAY_EQ_PARAMS))
            wire = encode_table_value(name, (name,) + spec[1:], params.get("value"))
            return await self._array_eq_set(c, spec[0], wire)
        if command == "set_speaker_eq_gain":
            c = self._channel(params, "output")
            gain = float(params["gain"])
            if not -15.0 <= gain <= 15.0:
                raise CommandParamError("speaker EQ gain is -15 to +15 dB")
            return await self._module_set(speaker_peq_label(c), SPEAKER_GAIN_PATH,
                                          format_number(round(gain * 2) / 2))
        if command == "set_band_pass":
            c = self._channel(params, "output")
            name = str(params.get("parameter", "")).strip().lower()
            spec = BAND_PASS_PARAMS.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(BAND_PASS_PARAMS))
            wire = encode_table_value(name, (name,) + spec[1:], params.get("value"))
            return await self._module_set(band_pass_label(c), f"1>{spec[0]}", wire)
        if command == "recall_parameter_set":
            n = int(params["number"])
            if not 1 <= n <= PARAMETER_SET_MAX:
                raise CommandParamError("a parameter set is 1..255")
            await self._fire(f"SS {n:x}")
            await self._query("GS", lambda f: bool(_S_RE.match(self._text(f))))
            # A parameter set can carry the standby property (5.8).
            self._schedule_power_read()
            return None
        if command in ("set_group_level", "step_group_level", "set_group_mute", "toggle_group_mute"):
            n = self._group(params)
            if command == "set_group_level":
                await self._fire(f"SG {n:x},{db_to_hex_level(float(params['level']))}")
                await self._slot_read(f"GG {n:x}")
            elif command == "step_group_level":
                amount = float(params.get("amount", 1.0))
                steps = int(round(abs(amount) * 2))
                if steps == 0:
                    return None
                await self._fire(f"SH {n:x},{1 if amount > 0 else 0},{steps:x}")
                await self._slot_read(f"GG {n:x}")
            else:
                state = "T" if command == "toggle_group_mute" else ("M" if coerce_onoff(params.get("mute")) else "U")
                await self._fire(f"SN {n:x},{state}")
                await self._slot_read(f"GN {n:x}")
            return None
        if command == "read_alarm_status":
            self._status_silent = False
            status = await self._read_status()
            if status is None:
                raise ValueError("the amplifier did not answer the fault query (GF)")
            return status
        if command == "clear_alarms":
            await self._acked("CF")
            # 5.11 clears every active alarm; 5.12 reports the next limiting
            # or clip again once they are cleared. Anything still present is
            # set again by the amplifier, which the status read shows.
            for n in range(1, self._n + 1):
                self.set_child_state_batch(OUTPUT_CHILD_TYPE, n, {"limiting": False})
                self.set_child_state_batch(INPUT_CHILD_TYPE, n, {"clipping": False})
            self._status_silent = False
            await self._read_status()
            return None
        if command == "get_alarm_log":
            return {"entries": await self._read_alarm_log()}
        if command == "clear_alarm_log":
            await self._acked("CH")
            await self._read_alarm_log()
            return None
        if command in ("set_module_parameter", "query_module_parameter"):
            name = str(params.get("module_name", "")).strip()
            path = re.sub(r"\s+", "", str(params.get("index", "")))
            if not name or '"' in name:
                raise CommandParamError("enter the module label without quotes")
            if not re.match(r"^\d+(>\d+){0,2}$", path):
                raise CommandParamError("the index is digits separated by >, e.g. 3 or 0>2 or 2>9")
            if command == "query_module_parameter":
                value = await self._ga_read(name, path)
                if value is None:
                    raise ValueError(f"no reply to {ga_line(name, path)}")
                return value
            value = str(params.get("value", "")).strip()
            if not value or '"' in value:
                raise CommandParamError("enter a value without quotes")
            return await self._module_set(name, path, value)
        if command == "set_ip_address":
            address = str(params["address"]).strip()
            if not _IPV4_RE.match(address):
                raise CommandParamError("enter a dotted IPv4 address")
            await self._fire(f"IP {address}")
            return None
        if command == "set_network_parameter":
            key = str(params["parameter"]).strip().upper()[:1]
            value = str(params["value"]).strip()
            if key == "T":
                value = value.upper()[:1]
                if value not in ("D", "S"):
                    raise CommandParamError("addressing is D (DHCP) or S (static)")
            elif key in ("M", "G"):
                if not _IPV4_RE.match(value):
                    raise CommandParamError("enter a dotted IPv4 address")
            else:
                raise CommandParamError("parameter is T, M or G")
            await self._fire(f"NP {key},{value}")
            return None
        if command == "reset_network_defaults":
            await self._fire("NP F")
            return None
        if command == "reboot":
            await self._fire("RESET")
            return None
        if command == "resync":
            self._refused.clear()
            await self._read_power()
            await self._read_configuration()
            await self._resync()
            return None
        raise ValueError(f"Unknown command: {command}")

    def _schedule_power_read(self) -> None:
        task = self._standby_task
        if task is not None and not task.done():
            task.cancel()
        self._standby_task = asyncio.ensure_future(self._read_back_power_later())

    # ── Test Connection (setup wizard) ──

    async def run_setup_action(self, action_id: str, params: dict[str, Any], progress: Any) -> dict[str, Any]:
        """Ask for the amplifier's standby state, configuration, subscription
        support, its fixed modules, each typed input label and each group,
        and report which answered. On the driver's own connection when there
        is one, otherwise on a socket of its own (the amplifier takes 32)."""
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")
        host = str(self.config.get("host", "")).strip()
        try:
            port = int(self.config.get("port") or PORT)
        except (TypeError, ValueError):
            port = PORT
        if not host:
            raise ValueError("Enter the amplifier's IP address first.")
        session: _WizardSession | None = None
        if self._link_alive():
            await progress(f"Asking on the driver's connection to {host}:{port}", 10)

            async def ask(line: str, matched: Callable[[str], bool]) -> str | None:
                reply = await self._query(line, lambda f: matched(self._text(f)), self.PROBE_TIMEOUT_S)
                return self._text(reply) if reply else None
        else:
            await progress(f"Connecting to {host}:{port}", 10)
            # From the adapter Settings > Network > Reach devices through chose.
            local_ip = self.local_address(host)
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, local_addr=(local_ip, 0) if local_ip else None), 10.0
                )
            except (OSError, asyncio.TimeoutError) as exc:
                raise ConnectionError(f"Could not reach {host}:{port}: check the address ({exc})") from exc
            session = _WizardSession(reader, writer, self.PROBE_TIMEOUT_S)
            ask = session.ask

        async def ask_or_none(line: str, matched: Callable[[str], bool]) -> str | None:
            try:
                return await ask(line, matched)
            except ValueError:
                return None

        answered: list[str] = []
        silent: list[str] = []
        rejected: list[str] = []
        array_eq = None
        modes: list[str] | None = None
        try:
            gy = await ask_or_none("GY", lambda t: bool(_GY_RE.match(t)))
            gc = await ask_or_none("GC", lambda t: parse_configuration(t) is not None)
            modes = parse_configuration(gc) if gc else None
            sub = await ask_or_none("SUB", lambda t: bool(_SUB_SUPPORT_RE.match(t)))
            push = bool(sub) and sub.strip().lower().endswith("yes")
            ip = await ask_or_none("IP", lambda t: bool(_IP_RE.match(t)))
            gf = await ask_or_none("GF", lambda t: bool(_GF_RE.match(t)))
            checks: list[tuple[str, str]] = [
                ("Matrix 1", ga_line(MATRIX_LABEL, "1>1")),
                (SIGGEN_LABEL, ga_line(SIGGEN_LABEL, f"{SIGGEN_INDEX}>3")),
                ("Limiter 1", ga_line(limiter_label(1), "0>1")),
                ("Delay 1", ga_line(delay_label(1), "1>1")),
                ("SpeakerPEQ 1 polarity", ga_line(speaker_peq_label(1), SPEAKER_POLARITY_PATH)),
                (peq_label(1), ga_line(peq_label(1), "1>1")),
            ]
            for n, label in sorted(self._input_labels.items()):
                checks.append((f"Input {input_letter(n)} label '{label}'",
                               ga_line(label, INPUT_SOURCE_INDEX)))
            for number, name in sorted(self._groups.items()):
                checks.append((f"{name} (group {number})", f"GG {number:x}"))
            await progress(f"Asking {len(checks) + 1} value(s)", 40)
            for what, text in checks:
                try:
                    reply = await ask(text, lambda t, x=text: self._reply_to(t.encode(), x))
                except ValueError as exc:
                    rejected.append(f"{what} ({exc})")
                    continue
                (answered if reply else silent).append(what)
            refusals: list[str] = []
            for label in array_eq_labels(1):
                text = ga_line(label, f"{ARRAY_EQ_BAND}>6")
                try:
                    reply = await ask(text, lambda t, x=text: self._reply_to(t.encode(), x))
                except ValueError as exc:
                    refusals.append(f"'{label}' ({exc})")
                    continue
                if reply:
                    array_eq = label
                break
            if array_eq:
                answered.append(f"array EQ as '{array_eq}'")
            elif refusals and len(refusals) == 2:
                rejected.append("Array EQ: " + ", ".join(refusals))
            else:
                silent.append("Array EQ")
        finally:
            if session is not None:
                await session.close()
        if gy is None:
            message = (f"{host}:{port} did not answer the standby query: check that the address "
                       f"is a PowerMatch amplifier's.")
        else:
            state = "in standby" if gy.strip().upper().endswith("S") else "on"
            parts = [f"The amplifier answered and is {state}"
                     + (f" at {ip.split()[1]}" if ip else "") + "."]
            if modes:
                parts.append(f"It reports {len(modes)} channels: "
                             + ", ".join(f"{n} {m.replace('_', ' ')}" for n, m in enumerate(modes, 1))
                             + ".")
            else:
                parts.append("It did not report its output configuration (GC).")
            total = len(answered) + len(silent) + len(rejected)
            if not silent and not rejected:
                parts.append(f"All {total} checked value(s) answered.")
            else:
                parts.append(f"{len(answered)} of {total} answered.")
                if rejected:
                    parts.append("Rejected: " + "; ".join(rejected) + ".")
                if silent:
                    parts.append("Silent: " + ", ".join(silent) + ".")
            if not push:
                parts.append("It did not answer SUB yes, so values will be read each poll.")
            if gf is None:
                parts.append("It did not answer the fault query.")
            message = " ".join(parts)
        await progress(message, 100)
        return {
            "ok": gy is not None and bool(modes) and not silent and not rejected,
            "message": message,
            "answered": answered,
            "silent": silent,
            "rejected": rejected,
            "push_supported": push,
            "standby": None if gy is None else gy.strip().upper().endswith("S"),
            "fault": None if gf is None else gf.strip().upper().endswith("F"),
            "channels": len(modes) if modes else None,
            "output_modes": modes,
            "ip_address": ip.split()[1] if ip else None,
            "array_eq_label": array_eq,
            "problems": list(self._problems),
        }


class _Refused(ValueError):
    """A NAK in answer to a query, with its two-digit code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


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
                frame, self.buf = parse_powermatch_stream(self.buf)
                if frame is None:
                    break
                if not frame:
                    if not self.buf:
                        break
                    continue
                if frame[0] == NAK:
                    raise _Refused(BosePowerMatchDriver._nak_message(frame),
                                   BosePowerMatchDriver._nak_code(frame))
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

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
