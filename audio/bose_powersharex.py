"""
Bose Professional PowerShareX PSX1204D / PSX2404D / PSX4804D — ControlSpace
Serial Control Protocol driver.

Controls a PowerShareX adaptable power amplifier (four input channels fed
from four analog and four Dante inputs, four output channels) through the
ControlSpace Serial Control Protocol. A PowerShareX is not reached on its own
address: the protocol runs through a PSXProxy on the system's RTC (an EX or
ESP processor, or the PC running ControlSpace Designer), and each amplifier
has a port of its own there, which Designer shows in the amplifier's
PowerShareX Amplifier Properties (section 2). So the device's address is the
processor's (or the PC's, 127.0.0.1 on that PC), and its port is the one
Designer assigns. The document gives the proxy no fixed port, no connection
count and nothing a scan could ask on a known port, so the driver has no
discovery fingerprint.

What it controls:
    Standby (``SY`` / ``GY``), each output's level and mute by slot 2
    (``SV`` / ``GV`` / ``SI`` / ``SM`` / ``GM``: a PowerShareX has volume and
    mute on its outputs only), the "Matrix 1" mixer (sixteen cross-points on /
    off and level, a mute per input and per output), each input channel's
    Input Backup Strategy (strategy and the source it plays first), each
    output's polarity, limiter, delay and align delay, the signal generator
    (pink noise per output), the input, speaker and array EQs and the
    band-pass filter by command, the Alarm/Fault Panel (pilot tone and load
    alarms), parameter sets, level groups, Get Signal Level for the analog
    and Dante inputs (dBFS) and the outputs (dBV max), and the amplifier's
    fault output and alarm reports (``GF`` / ``SF``, ``GR`` / ``SR``).
    Every module on a PowerShareX has a fixed label, so nothing needs typing.

Why Python:
    Every system and device command carries hexadecimal numbers (a level of
    5Bh is -14.5 dB, parameter set 11 is ``b``, the signal-level arrays are
    hex), and a YAML driver reads a captured number in base 10 only. On top
    of that: the module commands' ACK / NAK bytes are not line-terminated and
    a NAK's code is the error the user should see; the subscribe handshake
    needs a capability check with a polled fallback; one Get Signal Level
    reply fans out into four children; unsolicited alarm lines carry a
    channel, a severity, a type and a condition that go to different children
    and, when one clears, a read of what is left; and a matrix cross-point's
    index is computed from its input and output.

Push, with a read-back after every write (Principle 2):
    ``SUB "<GET command>"`` makes the amplifier send the value at once and
    again whenever it changes (section 9: parameter set, group levels and
    mutes, slot levels and mutes, any module parameter). On connect the
    driver asks ``SUB`` alone; an amplifier answering ``SUB yes`` gets every
    output level and mute, the matrix, polarity, the signal generator, each
    input's strategy and first source, the parameter set and the groups
    subscribed, renewed every Resync interval; a value it will not
    subscribe is read every poll instead. One that does not answer ``SUB
    yes`` is read every Poll interval, and ``push_supported`` says which. ``SF O``
    and ``SR O`` turn on the amplifier's own fault and alarm reports (5.10,
    5.12; "not retained on power down", so they are sent again every Resync
    interval). Limiting and clipping arrive only that way. Standby, the fault
    output and each channel's alarm status are read every poll as well. The
    limiter, delay and alarm panel are read at connect and every Resync
    interval, and after a write. The document recommends following every Set
    with a Get, and the driver does.

Liveness:
    ``GY`` answers ``GY N`` or ``GY S`` in either power state. A link quiet
    for the probe interval is asked; two misses drop it with a typed
    ``no_response`` fault.

Not this driver: the PowerShare PS404D / PS604D (``bose_powershare``, on its
own address at TCP 10055), the PowerMatch amplifiers and the ControlSpace
processors (``bose_controlspace``).

Source (Bose Professional, manufacturer document):
    ControlSpace Serial Control Protocol v5.15, revision date May 27, 2025
    (published as a zip holding the PDF). Sections 2 ("Using
    Serial-over-Ethernet (PowerShareX)"), 3, 4, 5, 6.2 (the PowerShareX
    module tables), 9 and Appendix B (PowerShareX Limiter Settings). v5.15 is
    the first revision with the PowerShareX.
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
CHANNELS = 4
OUTPUT_SLOT = "2"      # 5.1-5.3: "or PSX, 2 (Out 1-4)"; no input slot
GL_ANALOG = "1"        # 5.4: "or PSX, 1: Analog Input, 2: Output, 3: Dante Input"
GL_OUTPUTS = "2"
GL_DANTE = "3"

DEFAULT_POLL_INTERVAL = 10
DEFAULT_RESYNC_INTERVAL = 60
DEFAULT_METER_INTERVAL_S = 1.0
MIN_METER_INTERVAL_S = 0.2
ACK_TIMEOUT_S = 2.0
QUERY_TIMEOUT_S = 2.0
SUB_TIMEOUT_S = 2.0
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
# 4.2-4.4: "or PSX, 1 - 3Fh (1-63 decimal)"; 4.1: "or PSX, 1 - 40h (1-64 decimal)".
GROUP_MAX = 0x3F
PARAMETER_SET_MAX = 0x40

# Fixed module labels (section 6.2). "Matrix 1" and "PEQ-8band n" are stated
# for the PowerShareX; "Limiter n", "Delay n", "Band Pass n" and "SpeakerPEQ
# n" are the fixed labels the section gives its amplifiers per channel;
# "SigGen 1", "Backup Strategy 1" and "Array EQ n" appear only in the
# PowerShareX examples. A label the amplifier refuses (NAK 01) is reported in
# config_problems and on the hardware checklist.
MATRIX_LABEL = "Matrix 1"
# 6.2.4: "or PSX Index 2 = (Input # -1) x 4 + Output #", Cross Point(4,4) = 16.
MATRIX_STRIDE = 4
SIGGEN_LABEL = "SigGen 1"
SIGGEN_INDEX = "5"          # 6.2.2: Index 1 is always 5
BACKUP_LABEL = "Backup Strategy 1"
# 6.2.12 says the module name "is fixed and is always "Alarm/Fault Panel"",
# and every one of its four examples writes "Alarm / Fault Panel". The
# driver asks the first and, refused as an unknown name, the second.
ALARM_PANEL_LABELS = ("Alarm/Fault Panel", "Alarm / Fault Panel")
# 6.2.10, PowerShareX: Index 1 = 1; Index 2 = 1 delay time (0 to 96000
# samples), 2 bypass, 3 align delay (0 to 4800 samples).
DELAY_MAX_SAMPLES = 96000
ALIGN_MAX_SAMPLES = 4800
# The document's own conversion (6.2.10 examples): samples = seconds x 48000.
SAMPLE_RATE_HZ = 48000


def peq_label(channel: int) -> str:
    """6.2.5: "Module labels for PowerShareX are "PEQ-8band 1" through to
    "PEQ-8band 4" per the channel"."""
    return f"PEQ-8band {channel}"


def array_eq_label(channel: int) -> str:
    return f"Array EQ {channel}"


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


def parse_powersharex_stream(buf: bytes) -> tuple[bytes | None, bytes]:
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
    """O = On, F = Off (every O,F,T parameter in section 6). Two examples
    print a digit zero where the table's O belongs (6.2.4 ``GA"Matrix
    1">3>2=0`` "the matrix input mute for Channel 2 is on", 6.2.9
    ``GA"Limiter 4">0>15=0`` "the Peak Limiter on Channel 4 is enabled"), so
    a 0 also reads as On; the hardware checklist asks which the amplifier
    sends."""
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
    -60.0 to 0.0 in 0.5 dB steps from 0h. On a PowerShareX 0 dBV is the
    channel's Vmax, which depends on the model and output mode (Appendix B,
    the Peak Limiter voltages)."""
    return round(-60.0 + raw / 2.0, 1)


def crosspoint_index(input_no: int, output_no: int) -> int:
    """6.2.4: "or PSX Index 2 = (Input # -1) x 4 + Output #"."""
    return (int(input_no) - 1) * MATRIX_STRIDE + int(output_no)


def crosspoint_of(index: int) -> tuple[int, int] | None:
    """The (input, output) a matrix Index 2 names on a 4 x 4 PowerShareX."""
    i, o = divmod(int(index) - 1, MATRIX_STRIDE)
    i, o = i + 1, o + 1
    if 1 <= i <= CHANNELS and 1 <= o <= CHANNELS:
        return i, o
    return None


def samples_to_ms(samples: int) -> float:
    return round(int(samples) * 1000.0 / SAMPLE_RATE_HZ, 3)


def ms_to_samples(ms: float) -> int:
    return int(round(float(ms) * SAMPLE_RATE_HZ / 1000.0))


# 6.2.3 Input Backup Strategy, Source of Priority n: "0=None, 1-4=Analog
# 1-4, 5-8=DDigital 1-4" (the Dante inputs).
SOURCE_NAMES = ["none", "analog_1", "analog_2", "analog_3", "analog_4",
                "dante_1", "dante_2", "dante_3", "dante_4"]
# 6.2.3 Type: "F=Force, S=Signal Presence, P=Pilot Tone".
STRATEGY_CODES = {"F": "force", "S": "signal_presence", "P": "pilot_tone"}
STRATEGY_WIRE = {v: k for k, v in STRATEGY_CODES.items()}


def source_name(raw: str) -> str | None:
    try:
        n = int(float(raw.strip()))
    except ValueError:
        return None
    return SOURCE_NAMES[n] if 0 <= n < len(SOURCE_NAMES) else None


def source_wire(name: Any) -> str:
    text = str(name).strip().lower()
    if text in SOURCE_NAMES:
        return str(SOURCE_NAMES.index(text))
    if text.isdigit() and 0 <= int(text) < len(SOURCE_NAMES):
        return text
    raise CommandParamError("source is none, analog_1 to analog_4 or dante_1 to dante_4")


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
_IP_RE = re.compile(r"^IP\s+(\d{1,3}(?:\.\d{1,3}){3})$")


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


# ── Module parameter tables (section 6.2, PowerShareX) ──────────────────────

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
INPUT_PEQ_BANDS = 8         # 6.2.5: "1-8 for PowerShareX"
SPEAKER_PEQ_BANDS = 9       # 6.2.8: "9 bands of parametric EQ"

# parameter -> (Index 2, kind, min, max). kind: number, onoff, choice, any
# (a number the document gives no range for: the amplifier judges it).
INPUT_PEQ_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "frequency": ("1", "number", 20, 20000),
    "q": ("2", "number", 0.1, 10.0),
    "gain": ("3", "number", -15.0, 15.0),       # "or PSX, -15.0dB to +15.0dB"
    # Index 2 = 4, Slope: "(Not supported on PSX)".
    "type": ("5", "choice", INPUT_PEQ_TYPES, None),
    "bypass": ("6", "onoff", None, None),
}
SPEAKER_PEQ_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "frequency": ("1", "number", 200, 20000),
    "q": ("2", "number", 0.1, 10.0),
    "gain": ("3", "number", -15.0, 15.0),       # "or PSX, -15.0dB to +15.0dB"
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
# 6.2.6, PowerShareX (Index 1 = 1): 1 Center Freq 100 to 4000 Hz, 2 Tilt and
# 3 Gain "0.1 step" with no range given, 5 Bypass.
ARRAY_EQ_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "center_frequency": ("1", "number", 100, 4000),
    "tilt": ("2", "any", None, None),
    "gain": ("3", "any", None, None),
    "bypass": ("5", "onoff", None, None),
}
# 6.2.9, PowerShareX, Index 1 always 0: (Index 2, state variable, kind, min,
# max, step). Thresholds run "10 to MAX", MAX by model and output mode
# (Appendix B): the largest is 280 V (peak and clip, a PSX4804D V-Bridged
# Lo-Z) and 200 V (RMS, the same).
LIMITER_PARAMS: dict[str, tuple[str, str, str, float, float, float]] = {
    "peak_threshold": ("1", "limiter_peak_v", "number", 10.0, 280.0, 0.5),
    "rms_threshold": ("2", "limiter_rms_v", "number", 10.0, 200.0, 0.5),
    "rms_attack": ("3", "limiter_rms_attack_ms", "int", 500, 10000, 100),
    "rms_release": ("4", "limiter_rms_release_ms", "int", 500, 10000, 100),
    "peak_attack": ("9", "limiter_peak_attack_ms", "number", 0.3, 2000.0, 0.1),
    "peak_release": ("10", "limiter_peak_release_ms", "number", 3.0, 8000.0, 1.0),
    "clip_threshold": ("13", "limiter_clip_v", "number", 10.0, 280.0, 0.5),
    "clip_enabled": ("14", "limiter_clip_enabled", "onoff", 0, 0, 0),
    "peak_enabled": ("15", "limiter_peak_enabled", "onoff", 0, 0, 0),
}
# 6.2.3, Index 1 = channel: parameter -> (Index 2, kind, min, max).
BACKUP_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "strategy": ("1", "strategy", None, None),
    "source_1": ("2", "source", None, None),
    "threshold_1": ("3", "number", -50.0, 15.0),
    "source_2": ("4", "source", None, None),
    "threshold_2": ("5", "number", -50.0, 15.0),
    "source_3": ("6", "source", None, None),
    "threshold_3": ("7", "number", -50.0, 15.0),
    "source_4": ("8", "source", None, None),
    "threshold_4": ("9", "number", -50.0, 15.0),
    "pilot_frequency": ("10", "number", 20, 22000),
    "pilot_upper_threshold": ("11", "number", -50.0, 14.0),
    "pilot_lower_threshold": ("12", "number", -50.0, 14.0),
    "pilot_alarm": ("13", "onoff", None, None),
}
# 6.2.12, Index 1 = channel: parameter -> (Index 2, kind, min, max).
ALARM_PANEL_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "pilot_generator": ("1", "onoff", None, None),
    "pilot_generator_frequency": ("2", "number", 20, 22000),
    "pilot_generator_amplitude": ("3", "number", 0.1, 40.0),
    "voltage_alarm": ("4", "onoff", None, None),
    "voltage_alarm_frequency": ("5", "number", 20, 22000),
    "voltage_alarm_high": ("6", "number", 0.1, 40.0),
    "voltage_alarm_low": ("7", "number", 0.1, 40.0),
    "load_alarm": ("10", "onoff", None, None),
    "load_alarm_frequency": ("11", "number", 20, 22000),
    "load_alarm_high": ("12", "number", 1.0, 500.0),
    "load_alarm_low": ("13", "number", 1.0, 500.0),
    "impedance_alarm": ("16", "onoff", None, None),
    "impedance_alarm_high": ("17", "number", 1.0, 500.0),
    "impedance_alarm_low": ("18", "number", 1.0, 500.0),
}
# The alarm panel's on / off values the driver follows, as output state.
ALARM_PANEL_STATE = {"1": "pilot_tone_generator", "4": "voltage_alarm_enabled",
                     "10": "load_alarm_enabled", "16": "impedance_alarm_enabled"}


def encode_table_value(parameter: str, spec: tuple, value: Any) -> str:
    """A value for one of the module parameters, checked against the
    document's range where it gives one."""
    kind, lo, hi = spec[1], spec[2], spec[3]
    if kind == "onoff":
        return "O" if coerce_onoff(value) else "F"
    if kind == "choice":
        text = str(value).strip()
        for choice in lo:
            if text.lower() == choice.lower():
                return choice
        raise CommandParamError(f"{parameter} is one of {', '.join(lo)}")
    if kind == "strategy":
        text = str(value).strip().lower()
        if text in STRATEGY_WIRE:
            return STRATEGY_WIRE[text]
        if text.upper() in STRATEGY_CODES:
            return text.upper()
        raise CommandParamError("strategy is force, signal_presence or pilot_tone")
    if kind == "source":
        return source_wire(value)
    try:
        v = float(str(value).strip())
    except ValueError as exc:
        raise CommandParamError(f"{parameter} takes a number") from exc
    if kind == "number" and not lo <= v <= hi:
        raise CommandParamError(f"{parameter} is {format_number(lo)} to {format_number(hi)}")
    return format_number(v)


# ── Children ────────────────────────────────────────────────────────────────

INPUT_CHILD_TYPE = "input"
OUTPUT_CHILD_TYPE = "output"
GROUP_CHILD_TYPE = "group"


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
            problems.append(f"group row {n} ({name}): group number {number} is outside 1..63")
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
            problems.append(f"parameter set row {n}: {number} is outside 1..64")
            continue
        names[number] = str(row.get("name") or "").strip() or f"Parameter Set {number}"
    return names, problems


class BosePowerShareXDriver(BaseDriver):
    """Bose Professional PowerShareX PSX1204D / PSX2404D / PSX4804D through
    the PSXProxy port ControlSpace Designer gives each amplifier."""

    DRIVER_INFO = {
        "id": "bose_powersharex",
        "name": "Bose Professional PowerShareX (PSX1204D / PSX2404D / PSX4804D)",
        "manufacturer": "Bose Professional",
        "category": "audio",
        "version": "1.0.0",
        # BaseDriver.local_address(), which the Test Connection socket binds
        # to, needs 0.37.0 (set by hand: the contract check cannot see it).
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": (
            "Controls Bose Professional PowerShareX PSX1204D, PSX2404D and PSX4804D "
            "amplifiers through the ControlSpace Serial Control Protocol, on the port "
            "ControlSpace Designer gives each amplifier on its EX or ESP processor: "
            "standby, each output's level, mute and polarity, the 4 x 4 matrix, each "
            "input's backup strategy and source, limiters, delays, EQs, the signal "
            "generator, the alarm panel, parameter sets, volume groups, signal levels, "
            "and the amplifier's fault and alarm reports. Values are subscribed, so "
            "changes made elsewhere appear at once."
        ),
        "source_url": "https://assets.boseprofessional.com/m/4b14907043cbedb3/original/ControlSpace-Serial-Protocol-v5-15.zip",
        "tags": ["amplifier", "bose", "powersharex", "powershare", "install-audio", "dante",
                 "controlspace"],
        "verified": False,
        "simulated": True,
        "protocols": ["bose-controlspace-serial"],
        "ports": [],
        "transport": "tcp",
        "compatible_models": [
            {
                "manufacturer": "Bose Professional",
                "models": ["PSX1204D", "PSX2404D", "PSX4804D"],
                "confidence": "untested",
                "notes": (
                    "PowerShareX adaptable power amplifiers: four input channels fed from "
                    "four analog and four Dante inputs, four outputs. Reached through the "
                    "PSXProxy on the system's EX or ESP processor (or the PC running "
                    "ControlSpace Designer), one port per amplifier. Built from the "
                    "ControlSpace Serial Control Protocol v5.15 and the simulator; not yet "
                    "run against an amplifier."
                ),
            },
        ],
        "help": {
            "overview": (
                "PowerShareX control over the ControlSpace Serial Control Protocol (no "
                "login), through the amplifier's own port on the processor that runs its "
                "PSXProxy. Each input and output is a child entity: output level, mute and "
                "polarity, the matrix cross-points into each output, each input's backup "
                "strategy and source, the limiter and delay, alarms and signal levels. "
                "Standby, the fault output, parameter sets, volume groups and the signal "
                "generator are on the device. A Discovery scan finds the processor, not "
                "the amplifier: add the amplifier by hand."
            ),
            "setup": (
                "STEP 1 - Find the amplifier's address and port.\n"
                "In ControlSpace Designer, open the amplifier's PowerShareX Amplifier "
                "Properties. It shows the IP address and port to use: the address of the "
                "EX or ESP processor running the amplifier's PSXProxy (or of the PC, when "
                "the proxies run there), and a port of its own for each amplifier.\n\n"
                "STEP 2 - Add the device.\n"
                "Enter that address and port. When the proxies run on the PC that runs "
                "OpenAVC, the address is 127.0.0.1.\n\n"
                "STEP 3 - Groups and parameter sets (optional).\n"
                "Add the volume groups and parameter sets from your design by number and "
                "name if you want to recall them by name or bind group levels.\n\n"
                "STEP 4 - Test.\n"
                "Run Test Connection. It reports whether the amplifier answered, whether "
                "it supports subscriptions, and whether its matrix, signal generator, "
                "backup strategy and alarm panel modules answered."
            ),
            "connection": (
                "No login. A port that answers but is not a PowerShareX amplifier's (the "
                "processor's own 10055, for instance) never answers the standby query, and "
                "the device shows as not responding. Going online with ControlSpace "
                "Designer can close the connection; the driver reconnects on its own."
            ),
        },
        "default_config": {
            "host": "",
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
            "host": {"type": "string", "required": True, "label": "Processor or PC Address",
                     "help": "The IP address in the amplifier's PowerShareX Amplifier Properties "
                             "in ControlSpace Designer: the EX or ESP processor running its "
                             "proxy, or the PC (127.0.0.1 on the same PC)."},
            "port": {"type": "integer", "required": True, "label": "Amplifier Port", "min": 1,
                     "max": 65535,
                     "help": "The port in the amplifier's PowerShareX Amplifier Properties in "
                             "ControlSpace Designer. Each amplifier has its own."},
            "groups": {
                "type": "table", "label": "Volume Groups", "row_label": "group",
                "columns": {
                    "number": {
                        "type": "integer",
                        "label": "Group Number",
                        "required": True,
                        "min": 1,
                        "max": 63,
                        "help": "The volume Group number in ControlSpace Designer (1-63).",
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
                        "max": 64,
                        "help": "The Parameter Set number in ControlSpace Designer (1-64).",
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
                        "limiter, delay and alarm panel read again.",
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
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "matrix_mute": {
                        "type": "boolean",
                        "label": "Matrix Input Mute",
                        "control": True,
                        "cloud_priority": "high",
                        "help": "The mute on this input of Matrix 1, which silences it in every "
                                "output.",
                    },
                    "source": {
                        "type": "enum",
                        "values": ["none", "analog_1", "analog_2", "analog_3", "analog_4",
                                   "dante_1", "dante_2", "dante_3", "dante_4"],
                        "label": "Primary Source",
                        "control": True,
                        "help": (
                            "The input this channel plays first (its Input Backup Strategy's "
                            "priority 1 source). With the strategy at Force it is the one it "
                            "plays."
                        ),
                    },
                    "backup_strategy": {
                        "type": "enum",
                        "values": ["force", "signal_presence", "pilot_tone"],
                        "label": "Backup Strategy",
                        "control": True,
                        "help": (
                            "Force plays the primary source; Signal Presence and Pilot Tone "
                            "fall back to the next source when it is lost."
                        ),
                    },
                    "clipping": {
                        "type": "boolean",
                        "label": "Input Clip Reported",
                        "cloud_priority": "high",
                        "help": (
                            "The amplifier reported this channel's input clipping. It reports "
                            "only the first clip until its alarms are cleared, so this stays on "
                            "until it reports the clip cleared. Needs Alarm reports on."
                        ),
                    },
                    "analog_signal_level": {
                        "type": "number",
                        "label": "Analog In Signal (dBFS)",
                        "min": -60.0,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "cloud_priority": "low",
                        "help": "The analog input of the same number. Read only while Poll "
                                "signal levels is on.",
                    },
                    "dante_signal_level": {
                        "type": "number",
                        "label": "Dante In Signal (dBFS)",
                        "min": -60.0,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "cloud_priority": "low",
                        "help": "The Dante input of the same number. Read only while Poll "
                                "signal levels is on.",
                    },
                },
                "summary_fields": ["source", "matrix_mute"],
                "label_field": "name",
            },
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "integer", "min": 1, "max": 4},
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
                    "matrix_mute": {
                        "type": "boolean",
                        "label": "Matrix Output Mute",
                        "control": True,
                        "help": "The mute on this output of Matrix 1.",
                    },
                    "polarity_inverted": {
                        "type": "boolean",
                        "label": "Polarity Inverted",
                        "control": True,
                        "help": "The speaker EQ's polarity for this output.",
                    },
                    "pink_noise": {
                        "type": "boolean",
                        "label": "Pink Noise",
                        "control": True,
                        "help": "The signal generator's pink noise on this output.",
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
                        "help": "What that alarm is: an open or shorted load, protection, or "
                                "another alarm.",
                    },
                    "limiting": {
                        "type": "boolean",
                        "label": "Limiting Reported",
                        "cloud_priority": "high",
                        "help": (
                            "The amplifier reported this output limiting. It reports only the "
                            "first time until its alarms are cleared, so this stays on until it "
                            "reports the limiting cleared. Needs Alarm reports on."
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
                            "0 dBV is the channel's full output, which depends on the model and "
                            "output mode. Read only while Poll signal levels is on."
                        ),
                    },
                    "input_1": {
                        "type": "boolean",
                        "label": "From Input 1",
                        "control": True,
                        "help": "Matrix 1 cross-point: input 1 into this output.",
                    },
                    "input_1_level": {
                        "type": "number",
                        "label": "From Input 1 Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "input_2": {
                        "type": "boolean",
                        "label": "From Input 2",
                        "control": True,
                        "help": "Matrix 1 cross-point: input 2 into this output.",
                    },
                    "input_2_level": {
                        "type": "number",
                        "label": "From Input 2 Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "input_3": {
                        "type": "boolean",
                        "label": "From Input 3",
                        "control": True,
                        "help": "Matrix 1 cross-point: input 3 into this output.",
                    },
                    "input_3_level": {
                        "type": "number",
                        "label": "From Input 3 Level (dB)",
                        "min": -60.5,
                        "max": 0.0,
                        "step": 0.5,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "input_4": {
                        "type": "boolean",
                        "label": "From Input 4",
                        "control": True,
                        "help": "Matrix 1 cross-point: input 4 into this output.",
                    },
                    "input_4_level": {
                        "type": "number",
                        "label": "From Input 4 Level (dB)",
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
                        "min": 10.0,
                        "max": 280.0,
                        "step": 0.5,
                        "unit": "V",
                        "help": "10 V up to the model's and output mode's maximum (70 to 280 V).",
                    },
                    "limiter_rms_v": {
                        "type": "number",
                        "label": "Limiter RMS Threshold (V)",
                        "min": 10.0,
                        "max": 200.0,
                        "step": 0.5,
                        "unit": "V",
                        "help": "10 V up to the model's and output mode's maximum (50 to 200 V).",
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
                    "limiter_peak_attack_ms": {
                        "type": "number",
                        "label": "Limiter Peak Attack (ms)",
                        "min": 0.3,
                        "max": 2000.0,
                        "step": 0.1,
                        "unit": "ms",
                    },
                    "limiter_peak_release_ms": {
                        "type": "number",
                        "label": "Limiter Peak Release (ms)",
                        "min": 3.0,
                        "max": 8000.0,
                        "step": 1.0,
                        "unit": "ms",
                    },
                    "limiter_clip_v": {
                        "type": "number",
                        "label": "Clip Limiter Threshold (V)",
                        "min": 10.0,
                        "max": 280.0,
                        "step": 0.5,
                        "unit": "V",
                        "help": "10 V up to the model's and output mode's maximum (80 to 280 V).",
                    },
                    "limiter_clip_enabled": {"type": "boolean", "label": "Clip Limiter On"},
                    "limiter_peak_enabled": {"type": "boolean", "label": "Peak Limiter On"},
                    "delay_ms": {
                        "type": "number",
                        "label": "Delay (ms)",
                        "min": 0.0,
                        "max": 2000.0,
                        "unit": "ms",
                        "help": "0 to 2000 ms (96000 samples at 48 kHz).",
                    },
                    "delay_bypass": {"type": "boolean", "label": "Delay Bypass"},
                    "align_delay_ms": {
                        "type": "number",
                        "label": "Align Delay (ms)",
                        "min": 0.0,
                        "max": 100.0,
                        "unit": "ms",
                        "help": "0 to 100 ms (4800 samples at 48 kHz).",
                    },
                    "pilot_tone_generator": {
                        "type": "boolean",
                        "label": "Pilot Tone Generator",
                        "help": "The Alarm/Fault Panel's pilot tone generator on this output.",
                    },
                    "voltage_alarm_enabled": {
                        "type": "boolean",
                        "label": "Pilot Tone Voltage Alarm",
                    },
                    "load_alarm_enabled": {
                        "type": "boolean",
                        "label": "Pilot Tone Load Alarm",
                    },
                    "impedance_alarm_enabled": {
                        "type": "boolean",
                        "label": "Nominal Impedance Alarm",
                    },
                },
                "summary_fields": ["level_db", "mute", "alarm"],
                "label_field": "name",
            },
            "group": {
                "label": "Group",
                "label_plural": "Groups",
                "id_format": {"type": "integer", "min": 1, "max": 63},
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
            "parameter_set": {"type": "integer", "label": "Last Parameter Set", "min": 0,
                              "max": 64, "control": True,
                              "help": "The last parameter set recalled; 0 after power-up."},
            "parameter_set_name": {"type": "string", "label": "Last Parameter Set Name"},
            "parameter_set_options": {"type": "string", "label": "Parameter Set List",
                                      "help": "The Parameter Sets table as a picker list."},
            "signal_generator_level_db": {"type": "number", "label": "Signal Generator Level (dB)",
                                          "min": -60.5, "max": 12.0, "step": 0.5, "unit": "dB",
                                          "control": True},
            "signal_generator_mute": {"type": "boolean", "label": "Signal Generator Mute",
                                      "control": True},
            "push_supported": {"type": "boolean", "label": "Subscriptions Supported",
                               "help": "True when the amplifier answered SUB with yes; otherwise "
                                       "every value is read each poll."},
            "ip_address": {"type": "string", "label": "IP Address",
                           "help": "The address the amplifier reports for itself."},
            "config_problems": {"type": "string", "label": "Problems",
                                "help": "A module the amplifier refused (its label or index), or a "
                                        "table row that could not be read."},
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
                    "Puts the amplifier in standby: every output goes silent. The amplifier still "
                    "answers on the network."
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
                "help": "Flips an output's mute.",
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
                    "inverted": {
                        "type": "boolean",
                        "required": True,
                        "label": "Inverted",
                        "help": "On inverts the output's polarity.",
                    },
                },
                "sets": {"polarity_inverted": "{inverted}"},
                "help": "Inverts or restores an output's polarity.",
            },
            "set_crosspoint": {
                "label": "Set Matrix Cross-Point",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Matrix Input",
                    },
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Matrix Output",
                    },
                    "on": {
                        "type": "boolean",
                        "required": True,
                        "label": "On",
                        "help": "On sends the input into the output.",
                    },
                },
                "help": (
                    "Turns a Matrix 1 cross-point on or off: whether the input is heard on the "
                    "output."
                ),
            },
            "toggle_crosspoint": {
                "label": "Toggle Matrix Cross-Point",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Matrix Input",
                    },
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Matrix Output",
                    },
                },
                "help": "Flips a Matrix 1 cross-point on or off.",
            },
            "set_crosspoint_level": {
                "label": "Set Matrix Cross-Point Level",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Matrix Input",
                    },
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Matrix Output",
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
                "help": "Sets how loud the input is in the output at a Matrix 1 cross-point.",
            },
            "set_matrix_input_mute": {
                "label": "Set Matrix Input Mute",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Matrix Input",
                    },
                    "mute": {"type": "boolean", "required": True, "label": "Mute"},
                },
                "sets": {"matrix_mute": "{mute}"},
                "help": "Mutes or unmutes an input of Matrix 1, silencing it in every output.",
            },
            "set_matrix_output_mute": {
                "label": "Set Matrix Output Mute",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Matrix Output",
                    },
                    "mute": {"type": "boolean", "required": True, "label": "Mute"},
                },
                "sets": {"matrix_mute": "{mute}"},
                "help": "Mutes or unmutes an output of Matrix 1.",
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
                        "values": [
                            {"value": "analog_1", "label": "Analog 1"},
                            {"value": "analog_2", "label": "Analog 2"},
                            {"value": "analog_3", "label": "Analog 3"},
                            {"value": "analog_4", "label": "Analog 4"},
                            {"value": "dante_1", "label": "Dante 1"},
                            {"value": "dante_2", "label": "Dante 2"},
                            {"value": "dante_3", "label": "Dante 3"},
                            {"value": "dante_4", "label": "Dante 4"},
                            {"value": "none", "label": "None"},
                        ],
                    },
                },
                "sets": {"source": "{source}"},
                "help": (
                    "Picks the analog or Dante input this channel plays first (its backup "
                    "strategy's priority 1 source). With the strategy at Force it is the one the "
                    "channel plays."
                ),
            },
            "set_input_backup_strategy": {
                "label": "Set Input Backup Strategy",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "strategy": {
                        "type": "enum",
                        "required": True,
                        "label": "Strategy",
                        "values": [
                            {"value": "force", "label": "Force"},
                            {"value": "signal_presence", "label": "Signal Presence"},
                            {"value": "pilot_tone", "label": "Pilot Tone"},
                        ],
                    },
                },
                "sets": {"backup_strategy": "{strategy}"},
                "help": (
                    "Sets how an input channel picks its source: Force plays the primary source, "
                    "Signal Presence and Pilot Tone move to the next source when the signal or "
                    "pilot tone is lost."
                ),
            },
            "set_input_backup": {
                "label": "Set Input Backup Parameter",
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
                            {"value": "source_1", "label": "Priority 1 Source"},
                            {"value": "threshold_1", "label": "Priority 1 Threshold (dB)"},
                            {"value": "source_2", "label": "Priority 2 Source"},
                            {"value": "threshold_2", "label": "Priority 2 Threshold (dB)"},
                            {"value": "source_3", "label": "Priority 3 Source"},
                            {"value": "threshold_3", "label": "Priority 3 Threshold (dB)"},
                            {"value": "source_4", "label": "Priority 4 Source"},
                            {"value": "threshold_4", "label": "Priority 4 Threshold (dB)"},
                            {"value": "pilot_frequency", "label": "Pilot Tone Frequency (Hz)"},
                            {"value": "pilot_upper_threshold", "label": "Pilot Tone Upper Threshold (dBu)"},
                            {"value": "pilot_lower_threshold", "label": "Pilot Tone Lower Threshold (dBu)"},
                            {"value": "pilot_alarm", "label": "Pilot Tone Alarm (on/off)"},
                            {"value": "strategy", "label": "Strategy"},
                        ],
                    },
                    "value": {
                        "type": "string",
                        "required": True,
                        "label": "Value",
                        "help": (
                            "A source (none, analog_1 to analog_4, dante_1 to dante_4), a threshold "
                            "(-50 to 15 dB; pilot tone -50 to 14 dBu), a frequency (20-22000 Hz), "
                            "on/off, or a strategy (force, signal_presence, pilot_tone)."
                        ),
                    },
                },
                "help": (
                    "Changes one setting of an input channel's backup strategy: the sources in "
                    "priority order, the threshold each must reach, and the pilot tone it listens "
                    "for. Returns the value the amplifier reports back."
                ),
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
                            {"value": "clip_threshold", "label": "Clip Threshold (V)"},
                            {"value": "rms_attack", "label": "RMS Attack (ms)"},
                            {"value": "rms_release", "label": "RMS Release (ms)"},
                            {"value": "peak_attack", "label": "Peak Attack (ms)"},
                            {"value": "peak_release", "label": "Peak Release (ms)"},
                            {"value": "peak_enabled", "label": "Peak Limiter On (1 = on, 0 = off)"},
                            {"value": "clip_enabled", "label": "Clip Limiter On (1 = on, 0 = off)"},
                        ],
                    },
                    "value": {
                        "type": "number",
                        "required": True,
                        "label": "Value",
                        "min": 0,
                        "max": 10000,
                        "help": (
                            "Thresholds from 10 V up to the model's and output mode's maximum "
                            "(peak and clip up to 280 V, RMS up to 200 V on a PSX4804D bridged), "
                            "in 0.5 V steps; RMS attack and release 500 to 10,000 ms; peak attack "
                            "0.3 to 2000 ms; peak release 3 to 8000 ms; 1 or 0 to turn a limiter on "
                            "or off."
                        ),
                    },
                },
                "help": (
                    "Changes one setting of an output's limiter, which protects the loudspeakers. "
                    "The amplifier refuses a threshold above the maximum for its model and the "
                    "output's mode."
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
                        "max": 2000,
                        "unit": "ms",
                        "help": "0 to 2000 ms, sent as samples at 48 kHz.",
                    },
                },
                "help": "Delays an output, to line loudspeakers up in time.",
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
                "help": "Bypasses an output's delay, or puts it back.",
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
                        "max": 100,
                        "unit": "ms",
                        "help": "0 to 100 ms, sent as samples at 48 kHz.",
                    },
                },
                "help": "Sets an output's alignment delay, a fine time offset for its loudspeaker.",
            },
            "set_input_eq": {
                "label": "Set Input EQ",
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
                        "max": 8,
                    },
                    "parameter": {
                        "type": "enum",
                        "required": True,
                        "label": "Parameter",
                        "values": [
                            {"value": "frequency", "label": "Frequency (20-20000 Hz)"},
                            {"value": "q", "label": "Q (0.1-10)"},
                            {"value": "gain", "label": "Gain (-15 to +15 dB)"},
                            {"value": "type", "label": "Type (B, HS, LS, HC, LC, N)"},
                            {"value": "bypass", "label": "Bypass (on/off)"},
                        ],
                    },
                    "value": {
                        "type": "string",
                        "required": True,
                        "label": "Value",
                        "help": (
                            "A number, a filter type (B band, HS high shelf, LS low shelf, HC high "
                            "cut, LC low cut, N notch) or on/off for bypass."
                        ),
                    },
                },
                "help": (
                    "Changes one band of an input's eight-band parametric EQ (PEQ-8band). Returns "
                    "the value the amplifier reports back."
                ),
            },
            "set_speaker_eq": {
                "label": "Set Speaker EQ",
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
                            {"value": "frequency", "label": "Frequency (200-20000 Hz)"},
                            {"value": "q", "label": "Q (0.1-10)"},
                            {"value": "gain", "label": "Gain (-15 to +15 dB)"},
                            {"value": "type", "label": "Type (B, HS, LS, N)"},
                            {"value": "bypass", "label": "Bypass (on/off)"},
                        ],
                    },
                    "value": {"type": "string", "required": True, "label": "Value"},
                },
                "help": (
                    "Changes one band of an output's speaker parametric EQ (SpeakerPEQ). Returns "
                    "the value the amplifier reports back."
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
                        "min": -15.0,
                        "max": 15.0,
                        "unit": "dB",
                        "help": "-15 to +15 dB in 0.5 dB steps.",
                    },
                },
                "help": (
                    "Sets the overall gain of an output's speaker EQ. Returns the value the "
                    "amplifier reports back."
                ),
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
                            {"value": "lpf_type", "label": "Low-Pass Type"},
                            {"value": "lpf_frequency", "label": "Low-Pass Frequency (Hz)"},
                            {"value": "hpf_bypass", "label": "High-Pass Bypass"},
                            {"value": "lpf_bypass", "label": "Low-Pass Bypass"},
                        ],
                    },
                    "value": {
                        "type": "string",
                        "required": True,
                        "label": "Value",
                        "help": (
                            "A frequency (20-20000 Hz), a filter type (But6 to But48, Bes12 to "
                            "Bes48, Lin12 to Lin48) or on/off for a bypass."
                        ),
                    },
                },
                "help": (
                    "Changes an output's high-pass or low-pass filter. Returns the value the "
                    "amplifier reports back."
                ),
            },
            "set_array_eq": {
                "label": "Set Array EQ",
                "params": {
                    "channel": {
                        "type": "integer",
                        "required": True,
                        "label": "Channel",
                        "min": 1,
                        "max": 4,
                    },
                    "parameter": {
                        "type": "enum",
                        "required": True,
                        "label": "Parameter",
                        "values": [
                            {"value": "center_frequency", "label": "Center Frequency (100-4000 Hz)"},
                            {"value": "tilt", "label": "Tilt"},
                            {"value": "gain", "label": "Gain (dB)"},
                            {"value": "bypass", "label": "Bypass (on/off)"},
                        ],
                    },
                    "value": {
                        "type": "string",
                        "required": True,
                        "label": "Value",
                        "help": "A number in 0.1 steps, or on/off for bypass. The amplifier "
                                "refuses a tilt or gain outside its range.",
                    },
                },
                "help": (
                    "Changes a channel's array EQ, which shapes the response of a line array. "
                    "Returns the value the amplifier reports back."
                ),
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
                "help": "Sets how loud the signal generator's pink noise is.",
            },
            "set_signal_generator_mute": {
                "label": "Set Signal Generator Mute",
                "params": {
                    "mute": {"type": "boolean", "required": True, "label": "Mute"},
                },
                "sets": {"signal_generator_mute": "{mute}"},
                "help": "Mutes or unmutes the signal generator on every output.",
            },
            "set_output_pink_noise": {
                "label": "Set Output Pink Noise",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "on": {"type": "boolean", "required": True, "label": "On",
                           "help": "On plays pink noise from the signal generator on the output."},
                },
                "sets": {"pink_noise": "{on}"},
                "help": (
                    "Plays the signal generator's pink noise on an output, or stops it, for "
                    "testing the loudspeakers on it."
                ),
            },
            "set_alarm_panel": {
                "label": "Set Alarm Panel Parameter",
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
                            {"value": "pilot_generator", "label": "Pilot Tone Generator (on/off)"},
                            {"value": "pilot_generator_frequency", "label": "Pilot Tone Generator Frequency (Hz)"},
                            {"value": "pilot_generator_amplitude", "label": "Pilot Tone Generator Amplitude (Vrms)"},
                            {"value": "voltage_alarm", "label": "Voltage Alarm (on/off)"},
                            {"value": "voltage_alarm_frequency", "label": "Voltage Alarm Frequency (Hz)"},
                            {"value": "voltage_alarm_high", "label": "Voltage Alarm High (Vrms)"},
                            {"value": "voltage_alarm_low", "label": "Voltage Alarm Low (Vrms)"},
                            {"value": "load_alarm", "label": "Load Alarm (on/off)"},
                            {"value": "load_alarm_frequency", "label": "Load Alarm Frequency (Hz)"},
                            {"value": "load_alarm_high", "label": "Load Alarm High (ohms)"},
                            {"value": "load_alarm_low", "label": "Load Alarm Low (ohms)"},
                            {"value": "impedance_alarm", "label": "Nominal Impedance Alarm (on/off)"},
                            {"value": "impedance_alarm_high", "label": "Nominal Impedance Alarm High (ohms)"},
                            {"value": "impedance_alarm_low", "label": "Nominal Impedance Alarm Low (ohms)"},
                        ],
                    },
                    "value": {
                        "type": "string",
                        "required": True,
                        "label": "Value",
                        "help": (
                            "On/off, a frequency (20-22000 Hz), a voltage (0.1-40 Vrms) or an "
                            "impedance (1-500 ohms)."
                        ),
                    },
                },
                "help": (
                    "Changes one setting of the Alarm/Fault Panel for an output: the pilot tone it "
                    "sends and the voltage, load and impedance alarms that watch the loudspeaker "
                    "line. Returns the value the amplifier reports back."
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
                        "max": 64,
                        "options_state": "parameter_set_options",
                        "help": (
                            "The Parameter Set number (1-64). Names come from the Parameter Sets "
                            "table in the device settings."
                        ),
                    },
                },
                "help": "Recalls a Parameter Set programmed in ControlSpace Designer.",
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
                "help": "Sets a volume group's master level. Amplifier outputs stop at 0 dB.",
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
                "help": (
                    "Reads the fault output and each channel's alarm status from the amplifier "
                    "and returns them."
                ),
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
                            "Limiter 2, PEQ-8band 3, Backup Strategy 1)."
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
                        "pattern": "^[^\"]{1,64}$",
                        "help": "The value as the protocol writes it: -6, 2000, O / F / T, B.",
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
                            "Limiter 2, PEQ-8band 3, Backup Strategy 1)."
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
                    "Asks the amplifier for its standby state, its subscription support, its "
                    "matrix, signal generator, backup strategy and alarm panel modules and each "
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
    STANDBY_SETTLE_S = STANDBY_SETTLE_S

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        self._problems: list[str] = []
        self._groups, problems = parse_groups_config(config.get("groups") or [])
        self._problems.extend(problems)
        self._parameter_set_names, problems = parse_parameter_sets_config(config.get("parameter_sets") or [])
        self._problems.extend(problems)
        # Module reads the amplifier refused on this connection: label -> why.
        self._refused: dict[str, str] = {}
        self._alarm_panel_label = ALARM_PANEL_LABELS[0]
        # (module label lowered, index path) -> (child type or "", channel, prop, kind)
        self._route: dict[tuple[str, tuple[str, ...]], tuple[str, int, str, str]] = {}
        mx = MATRIX_LABEL.lower()
        for c in range(1, CHANNELS + 1):
            self._route[(mx, ("3", str(c)))] = (INPUT_CHILD_TYPE, c, "matrix_mute", "onoff")
            self._route[(mx, ("4", str(c)))] = (OUTPUT_CHILD_TYPE, c, "matrix_mute", "onoff")
            for i in range(1, CHANNELS + 1):
                idx = str(crosspoint_index(i, c))
                self._route[(mx, ("1", idx))] = (OUTPUT_CHILD_TYPE, c, f"input_{i}", "onoff")
                self._route[(mx, ("2", idx))] = (OUTPUT_CHILD_TYPE, c, f"input_{i}_level", "number")
            for index2, prop, kind, *_rest in LIMITER_PARAMS.values():
                self._route[(limiter_label(c).lower(), ("0", index2))] = (OUTPUT_CHILD_TYPE, c, prop, kind)
            self._route[(delay_label(c).lower(), ("1", "1"))] = (OUTPUT_CHILD_TYPE, c, "delay_ms", "samples")
            self._route[(delay_label(c).lower(), ("1", "2"))] = (OUTPUT_CHILD_TYPE, c, "delay_bypass", "onoff")
            self._route[(delay_label(c).lower(), ("1", "3"))] = (OUTPUT_CHILD_TYPE, c, "align_delay_ms", "samples")
            self._route[(speaker_peq_label(c).lower(), ("0", "4"))] = (
                OUTPUT_CHILD_TYPE, c, "polarity_inverted", "onoff")
            self._route[(SIGGEN_LABEL.lower(), (SIGGEN_INDEX, str(c + 2)))] = (
                OUTPUT_CHILD_TYPE, c, "pink_noise", "onoff")
            self._route[(BACKUP_LABEL.lower(), (str(c), "1"))] = (INPUT_CHILD_TYPE, c, "backup_strategy", "strategy")
            self._route[(BACKUP_LABEL.lower(), (str(c), "2"))] = (INPUT_CHILD_TYPE, c, "source", "source")
        self._route[(SIGGEN_LABEL.lower(), (SIGGEN_INDEX, "1"))] = ("", 0, "signal_generator_level_db", "number")
        self._route[(SIGGEN_LABEL.lower(), (SIGGEN_INDEX, "2"))] = ("", 0, "signal_generator_mute", "onoff")
        self._waiters: list[tuple[Callable[[bytes], bool], asyncio.Future[bytes]]] = []
        self._push_supported = False
        # Live values a subscribing amplifier would not subscribe, read every
        # poll instead.
        self._polled_gets: list[str] = []
        # Set when the fault query drew silence: the status reads then wait
        # for the next resync rather than costing a timeout every poll.
        self._status_silent = False
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
        return CallableFrameParser(parse_powersharex_stream)

    # ── Lifecycle ──

    async def _initial_sync(self) -> None:
        self.set_state("parameter_set_options", json.dumps(
            [{"value": n, "label": f"{n}: {name}"} for n, name in sorted(self._parameter_set_names.items())]
        ))
        self._refused.clear()
        self._status_silent = False
        self.set_states({"ac_mains_loss": False, "digital_audio_loss": False})
        self._publish_problems()
        self._register_children()
        await self._read_power()
        await self._detect_push_support()
        await self._fire("IP")
        await self._find_alarm_panel_label()
        await self._resync()
        self._start_meter_loop()

    async def poll(self) -> None:
        """Standby every poll (it has no subscription), then the fault output
        and alarm status; every live value too on an amplifier without
        subscriptions; the subscriptions and alarm reports renewed and the
        limiter, delay and alarm panel read every Resync interval. Stops at
        the first silence and lets the platform's checks judge it."""
        if await self._read_power() is None:
            return
        due = self._last_resync is None or (
            asyncio.get_running_loop().time() - self._last_resync >= self._resync_interval()
        )
        if due:
            await self._resync()
        else:
            if not self._push_supported:
                await self._read_live()
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

    def _register_children(self) -> None:
        for c in range(1, CHANNELS + 1):
            self.register_child(INPUT_CHILD_TYPE, c,
                                initial_state={"name": f"Input {c}", "clipping": False})
            self.register_child(OUTPUT_CHILD_TYPE, c,
                                initial_state={"name": f"Output {c}", "limiting": False})
        for number, name in self._groups.items():
            self.register_child(GROUP_CHILD_TYPE, number, initial_state={"name": name, "responding": False})

    async def refresh_children(self) -> dict[str, Any]:
        self._register_children()
        await self._resync()
        return {"inputs": CHANNELS, "outputs": CHANNELS, "groups": len(self._groups)}

    def _resync_interval(self) -> float:
        try:
            return max(10.0, float(self.config.get("resync_interval", DEFAULT_RESYNC_INTERVAL)))
        except (TypeError, ValueError):
            return float(DEFAULT_RESYNC_INTERVAL)

    def _publish_problems(self) -> None:
        """Table problems plus every module the amplifier refused, written
        again on every poll while they hold."""
        refused = [f"{label}: {why}" for label, why in sorted(self._refused.items())]
        self.set_state("config_problems", "; ".join(self._problems + refused))

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

    async def _module_write(self, line: str) -> None:
        """A module command (SA): await its ACK or NAK. A NAK becomes the
        error the user sees; silence is tolerated, and the read-back that
        follows shows what the amplifier holds."""
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
            raise ValueError(message)

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

    async def _find_alarm_panel_label(self) -> None:
        """6.2.12 names the module two ways. Ask the first spelling; when the
        amplifier refuses it as an unknown name, ask the second. Silence (an
        amplifier still booting) keeps the first and asks again next connect."""
        for label in ALARM_PANEL_LABELS:
            text = ga_line(label, "1>1")
            try:
                reply = await self._query(text, lambda f, t=text: self._reply_to(f, t))
            except _Refused as exc:
                if exc.code == "01":
                    continue
                self._alarm_panel_label = label
                return
            self._alarm_panel_label = label
            if reply is None:
                self._alarm_panel_label = ALARM_PANEL_LABELS[0]
            return
        self._alarm_panel_label = ALARM_PANEL_LABELS[0]
        self._refused["Alarm/Fault Panel"] = ("the amplifier knows neither 'Alarm/Fault Panel' nor "
                                              "'Alarm / Fault Panel' (NAK 01)")

    def _live_gets(self) -> list[str]:
        """Every subscribable value the driver follows."""
        out: list[str] = ["GS"]
        for c in range(1, CHANNELS + 1):
            out.append(f"GV {OUTPUT_SLOT},{c:x}")
            out.append(f"GM {OUTPUT_SLOT},{c:x}")
        for c in range(1, CHANNELS + 1):
            out.append(ga_line(MATRIX_LABEL, f"3>{c}"))
            out.append(ga_line(MATRIX_LABEL, f"4>{c}"))
        for i in range(1, CHANNELS + 1):
            for o in range(1, CHANNELS + 1):
                idx = crosspoint_index(i, o)
                out.append(ga_line(MATRIX_LABEL, f"1>{idx}"))
                out.append(ga_line(MATRIX_LABEL, f"2>{idx}"))
        for c in range(1, CHANNELS + 1):
            out.append(ga_line(speaker_peq_label(c), SPEAKER_POLARITY_PATH))
        out.append(ga_line(SIGGEN_LABEL, f"{SIGGEN_INDEX}>1"))
        out.append(ga_line(SIGGEN_LABEL, f"{SIGGEN_INDEX}>2"))
        for c in range(1, CHANNELS + 1):
            out.append(ga_line(SIGGEN_LABEL, f"{SIGGEN_INDEX}>{c + 2}"))
        for c in range(1, CHANNELS + 1):
            out.append(ga_line(BACKUP_LABEL, f"{c}>1"))
            out.append(ga_line(BACKUP_LABEL, f"{c}>2"))
        for number in sorted(self._groups):
            out.append(f"GG {number:x}")
            out.append(f"GN {number:x}")
        return out

    def _setting_gets(self) -> list[str]:
        """Values read at connect and every Resync interval, not subscribed:
        they change only when someone tunes the system."""
        out: list[str] = []
        for c in range(1, CHANNELS + 1):
            for index2, *_ in LIMITER_PARAMS.values():
                out.append(ga_line(limiter_label(c), f"0>{index2}"))
            for index2 in ("1", "2", "3"):
                out.append(ga_line(delay_label(c), f"1>{index2}"))
            for index2 in ALARM_PANEL_STATE:
                out.append(ga_line(self._alarm_panel_label, f"{c}>{index2}"))
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
            await self._read_live()
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

    async def _read_live(self) -> None:
        await self._read_list([t for t in self._live_gets() if not self._is_refused(t)])

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
        for c in range(1, CHANNELS + 1):
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
        """Get Signal Level for the analog inputs, the outputs and the Dante
        inputs; how many answered."""
        answered = 0
        for slot in (GL_ANALOG, GL_OUTPUTS, GL_DANTE):
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
        m = _GV_RE.match(text)
        if m:
            slot, ch, raw = m.group(1).lower(), int(m.group(2), 16), int(m.group(3), 16)
            if slot == OUTPUT_SLOT and 1 <= ch <= CHANNELS:
                self.set_child_state_batch(OUTPUT_CHILD_TYPE, ch, {"level_db": hex_level_to_db(raw)})
            return
        m = _GM_RE.match(text)
        if m:
            slot, ch, muted = m.group(1).lower(), int(m.group(2), 16), m.group(3).upper() == "M"
            if slot == OUTPUT_SLOT and 1 <= ch <= CHANNELS:
                self.set_child_state_batch(OUTPUT_CHILD_TYPE, ch, {"mute": muted})
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
        if _SUB_RE.match(text) or _SUB_SUPPORT_RE.match(text):
            return
        log.debug(f"[{self.device_id}] Unhandled line: {text!r}")

    def _apply_alarm_status(self, channel: int, severity: str, kind: str) -> None:
        """A query's reply ``GR c,s,t``: the channel's most severe standing
        alarm."""
        if not 1 <= channel <= CHANNELS:
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
        if not 1 <= channel <= CHANNELS:
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
            if label.lower() == self._alarm_panel_label.lower() and len(idx) == 2 \
                    and idx[1] in ALARM_PANEL_STATE and idx[0].isdigit():
                route = (OUTPUT_CHILD_TYPE, int(idx[0]), ALARM_PANEL_STATE[idx[1]], "onoff")
            else:
                log.debug(f"[{self.device_id}] GA for a value the driver does not follow: "
                          f"{label!r} {'>'.join(idx)}")
                return
        ctype, channel, prop, kind = route
        value: Any
        try:
            if kind == "onoff":
                value = parse_onoff(raw)
            elif kind == "source":
                value = source_name(raw)
            elif kind == "strategy":
                value = STRATEGY_CODES.get(raw.strip().strip('"').upper())
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
        if 1 <= channel <= CHANNELS:
            self.set_child_state_batch(ctype, channel, {prop: value})

    def _apply_signal_levels(self, slot: str, body: str) -> None:
        if slot == GL_ANALOG:
            ctype, prop = INPUT_CHILD_TYPE, "analog_signal_level"
        elif slot == GL_DANTE:
            ctype, prop = INPUT_CHILD_TYPE, "dante_signal_level"
        elif slot == GL_OUTPUTS:
            ctype, prop = OUTPUT_CHILD_TYPE, "signal_level"
        else:
            return
        try:
            raws = [int(tok.strip(), 16) for tok in body.split(",") if tok.strip()]
        except ValueError:
            log.warning(f"[{self.device_id}] Signal levels for slot {slot} are not hexadecimal: {body!r}")
            return
        for c, raw in enumerate(raws[:CHANNELS], 1):
            self.set_child_state_batch(ctype, c, {prop: meter_to_db(raw)})

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

    @staticmethod
    def _channel(params: dict[str, Any], key: str) -> int:
        try:
            c = int(str(params.get(key, "")).strip())
        except ValueError as exc:
            raise CommandParamError(f"{key} is a channel number, 1 to {CHANNELS}") from exc
        if not 1 <= c <= CHANNELS:
            raise CommandParamError(f"{key} is a channel number, 1 to {CHANNELS}")
        return c

    def _group(self, params: dict[str, Any]) -> int:
        try:
            n = int(str(params.get("group", "")).strip())
        except ValueError as exc:
            raise CommandParamError("group is a group number") from exc
        if n not in self._groups:
            raise CommandParamError(f"group {n} is not in the Volume Groups table")
        return n

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
        await self._module_write(sa_line(label, path, value))
        return await self._ga_read(label, path)

    async def _slot_read(self, text: str) -> None:
        await self._query(text, lambda f, t=text: self._reply_to(f, t))

    @staticmethod
    def _onoff_param(params: dict[str, Any], key: str) -> str:
        return "O" if coerce_onoff(params.get(key)) else "F"

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
            if command == "set_output_level":
                await self._fire(f"SV {OUTPUT_SLOT},{c:x},{db_to_hex_level(float(params['level']))}")
            else:
                amount = float(params.get("amount", 1.0))
                steps = int(round(abs(amount) * 2))
                if steps == 0:
                    return None
                await self._fire(f"SI {OUTPUT_SLOT},{c:x},{1 if amount > 0 else 0},{steps:x}")
            await self._slot_read(f"GV {OUTPUT_SLOT},{c:x}")
            return None
        if command in ("set_output_mute", "toggle_output_mute"):
            c = self._channel(params, "output")
            state = "T" if command == "toggle_output_mute" else ("M" if coerce_onoff(params.get("mute")) else "U")
            await self._fire(f"SM {OUTPUT_SLOT},{c:x},{state}")
            await self._slot_read(f"GM {OUTPUT_SLOT},{c:x}")
            return None
        if command == "set_output_polarity":
            c = self._channel(params, "output")
            await self._module_set(speaker_peq_label(c), SPEAKER_POLARITY_PATH, self._onoff_param(params, "inverted"))
            return None
        if command in ("set_crosspoint", "toggle_crosspoint", "set_crosspoint_level"):
            i = self._channel(params, "input")
            o = self._channel(params, "output")
            idx = crosspoint_index(i, o)
            if command == "set_crosspoint_level":
                level = float(params["level"])
                if not LEVEL_MIN <= level <= LEVEL_MAX:
                    raise CommandParamError("a cross-point level is -60.5 to 0 dB")
                await self._module_set(MATRIX_LABEL, f"2>{idx}", format_number(round(level * 2) / 2))
            else:
                value = "T" if command == "toggle_crosspoint" else self._onoff_param(params, "on")
                await self._module_set(MATRIX_LABEL, f"1>{idx}", value)
            return None
        if command == "set_matrix_input_mute":
            c = self._channel(params, "input")
            await self._module_set(MATRIX_LABEL, f"3>{c}", self._onoff_param(params, "mute"))
            return None
        if command == "set_matrix_output_mute":
            c = self._channel(params, "output")
            await self._module_set(MATRIX_LABEL, f"4>{c}", self._onoff_param(params, "mute"))
            return None
        if command == "set_input_source":
            c = self._channel(params, "input")
            await self._module_set(BACKUP_LABEL, f"{c}>2", source_wire(params.get("source", "")))
            return None
        if command == "set_input_backup_strategy":
            c = self._channel(params, "input")
            wire = encode_table_value("strategy", BACKUP_PARAMS["strategy"], params.get("strategy", ""))
            await self._module_set(BACKUP_LABEL, f"{c}>1", wire)
            return None
        if command == "set_input_backup":
            c = self._channel(params, "input")
            name = str(params.get("parameter", "")).strip().lower()
            spec = BACKUP_PARAMS.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(BACKUP_PARAMS))
            wire = encode_table_value(name, spec, params.get("value"))
            return await self._module_set(BACKUP_LABEL, f"{c}>{spec[0]}", wire)
        if command == "set_output_limiter":
            c = self._channel(params, "output")
            name = str(params.get("parameter", "")).strip()
            spec = LIMITER_PARAMS.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(LIMITER_PARAMS))
            index2, _, kind, lo, hi, step = spec
            if kind == "onoff":
                raw = params.get("value")
                try:
                    on = float(raw) != 0
                except (TypeError, ValueError):
                    on = coerce_onoff(raw)
                wire = "O" if on else "F"
            else:
                value = float(params["value"])
                if not lo <= value <= hi:
                    raise CommandParamError(f"{name} is {format_number(lo)} to {format_number(hi)}")
                wire = format_number(round(round(value / step) * step, 3))
            await self._module_set(limiter_label(c), f"0>{index2}", wire)
            return None
        if command in ("set_output_delay", "set_output_align_delay"):
            c = self._channel(params, "output")
            samples = ms_to_samples(float(params["delay_ms"]))
            ceiling, path, what = ((DELAY_MAX_SAMPLES, "1>1", "delay is 0 to 2000 ms")
                                   if command == "set_output_delay"
                                   else (ALIGN_MAX_SAMPLES, "1>3", "align delay is 0 to 100 ms"))
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
                c, table, label, bands = (self._channel(params, "input"), INPUT_PEQ_PARAMS, peq_label,
                                          INPUT_PEQ_BANDS)
            else:
                c, table, label, bands = (self._channel(params, "output"), SPEAKER_PEQ_PARAMS,
                                          speaker_peq_label, SPEAKER_PEQ_BANDS)
            band = int(params["band"])
            if not 1 <= band <= bands:
                raise CommandParamError(f"band is 1 to {bands}")
            name = str(params.get("parameter", "")).strip().lower()
            spec = table.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(table))
            wire = encode_table_value(name, (name,) + spec[1:], params.get("value"))
            return await self._module_set(label(c), f"{band}>{spec[0]}", wire)
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
        if command == "set_array_eq":
            c = self._channel(params, "channel")
            name = str(params.get("parameter", "")).strip().lower()
            spec = ARRAY_EQ_PARAMS.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(ARRAY_EQ_PARAMS))
            wire = encode_table_value(name, (name,) + spec[1:], params.get("value"))
            return await self._module_set(array_eq_label(c), f"1>{spec[0]}", wire)
        if command == "set_signal_generator_level":
            level = float(params["level"])
            if not LEVEL_MIN <= level <= 12.0:
                raise CommandParamError("the signal generator level is -60.5 to +12 dB")
            await self._module_set(SIGGEN_LABEL, f"{SIGGEN_INDEX}>1", format_number(round(level * 2) / 2))
            return None
        if command == "set_signal_generator_mute":
            await self._module_set(SIGGEN_LABEL, f"{SIGGEN_INDEX}>2", self._onoff_param(params, "mute"))
            return None
        if command == "set_output_pink_noise":
            c = self._channel(params, "output")
            await self._module_set(SIGGEN_LABEL, f"{SIGGEN_INDEX}>{c + 2}", self._onoff_param(params, "on"))
            return None
        if command == "set_alarm_panel":
            c = self._channel(params, "output")
            name = str(params.get("parameter", "")).strip().lower()
            spec = ALARM_PANEL_PARAMS.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(ALARM_PANEL_PARAMS))
            wire = encode_table_value(name, (name,) + spec[1:], params.get("value"))
            return await self._module_set(self._alarm_panel_label, f"{c}>{spec[0]}", wire)
        if command == "recall_parameter_set":
            n = int(params["number"])
            if not 1 <= n <= PARAMETER_SET_MAX:
                raise CommandParamError("a parameter set is 1..64")
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
        if command == "resync":
            self._refused.clear()
            await self._read_power()
            await self._find_alarm_panel_label()
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
        """Ask for the amplifier's standby state, subscription support, its
        fixed modules and each group, and report which answered. It asks on
        the driver's own connection when there is one: the document says
        nothing about how many connections a PSXProxy port takes."""
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")
        host = str(self.config.get("host", "")).strip()
        try:
            port = int(self.config.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not host or not port:
            raise ValueError("Enter the address and port from the amplifier's PowerShareX "
                             "Amplifier Properties first.")
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
                raise ConnectionError(f"Could not reach {host}:{port}: check the address and port ({exc})") from exc
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
        panel = None
        try:
            gy = await ask_or_none("GY", lambda t: bool(_GY_RE.match(t)))
            sub = await ask_or_none("SUB", lambda t: bool(_SUB_SUPPORT_RE.match(t)))
            push = bool(sub) and sub.strip().lower().endswith("yes")
            ip = await ask_or_none("IP", lambda t: bool(_IP_RE.match(t)))
            gf = await ask_or_none("GF", lambda t: bool(_GF_RE.match(t)))
            checks: list[tuple[str, str]] = [
                ("Matrix 1", ga_line(MATRIX_LABEL, "3>1")),
                (SIGGEN_LABEL, ga_line(SIGGEN_LABEL, f"{SIGGEN_INDEX}>2")),
                (BACKUP_LABEL, ga_line(BACKUP_LABEL, "1>1")),
                ("Limiter 1", ga_line(limiter_label(1), "0>1")),
                ("Delay 1", ga_line(delay_label(1), "1>1")),
                ("SpeakerPEQ 1 polarity", ga_line(speaker_peq_label(1), SPEAKER_POLARITY_PATH)),
            ]
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
            for label in ALARM_PANEL_LABELS:
                text = ga_line(label, "1>1")
                try:
                    reply = await ask(text, lambda t, x=text: self._reply_to(t.encode(), x))
                except ValueError as exc:
                    refusals.append(f"'{label}' ({exc})")
                    continue
                if reply:
                    panel = label
                break
            if panel:
                answered.append(f"alarm panel as '{panel}'")
            elif refusals and len(refusals) == len(ALARM_PANEL_LABELS):
                rejected.append("Alarm/Fault Panel: " + ", ".join(refusals))
            else:
                silent.append("Alarm/Fault Panel")
        finally:
            if session is not None:
                await session.close()
        if gy is None:
            message = (f"{host}:{port} did not answer the standby query: check that the port is "
                       f"the one in this amplifier's PowerShareX Amplifier Properties.")
        else:
            state = "in standby" if gy.strip().upper().endswith("S") else "on"
            parts = [f"The amplifier answered and is {state}"
                     + (f" at {ip.split()[1]}" if ip else "") + "."]
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
            "ok": gy is not None and not silent and not rejected,
            "message": message,
            "answered": answered,
            "silent": silent,
            "rejected": rejected,
            "push_supported": push,
            "standby": None if gy is None else gy.strip().upper().endswith("S"),
            "fault": None if gf is None else gf.strip().upper().endswith("F"),
            "ip_address": ip.split()[1] if ip else None,
            "alarm_panel_label": panel,
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
                frame, self.buf = parse_powersharex_stream(self.buf)
                if frame is None:
                    break
                if not frame:
                    if not self.buf:
                        break
                    continue
                if frame[0] == NAK:
                    raise _Refused(BosePowerShareXDriver._nak_message(frame),
                                   BosePowerShareXDriver._nak_code(frame))
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
