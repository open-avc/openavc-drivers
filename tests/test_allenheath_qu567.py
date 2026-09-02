"""Driver + simulator tests for allenheath_qu567 (Qu-5/6/7 MIDI over TCP).

Correctness is proven two ways.

**Against hardware.** The parameter map, the fader-law tables and the
roster rules were measured on a real Allen & Heath Qu-5 (2026-09-01) by
sweeping the whole address space with NRPN Gets and matching every reply by
its echoed parameter number. Those measurements are pinned here as literal
expectations, so a change to the address arithmetic has to disagree with the
console to pass.

**Against the simulator.** The rest is a dual-proof round trip wiring the real
driver to the real simulator: the sim parses NRPN byte streams, answers Gets,
stays silent for parameters it does not have, and pushes console-side moves;
the driver frames, sends, discovers its roster and fans replies into child
state. Results are asserted on both sides.

The three behaviours worth knowing about, because a friendlier simulator would
hide all of them:

  - a Get for an absent parameter is answered with silence, which is what the
    driver's channel discovery reads;
  - a change received over MIDI is never echoed, so a driver that mirrored
    what it sent would be wrong and these tests would not notice;
  - Audio Taper quantises a level to 64-count steps, so the value read back
    after a write is routinely not the value written.

Loads the driver + simulator with the ``openavc.*`` imports stubbed so the
community CI stays self-contained (conftest.py rolls the stubs back after this
module is collected).
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
DRIVER_PATH = REPO_ROOT / "audio" / "allenheath_qu567.py"
SIM_PATH = REPO_ROOT / "audio" / "allenheath_qu567_sim.py"


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

    # -- disconnect bookkeeping --

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


_CURRENT_SIM: object | None = None
# When True the transport carries requests but DROPS every reply — a silently
# vanished console, for the liveness and discovery-fallback tests.
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


qu = _load("allenheath_qu567_under_test", DRIVER_PATH)
qusim = _load("allenheath_qu567_sim_under_test", SIM_PATH)

DRV = qu.AllenHeathQu567Driver
SIM = qusim.AllenHeathQu567Simulator


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _make_pair(config=None, sim_config=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM("sim1", {"midi_channel": 1, **(sim_config or {})})
    _CURRENT_SIM = sim
    cfg = {"host": "10.0.0.7", "port": 51325, "midi_channel": 1,
           "fader_law": "audio", "poll_interval": 0}
    cfg.update(config or {})
    driver = DRV("qu1", cfg, _FakeState(), _FakeEvents())
    driver.DISCOVERY_WINDOW_S = 0.05
    driver.CONFIRM_DELAY_S = 0.01
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_shape():
    info = DRV.DRIVER_INFO
    assert info["id"] == "allenheath_qu567"
    assert info["min_platform_version"] == "0.25.0"
    assert info["commands"], "class-level command catalog must not be empty"
    for qa in info["quick_actions"]:
        assert qa in info["commands"], f"quick action {qa} is not a command"
    for action in info["actions"]:
        if action.get("kind", "command") == "command":
            assert action["id"] in info["commands"]


def test_every_declared_command_has_a_method_that_accepts_its_params():
    """A command with no branch in send_command returns success and does
    nothing — byte-identical to a command that worked. Nothing else catches it.
    """
    for cid, spec in DRV.DRIVER_INFO["commands"].items():
        method = getattr(DRV, f"cmd_{cid}", None)
        assert method is not None, f"declared command {cid} has no cmd_{cid}"
        params = set(inspect.signature(method).parameters) - {"self"}
        declared = set(spec.get("params") or {})
        assert declared <= params, (
            f"{cid} declares params the method cannot take: "
            f"{sorted(declared - params)}")
        needed = {
            p for p in params - declared
            if inspect.signature(method).parameters[p].default
            is inspect.Parameter.empty
        }
        assert not needed, f"{cid} method needs args no caller can supply: {needed}"


def test_no_orphan_command_methods():
    orphans = [n[4:] for n in dir(DRV)
               if n.startswith("cmd_") and n[4:] not in DRV.DRIVER_INFO["commands"]]
    assert not orphans, f"cmd_ methods with no declared command: {orphans}"


def test_child_types_declare_string_ids():
    """The platform defaults to integer child ids and rejects strings at
    register_child, so every type has to say so explicitly."""
    for ctype, tdef in DRV.DRIVER_INFO["child_entity_types"].items():
        assert tdef["id_format"]["type"] == "string", ctype


# ── Address map, pinned to a real Qu-5 ──────────────────────────────────────

def test_address_map_matches_the_console():
    """Every one of these was read back off a real Qu-5: the Get was sent and
    the console answered with the same parameter number echoed."""
    cases = [
        ("Ip1 mute",       qu.mute_addr(qu._src_input(1)),               (0x00, 0x00)),
        ("Ip32 mute",      qu.mute_addr(qu._src_input(32)),              (0x00, 0x1F)),
        ("ST1 mute",       qu.mute_addr(qu._src_stereo(1)),              (0x00, 0x20)),
        ("ST2 mute",       qu.mute_addr(qu._src_stereo(2)),              (0x00, 0x22)),
        ("USB mute",       qu.mute_addr(qu.SRC_USB),                     (0x00, 0x24)),
        ("FX1Rtn mute",    qu.mute_addr(qu._src_fx_return(1)),           (0x00, 0x3C)),
        ("FX6Rtn mute",    qu.mute_addr(qu._src_fx_return(6)),           (0x00, 0x41)),
        ("LR mute",        qu.mute_addr(qu.SRC_LR),                      (0x00, 0x44)),
        ("MIX1 mute",      qu.mute_addr(qu._src_mix(1)),                 (0x00, 0x45)),
        ("MIX12 mute",     qu.mute_addr(qu._src_mix(12)),                (0x00, 0x50)),
        ("FX1Snd mute",    qu.mute_addr(qu._src_fx_send(1)),             (0x00, 0x51)),
        ("Mtx1 mute",      qu.mute_addr(qu._src_matrix(1)),              (0x00, 0x55)),
        ("Mtx3 mute",      qu.mute_addr(qu._src_matrix(3)),              (0x00, 0x57)),
        ("DCA1 mute",      qu.mute_dca(1),                               (0x02, 0x00)),
        ("DCA8 mute",      qu.mute_dca(8),                               (0x02, 0x07)),
        ("MGRP1 mute",     qu.mute_mgrp(1),                              (0x04, 0x00)),
        ("MGRP8 mute",     qu.mute_mgrp(8),                              (0x04, 0x07)),
        ("Ip1->LR",        qu.to_lr(qu._src_input(1)),                   (0x40, 0x00)),
        ("Ip1->MIX1",      qu.to_mix(qu._src_input(1), 1),               (0x40, 0x44)),
        ("Ip2->MIX1",      qu.to_mix(qu._src_input(2), 1),               (0x40, 0x50)),
        ("ST1->MIX1",      qu.to_mix(qu._src_stereo(1), 1),              (0x43, 0x44)),
        ("ST2->MIX1",      qu.to_mix(qu._src_stereo(2), 1),              (0x43, 0x5C)),
        ("USB->MIX1",      qu.to_mix(qu.SRC_USB, 1),                     (0x43, 0x74)),
        ("Grp1->MIX1",     qu.to_mix(qu._src_group(1), 1),               (0x45, 0x04)),
        ("FX1Rtn->MIX1",   qu.to_mix(qu._src_fx_return(1), 1),           (0x46, 0x14)),
        ("Ip1->FX1Snd",    qu.to_fx_send(qu._src_input(1), 1),           (0x4C, 0x14)),
        ("Ip25->FX1Snd",   qu.to_fx_send(qu._src_input(25), 1),          (0x4C, 0x74)),
        ("ST1->FX1Snd",    qu.to_fx_send(qu._src_stereo(1), 1),          (0x4D, 0x14)),
        ("Grp1->FX1Snd",   qu.to_fx_send(qu._src_group(1), 1),           (0x4D, 0x54)),
        ("FX1Rtn->FX1Snd", qu.to_fx_send(qu._src_fx_return(1), 1),       (0x4E, 0x04)),
        ("LR->Mtx1",       qu.to_matrix(0, 1),                           (0x4E, 0x24)),
        ("MIX1->Mtx1",     qu.to_matrix(1, 1),                           (0x4E, 0x27)),
        ("MIX12->Mtx3",    qu.to_matrix(12, 3),                          (0x4E, 0x4A)),
        ("LR master",      qu.master_lr(),                               (0x4F, 0x00)),
        ("MIX1 master",    qu.master_mix(1),                             (0x4F, 0x01)),
        ("MIX12 master",   qu.master_mix(12),                            (0x4F, 0x0C)),
        ("FX1Snd master",  qu.master_fx_send(1),                         (0x4F, 0x0D)),
        ("Mtx1 master",    qu.master_matrix(1),                          (0x4F, 0x11)),
        ("Mtx3 master",    qu.master_matrix(3),                          (0x4F, 0x13)),
        ("DCA1 master",    qu.master_dca(1),                             (0x4F, 0x20)),
        ("DCA8 master",    qu.master_dca(8),                             (0x4F, 0x27)),
    ]
    for label, got, want in cases:
        assert got == want, f"{label}: {got[0]:02X} {got[1]:02X} != {want[0]:02X} {want[1]:02X}"


def test_pan_and_assign_are_the_level_map_shifted_by_a_constant():
    """Sweeping the console showed the level, pan and assign planes are the
    same base map at MSB +0x00 / +0x10 / +0x20. If that ever stops holding,
    every pan and assign address is silently wrong."""
    for src in (qu._src_input(1), qu._src_input(32), qu._src_stereo(1),
                qu._src_group(1), qu._src_fx_return(6)):
        lvl = qu.to_lr(src)
        assert qu.to_lr(src, qu.PLANE_PAN) == (lvl[0] + 0x10, lvl[1])
        assert qu.to_lr(src, qu.PLANE_ASSIGN) == (lvl[0] + 0x20, lvl[1])
        for mix in (1, 6, 12):
            lvl = qu.to_mix(src, mix)
            assert qu.to_mix(src, mix, qu.PLANE_PAN) == (lvl[0] + 0x10, lvl[1])
            assert qu.to_mix(src, mix, qu.PLANE_ASSIGN) == (lvl[0] + 0x20, lvl[1])


def test_lsb_rollover_into_the_next_msb():
    """MSB and LSB are two independent 7-bit bytes, not a packed 14-bit
    number, so a flat offset past 0x7F rolls into MSB+1."""
    assert qu._addr(0x40, 0x7F) == (0x40, 0x7F)
    assert qu._addr(0x40, 0x80) == (0x41, 0x00)
    assert qu._addr(0x40, 0x1FF) == (0x43, 0x7F)


# ── Fader law ───────────────────────────────────────────────────────────────

def test_taper_tables_hit_the_published_values():
    for law, db, vc, vf in [
        ("audio", 0, 0x62, 0x00), ("audio", -20, 0x2E, 0x40),
        ("audio", -10, 0x3E, 0x00), ("audio", 10, 0x7F, 0x40),
        ("linear", 0, 0x76, 0x5C), ("linear", -20, 0x64, 0x16),
        ("linear", -10, 0x6D, 0x39), ("linear", 10, 0x7F, 0x7F),
    ]:
        assert qu.db_to_raw(db, law) == (vc << 7) | vf, f"{law} {db} dB"


def test_taper_round_trips():
    for law in qu.FADER_LAWS:
        for db in (-80, -40, -20, -10, -6, -3, 0, 3, 6, 10):
            back = qu.raw_to_db(qu.db_to_raw(db, law), law)
            assert abs(back - db) < 0.6, f"{law} {db} -> {back}"


def test_minus_infinity_is_zero_both_ways():
    for law in qu.FADER_LAWS:
        assert qu.db_to_raw(qu.DB_MIN, law) == 0
        assert qu.db_to_raw(-200, law) == 0
        assert qu.raw_to_db(0, law) == qu.DB_MIN


def test_the_two_laws_disagree_which_is_the_whole_reason_it_is_configurable():
    assert qu.db_to_raw(0, "audio") != qu.db_to_raw(0, "linear")


def test_fader_position_round_trips_regardless_of_law():
    """Position is the raw value normalised, so it survives a wrong fader-law
    setting; only dB depends on it. That is the graceful-degradation claim the
    config field's description makes."""
    for level in (0.0, 0.25, 0.5, 0.766, 1.0):
        vc, vf = qu.level_to_vcvf(level)
        assert abs(qu.vcvf_to_level(vc, vf) - level) < 0.001


def test_pan_hits_the_documented_anchors():
    assert qu.pan_to_vcvf(-1.0) == (0x00, 0x00)
    assert qu.pan_to_vcvf(0.0) == (0x3F, 0x7F)
    assert qu.pan_to_vcvf(1.0) == (0x7F, 0x7F)


def test_scene_and_softkey_encoding():
    assert qu.scene_to_bank_program(7) == (0, 6)
    assert qu.scene_to_bank_program(120) == (0, 119)
    assert qu.scene_to_bank_program(156) == (1, 27)
    assert qu.scene_to_bank_program(264) == (2, 7)
    assert qu.softkey_to_note(1) == 0x30
    assert qu.softkey_to_note(16) == 0x3F


# ── Roster discovery ────────────────────────────────────────────────────────

def test_connect_discovers_the_roster_the_console_actually_has():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        assert driver.count_children("input") == 32
        assert driver.count_children("stereo_input") == 3      # ST1, ST2, USB
        assert driver.count_children("fx_return") == 6
        assert driver.count_children("fx_send") == 4
        assert driver.count_children("mix") == 12
        assert driver.count_children("dca") == 8
        assert driver.count_children("mute_group") == 8
        # Matrices are stereo pairs: only the odd index is addressable.
        assert driver.count_children("matrix") == 2
        assert driver.get_state("channel_count") == 75
        await driver.disconnect()
    _run(main())


def test_a_channel_the_console_lacks_is_never_registered():
    """The whole point of probing. The Qu has no FX7/FX8 return and no
    Mtx2/Mtx4 — a driver that trusted the documented maximum would offer four
    matrices, two of which address nothing."""
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        ids = driver._order
        assert "mtx2" not in ids["matrix"] and "mtx4" not in ids["matrix"]
        assert ids["matrix"] == ["mtx1", "mtx3"]
        assert driver.count_children("fx_return") == 6
        await driver.disconnect()
    _run(main())


def test_the_roster_follows_a_differently_configured_console():
    """A Qu mix bus is configurable, so the channel list is a property of this
    desk rather than of the model. A smaller console must come up smaller."""
    async def main():
        driver, _sim = await _make_pair(
            sim_config={"num_mixes": 6, "num_inputs": 16})
        await driver.connect()
        assert driver.count_children("input") == 16
        assert driver.count_children("mix") == 6
        await driver.disconnect()
    _run(main())


def test_a_console_that_answers_nothing_falls_back_to_the_full_roster():
    """A firmware without Get, or probes lost to a bad link, must degrade to
    the documented channel list rather than to no channels at all."""
    async def main():
        global _SWALLOW
        driver, _sim = await _make_pair()
        _SWALLOW = True
        await driver.connect()
        _SWALLOW = False
        assert driver.count_children("input") == qu.NUM_INPUTS
        assert driver.count_children("matrix") == qu.NUM_MATRICES
        assert driver.get_state("channel_count") > 0
        await driver.disconnect()
    _run(main())


def test_rediscover_rebuilds_the_roster_after_the_console_is_reconfigured():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        assert driver.count_children("mix") == 12
        # The console is reconfigured underneath us.
        sim.NUM_MIXES = 6
        sim._valid = sim._build_roster()
        result = await driver.send_command("rediscover")
        assert result["channels"] < 75
        await driver.disconnect()
    _run(main())


def test_a_project_label_is_never_overwritten_by_the_driver():
    async def main():
        driver, _sim = await _make_pair()
        driver._project_child_entities = {"input": {"in01": {"label": "Pulpit"}}}
        await driver.connect()
        # in01 is already named in the project, so the driver must seed
        # nothing (the platform applies the project label itself; this stub
        # leaves it empty, which is how we can tell the driver kept quiet).
        assert driver.get_child_state("input", "in01")["label"] == ""
        # A channel the user did not name still gets a positional label.
        assert driver.get_child_state("input", "in02")["label"] == "Input 2"
        assert driver.get_child_state("mute_group", "mg8")["label"] == "Mute Group 8"
        await driver.disconnect()
    _run(main())


# ── State sync ──────────────────────────────────────────────────────────────

def test_connect_populates_child_state_from_the_console():
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        await asyncio.sleep(0.05)
        # The sim rests its masters at unity, like a console out of the box.
        mix1 = driver.get_child_state("mix", "mix01")
        assert abs(mix1["fader"] - 12544 / qu.VALUE_MAX) < 0.001
        assert abs(mix1["fader_db"] - 0.0) < 0.3
        assert driver.get_state("lr_fader_db") == 0.0
        await driver.disconnect()
    _run(main())


def test_pan_and_balance_reach_state_too():
    """Regression: the roster probe once asked only about mutes and levels, so
    no pan address was ever in the answered set and every pan and balance route
    was silently dropped — the state existed and never moved."""
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        pan_routes = [a for a, r in driver._route.items() if r[0] == "pan"]
        assert pan_routes, "no pan routes were built at all"
        assert qu.to_lr(qu._src_input(1), qu.PLANE_PAN) in driver._route
        assert qu.master_mix(1, qu.PLANE_PAN) in driver._route
        assert qu.master_lr(qu.PLANE_PAN) in driver._route
        # ...and a console-side pan move lands.
        await sim.push_param(*qu.to_lr(qu._src_input(4), qu.PLANE_PAN), 0x00, 0x00)
        await asyncio.sleep(0.02)
        assert driver.get_child_state("input", "in04")["lr_pan"] == -1.0
        await driver.disconnect()
    _run(main())


def test_a_dca_has_a_fader_but_no_pan():
    """The console has no DCA balance; offering one would address nothing."""
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        assert "balance" not in driver.get_child_state("dca", "dca1")
        assert qu.master_dca(1) in driver._route
        await driver.disconnect()
    _run(main())


def test_levels_publish_both_a_position_and_a_dB_reading():
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        st = driver.get_child_state("input", "in01")
        assert "lr_level" in st and "lr_level_db" in st
        await driver.disconnect()
    _run(main())


def test_dB_readings_follow_the_configured_fader_law():
    async def main():
        for law, raw, expected_db in (("audio", 12544, 0.0), ("linear", 15196, 0.0)):
            driver, sim = await _make_pair(
                config={"fader_law": law}, sim_config={"fader_law": law})
            await driver.connect()
            await sim.push_param(*qu.to_lr(qu._src_input(1)),
                                 (raw >> 7) & 0x7F, raw & 0x7F)
            await asyncio.sleep(0.02)
            got = driver.get_child_state("input", "in01")["lr_level_db"]
            assert abs(got - expected_db) < 0.3, f"{law}: {got}"
            await driver.disconnect()
    _run(main())


# ── Writes ──────────────────────────────────────────────────────────────────

def test_a_mute_write_round_trips_through_the_console():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await driver.send_command("mute_input", {"input": "in05", "action": "on"})
        await asyncio.sleep(0.05)
        assert driver.get_child_state("input", "in05")["mute"] is True
        assert sim._params[qu.mute_addr(qu._src_input(5))] == 1
        await driver.send_command("mute_input", {"input": "in05", "action": "off"})
        await asyncio.sleep(0.05)
        assert driver.get_child_state("input", "in05")["mute"] is False
        await driver.disconnect()
    _run(main())


def test_state_reflects_what_the_console_kept_not_what_was_sent():
    """Audio Taper resolves a fader to 64-count steps, so a write of an
    arbitrary position is snapped. A driver that mirrored the commanded value
    would sit a step away from the console forever; this one asks."""
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        # A position that is deliberately NOT on a 64-count boundary.
        wanted = 10000 / qu.VALUE_MAX
        await driver.send_command("set_dca_fader", {"dca": "dca1", "level": wanted})
        await asyncio.sleep(0.05)
        stored = sim._params[qu.master_dca(1)]
        assert stored % qusim.AUDIO_STEP == 0, "sim should have quantised"
        assert stored != 10000, "the console did not keep the exact value"
        reported = driver.get_child_state("dca", "dca1")["fader"]
        assert abs(reported - stored / qu.VALUE_MAX) < 1e-6, (
            "driver state must equal what the console kept, not what was sent")
        await driver.disconnect()
    _run(main())


def test_the_simulator_never_echoes_a_change_it_was_sent():
    """Allen & Heath consoles transmit surface moves, not changes a controller
    sends them. Pinned because an echoing simulator would let a mirroring
    driver pass every other test in this file."""
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        raw = sim.handle_command(
            driver._nrpn(*qu.mute_addr(qu._src_input(1)), 0x00, 0x01))
        assert raw is None
        await driver.disconnect()
    _run(main())


def test_a_dB_write_lands_on_the_published_value():
    async def main():
        driver, sim = await _make_pair(config={"fader_law": "linear"},
                                       sim_config={"fader_law": "linear"})
        await driver.connect()
        await driver.send_command("set_lr_fader_db", {"db": -20})
        await asyncio.sleep(0.05)
        assert sim._params[qu.master_lr()] == (0x64 << 7) | 0x16
        await driver.disconnect()
    _run(main())


def test_toggle_reads_the_result_back_rather_than_guessing():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        addr = qu.mute_addr(qu._src_mix(3))
        sim._params[addr] = 1
        await driver.send_command("mute_mix", {"mix": "mix03", "action": "toggle"})
        await asyncio.sleep(0.05)
        assert sim._params[addr] == 0
        assert driver.get_child_state("mix", "mix03")["mute"] is False
        await driver.disconnect()
    _run(main())


def test_a_nudge_steps_the_console_and_reads_back():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        before = sim._params[qu.master_lr()]
        await driver.send_command("step_lr_fader", {"direction": "down"})
        await asyncio.sleep(0.05)
        assert sim._params[qu.master_lr()] < before
        assert driver.get_state("lr_fader") < before / qu.VALUE_MAX + 1e-9
        await driver.disconnect()
    _run(main())


def test_mute_all_inputs_covers_stereo_inputs_too():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await driver.send_command("mute_all_inputs")
        await asyncio.sleep(0.1)
        assert driver.get_child_state("input", "in01")["mute"] is True
        assert driver.get_child_state("input", "in32")["mute"] is True
        assert driver.get_child_state("stereo_input", "st1")["mute"] is True
        assert driver.get_child_state("stereo_input", "usb")["mute"] is True
        # ...and not the buses.
        assert driver.get_child_state("mix", "mix01")["mute"] is False
        await driver.disconnect()
    _run(main())


def test_addressing_a_channel_the_console_lacks_raises():
    """Better a clear error than a write that silently lands somewhere else."""
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        for cmd, params in (
            ("mute_matrix", {"matrix": "mtx2", "action": "on"}),
            ("mute_input", {"input": "in99", "action": "on"}),
        ):
            try:
                await driver.send_command(cmd, params)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{cmd} {params} should have raised")
        await driver.disconnect()
    _run(main())


def test_scene_recall_sends_bank_then_program():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        driver.transport.sent.clear()
        await driver.send_command("recall_scene", {"scene": 156})
        assert bytes(driver.transport.sent) == bytes([0xB0, 0x00, 0x01, 0xC0, 0x1B])
        assert sim._current_scene == 156
        assert driver.get_state("current_scene") == 156
        await driver.disconnect()
    _run(main())


def test_softkey_pulse_presses_and_releases():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        driver.transport.sent.clear()
        await driver.send_command("softkey_pulse", {"softkey": 3})
        assert bytes(driver.transport.sent) == bytes(
            [0x90, 0x32, 0x7F, 0x80, 0x32, 0x00])
        assert sim._softkey_presses == 1
        await driver.disconnect()
    _run(main())


def test_midi_channel_config_moves_every_message():
    async def main():
        driver, _sim = await _make_pair(config={"midi_channel": 5},
                                        sim_config={"midi_channel": 5})
        await driver.connect()
        driver.transport.sent.clear()
        await driver.send_command("recall_scene", {"scene": 1})
        assert driver.transport.sent[0] == 0xB4
        await driver.disconnect()
    _run(main())


# ── Push ────────────────────────────────────────────────────────────────────

def test_a_console_side_move_fans_out_to_the_right_child():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await sim.push_param(*qu.mute_addr(qu._src_input(7)), 0x00, 0x01)
        await asyncio.sleep(0.02)
        assert driver.get_child_state("input", "in07")["mute"] is True
        assert driver.get_child_state("input", "in06")["mute"] is False
        await sim.push_param(*qu.master_dca(2), 0x40, 0x00)
        await asyncio.sleep(0.02)
        assert driver.get_child_state("dca", "dca2")["fader"] > 0.4
        await driver.disconnect()
    _run(main())


def test_a_console_side_scene_change_lands_in_state():
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        driver.on_data_received(bytes([0xB0, 0x00, 0x01, 0xC0, 0x1B]))
        assert driver.get_state("current_scene") == 156
        await driver.disconnect()
    _run(main())


def test_running_status_and_realtime_bytes_are_parsed():
    """The console may use running status, and Active Sense can appear mid
    stream; neither may derail the NRPN aggregator."""
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        msb, lsb = qu.mute_addr(qu._src_input(9))
        # One status byte, then data pairs under running status, with a
        # real-time byte spliced into the middle.
        driver.on_data_received(bytes([0xB0, 0x63, msb, 0x62, lsb, 0xFE,
                                       0x06, 0x00, 0x26, 0x01]))
        assert driver.get_child_state("input", "in09")["mute"] is True
        await driver.disconnect()
    _run(main())


def test_a_split_midi_message_is_buffered_until_complete():
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        msb, lsb = qu.mute_addr(qu._src_input(11))
        driver.on_data_received(bytes([0xB0, 0x63, msb, 0xB0, 0x62]))
        driver.on_data_received(bytes([lsb, 0xB0, 0x06, 0x00, 0xB0, 0x26, 0x01]))
        assert driver.get_child_state("input", "in11")["mute"] is True
        await driver.disconnect()
    _run(main())


def test_sysex_is_skipped_without_disturbing_the_stream():
    """The original Qu's All-Call reply would arrive as SysEx. This generation
    never sends one, but a stray frame must not eat the NRPN after it."""
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        msb, lsb = qu.mute_addr(qu._src_input(13))
        driver.on_data_received(
            bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x11, 0xF7])
            + bytes([0xB0, 0x63, msb, 0xB0, 0x62, lsb,
                     0xB0, 0x06, 0x00, 0xB0, 0x26, 0x01]))
        assert driver.get_child_state("input", "in13")["mute"] is True
        await driver.disconnect()
    _run(main())


# ── Liveness ────────────────────────────────────────────────────────────────

def test_the_probe_is_resolved_only_by_the_address_it_asked_about():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        probe = asyncio.ensure_future(driver._liveness_probe())
        await asyncio.sleep(0.05)
        # The sim answered the Get, so the probe is already satisfied.
        await asyncio.wait_for(probe, 1.0)

        # A push of a DIFFERENT parameter must not satisfy a fresh probe.
        global _SWALLOW
        _SWALLOW = True
        probe2 = asyncio.ensure_future(driver._liveness_probe())
        await asyncio.sleep(0.05)
        _SWALLOW = False
        await sim.push_param(*qu.mute_addr(qu._src_input(1)), 0x00, 0x01)
        await asyncio.sleep(0.05)
        assert not probe2.done()
        # A move of the probed fader does — it equally proves the desk is alive.
        await sim.push_param(*qu.master_lr(), 0x40, 0x00)
        await asyncio.wait_for(probe2, 1.0)
        await driver.disconnect()
    _run(main())


def test_a_silent_console_forces_a_typed_no_response_disconnect():
    async def main():
        global _SWALLOW
        driver, _sim = await _make_pair()
        driver.HEALTH_INTERVAL_S = 0.02
        driver.HEALTH_TIMEOUT_S = 0.05
        driver.HEALTH_MAX_FAILURES = 2
        await driver.connect()
        _SWALLOW = True                     # console vanished without a FIN
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


# ── Simulator roster ────────────────────────────────────────────────────────

def test_the_simulator_is_silent_for_a_parameter_the_console_lacks():
    """The behaviour the whole discovery design rests on."""
    sim = SIM("s", {})
    ch = 0
    present = sim._reply_get(ch, *qu.mute_addr(qu._src_input(1)))
    assert present is not None
    for absent in (
        qu.mute_addr(0x25),                       # Ip33 — an SQ channel
        qu.mute_addr(0x21),                       # ST1's unused right half
        qu.mute_addr(qu._src_matrix(2)),          # Mtx2 — right half of a pair
        qu.mute_addr(qu._src_group(1)),           # groups have no mute
        (0x02, 0x08),                             # a ninth DCA
    ):
        assert sim._reply_get(ch, *absent) is None, f"{absent} should be silent"


def test_the_simulator_models_a_group_as_a_send_source_with_no_mute():
    sim = SIM("s", {})
    assert qu.to_mix(qu._src_group(1), 1) in sim._valid
    assert qu.to_fx_send(qu._src_group(1), 1) in sim._valid
    assert qu.mute_addr(qu._src_group(1)) not in sim._valid
