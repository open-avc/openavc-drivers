"""
Middle Atlantic / Legrand RackLink RLNK PDU — Simulator.

Implements the RackLink Control Protocol server side on TCP 60000:

  - Length-framed binary protocol with header 0xFE, tail 0xFF, escape
    byte 0xFD, 7-bit additive checksum.
  - Accepts the login command and responds with "accepted" for any
    credentials (matches Select / Premium "user" account behavior; the
    real password check is the authoritative concern of a deployed
    unit, not of an integration-test sim).
  - Honours Register Status Change (0x41) — when a client subscribes,
    the sim pushes 0x12 status changes whenever a relevant state
    variable mutates.
  - Models a configurable outlet count (default 8 for an RLNK-915-shape
    unit) and 4 dry contacts, plus the full set of Premium telemetry
    sensors with believable defaults.

Driver side: ``power/racklink_rlnk.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


HEADER = 0xFE
TAIL = 0xFF
ESCAPE = 0xFD

DEFAULT_OUTLETS = 8
DEFAULT_CONTACTS = 4
MAX_OUTLETS = 16
MAX_CONTACTS = 8

# Command codes (mirror of driver constants)
CMD_NACK = 0x10
CMD_PING = 0x01
CMD_LOGIN = 0x02
CMD_OUTLET = 0x20
CMD_OUTLET_NAME = 0x21
CMD_OUTLET_COUNT = 0x22
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

SUB_SET = 0x01
SUB_GET = 0x02
SUB_RESPONSE = 0x10
SUB_STATUS_CHANGE = 0x12

OUTLET_OFF = 0x00
OUTLET_ON = 0x01
OUTLET_CYCLE = 0x02


def _checksum(unescaped: bytes) -> int:
    return sum(unescaped) & 0x7F


def _escape(payload: bytes) -> bytes:
    out = bytearray()
    for b in payload:
        if b in (HEADER, TAIL, ESCAPE):
            out.append(ESCAPE)
            out.append((~b) & 0xFF)
        else:
            out.append(b)
    return bytes(out)


def _unescape(payload: bytes) -> bytes:
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
    length = len(envelope)
    unescaped_for_checksum = bytes([HEADER, length]) + envelope
    cksum = _checksum(unescaped_for_checksum)
    body = _escape(envelope)
    cksum_bytes = _escape(bytes([cksum]))
    return bytes([HEADER, length]) + body + cksum_bytes + bytes([TAIL])


def _build_envelope(cmd: int, sub: int, data: bytes = b"") -> bytes:
    return bytes([0x00, cmd, sub]) + data


class RackLinkRLNKSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "racklink_rlnk",
        "name": "Middle Atlantic RackLink PDU Simulator",
        "category": "power",
        "transport": "tcp",
        "default_port": 60000,
        # Binary protocol — no line delimiter. Setting None explicitly
        # so the framework skips line-mode wrapping.
        "delimiter": None,
        "initial_state": {
            "model": "RLNK-P920R",
            "firmware": "1.6.0",
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # TCPSimulator's binary mode: ensure neither delimiter nor line
        # mode are set. The base class only reads SIMULATOR_INFO when
        # the value is truthy, so a None delimiter falls through fine,
        # but be defensive on a config override.
        self._delimiter = None
        self._line_mode = False

        n_outlets = int(self.config.get("outlets", DEFAULT_OUTLETS))
        n_contacts = int(self.config.get("contacts", DEFAULT_CONTACTS))
        self._n_outlets = max(1, min(MAX_OUTLETS, n_outlets))
        self._n_contacts = max(0, min(MAX_CONTACTS, n_contacts))

        self._outlet_state: list[int] = [OUTLET_OFF] * MAX_OUTLETS
        self._outlet_cycle: list[int] = [0] * MAX_OUTLETS
        self._outlet_name: list[str] = [
            f"Outlet {i + 1}" for i in range(MAX_OUTLETS)
        ]
        self._contact_state: list[int] = [OUTLET_OFF] * MAX_CONTACTS
        self._contact_name: list[str] = [
            f"Contact {i + 1}" for i in range(MAX_CONTACTS)
        ]

        # Sensor defaults — Premium+ values for a typical 20A circuit.
        self._part_number = self.state.get("model", "RLNK-P920R")
        self._amp_rating = 20
        self._surge_present = True
        self._ip_address = "192.168.1.50"
        self._mac_address = "00:1B:21:00:00:01"
        self._kilowatt_hours = 1234.5
        self._voltage_peak = 170
        self._voltage_rms = 120
        self._load_peak = 7.5
        self._load_rms = 5.0
        self._temperature_f = 78
        self._wattage = 600
        self._power_factor = 0.9
        self._thermal_load_btu = 2046.0
        self._surge_state = 0x01  # protected
        self._occupancy = b"O"
        self._epo_active = False

        # Per-client state.
        self._authenticated: dict[str, bool] = {}
        self._subscribed: dict[str, bool] = {}
        self._buffers: dict[str, bytearray] = {}
        # Session lock so concurrent writes (a poll burst) don't
        # interleave their frame bytes when the sim pushes between
        # them.
        self._send_lock = asyncio.Lock()

    # ── Connection ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        self._authenticated[client_id] = False
        self._subscribed[client_id] = False
        self._buffers[client_id] = bytearray()
        return None

    # ── Frame parsing ──

    def handle_command(self, data: bytes) -> bytes | None:
        # Binary protocol — handle_command is called on raw chunks.
        # Buffer per-client and emit a concatenated response covering
        # every complete frame we found in the chunk.
        # The framework doesn't pass client_id through handle_command,
        # so we maintain a single shared buffer keyed by the most
        # recent client. With one driver per sim instance this is
        # effectively safe; for multi-client testing the framework
        # would need extending.
        client_id = self._latest_client_id()
        if client_id is None:
            return None
        buf = self._buffers.setdefault(client_id, bytearray())
        buf.extend(data)
        responses: list[bytes] = []
        for frame in self._extract_frames(buf):
            response = self._handle_frame(client_id, frame)
            if response:
                responses.append(response)
        return b"".join(responses) if responses else None

    def _latest_client_id(self) -> str | None:
        if not self._clients:
            return None
        return next(reversed(self._clients))

    def _extract_frames(self, buf: bytearray) -> list[bytes]:
        frames: list[bytes] = []
        while True:
            try:
                start = buf.index(HEADER)
            except ValueError:
                buf.clear()
                return frames
            if start > 0:
                del buf[:start]
            i = 1
            end = -1
            while i < len(buf):
                b = buf[i]
                if b == ESCAPE:
                    i += 2
                    continue
                if b == TAIL:
                    end = i
                    break
                i += 1
            if end < 0:
                return frames
            frames.append(bytes(buf[: end + 1]))
            del buf[: end + 1]

    def _handle_frame(self, client_id: str, frame: bytes) -> bytes | None:
        if len(frame) < 5 or frame[0] != HEADER or frame[-1] != TAIL:
            return None
        body = _unescape(frame[1:-1])
        if len(body) < 2:
            return None
        length = body[0]
        if len(body) < 1 + length + 1:
            return None
        envelope = body[1 : 1 + length]
        cksum = body[1 + length]
        expected = _checksum(bytes([HEADER, length]) + envelope)
        if cksum != expected:
            return build_frame(_build_envelope(CMD_NACK, SUB_RESPONSE, bytes([0x01])))
        if len(envelope) < 3:
            return build_frame(_build_envelope(CMD_NACK, SUB_RESPONSE, bytes([0x06])))
        cmd = envelope[1]
        sub = envelope[2]
        data = envelope[3:]
        return self._dispatch_command(client_id, cmd, sub, data)

    # ── Command dispatch ──

    def _dispatch_command(
        self, client_id: str, cmd: int, sub: int, data: bytes
    ) -> bytes | None:
        if cmd == CMD_LOGIN and sub == SUB_SET:
            # `reject_auth` lets a test exercise the driver's auth-fault
            # path: reply with a rejected login (0x01 -> 0x00) and leave
            # the client unauthenticated.
            if self.config.get("reject_auth"):
                self._authenticated[client_id] = False
                return build_frame(
                    _build_envelope(CMD_LOGIN, SUB_RESPONSE, bytes([0x00]))
                )
            self._authenticated[client_id] = True
            return build_frame(_build_envelope(CMD_LOGIN, SUB_RESPONSE, bytes([0x01])))

        if not self._authenticated.get(client_id):
            return build_frame(_build_envelope(CMD_NACK, SUB_RESPONSE, bytes([0x08])))

        if cmd == CMD_PING and sub == SUB_RESPONSE:
            # Driver responding to our (hypothetical) ping — no reply.
            return None

        if cmd == CMD_REGISTER_STATUS and sub == SUB_SET:
            self._subscribed[client_id] = True
            # Echo the requested bitmask back as the response payload.
            return build_frame(
                _build_envelope(CMD_REGISTER_STATUS, SUB_RESPONSE, data[:6])
            )
        if cmd == CMD_REGISTER_STATUS and sub == SUB_GET:
            mask = b"\x01\x0f\x4b\x01\x7f\x0f" if self._subscribed.get(client_id) else b"\x00" * 6
            return build_frame(_build_envelope(CMD_REGISTER_STATUS, SUB_RESPONSE, mask))

        if cmd == CMD_OUTLET:
            return self._handle_outlet(sub, data)
        if cmd == CMD_OUTLET_NAME:
            return self._handle_outlet_name(sub, data)
        if cmd == CMD_OUTLET_COUNT and sub == SUB_GET:
            return build_frame(
                _build_envelope(CMD_OUTLET_COUNT, SUB_RESPONSE, self._outlet_count_payload())
            )
        if cmd == CMD_CONTACT:
            return self._handle_contact(sub, data)
        if cmd == CMD_CONTACT_NAME:
            return self._handle_contact_name(sub, data)
        if cmd == CMD_CONTACT_COUNT and sub == SUB_GET:
            return build_frame(
                _build_envelope(CMD_CONTACT_COUNT, SUB_RESPONSE, self._contact_count_payload())
            )
        if cmd == CMD_SEQUENCE and sub == SUB_SET:
            return self._handle_sequence(data)
        if cmd == CMD_EPO and sub == SUB_SET:
            return self._handle_epo(data)
        if cmd == CMD_PART_NUMBER and sub == SUB_GET:
            return build_frame(
                _build_envelope(CMD_PART_NUMBER, SUB_RESPONSE, self._part_number.encode())
            )
        if cmd == CMD_AMP_RATING and sub == SUB_GET:
            return build_frame(
                _build_envelope(
                    CMD_AMP_RATING, SUB_RESPONSE, f"{self._amp_rating:03d}".encode()
                )
            )
        if cmd == CMD_SURGE_PRESENT and sub == SUB_GET:
            return build_frame(
                _build_envelope(
                    CMD_SURGE_PRESENT,
                    SUB_RESPONSE,
                    b"Y" if self._surge_present else b"N",
                )
            )
        if cmd == CMD_IP_ADDRESS and sub == SUB_GET:
            return build_frame(
                _build_envelope(CMD_IP_ADDRESS, SUB_RESPONSE, self._ip_address.encode())
            )
        if cmd == CMD_MAC_ADDRESS and sub == SUB_GET:
            return build_frame(
                _build_envelope(CMD_MAC_ADDRESS, SUB_RESPONSE, self._mac_address.encode())
            )

        # Sensor reads
        sensor_response = self._handle_sensor(cmd, sub)
        if sensor_response is not None:
            return sensor_response

        # Unknown command
        return build_frame(_build_envelope(CMD_NACK, SUB_RESPONSE, bytes([0x04])))

    # ── Outlet handlers ──

    def _outlet_count_payload(self) -> bytes:
        out = bytearray()
        for i in range(MAX_OUTLETS):
            if i < self._n_outlets:
                out.append(ord("C"))
            else:
                out.append(ord("X"))
        return bytes(out)

    def _contact_count_payload(self) -> bytes:
        out = bytearray()
        for i in range(MAX_CONTACTS):
            if i < self._n_contacts:
                out.append(ord("C"))
            else:
                out.append(ord("X"))
        return bytes(out)

    def _outlet_status_envelope(self, outlet: int, sub: int = SUB_RESPONSE) -> bytes:
        idx = outlet - 1
        cycle = f"{self._outlet_cycle[idx]:04d}".encode("ascii")
        return _build_envelope(
            CMD_OUTLET,
            sub,
            bytes([outlet, self._outlet_state[idx]]) + cycle,
        )

    def _handle_outlet(self, sub: int, data: bytes) -> bytes | None:
        if sub == SUB_GET and len(data) >= 1:
            outlet = data[0]
            if 1 <= outlet <= self._n_outlets:
                return build_frame(self._outlet_status_envelope(outlet))
            return build_frame(_build_envelope(CMD_NACK, SUB_RESPONSE, bytes([0x07])))
        if sub == SUB_SET and len(data) >= 6:
            outlet = data[0]
            state = data[1]
            try:
                cycle = int(data[2:6].decode("ascii"))
            except ValueError:
                cycle = 0
            if 1 <= outlet <= self._n_outlets:
                idx = outlet - 1
                if state == OUTLET_CYCLE:
                    # Treat cycle as toggle-off-then-on to keep the sim simple;
                    # the response shows the outlet ON after cycling.
                    self._outlet_cycle[idx] = cycle
                    self._outlet_state[idx] = OUTLET_ON
                else:
                    self._outlet_state[idx] = state
                # Push status change (subscribers only).
                asyncio.create_task(self._broadcast_outlet_change(outlet))
                return build_frame(self._outlet_status_envelope(outlet))
        return None

    def _handle_outlet_name(self, sub: int, data: bytes) -> bytes | None:
        if sub == SUB_GET and len(data) >= 1:
            outlet = data[0]
            if 1 <= outlet <= self._n_outlets:
                name = self._outlet_name[outlet - 1].encode("utf-8")
                return build_frame(
                    _build_envelope(
                        CMD_OUTLET_NAME, SUB_RESPONSE, bytes([outlet]) + name
                    )
                )
        if sub == SUB_SET and len(data) >= 1:
            outlet = data[0]
            name = data[1:].decode("utf-8", errors="replace")
            if 1 <= outlet <= self._n_outlets:
                self._outlet_name[outlet - 1] = name
                return build_frame(
                    _build_envelope(
                        CMD_OUTLET_NAME,
                        SUB_RESPONSE,
                        bytes([outlet]) + name.encode("utf-8"),
                    )
                )
        return None

    def _handle_contact(self, sub: int, data: bytes) -> bytes | None:
        if sub == SUB_GET and len(data) >= 1:
            contact = data[0]
            if 1 <= contact <= self._n_contacts:
                return build_frame(self._contact_status_envelope(contact))
        if sub == SUB_SET and len(data) >= 6:
            contact = data[0]
            state = data[1]
            if 1 <= contact <= self._n_contacts:
                self._contact_state[contact - 1] = state
                asyncio.create_task(self._broadcast_contact_change(contact))
                return build_frame(self._contact_status_envelope(contact))
        return None

    def _contact_status_envelope(self, contact: int, sub: int = SUB_RESPONSE) -> bytes:
        idx = contact - 1
        return _build_envelope(
            CMD_CONTACT,
            sub,
            bytes([contact, self._contact_state[idx]]) + b"0000",
        )

    def _handle_contact_name(self, sub: int, data: bytes) -> bytes | None:
        if sub == SUB_GET and len(data) >= 1:
            contact = data[0]
            if 1 <= contact <= self._n_contacts:
                name = self._contact_name[contact - 1].encode("utf-8")
                return build_frame(
                    _build_envelope(
                        CMD_CONTACT_NAME, SUB_RESPONSE, bytes([contact]) + name
                    )
                )
        if sub == SUB_SET and len(data) >= 1:
            contact = data[0]
            name = data[1:].decode("utf-8", errors="replace")
            if 1 <= contact <= self._n_contacts:
                self._contact_name[contact - 1] = name
                return build_frame(
                    _build_envelope(
                        CMD_CONTACT_NAME,
                        SUB_RESPONSE,
                        bytes([contact]) + name.encode("utf-8"),
                    )
                )
        return None

    def _handle_sequence(self, data: bytes) -> bytes | None:
        if len(data) < 5:
            return None
        direction = data[0]
        try:
            delay = int(data[1:5].decode("ascii"))
        except ValueError:
            delay = 0
        if direction == 0x01:
            for i in range(self._n_outlets):
                self._outlet_state[i] = OUTLET_ON
            new_dir = 0x02  # sequencing up complete
        elif direction == 0x03:
            for i in range(self._n_outlets):
                self._outlet_state[i] = OUTLET_OFF
            new_dir = 0x04  # sequencing down complete
        else:
            new_dir = 0x00
        delay_str = f"{delay:04d}".encode("ascii")
        # Push outlet status changes after sequence
        for n in range(1, self._n_outlets + 1):
            asyncio.create_task(self._broadcast_outlet_change(n))
        return build_frame(
            _build_envelope(CMD_SEQUENCE, SUB_SET, bytes([new_dir]) + delay_str)
        )

    def _handle_epo(self, data: bytes) -> bytes | None:
        if not data:
            return None
        self._epo_active = data[0] == 0x01
        return build_frame(
            _build_envelope(
                CMD_EPO,
                SUB_RESPONSE,
                bytes([0x01 if self._epo_active else 0x00]),
            )
        )

    def _handle_sensor(self, cmd: int, sub: int) -> bytes | None:
        if sub != SUB_GET:
            return None
        if cmd == CMD_KILOWATT_HOURS:
            payload = f"{self._kilowatt_hours:013.1f}".encode("ascii")
        elif cmd == CMD_PEAK_VOLTAGE:
            payload = f"{self._voltage_peak:03d}".encode("ascii")
        elif cmd == CMD_RMS_VOLTAGE:
            payload = f"{self._voltage_rms:03d}".encode("ascii")
        elif cmd == CMD_PEAK_LOAD:
            payload = f"{self._load_peak:04.1f}".encode("ascii")
        elif cmd == CMD_RMS_LOAD:
            payload = f"{self._load_rms:04.1f}".encode("ascii")
        elif cmd == CMD_TEMPERATURE:
            payload = f"{self._temperature_f:03d}".encode("ascii")
        elif cmd == CMD_WATTAGE:
            payload = f"{self._wattage:04d}".encode("ascii")
        elif cmd == CMD_POWER_FACTOR:
            payload = f"{self._power_factor:3.1f}".encode("ascii")
        elif cmd == CMD_THERMAL_LOAD:
            payload = f"{self._thermal_load_btu:06.1f}".encode("ascii")
        elif cmd == CMD_SURGE_STATE:
            payload = bytes([self._surge_state])
        elif cmd == CMD_OCCUPANCY:
            payload = self._occupancy
        else:
            return None
        return build_frame(_build_envelope(cmd, SUB_RESPONSE, payload))

    # ── Push notifications ──

    async def _broadcast_outlet_change(self, outlet: int) -> None:
        if not any(self._subscribed.values()):
            return
        envelope = self._outlet_status_envelope(outlet, sub=SUB_STATUS_CHANGE)
        await self._push_to_subscribers(build_frame(envelope))

    async def _broadcast_contact_change(self, contact: int) -> None:
        if not any(self._subscribed.values()):
            return
        envelope = self._contact_status_envelope(contact, sub=SUB_STATUS_CHANGE)
        await self._push_to_subscribers(build_frame(envelope))

    async def _push_to_subscribers(self, data: bytes) -> None:
        # Tiny yield so the request response writes first; otherwise the
        # SET response and the pushed change can interleave on slow
        # event loops.
        await asyncio.sleep(0)
        async with self._send_lock:
            for client_id, subscribed in list(self._subscribed.items()):
                if subscribed:
                    await self.push_to(client_id, data)

    # ── Test hooks (not part of the protocol) ──

    def trigger_outlet_push(self, outlet: int, on: bool) -> None:
        """Force an outlet state change + push for testing.

        Used by the round-trip test to verify the driver actually
        reacts to unsolicited 0x12 frames.
        """
        if 1 <= outlet <= self._n_outlets:
            self._outlet_state[outlet - 1] = OUTLET_ON if on else OUTLET_OFF
            asyncio.create_task(self._broadcast_outlet_change(outlet))

    def trigger_sensor_push(self, cmd: int, value: Any) -> None:
        """Force a sensor change + push for testing."""
        if cmd == CMD_RMS_VOLTAGE:
            self._voltage_rms = int(value)
            payload = f"{self._voltage_rms:03d}".encode("ascii")
        elif cmd == CMD_TEMPERATURE:
            self._temperature_f = int(value)
            payload = f"{self._temperature_f:03d}".encode("ascii")
        elif cmd == CMD_WATTAGE:
            self._wattage = int(value)
            payload = f"{self._wattage:04d}".encode("ascii")
        else:
            return
        envelope = _build_envelope(cmd, SUB_STATUS_CHANGE, payload)
        asyncio.create_task(self._push_to_subscribers(build_frame(envelope)))
