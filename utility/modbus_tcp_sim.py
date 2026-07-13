"""Modbus TCP device — Simulator.

A minimal Modbus TCP server (MBAP framing on the sim's assigned port) that
answers the read/write function codes the ``modbus_tcp`` driver uses:

  0x01 Read Coils            0x05 Write Single Coil       0x0F Write Multiple Coils
  0x02 Read Discrete Inputs  0x06 Write Single Register   0x10 Write Multiple Registers
  0x03 Read Holding Registers
  0x04 Read Input Registers

It keeps four register banks (coils, discrete inputs, input registers, holding
registers) that default to 0/False and persist writes, so a driver's declared
register map round-trips: write a holding register, read it back, see the value.
A few holding/input registers are seeded so an initial poll shows non-zero data.

Since a generic Modbus slave has no fixed named controls (the meaning of each
address lives in the driver's register map, not here), the Simulator UI stays
minimal; correctness is proven by the driver's decoded device card.

Driver side: ``utility/modbus_tcp.py``.
"""

from __future__ import annotations

import logging
import struct

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

# Function codes.
FC_READ_COILS = 0x01
FC_READ_DISCRETE = 0x02
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_COIL = 0x05
FC_WRITE_REGISTER = 0x06
FC_WRITE_COILS = 0x0F
FC_WRITE_REGISTERS = 0x10

EX_ILLEGAL_FUNCTION = 0x01
EX_ILLEGAL_ADDRESS = 0x02
EX_ILLEGAL_VALUE = 0x03


class ModbusTCPSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "modbus_tcp",
        "name": "Modbus TCP Device Simulator",
        "category": "utility",
        "transport": "tcp",
        "default_port": 502,
        # Binary protocol — no line delimiter.
        "delimiter": None,
        "initial_state": {},
        "controls": [
            {"type": "indicator", "key": "last_write", "label": "Last Write"},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._delimiter = None
        self._line_mode = False
        self._buf = bytearray()

        # Register banks: address -> value. Registers are 16-bit ints, coils and
        # discrete inputs are bools. Missing addresses read as 0/False.
        self._coils: dict[int, bool] = {}
        self._discrete: dict[int, bool] = {}
        self._holding: dict[int, int] = {}
        self._input: dict[int, int] = {}

        # Seed some believable values so an initial poll isn't all zeros.
        for addr in range(0, 16):
            self._holding[addr] = 100 + addr * 5
            self._input[addr] = 200 + addr
        for addr in range(0, 16):
            self._discrete[addr] = addr % 2 == 0

    # ── Frame handling ──

    def handle_command(self, data: bytes) -> bytes | None:
        self._buf.extend(data)
        out = bytearray()
        while len(self._buf) >= 7:
            length = struct.unpack(">H", self._buf[4:6])[0]
            total = 6 + length
            if len(self._buf) < total:
                break
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            resp = self._handle_frame(frame)
            if resp:
                out.extend(resp)
        return bytes(out) if out else None

    def _handle_frame(self, frame: bytes) -> bytes | None:
        if len(frame) < 8:
            return None
        txid = struct.unpack(">H", frame[0:2])[0]
        unit = frame[6]
        pdu = frame[7:]
        resp_pdu = self._handle_pdu(pdu)
        if resp_pdu is None:
            return None
        return struct.pack(">HHHB", txid, 0, len(resp_pdu) + 1, unit) + resp_pdu

    def _exception(self, func: int, code: int) -> bytes:
        return struct.pack(">BB", func | 0x80, code)

    def _handle_pdu(self, pdu: bytes) -> bytes | None:
        if not pdu:
            return None
        func = pdu[0]
        try:
            if func in (FC_READ_COILS, FC_READ_DISCRETE):
                return self._read_bits(func, pdu)
            if func in (FC_READ_HOLDING, FC_READ_INPUT):
                return self._read_registers(func, pdu)
            if func == FC_WRITE_COIL:
                return self._write_coil(pdu)
            if func == FC_WRITE_REGISTER:
                return self._write_register(pdu)
            if func == FC_WRITE_COILS:
                return self._write_coils(pdu)
            if func == FC_WRITE_REGISTERS:
                return self._write_registers(pdu)
        except struct.error:
            return self._exception(func, EX_ILLEGAL_VALUE)
        return self._exception(func, EX_ILLEGAL_FUNCTION)

    # ── Reads ──

    def _read_bits(self, func: int, pdu: bytes) -> bytes:
        _, start, qty = struct.unpack(">BHH", pdu[:5])
        if qty < 1 or qty > 2000:
            return self._exception(func, EX_ILLEGAL_VALUE)
        bank = self._coils if func == FC_READ_COILS else self._discrete
        byte_count = (qty + 7) // 8
        data = bytearray(byte_count)
        for i in range(qty):
            if bank.get(start + i, False):
                data[i // 8] |= 1 << (i % 8)  # LSB = lowest-addressed bit
        return struct.pack(">BB", func, byte_count) + bytes(data)

    def _read_registers(self, func: int, pdu: bytes) -> bytes:
        _, start, qty = struct.unpack(">BHH", pdu[:5])
        if qty < 1 or qty > 125:
            return self._exception(func, EX_ILLEGAL_VALUE)
        bank = self._holding if func == FC_READ_HOLDING else self._input
        data = b"".join(struct.pack(">H", bank.get(start + i, 0) & 0xFFFF) for i in range(qty))
        return struct.pack(">BB", func, qty * 2) + data

    # ── Writes ──

    def _write_coil(self, pdu: bytes) -> bytes:
        _, addr, value = struct.unpack(">BHH", pdu[:5])
        self._coils[addr] = value == 0xFF00
        self.set_state("last_write", f"coil {addr}={self._coils[addr]}")
        return pdu[:5]  # echo request

    def _write_register(self, pdu: bytes) -> bytes:
        _, addr, value = struct.unpack(">BHH", pdu[:5])
        self._holding[addr] = value & 0xFFFF
        self.set_state("last_write", f"holding {addr}={value}")
        return pdu[:5]  # echo request

    def _write_coils(self, pdu: bytes) -> bytes:
        _, start, qty, byte_count = struct.unpack(">BHHB", pdu[:6])
        data = pdu[6:6 + byte_count]
        for i in range(qty):
            self._coils[start + i] = bool(data[i // 8] & (1 << (i % 8)))
        self.set_state("last_write", f"coils {start}x{qty}")
        return struct.pack(">BHH", FC_WRITE_COILS, start, qty)

    def _write_registers(self, pdu: bytes) -> bytes:
        _, start, qty, byte_count = struct.unpack(">BHHB", pdu[:6])
        data = pdu[6:6 + byte_count]
        for i in range(qty):
            self._holding[start + i] = struct.unpack(">H", data[i * 2:i * 2 + 2])[0]
        self.set_state("last_write", f"holding {start}x{qty}")
        return struct.pack(">BHH", FC_WRITE_REGISTERS, start, qty)
