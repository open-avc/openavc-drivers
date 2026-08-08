"""
Panasonic PT-Series Projector — NTCONTROL (Protocol 2) Simulator.

Implements the projector side of Panasonic NTCONTROL on TCP 1024:

  - Sends the NTCONTROL greeting on connect — protected mode
    ``NTCONTROL 1 <random8>(CR)`` if ``config["password"]`` is set,
    otherwise non-protected ``NTCONTROL 0(CR)``.
  - Validates the MD5 session prefix (lowercase hex digest of
    ``"<username>:<password>:<random8>"``) against the leading 32
    bytes of every incoming command. A mismatch returns ``ERRA``.
  - Handles the universal command surface — power (PON / POF / QPW),
    input select (IIS:* / QIN), shutter / AV mute (OSH:* / QSH),
    freeze (OFZ:* / QFZ), and operating-hours readout (Q$S). Setters
    echo the command on success; unsupported inputs return ``ERR2``;
    queries while the lamp is off return ``ERR3`` for input / shutter
    / freeze (matches real-device behaviour and exercises the
    driver's ``ERR3 = unavailable`` path).

Driver side: ``projectors/panasonic_pt.py``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


# Universal Panasonic input codes the simulator accepts.
VALID_INPUT_CODES = {
    "HD1",
    "HD2",
    "RG1",
    "RG2",
    "VID",
    "SVD",
    "DV1",
    "SD1",
    "SD2",
    "DL1",
}

# Power query response.
_POWER_REV = {"on": "001", "off": "000"}

# Picture settings: query name -> state key, and setter code -> (key, min, max).
_PICTURE_QUERY = {
    "QVB": "brightness",
    "QVR": "contrast",
    "QVC": "color",
    "QVT": "tint",
    "QVS": "sharpness",
}
_PICTURE_SET = {
    "VBR": ("brightness", 1, 63),
    "VCN": ("contrast", 1, 63),
    "VCO": ("color", 1, 63),
    "VTN": ("tint", 1, 63),
    "VSR": ("sharpness", 0, 15),
}

# Pattern for any ``<setter>:<value>`` line.
_SET_RE = re.compile(r"^([A-Z]{3})(?::(.+))?$")
# Pattern for query lines ``Q<XX>`` (3 letters) and ``Q$<L>``.
_QUERY_RE = re.compile(r"^(Q[A-Z\$]{2,3})$")


class PanasonicPtSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "panasonic_pt",
        "name": "Panasonic PT Projector Simulator",
        "category": "projector",
        "transport": "tcp",
        "default_port": 1024,
        "delimiter": "\r",
        "initial_state": {
            "power": "off",
            "input": "HD1",
            "mute_video": False,
            "freeze": False,
            "operating_hours": 1234,
            "brightness": 32,
            "contrast": 32,
            "color": 32,
            "tint": 32,
            "sharpness": 8,
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
                "type": "indicator",
                "key": "mute_video",
                "label": "Shutter",
            },
            {"type": "indicator", "key": "freeze", "label": "Freeze"},
            {
                "type": "indicator",
                "key": "operating_hours",
                "label": "Operating Hours",
            },
        ],
        "delays": {"command_response": 0.005},
        "error_modes": {
            "auth_fail": {
                "description": (
                    "Reject all authenticated commands with ERRA "
                    "until cleared"
                ),
                "set_state": {"force_auth_fail": True},
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Per-client auth state — protected mode keeps a random and a
        # computed expected prefix; non-protected sets ``""`` to mean
        # "no prefix required".
        self._auth_prefix: dict[str, str] = {}

    # ── Connection greeting ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        password = self._password()
        if password:
            random_hex = secrets.token_hex(4)  # 8 lowercase hex chars
            username = self._username()
            digest = hashlib.md5(
                f"{username}:{password}:{random_hex}".encode("ascii")
            ).hexdigest()
            self._auth_prefix[client_id] = digest
            return f"NTCONTROL 1 {random_hex}\r".encode("ascii")
        # Non-protected.
        self._auth_prefix[client_id] = ""
        return b"NTCONTROL 0\r"

    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    def _username(self) -> str:
        return str(self.config.get("username", "admin1") or "admin1")

    def _client_id(self) -> str | None:
        if not self._clients:
            return None
        return next(reversed(self._clients))

    # ── Command dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        client_id = self._client_id()
        if client_id is None:
            return None

        line = data.decode("ascii", errors="ignore").strip()
        if not line:
            return None

        if self.state.get("force_auth_fail"):
            return b"ERRA\r"

        prefix = self._auth_prefix.get(client_id, "")
        if prefix:
            # Protected mode — first 32 chars must match the hash.
            if len(line) < 32 or line[:32] != prefix:
                return b"ERRA\r"
            line = line[32:]

        # Strip the literal ``00`` framing pair.
        if not line.startswith("00"):
            return b"ERR1\r"
        body = line[2:]

        return self._dispatch(body)

    # ── Body dispatch ──

    def _dispatch(self, body: str) -> bytes | None:
        if not body:
            return b"ERR1\r"

        # Queries first (single-token starting with Q).
        m = _QUERY_RE.match(body)
        if m:
            return self._handle_query(m.group(1))

        m = _SET_RE.match(body)
        if m:
            code = m.group(1)
            param = m.group(2)
            return self._handle_setter(code, param, body)

        return b"ERR1\r"

    # ── Queries ──

    def _handle_query(self, name: str) -> bytes:
        power = self.state.get("power", "off")
        if name == "QPW":
            return self._respond(_POWER_REV.get(power, "000"))
        if name == "Q$S":
            # 6-digit zero-padded operating hours, matches PT-MZ format.
            hours = int(self.state.get("operating_hours", 0))
            return self._respond(f"{hours:06d}")
        # The remaining queries report only when the lamp is on. Real
        # devices return ERR3 ("busy / unavailable period") otherwise.
        if power != "on":
            return b"ERR3\r"
        if name == "QIN":
            return self._respond(self.state.get("input", "HD1"))
        if name == "QSH":
            return self._respond(
                "1" if self.state.get("mute_video") else "0"
            )
        if name == "QFZ":
            return self._respond(
                "1" if self.state.get("freeze") else "0"
            )
        if name in _PICTURE_QUERY:
            value = int(self.state.get(_PICTURE_QUERY[name], 0))
            return self._respond(f"{value:03d}")
        return b"ERR1\r"

    # ── Setters ──

    def _handle_setter(
        self, code: str, param: str | None, raw_body: str
    ) -> bytes:
        if code == "PON":
            self.set_state("power", "on")
            return self._respond(raw_body)
        if code == "POF":
            self.set_state("power", "off")
            return self._respond(raw_body)
        if code == "IIS":
            if param not in VALID_INPUT_CODES:
                return b"ERR2\r"
            if self.state.get("power") != "on":
                return b"ERR3\r"
            self.set_state("input", param)
            return self._respond(raw_body)
        if code == "OSH":
            if param not in ("0", "1"):
                return b"ERR2\r"
            if self.state.get("power") != "on":
                return b"ERR3\r"
            self.set_state("mute_video", param == "1")
            return self._respond(raw_body)
        if code == "OFZ":
            if param not in ("0", "1"):
                return b"ERR2\r"
            if self.state.get("power") != "on":
                return b"ERR3\r"
            self.set_state("freeze", param == "1")
            return self._respond(raw_body)
        if code in _PICTURE_SET:
            key, lo, hi = _PICTURE_SET[code]
            if self.state.get("power") != "on":
                return b"ERR3\r"
            try:
                value = int(param) if param is not None else None
            except ValueError:
                return b"ERR2\r"
            if value is None or not (lo <= value <= hi):
                return b"ERR2\r"
            self.set_state(key, value)
            return self._respond(raw_body)
        return b"ERR1\r"

    def _respond(self, body: str) -> bytes:
        # Protocol 2 success responses are ``00<body>(CR)``.
        return f"00{body}\r".encode("ascii")
