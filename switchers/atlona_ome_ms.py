"""
OpenAVC Atlona AT-OME-MS Series Driver.

Controls the AT-OME-MS family of presentation matrix switchers (MS42 / MS42-HDBT
/ MS52 / MS52W / MS52W-EU) over Telnet on TCP port 23. The MS-series shares a
single command syntax — `Display:Matrix:Set`, `Audio:Volume:Set`, etc. — and is
the modern Atlona presentation-matrix protocol family. The older AT-OME-PS62 uses
a completely different `xNAVxM` / `VOUTX Y` syntax and has its own driver
(`atlona_ome_ps62`).

Why Python (not YAML):
    The MS-series feedback is JSON. Default is multi-line "pretty" JSON;
    `OutputMode j` switches it to single-line compact JSON. Either way,
    YAML's per-line response dispatch is the wrong tool — JSON parsing
    via `json.loads()` is the natural fit. We send `OutputMode j` on
    connect, then `json.loads` every line and route by the `methodreturn`
    field. Set commands echo `{"result":{"success":true},"methodreturn":...}`
    which we tolerate; Get commands echo a payload that we map into state.

Auth:
    The AT-OME-MS family has a "Telnet Login Mode" toggle in the device's
    web UI (System tab). When ON the device prompts `Login: ` then
    `Password: ` before commands; when OFF commands are accepted
    immediately. The driver follows the same fire-and-forget pattern as
    `wattbox_ip` — if a username is configured, send `<user>` then
    `<pass>` after short pauses; if username is blank, skip auth. Either
    path ends with `OutputMode j`.

Wire format:
    Outgoing commands are CR-terminated (0x0d).
    Incoming feedback is CRLF-terminated (0x0d 0x0a).
    Atlona requires a 500 ms minimum delay between commands; the driver
    enforces this with an asyncio lock + sleep.

Device settings:
    Volume, both mutes, external audio source, USB routing mode, and
    matrix mode are set-and-polled values, so they're exposed as device
    settings (editable field + offline pending queue) with their polled
    state vars as the read-back. Matrix mode's Get reports only on/off,
    so the setting is boolean; static-routing mode stays on the Set
    Matrix Mode command.

Liveness:
    Sends are fire-and-forget on raw TCP, so a matrix that drops without
    a FIN used to stay "online" indefinitely. The BaseDriver watchdog
    probes Misc:Model:Get and awaits the parsed reply; two silent probes
    force a reconnect with a typed no_response fault.

Protocol reference: AT-OME-MS52W Application Programming Interface 2.9.6
    https://atlona.com/pdf/AT-OME-MS52W_API.pdf
The MS42 / MS42-HDBT / MS52 use the same dotted command vocabulary; the
MS52W adds a few BYOD / wireless-link commands not exposed here.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)


# Volume range per Audio:Volume:Set — -80 dB .. 0 dB on the MS52W.
VOLUME_MIN = -80
VOLUME_MAX = 0

# Inputs 0-4 (USB-C, DisplayPort, HDMI, HDMI, BYOD); outputs 0-1
# (HDBaseT, HDMI). Range varies slightly across MS42 / MS52 — the driver
# accepts the union and lets the device reject anything it doesn't have.
INPUT_MIN, INPUT_MAX = 0, 4
OUTPUT_MIN, OUTPUT_MAX = 0, 1


def _build_state_vars() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        "model": {"type": "string", "label": "Model"},
        "firmware": {"type": "string", "label": "Firmware Version"},
        "temperature": {"type": "number", "label": "Internal Temp (deg C)"},
        "matrix_mode": {"type": "boolean", "label": "Matrix Mode"},
        "active_input": {
            "type": "integer",
            "label": "Active Input (single-input mode)",
        },
        "display_power": {
            "type": "boolean",
            "label": "Display Power (CEC)",
        },
        "volume": {
            "type": "integer",
            "label": "Volume (dB)",
            "min": VOLUME_MIN,
            "max": VOLUME_MAX,
        },
        "mute_hdmi": {"type": "boolean", "label": "HDMI Audio Mute"},
        "mute_analog": {"type": "boolean", "label": "Analog Audio Mute"},
        "audio_source": {
            "type": "enum",
            "values": ["digital", "analog"],
            "label": "External Audio Source",
        },
        "usb_mode": {
            "type": "enum",
            "values": ["follow", "manual"],
            "label": "USB Routing Mode",
        },
        "usb_route": {"type": "integer", "label": "USB Active Input"},
    }
    for o in range(OUTPUT_MIN, OUTPUT_MAX + 1):
        label = "HDBaseT" if o == 0 else "HDMI"
        out[f"route_{o}"] = {
            "type": "integer",
            "label": f"Output {o} ({label}) — Source",
        }
    for i in range(INPUT_MIN, INPUT_MAX + 1):
        out[f"input_signal_{i}"] = {
            "type": "boolean",
            "label": f"Input {i} — Signal",
        }
    return out


class AtlonaOmeMsDriver(BaseDriver):
    """Atlona AT-OME-MS series matrix presentation switcher."""

    DRIVER_INFO = {
        "id": "atlona_ome_ms",
        "name": "Atlona AT-OME-MS Series Matrix Switcher",
        "manufacturer": "Atlona",
        "category": "switcher",
        "version": "1.3.1",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "author": "OpenAVC",
        "description": (
            "Controls the Atlona AT-OME-MS family of 4K/UHD matrix "
            "presentation switchers (MS42 / MS42-HDBT / MS52 / MS52W / "
            "MS52W-EU) over Telnet on TCP port 23. Routes any of five "
            "inputs (USB-C / DisplayPort / HDMI / HDMI / BYOD) to two "
            "outputs (HDBaseT and HDMI), with audio volume / mute (HDMI "
            "and analog independently), CEC display control, USB host "
            "switching, and per-input signal detection. Use this driver "
            "for the Display:Matrix:Set protocol family; the older PS62 "
            "with its xNAVxM syntax is covered by atlona_ome_ps62."
        ),
        "source_url": "https://atlona.com/pdf/AT-OME-MS52W_API.pdf",
        "tags": ["matrix-switcher", "presentation", "hdbaset", "atlona", "ome", "telnet"],
        "verified": False,
        "simulated": True,
        "protocols": ["atlona_ome_ms"],
        "ports": [23],
        "transport": "tcp",
        "discovery": {
            # Same Atlona hint-only situation as the other OME drivers —
            # KB01625 confirms mDNS use but the exact PTR string isn't
            # public. Hint-only via OUI + manufacturer alias.
            "oui": ["b8:98:b0"],
            "manufacturer_alias": ["atlona"],
        },
        "compatible_models": [
            {
                "manufacturer": "Atlona",
                "models": [
                    "AT-OME-MS42",
                    "AT-OME-MS42-HDBT",
                    "AT-OME-MS52",
                    "AT-OME-MS52W",
                    "AT-OME-MS52W-EU",
                ],
                "confidence": "untested",
                "notes": (
                    "All MS-series presentation matrices share the dotted "
                    "Display:Matrix:Set / Audio:Volume:Set command vocabulary. "
                    "MS42 (4x2) inputs 0-3, MS52/MS52W (5x2) inputs 0-4. "
                    "The MS52W adds a 4 = BYOD wireless-link input. Outputs "
                    "are always 0=HDBaseT, 1=HDMI. The driver advertises "
                    "the 0-4 input range; the device rejects anything it "
                    "doesn't have."
                ),
            }
        ],
        "help": {
            "overview": (
                "AT-OME-MS matrices route HDMI / DisplayPort / USB-C "
                "sources to a HDBaseT extender output and a local HDMI "
                "output. Audio is independent of video routing — the HDMI "
                "and analog outputs each have their own mute and a shared "
                "system volume in dB. Display power can be controlled "
                "over CEC. USB host follow modes let a connected control "
                "host follow the active video source."
            ),
            "setup": (
                "1. Set a static IP on the matrix via its web UI (or DHCP "
                "reservation).\n"
                "2. If Telnet Login Mode is ON in the matrix's web UI "
                "(System tab), enter the same Username and Password used "
                "for the web GUI in this driver's config. Leave both "
                "blank if Telnet Login Mode is OFF.\n"
                "3. Atlona requires at least 500 ms between commands; the "
                "driver enforces that automatically.\n"
                "4. Use Set Matrix Mode to switch between single-input "
                "(both outputs follow Display:Input:Set) and matrix mode "
                "(per-output routing via Display:Matrix:Set)."
            ),
        },
        "default_config": {
            "host": "",
            "port": 23,
            "username": "",
            "password": "",
            "poll_interval": 30,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 23,
                "label": "Telnet Port",
            },
            "username": {
                "type": "string",
                "default": "",
                "label": "Username",
                "description": (
                    "Telnet login. Required only when Telnet Login Mode "
                    "is enabled in the matrix's web UI."
                ),
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "Password",
                "secret": True,
                "description": "Telnet password. Same credentials as the matrix web GUI.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": "Set to 0 to disable polling.",
            },
        },
        "state_variables": _build_state_vars(),
        "commands": {
            "route": {
                "label": "Route Input to Output",
                "params": {
                    "input": {
                        "type": "integer",
                        "required": True,
                        "min": INPUT_MIN,
                        "max": INPUT_MAX,
                        "help": "0=USB-C, 1=DisplayPort, 2=HDMI, 3=HDMI, 4=BYOD.",
                    },
                    "output": {
                        "type": "integer",
                        "required": True,
                        "min": OUTPUT_MIN,
                        "max": OUTPUT_MAX,
                        "help": "0=HDBaseT, 1=HDMI.",
                    },
                },
                "help": (
                    "Route an input to a specific output (matrix mode "
                    "only — fails with 'Command Failure' if matrix mode "
                    "is disabled). Use Set Matrix Mode to enable matrix "
                    "mode first."
                ),
            },
            "query_routing": {
                "label": "Query Routing",
                "params": {},
                "help": "Refresh route_0 and route_1 from the matrix.",
            },
            "set_active_input": {
                "label": "Set Active Input (single-input mode)",
                "params": {
                    "input": {
                        "type": "integer",
                        "required": True,
                        "min": INPUT_MIN,
                        "max": INPUT_MAX,
                    },
                },
                "help": (
                    "Set the active input when matrix mode is disabled. "
                    "Both outputs follow this input."
                ),
            },
            "set_matrix_mode": {
                "label": "Set Matrix Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["off", "on", "static"],
                        "help": (
                            "off = single-input (both outputs follow Set "
                            "Active Input). on = matrix mode. static = "
                            "matrix mode with static routing."
                        ),
                    },
                },
                "help": "Enable or disable matrix mode.",
            },
            "display_on": {
                "label": "Display On (CEC)",
                "params": {},
                "help": "Send CEC power-on to the connected display.",
            },
            "display_off": {
                "label": "Display Off (CEC)",
                "params": {},
                "help": "Send CEC power-off to the connected display.",
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "min": VOLUME_MIN,
                        "max": VOLUME_MAX,
                        "help": "Volume in decibels (-80..0).",
                    },
                },
                "help": "Set the overall audio output level.",
            },
            "volume_up": {
                "label": "Volume Up",
                "params": {
                    "amount": {
                        "type": "integer",
                        "required": False,
                        "min": 1,
                        "max": 80,
                        "help": "dB to increase by (default 1).",
                    },
                },
                "help": "Increase volume by N decibels (default 1).",
            },
            "volume_down": {
                "label": "Volume Down",
                "params": {
                    "amount": {
                        "type": "integer",
                        "required": False,
                        "min": 1,
                        "max": 80,
                    },
                },
                "help": "Decrease volume by N decibels (default 1).",
            },
            "mute_on": {
                "label": "Mute Audio",
                "params": {
                    "channel": {
                        "type": "enum",
                        "required": True,
                        "values": ["hdmi", "analog"],
                    },
                },
                "help": "Mute either the HDMI or analog audio output.",
            },
            "mute_off": {
                "label": "Unmute Audio",
                "params": {
                    "channel": {
                        "type": "enum",
                        "required": True,
                        "values": ["hdmi", "analog"],
                    },
                },
                "help": "Unmute either the HDMI or analog audio output.",
            },
            "set_audio_source": {
                "label": "Set External Audio Source",
                "params": {
                    "source": {
                        "type": "enum",
                        "required": True,
                        "values": ["digital", "analog"],
                    },
                },
                "help": (
                    "Select whether external audio (when not embedded) "
                    "comes from the digital or analog input."
                ),
            },
            "set_usb_mode": {
                "label": "Set USB Routing Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["follow", "manual"],
                    },
                },
                "help": (
                    "follow = USB host tracks the active video input; "
                    "manual = USB host stays where you put it."
                ),
            },
            "set_usb_route": {
                "label": "Set USB Host Source",
                "params": {
                    "input": {
                        "type": "integer",
                        "required": True,
                        "min": INPUT_MIN,
                        "max": INPUT_MAX,
                    },
                },
                "help": "Pick which input gets the USB host (manual mode).",
            },
            "query_input_states": {
                "label": "Query Input Signal States",
                "params": {},
                "help": "Refresh per-input signal detection.",
            },
            "platform_restart": {
                "label": "Restart Device",
                "params": {},
                "help": "Reboot the matrix. Connection drops until it's back.",
            },
            "platform_shutdown": {
                "label": "Shut Down Device",
                "params": {},
                "help": "Power down the matrix. Use only if you can power-cycle physically.",
            },
            "factory_reset": {
                "label": "Factory Reset",
                "params": {},
                "help": (
                    "WARNING: Resets every setting including network "
                    "config to factory defaults. Network preserved by "
                    "default per the API."
                ),
            },
            "refresh": {
                "label": "Refresh All Status",
                "params": {},
                "help": "Re-poll model, firmware, routing, audio, and signal states.",
            },
        },
        # Device settings: every entry is already both written and polled
        # by the commands above, so each gets the editable-field + offline
        # pending-queue treatment with its polled state var as the
        # read-back. Matrix mode's Get reports only on/off (a boolean), so
        # the setting is the on/off pair — static-routing mode is still
        # available through the Set Matrix Mode command.
        "device_settings": {
            "volume": {
                "type": "integer",
                "label": "Volume (dB)",
                "min": VOLUME_MIN,
                "max": VOLUME_MAX,
                "state_key": "volume",
                "default": -40,
                "setup": False,
                "help": "Overall audio output level in decibels (-80..0).",
            },
            "mute_hdmi": {
                "type": "boolean",
                "label": "HDMI Audio Mute",
                "state_key": "mute_hdmi",
                "default": False,
                "setup": False,
            },
            "mute_analog": {
                "type": "boolean",
                "label": "Analog Audio Mute",
                "state_key": "mute_analog",
                "default": False,
                "setup": False,
            },
            "audio_source": {
                "type": "enum",
                "values": ["digital", "analog"],
                "label": "External Audio Source",
                "state_key": "audio_source",
                "default": "digital",
                "setup": False,
                "help": (
                    "Whether external (non-embedded) audio comes from the "
                    "digital or analog input."
                ),
            },
            "usb_mode": {
                "type": "enum",
                "values": ["follow", "manual"],
                "label": "USB Routing Mode",
                "state_key": "usb_mode",
                "default": "follow",
                "setup": False,
                "help": (
                    "follow = USB host tracks the active video input; "
                    "manual = USB host stays on the input picked with Set "
                    "USB Host Source."
                ),
            },
            "matrix_mode": {
                "type": "boolean",
                "label": "Matrix Mode",
                "state_key": "matrix_mode",
                "default": False,
                "setup": False,
                "help": (
                    "Off = single-input mode (both outputs follow the "
                    "active input). On = independent per-output routing. "
                    "The static-routing variant is available via the Set "
                    "Matrix Mode command."
                ),
            },
        },
        # Quick Action strip: display power is the daily surface; restart
        # confirms because the room goes dark until the matrix is back.
        "actions": [
            {"id": "display_on", "kind": "command", "icon": "monitor"},
            {"id": "display_off", "kind": "command", "icon": "monitor-off"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "platform_restart", "kind": "command", "icon": "power",
                "confirm": (
                    "Reboot the matrix? Video and audio drop until it "
                    "finishes restarting."
                ),
            },
        ],
    }

    # BaseDriver liveness watchdog fault wording (typed no_response).
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the matrix stopped answering status queries."
    )
    # The 500 ms inter-command lock means a probe that fires while a full
    # poll cycle (15 queries ~= 7.5 s) holds the lock can legitimately
    # wait ~8 s before its answer arrives. Give the probe headroom past
    # that worst case so a busy cycle can't read as a dead matrix.
    HEALTH_TIMEOUT_S = 10.0

    def __init__(self, device_id, config, state, events):
        self._line_buffer = b""
        self._authenticated = False
        # Atlona requires 500 ms minimum between commands. Serialize sends
        # behind a lock + sleep so back-to-back commands don't collide.
        self._send_lock = asyncio.Lock()
        self._inter_cmd_delay = 0.5
        # Liveness probe correlation: the watchdog awaits the next parsed
        # Misc:Model:Get reply through this future.
        self._probe_future: asyncio.Future[None] | None = None
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ──

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # Raw transport — we control the line read / auth handshake.
        kwargs["delimiter"] = None
        return kwargs

    async def _post_connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 23))
        username = self.config.get("username", "") or ""
        password = self.config.get("password", "") or ""

        try:
            # Brief pause so the device flushes any login banner.
            await asyncio.sleep(0.3)
            if username:
                # Telnet Login Mode is ON. Fire-and-forget the prompts —
                # banner timing varies across firmware versions; we trust
                # the device to consume the credentials in order.
                await self.transport.send((username + "\r").encode("utf-8"))
                await asyncio.sleep(0.2)
                await self.transport.send((password + "\r").encode("utf-8"))
                await asyncio.sleep(0.2)
            # Switch to single-line compact JSON for the rest of the
            # session. Without this, default is multi-line "pretty" JSON
            # which the line parser can't handle cleanly.
            await self.transport.send(b"OutputMode j\r")
            await asyncio.sleep(0.2)
        except (ConnectionError, OSError) as e:
            raise ConnectionError(
                f"AT-OME-MS auth handshake failed for {host}:{port}: {e}"
            ) from e

    async def _initial_sync(self) -> None:
        try:
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

    async def _close_session(self) -> None:
        # Runs on every teardown path: cancel the awaited liveness probe
        # and reset the auth/line state for the next attempt.
        if self._probe_future is not None and not self._probe_future.done():
            self._probe_future.cancel()
        self._probe_future = None
        self._authenticated = False
        self._line_buffer = b""

    # ── Sending (with 500 ms inter-command delay) ──

    async def _send(self, line: str) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._send_lock:
            await self.transport.send((line + "\r").encode("utf-8"))
            await asyncio.sleep(self._inter_cmd_delay)

    async def send_command(self, command: str, params: dict | None = None) -> Any:
        params = params or {}

        if command == "route":
            inp = int(params["input"])
            out = int(params["output"])
            await self._send(f"Display:Matrix:Set {inp} {out}")
        elif command == "query_routing":
            await self._send("Display:Matrix:Get 0")
            await self._send("Display:Matrix:Get 1")
        elif command == "set_active_input":
            inp = int(params["input"])
            await self._send(f"Display:Input:Set {inp}")
        elif command == "set_matrix_mode":
            mode_map = {"off": 0, "on": 1, "static": 2}
            mode = mode_map[str(params["mode"])]
            await self._send(f"Display:Matrix:Mode:Set {mode}")
        elif command == "display_on":
            # The MS-series accepts both Display:Minimal:Set and CEC:Trig
            # for display power. Display:Minimal:Set is more common — it
            # also handles non-CEC displays via Display:Control:IP when
            # configured. Use Display:Minimal:Set for both directions.
            await self._send("Display:Minimal:Set 1")
        elif command == "display_off":
            await self._send("Display:Minimal:Set 0")
        elif command == "set_volume":
            level = int(params["level"])
            level = max(VOLUME_MIN, min(VOLUME_MAX, level))
            await self._send(f"Audio:Volume:Set {level}")
        elif command == "volume_up":
            amount = int(params.get("amount") or 1)
            await self._send(f"Audio:Volume:Increase {amount}")
        elif command == "volume_down":
            amount = int(params.get("amount") or 1)
            await self._send(f"Audio:Volume:Decrease {amount}")
        elif command == "mute_on":
            ch = str(params["channel"])
            await self._send(f"Audio:Mute:Set {ch} true")
        elif command == "mute_off":
            ch = str(params["channel"])
            await self._send(f"Audio:Mute:Set {ch} false")
        elif command == "set_audio_source":
            src = str(params["source"])
            await self._send(f"Audio:SetSource {src}")
        elif command == "set_usb_mode":
            mode = str(params["mode"])  # follow / manual
            await self._send(f"USBRouting:Mode:Set {mode}")
        elif command == "set_usb_route":
            inp = int(params["input"])
            await self._send(f"USBRouting:Input:Set {inp}")
        elif command == "query_input_states":
            for i in range(INPUT_MIN, INPUT_MAX + 1):
                await self._send(f"Display:InputState:Get {i}")
        elif command == "platform_restart":
            await self._send("Platform:Restart")
        elif command == "platform_shutdown":
            await self._send("Platform:Shutdown")
        elif command == "factory_reset":
            await self._send("Platform:Reset all")
        elif command == "refresh":
            await self.poll()
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting; read-back rides the polled state_key.

        Every setting reuses the wire command its transient counterpart
        sends, so byte-building stays in one place. The set-ack echo
        (``_dispatch_set_ack``) updates state immediately; polling is the
        steady-state read-back.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if key == "volume":
            level = max(VOLUME_MIN, min(VOLUME_MAX, int(value)))
            await self._send(f"Audio:Volume:Set {level}")
        elif key in ("mute_hdmi", "mute_analog"):
            ch = key.split("_", 1)[1]
            flag = (
                value if isinstance(value, bool)
                else str(value).strip().lower() in ("1", "true", "on", "yes")
            )
            await self._send(f"Audio:Mute:Set {ch} {'true' if flag else 'false'}")
        elif key == "audio_source":
            src = str(value)
            if src not in ("digital", "analog"):
                raise ValueError(f"audio_source must be digital/analog: {value!r}")
            await self._send(f"Audio:SetSource {src}")
        elif key == "usb_mode":
            mode = str(value)
            if mode not in ("follow", "manual"):
                raise ValueError(f"usb_mode must be follow/manual: {value!r}")
            await self._send(f"USBRouting:Mode:Set {mode}")
        elif key == "matrix_mode":
            flag = (
                value if isinstance(value, bool)
                else str(value).strip().lower() in ("1", "true", "on", "yes")
            )
            await self._send(f"Display:Matrix:Mode:Set {1 if flag else 0}")
        else:
            raise ValueError(f"Unknown device setting: {key}")

    async def _liveness_probe(self) -> None:
        """Send Misc:Model:Get and await its parsed reply (watchdog hook).

        Sends are fire-and-forget on raw TCP, so a silently dead matrix is
        only detectable by awaiting an answer. The future is primed before
        the send so a fast reply can't beat the awaiter.
        """
        loop = asyncio.get_running_loop()
        self._probe_future = loop.create_future()
        try:
            await self._send("Misc:Model:Get")
            await self._probe_future
        finally:
            self._probe_future = None

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return
        try:
            await self._send("Misc:Model:Get")
            await self._send("Misc:Version:Get master")
            await self._send("Display:Matrix:Mode:Get")
            await self._send("Display:Input:Get")
            await self._send("Display:Matrix:Get 0")
            await self._send("Display:Matrix:Get 1")
            await self._send("Display:Minimal:Get")
            await self._send("Audio:Volume:Get")
            await self._send("Audio:Mute:Get")
            await self._send("Audio:GetSource")
            await self._send("USBRouting:Mode:Get")
            await self._send("USBRouting:Input:Get")
            for i in range(INPUT_MIN, INPUT_MAX + 1):
                await self._send(f"Display:InputState:Get {i}")
            await self._send("Instruments:Temperature:Get")
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")

    # ── Receive / parse ──

    async def on_data_received(self, data: bytes) -> None:
        self._line_buffer += data
        while b"\n" in self._line_buffer:
            line_bytes, self._line_buffer = self._line_buffer.split(b"\n", 1)
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        # Auth banner / prompts — drop. Atlona prints "Login: " and
        # "Password: " (no newline before our credentials), so they may
        # arrive embedded in larger reads; the strip() above keeps just
        # the visible chars. We don't strictly track auth state — once
        # OutputMode j is sent, parseable JSON arrives.
        if line.startswith("Login:") or line.startswith("Password:"):
            return
        if line.startswith("Welcome") or line.startswith("OutputMode"):
            self._authenticated = True
            return
        if line == "Unknown command" or line == "Command Failure":
            log.warning(f"[{self.device_id}] Device reported: {line}")
            return

        # Compact JSON lines look like {"result":{...},"methodreturn":"...",...}.
        # Anything that isn't valid JSON is ignored — covers stray banners
        # and the "OutputMode is JSON" plain-text confirmation.
        if not (line.startswith("{") and line.endswith("}")):
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.debug(f"[{self.device_id}] Ignored non-JSON line: {line!r}")
            return

        result = obj.get("result")
        method = obj.get("methodreturn", "")
        if not isinstance(result, dict):
            return
        # Normalize method to "noun:verb..." with no spaces, lowercase.
        # Doc shows e.g. "display: matrix: get 1" with spaces around colons.
        method_key = re.sub(r"\s*:\s*", ":", method.strip().lower())
        self._dispatch(method_key, result)

    def _dispatch(self, method_key: str, result: dict) -> None:
        # Set / write commands — parse the args echoed in methodreturn so
        # the driver state mirrors the device without a follow-up Get on
        # every set. Display:Input:Set returns {"activeinput": N},
        # Display:Matrix:Set / Audio:Volume:Set / Audio:Mute:Set return
        # {"success": true}, and Audio:Volume:Increase/Decrease return
        # {"volume": N, "success": true} — all share `:set ` / `setsource`
        # / `volume:increase` / `volume:decrease` in the method key.
        # Increase / Decrease are kept in the Get path below because the
        # actual new volume value lives in result.volume.
        if (
            ":set" in method_key
            or method_key.startswith("audio:setsource")
        ):
            self._dispatch_set_ack(method_key)
            return

        # ── Misc:Model:Get ──
        if method_key == "misc:model:get":
            # A parsed model reply is the liveness probe's answer. The
            # poll cycle's own Misc:Model:Get satisfies a concurrent
            # probe too — any correlated answer proves the link is alive.
            if self._probe_future is not None and not self._probe_future.done():
                self._probe_future.set_result(None)
            model = str(result.get("model", "")).replace(" ", "")
            if model:
                self.set_state("model", model)
            return

        # ── Misc:Version:Get [master|mcu] ──
        # `result.version` is a dict like {"master": "1.0.00"} — pull the
        # first value as the firmware string.
        if method_key.startswith("misc:version:get"):
            version = result.get("version")
            if isinstance(version, dict) and version:
                value = next(iter(version.values()))
                self.set_state("firmware", str(value).replace(" ", ""))
            elif isinstance(version, str):
                self.set_state("firmware", version)
            return

        # ── Display:Matrix:Mode:Get → {"mode": true/false, "subtype": ...} ──
        if method_key == "display:matrix:mode:get":
            mode = result.get("mode")
            if isinstance(mode, bool):
                self.set_state("matrix_mode", mode)
            return

        # ── Display:Input:Get → {"input": N, "type": "..."} ──
        if method_key == "display:input:get":
            inp = result.get("input")
            if isinstance(inp, int):
                self.set_state("active_input", inp)
            return

        # ── Display:Matrix:Get N → {"input": M}, methodreturn names output ──
        if method_key.startswith("display:matrix:get"):
            inp = result.get("input")
            # Pull the output index from the method tail ("display:matrix:get 0").
            tail = method_key.split()
            if isinstance(inp, int) and len(tail) >= 2:
                try:
                    out = int(tail[-1])
                except ValueError:
                    return
                if OUTPUT_MIN <= out <= OUTPUT_MAX:
                    self.set_state(f"route_{out}", inp)
            return

        # ── Display:Minimal:Get / Set → {"state": bool} or {"success": ...} ──
        if method_key.startswith("display:minimal:"):
            st = result.get("state")
            if isinstance(st, bool):
                self.set_state("display_power", st)
            return

        # ── Audio:Volume:Get / Set / Increase / Decrease ──
        # Get returns nested {"volume":{"units":"dB","value":N}}; Inc/Dec
        # returns flat {"volume": N, "success": true}.
        if method_key.startswith("audio:volume:"):
            volume = result.get("volume")
            if isinstance(volume, dict):
                value = volume.get("value")
                if isinstance(value, (int, float)):
                    self.set_state("volume", int(value))
            elif isinstance(volume, (int, float)):
                self.set_state("volume", int(volume))
            return

        # ── Audio:Mute:Get → {"outputmute":{"hdmi":bool,"analog":bool}} ──
        if method_key == "audio:mute:get":
            mute = result.get("outputmute")
            if isinstance(mute, dict):
                if isinstance(mute.get("hdmi"), bool):
                    self.set_state("mute_hdmi", mute["hdmi"])
                if isinstance(mute.get("analog"), bool):
                    self.set_state("mute_analog", mute["analog"])
            return

        # ── Audio:GetSource → {"audiosource": "digital"|"analog"} ──
        if method_key == "audio:getsource":
            src = result.get("audiosource")
            if isinstance(src, str) and src in ("digital", "analog"):
                self.set_state("audio_source", src)
            return

        # ── USBRouting:Mode:Get → {"mode": "follow"|"manual"} ──
        if method_key == "usbrouting:mode:get":
            mode = result.get("mode")
            if isinstance(mode, str) and mode in ("follow", "manual"):
                self.set_state("usb_mode", mode)
            return

        # ── USBRouting:Input:Get → {"input": N} ──
        if method_key.startswith("usbrouting:input:get"):
            inp = result.get("input")
            if isinstance(inp, int):
                self.set_state("usb_route", inp)
            return

        # ── Display:InputState:Get N → {"input": N, "state": bool} ──
        if method_key.startswith("display:inputstate:get"):
            inp = result.get("input")
            st = result.get("state")
            if isinstance(inp, int) and isinstance(st, bool):
                if INPUT_MIN <= inp <= INPUT_MAX:
                    self.set_state(f"input_signal_{inp}", st)
            return

        # ── Instruments:Temperature:Get → {"temperature": N} ──
        if method_key == "instruments:temperature:get":
            temp = result.get("temperature")
            if isinstance(temp, (int, float)):
                self.set_state("temperature", float(temp))
            return

    def _dispatch_set_ack(self, method_key: str) -> None:
        """Update state from the args echoed in a Set command's methodreturn.

        The MS-series returns just {"success": true} for write commands;
        the original args (lowercased, colons-only) are in
        ``method_key`` after the verb. Parsing them out here lets the
        driver mirror device state without a follow-up Get on every set.
        """
        # display:matrix:set <input> <output>
        m = re.match(r"^display:matrix:set\s+(\d+)\s+(\d+)$", method_key)
        if m:
            inp, out = int(m.group(1)), int(m.group(2))
            if OUTPUT_MIN <= out <= OUTPUT_MAX:
                self.set_state(f"route_{out}", inp)
            return

        # display:input:set <input>
        m = re.match(r"^display:input:set\s+(\d+)$", method_key)
        if m:
            self.set_state("active_input", int(m.group(1)))
            return

        # display:matrix:mode:set <0|1|2>
        m = re.match(r"^display:matrix:mode:set\s+(\d+)$", method_key)
        if m:
            self.set_state("matrix_mode", int(m.group(1)) != 0)
            return

        # display:minimal:set <0|1>
        m = re.match(r"^display:minimal:set\s+([01])$", method_key)
        if m:
            self.set_state("display_power", m.group(1) == "1")
            return

        # audio:volume:set <-?N> — the doc shows "audio: volume: set - 10"
        # with a space between the sign and the digits; tolerate both.
        m = re.match(r"^audio:volume:set\s+(-?\s*\d+)$", method_key)
        if m:
            level = int(m.group(1).replace(" ", ""))
            self.set_state("volume", level)
            return

        # audio:mute:set <hdmi|analog> <true|false>
        m = re.match(r"^audio:mute:set\s+(hdmi|analog)\s+(true|false)$", method_key)
        if m:
            self.set_state(f"mute_{m.group(1)}", m.group(2) == "true")
            return

        # audio:setsource <digital|analog>
        m = re.match(r"^audio:setsource\s+(digital|analog)$", method_key)
        if m:
            self.set_state("audio_source", m.group(1))
            return

        # usbrouting:mode:set <follow|manual>
        m = re.match(r"^usbrouting:mode:set\s+(follow|manual)$", method_key)
        if m:
            self.set_state("usb_mode", m.group(1))
            return

        # usbrouting:input:set <input>
        m = re.match(r"^usbrouting:input:set\s+(\d+)$", method_key)
        if m:
            self.set_state("usb_route", int(m.group(1)))
            return
