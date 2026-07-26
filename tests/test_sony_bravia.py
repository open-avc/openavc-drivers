"""Driver + simulator tests for sony_bravia (Bravia JSON-RPC REST + IRCC).

No Bravia on hand, so correctness is a dual-proof round trip: the real driver's
HTTP layer (a faithful ``HTTPClientTransport`` stub backed by
``httpx.MockTransport``) is wired to the real simulator's synchronous
``handle_request``. The driver fires JSON-RPC setters, the sim mutates, the
driver's poll parses the result, both sides asserted. The API shapes
(setPictureQualitySettings / getPictureQualitySettings on /sony/video,
set/getLEDIndicatorStatus on /sony/system, 403 on a wrong PSK) were confirmed
against the pybravia library and the Bravia REST API reference.

Covers:
  - the connection lifecycle: the driver supplies its PSK auth and transport
    shape through _transport_kwargs() and classifies a rejected key in
    _post_connect(); the fake BaseDriver below mirrors the platform's
    hook-driven connect (kwargs assembly, the reachability verify for HTTP
    transports, _post_connect with its failure teardown, the canonical
    connected declare, _initial_sync with its failure teardown, and
    _close_session on every teardown path);
  - device settings: LED indicator + picture mode / brightness / contrast /
    color / sharpness write and read back through the poll;
  - a wrong Pre-Shared Key raises an auth-worded ConnectionError on connect
    (classifier -> auth_failed) instead of silently connecting and then
    failing every poll;
  - the Test Pre-Shared Key setup wizard accepts / rejects a key out-of-band.

Loads the driver + sim with ``server.*`` / ``simulator.*`` imports stubbed so
the community CI stays self-contained (conftest.py rolls the stubs back after
collection). httpx is a real dependency.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from _lifecycle_fake import LifecycleFake

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "displays" / "sony_bravia.py"
SIM_PATH = REPO_ROOT / "displays" / "sony_bravia_sim.py"


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


class _FakeBaseDriver(LifecycleFake):
    """Mirrors BaseDriver's hook-driven connection lifecycle: clean-slate
    reset, _pre_connect, _create_transport (constructor kwargs pass through
    _transport_kwargs), the reachability verify for connectionless
    transports, _post_connect with its failure teardown (error stash +
    transport close + _close_session), the canonical connected declare,
    _initial_sync with its full teardown on failure, and _close_session on
    every teardown path."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._last_transport_error = ""
        self.close_session_calls = 0
        self.polling_started_with = None
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
        self._connected = False

    async def start_polling(self, interval) -> None:
        self.polling_started_with = interval

    async def request_config_update(self, delta) -> None:
        self.config_updates.append(delta)
        self.config.update(delta)

    async def request_reconnect(self) -> None:
        self.reconnects += 1

    def _stash_transport_error(self) -> None:
        transport = self.transport
        if transport is not None:
            err = getattr(transport, "last_error", "") or ""
            if err:
                self._last_transport_error = err

    # Hook defaults (the driver overrides the ones it needs).
    async def _pre_connect(self):
        return None

    async def _create_transport(self, transport_type):
        # Mirrors the platform's http branch: base_url from host/port/ssl
        # config, credentials from auth_type config, everything through
        # _transport_kwargs() just before construction. References the
        # module-level fake transport directly (a deferred import at
        # test-run time would miss the stubs).
        host = self.config.get("host", "")
        port = self.config.get("port")
        use_ssl = self.config.get("ssl", False)
        scheme = "https" if use_ssl else "http"
        if port is None:
            port = 443 if use_ssl else 80
        auth_type = self.config.get("auth_type", "none")
        credentials = {}
        if auth_type in ("basic", "digest"):
            credentials["username"] = self.config.get("username", "")
            credentials["password"] = self.config.get("password", "")
        elif auth_type == "bearer":
            credentials["token"] = self.config.get("token", "")
        elif auth_type == "api_key":
            credentials["header"] = self.config.get("api_key_header", "X-API-Key")
            credentials["key"] = self.config.get("api_key", "")
        kwargs = dict(
            base_url=f"{scheme}://{host}:{port}",
            auth_type=auth_type,
            credentials=credentials,
            verify_ssl=self.config.get("verify_ssl", True),
            default_headers=self.config.get("default_headers", {}),
            timeout=self.config.get("timeout", 10.0),
            name=self.device_id,
        )
        self.transport = _FakeHTTPClientTransport(
            **self._transport_kwargs(transport_type, kwargs)
        )
        await self.transport.open()

    async def _post_connect(self):
        return None

    async def _initial_sync(self):
        return None

    async def _close_session(self):
        self.close_session_calls += 1

    def _link_alive(self):
        return self.transport is not None

    async def _start_push(self):
        return None

    async def _stop_push(self):
        return None

    async def connect(self):
        # Mirrors BaseDriver.connect()'s stages.
        self._last_transport_error = ""
        await self._stop_push()
        await self._close_session()
        if self.transport:
            await self.transport.close()
            self.transport = None
        transport_type = self.config.get("transport") or self.DRIVER_INFO.get(
            "transport", "tcp"
        )
        await self._pre_connect()
        await self._create_transport(transport_type)
        verify_timeout = self.config.get("verify_timeout", 3.0)
        if verify_timeout > 0 and hasattr(self.transport, "verify"):
            if not await self.transport.verify(timeout=verify_timeout):
                self._stash_transport_error()
                if self.transport:
                    await self.transport.close()
                    self.transport = None
                raise ConnectionError(
                    f"Device at {self.config.get('host', '?')}:"
                    f"{self.config.get('port', '?')} is not responding"
                )
        try:
            await self._post_connect()
            self._connected = True
            self.set_state("connected", True)
            await self.events.emit(f"device.connected.{self.device_id}")
        except Exception:
            self._stash_transport_error()
            if self.transport:
                await self.transport.close()
                self.transport = None
            await self._close_session()
            self._connected = False
            raise
        await self._start_push()
        try:
            await self._initial_sync()
        except Exception:
            self._stash_transport_error()
            await self._stop_push()
            transport = self.transport
            self.transport = None
            if transport is not None:
                try:
                    await transport.close()
                except Exception:  # noqa: BLE001
                    pass
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise
        poll_interval = self.config.get("poll_interval", 0)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self):
        await self._stop_push()
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


class _FakeHTTPSimulator:
    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

    def get_state(self, key, default=None):
        return self.state.get(key, default)

    def set_state(self, key, value) -> None:
        self.state[key] = value

    def log_protocol(self, *a, **k) -> None:
        pass


# ── Faithful HTTPClientTransport stub (real httpx via MockTransport) ─────────

class _FakeHTTPClientTransport:
    """Mirrors server.transport.http_client.HTTPClientTransport: api_key auth
    puts a header on every request, get/post/request return ok/status_code/
    text/json_data, verify() HEADs "/" and reports False on any exception,
    and a ConnectError surfaces as a ConnectionError. Records its constructor
    kwargs so tests can assert what the driver asked the platform to build."""

    device = None  # the simulator every request routes to (set per test)

    def __init__(
        self,
        base_url,
        auth_type="none",
        credentials=None,
        verify_ssl=True,
        timeout=10.0,
        name="",
        **_ignored,
    ) -> None:
        self.base_url = base_url
        self.auth_type = auth_type
        self.credentials = credentials or {}
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.name = name
        self.verify_calls = 0
        self._last_error = ""
        headers = {}
        if auth_type == "api_key" and credentials:
            headers[credentials.get("header", "X-API-Key")] = credentials.get(
                "key", ""
            )
        sim = type(self).device
        assert sim is not None, "test must set _FakeHTTPClientTransport.device"
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            transport=httpx.MockTransport(_make_handler(sim)),
        )

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        await self._client.aclose()
        self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def last_error(self) -> str:
        return self._last_error

    async def verify(self, timeout=5.0) -> bool:
        self.verify_calls += 1
        try:
            await self._client.head("/", timeout=timeout)
            return True
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e) or type(e).__name__
            return False

    async def get(self, path, params=None):
        return await self.request("GET", path)

    async def post(self, path, body=None, form_data=None):
        return await self.request("POST", path, json_body=body)

    async def request(self, method, path, json_body=None, content=None, headers=None):
        try:
            resp = await self._client.request(
                method, path, json=json_body, content=content, headers=headers
            )
        except httpx.ConnectError as e:
            self._last_error = str(e) or type(e).__name__
            raise ConnectionError(
                f"Failed to connect to {self.base_url}{path}: {e}"
            ) from e
        ctype = resp.headers.get("content-type", "")
        json_data = None
        if "json" in ctype:
            try:
                json_data = resp.json()
            except Exception:  # noqa: BLE001
                pass
        return SimpleNamespace(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            text=resp.text,
            json_data=json_data,
            ok=resp.is_success,
        )


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


# ── Loaders ─────────────────────────────────────────────────────────────────

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
    http_client = ModuleType("server.transport.http_client")
    http_client.HTTPClientTransport = _FakeHTTPClientTransport
    sys.modules["server.transport.http_client"] = http_client
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


DRV = _load("sony_bravia_under_test", DRIVER_PATH)
SIM = _load("sony_bravia_sim_under_test", SIM_PATH)


def _make_sim(psk="secret", power="active"):
    sim = SIM.SonyBraviaSimulator("sim1", {"psk": psk})
    sim.set_state("power", power)
    return sim


def _make_driver(sim, psk="secret"):
    _FakeHTTPClientTransport.device = sim
    config = {
        "host": "test",
        "port": 80,
        "psk": psk,
        "poll_interval": 0,
    }
    return DRV.SonyBraviaDriver("tv1", config, _FakeState(), _FakeEvents())


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.SonyBraviaDriver.DRIVER_INFO["version"] == "1.5.1"
    assert DRV.SonyBraviaDriver.DRIVER_INFO["min_platform_version"] == "0.24.0"


def test_device_settings_declared_and_backed():
    info = DRV.SonyBraviaDriver.DRIVER_INFO
    ds = info["device_settings"]
    expected = {
        "led_indicator", "picture_mode", "brightness", "contrast",
        "color", "sharpness",
    }
    assert set(ds) == expected
    state_vars = info["state_variables"]
    cmds = set(info["commands"])
    for key, defn in ds.items():
        assert defn["state_key"] in state_vars, key
        # Every DS routes to a real command.
        command, _ = DRV.SonyBraviaDriver._DS_COMMANDS[key]
        assert command in cmds, command


def test_actions_reference_real_commands():
    info = DRV.SonyBraviaDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for action in info["actions"]:
        if action.get("kind") == "setup":
            continue
        cmd = action.get("command", action["id"])
        assert cmd in cmds, f"action {action['id']} -> {cmd} is not a command"


# ── Connect + poll (LED unconditional, picture when on) ─────────────────────

def test_connect_builds_psk_transport_and_caches_model():
    async def go():
        sim = _make_sim()
        driver = _make_driver(sim)
        await driver.connect()
        try:
            # Transport shape: plain HTTP, PSK in the X-Auth-PSK header.
            assert driver.transport.base_url == "http://test:80"
            assert driver.transport.auth_type == "api_key"
            assert driver.transport.credentials == {
                "header": "X-Auth-PSK",
                "key": "secret",
            }
            assert driver.transport.verify_ssl is False
            # The reachability verify ran before the device went connected.
            assert driver.transport.verify_calls == 1
            # The PSK probe cached the model before the connected declare.
            assert driver.get_state("model") == "XBR-65X950G-SIM"
            assert driver._connected is True
            assert driver.get_state("connected") is True
            assert "device.connected.tv1" in driver.events.emitted
        finally:
            await driver.disconnect()
        assert driver.transport is None
        assert driver.get_state("connected") is False
        assert "device.disconnected.tv1" in driver.events.emitted

    asyncio.run(go())


def test_poll_reads_led_and_picture():
    async def go():
        sim = _make_sim(power="active")
        sim.set_state("led_indicator", "AutoBrightnessAdjust")
        sim.set_state("brightness", 42)
        sim.set_state("contrast", 77)
        sim.set_state("color", 33)
        sim.set_state("sharpness", 12)
        sim.set_state("picture_mode", "Cinema")
        driver = _make_driver(sim)
        await driver.connect()
        try:
            await driver.poll()
            assert driver.get_state("power") == "on"
            assert driver.get_state("led_indicator") == "AutoBrightnessAdjust"
            assert driver.get_state("brightness") == 42
            assert driver.get_state("contrast") == 77
            assert driver.get_state("color") == 33
            assert driver.get_state("sharpness") == 12
            assert driver.get_state("picture_mode") == "Cinema"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_poll_reads_led_when_off_but_skips_picture():
    async def go():
        sim = _make_sim(power="off")
        sim.set_state("led_indicator", "Off")
        driver = _make_driver(sim)
        await driver.connect()
        try:
            await driver.poll()
            assert driver.get_state("power") == "off"
            # LED is a system setting, readable in standby.
            assert driver.get_state("led_indicator") == "Off"
            # Picture settings are not read when the panel is off.
            assert driver.get_state("brightness") is None
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Device settings: write + read-back ──────────────────────────────────────

_DS_CASES = [
    ("led_indicator", "SimpleResponse", "SimpleResponse"),
    ("picture_mode", "Vivid", "Vivid"),
    ("brightness", 70, 70),
    ("contrast", 55, 55),
    ("color", 60, 60),
    ("sharpness", 25, 25),
]


@pytest.mark.parametrize("key,value,expected", _DS_CASES)
def test_device_setting_round_trip(key, value, expected):
    async def go():
        sim = _make_sim(power="active")
        driver = _make_driver(sim)
        await driver.connect()
        try:
            result = await driver.set_device_setting(key, value)
            assert result == expected
            assert driver.get_state(key) == expected
            # The value actually reached the simulated device.
            assert sim.get_state(key) == expected
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_unknown_device_setting_raises():
    async def go():
        sim = _make_sim(power="active")
        driver = _make_driver(sim)
        await driver.connect()
        try:
            with pytest.raises(ValueError):
                await driver.set_device_setting("no_such_setting", 1)
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── CF fix: wrong PSK -> auth-worded ConnectionError ───────────────────────

def test_wrong_psk_raises_auth_worded_error():
    async def go():
        sim = _make_sim(psk="secret")
        driver = _make_driver(sim, psk="wrong")
        with pytest.raises(ConnectionError) as exc:
            await driver.connect()
        assert "authentication failed" in str(exc.value).lower()
        # The failed handshake tore the transport down before the device was
        # ever declared connected.
        assert driver.transport is None
        assert driver._connected is False
        assert driver.get_state("connected") is not True
        # _close_session ran for the clean-slate reset AND the teardown.
        assert driver.close_session_calls == 2

    asyncio.run(go())


def test_correct_psk_connects():
    async def go():
        sim = _make_sim(psk="secret")
        driver = _make_driver(sim, psk="secret")
        await driver.connect()
        try:
            assert driver._connected is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Setup wizard: Test Pre-Shared Key ──────────────────────────────────────

async def _noop_progress(step, pct=None):
    return None


def test_setup_wizard_accepts_and_saves():
    async def go():
        sim = _make_sim(psk="secret")
        driver = _make_driver(sim, psk="")
        result = await driver.run_setup_action(
            "test_psk", {"psk": "secret", "save": True}, _noop_progress
        )
        assert result["auth_ok"] is True
        assert result["saved"] is True
        assert driver.config_updates[-1]["psk"] == "secret"
        assert driver.reconnects == 1

    asyncio.run(go())


def test_setup_wizard_rejects_bad_psk():
    async def go():
        sim = _make_sim(psk="secret")
        driver = _make_driver(sim, psk="")
        with pytest.raises(ConnectionError):
            await driver.run_setup_action(
                "test_psk", {"psk": "nope", "save": True}, _noop_progress
            )
        assert driver.config_updates == []
        assert driver.reconnects == 0

    asyncio.run(go())


def test_setup_wizard_no_save():
    async def go():
        sim = _make_sim(psk="secret")
        driver = _make_driver(sim, psk="")
        result = await driver.run_setup_action(
            "test_psk", {"psk": "secret", "save": False}, _noop_progress
        )
        assert result["auth_ok"] is True
        assert result["saved"] is False
        assert driver.config_updates == []

    asyncio.run(go())
