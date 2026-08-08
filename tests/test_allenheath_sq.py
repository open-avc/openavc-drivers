"""Driver + simulator tests for allenheath_sq (SQ MIDI over TCP).

No SQ hardware on hand, so correctness is proven as a **dual-proof round
trip** wiring the real driver to the real simulator: the sim parses NRPN
byte streams, answers Get requests, echoes absolute sets and pushes
console-side moves; the driver frames, sends and fans replies into child
state; results are asserted on both sides (same approach as
test_biamp_tesira_ttp.py / test_allenheath_qu.py).

Covers the v2.0.0 first-class adoption:
  - CE: the fixed channel roster registers as string-id child entities,
    the on-connect Get sweep populates child state, pushes fan out to the
    right child, and a driver-seeded label never overrides a project one;
  - LV: the Get-probe watchdog — the sim's reply resolves the probe, a
    push of a DIFFERENT parameter does not, and a silent console forces a
    reconnect with a typed no_response fault;
  - optimistic writes (A&H consoles don't echo changes they receive over
    MIDI — confirmed on Qu hardware), toggle/step read-back via Get;
  - address tables byte-exact against the SQ MIDI Protocol Issue 5
    worked examples (including the LSB→MSB rollover).

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
DRIVER_PATH = REPO_ROOT / "audio" / "allenheath_sq.py"
SIM_PATH = REPO_ROOT / "audio" / "allenheath_sq_sim.py"


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


sq = _load("allenheath_sq_under_test", DRIVER_PATH)
sqsim = _load("allenheath_sq_sim_under_test", SIM_PATH)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _make_pair(config=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = sqsim.AllenHeathSQSimulator("sim1", {"midi_channel": 1})
    _CURRENT_SIM = sim
    cfg = {"host": "10.0.0.7", "port": 51325, "midi_channel": 1,
           "poll_interval": 0}
    cfg.update(config or {})
    driver = sq.AllenHeathSQDriver("sq1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_shape():
    info = sq.AllenHeathSQDriver.DRIVER_INFO
    assert info["version"] == "2.0.3"
    assert info["min_platform_version"] == "0.25.0"
    assert info["commands"], "class-level command catalog must not be empty"
    for qa in info["quick_actions"]:
        assert qa in info["commands"]
    for act in info["actions"]:
        assert act["id"] in info["commands"]
    # String child ids need an explicit id_format declaration.
    for ctype, tdef in sq.CHILD_ENTITY_TYPES.items():
        assert tdef.get("id_format", {}).get("type") == "string", ctype
        # Mutes relay high, continuous values low.
        svars = tdef["state_variables"]
        assert svars["mute"]["cloud_priority"] == "high", ctype
        for prop, vd in svars.items():
            if prop != "mute":
                assert vd["cloud_priority"] == "low", (ctype, prop)


def test_command_method_parity():
    cmds = sq.AllenHeathSQDriver.DRIVER_INFO["commands"]
    for cid, cdef in cmds.items():
        method = getattr(sq.AllenHeathSQDriver, f"cmd_{cid}", None)
        assert method is not None, f"missing cmd_{cid}"
        accepted = set(inspect.signature(method).parameters) - {"self"}
        assert set(cdef.get("params", {})) == accepted, cid


# ── Address tables (byte-exact vs SQ MIDI Protocol Issue 5) ─────────────────

def test_mute_addresses_match_spec_examples():
    # §3.3 examples: Ip1 = 00 00, LR = 00 44, Mute Grp 4 = 04 03.
    assert sq.mute_input(1) == (0x00, 0x00)
    assert sq.mute_lr() == (0x00, 0x44)
    assert sq.mute_mgrp(4) == (0x04, 0x03)
    assert sq.mute_dca(1) == (0x02, 0x00)
    assert sq.mute_aux_master(12) == (0x00, 0x50)
    assert sq.mute_mtx_master(3) == (0x00, 0x57)


def test_level_addresses_match_spec_examples():
    # §3.4 examples: Ip1→LR = 40 00, Ip40→LR = 40 27, Ip40→Aux5 = 44 1C,
    # Grp4→Aux8 = 45 2F, Ip36→FX3 = 4D 22; masters at 4F xx.
    assert sq.level_input_to_lr(1) == (0x40, 0x00)
    assert sq.level_input_to_lr(40) == (0x40, 0x27)
    assert sq.level_input_to_aux(40, 5) == (0x44, 0x1C)
    assert sq.level_group_to_aux(4, 8) == (0x45, 0x2F)
    assert sq.level_input_to_fx_send(36, 3) == (0x4D, 0x22)
    assert sq.level_lr_master() == (0x4F, 0x00)
    assert sq.level_dca(8) == (0x4F, 0x27)


def test_pan_addresses_match_spec_examples():
    # §3.7 Get examples: Ip30→Aux5 pan = 53 24, Aux7→Mtx1 balance = 5E 39.
    # §4 Master Sends balance table: LR = 5F 00, Aux1-12 = 5F 01-0C,
    # Mtx1-3 = 5F 11-13.
    assert sq.pan_input_to_aux(30, 5) == (0x53, 0x24)
    assert sq.pan_aux_to_mtx(7, 1) == (0x5E, 0x39)
    assert sq.pan_lr_master() == (0x5F, 0x00)
    assert sq.pan_aux_master(12) == (0x5F, 0x0C)
    assert sq.pan_mtx_master(3) == (0x5F, 0x13)


def test_assign_addresses_roll_lsb_into_next_msb():
    # §4 assignment table: Ip1→LR = 60 00, Ip1→Aux1 = 60 44, Ip5→Aux12 =
    # 60 7F, Ip6→Aux1 = 61 00 (the LSB rollover).
    assert sq.assign_input_to_lr(1) == (0x60, 0x00)
    assert sq.assign_input_to_aux(1, 1) == (0x60, 0x44)
    assert sq.assign_input_to_aux(5, 12) == (0x60, 0x7F)
    assert sq.assign_input_to_aux(6, 1) == (0x61, 0x00)


def test_value_codecs():
    assert sq.level_to_vcvf(0.0) == (0x00, 0x00)
    assert sq.level_to_vcvf(1.0) == (0x7F, 0x7F)
    # Pan doc: L100% = 00 00, CTR = 3F 7F, R100% = 7F 7F.
    assert sq.pan_to_vcvf(-1.0) == (0x00, 0x00)
    assert sq.pan_to_vcvf(0.0) == (0x3F, 0x7F)
    assert sq.pan_to_vcvf(1.0) == (0x7F, 0x7F)
    assert abs(sq.vcvf_to_pan(0x3F, 0x7F)) < 0.001
    # Scene banking (§3.1): scene 96 = bank 00 program 5F; 156 = 01 1B.
    assert sq.scene_to_bank_program(96) == (0, 0x5F)
    assert sq.scene_to_bank_program(156) == (1, 0x1B)
    assert sq.scene_to_bank_program(300) == (2, 43)
    for bad in (0, 301):
        try:
            sq.scene_to_bank_program(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    assert sq.softkey_to_note(1) == 0x30
    assert sq.softkey_to_note(16) == 0x3F


# ── CE: connect, roster, sweep, labels ──────────────────────────────────────

def test_connect_registers_roster_and_sweep_populates():
    async def main():
        driver, sim = await _make_pair()
        # Pre-set console-side state the sweep must read back.
        sim._params[sq.mute_input(3)] = 1
        sim._params[sq.level_lr_master()] = 0x1FFF
        sim._params[sq.level_aux_master(2)] = 0x3FFF
        await driver.connect()
        assert driver.count_children("input") == 48
        assert driver.count_children("group") == 12
        assert driver.count_children("aux") == 12
        assert driver.count_children("matrix") == 3
        assert driver.count_children("fx_send") == 4
        assert driver.count_children("fx_return") == 8
        assert driver.count_children("dca") == 8
        assert driver.count_children("mute_group") == 8
        assert driver.get_child_state("input", "in03")["mute"] is True
        assert driver.get_child_state("input", "in01")["mute"] is False
        assert abs(driver.get_state("lr_fader") - 0.5) < 0.01
        assert abs(driver.get_child_state("aux", "aux02")["fader"] - 1.0) < 0.001
        # Untouched pans/balances read back center (sim default fidelity).
        assert abs(driver.get_child_state("input", "in01")["lr_pan"]) < 0.001
        assert abs(driver.get_state("lr_balance")) < 0.001
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
        # in02 has none -> the driver seeds the generic label.
        assert driver.get_child_state("input", "in02")["label"] == "Input 2"
        assert driver.get_child_state("mute_group", "mg8")["label"] == "Mute Group 8"
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
                                  {"input": "in05", "action": "on"})
        assert sim._params[sq.mute_input(5)] == 1          # reached the wire
        assert driver.get_child_state("input", "in05")["mute"] is True
        await driver.send_command("mute_lr", {"action": "on"})
        assert driver.get_state("lr_mute") is True
        _SWALLOW = False
        await driver.disconnect()
    _run(main())


def test_mute_toggle_reads_result_back():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        sim._params[sq.mute_dca(2)] = 1
        await driver.send_command("mute_dca", {"dca": "dca2",
                                               "action": "toggle"})
        assert sim._params[sq.mute_dca(2)] == 0
        assert driver.get_child_state("dca", "dca2")["mute"] is False
        await driver.disconnect()
    _run(main())


def test_fader_and_pan_round_trips():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await driver.send_command("set_aux_master_level",
                                  {"aux": "aux03", "level": 0.5})
        assert abs(driver.get_child_state("aux", "aux03")["fader"] - 0.5) < 0.01
        assert abs(sim._params[sq.level_aux_master(3)] - 0x2000) <= 0x40
        await driver.send_command("set_input_to_lr_pan",
                                  {"input": "in01", "pan": -1.0})
        assert driver.get_child_state("input", "in01")["lr_pan"] == -1.0
        assert sim._params[sq.pan_input_to_lr(1)] == 0
        await driver.send_command("set_mtx_master_balance",
                                  {"mtx": "mtx2", "pan": 1.0})
        assert driver.get_child_state("matrix", "mtx2")["balance"] == 1.0
        await driver.send_command("set_lr_master_level", {"level": 1.0})
        assert driver.get_state("lr_fader") == 1.0
        await driver.disconnect()
    _run(main())


def test_step_sends_native_nudge_then_get():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        driver.transport.sent.clear()
        await driver.send_command("step_dca_level",
                                  {"dca": "dca1", "direction": "up"})
        msb, lsb = sq.level_dca(1)
        sent = bytes(driver.transport.sent)
        inc = bytes([0xB0, 0x63, msb, 0xB0, 0x62, lsb, 0xB0, 0x60, 0x00])
        get = bytes([0xB0, 0x63, msb, 0xB0, 0x62, lsb, 0xB0, 0x60, 0x7F])
        assert sent == inc + get
        # The sim stepped the value and the Get read it back into state.
        assert driver.get_child_state("dca", "dca1")["fader"] > 0.0
        await driver.disconnect()
    _run(main())


def test_scene_recall_bytes_and_state():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        driver.transport.sent.clear()
        # §3.1 example: Scene 7, Ch1 = B0 00 00 C0 06.
        await driver.send_command("recall_scene", {"scene": 7})
        assert bytes(driver.transport.sent) == bytes(
            [0xB0, 0x00, 0x00, 0xC0, 0x06])
        assert driver.get_state("current_scene") == 7
        assert sim._current_scene == 7
        driver.transport.sent.clear()
        # §3.1 example: Scene 156, Ch1 = B0 00 01 C0 1B.
        await driver.send_command("recall_scene", {"scene": 156})
        assert bytes(driver.transport.sent) == bytes(
            [0xB0, 0x00, 0x01, 0xC0, 0x1B])
        assert sim._current_scene == 156
        await driver.disconnect()
    _run(main())


def test_mute_all_inputs():
    async def main():
        driver, sim = await _make_pair()
        await driver.connect()
        await driver.send_command("mute_all_inputs", {})
        for n in range(1, 49):
            assert sim._params[sq.mute_input(n)] == 1
        assert driver.get_child_state("input", "in48")["mute"] is True
        await driver.send_command("unmute_all_inputs", {})
        assert driver.get_child_state("input", "in48")["mute"] is False
        await driver.disconnect()
    _run(main())


def test_unknown_child_id_raises():
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("mute_input",
                                      {"input": "in99", "action": "on"})
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
        await sim.push_param(*sq.mute_dca(2), 0x00, 0x01)
        assert driver.get_child_state("dca", "dca2")["mute"] is True
        await sim.push_param(*sq.level_input_to_lr(7), 0x7F, 0x7F)
        assert abs(driver.get_child_state("input", "in07")["lr_level"] - 1.0) < 0.001
        await sim.push_param(*sq.pan_group_to_lr(3), 0x3F, 0x7F)
        assert abs(driver.get_child_state("group", "grp03")["lr_pan"]) < 0.001
        await sim.push_param(*sq.mute_lr(), 0x00, 0x01)
        assert driver.get_state("lr_mute") is True
        # Scene push (Bank Select + Program Change).
        await sim.set_state_value("current_scene", 156)
        assert driver.get_state("current_scene") == 156
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
        # A push of a DIFFERENT parameter must not satisfy the probe.
        await sim.push_param(*sq.mute_input(1), 0x00, 0x01)
        await asyncio.sleep(0.05)
        assert not probe.done()
        # An absolute for the probed address (here a console-side LR master
        # fader move) does — it equally proves the console is alive.
        await sim.push_param(*sq.level_lr_master(), 0x40, 0x00)
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


def test_incoming_scene_push_via_raw_bytes():
    async def main():
        driver, _sim = await _make_pair()
        await driver.connect()
        driver.on_data_received(bytes([0xB0, 0x00, 0x01, 0xC0, 0x1B]))
        assert driver.get_state("current_scene") == 156
        await driver.disconnect()
    _run(main())
