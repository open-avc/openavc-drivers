"""
OpenAVC Panasonic PT-Series Projector Driver.

Controls Panasonic PT-MZ and PT-RZ professional installation projectors
via Panasonic's "Protocol 2" / NTCONTROL TCP command channel. Default
TCP port is 1024 (configurable on the projector under
``[Network] -> [Network Control] -> [Command Port]``).

Protocol shape (Protocol 2 / NTCONTROL):
    1. Open TCP. Projector sends a one-line greeting:
         ``NTCONTROL <mode> <random8>(CR)``    (Protected mode, mode=1)
         ``NTCONTROL 0(CR)``                    (Non-protected mode)
    2. Protected mode: compute MD5 of
       ``"<username>:<password>:<random8>"``. The 32-char lowercase hex
       digest is prefixed to every command issued during this TCP
       session — auth is per-session, not per-command.
    3. Each command line is ``<prefix>00<COMMAND>(CR)``. ``00`` is the
       literal two ASCII zeros that prefix every Protocol 2 frame; the
       projector echoes the same shape on success: ``00<RESPONSE>(CR)``.
    4. Errors come back as bare tokens (``ERR1`` / ``ERR2`` / ``ERR3`` /
       ``ERR4`` / ``ERR5`` / ``ERRA`` / ``ER401``) terminated with
       (CR). ``ERRA`` specifically means the password / hash is wrong.
    5. After 30 s of idle the projector closes the socket. Keeping the
       poll interval well below that (default 15 s) avoids the close.
       The platform's TCP transport reconnects automatically anyway.

Push vs poll:
    NTCONTROL is request/response only. The projector has no
    subscription / notification channel — polling is the only way to
    track power transitions, lamp hours, etc. Documented choice.

Auth shape and why this is Python:
    NTCONTROL uses an MD5 *session* challenge — the digest of
    ``user:pass:random`` is prefixed on every command for the whole
    session. ``ConfigurableDriver``'s declarative ``auth:`` block
    today only knows ``type: telnet_login`` (prompt-driven plain-text
    login). The shape doesn't fit, so the driver is Python.

    This is a third concrete example of the broader "hashed challenge"
    family alongside ``sony_vpl`` (sha256, session) and
    ``pjlink_class1`` (md5, per_command). When a fourth driver in any
    variant lands, the right next move is to ship a generic
    ``auth.type: hashed_challenge`` extension to ConfigurableDriver
    parameterised on algorithm + scope + format string, and migrate
    these three to YAML.

Models covered:
    Panasonic uses the same Protocol 2 command set across the entire
    PT-MZ (LCD) and PT-RZ (1-chip / 3-chip DLP) installation lines —
    PT-MZ20K / MZ17K / MZ14K / MZ11K, PT-MZ16K / MZ13K / MZ10K, PT-MZ880
    series, PT-MZ770 / MZ670 series, PT-RZ990, PT-RZ970 / RW930 / RX110,
    PT-RZ690, PT-RZ660 / RW620, PT-RZ570, PT-RCQ10 / RCQ80, PT-RZ34K /
    RZ31K / RZ24K / RZ21K, PT-RZ16K / RZ12K, PT-RZ575 / RZ475, and
    others. The exact set of inputs varies by chassis — the driver
    exposes the union; unsupported inputs return ``ERR2`` on the
    projector and are logged at debug.

Sources:
    LAN Control Protocol (mechanism + auth):
      https://docs.connect.panasonic.com/prodisplays/support/download/pdf/LAN_Protocol_exp.pdf
    Control Command List (PT-MZ20K series, used as the canonical
    command surface — PT-RZ command codes are a strict superset):
      https://bizpartner.panasonic.net/public/ppr/file_view/210656
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)


# Universal Panasonic input codes. The projector's `IIS:<code>` setter
# accepts any of these; chassis without a given physical input return
# ``ERR2``. See PT-MZ20K and PT-RZ990 command lists for per-model
# coverage.
INPUT_VALUES = [
    "hdmi1",
    "hdmi2",
    "computer1",
    "computer2",
    "video",
    "svideo",
    "dvi",
    "sdi1",
    "sdi2",
    "digital_link",
]

# Friendly name -> protocol code.
INPUT_TO_CODE = {
    "hdmi1": "HD1",
    "hdmi2": "HD2",
    "computer1": "RG1",
    "computer2": "RG2",
    "video": "VID",
    "svideo": "SVD",
    "dvi": "DV1",
    "sdi1": "SD1",
    "sdi2": "SD2",
    "digital_link": "DL1",
}

# Reverse of the above for `QIN` responses.
CODE_TO_INPUT = {v: k for k, v in INPUT_TO_CODE.items()}

# Power query response codes.
POWER_QPW_MAP = {"000": "off", "001": "on"}


class PanasonicPTDriver(BaseDriver):
    """Panasonic PT-MZ / PT-RZ projector driver (NTCONTROL / Protocol 2)."""

    DRIVER_INFO = {
        "id": "panasonic_pt",
        "name": "Panasonic PT-MZ / PT-RZ Projector",
        "manufacturer": "Panasonic",
        "category": "projector",
        "version": "1.1.0",
        "author": "OpenAVC",
        "description": (
            "Controls Panasonic PT-MZ (LCD) and PT-RZ (DLP) "
            "professional installation projectors via the NTCONTROL "
            "TCP protocol (Protocol 2). Power, input, shutter / AV "
            "mute, freeze, and operating-hours readout. Supports "
            "Panasonic's MD5 session-challenge authentication so the "
            "projector keeps its default Web Control admin password."
        ),
        "source_url": "https://docs.connect.panasonic.com/prodisplays/support/download/pdf/LAN_Protocol_exp.pdf",
        "tags": ["projector", "panasonic", "ntcontrol", "pt", "installation"],
        "verified": False,
        "simulated": True,
        "protocols": ["ntcontrol"],
        "ports": [1024],
        "transport": "tcp",
        "discovery": {"manual_only": True},
        "compatible_models": [
            {
                "manufacturer": "Panasonic",
                "models": [
                    # PT-MZ (LCD) — high-end 20K / 17K / 14K / 11K class
                    "PT-MZ20K",
                    "PT-SMZ20KC",
                    "PT-MZ17K",
                    "PT-SMZ17KC",
                    "PT-MZ14K",
                    "PT-MZ11K",
                    "PT-SMZ11KC",
                    "PT-MZ16K",
                    "PT-MZ13K",
                    "PT-MZ10K",
                    # PT-MZ mid / lower-mid
                    "PT-MZ880",
                    "PT-MZ780",
                    "PT-MZ680",
                    "PT-MZ770",
                    "PT-MZ670",
                    "PT-MZ570",
                    "PT-MZ16K",
                    # PT-RZ (1-chip DLP)
                    "PT-RZ990",
                    "PT-RZ970",
                    "PT-RW930",
                    "PT-RX110",
                    "PT-RZ690",
                    "PT-RZ660",
                    "PT-RW620",
                    "PT-RZ570",
                    "PT-RZ575",
                    "PT-RZ475",
                    # PT-RZ (3-chip DLP, large venue)
                    "PT-RZ34K",
                    "PT-RZ31K",
                    "PT-RZ24K",
                    "PT-RZ21K",
                    "PT-RZ16K",
                    "PT-RZ12K",
                    # PT-RCQ (4K phosphor laser)
                    "PT-RCQ10",
                    "PT-RCQ80",
                ],
                "confidence": "untested",
                "notes": (
                    "Protocol 2 / NTCONTROL is the same on every "
                    "Panasonic professional projector — the universal "
                    "command surface (power, input select, shutter, "
                    "freeze, operating hours) is consistent. Per-model "
                    "variation is which physical inputs are present; "
                    "the driver exposes the union and the projector "
                    "returns ``ERR2`` for inputs it doesn't have, "
                    "which the driver logs at debug."
                ),
            }
        ],
        "help": {
            "overview": (
                "Panasonic NTCONTROL (Protocol 2) is the TCP control "
                "channel shared across all Panasonic professional "
                "projectors. It runs on TCP 1024 by default with "
                "optional MD5 challenge-response authentication tied "
                "to the projector's Web Control admin account."
            ),
            "setup": (
                "1. Connect the projector to the network and assign a "
                "static IP.\n"
                "2. In the projector's main menu, set [Network] -> "
                "[Network Control] -> [Command Control] = ON. The "
                "default command port is 1024.\n"
                "3. Open the projector's Web Control page in a "
                "browser. Set or change the Administrator-Authorized "
                "password (default username is ``admin1``, default "
                "password is ``panasonic``).\n"
                "4. In OpenAVC, enter the projector's IP and the "
                "Web Control admin username and password. If you "
                "left the password blank in the projector's web UI "
                "(non-protected mode), leave the password field "
                "empty here too."
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
                    "verify under [Network] -> [Network Control] -> "
                    "[Command Port] on the projector."
                ),
            },
            "username": {
                "type": "string",
                "default": "admin1",
                "label": "Web Admin Username",
                "description": (
                    "Web Control Administrator username. Panasonic "
                    "factory default is ``admin1``."
                ),
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "Web Admin Password",
                "secret": True,
                "description": (
                    "Web Control Administrator password set in the "
                    "projector's web UI. Leave blank if the projector "
                    "is in non-protected mode (no password set)."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "NTCONTROL has no push notifications. Polling "
                    "below 30 s also keeps the TCP socket from being "
                    "idle-closed by the projector. Set to 0 to "
                    "disable polling."
                ),
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["off", "on"],
                "label": "Power State",
            },
            "input": {
                "type": "string",
                "label": "Input",
            },
            "mute_video": {
                "type": "boolean",
                "label": "Shutter (AV Mute)",
            },
            "freeze": {
                "type": "boolean",
                "label": "Freeze",
            },
            "operating_hours": {
                "type": "integer",
                "label": "Operating Hours",
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
                    "subset of these — picking an unsupported value "
                    "returns ERR2 on the projector."
                ),
            },
            "mute_video": {"label": "Shutter / AV Mute On", "params": {}},
            "unmute_video": {"label": "Shutter / AV Mute Off", "params": {}},
            "freeze_on": {"label": "Freeze On", "params": {}},
            "freeze_off": {"label": "Freeze Off", "params": {}},
            "raw_command": {
                "label": "Send Raw NTCONTROL Command",
                "params": {
                    "command": {
                        "type": "string",
                        "required": True,
                        "help": (
                            "Bare command body without the ``00`` "
                            "prefix or terminator, e.g. ``QPW`` or "
                            "``IIS:HD1``."
                        ),
                    },
                },
                "help": (
                    "Escape hatch for NTCONTROL commands not surfaced "
                    "as named commands."
                ),
            },
            "refresh": {"label": "Refresh Status", "params": {}},
        },
    }

    # Match the connect-time greeting line (with or without the leading
    # space the spec inserts between fields).
    _GREETING_RE = re.compile(
        r"^NTCONTROL\s+(?P<mode>[01])(?:\s+(?P<random>[0-9A-Fa-f]{8}))?\s*$"
    )

    # Tokens the projector returns as bare error responses (no ``00``
    # prefix). ``ER401`` is a software-stack error from newer firmware.
    _ERROR_TOKENS = {"ERR1", "ERR2", "ERR3", "ERR4", "ERR5", "ERRA", "ER401"}

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state,
        events,
    ) -> None:
        self._auth_prefix = ""
        self._auth_done = asyncio.Event()
        self._auth_failed = False
        self._pending_queries: list[str] = []
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ──

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 1024))

        self._auth_done.clear()
        self._auth_prefix = ""
        self._auth_failed = False
        self._pending_queries.clear()

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\r",
            timeout=5.0,
            name=self.device_id,
        )

        # Wait for the projector's NTCONTROL greeting and auth setup.
        try:
            await asyncio.wait_for(self._auth_done.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            await self.transport.close()
            self.transport = None
            raise ConnectionError(
                f"[{self.device_id}] No NTCONTROL greeting received "
                f"from {host}:{port} within 8s"
            )

        if self._auth_failed:
            await self.transport.close()
            self.transport = None
            raise ConnectionError(
                f"[{self.device_id}] NTCONTROL authentication failed "
                "— check the Web Control admin username and password"
            )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(
            f"[{self.device_id}] Connected to Panasonic PT projector "
            f"at {host}:{port}"
        )

        # Initial status sweep so the UI populates immediately.
        try:
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

        poll_interval = int(self.config.get("poll_interval", 15))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self._auth_done.clear()
        self._auth_prefix = ""
        self._auth_failed = False
        self._pending_queries.clear()
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    # ── Sending ──

    async def _send_ntcontrol(self, body: str) -> None:
        """Send a NTCONTROL command body. The session auth prefix (if
        any), the literal ``00`` framing pair, and the trailing CR are
        added automatically.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        line = f"{self._auth_prefix}00{body}\r".encode("ascii")
        await self.transport.send(line)

    async def _send_query(self, body: str, name: str) -> None:
        # NTCONTROL responses come back in order without echoing the
        # query, so we queue ``name`` and pop it when a response
        # arrives. Same trick the Sony VPL driver uses.
        self._pending_queries.append(name)
        await self._send_ntcontrol(body)

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}
        followup: tuple[str, str] | None = None  # (body, response-name)

        if command == "power_on":
            await self._send_ntcontrol("PON")
            followup = ("QPW", "power")
        elif command == "power_off":
            await self._send_ntcontrol("POF")
            followup = ("QPW", "power")
        elif command == "set_input":
            name = str(params.get("input", "")).strip().lower()
            code = INPUT_TO_CODE.get(name)
            if code is None:
                log.warning(
                    f"[{self.device_id}] Unknown input: {name!r}"
                )
                return
            await self._send_ntcontrol(f"IIS:{code}")
            followup = ("QIN", "input")
        elif command == "mute_video":
            await self._send_ntcontrol("OSH:1")
            followup = ("QSH", "mute_video")
        elif command == "unmute_video":
            await self._send_ntcontrol("OSH:0")
            followup = ("QSH", "mute_video")
        elif command == "freeze_on":
            await self._send_ntcontrol("OFZ:1")
            followup = ("QFZ", "freeze")
        elif command == "freeze_off":
            await self._send_ntcontrol("OFZ:0")
            followup = ("QFZ", "freeze")
        elif command == "raw_command":
            body = str(params.get("command", "")).strip()
            if body:
                await self._send_ntcontrol(body)
        elif command == "refresh":
            await self.poll()
            return
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            return

        if followup is not None:
            body, name = followup
            # Brief pause so the setter's ack arrives before we issue
            # the follow-up query — keeps responses paired correctly.
            await asyncio.sleep(0.05)
            await self._send_query(body, name)

    # ── Polling ──

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return
        try:
            await self._send_query("QPW", "power")
            # Other queries only return meaningful values when the lamp
            # is on (the projector returns ``ERR3`` otherwise, which
            # we log at debug).
            power = self.get_state("power")
            if power == "on":
                await self._send_query("QIN", "input")
                await self._send_query("QSH", "mute_video")
                await self._send_query("QFZ", "freeze")
            await self._send_query("Q$S", "operating_hours")
        except ConnectionError:
            log.warning(
                f"[{self.device_id}] Poll failed — not connected"
            )

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        line = data.decode("ascii", errors="ignore").strip()
        if not line:
            return

        # Auth phase: first line is the NTCONTROL greeting; we hash
        # (or not) and signal the connect path to proceed.
        if not self._auth_done.is_set():
            self._handle_greeting(line)
            return

        # Bare error tokens (ERRA, ERR2, ER401, …) — no ``00`` prefix.
        if line in self._ERROR_TOKENS:
            self._handle_error_token(line)
            return

        # Normal Protocol-2 response: ``00<body>``. Strip the framing
        # pair and dispatch.
        body = line[2:] if line.startswith("00") else line
        self._dispatch_response(body)

    def _handle_greeting(self, line: str) -> None:
        m = self._GREETING_RE.match(line)
        if not m:
            log.warning(
                f"[{self.device_id}] Unrecognized greeting: {line!r}"
            )
            self._auth_failed = True
            self._auth_done.set()
            return

        mode = m.group("mode")
        if mode == "0":
            # Non-protected — no hash needed.
            self._auth_prefix = ""
            self.set_state("auth_required", False)
            log.info(
                f"[{self.device_id}] NTCONTROL non-protected mode "
                "(no password required)"
            )
            self._auth_done.set()
            return

        # Protected mode — compute MD5(username:password:random).
        random_token = m.group("random") or ""
        username = self.config.get("username", "admin1") or "admin1"
        password = self.config.get("password", "") or ""
        digest = hashlib.md5(
            f"{username}:{password}:{random_token}".encode(
                "ascii", errors="ignore"
            )
        ).hexdigest()
        self._auth_prefix = digest
        self.set_state("auth_required", True)
        log.info(
            f"[{self.device_id}] NTCONTROL protected mode — MD5 "
            "session prefix computed"
        )
        # The projector confirms or rejects the hash on the next
        # command; success here is provisional. We open the gate so
        # the connect path can proceed and issue the first poll.
        self._auth_done.set()

    def _handle_error_token(self, token: str) -> None:
        # Pop the pending query (if any) so we stay aligned with future
        # responses. The error itself is usually benign (ERR3 during
        # cooldown / warmup, ERR2 for an unsupported input on this
        # chassis) — only ERRA is fatal.
        pending = (
            self._pending_queries.pop(0)
            if self._pending_queries
            else None
        )
        if token == "ERRA":
            log.error(
                f"[{self.device_id}] NTCONTROL auth rejected — check "
                "the Web Control admin username and password"
            )
            self._auth_failed = True
            return
        if token == "ER401":
            log.error(
                f"[{self.device_id}] Projector reported processing "
                f"error on {pending or 'last command'}"
            )
            return
        # ERR1 (undefined), ERR2 (param out of range), ERR3 (busy /
        # unavailable period), ERR4 (timeout / unavailable), ERR5
        # (invalid length) are all things the projector decides on the
        # fly — they don't indicate a driver bug, just a momentary
        # state mismatch.
        log.debug(
            f"[{self.device_id}] NTCONTROL {token} on "
            f"{pending or 'last command'}"
        )

    def _dispatch_response(self, body: str) -> None:
        pending = (
            self._pending_queries.pop(0)
            if self._pending_queries
            else None
        )

        # Setter acknowledgements echo the command, e.g. ``PON``,
        # ``OSH:1``, ``IIS:HD1``. They have no pending entry because
        # we don't queue setters — bail out quietly.
        if pending is None:
            log.debug(
                f"[{self.device_id}] Ack: {body!r}"
            )
            return

        if pending == "power":
            mapped = POWER_QPW_MAP.get(body, body)
            self.set_state("power", mapped)
            return

        if pending == "input":
            # The projector returns the protocol code (e.g. ``HD1``);
            # surface our friendly name so the UI is consistent with
            # what the user picked from the input dropdown.
            mapped = CODE_TO_INPUT.get(body, body.lower())
            self.set_state("input", mapped)
            return

        if pending == "mute_video":
            self.set_state("mute_video", body == "1")
            return

        if pending == "freeze":
            self.set_state("freeze", body == "1")
            return

        if pending == "operating_hours":
            # Q$S responses look like "00100" (leading zeros, plain
            # integer hours).
            try:
                self.set_state("operating_hours", int(body))
            except ValueError:
                log.debug(
                    f"[{self.device_id}] Unparseable Q$S: {body!r}"
                )
            return

        log.debug(
            f"[{self.device_id}] Unmatched response {body!r} "
            f"(pending={pending!r})"
        )

    # ── Disconnect ──

    def _handle_transport_disconnect(self) -> None:
        self._connected = False
        self._auth_done.clear()
        self._auth_prefix = ""
        self._pending_queries.clear()
        self.set_state("connected", False)
        log.warning(f"[{self.device_id}] Connection lost")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self.events.emit(
                    f"device.disconnected.{self.device_id}"
                )
            )
        except RuntimeError:
            pass
