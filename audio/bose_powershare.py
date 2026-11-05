"""
Bose Professional PowerShare PS404D / PS604D — ControlSpace Serial Control
Protocol driver.

Controls a PowerShare PS404D or PS604D adaptable power amplifier (four input
channels, four output channels, Dante) through the ControlSpace Serial
Control Protocol on TCP port 10055, the amplifier's fixed serial-over-Ethernet
port (it serves up to eight connections). There is no RS-232 port on these
amplifiers; the document gives RS-232 to the ESP and EX processors only.

What it controls:
    Standby (``SY`` / ``GY``), each output's level and mute and each input's
    mute by slot and channel (``SV`` / ``GV`` / ``SI`` / ``SM`` / ``GM``,
    inputs are slot 1 and outputs slot 2 on a PSx04D, Table 2), the "Matrix 1"
    mixer (sixteen cross-points on / off and level, a mute per input and per
    output), each output's limiter and delay, the input and speaker
    parametric EQs and the band-pass filter by command, parameter sets,
    level groups, Get Signal Level for inputs (dBFS) and outputs (dBV), and the
    alarm log (``GH`` / ``CH``). Two settings live in modules the integrator
    can rename in ControlSpace Designer (each input's analog / Dante source,
    each output's polarity), so they need the label typed in the device's
    settings; nothing else depends on a label.

Why Python:
    Every system and device command carries hexadecimal numbers (a level of
    5Bh is -14.5 dB, parameter set 11 is ``b``, the signal-level array is hex),
    and a YAML driver reads a captured number in base 10 only. On top of that: the
    module commands' ACK / NAK bytes are not line-terminated and a NAK's code
    is the error the user should see; the subscribe handshake needs a
    capability check with a polled fallback; one Get Signal Level reply fans
    out into four children; the alarm log arrives as a bracketed block over
    several lines; and a matrix cross-point's index is computed from its input
    and output.

Push, with a read-back after every write (Principle 2):
    ``SUB "<GET command>"`` makes the amplifier send the value at once and
    again whenever it changes (section 9: parameter set, group levels and
    mutes, slot levels and mutes, any module parameter). On connect the driver
    asks ``SUB`` alone; an amplifier answering ``SUB yes`` gets every output
    level and mute, every input mute, the matrix, the labelled source and
    polarity settings, the parameter set and the groups subscribed, renewed
    every Resync interval (which is also the re-arm after a reboot). One that
    does not is read every Poll interval instead, and ``push_supported`` says
    which. Standby has no subscription (it is not in section 9's tables), so
    ``GY`` is read every poll. The limiter, delay and alarm log are read at
    connect and every Resync interval, and after a write. The document
    recommends following every Set with a Get, and the driver does.

Liveness:
    ``GY`` answers ``GY N`` or ``GY S`` in either power state. A link quiet for
    the probe interval is asked; two misses drop it with a typed
    ``no_response`` fault.

Not this driver: the PowerMatch amplifiers (PM8500N and the discontinued
PM8250N / PM4500N / PM4250N: eight channels on four slots, fault and alarm
reporting, output configuration, a signal generator, array EQ and other
module layouts), the PowerShareX (reached through a proxy port on an EX or
ESP, with its own module tables) and the ControlSpace processors
(``bose_controlspace``). Each is its own roadmap row.

Source (Bose Professional, manufacturer document):
    ControlSpace Serial Control Protocol v5.15, revision date May 27, 2025
    (published as a zip holding the PDF). Sections 2-5, 6 (6.2 for the
    amplifier's modules) and 9. For the PowerShare it differs from v5.13 only
    in the matrix: mutes for inputs and outputs 1-4 (v5.13 printed 1-32) and a
    note that only PowerShare and PowerShareX amplifiers have them.
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
DEFAULT_PORT = 10055
CHANNELS = 4
INPUT_SLOT = "1"       # Table 2, PSx04D: slot 1 = In 1-4
OUTPUT_SLOT = "2"      # Table 2, PSx04D: slot 2 = Out 1-4
GL_INPUTS = "1"        # GL Indices, PS404D / PS604D: In 1-4
GL_OUTPUTS = "2"       # GL Indices, PS404D / PS604D: Out 1-4

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
    "01": "Invalid module name: no module in the amplifier's design has that label, "
          "or two modules share it",
    "02": "Illegal index: the index or number of indices is wrong for that module",
    "03": "Value out of range for that parameter",
    "99": "Unknown error",
}

# Amplifier outputs have no gain: levels run -60 dB (0h) to 0 dB (78h) in
# 0.5 dB steps, FFh = -60.5 dB / off (sections 4.2 and 5.1); the Amp Output
# and matrix module levels -60.5 to 0.0 dB (6.2.4, 6.2.11).
LEVEL_MIN = -60.5
LEVEL_MAX = 0.0
HEX_LEVEL_MAX = 0x78
HEX_LEVEL_OFF = 0xFF
GROUP_MAX = 0x40
PARAMETER_SET_MAX = 0xFF

# Fixed module labels on a PowerShare (section 6.2: only Input and Amp
# Output modules can be renamed).
MATRIX_LABEL = "Matrix 1"
# 6.2.4: Index 2 = (Input # - 1) x 8 + Output #. Only the PowerShareX is
# given the x 4 form; the PowerShare is not, so it keeps the x 8 stride even
# with four outputs. Unverified on a unit: the first bench check.
MATRIX_STRIDE = 8
# 6.2.10: one tap; on a PowerShare Index 2 is 1 = delay time (0 to 2400
# samples, 50 ms), 2 reserved, 3 = bypass. The document names the modules
# "Delay 3" through to "Delay 4" for PowerShare, which reads as a typo for
# 1 to 4 (four channels, and every other fixed module counts from 1).
DELAY_MAX_SAMPLES = 2400
# The document's own conversion (6.2.10 examples): samples = seconds x 48000.
SAMPLE_RATE_HZ = 48000


def peq_label(channel: int) -> str:
    """6.2.5: the PowerShare's input PEQ modules (nine bands each)."""
    return f"PEQ-9band {channel}"


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


def parse_powershare_stream(buf: bytes) -> tuple[bytes | None, bytes]:
    """CallableFrameParser function: one CR (or LF) terminated line per
    message, with a bare ACK byte and a ``NAK nn`` delivered as their own
    frames whether or not a line ending follows them (section 3 says only
    "ACK (ASCII 0x06)"; 5.11 and 5.13 print ``<ACK><CR>``). The returned
    buffer is what the parser keeps."""
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
    if s in ("1", "true", "on", "yes", "y", "o", "mute", "muted", "inverted", "bypass", "bypassed"):
        return True
    if s in ("0", "false", "off", "no", "n", "f", "unmute", "unmuted", "normal"):
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
    """Get Signal Level (5.4): on a PowerShare inputs read dBFS and outputs
    dBV max, both -60.0 to 0.0 in 0.5 dB steps from 0h (0 dBV = 71 V for a
    mono channel, 141 V bridged)."""
    return round(-60.0 + raw / 2.0, 1)


def crosspoint_index(input_no: int, output_no: int) -> int:
    """6.2.4: Index 2 = (Input # - 1) x 8 + Output #."""
    return (int(input_no) - 1) * MATRIX_STRIDE + int(output_no)


def crosspoint_of(index: int) -> tuple[int, int] | None:
    """The (input, output) a matrix Index 2 names on a 4 x 4 PowerShare."""
    i, o = divmod(int(index) - 1, MATRIX_STRIDE)
    i, o = i + 1, o + 1
    if 1 <= i <= CHANNELS and 1 <= o <= CHANNELS:
        return i, o
    return None


def samples_to_ms(samples: int) -> float:
    return round(int(samples) * 1000.0 / SAMPLE_RATE_HZ, 3)


def ms_to_samples(ms: float) -> int:
    return int(round(float(ms) * SAMPLE_RATE_HZ / 1000.0))


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
_GH_START_RE = re.compile(r"^GH\s*\[(.*)$")
_IP_RE = re.compile(r"^IP\s+(\d{1,3}(?:\.\d{1,3}){3})$")
_NP_RE = re.compile(r"^NP\s*([TMGtmg]),(.+)$")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{1,2}$")


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


# ── Module parameter tables (section 6.2) ───────────────────────────────────

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

# parameter -> (Index 2, kind, min, max). kind: number, onoff, choice.
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
BAND_PASS_PARAMS: dict[str, tuple[str, str, Any, Any]] = {
    "hpf_type": ("2>1", "choice", [t for t, _ in FILTER_TYPES], None),
    "hpf_frequency": ("2>2", "number", 20, 20000),
    "lpf_type": ("2>3", "choice", [t for t, _ in FILTER_TYPES], None),
    "lpf_frequency": ("2>4", "number", 20, 20000),
    "hpf_bypass": ("2>8", "onoff", None, None),
    "lpf_bypass": ("2>9", "onoff", None, None),
}
# 6.2.9, Index 1 always 0: (Index 2, state variable, min, max, step).
LIMITER_PARAMS: dict[str, tuple[str, str, float, float, float]] = {
    "peak_threshold": ("1", "limiter_peak_v", 0.5, 142.0, 0.5),
    "rms_threshold": ("2", "limiter_rms_v", 0.5, 142.0, 0.5),
    "rms_attack": ("3", "limiter_rms_attack_ms", 500, 10000, 100),
    "rms_release": ("4", "limiter_rms_release_ms", 500, 10000, 100),
}


def band_pass_path(parameter: str) -> str:
    """6.2.7: Index 1 is always 1; Index 2 is the parameter."""
    return "1>" + BAND_PASS_PARAMS[parameter][0].split(">")[1]


def encode_table_value(parameter: str, spec: tuple[str, str, Any, Any], value: Any) -> str:
    """A value for one of the EQ / band-pass parameters, checked against the
    document's range."""
    _, kind, lo, hi = spec
    if kind == "onoff":
        return "O" if coerce_onoff(value) else "F"
    if kind == "choice":
        text = str(value).strip()
        for choice in lo:
            if text.lower() == choice.lower():
                return choice
        raise CommandParamError(f"{parameter} is one of {', '.join(lo)}")
    try:
        v = float(str(value).strip())
    except ValueError as exc:
        raise CommandParamError(f"{parameter} takes a number") from exc
    if not lo <= v <= hi:
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


def read_labels(config: dict[str, Any], kind: str) -> tuple[dict[int, str], list[str]]:
    """The typed Input / Amp Output labels, by channel; blank = not set."""
    labels: dict[int, str] = {}
    problems: list[str] = []
    for c in range(1, CHANNELS + 1):
        text = str(config.get(f"{kind}_{c}_label") or "").strip()
        if not text:
            continue
        if '"' in text:
            problems.append(f"{kind.title()} {c} label: a label cannot contain a double quote")
            continue
        labels[c] = text
    return labels, problems


class BosePowerShareDriver(BaseDriver):
    """Bose Professional PowerShare PS404D / PS604D over TCP 10055."""

    DRIVER_INFO = {
        "id": "bose_powershare",
        "name": "Bose Professional PowerShare (PS404D / PS604D)",
        "manufacturer": "Bose Professional",
        "category": "audio",
        "version": "1.0.0",
        # BaseDriver.local_address(), which the Test Connection socket binds
        # to, needs 0.37.0 (set by hand: the contract check cannot see it).
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": (
            "Controls Bose Professional PowerShare PS404D and PS604D amplifiers through "
            "the ControlSpace Serial Control Protocol on TCP port 10055: standby, each "
            "output's level and mute, each input's mute and analog / Dante source, the "
            "4 x 4 matrix, output limiters and delays, the input and speaker EQs, "
            "parameter sets, volume groups, signal levels and the alarm log. Values are "
            "subscribed, so changes made from a ControlCenter zone controller or "
            "ControlSpace Designer appear at once."
        ),
        "source_url": "https://assets.boseprofessional.com/m/4b14907043cbedb3/original/ControlSpace-Serial-Protocol-v5-15.zip",
        "tags": ["amplifier", "bose", "powershare", "install-audio", "dante", "controlspace"],
        "verified": False,
        "simulated": True,
        "protocols": ["bose-controlspace-serial"],
        "ports": [10055],
        "transport": "tcp",
        "discovery": {
            # GY (Get Standby Status, 5.8) is answered by PowerMatch,
            # PowerShare and PowerShareX amplifiers and "not supported on ESP
            # devices", and changes nothing. GC (Get Configuration, 5.9) is
            # PowerMatch only, so a PowerShare answers GY and leaves GC alone.
            "tcp_probe": {
                "port": 10055,
                "send_ascii": "GY\r",
                "expect_regex": r"^GY ?[SN]",
                "timeout_ms": 1500,
                "then": {"send_ascii": "GC\r", "expect_silence": True, "timeout_ms": 600},
            },
            "port_open": [10055],
            # Bose Corporation's registered prefixes (IEEE registry), the
            # same list bose_controlspace carries.
            "oui": ["00:0c:8a", "04:52:c7", "08:df:1f", "28:11:a5", "2c:41:a1", "48:22:1d",
                    "4c:87:5d", "60:ab:d2", "68:f2:1f", "78:2b:64", "ac:bf:71", "bc:87:fa",
                    "c8:7b:23", "e4:58:bc"],
            "manufacturer_alias": ["bose", "bose professional", "bose corporation", "powershare"],
        },
        "compatible_models": [
            {
                "manufacturer": "Bose Professional",
                "models": ["PS404D", "PS604D"],
                "confidence": "untested",
                "notes": (
                    "PowerShare adaptable power amplifiers: four inputs (analog or Dante), "
                    "four outputs, TCP 10055 (fixed, up to eight connections). Built from "
                    "the ControlSpace Serial Control Protocol v5.15 and the simulator; not "
                    "yet run against an amplifier."
                ),
            },
        ],
        "help": {
            "overview": (
                "PowerShare control over the ControlSpace Serial Control Protocol (TCP "
                "10055, no login). Each input and output is a child entity: output level "
                "and mute, input mute, the matrix cross-points into each output, the "
                "limiter and delay, and signal levels. Standby, parameter sets, volume "
                "groups and the alarm log are on the device. Values are subscribed where "
                "the amplifier supports it, so panels follow changes made anywhere."
            ),
            "setup": (
                "STEP 1 - Add the device.\n"
                "Enter the amplifier's IP address (ControlSpace Designer's Hardware "
                "Manager shows it). The port is 10055.\n\n"
                "STEP 2 - Optional labels.\n"
                "Each input's analog / Dante source and each output's polarity are set "
                "through modules you can rename in ControlSpace Designer. To control "
                "them, type each Input and Amp Output label exactly as Designer shows "
                "it. Everything else works without labels.\n\n"
                "STEP 3 - Groups and parameter sets.\n"
                "Add the volume groups and parameter sets from your design by number "
                "and name if you want to recall them by name or bind group levels.\n\n"
                "STEP 4 - Test.\n"
                "Run Test Connection. It reports whether the amplifier answered, "
                "whether it supports subscriptions, and whether each label you typed "
                "is in its design."
            ),
            "connection": (
                "Port 10055 needs no login. Going online with ControlSpace Designer "
                "closes every third-party connection; the driver reconnects on its own "
                "once the design is loaded. The amplifier takes up to eight connections."
            ),
        },
        "default_config": {
            "host": "",
            "port": 10055,
            "input_1_label": "",
            "input_2_label": "",
            "input_3_label": "",
            "input_4_label": "",
            "output_1_label": "",
            "output_2_label": "",
            "output_3_label": "",
            "output_4_label": "",
            "groups": [],
            "parameter_sets": [],
            "enable_meters": False,
            "meter_interval_s": 1.0,
            "poll_interval": 10,
            "resync_interval": 60,
            "inter_command_delay": 0,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 10055, "label": "Port", "min": 1,
                     "max": 65535, "advanced": True,
                     "description": "Fixed at 10055 on PowerShare amplifiers."},
            "input_1_label": {"type": "string", "default": "", "label": "Input 1 Label",
                              "help": "The Input module's label in ControlSpace Designer. "
                                      "Needed only for the Analog / Dante source."},
            "input_2_label": {"type": "string", "default": "", "label": "Input 2 Label"},
            "input_3_label": {"type": "string", "default": "", "label": "Input 3 Label"},
            "input_4_label": {"type": "string", "default": "", "label": "Input 4 Label"},
            "output_1_label": {"type": "string", "default": "", "label": "Output 1 Label",
                               "help": "The Amp Output module's label in ControlSpace Designer. "
                                       "Needed only for polarity."},
            "output_2_label": {"type": "string", "default": "", "label": "Output 2 Label"},
            "output_3_label": {"type": "string", "default": "", "label": "Output 3 Label"},
            "output_4_label": {"type": "string", "default": "", "label": "Output 4 Label"},
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
                "help": "How often standby is read. On an amplifier without subscription "
                        "support every level, mute and cross-point is read this often too.",
            },
            "resync_interval": {
                "type": "integer", "default": 60, "min": 10, "max": 3600,
                "label": "Resync interval (s)", "advanced": True,
                "help": "How often the subscriptions are renewed and the limiter, delay and "
                        "alarm log read again.",
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
                    "name": {
                        "type": "string",
                        "label": "Label",
                        "help": "The Input's label from the device settings, else Input n.",
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Mute",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "matrix_mute": {
                        "type": "boolean",
                        "label": "Matrix Input Mute",
                        "control": True,
                        "help": "The mute on this input of Matrix 1.",
                    },
                    "source": {
                        "type": "enum",
                        "values": ["analog", "dante"],
                        "label": "Source",
                        "control": True,
                        "help": (
                            "Analog or Dante. Read only when the input's label is set in the "
                            "device settings."
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
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "state_variables": {
                    "name": {
                        "type": "string",
                        "label": "Label",
                        "help": "The Amp Output's label from the device settings, else Output n.",
                    },
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
                        "help": "Read only when the output's label is set in the device settings.",
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
                            "0 dBV is the channel's full output (71 V mono, 141 V bridged). Read "
                            "only while Poll signal levels is on."
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
                        "min": 0.5,
                        "max": 142.0,
                        "step": 0.5,
                        "unit": "V",
                        "help": "0.5 to 71 V, or to 142 V bridged.",
                    },
                    "limiter_rms_v": {
                        "type": "number",
                        "label": "Limiter RMS Threshold (V)",
                        "min": 0.5,
                        "max": 142.0,
                        "step": 0.5,
                        "unit": "V",
                        "help": "0.5 to 50 V, or to 142 V bridged.",
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
                    "delay_ms": {
                        "type": "number",
                        "label": "Delay (ms)",
                        "min": 0.0,
                        "max": 50.0,
                        "unit": "ms",
                        "help": "0 to 50 ms (2400 samples at 48 kHz).",
                    },
                    "delay_bypass": {"type": "boolean", "label": "Delay Bypass"},
                },
                "summary_fields": ["level_db", "mute"],
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
            "parameter_set": {"type": "integer", "label": "Last Parameter Set", "min": 0,
                              "max": 255, "control": True,
                              "help": "The last parameter set recalled; 0 after power-up."},
            "parameter_set_name": {"type": "string", "label": "Last Parameter Set Name"},
            "parameter_set_options": {"type": "string", "label": "Parameter Set List",
                                      "help": "The Parameter Sets table as a picker list."},
            "push_supported": {"type": "boolean", "label": "Subscriptions Supported",
                               "help": "True when the amplifier answered SUB with yes; otherwise "
                                       "every value is read each poll."},
            "alarm_count": {"type": "integer", "label": "Alarm Log Entries", "min": 0,
                            "cloud_priority": "high",
                            "help": "How many alarms the amplifier's log holds."},
            "alarm_log": {"type": "string", "label": "Alarm Log",
                          "help": "Every entry in the amplifier's alarm log as it reports them, "
                                  "separated by |."},
            "ip_address": {"type": "string", "label": "IP Address"},
            "subnet_mask": {"type": "string", "label": "Subnet Mask"},
            "gateway": {"type": "string", "label": "Default Gateway"},
            "addressing": {"type": "enum", "values": ["dhcp", "static"], "label": "Addressing"},
            "config_problems": {"type": "string", "label": "Setting Problems",
                                "help": "A label the amplifier does not know, or a table row "
                                        "that could not be read."},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "commands": {
            "power_on": {
                "label": "Power On (Normal)",
                "params": {},
                "sets": {"power": True},
                "help": (
                    "Takes the amplifier out of standby: the power LED goes solid green and the "
                    "outputs pass audio again. It takes a few seconds."
                ),
            },
            "standby": {
                "label": "Standby",
                "params": {},
                "sets": {"power": False},
                "help": (
                    "Puts the amplifier in standby: the power LED blinks green and every output "
                    "goes silent. The amplifier still answers on the network."
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
                "help": (
                    "Inverts or restores an output's polarity. Needs the output's Amp Output label "
                    "in the device settings."
                ),
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
                "help": "Mutes or unmutes an input channel.",
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
                        "values": [
                            {"value": "analog", "label": "Analog"},
                            {"value": "dante", "label": "Dante"},
                        ],
                    },
                },
                "sets": {"source": "{source}"},
                "help": (
                    "Switches an input between its analog connector and its Dante channel. Needs "
                    "the input's label in the device settings."
                ),
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
                        ],
                    },
                    "value": {
                        "type": "number",
                        "required": True,
                        "label": "Value",
                        "min": 0.5,
                        "max": 10000,
                        "help": (
                            "Thresholds 0.5 to 71 V peak and 50 V RMS (142 V bridged) in 0.5 V "
                            "steps; attack and release 500 to 10,000 ms in 100 ms steps."
                        ),
                    },
                },
                "help": (
                    "Changes one setting of an output's limiter, which protects the loudspeakers. "
                    "The amplifier refuses a value outside the range for the output's mode."
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
                        "max": 50,
                        "unit": "ms",
                        "help": "0 to 50 ms, sent as samples at 48 kHz.",
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
                        "max": 9,
                    },
                    "parameter": {
                        "type": "enum",
                        "required": True,
                        "label": "Parameter",
                        "values": [
                            {"value": "frequency", "label": "Frequency (20-20000 Hz)"},
                            {"value": "q", "label": "Q (0.1-10)"},
                            {"value": "gain", "label": "Gain (-20 to +20 dB)"},
                            {"value": "slope", "label": "Slope (0, -6, -12 dB/oct)"},
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
                    "Changes one band of an input's nine-band parametric EQ (PEQ-9band). Returns "
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
                            {"value": "gain", "label": "Gain (-20 to +20 dB)"},
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
                        "help": (
                            "The Parameter Set number (1-255). Names come from the Parameter Sets "
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
                            "Limiter 2, PEQ-9band 3, or a renamed Input or Amp Output)."
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
                            "Limiter 2, PEQ-9band 3, or a renamed Input or Amp Output)."
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
                    "Returns every network setting to its factory default (DHCP) at the next "
                    "reboot, so the amplifier may come back on a different address."
                ),
                "help": (
                    "Returns the amplifier's network settings to their defaults (DHCP) at the next "
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
                "help": "Reads every value again and renews the subscriptions.",
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
                    "Opens a second session to the amplifier and asks it for its standby "
                    "state, its matrix, each label you typed and each group. Nothing on "
                    "the amplifier changes."
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
        self._input_labels, problems = read_labels(config, "input")
        self._problems.extend(problems)
        self._output_labels, problems = read_labels(config, "output")
        self._problems.extend(problems)
        self._groups, problems = parse_groups_config(config.get("groups") or [])
        self._problems.extend(problems)
        self._parameter_set_names, problems = parse_parameter_sets_config(config.get("parameter_sets") or [])
        self._problems.extend(problems)
        self._label_problems: dict[str, str] = {}
        # (module label lowered, index path) -> (child type, channel, prop, kind)
        self._route: dict[tuple[str, tuple[str, ...]], tuple[str, int, str, str]] = {}
        for c in range(1, CHANNELS + 1):
            self._route[(MATRIX_LABEL.lower(), ("3", str(c)))] = (INPUT_CHILD_TYPE, c, "matrix_mute", "onoff")
            self._route[(MATRIX_LABEL.lower(), ("4", str(c)))] = (OUTPUT_CHILD_TYPE, c, "matrix_mute", "onoff")
            for i in range(1, CHANNELS + 1):
                idx = str(crosspoint_index(i, c))
                self._route[(MATRIX_LABEL.lower(), ("1", idx))] = (OUTPUT_CHILD_TYPE, c, f"input_{i}", "onoff")
                self._route[(MATRIX_LABEL.lower(), ("2", idx))] = (OUTPUT_CHILD_TYPE, c, f"input_{i}_level", "number")
            for _, (index2, prop, *_rest) in LIMITER_PARAMS.items():
                kind = "int" if prop.endswith("_ms") else "number"
                self._route[(limiter_label(c).lower(), ("0", index2))] = (OUTPUT_CHILD_TYPE, c, prop, kind)
            self._route[(delay_label(c).lower(), ("1", "1"))] = (OUTPUT_CHILD_TYPE, c, "delay_ms", "samples")
            self._route[(delay_label(c).lower(), ("1", "3"))] = (OUTPUT_CHILD_TYPE, c, "delay_bypass", "onoff")
        for c, label in self._input_labels.items():
            self._route[(label.lower(), ("3",))] = (INPUT_CHILD_TYPE, c, "source", "source")
        for c, label in self._output_labels.items():
            self._route[(label.lower(), ("3",))] = (OUTPUT_CHILD_TYPE, c, "polarity_inverted", "onoff")
        self._waiters: list[tuple[Callable[[bytes], bool], asyncio.Future[bytes]]] = []
        self._push_supported = False
        self._send_lock = asyncio.Lock()
        # When the amplifier last sent anything (event-loop time).
        self._last_rx: float | None = None
        self._last_resync: float | None = None
        self._meter_task: asyncio.Task | None = None
        self._standby_task: asyncio.Task | None = None
        # The alarm log block being collected, and who is waiting for it.
        self._gh_lines: list[str] | None = None
        self._gh_waiter: asyncio.Future[list[str]] | None = None
        super().__init__(device_id, config, state, events)
        for problem in self._problems:
            log.warning(f"[{self.device_id}] Settings: {problem}")

    # ── Transport ──

    def _transport_kwargs(self, transport_type: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        kwargs["delimiter"] = None
        return kwargs

    def _create_frame_parser(self) -> CallableFrameParser:
        return CallableFrameParser(parse_powershare_stream)

    # ── Lifecycle ──

    async def _initial_sync(self) -> None:
        self.set_state("parameter_set_options", json.dumps(
            [{"value": n, "label": f"{n}: {name}"} for n, name in sorted(self._parameter_set_names.items())]
        ))
        self._label_problems.clear()
        self._publish_problems()
        self._register_children()
        await self._read_power()
        await self._detect_push_support()
        await self._read_network()
        await self._resync()
        self._start_meter_loop()

    async def poll(self) -> None:
        """Standby every poll (it has no subscription); every live value too
        on an amplifier without subscriptions; the subscriptions renewed and
        the limiter, delay and alarm log read every Resync interval. Stops at
        the first silence and lets the platform's checks judge it."""
        if await self._read_power() is None:
            return
        due = self._last_resync is None or (
            asyncio.get_running_loop().time() - self._last_resync >= self._resync_interval()
        )
        if due:
            await self._resync()
        elif not self._push_supported:
            await self._read_live()
        self._publish_problems()

    async def _close_session(self) -> None:
        self._stop_meter_loop()
        task = self._standby_task
        self._standby_task = None
        if task is not None and not task.done():
            task.cancel()
        for _, fut in self._waiters:
            if not fut.done():
                fut.cancel()
        self._waiters.clear()
        if self._gh_waiter is not None and not self._gh_waiter.done():
            self._gh_waiter.cancel()
        self._gh_waiter = None
        self._gh_lines = None

    def _register_children(self) -> None:
        for c in range(1, CHANNELS + 1):
            self.register_child(INPUT_CHILD_TYPE, c,
                                initial_state={"name": self._input_labels.get(c, f"Input {c}")})
            self.register_child(OUTPUT_CHILD_TYPE, c,
                                initial_state={"name": self._output_labels.get(c, f"Output {c}")})
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
        """Table problems plus every label the amplifier refused, written
        again on every poll while they hold."""
        self.set_state("config_problems", "; ".join(self._problems + list(self._label_problems.values())))

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
        raises). None on silence. The reply also routes to state through
        on_data_received."""
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

    async def _read_network(self) -> None:
        """Identity reads, fire-and-forget: the replies route to state."""
        for line in ("IP", "NP T", "NP M", "NP G"):
            await self._fire(line)

    def _live_gets(self) -> list[tuple[str, str | None]]:
        """Every subscribable value the driver follows, as (GET text, the
        label it depends on or None)."""
        out: list[tuple[str, str | None]] = [("GS", None)]
        for c in range(1, CHANNELS + 1):
            out.append((f"GV {OUTPUT_SLOT},{c:x}", None))
            out.append((f"GM {OUTPUT_SLOT},{c:x}", None))
            out.append((f"GM {INPUT_SLOT},{c:x}", None))
        for c in range(1, CHANNELS + 1):
            out.append((ga_line(MATRIX_LABEL, f"3>{c}"), None))
            out.append((ga_line(MATRIX_LABEL, f"4>{c}"), None))
        for i in range(1, CHANNELS + 1):
            for o in range(1, CHANNELS + 1):
                idx = crosspoint_index(i, o)
                out.append((ga_line(MATRIX_LABEL, f"1>{idx}"), None))
                out.append((ga_line(MATRIX_LABEL, f"2>{idx}"), None))
        for c, label in sorted(self._input_labels.items()):
            out.append((ga_line(label, "3"), f"input:{c}"))
        for c, label in sorted(self._output_labels.items()):
            out.append((ga_line(label, "3"), f"output:{c}"))
        for number in sorted(self._groups):
            out.append((f"GG {number:x}", None))
            out.append((f"GN {number:x}", None))
        return out

    def _setting_gets(self) -> list[str]:
        """Values read at connect and every Resync interval, not subscribed:
        they change only when someone tunes the system."""
        out: list[str] = []
        for c in range(1, CHANNELS + 1):
            for index2, *_ in LIMITER_PARAMS.values():
                out.append(ga_line(limiter_label(c), f"0>{index2}"))
            out.append(ga_line(delay_label(c), "1>1"))
            out.append(ga_line(delay_label(c), "1>3"))
        return out

    async def _resync(self) -> None:
        self._last_resync = asyncio.get_running_loop().time()
        if self._push_supported:
            for text, owner in self._live_gets():
                await self._sub(text, owner)
        else:
            await self._read_live()
        await self._read_list(self._setting_gets())
        try:
            await self._read_alarm_log()
        except (ValueError, TimeoutError) as exc:
            log.debug(f"[{self.device_id}] Alarm log: {exc}")
        self._publish_problems()

    async def _sub(self, get_text: str, owner: str | None) -> bool:
        def matched(frame: bytes) -> bool:
            m = _SUB_RE.match(self._text(frame))
            return bool(m) and same_get(m.group(2), get_text)
        try:
            reply = await self._query(sub_line(get_text), matched, self.SUB_TIMEOUT_S)
        except ValueError as exc:
            self._label_refused(owner, exc)
            log.warning(f"[{self.device_id}] Subscribe refused for {get_text}: {exc}")
            return False
        if reply is None:
            log.debug(f"[{self.device_id}] No answer to SUB {get_text!r}")
            return False
        ok = self._text(reply).lower().endswith("yes")
        if ok:
            self._label_answered(owner)
        else:
            self._label_refused(owner, ValueError("subscription not accepted"))
            log.warning(f"[{self.device_id}] Subscription not accepted for {get_text}")
        return ok

    async def _read_live(self) -> None:
        for text, owner in self._live_gets():
            try:
                reply = await self._query(text, lambda f, t=text: self._reply_to(f, t))
            except ValueError as exc:
                self._label_refused(owner, exc)
                log.warning(f"[{self.device_id}] {text}: {exc}")
                continue
            if reply is not None:
                self._label_answered(owner)

    async def _read_list(self, texts: list[str]) -> None:
        for text in texts:
            try:
                await self._query(text, lambda f, t=text: self._reply_to(f, t))
            except ValueError as exc:
                log.warning(f"[{self.device_id}] {text}: {exc}")

    def _label_refused(self, owner: str | None, exc: Exception) -> None:
        if not owner:
            return
        kind, c = owner.split(":")
        labels = self._input_labels if kind == "input" else self._output_labels
        label = labels.get(int(c), "")
        self._label_problems[owner] = (f"{kind.title()} {c} label '{label}' was not accepted ({exc}); "
                                       f"check it against ControlSpace Designer")

    def _label_answered(self, owner: str | None) -> None:
        if owner:
            self._label_problems.pop(owner, None)

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

    async def _read_back_power_later(self) -> None:
        try:
            await asyncio.sleep(self.STANDBY_SETTLE_S)
            if self._link_alive():
                await self._read_power()
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug(f"[{self.device_id}] Standby read-back failed", exc_info=True)

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
        """Get Signal Level for the inputs and the outputs; how many answered."""
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
        log block (subscriptions go out on the same connection)."""
        return bool(parse_ga_reply(text) or _S_RE.match(text) or _GG_RE.match(text)
                    or _GN_RE.match(text) or _GV_RE.match(text) or _GM_RE.match(text)
                    or _GL_RE.match(text) or _GY_RE.match(text) or _SUB_RE.match(text)
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
        m = _GV_RE.match(text)
        if m:
            slot, ch, raw = m.group(1).lower(), int(m.group(2), 16), int(m.group(3), 16)
            if slot == OUTPUT_SLOT and 1 <= ch <= CHANNELS:
                self.set_child_state_batch(OUTPUT_CHILD_TYPE, ch, {"level_db": hex_level_to_db(raw)})
            return
        m = _GM_RE.match(text)
        if m:
            slot, ch, muted = m.group(1).lower(), int(m.group(2), 16), m.group(3).upper() == "M"
            if 1 <= ch <= CHANNELS:
                if slot == OUTPUT_SLOT:
                    self.set_child_state_batch(OUTPUT_CHILD_TYPE, ch, {"mute": muted})
                elif slot == INPUT_SLOT:
                    self.set_child_state_batch(INPUT_CHILD_TYPE, ch, {"mute": muted})
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

    def _apply_module_reply(self, label: str, idx: tuple[str, ...], raw: str) -> None:
        route = self._route.get((label.lower(), idx))
        if route is None:
            log.debug(f"[{self.device_id}] GA for a value the driver does not follow: {label!r} {'>'.join(idx)}")
            return
        ctype, channel, prop, kind = route
        value: Any
        try:
            if kind == "onoff":
                value = parse_onoff(raw)
            elif kind == "source":
                on = parse_onoff(raw)
                value = None if on is None else ("dante" if on else "analog")
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
        for c, raw in enumerate(raws[:CHANNELS], 1):
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

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if command == "power_on":
            await self._fire("SY N")
            await self._read_power()
            self._schedule_power_read()
            return None
        if command == "standby":
            await self._fire("SY S")
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
        if command in ("set_input_mute", "toggle_input_mute"):
            c = self._channel(params, "input")
            state = "T" if command == "toggle_input_mute" else ("M" if coerce_onoff(params.get("mute")) else "U")
            await self._fire(f"SM {INPUT_SLOT},{c:x},{state}")
            await self._slot_read(f"GM {INPUT_SLOT},{c:x}")
            return None
        if command == "set_output_polarity":
            c = self._channel(params, "output")
            label = self._output_labels.get(c)
            if not label:
                raise CommandParamError(f"Type Output {c}'s Amp Output label in the device settings "
                                        f"to set its polarity")
            await self._module_set(label, "3", "O" if coerce_onoff(params.get("inverted")) else "F")
            return None
        if command == "set_input_source":
            c = self._channel(params, "input")
            label = self._input_labels.get(c)
            if not label:
                raise CommandParamError(f"Type Input {c}'s label in the device settings to set its source")
            source = str(params.get("source", "")).strip().lower()
            if source not in ("analog", "dante"):
                raise CommandParamError("source is analog or dante")
            # 6.2.1 Source: O = On (the digital, Dante input), F = Analog.
            await self._module_set(label, "3", "O" if source == "dante" else "F")
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
                value = "T" if command == "toggle_crosspoint" else ("O" if coerce_onoff(params.get("on")) else "F")
                await self._module_set(MATRIX_LABEL, f"1>{idx}", value)
            return None
        if command == "set_matrix_input_mute":
            c = self._channel(params, "input")
            await self._module_set(MATRIX_LABEL, f"3>{c}", "O" if coerce_onoff(params.get("mute")) else "F")
            return None
        if command == "set_matrix_output_mute":
            c = self._channel(params, "output")
            await self._module_set(MATRIX_LABEL, f"4>{c}", "O" if coerce_onoff(params.get("mute")) else "F")
            return None
        if command == "set_output_limiter":
            c = self._channel(params, "output")
            name = str(params.get("parameter", "")).strip()
            spec = LIMITER_PARAMS.get(name)
            if spec is None:
                raise CommandParamError("parameter is peak_threshold, rms_threshold, rms_attack or rms_release")
            index2, _, lo, hi, step = spec
            value = float(params["value"])
            if not lo <= value <= hi:
                raise CommandParamError(f"{name} is {format_number(lo)} to {format_number(hi)}")
            value = round(round(value / step) * step, 3)
            await self._module_set(limiter_label(c), f"0>{index2}", format_number(value))
            return None
        if command == "set_output_delay":
            c = self._channel(params, "output")
            samples = ms_to_samples(float(params["delay_ms"]))
            if not 0 <= samples <= DELAY_MAX_SAMPLES:
                raise CommandParamError("delay is 0 to 50 ms")
            await self._module_set(delay_label(c), "1>1", str(samples))
            return None
        if command == "set_output_delay_bypass":
            c = self._channel(params, "output")
            await self._module_set(delay_label(c), "1>3", "O" if coerce_onoff(params.get("bypass")) else "F")
            return None
        if command in ("set_input_eq", "set_speaker_eq"):
            if command == "set_input_eq":
                c, table, label = self._channel(params, "input"), INPUT_PEQ_PARAMS, peq_label
            else:
                c, table, label = self._channel(params, "output"), SPEAKER_PEQ_PARAMS, speaker_peq_label
            band = int(params["band"])
            if not 1 <= band <= 9:
                raise CommandParamError("band is 1 to 9")
            name = str(params.get("parameter", "")).strip().lower()
            spec = table.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(table))
            wire = encode_table_value(name, spec, params.get("value"))
            return await self._module_set(label(c), f"{band}>{spec[0]}", wire)
        if command == "set_band_pass":
            c = self._channel(params, "output")
            name = str(params.get("parameter", "")).strip().lower()
            spec = BAND_PASS_PARAMS.get(name)
            if spec is None:
                raise CommandParamError("parameter is one of " + ", ".join(BAND_PASS_PARAMS))
            wire = encode_table_value(name, spec, params.get("value"))
            return await self._module_set(band_pass_label(c), band_pass_path(name), wire)
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
        if command == "get_alarm_log":
            return {"entries": await self._read_alarm_log()}
        if command == "clear_alarm_log":
            await self._module_write("CH")
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
            await self._read_power()
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
        """Open a session of its own (the amplifier takes eight), ask for its
        standby state, subscription support, the matrix, each typed label and
        each group, and report which answered."""
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", DEFAULT_PORT))
        if not host:
            raise ValueError("No IP address configured yet.")
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
        answered: list[str] = []
        silent: list[str] = []
        rejected: list[str] = []
        try:
            gy = await session.ask_or_none("GY", lambda t: bool(_GY_RE.match(t)))
            sub = await session.ask_or_none("SUB", lambda t: bool(_SUB_SUPPORT_RE.match(t)))
            push = bool(sub) and sub.strip().lower().endswith("yes")
            ip = await session.ask_or_none("IP", lambda t: bool(_IP_RE.match(t)))
            checks: list[tuple[str, str, Callable[[str], bool]]] = []
            matrix = ga_line(MATRIX_LABEL, "3>1")
            checks.append(("Matrix 1", matrix, lambda t, x=matrix: self._reply_to(t.encode(), x)))
            for c, label in sorted(self._input_labels.items()):
                text = ga_line(label, "3")
                checks.append((f"Input {c} label '{label}'", text,
                               lambda t, x=text: self._reply_to(t.encode(), x)))
            for c, label in sorted(self._output_labels.items()):
                text = ga_line(label, "3")
                checks.append((f"Output {c} label '{label}'", text,
                               lambda t, x=text: self._reply_to(t.encode(), x)))
            for number, name in sorted(self._groups.items()):
                text = f"GG {number:x}"
                checks.append((f"{name} (group {number})", text,
                               lambda t, x=text: self._reply_to(t.encode(), x)))
            await progress(f"Asking {len(checks)} value(s)", 40)
            for what, text, matched in checks:
                try:
                    reply = await session.ask(text, matched)
                except ValueError as exc:
                    rejected.append(f"{what} ({exc})")
                    continue
                (answered if reply else silent).append(what)
        finally:
            await session.close()
        if gy is None:
            message = (f"Connected to {host}:{port}, but it did not answer the standby query: "
                       f"check that this is a PowerShare amplifier.")
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
                frame, self.buf = parse_powershare_stream(self.buf)
                if frame is None:
                    break
                if not frame:
                    if not self.buf:
                        break
                    continue
                if frame[0] == NAK:
                    raise ValueError(BosePowerShareDriver._nak_message(frame))
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
