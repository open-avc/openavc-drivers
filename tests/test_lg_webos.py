"""Isolated logic tests for the lg_webos driver (LG webOS SSAP over WebSocket).

The driver's transport is a live secure WebSocket, so the full connect /
subscribe / push round trip is exercised against the real simulator and real
hardware (not committed here). What this module locks down is the protocol
logic that has to be right regardless of the socket:

  - subscription payload -> state mapping (power / volume+mute / foreground),
  - command -> SSAP request mapping (set_volume, mute, inputs, power_off),
  - Wake-on-LAN magic-packet bytes,
  - picker lists built from the metadata queries,
  - pairing-key persistence and the small helpers.

Loads the driver with the ``server.*`` / ``websockets`` imports stubbed so the
community CI stays self-contained (conftest.py rolls the stubs back after this
module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "displays" / "lg_webos.py"

_TMP = tempfile.mkdtemp(prefix="lg_webos_test_")


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value

    def set_batch(self, updates, **_):
        self.data.update(updates)


class _FakeEvents:
    async def emit(self, *args, **kwargs):
        return None


class _FakeFault(Exception):
    def __init__(self, message="", code=""):
        super().__init__(message)
        self.code = code


class _FakeBaseDriver:
    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._last_transport_error = ""

    def set_state(self, key, value):
        self.state.set(f"device.{self.device_id}.{key}", value)

    def set_states(self, updates):
        self.state.set_batch(
            {f"device.{self.device_id}.{k}": v for k, v in updates.items()})

    def get_state(self, key, default=None):
        return self.state.data.get(f"device.{self.device_id}.{key}", default)

    async def _verify_reachable(self, *a, **k):
        return True

    def _start_health_loop(self):
        pass

    def _stop_health_loop(self):
        pass

    def _handle_transport_disconnect(self):
        self._connected = False
        self.set_state("connected", False)


def _install_stubs() -> None:
    server = ModuleType("server")
    drivers = ModuleType("server.drivers")
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    base.ConnectionFaultError = _FakeFault
    drivers.base = base
    server.drivers = drivers

    system_config = ModuleType("server.system_config")
    system_config.get_system_config = lambda: SimpleNamespace(data_dir=_TMP)

    utils = ModuleType("server.utils")
    logger_mod = ModuleType("server.utils.logger")
    logger_mod.get_logger = lambda *_: SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None,
        warning=lambda *a, **k: None, error=lambda *a, **k: None)
    utils.logger = logger_mod

    ws_stub = ModuleType("websockets")
    ws_stub.connect = None  # tests never open a socket

    for name, mod in (
        ("server", server), ("server.drivers", drivers),
        ("server.drivers.base", base), ("server.system_config", system_config),
        ("server.utils", utils), ("server.utils.logger", logger_mod),
        ("websockets", ws_stub),
    ):
        sys.modules[name] = mod


def _load_driver():
    _install_stubs()
    spec = importlib.util.spec_from_file_location("lg_webos", DRIVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["lg_webos"] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_driver()
LgWebosDriver = _MOD.LgWebosDriver


def _driver(**config):
    cfg = {"host": "10.0.0.5", "port": 3001, "ssl": True,
           "mac_address": "60:8d:26:24:97:62"}
    cfg.update(config)
    return LgWebosDriver("tv1", cfg, _FakeState(), _FakeEvents())


# ── Subscription payload -> state mapping ───────────────────────────────────

def test_apply_power_states():
    d = _driver()
    d._apply_power({"state": "Active"})
    assert d.get_state("power") == "on"
    d._apply_power({"state": "Suspend"})
    assert d.get_state("power") == "standby"
    d._apply_power({"state": "Active Standby"})
    assert d.get_state("power") == "standby"


def test_apply_volume_maps_full_status():
    d = _driver()
    d._apply_volume({"volumeStatus": {
        "volume": 42, "muteStatus": True, "maxVolume": 100,
        "adjustVolume": False, "soundOutput": "external_arc"}})
    assert d.get_state("volume") == 42
    assert d.get_state("mute") is True
    assert d.get_state("max_volume") == 100
    assert d.get_state("can_set_volume") is False
    assert d.get_state("sound_output") == "HDMI ARC"


def test_apply_volume_change_carries_cause_shape():
    # A push on change carries a flat volumeStatus with a 'cause' field.
    d = _driver()
    d._apply_volume({"volumeStatus": {
        "cause": "volumeUp", "volume": 13, "muteStatus": False,
        "soundOutput": "tv_speaker"}})
    assert d.get_state("volume") == 13
    assert d.get_state("mute") is False
    assert d.get_state("sound_output") == "TV Speakers"


def test_apply_foreground_uses_label_map():
    d = _driver()
    d._app_labels = {"com.webos.app.hdmi2": "HDMI 2"}
    d._apply_foreground({"appId": "com.webos.app.hdmi2"})
    assert d.get_state("app_id") == "com.webos.app.hdmi2"
    assert d.get_state("input") == "HDMI 2"
    # An unknown app falls back to a humanized name.
    d._apply_foreground({"appId": "netflix"})
    assert d.get_state("input") == "Netflix"


# ── Command -> SSAP request mapping ─────────────────────────────────────────

class _RecordRequests:
    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    async def __call__(self, uri, payload=None, **kwargs):
        self.calls.append((uri, payload))
        return {}


def _with_recorded_requests(d):
    rec = _RecordRequests()
    d._request = rec
    return rec


def test_set_volume_sends_setvolume():
    d = _driver()
    rec = _with_recorded_requests(d)
    assert asyncio.run(d.send_command("set_volume", {"level": 30})) is True
    assert rec.calls == [("ssap://audio/setVolume", {"volume": 30})]
    # It does NOT optimistically set state — the subscription confirms it.
    assert d.get_state("volume") is None


def test_mute_and_volume_steps():
    d = _driver()
    rec = _with_recorded_requests(d)
    asyncio.run(d.send_command("mute", {"value": True}))
    asyncio.run(d.send_command("volume_up"))
    asyncio.run(d.send_command("volume_down"))
    assert rec.calls == [
        ("ssap://audio/setMute", {"mute": True}),
        ("ssap://audio/volumeUp", None),
        ("ssap://audio/volumeDown", None),
    ]


def test_input_and_app_launch_by_id():
    d = _driver()
    rec = _with_recorded_requests(d)
    asyncio.run(d.send_command("set_input", {"input": "com.webos.app.hdmi1"}))
    asyncio.run(d.send_command("launch_app", {"app": "netflix"}))
    assert rec.calls == [
        ("ssap://system.launcher/launch", {"id": "com.webos.app.hdmi1"}),
        ("ssap://system.launcher/launch", {"id": "netflix"}),
    ]


def test_power_off_sends_turnoff():
    d = _driver()
    rec = _with_recorded_requests(d)
    asyncio.run(d.send_command("power_off"))
    assert rec.calls == [("ssap://system/turnOff", None)]


def test_power_true_is_wol():
    d = _driver()
    _with_recorded_requests(d)
    sent = {}
    d._wake_on_lan = lambda: sent.setdefault("wol", True) or True
    assert asyncio.run(d.send_command("power", {"value": True})) is True
    assert sent.get("wol") is True


# ── Wake-on-LAN ─────────────────────────────────────────────────────────────

def test_wol_packet_bytes(monkeypatch):
    d = _driver(mac_address="60:8D:26:24:97:62")
    captured: list[tuple[bytes, tuple]] = []

    class _FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def sendto(self, data, dest):
            captured.append((data, dest))

    monkeypatch.setattr(_MOD.socket, "socket", lambda *a, **k: _FakeSock())
    assert d._wake_on_lan() is True
    packet = captured[0][0]
    assert len(packet) == 102               # 6 x 0xff + MAC x 16
    assert packet[:6] == b"\xff" * 6
    assert packet[6:12] == bytes.fromhex("608d26249762")
    assert packet.count(bytes.fromhex("608d26249762")) == 16


def test_wol_rejects_bad_mac():
    d = _driver(mac_address="not-a-mac")
    assert d._wake_on_lan() is False


# ── Offline Power On setup action ───────────────────────────────────────────

async def _noop_progress(*a, **k):
    return None


def test_power_on_is_an_offline_setup_action():
    # The only reliable power-on path is a kind:setup action marked offline,
    # because commands are blocked while the device is offline.
    actions = LgWebosDriver.DRIVER_INFO["actions"]
    wake = next(a for a in actions if a["id"] == "wake_tv")
    assert wake["kind"] == "setup"
    assert wake["availability"] == "offline"
    # power_on must NOT be a promoted quick action (it can't run while offline).
    assert "power_on" not in LgWebosDriver.DRIVER_INFO["quick_actions"]


def test_run_setup_action_fires_wol():
    d = _driver()
    fired = {}
    d._wake_on_lan = lambda: fired.setdefault("wol", True) or True
    result = asyncio.run(d.run_setup_action("wake_tv", {}, _noop_progress))
    assert fired.get("wol") is True
    assert result["success"] is True


def test_run_setup_action_requires_mac():
    d = _driver(mac_address="")
    import pytest
    with pytest.raises(ValueError):
        asyncio.run(d.run_setup_action("wake_tv", {}, _noop_progress))


def test_run_setup_action_rejects_unknown():
    d = _driver()
    import pytest
    with pytest.raises(ValueError):
        asyncio.run(d.run_setup_action("bogus", {}, _noop_progress))


# ── Metadata -> picker lists ────────────────────────────────────────────────

def test_load_metadata_builds_pickers():
    d = _driver()

    async def fake_request(uri, payload=None, **kwargs):
        if uri.endswith("getSystemInfo"):
            return {"modelName": "65UN6950ZUA"}
        if uri.endswith("getExternalInputList"):
            return {"devices": [
                {"appId": "com.webos.app.hdmi1", "label": "HDMI 1"},
                {"appId": "com.webos.app.hdmi2", "label": "HDMI 2"},
            ]}
        if uri.endswith("listLaunchPoints"):
            return {"launchPoints": [
                {"id": "netflix", "title": "Netflix"},
                {"id": "youtube.leanback.v4", "title": "YouTube"},
            ]}
        return {}

    d._request = fake_request
    asyncio.run(d._load_metadata())

    assert d.get_state("model") == "65UN6950ZUA"
    inputs = json.loads(d.get_state("input_options"))
    assert {"value": "com.webos.app.hdmi2", "label": "HDMI 2"} in inputs
    apps = json.loads(d.get_state("app_options"))
    assert {"value": "netflix", "label": "Netflix"} in apps
    # The appId->label map feeds foreground display.
    assert d._app_labels["com.webos.app.hdmi1"] == "HDMI 1"


# ── Pairing key + helpers ───────────────────────────────────────────────────

def test_pairing_key_store_and_forget():
    d = _driver()
    d._store_key("abc123")
    assert Path(d._key_path).read_text() == "abc123"
    d._forget_key()
    assert not Path(d._key_path).exists()


def test_helpers():
    assert _MOD._as_bool("true") is True
    assert _MOD._as_bool("off") is False
    assert _MOD._as_bool(1) is True
    assert _MOD._humanize_app("com.webos.app.hdmi3") == "HDMI 3"
    assert _MOD._humanize_app("com.acme.player") == "Player"


def test_ws_is_open_reads_state_enum():
    open_ws = SimpleNamespace(state=SimpleNamespace(name="OPEN"))
    closed_ws = SimpleNamespace(state=SimpleNamespace(name="CLOSED"))
    assert _MOD._ws_is_open(open_ws) is True
    assert _MOD._ws_is_open(closed_ws) is False


def test_discovery_block_declares_cert_probe():
    disc = LgWebosDriver.DRIVER_INFO["discovery"]
    assert disc["tcp_probe"]["port"] == 3001
    assert disc["tcp_probe"]["tls"] is True
    assert disc["tcp_probe"]["cert_subject"] == r"CN=LGE TV SSG"
    assert "urn:lge-com:service:webos-second-screen:1" in disc["ssdp"]
