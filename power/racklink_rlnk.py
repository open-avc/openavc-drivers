"""
OpenAVC Middle Atlantic / Legrand RackLink RLNK PDU Driver.

Controls Middle Atlantic / Legrand RackLink power distribution units
over the RackLink Control Protocol on TCP port 60000. The protocol is
a length-framed binary protocol with header 0xFE, tail 0xFF, an escape
byte 0xFD, and a 7-bit additive checksum. Authentication is required
on connect (``username|password``).

Push vs poll:
    Premium and Premium+ models support unsolicited status updates
    (subcommand 0x12). On connect the driver calls Register Status
    Change (0x41) to subscribe to outlet, dry-contact, EPO, and sensor
    changes — those then arrive on demand. Select-series models reject
    the registration with a NACK; the driver simply falls back to its
    polling cycle, which runs on every model as a backstop because
    pushed messages can be missed during reconnects. An initial Get
    sweep on connect populates state up front (push only fires on
    change).

Models covered (all RackLink series — Select, Premium, Premium+):
    Select: RLNK-215, RLNK-415, RLNK-515, RLNK-915
    Premium: RLNK-915R
    Premium+: RLNK-P415, RLNK-P420, RLNK-P420R, RLNK-P915,
              RLNK-P915R, RLNK-P920, RLNK-P920R, RLNK-P-DUO20,
              RLNK-P-DUO20HW

Source:
    https://shop.exertis.no/sv-SE/Product/112284/Link/66906/RLNK-Series-Protocol.pdf
    (RackLink Control System Communication Protocol Manual, Rev G,
    document I-00472.)
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)


HEADER = 0xFE
TAIL = 0xFF
ESCAPE = 0xFD

MAX_OUTLETS = 16
MAX_CONTACTS = 8

# Command codes
CMD_NACK = 0x10
CMD_PING = 0x01
CMD_LOGIN = 0x02
CMD_OUTLET = 0x20
CMD_OUTLET_NAME = 0x21
CMD_OUTLET_COUNT = 0x22
CMD_ENERGY_STATE = 0x23
CMD_CONTACT = 0x30
CMD_CONTACT_NAME = 0x31
CMD_CONTACT_COUNT = 0x32
CMD_SEQUENCE = 0x36
CMD_EPO = 0x37
CMD_REGISTER_STATUS = 0x41
CMD_KILOWATT_HOURS = 0x50
CMD_PEAK_VOLTAGE = 0x51
CMD_RMS_VOLTAGE = 0x52
CMD_PEAK_LOAD = 0x53
CMD_RMS_LOAD = 0x54
CMD_TEMPERATURE = 0x55
CMD_WATTAGE = 0x56
CMD_POWER_FACTOR = 0x57
CMD_THERMAL_LOAD = 0x58
CMD_SURGE_STATE = 0x59
CMD_OCCUPANCY = 0x61
CMD_PART_NUMBER = 0x90
CMD_AMP_RATING = 0x91
CMD_SURGE_PRESENT = 0x93
CMD_IP_ADDRESS = 0x94
CMD_MAC_ADDRESS = 0x95

# Subcommands
SUB_SET = 0x01
SUB_GET = 0x02
SUB_RESPONSE = 0x10
SUB_STATUS_CHANGE = 0x12  # unsolicited push from device
SUB_LOG_UPDATE = 0x30

# Outlet desired states
OUTLET_OFF = 0x00
OUTLET_ON = 0x01
OUTLET_CYCLE = 0x02
OUTLET_NOT_CONTROLLABLE = 0x03  # response only

NACK_REASONS = {
    0x01: "Bad CRC on previous command",
    0x02: "Bad length on previous command",
    0x03: "Bad escape sequence on previous command",
    0x04: "Previous command invalid",
    0x05: "Previous sub-command invalid",
    0x06: "Previous command incorrect byte count",
    0x07: "Invalid data bytes in previous command",
    0x08: "Invalid credentials (need to login again)",
    0x10: "Unknown error",
    0x11: "Access denied (EPO)",
}


def _build_state_vars() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        "model": {"type": "string", "label": "Part Number"},
        "ip_address": {"type": "string", "label": "IP Address"},
        "mac_address": {"type": "string", "label": "MAC Address"},
        "amp_rating": {"type": "integer", "label": "Amp Rating", "min": 0},
        "surge_present": {"type": "boolean", "label": "Surge Protection Installed"},
        "outlet_count": {"type": "integer", "label": "Outlet Count", "min": 0},
        "contact_count": {"type": "integer", "label": "Dry Contact Count", "min": 0},
        "epo_active": {"type": "boolean", "label": "Emergency Power Off Active"},
        "kilowatt_hours": {"type": "number", "label": "Kilowatt Hours"},
        "voltage_peak": {"type": "integer", "label": "Peak Voltage (V)"},
        "voltage_rms": {"type": "integer", "label": "RMS Voltage (V)"},
        "load_peak": {"type": "number", "label": "Peak Load (A)"},
        "load_rms": {"type": "number", "label": "RMS Load (A)"},
        "temperature_f": {"type": "integer", "label": "Temperature (°F)"},
        "wattage": {"type": "integer", "label": "Wattage (W)"},
        "power_factor": {"type": "number", "label": "Power Factor"},
        "thermal_load_btu": {"type": "number", "label": "Thermal Load (BTU)"},
        "surge_state": {
            "type": "enum",
            "values": ["unknown", "protected", "compromised"],
            "label": "Surge Protection State",
        },
        "occupancy": {
            "type": "enum",
            "values": ["unoccupied", "occupied"],
            "label": "Occupancy",
        },
    }
    for n in range(1, MAX_OUTLETS + 1):
        out[f"outlet_{n}_state"] = {
            "type": "boolean",
            "label": f"Outlet {n} State",
        }
        out[f"outlet_{n}_name"] = {
            "type": "string",
            "label": f"Outlet {n} Name",
        }
        out[f"outlet_{n}_controllable"] = {
            "type": "boolean",
            "label": f"Outlet {n} Controllable",
        }
    for n in range(1, MAX_CONTACTS + 1):
        out[f"contact_{n}_state"] = {
            "type": "boolean",
            "label": f"Contact {n} Closed",
        }
        out[f"contact_{n}_name"] = {
            "type": "string",
            "label": f"Contact {n} Name",
        }
    return out


def _checksum(unescaped: bytes) -> int:
    """Sum of every byte from header through end of data envelope, low 7 bits."""
    return sum(unescaped) & 0x7F


def _escape(payload: bytes) -> bytes:
    """Insert 0xFD before each protected byte and invert that byte's bits."""
    out = bytearray()
    for b in payload:
        if b in (HEADER, TAIL, ESCAPE):
            out.append(ESCAPE)
            out.append((~b) & 0xFF)
        else:
            out.append(b)
    return bytes(out)


def _unescape(payload: bytes) -> bytes:
    """Remove 0xFD escapes and re-invert escaped bytes."""
    out = bytearray()
    i = 0
    while i < len(payload):
        b = payload[i]
        if b == ESCAPE and i + 1 < len(payload):
            out.append((~payload[i + 1]) & 0xFF)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def build_frame(envelope: bytes) -> bytes:
    """Wrap an unescaped data envelope (Dest + Cmd + Subcmd + Data) into a
    full RackLink frame: ``<Header><Length><envelope><Checksum><Tail>``.
    Length and checksum are computed on the unescaped form, but the
    envelope is escaped before transmission. The header and tail are
    never escaped.
    """
    length = len(envelope)
    if length < 3 or length > 250:
        raise ValueError(f"Envelope length {length} outside 3..250")
    unescaped_for_checksum = bytes([HEADER, length]) + envelope
    cksum = _checksum(unescaped_for_checksum)
    body = _escape(envelope)
    cksum_bytes = _escape(bytes([cksum]))
    return bytes([HEADER, length]) + body + cksum_bytes + bytes([TAIL])


class RackLinkRLNKDriver(BaseDriver):
    """Middle Atlantic / Legrand RackLink RLNK PDU driver."""

    DRIVER_INFO = {
        "id": "racklink_rlnk",
        "name": "Middle Atlantic RackLink PDU",
        "manufacturer": "Middle Atlantic",
        "category": "power",
        "version": "1.1.1",
        "author": "OpenAVC",
        "description": (
            "Controls Middle Atlantic / Legrand RackLink RLNK power "
            "distribution units (Select, Premium, Premium+) over the "
            "RackLink Control Protocol on TCP port 60000. Outlet and "
            "dry-contact control, sequencing, naming, EPO, and live "
            "voltage / current / temperature / wattage telemetry. "
            "Premium models push unsolicited status changes; Select "
            "models fall back to polling."
        ),
        "source_url": "https://shop.exertis.no/sv-SE/Product/112284/Link/66906/RLNK-Series-Protocol.pdf",
        "tags": ["pdu", "power", "outlet-control", "rack", "legrand"],
        "verified": False,
        "simulated": True,
        "protocols": ["racklink-control-protocol"],
        "ports": [60000],
        "transport": "tcp",
        "discovery": {
            "oui_prefixes": ["00:1e:c5"],   # Middle Atlantic Products Inc
        },
        "compatible_models": [
            {
                "manufacturer": "Middle Atlantic",
                "models": [
                    "RLNK-215",
                    "RLNK-415",
                    "RLNK-515",
                    "RLNK-915",
                    "RLNK-915R",
                    "RLNK-P415",
                    "RLNK-P420",
                    "RLNK-P420R",
                    "RLNK-P915",
                    "RLNK-P915R",
                    "RLNK-P920",
                    "RLNK-P920R",
                    "RLNK-P-DUO20",
                    "RLNK-P-DUO20HW",
                ],
                "confidence": "untested",
                "notes": (
                    "All RackLink series share the Control Protocol on "
                    "TCP 60000. Select models support outlet control, "
                    "naming, sequencing, and dry contacts. Premium and "
                    "Premium+ add per-system telemetry (voltage, "
                    "current, wattage, temperature, surge state, "
                    "occupancy), EPO, and pushed status updates."
                ),
            }
        ],
        "help": {
            "overview": (
                "RackLink is the Middle Atlantic (now Legrand AV) "
                "family of network-controlled rack PDUs commonly "
                "installed in commercial AV racks. The driver speaks "
                "the binary RackLink Control Protocol so macros can "
                "switch outlets on / off, cycle outlets to power-cycle "
                "frozen gear, sequence the rack up / down, and (on "
                "Premium / Premium+ units) read live telemetry."
            ),
            "setup": (
                "1. Connect the PDU to your network via Ethernet.\n"
                "2. Find the PDU's IP from the RackLink front panel "
                "or web UI.\n"
                "3. Log into the PDU's web UI as Administrator and "
                "set the passwords for Administrator, User, and "
                "Control Systems accounts. The control protocol is "
                "disabled until those passwords are set.\n"
                "4. On Premium+ models, enable the control protocol "
                "under Device Settings → Network Services → "
                "Control Protocol, and enter a username for an admin "
                "account that has the control protocol enabled.\n"
                "5. Enter the IP, port 60000, and credentials in the "
                "device config in OpenAVC. Default Select / Premium "
                "credentials are user / password (whatever was set in "
                "step 3)."
            ),
        },
        "default_config": {
            "host": "",
            "port": 60000,
            "username": "user",
            "password": "",
            "poll_interval": 60,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 60000,
                "label": "TCP Port",
                "description": "RackLink Control Protocol port — always 60000.",
            },
            "username": {
                "type": "string",
                "required": True,
                "default": "user",
                "label": "Username",
                "description": (
                    "On Select / Premium models, always 'user'. On "
                    "Premium+ models, any account that has the control "
                    "protocol enabled."
                ),
            },
            "password": {
                "type": "string",
                "required": True,
                "default": "",
                "label": "Password",
                "secret": True,
            },
            "poll_interval": {
                "type": "integer",
                "default": 60,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "Backstop poll cycle. Premium models push status "
                    "updates so polling can be infrequent (60 s); "
                    "Select models depend on polling. Set to 0 to "
                    "disable."
                ),
            },
        },
        "state_variables": _build_state_vars(),
        "commands": {
            "outlet_on": {
                "label": "Turn Outlet On",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": MAX_OUTLETS,
                    },
                },
            },
            "outlet_off": {
                "label": "Turn Outlet Off",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": MAX_OUTLETS,
                    },
                },
            },
            "outlet_cycle": {
                "label": "Cycle Outlet",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": MAX_OUTLETS,
                    },
                    "delay": {
                        "type": "integer",
                        "required": False,
                        "min": 0,
                        "max": 3600,
                        "help": "Cycle off-time in seconds (0–3600).",
                    },
                },
                "help": "Power-cycle a single outlet to reset frozen gear.",
            },
            "set_outlet_name": {
                "label": "Rename Outlet",
                "params": {
                    "outlet": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": MAX_OUTLETS,
                    },
                    "name": {"type": "string", "required": True},
                },
            },
            "sequence_up": {
                "label": "Sequence Outlets Up",
                "params": {
                    "delay": {
                        "type": "integer",
                        "required": False,
                        "min": 0,
                        "max": 999,
                        "help": (
                            "Delay between outlets in seconds (0–999). "
                            "Premium+ ignores this and uses configured "
                            "delays — send 0."
                        ),
                    },
                },
                "help": "Sequence the rack up (turn outlets on in order).",
            },
            "sequence_down": {
                "label": "Sequence Outlets Down",
                "params": {
                    "delay": {
                        "type": "integer",
                        "required": False,
                        "min": 0,
                        "max": 999,
                    },
                },
                "help": "Sequence the rack down (turn outlets off in order).",
            },
            "contact_open": {
                "label": "Open Dry Contact",
                "params": {
                    "contact": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": MAX_CONTACTS,
                    },
                },
            },
            "contact_close": {
                "label": "Close Dry Contact",
                "params": {
                    "contact": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": MAX_CONTACTS,
                    },
                },
            },
            "set_contact_name": {
                "label": "Rename Dry Contact",
                "params": {
                    "contact": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": MAX_CONTACTS,
                    },
                    "name": {"type": "string", "required": True},
                },
            },
            "epo_initiate": {
                "label": "Emergency Power Off",
                "params": {},
                "help": (
                    "Initiate Emergency Power Off (Premium / Premium+ "
                    "only). All outlets shut down and the unit "
                    "rejects further outlet commands until recovered."
                ),
            },
            "epo_recover": {
                "label": "Recover from EPO",
                "params": {},
                "help": "Return to normal operation after an EPO event.",
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Re-query everything from the PDU.",
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
        self._buffer = bytearray()
        self._authenticated = False
        self._push_subscribed = False
        super().__init__(device_id, config, state, events)

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 60000))
        username = self.config.get("username", "user") or "user"
        password = self.config.get("password", "") or ""

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=None,
            timeout=5.0,
            name=self.device_id,
        )

        # Send login. We don't block on the response — the auth result
        # comes back through the normal data path and flips
        # self._authenticated.
        try:
            await self._send_login(username, password)
        except (ConnectionError, OSError) as e:
            await self.transport.close()
            self.transport = None
            raise ConnectionError(
                f"RackLink login send failed for {host}:{port}: {e}"
            ) from e

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to RackLink at {host}:{port}")

        # Give the device a beat to authenticate, then subscribe and
        # do an initial sweep. If push subscription fails (Select-only
        # gear), polling still keeps state fresh.
        await asyncio.sleep(0.3)
        try:
            await self._register_status_change()
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

        poll_interval = int(self.config.get("poll_interval", 60))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self._authenticated = False
        self._push_subscribed = False
        self._buffer.clear()
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    # ── Sending ──

    async def _send_envelope(self, envelope: bytes) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(build_frame(envelope))

    async def _send_login(self, username: str, password: str) -> None:
        creds = f"{username}|{password}".encode("utf-8")
        await self._send_envelope(bytes([0x00, CMD_LOGIN, SUB_SET]) + creds)

    async def _send_pong(self) -> None:
        # Ping response: <0xFE><0x03><0x00><0x01><0x10><checksum><0xFF>
        await self._send_envelope(bytes([0x00, CMD_PING, SUB_RESPONSE]))

    async def _register_status_change(self) -> None:
        """Subscribe to all interesting unsolicited push messages.

        The bit fields are documented on pages 19–21 of the
        RackLink protocol manual. We enable every bit that maps to a
        state variable we surface, leaving the reserved / future bits
        zero.
        """
        # Byte 1: outlet status
        b1 = 0b00000001
        # Byte 2: contacts | input | sequence | EPO
        b2 = 0b00001111
        # Byte 3: voltage / load / temperature thresholds
        b3 = 0b01001011
        # Byte 4: min temperature
        b4 = 0b00000001
        # Byte 6: kWh / peak V / RMS V / peak load / RMS load / temp / W
        b6 = 0b01111111
        # Byte 7: power factor / thermal load / log count / surge state
        b7 = 0b00001111
        await self._send_envelope(
            bytes([0x00, CMD_REGISTER_STATUS, SUB_SET, b1, b2, b3, b4, b6, b7])
        )

    async def _send_get(self, cmd: int) -> None:
        await self._send_envelope(bytes([0x00, cmd, SUB_GET]))

    async def _send_get_indexed(self, cmd: int, index: int) -> None:
        await self._send_envelope(bytes([0x00, cmd, SUB_GET, index & 0xFF]))

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "outlet_on":
            await self._set_outlet(int(params["outlet"]), OUTLET_ON, 0)
        elif command == "outlet_off":
            await self._set_outlet(int(params["outlet"]), OUTLET_OFF, 0)
        elif command == "outlet_cycle":
            delay = int(params.get("delay", 0))
            await self._set_outlet(int(params["outlet"]), OUTLET_CYCLE, delay)
        elif command == "set_outlet_name":
            await self._set_outlet_name(
                int(params["outlet"]), str(params["name"])
            )
        elif command == "sequence_up":
            delay = int(params.get("delay", 0))
            await self._sequence(0x01, delay)
        elif command == "sequence_down":
            delay = int(params.get("delay", 0))
            await self._sequence(0x03, delay)
        elif command == "contact_open":
            # Dry contacts are tri-state in the protocol but ON / OFF
            # is the meaningful split for users.
            await self._set_contact(int(params["contact"]), OUTLET_OFF)
        elif command == "contact_close":
            await self._set_contact(int(params["contact"]), OUTLET_ON)
        elif command == "set_contact_name":
            await self._set_contact_name(
                int(params["contact"]), str(params["name"])
            )
        elif command == "epo_initiate":
            await self._send_envelope(bytes([0x00, CMD_EPO, SUB_SET, 0x01]))
        elif command == "epo_recover":
            await self._send_envelope(bytes([0x00, CMD_EPO, SUB_SET, 0x00]))
        elif command == "refresh":
            await self.poll()
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def _set_outlet(self, outlet: int, state: int, cycle_seconds: int) -> None:
        cycle_str = f"{max(0, min(3600, cycle_seconds)):04d}".encode("ascii")
        await self._send_envelope(
            bytes([0x00, CMD_OUTLET, SUB_SET, outlet & 0xFF, state]) + cycle_str
        )

    async def _set_contact(self, contact: int, state: int) -> None:
        cycle_str = b"0000"
        await self._send_envelope(
            bytes([0x00, CMD_CONTACT, SUB_SET, contact & 0xFF, state]) + cycle_str
        )

    async def _set_outlet_name(self, outlet: int, name: str) -> None:
        name_bytes = name.encode("utf-8")[:64]
        await self._send_envelope(
            bytes([0x00, CMD_OUTLET_NAME, SUB_SET, outlet & 0xFF]) + name_bytes
        )

    async def _set_contact_name(self, contact: int, name: str) -> None:
        name_bytes = name.encode("utf-8")[:64]
        await self._send_envelope(
            bytes([0x00, CMD_CONTACT_NAME, SUB_SET, contact & 0xFF]) + name_bytes
        )

    async def _sequence(self, direction: int, delay: int) -> None:
        delay_str = f"{max(0, min(999, delay)):04d}".encode("ascii")
        await self._send_envelope(
            bytes([0x00, CMD_SEQUENCE, SUB_SET, direction]) + delay_str
        )

    # ── Polling ──

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return
        try:
            await self._send_get(CMD_PART_NUMBER)
            await self._send_get(CMD_AMP_RATING)
            await self._send_get(CMD_SURGE_PRESENT)
            await self._send_get(CMD_IP_ADDRESS)
            await self._send_get(CMD_MAC_ADDRESS)
            await self._send_get(CMD_OUTLET_COUNT)
            await self._send_get(CMD_CONTACT_COUNT)
            for n in range(1, MAX_OUTLETS + 1):
                await self._send_get_indexed(CMD_OUTLET, n)
                await self._send_get_indexed(CMD_OUTLET_NAME, n)
            for n in range(1, MAX_CONTACTS + 1):
                await self._send_get_indexed(CMD_CONTACT, n)
                await self._send_get_indexed(CMD_CONTACT_NAME, n)
            for cmd in (
                CMD_KILOWATT_HOURS,
                CMD_PEAK_VOLTAGE,
                CMD_RMS_VOLTAGE,
                CMD_PEAK_LOAD,
                CMD_RMS_LOAD,
                CMD_TEMPERATURE,
                CMD_WATTAGE,
                CMD_POWER_FACTOR,
                CMD_THERMAL_LOAD,
                CMD_SURGE_STATE,
                CMD_OCCUPANCY,
            ):
                await self._send_get(cmd)
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        self._buffer.extend(data)
        for frame in self._extract_frames():
            try:
                self._handle_frame(frame)
            except Exception:  # noqa: BLE001
                log.exception(f"[{self.device_id}] Error handling frame {frame.hex()}")

    def _extract_frames(self) -> list[bytes]:
        """Pull complete frames out of self._buffer, leaving partials behind.

        A frame is bounded by 0xFE..0xFF, with 0xFD escapes skipped over
        (the byte after 0xFD is part of the body and never a tail/header).
        """
        frames: list[bytes] = []
        while True:
            try:
                start = self._buffer.index(HEADER)
            except ValueError:
                self._buffer.clear()
                return frames
            if start > 0:
                del self._buffer[:start]
            i = 1
            end = -1
            while i < len(self._buffer):
                b = self._buffer[i]
                if b == ESCAPE:
                    i += 2
                    continue
                if b == TAIL:
                    end = i
                    break
                i += 1
            if end < 0:
                return frames
            frames.append(bytes(self._buffer[: end + 1]))
            del self._buffer[: end + 1]

    def _handle_frame(self, frame: bytes) -> None:
        if len(frame) < 5 or frame[0] != HEADER or frame[-1] != TAIL:
            return
        # Strip header / tail; unescape body (which is everything between)
        body_escaped = frame[1:-1]
        body = _unescape(body_escaped)
        # body = <Length><envelope (Length bytes)><checksum>
        if len(body) < 2:
            return
        length = body[0]
        if len(body) < 1 + length + 1:
            return
        envelope = body[1 : 1 + length]
        cksum = body[1 + length]
        expected = _checksum(bytes([HEADER, length]) + envelope)
        if cksum != expected:
            log.warning(
                f"[{self.device_id}] Bad checksum: got 0x{cksum:02x}, "
                f"expected 0x{expected:02x} on frame {frame.hex()}"
            )
            return
        if len(envelope) < 3:
            return
        # envelope = Destination + Cmd + Subcmd + Data
        cmd = envelope[1]
        sub = envelope[2]
        data = envelope[3:]
        self._dispatch(cmd, sub, data)

    def _dispatch(self, cmd: int, sub: int, data: bytes) -> None:
        if cmd == CMD_PING and sub == SUB_SET:
            asyncio.create_task(self._send_pong())
            return
        if cmd == CMD_NACK:
            self._handle_nack(data)
            return
        if cmd == CMD_LOGIN and sub == SUB_RESPONSE:
            self._authenticated = bool(data and data[0] == 0x01)
            log.info(
                f"[{self.device_id}] Login {'accepted' if self._authenticated else 'rejected'}"
            )
            return
        if cmd == CMD_REGISTER_STATUS and sub == SUB_RESPONSE:
            self._push_subscribed = True
            log.info(f"[{self.device_id}] Subscribed to status changes")
            return

        # Status change pushes (sub 0x12), responses (0x10), and log
        # updates (0x30) all carry the same payload shape per command.
        is_state_msg = sub in (SUB_RESPONSE, SUB_STATUS_CHANGE, SUB_LOG_UPDATE)
        if not is_state_msg:
            return

        if cmd == CMD_OUTLET:
            self._dispatch_outlet_status(data)
        elif cmd == CMD_OUTLET_NAME:
            self._dispatch_outlet_name(data)
        elif cmd == CMD_OUTLET_COUNT:
            self._dispatch_outlet_count(data)
        elif cmd == CMD_CONTACT:
            self._dispatch_contact_status(data)
        elif cmd == CMD_CONTACT_NAME:
            self._dispatch_contact_name(data)
        elif cmd == CMD_CONTACT_COUNT:
            self._dispatch_contact_count(data)
        elif cmd == CMD_EPO:
            if data:
                self.set_state("epo_active", data[0] == 0x01)
        elif cmd == CMD_KILOWATT_HOURS:
            self._set_float("kilowatt_hours", data)
        elif cmd == CMD_PEAK_VOLTAGE:
            self._set_int("voltage_peak", data)
        elif cmd == CMD_RMS_VOLTAGE:
            self._set_int("voltage_rms", data)
        elif cmd == CMD_PEAK_LOAD:
            self._set_float("load_peak", data)
        elif cmd == CMD_RMS_LOAD:
            self._set_float("load_rms", data)
        elif cmd == CMD_TEMPERATURE:
            self._set_int("temperature_f", data)
        elif cmd == CMD_WATTAGE:
            self._set_int("wattage", data)
        elif cmd == CMD_POWER_FACTOR:
            self._set_float("power_factor", data)
        elif cmd == CMD_THERMAL_LOAD:
            self._set_float("thermal_load_btu", data)
        elif cmd == CMD_SURGE_STATE:
            if data:
                mapping = {0x00: "unknown", 0x01: "protected", 0x02: "compromised"}
                self.set_state("surge_state", mapping.get(data[0], "unknown"))
        elif cmd == CMD_OCCUPANCY:
            if data:
                self.set_state(
                    "occupancy",
                    "occupied" if data[:1] == b"O" else "unoccupied",
                )
        elif cmd == CMD_PART_NUMBER:
            self.set_state("model", data.decode("utf-8", errors="replace").strip())
        elif cmd == CMD_AMP_RATING:
            self._set_int("amp_rating", data)
        elif cmd == CMD_SURGE_PRESENT:
            self.set_state("surge_present", data[:1] == b"Y")
        elif cmd == CMD_IP_ADDRESS:
            self.set_state(
                "ip_address", data.decode("utf-8", errors="replace").strip()
            )
        elif cmd == CMD_MAC_ADDRESS:
            self.set_state(
                "mac_address", data.decode("utf-8", errors="replace").strip()
            )

    def _handle_nack(self, data: bytes) -> None:
        if not data:
            return
        reason = NACK_REASONS.get(data[0], f"reason 0x{data[0]:02x}")
        if data[0] == 0x08:
            self._authenticated = False
            log.warning(
                f"[{self.device_id}] NACK: {reason} — will reconnect"
            )
        else:
            log.debug(f"[{self.device_id}] NACK: {reason}")

    def _dispatch_outlet_status(self, data: bytes) -> None:
        # Format: <Outlet 1..16><State 0x00 OFF / 0x01 ON / 0x02 cycle / 0x03 not-controllable><cycle 4 ASCII>
        if len(data) < 2:
            return
        outlet = data[0]
        if outlet < 1 or outlet > MAX_OUTLETS:
            return
        state = data[1]
        if state == OUTLET_NOT_CONTROLLABLE:
            self.set_state(f"outlet_{outlet}_controllable", False)
            return
        # ON / OFF / cycling — expose as boolean (cycle is transient)
        self.set_state(f"outlet_{outlet}_state", state == OUTLET_ON)
        self.set_state(f"outlet_{outlet}_controllable", True)

    def _dispatch_outlet_name(self, data: bytes) -> None:
        if not data:
            return
        outlet = data[0]
        if outlet < 1 or outlet > MAX_OUTLETS:
            return
        name = data[1:].decode("utf-8", errors="replace").rstrip("\x00")
        self.set_state(f"outlet_{outlet}_name", name)

    def _dispatch_outlet_count(self, data: bytes) -> None:
        # 16 bytes, each "C" / "N" / "X"
        controllable = 0
        for i, b in enumerate(data[:MAX_OUTLETS], start=1):
            ch = bytes([b])
            self.set_state(f"outlet_{i}_controllable", ch == b"C")
            if ch in (b"C", b"N"):
                controllable += 1
        self.set_state("outlet_count", controllable)

    def _dispatch_contact_status(self, data: bytes) -> None:
        if len(data) < 2:
            return
        contact = data[0]
        if contact < 1 or contact > MAX_CONTACTS:
            return
        state = data[1]
        self.set_state(f"contact_{contact}_state", state == OUTLET_ON)

    def _dispatch_contact_name(self, data: bytes) -> None:
        if not data:
            return
        contact = data[0]
        if contact < 1 or contact > MAX_CONTACTS:
            return
        name = data[1:].decode("utf-8", errors="replace").rstrip("\x00")
        self.set_state(f"contact_{contact}_name", name)

    def _dispatch_contact_count(self, data: bytes) -> None:
        present = 0
        for b in data[:MAX_CONTACTS]:
            if bytes([b]) in (b"C", b"N"):
                present += 1
        self.set_state("contact_count", present)

    def _set_int(self, key: str, data: bytes) -> None:
        try:
            self.set_state(key, int(data.decode("ascii", errors="replace").strip() or 0))
        except ValueError:
            pass

    def _set_float(self, key: str, data: bytes) -> None:
        try:
            self.set_state(key, float(data.decode("ascii", errors="replace").strip() or 0))
        except ValueError:
            pass
