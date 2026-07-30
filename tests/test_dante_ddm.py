"""Driver + simulator tests for dante_ddm (Audinate Managed API / GraphQL).

No Dante Domain Manager on hand, so correctness is a dual-proof round trip: the
real driver's httpx client is wired to the real simulator's GraphQL handler via
httpx.MockTransport — the driver POSTs /graphql, the sim mutates, the driver
re-reads it back, both sides asserted.

Covers the v1.7.0 first-class adoption:
  - child entities: each Dante device is a dynamic string-id child whose Rx
    channels carry their live subscription (routing) as child state;
  - param pickers: rx_device / tx_device are device pickers, rx_channel
    cascades off the picked device's schema, tx_channel offers the published
    Tx-channel-name list;
  - connection fault: a rejected API key surfaces as an auth-worded
    ConnectionError the shared classifier tags auth_failed.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after collection). httpx is a real dependency.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "dante_ddm.py"
SIM_PATH = REPO_ROOT / "audio" / "dante_ddm_sim.py"

_REAL_ASYNC_CLIENT = httpx.AsyncClient


# ── Platform stand-ins ──────────────────────────────────────────────────────

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


class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver dynamic child-entity API."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        # ctype -> {sid -> {"schema": {...}, "state": {...}}}
        self._children: dict[str, dict[str, dict]] = {}

    def _type_def(self, ctype: str) -> dict:
        return self.DRIVER_INFO["child_entity_types"][ctype]

    def register_child(self, ctype, sid, schema=None, initial_state=None) -> None:
        bucket = self._children.setdefault(ctype, {})
        if sid in bucket:
            return  # idempotent (platform semantics)
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
        bucket[sid] = {"schema": eff, "state": st}

    def deregister_child(self, ctype, sid) -> None:
        self._children.get(ctype, {}).pop(sid, None)

    def is_child_registered(self, ctype, sid) -> bool:
        return sid in self._children.get(ctype, {})

    def list_children(self, ctype) -> list:
        return list(self._children.get(ctype, {}).keys())

    def get_child_state(self, ctype, sid) -> dict:
        e = self._children.get(ctype, {}).get(sid)
        return dict(e["state"]) if e else {}

    def get_child_schema(self, ctype, sid) -> dict:
        e = self._children.get(ctype, {}).get(sid)
        return dict(e["schema"]) if e else {}

    def set_child_state_batch(self, ctype, sid, updates) -> None:
        e = self._children.get(ctype, {}).get(sid)
        if e is None:
            raise ValueError(f"child {ctype}/{sid} not registered")
        for prop in updates:
            if prop not in e["schema"]:
                raise ValueError(f"unknown child prop {prop!r}")
        e["state"].update(updates)

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    def _handle_transport_disconnect(self) -> None:
        pass

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
            await self.events.emit(f"device.connected.{self.device_id}")
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
            await self.events.emit(f"device.disconnected.{self.device_id}")
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
        await self.events.emit(f"device.disconnected.{self.device_id}")


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


DRV = _load("dante_ddm_under_test", DRIVER_PATH)
SIM = _load("dante_ddm_sim_under_test", SIM_PATH)


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
    """A drop-in httpx.AsyncClient that routes through the sim, for connect()."""
    def make(**kw):
        kw.pop("verify", None)
        kw["transport"] = httpx.MockTransport(_make_handler(link))
        return _REAL_ASYNC_CLIENT(**kw)
    return make


_CFG = {
    "host": "test", "port": 443, "api_key": "k",
    "domain_name": "OpenAVC Domain", "poll_interval": 0,
}


def _make_driver(link):
    """A driver wired to the sim, primed as if already connected (skips the
    connect() handshake so a test can drive _refresh_devices / commands)."""
    d = DRV.DanteDDMDriver("dante1", dict(_CFG), _FakeState(), _FakeEvents())
    d._base_url = "https://test"
    d._client = _REAL_ASYNC_CLIENT(
        base_url="https://test", transport=httpx.MockTransport(_make_handler(link)))
    d._domain_id = "d-001"
    d._domain_name = "OpenAVC Domain"
    d._connected = True
    return d


async def _close(driver):
    if driver._client:
        await driver._client.aclose()


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_and_min_platform():
    info = DRV.DanteDDMDriver.DRIVER_INFO
    assert info["version"] == "1.7.1"
    assert info["min_platform_version"] == "0.24.0"


def test_device_child_type_declared():
    types = DRV.DanteDDMDriver.DRIVER_INFO["child_entity_types"]
    assert set(types) == {"device"}
    dev = types["device"]
    assert dev["dynamic"] is True
    assert dev["id_format"]["type"] == "string"
    sv = dev["state_variables"]
    assert "online" not in sv and "label" not in sv
    assert set(sv) == {"name", "rx_channels", "tx_channels", "subscribed"}


def test_route_params_use_pickers():
    cmds = DRV.DanteDDMDriver.DRIVER_INFO["commands"]
    route = cmds["route"]["params"]
    assert route["rx_device"]["type"] == "child_id"
    assert route["rx_device"]["child_type"] == "device"
    assert route["tx_device"]["type"] == "child_id"
    assert route["rx_channel"]["options_from"] == {
        "param": "rx_device", "source": "child_schema"}
    assert route["tx_channel"]["options_state"] == "tx_channel_names"
    unroute = cmds["unroute"]["params"]
    assert unroute["rx_device"]["child_type"] == "device"
    assert unroute["rx_channel"]["options_from"]["param"] == "rx_device"


# ── CE: devices discovered as children ───────────────────────────────────────

def test_refresh_registers_device_children():
    async def go():
        driver = _make_driver(_Link(SIM.DanteDdmSimulator("sim1", {})))
        try:
            await driver._refresh_devices()
            assert set(driver.list_children("device")) == {
                "Tesira-1", "MXA920-1", "AMP-1"}
            assert driver.get_state("device_count") == 3
            assert driver.get_state("subscription_count") == 2

            tesira = driver.get_child_state("device", "Tesira-1")
            assert tesira["name"] == "Tesira-1"
            assert tesira["rx_channels"] == 8
            assert tesira["tx_channels"] == 8
            assert tesira["subscribed"] == 2
            # Rx 1 and 2 are subscribed; the rest are empty.
            assert tesira["rx1"] == "MXA920-1: Channel 1"
            assert tesira["rx2"] == "MXA920-1: Channel 2"
            assert tesira["rx3"] == ""

            amp = driver.get_child_state("device", "AMP-1")
            assert amp["rx_channels"] == 4
            assert amp["subscribed"] == 0

            # A pure transmitter has no rx props beyond the summary/reserved.
            mxa = driver.get_child_state("device", "MXA920-1")
            assert mxa["rx_channels"] == 0
            assert not any(k.startswith("rx") for k in mxa if k != "rx_channels")
        finally:
            await _close(driver)

    asyncio.run(go())


def test_rx_channel_props_are_control_flagged():
    async def go():
        driver = _make_driver(_Link(SIM.DanteDdmSimulator("sim1", {})))
        try:
            await driver._refresh_devices()
            schema = driver.get_child_schema("device", "Tesira-1")
            # rx props feed the rx_channel picker cascade; summary props do not.
            assert schema["rx1"]["control"] is True
            assert schema["rx1"]["cloud_priority"] == "high"
            assert schema["rx1"]["label"] == "Input 1"
            assert "control" not in schema["name"]
        finally:
            await _close(driver)

    asyncio.run(go())


def test_tx_channel_names_published():
    async def go():
        driver = _make_driver(_Link(SIM.DanteDdmSimulator("sim1", {})))
        try:
            await driver._refresh_devices()
            import json
            names = json.loads(driver.get_state("tx_channel_names"))
            assert "Channel 1" in names          # from MXA920-1
            assert "Output 5" in names           # from Tesira-1
            assert names == sorted(names)
        finally:
            await _close(driver)

    asyncio.run(go())


# ── Routing round trips ──────────────────────────────────────────────────────

def test_route_round_trip_picker_ids():
    async def go():
        link = _Link(SIM.DanteDdmSimulator("sim1", {}))
        driver = _make_driver(link)
        try:
            await driver._refresh_devices()
            # Picker ids: rx_device/tx_device sids, rx_channel the schema key.
            await driver.send_command("route", {
                "rx_device": "AMP-1", "rx_channel": "rx1",
                "tx_device": "Tesira-1", "tx_channel": "Output 3",
            })
            # Sim side: AMP-1 Rx index 1 now subscribes to Tesira-1 / Output 3.
            amp_sim = next(d for d in link.sim._devices if d["name"] == "AMP-1")
            rx1 = next(c for c in amp_sim["rxChannels"] if c["index"] == 1)
            assert rx1["subscribedDevice"] == "Tesira-1"
            assert rx1["subscribedChannel"] == "Output 3"
            # Driver side: _set_subscription refreshes, so the child reflects it.
            assert driver.get_child_state("device", "AMP-1")["rx1"] == "Tesira-1: Output 3"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_route_by_channel_name():
    async def go():
        link = _Link(SIM.DanteDdmSimulator("sim1", {}))
        driver = _make_driver(link)
        try:
            await driver._refresh_devices()
            # rx_channel given as the channel name rather than the picker key.
            await driver.send_command("route", {
                "rx_device": "AMP-1", "rx_channel": "Input 2",
                "tx_device": "Tesira-1", "tx_channel": "Output 1",
            })
            amp_sim = next(d for d in link.sim._devices if d["name"] == "AMP-1")
            rx2 = next(c for c in amp_sim["rxChannels"] if c["index"] == 2)
            assert rx2["subscribedDevice"] == "Tesira-1"
        finally:
            await _close(driver)

    asyncio.run(go())


def test_unroute_round_trip():
    async def go():
        link = _Link(SIM.DanteDdmSimulator("sim1", {}))
        driver = _make_driver(link)
        try:
            await driver._refresh_devices()
            # Tesira-1 Rx 1 starts subscribed to MXA920-1.
            assert driver.get_child_state("device", "Tesira-1")["rx1"]
            await driver.send_command("unroute", {
                "rx_device": "Tesira-1", "rx_channel": "rx1"})
            assert driver.get_child_state("device", "Tesira-1")["rx1"] == ""
        finally:
            await _close(driver)

    asyncio.run(go())


def test_refresh_children_reports_count():
    async def go():
        driver = _make_driver(_Link(SIM.DanteDdmSimulator("sim1", {})))
        try:
            result = await driver.refresh_children()
            assert result == {"devices": 3}
        finally:
            await _close(driver)

    asyncio.run(go())


def test_reconcile_drops_absent_device():
    async def go():
        link = _Link(SIM.DanteDdmSimulator("sim1", {}))
        driver = _make_driver(link)
        try:
            await driver._refresh_devices()
            assert driver.is_child_registered("device", "AMP-1")
            # AMP-1 leaves the domain.
            link.sim._devices = [d for d in link.sim._devices if d["name"] != "AMP-1"]
            await driver._refresh_devices()
            assert not driver.is_child_registered("device", "AMP-1")
            assert driver.is_child_registered("device", "Tesira-1")
        finally:
            await _close(driver)

    asyncio.run(go())


def test_device_name_resolution():
    driver = _make_driver(_Link(SIM.DanteDdmSimulator("sim1", {})))
    asyncio.run(driver._refresh_devices())
    assert driver._device_name_for("Tesira-1") == "Tesira-1"
    # An unknown value passes through as a literal device name.
    assert driver._device_name_for("Some Other Device") == "Some Other Device"
    asyncio.run(_close(driver))


# ── Connection fault: rejected API key ───────────────────────────────────────

def test_graphql_401_raises_auth_worded_error():
    async def go():
        link = _Link(SIM.DanteDdmSimulator("sim1", {}))
        link.sim.active_errors.add("auth_failure")  # sim returns 401
        driver = _make_driver(link)
        try:
            with pytest.raises(ConnectionError) as ei:
                await driver._graphql("query { domains { id } }")
            # Wording the shared connection-fault classifier maps to auth_failed.
            assert "authentication failed" in str(ei.value).lower()
        finally:
            await _close(driver)

    asyncio.run(go())


def test_connect_surfaces_auth_failure(monkeypatch):
    async def go():
        link = _Link(SIM.DanteDdmSimulator("sim1", {}))
        link.sim.active_errors.add("auth_failure")
        monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
        driver = DRV.DanteDDMDriver("dante1", dict(_CFG), _FakeState(), _FakeEvents())
        with pytest.raises(ConnectionError) as ei:
            await driver.connect()
        assert "authentication failed" in str(ei.value).lower()

    asyncio.run(go())


def test_connect_registers_children_end_to_end(monkeypatch):
    async def go():
        link = _Link(SIM.DanteDdmSimulator("sim1", {}))
        monkeypatch.setattr(DRV.httpx, "AsyncClient", _client_factory(link))
        driver = DRV.DanteDDMDriver("dante1", dict(_CFG), _FakeState(), _FakeEvents())
        await driver.connect()
        try:
            assert driver._domain_id == "d-001"
            assert set(driver.list_children("device")) == {
                "Tesira-1", "MXA920-1", "AMP-1"}
        finally:
            await driver.disconnect()

    asyncio.run(go())
