"""Unit tests for the hisense_vidaa driver.

Loads ``displays/hisense_vidaa.py`` directly, stubbing the ``server.*`` imports
it needs (BaseDriver, get_system_config, get_logger) so the community repo's
test suite stays self-contained — mirrors test_qsc_qrc.py.

The credential generator is checked against the worked example documented in the
reverse-engineered protocol analysis (a real test vector), so the trickiest
piece is verified without a TV. Topic construction, command payloads, and state
parsing are exercised against a fake transport; the connection lifecycle
(_pre_connect host check + store load, the _create_transport credential ladder,
_initial_sync subscribe/announce/refresh) runs through a stub BaseDriver that
mirrors the platform's hook-driven connect()/disconnect() order.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "displays" / "hisense_vidaa.py"


# ── Stub server.* so the driver imports without an openavc install ──

class _BaseDriver:
    """Functional stand-in for the platform BaseDriver: the driver supplies
    lifecycle hooks (_pre_connect / _create_transport / _initial_sync /
    _close_session) and connect()/disconnect() here run them in the platform's
    order — clean slate, _pre_connect, transport build, _post_connect (with
    failure teardown), declare, _initial_sync (with full teardown on failure),
    then polling. ``connected`` is the platform's _link_alive-backed property.
    (hisense_vidaa supplies no _liveness_probe, so the watchdog stage is
    omitted here.)"""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state=None, events=None):
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events or _Events()
        self.transport = None
        self._connected = False
        self._state: dict = {}

    def set_state(self, key, value):
        self._state[key] = value

    def get_state(self, key):
        return self._state.get(key)

    async def start_polling(self, interval):
        pass

    async def stop_polling(self):
        pass

    def _stash_transport_error(self):
        pass

    def _handle_transport_disconnect(self):
        self._connected = False
        self.set_state("connected", False)

    # -- lifecycle hooks (drivers override; defaults are no-ops) --

    async def _pre_connect(self):
        pass

    async def _post_connect(self):
        pass

    async def _initial_sync(self):
        pass

    async def _close_session(self):
        pass

    async def _stop_push(self):
        pass

    async def _create_transport(self, transport_type):
        # The platform ladder isn't modeled — the driver under test
        # overrides this wholesale (its credential-candidate retry loop).
        raise NotImplementedError

    def _link_alive(self):
        if self.transport is None:
            return False
        return bool(getattr(self.transport, "connected", False))

    @property
    def connected(self):
        return self._connected and self._link_alive()

    # -- connect/disconnect (mirror BaseDriver's stage order) --

    async def connect(self):
        # Clean slate: drop a previous attempt's push subscription, driver
        # session, and stale transport before the hooks run.
        await self._stop_push()
        await self._close_session()
        if self.transport:
            try:
                await self.transport.close()
            except Exception:
                pass
            self.transport = None

        await self._pre_connect()
        await self._create_transport(
            self.config.get("transport")
            or self.DRIVER_INFO.get("transport", "tcp")
        )
        # Transport verify stage skipped: hasattr(transport, "verify") is
        # False for the MQTT stub, same as the real MQTTTransport.

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
                except Exception:
                    pass
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise

        if self.config.get("poll_interval", 0) > 0:
            await self.start_polling(self.config["poll_interval"])

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


class FakeMQTTTransport:
    """Stand-in for server.transport.mqtt.MQTTTransport. The driver imports it
    with a deferred ``from server.transport.mqtt import MQTTTransport`` INSIDE
    _create_transport at test-run time, so the stub module must already be in
    sys.modules (installed by _install_server_stubs) — a module-level class
    referenced there directly, per the repo's stub convention."""

    created: list[dict] = []  # kwargs of every create() attempt
    reject = None  # callable(kwargs) -> True to refuse that candidate

    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.connected = True
        self.published: list[tuple] = []
        self.subscribed: list[str] = []
        self.last_error = ""

    @classmethod
    async def create(cls, host, port, **kwargs):
        kwargs = dict(kwargs, host=host, port=port)
        cls.created.append(kwargs)
        if cls.reject is not None and cls.reject(kwargs):
            raise ConnectionError("Connection Refused: not authorized")
        return cls(kwargs)

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))

    async def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    async def close(self):
        self.connected = False


def _install_server_stubs(tmp_dir: str) -> None:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server

    drivers = ModuleType("server.drivers")
    drivers.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server.drivers"] = drivers

    base = ModuleType("server.drivers.base")
    base.BaseDriver = _BaseDriver
    sys.modules["server.drivers.base"] = base

    transport_pkg = ModuleType("server.transport")
    transport_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server.transport"] = transport_pkg
    mqtt = ModuleType("server.transport.mqtt")
    mqtt.MQTTTransport = FakeMQTTTransport
    sys.modules["server.transport.mqtt"] = mqtt

    sysconfig = ModuleType("server.system_config")
    sysconfig.get_system_config = lambda: _SysConfig(tmp_dir)
    sys.modules["server.system_config"] = sysconfig

    utils = ModuleType("server.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server.utils"] = utils
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name: _Logger()
    sys.modules["server.utils.logger"] = logger


class _Events:
    def __init__(self):
        self.emitted: list[str] = []

    async def emit(self, name, *a, **k):
        self.emitted.append(name)


class _SysConfig:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class FakeTransport:
    def __init__(self):
        self.connected = True
        self.published: list[tuple] = []
        self.subscribed: list[str] = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))

    async def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    async def close(self):
        self.connected = False


@pytest.fixture(scope="module")
def mod(tmp_path_factory):
    tmp_dir = str(tmp_path_factory.mktemp("vidaa_data"))
    # These server.* stubs are installed at fixture (run) time, not module-import
    # time, so conftest.py's collection-time snapshot/rollback does NOT cover
    # them. Without restoring here they leak into later test modules that use the
    # REAL platform — e.g. test_netgear, whose BaseDriver.connect() then picks up
    # this fixture's stubbed server.system_config (a _SysConfig with no .get) and
    # fails. Snapshot the platform modules and roll them back on teardown.
    _platform = ("server", "simulator")
    _before = {
        n: m for n, m in sys.modules.items() if n.split(".", 1)[0] in _platform
    }
    _before_keys = set(sys.modules)
    _install_server_stubs(tmp_dir)
    spec = importlib.util.spec_from_file_location("hisense_vidaa", DRIVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for name in list(sys.modules):
        if name.split(".", 1)[0] in _platform and name not in _before_keys:
            del sys.modules[name]
    for name, m in _before.items():
        sys.modules[name] = m


@pytest.fixture(autouse=True)
def _reset_mqtt_stub():
    FakeMQTTTransport.created = []
    FakeMQTTTransport.reject = None
    yield
    FakeMQTTTransport.created = []
    FakeMQTTTransport.reject = None


def _make_driver(mod, **config):
    """Driver with a pre-injected fake transport, for command/parse tests
    that skip connect(). Lifecycle tests must NOT use this — connect()'s
    clean-slate stage would destroy the injected transport."""
    cfg = {"host": "10.0.0.5"}
    cfg.update(config)
    drv = mod.HisenseVidaaDriver("tv1", cfg, None, _Events())
    drv._client_id = "AA:BB:CC:DD:EE:FF$his$256DBF_vidaacommon_001"
    drv.transport = FakeTransport()
    return drv


# ── Connection lifecycle (hook-driven connect/disconnect) ──

def test_connect_runs_hook_lifecycle(mod):
    drv = mod.HisenseVidaaDriver("tv1", {"host": "10.0.0.5"}, None, _Events())
    asyncio.run(drv.connect())
    # _create_transport: first credential candidate accepted, TLS on 36669.
    assert isinstance(drv.transport, FakeMQTTTransport)
    assert len(FakeMQTTTransport.created) == 1
    assert FakeMQTTTransport.created[0]["port"] == 36669
    assert FakeMQTTTransport.created[0]["use_tls"] is True
    assert drv.get_state("auth_mode_active") == "dynamic"
    # _pre_connect: store loaded — a uuid was minted, feeding the client id.
    assert drv._uuid
    assert drv._client_id.startswith(f"{drv._uuid}$his$")
    assert drv._client_id.endswith("_vidaacommon_001")
    # Declared connected: flag/property, state key, canonical event.
    assert drv.connected is True
    assert drv.get_state("connected") is True
    assert "device.connected.tv1" in drv.events.emitted
    # _initial_sync: subscriptions, the pairing announce, the state refresh.
    assert drv._resp_topic("ui_service", "state") in drv.transport.subscribed
    topics = [t for t, _ in drv.transport.published]
    assert drv._cmd_topic("ui_service", "vidaa_app_connect") in topics
    assert drv._cmd_topic("ui_service", "gettvstate") in topics
    assert drv._cmd_topic("platform_service", "getvolume") in topics
    assert drv._cmd_topic("ui_service", "sourcelist") in topics


def test_connect_without_host_raises(mod):
    drv = mod.HisenseVidaaDriver("tv1", {"host": "   "}, None, _Events())
    with pytest.raises(ConnectionError):
        asyncio.run(drv.connect())
    # _pre_connect failed before any transport attempt.
    assert FakeMQTTTransport.created == []
    assert drv.transport is None
    assert drv.connected is False
    assert "device.connected.tv1" not in drv.events.emitted


def test_connect_falls_back_through_credential_candidates(mod):
    # Refuse everything except the static scheme: dynamic is tried first
    # and rejected, static (candidate #2) gets in.
    FakeMQTTTransport.reject = lambda kw: kw["username"] != "hisenseservice"
    drv = mod.HisenseVidaaDriver("tv1", {"host": "10.0.0.5"}, None, _Events())
    asyncio.run(drv.connect())
    assert len(FakeMQTTTransport.created) == 2
    assert FakeMQTTTransport.created[-1]["username"] == "hisenseservice"
    assert drv.get_state("auth_mode_active") == "static"
    assert drv.connected is True


def test_connect_all_candidates_refused_raises(mod):
    FakeMQTTTransport.reject = lambda kw: True
    drv = mod.HisenseVidaaDriver("tv1", {"host": "10.0.0.5"}, None, _Events())
    with pytest.raises(ConnectionError):
        asyncio.run(drv.connect())
    assert len(FakeMQTTTransport.created) == 3  # dynamic, static, legacy
    assert drv.transport is None
    assert drv.connected is False
    assert drv.get_state("connected") is not True
    assert "device.connected.tv1" not in drv.events.emitted


def test_disconnect_closes_transport_and_emits(mod):
    drv = mod.HisenseVidaaDriver("tv1", {"host": "10.0.0.5"}, None, _Events())
    asyncio.run(drv.connect())
    transport = drv.transport
    asyncio.run(drv.disconnect())
    assert transport.connected is False
    assert drv.transport is None
    assert drv.connected is False
    assert drv.get_state("connected") is False
    assert drv.events.emitted[-1] == "device.disconnected.tv1"


# ── Credential generator: documented test vector ──

def test_credential_vector(mod):
    """Matches the worked example in the protocol analysis exactly."""
    client_id, username, password = mod.generate_credentials(
        "56:b8:88:4e:f7:19", timestamp=1766974704, modern=True
    )
    assert client_id == "56:b8:88:4e:f7:19$his$256DBF_vidaacommon_001"
    assert username == "his$6239759786168176024"
    assert password == "C3BA44782E18ABF4892AC44D79A622D2"


def test_credentials_stable_per_uuid_and_time(mod):
    a = mod.generate_credentials("aa:bb:cc:dd:ee:ff", timestamp=1700000000)
    b = mod.generate_credentials("aa:bb:cc:dd:ee:ff", timestamp=1700000000)
    assert a == b
    c = mod.generate_credentials("aa:bb:cc:dd:ee:ff", timestamp=1700000001)
    assert c != a  # timestamp feeds username + password


def test_legacy_suffix_changes_password(mod):
    modern = mod.generate_credentials("aa:bb:cc:dd:ee:ff", timestamp=1700000000, modern=True)
    legacy = mod.generate_credentials("aa:bb:cc:dd:ee:ff", timestamp=1700000000, modern=False)
    assert modern[0] == legacy[0]  # client id is suffix-independent
    assert modern[2] != legacy[2]  # password differs by value suffix


# ── Topic construction ──

def test_topic_construction(mod):
    drv = _make_driver(mod)
    cid = drv._client_id
    assert drv._cmd_topic("remote_service", "sendkey") == \
        f"/remoteapp/tv/remote_service/{cid}/actions/sendkey"
    assert drv._resp_topic("ui_service", "state") == \
        f"/remoteapp/mobile/{cid}/ui_service/data/state"


# ── Commands publish the right topic + payload ──

def test_set_volume_publishes_plain_number(mod):
    drv = _make_driver(mod)
    asyncio.run(drv.send_command("set_volume", {"level": 142}))  # clamps to 100
    topic, payload = drv.transport.published[-1]
    assert topic.endswith("/platform_service/" + drv._client_id + "/actions/changevolume")
    assert payload == "100"


def test_set_source_uses_sourceid_payload(mod):
    drv = _make_driver(mod)
    asyncio.run(drv.send_command("set_source", {"source": "hdmi1"}))
    topic, payload = drv.transport.published[-1]
    assert topic.endswith("/ui_service/" + drv._client_id + "/actions/changesource")
    assert json.loads(payload) == {"sourceid": "1"}


def test_send_key_maps_friendly_name(mod):
    drv = _make_driver(mod)
    asyncio.run(drv.send_command("send_key", {"key": "ok"}))
    topic, payload = drv.transport.published[-1]
    assert topic.endswith("/remote_service/" + drv._client_id + "/actions/sendkey")
    assert payload == "KEY_OK"


def test_unknown_key_rejected(mod):
    drv = _make_driver(mod)
    with pytest.raises(ValueError):
        asyncio.run(drv.send_command("send_key", {"key": "nope"}))


def test_submit_pin_payload(mod):
    drv = _make_driver(mod)
    asyncio.run(drv.send_command("submit_pin", {"pin": "1234"}))
    topic, payload = drv.transport.published[-1]
    assert topic.endswith("/ui_service/" + drv._client_id + "/actions/authenticationcode")
    assert json.loads(payload) == {"authNum": "1234"}


def test_request_pairing_triggers_connect(mod):
    drv = _make_driver(mod)
    asyncio.run(drv.send_command("request_pairing"))
    topic, payload = drv.transport.published[-1]
    assert topic.endswith("/actions/vidaa_app_connect")
    assert json.loads(payload)["device_type"] == "Mobile App"
    assert drv.get_state("pin_pending") is True


def test_power_off_only_sends_when_on(mod):
    drv = _make_driver(mod)
    drv.set_state("power", False)
    asyncio.run(drv.send_command("power_off"))
    assert drv.transport.published == []  # already off, no key sent
    drv.set_state("power", True)
    asyncio.run(drv.send_command("power_off"))
    assert drv.transport.published[-1][1] == "KEY_POWER"


# ── Inbound message parsing ──

def test_handle_volume_message(mod):
    drv = _make_driver(mod)
    asyncio.run(drv.on_mqtt_message(
        "/remoteapp/mobile/broadcast/platform_service/actions/volumechange",
        b'{"volume_value": 37}'))
    assert drv.get_state("volume") == 37


def test_handle_state_power_and_source(mod):
    drv = _make_driver(mod)
    asyncio.run(drv.on_mqtt_message(
        drv._resp_topic("ui_service", "state"),
        b'{"statetype": "sourceswitch", "sourcename": "HDMI 1"}'))
    assert drv.get_state("power") is True
    assert drv.get_state("source") == "HDMI 1"
    asyncio.run(drv.on_mqtt_message(
        drv._resp_topic("ui_service", "state"),
        b'{"statetype": "fake_sleep_0"}'))
    assert drv.get_state("power") is False


def test_handle_sourcelist_populates_picker(mod):
    drv = _make_driver(mod)
    asyncio.run(drv.on_mqtt_message(
        drv._resp_topic("ui_service", "sourcelist"),
        b'[{"sourcename": "TV"}, {"sourcename": "HDMI 1"}]'))
    assert json.loads(drv.get_state("sources")) == ["TV", "HDMI 1"]


def test_tokenissuance_marks_paired(mod):
    drv = _make_driver(mod)
    drv._uuid = "aa:bb:cc:dd:ee:ff"
    asyncio.run(drv.on_mqtt_message(
        drv._resp_topic("ui_service", "tokenissuance"),
        b'{"accesstoken": "abc", "refreshtoken": "def"}'))
    assert drv.get_state("paired") is True
    assert drv.get_state("pin_pending") is False
