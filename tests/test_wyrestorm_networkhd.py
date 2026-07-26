"""Driver + simulator tests for wyrestorm_networkhd (WyreStorm NetworkHD).

No NetworkHD hardware on hand, so correctness is proven two ways: metadata /
shape assertions on the driver, and **dual-proof round trips** wiring the
real driver to the real simulator over an in-memory transport — the
simulator renders the NetworkHD API v6.5 wire format (pretty-printed
multi-line JSON documents, matrix list blocks, command mirrors, documented
success/failure acks), the driver parses it, and results are asserted on
both sides (same approach as test_tvone_coriomatrix.py).

Covers the driver's Python-justified shapes:
  - device-enumerated roster: the multi-line devicejsonstring JSON array
    registers TX/RX children (alias ids, online flags, models) and
    reconciles adds/removes on later polls;
  - JSON block accumulation fanning device info / device status documents
    out into per-child state, with per-series field sets;
  - matrix list blocks and command-mirror acks folding into child routing
    state;
  - documented notifications: endpoint online/offline, video lost/found,
    cecinfo/irinfo, and the counted serialinfo consume (including a
    payload containing line endings);
  - the never-offline guard: two consecutive unanswered roster queries
    raise a typed no-response ConnectionError.

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

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "wyrestorm_networkhd.py"
SIM_PATH = REPO_ROOT / "switchers" / "wyrestorm_networkhd_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value

    def set_batch(self, updates, **_):
        self.data.update(updates)

    def delete(self, key, **_):
        self.data.pop(key, None)


class _FakeEvents:
    async def emit(self, name, *args, **kwargs):
        pass


class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver surface this driver
    uses: the hook-driven connect()/disconnect() lifecycle plus the
    child-entity registry (mirrors base.py semantics: unregistered children
    are skipped, state keys are namespaced)."""

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

    def register_child(self, child_type, local_id, initial_state=None, schema=None):
        self._children.setdefault(child_type, set())
        if local_id in self._children[child_type]:
            return
        self._children[child_type].add(local_id)
        for prop, value in (initial_state or {}).items():
            self.state.set(
                f"device.{self.device_id}.{child_type}.{local_id}.{prop}", value
            )

    def deregister_child(self, child_type, local_id):
        self._children.get(child_type, set()).discard(local_id)
        prefix = f"device.{self.device_id}.{child_type}.{local_id}."
        for key in [k for k in self.state.data if k.startswith(prefix)]:
            self.state.delete(key)

    def list_children(self, child_type):
        return sorted(self._children.get(child_type, set()))

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
            port=self.config.get("port", 23),
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
    """Stand-in for simulator.tcp_simulator.TCPSimulator — mirrors the real
    BaseSimulator semantics that matter: ``state`` returns a COPY, writes go
    through set_state."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self.pushed: list[bytes] = []

    @property
    def state(self):
        return dict(self._state)

    def set_state(self, key, value) -> None:
        self._state[key] = value

    async def push(self, data: bytes) -> None:
        self.pushed.append(data)


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport sends to the sim but DROPS the reply — a
# controller that accepts the connection and stays silent.
_SWALLOW = False


class _FakeTCPTransport:
    sent_lines: list[str] = []

    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._sim = _CURRENT_SIM

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
            await _deliver(self.on_data, resp)

    async def close(self) -> None:
        self.connected = False


async def _deliver(on_data, payload: bytes) -> None:
    """Deliver a reply the way the real transport does: framed on LF, one
    on_data call per line (the \\r stays attached, like the wire)."""
    for reply_line in payload.decode().split("\n"):
        if reply_line.strip("\r"):
            await on_data(reply_line.encode())


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


DRV = _load("wyrestorm_networkhd_under_test", DRIVER_PATH)
SIM = _load("wyrestorm_networkhd_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(connect=True):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    _FakeTCPTransport.sent_lines = []
    sim = SIM.WyrestormNetworkHDSimulator("sim1", {})
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.30", "port": 23, "poll_interval": 0}
    driver = DRV.WyrestormNetworkHDDriver("nhd1", cfg, _FakeState(), _FakeEvents())
    if connect:
        await driver.connect()
    return driver, sim


def _child(driver, ctype, cid, prop):
    return driver.state.data.get(f"device.nhd1.{ctype}.{cid}.{prop}")


def _dev(driver, key):
    return driver.state.data.get(f"device.nhd1.{key}")


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_and_platform_gate():
    info = DRV.WyrestormNetworkHDDriver.DRIVER_INFO
    assert info["version"] == "1.0.1"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    assert info["min_platform_version"] == "0.24.0"
    assert info["simulated"] is True
    assert info["source_url"].startswith("https://")
    for ctype in ("tx", "rx"):
        fmt = info["child_entity_types"][ctype]["id_format"]
        assert fmt["type"] == "string"


def test_actions_reference_real_commands():
    info = DRV.WyrestormNetworkHDDriver.DRIVER_INFO
    commands = set(info["commands"])
    for action in info["actions"]:
        assert action["id"] in commands, f"action {action['id']} has no command"
    for qa in info["quick_actions"]:
        assert qa in {a["id"] for a in info["actions"]}


def test_endpoint_params_carry_pickers():
    info = DRV.WyrestormNetworkHDDriver.DRIVER_INFO
    for cmd_id, cmd in info["commands"].items():
        for pname, p in cmd.get("params", {}).items():
            if p.get("type") == "child_id":
                assert p["child_type"] in ("tx", "rx"), (cmd_id, pname)
            if pname == "endpoint" and p.get("type") == "string":
                assert p.get("options_state") == "endpoint_options", (cmd_id, pname)


# ── Connect / roster ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_registers_roster():
    driver, sim = await _make_pair()
    # Session alias mode is forced on, first thing on the wire.
    assert _FakeTCPTransport.sent_lines[0] == "config set session alias on"
    assert _dev(driver, "api_version") == "6.5"
    assert _dev(driver, "system_version") == "8.3.1(v3.6.0)"

    assert driver.list_children("tx") == ["source1", "source2", "source3"]
    assert driver.list_children("rx") == ["display1", "display2", "display3"]
    assert _child(driver, "tx", "source1", "online") is True
    assert _child(driver, "tx", "source3", "online") is False
    assert _child(driver, "tx", "source1", "model") == "NHD-600-TX"
    assert _child(driver, "rx", "display2", "model") == "NHD-400-RX"
    assert _child(driver, "tx", "source1", "name") == "SOURCE1"
    assert _dev(driver, "tx_count") == 3
    assert _dev(driver, "tx_online") == 2
    assert _dev(driver, "rx_online") == 3

    # The roster document really is a multi-line pretty JSON block.
    raw = sim._device_json_string().decode()
    assert raw.count("\r\n") > 10 and "\t" in raw


@pytest.mark.asyncio
async def test_connect_poll_populates_status_and_matrix():
    driver, _sim = await _make_pair()
    # matrix get → all-media source per RX (as child ids).
    assert _child(driver, "rx", "display1", "source") == "source1"
    assert _child(driver, "rx", "display2", "source") == "source2"
    assert _child(driver, "rx", "display3", "source") == ""
    # device status fan-out, per-series fields.
    assert _child(driver, "tx", "source1", "signal") is True
    assert _child(driver, "tx", "source3", "signal") is False
    assert _child(driver, "tx", "source1", "resolution") == "1920x1080"
    assert _child(driver, "rx", "display1", "hdmi_out_active") is True
    assert _child(driver, "rx", "display1", "hdcp") == "hdcp14"
    assert _child(driver, "rx", "display2", "hdcp") == "hdcp22"
    assert _child(driver, "rx", "display3", "hdcp") == "hdcp14"  # "hdcp status"
    # device info fan-out (full-refresh path runs on cycle 0).
    assert _child(driver, "rx", "display2", "firmware") == "0.10.1" or \
        _child(driver, "rx", "display2", "firmware") == "v0.10.1"
    # breakaway views.
    assert _child(driver, "rx", "display2", "usb_source") == "source2"
    assert _child(driver, "rx", "display1", "video_source") == "source1"


@pytest.mark.asyncio
async def test_option_lists_published():
    driver, _sim = await _make_pair()
    endpoints = json.loads(_dev(driver, "endpoint_options"))
    assert len(endpoints) == 6
    assert {"value": "SOURCE1", "label": "SOURCE1 (TX)"} in endpoints
    scenes = [o["value"] for o in json.loads(_dev(driver, "scene_options"))]
    assert scenes == sorted(["OfficeVW-Splitmode", "OfficeVW-Combined"])
    wscenes = [o["value"] for o in json.loads(_dev(driver, "wscene_options"))]
    assert wscenes == ["OfficeVW-windows"]
    layouts = [o["value"] for o in json.loads(_dev(driver, "mscene_options"))]
    assert layouts == ["gridlayout", "piplayout"]
    vw = [o["value"] for o in json.loads(_dev(driver, "vw_options"))]
    assert vw == ["OfficeVW-Combined_TopTwo"]
    assert json.loads(_child(driver, "rx", "display3", "layouts")) == \
        ["gridlayout", "piplayout"]


@pytest.mark.asyncio
async def test_roster_reconcile_add_and_remove(monkeypatch):
    driver, sim = await _make_pair()
    assert driver.is_child_registered("rx", "display3")
    monkeypatch.delitem(SIM.ROSTER, "display3")
    monkeypatch.setitem(SIM.ROSTER, "display4",
                        ("NHD-500-RX-AABBCCDDEEFF", 400, "rx"))
    sim.set_state("online_display4", True)
    await driver.poll()
    assert not driver.is_child_registered("rx", "display3")
    assert driver.is_child_registered("rx", "display4")
    assert _child(driver, "rx", "display4", "model") == "NHD-500-RX"
    # The removed child's state keys are gone.
    assert _child(driver, "rx", "display3", "online") is None


# ── Routing ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_route_updates_both_sides():
    driver, sim = await _make_pair()
    ok = await driver.send_command("route", {"tx": "source2", "rx": "display1"})
    assert ok is True
    assert sim.state["matrix_display1"] == "source2"       # sim side
    assert _child(driver, "rx", "display1", "source") == "source2"  # mirror ack

    ok = await driver.send_command("route_off", {"rx": "display1"})
    assert ok is True
    assert sim.state["matrix_display1"] == ""
    assert _child(driver, "rx", "display1", "source") == ""


@pytest.mark.asyncio
async def test_breakaway_video_route_leaves_av_view():
    driver, sim = await _make_pair()
    ok = await driver.send_command("route_video", {"tx": "source2", "rx": "display1"})
    assert ok is True
    assert sim.state["matrix_video_display1"] == "source2"
    assert sim.state["matrix_display1"] == "source1"  # all-media untouched
    assert _child(driver, "rx", "display1", "video_source") == "source2"
    assert _child(driver, "rx", "display1", "source") == "source1"


@pytest.mark.asyncio
async def test_unknown_endpoint_raises_before_sending():
    driver, _sim = await _make_pair()
    sent_before = len(_FakeTCPTransport.sent_lines)
    with pytest.raises(ValueError, match="Unknown TX endpoint"):
        await driver.send_command("route", {"tx": "nope", "rx": "display1"})
    assert len(_FakeTCPTransport.sent_lines) == sent_before


# ── Scenes / video wall / multiview ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_scene_recall_success_and_failure():
    driver, _sim = await _make_pair()
    assert await driver.send_command(
        "scene_recall", {"scene": "OfficeVW-Splitmode"}) is True
    assert await driver.send_command(
        "scene_recall", {"scene": "NoSuchWall-Scene"}) is False


@pytest.mark.asyncio
async def test_wscene_and_windows():
    driver, _sim = await _make_pair()
    assert await driver.send_command(
        "wscene_recall", {"wscene": "OfficeVW-windows"}) is True
    assert await driver.send_command(
        "wscene_recall", {"wscene": "Bogus-windows"}) is False
    assert await driver.send_command("window_open", {
        "wscene": "OfficeVW-windows", "window": "w1",
        "x": 0, "y": 0, "width": 2, "height": 2, "tx": "source1"}) is True
    assert _FakeTCPTransport.sent_lines[-1] == \
        "wscene2 window open OfficeVW-windows w1 0 0 2 2 SOURCE1"
    assert await driver.send_command("window_layer", {
        "wscene": "OfficeVW-windows", "window": "w1", "layer": "top"}) is True


@pytest.mark.asyncio
async def test_multiview_layouts():
    driver, _sim = await _make_pair()
    assert await driver.send_command(
        "mview_layout", {"rx": "display3", "layout": "gridlayout"}) is True
    assert await driver.send_command(
        "mview_layout", {"rx": "display3", "layout": "nolayout"}) is False
    assert await driver.send_command("mview_tile_source", {
        "rx": "display3", "layout": "gridlayout", "tile": 1,
        "tx": "source1"}) is True
    assert await driver.send_command("mview_single", {
        "rx": "display3", "tx": "source2"}) is True
    assert await driver.send_command("mview_custom", {
        "rx": "display3", "mode": "tile",
        "tiles": "SOURCE1:0_0_960_540:fit SOURCE2:960_0_960_540:fit"}) is True


# ── Connected-device control ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_proxy_and_injection_wire_format():
    driver, _sim = await _make_pair()
    assert await driver.send_command(
        "sink_power", {"rx": "display2", "power": "on"}) is True
    assert _FakeTCPTransport.sent_lines[-1] == \
        "config set device sinkpower on DISPLAY2"
    assert await driver.send_command(
        "cec_power", {"rx": "display1", "power": "standby"}) is True
    assert _FakeTCPTransport.sent_lines[-1] == \
        "config set device cec standby DISPLAY1"
    assert await driver.send_command(
        "send_cec", {"rx": "display1", "data": "FF36"}) is True
    assert _FakeTCPTransport.sent_lines[-1] == 'cec "FF36" DISPLAY1'
    assert await driver.send_command("send_serial", {
        "endpoint": "SOURCE1", "data": "vol +80dB", "baud": "115200",
        "append_cr": True}) is True
    assert _FakeTCPTransport.sent_lines[-1] == \
        'serial -b 115200-8n1 -r on -n off -h off "vol +80dB" SOURCE1'
    assert await driver.send_command(
        "set_volume", {"endpoint": "DISPLAY2", "action": "down"}) is True
    assert _FakeTCPTransport.sent_lines[-1] == \
        "config set device audio volume down analog DISPLAY2"


@pytest.mark.asyncio
async def test_port_switch_commands():
    driver, _sim = await _make_pair()
    assert await driver.send_command(
        "set_video_port", {"tx": "source1", "port": "dp"}) is True
    assert _FakeTCPTransport.sent_lines[-1] == \
        "config set device videosource SOURCE1 dp"
    assert await driver.send_command(
        "set_audio_source", {"rx": "display1", "source": "dmix"}) is True
    assert await driver.send_command(
        "set_usb_mode", {"endpoint": "DISPLAY2", "kmoip": False}) is True
    assert _FakeTCPTransport.sent_lines[-1] == \
        "config set device info km_over_ip_enable=off DISPLAY2"
    assert await driver.send_command("set_infrared_mode", {
        "endpoint": "SOURCE1", "mode": "single", "target": "DISPLAY1"}) is True
    assert _FakeTCPTransport.sent_lines[-1] == \
        "matrix infrared2 set SOURCE1 single DISPLAY1"
    with pytest.raises(ValueError, match="needs a target"):
        await driver.send_command("set_serial_mode",
                                  {"endpoint": "SOURCE1", "mode": "single"})


# ── Notifications ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_endpoint_online_offline():
    driver, _sim = await _make_pair()
    await _deliver(driver.on_data_received, SIM.build_notify_endpoint("display1", False))
    assert _child(driver, "rx", "display1", "online") is False
    await _deliver(driver.on_data_received, SIM.build_notify_endpoint("display1", True))
    assert _child(driver, "rx", "display1", "online") is True


@pytest.mark.asyncio
async def test_notify_video_lost_found():
    driver, _sim = await _make_pair()
    await _deliver(driver.on_data_received, SIM.build_notify_video("lost", "source1"))
    assert _child(driver, "tx", "source1", "signal") is False
    await _deliver(driver.on_data_received,
                   SIM.build_notify_video("found", "display1", "source1"))
    assert _child(driver, "rx", "display1", "stream_active") is True


@pytest.mark.asyncio
async def test_notify_cec_and_ir_data():
    driver, _sim = await _make_pair()
    await _deliver(driver.on_data_received, SIM.build_notify_cec("display1", "FF36"))
    assert _child(driver, "rx", "display1", "cec_data") == "FF36"
    await _deliver(driver.on_data_received,
                   SIM.build_notify_ir("source1", "0000 0067 0000 0015"))
    assert _child(driver, "tx", "source1", "ir_data") == "0000 0067 0000 0015"


@pytest.mark.asyncio
async def test_notify_serialinfo_counted_single_line():
    driver, _sim = await _make_pair()
    await _deliver(driver.on_data_received,
                   SIM.build_notify_serial("display1", "ascii", "OK+VOL=12"))
    assert _child(driver, "rx", "display1", "serial_data") == "OK+VOL=12"


@pytest.mark.asyncio
async def test_notify_serialinfo_counted_multiline_payload():
    driver, _sim = await _make_pair()
    # A payload containing a line ending: infolen counts the CRLF, so the
    # counted consume must span two delivered lines.
    payload = "LINE-A\r\nLINE-B"
    await _deliver(driver.on_data_received,
                   SIM.build_notify_serial("source1", "ascii", payload))
    assert _child(driver, "tx", "source1", "serial_data") == payload
    # The parser is back to normal afterwards: a routing mirror still parses.
    await driver.on_data_received(b"matrix set source2 display1\r")
    assert _child(driver, "rx", "display1", "source") == "source2"


# ── Maintenance / errors ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reboots_and_raw_command():
    driver, _sim = await _make_pair()
    assert await driver.send_command(
        "reboot_device", {"endpoint": "DISPLAY2"}) is True
    assert await driver.send_command("reboot_controller", {}) is True
    out = await driver.send_command("raw_command", {"command": "config get devicelist"})
    assert "devicelist is" in out


@pytest.mark.asyncio
async def test_unknown_command_completes_as_failure(monkeypatch):
    monkeypatch.setattr(DRV, "REQUEST_TIMEOUT_S", 0.2)
    driver, _sim = await _make_pair()
    out = await driver.send_command("raw_command", {"command": "bogus verb"})
    assert out == "unknown command"


@pytest.mark.asyncio
async def test_connect_fails_cleanly_when_controller_silent(monkeypatch):
    global _SWALLOW
    monkeypatch.setattr(DRV, "REQUEST_TIMEOUT_S", 0.05)
    driver, _sim = await _make_pair(connect=False)
    _SWALLOW = True
    try:
        with pytest.raises(ConnectionError, match="No response from the NetworkHD API"):
            await driver.connect()
    finally:
        _SWALLOW = False
    # The failed handshake tore everything down: no leaked transport, no
    # connected flag, no dangling in-flight request.
    assert driver.transport is None
    assert driver._connected is False
    assert driver._pending is None


@pytest.mark.asyncio
async def test_never_offline_guard(monkeypatch):
    global _SWALLOW
    monkeypatch.setattr(DRV, "REQUEST_TIMEOUT_S", 0.05)
    driver, _sim = await _make_pair()
    _SWALLOW = True
    await driver.poll()  # first miss tolerated
    with pytest.raises(ConnectionError, match="not responding"):
        await driver.poll()  # second consecutive miss raises
    _SWALLOW = False
    await driver.poll()  # recovery resets the miss counter
    assert driver._roster_misses == 0


@pytest.mark.asyncio
async def test_refresh_children_counts():
    driver, _sim = await _make_pair()
    result = await driver.refresh_children()
    assert result == {"encoders": 3, "decoders": 3}
