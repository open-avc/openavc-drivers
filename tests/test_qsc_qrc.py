"""Unit tests for the qsc_qrc driver.

Loads ``audio/qsc_qrc.py`` directly, stubbing the ``server.*`` imports it needs
(BaseDriver, TCPTransport, get_logger) so the community repo's test suite stays
self-contained — mirrors test_chazy_control_pro.py.

The interesting logic (control-schema building, change-group push fan-out, type
inference) is exercised against **byte-exact captures from a live Core 24f**
under ``fixtures/qsc_qrc_qrc.json`` (recapture with
``qsys-scratch/capture_fixtures.py`` in the workspace). The BaseDriver stub
reproduces the platform's child-entity semantics (per-child dynamic schemas,
prop validation) in memory so the driver's real methods run unchanged.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "qsc_qrc.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "qsc_qrc_qrc.json"


# ── Stub server.* with an in-memory BaseDriver that mimics child semantics ──

def _install_server_stubs() -> None:
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
        """Faithful enough stand-in: device + child state in plain dicts, with
        the same dynamic-schema prop validation the real platform enforces."""

        DRIVER_INFO: dict = {}

        def __init__(self, device_id, config, state, events):
            self.device_id = device_id
            self.config = config
            self.state = state
            self.events = events
            self.transport = None
            self._connected = False
            self.device_state: dict = {}
            self.stashed_fault: tuple | None = None
            self._children: dict = {}          # (type, id) -> dict(state)
            self._schemas: dict = {}           # (type, id) -> schema
            self._order: dict = {}             # type -> [ids in order]

        # -- device state --
        def set_state(self, key, value):
            self.device_state[key] = value

        def get_state(self, key, default=None):
            return self.device_state.get(key, default)

        # -- child entities --
        def _eff_schema(self, child_type, local_id):
            sch = dict(self._schemas.get((child_type, local_id), {}))
            sch.setdefault("online", {"type": "boolean"})
            sch.setdefault("label", {"type": "string"})
            return sch

        def register_child(self, child_type, local_id, initial_state=None,
                           schema=None):
            if (child_type, local_id) in self._children:
                return  # idempotent
            if schema is not None:
                self._schemas[(child_type, local_id)] = dict(schema)
            eff = self._eff_schema(child_type, local_id)
            st = {}
            for prop, vd in eff.items():
                if prop == "online":
                    st[prop] = True
                elif prop == "label":
                    st[prop] = ""
                else:
                    t = vd.get("type")
                    st[prop] = (False if t == "boolean" else
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
            self._schemas.pop((child_type, local_id), None)
            if local_id in self._order.get(child_type, []):
                self._order[child_type].remove(local_id)

        def is_child_registered(self, child_type, local_id):
            return (child_type, local_id) in self._children

        def list_children(self, child_type):
            return list(self._order.get(child_type, []))

        def get_child_state(self, child_type, local_id):
            return dict(self._children.get((child_type, local_id), {}))

        def _validate(self, child_type, local_id, prop):
            if prop not in self._eff_schema(child_type, local_id):
                raise ValueError(
                    f"prop {prop!r} not in schema for {child_type}.{local_id}")

        def set_child_state(self, child_type, local_id, prop, value):
            self._validate(child_type, local_id, prop)
            self._children[(child_type, local_id)][prop] = value

        def set_child_state_batch(self, child_type, local_id, updates):
            for prop in updates:
                self._validate(child_type, local_id, prop)
            self._children[(child_type, local_id)].update(updates)

        def _stash_fault(self, code, message=""):
            self.stashed_fault = (code, message)

        def set_children_state_batch(self, updates):
            for ctype, lid, ups in updates:
                for prop in ups:
                    self._validate(ctype, lid, prop)
            for ctype, lid, ups in updates:
                self._children[(ctype, lid)].update(ups)

    class _ConnectionFaultError(ConnectionError):
        def __init__(self, message="", *, code):
            super().__init__(message)
            self.fault_code = code

    base.BaseDriver = _BaseDriver
    base.ConnectionFaultError = _ConnectionFaultError
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

    class _Log:
        def __getattr__(self, _):
            return lambda *a, **k: None
    logger.get_logger = lambda *_a, **_k: _Log()
    sys.modules["server.utils.logger"] = logger


def _load_driver():
    _install_server_stubs()
    spec = importlib.util.spec_from_file_location("qsc_qrc", DRIVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qsc = _load_driver()
FX = json.loads(FIXTURES.read_text())


def _make_driver(config=None):
    cfg = {"host": "10.0.0.5", "port": 1710}
    if config:
        cfg.update(config)
    return qsc.QSCQRCDriver("qsys", cfg, object(), object())


# ── Pure helpers ──

def test_parse_named_controls():
    txt = "# comment\nVolume number\nMute boolean\nLevel\nGate trigger\nBad weird\n"
    out = qsc.parse_named_controls(txt)
    assert out == [
        ("Volume", "number"), ("Mute", "boolean"), ("Level", None),
        ("Gate", "trigger"), ("Bad", None),
    ]


@pytest.mark.parametrize("raw,hint,expected", [
    (-12.0, "number", -12.0),
    (0.0, "boolean", False),
    (1.0, "boolean", True),
    ("muted", "boolean", True),
    ("unmuted", "boolean", False),
    (3.7, "integer", 3),
    ("hello", "string", "hello"),
    (None, "number", None),
])
def test_coerce_value(raw, hint, expected):
    assert qsc._coerce_value(raw, hint) == expected


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("false", False), ("-12", -12), ("-12.5", -12.5),
    ("hello", "hello"), (True, True), (5, 5),
])
def test_coerce_outgoing(raw, expected):
    assert qsc._coerce_outgoing(raw) == expected


def test_safe_id_and_prop():
    assert qsc._safe_id("Pgm Gain.1") == "Pgm_Gain_1"      # id: no dots
    assert qsc._safe_prop("input.1.gain") == "input.1.gain"  # prop: keep dots
    assert qsc._safe_prop("weird name*") == "weird_name_"


def test_category_for():
    assert qsc._category_for("gain") == "Gain"
    assert qsc._category_for("router_with_output") == "Router"
    assert qsc._category_for("snapshot_controller") == "Snapshots"
    assert qsc._category_for("some_plugin_x") == "Some Plugin X"


def test_csv_ints():
    assert qsc._csv_ints("1, 3,4") == [1, 3, 4]
    assert qsc._csv_ints("") == []


# ── Command catalog integrity ──

def test_command_catalog_and_handlers_match():
    cmds = qsc._build_commands()
    assert set(cmds) == set(qsc._COMMAND_HANDLERS)
    di = qsc.QSCQRCDriver.DRIVER_INFO
    for qa in di["quick_actions"]:
        assert qa in cmds, f"quick_action {qa} missing from commands"
    for a in di["actions"]:
        if a.get("kind", "command") == "command":
            assert a.get("command", a["id"]) in cmds


def test_child_types_dynamic_string():
    cet = qsc.QSCQRCDriver.DRIVER_INFO["child_entity_types"]
    for t in ("component", "named_control"):
        assert cet[t]["dynamic"] is True
        assert cet[t]["id_format"]["type"] == "string"


# ── Discovery probe uses StatusGet, not EngineStatus ──

def test_discovery_probe_is_statusget():
    probe = qsc.QSCQRCDriver.DRIVER_INFO["discovery"]["tcp_probe"]
    assert "StatusGet" in probe["send_ascii"]
    assert "EngineStatus" not in probe["send_ascii"]
    assert probe["send_ascii"].endswith("\x00")


# ── Status mapping from a real StatusGet ──

def test_status_get_mapping():
    drv = _make_driver()
    drv._update_status_from_payload(FX["status_get_result"])
    assert drv.get_state("core_platform") == "Core 24f"
    assert drv.get_state("core_state") == "Active"
    assert drv.get_state("core_design_name") == "Untitled"
    assert drv.get_state("core_health") == "OK"
    assert drv.get_state("core_health_code") == 0
    assert drv.get_state("core_standby") is False


# ── Component schema building from real GetControls ──

def test_register_component_gain_schema():
    drv = _make_driver()
    controls = FX["get_controls"]["PgmGain"]["Controls"]
    drv._register_component("PgmGain", "PgmGain", "gain", controls)
    sch = drv._schemas[("component", "PgmGain")]
    # gain -> number with min/max + __str companion; booleans + companions
    assert sch["gain"]["type"] == "number"
    assert sch["gain"]["min"] == -100.0 and sch["gain"]["max"] == 20.0
    assert sch["gain__str"]["type"] == "string"
    assert sch["mute"]["type"] == "boolean"
    assert sch["mute__str"]["type"] == "string"
    st = drv.get_child_state("component", "PgmGain")
    assert st["name"] == "PgmGain"
    assert st["category"] == "Gain"
    assert st["control_count"] == 4


def test_register_component_router_boolean_crosspoints():
    drv = _make_driver()
    controls = FX["get_controls"]["PrgmRouter"]["Controls"]
    drv._register_component("PrgmRouter", "PrgmRouter", "router_with_output",
                            controls)
    sch = drv._schemas[("component", "PrgmRouter")]
    # The real router exposes boolean output.N.input.M.select crosspoints
    # (NOT select.N integers as the old driver assumed) AND select.N integers.
    assert sch["output.1.input.1.select"]["type"] == "boolean"
    assert sch["select.1"]["type"] == "integer"
    assert sch["mute.1"]["type"] == "boolean"


def test_register_component_mixer_text_and_trigger_handling():
    drv = _make_driver()
    controls = FX["get_controls"]["PrgMix"]["Controls"]
    drv._register_component("PrgMix", "PrgMix", "mixer", controls)
    sch = drv._schemas[("component", "PrgMix")]
    # Text label -> string, no __str companion (the String is the value).
    assert sch["input.1.label"]["type"] == "string"
    assert "input.1.label__str" not in sch
    # Float crosspoint gain -> number.
    assert sch["input.1.output.1.gain"]["type"] == "number"


def test_register_component_snapshot_skips_triggers():
    drv = _make_driver()
    controls = FX["get_controls"]["RoomScenes"]["Controls"]
    drv._register_component("RoomScenes", "RoomScenes", "snapshot_controller",
                            controls)
    sch = drv._schemas[("component", "RoomScenes")]
    # Trigger controls (load.next, save.1, load.trigger.1) carry no value and
    # must be skipped from state; Boolean/Integer/State Trigger are kept.
    assert "load.next" not in sch
    assert "save.1" not in sch
    assert sch["last.1"]["type"] == "boolean"          # RO indicator
    assert sch["load.state.1"]["type"] == "integer"
    assert sch["load.1"]["type"] == "number"           # State Trigger has a value


# ── Change-group push fan-out against real captures ──

def test_push_fanout_full_and_delta():
    drv = _make_driver()
    controls = FX["get_controls"]["PgmGain"]["Controls"]
    drv._register_component("PgmGain", "PgmGain", "gain", controls)
    # Build the route map the way _discover_topology does.
    for c in controls:
        if c["Type"] == "Trigger":
            continue
        drv._control_route[("PgmGain", c["Name"])] = {
            "ctype": "component", "sid": "PgmGain", "prop": c["Name"],
            "type": qsc._QSYS_TYPE_MAP.get(c["Type"], "string"),
            "value_from": "string" if c["Type"] in ("Text", "Time") else "value",
            "str_prop": (f"{c['Name']}__str"
                         if c["Type"] not in ("Text", "Time") else None),
            "pos_prop": None,
        }
    drv._handle_change_group_changes(FX["autopoll_push_full"]["params"])
    st = drv.get_child_state("component", "PgmGain")
    assert st["gain"] == -6.0
    assert st["gain__str"] == "-6.00dB"
    assert st["mute"] is False

    drv._handle_change_group_changes(FX["autopoll_push_delta"]["params"])
    st = drv.get_child_state("component", "PgmGain")
    assert st["gain"] == -18.0
    assert st["gain__str"] == "-18.0dB"


def test_push_fanout_named_control():
    drv = _make_driver()
    drv.register_child("named_control", "Volume", schema={
        "name": {"type": "string"}, "value": {"type": "number"},
        "string": {"type": "string"}, "position": {"type": "number"}})
    drv._control_route[(None, "Volume")] = {
        "ctype": "named_control", "sid": "Volume", "prop": "value",
        "type": "number", "value_from": "value",
        "str_prop": "string", "pos_prop": "position"}
    drv._handle_change_group_changes(FX["named_push"]["params"])
    st = drv.get_child_state("named_control", "Volume")
    assert st["value"] == -44.0
    assert st["string"] == "-44.0dB"
    assert st["position"] == pytest.approx(0.46666666)


# ── Named-control type inference ──

def test_infer_nc_type():
    assert qsc.QSCQRCDriver._infer_nc_type(True) == "boolean"
    assert qsc.QSCQRCDriver._infer_nc_type(-12.0) == "number"
    assert qsc.QSCQRCDriver._infer_nc_type("text") == "string"


# ── Reconcile: a component that disappears is deregistered ──

def test_reconcile_drops_missing_component():
    drv = _make_driver()
    pg = FX["get_controls"]["PgmGain"]["Controls"]
    drv._register_component("PgmGain", "PgmGain", "gain", pg)
    drv._register_component("Ghost", "Ghost", "gain", pg)
    assert set(drv.list_children("component")) == {"PgmGain", "Ghost"}
    # Simulate a reconcile where only PgmGain remains.
    seen = {"PgmGain"}
    for sid in list(drv._component_codename):
        if sid not in seen:
            drv.deregister_child("component", sid)
            drv._component_codename.pop(sid, None)
            drv._component_schemas.pop(sid, None)
    assert drv.list_children("component") == ["PgmGain"]


# ── Command dispatch builds the right QRC envelope ──

def test_set_gain_builds_component_set(monkeypatch):
    drv = _make_driver()
    drv._component_codename["PgmGain"] = "PgmGain"
    sent = {}

    async def fake_send(method, params=None, expect_response=True, timeout=5.0):
        sent["method"] = method
        sent["params"] = params
        return None
    drv._send_jsonrpc = fake_send  # type: ignore[assignment]
    asyncio.run(drv.send_command("set_gain", {"component": "PgmGain",
                                              "gain_db": -12, "ramp": 1.5}))
    assert sent["method"] == "Component.Set"
    assert sent["params"]["Name"] == "PgmGain"
    ctrl = sent["params"]["Controls"][0]
    assert ctrl["Name"] == "gain" and ctrl["Value"] == -12.0 and ctrl["Ramp"] == 1.5


def test_mixer_crosspoint_builds_envelope():
    drv = _make_driver()
    drv._component_codename["PrgMix"] = "PrgMix"
    sent = {}

    async def fake_send(method, params=None, expect_response=True, timeout=5.0):
        sent["method"] = method
        sent["params"] = params
    drv._send_jsonrpc = fake_send  # type: ignore[assignment]
    asyncio.run(drv.send_command("mixer_set_crosspoint_gain", {
        "component": "PrgMix", "inputs": "1-3", "outputs": "*",
        "gain_db": -6, "ramp": 0}))
    assert sent["method"] == "Mixer.SetCrossPointGain"
    assert sent["params"] == {"Name": "PrgMix", "Inputs": "1-3",
                              "Outputs": "*", "Value": -6.0}


def test_pa_page_submit_parapi_params():
    drv = _make_driver()
    sent = {}

    async def fake_send(method, params=None, expect_response=True, timeout=5.0):
        sent["method"] = method
        sent["params"] = params
    drv._send_jsonrpc = fake_send  # type: ignore[assignment]
    asyncio.run(drv.send_command("pa_page_submit", {
        "mode": "live", "zones": "2,3", "priority": "3", "station": "4",
        "max_page_time": "45"}))
    assert sent["method"] == "PA.PageSubmit"
    assert sent["params"]["Mode"] == "live"
    assert sent["params"]["Zones"] == [2, 3]
    assert sent["params"]["Priority"] == 3
    assert sent["params"]["Station"] == 4
    assert sent["params"]["MaxPageTime"] == 45


# ── Keep-alive / liveness health loop ──
# AutoPoll is core->client only, so without client traffic the Core closes the
# socket at 60s (flap) and a vanished Core is never noticed. The health loop
# NoOps to keep the session alive and tears down on consecutive failures.

def test_health_loop_tears_down_after_consecutive_failures(monkeypatch):
    monkeypatch.setattr(qsc, "KEEPALIVE_INTERVAL_S", 0.001)
    monkeypatch.setattr(qsc, "KEEPALIVE_TIMEOUT_S", 0.001)
    drv = _make_driver()
    drv.transport = SimpleNamespace(connected=True)
    calls = {"noop": 0, "down": 0}

    async def fake_send(method, params=None, expect_response=True, timeout=5.0):
        if method == "NoOp":
            calls["noop"] += 1
            raise TimeoutError("no answer")
    drv._send_jsonrpc = fake_send  # type: ignore[assignment]

    def fake_down():
        calls["down"] += 1
        drv.transport.connected = False
    drv._handle_transport_disconnect = fake_down  # type: ignore[assignment]

    asyncio.run(asyncio.wait_for(drv._health_loop(), timeout=2))
    assert calls["down"] == 1
    assert calls["noop"] == qsc.KEEPALIVE_MAX_FAILURES


def test_health_loop_success_resets_failure_count(monkeypatch):
    monkeypatch.setattr(qsc, "KEEPALIVE_INTERVAL_S", 0.001)
    drv = _make_driver()
    drv.transport = SimpleNamespace(connected=True)
    calls = {"noop": 0, "down": 0}

    async def fake_send(method, params=None, expect_response=True, timeout=5.0):
        if method == "NoOp":
            calls["noop"] += 1
            if calls["noop"] >= 5:          # end the loop after a few good pings
                drv.transport.connected = False
    drv._send_jsonrpc = fake_send  # type: ignore[assignment]
    drv._handle_transport_disconnect = lambda: calls.__setitem__(
        "down", calls["down"] + 1)  # type: ignore[assignment]

    asyncio.run(asyncio.wait_for(drv._health_loop(), timeout=2))
    assert calls["down"] == 0
    assert calls["noop"] >= 5
    assert drv._health_failures == 0


def test_force_disconnect_nulls_task_and_fires_handler():
    drv = _make_driver()
    drv._health_task = "sentinel"   # we're notionally inside the loop
    fired = {"n": 0}
    drv._handle_transport_disconnect = lambda: fired.__setitem__(
        "n", fired["n"] + 1)  # type: ignore[assignment]
    drv._force_disconnect()
    assert drv._health_task is None   # avoids self-cancel from the handler
    assert fired["n"] == 1
    # The typed no_response fault is stashed for the classifier — this
    # disconnect has no exception to carry the cause.
    assert drv.stashed_fault is not None
    assert drv.stashed_fault[0] == "no_response"


# ── Param pickers (platform §69 Phase 2 adoption) ──

def test_set_component_control_cascades_off_component():
    cmds = qsc._build_commands()
    for name in ("set_component_control", "get_component_control"):
        ctrl = cmds[name]["params"]["control"]
        assert ctrl["options_from"] == {
            "param": "component", "source": "child_schema",
        }
        # The sibling it names is the component child_id param.
        assert cmds[name]["params"]["component"]["type"] == "child_id"


def test_snapshot_bank_name_uses_options_state():
    cmds = qsc._build_commands()
    for name in ("snapshot_load", "snapshot_save"):
        assert cmds[name]["params"]["bank_name"]["options_state"] == "snapshot_banks"


def test_register_component_marks_controls_only():
    drv = _make_driver()
    controls = FX["get_controls"]["PgmGain"]["Controls"]
    drv._register_component("PgmGain", "PgmGain", "gain", controls)
    sch = drv._schemas[("component", "PgmGain")]
    # Real, readable controls carry control: True so the cascade picker offers
    # them.
    assert sch["gain"].get("control") is True
    assert sch["mute"].get("control") is True
    # Metadata + the __str display mirror are NOT controls.
    for meta in ("name", "category", "qsys_type", "control_count", "gain__str"):
        assert sch[meta].get("control") is not True


def test_discover_topology_publishes_snapshot_banks():
    drv = _make_driver()

    async def fake_send(method, params=None, **_kw):
        if method == "Component.GetComponents":
            return FX["get_components_result"]
        if method == "Component.GetControls":
            return FX["get_controls"][params["Name"]]
        return {}  # ChangeGroup.* / AutoPoll — no changes to fan in

    drv._send_jsonrpc = fake_send  # type: ignore[assignment]
    asyncio.run(drv._discover_topology())

    # RoomScenes is the only snapshot_controller in the fixture roster.
    banks = json.loads(drv.get_state("snapshot_banks"))
    assert banks == ["RoomScenes"]
    assert drv.get_state("component_count") == 5


def test_set_component_control_value_type_follows_control():
    cmds = qsc._build_commands()
    value = cmds["set_component_control"]["params"]["value"]
    assert value["type_from"] == {"param": "control"}
    # and the named sibling is the child_schema cascade it chains through
    control = cmds["set_component_control"]["params"]["control"]
    assert control["options_from"]["source"] == "child_schema"
