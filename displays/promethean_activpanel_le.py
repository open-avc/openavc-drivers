"""
OpenAVC Promethean ActivPanel LE driver (RS232 control codes over serial).

Controls the Promethean ActivPanel LE interactive displays with
Promethean's RS232 control codes on the panel's RS-232 port (115200 8N1),
on a serial port of the OpenAVC server or through an IP-to-serial bridge.
Promethean documents no network control for the LE: its control-codes
article covers RS232 over Serial only.

Why Python: every command and reply is a binary frame, ``F6 <command>
<type> <data...> <checksum> 6F``, with a one-byte additive checksum and no
length field. The model, serial number and firmware replies are text of
any length, which can hold 0x6F (a lowercase "o") and can checksum to 0x6F,
so neither delimiter nor fixed-length framing can split the stream: a frame
ends at the first 0x6F whose preceding byte is the checksum of everything
before it, which takes code.

Push vs poll: POLL. The control codes are request/response only; nothing
in Promethean's documents describes the panel sending anything it was not
asked for. Gets are answered with the same command and type bytes as the
question, so each reply is matched to its question by those two bytes.

Login: none. The article gives the line settings and the commands, and no
control password.

Power model (article 2905): 01 on, 02 standby, 03 sleep. Power On, Set
Standby and Set Shutdown (Sleep) are all serial commands, and serial is the
LE's only control port, so all three work wherever the panel is reached.
Power Off is Standby. A bridged device is connected over TCP to the bridge's
pass-through, so nothing here depends on the transport.

Model check: the ActivPanel 9, 10 and LX answer the same frames, with input
codes that mean different inputs (09 is HDMI 1 on an ActivPanel 10 and
HDMI (Front) here), and the ActivPanel 9's power-status query can lock its
power up (Promethean article 2586). This driver asks the model number at
connect and refuses any other ActivPanel.

Protocol reference: Promethean support article 2905, "RS232 Control Codes
for ActivPanel LE" (11 November 2025). The frame codec below is the same as
the one in the ActivPanel 10 driver (``promethean_activpanel10``), copied
because a driver is installed on its own.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import CallableFrameParser, FrameParser
from openavc.utils.logger import get_logger

try:
    from openavc.core.connection_fault import ConnectionFaultError
except ImportError:  # pragma: no cover - every supported platform has it
    ConnectionFaultError = None

log = get_logger(__name__)

HEAD = 0xF6
END = 0x6F
#: The longest frame the article shows is the 13-byte firmware reply; a
#: model or serial number may run longer. A buffer this long with no valid
#: end is noise, and the parser drops its first byte to resync.
MAX_FRAME = 64

#: How long a get waits for its reply.
REPLY_TIMEOUT_S = 1.5


# ── Framing (module level so tests/protocol_math can reach it) ───────────────

def checksum(data: bytes) -> int:
    """The control codes' checksum: the sum of every byte before it, mod 256.

    Article 2905: "CS = F6+[Command]+[Type]+[Data]"; the firmware-version
    example (``F6 03 01 33 2E 32 2E 30 2E 31 34 7E 6F``) shows the sum runs
    over every data byte when the data is longer than one.
    """
    return sum(data) & 0xFF


def build_frame(body: bytes) -> bytes:
    """Frame a command body (command, type, data...) for the wire."""
    head = bytes([HEAD]) + bytes(body)
    return head + bytes([checksum(head), END])


def _frame_end(buffer: bytes, start: int) -> int:
    """Index of the end code of a valid frame whose head is at ``start``, or -1.

    The end is the first 0x6F whose preceding byte is the checksum of every
    byte from the head up to it. The shortest frame is six bytes (head,
    command, type, data, checksum, end).
    """
    total = 0
    for i in range(start, min(len(buffer), start + MAX_FRAME)):
        if i - start >= 5 and buffer[i] == END and buffer[i - 1] == (total - buffer[i - 1]) & 0xFF:
            return i
        total += buffer[i]
    return -1


def parse_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Split one frame off the front of ``buffer`` (the CallableFrameParser contract).

    Returns ``(frame, rest)`` with the frame whole (head to end code), or
    ``(None, buffer)`` to wait for more bytes, or ``(None, shorter)`` to drop
    bytes no frame can use: anything before a head byte, a frame whose
    checksum failed (once a later head byte starts a whole frame), or a head
    byte with no valid end within MAX_FRAME bytes.
    """
    start = buffer.find(bytes([HEAD]))
    if start < 0:
        return None, b""
    if start:
        buffer = buffer[start:]
    end = _frame_end(buffer, 0)
    if end >= 0:
        return buffer[: end + 1], buffer[end + 1:]
    if len(buffer) >= MAX_FRAME:
        return None, buffer[1:]
    # No end yet: either the frame is still arriving, or it was corrupt and
    # a whole frame already follows it. Skip to that one rather than wait.
    later = buffer.find(bytes([HEAD]), 1)
    while later > 0:
        if _frame_end(buffer, later) >= 0:
            return None, buffer[later:]
        later = buffer.find(bytes([HEAD]), later + 1)
    return None, buffer


def frame_parts(frame: bytes) -> tuple[int, int, bytes] | None:
    """(command, type, data) of a whole frame, or None if it is not one."""
    if len(frame) < 6 or frame[0] != HEAD or frame[-1] != END:
        return None
    if frame[-2] != checksum(frame[:-2]):
        return None
    return frame[1], frame[2], bytes(frame[3:-2])


# ── Wire tables (article 2905) ──────────────────────────────────────────────

POWER_STATES = {0x01: "on", 0x02: "standby", 0x03: "sleep"}

# The LE's own input table: the same codes mean other inputs on the
# ActivPanel 9 and 10.
INPUT_CODES = {
    "hdmi_front": 0x09,
    "hdmi_1": 0x0A,
    "hdmi_2": 0x13,
    "usb_c_front": 0x0C,
    "usb_c_2": 0x0D,
    "displayport": 0x14,
    "ops": 0x12,
    "android": 0x0B,
}
INPUT_NAMES = {code: name for name, code in INPUT_CODES.items()}

# Gets that read one data byte, by (command, type).
GET_POWER = (0x01, 0x02)
GET_MUTE = (0x02, 0x02)
GET_VOLUME = (0x0C, 0x02)
GET_INPUT = (0x30, 0x02)

# Identity, text replies (hexadecimal ASCII).
GET_SCALER_FW = (0x03, 0x01)
GET_TOUCH_FW = (0x03, 0x02)
GET_SERIAL = (0x03, 0x04)
GET_MODEL = (0x03, 0x05)
_TEXT_GETS = {
    GET_SCALER_FW: "firmware_version",
    GET_TOUCH_FW: "touch_firmware_version",
    GET_SERIAL: "serial_number",
    GET_MODEL: "model",
}

# Everything polled while the panel is on, after the power read.
_POLL_GETS = [GET_INPUT, GET_VOLUME, GET_MUTE]

#: A model number of another ActivPanel: any "AP..." that is not "APLE"
#: (AP10-, AP9-, APLX-, the Elements series' AP7-, which article 1477 also
#: prints for the 86-inch ActivPanel 9, and the older AP6-, AP5-, APD1-),
#: or "ActivPanel <name>" for any name but LE.
_OTHER_PANEL = re.compile(r"^(?:AP(?!LE)|ACTIVPANEL\s*(?!\s|LE))", re.IGNORECASE)


def _text(data: bytes) -> str:
    return "".join(chr(b) for b in data if 32 <= b < 127).strip()


class PrometheanActivPanelLEDriver(BaseDriver):
    """Promethean ActivPanel LE RS232 control codes driver."""

    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = (
        "The panel stopped answering on its RS-232 port. Check that it has "
        "mains power, the cable, and the serial settings (115200 baud, 8 "
        "data bits, no parity, 1 stop bit)."
    )

    DRIVER_INFO = {
        "id": "promethean_activpanel_le",
        "name": "Promethean ActivPanel LE",
        "manufacturer": "Promethean",
        "category": "display",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls the Promethean ActivPanel LE interactive displays over "
            "RS-232, on a serial port or through an IP-to-serial bridge. "
            "Power, standby and sleep, input select, volume and mute, and the "
            "model, serial number and firmware the panel reports. Polls for "
            "status."
        ),
        "source_url": "https://support.prometheanworld.com/s/article/2905",
        "tags": ["display", "promethean", "activpanel", "activpanel-le", "ifp", "interactive-display", "rs232", "serial", "bridge", "education"],
        "verified": False,
        "simulated": True,
        "ports": [],
        "min_platform_version": "0.36.0",
        "transport": "serial",
        "compatible_models": [
            {
                "manufacturer": "Promethean",
                "models": ["APLE-65", "APLE-75", "APLE-86"],
                "confidence": "untested",
                "notes": "ActivPanel LE. RS-232 only, 115200 8N1, on the 9-pin port labelled RS232 at the back of the panel (underside, behind the left side).",
            },
        ],
        "help": {
            "overview": (
                "Promethean ActivPanel LE interactive displays, controlled over "
                "RS-232. Power (on, standby and sleep), input, volume and mute, "
                "and the model, serial number and firmware the panel reports. "
                "Polls every 15 seconds. Promethean documents RS-232 control "
                "only for the LE: wire it to a serial port on the OpenAVC "
                "server, or to an IP-to-serial bridge."
            ),
            "setup": (
                "1. Wire the panel's RS232 port, a 9-pin D-sub at the back of "
                "the panel on the underside behind the left side. Pin 2 is the "
                "panel's receive, pin 3 its transmit and pin 5 ground: the "
                "controller's transmit goes to pin 2 and its receive to pin 3.\n"
                "2. Direct serial: set the Serial Port (for example COM3 on "
                "Windows or /dev/ttyUSB0 on Linux). Through a bridge: add the "
                "bridge first, then choose Through a bridge in this device's "
                "Connection settings and pick the port the panel is wired to.\n"
                "3. Serial settings: 115200 baud, 8 data bits, no parity, 1 "
                "stop bit. They are this driver's defaults; through a bridge, "
                "OpenAVC applies them to the bridge's port.\n"
                "4. No username or password is needed.\n"
                "5. If the panel does not answer with the wiring and settings "
                "right, update it to its latest firmware: Promethean notes that "
                "some earlier versions do not support every RS-232 command."
            ),
            "connection": (
                "The panel answers on its RS-232 port only. If it shows "
                "offline, check that it has mains power, the wiring (the "
                "controller's transmit to pin 2, its receive to pin 3), and "
                "the serial settings: 115200 baud, 8 data bits, no parity, 1 "
                "stop bit. Through a bridge, check that the bridge's own card "
                "shows connected."
            ),
        },
        "default_config": {
            "port": "",
            "baudrate": 115200,
            "parity": "N",
            "bytesize": 8,
            "stopbits": 1,
            "poll_interval": 15,
        },
        "config_schema": {
            "port": {
                "type": "string", "required": True, "label": "Serial Port",
                "description": "The serial port the panel is wired to, for example COM3 (Windows) or /dev/ttyUSB0 (Linux).",
            },
            "baudrate": {
                "type": "integer", "default": 115200, "label": "Baud Rate",
                "description": "115200 for the ActivPanel LE.",
            },
            "poll_interval": {
                "type": "integer", "default": 15, "min": 5,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum", "values": ["on", "standby", "sleep"],
                "label": "Power", "control": True,
                "help": "on, standby, or sleep.",
            },
            "input": {
                "type": "enum",
                "values": ["hdmi_front", "hdmi_1", "hdmi_2", "usb_c_front", "usb_c_2", "displayport", "ops", "android", "other"],
                "label": "Input", "control": True,
                "help": "Active input. 'other' is a code this driver does not know.",
            },
            "volume": {
                "type": "integer", "label": "Volume", "control": True,
                "min": 0, "max": 100, "step": 1,
                "help": "Speaker volume, 0-100.",
            },
            "mute": {"type": "boolean", "label": "Mute", "control": True, "help": "Audio mute."},
            "model": {"type": "string", "label": "Model", "help": "Model number the panel reports."},
            "serial_number": {"type": "string", "label": "Serial Number", "help": "Serial number the panel reports."},
            "firmware_version": {"type": "string", "label": "Firmware Version", "help": "Scaler (main board) firmware version."},
            "touch_firmware_version": {"type": "string", "label": "Touch Firmware Version", "help": "Touch firmware version."},
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "help": "Turns the panel on from standby or sleep.",
                "sets": {"power": "on"},
                "params": {},
            },
            "power_off": {
                "label": "Power Off",
                "help": "Puts the panel in standby.",
                "sets": {"power": "standby"},
                "params": {},
            },
            "sleep": {
                "label": "Sleep",
                "help": "Puts the panel to sleep (Promethean's Set Shutdown). Power On wakes it.",
                "sets": {"power": "sleep"},
                "params": {},
            },
            "set_input": {
                "label": "Select Input",
                "help": "Switches the panel to an input.",
                "sets": {"input": "{input}"},
                "params": {
                    "input": {
                        "type": "enum", "required": True, "label": "Input",
                        "values": [
                            {"value": "hdmi_front", "label": "HDMI (Front)"},
                            {"value": "hdmi_1", "label": "HDMI 1"},
                            {"value": "hdmi_2", "label": "HDMI 2"},
                            {"value": "usb_c_front", "label": "USB-C (Front)"},
                            {"value": "usb_c_2", "label": "USB-C 2"},
                            {"value": "displayport", "label": "DisplayPort"},
                            {"value": "ops", "label": "OPS"},
                            {"value": "android", "label": "Android"},
                        ],
                    },
                },
            },
            "set_volume": {
                "label": "Set Volume",
                "help": "Sets the speaker volume.",
                "sets": {"volume": "{level}"},
                "params": {"level": {"type": "integer", "required": True, "label": "Volume (0-100)", "min": 0, "max": 100}},
            },
            "volume_up": {"label": "Volume Up", "help": "Raises the volume one step.", "params": {}},
            "volume_down": {"label": "Volume Down", "help": "Lowers the volume one step.", "params": {}},
            "mute_on": {"label": "Mute On", "help": "Mutes the panel's audio.", "sets": {"mute": True}, "params": {}},
            "mute_off": {"label": "Mute Off", "help": "Unmutes the panel's audio.", "sets": {"mute": False}, "params": {}},
            "reset_apps_and_data": {
                "label": "Reset Apps and Data",
                "help": "Resets the panel's apps and their data.",
                "confirm": "Resets the panel's apps and erases their data.",
                "params": {},
            },
            "factory_reset": {
                "label": "Factory Reset",
                "help": "Returns the panel to its factory settings.",
                "confirm": "Erases the panel's settings and returns it to factory defaults.",
                "params": {},
            },
            "send_raw": {
                "label": "Send Raw Command",
                "help": "Sends one control code. Give the command, type and data bytes in hex (for example 02 00 01 for Mute On); the driver adds the head, checksum and end bytes.",
                "params": {
                    "body": {
                        "type": "string", "required": True, "label": "Command bytes (hex)",
                        "pattern": "^\\s*[0-9A-Fa-f]{2}(?:\\s*[0-9A-Fa-f]{2}){2,30}\\s*$",
                    },
                },
            },
        },
        "quick_actions": ["power_on", "power_off", "mute_on", "mute_off"],
        "protocols": ["promethean_rs232"],
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any):
        super().__init__(device_id, config, state, events)
        self._waiters: dict[tuple[int, int], list[asyncio.Future]] = {}
        # Gets this panel left unanswered while it was on, this connection,
        # counted so one slow reply does not drop a value for good.
        self._misses: dict[tuple[int, int], int] = {}
        self._model_checked = False

    # ── Framing hooks ────────────────────────────────────────────────────────

    def _create_frame_parser(self) -> Optional[FrameParser]:
        return CallableFrameParser(parse_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        return None

    # ── Send / request ───────────────────────────────────────────────────────

    async def _send(self, body: bytes) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(build_frame(body))

    async def _request(self, key: tuple[int, int], timeout: float | None = None) -> bytes:
        """Send a get and wait for the reply carrying the same command and type."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, []).append(fut)
        try:
            await self._send(bytes([key[0], key[1], 0x00]))
            return await asyncio.wait_for(fut, timeout or REPLY_TIMEOUT_S)
        finally:
            waiters = self._waiters.get(key, [])
            if fut in waiters:
                waiters.remove(fut)

    async def _read(self, key: tuple[int, int]) -> bytes | None:
        """A get whose silence is not a fault: None when it goes unanswered."""
        try:
            return await self._request(key)
        except asyncio.TimeoutError:
            return None

    async def _set(self, body: bytes, read_back: tuple[int, int] | None = None) -> bool:
        """Send a set, then read the value back so state follows the panel."""
        await self._send(body)
        if read_back is not None:
            await self._read(read_back)
        return True

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _initial_sync(self) -> None:
        self._misses.clear()
        self._model_checked = False
        await self._check_model()
        await self._read_identity()
        if not self.config.get("poll_interval", 0) > 0:
            await self.poll()

    async def _check_model(self) -> None:
        """Ask the model, and refuse a panel this driver is not for.

        Another ActivPanel answers the same frames with its own input codes,
        and the ActivPanel 9's power-status query can lock its power up
        (Promethean article 2586), so the model is settled before the first
        power poll whenever the panel answers it.
        """
        model = await self._read(GET_MODEL)
        if model is None:
            log.info(f"[{self.device_id}] The panel did not report its model")
            return
        self._model_checked = True
        text = _text(model)
        if _OTHER_PANEL.match(text):
            message = (
                f"This panel reports model {text}, which is not an ActivPanel "
                f"LE. This driver is for the ActivPanel LE: choose the driver "
                f"for your panel."
            )
            if ConnectionFaultError is not None:
                raise ConnectionFaultError(message, code="invalid_config")
            raise ConnectionError(message)

    async def _read_identity(self) -> None:
        """Serial number and firmware: read once a connection, and again the
        first time the panel is on if a panel in standby left them
        unanswered."""
        for key in (GET_SERIAL, GET_SCALER_FW, GET_TOUCH_FW):
            await self._read(key)

    async def poll(self) -> None:
        if not self._model_checked:
            await self._check_model()
        # Power first: the one question a panel answers in every state (the
        # article lists Sleep among its replies). Its silence means the link
        # is gone.
        was_on = self.get_state("power") == "on"
        await self._request(GET_POWER)
        if self.get_state("power") != "on":
            return
        if not was_on and not self.get_state("serial_number"):
            await self._read_identity()
        for key in _POLL_GETS:
            if self._misses.get(key, 0) >= 2:
                continue
            if await self._read(key) is None:
                self._misses[key] = self._misses.get(key, 0) + 1
                if self._misses[key] == 2:
                    log.info(
                        f"[{self.device_id}] No reply to get {key[0]:02X} {key[1]:02X} "
                        f"twice; not asked again until the panel next comes on"
                    )
            else:
                self._misses.pop(key, None)

    async def _liveness_probe(self) -> None:
        await self._request(GET_POWER)

    # ── Receive ──────────────────────────────────────────────────────────────

    async def on_data_received(self, data: bytes) -> None:
        parts = frame_parts(data)
        if parts is None:
            return
        command, ftype, payload = parts
        key = (command, ftype)
        self._apply_reply(key, payload)
        # Every waiter on this question gets the answer: a poll and a
        # command's read-back can ask the same get at once.
        for fut in self._waiters.get(key, []):
            if not fut.done():
                fut.set_result(payload)

    def _apply_reply(self, key: tuple[int, int], data: bytes) -> None:
        if not data:
            return
        value = data[0]
        if key == GET_POWER:
            state = POWER_STATES.get(value)
            if state:
                if state == "on" and self.get_state("power") != "on":
                    # Ask everything again: a panel coming on may answer
                    # what it skipped in standby.
                    self._misses.clear()
                self.set_state("power", state)
        elif key == GET_INPUT:
            self.set_state("input", INPUT_NAMES.get(value, "other"))
        elif key == GET_VOLUME:
            self.set_state("volume", value)
        elif key == GET_MUTE:
            self.set_state("mute", value == 0x01)
        elif key in _TEXT_GETS:
            text = _text(data)
            if text:
                self.set_state(_TEXT_GETS[key], text)

    # ── Commands ─────────────────────────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        simple = {
            "power_on": (bytes([0x01, 0x01, 0x01]), None),
            "power_off": (bytes([0x01, 0x01, 0x00]), GET_POWER),
            "sleep": (bytes([0x01, 0x05, 0x00]), None),
            "volume_up": (bytes([0x0C, 0x00, 0x01]), None),
            "volume_down": (bytes([0x0C, 0x00, 0x00]), None),
            "mute_on": (bytes([0x02, 0x00, 0x01]), GET_MUTE),
            "mute_off": (bytes([0x02, 0x00, 0x00]), GET_MUTE),
            "reset_apps_and_data": (bytes([0x3B, 0x01, 0x01]), None),
            "factory_reset": (bytes([0x03, 0x06, 0x00]), None),
        }
        # No power read straight after Power On or Sleep: on the ActivPanel 9
        # that read can lock the panel's power up (article 2586), and the LE's
        # article does not say whether it shares the fault. Volume steps and
        # the resets are left to the next poll too.
        if command in simple:
            body, read_back = simple[command]
            return await self._set(body, read_back)

        if command == "set_input":
            name = str(params["input"])
            if name not in INPUT_CODES:
                raise ValueError(f"Unknown input '{name}'")
            return await self._set(bytes([0x30, 0x01, INPUT_CODES[name]]), GET_INPUT)

        if command == "set_volume":
            return await self._set(bytes([0x0C, 0x01, self._level(params["level"])]), GET_VOLUME)

        if command == "send_raw":
            body = bytes.fromhex(str(params["body"]))
            if len(body) < 3:
                raise ValueError("Give at least the command, type and data bytes")
            await self._send(body)
            return True

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    @staticmethod
    def _level(value: Any) -> int:
        level = int(value)
        if not 0 <= level <= 100:
            raise ValueError("Level must be 0-100")
        return level
