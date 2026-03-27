"""
OpenAVC PJLink Class 1 Driver.

Controls any PJLink Class 1 compatible projector (Epson, NEC, Panasonic,
Sony, Hitachi, Christie, and many others).

Protocol spec: https://pjlink.jbmia.or.jp/english/
TCP-based text protocol on port 4352.
Command format: %1<CMD> <param>\r
Response format: %1<CMD>=<response>\r

Features:
- Power on/off with warming/cooling state tracking
- Input selection with auto-discovery (INST)
- Video/audio mute control
- Lamp hours monitoring (multi-lamp)
- Error status monitoring (fan, lamp, temp, cover, filter, other)
- PJLink authentication (MD5 challenge-response)
- Device info queries (manufacturer, product, name, class)
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)


class PJLinkDriver(BaseDriver):
    """PJLink Class 1 projector control driver."""

    DRIVER_INFO = {
        "id": "pjlink_class1",
        "name": "PJLink Class 1 Projector",
        "manufacturer": "Generic",
        "category": "projector",
        "version": "2.0.0",
        "author": "OpenAVC",
        "description": "Controls any PJLink Class 1 compatible projector.",
        "transport": "tcp",
        "discovery": {
            "ports": [4352],
        },
        "help": {
            "overview": (
                "Universal projector control via the PJLink Class 1 standard. "
                "Works with 100+ models from Epson, NEC, Panasonic, Sony, Christie, "
                "Hitachi, BenQ, Optoma, Canon, and Vivitek."
            ),
            "setup": (
                "1. Enable network control on the projector (varies by manufacturer)\n"
                "2. Assign a static IP address to the projector\n"
                "3. If authentication is enabled, set the PJLink password\n"
                "4. Default port is 4352 (PJLink standard)"
            ),
        },
        "default_config": {
            "host": "",
            "port": 4352,
            "password": "",
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 4352, "label": "Port"},
            "password": {
                "type": "string",
                "default": "",
                "label": "Password",
                "secret": True,
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["off", "on", "warming", "cooling"],
                "label": "Power State",
            },
            "input": {
                "type": "string",
                "label": "Input",
            },
            "available_inputs": {
                "type": "string",
                "label": "Available Inputs",
            },
            "mute_video": {"type": "boolean", "label": "Video Mute"},
            "mute_audio": {"type": "boolean", "label": "Audio Mute"},
            "lamp_hours": {"type": "integer", "label": "Lamp Hours"},
            "lamp_count": {"type": "integer", "label": "Number of Lamps"},
            "error_status": {
                "type": "string",
                "label": "Error Status",
            },
            "error_fan": {
                "type": "enum",
                "values": ["ok", "warning", "error"],
                "label": "Fan Status",
            },
            "error_lamp": {
                "type": "enum",
                "values": ["ok", "warning", "error"],
                "label": "Lamp Status",
            },
            "error_temp": {
                "type": "enum",
                "values": ["ok", "warning", "error"],
                "label": "Temperature Status",
            },
            "error_cover": {
                "type": "enum",
                "values": ["ok", "warning", "error"],
                "label": "Cover Status",
            },
            "error_filter": {
                "type": "enum",
                "values": ["ok", "warning", "error"],
                "label": "Filter Status",
            },
            "error_other": {
                "type": "enum",
                "values": ["ok", "warning", "error"],
                "label": "Other Error Status",
            },
            "projector_name": {"type": "string", "label": "Projector Name"},
            "manufacturer": {"type": "string", "label": "Manufacturer"},
            "product_name": {"type": "string", "label": "Product Name"},
            "pjlink_class": {"type": "string", "label": "PJLink Class"},
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "params": {},
                "help": "Turn on the projector. Enters warming state for 30-60 seconds.",
            },
            "power_off": {
                "label": "Power Off",
                "params": {},
                "help": "Turn off the projector. Enters cooling state before fully powering down.",
            },
            "set_input": {
                "label": "Set Input",
                "params": {
                    "input": {
                        "type": "string",
                        "required": True,
                        "help": (
                            "Input source name (e.g. hdmi1, vga1, digital2) "
                            "or PJLink code (e.g. 31, 11)"
                        ),
                    }
                },
                "help": "Switch the projector's active input source.",
            },
            "mute_video": {
                "label": "Video Mute On",
                "params": {},
                "help": "Blank the projected image (lamp stays on).",
            },
            "unmute_video": {
                "label": "Video Mute Off",
                "params": {},
                "help": "Restore the projected image.",
            },
            "mute_audio": {
                "label": "Audio Mute On",
                "params": {},
                "help": "Mute the projector's built-in speaker.",
            },
            "unmute_audio": {
                "label": "Audio Mute Off",
                "params": {},
                "help": "Unmute the projector's built-in speaker.",
            },
            "mute_all": {
                "label": "AV Mute On",
                "params": {},
                "help": "Mute both audio and video.",
            },
            "unmute_all": {
                "label": "AV Mute Off",
                "params": {},
                "help": "Unmute both audio and video.",
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Query all status from the projector immediately.",
            },
        },
    }

    # PJLink input code -> friendly name mapping
    # Type 1x = RGB, 2x = VIDEO, 3x = DIGITAL, 4x = STORAGE, 5x = NETWORK
    INPUT_MAP = {
        # RGB / VGA
        "rgb1": "11", "rgb2": "12", "rgb3": "13",
        "vga1": "11", "vga2": "12",
        # Video (composite, S-Video)
        "video1": "21", "video2": "22", "video3": "23",
        "composite": "21", "svideo": "22",
        # Digital (HDMI, DVI)
        "digital1": "31", "digital2": "32", "digital3": "33",
        "hdmi1": "31", "hdmi2": "32", "hdmi3": "33",
        "dvi": "31",
        # Storage (USB, internal)
        "storage1": "41", "storage2": "42",
        "usb": "41",
        # Network
        "network": "51", "network1": "51", "network2": "52",
    }

    # Reverse mapping: PJLink code -> canonical friendly name
    INPUT_REVERSE = {
        "11": "rgb1", "12": "rgb2", "13": "rgb3",
        "14": "rgb4", "15": "rgb5", "16": "rgb6",
        "17": "rgb7", "18": "rgb8", "19": "rgb9",
        "21": "video1", "22": "video2", "23": "video3",
        "24": "video4", "25": "video5", "26": "video6",
        "27": "video7", "28": "video8", "29": "video9",
        "31": "digital1", "32": "digital2", "33": "digital3",
        "34": "digital4", "35": "digital5", "36": "digital6",
        "37": "digital7", "38": "digital8", "39": "digital9",
        "41": "storage1", "42": "storage2", "43": "storage3",
        "44": "storage4", "45": "storage5", "46": "storage6",
        "47": "storage7", "48": "storage8", "49": "storage9",
        "51": "network1", "52": "network2", "53": "network3",
        "54": "network4", "55": "network5", "56": "network6",
        "57": "network7", "58": "network8", "59": "network9",
    }

    # PJLink power state mapping
    POWER_MAP = {"0": "off", "1": "on", "2": "cooling", "3": "warming"}

    # ERST position names and level codes
    ERROR_POSITIONS = ["fan", "lamp", "temp", "cover", "filter", "other"]
    ERROR_LEVELS = {"0": "ok", "1": "warning", "2": "error"}

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state: "StateStore",
        events: "EventBus",
    ):
        self._auth_prefix = ""
        self._greeting_event = asyncio.Event()
        self._transition_task: asyncio.Task | None = None
        super().__init__(device_id, config, state, events)

    async def connect(self) -> None:
        """Connect to the projector, handle auth, query device info."""
        host = self.config.get("host", "")
        port = self.config.get("port", 4352)
        self._greeting_event.clear()
        self._auth_prefix = ""

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_disconnect,
            delimiter=b"\r",
            timeout=5.0,
            name=self.device_id,
        )

        # Wait for PJLink greeting (with timeout)
        try:
            await asyncio.wait_for(self._greeting_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning(
                f"[{self.device_id}] No PJLink greeting received, proceeding without auth"
            )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(
            f"[{self.device_id}] Connected to PJLink projector at {host}:{port}"
        )

        # Query device info (name, manufacturer, product, class, available inputs)
        await self._query_device_info()

        # Start polling
        poll_interval = self.config.get("poll_interval", 15)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        """Disconnect from the projector."""
        if self._transition_task and not self._transition_task.done():
            self._transition_task.cancel()
            try:
                await self._transition_task
            except asyncio.CancelledError:
                pass
            self._transition_task = None
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    # --- Internal helpers ---

    async def _send_pjlink(self, cmd: str) -> None:
        """Send a PJLink command with optional auth prefix."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        full_cmd = f"{self._auth_prefix}{cmd}\r"
        await self.transport.send(full_cmd.encode("ascii"))

    async def _query_device_info(self) -> None:
        """Query manufacturer, product, name, class, and available inputs."""
        try:
            for cmd in ["%1NAME ?", "%1INF1 ?", "%1INF2 ?", "%1CLSS ?", "%1INST ?"]:
                await self._send_pjlink(cmd)
                await asyncio.sleep(0.2)
        except ConnectionError:
            log.warning(f"[{self.device_id}] Failed to query device info")

    def _start_transition_monitor(self) -> None:
        """Start fast polling during power transitions to track warming/cooling."""
        if self._transition_task and not self._transition_task.done():
            self._transition_task.cancel()

        async def _monitor():
            try:
                # Poll power every 2 seconds for up to 60 seconds
                for _ in range(30):
                    await asyncio.sleep(2.0)
                    power = self.get_state("power")
                    if power in ("on", "off"):
                        log.info(
                            f"[{self.device_id}] Power transition complete: {power}"
                        )
                        return
                    await self._send_pjlink("%1POWR ?")
                log.warning(
                    f"[{self.device_id}] Power transition monitor timed out"
                )
            except (asyncio.CancelledError, ConnectionError):
                pass

        self._transition_task = asyncio.create_task(_monitor())

    # --- Command interface ---

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a named command to the projector."""
        params = params or {}

        match command:
            case "power_on":
                self.set_state("power", "warming")
                await self._send_pjlink("%1POWR 1")
                await asyncio.sleep(0.1)
                await self._send_pjlink("%1POWR ?")
                self._start_transition_monitor()
            case "power_off":
                self.set_state("power", "cooling")
                await self._send_pjlink("%1POWR 0")
                await asyncio.sleep(0.1)
                await self._send_pjlink("%1POWR ?")
                self._start_transition_monitor()
            case "set_input":
                input_name = params.get("input", "")
                input_code = self.INPUT_MAP.get(input_name.lower(), input_name)
                if len(input_code) == 2 and input_code.isdigit():
                    await self._send_pjlink(f"%1INPT {input_code}")
                else:
                    log.warning(
                        f"[{self.device_id}] Unknown input: {input_name}"
                    )
            case "mute_video":
                await self._send_pjlink("%1AVMT 11")
            case "unmute_video":
                await self._send_pjlink("%1AVMT 10")
            case "mute_audio":
                await self._send_pjlink("%1AVMT 21")
            case "unmute_audio":
                await self._send_pjlink("%1AVMT 20")
            case "mute_all":
                await self._send_pjlink("%1AVMT 31")
            case "unmute_all":
                await self._send_pjlink("%1AVMT 30")
            case "refresh":
                await self.poll()
            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

    # --- Data parsing ---

    async def on_data_received(self, data: bytes) -> None:
        """Parse PJLink responses and update state."""
        response = data.decode("ascii", errors="ignore").strip()

        # Handle PJLink greeting
        if response.startswith("PJLINK"):
            self._parse_greeting(response)
            return

        # Handle auth error
        if response == "PJLINK ERRA":
            log.error(
                f"[{self.device_id}] PJLink authentication failed — check password"
            )
            return

        # Log OK acknowledgements
        if response.endswith("=OK"):
            log.info(f"[{self.device_id}] Response: {response}")
            return

        # Handle error responses with context
        if "=ERR" in response:
            self._handle_error_response(response)
            return

        # Find the %1 prefix (may be preceded by auth hash)
        pj_idx = response.find("%1")
        if pj_idx == -1 or "=" not in response[pj_idx:]:
            return

        # Parse: %1CODE=value
        after_prefix = response[pj_idx + 2:]
        code_part, value = after_prefix.split("=", 1)

        if code_part == "POWR":
            power_state = self.POWER_MAP.get(value, "unknown")
            old_power = self.get_state("power")
            self.set_state("power", power_state)
            if power_state != old_power:
                log.info(f"[{self.device_id}] Power: {power_state}")

        elif code_part == "INPT":
            input_name = self.INPUT_REVERSE.get(value, f"input_{value}")
            old_input = self.get_state("input")
            self.set_state("input", input_name)
            if input_name != old_input:
                log.info(f"[{self.device_id}] Input: {input_name}")

        elif code_part == "AVMT":
            if value in ("11", "31"):
                self.set_state("mute_video", True)
            elif value in ("10", "30"):
                self.set_state("mute_video", False)
            if value in ("21", "31"):
                self.set_state("mute_audio", True)
            elif value in ("20", "30"):
                self.set_state("mute_audio", False)

        elif code_part == "LAMP":
            # Format: "hours1 on1 hours2 on2 ..." (multi-lamp support)
            parts = value.split()
            if len(parts) >= 2:
                try:
                    self.set_state("lamp_hours", int(parts[0]))
                    lamp_count = len(parts) // 2
                    self.set_state("lamp_count", lamp_count)
                    for i in range(lamp_count):
                        hours = int(parts[i * 2])
                        self.set_state(f"lamp{i + 1}_hours", hours)
                except (ValueError, IndexError):
                    pass

        elif code_part == "ERST":
            self._parse_error_status(value)

        elif code_part == "NAME":
            self.set_state("projector_name", value)
            log.info(f"[{self.device_id}] Projector name: {value}")

        elif code_part == "INF1":
            self.set_state("manufacturer", value)
            log.info(f"[{self.device_id}] Manufacturer: {value}")

        elif code_part == "INF2":
            self.set_state("product_name", value)
            log.info(f"[{self.device_id}] Product: {value}")

        elif code_part == "CLSS":
            self.set_state("pjlink_class", value)
            log.info(f"[{self.device_id}] PJLink Class: {value}")

        elif code_part == "INST":
            self._parse_available_inputs(value)

    def _parse_greeting(self, response: str) -> None:
        """Parse PJLink greeting and set up authentication if needed."""
        parts = response.split()
        if len(parts) >= 3 and parts[1] == "1":
            # Auth required: PJLINK 1 <random>
            random_key = parts[2]
            password = self.config.get("password", "")
            if password:
                digest = hashlib.md5(
                    (random_key + password).encode("ascii")
                ).hexdigest()
                self._auth_prefix = digest
                log.info(f"[{self.device_id}] PJLink authentication enabled")
            else:
                log.warning(
                    f"[{self.device_id}] Projector requires authentication "
                    "but no password configured"
                )
        else:
            log.debug(
                f"[{self.device_id}] PJLink greeting: no auth required"
            )
        self._greeting_event.set()

    def _handle_error_response(self, response: str) -> None:
        """Handle PJLink error responses with appropriate log levels."""
        pj_idx = response.find("%1")
        if pj_idx == -1:
            return
        after_prefix = response[pj_idx + 2:]
        if "=" not in after_prefix:
            return

        code_part, error = after_prefix.split("=", 1)
        power_state = self.get_state("power")

        # ERR2 on INPT/AVMT when powered off is expected — don't spam warnings
        if (
            error == "ERR2"
            and code_part in ("INPT", "AVMT")
            and power_state in ("off", "cooling", "warming", None)
        ):
            log.debug(
                f"[{self.device_id}] {code_part} unavailable (projector not on)"
            )
            return

        # ERR3 = unavailable time (during transitions) — expected
        if error == "ERR3":
            log.debug(
                f"[{self.device_id}] {code_part} temporarily unavailable"
            )
            return

        # ERR4 = projector/display failure
        if error == "ERR4":
            log.error(
                f"[{self.device_id}] Projector reports failure on {code_part}"
            )
        else:
            log.warning(f"[{self.device_id}] Error response: {response}")

    def _parse_error_status(self, value: str) -> None:
        """Parse ERST response into individual error state variables."""
        # ERST format: 6 chars, each 0=ok, 1=warning, 2=error
        # Positions: fan, lamp, temp, cover, filter, other
        issues = []
        for i, name in enumerate(self.ERROR_POSITIONS):
            if i < len(value):
                level = self.ERROR_LEVELS.get(value[i], "unknown")
                self.set_state(f"error_{name}", level)
                if level != "ok":
                    issues.append(f"{name}:{level}")

        # Summary string
        self.set_state("error_status", ", ".join(issues) if issues else "ok")

    def _parse_available_inputs(self, value: str) -> None:
        """Parse INST response into available input list."""
        codes = value.split()
        input_names = []
        for code in codes:
            name = self.INPUT_REVERSE.get(code.strip(), f"input_{code.strip()}")
            input_names.append(name)
        self.set_state("available_inputs", ", ".join(input_names))
        log.info(
            f"[{self.device_id}] Available inputs: {', '.join(input_names)}"
        )

    # --- Polling ---

    async def poll(self) -> None:
        """Query status, skipping input/mute when projector is off."""
        if not self.transport or not self.transport.connected:
            return

        try:
            # Always query power
            await self._send_pjlink("%1POWR ?")
            await asyncio.sleep(0.2)

            # Only query input/mute when projector is on
            power = self.get_state("power")
            if power == "on":
                await self._send_pjlink("%1INPT ?")
                await asyncio.sleep(0.2)
                await self._send_pjlink("%1AVMT ?")
                await asyncio.sleep(0.2)

            # Lamp and errors can be queried any time
            await self._send_pjlink("%1LAMP ?")
            await asyncio.sleep(0.2)
            await self._send_pjlink("%1ERST ?")
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")

    # --- Disconnect handler ---

    def _handle_disconnect(self) -> None:
        """Called by the transport when the connection is lost."""
        if self._transition_task and not self._transition_task.done():
            self._transition_task.cancel()
        self._connected = False
        self.set_state("connected", False)
        log.warning(f"[{self.device_id}] Connection lost")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self.events.emit(f"device.disconnected.{self.device_id}")
            )
        except RuntimeError:
            pass
