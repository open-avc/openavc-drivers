"""Driver + simulator tests for biamp_tesira_ttp (Tesira Text Protocol).

No Tesira hardware on hand, so correctness is proven as a **dual-proof round
trip** wiring the real driver to the real simulator: the sim speaks the IAC +
welcome-banner handshake and answers TTP lines, the driver parses them, and
results are asserted on both sides (same approach as
test_blackmagic_videohub.py).

Covers the v2.2.0 first-class adoption:
  - liveness: `DEVICE get version` probe correlated through the pending-GET
    FIFO — a +OK or -ERR reply resolves it, a subscription push does NOT,
    and a silent device forces a reconnect with a typed no_response fault
    (a push driver with poll_interval=0 would otherwise never flip offline);
  - reconnect clears stale pending-GET entries (a session that died with
    unanswered GETs would otherwise mis-route the new session's replies);
  - quick actions + the Test Connection / Verify Blocks setup wizard against
    a real in-test TTP server (ok path, typo'd instance tag, unreachable);
  - the class-level catalog surface (generic commands / system state vars)
    is non-empty.

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

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "biamp_tesira_ttp.py"
SIM_PATH = REPO_ROOT / "audio" / "biamp_tesira_ttp_sim.py"

TEST_BLOCKS = """\
Level1 level 1-4
Mute1 mute 1-4
PgmSrc source_select
"""


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

    # -- liveness watchdog (mirrors the platform BaseDriver: probe every
    # HEALTH_INTERVAL_S under a HEALTH_TIMEOUT_S deadline; HEALTH_MAX_FAILURES
    # misses force a disconnect with a typed no_response fault) --

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


class _DelimiterFrameParser:
    """Functional stand-in for server.transport.frame_parsers."""

    def __init__(self, delimiter=b"\r\n") -> None:
        self.delimiter = delimiter
        self._buf = bytearray()

    def feed(self, data):
        self._buf.extend(data)
        frames = []
        while True:
            idx = self._buf.find(self.delimiter)
            if idx < 0:
                break
            frames.append(bytes(self._buf[:idx]))
            del self._buf[: idx + len(self.delimiter)]
        return frames


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
        """Mirror the framework's per-client push. The registered client is
        either the in-memory fake transport or an asyncio StreamWriter."""
        target = self._clients.get(client_id)
        if target is None:
            return
        deliver = getattr(target, "_deliver", None)
        if deliver is not None:
            await deliver(data)
        else:
            try:
                target.write(data)
                await target.drain()
            except (ConnectionError, OSError):
                self._clients.pop(client_id, None)


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport processes requests but DROPS every reply — a
# silently-vanished device for the liveness tests.
_SWALLOW = False


class _FakeTCPTransport:
    """In-memory transport pairing the driver with the live simulator.

    Mirrors the two-phase Tesira link: raw mode during the IAC/banner
    handshake (no _frame_parser), then the driver swaps in the \\r\\n
    delimiter parser and every sim reply is framed through it.
    """

    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._frame_parser = None
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, inter_command_delay=0.0, name="",
                     local_addr=None, **kw):
        t = cls(on_data, on_disconnect)
        t._sim._clients["c1"] = t
        greeting = await t._sim.on_client_connected("c1")
        if greeting:
            await t._deliver(greeting)
        return t

    async def _deliver(self, data: bytes) -> None:
        if _SWALLOW:
            return
        if self._frame_parser is None:
            await self.on_data(data)
        else:
            for frame in self._frame_parser.feed(data):
                await self.on_data(frame)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        for raw in bytes(data).split(b"\n"):
            raw = raw.strip(b"\r")
            if not raw:
                continue
            # Pure-IAC payloads (the driver's WONT/DONT replies) decode to
            # an empty line inside the sim and are ignored there.
            resp = self._sim.handle_command(raw)
            if resp:
                await self._deliver(resp)

    async def close(self) -> None:
        self.connected = False


class _FakeSystemConfig:
    def get(self, section, key, default=None):
        return default


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
    parsers = ModuleType("server.transport.frame_parsers")
    parsers.DelimiterFrameParser = _DelimiterFrameParser
    stubs["server.transport.frame_parsers"] = parsers
    sysconf = ModuleType("server.system_config")
    sysconf.get_system_config = lambda: _FakeSystemConfig()
    stubs["server.system_config"] = sysconf
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


DRV = _load("tesira_under_test", DRIVER_PATH)
SIM = _load("tesira_sim_under_test", SIM_PATH)


@pytest.fixture(autouse=True)
def _stub_platform_modules(monkeypatch):
    """Re-install the platform stubs for each test's runtime.

    The driver imports TCPTransport / get_system_config lazily inside
    connect(), which resolves through sys.modules at call time — after
    conftest.py has rolled the collection-time stubs back. Without this the
    lazy import would hit the real platform (when installed) or ImportError
    (community CI). monkeypatch restores the originals after each test.
    """
    for name, mod in _STUB_MODULES.items():
        monkeypatch.setitem(sys.modules, name, mod)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.BiampTesiraTTPSimulator("sim1", {})
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.5",
        "port": 23,
        "blocks": TEST_BLOCKS,
        "poll_interval": 0,
        "inter_command_delay": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.BiampTesiraTTPDriver("dsp1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_and_actions_shape():
    info = DRV.BiampTesiraTTPDriver.DRIVER_INFO
    assert info["version"] == "2.2.0"
    assert info["min_platform_version"] == "0.22.0"

    # The class-level catalog surface must not be empty (the generic escape
    # hatches and system vars are always present; __init__ re-expands).
    assert "recall_preset" in info["commands"]
    assert "firmware_version" in info["state_variables"]

    # Every promoted quick action resolves to a declared command.
    for cid in info["quick_actions"]:
        assert cid in info["commands"], cid
    action_ids = {a["id"] for a in info["actions"]}
    assert "test_connection" in action_ids
    setup = next(a for a in info["actions"] if a["id"] == "test_connection")
    assert setup["kind"] == "setup"
    assert setup["availability"] == "always"


def test_per_instance_expansion_still_works():
    driver, _sim = _run(_make_pair())
    assert "Level1_level_set" in driver.DRIVER_INFO["commands"]
    assert "Level1_level_1" in driver.DRIVER_INFO["state_variables"]
    assert "recall_preset" in driver.DRIVER_INFO["commands"]


# ── Connect round trip ──────────────────────────────────────────────────────

def test_connect_handshake_and_initial_state():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert driver._connected
            # Metadata gets routed through the pending-GET FIFO.
            assert driver.get_state("serial_number") == "SIM00001"
            assert driver.get_state("firmware_version") == "4.14.0"
            # Subscriptions registered on the sim side.
            subs = sim._client_subs["c1"]
            assert len(subs) == len(driver._subscriptions)
            # Initial GET populated block state from the sim's DSP state.
            assert driver.get_state("Level1_level_1") == -10.0
            assert driver.get_state("Mute1_mute_2") is False
        finally:
            await driver.disconnect()
    _run(scenario())


def test_reconnect_clears_stale_pending_gets():
    """A session that died with unanswered GETs must not desync the next
    session's reply routing (latent-defect regression)."""
    async def scenario():
        driver, sim = await _make_pair()
        # Simulate a dead session's leftovers: two never-answered GETs.
        driver._pending_gets.append(("Level1_level_1", "number"))
        driver._pending_gets.append(("Mute1_mute_1", "boolean"))
        await driver.connect()
        try:
            # If connect() hadn't cleared the queue, the serial-number reply
            # would have landed in Level1_level_1.
            assert driver.get_state("serial_number") == "SIM00001"
            assert driver.get_state("Level1_level_1") == -10.0
        finally:
            await driver.disconnect()
    _run(scenario())


# ── Liveness ────────────────────────────────────────────────────────────────

def test_liveness_probe_resolves_on_reply():
    async def scenario():
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            await asyncio.wait_for(driver._liveness_probe(), 1.0)
        finally:
            await driver.disconnect()
    _run(scenario())


def test_liveness_probe_times_out_when_silent():
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


def test_push_does_not_satisfy_probe():
    """A subscription push proves nothing about the probe round-trip — only
    a reply that consumes the probe's FIFO entry may resolve it."""
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            _SWALLOW = True  # sim replies dropped; we inject lines by hand
            probe = asyncio.ensure_future(driver._liveness_probe())
            await asyncio.sleep(0.05)
            assert not probe.done()
            # A push arrives — probe must stay pending.
            await driver.on_data_received(
                b'! "publishToken":"Level1_level_1" "value":-6.0')
            await asyncio.sleep(0.05)
            assert not probe.done()
            assert driver.get_state("Level1_level_1") == -6.0
            # The version reply arrives — probe resolves.
            await driver.on_data_received(b'+OK "value":"4.14.0"')
            await asyncio.wait_for(probe, 1.0)
        finally:
            _SWALLOW = False
            await driver.disconnect()
    _run(scenario())


def test_err_reply_satisfies_probe():
    """-ERR still proves the device answered (alive, just unhappy)."""
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            _SWALLOW = True
            probe = asyncio.ensure_future(driver._liveness_probe())
            await asyncio.sleep(0.05)
            assert not probe.done()
            await driver.on_data_received(b"-ERR Simulated failure")
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
            assert "DEVICE get version" in message
        finally:
            _SWALLOW = False
            driver._stop_health_loop()
            await driver.disconnect()
    _run(scenario())


# ── Setup wizard against a real in-test TTP server ──────────────────────────

class _TTPServer:
    """The real simulator behind a real asyncio TCP socket."""

    def __init__(self, sim) -> None:
        self.sim = sim
        self.server = None
        self.port = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(
            self._on_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _on_client(self, reader, writer):
        cid = f"w{id(writer)}"
        self.sim._clients[cid] = writer
        greeting = await self.sim.on_client_connected(cid)
        if greeting:
            writer.write(greeting)
            await writer.drain()
        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(256)
                if not chunk:
                    break
                buf.extend(chunk)
                while b"\n" in buf:
                    line, _, rest = bytes(buf).partition(b"\n")
                    buf = bytearray(rest)
                    resp = self.sim.handle_command(line)
                    if resp:
                        writer.write(resp)
                        await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


async def _progress(step, pct=None):
    pass


def test_setup_wizard_all_blocks_ok():
    async def scenario():
        driver, sim = await _make_pair()
        async with _TTPServer(sim) as srv:
            driver.config["host"] = "127.0.0.1"
            driver.config["port"] = srv.port
            result = await driver.run_setup_action(
                "test_connection", {}, _progress)
        assert result["firmware"] == "4.14.0"
        assert result["blocks_failed"] == []
        assert set(result["blocks_ok"]) == {"Level1", "Mute1", "PgmSrc"}
    _run(scenario())


def test_setup_wizard_flags_typoed_tag():
    """Real Tesira answers `-ERR address not found` for an unknown instance
    tag; the stock sim deliberately auto-seeds any tag so arbitrary user
    block lists work against it — use a strict subclass for the typo path."""
    class _StrictSim(SIM.BiampTesiraTTPSimulator):
        def _handle_get(self, tag, rest):
            if not any(t == tag for (t, _a, _i) in self._dsp):
                return SIM._err(f"address not found: {tag}")
            return super()._handle_get(tag, rest)

    async def scenario():
        global _CURRENT_SIM
        driver, _sim = await _make_pair(
            {"blocks": "Level1 level 1-4\nLvel2 level 1-2\n"})
        sim = _StrictSim("sim-strict", {})
        _CURRENT_SIM = sim
        async with _TTPServer(sim) as srv:
            driver.config["host"] = "127.0.0.1"
            driver.config["port"] = srv.port
            result = await driver.run_setup_action(
                "test_connection", {}, _progress)
        assert result["blocks_ok"] == ["Level1"]
        assert result["blocks_failed"] == ["Lvel2"]
    _run(scenario())


def test_setup_wizard_unreachable_host():
    async def scenario():
        driver, _sim = await _make_pair()
        # A port nothing listens on — connect is refused immediately.
        driver.config["host"] = "127.0.0.1"
        driver.config["port"] = 1
        try:
            await driver.run_setup_action("test_connection", {}, _progress)
        except ConnectionError as exc:
            assert "Telnet" in str(exc)
            return
        raise AssertionError("expected ConnectionError")
    _run(scenario())
