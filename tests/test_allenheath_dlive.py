"""Driver + simulator tests for allenheath_dlive (dLive MIDI over TCP).

No dLive hardware on hand, so correctness is proven as a **dual-proof
round trip** wiring the real driver to the real simulator: the sim parses
Note-On / NRPN / Pitchbend / SysEx byte streams, answers Get requests and
pushes console-side moves; the driver frames, sends and fans replies into
child state; results are asserted on both sides (same approach as
test_allenheath_sq.py / test_biamp_tesira_ttp.py).

Covers the v2.0.0 first-class adoption:
  - CE: the fixed channel + preamp-socket roster registers as string-id
    child entities, the on-connect SysEx Get sweep populates child state
    (mute / fader / name / colour, gain / pad / 48V), pushes fan out to
    the right child, and a driver-seeded label never overrides a project
    one; console channel names land in the ``name`` prop (label_field);
  - LV: the Get-probe watchdog — the sim's Main 1 fader reply resolves
    the probe, a push of a DIFFERENT parameter does not, and a silent
    console forces a reconnect with a typed no_response fault;
  - optimistic writes (A&H consoles don't echo changes they receive over
    MIDI — confirmed on Qu hardware), mute toggle read-back via Get;
  - address tables and codecs byte-exact against the dLive MIDI Over
    TCP/IP Protocol V2.0 reference tables (mute-group shift vs Avantis,
    socket banks, gain / fader anchors, scene / cue banking, the
    running-status example).

Loads the driver + simulator with the ``openavc.*``
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
DRIVER_PATH = REPO_ROOT / "audio" / "allenheath_dlive.py"
SIM_PATH = REPO_ROOT / "audio" / "allenheath_dlive_sim.py"


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
    """Stand-in for openavc.simulator.tcp_simulator.TCPSimulator."""

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
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    stubs: dict[str, ModuleType] = {"openavc": server}
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"openavc.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        stubs[f"openavc.{sub}"] = m
    base = ModuleType("openavc.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    stubs["openavc.drivers.base"] = base
    tcp = ModuleType("openavc.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    stubs["openavc.transport.tcp"] = tcp
    logger = ModuleType("openavc.utils.logger")

    class _Log:
        def __getattr__(self, _):
            return lambda *a, **k: None
    logger.get_logger = lambda *_a, **_k: _Log()
    stubs["openavc.utils.logger"] = logger

    sim_pkg = ModuleType("openavc.simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    stubs["openavc.simulator"] = sim_pkg
    sim_tcp = ModuleType("openavc.simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulatorBase
    stubs["openavc.simulator.tcp_simulator"] = sim_tcp
    return stubs


_STUB_MODULES = _build_stub_modules()


def _load(name: str, path: Path) -> ModuleType:
    sys.modules.update(_STUB_MODULES)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dl = _load("allenheath_dlive_under_test", DRIVER_PATH)
dlsim = _load("allenheath_dlive_sim_under_test", SIM_PATH)

SYSEX_HDR = bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x10, 0x01, 0x00])


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _make_pair(config=None, sim_config=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = dlsim.AllenHeathDLiveSimulator(
        "sim1", {"base_midi_channel": 1, **(sim_config or {})})
    _CURRENT_SIM = sim
    cfg = {"host": "10.0.0.9", "port": 51325, "base_midi_channel": 1,
           "poll_interval": 0}
    cfg.update(config or {})
    driver = dl.AllenHeathDLiveDriver("dlive1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_shape():
    info = dl.AllenHeathDLiveDriver.DRIVER_INFO
    assert info["version"] == "2.1.0"
    assert info["min_platform_version"] == "0.25.0"
    assert info["commands"], "class-level command catalog must not be empty"
    for qa in info["quick_actions"]:
        assert qa in info["commands"]
    for act in info["actions"]:
        assert act["id"] in info["commands"]
    assert set(dl.CHILD_ENTITY_TYPES) == set(dl.CHANNEL_TYPES) | {"socket"}
    for ctype, tdef in dl.CHILD_ENTITY_TYPES.items():
        # String child ids need an explicit id_format declaration.
        assert tdef.get("id_format", {}).get("type") == "string", ctype
        svars = tdef["state_variables"]
        if ctype == "socket":
            # Sockets carry no console name — no label_field.
            assert "label_field" not in tdef
            assert svars["phantom"]["cloud_priority"] == "high"
            assert svars["gain_db"]["cloud_priority"] == "low"
            assert svars["pad"]["cloud_priority"] == "low"
            continue
        # Console channel names are the display labels.
        assert tdef["label_field"] == "name", ctype
        assert svars["mute"]["cloud_priority"] == "high", ctype
        for prop in ("fader", "name", "colour"):
            if prop in svars:
                assert svars[prop]["cloud_priority"] == "low", (ctype, prop)
        if ctype == "mute_group":
            assert "fader" not in svars
        else:
            assert "fader" in svars


def test_command_method_parity():
    cmds = dl.AllenHeathDLiveDriver.DRIVER_INFO["commands"]
    for cid, cdef in cmds.items():
        method = getattr(dl.AllenHeathDLiveDriver, f"cmd_{cid}", None)
        assert method is not None, f"missing cmd_{cid}"
        accepted = set(inspect.signature(method).parameters) - {"self"}
        assert set(cdef.get("params", {})) == accepted, cid


def test_the_port_hints_include_the_ones_avantis_does_not_have():
    """51325 is shared with every other Allen & Heath console, so on its own it
    separates nothing -- all five drivers hint on it and a scan ties. The
    Surface's control port and the two TLS ports are dLive's alone (the Avantis
    protocol document names 51325 and nothing else), and the matcher leads with
    the narrowest signal, so those are what let a dLive rank first."""
    ports = set(dl.AllenHeathDLiveDriver.DRIVER_INFO["discovery"]["port_open"])
    assert 51325 in ports, "the shared control port is still a hint"
    # Surface plain, MixRack TLS, Surface TLS — none of them Avantis ports.
    assert {51328, 51327, 51329} <= ports


# ── Address tables / codecs (byte-exact vs the V2.0 reference tables) ───────

def test_channel_addresses_match_reference_table():
    # Channel Selection table: Inputs N/00-7F, Mono Group N+1/00-3D,
    # Stereo Group N+1/40-5E, DCA N+4/36-4D (24), Mute Group N+4/4E-55
    # (shifted up from Avantis), UFX Return N+4/5E-65.
    assert dl.channel_address("input", 1) == (0, 0x00)
    assert dl.channel_address("input", 128) == (0, 0x7F)
    assert dl.channel_address("mono_group", 62) == (1, 0x3D)
    assert dl.channel_address("stereo_group", 1) == (1, 0x40)
    assert dl.channel_address("stereo_aux", 31) == (2, 0x5E)
    assert dl.channel_address("mono_matrix", 1) == (3, 0x00)
    assert dl.channel_address("stereo_fx_send", 16) == (4, 0x1F)
    assert dl.channel_address("main", 6) == (4, 0x35)
    assert dl.channel_address("dca", 24) == (4, 0x4D)
    assert dl.channel_address("mute_group", 1) == (4, 0x4E)
    assert dl.channel_address("mute_group", 8) == (4, 0x55)
    assert dl.channel_address("ufx_send", 1) == (4, 0x56)
    assert dl.channel_address("ufx_return", 8) == (4, 0x65)
    for bad in (("input", 129), ("dca", 25), ("mute_group", 0)):
        try:
            dl.channel_address(*bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_socket_banks_and_gain_anchors():
    # Socket Preamp numbers: MixRack 1-64 = MP 00-3F, DX1/2 = 40-5F,
    # DX3/4 = 60-7F. Gain Value table: +5 dB = 00, +10 = 0C, +30 = 3A,
    # +60 = 7F.
    assert dl.socket_sid(1) == "skt001"
    assert dl.socket_sid(128) == "skt128"
    assert dl.socket_label(1) == "MixRack Socket 1"
    assert dl.socket_label(65) == "DX1/2 Socket 1"
    assert dl.socket_label(97) == "DX3/4 Socket 1"
    assert dl.gain_to_gv(5.0) == 0x00
    assert dl.gain_to_gv(10.0) == 0x0C
    assert dl.gain_to_gv(30.0) == 0x3A
    assert dl.gain_to_gv(60.0) == 0x7F
    assert abs(dl.gv_to_gain(0x3A) - 30.0) < 0.2


def test_fader_codec_anchors():
    # Fader Level table: +10 dB = 7F, 0 dB = 6B (107), -inf = 00.
    assert dl.db_to_lv(10.0) == 0x7F
    assert dl.db_to_lv(0.0) == 0x6B
    assert dl.db_to_lv(-54.0) == 0x00
    assert dl.db_to_lv(-100.0) == 0x00
    assert dl.level_to_lv(0.0) == 0x00
    assert dl.level_to_lv(1.0) == 0x7F
    assert abs(dl.lv_to_level(0x7F) - 1.0) < 0.001


def test_scene_cue_banking():
    assert dl.scene_to_bank_program(1) == (0, 0)
    assert dl.scene_to_bank_program(129) == (1, 0)
    assert dl.scene_to_bank_program(500) == (3, 0x73)
    assert dl.cue_to_bank_program(1) == (0, 0)
    assert dl.cue_to_bank_program(1153) == (9, 0)
    assert dl.cue_to_bank_program(2000) == (0x0F, 0x4F)
    for fn, bad in ((dl.scene_to_bank_program, 501),
                    (dl.scene_to_bank_program, 0),
                    (dl.cue_to_bank_program, 2001)):
        try:
            fn(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_colour_table():
    # Colour table: Off 00, Red 01 ... White 07.
    assert dl.colour_to_value("off") == 0
    assert dl.colour_to_value("red") == 1
    assert dl.colour_to_value("white") == 7
    assert dl.value_to_colour(6) == "light_blue"
    try:
        dl.colour_to_value("mauve")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_ufx_global_scale_is_major_minor_only():
    # Spec: Global Scale value is 00-01 (Major / Minor). Chromatic exists
    # only for per-UFX-unit CC parameters — v1 wrongly offered it here.
    assert dl.UFX_SCALE_NAMES == ["Major", "Minor"]
    assert dl.ufx_scale_to_value("Minor") == 1
    try:
        dl.ufx_scale_to_value("Chromatic")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_probe_and_get_wire_bytes():
    async def main():
        driver, _sim = await _make_pair()
        # Main 1 fader Get: SysEx Header, 0N(=4), 05, 0B, 17, 30, F7.
        assert driver._build_get_nrpn(4, 0x17, 0x30) == (
            SYSEX_HDR + bytes([0x04, 0x05, 0x0B, 0x17, 0x30, 0xF7]))
        # Mute Get: SysEx Header, 0N, 05, 09, CH, F7.
        assert driver._build_get_mute(0, 0x00) == (
            SYSEX_HDR + bytes([0x00, 0x05, 0x09, 0x00, 0xF7]))
        # Name / Colour Gets: 0N 01 CH / 0N 04 CH.
        assert driver._build_get_name(0, 0x05) == (
            SYSEX_HDR + bytes([0x00, 0x01, 0x05, 0xF7]))
        assert driver._build_get_colour(0, 0x05) == (
            SYSEX_HDR + bytes([0x00, 0x04, 0x05, 0xF7]))
        # Pad / 48V / Gain Gets (socket MP on the base channel):
        # 0N 07 MP / 0N 0A MP / 0N 05 0B 19 MP.
        assert driver._build_get_pad(0x7F) == (
            SYSEX_HDR + bytes([0x00, 0x07, 0x7F, 0xF7]))
        assert driver._build_get_48v(0x00) == (
            SYSEX_HDR + bytes([0x00, 0x0A, 0x00, 0xF7]))
        assert driver._build_get_gain(0x40) == (
            SYSEX_HDR + bytes([0x00, 0x05, 0x0B, 0x19, 0x40, 0xF7]))
    _run(main())


# ── CE: connect, roster, sweep, labels ──────────────────────────────────────

def test_connect_registers_roster_and_sweep_populates():
    async def main():
        driver, sim = await _make_pair()
        # Pre-set console-side state the sweep must read back.
        sim._mute[("input", 3)] = True
        sim._fader[("main", 1)] = 0x6B          # 0 dB
        sim._name[("input", 1)] = b"Kick"
        sim._colour[("input", 1)] = 1           # red
        sim._pad[1] = True                      # socket 2
        sim._48v[0] = True                      # socket 1
        sim._gain[0] = 0x3A                     # socket 1, +30 dB
        await driver.connect()
        counts = {
            "input": 128, "mono_group": 62, "stereo_group": 31,
            "mono_aux": 62, "stereo_aux": 31, "mono_matrix": 62,
            "stereo_matrix": 31, "mono_fx_send": 16, "stereo_fx_send": 16,
            "fx_return": 16, "main": 6, "dca": 24, "mute_group": 8,
            "ufx_send": 8, "ufx_return": 8, "socket": 128,
        }
        for ctype, count in counts.items():
            assert driver.count_children(ctype) == count, ctype
        assert driver.get_child_state("input", "in003")["mute"] is True
        assert driver.get_child_state("input", "in001")["mute"] is False
        assert abs(driver.get_child_state("main", "main1")["fader"]
                   - 0x6B / 127) < 0.001
        # Console names / colours land in child props (label_field: name).
        assert driver.get_child_state("input", "in001")["name"] == "Kick"
        assert driver.get_child_state("input", "in001")["colour"] == "red"
        assert driver.get_child_state("input", "in002")["colour"] == "off"
        # Preamp sockets.
        assert driver.get_child_state("socket", "skt002")["pad"] is True
        assert driver.get_child_state("socket", "skt001")["phantom"] is True
        assert abs(driver.get_child_state("socket", "skt001")["gain_db"]
                   - 30.1) < 0.2
        assert driver.get_child_state("socket", "skt003")["pad"] is False
        await driver.disconnect()
    _run(main())


def test_label_seeding_respects_project_label():
    async def main():
        driver, _sim = await _make_pair()
        driver._project_child_entities = {
            "input": {"in001": {"label": "Lectern"}},
            "socket": {"skt001": {"label": "Stage Box 1"}},
        }
        await driver.connect()
        # in001 has a project label -> the driver must NOT seed one (the
        # platform fills the project label itself; the stub defaults to "").
        assert driver.get_child_state("input", "in001")["label"] == ""
        assert driver.get_child_state("socket", "skt001")["label"] == ""
        # No project label -> the driver seeds the generic placeholder
        # (the console name displays via label_field once swept).
        assert driver.get_child_state("input", "in002")["label"] == "Input 2"
        assert driver.get_child_state("mute_group", "mtgrp8")["label"] == "Mute Group 8"
        assert driver.get_child_state("socket", "skt065")["label"] == "DX1/2 Socket 1"
        await driver.disconnect()
    _run(main())


# ── Writes: optimistic state, toggle read-back, wire bytes ──────────────────

def test_mute_write_is_optimistic_even_without_echo():
    async def main():
        global _SWALLOW
        driver, sim = await _make_pair()
        await driver.connect()
        _SWALLOW = True          # console answers nothing (Qu-hardware behavior)
        await driver.send_command("mute_input",
                                  {"channel": "in005", "action": "on"})
        assert sim._mute[("input", 5)] is True             # reached the wire
        assert driver.get_child_state("input", "in005")["mute"] is True
        await driver.send_command("mute_dca",
                                  {"channel": "dca24", "action": "on"})
        assert driver.get_child_state("dca", "dca24")["mute"] is True
        _SWALLOW = False
        await driver.disconnect()
    _run(main())


def test_mute_toggle_reads_result_back():
    async def main():
        driver, sim = await _make_pair()
        sim._mute[("dca", 2)] = True
        await driver.connect()   # sweep reads the mute in
        assert driver.get_child_state("dca", "dca02")["mute"] is True
        await driver.send_command("mute_dca", {"channel": "dca02",
                                               "action": "toggle"})
        assert sim._mute[("dca", 2)] is False
        assert driver.get_child_state("dca", "dca02")["mute"] is False
        await driver.disconnect()
    _run(main())


def test_fader_round_trips():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await driver.send_command("set_input_fader",
                                  {"channel": "in001", "level": 0.5})
        assert sim._fader[("input", 1)] == 64
        assert abs(driver.get_child_state("input", "in001")["fader"] - 0.5) < 0.01
        await driver.send_command("set_main_fader_db",
                                  {"channel": "main1", "db": 0.0})
        assert sim._fader[("main", 1)] == 0x6B
        assert abs(driver.get_child_state("main", "main1")["fader"]
                   - 0x6B / 127) < 0.001
        await driver.send_command("set_stereo_aux_fader",
                                  {"channel": "saux31", "level": 1.0})
        assert sim._fader[("stereo_aux", 31)] == 0x7F
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
        assert driver.get_child_state("input", "in001")["name"] == "Vox 1"
        await driver.send_command("set_channel_colour",
                                  {"channel_type": "dca", "channel": 24,
                                   "colour": "purple"})
        assert sim._colour[("dca", 24)] == 5
        assert driver.get_child_state("dca", "dca24")["colour"] == "purple"
        await driver.disconnect()
    _run(main())


def test_preamp_round_trips():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await driver.send_command("set_preamp_gain",
                                  {"socket": "skt001", "gain_db": 30.0})
        assert sim._gain[0] == 0x3A
        assert abs(driver.get_child_state("socket", "skt001")["gain_db"]
                   - 30.1) < 0.2
        await driver.send_command("set_preamp_pad",
                                  {"socket": "skt128", "action": "on"})
        assert sim._pad[127] is True
        assert driver.get_child_state("socket", "skt128")["pad"] is True
        await driver.send_command("set_preamp_48v",
                                  {"socket": "skt001", "action": "on"})
        assert sim._48v[0] is True
        assert driver.get_child_state("socket", "skt001")["phantom"] is True
        await driver.disconnect()
    _run(main())


def test_scene_and_cue_recall_bytes_and_state():
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
        # Cue 2000 = Recall Id 1999 = bank 0F, program 4F.
        await driver.send_command("recall_cue", {"cue": 2000})
        assert bytes(driver.transport.sent) == bytes(
            [0xB0, 0x00, 0x0F, 0xC0, 0x4F])
        assert driver.get_state("current_cue") == 2000
        assert sim._current_cue == 2000
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
        await driver.disconnect()
    _run(main())


def test_mute_all_inputs():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await driver.send_command("mute_all_inputs", {})
        for n in range(1, 129):
            assert sim._mute[("input", n)] is True
        assert driver.get_child_state("input", "in128")["mute"] is True
        await driver.send_command("unmute_all_inputs", {})
        assert driver.get_child_state("input", "in128")["mute"] is False
        await driver.disconnect()
    _run(main())


def test_unknown_child_id_raises():
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        for cmd, params in (
                ("mute_input", {"channel": "in129", "action": "on"}),
                ("set_preamp_gain", {"socket": "skt200", "gain_db": 30.0})):
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
        assert abs(driver.get_child_state("input", "in007")["fader"] - 1.0) < 0.001
        await sim.push_gain(3, 0x7F)
        assert abs(driver.get_child_state("socket", "skt003")["gain_db"]
                   - 60.0) < 0.001
        # Scene push (Bank Select + Program Change).
        await sim.set_state_value("current_scene", 257)
        assert driver.get_state("current_scene") == 257
        await sim.set_state_value("current_cue", 1153)
        assert driver.get_state("current_cue") == 1153
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
            assert driver.get_child_state("input", f"in{n:03d}")["mute"] is True
        await driver.disconnect()
    _run(main())


# ── LV: the Get-probe watchdog ──────────────────────────────────────────────

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
        # even another channel's fader or the probed channel's mute.
        await sim.push_fader("input", 1, 0x40)
        await sim.push_mute("main", 1, True)
        await asyncio.sleep(0.05)
        assert not probe.done()
        # A fader message for the probed channel (here a console-side
        # Main 1 fader move) does — it equally proves the console is alive.
        await sim.push_fader("main", 1, 0x50)
        await asyncio.wait_for(probe, 1.0)
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
