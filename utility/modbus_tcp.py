"""Modbus TCP generic driver.

Modbus is a binary request/response protocol carried over TCP port 502 with a
7-byte MBAP header (Transaction ID, Protocol ID, Length, Unit ID) in front of
each PDU. This is a GENERIC: the device's control surface is whatever register
map the integrator declares, so the driver has no fixed state variables or
commands — it reads a user-authored ``register_map`` (a ``type: table`` config
field) and builds its state variables (read registers), commands (write
registers), and device settings (read/write registers) per device at init.

Python, not YAML, for two reasons ConfigurableDriver can't cover: binary MBAP
framing, and a per-instance schema derived from config rather than declared in
the driver file.

Register map columns (one row per register):
  name      the id it gets as a state value / command / setting
  area      coil (rw bit) | discrete (r bit) | input (r 16-bit) | holding (rw 16-bit)
  address   0-based PDU address. A manual's "40001" is holding address 0.
  datatype  registers only: uint16 int16 uint32 int32 float32 (+ *_swapped for
            low-word-first devices). Coils/discretes are always bool.
  scale     engineering value = raw * scale + offset
  offset    (as above)
  access    r (status) | w (command) | rw (device setting, offline pending queue)
  unit      display unit for value hints (optional)
  slave_id  per-row unit-id override for a gateway fronting multiple RTU slaves

Source:
  MODBUS Application Protocol Specification V1.1b3
    https://www.modbus.org/file/secure/modbusprotocolspecification.pdf
  MODBUS Messaging on TCP/IP Implementation Guide V1.0b
    https://www.modbus.org/file/secure/messagingimplementationguide.pdf
"""

from __future__ import annotations

import asyncio
import copy
import re
import struct
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_PORT = 502
TRANSACTION_TIMEOUT_S = 3.0

# Modbus data model areas → the function codes that read/write them.
# is_bit distinguishes 1-bit coils/discretes (packed bits) from 16-bit registers.
AREAS: dict[str, dict[str, Any]] = {
    "coil": {"read_fc": 0x01, "write_single": 0x05, "write_multi": 0x0F, "is_bit": True, "writable": True},
    "discrete": {"read_fc": 0x02, "is_bit": True, "writable": False},
    "input": {"read_fc": 0x04, "is_bit": False, "writable": False},
    "holding": {"read_fc": 0x03, "write_single": 0x06, "write_multi": 0x10, "is_bit": False, "writable": True},
}

# Register data types → word count + how to decode. Coils/discretes are bool and
# don't use this table.
DATATYPES: dict[str, dict[str, Any]] = {
    "uint16": {"words": 1, "signed": False},
    "int16": {"words": 1, "signed": True},
    "uint32": {"words": 2, "signed": False, "swap": False},
    "int32": {"words": 2, "signed": True, "swap": False},
    "uint32_swapped": {"words": 2, "signed": False, "swap": True},
    "float32": {"words": 2, "float": True, "swap": False},
    "float32_swapped": {"words": 2, "float": True, "swap": True},
}

# Modbus exception codes (spec §7). Surfaced as protocol errors — the device is
# still reachable, so we log the reason and leave the value stale, not offline.
EXCEPTION_CODES = {
    0x01: "Illegal function",
    0x02: "Illegal data address",
    0x03: "Illegal data value",
    0x04: "Server device failure",
    0x05: "Acknowledge",
    0x06: "Server device busy",
    0x08: "Memory parity error",
    0x0A: "Gateway path unavailable",
    0x0B: "Gateway target device failed to respond",
}


class ModbusException(Exception):
    """A Modbus exception response (function code with the high bit set)."""

    def __init__(self, code: int):
        self.code = code
        super().__init__(EXCEPTION_CODES.get(code, f"exception 0x{code:02X}"))


# Column schema for the register_map table field. Declared once and reused in
# config_schema so the device-page table editor renders the right widgets.
REGISTER_MAP_COLUMNS = {
    "name": {"type": "string", "label": "Name", "required": True,
             "help": "The status value / command / setting id this register becomes."},
    "area": {"type": "enum", "label": "Register Type", "default": "holding",
             "values": [
                 {"value": "coil", "label": "Coil (read/write bit)"},
                 {"value": "discrete", "label": "Discrete Input (read-only bit)"},
                 {"value": "input", "label": "Input Register (read-only 16-bit)"},
                 {"value": "holding", "label": "Holding Register (read/write 16-bit)"}]},
    "address": {"type": "integer", "label": "Address", "required": True, "min": 0, "max": 65535,
                "help": "0-based PDU address. Holding register 40001 in a manual = address 0."},
    "datatype": {"type": "enum", "label": "Data Type", "default": "uint16",
                 "values": ["uint16", "int16", "uint32", "int32", "float32",
                            "uint32_swapped", "float32_swapped"],
                 "help": "Registers only. Coils/discretes are always bool. *_swapped = low word first."},
    "scale": {"type": "number", "label": "Scale", "default": 1,
              "help": "Engineering value = raw x scale + offset."},
    "offset": {"type": "number", "label": "Offset", "default": 0},
    "access": {"type": "enum", "label": "Access", "default": "r",
               "values": [
                   {"value": "r", "label": "Read (status value)"},
                   {"value": "w", "label": "Write (command)"},
                   {"value": "rw", "label": "Read/Write (device setting)"}]},
    "unit": {"type": "string", "label": "Unit", "help": "Display unit, e.g. C, %, dB (optional)."},
    "slave_id": {"type": "integer", "label": "Slave ID", "min": 0, "max": 255,
                 "help": "Optional per-row unit-id override for a gateway fronting multiple RTU slaves."},
}


def _sanitize_name(name: str) -> str:
    """Turn a user-typed register name into a safe state/command key."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(name).strip()).strip("_")
    return cleaned or "reg"


def _decode_registers(datatype: str, words: list[int]) -> float | int:
    """Decode a list of 16-bit register words into a Python number."""
    spec = DATATYPES[datatype]
    if spec["words"] == 1:
        raw = words[0] & 0xFFFF
        if spec.get("signed") and raw >= 0x8000:
            raw -= 0x10000
        return raw
    # 32-bit: two words, high word first unless the device is word-swapped.
    hi, lo = (words[1], words[0]) if spec.get("swap") else (words[0], words[1])
    packed = struct.pack(">HH", hi & 0xFFFF, lo & 0xFFFF)
    if spec.get("float"):
        return struct.unpack(">f", packed)[0]
    return struct.unpack(">i" if spec.get("signed") else ">I", packed)[0]


def _encode_value(datatype: str, raw: int | float) -> list[int]:
    """Encode a raw (already de-scaled) number into 16-bit register words."""
    spec = DATATYPES[datatype]
    if spec["words"] == 1:
        val = int(round(raw))
        if spec.get("signed"):
            if not -0x8000 <= val <= 0x7FFF:
                raise ValueError(f"{val} out of range for {datatype}")
            val &= 0xFFFF
        else:
            if not 0 <= val <= 0xFFFF:
                raise ValueError(f"{val} out of range for {datatype}")
        return [val]
    if spec.get("float"):
        packed = struct.pack(">f", float(raw))
    else:
        val = int(round(raw))
        fmt = ">i" if spec.get("signed") else ">I"
        lo_b, hi_b = (0, 0xFFFFFFFF) if not spec.get("signed") else (-0x80000000, 0x7FFFFFFF)
        if not lo_b <= val <= hi_b:
            raise ValueError(f"{val} out of range for {datatype}")
        packed = struct.pack(fmt, val)
    hi, lo = struct.unpack(">HH", packed)
    return [lo, hi] if spec.get("swap") else [hi, lo]


def _state_type(reg: dict[str, Any]) -> str:
    """State-variable type for a register: bool for bits, int for unscaled
    integers, number for floats / scaled values."""
    if reg["is_bit"]:
        return "boolean"
    dt = DATATYPES[reg["datatype"]]
    if dt.get("float") or reg["scale"] != 1 or reg["offset"] != 0:
        return "number"
    return "integer"


def parse_register_map(rows: Any, default_unit_id: int) -> list[dict[str, Any]]:
    """Normalize the user's register_map rows into validated register specs.

    Tolerant of hand/AI-authored config: an unknown area or datatype, or a
    write access on a read-only area, is corrected with a warning rather than
    crashing driver init. Duplicate names keep the last (a warning notes it).
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(rows, list):
        return out
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        name = _sanitize_name(raw.get("name", ""))
        if not raw.get("name"):
            log.warning("modbus register with no name skipped")
            continue
        area = str(raw.get("area", "holding")).lower()
        if area not in AREAS:
            log.warning("modbus register %r: unknown area %r, skipping", name, area)
            continue
        area_def = AREAS[area]
        try:
            address = int(raw.get("address"))
        except (TypeError, ValueError):
            log.warning("modbus register %r: invalid address, skipping", name)
            continue
        is_bit = area_def["is_bit"]
        datatype = "uint16" if is_bit else str(raw.get("datatype", "uint16")).lower()
        if not is_bit and datatype not in DATATYPES:
            log.warning("modbus register %r: unknown datatype %r, using uint16", name, datatype)
            datatype = "uint16"
        access = str(raw.get("access", "r")).lower()
        if access not in ("r", "w", "rw"):
            access = "r"
        # A read-only area can't be written; downgrade w/rw to r.
        if not area_def["writable"] and access in ("w", "rw"):
            log.warning("modbus register %r: %s is read-only, forcing access=r", name, area)
            access = "r"
        try:
            scale = float(raw.get("scale", 1) or 1)
        except (TypeError, ValueError):
            scale = 1.0
        try:
            offset = float(raw.get("offset", 0) or 0)
        except (TypeError, ValueError):
            offset = 0.0
        slave = raw.get("slave_id")
        try:
            unit_id = int(slave) if slave not in (None, "") else default_unit_id
        except (TypeError, ValueError):
            unit_id = default_unit_id
        if name in seen:
            log.warning("modbus duplicate register name %r: keeping the last", name)
            out = [r for r in out if r["name"] != name]
        seen.add(name)
        out.append({
            "name": name,
            "area": area,
            "address": address,
            "datatype": datatype,
            "is_bit": is_bit,
            "words": 1 if is_bit else DATATYPES[datatype]["words"],
            "scale": scale,
            "offset": offset,
            "access": access,
            "unit": str(raw.get("unit", "") or ""),
            "unit_id": unit_id,
            "read_fc": area_def["read_fc"],
            "write_single": area_def.get("write_single"),
            "write_multi": area_def.get("write_multi"),
        })
    return out


class ModbusTCPDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "modbus_tcp",
        "name": "Modbus TCP Device",
        "manufacturer": "Generic",
        "category": "utility",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": "Read and write any Modbus TCP device by declaring its register map.",
        "source_url": "https://www.modbus.org/modbus-specifications",
        "simulated": True,
        # The register_map table editor is a 0.23.0 platform feature; on older
        # builds the map can't be authored.
        "min_platform_version": "0.23.0",
        "transport": "tcp",
        "protocols": ["modbus_tcp"],
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "unit_id": 1,
            "poll_interval": 10,
            "register_map": [],
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address",
                     "description": "Modbus TCP device IP address or hostname."},
            "port": {"type": "integer", "default": DEFAULT_PORT, "label": "Port",
                     "min": 1, "max": 65535},
            "unit_id": {"type": "integer", "default": 1, "label": "Unit ID", "min": 0, "max": 255,
                        "description": "Modbus slave/unit id. 1 is typical; 255 for some native-TCP "
                                       "devices; a serial slave behind a gateway uses its bus id "
                                       "(override per register with Slave ID)."},
            "poll_interval": {"type": "integer", "default": 10, "label": "Poll Interval (sec)",
                              "min": 0, "description": "How often to read the mapped registers. 0 disables polling."},
            "register_map": {
                "type": "table",
                "label": "Register Map",
                "row_label": "register",
                "help": "Declare each register to read or write. Read rows become status values, "
                        "write rows become commands, read/write rows become device settings.",
                "columns": REGISTER_MAP_COLUMNS,
            },
        },
        "state_variables": {},
        "commands": {},
        "device_settings": {},
        "help": {
            "overview": "Controls any Modbus TCP device (PLCs, HVAC controllers, power meters, "
                        "industrial gear). You declare the device's register map on the device page; "
                        "the driver turns read registers into live status values, write registers into "
                        "commands, and read/write registers into device settings.",
            "setup": "Enter the IP address and Unit ID (usually 1), then add the registers from the "
                     "device's Modbus register map. Addresses are 0-based: a register listed as 40001 "
                     "in the manual is holding address 0.",
        },
    }

    # Liveness watchdog tuning (see _liveness_probe).
    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._buffer = bytearray()
        self._pending: dict[int, asyncio.Future[bytes]] = {}
        self._txid = 0
        default_unit = self._safe_int(config.get("unit_id"), 1)
        self._registers = parse_register_map(config.get("register_map"), default_unit)
        # Map command / setting names back to their register spec.
        self._by_name: dict[str, dict[str, Any]] = {r["name"]: r for r in self._registers}
        self._write_commands: dict[str, dict[str, Any]] = {}
        # Build a per-instance DRIVER_INFO from the register map BEFORE
        # super().__init__ runs _init_state_variables off self.DRIVER_INFO.
        self.DRIVER_INFO = self._build_driver_info()
        super().__init__(device_id, config, state, events)

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _build_driver_info(self) -> dict[str, Any]:
        """Deep-copy the class DRIVER_INFO and inject the register-derived
        state variables, commands, and device settings for this device."""
        info = copy.deepcopy(type(self).DRIVER_INFO)
        state_vars: dict[str, Any] = {}
        commands: dict[str, Any] = {}
        settings: dict[str, Any] = {}
        for reg in self._registers:
            name = reg["name"]
            vtype = _state_type(reg)
            label = name.replace("_", " ").title()
            if reg["access"] in ("r", "rw"):
                var: dict[str, Any] = {"type": vtype, "label": label, "control": True}
                if reg["unit"]:
                    var["unit"] = reg["unit"]
                state_vars[name] = var
            if reg["access"] == "w":
                cmd_name = f"set_{name}"
                commands[cmd_name] = self._write_command_def(reg, label)
                self._write_commands[cmd_name] = reg
            if reg["access"] == "rw":
                setting: dict[str, Any] = {
                    "type": vtype if vtype != "integer" else "integer",
                    "label": label,
                    "state_key": name,
                    "setup": False,
                    "default": 0 if vtype != "boolean" else False,
                }
                if reg["unit"]:
                    setting["help"] = f"Value in {reg['unit']}."
                settings[name] = setting
        info["state_variables"] = state_vars
        info["commands"] = commands
        info["device_settings"] = settings
        return info

    @staticmethod
    def _write_command_def(reg: dict[str, Any], label: str) -> dict[str, Any]:
        if reg["is_bit"]:
            param = {"type": "boolean", "required": True, "label": "On"}
        elif _state_type(reg) == "integer":
            param = {"type": "integer", "required": True, "label": "Value"}
        else:
            param = {"type": "number", "required": True, "label": "Value"}
        if reg.get("unit"):
            param["label"] = f"Value ({reg['unit']})"
        return {"label": f"Set {label}", "params": {"value": param}}

    # ── Connection lifecycle ──

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = self._safe_int(self.config.get("port"), DEFAULT_PORT)
        try:
            self.transport = await TCPTransport.create(
                host=host,
                port=port,
                on_data=self.on_data_received,
                on_disconnect=self._handle_transport_disconnect,
                delimiter=None,
                timeout=5.0,
                name=self.device_id,
            )
        except (ConnectionError, OSError) as e:
            raise ConnectionError(f"Modbus connect to {host}:{port} failed: {e}") from e

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info("[%s] Connected to Modbus TCP %s:%s (%d registers)",
                 self.device_id, host, port, len(self._registers))

        # Prime state with one read so the card isn't blank until the first poll.
        try:
            await self.poll()
        except (ConnectionError, OSError, asyncio.TimeoutError):
            log.warning("[%s] Initial Modbus poll failed", self.device_id)

        # Custom connect() skips BaseDriver.connect()'s auto-start, so start the
        # liveness watchdog and polling explicitly.
        self._start_health_loop()
        poll_interval = self._safe_int(self.config.get("poll_interval"), 10)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self._buffer.clear()
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info("[%s] Disconnected", self.device_id)

    async def _handle_transport_disconnect(self) -> None:
        self._connected = False
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self.set_state("connected", False)

    # ── MBAP framing + request/response correlation ──

    async def on_data_received(self, data: bytes) -> None:
        """Accumulate bytes and dispatch each complete MBAP frame to its
        awaiting transaction. Frame length = 6 + the MBAP Length field."""
        self._buffer.extend(data)
        while len(self._buffer) >= 7:
            length = struct.unpack(">H", self._buffer[4:6])[0]
            total = 6 + length
            if len(self._buffer) < total:
                break
            frame = bytes(self._buffer[:total])
            del self._buffer[:total]
            txid = struct.unpack(">H", frame[0:2])[0]
            pdu = frame[7:total]
            fut = self._pending.pop(txid, None)
            if fut is not None and not fut.done():
                fut.set_result(pdu)

    def _next_txid(self) -> int:
        self._txid = (self._txid + 1) & 0xFFFF
        return self._txid

    async def _transact(self, unit_id: int, pdu: bytes) -> bytes:
        """Send one Modbus request and await its response PDU.

        Raises ModbusException on an exception response, ConnectionError if not
        connected, and TimeoutError if the device doesn't answer.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        txid = self._next_txid()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()
        self._pending[txid] = fut
        # MBAP: transaction id, protocol id (0), length (unit + pdu), unit id.
        adu = struct.pack(">HHHB", txid, 0, len(pdu) + 1, unit_id & 0xFF) + pdu
        try:
            await self.transport.send(adu)
            resp = await asyncio.wait_for(fut, TRANSACTION_TIMEOUT_S)
        finally:
            self._pending.pop(txid, None)
        if not resp:
            raise ConnectionError("empty Modbus response")
        func = resp[0]
        if func & 0x80:  # exception response
            code = resp[1] if len(resp) > 1 else 0
            raise ModbusException(code)
        return resp

    # ── Reads ──

    async def _read_register(self, reg: dict[str, Any]) -> bool | int | float:
        """Read one mapped register and return its engineering value."""
        pdu = struct.pack(">BHH", reg["read_fc"], reg["address"] & 0xFFFF, reg["words"])
        resp = await self._transact(reg["unit_id"], pdu)
        # resp: [func, byte_count, data...]
        if len(resp) < 2:
            raise ValueError("short Modbus read response")
        byte_count = resp[1]
        data = resp[2:2 + byte_count]
        if reg["is_bit"]:
            return bool(data[0] & 0x01) if data else False
        words = list(struct.unpack(f">{reg['words']}H", data[: reg["words"] * 2]))
        raw = _decode_registers(reg["datatype"], words)
        value = raw * reg["scale"] + reg["offset"]
        # Keep an unscaled integer register integral.
        if _state_type(reg) == "integer":
            return int(value)
        return value

    async def poll(self) -> None:
        """Read every mapped read/read-write register and update state.

        Reads one register per request (correct for any device; a block-read
        optimization for contiguous addresses is a possible future enhancement).
        Transport errors propagate so the platform watchdog counts a dry poll;
        a per-register Modbus exception is logged and skipped (device is up).
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        for reg in self._registers:
            if reg["access"] not in ("r", "rw"):
                continue
            try:
                value = await self._read_register(reg)
                self.set_state(reg["name"], value)
            except ModbusException as e:
                log.warning("[%s] register %r read exception: %s",
                            self.device_id, reg["name"], e)
            except asyncio.TimeoutError as e:
                raise ConnectionError(
                    f"Modbus device {self.config.get('host')} did not answer") from e

    # ── Writes ──

    async def _write_register(self, reg: dict[str, Any], value: Any) -> bool | int | float:
        """Write a value to a register and return the confirmed engineering
        value (from the echo for single writes, else the value sent)."""
        if reg["is_bit"]:
            on = bool(value) if not isinstance(value, str) else value.strip().lower() in ("1", "true", "on", "yes")
            pdu = struct.pack(">BHH", reg["write_single"], reg["address"] & 0xFFFF,
                              0xFF00 if on else 0x0000)
            resp = await self._transact(reg["unit_id"], pdu)
            # FC05 echoes address + value.
            echoed = struct.unpack(">H", resp[3:5])[0] if len(resp) >= 5 else (0xFF00 if on else 0)
            return echoed == 0xFF00
        raw = (float(value) - reg["offset"]) / reg["scale"]
        words = _encode_value(reg["datatype"], raw)
        if len(words) == 1:
            pdu = struct.pack(">BHH", reg["write_single"], reg["address"] & 0xFFFF, words[0])
            resp = await self._transact(reg["unit_id"], pdu)
            echoed = struct.unpack(">H", resp[3:5])[0] if len(resp) >= 5 else words[0]
            confirmed = _decode_registers(reg["datatype"], [echoed])
        else:
            body = struct.pack(f">{len(words)}H", *words)
            pdu = (struct.pack(">BHHB", reg["write_multi"], reg["address"] & 0xFFFF,
                               len(words), len(body)) + body)
            await self._transact(reg["unit_id"], pdu)  # response echoes addr + qty only
            confirmed = _decode_registers(reg["datatype"], words)
        value_eng = confirmed * reg["scale"] + reg["offset"]
        return int(value_eng) if _state_type(reg) == "integer" else value_eng

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        reg = self._write_commands.get(command)
        if reg is None:
            raise ValueError(f"Unknown command: {command}")
        if "value" not in params:
            raise ValueError(f"{command} requires a 'value' parameter")
        confirmed = await self._write_register(reg, params["value"])
        # The write was accepted (no exception); reflect the confirmed value if
        # this register is also read back elsewhere. Write-only commands have no
        # state var, so nothing to set.
        if reg["name"] in self.DRIVER_INFO["state_variables"]:
            self.set_state(reg["name"], confirmed)
        return confirmed

    async def set_device_setting(self, key: str, value: Any) -> Any:
        reg = self._by_name.get(key)
        if reg is None or reg["access"] != "rw":
            raise ValueError(f"Unknown device setting: {key}")
        confirmed = await self._write_register(reg, value)
        self.set_state(key, confirmed)
        return confirmed

    # ── Liveness ──

    async def _liveness_probe(self) -> None:
        """Read the first mapped read register as a keep-alive. Modbus over TCP
        can drop silently (no FIN); an awaited read detects it. If the map has
        no read register, there's nothing cheap to probe — skip."""
        for reg in self._registers:
            if reg["access"] in ("r", "rw"):
                await self._read_register(reg)
                return


DRIVER_CLASS = ModbusTCPDriver
