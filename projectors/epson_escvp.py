"""
OpenAVC Epson ESC/VP21 Projector Driver.

Controls Epson business and installation projectors over ESC/VP21
(Epson's text command set) wrapped in ESC/VP.net (Epson's TCP transport
on port 3629). Covers Pro L, Pro G, EB-L, and EB-series projectors as
well as a long tail of business projectors that share the same command
surface.

Why Python and not YAML:
    Two pieces of framing don't fit ConfigurableDriver:

    1. Connection handshake. Before any ASCII command works, the client
       sends a fixed 16-byte preamble (``ESC/VP.net\\x10\\x03\\x00\\x00\\x00\\x00``)
       and the projector answers with another 16 bytes
       (``ESC/VP.net\\x10\\x03\\x00\\x00\\x20\\x00`` on success). YAML's
       ``auth: telnet_login`` is a prompt/credential handshake, not a
       binary preamble — and there are no credentials to type, so even
       a hypothetical ``binary_preamble`` type wouldn't be ``auth`` in
       any user sense. It's transport-level negotiation.

    2. Reply framing. Commands end with CR (``\\r``); replies end with
       a colon (``:``) which is the projector's ready prompt. A ``set``
       reply is just ``:`` (a single byte); a ``get`` reply is
       ``KEY=VAL\\r:`` — the value, a CR, then the colon. YAML drivers
       declare one delimiter and parse line-at-a-time; Epson's frame
       boundary is the colon while the value-vs-ack distinction lives
       in whether anything came before it.

    A stateful CallableFrameParser handles both: the first frame is the
    16-byte handshake response, after which the parser flips to
    colon-delimited mode for everything else. If a second device family
    ever shows up needing a binary connection preamble (unlikely — this
    is an Epson-specific quirk), we can lift this into a generic
    transport feature; for now, one driver, one custom parser.

Password and refusals:
    The 16-byte preamble ends in a header count. With a Monitor Password
    configured the driver sends one 18-byte header after it: identifier
    0x01 (Password), attribute 0x01 (plain text), and the password
    NUL-padded to 16 bytes, 34 bytes in all. That is ESC/VP.net 1.0's
    plain-text authentication, which Epson's "Compatible" Command
    Communication mode uses with the Monitor Password; "Protected" mode
    (digest authentication over version 2.0) is not supported. The header
    layout was confirmed against a mature open-source implementation
    tested on Epson hardware, and on an EB-L530U in Compatible mode.

    The reply's status byte (offset 14) says why a connection is refused:
    0x41 a password is required, 0x43 the password is wrong, 0x45 the
    request is not allowed (an EB-L530U answers it to every version 1.0
    and 2.0 request while Command Communication is Protected, and until a
    Web Control Password is set), 0x53 the service is unavailable, 0x55
    the protocol version is not supported. The EB-L530U closes the
    connection straight after 0x41 and 0x45, so the verdict is taken in
    the frame parser, while the reply is being read, and stashed as the
    offline reason before the close is seen; otherwise the close can win
    and the device card says only that the connection dropped. A second
    authentication mode would be a second request builder beside
    handshake_request(); the verdict does not depend on how the request
    was built.

Push vs poll:
    Epson projectors emit unsolicited ``IMEVENT=...\\r:`` messages on
    image / signal events. The IMEVENT format is not formally
    documented, so the driver logs them at debug only and does not
    derive state from them. All state changes are driven by polling
    (PWR? / SOURCE? / MUTE? / FREEZE? / ASPECT? / CMODE? / LAMP?).

Source:
    Epson "ESC/VP21 Command User's Guide for Business Projectors"
    https://files.support.epson.com/pdf/pl600p/pl600pcm.pdf
    Epson "ESC/VP.net" handshake reference (verified against the
    public ``mordae/racket-esc-vp21`` implementation):
    https://github.com/mordae/racket-esc-vp21/blob/master/esc/vp-net.rkt
    Epson "ESC/VP.net Commands" (Compatible mode and the Monitor Password):
    https://files.support.epson.com/docid/cpd6/cpd64101/source/network/concepts/esc_vpnet.html
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.transport.frame_parsers import CallableFrameParser
from openavc.utils.logger import get_logger

log = get_logger(__name__)


# 16-byte ESC/VP.net handshake preamble (sent by client): "ESC/VP.net",
# version 0x10, type 0x03 (connect), two reserved bytes, a status byte and
# the number of 18-byte headers that follow.
HANDSHAKE_BANNER = b"ESC/VP.net"
HANDSHAKE_REQUEST = b"ESC/VP.net\x10\x03\x00\x00\x00\x00"

# Expected projector reply on successful handshake. The byte at offset
# 14 is the status; ``0x20`` means "ok". Any other status is a refusal
# (HANDSHAKE_REFUSALS).
HANDSHAKE_REPLY_OK = b"ESC/VP.net\x10\x03\x00\x00\x20\x00"
HANDSHAKE_LEN = 16
STATUS_OK = 0x20

# One header: identifier, attribute, 16 bytes of value. The password header
# is identifier 0x01 (Password) with attribute 0x01 (plain text).
HEADER_LEN = 18
PASSWORD_HEADER = b"\x01\x01"
PASSWORD_MAX_LEN = 16

PASSWORD_INVALID_MESSAGE = (
    "The Monitor Password entered for this device can't be sent. An Epson "
    "Monitor Password is up to 16 letters and numbers."
)

# What the projector's refusal means, keyed by the reply's status byte: the
# offline reason it is reported under and the sentence on the device card.
# Each says what was refused and what to change; the device page adds the
# edit-and-reconnect step under auth_failed itself.
HANDSHAKE_REFUSALS = {
    0x41: (
        "auth_failed",
        "The projector needs its Monitor Password, and none is entered for "
        "this device.",
    ),
    0x43: (
        "auth_failed",
        "The projector refused the Monitor Password.",
    ),
    0x45: (
        "connection_refused",
        "The projector refuses network control. On the projector, set "
        "Network > Network Settings > Projector Control > Command "
        "Communication to Compatible, choose Set, and create the passwords "
        "it asks for. Then enter the Monitor Password under Edit.",
    ),
    0x53: (
        "connection_refused",
        "The projector is busy, or another controller is connected to it.",
    ),
    0x55: (
        "connection_refused",
        "The projector does not accept this version of its control protocol. "
        "On the projector, set Network > Network Settings > Projector "
        "Control > Command Communication to Compatible.",
    ),
}


def handshake_request(password: str = "") -> bytes:
    """The connect preamble, with a password header when there is a password."""
    if not password:
        return HANDSHAKE_REQUEST
    return (
        HANDSHAKE_REQUEST[:15]
        + b"\x01"
        + PASSWORD_HEADER
        + password.encode("ascii").ljust(PASSWORD_MAX_LEN, b"\x00")
    )


def handshake_verdict(reply: bytes) -> tuple[str, str] | None:
    """None when the reply accepts the connection, else ``(code, message)``."""
    if not reply.startswith(HANDSHAKE_BANNER) or len(reply) < HANDSHAKE_LEN:
        return (
            "no_response",
            "The device answered the ESC/VP.net handshake, but not as an "
            "Epson projector does. Check the address and the port.",
        )
    status = reply[14]
    if status == STATUS_OK:
        return None
    return HANDSHAKE_REFUSALS.get(status) or (
        "connection_refused",
        f"The projector refused the connection (ESC/VP.net status "
        f"0x{status:02X}). Check its network control settings.",
    )


# Mapping from PWR? raw response codes to human-readable state names.
# The codes come from the ESC/VP21 spec table for PWR?; we collapse the
# two "standby" variants (network-on / network-off) into a single
# ``standby`` state because the distinction isn't useful for control.
POWER_STATE_MAP = {
    "00": "standby",
    "01": "on",
    "02": "warming_up",
    "03": "cooling_down",
    "04": "standby",
    "05": "abnormal",
    "09": "av_standby",
}
POWER_STATES = sorted({*POWER_STATE_MAP.values(), "unknown"})

# PWR ON answers its colon only once the lamp has lit: within 40, 70 and
# 100 seconds for the first, second and third ignition attempt (ESC/VP21
# guide, section 5.2 note 1). Polls wait for it that long; the guide asks
# for the colon of one command before the next is sent.
POWER_ON_MAX_S = 100.0

# The most lines kept waiting for a reply. A projector switching inputs
# answers none of the lines that reach it meanwhile (measured on an
# EB-L530U: the commands run, their replies never come), so entries can
# be left behind; a poll sends nine lines.
PENDING_MAX = 32

# Said with the platform's "stopped answering" verdict. In an abnormal
# state (lamp failure, abnormally high temperature) the projector executes
# nothing and returns no colon (ESC/VP21 guide, section 5.3), so silence
# is how a lamp failure shows over the network.
ABNORMAL_STATE_HINT = (
    "An Epson projector stops answering in an abnormal state, such as a "
    "lamp failure or a high temperature: check the projector's indicators."
)

# Every get reply names its command (``PWR=01``), so a value goes to the
# state its reply names, whatever line it answers.
REPLY_KEYS = {
    "PWR": "power",
    "ERR": "error",
    "SOURCE": "source",
    "MUTE": "video_mute",
    "FREEZE": "freeze",
    "ASPECT": "aspect",
    "CMODE": "color_mode",
    "LAMP": "lamp_hours",
    "SNO": "serial_number",
}


# Common Epson source codes. The full list is model-dependent; Computer1,
# Computer2, HDMI1, HDMI2, USB, LAN and Screen Mirroring follow the
# command lists in Epson's current Specifications (EB-L735U series,
# EB-992F series), which agree on them, and were measured on an
# EB-L530U. Users can pick a code outside this list with the raw_command
# escape hatch.
SOURCE_CODES = {
    "Computer1": "10",
    "Computer2": "20",
    "DVI": "A0",
    "HDMI1": "30",
    "HDMI2": "A0",
    "HDBaseT": "80",
    "Component": "14",
    "S-Video": "42",
    "Video": "41",
    "USB-A": "52",
    "LAN": "53",
    "Screen Mirroring": "56",
    "Screen Mirroring 2": "59",
    "Wireless LAN": "57",
}

# Aspect codes (the union across models; each model takes a subset and
# answers ERR to the rest).
ASPECT_CODES = {
    "Normal": "00",
    "4:3": "10",
    "16:9": "20",
    "Auto": "30",
    "Full": "40",
    "Zoom": "50",
    "Native": "60",
    "V-Zoom": "A0",
}
ASPECT_AUTO = "30"

# Color modes (the union across models, as for aspect).
COLOR_MODES = {
    "sRGB": "01",
    "Presentation": "04",
    "Theatre": "05",
    "Dynamic": "06",
    "Sports": "08",
    "DICOM SIM": "0F",
    "Custom": "10",
    "Black Board": "11",
    "White Board": "12",
    "Photo": "14",
    "Cinema": "15",
    "Multi-Projection": "1A",
}

# Reverse maps (hex code -> friendly name) for the device-settings read-back.
# The projector reports ASPECT / CMODE as a hex code; the settings editor shows
# the friendly name, so poll() decodes the hex into the *_name state vars the
# settings point at. An unrecognised (model-specific) code falls back to the
# raw hex so the field is never blank.
ASPECT_NAMES = {code: name for name, code in ASPECT_CODES.items()}
COLOR_MODE_NAMES = {code: name for name, code in COLOR_MODES.items()}

# KEY codes: the remote-control and control-panel key codes from the
# command list in Epson's EB-L735U series Specifications, each measured
# on an EB-L530U. The arrows and Enter are control-panel codes, the rest
# remote-control codes. Older models may number some keys differently.
# Power is the power_on / power_off commands, not a key: in standby the
# projector refuses key presses, and the remote's Off key asks for a
# second press.
KEY_CODES = {
    "Menu": "3C",
    "Up": "35",
    "Down": "36",
    "Left": "37",
    "Right": "38",
    "Enter": "16",
    "Esc": "3D",
    "Source Search": "67",
    "Mute": "3E",
    "Freeze": "47",
    "Aspect": "20",
    "Color Mode": "3F",
    "Volume Up": "56",
    "Volume Down": "57",
    "Auto": "4A",
}


class EpsonEscVpDriver(BaseDriver):
    """Epson ESC/VP21 over ESC/VP.net (TCP 3629)."""

    DRIVER_INFO = {
        "id": "epson_escvp",
        "name": "Epson Projector (ESC/VP21)",
        "manufacturer": "Epson",
        "category": "projector",
        "version": "1.5.0",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls Epson business and installation projectors over "
            "ESC/VP21 wrapped in ESC/VP.net on TCP port 3629. Power, "
            "input, A/V mute, freeze, aspect ratio, color mode, lamp "
            "hours, and remote-key passthrough."
        ),
        "source_url": "https://files.support.epson.com/pdf/pl600p/pl600pcm.pdf",
        "tags": ["projector", "epson", "esc-vp21", "esc-vp-net", "pro-l", "eb"],
        "verified": False,
        "simulated": True,
        "protocols": ["esc_vp_net"],
        "ports": [3629],
        "transport": "tcp",
        "discovery": {
            # Active fingerprint: send the 16-byte ESC/VP.net handshake preamble
            # to TCP 3629 and expect a reply that starts with the "ESC/VP.net"
            # banner (offset 0-9). Only the status byte at offset 14 differs
            # between an ok reply and a password-required one, so the banner
            # prefix identifies the device regardless of whether ESC/VP.net auth
            # is set. Epson projectors also speak PJLink Class 2 (claimed by
            # pjlink_class1); the manufacturer-alias hint surfaces this
            # brand-specific driver as a candidate when a PJLink scan reports
            # "EPSON" (or the legacy "Seiko Epson").
            "tcp_probe": {
                "port": 3629,
                "send_hex": "4553432f56502e6e6574100300000000",
                "expect_hex": "4553432f56502e6e6574",
                "extract_manufacturer": "Epson",
            },
            "manufacturer_alias": ["EPSON", "Seiko Epson"],
        },
        "compatible_models": [
            {
                "manufacturer": "Epson",
                "models": ["EB-L530U"],
                "confidence": "full",
                "notes": (
                    "Tested on the projector with Command Communication set "
                    "to Compatible. It takes the Auto, Full, Zoom, Native and "
                    "V-Zoom aspect ratios; the sRGB, Presentation, Dynamic, "
                    "DICOM SIM, Cinema and Multi-Projection color modes; and "
                    "the Computer1, HDMI1, HDMI2, HDBaseT, USB-A, LAN and "
                    "Screen Mirroring inputs. Freeze needs a picture on the "
                    "input. Lamp Hours reads 0 and Error Status stays empty "
                    "on this laser model."
                ),
            },
            {
                "manufacturer": "Epson",
                "models": [
                    "Pro L1100U / L1200U / L1300U / L1405U / L1490U / L1505U / L1715S",
                    "Pro L25000U / L30000U / L30002U",
                    "EB-L Series (EB-L200X, EB-L520W, EB-L630U, EB-L1075U, EB-L1100U)",
                    "EB-Pro Series (EB-G7905U, EB-G7000W)",
                    "Pro G6800NL / G7905U / G7000W",
                    "Home Cinema 5050UB / 6050UB / LS12000",
                    "PowerLite L Series",
                    "PowerLite Pro Z Series (Z8000WU, Z8050W)",
                ],
                "confidence": "untested",
                "notes": (
                    "ESC/VP21 is shared across Epson's business and "
                    "installation projector lines. Per-model variation "
                    "is in which input codes the projector actually "
                    "accepts (e.g. older units have no HDBaseT input). "
                    "The driver lets the user pick from the union of "
                    "all common codes; unsupported codes return ERR "
                    "on the projector, which the driver logs at debug."
                ),
            },
        ],
        "help": {
            "overview": (
                "ESC/VP21 is Epson's text-based projector control "
                "protocol. Over the network it runs inside ESC/VP.net "
                "on TCP port 3629, and the driver handles its binary "
                "handshake. Most modern Epson projectors respond to "
                "PJLink as well, but the native ESC/VP21 surface gives "
                "access to color mode, aspect ratio, freeze, "
                "image-quality numerics, and the full virtual remote "
                "keypad, none of which PJLink covers. Recent Epson "
                "firmware keeps network control off until passwords are "
                "set on the projector. The steps appear when you add the "
                "device."
            ),
            "setup": (
                "1. On the projector, open Network > Network Settings > "
                "Projector Control and set Command Communication to "
                "Compatible (on some models it is under Network > "
                "Network Configuration > Others). Create the Monitor "
                "Password it asks for.\n"
                "2. Go back to Network Settings and choose Set. Create "
                "the Web Control Password too if it asks for one: "
                "network control stays off until one exists.\n"
                "3. Enter the Monitor Password below.\n"
                "Projectors on older firmware with no Command "
                "Communication setting need no password: leave it blank. "
                "Command Communication set to Protected is not supported."
            ),
            "connection": (
                "If the projector goes offline only while it is off, set "
                "Standby Mode to Communication On (Network Standby on "
                "some models) on the projector."
            ),
        },
        "default_config": {
            "host": "",
            "port": 3629,
            "poll_interval": 30,
            "inter_command_delay": 0.1,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 3629,
                "label": "TCP Port",
                "description": "ESC/VP.net port. Epson default is 3629.",
            },
            "password": {
                "type": "string",
                "label": "Monitor Password",
                "secret": True,
                # description is read under the field in Add / Edit Device,
                # help by the Device Audit's connection step.
                "description": (
                    "The projector's Monitor Password, under Network > "
                    "Network Settings > Projector Control. Leave blank if "
                    "the projector has none."
                ),
                "help": (
                    "The projector's Monitor Password, under Network > "
                    "Network Settings > Projector Control. Leave blank if "
                    "the projector has none."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "ESC/VP21 has no formal subscription mechanism; "
                    "polling tracks power transitions and source / "
                    "mute / freeze state. Set to 0 to disable."
                ),
            },
            "inter_command_delay": {
                "type": "number",
                "default": 0.1,
                "min": 0,
                "label": "Inter-command Delay (sec)",
                "description": (
                    "Brief safety delay between sends. The protocol "
                    "is request/response so this is just a guard "
                    "against bursting commands into a slow projector."
                ),
            },
        },
        "state_variables": {
            "power_state": {
                "type": "enum",
                "values": POWER_STATES,
                "label": "Power State",
            },
            "source": {
                "type": "string",
                "label": "Source (hex code)",
            },
            "video_mute": {
                "type": "boolean",
                "label": "A/V Mute",
            },
            "freeze": {
                "type": "boolean",
                "label": "Freeze",
            },
            "aspect": {
                "type": "string",
                "label": "Aspect (hex code)",
            },
            "aspect_name": {
                "type": "string",
                "label": "Aspect Ratio",
                "help": "Aspect ratio decoded from the ASPECT hex code.",
            },
            "color_mode": {
                "type": "string",
                "label": "Color Mode (hex code)",
            },
            "color_mode_name": {
                "type": "string",
                "label": "Color Mode",
                "help": "Color mode decoded from the CMODE hex code.",
            },
            "lamp_hours": {
                "type": "integer",
                "label": "Lamp Hours",
            },
            "error_status": {
                "type": "string",
                "label": "Error Status (hex code)",
            },
            "serial_number": {
                "type": "string",
                "label": "Serial Number",
            },
        },
        "commands": {
            "power_on": {"label": "Power On", "params": {}},
            "power_off": {"label": "Power Off", "params": {}},
            "set_source": {
                "label": "Set Source",
                "params": {
                    "source": {
                        "type": "enum",
                        "required": True,
                        "values": list(SOURCE_CODES.keys()),
                        "label": "Source",
                    },
                },
                "help": (
                    "Select an input. The driver maps the friendly "
                    "name to Epson's two-digit hex source code. Models "
                    "expose only a subset; unsupported codes return "
                    "ERR (logged at debug)."
                ),
            },
            "video_mute_on": {
                "label": "A/V Mute On",
                "params": {},
                "help": "Blank the picture (and audio on models that route audio).",
            },
            "video_mute_off": {
                "label": "A/V Mute Off",
                "params": {},
            },
            "freeze_on": {"label": "Freeze On", "params": {}},
            "freeze_off": {"label": "Freeze Off", "params": {}},
            "set_aspect": {
                "label": "Set Aspect Ratio",
                "params": {
                    "aspect": {
                        "type": "enum",
                        "required": True,
                        "values": list(ASPECT_CODES.keys()),
                    },
                },
            },
            "set_color_mode": {
                "label": "Set Color Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": list(COLOR_MODES.keys()),
                    },
                },
            },
            "send_key": {
                "label": "Send Remote Key",
                "params": {
                    "key": {
                        "type": "enum",
                        "required": True,
                        "values": list(KEY_CODES.keys()),
                    },
                },
                "help": (
                    "Emulate a press of a remote-control key. Use "
                    "for menu navigation and functions without a "
                    "dedicated ESC/VP21 command."
                ),
            },
            "raw_command": {
                "label": "Send Raw ESC/VP21 Command",
                "params": {
                    "command": {
                        "type": "string",
                        "required": True,
                        "label": "Command",
                        "help": (
                            "Full ESC/VP21 line without CR, e.g. "
                            "``BRIGHT INC`` or ``ASPECT?``."
                        ),
                    },
                },
                "help": "Escape hatch for commands or values not in the dropdowns.",
            },
            "refresh": {"label": "Refresh Status", "params": {}},
        },
        # Aspect and color mode are values the projector persists and reports
        # back (ASPECT? / CMODE? are the read-back — decoded into the *_name
        # state vars the settings point at), so each gets the editable-field +
        # offline-pending-queue treatment on top of the transient set_* command.
        # set_device_setting routes to that command so the hex mapping lives in
        # one place; the command stays for macro use (dual surface).
        #
        # Brightness / contrast are deliberately NOT device settings: ESC/VP21
        # has BRIGHT / CONTRAST setters but no query verb (no BRIGHT? / CONTRAST?
        # in the command reference), so there is no read-back to gate on — a
        # setting with no read-back would show a stale value. They remain
        # reachable through raw_command.
        "device_settings": {
            "aspect": {
                "type": "enum",
                "values": list(ASPECT_CODES.keys()),
                "label": "Aspect Ratio",
                "help": (
                    "Image aspect ratio. Models expose only a subset; an "
                    "unsupported value returns ERR on the projector."
                ),
                "state_key": "aspect_name",
                "default": "Normal",
                "setup": False,
            },
            "color_mode": {
                "type": "enum",
                "values": list(COLOR_MODES.keys()),
                "label": "Color Mode",
                "help": (
                    "Picture colour preset. Per-model availability varies; an "
                    "unsupported value returns ERR on the projector."
                ),
                "state_key": "color_mode_name",
                "default": "Presentation",
                "setup": False,
            },
        },
        # High-use one-tap controls.
        "quick_actions": [
            "power_on", "power_off", "video_mute_on", "video_mute_off",
        ],
    }

    # ── Construction ──

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state,
        events,
    ):
        # Handshake state for the framing function. ``False`` means the
        # parser is still waiting on the 16-byte handshake reply from the
        # projector; ``True`` means it has flipped to colon-delimited
        # mode for ESC/VP21 traffic.
        self._handshake_complete = False
        self._handshake_event = asyncio.Event()
        # The projector's refusal as (offline reason, message), set by the
        # frame parser as the reply is read; None while accepted or waiting.
        self._handshake_fault: tuple[str, str] | None = None
        # One entry per ESC/VP21 line sent, oldest first: the line itself.
        # Every line is answered by exactly one colon-terminated reply (a
        # bare colon, ``KEY=VAL``, or ``ERR``), so each reply pops one.
        self._pending: list[str] = []
        # When PWR ON went out, until its colon arrives (or POWER_ON_MAX_S).
        self._power_on_sent_at: float | None = None
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        # A password the 16-byte header cannot hold is refused before the
        # projector is dialed: sending a cut-down one could only fail.
        password = self._password()
        if len(password) > PASSWORD_MAX_LEN or not password.isascii():
            raise ConnectionFaultError(
                PASSWORD_INVALID_MESSAGE, code="invalid_config"
            )
        # Arm the handshake state before the transport exists — the framing
        # function starts in 16-byte handshake mode and flips to
        # colon-delimited ESC/VP21 mode once the reply lands.
        self._handshake_complete = False
        self._handshake_event.clear()
        self._handshake_fault = None
        self._pending.clear()
        self._power_on_sent_at = None

    def _password(self) -> str:
        return str(self.config.get("password") or "")

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # Raw byte stream — framing is handled by the handshake-aware
        # callable parser, not a line delimiter.
        kwargs["delimiter"] = None
        return kwargs

    def _create_frame_parser(self):
        return CallableFrameParser(self._parse_frame)

    async def _post_connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 3629))

        # Send the handshake immediately and wait for the reply.
        await self.transport.send(handshake_request(self._password()))
        try:
            await asyncio.wait_for(self._handshake_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            raise ConnectionFaultError(
                f"The Epson projector at {host}:{port} is not responding to "
                f"the ESC/VP.net handshake (no reply within 5s).",
                code="no_response",
            )

        if self._handshake_fault is not None:
            # The projector refused; the frame parser already stashed the
            # same fault for the case where its close is seen first.
            code, message = self._handshake_fault
            raise ConnectionFaultError(message, code=code)

    async def _initial_sync(self) -> None:
        # Initial sweep so the UI populates. Issue PWR? first and let
        # the response settle before the rest of the poll fans out —
        # the conditional sub-queries in poll() depend on power_state.
        try:
            await self._send_query("power", "PWR?")
            await asyncio.sleep(0.1)
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

    async def _close_session(self) -> None:
        # Runs on every teardown path: disarm the handshake state machine and
        # drop queued queries so a reconnect starts a fresh handshake.
        self._handshake_complete = False
        self._handshake_event.clear()
        self._handshake_fault = None
        self._pending.clear()
        self._power_on_sent_at = None

    # ── Frame parsing ──

    def _parse_frame(self, buffer: bytes) -> tuple[bytes | None, bytes]:
        """Stateful framer: 16-byte preamble first, then colon-delimited.

        The flag flip happens *inside* parse so the next iteration of
        ``CallableFrameParser.feed``'s loop sees the new state — if we
        only flipped it from ``on_data_received`` we'd extract a second
        bogus 16-byte "handshake" from any ESC/VP21 traffic that arrived
        in the same recv as the handshake reply.

        The verdict is taken here too, for the same reason of ordering: a
        projector that refuses closes the connection right after its reply,
        and the transport reports that close before an ``on_data_received``
        task for the reply has run. Stashing the refusal now makes it the
        offline reason whichever the connect sees first.
        """
        if not self._handshake_complete:
            if len(buffer) < HANDSHAKE_LEN:
                return None, buffer
            size = HANDSHAKE_LEN
            if buffer.startswith(HANDSHAKE_BANNER):
                # Any headers the reply carries belong to it, not to the
                # ESC/VP21 stream after it.
                size += HEADER_LEN * buffer[15]
                if len(buffer) < size:
                    return None, buffer
            self._handshake_complete = True
            reply = buffer[:size]
            self._handshake_fault = handshake_verdict(reply)
            if self._handshake_fault is not None:
                self._stash_fault(*self._handshake_fault)
            return reply, buffer[size:]

        # Post-handshake: every reply ends at a colon. Empty frame =
        # bare set ack; non-empty frame may have a trailing CR which
        # we strip in the dispatcher.
        idx = buffer.find(b":")
        if idx < 0:
            return None, buffer
        return buffer[:idx], buffer[idx + 1 :]

    # ── Sending ──

    async def _send_line(self, line: str) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        # ESC/VP21 commands are CR-terminated.
        await self.transport.send((line + "\r").encode("ascii"))

    async def _send_tracked(self, line: str) -> None:
        # Queued before it is sent, so its reply always finds it.
        if len(self._pending) >= PENDING_MAX:
            del self._pending[0]
        self._pending.append(line)
        await self._send_line(line)

    async def _send_query(self, key: str, command: str) -> None:
        # ``key`` names the state the reply feeds; the reply names it too
        # (REPLY_KEYS), which is what routes it.
        await self._send_tracked(command)

    def _power_on_pending(self) -> bool:
        """True while a PWR ON waits for its colon, up to POWER_ON_MAX_S."""
        sent = self._power_on_sent_at
        if sent is None:
            return False
        if asyncio.get_running_loop().time() - sent < POWER_ON_MAX_S:
            return True
        self._power_on_sent_at = None
        return False

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "power_on":
            # Its colon comes once the lamp has lit; the power read-back
            # goes out then (on_data_received), and polls hold until it.
            # Nothing else goes out meanwhile, so the next reply is its own:
            # lines whose replies never came are dropped first, or one of
            # them would take PWR ON's colon and polls would hold for
            # POWER_ON_MAX_S with the projector already warming up.
            self._pending.clear()
            self._power_on_sent_at = asyncio.get_running_loop().time()
            await self._send_tracked("PWR ON")
        elif command == "power_off":
            await self._send_tracked("PWR OFF")
            await asyncio.sleep(0.05)
            await self._send_query("power", "PWR?")
        elif command == "set_source":
            name = str(params.get("source", "")).strip()
            code = SOURCE_CODES.get(name)
            if not code:
                log.warning(f"[{self.device_id}] Unknown source name: {name}")
                return
            await self._send_tracked(f"SOURCE {code}")
            await asyncio.sleep(0.05)
            await self._send_query("source", "SOURCE?")
        elif command == "video_mute_on":
            await self._send_tracked("MUTE ON")
            await self._send_query("video_mute", "MUTE?")
        elif command == "video_mute_off":
            await self._send_tracked("MUTE OFF")
            await self._send_query("video_mute", "MUTE?")
        elif command == "freeze_on":
            await self._send_tracked("FREEZE ON")
            await self._send_query("freeze", "FREEZE?")
        elif command == "freeze_off":
            await self._send_tracked("FREEZE OFF")
            await self._send_query("freeze", "FREEZE?")
        elif command == "set_aspect":
            name = str(params.get("aspect", "")).strip()
            code = ASPECT_CODES.get(name)
            if not code:
                log.warning(f"[{self.device_id}] Unknown aspect name: {name}")
                return
            await self._send_tracked(f"ASPECT {code}")
            await self._send_query("aspect", "ASPECT?")
        elif command == "set_color_mode":
            name = str(params.get("mode", "")).strip()
            code = COLOR_MODES.get(name)
            if not code:
                log.warning(f"[{self.device_id}] Unknown color mode: {name}")
                return
            await self._send_tracked(f"CMODE {code}")
            await self._send_query("color_mode", "CMODE?")
        elif command == "send_key":
            name = str(params.get("key", "")).strip()
            code = KEY_CODES.get(name)
            if not code:
                log.warning(f"[{self.device_id}] Unknown key name: {name}")
                return
            await self._send_tracked(f"KEY {code}")
        elif command == "raw_command":
            line = str(params.get("command", "")).strip()
            if line:
                await self._send_tracked(line)
        elif command == "refresh":
            await self.poll()
            return
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting by routing to its transient set_* command.

        The command maps the friendly name to the ESC/VP21 hex code, sends the
        setter, and queries it back; poll() decodes the returned hex into the
        *_name state var the setting reads from. The offline pending queue is
        handled by the platform.
        """
        if key == "aspect":
            await self.send_command("set_aspect", {"aspect": str(value)})
        elif key == "color_mode":
            await self.send_command("set_color_mode", {"mode": str(value)})
        else:
            raise ValueError(f"Unknown device setting: {key}")

    # ── Polling ──

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return
        if self._power_on_pending():
            # The guide asks for the colon of one command before the next.
            # A poll that sends nothing is not counted as a silent one.
            log.debug(f"[{self.device_id}] Waiting for PWR ON to complete")
            return
        try:
            # PWR? and ERR? always work, even in standby. The rest
            # only return useful values when the projector is on; the
            # projector returns ERR for unavailable getters which we
            # log at debug.
            await self._send_query("power", "PWR?")
            await self._send_query("error", "ERR?")
            power = self.get_state("power_state")
            if power == "on":
                await self._send_query("source", "SOURCE?")
                await self._send_query("video_mute", "MUTE?")
                await self._send_query("freeze", "FREEZE?")
                await self._send_query("aspect", "ASPECT?")
                await self._send_query("color_mode", "CMODE?")
                await self._send_query("lamp_hours", "LAMP?")
                # Serial number rarely changes; query once after power-on.
                if not self.get_state("serial_number"):
                    await self._send_query("serial_number", "SNO?")
        except ConnectionError:
            # Let it out: the platform counts a poll that fails toward
            # taking the device offline.
            log.warning(f"[{self.device_id}] Poll failed — not connected")
            raise

    def _silence_fault(self, cycles: int) -> Any:
        fault = super()._silence_fault(cycles)
        counts = self._io_counts()
        if counts is not None and counts[2] > 0:
            # It answered on this connection and then stopped.
            return dataclasses.replace(
                fault, message=f"{fault.message} {ABNORMAL_STATE_HINT}"
            )
        return fault

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        # Handshake reply: arrives as the first frame, already judged by the
        # frame parser.
        if not self._handshake_event.is_set():
            if self._handshake_fault is not None:
                log.warning(
                    f"[{self.device_id}] ESC/VP.net handshake refused: "
                    f"{data.hex()} ({self._handshake_fault[0]})"
                )
            else:
                log.info(f"[{self.device_id}] ESC/VP.net handshake accepted")
            self._handshake_event.set()
            return

        # Post-handshake frame. Strip the trailing CR if present (queries
        # respond ``KEY=VAL\r:``; the parser hands us ``KEY=VAL\r``).
        line = data.rstrip(b"\r").decode("ascii", errors="ignore").strip()

        # Async push event (signal change etc). Format isn't formally
        # documented; it answers no line, so it pops nothing.
        if line.startswith("IMEVENT="):
            log.debug(f"[{self.device_id}] IMEVENT: {line}")
            return

        # Every other reply answers the oldest line still waiting: a bare
        # colon (a set done), ``KEY=VALUE`` (a get) or ``ERR``.
        sent = self._pending.pop(0) if self._pending else None
        if sent == "PWR ON":
            self._power_on_sent_at = None
            if line == "":
                # The lamp is lit: read the power state back now.
                task = asyncio.ensure_future(self._read_power_after_power_on())
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
        if not line:
            return
        if line == "ERR":
            # Some queries legally return ERR (SOURCE? in standby).
            log.debug(f"[{self.device_id}] ESC/VP21 ERR for {sent!r}")
            return

        self._dispatch_response(line)

    async def _read_power_after_power_on(self) -> None:
        try:
            await self._send_query("power", "PWR?")
        except ConnectionError:
            pass

    def _dispatch_response(self, line: str) -> None:
        # Successful query responses are ``KEY=VALUE``, and the key says
        # which query it answers.
        if "=" not in line:
            log.debug(f"[{self.device_id}] Unparseable response: {line!r}")
            return
        reply_key, value = line.split("=", 1)
        value = value.strip()
        pending = REPLY_KEYS.get(reply_key.strip().upper())

        if pending == "power":
            self.set_state(
                "power_state", POWER_STATE_MAP.get(value, "unknown")
            )
        elif pending == "source":
            self.set_state("source", value)
        elif pending == "video_mute":
            self.set_state("video_mute", value.upper() == "ON")
        elif pending == "freeze":
            self.set_state("freeze", value.upper() == "ON")
        elif pending == "aspect":
            # With Auto selected the projector reports two codes, the aspect
            # it is applying and a fixed 30 (``ASPECT=00 30``); the setting
            # is Auto.
            parts = value.split()
            code = ASPECT_AUTO if parts[1:] == [ASPECT_AUTO] else (parts[0] if parts else value)
            self.set_state("aspect", code)
            self.set_state("aspect_name", ASPECT_NAMES.get(code, code))
        elif pending == "color_mode":
            self.set_state("color_mode", value)
            self.set_state(
                "color_mode_name", COLOR_MODE_NAMES.get(value, value)
            )
        elif pending == "lamp_hours":
            try:
                self.set_state("lamp_hours", int(value))
            except ValueError:
                pass
        elif pending == "error":
            self.set_state("error_status", value)
        elif pending == "serial_number":
            self.set_state("serial_number", value)
        else:
            log.debug(
                f"[{self.device_id}] Unmatched response: {line!r} "
                f"(pending={pending!r})"
            )

