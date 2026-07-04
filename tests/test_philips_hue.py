"""Driver + simulator tests for philips_hue (Hue Bridge REST API V1).

No Hue Bridge on hand, so correctness is a dual-proof round trip: the real
driver's httpx client is wired to the real simulator's handle_request via
httpx.MockTransport — the driver PUTs, the sim mutates its light/group
documents, the driver re-reads them back, both sides asserted.

Covers the v2.0.0 Python conversion (was YAML through 1.1.x):
  - child entities: every light is a dynamic child whose per-child schema
    matches its real capability set (extended color vs color-temperature
    vs dimmable vs on/off plug); groups are children including the special
    all-lights group 0 (excluded from GET /groups, fetched at /groups/0);
  - the platform `online` on a light mirrors the Zigbee `reachable` flag;
  - group actions and scene recalls fan out to member lights and the
    driver re-reads the fan-out;
  - device setting: bridge_name writes PUT /config and reads back;
  - pairing setup wizard: link button pressed / not pressed / save+reconnect;
  - connection faults: missing or rejected app key -> typed auth_failed;
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
        self._children: dict[str, dict[int, dict]] = {}
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


class _FakeHTTPSimulator:
    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self.active_errors: set[str] = set()

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value) -> None:
        self._state[key] = value


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


def _make_handler(link):
    def handler(request: httpx.Request) -> httpx.Response:
        if not link.reachable:
            raise httpx.ConnectError("Connection refused")
        body = request.content.decode() if request.content else ""
        status, resp_body = link.sim.handle_request(
            request.method, request.url.path, dict(request.headers), body
        )
        if isinstance(resp_body, dict):
            return httpx.Response(status, json=resp_body)
        return httpx.Response(status, text=str(resp_body))
    return handler


def _client_factory(link):
    """A drop-in httpx.AsyncClient that routes through the sim."""
    def make(**kw):
        kw["transport"] = httpx.MockTransport(_make_handler(link))
        return _REAL_ASYNC_CLIENT(**kw)
    return make


_CFG = {
    "host": "hue-test",
    "port": 80,
    "app_key": "test-app-key",
    "timeout": 5.0,
    "poll_interval": 0,
}


def _make_sim():
    return SIM.PhilipsHueSimulator("sim1", {})


def _make_driver(link, **cfg_overrides):
    """A driver wired to the sim, primed as if already connected."""
    cfg = dict(_CFG)
    cfg.update(cfg_overrides)
    d = DRV.PhilipsHueDriver("hue1", cfg, _FakeState(), _FakeEvents())
    d._app_key = cfg["app_key"]
    d._client = _REAL_ASYNC_CLIENT(
        base_url="http://hue-test",
        transport=httpx.MockTransport(_make_handler(link)),
    )
    d._connected = True
    return d


async def _connected_driver(link, monkeypatch, **cfg_overrides):
    """Run the driver's real connect() against the sim."""
    monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
    cfg = dict(_CFG)
    cfg.update(cfg_overrides)
    d = DRV.PhilipsHueDriver("hue1", cfg, _FakeState(), _FakeEvents())
    await d.connect()
    return d


async def _close(driver):
    if driver._client:
        await driver._client.aclose()


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_and_min_platform():
    info = DRV.PhilipsHueDriver.DRIVER_INFO
    assert info["version"] == "2.0.1"
    assert info["min_platform_version"] == "0.22.0"
    assert info["transport"] == "http"


def test_child_types_declared():
    types = DRV.PhilipsHueDriver.DRIVER_INFO["child_entity_types"]
    assert set(types) == {"light", "group"}
    light = types["light"]
    assert light["dynamic"] is True
    assert light["id_format"]["type"] == "integer"
    group = types["group"]
    assert "dynamic" not in group
    # Group 0 is the bridge's special all-lights group.
    assert group["id_format"]["min"] == 0
    for tdef in types.values():
        sv = tdef["state_variables"]
        assert "online" not in sv and "label" not in sv


def test_params_use_pickers():
    cmds = DRV.PhilipsHueDriver.DRIVER_INFO["commands"]
    assert cmds["light_on"]["params"]["light_id"]["type"] == "child_id"
    assert cmds["light_on"]["params"]["light_id"]["child_type"] == "light"
    assert cmds["group_on"]["params"]["group_id"]["child_type"] == "group"
    assert cmds["recall_scene"]["params"]["scene_id"]["options_state"] == "scene_options"
    assert cmds["group_recall_scene"]["params"]["scene_id"]["options_state"] == "scene_options"


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

def test_connect_registers_children(monkeypatch):
    async def go():
        driver = await _connected_driver(_Link(_make_sim()), monkeypatch)
        try:
            assert set(driver.list_children("light")) == {1, 2, 3, 4, 5}
            assert set(driver.list_children("group")) == {0, 1, 2, 3}
            assert driver.get_state("light_count") == 5
            # Group 0 is not counted as a user group.
            assert driver.get_state("group_count") == 3
            assert driver.get_state("scene_count") == 3
            assert driver.get_state("bridge_name") == "Sim Hue Bridge"
            assert driver.get_state("model_id") == "BSB002"
            assert driver.get_state("zigbee_channel") == 25

            light1 = driver.get_child_state("light", 1)
            assert light1["name"] == "Boardroom Front"
            assert light1["on"] is True
            assert light1["bri"] == 200
            assert light1["colormode"] == "ct"
            assert light1["xy"] == "0.4573,0.4100"

            room = driver.get_child_state("group", 1)
            assert room["name"] == "Boardroom"
            assert room["type"] == "Room"
            assert room["room_class"] == "Meeting"
            assert room["lights"] == "1,2"
            assert room["any_on"] is True and room["all_on"] is False

            all_lights = driver.get_child_state("group", 0)
            assert all_lights["type"] == "LightGroup"
            assert all_lights["any_on"] is True
        finally:
            await _close(driver)

    asyncio.run(go())


def test_light_schemas_match_capabilities(monkeypatch):
    async def go():
        driver = await _connected_driver(_Link(_make_sim()), monkeypatch)
        try:
            color = driver.get_child_schema("light", 1)
            assert {"on", "bri", "ct", "hue", "sat", "xy", "colormode"} <= set(color)
            assert color["on"]["control"] is True
            assert color["on"]["cloud_priority"] == "high"

            ct_only = driver.get_child_schema("light", 3)
            assert "ct" in ct_only and "bri" in ct_only
            assert "hue" not in ct_only and "xy" not in ct_only

            dimmable = driver.get_child_schema("light", 4)
            assert "bri" in dimmable
            assert "ct" not in dimmable and "hue" not in dimmable

            plug = driver.get_child_schema("light", 5)
            assert "on" in plug
            assert "bri" not in plug and "ct" not in plug
        finally:
            await _close(driver)

    asyncio.run(go())


def test_online_mirrors_reachable(monkeypatch):
    async def go():
        link = _Link(_make_sim())
        driver = await _connected_driver(link, monkeypatch)
        try:
            assert driver.get_child_state("light", 4)["online"] is True
            link.sim._lights["4"]["state"]["reachable"] = False
            await driver.poll()
            assert driver.get_child_state("light", 4)["online"] is False
        finally:
            await _close(driver)

    asyncio.run(go())


def test_poll_reconciles_roster(monkeypatch):
    async def go():
        link = _Link(_make_sim())
        driver = await _connected_driver(link, monkeypatch)
        try:
            # Light 5 removed on the bridge; a new group appears.
            del link.sim._lights["5"]
            link.sim._groups["4"] = {
                "name": "Annex", "type": "Zone", "class": "Other",
                "lights": ["4"], "sensors": [], "action": {"on": False},
            }
            await driver.poll()
            assert not driver.is_child_registered("light", 5)
            assert driver.is_child_registered("group", 4)
            assert driver.get_state("light_count") == 4
            assert driver.get_state("group_count") == 4
        finally:
            await _close(driver)

    asyncio.run(go())


def test_scene_options_labeled_with_group(monkeypatch):
    async def go():
        driver = await _connected_driver(_Link(_make_sim()), monkeypatch)
        try:
            options = json.loads(driver.get_state("scene_options"))
            by_value = {o["value"]: o["label"] for o in options}
            # GroupScenes carry their room's name; the LightScene doesn't.
            assert by_value["AB34ef5-presnt1"] == "Presentation (Boardroom)"
            assert by_value["EF12ij3-energz"] == "Energize"
            labels = [o["label"] for o in options]
            assert labels == sorted(labels, key=str.lower)
        finally:
            await _close(driver)

    asyncio.run(go())


def test_refresh_children_reports_counts(monkeypatch):
    async def go():
        driver = await _connected_driver(_Link(_make_sim()), monkeypatch)
        try:
            result = await driver.refresh_children()
            assert result == {"lights": 5, "groups": 4, "scenes": 3}
        finally:
            await _close(driver)

    asyncio.run(go())


# ── Command round trips (driver -> sim -> driver) ───────────────────────────

def test_light_on_off_round_trip():
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            await driver.send_command("light_on", {"light_id": 2})
            assert link.sim._lights["2"]["state"]["on"] is True
            # Applied from the PUT success confirmation, no extra poll.
            assert driver.get_child_state("light", 2)["on"] is True

            await driver.send_command("light_off", {"light_id": 2})
            assert link.sim._lights["2"]["state"]["on"] is False
            assert driver.get_child_state("light", 2)["on"] is False
        finally:
            await _close(driver)

    asyncio.run(go())


def test_light_brightness_round_trip():
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            # Light 4 starts off; the command turns it on in the same PUT
            # (the bridge rejects dimming writes to an off light).
            await driver.send_command(
                "light_set_brightness", {"light_id": 4, "bri": 120})
            assert link.sim._lights["4"]["state"]["on"] is True
            assert link.sim._lights["4"]["state"]["bri"] == 120
            child = driver.get_child_state("light", 4)
            assert child["on"] is True and child["bri"] == 120
        finally:
            await _close(driver)

    asyncio.run(go())


def test_light_color_round_trip():
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            await driver.send_command(
                "light_set_hue_sat", {"light_id": 1, "hue": 21845, "sat": 200})
            state = link.sim._lights["1"]["state"]
            assert state["hue"] == 21845 and state["sat"] == 200
            assert state["colormode"] == "hs"
            child = driver.get_child_state("light", 1)
            assert child["hue"] == 21845 and child["sat"] == 200

            await driver.send_command(
                "light_set_xy", {"light_id": 1, "x": 0.675, "y": 0.322})
            assert link.sim._lights["1"]["state"]["colormode"] == "xy"
            assert driver.get_child_state("light", 1)["xy"] == "0.6750,0.3220"

            await driver.send_command(
                "light_set_color_temp", {"light_id": 3, "ct": 400})
            assert link.sim._lights["3"]["state"]["ct"] == 400
            assert driver.get_child_state("light", 3)["ct"] == 400
        finally:
            await _close(driver)

    asyncio.run(go())


def test_unsupported_field_tolerated_as_partial():
    """An xy write to a ct-only light draws a per-field Hue error next to
    the on-field success; the driver applies the successes and records the
    error instead of failing the command."""
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            await driver.send_command(
                "light_set_xy", {"light_id": 3, "x": 0.5, "y": 0.4})
            assert driver.get_child_state("light", 3)["on"] is True
            assert "xy" not in link.sim._lights["3"]["state"]
            assert "not available" in (driver.get_state("last_error") or "")
        finally:
            await _close(driver)

    asyncio.run(go())


def test_group_on_fans_out_to_member_lights():
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            assert driver.get_child_state("light", 2)["on"] is False
            await driver.send_command("group_on", {"group_id": 1})
            # Sim side: both Boardroom lights are now on.
            assert link.sim._lights["1"]["state"]["on"] is True
            assert link.sim._lights["2"]["state"]["on"] is True
            # Driver side: the post-command re-read caught the fan-out.
            assert driver.get_child_state("light", 2)["on"] is True
            group = driver.get_child_state("group", 1)
            assert group["any_on"] is True and group["all_on"] is True
        finally:
            await _close(driver)

    asyncio.run(go())


def test_group_brightness_round_trip():
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            await driver.send_command(
                "group_set_brightness", {"group_id": 2, "bri": 90})
            assert link.sim._lights["3"]["state"]["bri"] == 90
            assert driver.get_child_state("group", 2)["bri"] == 90
            assert driver.get_child_state("light", 3)["bri"] == 90
        finally:
            await _close(driver)

    asyncio.run(go())


def test_scene_recall_applies_light_states():
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            await driver.send_command("group_recall_scene", {
                "group_id": 1, "scene_id": "AB34ef5-presnt1"})
            # The Presentation scene dims light 1 and turns light 2 off.
            assert link.sim._lights["1"]["state"]["bri"] == 60
            assert link.sim._lights["1"]["state"]["ct"] == 400
            assert link.sim._lights["2"]["state"]["on"] is False
            child1 = driver.get_child_state("light", 1)
            assert child1["bri"] == 60 and child1["ct"] == 400
        finally:
            await _close(driver)

    asyncio.run(go())


def test_all_on_off_via_group_zero():
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            await driver.send_command("all_off", {})
            assert all(
                not li["state"]["on"] for li in link.sim._lights.values())
            assert driver.get_child_state("group", 0)["any_on"] is False
            assert driver.get_child_state("light", 1)["on"] is False

            await driver.send_command("all_on", {})
            assert all(li["state"]["on"] for li in link.sim._lights.values())
            group0 = driver.get_child_state("group", 0)
            assert group0["any_on"] is True and group0["all_on"] is True
        finally:
            await _close(driver)

    asyncio.run(go())


def test_create_user_command_when_paired():
    async def go():
        link = _Link(_make_sim())
        link.sim.set_state("link_button_pressed", True)
        driver = _make_driver(link)
        try:
            result = await driver.send_command("create_user", {})
            assert result[0]["success"]["username"].startswith("simhueappkey")
        finally:
            await _close(driver)

    asyncio.run(go())


# ── Device setting: bridge_name ─────────────────────────────────────────────

def test_bridge_name_setting_round_trip():
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            result = await driver.set_device_setting("bridge_name", "Boardroom Hue")
            assert link.sim.get_state("bridge_name") == "Boardroom Hue"
            # Read back through the config poll, not assumed.
            assert driver.get_state("bridge_name") == "Boardroom Hue"
            assert result == "Boardroom Hue"
        finally:
            await _close(driver)

    asyncio.run(go())


# ── Setup wizard: pairing ───────────────────────────────────────────────────

def _progress_collector():
    steps: list[str] = []

    async def progress(step, pct=None):
        steps.append(step)

    return steps, progress


def test_pair_wizard_success_saves_and_reconnects(monkeypatch):
    async def go():
        link = _Link(_make_sim())
        link.sim.set_state("link_button_pressed", True)
        monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
        driver = DRV.PhilipsHueDriver(
            "hue1", dict(_CFG, app_key=""), _FakeState(), _FakeEvents())
        steps, progress = _progress_collector()
        result = await driver.run_setup_action("pair_bridge", {}, progress)
        assert result["paired"] is True
        assert result["app_key"].startswith("simhueappkey")
        assert result["saved"] is True
        assert result["bridge_id"] == "001788FFFEABCDEF"
        assert driver.config_updates == [{"app_key": result["app_key"]}]
        assert driver.reconnects == 1
        assert steps

    asyncio.run(go())


def test_pair_wizard_no_save(monkeypatch):
    async def go():
        link = _Link(_make_sim())
        link.sim.set_state("link_button_pressed", True)
        monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
        driver = DRV.PhilipsHueDriver(
            "hue1", dict(_CFG, app_key=""), _FakeState(), _FakeEvents())
        _, progress = _progress_collector()
        result = await driver.run_setup_action(
            "pair_bridge", {"save": False}, progress)
        assert result["paired"] is True and result["saved"] is False
        assert driver.config_updates == []
        assert driver.reconnects == 0

    asyncio.run(go())


def test_pair_wizard_link_button_not_pressed(monkeypatch):
    async def go():
        link = _Link(_make_sim())
        monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
        driver = DRV.PhilipsHueDriver(
            "hue1", dict(_CFG, app_key=""), _FakeState(), _FakeEvents())
        _, progress = _progress_collector()
        with pytest.raises(ConnectionError) as ei:
            await driver.run_setup_action("pair_bridge", {}, progress)
        assert "link button" in str(ei.value).lower()
        assert driver.config_updates == []

    asyncio.run(go())


def test_pair_wizard_unreachable(monkeypatch):
    async def go():
        link = _Link(_make_sim())
        link.reachable = False
        monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
        driver = DRV.PhilipsHueDriver(
            "hue1", dict(_CFG, app_key=""), _FakeState(), _FakeEvents())
        _, progress = _progress_collector()
        with pytest.raises(ConnectionError) as ei:
            await driver.run_setup_action("pair_bridge", {}, progress)
        assert "could not reach" in str(ei.value).lower()

    asyncio.run(go())


# ── Connection faults ───────────────────────────────────────────────────────

def test_connect_without_app_key_is_auth_fault(monkeypatch):
    async def go():
        link = _Link(_make_sim())
        monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
        driver = DRV.PhilipsHueDriver(
            "hue1", dict(_CFG, app_key=""), _FakeState(), _FakeEvents())
        with pytest.raises(_FakeConnectionFaultError) as ei:
            await driver.connect()
        assert ei.value.code == "auth_failed"
        assert "pair" in str(ei.value).lower()

    asyncio.run(go())


def test_connect_with_rejected_key_is_auth_fault(monkeypatch):
    async def go():
        link = _Link(_make_sim())
        monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
        # "invalid" is the sim's designated bad-credential sentinel.
        driver = DRV.PhilipsHueDriver(
            "hue1", dict(_CFG, app_key="invalid"), _FakeState(), _FakeEvents())
        with pytest.raises(_FakeConnectionFaultError) as ei:
            await driver.connect()
        assert ei.value.code == "auth_failed"

    asyncio.run(go())


def test_key_revoked_mid_session_faults_on_poll():
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            link.sim.active_errors.add("unauthorized")
            with pytest.raises(_FakeConnectionFaultError) as ei:
                await driver.poll()
            assert ei.value.code == "auth_failed"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_poll_propagates_transport_errors():
    """Never-offline guard: an unreachable bridge must raise from poll()
    so the platform watchdog can flip the device offline."""
    async def go():
        link = _Link(_make_sim())
        driver = _make_driver(link)
        try:
            await driver._refresh_all()
            link.reachable = False
            with pytest.raises(ConnectionError):
                await driver.poll()
        finally:
            await _close(driver)

    asyncio.run(go())


def test_connect_unreachable_is_connection_error(monkeypatch):
    async def go():
        link = _Link(_make_sim())
        link.reachable = False
        monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
        driver = DRV.PhilipsHueDriver(
            "hue1", dict(_CFG), _FakeState(), _FakeEvents())
        with pytest.raises(ConnectionError) as ei:
            await driver.connect()
        assert "could not reach" in str(ei.value).lower()

    asyncio.run(go())
