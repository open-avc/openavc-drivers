"""Tests for the hisense_vidaa simulator + driver duality.

Loads ``displays/hisense_vidaa_sim.py`` (stubbing the platform
``simulator.mqtt_simulator`` broker base with a fake that just records
publishes) and ``displays/hisense_vidaa.py`` (stubbing ``server.*``), then:

  1. drives the simulator's command hooks and checks it answers on the topics a
     real VIDAA TV uses, and
  2. pipes each simulated response straight into the driver's ``on_mqtt_message``
     and checks the driver's state updates — proving the topic/payload contract
     closes the loop without a real set.

Self-contained / stdlib so it runs in the community repo's isolated CI.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
SIM_PATH = REPO_ROOT / "displays" / "hisense_vidaa_sim.py"
DRIVER_PATH = REPO_ROOT / "displays" / "hisense_vidaa.py"

# The driver's MQTT client id (built from generate_credentials); the sim echoes
# it into response topics. Format mirrors a real connection.
CID = "AA:BB:CC:DD:EE:FF$his$256DBF_vidaacommon_001"


# ── Stub the platform MQTTSimulator broker base ──

def _install_simulator_stub() -> None:
    if "simulator.mqtt_simulator" in sys.modules:
        return
    pkg = ModuleType("simulator")
    pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("simulator", pkg)
    mod = ModuleType("simulator.mqtt_simulator")

    class _MQTTSimulator:
        """Fake broker base: records publishes, no sockets."""

        SIMULATOR_INFO: dict = {}

        def __init__(self, device_id, config=None):
            self.device_id = device_id
            self.config = config or {}
            self.name = self.SIMULATOR_INFO.get("name", device_id)
            self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
            self._client_meta: dict = {}
            self.unicast: list[tuple] = []     # (client_id, topic, payload)
            self.broadcasts: list[tuple] = []  # (topic, payload)

        def set_state(self, key, value):
            self._state[key] = value

        def get_state(self, key, default=None):
            return self._state.get(key, default)

        async def publish_to(self, client_id, topic, payload):
            self.unicast.append((client_id, topic, payload))

        async def broadcast(self, topic, payload):
            self.broadcasts.append((topic, payload))

    mod.MQTTSimulator = _MQTTSimulator
    sys.modules["simulator.mqtt_simulator"] = mod


# ── Stub server.* so the driver imports without an openavc install ──

def _install_server_stubs() -> None:
    if "server.drivers.base" in sys.modules:
        return
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server", server)
    drivers = ModuleType("server.drivers")
    drivers.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.drivers", drivers)
    base = ModuleType("server.drivers.base")

    class _BaseDriver:
        DRIVER_INFO: dict = {}

        def __init__(self, device_id, config, state=None, events=None):
            self.device_id = device_id
            self.config = config
            self.state = state
            self.events = events
            self.transport = None
            self._connected = False
            self._state: dict = {}

        def set_state(self, key, value):
            self._state[key] = value

        def get_state(self, key):
            return self._state.get(key)

    base.BaseDriver = _BaseDriver
    sys.modules["server.drivers.base"] = base

    sysconfig = ModuleType("server.system_config")
    sysconfig.get_system_config = lambda: type("C", (), {"data_dir": "/tmp"})()
    sys.modules["server.system_config"] = sysconfig
    utils = ModuleType("server.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.utils", utils)
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda *_a, **_k: type(
        "L", (), {"__getattr__": lambda s, n: (lambda *a, **k: None)})()
    sys.modules["server.utils.logger"] = logger


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


_install_simulator_stub()
_install_server_stubs()
simmod = _load(SIM_PATH, "hisense_vidaa_sim_under_test")
drvmod = _load(DRIVER_PATH, "hisense_vidaa_driver_for_sim_test")


def _make_sim():
    sim = simmod.HisenseVidaaSimulator("vidaa-sim")
    sim._client_meta["c1"] = {"mqtt_client_id": CID, "username": "his$x"}
    return sim


def _cmd(service, action):
    return f"/remoteapp/tv/{service}/{CID}/actions/{action}"


def _pub(sim, service, action, payload=""):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    asyncio.run(sim.on_publish("c1", _cmd(service, action), body.encode()))


# ── Simulator answers on the topics a real VIDAA TV uses ──

def test_app_connect_requests_pairing_when_unpaired():
    sim = _make_sim()
    _pub(sim, "ui_service", "vidaa_app_connect",
         {"app_version": 2, "device_type": "Mobile App"})
    assert sim.unicast
    cid, topic, _ = sim.unicast[-1]
    assert topic == f"/remoteapp/mobile/{CID}/ui_service/data/authentication"


def test_auth_code_issues_token():
    sim = _make_sim()
    _pub(sim, "ui_service", "authenticationcode", {"authNum": "1234"})
    topics = {t for _, t, _ in sim.unicast}
    token_topic = f"/remoteapp/mobile/{CID}/ui_service/data/tokenissuance"
    assert token_topic in topics
    token_payload = next(p for _, t, p in sim.unicast if t == token_topic)
    assert json.loads(token_payload)["accesstoken"]
    assert sim.get_state("authenticated") is True


def test_app_connect_after_pairing_pushes_state_not_pin():
    sim = _make_sim()
    sim.set_state("authenticated", True)
    _pub(sim, "ui_service", "vidaa_app_connect", {"device_type": "Mobile App"})
    cid, topic, _ = sim.unicast[-1]
    assert topic.endswith("/ui_service/data/state")


def test_sendkey_volume_up_broadcasts_incremented_volume():
    sim = _make_sim()
    sim.set_state("volume", 20)
    _pub(sim, "remote_service", "sendkey", "KEY_VOLUMEUP")
    topic, payload = sim.broadcasts[-1]
    assert "volumechange" in topic
    assert json.loads(payload)["volume_value"] == 21


def test_power_key_toggles_and_broadcasts_standby():
    sim = _make_sim()
    sim.set_state("power", True)
    _pub(sim, "remote_service", "sendkey", "KEY_POWER")
    topic, payload = sim.broadcasts[-1]
    assert topic.endswith("/ui_service/state")
    assert json.loads(payload)["statetype"] == "fake_sleep_0"


def test_changevolume_sets_level():
    sim = _make_sim()
    _pub(sim, "platform_service", "changevolume", "55")
    topic, payload = sim.broadcasts[-1]
    assert json.loads(payload)["volume_value"] == 55


def test_changesource_maps_id_to_name():
    sim = _make_sim()
    _pub(sim, "ui_service", "changesource", {"sourceid": "1"})
    topic, payload = sim.broadcasts[-1]
    assert json.loads(payload)["sourcename"] == "HDMI 1"
    assert sim.get_state("source") == "HDMI 1"


def test_gettvstate_answers_on_data_state():
    sim = _make_sim()
    _pub(sim, "ui_service", "gettvstate")
    cid, topic, payload = sim.unicast[-1]
    assert topic == f"/remoteapp/mobile/{CID}/ui_service/data/state"
    assert json.loads(payload)["statetype"]


def test_sourcelist_returns_inputs():
    sim = _make_sim()
    _pub(sim, "ui_service", "sourcelist")
    cid, topic, payload = sim.unicast[-1]
    assert topic.endswith("/ui_service/data/sourcelist")
    names = [s["sourcename"] for s in json.loads(payload)]
    assert "HDMI 1" in names and "TV" in names


def test_cid_falls_back_to_topic_when_meta_missing():
    sim = simmod.HisenseVidaaSimulator("vidaa-sim")  # no _client_meta entry
    _pub(sim, "ui_service", "gettvstate")
    cid, topic, _ = sim.unicast[-1]
    assert topic == f"/remoteapp/mobile/{CID}/ui_service/data/state"


# ── Duality: sim responses drive the real driver's state ──

def _make_driver():
    drv = drvmod.HisenseVidaaDriver("tv1", {"host": "10.0.0.5"}, None, None)
    drv._client_id = CID
    return drv


def _feed(drv, sim):
    """Pipe every message the sim emitted into the driver, then clear."""
    msgs = [(t, p) for _, t, p in sim.unicast] + list(sim.broadcasts)
    for topic, payload in msgs:
        body = payload if isinstance(payload, bytes) else payload.encode()
        asyncio.run(drv.on_mqtt_message(topic, body))
    sim.unicast.clear()
    sim.broadcasts.clear()


def test_pairing_round_trip_marks_driver_paired():
    sim, drv = _make_sim(), _make_driver()
    _pub(sim, "ui_service", "vidaa_app_connect", {"device_type": "Mobile App"})
    _feed(drv, sim)
    assert drv.get_state("pin_pending") is True

    _pub(sim, "ui_service", "authenticationcode", {"authNum": "4821"})
    _feed(drv, sim)
    assert drv.get_state("paired") is True
    assert drv.get_state("pin_pending") is False


def test_volume_round_trip_updates_driver():
    sim, drv = _make_sim(), _make_driver()
    _pub(sim, "platform_service", "changevolume", "42")
    _feed(drv, sim)
    assert drv.get_state("volume") == 42


def test_source_round_trip_updates_driver():
    sim, drv = _make_sim(), _make_driver()
    _pub(sim, "ui_service", "changesource", {"sourceid": "2"})
    _feed(drv, sim)
    assert drv.get_state("source") == "HDMI 2"
    assert drv.get_state("power") is True


def test_sourcelist_round_trip_populates_picker():
    sim, drv = _make_sim(), _make_driver()
    _pub(sim, "ui_service", "sourcelist")
    _feed(drv, sim)
    sources = json.loads(drv.get_state("sources"))
    assert "HDMI 1" in sources


def test_power_key_round_trip_sets_driver_standby():
    sim, drv = _make_sim(), _make_driver()
    sim.set_state("power", True)
    _pub(sim, "remote_service", "sendkey", "KEY_POWER")
    _feed(drv, sim)
    assert drv.get_state("power") is False
