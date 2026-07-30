"""Driver + simulator tests for avproedge_mxnet_1g (AVPro Edge MXNet 1G CBOX).

No MXNet hardware on hand, so correctness is proven two ways: metadata / shape
assertions on the driver, and dual-proof round trips wiring the real driver to
the real simulator through an in-memory transport that runs the driver's own
frame parser — the simulator renders the CBOX's JSON replies, the driver parses
them, and results are asserted on both sides (same approach as
test_tvone_coriomatrix.py).

Covers the driver's Python-justified shapes:
  - brace-balanced JSON framing, including two objects arriving in one read and
    an object split across reads (the API doc never states a terminator);
  - device-enumerated roster: `config get devicelist` splits into encoder and
    decoder children on `is_host`, and refresh_children reconciles a removal;
  - one broad reply fanning out into N children x M props (`device status ALL`,
    `device routes vaurs ALLRX`), with routes resolved from the CBOX's source
    NAMES back to the source encoder's child id;
  - the unsolicited RS-232 frame (empty `cmd`, "source":"rs232") landing on the
    right endpoint's serial_data, decoded per the configured encapsulation;
  - device settings writing through and mirroring back;
  - error replies (code -1) surfacing as a raised ValueError;
  - the never-offline guard: poll propagates a silent CBOX as a ConnectionError.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected).
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    CallableFrameParser as _FakeCallableFrameParser,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "avproedge_mxnet_1g.py"
SIM_PATH = REPO_ROOT / "switchers" / "avproedge_mxnet_1g_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver surface this driver
    uses: the hook-driven connect()/disconnect() lifecycle plus the
    child-entity registry (mirrors base.py semantics: writes to an
    unregistered child are skipped, state keys are namespaced)."""

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

    def set_states(self, updates) -> None:
        for key, value in updates.items():
            self.set_state(key, value)

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
        self._children.setdefault(child_type, set())
        if local_id in self._children[child_type]:
            return
        self._children[child_type].add(local_id)
        for prop, value in (initial_state or {}).items():
            self.state.set(f"device.{self.device_id}.{child_type}.{local_id}.{prop}", value)
        self.state.set(f"device.{self.device_id}.{child_type}.{local_id}.online", True)

    def deregister_child(self, child_type, local_id):
        self._children.get(child_type, set()).discard(local_id)
        prefix = f"device.{self.device_id}.{child_type}.{local_id}."
        for key in [k for k in self.state.data if k.startswith(prefix)]:
            self.state.delete(key)

    def list_children(self, child_type):
        return sorted(self._children.get(child_type, set()))

    def is_child_registered(self, child_type, local_id):
        return local_id in self._children.get(child_type, set())

    def set_child_state(self, child_type, local_id, prop, value):
        self.set_child_state_batch(child_type, local_id, {prop: value})

    def set_child_state_batch(self, child_type, local_id, updates):
        if not self.is_child_registered(child_type, local_id):
            return
        for prop, value in updates.items():
            self.state.set(f"device.{self.device_id}.{child_type}.{local_id}.{prop}", value)

    def set_children_state_batch(self, entries):
        for child_type, local_id, updates in entries:
            self.set_child_state_batch(child_type, local_id, updates)

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
            port=self.config.get("port", 24),
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
    through set_state, and push() reaches the connected client."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self.pushed: list[bytes] = []
        self.on_push = None

    @property
    def state(self):
        return dict(self._state)

    def set_state(self, key, value) -> None:
        self._state[key] = value

    async def push(self, data: bytes) -> None:
        self.pushed.append(data)
        if self.on_push is not None:
            await self.on_push(data)


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport sends to the sim but DROPS the reply — a CBOX that
# accepts the connection and then goes silent.
_SWALLOW = False
# When True, replies are delivered one byte at a time, proving the frame parser
# reassembles an object split across reads.
_DRIP = False


class _FakeTCPTransport:
    sent_lines: list[str] = []

    def __init__(self, on_data, on_disconnect, frame_parser) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._sim = _CURRENT_SIM
        self._parser = frame_parser

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     frame_parser=None, delimiter=None,
                     inter_command_delay=0.0, timeout=5.0, name=""):
        transport = cls(on_data, on_disconnect, frame_parser)
        if _CURRENT_SIM is not None:
            _CURRENT_SIM.on_push = transport._deliver
        return transport

    async def _deliver(self, raw: bytes) -> None:
        """Feed device bytes through the driver's own frame parser, like the wire."""
        chunks = [raw[i : i + 1] for i in range(len(raw))] if _DRIP else [raw]
        for chunk in chunks:
            for frame in self._parser.feed(chunk):
                await self.on_data(frame)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        _FakeTCPTransport.sent_lines.append(bytes(data).decode().strip())
        resp = self._sim.handle_command(bytes(data))
        if resp and not _SWALLOW:
            await self._deliver(resp)

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
    parsers = ModuleType("server.transport.frame_parsers")
    parsers.CallableFrameParser = _FakeCallableFrameParser
    sys.modules["server.transport.frame_parsers"] = parsers
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


DRV = _load("avproedge_mxnet_1g_under_test", DRIVER_PATH)
SIM = _load("avproedge_mxnet_1g_sim_under_test", SIM_PATH)

# The simulator's roster (see the sim's ENDPOINTS table).
TX_APPLE = "188A6A0067A2"
TX_LAPTOP = "188A6A0F4485"
TX_CABLE = "188A6ACE87DC"
RX_LOBBY = "188A6A0102A8"
RX_BAR_L = "188A6A45C4A5"
RX_BOARD = "188A6A1887E3"


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, connect=True, drip=False, swallow=False):
    global _CURRENT_SIM, _SWALLOW, _DRIP
    _SWALLOW = swallow
    _DRIP = drip
    _FakeTCPTransport.sent_lines = []
    sim = SIM.AVProEdgeMXNet1GSimulator("sim1", {})
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.30", "port": 24, "poll_interval": 0}
    cfg.update(driver_overrides or {})
    driver = DRV.AVProEdgeMXNet1GDriver("mx1", cfg, _FakeState(), _FakeEvents())
    if connect:
        await driver.connect()
    return driver, sim


def _dev(driver, prop):
    return driver.state.data.get(f"device.mx1.{prop}")


def _child(driver, ctype, cid, prop):
    return driver.state.data.get(f"device.mx1.{ctype}.{cid}.{prop}")


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata():
    info = DRV.AVProEdgeMXNet1GDriver.DRIVER_INFO
    assert info["id"] == "avproedge_mxnet_1g"
    assert info["manufacturer"] == "AVPro Edge"
    assert info["transport"] == "tcp"
    assert info["ports"] == [24]
    assert info["version"] == "1.0.2"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    assert info["min_platform_version"] == "0.24.0"
    assert info["source_url"].startswith("https://support.avproglobal.com")
    # String-id children (MACs) from a device-enumerated roster.
    for ctype in ("encoder", "decoder"):
        assert info["child_entity_types"][ctype]["id_format"]["type"] == "string"
        assert info["child_entity_types"][ctype]["label_field"] == "name"


def test_quick_actions_and_pickers_resolve():
    info = DRV.AVProEdgeMXNet1GDriver.DRIVER_INFO
    commands = info["commands"]
    state_vars = info["state_variables"]

    for action in info["quick_actions"]:
        assert action in commands, f"quick action {action} has no command"
    for action in info["actions"]:
        if action["kind"] == "command":
            assert action["id"] in commands

    # Every dropdown must point at a state var the driver actually publishes.
    for name, spec in commands.items():
        for param, pspec in spec.get("params", {}).items():
            src = pspec.get("options_state")
            if src:
                assert src in state_vars, f"{name}.{param} -> unknown state var {src}"
            ctype = pspec.get("child_type")
            if ctype:
                assert ctype in info["child_entity_types"]


def test_device_settings_have_backing_state():
    info = DRV.AVProEdgeMXNet1GDriver.DRIVER_INFO
    for name, spec in info["device_settings"].items():
        assert spec["state_key"] in info["state_variables"], name


# ── Frame parser ────────────────────────────────────────────────────────────

def test_json_framing_handles_split_and_batched_objects():
    frame = DRV._json_frame

    # Incomplete object: wait for more bytes, keep the buffer.
    msg, rest = frame(b'{"cmd":"config get na')
    assert msg is None and rest == b'{"cmd":"config get na'

    # Two objects in one read: the first comes out, the second stays buffered.
    two = b'{"code":0,"cmd":"a"}{"code":0,"cmd":"b"}'
    msg, rest = frame(two)
    assert json.loads(msg)["cmd"] == "a"
    msg, rest = frame(rest)
    assert json.loads(msg)["cmd"] == "b"

    # A brace inside a string must not close the object.
    tricky = b'{"info":"a}b\\"c{","code":0}'
    msg, rest = frame(tricky)
    assert json.loads(msg)["info"] == 'a}b"c{'
    assert rest == b""

    # Inter-object noise (the CRLF the sim sends) is consumed as an EMPTY frame
    # with the trimmed remainder. (None, trimmed) would also work — the parser
    # keeps whatever buffer the parse_fn returns — but this driver pins the
    # empty-frame form.
    msg, rest = frame(b'\r\n{"code":0}')
    assert msg == b"" and rest == b'{"code":0}'


# ── Roster ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_enumerates_roster_into_two_child_types():
    driver, _sim = await _make_pair()

    assert driver.list_children("encoder") == sorted([TX_APPLE, TX_LAPTOP, TX_CABLE])
    assert driver.list_children("decoder") == sorted(
        [RX_LOBBY, RX_BAR_L, "188A6A45C4A6", RX_BOARD]
    )
    assert _dev(driver, "encoder_count") == 3
    assert _dev(driver, "decoder_count") == 4
    assert _dev(driver, "model") == "AC-MXNET-CBOX"

    # Names, not MACs, are the label; MAC is the identity.
    assert _child(driver, "encoder", TX_APPLE, "name") == "Apple-TV"
    assert _child(driver, "decoder", RX_LOBBY, "name") == "Lobby-Display"

    # An endpoint the CBOX lists as not-yet-attached stays registered, offline.
    assert _child(driver, "decoder", RX_BOARD, "online") is False
    assert _dev(driver, "offline_endpoints") == 1


@pytest.mark.asyncio
async def test_endpoint_options_feed_the_pickers():
    driver, _sim = await _make_pair()

    encoders = json.loads(_dev(driver, "encoder_options"))
    assert {"value": TX_APPLE, "label": "Apple-TV"} in encoders

    everything = json.loads(_dev(driver, "endpoint_options"))
    labels = {opt["value"]: opt["label"] for opt in everything}
    assert labels[TX_APPLE] == "Apple-TV (Encoder)"
    assert labels[RX_LOBBY] == "Lobby-Display (Decoder)"


@pytest.mark.asyncio
async def test_refresh_children_reconciles_a_removed_endpoint():
    driver, sim = await _make_pair()
    assert RX_BAR_L in driver.list_children("decoder")

    sim._eps.pop(RX_BAR_L)
    sim._routes.pop(RX_BAR_L)
    summary = await driver.refresh_children()

    assert RX_BAR_L not in driver.list_children("decoder")
    assert summary == {"encoders": 3, "decoders": 3}
    assert _child(driver, "decoder", RX_BAR_L, "name") is None


# ── Broad-reply fan-out ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_all_fans_out_into_every_child():
    driver, _sim = await _make_pair()
    await driver.poll()

    # Encoder with a live source vs one without (sim: Cable-Box has no signal).
    assert _child(driver, "encoder", TX_APPLE, "signal_present") is True
    assert _child(driver, "encoder", TX_APPLE, "resolution") == "3840X2160p/60Hz"
    assert _child(driver, "encoder", TX_APPLE, "hdr") is True
    assert _child(driver, "encoder", TX_CABLE, "signal_present") is False

    # Decoder side: display identity + hot-plug, from the same single reply.
    assert _child(driver, "decoder", RX_LOBBY, "display_name") == "65Q825"
    assert _child(driver, "decoder", RX_LOBBY, "display_connected") is True
    assert _child(driver, "decoder", RX_LOBBY, "switch_ip") == "192.168.1.50"


@pytest.mark.asyncio
async def test_routes_reply_resolves_source_names_to_child_ids():
    driver, _sim = await _make_pair()
    await driver.poll()

    # The CBOX answers with source NAMES; state carries the encoder's child id.
    assert _child(driver, "decoder", RX_LOBBY, "source_video") == TX_APPLE
    assert _child(driver, "decoder", RX_BAR_L, "source_video") == TX_LAPTOP
    assert _child(driver, "decoder", RX_BOARD, "source_video") == ""


@pytest.mark.asyncio
async def test_named_lists_populate_the_recall_pickers():
    driver, _sim = await _make_pair()
    await driver.poll()

    assert json.loads(_dev(driver, "preset_options")) == [
        {"value": "AllHands", "label": "AllHands"},
        {"value": "Lunch", "label": "Lunch"},
    ]
    assert json.loads(_dev(driver, "videowall_options"))[0]["value"] == "BarWall"
    assert _dev(driver, "firmware") == "2.28"
    assert _dev(driver, "lan_ip") == "192.168.1.239"
    assert _dev(driver, "ntp_servers").startswith("0.north-america")


# ── Routing commands ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_route_all_moves_every_stream_and_polls_back():
    driver, sim = await _make_pair()
    await driver.send_command("route", {"tx": TX_CABLE, "rx": RX_LOBBY, "stream": "all"})

    # Sim side: every stream followed.
    assert sim._routes[RX_LOBBY] == {s: TX_CABLE for s in SIM.STREAMS}
    # Driver side: the next poll reads it back (no fabricated state).
    await driver.poll()
    assert _child(driver, "decoder", RX_LOBBY, "source_video") == TX_CABLE
    assert _child(driver, "decoder", RX_LOBBY, "source_audio") == TX_CABLE
    assert _child(driver, "decoder", RX_LOBBY, "source_serial") == TX_CABLE


@pytest.mark.asyncio
async def test_route_one_stream_leaves_the_others_alone():
    driver, sim = await _make_pair()
    await driver.send_command("route", {"tx": TX_CABLE, "rx": RX_LOBBY, "stream": "audio"})

    assert sim._routes[RX_LOBBY]["audio"] == TX_CABLE
    assert sim._routes[RX_LOBBY]["video"] == TX_APPLE
    assert "config set device audiopath" in _FakeTCPTransport.sent_lines[-1]


@pytest.mark.asyncio
async def test_route_off_all_clears_every_stream():
    driver, sim = await _make_pair()
    await driver.send_command("route_off", {"rx": RX_BAR_L, "stream": "all"})

    assert sim._routes[RX_BAR_L] == {s: "" for s in SIM.STREAMS}
    await driver.poll()
    assert _child(driver, "decoder", RX_BAR_L, "source_video") == ""


@pytest.mark.asyncio
async def test_route_rejects_an_unknown_endpoint():
    driver, _sim = await _make_pair()
    with pytest.raises(ValueError, match="Unknown decoder"):
        await driver.send_command("route", {"tx": TX_APPLE, "rx": "NOPE"})
    # An encoder passed where a decoder belongs is caught by the type check.
    with pytest.raises(ValueError, match="Unknown decoder"):
        await driver.send_command("route", {"tx": TX_APPLE, "rx": TX_LAPTOP})


@pytest.mark.asyncio
async def test_recall_preset_and_force_flag():
    driver, _sim = await _make_pair()
    await driver.send_command("recall_preset", {"name": "AllHands", "force": True})
    assert _FakeTCPTransport.sent_lines[-1] == "matrix preset active AllHands force"

    with pytest.raises(ValueError, match="rejected"):
        await driver.send_command("recall_preset", {"name": "Nope"})


# ── Endpoint commands ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_display_controls_write_through_and_mirror():
    driver, sim = await _make_pair()

    await driver.send_command("set_blackout", {"rx": RX_LOBBY, "state": "on"})
    assert sim._eps[RX_LOBBY]["blackout"] == "on"
    assert _child(driver, "decoder", RX_LOBBY, "blackout") is True

    await driver.send_command("set_stream", {"rx": RX_LOBBY, "state": "off"})
    assert sim._eps[RX_LOBBY]["stream"] == "off"
    assert _child(driver, "decoder", RX_LOBBY, "stream") is False

    await driver.send_command("set_rotate", {"rx": RX_LOBBY, "rotation": "5"})
    assert sim._eps[RX_LOBBY]["rotate"] == "5"

    # A poll confirms the mirrored values came back from the device, too.
    await driver.poll()
    assert _child(driver, "decoder", RX_LOBBY, "blackout") is True
    assert _child(driver, "decoder", RX_LOBBY, "rotate") == "5"


@pytest.mark.asyncio
async def test_encoder_edid_and_volume():
    driver, sim = await _make_pair()

    await driver.send_command("set_edid", {"tx": TX_APPLE, "edid": "13"})
    assert sim._eps[TX_APPLE]["edid"] == "13"

    await driver.send_command("set_encoder_volume", {"tx": TX_APPLE, "level": 40})
    assert sim._eps[TX_APPLE]["volume"] == 40
    await driver.poll()
    assert _child(driver, "encoder", TX_APPLE, "audio_volume") == 40

    # copy_edid reads the display's EDID off a decoder and lands it on an encoder.
    await driver.send_command("copy_edid", {"rx": RX_LOBBY, "tx": TX_LAPTOP})
    assert sim._eps[TX_LAPTOP]["edid"] == "14"


@pytest.mark.asyncio
async def test_rename_endpoint_refreshes_the_roster_label():
    driver, sim = await _make_pair()
    await driver.send_command("rename_endpoint", {"endpoint": RX_LOBBY, "name": "Atrium"})

    assert sim._eps[RX_LOBBY]["name"] == "Atrium"
    # Identity is the MAC, so the child survives the rename; only the label moves.
    assert RX_LOBBY in driver.list_children("decoder")
    assert _child(driver, "decoder", RX_LOBBY, "name") == "Atrium"


@pytest.mark.asyncio
async def test_serial_settings_and_cec_reach_the_endpoint():
    driver, sim = await _make_pair()

    await driver.send_command(
        "set_serial_settings", {"endpoint": RX_BAR_L, "baud": "38400", "parity": "1"}
    )
    assert sim._eps[RX_BAR_L]["serial_setting"] == "38400 8 1 1 0"

    await driver.send_command("send_cec", {"endpoint": RX_LOBBY, "data": "40 04"})
    assert _FakeTCPTransport.sent_lines[-1] == f"config set device cec 4004 {RX_LOBBY}"


# ── Unsolicited RS-232 feedback ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_normalizes_serial_encapsulation():
    driver, sim = await _make_pair()
    # ASCII (type 2) is the driver default, pushed to every endpoint on connect.
    assert "config set device rs232responsetype 2 ALL" in _FakeTCPTransport.sent_lines
    assert sim._eps[TX_APPLE]["serial_type"] == "2"


@pytest.mark.asyncio
async def test_inbound_serial_lands_on_the_endpoint_that_received_it():
    driver, sim = await _make_pair()

    # The sim loops the endpoint's serial TX back to its RX (the API doc's own
    # feedback rig), so sending produces an unsolicited frame with cmd:"".
    await driver.send_command("send_serial", {"endpoint": RX_BAR_L, "data": "PWR ON"})

    assert _child(driver, "decoder", RX_BAR_L, "serial_data") == "PWR ON"
    # It must not smear onto any other endpoint.
    assert _child(driver, "decoder", RX_LOBBY, "serial_data") in (None, "")


@pytest.mark.asyncio
async def test_inbound_serial_decodes_base64_when_configured():
    driver, sim = await _make_pair({"serial_feedback_format": "base64"})
    assert sim._eps[TX_APPLE]["serial_type"] == "1"

    await sim.emit_serial("Apple-TV", "TEST")
    await asyncio.sleep(0)

    assert sim.pushed and base64.b64encode(b"TEST").decode() in sim.pushed[-1].decode()
    assert _child(driver, "encoder", TX_APPLE, "serial_data") == "TEST"


@pytest.mark.asyncio
async def test_unsolicited_frame_never_completes_a_pending_request():
    driver, sim = await _make_pair()

    # A serial frame arriving mid-request must not be mistaken for its reply.
    async def _interleave():
        await asyncio.sleep(0)
        await sim.emit_serial("Bar-Left", "NOISE")

    task = asyncio.ensure_future(_interleave())
    doc = await driver._request("config get version")
    await task

    assert doc["info"] == "2.28"
    assert _child(driver, "decoder", RX_BAR_L, "serial_data") == "NOISE"


# ── Device settings ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_settings_write_and_read_back():
    driver, sim = await _make_pair()

    await driver.set_device_setting("previews", False)
    assert sim.state["previews"] == 0
    assert _dev(driver, "previews") is False

    await driver.set_device_setting("timezone", "utc-5")
    assert sim.state["timezone"] == "UTC-5"

    await driver.set_device_setting("ntp_servers", "time.nist.gov pool.ntp.org")
    assert sim._ntp == ["time.nist.gov", "pool.ntp.org"]

    # The read-back path (slow poll cycle) agrees with what we wrote.
    await driver.poll()
    assert _dev(driver, "previews") is False
    assert _dev(driver, "timezone") == "UTC-5"
    assert _dev(driver, "ntp_servers") == "time.nist.gov pool.ntp.org"


@pytest.mark.asyncio
async def test_device_setting_validates_before_it_reaches_the_device():
    driver, _sim = await _make_pair()
    with pytest.raises(ValueError, match="UTC-12 to UTC"):
        await driver.set_device_setting("timezone", "EST")
    with pytest.raises(ValueError, match="at most five"):
        await driver.set_device_setting("ntp_servers", "a b c d e f")


# ── Failure paths ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_error_reply_raises_with_the_cbox_reason():
    driver, _sim = await _make_pair()
    with pytest.raises(ValueError, match="invalid hdcp value"):
        await driver.send_command("set_hdcp", {"rx": RX_LOBBY, "mode": "9"})


@pytest.fixture
def fast_timeouts(monkeypatch):
    """Don't sit through the real 6s / 15s deadlines to prove a timeout path."""
    monkeypatch.setattr(DRV, "REQUEST_TIMEOUT_S", 0.05)
    monkeypatch.setattr(DRV, "ROSTER_TIMEOUT_S", 0.05)


@pytest.mark.asyncio
async def test_silent_cbox_flips_the_device_offline(fast_timeouts):
    global _SWALLOW
    driver, _sim = await _make_pair()

    _SWALLOW = True
    try:
        # First miss is tolerated (one dropped reply is not a dead box)...
        await driver.poll()
        # ...the second raises, so the platform's watchdog marks it offline.
        with pytest.raises(ConnectionError, match="stopped answering"):
            await driver.poll()
    finally:
        _SWALLOW = False


@pytest.mark.asyncio
async def test_connect_fails_cleanly_when_nothing_answers(fast_timeouts):
    driver, _sim = await _make_pair(connect=False, swallow=True)
    with pytest.raises(ConnectionError, match="No answer from the MXNet API"):
        await driver.connect()
    # The failed handshake tore everything down: no leaked transport, no
    # connected flag, session bookkeeping back to a clean slate.
    assert driver.transport is None
    assert driver._connected is False
    assert driver._pending is None


@pytest.mark.asyncio
async def test_object_split_across_reads_is_reassembled():
    # Every reply is delivered one byte at a time — the brace-balancing parser
    # must still hand the driver whole objects.
    driver, _sim = await _make_pair(drip=True)
    await driver.poll()

    assert driver.list_children("encoder") == sorted([TX_APPLE, TX_LAPTOP, TX_CABLE])
    assert _child(driver, "decoder", RX_LOBBY, "source_video") == TX_APPLE
