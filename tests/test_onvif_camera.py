"""Driver + simulator tests for onvif_camera (generic ONVIF camera, SOAP + pull-point events).

Dual-proof round trip: the real driver is wired to the real simulator over an
in-memory httpx transport, so the simulator renders the SOAP a camera sends
and the driver parses it, both sides asserted.

Covers:
  - the connect sequence: clock read, GetServices, identity, Media2 profiles as
    child entities with stream + snapshot URLs, PTZ node, imaging options, relay
    and digital-input children, and the pull-point subscription;
  - both authentication paths of Core spec 5.9.1: WS-UsernameToken digest
    (with the camera's clock ten minutes off), the HTTP Digest fallback on a
    401, and a wrong password surfacing as a typed auth_failed fault;
  - the Media 1 path and the GetCapabilities path for older firmware, and the
    XAddr rewrite for a camera that announces an address it is not reached on;
  - PTZ: continuous drive changes the position read back by poll, stop, absolute
    and relative moves, home, presets (save / recall by name / delete / refused
    while moving) and the preset Reached event;
  - imaging: level settings inside and outside the camera's range, modes, focus
    drive with the mode switching to manual, imaging presets;
  - relays and inputs: a relay command lands in the simulator and comes back
    through the Relay event; an input toggle, motion, tamper and signal loss
    arrive push-only with polling off;
  - the subscription lifecycle: reference parameters echoed, renewal when the
    camera grants a short term, resubscribe after the camera forgets the pull
    point, unsubscribe on disconnect;
  - stream credentials stay out of state unless asked for.

The driver is loaded with the ``openavc.*`` imports stubbed so the community CI
stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    ConnectionFaultError,
    DeviceSettingValueError,
    StubBaseDriver,
    StubEvents,
    StubState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "cameras" / "onvif_camera.py"
SIM_PATH = REPO_ROOT / "cameras" / "onvif_camera_sim.py"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect lifecycle for a driver that owns its
    session; state, children and the watchdog come from the shared stubs."""

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0
        self._push_subscription = None

    async def _verify_reachable(self, host, port, timeout=3.0):
        return True

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

    def _handle_transport_disconnect(self):
        self._connected = False
        self.set_state("connected", False)

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
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise
        interval = self.config.get("poll_interval", 0)
        if interval > 0:
            await self.start_polling(interval)

    async def disconnect(self):
        await self._stop_push()
        await self.stop_polling()
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


install_stubs(base_driver=_FakeBaseDriver)
DRV = load_module("onvif_camera_under_test", DRIVER_PATH)
SIM = load_module("onvif_camera_sim_under_test", SIM_PATH)


# ── Harness ──────────────────────────────────────────────────────────────────


def _make(sim_config=None, driver_config=None):
    sim = SIM.OnvifCameraSimulator("cam-sim", sim_config or {})

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        headers = dict(request.headers)
        path = request.url.raw_path.decode("ascii")
        result = await sim.handle_request_async(request.method, path, headers, body)
        if len(result) == 3:
            status, text, resp_headers = result
        else:
            status, text = result
            resp_headers = {}
        return httpx.Response(status, text=str(text), headers=resp_headers)

    cfg = {
        "host": "10.0.0.9", "port": 80, "username": "admin", "password": "secret",
        "poll_interval": 0,
    }
    cfg.update(driver_config or {})
    driver = DRV.OnvifCameraDriver("cam1", cfg, StubState(), StubEvents())
    return driver, sim, handler


async def _connect(driver, handler):
    original = httpx.AsyncClient

    def _mocked(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = _mocked  # type: ignore[assignment]
    try:
        await driver.connect()
    finally:
        httpx.AsyncClient = original  # type: ignore[assignment]
    await _settle()


async def _settle(seconds: float = 0.08):
    await asyncio.sleep(seconds)


async def _connected_pair(sim_config=None, driver_config=None):
    driver, sim, handler = _make(sim_config, driver_config)
    await _connect(driver, handler)
    return driver, sim


def _run(coro):
    return asyncio.run(coro)


def _st(driver, key):
    return driver.state.get(f"device.{driver.device_id}.{key}")


def _child(driver, child_type, local_id, key):
    return driver.state.get(f"device.{driver.device_id}.{child_type}.{local_id}.{key}")


# ── Metadata / shape ─────────────────────────────────────────────────────────


def test_every_declared_command_has_a_dispatch_branch():
    declared = set(DRV.OnvifCameraDriver.DRIVER_INFO["commands"])
    assert set(DRV.OnvifCameraDriver._DISPATCH) == declared


def test_driver_declares_http_and_its_discovery_companion():
    info = DRV.OnvifCameraDriver.DRIVER_INFO
    assert info["transport"] == "http"
    assert info["discovery"]["python"]["cross_vendor"] is True
    assert (DRIVER_PATH.parent / info["discovery"]["python"]["file"].lstrip("./")).exists()
    for action in info["actions"]:
        assert action["command"] in info["commands"]
    for quick in info["quick_actions"]:
        assert quick in info["commands"]


def test_password_digest_matches_the_wss_profile_formula():
    # WSS UsernameToken Profile 1.1 §3.1: Base64(SHA-1(nonce + created + password)).
    import base64
    import hashlib
    import re
    from datetime import datetime, timezone

    header = DRV._wsse_header("u", "pw", datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc))
    nonce_b64 = re.search(r"<wsse:Nonce[^>]*>([^<]+)</wsse:Nonce>", header).group(1)
    created = re.search(r"<wsu:Created>([^<]+)</wsu:Created>", header).group(1)
    digest = re.search(r"<wsse:Password[^>]*>([^<]+)</wsse:Password>", header).group(1)
    expected = base64.b64encode(
        hashlib.sha1(base64.b64decode(nonce_b64) + created.encode() + b"pw").digest()
    ).decode()
    assert digest == expected
    assert created == "2026-09-08T12:00:00.000Z"


# ── Connect ──────────────────────────────────────────────────────────────────


def test_connect_reads_identity_profiles_ptz_and_io():
    async def scenario():
        driver, sim = await _connected_pair({"require_auth": True})
        try:
            assert _st(driver, "connected") is True
            assert _st(driver, "manufacturer") == "OpenAVC"
            assert _st(driver, "model") == "Simulated PTZ Camera"
            assert _st(driver, "serial_number") == "SIM-ONVIF-0001"
            assert driver._auth_mode == "wsse"
            assert abs(_st(driver, "clock_offset_s")) < 2
            # Media2 profiles as children, with their stream and snapshot.
            assert driver.list_children("profile") == ["profile_1", "profile_2"]
            assert _child(driver, "profile", "profile_1", "preview_url") == "rtsp://10.0.0.9:554/stream1"
            assert _child(driver, "profile", "profile_1", "preview_format") == "rtsp"
            assert _child(driver, "profile", "profile_1", "resolution") == "1920x1080"
            assert _child(driver, "profile", "profile_1", "has_ptz") is True
            assert _child(driver, "profile", "profile_2", "has_ptz") is False
            assert _child(driver, "profile", "profile_2", "snapshot_url") == "http://10.0.0.9:80/snapshot2.jpg"
            # The control profile is the one with PTZ, and the device-level
            # preview mirrors it.
            assert _st(driver, "profile_token") == "profile_1"
            assert _st(driver, "preview_url") == "rtsp://10.0.0.9:554/stream1"
            assert "@" not in _st(driver, "preview_url")
            assert _st(driver, "ptz_supported") is True
            assert _st(driver, "home_supported") is True
            assert _st(driver, "preset_count") == 2
            options = json.loads(_st(driver, "preset_options"))
            assert {o["value"] for o in options} == {"1", "2"}
            assert json.loads(_st(driver, "aux_command_options")) == ["tt:IRLamp|On", "tt:IRLamp|Off", "tt:Wiper|On"]
            assert _st(driver, "move_status") == "idle"
            assert _st(driver, "pan_position") == 0.0
            # Imaging.
            assert _st(driver, "brightness") == 50.0
            assert _st(driver, "brightness_range") == "0..100"
            assert _st(driver, "exposure_mode") == "auto"
            assert _st(driver, "focus_mode") == "auto"
            assert _st(driver, "ir_cut_filter") == "auto"
            assert _st(driver, "backlight_compensation") is False
            assert _st(driver, "focus_move_status") == "idle"
            assert json.loads(_st(driver, "imaging_preset_options")) == [
                {"value": "indoor", "label": "Indoor"}, {"value": "outdoor", "label": "Outdoor"},
            ]
            # Relay + input children.
            assert driver.list_children("relay") == ["relay_1"]
            assert _child(driver, "relay", "relay_1", "mode") == "bistable"
            assert _child(driver, "relay", "relay_1", "idle_state") == "open"
            assert driver.list_children("input") == ["input_1"]
            # Events: the subscription is open and the Initialized properties landed.
            assert _st(driver, "events_active") is True
            assert sim.subscribe_count == 1
            assert _child(driver, "relay", "relay_1", "active") is False
            assert _child(driver, "input", "input_1", "active") is False
            assert _st(driver, "motion") is False
            assert _st(driver, "signal_loss") is False
            assert "secret" in driver.redacted_secrets
        finally:
            await driver.disconnect()
        assert _st(driver, "connected") is False
        assert _st(driver, "events_active") is False
        assert sim.unsubscribe_count == 1
        assert sim.get_state("subscriptions") == 0

    _run(scenario())


def test_wrong_password_is_a_typed_auth_failure():
    async def scenario():
        driver, sim, handler = _make({"require_auth": True}, {"password": "wrong"})
        with pytest.raises(ConnectionFaultError) as exc_info:
            await _connect(driver, handler)
        assert exc_info.value.fault_code == "auth_failed"
        assert _st(driver, "connected") is not True

    _run(scenario())


def test_no_login_entered_says_so_instead_of_blaming_the_password():
    async def scenario():
        driver, sim, handler = _make({"require_auth": True}, {"username": "", "password": ""})
        with pytest.raises(ConnectionFaultError) as exc_info:
            await _connect(driver, handler)
        assert exc_info.value.fault_code == "auth_failed"
        assert "none is entered" in str(exc_info.value)
        assert "create an ONVIF user" in str(exc_info.value)

    _run(scenario())


def test_camera_clock_ten_minutes_off_still_logs_in():
    async def scenario():
        driver, sim = await _connected_pair({"require_auth": True, "clock_skew_s": 600})
        try:
            assert _st(driver, "connected") is True
            assert 595 < _st(driver, "clock_offset_s") < 605
        finally:
            await driver.disconnect()

    _run(scenario())


def test_http_digest_only_camera_switches_the_session():
    async def scenario():
        driver, sim = await _connected_pair({"require_auth": True, "auth_mode": "digest"})
        try:
            assert _st(driver, "connected") is True
            assert driver._auth_mode == "digest"
            assert _st(driver, "model") == "Simulated PTZ Camera"
            # And commands keep working over Digest.
            await driver.send_command("pt_right", {"speed": 0.5})
            assert sim.get_state("pan_velocity") == 0.5
        finally:
            await driver.disconnect()

    _run(scenario())


def test_media1_only_camera_still_yields_streams():
    async def scenario():
        driver, sim = await _connected_pair({"media2": False})
        try:
            assert "media2" not in driver._services
            assert driver.list_children("profile") == ["profile_1", "profile_2"]
            assert _child(driver, "profile", "profile_2", "preview_url") == "rtsp://10.0.0.9:554/stream2"
            assert _child(driver, "profile", "profile_2", "resolution") == "640x360"
            assert _child(driver, "profile", "profile_2", "framerate") == 15.0
            assert "GetStreamUri" in sim.calls
        finally:
            await driver.disconnect()

    _run(scenario())


def test_legacy_firmware_without_getservices_uses_getcapabilities():
    async def scenario():
        driver, sim = await _connected_pair({"legacy_capabilities": True})
        try:
            assert "GetCapabilities" in sim.calls
            assert set(driver._services) >= {"device", "media", "ptz", "imaging", "events"}
            assert _st(driver, "ptz_supported") is True
            assert _st(driver, "events_active") is True
        finally:
            await driver.disconnect()

    _run(scenario())


def test_announced_addresses_are_repointed_at_the_configured_host():
    async def scenario():
        driver, sim = await _connected_pair({"xaddr_host": "192.0.2.1"})
        try:
            assert driver._services["ptz"] == "http://10.0.0.9:80/onvif/ptz_service"
            assert driver._subscription_url.startswith("http://10.0.0.9:80/onvif/event_service/pullpoint_")
            # The stream URL is the camera's own claim and is left alone.
            assert _st(driver, "preview_url") == "rtsp://192.0.2.1:554/stream1"
        finally:
            await driver.disconnect()

    _run(scenario())


def test_credentials_go_into_stream_urls_only_when_asked():
    async def scenario():
        driver, sim = await _connected_pair(None, {"credentials_in_stream_url": True})
        try:
            assert _st(driver, "preview_url") == "rtsp://admin:secret@10.0.0.9:554/stream1"
            assert _child(driver, "profile", "profile_2", "snapshot_url") == "http://admin:secret@10.0.0.9:80/snapshot2.jpg"
        finally:
            await driver.disconnect()

    _run(scenario())


# ── PTZ ──────────────────────────────────────────────────────────────────────


def test_continuous_drive_moves_the_camera_and_poll_reads_it_back():
    async def scenario():
        driver, sim = await _connected_pair()
        try:
            await driver.send_command("pt_right", {"speed": 0.5})
            assert sim.get_state("pan_velocity") == 0.5
            assert sim.get_state("tilt_velocity") == 0.0
            sim.tick(0.5)
            await driver.poll()
            assert _st(driver, "pan_position") > 0.1
            assert _st(driver, "move_status") == "moving"
            await driver.send_command("pt_stop")
            assert sim.get_state("pan_velocity") == 0.0
            await driver.poll()
            assert _st(driver, "move_status") == "idle"

            await driver.send_command("pt_up_left", {"speed": 1.0})
            assert (sim.get_state("pan_velocity"), sim.get_state("tilt_velocity")) == (-1.0, 1.0)
            await driver.send_command("pt_drive", {"pan": 0.2, "tilt": -0.3, "zoom": 0.4})
            assert sim.get_state("zoom_velocity") == 0.4
            await driver.send_command("zoom_out", {"speed": 0.25})
            assert sim.get_state("zoom_velocity") == -0.25
            await driver.send_command("zoom_stop")
            assert sim.get_state("zoom_velocity") == 0.0
            assert sim.get_state("pan_velocity") == 0.2  # zoom_stop leaves pan/tilt alone
            await driver.send_command("pt_stop")
        finally:
            await driver.disconnect()

    _run(scenario())


def test_absolute_relative_and_home_moves():
    async def scenario():
        driver, sim = await _connected_pair()
        try:
            await driver.send_command("pt_absolute", {"pan": 0.5, "tilt": -0.25, "speed": 0.8})
            assert (sim.get_state("pan"), sim.get_state("tilt")) == (0.5, -0.25)
            await driver.send_command("zoom_absolute", {"zoom": 0.75})
            assert sim.get_state("zoom") == 0.75
            await driver.send_command("pt_relative", {"pan": -0.1, "tilt": 0.05})
            assert (sim.get_state("pan"), sim.get_state("tilt")) == (0.4, -0.2)
            await driver.send_command("zoom_relative", {"zoom": -0.25})
            assert sim.get_state("zoom") == 0.5
            await driver.send_command("set_home")
            await driver.send_command("pt_absolute", {"pan": 0.0, "tilt": 0.0})
            await driver.send_command("pt_home")
            assert (sim.get_state("pan"), sim.get_state("tilt"), sim.get_state("zoom")) == (0.4, -0.2, 0.5)
            await driver.poll()
            assert _st(driver, "pan_position") == 0.4
            assert _st(driver, "zoom_position") == 0.5
        finally:
            await driver.disconnect()

    _run(scenario())


def test_presets_save_recall_delete_and_the_reached_event():
    async def scenario():
        driver, sim = await _connected_pair()
        try:
            await driver.send_command("pt_absolute", {"pan": -0.5, "tilt": 0.5})
            token = await driver.send_command("preset_save", {"name": "Stage"})
            assert token == "3"
            assert _st(driver, "preset_count") == 3
            options = {o["value"]: o["label"] for o in json.loads(_st(driver, "preset_options"))}
            assert options["3"] == "Stage (3)"

            await driver.send_command("pt_home")
            await driver.send_command("preset_recall", {"preset": "Stage"})
            assert (sim.get_state("pan"), sim.get_state("tilt")) == (-0.5, 0.5)
            await _settle()
            assert _st(driver, "preset_status") == "reached"
            assert _st(driver, "preset_last") == "3"

            await driver.send_command("preset_recall", {"preset": "2"})
            assert sim.get_state("zoom") == 0.6

            await driver.send_command("preset_delete", {"preset": "3"})
            assert _st(driver, "preset_count") == 2
            with pytest.raises(DRV.OnvifCommandError) as exc_info:
                await driver.send_command("preset_recall", {"preset": "3"})
            assert "not on the camera" in str(exc_info.value)

            await driver.send_command("pt_right", {"speed": 0.5})
            with pytest.raises(DRV.OnvifCommandError) as exc_info:
                await driver.send_command("preset_save", {"name": "Moving"})
            assert "still moving" in str(exc_info.value)
            await driver.send_command("pt_stop")
        finally:
            await driver.disconnect()

    _run(scenario())


def test_aux_command_and_reboot():
    async def scenario():
        driver, sim = await _connected_pair()
        try:
            echoed = await driver.send_command("aux_command", {"command": "tt:Wiper|On"})
            assert echoed == "tt:Wiper|On"
            assert sim.get_state("last_aux") == "tt:Wiper|On"
            assert await driver.send_command("reboot") == "Rebooting"
            assert sim.get_state("rebooted") is True
        finally:
            await driver.disconnect()

    _run(scenario())


# ── Imaging ──────────────────────────────────────────────────────────────────


def test_imaging_settings_round_trip_and_range_refusal():
    async def scenario():
        driver, sim = await _connected_pair()
        try:
            await driver.set_device_setting("brightness", 70)
            assert sim.get_state("brightness") == 70.0
            assert _st(driver, "brightness") == 70.0
            with pytest.raises(DeviceSettingValueError) as exc_info:
                await driver.set_device_setting("brightness", 150)
            assert "between 0 and 100" in str(exc_info.value)
            assert sim.get_state("brightness") == 70.0

            await driver.set_device_setting("ir_cut_filter", "off")
            assert sim.get_state("ir_cut_filter") == "OFF"
            assert _st(driver, "ir_cut_filter") == "off"
            await driver.set_device_setting("exposure_mode", "manual")
            assert sim.get_state("exposure_mode") == "MANUAL"
            assert _st(driver, "exposure_mode") == "manual"
            await driver.set_device_setting("backlight_compensation", True)
            assert sim.get_state("backlight") == "ON"
            assert _st(driver, "backlight_compensation") is True
            await driver.set_device_setting("wide_dynamic_range", True)
            assert sim.get_state("wdr") == "ON"
            # A write sends the whole settings block back in schema order.
            assert sim.get_state("contrast") == 50.0

            await driver.send_command("imaging_preset_apply", {"preset": "Outdoor"})
            assert sim.get_state("imaging_preset") == "outdoor"
            assert _st(driver, "imaging_preset") == "outdoor"
            assert _st(driver, "ir_cut_filter") == "auto"
        finally:
            await driver.disconnect()

    _run(scenario())


def test_focus_drive_switches_to_manual_and_auto_hands_it_back():
    async def scenario():
        driver, sim = await _connected_pair()
        try:
            await driver.send_command("focus_near", {"speed": 0.5})
            assert sim.get_state("focus_velocity") == -0.5
            assert sim.get_state("focus_mode") == "MANUAL"
            sim.tick(0.4)
            await driver.poll()
            assert _st(driver, "focus_move_status") == "moving"
            assert _st(driver, "focus_position") < 0.5
            await driver.send_command("focus_stop")
            assert sim.get_state("focus_velocity") == 0.0
            await driver.send_command("focus_far", {"speed": 1.0})
            assert sim.get_state("focus_velocity") == 1.0
            await driver.send_command("focus_stop")
            await driver.send_command("focus_absolute", {"position": 0.9})
            assert sim.get_state("focus_position") == 0.9
            await driver.send_command("focus_auto")
            assert sim.get_state("focus_mode") == "AUTO"
            assert _st(driver, "focus_mode") == "auto"
            await driver.send_command("focus_manual")
            assert sim.get_state("focus_mode") == "MANUAL"
        finally:
            await driver.disconnect()

    _run(scenario())


# ── I/O and events ───────────────────────────────────────────────────────────


def test_relay_command_lands_and_comes_back_through_the_event():
    async def scenario():
        driver, sim = await _connected_pair()
        try:
            await driver.send_command("relay_on", {"relay": "relay_1"})
            assert sim.get_state("relay_1") is True
            await _settle()
            assert _child(driver, "relay", "relay_1", "active") is True
            await driver.send_command("relay_off", {"relay": "relay_1"})
            await _settle()
            assert _child(driver, "relay", "relay_1", "active") is False
            with pytest.raises(DRV.OnvifCommandError):
                await driver.send_command("relay_on", {"relay": "relay_9"})
        finally:
            await driver.disconnect()

    _run(scenario())


def test_input_motion_tamper_and_signal_loss_arrive_push_only():
    async def scenario():
        driver, sim = await _connected_pair()
        try:
            calls_before = len(sim.calls)
            sim.set_state("input_1", True)
            await _settle()
            assert _child(driver, "input", "input_1", "active") is True
            sim.set_state("motion", True)
            await _settle()
            assert _st(driver, "motion") is True
            sim.set_state("tamper_dark", True)
            await _settle()
            assert _st(driver, "tamper") is True
            assert _st(driver, "tamper_reason") == "dark"
            sim.set_state("tamper_dark", False)
            await _settle()
            assert _st(driver, "tamper") is False
            assert _st(driver, "tamper_reason") == ""
            sim.set_state("signal_loss", True)
            await _settle()
            assert _st(driver, "signal_loss") is True
            # Nothing but PullMessages was called while this happened.
            assert set(sim.calls[calls_before:]) == {"PullMessages"}
        finally:
            await driver.disconnect()

    _run(scenario())


def test_subscription_is_renewed_when_the_camera_grants_a_short_term():
    async def scenario():
        driver, sim = await _connected_pair({"max_term_s": 30})
        try:
            assert sim.renew_count >= 1
            assert _st(driver, "events_active") is True
        finally:
            await driver.disconnect()

    _run(scenario())


def test_lost_pull_point_is_resubscribed(monkeypatch):
    monkeypatch.setattr(DRV, "EVENT_RETRY_MIN_S", 0.02)

    async def scenario():
        driver, sim = await _connected_pair()
        try:
            assert sim.subscribe_count == 1
            sim.drop_pullpoints()
            await _settle(0.3)
            assert sim.subscribe_count == 2
            assert _st(driver, "events_active") is True
            sim.set_state("motion", True)
            await _settle()
            assert _st(driver, "motion") is True
        finally:
            await driver.disconnect()

    _run(scenario())


def test_events_can_be_turned_off_and_polling_still_works():
    async def scenario():
        driver, sim = await _connected_pair(None, {"events": False})
        try:
            assert _st(driver, "events_active") is False
            assert sim.subscribe_count == 0
            await driver.send_command("pt_absolute", {"pan": 0.3, "tilt": 0.3})
            await driver.poll()
            assert _st(driver, "pan_position") == 0.3
        finally:
            await driver.disconnect()

    _run(scenario())


def test_refresh_children_reconciles_the_rosters():
    async def scenario():
        driver, sim = await _connected_pair()
        try:
            summary = await driver.refresh_children()
            assert summary == {"profiles": 2, "relays": 1, "inputs": 1}
            assert driver.list_children("profile") == ["profile_1", "profile_2"]
        finally:
            await driver.disconnect()

    _run(scenario())


def test_polling_reports_a_camera_fault_in_last_error_not_as_offline():
    async def scenario():
        driver, sim = await _connected_pair()
        try:
            sim._ptz = False  # the camera stops answering PTZ mid-session
            await driver.poll()
            assert _st(driver, "connected") is True
            assert "ActionNotSupported" in _st(driver, "last_error")
        finally:
            await driver.disconnect()

    _run(scenario())
