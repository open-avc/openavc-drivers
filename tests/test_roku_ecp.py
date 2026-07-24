"""Unit tests for the roku_ecp driver.

Loads ``streaming/roku_ecp.py`` directly, stubbing the ``server.*`` imports it
needs (BaseDriver, get_logger) so the community repo's test suite stays
self-contained — the CI has no ``openavc`` install (conftest.py rolls the
stubs back after collection). httpx is a real dependency.

Two layers of coverage:
  - the module-level ECP XML parsers and the declared command surface, using
    samples synthesized from Roku's published External Control Protocol
    responses;
  - the connection lifecycle, by running the driver's hooks under a fake
    BaseDriver that mirrors the platform's hook-driven connect: kwargs
    assembly through _transport_kwargs(), the reachability verify for HTTP
    transports, _post_connect with its failure teardown, the canonical
    connected declare, _initial_sync with its failure teardown, and
    _close_session on every teardown path. The HTTP layer is a faithful
    ``HTTPClientTransport`` stub backed by ``httpx.MockTransport`` wired to an
    emulated ECP device.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "streaming" / "roku_ecp.py"


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

    async def stop_polling(self) -> None:
        pass

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

    def _transport_kwargs(self, transport_type, kwargs):
        return kwargs

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


# ── Faithful HTTPClientTransport stub (real httpx via MockTransport) ─────────

class _FakeHTTPClientTransport:
    """Mirrors server.transport.http_client.HTTPClientTransport closely enough
    for the driver: get/post/request return an object with ok/status_code/
    text/json_data, verify() HEADs "/" and reports False on any exception,
    and a ConnectError surfaces as a ConnectionError (as the real transport
    wraps it). Records its constructor kwargs so tests can assert what the
    driver asked the platform to build."""

    # The test sets this to the _RokuDevice every request should route to.
    device: "_RokuDevice | None" = None

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
        return await self.request("GET", path, params=params)

    async def post(self, path, body=None, form_data=None):
        return await self.request("POST", path, json_body=body)

    async def request(
        self, method, path, params=None, json_body=None, content=None,
        headers=None,
    ):
        try:
            resp = await self._client.request(
                method, path, params=params, json=json_body, content=content,
                headers=headers,
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


# ── XML samples (synthesized from Roku's published ECP responses) ───────────

# A representative /query/device-info body (Roku Ultra, fully on).
_DEVICE_INFO = """<?xml version="1.0" encoding="UTF-8" ?>
<device-info>
  <udn>015e5108-9000-1046-8035-b0a737964dfb</udn>
  <serial-number>1GU48T017973</serial-number>
  <device-id>1GU48T017973</device-id>
  <model-name>Roku Ultra</model-name>
  <model-number>4800X</model-number>
  <friendly-device-name>Roku Ultra Living Room</friendly-device-name>
  <user-device-name>Living Room</user-device-name>
  <software-version>13.0.0</software-version>
  <software-build>4209</software-build>
  <power-mode>PowerOn</power-mode>
  <network-type>ethernet</network-type>
  <supports-tv-power-control>false</supports-tv-power-control>
  <is-tv>false</is-tv>
  <is-stick>false</is-stick>
</device-info>
"""

# A Roku TV in networked standby.
_DEVICE_INFO_TV_STANDBY = """<device-info>
  <model-name>TCL Roku TV</model-name>
  <power-mode>Ready</power-mode>
  <supports-tv-power-control>true</supports-tv-power-control>
  <is-tv>true</is-tv>
</device-info>
"""


# ── Emulated Roku ECP device ────────────────────────────────────────────────

class _RokuDevice:
    def __init__(self, reachable=True, control_enabled=True):
        self.reachable = reachable
        # False = "Control by mobile apps" disabled: key presses return 403.
        self.control_enabled = control_enabled
        # True = answers the reachability HEAD but drops every query, the
        # shape of a device that dies right after the transport comes up.
        self.query_fail = False
        self.device_info_xml = _DEVICE_INFO
        self.active_app_xml = (
            '<active-app><app id="12" type="appl">Netflix</app></active-app>'
        )
        self.media_player_xml = '<player error="false" state="none"></player>'
        self.keypresses: list[str] = []

    @staticmethod
    def _xml(body: str) -> httpx.Response:
        return httpx.Response(
            200, text=body, headers={"content-type": "text/xml"}
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("Connection refused")
        method = request.method
        path = request.url.path
        if method == "HEAD":
            return httpx.Response(200)
        if self.query_fail:
            raise httpx.ConnectError("Connection reset")
        if method == "GET" and path == "/query/device-info":
            return self._xml(self.device_info_xml)
        if method == "GET" and path == "/query/active-app":
            return self._xml(self.active_app_xml)
        if method == "GET" and path == "/query/media-player":
            return self._xml(self.media_player_xml)
        if method == "GET" and path == "/query/tv-active-channel":
            return self._xml("<tv-channel><channel></channel></tv-channel>")
        if method == "POST" and path.startswith("/keypress/"):
            self.keypresses.append(path.split("/keypress/", 1)[1])
            return httpx.Response(200 if self.control_enabled else 403)
        if method == "POST" and path.split("/")[1] in (
            "keydown", "keyup", "launch", "install"
        ):
            return httpx.Response(200)
        return httpx.Response(404)


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


drv = _load("roku_ecp_under_test", DRIVER_PATH)
INFO = drv.RokuECPDriver.DRIVER_INFO

_SPECIAL_COMMANDS = {
    "launch_app", "install_app", "type_text", "keypress", "key_down", "key_up",
}


def _make_driver(device: _RokuDevice, **cfg):
    _FakeHTTPClientTransport.device = device
    config = {
        "host": "test",
        "port": 8060,
        "poll_interval": 5,
        "timeout": 10.0,
        "text_entry_delay": 0,
    }
    config.update(cfg)
    return drv.RokuECPDriver("rk1", config, _FakeState(), _FakeEvents())


# ── Metadata ────────────────────────────────────────────────────────────────

def test_version_and_platform_floor():
    assert INFO["version"] == "1.0.3"
    assert INFO["min_platform_version"] == "0.24.0"


# ── device-info parsing ──

def test_device_info_extracts_identity():
    info = drv._parse_device_info(_DEVICE_INFO)
    assert info["model_name"] == "Roku Ultra"
    assert info["serial_number"] == "1GU48T017973"
    assert info["software_version"] == "13.0.0"
    assert info["network_type"] == "ethernet"
    # user-device-name wins over friendly-device-name.
    assert info["device_name"] == "Living Room"


def test_device_info_power_and_booleans():
    info = drv._parse_device_info(_DEVICE_INFO)
    assert info["power_mode"] == "PowerOn"
    assert info["power"] == "on"
    assert info["is_tv"] is False
    assert info["supports_tv_power"] is False


def test_device_info_tv_standby():
    info = drv._parse_device_info(_DEVICE_INFO_TV_STANDBY)
    assert info["power"] == "standby"
    assert info["is_tv"] is True
    assert info["supports_tv_power"] is True
    # Absent elements are simply omitted, not defaulted.
    assert "serial_number" not in info


def test_power_mode_mapping():
    assert drv._POWER_MAP["PowerOn"] == "on"
    assert drv._POWER_MAP["Ready"] == "standby"
    assert drv._POWER_MAP["DisplayOff"] == "standby"
    assert drv._POWER_MAP["PowerOff"] == "off"
    # Unknown modes fall back to "off" in the parser.
    info = drv._parse_device_info("<device-info><power-mode>Surprise</power-mode></device-info>")
    assert info["power"] == "off"
    assert info["power_mode"] == "Surprise"


# ── active-app parsing ──

def test_active_app_running():
    xml = '<active-app><app id="12" type="appl" version="4.1.218">Netflix</app></active-app>'
    info = drv._parse_active_app(xml)
    assert info == {"active_app_id": "12", "active_app_name": "Netflix"}


def test_active_app_home_screen():
    # The home screen reports the app named "Roku" with no id.
    info = drv._parse_active_app("<active-app><app>Roku</app></active-app>")
    assert info == {"active_app_id": "", "active_app_name": "Home"}


def test_active_app_screensaver():
    xml = (
        "<active-app><app>Roku</app>"
        '<screensaver id="55545" type="ssvr" version="1.0">Default</screensaver>'
        "</active-app>"
    )
    info = drv._parse_active_app(xml)
    assert info["active_app_id"] == "55545"
    assert info["active_app_name"] == "Default"


# ── media-player parsing ──

def test_media_player_playing():
    xml = (
        '<player error="false" state="play">'
        "<position>6916 ms</position><duration>887999 ms</duration></player>"
    )
    info = drv._parse_media_player(xml)
    assert info["media_state"] == "play"
    assert info["media_position"] == 6916
    assert info["media_duration"] == 887999


def test_media_player_idle():
    info = drv._parse_media_player('<player error="false" state="none"></player>')
    assert info["media_state"] == "none"
    assert "media_position" not in info


def test_parsers_tolerate_garbage():
    assert drv._parse_device_info("not xml at all") == {}
    assert drv._parse_active_app("<broken") == {}
    assert drv._parse_media_player("") == {}
    assert drv._parse_tv_channel("nope") == {}


# ── tv-active-channel parsing ──

def test_tv_channel_active():
    xml = (
        "<tv-channel><channel>"
        "<number>14.3</number><name>getTV</name>"
        "<type>air-digital</type><program-title>Airwolf</program-title>"
        "</channel></tv-channel>"
    )
    info = drv._parse_tv_channel(xml)
    assert info["tv_channel"] == "14.3"
    assert info["tv_channel_name"] == "getTV"
    assert info["tv_program"] == "Airwolf"


def test_tv_channel_blank_when_not_tuned():
    info = drv._parse_tv_channel("<tv-channel><channel></channel></tv-channel>")
    assert info == {"tv_channel": "", "tv_channel_name": "", "tv_program": ""}


# ── command surface ──

def test_every_command_has_a_handler():
    for name in INFO["commands"]:
        assert name in drv._KEYPRESS or name in _SPECIAL_COMMANDS, (
            f"command {name!r} has no dispatch path"
        )


def test_keypress_table_matches_declared_commands():
    for name in drv._KEYPRESS_COMMANDS:
        assert name in INFO["commands"], f"{name} missing from commands"
        assert name in drv._KEYPRESS


def test_keypress_keys_are_nonempty_tokens():
    for name, key in drv._KEYPRESS.items():
        assert key and key.isalnum(), f"{name} maps to a bad ECP key {key!r}"


def test_every_command_declares_label_and_params():
    for name, spec in INFO["commands"].items():
        assert spec.get("label"), f"{name} missing label"
        assert "params" in spec, f"{name} missing params"


# ── discovery declaration sanity ──

def test_discovery_probe_matches_device_info():
    probe = INFO["discovery"]["tcp_probe"]
    assert probe["port"] == 8060
    # The declared substring matcher must hit a real device-info body.
    assert probe["expect"] in _DEVICE_INFO
    # And the model extract must pull a value.
    import re
    match = re.search(probe["extract"]["model"]["regex"], _DEVICE_INFO)
    assert match and match.group(1) == "Roku Ultra"


# ── Connection lifecycle ────────────────────────────────────────────────────

def test_connect_builds_plain_http_transport_and_seeds_state():
    async def go():
        dev = _RokuDevice()
        driver = _make_driver(dev)
        await driver.connect()
        try:
            # Transport shape: plain HTTP on 8060, no auth, no cert checks.
            assert driver.transport.base_url == "http://test:8060"
            assert driver.transport.auth_type == "none"
            assert driver.transport.credentials == {}
            assert driver.transport.verify_ssl is False
            assert driver.transport.timeout == 10.0
            # The reachability verify ran before the device went connected.
            assert driver.transport.verify_calls == 1
            assert driver.get_state("connected") is True
            assert "device.connected.rk1" in driver.events.emitted
            # Optimistic control seed + the initial device-info read.
            assert driver.get_state("control_enabled") is True
            assert driver.get_state("model_name") == "Roku Ultra"
            assert driver.get_state("power") == "on"
            assert driver.get_state("device_name") == "Living Room"
            # Polling came from config, started by the platform.
            assert driver.polling_started_with == 5
        finally:
            await driver.disconnect()
        assert driver.transport is None
        assert driver.get_state("connected") is False
        assert "device.disconnected.rk1" in driver.events.emitted

    asyncio.run(go())


def test_unreachable_roku_fails_connect():
    async def go():
        dev = _RokuDevice(reachable=False)
        driver = _make_driver(dev)
        with pytest.raises(ConnectionError) as exc:
            await driver.connect()
        assert "not responding" in str(exc.value)
        assert driver.transport is None
        assert driver.get_state("connected") is not True

    asyncio.run(go())


def test_device_info_failure_tears_connection_down():
    async def go():
        # The Roku answers the reachability check, then drops off the network
        # before the initial device-info read.
        dev = _RokuDevice()
        dev.query_fail = True
        driver = _make_driver(dev)
        with pytest.raises(ConnectionError):
            await driver.connect()
        assert driver.transport is None
        assert driver._connected is False
        assert driver.get_state("connected") is False
        assert "device.disconnected.rk1" in driver.events.emitted
        # _close_session ran for the clean-slate reset AND the teardown.
        assert driver.close_session_calls == 2

    asyncio.run(go())


def test_poll_updates_active_app_and_media_state():
    async def go():
        dev = _RokuDevice()
        driver = _make_driver(dev)
        await driver.connect()
        try:
            dev.active_app_xml = (
                '<active-app><app id="837" type="appl">YouTube</app></active-app>'
            )
            dev.media_player_xml = (
                '<player error="false" state="play">'
                "<position>1000 ms</position><duration>2000 ms</duration></player>"
            )
            await driver.poll()
            assert driver.get_state("active_app_id") == "837"
            assert driver.get_state("active_app_name") == "YouTube"
            assert driver.get_state("media_state") == "play"
            assert driver.get_state("media_position") == 1000
            assert driver.get_state("media_duration") == 2000
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_keypress_403_flips_control_enabled():
    async def go():
        dev = _RokuDevice(control_enabled=False)
        driver = _make_driver(dev)
        await driver.connect()
        try:
            # Optimistic seed, then the 403 proves control is blocked.
            assert driver.get_state("control_enabled") is True
            await driver.send_command("home")
            assert driver.get_state("control_enabled") is False
            # The user enables mobile control; the next press restores it.
            dev.control_enabled = True
            await driver.send_command("home")
            assert driver.get_state("control_enabled") is True
            assert dev.keypresses == ["Home", "Home"]
        finally:
            await driver.disconnect()

    asyncio.run(go())
