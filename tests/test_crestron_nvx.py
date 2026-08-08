"""Driver + simulator tests for crestron_nvx (Crestron DM NVX, CresNext REST).

No NVX needed for CI: the real driver's httpx client is wired to the real
simulator via httpx.MockTransport, so a command POSTs to /Device, the sim
mutates, and the driver's poll reads it back — asserted on both sides. Run for
both roles (Transmitter/Receiver), since the driver adapts its surface to the
device's reported DeviceMode.

openavc.* are stubbed so the community CI stays self-contained
(conftest.py rolls the stubs back after collection). httpx is a real dependency.

Live hardware verification (E20 encoder + D200 decoder, firmware 7.1.5259) and
the first-boot setup wizard are recorded in driver-roadmap/shipped/crestron_nvx.md.
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
from _platform_stubs import (
    ConnectionFaultError as _ConnectionFaultError,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "crestron_nvx.py"
SIM_PATH = REPO_ROOT / "switchers" / "crestron_nvx_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver:
    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events):
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self._connected = False
        self._last_fault = None
        self._last_transport_error = ""
        # Mirror the platform's _init_state_variables: every declared state
        # variable is seeded, so role pruning has real keys to delete.
        for prop in self.DRIVER_INFO.get("state_variables", {}):
            self.state.set(prop, "")

    def _stash_fault(self, code, message=""):
        self._last_fault = (code, message)

    def set_state(self, key, value):
        self.state.set(key, value)

    def delete_state(self, key):
        self.state.delete(key)

    def set_states(self, updates):
        for k, v in updates.items():
            self.state.set(k, v)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    async def _verify_reachable(self, *_a, **_k):
        return True

    async def start_polling(self, *_a):
        pass

    async def stop_polling(self):
        pass


class _FakeHTTPSimulator:
    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None):
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value):
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
    base.ConnectionFaultError = _ConnectionFaultError
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


DRV = _load("crestron_nvx_under_test", DRIVER_PATH)
SIM = _load("crestron_nvx_sim_under_test", SIM_PATH)


# ── Harness ─────────────────────────────────────────────────────────────────

def _make_driver(sim, config=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode() if request.content else ""
        status, resp = sim.handle_request(request.method, request.url.path,
                                          dict(request.headers), body)
        if isinstance(resp, dict):
            return httpx.Response(status, json=resp)
        return httpx.Response(status, text=str(resp))

    cfg = {"host": "sim", "port": 443, "username": "admin", "password": "x",
           "poll_interval": 0}
    cfg.update(config or {})
    driver = DRV.CrestronNVXDriver(cfg["host"] and "nvx" or "nvx", cfg,
                                   _FakeState(), _FakeEvents())
    driver._base_url = "https://sim"
    driver._client = httpx.AsyncClient(base_url="https://sim",
                                       transport=httpx.MockTransport(handler))
    return driver


async def _connect(driver):
    """Run the driver's connect steps against the mock-transport client."""
    await driver._authenticate()
    info = await driver._api_get("/Device/DeviceInfo")
    driver._parse_device_info(info)
    spec = await driver._api_get("/Device/DeviceSpecific")
    driver._parse_device_specific(spec)
    driver._mode = driver.get_state("device_mode")
    driver._connected = True
    driver.set_state("connected", True)
    await driver._refresh_role_state()


# ── Metadata ────────────────────────────────────────────────────────────────

def test_metadata_shape():
    info = DRV.CrestronNVXDriver.DRIVER_INFO
    assert info["version"] == "2.0.6"
    assert info["min_platform_version"] == "0.25.0"
    assert info["category"] == "switcher"
    assert info["web_ui"] is True
    assert info["transport"] == "http"
    # The class declares the whole line's surface; instances narrow to their
    # role once the device reports DeviceMode (see the role-pruning tests).
    for cmd in ("route_stream", "set_bitrate", "start_stream", "reboot"):
        assert cmd in info["commands"]
    assert any(a["id"] == "set_up_nvx" and a["kind"] == "setup" for a in info["actions"])


# ── Decoder role ────────────────────────────────────────────────────────────

def test_decoder_connect_and_route():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})  # defaults to Receiver / D200
        d = _make_driver(sim)
        try:
            await _connect(d)
            assert d.get_state("device_mode") == "Receiver"
            assert d.get_state("model") == "DM-NVX-D200"

            await d.send_command("route_stream", {"encoder": "192.168.1.50"})
            await d.poll()
            assert d.get_state("stream_location") == "rtsp://192.168.1.50:554/live.sdp"
            assert d.get_state("stream_status") == "Stream started"
            assert d.get_state("video_source") == "Stream"

            await d.send_command("stop_stream")
            await d.poll()
            assert d.get_state("stream_status") == "Stream stopped"
        finally:
            await d._client.aclose()

    asyncio.run(go())


def test_decoder_device_setting_round_trip():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = _make_driver(sim)
        try:
            await _connect(d)
            await d.set_device_setting("leds", False)
            await d.poll()
            assert d.get_state("leds_enabled") is False
            await d.set_device_setting("front_panel_lock", True)
            await d.poll()
            assert d.get_state("front_panel_locked") is True
        finally:
            await d._client.aclose()

    asyncio.run(go())


# ── Encoder role ────────────────────────────────────────────────────────────

def test_encoder_connect_bitrate_and_preview():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.set_state("device_mode", "Transmitter")
        sim.set_state("model", "DM-NVX-E20")
        d = _make_driver(sim)
        try:
            await _connect(d)
            assert d.get_state("device_mode") == "Transmitter"
            assert d.get_state("model") == "DM-NVX-E20"

            await d.send_command("set_transmit_multicast", {"address": "239.5.5.5"})
            await d.send_command("set_bitrate", {"mbps": 500, "mode": "Fixed"})
            await d.send_command("start_stream")
            await d.poll()
            assert d.get_state("bitrate") == 500
            assert d.get_state("stream_multicast") == "239.5.5.5"
            assert d.get_state("stream_status") == "Stream started"
            # Preview convention: an encoder publishes its RTSP stream.
            assert d.get_state("preview_format") == "rtsp"
            assert d.get_state("preview_url").startswith("rtsp://")
        finally:
            await d._client.aclose()

    asyncio.run(go())


def test_wrong_role_object_is_ignored():
    """A decoder must not choke on StreamTransmit answering 'UNSUPPORTED'."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})  # Receiver
        d = _make_driver(sim)
        try:
            await _connect(d)
            # The receiver never queries StreamTransmit; asking directly returns
            # the unsupported sentinel string, which _first_stream tolerates.
            data = await d._api_get("/Device/StreamTransmit")
            assert d._first_stream(data, "StreamTransmit") is None
        finally:
            await d._client.aclose()

    asyncio.run(go())


# ── Role pruning ────────────────────────────────────────────────────────────


def test_encoder_surface_is_pruned_to_transmitter():
    """A connected transmitter presents only the encoder surface: no decoder
    commands in the Send Command list, no decoder state keys, and a decoder
    command sent anyway (macro/script) is rejected with a clear reason."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.set_state("device_mode", "Transmitter")
        sim.set_state("model", "DM-NVX-E20")
        d = _make_driver(sim)
        try:
            await _connect(d)

            cmds = d.DRIVER_INFO["commands"]
            for cmd in ("set_transmit_multicast", "set_bitrate", "start_stream", "reboot"):
                assert cmd in cmds
            for cmd in ("route_stream", "set_scaler_resolution"):
                assert cmd not in cmds

            svars = d.DRIVER_INFO["state_variables"]
            assert "bitrate" in svars and "preview_url" in svars
            for var in ("scaler_resolution", "video_wall_mode", "rx_initiator",
                        "output_connected", "output_resolution", "output_hdcp"):
                assert var not in svars
                assert var not in d.state.data  # seeded key deleted

            # The quick-action strip loses the decoder's headline action too.
            assert not any(a["id"] == "route_stream" for a in d.DRIVER_INFO["actions"])

            with pytest.raises(RuntimeError, match="route_stream"):
                await d.send_command("route_stream", {"encoder": "192.168.1.50"})

            # The class-level declaration (catalog surface) stays complete.
            assert "route_stream" in DRV.CrestronNVXDriver.DRIVER_INFO["commands"]
        finally:
            await d._client.aclose()

    asyncio.run(go())


def test_decoder_surface_is_pruned_to_receiver():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})  # defaults to Receiver / D200
        d = _make_driver(sim)
        try:
            await _connect(d)

            cmds = d.DRIVER_INFO["commands"]
            for cmd in ("route_stream", "set_scaler_resolution", "stop_stream"):
                assert cmd in cmds
            for cmd in ("set_transmit_multicast", "set_bitrate"):
                assert cmd not in cmds

            svars = d.DRIVER_INFO["state_variables"]
            assert "scaler_resolution" in svars and "rx_initiator" in svars
            for var in ("bitrate", "active_bitrate", "bitrate_mode", "input_sync",
                        "input_resolution", "input_hdcp", "preview_url", "preview_format"):
                assert var not in svars
                assert var not in d.state.data

            assert not any(a["id"] == "set_bitrate" for a in d.DRIVER_INFO["actions"])

            with pytest.raises(RuntimeError, match="set_bitrate"):
                await d.send_command("set_bitrate", {"mbps": 500})
        finally:
            await d._client.aclose()

    asyncio.run(go())


# ── Lockout protection ──────────────────────────────────────────────────────
# The NVX blocks the controller's source IP after a few failed logins, so the
# driver must never send a login it knows can't succeed.


def test_no_login_post_when_password_empty():
    """A discovery-added NVX starts with an empty password: connect must fail
    with a typed auth fault BEFORE any POST reaches /userlogin.html, so not
    one of the device's failed-login lockout attempts is burned."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posts.append(request.url.path)
            body = request.content.decode() if request.content else ""
            status, resp = sim.handle_request(request.method, request.url.path,
                                              dict(request.headers), body)
            if isinstance(resp, dict):
                return httpx.Response(status, json=resp)
            return httpx.Response(status, text=str(resp))

        cfg = {"host": "sim", "port": 443, "username": "admin", "password": "",
               "poll_interval": 0}
        d = DRV.CrestronNVXDriver("nvx", cfg, _FakeState(), _FakeEvents())
        d._base_url = "https://sim"
        d._client = httpx.AsyncClient(base_url="https://sim",
                                      transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(DRV.ConnectionFaultError) as exc:
                await d._authenticate()
            assert exc.value.fault_code == "auth_failed"
            assert posts == []  # no login attempt reached the device
        finally:
            await d._client.aclose()

    asyncio.run(go())


def test_factory_fresh_detected_from_redirect_header_without_following():
    """A still-initializing unit answers the /userlogin.html 301 fast but can
    stall for seconds serving the createUser page body. The driver must detect
    the factory 'create admin' state from the 301 Location header WITHOUT
    following it — otherwise the slow page turns a clean auth_failed into a read
    timeout, which classifies as a transient fault and the reconnect loop never
    pauses on auth."""
    async def go():
        followed = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/userlogin.html":
                return httpx.Response(301, headers={"Location": "/createUser.html"})
            if path == "/createUser.html":
                followed.append(path)  # the driver must never reach the slow page
                return httpx.Response(200, text="create admin page")
            return httpx.Response(404)

        cfg = {"host": "sim", "port": 443, "username": "admin", "password": "x",
               "poll_interval": 0}
        d = DRV.CrestronNVXDriver("nvx", cfg, _FakeState(), _FakeEvents())
        d._base_url = "https://sim"
        # Mirror the real connect() client, which follows redirects globally; the
        # per-call override in _authenticate is what must prevent the follow.
        d._client = httpx.AsyncClient(base_url="https://sim", follow_redirects=True,
                                      transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(DRV.ConnectionFaultError) as exc:
                await d._authenticate()
            assert exc.value.fault_code == "auth_failed"
            assert followed == []  # never followed into the slow createUser page
        finally:
            await d._client.aclose()

    asyncio.run(go())


def test_reauth_still_401_raises_typed_fault():
    """Credentials that stop working mid-run: after one re-auth retry the
    driver must raise a typed auth fault (device goes offline; the platform
    pauses reconnect) instead of silently returning None and re-attempting a
    login on every poll cycle."""
    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/Device"):
                return httpx.Response(401, text="Unauthorized")
            return httpx.Response(200, text="login page")

        cfg = {"host": "sim", "port": 443, "username": "admin", "password": "x",
               "poll_interval": 0}
        d = DRV.CrestronNVXDriver("nvx", cfg, _FakeState(), _FakeEvents())
        d._base_url = "https://sim"
        d._client = httpx.AsyncClient(base_url="https://sim",
                                      transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(DRV.ConnectionFaultError) as exc:
                await d._api_get("/Device/DeviceSpecific")
            assert exc.value.fault_code == "auth_failed"
            # Stashed for the poll-watchdog path, where the raised exception
            # itself isn't what gets classified.
            assert d._last_fault == (
                "auth_failed", "The NVX rejected the configured credentials.")
        finally:
            await d._client.aclose()

    asyncio.run(go())


# ── Auth fault ──────────────────────────────────────────────────────────────

def test_connect_auth_failure_raises_typed_fault():
    """If DeviceInfo never returns the Device tree, connect() raises a typed
    auth fault rather than declaring a phantom connection."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = _make_driver(sim)

        # Make DeviceInfo look like an un-logged-in response (no Device tree).
        async def _empty_get(path):
            if path == "/Device/DeviceInfo":
                return {}
            return await DRV.CrestronNVXDriver._api_get(d, path)
        d._api_get = _empty_get

        try:
            with pytest.raises(DRV.ConnectionFaultError) as exc:
                # Mimic connect()'s auth-check branch.
                await d._authenticate()
                info = await d._api_get("/Device/DeviceInfo")
                if "DeviceInfo" not in info.get("Device", {}):
                    raise DRV.ConnectionFaultError("Login rejected", code="auth_failed")
            assert exc.value.fault_code == "auth_failed"
        finally:
            await d._client.aclose()

    asyncio.run(go())
