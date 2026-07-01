"""Driver + simulator tests for mersive_solstice (Solstice OpenControl REST).

No Solstice Pod on hand, so correctness is a dual-proof round trip: the real
driver's httpx client is wired to the real simulator's OpenControl handler via
httpx.MockTransport — the driver POSTs /api/config, the sim mutates, the
driver's poll re-reads /api/stats + /api/config, both sides asserted. The sim
enforces the admin password (401 on a mismatch) exactly like a real Pod.

Covers the v1.4.0 adoption + fix:
  - device setting: display_name promoted from a command (already persisted +
    read back);
  - connection-fault: a wrong admin password (HTTP 401/403) now raises an
    auth-worded ConnectionError on connect (classifier -> auth_failed);
  - the Test Admin Password setup wizard accepts / rejects out-of-band.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after collection). httpx is a real dependency.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "streaming" / "mersive_solstice.py"
SIM_PATH = REPO_ROOT / "streaming" / "mersive_solstice_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value


class _FakeEvents:
    def __init__(self) -> None:
        self.emitted: list[str] = []

    async def emit(self, name, *args, **kwargs):
        self.emitted.append(name)


class _FakeBaseDriver:
    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self.config_updates: list[dict] = []
        self.reconnects = 0

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def set_states(self, updates) -> None:
        for k, v in updates.items():
            self.state.set(k, v)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    def _handle_transport_disconnect(self) -> None:
        pass

    async def start_polling(self, interval) -> None:
        pass

    async def stop_polling(self) -> None:
        pass

    async def request_config_update(self, delta) -> None:
        self.config_updates.append(delta)
        self.config.update(delta)

    async def request_reconnect(self) -> None:
        self.reconnects += 1


class _FakeHTTPSimulator:
    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value) -> None:
        self._state[key] = value

    def log_protocol(self, *a, **k) -> None:
        pass


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

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["simulator"] = sim_pkg
    sim_http = ModuleType("simulator.http_simulator")
    sim_http.HTTPSimulator = _FakeHTTPSimulator
    sys.modules["simulator.http_simulator"] = sim_http

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("mersive_solstice_under_test", DRIVER_PATH)
SIM = _load("mersive_solstice_sim_under_test", SIM_PATH)


# ── Harness ─────────────────────────────────────────────────────────────────

def _make_handler(sim):
    def handler(request: httpx.Request) -> httpx.Response:
        # Solstice carries the GET password in the query string, so pass the
        # full path + query to the sim (request.url.path drops the query).
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        body = request.content.decode() if request.content else ""
        status, resp_body = sim.handle_request(
            request.method, path, dict(request.headers), body
        )
        if isinstance(resp_body, dict):
            return httpx.Response(status, json=resp_body)
        return httpx.Response(status, text=str(resp_body))

    return handler


@contextlib.contextmanager
def _patched_httpx(sim):
    """Route every httpx.AsyncClient the driver builds (in connect() and the
    setup wizard) to the sim via MockTransport, then restore."""
    real = httpx.AsyncClient

    def factory(*a, **kw):
        return real(
            base_url=kw.get("base_url", ""),
            timeout=kw.get("timeout", 10.0),
            transport=httpx.MockTransport(_make_handler(sim)),
        )

    httpx.AsyncClient = factory
    try:
        yield
    finally:
        httpx.AsyncClient = real


def _make_driver_bypass(sim, admin_password=""):
    """Construct the driver with a mock-backed client, skipping connect()."""
    driver = DRV.SolsticeDriver(
        "pod1",
        {"host": "test", "port": 80, "admin_password": admin_password,
         "poll_interval": 0},
        _FakeState(), _FakeEvents(),
    )
    driver._base_url = "http://test"
    driver._admin_password = admin_password
    driver._client = httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(_make_handler(sim)),
    )
    driver._connected = True
    return driver


async def _close(driver):
    if driver._client:
        await driver._client.aclose()


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.SolsticeDriver.DRIVER_INFO["version"] == "1.4.0"


def test_display_name_promoted_to_setting():
    info = DRV.SolsticeDriver.DRIVER_INFO
    ds = info["device_settings"]
    assert "display_name" in ds
    assert ds["display_name"]["state_key"] == "display_name"
    assert ds["display_name"]["state_key"] in info["state_variables"]
    # The transient command stays a dual surface.
    assert "set_display_name" in info["commands"]


def test_actions_reference_real_commands():
    info = DRV.SolsticeDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for action in info["actions"]:
        if action.get("kind") == "setup":
            continue
        cmd = action.get("command", action["id"])
        assert cmd in cmds, f"action {action['id']} -> {cmd} is not a command"


def test_setup_action_declared():
    setup = [a for a in DRV.SolsticeDriver.DRIVER_INFO["actions"]
             if a.get("kind") == "setup"]
    assert len(setup) == 1
    assert setup[0]["id"] == "test_password"


# ── Device settings: write + read-back ──────────────────────────────────────

def test_display_name_setting_round_trip():
    async def go():
        sim = SIM.MersiveSolsticeSimulator("sim1", {})
        driver = _make_driver_bypass(sim)
        try:
            await driver.set_device_setting("display_name", "Boardroom A")
            assert sim.get_state("display_name") == "Boardroom A"
            await driver.poll()
            assert driver.get_state("display_name") == "Boardroom A"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_screen_key_setting_round_trip():
    async def go():
        sim = SIM.MersiveSolsticeSimulator("sim1", {})
        driver = _make_driver_bypass(sim)
        try:
            await driver.set_device_setting("screen_key_enabled", False)
            await driver.poll()
            assert driver.get_state("screen_key_enabled") is False
        finally:
            await _close(driver)

    asyncio.run(go())


def test_unknown_device_setting_raises():
    async def go():
        sim = SIM.MersiveSolsticeSimulator("sim1", {})
        driver = _make_driver_bypass(sim)
        try:
            with pytest.raises(ValueError):
                await driver.set_device_setting("nonsense", 1)
        finally:
            await _close(driver)

    asyncio.run(go())


# ── Connect + poll ──────────────────────────────────────────────────────────

def test_connect_and_poll_populate_state():
    async def go():
        sim = SIM.MersiveSolsticeSimulator("sim1", {})
        with _patched_httpx(sim):
            driver = DRV.SolsticeDriver(
                "pod1", {"host": "test", "port": 80, "poll_interval": 0},
                _FakeState(), _FakeEvents(),
            )
            await driver.connect()
            try:
                assert driver._connected is True
                assert driver.get_state("display_name") == "Solstice Sim Room"
            finally:
                await _close(driver)

    asyncio.run(go())


# ── CF fix: wrong admin password -> auth-worded ConnectionError ─────────────

def test_wrong_password_raises_auth_worded_error():
    async def go():
        sim = SIM.MersiveSolsticeSimulator("sim1", {})
        sim.set_state("admin_password", "secret")
        with _patched_httpx(sim):
            driver = DRV.SolsticeDriver(
                "pod1",
                {"host": "test", "port": 80, "admin_password": "wrong",
                 "poll_interval": 0},
                _FakeState(), _FakeEvents(),
            )
            with pytest.raises(ConnectionError) as exc:
                await driver.connect()
            assert "authentication failed" in str(exc.value).lower()

    asyncio.run(go())


def test_correct_password_connects():
    async def go():
        sim = SIM.MersiveSolsticeSimulator("sim1", {})
        sim.set_state("admin_password", "secret")
        with _patched_httpx(sim):
            driver = DRV.SolsticeDriver(
                "pod1",
                {"host": "test", "port": 80, "admin_password": "secret",
                 "poll_interval": 0},
                _FakeState(), _FakeEvents(),
            )
            await driver.connect()
            try:
                assert driver._connected is True
            finally:
                await _close(driver)

    asyncio.run(go())


# ── Setup wizard: Test Admin Password ──────────────────────────────────────

async def _noop_progress(step, pct=None):
    return None


def test_setup_wizard_accepts_and_saves():
    async def go():
        sim = SIM.MersiveSolsticeSimulator("sim1", {})
        sim.set_state("admin_password", "secret")
        with _patched_httpx(sim):
            driver = DRV.SolsticeDriver(
                "pod1",
                {"host": "test", "port": 80, "admin_password": "",
                 "poll_interval": 0},
                _FakeState(), _FakeEvents(),
            )
            result = await driver.run_setup_action(
                "test_password",
                {"admin_password": "secret", "save": True},
                _noop_progress,
            )
            assert result["auth_ok"] is True
            assert result["saved"] is True
            assert driver.config_updates[-1]["admin_password"] == "secret"
            assert driver.reconnects == 1

    asyncio.run(go())


def test_setup_wizard_rejects_bad_password():
    async def go():
        sim = SIM.MersiveSolsticeSimulator("sim1", {})
        sim.set_state("admin_password", "secret")
        with _patched_httpx(sim):
            driver = DRV.SolsticeDriver(
                "pod1",
                {"host": "test", "port": 80, "admin_password": "",
                 "poll_interval": 0},
                _FakeState(), _FakeEvents(),
            )
            with pytest.raises(ConnectionError):
                await driver.run_setup_action(
                    "test_password",
                    {"admin_password": "nope", "save": True},
                    _noop_progress,
                )
            assert driver.config_updates == []
            assert driver.reconnects == 0

    asyncio.run(go())
