"""Driver tests for modbus_tcp (generic Modbus TCP register-map driver).

Smoke harness, not exhaustive protocol coverage: the driver is wired to a
scripted in-memory Modbus endpoint that parses each MBAP request and
answers from a small canned register table (echoing writes like a real
server), so every assertion covers the driver's own framing and encoding.

Covers:
  - the register map building a per-instance DRIVER_INFO (status value
    from a read register, command from a write register);
  - the connect lifecycle end to end: connected declared, the priming
    poll read the mapped register through the wire, polling started from
    config, and the liveness watchdog running (the driver has a probe);
  - MBAP decode across split TCP delivery, and a write round trip with
    the exact request bytes asserted;
  - teardown (graceful disconnect and transport drop) cancelling pending
    transactions and dropping any half-received frame;
  - the connect-failure message wording.

Loads the driver with the ``server.*`` imports stubbed so the community
CI stays self-contained (conftest.py rolls the stubs back after this
module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "utility" / "modbus_tcp.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value


class _FakeEvents:
    async def emit(self, name, *args, **kwargs):
        pass


class _FakeBaseDriver:
    """Functional stand-in for the platform BaseDriver surface this driver
    uses, including the liveness watchdog loop (mirrors base.py)."""

    DRIVER_INFO: dict = {}

    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the device stopped answering keep-alive probes."
    )

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._last_transport_error = ""
        self._last_fault = None
        self.disconnect_calls = 0
        self.stashed_fault: tuple[str, str] | None = None
        self.polling_intervals: list = []
        self.lifecycle: list[str] = []
        self._health_task = None
        self._health_failures = 0
        self._bg_tasks: set = set()

    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def get_state(self, key, default=None):
        return self.state.data.get(f"device.{self.device_id}.{key}", default)

    def _handle_transport_disconnect(self) -> None:
        # Mirrors the platform: flip the flags synchronously, then schedule
        # the async teardown (stop loops, close transport, _close_session,
        # disconnect event).
        self._connected = False
        self.set_state("connected", False)
        self.disconnect_calls += 1
        if self.transport is not None:
            self.transport.connected = False
        task = asyncio.ensure_future(self._on_disconnect_cleanup())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _on_disconnect_cleanup(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        transport = self.transport
        self.transport = None
        if transport is not None:
            await transport.close()
        await self._close_session()
        await self.events.emit(f"device.disconnected.{self.device_id}")

    def _stash_fault(self, code, message="") -> None:
        self.stashed_fault = (code, message)

    def _stash_transport_error(self) -> None:
        pass

    async def _liveness_probe(self) -> None:
        raise NotImplementedError

    def _health_enabled(self) -> bool:
        return type(self)._liveness_probe is not _FakeBaseDriver._liveness_probe

    def _start_health_loop(self) -> None:
        if self._health_task is None or self._health_task.done():
            self._health_failures = 0
            self._health_task = asyncio.ensure_future(self._health_loop())
        self.lifecycle.append("watchdog")

    def _stop_health_loop(self) -> None:
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
        self._health_task = None

    async def _health_loop(self) -> None:
        interval = float(self.HEALTH_INTERVAL_S)
        timeout = float(self.HEALTH_TIMEOUT_S)
        max_failures = max(int(self.HEALTH_MAX_FAILURES), 1)
        try:
            while self.transport is not None and getattr(
                    self.transport, "connected", False):
                await asyncio.sleep(interval)
                if not (self.transport is not None and getattr(
                        self.transport, "connected", False)):
                    return
                try:
                    await asyncio.wait_for(self._liveness_probe(), timeout)
                    self._health_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._health_failures += 1
                    if self._health_failures >= max_failures:
                        self._force_disconnect(
                            "no_response", self.HEALTH_FAULT_MESSAGE)
                        return
        except asyncio.CancelledError:
            return

    def _force_disconnect(self, code="no_response", message="") -> None:
        self._health_task = None
        self._stash_fault(code, message)
        self._handle_transport_disconnect()

    async def start_polling(self, interval) -> None:
        self.polling_intervals.append(interval)
        self.lifecycle.append("polling")

    async def stop_polling(self) -> None:
        pass

    # -- connection lifecycle (mirrors the platform's hook-driven connect) --

    async def _pre_connect(self) -> None:
        pass

    def _transport_kwargs(self, transport_type, kwargs):
        return kwargs

    def _create_frame_parser(self):
        return None

    async def _post_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    async def _create_transport(self, transport_type) -> None:
        kwargs = dict(
            host=self.config.get("host", ""),
            port=self.config.get("port", 502),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\r",
            frame_parser=self._create_frame_parser(),
            inter_command_delay=self.config.get("inter_command_delay", 0.0),
            timeout=self.config.get("timeout", 5.0),
            name=self.device_id,
        )
        self.transport = await _FakeTCPTransport.create(
            **self._transport_kwargs(transport_type, kwargs))

    async def connect(self) -> None:
        # 1. Clean slate: reset fault classification, drop a previous
        #    attempt's driver session and stale transport.
        self._last_transport_error = ""
        self._last_fault = None
        self.stashed_fault = None
        await self._close_session()
        if self.transport:
            await self.transport.close()
            self.transport = None
        # 2-3. Establish: pre-connect hook, then the transport.
        await self._pre_connect()
        await self._create_transport("tcp")
        # 4. Handshake: a raise here aborts the connection.
        try:
            await self._post_connect()
        except Exception:
            self._stash_transport_error()
            if self.transport:
                await self.transport.close()
                self.transport = None
            await self._close_session()
            self._connected = False
            raise
        # 5. Declare connected.
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        self.lifecycle.append("declared")
        # 6. Initial sync: a raise here tears the connection back down.
        try:
            await self._initial_sync()
        except Exception:
            self._stash_transport_error()
            transport = self.transport
            self.transport = None
            if transport is not None:
                await transport.close()
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise
        self.lifecycle.append("synced")
        # 7. Polling + liveness watchdog.
        if self.config.get("poll_interval", 0):
            await self.start_polling(self.config["poll_interval"])
        if self._health_enabled():
            self._start_health_loop()

    async def disconnect(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


# Canned register values the scripted endpoint answers reads from:
# (read function code, PDU address) -> one 16-bit register word.
# Input register 10 holds int16 raw -55 (0xFFC9) — scaled x0.1 -> -5.5.
_REGISTERS = {
    (0x04, 10): b"\xff\xc9",
}

# When True, the fake transport's create() raises like an unreachable host.
_FAIL_CONNECT = False


class _FakeTCPTransport:
    """Scripted Modbus TCP endpoint: parses each MBAP request and answers
    from the canned register table, echoing writes like a real server."""

    def __init__(self, on_data, on_disconnect, delimiter) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.delimiter = delimiter
        self.connected = True
        self.requests: list[bytes] = []
        self.chunked = False  # deliver responses split across two chunks

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=b"\r", frame_parser=None,
                     inter_command_delay=0.0, timeout=5.0, name=""):
        if _FAIL_CONNECT:
            raise ConnectionError("connection refused")
        return cls(on_data, on_disconnect, delimiter)

    async def send(self, adu) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        adu = bytes(adu)
        self.requests.append(adu)
        txid, _proto, _length, unit = struct.unpack(">HHHB", adu[:7])
        pdu = adu[7:]
        func = pdu[0]
        if func in (0x01, 0x02):  # read coils / discrete inputs
            resp_pdu = bytes([func, 1, 0x01])
        elif func in (0x03, 0x04):  # read holding / input registers
            addr, count = struct.unpack(">HH", pdu[1:5])
            data = b"".join(
                _REGISTERS.get((func, addr + i), b"\x00\x00")
                for i in range(count)
            )
            resp_pdu = bytes([func, len(data)]) + data
        elif func in (0x05, 0x06):  # write single coil / register: echo
            resp_pdu = pdu
        else:
            resp_pdu = bytes([func | 0x80, 0x01])  # Illegal function
        resp = struct.pack(">HHHB", txid, 0, len(resp_pdu) + 1, unit) + resp_pdu
        if self.chunked:
            mid = len(resp) // 2
            await self.on_data(resp[:mid])
            await self.on_data(resp[mid:])
        else:
            await self.on_data(resp)

    async def close(self) -> None:
        self.connected = False


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server
    for sub in ("drivers", "utils"):
        m = ModuleType(f"server.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"server.{sub}"] = m
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["server.drivers.base"] = base
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("modbus_tcp_under_test", DRIVER_PATH)


# ── Harness ─────────────────────────────────────────────────────────────────

REGISTER_MAP = [
    {"name": "temperature", "area": "input", "address": 10,
     "datatype": "int16", "scale": 0.1, "offset": 0, "access": "r",
     "unit": "C"},
    {"name": "setpoint", "area": "holding", "address": 5,
     "datatype": "uint16", "access": "w"},
]


def _make_driver(config_overrides=None):
    cfg = {
        "host": "10.0.0.90",
        "port": 502,
        "unit_id": 1,
        "poll_interval": 10,
        "register_map": [dict(r) for r in REGISTER_MAP],
    }
    cfg.update(config_overrides or {})
    return DRV.ModbusTCPDriver("plc1", cfg, _FakeState(), _FakeEvents())


# ── Metadata / register-map build ───────────────────────────────────────────

def test_version_and_platform_gate():
    info = DRV.ModbusTCPDriver.DRIVER_INFO
    assert info["version"] == "1.0.1"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    assert info["min_platform_version"] == "0.24.0"
    assert info["transport"] == "tcp"


def test_register_map_builds_instance_driver_info():
    driver = _make_driver()
    info = driver.DRIVER_INFO
    # Read register -> status value; write register -> command; no rw rows.
    assert set(info["state_variables"]) == {"temperature"}
    assert info["state_variables"]["temperature"]["unit"] == "C"
    assert set(info["commands"]) == {"set_setpoint"}
    assert info["device_settings"] == {}
    # The class-level template stays clean of per-instance registers.
    assert DRV.ModbusTCPDriver.DRIVER_INFO["state_variables"] == {}


# ── Connect lifecycle ───────────────────────────────────────────────────────

def test_connect_lifecycle_and_priming_poll():
    async def go():
        driver = _make_driver()
        await driver.connect()
        try:
            assert driver._connected is True
            assert driver.get_state("connected") is True
            # _transport_kwargs dropped the line delimiter for MBAP framing.
            assert driver.transport.delimiter is None
            # The priming poll ran inside the lifecycle, before polling and
            # the watchdog started — and it's the only wire traffic so far.
            assert driver.lifecycle == [
                "declared", "synced", "polling", "watchdog",
            ]
            assert driver.polling_intervals == [10]
            assert len(driver.transport.requests) == 1
            # Exact MBAP + PDU bytes: txid 1, unit 1, FC04 read one word
            # at input address 10.
            assert driver.transport.requests[0] == (
                struct.pack(">HHHB", 1, 0, 6, 1)
                + struct.pack(">BHH", 0x04, 10, 1)
            )
            # int16 0xFFC9 = -55, scaled x0.1.
            assert driver.get_state("temperature") == pytest.approx(-5.5)
            # The driver supplies a liveness probe, so the watchdog runs.
            assert driver._health_task is not None
            assert not driver._health_task.done()
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_connect_failure_keeps_wording():
    async def go():
        global _FAIL_CONNECT
        driver = _make_driver()
        _FAIL_CONNECT = True
        try:
            with pytest.raises(ConnectionError) as excinfo:
                await driver.connect()
        finally:
            _FAIL_CONNECT = False
        assert "Modbus connect to 10.0.0.90:502 failed" in str(excinfo.value)
        assert driver._connected is False
        assert driver.transport is None

    asyncio.run(go())


# ── Reads and writes ────────────────────────────────────────────────────────

def test_read_decodes_across_split_frames():
    """A response delivered in two TCP chunks reassembles via the MBAP
    length field before decoding."""
    async def go():
        driver = _make_driver()
        await driver.connect()
        try:
            driver.state.data.pop("device.plc1.temperature", None)
            driver.transport.chunked = True
            await driver.poll()
            assert driver.get_state("temperature") == pytest.approx(-5.5)
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_write_command_round_trip():
    async def go():
        driver = _make_driver()
        await driver.connect()
        try:
            confirmed = await driver.send_command(
                "set_setpoint", {"value": 123},
            )
            # FC06 echoes the written value; uint16 unscaled stays integral.
            assert confirmed == 123
            # Exact request bytes: txid 2 (after the priming read), unit 1,
            # FC06 write single holding register, address 5, value 123.
            assert driver.transport.requests[-1] == (
                struct.pack(">HHHB", 2, 0, 6, 1)
                + struct.pack(">BHH", 0x06, 5, 123)
            )
            # Write-only register: no state variable to update.
            assert driver.get_state("setpoint") is None
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_liveness_probe_reads_first_mapped_register():
    async def go():
        driver = _make_driver()
        await driver.connect()
        try:
            n = len(driver.transport.requests)
            await driver._liveness_probe()
            assert len(driver.transport.requests) == n + 1
            # The probe re-reads the first readable register (FC04).
            assert driver.transport.requests[-1][7] == 0x04
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Teardown paths ──────────────────────────────────────────────────────────

def test_teardown_cancels_pending_and_clears_buffer():
    """Both teardown paths (graceful disconnect and the transport-drop
    cleanup) run _close_session, so pending transactions are aborted and
    any half-received frame is dropped."""
    async def go():
        # Graceful disconnect.
        driver = _make_driver()
        await driver.connect()
        fut = asyncio.get_event_loop().create_future()
        driver._pending[99] = fut
        driver._buffer.extend(b"\x00\x63\x00\x00\x00\x04")  # partial frame
        await driver.disconnect()
        assert fut.cancelled()
        assert driver._pending == {}
        assert len(driver._buffer) == 0
        assert driver.transport is None
        assert driver.get_state("connected") is False

        # Transport drop.
        driver = _make_driver()
        await driver.connect()
        fut = asyncio.get_event_loop().create_future()
        driver._pending[7] = fut
        driver._buffer.extend(b"\xff")
        driver._handle_transport_disconnect()
        for _ in range(4):
            await asyncio.sleep(0)
        assert fut.cancelled()
        assert driver._pending == {}
        assert len(driver._buffer) == 0
        assert driver.disconnect_calls == 1
        assert driver.get_state("connected") is False

    asyncio.run(go())
