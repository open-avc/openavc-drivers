"""Driver + simulator tests for nureva_hdl310_hdl410 (Nureva HDL310 / HDL410
local API).

Dual-proof round trip: the real driver's httpx client is wired to the real
simulator through httpx.MockTransport, and the two event streams are served
as real streaming responses fed by the simulator's stream queues, so the
driver's own stream loops run in-test. Both sides are asserted.

Covers:
  - connect: the two mandatory headers, the login, the model guard, the
    first read of every resource, the zone children;
  - every command and every device setting against the simulator, read back
    from the device rather than assumed;
  - push: status lights, USB (both documented shapes), the speaker bars,
    calibration started and completed, the camera switcher, a layout change
    reconciling the zone roster;
  - sound location: the zone a reading falls in sets that zone's active flag,
    the throttle writes the newest reading, the errors the system sends
    instead of readings;
  - the documented field-name variants (zoneId / ZoneLabel and the plural
    zone map);
  - faults: a refused password, a blank password accepted, a password changed
    mid-session (one re-login, then auth_failed), the rate limit kept apart
    from a refused password, an HDL300 refused as the wrong model, an
    unreachable host, poll propagating transport errors;
  - firmware: an attribute this firmware lacks refused with the device's
    reason; an HDL310 without room profiles;
  - the discovery probe matching the simulator's own capabilities reply.

Loads the driver and simulator with the ``openavc.*`` imports stubbed so the
community CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
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
DRIVER_PATH = REPO_ROOT / "audio" / "nureva_hdl310_hdl410.py"
SIM_PATH = REPO_ROOT / "audio" / "nureva_hdl310_hdl410_sim.py"

Z1 = "2345313e-deb8-4cd4-a58a-2df031296958"
Z2 = "9599a6a6-1603-4ceb-97f8-0008163ad88c"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect lifecycle for a driver that owns its
    session; state and the watchdog come from the shared stubs."""

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0
        self.forced: list[tuple[str, str]] = []

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

    def _force_disconnect(self, code="no_response", message=""):
        self.forced.append((code, message))
        self._connected = False
        self.set_state("connected", False)

    async def connect(self):
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
        try:
            await self._initial_sync()
        except Exception:
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            raise

    async def disconnect(self):
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


install_stubs(base_driver=_FakeBaseDriver)
DRV = load_module("nureva_hdl310_hdl410_under_test", DRIVER_PATH)
SIM = load_module("nureva_hdl310_hdl410_sim_under_test", SIM_PATH)

INFO = DRV.NurevaHdl310Hdl410Driver.DRIVER_INFO


# ── Harness ──────────────────────────────────────────────────────────────────


class _Link:
    def __init__(self, sim):
        self.sim = sim
        self.reachable = True
        self.requests: list[tuple[str, str, dict]] = []


async def _sse_body(queue: asyncio.Queue, link: _Link):
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            yield chunk.encode("utf-8")
    finally:
        link.sim.close_stream(queue)


def _make_handler(link: _Link):
    def handler(request: httpx.Request) -> httpx.Response:
        if not link.reachable:
            raise httpx.ConnectError("Connection refused")
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode("ascii")
        headers = dict(request.headers)
        link.requests.append((request.method, path, headers))
        if (
            request.method == "GET"
            and request.url.path in ("/api/v1/events", "/api/v1/data", "/api/v1/heartbeat")
            and "text/event-stream" in headers.get("accept", "")
        ):
            status, error, queue = link.sim.open_stream(path, headers)
            if status != 200 or queue is None:
                return httpx.Response(status, json=error or {})
            return httpx.Response(
                200, content=_sse_body(queue, link),
                headers={"content-type": "text/event-stream"},
            )
        body = request.content.decode("utf-8") if request.content else ""
        status, resp_body = link.sim.handle_request(request.method, path, headers, body)[:2]
        if isinstance(resp_body, dict):
            return httpx.Response(status, json=resp_body)
        return httpx.Response(status, text=str(resp_body))

    return handler


def _make(sim_config=None, driver_config=None):
    sim = SIM.NurevaHdl310Hdl410Simulator("hdl-sim", sim_config or {})
    link = _Link(sim)
    cfg = dict(INFO["default_config"])
    cfg.update({"host": "10.0.0.1", "poll_interval": 0, "timeout": 2.0, "sound_location_interval": 0.2})
    cfg.update(driver_config or {})
    driver = DRV.NurevaHdl310Hdl410Driver("hdl", cfg, StubState(), StubEvents())
    return driver, sim, link


def _zone(driver, zone_id: str, prop: str):
    return driver.get_child_state("zone", zone_id).get(prop)


async def _settle(rounds: int = 10) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0.01)


async def _until(pred, timeout: float = 2.0) -> bool:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


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


async def _close(driver, sim):
    await driver.disconnect()
    for queue in list(sim._streams):
        queue.put_nowait(None)
    if sim._ticker is not None:
        sim._ticker.cancel()
    await _settle(3)


# ── Declarations ─────────────────────────────────────────────────────────────


def test_metadata_contract():
    for key in ("id", "name", "manufacturer", "category", "version", "author",
                "transport", "description", "source_url"):
        assert INFO.get(key), key
    assert INFO["id"] == "nureva_hdl310_hdl410"
    assert INFO["verified"] is False
    assert INFO["compatible_models"][0]["confidence"] == "untested"
    assert INFO["config_schema"]["password"]["secret"] is True
    assert INFO["default_config"]["password"] == ""
    for key, sdef in INFO["device_settings"].items():
        assert sdef.get("state_key", key) in INFO["state_variables"], key
    for action in INFO["quick_actions"]:
        assert action in INFO["commands"]
    # Volume is relative only: no command or setting writes a level.
    assert "speaker_volume" not in INFO["device_settings"]
    assert not any("set_volume" in c for c in INFO["commands"])
    # The first parameter-free command (what the lifecycle smoke sends) is
    # harmless.
    first = next(c for c, d in INFO["commands"].items() if not d.get("params"))
    assert first == "mute_microphone"
    assert INFO["commands"]["calibrate"].get("confirm")


def test_every_audio_field_is_a_declared_state_variable():
    for key in DRV._AUDIO_FIELDS.values():
        assert key in INFO["state_variables"], key
    # The simulator serves the same attributes.
    assert set(DRV._AUDIO_FIELDS) <= set(SIM._AUDIO)


def test_discovery_probe_matches_the_simulators_capabilities_reply():
    probe = INFO["discovery"]["tcp_probe"]
    request = probe["send_ascii"]
    head, _, _ = request.partition("\r\n\r\n")
    lines = head.split("\r\n")
    method, path, _proto = lines[0].split(" ")
    headers = dict(line.split(": ", 1) for line in lines[1:])
    sim = SIM.NurevaHdl310Hdl410Simulator("hdl-sim", {})
    status, body = sim.handle_request(method, path, headers, "")
    assert status == 200
    # The probe reads at most 4096 bytes: the title must be inside them.
    text = json.dumps(body)[:3800]
    assert re.search(probe["expect_regex"], text)
    # Without the two headers the device answers 400, never the title.
    status, body = sim.handle_request("GET", "/api/v1", {"Host": "x"}, "")
    assert status == 400
    assert not re.search(probe["expect_regex"], json.dumps(body))
    assert INFO["discovery"]["oui"] == ["e0:e7:bb"]


# ── Connect ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_populates_state(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        assert driver.get_state("connected") is True
        assert driver.get_state("model") == "hdl410"
        assert driver.get_state("bar_count") == 2
        assert driver.get_state("firmware_version") == "1.9.278056-0"
        assert driver.get_state("speaker_volume") == 14
        assert driver.get_state("microphone_mute") is False
        assert driver.get_state("network_led_colour") == "green"
        assert driver.get_state("usb_status") == "connected"
        assert driver.get_state("zone_count") == 2
        assert set(driver.list_children("zone")) == {Z1, Z2}
        assert _zone(driver, Z1, "label") == "Presenter"
        assert _zone(driver, Z2, "camera_input") == "USB1"
        assert driver.get_state("room_profile") == "Room Profile 1"
        assert driver.get_state("camera_switcher_model") == "CAM230"
        assert driver.get_state("gateway") == "10.0.0.254"
        # Every request carried both mandatory headers; the authorized ones
        # carried the token the login returned, under the device's scheme.
        for _method, _path, headers in link.requests:
            assert headers.get("nureva-client-id") == "OpenAVC"
            assert headers.get("nureva-client-version") == INFO["version"]
        authed = [h for m, p, h in link.requests if p != "/api/v1/auth/login"]
        assert authed and all(h.get("authorization", "").startswith("Nureva ") for h in authed)
        # Both streams are open, the data stream asking for both events.
        paths = [p for _m, p, _h in link.requests]
        assert "/api/v1/events" in paths
        assert "/api/v1/data?events=soundLocation,deviceMetrics" in paths
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_blank_password_is_sent(mocked_client):
    driver, sim, link = _make(driver_config={"password": ""})
    await _connect(driver, link, mocked_client)
    try:
        logins = [h for m, p, h in link.requests if p == "/api/v1/auth/login"]
        assert len(logins) == 1
        assert driver.get_state("connected") is True
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_refused_password_is_auth_failed(mocked_client):
    driver, sim, link = _make(sim_config={"password": "ABC-ABC-ABC"}, driver_config={"password": "nope"})
    mocked_client(link)
    with pytest.raises(ConnectionFaultError) as err:
        await driver.connect()
    assert err.value.fault_code == "auth_failed"
    assert driver.get_state("connected") is not True


@pytest.mark.asyncio
async def test_rate_limit_is_not_a_refused_password(mocked_client):
    driver, sim, link = _make()
    sim.inject_error("rate_limited")
    mocked_client(link)
    with pytest.raises(ConnectionError) as err:
        await driver.connect()
    assert not isinstance(err.value, ConnectionFaultError)
    assert "limiting requests" in str(err.value)


@pytest.mark.asyncio
async def test_wrong_model_is_invalid_config(mocked_client):
    driver, sim, link = _make(sim_config={"model": "hdl300"})
    mocked_client(link)
    with pytest.raises(ConnectionFaultError) as err:
        await driver.connect()
    assert err.value.fault_code == "invalid_config"
    assert "hdl300" in str(err.value)


@pytest.mark.asyncio
async def test_unreachable_host(mocked_client):
    driver, sim, link = _make()
    link.reachable = False
    mocked_client(link)
    with pytest.raises(ConnectionError) as err:
        await driver.connect()
    assert not isinstance(err.value, ConnectionFaultError)


@pytest.mark.asyncio
async def test_password_changed_mid_session_relogs_once_then_auth_failed(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        sim.inject_error("password_changed")
        before = sum(1 for _m, p, _h in link.requests if p == "/api/v1/auth/login")
        with pytest.raises(ConnectionFaultError) as err:
            await driver.poll()
        assert err.value.fault_code == "auth_failed"
        after = sum(1 for _m, p, _h in link.requests if p == "/api/v1/auth/login")
        assert after - before == 1
        assert driver.forced and driver.forced[0][0] == "auth_failed"
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_poll_propagates_transport_errors(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        link.reachable = False
        with pytest.raises(httpx.ConnectError):
            await driver.poll()
    finally:
        link.reachable = True
        await _close(driver, sim)


# ── Commands and settings ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_command(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        await driver.send_command("mute_microphone")
        assert sim.get_state("microphone_mute") is True and driver.get_state("microphone_mute") is True
        await driver.send_command("unmute_microphone")
        assert driver.get_state("microphone_mute") is False
        await driver.send_command("toggle_microphone_mute")
        assert driver.get_state("microphone_mute") is True
        await driver.send_command("toggle_microphone_mute")
        assert driver.get_state("microphone_mute") is False
        await driver.send_command("volume_up")
        assert driver.get_state("speaker_volume") == 15
        await driver.send_command("volume_down")
        assert driver.get_state("speaker_volume") == 14
        sim.set_state("speaker_volume", 0)
        await driver.send_command("volume_down")
        assert driver.get_state("speaker_volume") == 0
        await driver.send_command("audience_mute_on")
        assert driver.get_state("audience_mute") is True
        await driver.send_command("audience_mute_off")
        assert driver.get_state("audience_mute") is False
        await driver.send_command("voice_amplification_on")
        assert driver.get_state("voice_amplification") is True
        await driver.send_command("voice_amplification_off")
        assert driver.get_state("voice_amplification") is False
        await driver.send_command("set_treble", {"level": 33})
        await driver.send_command("set_bass", {"level": 66})
        assert driver.get_state("speaker_treble") == 33 and driver.get_state("speaker_bass") == 66
        assert await driver.send_command("identify") is True
        identify = [(m, p) for m, p, _h in link.requests if p == "/api/v1/audio/identify"]
        assert identify == [("POST", "/api/v1/audio/identify")]
        await driver.send_command("activate_room_profile", {"profile": "Room Profile 2"})
        assert driver.get_state("room_profile") == "Room Profile 2"
        await driver.send_command("calibrate")
        assert await _until(lambda: driver.get_state("calibrating") is True)
        with pytest.raises(ValueError):
            await driver.send_command("no_such_command")
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_every_command_in_driver_info_is_handled(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        params = {
            "set_treble": {"level": 10}, "set_bass": {"level": 10},
            "activate_room_profile": {"profile": "Room Profile 1"},
        }
        for command in INFO["commands"]:
            result = await driver.send_command(command, params.get(command))
            assert result is True, command
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_device_settings_round_trip(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        values = {
            "speaker_treble": 61, "speaker_bass": 39, "microphone_gain": -2,
            "echo_reduction": "High", "noise_reduction": "Low",
            "aux_output_mode": "MixedSignal", "voice_amplification": True,
            "voice_amplification_level": 18, "voice_amplification_aux_in": "Mic",
            "dynamic_boost": True, "microphone_ducking": True,
            "camera_switcher_enabled": False, "default_camera_input": "USB1",
            "zone_trigger_wait_ms": 2000, "switch_to_default_wait_ms": 9000,
        }
        for key, value in values.items():
            await driver.set_device_setting(key, value)
            await _settle(2)
            assert driver.get_state(key) == value, key
        # The switching defaults went out as one complete object.
        assert sim.get_state("default_camera_input") == "USB1"
        assert sim.get_state("zone_trigger_wait_ms") == 2000
        assert sim.get_state("switch_to_default_wait_ms") == 9000
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_firmware_refusal_lands_in_last_error(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        with pytest.raises(ValueError) as err:
            await driver.set_device_setting("voice_amp_gate_threshold", 70)
        assert "Firmware version is < 2.0" in str(err.value)
        assert "Firmware version is < 2.0" in driver.get_state("last_error")
        with pytest.raises(ValueError):
            await driver.set_device_setting("speaker_treble", 101)
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_hdl310_on_firmware_2(mocked_client):
    driver, sim, link = _make(sim_config={"model": "hdl310", "firmware_version": "2.0.10-0"})
    await _connect(driver, link, mocked_client)
    try:
        assert driver.get_state("bar_count") == 1
        assert driver.get_state("room_profile_options") == "[]"
        assert driver.get_state("microphone_gain") is None
        await driver.set_device_setting("voice_amp_usb_gain", 12)
        assert driver.get_state("voice_amp_usb_gain") == 12
        await driver.set_device_setting("sound_location_algorithm", "TDOA")
        assert driver.get_state("sound_location_algorithm") == "TDOA"
        with pytest.raises(ValueError) as err:
            await driver.send_command("activate_room_profile", {"profile": "anything"})
        assert "Unsupported device type" in str(err.value)
    finally:
        await _close(driver, sim)


# ── Push ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_events(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        sim.set_state("usb_status", "disconnected")
        assert await _until(lambda: driver.get_state("usb_status") == "disconnected")
        # The room status tutorial's shape for the same event.
        await driver._handle_event("usbConnection", '{"connected": true}')
        assert driver.get_state("usb_status") == "connected"
        sim.set_state("components_status", "disconnected")
        assert await _until(lambda: driver.get_state("components_status") == "disconnected")
        sim.set_state("network_led_colour", "red")
        assert await _until(lambda: driver.get_state("network_led_colour") == "red")
        sim.set_state("console_led_colour", "yellow")
        assert await _until(lambda: driver.get_state("console_led_colour") == "yellow")
        assert driver.get_state("network_led_colour") == "red"
        sim.set_state("device_status", "Disconnected")
        assert await _until(lambda: driver.get_state("device_status") == "Disconnected")
        sim.set_state("active_camera_input", "USB2")
        assert await _until(lambda: driver.get_state("active_camera_input") == "USB2")
        sim.set_state("calibrating", True)
        assert await _until(lambda: driver.get_state("calibrating") is True)
        sim.set_state("calibrating", False)
        assert await _until(lambda: driver.get_state("calibrating") is False)
        # Audio settings are not announced: a mute at the device shows on the
        # next poll, not before.
        sim.set_state("microphone_mute", True)
        await _settle()
        assert driver.get_state("microphone_mute") is False
        await driver.poll()
        assert driver.get_state("microphone_mute") is True
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_layout_change_reconciles_zones(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        new_zone = "aaaaaaaa-0000-0000-0000-000000000001"
        sim._patch_layout(json.dumps({
            "zones": [{"id": new_zone, "label": "Lectern",
                       "geometry": {"point1": {"x": -1000, "y": 300}, "point2": {"x": 1500, "y": 3000}}}],
            "cameraSwitcherZoneInputMap": [{"zoneId": new_zone, "inputPort": "USB2"}],
        }))
        assert await _until(lambda: driver.list_children("zone") == [new_zone])
        assert _zone(driver, new_zone, "label") == "Lectern"
        assert _zone(driver, new_zone, "camera_input") == "USB2"
        assert driver.get_state("zone_count") == 1
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_example_spelling_of_zones(mocked_client):
    driver, sim, link = _make(sim_config={"example_spelling": True})
    await _connect(driver, link, mocked_client)
    try:
        assert _zone(driver, Z1, "label") == "Presenter"
        assert _zone(driver, Z1, "camera_input") == "HDMI"
    finally:
        await _close(driver, sim)


# ── Sound location ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sound_location_drives_zone_flags(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        assert await _until(lambda: driver.get_state("active_zone_label") == "Presenter")
        assert _zone(driver, Z1, "active") is True
        assert _zone(driver, Z2, "active") is False
        assert driver.get_state("sound_detected") is True
        sim.set_state("talker_y_mm", 5000)
        assert await _until(lambda: driver.get_state("active_zone_id") == Z2)
        assert _zone(driver, Z2, "active") is True
        assert _zone(driver, Z1, "active") is False
        sim.set_state("talker_power_db", 0)
        assert await _until(lambda: driver.get_state("sound_detected") is False)
        assert driver.get_state("active_zone_label") == ""
        # The position is not updated from a 0 dB reading.
        assert driver.get_state("sound_y_mm") == 5000
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_throttle_writes_the_newest_reading(mocked_client):
    driver, sim, link = _make(driver_config={"sound_location_interval": 1.0, "enable_sound_location": False})
    await _connect(driver, link, mocked_client)
    try:
        writes: list[int] = []
        original = driver._apply_location

        def counting(body):
            writes.append(body["coordinates"]["x"])
            original(body)

        driver._apply_location = counting
        driver._sound_location = True
        for x in (100, 200, 300, 400):
            driver._queue_location({"powerLevel": 50, "coordinates": {"x": x, "y": 1000}, "triggeredZones": []})
        assert writes == [100]
        assert await _until(lambda: writes == [100, 400], 2.0)
        assert driver.get_state("sound_x_mm") == 400
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_sound_location_errors(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        await driver.send_command("mute_microphone")
        assert await _until(lambda: driver.get_state("sound_location_status") == "Microphone muted")
        assert _zone(driver, Z1, "active") is False
        await driver.send_command("unmute_microphone")
        assert await _until(lambda: driver.get_state("sound_location_status") == "")
        assert await _until(lambda: _zone(driver, Z1, "active") is True)
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_unsupported_sound_location_is_dropped_from_the_filter(mocked_client):
    driver, sim, link = _make(sim_config={"sound_location_supported": False})
    await _connect(driver, link, mocked_client)
    try:
        assert await _until(lambda: driver.get_state("sound_location_status") == "Unsupported device")
        assert driver._data_path() == "/api/v1/data?events=deviceMetrics"
        # Background noise still arrives.
        await driver._handle_data("deviceMetrics", '{"backgroundNoise": 41}')
        assert driver.get_state("background_noise_db") == 41
        # An event name the system does not have is the documented 404 event.
        await driver._handle_data(
            "error (soundLocation)", '{"statusCode": 404, "event": "soundLocation", "message": "Event soundLocation is not supported"}',
        )
        assert "not supported" in driver.get_state("sound_location_status")
    finally:
        await _close(driver, sim)


@pytest.mark.asyncio
async def test_sound_location_off_and_on(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    try:
        assert await _until(lambda: driver.get_state("active_zone_label") == "Presenter")
        await driver.send_command("sound_location_off")
        assert driver.get_state("sound_location_feed") is False
        assert driver.get_state("active_zone_label") == ""
        await _settle()
        assert driver._data_path() == "/api/v1/data?events=deviceMetrics"
        await driver.send_command("sound_location_on")
        assert await _until(lambda: driver.get_state("active_zone_label") == "Presenter")
    finally:
        await _close(driver, sim)


# ── Simulator on its own ─────────────────────────────────────────────────────


def _hdr(token=""):
    headers = {"Nureva-Client-Id": "t", "Nureva-Client-Version": "1"}
    if token:
        headers["Authorization"] = f"Nureva {token}"
    return headers


def test_simulator_gates():
    sim = SIM.NurevaHdl310Hdl410Simulator("hdl-sim", {})
    status, body = sim.handle_request("GET", "/api/v1/audio", {}, "")
    assert status == 400
    assert [e["message"] for e in body["errors"]] == [
        "RESTApi: Nureva-Client-Id is missing", "RESTApi: Nureva-Client-Version is missing",
    ]
    assert sim.handle_request("GET", "/api/v1/audio", _hdr(), "")[0] == 401
    status, body = sim.handle_request("POST", "/api/v1/auth/login", _hdr(), json.dumps({"account": "general", "password": "x"}))
    assert status == 200
    token = body["authParameters"]
    assert sim.handle_request("GET", "/api/v1/audio", _hdr(token), "")[0] == 200
    status, body = sim.handle_request("POST", "/api/v1/auth/login", _hdr(), json.dumps({"account": "general", "password": 5}))
    assert status == 400
    status, body = sim.handle_request("PATCH", "/api/v1/audio", _hdr(token), json.dumps({"speakerVolume": 3}))
    assert status == 400 and "speakerVolume" in body["errors"][0]["message"]
    status, body = sim.handle_request("PATCH", "/api/v1/audio", _hdr(token), "{}")
    assert status == 400
    status, body = sim.handle_request("PUT", "/api/v1/audio/volume/change", _hdr(token), json.dumps({"operation": "up"}))
    assert status == 400
    status, body = sim.handle_request("POST", "/api/v1/audio/identify", _hdr(token), json.dumps({"port": 1}))
    assert status == 400 and body["problems"]
    assert sim.handle_request("GET", "/api/v1/nothing", _hdr(token), "")[0] == 404
