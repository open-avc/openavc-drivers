"""Driver + simulator tests for philips_hue (Hue Bridge CLIP API v2).

Dual-proof round trip: the real driver's httpx client is wired to the real
simulator's handle_request via httpx.MockTransport — the driver PUTs, the
sim mutates its v2 resource graph, and both sides are asserted. The event
stream is real too: the MockTransport serves /eventstream/clip/v2 as a
streaming response fed by the sim's push_sse_event, so the driver's actual
SSE loop (connect, parse, apply) runs in-test.

Covers the v3.0.0 CLIP v2 rewrite (was v1 API through 2.0.x):
  - child entities: every light is a dynamic child whose per-child schema
    matches its real capability set, with per-light mirek bounds from its
    own mirek_schema; groups are rooms + zones + the whole-home
    bridge_home group, each keyed by its v2 UUID;
  - the platform `online` on a light mirrors its owner device's
    zigbee_connectivity status;
  - SSE push: light/group/reachability/rename changes stream into child
    state without polling; add/delete events trigger a roster refresh;
  - commands PUT partial v2 bodies (on/dimming/color_temperature/color,
    optional dynamics transition) and the sim's SSE echo lands the result
    back in child state; scene recall applies the scene's actions;
  - device setting: bridge_name writes the bridge device's metadata;
  - pairing setup wizard: link button pressed / not pressed / save+reconnect;
  - connection faults: missing or rejected app key -> typed auth_failed
    (v2 signals both as HTTP 403);
  - poll propagates transport errors (never-offline guard).

Loads the driver + simulator with the ``server.*`` / ``simulator.*``
imports stubbed so the community CI stays self-contained (conftest.py
rolls the stubs back after collection). httpx is a real dependency.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "lighting" / "philips_hue.py"
SIM_PATH = REPO_ROOT / "lighting" / "philips_hue_sim.py"

_REAL_ASYNC_CLIENT = httpx.AsyncClient


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value


class _FakeEvents:
    def __init__(self) -> None:
        self.emitted: list[str] = []

    async def emit(self, name, *args, **kwargs):
        self.emitted.append(name)


def _default_for(var_def: dict):
    vt = var_def.get("type", "string")
    if vt == "boolean":
        return False
    if vt == "integer":
        return int(var_def.get("min", 0) or 0)
    if vt in ("number", "float"):
        return float(var_def.get("min", 0) or 0)
    if vt == "enum":
        vals = var_def.get("values", [])
        return vals[0] if vals else ""
    return ""


class _FakeConnectionFaultError(ConnectionError):
    """Mirror of the platform's typed fault (code -> offline_reason)."""

    def __init__(self, message: str = "", *, code: str):
        super().__init__(message)
        self.code = code


class _FakeBaseDriver:
    """Functional stand-in for the platform BaseDriver child-entity API."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        # ctype -> {local_id -> {"schema": {...}, "state": {...}}}
        self._children: dict[str, dict[str, dict]] = {}
        self.config_updates: list[dict] = []
        self.reconnects = 0

    def _type_def(self, ctype: str) -> dict:
        return self.DRIVER_INFO["child_entity_types"][ctype]

    def register_child(self, ctype, cid, schema=None, initial_state=None) -> None:
        bucket = self._children.setdefault(ctype, {})
        if cid in bucket:
            return  # idempotent (platform semantics)
        if schema is not None and not self._type_def(ctype).get("dynamic"):
            raise ValueError(f"child type {ctype!r} is not dynamic")
        eff = dict(schema if schema is not None
                   else self._type_def(ctype).get("state_variables", {}))
        eff.setdefault("online", {"type": "boolean"})
        eff.setdefault("label", {"type": "string"})
        ov = dict(initial_state or {})
        for prop in ov:
            if prop not in eff:
                raise ValueError(f"unknown child prop {prop!r}")
        st: dict = {}
        for prop, vd in eff.items():
            if prop == "online":
                st[prop] = ov.get("online", True)
            elif prop == "label":
                st[prop] = ov.get("label", "")
            elif prop in ov:
                st[prop] = ov[prop]
            else:
                st[prop] = _default_for(vd)
        bucket[cid] = {"schema": eff, "state": st}

    def deregister_child(self, ctype, cid) -> None:
        self._children.get(ctype, {}).pop(cid, None)

    def is_child_registered(self, ctype, cid) -> bool:
        return cid in self._children.get(ctype, {})

    def list_children(self, ctype) -> list:
        return list(self._children.get(ctype, {}).keys())

    def get_child_state(self, ctype, cid) -> dict:
        e = self._children.get(ctype, {}).get(cid)
        return dict(e["state"]) if e else {}

    def get_child_schema(self, ctype, cid) -> dict:
        e = self._children.get(ctype, {}).get(cid)
        return dict(e["schema"]) if e else {}

    def set_child_state_batch(self, ctype, cid, updates) -> None:
        e = self._children.get(ctype, {}).get(cid)
        if e is None:
            raise ValueError(f"child {ctype}/{cid} not registered")
        for prop in updates:
            if prop not in e["schema"]:
                raise ValueError(f"unknown child prop {prop!r}")
        e["state"].update(updates)

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def set_states(self, updates: dict) -> None:
        for key, value in updates.items():
            self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    async def start_polling(self, interval) -> None:
        pass

    async def stop_polling(self) -> None:
        pass

    async def request_config_update(self, delta: dict) -> None:
        self.config_updates.append(dict(delta))
        self.config.update(delta)

    async def request_reconnect(self) -> None:
        self.reconnects += 1

    # Hook defaults + the hook-driven connect lifecycle the platform runs
    # (the driver has no connect() of its own anymore).
    async def _pre_connect(self):
        return None

    async def _create_transport(self, transport_type):
        return None

    async def _post_connect(self):
        return None

    async def _initial_sync(self):
        return None

    async def _close_session(self):
        return None

    def _link_alive(self):
        return False

    async def _start_push(self):
        return None

    async def _stop_push(self):
        return None

    async def connect(self):
        # Mirrors BaseDriver.connect()'s stages for a driver-owned session.
        await self._stop_push()
        await self._close_session()
        await self._pre_connect()
        transport_type = self.config.get("transport") or self.DRIVER_INFO.get(
            "transport", "tcp"
        )
        await self._create_transport(transport_type)
        try:
            await self._post_connect()
            self._connected = True
            self.set_state("connected", True)
        except Exception:
            await self._close_session()
            self._connected = False
            raise
        await self._start_push()
        try:
            await self._initial_sync()
        except Exception:
            await self._stop_push()
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            raise
        poll_interval = self.config.get("poll_interval", 0)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self):
        await self._stop_push()
        await self.stop_polling()
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)


class _FakeHTTPSimulator:
    SIMULATOR_INFO: dict = {}
    sse_paths: list[str] = []

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self.active_errors: set[str] = set()
        # Event-stream stand-in: emitted payloads are recorded and fanned
        # out to any queues the MockTransport eventstream handler registered.
        self.sse_emitted: list[str] = []
        self._sse_queues: list[asyncio.Queue] = []

    @property
    def state(self) -> dict:
        return dict(self._state)

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value) -> None:
        self._state[key] = value

    def push_sse_event(self, data: str) -> None:
        self.sse_emitted.append(data)
        for queue in list(self._sse_queues):
            queue.put_nowait(data)

    def log_protocol(self, *_a, **_k) -> None:
        pass


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server
    for sub in ("drivers", "utils"):
        m = ModuleType(f"server.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"server.{sub}"] = m
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    base.ConnectionFaultError = _FakeConnectionFaultError
    sys.modules["server.drivers.base"] = base
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["simulator"] = sim_pkg
    sim_http = ModuleType("simulator.http_simulator")
    sim_http.HTTPSimulator = _FakeHTTPSimulator
    sys.modules["simulator.http_simulator"] = sim_http

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("philips_hue_under_test", DRIVER_PATH)
SIM = _load("philips_hue_sim_under_test", SIM_PATH)


# ── Harness ─────────────────────────────────────────────────────────────────

class _Link:
    def __init__(self, sim) -> None:
        self.sim = sim
        self.reachable = True


async def _sse_body(queue: asyncio.Queue):
    while True:
        item = await queue.get()
        if item is None:
            return
        yield f"data: {item}\n\n".encode()


def _make_handler(link):
    def handler(request: httpx.Request) -> httpx.Response:
        if not link.reachable:
            raise httpx.ConnectError("Connection refused")
        path = request.url.path
        headers = dict(request.headers)
        # Event stream: held open, fed by the sim's push_sse_event.
        if path == "/eventstream/clip/v2":
            key = headers.get("hue-application-key", "")
            if not key or key == "invalid":
                return httpx.Response(
                    403,
                    json={"errors": [{"description": "unauthorized user"}],
                          "data": []},
                )
            queue: asyncio.Queue = asyncio.Queue()
            link.sim._sse_queues.append(queue)
            return httpx.Response(
                200,
                content=_sse_body(queue),
                headers={"content-type": "text/event-stream"},
            )
        body = request.content.decode() if request.content else ""
        status, resp_body = link.sim.handle_request(
            request.method, path, headers, body
        )
        if isinstance(resp_body, dict):
            return httpx.Response(status, json=resp_body)
        return httpx.Response(status, text=str(resp_body))
    return handler


def _client_factory(link):
    """A drop-in httpx.AsyncClient that routes through the sim."""
    def make(**kw):
        kw["transport"] = httpx.MockTransport(_make_handler(link))
        kw.pop("verify", None)
        kw.pop("limits", None)
        return _REAL_ASYNC_CLIENT(**kw)
    return make


_CFG = {
    "host": "hue-test",
    "app_key": "test-app-key",
    "timeout": 5.0,
    "poll_interval": 0,
}


def _make_sim():
    return SIM.PhilipsHueSimulator("sim1", {})


async def _connected_driver(link, monkeypatch, **cfg_overrides):
    """Run the driver's real connect() against the sim."""
    monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
    cfg = dict(_CFG)
    cfg.update(cfg_overrides)
    d = DRV.PhilipsHueDriver("hue1", cfg, _FakeState(), _FakeEvents())
    await d.connect()
    return d


async def _close(driver):
    await driver._stop_task("_event_task")
    await driver._stop_task("_roster_refresh_task")
    if driver._client:
        await driver._client.aclose()
        driver._client = None


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.02)


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_and_min_platform():
    info = DRV.PhilipsHueDriver.DRIVER_INFO
    assert info["version"] == "3.0.1"
    assert info["min_platform_version"] == "0.24.0"
    assert info["transport"] == "http"
    assert info["ports"] == [443]


def test_child_types_declared():
    types = DRV.PhilipsHueDriver.DRIVER_INFO["child_entity_types"]
    assert set(types) == {"light", "group"}
    light = types["light"]
    assert light["dynamic"] is True
    assert light["id_format"]["type"] == "string"
    group = types["group"]
    assert "dynamic" not in group
    assert group["id_format"]["type"] == "string"
    for tdef in types.values():
        sv = tdef["state_variables"]
        assert "online" not in sv and "label" not in sv


def test_params_use_pickers():
    cmds = DRV.PhilipsHueDriver.DRIVER_INFO["commands"]
    assert cmds["light_on"]["params"]["light"]["type"] == "child_id"
    assert cmds["light_on"]["params"]["light"]["child_type"] == "light"
    assert cmds["group_on"]["params"]["group"]["child_type"] == "group"
    assert cmds["recall_scene"]["params"]["scene"]["options_state"] == "scene_options"


def test_actions_shape():
    info = DRV.PhilipsHueDriver.DRIVER_INFO
    cmds = info["commands"]
    for cmd_id in info["quick_actions"]:
        assert cmd_id in cmds
    setup = next(a for a in info["actions"] if a["kind"] == "setup")
    assert setup["id"] == "pair_bridge"
    assert setup["availability"] == "always"


def test_discovery_probe_matches_sim():
    """The tcp_probe expects "bridgeid" — the sim's unauthenticated
    GET /api/config (like a real bridge's) must carry it."""
    probe = DRV.PhilipsHueDriver.DRIVER_INFO["discovery"]["tcp_probe"]
    assert probe["port"] == 80
    sim = _make_sim()
    status, body = sim.handle_request("GET", "/api/config", {}, "")
    assert status == 200
    assert probe["expect"].strip('"') in json.dumps(body)


# ── CE: lights + groups enumerated as children ──────────────────────────────

@pytest.mark.asyncio
async def test_connect_registers_children(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        assert d.get_state("light_count") == 5
        # 2 rooms + 1 zone; the whole-home group is a child but not counted.
        assert d.get_state("group_count") == 3
        assert len(d.list_children("light")) == 5
        assert len(d.list_children("group")) == 4
        assert d.get_state("scene_count") == 3
        assert d.get_state("bridge_id") == "001788fffeabcdef"
        assert d.get_state("bridge_name") == "Sim Hue Bridge"
        assert d.get_state("model_id") == "BSB002"
        assert d.get_state("sw_version") == "1.108.7"
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_per_capability_schemas(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        color = d.get_child_schema("light", SIM.L1)
        assert {"on", "brightness", "mirek", "xy"} <= set(color)
        color_only = d.get_child_schema("light", SIM.L2)
        assert "xy" in color_only and "mirek" not in color_only
        ct = d.get_child_schema("light", SIM.L3)
        assert "mirek" in ct and "xy" not in ct
        # Per-light mirek bounds come from the light's own mirek_schema.
        assert ct["mirek"]["max"] == 454
        dimmable = d.get_child_schema("light", SIM.L4)
        assert "brightness" in dimmable and "mirek" not in dimmable
        plug = d.get_child_schema("light", SIM.L5)
        assert "on" in plug
        assert not {"brightness", "mirek", "xy"} & set(plug)
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_online_mirrors_zigbee_connectivity(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        assert d.get_child_state("light", SIM.L1)["online"] is True
        # The rack plug's zigbee_connectivity ships connectivity_issue.
        assert d.get_child_state("light", SIM.L5)["online"] is False
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_group_children_carry_grouped_light_state(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        board = d.get_child_state("group", SIM.ROOM_BOARD)
        assert board["name"] == "Boardroom"
        assert board["type"] == "room"
        assert board["room_class"] == "office"
        assert board["on"] is True  # L1 is on
        assert "Boardroom Front" in board["lights"]
        zone = d.get_child_state("group", SIM.ZONE_STAGE)
        assert zone["type"] == "zone"
        assert "Boardroom Front" in zone["lights"]  # zone children are lights
        home = d.get_child_state("group", SIM.HOME)
        assert home["type"] == "home"
        assert home["name"] == "All Lights"
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_scene_options_labeled_with_group(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        options = json.loads(d.get_state("scene_options"))
        labels = {o["label"] for o in options}
        assert "Presentation (Boardroom)" in labels
        assert "Wash Blue (Stage Wash)" in labels
        values = {o["value"] for o in options}
        assert SIM.SCENE_PRESENT in values
    finally:
        await _close(d)


# ── SSE push ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sim_state_change_streams_to_child_state(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        await _wait_for(lambda: link.sim._sse_queues)
        # No polling in this harness (poll_interval 0) — the only path from
        # sim to driver is the event stream.
        link.sim.set_state("light_1_on", False)
        await _wait_for(
            lambda: d.get_child_state("light", SIM.L1)["on"] is False
        )
        # The grouped_light recompute streams the room's any_on too.
        await _wait_for(
            lambda: d.get_child_state("group", SIM.ROOM_BOARD)["on"] is False
        )
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_reachability_change_streams_to_online(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        await _wait_for(lambda: link.sim._sse_queues)
        link.sim.set_state("light_1_reachable", False)
        await _wait_for(
            lambda: d.get_child_state("light", SIM.L1)["online"] is False
        )
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_bridge_rename_streams_to_state(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        await _wait_for(lambda: link.sim._sse_queues)
        link.sim.set_state("bridge_name", "Rack Bridge")
        await _wait_for(
            lambda: d.get_state("bridge_name") == "Rack Bridge"
        )
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_add_delete_events_schedule_roster_refresh(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        payload = json.dumps([{
            "creationtime": "2026-01-01T00:00:00Z",
            "data": [{"id": "someuuid", "type": "light"}],
            "id": "eventuuid",
            "type": "delete",
        }])
        d._apply_event_payload(payload)
        assert d._roster_refresh_task is not None
        # The refresh runs against the unchanged sim — rosters stay intact.
        await asyncio.wait_for(d._roster_refresh_task, timeout=5.0)
        assert d.get_state("light_count") == 5
    finally:
        await _close(d)


# ── Commands ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_light_on_off_round_trip(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        await _wait_for(lambda: link.sim._sse_queues)
        await d.send_command("light_off", {"light": SIM.L1})
        assert link.sim.resources[SIM.L1]["on"]["on"] is False
        await _wait_for(
            lambda: d.get_child_state("light", SIM.L1)["on"] is False
        )
        await d.send_command("light_on", {"light": SIM.L1})
        assert link.sim.resources[SIM.L1]["on"]["on"] is True
        await _wait_for(
            lambda: d.get_child_state("light", SIM.L1)["on"] is True
        )
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_brightness_ct_xy_bodies(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        await _wait_for(lambda: link.sim._sse_queues)
        await d.send_command(
            "set_light_brightness",
            {"light": SIM.L1, "brightness": 42.5, "transition_ms": 400},
        )
        res = link.sim.resources[SIM.L1]
        assert res["dimming"]["brightness"] == 42.5
        assert res["on"]["on"] is True
        await _wait_for(
            lambda: d.get_child_state("light", SIM.L1)["brightness"] == 42.5
        )

        # Out-of-range mirek clamps to the light's own schema (454 max).
        await d.send_command("set_light_ct", {"light": SIM.L3, "mirek": 500})
        assert link.sim.resources[SIM.L3]["color_temperature"]["mirek"] == 454

        await d.send_command(
            "set_light_xy", {"light": SIM.L2, "x": 0.2, "y": 0.3}
        )
        assert link.sim.resources[SIM.L2]["color"]["xy"] == {"x": 0.2, "y": 0.3}
        await _wait_for(
            lambda: d.get_child_state("light", SIM.L2)["xy"] == "0.2000,0.3000"
        )
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_group_commands_fan_out(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        await _wait_for(lambda: link.sim._sse_queues)
        await d.send_command("group_on", {"group": SIM.ROOM_LOBBY})
        assert link.sim.resources[SIM.L3]["on"]["on"] is True
        assert link.sim.resources[SIM.L4]["on"]["on"] is True
        await _wait_for(
            lambda: d.get_child_state("group", SIM.ROOM_LOBBY)["on"] is True
        )
        await d.send_command(
            "set_group_brightness", {"group": SIM.ROOM_LOBBY, "brightness": 25}
        )
        assert link.sim.resources[SIM.L3]["dimming"]["brightness"] == 25.0
        assert link.sim.resources[SIM.L4]["dimming"]["brightness"] == 25.0
        await d.send_command("group_off", {"group": SIM.ROOM_LOBBY})
        assert link.sim.resources[SIM.L3]["on"]["on"] is False
        await _wait_for(
            lambda: d.get_child_state("group", SIM.ROOM_LOBBY)["on"] is False
        )
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_all_on_off_uses_home_group(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        await _wait_for(lambda: link.sim._sse_queues)
        await d.send_command("all_on")
        assert all(
            link.sim.resources[lid]["on"]["on"]
            for lid in (SIM.L1, SIM.L2, SIM.L3, SIM.L4, SIM.L5)
        )
        await _wait_for(
            lambda: d.get_child_state("group", SIM.HOME)["on"] is True
        )
        await d.send_command("all_off")
        assert not any(
            link.sim.resources[lid]["on"]["on"]
            for lid in (SIM.L1, SIM.L2, SIM.L3, SIM.L4, SIM.L5)
        )
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_recall_scene_applies_actions(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        await _wait_for(lambda: link.sim._sse_queues)
        await d.send_command(
            "recall_scene", {"scene": SIM.SCENE_PRESENT, "duration_ms": 1000}
        )
        l1 = link.sim.resources[SIM.L1]
        assert l1["on"]["on"] is True
        assert l1["dimming"]["brightness"] == 100.0
        assert l1["color_temperature"]["mirek"] == 233
        assert link.sim.resources[SIM.L2]["on"]["on"] is False
        await _wait_for(
            lambda: d.get_child_state("light", SIM.L1)["mirek"] == 233
        )
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_light_identify_targets_owner_device(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        result = await d.send_command("light_identify", {"light": SIM.L1})
        assert result == [{"rid": SIM.D1, "rtype": "device"}]
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_unknown_command_and_group_rejected(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        with pytest.raises(ValueError):
            await d.send_command("warp_drive")
        with pytest.raises(ValueError):
            await d.send_command("group_on", {"group": "no-such-group"})
    finally:
        await _close(d)


# ── Device settings ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bridge_name_setting_round_trip(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        await d.set_device_setting("bridge_name", "AV Rack Bridge")
        bdev = link.sim.resources[SIM.BRIDGE_DEV]
        assert bdev["metadata"]["name"] == "AV Rack Bridge"
        assert d.get_state("bridge_name") == "AV Rack Bridge"
        # The resync keeps agreeing with the device.
        await d.poll()
        assert d.get_state("bridge_name") == "AV Rack Bridge"
    finally:
        await _close(d)


# ── Connection faults ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_without_app_key_is_auth_fault(monkeypatch):
    link = _Link(_make_sim())
    monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
    d = DRV.PhilipsHueDriver(
        "hue1", dict(_CFG, app_key=""), _FakeState(), _FakeEvents()
    )
    with pytest.raises(_FakeConnectionFaultError) as excinfo:
        await d.connect()
    assert excinfo.value.code == "auth_failed"
    assert d._client is None


@pytest.mark.asyncio
async def test_connect_with_rejected_key_is_auth_fault(monkeypatch):
    link = _Link(_make_sim())
    monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
    d = DRV.PhilipsHueDriver(
        "hue1", dict(_CFG, app_key="invalid"), _FakeState(), _FakeEvents()
    )
    with pytest.raises(_FakeConnectionFaultError) as excinfo:
        await d.connect()
    assert excinfo.value.code == "auth_failed"


@pytest.mark.asyncio
async def test_mid_session_revocation_surfaces_via_poll(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        link.sim.active_errors.add("unauthorized")
        with pytest.raises(_FakeConnectionFaultError) as excinfo:
            await d.poll()
        assert excinfo.value.code == "auth_failed"
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_poll_propagates_transport_errors(monkeypatch):
    link = _Link(_make_sim())
    d = await _connected_driver(link, monkeypatch)
    try:
        link.reachable = False
        with pytest.raises(ConnectionError):
            await d.poll()
    finally:
        await _close(d)


@pytest.mark.asyncio
async def test_connect_unreachable_is_connection_error(monkeypatch):
    link = _Link(_make_sim())
    link.reachable = False
    monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
    d = DRV.PhilipsHueDriver("hue1", dict(_CFG), _FakeState(), _FakeEvents())
    with pytest.raises(ConnectionError):
        await d.connect()
    assert d._client is None


# ── Pairing wizard ──────────────────────────────────────────────────────────

async def _progress(step, pct=None):
    pass


@pytest.mark.asyncio
async def test_wizard_pairs_saves_and_reconnects(monkeypatch):
    link = _Link(_make_sim())
    link.sim.set_state("link_button_pressed", True)
    monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
    d = DRV.PhilipsHueDriver(
        "hue1", dict(_CFG, app_key=""), _FakeState(), _FakeEvents()
    )
    result = await d.run_setup_action("pair_bridge", {"save": True}, _progress)
    assert result["paired"] is True
    assert result["app_key"] == "simulated-app-key-0001"
    assert result["saved"] is True
    assert d.config["app_key"] == "simulated-app-key-0001"
    assert d.reconnects == 1
    assert result["bridge_id"]


@pytest.mark.asyncio
async def test_wizard_no_save(monkeypatch):
    link = _Link(_make_sim())
    link.sim.set_state("link_button_pressed", True)
    monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
    d = DRV.PhilipsHueDriver(
        "hue1", dict(_CFG, app_key=""), _FakeState(), _FakeEvents()
    )
    result = await d.run_setup_action("pair_bridge", {"save": False}, _progress)
    assert result["saved"] is False
    assert d.config["app_key"] == ""
    assert d.reconnects == 0


@pytest.mark.asyncio
async def test_wizard_link_button_not_pressed(monkeypatch):
    link = _Link(_make_sim())
    monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
    d = DRV.PhilipsHueDriver(
        "hue1", dict(_CFG, app_key=""), _FakeState(), _FakeEvents()
    )
    with pytest.raises(ConnectionError, match="link button"):
        await d.run_setup_action("pair_bridge", {"save": True}, _progress)


@pytest.mark.asyncio
async def test_wizard_unreachable(monkeypatch):
    link = _Link(_make_sim())
    link.reachable = False
    monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
    d = DRV.PhilipsHueDriver(
        "hue1", dict(_CFG, app_key=""), _FakeState(), _FakeEvents()
    )
    with pytest.raises(ConnectionError, match="Could not reach"):
        await d.run_setup_action("pair_bridge", {"save": True}, _progress)


# ── Sim fidelity details ────────────────────────────────────────────────────

def test_sim_clip_requires_app_key():
    sim = _make_sim()
    status, body = sim.handle_request("GET", "/clip/v2/resource", {}, "")
    assert status == 403
    status, _ = sim.handle_request(
        "GET", "/clip/v2/resource", {"hue-application-key": "invalid"}, ""
    )
    assert status == 403
    status, body = sim.handle_request(
        "GET", "/clip/v2/resource", {"hue-application-key": "ok-key"}, ""
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["errors"] == []
    types = {r["type"] for r in payload["data"]}
    assert {"light", "device", "zigbee_connectivity", "room", "zone",
            "bridge_home", "grouped_light", "scene", "bridge"} <= types


def test_sim_pairing_returns_clientkey_when_requested():
    sim = _make_sim()
    sim.set_state("link_button_pressed", True)
    status, body = sim.handle_request(
        "POST", "/api", {},
        json.dumps({"devicetype": "x#y", "generateclientkey": True}),
    )
    assert status == 200
    entry = json.loads(body)[0]["success"]
    assert entry["username"]
    assert entry["clientkey"]


def test_sim_put_emits_update_envelope():
    sim = _make_sim()
    sim.handle_request(
        "PUT", f"/clip/v2/resource/light/{SIM.L1}",
        {"hue-application-key": "k"},
        json.dumps({"on": {"on": False}}),
    )
    assert sim.sse_emitted, "expected an SSE event"
    envelopes = json.loads(sim.sse_emitted[-1])
    assert envelopes[0]["type"] == "update"
    ids = {res["id"] for env in envelopes for res in env["data"]}
    assert SIM.L1 in ids
