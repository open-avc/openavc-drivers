"""
OpenAVC Sony VPL (ADCP) Projector Driver.

Controls Sony VPL professional installation projectors over the
Advanced Display Control Protocol (ADCP) on TCP port 53595. ADCP is a
text-based command/response protocol — commands are ASCII strings
terminated by CR+LF, responses are ``ok``, a value, or an ``err_*``
code. Inputs and picture modes vary by model (HDMI 1/2, HDBaseT, USB,
network, etc. — see the protocol's SUPPORTED COMMAND LIST), but the
core surface (power, input, blank, freeze, picture mode, aspect,
image-quality numerics, remote keys) is consistent across the line.

Push vs poll:
    ADCP is request/response. The protocol has no subscription /
    notification mechanism — Sony's network push lives in the SDAP
    advertisement service (broadcast UDP, equipment discovery only,
    no per-state push). Polling is correct here.

Auth:
    By default Sony VPLs ship with ADCP authentication ON. On
    connect the projector sends one of two greetings:
      * a random hex challenge — the controller replies with the
        SHA-256 of ``<challenge> <password>`` (lowercase hex), then
        the projector returns ``ok`` (success) or ``err_auth``;
      * the literal string ``NOKEY`` if authentication is disabled
        in the projector's web setup, in which case commands flow
        directly.
    This SHA-256 challenge-response scheme does NOT fit
    ``ConfigurableDriver``'s ``auth: telnet_login`` block (prompt
    matching, no hashing), which is why this driver is Python rather
    than YAML. If a second projector family ever needs the same
    pattern, the right next step is to add a generic
    ``auth.type: hashed_challenge`` to ConfigurableDriver — for now,
    one driver, one custom path.

Models covered:
    Most VPL professional / installation series share the ADCP
    command set. The exact set of inputs and picture modes varies
    per model — see Sony's "Protocol Manual (SUPPORTED COMMAND
    LIST)" for the per-model coverage matrix.

Source:
    Protocol Manual (COMMON), 1st Edition (Revised 2):
    https://pro.sony/s3/2018/07/03140912/Sony_Protocol-Manual_1st-Edition-Revised-2.pdf
    Protocol Manual (SUPPORTED COMMAND LIST), 1st Edition (Revised 1):
    https://pro.sony/s3/2018/07/19110602/Sony_Protocol-Manual_Supported-Command-List_1st-Edition-Revised-1.pdf
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)


# Power states reported by `power_status ?`. Mapped through verbatim —
# users see the protocol's own labels and macros can match on them.
POWER_STATES = [
    "standby",
    "startup",
    "on",
    "cooling1",
    "cooling2",
    "saving_cooling1",
    "saving_cooling2",
    "saving_standby",
    "update",
]

# Common picture modes. Per-model availability varies; the UI lets the
# user pick any of these and the projector returns err_val if it does
# not support a given mode on this hardware.
PICTURE_MODES = [
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
]

# Aspect ratios supported across the line.
ASPECT_VALUES = [
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
]

# Inputs covering the full VPL line.
INPUT_VALUES = [
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
]

# Remote keys — the universal subset that every model supports.
KEY_CODES = [
    "power_on",
    "power_off",
    "power",
    "input",
    "blank",
    "muting",
    "vol+",
    "vol-",
    "menu",
    "up",
    "down",
    "left",
    "right",
    "enter",
    "reset",
    "return",
    "freeze",
    "aspect",
    "picmode",
    "lens_focus_far",
    "lens_focus_near",
    "lens_zoom_up",
    "lens_zoom_down",
    "lens_shift_up",
    "lens_shift_down",
    "lens_shift_left",
    "lens_shift_right",
    "keystone+",
    "keystone-",
    "status_on",
    "status_off",
]


class SonyVPLDriver(BaseDriver):
    """Sony VPL professional projector driver (ADCP protocol)."""

    DRIVER_INFO = {
        "id": "sony_vpl",
        "name": "Sony VPL Projector (ADCP)",
        "manufacturer": "Sony",
        "category": "projector",
        "version": "1.3.0",
        "author": "OpenAVC",
        "min_platform_version": "0.10.3",
        "description": (
            "Controls Sony VPL professional installation projectors "
            "over Sony's Advanced Display Control Protocol (ADCP) on "
            "TCP port 53595. Power, input, blank, freeze, picture "
            "mode, aspect ratio, image-quality (contrast / "
            "brightness / colour / sharpness), and the universal "
            "remote-key set. Supports SHA-256 challenge-response "
            "authentication so the projector can keep its default "
            "auth-on web setting."
        ),
        "source_url": "https://pro.sony/s3/2018/07/03140912/Sony_Protocol-Manual_1st-Edition-Revised-2.pdf",
        "tags": ["projector", "sony", "adcp", "vpl", "installation"],
        "verified": False,
        "simulated": True,
        "protocols": ["adcp"],
        "ports": [53595],
        "transport": "tcp",
        "discovery": {
            # Sony VPL projectors also support PJLink. Surface this
            # ADCP-specific driver as a soft candidate when the PJLink
            # response identifies the device as Sony.
            "vendor_aliases": ["Sony"],
        },
        "compatible_models": [
            {
                "manufacturer": "Sony",
                "models": [
                    "VPL-FHZ120",
                    "VPL-FHZ90",
                    "VPL-FHZ75",
                    "VPL-FHZ70",
                    "VPL-FHZ65",
                    "VPL-FHZ60",
                    "VPL-FHZ58",
                    "VPL-FHZ57",
                    "VPL-FHZ55",
                    "VPL-FHZ50",
                    "VPL-FWZ65",
                    "VPL-FWZ60",
                    "VPL-FX35",
                    "VPL-FX37",
                    "VPL-FX40",
                    "VPL-FH65",
                    "VPL-FH60",
                    "VPL-FH35",
                    "VPL-FH30",
                    "VPL-FW60",
                    "VPL-FW41",
                    "VPL-PHZ60",
                    "VPL-PHZ50",
                    "VPL-PWZ10",
                    "VPL-CWZ10",
                    "VPL-VW1100ES",
                    "VPL-VW885ES",
                    "VPL-VW675ES",
                    "VPL-VW325ES",
                ],
                "confidence": "untested",
                "notes": (
                    "ADCP is the same on every VPL professional / "
                    "installation projector — the command surface is "
                    "consistent, the variation is which inputs and "
                    "picture modes a given chassis exposes. The "
                    "driver lets the user pick from the union of all "
                    "inputs / modes; unsupported values return "
                    "err_val on the projector, which the driver "
                    "logs but does not raise."
                ),
            }
        ],
        "help": {
            "overview": (
                "ADCP is Sony's text-based control protocol for "
                "professional VPL projectors. It runs on TCP 53595 "
                "with optional SHA-256 challenge-response "
                "authentication. The driver covers the universal "
                "command surface (power, input, blank, freeze, "
                "picture mode, image quality, aspect, remote keys) "
                "and surfaces the projector's reported power and "
                "error state."
            ),
            "setup": (
                "1. Connect the projector to the network via its "
                "LAN or HDBaseT terminal and assign it a static IP.\n"
                "2. In the projector's main menu, set Standby Mode "
                "= Standard so the projector responds to network "
                "commands while powered down.\n"
                "3. Open the projector's web setup page. The "
                "default ADCP password is ``Projector`` — change it "
                "to something private.\n"
                "4. In OpenAVC, enter the projector's IP and the "
                "ADCP password. Leave port at 53595."
            ),
        },
        "default_config": {
            "host": "",
            "port": 53595,
            "password": "Projector",
            "poll_interval": 15,
            "inter_command_delay": 0.05,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 53595,
                "label": "TCP Port",
                "description": "ADCP port. Sony default is 53595.",
            },
            "password": {
                "type": "string",
                "default": "Projector",
                "label": "ADCP Password",
                "secret": True,
                "description": (
                    "Set in the projector's web UI under Setup → "
                    "Advanced Menu → ADCP. Sony's factory default "
                    "is ``Projector``. If ADCP authentication is "
                    "disabled, the value is ignored."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "ADCP has no push notifications — polling is "
                    "the only way to track power transitions. Set "
                    "to 0 to disable."
                ),
            },
            "inter_command_delay": {
                "type": "number",
                "default": 0.05,
                "min": 0,
                "label": "Inter-command Delay (sec)",
                "description": (
                    "Sony's spec says wait for the response before "
                    "sending the next command. The transport "
                    "enforces this delay between sends as a "
                    "safety net."
                ),
            },
        },
        "state_variables": {
            "power_status": {
                "type": "enum",
                "values": POWER_STATES,
                "label": "Power State",
            },
            "error_status": {
                "type": "string",
                "label": "Error Status",
            },
            "input": {
                "type": "string",
                "label": "Input",
            },
            "mute_video": {"type": "boolean", "label": "Video Mute (Blank)"},
            "mute_audio": {"type": "boolean", "label": "Audio Mute"},
            "freeze": {"type": "boolean", "label": "Freeze"},
            "picture_mode": {"type": "string", "label": "Picture Mode"},
            "aspect": {"type": "string", "label": "Aspect Ratio"},
            "contrast": {"type": "integer", "label": "Contrast"},
            "brightness": {"type": "integer", "label": "Brightness"},
            "color": {"type": "integer", "label": "Color Depth"},
            "sharpness": {"type": "integer", "label": "Sharpness"},
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
                    "returns err_val on the projector."
                ),
            },
            "mute_video": {
                "label": "Video Mute (Blank) On",
                "params": {},
            },
            "unmute_video": {
                "label": "Video Mute (Blank) Off",
                "params": {},
            },
            "mute_audio": {"label": "Audio Mute On", "params": {}},
            "unmute_audio": {"label": "Audio Mute Off", "params": {}},
            "freeze_on": {"label": "Freeze On", "params": {}},
            "freeze_off": {"label": "Freeze Off", "params": {}},
            "set_picture_mode": {
                "label": "Set Picture Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": PICTURE_MODES,
                    },
                },
            },
            "set_aspect": {
                "label": "Set Aspect Ratio",
                "params": {
                    "aspect": {
                        "type": "enum",
                        "required": True,
                        "values": ASPECT_VALUES,
                    },
                },
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
            },
            "set_brightness": {
                "label": "Set Brightness",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
            },
            "set_color": {
                "label": "Set Color Depth",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
            },
            "set_sharpness": {
                "label": "Set Sharpness",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    },
                },
            },
            "send_key": {
                "label": "Send Remote Key",
                "params": {
                    "key": {
                        "type": "enum",
                        "required": True,
                        "values": KEY_CODES,
                    },
                },
                "help": (
                    "Emulate a press of a remote control key. "
                    "Useful for menu navigation, lens adjustment, "
                    "and functions without a dedicated ADCP "
                    "command."
                ),
            },
            "raw_command": {
                "label": "Send Raw ADCP Command",
                "params": {
                    "command": {
                        "type": "string",
                        "required": True,
                        "help": (
                            "Full ADCP line without CR+LF, e.g. "
                            "``contrast 75`` or ``power_status ?``."
                        ),
                    },
                },
                "help": (
                    "Escape hatch for ADCP commands not surfaced "
                    "as named commands."
                ),
            },
            "refresh": {"label": "Refresh Status", "params": {}},
        },
    }

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state,
        events,
    ):
        self._auth_done = asyncio.Event()
        self._auth_ok: bool | None = None
        self._challenge: str | None = None
        self._pending_queries: list[str] = []
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ──

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 53595))
        delay = float(self.config.get("inter_command_delay", 0.05))

        self._auth_done.clear()
        self._auth_ok = None
        self._challenge = None

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\r\n",
            inter_command_delay=delay,
            timeout=5.0,
            name=self.device_id,
        )

        # Wait for the projector's greeting (random challenge or NOKEY),
        # send the auth response if needed, and wait for the auth verdict.
        try:
            await asyncio.wait_for(self._auth_done.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            await self.transport.close()
            self.transport = None
            raise ConnectionError(
                f"[{self.device_id}] No ADCP greeting received from "
                f"{host}:{port} within 8s"
            )

        if self._auth_ok is False:
            await self.transport.close()
            self.transport = None
            raise ConnectionError(
                f"[{self.device_id}] ADCP authentication failed — "
                "check the password in the projector's web UI"
            )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(
            f"[{self.device_id}] Connected to Sony VPL (ADCP) at {host}:{port}"
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
        self._auth_ok = None
        self._challenge = None
        self._pending_queries.clear()
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    # ── Sending ──

    async def _send_line(self, line: str) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send((line + "\r\n").encode("ascii"))

    async def _send_query(self, command: str) -> None:
        # ADCP responses come back in order, so we queue the query name
        # and pop the head off when a response arrives. This handles
        # burst polling without losing track of which response maps to
        # which state variable.
        self._pending_queries.append(command)
        await self._send_line(f"{command} ?")

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        # ADCP setters return ``ok`` only — no echoed value. After a
        # setter we issue a follow-up query (mapped below) so state
        # reflects what the projector now reports rather than what we
        # asked for. Commands without a sensible echo target
        # (remote-key passthrough, raw escape hatch) skip this step.
        followup: str | None = None

        if command == "power_on":
            await self._send_line('power "on"')
            followup = "power_status"
        elif command == "power_off":
            await self._send_line('power "off"')
            followup = "power_status"
        elif command == "set_input":
            value = str(params.get("input", "")).strip()
            await self._send_line(f'input "{value}"')
            followup = "input"
        elif command == "mute_video":
            await self._send_line('blank "on"')
            followup = "blank"
        elif command == "unmute_video":
            await self._send_line('blank "off"')
            followup = "blank"
        elif command == "mute_audio":
            await self._send_line('muting "on"')
            followup = "muting"
        elif command == "unmute_audio":
            await self._send_line('muting "off"')
            followup = "muting"
        elif command == "freeze_on":
            await self._send_line('freeze "on"')
            followup = "freeze"
        elif command == "freeze_off":
            await self._send_line('freeze "off"')
            followup = "freeze"
        elif command == "set_picture_mode":
            mode = str(params.get("mode", "")).strip()
            await self._send_line(f'picture_mode "{mode}"')
            followup = "picture_mode"
        elif command == "set_aspect":
            aspect = str(params.get("aspect", "")).strip()
            await self._send_line(f'aspect "{aspect}"')
            followup = "aspect"
        elif command in (
            "set_contrast",
            "set_brightness",
            "set_color",
            "set_sharpness",
        ):
            value = int(params.get("value", 0))
            field = command[len("set_") :]
            await self._send_line(f"{field} {value}")
            followup = field
        elif command == "send_key":
            key = str(params.get("key", "")).strip()
            await self._send_line(f'key "{key}"')
            # A key press can change any number of states (power, blank,
            # muting, freeze, picture/aspect on toggle keys, etc.). The
            # cheapest correct thing is to re-poll the common toggles.
            await asyncio.sleep(0.05)
            for q in ("power_status", "blank", "muting", "freeze"):
                await self._send_query(q)
        elif command == "raw_command":
            line = str(params.get("command", "")).strip()
            if line:
                await self._send_line(line)
        elif command == "refresh":
            await self.poll()
            return
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            return

        if followup is not None:
            # Brief pause so the setter's `ok` arrives before we issue
            # the follow-up query; this keeps the pending-query slot
            # tied to the right response.
            await asyncio.sleep(0.05)
            await self._send_query(followup)

    # ── Polling ──

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return
        try:
            # Power and error first — they always work, even when the
            # projector is in standby. The rest only return meaningful
            # values when the projector is on; ADCP returns err_val if
            # the value isn't currently retrievable, which we log at
            # debug only.
            await self._send_query("power_status")
            await self._send_query("error")
            power = self.get_state("power_status")
            if power == "on":
                await self._send_query("input")
                await self._send_query("blank")
                await self._send_query("muting")
                await self._send_query("freeze")
                await self._send_query("picture_mode")
                await self._send_query("aspect")
                await self._send_query("contrast")
                await self._send_query("brightness")
                await self._send_query("color")
                await self._send_query("sharpness")
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        line = data.decode("ascii", errors="ignore").strip()
        if not line:
            return

        # Auth phase: greeting line determines whether we hash or not,
        # and the next line ("ok" / "err_auth") finalises the verdict.
        if not self._auth_done.is_set():
            self._handle_auth_line(line)
            return

        # Post-auth: error responses are diagnostic, ok confirms the last
        # set, and everything else is a query response.
        if line in ("ok", "OK"):
            return
        if line.startswith("err_"):
            log.debug(f"[{self.device_id}] ADCP error: {line}")
            return

        self._dispatch_response(line)

    def _handle_auth_line(self, line: str) -> None:
        if self._challenge is None and self._auth_ok is None:
            # First post-connect line — either NOKEY or the random hex.
            if line == "NOKEY":
                self.set_state("auth_required", False)
                self._auth_ok = True
                self._auth_done.set()
                log.info(
                    f"[{self.device_id}] ADCP authentication disabled "
                    "(NOKEY)"
                )
                return

            # Treat the line as a challenge — hash it with the password
            # and send back. Sony's spec uses lowercase hex digest.
            self._challenge = line
            self.set_state("auth_required", True)
            password = self.config.get("password", "") or ""
            digest = hashlib.sha256(
                f"{line} {password}".encode("ascii", errors="ignore")
            ).hexdigest()
            log.info(f"[{self.device_id}] ADCP auth challenge received")
            asyncio.create_task(self._send_line(digest))
            return

        # Verdict line.
        if line in ("ok", "OK"):
            self._auth_ok = True
            log.info(f"[{self.device_id}] ADCP authentication accepted")
        else:
            self._auth_ok = False
            log.error(
                f"[{self.device_id}] ADCP authentication rejected: {line}"
            )
        self._auth_done.set()

    def _dispatch_response(self, line: str) -> None:
        # Most query responses are bare values, sometimes quoted.
        # `error ?` is a JSON array. We pair the response with the
        # head of the pending-query queue.
        pending = self._pending_queries.pop(0) if self._pending_queries else None

        if pending == "error":
            self._parse_error_response(line)
            return

        value = line.strip().strip('"')
        if pending == "power_status":
            self.set_state("power_status", value)
        elif pending == "input":
            self.set_state("input", value)
        elif pending == "blank":
            self.set_state("mute_video", value == "on")
        elif pending == "muting":
            self.set_state("mute_audio", value == "on")
        elif pending == "freeze":
            self.set_state("freeze", value == "on")
        elif pending == "picture_mode":
            self.set_state("picture_mode", value)
        elif pending == "aspect":
            self.set_state("aspect", value)
        elif pending in ("contrast", "brightness", "color", "sharpness"):
            try:
                self.set_state(pending, int(value))
            except ValueError:
                pass
        else:
            log.debug(
                f"[{self.device_id}] Unmatched ADCP response: {line!r} "
                f"(pending={pending!r})"
            )

    def _parse_error_response(self, line: str) -> None:
        # "no_err" is a bare token; otherwise the projector returns a
        # JSON array like ["err_temp","err_fan"].
        stripped = line.strip().strip('"')
        if stripped == "no_err":
            self.set_state("error_status", "ok")
            return
        if line.startswith("[") and line.endswith("]"):
            inner = line[1:-1].strip()
            errs = [
                tok.strip().strip('"')
                for tok in inner.split(",")
                if tok.strip()
            ]
            if errs:
                self.set_state("error_status", ", ".join(errs))
                return
        self.set_state("error_status", stripped)

    def _handle_transport_disconnect(self) -> None:
        self._connected = False
        self._auth_done.clear()
        self._auth_ok = None
        self._challenge = None
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
