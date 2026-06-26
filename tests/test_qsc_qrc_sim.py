"""Integration tests for the qsc_qrc simulator + driver duality.

Loads ``audio/qsc_qrc_sim.py`` (stubbing ``simulator.tcp_simulator``) and
``audio/qsc_qrc.py`` (stubbing ``server.*`` with a faithful in-memory
BaseDriver), then drives the simulator's JSON-RPC surface and feeds its output
into the driver — proving the simulated Core exposes a topology the driver
auto-discovers and that change-group pushes fan out into child state. The
simulator mirrors the real Core 24f shapes (boolean router crosspoints, Time/
Text controls, Read Only directions, Triggers).

Self-contained / stdlib so it runs in the community repo's isolated CI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parent.parent
SIM_PATH = REPO_ROOT / "audio" / "qsc_qrc_sim.py"
DRIVER_PATH = REPO_ROOT / "audio" / "qsc_qrc.py"

NUL = b"\x00"


# ── Stubs ──

def _install_simulator_stub() -> None:
    if "simulator.tcp_simulator" in sys.modules:
        return
    pkg = ModuleType("simulator")
    pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("simulator", pkg)
    mod = ModuleType("simulator.tcp_simulator")

    class _TCPSimulator:
        SIMULATOR_INFO: dict = {}

        def __init__(self, device_id, config=None):
            self.device_id = device_id
            self.name = device_id
            self.config = config or {}
            self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
            self._clients: dict = {}
            self.pushed: list = []

        @property
        def state(self):
            return dict(self._state)

        def set_state(self, key, value):
            self._state[key] = value

        def get_state(self, key, default=None):
            return self._state.get(key, default)

        async def push_to(self, client_id, frame):
            self.pushed.append((client_id, frame))

    mod.TCPSimulator = _TCPSimulator
    sys.modules["simulator.tcp_simulator"] = mod


def _install_server_stubs() -> None:
    """Faithful in-memory BaseDriver (per-child dynamic schemas + validation),
    so the driver's real register/fan-out methods run unchanged."""
    if "server.drivers.base" in sys.modules:
        return
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server", server)
    drivers = ModuleType("server.drivers")
    drivers.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.drivers", drivers)
    base = ModuleType("server.drivers.base")

    class _BaseDriver:
        DRIVER_INFO: dict = {}

        def __init__(self, device_id, config, state, events):
            self.device_id = device_id
            self.config = config
            self.state = state
            self.events = events
            self.transport = None
            self._connected = False
            self.device_state: dict = {}
            self._children: dict = {}
            self._schemas: dict = {}
            self._order: dict = {}

        def set_state(self, key, value):
            self.device_state[key] = value

        def _eff(self, t, i):
            s = dict(self._schemas.get((t, i), {}))
            s.setdefault("online", {"type": "boolean"})
            s.setdefault("label", {"type": "string"})
            return s

        def register_child(self, t, i, initial_state=None, schema=None):
            if (t, i) in self._children:
                return
            if schema is not None:
                self._schemas[(t, i)] = dict(schema)
            st = {p: ("" ) for p in self._eff(t, i)}
            for p, v in (initial_state or {}).items():
                if p not in self._eff(t, i):
                    raise ValueError(p)
                st[p] = v
            self._children[(t, i)] = st
            self._order.setdefault(t, []).append(i)

        def deregister_child(self, t, i):
            self._children.pop((t, i), None)
            self._schemas.pop((t, i), None)
            if i in self._order.get(t, []):
                self._order[t].remove(i)

        def is_child_registered(self, t, i):
            return (t, i) in self._children

        def list_children(self, t):
            return list(self._order.get(t, []))

        def get_child_state(self, t, i):
            return dict(self._children.get((t, i), {}))

        def _val(self, t, i, p):
            if p not in self._eff(t, i):
                raise ValueError(p)

        def set_child_state(self, t, i, p, v):
            self._val(t, i, p)
            self._children[(t, i)][p] = v

        def set_child_state_batch(self, t, i, ups):
            for p in ups:
                self._val(t, i, p)
            self._children[(t, i)].update(ups)

        def set_children_state_batch(self, updates):
            for t, i, ups in updates:
                for p in ups:
                    self._val(t, i, p)
            for t, i, ups in updates:
                self._children[(t, i)].update(ups)

    base.BaseDriver = _BaseDriver
    sys.modules["server.drivers.base"] = base

    transport = ModuleType("server.transport")
    transport.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.transport", transport)
    tcp = ModuleType("server.transport.tcp")
    tcp.TCPTransport = type("TCPTransport", (), {})
    sys.modules["server.transport.tcp"] = tcp
    sysconfig = ModuleType("server.system_config")
    sysconfig.get_system_config = lambda: type(
        "C", (), {"get": staticmethod(lambda *a, **k: None)})()
    sys.modules["server.system_config"] = sysconfig
    utils = ModuleType("server.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.utils", utils)
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda *_a, **_k: type(
        "L", (), {"__getattr__": lambda s, n: (lambda *a, **k: None)})()
    sys.modules["server.utils.logger"] = logger


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


_install_simulator_stub()
_install_server_stubs()
simmod = _load(SIM_PATH, "qsc_qrc_sim_under_test")
drvmod = _load(DRIVER_PATH, "qsc_qrc_driver_for_sim_test")


def _make_sim():
    sim = simmod.QSCQRCSimulator("qsc-sim")
    sim._clients["c1"] = object()
    # on_client_connected is async in the real base; initialize the per-client
    # bookkeeping it would set up directly (handle_command needs both dicts).
    sim._client_groups["c1"] = {}
    sim._client_logged_in["c1"] = True
    return sim


def _call(sim, method, params, rid=1):
    env = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        env["params"] = params
    resp = sim.handle_command(json.dumps(env).encode() + NUL)
    if resp is None:
        return None
    return json.loads(resp.rstrip(NUL).decode())


# ── Simulator topology fidelity ──

def test_get_components_types():
    sim = _make_sim()
    comps = _call(sim, "Component.GetComponents", None)["result"]
    by_name = {c["Name"]: c["Type"] for c in comps}
    assert by_name == {
        "PgmGain": "gain",
        "PrgMix": "mixer",
        "PrgmRouter": "router_with_output",
        "RoomScenes": "snapshot_controller",
        "BgmLoop": "loop_player",
    }


def test_router_boolean_crosspoints():
    sim = _make_sim()
    res = _call(sim, "Component.GetControls", {"Name": "PrgmRouter"})["result"]
    by = {c["Name"]: c for c in res["Controls"]}
    assert by["output.1.input.1.select"]["Type"] == "Boolean"
    assert by["select.1"]["Type"] == "Integer"
    assert by["mute.1"]["Type"] == "Boolean"


def test_loop_player_time_controls_are_read_only():
    sim = _make_sim()
    res = _call(sim, "Component.GetControls", {"Name": "BgmLoop"})["result"]
    by = {c["Name"]: c for c in res["Controls"]}
    assert by["output.1.elapsed"]["Type"] == "Time"
    assert by["output.1.elapsed"]["Direction"] == "Read Only"
    assert by["output.1.status"]["Type"] == "Text"


def test_snapshot_triggers_have_no_value():
    sim = _make_sim()
    res = _call(sim, "Component.GetControls", {"Name": "RoomScenes"})["result"]
    by = {c["Name"]: c for c in res["Controls"]}
    assert by["load.next"]["Type"] == "Trigger"
    assert "Value" not in by["load.next"]
    assert by["last.1"]["Type"] == "Boolean"
    assert by["last.1"]["Direction"] == "Read Only"
    assert by["load.1"]["Type"] == "State Trigger"


def test_component_set_then_poll_reflects_change():
    sim = _make_sim()
    _call(sim, "ChangeGroup.AddComponentControl",
          {"Id": "g", "Component": {"Name": "PgmGain",
                                    "Controls": [{"Name": "gain"}]}}, 1)
    _call(sim, "ChangeGroup.Poll", {"Id": "g"}, 2)  # first poll = all
    _call(sim, "Component.Set",
          {"Name": "PgmGain", "Controls": [{"Name": "gain", "Value": -22}]}, 3)
    poll = _call(sim, "ChangeGroup.Poll", {"Id": "g"}, 4)["result"]
    changes = {(c.get("Component"), c["Name"]): c["Value"]
               for c in poll["Changes"]}
    assert changes[("PgmGain", "gain")] == -22.0


# ── Duality: the driver auto-discovers the simulated topology ──

def test_driver_consumes_simulator_output():
    sim = _make_sim()
    drv = drvmod.QSCQRCDriver("d", {"host": "x"}, object(), object())

    # Feed each simulated component through the driver's registration.
    comps = _call(sim, "Component.GetComponents", None)["result"]
    for c in comps:
        ctrls = _call(sim, "Component.GetControls", {"Name": c["Name"]})["result"]
        drv._register_component(
            drvmod._safe_id(c["Name"]), c["Name"], c["Type"],
            ctrls["Controls"])
    assert set(drv.list_children("component")) == {
        "PgmGain", "PrgMix", "PrgmRouter", "RoomScenes", "BgmLoop"}

    # Router crosspoints + mixer Text labels became typed child schema entries.
    rsch = drv._schemas[("component", "PrgmRouter")]
    assert rsch["output.1.input.1.select"]["type"] == "boolean"
    msch = drv._schemas[("component", "PrgMix")]
    assert msch["input.1.label"]["type"] == "string"

    # A simulated change-group push fans out into child state.
    drv._control_route[("PgmGain", "gain")] = {
        "ctype": "component", "sid": "PgmGain", "prop": "gain",
        "type": "number", "value_from": "value",
        "str_prop": "gain__str", "pos_prop": None}
    _call(sim, "ChangeGroup.AddComponentControl",
          {"Id": "g", "Component": {"Name": "PgmGain",
                                    "Controls": [{"Name": "gain"}]}}, 1)
    _call(sim, "ChangeGroup.Poll", {"Id": "g"}, 2)
    _call(sim, "Component.Set",
          {"Name": "PgmGain", "Controls": [{"Name": "gain", "Value": -33}]}, 3)
    push = _call(sim, "ChangeGroup.Poll", {"Id": "g"}, 4)["result"]
    drv._handle_change_group_changes(push)
    assert drv.get_child_state("component", "PgmGain")["gain"] == -33.0
