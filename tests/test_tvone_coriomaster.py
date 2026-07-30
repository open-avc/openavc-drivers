"""Driver + simulator tests for tvone_coriomaster (tvONE CORIOmaster).

No CORIOmaster hardware on hand, so correctness is proven two ways: metadata
/ shape assertions on the driver, and **dual-proof round trips** wiring the
real driver to the real simulator over an in-memory transport — the
simulator renders the CORIOmax CLI (long-form property replies, !Done /
!Failed terminals, the documented login confirmation, and !Event pushes),
the driver parses it, and results are asserted on both sides (same approach
as test_tvone_coriomatrix.py).

Covers the driver's Python-justified shapes:
  - the login gate: success, rejected credential (typed auth wording),
    and a silent unit (no_response wording);
  - device-enumerated rosters: slot/port children, window/canvas children
    filtered by Status (FREE table slots are not registered), media-card
    inputs getting the playback props via a per-child dynamic schema;
  - the AddEvents subscription: armed on connect, !Event lines folding
    into window/canvas/output/input/media state, events never counting as
    request reply lines, unknown-window events triggering re-enumeration;
  - picker aggregation (presets, storyboards, inputs, media inputs,
    playlists);
  - the never-offline guard: two consecutive unanswered poll cycles raise
    (a hung controller keeps the TCP socket open), and a transport failure
    propagates.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the
stubs back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "video" / "tvone_coriomaster.py"
SIM_PATH = REPO_ROOT / "video" / "tvone_coriomaster_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver surface this driver
    uses: the hook-driven connect()/disconnect() lifecycle plus the
    child-entity registry (mirrors base.py semantics: unregistered children
    are skipped, state keys are namespaced, a per-child schema is only
    allowed for dynamic child types)."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._last_transport_error = ""
        self._last_fault = None
        self.disconnect_calls = 0
        self.stashed_fault: tuple[str, str] | None = None
        self._health_task = None
        self._bg_tasks: set = set()
        self._children: dict[str, set] = {}
        self._schemas: dict = {}

    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def get_state(self, key, default=None):
        return self.state.data.get(f"device.{self.device_id}.{key}", default)

    def _handle_transport_disconnect(self) -> None:
        # Mirrors the platform: flip the flags synchronously, then schedule
        # the async teardown (stop loops, close transport, _close_session,
        # disconnect event).
        self._connected = False
        self.set_state("connected", False)
        self.disconnect_calls += 1
        if self.transport is not None:
            self.transport.connected = False
        task = asyncio.ensure_future(self._on_disconnect_cleanup())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _on_disconnect_cleanup(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        transport = self.transport
        self.transport = None
        if transport is not None:
            await transport.close()
        await self._close_session()
        await self.events.emit(f"device.disconnected.{self.device_id}")

    # -- liveness watchdog: this driver supplies no probe (steady polling is
    # the keep-alive), so connect() never starts the loop. The raise flags a
    # future probe addition so the loop gets modeled here then. --

    def _start_health_loop(self) -> None:
        raise NotImplementedError(
            "driver grew a liveness probe - model the health loop here")

    def _stop_health_loop(self) -> None:
        self._health_task = None

    # ── Child registry (mirrors BaseDriver) ──

    def register_child(self, child_type, local_id, initial_state=None, schema=None):
        if schema is not None:
            type_def = self.DRIVER_INFO["child_entity_types"][child_type]
            assert type_def.get("dynamic"), (
                f"per-child schema for non-dynamic type {child_type!r}"
            )
        self._children.setdefault(child_type, set())
        if local_id in self._children[child_type]:
            return
        self._children[child_type].add(local_id)
        if schema is not None:
            self._schemas[(child_type, local_id)] = dict(schema)
        for prop, value in (initial_state or {}).items():
            self.state.set(
                f"device.{self.device_id}.{child_type}.{local_id}.{prop}", value
            )
        self.state.set(
            f"device.{self.device_id}.{child_type}.{local_id}.online", True
        )

    def deregister_child(self, child_type, local_id):
        self._children.get(child_type, set()).discard(local_id)
        prefix = f"device.{self.device_id}.{child_type}.{local_id}."
        for key in [k for k in self.state.data if k.startswith(prefix)]:
            self.state.delete(key)

    def list_children(self, child_type):
        return sorted(self._children.get(child_type, set()), key=str)

    def is_child_registered(self, child_type, local_id):
        return local_id in self._children.get(child_type, set())

    def set_child_state_batch(self, child_type, local_id, updates):
        if not self.is_child_registered(child_type, local_id):
            return
        for prop, value in updates.items():
            self.state.set(
                f"device.{self.device_id}.{child_type}.{local_id}.{prop}", value
            )

    # -- connection lifecycle (mirrors the platform's hook-driven connect) --

    async def _pre_connect(self) -> None:
        pass

    async def _post_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    async def _create_transport(self, transport_type) -> None:
        kwargs = dict(
            host=self.config.get("host", ""),
            port=self.config.get("port", 10001),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\r",
            frame_parser=self._create_frame_parser(),
            inter_command_delay=self.config.get("inter_command_delay", 0.0),
            timeout=self.config.get("timeout", 5.0),
            name=self.device_id,
        )
        self.transport = await _FakeTCPTransport.create(
            **self._transport_kwargs(transport_type, kwargs))

    async def connect(self) -> None:
        # 1. Clean slate: reset fault classification, drop a previous
        #    attempt's driver session and stale transport.
        self._last_transport_error = ""
        self._last_fault = None
        self.stashed_fault = None
        await self._close_session()
        if self.transport:
            await self.transport.close()
            self.transport = None
        # 2-3. Establish: pre-connect hook, then the transport.
        await self._pre_connect()
        await self._create_transport("tcp")
        # 4. Handshake: a raise here aborts the connection.
        try:
            await self._post_connect()
        except Exception:
            self._stash_transport_error()
            if self.transport:
                await self.transport.close()
                self.transport = None
            await self._close_session()
            self._connected = False
            raise
        # 5. Declare connected.
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        # 6. Initial sync: a raise here tears the connection back down.
        try:
            await self._initial_sync()
        except Exception:
            self._stash_transport_error()
            transport = self.transport
            self.transport = None
            if transport is not None:
                await transport.close()
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise
        # 7. Polling + liveness watchdog.
        if self.config.get("poll_interval", 0):
            await self.start_polling(self.config["poll_interval"])
        if self._health_enabled():
            self._start_health_loop()

    async def disconnect(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


class _FakeTCPSimulator:
    """Stand-in for simulator.tcp_simulator.TCPSimulator. Mirrors the real
    BaseSimulator semantics that matter: ``state`` returns a COPY, writes go
    through set_state. No push() — the sim guards for its absence."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

    @property
    def state(self):
        return dict(self._state)

    def set_state(self, key, value) -> None:
        self._state[key] = value


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport sends to the sim but DROPS the reply — a unit
# that accepts the connection and stays silent.
_SWALLOW = False


class _FakeTCPTransport:
    sent_lines: list[str] = []

    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._sim = _CURRENT_SIM
        # Events emitted by the sim during a command land here and are
        # delivered after the reply lines, like the asynchronous wire.
        self._pending_events: list[bytes] = []
        self._sim.on_event = self._pending_events.append

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, frame_parser=None,
                     inter_command_delay=0.0, timeout=5.0, name=""):
        return cls(on_data, on_disconnect)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        line = bytes(data).decode()
        _FakeTCPTransport.sent_lines.append(line.strip())
        resp = self._sim.handle_command(bytes(data))
        if resp and not _SWALLOW:
            # The real transport frames on \n and strips the delimiter —
            # deliver the reply one line per on_data call, like the wire.
            for reply_line in resp.decode().split("\r\n"):
                if reply_line:
                    await self.on_data(reply_line.encode())
        await self.drain_events()

    async def drain_events(self) -> None:
        while self._pending_events:
            for ev_line in self._pending_events.pop(0).decode().split("\r\n"):
                if ev_line:
                    await self.on_data(ev_line.encode())

    async def close(self) -> None:
        self.connected = False


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
    tcp = ModuleType("server.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    sys.modules["server.transport.tcp"] = tcp
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["simulator"] = sim_pkg
    sim_tcp = ModuleType("simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    sys.modules["simulator.tcp_simulator"] = sim_tcp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("tvone_coriomaster_under_test", DRIVER_PATH)
SIM = _load("tvone_coriomaster_sim_under_test", SIM_PATH)

_FakeBaseDriver.DRIVER_INFO = DRV.TvoneCoriomasterDriver.DRIVER_INFO


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, connect=True):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    _FakeTCPTransport.sent_lines = []
    sim = SIM.TvoneCoriomasterSimulator("sim1", {})
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.20",
        "port": 10001,
        "username": "admin",
        "password": "adminpw",
        "poll_interval": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.TvoneCoriomasterDriver("vw1", cfg, _FakeState(), _FakeEvents())
    if connect:
        await driver.connect()
    return driver, sim


def _child(driver, ctype, cid, prop):
    return driver.state.data.get(f"device.vw1.{ctype}.{cid}.{prop}")


async def _inject(driver, line: str) -> None:
    await driver.on_data_received(line.encode())


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_and_platform_gate():
    info = DRV.TvoneCoriomasterDriver.DRIVER_INFO
    assert info["version"] == "1.0.1"
    assert info["category"] == "video"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    assert info["min_platform_version"] == "0.24.0"
    assert info["ports"] == [10001]
    # No active probe on purpose: the CLI answers nothing documented
    # before login.
    assert "tcp_probe" not in info["discovery"]
    assert info["discovery"]["port_open"] == [10001]
    # Media inputs need a per-child schema, so the input type is dynamic.
    assert info["child_entity_types"]["input"].get("dynamic") is True
    assert info["child_entity_types"]["window"]["id_format"]["type"] == "integer"
    assert info["child_entity_types"]["canvas"]["id_format"]["type"] == "integer"


def test_actions_reference_real_commands():
    info = DRV.TvoneCoriomasterDriver.DRIVER_INFO
    commands = info["commands"]
    for action in info["actions"]:
        if action.get("kind") == "command":
            assert action["id"] in commands, action["id"]
    for qa in info["quick_actions"]:
        assert qa in commands, qa


def test_picker_params_reference_declared_state():
    info = DRV.TvoneCoriomasterDriver.DRIVER_INFO
    declared = set(info["state_variables"])
    for cmd_name, cmd in info["commands"].items():
        for pname, pdef in cmd.get("params", {}).items():
            opts = pdef.get("options_state")
            if opts:
                assert opts in declared, f"{cmd_name}.{pname} -> {opts}"


def test_device_setting_reads_back_through_declared_state():
    info = DRV.TvoneCoriomasterDriver.DRIVER_INFO
    ds = info["device_settings"]
    assert set(ds) == {"device_name"}
    assert ds["device_name"]["state_key"] in info["state_variables"]


def test_password_has_no_prefilled_default():
    info = DRV.TvoneCoriomasterDriver.DRIVER_INFO
    assert info["default_config"]["password"] == ""
    assert info["config_schema"]["password"]["secret"] is True


# ── Login gate ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_rejected_uses_auth_wording():
    with pytest.raises(ConnectionError) as exc:
        await _make_pair({"password": "invalid"})
    assert "authentication failed" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_login_silent_uses_no_response_wording(monkeypatch):
    global _SWALLOW
    monkeypatch.setattr(DRV, "LOGIN_TIMEOUT_S", 0.05)
    driver, sim = await _make_pair(connect=False)
    _SWALLOW = True
    with pytest.raises(ConnectionError) as exc:
        await driver.connect()
    assert "no response" in str(exc.value).lower()


# ── Rosters ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_registers_rosters_and_arms_events():
    driver, sim = await _make_pair()
    assert driver.list_children("input") == ["s4i1", "s4i2", "s5i1", "s5i2"]
    assert driver.list_children("output") == ["s13o1", "s13o2", "s14o1", "s14o2"]
    # Window 3 and canvas 2 are FREE table slots — not registered.
    assert driver.list_children("window") == [1, 2]
    assert driver.list_children("canvas") == [1]
    assert set(sim._event_categories) == set(DRV.EVENT_CATEGORIES)
    assert driver.get_state("model") == "CORIOmaster C3-540"


@pytest.mark.asyncio
async def test_media_inputs_get_playback_schema():
    driver, sim = await _make_pair()
    # Slot 5 is the Streaming Media card; its inputs carry the media props.
    assert ("input", "s5i1") in driver._schemas
    assert "media_status" in driver._schemas[("input", "s5i1")]
    assert ("input", "s4i1") not in driver._schemas
    assert _child(driver, "input", "s5i1", "media_status") == "Idle"
    assert _child(driver, "input", "s5i1", "media_item") == 1


@pytest.mark.asyncio
async def test_refresh_children_reconciles_removed_card_and_window():
    driver, sim = await _make_pair()
    del sim._cards[4]
    sim._windows[2] = ("FREE", "NULL", "NULL")
    result = await driver.refresh_children()
    assert driver.list_children("input") == ["s5i1", "s5i2"]
    assert driver.list_children("window") == [1]
    assert result["windows"] == 1 and result["inputs"] == 2
    assert "device.vw1.input.s4i1.status" not in driver.state.data


# ── Poll population ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_poll_populates_device_children_and_pickers():
    driver, sim = await _make_pair()
    # Device level.
    assert driver.get_state("status") == "Serving"
    assert driver.get_state("api_version") == "4.11.13075"
    assert driver.get_state("device_name") == "CORIOmaster"
    assert driver.get_state("standby") is False
    assert driver.get_state("active_preset") == 1
    assert driver.get_state("firmware_version") == "M411 Master"
    # Windows: dumps fold into child state; Alias becomes the name.
    assert _child(driver, "window", 1, "input") == "s4i1"
    assert _child(driver, "window", 1, "name") == "Presenter"
    assert _child(driver, "window", 1, "canvas") == "Canvas1"
    assert _child(driver, "window", 1, "width") == 1920
    assert _child(driver, "window", 2, "input") == "s5i1"
    assert _child(driver, "window", 2, "zorder") == 2
    # Canvas audio + storyboard state.
    assert _child(driver, "canvas", 1, "audio_volume") == 100
    assert _child(driver, "canvas", 1, "audio_mute") is False
    assert _child(driver, "canvas", 1, "audio_mode") == "FromSource"
    assert _child(driver, "canvas", 1, "audio_source") == "s4i1"
    assert _child(driver, "canvas", 1, "current_storyboard") == ""
    assert "Window1" in _child(driver, "canvas", 1, "window_list")
    assert _child(driver, "canvas", 1, "name") == "MainWall"
    # Ports.
    assert _child(driver, "input", "s4i1", "status") == "OK"
    assert _child(driver, "input", "s4i2", "status") == "INVALID"
    assert _child(driver, "input", "s4i1", "resolution") == "1920x1080p60"
    assert _child(driver, "output", "s13o1", "cut_to_black") is False
    assert "s4i1" in _child(driver, "output", "s13o1", "source_list")
    # Pickers.
    presets = json.loads(driver.get_state("preset_options"))
    assert [p["value"] for p in presets] == ["1", "2"]
    assert presets[0]["label"] == "1: start"
    stbds = json.loads(driver.get_state("stbd_options"))
    assert [s["value"] for s in stbds] == ["1"]  # unnamed Stbd2 skipped
    inputs = json.loads(driver.get_state("input_options"))
    assert len(inputs) == 4
    media = json.loads(driver.get_state("media_input_options"))
    assert [m["value"] for m in media] == ["s5i1", "s5i2"]
    playlists = json.loads(driver.get_state("playlist_options"))
    assert playlists == [
        {"value": "Resources.Playlists.Playlist1", "label": "Loop_A"}
    ]


# ── Command round trips (both sides asserted) ───────────────────────────────

@pytest.mark.asyncio
async def test_set_window_input_round_trip():
    driver, sim = await _make_pair()
    ok = await driver.send_command(
        "set_window_input", {"window": 1, "input": "s5i2"}
    )
    assert ok is True
    assert "Window1.Input = Slot5.In2" in _FakeTCPTransport.sent_lines
    assert sim.state["win_1_input"] == "s5i2"
    assert _child(driver, "window", 1, "input") == "s5i2"


@pytest.mark.asyncio
async def test_window_geometry_round_trips():
    driver, sim = await _make_pair()
    assert await driver.send_command(
        "set_window_position", {"window": 1, "x": -100, "y": 250}
    )
    assert sim.state["win_1_x"] == -100 and sim.state["win_1_y"] == 250
    assert _child(driver, "window", 1, "x") == -100
    assert _child(driver, "window", 1, "y") == 250
    assert await driver.send_command(
        "set_window_size", {"window": 1, "width": 960, "height": 540}
    )
    assert sim.state["win_1_width"] == 960
    assert _child(driver, "window", 1, "height") == 540
    assert await driver.send_command("set_window_zorder", {"window": 1, "zorder": 5})
    assert _child(driver, "window", 1, "zorder") == 5
    assert await driver.send_command("set_window_ftb", {"window": 1, "level": 256})
    assert sim.state["win_1_ftb"] == 256
    assert _child(driver, "window", 1, "ftb") == 256


@pytest.mark.asyncio
async def test_recall_preset_applies_wall_and_event_updates_state():
    driver, sim = await _make_pair()
    ok = await driver.send_command("recall_preset", {"preset": 2})
    assert ok is True
    # Sim rerouted the wall; the PRESET,TAKE event and the re-read both
    # land on the driver.
    assert sim.state["active_preset"] == 2
    assert driver.get_state("active_preset") == 2
    assert sim.state["win_1_input"] == "s5i2"
    assert _child(driver, "window", 1, "input") == "s5i2"
    assert _child(driver, "window", 2, "input") == "s4i2"


@pytest.mark.asyncio
async def test_recall_unknown_preset_answers_failed():
    driver, sim = await _make_pair()
    ok = await driver.send_command("recall_preset", {"preset": 30})
    assert ok is False
    assert driver.get_state("active_preset") == 1


@pytest.mark.asyncio
async def test_save_preset_flow_and_picker_refresh():
    driver, sim = await _make_pair()
    ok = await driver.send_command("save_preset", {"preset": 5, "name": "lobby"})
    assert ok is True
    assert sim._presets[5]["name"] == "lobby"
    assert sim._presets[5]["windows"] == {1: "s4i1", 2: "s5i1"}
    presets = json.loads(driver.get_state("preset_options"))
    assert {"value": "5", "label": "5: lobby"} in presets


@pytest.mark.asyncio
async def test_take_storyboard_updates_canvas():
    driver, sim = await _make_pair()
    ok = await driver.send_command("take_storyboard", {"storyboard": 1})
    assert ok is True
    assert "Stbds.Stbd1.Take()" in _FakeTCPTransport.sent_lines
    assert sim.state["cv_1_stbd"] == "Stbd1"
    # STBDCURRENT_CHANGED event folded into the canvas child.
    assert _child(driver, "canvas", 1, "current_storyboard") == "Stbd1"


@pytest.mark.asyncio
async def test_canvas_audio_round_trips():
    driver, sim = await _make_pair()
    assert await driver.send_command(
        "set_canvas_volume", {"canvas": 1, "volume": 55}
    )
    assert sim.state["cv_1_volume"] == 55
    assert _child(driver, "canvas", 1, "audio_volume") == 55
    assert await driver.send_command("canvas_mute_on", {"canvas": 1})
    assert sim.state["cv_1_mute"] is True
    assert _child(driver, "canvas", 1, "audio_mute") is True
    assert await driver.send_command("canvas_mute_off", {"canvas": 1})
    assert _child(driver, "canvas", 1, "audio_mute") is False
    assert await driver.send_command(
        "set_canvas_audio_mode", {"canvas": 1, "mode": "FollowWindow"}
    )
    assert _child(driver, "canvas", 1, "audio_mode") == "FollowWindow"
    assert await driver.send_command(
        "set_canvas_audio_window", {"canvas": 1, "window": 2}
    )
    assert sim.state["cv_1_follow"] == 2
    assert _child(driver, "canvas", 1, "audio_follow_window") == 2
    assert await driver.send_command(
        "set_canvas_audio_source", {"canvas": 1, "source": "s4i2"}
    )
    assert sim.state["cv_1_source"] == "Slot4.In2"
    assert _child(driver, "canvas", 1, "audio_source") == "s4i2"


@pytest.mark.asyncio
async def test_cut_to_black_round_trip():
    driver, sim = await _make_pair()
    assert await driver.send_command("cut_to_black_on", {"output": "s13o1"})
    assert sim.state["ctb_13_1"] is True
    assert _child(driver, "output", "s13o1", "cut_to_black") is True
    assert await driver.send_command("cut_to_black_off", {"output": "s13o1"})
    assert _child(driver, "output", "s13o1", "cut_to_black") is False


@pytest.mark.asyncio
async def test_media_transport_round_trips():
    driver, sim = await _make_pair()
    assert await driver.send_command("media_play", {"input": "s5i1"})
    assert sim.state["mq_5_1_status"] == "Playing"
    # MEDIA_PLAYER,STATUS_UPDATE event folded into the media child.
    assert _child(driver, "input", "s5i1", "media_status") == "Playing"
    assert await driver.send_command("media_skip_forward", {"input": "s5i1"})
    assert _child(driver, "input", "s5i1", "media_item") == 2
    assert await driver.send_command("media_pause", {"input": "s5i1"})
    assert _child(driver, "input", "s5i1", "media_status") == "Paused"
    assert await driver.send_command("media_stop", {"input": "s5i1"})
    assert _child(driver, "input", "s5i1", "media_status") == "Idle"
    assert await driver.send_command(
        "media_load_playlist",
        {"input": "s5i1", "playlist": "Resources.Playlists.Playlist1"},
    )
    assert (
        'Slot5.In1.ActiveQueue.LoadPlayList("Resources.Playlists.Playlist1")'
        in _FakeTCPTransport.sent_lines
    )
    assert await driver.send_command(
        "media_play_mode", {"input": "s5i1", "mode": "Repeat"}
    )
    assert sim.state["mq_5_1_mode"] == "Repeat"


@pytest.mark.asyncio
async def test_media_command_rejects_non_media_input():
    driver, sim = await _make_pair()
    with pytest.raises(ValueError):
        await driver.send_command("media_play", {"input": "s4i1"})


@pytest.mark.asyncio
async def test_standby_round_trip():
    driver, sim = await _make_pair()
    assert await driver.send_command("standby_on", {})
    assert sim.state["standby"] is True
    assert driver.get_state("standby") is True
    assert await driver.send_command("standby_off", {})
    assert driver.get_state("standby") is False


@pytest.mark.asyncio
async def test_device_name_setting_round_trip():
    driver, sim = await _make_pair()
    await driver.set_device_setting("device_name", "Lobby Wall")
    assert sim.state["unit_description"] == "Lobby Wall"
    assert driver.get_state("device_name") == "Lobby Wall"


@pytest.mark.asyncio
async def test_raw_command_returns_reply_lines():
    driver, sim = await _make_pair()
    out = await driver.send_command("raw_command", {"command": "System.Status"})
    assert "System.Status = Serving" in out
    assert "!Done" in out


# ── Event handling ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_input_status_event_folds_into_child():
    driver, sim = await _make_pair()
    await _inject(driver, "!Event INPUT,STATUS_GROUP,Slot4.In1,Status,INVALID")
    assert _child(driver, "input", "s4i1", "status") == "INVALID"
    await _inject(
        driver,
        "!Event INPUT,STATUS_GROUP,Slot4.In1,Measured_Resolution,3840x2160p30",
    )
    assert _child(driver, "input", "s4i1", "resolution") == "3840x2160p30"


@pytest.mark.asyncio
async def test_event_without_category_prefix_parses():
    # Some module tables print events without the category token.
    driver, sim = await _make_pair()
    await _inject(driver, "!Event STATUS_GROUP,Slot4.In1,Status,INVALID")
    assert _child(driver, "input", "s4i1", "status") == "INVALID"


@pytest.mark.asyncio
async def test_event_tokens_tolerate_spaces():
    # The doc's own examples carry stray spaces: '!Event PRESET, COMPLETE,1'.
    driver, sim = await _make_pair()
    await _inject(driver, "!Event PRESET, TAKE, 2")
    assert driver.get_state("active_preset") == 2


@pytest.mark.asyncio
async def test_events_never_count_as_reply_lines():
    driver, sim = await _make_pair()
    # A window-input write makes the sim emit WINDOW,INPUT mid-exchange;
    # the request's reply must still be just its own echo.
    ok, lines = await driver._request("Window1.Input = Slot4.In2")
    assert ok is True
    assert not any(ln.startswith("!Event") for ln in lines)
    # The event still landed in state.
    assert _child(driver, "window", 1, "input") == "s4i2"


@pytest.mark.asyncio
async def test_unknown_window_event_triggers_reenumeration():
    driver, sim = await _make_pair()
    # The unit reports a source change on a window we don't know (built
    # after connect) — the driver marks the roster dirty and the next poll
    # re-enumerates.
    sim._windows[3] = ("IN USE", "NewWin", "Canvas1")
    sim.set_state("win_3_input", "s4i1")
    for key, value in (
        ("win_3_x", 0), ("win_3_y", 0), ("win_3_width", 640),
        ("win_3_height", 360), ("win_3_zorder", 3), ("win_3_ftb", 0),
    ):
        sim.set_state(key, value)
    await _inject(driver, "!Event WINDOW,INPUT,Window3,Slot4.In1")
    assert 3 not in driver.list_children("window")
    await driver.poll()
    assert 3 in driver.list_children("window")
    assert _child(driver, "window", 3, "input") == "s4i1"
    assert _child(driver, "window", 3, "name") == "NewWin"


@pytest.mark.asyncio
async def test_preset_save_event_refreshes_picker_on_next_poll():
    driver, sim = await _make_pair()
    sim._presets[7] = {"name": "added_outside", "windows": {1: "s4i1"}}
    await _inject(driver, "!Event PRESET,SAVE,7")
    assert driver._presets_dirty is True
    await driver.poll()  # steady cycle — dirty flag forces the refresh
    presets = json.loads(driver.get_state("preset_options"))
    assert {"value": "7", "label": "7: added_outside"} in presets


@pytest.mark.asyncio
async def test_out_of_band_change_seen_by_event_and_poll():
    driver, sim = await _make_pair()
    # A change made from another surface (CORIOgrapher on RS-232, front
    # panel): the sim pushes the documented event; the driver folds it in
    # without any request in flight.
    sim.set_state("win_2_input", "s4i1")
    sim.emit_window_input(2, "Slot4.In1")
    await driver.transport.drain_events()
    assert _child(driver, "window", 2, "input") == "s4i1"


# ── Never-offline guards ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_silent_unit_raises_after_two_unanswered_cycles(monkeypatch):
    global _SWALLOW
    driver, sim = await _make_pair()
    monkeypatch.setattr(DRV, "REQUEST_TIMEOUT_S", 0.02)
    _SWALLOW = True
    await driver.poll()  # first fully-unanswered cycle tolerated
    with pytest.raises(ConnectionError) as exc:
        await driver.poll()
    assert "not responding" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_recovery_resets_miss_counter(monkeypatch):
    global _SWALLOW
    driver, sim = await _make_pair()
    monkeypatch.setattr(DRV, "REQUEST_TIMEOUT_S", 0.02)
    _SWALLOW = True
    await driver.poll()
    _SWALLOW = False
    await driver.poll()  # answered — counter resets
    _SWALLOW = True
    await driver.poll()  # first miss again, no raise
    assert driver._poll_misses == 1


@pytest.mark.asyncio
async def test_poll_propagates_transport_failure():
    driver, sim = await _make_pair()
    driver.transport.connected = False
    with pytest.raises(ConnectionError):
        await driver.poll()
