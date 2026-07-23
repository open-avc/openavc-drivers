"""Driver tests for poly_studio (Poly Studio X / G7500 VideoOS REST).

No Poly bar on hand, so correctness is proven by wiring the real driver's HTTP
layer (a faithful ``HTTPClientTransport`` stub backed by ``httpx.MockTransport``)
to an in-test emulation of the documented VideoOS REST device — the driver logs
in over ``/rest/session``, replays the session cookie, polls, and the emulated
bar mutates / reports back. The emulation mirrors the response shapes in the
Poly VideoOS REST API Reference Guide (bare-boolean mute endpoints, bare-int
volume, ``/rest/system`` envelope with ``systemName``, 403 on a bad login).

Covers the v1.3.0 adoption + bug-fixes:
  - poll no longer swallows a transport failure -> an unreachable bar now
    propagates ConnectionError so the watchdog flips it offline (was: stayed
    online forever);
  - the ``system_name`` state var, declared but never populated, is now read
    from ``GET /rest/system``;
  - a rejected login raises an auth-worded ConnectionError (shared classifier
    -> auth_failed) instead of a generic "login failed";
  - the Test Admin Login setup wizard accepts / rejects credentials out-of-band.

Loads the driver with the ``server.*`` imports stubbed so the community CI stays
self-contained (conftest.py rolls the stubs back after collection). httpx is a
real dependency.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "video" / "poly_studio.py"


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
        self._connected = False

    async def start_polling(self, interval) -> None:
        pass

    async def stop_polling(self) -> None:
        pass

    async def request_config_update(self, delta) -> None:
        self.config_updates.append(delta)
        self.config.update(delta)

    async def request_reconnect(self) -> None:
        self.reconnects += 1

    # Hook defaults + the hook-driven connect lifecycle the platform runs
    # (the driver has no connect() of its own anymore).
    async def _pre_connect(self):
        return None

    def _transport_kwargs(self, transport_type, kwargs):
        return kwargs

    async def _create_transport(self, transport_type):
        # Mirrors the platform's http branch: assemble kwargs, pass them
        # through _transport_kwargs(), build the transport. References the
        # module-level fake transport directly (a deferred import at test-run
        # time would miss the stubs).
        kwargs = dict(
            base_url="",
            auth_type="none",
            credentials={},
            verify_ssl=True,
            timeout=10.0,
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
        return None

    def _link_alive(self):
        return self.transport is not None

    async def _start_push(self):
        return None

    async def _stop_push(self):
        return None

    async def connect(self):
        # Mirrors BaseDriver.connect()'s stages.
        await self._stop_push()
        await self._close_session()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._pre_connect()
        transport_type = self.config.get("transport") or self.DRIVER_INFO.get(
            "transport", "tcp"
        )
        await self._create_transport(transport_type)
        try:
            await self._post_connect()
            self._connected = True
            self.set_state("connected", True)
        except Exception:
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
            await self._stop_push()
            if self.transport:
                await self.transport.close()
                self.transport = None
            await self._close_session()
            self._connected = False
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


# ── Faithful HTTPClientTransport stub (real httpx via MockTransport) ─────────

class _FakeHTTPClientTransport:
    """Mirrors server.transport.http_client.HTTPClientTransport closely enough
    for the driver: get/post/delete/put return an object with
    ok/status_code/text/json_data, and a ConnectError surfaces as a
    ConnectionError (as the real transport wraps it)."""

    # The test sets this to the _PolyDevice every request should route to.
    device: "_PolyDevice | None" = None

    def __init__(
        self,
        base_url,
        auth_type="none",
        credentials=None,
        verify_ssl=True,
        timeout=8.0,
        name="",
        **_ignored,
    ) -> None:
        self.base_url = base_url
        self.name = name
        self._last_error = ""
        dev = type(self).device
        assert dev is not None, "test must set _FakeHTTPClientTransport.device"
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=httpx.MockTransport(dev.handler),
        )

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def last_error(self) -> str:
        return self._last_error

    async def get(self, path, params=None):
        return await self._request("GET", path)

    async def post(self, path, body=None, form_data=None):
        return await self._request("POST", path, body)

    async def put(self, path, body=None):
        return await self._request("PUT", path, body)

    async def delete(self, path):
        return await self._request("DELETE", path)

    async def _request(self, method, path, body=None):
        try:
            resp = await self._client.request(method, path, json=body)
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


# ── Emulated Poly VideoOS device ────────────────────────────────────────────

def _truthy(body: str) -> bool:
    return body.strip().lower() in ("true", "1")


class _PolyDevice:
    def __init__(self, username="admin", password="secret", reachable=True):
        self.username = username
        self.password = password
        self.reachable = reachable
        self.sessions: set[str] = set()
        self.audio_mute = False
        self.video_mute = False
        self.volume = 25
        self.system_name = "Boardroom X50"
        self.calls: list[dict] = []

    def _bool_resp(self, value: bool) -> httpx.Response:
        return httpx.Response(
            200,
            text="true" if value else "false",
            headers={"content-type": "application/json"},
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("Connection refused")
        method = request.method
        path = request.url.path
        body = request.content.decode() if request.content else ""

        # /rest/session — the only unauthenticated path.
        if path == "/rest/session":
            if method == "POST":
                try:
                    payload = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    payload = {}
                if (
                    str(payload.get("user", "")) != self.username
                    or str(payload.get("password", "")) != self.password
                ):
                    # 403 LOG-IN ATTEMPT FAILED.
                    return httpx.Response(
                        403, json={"success": False, "loginStatus": {}}
                    )
                sid = f"sess-{len(self.sessions) + 1}"
                self.sessions.add(sid)
                resp = httpx.Response(
                    200,
                    json={
                        "success": True,
                        "session": {"sessionId": sid, "isAuthenticated": True},
                    },
                )
                resp.headers["set-cookie"] = f"session={sid}; Path=/; HttpOnly"
                return resp
            if method == "DELETE":
                return httpx.Response(200, json={"success": True})

        # Auth gate — every other path needs the session cookie.
        cookie = request.headers.get("cookie", "")
        if not any(f"session={s}" in cookie for s in self.sessions):
            return httpx.Response(401, json={"error": "Not authenticated"})

        if path == "/rest/audio/muted":
            if method == "GET":
                return self._bool_resp(self.audio_mute)
            if method == "POST":
                self.audio_mute = _truthy(body)
                return httpx.Response(200)
        if path == "/rest/audio/volume":
            if method == "GET":
                return httpx.Response(200, json=self.volume)
            if method == "POST":
                try:
                    self.volume = max(0, min(50, int(json.loads(body or "0"))))
                except (ValueError, json.JSONDecodeError):
                    pass
                return httpx.Response(200)
        if path == "/rest/video/local/mute":
            if method == "GET":
                return self._bool_resp(self.video_mute)
            if method == "POST":
                self.video_mute = _truthy(body)
                return httpx.Response(200)
        if path == "/rest/conferences" and method == "GET":
            return httpx.Response(200, json=self.calls)
        if path.startswith("/rest/conferences/") and method == "DELETE":
            cid = path.rsplit("/", 1)[-1]
            self.calls = [c for c in self.calls if str(c.get("id")) != cid]
            return httpx.Response(200)
        if path == "/rest/system" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "systemName": self.system_name,
                    "model": "Studio X50",
                    "serialNumber": "TEST0001",
                    "softwareVersion": "3.7.0",
                    "state": "READY",
                    "uptime": 100,
                },
            )
        if path == "/rest/system/status" and method == "GET":
            return httpx.Response(
                200,
                json=[{"name": "system.status.ipnetwork", "stateList": ["up"]}],
            )
        if path == "/rest/system/reboot" and method == "POST":
            return httpx.Response(200)
        return httpx.Response(404, json={"error": f"not handled: {path}"})


# ── Driver loader (server.* stubbed) ────────────────────────────────────────

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

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("poly_studio_under_test", DRIVER_PATH)


def _make_driver(device: _PolyDevice, **cfg):
    _FakeHTTPClientTransport.device = device
    config = {
        "host": "test",
        "port": 443,
        "username": "admin",
        "password": "secret",
        "verify_ssl": False,
        "poll_interval": 0,
    }
    config.update(cfg)
    return DRV.PolyStudioDriver("bar1", config, _FakeState(), _FakeEvents())


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.PolyStudioDriver.DRIVER_INFO["version"] == "1.3.1"
    assert DRV.PolyStudioDriver.DRIVER_INFO["min_platform_version"] == "0.24.0"


def test_actions_reference_real_commands():
    info = DRV.PolyStudioDriver.DRIVER_INFO
    cmds = set(info["commands"])
    for action in info["actions"]:
        if action.get("kind") == "setup":
            continue  # setup wizards aren't backed by a command
        cmd = action.get("command", action["id"])
        assert cmd in cmds, f"action {action['id']} -> {cmd} is not a command"


def test_setup_action_declared():
    info = DRV.PolyStudioDriver.DRIVER_INFO
    setup = [a for a in info["actions"] if a.get("kind") == "setup"]
    assert len(setup) == 1
    assert setup[0]["id"] == "test_login"
    assert setup[0]["availability"] == "always"


def test_device_settings_deliberately_absent():
    # DS was RESEARCHed and skipped: the VideoOS REST API exposes no
    # writable-and-read-back device setting (systemName is read-only via
    # GET /rest/system; there is no documented config-write endpoint). Guard
    # the deliberate skip so a future edit doesn't half-add one.
    assert not DRV.PolyStudioDriver.DRIVER_INFO.get("device_settings")


# ── Connect + poll (incl. the system_name read-back fix) ────────────────────

def test_connect_populates_state_including_system_name():
    async def go():
        dev = _PolyDevice()
        dev.audio_mute = True
        dev.volume = 40
        dev.video_mute = True
        driver = _make_driver(dev)
        await driver.connect()
        try:
            assert driver.get_state("audio_mute") is True
            assert driver.get_state("volume") == 40
            assert driver.get_state("video_mute") is True
            # bug #10: system_name was declared but never polled before.
            assert driver.get_state("system_name") == "Boardroom X50"
            assert driver.get_state("network_status") == "up"
            assert driver.get_state("in_call") is False
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_poll_reflects_device_changes():
    async def go():
        dev = _PolyDevice()
        driver = _make_driver(dev)
        await driver.connect()
        try:
            dev.system_name = "Huddle Bar"
            dev.volume = 12
            dev.calls = [{"id": "0", "isActive": True}]
            await driver.poll()
            assert driver.get_state("system_name") == "Huddle Bar"
            assert driver.get_state("volume") == 12
            assert driver.get_state("in_call") is True
            assert driver.get_state("active_call_count") == 1
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── LV fix (§4.3): a dead link must propagate, not be swallowed ─────────────

def test_poll_propagates_connection_error():
    async def go():
        dev = _PolyDevice()
        driver = _make_driver(dev)
        await driver.connect()
        try:
            # The bar drops off the network mid-session.
            dev.reachable = False
            with pytest.raises(ConnectionError):
                await driver.poll()
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── CF fix: rejected login is auth-worded (classifier -> auth_failed) ───────

def test_bad_password_raises_auth_worded_error():
    async def go():
        dev = _PolyDevice(password="secret")
        driver = _make_driver(dev, password="wrong")
        with pytest.raises(ConnectionError) as exc:
            await driver.connect()
        # The shared classifier keys "authentication failed" -> auth_failed.
        assert "authentication failed" in str(exc.value).lower()

    asyncio.run(go())


def test_good_password_connects():
    async def go():
        dev = _PolyDevice(password="secret")
        driver = _make_driver(dev, password="secret")
        await driver.connect()
        try:
            assert driver._authed is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Hangup path ─────────────────────────────────────────────────────────────

def test_hangup_clears_active_calls():
    async def go():
        dev = _PolyDevice()
        dev.calls = [{"id": "42", "isActive": True}]
        driver = _make_driver(dev)
        await driver.connect()
        try:
            await driver.send_command("hangup")
            assert dev.calls == []
            assert driver.get_state("in_call") is False
            assert driver.get_state("active_call_count") == 0
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Setup wizard: Test Admin Login ─────────────────────────────────────────

async def _noop_progress(step, pct=None):
    return None


def test_setup_wizard_accepts_and_saves():
    async def go():
        dev = _PolyDevice(password="secret")
        # Device configured with a stale/blank password; the wizard tests a
        # freshly-typed one and saves it on success.
        driver = _make_driver(dev, password="")
        result = await driver.run_setup_action(
            "test_login",
            {"username": "admin", "password": "secret", "save": True},
            _noop_progress,
        )
        assert result["auth_ok"] is True
        assert result["saved"] is True
        # Credentials were persisted and a reconnect requested.
        assert driver.config_updates and driver.config_updates[-1]["password"] == "secret"
        assert driver.reconnects == 1

    asyncio.run(go())


def test_setup_wizard_rejects_bad_password():
    async def go():
        dev = _PolyDevice(password="secret")
        driver = _make_driver(dev, password="")
        with pytest.raises(ConnectionError):
            await driver.run_setup_action(
                "test_login",
                {"username": "admin", "password": "nope", "save": True},
                _noop_progress,
            )
        # Nothing persisted on failure.
        assert driver.config_updates == []
        assert driver.reconnects == 0

    asyncio.run(go())


def test_setup_wizard_no_save_leaves_config_untouched():
    async def go():
        dev = _PolyDevice(password="secret")
        driver = _make_driver(dev, password="")
        result = await driver.run_setup_action(
            "test_login",
            {"username": "admin", "password": "secret", "save": False},
            _noop_progress,
        )
        assert result["auth_ok"] is True
        assert result["saved"] is False
        assert driver.config_updates == []
        assert driver.reconnects == 0

    asyncio.run(go())
