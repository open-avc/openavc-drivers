"""Driver + simulator tests for atlona_ome_ms (AT-OME-MS matrix switchers).

No MS-series hardware on hand, so correctness is proven two ways: metadata /
shape assertions on the driver, and a **dual-proof round trip** that wires the
real driver to the real simulator over an in-memory transport — the simulator
renders the MS-series compact-JSON protocol, the driver parses it, and the
results are asserted on both sides (same approach as test_wattbox_ip.py).

Covers the v1.3.0 first-class adoption:
  - device settings: volume / both mutes / audio source / USB mode / matrix
    mode write through set_device_setting and read back through their polled
    state_keys;
  - quick actions reference real commands (restart carries a confirm);
  - liveness: the awaited Misc:Model:Get probe resolves while the device
    answers and forces a typed no_response reconnect when it goes silent.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "atlona_ome_ms.py"
SIM_PATH = REPO_ROOT / "switchers" / "atlona_ome_ms_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver surface this driver
    uses: the hook-driven connect()/disconnect() lifecycle and the liveness
    watchdog loop (mirrors base.py)."""

    DRIVER_INFO: dict = {}

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

    # -- connection lifecycle (mirrors the platform's hook-driven connect) --

    async def _pre_connect(self) -> None:
        pass

    async def _post_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    async def _create_transport(self, transport_type) -> None:
        kwargs = dict(
            host=self.config.get("host", ""),
            port=self.config.get("port", 23),
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


class _FakeTCPSimulator:
    """Stand-in for simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport sends to the sim but DROPS the reply — simulating a
# silently-vanished device for the liveness test.
_SWALLOW = False


class _FakeTCPTransport:
    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, frame_parser=None,
                     inter_command_delay=0.0, timeout=5.0, name=""):
        return cls(on_data, on_disconnect)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        resp = self._sim.handle_command(bytes(data))
        if resp and not _SWALLOW:
            await self.on_data(resp)

    async def close(self) -> None:
        self.connected = False


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"server.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"server.{sub}"] = m
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["server.drivers.base"] = base
    tcp = ModuleType("server.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    sys.modules["server.transport.tcp"] = tcp
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["simulator"] = sim_pkg
    sim_tcp = ModuleType("simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    sys.modules["simulator.tcp_simulator"] = sim_tcp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("atlona_ome_ms_under_test", DRIVER_PATH)
SIM = _load("atlona_ome_ms_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.AtlonaOmeMsSimulator("sim1", sim_config or {})
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.9",
        "port": 23,
        "username": "",
        "password": "",
        "poll_interval": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.AtlonaOmeMsDriver("swx1", cfg, _FakeState(), _FakeEvents())
    # Drop the 500 ms Atlona pacing delay so tests run at full speed — the
    # pacing is a wire-etiquette knob, not protocol logic.
    driver._inter_cmd_delay = 0
    return driver, sim


async def _settle(n: int = 4) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_and_platform_gate():
    info = DRV.AtlonaOmeMsDriver.DRIVER_INFO
    assert info["version"] == "1.3.2"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    assert info["min_platform_version"] == "0.24.0"


def test_device_settings_declared():
    info = DRV.AtlonaOmeMsDriver.DRIVER_INFO
    ds = info["device_settings"]
    assert set(ds) == {
        "volume", "mute_hdmi", "mute_analog",
        "audio_source", "usb_mode", "matrix_mode",
    }
    # Every setting reads back through a declared, polled state var.
    for key, entry in ds.items():
        assert entry["state_key"] in info["state_variables"], key
        assert entry["setup"] is False, key
    assert ds["volume"]["min"] == -80 and ds["volume"]["max"] == 0
    assert ds["audio_source"]["values"] == ["digital", "analog"]
    assert ds["usb_mode"]["values"] == ["follow", "manual"]
    # Matrix mode's Get reports only on/off, so the setting is boolean —
    # the three-way command (off/on/static) must stay available.
    assert ds["matrix_mode"]["type"] == "boolean"
    assert "set_matrix_mode" in info["commands"]


def test_actions_reference_real_commands():
    info = DRV.AtlonaOmeMsDriver.DRIVER_INFO
    commands = info["commands"]
    actions = info["actions"]
    assert [a["id"] for a in actions] == [
        "display_on", "display_off", "refresh", "platform_restart",
    ]
    for entry in actions:
        assert entry["id"] in commands, entry["id"]
        assert entry["kind"] == "command"
    restart = next(a for a in actions if a["id"] == "platform_restart")
    assert restart.get("confirm")


# ── Poll population ─────────────────────────────────────────────────────────

def test_connect_poll_populates_state():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await _settle()
            assert driver.get_state("model") == "AT-OME-MS52W"
            assert driver.get_state("volume") == sim._state["volume"]
            assert driver.get_state("matrix_mode") == sim._state["matrix_mode"]
            assert driver.get_state("route_0") == sim._state["route_0"]
            assert driver.get_state("route_1") == sim._state["route_1"]
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Device settings: write → sim mutates → read-back ────────────────────────

def test_device_setting_round_trips():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            cases = [
                ("volume", -25, "volume", -25),
                ("mute_hdmi", True, "mute_hdmi", True),
                ("mute_analog", True, "mute_analog", True),
                ("audio_source", "analog", "audio_source", "analog"),
                ("usb_mode", "manual", "usb_mode", "manual"),
                ("matrix_mode", False, "matrix_mode", False),
            ]
            for key, value, sim_key, expected in cases:
                await driver.set_device_setting(key, value)
                await _settle()
                # Sim side mutated…
                assert sim._state[sim_key] == expected, key
                # …and the set-ack echo updated the driver immediately.
                assert driver.get_state(key) == expected, key

            # Read-back path: wipe local state, re-poll, everything returns.
            for key, _v, _sk, expected in cases:
                driver.state.data.pop(f"device.swx1.{key}", None)
            await driver.poll()
            await _settle()
            for key, _v, _sk, expected in cases:
                assert driver.get_state(key) == expected, key
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_volume_setting_clamps():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.set_device_setting("volume", -500)
            await _settle()
            assert sim._state["volume"] == -80
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_unknown_setting_raises():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            with pytest.raises(ValueError):
                await driver.set_device_setting("nonexistent", 1)
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Liveness: awaited probe + silent-device reconnect ───────────────────────

def test_health_probe_succeeds_when_alive():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await asyncio.wait_for(driver._liveness_probe(), 1.0)
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_health_loop_forces_reconnect_on_silent_device():
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        driver.HEALTH_INTERVAL_S = 0.01
        driver.HEALTH_TIMEOUT_S = 0.05
        await driver.connect()
        try:
            # The platform starts the watchdog itself because the driver
            # overrides _liveness_probe.
            assert driver._health_task is not None
            _SWALLOW = True  # matrix stops replying
            await asyncio.sleep(0.3)
            assert driver.disconnect_calls >= 1
            # Typed no_response fault stashed for the classifier.
            assert driver.stashed_fault is not None
            assert driver.stashed_fault[0] == "no_response"
        finally:
            _SWALLOW = False
            await _settle()
            driver._stop_health_loop()

    asyncio.run(go())


# ── Command pacing ──────────────────────────────────────────────────────────

def test_pacing_waits_only_the_remaining_gap_and_never_trails():
    """Atlona's 500 ms rule is a gap BETWEEN commands, so it is paid before a
    write, never after one.

    Two things must hold: back-to-back sends stay at least the full delay
    apart (the device's requirement), and a send after an idle stretch goes
    out with no wait at all (the caller isn't charged for etiquette it already
    satisfied). Sleeping after each write got the first right and the second
    wrong — every command returned 500 ms late, and a poll cycle paid one
    extra gap on its way out.
    """
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            driver._inter_cmd_delay = 0.05
            sent_at: list[float] = []

            async def _record(data: bytes) -> None:
                sent_at.append(time.monotonic())

            driver.transport.send = _record

            # Back-to-back: the second waits out the gap.
            driver._last_send = time.monotonic()
            await driver._send("Misc:Model:Get")
            await driver._send("Misc:Model:Get")
            assert sent_at[1] - sent_at[0] >= 0.05

            # Idle longer than the gap: the next send does not wait.
            driver._last_send = time.monotonic() - 1.0
            before = time.monotonic()
            await driver._send("Misc:Model:Get")
            assert time.monotonic() - before < 0.02

            # And nothing trails the write — the call returned immediately
            # after it, which is what the assertion above measures.
        finally:
            await _settle()

    asyncio.run(go())
