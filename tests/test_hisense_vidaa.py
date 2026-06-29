"""Unit tests for the hisense_vidaa driver.

Loads ``displays/hisense_vidaa.py`` directly, stubbing the ``server.*`` imports
it needs (BaseDriver, get_system_config, get_logger) so the community repo's
test suite stays self-contained — mirrors test_qsc_qrc.py.

The credential generator is checked against the worked example documented in the
reverse-engineered protocol analysis (a real test vector), so the trickiest
piece is verified without a TV. Topic construction, command payloads, and state
parsing are exercised against a fake transport.
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

def _install_server_stubs(tmp_dir: str) -> None:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server

    drivers = ModuleType("server.drivers")
    drivers.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server.drivers"] = drivers

    base = ModuleType("server.drivers.base")

    class _BaseDriver:
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
            pass

    base.BaseDriver = _BaseDriver
    sys.modules["server.drivers.base"] = base

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
    async def emit(self, *a, **k):
        pass


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
    _install_server_stubs(tmp_dir)
    spec = importlib.util.spec_from_file_location("hisense_vidaa", DRIVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_driver(mod, **config):
    cfg = {"host": "10.0.0.5"}
    cfg.update(config)
    drv = mod.HisenseVidaaDriver("tv1", cfg, None, _Events())
    drv._client_id = "AA:BB:CC:DD:EE:FF$his$256DBF_vidaacommon_001"
    drv.transport = FakeTransport()
    return drv


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
