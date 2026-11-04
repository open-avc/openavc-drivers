"""
OpenAVC WolfVision Cynap driver.

Controls WolfVision Cynap presentation and collaboration systems on firmware
1.7x (Cynap Pro, Cynap Core Pro, Cynap Pure Mini) over WolfProt, WolfVision's
binary control protocol, on TCP port 50915 (50917 with TLS).

Protocol source (WolfVision, public): "WolfProt" command reference for Cynap
firmware 1.78a, https://wolfvision.com/api_wolfprot/commands_1.78a.html, the
reference each of these products' support page links. Ports from WolfVision's
"Cynap Pro Network Integration" guide.

Frames: ``<header> [<extended header>] <command:2> <length:1|2|4> <payload>``.
Header bit 0 marks a SET, bit 1 an extended header (whose bit 0 means a
four-byte length), bit 2 a two-byte length, bit 3 a two-byte command, bit 7 an
error reply (the byte after the command is then the error code and nothing
follows). Lengths are big-endian (the reference writes 15 360 as ``3C00`` and
4 MiB as ``00400000``); numbers inside a payload are little-endian.

Why Python: the binary framing above (three length widths, error replies of a
different shape), a login command that sets the connection's access level,
subscriptions whose notices arrive on the control connection between replies,
and a window list of four length-prefixed blocks fanned out into children.

Push vs poll: HYBRID. Subscribe (CD40) asks the Cynap to report power,
volume, mutes, freeze, presentation status, screensaver, recording,
presentation mode and power-down mode as they change. The reference does not
show a notice: this driver reads every GET-shaped frame as the value it names,
reply or notice, so a notice in that shape lands at once, and the poll reads
every value either way. Unverified on a unit.

Standby: Power Off puts the Cynap in standby, which closes every connection
(Network Integration guide). Power On then needs Wake-on-LAN; the protocol's
own Power On works only within 30 seconds of Power Off (reference, Power).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple, Optional

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.transport.frame_parsers import CallableFrameParser, FrameParser
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1 has no typed refusal
    CommandRefusedError = RuntimeError

try:
    from openavc.drivers.base import WakeAddressError
except ImportError:  # OpenAVC before 0.37.1 has no typed wake refusal
    WakeAddressError = ValueError

log = get_logger(__name__)

REPLY_TIMEOUT_S = 3.0
#: A payload this large is never one this driver asked for: resync instead.
MAX_PAYLOAD = 1 << 20
#: Every this many polls, identity and settings are read again.
SETTINGS_EVERY = 8

# ── Command codes (reference 1.78a) ──────────────────────────────────────────

MODEL = 0xCB00
REMOTE = 0xCB01
BOXNAME = 0xCB03
LAN_MAC = 0xCB09
POWER = 0xCB0C
STREAMING = 0xCB20
RECORDING = 0xCB25
WINDOW_CONTROL = 0xCB28
PRESENTATION_MODE = 0xCB38
LOGIN = 0xCB42
END_PRESENTATION = 0xCB49
FIRMWARE = 0xCB50
LOGIN_LEVEL = 0xCB55
VOLUME = 0xCB56
MUTE = 0xCB58
MIC_MUTE = 0xCB59
FREEZE = 0xCB5A
AUX_MODE = 0xCB5B
SERIAL = 0xCB60
MIC_STATUS = 0xCBEA
SCREENSAVER = 0xCC76
POWER_DOWN_MODE = 0xCCC7
REBOOT = 0xCCF5
SCREENSAVER_OFF = 0xCD00
PRESENTATION = 0xCD30
HDMI_INPUT = 0xCD3B
SUBSCRIBE = 0xCD40
START_APP = 0xCD60
WINDOWS = 0xCD85

#: What Subscribe asks the Cynap to report. The reference's list also names
#: Simple Windows Basic as 0x08CD7300, a code its command list does not have
#: (Simple Windows Basic is CD85), so the window list is polled instead.
SUBSCRIBED = (
    POWER, PRESENTATION, SCREENSAVER, PRESENTATION_MODE, FREEZE, MUTE,
    MIC_MUTE, VOLUME, RECORDING, POWER_DOWN_MODE,
)

ERRORS = {
    0x01: "timeout",
    0x02: "invalid command",
    0x03: "invalid parameter",
    0x04: "invalid length",
    0x05: "FIFO full",
    0x06: "firmware update error",
    0x07: "access denied",
    0x08: "authentication required",
    0x09: "busy",
    0x0A: "SIP required",
    0x0B: "power off",
}
INVALID_COMMAND = 0x02
ACCESS_DENIED = 0x07
AUTH_REQUIRED = 0x08

LEVELS = {0x00: "none", 0x01: "user", 0x02: "admin", 0x03: "annotation", 0x04: "viewer"}
LOGIN_LEVELS = {"user": 0x01, "admin": 0x02}
SCREENSAVER_STATES = {0: "off", 1: "standby", 2: "screen_off", 3: "screensaver"}
RECORDING_STATES = {0: "recording", 1: "paused", 2: "stopped"}
PRESENTATION_MODES = {"meeting": 0, "lecture": 1}
POWER_DOWN_MODES = {"suspend": 0, "shutdown": 1, "power_saving": 2}
AUX_MODES = {
    "mirror": 0, "mirror_1080p60": 1, "mirror_720p60": 2,
    "content_1080p60": 3, "content_720p60": 4, "moderator_1080p60": 5,
}
MIC_STATES = {0: "disabled", 1: "working", 2: "quiet"}
END_MODES = {"new_presentation": 0, "standby": 1, "screen_off": 2, "screensaver": 3}
OUTPUTS = {"main": 0x0001, "aux": 0x0002, "both": 0x0003}
REMOTE_KEYS = {
    "left": 0x01, "right": 0x02, "up": 0x03, "down": 0x04, "ok": 0x05,
    "menu": 0x06, "red": 0x07, "green": 0x08, "yellow": 0x09, "blue": 0x0A,
    "toolbox": 0x0B,
}
WINDOW_TYPES = {
    0x00: "none", 0x01: "visualizer", 0x02: "hdmi", 0x03: "browser",
    0x04: "miracast", 0x05: "airplay_chromecast", 0x06: "video",
    0x07: "vsolution_cast", 0x08: "image", 0x09: "pdf", 0x0A: "powerpoint",
    0x0B: "document", 0x0C: "spreadsheet", 0x0D: "whiteboard", 0x0E: "audio",
    0x0F: "webconference", 0x10: "webcam", 0x11: "stream_input",
    0x13: "office365_outlook", 0x14: "office365_word", 0x15: "office365_excel",
    0x16: "office365_powerpoint", 0x17: "office365_onenote",
    0x18: "vmatrix_pull", 0x19: "vmatrix_push", 0x1A: "vmatrix_browser",
    0x1B: "office365_teams", 0x1C: "vmatrix_groupwork",
    0x1E: "office365_whiteboard", 0x1F: "office365_planner",
}
WINDOW_ACTIONS = {"close": 0x00, "fullscreen": 0x01, "normal": 0x02}


# ── Framing (module level so tests/protocol_math can reach it) ───────────────

class Frame(NamedTuple):
    is_set: bool
    command: int
    payload: bytes
    error: Optional[int]


def build_frame(command: int, payload: bytes = b"", *, is_set: bool = False,
                length_width: Optional[int] = None) -> bytes:
    """One WolfProt request: header, two-byte command, length, payload.

    ``length_width`` follows the reference where it prints a fixed header
    (Subscribe's ``0D CD40 ab cd``); otherwise the smallest width that fits.
    """
    n = len(payload)
    width = length_width or (1 if n <= 0xFF else 2 if n <= 0xFFFF else 4)
    header = 0x08 | (0x01 if is_set else 0x00)
    ext = b""
    if width == 2:
        header |= 0x04
    elif width == 4:
        header |= 0x02
        ext = b"\x01"
    return bytes([header]) + ext + command.to_bytes(2, "big") + n.to_bytes(width, "big") + payload


def frame_size(buf: bytes) -> Optional[int]:
    """Bytes in the first frame of ``buf``, None while it is incomplete.

    Raises ValueError when the bytes cannot begin a frame (a reserved header
    bit set, a length no reply here could have), so the parser can resync.
    """
    if not buf:
        return None
    header = buf[0]
    if header & 0x70:
        raise ValueError("reserved header bits set")
    pos = 1
    ext = 0
    if header & 0x02:
        if len(buf) < 2:
            return None
        ext = buf[1]
        if ext & 0xFE:
            raise ValueError("reserved extended header bits set")
        pos = 2
    pos += 2 if header & 0x08 else 1
    if header & 0x80:
        return pos + 1 if len(buf) >= pos + 1 else None
    width = 4 if (header & 0x02 and ext & 0x01) else 2 if header & 0x04 else 1
    if len(buf) < pos + width:
        return None
    length = int.from_bytes(buf[pos:pos + width], "big")
    if length > MAX_PAYLOAD:
        raise ValueError("payload too large")
    total = pos + width + length
    return total if len(buf) >= total else None


def parse_frame(buffer: bytes) -> tuple[Optional[bytes], bytes]:
    """CallableFrameParser contract: (one frame, the rest) or (None, kept)."""
    try:
        size = frame_size(buffer)
    except ValueError:
        return None, buffer[1:]
    if size is None:
        return None, buffer
    return buffer[:size], buffer[size:]


def decode_frame(frame: bytes) -> Optional[Frame]:
    """Split one whole frame into its parts; None if it is not one."""
    try:
        if frame_size(frame) != len(frame):
            return None
    except ValueError:
        return None
    header = frame[0]
    pos = 2 if header & 0x02 else 1
    cwidth = 2 if header & 0x08 else 1
    command = int.from_bytes(frame[pos:pos + cwidth], "big")
    pos += cwidth
    if header & 0x80:
        return Frame(bool(header & 0x01), command, b"", frame[pos])
    width = 4 if (header & 0x02 and frame[1] & 0x01) else 2 if header & 0x04 else 1
    return Frame(bool(header & 0x01), command, frame[pos + width:], None)


def windows_blocks(payload: bytes) -> list[bytes]:
    """Simple Windows Basic: a block length and block data, four times.

    Walked by each block's own length, so a field added later does not move
    the next window.
    """
    blocks = []
    pos = 0
    for _ in range(4):
        if pos >= len(payload):
            break
        size = payload[pos]
        blocks.append(payload[pos + 1:pos + 1 + size])
        pos += 1 + size
    return blocks


def subscribe_payload(codes: tuple[int, ...] = SUBSCRIBED) -> bytes:
    """Subscribe's list: each subscribed command as its own GET request."""
    return b"".join(build_frame(code) for code in codes)


def login_payload(level: int, password: str, pin: str = "") -> bytes:
    """Login: access level, password length and password, PIN length and PIN."""
    pw = password.encode("utf-8")
    pn = pin.encode("utf-8")
    return bytes([level, len(pw)]) + pw + bytes([len(pn)]) + pn


class WolfProtError(Exception):
    """The Cynap answered a request with an error code."""

    def __init__(self, command: int, code: int):
        self.command = command
        self.code = code
        super().__init__(f"{command:04X}: {ERRORS.get(code, f'error 0x{code:02X}')}")


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip("\x00").strip()


class WolfVisionCynapDriver(BaseDriver):
    """WolfVision Cynap (firmware 1.7x) over WolfProt."""

    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The Cynap stopped answering status queries."

    DRIVER_INFO = {
        "id": "wolfvision_cynap",
        "name": "WolfVision Cynap",
        "manufacturer": "WolfVision",
        "category": "streaming",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls WolfVision Cynap presentation and collaboration systems "
            "(Cynap Pro, Cynap Core Pro, Cynap Pure Mini on firmware 1.7x) over "
            "WolfProt: standby and Wake-on-LAN, volume and mutes, freeze, the "
            "four presentation windows, opening an HDMI input or the "
            "whiteboard, recording and streaming, ending a presentation, and "
            "status pushed as it changes."
        ),
        "source_url": "https://wolfvision.com/api_wolfprot/commands_1.78a.html",
        "tags": ["wireless-presentation", "byod", "collaboration", "wolfvision", "cynap", "recording"],
        "verified": False,
        "simulated": True,
        "ports": [50915],
        "min_platform_version": "0.37.0",
        "transport": "tcp",
        "discovery": {
            # GET Model (user level None) on the control port; a Cynap answers
            # 08 CB00 <length> <model>. WolfVision's own IEEE block (MA-L).
            "tcp_probe": {
                "port": 50915,
                "send_hex": "08CB0000",
                "expect_hex": "08CB00",
                "timeout_ms": 3000,
            },
            "oui": ["00:0c:92"],
            "manufacturer_alias": ["wolfvision", "wolfvision gmbh"],
        },
        "compatible_models": [
            {
                "manufacturer": "WolfVision",
                "models": ["Cynap Pro", "Cynap Core Pro"],
                "confidence": "untested",
                "notes": (
                    "Firmware 1.7x (built from the 1.78a WolfProt reference). Two "
                    "HDMI outputs, recording and streaming."
                ),
            },
            {
                "manufacturer": "WolfVision",
                "models": ["Cynap Pure Mini"],
                "confidence": "untested",
                "notes": (
                    "Firmware 1.7x. Commands for features this model lacks "
                    "(recording, a second output) are refused by the unit."
                ),
            },
        ],
        "help": {
            "overview": (
                "WolfVision Cynap presentation systems over WolfProt (TCP 50915). "
                "Standby and Wake-on-LAN, master volume and mute, microphone "
                "mute, freeze, the four presentation windows, HDMI inputs and the "
                "whiteboard, recording and streaming, and status the Cynap "
                "reports as it changes. Polls every 15 seconds as well."
            ),
            "setup": (
                "1. Give the Cynap a fixed IP address and enter it. Control uses "
                "TCP 50915; for TLS, turn on TLS and use port 50917.\n"
                "2. Login: with no moderator password on the Cynap, leave the "
                "password blank. If the Cynap requires a moderator password "
                "(Settings > Login), enter it with Access Level User. Changing "
                "the device name, presentation mode, power-down mode, output 2 "
                "mode or restarting needs Access Level Admin and the "
                "administrator password (and the Admin Remote PIN if the Cynap "
                "asks for one).\n"
                "3. Power On from standby uses Wake-on-LAN: the MAC address is "
                "read from the Cynap when it first connects. Standby closes the "
                "Cynap's connections, so it shows offline until it is woken. A "
                "Power Down Mode of Shutdown may not wake on the network."
            ),
        },
        "default_config": {
            "host": "",
            "port": 50915,
            "ssl": False,
            "verify_ssl": False,
            "access_level": "user",
            "password": "",
            "admin_pin": "",
            "mac_address": "",
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 50915, "label": "Port",
                     "description": "50915, or 50917 with TLS."},
            "ssl": {"type": "boolean", "default": False, "label": "TLS",
                    "description": "Use the Cynap's TLS control port (50917)."},
            "verify_ssl": {"type": "boolean", "default": False, "label": "Verify Certificate",
                           "description": "The Cynap's certificate is self-signed: leave off unless a trusted one is installed."},
            "access_level": {"type": "enum", "values": ["user", "admin"], "default": "user",
                             "label": "Access Level",
                             "description": "Admin is needed to change settings and restart the Cynap."},
            "password": {"type": "string", "default": "", "secret": True, "label": "Password",
                         "description": "The moderator (User) or administrator (Admin) password. Blank: no login."},
            "admin_pin": {"type": "string", "default": "", "secret": True, "label": "Admin Remote PIN",
                          "description": "Only when the Cynap asks for a PIN with the administrator password."},
            "mac_address": {"type": "string", "default": "", "label": "MAC Address",
                            "learned_from": "mac_address",
                            "description": "For Wake-on-LAN. Read from the Cynap when it connects."},
            "poll_interval": {"type": "integer", "default": 15, "min": 5,
                              "label": "Poll Interval (sec)"},
        },
        "state_variables": {
            "power": {"type": "enum", "values": ["on", "off"], "label": "Power",
                      "control": True, "cloud_priority": "high"},
            "volume": {"type": "integer", "label": "Volume", "min": 0, "max": 100,
                       "unit": "%", "control": True},
            "mute": {"type": "boolean", "label": "Mute", "control": True,
                     "cloud_priority": "high"},
            "mic_mute": {"type": "boolean", "label": "Microphone Mute", "control": True,
                         "cloud_priority": "high"},
            "freeze": {"type": "boolean", "label": "Freeze", "control": True,
                       "help": "The main output's picture is frozen."},
            "presentation_active": {"type": "boolean", "label": "Presentation Active",
                                    "cloud_priority": "high"},
            "screensaver": {"type": "enum", "label": "Screensaver",
                            "values": ["off", "standby", "screen_off", "screensaver"],
                            "help": "Which idle state the Cynap is in, if any."},
            "recording": {"type": "enum", "values": ["recording", "paused", "stopped"],
                          "label": "Recording", "control": True, "cloud_priority": "high"},
            "recording_time": {"type": "integer", "label": "Recording Time", "unit": "s",
                               "cloud_priority": "low"},
            "streaming": {"type": "boolean", "label": "Streaming", "control": True},
            "windows_open": {"type": "integer", "label": "Windows Open", "min": 0, "max": 4},
            "presentation_mode": {"type": "enum", "values": ["meeting", "lecture"],
                                  "label": "Presentation Mode"},
            "power_down_mode": {"type": "enum", "values": ["suspend", "shutdown", "power_saving"],
                                "label": "Power Down Mode",
                                "help": "What standby does: Shutdown may not wake on the network."},
            "aux_mode": {"type": "enum", "label": "Output 2 Mode",
                         "values": ["mirror", "mirror_1080p60", "mirror_720p60",
                                    "content_1080p60", "content_720p60", "moderator_1080p60"]},
            "mic_status": {"type": "enum", "values": ["disabled", "working", "quiet"],
                           "label": "Microphone Status"},
            "hdmi_1_signal": {"type": "boolean", "label": "HDMI In 1 Signal"},
            "hdmi_1_resolution": {"type": "string", "label": "HDMI In 1 Resolution"},
            "hdmi_2_signal": {"type": "boolean", "label": "HDMI In 2 Signal"},
            "hdmi_2_resolution": {"type": "string", "label": "HDMI In 2 Resolution"},
            "login_level": {"type": "enum", "label": "Login Level",
                            "values": ["none", "user", "admin", "annotation", "viewer"]},
            "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
            "device_name": {"type": "string", "label": "Device Name", "cloud_priority": "low"},
            "serial_number": {"type": "string", "label": "Serial Number", "cloud_priority": "low"},
            "firmware_version": {"type": "string", "label": "Firmware Version", "cloud_priority": "low"},
            "mac_address": {"type": "string", "label": "MAC Address", "cloud_priority": "low"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "window": {
                "label": "Window",
                "label_plural": "Windows",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "state_variables": {
                    "content": {"type": "enum", "label": "Content",
                                "values": ["none", "visualizer", "hdmi", "browser", "miracast",
                                           "airplay_chromecast", "video", "vsolution_cast", "image",
                                           "pdf", "powerpoint", "document", "spreadsheet",
                                           "whiteboard", "audio", "webconference", "webcam",
                                           "stream_input", "office365_outlook", "office365_word",
                                           "office365_excel", "office365_powerpoint",
                                           "office365_onenote", "vmatrix_pull", "vmatrix_push",
                                           "vmatrix_browser", "office365_teams",
                                           "vmatrix_groupwork", "office365_whiteboard",
                                           "office365_planner", "other"]},
                    "open": {"type": "boolean", "label": "Open"},
                    "fullscreen": {"type": "boolean", "label": "Full Screen", "control": True},
                    "mute": {"type": "boolean", "label": "Mute", "control": True},
                },
                "summary_fields": ["content", "fullscreen", "mute"],
            },
        },
        "device_settings": {
            "device_name": {
                "type": "string", "label": "Device Name", "state_key": "device_name",
                "default": "", "setup": False,
                "help": "The Cynap's box name (up to 32 bytes). Needs Access Level Admin.",
            },
            "presentation_mode": {
                "type": "enum", "label": "Presentation Mode", "state_key": "presentation_mode",
                "values": ["meeting", "lecture"], "default": "meeting", "setup": False,
                "help": "Meeting: anyone may present. Lecture: a moderator admits presenters. Needs Access Level Admin.",
            },
            "power_down_mode": {
                "type": "enum", "label": "Power Down Mode", "state_key": "power_down_mode",
                "values": ["suspend", "shutdown", "power_saving"], "default": "suspend",
                "setup": False,
                "help": "What standby does. Needs Access Level Admin.",
            },
            "aux_mode": {
                "type": "enum", "label": "Output 2 Mode", "state_key": "aux_mode",
                "values": ["mirror", "mirror_1080p60", "mirror_720p60",
                           "content_1080p60", "content_720p60", "moderator_1080p60"],
                "default": "mirror", "setup": False,
                "help": "What the second HDMI output shows. Needs Access Level Admin.",
            },
        },
        "quick_actions": ["power_on", "power_off", "mute_on", "mute_off", "open_hdmi_input"],
        "commands": {
            "power_on": {
                "label": "Power On", "params": {}, "available_offline": True,
                "help": "Wakes the Cynap from standby with Wake-on-LAN, and turns it on if it was put in standby in the last 30 seconds.",
            },
            "power_off": {
                "label": "Standby", "params": {},
                "help": "Puts the Cynap in standby. Its connections close, so it shows offline until Power On wakes it.",
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {"level": {"type": "integer", "required": True, "min": 0, "max": 100,
                                     "label": "Volume (%)"}},
                "sets": {"volume": "{level}"},
                "help": "Sets the Cynap's master volume.",
            },
            "mute_on": {"label": "Mute", "params": {}, "sets": {"mute": True}, "help": "Mutes the Cynap's master audio."},
            "mute_off": {"label": "Unmute", "params": {}, "sets": {"mute": False}, "help": "Unmutes the Cynap's master audio."},
            "mute_toggle": {"label": "Toggle Mute", "params": {},
                            "help": "Mutes or unmutes the Cynap's master audio."},
            "mic_mute_on": {"label": "Mute Microphone", "params": {}, "sets": {"mic_mute": True},
                            "help": "Mutes the microphone connected to the Cynap."},
            "mic_mute_off": {"label": "Unmute Microphone", "params": {}, "sets": {"mic_mute": False},
                             "help": "Unmutes the microphone connected to the Cynap."},
            "mic_mute_toggle": {"label": "Toggle Microphone Mute", "params": {},
                                "help": "Mutes or unmutes the microphone connected to the Cynap."},
            "freeze_on": {"label": "Freeze", "params": {}, "sets": {"freeze": True},
                          "help": "Freezes the picture on the main output."},
            "freeze_off": {"label": "Unfreeze", "params": {}, "sets": {"freeze": False},
                           "help": "Unfreezes the picture on the main output."},
            "freeze_toggle": {"label": "Toggle Freeze", "params": {},
                              "help": "Freezes or unfreezes the picture on the main output."},
            "open_hdmi_input": {
                "label": "Show HDMI Input",
                "params": {
                    "input": {"type": "enum", "required": True, "values": ["hdmi_1", "hdmi_2"],
                              "default": "hdmi_1", "label": "Input"},
                    "output": {"type": "enum", "required": True, "values": ["main", "aux", "both"],
                               "default": "main", "label": "Output"},
                },
                "help": "Opens a window showing the HDMI input on the output.",
            },
            "open_whiteboard": {
                "label": "Open Whiteboard",
                "params": {"output": {"type": "enum", "required": True, "values": ["main", "aux", "both"],
                                      "default": "main", "label": "Output"}},
                "help": "Opens a whiteboard window on the output.",
            },
            "window_close": {
                "label": "Close Window",
                "params": {"window": {"type": "child_id", "child_type": "window", "required": True,
                                      "label": "Window"}},
                "sets": {"open": False},
                "help": "Closes the window on the Cynap's screen.",
            },
            "window_fullscreen": {
                "label": "Window Full Screen",
                "params": {"window": {"type": "child_id", "child_type": "window", "required": True,
                                      "label": "Window"}},
                "sets": {"fullscreen": True},
                "help": "Shows the window full screen.",
            },
            "window_normal": {
                "label": "Window Normal Size",
                "params": {"window": {"type": "child_id", "child_type": "window", "required": True,
                                      "label": "Window"}},
                "sets": {"fullscreen": False},
                "help": "Returns the window to its normal size beside the others.",
            },
            "window_mute": {
                "label": "Mute Window",
                "params": {"window": {"type": "child_id", "child_type": "window", "required": True,
                                      "label": "Window"}},
                "sets": {"mute": True},
                "help": "Mutes the window's sound.",
            },
            "window_unmute": {
                "label": "Unmute Window",
                "params": {"window": {"type": "child_id", "child_type": "window", "required": True,
                                      "label": "Window"}},
                "sets": {"mute": False},
                "help": "Unmutes the window's sound.",
            },
            "window_volume": {
                "label": "Set Window Volume",
                "params": {
                    "window": {"type": "child_id", "child_type": "window", "required": True,
                               "label": "Window"},
                    "level": {"type": "integer", "required": True, "min": 0, "max": 125,
                              "label": "Volume (%)"},
                },
                "help": "Sets the window's own volume (up to 125%).",
            },
            "recording_start": {"label": "Start Recording", "params": {}, "sets": {"recording": "recording"},
                                "help": "Starts recording the presentation."},
            "recording_pause_resume": {"label": "Pause or Resume Recording", "params": {},
                                       "help": "Pauses the recording, or resumes a paused one."},
            "recording_stop": {"label": "Stop Recording", "params": {}, "sets": {"recording": "stopped"},
                               "help": "Stops the recording and saves it."},
            "streaming_on": {"label": "Start Streaming", "params": {}, "sets": {"streaming": True},
                             "help": "Starts the Cynap's stream."},
            "streaming_off": {"label": "Stop Streaming", "params": {}, "sets": {"streaming": False},
                              "help": "Stops the Cynap's stream."},
            "screensaver_off": {"label": "Wake Screen", "params": {}, "sets": {"screensaver": "off"},
                                "help": "Ends the screensaver and shows the screen again."},
            "end_presentation": {
                "label": "End Presentation",
                "params": {"mode": {"type": "enum", "required": True,
                                    "values": ["new_presentation", "standby", "screen_off", "screensaver"],
                                    "default": "new_presentation", "label": "Then"}},
                "help": "Closes every window and starts a new presentation, or goes to standby, screen off or the screensaver. Saved files are kept.",
            },
            "end_presentation_and_delete": {
                "label": "End Presentation and Delete Files",
                "params": {"mode": {"type": "enum", "required": True,
                                    "values": ["new_presentation", "standby", "screen_off", "screensaver"],
                                    "default": "new_presentation", "label": "Then"}},
                "confirm": "Ends the presentation and deletes the recordings, snapshots and whiteboards saved on the Cynap.",
                "help": "Ends the presentation and deletes the recordings, snapshots and whiteboards saved on the Cynap.",
            },
            "remote_key": {
                "label": "Send Remote Key",
                "params": {"key": {"type": "enum", "required": True, "label": "Key",
                                   "values": ["left", "right", "up", "down", "ok", "menu", "red",
                                              "green", "yellow", "blue", "toolbox"]}},
                "help": "Presses a key on the Cynap's remote control.",
            },
            "reboot": {
                "label": "Restart Cynap", "params": {},
                "help": "Restarts the Cynap at once. It is offline until it has started. Needs Access Level Admin.",
            },
        },
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any):
        super().__init__(device_id, config, state, events)
        self._waiters: dict[tuple[bool, int], list[asyncio.Future]] = {}
        self._lock = asyncio.Lock()
        self._unsupported: set[int] = set()
        self._refused: dict[int, str] = {}
        self._cycle = 0

    # ── Transport ────────────────────────────────────────────────────────────

    def _create_frame_parser(self) -> Optional[FrameParser]:
        return CallableFrameParser(parse_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        return None

    async def _request(self, command: int, payload: bytes = b"", *, is_set: bool = False,
                       length_width: Optional[int] = None,
                       timeout: float = REPLY_TIMEOUT_S) -> bytes:
        """Send one request and wait for its reply; one request at a time.

        The reference: replies come in the order requests arrive, and the best
        practice is to wait for each reply before sending the next request.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._lock:
            key = (is_set, command)
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._waiters.setdefault(key, []).append(fut)
            try:
                await self.transport.send(
                    build_frame(command, payload, is_set=is_set, length_width=length_width))
                return await asyncio.wait_for(fut, timeout)
            finally:
                waiters = self._waiters.get(key, [])
                if fut in waiters:
                    waiters.remove(fut)

    async def _read(self, command: int, payload: bytes = b"", label: str = "") -> Optional[bytes]:
        """A GET whose refusal is not a fault: an unsupported or refused read
        is skipped. Silence still raises, so a poll stops at the first one."""
        if command in self._unsupported:
            return None
        try:
            data = await self._request(command, payload)
        except WolfProtError as exc:
            if exc.code == INVALID_COMMAND:
                self._unsupported.add(command)
            elif exc.code in (ACCESS_DENIED, AUTH_REQUIRED):
                self._refused[command] = label or f"{command:04X}"
            return None
        self._refused.pop(command, None)
        return data

    async def _set(self, command: int, payload: bytes, label: str,
                   read_back: Optional[int] = None,
                   length_width: Optional[int] = None) -> bool:
        """A SET, refused into a sentence, then the value read back."""
        try:
            await self._request(command, payload, is_set=True, length_width=length_width)
        except WolfProtError as exc:
            raise CommandRefusedError(self._refusal(label, exc.code)) from None
        except asyncio.TimeoutError:
            raise RuntimeError(f"The Cynap did not answer {label}.") from None
        if read_back is not None:
            await self._read(read_back)
        return True

    def _refusal(self, label: str, code: int) -> str:
        if code in (ACCESS_DENIED, AUTH_REQUIRED):
            level = str(self.get_state("login_level") or "")
            return (f"The Cynap refused {label}: it needs a login with more rights "
                    f"(logged in as {level or 'nobody'}).")
        if code == INVALID_COMMAND:
            return f"The Cynap does not support {label}."
        return f"The Cynap refused {label} ({ERRORS.get(code, f'error 0x{code:02X}')})."

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _post_connect(self) -> None:
        """Log in, when a password is set, before the device is reported connected."""
        self._unsupported.clear()
        self._refused.clear()
        await self._login()

    async def _login(self) -> None:
        password = str(self.config.get("password") or "")
        if not password:
            return
        level_name = str(self.config.get("access_level") or "user").lower()
        level = LOGIN_LEVELS.get(level_name, 0x01)
        pin = str(self.config.get("admin_pin") or "") if level == 0x02 else ""
        try:
            await self._request(LOGIN, login_payload(level, password, pin), is_set=True)
        except WolfProtError as exc:
            if exc.code in (ACCESS_DENIED, AUTH_REQUIRED):
                which = "administrator" if level == 0x02 else "moderator"
                pin_note = " or the Admin Remote PIN" if pin else ""
                raise ConnectionFaultError(
                    f"The Cynap refused the {which} password{pin_note}.",
                    code="auth_failed",
                ) from None
            raise ConnectionError(f"The Cynap refused the login ({ERRORS.get(exc.code, exc.code)}).") from None
        except asyncio.TimeoutError:
            raise ConnectionError("The Cynap did not answer the login.") from None

    async def _initial_sync(self) -> None:
        self._cycle = 0
        for index in range(1, 5):
            self.register_child("window", index)
        try:
            await self._request(SUBSCRIBE, subscribe_payload(), is_set=True, length_width=2)
        except WolfProtError as exc:
            log.info(f"[{self.device_id}] Subscribe refused ({exc}); status is polled only")
        except asyncio.TimeoutError:
            log.info(f"[{self.device_id}] Subscribe was not answered; status is polled only")
        await self.poll()

    async def poll(self) -> None:
        # Power answers at every login level and in every state that keeps the
        # connection open; its silence is the link gone.
        await self._request(POWER)
        self._cycle += 1
        for command, label in (
            (VOLUME, "the volume"), (MUTE, "mute"), (MIC_MUTE, "microphone mute"),
            (FREEZE, "freeze"), (PRESENTATION, "presentation status"),
            (SCREENSAVER, "the screensaver state"), (RECORDING, "recording"),
            (STREAMING, "streaming"), (WINDOWS, "the window list"),
        ):
            await self._read(command, label=label)
        if self._cycle == 1 or self._cycle % SETTINGS_EVERY == 0:
            for command, label in (
                (MODEL, "the model"), (BOXNAME, "the device name"), (SERIAL, "the serial number"),
                (FIRMWARE, "the firmware version"), (LAN_MAC, "the MAC address"),
                (LOGIN_LEVEL, "the login level"), (PRESENTATION_MODE, "the presentation mode"),
                (POWER_DOWN_MODE, "the power-down mode"), (AUX_MODE, "the output 2 mode"),
                (MIC_STATUS, "the microphone status"),
            ):
                await self._read(command, label=label)
            for index in (0, 1):
                await self._read_hdmi(index)
        if self._refused:
            # A standing condition: written again on every poll while it holds.
            names = ", ".join(sorted(set(self._refused.values())))
            self.set_state("last_error", f"The Cynap refused to report {names} at this login level.")

    async def _read_hdmi(self, index: int) -> None:
        """HDMI Input Status: the reply does not say which input, so it is
        read here, under the request it answers."""
        data = await self._read(HDMI_INPUT, bytes([index]), label=f"HDMI input {index + 1}")
        if data is None or len(data) < 7:
            return
        present = data[0] == 1
        width = int.from_bytes(data[1:3], "little")
        height = int.from_bytes(data[3:5], "little")
        scan = "p" if data[5] == 1 else "i"
        self.set_state(f"hdmi_{index + 1}_signal", present)
        self.set_state(f"hdmi_{index + 1}_resolution", f"{width}x{height}{scan}" if present else "")

    async def _liveness_probe(self) -> None:
        # Any answer counts, an error included: the question is whether the
        # Cynap is answering, not what it allows this login.
        try:
            await self._request(POWER)
        except WolfProtError:
            return

    # ── Receive ──────────────────────────────────────────────────────────────

    async def on_data_received(self, data: bytes) -> None:
        # Nothing here awaits: each message's handler runs as a task of its
        # own, and one that paused could let the next message in first.
        frame = decode_frame(data)
        if frame is None:
            return
        key = (frame.is_set, frame.command)
        if frame.error is None and not frame.is_set and frame.command != HDMI_INPUT:
            # A reply or a subscription notice: either is the current value.
            self._apply(frame.command, frame.payload)
        for fut in self._waiters.get(key) or []:
            if not fut.done():
                if frame.error is None:
                    fut.set_result(frame.payload)
                else:
                    fut.set_exception(WolfProtError(frame.command, frame.error))
                break

    def _apply(self, command: int, p: bytes) -> None:
        if not p:
            return
        if command == POWER:
            self.set_state("power", "on" if p[0] == 1 else "off")
        elif command == VOLUME:
            self.set_state("volume", p[0])
        elif command == MUTE:
            self.set_state("mute", p[0] == 1)
        elif command == MIC_MUTE:
            self.set_state("mic_mute", p[0] == 1)
        elif command == FREEZE:
            self.set_state("freeze", p[0] == 1)
        elif command == PRESENTATION:
            self.set_state("presentation_active", p[0] == 1)
        elif command == SCREENSAVER:
            self.set_state("screensaver", SCREENSAVER_STATES.get(p[0], "off"))
        elif command == RECORDING:
            self.set_state("recording", RECORDING_STATES.get(p[0], "stopped"))
            if len(p) >= 5:
                seconds = int.from_bytes(p[1:5], "little") // 1000
                if seconds != self.get_state("recording_time"):
                    self.set_state("recording_time", seconds)
        elif command == STREAMING:
            self.set_state("streaming", p[0] == 1)
        elif command == PRESENTATION_MODE:
            self.set_state("presentation_mode", "lecture" if p[0] == 1 else "meeting")
        elif command == POWER_DOWN_MODE:
            name = {v: k for k, v in POWER_DOWN_MODES.items()}.get(p[0])
            if name:
                self.set_state("power_down_mode", name)
        elif command == AUX_MODE:
            name = {v: k for k, v in AUX_MODES.items()}.get(p[0])
            if name:
                self.set_state("aux_mode", name)
        elif command == MIC_STATUS:
            self.set_state("mic_status", MIC_STATES.get(p[0], "disabled"))
        elif command == LOGIN_LEVEL:
            self.set_state("login_level", LEVELS.get(p[0], "none"))
        elif command == MODEL:
            self.set_state("model", _text(p))
        elif command == BOXNAME:
            self.set_state("device_name", _text(p))
        elif command == SERIAL and len(p) >= 4:
            self.set_state("serial_number", str(int.from_bytes(p[:4], "little")))
        elif command == FIRMWARE:
            self.set_state("firmware_version", self._firmware(p))
        elif command == LAN_MAC:
            self.set_state("mac_address", _text(p).upper())
        elif command == WINDOWS:
            self._apply_windows(p)

    @staticmethod
    def _firmware(p: bytes) -> str:
        """Firmware Version is a JSON string: {"cynap": {"version": "V1.78a", ...}}."""
        text = _text(p)
        try:
            doc = json.loads(text)
        except ValueError:
            return text
        if isinstance(doc, dict):
            for value in doc.values():
                if isinstance(value, dict) and value.get("version"):
                    return str(value["version"])
        return text

    def _apply_windows(self, p: bytes) -> None:
        opened = 0
        for index, block in enumerate(windows_blocks(p), start=1):
            if not block or not self.is_child_registered("window", index):
                continue
            content = WINDOW_TYPES.get(block[0], "other")
            if content != "none":
                opened += 1
            self.set_child_state_batch("window", index, {
                "content": content,
                "open": content != "none",
                "fullscreen": len(block) > 1 and block[1] == 1,
                "mute": len(block) > 2 and block[2] == 1,
            })
        self.set_state("windows_open", opened)

    # ── Commands ─────────────────────────────────────────────────────────────

    @staticmethod
    def _window(params: dict[str, Any]) -> int:
        """The window child (1-4) as WolfProt's window number (0-3)."""
        index = int(params.get("window"))
        if not 1 <= index <= 4:
            raise ValueError("Window must be 1 to 4")
        return index - 1

    @staticmethod
    def _choice(table: dict[str, int], value: Any, what: str) -> int:
        code = table.get(str(value))
        if code is None:
            raise ValueError(f"{what} must be one of {', '.join(table)}")
        return code

    async def _start_app(self, output: Any, window_type: int, index: Optional[int], label: str) -> bool:
        outputs = self._choice(OUTPUTS, output, "Output")
        payload = outputs.to_bytes(2, "little") + bytes(8) + b"\xff\x00" + bytes([window_type])
        if index is not None:
            payload += bytes([index])
        return await self._set(START_APP, payload, label, read_back=WINDOWS)

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if command == "power_on":
            return await self._power_on()
        if command == "power_off":
            return await self._set(POWER, b"\x00", "Standby")
        if command == "set_volume":
            level = int(params["level"])
            if not 0 <= level <= 100:
                raise ValueError("Volume must be 0-100")
            return await self._set(VOLUME, bytes([level]), "the volume", read_back=VOLUME)
        for prefix, code, what in (("mute", MUTE, "mute"), ("mic_mute", MIC_MUTE, "microphone mute"),
                                   ("freeze", FREEZE, "freeze")):
            for suffix, value in (("_on", 1), ("_off", 0), ("_toggle", 2)):
                if command == prefix + suffix:
                    return await self._set(code, bytes([value]), what, read_back=code)
        if command == "open_hdmi_input":
            index = {"hdmi_1": 0, "hdmi_2": 1}.get(str(params.get("input")))
            if index is None:
                raise ValueError("Input must be hdmi_1 or hdmi_2")
            return await self._start_app(params.get("output", "main"), 0x02, index, "the HDMI input")
        if command == "open_whiteboard":
            return await self._start_app(params.get("output", "main"), 0x0D, None, "the whiteboard")
        if command in ("window_close", "window_fullscreen", "window_normal"):
            action = WINDOW_ACTIONS[command.removeprefix("window_")]
            return await self._set(WINDOW_CONTROL, bytes([self._window(params), action]),
                                   "the window", read_back=WINDOWS)
        if command in ("window_mute", "window_unmute"):
            on = 1 if command == "window_mute" else 0
            return await self._set(WINDOW_CONTROL, bytes([self._window(params), 0x03, on]),
                                   "the window's mute", read_back=WINDOWS)
        if command == "window_volume":
            level = int(params["level"])
            if not 0 <= level <= 125:
                raise ValueError("Window volume must be 0-125")
            return await self._set(WINDOW_CONTROL, bytes([self._window(params), 0x04, level]),
                                   "the window's volume")
        if command in ("recording_start", "recording_pause_resume", "recording_stop"):
            action = {"recording_start": 0, "recording_pause_resume": 1, "recording_stop": 2}[command]
            return await self._set(RECORDING, bytes([action]), "recording", read_back=RECORDING)
        if command in ("streaming_on", "streaming_off"):
            return await self._set(STREAMING, b"\x01" if command == "streaming_on" else b"\x00",
                                   "streaming", read_back=STREAMING)
        if command == "screensaver_off":
            return await self._set(SCREENSAVER_OFF, b"\x00", "Wake Screen", read_back=SCREENSAVER)
        if command in ("end_presentation", "end_presentation_and_delete"):
            mode = self._choice(END_MODES, params.get("mode", "new_presentation"), "Then")
            delete = 1 if command == "end_presentation_and_delete" else 0
            # Delete recordings, delete snapshots, then mode, delete whiteboards.
            return await self._set(END_PRESENTATION, bytes([delete, delete, mode, delete]),
                                   "End Presentation")
        if command == "remote_key":
            key = self._choice(REMOTE_KEYS, params.get("key"), "Key")
            return await self._set(REMOTE, bytes([key, 0x03]), "the remote key")
        if command == "reboot":
            return await self._set(REBOOT, b"\x01", "Restart Cynap")
        raise ValueError(f"Unknown command: {command}")

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "device_name":
            name = str(value).encode("utf-8")
            if not name or len(name) > 32:
                raise ValueError("The device name is 1 to 32 bytes")
            return await self._set(BOXNAME, name, "the device name", read_back=BOXNAME)
        if key == "presentation_mode":
            mode = self._choice(PRESENTATION_MODES, value, "Presentation mode")
            return await self._set(PRESENTATION_MODE, bytes([mode]), "the presentation mode",
                                   read_back=PRESENTATION_MODE)
        if key == "power_down_mode":
            mode = self._choice(POWER_DOWN_MODES, value, "Power down mode")
            return await self._set(POWER_DOWN_MODE, bytes([mode]), "the power-down mode",
                                   read_back=POWER_DOWN_MODE)
        if key == "aux_mode":
            mode = self._choice(AUX_MODES, value, "Output 2 mode")
            return await self._set(AUX_MODE, bytes([mode]), "the output 2 mode", read_back=AUX_MODE)
        raise ValueError(f"Unknown device setting '{key}'")

    # ── Power On: Wake-on-LAN, then the protocol ─────────────────────────────

    def _wake_mac(self) -> str:
        return (
            str(self.get_state("mac_address") or "").strip()
            or str(self.config.get("mac_address") or "").strip()
        )

    async def _power_on(self) -> bool:
        """Declared ``available_offline``. Standby closes the Cynap's
        connections, so a Cynap in standby is woken with Wake-on-LAN; one
        still connected (inside the 30 seconds after Standby) also gets the
        protocol's Power On."""
        connected = bool(self.transport and self.transport.connected)
        mac = self._wake_mac()
        woke = False
        if mac:
            try:
                await self.wake_on_lan(mac)
                woke = True
            except ValueError as exc:
                if not connected:
                    raise WakeAddressError(
                        f"The Cynap cannot be woken: '{mac}' is not a MAC address "
                        f"(six pairs of hex digits, like 00:0C:92:AA:BB:CC)."
                    ) from exc
        if connected:
            return await self._set(POWER, b"\x01", "Power On", read_back=POWER)
        if not woke:
            raise WakeAddressError(
                "The Cynap is offline and no MAC address is known, so it cannot be "
                "woken. The MAC is read from the Cynap on its first connection; until "
                "then, enter it under Edit Device, or turn the Cynap on at the unit."
            )
        return True
