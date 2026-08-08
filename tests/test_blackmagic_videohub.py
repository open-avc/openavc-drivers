"""Driver + simulator tests for blackmagic_videohub (Videohub Ethernet Protocol).

No Videohub hardware on hand, so correctness is proven two ways: metadata/shape
assertions, and a **dual-proof round trip** wiring the real driver to the real
simulator over an in-memory transport that speaks the block protocol line by
line — the sim renders the blocks, the driver parses them, and results are
asserted on both sides (same approach as test_racklink_rlnk.py).

Covers the v1.3.0 first-class adoption:
  - child entities: inputs / outputs / monitoring outputs registered and sized
    from the VIDEOHUB DEVICE block — the fix for the old 16-video / 4-monitor
    hard cap that truncated wide frames (a 40×40 is asserted end to end);
  - child-prop routing / lock / label state, and child_id command params +
    coercion of a zero-padded picker value;
  - liveness: the PING/ACK watchdog forces a reconnect when the unit goes
    silent (a push device with no poll would otherwise never flip offline);
  - the Test Connection setup action reads a real banner off the wire.

Loads the driver + simulator with the ``openavc.*`` imports
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

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "blackmagic_videohub.py"
SIM_PATH = REPO_ROOT / "switchers" / "blackmagic_videohub_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver child-entity API."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._children: dict[str, dict[int, dict]] = {}
        self._connected = False
        self.disconnect_calls = 0
        self.stashed_fault: tuple[str, str] | None = None
        self._health_task = None
        self._health_failures = 0

    def _eff_schema(self, ctype: str) -> dict:
        schema = dict(self.DRIVER_INFO["child_entity_types"][ctype]["state_variables"])
        schema.setdefault("online", {"type": "boolean"})
        schema.setdefault("label", {"type": "string"})
        return schema

    def get_child_entity_types(self) -> dict:
        out = {}
        for ct, d in self.DRIVER_INFO.get("child_entity_types", {}).items():
            md = dict(d)
            md["state_variables"] = self._eff_schema(ct)
            out[ct] = md
        return out

    def register_child(self, ctype, lid, initial_state=None) -> None:
        bucket = self._children.setdefault(ctype, {})
        if lid in bucket:
            return  # idempotent
        schema = self._eff_schema(ctype)
        ov = dict(initial_state or {})
        for prop in ov:
            if prop not in schema:
                raise ValueError(f"unknown child prop {prop!r}")
        st: dict = {}
        for prop in schema:
            if prop == "online":
                st[prop] = ov.get("online", True)
            elif prop == "label":
                st[prop] = ov.get("label", "")
            else:
                st[prop] = ov.get(prop)
        bucket[lid] = st

    def deregister_child(self, ctype, lid) -> None:
        self._children.get(ctype, {}).pop(lid, None)

    def is_child_registered(self, ctype, lid) -> bool:
        return lid in self._children.get(ctype, {})

    def list_children(self, ctype) -> list:
        return sorted(self._children.get(ctype, {}).keys())

    def get_child_state(self, ctype, lid) -> dict:
        return dict(self._children.get(ctype, {}).get(lid, {}))

    def set_child_state(self, ctype, lid, prop, value) -> None:
        schema = self._eff_schema(ctype)
        if prop not in schema:
            raise ValueError(f"unknown child prop {prop!r}")
        if lid not in self._children.get(ctype, {}):
            raise ValueError(f"child {ctype}/{lid} not registered")
        self._children[ctype][lid][prop] = value

    def set_children_state_batch(self, updates) -> None:
        # Validate the whole batch before applying (matches the platform).
        for ctype, lid, child_updates in updates:
            schema = self._eff_schema(ctype)
            for prop in child_updates:
                if prop not in schema:
                    raise ValueError(f"unknown child prop {prop!r}")
            if lid not in self._children.get(ctype, {}):
                raise ValueError(f"child {ctype}/{lid} not registered")
        for ctype, lid, child_updates in updates:
            self._children[ctype][lid].update(child_updates)

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    def _handle_transport_disconnect(self) -> None:
        self._connected = False
        self.set_state("connected", False)
        self.disconnect_calls += 1
        if self.transport is not None:
            self.transport.connected = False

    # -- liveness watchdog (mirrors the platform BaseDriver: probe every
    # HEALTH_INTERVAL_S under a HEALTH_TIMEOUT_S deadline; HEALTH_MAX_FAILURES
    # misses force a disconnect with a typed no_response fault) --

    # -- connection lifecycle (mirrors the platform's hook-driven connect) --

    async def _pre_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    async def _create_transport(self, transport_type) -> None:
        kwargs = dict(
            host=self.config.get("host", ""),
            port=self.config.get("port", 9990),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\n",
            timeout=5.0,
            name=self.device_id,
        )
        self.transport = await _FakeTCPTransport.create(
            **self._transport_kwargs(transport_type, kwargs))

    async def connect(self) -> None:
        await self._close_session()
        await self._pre_connect()
        await self._create_transport("tcp")
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        await self._initial_sync()
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


class _FakeSimState:
    def __init__(self, initial) -> None:
        self.data = dict(initial)

    def get(self, key, default=None):
        return self.data.get(key, default)


class _FakeTCPSimulator:
    """Stand-in for openavc.simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = _FakeSimState(self.SIMULATOR_INFO.get("initial_state", {}))
        self._clients: dict = {}

    def set_state(self, key, value) -> None:
        self.state.data[key] = value


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport processes the request but DROPS the reply — a
# silently-vanished device for the liveness test.
_SWALLOW = False


class _FakeTCPTransport:
    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self.last_error = None
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, timeout=5.0, name=""):
        t = cls(on_data, on_disconnect)
        # The Videohub server dumps full state on connect.
        greeting = await t._sim.on_client_connected("c1")
        if greeting and not _SWALLOW:
            await t.on_data(greeting)
        return t

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        # The driver sends a whole block ("HEADER:\n...\n\n") per call; the sim
        # processes one line at a time, so feed it line by line.
        responses = []
        for raw in bytes(data).split(b"\n"):
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            resp = self._sim.handle_command(raw)
            if resp:
                responses.append(resp)
        if _SWALLOW:
            return
        for resp in responses:
            await self.on_data(resp)

    async def close(self) -> None:
        self.connected = False


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc"] = server
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"openavc.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"openavc.{sub}"] = m
    base = ModuleType("openavc.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["openavc.drivers.base"] = base
    tcp = ModuleType("openavc.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    sys.modules["openavc.transport.tcp"] = tcp
    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger

    sim_pkg = ModuleType("openavc.simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc.simulator"] = sim_pkg
    sim_tcp = ModuleType("openavc.simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    sys.modules["openavc.simulator.tcp_simulator"] = sim_tcp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("videohub_under_test", DRIVER_PATH)
SIM = _load("videohub_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.BlackmagicVideohubSimulator("sim1", sim_config or {})
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.5", "port": 9990, "poll_interval": 0}
    cfg.update(driver_overrides or {})
    driver = DRV.BlackmagicVideohubDriver("vh1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.BlackmagicVideohubDriver.DRIVER_INFO["version"] == "1.3.2"


def test_child_entity_types_declared():
    types = DRV.BlackmagicVideohubDriver.DRIVER_INFO["child_entity_types"]
    assert set(types) == {"input", "output", "monitor"}
    for t in types.values():
        assert t["id_format"]["type"] == "integer"
        assert t["id_format"]["max"] == DRV.PORT_MAX
        # reserved props must NOT be declared by the driver.
        assert "online" not in t["state_variables"]
        assert "label" not in t["state_variables"]
    # routed source + lock are the high-priority operational props.
    out_sv = types["output"]["state_variables"]
    assert out_sv["input"]["cloud_priority"] == "high"
    assert out_sv["lock"]["cloud_priority"] == "high"
    assert out_sv["name"]["cloud_priority"] == "low"


def test_flat_per_port_keys_removed():
    sv = DRV.BlackmagicVideohubDriver.DRIVER_INFO["state_variables"]
    # The old hard-capped flat keys are gone (that was the truncation bug).
    assert not any(k.startswith(("in_", "out_", "mon_")) for k in sv)
    # device-level info stays flat.
    assert "video_inputs" in sv and "monitoring_outputs" in sv


def test_routing_commands_use_child_id():
    cmds = DRV.BlackmagicVideohubDriver.DRIVER_INFO["commands"]
    assert cmds["route"]["params"]["output"]["type"] == "child_id"
    assert cmds["route"]["params"]["output"]["child_type"] == "output"
    assert cmds["route"]["params"]["input"]["child_type"] == "input"
    assert cmds["route_monitoring"]["params"]["output"]["child_type"] == "monitor"
    for cmd in ("lock_output", "unlock_output", "force_unlock_output"):
        assert cmds[cmd]["params"]["output"]["child_type"] == "output"


def test_discovery_probe_and_actions_present():
    info = DRV.BlackmagicVideohubDriver.DRIVER_INFO
    probe = info["discovery"]["tcp_probe"]
    assert probe["port"] == 9990
    assert "PROTOCOL PREAMBLE" in probe["expect_regex"]
    assert probe["extract_manufacturer"] == "Blackmagic Design"
    action_ids = {a["id"] for a in info["actions"]}
    assert "refresh" in action_ids and "test_connection" in action_ids
    # The BaseDriver liveness watchdog hook is a 0.22.0 platform API.
    assert info["min_platform_version"] == "0.24.0"


# ── CE: children sized to the frame (the truncation fix) ─────────────────────

def test_wide_frame_registers_all_children():
    async def go():
        # 40×40 with 2 monitoring outputs — past the old 16/4 cap.
        driver, sim = await _make_pair(
            sim_config={"inputs": 40, "outputs": 40, "monitors": 2}
        )
        await driver.connect()
        try:
            assert driver.list_children("input") == list(range(1, 41))
            assert driver.list_children("output") == list(range(1, 41))
            assert driver.list_children("monitor") == [1, 2]
            assert driver.get_state("video_outputs") == 40
            assert driver.get_state("monitoring_outputs") == 2
            # port 40 (well past the old cap of 16) carries real state.
            assert driver.get_child_state("output", 40)["name"] == "Output 40"
            assert driver.get_child_state("input", 40)["name"] == "Input 40"
            # protocol version came from the connect banner.
            assert driver.get_state("protocol_version") == "2.3"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_reconcile_shrinks_roster():
    async def go():
        driver, sim = await _make_pair(sim_config={"inputs": 40, "outputs": 40})
        await driver.connect()
        try:
            assert len(driver.list_children("output")) == 40
            # Device reports a smaller frame (module pull / frame swap).
            sim._n_outputs = 12
            sim._output_labels = sim._output_labels[:12]
            sim._routing = sim._routing[:12]
            sim._locks = sim._locks[:12]
            sim.set_state("video_outputs", 12)
            await driver.poll()
            assert driver.list_children("output") == list(range(1, 13))
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Round trips: command mutates the sim, dump updates the driver's children ─

def test_route_round_trip():
    async def go():
        driver, sim = await _make_pair(sim_config={"inputs": 40, "outputs": 40})
        await driver.connect()
        try:
            await driver.send_command("route", {"output": 20, "input": 5})
            assert sim._routing[19] == 4              # 0-indexed on the wire
            assert driver.get_child_state("output", 20)["input"] == 5
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_child_id_padded_string_coerced():
    async def go():
        driver, sim = await _make_pair(sim_config={"inputs": 40, "outputs": 40})
        await driver.connect()
        try:
            # The IDE child picker can hand back a zero-padded id ("020").
            await driver.send_command("route", {"output": "020", "input": "007"})
            assert sim._routing[19] == 6
            assert driver.get_child_state("output", 20)["input"] == 7
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_rename_and_lock_round_trip():
    async def go():
        driver, sim = await _make_pair(sim_config={"inputs": 40, "outputs": 40})
        await driver.connect()
        try:
            await driver.send_command(
                "set_output_label", {"output": 3, "label": "PGM"}
            )
            assert sim._output_labels[2] == "PGM"
            assert driver.get_child_state("output", 3)["name"] == "PGM"

            await driver.send_command("lock_output", {"output": 7})
            assert sim._locks[6] == "O"
            assert driver.get_child_state("output", 7)["lock"] == "owned"

            await driver.send_command("force_unlock_output", {"output": 7})
            assert sim._locks[6] == "U"
            assert driver.get_child_state("output", 7)["lock"] == "unlocked"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_monitor_route_round_trip():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"inputs": 16, "outputs": 16, "monitors": 2}
        )
        await driver.connect()
        try:
            await driver.send_command("route_monitoring", {"output": 1, "input": 9})
            assert sim._monitor_routing[0] == 8
            assert driver.get_child_state("monitor", 1)["input"] == 9
            assert driver.get_child_state("monitor", 1)["name"] == "Monitor 1"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_refresh_children_reports_counts():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"inputs": 20, "outputs": 20, "monitors": 1}
        )
        await driver.connect()
        try:
            result = await driver.refresh_children()
            assert result == {"inputs": 20, "outputs": 20, "monitors": 1}
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Liveness: PING/ACK probe via the BaseDriver watchdog hook ────────────────

def test_health_probe_succeeds_when_alive():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # The driver opts into the base watchdog by overriding the probe.
            assert driver._health_enabled()
            await driver._liveness_probe()  # sim ACKs → resolves, no raise
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_health_loop_forces_reconnect_on_silent_device():
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        await driver.connect()
        driver._stop_health_loop()  # restart below with test-speed cadence
        driver.HEALTH_INTERVAL_S = 0.01
        driver.HEALTH_TIMEOUT_S = 0.05
        try:
            _SWALLOW = True  # device stops replying to PING
            driver._start_health_loop()
            await asyncio.sleep(0.3)
            assert driver.disconnect_calls >= 1
            # The forced disconnect carries a typed no_response fault so the
            # device card shows the real cause, not a generic drop.
            assert driver.stashed_fault is not None
            assert driver.stashed_fault[0] == "no_response"
        finally:
            _SWALLOW = False
            driver._stop_health_loop()

    asyncio.run(go())


# ── Setup action: Test Connection reads a real banner off the wire ───────────

def test_setup_action_reads_banner():
    async def go():
        banner = (
            "PROTOCOL PREAMBLE:\nVersion: 2.3\n\n"
            "VIDEOHUB DEVICE:\nDevice present: true\n"
            "Model name: Blackmagic Videohub 12G 40x40\n"
            "Video inputs: 40\nVideo outputs: 40\n"
            "Video monitoring outputs: 0\nSerial ports: 0\n\n"
        ).encode()

        async def handle(reader, writer):
            writer.write(banner)
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            driver, _ = await _make_pair(
                driver_overrides={"host": "127.0.0.1", "port": port}
            )

            async def progress(msg, pct):
                pass

            result = await driver.run_setup_action("test_connection", {}, progress)
        assert result["success"] is True
        assert "Videohub 12G 40x40" in result["message"]
        assert "40×40" in result["message"]

    asyncio.run(go())
