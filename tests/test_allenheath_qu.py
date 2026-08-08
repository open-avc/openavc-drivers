"""Unit tests for the allenheath_qu driver.

Loads ``audio/allenheath_qu.py`` directly, stubbing the ``openavc.*`` imports it
needs (BaseDriver, ConnectionFaultError, TCPTransport, get_logger) so the
community repo's test suite stays self-contained — mirrors test_qsc_qrc.py.

Coverage: the fader/gain/pan value tables and channel-select addressing (exact
bytes from the Qu MIDI Protocol V1.9+ reference tables), command/handler parity,
outgoing wire framing, and the incoming identify -> child-state fan-out driven
by hand-built protocol messages.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from _platform_stubs import ConnectionFaultError as _ConnectionFaultError

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "allenheath_qu.py"


# ── Stub openavc.* with an in-memory BaseDriver ──

def _install_server_stubs() -> None:
    if "openavc.drivers.base" in sys.modules:
        return
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("openavc", server)
    drivers = ModuleType("openavc.drivers")
    drivers.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("openavc.drivers", drivers)
    base = ModuleType("openavc.drivers.base")

    class _BaseDriver:
        DRIVER_INFO: dict = {}

        def __init__(self, device_id, config, state, events):
            self.device_id = device_id
            self.config = config
            self.state = state
            self.events = events
            self.transport = None
            self._connected = False
            self._last_transport_error = ""
            self._last_fault = None
            self.device_state: dict = {}
            self._children: dict = {}
            self._order: dict = {}

        def set_state(self, key, value):
            self.device_state[key] = value

        def get_state(self, key, default=None):
            return self.device_state.get(key, default)

        def _eff_schema(self, child_type, local_id):
            types = self.DRIVER_INFO.get("child_entity_types", {})
            sch = dict(types.get(child_type, {}).get("state_variables", {}))
            sch.setdefault("online", {"type": "boolean"})
            sch.setdefault("label", {"type": "string"})
            return sch

        def register_child(self, child_type, local_id, initial_state=None,
                           schema=None):
            # Mirror the platform's child-id validation: types default to
            # integer ids and only accept strings when they declare
            # id_format: {type: string}. (Without this the stub was too lenient
            # and let string ids pass that the real BaseDriver rejects.)
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

        def is_child_registered(self, child_type, local_id):
            return (child_type, local_id) in self._children

        def get_child_state(self, child_type, local_id):
            return dict(self._children.get((child_type, local_id), {}))

        def _validate(self, child_type, local_id, prop):
            if prop not in self._eff_schema(child_type, local_id):
                raise ValueError(f"bad prop {prop!r} for {child_type}")

        def set_child_state_batch(self, child_type, local_id, updates):
            for prop in updates:
                self._validate(child_type, local_id, prop)
            self._children[(child_type, local_id)].update(updates)

        def set_children_state_batch(self, updates):
            for ctype, lid, ups in updates:
                for prop in ups:
                    self._validate(ctype, lid, prop)
            for ctype, lid, ups in updates:
                self._children[(ctype, lid)].update(ups)

        def count_children(self, child_type):
            return len(self._order.get(child_type, []))

        def _handle_transport_disconnect(self):
            self._connected = False

    base.BaseDriver = _BaseDriver
    base.ConnectionFaultError = _ConnectionFaultError
    sys.modules["openavc.drivers.base"] = base

    transport = ModuleType("openavc.transport")
    transport.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("openavc.transport", transport)
    tcp = ModuleType("openavc.transport.tcp")
    tcp.TCPTransport = type("TCPTransport", (), {})
    sys.modules["openavc.transport.tcp"] = tcp

    utils = ModuleType("openavc.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("openavc.utils", utils)
    logger = ModuleType("openavc.utils.logger")

    class _Log:
        def __getattr__(self, _):
            return lambda *a, **k: None
    logger.get_logger = lambda *_a, **_k: _Log()
    sys.modules["openavc.utils.logger"] = logger


def _load_driver():
    _install_server_stubs()
    spec = importlib.util.spec_from_file_location("allenheath_qu", DRIVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qu = _load_driver()


class _FakeTransport:
    def __init__(self):
        self.sent = bytearray()
        self.connected = True

    async def send(self, data):
        self.sent.extend(data)

    async def close(self):
        self.connected = False


def _make(config=None, midi_n=0):
    cfg = {"host": "10.0.0.9"}
    if config:
        cfg.update(config)
    d = qu.AllenHeathQuDriver("qu", cfg, object(), object())
    d.transport = _FakeTransport()
    d._connected = True
    d._midi_n = midi_n
    return d


def _run(coro):
    return asyncio.run(coro)


async def _feed(d, data):
    d.on_data_received(bytes(data))


# ── Value tables (exact bytes from the protocol reference) ──

def test_fader_table():
    assert qu.fader_db_to_byte(0.0) == 0x62
    assert qu.fader_db_to_byte(10.0) == 0x7F
    assert qu.fader_db_to_byte(-10.0) == 0x3F
    assert qu.fader_db_to_byte(-90.0) == 0x00
    assert qu.fader_pos_to_byte(0.0) == 0x00
    assert qu.fader_pos_to_byte(1.0) == 0x7F


def test_gain_table():
    assert qu.gain_db_to_byte(0.0) == 0x0A
    assert qu.gain_db_to_byte(60.0) == 0x7F
    assert qu.gain_db_to_byte(-5.0) == 0x00


def test_pan_table():
    assert qu.pan_to_byte(-1.0) == 0x00
    assert qu.pan_to_byte(0.0) == 0x25
    assert qu.pan_to_byte(1.0) == 0x4A
    assert abs(qu.byte_to_pan(qu.pan_to_byte(0.3)) - 0.3) < 0.03


def test_addressing():
    assert (qu.input_ch(1), qu.input_ch(32)) == (0x20, 0x3F)
    assert (qu.stereo_ch(1), qu.stereo_ch(3)) == (0x40, 0x42)
    assert (qu.dca_ch(1), qu.mute_group_ch(1)) == (0x10, 0x50)
    assert (qu.fx_send_ch(1), qu.fx_return_ch(1)) == (0x00, 0x08)
    assert (qu.group_ch(1), qu.matrix_ch(1), qu.LR_CH) == (0x68, 0x6C, 0x67)


# ── Command <-> handler parity ──

def test_command_handler_parity():
    cmds = qu._build_commands()
    handlers = qu._COMMAND_HANDLERS
    assert set(cmds) == set(handlers)
    info = qu.AllenHeathQuDriver.DRIVER_INFO
    for qa in info["quick_actions"]:
        assert qa in cmds
    for act in info["actions"]:
        assert act["id"] in cmds


# ── Outgoing wire framing ──

def test_recall_scene_bytes():
    d = _make()
    _run(d.send_command("recall_scene", {"scene": 5}))
    # Bank 1 select then Program Change to scene-1: B0 00 00 B0 20 00 C0 04
    assert bytes(d.transport.sent) == bytes(
        [0xB0, 0x00, 0x00, 0xB0, 0x20, 0x00, 0xC0, 0x04])
    assert d.get_state("current_scene") == 5


def test_mute_input_bytes():
    d = _make()
    d._register_topology("Qu-16")
    d.transport.sent.clear()
    _run(d.send_command("mute_input", {"channel": "in01", "action": "on"}))
    # Note On >= 0x40 then Note Off, note = input 1 CH = 0x20.
    assert bytes(d.transport.sent) == bytes(
        [0x90, 0x20, 0x7F, 0x90, 0x20, 0x00])


def test_set_fader_db_bytes():
    d = _make()
    d._register_topology("Qu-16")
    d.transport.sent.clear()
    _run(d.send_command("set_input_fader_db", {"channel": "in01", "db": 0.0}))
    # NRPN fader on input 1 to 0 dB (0x62), index 0x07.
    assert bytes(d.transport.sent) == bytes(
        [0xB0, 0x63, 0x20, 0xB0, 0x62, 0x17, 0xB0, 0x06, 0x62, 0xB0, 0x26, 0x07])


def test_send_level_addresses_dest_vx():
    d = _make()
    d._register_topology("Qu-24")
    d.transport.sent.clear()
    # Input 3 -> Mix 2 send level; Mix 2 VX = 0x01, input 3 CH = 0x22.
    _run(d.send_command("set_send_level", {
        "source_type": "input", "source": 3,
        "dest_type": "mix", "dest": 2, "level": 1.0}))
    sent = bytes(d.transport.sent)
    assert sent == bytes(
        [0xB0, 0x63, 0x22, 0xB0, 0x62, 0x20, 0xB0, 0x06, 0x7F, 0xB0, 0x26, 0x01])


def test_raw_parameter_bytes():
    d = _make()
    _run(d.send_command("set_raw_parameter",
                        {"ch": 0x20, "param_id": 0x17, "value": 0x62,
                         "index": 0x07}))
    assert bytes(d.transport.sent) == bytes(
        [0xB0, 0x63, 0x20, 0xB0, 0x62, 0x17, 0xB0, 0x06, 0x62, 0xB0, 0x26, 0x07])


def test_optimistic_state_on_send():
    # The Qu doesn't echo MIDI-received changes, so a sent command must update
    # our own state immediately (no inbound bytes here).
    d = _make()
    d._register_topology("Qu-16")
    _run(d.send_command("mute_input", {"channel": "in02", "action": "on"}))
    assert d.get_child_state("input", "in02")["mute"] is True
    _run(d.send_command("set_input_fader_db", {"channel": "in02", "db": 0.0}))
    assert abs(d.get_child_state("input", "in02")["fader_db"]) < 0.5
    _run(d.send_command("set_input_pan", {"channel": "in02", "pan": -1.0}))
    assert d.get_child_state("input", "in02")["pan"] < -0.9
    _run(d.send_command("mute_lr", {"action": "on"}))
    assert d.get_state("lr_mute") is True


def test_child_types_declare_string_id_format():
    # Children use sanitized string ids (in01, mix56, dca1, ...); the platform
    # defaults to integer ids and rejects strings without this declaration.
    for ctype, tdef in qu.CHILD_ENTITY_TYPES.items():
        assert tdef.get("id_format", {}).get("type") == "string", ctype


# ── Topology + identify ──

def test_qu16_topology_has_no_groups_or_matrix():
    d = _make()
    d._register_topology("Qu-16")
    assert d.count_children("input") == 16
    assert d.count_children("group") == 0
    assert d.count_children("matrix") == 0
    assert d.count_children("fx_send") == 2
    assert d.count_children("dca") == 4


def test_qu32_topology():
    d = _make()
    d._register_topology("Qu-32")
    assert d.count_children("input") == 32
    assert d.count_children("group") == 4
    assert d.count_children("matrix") == 2
    assert d.count_children("fx_send") == 4


def test_identify_reply_learns_model_and_channel():
    d = _make(midi_n=0)
    # System State reply on MIDI channel 2 (header byte 0x02), BoxID 2 = Qu-24.
    reply = bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00, 0x02,
                   0x11, 0x02, 0x01, 0x09, 0xF7])
    _run(_feed(d, reply))
    assert d.get_state("model") == "Qu-24"
    assert d.get_state("identified") is True
    assert d._midi_n == 2                     # learned from the reply
    assert d.count_children("input") == 24
    assert d.count_children("matrix") == 2


# ── Incoming push fan-out ──

def test_incoming_mute_updates_child():
    d = _make()
    d._register_topology("Qu-16")
    _run(_feed(d, [0x90, 0x20, 0x7F, 0x90, 0x20, 0x00]))   # input 1 mute on
    assert d.get_child_state("input", "in01")["mute"] is True
    _run(_feed(d, [0x90, 0x20, 0x3F, 0x90, 0x20, 0x00]))   # input 1 mute off
    assert d.get_child_state("input", "in01")["mute"] is False


def test_incoming_fader_updates_lr():
    d = _make()
    d._register_topology("Qu-16")
    # LR fader (CH 0x67) NRPN to 0 dB (0x62).
    _run(_feed(d, [0xB0, 0x63, 0x67, 0xB0, 0x62, 0x17,
                   0xB0, 0x06, 0x62, 0xB0, 0x26, 0x07]))
    assert abs(d.get_state("lr_fader_db") - 0.0) < 0.5
    assert 0.7 < d.get_state("lr_fader") < 0.8


def test_incoming_name_reply_labels_child():
    d = _make()
    d._register_topology("Qu-16")
    name = b"Vox 1"
    msg = bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00, 0x00,
                 0x02, 0x20]) + name + bytes([0xF7])
    _run(_feed(d, msg))
    assert d.get_child_state("input", "in01")["name"] == "Vox 1"


def test_incoming_program_change_sets_scene():
    d = _make()
    d._register_topology("Qu-16")
    _run(_feed(d, [0xC0, 0x09]))              # Program 9 -> scene 10
    assert d.get_state("current_scene") == 10
