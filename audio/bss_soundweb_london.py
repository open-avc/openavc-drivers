"""
BSS Soundweb London (BLU series) — Direct Inject protocol driver.

Controls any Soundweb London processor (BLU-100 / 101 / 102 / 103 / 120 /
160 / 320 / 800 / 805 / 806 and the earlier BLU-16 / 32 / 80) through the
London Direct Inject (DI) message protocol on TCP port 1023. The unit's 9-pin
RS-232 port carries the same bytes.

Why Python (rules.md Principle 9):
    DI is a binary protocol: STX / ETX framing, five reserved bytes escaped
    as 0x1B + (byte + 128), an XOR checksum computed before escaping, and
    every value a big-endian signed 32-bit word whose meaning depends on the
    state variable (fader law, x10000 fixed point, log10 x 1e6, x96
    samples...). And the control surface is the integrator's design, not the
    driver's: every processing object placed in Audio Architect / London
    Architect gets its own HiQnet address, so the driver takes a declared
    object table and builds one child entity per object from it — the
    ``biamp_tesira_ttp`` / ``qsc_qrc`` shape.

Push, not polling (Principle 2):
    A DI_SUBSCRIBESV makes the unit send a DI_SETSV whenever that state
    variable changes, and immediately once with the current value, so a
    subscribe doubles as a GET (Interface Kit p.23). On connect the driver
    subscribes to every control of every declared object. Meters stream
    continuously, so they are opt-in and subscribed with a rate (ms, 50 ms
    granularity). Subscriptions live until unsubscribe or a unit reboot; the
    poll cycle (default 60 s) re-subscribes everything, which is both the
    re-arm after a reboot and a full resync.

    Measured on a BLU-100 (firmware 86.4.2): subscriptions are kept per
    session; a change is passed to every other subscribed session in the
    form it was made (a value, a percent of travel, or for a bump only the
    amount) but never back to the session that made it. So after each of
    its own writes the driver subscribes again to read the value back, and a
    percent notice from another controller is answered with a subscribe
    rather than stored.

Node address:
    A frame to node 0 reaches the unit at the other end of the connection
    (Kit p.8, and so over TCP too), and its reply carries the unit's real
    node (p.12; a BLU-100's is the last two bytes of its MAC). Rows declared
    without a node are matched to that reply, and the node is published as
    ``reported_node_address``. A frame for a node not on the network gets
    no answer.

Liveness:
    A subscribed session can sit silent for hours. The watchdog re-subscribes
    the control that answered last on this connection, then one control of
    every other answering object, and awaits a DI_SETSV echo; two rounds
    with no echo at all force a reconnect with a typed ``no_response``
    fault. A unit says nothing at all about an address it does not have, so
    while no control has answered the watchdog only re-sends the subscribe
    and ``last_error`` names what to check: a wrong row or node is a setup
    problem, not a dead unit.

Acknowledgements:
    None over Ethernet (Kit p.9: TCP provides it). A BLU-100 sends no ACK or
    NAK on port 1023, not for a frame it acts on and not for a bad checksum,
    which it drops; so the driver neither expects nor sends them. The
    serial-port ACK/NAK exchange (and FAQ Q1's once-a-second re-sends) is an
    RS-232 matter this TCP driver does not meet.

Not modelled (the document does not give the addresses): the device-level
state variables in Appendix G (Locate, display contrast, conductor priority)
are listed without the virtual device / object they live under, so they are
reachable only through ``set_raw_sv`` with an address read from Architect.

Source (BSS Audio, manufacturer documents):
    Soundweb London Interface Kit — 3rd Party Control, Revision 2.7, April
    2013. https://bssaudio.com/en-US/site_elements/soundweb-london-di-kit
    Its Audio Architect companion, "Soundweb London Third Party Control"
    (2016), gives the gain range (-80 to +10 dB), the meter scale and the
    Ethernet no-ACK rule, all three also measured on a BLU-100 (86.4.2).
    https://bssaudio.com/en-US/site_elements/soundweb-london-third-party-control-application-guide
"""


import asyncio
import math
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import CallableFrameParser
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ── Wire constants (Interface Kit pp. 6-8) ──────────────────────────────────

STX = 0x02
ETX = 0x03
ACK = 0x06
NAK = 0x15
ESC = 0x1B
SPECIAL_BYTES = frozenset((STX, ETX, ACK, NAK, ESC))

DI_SETSV = 0x88
DI_SUBSCRIBESV = 0x89
DI_UNSUBSCRIBESV = 0x8A
DI_VENUE_PRESET_RECALL = 0x8B
DI_PARAM_PRESET_RECALL = 0x8C
DI_SETSVPERCENT = 0x8D
DI_SUBSCRIBESVPERCENT = 0x8E
DI_UNSUBSCRIBESVPERCENT = 0x8F
DI_BUMPSVPERCENT = 0x90
DI_SETSTRINGSV = 0x91

PRESET_COMMANDS = frozenset((DI_VENUE_PRESET_RECALL, DI_PARAM_PRESET_RECALL))
ADDRESSED_COMMANDS = frozenset((
    DI_SETSV, DI_SUBSCRIBESV, DI_UNSUBSCRIBESV, DI_SETSVPERCENT,
    DI_SUBSCRIBESVPERCENT, DI_UNSUBSCRIBESVPERCENT, DI_BUMPSVPERCENT,
    DI_SETSTRINGSV,
))

VD_AUDIO = 0x03      # every audio processing object (p.8)
VD_LOGIC = 0x02      # logic objects (Harman help centre, Soundweb London Third Party Control)

PERCENT_SCALE = 65536          # DI_SETSVPERCENT / DI_BUMPSVPERCENT (Appendix A)
MAX_STRING_SV_LEN = 32         # Appendix F
DEFAULT_PORT = 1023
DEFAULT_POLL_INTERVAL = 60
DEFAULT_METER_RATE_MS = 100
MIN_METER_RATE_MS = 50
MAX_NODE = 0xFFFE
MAX_OBJECT = 0xFFFFFF
MAX_SV = 0xFFFF
INT32_MIN = -(2 ** 31)
INT32_MAX = 2 ** 31 - 1

# Fader-law limits. Appendix A draws the law from +10 dB down to -100 dB
# (linear above -10, logarithmic below), but every gain on a BLU-100 (86.4.2)
# stops at -80 dB: 0 % of travel and a written -100 dB both read back as
# -280617. Percent of travel is linear in the raw word between the two ends.
GAIN_DB_MIN = -80.0
GAIN_DB_MAX = 10.0
GAIN_LINEAR_FLOOR_DB = -10.0
GAIN_LINEAR_FLOOR_RAW = -100000


# ── Frame codec ─────────────────────────────────────────────────────────────

def escape_body(body: bytes) -> bytes:
    """Escape the reserved bytes of an already-checksummed body (p.6)."""
    out = bytearray()
    for b in body:
        if b in SPECIAL_BYTES:
            out.append(ESC)
            out.append(b + 128)
        else:
            out.append(b)
    return bytes(out)


def xor_checksum(body: bytes) -> int:
    c = 0
    for b in body:
        c ^= b
    return c


def build_frame(body: bytes) -> bytes:
    """<STX> <escaped body + checksum> <ETX> — the checksum is XOR of the
    unescaped body and is itself escaped when reserved (p.7)."""
    return bytes([STX]) + escape_body(body + bytes([xor_checksum(body)])) + bytes([ETX])


def unescape(data: bytes) -> bytes:
    out = bytearray()
    pending = False
    for b in data:
        if pending:
            out.append((b - 128) & 0xFF)
            pending = False
        elif b == ESC:
            pending = True
        else:
            out.append(b)
    return bytes(out)


def decode_frame(frame: bytes) -> bytes | None:
    """Return the unescaped body of a complete STX..ETX frame, or None when
    the checksum fails or the frame is malformed."""
    if len(frame) < 4 or frame[0] != STX or frame[-1] != ETX:
        return None
    inner = unescape(frame[1:-1])
    if len(inner) < 2:
        return None
    body, checksum = inner[:-1], inner[-1]
    if xor_checksum(body) != checksum:
        return None
    return body


def parse_di_stream(buf: bytes) -> tuple[bytes | None, bytes]:
    """CallableFrameParser function: one STX..ETX frame per message, a bare
    ACK / NAK byte delivered as a one-byte message, anything else dropped.

    An escaped byte is 0x80-0xFF and never STX / ETX, so scanning for ETX
    only has to skip the byte after each ESC."""
    if not buf:
        return None, buf
    first = buf[0]
    if first in (ACK, NAK):
        return buf[:1], buf[1:]
    if first != STX:
        # Drop garbage up to the next byte that can start a message.
        for i in range(1, len(buf)):
            if buf[i] in (STX, ACK, NAK):
                return b"", buf[i:]
        return b"", b""
    i = 1
    while i < len(buf):
        b = buf[i]
        if b == ESC:
            i += 2
            continue
        if b == ETX:
            return buf[: i + 1], buf[i + 1:]
        if b == STX:
            # A new frame started before this one ended: the earlier bytes
            # are a truncated frame; resync on the new STX.
            return b"", buf[i:]
        i += 1
    return None, buf


def encode_address(node: int, vd: int, obj: int, sv: int) -> bytes:
    return struct.pack(">HB", node, vd) + obj.to_bytes(3, "big") + struct.pack(">H", sv)


def build_addressed(cmd: int, node: int, vd: int, obj: int, sv: int, data: int) -> bytes:
    data = max(INT32_MIN, min(INT32_MAX, int(data)))
    return build_frame(bytes([cmd]) + encode_address(node, vd, obj, sv) + struct.pack(">i", data))


def build_set(node: int, vd: int, obj: int, sv: int, raw: int) -> bytes:
    return build_addressed(DI_SETSV, node, vd, obj, sv, raw)


def build_subscribe(node: int, vd: int, obj: int, sv: int, rate_ms: int = 0) -> bytes:
    return build_addressed(DI_SUBSCRIBESV, node, vd, obj, sv, rate_ms)


def build_unsubscribe(node: int, vd: int, obj: int, sv: int) -> bytes:
    return build_addressed(DI_UNSUBSCRIBESV, node, vd, obj, sv, 0)


def build_set_percent(node: int, vd: int, obj: int, sv: int, percent: float) -> bytes:
    return build_addressed(DI_SETSVPERCENT, node, vd, obj, sv, round(percent * PERCENT_SCALE))


def build_bump_percent(node: int, vd: int, obj: int, sv: int, delta: float) -> bytes:
    return build_addressed(DI_BUMPSVPERCENT, node, vd, obj, sv, round(delta * PERCENT_SCALE))


def build_preset_recall(cmd: int, index: int) -> bytes:
    return build_frame(bytes([cmd]) + struct.pack(">i", int(index)))


def build_set_string(node: int, vd: int, obj: int, sv: int, text: str) -> bytes:
    """Appendix F: <len (2 bytes, includes the terminator)> <ascii> <0>."""
    raw = text.encode("ascii", errors="replace")[:MAX_STRING_SV_LEN]
    return build_frame(
        bytes([DI_SETSTRINGSV]) + encode_address(node, vd, obj, sv)
        + struct.pack(">H", len(raw) + 1) + raw + b"\x00"
    )


@dataclass
class DIMessage:
    cmd: int
    node: int = 0
    vd: int = 0
    obj: int = 0
    sv: int = 0
    raw: int = 0
    text: str | None = None

    @property
    def key(self) -> tuple[int, int, int, int]:
        return (self.node, self.vd, self.obj, self.sv)


def parse_body(body: bytes) -> DIMessage | None:
    """Split an unescaped, checksum-verified body into its fields."""
    if not body:
        return None
    cmd = body[0]
    if cmd in PRESET_COMMANDS:
        if len(body) < 5:
            return None
        return DIMessage(cmd=cmd, raw=struct.unpack(">i", body[1:5])[0])
    if cmd not in ADDRESSED_COMMANDS or len(body) < 9:
        return None
    node, vd = struct.unpack(">HB", body[1:4])
    obj = int.from_bytes(body[4:7], "big")
    sv = struct.unpack(">H", body[7:9])[0]
    if cmd == DI_SETSTRINGSV:
        if len(body) < 11:
            return None
        length = struct.unpack(">H", body[9:11])[0]
        payload = body[11:11 + length]
        text = payload.split(b"\x00", 1)[0].decode("ascii", errors="replace")
        return DIMessage(cmd=cmd, node=node, vd=vd, obj=obj, sv=sv, text=text)
    if len(body) < 13:
        return None
    raw = struct.unpack(">i", body[9:13])[0]
    return DIMessage(cmd=cmd, node=node, vd=vd, obj=obj, sv=sv, raw=raw)


# ── Value scaling (Appendix A) ──────────────────────────────────────────────

FMT_GAIN = "gain"        # fader law, dB
FMT_METER = "meter"      # read-only level, dB x 10000, floored at -80 dB (measured on a BLU-100)
FMT_INPUT_GAIN = "input_gain"  # input card gain: one count per 6 dB step (measured on a BLU-100)
FMT_BOOL = "bool"        # discrete 0 / 1
FMT_INT = "int"          # discrete, sent as is
FMT_SCALAR = "scalar"    # value x 10000
FMT_PERCENT = "percent"  # value x 100 (a control whose native unit is %)
FMT_DELAY = "delay"      # ms x 96 (96 kHz samples)
FMT_FREQ = "freq"        # log10(Hz) x 1e6
FMT_SPEED = "speed"      # log10(ms) x 1e6
FMT_STRING = "string"    # Appendix F string SV

VALUE_FORMATS = (
    FMT_GAIN, FMT_METER, FMT_INPUT_GAIN, FMT_BOOL, FMT_INT, FMT_SCALAR,
    FMT_PERCENT, FMT_DELAY, FMT_FREQ, FMT_SPEED, FMT_STRING,
)
FORMAT_ALIASES = {
    "gain_db": FMT_GAIN, "db": FMT_GAIN, "fader": FMT_GAIN,
    "boolean": FMT_BOOL, "mute": FMT_BOOL, "switch": FMT_BOOL,
    "discrete": FMT_INT, "integer": FMT_INT, "enum": FMT_INT,
    "linear": FMT_SCALAR, "float": FMT_SCALAR, "number": FMT_SCALAR,
    "pct": FMT_PERCENT, "%": FMT_PERCENT,
    "delay_ms": FMT_DELAY, "ms": FMT_DELAY,
    "hz": FMT_FREQ, "frequency": FMT_FREQ,
    "speed_ms": FMT_SPEED, "attack": FMT_SPEED, "release": FMT_SPEED,
    "str": FMT_STRING, "text": FMT_STRING,
}

# Meters: dB x 10000 from -80 to +40 dB, linear (BSS "Soundweb London Third
# Party Control", 2016). A BLU-100 on 86.4.2 floors every meter at exactly
# -800000 and shifts the reading by its Reference SV one for one.
METER_DB_MIN = -80.0
METER_DB_MAX = 40.0

# Input card gain: the unit sends 0, 1, 2 for 0, +6, +12 dB; a BLU analogue
# input reaches +48 dB.
INPUT_GAIN_STEP_DB = 6
INPUT_GAIN_MAX_DB = 48


def gain_db_to_raw(db: float) -> int:
    db = max(GAIN_DB_MIN, min(GAIN_DB_MAX, float(db)))
    if db >= GAIN_LINEAR_FLOOR_DB:
        return round(db * 10000)
    return round(-math.log10(abs(db / 10.0)) * 200000 - 100000)


def raw_to_gain_db(raw: int) -> float:
    if raw >= GAIN_LINEAR_FLOOR_RAW:
        return raw / 10000
    return -10 * (10 ** (abs(raw + 100000) / 200000))


def input_gain_db_to_raw(db: float) -> int:
    db = float(db)
    steps = db / INPUT_GAIN_STEP_DB
    if steps != int(steps) or not 0 <= db <= INPUT_GAIN_MAX_DB:
        raise ValueError(
            f"input gain moves in {INPUT_GAIN_STEP_DB} dB steps from 0 to "
            f"{INPUT_GAIN_MAX_DB} dB; {db:g} dB is not one of them"
        )
    return int(steps)


def value_to_raw(fmt: str, value: Any) -> int:
    if fmt == FMT_GAIN:
        return gain_db_to_raw(float(value))
    if fmt == FMT_METER:
        return round(float(value) * 10000)
    if fmt == FMT_INPUT_GAIN:
        return input_gain_db_to_raw(value)
    if fmt == FMT_BOOL:
        return 1 if value else 0
    if fmt == FMT_INT:
        return int(value)
    if fmt == FMT_SCALAR:
        return round(float(value) * 10000)
    if fmt == FMT_PERCENT:
        return round(float(value) * 100)
    if fmt == FMT_DELAY:
        return round(float(value) * 96)
    if fmt in (FMT_FREQ, FMT_SPEED):
        v = float(value)
        if v <= 0:
            raise ValueError("a frequency or time must be greater than zero")
        return round(math.log10(v) * 1000000)
    raise ValueError(f"cannot encode a {fmt} value as a number")


def raw_to_value(fmt: str, raw: int) -> Any:
    if fmt == FMT_GAIN:
        return round(raw_to_gain_db(raw), 2)
    if fmt == FMT_METER:
        return round(raw / 10000, 2)
    if fmt == FMT_INPUT_GAIN:
        return int(raw) * INPUT_GAIN_STEP_DB
    if fmt == FMT_BOOL:
        return raw != 0
    if fmt == FMT_INT:
        return int(raw)
    if fmt == FMT_SCALAR:
        return raw / 10000
    if fmt == FMT_PERCENT:
        return raw / 100
    if fmt == FMT_DELAY:
        return round(raw / 96, 3)
    if fmt in (FMT_FREQ, FMT_SPEED):
        return round(10 ** (raw / 1000000), 3)
    return raw


def coerce_user_value(fmt: str, value: Any) -> Any:
    """A Set Control value arrives as typed text; make it the control's type."""
    if fmt == FMT_STRING:
        return "" if value is None else str(value)
    if fmt == FMT_BOOL:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "on", "yes", "y", "mute", "muted"):
            return True
        if s in ("0", "false", "off", "no", "n", "unmute", "unmuted"):
            return False
        raise ValueError(f"{value!r} is not an on/off value")
    if fmt == FMT_INT:
        return int(float(str(value).strip()))
    return float(str(value).strip())


# ── Address parsing ─────────────────────────────────────────────────────────

_HEX_RE = re.compile(r"^0[xX]([0-9A-Fa-f]{1,12})$")
_DEC_RE = re.compile(r"^\d{1,8}$")
# Audio Architect's Properties window shows an Object ID as its three bytes:
# 0.1.0 is object 0x100 (a BLU-100's first five objects read 0.1.0 to 0.1.4).
_DOTTED_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def parse_node_address(text: Any) -> int:
    """The unit's HiQnet Node Address as Architect shows it (hex 0x08AD or
    decimal). Blank means 0 — the unit at the other end of the cable (p.8)."""
    s = str(text or "").strip()
    if not s:
        return 0
    m = _HEX_RE.match(s)
    if m:
        node = int(m.group(1), 16)
    elif _DEC_RE.match(s):
        node = int(s)
    else:
        raise ValueError(f"node address {s!r} is not hex (0x08AD) or decimal")
    if node > MAX_NODE:
        raise ValueError(f"node address {s!r} is above 0xFFFE")
    return node


def parse_object_address(text: Any, default_node: int, default_vd: int = VD_AUDIO
                         ) -> tuple[int, int, int]:
    """An object address as the Properties window shows it: the full HiQnet
    address ``0x083203000100`` (node, virtual device, object), or the object
    part alone (``0x100`` / ``256`` / Audio Architect's ``0.1.0``), which
    takes the device's node and the audio virtual device."""
    s = str(text or "").strip().replace(" ", "")
    if not s:
        raise ValueError("object address is blank")
    m = _DOTTED_RE.match(s)
    if m:
        parts = [int(g) for g in m.groups()]
        if any(p > 255 for p in parts):
            raise ValueError(f"object id {s!r} has a part above 255")
        return default_node, default_vd, (parts[0] << 16) | (parts[1] << 8) | parts[2]
    m = _HEX_RE.match(s)
    if m:
        digits = m.group(1)
        if len(digits) > 6:
            digits = digits.rjust(12, "0")
            return int(digits[0:4], 16), int(digits[4:6], 16), int(digits[6:12], 16)
        return default_node, default_vd, int(digits, 16)
    if _DEC_RE.match(s):
        obj = int(s)
        if obj > MAX_OBJECT:
            raise ValueError(f"object address {s!r} is above 0xFFFFFF")
        return default_node, default_vd, obj
    raise ValueError(
        f"object address {s!r} is not a HiQnet address (0x083203000100) "
        f"or an object id (0.1.0 or 0x100)"
    )


def format_hiqnet(node: int, vd: int, obj: int) -> str:
    return f"0x{node:04X}{vd:02X}{obj:06X}"


# ── Object types (Appendix G state-variable ids, decimal) ───────────────────

@dataclass
class ControlDef:
    prop: str
    label: str
    sv: int
    fmt: str
    writable: bool = True
    hints: dict[str, Any] = field(default_factory=dict)

    def schema(self) -> dict[str, Any]:
        d: dict[str, Any] = {"label": self.label}
        if self.fmt == FMT_GAIN:
            d.update({"type": "number", "unit": "dB", "min": GAIN_DB_MIN,
                      "max": GAIN_DB_MAX, "step": 0.5})
        elif self.fmt == FMT_METER:
            d.update({"type": "number", "unit": "dB", "min": METER_DB_MIN,
                      "max": METER_DB_MAX, "cloud_priority": "low"})
        elif self.fmt == FMT_INPUT_GAIN:
            d.update({"type": "number", "unit": "dB", "min": 0,
                      "max": INPUT_GAIN_MAX_DB, "step": INPUT_GAIN_STEP_DB})
        elif self.fmt == FMT_BOOL:
            d["type"] = "boolean"
        elif self.fmt == FMT_INT:
            d["type"] = "integer"
        elif self.fmt == FMT_PERCENT:
            d.update({"type": "number", "unit": "%", "min": 0, "max": 100})
        elif self.fmt == FMT_DELAY:
            d.update({"type": "number", "unit": "ms", "min": 0})
        elif self.fmt == FMT_FREQ:
            d.update({"type": "number", "unit": "Hz"})
        elif self.fmt == FMT_SPEED:
            d.update({"type": "number", "unit": "ms"})
        elif self.fmt == FMT_STRING:
            d["type"] = "string"
        else:
            d["type"] = "number"
        d["control"] = bool(self.writable)
        d.update(self.hints)
        return d


def _gain(prop: str, label: str, sv: int) -> ControlDef:
    return ControlDef(prop, label, sv, FMT_GAIN)


def _bool(prop: str, label: str, sv: int) -> ControlDef:
    return ControlDef(prop, label, sv, FMT_BOOL)


def _int(prop: str, label: str, sv: int, **hints: Any) -> ControlDef:
    return ControlDef(prop, label, sv, FMT_INT, hints=hints)


def _meter(prop: str, label: str, sv: int) -> ControlDef:
    return ControlDef(prop, label, sv, FMT_METER, writable=False)


def _speed(prop: str, label: str, sv: int) -> ControlDef:
    return ControlDef(prop, label, sv, FMT_SPEED)


def _scalar(prop: str, label: str, sv: int) -> ControlDef:
    return ControlDef(prop, label, sv, FMT_SCALAR)


def _meter_block(prefix: str, label: str, base: int, order: tuple[int, int, int, int]
                 ) -> list[ControlDef]:
    """Meter + attack + release + reference at the given SV offsets."""
    m, a, r, ref = order
    return [
        _meter(f"{prefix}meter", f"{label}Meter", base + m),
        _speed(f"{prefix}attack", f"{label}Attack", base + a),
        _speed(f"{prefix}release", f"{label}Release", base + r),
        _scalar(f"{prefix}reference", f"{label}Reference", base + ref),
    ]


def build_gain_object(size: str) -> list[ControlDef]:
    return [_gain("gain", "Gain", 0), _bool("mute", "Mute", 1), _bool("polarity", "Polarity", 2)]


def build_n_input_gain(size: str) -> list[ControlDef]:
    n = _parse_count(size, default=4, maximum=32)
    out: list[ControlDef] = []
    for i in range(1, n + 1):
        out += [
            _gain(f"input_{i}_gain", f"Input {i} Gain", i - 1),
            _bool(f"input_{i}_mute", f"Input {i} Mute", 32 + i - 1),
            _bool(f"input_{i}_polarity", f"Input {i} Polarity", 64 + i - 1),
        ]
    out += [_gain("master_gain", "Master Gain", 96), _bool("override_mute", "Override Mute", 97)]
    return out


@dataclass
class MixerShape:
    inputs: int
    stereo: bool = True
    aux: int = 4
    groups: int = 4


def _parse_mixer(size: Any, default: int, maximum: int) -> MixerShape:
    """``8``, or ``8 mono aux 2 groups 0``: the input count, then what the
    mixer placed in the design has. A mixer only answers for the parts it
    has (a mono one has no pan and no right output), so the row says which.
    Left out: stereo, four aux sends, four groups (the whole Appendix G map)."""
    tokens = str(size or "").strip().lower().replace(",", " ").split()
    shape = MixerShape(inputs=default)
    if tokens and tokens[0].isdigit():
        shape.inputs = int(tokens.pop(0))
        if not 1 <= shape.inputs <= maximum:
            raise ValueError(f"size {shape.inputs} is outside 1..{maximum}")
    while tokens:
        word = tokens.pop(0)
        if word in ("mono", "stereo"):
            shape.stereo = word == "stereo"
        elif word in ("aux", "groups", "group") and tokens and tokens[0].isdigit():
            count = int(tokens.pop(0))
            if not 0 <= count <= 4:
                raise ValueError(f"{word} {count} is outside 0..4")
            if word == "aux":
                shape.aux = count
            else:
                shape.groups = count
        else:
            raise ValueError(
                f"size {size!r} must be the input count, then optionally mono, "
                f"aux 0-4 and groups 0-4 (e.g. 8 mono aux 2 groups 0)"
            )
    return shape


def _mixer_inputs(shape: MixerShape, automix: bool) -> list[ControlDef]:
    out: list[ControlDef] = []
    for i in range(1, shape.inputs + 1):
        b = (i - 1) * 100
        out += [
            _gain(f"input_{i}_gain", f"Input {i} Gain", b),
            _bool(f"input_{i}_mute", f"Input {i} Mute", b + 1),
        ]
        if shape.stereo:
            out.append(_scalar(f"input_{i}_pan", f"Input {i} Pan", b + 2))
        out += [
            _bool(f"input_{i}_polarity", f"Input {i} Polarity", b + 3),
            _bool(f"input_{i}_solo", f"Input {i} Solo", b + 4),
        ]
        if automix:
            out += [
                _bool(f"input_{i}_override", f"Input {i} Override", b + 5),
                _gain(f"input_{i}_off_gain", f"Input {i} Off Gain", b + 6),
                _bool(f"input_{i}_auto", f"Input {i} Auto", b + 7),
                _bool(f"input_{i}_on", f"Input {i} On", b + 8),
            ]
        for a in range(1, shape.aux + 1):
            out.append(_gain(f"input_{i}_aux_{a}_send", f"Input {i} Aux {a} Send", b + 19 + a))
        for g in range(1, shape.groups + 1):
            out.append(_bool(f"input_{i}_group_{g}", f"Input {i} to Group {g}", b + 39 + g))
    return out


def _mixer_buses(shape: MixerShape, pre_post: bool) -> list[ControlDef]:
    out: list[ControlDef] = []
    for k, letter in enumerate("abcd"[: shape.aux]):
        b = 10000 + k * 10
        if pre_post:
            out.append(_bool(f"aux_{letter}_pre_post", f"Aux {letter.upper()} Pre/Post", b))
        out += [
            _gain(f"aux_{letter}_gain", f"Aux {letter.upper()} Gain", b + 1),
            _bool(f"aux_{letter}_mute", f"Aux {letter.upper()} Mute", b + 2),
        ]
    for k, letter in enumerate("abcd"[: shape.groups]):
        b = 11000 + k * 10
        out += [
            _gain(f"group_{letter}_gain", f"Group {letter.upper()} Gain", b),
            _bool(f"group_{letter}_mute", f"Group {letter.upper()} Mute", b + 1),
        ]
    if shape.stereo:
        out += [
            _gain("output_gain_left", "Output Gain Left", 20000),
            _bool("output_mute_left", "Output Mute Left", 20001),
            _gain("output_gain_right", "Output Gain Right", 20002),
            _bool("output_mute_right", "Output Mute Right", 20003),
        ]
    else:
        out += [
            _gain("output_gain", "Output Gain", 20000),
            _bool("output_mute", "Output Mute", 20001),
        ]
    return out


def build_mixer(size: str) -> list[ControlDef]:
    shape = _parse_mixer(size, default=8, maximum=48)
    return _mixer_inputs(shape, automix=False) + _mixer_buses(shape, pre_post=True)


def build_automixer(size: str) -> list[ControlDef]:
    shape = _parse_mixer(size, default=8, maximum=48)
    return (
        _mixer_inputs(shape, automix=True) + _mixer_buses(shape, pre_post=False)
        + [_speed("output_speed", "Output Speed", 20004),
           _scalar("output_slope", "Output Slope", 20005)]
    )


def build_matrix_mixer(size: str) -> list[ControlDef]:
    ins, outs = _parse_matrix(size, default=(8, 8), maximum=48)
    return [
        _gain(f"xp_{i}_{o}_gain", f"In {i} to Out {o} Gain", 16384 + (o - 1) * 128 + (i - 1))
        for o in range(1, outs + 1) for i in range(1, ins + 1)
    ]


def build_matrix_router(size: str) -> list[ControlDef]:
    ins, outs = _parse_matrix(size, default=(8, 8), maximum=48)
    return [
        _bool(f"xp_{i}_{o}", f"In {i} to Out {o}", (o - 1) * 128 + (i - 1))
        for o in range(1, outs + 1) for i in range(1, ins + 1)
    ]


def build_source_matrix(size: str) -> list[ControlDef]:
    outs = _parse_count(size, default=8, maximum=96)
    return [_int(f"output_{o}_source", f"Output {o} Source", o - 1, min=0)
            for o in range(1, outs + 1)]


def build_source_selector(size: str) -> list[ControlDef]:
    # 1 selects input 1; 0 selects none (a new selector's value). Measured
    # on a BLU-100: Architect's inputs 1, 2, 3 arrive as 1, 2, 3.
    return [_int("source", "Source", 0, min=0,
                 help="The selected input: 1 is input 1. 0 selects no input.")]


def build_meter(size: str) -> list[ControlDef]:
    return _meter_block("", "", 0, (0, 1, 2, 3))


def build_input_card(size: str) -> list[ControlDef]:
    """BLU Analogue Input Card, Appendix G: per channel meter / reference /
    attack / release / gain / phantom at 6 SVs a channel."""
    chans = _parse_channels(size, default=4, maximum=4)
    out: list[ControlDef] = []
    for c in chans:
        b = (c - 1) * 6
        out += _meter_block(f"channel_{c}_", f"Channel {c} ", b, (0, 2, 3, 1))
        out += [
            ControlDef(f"channel_{c}_gain", f"Channel {c} Input Gain", b + 4, FMT_INPUT_GAIN),
            _bool(f"channel_{c}_phantom", f"Channel {c} Phantom Power", b + 5),
        ]
    return out


def build_output_card(size: str) -> list[ControlDef]:
    """BLU Analogue Output Card, Appendix G: meter / reference / attack /
    release at 4 SVs a channel."""
    chans = _parse_channels(size, default=4, maximum=4)
    out: list[ControlDef] = []
    for c in chans:
        out += _meter_block(f"channel_{c}_", f"Channel {c} ", (c - 1) * 4, (0, 2, 3, 1))
    return out


def build_custom(size: str) -> list[ControlDef]:
    """One state variable: ``size`` is "<sv id> <format>" (format defaults
    to a raw integer). The whole SV universe is reachable this way."""
    parts = str(size or "").split()
    if not parts or not parts[0].lstrip("-").isdigit():
        raise ValueError("a custom row needs the state-variable id in its Size/SV field, e.g. '1 mute'")
    sv = int(parts[0])
    if not 0 <= sv <= MAX_SV:
        raise ValueError(f"state-variable id {sv} is outside 0..65535")
    fmt = FMT_INT
    if len(parts) > 1:
        token = parts[1].lower()
        fmt = FORMAT_ALIASES.get(token, token)
        if fmt not in VALUE_FORMATS:
            raise ValueError(
                f"unknown value format {parts[1]!r} (use one of {', '.join(VALUE_FORMATS)})"
            )
    return [ControlDef("value", "Value", sv, fmt, writable=fmt != FMT_METER)]


OBJECT_TYPES: dict[str, dict[str, Any]] = {
    "gain": {"label": "Gain", "build": build_gain_object},
    "n_input_gain": {"label": "N-Input Gain", "build": build_n_input_gain},
    "mixer": {"label": "Mixer", "build": build_mixer},
    "automixer": {"label": "Automixer", "build": build_automixer},
    "matrix_mixer": {"label": "Matrix Mixer (NxM)", "build": build_matrix_mixer},
    "matrix_router": {"label": "Matrix Router (NxM)", "build": build_matrix_router},
    "source_matrix": {"label": "Source Matrix", "build": build_source_matrix},
    "source_selector": {"label": "Source Selector", "build": build_source_selector},
    "meter": {"label": "Meter / RMS Meter", "build": build_meter},
    "input_card": {"label": "Analogue Input Card", "build": build_input_card},
    "output_card": {"label": "Analogue Output Card", "build": build_output_card},
    "custom": {"label": "Custom (one state variable)", "build": build_custom},
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


def _parse_channels(size: Any, default: int, maximum: int) -> list[int]:
    s = str(size or "").strip()
    if not s:
        return list(range(1, default + 1))
    if s.isdigit():
        # A bare number is a count (a 4-channel card), as for every other type.
        n = int(s)
        if not 1 <= n <= maximum:
            raise ValueError(f"channel count {n} is outside 1..{maximum}")
        return list(range(1, n + 1))
    out: list[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m:
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
            out.extend(range(lo, hi + 1))
        elif part.isdigit():
            out.append(int(part))
        else:
            raise ValueError(f"channels {size!r} must be a count (4), a range (1-4) or a list (1,3)")
    out = sorted(set(out))
    if not out or out[0] < 1 or out[-1] > maximum:
        raise ValueError(f"channels {size!r} are outside 1..{maximum}")
    return out


# ── The declared object table ───────────────────────────────────────────────

OBJECT_CHILD_TYPE = "object"

CONTROL_COLUMNS: dict[str, dict[str, Any]] = {
    "name": {
        "type": "string", "label": "Name", "required": True,
        "help": "What this object is (Program Level, Mic Mixer). Becomes the "
                "child entity's id, so keep it unique.",
    },
    "address": {
        "type": "string", "label": "HiQnet Address", "required": True,
        "help": "Select the object in Audio Architect and copy the Object "
                "ID from the Properties window (0.1.0), or the full HiQnet "
                "address London Architect shows (0x083203000100). Input and "
                "output cards are fixed: 0.0.1 = card A ... 0.0.4 = card D.",
    },
    "type": {
        "type": "enum", "label": "Object Type", "required": True,
        "values": [{"value": k, "label": v["label"]} for k, v in OBJECT_TYPES.items()],
        "help": "The processing object placed in the design. Decides which "
                "controls are exposed.",
    },
    "size": {
        "type": "string", "label": "Size / SV",
        "help": "Inputs for an N-Input Gain (8). For a mixer or automixer: its "
                "inputs, then 'mono' and its aux and group counts when it has "
                "fewer than four of each (8 mono aux 2 groups 0). Inputs x "
                "outputs for a matrix (8x4), outputs for a source matrix, "
                "channels for a card (1-4). Custom: the state-variable id and "
                "its format, e.g. '1 mute' or '0 gain'. Blank = the default size.",
    },
}

DEFAULT_CONTROLS: list[dict[str, str]] = [
    {"name": "Program", "address": "0.1.0", "type": "gain", "size": ""},
    {"name": "Mics", "address": "0.1.1", "type": "n_input_gain", "size": "4"},
    {"name": "Source", "address": "0.1.2", "type": "source_selector", "size": ""},
    {"name": "Input Card A", "address": "0.0.1", "type": "input_card", "size": "1-4"},
]


@dataclass
class DIObject:
    cid: str
    name: str
    node: int
    vd: int
    obj: int
    type_id: str
    size: str
    controls: dict[str, ControlDef]

    @property
    def address(self) -> str:
        return format_hiqnet(self.node, self.vd, self.obj)

    def key(self, ctl: ControlDef) -> tuple[int, int, int, int]:
        return (self.node, self.vd, self.obj, ctl.sv)


def safe_child_id(name: str) -> str:
    cid = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name).strip()).strip("_")
    return cid[:120] or "object"


def parse_controls_config(rows: Any, node: int) -> tuple[list[DIObject], list[str]]:
    """Expand the table rows into objects; a bad row is reported, not fatal,
    so one typo does not take the whole device down."""
    objects: list[DIObject] = []
    problems: list[str] = []
    seen: set[str] = set()
    if isinstance(rows, str):
        rows = _rows_from_text(rows)
    if not isinstance(rows, list):
        return objects, ["the object list is not a list of rows"]
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            problems.append(f"row {n}: not a table row")
            continue
        name = str(row.get("name") or "").strip()
        type_id = str(row.get("type") or "").strip().lower()
        size = str(row.get("size") or "").strip()
        try:
            if not name:
                raise ValueError("no name")
            if type_id not in OBJECT_TYPES:
                raise ValueError(f"unknown object type {type_id!r}")
            rnode, vd, obj = parse_object_address(row.get("address"), node)
            controls = OBJECT_TYPES[type_id]["build"](size)
        except ValueError as exc:
            problems.append(f"row {n} ({name or '?'}): {exc}")
            continue
        cid = safe_child_id(name)
        if cid in seen:
            problems.append(f"row {n} ({name}): duplicates another row's name")
            continue
        seen.add(cid)
        objects.append(DIObject(cid, name, rnode, vd, obj, type_id, size,
                                {c.prop: c for c in controls}))
    return objects, problems


def _rows_from_text(text: str) -> list[dict[str, str]]:
    """``name address type [size]`` per line — for a device config written
    by hand instead of the table editor."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or line.lstrip().startswith("#"):
            continue
        rows.append({"name": parts[0], "address": parts[1], "type": parts[2],
                     "size": " ".join(parts[3:])})
    return rows


_OBJECT_SUMMARY_SCHEMA: dict[str, dict[str, Any]] = {
    "name": {"type": "string", "label": "Name"},
    "object_type": {"type": "string", "label": "Object Type"},
    "address": {"type": "string", "label": "HiQnet Address"},
    "responding": {"type": "boolean", "label": "Responding"},
}

OBJECT_CHILD_TYPES: dict[str, dict[str, Any]] = {
    OBJECT_CHILD_TYPE: {
        "label": "Processing Object",
        "label_plural": "Processing Objects",
        "dynamic": True,
        "id_format": {"type": "string", "max_length": 128},
        "state_variables": dict(_OBJECT_SUMMARY_SCHEMA),
        "summary_fields": ["object_type", "address", "responding"],
        "label_field": "name",
    },
}


# ── Command surface ─────────────────────────────────────────────────────────

def _object_param() -> dict[str, Any]:
    return {"type": "child_id", "child_type": OBJECT_CHILD_TYPE, "required": True,
            "label": "Object",
            "help": "One of the processing objects declared on the device page."}


def _control_param(help: str) -> dict[str, Any]:
    p: dict[str, Any] = {"type": "string", "required": True, "label": "Control",
                         "options_from": {"param": "object", "source": "child_schema"}}
    if help:
        p["help"] = help
    return p


def _address_param() -> dict[str, Any]:
    return {"type": "string", "required": True, "label": "HiQnet Address",
            "pattern": r"^\s*(0[xX][0-9A-Fa-f]{1,12}|\d{1,8}|\d{1,3}\.\d{1,3}\.\d{1,3})\s*$",
            "help": "The Object ID as Audio Architect shows it (0.1.0), or a full HiQnet address (0x083203000100)."}


def _sv_param() -> dict[str, Any]:
    return {"type": "integer", "required": True, "label": "State Variable ID",
            "min": 0, "max": MAX_SV,
            "help": "The SV id in decimal, as Architect and the Interface Kit list it."}


COMMANDS: dict[str, dict[str, Any]] = {
    "recall_venue_preset": {
        "label": "Recall Venue Preset",
        "params": {"preset": {"type": "integer", "required": True, "label": "Preset ID",
                              "min": 0, "max": INT32_MAX,
                              "help": "The number in square brackets in London Architect's design tree."}},
        "help": "Broadcasts a Venue Preset recall to every unit on the network "
                "that is configured to respond to it. Venue presets come from "
                "London Architect designs; in an Audio Architect design use "
                "Recall Parameter Preset.",
    },
    "recall_parameter_preset": {
        "label": "Recall Parameter Preset",
        "params": {"preset": {"type": "integer", "required": True, "label": "Preset ID",
                              "min": 0, "max": INT32_MAX,
                              "help": "The number in square brackets after the preset's "
                                      "name in Architect: Preset 1 [0] is 0."}},
        "help": "Broadcasts a Parameter Preset recall. The recalled values "
                "arrive on every subscribed control.",
    },
    "set_control": {
        "label": "Set Control",
        "params": {
            "object": _object_param(),
            "control": _control_param(""),
            "value": {"type": "string", "required": True, "label": "Value",
                      "type_from": {"param": "control"},
                      "help": "dB for a gain, on/off for a mute, a number for a "
                              "source or a pan, text for a string."},
        },
        "help": "Set any control on a declared object.",
    },
    "toggle_control": {
        "label": "Toggle Control",
        "params": {
            "object": _object_param(),
            "control": _control_param("A mute, polarity, solo, crosspoint or other on/off control."),
        },
        "help": "Flip an on/off control. Needs a current value from the unit.",
    },
    "step_gain": {
        "label": "Step Gain (dB)",
        "params": {
            "object": _object_param(),
            "control": _control_param("A gain control."),
            "amount": {"type": "number", "required": True, "label": "Amount (dB)",
                       "default": 1.0, "min": -110, "max": 110, "unit": "dB",
                       "help": "Positive raises, negative lowers. An input "
                               "card's gain moves in 6 dB steps."},
        },
        "help": "Nudge a gain by a number of dB from its current value.",
    },
    "set_percent": {
        "label": "Set Control (% of travel)",
        "params": {
            "object": _object_param(),
            "control": _control_param("Any control."),
            "percent": {"type": "number", "required": True, "label": "Percent",
                        "min": 0, "max": 100, "unit": "%"},
        },
        "help": "Set a control by its position along its travel (0-100 %). "
                "The unit maps the percentage onto the control's own range.",
    },
    "bump_percent": {
        "label": "Bump Control (± % of travel)",
        "params": {
            "object": _object_param(),
            "control": _control_param("Any control."),
            "delta": {"type": "number", "required": True, "label": "Change",
                      "min": -100, "max": 100, "unit": "%", "default": 5},
        },
        "help": "Move a control up or down by a percentage of its travel.",
    },
    "set_raw_sv": {
        "label": "Set State Variable (raw)",
        "params": {
            "address": _address_param(),
            "sv": _sv_param(),
            "value": {"type": "integer", "required": True, "label": "Raw Value",
                      "min": INT32_MIN, "max": INT32_MAX,
                      "help": "The 32-bit value exactly as the Interface Kit "
                              "encodes it (Appendix A)."},
        },
        "help": "Write any state variable on any object, including ones not "
                "in the object list. The value goes on the wire unscaled.",
    },
    "set_string_sv": {
        "label": "Set String State Variable",
        "params": {
            "address": _address_param(),
            "sv": _sv_param(),
            "text": {"type": "string", "required": True, "label": "Text",
                     "pattern": r"^.{0,32}$",
                     "help": "Up to 32 characters (a telephone number on a BLU-103, for example)."},
        },
        "help": "Write a string state variable (Interface Kit Appendix F).",
    },
    "resync": {
        "label": "Resync from Unit",
        "help": "Re-subscribe to every declared control so each one reports "
                "its current value.",
    },
}


class BSSSoundwebLondonDriver(BaseDriver):
    """BSS Soundweb London — Direct Inject over TCP 1023."""

    DRIVER_INFO = {
        "id": "bss_soundweb_london",
        "name": "BSS Soundweb London (BLU)",
        "manufacturer": "BSS Audio",
        "category": "audio",
        "version": "1.1.1",
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls BSS Soundweb London BLU-series processors through the "
            "London Direct Inject protocol on TCP port 1023. Declare the "
            "processing objects from your Audio Architect design (gains, "
            "mixers, automixers, matrices, routers, source selectors, meters, "
            "input and output cards, or any single state variable) and each "
            "becomes a child entity whose controls panels bind to. Every "
            "control is subscribed, so changes made from Architect or a wall "
            "controller appear instantly; venue and parameter presets recall "
            "by number."
        ),
        "source_url": "https://bssaudio.com/en-US/site_elements/soundweb-london-di-kit",
        "tags": ["dsp", "bss", "soundweb", "blu", "hiqnet", "harman"],
        "verified": True,
        "simulated": True,
        "protocols": ["bss-direct-inject"],
        "ports": [1023],
        "transport": "tcp",
        "discovery": {
            # The unit's Telnet port greets unprompted with
            # "BSS Soundweb (London)\r\nUser-name: " (a BLU-100 on 6.0.5 and
            # again on 86.4.2, within 30 ms). Port 1023 cannot identify a
            # unit: it answers nothing for an object the loaded design does
            # not have, not even an ACK.
            "tcp_probe": {
                "port": 23,
                "expect": "BSS Soundweb (London)",
                "timeout_ms": 1500,
            },
            "port_open": [1023],
            "manufacturer_alias": ["bss", "bss audio", "soundweb", "soundweb london"],
        },
        "compatible_models": [
            {
                "manufacturer": "BSS Audio",
                "models": ["BLU-100"],
                "confidence": "full",
                "notes": (
                    "Run against a BLU-100 on firmware 86.4.2 with an Audio "
                    "Architect design: gain, N-input gain, source selector, "
                    "mixer, meter and input card controls set and read back, "
                    "a parameter preset recalled, and changes made in "
                    "Architect arriving live. Not run on a unit: venue "
                    "presets (London Architect designs only), string state "
                    "variables (a BLU-100 has none), and the matrix object "
                    "types."
                ),
            },
            {
                "manufacturer": "BSS Audio",
                "models": [
                    "BLU-101", "BLU-102", "BLU-103", "BLU-120",
                    "BLU-160", "BLU-320", "BLU-800", "BLU-805", "BLU-806",
                    "BLU-806DA", "BLU-16", "BLU-32", "BLU-80",
                ],
                "confidence": "untested",
                "notes": (
                    "Every Soundweb London processor speaks the same Direct "
                    "Inject protocol; the object addresses come from the "
                    "design loaded on the unit."
                ),
            },
        ],
        "help": {
            "overview": (
                "Soundweb London control over the Direct Inject protocol "
                "(TCP 1023, no login). Declare each processing object you want "
                "to control in the Objects table on the device page: its name, "
                "its HiQnet address from Architect, and its type. Every "
                "control of every object is subscribed, so panels update the "
                "moment a value changes anywhere. Drive them with Set / Toggle "
                "/ Step Control (pick the object, then the control), recall "
                "presets by number, and use Set State Variable for anything "
                "the object types do not cover."
            ),
            "setup": (
                "STEP 1 - Find the addresses.\n"
                "In Audio Architect select a processing object: the Properties "
                "window shows its Object ID (0.1.0 for the first object "
                "placed, which is 0x100). London Architect shows the full "
                "HiQnet address instead, 0x083203000100 (node, virtual "
                "device 03, object 000100).\n\n"
                "STEP 2 - Add the device.\n"
                "Enter the unit's IP address and port 1023. Leave the Node "
                "Address blank to control that unit; the address it answers "
                "as appears as Reported Node Address. Then add one row per "
                "object in the Objects table: its Object ID, the object type, "
                "and a size where the type needs one (8 inputs, 8x4, 1-4).\n\n"
                "STEP 3 - Test.\n"
                "Run Test Connection / Verify Objects. Every object that "
                "answers is listed; one that stays silent has a wrong address "
                "or is not in the design loaded on the unit.\n\n"
                "Meters are off by default. Turn on 'Subscribe to meters' to "
                "stream them at the chosen rate."
            ),
            "connection": (
                "Port 1023 needs no login. If nothing answers, check the "
                "object addresses against Audio Architect and that the "
                "design is loaded on the unit."
            ),
        },
        "default_config": {
            "host": "",
            "port": 1023,
            "node_address": "",
            "controls": DEFAULT_CONTROLS,
            "enable_meters": False,
            "meter_rate_ms": 100,
            "poll_interval": 60,
            "inter_command_delay": 0,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 1023, "label": "Port",
                     "min": 1, "max": 65535,
                     "description": "The Direct Inject port. Always 1023 on a Soundweb London."},
            "node_address": {
                "type": "string", "label": "HiQnet Node Address",
                "default": "",
                "regex": r"^\s*(0[xX][0-9A-Fa-f]{1,4}|\d{1,5})?\s*$",
                "help": "Leave blank to control the unit at this IP address. "
                        "Otherwise the unit's Node Address as Architect shows "
                        "it (0x6362 or decimal). An object row with a full "
                        "12-digit address carries its own node and can reach "
                        "another unit on the same network through this one.",
            },
            "controls": {
                "type": "table", "label": "Objects", "row_label": "object",
                "columns": CONTROL_COLUMNS,
                "help": "One row per processing object to control or watch. "
                        "Each becomes a child entity named after the row.",
            },
            "enable_meters": {
                "type": "boolean", "default": False, "label": "Subscribe to meters",
                "help": "Stream every declared meter at the rate below. Off "
                        "keeps the link quiet; meters then read as unknown.",
            },
            "meter_rate_ms": {
                "type": "integer", "default": 100,
                "min": 50, "max": 10000, "label": "Meter rate (ms)",
                "advanced": True,
                "help": "Update period per meter in 50 ms steps (50 = 20 updates a second).",
            },
            "poll_interval": {
                "type": "integer", "default": 60, "min": 0, "max": 3600,
                "label": "Resync interval (s)", "advanced": True,
                "help": "How often every subscription is renewed. Values arrive "
                        "by push in between; the renewal re-arms a unit that "
                        "rebooted and re-reads every value. 0 turns it off.",
            },
            "inter_command_delay": {
                "type": "number", "default": 0, "min": 0, "max": 1, "label": "Inter-command delay (s)",
                "advanced": True,
            },
        },
        "child_entity_types": OBJECT_CHILD_TYPES,
        "state_variables": {
            "reported_node_address": {
                "type": "string", "label": "Reported Node Address",
                "help": "The HiQnet node address the unit answers as.",
            },
            "objects_declared": {"type": "integer", "label": "Objects Declared", "min": 0},
            "objects_responding": {"type": "integer", "label": "Objects Responding", "min": 0},
            "config_problems": {"type": "string", "label": "Object List Problems"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "commands": COMMANDS,
        "quick_actions": ["recall_venue_preset", "recall_parameter_preset", "resync"],
        "actions": [
            {"id": "recall_venue_preset", "kind": "command", "icon": "bookmark"},
            {"id": "recall_parameter_preset", "kind": "command", "icon": "sliders-horizontal"},
            {"id": "resync", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection / Verify Objects",
                "icon": "search",
                "availability": "always",
                "confirm": (
                    "Opens a second session to the unit and asks every declared "
                    "object for a value, then releases those subscriptions. "
                    "Nothing on the unit changes."
                ),
            },
        ],
    }

    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the unit stopped answering (no reply to a subscribe)."
    )
    PROBE_TIMEOUT_S = 3.0
    # The liveness probe's two waits, inside HEALTH_TIMEOUT_S: the control
    # that answered last, then one control of every other answering object.
    # A BLU-100 answers a subscribe in about 20 ms.
    LIVENESS_FIRST_WAIT_S = 1.5
    LIVENESS_REST_WAIT_S = 2.5

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        self._node = 0
        self._objects: list[DIObject] = []
        self._problems: list[str] = []
        try:
            self._node = parse_node_address(config.get("node_address", ""))
        except ValueError as exc:
            self._problems.append(str(exc))
        objects, problems = parse_controls_config(config.get("controls", DEFAULT_CONTROLS), self._node)
        self._objects = objects
        self._problems.extend(problems)
        if not objects and not problems:
            self._problems.append(
                "No objects declared yet: add one row per processing object in the "
                "Objects table on the device page"
            )
        self._by_cid: dict[str, DIObject] = {o.cid: o for o in objects}
        # (node, vd, obj, sv) -> (cid, prop): where an inbound DI_SETSV lands.
        self._route: dict[tuple[int, int, int, int], tuple[str, str]] = {}
        for o in objects:
            for ctl in o.controls.values():
                self._route[o.key(ctl)] = (o.cid, ctl.prop)
        # Waiters for a DI_SETSV on a key (the liveness probe, Test Connection).
        self._waiters: dict[tuple[int, int, int, int], list[asyncio.Future[int]]] = {}
        self._responding: set[str] = set()
        # Declared control keys the unit has reported on this connection, and
        # when: the only ones the liveness probe may wait on (_liveness_probe).
        self._answered: dict[tuple[int, int, int, int], float] = {}
        self._silence_reported = False
        # The node the connected unit answers as. A frame to node 0 reaches
        # the unit at the other end of the connection, and its reply carries
        # the unit's real node (Kit p.12), so rows declared with node 0 are
        # matched to it here.
        self._unit_node: int | None = None
        self._send_lock = asyncio.Lock()
        super().__init__(device_id, config, state, events)
        for problem in self._problems:
            log.warning(f"[{self.device_id}] Object list: {problem}")

    # ── Transport ──

    def _transport_kwargs(self, transport_type: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        kwargs["delimiter"] = None
        return kwargs

    def _create_frame_parser(self) -> CallableFrameParser:
        return CallableFrameParser(parse_di_stream)

    # ── Lifecycle ──

    async def _initial_sync(self) -> None:
        self.set_state("objects_declared", len(self._objects))
        self.set_state("config_problems", "; ".join(self._problems))
        self._responding.clear()
        self._answered.clear()
        self._silence_reported = False
        self._unit_node = None
        self.set_state("objects_responding", 0)
        self._register_objects()
        await self._subscribe_all()

    async def poll(self) -> None:
        """Renew every subscription: a subscribe answers with the current
        value, so this is the resync, and it re-arms a unit that rebooted.
        While nothing has answered, ``last_error`` is written again: the
        platform clears it after a poll that leaves it alone."""
        await self._subscribe_all()
        if self._silence_reported:
            self.set_state("last_error", self._silence_message())

    async def _close_session(self) -> None:
        for waiters in self._waiters.values():
            for fut in waiters:
                if not fut.done():
                    fut.cancel()
        self._waiters.clear()

    def _register_objects(self) -> None:
        for o in self._objects:
            schema = dict(_OBJECT_SUMMARY_SCHEMA)
            for ctl in o.controls.values():
                schema[ctl.prop] = ctl.schema()
            try:
                self.register_child(
                    OBJECT_CHILD_TYPE, o.cid, schema=schema,
                    initial_state={
                        "name": o.name,
                        "object_type": OBJECT_TYPES[o.type_id]["label"],
                        "address": o.address,
                        "responding": False,
                    },
                )
            except (ValueError, TypeError) as exc:
                log.warning(f"[{self.device_id}] Could not register object {o.name!r}: {exc}")

    async def refresh_children(self) -> dict[str, Any]:
        self._register_objects()
        await self._subscribe_all()
        return {"objects": len(self._objects), "responding": len(self._responding)}

    # ── Sending ──

    async def _send(self, frame: bytes) -> None:
        if not self.transport:
            raise ConnectionError("Not connected")
        delay = float(self.config.get("inter_command_delay", 0) or 0)
        async with self._send_lock:
            await self.transport.send(frame)
            if delay > 0:
                await asyncio.sleep(delay)

    async def _send_from_receive_path(self, frame: bytes) -> None:
        """A frame sent while handling an inbound one goes straight to the
        transport: the receive path may run while a command holds the send
        lock, and one write call cannot interleave with another frame."""
        if not self.transport:
            return
        try:
            await self.transport.send(frame)
        except Exception:
            log.debug(f"[{self.device_id}] Could not send from the receive path", exc_info=True)

    async def _read_back(self, node: int, vd: int, obj: int, sv: int) -> None:
        """Ask for a value after this session changed it. The unit tells every
        other subscribed session about a change, never the one that made it
        (measured on a BLU-100), so without this the driver's own writes
        would not appear until the next resync. A subscribe is the GET."""
        await self._send(build_subscribe(node, vd, obj, sv, 0))

    def _declared(self, node: int, vd: int, obj: int, sv: int) -> bool:
        return (node, vd, obj, sv) in self._route

    def _meters_enabled(self) -> bool:
        return bool(self.config.get("enable_meters", False))

    def _meter_rate(self) -> int:
        try:
            rate = int(self.config.get("meter_rate_ms", DEFAULT_METER_RATE_MS))
        except (TypeError, ValueError):
            rate = DEFAULT_METER_RATE_MS
        rate = max(MIN_METER_RATE_MS, rate)
        return rate - rate % MIN_METER_RATE_MS

    async def _subscribe_all(self) -> None:
        meters = self._meters_enabled()
        rate = self._meter_rate()
        for o in self._objects:
            for ctl in o.controls.values():
                if ctl.fmt == FMT_METER:
                    if not meters:
                        continue
                    await self._send(build_subscribe(o.node, o.vd, o.obj, ctl.sv, rate))
                else:
                    await self._send(build_subscribe(o.node, o.vd, o.obj, ctl.sv, 0))

    def _first_control(self) -> tuple[DIObject, ControlDef] | None:
        for o in self._objects:
            for ctl in o.controls.values():
                if ctl.fmt != FMT_METER:
                    return o, ctl
        return None

    def _liveness_keys(self) -> list[tuple[int, int, int, int]]:
        """Controls that answered on this connection, one per object, the
        object that answered last first. Meters are left out: a subscribe
        at rate 0 would stop a meter's stream."""
        latest: dict[str, tuple[float, tuple[int, int, int, int]]] = {}
        for key, at in self._answered.items():
            cid, prop = self._route[key]
            o = self._by_cid.get(cid)
            if o is None or o.controls[prop].fmt == FMT_METER:
                continue
            if cid not in latest or at > latest[cid][0]:
                latest[cid] = (at, key)
        return [key for _, key in sorted(latest.values(), reverse=True)]

    # ── Receiving ──

    def _route_key(self, msg: DIMessage) -> tuple[int, int, int, int] | None:
        """The declared key an inbound frame belongs to. A reply to a frame
        sent to node 0 carries the unit's real node, so it matches the row
        declared with node 0; the node is learned from the first one."""
        key = msg.key
        if key in self._route:
            if msg.node and msg.node == self._node:
                self._learn_node(msg.node)
            return key
        alias = (0, msg.vd, msg.obj, msg.sv)
        if msg.node and alias in self._route and self._unit_node in (None, msg.node):
            self._learn_node(msg.node)
            return alias
        return None

    def _learn_node(self, node: int) -> None:
        if self._unit_node == node:
            return
        self._unit_node = node
        self.set_state("reported_node_address", f"0x{node:04X}")
        # Rows declared without a node now show the address Architect shows.
        for o in self._objects:
            if o.node == 0:
                try:
                    self.set_child_state(OBJECT_CHILD_TYPE, o.cid, "address",
                                         format_hiqnet(node, o.vd, o.obj))
                except (ValueError, KeyError):
                    pass

    async def on_data_received(self, data: bytes) -> None:
        """Nothing here is acknowledged: over Ethernet a Soundweb London
        neither sends nor expects ACK / NAK (Kit p.9; a BLU-100 sends none,
        not even for a bad checksum, which it drops)."""
        if not data or data in (bytes([ACK]), bytes([NAK])):
            return
        body = decode_frame(data)
        if body is None:
            log.warning(f"[{self.device_id}] Bad frame from unit (checksum or framing): {data.hex()}")
            return
        msg = parse_body(body)
        if msg is None:
            log.debug(f"[{self.device_id}] Unhandled DI body {body.hex()}")
            return
        if msg.cmd in PRESET_COMMANDS:
            # Another controller's broadcast recall passing through; nothing to mirror.
            return
        if msg.cmd not in (DI_SETSV, DI_SETSTRINGSV, DI_SETSVPERCENT):
            return
        key = self._route_key(msg)
        if key is None:
            log.debug(f"[{self.device_id}] Value for an undeclared SV {format_hiqnet(msg.node, msg.vd, msg.obj)} sv {msg.sv}")
            return
        if msg.cmd == DI_SETSVPERCENT:
            # Another controller set this control by percentage, and the unit
            # passes the change on in the form it was made: a percent of
            # travel, or for a bump only the amount it moved. Neither is the
            # value, so ask for it.
            node, vd, obj, sv = key
            await self._send_from_receive_path(build_subscribe(node, vd, obj, sv, 0))
            return
        self._resolve_waiters(key, msg.raw)
        cid, prop = self._route[key]
        o = self._by_cid.get(cid)
        if o is None:
            return
        ctl = o.controls[prop]
        if msg.cmd == DI_SETSTRINGSV:
            value: Any = msg.text if ctl.fmt == FMT_STRING else (msg.text or "")
        elif ctl.fmt == FMT_STRING:
            value = str(msg.raw)
        else:
            value = raw_to_value(ctl.fmt, msg.raw)
        self._answered[key] = asyncio.get_running_loop().time()
        if self._silence_reported:
            self._silence_reported = False
            self.set_state("last_error", "")
        updates: dict[str, Any] = {prop: value}
        if cid not in self._responding:
            self._responding.add(cid)
            updates["responding"] = True
            self.set_state("objects_responding", len(self._responding))
        try:
            self.set_child_state_batch(OBJECT_CHILD_TYPE, cid, updates)
        except ValueError as exc:
            log.warning(f"[{self.device_id}] Could not store {cid}.{prop}: {exc}")

    def _resolve_waiters(self, key: tuple[int, int, int, int], raw: int) -> None:
        waiters = self._waiters.pop(key, None)
        if not waiters:
            return
        for fut in waiters:
            if not fut.done():
                fut.set_result(raw)

    def _wait_for(self, key: tuple[int, int, int, int]) -> asyncio.Future[int]:
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, []).append(fut)
        return fut

    async def _liveness_probe(self) -> None:
        """Re-subscribe controls and await an echo. A subscribe is the
        protocol's GET (p.23), so any echo proves the unit is alive.

        The Interface Kit has no message a unit answers whatever design it
        holds, and the unit sends nothing back for an address it does not
        have (not even an ACK over Ethernet), so only controls that have
        answered on this connection are waited on: the one that answered
        last, then one control of every other answering object at once. The
        link is condemned only when none of them answers, so a wrong row, a
        wrong node or an object deleted from the design never drops a unit
        that is answering. When nothing has answered yet, a subscribe still
        goes out, so a unit that has gone away surfaces as a transport error,
        and ``last_error`` says what to check instead."""
        keys = self._liveness_keys()
        if not keys:
            first = self._first_control()
            if first is None:
                return
            o, ctl = first
            await self._send(build_subscribe(o.node, o.vd, o.obj, ctl.sv, 0))
            if not self._silence_reported:
                self._silence_reported = True
                self.set_state("last_error", self._silence_message())
            return
        if await self._await_any(keys[:1], self.LIVENESS_FIRST_WAIT_S):
            return
        if len(keys) > 1 and await self._await_any(keys[1:], self.LIVENESS_REST_WAIT_S):
            return
        raise TimeoutError(
            f"no reply to a subscribe on any of the {len(keys)} object(s) that had answered"
        )

    async def _await_any(self, keys: list[tuple[int, int, int, int]], timeout: float) -> bool:
        """Subscribe to each key and wait for an echo on any of them."""
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        for key in keys:
            self._waiters.setdefault(key, []).append(fut)
        try:
            for node, vd, obj, sv in keys:
                await self._send(build_subscribe(node, vd, obj, sv, 0))
            await asyncio.wait_for(fut, timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            for key in keys:
                lst = self._waiters.get(key)
                if lst and fut in lst:
                    lst.remove(fut)
                    if not lst:
                        self._waiters.pop(key, None)

    def _silence_message(self) -> str:
        return (
            f"Connected, but none of the {len(self._objects)} declared object(s) "
            f"has answered. Check the Node Address and each object's HiQnet "
            f"address against Audio Architect, and that the design is loaded "
            f"on the unit."
        )

    # ── Commands ──

    def _lookup(self, params: dict[str, Any]) -> tuple[DIObject, ControlDef]:
        cid = str(params.get("object") or "").strip()
        o = self._by_cid.get(cid) or self._by_cid.get(safe_child_id(cid))
        if o is None:
            raise ValueError(f"'{cid}' is not one of the declared objects")
        name = str(params.get("control") or "").strip()
        ctl = o.controls.get(name)
        if ctl is None:
            # Accept the label form the picker shows, case-insensitively.
            lowered = name.lower()
            for c in o.controls.values():
                if c.label.lower() == lowered or c.prop.lower() == lowered:
                    ctl = c
                    break
        if ctl is None:
            raise ValueError(f"{o.name} has no control named '{name}'")
        return o, ctl

    def _current(self, o: DIObject, ctl: ControlDef) -> Any:
        return self.get_child_state(OBJECT_CHILD_TYPE, o.cid).get(ctl.prop)

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if command == "recall_venue_preset":
            await self._send(build_preset_recall(DI_VENUE_PRESET_RECALL, int(params["preset"])))
            return None
        if command == "recall_parameter_preset":
            await self._send(build_preset_recall(DI_PARAM_PRESET_RECALL, int(params["preset"])))
            return None
        if command == "resync":
            await self._subscribe_all()
            return None
        if command == "set_control":
            o, ctl = self._lookup(params)
            if not ctl.writable:
                raise ValueError(f"{ctl.label} on {o.name} is read-only")
            value = coerce_user_value(ctl.fmt, params.get("value"))
            if ctl.fmt == FMT_STRING:
                await self._send(build_set_string(o.node, o.vd, o.obj, ctl.sv, value))
            else:
                await self._send(build_set(o.node, o.vd, o.obj, ctl.sv, value_to_raw(ctl.fmt, value)))
            await self._read_back(o.node, o.vd, o.obj, ctl.sv)
            return None
        if command == "toggle_control":
            o, ctl = self._lookup(params)
            if ctl.fmt != FMT_BOOL:
                raise ValueError(f"{ctl.label} on {o.name} is not an on/off control")
            current = self._current(o, ctl)
            if current is None:
                raise ValueError(f"{o.name} {ctl.label} has not reported a value yet")
            await self._send(build_set(o.node, o.vd, o.obj, ctl.sv, 0 if current else 1))
            await self._read_back(o.node, o.vd, o.obj, ctl.sv)
            return None
        if command == "step_gain":
            o, ctl = self._lookup(params)
            amount = float(params.get("amount", 1.0))
            if ctl.fmt == FMT_GAIN:
                current = self._current(o, ctl)
                if current is None:
                    raise ValueError(f"{o.name} {ctl.label} has not reported a value yet")
                target = max(GAIN_DB_MIN, min(GAIN_DB_MAX, float(current) + amount))
                raw = gain_db_to_raw(target)
            elif ctl.fmt == FMT_INPUT_GAIN:
                steps = round(amount / INPUT_GAIN_STEP_DB)
                if steps == 0:
                    raise ValueError(
                        f"{ctl.label} on {o.name} moves in {INPUT_GAIN_STEP_DB} dB steps. "
                        f"Use an amount of {INPUT_GAIN_STEP_DB} or -{INPUT_GAIN_STEP_DB}."
                    )
                current = self._current(o, ctl)
                if current is None:
                    raise ValueError(f"{o.name} {ctl.label} has not reported a value yet")
                target = max(0, min(INPUT_GAIN_MAX_DB, int(current) + steps * INPUT_GAIN_STEP_DB))
                raw = input_gain_db_to_raw(target)
            else:
                raise ValueError(
                    f"{ctl.label} on {o.name} is not a level, so it has no dB to step. "
                    f"Use Set Control."
                )
            await self._send(build_set(o.node, o.vd, o.obj, ctl.sv, raw))
            await self._read_back(o.node, o.vd, o.obj, ctl.sv)
            return None
        if command == "set_percent":
            o, ctl = self._lookup(params)
            if not ctl.writable:
                raise ValueError(f"{ctl.label} on {o.name} is read-only")
            pct = max(0.0, min(100.0, float(params["percent"])))
            await self._send(build_set_percent(o.node, o.vd, o.obj, ctl.sv, pct))
            await self._read_back(o.node, o.vd, o.obj, ctl.sv)
            return None
        if command == "bump_percent":
            o, ctl = self._lookup(params)
            if not ctl.writable:
                raise ValueError(f"{ctl.label} on {o.name} is read-only")
            delta = max(-100.0, min(100.0, float(params["delta"])))
            await self._send(build_bump_percent(o.node, o.vd, o.obj, ctl.sv, delta))
            await self._read_back(o.node, o.vd, o.obj, ctl.sv)
            return None
        if command == "set_raw_sv":
            node, vd, obj = parse_object_address(params.get("address"), self._node)
            sv = int(params["sv"])
            await self._send(build_set(node, vd, obj, sv, int(params["value"])))
            if self._declared(node, vd, obj, sv):
                await self._read_back(node, vd, obj, sv)
            return None
        if command == "set_string_sv":
            node, vd, obj = parse_object_address(params.get("address"), self._node)
            sv = int(params["sv"])
            await self._send(build_set_string(node, vd, obj, sv, str(params.get("text", ""))))
            if self._declared(node, vd, obj, sv):
                await self._read_back(node, vd, obj, sv)
            return None
        raise ValueError(f"Unknown command: {command}")

    # ── Test Connection / Verify Objects (setup wizard) ──

    async def _ask(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        keys: list[tuple[int, int, int, int]],
    ) -> dict[tuple[int, int, int, int], int]:
        """Subscribe to each key on the wizard's own socket and return the
        node each one answered from. A key sent to node 0 matches a reply
        from any node (the unit answers from its own)."""
        for node, vd, obj, sv in keys:
            writer.write(build_subscribe(node, vd, obj, sv, 0))
        await writer.drain()
        answered: dict[tuple[int, int, int, int], int] = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.PROBE_TIMEOUT_S
        buf = b""
        while len(answered) < len(keys):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(4096), remaining)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                frame, buf = parse_di_stream(buf)
                if frame is None or (frame == b"" and not buf):
                    break
                if not frame or frame[0] != STX:
                    continue
                body = decode_frame(frame)
                msg = parse_body(body) if body else None
                if not msg or msg.cmd not in (DI_SETSV, DI_SETSTRINGSV, DI_SETSVPERCENT):
                    continue
                for key in keys:
                    node, vd, obj, sv = key
                    if (vd, obj, sv) == (msg.vd, msg.obj, msg.sv) and node in (0, msg.node):
                        answered.setdefault(key, msg.node)
        return answered

    async def run_setup_action(self, action_id: str, params: dict[str, Any], progress: Any) -> dict[str, Any]:
        """Open a session of its own (a Soundweb takes several DI clients and
        keeps each one's subscriptions apart), subscribe to the first control
        of every declared object, and report which objects answered and the
        node the unit answers as. A unit says nothing about an address it
        does not have, so this is the only place a wrong row or node shows.
        When nothing answers at the configured node, the same controls are
        asked at node 0 (the unit at the other end of the connection), whose
        reply names the node to use."""
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", DEFAULT_PORT))
        if not host:
            raise ValueError("No IP address configured yet.")
        if self._problems:
            await progress("Object list has problems: " + "; ".join(self._problems), 5)
        await progress(f"Connecting to {host}:{port}", 10)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10.0)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach {host}:{port}. Check the address and that the unit is on the network ({exc})."
            ) from exc
        probes: list[tuple[DIObject, ControlDef]] = []
        for o in self._objects:
            ctl = next((c for c in o.controls.values() if c.fmt != FMT_METER), None) \
                or next(iter(o.controls.values()), None)
            if ctl is not None:
                probes.append((o, ctl))
        keys = [o.key(ctl) for o, ctl in probes]
        asked = list(keys)
        at_node_0: dict[tuple[int, int, int, int], int] = {}
        try:
            await progress(f"Asking {len(probes)} object(s) for a value", 30)
            answered = await self._ask(reader, writer, keys)
            if probes and not answered and any(k[0] for k in keys):
                await progress("Nothing answered at that node: asking the unit as node 0", 60)
                zero = [(0, vd, obj, sv) for _, vd, obj, sv in keys]
                asked += zero
                at_node_0 = await self._ask(reader, writer, zero)
            await progress("Releasing the test subscriptions", 90)
            for node, vd, obj, sv in asked:
                writer.write(build_unsubscribe(node, vd, obj, sv))
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        nodes = set(answered.values()) | set(at_node_0.values())
        local = [n for k, n in answered.items() if k[0] in (0, self._node)] + list(at_node_0.values())
        unit_node = local[0] if local else None
        if unit_node is not None:
            self._learn_node(unit_node)
        ok = [o.name for o, ctl in probes if o.key(ctl) in answered]
        silent = [f"{o.name} ({o.address} sv {ctl.sv})" for o, ctl in probes if o.key(ctl) not in answered]
        node_text = f" The unit answers as node 0x{unit_node:04X}." if unit_node is not None else ""
        if probes and not ok and at_node_0:
            message = (
                f"None of the {len(probes)} object(s) answered at node 0x{self._node:04X}, but the "
                f"unit answers as node 0x{unit_node:04X}. Set the Node Address to "
                f"0x{unit_node:04X}, or leave it blank."
            )
        elif probes and not ok:
            message = (
                f"Connected to {host}:{port}, but none of the {len(probes)} object(s) answered. "
                f"Check each object's HiQnet address against Audio Architect, and that the "
                f"design is loaded on the unit."
            )
        elif silent:
            message = (
                f"{len(ok)} of {len(probes)} object(s) answered. Silent: {', '.join(silent)}. "
                f"A silent object has a wrong address or is not in the design loaded on the "
                f"unit.{node_text}"
            )
        elif probes:
            message = f"All {len(probes)} object(s) answered.{node_text}"
        else:
            message = f"Connected to {host}:{port}. No objects are declared yet, so nothing was verified."
        await progress(message, 100)
        return {
            "ok": bool(probes) and not silent,
            "message": message,
            "answered": ok,
            "silent": silent,
            "unit_node": f"0x{unit_node:04X}" if unit_node is not None else "",
            "nodes_seen": sorted(f"0x{n:04X}" for n in nodes),
            "problems": list(self._problems),
        }
