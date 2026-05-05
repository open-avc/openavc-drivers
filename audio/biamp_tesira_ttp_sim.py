"""
Biamp Tesira TTP — Simulator.

Implements the Tesira Text Protocol on TCP port 23. Behaves like a real
Tesira DSP for everything the driver does:

- Sends a Telnet IAC option-negotiation sequence on connect, followed by
  the canonical "Welcome to the Tesira Text Protocol Server" banner. The
  driver under test exercises the full handshake path.
- Accepts commands: <TAG> get/set/toggle/increment/decrement/subscribe/
  unsubscribe <attribute> [<index>] [<value>], plus DEVICE recallPreset,
  DEVICE recallPresetByName, SESSION set verbose, SESSION quit, etc.
- Maintains a per-block, per-channel state model that defaults to a
  representative conferencing room. Set commands mutate state; get
  commands return the current value.
- Tracks per-client subscriptions and pushes ``! "publishToken":...``
  notifications to subscribed clients whenever a watched attribute
  changes — same as a real Tesira unit.

Driver side: ``audio/biamp_tesira_ttp.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


WELCOME_BANNER = b"Welcome to the Tesira Text Protocol Server\r\n"
LINE_ENDING = b"\r\n"

# Sample IAC option negotiation we send on connect to exercise the driver's
# IAC handling. IAC=255, DO=253, ECHO=1; IAC, DO, SUPPRESS_GO_AHEAD=3.
IAC_NEGOTIATION = bytes([0xFF, 0xFD, 0x01, 0xFF, 0xFD, 0x03])

# Default DSP state — enough blocks to demonstrate a typical conference room.
# Drivers that override the blocks config will exercise different state
# names, but the simulator's command handlers are generic — any unknown
# (tag, attribute, channel) tuple is just stored in self._dsp without
# special handling, so the simulator works for any block topology.
DEFAULT_DSP_STATE: dict[tuple[str, str, int | None], Any] = {
    # (instance_tag, attribute, index) -> value
    ("Mute1", "mute", 1): False,
    ("Mute1", "mute", 2): False,
    ("Mute1", "mute", 3): False,
    ("Mute1", "mute", 4): False,
    ("Level1", "level", 1): -10.0,
    ("Level1", "level", 2): -10.0,
    ("Level1", "level", 3): -10.0,
    ("Level1", "level", 4): -10.0,
    ("Level1", "mute", 1): False,
    ("Level1", "mute", 2): False,
    ("Level1", "mute", 3): False,
    ("Level1", "mute", 4): False,
    ("PgmMute", "mute", 1): False,
    ("PgmLvl", "level", 1): 0.0,
    ("PgmLvl", "mute", 1): False,
    ("PgmSrc", "sourceSelection", None): 1,
    ("PgmSrc", "outputLevel", None): 0.0,
    ("PgmSrc", "outputMute", None): False,
}


# ── Wire format helpers ──

def _format_value(v: Any) -> str:
    """Format a Python value as the TTP wire token."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        # Tesira returns numeric values with high precision for floats.
        # Match the format Companion observes from real hardware:
        # "-12.500000" for floats, plain digits for ints.
        if isinstance(v, float):
            return f"{v:.6f}"
        return str(v)
    return f'"{v}"'


def _ok_value(v: Any) -> bytes:
    return f'+OK "value":{_format_value(v)}\r\n'.encode("utf-8")


def _push_value(token: str, v: Any) -> bytes:
    return f'! "publishToken":"{token}" "value":{_format_value(v)}\r\n'.encode("utf-8")


def _err(msg: str) -> bytes:
    return f"-ERR {msg}\r\n".encode("utf-8")


# Command parsing regexes
RE_DEVICE = re.compile(r"^DEVICE\s+(.*)$", re.IGNORECASE)
RE_SESSION = re.compile(r"^SESSION\s+(.*)$", re.IGNORECASE)


class BiampTesiraTTPSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "biamp_tesira_ttp",
        "name": "Biamp Tesira TTP Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 23,
        "delimiter": "\r\n",
        "initial_state": {
            "model": "TesiraFORTÉ X 800",
            "firmware": "4.14.0",
            "serial_number": "SIM00001",
            "hostname": "TesiraSim01",
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
            {"type": "indicator", "key": "serial_number", "label": "Serial"},
            # Editable controls so the operator can drive state changes from
            # the simulator UI and watch the driver react via subscription
            # pushes.
            {"type": "slider", "key": "Level1_ch1_db", "label": "Level1 Ch1 (dB)",
             "min": -100, "max": 12, "step": 0.5},
            {"type": "slider", "key": "Level1_ch2_db", "label": "Level1 Ch2 (dB)",
             "min": -100, "max": 12, "step": 0.5},
            {"type": "toggle", "key": "Mute1_ch1", "label": "Mute1 Ch1"},
            {"type": "toggle", "key": "Mute1_ch2", "label": "Mute1 Ch2"},
            {"type": "slider", "key": "PgmLvl_ch1_db", "label": "Program Level (dB)",
             "min": -100, "max": 12, "step": 0.5},
            {"type": "toggle", "key": "PgmMute_ch1", "label": "Program Mute"},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # DSP state, keyed by (tag, attribute, index_or_None)
        self._dsp: dict[tuple[str, str, int | None], Any] = dict(DEFAULT_DSP_STATE)
        # Per-client subscriptions:
        # {client_id: [{"tag", "attribute", "index", "token", "rate_ms"}]}
        self._client_subs: dict[str, list[dict[str, Any]]] = {}
        # Per-client verbose mode (ignored — we never echo)
        self._client_verbose: dict[str, bool] = {}
        # Last-recalled preset
        self._last_preset = ""

        # Bridge simulator UI controls back into the DSP state map: when
        # the operator moves a slider in the simulator UI, the framework
        # calls set_state() with the control key — we intercept that
        # change here and propagate to the DSP state + push subscribers.
        # This is a deliberate convenience: the simulator UI control
        # `Level1_ch1_db` corresponds to the DSP attribute (Level1, level,
        # 1).
        self._ui_to_dsp = {
            "Level1_ch1_db": ("Level1", "level", 1),
            "Level1_ch2_db": ("Level1", "level", 2),
            "Mute1_ch1": ("Mute1", "mute", 1),
            "Mute1_ch2": ("Mute1", "mute", 2),
            "PgmLvl_ch1_db": ("PgmLvl", "level", 1),
            "PgmMute_ch1": ("PgmMute", "mute", 1),
        }

    # ── Connection ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        # Initialize per-client state
        self._client_subs[client_id] = []
        self._client_verbose[client_id] = True
        # Send IAC negotiation followed by the welcome banner. The driver
        # will reply WONT/DONT to our IAC and then look for the banner.
        return IAC_NEGOTIATION + LINE_ENDING + WELCOME_BANNER

    # ── Command dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        # The framework strips line endings. The driver's IAC option
        # replies (raw 3-byte sequences with no delimiter) can land at
        # the start of an otherwise-printable command — TCP fragmentation
        # plus the framework's per-client line buffer will concatenate
        # them with the next command line. Strip any leading bytes that
        # aren't ASCII alphanumeric / common command characters before
        # decoding so the regex-based parsers below see a clean line.
        i = 0
        while i < len(data) and data[i] not in b" \t\r\n" and not (
            0x20 <= data[i] < 0x7F
        ):
            i += 1
        if i:
            data = data[i:]

        line = data.decode("utf-8", errors="replace").strip()
        if not line:
            return None

        # Reject lines that still contain non-printable garbage (defensive).
        if not any(c.isprintable() and c not in "�" for c in line):
            return None

        client_id = self._latest_client_id()
        if client_id is None:
            return None

        # SESSION commands
        m = RE_SESSION.match(line)
        if m:
            return self._handle_session(client_id, m.group(1).strip())

        # DEVICE commands (recallPreset, savePreset, get serialNumber, etc.)
        m = RE_DEVICE.match(line)
        if m:
            return self._handle_device(client_id, m.group(1).strip())

        # Generic <TAG> <command> <attr> [...]
        parts = line.split()
        if len(parts) < 2:
            return _err("Parse error: not enough tokens")
        tag = parts[0]
        cmd = parts[1].lower()
        rest = parts[2:]

        if cmd == "get":
            return self._handle_get(tag, rest)
        if cmd == "set":
            return self._handle_set(client_id, tag, rest)
        if cmd == "toggle":
            return self._handle_toggle(client_id, tag, rest)
        if cmd == "increment":
            return self._handle_step(client_id, tag, rest, sign=1)
        if cmd == "decrement":
            return self._handle_step(client_id, tag, rest, sign=-1)
        if cmd == "subscribe":
            return self._handle_subscribe(client_id, tag, rest)
        if cmd == "unsubscribe":
            return self._handle_unsubscribe(client_id, tag, rest)
        if cmd in ("dial", "end", "answer", "dtmf"):
            # Dialer convenience — accept and ack (no real call state)
            return b"+OK\r\n"

        return _err(f"Unknown command: {cmd}")

    def _latest_client_id(self) -> str | None:
        if not self._clients:
            return None
        return next(reversed(self._clients))

    # ── SESSION / DEVICE handlers ──

    def _handle_session(self, client_id: str, body: str) -> bytes:
        body = body.lower().strip()
        if body == "quit":
            # Caller wants to disconnect. We can't tear down the writer
            # from here; just ack and let the client close.
            return b"+OK\r\n"
        if body.startswith("set verbose"):
            tok = body.split()[-1]
            self._client_verbose[client_id] = tok in ("true", "1", "on")
            return b"+OK\r\n"
        if body.startswith("set aliasusage"):
            return b"+OK\r\n"
        if body == "get aliases":
            tags = sorted({k[0] for k in self._dsp.keys()})
            joined = " ".join(f'"{t}"' for t in tags)
            return f'+OK "list":[{joined}]\r\n'.encode("utf-8")
        return b"+OK\r\n"

    def _handle_device(self, client_id: str, body: str) -> bytes:
        # DEVICE get serialNumber / version / hostname
        if body.lower().startswith("get "):
            attr = body[4:].strip()
            if attr == "serialNumber":
                return _ok_value(self.state.get("serial_number") or "SIM00001")
            if attr == "version":
                return _ok_value(self.state.get("firmware") or "4.14.0")
            if attr == "hostname":
                return _ok_value(self.state.get("hostname") or "TesiraSim01")
            if attr == "model":
                return _ok_value(self.state.get("model") or "TesiraFORTÉ X 800")
            return _err(f"address not found: DEVICE {attr}")
        if body.lower().startswith("recallpreset "):
            preset = body[13:].strip()
            self._last_preset = preset
            return b"+OK\r\n"
        if body.lower().startswith("recallpresetbyname "):
            name = body[19:].strip().strip('"')
            self._last_preset = name
            return b"+OK\r\n"
        if body.lower().startswith("savepreset "):
            return b"+OK\r\n"
        return _err(f"DEVICE: unknown command {body}")

    # ── get / set / toggle / inc/dec ──

    def _handle_get(self, tag: str, rest: list[str]) -> bytes:
        if not rest:
            return _err("Parse error: missing attribute")
        attr = rest[0]
        # Two-index attributes (crosspointLevel, crosspointLevelState)
        # carry two indexes after the attribute name.
        if attr in ("crosspointLevel", "crosspointLevelState") and len(rest) >= 3:
            try:
                idx: int | tuple[int, int] | None = (int(rest[1]), int(rest[2]))
            except ValueError:
                idx = None
        elif len(rest) > 1:
            idx = self._parse_index(rest[1])
        else:
            idx = None
        key = (tag, attr, idx)
        # If the integrator declared this attribute and we don't have a
        # value yet, seed a sensible default so subsequent gets/pushes
        # produce something. We DON'T register state here — pushes only
        # fire when an explicit set / toggle / inc / dec occurs.
        if key not in self._dsp:
            seeded = self._seed_default(attr)
            if seeded is None:
                return _err(f'address not found: {tag} {attr}')
            self._dsp[key] = seeded
        return _ok_value(self._dsp[key])

    def _handle_set(self, client_id: str, tag: str, rest: list[str]) -> bytes:
        # Special case: crosspointLevelState / crosspointLevel take TWO
        # indexes — handle by detecting the attribute name.
        if rest and rest[0] in ("crosspointLevelState", "crosspointLevel"):
            attr = rest[0]
            if len(rest) < 4:
                return _err("Parse error: not enough parameters supplied")
            try:
                i, o = int(rest[1]), int(rest[2])
            except ValueError:
                return _err("Parse error: index must be integer")
            value = self._coerce_token(" ".join(rest[3:]))
            key = (tag, attr, (i, o))
            self._dsp[key] = value
            self._push_subscribers(tag, attr, (i, o))
            return b"+OK\r\n"

        # Special case: rampLevel — <TAG> set rampLevel <ch> <dB> <s>
        if rest and rest[0] == "rampLevel":
            if len(rest) < 4:
                return _err("Parse error: not enough parameters")
            try:
                ch = int(rest[1])
                target = float(rest[2])
                # duration ignored in sim — instant ramp
            except ValueError:
                return _err("Parse error: numeric argument expected")
            key = (tag, "level", ch)
            self._dsp[key] = target
            self._push_subscribers(tag, "level", ch)
            return b"+OK\r\n"

        if not rest:
            return _err("Parse error: missing attribute")
        attr = rest[0]
        # Determine index/value layout. Indexed attrs have 3+ tokens
        # (attr, idx, val). Indexless attrs have 2 (attr, val).
        if len(rest) == 2:
            idx = None
            value_tok = rest[1]
        elif len(rest) >= 3:
            try:
                idx = int(rest[1])
                value_tok = " ".join(rest[2:])
            except ValueError:
                # Index wasn't a number — treat as indexless multi-word value
                idx = None
                value_tok = " ".join(rest[1:])
        else:
            return _err("Parse error: not enough parameters")

        value = self._coerce_token(value_tok)
        key = (tag, attr, idx)
        self._dsp[key] = value
        self._push_subscribers(tag, attr, idx)
        return b"+OK\r\n"

    def _handle_toggle(self, client_id: str, tag: str, rest: list[str]) -> bytes:
        if not rest:
            return _err("Parse error: missing attribute")
        attr = rest[0]
        idx = self._parse_index(rest[1] if len(rest) > 1 else None)
        key = (tag, attr, idx)
        cur = self._dsp.get(key, False)
        new = not bool(cur)
        self._dsp[key] = new
        self._push_subscribers(tag, attr, idx)
        return b"+OK\r\n"

    def _handle_step(
        self, client_id: str, tag: str, rest: list[str], sign: int,
    ) -> bytes:
        if len(rest) < 2:
            return _err("Parse error: increment requires attribute and amount")
        attr = rest[0]
        # Determine if there's an index in the middle
        if len(rest) >= 3:
            try:
                idx: int | None = int(rest[1])
                amount = float(rest[2])
            except ValueError:
                idx = None
                amount = float(rest[1])
        else:
            idx = None
            amount = float(rest[1])
        key = (tag, attr, idx)
        cur = float(self._dsp.get(key, 0.0))
        new = cur + sign * abs(amount)
        # Tesira clamps level to [-100, 12] dB
        if attr == "level":
            new = max(-100.0, min(12.0, new))
        self._dsp[key] = new
        self._push_subscribers(tag, attr, idx)
        return b"+OK\r\n"

    # ── subscribe / unsubscribe ──

    def _handle_subscribe(
        self, client_id: str, tag: str, rest: list[str],
    ) -> bytes:
        # Wire format: <TAG> subscribe <attr> [<idx>] [<idx2>] "<token>" <rate_ms>
        # Two-index attributes (crosspointLevel, crosspointLevelState) supply
        # two indexes between attribute and token.
        if len(rest) < 3:
            return _err("Parse error: subscribe requires attribute, token, rate")
        attr = rest[0]
        # Identify rate (last token) and token (second-to-last, quoted).
        try:
            rate_ms = int(rest[-1])
        except ValueError:
            return _err("Parse error: rate must be integer")
        token = rest[-2].strip('"')
        middle = rest[1:-2]  # one or two indexes, or empty
        idx: int | tuple[int, int] | None
        if len(middle) >= 2:
            try:
                idx = (int(middle[0]), int(middle[1]))
            except ValueError:
                idx = None
        elif len(middle) == 1:
            try:
                idx = int(middle[0])
            except ValueError:
                idx = None
        else:
            idx = None

        sub = {
            "tag": tag,
            "attribute": attr,
            "index": idx,
            "token": token,
            "rate_ms": rate_ms,
        }
        # Replace any existing sub with the same token
        subs = self._client_subs.setdefault(client_id, [])
        subs[:] = [s for s in subs if s["token"] != token]
        subs.append(sub)
        # Tesira sends the initial value immediately upon subscribe.
        key = (tag, attr, idx)
        if key not in self._dsp:
            seeded = self._seed_default(attr)
            if seeded is not None:
                self._dsp[key] = seeded
        if key in self._dsp:
            asyncio.create_task(
                self.push_to(client_id, _push_value(token, self._dsp[key]))
            )
        return b"+OK\r\n"

    def _handle_unsubscribe(
        self, client_id: str, tag: str, rest: list[str],
    ) -> bytes:
        if len(rest) < 2:
            return _err("Parse error: unsubscribe requires attribute and token")
        token = rest[-1].strip('"')
        subs = self._client_subs.get(client_id, [])
        subs[:] = [s for s in subs if s["token"] != token]
        return b"+OK\r\n"

    # ── Push helpers ──

    def _push_subscribers(
        self, tag: str, attr: str, idx: int | tuple[int, int] | None,
    ) -> None:
        """Find any client subs that match (tag, attr, idx) and push."""
        key = (tag, attr, idx)
        if key not in self._dsp:
            return
        value = self._dsp[key]
        for client_id, subs in self._client_subs.items():
            for sub in subs:
                if sub["tag"] != tag or sub["attribute"] != attr:
                    continue
                if sub["index"] != idx:
                    continue
                asyncio.create_task(
                    self.push_to(client_id, _push_value(sub["token"], value))
                )

    # ── Value coercion / seeding ──

    @staticmethod
    def _parse_index(tok: str | None) -> int | None:
        if tok is None:
            return None
        try:
            return int(tok)
        except ValueError:
            return None

    @staticmethod
    def _coerce_token(tok: str) -> Any:
        """Turn a TTP value token into a Python primitive."""
        s = tok.strip()
        if s.lower() == "true":
            return True
        if s.lower() == "false":
            return False
        # Quoted string
        if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
            return s[1:-1].replace('\\"', '"')
        # Number
        try:
            if "." in s:
                return float(s)
            return int(s)
        except ValueError:
            return s

    @staticmethod
    def _seed_default(attr: str) -> Any:
        """Pick a sensible default for an unknown (tag, attr, idx) tuple."""
        bool_attrs = {
            "mute", "outputMute", "inputMute", "channelMute",
            "combine", "state", "ercState", "signalPresent",
            "generatorEnable", "crosspointLevelState",
        }
        number_attrs = {
            "level", "outputLevel", "inputLevel", "channelLevel",
            "crosspointLevel", "amplitude", "gainReduction",
            "signalLevel", "minLevel", "maxLevel",
        }
        integer_attrs = {
            "sourceSelection", "output", "group", "frequency",
        }
        string_attrs = {"callState", "lastNumberDialed"}
        if attr in bool_attrs:
            return False
        if attr in number_attrs:
            return -10.0
        if attr in integer_attrs:
            return 0
        if attr in string_attrs:
            return ""
        return None

    # ── Bridge UI controls to DSP state ──

    def set_state(self, key: str, value: Any) -> None:
        """Override BaseSimulator.set_state to bridge UI controls.

        When the simulator UI moves a slider/toggle that's in our
        ui_to_dsp map, also update the matching DSP attribute and push
        to any subscribed clients.
        """
        super().set_state(key, value)
        mapping = self._ui_to_dsp.get(key)
        if mapping is None:
            return
        tag, attr, idx = mapping
        # Coerce slider strings to floats / toggles to booleans
        if isinstance(value, str):
            if value.lower() in ("true", "false"):
                value = value.lower() == "true"
            else:
                try:
                    value = float(value) if "." in value or attr == "level" else int(value)
                except ValueError:
                    pass
        elif attr in ("mute", "outputMute") and not isinstance(value, bool):
            value = bool(value)
        self._dsp[(tag, attr, idx)] = value
        self._push_subscribers(tag, attr, idx)

    # ── Test hooks (not part of the protocol) ──

    def trigger_external_change(
        self, tag: str, attr: str, idx: int | None, value: Any,
    ) -> None:
        """Force a state change from outside the wire — e.g. an operator
        nudges a knob on the front panel — and push to subscribers.

        Used by the round-trip test to verify the driver reacts to
        unsolicited state changes.
        """
        self._dsp[(tag, attr, idx)] = value
        self._push_subscribers(tag, attr, idx)
