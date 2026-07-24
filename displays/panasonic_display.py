"""
OpenAVC Panasonic Professional Display Driver.

Controls Panasonic TH-series professional LCD displays (SQ / EQ / CQ /
VF / SF / BQ / LFV / LQ lines) over the LAN command channel on TCP
port 1024 (configurable on the display under ``[Initial settings] ->
[Network settings] -> [LAN settings]``).

Protocol shape — the display speaks ONE of two greeting-selected
protocols, chosen by the ``[LAN control protocol]`` option in the
display's hidden Options service menu (default varies by generation):

  Protocol 1 (exclusive to Panasonic displays):
    1. Open TCP. Display sends ``PDPCONTROL <mode>[ <random8>](CR)``
       — mode 1 = Protected (password set in Web Control), 0 = not.
    2. Protected mode: prefix every command with the 32-char lowercase
       MD5 hex digest of ``"<random8><password>"``.
    3. Command frame: ``[hash](STX)<body>(ETX)(CR)``. Success
       responses are ``(STX)<content>(ETX)(CR)``; a query response
       echoes the query, e.g. ``QAV:050`` / ``QPC:MENVIV``.
    4. Errors come back as bare tokens (``ERR1``..``ERR5`` /
       ``ER401``) or ``PDPCONTROL ERRA`` for a password mismatch.

  Protocol 2 (shared with Panasonic projectors / NTCONTROL):
    1. Greeting is ``NTCONTROL <mode>[ <random8>](CR)``.
    2. Protected mode: prefix is the MD5 digest of
       ``"<username>:<password>:<random8>"``.
    3. Command frame: ``[hash]00<body>(CR)``. Success responses are
       ``00<content>(CR)`` where content does NOT echo the query —
       e.g. the reply to ``QPW`` arrives as ``001`` on the wire
       (``00`` framing + value ``1``). Correlation is positional.
    4. Errors are bare tokens (``ERR1``..``ERR5`` / ``ERRA`` /
       ``ER401``).

The driver auto-detects the protocol from the greeting on every
connection, so it works whichever way the display is set — the
selector lives in a hidden service menu integrators should not have
to find.

Connection model — no Panasonic display holds the control connection
open. Per the LAN Control Protocol document (FAQ Q-4): VF1H, SF2/SF2H,
EQ1, SQ1, VF2/VF2H and BQ1 units disconnect after EVERY command
response; all other models disconnect after 30 seconds of idle, and
connect-per-command is explicitly sanctioned. The driver therefore
opens a short-lived session per command burst (greeting + hash each
time), tolerates the peer closing after any response, and reopens as
needed. There is no persistent transport; reachability is verified on
connect and re-verified by every poll cycle (a failed poll raises, so
the platform watchdog marks the device offline).

Push vs poll: request/response only — the display has no
subscription / notification channel, so polling is the only way to
track power / volume / input changes made at the panel or via IR.
Documented choice.

Why Python (not YAML):
  1. Per-request connection lifecycle — ``ConfigurableDriver`` models
     one persistent TCP transport; it cannot re-run a greeting +
     challenge handshake per command burst or treat peer-close-after-
     response as normal.
  2. Dual-protocol auto-detection — framing (STX/ETX vs ``00``
     prefix) and hash recipe both switch at runtime based on the
     greeting.
  3. The MD5 session-challenge auth itself (in two recipes) is
     outside the declarative ``auth:`` block's ``telnet_login``. Note
     the auth shape alone would NOT convert this driver to YAML —
     reasons 1 and 2 stand regardless.

Models covered:
    The command core (power, input select, volume, audio / video
    mute, aspect, picture adjustments) is uniform across the
    TH-series professional line — the SQ2H and EQ2 command lists are
    byte-identical apart from layout, and the CQ1 list carries the
    same core. Per-series variation is which inputs exist (e.g. CQ1
    adds a TV tuner input, older units have DVI); the driver exposes
    the SQ2H/EQ2 union as the picker and passes through free-typed
    protocol codes for model extras. Unsupported values return
    ``ERR2`` on the display and are logged at debug.

Sources:
    LAN Control Protocol (mechanism, auth, connection behaviour):
      https://docs.connect.panasonic.com/prodisplays/support/download/pdf/LAN_Protocol_exp.pdf
    Communication samples (byte-level, both protocols):
      https://docs.connect.panasonic.com/prodisplays/support/download/pdf/LAN_Command_sequence_exp.pdf
    Command list (SQ2H series, canonical surface; EQ2 identical):
      https://docs.connect.panasonic.com/prodisplays/support/download/pdf/SQ2H_SerialCommandList.pdf
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)


# Input codes from the SQ2H / EQ2 command lists (`IMS:<code>` / `QMI`).
# Other series expose a subset or model extras (CQ1: TV1 tuner; older
# units: DV1); free-typed codes pass through for those.
INPUT_TO_CODE = {
    "hdmi1": "HM1",
    "hdmi2": "HM2",
    "hdmi3": "HM3",
    "usb_c": "UC1",
    "slot": "SL1",
    "pc": "PC1",
    "screen_transfer": "NW1",
    "usb_memory": "UD1",
    "memory_viewer": "MV1",
    "whiteboard": "WB1",
}
CODE_TO_INPUT = {v: k for k, v in INPUT_TO_CODE.items()}
INPUT_VALUES = list(INPUT_TO_CODE)

# Aspect codes (`DAM:<code>` / `QAS`). Display labels per the command
# list: FULL / NORMAL / REAL / H FIT / V FIT / ZOOM1 / ZOOM2.
ASPECT_TO_CODE = {
    "full": "FULL",
    "normal": "NORM",
    "real": "NATV",
    "h_fit": "HFIT",
    "v_fit": "VFIT",
    "zoom1": "ZOOM",
    "zoom2": "ZOM2",
}
CODE_TO_ASPECT = {v: k for k, v in ASPECT_TO_CODE.items()}
ASPECT_VALUES = list(ASPECT_TO_CODE)

# Picture mode codes (`VPC:MEN<code>` / `QPC:MEN`).
PICTURE_MODE_TO_CODE = {
    "vivid": "VIV",
    "natural": "NAT",
    "standard": "STD",
    "surveillance": "SUV",
    "graphic": "GRH",
    "dicom": "DCM",
}
CODE_TO_PICTURE_MODE = {v: k for k, v in PICTURE_MODE_TO_CODE.items()}
PICTURE_MODE_VALUES = list(PICTURE_MODE_TO_CODE)

# Picture adjustment surface: state var -> (setter code, query body).
# All 0-100 with query read-back per the command list.
PICTURE_VARS = {
    "backlight": ("VPC:BLT", "QPC:BLT"),
    "contrast": ("VPC:PIC", "QPC:PIC"),
    "black_level": ("VPC:BLK", "QPC:BLK"),
    "color": ("VPC:COL", "QPC:COL"),
    "tint": ("VPC:TIN", "QPC:TIN"),
    "sharpness": ("VPC:SHP", "QPC:SHP"),
}

_STX = "\x02"
_ETX = "\x03"

# Bare error tokens (both protocols). ``PDPCONTROL ERRA`` is the
# Protocol 1 password-mismatch form and is handled separately.
_ERROR_TOKENS = {"ERR1", "ERR2", "ERR3", "ERR4", "ERR5", "ERRA", "ER401"}

_GREETING_RE = re.compile(
    r"^(?P<proto>NTCONTROL|PDPCONTROL)\s+(?P<mode>[01])"
    r"(?:\s+(?P<random>[0-9A-Fa-f]{8}))?\s*$"
)

# Free-typed protocol codes accepted as pass-through where a picker
# value isn't in the documented union (model extras like CQ1's TV1).
_RAW_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,3}$")

_CONNECT_TIMEOUT = 5.0
_REPLY_TIMEOUT = 5.0


class _AuthRejected(Exception):
    """The display rejected the session hash (ERRA)."""


class _Session:
    """One short-lived control connection: greeting read, protocol
    detected, session hash computed. The display may close it after
    any response (documented behaviour on half the family)."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        protocol: int,
        protected: bool,
        prefix: str,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.protocol = protocol  # 1 = PDPCONTROL, 2 = NTCONTROL
        self.protected = protected
        self.prefix = prefix

    def frame(self, body: str) -> bytes:
        if self.protocol == 1:
            return f"{self.prefix}{_STX}{body}{_ETX}\r".encode("ascii")
        return f"{self.prefix}00{body}\r".encode("ascii")

    async def request(self, body: str) -> str:
        """Send one command and read its one-line response."""
        self.writer.write(self.frame(body))
        await self.writer.drain()
        raw = await asyncio.wait_for(
            self.reader.readuntil(b"\r"), timeout=_REPLY_TIMEOUT
        )
        return raw.decode("ascii", errors="ignore").strip("\r\n")

    def at_eof(self) -> bool:
        return self.reader.at_eof()

    async def close(self) -> None:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass


class PanasonicDisplayDriver(BaseDriver):
    """Panasonic TH-series professional display driver (Protocol 1 /
    Protocol 2, auto-detected)."""

    DRIVER_INFO = {
        "id": "panasonic_display",
        "name": "Panasonic Professional Display",
        "manufacturer": "Panasonic",
        "category": "display",
        "version": "1.0.1",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "author": "OpenAVC",
        "description": (
            "Controls Panasonic TH-series professional displays (SQ / "
            "EQ / CQ / VF / SF / BQ lines) over the LAN command "
            "channel. Power, input, volume, audio / video mute, "
            "aspect, and the picture adjustment surface. Speaks both "
            "of Panasonic's LAN control protocols (auto-detected) and "
            "supports the MD5 challenge authentication, so the "
            "display works whichever way its service menu is set."
        ),
        "source_url": "https://docs.connect.panasonic.com/prodisplays/support/download/pdf/LAN_Protocol_exp.pdf",
        "tags": ["display", "panasonic", "th", "signage", "ntcontrol"],
        "verified": False,
        "simulated": True,
        "protocols": ["ntcontrol", "pdpcontrol"],
        "ports": [1024],
        "transport": "tcp",
        "discovery": {
            # Both protocols are server-speaks-first: the display sends
            # ``PDPCONTROL <mode> [random]`` or ``NTCONTROL <mode>
            # [random]`` the instant a TCP client connects. A
            # connect-only banner read identifies the driver without
            # touching auth. A PDPCONTROL greeting is display-definitive;
            # NTCONTROL is shared with Panasonic projectors, so a
            # projector scan may surface both this driver and
            # panasonic_pt as candidates — the integrator picks.
            "tcp_probe": {
                "port": 1024,
                "expect_regex": r"^(NTCONTROL|PDPCONTROL) [01]",
                "extract_manufacturer": "Panasonic",
            },
            "manufacturer_alias": ["Panasonic"],
        },
        "compatible_models": [
            {
                "manufacturer": "Panasonic",
                "models": [
                    "TH-98/86/75/65/55/50/43SQ2H",
                    "TH-98/86/75/65/55/50/43SQ1H",
                    "TH-98/86/75/65/55/49/43SQ1",
                    "TH-86/75/65/55/50/43SQ3",
                    "TH-86/75/65/55/50/43SQE2",
                    "TH-98/86/75/65/55/49/43SQE1",
                    "TH-86/75/65/55/50/43EQ3",
                    "TH-86/75/65/55/50/43EQ2",
                    "TH-86/75/65/55/49/43EQ1",
                    "TH-86/75/65/55/50/43CQE2",
                    "TH-98/86/75/65/55/50/43CQE1",
                    "TH-86/75/65/55/50/43CQ2",
                    "TH-86/75/65/55/50/43CQ1",
                    "TH-55VF2H / TH-55VF2",
                    "TH-55VF1H",
                    "TH-80/70SF2H / TH-80/70SF2",
                    "TH-86/75/65BQ1",
                    "TH-55LFV9",
                    "TH-98/84LQ70",
                ],
                "confidence": "untested",
                "notes": (
                    "The command core (power, input, volume, mutes, "
                    "aspect, picture) is uniform across the TH-series "
                    "professional line; the SQ2H and EQ2 command "
                    "lists are identical and CQ1 carries the same "
                    "core. Per-series variation is which inputs "
                    "exist — the input picker offers the SQ2H/EQ2 "
                    "union and free-typed protocol codes pass "
                    "through for model extras (e.g. CQ1's TV1 tuner "
                    "input). Unsupported values return ERR2 on the "
                    "display, logged at debug."
                ),
            }
        ],
        "help": {
            "overview": (
                "Panasonic professional displays are controlled over "
                "TCP port 1024 using one of two LAN control "
                "protocols selected in the display's service menu. "
                "This driver auto-detects the protocol from the "
                "connection greeting, so either setting works. "
                "Optional MD5 challenge authentication is tied to "
                "the display's Web Control administrator account. "
                "The display closes the control connection between "
                "commands — this is normal, documented behaviour "
                "the driver expects."
            ),
            "setup": (
                "1. Connect the display to the network and assign a "
                "static IP.\n"
                "2. Enable network control: [Initial settings] -> "
                "[Network settings] -> [Network control] = On. The "
                "default command port is 1024 (shown under [LAN "
                "settings]).\n"
                "3. Open the display's Web Control page in a "
                "browser. Set or note the Administrator-Authorized "
                "password (default username is ``admin1``, default "
                "password is ``panasonic``).\n"
                "4. In OpenAVC, enter the display's IP and the Web "
                "Control admin username and password. If the "
                "display has no password set (non-protected mode), "
                "leave the password field empty."
            ),
        },
        "default_config": {
            "host": "",
            "port": 1024,
            "username": "admin1",
            "password": "",
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 1024,
                "label": "Command Port",
                "description": (
                    "TCP command port. Panasonic default is 1024 — "
                    "shown on the display under [Initial settings] "
                    "-> [Network settings] -> [LAN settings]."
                ),
            },
            "username": {
                "type": "string",
                "default": "admin1",
                "label": "Web Admin Username",
                "description": (
                    "Web Control Administrator username. Panasonic "
                    "factory default is ``admin1``. Only used when "
                    "the display is in Protocol 2 protected mode."
                ),
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "Web Admin Password",
                "secret": True,
                "description": (
                    "Web Control Administrator password. Leave blank "
                    "if the display has no password set "
                    "(non-protected mode)."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "The display has no push notifications, so "
                    "state is polled. Each poll opens a short "
                    "control connection (the display drops idle "
                    "connections by design). Set to 0 to disable "
                    "polling."
                ),
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["off", "on"],
                "label": "Power State",
                "control": True,
            },
            "input": {
                "type": "string",
                "label": "Input",
                "control": True,
            },
            "volume": {
                "type": "integer",
                "label": "Volume",
                "min": 0,
                "max": 100,
                "control": True,
            },
            "audio_mute": {
                "type": "boolean",
                "label": "Audio Mute",
                "control": True,
            },
            "video_mute": {
                "type": "boolean",
                "label": "Video Mute",
                "control": True,
            },
            "aspect": {
                "type": "string",
                "label": "Aspect",
            },
            "picture_mode": {
                "type": "string",
                "label": "Picture Mode",
            },
            "backlight": {
                "type": "integer",
                "label": "Backlight",
                "min": 0,
                "max": 100,
            },
            "contrast": {
                "type": "integer",
                "label": "Contrast",
                "min": 0,
                "max": 100,
            },
            "black_level": {
                "type": "integer",
                "label": "Black Level",
                "min": 0,
                "max": 100,
            },
            "color": {
                "type": "integer",
                "label": "Color",
                "min": 0,
                "max": 100,
            },
            "tint": {
                "type": "integer",
                "label": "Tint",
                "min": 0,
                "max": 100,
            },
            "sharpness": {
                "type": "integer",
                "label": "Sharpness",
                "min": 0,
                "max": 100,
            },
            "lan_protocol": {
                "type": "enum",
                "values": ["protocol1", "protocol2"],
                "label": "LAN Protocol",
            },
            "auth_required": {
                "type": "boolean",
                "label": "Auth Required",
            },
        },
        "commands": {
            "power_on": {"label": "Power On", "params": {}},
            "power_off": {"label": "Power Off", "params": {}},
            "set_input": {
                "label": "Set Input",
                "params": {
                    "input": {
                        "type": "enum",
                        "required": True,
                        "values": INPUT_VALUES,
                    },
                },
                "help": (
                    "Switch the active input. Models expose only a "
                    "subset of these; a raw protocol code (e.g. "
                    "``TV1`` on a CQ1) can be typed for model "
                    "extras. Unsupported values return ERR2 on the "
                    "display."
                ),
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
                "help": "Audio volume, 0-100.",
            },
            "volume_up": {"label": "Volume Up", "params": {}},
            "volume_down": {"label": "Volume Down", "params": {}},
            "audio_mute_on": {"label": "Audio Mute On", "params": {}},
            "audio_mute_off": {"label": "Audio Mute Off", "params": {}},
            "video_mute_on": {"label": "Video Mute On", "params": {}},
            "video_mute_off": {"label": "Video Mute Off", "params": {}},
            "set_aspect": {
                "label": "Set Aspect",
                "params": {
                    "aspect": {
                        "type": "enum",
                        "required": True,
                        "values": ASPECT_VALUES,
                    },
                },
                "help": (
                    "Aspect mode. ``real`` is the display's REAL "
                    "(dot-by-dot) mode."
                ),
            },
            "set_picture_mode": {
                "label": "Set Picture Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": PICTURE_MODE_VALUES,
                    },
                },
            },
            "backlight_set": {
                "label": "Set Backlight",
                "params": {
                    "value": {
                        "type": "integer", "required": True,
                        "min": 0, "max": 100,
                    },
                },
                "help": "Backlight level, 0-100.",
            },
            "contrast_set": {
                "label": "Set Contrast",
                "params": {
                    "value": {
                        "type": "integer", "required": True,
                        "min": 0, "max": 100,
                    },
                },
                "help": "Picture contrast, 0-100.",
            },
            "black_level_set": {
                "label": "Set Black Level",
                "params": {
                    "value": {
                        "type": "integer", "required": True,
                        "min": 0, "max": 100,
                    },
                },
                "help": "Black level brightness, 0-100.",
            },
            "color_set": {
                "label": "Set Color",
                "params": {
                    "value": {
                        "type": "integer", "required": True,
                        "min": 0, "max": 100,
                    },
                },
                "help": "Colour saturation, 0-100.",
            },
            "tint_set": {
                "label": "Set Tint",
                "params": {
                    "value": {
                        "type": "integer", "required": True,
                        "min": 0, "max": 100,
                    },
                },
                "help": "Colour tint / hue, 0-100.",
            },
            "sharpness_set": {
                "label": "Set Sharpness",
                "params": {
                    "value": {
                        "type": "integer", "required": True,
                        "min": 0, "max": 100,
                    },
                },
                "help": "Picture sharpness, 0-100.",
            },
            "raw_command": {
                "label": "Send Raw Command",
                "params": {
                    "command": {
                        "type": "string",
                        "required": True,
                        "help": (
                            "Bare command body without framing or "
                            "terminator, e.g. ``QPW`` or "
                            "``IMS:HM1``. The driver adds the "
                            "protocol framing and auth prefix."
                        ),
                    },
                },
                "help": (
                    "Escape hatch for commands not surfaced as "
                    "named commands (timers, white balance, setup "
                    "options — see the model's command list)."
                ),
            },
            "refresh": {"label": "Refresh Status", "params": {}},
        },
        # Values the display persists and reports back. Input reads
        # back via QMI, picture mode / adjustments via QPC:* (all
        # confirmed in the command list). Power / volume / mutes /
        # aspect are live operational controls, not settings.
        "device_settings": {
            "input": {
                "type": "enum",
                "values": INPUT_VALUES,
                "label": "Input",
                "help": (
                    "Active input. Models expose only a subset; an "
                    "unsupported value returns ERR2 on the display."
                ),
                "state_key": "input",
                "default": "hdmi1",
                "setup": False,
            },
            "picture_mode": {
                "type": "enum",
                "values": PICTURE_MODE_VALUES,
                "label": "Picture Mode",
                "state_key": "picture_mode",
                "default": "standard",
                "setup": False,
            },
            "backlight": {
                "type": "integer", "min": 0, "max": 100,
                "label": "Backlight", "help": "Backlight level, 0-100.",
                "state_key": "backlight", "default": 70, "setup": False,
            },
            "contrast": {
                "type": "integer", "min": 0, "max": 100,
                "label": "Contrast", "help": "Picture contrast, 0-100.",
                "state_key": "contrast", "default": 50, "setup": False,
            },
            "black_level": {
                "type": "integer", "min": 0, "max": 100,
                "label": "Black Level",
                "help": "Black level brightness, 0-100.",
                "state_key": "black_level", "default": 50, "setup": False,
            },
            "color": {
                "type": "integer", "min": 0, "max": 100,
                "label": "Color", "help": "Colour saturation, 0-100.",
                "state_key": "color", "default": 50, "setup": False,
            },
            "tint": {
                "type": "integer", "min": 0, "max": 100,
                "label": "Tint", "help": "Colour tint / hue, 0-100.",
                "state_key": "tint", "default": 50, "setup": False,
            },
            "sharpness": {
                "type": "integer", "min": 0, "max": 100,
                "label": "Sharpness", "help": "Picture sharpness, 0-100.",
                "state_key": "sharpness", "default": 50, "setup": False,
            },
        },
        # Quick Action strip: high-use one-tap controls + a setup
        # wizard that tests (and optionally saves) the Web Control
        # admin credentials out-of-band — useful when the device is
        # offline on a bad password.
        "actions": [
            {"id": "power_on", "kind": "command", "icon": "power"},
            {"id": "power_off", "kind": "command", "icon": "power-off"},
            {"id": "audio_mute_on", "kind": "command", "icon": "volume-x"},
            {"id": "audio_mute_off", "kind": "command", "icon": "volume-2"},
            {
                "id": "test_credentials",
                "kind": "setup",
                "label": "Test Admin Credentials",
                "icon": "key-round",
                "availability": "always",
                "params": {
                    "username": {
                        "type": "string",
                        "default": "admin1",
                        "label": "Web Admin Username",
                    },
                    "password": {
                        "type": "password",
                        "secret": True,
                        "label": "Web Admin Password",
                        "help": (
                            "The Web Control admin password. Leave "
                            "blank for a display in non-protected "
                            "mode."
                        ),
                    },
                    "save": {
                        "type": "boolean",
                        "default": True,
                        "label": "Save these credentials if they work",
                    },
                },
            },
        ],
    }

    # ── Lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        # Session-per-request protocol: every command opens its own TCP
        # connection (greeting + per-session MD5 hash), so there is no
        # persistent platform transport to build. self.transport stays None.
        self.transport = None

    def _link_alive(self) -> bool:
        # No persistent link to test — reachability is proven by every
        # request round-trip, and a dead display fails the next poll.
        return True

    async def _post_connect(self) -> None:
        # One probe round-trip validates reachability, the greeting,
        # and (in protected mode) the credentials — the display
        # answers ERRA to the first command on a bad hash.
        try:
            results = await self._run_requests([("QPW", "power")])
        except _AuthRejected:
            raise ConnectionError(
                f"[{self.device_id}] Authentication failed — check "
                "the Web Control admin username and password"
            ) from None
        self._apply_results(results)

    async def _initial_sync(self) -> None:
        try:
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

    # ── Session handling ──

    async def _open_session(
        self,
        username: str | None = None,
        password: str | None = None,
    ) -> _Session:
        """Open one control connection: TCP connect, read the
        greeting, detect the protocol, compute the session hash."""
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 1024))
        if username is None:
            username = str(self.config.get("username", "admin1") or "admin1")
        if password is None:
            password = str(self.config.get("password", "") or "")

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=_CONNECT_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"[{self.device_id}] Could not reach the display on "
                f"{host}:{port} ({exc})"
            ) from exc

        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r"), timeout=_CONNECT_TIMEOUT
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            writer.close()
            raise ConnectionError(
                f"[{self.device_id}] No control greeting received "
                f"from {host}:{port} — the port is open but the "
                "device is not speaking the Panasonic display "
                "protocol"
            ) from exc

        greeting = raw.decode("ascii", errors="ignore").strip()
        m = _GREETING_RE.match(greeting)
        if not m:
            writer.close()
            raise ConnectionError(
                f"[{self.device_id}] Unrecognized greeting: "
                f"{greeting!r}"
            )

        protocol = 1 if m.group("proto") == "PDPCONTROL" else 2
        protected = m.group("mode") == "1"
        prefix = ""
        if protected:
            random_token = m.group("random") or ""
            if protocol == 1:
                seed = f"{random_token}{password}"
            else:
                seed = f"{username}:{password}:{random_token}"
            prefix = hashlib.md5(
                seed.encode("ascii", errors="ignore")
            ).hexdigest()

        self.set_state(
            "lan_protocol", "protocol1" if protocol == 1 else "protocol2"
        )
        self.set_state("auth_required", protected)
        return _Session(reader, writer, protocol, protected, prefix)

    async def _run_requests(
        self, items: list[tuple[str, str | None]]
    ) -> list[tuple[str, str, str]]:
        """Run command bodies over as few connections as the display
        allows. ``items`` is ``(body, name)`` where name tags query
        results for ``_apply_results`` (None for setters). Returns
        ``(body, name, content)`` for each named item that produced a
        successful response. The peer closing after a response
        (documented on half the family) is handled by reopening.
        """
        results: list[tuple[str, str, str]] = []
        session: _Session | None = None
        try:
            for body, name in items:
                line = None
                for attempt in (0, 1):
                    if session is None:
                        session = await self._open_session()
                    try:
                        line = await session.request(body)
                        break
                    except (
                        OSError,
                        asyncio.TimeoutError,
                        asyncio.IncompleteReadError,
                    ) as exc:
                        await session.close()
                        session = None
                        if attempt == 1:
                            raise ConnectionError(
                                f"[{self.device_id}] Display stopped "
                                f"responding ({exc})"
                            ) from exc
                assert line is not None and session is not None
                content = self._unwrap(session, line)
                if name is not None and content is not None:
                    results.append((body, name, content))
                # A close right after the response is normal — open a
                # fresh session for the next command instead of
                # burning a failed write.
                if session.at_eof():
                    await session.close()
                    session = None
        finally:
            if session is not None:
                await session.close()
        return results

    def _unwrap(self, session: _Session, line: str) -> str | None:
        """Strip protocol framing from a response line and handle
        error tokens. Returns the content, or None when the response
        was a benign error (logged)."""
        if line == "PDPCONTROL ERRA":
            raise _AuthRejected()

        content = line
        if content.startswith(_STX):
            content = content.strip(_STX + _ETX)
        elif session.protocol == 2 and content.startswith("00"):
            content = content[2:]

        if content in _ERROR_TOKENS or line in _ERROR_TOKENS:
            token = content if content in _ERROR_TOKENS else line
            if token == "ERRA":
                raise _AuthRejected()
            if token == "ER401":
                log.warning(
                    f"[{self.device_id}] Display reported a "
                    "processing error (ER401)"
                )
            else:
                # ERR1 undefined / ERR2 out of range / ERR3 busy or
                # unavailable (standby) / ERR4 timeout / ERR5 length —
                # momentary state mismatches, not driver bugs.
                log.debug(f"[{self.device_id}] Display returned {token}")
            return None
        return content

    @staticmethod
    def _query_value(query: str, content: str) -> str:
        """Extract the value from a query response. Protocol 1 echoes
        the query (``QAV:050`` / ``QPC:MENVIV``); Protocol 2 sends the
        value alone, possibly retaining a compound query's sub-key.
        Tolerates all documented shapes."""
        if content.startswith(query):
            rest = content[len(query):]
            return rest[1:] if rest.startswith(":") else rest
        if ":" in query:
            sub = query.split(":", 1)[1]
            if sub and content.startswith(sub):
                return content[len(sub):]
        return content

    # ── State mapping ──

    def _apply_results(
        self, results: list[tuple[str, str, str]]
    ) -> None:
        for body, name, content in results:
            value = self._query_value(body, content)
            if name == "power":
                try:
                    self.set_state("power", "on" if int(value) else "off")
                except ValueError:
                    log.debug(
                        f"[{self.device_id}] Unparseable QPW: {value!r}"
                    )
            elif name == "volume":
                try:
                    self.set_state("volume", int(value))
                except ValueError:
                    log.debug(
                        f"[{self.device_id}] Unparseable QAV: {value!r}"
                    )
            elif name in ("audio_mute", "video_mute"):
                try:
                    self.set_state(name, bool(int(value)))
                except ValueError:
                    log.debug(
                        f"[{self.device_id}] Unparseable {name}: "
                        f"{value!r}"
                    )
            elif name == "input":
                self.set_state(
                    "input", CODE_TO_INPUT.get(value, value.lower())
                )
            elif name == "aspect":
                self.set_state(
                    "aspect", CODE_TO_ASPECT.get(value, value.lower())
                )
            elif name == "picture_mode":
                self.set_state(
                    "picture_mode",
                    CODE_TO_PICTURE_MODE.get(value, value.lower()),
                )
            elif name in PICTURE_VARS:
                try:
                    self.set_state(name, int(value))
                except ValueError:
                    log.debug(
                        f"[{self.device_id}] Unparseable {name}: "
                        f"{value!r}"
                    )
            else:
                log.debug(
                    f"[{self.device_id}] Unmatched result "
                    f"{name!r}={value!r}"
                )

    # ── Polling ──

    async def poll(self) -> None:
        # Power, volume and audio mute answer even in standby (per
        # the command list's standby-availability column).
        results = await self._auth_guarded(
            self._run_requests(
                [("QPW", "power"), ("QAV", "volume"), ("QAM", "audio_mute")]
            )
        )
        self._apply_results(results)

        if self.get_state("power") != "on":
            return

        items: list[tuple[str, str | None]] = [
            ("QMI", "input"),
            ("QVM", "video_mute"),
            ("QAS", "aspect"),
            ("QPC:MEN", "picture_mode"),
        ]
        items += [(query, name) for name, (_set, query) in PICTURE_VARS.items()]
        results = await self._auth_guarded(self._run_requests(items))
        self._apply_results(results)

    async def _auth_guarded(self, coro):
        """Convert a mid-session ERRA (password changed on the
        display) into an auth-worded ConnectionError so the poll
        watchdog surfaces the right offline reason."""
        try:
            return await coro
        except _AuthRejected:
            raise ConnectionError(
                f"[{self.device_id}] Authentication failed — check "
                "the Web Control admin username and password"
            ) from None

    # ── Commands ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}
        items: list[tuple[str, str | None]] | None = None

        if command == "power_on":
            items = [("PON", None), ("QPW", "power")]
        elif command == "power_off":
            items = [("POF", None), ("QPW", "power")]
        elif command == "set_input":
            name = str(params.get("input", "")).strip()
            code = INPUT_TO_CODE.get(name.lower())
            if code is None and _RAW_CODE_RE.match(name):
                code = name.upper()  # model-extra pass-through
            if code is None:
                log.warning(f"[{self.device_id}] Unknown input: {name!r}")
                return
            items = [(f"IMS:{code}", None), ("QMI", "input")]
        elif command == "set_volume":
            items = [
                (f"AVL:{int(params.get('value', 0)):03d}", None),
                ("QAV", "volume"),
            ]
        elif command == "volume_up":
            items = [("AUU", None), ("QAV", "volume")]
        elif command == "volume_down":
            items = [("AUD", None), ("QAV", "volume")]
        elif command == "audio_mute_on":
            items = [("AMT:1", None), ("QAM", "audio_mute")]
        elif command == "audio_mute_off":
            items = [("AMT:0", None), ("QAM", "audio_mute")]
        elif command == "video_mute_on":
            items = [("VMT:1", None), ("QVM", "video_mute")]
        elif command == "video_mute_off":
            items = [("VMT:0", None), ("QVM", "video_mute")]
        elif command == "set_aspect":
            name = str(params.get("aspect", "")).strip().lower()
            code = ASPECT_TO_CODE.get(name)
            if code is None:
                log.warning(f"[{self.device_id}] Unknown aspect: {name!r}")
                return
            items = [(f"DAM:{code}", None), ("QAS", "aspect")]
        elif command == "set_picture_mode":
            name = str(params.get("mode", "")).strip().lower()
            code = PICTURE_MODE_TO_CODE.get(name)
            if code is None:
                log.warning(
                    f"[{self.device_id}] Unknown picture mode: {name!r}"
                )
                return
            items = [(f"VPC:MEN{code}", None), ("QPC:MEN", "picture_mode")]
        elif command in (
            "backlight_set", "contrast_set", "black_level_set",
            "color_set", "tint_set", "sharpness_set",
        ):
            var = command[: -len("_set")]
            setter, query = PICTURE_VARS[var]
            items = [
                (f"{setter}{int(params.get('value', 0)):03d}", None),
                (query, var),
            ]
        elif command == "raw_command":
            body = str(params.get("command", "")).strip()
            if not body:
                return
            results = await self._auth_guarded(
                self._run_requests([(body, "_raw")])
            )
            return results[0][2] if results else None
        elif command == "refresh":
            await self.poll()
            return
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            return

        results = await self._auth_guarded(self._run_requests(items))
        self._apply_results(results)

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting by routing to its transient command.
        Each command issues a follow-up query, so the setting's
        state_key reads back what the display now reports; the
        offline pending queue is platform-run."""
        if key == "input":
            await self.send_command("set_input", {"input": str(value)})
        elif key == "picture_mode":
            await self.send_command("set_picture_mode", {"mode": str(value)})
        elif key in PICTURE_VARS:
            await self.send_command(f"{key}_set", {"value": int(value)})
        else:
            raise ValueError(f"Unknown device setting: {key}")

    # ── Setup wizard: test (and optionally save) the admin credentials ──

    async def run_setup_action(
        self,
        action_id: str,
        params: dict[str, Any],
        progress: Any,
    ) -> dict[str, Any]:
        """Test the Web Control admin credentials over an out-of-band
        connection. Reads the greeting (either protocol), computes the
        matching MD5 session prefix, issues QPW, and reports whether
        the display accepts it. On success, optionally persists the
        credentials and reconnects."""
        if action_id != "test_credentials":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 1024))
        username = str(params.get("username", "admin1") or "admin1")
        password = str(params.get("password", "") or "")
        save = bool(params.get("save", True))
        if not host:
            raise ValueError("No IP address configured")

        await progress(f"Connecting to {host}:{port}…", 20)
        session = await self._open_session(
            username=username, password=password
        )
        protocol = session.protocol
        protected = session.protected

        await progress("Verifying credentials…", 60)
        try:
            line = await session.request("QPW")
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            raise ConnectionError(
                "No response to the test command — check the "
                "command port."
            ) from None
        finally:
            await session.close()

        try:
            auth_ok = self._unwrap(session, line) is not None
            message = "Credentials accepted."
        except _AuthRejected:
            auth_ok = False
            message = "Credentials rejected by the display (ERRA)."
        await progress("Done", 100)

        if not auth_ok:
            raise ConnectionError(message)

        saved = False
        if save:
            await self.request_config_update(
                {"username": username, "password": password}
            )
            saved = True
            await progress("Saved. Reconnecting…", 95)
            await self.request_reconnect()

        return {
            "reachable": True,
            "protocol": f"protocol{protocol}",
            "auth_enabled": protected,
            "auth_ok": auth_ok,
            "saved": saved,
            "message": message,
        }
