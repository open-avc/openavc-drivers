"""Driver + simulator tests for biamp_tesira_ttp (Tesira Text Protocol).

No Tesira hardware on hand, so correctness is proven as a **dual-proof round
trip** wiring the real driver to the real simulator: the sim speaks the IAC +
welcome-banner handshake and answers TTP lines, the driver parses them, and
results are asserted on both sides (same approach as
test_blackmagic_videohub.py).

Covers the v2.2.0 first-class adoption:
  - liveness: `DEVICE get version` probe correlated through the pending-GET
    FIFO — a +OK or -ERR reply resolves it, a subscription push does NOT,
    and a silent device forces a reconnect with a typed no_response fault
    (a push driver with poll_interval=0 would otherwise never flip offline);
  - reconnect clears stale pending-GET entries (a session that died with
    unanswered GETs would otherwise mis-route the new session's replies);
  - quick actions + the Test Connection / Verify Blocks setup wizard against
    a real in-test TTP server (ok path, typo'd instance tag, unreachable);
  - the class-level catalog surface (generic commands / system state vars)
    is non-empty.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

from _lifecycle_fake import LifecycleFake

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "biamp_tesira_ttp.py"
SIM_PATH = REPO_ROOT / "audio" / "biamp_tesira_ttp_sim.py"

TEST_BLOCKS = """\
Level1 level 1-4
Mute1 mute 1-4
PgmSrc source_select
"""


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value


class _FakeEvents:
    async def emit(self, name, *args, **kwargs):
        pass


class _FakeBaseDriver(LifecycleFake):
    """Stand-in mirroring the platform BaseDriver liveness watchdog."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self.disconnect_calls = 0
        self.stashed_fault: tuple[str, str] | None = None
        self._health_task = None
        self._health_failures = 0
        self._bg_tasks: set = set()
        # Child-entity registry (mirrors the platform child API closely
        # enough for the dual-proof tests; the real schema/id validation is
        # covered by the ad-hoc real-BaseDriver smoke).
        self._children: dict = {}
        self._child_schemas: dict = {}

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    # -- child entities --

    def _child_key(self, child_type, local_id, prop) -> str:
        return f"device.{self.device_id}.{child_type}.{local_id}.{prop}"

    def register_child(self, child_type, local_id, initial_state=None, schema=None) -> None:
        bucket = self._children.setdefault(child_type, {})
        if local_id in bucket:
            return  # idempotent — same as the platform
        eff = dict(schema or {})
        eff.setdefault("online", {"type": "boolean"})
        eff.setdefault("label", {"type": "string"})
        overrides = dict(initial_state or {})
        for prop in overrides:
            if prop not in eff:
                raise ValueError(
                    f"initial_state property {prop!r} not in child schema")
        bucket[local_id] = True
        self._child_schemas[(child_type, local_id)] = eff
        for prop in eff:
            if prop == "online":
                value = overrides.get("online", True)
            elif prop == "label":
                value = overrides.get("label", "")
            else:
                value = overrides.get(prop)
            self.state.set(self._child_key(child_type, local_id, prop), value)

    def deregister_child(self, child_type, local_id) -> None:
        self._children.get(child_type, {}).pop(local_id, None)
        self._child_schemas.pop((child_type, local_id), None)

    def is_child_registered(self, child_type, local_id) -> bool:
        return local_id in self._children.get(child_type, {})

    def set_child_state_batch(self, child_type, local_id, updates) -> None:
        if not self.is_child_registered(child_type, local_id):
            return
        eff = self._child_schemas.get((child_type, local_id), {})
        for prop in updates:
            if prop not in eff:
                raise ValueError(f"unknown child prop {prop!r}")
        for prop, value in updates.items():
            self.state.set(self._child_key(child_type, local_id, prop), value)

    def get_child_schema(self, child_type, local_id):
        return dict(self._child_schemas.get((child_type, local_id), {}))

    def get_child_state(self, child_type, local_id):
        eff = self._child_schemas.get((child_type, local_id), {})
        return {
            prop: self.state.data.get(self._child_key(child_type, local_id, prop))
            for prop in eff
        }

    def _handle_transport_disconnect(self) -> None:
        # Mirrors the platform: flip the flags synchronously, then schedule
        # the async teardown (stop loops, close+null transport, close the
        # driver session, emit the canonical disconnect event).
        self._connected = False
        self.set_state("connected", False)
        self.disconnect_calls += 1
        if self.transport is not None:
            self.transport.connected = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._on_disconnect_cleanup())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _on_disconnect_cleanup(self) -> None:
        self._stop_health_loop()
        await self._stop_push()
        await self.stop_polling()
        transport = self.transport
        self.transport = None
        if transport is not None:
            try:
                await transport.close()
            except Exception:
                pass
        await self._close_session()
        await self.events.emit(f"device.disconnected.{self.device_id}")

    # -- liveness watchdog (mirrors the platform BaseDriver: probe every
    # HEALTH_INTERVAL_S under a HEALTH_TIMEOUT_S deadline; HEALTH_MAX_FAILURES
    # misses force a disconnect with a typed no_response fault) --

    # -- connection lifecycle (mirrors BaseDriver.connect/disconnect stage
    # order; the hook defaults are no-ops the driver under test overrides) --

    async def _stop_push(self) -> None:
        pass

    async def _pre_connect(self) -> None:
        pass

    async def _post_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    def _resolve_delimiter(self):
        return b"\r"

    async def _create_transport(self, transport_type) -> None:
        kwargs = dict(
            host=self.config.get("host", ""),
            port=self.config.get("port", 23),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=self._resolve_delimiter(),
            frame_parser=None,
            timeout=self.config.get("timeout", 5.0),
            inter_command_delay=self.config.get("inter_command_delay", 0.0),
            name=self.device_id,
            local_addr=None,
        )
        # The module-level fake is referenced directly — a deferred
        # `from server.transport...` import at test-run time would miss the
        # collection-time stubs.
        self.transport = await _FakeTCPTransport.create(
            **self._transport_kwargs(transport_type, kwargs))

    async def connect(self) -> None:
        # 1. Clean slate — a retry must not inherit a stale session/transport.
        await self._stop_push()
        await self._close_session()
        if self.transport:
            try:
                await self.transport.close()
            except Exception:
                pass
            self.transport = None
        # 2-3. Establish.
        await self._pre_connect()
        transport_type = self.config.get("transport") or self.DRIVER_INFO.get(
            "transport", "tcp")
        await self._create_transport(transport_type)
        # 4. Handshake before `connected` is declared; a raise tears down.
        try:
            await self._post_connect()
        except Exception:
            if self.transport:
                try:
                    await self.transport.close()
                except Exception:
                    pass
                self.transport = None
            await self._close_session()
            self._connected = False
            raise
        # 5. Declare.
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        # 6. Initial sync; a raise here tears the connection back down.
        try:
            await self._initial_sync()
        except Exception:
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
        # 7. Polling + liveness watchdog.
        if self.config.get("poll_interval", 0) > 0:
            await self.start_polling(self.config["poll_interval"])
        if self._health_enabled():
            self._start_health_loop()

    async def disconnect(self) -> None:
        self._stop_health_loop()
        await self._stop_push()
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


class _DelimiterFrameParser:
    """Functional stand-in for server.transport.frame_parsers."""

    def __init__(self, delimiter=b"\r\n") -> None:
        self.delimiter = delimiter
        self._buf = bytearray()

    def feed(self, data):
        self._buf.extend(data)
        frames = []
        while True:
            idx = self._buf.find(self.delimiter)
            if idx < 0:
                break
            frames.append(bytes(self._buf[:idx]))
            del self._buf[: idx + len(self.delimiter)]
        return frames


class _FakeSimState:
    def __init__(self, initial) -> None:
        self.data = dict(initial)

    def get(self, key, default=None):
        return self.data.get(key, default)


class _FakeTCPSimulator:
    """Stand-in for simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = _FakeSimState(self.SIMULATOR_INFO.get("initial_state", {}))
        self._clients: dict = {}

    def set_state(self, key, value) -> None:
        self.state.data[key] = value

    async def push_to(self, client_id, data: bytes) -> None:
        """Mirror the framework's per-client push. The registered client is
        either the in-memory fake transport or an asyncio StreamWriter."""
        target = self._clients.get(client_id)
        if target is None:
            return
        deliver = getattr(target, "_deliver", None)
        if deliver is not None:
            await deliver(data)
        else:
            try:
                target.write(data)
                await target.drain()
            except (ConnectionError, OSError):
                self._clients.pop(client_id, None)


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport processes requests but DROPS every reply — a
# silently-vanished device for the liveness tests.
_SWALLOW = False


class _FakeTCPTransport:
    """In-memory transport pairing the driver with the live simulator.

    Mirrors the two-phase Tesira link: raw mode during the IAC/banner
    handshake (no _frame_parser), then the driver swaps in the \\r\\n
    delimiter parser and every sim reply is framed through it.
    """

    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._frame_parser = None
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, inter_command_delay=0.0, name="",
                     local_addr=None, **kw):
        t = cls(on_data, on_disconnect)
        t._sim._clients["c1"] = t
        greeting = await t._sim.on_client_connected("c1")
        if greeting:
            await t._deliver(greeting)
        return t

    async def _deliver(self, data: bytes) -> None:
        if _SWALLOW:
            return
        if self._frame_parser is None:
            await self.on_data(data)
        else:
            for frame in self._frame_parser.feed(data):
                await self.on_data(frame)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        for raw in bytes(data).split(b"\n"):
            raw = raw.strip(b"\r")
            if not raw:
                continue
            # Pure-IAC payloads (the driver's WONT/DONT replies) decode to
            # an empty line inside the sim and are ignored there.
            resp = self._sim.handle_command(raw)
            if resp:
                await self._deliver(resp)

    async def close(self) -> None:
        self.connected = False


class _FakeSystemConfig:
    def get(self, section, key, default=None):
        return default


def _build_stub_modules() -> dict[str, ModuleType]:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    stubs: dict[str, ModuleType] = {"server": server}
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"server.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        stubs[f"server.{sub}"] = m
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    stubs["server.drivers.base"] = base
    tcp = ModuleType("server.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    stubs["server.transport.tcp"] = tcp
    parsers = ModuleType("server.transport.frame_parsers")
    parsers.DelimiterFrameParser = _DelimiterFrameParser
    stubs["server.transport.frame_parsers"] = parsers
    sysconf = ModuleType("server.system_config")
    sysconf.get_system_config = lambda: _FakeSystemConfig()
    stubs["server.system_config"] = sysconf
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    stubs["server.utils.logger"] = logger

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    stubs["simulator"] = sim_pkg
    sim_tcp = ModuleType("simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    stubs["simulator.tcp_simulator"] = sim_tcp
    return stubs


_STUB_MODULES = _build_stub_modules()


def _load(name: str, path: Path) -> ModuleType:
    sys.modules.update(_STUB_MODULES)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("tesira_under_test", DRIVER_PATH)
SIM = _load("tesira_sim_under_test", SIM_PATH)


@pytest.fixture(autouse=True)
def _stub_platform_modules(monkeypatch):
    """Re-install the platform stubs for each test's runtime.

    The driver imports TCPTransport / get_system_config lazily inside
    connect(), which resolves through sys.modules at call time — after
    conftest.py has rolled the collection-time stubs back. Without this the
    lazy import would hit the real platform (when installed) or ImportError
    (community CI). monkeypatch restores the originals after each test.
    """
    for name, mod in _STUB_MODULES.items():
        monkeypatch.setitem(sys.modules, name, mod)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.BiampTesiraTTPSimulator("sim1", {})
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.5",
        "port": 23,
        "blocks": TEST_BLOCKS,
        "poll_interval": 0,
        "inter_command_delay": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.BiampTesiraTTPDriver("dsp1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _child(driver, cid, prop, default=None):
    """Read one block child's property from the fake state store."""
    return driver.state.data.get(f"device.{driver.device_id}.block.{cid}.{prop}", default)


# ── blocks config parsing (row list + legacy string) ────────────────────────

def test_parse_blocks_rows():
    # The `type: table` config value is a list of row dicts. Channels/NxM come
    # from the "channels" cell; aliases and unknown types are still handled;
    # a nameless row is dropped.
    rows = [
        {"tag": "Mute1", "type": "mute", "channels": "1-4"},
        {"tag": "PgmSrc", "type": "source_select"},
        {"tag": "PgmMix", "type": "matrix_mixer", "channels": "8x4"},
        {"tag": "Solo", "type": "fader"},              # alias -> level, chan 1
        {"tag": "Huh", "type": "bogus"},               # unknown -> "unknown"
        {"tag": "", "type": "mute"},                   # no tag -> dropped
        {"type": "mute"},                              # no tag -> dropped
        "not-a-dict",
    ]
    blocks = DRV.parse_blocks_config(rows)
    by_tag = {b["tag"]: b for b in blocks}
    assert set(by_tag) == {"Mute1", "PgmSrc", "PgmMix", "Solo", "Huh"}
    assert by_tag["Mute1"]["type"] == "mute" and by_tag["Mute1"]["channels"] == [1, 2, 3, 4]
    assert by_tag["PgmSrc"]["type"] == "source_select" and by_tag["PgmSrc"]["channels"] == []
    assert by_tag["PgmMix"]["type"] == "matrix_mixer"
    assert by_tag["PgmMix"]["extra"] == {"inputs": 8, "outputs": 4}
    assert by_tag["Solo"]["type"] == "level" and by_tag["Solo"]["channels"] == [1]
    assert by_tag["Huh"]["type"] == "unknown"


def test_parse_blocks_legacy_string_and_rows_agree():
    # A project saved before the table editor stores a string; the parser
    # converts it (reusing the old line grammar) so it still loads. The
    # converter output fed back through the row path must expand identically —
    # proving a stored string can be re-authored without data loss.
    txt = (
        "# comment\n"
        "Mute1   mute    1-4     # inline comment\n"
        "PgmSrc  source_select\n"
        "PgmMix  matrix_mixer 8x4\n"
        "Short\n"                       # too short -> dropped
    )
    from_string = DRV.parse_blocks_config(txt)
    rows = DRV._blocks_text_to_rows(txt)
    assert rows == [
        {"tag": "Mute1", "type": "mute", "channels": "1-4"},
        {"tag": "PgmSrc", "type": "source_select"},
        {"tag": "PgmMix", "type": "matrix_mixer", "channels": "8x4"},
    ]
    assert from_string == DRV.parse_blocks_config(rows)
    assert [b["tag"] for b in from_string] == ["Mute1", "PgmSrc", "PgmMix"]


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_and_actions_shape():
    info = DRV.BiampTesiraTTPDriver.DRIVER_INFO
    assert info["version"] == "3.1.1"
    # The connection-lifecycle hooks the driver overrides landed in 0.24.0.
    assert info["min_platform_version"] == "0.24.0"

    # Static class-level surface: escape hatches + child-scoped commands +
    # system state vars, plus the dynamic "block" child type.
    assert "recall_preset" in info["commands"]
    assert "set_control" in info["commands"]
    assert "firmware_version" in info["state_variables"]
    block_type = info["child_entity_types"]["block"]
    assert block_type["dynamic"] is True
    assert block_type["id_format"]["type"] == "string"

    # set_control wires the block picker + control cascade + typed value.
    set_ctl = info["commands"]["set_control"]["params"]
    assert set_ctl["block"]["type"] == "child_id"
    assert set_ctl["block"]["child_type"] == "block"
    assert set_ctl["control"]["options_from"] == {"param": "block", "source": "child_schema"}
    assert set_ctl["value"]["type_from"] == {"param": "control"}

    # Every promoted quick action resolves to a declared command.
    for cid in info["quick_actions"]:
        assert cid in info["commands"], cid
    action_ids = {a["id"] for a in info["actions"]}
    assert "test_connection" in action_ids
    setup = next(a for a in info["actions"] if a["id"] == "test_connection")
    assert setup["kind"] == "setup"
    assert setup["availability"] == "always"


def test_block_children_expanded_from_config():
    driver, _sim = _run(_make_pair())
    # Each declared block becomes a child with a per-instance schema; the
    # command surface is static (not per-block-named).
    assert set(driver._block_schemas) == {"Level1", "Mute1", "PgmSrc"}
    lvl = driver._block_schemas["Level1"]
    assert lvl["level_1"]["control"] is True
    assert lvl["level_1"]["min"] == -100 and lvl["level_1"]["max"] == 12
    assert lvl["level_1"]["cloud_priority"] == "low"
    assert lvl["mute_1"]["cloud_priority"] == "high"
    assert "Level1_level_set" not in driver.DRIVER_INFO["commands"]
    for c in ("set_control", "toggle_control", "step_control", "ramp_level",
              "recall_preset"):
        assert c in driver.DRIVER_INFO["commands"]
    # The wire map resolves a child (block, control) back to (attr, index).
    assert driver._prop_wire[("Level1", "level_1")] == ("level", 1)
    assert driver._prop_wire[("PgmSrc", "source")] == ("sourceSelection", None)


def test_block_children_expanded_from_row_list_config():
    # Same expansion, but the `blocks` config is the `type: table` row list
    # (list of dicts) rather than a legacy string — the runtime path a device
    # authored in the table editor takes.
    rows = [
        {"tag": "Level1", "type": "level", "channels": "1-4"},
        {"tag": "Mute1", "type": "mute", "channels": "1-4"},
        {"tag": "PgmSrc", "type": "source_select"},
    ]
    driver, _sim = _run(_make_pair({"blocks": rows}))
    assert set(driver._block_schemas) == {"Level1", "Mute1", "PgmSrc"}
    lvl = driver._block_schemas["Level1"]
    assert lvl["level_1"]["control"] is True
    assert lvl["level_4"]["min"] == -100 and lvl["level_4"]["max"] == 12
    assert driver._prop_wire[("Level1", "level_1")] == ("level", 1)
    assert driver._prop_wire[("PgmSrc", "source")] == ("sourceSelection", None)


# ── Connect round trip ──────────────────────────────────────────────────────

def test_connect_handshake_and_initial_state():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert driver._connected
            # Metadata gets routed through the pending-GET FIFO.
            assert driver.get_state("serial_number") == "SIM00001"
            assert driver.get_state("firmware_version") == "4.14.0"
            # Every declared block registered as a child.
            for cid in ("Level1", "Mute1", "PgmSrc"):
                assert driver.is_child_registered("block", cid)
            assert _child(driver, "Level1", "block_type") == "level"
            # Subscriptions registered on the sim side.
            subs = sim._client_subs["c1"]
            assert len(subs) == len(driver._subscriptions)
            # Initial GET populated child state from the sim's DSP state.
            assert _child(driver, "Level1", "level_1") == -10.0
            assert _child(driver, "Mute1", "mute_2") is False
        finally:
            await driver.disconnect()
    _run(scenario())


def test_connect_round_trip_row_list_config():
    # The full connect round-trip through the real simulator, but with the
    # `type: table` row-list config a device authored in the editor stores —
    # children register and populate from the DSP's live state identically to
    # the legacy-string path above.
    rows = [
        {"tag": "Level1", "type": "level", "channels": "1-4"},
        {"tag": "Mute1", "type": "mute", "channels": "1-4"},
        {"tag": "PgmSrc", "type": "source_select"},
    ]

    async def scenario():
        driver, sim = await _make_pair({"blocks": rows})
        await driver.connect()
        try:
            for cid in ("Level1", "Mute1", "PgmSrc"):
                assert driver.is_child_registered("block", cid)
            assert _child(driver, "Level1", "block_type") == "level"
            assert _child(driver, "Level1", "level_1") == -10.0
            assert _child(driver, "Mute1", "mute_2") is False
        finally:
            await driver.disconnect()
    _run(scenario())


# ── Child-entity control extraction (CE) ────────────────────────────────────

async def _settle(driver, cid, prop, want, ticks=20):
    """Yield the loop until an async sim push lands in child state."""
    for _ in range(ticks):
        await asyncio.sleep(0)
        if _child(driver, cid, prop) == want:
            return
    return


def test_set_control_round_trips_through_sim():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command(
                "set_control", {"block": "Level1", "control": "level_2", "value": -20.0})
            await _settle(driver, "Level1", "level_2", -20.0)
            assert sim._dsp[("Level1", "level", 2)] == -20.0
            assert _child(driver, "Level1", "level_2") == -20.0

            # A source_select (indexless) control resolves too.
            await driver.send_command(
                "set_control", {"block": "PgmSrc", "control": "source", "value": 3})
            await _settle(driver, "PgmSrc", "source", 3)
            assert sim._dsp[("PgmSrc", "sourceSelection", None)] == 3
        finally:
            await driver.disconnect()
    _run(scenario())


def test_toggle_and_step_control_reach_wire():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim._dsp[("Mute1", "mute", 1)] = False
            await driver.send_command(
                "toggle_control", {"block": "Mute1", "control": "mute_1"})
            await _settle(driver, "Mute1", "mute_1", True)
            assert sim._dsp[("Mute1", "mute", 1)] is True

            sim._dsp[("Level1", "level", 1)] = -10.0
            await driver.send_command(
                "step_control", {"block": "Level1", "control": "level_1", "amount": -3.0})
            await _settle(driver, "Level1", "level_1", -13.0)
            assert sim._dsp[("Level1", "level", 1)] == -13.0
        finally:
            await driver.disconnect()
    _run(scenario())


def test_external_change_pushes_to_child_state():
    """A front-panel nudge on the DSP (external state change) pushes to
    subscribers and updates the block child, no command sent by us."""
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim._dsp[("Mute1", "mute", 3)] = True
            sim._push_subscribers("Mute1", "mute", 3)
            await _settle(driver, "Mute1", "mute_3", True)
            assert _child(driver, "Mute1", "mute_3") is True
        finally:
            await driver.disconnect()
    _run(scenario())


def test_big_matrix_crosspoints_settable_but_not_subscribed():
    """A >64-crosspoint mixer keeps every crosspoint in the child schema
    (settable) but does not auto-subscribe them (wire-flood guard)."""
    driver, _sim = _run(_make_pair({"blocks": "Big matrix_mixer 16x16\n"}))
    schema = driver._block_schemas["Big"]
    assert "xpoint_16_16" in schema and schema["xpoint_16_16"]["control"] is True
    assert ("Big", "xpoint_16_16") in driver._prop_wire
    assert not any(t.startswith("Big_xpoint") for t in driver._route_by_key)
    # An 8x4 mixer (32 crosspoints) IS subscribed.
    d2, _ = _run(_make_pair({"blocks": "Mix matrix_mixer 8x4\n"}))
    assert any(t.startswith("Mix_xpoint") for t in d2._route_by_key)


def test_unknown_block_or_control_is_a_no_op():
    async def scenario():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            before = dict(sim._dsp)
            assert await driver.send_command(
                "set_control", {"block": "Nope", "control": "x", "value": 1}) is None
            assert await driver.send_command(
                "set_control", {"block": "Level1", "control": "no_such", "value": 1}) is None
            assert sim._dsp == before
        finally:
            await driver.disconnect()
    _run(scenario())


def test_reconnect_clears_stale_pending_gets():
    """A session that died with unanswered GETs must not desync the next
    session's reply routing (latent-defect regression)."""
    async def scenario():
        driver, sim = await _make_pair()
        # Simulate a dead session's leftovers: two never-answered GETs.
        driver._pending_gets.append(("Level1_level_1", "number"))
        driver._pending_gets.append(("Mute1_mute_1", "boolean"))
        await driver.connect()
        try:
            # If connect() hadn't cleared the queue, the serial-number reply
            # would have landed in Level1's level_1 child prop.
            assert driver.get_state("serial_number") == "SIM00001"
            assert _child(driver, "Level1", "level_1") == -10.0
        finally:
            await driver.disconnect()
    _run(scenario())


# ── Liveness ────────────────────────────────────────────────────────────────

def test_liveness_probe_resolves_on_reply():
    async def scenario():
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            await asyncio.wait_for(driver._liveness_probe(), 1.0)
        finally:
            await driver.disconnect()
    _run(scenario())


def test_liveness_probe_times_out_when_silent():
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            _SWALLOW = True
            try:
                await asyncio.wait_for(driver._liveness_probe(), 0.1)
            except asyncio.TimeoutError:
                return
            raise AssertionError("probe resolved with no reply")
        finally:
            _SWALLOW = False
            await driver.disconnect()
    _run(scenario())


def test_push_does_not_satisfy_probe():
    """A subscription push proves nothing about the probe round-trip — only
    a reply that consumes the probe's FIFO entry may resolve it."""
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            _SWALLOW = True  # sim replies dropped; we inject lines by hand
            probe = asyncio.ensure_future(driver._liveness_probe())
            await asyncio.sleep(0.05)
            assert not probe.done()
            # A push arrives — probe must stay pending.
            await driver.on_data_received(
                b'! "publishToken":"Level1_level_1" "value":-6.0')
            await asyncio.sleep(0.05)
            assert not probe.done()
            assert _child(driver, "Level1", "level_1") == -6.0
            # The version reply arrives — probe resolves.
            await driver.on_data_received(b'+OK "value":"4.14.0"')
            await asyncio.wait_for(probe, 1.0)
        finally:
            _SWALLOW = False
            await driver.disconnect()
    _run(scenario())


def test_err_reply_satisfies_probe():
    """-ERR still proves the device answered (alive, just unhappy)."""
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            _SWALLOW = True
            probe = asyncio.ensure_future(driver._liveness_probe())
            await asyncio.sleep(0.05)
            assert not probe.done()
            await driver.on_data_received(b"-ERR Simulated failure")
            await asyncio.wait_for(probe, 1.0)
        finally:
            _SWALLOW = False
            await driver.disconnect()
    _run(scenario())


def test_health_loop_forces_reconnect_on_silent_device():
    async def scenario():
        global _SWALLOW
        driver, _sim = await _make_pair()
        await driver.connect()
        driver._stop_health_loop()  # restart below with test-speed cadence
        driver.HEALTH_INTERVAL_S = 0.01
        driver.HEALTH_TIMEOUT_S = 0.05
        _SWALLOW = True
        try:
            driver._start_health_loop()
            for _ in range(100):
                await asyncio.sleep(0.05)
                if driver.disconnect_calls:
                    break
            assert driver.disconnect_calls == 1
            assert driver.stashed_fault is not None
            code, message = driver.stashed_fault
            assert code == "no_response"
            assert "DEVICE get version" in message
        finally:
            _SWALLOW = False
            driver._stop_health_loop()
            await driver.disconnect()
    _run(scenario())


# ── Setup wizard against a real in-test TTP server ──────────────────────────

class _TTPServer:
    """The real simulator behind a real asyncio TCP socket."""

    def __init__(self, sim) -> None:
        self.sim = sim
        self.server = None
        self.port = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(
            self._on_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _on_client(self, reader, writer):
        cid = f"w{id(writer)}"
        self.sim._clients[cid] = writer
        greeting = await self.sim.on_client_connected(cid)
        if greeting:
            writer.write(greeting)
            await writer.drain()
        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(256)
                if not chunk:
                    break
                buf.extend(chunk)
                while b"\n" in buf:
                    line, _, rest = bytes(buf).partition(b"\n")
                    buf = bytearray(rest)
                    resp = self.sim.handle_command(line)
                    if resp:
                        writer.write(resp)
                        await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


async def _progress(step, pct=None):
    pass


def test_setup_wizard_all_blocks_ok():
    async def scenario():
        driver, sim = await _make_pair()
        async with _TTPServer(sim) as srv:
            driver.config["host"] = "127.0.0.1"
            driver.config["port"] = srv.port
            result = await driver.run_setup_action(
                "test_connection", {}, _progress)
        assert result["firmware"] == "4.14.0"
        assert result["blocks_failed"] == []
        assert set(result["blocks_ok"]) == {"Level1", "Mute1", "PgmSrc"}
    _run(scenario())


def test_setup_wizard_flags_typoed_tag():
    """Real Tesira answers `-ERR address not found` for an unknown instance
    tag; the stock sim deliberately auto-seeds any tag so arbitrary user
    block lists work against it — use a strict subclass for the typo path."""
    class _StrictSim(SIM.BiampTesiraTTPSimulator):
        def _handle_get(self, tag, rest):
            if not any(t == tag for (t, _a, _i) in self._dsp):
                return SIM._err(f"address not found: {tag}")
            return super()._handle_get(tag, rest)

    async def scenario():
        global _CURRENT_SIM
        driver, _sim = await _make_pair(
            {"blocks": "Level1 level 1-4\nLvel2 level 1-2\n"})
        sim = _StrictSim("sim-strict", {})
        _CURRENT_SIM = sim
        async with _TTPServer(sim) as srv:
            driver.config["host"] = "127.0.0.1"
            driver.config["port"] = srv.port
            result = await driver.run_setup_action(
                "test_connection", {}, _progress)
        assert result["blocks_ok"] == ["Level1"]
        assert result["blocks_failed"] == ["Lvel2"]
    _run(scenario())


def test_setup_wizard_unreachable_host():
    async def scenario():
        driver, _sim = await _make_pair()
        # A port nothing listens on — connect is refused immediately.
        driver.config["host"] = "127.0.0.1"
        driver.config["port"] = 1
        try:
            await driver.run_setup_action("test_connection", {}, _progress)
        except ConnectionError as exc:
            assert "Telnet" in str(exc)
            return
        raise AssertionError("expected ConnectionError")
    _run(scenario())
