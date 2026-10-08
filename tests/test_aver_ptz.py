"""Driver + simulator tests for aver_ptz (AVer PTZ310/330 HTTP CGI + VISCA).

No AVer camera on hand, so correctness is proven two ways: metadata/shape
assertions, and a **dual-proof round trip** that wires the real driver's httpx
client to the real simulator's CGI handler via httpx.MockTransport — the driver
fires /storks?cmd=... setters, the sim mutates, the driver's bulk get_sys_stat
poll parses the result, both sides asserted. (The VISCA-over-UDP path that
connect() also opens is unchanged by the v1.3.0 adoption and out of scope here,
so the tests drive the HTTP image surface directly instead of through connect().)

Covers the v1.3.0 first-class adoption:
  - device settings: the 19-entry image / exposure / WB / picture / AI surface
    writes + reads back through the get_sys_stat poll;
  - connection-fault: a 401 (the camera refusing, or asking for, the Basic
    credentials) is the typed auth_failed fault, from a command, from connect
    and out of the poll at the first refusal; a 403 keeps its untyped error.

Loads the driver + simulator with the ``openavc.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected). httpx is a real dependency.
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

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    ConnectionFaultError,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "cameras" / "aver_ptz.py"
SIM_PATH = REPO_ROOT / "cameras" / "aver_ptz_sim.py"


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
        self.active_errors: set[str] = set()

    def inject_error(self, mode) -> None:
        assert mode in self.SIMULATOR_INFO.get("error_modes", {}), mode
        self.active_errors.add(mode)

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value) -> None:
        self._state[key] = value


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc"] = server
    for sub in ("drivers", "utils"):
        m = ModuleType(f"openavc.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"openavc.{sub}"] = m
    base = ModuleType("openavc.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    base.ConnectionFaultError = ConnectionFaultError
    sys.modules["openavc.drivers.base"] = base
    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger

    sim_pkg = ModuleType("openavc.simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc.simulator"] = sim_pkg
    sim_http = ModuleType("openavc.simulator.http_simulator")
    sim_http.HTTPSimulator = _FakeHTTPSimulator
    sys.modules["openavc.simulator.http_simulator"] = sim_http

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("aver_ptz_under_test", DRIVER_PATH)
SIM = _load("aver_ptz_sim_under_test", SIM_PATH)


# ── Harness: wire the driver's httpx client to the sim CGI handler ──────────

def _make_handler(sim, reject_paths=(), reject_status=401):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Simulate the camera's HTTP 401 for auth-required commands.
        if any(p in url for p in reject_paths):
            return httpx.Response(reject_status, text="unauthorized")
        body = request.content.decode() if request.content else ""
        status, resp_body = sim.handle_request(
            request.method, url, dict(request.headers), body
        )
        if isinstance(resp_body, dict):
            return httpx.Response(status, json=resp_body)
        return httpx.Response(status, text=str(resp_body))

    return handler


def _make_driver(sim, reject_paths=(), reject_status=401, username="",
                 password=""):
    """Construct the driver and wire its httpx client to the sim, bypassing
    connect()'s VISCA-over-UDP setup (unchanged, out of scope)."""
    driver = DRV.AVerPTZDriver(
        "cam1",
        {"host": "test", "port": 80, "poll_interval": 0,
         "username": username, "password": password},
        _FakeState(), _FakeEvents(),
    )
    driver._base_url = "http://test"
    driver._http = httpx.AsyncClient(
        base_url="http://test",
        auth=(username, password) if username else None,
        transport=httpx.MockTransport(
            _make_handler(sim, reject_paths, reject_status)),
    )
    driver._inquiry_lock = asyncio.Lock()
    driver._connected = True
    return driver


async def _close(driver):
    if driver._http:
        await driver._http.aclose()


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.AVerPTZDriver.DRIVER_INFO["version"] == "1.3.5"
    assert DRV.AVerPTZDriver.DRIVER_INFO["min_platform_version"] == "0.36.0"


def test_the_rtmp_stream_key_is_a_secret():
    # A secret parameter is masked in the server log, on the device page and
    # in a device audit's report, the URL-quoted form on the wire included.
    params = DRV.AVerPTZDriver.DRIVER_INFO["commands"]["rtmp_start"]["params"]
    assert params["key"].get("secret") is True
    assert not params["server"].get("secret")


def test_device_settings_declared():
    ds = DRV.AVerPTZDriver.DRIVER_INFO["device_settings"]
    expected = {
        "exposure_mode", "exposure_value", "shutter_value", "iris_value",
        "gain_value", "gain_limit", "slow_shutter", "back_light", "wb_mode",
        "color_temperature", "saturation", "contrast", "sharpness",
        "noise_filter", "mirror_flip", "power_frequency", "pt_slow",
        "smart_shoot", "smart_framing",
    }
    assert set(ds) == expected
    state_vars = DRV.AVerPTZDriver.DRIVER_INFO["state_variables"]
    for key, defn in ds.items():
        assert defn["state_key"] in state_vars, key
    # Every DS routes to a real command.
    cmds = set(DRV.AVerPTZDriver.DRIVER_INFO["commands"])
    for command, _ in DRV.AVerPTZDriver._DS_COMMANDS.values():
        assert command in cmds, command


def test_quick_actions_reference_real_commands():
    info = DRV.AVerPTZDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for qa in info["quick_actions"]:
        assert qa in cmds, f"quick_action {qa} is not a real command"


# ── Device settings: write + read-back across the surface ───────────────────

# (setting key, value to write, expected driver state after read-back)
_DS_CASES = [
    ("exposure_mode", "manual", "manual"),
    ("exposure_value", -3, -3),
    ("shutter_value", 10, 10),
    ("iris_value", 9, 9),
    ("gain_value", 12, 12),
    ("gain_limit", 6, 6),
    ("slow_shutter", True, True),
    ("back_light", "high", "high"),
    ("wb_mode", "indoor", "indoor"),
    ("color_temperature", 7200, 7200),
    ("saturation", 8, 8),
    ("contrast", 3, 3),
    ("sharpness", 2, 2),
    ("noise_filter", "high", "high"),
    ("mirror_flip", "both", "both"),
    ("power_frequency", "50hz", "50hz"),
    ("pt_slow", True, True),
    ("smart_shoot", True, True),
    ("smart_framing", True, True),
]


@pytest.mark.parametrize("key,value,expected", _DS_CASES)
def test_device_setting_round_trip(key, value, expected):
    async def go():
        sim = SIM.AverPtzSimulator("sim1", {})
        driver = _make_driver(sim)
        try:
            await driver.set_device_setting(key, value)
            await driver.poll()
            assert driver.get_state(key) == expected
        finally:
            await _close(driver)

    asyncio.run(go())


def test_backlight_bool_setting_coerces_string():
    async def go():
        sim = SIM.AverPtzSimulator("sim1", {})
        driver = _make_driver(sim)
        try:
            await driver.set_device_setting("slow_shutter", "true")
            await driver.poll()
            assert driver.get_state("slow_shutter") is True
            await driver.set_device_setting("slow_shutter", "false")
            await driver.poll()
            assert driver.get_state("slow_shutter") is False
        finally:
            await _close(driver)

    asyncio.run(go())


def test_unknown_device_setting_raises():
    async def go():
        sim = SIM.AverPtzSimulator("sim1", {})
        driver = _make_driver(sim)
        try:
            with pytest.raises(ValueError):
                await driver.set_device_setting("nonsense", 1)
        finally:
            await _close(driver)

    asyncio.run(go())


# ── Connection-fault: a 401 is the typed auth_failed fault ──────────────────

def test_reboot_401_raises_auth_error():
    """A 401 on reboot (Basic auth required on the S-SKUs) raises the typed
    auth_failed fault, not a silent no-op."""
    async def go():
        sim = SIM.AverPtzSimulator("sim1", {})
        driver = _make_driver(sim, reject_paths=("sys_reboot",))
        try:
            with pytest.raises(ConnectionFaultError) as ei:
                await driver.send_command("reboot")
            assert ei.value.fault_code == "auth_failed"
            # No username was configured, so the camera asked for one.
            assert "needs a username and password" in str(ei.value)
        finally:
            await _close(driver)

    asyncio.run(go())


def test_factory_reset_401_raises_auth_error():
    async def go():
        sim = SIM.AverPtzSimulator("sim1", {})
        driver = _make_driver(sim, reject_paths=("set_factory_default",))
        try:
            with pytest.raises(ConnectionFaultError) as ei:
                await driver.send_command("factory_reset")
            assert ei.value.fault_code == "auth_failed"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_a_password_changed_on_the_camera_ends_the_poll_at_the_first_refusal():
    # Basic credentials go with every CGI request once a username is set. The
    # password is changed on the camera while connected: the poll's
    # get_sys_stat is refused, and the typed fault leaves poll() so the
    # platform drops the connection there instead of after three polls.
    async def go():
        sim = SIM.AverPtzSimulator("sim1", {})
        driver = _make_driver(sim, username="admin", password="secret")
        try:
            await driver.poll()
            assert driver.get_state("model_name") == "PTZ330"

            refused = []
            inner = sim.handle_request

            def counting(method, path, headers, body):
                status, resp = inner(method, path, headers, body)
                if status in (401, 403):
                    refused.append(status)
                return status, resp

            sim.handle_request = counting
            sim.inject_error("wrong_password")
            with pytest.raises(ConnectionFaultError) as ei:
                await driver.poll()
            assert ei.value.fault_code == "auth_failed"
            assert "refused the username and password" in str(ei.value)
            assert refused == [401]
        finally:
            await _close(driver)

    asyncio.run(go())


def test_connect_with_a_refused_password_is_auth_failed():
    # The connect probe (get_sys_stat) refused: the typed fault, not the
    # untyped "HTTP CGI probe failed".
    async def go():
        sim = SIM.AverPtzSimulator("sim1", {})
        sim.inject_error("wrong_password")
        driver = _make_driver(sim, username="admin", password="secret")
        try:
            with pytest.raises(ConnectionFaultError) as ei:
                await driver._post_connect()
            assert ei.value.fault_code == "auth_failed"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_a_403_keeps_its_untyped_error():
    # The CGI document names Basic authentication, whose refusal is a 401;
    # it says nothing about a 403, so a 403 is not taken for a refused
    # password and keeps the untyped error it had.
    async def go():
        sim = SIM.AverPtzSimulator("sim1", {})
        driver = _make_driver(sim, reject_paths=("get_sys_stat",),
                              reject_status=403)
        try:
            with pytest.raises(ConnectionError) as ei:
                await driver.poll()
            assert not isinstance(ei.value, ConnectionFaultError)
        finally:
            await _close(driver)

    asyncio.run(go())


def test_non_auth_command_succeeds():
    """A normal CGI command without auth still works (no false 401)."""
    async def go():
        sim = SIM.AverPtzSimulator("sim1", {})
        driver = _make_driver(sim)
        try:
            await driver.send_command("set_saturation", {"value": 7})
            await driver.poll()
            assert driver.get_state("saturation") == 7
        finally:
            await _close(driver)

    asyncio.run(go())
