"""
Epson ESC/VP21 Projector Simulator.

Server side of ESC/VP.net on TCP 3629:

  - On client connect, reads the 16-byte ``ESC/VP.net\\x10\\x03\\x00\\x00\\x00\\x00``
    handshake preamble and replies with the 16-byte success frame
    ``ESC/VP.net\\x10\\x03\\x00\\x00\\x20\\x00``.
  - Switches the read loop to CR-delimited (``\\r``) ESC/VP21 commands.
  - Replies with ``:`` for set commands, ``KEY=VAL\\r:`` for queries,
    and ``ERR\\r:`` for unknown / invalid commands.

Driver side: ``projectors/epson_escvp.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


HANDSHAKE_REQUEST = b"ESC/VP.net\x10\x03\x00\x00\x00\x00"
HANDSHAKE_REPLY_OK = b"ESC/VP.net\x10\x03\x00\x00\x20\x00"
HANDSHAKE_LEN = 16


# Set commands of the form ``KEY VALUE`` — value can be a fixed token
# (e.g. ``ON`` / ``OFF``) or a hex code (e.g. ``ASPECT 20``).
_SET = re.compile(r"^([A-Z][A-Z0-9_]*)\s+([A-Za-z0-9+\-]+)$")
# Query commands of the form ``KEY?``.
_QUERY = re.compile(r"^([A-Z][A-Z0-9_]*)\?$")


class EpsonEscVpSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "epson_escvp",
        "name": "Epson Projector Simulator (ESC/VP21)",
        "category": "projector",
        "transport": "tcp",
        "default_port": 3629,
        "delimiter": "\r",
        "initial_state": {
            "PWR": "01",        # power on
            "SOURCE": "30",     # HDMI1
            "MUTE": "OFF",
            "FREEZE": "OFF",
            "ASPECT": "00",     # Normal
            "CMODE": "04",      # Presentation
            "LAMP": 1234,       # lamp hours
            "ERR": "00",        # no error
            "SNO": "ABCD1234",
            "VOL": 50,
            "BRIGHT": 50,
            "CONTRAST": 50,
        },
        "controls": [
            {"type": "select", "key": "PWR", "options": ["00", "01", "02", "03"], "label": "Power"},
            {"type": "indicator", "key": "SOURCE", "label": "Source"},
            {"type": "indicator", "key": "MUTE", "label": "A/V Mute"},
            {"type": "indicator", "key": "FREEZE", "label": "Freeze"},
            {"type": "indicator", "key": "ASPECT", "label": "Aspect"},
            {"type": "indicator", "key": "CMODE", "label": "Color Mode"},
            {"type": "indicator", "key": "LAMP", "label": "Lamp Hours"},
        ],
        "delays": {"command_response": 0.005},
        "error_modes": {
            "lamp_failure": {
                "description": "Lamp failure",
                "set_state": {"ERR": "06"},
            },
            "fan_failure": {
                "description": "Fan error",
                "set_state": {"ERR": "01"},
            },
            "overtemp": {
                "description": "High internal temperature",
                "set_state": {"ERR": "04"},
            },
        },
    }

    # ── Connection-time handshake ──
    #
    # We override ``authenticate_client`` because the ESC/VP.net
    # handshake is fixed-length binary that doesn't fit the
    # ``\r``-delimited read loop used after handshake. Reading 16 raw
    # bytes here keeps it out of the message stream that ``handle_command``
    # processes.
    async def authenticate_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_id: str,
    ) -> bool:
        try:
            preamble = await asyncio.wait_for(
                reader.readexactly(HANDSHAKE_LEN), timeout=5.0
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            logger.warning("%s: client %s never sent handshake", self.name, client_id)
            return False

        if preamble != HANDSHAKE_REQUEST:
            logger.warning(
                "%s: client %s sent bad handshake: %s",
                self.name,
                client_id,
                preamble.hex(),
            )
            return False

        writer.write(HANDSHAKE_REPLY_OK)
        await writer.drain()
        self.log_protocol("out", HANDSHAKE_REPLY_OK, client_id)
        return True

    # ── Command dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        # Strip CR and any stray LF.
        line = data.decode("ascii", errors="ignore").strip()
        if not line:
            # Null command — projector returns just the ready prompt.
            return b":"

        m = _QUERY.match(line)
        if m:
            return self._handle_query(m.group(1))

        m = _SET.match(line)
        if m:
            return self._handle_set(m.group(1), m.group(2))

        return b"ERR\r:"

    # ── Queries ──

    def _handle_query(self, key: str) -> bytes:
        state = self.state
        if key == "PWR":
            return self._respond_kv("PWR", state.get("PWR", "00"))
        if key == "SOURCE":
            if state.get("PWR", "00") not in ("01", "02"):
                return b"ERR\r:"
            return self._respond_kv("SOURCE", state.get("SOURCE", "30"))
        if key == "MUTE":
            return self._respond_kv("MUTE", state.get("MUTE", "OFF"))
        if key == "FREEZE":
            return self._respond_kv("FREEZE", state.get("FREEZE", "OFF"))
        if key == "ASPECT":
            return self._respond_kv("ASPECT", state.get("ASPECT", "00"))
        if key == "CMODE":
            return self._respond_kv("CMODE", state.get("CMODE", "04"))
        if key == "LAMP":
            return self._respond_kv("LAMP", str(int(state.get("LAMP", 0))))
        if key == "ERR":
            return self._respond_kv("ERR", state.get("ERR", "00"))
        if key == "SNO":
            return self._respond_kv("SNO", state.get("SNO", ""))
        if key in ("VOL", "BRIGHT", "CONTRAST"):
            return self._respond_kv(key, str(int(state.get(key, 0))))
        return b"ERR\r:"

    # ── Setters ──

    def _handle_set(self, key: str, value: str) -> bytes:
        if key == "PWR":
            v = value.upper()
            if v == "ON":
                self.set_state("PWR", "01")
                return b":"
            if v == "OFF":
                self.set_state("PWR", "00")
                return b":"
            return b"ERR\r:"
        if key == "SOURCE":
            # Two hex digits, otherwise reject.
            if not re.fullmatch(r"[0-9A-Fa-f]{2}", value):
                return b"ERR\r:"
            self.set_state("SOURCE", value.upper())
            return b":"
        if key in ("MUTE", "FREEZE"):
            v = value.upper()
            if v not in ("ON", "OFF"):
                return b"ERR\r:"
            self.set_state(key, v)
            return b":"
        if key in ("ASPECT", "CMODE"):
            if not re.fullmatch(r"[0-9A-Fa-f]{2}", value):
                return b"ERR\r:"
            self.set_state(key, value.upper())
            return b":"
        if key == "KEY":
            # Remote-key passthrough — just ack. Map a few common keys
            # to state changes so macro tests against the sim feel real.
            v = value.upper()
            if v == "04":  # Power On
                self.set_state("PWR", "01")
            elif v == "05":  # Power Off / Esc
                self.set_state("PWR", "00")
            elif v == "3E":  # Mute
                cur = self.state.get("MUTE", "OFF")
                self.set_state("MUTE", "OFF" if cur == "ON" else "ON")
            elif v == "32":  # Freeze
                cur = self.state.get("FREEZE", "OFF")
                self.set_state("FREEZE", "OFF" if cur == "ON" else "ON")
            return b":"
        if key in ("VOL", "BRIGHT", "CONTRAST", "TINT"):
            up = value.upper()
            cur = int(self.state.get(key, 0))
            if up == "INC":
                self.set_state(key, min(100, cur + 1))
            elif up == "DEC":
                self.set_state(key, max(0, cur - 1))
            elif up == "INIT":
                self.set_state(key, 50)
            else:
                try:
                    self.set_state(key, int(value))
                except ValueError:
                    return b"ERR\r:"
            return b":"
        return b"ERR\r:"

    @staticmethod
    def _respond_kv(key: str, value: str) -> bytes:
        return f"{key}={value}\r:".encode("ascii")
