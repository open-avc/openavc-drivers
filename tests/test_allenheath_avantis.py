"""Driver + simulator tests for allenheath_avantis (Avantis MIDI over TCP).

No Avantis hardware on hand, so correctness is proven as a **dual-proof
round trip** wiring the real driver to the real simulator: the sim parses
Note-On / NRPN / SysEx byte streams, answers name / colour Gets and pushes
console-side moves; the driver frames, sends and fans replies into child
state; results are asserted on both sides (same approach as
test_allenheath_dlive.py / test_allenheath_sq.py).

Covers the v2.0.0 first-class adoption:
  - CE: the fixed channel roster registers as string-id child entities,
    the on-connect SysEx name/colour Get sweep populates child state,
    pushes fan out to the right child, and a driver-seeded label never
    overrides a project one; console channel names land in the ``name``
    prop (label_field);
  - the protocol's no-Get reality: mute / fader have NO documented Get on
    Avantis (unlike dLive's Get-status family), so the sweep must NOT
    populate them — they sync via push and optimistic writes only;
  - LV: the name-Get probe watchdog — the sim's Input 1 name reply
    resolves the probe, pushes of OTHER parameters (even the probed
    channel's fader / mute / colour, or another channel's name) do not,
    and a silent console forces a reconnect with a typed no_response
    fault;
  - optimistic writes (A&H consoles don't echo changes they receive over
    MIDI — confirmed on Qu hardware), mute toggle without read-back;
  - address tables and codecs byte-exact against the Avantis MIDI TCP/IP
    Protocol reference tables (DCA 16 at 36-45, mute groups at 46-4D,
    DCA-assign 40-4F/00-0F, mute-group-assign 50-57/10-17 — all shifted
    vs dLive — fader anchors, scene banking, the running-status example).

Loads the driver + simulator with the ``server.*`` / ``simulator.*``
imports stubbed so the community CI stays self-contained (conftest.py
rolls the stubs back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "allenheath_avantis.py"
SIM_PATH = REPO_ROOT / "audio" / "allenheath_avantis_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Stand-in mirroring the platform BaseDriver child registry and
    liveness watchdog."""

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
        self._project_child_entities: dict = {}
        self.device_state: dict = {}
        self._children: dict = {}
        self._order: dict = {}

    @property
    def connected(self):
        return self._connected

    def set_state(self, key, value) -> None:
        self.device_state[key] = value
        self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.device_state.get(key, default)

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
            port=int(self.config.get("port", 0) or 0),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\n",
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

    # -- child registry (mirrors the platform's string-id validation) --

    def _eff_schema(self, child_type, local_id):
        types = self.DRIVER_INFO.get("child_entity_types", {})
        sch = dict(types.get(child_type, {}).get("state_variables", {}))
        sch.setdefault("online", {"type": "boolean"})
        sch.setdefault("label", {"type": "string"})
        return sch

    def register_child(self, child_type, local_id, initial_state=None,
                       schema=None):
        tdef = self.DRIVER_INFO.get("child_entity_types", {}).get(
            child_type, {})
        id_fmt = tdef.get("id_format")
        if id_fmt and id_fmt.get("type") == "string":
            if not isinstance(local_id, str):
                raise TypeError(
                    f"Child {child_type} local_id must be str, got "
                    f"{type(local_id).__name__}: {local_id!r}")
            if len(local_id) > id_fmt.get("max_length", 64):
                raise ValueError(f"Child {child_type} local_id too long")
        elif not isinstance(local_id, int):
            raise TypeError(
                f"Child {child_type} local_id must be int, got "
                f"{type(local_id).__name__}: {local_id!r}")
        if (child_type, local_id) in self._children:
            return
        eff = self._eff_schema(child_type, local_id)
        st = {}
        for prop, vd in eff.items():
            t = vd.get("type")
            st[prop] = (True if prop == "online" else
                        False if t == "boolean" else
                        0 if t == "integer" else
                        0.0 if t in ("number", "float") else "")
        for prop, val in (initial_state or {}).items():
            if prop not in eff:
                raise ValueError(f"unknown prop {prop!r} for {child_type}")
            st[prop] = val
        self._children[(child_type, local_id)] = st
        self._order.setdefault(child_type, []).append(local_id)

    def deregister_child(self, child_type, local_id):
        self._children.pop((child_type, local_id), None)
        if local_id in self._order.get(child_type, []):
            self._order[child_type].remove(local_id)

    def get_child_state(self, child_type, local_id):
        return dict(self._children.get((child_type, local_id), {}))

    def set_child_state_batch(self, child_type, local_id, updates):
        eff = self._eff_schema(child_type, local_id)
        for prop in updates:
            if prop not in eff:
                raise ValueError(f"bad prop {prop!r} for {child_type}")
        self._children[(child_type, local_id)].update(updates)

    def count_children(self, child_type):
        return len(self._order.get(child_type, []))

    # -- disconnect bookkeeping + liveness watchdog (mirrors the platform) --

    def _handle_transport_disconnect(self) -> None:
        self._connected = False
        self.set_state("connected", False)
        self.disconnect_calls += 1
        self._stop_health_loop()
        if self.transport is not None:
            self.transport.connected = False


class _FakeTCPSimulatorBase:
    """Stand-in for simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._push_targets: list = []

    async def push(self, data: bytes) -> None:
        for t in list(self._push_targets):
            t.deliver(data)


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport processes requests but DROPS every reply — a
# silently-vanished console for the liveness / optimistic-state tests.
_SWALLOW = False


class _FakeTCPTransport:
    """In-memory transport pairing the driver with the live simulator."""

    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self.sent = bytearray()
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, name="", **kw):
        t = cls(on_data, on_disconnect)
        if t._sim is not None:
            t._sim._push_targets.append(t)
        return t

    def deliver(self, data: bytes) -> None:
        if not _SWALLOW:
            self.on_data(data)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        self.sent.extend(data)
        if self._sim is None:
            return
        resp = self._sim.handle_command(bytes(data))
        if resp:
            self.deliver(resp)

    async def close(self) -> None:
        self.connected = False
        if self._sim is not None and self in self._sim._push_targets:
            self._sim._push_targets.remove(self)


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
    logger = ModuleType("server.utils.logger")

    class _Log:
        def __getattr__(self, _):
            return lambda *a, **k: None
    logger.get_logger = lambda *_a, **_k: _Log()
    stubs["server.utils.logger"] = logger

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    stubs["simulator"] = sim_pkg
    sim_tcp = ModuleType("simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulatorBase
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


av = _load("allenheath_avantis_under_test", DRIVER_PATH)
avsim = _load("allenheath_avantis_sim_under_test", SIM_PATH)

SYSEX_HDR = bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x10, 0x01, 0x00])


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _make_pair(config=None, sim_config=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = avsim.AllenHeathAvantisSimulator(
        "sim1", {"base_midi_channel": 1, **(sim_config or {})})
    _CURRENT_SIM = sim
    cfg = {"host": "10.0.0.9", "port": 51325, "base_midi_channel": 1,
           "poll_interval": 0}
    cfg.update(config or {})
    driver = av.AllenHeathAvantisDriver(
        "avantis1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# Expected roster (the full documented Avantis address space).
ROSTER_COUNTS = {
    "input": 96, "mono_group": 54, "stereo_group": 27,
    "mono_aux": 54, "stereo_aux": 27, "mono_matrix": 54,
    "stereo_matrix": 27, "mono_fx_send": 12, "stereo_fx_send": 12,
    "fx_return": 12, "main": 3, "dca": 16, "mute_group": 8,
    "ufx_send": 8, "ufx_return": 8,
}


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_shape():
    info = av.AllenHeathAvantisDriver.DRIVER_INFO
    assert info["version"] == "2.0.2"
    assert info["min_platform_version"] == "0.24.0"
    assert info["commands"], "class-level command catalog must not be empty"
    for qa in info["quick_actions"]:
        assert qa in info["commands"]
    for act in info["actions"]:
        assert act["id"] in info["commands"]
    # No socket type on Avantis — the MIDI protocol has no preamp control.
    assert set(av.CHILD_ENTITY_TYPES) == set(av.CHANNEL_TYPES)
    for ctype, tdef in av.CHILD_ENTITY_TYPES.items():
        # String child ids need an explicit id_format declaration.
        assert tdef.get("id_format", {}).get("type") == "string", ctype
        # Console channel names are the display labels.
        assert tdef["label_field"] == "name", ctype
        svars = tdef["state_variables"]
        assert svars["mute"]["cloud_priority"] == "high", ctype
        for prop in ("fader", "name", "colour"):
            if prop in svars:
                assert svars[prop]["cloud_priority"] == "low", (ctype, prop)
        if ctype == "mute_group":
            assert "fader" not in svars
        else:
            assert "fader" in svars


def test_command_method_parity():
    cmds = av.AllenHeathAvantisDriver.DRIVER_INFO["commands"]
    for cid, cdef in cmds.items():
        method = getattr(av.AllenHeathAvantisDriver, f"cmd_{cid}", None)
        assert method is not None, f"missing cmd_{cid}"
        accepted = set(inspect.signature(method).parameters) - {"self"}
        assert set(cdef.get("params", {})) == accepted, cid


# ── Address tables / codecs (byte-exact vs the reference tables) ────────────

def test_channel_addresses_match_reference_table():
    # Channel Selection table: Inputs N/00-5F (96), Mono Group N+1/00-35,
    # Stereo Group N+1/40-5A, Mono FX Send N+4/00-0B, Stereo FX Send
    # N+4/10-1B, FX Return N+4/20-2B, Mains N+4/30-32, DCA N+4/36-45
    # (16), Mute Group N+4/46-4D (dLive's sit at 4E-55), UFX Send
    # N+4/56-5D, UFX Return N+4/5E-65.
    assert av.channel_address("input", 1) == (0, 0x00)
    assert av.channel_address("input", 96) == (0, 0x5F)
    assert av.channel_address("mono_group", 54) == (1, 0x35)
    assert av.channel_address("stereo_group", 1) == (1, 0x40)
    assert av.channel_address("stereo_group", 27) == (1, 0x5A)
    assert av.channel_address("stereo_aux", 27) == (2, 0x5A)
    assert av.channel_address("mono_matrix", 1) == (3, 0x00)
    assert av.channel_address("mono_fx_send", 12) == (4, 0x0B)
    assert av.channel_address("stereo_fx_send", 1) == (4, 0x10)
    assert av.channel_address("fx_return", 12) == (4, 0x2B)
    assert av.channel_address("main", 3) == (4, 0x32)
    assert av.channel_address("dca", 1) == (4, 0x36)
    assert av.channel_address("dca", 16) == (4, 0x45)
    assert av.channel_address("mute_group", 1) == (4, 0x46)
    assert av.channel_address("mute_group", 8) == (4, 0x4D)
    assert av.channel_address("ufx_send", 1) == (4, 0x56)
    assert av.channel_address("ufx_return", 8) == (4, 0x65)
    for bad in (("input", 97), ("mono_group", 55), ("main", 4),
                ("dca", 17), ("mute_group", 0)):
        try:
            av.channel_address(*bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_fader_codec_anchors():
    # Fader Level table: +10 dB = 7F, 0 dB = 6B (107), -5 dB = 61 (97),
    # -inf = 00.
    assert av.db_to_lv(10.0) == 0x7F
    assert av.db_to_lv(0.0) == 0x6B
    assert av.db_to_lv(-5.0) == 0x61
    assert av.db_to_lv(-54.0) == 0x00
    assert av.db_to_lv(-100.0) == 0x00
    assert av.level_to_lv(0.0) == 0x00
    assert av.level_to_lv(1.0) == 0x7F
    assert abs(av.lv_to_level(0x7F) - 1.0) < 0.001


def test_scene_banking():
    # Scene Recall table: 4 banks of 128; scene 500 = bank 03, SS 73.
    assert av.scene_to_bank_program(1) == (0, 0)
    assert av.scene_to_bank_program(129) == (1, 0)
    assert av.scene_to_bank_program(500) == (3, 0x73)
    for bad in (0, 501):
        try:
            av.scene_to_bank_program(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_colour_table():
    # Off 00 .. White 07 (the dLive V2.0 table — the Avantis article
    # omits the colour value table; see the driver docstring).
    assert av.colour_to_value("off") == 0
    assert av.colour_to_value("red") == 1
    assert av.colour_to_value("white") == 7
    assert av.value_to_colour(6) == "light_blue"
    try:
        av.colour_to_value("mauve")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_ufx_global_scale_is_major_minor_only():
    # Message spec: Global Scale value is 00-01 (Major / Minor). The
    # article's reference table lists Chromatic 02, but only the per-UFX-
    # unit CC parameter has it — v1 wrongly offered it here.
    assert av.UFX_SCALE_NAMES == ["Major", "Minor"]
    assert av.ufx_scale_to_value("Minor") == 1
    try:
        av.ufx_scale_to_value("Chromatic")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_probe_and_get_wire_bytes():
    async def main():
        driver, _sim = await _make_pair()
        # Name Get: SysEx Header, 0N, 01, CH, F7.
        assert driver._build_get_name(0, 0x05) == (
            SYSEX_HDR + bytes([0x00, 0x01, 0x05, 0xF7]))
        # Colour Get: SysEx Header, 0N, 04, CH, F7.
        assert driver._build_get_colour(4, 0x36) == (
            SYSEX_HDR + bytes([0x04, 0x04, 0x36, 0xF7]))
        # The liveness probe targets Input 1 (name is the only queryable
        # per-channel parameter on Avantis).
        assert driver._probe_addr == (0, 0x00)
    _run(main())


# ── CE: connect, roster, sweep, labels ──────────────────────────────────────

def test_connect_registers_roster_and_sweep_populates():
    async def main():
        driver, sim = await _make_pair()
        # Pre-set console-side state. Names / colours must sweep in;
        # mute / fader must NOT (Avantis documents no Get for them).
        sim._name[("input", 1)] = b"Kick"
        sim._colour[("input", 1)] = 1           # red
        sim._name[("dca", 16)] = b"Band"
        sim._colour[("ufx_return", 8)] = 7      # white
        sim._mute[("input", 3)] = True
        sim._fader[("main", 1)] = 0x6B
        await driver.connect()
        for ctype, count in ROSTER_COUNTS.items():
            assert driver.count_children(ctype) == count, ctype
        # Console names / colours land in child props (label_field: name).
        assert driver.get_child_state("input", "in01")["name"] == "Kick"
        assert driver.get_child_state("input", "in01")["colour"] == "red"
        assert driver.get_child_state("input", "in02")["colour"] == "off"
        assert driver.get_child_state("dca", "dca16")["name"] == "Band"
        assert driver.get_child_state("ufx_return", "ufxr8")["colour"] == "white"
        # No mute / fader Get exists — the sweep must leave them at
        # defaults even though the console-side values differ. They sync
        # via push (see the push tests).
        assert driver.get_child_state("input", "in03")["mute"] is False
        assert driver.get_child_state("main", "main1")["fader"] == 0.0
        await driver.disconnect()
    _run(main())


def test_label_seeding_respects_project_label():
    async def main():
        driver, _sim = await _make_pair()
        driver._project_child_entities = {
            "input": {"in01": {"label": "Lectern"}},
        }
        await driver.connect()
        # in01 has a project label -> the driver must NOT seed one (the
        # platform fills the project label itself; the stub defaults to "").
        assert driver.get_child_state("input", "in01")["label"] == ""
        # No project label -> the driver seeds the generic placeholder
        # (the console name displays via label_field once swept).
        assert driver.get_child_state("input", "in02")["label"] == "Input 2"
        assert driver.get_child_state("mute_group", "mtgrp8")["label"] == "Mute Group 8"
        assert driver.get_child_state("main", "main1")["label"] == "Main 1"
        await driver.disconnect()
    _run(main())


# ── Writes: optimistic state, toggle, wire bytes ────────────────────────────

def test_mute_write_is_optimistic_even_without_echo():
    async def main():
        global _SWALLOW
        driver, sim = await _make_pair()
        await driver.connect()
        _SWALLOW = True          # console answers nothing (Qu-hardware behavior)
        await driver.send_command("mute_input",
                                  {"channel": "in05", "action": "on"})
        assert sim._mute[("input", 5)] is True             # reached the wire
        assert driver.get_child_state("input", "in05")["mute"] is True
        await driver.send_command("mute_dca",
                                  {"channel": "dca16", "action": "on"})
        assert driver.get_child_state("dca", "dca16")["mute"] is True
        _SWALLOW = False
        await driver.disconnect()
    _run(main())


def test_mute_toggle_flips_last_known_state():
    async def main():
        global _SWALLOW
        driver, sim = await _make_pair()
        await driver.connect()
        # Console-side push primes the driver's last-known state.
        await sim.push_mute("dca", 2, True)
        assert driver.get_child_state("dca", "dca02")["mute"] is True
        await driver.send_command("mute_dca", {"channel": "dca02",
                                               "action": "toggle"})
        assert sim._mute[("dca", 2)] is False
        assert driver.get_child_state("dca", "dca02")["mute"] is False
        # There is no mute Get on Avantis, so toggling again works purely
        # from the optimistic mirror — even with every reply swallowed.
        _SWALLOW = True
        await driver.send_command("mute_dca", {"channel": "dca02",
                                               "action": "toggle"})
        assert sim._mute[("dca", 2)] is True
        assert driver.get_child_state("dca", "dca02")["mute"] is True
        _SWALLOW = False
        await driver.disconnect()
    _run(main())


def test_fader_round_trips():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await driver.send_command("set_input_fader",
                                  {"channel": "in01", "level": 0.5})
        assert sim._fader[("input", 1)] == 64
        assert abs(driver.get_child_state("input", "in01")["fader"] - 0.5) < 0.01
        await driver.send_command("set_main_fader_db",
                                  {"channel": "main1", "db": 0.0})
        assert sim._fader[("main", 1)] == 0x6B
        assert abs(driver.get_child_state("main", "main1")["fader"]
                   - 0x6B / 127) < 0.001
        await driver.send_command("set_stereo_aux_fader",
                                  {"channel": "saux27", "level": 1.0})
        assert sim._fader[("stereo_aux", 27)] == 0x7F
        await driver.disconnect()
    _run(main())


def test_name_and_colour_set_round_trip():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await driver.send_command("set_channel_name",
                                  {"channel_type": "input", "channel": 1,
                                   "name": "Vox 1"})
        assert sim._name[("input", 1)] == b"Vox 1"
        assert driver.get_child_state("input", "in01")["name"] == "Vox 1"
        await driver.send_command("set_channel_colour",
                                  {"channel_type": "dca", "channel": 16,
                                   "colour": "purple"})
        assert sim._colour[("dca", 16)] == 5
        assert driver.get_child_state("dca", "dca16")["colour"] == "purple"
        await driver.disconnect()
    _run(main())


def test_assign_wire_bytes_match_avantis_tables():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        # DCA assign values are Avantis-specific: ON DB 40-4F, OFF DA
        # 00-0F for DCA 1-16 (dLive spans 40-57/00-17 for 24).
        driver.transport.sent.clear()
        await driver.send_command("set_dca_assign",
                                  {"source_type": "input", "source": 1,
                                   "dca": 16, "action": "on"})
        assert bytes(driver.transport.sent) == bytes(
            [0xB0, 0x63, 0x00, 0xB0, 0x62, 0x40, 0xB0, 0x06, 0x4F])
        assert sim._dca_assign[(("input", 1), 16)] is True
        driver.transport.sent.clear()
        await driver.send_command("set_dca_assign",
                                  {"source_type": "input", "source": 1,
                                   "dca": 16, "action": "off"})
        assert bytes(driver.transport.sent)[-1] == 0x0F
        assert sim._dca_assign[(("input", 1), 16)] is False
        # Mute-group assign: ON 50-57, OFF 10-17 for groups 1-8 (dLive's
        # sit at 58-5F/18-1F).
        driver.transport.sent.clear()
        await driver.send_command("set_mute_group_assign",
                                  {"source_type": "input", "source": 2,
                                   "mute_group": 8, "action": "on"})
        assert bytes(driver.transport.sent) == bytes(
            [0xB0, 0x63, 0x01, 0xB0, 0x62, 0x40, 0xB0, 0x06, 0x57])
        assert sim._mute_group_assign[(("input", 2), 8)] is True
        driver.transport.sent.clear()
        await driver.send_command("set_mute_group_assign",
                                  {"source_type": "input", "source": 2,
                                   "mute_group": 1, "action": "off"})
        assert bytes(driver.transport.sent)[-1] == 0x10
        # Channel -> Main assign (NRPN 18): ON 7F, OFF 3F.
        driver.transport.sent.clear()
        await driver.send_command("set_channel_to_main_assign",
                                  {"source_type": "input", "source": 1,
                                   "action": "off"})
        assert bytes(driver.transport.sent) == bytes(
            [0xB0, 0x63, 0x00, 0xB0, 0x62, 0x18, 0xB0, 0x06, 0x3F])
        assert sim._main_assign[("input", 1)] is False
        await driver.disconnect()
    _run(main())


def test_scene_recall_bytes_and_state():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        driver.transport.sent.clear()
        # Scene 7 on base channel 1: B0 00 00 C0 06.
        await driver.send_command("recall_scene", {"scene": 7})
        assert bytes(driver.transport.sent) == bytes(
            [0xB0, 0x00, 0x00, 0xC0, 0x06])
        assert driver.get_state("current_scene") == 7
        assert sim._current_scene == 7
        driver.transport.sent.clear()
        # Scene 500 = bank 03, program 73.
        await driver.send_command("recall_scene", {"scene": 500})
        assert bytes(driver.transport.sent) == bytes(
            [0xB0, 0x00, 0x03, 0xC0, 0x73])
        assert sim._current_scene == 500
        await driver.disconnect()
    _run(main())


def test_send_level_wire_bytes():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        driver.transport.sent.clear()
        # Input 1 -> Mono Aux 1 at full: Header, 0N(=0), 0D, CH, SndN(=2),
        # SndCH, LV, F7.
        await driver.send_command("set_send_level",
                                  {"source_type": "input", "source": 1,
                                   "target_type": "mono_aux", "target": 1,
                                   "level": 1.0})
        assert bytes(driver.transport.sent) == (
            SYSEX_HDR + bytes([0x00, 0x0D, 0x00, 0x02, 0x00, 0x7F, 0xF7]))
        assert sim._send_level[(("input", 1), ("mono_aux", 1))] == 0x7F
        driver.transport.sent.clear()
        # Input 1 -> UFX Send 1 (SndN = base+4, SndCH = 56).
        await driver.send_command("set_send_level",
                                  {"source_type": "input", "source": 1,
                                   "target_type": "ufx_send", "target": 1,
                                   "level": 0.0})
        assert bytes(driver.transport.sent) == (
            SYSEX_HDR + bytes([0x00, 0x0D, 0x00, 0x04, 0x56, 0x00, 0xF7]))
        assert sim._send_level[(("input", 1), ("ufx_send", 1))] == 0x00
        await driver.disconnect()
    _run(main())


def test_mute_all_inputs():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await driver.send_command("mute_all_inputs", {})
        for n in range(1, 97):
            assert sim._mute[("input", n)] is True
        assert driver.get_child_state("input", "in96")["mute"] is True
        await driver.send_command("unmute_all_inputs", {})
        assert driver.get_child_state("input", "in96")["mute"] is False
        await driver.disconnect()
    _run(main())


def test_unknown_child_id_raises():
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        for cmd, params in (
                ("mute_input", {"channel": "in97", "action": "on"}),
                ("set_dca_fader", {"channel": "dca17", "level": 0.5})):
            try:
                await driver.send_command(cmd, params)
                raise AssertionError("expected ValueError")
            except ValueError:
                pass
        await driver.disconnect()
    _run(main())


# ── Push fan-out (console-side moves) ───────────────────────────────────────

def test_console_push_fans_into_child_state():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await sim.push_mute("dca", 2, True)
        assert driver.get_child_state("dca", "dca02")["mute"] is True
        await sim.push_mute("mute_group", 8, True)
        assert driver.get_child_state("mute_group", "mtgrp8")["mute"] is True
        await sim.push_fader("input", 7, 0x7F)
        assert abs(driver.get_child_state("input", "in07")["fader"] - 1.0) < 0.001
        # Scene push (Bank Select + Program Change).
        await sim.set_state_value("current_scene", 257)
        assert driver.get_state("current_scene") == 257
        await driver.disconnect()
    _run(main())


def test_running_status_push_spec_example():
    async def main():
        # Spec example: muting Inputs 1, 2, 3 on MIDI channel 12 with
        # running status = 9B 00 7F 01 7F 02 7F.
        driver, _sim = await _make_pair(
            config={"base_midi_channel": 12},
            sim_config={"base_midi_channel": 12})
        await driver.connect()
        driver.on_data_received(bytes([0x9B, 0x00, 0x7F, 0x01, 0x7F, 0x02, 0x7F]))
        for n in (1, 2, 3):
            assert driver.get_child_state("input", f"in{n:02d}")["mute"] is True
        await driver.disconnect()
    _run(main())


# ── LV: the name-Get probe watchdog ─────────────────────────────────────────

def test_liveness_probe_resolves_on_reply():
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        await asyncio.wait_for(driver._liveness_probe(), 1.0)
        await driver.disconnect()
    _run(main())


def test_liveness_probe_not_satisfied_by_other_pushes():
    async def main():
        global _SWALLOW
        driver, sim = await _make_pair()
        await driver.connect()
        _SWALLOW = True                     # the Get reply never arrives
        probe = asyncio.ensure_future(driver._liveness_probe())
        await asyncio.sleep(0.05)
        _SWALLOW = False
        # Pushes of DIFFERENT parameters must not satisfy the probe — not
        # even the probed channel's own fader / mute / colour, nor another
        # channel's name reply.
        await sim.push_fader("input", 1, 0x40)
        await sim.push_mute("input", 1, True)
        driver.on_data_received(
            SYSEX_HDR + bytes([0x00, 0x05, 0x00, 0x02, 0xF7]))  # in1 colour
        driver.on_data_received(
            SYSEX_HDR + bytes([0x00, 0x02, 0x01]) + b"Snare" + bytes([0xF7]))
        await asyncio.sleep(0.05)
        assert not probe.done()
        # ...but they DID land in state (correlation, not parsing, gated).
        assert driver.get_child_state("input", "in01")["colour"] == "green"
        assert driver.get_child_state("input", "in02")["name"] == "Snare"
        # A name reply for the probed channel resolves it.
        driver.on_data_received(
            SYSEX_HDR + bytes([0x00, 0x02, 0x00]) + b"Kick" + bytes([0xF7]))
        await asyncio.wait_for(probe, 1.0)
        assert driver.get_child_state("input", "in01")["name"] == "Kick"
        await driver.disconnect()
    _run(main())


def test_silent_console_forces_typed_no_response_disconnect():
    async def main():
        global _SWALLOW
        driver, _sim = await _make_pair()
        driver.HEALTH_INTERVAL_S = 0.02
        driver.HEALTH_TIMEOUT_S = 0.05
        driver.HEALTH_MAX_FAILURES = 2
        await driver.connect()
        _SWALLOW = True                     # console vanished silently
        for _ in range(100):
            await asyncio.sleep(0.02)
            if driver.disconnect_calls:
                break
        assert driver.disconnect_calls >= 1
        assert driver.stashed_fault is not None
        assert driver.stashed_fault[0] == "no_response"
        _SWALLOW = False
        await driver.disconnect()
    _run(main())
