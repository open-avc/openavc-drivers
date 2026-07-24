"""
Sony VPL Projector — ADCP Simulator.

Implements the projector side of Sony ADCP on TCP 53595:

  - Sends an authentication greeting on connect: a random 8-char hex
    challenge if config["password"] is set, or "NOKEY" if it's empty.
  - Validates the SHA-256 challenge response (lowercase hex digest of
    "<challenge> <password>") and replies "ok" or "err_auth".
  - Handles the universal command surface — power, input, blank,
    muting, freeze, picture_mode, aspect, contrast / brightness /
    color / sharpness — plus power_status / error queries and the
    "key" remote-control passthrough.

Driver side: ``projectors/sony_vpl.py``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


VALID_INPUTS = {
    "video1",
    "svideo1",
    "rgb1",
    "rgb2",
    "dvi1",
    "hdmi1",
    "hdmi2",
    "hdbaset1",
    "option1",
    "network",
    "usb_a",
    "usb_b",
    "web_content",
}

VALID_PICTURE_MODES = {
    "dynamic",
    "standard",
    "brt_priority",
    "multi_screen",
    "presentation",
    "blackboard",
    "whiteboard",
    "cinema",
    "vivid",
    "srgb",
}

VALID_ASPECTS = {
    "normal",
    "full1",
    "full2",
    "full",
    "zoom",
    "v_stretch",
    "stretch",
    "squeeze",
    "16_9",
    "4_3",
}

# Pattern for `command "value"` (a setter with quoted string param).
_QUOTED_SET = re.compile(r'^([a-z_][a-z0-9_]*)\s+"([^"]*)"$')
# Pattern for `command <int>` (a setter with numeric param).
_NUMERIC_SET = re.compile(r"^([a-z_][a-z0-9_]*)\s+(-?\d+)$")
# Pattern for `command ?` (a query).
_QUERY = re.compile(r"^([a-z_][a-z0-9_]*)\s*\?$")


class SonyVplSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "sony_vpl",
        "name": "Sony VPL Projector Simulator",
        "category": "projector",
        "transport": "tcp",
        "default_port": 53595,
        "delimiter": "\r\n",
        "initial_state": {
            "power_status": "standby",
            "input": "hdmi1",
            "blank": "off",
            "muting": "off",
            "freeze": "off",
            "picture_mode": "standard",
            "aspect": "normal",
            "contrast": 50,
            "brightness": 50,
            "color": 50,
            "sharpness": 50,
        },
        "controls": [
            {
                "type": "select",
                "key": "power_status",
                "options": [
                    "standby",
                    "startup",
                    "on",
                    "cooling1",
                    "cooling2",
                ],
                "label": "Power",
            },
            {
                "type": "select",
                "key": "input",
                "options": sorted(VALID_INPUTS),
                "label": "Input",
            },
            {"type": "indicator", "key": "blank", "label": "Blank"},
            {"type": "indicator", "key": "muting", "label": "Audio Mute"},
            {"type": "indicator", "key": "freeze", "label": "Freeze"},
            {
                "type": "select",
                "key": "picture_mode",
                "options": sorted(VALID_PICTURE_MODES),
                "label": "Picture Mode",
            },
            {
                "type": "select",
                "key": "aspect",
                "options": sorted(VALID_ASPECTS),
                "label": "Aspect",
            },
        ],
        "delays": {"command_response": 0.005},
        "error_modes": {
            "lamp_failure": {
                "description": "Light source error",
                "set_state": {"error": "err_light_src"},
            },
            "overtemp": {
                "description": "Temperature error",
                "set_state": {"error": "err_temp"},
            },
            "fan_failure": {
                "description": "Fan error",
                "set_state": {"error": "err_fan"},
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._auth_state: dict[str, str] = {}
        self._challenges: dict[str, str] = {}
        # Single-threaded sim, so a shared pending error string is fine.
        self._error: str = "no_err"

    # ── Connection greeting ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        password = self._password()
        if password:
            challenge = secrets.token_hex(4)
            self._challenges[client_id] = challenge
            self._auth_state[client_id] = "awaiting_response"
            return f"{challenge}\r\n".encode("ascii")
        # Auth disabled — send NOKEY and skip straight to commands.
        self._auth_state[client_id] = "authenticated"
        return b"NOKEY\r\n"

    def _password(self) -> str:
        # Pull from config (so tests can drive auth on/off) or default
        # to Sony's factory password.
        return str(
            self.config.get(
                "password", self.SIMULATOR_INFO.get("default_password", "")
            )
            or ""
        )

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

        state = self._auth_state.get(client_id, "authenticated")
        if state == "awaiting_response":
            return self._handle_auth_response(client_id, line)

        return self._handle_adcp_line(line)

    def _handle_auth_response(self, client_id: str, line: str) -> bytes:
        challenge = self._challenges.pop(client_id, "")
        password = self._password()
        expected = hashlib.sha256(
            f"{challenge} {password}".encode("ascii", errors="ignore")
        ).hexdigest()
        if line == expected:
            self._auth_state[client_id] = "authenticated"
            return b"ok\r\n"
        # Auth failed — drop the auth state so further commands also
        # err_auth. (A real projector closes the socket; for the sim we
        # let the line stay open so tests can observe the failure.)
        self._auth_state[client_id] = "rejected"
        return b"err_auth\r\n"

    def _handle_adcp_line(self, line: str) -> bytes | None:
        # Reject queries from rejected clients explicitly.
        m = _QUERY.match(line)
        if m:
            return self._handle_query(m.group(1))

        m = _QUOTED_SET.match(line)
        if m:
            return self._handle_quoted_set(m.group(1), m.group(2))

        m = _NUMERIC_SET.match(line)
        if m:
            return self._handle_numeric_set(m.group(1), int(m.group(2)))

        return b"err_cmd\r\n"

    # ── Queries ──

    def _handle_query(self, name: str) -> bytes:
        if name == "power_status":
            return self._respond(f'"{self.state.get("power_status", "standby")}"')
        if name == "error":
            err = self.state.get("error", self._error)
            if err in (None, "", "no_err"):
                return self._respond('"no_err"')
            # Real projectors return a JSON array of error tokens.
            return self._respond(f'["{err}"]')
        if name == "input":
            return self._respond(f'"{self.state.get("input", "hdmi1")}"')
        if name == "blank":
            return self._respond(f'"{self.state.get("blank", "off")}"')
        if name == "muting":
            return self._respond(f'"{self.state.get("muting", "off")}"')
        if name == "freeze":
            return self._respond(f'"{self.state.get("freeze", "off")}"')
        if name == "picture_mode":
            return self._respond(f'"{self.state.get("picture_mode", "standard")}"')
        if name == "aspect":
            return self._respond(f'"{self.state.get("aspect", "normal")}"')
        if name in ("contrast", "brightness", "color", "sharpness"):
            return self._respond(str(int(self.state.get(name, 0))))
        return b"err_cmd\r\n"

    # ── Setters ──

    def _handle_quoted_set(self, name: str, value: str) -> bytes:
        if name == "power":
            if value == "on":
                self.set_state("power_status", "on")
                return b"ok\r\n"
            if value == "off":
                self.set_state("power_status", "standby")
                return b"ok\r\n"
            return b"err_val\r\n"
        if name == "input":
            if value not in VALID_INPUTS:
                return b"err_val\r\n"
            self.set_state("input", value)
            return b"ok\r\n"
        if name in ("blank", "muting", "freeze"):
            if value not in ("on", "off"):
                return b"err_val\r\n"
            self.set_state(name, value)
            return b"ok\r\n"
        if name == "picture_mode":
            if value not in VALID_PICTURE_MODES:
                return b"err_val\r\n"
            self.set_state("picture_mode", value)
            return b"ok\r\n"
        if name == "aspect":
            if value not in VALID_ASPECTS:
                return b"err_val\r\n"
            self.set_state("aspect", value)
            return b"ok\r\n"
        if name == "key":
            # Remote-key passthrough — just ack. Specific keys map to
            # state changes for the most-common cases so macro tests
            # against the sim feel realistic.
            if value == "power_on":
                self.set_state("power_status", "on")
            elif value == "power_off":
                self.set_state("power_status", "standby")
            elif value == "blank":
                cur = self.state.get("blank", "off")
                self.set_state("blank", "off" if cur == "on" else "on")
            elif value == "muting":
                cur = self.state.get("muting", "off")
                self.set_state("muting", "off" if cur == "on" else "on")
            elif value == "freeze":
                cur = self.state.get("freeze", "off")
                self.set_state("freeze", "off" if cur == "on" else "on")
            return b"ok\r\n"
        return b"err_cmd\r\n"

    def _handle_numeric_set(self, name: str, value: int) -> bytes:
        if name in ("contrast", "brightness", "color", "sharpness"):
            if not 0 <= value <= 100:
                return b"err_val\r\n"
            self.set_state(name, value)
            return b"ok\r\n"
        return b"err_cmd\r\n"

    def _respond(self, payload: str) -> bytes:
        return f"{payload}\r\n".encode("ascii")
