"""
Panasonic Professional Display — LAN control simulator.

Implements the display side of Panasonic's LAN command channel on TCP
1024, in either of the two greeting-selected protocols:

  - ``lan_protocol`` config "2" (default): NTCONTROL framing —
    greeting ``NTCONTROL <mode>[ <random8>]``, commands
    ``[hash]00<body>(CR)``, responses ``00<content>(CR)`` where a
    query's content is the bare value (no query echo).
  - ``lan_protocol`` config "1": PDPCONTROL framing — greeting
    ``PDPCONTROL <mode>[ <random8>]``, commands
    ``[hash](STX)<body>(ETX)(CR)``, responses
    ``(STX)<content>(ETX)(CR)`` where a query's content echoes the
    query (``QAV:050`` / ``QPC:MENVIV``).

Protected mode engages when ``config["password"]`` is set: the
greeting carries a random challenge and every command must lead with
the 32-char MD5 hex prefix — ``md5(random + password)`` for Protocol
1, ``md5(username + ":" + password + ":" + random)`` for Protocol 2.
A mismatch returns the protocol's password-mismatch form
(``PDPCONTROL ERRA`` / ``ERRA``).

Command surface mirrors the SQ2H / EQ2 command list: power (PON /
POF / QPW), input (IMS:* / QMI), volume (AVL / AUU / AUD / QAV),
audio and video mute (AMT / QAM, VMT / QVM), aspect (DAM / QAS),
picture mode + adjustments (VPC:* / QPC:*). Standby availability
matches the list: QPW / QAV / QAM answer in standby; other queries
and all setters except PON return ``ERR3`` (matches the real
device's "busy / unavailable period" behaviour).

Connection model note: real displays close the control connection
between commands (immediately after a response on VF1H / SF2 / EQ1 /
SQ1 / VF2 / BQ1 units, after 30 s idle on the rest). The simulator
keeps the connection open like the 30-s-idle class — the driver's
close-after-response handling is exercised by its test suite, which
wraps this simulator's logic in a server that closes per response.

Driver side: ``displays/panasonic_display.py``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

_STX = "\x02"
_ETX = "\x03"

VALID_INPUT_CODES = {
    "HM1", "HM2", "HM3", "UC1", "SL1",
    "PC1", "NW1", "UD1", "MV1", "WB1",
}

VALID_ASPECT_CODES = {
    "FULL", "NORM", "NATV", "HFIT", "VFIT", "ZOOM", "ZOM2",
}

VALID_PICTURE_MODES = {"VIV", "NAT", "STD", "SUV", "GRH", "DCM"}

# Picture adjustments: sub-key -> state key (all 0-100).
_PICTURE_KEYS = {
    "BLT": "backlight",
    "PIC": "contrast",
    "BLK": "black_level",
    "COL": "color",
    "TIN": "tint",
    "SHP": "sharpness",
}


class PanasonicDisplaySimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "panasonic_display",
        "name": "Panasonic Professional Display Simulator",
        "category": "display",
        "transport": "tcp",
        "default_port": 1024,
        "delimiter": "\r",
        "initial_state": {
            "power": "off",
            "input": "HM1",
            "volume": 20,
            "audio_mute": False,
            "video_mute": False,
            "aspect": "FULL",
            "picture_mode": "STD",
            "backlight": 70,
            "contrast": 50,
            "black_level": 50,
            "color": 50,
            "tint": 50,
            "sharpness": 50,
        },
        "controls": [
            {
                "type": "select",
                "key": "power",
                "options": ["off", "on"],
                "label": "Power",
            },
            {
                "type": "select",
                "key": "input",
                "options": sorted(VALID_INPUT_CODES),
                "label": "Input",
            },
            {
                "type": "slider",
                "key": "volume",
                "min": 0,
                "max": 100,
                "label": "Volume",
            },
            {"type": "indicator", "key": "audio_mute", "label": "Audio Mute"},
            {"type": "indicator", "key": "video_mute", "label": "Video Mute"},
            {
                "type": "select",
                "key": "aspect",
                "options": sorted(VALID_ASPECT_CODES),
                "label": "Aspect",
            },
            {
                "type": "select",
                "key": "picture_mode",
                "options": sorted(VALID_PICTURE_MODES),
                "label": "Picture Mode",
            },
        ],
        "delays": {"command_response": 0.005},
        "error_modes": {
            "auth_fail": {
                "description": (
                    "Reject all authenticated commands with the "
                    "password-mismatch response until cleared"
                ),
                "set_state": {"force_auth_fail": True},
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Per-client expected hash prefix ("" = non-protected).
        self._auth_prefix: dict[str, str] = {}

    # ── Config helpers ──

    def _protocol(self) -> int:
        return 1 if str(self.config.get("lan_protocol", "2")) == "1" else 2

    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    def _username(self) -> str:
        return str(self.config.get("username", "admin1") or "admin1")

    def _client_id(self) -> str | None:
        if not self._clients:
            return None
        return next(reversed(self._clients))

    # ── Connection greeting ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        word = "PDPCONTROL" if self._protocol() == 1 else "NTCONTROL"
        password = self._password()
        if password:
            random_hex = secrets.token_hex(4)  # 8 lowercase hex chars
            if self._protocol() == 1:
                seed = f"{random_hex}{password}"
            else:
                seed = f"{self._username()}:{password}:{random_hex}"
            self._auth_prefix[client_id] = hashlib.md5(
                seed.encode("ascii")
            ).hexdigest()
            return f"{word} 1 {random_hex}\r".encode("ascii")
        self._auth_prefix[client_id] = ""
        return f"{word} 0\r".encode("ascii")

    # ── Command dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        client_id = self._client_id()
        if client_id is None:
            return None

        line = data.decode("ascii", errors="ignore").strip("\r\n")
        if not line:
            return None

        if self.state.get("force_auth_fail"):
            return self._auth_error()

        prefix = self._auth_prefix.get(client_id, "")
        if prefix:
            if len(line) < 32 or line[:32] != prefix:
                return self._auth_error()
            line = line[32:]

        # Strip the protocol framing around the body.
        if self._protocol() == 1:
            if not (line.startswith(_STX) and line.endswith(_ETX)):
                return b"ERR1\r"
            body = line[1:-1]
        else:
            if not line.startswith("00"):
                return b"ERR1\r"
            body = line[2:]

        return self._dispatch(body)

    def _auth_error(self) -> bytes:
        if self._protocol() == 1:
            return b"PDPCONTROL ERRA\r"
        return b"ERRA\r"

    # ── Body dispatch ──

    def _dispatch(self, body: str) -> bytes | None:
        if not body:
            return b"ERR1\r"
        if body.startswith("Q"):
            return self._handle_query(body)
        return self._handle_setter(body)

    # ── Response framing ──

    def _respond(self, content: str) -> bytes:
        if self._protocol() == 1:
            return f"{_STX}{content}{_ETX}\r".encode("ascii")
        return f"00{content}\r".encode("ascii")

    def _respond_query(self, query: str, value: str) -> bytes:
        """Protocol 1 echoes the query (``QAV:050`` for simple
        queries, ``QPC:MENVIV`` for compound ones); Protocol 2 sends
        the value alone — for a compound query the sub-key rides in
        front of the value, mirroring the documented "response
        content" split at the query's first colon."""
        if self._protocol() == 1:
            joiner = "" if ":" in query else ":"
            return self._respond(f"{query}{joiner}{value}")
        if ":" in query:
            sub = query.split(":", 1)[1]
            return self._respond(f"{sub}{value}")
        return self._respond(value)

    # ── Queries ──

    def _handle_query(self, body: str) -> bytes:
        power_on = self.state.get("power") == "on"
        if body == "QPW":
            return self._respond_query(body, "1" if power_on else "0")
        if body == "QAV":
            return self._respond_query(
                body, f"{int(self.state.get('volume', 0)):03d}"
            )
        if body == "QAM":
            return self._respond_query(
                body, "1" if self.state.get("audio_mute") else "0"
            )
        # Everything else only answers when the panel is on.
        if not power_on:
            return b"ERR3\r"
        if body == "QMI":
            return self._respond_query(body, self.state.get("input", "HM1"))
        if body == "QVM":
            return self._respond_query(
                body, "1" if self.state.get("video_mute") else "0"
            )
        if body == "QAS":
            return self._respond_query(body, self.state.get("aspect", "FULL"))
        if body == "QPC:MEN":
            return self._respond_query(
                body, self.state.get("picture_mode", "STD")
            )
        m = re.fullmatch(r"QPC:([A-Z]{3})", body)
        if m and m.group(1) in _PICTURE_KEYS:
            key = _PICTURE_KEYS[m.group(1)]
            return self._respond_query(
                body, f"{int(self.state.get(key, 0)):03d}"
            )
        return b"ERR1\r"

    # ── Setters ──

    def _handle_setter(self, body: str) -> bytes:
        power_on = self.state.get("power") == "on"

        if body == "PON":
            self.set_state("power", "on")
            return self._respond(body)
        if body == "POF":
            self.set_state("power", "off")
            return self._respond(body)

        # All other setters are unavailable in standby (command-list
        # standby column).
        if not power_on:
            return b"ERR3\r"

        m = re.fullmatch(r"IMS:([A-Z0-9]{2,3})", body)
        if m:
            if m.group(1) not in VALID_INPUT_CODES:
                return b"ERR2\r"
            self.set_state("input", m.group(1))
            return self._respond(body)

        m = re.fullmatch(r"AVL:(\d{3})", body)
        if m:
            value = int(m.group(1))
            if value > 100:
                return b"ERR2\r"
            self.set_state("volume", value)
            return self._respond(body)
        if body == "AUU":
            self.set_state(
                "volume", min(100, int(self.state.get("volume", 0)) + 1)
            )
            return self._respond(body)
        if body == "AUD":
            self.set_state(
                "volume", max(0, int(self.state.get("volume", 0)) - 1)
            )
            return self._respond(body)

        m = re.fullmatch(r"AMT:([01])", body)
        if m:
            self.set_state("audio_mute", m.group(1) == "1")
            return self._respond(body)
        m = re.fullmatch(r"VMT:([01])", body)
        if m:
            self.set_state("video_mute", m.group(1) == "1")
            return self._respond(body)

        m = re.fullmatch(r"DAM:([A-Z0-9]{4})", body)
        if m:
            if m.group(1) not in VALID_ASPECT_CODES:
                return b"ERR2\r"
            self.set_state("aspect", m.group(1))
            return self._respond(body)

        m = re.fullmatch(r"VPC:MEN([A-Z]{3})", body)
        if m:
            if m.group(1) not in VALID_PICTURE_MODES:
                return b"ERR2\r"
            self.set_state("picture_mode", m.group(1))
            return self._respond(body)

        m = re.fullmatch(r"VPC:([A-Z]{3})(\d{3})", body)
        if m and m.group(1) in _PICTURE_KEYS:
            value = int(m.group(2))
            if value > 100:
                return b"ERR2\r"
            self.set_state(_PICTURE_KEYS[m.group(1)], value)
            return self._respond(body)

        return b"ERR1\r"
