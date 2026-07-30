"""Driver + simulator tests for birddog_codec (BirdDog NDI encoder/decoder REST).

No BirdDog codec on hand, so correctness is a dual-proof round trip: the real
driver's httpx client is wired to the real simulator's REST handler via
httpx.MockTransport — the driver POSTs /encodesetup etc., the sim mutates, the
driver's poll re-reads it back, both sides asserted. The API split (HostName via
GET /about vs NDIName via GET /encodesetup) is per the BirdDog RESTful API 2.0
reference.

Covers the v1.5.0 adoption + fix:
  - read-back fix (audit §4.9): the ndi_name device_setting pointed its
    state_key at `hostname` (a different value), so it showed the wrong thing.
    NDI name now has its own state var read from GET /encodesetup, and the
    setting reads that back — without corrupting the hostname;
  - param picker: the cached NDI source list is published as a JSON state var
    so Select NDI Source is a dropdown (options_state);
  - a Quick Action strip.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after collection). httpx is a real dependency.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "video" / "birddog_codec.py"
SIM_PATH = REPO_ROOT / "video" / "birddog_codec_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
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


class _FakeHTTPSimulator:
    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self.active_errors: set = set()

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


DRV = _load("birddog_codec_under_test", DRIVER_PATH)
SIM = _load("birddog_codec_sim_under_test", SIM_PATH)


# ── Harness ─────────────────────────────────────────────────────────────────

def _make_handler(sim):
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode() if request.content else ""
        status, resp_body = sim.handle_request(
            request.method, request.url.path, dict(request.headers), body
        )
        if isinstance(resp_body, dict):
            return httpx.Response(status, json=resp_body)
        return httpx.Response(status, text=str(resp_body))

    return handler


def _make_driver(sim):
    driver = DRV.BirdDogCodecDriver(
        "codec1", {"host": "test", "port": 8080, "poll_interval": 0},
        _FakeState(), _FakeEvents(),
    )
    driver._base_url = "http://test"
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
    assert DRV.BirdDogCodecDriver.DRIVER_INFO["version"] == "1.5.1"
    assert DRV.BirdDogCodecDriver.DRIVER_INFO["min_platform_version"] == "0.24.0"


def test_ndi_name_setting_reads_back_its_own_state_key():
    # The §4.9 fix: ndi_name must read back from its own state var, not the
    # hostname (a different value on the device).
    ds = DRV.BirdDogCodecDriver.DRIVER_INFO["device_settings"]
    assert ds["ndi_name"]["state_key"] == "ndi_name"
    assert ds["hostname"]["state_key"] == "hostname"
    state_vars = DRV.BirdDogCodecDriver.DRIVER_INFO["state_variables"]
    assert "ndi_name" in state_vars


def test_select_source_uses_picker():
    cmds = DRV.BirdDogCodecDriver.DRIVER_INFO["commands"]
    assert cmds["select_source"]["params"]["source_name"]["options_state"] == "ndi_sources"


def test_actions_reference_real_commands():
    info = DRV.BirdDogCodecDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for action in info["actions"]:
        cmd = action.get("command", action["id"])
        assert cmd in cmds, f"action {action['id']} -> {cmd} is not a command"


# ── §4.9 read-back fix: ndi_name is its own value, not the hostname ─────────

def test_ndi_name_reads_back_from_encodesetup():
    async def go():
        sim = SIM.BirddogCodecSimulator("sim1", {})
        sim.set_state("operation_mode", "Encode")
        sim.set_state("ndi_name", "Lobby Cam")
        driver = _make_driver(sim)
        try:
            await driver.poll()
            # Read from GET /encodesetup, not GET /about.
            assert driver.get_state("ndi_name") == "Lobby Cam"
            # The hostname is a separate value and must not be conflated.
            assert driver.get_state("hostname") != "Lobby Cam"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_setting_ndi_name_does_not_touch_hostname():
    async def go():
        sim = SIM.BirddogCodecSimulator("sim1", {})
        sim.set_state("operation_mode", "Encode")
        driver = _make_driver(sim)
        try:
            await driver.set_device_setting("ndi_name", "Studio B")
            # The write hit /encodesetup (NDIName), not /about (HostName).
            assert sim.get_state("ndi_name") == "Studio B"
            assert sim.get_state("hostname") == "BIRDDOG-CODEC-SIM"
            await driver.poll()
            assert driver.get_state("ndi_name") == "Studio B"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_hostname_setting_round_trip():
    async def go():
        sim = SIM.BirddogCodecSimulator("sim1", {})
        driver = _make_driver(sim)
        try:
            await driver.set_device_setting("hostname", "STAGE-LEFT")
            assert sim.get_state("hostname") == "STAGE-LEFT"
            assert driver.get_state("hostname") == "STAGE-LEFT"
            # The NDI name is untouched by a hostname write.
            assert sim.get_state("ndi_name") == "Studio Camera"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_operation_mode_setting_round_trip():
    async def go():
        sim = SIM.BirddogCodecSimulator("sim1", {})
        driver = _make_driver(sim)
        try:
            await driver.set_device_setting("operation_mode", "Encode")
            assert sim.get_state("operation_mode") == "Encode"
            await driver.poll()
            assert driver.get_state("operation_mode") == "Encode"
        finally:
            await _close(driver)

    asyncio.run(go())


# ── Param picker: NDI source list published as a JSON state var ─────────────

def test_ndi_sources_published_as_json_list():
    async def go():
        sim = SIM.BirddogCodecSimulator("sim1", {})
        driver = _make_driver(sim)
        try:
            await driver.poll()
            raw = driver.get_state("ndi_sources")
            assert raw is not None
            names = json.loads(raw)
            assert names == ["NDI Source 1", "NDI Source 2", "NDI Source 3"]
            assert driver.get_state("source_count") == 3
        finally:
            await _close(driver)

    asyncio.run(go())


def test_select_source_switches_and_picker_lists_it():
    async def go():
        sim = SIM.BirddogCodecSimulator("sim1", {})
        driver = _make_driver(sim)
        try:
            await driver.poll()
            names = json.loads(driver.get_state("ndi_sources"))
            target = names[1]
            await driver.send_command("select_source", {"source_name": target})
            assert sim.get_state("decode_source") == target
        finally:
            await _close(driver)

    asyncio.run(go())


def test_unknown_device_setting_raises():
    async def go():
        sim = SIM.BirddogCodecSimulator("sim1", {})
        driver = _make_driver(sim)
        try:
            with pytest.raises(ValueError):
                await driver.set_device_setting("nonsense", 1)
        finally:
            await _close(driver)

    asyncio.run(go())
