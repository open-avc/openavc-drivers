"""
OpenAVC tvONE CORIOmatrix Driver.

Controls tvONE CORIOmatrix and CORIOmatrix mini modular matrix routers over
the CORIOmax command-line API — plain text on TCP port 10001 (or RS-232 at
115200 8N1).

Protocol source: "tvONE CORIOmatrix Commands", document v2.0.8 (System API
4.5, firmware M405), tvONE. The !Failed failure terminal and command-queue
behavior are documented in the companion "tvONE CORIOmaster Commands"
document (same CORIOmax API family; its legend marks these commands common
across all CORIOmax products).

Wire format:
    Commands are single CRLF-terminated lines. A property read is the bare
    property path ("Slot14.Out1.InsList"); a write is "path = value"; a
    method call ends in parentheses ("Preset.PresetList()"). Every request
    answers zero or more reply lines followed by a terminal line:
    "!Done <echo>" on success or "!Failed <echo>" on failure. Sessions
    start with "login(<user>,<password>)", answered by
    "!Info : User <name> Logged In". Property replies always use the
    long-form path ("Slot14.Out1.InsList = Slot3.In1") even when the
    request used the short S<n>I<n>/S<n>O<n> alias.

Push vs poll: POLL-ONLY. The CORIOmax CLI is strictly request/response —
no notification mechanism is documented for the CORIOmatrix — and the
Constraints page limits the unit to ONE controlling connection at a time,
so polling on this connection sees every change made through it plus
front-panel/web changes on the next cycle.

Why Python (not .avcdriver YAML):
  * Session login must gate the connection — the login( ) command has to be
    sent and its documented success line awaited before anything else, and
    a rejection must surface as a typed authentication fault. YAML's
    on_connect is fire-and-forget and its auth: block only speaks
    prompt-driven Telnet logins (this is the send_login shape tracked in
    the roadmap's auth-extension table).
  * The port roster is device-enumerated: "Slots" reports which slots hold
    cards, and each slot dump reports the card type and its In<n>/Out<n>
    ports. Children are registered from that enumeration (dynamic string
    ids like "s3i1"), not from a hand-typed config list.
  * Reply identities are long-form ("Slot14.Out1.InsList = ...") while the
    child ids and routing syntax use the compact alias form — composing a
    child id from two captured numbers is beyond YAML's child_set.
  * Preset.PresetList() answers one line per preset which must be
    aggregated into a {value,label} picker list for the recall dropdown.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

# Serialized request/response: how long to wait for the !Done / !Failed
# terminal of one request before giving up on it.
REQUEST_TIMEOUT_S = 4.0
LOGIN_TIMEOUT_S = 6.0

# CORIOmatrix frames have 16 slots; the mini has 4. Used only as an
# enumeration bound, not a registered-children cap.
SLOT_MAX = 16

# ── Reply-line patterns (long-form paths, per the command doc examples) ──

_RE_SLOT_LIST = re.compile(r"(?i)^slots\.slot(\d+)\s*=\s*(.+?)\s*$")
_RE_CARDTYPE = re.compile(r"(?i)^(?:slots\.)?slot(\d+)\.cardtype\s*=\s*(.+?)\s*$")
_RE_PORT_STUB = re.compile(r"(?i)^(?:slots\.)?slot(\d+)\.(in|out)(\d+)\s*=\s*<", )
_RE_IN_STATUS = re.compile(r"(?i)^(?:slots\.)?slot(\d+)\.in(\d+)\.status\s*=\s*(.+?)\s*$")
_RE_OUT_STATUS = re.compile(r"(?i)^(?:slots\.)?slot(\d+)\.out(\d+)\.status\s*=\s*(.+?)\s*$")
_RE_INSLIST = re.compile(r"(?i)^(?:slots\.)?slot(\d+)\.out(\d+)\.inslist\s*=\s*(.+?)\s*$")
_RE_CTB = re.compile(r"(?i)^(?:slots\.)?slot(\d+)\.out(\d+)\.cuttoblack\s*=\s*(on|off)\s*$")
_RE_SYS_STATUS = re.compile(r"(?i)^system\.status\s*=\s*(\S+)")
_RE_API_VER = re.compile(r"(?i)^system\.api_version\s*=\s*(.+?)\s*$")
_RE_UNIT_DESC = re.compile(r'(?i)^system\.unit_description\s*=\s*"?(.*?)"?\s*$')
_RE_PRESET_TAKE = re.compile(r"(?i)^(?:routing\.)?preset\.take\s*=\s*(\d+)")
_RE_PRESET_LIST = re.compile(r"(?i)^(?:routing\.)?preset\.presetlist\[(\d+)\]\s*=\s*(.*?)\s*$")
_RE_LOGIN_OK = re.compile(r"(?i)^!info\s*:\s*user\s+.+\s+logged\s+in")
# Routing echo, long or alias form: "!Done Slot3.In1 > Slot14.Out1" /
# "!Done S3I1 > S14O1".
_RE_ROUTE_ECHO = re.compile(
    r"(?i)^!done\s+(?:(?:slots\.)?slot(\d+)\.in(\d+)|s(\d+)i(\d+))\s*>\s*"
    r"(?:(?:slots\.)?slot(\d+)\.out(\d+)|s(\d+)o(\d+))"
)

_RE_TERMINAL = re.compile(r"(?i)^!(done|failed)\b")


def _alias(kind: str, slot: int, port: int) -> str:
    """Compact alias id for a port ('s3i1' / 's14o1')."""
    return f"s{slot}{'i' if kind == 'in' else 'o'}{port}"


def _long_form(alias_id: str) -> str | None:
    """'s3i1' -> 'Slot3.In1'; 's14o2' -> 'Slot14.Out2'. None if malformed."""
    m = re.fullmatch(r"(?i)s(\d+)([io])(\d+)", str(alias_id).strip())
    if not m:
        return None
    word = "In" if m.group(2).lower() == "i" else "Out"
    return f"Slot{int(m.group(1))}.{word}{int(m.group(3))}"


def _normalize_source(value: str) -> str:
    """Normalize an InsList value to compact alias form.

    'Slot3.In1' -> 's3i1'; a comma list normalizes per item; 'NULL' -> ''.
    Unrecognized items pass through untouched.
    """
    value = value.strip()
    if not value or value.upper() == "NULL":
        return ""
    items = []
    for item in value.split(","):
        m = re.fullmatch(r"(?i)(?:slots\.)?slot(\d+)\.in(\d+)", item.strip())
        if m:
            items.append(_alias("in", int(m.group(1)), int(m.group(2))))
        else:
            items.append(item.strip())
    return ",".join(items)


class TvoneCoriomatrixDriver(BaseDriver):
    """tvONE CORIOmatrix / CORIOmatrix mini (CORIOmax CLI, TCP 10001)."""

    DRIVER_INFO = {
        "id": "tvone_coriomatrix",
        "name": "tvONE CORIOmatrix",
        "manufacturer": "tvONE",
        "category": "switcher",
        "version": "1.0.0",
        # String-id children registered from a device-enumerated roster.
        "min_platform_version": "0.19.4",
        "author": "OpenAVC",
        "description": (
            "Controls tvONE CORIOmatrix and CORIOmatrix mini modular matrix "
            "routers over the CORIOmax command-line API (TCP 10001). The "
            "installed input/output cards are discovered from the frame, so "
            "every port appears as a child entity with its routing, signal "
            "status, and cut-to-black state. Includes routing preset recall "
            "and save with a live preset picker."
        ),
        "source_url": "https://tvone.com/filestore/Manuals-CORIO-Products/tvONE%20CORIOmatrix%20Commands-v2.0.8.pdf",
        "tags": ["matrix-switcher", "modular", "coriomatrix", "coriomax", "tvone"],
        "verified": False,
        "simulated": True,
        "protocols": ["coriomax_cli"],
        "ports": [10001],
        "transport": "tcp",
        "discovery": {
            # The CORIOmax CLI requires a login before answering anything
            # documented, so there is no safe active probe; port 10001 plus
            # the vendor string is the available evidence.
            "port_open": [10001],
            "manufacturer_alias": ["tvone", "tv one"],
        },
        "compatible_models": [
            {
                "manufacturer": "tvONE",
                "models": [
                    "CORIOmatrix (C3-340)",
                    "CORIOmatrix mini (C3-310)",
                ],
                "confidence": "untested",
                "notes": (
                    "Both frames speak the same CORIOmax command-line API "
                    "(document v2.0.8, System API 4.5+). The unit accepts "
                    "ONE controlling connection at a time (serial or "
                    "Ethernet) — close CORIOdiscover / CORIOgrapher "
                    "sessions before connecting OpenAVC. Factory login is "
                    "admin / adminpw."
                ),
            },
        ],
        "help": {
            "overview": (
                "CORIOmatrix is tvONE's modular card-frame matrix router — "
                "slots take DVI, HDMI, SDI, and HDBaseT input or output "
                "cards. The driver logs in, discovers which cards are "
                "installed, and models every port as a child entity: "
                "outputs carry their routed source and cut-to-black state, "
                "inputs their signal status. Routing presets stored on the "
                "unit can be recalled from a live dropdown or saved from "
                "the current routing."
            ),
            "setup": (
                "1. Give the unit a static IP (default 192.168.0.10) and "
                "add it here with port 10001.\n"
                "2. Enter the control login — the factory account is "
                "username admin, password adminpw (the password field is "
                "left blank on purpose; type the unit's real password).\n"
                "3. The unit accepts one controller at a time: close any "
                "open CORIOdiscover / web session first.\n"
                "4. Ports are addressed by their factory alias — slot 3 "
                "input 1 is s3i1 — matching what the front panel and web "
                "UI show. Use Refresh from Device after changing cards.\n"
                "5. Keep polling enabled: the session logs out after five "
                "minutes idle, and polling doubles as the keep-alive."
            ),
        },
        "default_config": {
            "host": "",
            "port": 10001,
            "username": "admin",
            "password": "",
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "default": 10001,
                "label": "TCP Port",
                "description": "CORIOmax CLI port — 10001 on every unit.",
            },
            "username": {
                "type": "string",
                "default": "admin",
                "label": "Username",
                "description": "Control account — the factory administrator account is 'admin'.",
            },
            "password": {
                "type": "string",
                "default": "",
                "secret": True,
                "label": "Password",
                "description": "Password for the control account. Factory default is 'adminpw'.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "Refreshes routing, signal status, cut-to-black, and the "
                    "active preset, and keeps the login session alive (the "
                    "unit logs an idle session out after ~5 minutes). Leave "
                    "above 0."
                ),
            },
        },
        "state_variables": {
            "status": {
                "type": "string",
                "label": "System Status",
                "help": "The unit's reported status (System.Status, e.g. Serving).",
            },
            "api_version": {"type": "string", "label": "API Version"},
            "device_name": {
                "type": "string",
                "label": "Device Name",
                "help": "The unit's name (System.Unit_Description).",
            },
            "active_preset": {
                "type": "integer",
                "label": "Active Preset",
                "help": "Last routing preset taken (Preset.Take).",
                "cloud_priority": "high",
            },
            "preset_options": {
                "type": "string",
                "label": "Preset Options",
                "help": "JSON list of the presets stored on the unit — feeds the Recall Preset picker.",
                "cloud_priority": "low",
            },
        },
        "child_entity_types": {
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "string", "max_length": 12},
                "state_variables": {
                    "status": {
                        "type": "string",
                        "label": "Signal Status",
                        "help": "The input's reported status (OK when a source is detected).",
                        "cloud_priority": "high",
                    },
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                },
                "summary_fields": ["status"],
                "label_field": "name",
            },
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "string", "max_length": 12},
                "state_variables": {
                    "source": {
                        "type": "string",
                        "label": "Routed Source",
                        "help": "Alias of the input routed to this output (e.g. s3i1); empty when nothing is routed.",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "cut_to_black": {
                        "type": "boolean",
                        "label": "Cut to Black",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "status": {"type": "string", "label": "Status", "cloud_priority": "low"},
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                },
                "summary_fields": ["source", "cut_to_black"],
                "label_field": "name",
            },
        },
        "device_settings": {
            "device_name": {
                "type": "string",
                "label": "Device Name",
                "help": "Name shown for the unit (up to 32 characters).",
                "state_key": "device_name",
                "default": "",
                "setup": False,
            },
        },
        "quick_actions": ["route", "recall_preset", "refresh"],
        "actions": [
            {"id": "route", "kind": "command", "icon": "route"},
            {"id": "recall_preset", "kind": "command", "icon": "bookmark"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "test_login",
                "kind": "setup",
                "label": "Test Login",
                "icon": "key-round",
                "availability": "always",
            },
        ],
        "commands": {
            "route": {
                "label": "Route Input to Output",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                },
                "help": "Route an input port to an output port (audio follows the unit's audio configuration).",
            },
            "cut_to_black_on": {
                "label": "Cut Output to Black",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                },
                "help": "Blank an output without changing its routing.",
            },
            "cut_to_black_off": {
                "label": "Restore Output from Black",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                },
            },
            "recall_preset": {
                "label": "Recall Preset",
                "params": {
                    "preset": {
                        "type": "integer",
                        "required": True,
                        "label": "Preset",
                        "min": 1,
                        "max": 49,
                        "options_state": "preset_options",
                    },
                },
                "help": "Take a stored routing preset (Preset.Take).",
            },
            "save_preset": {
                "label": "Save Preset",
                "params": {
                    "preset": {
                        "type": "integer",
                        "required": True,
                        "label": "Preset (1-49)",
                        "min": 1,
                        "max": 49,
                    },
                    "name": {
                        "type": "string",
                        "required": False,
                        "label": "Name (optional)",
                        "pattern": "^[A-Za-z0-9_-]{0,19}$",
                    },
                },
                "help": "Save the current routing to a preset slot, optionally naming it (19 alphanumeric characters max, no spaces).",
            },
            "set_device_name": {
                "label": "Set Device Name",
                "params": {
                    "name": {"type": "string", "required": True, "label": "Name"},
                },
                "help": "Set the unit's name (System.Unit_Description, 32 characters max).",
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Re-enumerate the installed cards and re-read all port state.",
            },
            "raw_command": {
                "label": "Send Raw Command",
                "params": {
                    "command": {"type": "string", "required": True, "label": "Command"},
                },
                "help": (
                    "Escape hatch for anything in the CORIOmatrix command "
                    "document, e.g. 'Slot3.In1.HDCP' or "
                    "'System.Fan1_Speed'. Returns the reply lines."
                ),
            },
        },
    }

    # ── Lifecycle ──

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._request_lock = asyncio.Lock()
        self._reply_lines: list[str] = []
        self._terminal_waiter: asyncio.Future | None = None
        self._preset_gather: dict[int, str] | None = None
        # alias id -> (slot, port) for registered children, per type.
        self._known: dict[str, set[str]] = {"input": set(), "output": set()}
        self._poll_cycle = 0
        super().__init__(device_id, config, state, events)

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 10001))

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\n",
            timeout=5.0,
            name=self.device_id,
        )

        try:
            await self._login()
            await self._enumerate_roster()
        except Exception:
            await self._teardown_transport()
            raise

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to CORIOmatrix at {host}:{port}")

        # First full read, then steady-state polling (also the session
        # keep-alive — the unit logs out an idle session).
        await self.poll()
        poll_interval = int(self.config.get("poll_interval", 15))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        await self._teardown_transport()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    async def _teardown_transport(self) -> None:
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._fail_terminal_waiter()

    # ── Login ──

    async def _login(self) -> None:
        """Send login( ) and require the documented confirmation line.

        Success is '!Info : User <name> Logged In'. Any other terminal
        reply is treated as a rejected credential; silence is treated as
        the unit not answering the CLI.
        """
        username = str(self.config.get("username", "admin")).strip() or "admin"
        password = str(self.config.get("password", ""))
        ok, lines = await self._request(
            f"login({username},{password})", timeout=LOGIN_TIMEOUT_S
        )
        if any(_RE_LOGIN_OK.match(ln) for ln in lines):
            return
        if ok is None:
            raise ConnectionError(
                f"[{self.device_id}] No response to login on the CORIOmax "
                f"CLI — check the unit is reachable and no other "
                f"controller holds the connection"
            )
        raise ConnectionError(
            f"[{self.device_id}] Authentication failed — the unit rejected "
            f"user '{username}'. Factory default is admin / adminpw."
        )

    # ── Serialized request/response ──

    async def _request(
        self, line: str, timeout: float = REQUEST_TIMEOUT_S
    ) -> tuple[bool | None, list[str]]:
        """Send one CLI line and gather its reply.

        Returns (ok, reply_lines): ok is True on '!Done', False on
        '!Failed', None on timeout. The terminal line is included in
        reply_lines. Requests are serialized — the CLI answers in order on
        a single connection.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._request_lock:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._reply_lines = []
            self._terminal_waiter = fut
            try:
                await self.transport.send((line + "\r\n").encode("utf-8"))
                try:
                    ok = await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    return None, list(self._reply_lines)
                return ok, list(self._reply_lines)
            finally:
                if self._terminal_waiter is fut:
                    self._terminal_waiter = None

    def _fail_terminal_waiter(self) -> None:
        waiter = self._terminal_waiter
        if waiter is not None and not waiter.done():
            waiter.set_exception(ConnectionError("connection closed"))
        self._terminal_waiter = None

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        line = data.decode("utf-8", errors="replace").strip("\r\n").strip()
        if not line:
            return
        self._apply_line(line)
        if self._terminal_waiter is not None:
            self._reply_lines.append(line)
            if self._terminal_waiter.done():
                return
            m = _RE_TERMINAL.match(line)
            if m:
                self._terminal_waiter.set_result(m.group(1).lower() == "done")
            elif _RE_LOGIN_OK.match(line):
                # login( ) answers the documented '!Info : ... Logged In'
                # confirmation with no !Done terminal — treat it as one.
                self._terminal_waiter.set_result(True)

    def _apply_line(self, line: str) -> None:
        """Fold one reply line into device / child state. All state flows
        through here, whether the line arrived from a poll read, a command
        echo, or a raw_command reply.

        A '!Done <echo>' terminal for a property WRITE restates the applied
        value ('!Done Slot14.Out1.CutToBlack = On'), so the prefix is
        stripped and the remainder run through the same property matchers.
        """
        m = _RE_ROUTE_ECHO.match(line)
        if m:
            in_slot = int(m.group(1) or m.group(3))
            in_port = int(m.group(2) or m.group(4))
            out_slot = int(m.group(5) or m.group(7))
            out_port = int(m.group(6) or m.group(8))
            self._set_output(out_slot, out_port,
                             {"source": _alias("in", in_slot, in_port)})
            return
        line = re.sub(r"(?i)^!done\s+", "", line)
        m = _RE_INSLIST.match(line)
        if m:
            self._set_output(int(m.group(1)), int(m.group(2)),
                             {"source": _normalize_source(m.group(3))})
            return
        m = _RE_CTB.match(line)
        if m:
            self._set_output(int(m.group(1)), int(m.group(2)),
                             {"cut_to_black": m.group(3).lower() == "on"})
            return
        m = _RE_OUT_STATUS.match(line)
        if m:
            self._set_output(int(m.group(1)), int(m.group(2)),
                             {"status": m.group(3)})
            return
        m = _RE_IN_STATUS.match(line)
        if m:
            alias = _alias("in", int(m.group(1)), int(m.group(2)))
            if alias in self._known["input"]:
                self.set_child_state_batch("input", alias, {"status": m.group(3)})
            return
        m = _RE_PRESET_TAKE.match(line)
        if m:
            self.set_state("active_preset", int(m.group(1)))
            return
        m = _RE_PRESET_LIST.match(line)
        if m and self._preset_gather is not None:
            # "PresetList[3]=top_and_bottom,Canvas1,1000" — name is field 1.
            name = m.group(2).split(",")[0].strip()
            self._preset_gather[int(m.group(1))] = name
            return
        m = _RE_SYS_STATUS.match(line)
        if m:
            self.set_state("status", m.group(1))
            return
        m = _RE_API_VER.match(line)
        if m:
            self.set_state("api_version", m.group(1))
            return
        m = _RE_UNIT_DESC.match(line)
        if m:
            self.set_state("device_name", m.group(1))
            return

    def _set_output(self, slot: int, port: int, updates: dict[str, Any]) -> None:
        alias = _alias("out", slot, port)
        if alias in self._known["output"]:
            self.set_child_state_batch("output", alias, updates)

    # ── Roster enumeration ──

    async def _enumerate_roster(self) -> None:
        """Discover installed cards and register a child per port.

        'Slots' answers one line per slot ('Slots.Slot4 = NO CARD' for the
        empty ones); each installed slot's dump lists its ports as
        'Slot3.In1 = <...>' stubs.
        """
        ok, lines = await self._request("Slots")
        if not ok:
            raise ConnectionError(
                f"[{self.device_id}] The unit did not answer the Slots "
                f"enumeration — cannot discover installed cards"
            )
        installed: list[int] = []
        for ln in lines:
            m = _RE_SLOT_LIST.match(ln)
            if m and "NO CARD" not in m.group(2).upper():
                installed.append(int(m.group(1)))

        found: dict[str, set[str]] = {"input": set(), "output": set()}
        for slot in installed:
            ok, lines = await self._request(f"Slot{slot}")
            if not ok:
                continue
            for ln in lines:
                m = _RE_PORT_STUB.match(ln)
                if not m:
                    continue
                kind = "in" if m.group(2).lower() == "in" else "out"
                ctype = "input" if kind == "in" else "output"
                alias = _alias(kind, int(m.group(1)), int(m.group(3)))
                found[ctype].add(alias)

        for ctype in ("input", "output"):
            want = found[ctype]
            have = set(self.list_children(ctype))
            for alias in sorted(want - have):
                self.register_child(ctype, alias, initial_state={"name": alias})
            for alias in have - want:
                self.deregister_child(ctype, alias)
            self._known[ctype] = want

        log.info(
            f"[{self.device_id}] Enumerated {len(found['input'])} inputs / "
            f"{len(found['output'])} outputs across slots {installed}"
        )

    async def refresh_children(self) -> dict[str, Any]:
        await self._enumerate_roster()
        await self.poll()
        return {
            "inputs": len(self._known["input"]),
            "outputs": len(self._known["output"]),
        }

    # ── Polling ──

    async def poll(self) -> None:
        """Read every port dump + the active preset; system info and the
        preset list refresh on the first and every 6th cycle.

        Transport failures propagate so the platform's poll loop can flip
        the device offline (never-offline guard).
        """
        for ctype in ("input", "output"):
            for alias in sorted(self._known[ctype]):
                await self._request(_long_form(alias) or alias)
        await self._request("Preset.Take")
        if self._poll_cycle % 6 == 0:
            await self._request("System.Status")
            await self._request("System.API_Version")
            await self._request("System.Unit_Description")
            await self._refresh_preset_list()
        self._poll_cycle += 1

    async def _refresh_preset_list(self) -> None:
        self._preset_gather = {}
        try:
            ok, _lines = await self._request("Preset.PresetList()")
            if ok:
                options = [
                    {"value": str(pid), "label": f"{pid}: {name}" if name else str(pid)}
                    for pid, name in sorted(self._preset_gather.items())
                ]
                self.set_state("preset_options", json.dumps(options))
        finally:
            self._preset_gather = None

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        if command == "route":
            src = _long_form(params.get("input"))
            dst = _long_form(params.get("output"))
            if not src or not dst:
                raise ValueError(
                    f"route: input/output must be port aliases like s3i1 "
                    f"(got {params.get('input')!r} > {params.get('output')!r})"
                )
            ok, lines = await self._request(f"{src} > {dst}")
            self._warn_failed(command, ok, lines)
            return ok

        if command in ("cut_to_black_on", "cut_to_black_off"):
            dst = _long_form(params.get("output"))
            if not dst:
                raise ValueError(f"{command}: output must be an alias like s14o1")
            value = "On" if command.endswith("_on") else "Off"
            ok, lines = await self._request(f"{dst}.CutToBlack = {value}")
            self._warn_failed(command, ok, lines)
            return ok

        if command == "recall_preset":
            ok, lines = await self._request(f"Preset.Take = {int(params['preset'])}")
            self._warn_failed(command, ok, lines)
            # Routing changed wholesale — re-read the outputs.
            for alias in sorted(self._known["output"]):
                await self._request(_long_form(alias) or alias)
            return ok

        if command == "save_preset":
            pid = int(params["preset"])
            name = str(params.get("name") or "").strip()
            ok, lines = await self._request(f"Preset.Read = {pid}")
            self._warn_failed(command, ok, lines)
            if name:
                ok, lines = await self._request(f"Preset.NameRead = {name}")
                self._warn_failed(command, ok, lines)
            ok, lines = await self._request("Preset.SaveRead()")
            self._warn_failed(command, ok, lines)
            await self._refresh_preset_list()
            return ok

        if command == "set_device_name":
            name = str(params.get("name", "")).strip()[:32]
            ok, lines = await self._request(f'System.Unit_Description = "{name}"')
            self._warn_failed(command, ok, lines)
            await self._request("System.Unit_Description")
            return ok

        if command == "refresh":
            await self.refresh_children()
            return True

        if command == "raw_command":
            ok, lines = await self._request(str(params.get("command", "")).strip())
            return "\n".join(lines)

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    def _warn_failed(self, command: str, ok: bool | None, lines: list[str]) -> None:
        if ok is False:
            log.warning(
                f"[{self.device_id}] {command}: unit answered "
                f"{lines[-1] if lines else '!Failed'}"
            )
        elif ok is None:
            log.warning(f"[{self.device_id}] {command}: no reply from unit")

    # ── Device settings ──

    async def set_device_setting(self, setting: str, value: Any) -> None:
        if setting == "device_name":
            await self.send_command("set_device_name", {"name": str(value)})
            return
        raise ValueError(f"Unknown device setting: {setting}")

    # ── Setup action ──

    async def run_setup_action(
        self, action_id: str, params: dict[str, Any], progress: Any
    ) -> dict[str, Any]:
        if action_id != "test_login":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 10001))
        username = str(self.config.get("username", "admin")).strip() or "admin"
        password = str(self.config.get("password", ""))
        if not host:
            raise ValueError("No IP address configured")

        await progress(f"Connecting to {host}:{port}…", 20)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach a CORIOmax CLI on {host}:{port} ({exc})."
            ) from exc

        try:
            await progress("Logging in…", 55)
            writer.write(f"login({username},{password})\r\n".encode())
            await writer.drain()
            deadline = asyncio.get_running_loop().time() + LOGIN_TIMEOUT_S
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ConnectionError(
                        "No login confirmation from the unit — is another "
                        "controller connected? The CORIOmatrix accepts one "
                        "control connection at a time."
                    )
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                if not raw:
                    raise ConnectionError("The unit closed the connection during login.")
                line = raw.decode("utf-8", errors="replace").strip()
                if _RE_LOGIN_OK.match(line):
                    break
                if _RE_TERMINAL.match(line):
                    raise ConnectionError(
                        f"The unit rejected user '{username}' ({line}). "
                        f"Factory default is admin / adminpw."
                    )
            await progress("Login OK — reading unit info…", 80)
            writer.write(b"System.API_Version\r\n")
            await writer.drain()
            api = ""
            try:
                for _ in range(4):
                    raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    line = raw.decode("utf-8", errors="replace").strip()
                    m = _RE_API_VER.match(line)
                    if m:
                        api = m.group(1)
                    if _RE_TERMINAL.match(line):
                        break
            except asyncio.TimeoutError:
                pass
            return {
                "success": True,
                "message": (
                    f"Logged in to {host} as {username}"
                    + (f" (API {api})" if api else "")
                ),
            }
        finally:
            writer.close()
