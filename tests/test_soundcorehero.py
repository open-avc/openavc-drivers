"""Self-contained tests for the soundcorehero driver (SoundCoreHero multi-zone
audio over an HTTPS/WebSocket API).

The driver owns its I/O (an httpx client for REST commands + a control
WebSocket that streams the resource graph), so the full socket round trip is
exercised against hardware and not committed here. What this module locks down
is the protocol logic that has to be right regardless of the sockets:

  - the WebSocket resource pipeline: snapshot -> child registration + state,
    incremental JSON-Patch deltas applied on top, snapshot reconciliation of
    removed objects;
  - device-level singletons (DSO, cpu/channels/update system signals);
  - orderless objects (controllers) getting a stable synthetic local id, and a
    controller mirroring its bound zone's master volume;
  - command -> REST body mapping and child-id resolution;
  - the connection lifecycle hooks the platform runs (login, WS snapshot gate,
    teardown) with httpx + websockets mocked.

Loads the driver with the ``server.*`` / ``websockets`` imports stubbed so the
community CI stays self-contained (conftest.py rolls the stubs back after this
module is collected). httpx is a real dependency.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "soundcorehero.py"


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


class _FakeBaseDriver:
    """Functional stand-in for the platform BaseDriver: the child-entity API
    (validating props against the driver's declared child state_variables) plus
    the connect() lifecycle the migrated driver relies on (it has no connect()
    of its own — the platform runs the hooks it overrides).
    """

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._last_transport_error = ""
        # ctype -> {local_id -> {"schema": {...}, "state": {...}}}
        self._children: dict[str, dict[int, dict]] = {}
        self.polling_starts = 0

    # Child entity API ----------------------------------------------------
    def _child_schema(self, ctype: str) -> dict:
        eff = dict(self.DRIVER_INFO["child_entity_types"][ctype]["state_variables"])
        eff.setdefault("online", {"type": "boolean"})
        eff.setdefault("label", {"type": "string"})
        return eff

    def register_child(self, ctype, cid, schema=None, initial_state=None) -> None:
        bucket = self._children.setdefault(ctype, {})
        if cid in bucket:
            return  # idempotent (platform semantics)
        eff = self._child_schema(ctype)
        st: dict = {}
        for prop, value in (initial_state or {}).items():
            if prop not in eff:
                raise ValueError(f"unknown child prop {ctype}.{prop!r}")
            st[prop] = value
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

    def set_child_state_batch(self, ctype, cid, updates) -> None:
        e = self._children.get(ctype, {}).get(cid)
        if e is None:
            raise ValueError(f"child {ctype}/{cid} not registered")
        for prop in updates:
            if prop not in e["schema"]:
                raise ValueError(f"unknown child prop {ctype}.{prop!r}")
        e["state"].update(updates)

    # Device state --------------------------------------------------------
    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def set_states(self, updates: dict) -> None:
        for k, v in updates.items():
            self.state.set(f"device.{self.device_id}.{k}", v)

    def get_state(self, key, default=None):
        return self.state.data.get(f"device.{self.device_id}.{key}", default)

    # Polling / push / hook defaults -------------------------------------
    async def start_polling(self, interval) -> None:
        self.polling_starts += 1

    async def stop_polling(self) -> None:
        return None

    async def _start_push(self):
        return None

    async def _stop_push(self):
        return None

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
        return self.transport is not None

    @property
    def connected(self) -> bool:
        return bool(self._connected) and self._link_alive()

    # The hook-driven connect / disconnect the platform runs --------------
    async def connect(self):
        self._last_transport_error = ""
        await self._stop_push()
        await self._close_session()
        self.transport = None
        await self._pre_connect()
        transport_type = self.config.get("transport") or self.DRIVER_INFO.get(
            "transport", "tcp")
        await self._create_transport(transport_type)
        try:
            await self._post_connect()
            self._connected = True
            self.set_state("connected", True)
            await self.events.emit(f"device.connected.{self.device_id}")
        except Exception:
            self.transport = None
            await self._close_session()
            self._connected = False
            raise
        await self._start_push()
        try:
            await self._initial_sync()
        except Exception:
            await self._stop_push()
            self.transport = None
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
        self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


def _install_stubs() -> None:
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
    # websockets is not a community-CI dependency; the protocol tests never open
    # a socket and the lifecycle test swaps in a fake connect().
    ws_stub = ModuleType("websockets")
    ws_stub.connect = None
    sys.modules["websockets"] = ws_stub


def _load_driver():
    _install_stubs()
    spec = importlib.util.spec_from_file_location("soundcorehero", DRIVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["soundcorehero"] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_driver()
SoundCoreHeroDriver = _MOD.SoundCoreHeroDriver
_SNAPSHOT_KEYS = _MOD._SNAPSHOT_KEYS


def _driver(**config):
    cfg = {"host": "10.0.0.9", "port": 443, "email": "op@site", "password": "pw",
           "verify_ssl": False}
    cfg.update(config)
    return SoundCoreHeroDriver("sch1", cfg, _FakeState(), _FakeEvents())


def _prime(d):
    """Initialize the WebSocket resource caches the state pipeline needs.

    Mirrors what the driver sets up at the head of its connection; works whether
    those attributes are seeded in __init__ or (pre-migration) inside connect().
    """
    d._session_id = None
    d._receiver_id = "test-receiver"
    d._ws_task = None
    d._ws_ready = asyncio.Event()
    d._client = None
    d._order_to_uuid = {c: {} for c in _SNAPSHOT_KEYS.values()}
    d._obj_cache = {c: {} for c in _SNAPSHOT_KEYS.values()}
    d._synthetic_ids = {c: {} for c in _SNAPSHOT_KEYS.values()}
    d._synthetic_next = {c: 0 for c in _SNAPSHOT_KEYS.values()}
    d._dso_cache = {}
    return d


# Sample wire objects -------------------------------------------------------

def _zone_obj(order, name, volume, muted=False):
    return {
        "name": name, "order": order, "signalActive": True,
        "mixers": {"master": {"volume": volume, "muted": muted, "balance": 0}},
        "connections": {"speakers": ["spk-a", "spk-b"]},
    }


def _player_obj(order, name, state="stopped", volume=50):
    return {
        "name": name, "order": order, "state": state, "volume": volume,
        "muted": False, "sourceType": "playlist",
        "connections": {"zones": ["z-1"]},
    }


# ── WebSocket resource pipeline ─────────────────────────────────────────────

def test_snapshot_registers_children_and_state():
    d = _prime(_driver())
    msg = {
        "zones": {"z-1": _zone_obj(0, "Lobby", 42, muted=True)},
        "players": {"p-1": _player_obj(0, "BGM", "playing", 30)},
    }
    d._handle_ws_message(msg)

    # First collection unblocks connect().
    assert d._ws_ready.is_set() is True
    assert d.is_child_registered("zone", 0) is True
    zst = d.get_child_state("zone", 0)
    assert zst["name"] == "Lobby"
    assert zst["master_volume"] == 42
    assert zst["master_muted"] is True
    assert zst["uuid"] == "z-1"
    assert zst["speakers"] == "spk-a, spk-b"
    pst = d.get_child_state("player", 0)
    assert pst["state"] == "playing"
    assert pst["volume"] == 30
    assert pst["zones"] == "z-1"


def test_incremental_patch_updates_state():
    d = _prime(_driver())
    d._handle_ws_message({"zones": {"z-1": _zone_obj(0, "Lobby", 42)}})
    # A JSON-Patch delta on the cached object (nested path).
    d._handle_ws_message({"zonesUpdate": {"z-1": [
        {"op": "replace", "path": "/mixers/master/volume", "value": 55},
        {"op": "replace", "path": "/name", "value": "Atrium"},
    ]}})
    zst = d.get_child_state("zone", 0)
    assert zst["master_volume"] == 55
    assert zst["name"] == "Atrium"


def test_incremental_before_snapshot_is_skipped():
    d = _prime(_driver())
    # No snapshot yet for this uuid -> the delta cannot be reconstructed.
    d._handle_ws_message({"zonesUpdate": {"z-9": [
        {"op": "replace", "path": "/name", "value": "Ghost"}]}})
    assert d.is_child_registered("zone", 0) is False
    assert d.list_children("zone") == []


def test_snapshot_reconciles_removed_children():
    d = _prime(_driver())
    d._handle_ws_message({"zones": {
        "z-1": _zone_obj(0, "Lobby", 42),
        "z-2": _zone_obj(1, "Hall", 10),
    }})
    assert sorted(d.list_children("zone")) == [0, 1]
    # A later full snapshot without z-2 drops it.
    d._handle_ws_message({"zones": {"z-1": _zone_obj(0, "Lobby", 42)}})
    assert d.list_children("zone") == [0]
    assert "z-2" not in d._obj_cache["zone"]


def test_goodbye_marks_disconnected():
    d = _prime(_driver())
    d._connected = True
    d._handle_ws_message({"message": "goodbye"})
    assert d._connected is False
    assert d.get_state("connected") is False


# ── Device-level singletons ─────────────────────────────────────────────────

def test_dso_snapshot_then_patch():
    d = _prime(_driver())
    d._handle_ws_message({"dso": {
        "enabled": True, "volume": 70, "muted": False,
        "channelsFormat": {"stereo": {"left": 1, "right": 2}},
        "signalActive": True, "presetEqId": "eq-a", "presetEqBypassed": False,
    }})
    assert d.get_state("dso_enabled") is True
    assert d.get_state("dso_volume") == 70
    assert d.get_state("dso_channels_format") == "stereo:1/2"

    d._handle_ws_message({"dsoUpdate": [
        {"op": "replace", "path": "/volume", "value": 33}]})
    assert d.get_state("dso_volume") == 33


def test_system_signals():
    d = _prime(_driver())
    d._handle_ws_message({"internetStatus": True, "cpuUsage": 12.5,
                          "channelsLimit": {"usedInputs": 4, "usedOutputs": 8,
                                            "maxAllowed": 32}})
    assert d.get_state("internet") is True
    assert d.get_state("cpu_usage") == 12.5
    assert d.get_state("channels_used_inputs") == 4
    assert d.get_state("channels_max") == 32


# ── Orderless objects (controllers) ─────────────────────────────────────────

def test_controller_gets_synthetic_id_and_mirrors_zone_volume():
    d = _prime(_driver())
    # A zone with a live master volume.
    d._handle_ws_message({"zones": {"z-1": _zone_obj(0, "Lobby", 42)}})
    # A controller has no `order`; it binds to the zone and mirrors its volume.
    d._handle_ws_message({"controllers": {"c-1": {
        "name": "Wall CC", "connections": {"zone": "z-1"}}}})
    # Synthetic id 0 for the first orderless controller.
    assert d.is_child_registered("controller", 0) is True
    assert d.get_child_state("controller", 0)["zone_volume"] == 42

    # When the bound zone's volume changes, the controller re-mirrors it.
    d._handle_ws_message({"zonesUpdate": {"z-1": [
        {"op": "replace", "path": "/mixers/master/volume", "value": 88}]}})
    assert d.get_child_state("controller", 0)["zone_volume"] == 88


def test_synthetic_ids_are_stable():
    d = _prime(_driver())
    assert d._synthetic_id("controller", "c-1") == 0
    assert d._synthetic_id("controller", "c-2") == 1
    assert d._synthetic_id("controller", "c-1") == 0  # stable across calls


# ── Command mapping + id resolution ─────────────────────────────────────────

class _RecordPost:
    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, endpoint, params, method="POST"):
        self.calls.append((endpoint, params, method))
        return {"ok": True}


def _with_recorded_post(d):
    rec = _RecordPost()
    d._post = rec
    return rec


def test_player_and_zone_commands_map_to_rest_bodies():
    d = _prime(_driver())
    d._handle_ws_message({
        "zones": {"z-1": _zone_obj(0, "Lobby", 42)},
        "players": {"p-1": _player_obj(0, "BGM")},
    })
    rec = _with_recorded_post(d)

    asyncio.run(d.send_command("player_set_volume", {"player_id": 0, "volume": 25}))
    asyncio.run(d.send_command("player_play", {"player_id": 0}))
    asyncio.run(d.send_command("zone_set_master_volume", {"zone_id": 0, "volume": 60}))

    assert rec.calls[0] == ("players-modify", {"playerIds": ["p-1"], "volume": 25.0}, "POST")
    assert rec.calls[1] == ("players-action", {"playerIds": ["p-1"], "action": "play"}, "POST")
    assert rec.calls[2] == (
        "zones-modify",
        {"zoneIds": ["z-1"], "mixers": {"master": {"volume": 60.0}}},
        "POST",
    )


def test_unknown_child_id_raises():
    d = _prime(_driver())
    _with_recorded_post(d)
    try:
        asyncio.run(d.send_command("player_set_volume", {"player_id": 7, "volume": 1}))
    except ValueError as exc:
        assert "Unknown player" in str(exc)
    else:
        raise AssertionError("expected a ValueError for an unresolved child id")


def test_resolve_ids_all_and_csv():
    d = _prime(_driver())
    d._handle_ws_message({"zones": {
        "z-1": _zone_obj(0, "A", 1), "z-2": _zone_obj(1, "B", 2)}})
    assert d._resolve_ids("zone", "all") == "all"
    assert d._resolve_ids("zone", "0,1") == ["z-1", "z-2"]
    assert d._resolve_ids("zone", None) == []


# ── Connection lifecycle (hook wiring) ──────────────────────────────────────
#
# The driver has no connect() of its own — the platform runs the hooks. This
# drives the fake platform connect()/disconnect() with httpx wired to a
# MockTransport (login/logout) and a fake control WebSocket (auth prompt ->
# snapshot), proving login -> WS snapshot gate -> connected -> polling and a
# clean, logout-first teardown.

class _FakeWsConn:
    """Async context manager + async iterator standing in for a websockets
    client connection: yields the given frames, then stays 'open'."""

    def __init__(self, frames):
        self._frames = frames
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for f in self._frames:
            yield f
        await asyncio.Event().wait()  # keep the socket alive until cancelled

    async def send(self, data):
        self.sent.append(data)


def test_connect_lifecycle_logs_in_and_gates_on_snapshot(monkeypatch):
    d = _driver(poll_interval=15)

    calls: list[tuple[str, str]] = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/usersmanager-login":
            return httpx.Response(200, json={"sessionId": "sess-1"})
        if request.url.path == "/api/usersmanager-logout":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    real_client = _MOD.httpx.AsyncClient

    def client_factory(*a, **k):
        k.pop("verify", None)  # MockTransport ignores TLS settings
        k["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **k)

    monkeypatch.setattr(_MOD.httpx, "AsyncClient", client_factory)

    prompt = json.dumps({"status": "unauthorized"})
    snapshot = json.dumps({"zones": {"z-1": _zone_obj(0, "Lobby", 42)}})
    ws_conn = _FakeWsConn([prompt, snapshot])
    monkeypatch.setattr(_MOD.websockets, "connect", lambda *a, **k: ws_conn)

    async def go():
        await d.connect()
        assert d._session_id == "sess-1"
        assert d.get_state("connected") is True
        assert d.connected is True
        assert d._link_alive() is True
        # The WS auth frame was sent in reply to the unauthorized prompt.
        assert json.loads(ws_conn.sent[0]) == {
            "sessionId": "sess-1", "receiverId": d._receiver_id}
        # The snapshot flowed through the real resource pipeline.
        assert d.is_child_registered("zone", 0) is True
        assert d.get_child_state("zone", 0)["master_volume"] == 42
        # poll_interval from config -> the platform started polling.
        assert d.polling_starts == 1

        await d.disconnect()
        assert d.get_state("connected") is False
        assert d._client is None
        assert d._link_alive() is False
        # Logged out (while the client was still open) before closing it.
        assert ("POST", "/api/usersmanager-logout") in calls
        # Cached resource graph cleared for a clean next connect.
        assert d._obj_cache["zone"] == {}

    asyncio.run(go())


def test_connect_login_failure_aborts_and_closes_client(monkeypatch):
    d = _driver()

    def handler(request):
        # Reject the login: the session never opens.
        return httpx.Response(401, json={"error": "bad credentials"})

    real_client = _MOD.httpx.AsyncClient

    def client_factory(*a, **k):
        k.pop("verify", None)
        k["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **k)

    monkeypatch.setattr(_MOD.httpx, "AsyncClient", client_factory)
    # WS should never be reached, but stub it so a bug surfaces as a hang-free
    # failure rather than an attribute error.
    monkeypatch.setattr(_MOD.websockets, "connect",
                        lambda *a, **k: _FakeWsConn([]))

    async def go():
        try:
            await d.connect()
        except ConnectionError:
            pass
        else:
            raise AssertionError("connect should have raised on login rejection")
        assert d.get_state("connected") in (None, False)
        assert d._client is None  # torn down by _close_session
        assert d._ws_task is None

    asyncio.run(go())
