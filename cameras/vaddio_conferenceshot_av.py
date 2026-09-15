"""
OpenAVC Vaddio ConferenceSHOT AV driver.

Controls the Vaddio (Legrand AV) ConferenceSHOT AV enterprise conferencing
system over its Telnet Serial Command API on port 23. The ConferenceSHOT AV
is a PTZ camera and a seven-channel audio mixer in one chassis, plus USB and
IP (RTSP/RTMP) streaming, so the control surface is much wider than the
camera-only RoboSHOT line: pan/tilt/zoom with absolute positioning, CCU
colour and lighting, sixteen presets, per-channel volume and mute for both
EasyMic inputs, the line output, USB record/playback and the IP stream,
video mute, the indicator LED, the IR cut filter, software triggers, stream
control and device health.

Telnet is DISABLED by default on every firmware issued after mid-December
2019. It is enabled on the Security page of the camera's web interface;
until it is, nothing on port 23 answers and the device cannot be controlled
at all. `help.setup` says so as step one.

Why Python rather than YAML
---------------------------
`ConfigurableDriver` matches responses per frame with no memory of what was
asked, and this protocol's replies do not say what they answer:

- All seven audio channels reply to `audio <ch> volume get` with the same
  `volume: -9.0 dB` and to `audio <ch> mute get` with the same `mute: off`.
  Nothing in either reply names the channel.
- `video mute get` answers `mute:   off` — byte-identical in shape to an
  audio channel's mute reply.
- `camera pan get`, `camera tilt get` and `camera zoom get` each answer with
  a bare number on its own line (`103.47`, `40.26`, `11`).

A per-frame matcher routes every one of those to whichever rule it wrote
first, so polling the mixer would scatter one channel's level across all
seven. Each reply has to be correlated to the request that produced it,
which means a driver that tracks the outstanding command.

Three more reasons follow from the same shell:

- The end of a reply is the `> ` prompt, not the line delimiter. Most
  commands end `OK` or `ERROR`, but `camera sensor get` and
  `system serial-number` answer with neither, so a driver framing on lines
  cannot tell where one reply stops.
- A failure must be reported as a failure. The shell has two shapes — a
  bare `Syntax error: Unknown or incomplete command` with no `ERROR` token,
  and a human sentence followed by `ERROR` — and a fire-and-forget send
  reports both as success.
- Four queries fan one reply out into many state variables:
  `camera ccu get all` (11), `streaming settings get` (16), `version` (5)
  and `network settings get` (7).

Push vs. poll
-------------
Poll. The Telnet API is request/response only — the manual has no
subscription, notification or feedback section, and the shell emits nothing
unsolicited except the operating system's own reboot broadcast. Position,
CCU, audio, mute and health state are polled; identity and streaming
settings are read once per connect and refreshed on a slow cycle.

Protocol reference
------------------
"Complete Manual for the ConferenceSHOT AV Enterprise-Class Conferencing
System", Vaddio document 411-0001-30 Rev F (August 2020), section
"Telnet Serial Command API" (pp. 69-90).

Where firmware 1.7.2 and that manual disagree, the hardware wins and the
divergence is recorded in `driver-roadmap/shipped/vaddio_conferenceshot_av.md`.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from openavc.core.connection_fault import ConnectionFaultError
from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import CallableFrameParser
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Wire constants
# ---------------------------------------------------------------------------

# Every reply block ends with the shell's prompt, which follows a CRLF and
# carries no newline of its own:
#     b'camera led get\r\nLED:    off\r\nOK\r\n> '
# Framing on the prompt (rather than on CRLF) is what makes one send equal
# one reply, which is what the whole correlation model rests on.
_PROMPT = b"\r\n> "

# The audio matrix. Ids are the channel names the protocol itself uses, so a
# macro or panel binding reads as the label on the chassis.
AUDIO_CHANNELS: tuple[tuple[str, str], ...] = (
    ("master", "Master Output"),
    ("easy_mic_1", "EasyMic 1"),
    ("easy_mic_2", "EasyMic 2"),
    ("usb_playback", "USB Playback (far end)"),
    ("line_out_1", "Line Output 1 (speaker)"),
    ("usb_record", "USB Record (near end)"),
    ("ip_stream", "IP Stream Output"),
)

# `audio master mute on` is an overlay on the near-end path only: measured on
# firmware 1.7.2, it makes exactly these three channels report muted and leaves
# usb_playback, line_out_1 and ip_stream alone. Turning it off restores each
# channel to its OWN mute setting rather than unmuting everything, so it is a
# "mute the mics" control, not a master kill.
MASTER_MUTE_COVERS: frozenset[str] = frozenset(
    {"easy_mic_1", "easy_mic_2", "usb_record"}
)

# Measured against the camera's own audio model (`filter_range`): every
# gain-class channel is -42.0..6.0 dB in 1 dB steps, and the shell refuses
# -43 and 7 with ERROR. `volume up` / `volume down` move one whole dB.
VOLUME_MIN_DB = -42.0
VOLUME_MAX_DB = 6.0

# `camera zoom set` is bounded by the camera, not by the driver: firmware
# 1.7.2 answers "zoom position 30.0 out of bounds (1.0..12.0)". 12x is the
# Super Wide ceiling; a unit with Super Wide off stops at 10 and refuses the
# rest itself. Declaring the wider bound keeps a Super Wide unit usable
# rather than blocking valid positions at the driver.
ZOOM_MIN = 1.0
ZOOM_MAX = 12.0

# Pan/tilt travel from the manual's specification table. "Approximately" is
# the manual's own word — individual cameras reach a little further — so
# these are the declared bounds and the camera refuses anything past its
# real limit.
PAN_MIN, PAN_MAX = -160.0, 160.0
TILT_MIN, TILT_MAX = -30.0, 90.0

# A reply's last line is one of these, or the reply has no terminator at all
# (`camera sensor get`, `system serial-number`).
_OK = "OK"
_ERR = "ERROR"
_SYNTAX_ERROR = "Syntax error"

# Telnet IAC. The camera opens with
# IAC DO ECHO / DO NAWS / WILL ECHO / WILL SGA and never negotiates again,
# but the stripper runs on every frame so a mid-session option can't corrupt
# a reply.
_IAC = 0xFF
_IAC_OPTS = (0xFB, 0xFC, 0xFD, 0xFE)  # WILL / WONT / DO / DONT

_ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")


def _strip_telnet(buf: bytes) -> bytes:
    """Drop Telnet IAC sequences and ANSI colour runs, keep the payload.

    The login banner is red (`\\x1b[1;31m`) and the shell rings the terminal
    bell (0x07) when tab completion fails on an unknown token, which is how
    an unsupported command's echo comes back mangled. None of it is protocol.
    """
    out = bytearray()
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if b == _IAC:
            nxt = buf[i + 1] if i + 1 < n else None
            if nxt in _IAC_OPTS:
                i += 3
                continue
            if nxt == _IAC:          # escaped literal 0xFF
                out.append(_IAC)
                i += 2
                continue
            i += 2                    # two-byte IAC command
            continue
        if b != 0x07:                 # BEL: tab-completion failure
            out.append(b)
        i += 1
    return _ANSI_RE.sub(b"", bytes(out))


def _frame_on_prompt(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Split one reply block off the buffer at the shell prompt.

    Returns ``(block, remaining)``. The block excludes the prompt itself;
    everything before it — the command echo, the body lines, and the
    ``OK`` / ``ERROR`` terminator when there is one — is one reply.
    """
    idx = buffer.find(_PROMPT)
    if idx < 0:
        return None, buffer
    return buffer[:idx], buffer[idx + len(_PROMPT):]


class VaddioReply:
    """One parsed reply block: its body lines and whether it succeeded."""

    __slots__ = ("lines", "ok", "error")

    def __init__(self, lines: list[str], ok: bool, error: str) -> None:
        self.lines = lines
        self.ok = ok
        self.error = error

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"VaddioReply(ok={self.ok}, error={self.error!r}, lines={self.lines!r})"


def parse_reply(block: str, sent: str) -> VaddioReply:
    """Turn a raw reply block into body lines plus a verdict.

    The shell echoes the command it received before answering, so the first
    line is dropped when it is that echo. The echo is NOT reliable for
    correlation — tab completion mangles an unknown command's echo into
    `camera ccu scenerecallfactory1` — so it is only ever discarded, never
    matched against.

    Three terminations, all seen on firmware 1.7.2:

        OK                                     -> success
        <sentence>\\nERROR                      -> failure, sentence is why
        Syntax error: Unknown or incomplete command
                                               -> failure, no ERROR token
        (neither)                              -> success, body is the value
    """
    raw = [ln.rstrip("\r") for ln in block.split("\n")]
    lines = [ln for ln in raw if ln.strip() != ""]

    # Drop the echo. Compare on collapsed whitespace because the shell's tab
    # completion eats the spaces between tokens it could not complete.
    if lines:
        squash = re.sub(r"\s+", "", lines[0])
        if squash == re.sub(r"\s+", "", sent):
            lines = lines[1:]

    for ln in lines:
        if ln.startswith(_SYNTAX_ERROR):
            return VaddioReply([], False, ln.strip())

    if lines and lines[-1] == _ERR:
        body = lines[:-1]
        why = body[-1].strip() if body else "the camera rejected the command"
        return VaddioReply(body, False, why)

    if lines and lines[-1] == _OK:
        return VaddioReply(lines[:-1], True, "")

    # No terminator at all — `camera sensor get`, `system serial-number`.
    return VaddioReply(lines, True, "")


def _as_bool(text: str) -> bool | None:
    """Read the protocol's several spellings of a boolean."""
    t = text.strip().lower()
    if t in ("on", "true", "yes", "enabled"):
        return True
    if t in ("off", "false", "no", "disabled"):
        return False
    return None


def _as_number(text: str) -> float | None:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return None


def _clean_number(value: float) -> float | int:
    """Publish 11.0 as 11 so a panel shows a zoom of 11, not 11.0."""
    return int(value) if float(value).is_integer() else round(value, 2)


class VaddioConferenceShotAVDriver(BaseDriver):
    """Vaddio ConferenceSHOT AV over the Telnet Serial Command API."""

    DRIVER_INFO: dict[str, Any] = {
        "id": "vaddio_conferenceshot_av",
        "name": "Vaddio ConferenceSHOT AV",
        "manufacturer": "Vaddio",
        "category": "camera",
        "version": "1.0.0",
        "author": "OpenAVC",
        "min_platform_version": "0.34.0",
        "description": (
            "Controls the Vaddio (Legrand AV) ConferenceSHOT AV conferencing "
            "system over its Telnet Serial Command API. Pan/tilt/zoom with "
            "absolute positioning and readable position, 16 presets, the full "
            "CCU colour and lighting surface, the seven-channel audio mixer as "
            "child entities, video mute, indicator LED, IR cut filter, software "
            "triggers, IP/USB streaming control with a previewable RTSP stream, "
            "and device health."
        ),
        "source_url": (
            "https://www.adorama.com/col/productManuals/VAD99995000W.pdf"
        ),
        "tags": [
            "ptz", "camera", "conferencing", "usb", "audio", "mixer",
            "vaddio", "legrand", "conferenceshot", "telnet", "rtsp",
        ],
        "verified": True,
        "simulated": True,
        "protocols": ["vaddio_telnet"],
        "ports": [23],
        "transport": "tcp",
        "delimiter": "\r\n",

        "compatible_models": [
            {
                "manufacturer": "Vaddio",
                "models": ["ConferenceSHOT AV"],
                "confidence": "full",
                "notes": (
                    "Tested against a ConferenceSHOT AV on firmware 1.7.2. "
                    "Telnet must be enabled on the web interface's Security "
                    "page first — it is off by default on every firmware "
                    "issued after mid-December 2019."
                ),
            },
        ],

        "help": {
            "overview": (
                "The Vaddio ConferenceSHOT AV is a PTZ camera and audio mixer "
                "in one: a 10x optical zoom camera, two EasyMic microphone "
                "inputs, a line output for the room speaker, USB 3.0 record and "
                "playback, and an H.264 IP stream. This driver drives all of it "
                "over the camera's Telnet API — camera moves and presets, "
                "colour and lighting, per-channel levels and mutes, video mute "
                "for privacy, and the stream itself."
            ),
            "setup": (
                "1. TURN TELNET ON FIRST. In the camera's web interface, open "
                "Security and enable Telnet access. Vaddio ships it disabled on "
                "every firmware issued after mid-December 2019, and nothing on "
                "port 23 answers until you enable it.\n"
                "2. Give the camera a static IP, or reserve its DHCP lease.\n"
                "3. Enter the IP address, the admin username (default 'admin') "
                "and the admin password below. Telnet uses the same admin "
                "account as the web interface.\n"
                "4. Pan, tilt and zoom come in two styles. The drive commands "
                "(Pan Left, Tilt Up, Zoom In) keep moving until you send the "
                "matching stop, which is what you bind to a press-and-hold "
                "button. The position commands (Go To Pan, Go To PTZ Position) "
                "move to an absolute angle and are what you bind to a recall.\n"
                "5. Audio channels appear as sub-devices. Each has a level in "
                "dB (-42 to +6) and a mute, so a panel fader binds straight to "
                "the channel you mean. Two things about mute are worth knowing "
                "before you wire a panel: master mute covers the microphone "
                "path only — both EasyMics and the USB record stream — and "
                "while it is on the camera refuses to change those three "
                "channels one at a time. Standby forces master mute on, so an "
                "audio button does nothing until the camera is awake.\n"
                "6. To see the camera in a Video Panel, turn IP streaming on "
                "and the driver publishes the RTSP address for you."
            ),
        },

        # Discovery. Every signal here was read off a live ConferenceSHOT AV.
        # The mDNS service is the strong one: `_vaddiodevice._tcp` is Vaddio's
        # own service type and the instance name carries the model, so the
        # camera identifies itself with no credentials and without Telnet being
        # enabled. The TLS probe is the fallback for a subnet where mDNS is not
        # forwarded; the camera's web UI answers `/api/config/system/product`
        # unauthenticated and names the model in the body.
        "discovery": {
            # NOT declared here: the `_vaddiodevice._tcp.local.` mDNS
            # service this camera advertises. It is Vaddio's vendor-wide
            # service type — a RoboSHOT answers it too — and a fingerprint
            # may be claimed by only one driver, so whichever driver claimed
            # it would mis-identify the rest of the family. What separates
            # the models is the mDNS instance name ("ConferenceSHOT AV
            # [system-<mac>]") and a JSON blob in the TXT record, and a
            # declared fingerprint can only filter on k/v TXT pairs. The
            # probe below reads the model out of the camera instead, which
            # is exact.
            "tcp_probe": {
                "port": 443,
                "tls": True,
                "send_ascii": (
                    "GET /api/config/system/product HTTP/1.1\r\n"
                    "Host: openavc-discovery\r\n"
                    "Connection: close\r\n\r\n"
                ),
                "expect_regex": r'"name"\s*:\s*"ConferenceSHOT AV"',
                "timeout_ms": 4000,
                "extract_manufacturer": "Vaddio",
                "extract": {
                    "model": {
                        "regex": r'"name"\s*:\s*"([^"]+)"',
                        "group": 1,
                    },
                },
            },
            # The factory hostname is `vaddio-conferenceshot-av-<mac>`, which
            # the camera also publishes as its mDNS server name. No OUI hint:
            # Vaddio's MACs come out of Microchip's blocks (this unit is
            # 68:27:19, the manual's example is 00:1E:C0), so an OUI rule here
            # would claim every Microchip-based device on the subnet.
            "hostname": [
                "^vaddio-conferenceshot-av",
                "^conferenceshot-av",
            ],
            "manufacturer_alias": ["vaddio", "legrand", "legrand av"],
        },

        "default_config": {
            "host": "",
            "port": 23,
            "username": "admin",
            "password": "",
            "poll_interval": 15,
            "slow_poll_every": 6,
            "inter_command_delay": 0.05,
            "command_timeout": 8.0,
            "move_timeout": 60.0,
        },

        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 23, "label": "Telnet Port"},
            "username": {
                "type": "string",
                "default": "admin",
                "label": "Username",
                "description": "Admin account on the camera. Default 'admin'.",
            },
            "password": {
                "type": "string",
                "default": "",
                "required": True,
                "label": "Password",
                "secret": True,
                "description": (
                    "The camera's admin password — the same one the web "
                    "interface uses."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 5,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often to refresh position, levels, mutes and CCU. "
                    "The camera's shell is slow and there is no bulk audio "
                    "query, so a cycle spends about four seconds asking each "
                    "of the seven channels for its level and its mute. Leave "
                    "a margin above that."
                ),
            },
            "slow_poll_every": {
                "type": "integer",
                "default": 6,
                "min": 1,
                "label": "Slow Poll Every N Cycles",
                "description": (
                    "Streaming settings, temperature and network details cost a "
                    "round trip each and rarely change, so they refresh every "
                    "Nth cycle instead of every one."
                ),
            },
            "inter_command_delay": {
                "type": "number",
                "default": 0.05,
                "label": "Inter-Command Delay (sec)",
            },
            "command_timeout": {
                "type": "number",
                "default": 8.0,
                "min": 1.0,
                "label": "Command Timeout (sec)",
                "description": (
                    "Most commands answer inside 400 ms; the slowest, "
                    "`streaming settings get`, measured two seconds."
                ),
            },
            "move_timeout": {
                "type": "number",
                "default": 60.0,
                "min": 5.0,
                "label": "Absolute Move Timeout (sec)",
                "description": (
                    "Only used by Go To PTZ Position with 'Wait for arrival' "
                    "on, which holds the camera's shell for the whole travel. "
                    "A corner-to-corner move measured 21.9 seconds, so the "
                    "ceiling is deliberately generous."
                ),
            },
        },

        "child_entity_types": {
            "audio_channel": {
                "label": "Audio Channel",
                "label_plural": "Audio Channels",
                "id_format": {"type": "string"},
                "label_field": "label",
                "summary_fields": ["volume_db", "muted"],
                "state_variables": {
                    "label": {"type": "string", "label": "Name"},
                    "volume_db": {
                        "type": "number",
                        "label": "Level (dB)",
                        "min": -42.0,
                        "max": 6.0,
                        "step": 1,
                        "unit": "dB",
                        "control": True,
                        "cloud_priority": "high",
                        "help": "Channel level, -42 to +6 dB.",
                    },
                    "muted": {
                        "type": "boolean",
                        "label": "Muted",
                        "control": True,
                        "cloud_priority": "high",
                    },
                },
            },
        },

        "state_variables": {
            # Identity
            "model_name": {"type": "string", "label": "Model"},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "audio_firmware": {"type": "string", "label": "Audio Firmware"},
            "usb_firmware": {"type": "string", "label": "USB Firmware"},
            "sensor_firmware": {"type": "string", "label": "Sensor Firmware"},
            "sensor_type": {
                "type": "string",
                "label": "Image Sensor",
                "help": "Sensor identifier reported by `camera sensor get`.",
            },
            "serial_number": {"type": "string", "label": "Serial Number"},

            # Operating state
            "standby": {
                "type": "boolean",
                "label": "Standby",
                "control": True,
                "cloud_priority": "high",
                "help": "True while the camera is asleep and sending no video.",
            },
            "video_muted": {
                "type": "boolean",
                "label": "Video Muted",
                "control": True,
                "cloud_priority": "high",
                "help": (
                    "True while the camera sends a blue or black frame instead "
                    "of the room. Audio is unaffected."
                ),
            },
            "focus_auto": {
                "type": "boolean",
                "label": "Auto Focus",
                "control": True,
            },
            "ir_correction": {
                "type": "enum",
                "values": ["standard", "ir-light"],
                "label": "IR Focus Correction",
                "help": (
                    "Focus compensation for a room lit by IR illuminators."
                ),
            },
            "led_on": {
                "type": "boolean",
                "label": "Indicator LED",
                "control": True,
                "help": (
                    "The front indicator. With it off you cannot tell by "
                    "looking whether the camera is sending video."
                ),
            },
            "ir_cut_filter": {
                "type": "boolean",
                "label": "IR Cut Filter",
                "help": (
                    "True when the filter is out (`on`), which is the daylight "
                    "setting. False puts it in for night use and the picture "
                    "goes black and white."
                ),
            },

            # Position
            "pan_position": {
                "type": "number",
                "label": "Pan Position",
                "min": -160.0, "max": 160.0, "step": 0.01, "unit": "deg",
                "cloud_priority": "low",
            },
            "tilt_position": {
                "type": "number",
                "label": "Tilt Position",
                "min": -30.0, "max": 90.0, "step": 0.01, "unit": "deg",
                "cloud_priority": "low",
            },
            "zoom_position": {
                "type": "number",
                "label": "Zoom Level",
                "min": 1.0, "max": 12.0, "step": 0.1, "unit": "x",
                "cloud_priority": "low",
            },

            # CCU
            "auto_iris": {"type": "boolean", "label": "Auto Iris", "control": True},
            "auto_white_balance": {
                "type": "boolean", "label": "Auto White Balance", "control": True,
            },
            "backlight_compensation": {
                "type": "boolean", "label": "Backlight Compensation",
            },
            "wide_dynamic_range": {
                "type": "boolean", "label": "Wide Dynamic Range",
            },
            "iris": {
                "type": "integer", "label": "Iris", "min": 0, "max": 11, "step": 1,
            },
            "gain": {
                "type": "integer", "label": "Gain", "min": 0, "max": 11, "step": 1,
            },
            "detail": {
                "type": "integer", "label": "Detail", "min": 0, "max": 15, "step": 1,
            },
            "chroma": {
                "type": "integer", "label": "Chroma", "min": 0, "max": 14, "step": 1,
            },
            "gamma": {
                "type": "integer", "label": "Gamma", "min": -16, "max": 64, "step": 1,
            },
            "red_gain": {
                "type": "integer", "label": "Red Gain", "min": 0, "max": 255, "step": 1,
            },
            "blue_gain": {
                "type": "integer", "label": "Blue Gain", "min": 0, "max": 255, "step": 1,
            },

            # Streaming
            "ip_streaming_enabled": {
                "type": "boolean", "label": "IP Streaming", "control": True,
                "cloud_priority": "high",
            },
            "stream_protocol": {"type": "string", "label": "Stream Protocol"},
            "stream_port": {"type": "integer", "label": "Stream Port"},
            "stream_path": {
                "type": "string",
                "label": "Stream Path",
                "help": "The stream name the camera serves the RTSP session at.",
            },
            "stream_resolution": {"type": "string", "label": "Stream Resolution"},
            "stream_quality": {"type": "string", "label": "Stream Quality"},
            "preview_url": {
                "type": "string",
                "label": "Preview URL",
                "help": (
                    "RTSP address of the camera's IP stream. The Video Panel "
                    "plugin lists it automatically. Empty while IP streaming is "
                    "off."
                ),
            },
            "preview_format": {"type": "string", "label": "Preview Format"},
            "usb_active": {
                "type": "boolean",
                "label": "USB Stream Active",
                "cloud_priority": "high",
                "help": "True while a conferencing client is taking the USB stream.",
            },
            "usb_device_name": {"type": "string", "label": "USB Device Name"},
            "usb_resolution": {"type": "string", "label": "USB Resolution"},
            "usb_frame_rate": {"type": "integer", "label": "USB Frame Rate"},
            "uvc_extensions_enabled": {
                "type": "boolean",
                "label": "Far-End Camera Control",
                "help": (
                    "True when a soft client at the far end may drive this "
                    "camera through the USB connection."
                ),
            },

            # Health / network
            "temperature_c": {
                "type": "number",
                "label": "Temperature",
                "unit": "C",
                "step": 0.01,
                "cloud_priority": "low",
            },
            "temperature_fault": {
                "type": "boolean",
                "label": "Temperature Fault",
                "cloud_priority": "high",
            },
            "factory_reset_armed": {
                "type": "boolean",
                "label": "Factory Reset Armed",
                "help": (
                    "True when the next reboot will reset the camera to "
                    "factory defaults."
                ),
            },
            "ip_address": {"type": "string", "label": "IP Address"},
            "mac_address": {"type": "string", "label": "MAC Address"},
            "hostname": {"type": "string", "label": "Hostname"},
            "gateway": {"type": "string", "label": "Gateway"},
        },

        "quick_actions": [
            "home", "standby_toggle", "video_mute_toggle", "master_mute_toggle",
        ],

        "commands": {
            # ---- Pan / tilt / zoom, drive style -------------------------------
            "pan_left": {
                "label": "Pan Left",
                "help": "Pan left until Pan Stop. Speed 1-24.",
                "params": {"speed": {
                    "type": "integer", "default": 12, "min": 1, "max": 24,
                    "label": "Speed (1-24)"}},
            },
            "pan_right": {
                "label": "Pan Right",
                "help": "Pan right until Pan Stop. Speed 1-24.",
                "params": {"speed": {
                    "type": "integer", "default": 12, "min": 1, "max": 24,
                    "label": "Speed (1-24)"}},
            },
            "pan_stop": {"label": "Pan Stop"},
            "tilt_up": {
                "label": "Tilt Up",
                "help": "Tilt up until Tilt Stop. Speed 1-20.",
                "params": {"speed": {
                    "type": "integer", "default": 10, "min": 1, "max": 20,
                    "label": "Speed (1-20)"}},
            },
            "tilt_down": {
                "label": "Tilt Down",
                "help": "Tilt down until Tilt Stop. Speed 1-20.",
                "params": {"speed": {
                    "type": "integer", "default": 10, "min": 1, "max": 20,
                    "label": "Speed (1-20)"}},
            },
            "tilt_stop": {"label": "Tilt Stop"},
            "zoom_in": {
                "label": "Zoom In",
                "help": "Zoom in until Zoom Stop. Speed 0-7.",
                "params": {"speed": {
                    "type": "integer", "default": 3, "min": 0, "max": 7,
                    "label": "Speed (0-7)"}},
            },
            "zoom_out": {
                "label": "Zoom Out",
                "help": "Zoom out until Zoom Stop. Speed 0-7.",
                "params": {"speed": {
                    "type": "integer", "default": 3, "min": 0, "max": 7,
                    "label": "Speed (0-7)"}},
            },
            "zoom_stop": {"label": "Zoom Stop"},
            "home": {
                "label": "Home",
                "help": (
                    "Send the camera to the home position stored on it — not "
                    "necessarily dead centre. Returns straight away; the move "
                    "itself takes a few seconds."
                ),
            },
            "recalibrate": {
                "label": "Recalibrate Motors",
                "help": (
                    "Re-home the pan and tilt motors. Use it after a motor fault. "
                    "The camera ignores movement commands while it calibrates."
                ),
            },

            # ---- Pan / tilt / zoom, absolute ----------------------------------
            "pan_set": {
                "label": "Go To Pan Angle",
                "help": "Pan to an absolute angle in degrees. Negative is left.",
                "params": {"position": {
                    "type": "number", "required": True, "default": 0,
                    "min": -160.0, "max": 160.0, "label": "Pan (deg)"}},
            },
            "tilt_set": {
                "label": "Go To Tilt Angle",
                "help": "Tilt to an absolute angle in degrees. Negative is down.",
                "params": {"position": {
                    "type": "number", "required": True, "default": 0,
                    "min": -30.0, "max": 90.0, "label": "Tilt (deg)"}},
            },
            "zoom_set": {
                "label": "Go To Zoom Level",
                "help": (
                    "Zoom to an absolute magnification. 1 is fully wide; the "
                    "ceiling is 10, or 12 with Super Wide enabled."
                ),
                "params": {"level": {
                    "type": "number", "required": True, "default": 1,
                    "min": 1.0, "max": 12.0, "label": "Zoom (x)"}},
            },
            "ptz_position_set": {
                "label": "Go To PTZ Position",
                "help": (
                    "Move all three axes at once. They start together, which is "
                    "what makes a recall look deliberate rather than stepped."
                ),
                "params": {
                    "pan": {"type": "number", "required": True, "default": 0,
                            "min": -160.0, "max": 160.0, "label": "Pan (deg)"},
                    "tilt": {"type": "number", "required": True, "default": 0,
                             "min": -30.0, "max": 90.0, "label": "Tilt (deg)"},
                    "zoom": {"type": "number", "required": True, "default": 1,
                             "min": 1.0, "max": 12.0, "label": "Zoom (x)"},
                    "wait": {"type": "boolean", "default": False,
                             "label": "Wait for arrival",
                             "help": (
                                 "Off by default: the command returns as the "
                                 "camera starts moving, so a macro's next step "
                                 "is not held for the whole travel.")},
                },
            },
            "query_position": {
                "label": "Query PTZ Position",
                "help": "Read pan, tilt and zoom back from the camera.",
            },

            # ---- Focus --------------------------------------------------------
            "focus_near": {
                "label": "Focus Near",
                "help": "Manual focus nearer. Auto Focus must be off. Speed 1-8.",
                "params": {"speed": {
                    "type": "integer", "default": 4, "min": 1, "max": 8,
                    "label": "Speed (1-8)"}},
            },
            "focus_far": {
                "label": "Focus Far",
                "help": "Manual focus further. Auto Focus must be off. Speed 1-8.",
                "params": {"speed": {
                    "type": "integer", "default": 4, "min": 1, "max": 8,
                    "label": "Speed (1-8)"}},
            },
            "focus_stop": {"label": "Focus Stop"},
            "focus_auto_on": {"label": "Auto Focus On"},
            "focus_auto_off": {"label": "Auto Focus Off (Manual)"},

            # ---- Presets ------------------------------------------------------
            "preset_recall": {
                "label": "Recall Preset",
                "help": (
                    "Move to one of the camera's 16 stored positions. Returns "
                    "straight away; the move takes a few seconds."
                ),
                "params": {"preset": {
                    "type": "integer", "required": True, "default": 1,
                    "min": 1, "max": 16, "label": "Preset (1-16)"}},
            },
            "preset_store": {
                "label": "Store Preset",
                "help": "Save the current position as one of the 16 presets.",
                "params": {"preset": {
                    "type": "integer", "required": True, "default": 1,
                    "min": 1, "max": 16, "label": "Preset (1-16)"}},
            },
            "preset_store_with_ccu": {
                "label": "Store Preset with Colour",
                "help": (
                    "Save the current position and the current colour settings "
                    "together, so the recall restores both."
                ),
                "params": {"preset": {
                    "type": "integer", "required": True, "default": 1,
                    "min": 1, "max": 16, "label": "Preset (1-16)"}},
            },

            # ---- CCU ----------------------------------------------------------
            "ccu_set_auto_iris": {
                "label": "Set Auto Iris",
                "params": {"value": {"type": "boolean", "required": True,
                                     "default": True, "label": "Auto Iris"}},
            },
            "ccu_set_iris": {
                "label": "Set Iris",
                "help": "Auto Iris must be off.",
                "params": {"value": {"type": "integer", "required": True,
                                     "min": 0, "max": 11, "label": "Iris (0-11)"}},
            },
            "ccu_set_gain": {
                "label": "Set Gain",
                "help": "Auto Iris must be off.",
                "params": {"value": {"type": "integer", "required": True,
                                     "min": 0, "max": 11, "label": "Gain (0-11)"}},
            },
            "ccu_set_auto_white_balance": {
                "label": "Set Auto White Balance",
                "params": {"value": {"type": "boolean", "required": True,
                                     "default": True, "label": "Auto White Balance"}},
            },
            "ccu_set_red_gain": {
                "label": "Set Red Gain",
                "help": "Auto White Balance must be off.",
                "params": {"value": {"type": "integer", "required": True,
                                     "min": 0, "max": 255, "label": "Red Gain"}},
            },
            "ccu_set_blue_gain": {
                "label": "Set Blue Gain",
                "help": "Auto White Balance must be off.",
                "params": {"value": {"type": "integer", "required": True,
                                     "min": 0, "max": 255, "label": "Blue Gain"}},
            },
            "ccu_set_detail": {
                "label": "Set Detail",
                "params": {"value": {"type": "integer", "required": True,
                                     "min": 0, "max": 15, "label": "Detail (0-15)"}},
            },
            "ccu_set_chroma": {
                "label": "Set Chroma",
                "params": {"value": {"type": "integer", "required": True,
                                     "min": 0, "max": 14, "label": "Chroma (0-14)"}},
            },
            "ccu_set_gamma": {
                "label": "Set Gamma",
                "params": {"value": {"type": "integer", "required": True,
                                     "min": -16, "max": 64, "label": "Gamma"}},
            },
            "ccu_set_backlight_compensation": {
                "label": "Set Backlight Compensation",
                "help": "Wide Dynamic Range must be off.",
                "params": {"value": {"type": "boolean", "required": True,
                                     "default": False,
                                     "label": "Backlight Compensation"}},
            },
            "ccu_set_wide_dynamic_range": {
                "label": "Set Wide Dynamic Range",
                "help": "Backlight Compensation must be off.",
                "params": {"value": {"type": "boolean", "required": True,
                                     "default": False,
                                     "label": "Wide Dynamic Range"}},
            },
            "query_ccu": {"label": "Query Colour Settings"},

            # ---- Audio --------------------------------------------------------
            "audio_volume_set": {
                "label": "Set Channel Level",
                "help": "Set one audio channel's level, -42 to +6 dB.",
                "params": {
                    "channel": {"type": "child_id", "child_type": "audio_channel",
                                "required": True, "label": "Channel"},
                    "level": {"type": "number", "required": True, "default": 0,
                              "min": -42.0, "max": 6.0,
                              "label": "Level (dB)"},
                },
            },
            "audio_volume_up": {
                "label": "Channel Level Up",
                "help": "Raise one channel by 1 dB.",
                "params": {"channel": {
                    "type": "child_id", "child_type": "audio_channel",
                    "required": True, "label": "Channel"}},
            },
            "audio_volume_down": {
                "label": "Channel Level Down",
                "help": "Lower one channel by 1 dB.",
                "params": {"channel": {
                    "type": "child_id", "child_type": "audio_channel",
                    "required": True, "label": "Channel"}},
            },
            "audio_mute_on": {
                "label": "Mute Channel",
                "params": {"channel": {
                    "type": "child_id", "child_type": "audio_channel",
                    "required": True, "label": "Channel"}},
            },
            "audio_mute_off": {
                "label": "Unmute Channel",
                "params": {"channel": {
                    "type": "child_id", "child_type": "audio_channel",
                    "required": True, "label": "Channel"}},
            },
            "audio_mute_toggle": {
                "label": "Toggle Channel Mute",
                "params": {"channel": {
                    "type": "child_id", "child_type": "audio_channel",
                    "required": True, "label": "Channel"}},
            },
            "master_mute_toggle": {
                "label": "Toggle Microphone Mute",
                "help": (
                    "Master mute covers the microphone path — both EasyMic "
                    "inputs and the USB record stream. It leaves the room "
                    "speaker, the far-end playback and the IP stream alone, "
                    "and turning it off restores each channel to its own "
                    "mute setting rather than opening everything. While it "
                    "is on the camera refuses to change those three "
                    "channels individually."
                ),
            },

            # ---- Video mute / LED / IR ----------------------------------------
            "video_mute_on": {
                "label": "Video Mute On",
                "help": "Send a blue or black frame instead of the room. Audio keeps running.",
            },
            "video_mute_off": {"label": "Video Mute Off"},
            "video_mute_toggle": {"label": "Toggle Video Mute"},
            "led_on": {"label": "Indicator LED On"},
            "led_off": {"label": "Indicator LED Off"},
            "ir_cut_filter_on": {
                "label": "IR Cut Filter On (Daylight)",
                "help": "Filter out, normal colour picture.",
            },
            "ir_cut_filter_off": {
                "label": "IR Cut Filter Off (Night)",
                "help": "Filter in for an IR-lit room. The picture goes black and white.",
            },

            # ---- Standby ------------------------------------------------------
            "standby_on": {"label": "Standby On"},
            "standby_off": {"label": "Standby Off (Wake)"},
            "standby_toggle": {"label": "Toggle Standby"},

            # ---- Triggers / streaming -----------------------------------------
            "trigger_on": {
                "label": "Trigger On",
                "help": (
                    "Fire one of the camera's own software triggers. A trigger "
                    "does nothing until you define it on the camera's Macros and "
                    "Triggers page."
                ),
                "params": {"index": {
                    "type": "integer", "required": True, "default": 1,
                    "min": 1, "max": 50, "label": "Trigger (1-50)"}},
            },
            "trigger_off": {
                "label": "Trigger Off",
                "help": "Release one of the camera's own software triggers.",
                "params": {"index": {
                    "type": "integer", "required": True, "default": 1,
                    "min": 1, "max": 50, "label": "Trigger (1-50)"}},
            },
            "streaming_on": {"label": "IP Streaming On"},
            "streaming_off": {"label": "IP Streaming Off"},
            "streaming_toggle": {"label": "Toggle IP Streaming"},
            "query_streaming": {"label": "Query Streaming Settings"},

            # ---- System -------------------------------------------------------
            "reboot": {
                "label": "Reboot Camera",
                "help": "Restart the camera. It is away for about a minute.",
                # Measured on a ConferenceSHOT AV 1.7.2: Telnet went away
                # 0.5 s after the acknowledgement, the port answered again at
                # 60.5 s and the shell served a real command at 64 s. Rounded
                # up to 90 for a cold start and a slower switch port — a
                # window that is too short turns a normal reboot into an
                # alert, which is the failure this field exists to prevent.
                "restarts_device_for": 90,
            },
            "query_version": {"label": "Query Version"},
            "query_network": {"label": "Query Network Settings"},
            "query_temperature": {"label": "Query Temperature"},
        },

        "device_settings": {
            "auto_iris": {
                "type": "boolean", "label": "Auto Iris", "state_key": "auto_iris",
                "default": True, "setup": False,
                "help": "On lets the camera set iris and gain itself.",
            },
            "iris": {
                "type": "integer", "label": "Iris", "min": 0, "max": 11,
                "state_key": "iris", "default": 6, "setup": False,
                "help": "Manual iris, 0-11. Auto Iris must be off.",
            },
            "gain": {
                "type": "integer", "label": "Gain", "min": 0, "max": 11,
                "state_key": "gain", "default": 3, "setup": False,
                "help": "Manual gain, 0-11. Auto Iris must be off.",
            },
            "auto_white_balance": {
                "type": "boolean", "label": "Auto White Balance",
                "state_key": "auto_white_balance", "default": True, "setup": False,
            },
            "red_gain": {
                "type": "integer", "label": "Red Gain", "min": 0, "max": 255,
                "state_key": "red_gain", "default": 200, "setup": False,
                "help": "Auto White Balance must be off.",
            },
            "blue_gain": {
                "type": "integer", "label": "Blue Gain", "min": 0, "max": 255,
                "state_key": "blue_gain", "default": 195, "setup": False,
                "help": "Auto White Balance must be off.",
            },
            "detail": {
                "type": "integer", "label": "Detail", "min": 0, "max": 15,
                "state_key": "detail", "default": 8, "setup": False,
            },
            "chroma": {
                "type": "integer", "label": "Chroma", "min": 0, "max": 14,
                "state_key": "chroma", "default": 7, "setup": False,
            },
            "gamma": {
                "type": "integer", "label": "Gamma", "min": -16, "max": 64,
                "state_key": "gamma", "default": 0, "setup": False,
            },
            "backlight_compensation": {
                "type": "boolean", "label": "Backlight Compensation",
                "state_key": "backlight_compensation", "default": False,
                "setup": False,
                "help": "Brightens a subject lit from behind. Wide Dynamic Range must be off.",
            },
            "wide_dynamic_range": {
                "type": "boolean", "label": "Wide Dynamic Range",
                "state_key": "wide_dynamic_range", "default": False, "setup": False,
                "help": "Balances bright and dark areas. Backlight Compensation must be off.",
            },
            "led_on": {
                "type": "boolean", "label": "Indicator LED",
                "state_key": "led_on", "default": True, "setup": False,
                "help": "Turn the front indicator off to hide whether the camera is live.",
            },
            "ir_correction": {
                "type": "enum",
                "values": [
                    {"value": "standard", "label": "Standard"},
                    {"value": "ir-light", "label": "IR Light"},
                ],
                "label": "IR Focus Correction",
                "state_key": "ir_correction", "default": "standard", "setup": False,
                "help": "Set to IR Light in a room lit by infrared illuminators.",
            },
        },
    }

    # The shell is silent between commands, so a dead link looks identical to
    # an idle one until something is asked. The watchdog asks.
    HEALTH_INTERVAL_S = 45.0
    HEALTH_TIMEOUT_S = 8.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The camera stopped answering its Telnet session"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._auth_mode = False
        self._auth_buffer = bytearray()
        self._auth_event = asyncio.Event()
        self._io_lock = asyncio.Lock()
        self._poll_count = 0
        self._saved_parser: Any = None

    # -- transport ---------------------------------------------------------

    def _create_frame_parser(self) -> Any:
        """Frame on the shell prompt, not on the line delimiter.

        One frame is then one whole reply — echo, body and terminator — which
        is what lets `send_and_wait` hand a caller the answer to its own
        question and nobody else's.
        """
        return CallableFrameParser(_frame_on_prompt)

    # -- login -------------------------------------------------------------

    async def _pre_connect(self) -> None:
        """Refuse a login that cannot succeed before opening the socket.

        The camera's shell wants both a username and a password, so a blank
        either side is a certain rejection. Raising here rather than after
        connecting keeps the platform's post-auth_failed reconnect pause
        from spending attempts on a question already answered.
        """
        if not str(self.config.get("username", "") or "") or not str(
            self.config.get("password", "") or ""
        ):
            raise ConnectionFaultError(
                "Enter the camera's admin username and password — its Telnet "
                "session requires both.",
                code="auth_failed",
            )

    async def _post_connect(self) -> None:
        """Log in before the platform declares the device connected.

        Runs in raw mode: the `login: ` and `Password: ` prompts arrive with
        no newline and no prompt terminator, so the prompt frame parser would
        sit on them forever. The parser is restored on every exit path.
        """
        username = str(self.config.get("username", "") or "")
        password = str(self.config.get("password", "") or "")
        transport = self.transport
        self._saved_parser = getattr(transport, "_frame_parser", None)
        self._auth_buffer.clear()
        self._auth_event.clear()
        self._auth_mode = True
        if hasattr(transport, "_frame_parser"):
            parser = self._saved_parser
            if parser is not None and hasattr(parser, "_buffer"):
                pending = bytes(parser._buffer)
                if pending:
                    self._auth_buffer.extend(pending)
                    self._auth_event.set()
                    parser._buffer = b""
            transport._frame_parser = None

        try:
            await asyncio.wait_for(self._login(username, password), timeout=20.0)
        except asyncio.TimeoutError:
            raise ConnectionFaultError(
                "The camera accepted the connection but never asked for a "
                "login. Check that Telnet is enabled on its Security page.",
                code="no_response",
            ) from None
        finally:
            self._auth_mode = False
            if hasattr(transport, "_frame_parser"):
                transport._frame_parser = self._saved_parser
                if self._saved_parser is not None:
                    self._saved_parser.reset()
            self._auth_buffer.clear()

    async def _login(self, username: str, password: str) -> None:
        await self._expect(r"login:\s*$")
        await self.transport.send(f"{username}\r\n".encode())
        await self._expect(r"[Pp]assword:\s*$")
        await self.transport.send(f"{password}\r\n".encode())

        # `Welcome <user>` is the post-login banner. Anchor on it rather than
        # on the prompt character: the shell paints the banner in ANSI red
        # and echoes plenty that a bare `>` would false-match.
        seen = await self._expect(r"Welcome |login:\s*$", timeout=12.0)
        if re.search(r"login:\s*$", seen):
            raise ConnectionFaultError(
                "The camera rejected that username or password.",
                code="auth_failed",
            )

    async def _expect(self, pattern: str, timeout: float = 12.0) -> str:
        """Wait for a regex to appear in the raw login stream."""
        rx = re.compile(pattern)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            text = _strip_telnet(bytes(self._auth_buffer)).decode(
                "utf-8", "replace"
            )
            if rx.search(text):
                self._auth_buffer.clear()
                return text
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(pattern)
            self._auth_event.clear()
            try:
                await asyncio.wait_for(self._auth_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(pattern) from None

    async def on_data_received(self, data: bytes) -> None:
        """Feed the login handshake; ignore everything else.

        Outside the handshake every reply is collected by `send_and_wait`,
        so nothing arrives here that anybody is waiting for.
        """
        if self._auth_mode:
            self._auth_buffer.extend(data)
            self._auth_event.set()

    # -- request / response ------------------------------------------------

    async def _ask(self, command: str, timeout: float | None = None) -> VaddioReply:
        """Send one command and return its reply block.

        The lock is what makes a reply belong to a request: the shell serves
        one command at a time and answers in order, so two callers must not
        have questions outstanding together.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError("not connected")
        if timeout is None:
            timeout = float(self.config.get("command_timeout", 6.0) or 6.0)
        async with self._io_lock:
            raw = await self.transport.send_and_wait(
                f"{command}\r\n".encode(), timeout=timeout
            )
        block = _strip_telnet(raw).decode("utf-8", "replace")
        reply = parse_reply(block, command)
        if not reply.ok:
            log.debug(f"[{self.device_id}] {command!r} -> {reply.error}")
        return reply

    async def _do(self, command: str, timeout: float | None = None) -> bool:
        """Send a command that should succeed; raise with the camera's own
        reason when it does not.

        This is the whole point of correlating replies. A fire-and-forget
        send reports success for a command the camera refused, so a button
        bound to it sits there looking fine and does nothing.
        """
        reply = await self._ask(command, timeout=timeout)
        if not reply.ok:
            raise ValueError(reply.error)
        return True

    # -- lifecycle ---------------------------------------------------------

    async def _initial_sync(self) -> None:
        for local_id, label in AUDIO_CHANNELS:
            self.register_child("audio_channel", local_id)
            self.set_child_state("audio_channel", local_id, "label", label)

        await self._read_identity()
        await self._read_ccu()
        await self._read_modes()
        await self._read_position()
        await self._read_audio()
        await self._read_streaming()
        await self._read_network()
        await self._read_temperature()

    async def _liveness_probe(self) -> None:
        """Ask the cheapest question there is.

        A Telnet session that has been cut without a FIN looks exactly like
        an idle one, and this protocol never speaks first, so the only way to
        find out is to ask and wait.
        """
        await self._ask("camera standby get", timeout=self.HEALTH_TIMEOUT_S)

    # -- readers -----------------------------------------------------------

    async def _read_identity(self) -> None:
        reply = await self._ask("version")
        if not reply.ok:
            return
        updates: dict[str, Any] = {}
        for line in reply.lines:
            m = re.match(r"^(.+?)\s{2,}(.+)$", line.strip())
            if not m:
                continue
            key, value = m.group(1).strip(), m.group(2).strip()
            if key == "System Version":
                # "ConferenceSHOT AV 1.7.2" — model then version.
                mv = re.match(r"^(.*?)\s+([\d.]+)$", value)
                if mv:
                    updates["model_name"] = mv.group(1).strip()
                    updates["firmware_version"] = mv.group(2)
                else:
                    updates["model_name"] = value
            elif key == "Audio":
                updates["audio_firmware"] = value
            elif key == "USB":
                updates["usb_firmware"] = value
            elif key == "Sensor Version":
                updates["sensor_firmware"] = value
        if updates:
            self.set_states(updates)

        sensor = await self._ask("camera sensor get")
        if sensor.ok and sensor.lines:
            self.set_state("sensor_type", sensor.lines[0].strip().strip('"'))

        serial = await self._ask("system serial-number")
        if serial.ok and serial.lines:
            text = serial.lines[0].strip()
            # A factory unit answers "Serial number not set" rather than
            # failing, so publish nothing instead of that sentence.
            self.set_state("serial_number", "" if "not set" in text.lower() else text)

    async def _read_ccu(self) -> None:
        reply = await self._ask("camera ccu get all")
        if not reply.ok:
            return
        bools = {
            "auto_iris", "auto_white_balance",
            "backlight_compensation", "wide_dynamic_range",
        }
        ints = {"iris", "gain", "detail", "chroma", "gamma", "red_gain", "blue_gain"}
        updates: dict[str, Any] = {}
        for line in reply.lines:
            m = re.match(r"^(\w+)\s+(\S+)\s*$", line.strip())
            if not m:
                continue
            key, value = m.group(1), m.group(2)
            if key in bools:
                flag = _as_bool(value)
                if flag is not None:
                    updates[key] = flag
            elif key in ints:
                num = _as_number(value)
                if num is not None:
                    updates[key] = int(num)
        if updates:
            self.set_states(updates)

    async def _read_modes(self) -> None:
        """Read the one-line mode queries.

        Each is its own round trip precisely because the replies are not
        distinguishable: `mute:   off` is the answer to `video mute get` and
        to every `audio <ch> mute get`, so only the question tells them apart.
        """
        focus = await self._ask("camera focus mode get")
        if focus.ok:
            flag = self._value_after(focus, r"auto_focus:\s*(\S+)")
            if flag is not None:
                self.set_state("focus_auto", _as_bool(flag))

        standby = await self._ask("camera standby get")
        if standby.ok:
            flag = self._value_after(standby, r"standby:\s*(\S+)")
            if flag is not None:
                self.set_state("standby", _as_bool(flag))

        vmute = await self._ask("video mute get")
        if vmute.ok:
            flag = self._value_after(vmute, r"mute:\s*(\S+)")
            if flag is not None:
                self.set_state("video_muted", _as_bool(flag))

        led = await self._ask("camera led get")
        if led.ok:
            # The manual prints `led: on`; firmware 1.7.2 sends `LED:    on`.
            flag = self._value_after(led, r"(?i:led):\s*(\S+)")
            if flag is not None:
                self.set_state("led_on", _as_bool(flag))

        icr = await self._ask("camera icr get")
        if icr.ok:
            # "  IR(Cut) filter on(Out)" — leading spaces, and the word in
            # brackets is the mechanical position, not the state.
            m = self._value_after(icr, r"IR\(Cut\) filter\s+(on|off)")
            if m is not None:
                self.set_state("ir_cut_filter", _as_bool(m))

        irc = await self._ask("camera focus ir-correction get")
        if irc.ok:
            value = self._value_after(irc, r"IR Correction:\s*(\S+)")
            if value in ("standard", "ir-light"):
                self.set_state("ir_correction", value)

    async def _read_position(self) -> None:
        """Read all three axes with one question.

        `camera pan get` answers a bare `103.47` and `camera tilt get` a bare
        `40.26`; nothing in either says which axis it is. `ptz-position get`
        labels all three, so it is both cheaper and the only unambiguous read.
        """
        reply = await self._ask("camera ptz-position get")
        if not reply.ok:
            return
        updates: dict[str, Any] = {}
        for line in reply.lines:
            m = re.match(r"^(pan|tilt|zoom):\s*(-?[\d.]+)\s*$", line.strip())
            if not m:
                continue
            value = _as_number(m.group(2))
            if value is not None:
                updates[f"{m.group(1)}_position"] = _clean_number(value)
        if updates:
            self.set_states(updates)

    async def _read_audio(self) -> None:
        for local_id, _label in AUDIO_CHANNELS:
            vol = await self._ask(f"audio {local_id} volume get")
            mute = await self._ask(f"audio {local_id} mute get")
            updates: dict[str, Any] = {}
            if vol.ok:
                raw = self._value_after(vol, r"volume:\s*(-?[\d.]+)\s*dB")
                value = _as_number(raw) if raw is not None else None
                if value is not None:
                    updates["volume_db"] = round(value, 2)
            if mute.ok:
                flag = self._value_after(mute, r"mute:\s*(\S+)")
                if flag is not None:
                    updates["muted"] = _as_bool(flag)
            if updates:
                self.set_child_state_batch("audio_channel", local_id, updates)

    async def _read_streaming(self) -> None:
        enabled = await self._ask("streaming ip enable get")
        if enabled.ok:
            flag = self._value_after(enabled, r"enabled:\s*(\S+)")
            if flag is not None:
                self.set_state("ip_streaming_enabled", _as_bool(flag))

        reply = await self._ask("streaming settings get", timeout=8.0)
        if not reply.ok:
            self._publish_preview()
            return
        fields: dict[str, str] = {}
        for line in reply.lines:
            m = re.match(r"^(IP|USB|UVC)\s+(\S+)\s+(.*)$", line.strip())
            if m:
                fields[f"{m.group(1)} {m.group(2)}"] = m.group(3).strip()

        updates: dict[str, Any] = {}
        if "IP Protocol" in fields:
            updates["stream_protocol"] = fields["IP Protocol"]
        if "IP Port" in fields:
            port = _as_number(fields["IP Port"])
            if port is not None:
                updates["stream_port"] = int(port)
        if "IP URL" in fields:
            updates["stream_path"] = fields["IP URL"]
        if "IP Preset_Resolution" in fields:
            updates["stream_resolution"] = fields["IP Preset_Resolution"]
        if "IP Preset_Quality" in fields:
            updates["stream_quality"] = fields["IP Preset_Quality"]
        if "IP Enabled" in fields:
            flag = _as_bool(fields["IP Enabled"])
            if flag is not None:
                updates["ip_streaming_enabled"] = flag
        if "USB Active" in fields:
            flag = _as_bool(fields["USB Active"])
            if flag is not None:
                updates["usb_active"] = flag
        if "USB Device" in fields:
            updates["usb_device_name"] = fields["USB Device"]
        if "USB Resolution" in fields:
            updates["usb_resolution"] = fields["USB Resolution"]
        if "USB Frame_Rate" in fields:
            rate = _as_number(fields["USB Frame_Rate"])
            if rate is not None:
                updates["usb_frame_rate"] = int(rate)
        if "UVC Extensions_Enabled" in fields:
            flag = _as_bool(fields["UVC Extensions_Enabled"])
            if flag is not None:
                updates["uvc_extensions_enabled"] = flag
        if updates:
            self.set_states(updates)
        self._publish_preview()

    def _publish_preview(self) -> None:
        """Publish the RTSP address, so the Video Panel lists the camera.

        Only while the stream is actually on: a preview URL for a disabled
        stream is a tile that never paints.
        """
        enabled = bool(self.get_state("ip_streaming_enabled"))
        protocol = str(self.get_state("stream_protocol") or "").lower()
        path = str(self.get_state("stream_path") or "")
        port = self.get_state("stream_port")
        host = str(self.config.get("host", "") or "")
        if not (enabled and protocol == "rtsp" and path and host):
            self.set_states({"preview_url": "", "preview_format": ""})
            return
        port_part = f":{int(port)}" if port else ""
        self.set_states({
            "preview_url": f"rtsp://{host}{port_part}/{path}",
            "preview_format": "rtsp",
        })

    async def _read_network(self) -> None:
        reply = await self._ask("network settings get")
        if not reply.ok:
            return
        wanted = {
            "IP Address": "ip_address",
            "MAC Address": "mac_address",
            "Hostname": "hostname",
            "Gateway": "gateway",
        }
        updates: dict[str, Any] = {}
        for line in reply.lines:
            m = re.match(r"^(.+?)\s{2,}(.+)$", line.strip())
            if not m:
                continue
            key = wanted.get(m.group(1).strip())
            if key:
                updates[key] = m.group(2).strip()
        if updates:
            self.set_states(updates)

    async def _read_temperature(self) -> None:
        reply = await self._ask("temperature get")
        if not reply.ok:
            return
        for line in reply.lines:
            # "zynq_c  57.95 C  fault?  false  18 seconds ago"
            m = re.search(
                r"(-?[\d.]+)\s*C\s+fault\?\s+(\S+)", line.strip(), re.IGNORECASE
            )
            if not m:
                continue
            value = _as_number(m.group(1))
            fault = _as_bool(m.group(2))
            updates: dict[str, Any] = {}
            if value is not None:
                updates["temperature_c"] = round(value, 2)
            if fault is not None:
                updates["temperature_fault"] = fault
            if updates:
                self.set_states(updates)
            return

    async def _read_factory_reset(self) -> None:
        reply = await self._ask("system factory-reset get")
        if not reply.ok:
            return
        for line in reply.lines:
            m = re.match(
                r"^factory-reset \(software\):\s*(\S+)", line.strip()
            )
            if m:
                flag = _as_bool(m.group(1))
                if flag is not None:
                    self.set_state("factory_reset_armed", flag)
                return

    @staticmethod
    def _value_after(reply: VaddioReply, pattern: str) -> str | None:
        rx = re.compile(pattern)
        for line in reply.lines:
            m = rx.search(line)
            if m:
                return m.group(1).strip()
        return None

    # -- polling -----------------------------------------------------------

    async def poll(self) -> None:
        if not self.connected:
            return
        self._poll_count += 1
        every = max(1, int(self.config.get("slow_poll_every", 6) or 6))

        await self._read_modes()
        await self._read_position()
        await self._read_ccu()
        await self._read_audio()

        if self._poll_count % every == 1 or every == 1:
            await self._read_streaming()
            await self._read_temperature()
            await self._read_network()
            await self._read_factory_reset()

    # -- commands ----------------------------------------------------------

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        p = params or {}
        move_timeout = float(self.config.get("move_timeout", 60.0) or 60.0)

        # --- drive -------------------------------------------------------
        if command == "pan_left":
            return await self._do(f"camera pan left {int(p.get('speed', 12))}")
        if command == "pan_right":
            return await self._do(f"camera pan right {int(p.get('speed', 12))}")
        if command == "pan_stop":
            return await self._do("camera pan stop")
        if command == "tilt_up":
            return await self._do(f"camera tilt up {int(p.get('speed', 10))}")
        if command == "tilt_down":
            return await self._do(f"camera tilt down {int(p.get('speed', 10))}")
        if command == "tilt_stop":
            return await self._do("camera tilt stop")
        if command == "zoom_in":
            return await self._do(f"camera zoom in {int(p.get('speed', 3))}")
        if command == "zoom_out":
            return await self._do(f"camera zoom out {int(p.get('speed', 3))}")
        if command == "zoom_stop":
            return await self._do("camera zoom stop")
        if command == "home":
            # `camera home` acknowledges at once and keeps moving. Reading the
            # position now would publish a mid-travel angle that the next poll
            # then corrects, which on a panel reads as the camera drifting.
            # Measured: home from the far end of pan takes several seconds and
            # lands on a stored position, not on zero.
            return await self._do("camera home")
        if command == "recalibrate":
            return await self._do("camera recalibrate")

        # --- absolute ----------------------------------------------------
        # Every single-axis `set` carries `no_wait`. Without it the camera
        # holds its shell for the whole travel — measured at 21.9 s corner to
        # corner — and since one connection serves one command at a time,
        # that would stall polling, the liveness probe and every other button
        # on the panel for the duration.
        if command == "pan_set":
            return await self._do(
                f"camera pan set {self._num(p.get('position', 0))} no_wait"
            )
        if command == "tilt_set":
            return await self._do(
                f"camera tilt set {self._num(p.get('position', 0))} no_wait"
            )
        if command == "zoom_set":
            return await self._do(
                f"camera zoom set {self._num(p.get('level', 1))} no_wait"
            )
        if command == "ptz_position_set":
            wait = bool(p.get("wait", False))
            line = (
                f"camera ptz-position set"
                f" pan {self._num(p.get('pan', 0))}"
                f" tilt {self._num(p.get('tilt', 0))}"
                f" zoom {self._num(p.get('zoom', 1))}"
            )
            if not wait:
                line += " no_wait"
            ok = await self._do(line, timeout=move_timeout if wait else None)
            if wait:
                await self._read_position()
            return ok
        if command == "query_position":
            await self._read_position()
            return True

        # --- focus -------------------------------------------------------
        if command == "focus_near":
            return await self._do(f"camera focus near {int(p.get('speed', 4))}")
        if command == "focus_far":
            return await self._do(f"camera focus far {int(p.get('speed', 4))}")
        if command == "focus_stop":
            return await self._do("camera focus stop")
        if command in ("focus_auto_on", "focus_auto_off"):
            mode = "auto" if command == "focus_auto_on" else "manual"
            ok = await self._do(f"camera focus mode {mode}")
            self.set_state("focus_auto", mode == "auto")
            return ok

        # --- presets -----------------------------------------------------
        if command == "preset_recall":
            # Acknowledged immediately, like `camera home`; the move outlives
            # the reply, so the position comes from the next poll rather than
            # from a read taken while the camera is still travelling.
            return await self._do(f"camera preset recall {int(p['preset'])}")
        if command == "preset_store":
            return await self._do(f"camera preset store {int(p['preset'])}")
        if command == "preset_store_with_ccu":
            return await self._do(
                f"camera preset store {int(p['preset'])} save-ccu"
            )

        # --- CCU ---------------------------------------------------------
        ccu_bool = {
            "ccu_set_auto_iris": "auto_iris",
            "ccu_set_auto_white_balance": "auto_white_balance",
            "ccu_set_backlight_compensation": "backlight_compensation",
            "ccu_set_wide_dynamic_range": "wide_dynamic_range",
        }
        if command in ccu_bool:
            key = ccu_bool[command]
            word = "on" if self._flag(p.get("value")) else "off"
            ok = await self._do(f"camera ccu set {key} {word}")
            self.set_state(key, word == "on")
            return ok
        ccu_int = {
            "ccu_set_iris": "iris",
            "ccu_set_gain": "gain",
            "ccu_set_red_gain": "red_gain",
            "ccu_set_blue_gain": "blue_gain",
            "ccu_set_detail": "detail",
            "ccu_set_chroma": "chroma",
            "ccu_set_gamma": "gamma",
        }
        if command in ccu_int:
            key = ccu_int[command]
            value = int(p["value"])
            ok = await self._do(f"camera ccu set {key} {value}")
            self.set_state(key, value)
            return ok
        if command == "query_ccu":
            await self._read_ccu()
            return True

        # --- audio -------------------------------------------------------
        if command in (
            "audio_volume_set", "audio_volume_up", "audio_volume_down",
            "audio_mute_on", "audio_mute_off", "audio_mute_toggle",
        ):
            return await self._audio_command(command, p)
        if command == "master_mute_toggle":
            ok = await self._audio_write("audio master mute toggle")
            await self._read_audio()
            return ok

        # --- video mute / LED / IR ---------------------------------------
        if command in ("video_mute_on", "video_mute_off", "video_mute_toggle"):
            action = command.rsplit("_", 1)[1]
            ok = await self._do(f"video mute {action}")
            if action == "toggle":
                current = self.get_state("video_muted")
                if isinstance(current, bool):
                    self.set_state("video_muted", not current)
            else:
                self.set_state("video_muted", action == "on")
            return ok
        if command in ("led_on", "led_off"):
            word = "on" if command == "led_on" else "off"
            ok = await self._do(f"camera led {word}")
            self.set_state("led_on", word == "on")
            return ok
        if command in ("ir_cut_filter_on", "ir_cut_filter_off"):
            word = "on" if command == "ir_cut_filter_on" else "off"
            ok = await self._do(f"camera icr {word}")
            self.set_state("ir_cut_filter", word == "on")
            return ok

        # --- standby -----------------------------------------------------
        if command in ("standby_on", "standby_off", "standby_toggle"):
            action = command.rsplit("_", 1)[1]
            ok = await self._do(f"camera standby {action}", timeout=move_timeout)
            await self._read_modes()
            return ok

        # --- triggers / streaming ----------------------------------------
        if command in ("trigger_on", "trigger_off"):
            word = "on" if command == "trigger_on" else "off"
            return await self._do(f"trigger {int(p['index'])} {word}")
        if command in ("streaming_on", "streaming_off", "streaming_toggle"):
            action = {"streaming_on": "on", "streaming_off": "off",
                      "streaming_toggle": "toggle"}[command]
            ok = await self._do(f"streaming ip enable {action}")
            await self._read_streaming()
            return ok
        if command == "query_streaming":
            await self._read_streaming()
            return True

        # --- system ------------------------------------------------------
        if command == "reboot":
            return await self._do("system reboot", timeout=10.0)
        if command == "query_version":
            await self._read_identity()
            return True
        if command == "query_network":
            await self._read_network()
            return True
        if command == "query_temperature":
            await self._read_temperature()
            return True

        raise ValueError(f"Unknown command: {command}")

    async def _audio_command(self, command: str, p: dict[str, Any]) -> bool:
        channel = str(p.get("channel", "") or "")
        valid = {cid for cid, _ in AUDIO_CHANNELS}
        if channel not in valid:
            raise ValueError(
                f"Unknown audio channel {channel!r}; "
                f"expected one of {', '.join(sorted(valid))}"
            )
        if command == "audio_volume_set":
            level = float(p["level"])
            ok = await self._audio_write(
                f"audio {channel} volume set {self._num(level)}"
            )
        elif command == "audio_volume_up":
            ok = await self._audio_write(f"audio {channel} volume up")
        elif command == "audio_volume_down":
            ok = await self._audio_write(f"audio {channel} volume down")
        else:
            action = {"audio_mute_on": "on", "audio_mute_off": "off",
                      "audio_mute_toggle": "toggle"}[command]
            ok = await self._audio_write(f"audio {channel} mute {action}")

        # Read the channel back rather than assuming. `volume up` clamps at
        # the ceiling, a mute toggle depends on where it started, and a
        # refused write must not leave a panel showing a level the camera
        # declined.
        await self._refresh_channel(channel)
        return ok

    async def _audio_write(self, command: str) -> bool:
        """Send an audio write and explain the refusal the camera does not.

        Two guards on this hardware answer a write and say little about why,
        and both are things an integrator hits on a live panel:

        - In standby the camera forces master mute on, refuses to clear it
          (a bare ERROR with no sentence at all), and then refuses a mic
          channel's mute with "Cannot modify while master mute is enabled."
          Waking the camera clears all of it.
        - Awake, master mute still blocks a mic channel's own mute for as
          long as it is on.

        A button that reports "the camera rejected the command" sends its
        user to the manual; one that names standby sends them to the right
        switch.
        """
        try:
            return await self._do(command)
        except ValueError as exc:
            reason = str(exc)
            if self.get_state("standby") is True:
                raise ValueError(
                    f"{reason} The camera is in standby, which forces master "
                    f"mute on and locks the microphone channels. Take it out "
                    f"of standby first."
                ) from exc
            if "master mute" in reason.lower():
                raise ValueError(
                    f"{reason} Turn master mute off before setting this "
                    f"channel."
                ) from exc
            raise

    async def _refresh_channel(self, channel: str) -> None:
        vol = await self._ask(f"audio {channel} volume get")
        mute = await self._ask(f"audio {channel} mute get")
        updates: dict[str, Any] = {}
        if vol.ok:
            raw = self._value_after(vol, r"volume:\s*(-?[\d.]+)\s*dB")
            value = _as_number(raw) if raw is not None else None
            if value is not None:
                updates["volume_db"] = round(value, 2)
        if mute.ok:
            flag = self._value_after(mute, r"mute:\s*(\S+)")
            if flag is not None:
                updates["muted"] = _as_bool(flag)
        if updates:
            self.set_child_state_batch("audio_channel", channel, updates)

    async def set_device_setting(self, key: str, value: Any) -> Any:
        writes = {
            "auto_iris": lambda v: f"camera ccu set auto_iris {self._word(v)}",
            "auto_white_balance":
                lambda v: f"camera ccu set auto_white_balance {self._word(v)}",
            "backlight_compensation":
                lambda v: f"camera ccu set backlight_compensation {self._word(v)}",
            "wide_dynamic_range":
                lambda v: f"camera ccu set wide_dynamic_range {self._word(v)}",
            "iris": lambda v: f"camera ccu set iris {int(v)}",
            "gain": lambda v: f"camera ccu set gain {int(v)}",
            "red_gain": lambda v: f"camera ccu set red_gain {int(v)}",
            "blue_gain": lambda v: f"camera ccu set blue_gain {int(v)}",
            "detail": lambda v: f"camera ccu set detail {int(v)}",
            "chroma": lambda v: f"camera ccu set chroma {int(v)}",
            "gamma": lambda v: f"camera ccu set gamma {int(v)}",
            "led_on": lambda v: f"camera led {self._word(v)}",
            "ir_correction": lambda v: f"camera focus ir-correction {v}",
        }
        if key not in writes:
            raise ValueError(f"Unknown device setting: {key}")

        await self._do(writes[key](value))

        # Read back through the same query the state variable is polled
        # from, so the setting shows what the camera has rather than what
        # was asked for.
        if key == "led_on":
            await self._read_modes()
        elif key == "ir_correction":
            await self._read_modes()
        else:
            await self._read_ccu()
        return True

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _flag(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("on", "true", "1", "yes")

    @classmethod
    def _word(cls, value: Any) -> str:
        return "on" if cls._flag(value) else "off"

    @staticmethod
    def _num(value: Any) -> str:
        """Render a number the way the shell's grammar accepts it."""
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:g}"
