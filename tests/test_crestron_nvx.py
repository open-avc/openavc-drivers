"""Driver + simulator tests for crestron_nvx (Crestron DM NVX REST).

No NVX on hand, so correctness is a dual-proof round trip: the real driver's
httpx client is wired to the real simulator's REST handler via
httpx.MockTransport — the driver POSTs /Device/DeviceSpecific, the sim mutates,
the driver's poll re-reads it back, both sides asserted.

Covers the v1.4.0 adoption + fix:
  - device settings: the video/audio source commands are promoted to
    device_settings (already persisted + read back by poll), routing through
    the same DeviceSpecific write;
  - never-offline fix: _api_get swallowed transport errors, so poll() never
    raised and an unreachable NVX stayed shown online. Transport failures now
    propagate so the watchdog flips it offline;
  - a Quick Action strip.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after collection). httpx is a real dependency.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "displays" / "crestron_nvx.py"
SIM_PATH = REPO_ROOT / "displays" / "crestron_nvx_sim.py"


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


DRV = _load("crestron_nvx_under_test", DRIVER_PATH)
SIM = _load("crestron_nvx_sim_under_test", SIM_PATH)


# ── Harness ─────────────────────────────────────────────────────────────────

class _Link:
    """The sim plus a reachability flag so a test can simulate the NVX
    dropping off the network mid-session."""

    def __init__(self, sim) -> None:
        self.sim = sim
        self.reachable = True


def _make_handler(link):
    def handler(request: httpx.Request) -> httpx.Response:
        if not link.reachable:
            raise httpx.ConnectError("Connection refused")
        body = request.content.decode() if request.content else ""
        status, resp_body = link.sim.handle_request(
            request.method, request.url.path, dict(request.headers), body
        )
        if isinstance(resp_body, dict):
            return httpx.Response(status, json=resp_body)
        return httpx.Response(status, text=str(resp_body))

    return handler


def _make_driver(link):
    driver = DRV.CrestronNVXDriver(
        "nvx1",
        {"host": "test", "port": 443, "poll_interval": 0, "auth_enabled": False},
        _FakeState(), _FakeEvents(),
    )
    driver._base_url = "https://test"
    driver._client = httpx.AsyncClient(
        base_url="https://test",
        transport=httpx.MockTransport(_make_handler(link)),
    )
    driver._connected = True
    return driver


async def _close(driver):
    if driver._client:
        await driver._client.aclose()


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.CrestronNVXDriver.DRIVER_INFO["version"] == "1.4.0"


def test_source_settings_promoted():
    info = DRV.CrestronNVXDriver.DRIVER_INFO
    ds = info["device_settings"]
    for key in ("video_source", "audio_source"):
        assert key in ds
        assert ds[key]["state_key"] == key
        assert ds[key]["state_key"] in info["state_variables"]
    # The transient commands stay as a dual surface for macros.
    assert "set_video_source" in info["commands"]
    assert "set_audio_source" in info["commands"]


def test_actions_reference_real_commands():
    info = DRV.CrestronNVXDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for action in info["actions"]:
        cmd = action.get("command", action["id"])
        assert cmd in cmds, f"action {action['id']} -> {cmd} is not a command"


# ── Device settings: write + read-back ──────────────────────────────────────

def test_video_source_setting_round_trip():
    async def go():
        link = _Link(SIM.CrestronNvxSimulator("sim1", {}))
        driver = _make_driver(link)
        try:
            await driver.set_device_setting("video_source", "Input1")
            assert link.sim.get_state("video_source") == "Input1"
            await driver.poll()
            assert driver.get_state("video_source") == "Input1"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_audio_source_setting_round_trip():
    async def go():
        link = _Link(SIM.CrestronNvxSimulator("sim1", {}))
        driver = _make_driver(link)
        try:
            await driver.set_device_setting("audio_source", "Analog")
            assert link.sim.get_state("audio_source") == "Analog"
            await driver.poll()
            assert driver.get_state("audio_source") == "Analog"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_video_source_command_still_works():
    """The transient command stays a usable dual surface."""
    async def go():
        link = _Link(SIM.CrestronNvxSimulator("sim1", {}))
        driver = _make_driver(link)
        try:
            await driver.send_command("set_video_source", {"source": "Input2"})
            assert link.sim.get_state("video_source") == "Input2"
            await driver.poll()
            assert driver.get_state("video_source") == "Input2"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_unknown_device_setting_raises():
    async def go():
        link = _Link(SIM.CrestronNvxSimulator("sim1", {}))
        driver = _make_driver(link)
        try:
            with pytest.raises(ValueError):
                await driver.set_device_setting("nonsense", 1)
        finally:
            await _close(driver)

    asyncio.run(go())


# ── Never-offline fix: a dead link must propagate from poll() ───────────────

def test_poll_propagates_transport_failure():
    async def go():
        link = _Link(SIM.CrestronNvxSimulator("sim1", {}))
        driver = _make_driver(link)
        try:
            await driver.poll()  # reachable — populates state
            assert driver.get_state("video_source") == "Stream"
            # The NVX drops off the network.
            link.reachable = False
            with pytest.raises(ConnectionError):
                await driver.poll()
        finally:
            await _close(driver)

    asyncio.run(go())
