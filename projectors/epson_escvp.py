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
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.frame_parsers import CallableFrameParser
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)


# 16-byte ESC/VP.net handshake preamble (sent by client).
HANDSHAKE_REQUEST = b"ESC/VP.net\x10\x03\x00\x00\x00\x00"

# Expected projector reply on successful handshake. The byte at offset
# 14 is the status; ``0x20`` means "ok". Other values indicate auth
# required (Epson supports an optional password over ESC/VP.net) or
# version mismatch — both rejected here as connection failures.
HANDSHAKE_REPLY_OK = b"ESC/VP.net\x10\x03\x00\x00\x20\x00"
HANDSHAKE_LEN = 16


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


# Common Epson source codes. The full list is model-dependent (see
# Epson's "ESC/VP21 all commands list" attached to the protocol PDF);
# this set covers HDMI / HDBaseT / Computer / USB / LAN inputs found
# on the vast majority of currently-shipping units. Users can pick a
# code outside this list with the raw_command escape hatch.
SOURCE_CODES = {
    "Computer1": "11",
    "Computer2": "21",
    "DVI": "A0",
    "HDMI1": "30",
    "HDMI2": "A0",
    "HDBaseT": "80",
    "Component": "14",
    "S-Video": "42",
    "Video": "41",
    "USB Display": "53",
    "USB-A": "52",
    "LAN": "56",
    "Wireless LAN": "57",
}

# Aspect codes (subset of values that are widely supported).
ASPECT_CODES = {
    "Normal": "00",
    "4:3": "10",
    "16:9": "20",
    "Auto": "30",
    "Full": "40",
    "Zoom": "50",
    "Native": "60",
}

# Color modes.
COLOR_MODES = {
    "sRGB": "01",
    "Presentation": "04",
    "Theatre": "05",
    "Dynamic": "06",
    "Sports": "08",
    "Custom": "10",
    "Black Board": "11",
    "White Board": "12",
    "Photo": "14",
}

# Common KEY codes (decimal-as-hex on the wire — Epson encodes the
# remote IR scancode as a two-digit hex number). These are the keys
# universally available across business and installation projectors.
KEY_CODES = {
    "Power On": "04",
    "Power Off": "05",
    "Menu": "1B",
    "Up": "35",
    "Down": "36",
    "Left": "37",
    "Right": "38",
    "Enter": "16",
    "Esc": "05",
    "Source Search": "31",
    "Mute": "3E",
    "Freeze": "32",
    "Aspect": "F4",
    "Color Mode": "F5",
    "Volume Up": "56",
    "Volume Down": "57",
    "Auto": "C7",
}


class EpsonEscVpDriver(BaseDriver):
    """Epson ESC/VP21 over ESC/VP.net (TCP 3629)."""

    DRIVER_INFO = {
        "id": "epson_escvp",
        "name": "Epson Projector (ESC/VP21)",
        "manufacturer": "Epson",
        "category": "projector",
        "version": "1.3.1",
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
            # Epson projectors also support PJLink Class 2 for vendor-
            # neutral discovery; the generic pjlink_class1 driver claims
            # that signal. Surface this brand-specific driver as a
            # candidate when the PJLink response identifies the unit as
            # an Epson — picks up "EPSON" in firmware-released MNFR
            # strings and the legacy "Seiko Epson" form.
            "manufacturer_alias": ["EPSON", "Seiko Epson"],
        },
        "compatible_models": [
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
                "on TCP port 3629 — the driver handles the binary "
                "handshake transparently. Most modern Epson projectors "
                "respond to PJLink as well, but the native ESC/VP21 "
                "surface gives access to color mode, aspect ratio, "
                "freeze, image-quality numerics, and the full virtual "
                "remote keypad — none of which PJLink covers."
            ),
            "setup": (
                "1. Connect the projector to the network and assign a "
                "static IP.\n"
                "2. In the projector's network menu, enable 'Network "
                "Standby' (or 'Standby Mode = Communication On' on "
                "older units) so the projector responds to network "
                "commands while powered off.\n"
                "3. Confirm 'Control Communications' (some menus call "
                "this 'PJLink' or 'ESC/VP.net') is enabled.\n"
                "4. Optional: if the projector has an ESC/VP.net "
                "password set in the web setup, this driver does not "
                "currently send a password — disable it for control "
                "use, or use the device's web UI for one-off setup."
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
            "color_mode": {
                "type": "string",
                "label": "Color Mode (hex code)",
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
        self._handshake_ok: bool | None = None
        # Pending query queue — see ``_send_query`` / ``_dispatch_response``.
        self._pending_queries: list[str] = []
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ──

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 3629))
        delay = float(self.config.get("inter_command_delay", 0.1))

        self._handshake_complete = False
        self._handshake_event.clear()
        self._handshake_ok = None
        self._pending_queries.clear()

        parser = CallableFrameParser(self._parse_frame)
        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=None,
            frame_parser=parser,
            inter_command_delay=delay,
            timeout=5.0,
            name=self.device_id,
        )

        # Send the handshake immediately and wait for the reply.
        await self.transport.send(HANDSHAKE_REQUEST)
        try:
            await asyncio.wait_for(self._handshake_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            await self.transport.close()
            self.transport = None
            raise ConnectionError(
                f"[{self.device_id}] No ESC/VP.net handshake reply from "
                f"{host}:{port} within 5s"
            )

        if not self._handshake_ok:
            await self.transport.close()
            self.transport = None
            raise ConnectionError(
                f"[{self.device_id}] ESC/VP.net handshake rejected by projector"
            )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(
            f"[{self.device_id}] Connected to Epson projector at {host}:{port}"
        )

        # Initial sweep so the UI populates. Issue PWR? first and let
        # the response settle before the rest of the poll fans out —
        # the conditional sub-queries in poll() depend on power_state.
        try:
            await self._send_query("power", "PWR?")
            await asyncio.sleep(0.1)
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

        poll_interval = int(self.config.get("poll_interval", 30))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self._handshake_complete = False
        self._handshake_event.clear()
        self._handshake_ok = None
        self._pending_queries.clear()
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    # ── Frame parsing ──

    def _parse_frame(self, buffer: bytes) -> tuple[bytes | None, bytes]:
        """Stateful framer: 16-byte preamble first, then colon-delimited.

        The flag flip happens *inside* parse so the next iteration of
        ``CallableFrameParser.feed``'s loop sees the new state — if we
        only flipped it from ``on_data_received`` we'd extract a second
        bogus 16-byte "handshake" from any ESC/VP21 traffic that arrived
        in the same recv as the handshake reply.
        """
        if not self._handshake_complete:
            if len(buffer) < HANDSHAKE_LEN:
                return None, buffer
            self._handshake_complete = True
            return buffer[:HANDSHAKE_LEN], buffer[HANDSHAKE_LEN:]

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

    async def _send_query(self, key: str, command: str) -> None:
        # ``key`` is the state-variable dispatch key; ``command`` is the
        # actual ESC/VP21 line to send. Pending-queue ordering is
        # preserved: every reply pops the head off and routes by key.
        self._pending_queries.append(key)
        await self._send_line(command)

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "power_on":
            await self._send_line("PWR ON")
            await asyncio.sleep(0.05)
            await self._send_query("power", "PWR?")
        elif command == "power_off":
            await self._send_line("PWR OFF")
            await asyncio.sleep(0.05)
            await self._send_query("power", "PWR?")
        elif command == "set_source":
            name = str(params.get("source", "")).strip()
            code = SOURCE_CODES.get(name)
            if not code:
                log.warning(f"[{self.device_id}] Unknown source name: {name}")
                return
            await self._send_line(f"SOURCE {code}")
            await asyncio.sleep(0.05)
            await self._send_query("source", "SOURCE?")
        elif command == "video_mute_on":
            await self._send_line("MUTE ON")
            await self._send_query("video_mute", "MUTE?")
        elif command == "video_mute_off":
            await self._send_line("MUTE OFF")
            await self._send_query("video_mute", "MUTE?")
        elif command == "freeze_on":
            await self._send_line("FREEZE ON")
            await self._send_query("freeze", "FREEZE?")
        elif command == "freeze_off":
            await self._send_line("FREEZE OFF")
            await self._send_query("freeze", "FREEZE?")
        elif command == "set_aspect":
            name = str(params.get("aspect", "")).strip()
            code = ASPECT_CODES.get(name)
            if not code:
                log.warning(f"[{self.device_id}] Unknown aspect name: {name}")
                return
            await self._send_line(f"ASPECT {code}")
            await self._send_query("aspect", "ASPECT?")
        elif command == "set_color_mode":
            name = str(params.get("mode", "")).strip()
            code = COLOR_MODES.get(name)
            if not code:
                log.warning(f"[{self.device_id}] Unknown color mode: {name}")
                return
            await self._send_line(f"CMODE {code}")
            await self._send_query("color_mode", "CMODE?")
        elif command == "send_key":
            name = str(params.get("key", "")).strip()
            code = KEY_CODES.get(name)
            if not code:
                log.warning(f"[{self.device_id}] Unknown key name: {name}")
                return
            await self._send_line(f"KEY {code}")
        elif command == "raw_command":
            line = str(params.get("command", "")).strip()
            if line:
                await self._send_line(line)
        elif command == "refresh":
            await self.poll()
            return
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    # ── Polling ──

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
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
            log.warning(f"[{self.device_id}] Poll failed — not connected")

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        # Handshake reply: arrives as the first 16-byte frame.
        if not self._handshake_event.is_set():
            self._handshake_ok = data == HANDSHAKE_REPLY_OK
            if not self._handshake_ok:
                log.error(
                    f"[{self.device_id}] Unexpected ESC/VP.net handshake "
                    f"reply: {data.hex()} (expected "
                    f"{HANDSHAKE_REPLY_OK.hex()})"
                )
            else:
                log.info(f"[{self.device_id}] ESC/VP.net handshake accepted")
            self._handshake_event.set()
            return

        # Post-handshake frame. Strip the trailing CR if present (queries
        # respond ``KEY=VAL\r:``; the parser hands us ``KEY=VAL\r``).
        line = data.rstrip(b"\r").decode("ascii", errors="ignore").strip()
        if not line:
            # Bare set ack — no payload to dispatch.
            return

        # Async push event (signal change etc). Format isn't formally
        # documented; log at debug and don't try to derive state.
        if line.startswith("IMEVENT="):
            log.debug(f"[{self.device_id}] IMEVENT: {line}")
            return

        # Error reply to a specific command — pop the pending key but
        # don't crash; some queries (e.g. SOURCE? in standby) legally
        # return ERR.
        if line == "ERR":
            pending = (
                self._pending_queries.pop(0) if self._pending_queries else None
            )
            log.debug(
                f"[{self.device_id}] ESC/VP21 ERR for query={pending!r}"
            )
            return

        self._dispatch_response(line)

    def _dispatch_response(self, line: str) -> None:
        # Successful query responses are ``KEY=VALUE``.
        if "=" not in line:
            log.debug(f"[{self.device_id}] Unparseable response: {line!r}")
            return
        _, value = line.split("=", 1)
        value = value.strip()
        pending = (
            self._pending_queries.pop(0) if self._pending_queries else None
        )

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
            self.set_state("aspect", value)
        elif pending == "color_mode":
            self.set_state("color_mode", value)
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

    def _handle_transport_disconnect(self) -> None:
        self._connected = False
        self._handshake_complete = False
        self._handshake_event.clear()
        self._handshake_ok = None
        self._pending_queries.clear()
        self.set_state("connected", False)
        log.warning(f"[{self.device_id}] Connection lost")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self.events.emit(f"device.disconnected.{self.device_id}")
            )
        except RuntimeError:
            pass
