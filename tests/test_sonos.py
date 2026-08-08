"""Driver + simulator tests for sonos (Sonos speakers, UPnP/SOAP + GENA push).

Correctness is proven with a **dual-proof round trip**: the real driver is wired
to the real simulator over an in-memory httpx transport (SOAP control) plus an
in-memory push registry (GENA eventing), so the simulator renders the UPnP
payloads a real speaker sends and the driver parses them — both sides asserted.

Covers the v2.0.0 push retrofit:
  - GENA lifecycle: SUBSCRIBE (CALLBACK + NT) -> SID, renewal with SID only,
    412 on an unknown SID, UNSUBSCRIBE on disconnect;
  - the initial event (SEQ 0) populating state before any poll;
  - LastChange parsing: Sonos's trailing-slash namespaces, per-channel
    Volume/Mute (only Master is the room level), double-escaped DIDL metadata;
  - stopped playback clearing the now-playing fields on both push and poll;
  - device settings (bass / treble / loudness / play mode) round-tripping;
  - **a rejected write raises and does NOT fabricate state** — the real Amp
    answers SetPlayMode with UPnP error 712 when a radio stream is playing, and
    the old driver reported success anyway (found on hardware, 2026-07-12);
  - track position coming only from polling (Sonos does not event RelTime).

The driver is loaded with the ``openavc.*`` imports stubbed so
the community CI stays self-contained (conftest.py rolls the stubs back).
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
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

DRIVER_PATH = Path(__file__).parent.parent / "audio" / "sonos.py"
SIM_PATH = Path(__file__).parent.parent / "audio" / "sonos_sim.py"


# ── Platform stubs ──────────────────────────────────────────────────────────


class _FakeBaseDriver:
    """Minimal BaseDriver: device-scoped state, the push handle contract, and
    the hook-driven connect lifecycle the platform runs (the driver has no
    connect() of its own anymore — it participates through the hooks)."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events):
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._push_subscription = None
        self._push_callback_url = ""
        self._polling = False

    def set_state(self, key, value):
        self.state.set(f"device.{self.device_id}.{key}", value)

    def get_state(self, key, default=None):
        return self.state.get(f"device.{self.device_id}.{key}", default)

    async def start_polling(self, interval):
        self._polling = True

    async def stop_polling(self):
        self._polling = False

    async def _stop_push(self):
        sub = self._push_subscription
        self._push_subscription = None
        if sub is None:
            return
        for handle in sub if isinstance(sub, list) else [sub]:
            await handle.close()

    # Hook defaults (overridden by the driver under test where it needs to).
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

    async def connect(self):
        # Mirrors BaseDriver.connect()'s stages for a driver-owned session:
        # clean slate, establish, handshake, declare, push, initial sync,
        # polling.
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


# In-memory stand-in for openavc.transport.http_listener: records each
# subscription's callback by label so the simulator's NOTIFY can be routed
# straight back into the driver, no sockets involved.
class _PushRegistry:
    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}

    def reset(self) -> None:
        self.callbacks.clear()


_REGISTRY = _PushRegistry()


class _FakeSubscription:
    def __init__(self, device_id: str, label: str) -> None:
        self.device_id = device_id
        self.label = label

    @property
    def path(self) -> str:
        return f"/api/push/{self.device_id}/{self.label}"

    async def close(self) -> None:
        _REGISTRY.callbacks.pop(self.label, None)


class _FakePushRequest:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self.body = body
        self.headers = headers
        self.method = "NOTIFY"
        self.source_ip = "10.0.0.5"


async def _fake_subscribe(device_id, source_ip, callback, name, label=""):
    _REGISTRY.callbacks[label] = callback
    return _FakeSubscription(device_id, label)


def _fake_callback_url(device_host, path):
    return f"http://10.0.0.1:8080{path}"


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc"] = server
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"openavc.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"openavc.{sub}"] = m

    base = ModuleType("openavc.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["openavc.drivers.base"] = base

    hl = ModuleType("openavc.transport.http_listener")
    hl.subscribe = _fake_subscribe
    hl.callback_url = _fake_callback_url
    hl.HTTPPushRequest = _FakePushRequest
    sys.modules["openavc.transport.http_listener"] = hl
    sys.modules["openavc.transport"].http_listener = hl  # type: ignore[attr-defined]

    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger

    sim_pkg = ModuleType("openavc.simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc.simulator"] = sim_pkg
    sim_http = ModuleType("openavc.simulator.http_simulator")
    sim_http.HTTPSimulator = _FakeHTTPSimulator
    sys.modules["openavc.simulator.http_simulator"] = sim_http

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeHTTPSimulator:
    """Stand-in for simulator.HTTPSimulator.

    ``post_http_callback`` delivers the simulated speaker's NOTIFY straight to
    the driver callback the push registry recorded for that URL's label — the
    same hop the platform's /api/push/ route makes, minus the socket.
    """

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id: str, config: dict | None = None):
        self.device_id = device_id
        self.config = config or {}
        self._state: dict = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self.name = f"{device_id}-sim"
        self.protocol_log: list[tuple[str, str]] = []

    @property
    def state(self) -> dict:
        # A COPY, mirroring the real BaseSimulator (writes must go through
        # set_state or they are silently dropped on the real platform).
        return dict(self._state)

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value) -> None:
        self._state[key] = value

    def log_protocol(self, direction: str, text) -> None:
        self.protocol_log.append((direction, str(text)))

    async def post_http_callback(self, url, body, headers=None, method="POST"):
        label = url.rstrip("/").rsplit("/", 1)[-1]
        callback = _REGISTRY.callbacks.get(label)
        if callback is None:
            return None
        data = body.encode("utf-8") if isinstance(body, str) else body
        await callback(_FakePushRequest(data, {k.lower(): v for k, v in (headers or {}).items()}))
        return 200


DRV = _load("sonos_under_test", DRIVER_PATH)
SIM = _load("sonos_sim_under_test", SIM_PATH)


# ── Harness: real driver <-> real simulator over an in-memory transport ─────


async def _make_pair(driver_overrides=None):
    _REGISTRY.reset()
    sim = SIM.SonosSimulator("sim1", {})

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        headers = {k: v for k, v in request.headers.items()}
        result = sim.handle_request(
            request.method, request.url.path, headers, body
        )
        if len(result) == 3:
            status, text, resp_headers = result
        else:
            status, text = result
            resp_headers = {}
        return httpx.Response(status, text=str(text), headers=resp_headers)

    cfg = {"host": "10.0.0.5", "port": 1400, "poll_interval": 0}
    cfg.update(driver_overrides or {})
    driver = DRV.SonosDriver("spk1", cfg, _FakeState(), _FakeEvents())
    return driver, sim, handler


async def _connected_pair(driver_overrides=None):
    driver, sim, handler = await _make_pair(driver_overrides)
    # connect() builds the client in _create_transport(); route it at the
    # simulator by giving every constructed client the MockTransport.
    original = httpx.AsyncClient

    def _mocked(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = _mocked  # type: ignore[assignment]
    try:
        await driver.connect()
    finally:
        httpx.AsyncClient = original  # type: ignore[assignment]
    await asyncio.sleep(0)  # let the simulator's initial events land
    await asyncio.sleep(0)
    return driver, sim


# ── Metadata / shape ───────────────────────────────────────────────────────


def test_driver_declares_http_listener_push():
    assert DRV.SonosDriver.DRIVER_INFO["push"] == {"type": "http_listener"}
    assert DRV.SonosDriver.DRIVER_INFO["min_platform_version"] == "0.24.0"


def test_device_settings_ranges_match_the_speaker_scpd():
    """Bass/Treble are i2 with allowedValueRange -10..10 in the speaker's own
    RenderingControl SCPD — the bounds are runtime-enforced, so a wrong one
    would block valid writes."""
    settings = DRV.SonosDriver.DRIVER_INFO["device_settings"]
    for key in ("bass", "treble"):
        assert settings[key]["min"] == -10
        assert settings[key]["max"] == 10
        assert settings[key]["state_key"] == key
    assert settings["loudness"]["type"] == "boolean"
    assert settings["play_mode"]["values"] == DRV._PLAY_MODES


# ── GENA lifecycle ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribes_to_both_services_and_captures_sids():
    driver, sim = await _connected_pair()
    try:
        assert set(driver._gena) == {"avt", "rc"}
        for label in ("avt", "rc"):
            assert driver._gena[label]["sid"].startswith("uuid:")
            assert driver._gena[label]["timeout"] == 1800
            assert driver._gena[label]["callback_url"].endswith(
                f"/api/push/spk1/{label}"
            )
        assert len(sim._subscriptions) == 2
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_initial_event_populates_state_before_any_poll():
    """Accepting a subscription delivers every evented variable (SEQ 0), so the
    driver is in sync without waiting a poll cycle."""
    driver, _sim = await _connected_pair()
    try:
        assert driver.get_state("volume") == 25
        assert driver.get_state("mute") is False
        assert driver.get_state("bass") == 0
        assert driver.get_state("treble") == 0
        assert driver.get_state("loudness") is True
        assert driver.get_state("play_mode") == "NORMAL"
        assert driver.get_state("transport_state") == "stopped"
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_renewal_and_unknown_sid():
    driver, _sim = await _connected_pair()
    try:
        gena = driver._gena["avt"]
        ok = await driver._gena_request(
            "SUBSCRIBE", gena["event_path"],
            {"SID": gena["sid"], "TIMEOUT": "Second-1800"},
        )
        assert ok.status_code == 200
        assert ok.headers["SID"] == gena["sid"]

        stale = await driver._gena_request(
            "SUBSCRIBE", gena["event_path"],
            {"SID": "uuid:gone", "TIMEOUT": "Second-1800"},
        )
        assert stale.status_code == 412
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_subscribe_with_sid_and_callback_is_rejected():
    """UPnP DA 2.0 §4.1.3: renewal and subscription use a disjoint header set."""
    driver, _sim = await _connected_pair()
    try:
        gena = driver._gena["avt"]
        bad = await driver._gena_request(
            "SUBSCRIBE", gena["event_path"],
            {"SID": gena["sid"], "CALLBACK": "<http://x/y>", "NT": "upnp:event"},
        )
        assert bad.status_code == 400
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_disconnect_unsubscribes_and_drops_callbacks():
    driver, sim = await _connected_pair()
    await driver.disconnect()
    assert sim._subscriptions == {}
    assert driver._gena == {}
    assert driver._push_subscription is None
    assert driver._renew_task is None
    assert _REGISTRY.callbacks == {}


# ── Push: LastChange parsing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_updates_every_evented_variable():
    driver, sim = await _connected_pair()
    try:
        for sim_key, sim_value, want in [
            ("volume", 42, 42),
            ("mute", True, True),
            ("bass", -6, -6),
            ("treble", 5, 5),
            ("loudness", False, False),
            ("play_mode", "SHUFFLE", "SHUFFLE"),
            ("transport_state", "playing", "playing"),
        ]:
            sim.set_state(sim_key, sim_value)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert driver.get_state(sim_key) == want, sim_key
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_push_ignores_non_master_channels():
    """A real speaker events Volume/Mute for Master AND the LF/RF driver trims;
    only Master is the room level."""
    driver, sim = await _connected_pair()
    try:
        sim.set_state("volume", 42)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # The simulator emitted LF=100 / RF=100 alongside Master=42.
        assert driver.get_state("volume") == 42
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_push_parses_double_escaped_track_metadata():
    driver, sim = await _connected_pair()
    try:
        sim.set_state("transport_state", "playing")
        sim.set_state("track_title", "Live Track")
        sim.set_state("track_artist", "The Band")
        sim.set_state("track_album", "The Album")
        for _ in range(4):
            await asyncio.sleep(0)
        assert driver.get_state("track_title") == "Live Track"
        assert driver.get_state("track_artist") == "The Band"
        assert driver.get_state("track_album") == "The Album"
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_push_stop_clears_now_playing():
    """Both the push and poll paths clear the track fields when playback stops,
    so they can't fight over them."""
    driver, sim = await _connected_pair()
    try:
        sim.set_state("transport_state", "playing")
        sim.set_state("track_title", "Live Track")
        for _ in range(4):
            await asyncio.sleep(0)
        assert driver.get_state("track_title") == "Live Track"

        sim.set_state("transport_state", "stopped")
        for _ in range(4):
            await asyncio.sleep(0)
        assert driver.get_state("transport_state") == "stopped"
        assert driver.get_state("track_title") is None
        assert driver.get_state("track_artist") is None
    finally:
        await driver.disconnect()


def test_last_change_namespaces_are_ignored():
    """Sonos's LastChange namespaces carry a trailing slash the UPnP examples
    don't show — tag matching must be namespace-insensitive."""
    driver = DRV.SonosDriver("spk1", {"host": "10.0.0.5"}, _FakeState(), _FakeEvents())
    driver._apply_last_change(
        '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/">'
        '<InstanceID val="0"><Volume channel="Master" val="31"/></InstanceID>'
        "</Event>"
    )
    assert driver.get_state("volume") == 31


# ── Commands + device settings ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_commands_reach_the_speaker_and_state_follows():
    driver, sim = await _connected_pair()
    try:
        await driver.send_command("set_volume", {"level": 77})
        assert sim.state["volume"] == 77
        assert driver.get_state("volume") == 77

        await driver.send_command("mute_toggle")
        assert sim.state["mute"] is True
        await driver.send_command("mute_toggle")
        assert sim.state["mute"] is False

        await driver.send_command("play")
        assert sim.state["transport_state"] == "playing"
        await driver.send_command("stop")
        assert sim.state["transport_state"] == "stopped"
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_device_settings_round_trip():
    driver, sim = await _connected_pair()
    try:
        for key, value in [
            ("bass", 8),
            ("treble", -4),
            ("loudness", False),
            ("play_mode", "REPEAT_ALL"),
        ]:
            await driver.set_device_setting(key, value)
            for _ in range(4):
                await asyncio.sleep(0)
            assert sim.state[key] == value, key
            assert driver.get_state(key) == value, key
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_rejected_write_raises_and_does_not_fabricate_state():
    """Regression (found on a real Sonos Amp, 2026-07-12): the speaker answers
    SetPlayMode with HTTP 500 / UPnP error 712 while a radio stream is playing.
    The driver used to write the new value into state anyway, so the panel
    showed a mode the speaker had refused."""
    driver, sim = await _connected_pair()
    try:
        before = driver.get_state("play_mode")

        # Make the simulated speaker reject the write, exactly as hardware does.
        def reject(action, body):
            if action == "SetPlayMode":
                return 500, (
                    '<?xml version="1.0"?><s:Envelope><s:Body><s:Fault>'
                    "<detail><UPnPError><errorCode>712</errorCode>"
                    "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
                )
            return original(action, body)

        original = sim._handle_av_transport
        sim._handle_av_transport = reject

        with pytest.raises(RuntimeError, match="712"):
            await driver.set_device_setting("play_mode", "REPEAT_ALL")

        assert driver.get_state("play_mode") == before
        assert sim.state["play_mode"] == before
    finally:
        await driver.disconnect()


# ── Polling ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_reads_track_position_which_is_never_pushed():
    """RelTime is not an evented variable (confirmed against a real speaker's
    event payload) — polling is its only source."""
    driver, sim = await _connected_pair()
    try:
        sim.set_state("transport_state", "playing")
        sim.set_state("track_position", "0:01:23")
        for _ in range(4):
            await asyncio.sleep(0)
        assert driver.get_state("track_position") is None  # push never sends it

        await driver.poll()
        assert driver.get_state("track_position") == "0:01:23"
    finally:
        await driver.disconnect()


@pytest.mark.asyncio
async def test_poll_resyncs_state():
    driver, sim = await _connected_pair()
    try:
        # Simulate a missed event: change the speaker without notifying.
        sim._state["volume"] = 13
        await driver.poll()
        assert driver.get_state("volume") == 13
    finally:
        await driver.disconnect()
