"""Driver + simulator tests for obs_studio (OBS Studio over obs-websocket 5).

Dual-proof round trip: the real driver is wired to the real simulator over an
in-memory WebSocket, so the simulator renders what OBS sends and the driver
parses it, both sides asserted.

Covers:
  - the Hello / Identify handshake: no password, the right password, a wrong
    password as a typed auth_failed, a blank password refused before Identify
    is sent, the event-subscription mask with and without meters;
  - the connect sync: version and video settings, scenes with program and
    preview flags, inputs with audio state and kind-specific extras (text,
    media), scene items, filters, outputs, transitions, collections,
    profiles, hotkeys, monitors and statistics;
  - every command against the simulator's own refusals (studio mode off,
    output already running, a non-audio input, a fixed transition);
  - changes made in OBS itself (the simulator's UI paths) landing in state
    through events: scene switches with active flags, mute, volume, renames,
    scene creation and removal, a scene collection change;
  - polling, the level meters, device settings, the liveness probe and a
    clean disconnect.

The driver is loaded with the ``openavc.*`` and ``websockets`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the
stubs back).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    ConnectionFaultError,
    StubBaseDriver,
    StubBaseSimulator,
    StubEvents,
    StubState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "video" / "obs_studio.py"
SIM_PATH = REPO_ROOT / "video" / "obs_studio_sim.py"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect lifecycle for a driver that owns its
    session; state, children and the watchdog come from the shared stubs."""

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0
        self.disconnect_events = 0

    @property
    def connected(self) -> bool:
        return bool(self._connected) and self._link_alive()

    async def _pre_connect(self):
        return None

    async def _create_transport(self, transport_type):
        return None

    async def _post_connect(self):
        return None

    async def _initial_sync(self):
        return None

    async def _close_session(self):
        return None

    def _link_alive(self):
        return False

    async def _start_push(self):
        return None

    async def _stop_push(self):
        return None

    def _handle_transport_disconnect(self):
        self.disconnect_events += 1
        self._connected = False
        self.set_state("connected", False)

    async def connect(self):
        await self._stop_push()
        await self._close_session()
        await self._pre_connect()
        await self._create_transport(self.DRIVER_INFO.get("transport", "tcp"))
        try:
            await self._post_connect()
            self._connected = True
            self.set_state("connected", True)
            await self.events.emit(f"device.connected.{self.device_id}")
        except Exception:
            await self._close_session()
            self._connected = False
            raise
        await self._start_push()
        try:
            await self._initial_sync()
        except Exception:
            await self._stop_push()
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise
        interval = self.config.get("poll_interval", 0)
        if interval > 0:
            await self.start_polling(interval)

    async def disconnect(self):
        await self._stop_push()
        await self.stop_polling()
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


class _FakeConnectionClosed(Exception):
    """websockets.exceptions.ConnectionClosed: carries the close frame."""

    def __init__(self, code=1000, reason=""):
        super().__init__(f"received {code} ({reason})")
        self.rcvd = SimpleNamespace(code=code, reason=reason)


class _FakeWsSimBase(StubBaseSimulator):
    """openavc.simulator.websocket_simulator.WebSocketSimulator: the state
    store with change listeners; the aiohttp server is not exercised."""

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._change_listeners = []
        self._clients = set()

    def add_change_listener(self, listener):
        self._change_listeners.append(listener)

    def set_state(self, key, value):
        old = self._state.get(key)
        if old == value:
            return
        self._state[key] = value
        for listener in list(self._change_listeners):
            listener("state", {"device_id": self.device_id, "key": key, "value": value, "old_value": old})


_WS_EXC = ModuleType("websockets.exceptions")
_WS_EXC.ConnectionClosed = _FakeConnectionClosed

install_stubs(
    {"websockets": {"connect": None, "exceptions": _WS_EXC},
     "websockets.exceptions": {"ConnectionClosed": _FakeConnectionClosed},
     "openavc.simulator.websocket_simulator": {"WebSocketSimulator": _FakeWsSimBase}},
    base_driver=_FakeBaseDriver,
)
DRV = load_module("obs_studio_under_test", DRIVER_PATH)
SIM = load_module("obs_studio_sim_under_test", SIM_PATH)


# ── Harness ──────────────────────────────────────────────────────────────────


class _ServerEnd:
    """The simulator's end of the socket: frames land in the driver's queue,
    a close lands as a ConnectionClosed with the code."""

    def __init__(self, incoming: asyncio.Queue):
        self._incoming = incoming
        self.close_code = None

    async def send(self, text):
        await self._incoming.put(text)

    async def close(self, code, reason):
        self.close_code = code
        await self._incoming.put(_FakeConnectionClosed(code, reason))


class _FakeSocket:
    """The driver's end. Everything the driver sends goes straight into the
    simulator's session; replies and events come back through recv."""

    def __init__(self, sim):
        self._sim = sim
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.server = _ServerEnd(self._incoming)
        self.client = sim.ws_open(self.server)
        self.state = SimpleNamespace(name="OPEN")
        self.sent: list[dict] = []

    async def send(self, text):
        self.sent.append(json.loads(text))
        await self._sim.ws_message(self.client, text)

    async def recv(self):
        item = await self._incoming.get()
        if isinstance(item, Exception):
            self.state = SimpleNamespace(name="CLOSED")
            raise item
        if item is None:
            self.state = SimpleNamespace(name="CLOSED")
            raise _FakeConnectionClosed(1000, "")
        return item

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.state.name != "OPEN":
            raise StopAsyncIteration
        try:
            return await self.recv()
        except _FakeConnectionClosed as exc:
            if exc.rcvd.code == 1000:
                raise StopAsyncIteration
            raise

    async def close(self):
        if self.state.name == "OPEN":
            self.state = SimpleNamespace(name="CLOSED")
            self._sim.ws_close(self.client)
            await self._incoming.put(None)


def _make(sim_config=None, driver_config=None):
    sim = SIM.ObsStudioSimulator("obs-sim", sim_config or {})
    sockets: list[_FakeSocket] = []

    async def fake_connect(url, **kwargs):
        sock = _FakeSocket(sim)
        sock.url = url
        sock.kwargs = kwargs
        sockets.append(sock)
        return sock

    cfg = {"host": "10.0.0.20", "port": 4455, "password": "", "poll_interval": 0}
    cfg.update(driver_config or {})
    driver = DRV.ObsStudioDriver("obs1", cfg, StubState(), StubEvents())
    return driver, sim, fake_connect, sockets


async def _connect(driver, fake_connect):
    DRV.websockets.connect = fake_connect
    await driver.connect()
    await _settle()


async def _settle(seconds: float = 0.05):
    await asyncio.sleep(seconds)


async def _connected(sim_config=None, driver_config=None):
    driver, sim, fake_connect, sockets = _make(sim_config, driver_config)
    await _connect(driver, fake_connect)
    return driver, sim, sockets


def _run(coro):
    async def bounded():
        return await asyncio.wait_for(coro, timeout=10)
    return asyncio.run(bounded())


def _st(driver, key):
    return driver.state.get(f"device.{driver.device_id}.{key}")


def _child(driver, child_type, local_id, key):
    return driver.state.get(f"device.{driver.device_id}.{child_type}.{local_id}.{key}")


def _children(driver, child_type):
    return sorted(driver.list_children(child_type))


# ── Metadata / shape ─────────────────────────────────────────────────────────


def test_every_declared_command_has_a_dispatch_branch():
    declared = set(DRV.ObsStudioDriver.DRIVER_INFO["commands"])
    assert set(DRV.ObsStudioDriver._DISPATCH) == declared


def test_actions_and_quick_actions_name_declared_commands():
    info = DRV.ObsStudioDriver.DRIVER_INFO
    assert info["category"] == "video"
    assert info["transport"] == "tcp"
    for action in info["actions"]:
        assert action["id"] in info["commands"]
    for qa in info["quick_actions"]:
        assert qa in info["commands"]
    gates = {a["id"]: a.get("visible_when", {}).get("key") for a in info["actions"] if "visible_when" in a}
    assert gates == {"transition": "device.$id.studio_mode", "save_replay_buffer": "device.$id.replay_buffer"}


def test_every_event_the_simulator_fires_has_a_handler_or_is_deliberately_unused():
    handled = set(DRV.ObsStudioDriver._EVENTS)
    fired = set(SIM.EVENT_INTENT)
    # CustomEvent is an application-level relay the driver does not consume.
    assert fired - handled == set()


def test_auth_string_follows_the_documented_recipe():
    import base64
    import hashlib
    password, salt, challenge = "supersecretpassword", "c2FsdA==", "Y2hhbGxlbmdl"
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    expected = base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()
    assert DRV.auth_string(password, salt, challenge) == expected


def test_child_ids_are_readable_and_never_collide():
    roster = DRV._Roster()
    assert roster.assign("Mic/Aux") == "Mic_Aux"
    assert roster.assign("Mic Aux") == "Mic_Aux_2"
    assert roster.assign("Mic/Aux") == "Mic_Aux"
    assert roster.name_by_id["Mic_Aux_2"] == "Mic Aux"
    assert roster.drop("Mic/Aux") == "Mic_Aux"
    assert "Mic_Aux" not in roster.name_by_id


# ── Handshake ────────────────────────────────────────────────────────────────


def test_connects_without_a_password_and_subscribes_to_events():
    async def go():
        driver, sim, sockets = await _connected()
        assert driver.connected
        assert _st(driver, "obs_version") == SIM.OBS_VERSION
        assert _st(driver, "websocket_version") == SIM.WS_VERSION
        assert _st(driver, "rpc_version") == 1
        identify = sockets[0].sent[0]
        assert identify["op"] == 1
        assert "authentication" not in identify["d"]
        mask = identify["d"]["eventSubscriptions"]
        assert mask & SIM.SUB["Scenes"] and mask & SIM.SUB["InputActiveStateChanged"]
        assert not mask & SIM.SUB["InputVolumeMeters"]
        assert sockets[0].kwargs["subprotocols"] == ["obswebsocket.json"]
        assert sockets[0].url == "ws://10.0.0.20:4455"
    _run(go())


def test_the_right_password_answers_the_challenge():
    async def go():
        driver, sim, sockets = await _connected({"password": "hunter2"}, {"password": "hunter2"})
        assert driver.connected
        identify = sockets[0].sent[0]["d"]
        assert identify["authentication"] == DRV.auth_string("hunter2", sockets[0].client.salt, sockets[0].client.challenge)
    _run(go())


def test_a_wrong_password_is_a_typed_auth_failure():
    async def go():
        driver, sim, fake_connect, sockets = _make({"password": "hunter2"}, {"password": "wrong"})
        with pytest.raises(ConnectionFaultError) as info:
            await _connect(driver, fake_connect)
        assert info.value.fault_code == "auth_failed"
        assert sockets[0].server.close_code == 4009
        assert not driver.connected
    _run(go())


def test_a_blank_password_is_refused_before_identify_is_sent():
    async def go():
        driver, sim, fake_connect, sockets = _make({"password": "hunter2"}, {"password": ""})
        with pytest.raises(ConnectionFaultError) as info:
            await _connect(driver, fake_connect)
        assert info.value.fault_code == "auth_failed"
        assert sockets[0].sent == []
    _run(go())


def test_the_authentication_failed_error_mode_rejects_every_password():
    async def go():
        driver, sim, fake_connect, sockets = _make({}, {"password": "anything"})
        sim.inject_error("authentication_failed")
        with pytest.raises(ConnectionFaultError) as info:
            await _connect(driver, fake_connect)
        assert info.value.fault_code == "auth_failed"
    _run(go())


def test_meters_are_subscribed_only_when_enabled():
    async def go():
        driver, sim, sockets = await _connected(None, {"enable_meters": True})
        mask = sockets[0].sent[0]["d"]["eventSubscriptions"]
        assert mask & SIM.SUB["InputVolumeMeters"]
        await _settle(0.3)
        assert _child(driver, "input", "Mic_Aux", "level_db") > -100
        # Desktop Audio is muted in the fixture: no meter for it.
        assert _child(driver, "input", "Desktop_Audio", "level_db") in (None, -100.0)
        await driver.disconnect()
        assert sim._meter_task is None or sim._meter_task.cancelled() or sim._meter_task.done()
    _run(go())


# ── Connect sync ─────────────────────────────────────────────────────────────


def test_connect_sync_reads_the_whole_fixture():
    async def go():
        driver, sim, sockets = await _connected()
        assert _st(driver, "platform") == "macos"
        assert _st(driver, "base_resolution") == "1920x1080"
        assert _st(driver, "output_resolution") == "1280x720"
        assert _st(driver, "fps") == 30.0
        assert _st(driver, "record_directory") == SIM.RECORD_DIRECTORY
        assert _st(driver, "studio_mode") is False
        assert _st(driver, "program_scene") == "Camera"
        assert _st(driver, "preview_scene") == ""
        assert json.loads(_st(driver, "scene_options")) == ["Camera", "Slides", "Break"]
        assert _st(driver, "scene_count") == 3
        assert _st(driver, "input_count") == 8
        assert _st(driver, "transition") == "Fade"
        assert _st(driver, "transition_duration_ms") == 300
        assert _st(driver, "transition_fixed") is False
        assert json.loads(_st(driver, "transition_options")) == ["Cut", "Fade", "Swipe"]
        assert _st(driver, "scene_collection") == "Lecture Hall"
        assert json.loads(_st(driver, "scene_collection_options")) == ["Lecture Hall", "Studio"]
        assert _st(driver, "profile") == "Default"
        assert json.loads(_st(driver, "profile_options")) == ["Default", "Streaming"]
        assert _st(driver, "streaming") is False and _st(driver, "recording") is False
        assert _st(driver, "stream_state") == "stopped" and _st(driver, "record_state") == "stopped"
        assert _st(driver, "virtual_camera") is False and _st(driver, "replay_buffer") is False
        assert _st(driver, "cpu_percent") == 4.2
        assert _st(driver, "active_fps") == 30.0
        assert _st(driver, "render_total_frames") == 180000
        assert json.loads(_st(driver, "hotkey_options")) == SIM.HOTKEYS
        monitors = json.loads(_st(driver, "monitor_options"))
        assert monitors[0] == {"value": "-1", "label": "Windowed"}
        assert monitors[1]["value"] == "0" and "Built-in Display" in monitors[1]["label"]
        assert set(json.loads(_st(driver, "source_options"))) == set(SIM.INPUTS) | set(SIM.SCENES)

        # Scenes: program flag on Camera, positions top-first.
        assert _children(driver, "scene") == ["Break", "Camera", "Slides"]
        assert _child(driver, "scene", "Camera", "program") is True
        assert _child(driver, "scene", "Slides", "program") is False
        assert _child(driver, "scene", "Camera", "index") == 2
        assert _child(driver, "scene", "Camera", "item_count") == 3

        # Inputs: audio state only where the input has audio.
        assert _children(driver, "input") == sorted([
            "Camera", "Mic_Aux", "Desktop_Audio", "Slides_Capture", "Lower_Third", "Logo", "Break_Loop", "Web_Timer"])
        assert _child(driver, "input", "Mic_Aux", "has_audio") is True
        assert _child(driver, "input", "Mic_Aux", "muted") is False
        assert _child(driver, "input", "Mic_Aux", "volume_db") == -6.0
        assert _child(driver, "input", "Mic_Aux", "balance") == 0.5
        assert _child(driver, "input", "Mic_Aux", "monitor_type") == "none"
        assert _child(driver, "input", "Desktop_Audio", "muted") is True
        assert _child(driver, "input", "Camera", "has_audio") is False
        assert _child(driver, "input", "Camera", "active") is True
        assert _child(driver, "input", "Slides_Capture", "active") is False
        assert _child(driver, "input", "Lower_Third", "is_text") is True
        assert _child(driver, "input", "Lower_Third", "text") == "Welcome"
        assert _child(driver, "input", "Break_Loop", "is_media") is True
        assert _child(driver, "input", "Break_Loop", "media_state") == "stopped"
        assert _child(driver, "input", "Break_Loop", "kind") == "ffmpeg_source"

        # Scene items: one child per (scene, item), labelled scene: source.
        items = _children(driver, "scene_item")
        assert len(items) == 8
        assert _child(driver, "scene_item", "Camera__1", "source") == "Camera"
        lower_third = next(i for i in items if _child(driver, "scene_item", i, "source") == "Lower Third")
        assert _child(driver, "scene_item", lower_third, "enabled") is False
        assert _child(driver, "scene_item", lower_third, "name") == "Camera: Lower Third"
        assert _child(driver, "scene_item", lower_third, "source_type") == "input"

        # Filters, outputs.
        assert _children(driver, "filter") == ["Camera__Color_Correction", "Mic_Aux__Compressor", "Mic_Aux__Noise_Suppression"]
        assert _child(driver, "filter", "Mic_Aux__Compressor", "enabled") is False
        assert _child(driver, "filter", "Mic_Aux__Compressor", "name") == "Mic/Aux: Compressor"
        assert _children(driver, "output") == ["Program_NDI", "simple_file_output", "simple_stream", "virtualcam_output"]
        assert _child(driver, "output", "Program_NDI", "kind") == "ndi_output"
        assert _child(driver, "output", "Program_NDI", "active") is False
    _run(go())


# ── Scenes, studio mode, transitions ─────────────────────────────────────────


def test_program_scene_switch_updates_flags_and_active_inputs():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.send_command("set_program_scene", {"scene": "Slides"})
        await _settle()
        assert sim.get_state("program_scene") == "Slides"
        assert _st(driver, "program_scene") == "Slides"
        assert _child(driver, "scene", "Slides", "program") is True
        assert _child(driver, "scene", "Camera", "program") is False
        assert _child(driver, "input", "Slides_Capture", "active") is True
        assert _child(driver, "input", "Logo", "active") is False
        assert _child(driver, "input", "Camera", "active") is True  # in both scenes
        assert _st(driver, "transition_in_progress") is True
        await _settle(0.35)
        assert _st(driver, "transition_in_progress") is False
        with pytest.raises(ValueError):
            await driver.send_command("set_program_scene", {"scene": "Nope"})
    _run(go())


def test_studio_mode_preview_and_transition():
    async def go():
        driver, sim, sockets = await _connected()
        with pytest.raises(ValueError) as info:
            await driver.send_command("set_preview_scene", {"scene": "Break"})
        assert "Studio mode is not active" in str(info.value)
        # A refusal that comes with no comment is rendered by its code.
        assert DRV.ObsRequestError("SetCurrentPreviewScene", 506).text == "studio mode is not active"
        assert "Studio mode" in _st(driver, "last_error")
        await driver.send_command("studio_mode_on", {})
        await _settle()
        assert _st(driver, "studio_mode") is True
        assert _st(driver, "preview_scene") == "Camera"
        assert _child(driver, "scene", "Camera", "preview") is True
        await driver.send_command("set_preview_scene", {"scene": "Break"})
        await _settle()
        assert _st(driver, "preview_scene") == "Break"
        assert _child(driver, "scene", "Break", "preview") is True
        assert _child(driver, "scene", "Camera", "preview") is False
        await driver.send_command("transition", {})
        await _settle()
        assert _st(driver, "program_scene") == "Break"
        assert _st(driver, "preview_scene") == "Camera"
        assert _st(driver, "last_error") == ""
        await driver.send_command("set_studio_mode", {"enabled": False})
        await _settle()
        assert _st(driver, "studio_mode") is False
        assert _st(driver, "preview_scene") == ""
        assert _child(driver, "scene", "Camera", "preview") is False
    _run(go())


def test_transition_kind_and_duration():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.send_command("set_transition_duration", {"duration_ms": 750})
        await _settle()
        assert _st(driver, "transition_duration_ms") == 750
        assert sim.get_state("transition_duration") == 750
        await driver.send_command("set_transition", {"name": "Cut"})
        await _settle()
        assert _st(driver, "transition") == "Cut"
        assert _st(driver, "transition_fixed") is True
        # OBS keeps a duration set while Cut is current (for the next transition).
        await driver.send_command("set_transition_duration", {"duration_ms": 500})
        await _settle()
        assert _st(driver, "transition_duration_ms") == 500
        with pytest.raises(ValueError):
            await driver.send_command("set_transition", {"name": "Wipe"})
        await driver.send_command("studio_mode_on", {})
        await driver.send_command("set_tbar_position", {"position": 1.0})
        await _settle()
        assert sim.get_state("tbar_position") == 1.0
    _run(go())


# ── Outputs ──────────────────────────────────────────────────────────────────


def test_stream_start_stop_and_refusals():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.send_command("start_stream", {})
        await _settle()
        assert sim.get_state("streaming") is True
        assert _st(driver, "streaming") is True
        assert _st(driver, "stream_state") == "started"
        assert _child(driver, "output", "simple_stream", "active") is True
        with pytest.raises(ValueError) as info:
            await driver.send_command("start_stream", {})
        assert "already active" in str(info.value)
        assert await driver.send_command("toggle_stream", {}) is False
        await _settle()
        assert _st(driver, "streaming") is False
        assert _st(driver, "stream_state") == "stopped"
        with pytest.raises(ValueError):
            await driver.send_command("stop_stream", {})
        await driver.send_command("send_caption", {"text": "Hello  "})
        assert sim.captions == ["Hello  "]
    _run(go())


def test_record_pause_resume_split_and_file():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.send_command("start_record", {})
        await _settle()
        assert _st(driver, "recording") is True and _st(driver, "record_paused") is False
        assert _st(driver, "record_state") == "started"
        # OBS acknowledges the pause and applies it at the next keyframe; the
        # command holds until the paused event lands, then state agrees.
        await driver.send_command("pause_record", {})
        assert _st(driver, "record_paused") is True
        assert _st(driver, "record_state") == "paused"
        with pytest.raises(ValueError):
            await driver.send_command("pause_record", {})
        await driver.send_command("resume_record", {})
        await _settle()
        assert _st(driver, "record_paused") is False
        assert _st(driver, "record_state") == "resumed"
        await driver.send_command("split_record_file", {})
        await _settle()
        assert _st(driver, "record_file").endswith("(1).mkv")
        await driver.send_command("create_record_chapter", {"name": "Q&A"})
        assert sim.chapters == ["Q&A"]
        path = await driver.send_command("stop_record", {})
        await _settle()
        assert path == sim.record_file
        assert _st(driver, "recording") is False
        assert _st(driver, "record_state") == "stopped"
        assert _st(driver, "record_file") == sim.record_file
        # A poll settles the steady state without touching a transient one.
        await driver.poll()
        assert _st(driver, "record_state") == "stopped"
    _run(go())


def test_a_pause_obs_never_applies_is_reported_not_faked(monkeypatch):
    async def go():
        driver, sim, sockets = await _connected()
        monkeypatch.setattr(DRV, "PAUSE_CONFIRM_S", 0.3)
        sim.inject_error("pause_unavailable")
        await driver.send_command("start_record", {})
        await _settle()
        with pytest.raises(ValueError) as info:
            await driver.send_command("pause_record", {})
        assert "did not pause" in str(info.value)
        assert _st(driver, "record_paused") is False
        assert _st(driver, "record_state") == "started"
        with pytest.raises(ValueError):
            await driver.send_command("toggle_record_pause", {})
        sim.clear_error("pause_unavailable")
        await driver.send_command("toggle_record_pause", {})
        assert _st(driver, "record_paused") is True
        await driver.send_command("toggle_record_pause", {})
        await _settle()
        assert _st(driver, "record_paused") is False
    _run(go())


def test_an_output_that_is_stopping_is_still_running():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.send_command("start_record", {})
        await _settle()
        # OBS sends STOPPING with outputActive false while a Start is still
        # refused; the driver keeps the output running until STOPPED.
        await sim.emit("RecordStateChanged", {"outputActive": False, "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPING"})
        await _settle()
        assert _st(driver, "recording") is True
        assert _st(driver, "record_state") == "stopping"
        await sim.emit("RecordStateChanged", {"outputActive": False, "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED", "outputPath": "/x.mkv"})
        await _settle()
        assert _st(driver, "recording") is False
        assert _st(driver, "record_file") == "/x.mkv"
        await sim.emit("StreamStateChanged", {"outputActive": False, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTING"})
        await _settle()
        assert _st(driver, "streaming") is True and _st(driver, "stream_state") == "starting"
    _run(go())


def test_virtual_camera_replay_buffer_and_other_outputs():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.send_command("start_virtual_camera", {})
        await _settle()
        assert _st(driver, "virtual_camera") is True
        assert _child(driver, "output", "virtualcam_output", "active") is True
        with pytest.raises(ValueError):
            await driver.send_command("save_replay_buffer", {})
        await driver.send_command("start_replay_buffer", {})
        await _settle()
        assert _st(driver, "replay_buffer") is True
        await driver.send_command("save_replay_buffer", {})
        await _settle()
        assert _st(driver, "last_replay_file").startswith(SIM.RECORD_DIRECTORY)
        assert await driver.send_command("toggle_virtual_camera", {}) is False
        await driver.send_command("start_output", {"output": "Program_NDI"})
        assert sim.outputs["Program NDI"]["active"] is True
        assert _child(driver, "output", "Program_NDI", "active") is True
        assert await driver.send_command("toggle_output", {"output": "Program_NDI"}) is False
        assert _child(driver, "output", "Program_NDI", "active") is False
        with pytest.raises(ValueError):
            await driver.send_command("stop_output", {"output": "Program_NDI"})
    _run(go())


# ── Inputs ───────────────────────────────────────────────────────────────────


def test_input_audio_commands_and_refusals():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.send_command("mute_input", {"input": "Mic_Aux"})
        await _settle()
        assert sim.inputs["Mic/Aux"]["muted"] is True
        assert _child(driver, "input", "Mic_Aux", "muted") is True
        assert sim.get_state("mic_muted") is True
        assert await driver.send_command("toggle_input_mute", {"input": "Mic_Aux"}) is False
        await _settle()
        assert _child(driver, "input", "Mic_Aux", "muted") is False
        with pytest.raises(ValueError) as info:
            await driver.send_command("mute_input", {"input": "Camera"})
        assert "does not support audio" in str(info.value)

        await driver.send_command("set_input_volume", {"input": "Mic_Aux", "level_db": -12.5})
        await _settle()
        assert _child(driver, "input", "Mic_Aux", "volume_db") == -12.5
        assert sim.get_state("mic_volume_db") == -12.5
        level = await driver.send_command("adjust_input_volume", {"input": "Mic_Aux", "delta_db": 50})
        assert level == 26.0
        await _settle()
        assert _child(driver, "input", "Mic_Aux", "volume_db") == 26.0
        await driver.send_command("set_input_volume", {"input": "Mic_Aux", "level_db": -100})
        await _settle()
        assert _child(driver, "input", "Mic_Aux", "volume_db") == -100.0  # OBS reports null, the floor
        assert _child(driver, "input", "Mic_Aux", "volume_mul") == 0.0

        await driver.send_command("set_input_balance", {"input": "Mic_Aux", "balance": 0.25})
        await driver.send_command("set_input_sync_offset", {"input": "Mic_Aux", "offset_ms": 120})
        await driver.send_command("set_input_monitoring", {"input": "Mic_Aux", "monitor_type": "monitor_and_output"})
        await _settle()
        assert _child(driver, "input", "Mic_Aux", "balance") == 0.25
        assert _child(driver, "input", "Mic_Aux", "sync_offset_ms") == 120
        assert _child(driver, "input", "Mic_Aux", "monitor_type") == "monitor_and_output"
        assert sim.inputs["Mic/Aux"]["monitor"] == "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"
        with pytest.raises(ValueError):
            await driver.send_command("set_input_monitoring", {"input": "Mic_Aux", "monitor_type": "loud"})
    _run(go())


def test_media_text_and_browser_commands():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.send_command("media_play", {"input": "Break_Loop"})
        await _settle()
        assert _child(driver, "input", "Break_Loop", "media_state") == "playing"
        assert _child(driver, "input", "Break_Loop", "media_duration_ms") == 184000
        await driver.send_command("set_media_position", {"input": "Break_Loop", "position_ms": 60000})
        assert _child(driver, "input", "Break_Loop", "media_cursor_ms") == 60000
        await driver.send_command("offset_media_position", {"input": "Break_Loop", "offset_ms": -10000})
        assert _child(driver, "input", "Break_Loop", "media_cursor_ms") == 50000
        await driver.send_command("media_pause", {"input": "Break_Loop"})
        await _settle()
        assert _child(driver, "input", "Break_Loop", "media_state") == "paused"
        await driver.send_command("media_stop", {"input": "Break_Loop"})
        await _settle()
        assert _child(driver, "input", "Break_Loop", "media_state") == "stopped"
        with pytest.raises(ValueError):
            await driver.send_command("media_play", {"input": "Logo"})

        await driver.send_command("set_text", {"input": "Lower_Third", "text": "  Dr Jones  "})
        await _settle()
        assert sim.inputs["Lower Third"]["settings"]["text"] == "  Dr Jones  "
        assert _child(driver, "input", "Lower_Third", "text") == "  Dr Jones  "
        await driver.send_command("refresh_browser_source", {"input": "Web_Timer"})
        assert sim.inputs["Web Timer"]["refreshes"] == 1
        with pytest.raises(ValueError):
            await driver.send_command("refresh_browser_source", {"input": "Logo"})
    _run(go())


# ── Scene items, filters ─────────────────────────────────────────────────────


def test_scene_items_and_source_visibility():
    async def go():
        driver, sim, sockets = await _connected()
        lower_third = next(i for i in _children(driver, "scene_item")
                           if _child(driver, "scene_item", i, "source") == "Lower Third")
        await driver.send_command("show_scene_item", {"item": lower_third})
        await _settle()
        assert _child(driver, "scene_item", lower_third, "enabled") is True
        assert _child(driver, "input", "Lower_Third", "active") is True  # Camera is on program
        assert await driver.send_command("toggle_scene_item", {"item": lower_third}) is False
        await _settle()
        assert _child(driver, "scene_item", lower_third, "enabled") is False
        await driver.send_command("set_source_visible", {"scene": "Camera", "source": "Logo", "visible": False})
        await _settle()
        logo = next(i for i in _children(driver, "scene_item")
                    if _child(driver, "scene_item", i, "scene") == "Camera"
                    and _child(driver, "scene_item", i, "source") == "Logo")
        assert _child(driver, "scene_item", logo, "enabled") is False
        with pytest.raises(ValueError):
            await driver.send_command("set_source_visible", {"scene": "Camera", "source": "Break Loop", "visible": True})
        with pytest.raises(ValueError):
            await driver.send_command("hide_scene_item", {"item": "Camera__99"})
    _run(go())


def test_filters():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.send_command("enable_filter", {"filter": "Mic_Aux__Compressor"})
        await _settle()
        assert _child(driver, "filter", "Mic_Aux__Compressor", "enabled") is True
        assert sim.filters["Mic/Aux"][1]["enabled"] is True
        assert await driver.send_command("toggle_filter", {"filter": "Mic_Aux__Compressor"}) is False
        await _settle()
        assert _child(driver, "filter", "Mic_Aux__Compressor", "enabled") is False
        await driver.send_command("disable_filter", {"filter": "Camera__Color_Correction"})
        await _settle()
        assert _child(driver, "filter", "Camera__Color_Correction", "enabled") is False
    _run(go())


# ── Collections, profiles, hotkeys, UI ───────────────────────────────────────


def test_scene_collection_change_pauses_polling_and_resyncs():
    async def go():
        driver, sim, sockets = await _connected()
        # Change the fixture behind the collection so the resync is visible.
        sim.scenes.append("Q&A Wall")
        sim.items["Q&A Wall"] = []
        await driver.send_command("set_scene_collection", {"name": "Studio"})
        await _settle()
        assert _st(driver, "scene_collection") == "Studio"
        assert driver._collection_changing is False
        assert "Q_A_Wall" in _children(driver, "scene")
        assert _child(driver, "scene", "Q_A_Wall", "name") == "Q&A Wall"
        with pytest.raises(ValueError):
            await driver.send_command("set_scene_collection", {"name": "Nope"})
    _run(go())


def test_profile_hotkeys_projectors_screenshot_and_custom_event():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.send_command("set_profile", {"name": "Streaming"})
        await _settle()
        assert _st(driver, "profile") == "Streaming"
        await driver.send_command("start_stream", {})
        with pytest.raises(ValueError) as info:
            await driver.send_command("set_profile", {"name": "Default"})
        assert "output is active" in str(info.value)
        await driver.send_command("trigger_hotkey", {"name": "OBSBasic.Screenshot"})
        await driver.send_command("trigger_key_sequence", {"key": "OBS_KEY_F5", "shift": True})
        assert sim.hotkeys_triggered == ["OBSBasic.Screenshot", "OBS_KEY_F5"]
        await driver.send_command("open_projector", {"mix": "multiview", "monitor": "0"})
        await driver.send_command("open_source_projector", {"source": "Camera"})
        assert sim.projectors == [
            {"mix": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_MULTIVIEW", "monitor": 0},
            {"source": "Camera", "monitor": -1},
        ]
        await driver.send_command("save_screenshot", {"source": "Slides", "file_path": "/tmp/slides.JPG", "width": 640})
        assert sim.screenshots == [{"source": "Slides", "format": "jpeg", "path": "/tmp/slides.JPG", "width": 640, "height": None}]
        await driver.send_command("broadcast_custom_event", {"data": '{"cue": 12}'})
        assert sim.custom_events == [{"cue": 12}]
        with pytest.raises(ValueError):
            await driver.send_command("broadcast_custom_event", {"data": "not json"})
    _run(go())


# ── Changes made in OBS itself (events) ──────────────────────────────────────


def test_changes_made_in_obs_land_through_events():
    async def go():
        driver, sim, sockets = await _connected()
        await sim.apply_program_scene("Break")
        await _settle()
        assert _st(driver, "program_scene") == "Break"
        assert _child(driver, "input", "Break_Loop", "active") is True
        assert _child(driver, "input", "Camera", "active") is False
        await sim.apply_mute("Desktop Audio", False)
        await sim.apply_volume("Mic/Aux", 3.0)
        await _settle()
        assert _child(driver, "input", "Desktop_Audio", "muted") is False
        assert _child(driver, "input", "Mic_Aux", "volume_db") == 3.0
        # The Simulator UI's controls run the same paths.
        sim.set_state("program_scene", "Slides")
        sim.set_state("mic_muted", True)
        sim.set_state("streaming", True)
        await _settle()
        assert _st(driver, "program_scene") == "Slides"
        assert _child(driver, "input", "Mic_Aux", "muted") is True
        assert _st(driver, "streaming") is True
        assert _st(driver, "stream_state") == "started"
    _run(go())


def test_renames_creations_and_removals_follow_obs():
    async def go():
        driver, sim, sockets = await _connected()
        await sim.rename_input("Mic/Aux", "Lectern Mic")
        await _settle(0.1)
        inputs = _children(driver, "input")
        assert "Lectern_Mic" in inputs and "Mic_Aux" not in inputs
        assert _child(driver, "input", "Lectern_Mic", "volume_db") == -6.0
        assert _children(driver, "filter") == ["Camera__Color_Correction", "Lectern_Mic__Compressor", "Lectern_Mic__Noise_Suppression"]
        await sim.add_scene("Panel")
        await _settle(0.1)
        assert "Panel" in _children(driver, "scene")
        assert json.loads(_st(driver, "scene_options"))[0] == "Panel"
        await sim.remove_scene("Break")
        await _settle(0.1)
        assert "Break" not in _children(driver, "scene")
        assert not any(_child(driver, "scene_item", i, "scene") == "Break" for i in _children(driver, "scene_item"))
        assert _st(driver, "scene_count") == 3
        result = await driver.refresh_children()
        assert result["scenes"] == 3 and result["inputs"] == 8
    _run(go())


# ── Poll, device settings, liveness, disconnect ──────────────────────────────


def test_poll_reads_stats_and_media_positions():
    async def go():
        driver, sim, sockets = await _connected()
        sim.set_state("cpu_percent", 37.5)
        await sim.apply_media("Break Loop", "OBS_MEDIA_STATE_PLAYING", cursor=4200)
        await driver.poll()
        assert _st(driver, "cpu_percent") == 37.5
        assert _child(driver, "input", "Break_Loop", "media_cursor_ms") == 4200
        driver._collection_changing = True
        sim.set_state("cpu_percent", 50.0)
        await driver.poll()
        assert _st(driver, "cpu_percent") == 37.5
    _run(go())


def test_device_settings():
    async def go():
        driver, sim, sockets = await _connected()
        await driver.set_device_setting("studio_mode", True)
        await _settle()
        assert _st(driver, "studio_mode") is True
        await driver.set_device_setting("transition_duration_ms", 1200)
        await _settle()
        assert _st(driver, "transition_duration_ms") == 1200
        with pytest.raises(ValueError):
            await driver.set_device_setting("transition_duration_ms", 20)  # below OBS's 50 ms floor
        with pytest.raises(ValueError):
            await driver.set_device_setting("colour", "blue")
    _run(go())


def test_liveness_probe_and_clean_disconnect():
    async def go():
        driver, sim, sockets = await _connected()
        await driver._liveness_probe()
        await driver.disconnect()
        assert not driver.connected
        assert sockets[0].state.name == "CLOSED"
        assert driver.disconnect_events == 0  # graceful: no reconnect trigger
        with pytest.raises(ConnectionError):
            await driver.send_command("start_stream", {})
    _run(go())


def test_obs_not_ready_during_connect_is_a_retryable_connection_error(monkeypatch):
    async def go():
        driver, sim, fake_connect, sockets = _make()

        async def not_ready(*a, **k):
            raise DRV.ObsRequestError("GetSceneList", 207, "OBS is not ready to perform the request.")

        monkeypatch.setattr(driver, "_full_sync", not_ready)
        with pytest.raises(ConnectionError) as info:
            await _connect(driver, fake_connect)
        assert not isinstance(info.value, ConnectionFaultError)
        assert "not ready" in str(info.value)
        assert not driver.connected
    _run(go())


def test_a_dropped_socket_triggers_the_reconnect_path():
    async def go():
        driver, sim, sockets = await _connected()
        await sockets[0].server.close(4011, "Session invalidated.")
        await _settle()
        assert driver.disconnect_events == 1
        assert not driver.connected
        assert driver.stashed_fault[0] == "transport_disconnected"
    _run(go())
