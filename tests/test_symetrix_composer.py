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

The platform stand-in mirrors the hook-driven connect()/disconnect()
lifecycle (clean slate per attempt, _post_connect abort, _initial_sync
failure teardown, _close_session on every teardown path, watchdog
auto-start), so the hooks this driver overrides run exactly as they do on
the real platform — including the polite ``Q!`` goodbye sent while the link
is still open.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

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
    """Functional stand-in for the platform BaseDriver surface this driver
    uses: the hook-driven connect()/disconnect() lifecycle and the liveness
    watchdog."""

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
        self._bg_tasks: set = set()

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

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
            port=self.config.get("port", 48631),
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
    assert info["version"] == "1.3.3"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    assert info["min_platform_version"] == "0.24.0"
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


def test_disconnect_sends_polite_quit():
    """The goodbye must go out while the link is still open, so the device
    frees the TCP slot immediately instead of aging it out."""
    async def scenario():
        driver, _sim = await _make_pair()
        await driver.connect()
        sent: list[str] = []
        real_send = driver.transport.send

        async def spy(data):
            sent.append(bytes(data).decode("ascii"))
            await real_send(data)

        driver.transport.send = spy
        await driver.disconnect()
        assert any(s.startswith("Q!") for s in sent)
        assert driver.transport is None
        assert not driver._connected
    _run(scenario())


def test_disconnect_unblocks_inflight_probe():
    """Teardown must fail a probe still awaiting its version reply so the
    health loop never hangs on a dead link."""
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        _SWALLOW = True
        try:
            probe = asyncio.ensure_future(driver._liveness_probe())
            await asyncio.sleep(0.05)
            assert not probe.done()
            await driver.disconnect()
            assert driver._probe_fut is None
            try:
                await asyncio.wait_for(probe, 1.0)
            except ConnectionError:
                return
            raise AssertionError("probe survived disconnect")
        finally:
            _SWALLOW = False
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
