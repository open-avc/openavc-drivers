"""
OpenAVC ZeeVee ZyPer Management Platform Driver.

Controls a ZeeVee (Kramer) AV-over-IP system through its ZyPer Management
Platform (ZMP): ZyPer4K, ZyPer4K-XS / XSE / XR, ZyPerUHD60 and Kramer KDS-17
encoders and decoders. A control system talks to the ZMP only, never to an
endpoint; every encoder and decoder the ZMP manages appears here as a child.

Protocol sources (ZeeVee, all public):
  * "ZyPer Management Platform API User Manual", API Release 4.1x,
    September 2025 (https://www.zeevee.com/zmp-api-manual/): the Telnet
    shell on TCP 23, the ``Zyper$`` prompt, every command and its reply.
  * "ZyPer Management Platform User Manual" 4.0.3, section 4, Event
    Mechanism: the ``events`` mode, the event line format and the event
    list, which the API manual sends the reader to ("See Section 4").
  * The same user manual's Accounts section for the two Telnet sign-ins:
    ``oldAuth`` (no prompt, or a password if one is set) and ``fullAuth``
    (``username:`` then ``password:``).

Push vs poll: HYBRID. A second Telnet session in ``events`` mode receives
every endpoint's state as it changes (up / down, cable, resolution, whether
a decoder is receiving video and why not, RS-232 and IR data), starting
with a DeviceStatus event for every device. No event reports a join, a
rename or a new video wall, so routing and the roster are polled:
``show device config all since <lastChangeIdMax>`` answers only the units
that changed, with a full read every minute and whenever a device was
deleted. Without the events session (Live Updates off, or the ZMP refusing
a second session) encoder inputs are polled instead.

Why Python (not .avcdriver YAML):
  * The roster is device-enumerated: encoders, decoders and video walls are
    registered from the ZMP's own lists. YAML rosters are config-driven.
  * A reply is a block of records under a ``device(<mac>);`` header, and the
    header says which device every following line belongs to; the CLI also
    wraps long lines mid-word at its line-wrap width. YAML response rules
    see one line at a time.
  * Live updates arrive on a second session the driver opens and signs in to.
  * Every join is answered "Success" and never with the new route, so each
    one is read back with ``show device config <mac>``, and one the ZMP
    accepted and does not report is refused.
  * Three sign-in shapes on one port: none (the factory default), a Telnet
    password, and a username and password per account.

Wire identity: commands address an endpoint by its MAC address, exactly as
the ZMP prints it, so a name that is a prefix of another (``show device
config`` matches part of a name) or a rename between polls can never reach
the wrong unit. Children are named by the device name set in the ZMP.

Not covered (commissioning work done in the ZMP's web interface, or reached
through Send API Command): accounts, TLS, LDAP, SNMP, EDID files, creating
presets, video walls, multiviews and zones, firmware updates, Dante and KVM
configuration, redundancy, and server settings. SSH is not offered: the API
manual names it but documents no sign-in for it.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import re
from dataclasses import dataclass, field
from typing import Any

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1
    CommandRefusedError = RuntimeError

log = get_logger(__name__)

# How long one command may take. A full roster read on a large system prints
# a few dozen lines per unit, so it gets longer.
REQUEST_TIMEOUT_S = 10.0
ROSTER_TIMEOUT_S = 30.0
# A reply that arrives after its request timed out is waited for this long
# before the next request goes out; past it the session is reopened, since
# every later reply would answer the wrong question.
LATE_REPLY_S = 5.0
SIGN_IN_TIMEOUT_S = 10.0
# Poll cycles between full reads (the roster, video walls, multiviews,
# zones and the server's details).
FULL_READ_EVERY = 12
# Without live updates, encoder inputs are read every this many cycles.
STATUS_READ_EVERY = 3
# An event line the CLI wrapped is complete when the next event starts or
# nothing more arrives for this long.
EVENT_SETTLE_S = 0.3
EVENTS_RETRY_S = (5.0, 15.0, 30.0, 60.0)
# A join the ZMP accepted and does not yet report is read again after this.
READBACK_RETRY_S = 1.0

JOIN_MODES = ("fastSwitched", "genlocked", "genlockedScaled")
VIDEO_PORTS = (
    "auto", "hdmi", "hdmiOptionalIn", "usbc", "displayPort", "hdsdi",
    "12gsdi", "component", "composite", "s-video", "vga",
)

# Telnet (RFC 854 / 855). The driver answers every option offer with a
# refusal, as a plain terminal does, and only while signing in: on the
# events session any byte sent ends events mode.
IAC, DONT, DO, WONT, WILL, SB, SE = 0xFF, 0xFE, 0xFD, 0xFC, 0xFB, 0xFA, 0xF0

_PROMPT = re.compile(r"^\r*zyper\$ ?", re.IGNORECASE)
_USER_PROMPT = re.compile(r"(user\s*name|login)[^\n:>]*[:>]\s*$", re.IGNORECASE)
_PASS_PROMPT = re.compile(r"password[^\n:>]*[:>]\s*$", re.IGNORECASE)
_REFUSAL = re.compile(r"invalid|incorrect|denied|fail|locked|disabled", re.IGNORECASE)
# A block header, ``device(d8:80:39:9a:96:7);``, and the counters that end a
# reply, ``lastChangeIdMax(176);``.
_HEADER = re.compile(r"^([A-Za-z]\w*)\(([^()]*)\);\s*$")
# A record, ``  device.gen; model=Zyper4K, ...`` or a zone, ``  1stfloor; a, b``.
_RECORD = re.compile(r"^\s*([A-Za-z0-9][\w\-]*(?:\.[\w\-]+)*);(?:\s+(.*))?$")
_OK = re.compile(r"^\s*Success\s*$", re.IGNORECASE)
_ERROR = re.compile(r"^\s*Error:\s*\((\d+)\)\s*(.*)$", re.IGNORECASE)
_WARNING = re.compile(r"^\s*Warning:\s*\((\d+)\)\s*(.*)$", re.IGNORECASE)
_EVENT = re.compile(r"^\s*Event::", re.IGNORECASE)
_SOURCE = re.compile(r"^(.*)\(([0-9A-Fa-f]{1,2}(?::[0-9A-Fa-f]{1,2}){5})\)\s*$")

_COUNTERS = ("lastchangeidmax", "lastdeleteidmax", "lastchangeid")


# ── Parsing helpers ──


def _strip_iac(buf: bytes) -> tuple[bytes, bytes, bytes]:
    """Split raw Telnet bytes into (text bytes, refusals owed, unfinished tail)."""
    out = bytearray()
    replies = bytearray()
    i, n = 0, len(buf)
    while i < n:
        b = buf[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            return bytes(out), bytes(replies), buf[i:]
        cmd = buf[i + 1]
        if cmd == IAC:
            out.append(IAC)
            i += 2
        elif cmd in (WILL, WONT, DO, DONT):
            if i + 2 >= n:
                return bytes(out), bytes(replies), buf[i:]
            opt = buf[i + 2]
            if cmd == WILL:
                replies += bytes((IAC, DONT, opt))
            elif cmd == DO:
                replies += bytes((IAC, WONT, opt))
            i += 3
        elif cmd == SB:
            end = buf.find(bytes((IAC, SE)), i + 2)
            if end < 0:
                return bytes(out), bytes(replies), buf[i:]
            i = end + 2
        else:
            i += 2  # NOP, GA and the other two-byte commands
    return bytes(out), bytes(replies), b""


def _split_values(text: str) -> list[str]:
    """Split ``a=1, b="x, y", c=3`` on the commas outside quotes."""
    parts: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            cur.append(ch)
        elif ch == "," and not quoted:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _parse_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in _split_values(text or ""):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        value = value.strip().rstrip(";").strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _after_arrow(value: str) -> str:
    """``1280->720`` is a change; the new value is after the arrow."""
    return value.rsplit("->", 1)[-1].strip() if "->" in value else value.strip()


def _norm_mac(mac: str) -> str:
    """The ZMP prints ``0:16:c0:4d:e3:12`` in some lists and ``00:16:c0:...``
    in others; compare in one form."""
    parts = str(mac).strip().lower().split(":")
    if len(parts) != 6:
        return str(mac).strip().lower()
    try:
        return ":".join(f"{int(p, 16):02x}" for p in parts)
    except ValueError:
        return str(mac).strip().lower()


def _is_none(value: Any) -> bool:
    return str(value or "").strip().lower() in ("", "none", "na", "null")


def _sanitize_id(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name).strip()).strip("_")
    return (cleaned or "unit").lower()[:40]


def _yes(value: Any) -> bool:
    return str(value or "").strip().lower() in ("yes", "true", "up", "connected", "enabled", "1")


def _is_start(line: str) -> bool:
    """Whether a physical line starts a new logical line, rather than carrying
    on one the CLI wrapped at its line-wrap width."""
    return bool(
        _HEADER.match(line.strip())
        or _RECORD.match(line)
        or _OK.match(line)
        or _ERROR.match(line)
        or _WARNING.match(line)
        or _EVENT.match(line)
    )


@dataclass
class _Block:
    kind: str
    ident: str
    records: list[tuple[str, dict[str, str], str]] = field(default_factory=list)

    def merged(self, path: str) -> dict[str, str]:
        """Every record at ``path`` folded into one dict (``device.gen`` is
        printed over several lines)."""
        out: dict[str, str] = {}
        for rec_path, values, _raw in self.records:
            if rec_path.lower() == path.lower():
                out.update(values)
        return out


def _parse_blocks(lines: list[str]) -> tuple[list[_Block], list[_Block], dict[str, int]]:
    """Logical reply lines into (blocks under a header, records with no
    header, the counters that end the reply)."""
    blocks: list[_Block] = []
    loose = _Block("", "")
    counters: dict[str, int] = {}
    current: _Block | None = None
    for line in lines:
        m = _HEADER.match(line.strip())
        if m:
            kind, ident = m.group(1), m.group(2)
            if kind.lower() in _COUNTERS:
                try:
                    counters[kind.lower()] = int(ident.strip())
                except ValueError:
                    pass
                continue
            current = _Block(kind, ident.strip())
            blocks.append(current)
            continue
        m = _RECORD.match(line)
        if m:
            rest = (m.group(2) or "").strip()
            record = (m.group(1), _parse_kv(rest), rest)
            (current if current is not None else loose).records.append(record)
    return blocks, [loose] if loose.records else [], counters


@dataclass
class _Reply:
    command: str
    lines: list[str] = field(default_factory=list)
    ok: bool | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)


class _Pending:
    def __init__(self, command: str, future: asyncio.Future):
        self.reply = _Reply(command)
        self.future = future


class _Session:
    """One Telnet conversation with the ZMP: option offers refused, text split
    into physical lines, and the bare ``Zyper$`` prompt (which has no line
    ending) noticed as soon as it arrives."""

    def __init__(self) -> None:
        self._carry = b""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.partial = ""
        self.negotiating = True
        self.prompted = False
        self.lines_seen: list[str] = []
        self.changed = asyncio.Event()

    def feed(self, data: bytes) -> tuple[list[tuple[str, str]], bytes]:
        clean, replies, self._carry = _strip_iac(self._carry + data)
        self.partial += self._decoder.decode(clean).replace("\x00", "")
        tokens: list[tuple[str, str]] = []
        while True:
            m = _PROMPT.match(self.partial)
            if m:
                tokens.append(("prompt", ""))
                self.prompted = True
                self.partial = self.partial[m.end():]
                continue
            nl = self.partial.find("\n")
            if nl < 0:
                break
            line = self.partial[:nl].rstrip("\r")
            self.partial = self.partial[nl + 1:]
            tokens.append(("line", line))
        self.changed.set()
        return tokens, (replies if self.negotiating else b"")


class ZeeveeZmpDriver(BaseDriver):
    """ZeeVee ZyPer AV-over-IP system via the ZyPer Management Platform (Telnet 23)."""

    DRIVER_INFO = {
        "id": "zeevee_zmp",
        "name": "ZeeVee ZyPer Management Platform",
        "manufacturer": "ZeeVee",
        "category": "switcher",
        "version": "1.0.0",
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls a ZeeVee ZyPer AV-over-IP system (ZyPer4K, ZyPer4K-XS / "
            "XSE / XR, ZyPerUHD60 and the Kramer KDS-17) through its ZyPer "
            "Management Platform. Every encoder, decoder and video wall the "
            "platform manages appears as a child with live state: routing "
            "with separate video, HDMI audio and analog audio, video walls, "
            "zones, multiviews, presets, channel up and down, and RS-232, IR "
            "and CEC sent to the equipment at each end."
        ),
        "source_url": "https://www.zeevee.com/zmp-api-manual/",
        "tags": [
            "av-over-ip", "zyper", "sdvoe", "matrix-switcher", "video-wall",
            "multiview", "encoder", "decoder",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["zyper_api"],
        "ports": [23],
        "transport": "tcp",
        # Each signal has its own join mode, so a plane names the command
        # that switches it. Video comes first because it is the everyday
        # one; a video wall is a destination of its own.
        "routing": {
            "destination_child_type": "decoder",
            "source_child_type": "encoder",
            "planes": [
                {"label": "Video", "route_property": "source", "command": "route"},
                {"label": "HDMI Audio", "route_property": "hdmi_audio_source",
                 "command": "route_hdmi_audio"},
                {"label": "Analog Audio", "route_property": "analog_audio_source",
                 "command": "route_analog_audio"},
                {"label": "Video Wall", "route_property": "source",
                 "command": "route_video_wall", "destination_child_type": "video_wall"},
            ],
        },
        "discovery": {
            # The API shell greets with its prompt before anything is typed
            # (API manual, "Using Telnet"), so a read of the greeting is
            # enough. A ZMP set to ask for a password greets with the
            # prompt for it instead and is not identified this way.
            "tcp_probe": {
                "port": 23,
                "expect_regex": r"(?i)zyper\$",
                "timeout_ms": 3000,
            },
            # zyper.local is the ZMP's factory hostname (API manual, "Using
            # Telnet"; show server info prints hostname=zyper.local).
            "hostname": [r"(?i)^zyper(\.local)?$"],
        },
        "compatible_models": [
            {
                "manufacturer": "ZeeVee",
                "models": [
                    "ZyPer Management Platform",
                    "ZyPer4K",
                    "ZyPer4K-XS",
                    "ZyPer4K-XSE",
                    "ZyPer4K-XR",
                    "ZyPerUHD60",
                ],
                "confidence": "untested",
                "notes": (
                    "Add the ZyPer Management Platform (ZMP) as the device: its "
                    "encoders and decoders appear as children. Written from "
                    "API release 4.1x; ZyPerUHD (non-60) units managed by the "
                    "same platform appear too. Genlocked joins, multiviews, "
                    "IR and the input port choice are ZyPer4K only."
                ),
            },
            {
                "manufacturer": "Kramer",
                "models": ["KDS-17"],
                "confidence": "untested",
                "notes": (
                    "Managed by the ZyPer Management Platform from release 4.1 "
                    "(compatible with ZyPerUHD60). Add the ZMP, not the KDS-17."
                ),
            },
        ],
        "help": {
            "overview": (
                "ZyPer is ZeeVee's AV-over-IP system (Kramer's 10G line): "
                "encoders take sources onto the network and decoders feed "
                "displays, with the ZyPer Management Platform (ZMP) as the "
                "one point of control. This driver connects to the ZMP and "
                "shows every encoder, decoder and video wall as a child. "
                "Decoders carry their video and audio sources and whether "
                "they are receiving video; encoders carry their input. "
                "Routing, video walls, zones, multiviews, presets and RS-232, "
                "IR and CEC to the connected equipment are available to "
                "macros and panels."
            ),
            "setup": (
                "1. Enter the ZMP's IP address (zyper.local also works on most "
                "networks). Port 23 is the API's Telnet port.\n"
                "2. Leave Username and Password empty if the ZMP does not ask "
                "for them, which is how it ships. Enter the password if a "
                "Telnet password is set, and both if accounts are set to "
                "full authentication (Accounts > All Configuration).\n"
                "3. Name every encoder and decoder in the ZMP first: children "
                "are named after the device names, and video walls, zones, "
                "multiviews and presets are picked by their names.\n"
                "4. Live Updates uses a second Telnet session. If the ZMP "
                "limits sessions per account to one, turn Live Updates off "
                "and encoder inputs are polled instead.\n"
                "5. RS-232 to an endpoint needs the endpoint linked to the "
                "ZMP once (Link RS-232 to ZMP), which can restart it.\n"
                "6. Use Refresh from Device after adding or renaming "
                "endpoints; otherwise the list catches up within a minute."
            ),
        },
        "default_config": {
            "host": "",
            "port": 23,
            "username": "",
            "password": "",
            "live_updates": True,
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address",
                     "description": "IP address or hostname of the ZyPer Management Platform."},
            "port": {"type": "integer", "default": 23, "min": 1, "max": 65535,
                     "label": "Telnet Port",
                     "description": "The API's Telnet port: 23 unless it was changed."},
            "username": {"type": "string", "default": "", "label": "Username",
                         "description": "Only when the ZMP asks for a username (full authentication)."},
            "password": {"type": "string", "default": "", "secret": True, "label": "Password",
                         "description": "The Telnet password or the account's password. Empty when the ZMP asks for none."},
            "live_updates": {
                "type": "boolean", "default": True, "label": "Live Updates",
                "description": (
                    "Keep a second Telnet session in events mode, so endpoint "
                    "status, signal and resolution changes show at once. Off: "
                    "encoder inputs are polled and decoder signal status is not shown."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 5, "min": 1, "label": "Poll Interval (sec)",
                "description": (
                    "How often routes and names are read. Routes changed "
                    "here are read back at once; this is how changes made "
                    "elsewhere are noticed."
                ),
            },
        },
        "state_variables": {
            "server_version": {"type": "string", "label": "ZMP Version"},
            "server_hostname": {"type": "string", "label": "ZMP Hostname", "cloud_priority": "low"},
            "server_type": {"type": "string", "label": "ZMP Hardware", "cloud_priority": "low"},
            "serial_number": {"type": "string", "label": "Serial Number", "cloud_priority": "low"},
            "redundancy_master": {"type": "boolean", "label": "Master",
                                  "help": "True on the master ZMP of a redundant pair (and on a single ZMP).",
                                  "cloud_priority": "low"},
            "zyper4k_limit": {"type": "integer", "label": "ZyPer4K Licence Limit",
                              "cloud_priority": "low"},
            "zyper4k_devices": {"type": "integer", "label": "ZyPer4K Devices",
                                "cloud_priority": "low"},
            "zyper4k_exceeded": {"type": "boolean", "label": "Licence Exceeded",
                                 "help": "More ZyPer4K endpoints than the licence allows.",
                                 "cloud_priority": "high"},
            "encoder_count": {"type": "integer", "label": "Encoders", "cloud_priority": "low"},
            "encoders_online": {"type": "integer", "label": "Encoders Online", "cloud_priority": "low"},
            "decoder_count": {"type": "integer", "label": "Decoders", "cloud_priority": "low"},
            "decoders_online": {"type": "integer", "label": "Decoders Online", "cloud_priority": "low"},
            "live_updates": {"type": "boolean", "label": "Live Updates Connected",
                             "help": "The events session is open and receiving.",
                             "cloud_priority": "low"},
            "video_wall_options": {"type": "string", "label": "Video Wall Options",
                                   "help": "JSON list of the video walls on the ZMP.",
                                   "cloud_priority": "low"},
            "multiview_options": {"type": "string", "label": "Multiview Options",
                                  "help": "JSON list of the multiviews on the ZMP (ZyPer4K).",
                                  "cloud_priority": "low"},
            "zone_options": {"type": "string", "label": "Zone Options",
                             "help": "JSON list of the zones on the ZMP.",
                             "cloud_priority": "low"},
            "endpoint_options": {"type": "string", "label": "Endpoint Options",
                                 "help": "JSON list of every encoder and decoder, for the endpoint dropdowns.",
                                 "cloud_priority": "low"},
            "last_error": {"type": "string", "label": "Last Error", "cloud_priority": "low"},
        },
        "child_entity_types": {
            "encoder": {
                "label": "Encoder",
                "label_plural": "Encoders",
                "id_format": {"type": "string", "max_length": 40},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
                    "mac": {"type": "string", "label": "MAC Address", "cloud_priority": "low"},
                    "ip": {"type": "string", "label": "IP Address", "cloud_priority": "low"},
                    "firmware": {"type": "string", "label": "Firmware", "cloud_priority": "low"},
                    "cable_connected": {"type": "boolean", "label": "Input Connected",
                                        "help": "A source is plugged into the encoder's input.",
                                        "control": True, "cloud_priority": "high"},
                    "resolution": {"type": "string", "label": "Input Resolution"},
                    "frame_rate": {"type": "string", "label": "Input Frame Rate",
                                   "cloud_priority": "low"},
                    "hdcp_version": {"type": "string", "label": "Input HDCP",
                                     "help": "none, 1.4 or 2.2.", "cloud_priority": "low"},
                    "video_port": {"type": "string", "label": "Input Port",
                                   "help": "The input in use on units with more than one.",
                                   "cloud_priority": "low"},
                    "rs232_data": {"type": "string", "label": "RS-232 Received",
                                   "help": "The last RS-232 data the endpoint received (needs Live Updates).",
                                   "cloud_priority": "low"},
                    "ir_data": {"type": "string", "label": "IR Received",
                                "help": "The last IR code the endpoint received (needs Live Updates).",
                                "cloud_priority": "low"},
                },
                "summary_fields": ["online", "cable_connected", "resolution"],
                "label_field": "name",
            },
            "decoder": {
                "label": "Decoder",
                "label_plural": "Decoders",
                "id_format": {"type": "string", "max_length": 40},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
                    "mac": {"type": "string", "label": "MAC Address", "cloud_priority": "low"},
                    "ip": {"type": "string", "label": "IP Address", "cloud_priority": "low"},
                    "firmware": {"type": "string", "label": "Firmware", "cloud_priority": "low"},
                    "source": {"type": "string", "label": "Video Source",
                               "help": "The encoder joined to this decoder; empty when none. A multiview or video wall shows its own name.",
                               "control": True, "cloud_priority": "high"},
                    "connection_mode": {"type": "string", "label": "Join Mode",
                                        "help": "fastSwitched, genlocked or genlockedScaled.",
                                        "cloud_priority": "low"},
                    "hdmi_audio_source": {"type": "string", "label": "HDMI Audio Source",
                                          "control": True, "cloud_priority": "high"},
                    "analog_audio_source": {"type": "string", "label": "Analog Audio Source",
                                            "control": True, "cloud_priority": "high"},
                    "audio_follows_video": {"type": "boolean", "label": "Audio Follows Video",
                                            "cloud_priority": "low"},
                    "video_status": {
                        "type": "enum", "label": "Receiving Video",
                        "values": ["yes", "no", "yes-with-warning"],
                        "help": "Whether the decoder is receiving video from its encoder (needs Live Updates).",
                        "cloud_priority": "high",
                    },
                    "video_status_detail": {"type": "string", "label": "Video Status Detail",
                                            "help": "Why there is no video, or the warning, in the ZMP's words."},
                    "cable_connected": {"type": "boolean", "label": "Output Cable Connected",
                                        "help": "The decoder's HDMI output cable, as the ZMP reports it (needs Live Updates).",
                                        "cloud_priority": "low"},
                    "resolution": {"type": "string", "label": "Output Resolution"},
                    "frame_rate": {"type": "string", "label": "Output Frame Rate",
                                   "cloud_priority": "low"},
                    "rs232_data": {"type": "string", "label": "RS-232 Received",
                                   "help": "The last RS-232 data the endpoint received (needs Live Updates).",
                                   "cloud_priority": "low"},
                    "ir_data": {"type": "string", "label": "IR Received",
                                "help": "The last IR code the endpoint received (needs Live Updates).",
                                "cloud_priority": "low"},
                },
                "summary_fields": ["online", "source", "video_status"],
                "label_field": "name",
            },
            "video_wall": {
                "label": "Video Wall",
                "label_plural": "Video Walls",
                "id_format": {"type": "string", "max_length": 40},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "source": {"type": "string", "label": "Source",
                               "help": "The encoder shown on the wall; empty when none.",
                               "control": True, "cloud_priority": "high"},
                    "rows": {"type": "integer", "label": "Rows", "cloud_priority": "low"},
                    "columns": {"type": "integer", "label": "Columns", "cloud_priority": "low"},
                    "decoders": {"type": "string", "label": "Decoders",
                                 "help": "The decoders in the wall, row by row.",
                                 "cloud_priority": "low"},
                },
                "summary_fields": ["source"],
                "label_field": "name",
            },
        },
        "quick_actions": ["route", "route_off", "run_preset", "send_cec"],
        "commands": {
            "route": {
                "label": "Route Video",
                "params": {
                    "encoder": {"type": "child_id", "child_type": "encoder",
                                "required": True, "label": "Source (Encoder)"},
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Display (Decoder)"},
                    "mode": {"type": "enum", "values": ["fastSwitched", "genlocked", "genlockedScaled"], "label": "Join Mode",
                             "help": "Leave empty to keep the decoder's current mode (fastSwitched when it has none). Genlocked modes are ZyPer4K only."},
                },
                "help": "Shows the encoder's video on the decoder's display. HDMI audio follows when the decoder is set to.",
            },
            "route_off": {
                "label": "Clear Video",
                "params": {
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Display (Decoder)"},
                },
                "help": "Removes the video from the decoder: a fast-switched decoder shows black, a genlocked one stops its output.",
            },
            "route_hdmi_audio": {
                "label": "Route HDMI Audio",
                "params": {
                    "encoder": {"type": "child_id", "child_type": "encoder",
                                "required": True, "label": "Source (Encoder)"},
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Display (Decoder)"},
                },
                "help": "Sends the encoder's HDMI audio to the decoder. Turns off Audio Follows Video on that decoder.",
            },
            "route_hdmi_audio_off": {
                "label": "Clear HDMI Audio",
                "params": {
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Display (Decoder)"},
                },
                "help": "Removes the HDMI audio from the decoder.",
            },
            "route_analog_audio": {
                "label": "Route Analog Audio",
                "params": {
                    "encoder": {"type": "child_id", "child_type": "encoder",
                                "required": True, "label": "Source (Encoder)"},
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Display (Decoder)"},
                },
                "help": "Sends the audio from the encoder's analog audio jack to the decoder. On a Dante ZyPerUHD60 encoder this sets it to Dante transmit.",
            },
            "route_analog_audio_off": {
                "label": "Clear Analog Audio",
                "params": {
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Display (Decoder)"},
                },
                "help": "Removes the analog audio from the decoder.",
            },
            "set_audio_follow_video": {
                "label": "Set Audio Follows Video",
                "params": {
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Display (Decoder)"},
                    "enabled": {"type": "boolean", "required": True, "label": "Follow Video"},
                },
                "help": "On: the decoder takes the HDMI audio of whatever video is routed to it. Off: its audio is routed separately.",
            },
            "route_usb": {
                "label": "Route USB",
                "params": {
                    "encoder": {"type": "child_id", "child_type": "encoder",
                                "required": True, "label": "Computer (Encoder)"},
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Desk (Decoder)"},
                },
                "help": "Connects the USB devices at the decoder to the computer at the encoder.",
            },
            "route_usb_off": {
                "label": "Clear USB",
                "params": {
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Desk (Decoder)"},
                },
                "help": "Disconnects the decoder's USB devices.",
            },
            "route_video_wall": {
                "label": "Route to Video Wall",
                "params": {
                    "encoder": {"type": "child_id", "child_type": "encoder",
                                "required": True, "label": "Source (Encoder)"},
                    "video_wall": {"type": "child_id", "child_type": "video_wall",
                                   "required": True, "label": "Video Wall"},
                },
                "help": "Shows the encoder's video across every display in the video wall.",
            },
            "route_zone": {
                "label": "Route to Zone",
                "params": {
                    "encoder": {"type": "child_id", "child_type": "encoder",
                                "required": True, "label": "Source (Encoder)"},
                    "zone": {"type": "string", "required": True, "label": "Zone",
                             "options_state": "zone_options",
                             "help": "A zone set up in the ZMP; a zone inside another is written Outer.Inner."},
                    "mode": {"type": "enum", "values": ["fastSwitched", "genlocked", "genlockedScaled"], "label": "Join Mode",
                             "help": "Leave empty for fastSwitched."},
                },
                "help": "Shows the encoder's video on every display in the zone.",
            },
            "show_multiview": {
                "label": "Show Multiview",
                "params": {
                    "multiview": {"type": "string", "required": True, "label": "Multiview",
                                  "options_state": "multiview_options"},
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Display (Decoder)"},
                },
                "help": "Shows a multiview (several sources on one screen) on the decoder's display. ZyPer4K only.",
            },
            "set_multiview_window": {
                "label": "Set Multiview Window Source",
                "params": {
                    "multiview": {"type": "string", "required": True, "label": "Multiview",
                                  "options_state": "multiview_options"},
                    "window": {"type": "integer", "required": True, "min": 1, "max": 19,
                               "label": "Window"},
                    "encoder": {"type": "child_id", "child_type": "encoder",
                                "required": False, "label": "Source (Encoder)",
                                "help": "Leave empty to empty the window."},
                },
                "help": "Changes which source a multiview window shows. ZyPer4K only.",
            },
            "channel_up": {
                "label": "Channel Up",
                "params": {
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Display (Decoder)"},
                },
                "help": "Switches the decoder to the next encoder whose name ends in a channel number (_1, _2, ...).",
            },
            "channel_down": {
                "label": "Channel Down",
                "params": {
                    "decoder": {"type": "child_id", "child_type": "decoder",
                                "required": True, "label": "Display (Decoder)"},
                },
                "help": "Switches the decoder to the previous encoder whose name ends in a channel number.",
            },
            "run_preset": {
                "label": "Run Preset",
                "params": {
                    "preset": {"type": "string", "required": True, "label": "Preset",
                               "help": "The preset's name in the ZMP, exactly as written (names are case-sensitive)."},
                },
                "help": "Runs a preset saved in the ZMP, making every route it holds.",
            },
            "select_video_port": {
                "label": "Select Input Port",
                "params": {
                    "encoder": {"type": "child_id", "child_type": "encoder",
                                "required": True, "label": "Encoder"},
                    "port": {"type": "enum", "values": ["auto", "hdmi", "hdmiOptionalIn", "usbc", "displayPort", "hdsdi",
                                        "12gsdi", "component", "composite", "s-video", "vga"], "required": True,
                             "label": "Input"},
                },
                "help": "Picks the input an encoder with more than one uses (ZyPer4K and ZyPerUHD60). On a ZyPerUHD60, hdmi is HDMI IN1 and hdmiOptionalIn is HDMI IN2.",
            },
            "send_rs232": {
                "label": "Send RS-232",
                "params": {
                    "endpoint": {"type": "string", "required": True, "label": "Endpoint",
                                 "options_state": "endpoint_options"},
                    "data": {"type": "string", "required": True, "label": "Data",
                             "pattern": "^[^\"]{1,256}$", "trim": False,
                             "help": "Up to 256 characters. Write \\r for a carriage return, \\n for a line feed and \\xNN for any byte, e.g. PWR ON\\r"},
                },
                "help": "Sends text out of the endpoint's RS-232 port to the equipment connected there. The endpoint must be linked to the ZMP once first.",
            },
            "send_ir": {
                "label": "Send IR",
                "params": {
                    "endpoint": {"type": "string", "required": True, "label": "Endpoint",
                                 "options_state": "endpoint_options"},
                    "code": {"type": "string", "required": True, "label": "Pronto Code",
                             "pattern": "^[0-9A-Fa-f ]{4,1024}$",
                             "help": "The code in Pronto hex."},
                },
                "help": "Sends an IR code out of the endpoint's IR port. ZyPer4K only.",
            },
            "send_cec": {
                "label": "Send CEC",
                "params": {
                    "endpoint": {"type": "string", "required": True, "label": "Endpoint",
                                 "options_state": "endpoint_options"},
                    "action": {"type": "enum", "values": ["on", "off"], "required": True,
                               "label": "Power"},
                },
                "help": "Turns the display or source connected to the endpoint on or off over HDMI-CEC.",
            },
            "send_cec_hex": {
                "label": "Send CEC Message",
                "params": {
                    "endpoint": {"type": "string", "required": True, "label": "Endpoint",
                                 "options_state": "endpoint_options"},
                    "hex": {"type": "string", "required": True, "label": "CEC Hex",
                            "pattern": "^[0-9A-Fa-f]{2,64}$",
                            "help": "Hex digits with no spaces, e.g. 4004."},
                },
                "help": "Sends any CEC message over HDMI. ZyPer4K and ZyPerUHD60 only.",
            },
            "link_rs232": {
                "label": "Link RS-232 to ZMP",
                "params": {
                    "endpoint": {"type": "string", "required": True, "label": "Endpoint",
                                 "options_state": "endpoint_options"},
                },
                "help": "Connects the endpoint's RS-232 port to the ZMP so Send RS-232 reaches it. Needed once per endpoint; the endpoint may restart.",
            },
            "identify": {
                "label": "Identify Endpoint",
                "params": {
                    "endpoint": {"type": "string", "required": True, "label": "Endpoint",
                                 "options_state": "endpoint_options"},
                },
                "help": "Flashes the endpoint's lights for 5 seconds so it can be found in the rack.",
            },
            "restart_endpoint": {
                "label": "Restart Endpoint",
                "params": {
                    "endpoint": {"type": "string", "required": True, "label": "Endpoint",
                                 "options_state": "endpoint_options"},
                },
                "help": "Restarts one encoder or decoder. Its picture drops until it is back.",
            },
            "refresh": {
                "label": "Refresh from ZMP",
                "params": {},
                "help": "Reads every endpoint, route, video wall, multiview and zone again.",
            },
            "api_command": {
                "label": "Send API Command",
                "params": {
                    "command": {"type": "string", "required": True, "label": "Command",
                                "help": "Any command from the ZMP API manual, e.g. show device connections."},
                },
                "help": "Sends one API command to the ZMP and returns its answer.",
            },
        },
    }

    # ── Lifecycle ──

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._session = _Session()
        self._request_lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._logical: str | None = None
        self._signed_in = False
        # Roster: normalized MAC -> {ctype, cid, name, wire}.
        self._units: dict[str, dict[str, Any]] = {}
        self._child_mac: dict[tuple[str, str], str] = {}
        self._mac_by_name: dict[str, str] = {}
        self._walls: dict[str, str] = {}  # child id -> wall name
        self._change_max: int | None = None
        self._delete_max: int | None = None
        self._since_works = True
        self._need_full = True
        self._cycle = 0
        self._error = ""
        # Events session.
        self._events_task: asyncio.Task | None = None
        self._events_transport: Any = None
        self._events_session: _Session | None = None
        self._events_closed: asyncio.Event | None = None
        self._events_live = False
        self._event_buf: str | None = None
        self._event_timer: asyncio.TimerHandle | None = None
        super().__init__(device_id, config, state, events)

    def _transport_kwargs(self, transport_type: str, kwargs: dict) -> dict:
        # Raw bytes: the prompt has no line ending, and Telnet option offers
        # are answered by the driver.
        kwargs["delimiter"] = None
        kwargs["frame_parser"] = None
        return kwargs

    async def _pre_connect(self) -> None:
        # Before the socket opens: the ZMP greets at once, and the greeting
        # must land on this connection's session.
        self._session = _Session()
        self._pending = None
        self._logical = None
        self._signed_in = False

    async def _post_connect(self) -> None:
        await self._sign_in(self._session, self.transport, "the API session")
        self._signed_in = True
        self._session.negotiating = False
        reply = await self._request("show server info")
        if reply.ok is not True:
            raise ConnectionError(
                f"[{self.device_id}] The ZMP did not answer 'show server info' "
                f"({reply.error or 'no Success'}); check this is the ZyPer "
                f"Management Platform's address"
            )
        self._apply_server_info(reply)

    async def _initial_sync(self) -> None:
        self._cycle = 0
        self._need_full = True
        await self.poll()
        if self.config.get("live_updates", True):
            self._events_task = asyncio.create_task(self._events_loop())
        else:
            self.set_state("live_updates", False)

    async def _close_session(self) -> None:
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(ConnectionError("connection closed"))
        self._pending = None
        self._signed_in = False
        task, self._events_task = self._events_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._close_events_transport()
        self._events_live = False
        self._change_max = None
        self._delete_max = None

    # ── Signing in ──

    async def _sign_in(self, session: _Session, transport: Any, what: str) -> None:
        """Answer the ZMP's prompts until it shows ``Zyper$``.

        The factory setting asks nothing. A Telnet password shows a password
        prompt; full authentication asks ``username:`` then ``password:``.
        A prompt shown again after the answer is a refusal.
        """
        username = str(self.config.get("username") or "").strip()
        password = str(self.config.get("password") or "")
        sent_user = sent_password = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SIGN_IN_TIMEOUT_S
        while not session.prompted:
            tail = session.partial
            if _PASS_PROMPT.search(tail):
                if sent_password:
                    raise ConnectionFaultError(
                        f"The ZMP refused the password for {what}.", code="auth_failed")
                if not password:
                    raise ConnectionFaultError(
                        "The ZMP asks for a password, and none is entered.", code="auth_failed")
                session.partial = ""
                await transport.send((password + "\r\n").encode("utf-8"))
                sent_password = True
            elif _USER_PROMPT.search(tail):
                if sent_user:
                    raise ConnectionFaultError(
                        f"The ZMP refused the username and password for {what}.",
                        code="auth_failed")
                if not username:
                    raise ConnectionFaultError(
                        "The ZMP asks for a username (full authentication), and "
                        "none is entered.", code="auth_failed")
                session.partial = ""
                await transport.send((username + "\r\n").encode("utf-8"))
                sent_user = True
            elif sent_password or sent_user:
                refusal = next((ln for ln in session.lines_seen if _REFUSAL.search(ln)), "")
                if refusal:
                    raise ConnectionFaultError(
                        f"The ZMP refused the sign-in: {refusal.strip()}", code="auth_failed")
            left = deadline - loop.time()
            if left <= 0:
                raise ConnectionError(
                    f"The ZMP did not show its Zyper$ prompt on {what} within "
                    f"{SIGN_IN_TIMEOUT_S:.0f} s; check this is the ZyPer "
                    "Management Platform and its Telnet access is enabled.")
            session.changed.clear()
            try:
                await asyncio.wait_for(session.changed.wait(), timeout=min(left, 0.5))
            except asyncio.TimeoutError:
                pass

    # ── Receiving (API session) ──

    async def on_data_received(self, data: bytes) -> None:
        # Nothing here awaits: the transport runs each chunk's handler as its
        # own task, and one that paused could let the next chunk's text in first.
        tokens, replies = self._session.feed(data)
        if replies and self.transport is not None:
            self._send_soon(self.transport, replies)
        for kind, text in tokens:
            if not self._signed_in:
                if kind == "line" and text.strip():
                    self._session.lines_seen.append(text)
                continue
            if kind == "prompt":
                self._flush_logical()
                pending = self._pending
                if pending is not None and pending.reply.ok is not None and not pending.future.done():
                    pending.future.set_result(pending.reply)
                continue
            if not text.strip():
                continue
            if _is_start(text) or self._logical is None:
                self._flush_logical()
                self._logical = text
            else:
                # The CLI wrapped this line at its width; the break is the
                # only thing it added.
                self._logical += text

    def _send_soon(self, transport: Any, data: bytes) -> None:
        task = asyncio.ensure_future(transport.send(data))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _flush_logical(self) -> None:
        line, self._logical = self._logical, None
        if line is None:
            return
        pending = self._pending
        if pending is None or pending.future.done():
            return
        reply = pending.reply
        if _OK.match(line):
            if reply.ok is None:
                reply.ok = True
            return
        m = _ERROR.match(line)
        if m:
            reply.ok = False
            reply.error = f"Error ({m.group(1)}): {m.group(2).strip()}"
            return
        m = _WARNING.match(line)
        if m:
            reply.warnings.append(m.group(2).strip())
            return
        if not reply.lines and line.strip() == reply.command:
            return  # the ZMP's echo of the command
        reply.lines.append(line)

    async def _request(self, command: str, timeout: float = REQUEST_TIMEOUT_S) -> _Reply:
        """Send one API command and wait for its Success or Error and the prompt."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._request_lock:
            stale = self._pending
            if stale is not None and not stale.future.done():
                # The last command timed out; its answer may still be coming.
                try:
                    await asyncio.wait_for(asyncio.shield(stale.future), LATE_REPLY_S)
                except asyncio.TimeoutError:
                    self._force_disconnect(
                        "no_response",
                        f"The ZMP stopped answering ('{stale.reply.command}' got no reply).")
                    raise ConnectionError(f"[{self.device_id}] The ZMP stopped answering")
                except Exception:
                    pass
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            pending = _Pending(command, fut)
            self._pending = pending
            self._logical = None
            await self.transport.send((command + "\r\n").encode("utf-8"))
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                # Left as the pending request so a late answer lands on it.
                raise ConnectionError(
                    f"[{self.device_id}] The ZMP did not answer '{command}' within {timeout:.0f} s")
            self._pending = None
            return pending.reply

    # ── Polling ──

    async def poll(self) -> None:
        self._cycle += 1
        first = self._cycle == 1
        full = (self._need_full or not self._since_works or self._change_max is None
                or self._cycle % FULL_READ_EVERY == 0)
        await self._read_config(full)
        if first or self._cycle % FULL_READ_EVERY == 0:
            await self._read_server_info()
            await self._read_walls()
            await self._read_multiviews()
            await self._read_zones()
        if not self._events_live and (first or self._cycle % STATUS_READ_EVERY == 0):
            await self._read_encoder_status()
        self._write_last_error()

    async def refresh_children(self) -> dict[str, Any]:
        self._need_full = True
        await self._read_config(True)
        await self._read_walls()
        await self._read_multiviews()
        await self._read_zones()
        return {
            "encoders": len(self.list_children("encoder")),
            "decoders": len(self.list_children("decoder")),
            "video_walls": len(self.list_children("video_wall")),
        }

    def _write_last_error(self) -> None:
        # A standing condition is written again on every poll while it holds.
        if self.config.get("live_updates", True) and not self._events_live and self._error:
            self.set_state("last_error", self._error)

    async def _read_config(self, full: bool) -> None:
        if full:
            command = "show device config all"
        else:
            command = f"show device config all since {self._change_max}"
        reply = await self._request(command, timeout=ROSTER_TIMEOUT_S)
        if reply.ok is not True:
            if not full:
                # A ZMP without "since" answers it with an error; read it whole.
                log.info(f"[{self.device_id}] '{command}' refused ({reply.error}); reading whole lists")
                self._since_works = False
                await self._read_config(True)
                return
            self.set_state("last_error", f"show device config: {reply.error}")
            return
        blocks, _loose, counters = _parse_blocks(reply.lines)
        seen = self._apply_devices(blocks)
        if full:
            self._need_full = False
            self._drop_missing(seen)
        change_max = counters.get("lastchangeidmax")
        delete_max = counters.get("lastdeleteidmax")
        if change_max is not None:
            if self._change_max is not None and change_max < self._change_max:
                self._need_full = True  # the ZMP restarted its counter
            self._change_max = change_max
        if delete_max is not None:
            if self._delete_max is not None and delete_max != self._delete_max:
                self._need_full = True  # a device was deleted
            self._delete_max = delete_max
        self._publish_roster()

    async def _read_server_info(self) -> None:
        reply = await self._request("show server info")
        if reply.ok is True:
            self._apply_server_info(reply)

    def _apply_server_info(self, reply: _Reply) -> None:
        blocks, _loose, _ = _parse_blocks(reply.lines)
        for block in blocks:
            if block.kind.lower() != "server":
                continue
            gen = block.merged("server.gen")
            lic = block.merged("server.license")
            updates: dict[str, Any] = {}
            if "version" in gen:
                updates["server_version"] = gen["version"]
            if "hostname" in gen:
                updates["server_hostname"] = gen["hostname"]
            if "serverType" in gen:
                updates["server_type"] = gen["serverType"]
            if "serialNumber" in gen:
                updates["serial_number"] = gen["serialNumber"]
            if "master" in gen:
                updates["redundancy_master"] = _yes(gen["master"])
            for key, var in (("Zyper4KLimit", "zyper4k_limit"), ("Zyper4KDevices", "zyper4k_devices")):
                if key in lic:
                    try:
                        updates[var] = int(lic[key])
                    except ValueError:
                        pass
            if "Zyper4KDevicesExceeded" in lic:
                try:
                    updates["zyper4k_exceeded"] = int(lic["Zyper4KDevicesExceeded"]) > 0
                except ValueError:
                    pass
            for key, value in updates.items():
                self.set_state(key, value)

    async def _read_walls(self) -> None:
        reply = await self._request("show videoWalls")
        if reply.ok is not True:
            return
        blocks, _loose, _ = _parse_blocks(reply.lines)
        found: dict[str, dict[str, Any]] = {}
        for block in blocks:
            if block.kind.lower() != "videowall" or not block.ident:
                continue
            gen = block.merged("videoWall.gen")
            rows: list[str] = []
            for path, values, _raw in block.records:
                if path.lower().startswith("videowall.decodersrow"):
                    rows.append(" ".join(v for _k, v in values.items() if not _is_none(v)))
            info: dict[str, Any] = {"name": block.ident, "source": self._source_from_mac(gen.get("videoSourceMac", ""))}
            for key, var in (("numDisplayRows", "rows"), ("numDisplayCols", "columns")):
                try:
                    info[var] = int(gen[key])
                except (KeyError, ValueError):
                    pass
            info["decoders"] = "; ".join(r for r in rows if r)
            found[block.ident] = info
        current = {cid: name for cid, name in self._walls.items()}
        by_name = {name: cid for cid, name in current.items()}
        keep: set[str] = set()
        for name, info in found.items():
            cid = by_name.get(name)
            if cid is None:
                cid = self._unique_id("video_wall", name)
                self.register_child("video_wall", cid, initial_state={"name": name})
                self._walls[cid] = name
            keep.add(cid)
            self.set_child_state_batch("video_wall", cid, info)
        for cid in list(self._walls):
            if cid not in keep:
                self._walls.pop(cid, None)
                self.deregister_child("video_wall", cid)
        self.set_state("video_wall_options", json.dumps(
            [{"value": name, "label": name} for name in sorted(found)]))

    async def _read_multiviews(self) -> None:
        reply = await self._request("show multiviews config")
        names: list[str] = []
        if reply.ok is True:
            blocks, _loose, _ = _parse_blocks(reply.lines)
            names = sorted({b.ident for b in blocks if b.kind.lower() == "multiview" and b.ident})
        self.set_state("multiview_options", json.dumps([{"value": n, "label": n} for n in names]))

    async def _read_zones(self) -> None:
        reply = await self._request("show zones")
        names: list[str] = []
        if reply.ok is True:
            _blocks, loose, _ = _parse_blocks(reply.lines)
            for block in loose:
                names.extend(path for path, _values, _raw in block.records)
        self.set_state("zone_options", json.dumps([{"value": n, "label": n} for n in sorted(set(names))]))

    async def _read_encoder_status(self) -> None:
        reply = await self._request("show device status encoders", timeout=ROSTER_TIMEOUT_S)
        if reply.ok is not True:
            return
        blocks, _loose, _ = _parse_blocks(reply.lines)
        for block in blocks:
            unit = self._units.get(_norm_mac(block.ident))
            if block.kind.lower() != "device" or unit is None or unit["ctype"] != "encoder":
                continue
            gen = block.merged("device.gen")
            hdmi = block.merged("device.hdmiInput")
            updates: dict[str, Any] = {}
            if "state" in gen:
                updates["online"] = _yes(gen["state"])
            if "cableConnected" in hdmi:
                updates["cable_connected"] = _yes(hdmi["cableConnected"])
            if "horizontalSize" in hdmi and "verticalSize" in hdmi:
                updates["resolution"] = f"{hdmi['horizontalSize']}x{hdmi['verticalSize']}"
            if "fps" in hdmi:
                updates["frame_rate"] = hdmi["fps"]
            if "hdcpVersion" in hdmi:
                updates["hdcp_version"] = hdmi["hdcpVersion"]
            if updates:
                self.set_child_state_batch("encoder", unit["cid"], updates)

    # ── Roster ──

    def _apply_devices(self, blocks: list[_Block]) -> set[str]:
        seen: set[str] = set()
        decoders: list[tuple[str, _Block]] = []
        for block in blocks:
            if block.kind.lower() != "device":
                continue
            mac = _norm_mac(block.ident)
            gen = block.merged("device.gen")
            ctype = str(gen.get("type", "")).strip().lower()
            name = str(gen.get("name", "")).strip()
            if ctype not in ("encoder", "decoder") or not name:
                continue
            seen.add(mac)
            unit = self._ensure_unit(mac, ctype, name, block.ident.strip())
            updates: dict[str, Any] = {"name": name, "mac": mac}
            if "model" in gen:
                updates["model"] = gen["model"]
            if "firmware" in gen:
                updates["firmware"] = gen["firmware"]
            if "state" in gen:
                updates["online"] = _yes(gen["state"])
            ip = block.merged("device.ip").get("address", "")
            if ip and not _is_none(ip):
                updates["ip"] = ip
            port = block.merged("device.ports").get("videoPort", "")
            if ctype == "encoder" and port:
                updates["video_port"] = port
            self.set_child_state_batch(ctype, unit["cid"], updates)
            if ctype == "decoder":
                decoders.append((unit["cid"], block))
        # Sources name encoders, which may come later in the same reply.
        for cid, block in decoders:
            self._apply_decoder_routes(cid, block)
        return seen

    def _apply_decoder_routes(self, cid: str, block: _Block) -> None:
        conn = block.merged("device.connectedEncoder")
        audio = block.merged("device.audioConnections")
        auto = block.merged("device.autoAudioConnections")
        updates: dict[str, Any] = {}
        if conn:
            updates["source"] = self._source_from(conn.get("macAddr", ""), conn.get("name", ""))
            mode = conn.get("connectionMode", "")
            updates["connection_mode"] = "" if _is_none(mode) else mode
        if audio:
            updates["hdmi_audio_source"] = self._source_from(
                audio.get("hdmiAudioSourceMac", ""), audio.get("hdmiAudioSourceName", ""))
            updates["analog_audio_source"] = self._source_from(
                audio.get("analogSourceMac", ""), audio.get("analogSourceName", ""))
        if "hdmiAudioFollowVideo" in auto:
            updates["audio_follows_video"] = _yes(auto["hdmiAudioFollowVideo"])
        if updates:
            self.set_child_state_batch("decoder", cid, updates)

    def _source_from(self, mac: str, name: str) -> str:
        """An encoder's child id from the MAC (or name) the ZMP reports; a
        name it does not know (a multiview) as it is; empty for none."""
        if not _is_none(mac):
            unit = self._units.get(_norm_mac(mac))
            if unit is not None and unit["ctype"] == "encoder":
                return unit["cid"]
        if _is_none(name):
            return ""
        unit = self._units.get(self._mac_by_name.get(name, ""))
        if unit is not None and unit["ctype"] == "encoder":
            return unit["cid"]
        return name

    def _source_from_mac(self, mac: str) -> str:
        return self._source_from(mac, "")

    def _ensure_unit(self, mac: str, ctype: str, name: str, wire: str) -> dict[str, Any]:
        unit = self._units.get(mac)
        if unit is not None and (unit["ctype"] != ctype or unit["name"] != name):
            # Renamed (or re-typed) in the ZMP: the child follows the name.
            self._forget(mac)
            unit = None
        if unit is None:
            cid = self._unique_id(ctype, name)
            self.register_child(ctype, cid, initial_state={"name": name, "mac": mac})
            unit = {"ctype": ctype, "cid": cid, "name": name, "wire": wire}
            self._units[mac] = unit
            self._child_mac[(ctype, cid)] = mac
            self._mac_by_name[name] = mac
        else:
            unit["wire"] = wire
        return unit

    def _forget(self, mac: str) -> None:
        unit = self._units.pop(mac, None)
        if unit is None:
            return
        self._child_mac.pop((unit["ctype"], unit["cid"]), None)
        if self._mac_by_name.get(unit["name"]) == mac:
            self._mac_by_name.pop(unit["name"], None)
        self.deregister_child(unit["ctype"], unit["cid"])

    def _drop_missing(self, seen: set[str]) -> None:
        for mac in list(self._units):
            if mac not in seen:
                self._forget(mac)

    def _unique_id(self, ctype: str, name: str) -> str:
        base = _sanitize_id(name)
        taken = {str(c) for c in self.list_children(ctype)}
        cid, n = base, 2
        while cid in taken:
            cid = f"{base[:36]}-{n}"
            n += 1
        return cid

    def _publish_roster(self) -> None:
        counts = {"encoder": [0, 0], "decoder": [0, 0]}
        options = []
        for unit in self._units.values():
            counts[unit["ctype"]][0] += 1
            if self.get_child_state(unit["ctype"], unit["cid"]).get("online"):
                counts[unit["ctype"]][1] += 1
            options.append({"value": unit["name"],
                            "label": f"{unit['name']} ({'Encoder' if unit['ctype'] == 'encoder' else 'Decoder'})"})
        self.set_state("encoder_count", counts["encoder"][0])
        self.set_state("encoders_online", counts["encoder"][1])
        self.set_state("decoder_count", counts["decoder"][0])
        self.set_state("decoders_online", counts["decoder"][1])
        self.set_state("endpoint_options", json.dumps(sorted(options, key=lambda o: o["label"].lower())))

    # ── Events session ──

    async def _events_loop(self) -> None:
        attempt = 0
        while True:
            try:
                await self._open_events()
                attempt = 0
                self._events_live = True
                self._error = ""
                self.set_state("live_updates", True)
                self.set_state("last_error", "")
                assert self._events_closed is not None
                await self._events_closed.wait()
                log.info(f"[{self.device_id}] Live updates session closed; reopening")
            except asyncio.CancelledError:
                raise
            except ConnectionFaultError as exc:
                self._error = f"Live updates: {exc}"
                log.warning(f"[{self.device_id}] {self._error}")
            except Exception as exc:  # the API session carries on without it
                self._error = f"Live updates could not open: {exc}"
                log.warning(f"[{self.device_id}] {self._error}")
            self._events_live = False
            self.set_state("live_updates", False)
            if self._error:
                self.set_state("last_error", self._error)
            await self._close_events_transport()
            await asyncio.sleep(EVENTS_RETRY_S[min(attempt, len(EVENTS_RETRY_S) - 1)])
            attempt += 1

    async def _open_events(self) -> None:
        from openavc.transport.tcp import TCPTransport

        host = self.config.get("host", "")
        port = int(self.config.get("port", 23))
        local = getattr(self, "local_address", None)
        ip = local() if callable(local) else None
        self._events_session = _Session()
        self._events_closed = asyncio.Event()
        self._event_buf = None
        self._events_transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self._on_events_data,
            on_disconnect=self._on_events_disconnect,
            delimiter=None,
            name=self.device_id,
            local_addr=(ip, 0) if ip else None,
            timeout=float(self.config.get("timeout", 5.0)),
            keepalive=True,
        )
        await self._sign_in(self._events_session, self._events_transport, "the live updates session")
        # From here on nothing more is sent: any byte ends events mode.
        self._events_session.negotiating = False
        self._events_session.prompted = False
        await self._events_transport.send(b"events\r\n")

    async def _close_events_transport(self) -> None:
        transport, self._events_transport = self._events_transport, None
        if self._event_timer is not None:
            self._event_timer.cancel()
            self._event_timer = None
        if transport is not None:
            try:
                await transport.close()
            except Exception:
                pass

    def _on_events_disconnect(self) -> None:
        if self._events_closed is not None:
            self._events_closed.set()

    async def _on_events_data(self, data: bytes) -> None:
        session = self._events_session
        if session is None:
            return
        tokens, replies = session.feed(data)
        if replies and self._events_transport is not None:
            self._send_soon(self._events_transport, replies)
        for kind, text in tokens:
            if kind != "line":
                continue
            if not session.negotiating and _EVENT.match(text):
                self._flush_event()
                self._event_buf = text.strip()
            elif self._event_buf is not None and text:
                self._event_buf += text  # an event the CLI wrapped
            elif session.negotiating and text.strip():
                session.lines_seen.append(text)
        if self._event_buf is not None:
            if self._event_timer is not None:
                self._event_timer.cancel()
            self._event_timer = asyncio.get_running_loop().call_later(
                EVENT_SETTLE_S, self._flush_event)

    def _flush_event(self) -> None:
        line, self._event_buf = self._event_buf, None
        if self._event_timer is not None:
            self._event_timer.cancel()
            self._event_timer = None
        if line:
            try:
                self._apply_event(line)
            except Exception:
                log.exception(f"[{self.device_id}] Could not read event: {line!r}")

    def _apply_event(self, line: str) -> None:
        """``Event::<name>::<source>::<date>::<lastChangeId>::: <message>``
        (ZMP User Manual 4.0.3, section 4)."""
        parts = line.strip().split("::", 5)
        if len(parts) < 6:
            return
        name = parts[1].strip().lower()
        source = parts[2].strip()
        message = parts[5].lstrip(":").strip()
        m = _SOURCE.match(source)
        unit = self._units.get(_norm_mac(m.group(2))) if m else None
        if unit is None:
            if m and name in ("devicestatus", "devicestate", "devicestatechange"):
                self._need_full = True  # a unit the roster has not read yet
            return
        values = _parse_kv(message)
        ctype, cid = unit["ctype"], unit["cid"]
        updates: dict[str, Any] = {}
        if name in ("devicestatus", "devicestate", "devicestatechange"):
            if "state" in values:
                updates["online"] = _yes(_after_arrow(values["state"]))
        if name in ("devicestatus", "devicestate", "cableconnection"):
            if "cable" in values:
                updates["cable_connected"] = _yes(_after_arrow(values["cable"]))
        if name in ("devicestatus", "devicestate", "resolutionchange"):
            if "sizeX" in values and "sizeY" in values:
                updates["resolution"] = f"{_after_arrow(values['sizeX'])}x{_after_arrow(values['sizeY'])}"
            if "fps" in values:
                updates["frame_rate"] = _after_arrow(values["fps"])
        if name in ("devicestatus", "devicestate", "videostatuschange") and ctype == "decoder":
            status = values.get("receivingVideoFromEncoder")
            if status:
                status = status.strip().lower()
                updates["video_status"] = status if status in ("yes", "no", "yes-with-warning") else "no"
                updates["video_status_detail"] = values.get("reason") or values.get("warning") or ""
        if name == "videochanged" and ctype == "encoder" and "hdcpVersion" in values:
            updates["hdcp_version"] = _after_arrow(values["hdcpVersion"])
        if name == "rs232data" and "data" in values:
            updates["rs232_data"] = values["data"]
        if name == "irdata" and "data" in values:
            updates["ir_data"] = values["data"].strip('\\"')
        if updates:
            self.set_child_state_batch(ctype, cid, updates)
            if "online" in updates:
                self._publish_roster()

    # ── Commands ──

    def _mac_of(self, ctype: str, cid: Any) -> str:
        mac = self._child_mac.get((ctype, str(cid).strip()))
        if mac is None:
            raise ValueError(
                f"Unknown {ctype} {cid!r}. Pick one from the list (Refresh from "
                "Device reads the ZMP's list again).")
        return mac

    def _wire(self, ctype: str, cid: Any) -> str:
        return self._units[self._mac_of(ctype, cid)]["wire"]

    def _endpoint_wire(self, value: Any) -> str:
        name = str(value or "").strip()
        mac = self._mac_by_name.get(name)
        if mac is None:
            for (ctype, cid), m in self._child_mac.items():
                if cid == name.lower():
                    mac = m
                    break
        if mac is None:
            raise ValueError(f"Unknown endpoint {name!r}. Pick one from the list.")
        return self._units[mac]["wire"]

    def _decoder_mode(self, cid: str, wanted: Any) -> str:
        if wanted and str(wanted) in JOIN_MODES:
            return str(wanted)
        current = self.get_child_state("decoder", cid).get("connection_mode")
        return str(current) if current in JOIN_MODES else "fastSwitched"

    async def _read_unit(self, ctype: str, cid: str) -> None:
        """Read one endpoint's config again, addressed by its MAC (a name
        would also match every unit whose name it begins)."""
        reply = await self._request(f"show device config {self._wire(ctype, cid)}")
        if reply.ok is True:
            self._apply_devices(_parse_blocks(reply.lines)[0])

    async def _ok(self, command: str) -> _Reply:
        reply = await self._request(command)
        if reply.ok is not True:
            raise CommandRefusedError(f"The ZMP refused '{command}': {reply.error or 'no Success'}")
        return reply

    async def _readback(self, ctype: str, cid: str, key: str, expected: Any, what: str) -> None:
        """Read the unit back after a change; refuse one the ZMP accepted and
        does not report (the API answers "Success" and nothing more)."""
        reported: Any = None
        for attempt in range(2):
            await self._read_unit(ctype, cid)
            reported = self.get_child_state(ctype, cid).get(key)
            if isinstance(expected, bool):
                if bool(reported) == expected:
                    return
            elif str(reported or "") == expected:
                return
            if attempt == 0:
                await asyncio.sleep(READBACK_RETRY_S)
        shown = reported if reported not in (None, "") else "none"
        raise CommandRefusedError(
            f"The ZMP accepted {what} but {ctype} {cid} still reports {shown!r}.")

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if command == "route":
            enc, dec = str(params["encoder"]), str(params["decoder"])
            mode = self._decoder_mode(dec, params.get("mode"))
            await self._ok(f"join {self._wire('encoder', enc)} {self._wire('decoder', dec)} {mode}")
            await self._readback("decoder", dec, "source", enc, f"the route to {dec}")
            return True
        if command == "route_off":
            dec = str(params["decoder"])
            mode = self._decoder_mode(dec, None)
            await self._ok(f"join none {self._wire('decoder', dec)} {mode}")
            await self._readback("decoder", dec, "source", "", f"clearing {dec}")
            return True
        if command in ("route_hdmi_audio", "route_analog_audio"):
            enc, dec = str(params["encoder"]), str(params["decoder"])
            kind, key = (("hdmiAudio", "hdmi_audio_source") if command == "route_hdmi_audio"
                         else ("analogAudio", "analog_audio_source"))
            await self._ok(f"join {self._wire('encoder', enc)} {self._wire('decoder', dec)} {kind}")
            await self._readback("decoder", dec, key, enc, f"the audio route to {dec}")
            return True
        if command in ("route_hdmi_audio_off", "route_analog_audio_off"):
            dec = str(params["decoder"])
            kind, key = (("hdmiAudio", "hdmi_audio_source") if command == "route_hdmi_audio_off"
                         else ("analogAudio", "analog_audio_source"))
            await self._ok(f"join none {self._wire('decoder', dec)} {kind}")
            await self._readback("decoder", dec, key, "", f"clearing the audio on {dec}")
            return True
        if command == "set_audio_follow_video":
            dec = str(params["decoder"])
            on = params.get("enabled") in (True, "true", "True", 1, "1", "on")
            await self._ok(f"set decoder {self._wire('decoder', dec)} autoAudioConnections "
                           f"hdmiAudioFollowVideo {'enabled' if on else 'disabled'}")
            await self._readback("decoder", dec, "audio_follows_video", on, "the audio setting")
            return True
        if command == "route_usb":
            # Read back by the next roster read: the API documents no field
            # that reports a USB join.
            await self._ok(f"join {self._wire('encoder', params['encoder'])} "
                           f"{self._wire('decoder', params['decoder'])} usb")
            return True
        if command == "route_usb_off":
            await self._ok(f"join none {self._wire('decoder', params['decoder'])} usb")
            return True
        if command == "route_video_wall":
            enc, wall_id = str(params["encoder"]), str(params["video_wall"])
            wall = self._walls.get(wall_id)
            if wall is None:
                raise ValueError(f"Unknown video wall {wall_id!r}. Pick one from the list.")
            await self._ok(f"join {self._wire('encoder', enc)} {wall} videoWall")
            reported: Any = None
            for attempt in range(2):
                await self._read_walls()
                reported = self.get_child_state("video_wall", wall_id).get("source")
                if str(reported or "") == enc:
                    # The wall's decoders changed source too.
                    await self._read_config(self._change_max is None)
                    return True
                if attempt == 0:
                    await asyncio.sleep(READBACK_RETRY_S)
            raise CommandRefusedError(
                f"The ZMP accepted the route to {wall} but the wall still shows {(reported or 'none')!r}.")
        if command == "route_zone":
            zone = str(params["zone"]).strip()
            if not zone or " " in zone:
                raise ValueError("Pick a zone from the list.")
            mode = str(params.get("mode") or "fastSwitched")
            if mode not in JOIN_MODES:
                mode = "fastSwitched"
            await self._ok(f"join {self._wire('encoder', params['encoder'])} {zone} {mode}")
            self._need_full = True
            await self._read_config(True)
            return True
        if command == "show_multiview":
            mv = str(params["multiview"]).strip()
            dec = str(params["decoder"])
            if not mv or " " in mv:
                raise ValueError("Pick a multiview from the list.")
            await self._ok(f"join {mv} {self._wire('decoder', dec)} multiview")
            await self._read_unit("decoder", dec)
            return True
        if command == "set_multiview_window":
            mv = str(params["multiview"]).strip()
            if not mv or " " in mv:
                raise ValueError("Pick a multiview from the list.")
            window = int(params["window"])
            enc = params.get("encoder")
            target = self._wire("encoder", enc) if enc not in (None, "") else "none"
            await self._ok(f"set multiview {mv} windowNumber {window} newEncoderName {target}")
            return True
        if command in ("channel_up", "channel_down"):
            dec = str(params["decoder"])
            direction = "up" if command == "channel_up" else "down"
            await self._ok(f"channel {direction} {self._wire('decoder', dec)}")
            await self._read_unit("decoder", dec)
            return True
        if command == "run_preset":
            preset = str(params["preset"]).strip()
            if not preset:
                raise ValueError("Enter the preset's name.")
            quoted = f'"{preset}"' if " " in preset else preset
            await self._ok(f"run preset {quoted}")
            self._need_full = True
            await self._read_config(True)
            return True
        if command == "select_video_port":
            enc = str(params["encoder"])
            port = str(params["port"])
            if port not in VIDEO_PORTS:
                raise ValueError(f"Input must be one of {', '.join(VIDEO_PORTS)}.")
            await self._ok(f"set device {self._wire('encoder', enc)} videoPort {port}")
            await self._readback("encoder", enc, "video_port", port, "the input choice")
            return True
        if command == "send_rs232":
            data = str(params["data"])
            if '"' in data:
                raise ValueError("RS-232 data cannot contain a double quote.")
            # Quoted, so spaces go through (Configuration Tips, p. 282-283).
            await self._ok(f'send {self._endpoint_wire(params["endpoint"])} rs232 "{data}"')
            return True
        if command == "send_ir":
            code = re.sub(r"\s+", "", str(params["code"]))
            await self._ok(f"send {self._endpoint_wire(params['endpoint'])} ir {code}")
            return True
        if command == "send_cec":
            action = str(params["action"]).lower()
            if action not in ("on", "off"):
                raise ValueError("Power must be on or off.")
            await self._ok(f"send {self._endpoint_wire(params['endpoint'])} cec {action}")
            return True
        if command == "send_cec_hex":
            await self._ok(f"send {self._endpoint_wire(params['endpoint'])} cec {params['hex']}")
            return True
        if command == "link_rs232":
            await self._ok(f"switch {self._endpoint_wire(params['endpoint'])} server rs232")
            return True
        if command == "identify":
            await self._ok(f"flashLeds {self._endpoint_wire(params['endpoint'])}")
            return True
        if command == "restart_endpoint":
            await self._ok(f"restart device {self._endpoint_wire(params['endpoint'])}")
            return True
        if command == "refresh":
            await self.refresh_children()
            await self._read_server_info()
            return True
        if command == "api_command":
            text = str(params["command"]).strip()
            if not text:
                raise ValueError("Enter a command.")
            if text.lower() == "events" or text.lower().startswith("events "):
                raise ValueError("'events' would stop this session answering; Live Updates uses it already.")
            reply = await self._request(text, timeout=ROSTER_TIMEOUT_S)
            body = "\n".join(reply.lines + [f"Warning: {w}" for w in reply.warnings])
            if reply.ok is not True:
                raise CommandRefusedError(f"{reply.error or 'No Success'}\n{body}".strip())
            return body or "Success"
        raise ValueError(f"Unknown command: {command}")
