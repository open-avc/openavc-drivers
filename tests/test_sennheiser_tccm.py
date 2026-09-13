"""Driver + simulator tests for sennheiser_tccm (TeamConnect Ceiling Medium, SSCv2).

Dual-proof round trip: the real driver's httpx client is wired to the real
simulator's handle_request through httpx.MockTransport, and the subscription
stream is served as a real streaming response fed by the simulator's session
queue, so the driver's own event loop (open, read the session from the
header, PUT the resource list, parse the events) runs in-test. Both sides are
asserted.

Covers:
  - connect: identity, the product guard, the authenticated read, the full
    first poll, and the subscription list the driver arms (feeds off);
  - the two feeds from config and from their runtime commands, and the
    rate limit on a feed's notifications;
  - push: a change on the simulator lands in state with polling off, keyed
    with or without the /api prefix, and a zone collection fans out by id;
  - every command against the simulator, every device setting round-tripped
    through the simulator and back over the stream;
  - the microphone's refusals: a value off its step refused before the wire,
    a manual reference gain while auto adjust is on, a zone too narrow;
  - a resource this firmware lacks: skipped on poll and dropped from the
    subscription list, the rest still armed;
  - the stream reopening after the device closes the session, and re-arming;
  - faults: no password (never sent), a rejected password, third-party
    access off, a wrong product, an unreachable host, a mid-session 401,
    and poll propagating transport errors;
  - the discovery probe matching the simulator's own identity reply;
  - the simulator's subscription and validation semantics on their own.

Loads the driver and simulator with the ``openavc.*`` imports stubbed so the
community CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

import httpx
import pytest
from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    ConnectionFaultError,
    StubBaseDriver,
    StubEvents,
    StubState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "sennheiser_tccm.py"
SIM_PATH = REPO_ROOT / "audio" / "sennheiser_tccm_sim.py"

_SUBS = "/api/ssc/state/subscriptions"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect lifecycle for a driver that owns its
    session; state and the watchdog come from the shared stubs."""

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0
        self._push_subscription = None

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
        await self._stop_push()
        await self._close_session()
        await self._pre_connect()
        await self._create_transport(self.DRIVER_INFO.get("transport", "http"))
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
            raise

    async def disconnect(self):
        await self._stop_push()
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


install_stubs(base_driver=_FakeBaseDriver)
DRV = load_module("sennheiser_tccm_under_test", DRIVER_PATH)
SIM = load_module("sennheiser_tccm_sim_under_test", SIM_PATH)

INFO = DRV.SennheiserTccmDriver.DRIVER_INFO


# ── Harness ──────────────────────────────────────────────────────────────────


class _Link:
    def __init__(self, sim):
        self.sim = sim
        self.reachable = True
        self.requests: list[tuple[str, str]] = []


async def _sse_body(queue: asyncio.Queue):
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        yield chunk.encode("utf-8")


def _make_handler(link: _Link):
    def handler(request: httpx.Request) -> httpx.Response:
        if not link.reachable:
            raise httpx.ConnectError("Connection refused")
        path = request.url.path
        headers = dict(request.headers)
        link.requests.append((request.method, path))
        if (
            path == _SUBS
            and request.method == "GET"
            and "text/event-stream" in headers.get("accept", "")
        ):
            status, session_uuid, queue = link.sim.open_session(headers)
            if status != 200:
                return httpx.Response(status, json={"error": "unauthorized"})
            return httpx.Response(
                200,
                content=_sse_body(queue),
                headers={
                    "content-type": "text/event-stream",
                    "content-location": f"{_SUBS}/{session_uuid}",
                },
            )
        body = request.content.decode("utf-8") if request.content else ""
        result = link.sim.handle_request(request.method, path, headers, body)
        status, resp_body = result[0], result[1]
        if isinstance(resp_body, dict):
            return httpx.Response(status, json=resp_body)
        return httpx.Response(status, text=str(resp_body))

    return handler


def _make(sim_config=None, driver_config=None):
    sim = SIM.SennheiserTccmSimulator("tccm-sim", sim_config or {})
    link = _Link(sim)
    cfg = {
        "host": "10.0.0.5", "port": 443, "password": "secret",
        "verify_ssl": False, "poll_interval": 0, "timeout": 2.0,
    }
    cfg.update(driver_config or {})
    driver = DRV.SennheiserTccmDriver("mic1", cfg, StubState(), StubEvents())
    return driver, sim, link


async def _settle(rounds: int = 8) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0.01)


@pytest.fixture
def mocked_client(monkeypatch):
    """Route every httpx.AsyncClient the driver builds through the link's
    handler. Returns the function that binds a link."""
    original = httpx.AsyncClient
    holder: dict = {}

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_make_handler(holder["link"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    def bind(link):
        holder["link"] = link

    return bind


async def _connect(driver, link, bind):
    bind(link)
    await driver.connect()
    await _settle()


def _session_paths(sim) -> set[str]:
    assert len(sim.sessions) == 1, sim.sessions
    return sim.session_paths(sim.sessions[0])


def _expected_paths(talker=False, meters=False, hidden=()) -> set[str]:
    wanted = set()
    for res in DRV._RESOURCES:
        if not res.subscribe or res.path in hidden:
            continue
        if res.fast:
            if res.path.endswith("/beam/direction"):
                if not talker:
                    continue
            elif not meters:
                continue
        wanted.add(res.path)
    return wanted


# ── Declarations ─────────────────────────────────────────────────────────────


def test_metadata_contract():
    for key in ("id", "name", "manufacturer", "category", "version", "author",
                "transport", "description", "source_url"):
        assert INFO.get(key), key
    assert INFO["id"] == "sennheiser_tccm"
    assert INFO["transport"] == "http"
    assert INFO["default_config"]["ssl"] is True
    assert INFO["default_config"]["username"] == "api"
    assert INFO["config_schema"]["password"]["secret"] is True
    # Every setting reads back from a declared state variable and maps to a
    # resource field the driver can write.
    for key, sdef in INFO["device_settings"].items():
        assert sdef.get("state_key", key) in INFO["state_variables"], key
        assert key in DRV._RESOURCE_BY_KEY, key
    for action in INFO["quick_actions"]:
        assert action in INFO["commands"]
    for action in INFO["actions"]:
        assert action["id"] in INFO["commands"]


def test_every_resource_field_is_a_declared_state_variable():
    for res in DRV._RESOURCES:
        for key in res.fields.values():
            assert key in INFO["state_variables"], (res.path, key)


def test_driver_and_simulator_agree_on_the_resource_tree():
    drv_paths = set(DRV._RESOURCE_BY_PATH)
    sim_paths = set(SIM._RESOURCES)
    assert drv_paths == sim_paths
    for path in drv_paths:
        drv_fields = set(DRV._RESOURCE_BY_PATH[path].fields.items())
        sim_fields = {(f, k) for f, k, _s in SIM._RESOURCES[path][1]}
        # The device state's warnings list is rendered by the simulator, not
        # mapped field-for-field.
        drv_fields.discard(("warnings", "warnings"))
        assert drv_fields == sim_fields, path


def test_simulator_seeds_every_device_value():
    seeded = set(SIM.SennheiserTccmSimulator.SIMULATOR_INFO["initial_state"])
    device_keys = {k for r in DRV._RESOURCES for k in r.fields.values()} - {"warnings"}
    assert device_keys <= seeded


# ── Connect ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_populates_state_and_arms_subscription(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    assert driver.get_state("connected") is True
    assert driver.get_state("product") == "TCCM"
    assert driver.get_state("serial") == "1023456789"
    assert driver.get_state("firmware_version") == "1.9.2"
    assert driver.get_state("mute") is False
    assert driver.get_state("device_state") == "Normal"
    assert driver.get_state("warnings") == ""
    assert driver.get_state("led_mic_mute_color") == "Red"
    assert driver.get_state("eq_1k_db") == 0
    assert driver.get_state("exclusion_zone_1_enabled") is True
    assert driver.get_state("exclusion_zone_3_azimuth_min") == 110
    assert driver.get_state("priority_zone_weight") == 1.5
    assert driver.get_state("room_in_use_release_s") == 300
    assert driver.get_state("poe_sufficient_power") is True
    assert driver.get_state("talker_position_feed") is False
    assert driver.get_state("meters_feed") is False
    # The stream is open and armed with every non-feed resource.
    assert _session_paths(sim) == _expected_paths()
    # The resource list went to the session named by the header.
    puts = [p for m, p in link.requests if m == "PUT"]
    assert puts and puts[0].startswith(_SUBS + "/")
    await driver.disconnect()
    assert driver.get_state("connected") is False
    assert driver._client is None


@pytest.mark.asyncio
async def test_feeds_from_config(mocked_client):
    driver, sim, link = _make(
        driver_config={"enable_talker_position": True, "enable_meters": True}
    )
    await _connect(driver, link, mocked_client)
    assert _session_paths(sim) == _expected_paths(talker=True, meters=True)
    assert driver.get_state("talker_position_feed") is True
    assert driver.get_state("meters_feed") is True
    # The subscription delivered the feeds' current values.
    assert driver.get_state("beam_azimuth") == 180
    assert driver.get_state("mic_peak_db") == -42
    await driver.disconnect()


# ── Push ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_on_device_streams_to_state(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    sim.set_state("mute", True)
    sim.set_state("room_in_use", True)
    sim.set_state("led_brightness", 2)
    sim.set_state("exclusion_zone_4_azimuth_max", 260)
    await _settle()
    assert driver.get_state("mute") is True
    assert driver.get_state("room_in_use") is True
    assert driver.get_state("led_brightness") == 2
    assert driver.get_state("exclusion_zone_4_azimuth_max") == 260
    await driver.disconnect()


@pytest.mark.asyncio
async def test_identify_shows_on_device_state(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    await driver.send_command("identify_on")
    await _settle()
    assert sim.get_state("identify_active") is True
    assert driver.get_state("identify_active") is True
    assert driver.get_state("device_state") == "Identifying"
    await driver.send_command("identify_off")
    await _settle()
    assert driver.get_state("device_state") == "Normal"
    await driver.disconnect()


@pytest.mark.asyncio
async def test_feed_off_means_no_updates(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    sim.set_state("beam_azimuth", 90)
    await _settle()
    assert driver.get_state("beam_azimuth") is None
    await driver.disconnect()


@pytest.mark.asyncio
async def test_feed_commands_change_the_subscription(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    await driver.send_command("talker_position_on")
    await _settle()
    assert _session_paths(sim) == _expected_paths(talker=True)
    assert driver.get_state("talker_position_feed") is True
    assert driver.get_state("beam_azimuth") == 180   # initial value on add
    sim.set_state("beam_azimuth", 90)
    sim.set_state("beam_freeze_active", True)
    await asyncio.sleep(DRV._FAST_MIN_INTERVAL_S + 0.05)
    assert driver.get_state("beam_azimuth") == 90
    assert driver.get_state("beam_freeze_active") is True

    await driver.send_command("meters_on")
    await _settle()
    assert _session_paths(sim) == _expected_paths(talker=True, meters=True)
    await driver.send_command("talker_position_off")
    await driver.send_command("meters_off")
    await _settle()
    assert _session_paths(sim) == _expected_paths()
    assert driver.get_state("talker_position_feed") is False
    assert driver.get_state("meters_feed") is False
    await driver.disconnect()


@pytest.mark.asyncio
async def test_feed_notifications_are_rate_limited(mocked_client):
    driver, sim, link = _make(driver_config={"enable_meters": True})
    await _connect(driver, link, mocked_client)
    assert driver.get_state("mic_peak_db") == -42
    await asyncio.sleep(DRV._FAST_MIN_INTERVAL_S + 0.05)
    # Three changes inside one window: the first lands at once, the middle
    # one is never written, the LAST lands when the window expires.
    sim.set_state("mic_peak_db", -30)
    await _settle(2)
    sim.set_state("mic_peak_db", -20)
    await _settle(2)
    sim.set_state("mic_peak_db", -10)
    await _settle(2)
    assert driver.get_state("mic_peak_db") == -30
    seen: list = []
    original = driver.set_states

    def spy(updates):
        seen.append(dict(updates))
        original(updates)

    driver.set_states = spy
    await asyncio.sleep(DRV._FAST_MIN_INTERVAL_S + 0.05)
    assert driver.get_state("mic_peak_db") == -10
    assert [u["mic_peak_db"] for u in seen if "mic_peak_db" in u] == [-10]
    await driver.disconnect()


def test_notification_paths_with_and_without_the_api_prefix():
    driver, _sim, _link = _make()
    driver._apply_notification({"/audio/outputs/global/mute": {"enabled": True}})
    assert driver.get_state("mute") is True
    driver._apply_notification({"/api/audio/outputs/global/mute": {"enabled": False}})
    assert driver.get_state("mute") is False
    driver._apply_notification({"/api/device/state": {"state": "Normal", "warnings": ["a", "b"]}})
    assert driver.get_state("warnings") == "a; b"


def test_zone_collection_notification_fans_out_by_id():
    driver, _sim, _link = _make()
    driver._apply_notification({
        "/api/audio/inputs/microphone/exclusionZones": [
            {"id": 1, "enabled": True, "azimuth": {"min": 20, "max": 70},
             "elevation": {"min": 10, "max": 50}},
            {"id": 4, "enabled": False, "azimuth": {"min": 290, "max": 340},
             "elevation": {"min": 10, "max": 50}},
        ]
    })
    assert driver.get_state("exclusion_zone_2_enabled") is True
    assert driver.get_state("exclusion_zone_2_azimuth_max") == 70
    assert driver.get_state("exclusion_zone_5_azimuth_min") == 290


# ── Commands ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mute_commands(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    await driver.send_command("mute_on")
    await _settle()
    assert sim.get_state("mute") is True
    assert driver.get_state("mute") is True
    await driver.send_command("mute_toggle")
    await _settle()
    assert sim.get_state("mute") is False
    assert driver.get_state("mute") is False
    await driver.send_command("mute_toggle")
    await _settle()
    assert driver.get_state("mute") is True
    await driver.send_command("mute_off")
    await _settle()
    assert driver.get_state("mute") is False
    await driver.disconnect()


@pytest.mark.asyncio
async def test_level_and_processing_commands(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    await driver.send_command("set_analog_gain", {"gain": -6})
    await driver.send_command("set_analog_output_source", {"source": "FarendOutput"})
    await driver.send_command("set_farend_gain", {"gain": 18})
    await driver.send_command("set_local_gain", {"gain": 3})
    await driver.send_command("set_denoiser", {"level": "High"})
    await driver.send_command("set_led_brightness", {"brightness": 1})
    await driver.send_command("set_led_colors", {"mic_on": "Blue", "mic_mute": "Orange"})
    await driver.send_command("led_custom_on", {"color": "Pink"})
    await _settle()
    assert sim.get_state("analog_gain_db") == -6
    assert sim.get_state("analog_output_source") == "FarendOutput"
    assert sim.get_state("farend_gain_db") == 18
    assert sim.get_state("local_gain_db") == 3
    assert sim.get_state("denoiser") == "High"
    assert sim.get_state("led_brightness") == 1
    assert sim.get_state("led_mic_on_color") == "Blue"
    assert sim.get_state("led_mic_mute_color") == "Orange"
    assert sim.get_state("led_custom_enabled") is True
    assert sim.get_state("led_custom_color") == "Pink"
    assert driver.get_state("farend_gain_db") == 18
    assert driver.get_state("led_custom_color") == "Pink"
    await driver.send_command("led_custom_off")
    await _settle()
    assert driver.get_state("led_custom_enabled") is False
    # The LED colours command needs at least one colour.
    with pytest.raises(ValueError):
        await driver.send_command("set_led_colors", {})
    await driver.disconnect()


@pytest.mark.asyncio
async def test_eq_band_and_flat(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    await driver.send_command("set_eq_band", {"band": "1k", "gain": 4})
    await _settle()
    await driver.send_command("set_eq_band", {"band": "125", "gain": -3})
    await _settle()
    assert sim.get_state("eq_1k_db") == 4
    assert sim.get_state("eq_125_db") == -3
    assert driver.get_state("eq_1k_db") == 4
    assert driver.get_state("eq_250_db") == 0
    await driver.send_command("eq_flat")
    await _settle()
    assert all(sim.get_state(k) == 0 for k in DRV._EQ_KEYS)
    assert all(driver.get_state(k) == 0 for k in DRV._EQ_KEYS)
    await driver.disconnect()


@pytest.mark.asyncio
async def test_zone_commands(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    await driver.send_command("exclusion_zone_on", {"zone": "3"})
    await driver.send_command("exclusion_zone_off", {"zone": "1"})
    await driver.send_command("priority_zone_on")
    await _settle()
    assert sim.get_state("exclusion_zone_3_enabled") is True
    assert sim.get_state("exclusion_zone_1_enabled") is False
    assert sim.get_state("priority_zone_enabled") is True
    assert driver.get_state("exclusion_zone_3_enabled") is True
    assert driver.get_state("priority_zone_enabled") is True
    await driver.send_command("priority_zone_off")
    await _settle()
    assert driver.get_state("priority_zone_enabled") is False
    with pytest.raises(ValueError):
        await driver.send_command("exclusion_zone_on", {"zone": "9"})
    await driver.disconnect()


@pytest.mark.asyncio
async def test_reference_gain_rules(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    assert driver.get_state("reference_auto_adjust") is True
    # Refused before the wire while auto adjust is on, with the fix named.
    requests_before = len(link.requests)
    with pytest.raises(ValueError, match="Auto Adjust off"):
        await driver.send_command("set_reference_gain", {"gain": -12})
    assert len(link.requests) == requests_before
    assert "Auto Adjust" in driver.get_state("last_error")
    await driver.send_command("reference_auto_adjust_off")
    await _settle()
    await driver.send_command("set_reference_gain", {"gain": -12})
    await _settle()
    assert sim.get_state("reference_gain_db") == -12
    assert driver.get_state("reference_gain_db") == -12
    # The device's own refusal when the driver's view is stale: 409.
    sim.set_state("reference_auto_adjust", True)
    driver.set_state("reference_auto_adjust", False)
    with pytest.raises(ValueError, match="refused the reference input gain"):
        await driver.send_command("set_reference_gain", {"gain": 0})
    assert "current state" in driver.get_state("last_error")
    await driver.disconnect()


@pytest.mark.asyncio
async def test_one_shot_reads(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    sim.set_state("beam_azimuth", 270)
    sim.set_state("mic_peak_db", -12)
    sim.set_state("room_activity_db", 30)
    await _settle()
    assert driver.get_state("beam_azimuth") is None   # feed off
    await driver.send_command("get_beam_position")
    await driver.send_command("get_levels")
    assert driver.get_state("beam_azimuth") == 270
    assert driver.get_state("mic_peak_db") == -12
    assert driver.get_state("room_activity_db") == 30
    await driver.disconnect()


@pytest.mark.asyncio
async def test_every_command_has_a_branch(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    samples = {
        "gain": 0, "source": "LocalOutput", "level": "Low", "band": "2k",
        "brightness": 3, "mic_on": "Green", "mic_mute": "Red", "color": "Cyan",
        "zone": "2",
    }
    await driver.send_command("reference_auto_adjust_off")
    await _settle()
    for name, cdef in INFO["commands"].items():
        params = {p: samples[p] for p in cdef.get("params", {})}
        await driver.send_command(name, params)
        await _settle(2)
    with pytest.raises(ValueError, match="Unknown command"):
        await driver.send_command("no_such_command")
    await driver.disconnect()


# ── Device settings ──────────────────────────────────────────────────────────


def _candidate(key: str, sdef: dict, current, sim) -> object:
    stype = sdef["type"]
    if stype == "boolean":
        return not bool(current)
    if stype == "enum":
        values = [v["value"] if isinstance(v, dict) else v for v in sdef["values"]]
        return next(v for v in values if v != current)
    if stype == "number":
        return 2.0 if current != 2.0 else 3.0
    lo, hi = int(sdef["min"]), int(sdef["max"])
    step = DRV._STEP_BY_KEY.get(key, 1)
    if key.endswith(("_min", "_max")) and "zone" in key:
        prefix, axis, edge = key.rsplit("_", 2)
        width = 15 if prefix == "priority_zone" else 10
        other = sim.get_state(f"{prefix}_{axis}_{'max' if edge == 'min' else 'min'}")
        rng = range(lo, hi + 1, int(step))
        if edge == "min":
            return next(v for v in rng if v != current and other - v >= width)
        return next(v for v in reversed(rng) if v != current and v - other >= width)
    for v in (hi, lo):
        if v != current and v % int(step) == 0:
            return v
    raise AssertionError(key)


@pytest.mark.asyncio
async def test_every_device_setting_round_trips(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    # Manual reference gain is only accepted with auto adjust off.
    await driver.set_device_setting("reference_auto_adjust", False)
    await _settle()
    # A zone's max is widened before its min is moved, so every angle has
    # room to change without the zone getting too narrow.
    keys = sorted(INFO["device_settings"], key=lambda k: k.endswith("_min"))
    for key in keys:
        sdef = INFO["device_settings"][key]
        if key == "reference_auto_adjust":
            continue
        current = sim.get_state(key)
        value = _candidate(key, sdef, current, sim)
        await driver.set_device_setting(key, value)
        await _settle(3)
        assert sim.get_state(key) == value, key
        assert driver.get_state(key) == value, key
    await driver.set_device_setting("reference_auto_adjust", True)
    await _settle()
    assert sim.get_state("reference_auto_adjust") is True
    with pytest.raises(ValueError, match="Unknown device setting"):
        await driver.set_device_setting("no_such_setting", 1)
    await driver.disconnect()


@pytest.mark.asyncio
async def test_nested_setting_write_carries_its_sibling(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    seen: list = []
    original = sim.handle_request

    def spy(method, path, headers, body):
        if method == "PUT":
            seen.append((path, json.loads(body)))
        return original(method, path, headers, body)

    sim.handle_request = spy
    await driver.set_device_setting("exclusion_zone_2_azimuth_max", 100)
    await driver.set_device_setting("led_custom_color", "Yellow")
    await _settle()
    zone_put = next(b for p, b in seen if p.endswith("/exclusionZones/1"))
    assert zone_put == {"azimuth": {"min": 20, "max": 100}}
    led_put = next(b for p, b in seen if p.endswith("/leds/ring"))
    assert led_put == {"micCustom": {"color": "Yellow", "enabled": False}}
    assert driver.get_state("exclusion_zone_2_azimuth_max") == 100
    await driver.disconnect()


@pytest.mark.asyncio
async def test_setting_off_its_step_is_refused_before_the_wire(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    before = len(link.requests)
    with pytest.raises(ValueError, match="multiple of 30"):
        await driver.set_device_setting("beam_offset", 45)
    with pytest.raises(ValueError, match="multiple of 5"):
        await driver.set_device_setting("exclusion_zone_2_azimuth_min", 22)
    with pytest.raises(ValueError, match="multiple of 0.1"):
        await driver.set_device_setting("priority_zone_weight", 1.55)
    assert len(link.requests) == before
    assert sim.get_state("beam_offset") == 0
    await driver.disconnect()


@pytest.mark.asyncio
async def test_device_refusal_names_the_setting_and_the_reason(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    # Exclusion zone 2 is 20..70 wide; a max of 25 leaves 5 degrees.
    with pytest.raises(ValueError, match="Exclusion Zone 2: Azimuth Max") as excinfo:
        await driver.set_device_setting("exclusion_zone_2_azimuth_max", 25)
    assert "at least 10 degrees" in str(excinfo.value)
    assert driver.get_state("last_error") == str(excinfo.value)
    assert sim.get_state("exclusion_zone_2_azimuth_max") == 70
    await driver.disconnect()


# ── Firmware without a resource ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_resource_is_skipped_not_fatal(mocked_client):
    hidden = {
        "/api/audio/inputs/microphone/singleCapsuleMode",
        "/api/audio/inputs/microphone/beam/beamfreeze/autoHold",
    }
    driver, sim, link = _make(sim_config={"hidden_paths": sorted(hidden)})
    await _connect(driver, link, mocked_client)
    assert driver.get_state("connected") is True
    assert driver._unsupported == hidden
    assert driver.get_state("single_capsule_mode") is None
    assert driver.get_state("beam_freeze_hold_ms") is None
    assert _session_paths(sim) == _expected_paths(hidden=hidden)
    # Everything else still streams.
    sim.set_state("mute", True)
    await _settle()
    assert driver.get_state("mute") is True
    await driver.disconnect()


# ── Stream lifecycle ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_reopens_and_rearms_after_device_closes_it(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    first = sim.sessions[0]
    sim.close_session(first, notify=True)
    await _settle(20)
    assert sim.sessions and sim.sessions[0] != first
    assert _session_paths(sim) == _expected_paths()
    sim.set_state("mute", True)
    await _settle()
    assert driver.get_state("mute") is True
    await driver.disconnect()


# ── Faults ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_password_is_auth_fault_before_any_request(mocked_client):
    driver, sim, link = _make(driver_config={"password": ""})
    mocked_client(link)
    with pytest.raises(ConnectionFaultError) as excinfo:
        await driver.connect()
    assert excinfo.value.fault_code == "auth_failed"
    assert link.requests == []
    assert driver.get_state("connected") is False


@pytest.mark.asyncio
async def test_rejected_password_is_auth_fault(mocked_client):
    driver, sim, link = _make(driver_config={"password": "invalid"})
    mocked_client(link)
    with pytest.raises(ConnectionFaultError) as excinfo:
        await driver.connect()
    assert excinfo.value.fault_code == "auth_failed"
    assert "Control Cockpit" in str(excinfo.value)
    assert driver.get_state("connected") is False


@pytest.mark.asyncio
async def test_third_party_access_off_is_auth_fault(mocked_client):
    driver, sim, link = _make()
    sim.inject_error("third_party_disabled")
    mocked_client(link)
    with pytest.raises(ConnectionFaultError) as excinfo:
        await driver.connect()
    assert excinfo.value.fault_code == "auth_failed"
    assert "third-party access" in str(excinfo.value)


@pytest.mark.asyncio
async def test_wrong_product_is_invalid_config(mocked_client):
    driver, sim, link = _make()
    sim.set_state("product", "TCC2")
    mocked_client(link)
    with pytest.raises(ConnectionFaultError) as excinfo:
        await driver.connect()
    assert excinfo.value.fault_code == "invalid_config"
    assert "TCC2" in str(excinfo.value)


@pytest.mark.asyncio
async def test_unreachable_is_connection_error(mocked_client):
    driver, sim, link = _make()
    link.reachable = False
    mocked_client(link)
    with pytest.raises(ConnectionError):
        await driver.connect()
    assert driver.get_state("connected") is False


@pytest.mark.asyncio
async def test_poll_propagates_transport_errors(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    link.reachable = False
    with pytest.raises(httpx.ConnectError):
        await driver.poll()
    await driver.disconnect()


@pytest.mark.asyncio
async def test_mid_session_rejection_surfaces_through_poll(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    sim.inject_error("wrong_password")
    with pytest.raises(ConnectionFaultError) as excinfo:
        await driver.poll()
    assert excinfo.value.fault_code == "auth_failed"
    await driver.disconnect()


# ── Discovery ────────────────────────────────────────────────────────────────


def test_discovery_probe_matches_the_simulators_identity():
    probe = INFO["discovery"]["tcp_probe"]
    assert probe["tls"] is True and probe["port"] == 443
    sim = SIM.SennheiserTccmSimulator("tccm-sim", {})
    status, body = sim.handle_request("GET", "/api/device/identity", {}, "")[:2]
    assert status == 200
    reply = json.dumps(body)
    assert re.search(probe["expect_regex"], reply)
    model = re.search(probe["extract"]["model"]["regex"], reply)
    assert model and model.group(probe["extract"]["model"]["group"]) == "TCCM"
    # And not a sibling product from the same family.
    assert not re.search(probe["expect_regex"], '{"product": "TCC2"}')


# ── Simulator on its own ─────────────────────────────────────────────────────


def _auth(password="secret") -> dict[str, str]:
    token = base64.b64encode(f"api:{password}".encode()).decode()
    return {"authorization": f"Basic {token}"}


def test_sim_requires_credentials_except_on_open_resources():
    sim = SIM.SennheiserTccmSimulator("tccm-sim", {})
    assert sim.handle_request("GET", "/api/device/identity", {}, "")[0] == 200
    assert sim.handle_request("GET", "/api/ssc/version", {}, "")[0] == 200
    assert sim.handle_request("GET", "/api/device/state", {}, "")[0] == 401
    assert sim.handle_request("GET", "/api/device/state", _auth("invalid"), "")[0] == 401
    assert sim.handle_request("GET", "/api/device/state", _auth(), "")[0] == 200
    strict = SIM.SennheiserTccmSimulator("tccm-sim", {"password": "pw1"})
    assert strict.handle_request("GET", "/api/device/state", _auth("other"), "")[0] == 401
    assert strict.handle_request("GET", "/api/device/state", _auth("pw1"), "")[0] == 200


def test_sim_validation_codes():
    sim = SIM.SennheiserTccmSimulator("tccm-sim", {})
    put = lambda path, body: sim.handle_request("PUT", path, _auth(), json.dumps(body))  # noqa: E731
    assert put("/api/audio/outputs/analog", {"gain": 5})[0] == 422
    assert put("/api/audio/outputs/analog", {"gain": "loud"})[0] == 400
    assert put("/api/audio/outputs/analog", {"volume": 1})[0] == 400
    assert put("/api/audio/inputs/microphone/beam", {"offset": 45})[0] == 422
    assert put("/api/audio/inputs/microphone/denoiser", {"setting": "Max"})[0] == 422
    assert put("/api/audio/inputs/dante/reference", {"gain": 3})[0] == 409
    assert put("/api/audio/inputs/dante/reference",
               {"gain": 3, "farEndAutoAdjustEnabled": True})[0] == 422
    assert put("/api/audio/inputs/dante/reference",
               {"gain": 3, "farEndAutoAdjustEnabled": False})[0] == 200
    assert sim.get_state("reference_gain_db") == 3
    assert put("/api/audio/inputs/microphone/exclusionZones/1",
               {"azimuth": {"min": 20, "max": 25}})[0] == 422
    assert put("/api/audio/inputs/microphone/priorityZones/0",
               {"elevation": {"min": 60, "max": 70}})[0] == 422
    assert put("/api/audio/inputs/microphone/priorityZones/0", {"weight": 2.5})[0] == 200
    assert put("/api/audio/equalizer", {"gains": [1, 2, 3]})[0] == 400
    assert put("/api/audio/equalizer", {"gains": [1, 2, 3, 4, 5, 6, 7]})[0] == 200
    assert sim.get_state("eq_8k_db") == 7
    assert sim.handle_request("PUT", "/api/device/site", _auth(), "{}")[0] == 405
    assert sim.handle_request("GET", "/api/nothing", _auth(), "")[0] == 404
    assert sim.handle_request("PUT", "/api/audio/outputs/analog", _auth(), "{not json")[0] == 400


def test_sim_zone_collections_are_bare_arrays():
    sim = SIM.SennheiserTccmSimulator("tccm-sim", {})
    status, body = sim.handle_request(
        "GET", "/api/audio/inputs/microphone/exclusionZones", _auth(), ""
    )[:2]
    assert status == 200
    zones = json.loads(body)
    assert [z["id"] for z in zones] == [0, 1, 2, 3, 4]
    assert zones[0]["enabled"] is True and zones[0]["azimuth"] == {"min": 0, "max": 360}


@pytest.mark.asyncio
async def test_sim_subscription_semantics():
    sim = SIM.SennheiserTccmSimulator("tccm-sim", {})
    status, session_uuid, queue = sim.open_session(_auth())
    assert status == 200 and session_uuid and queue is not None
    first = queue.get_nowait()
    assert first.startswith("event: open\n")
    assert json.loads(first.split("data: ", 1)[1])["sessionUUID"] == session_uuid
    put = lambda path, body: sim.handle_request("PUT", path, _auth(), json.dumps(body))  # noqa: E731

    assert put(f"{_SUBS}/not-a-session", ["/api/device/state"])[0] == 422
    status, body = put(f"{_SUBS}/{session_uuid}", ["/api/device/state", "/api/nope"])[:2]
    assert status == 400 and body == {"path": "/api/nope", "error": 404}
    assert sim.session_paths(session_uuid) == set()

    assert put(f"{_SUBS}/{session_uuid}", ["/api/device/state", "/audio/outputs/global/mute"])[0] == 200
    assert sim.session_paths(session_uuid) == {"/api/device/state", "/api/audio/outputs/global/mute"}
    initial = [json.loads(queue.get_nowait().split("data: ", 1)[1]) for _ in range(2)]
    assert {list(d)[0] for d in initial} == {"/api/device/state", "/api/audio/outputs/global/mute"}
    assert queue.empty()

    assert put(f"{_SUBS}/{session_uuid}/add", ["/api/audio/outputs/analog"])[0] == 200
    assert list(json.loads(queue.get_nowait().split("data: ", 1)[1])) == ["/api/audio/outputs/analog"]
    assert put(f"{_SUBS}/{session_uuid}/remove", ["/api/device/state"])[0] == 200
    assert put(f"{_SUBS}/{session_uuid}/remove", ["/api/device/state"])[0] == 400
    status, listing = sim.handle_request("GET", f"{_SUBS}/{session_uuid}", _auth(), "")[:2]
    assert status == 200 and json.loads(listing) == [
        "/api/audio/outputs/analog", "/api/audio/outputs/global/mute",
    ]

    sim.set_state("mute", True)
    assert json.loads(queue.get_nowait().split("data: ", 1)[1]) == {
        "/api/audio/outputs/global/mute": {"enabled": True}
    }
    sim.set_state("device_state", "FirmwareUpdate")   # not subscribed any more
    assert queue.empty()

    assert sim.handle_request("DELETE", f"{_SUBS}/{session_uuid}", _auth(), "")[0] == 200
    assert queue.get_nowait().startswith("event: close\n")
    assert queue.get_nowait() is None
    assert sim.sessions == []
    assert sim.open_session(_auth("invalid"))[0] == 401
