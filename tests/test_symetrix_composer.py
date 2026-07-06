"""Driver + simulator tests for symetrix_composer (Composer Control Protocol).

No Symetrix hardware on hand, so correctness is proven as a **dual-proof
round trip** wiring the real driver to the real simulator over an in-memory
transport that frames on the protocol's \\r terminator.

Covers the v1.3.0 first-class adoption:
  - liveness: a `V` (version) probe awaited through the line dispatcher —
    a controller push (#N=V) does NOT satisfy it, and a silent device
    forces a reconnect with a typed no_response fault (a push driver with
    poll_interval=0 would otherwise never flip offline);
  - poll() now propagates send failures instead of swallowing them;
  - quick actions promote load_preset / flash_unit / refresh.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected; an autouse fixture re-installs them for
each test because the driver imports TCPTransport lazily inside connect()).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "symetrix_composer.py"
SIM_PATH = REPO_ROOT / "audio" / "symetrix_composer_sim.py"


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
    """Stand-in mirroring the platform BaseDriver liveness watchdog."""

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
        self.disconnect_calls = 0
        self.stashed_fault: tuple[str, str] | None = None
        self._health_task = None
        self._health_failures = 0

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    def _handle_transport_disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.transport is not None:
            self.transport.connected = False

    def _stash_fault(self, code, message="") -> None:
        self.stashed_fault = (code, message)

    async def _liveness_probe(self) -> None:
        raise NotImplementedError

    def _start_health_loop(self) -> None:
        if self._health_task is None or self._health_task.done():
            self._health_failures = 0
            self._health_task = asyncio.ensure_future(self._health_loop())

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
        pass

    async def stop_polling(self) -> None:
        pass


class _FakeSimState:
    def __init__(self, initial) -> None:
        self.data = dict(initial)

    def get(self, key, default=None):
        return self.data.get(key, default)


class _FakeTCPSimulator:
    """Stand-in for simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = _FakeSimState(self.SIMULATOR_INFO.get("initial_state", {}))
        self._clients: dict = {}

    def set_state(self, key, value) -> None:
        self.state.data[key] = value

    async def push_to(self, client_id, data: bytes) -> None:
        target = self._clients.get(client_id)
        if target is not None:
            await target._deliver(data)


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport processes requests but DROPS every reply — a
# silently-vanished device for the liveness tests.
_SWALLOW = False


class _FakeTCPTransport:
    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, timeout=5.0, name="", **kw):
        t = cls(on_data, on_disconnect)
        t._sim._clients["c1"] = t
        return t

    async def _deliver(self, data: bytes) -> None:
        """Frame on \\r like the real delimiter transport: one stripped
        line per on_data call."""
        if _SWALLOW:
            return
        for line in bytes(data).split(b"\r"):
            if line:
                await self.on_data(line)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        for raw in bytes(data).split(b"\r"):
            raw = raw.strip()
            if not raw:
                continue
            resp = self._sim.handle_command(raw)
            if resp:
                await self._deliver(resp)

    async def close(self) -> None:
        self.connected = False


def _build_stub_modules() -> dict[str, ModuleType]:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    stubs: dict[str, ModuleType] = {"server": server}
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"server.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        stubs[f"server.{sub}"] = m
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    stubs["server.drivers.base"] = base
    tcp = ModuleType("server.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    stubs["server.transport.tcp"] = tcp
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    stubs["server.utils.logger"] = logger

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    stubs["simulator"] = sim_pkg
    sim_tcp = ModuleType("simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    stubs["simulator.tcp_simulator"] = sim_tcp
    return stubs


_STUB_MODULES = _build_stub_modules()


def _load(name: str, path: Path) -> ModuleType:
    sys.modules.update(_STUB_MODULES)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("symetrix_under_test", DRIVER_PATH)
SIM = _load("symetrix_sim_under_test", SIM_PATH)


@pytest.fixture(autouse=True)
def _stub_platform_modules(monkeypatch):
    """Re-install the platform stubs for each test's runtime — the driver
    imports TCPTransport lazily inside connect(), which resolves through
    sys.modules after conftest.py has rolled the collection-time stubs
    back. monkeypatch restores the originals after each test."""
    for name, mod in _STUB_MODULES.items():
        monkeypatch.setitem(sys.modules, name, mod)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.SymetrixComposerSimulator("sim1", {})
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.5", "port": 48631, "num_controllers": 8}
    cfg.update(driver_overrides or {})
    driver = DRV.SymetrixComposerDriver(
        "dsp1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        # Drain any in-flight sim push tasks so loop teardown stays quiet.
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        loop.close()


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_and_actions_shape():
    info = DRV.SymetrixComposerDriver.DRIVER_INFO
    assert info["version"] == "1.3.0"
    assert info["min_platform_version"] == "0.22.0"
    for cid in info["quick_actions"]:
        assert cid in info["commands"], cid
    assert {a["id"] for a in info["actions"]} == set(info["quick_actions"])


# ── Connect round trip ──────────────────────────────────────────────────────

def test_connect_populates_state_from_sim():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await asyncio.sleep(0.05)  # PUR pushes arrive via create_task
            assert driver._connected
            assert driver.get_state("firmware_version")
            assert driver.get_state("ip_address")
        finally:
            await driver.disconnect()
    _run(scenario())


# ── Liveness ────────────────────────────────────────────────────────────────

def test_probe_resolves_on_version_reply():
    async def scenario():
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            await asyncio.wait_for(driver._liveness_probe(), 1.0)
        finally:
            await driver.disconnect()
    _run(scenario())


def test_probe_times_out_when_silent():
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            _SWALLOW = True
            try:
                await asyncio.wait_for(driver._liveness_probe(), 0.1)
            except asyncio.TimeoutError:
                return
            raise AssertionError("probe resolved with no reply")
        finally:
            _SWALLOW = False
            await driver.disconnect()
    _run(scenario())


def test_controller_push_does_not_satisfy_probe():
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            _SWALLOW = True  # sim replies dropped; we inject lines by hand
            probe = asyncio.ensure_future(driver._liveness_probe())
            await asyncio.sleep(0.05)
            assert not probe.done()
            await driver.on_data_received(b"#00003=12345")
            await asyncio.sleep(0.05)
            assert not probe.done()
            assert driver.get_state("controller_3_value") == 12345
            await driver.on_data_received(b"1.0.7 (3.6.4)")
            await asyncio.wait_for(probe, 1.0)
        finally:
            _SWALLOW = False
            await driver.disconnect()
    _run(scenario())


def test_health_loop_forces_reconnect_on_silent_device():
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        driver._stop_health_loop()  # restart below with test-speed cadence
        driver.HEALTH_INTERVAL_S = 0.01
        driver.HEALTH_TIMEOUT_S = 0.05
        _SWALLOW = True
        try:
            driver._start_health_loop()
            for _ in range(100):
                await asyncio.sleep(0.05)
                if driver.disconnect_calls:
                    break
            assert driver.disconnect_calls == 1
            assert driver.stashed_fault is not None
            code, message = driver.stashed_fault
            assert code == "no_response"
            assert "version" in message
        finally:
            _SWALLOW = False
            driver._stop_health_loop()
            await driver.disconnect()
    _run(scenario())


def test_poll_propagates_send_failure():
    """poll() used to swallow ConnectionError, hiding a dead link from the
    platform's poll loop (the never-offline defect class)."""
    async def scenario():
        driver, _sim = await _make_pair()
        await driver.connect()
        driver._stop_health_loop()

        async def _dead_send(data):
            raise ConnectionError("transport closed")
        driver.transport.send = _dead_send
        try:
            await driver.poll()
        except ConnectionError:
            await driver.disconnect()
            return
        raise AssertionError("poll swallowed the transport failure")
    _run(scenario())
