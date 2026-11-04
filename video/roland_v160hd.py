"""
OpenAVC Roland V-160HD / V-80HD / VR-120HD / VR-6HD / V-1-4K driver.

Controls Roland's current video switchers and streaming AV mixers over the
LAN/RS-232 Basic Control Commands, an ASCII protocol shared by all five
models: ``CODE:parameter,parameter;``. Source: "LAN/RS-232 Basic Control
Commands" (Roland, eng02, April 2026), with each model's Remote Control Guide
for the connection (V-160HD eng06, V-80HD eng01, VR-120HD eng04, VR-6HD eng03,
V-1-4K eng01) and Roland's support pages "V-160HD: LAN and RS-232 Control
Tips", "Professional A/V: LAN and RS-232 Control Tips and Documentation" and
"Professional A/V: Bitfocus Companion Compatibility" for the network password.

Why Python:
- What the switcher has depends on the model it reports to ``VER;``: XLR1-2 or
  XLR1-6, HDMI1-8 or HDMI1-5, AUX1-3 or none, 30, 32 or 8 scene memories. Each
  roster (audio inputs, outputs, AUX buses, keyers, cameras) and each picker's
  list of sources is the reported model's own. A YAML roster follows a config
  field or an integer count, not a list chosen by a reported model.
- A query is answered by its value and then ``ACK;``; a set by ``ACK;`` or
  ``ERR:n;``. Requests go one at a time, so a refusal belongs to the command
  that drew it and Send Command or a macro reports it, where a YAML command
  sends and forgets.
- Two poll speeds: what an operator changes during a show (program and
  preset, keyers on air, AUX buses, fade to black) is read every poll, and the
  rest a few at a time around it. A YAML poll reads everything every cycle,
  and the full list on a V-160HD is about 250 queries.
- On RS-232 the V-160HD, VR-120HD and VR-6HD need STX (0x02) before every
  command (their guides list ERR:6 "no stx error", RS-232 only); on the LAN
  it may be left off, and the V-80HD and V-1-4K guides never show it. One
  driver serves both connections with the framing each needs.

Push and poll: the document lists nothing the switcher sends on its own except
``ACK;`` and ``ERR:n;`` answering a command, so everything is polled.

Wire rules, as built (marked unverified where no document shows it):
- Commands end in ``;``; the driver sends a line feed after it, as Roland's
  support pages say the units take (unverified on a unit).
- Replies end in ``;`` followed by a line feed, and the model guides show STX
  before each one (``stxACK;``). The driver frames on ``;`` and strips STX,
  CR, LF and XON/XOFF around each message, so it reads every placement of
  those bytes the documents allow.
- LAN: TCP 8023. "A password is required to connect" (Roland's support page)
  for the V-160HD, VR-120HD and VR-6HD: a four-character network password set
  in the switcher's LAN menu, eight characters on the V-80HD and V-1-4K. No
  document shows the exchange. Roland's support pages send the password and a
  line feed before the commands, so the driver sends it once, first, on each
  connection, then asks ``VER;``. What the unit answers to the password line,
  and to a wrong one, is not documented: a switcher that stays silent reads as
  not answering, never as a refused password. A blank password sends no
  password line.
- RS-232 has no password. 9600, 38400 or 115200 baud, 8N1 (115200 is the
  V-160HD's default); RS-232 must be ON in the switcher's menu.

Levels: the document gives audio levels as -INF or -800..100 (-80.0..+10.0
dB). The driver reports -INF as -81 dB and sends -INF for any level below
-80.0, so one fader covers the whole range.
"""

from __future__ import annotations

import asyncio
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

DEFAULT_PORT = 8023
REQUEST_TIMEOUT_S = 3.0
VERSION_TIMEOUT_S = 5.0
# A reply that arrives after its request timed out is waited for this long
# before the next request goes out; past it the connection is reopened, since
# every later reply would answer the wrong question.
LATE_REPLY_S = 2.0
# After the network password line, time for the switcher to say anything it
# says to it before the first command goes out.
SIGN_IN_SETTLE_S = 0.3
# Whether an ACK follows a query's value is read off the first query of each
# connection (the documents show it both ways); after that it is waited for
# this long.
ACK_FIRST_WAIT_S = 0.5
ACK_WAIT_S = 1.0
# Bytes with no ";" in them past this are not the protocol; the buffer is dropped.
RX_LIMIT = 64 * 1024
# A poll reads its fast list, then this many slow reads, fewer once this share
# of the poll interval has gone (a slow link): about 22 queries a second on a
# V-160HD at the default interval. Of the slow reads, OPERATIONAL_SHARE
# operational ones go per setup one, so the operational list (levels, mutes,
# signal) comes round in about 25 s and the setup list in about 75 s.
SLOW_READS = 6
POLL_BUDGET = 0.6
OPERATIONAL_SHARE = 2

INF_DB = -81.0
LEVEL_MIN_DB = -80.0
LEVEL_MAX_DB = 10.0

STX = "\x02"
_STRIP = "\x02\x11\x13\r\n \t"
_TOKEN = re.compile(r"^([A-Z][A-Z0-9]*)(?::(.*))?$")

ERR_WORDS = {
    "0": "syntax error: the switcher does not take the command",
    "4": "no effect: another setting controls it",
    "5": "a value is out of range",
    "6": "no STX at the start of the command",
}

# Gets the document shows answered with no ACK after the value.
NO_TRAILING_ACK = frozenset({"BSY", "SRTIST", "GRCH", "AMCH", "SPCH", "AUXCH", "QAUXCH"})
# Query code -> reply code, where the reply is not the query without its Q.
REPLY_CODE = {
    "QCAMPTS": "CPTS",
    "QCAMAFC": "CAFC",
    "QCAMAEP": "CAEP",
    "QCAMPR": "CPR",
    "QSEQSW": "SQS",
    "QSEQAS": "SQA",
    "QHDCP": "HCP",
    "QAUXLV": "QAUXLV",
    "STRSTB": "STRSTB",
    "VPSTB": "VPSTB",
    "SRTISTB": "SRTISTB",
    "VER": "VER",
}

MODEL_NAMES = ("V-160HD", "V-80HD", "VR-120HD", "VR-6HD", "V-1-4K")


def _seq(prefix: str, last: int, first: int = 1) -> list[str]:
    return [f"{prefix}{n}" for n in range(first, last + 1)]


_ALL_PATTERNS = [
    "OFF", "COLORBAR75", "COLORBAR100", "RAMP", "STEP", "HATCH", "DIAMOND", "CIRCLE",
    "COLORBAR75-SP", "COLORBAR100-SP", "RAMP-SP", "STEP-SP", "HATCH-SP",
]


def _model(
    *, hdmi: list[str], sdi: list[str], stills: int, players: bool, channels: int,
    assign_extra: list[str], video_outputs: list[str], output_assign: list[str],
    aux: int, pinp: int, pinp_source: bool, dsk: int, split: int, freeze: bool,
    still_output: bool, roi: int, audio_inputs: list[str], stereo_link: list[str],
    voice_changer: list[str], audio_outputs: list[str], audio_jacks: list[str],
    jack_assign: list[str], reverb: bool, memories: int, gpo: int, cameras: int,
    patterns: list[str], streaming: bool, pads: list[str], macros: bool,
    fader_max: int, pinp_time: bool, dsk_time_query: str | None,
) -> dict[str, Any]:
    stills_list = _seq("STILL", stills)
    player = ["V.PLAYER", "SRT"] if players else []
    physical = hdmi + sdi + stills_list + player
    channel_ids = _seq("INPUT", channels)
    sources = physical + channel_ids
    targets = ["MIX", "WIPE"]
    if pinp_time:
        targets += _seq("PinP", pinp)
    targets += _seq("DSK", dsk) + ["OUTPUTFADE"]
    return {
        "video_inputs": hdmi + sdi,
        "sources": sources,
        "aux_sources": sources + ["PGMLINK"] if aux else [],
        "channels": channels,
        "input_assign": assign_extra,
        "video_outputs": video_outputs,
        "output_assign": output_assign,
        "aux": aux,
        "pinp": pinp,
        "pinp_source": pinp_source,
        "dsk": dsk,
        "split": split,
        "freeze": freeze,
        "still_output": (["OFF"] + stills_list + player) if still_output else [],
        "roi": roi,
        "audio_inputs": audio_inputs,
        "stereo_link": stereo_link,
        "voice_changer": voice_changer,
        "audio_outputs": audio_outputs,
        "audio_jacks": audio_jacks,
        "jack_assign": jack_assign,
        "reverb": reverb,
        "memories": memories,
        "gpo": gpo,
        "cameras": cameras,
        "patterns": patterns,
        "streaming": streaming,
        "pads": pads,
        "macros": macros,
        "fader_max": fader_max,
        "transition_targets": targets,
        "dsk_time_query": dsk_time_query,
    }


def _pads(banks: str, per_bank: int) -> list[str]:
    return [f"{bank}-{n}" for bank in banks for n in range(1, per_bank + 1)]


# Per model, from the Basic Control Commands' parameter tables.
MODELS: dict[str, dict[str, Any]] = {
    "V-160HD": _model(
        hdmi=_seq("HDMI", 8), sdi=_seq("SDI", 8), stills=16, players=False, channels=20,
        assign_extra=_seq("HDMI", 8) + _seq("SDI", 8) + _seq("STILL", 16) + ["N/A"],
        video_outputs=_seq("HDMI", 3) + _seq("SDI", 3) + ["USB"],
        output_assign=["PGM", "SUB", "PVW"] + _seq("AUX", 3) + _seq("DSK", 2)
        + ["MULTIVIEW", "INPUTVIEW1", "STILLVIEW"],
        aux=3, pinp=4, pinp_source=True, dsk=2, split=2, freeze=True, still_output=True,
        roi=0,
        audio_inputs=_seq("XLR", 2) + ["RCA1", "USB", "BLUETOOTH"] + _seq("HDMI", 8) + _seq("SDI", 8),
        stereo_link=_seq("XLR", 2), voice_changer=_seq("XLR", 2),
        audio_outputs=["MAIN"] + _seq("AUX", 3) + ["USB"],
        audio_jacks=_seq("HDMI", 3) + _seq("SDI", 3) + ["XLR1", "RCA1", "USB", "PHONES"],
        jack_assign=["AUTO", "MAIN"] + _seq("AUX", 3),
        reverb=True, memories=30, gpo=16, cameras=16, patterns=_ALL_PATTERNS,
        streaming=False, pads=[], macros=True, fader_max=2047, pinp_time=True,
        dsk_time_query=None,
    ),
    "V-80HD": _model(
        hdmi=_seq("HDMI", 4), sdi=_seq("SDI", 4), stills=32, players=True, channels=16,
        assign_extra=_seq("HDMI", 4) + _seq("SDI", 4) + _seq("STILL", 32)
        + ["V.PLAYER", "SRT", "STATUS1", "STATUS2", "TIME", "N/A"],
        video_outputs=_seq("HDMI", 3) + _seq("SDI", 2) + ["USB", "STREAM", "LCD"],
        output_assign=["PGM", "SUB", "PVW"] + _seq("AUX", 2) + ["DSK1", "MULTIVIEW"]
        + _seq("INPUTVIEW", 2) + ["QUADVIEW"],
        aux=2, pinp=2, pinp_source=True, dsk=1, split=2, freeze=True, still_output=True,
        roi=0,
        audio_inputs=_seq("XLR", 2) + ["RCA1", "USB", "BLUETOOTH", "A.PLAYER"]
        + _seq("HDMI", 4) + _seq("SDI", 4) + ["V.PLAYER", "SRT"],
        stereo_link=_seq("XLR", 2), voice_changer=_seq("XLR", 2),
        audio_outputs=["MAIN"] + _seq("AUX", 2) + ["USB", "STREAM", "REC"],
        audio_jacks=_seq("HDMI", 3) + _seq("SDI", 2) + ["XLR1", "USB", "STREAM", "REC", "PHONES"],
        jack_assign=["AUTO", "MAIN"] + _seq("AUX", 2) + ["MONITOR"],
        reverb=True, memories=32, gpo=16, cameras=8, patterns=_ALL_PATTERNS,
        streaming=True, pads=_pads("ABCDEFGH", 8), macros=True, fader_max=2047,
        pinp_time=True, dsk_time_query=None,
    ),
    "VR-120HD": _model(
        hdmi=_seq("HDMI", 6), sdi=_seq("SDI", 6), stills=16, players=True, channels=8,
        assign_extra=_seq("HDMI", 6) + _seq("SDI", 6) + _seq("STILL", 16)
        + ["V.PLAYER", "SRT", "STATUS1", "STATUS2", "TIME", "N/A"],
        video_outputs=_seq("HDMI", 3) + _seq("SDI", 3) + ["USB", "STREAM", "LCD"],
        output_assign=["PGM", "SUB", "PVW"] + _seq("AUX", 3) + _seq("DSK", 2)
        + ["MULTIVIEW", "INPUTVIEW1", "STILLVIEW"],
        aux=3, pinp=4, pinp_source=True, dsk=2, split=2, freeze=True, still_output=True,
        roi=0,
        audio_inputs=_seq("XLR", 6) + ["RCA1", "RCA2", "USB", "BLUETOOTH", "A.PLAYER"]
        + _seq("HDMI", 6) + _seq("SDI", 6) + ["V.PLAYER", "SRT"],
        stereo_link=_seq("XLR", 6), voice_changer=_seq("XLR", 2),
        audio_outputs=["MAIN"] + _seq("AUX", 3) + ["USB", "STREAM", "REC"],
        audio_jacks=_seq("HDMI", 3) + _seq("SDI", 3)
        + ["XLR1", "XLR2", "RCA1", "USB", "STREAM", "REC", "PHONES"],
        jack_assign=["MAIN"] + _seq("AUX", 3) + ["MONITOR"],
        reverb=True, memories=32, gpo=16, cameras=12, patterns=_ALL_PATTERNS,
        streaming=True, pads=_pads("AB", 8), macros=True, fader_max=2047,
        pinp_time=True, dsk_time_query=None,
    ),
    "VR-6HD": _model(
        hdmi=_seq("HDMI", 6), sdi=[], stills=16, players=True, channels=6,
        assign_extra=_seq("HDMI", 6) + _seq("STILL", 16) + ["V.PLAYER", "SRT", "N/A"],
        video_outputs=_seq("HDMI", 3) + ["USB", "STREAM", "LCD"],
        output_assign=["PGM", "SUB", "PVW", "AUX1", "DSK1", "MULTIVIEW", "STILLVIEW"],
        aux=1, pinp=2, pinp_source=True, dsk=1, split=1, freeze=True, still_output=True,
        roi=0,
        audio_inputs=_seq("XLR", 6) + ["RCA1", "USB", "BLUETOOTH", "A.PLAYER"]
        + _seq("HDMI", 6) + ["V.PLAYER", "SRT"],
        stereo_link=_seq("XLR", 6), voice_changer=_seq("XLR", 2),
        audio_outputs=["MAIN"] + _seq("AUX", 2) + ["USB", "STREAM", "REC"],
        audio_jacks=_seq("HDMI", 3) + ["XLR1", "RCA1", "USB", "STREAM", "REC", "PHONES"],
        jack_assign=["MAIN"] + _seq("AUX", 2) + ["MONITOR"],
        reverb=True, memories=32, gpo=16, cameras=6, patterns=_ALL_PATTERNS,
        streaming=True, pads=_pads("AB", 6), macros=True, fader_max=2047,
        pinp_time=True, dsk_time_query=None,
    ),
    "V-1-4K": _model(
        hdmi=_seq("HDMI", 5), sdi=[], stills=2, players=False, channels=8,
        assign_extra=["HDMI", "STILL1", "STILL2", "BLACK", "N/A"],
        video_outputs=["HDMI3", "HDMI4"],
        output_assign=["PGM", "PVW"] + _seq("INPUT", 8),
        aux=0, pinp=1, pinp_source=False, dsk=1, split=1, freeze=False, still_output=False,
        roi=4,
        audio_inputs=["XLR1", "USB"] + _seq("HDMI", 5),
        stereo_link=[], voice_changer=[],
        audio_outputs=["MAIN"] + _seq("AUX", 2) + ["USB"],
        audio_jacks=_seq("HDMI", 5) + ["XLR1", "USB", "PHONES"],
        jack_assign=["MAIN"] + _seq("AUX", 2) + ["MONITOR"],
        reverb=False, memories=8, gpo=0, cameras=5, patterns=["OFF", "COLORBAR"],
        streaming=False, pads=[], macros=False, fader_max=4095,
        # The V-1-4K takes Transition Time for DSK1 and is asked it as DSK
        # (the document's QTIM table for the V-1-4K).
        pinp_time=False, dsk_time_query="DSK",
    ),
}
DEFAULT_MODEL = "V-160HD"

# A child id takes letters, digits, "_" and "-": a word id is the wire id with
# "." as "_" (A.PLAYER is A_PLAYER).


def _local(wire: str) -> str:
    return wire.replace(".", "_")


def _wire(local: Any) -> str:
    return str(local).replace("_", ".")


@dataclass
class _Pending:
    line: str
    want: str  # the reply code a query waits for, or "ACK" for a set
    future: asyncio.Future = field(repr=False)
    ack: asyncio.Future | None = field(default=None, repr=False)
    value_seen: bool = False


class RolandV160HDDriver(BaseDriver):
    """Roland V-160HD / V-80HD / VR-120HD / VR-6HD / V-1-4K LAN/RS-232 driver."""

    DRIVER_INFO = {
        "id": "roland_v160hd",
        "name": "Roland V-160HD / V-80HD / VR-120HD / VR-6HD / V-1-4K",
        "manufacturer": "Roland",
        "category": "video",
        "version": "1.0.0",
        "author": "OpenAVC",
        "min_platform_version": "0.37.0",
        "description": (
            "Controls Roland's V-160HD and V-80HD video switchers, VR-120HD and "
            "VR-6HD streaming AV mixers and the V-1-4K video switcher over their "
            "LAN/RS-232 Basic Control Commands: program and preset sources, cut "
            "and auto transitions, fade to black, keyers (PinP and DSK), AUX "
            "buses, split, input and output assignment, audio input and output "
            "levels and mutes, scene memories, PTZ cameras the switcher "
            "controls, macros, the sequencer, streaming and recording, and "
            "signal status. Each list follows the model the switcher reports."
        ),
        "source_url": "https://static.roland.com/assets/media/pdf/LAN_RS-232_Basic_Control_Commands_eng02_W.pdf",
        "tags": [
            "video-switcher", "production-switcher", "av-mixer", "streaming",
            "roland", "rs232", "lan-control",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["roland-basic-control"],
        "ports": [8023],
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "discovery": {
            # VER; answers the model name. Whether a unit answers it before
            # the network password is sent is not documented, so a unit with
            # a password set may answer nothing and be found by its port only.
            # Roland Corporation holds no IEEE block (0040AB is Roland DG, a
            # different company), so no oui.
            "port_open": [8023],
            "manufacturer_alias": ["roland"],
            "tcp_probe": {
                "port": 8023,
                "send_ascii": "VER;\n",
                "expect_regex": r"VER:(?:V-160HD|V-80HD|VR-120HD|VR-6HD|V-1-4K),",
                "extract_manufacturer": "Roland",
                "extract": {
                    "model": {"regex": r"VER:(V-160HD|V-80HD|VR-120HD|VR-6HD|V-1-4K),"},
                },
                "timeout_ms": 3000,
            },
        },
        "compatible_models": [
            {
                "manufacturer": "Roland",
                "models": ["V-160HD"],
                "confidence": "untested",
                "notes": (
                    "Version 3.5 and later (the Basic Control Commands' own "
                    "floor). LAN (TCP 8023, four-character network password) or "
                    "RS-232. 20 input channels, AUX 1-3, four PinP & KEY, two "
                    "DSK, 30 scene memories, 16 cameras."
                ),
            },
            {
                "manufacturer": "Roland",
                "models": ["V-80HD"],
                "confidence": "untested",
                "notes": (
                    "Version 1.3 and later. LAN (TCP 8023, eight-character "
                    "network password) or RS-232. Video and audio players, SRT, "
                    "streaming and recording."
                ),
            },
            {
                "manufacturer": "Roland",
                "models": ["VR-120HD"],
                "confidence": "untested",
                "notes": (
                    "Version 2.2 and later. LAN (TCP 8023, four-character network "
                    "password) or RS-232. Streaming and recording."
                ),
            },
            {
                "manufacturer": "Roland",
                "models": ["VR-6HD"],
                "confidence": "untested",
                "notes": (
                    "Version 2.2 and later. LAN (TCP 8023, four-character network "
                    "password) or RS-232. Streaming and recording."
                ),
            },
            {
                "manufacturer": "Roland",
                "models": ["V-1-4K"],
                "confidence": "untested",
                "notes": (
                    "Version 1.0 and later. LAN only (TCP 8023, eight-character "
                    "network password). ROI windows; no AUX buses, freeze or "
                    "macros."
                ),
            },
        ],
        "help": {
            "overview": (
                "Roland V-160HD, V-80HD and V-1-4K video switchers and VR-120HD "
                "and VR-6HD streaming AV mixers. Choose program and preset "
                "sources, cut or auto-transition, fade to black, put keyers on "
                "air, route AUX buses, assign inputs and outputs, set audio "
                "levels and mutes, recall scene memories, move the PTZ cameras "
                "the switcher controls, run macros and the sequencer, and start "
                "streaming or recording. The source lists, inputs and outputs "
                "follow the model the switcher reports. Program, preset, keyers "
                "and AUX buses are read every second; the rest a few at a time."
            ),
            "setup": (
                "1. Over the network: on the switcher, MENU > LAN CONTROL (V-160HD) "
                "or MENU > Network > LAN Setup, give it a fixed IP address and "
                "set a Network Password (four characters on the V-160HD, VR-120HD "
                "and VR-6HD, eight on the V-80HD and V-1-4K). LAN control does "
                "not work until a password is set. Enter the address and the "
                "password here. Control uses TCP 8023.\n"
                "2. Over RS-232 (not the V-1-4K): turn RS-232 on in the switcher's "
                "RS-232 menu, note its baud rate (115200 on a V-160HD as it "
                "ships), connect a crossover (null-modem) cable to the 9-pin "
                "RS-232 port, and choose the serial connection. No password is "
                "used.\n"
                "3. PTZ Cameras: the number of cameras set up in the switcher's "
                "camera menu, to control and read them from here. 0 leaves "
                "camera control off.\n"
                "4. Model is read from the switcher when it connects; pick it "
                "here to build or simulate a project before the switcher is on "
                "the network."
            ),
        },
        "default_config": {
            "host": "",
            "port": 8023,
            "password": "",
            "model": "V-160HD",
            "camera_count": 0,
            "baudrate": 115200,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "xonxoff": True,
            "poll_interval": 1,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "The switcher's IP address (LAN control).",
            },
            "port": {
                "type": "integer", "default": 8023, "label": "TCP Port",
                "description": "LAN control port, 8023.",
            },
            "password": {
                "type": "string", "default": "", "secret": True, "label": "Network Password",
                "description": (
                    "The Network Password set in the switcher's LAN menu (four "
                    "characters on the V-160HD, VR-120HD and VR-6HD, eight on "
                    "the V-80HD and V-1-4K). Not used over RS-232."
                ),
            },
            "model": {
                "type": "enum", "default": "V-160HD", "label": "Model",
                "values": ["V-160HD", "V-80HD", "VR-120HD", "VR-6HD", "V-1-4K"],
                "learned_from": "model",
                "description": "Read from the switcher when it connects.",
            },
            "camera_count": {
                "type": "integer", "default": 0, "min": 0, "max": 16, "label": "PTZ Cameras",
                "description": (
                    "How many PTZ cameras the switcher controls (its camera "
                    "menu). 0 leaves camera control off."
                ),
            },
            "baudrate": {
                "type": "integer", "default": 115200, "label": "Baud Rate (RS-232)",
                "description": "9600, 38400 or 115200, as set in the switcher's RS-232 menu.",
            },
            "poll_interval": {
                "type": "integer", "default": 1, "min": 1, "label": "Poll Interval (sec)",
                "description": "How often program, preset, the keyers and the AUX buses are read.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
            "firmware_version": {"type": "string", "label": "Firmware Version", "cloud_priority": "low"},
            "pgm_source": {
                "type": "string", "label": "Program Source", "control": True, "cloud_priority": "high",
                "help": "What is on program (PGM/A): HDMI1, SDI3, STILL2, V.PLAYER or SRT.",
            },
            "pgm_channel": {
                "type": "string", "label": "Program Input Channel", "control": True,
                "help": "The input channel (INPUT1-20) program was chosen by; blank when chosen by source.",
            },
            "pst_source": {
                "type": "string", "label": "Preset Source", "control": True, "cloud_priority": "high",
                "help": "What is on preset (PST/B).",
            },
            "pst_channel": {
                "type": "string", "label": "Preset Input Channel", "control": True,
                "help": "The input channel preset was chosen by; blank when chosen by source.",
            },
            "transition_running": {
                "type": "boolean", "label": "Auto Transition Running",
                "help": "True while an auto transition is under way.",
            },
            "output_fade": {
                "type": "enum", "label": "Fade to Black", "values": ["off", "on", "fadein", "fadeout"],
                "control": True, "cloud_priority": "high",
                "help": "on: the output is faded out. fadein and fadeout: the fade is running.",
            },
            "freeze": {"type": "boolean", "label": "Freeze", "control": True},
            "video_fader": {
                "type": "integer", "label": "Video Fader", "min": 0, "max": 4095,
                "help": "The T-bar's position, 0-2047 (0-4095 on the V-1-4K).",
            },
            "transition_type": {"type": "enum", "label": "Transition Type", "values": ["mix", "wipe"], "control": True},
            "mix_time": {
                "type": "number", "label": "Mix Time", "min": 0, "max": 4, "step": 0.1, "unit": "s",
            },
            "wipe_time": {
                "type": "number", "label": "Wipe Time", "min": 0, "max": 4, "step": 0.1, "unit": "s",
            },
            "output_fade_time": {
                "type": "number", "label": "Output Fade Time", "min": 0, "max": 4, "step": 0.1, "unit": "s",
            },
            "still_output": {
                "type": "string", "label": "Still Image Output",
                "help": "The still (or player) shown in place of the output, or OFF.",
            },
            "memory": {
                "type": "integer", "label": "Scene Memory", "control": True,
                "help": "The scene memory last recalled; blank when none is selected.",
            },
            "auto_switching": {"type": "boolean", "label": "Auto Switching"},
            "reverb": {"type": "boolean", "label": "Reverb", "control": True},
            "auto_mixing": {"type": "boolean", "label": "Audio Auto Mixing", "control": True},
            "hdcp": {"type": "boolean", "label": "HDCP"},
            "test_pattern": {"type": "string", "label": "Test Pattern"},
            "test_tone_level": {
                "type": "string", "label": "Test Tone Level",
                "help": "OFF, -20, -10 or 0dB.",
            },
            "test_tone_left": {"type": "string", "label": "Test Tone Frequency L"},
            "test_tone_right": {"type": "string", "label": "Test Tone Frequency R"},
            "roi_mode": {"type": "boolean", "label": "ROI Mode", "help": "V-1-4K: the ROI windows are in use."},
            "sequencer": {"type": "boolean", "label": "Sequencer"},
            "auto_sequence": {"type": "boolean", "label": "Auto Sequence"},
            "stream_type": {"type": "enum", "label": "Stream & Record Type", "values": ["rtmp", "srt"]},
            "stream_status": {
                "type": "enum", "label": "Stream & Record", "values": ["stop", "stopping", "starting", "onair"],
                "control": True, "cloud_priority": "high",
            },
            "stream_time": {
                "type": "integer", "label": "Stream & Record Time", "unit": "s", "cloud_priority": "low",
                "help": "How long the stream and recording have been running.",
            },
            "safety_image": {"type": "enum", "label": "Safety Image", "values": ["off", "on", "delayed"]},
            "external_rec_control": {"type": "boolean", "label": "External Rec Control"},
            "player_type": {
                "type": "enum", "label": "Player / SRT In", "values": ["v.player", "srt"],
                "help": "Whether the video player or SRT In feeds the V.PLAYER/SRT source.",
            },
            "video_player_status": {
                "type": "enum", "label": "Video Player", "values": ["stop", "play", "pause", "starting", "stopping"],
            },
            "video_player_media": {
                "type": "enum", "label": "Video Player File", "values": ["none", "internal", "sdcard", "usb"],
                "help": "Where the loaded file is: none, internal storage, the SD card or USB.",
            },
            "video_player_length": {"type": "integer", "label": "Video Player Length", "unit": "ms", "cloud_priority": "low"},
            "video_player_position": {"type": "integer", "label": "Video Player Position", "unit": "ms", "cloud_priority": "low"},
            "srt_in_status": {"type": "enum", "label": "SRT In", "values": ["stopped", "stopping", "starting", "started"]},
            "source_options": {
                "type": "string", "label": "Source List", "cloud_priority": "low",
                "help": "The model's sources as a picker list.",
            },
            "aux_source_options": {"type": "string", "label": "AUX Source List", "cloud_priority": "low"},
            "input_assign_options": {"type": "string", "label": "Input Assign List", "cloud_priority": "low"},
            "output_assign_options": {"type": "string", "label": "Output Assign List", "cloud_priority": "low"},
            "still_output_options": {"type": "string", "label": "Still Output List", "cloud_priority": "low"},
            "transition_target_options": {"type": "string", "label": "Transition Time List", "cloud_priority": "low"},
            "audio_jack_options": {"type": "string", "label": "Audio Bus List", "cloud_priority": "low"},
            "test_pattern_options": {"type": "string", "label": "Test Pattern List", "cloud_priority": "low"},
            "audio_player_pad_options": {"type": "string", "label": "Audio Player Pad List", "cloud_priority": "low"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "video_input": {
                "label": "Video Input",
                "label_plural": "Video Inputs",
                "id_format": {"type": "string"},
                "state_variables": {
                    "signal": {
                        "type": "enum", "label": "Signal", "values": ["no_signal", "detected", "unsupported"],
                        "cloud_priority": "high",
                    },
                },
                "summary_fields": ["signal"],
            },
            "input_channel": {
                "label": "Input Channel",
                "label_plural": "Input Channels",
                "id_format": {"type": "integer", "min": 1, "max": 20},
                "state_variables": {
                    "assign": {
                        "type": "string", "label": "Assigned Source", "control": True,
                        "help": "The source this crosspoint channel (INPUT n) shows, or N/A.",
                    },
                },
                "summary_fields": ["assign"],
            },
            "video_output": {
                "label": "Video Output",
                "label_plural": "Video Outputs",
                "id_format": {"type": "string"},
                "state_variables": {
                    "assign": {
                        "type": "string", "label": "Shows", "control": True,
                        "help": "PGM, SUB, PVW, an AUX bus, a DSK, the multi-view and the rest.",
                    },
                },
                "summary_fields": ["assign"],
            },
            "aux": {
                "label": "AUX Bus",
                "label_plural": "AUX Buses",
                "id_format": {"type": "integer", "min": 1, "max": 3},
                "state_variables": {
                    "source": {"type": "string", "label": "Source", "control": True, "cloud_priority": "high"},
                    "channel": {
                        "type": "string", "label": "Input Channel",
                        "help": "The input channel the source was chosen by; blank when chosen by source.",
                    },
                },
                "summary_fields": ["source"],
            },
            "pinp": {
                "label": "PinP & Key",
                "label_plural": "PinP & Keys",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "state_variables": {
                    "pgm": {"type": "boolean", "label": "On Program", "control": True, "cloud_priority": "high"},
                    "pvw": {"type": "boolean", "label": "On Preview", "control": True},
                    "source": {"type": "string", "label": "Source", "control": True},
                    "position_h": {
                        "type": "number", "label": "Position H", "min": -50, "max": 50, "step": 0.1, "unit": "%",
                    },
                    "position_v": {
                        "type": "number", "label": "Position V", "min": -50, "max": 50, "step": 0.1, "unit": "%",
                    },
                    "transition_time": {
                        "type": "number", "label": "Transition Time", "min": 0, "max": 4, "step": 0.1, "unit": "s",
                    },
                },
                "summary_fields": ["pgm", "source"],
            },
            "dsk": {
                "label": "DSK",
                "label_plural": "DSKs",
                "id_format": {"type": "integer", "min": 1, "max": 2},
                "state_variables": {
                    "pgm": {"type": "boolean", "label": "On Program", "control": True, "cloud_priority": "high"},
                    "pvw": {"type": "boolean", "label": "On Preview", "control": True},
                    "fill_source": {"type": "string", "label": "Fill Source", "control": True},
                    "fill_channel": {"type": "string", "label": "Fill Input Channel"},
                    "level": {"type": "integer", "label": "Key Level", "min": 0, "max": 255, "control": True},
                    "gain": {"type": "integer", "label": "Key Gain", "min": 0, "max": 255, "control": True},
                    "transition_time": {
                        "type": "number", "label": "Transition Time", "min": 0, "max": 4, "step": 0.1, "unit": "s",
                    },
                },
                "summary_fields": ["pgm", "fill_source"],
            },
            "split": {
                "label": "Split",
                "label_plural": "Splits",
                "id_format": {"type": "integer", "min": 1, "max": 2},
                "state_variables": {
                    "on": {"type": "boolean", "label": "On", "control": True},
                    "pgm_center": {
                        "type": "number", "label": "PGM/A Center", "min": -50, "max": 50, "step": 0.1, "unit": "%",
                    },
                    "pst_center": {
                        "type": "number", "label": "PST/B Center", "min": -50, "max": 50, "step": 0.1, "unit": "%",
                    },
                    "center_position": {
                        "type": "number", "label": "Center Position", "min": -50, "max": 50, "step": 0.1, "unit": "%",
                    },
                },
                "summary_fields": ["on"],
            },
            "roi": {
                "label": "ROI Window",
                "label_plural": "ROI Windows",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "state_variables": {
                    "zoom": {"type": "number", "label": "Zoom", "min": 100, "max": 400, "step": 0.1, "unit": "%", "control": True},
                    "position_h": {"type": "integer", "label": "Position H", "min": -1920, "max": 1920},
                    "position_v": {"type": "integer", "label": "Position V", "min": -1080, "max": 1080},
                },
                "summary_fields": ["zoom"],
            },
            "audio_input": {
                "label": "Audio Input",
                "label_plural": "Audio Inputs",
                "id_format": {"type": "string"},
                "state_variables": {
                    "level": {
                        "type": "number", "label": "Level", "min": -81, "max": 10, "step": 0.1, "unit": "dB",
                        "control": True,
                        "help": "-80.0 to +10.0 dB; -81 is -INF (off).",
                    },
                    "mute": {"type": "boolean", "label": "Mute", "control": True, "cloud_priority": "high"},
                    "solo": {"type": "boolean", "label": "Solo", "control": True},
                    "delay": {"type": "number", "label": "Delay", "min": 0, "max": 500, "step": 0.1, "unit": "ms"},
                    "high_pass_filter": {"type": "boolean", "label": "High Pass Filter"},
                    "gate": {"type": "boolean", "label": "Gate"},
                    "stereo_link": {"type": "boolean", "label": "Stereo Link", "help": "XLR inputs."},
                    "voice_changer": {"type": "boolean", "label": "Voice Changer", "help": "XLR 1 and 2."},
                },
                "summary_fields": ["level", "mute"],
            },
            "audio_output": {
                "label": "Audio Output",
                "label_plural": "Audio Outputs",
                "id_format": {"type": "string"},
                "state_variables": {
                    "level": {
                        "type": "number", "label": "Level", "min": -81, "max": 10, "step": 0.1, "unit": "dB",
                        "control": True,
                        "help": "-80.0 to +10.0 dB; -81 is -INF (off).",
                    },
                    "mute": {"type": "boolean", "label": "Mute", "control": True, "cloud_priority": "high"},
                },
                "summary_fields": ["level", "mute"],
            },
            "audio_jack": {
                "label": "Audio Output Jack",
                "label_plural": "Audio Output Jacks",
                "id_format": {"type": "string"},
                "state_variables": {
                    "assign": {
                        "type": "string", "label": "Bus",
                        "help": "The bus the jack plays: AUTO, MAIN, an AUX bus or MONITOR.",
                    },
                },
                "summary_fields": ["assign"],
            },
            "gpo": {
                "label": "GPO",
                "label_plural": "GPOs",
                "id_format": {"type": "integer", "min": 1, "max": 16},
                "state_variables": {"on": {"type": "boolean", "label": "On", "control": True}},
                "summary_fields": ["on"],
            },
            "camera": {
                "label": "PTZ Camera",
                "label_plural": "PTZ Cameras",
                "id_format": {"type": "integer", "min": 1, "max": 16},
                "state_variables": {
                    "pan_tilt_speed": {"type": "integer", "label": "Pan/Tilt Speed", "min": 1, "max": 24},
                    "auto_focus": {"type": "boolean", "label": "Auto Focus", "control": True},
                    "auto_exposure": {"type": "boolean", "label": "Auto Exposure", "control": True},
                    "preset": {
                        "type": "integer", "label": "Preset", "min": 1, "max": 100, "control": True,
                        "help": "The preset last recalled; blank when none.",
                    },
                },
                "summary_fields": ["preset"],
            },
        },
        "device_settings": {
            "hdcp": {
                "type": "boolean", "label": "HDCP", "state_key": "hdcp", "default": False, "setup": False,
                "help": "Accepts HDCP-protected sources. With it on, streaming, recording and some outputs carry no protected picture.",
            },
        },
        "quick_actions": ["cut", "auto_take", "select_pgm", "recall_memory"],
        "commands": {
            # ── Switching ──
            "select_pgm": {
                "label": "Select Program",
                "params": {
                    "source": {
                        "type": "string", "label": "Source", "required": True,
                        "options_state": "source_options", "pattern": r"^[A-Za-z0-9.]+$",
                        "help": "A source (HDMI1, SDI2, STILL3, V.PLAYER, SRT) or an input channel (INPUT1-20).",
                    },
                },
                "help": "Puts the source on program (PGM/A) at once.",
            },
            "select_pst": {
                "label": "Select Preset",
                "params": {
                    "source": {
                        "type": "string", "label": "Source", "required": True,
                        "options_state": "source_options", "pattern": r"^[A-Za-z0-9.]+$",
                        "help": "A source or an input channel.",
                    },
                },
                "help": "Puts the source on preset (PST/B), ready for the next transition.",
            },
            "cut": {
                "label": "Cut",
                "params": {
                    "source": {
                        "type": "string", "label": "Source", "options_state": "source_options",
                        "pattern": r"^[A-Za-z0-9.]*$",
                        "help": "Blank: cuts to the preset source. A source: cuts straight to it.",
                    },
                },
                "help": "Switches program to preset (or to the chosen source) as a cut. Not while SPLIT is on.",
            },
            "auto_take": {
                "label": "Auto Transition",
                "params": {
                    "source": {
                        "type": "string", "label": "Source", "options_state": "source_options",
                        "pattern": r"^[A-Za-z0-9.]*$",
                        "help": "Blank: transitions to the preset source. A source: transitions straight to it.",
                    },
                    "time": {
                        "type": "number", "label": "Time", "min": 0, "max": 4, "unit": "s",
                        "help": "Blank uses the switcher's Transition Time. Needs a source.",
                    },
                },
                "help": "Runs the transition (mix or wipe) from program to preset, or to the chosen source. Not while SPLIT is on.",
            },
            "set_video_fader": {
                "label": "Set Video Fader",
                "params": {
                    "level": {
                        "type": "integer", "label": "Level", "required": True, "min": 0, "max": 4095,
                        "help": "0-2047 (0-4095 on the V-1-4K): the T-bar's position.",
                    },
                },
                "sets": {"video_fader": "{level}"},
                "help": "Moves the transition to the position, as the T-bar does.",
            },
            "set_transition_type": {
                "label": "Set Transition Type",
                "params": {
                    "type": {
                        "type": "enum", "label": "Type", "required": True,
                        "values": [{"value": "mix", "label": "Mix"}, {"value": "wipe", "label": "Wipe"}],
                    },
                },
                "sets": {"transition_type": "{type}"},
                "help": "Chooses mix or wipe for the next transition.",
            },
            "set_transition_time": {
                "label": "Set Transition Time",
                "params": {
                    "target": {
                        "type": "string", "label": "Transition", "required": True,
                        "options_state": "transition_target_options", "pattern": r"^[A-Za-z0-9]+$",
                        "help": "MIX, WIPE, PinP1-4, DSK1-2 or OUTPUTFADE.",
                    },
                    "time": {
                        "type": "number", "label": "Time", "required": True, "min": 0, "max": 4, "unit": "s",
                    },
                },
                "help": "Sets how long the chosen transition takes.",
            },
            "set_output_fade": {
                "label": "Set Fade to Black",
                "params": {"on": {"type": "boolean", "label": "Faded Out", "required": True}},
                "help": "On fades the output to black; off fades it back in.",
            },
            "toggle_output_fade": {
                "label": "Fade to Black",
                "params": {},
                "help": "Fades the output to black, or back in when it is faded out.",
            },
            "set_freeze": {
                "label": "Set Freeze",
                "params": {"on": {"type": "boolean", "label": "Frozen", "required": True}},
                "sets": {"freeze": "{on}"},
                "help": "Freezes the picture on every input, or lets it run. Not on the V-1-4K.",
            },
            "toggle_freeze": {
                "label": "Toggle Freeze",
                "params": {},
                "help": "Freezes the inputs, or lets them run when frozen. Not on the V-1-4K.",
            },
            "set_still_output": {
                "label": "Still Image Output",
                "params": {
                    "source": {
                        "type": "string", "label": "Still", "required": True,
                        "options_state": "still_output_options", "pattern": r"^[A-Za-z0-9.]+$",
                        "help": "OFF, a still, or (V-80HD, VR-120HD, VR-6HD) V.PLAYER or SRT.",
                    },
                },
                "sets": {"still_output": "{source}"},
                "help": "Shows the still in place of the output, or OFF to go back. Not on the V-1-4K.",
            },
            "assign_input": {
                "label": "Assign Input Channel",
                "params": {
                    "channel": {
                        "type": "child_id", "child_type": "input_channel", "label": "Input Channel",
                        "required": True,
                    },
                    "source": {
                        "type": "string", "label": "Source", "required": True,
                        "options_state": "input_assign_options", "pattern": r"^[A-Za-z0-9./]+$",
                        "help": "A source, or N/A to leave the channel empty.",
                    },
                },
                "sets": {"assign": "{source}"},
                "help": "Sets the source a crosspoint input channel shows.",
            },
            "assign_video_output": {
                "label": "Assign Video Output",
                "params": {
                    "output": {
                        "type": "child_id", "child_type": "video_output", "label": "Output", "required": True,
                    },
                    "assign": {
                        "type": "string", "label": "Shows", "required": True,
                        "options_state": "output_assign_options", "pattern": r"^[A-Za-z0-9]+$",
                    },
                },
                "sets": {"assign": "{assign}"},
                "help": "Sets what a video output (HDMI, SDI, USB, the stream) shows: PGM, PVW, an AUX bus, the multi-view and so on.",
            },
            "set_aux": {
                "label": "Set AUX Source",
                "params": {
                    "aux": {"type": "child_id", "child_type": "aux", "label": "AUX", "required": True},
                    "source": {
                        "type": "string", "label": "Source", "required": True,
                        "options_state": "aux_source_options", "pattern": r"^[A-Za-z0-9.]+$",
                        "help": "A source, an input channel, or PGMLINK to follow program (when the AUX's Linked PGM is on).",
                    },
                },
                "help": "Sends the source to the AUX bus. Not on the V-1-4K.",
            },
            # ── PinP & KEY ──
            "set_pinp_pgm": {
                "label": "Set PinP on Program",
                "params": {
                    "pinp": {"type": "child_id", "child_type": "pinp", "label": "PinP & Key", "required": True},
                    "on": {"type": "boolean", "label": "On Program", "required": True},
                },
                "sets": {"pgm": "{on}"},
                "help": "Puts the PinP or key on program, or takes it off.",
            },
            "toggle_pinp_pgm": {
                "label": "Toggle PinP on Program",
                "params": {
                    "pinp": {"type": "child_id", "child_type": "pinp", "label": "PinP & Key", "required": True},
                },
                "help": "Puts the PinP or key on program, or takes it off when it is on.",
            },
            "set_pinp_pvw": {
                "label": "Set PinP on Preview",
                "params": {
                    "pinp": {"type": "child_id", "child_type": "pinp", "label": "PinP & Key", "required": True},
                    "on": {"type": "boolean", "label": "On Preview", "required": True},
                },
                "sets": {"pvw": "{on}"},
                "help": "Shows the PinP or key on preview, or takes it off.",
            },
            "toggle_pinp_pvw": {
                "label": "Toggle PinP on Preview",
                "params": {
                    "pinp": {"type": "child_id", "child_type": "pinp", "label": "PinP & Key", "required": True},
                },
                "help": "Shows the PinP or key on preview, or takes it off when it is on.",
            },
            "set_pinp_source": {
                "label": "Set PinP Source",
                "params": {
                    "pinp": {"type": "child_id", "child_type": "pinp", "label": "PinP & Key", "required": True},
                    "source": {
                        "type": "string", "label": "Source", "required": True,
                        "options_state": "source_options", "pattern": r"^[A-Za-z0-9.]+$",
                    },
                },
                "sets": {"source": "{source}"},
                "help": "Sets the picture in the PinP window or key. Not on the V-1-4K.",
            },
            "set_pinp_position": {
                "label": "Set PinP Position",
                "params": {
                    "pinp": {"type": "child_id", "child_type": "pinp", "label": "PinP & Key", "required": True},
                    "position_h": {
                        "type": "number", "label": "Position H", "required": True, "min": -50, "max": 50,
                        "unit": "%",
                    },
                    "position_v": {
                        "type": "number", "label": "Position V", "required": True, "min": -50, "max": 50,
                        "unit": "%",
                    },
                },
                "sets": {"position_h": "{position_h}", "position_v": "{position_v}"},
                "help": "Moves the PinP window; 0, 0 is the centre.",
            },
            # ── DSK ──
            "set_dsk_pgm": {
                "label": "Set DSK on Program",
                "params": {
                    "dsk": {"type": "child_id", "child_type": "dsk", "label": "DSK", "required": True},
                    "on": {"type": "boolean", "label": "On Program", "required": True},
                },
                "sets": {"pgm": "{on}"},
                "help": "Puts the downstream key (a title or logo) on program, or takes it off.",
            },
            "toggle_dsk_pgm": {
                "label": "Toggle DSK on Program",
                "params": {"dsk": {"type": "child_id", "child_type": "dsk", "label": "DSK", "required": True}},
                "help": "Puts the downstream key on program, or takes it off when it is on.",
            },
            "set_dsk_pvw": {
                "label": "Set DSK on Preview",
                "params": {
                    "dsk": {"type": "child_id", "child_type": "dsk", "label": "DSK", "required": True},
                    "on": {"type": "boolean", "label": "On Preview", "required": True},
                },
                "sets": {"pvw": "{on}"},
                "help": "Shows the downstream key on preview, or takes it off.",
            },
            "toggle_dsk_pvw": {
                "label": "Toggle DSK on Preview",
                "params": {"dsk": {"type": "child_id", "child_type": "dsk", "label": "DSK", "required": True}},
                "help": "Shows the downstream key on preview, or takes it off when it is on.",
            },
            "set_dsk_source": {
                "label": "Set DSK Fill Source",
                "params": {
                    "dsk": {"type": "child_id", "child_type": "dsk", "label": "DSK", "required": True},
                    "source": {
                        "type": "string", "label": "Fill Source", "required": True,
                        "options_state": "source_options", "pattern": r"^[A-Za-z0-9.]+$",
                    },
                },
                "help": "Sets the picture the downstream key fills with.",
            },
            "set_dsk_level": {
                "label": "Set DSK Level",
                "params": {
                    "dsk": {"type": "child_id", "child_type": "dsk", "label": "DSK", "required": True},
                    "level": {"type": "integer", "label": "Key Level", "required": True, "min": 0, "max": 255},
                },
                "sets": {"level": "{level}"},
                "help": "Sets the key level: how much of the fill is cut out.",
            },
            "set_dsk_gain": {
                "label": "Set DSK Gain",
                "params": {
                    "dsk": {"type": "child_id", "child_type": "dsk", "label": "DSK", "required": True},
                    "gain": {"type": "integer", "label": "Key Gain", "required": True, "min": 0, "max": 255},
                },
                "sets": {"gain": "{gain}"},
                "help": "Sets the key gain: how soft the key's edge is.",
            },
            # ── Split ──
            "set_split": {
                "label": "Set Split",
                "params": {
                    "split": {"type": "child_id", "child_type": "split", "label": "Split", "required": True},
                    "on": {"type": "boolean", "label": "On", "required": True},
                },
                "sets": {"on": "{on}"},
                "help": "Shows program and preset side by side, or ends it.",
            },
            "toggle_split": {
                "label": "Toggle Split",
                "params": {"split": {"type": "child_id", "child_type": "split", "label": "Split", "required": True}},
                "help": "Turns the split on, or off when it is on.",
            },
            "set_split_positions": {
                "label": "Set Split Positions",
                "params": {
                    "split": {"type": "child_id", "child_type": "split", "label": "Split", "required": True},
                    "pgm_center": {
                        "type": "number", "label": "PGM/A Center", "required": True, "min": -50, "max": 50,
                        "unit": "%",
                    },
                    "pst_center": {
                        "type": "number", "label": "PST/B Center", "required": True, "min": -50, "max": 50,
                        "unit": "%",
                    },
                    "center_position": {
                        "type": "number", "label": "Center Position", "min": -50, "max": 50, "unit": "%",
                        "help": "Blank leaves the dividing line where it is.",
                    },
                },
                "help": "Moves the two pictures in the split, and the line between them.",
            },
            # ── ROI (V-1-4K) ──
            "set_roi_mode": {
                "label": "Set ROI Mode",
                "params": {"on": {"type": "boolean", "label": "On", "required": True}},
                "sets": {"roi_mode": "{on}"},
                "help": "V-1-4K: turns the ROI windows (crops of the 4K input) on or off.",
            },
            "set_roi_zoom": {
                "label": "Set ROI Zoom",
                "params": {
                    "roi": {"type": "child_id", "child_type": "roi", "label": "ROI", "required": True},
                    "zoom": {
                        "type": "number", "label": "Zoom", "required": True, "min": 100, "max": 400, "unit": "%",
                    },
                },
                "sets": {"zoom": "{zoom}"},
                "help": "V-1-4K: zooms the ROI window into the 4K input.",
            },
            "set_roi_position": {
                "label": "Set ROI Position",
                "params": {
                    "roi": {"type": "child_id", "child_type": "roi", "label": "ROI", "required": True},
                    "position_h": {"type": "integer", "label": "Position H", "required": True, "min": -1920, "max": 1920},
                    "position_v": {"type": "integer", "label": "Position V", "required": True, "min": -1080, "max": 1080},
                },
                "sets": {"position_h": "{position_h}", "position_v": "{position_v}"},
                "help": "V-1-4K: moves the ROI window across the 4K input.",
            },
            # ── Audio ──
            "set_input_level": {
                "label": "Set Audio Input Level",
                "params": {
                    "input": {"type": "child_id", "child_type": "audio_input", "label": "Input", "required": True},
                    "level": {
                        "type": "number", "label": "Level", "required": True, "min": -81, "max": 10, "unit": "dB", "help": "-80.0 to +10.0 dB; below -80 sends -INF (off).",
                    },
                },
                "sets": {"level": "{level}"},
                "help": "Sets the audio input's fader.",
            },
            "set_input_mute": {
                "label": "Set Audio Input Mute",
                "params": {
                    "input": {"type": "child_id", "child_type": "audio_input", "label": "Input", "required": True},
                    "muted": {"type": "boolean", "label": "Muted", "required": True},
                },
                "sets": {"mute": "{muted}"},
                "help": "Mutes or unmutes the audio input.",
            },
            "toggle_input_mute": {
                "label": "Toggle Audio Input Mute",
                "params": {"input": {"type": "child_id", "child_type": "audio_input", "label": "Input", "required": True}},
                "help": "Mutes the audio input, or unmutes it when muted.",
            },
            "set_input_solo": {
                "label": "Set Audio Input Solo",
                "params": {
                    "input": {"type": "child_id", "child_type": "audio_input", "label": "Input", "required": True},
                    "on": {"type": "boolean", "label": "Solo", "required": True},
                },
                "sets": {"solo": "{on}"},
                "help": "Solos the input in the headphones and monitor, or ends the solo.",
            },
            "toggle_input_solo": {
                "label": "Toggle Audio Input Solo",
                "params": {"input": {"type": "child_id", "child_type": "audio_input", "label": "Input", "required": True}},
                "help": "Solos the input, or ends the solo when it is soloed.",
            },
            "set_input_delay": {
                "label": "Set Audio Input Delay",
                "params": {
                    "input": {"type": "child_id", "child_type": "audio_input", "label": "Input", "required": True},
                    "delay": {
                        "type": "number", "label": "Delay", "required": True, "min": 0, "max": 500, "unit": "ms",
                    },
                },
                "sets": {"delay": "{delay}"},
                "help": "Delays the input's audio to line it up with the picture.",
            },
            "set_input_high_pass_filter": {
                "label": "Set High Pass Filter",
                "params": {
                    "input": {"type": "child_id", "child_type": "audio_input", "label": "Input", "required": True},
                    "on": {"type": "boolean", "label": "On", "required": True},
                },
                "sets": {"high_pass_filter": "{on}"},
                "help": "Turns the input's high pass filter (cuts rumble) on or off.",
            },
            "set_input_gate": {
                "label": "Set Gate",
                "params": {
                    "input": {"type": "child_id", "child_type": "audio_input", "label": "Input", "required": True},
                    "on": {"type": "boolean", "label": "On", "required": True},
                },
                "sets": {"gate": "{on}"},
                "help": "Turns the input's noise gate on or off.",
            },
            "set_stereo_link": {
                "label": "Set Stereo Link",
                "params": {
                    "input": {"type": "child_id", "child_type": "audio_input", "label": "XLR Input", "required": True},
                    "on": {"type": "boolean", "label": "On", "required": True},
                },
                "sets": {"stereo_link": "{on}"},
                "help": "Links the XLR input with its neighbour as a stereo pair, or unlinks it. Not on the V-1-4K.",
            },
            "set_voice_changer": {
                "label": "Set Voice Changer",
                "params": {
                    "input": {"type": "child_id", "child_type": "audio_input", "label": "XLR Input", "required": True},
                    "on": {"type": "boolean", "label": "On", "required": True},
                },
                "sets": {"voice_changer": "{on}"},
                "help": "Turns the voice changer on XLR 1 or 2 on or off. Not on the V-1-4K.",
            },
            "set_output_level": {
                "label": "Set Audio Output Level",
                "params": {
                    "output": {"type": "child_id", "child_type": "audio_output", "label": "Output", "required": True},
                    "level": {
                        "type": "number", "label": "Level", "required": True, "min": -81, "max": 10, "unit": "dB", "help": "-80.0 to +10.0 dB; below -80 sends -INF (off).",
                    },
                },
                "sets": {"level": "{level}"},
                "help": "Sets the level of MAIN, an AUX bus, USB, STREAM or REC.",
            },
            "set_output_mute": {
                "label": "Set Audio Output Mute",
                "params": {
                    "output": {"type": "child_id", "child_type": "audio_output", "label": "Output", "required": True},
                    "muted": {"type": "boolean", "label": "Muted", "required": True},
                },
                "sets": {"mute": "{muted}"},
                "help": "Mutes or unmutes MAIN, an AUX bus, USB, STREAM or REC.",
            },
            "assign_audio_output": {
                "label": "Assign Audio Output Jack",
                "params": {
                    "jack": {"type": "child_id", "child_type": "audio_jack", "label": "Jack", "required": True},
                    "bus": {
                        "type": "string", "label": "Bus", "required": True,
                        "options_state": "audio_jack_options", "pattern": r"^[A-Za-z0-9]+$",
                        "help": "AUTO, MAIN, an AUX bus or MONITOR; which ones a jack takes depends on the jack and model.",
                    },
                },
                "sets": {"assign": "{bus}"},
                "help": "Sets the bus an output jack (HDMI, SDI, XLR, USB, PHONES) plays.",
            },
            "set_reverb": {
                "label": "Set Reverb",
                "params": {"on": {"type": "boolean", "label": "On", "required": True}},
                "sets": {"reverb": "{on}"},
                "help": "Turns the reverb on or off. Not on the V-1-4K.",
            },
            "toggle_reverb": {
                "label": "Toggle Reverb",
                "params": {},
                "help": "Turns the reverb on, or off when it is on. Not on the V-1-4K.",
            },
            "set_auto_mixing": {
                "label": "Set Audio Auto Mixing",
                "params": {"on": {"type": "boolean", "label": "On", "required": True}},
                "sets": {"auto_mixing": "{on}"},
                "help": "Turns audio auto mixing (levels follow who is talking) on or off.",
            },
            "toggle_auto_mixing": {
                "label": "Toggle Audio Auto Mixing",
                "params": {},
                "help": "Turns audio auto mixing on, or off when it is on.",
            },
            # ── Control ──
            "recall_memory": {
                "label": "Recall Scene Memory",
                "params": {
                    "memory": {
                        "type": "integer", "label": "Memory", "required": True, "min": 1, "max": 32,
                        "help": "1-30 on the V-160HD, 1-32 on the V-80HD, VR-120HD and VR-6HD, 1-8 on the V-1-4K.",
                    },
                },
                "sets": {"memory": "{memory}"},
                "help": "Recalls the scene memory: the switcher's settings as they were stored in it.",
            },
            "set_auto_switching": {
                "label": "Set Auto Switching",
                "params": {"on": {"type": "boolean", "label": "On", "required": True}},
                "sets": {"auto_switching": "{on}"},
                "help": "Turns automatic switching between sources on or off.",
            },
            "toggle_auto_switching": {
                "label": "Toggle Auto Switching",
                "params": {},
                "help": "Turns automatic switching on, or off when it is on.",
            },
            "input_scan": {
                "label": "Input Scan",
                "params": {
                    "order": {
                        "type": "enum", "label": "Order", "required": True,
                        "values": [
                            {"value": "NORMAL", "label": "Normal"},
                            {"value": "REVERSE", "label": "Reverse"},
                            {"value": "RANDOM", "label": "Random"},
                        ],
                    },
                },
                "help": "Steps program on to the next input in the order chosen.",
            },
            "memory_scan": {
                "label": "Scene Memory Scan",
                "params": {
                    "order": {
                        "type": "enum", "label": "Order", "required": True,
                        "values": [
                            {"value": "NORMAL", "label": "Normal"},
                            {"value": "REVERSE", "label": "Reverse"},
                            {"value": "RANDOM", "label": "Random"},
                        ],
                    },
                },
                "help": "Recalls the next scene memory in the order chosen. Not on the V-1-4K.",
            },
            "pinp_scan": {
                "label": "PinP Source Scan",
                "params": {
                    "pinp": {"type": "child_id", "child_type": "pinp", "label": "PinP & Key", "required": True},
                    "order": {
                        "type": "enum", "label": "Order", "required": True,
                        "values": [
                            {"value": "NORMAL", "label": "Normal"},
                            {"value": "REVERSE", "label": "Reverse"},
                            {"value": "RANDOM", "label": "Random"},
                        ],
                    },
                },
                "help": "Steps the PinP window on to its next source. Not on the V-1-4K.",
            },
            "dsk_scan": {
                "label": "DSK Source Scan",
                "params": {
                    "dsk": {"type": "child_id", "child_type": "dsk", "label": "DSK", "required": True},
                    "order": {
                        "type": "enum", "label": "Order", "required": True,
                        "values": [
                            {"value": "NORMAL", "label": "Normal"},
                            {"value": "REVERSE", "label": "Reverse"},
                            {"value": "RANDOM", "label": "Random"},
                        ],
                    },
                },
                "help": "Steps the downstream key on to its next fill source. Not on the V-1-4K.",
            },
            "pulse_gpo": {
                "label": "Pulse GPO",
                "params": {"gpo": {"type": "child_id", "child_type": "gpo", "label": "GPO", "required": True}},
                "help": "Sends a one-shot pulse on the GPO pin. Not on the V-1-4K.",
            },
            "set_gpo": {
                "label": "Set GPO",
                "params": {
                    "gpo": {"type": "child_id", "child_type": "gpo", "label": "GPO", "required": True},
                    "on": {"type": "boolean", "label": "On", "required": True},
                },
                "sets": {"on": "{on}"},
                "help": "Holds the GPO pin on or off. Not on the V-1-4K.",
            },
            # ── Stream & record, players (V-80HD, VR-120HD, VR-6HD) ──
            "set_stream_type": {
                "label": "Set Stream & Record Type",
                "params": {
                    "type": {
                        "type": "enum", "label": "Type", "required": True,
                        "values": [{"value": "rtmp", "label": "RTMP"}, {"value": "srt", "label": "SRT"}],
                    },
                },
                "sets": {"stream_type": "{type}"},
                "help": "Chooses whether Start Stream sends RTMP (with recording) or SRT Out.",
            },
            "start_stream": {
                "label": "Start Stream & Record",
                "params": {},
                "help": "Starts streaming and recording (Type RTMP).",
            },
            "stop_stream": {
                "label": "Stop Stream & Record",
                "params": {},
                "help": "Stops streaming and recording (Type RTMP).",
            },
            "start_srt_out": {
                "label": "Start SRT Out",
                "params": {},
                "help": "Starts sending SRT (Type SRT).",
            },
            "stop_srt_out": {
                "label": "Stop SRT Out",
                "params": {},
                "help": "Stops sending SRT (Type SRT).",
            },
            "set_safety_image": {
                "label": "Set Safety Image",
                "params": {"on": {"type": "boolean", "label": "On", "required": True}},
                "help": "Sends the safety image to the stream instead of program, or ends it. Only while on air.",
            },
            "set_external_rec_control": {
                "label": "Set External Rec Control",
                "params": {"on": {"type": "boolean", "label": "On", "required": True}},
                "sets": {"external_rec_control": "{on}"},
                "help": "Turns on or off the start and stop of recording on a connected recorder.",
            },
            "audio_player": {
                "label": "Audio Player",
                "params": {
                    "pad": {
                        "type": "string", "label": "Pad", "required": True,
                        "options_state": "audio_player_pad_options", "pattern": r"^[A-H]-[1-8]$",
                        "help": "The pad, A-1 to H-8 (V-80HD), A-1 to B-8 (VR-120HD), A-1 to B-6 (VR-6HD).",
                    },
                    "action": {
                        "type": "enum", "label": "Action", "required": True,
                        "values": [
                            {"value": "PLAY", "label": "Play"},
                            {"value": "STOP", "label": "Stop"},
                            {"value": "PAUSE", "label": "Pause"},
                            {"value": "RESUME", "label": "Resume"},
                        ],
                    },
                },
                "help": "Plays, stops, pauses or resumes the sound on an audio player pad.",
            },
            "set_audio_player_repeat": {
                "label": "Set Audio Player Repeat",
                "params": {
                    "pad": {
                        "type": "string", "label": "Pad", "required": True,
                        "options_state": "audio_player_pad_options", "pattern": r"^[A-H]-[1-8]$",
                    },
                    "on": {"type": "boolean", "label": "Repeat", "required": True},
                },
                "help": "Makes the pad's sound repeat, or play once.",
            },
            "set_player_type": {
                "label": "Set Player / SRT In",
                "params": {
                    "type": {
                        "type": "enum", "label": "Type", "required": True,
                        "values": [{"value": "v.player", "label": "Video Player"}, {"value": "srt", "label": "SRT In"}],
                    },
                },
                "sets": {"player_type": "{type}"},
                "help": "Chooses whether the V.PLAYER/SRT source is the video player or SRT In.",
            },
            "video_player": {
                "label": "Video Player",
                "params": {
                    "action": {
                        "type": "enum", "label": "Action", "required": True,
                        "values": [
                            {"value": "PLAY", "label": "Play"},
                            {"value": "STOP", "label": "Stop"},
                            {"value": "PAUSE", "label": "Pause"},
                            {"value": "RESUME", "label": "Resume"},
                        ],
                    },
                },
                "help": "Plays, stops, pauses or resumes the video player (Type Video Player).",
            },
            "start_srt_in": {"label": "Start SRT In", "params": {}, "help": "Starts receiving SRT In (Type SRT In)."},
            "stop_srt_in": {"label": "Stop SRT In", "params": {}, "help": "Stops receiving SRT In (Type SRT In)."},
            # ── PTZ cameras ──
            "camera_pan_tilt": {
                "label": "Camera Pan/Tilt",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "label": "Camera", "required": True},
                    "pan": {
                        "type": "enum", "label": "Pan", "required": True,
                        "values": [
                            {"value": "LEFT", "label": "Left"},
                            {"value": "STOP", "label": "Stop"},
                            {"value": "RIGHT", "label": "Right"},
                        ],
                    },
                    "tilt": {
                        "type": "enum", "label": "Tilt", "required": True,
                        "values": [
                            {"value": "UP", "label": "Up"},
                            {"value": "STOP", "label": "Stop"},
                            {"value": "DOWN", "label": "Down"},
                        ],
                    },
                },
                "help": "Starts the camera moving (or stops it) at its pan/tilt speed. Send Stop, Stop to end a move.",
            },
            "camera_stop": {
                "label": "Camera Stop",
                "params": {"camera": {"type": "child_id", "child_type": "camera", "label": "Camera", "required": True}},
                "help": "Stops the camera's pan, tilt and zoom.",
            },
            "set_camera_speed": {
                "label": "Set Camera Pan/Tilt Speed",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "label": "Camera", "required": True},
                    "speed": {"type": "integer", "label": "Speed", "required": True, "min": 1, "max": 24},
                },
                "sets": {"pan_tilt_speed": "{speed}"},
                "help": "Sets how fast the camera pans and tilts, 1 (slow) to 24.",
            },
            "camera_zoom": {
                "label": "Camera Zoom",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "label": "Camera", "required": True},
                    "direction": {
                        "type": "enum", "label": "Zoom", "required": True,
                        "values": [
                            {"value": "TELE_FAST", "label": "In, fast"},
                            {"value": "TELE_SLOW", "label": "In, slow"},
                            {"value": "STOP", "label": "Stop"},
                            {"value": "WIDE_SLOW", "label": "Out, slow"},
                            {"value": "WIDE_FAST", "label": "Out, fast"},
                        ],
                    },
                },
                "help": "Starts the camera zooming in or out, or stops it.",
            },
            "camera_zoom_reset": {
                "label": "Camera Zoom Reset",
                "params": {"camera": {"type": "child_id", "child_type": "camera", "label": "Camera", "required": True}},
                "help": "Returns the camera's zoom to its reset position.",
            },
            "camera_focus": {
                "label": "Camera Focus",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "label": "Camera", "required": True},
                    "direction": {
                        "type": "enum", "label": "Focus", "required": True,
                        "values": [
                            {"value": "NEAR", "label": "Near"},
                            {"value": "STOP", "label": "Stop"},
                            {"value": "FAR", "label": "Far"},
                        ],
                    },
                },
                "help": "Starts the camera's focus moving near or far, or stops it.",
            },
            "set_camera_auto_focus": {
                "label": "Set Camera Auto Focus",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "label": "Camera", "required": True},
                    "on": {"type": "boolean", "label": "On", "required": True},
                },
                "sets": {"auto_focus": "{on}"},
                "help": "Turns the camera's auto focus on or off.",
            },
            "set_camera_auto_exposure": {
                "label": "Set Camera Auto Exposure",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "label": "Camera", "required": True},
                    "on": {"type": "boolean", "label": "On", "required": True},
                },
                "sets": {"auto_exposure": "{on}"},
                "help": "Turns the camera's auto exposure on or off.",
            },
            "recall_camera_preset": {
                "label": "Recall Camera Preset",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "label": "Camera", "required": True},
                    "preset": {"type": "integer", "label": "Preset", "required": True, "min": 1, "max": 100},
                },
                "sets": {"preset": "{preset}"},
                "help": "Moves the camera to the preset.",
            },
            # ── Macros, sequencer, Graphics Presenter ──
            "run_macro": {
                "label": "Run Macro",
                "params": {"macro": {"type": "integer", "label": "Macro", "required": True, "min": 1, "max": 100}},
                "help": "Runs the macro stored on the switcher. Not on the V-1-4K.",
            },
            "set_sequencer": {
                "label": "Set Sequencer",
                "params": {"on": {"type": "boolean", "label": "On", "required": True}},
                "sets": {"sequencer": "{on}"},
                "help": "Turns the sequencer on or off. Not on the V-1-4K.",
            },
            "set_auto_sequence": {
                "label": "Set Auto Sequence",
                "params": {"on": {"type": "boolean", "label": "On", "required": True}},
                "sets": {"auto_sequence": "{on}"},
                "help": "Turns the sequencer's automatic stepping on or off. Not on the V-1-4K.",
            },
            "sequencer_next": {
                "label": "Sequencer Next",
                "params": {},
                "help": "Steps the sequencer on to the next step. Not on the V-1-4K.",
            },
            "sequencer_previous": {
                "label": "Sequencer Back",
                "params": {},
                "help": "Steps the sequencer back one step. Not on the V-1-4K.",
            },
            "sequencer_to_start": {
                "label": "Sequencer to Start",
                "params": {},
                "help": "Takes the sequencer back to its first step. Not on the V-1-4K.",
            },
            "sequencer_jump": {
                "label": "Sequencer Jump",
                "params": {
                    "step": {
                        "type": "integer", "label": "Step", "required": True, "min": 0, "max": 1000,
                        "help": "1-1000, or 0 for START.",
                    },
                },
                "help": "Jumps the sequencer to the step. Not on the V-1-4K.",
            },
            "graphics_next": {
                "label": "Graphics Presenter Next",
                "params": {},
                "help": "Selects the next content in Graphics Presenter.",
            },
            "graphics_select": {
                "label": "Graphics Presenter Select",
                "params": {"content": {"type": "integer", "label": "Content", "required": True, "min": 1, "max": 124}},
                "help": "Selects the content in Graphics Presenter.",
            },
            "graphics_hide_front": {
                "label": "Graphics Presenter Hide Front",
                "params": {},
                "help": "Hides the front contents in Graphics Presenter.",
            },
            "graphics_hide_background": {
                "label": "Graphics Presenter Hide Background",
                "params": {},
                "help": "Hides the background contents in Graphics Presenter.",
            },
            "graphics_toggle_on_air": {
                "label": "Graphics Presenter On Air",
                "params": {},
                "help": "Puts Graphics Presenter on air, or takes it off when on air.",
            },
            # ── System ──
            "set_test_pattern": {
                "label": "Set Test Pattern",
                "params": {
                    "pattern": {
                        "type": "string", "label": "Pattern", "required": True,
                        "options_state": "test_pattern_options", "pattern": r"^[A-Za-z0-9-]+$",
                        "help": "OFF or a pattern (COLORBAR on the V-1-4K).",
                    },
                },
                "sets": {"test_pattern": "{pattern}"},
                "help": "Shows a test pattern on the outputs, or OFF to go back.",
            },
            "set_test_tone": {
                "label": "Set Test Tone",
                "params": {
                    "level": {
                        "type": "enum", "label": "Level", "required": True,
                        "values": [
                            {"value": "OFF", "label": "Off"},
                            {"value": "-20", "label": "-20 dB"},
                            {"value": "-10", "label": "-10 dB"},
                            {"value": "0dB", "label": "0 dB"},
                        ],
                    },
                    "left": {
                        "type": "enum", "label": "Frequency L",
                        "values": [
                            {"value": "500", "label": "500 Hz"},
                            {"value": "1k", "label": "1 kHz"},
                            {"value": "2kHz", "label": "2 kHz"},
                        ],
                        "help": "Blank leaves the frequencies as they are.",
                    },
                    "right": {
                        "type": "enum", "label": "Frequency R",
                        "values": [
                            {"value": "500", "label": "500 Hz"},
                            {"value": "1k", "label": "1 kHz"},
                            {"value": "2kHz", "label": "2 kHz"},
                        ],
                        "help": "Give both frequencies or neither.",
                    },
                },
                "sets": {"test_tone_level": "{level}"},
                "help": "Plays a test tone on the outputs at the level, or OFF to stop it.",
            },
        },
    }

    # ACS; is the document's no-op ("Return ACK"); any answer counts.
    HEALTH_INTERVAL_S = 15.0
    # Covers waiting behind a request in progress and a late reply.
    HEALTH_TIMEOUT_S = 10.0
    HEALTH_FAULT_MESSAGE = "The switcher stopped answering."

    # ── Lifecycle ──

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._rx = ""
        self._pending: _Pending | None = None
        self._lock = asyncio.Lock()
        self._trailing_ack: bool | None = None
        self._owed_acks = 0
        self._session_up = False
        self._unsolicited: list[str] = []
        self._spec: dict[str, Any] = MODELS[DEFAULT_MODEL]
        self._fast: list[str] = []
        self._operational: list[str] = []
        self._setup: list[str] = []
        self._op_index = 0
        self._setup_index = 0
        self._unsupported: set[str] = set()
        self._read_problems: dict[str, str] = {}
        self._standing: dict[str, str] = {}
        super().__init__(device_id, config, state, events)

    def _over_network(self) -> bool:
        # Read here rather than through the platform's _effective_transport()
        # so the choice is made the same way on every release this installs on.
        chosen = self.config.get("transport") or self.DRIVER_INFO["transport"]
        return str(chosen).lower() != "serial"

    def _transport_kwargs(self, transport_type: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        # Raw bytes: messages end at ";", and the driver frames them itself.
        kwargs["delimiter"] = None
        kwargs["frame_parser"] = None
        return kwargs

    async def _pre_connect(self) -> None:
        self._rx = ""
        self._pending = None
        self._trailing_ack = None
        self._owed_acks = 0
        self._session_up = False
        self._unsolicited = []
        self._unsupported = set()
        self._read_problems = {}

    async def _post_connect(self) -> None:
        network = self._over_network()
        password = str(self.config.get("password") or "")
        sent_password = False
        if network and password:
            if "\r" in password or "\n" in password:
                raise ConnectionFaultError(
                    "The network password holds a line break, which the switcher cannot take.",
                    code="invalid_config",
                )
            # Roland's support pages: the password and a line feed, before the
            # commands. What the switcher answers to it is not documented.
            await self._write(password + "\n", raw=True)
            sent_password = True
            await asyncio.sleep(SIGN_IN_SETTLE_S)
            if self._unsolicited:
                log.info(
                    f"[{self.device_id}] The switcher answered the network password with "
                    f"{', '.join(self._unsolicited)}"
                )
        try:
            ok, words = await self._query("VER", timeout=VERSION_TIMEOUT_S)
        except (ConnectionError, asyncio.TimeoutError):
            raise ConnectionFaultError(self._silent_sentence(network, sent_password), code="no_response") from None
        if not ok:
            if network:
                if sent_password:
                    raise ConnectionFaultError(
                        f"The switcher refused the version query ({words}) after the network "
                        "password was sent.",
                        code="auth_failed",
                    )
                raise ConnectionFaultError(
                    f"The switcher refused the version query ({words}), and no network "
                    "password is entered. LAN control needs the password set on the switcher.",
                    code="auth_failed",
                )
            raise ConnectionFaultError(
                f"The switcher refused the version query on RS-232 ({words}).",
                code="no_response",
            )
        self._session_up = True

    def _silent_sentence(self, network: bool, sent_password: bool) -> str:
        if not network:
            return (
                "The switcher did not answer the version query on RS-232. RS-232 must be "
                "on in its menu, at the baud rate set here, through a crossover cable."
            )
        if sent_password:
            return "The switcher did not answer the version query after the network password was sent."
        return (
            "The switcher did not answer the version query, and no network password is "
            "entered. Its LAN control answers only once a password is set on it and here."
        )

    async def _initial_sync(self) -> None:
        self._op_index = 0
        self._setup_index = 0
        # Every reading once, in order, before the poll's rotation takes over.
        for line in self._fast + self._operational + self._setup:
            await self._read(line)
        self._write_last_error()

    async def _close_session(self) -> None:
        self._session_up = False
        pending, self._pending = self._pending, None
        if pending is not None:
            for future in (pending.future, pending.ack):
                if future is not None and not future.done():
                    future.set_exception(ConnectionError("connection closed"))
                    future.exception()
        self._rx = ""

    # ── Model ──

    def _apply_model(self, reported: str) -> None:
        name = reported if reported in MODELS else None
        if name is None:
            configured = str(self.config.get("model") or "")
            name = configured if configured in MODELS else DEFAULT_MODEL
            self._standing["model"] = (
                f"The switcher reports itself as '{reported}', which this driver does not "
                f"list; it is treated as a {name}."
            )
        else:
            self._standing.pop("model", None)
        self._spec = spec = MODELS[name]
        self._reconcile("video_input", [_local(w) for w in spec["video_inputs"]])
        self._reconcile("input_channel", list(range(1, spec["channels"] + 1)))
        self._reconcile("video_output", [_local(w) for w in spec["video_outputs"]])
        self._reconcile("aux", list(range(1, spec["aux"] + 1)))
        self._reconcile("pinp", list(range(1, spec["pinp"] + 1)))
        self._reconcile("dsk", list(range(1, spec["dsk"] + 1)))
        self._reconcile("split", list(range(1, spec["split"] + 1)))
        self._reconcile("roi", list(range(1, spec["roi"] + 1)))
        self._reconcile("audio_input", [_local(w) for w in spec["audio_inputs"]])
        self._reconcile("audio_output", [_local(w) for w in spec["audio_outputs"]])
        self._reconcile("audio_jack", [_local(w) for w in spec["audio_jacks"]])
        self._reconcile("gpo", list(range(1, spec["gpo"] + 1)))
        cameras = min(_int(self.config.get("camera_count")) or 0, spec["cameras"])
        self._reconcile("camera", list(range(1, cameras + 1)))
        self.set_states({
            "source_options": _options(spec["sources"]),
            "aux_source_options": _options(spec["aux_sources"]),
            "input_assign_options": _options(spec["input_assign"]),
            "output_assign_options": _options(spec["output_assign"]),
            "still_output_options": _options(spec["still_output"]),
            "transition_target_options": _options(spec["transition_targets"]),
            "audio_jack_options": _options(spec["jack_assign"]),
            "test_pattern_options": _options(spec["patterns"]),
            "audio_player_pad_options": _options(spec["pads"]),
        })
        self._build_reads()

    def _reconcile(self, child_type: str, wanted: list[Any]) -> None:
        for local in self.list_children(child_type):
            if local not in wanted:
                self.deregister_child(child_type, local)
        for local in wanted:
            if not self.is_child_registered(child_type, local):
                self.register_child(child_type, local)

    def _build_reads(self) -> None:
        spec = self._spec
        fast = ["QPGM;", "QPST;", "QATG;", "QFTB;"]
        fast += [f"QPPS:PinP{n};" for n in range(1, spec["pinp"] + 1)]
        fast += [f"QDSK:DSK{n};" for n in range(1, spec["dsk"] + 1)]
        fast += [f"QAUX:AUX{n};" for n in range(1, spec["aux"] + 1)]
        fast.append("QMEM;")
        if spec["freeze"]:
            fast.append("QFRZ;")
        if spec["streaming"]:
            fast.append("QSTRST;")

        op = ["QVFL;", "QTRS;"]
        op += [f"QVIST:{w};" for w in spec["video_inputs"]]
        for w in spec["audio_inputs"]:
            op += [f"QIAL:{w};", f"QIAM:{w};", f"QIAS:{w};"]
        for w in spec["audio_outputs"]:
            op += [f"QOAL:{w};", f"QOAM:{w};"]
        for n in range(1, spec["pinp"] + 1):
            if spec["pinp_source"]:
                op.append(f"QPIS:PinP{n};")
            op.append(f"QPPW:PinP{n};")
        for n in range(1, spec["dsk"] + 1):
            op += [f"QDVW:DSK{n};", f"QDSS:DSK{n};"]
        op += [f"QSPS:SPLIT{n};" for n in range(1, spec["split"] + 1)]
        if spec["still_output"]:
            op.append("QSTO;")
        op += ["QASW;", "QATM;"]
        if spec["reverb"]:
            op.append("QRVB;")
        if spec["roi"]:
            op.append("QROISW;")
        if spec["streaming"]:
            op += ["QSTRTM;", "QSFIM;"]
        if spec["macros"]:
            op += ["QSEQSW;", "QSEQAS;"]

        setup = [f"QVIS:INPUT{n};" for n in range(1, spec["channels"] + 1)]
        setup += [f"QVOS:{w};" for w in spec["video_outputs"]]
        setup += [f"QAOS:{w};" for w in spec["audio_jacks"]]
        for w in spec["audio_inputs"]:
            setup += [f"QADT:{w};", f"QHPF:{w};", f"QGATE:{w};"]
        setup += [f"QSTLK:{w};" for w in spec["stereo_link"]]
        setup += [f"QVOCH:{w};" for w in spec["voice_changer"]]
        for target in spec["transition_targets"]:
            if target.startswith("DSK") and spec["dsk_time_query"]:
                target = spec["dsk_time_query"]
            setup.append(f"QTIM:{target};")
        setup += [f"QPIP:PinP{n};" for n in range(1, spec["pinp"] + 1)]
        for n in range(1, spec["dsk"] + 1):
            setup += [f"QKYL:DSK{n};", f"QKYG:DSK{n};"]
        setup += [f"QSPT:SPLIT{n};" for n in range(1, spec["split"] + 1)]
        for n in range(1, spec["roi"] + 1):
            setup += [f"QROIZM:ROI{n};", f"QROIPT:ROI{n};"]
        setup += [f"QGPO:GPO{n};" for n in range(1, spec["gpo"] + 1)]
        for n in self.list_children("camera"):
            setup += [f"QCAMPTS:CAMERA{n};", f"QCAMAFC:CAMERA{n};", f"QCAMAEP:CAMERA{n};", f"QCAMPR:CAMERA{n};"]
        setup += ["QHDCP;", "QTPT;", "QTTN;"]
        if spec["streaming"]:
            setup += ["QSTRTY;", "QEXRC;", "QPBTY;", "QVPS;", "QVPE;", "QVPD;", "QVPC;", "QSRTIST;"]

        self._fast, self._operational, self._setup = fast, op, setup
        self._op_index = 0
        self._setup_index = 0

    # ── Requests (one at a time) ──

    async def _write(self, text: str, raw: bool = False) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if not raw and not self._over_network():
            # RS-232: STX before each command (V-160HD, VR-120HD and VR-6HD
            # guides; the V-80HD guide shows none, unverified there).
            text = STX + text
        await self.transport.send(text.encode("ascii", errors="replace"))

    async def _request(self, line: str, want: str, timeout: float = REQUEST_TIMEOUT_S) -> tuple[bool, str]:
        """Send one command line and wait for its answer.

        ``want`` is the reply code a query waits for, or "ACK" for a set.
        Returns (True, value) for an answer, (False, words) for an ERR. Silence
        raises ConnectionError.
        """
        async with self._lock:
            await self._settle_late_reply()
            loop = asyncio.get_running_loop()
            pending = _Pending(line, want, loop.create_future())
            expects_ack = want != "ACK" and want not in NO_TRAILING_ACK and self._trailing_ack is not False
            if expects_ack:
                pending.ack = loop.create_future()
            self._pending = pending
            await self._write(line + "\n")
            try:
                kind, value = await asyncio.wait_for(asyncio.shield(pending.future), timeout)
            except asyncio.TimeoutError:
                # Left pending, so a late answer lands on it and not on the
                # next request.
                raise ConnectionError(
                    f"[{self.device_id}] The switcher did not answer '{line}' within {timeout:.0f} s"
                ) from None
            if kind == "ERR":
                self._pending = None
                return False, value
            if pending.ack is not None:
                wait = ACK_FIRST_WAIT_S if self._trailing_ack is None else ACK_WAIT_S
                try:
                    await asyncio.wait_for(asyncio.shield(pending.ack), wait)
                    self._trailing_ack = True
                except asyncio.TimeoutError:
                    if self._trailing_ack is None:
                        self._trailing_ack = False
                        log.debug(f"[{self.device_id}] No ACK follows a query's value")
                    else:
                        self._owed_acks += 1
            if self._pending is pending:
                self._pending = None
            return True, value

    async def _settle_late_reply(self) -> None:
        stale = self._pending
        if stale is None:
            return
        if not stale.future.done():
            try:
                await asyncio.wait_for(asyncio.shield(stale.future), LATE_REPLY_S)
            except asyncio.TimeoutError:
                self._force_disconnect(
                    "no_response", f"The switcher stopped answering ('{stale.line}' got no reply).",
                )
                raise ConnectionError(f"[{self.device_id}] The switcher stopped answering") from None
            except Exception:
                pass
        if stale.value_seen and stale.ack is not None and not stale.ack.done() and self._trailing_ack:
            # Its value came late; the ACK after it is still on the way.
            self._owed_acks += 1
        if self._pending is stale:
            self._pending = None

    async def _query(self, code: str, arg: str | None = None, timeout: float = REQUEST_TIMEOUT_S) -> tuple[bool, str]:
        line = f"{code}:{arg};" if arg is not None else f"{code};"
        return await self._request(line, _reply_code(code), timeout)

    async def _read(self, line: str) -> None:
        """A poll read: a refusal is noted, never raised; silence raises."""
        if line in self._unsupported:
            return
        code = line.split(":", 1)[0].rstrip(";")
        ok, words = await self._request(line, _reply_code(code))
        if ok:
            self._read_problems.pop(line, None)
            return
        if words.startswith("ERR:0"):
            # The switcher does not take this read: not asked again on this
            # connection.
            self._unsupported.add(line)
            log.info(f"[{self.device_id}] '{line}' is not taken by this switcher ({words}); not read again")
            return
        if code in ("QCAMPTS", "QCAMAFC", "QCAMAEP", "QCAMPR", "QVPS", "QVPE", "QVPD", "QVPC", "QSRTIST"):
            # A camera the switcher is not set up for, or a player in the
            # other mode: an answer, not a fault.
            return
        self._read_problems[line] = f"'{line}' refused ({words})."

    async def _command(self, line: str, label: str) -> bool:
        ok, words = await self._request(line, "ACK")
        if not ok:
            raise CommandRefusedError(f"The switcher refused {label} ({words}).")
        return True

    async def _command_then_read(self, line: str, label: str, *reads: str) -> bool:
        await self._command(line, label)
        for read in reads:
            await self._read(read)
        return True

    # ── Receiving ──

    def on_data_received(self, data: bytes) -> None:
        # Synchronous, so messages are applied in the order they arrive and a
        # waiting request is answered before the next message is read.
        self._rx += data.decode("ascii", errors="replace")
        while True:
            end = self._rx.find(";")
            if end < 0:
                break
            raw, self._rx = self._rx[:end], self._rx[end + 1:]
            self._message(raw)
        if len(self._rx) > RX_LIMIT:
            log.warning(f"[{self.device_id}] Dropped {len(self._rx)} characters with no ';'")
            self._rx = ""

    def _message(self, raw: str) -> None:
        # Whatever precedes the message on its line (a line feed, STX, text
        # with no ";" of its own) is not part of it.
        text = re.split(r"[\x02\r\n]", raw)[-1].replace("\x11", "").replace("\x13", "").strip(_STRIP)
        if not text:
            return
        match = _TOKEN.match(text)
        if not match:
            log.debug(f"[{self.device_id}] Unexpected message: {raw!r}")
            return
        code, args = match.group(1), match.group(2)
        pending = self._pending
        if code == "ACK":
            if self._owed_acks:
                self._owed_acks -= 1
                return
            if pending is not None:
                if pending.want == "ACK" and not pending.future.done():
                    pending.future.set_result(("ACK", ""))
                    return
                if pending.value_seen and pending.ack is not None and not pending.ack.done():
                    pending.ack.set_result(True)
                    return
            if not self._session_up:
                # An answer to the network password line, noted for the log.
                self._unsolicited.append("ACK")
            log.debug(f"[{self.device_id}] ACK with no request waiting")
            return
        if code == "ERR":
            words = _err_words(args)
            if pending is not None and not pending.future.done():
                pending.future.set_result(("ERR", words))
                return
            if not self._session_up:
                self._unsolicited.append(words)
            log.debug(f"[{self.device_id}] {words} with no request waiting")
            return
        if not self._session_up and code != "VER":
            self._unsolicited.append(text)
        values = [v.strip() for v in args.split(",")] if args is not None else []
        try:
            self._apply(code, values)
        except Exception as exc:  # a malformed value is logged, never fatal
            log.warning(f"[{self.device_id}] Could not read {text!r}: {exc}")
        if pending is not None and pending.want == code and not pending.future.done():
            pending.value_seen = True
            pending.future.set_result(("DATA", args or ""))

    # ── Applying what the switcher reports ──

    def _apply(self, code: str, v: list[str]) -> None:
        first = v[0] if v else ""
        if code == "VER":
            if first:
                self.set_states({"model": first, "firmware_version": v[1] if len(v) > 1 else ""})
            self._apply_model(first)
        elif code in ("PGM", "PST"):
            key = code.lower()
            self.set_states({f"{key}_source": first, f"{key}_channel": v[1] if len(v) > 1 else ""})
        elif code == "AUX":
            n = _number(first, "AUX")
            self._child("aux", n, {"source": v[1] if len(v) > 1 else "", "channel": v[2] if len(v) > 2 else ""})
        elif code == "VIST":
            self._child("video_input", _local(first), {"signal": _signal(v[1] if len(v) > 1 else "")})
        elif code == "VFL":
            self.set_state("video_fader", _int(first))
        elif code == "ATG":
            self.set_state("transition_running", _on(first))
        elif code == "FRZ":
            self.set_state("freeze", _on(first))
        elif code == "FTB":
            self.set_state("output_fade", first.lower() if first else None)
        elif code == "VIS":
            self._child("input_channel", _number(first, "INPUT"), {"assign": v[1] if len(v) > 1 else ""})
        elif code == "VOS":
            self._child("video_output", _local(first), {"assign": v[1] if len(v) > 1 else ""})
        elif code == "TRS":
            self.set_state("transition_type", first.lower() if first else None)
        elif code == "TIM":
            seconds = _tenths(v[1] if len(v) > 1 else "")
            target = first.upper()
            if target == "MIX":
                self.set_state("mix_time", seconds)
            elif target == "WIPE":
                self.set_state("wipe_time", seconds)
            elif target == "OUTPUTFADE":
                self.set_state("output_fade_time", seconds)
            elif target.startswith("PINP"):
                self._child("pinp", _number(first, "PinP"), {"transition_time": seconds})
            elif target.startswith("DSK"):
                self._child("dsk", _number(first, "DSK") or 1, {"transition_time": seconds})
        elif code == "PIS":
            self._child("pinp", _number(first, "PinP"), {"source": v[1] if len(v) > 1 else ""})
        elif code == "PPS":
            self._child("pinp", _number(first, "PinP"), {"pgm": _on(v[1] if len(v) > 1 else "")})
        elif code == "PPW":
            self._child("pinp", _number(first, "PinP"), {"pvw": _on(v[1] if len(v) > 1 else "")})
        elif code == "PIP":
            self._child("pinp", _number(first, "PinP"), {
                "position_h": _tenths(v[1] if len(v) > 1 else ""),
                "position_v": _tenths(v[2] if len(v) > 2 else ""),
            })
        elif code == "DSK":
            self._child("dsk", _number(first, "DSK"), {"pgm": _on(v[1] if len(v) > 1 else "")})
        elif code == "DVW":
            self._child("dsk", _number(first, "DSK"), {"pvw": _on(v[1] if len(v) > 1 else "")})
        elif code == "DSS":
            self._child("dsk", _number(first, "DSK"), {
                "fill_source": v[1] if len(v) > 1 else "",
                "fill_channel": v[2] if len(v) > 2 else "",
            })
        elif code == "KYL":
            self._child("dsk", _number(first, "DSK"), {"level": _int(v[1] if len(v) > 1 else "")})
        elif code == "KYG":
            self._child("dsk", _number(first, "DSK"), {"gain": _int(v[1] if len(v) > 1 else "")})
        elif code == "SPS":
            self._child("split", _number(first, "SPLIT"), {"on": _on(v[1] if len(v) > 1 else "")})
        elif code == "SPT":
            self._child("split", _number(first, "SPLIT"), {
                "pgm_center": _tenths(v[1] if len(v) > 1 else ""),
                "pst_center": _tenths(v[2] if len(v) > 2 else ""),
                "center_position": _tenths(v[3] if len(v) > 3 else ""),
            })
        elif code == "STO":
            self.set_state("still_output", first)
        elif code == "ROISW":
            self.set_state("roi_mode", _on(first))
        elif code == "ROIZM":
            self._child("roi", _number(first, "ROI"), {"zoom": _tenths(v[1] if len(v) > 1 else "")})
        elif code == "ROIPT":
            self._child("roi", _number(first, "ROI"), {
                "position_h": _int(v[1] if len(v) > 1 else ""),
                "position_v": _int(v[2] if len(v) > 2 else ""),
            })
        elif code == "AOS":
            self._child("audio_jack", _local(first), {"assign": v[1] if len(v) > 1 else ""})
        elif code in ("IAL", "OAL"):
            ctype = "audio_input" if code == "IAL" else "audio_output"
            self._child(ctype, _local(first), {"level": _level(v[1] if len(v) > 1 else "")})
        elif code in ("IAM", "OAM"):
            ctype = "audio_input" if code == "IAM" else "audio_output"
            self._child(ctype, _local(first), {"mute": _on(v[1] if len(v) > 1 else "")})
        elif code in ("IAS", "HPF", "GATE", "STLK", "VOCH"):
            prop = {"IAS": "solo", "HPF": "high_pass_filter", "GATE": "gate",
                    "STLK": "stereo_link", "VOCH": "voice_changer"}[code]
            self._child("audio_input", _local(first), {prop: _on(v[1] if len(v) > 1 else "")})
        elif code == "ADT":
            self._child("audio_input", _local(first), {"delay": _tenths(v[1] if len(v) > 1 else "")})
        elif code in ("RVB", "ATM", "ASW", "HCP", "EXRC", "SQS", "SQA"):
            key = {"RVB": "reverb", "ATM": "auto_mixing", "ASW": "auto_switching", "HCP": "hdcp",
                   "EXRC": "external_rec_control", "SQS": "sequencer", "SQA": "auto_sequence"}[code]
            self.set_state(key, _on(first))
        elif code == "MEM":
            self.set_state("memory", _number(first, "MEMORY"))
        elif code == "GPO":
            self._child("gpo", _number(first, "GPO"), {"on": _on(v[1] if len(v) > 1 else "")})
        elif code == "TPT":
            self.set_state("test_pattern", first)
        elif code == "TTN":
            self.set_states({
                "test_tone_level": first,
                "test_tone_left": v[1] if len(v) > 1 else "",
                "test_tone_right": v[2] if len(v) > 2 else "",
            })
        elif code == "STRTY":
            self.set_state("stream_type", first.lower() if first else None)
        elif code == "STRST":
            self.set_state("stream_status", first.lower() if first else None)
        elif code == "STRTM":
            self.set_state("stream_time", _int(first))
        elif code == "SFIM":
            self.set_state("safety_image", first.lower() if first else None)
        elif code == "PBTY":
            self.set_state("player_type", first.lower() if first else None)
        elif code == "VPS":
            self.set_state("video_player_status", first.lower() if first else None)
        elif code == "VPE":
            media = first.lower()
            self.set_state("video_player_media", "none" if media in ("n/a", "") else media)
        elif code == "VPD":
            self.set_state("video_player_length", _int(first))
        elif code == "VPC":
            self.set_state("video_player_position", _int(first))
        elif code == "SRTIST":
            self.set_state("srt_in_status", first.lower() if first else None)
        elif code == "CPTS":
            self._child("camera", _number(first, "CAMERA"), {"pan_tilt_speed": _int(v[1] if len(v) > 1 else "")})
        elif code == "CAFC":
            self._child("camera", _number(first, "CAMERA"), {"auto_focus": _on(v[1] if len(v) > 1 else "")})
        elif code == "CAEP":
            self._child("camera", _number(first, "CAMERA"), {"auto_exposure": _on(v[1] if len(v) > 1 else "")})
        elif code == "CPR":
            self._child("camera", _number(first, "CAMERA"), {"preset": _number(v[1] if len(v) > 1 else "", "PRESET")})

    def _child(self, child_type: str, local: Any, values: dict[str, Any]) -> None:
        if local is None or not self.is_child_registered(child_type, local):
            return
        self.set_child_state_batch(child_type, local, values)

    # ── Polling ──

    async def poll(self) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        for line in self._fast:
            await self._read(line)
        interval = float(_number_or(self.config.get("poll_interval"), 1.0))
        budget = max(interval * POLL_BUDGET, 0.0)
        total = len(self._operational) + len(self._setup)
        for reads in range(min(SLOW_READS, total)):
            if reads and loop.time() - started >= budget:
                break
            if self._setup and (not self._operational or reads % (OPERATIONAL_SHARE + 1) == OPERATIONAL_SHARE):
                line = self._setup[self._setup_index % len(self._setup)]
                self._setup_index += 1
            else:
                line = self._operational[self._op_index % len(self._operational)]
                self._op_index += 1
            await self._read(line)
        self._write_last_error()

    def _write_last_error(self) -> None:
        # Standing conditions are written again on every poll while they hold.
        problems = list(self._standing.values()) + list(self._read_problems.values())
        if problems:
            self.set_state("last_error", " ".join(problems[:4]))

    async def refresh_children(self) -> dict[str, Any]:
        await self._query("VER", timeout=VERSION_TIMEOUT_S)
        for line in self._fast + self._operational + self._setup:
            await self._read(line)
        return {"model": self.get_state("model")}

    async def _liveness_probe(self) -> None:
        # Any answer counts, an ERR included. The platform's deadline governs
        # the wait (on a reconnect held offline it is HEALTH_INTERVAL_S, so a
        # switcher still starting comes online at its late answer); the
        # request's own timeout is longer than either deadline.
        await self._request(
            "ACS;", "ACK", timeout=float(self.HEALTH_INTERVAL_S) + float(self.HEALTH_TIMEOUT_S),
        )

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        p = params or {}
        spec = self._spec
        if command == "select_pgm":
            source = _token_param(p.get("source"), "Source")
            return await self._command_then_read(f"PGM:{source};", f"program {source}", "QPGM;")
        if command == "select_pst":
            source = _token_param(p.get("source"), "Source")
            return await self._command_then_read(f"PST:{source};", f"preset {source}", "QPST;")
        if command == "cut":
            source = _optional_token(p.get("source"))
            line = f"CUT:{source};" if source else "CUT;"
            return await self._command_then_read(line, "Cut", "QPGM;", "QPST;")
        if command == "auto_take":
            source = _optional_token(p.get("source"))
            time = p.get("time")
            if time not in (None, "") and not source:
                raise ValueError("A transition time needs a source")
            if source and time not in (None, ""):
                line = f"ATO:{source},{_to_tenths(time, 0, 4)};"
            elif source:
                line = f"ATO:{source};"
            else:
                line = "ATO;"
            # The transition runs on; program and preset are read by the poll.
            return await self._command_then_read(line, "Auto Transition", "QATG;")
        if command == "set_video_fader":
            level = _bounded_int(p.get("level"), 0, spec["fader_max"], "Level")
            return await self._command_then_read(f"VFL:{level};", "the video fader", "QVFL;", "QPGM;", "QPST;")
        if command == "set_transition_type":
            kind = str(p.get("type") or "").strip().upper()
            if kind not in ("MIX", "WIPE"):
                raise ValueError("The transition type is mix or wipe")
            return await self._command_then_read(f"TRS:{kind};", "the transition type", "QTRS;")
        if command == "set_transition_time":
            target = _token_param(p.get("target"), "Transition")
            tenths = _to_tenths(p.get("time"), 0, 4)
            read = target
            if target.upper().startswith("DSK") and spec["dsk_time_query"]:
                read = spec["dsk_time_query"]
            return await self._command_then_read(f"TIM:{target},{tenths};", f"{target} transition time", f"QTIM:{read};")
        if command == "set_output_fade":
            return await self._command_then_read(f"FTB:{_word(p.get('on'))};", "Fade to Black", "QFTB;")
        if command == "toggle_output_fade":
            return await self._command_then_read("FTB;", "Fade to Black", "QFTB;")
        if command == "set_freeze":
            return await self._command_then_read(f"FRZ:{_word(p.get('on'))};", "Freeze", "QFRZ;")
        if command == "toggle_freeze":
            return await self._command_then_read("FRZ;", "Freeze", "QFRZ;")
        if command == "set_still_output":
            source = _token_param(p.get("source"), "Still")
            return await self._command_then_read(f"STO:{source};", "the still image output", "QSTO;")
        if command == "assign_input":
            n = _child_int(p.get("channel"), "Input Channel")
            source = _token_param(p.get("source"), "Source", allow="/")
            return await self._command_then_read(f"VIS:INPUT{n},{source};", f"input {n}'s assignment", f"QVIS:INPUT{n};")
        if command == "assign_video_output":
            out = _wire(_child_word(p.get("output"), "Output"))
            assign = _token_param(p.get("assign"), "Shows")
            return await self._command_then_read(f"VOS:{out},{assign};", f"output {out}'s assignment", f"QVOS:{out};")
        if command == "set_aux":
            n = _child_int(p.get("aux"), "AUX")
            source = _token_param(p.get("source"), "Source")
            return await self._command_then_read(f"AUX:AUX{n},{source};", f"AUX {n}", f"QAUX:AUX{n};")
        if command in ("set_pinp_pgm", "toggle_pinp_pgm", "set_pinp_pvw", "toggle_pinp_pvw"):
            n = _child_int(p.get("pinp"), "PinP & Key")
            code = "PPS" if "pgm" in command else "PPW"
            line = f"{code}:PinP{n};" if command.startswith("toggle") else f"{code}:PinP{n},{_word(p.get('on'))};"
            return await self._command_then_read(line, f"PinP {n}", f"Q{code}:PinP{n};")
        if command == "set_pinp_source":
            n = _child_int(p.get("pinp"), "PinP & Key")
            source = _token_param(p.get("source"), "Source")
            return await self._command_then_read(f"PIS:PinP{n},{source};", f"PinP {n}'s source", f"QPIS:PinP{n};")
        if command == "set_pinp_position":
            n = _child_int(p.get("pinp"), "PinP & Key")
            h = _to_tenths(p.get("position_h"), -50, 50)
            v = _to_tenths(p.get("position_v"), -50, 50)
            return await self._command_then_read(f"PIP:PinP{n},{h},{v};", f"PinP {n}'s position", f"QPIP:PinP{n};")
        if command in ("set_dsk_pgm", "toggle_dsk_pgm", "set_dsk_pvw", "toggle_dsk_pvw"):
            n = _child_int(p.get("dsk"), "DSK")
            code = "DSK" if "pgm" in command else "DVW"
            line = f"{code}:DSK{n};" if command.startswith("toggle") else f"{code}:DSK{n},{_word(p.get('on'))};"
            return await self._command_then_read(line, f"DSK {n}", f"Q{code}:DSK{n};")
        if command == "set_dsk_source":
            n = _child_int(p.get("dsk"), "DSK")
            source = _token_param(p.get("source"), "Fill Source")
            return await self._command_then_read(f"DSS:DSK{n},{source};", f"DSK {n}'s fill", f"QDSS:DSK{n};")
        if command in ("set_dsk_level", "set_dsk_gain"):
            n = _child_int(p.get("dsk"), "DSK")
            code, name = ("KYL", "level") if command == "set_dsk_level" else ("KYG", "gain")
            value = _bounded_int(p.get(name), 0, 255, name.title())
            return await self._command_then_read(f"{code}:DSK{n},{value};", f"DSK {n}'s {name}", f"Q{code}:DSK{n};")
        if command in ("set_split", "toggle_split"):
            n = _child_int(p.get("split"), "Split")
            line = f"SPS:SPLIT{n};" if command == "toggle_split" else f"SPS:SPLIT{n},{_word(p.get('on'))};"
            return await self._command_then_read(line, f"Split {n}", f"QSPS:SPLIT{n};")
        if command == "set_split_positions":
            n = _child_int(p.get("split"), "Split")
            a = _to_tenths(p.get("pgm_center"), -50, 50)
            b = _to_tenths(p.get("pst_center"), -50, 50)
            c = p.get("center_position")
            line = f"SPT:SPLIT{n},{a},{b};" if c in (None, "") else f"SPT:SPLIT{n},{a},{b},{_to_tenths(c, -50, 50)};"
            return await self._command_then_read(line, f"Split {n}'s positions", f"QSPT:SPLIT{n};")
        if command == "set_roi_mode":
            return await self._command_then_read(f"ROISW:{_word(p.get('on'))};", "ROI mode", "QROISW;")
        if command == "set_roi_zoom":
            n = _child_int(p.get("roi"), "ROI")
            zoom = _to_tenths(p.get("zoom"), 100, 400)
            return await self._command_then_read(f"ROIZM:ROI{n},{zoom};", f"ROI {n}'s zoom", f"QROIZM:ROI{n};")
        if command == "set_roi_position":
            n = _child_int(p.get("roi"), "ROI")
            h = _bounded_int(p.get("position_h"), -1920, 1920, "Position H")
            v = _bounded_int(p.get("position_v"), -1080, 1080, "Position V")
            return await self._command_then_read(f"ROIPT:ROI{n},{h},{v};", f"ROI {n}'s position", f"QROIPT:ROI{n};")
        # Audio
        if command in ("set_input_level", "set_output_level"):
            key = "input" if command == "set_input_level" else "output"
            target = _wire(_child_word(p.get(key), key.title()))
            code = "IAL" if key == "input" else "OAL"
            return await self._command_then_read(
                f"{code}:{target},{_level_wire(p.get('level'))};", f"{target}'s level", f"Q{code}:{target};",
            )
        if command in ("set_input_mute", "toggle_input_mute", "set_output_mute"):
            key = "output" if command == "set_output_mute" else "input"
            target = _wire(_child_word(p.get(key), key.title()))
            code = "OAM" if key == "output" else "IAM"
            line = f"IAM:{target};" if command == "toggle_input_mute" else f"{code}:{target},{_word(p.get('muted'))};"
            return await self._command_then_read(line, f"{target}'s mute", f"Q{code}:{target};")
        if command in ("set_input_solo", "toggle_input_solo"):
            target = _wire(_child_word(p.get("input"), "Input"))
            line = f"IAS:{target};" if command == "toggle_input_solo" else f"IAS:{target},{_word(p.get('on'))};"
            return await self._command_then_read(line, f"{target}'s solo", f"QIAS:{target};")
        if command == "set_input_delay":
            target = _wire(_child_word(p.get("input"), "Input"))
            delay = _to_tenths(p.get("delay"), 0, 500)
            return await self._command_then_read(f"ADT:{target},{delay};", f"{target}'s delay", f"QADT:{target};")
        if command in ("set_input_high_pass_filter", "set_input_gate", "set_stereo_link", "set_voice_changer"):
            target = _wire(_child_word(p.get("input"), "Input"))
            code = {"set_input_high_pass_filter": "HPF", "set_input_gate": "GATE",
                    "set_stereo_link": "STLK", "set_voice_changer": "VOCH"}[command]
            return await self._command_then_read(
                f"{code}:{target},{_word(p.get('on'))};", f"{target}'s {code}", f"Q{code}:{target};",
            )
        if command == "assign_audio_output":
            jack = _wire(_child_word(p.get("jack"), "Jack"))
            bus = _token_param(p.get("bus"), "Bus")
            return await self._command_then_read(f"AOS:{jack},{bus};", f"{jack}'s bus", f"QAOS:{jack};")
        if command in ("set_reverb", "toggle_reverb", "set_auto_mixing", "toggle_auto_mixing",
                       "set_auto_switching", "toggle_auto_switching"):
            code = {"reverb": "RVB", "auto_mixing": "ATM", "auto_switching": "ASW"}[command.split("_", 1)[1]]
            line = f"{code};" if command.startswith("toggle") else f"{code}:{_word(p.get('on'))};"
            return await self._command_then_read(line, command.split("_", 1)[1].replace("_", " "), f"Q{code};")
        # Control
        if command == "recall_memory":
            n = _bounded_int(p.get("memory"), 1, spec["memories"], "Memory")
            return await self._command_then_read(f"MEM:MEMORY{n};", f"scene memory {n}", "QMEM;")
        if command in ("input_scan", "memory_scan"):
            order = _order(p.get("order"))
            code = "INSC" if command == "input_scan" else "MEMSC"
            return await self._command(f"{code}:{order};", "the scan")
        if command == "pinp_scan":
            n = _child_int(p.get("pinp"), "PinP & Key")
            return await self._command(f"PPSC:PinP{n},{_order(p.get('order'))};", f"PinP {n}'s scan")
        if command == "dsk_scan":
            n = _child_int(p.get("dsk"), "DSK")
            return await self._command(f"DSKSC:DSK{n},{_order(p.get('order'))};", f"DSK {n}'s scan")
        if command == "pulse_gpo":
            n = _child_int(p.get("gpo"), "GPO")
            return await self._command(f"GPO:GPO{n};", f"GPO {n}")
        if command == "set_gpo":
            n = _child_int(p.get("gpo"), "GPO")
            return await self._command_then_read(f"GPO:GPO{n},{_word(p.get('on'))};", f"GPO {n}", f"QGPO:GPO{n};")
        # Stream & record, players
        if command == "set_stream_type":
            kind = str(p.get("type") or "").strip().upper()
            if kind not in ("RTMP", "SRT"):
                raise ValueError("The type is rtmp or srt")
            return await self._command_then_read(f"STRTY:{kind};", "the stream type", "QSTRTY;")
        if command in ("start_stream", "stop_stream"):
            word = "START" if command == "start_stream" else "STOP"
            return await self._command_then_read(f"STROA:{word};", "Stream & Record", "QSTRST;")
        if command in ("start_srt_out", "stop_srt_out"):
            word = "START" if command == "start_srt_out" else "STOP"
            return await self._command_then_read(f"SRTOSS:{word};", "SRT Out", "QSTRST;")
        if command == "set_safety_image":
            return await self._command_then_read(f"SFIM:{_word(p.get('on'))};", "the safety image", "QSFIM;")
        if command == "set_external_rec_control":
            return await self._command_then_read(f"EXRC:{_word(p.get('on'))};", "External Rec Control", "QEXRC;")
        if command in ("audio_player", "set_audio_player_repeat"):
            pad = str(p.get("pad") or "").strip().upper()
            if not re.fullmatch(r"[A-H]-[1-8]", pad):
                raise ValueError("The pad is a bank letter and a number, such as A-1")
            if command == "audio_player":
                action = _choice(p.get("action"), ("PLAY", "STOP", "PAUSE", "RESUME"), "Action")
                return await self._command(f"APL:{pad},{action};", f"pad {pad}")
            return await self._command(f"APR:{pad},{_word(p.get('on'))};", f"pad {pad}'s repeat")
        if command == "set_player_type":
            kind = str(p.get("type") or "").strip().upper()
            if kind not in ("V.PLAYER", "SRT"):
                raise ValueError("The type is v.player or srt")
            return await self._command_then_read(f"PBTY:{kind};", "the player type", "QPBTY;")
        if command == "video_player":
            action = _choice(p.get("action"), ("PLAY", "STOP", "PAUSE", "RESUME"), "Action")
            return await self._command_then_read(f"VPL:{action};", "the video player", "QVPS;")
        if command in ("start_srt_in", "stop_srt_in"):
            word = "START" if command == "start_srt_in" else "STOP"
            return await self._command_then_read(f"SRTISS:{word};", "SRT In", "QSRTIST;")
        # Cameras
        if command == "camera_pan_tilt":
            n = _child_int(p.get("camera"), "Camera")
            pan = _choice(p.get("pan"), ("LEFT", "STOP", "RIGHT"), "Pan")
            tilt = _choice(p.get("tilt"), ("DOWN", "STOP", "UP"), "Tilt")
            return await self._command(f"CAMPT:CAMERA{n},{pan},{tilt};", f"camera {n}")
        if command == "camera_stop":
            n = _child_int(p.get("camera"), "Camera")
            await self._command(f"CAMPT:CAMERA{n},STOP,STOP;", f"camera {n}")
            return await self._command(f"CAMZM:CAMERA{n},STOP;", f"camera {n}'s zoom")
        if command == "set_camera_speed":
            n = _child_int(p.get("camera"), "Camera")
            speed = _bounded_int(p.get("speed"), 1, 24, "Speed")
            return await self._command_then_read(f"CAMPTS:CAMERA{n},{speed};", f"camera {n}'s speed", f"QCAMPTS:CAMERA{n};")
        if command == "camera_zoom":
            n = _child_int(p.get("camera"), "Camera")
            direction = _choice(p.get("direction"), ("WIDE_FAST", "WIDE_SLOW", "STOP", "TELE_SLOW", "TELE_FAST"), "Zoom")
            return await self._command(f"CAMZM:CAMERA{n},{direction};", f"camera {n}'s zoom")
        if command == "camera_zoom_reset":
            n = _child_int(p.get("camera"), "Camera")
            return await self._command(f"CAMZMR:CAMERA{n};", f"camera {n}'s zoom reset")
        if command == "camera_focus":
            n = _child_int(p.get("camera"), "Camera")
            direction = _choice(p.get("direction"), ("NEAR", "STOP", "FAR"), "Focus")
            return await self._command(f"CAMFC:CAMERA{n},{direction};", f"camera {n}'s focus")
        if command in ("set_camera_auto_focus", "set_camera_auto_exposure"):
            n = _child_int(p.get("camera"), "Camera")
            code, query = ("CAMAFC", "QCAMAFC") if command == "set_camera_auto_focus" else ("CAMAEP", "QCAMAEP")
            return await self._command_then_read(
                f"{code}:CAMERA{n},{_word(p.get('on'))};", f"camera {n}", f"{query}:CAMERA{n};",
            )
        if command == "recall_camera_preset":
            n = _child_int(p.get("camera"), "Camera")
            preset = _bounded_int(p.get("preset"), 1, 100, "Preset")
            return await self._command_then_read(
                f"CAMPR:CAMERA{n},PRESET{preset};", f"camera {n}'s preset {preset}", f"QCAMPR:CAMERA{n};",
            )
        # Macros, sequencer, Graphics Presenter
        if command == "run_macro":
            n = _bounded_int(p.get("macro"), 1, 100, "Macro")
            return await self._command(f"MCREX:MACRO{n};", f"macro {n}")
        if command in ("set_sequencer", "set_auto_sequence"):
            code, query = ("SEQSW", "QSEQSW") if command == "set_sequencer" else ("SEQAS", "QSEQAS")
            return await self._command_then_read(f"{code}:{_word(p.get('on'))};", "the sequencer", f"{query};")
        if command == "sequencer_next":
            return await self._command("SEQNX;", "Sequencer Next")
        if command == "sequencer_previous":
            return await self._command("SEQPV;", "Sequencer Back")
        if command == "sequencer_to_start":
            return await self._command("SEQPV:1;", "Sequencer to Start")
        if command == "sequencer_jump":
            step = _bounded_int(p.get("step"), 0, 1000, "Step")
            return await self._command(f"SEQJP:{'START' if step == 0 else f'SEQ{step}'};", "Sequencer Jump")
        if command == "graphics_next":
            return await self._command("GPNC;", "Graphics Presenter Next")
        if command == "graphics_select":
            n = _bounded_int(p.get("content"), 1, 124, "Content")
            return await self._command(f"GPSC:CONTENT{n};", f"content {n}")
        if command == "graphics_hide_front":
            return await self._command("GPHF;", "Hide Front")
        if command == "graphics_hide_background":
            return await self._command("GPHB;", "Hide Background")
        if command == "graphics_toggle_on_air":
            return await self._command("GPOA;", "Graphics Presenter On Air")
        # System
        if command == "set_test_pattern":
            pattern = _token_param(p.get("pattern"), "Pattern", allow="-")
            return await self._command_then_read(f"TPT:{pattern};", "the test pattern", "QTPT;")
        if command == "set_test_tone":
            level = _choice(p.get("level"), ("OFF", "-20", "-10", "0DB"), "Level")
            level = "0dB" if level == "0DB" else level
            left, right = p.get("left"), p.get("right")
            if left in (None, "") and right in (None, ""):
                line = f"TTN:{level};"
            elif left in (None, "") or right in (None, ""):
                raise ValueError("Give both test tone frequencies or neither")
            else:
                line = f"TTN:{level},{_frequency(left)},{_frequency(right)};"
            return await self._command_then_read(line, "the test tone", "QTTN;")
        raise ValueError(f"Unknown command '{command}'")

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "hdcp":
            return await self._command_then_read(f"HDCP:{_word(value)};", "HDCP", "QHDCP;")
        raise ValueError(f"Unknown device setting '{key}'")


# ── Helpers ──


def _reply_code(query: str) -> str:
    if query in REPLY_CODE:
        return REPLY_CODE[query]
    return query[1:] if query.startswith("Q") else query


def _err_words(args: str | None) -> str:
    code = (args or "").strip()
    meaning = ERR_WORDS.get(code)
    return f"ERR:{code}, {meaning}" if meaning else f"ERR:{code}"


def _options(values: list[str]) -> str:
    return json.dumps(list(values))


def _on(text: str) -> bool | None:
    word = (text or "").strip().upper()
    if word == "ON":
        return True
    if word == "OFF":
        return False
    return None


def _word(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "on", "1", "yes"):
            return "ON"
        if text in ("false", "off", "0", "no"):
            return "OFF"
        raise ValueError(f"'{value}' is not on or off")
    if value is None:
        raise ValueError("On or off is required")
    return "ON" if bool(value) else "OFF"


def _int(text: Any) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _number_or(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tenths(text: str) -> float | None:
    value = _int(text)
    return None if value is None else round(value / 10.0, 1)


def _level(text: str) -> float | None:
    word = (text or "").strip().upper()
    if word in ("-INF", "INF"):
        return INF_DB
    return _tenths(word)


def _level_wire(value: Any) -> str:
    try:
        level = float(value)
    except (TypeError, ValueError):
        raise ValueError("The level is a number of dB") from None
    if level < LEVEL_MIN_DB:
        return "-INF"
    if level > LEVEL_MAX_DB:
        raise ValueError("The level is at most +10.0 dB")
    return str(int(round(level * 10)))


def _to_tenths(value: Any, low: float, high: float) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"A number from {low:g} to {high:g} is required") from None
    if number < low or number > high:
        raise ValueError(f"{number:g} is outside {low:g} to {high:g}")
    return int(round(number * 10))


def _bounded_int(value: Any, low: int, high: int, label: str) -> int:
    number = _int(value)
    if number is None:
        raise ValueError(f"{label} is a whole number")
    if number < low or number > high:
        raise ValueError(f"{label} {number} is outside {low} to {high} on this switcher")
    return number


def _number(text: str, prefix: str) -> int | None:
    match = re.fullmatch(re.escape(prefix) + r"\s*(\d+)", (text or "").strip(), re.I)
    return int(match.group(1)) if match else None


def _signal(text: str) -> str | None:
    return {"NOSIGNAL": "no_signal", "DETECTED": "detected", "UNSUPPORTED": "unsupported"}.get(
        (text or "").strip().upper()
    )


def _token_param(value: Any, label: str, allow: str = "") -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    if not re.fullmatch(r"[A-Za-z0-9." + re.escape(allow) + r"]+", text):
        raise ValueError(f"{label} '{text}' is not one of the switcher's names")
    # Every name the switcher takes is upper case except PinP.
    if text.upper().startswith("PINP"):
        return "PinP" + text[4:]
    return text.upper()


def _optional_token(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    return _token_param(text, "Source") if text else ""


def _child_int(value: Any, label: str) -> int:
    number = _int(value)
    if number is None or number < 1:
        raise ValueError(f"{label} is required")
    return number


def _child_word(value: Any, label: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9_]+", text):
        raise ValueError(f"{label} is required")
    return text.upper()


def _choice(value: Any, choices: tuple[str, ...], label: str) -> str:
    text = str(value if value is not None else "").strip().upper()
    if text not in choices:
        raise ValueError(f"{label} is one of {', '.join(choices)}")
    return text


def _order(value: Any) -> str:
    return _choice(value, ("NORMAL", "REVERSE", "RANDOM"), "Order")


def _frequency(value: Any) -> str:
    text = str(value if value is not None else "").strip().lower()
    mapping = {"500": "500", "1k": "1k", "1khz": "1k", "2k": "2kHz", "2khz": "2kHz"}
    if text not in mapping:
        raise ValueError("A test tone frequency is 500, 1k or 2kHz")
    return mapping[text]
