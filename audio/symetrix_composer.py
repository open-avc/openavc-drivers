"""
OpenAVC Symetrix Composer Control Driver.

Controls Symetrix Edge, Radius, Radius AEC, Radius NX, Prism,
Solus NX, and xControl DSPs over the Composer Control Protocol on
TCP port 48631. The protocol is line-oriented ASCII (terms separated
by spaces, terminated by ``\\r``) and exposes a generic 16-bit
controller-number scheme (1–10000) that AV integrators map in
Composer to specific DSP parameters (faders, mutes, selectors,
meters).

Push vs poll:
    The Symetrix protocol natively pushes parameter changes
    (``#NNNNN=VVVVV<CR>`` lines) whenever any externally controllable
    value changes. The driver enables global push (``PU 1``), then
    issues Push Refresh (``PUR``) to receive every currently
    push-enabled controller's value up front. From there it reacts to
    pushes — no polling required, though ``refresh`` re-issues PUR on
    demand.

Source:
    https://audiobrains.com/data/symetrix/others/Composer-Control-Protocol-v7.0-080918.pdf
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)


DEFAULT_NUM_CONTROLLERS = 64
MAX_NUM_CONTROLLERS = 256

PUSH_LINE_RE = re.compile(r"^#(\d{1,5})=(-?\d{1,5})$")


def _build_state_vars(num_controllers: int) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        "firmware_version": {"type": "string", "label": "Firmware Version"},
        "ip_address": {"type": "string", "label": "IP Address"},
        "last_preset": {
            "type": "integer",
            "label": "Last Loaded Preset",
            "min": 0,
            "max": 1000,
        },
    }
    for n in range(1, num_controllers + 1):
        out[f"controller_{n}_value"] = {
            "type": "integer",
            "label": f"Controller {n}",
            "min": -1,
            "max": 65535,
        }
    return out


class SymetrixComposerDriver(BaseDriver):
    """Symetrix Composer Control Protocol driver."""

    DRIVER_INFO = {
        "id": "symetrix_composer",
        "name": "Symetrix Composer DSP",
        "manufacturer": "Symetrix",
        "category": "audio",
        "version": "1.2.1",
        "author": "OpenAVC",
        "description": (
            "Controls Symetrix Edge, Radius, Radius AEC, Radius NX, "
            "Prism, Solus NX, and xControl DSPs via the Composer "
            "Control Protocol on TCP port 48631. Generic controller "
            "model: assign controller numbers (1–10000) in Composer "
            "to faders, mutes, selectors, and meters; this driver "
            "reads / writes those numbers and subscribes to "
            "push-enabled value changes."
        ),
        "source_url": "https://audiobrains.com/data/symetrix/others/Composer-Control-Protocol-v7.0-080918.pdf",
        "tags": ["dsp", "symetrix", "composer", "ceiling-mic"],
        "verified": False,
        "simulated": True,
        "protocols": ["symetrix-composer"],
        "ports": [48631],
        "transport": "tcp",
        "discovery": {
            # Symetrix ControlNet Discovery on UDP 49216 IS documented
            # at the transport level (Composer "Locate Hardware" tool
            # broadcasts there and devices reply with name + IP), but
            # the request/reply byte layout is NOT in any publicly
            # accessible Symetrix doc, the Composer Control Protocol
            # v7.0 PDF, or the open-source CommLink-Integration
            # symetrix-control Node.js client. A declarative probe
            # block needs a PCAP from real Radius / Edge hardware to
            # lock down. Hint-only via OUI + open Composer port +
            # manufacturer alias.
            #   Refs:
            #     symetrix.co/knowledge/symetrix-control-network-considerations/
            #     symetrix.co/wp-content/uploads/2024/04/Composer-Control-Protocol-v7.0-080918.pdf
            "oui": ["00:0c:d0"],
            "port_open": [48631],
            "manufacturer_alias": ["symetrix"],
        },
        "compatible_models": [
            {
                "manufacturer": "Symetrix",
                "models": [
                    "Edge",
                    "Radius",
                    "Radius 12x8 EX",
                    "Radius AEC",
                    "Radius NX 4x4",
                    "Radius NX 12x8",
                    "Radius NX 12x12",
                    "Prism 0x0",
                    "Prism 4x4",
                    "Prism 8x8",
                    "Prism 12x12",
                    "Prism 16x16",
                    "Solus NX 4",
                    "Solus NX 8",
                    "Solus NX 16",
                    "xControl",
                ],
                "confidence": "untested",
                "notes": (
                    "Every Symetrix DSP that runs Composer 3.0+ shares "
                    "the same Control Protocol on TCP 48631. Newer NX "
                    "models require Composer 6.0+. The driver's "
                    "controller-number coverage is bounded only by "
                    "the configured num_controllers value (default "
                    "64, max 256)."
                ),
            }
        ],
        "help": {
            "overview": (
                "Symetrix DSPs use a generic external-control model: "
                "the AV integrator wires controller numbers to "
                "specific DSP elements in Composer (Tools → "
                "Controller Manager) with the 'Enable Push' setting "
                "active. This driver speaks that protocol, so macros "
                "can move controllers, recall presets, and react to "
                "DSP state changes without writing custom protocol "
                "code."
            ),
            "setup": (
                "1. In Composer, open Tools → Controller Manager and "
                "assign controller numbers (1–10000) to the faders, "
                "mutes, selectors, and meters you want to control "
                "from OpenAVC. Make sure the 'Push' checkbox is "
                "enabled for each.\n"
                "2. Compile and push the design to the unit.\n"
                "3. Note the unit's IP address (System Manager).\n"
                "4. Enter the IP, port 48631, and num_controllers "
                "(set to the highest controller number you assigned, "
                "rounded up — default 64 covers most installations) "
                "in the device config in OpenAVC.\n"
                "5. Connect. The driver enables global push and "
                "issues Push Refresh on connect, so all current "
                "values populate immediately."
            ),
        },
        "default_config": {
            "host": "",
            "port": 48631,
            "num_controllers": DEFAULT_NUM_CONTROLLERS,
            "poll_interval": 0,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 48631,
                "label": "TCP Port",
                "description": (
                    "Composer Control Protocol port — always 48631."
                ),
            },
            "num_controllers": {
                "type": "integer",
                "default": DEFAULT_NUM_CONTROLLERS,
                "min": 1,
                "max": MAX_NUM_CONTROLLERS,
                "label": "Tracked Controllers",
                "description": (
                    "Number of controller_N_value state variables to "
                    "expose (1..N). Pushed values for controllers "
                    "above N are ignored. Set this to cover the "
                    "range of controller numbers you wired up in "
                    "Composer."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "Backstop refresh cycle (sends Push Refresh). "
                    "Symetrix push is reliable; leave at 0 unless "
                    "you've seen state drift."
                ),
            },
        },
        "state_variables": _build_state_vars(DEFAULT_NUM_CONTROLLERS),
        "commands": {
            "set_controller": {
                "label": "Set Controller",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 10000,
                    },
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 65535,
                    },
                },
                "help": (
                    "Move a controller to an absolute position "
                    "(0–65535)."
                ),
            },
            "set_controller_quick": {
                "label": "Set Controller (Quick)",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 10000,
                    },
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 65535,
                    },
                },
                "help": (
                    "Move a controller without server-side existence "
                    "validation. Faster (5–10 ms vs 40 ms) but the "
                    "device always ACKs even if the controller "
                    "doesn't exist."
                ),
            },
            "increment_controller": {
                "label": "Increment Controller",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 10000,
                    },
                    "amount": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 65535,
                    },
                },
                "help": "Step a controller up by an amount (clamped at 65535).",
            },
            "decrement_controller": {
                "label": "Decrement Controller",
                "params": {
                    "number": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 10000,
                    },
                    "amount": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 65535,
                    },
                },
                "help": "Step a controller down by an amount (clamped at 0).",
            },
            "load_preset": {
                "label": "Load Preset",
                "params": {
                    "preset": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 1000,
                    },
                },
                "help": "Recall a Composer preset (1–1000).",
            },
            "flash_unit": {
                "label": "Flash Unit LEDs",
                "params": {
                    "cycles": {
                        "type": "integer",
                        "required": False,
                        "min": 0,
                        "max": 65535,
                        "help": "Number of cycles (0 stops). Default 8.",
                    },
                },
                "help": (
                    "Flash the front-panel LEDs — useful as a "
                    "comms-test ping."
                ),
            },
            "refresh": {
                "label": "Refresh All Controllers",
                "params": {},
                "help": (
                    "Re-issue Push Refresh (PUR) to receive every "
                    "push-enabled controller value again."
                ),
            },
        },
    }

    # ── Lifecycle ──

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state,
        events,
    ):
        self._line_buffer = bytearray()
        self._num_controllers = max(
            1,
            min(
                MAX_NUM_CONTROLLERS,
                int(config.get("num_controllers", DEFAULT_NUM_CONTROLLERS)),
            ),
        )
        # Rebuild state_variables for this instance to reflect the
        # configured num_controllers — the class-level dict is sized
        # for the default.
        self.DRIVER_INFO = dict(self.DRIVER_INFO)
        self.DRIVER_INFO["state_variables"] = _build_state_vars(
            self._num_controllers
        )
        super().__init__(device_id, config, state, events)

    async def connect(self) -> None:
        # Use BaseDriver auto-transport. Symetrix delimiter is \r per
        # the protocol manual.
        from server.transport.tcp import TCPTransport

        host = self.config.get("host", "")
        port = int(self.config.get("port", 48631))
        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\r",
            timeout=5.0,
            name=self.device_id,
        )
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to Symetrix at {host}:{port}")

        # Enable push globally and refresh — every push-enabled
        # controller's current value comes back as #N=V lines.
        try:
            await self._send("PU 1")
            await self._send("V")  # firmware version
            await self._send("RI")  # IP address
            await self._send("GPR")  # last loaded preset
            await self._send("PUR")  # push refresh
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial setup failed")

        poll_interval = int(self.config.get("poll_interval", 0))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            try:
                # Polite quit so the device frees the TCP slot
                # immediately rather than aging it out.
                await self._send("Q!")
            except (ConnectionError, OSError):
                pass
            await self.transport.close()
            self.transport = None
        self._connected = False
        self._line_buffer.clear()
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    # ── Sending ──

    async def _send(self, command: str) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send((command + "\r").encode("ascii"))

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "set_controller":
            await self._send(f"CS {int(params['number'])} {int(params['value'])}")
        elif command == "set_controller_quick":
            await self._send(f"CSQ {int(params['number'])} {int(params['value'])}")
        elif command == "increment_controller":
            await self._send(f"CC {int(params['number'])} 1 {int(params['amount'])}")
        elif command == "decrement_controller":
            await self._send(f"CC {int(params['number'])} 0 {int(params['amount'])}")
        elif command == "load_preset":
            await self._send(f"LP {int(params['preset'])}")
        elif command == "flash_unit":
            cycles = params.get("cycles")
            if cycles is None:
                await self._send("FU")
            else:
                await self._send(f"FU {int(cycles)}")
        elif command == "refresh":
            await self._send("PUR")
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def poll(self) -> None:
        # Polling is purely a backstop — push is the source of truth.
        if self.transport and self.transport.connected:
            try:
                await self._send("PUR")
            except ConnectionError:
                pass

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        # TCPTransport is configured with delimiter=\r so each call to
        # on_data_received gives us one stripped line.
        line = data.decode("ascii", errors="replace").strip()
        if not line:
            return
        self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        if line in ("ACK", "NAK"):
            if line == "NAK":
                log.debug(f"[{self.device_id}] NAK")
            return

        # Push / GSB2-style line: #NNNNN=VVVVV
        m = PUSH_LINE_RE.match(line)
        if m:
            number = int(m.group(1))
            value = int(m.group(2))
            if 1 <= number <= self._num_controllers:
                # -1 ("doesn't exist") clips to -1; the schema allows it.
                self.set_state(f"controller_{number}_value", value)
            return

        # Echo from GSB3 — same `GSB3 ...` line we asked, just skip.
        if line.startswith("GSB3 "):
            return

        # IP address: four dot-separated decimal octets.
        parts = line.split(".")
        if (
            len(parts) == 4
            and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
        ):
            self.set_state("ip_address", line)
            return

        # Preset response: 1–4 digits (GPR returns zero-padded 4).
        if line.isdigit() and 1 <= len(line) <= 4:
            try:
                preset = int(line)
                if 0 <= preset <= 1000:
                    self.set_state("last_preset", preset)
            except ValueError:
                pass
            return

        # Version response: any other ASCII string with a dot and a
        # leading digit — e.g. "1.0.7 (3.6.4)".
        if line[:1].isdigit() and "." in line and len(line) <= 64 and "=" not in line:
            self.set_state("firmware_version", line)
            return
