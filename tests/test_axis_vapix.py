"""Driver + simulator tests for axis_vapix (Axis cameras over VAPIX).

Dual-proof round trip: the real driver is wired to the real simulator over an
in-memory httpx transport, and its event WebSocket to the simulator's event
session over an in-memory socket, so the simulator renders what a camera
sends and the driver parses it, both sides asserted.

Covers:
  - the connect sequence: device information, API discovery, the Properties
    group, view areas with stream and snapshot addresses, stream profiles,
    optics capabilities and position, the DayNight configuration, the sensor
    and appearance parameters, the I/O port roster, the illuminator, overlays,
    audio, the clock, and the event stream with its stateful replay;
  - HTTP Digest on every request, a wrong password as a typed auth_failed, a
    blank login refused before any request, a Basic-only camera refused over
    HTTP and accepted over HTTPS;
  - the event WebSocket's Digest handshake and the wssession token alternative;
  - remote zoom and focus, the IR cut filter and the day/night event it fires,
    every device setting with the camera's own refusals, the I/O ports (set,
    pulse, input change as a push-only event), illuminators, overlays;
  - a PTZ model: continuous drive integrated over time, stop, absolute and
    relative moves, presets (recall, save, delete, home), a guard tour, the
    IR cut filter through ptz.cgi, and the optics commands refusing;
  - an older camera without the JSON APIs (Brand group, port.cgi, restart.cgi);
  - stream credentials embedded only on request; a mid-session camera error
    landing in last_error rather than offline.

The driver is loaded with the ``openavc.*`` and ``websockets`` imports stubbed
so the community CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

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
DRIVER_PATH = REPO_ROOT / "cameras" / "axis_vapix.py"
SIM_PATH = REPO_ROOT / "cameras" / "axis_vapix_sim.py"


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


class _FakeInvalidStatus(Exception):
    """websockets.exceptions.InvalidStatus: carries the handshake response."""

    def __init__(self, status_code, headers=None):
        super().__init__(f"server rejected WebSocket connection: HTTP {status_code}")
        self.response = SimpleNamespace(status_code=status_code, headers=headers or {})


_WS_EXC = ModuleType("websockets.exceptions")
_WS_EXC.InvalidStatus = _FakeInvalidStatus

install_stubs(
    {"websockets": {"connect": None, "exceptions": _WS_EXC},
     "websockets.exceptions": {"InvalidStatus": _FakeInvalidStatus}},
    base_driver=_FakeBaseDriver,
)
DRV = load_module("axis_vapix_under_test", DRIVER_PATH)
SIM = load_module("axis_vapix_sim_under_test", SIM_PATH)


# ── Harness ──────────────────────────────────────────────────────────────────


class _FakeServerEnd:
    """The simulator's end of the in-memory event socket: what it sends
    lands in the driver's incoming queue."""

    def __init__(self, incoming: asyncio.Queue):
        self._incoming = incoming

    async def send(self, text):
        await self._incoming.put(text)

    async def close(self):
        await self._incoming.put(None)


class _FakeSocket:
    """The driver's end: configure frames go to the simulator's session, its
    replies and notifications come out through recv / iteration."""

    def __init__(self, sim):
        self._sim = sim
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._client = sim.ws_open(_FakeServerEnd(self._incoming))
        self.closed = False

    async def send(self, text):
        reply = await self._sim.ws_message(self._client, text)
        await self._incoming.put(reply)

    async def recv(self):
        return await self._incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        item = await self._incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        if not self.closed:
            self.closed = True
            self._sim.ws_close(self._client)
            await self._incoming.put(None)  # unblock a reader


def _make(sim_config=None, driver_config=None, *, ws_challenge="digest"):
    sim = SIM.AxisVapixSimulator("cam-sim", sim_config or {})
    attempts = {"connect": 0, "urls": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        headers = dict(request.headers)
        path = request.url.raw_path.decode("ascii")
        result = sim.handle_request(request.method, path, headers, body)
        if len(result) == 3:
            status, text, resp_headers = result
        else:
            status, text = result
            resp_headers = {}
        return httpx.Response(status, text=str(text), headers=resp_headers)

    async def fake_connect(url, additional_headers=None, **kwargs):
        attempts["connect"] += 1
        attempts["urls"].append(url)
        path = url.split("/", 3)[-1]
        path = "/" + path
        headers = dict(additional_headers or {})
        if sim._require_auth and not sim.ws_authorized(path, headers):
            challenge = sim._challenge()[2]["WWW-Authenticate"]
            if ws_challenge != "digest":
                challenge = 'Basic realm="AXIS"'
            raise _FakeInvalidStatus(401, {"WWW-Authenticate": challenge})
        return _FakeSocket(sim)

    cfg = {
        "host": "10.0.0.9", "port": 80, "username": "root", "password": "secret",
        "poll_interval": 0,
    }
    cfg.update(driver_config or {})
    driver = DRV.AxisVapixDriver("cam1", cfg, StubState(), StubEvents())
    return driver, sim, handler, fake_connect, attempts


async def _connect(driver, handler, fake_connect):
    original = httpx.AsyncClient

    def _mocked(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = _mocked  # type: ignore[assignment]
    DRV.websockets.connect = fake_connect
    try:
        await driver.connect()
    finally:
        httpx.AsyncClient = original  # type: ignore[assignment]
    await _settle()


async def _settle(seconds: float = 0.08):
    await asyncio.sleep(seconds)


async def _connected(sim_config=None, driver_config=None, **kw):
    driver, sim, handler, fake_connect, attempts = _make(sim_config, driver_config, **kw)
    await _connect(driver, handler, fake_connect)
    return driver, sim, attempts


def _run(coro):
    return asyncio.run(coro)


def _st(driver, key):
    return driver.state.get(f"device.{driver.device_id}.{key}")


def _child(driver, child_type, local_id, key):
    return driver.state.get(f"device.{driver.device_id}.{child_type}.{local_id}.{key}")


# ── Metadata / shape ─────────────────────────────────────────────────────────


def test_every_declared_command_has_a_dispatch_branch():
    declared = set(DRV.AxisVapixDriver.DRIVER_INFO["commands"])
    assert set(DRV.AxisVapixDriver._DISPATCH) == declared


def test_actions_name_declared_commands_and_hide_per_capability():
    info = DRV.AxisVapixDriver.DRIVER_INFO
    assert info["transport"] == "http"
    for action in info["actions"]:
        assert action["command"] in info["commands"]
    gates = {a["id"]: a.get("visible_when", {}).get("key") for a in info["actions"]}
    assert gates == {
        "ir_cut_auto": "device.$id.ir_cut_supported",
        "ir_cut_on": "device.$id.ir_cut_supported",
        "ir_cut_off": "device.$id.ir_cut_supported",
        "autofocus": "device.$id.focus_supported",
        "pt_home": "device.$id.ptz_supported",
        "pt_stop": "device.$id.ptz_supported",
        "reboot": None,
    }
    # Every device setting reads back from a declared state variable.
    for key, setting in info["device_settings"].items():
        assert setting["state_key"] in info["state_variables"], key


def test_digest_authorization_matches_rfc2617():
    import hashlib
    import re

    challenge = 'Digest realm="AXIS_X", nonce="abc", algorithm=MD5, qop="auth"'
    header = DRV._digest_authorization(challenge, "GET", "/vapix/ws-data-stream?sources=events", "root", "pw")
    fields = {k: v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', header[7:])}
    ha1 = hashlib.md5(b"root:AXIS_X:pw").hexdigest()
    ha2 = hashlib.md5(b"GET:/vapix/ws-data-stream?sources=events").hexdigest()
    expected = hashlib.md5(f"{ha1}:abc:{fields['nc']}:{fields['cnonce']}:auth:{ha2}".encode()).hexdigest()
    assert fields["response"] == expected
    assert fields["uri"] == "/vapix/ws-data-stream?sources=events"


# ── Connect ──────────────────────────────────────────────────────────────────


def test_connect_reads_everything_the_fixed_dome_offers():
    async def scenario():
        driver, sim, attempts = await _connected({"require_auth": True})
        try:
            assert _st(driver, "connected") is True
            assert _st(driver, "model") == "P3265-V"
            assert _st(driver, "product_name") == "AXIS P3265-V Dome Camera"
            assert _st(driver, "firmware_version") == "10.12.165"
            assert _st(driver, "serial_number") == SIM.SERIAL
            assert "optics-control" in _st(driver, "api_list")
            # View areas as children with the three addresses; the device
            # level mirrors view area 1, with no login in any of them.
            assert driver.list_children("view") == [1, 2]
            assert _child(driver, "view", 1, "preview_url") == "rtsp://10.0.0.9/axis-media/media.amp?camera=1"
            assert _child(driver, "view", 1, "preview_format") == "rtsp"
            assert _child(driver, "view", 2, "snapshot_url") == "http://10.0.0.9:80/axis-cgi/jpg/image.cgi?camera=2"
            assert _child(driver, "view", 2, "mjpeg_url") == "http://10.0.0.9:80/axis-cgi/mjpg/video.cgi?camera=2"
            assert _child(driver, "view", 2, "resolution") == "640x360+128+64"
            assert _st(driver, "preview_url") == "rtsp://10.0.0.9/axis-media/media.amp?camera=1"
            assert "@" not in _st(driver, "preview_url")
            assert json.loads(_st(driver, "stream_profile_options")) == ["Quality", "Bandwidth"]
            # Optics.
            assert _st(driver, "zoom_supported") is True
            assert _st(driver, "focus_supported") is True
            assert _st(driver, "ir_cut_supported") is True
            assert _st(driver, "magnification") == 1.0
            assert _st(driver, "max_magnification") == 2.4
            assert _st(driver, "focus_position") == 0.5
            assert _st(driver, "ir_cut_filter") == "auto"
            # Day/night configuration.
            assert _st(driver, "day_night_shift_level") == 50
            assert _st(driver, "day_night_autotune") is True
            assert _st(driver, "night_filter") == "clear"
            # Sensor + appearance parameters.
            assert _st(driver, "brightness") == 50
            assert _st(driver, "wdr") is True
            assert _st(driver, "exposure_mode") == "auto"
            assert _st(driver, "white_balance") == "auto"
            assert _st(driver, "backlight_compensation") is False
            assert _st(driver, "rotation") == 0
            assert _st(driver, "mirror") is False
            assert _st(driver, "overlays_shown") == "all"
            # I/O ports.
            assert driver.list_children("port") == ["0", "1"]
            assert _child(driver, "port", "0", "direction") == "input"
            assert _child(driver, "port", "1", "direction") == "output"
            assert _child(driver, "port", "1", "active") is False
            assert _child(driver, "port", "1", "normal_state") == "open"
            assert _st(driver, "port_count") == 2
            # Illuminator, overlays, audio, clock.
            assert driver.list_children("light") == ["led0"]
            assert _child(driver, "light", "led0", "light_type") == "IR"
            assert _child(driver, "light", "led0", "on") is False
            assert driver.list_children("overlay") == []
            assert [o["value"] for o in json.loads(_st(driver, "overlay_image_options"))] == [
                "/etc/overlays/axis(128x44).ovl", "/etc/overlays/logo.ovl",
            ]
            assert _st(driver, "audio_supported") is True
            assert _st(driver, "audio_enabled") is False
            assert _st(driver, "audio_input_gain") == 0.0
            assert abs(_st(driver, "clock_offset_s")) < 2
            assert _st(driver, "time_zone") == "America/New_York"
            # Events: the stream opened through the Digest handshake (one
            # refused attempt, one accepted) and replayed the stateful events.
            assert _st(driver, "events_active") is True
            assert attempts["connect"] == 2
            assert sim.configure_count == 1
            assert _st(driver, "day_mode") is True
            assert _st(driver, "system_ready") is True
            # A fixed dome: digital PTZ exists but ships turned off.
            assert _st(driver, "ptz_digital") is True
            assert _st(driver, "ptz_enabled") is False
            assert _st(driver, "ptz_supported") is False
            assert _st(driver, "ptz_driver") == "PTZ disabled"
            assert _st(driver, "ptz_ready") is False
            # An empty card slot is storage disruption, not a hardware fault.
            assert _st(driver, "storage_fault") is True
            assert _st(driver, "storage_fault_detail") == "NetworkShare, SD_DISK"
            assert _st(driver, "hardware_fault") is None
            assert _st(driver, "scene_change") is False
            # View areas: only the first is turned on in the camera.
            assert _child(driver, "view", 1, "enabled") is True
            assert _child(driver, "view", 2, "enabled") is False
            assert _child(driver, "view", 2, "online") is False
            # Turned off in the camera is an empty slot, not a fault the IDE should banner.
            assert _child(driver, "view", 2, "offline_reason") == "not_fitted"
            assert "View area 2 is turned off" in _child(driver, "view", 2, "offline_detail")
            assert _child(driver, "view", 1, "offline_reason") is None
            assert _st(driver, "motion") is False
            assert _st(driver, "manual_trigger") is False
            assert "secret" in driver.redacted_secrets
        finally:
            await driver.disconnect()
        assert _st(driver, "connected") is False
        assert _st(driver, "events_active") is False
        assert sim.get_state("ws_clients") == 0

    _run(scenario())


def test_wrong_password_is_a_typed_auth_failure():
    async def scenario():
        driver, sim, handler, fake_connect, _ = _make({"require_auth": True}, {"password": "wrong"})
        with pytest.raises(ConnectionFaultError) as exc_info:
            await _connect(driver, handler, fake_connect)
        assert exc_info.value.fault_code == "auth_failed"
        assert "ONVIF" in str(exc_info.value)
        assert _st(driver, "connected") is not True

    _run(scenario())


def test_blank_login_is_refused_before_any_request():
    async def scenario():
        driver, sim, handler, fake_connect, _ = _make({"require_auth": True}, {"username": "", "password": ""})
        with pytest.raises(ConnectionFaultError) as exc_info:
            await _connect(driver, handler, fake_connect)
        assert exc_info.value.fault_code == "auth_failed"
        assert "System > Accounts" in str(exc_info.value)
        assert sim.calls == []

    _run(scenario())


def test_basic_only_camera_is_refused_over_http_and_followed_over_https():
    async def scenario():
        driver, sim, handler, fake_connect, _ = _make({"require_auth": True, "auth_mode": "basic"})
        with pytest.raises(ConnectionFaultError) as exc_info:
            await _connect(driver, handler, fake_connect)
        assert exc_info.value.fault_code == "invalid_config"
        assert "HTTPS" in str(exc_info.value)

        driver, sim, attempts = await _connected(
            {"require_auth": True, "auth_mode": "basic"}, {"ssl": True, "port": 443},
        )
        try:
            assert _st(driver, "connected") is True
            assert isinstance(driver._auth, httpx.BasicAuth)
            assert _st(driver, "preview_url") == "rtsp://10.0.0.9/axis-media/media.amp?camera=1"
            assert _st(driver, "snapshot_url") == "https://10.0.0.9:443/axis-cgi/jpg/image.cgi?camera=1"
        finally:
            await driver.disconnect()

    _run(scenario())


def test_event_stream_falls_back_to_the_session_token():
    async def scenario():
        driver, sim, attempts = await _connected({"require_auth": True}, ws_challenge="basic")
        try:
            assert _st(driver, "events_active") is True
            assert attempts["connect"] == 2
            assert "wssession=" in attempts["urls"][-1]
            assert "/axis-cgi/wssession.cgi" in sim.calls
        finally:
            await driver.disconnect()

    _run(scenario())


def test_credentials_embedded_only_on_request():
    async def scenario():
        driver, sim, _ = await _connected(None, {"credentials_in_stream_url": True, "stream_profile": "Quality"})
        try:
            assert _st(driver, "preview_url") == "rtsp://root:secret@10.0.0.9/axis-media/media.amp?camera=1&streamprofile=Quality"
            assert _st(driver, "snapshot_url").startswith("http://root:secret@10.0.0.9:80/")
            assert _child(driver, "view", 2, "mjpeg_url").startswith("http://root:secret@")
        finally:
            await driver.disconnect()

    _run(scenario())


# ── Optics, IR cut, day/night ────────────────────────────────────────────────


def test_zoom_and_focus_move_the_lens_and_read_back():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True})
        try:
            await driver.send_command("zoom_set", {"magnification": 2.0})
            assert sim.get_state("magnification") == 2.0
            assert _st(driver, "magnification") == 2.0
            with pytest.raises(DRV.VapixCommandError, match="1 to 2.4"):
                await driver.send_command("zoom_set", {"magnification": 5})
            await driver.send_command("zoom_in", {"step": "small"})
            assert _st(driver, "magnification") == pytest.approx(2.05)
            await driver.send_command("zoom_out", {"amount": 0.5})
            assert _st(driver, "magnification") == pytest.approx(1.55)
            await driver.send_command("zoom_in", {})
            assert _st(driver, "magnification") == pytest.approx(1.75)
            await driver.send_command("focus_set", {"position": 0.3})
            assert sim.get_state("focus_position") == 0.3
            await driver.send_command("focus_far", {"step": "small"})
            assert _st(driver, "focus_position") == pytest.approx(0.32)
            await driver.send_command("focus_near", {"step": "big"})
            assert _st(driver, "focus_position") == pytest.approx(0.22)
            await driver.send_command("autofocus", {})
            assert _st(driver, "focus_position") == 0.5
            await driver.send_command("focus_window", {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5})
            with pytest.raises(DRV.VapixCommandError):
                await driver.send_command("focus_window", {"x": 2, "y": 0, "width": 1, "height": 1})
            await driver.send_command("optics_reset", {"zoom": True, "focus": False})
            assert _st(driver, "magnification") == 1.0
            await driver.send_command("optics_calibrate", {})
        finally:
            await driver.disconnect()

    _run(scenario())


def test_ir_cut_filter_switches_and_the_day_night_event_follows():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True})
        try:
            await driver.send_command("ir_cut_off", {})
            await _settle()
            assert sim.get_state("ir_cut_filter") == "off"
            assert _st(driver, "ir_cut_filter") == "off"
            assert _st(driver, "day_mode") is False
            await driver.send_command("ir_cut_on", {})
            await _settle()
            assert _st(driver, "day_mode") is True
            await driver.set_device_setting("ir_cut_filter", "auto")
            assert sim.get_state("ir_cut_filter") == "auto"
            assert _st(driver, "ir_cut_filter") == "auto"
            # The camera's own day/night switch reaches the driver as an event.
            sim.set_state("day_mode", False)
            await _settle()
            assert _st(driver, "day_mode") is False
        finally:
            await driver.disconnect()

    _run(scenario())


def test_device_settings_write_through_and_the_camera_refuses_bad_values():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True})
        try:
            await driver.set_device_setting("brightness", 65)
            assert sim._param("ImageSource.I0.Sensor.Brightness") == "65"
            assert _st(driver, "brightness") == 65
            with pytest.raises(DeviceSettingValueError):
                await driver.set_device_setting("brightness", 150)
            await driver.set_device_setting("wdr", False)
            assert sim._param("ImageSource.I0.Sensor.WDR") == "off"
            assert _st(driver, "wdr") is False
            await driver.set_device_setting("exposure_mode", "flickerfree60")
            assert _st(driver, "exposure_mode") == "flickerfree60"
            with pytest.raises(DeviceSettingValueError):
                await driver.set_device_setting("exposure_mode", "flicker")
            await driver.set_device_setting("backlight_compensation", True)
            assert sim._param("ImageSource.I0.Sensor.BacklightCompensation") == "yes"
            await driver.set_device_setting("white_balance", "fixed_indoor")
            assert _st(driver, "white_balance") == "fixed_indoor"
            await driver.set_device_setting("rotation", "180")
            assert sim._param("ImageSource.I0.Rotation") == "180"
            assert _st(driver, "rotation") == 180
            with pytest.raises(DeviceSettingValueError):
                await driver.set_device_setting("rotation", 45)
            await driver.set_device_setting("mirror", True)
            assert sim._param("Image.I0.Appearance.MirrorEnabled") == "yes"
            assert _st(driver, "mirror") is True
            await driver.set_device_setting("overlays_shown", "text")
            assert _st(driver, "overlays_shown") == "text"
            # Day/night configuration through daynight.cgi, with its rules.
            await driver.set_device_setting("day_night_shift_level", 70)
            assert sim._daynight["DayNightShiftLevel"] == 70
            assert _st(driver, "day_night_shift_level") == 70
            with pytest.raises(DeviceSettingValueError, match="Autotune"):
                await driver.set_device_setting("night_day_shift_level", 40)
            await driver.set_device_setting("day_night_autotune", False)
            await driver.set_device_setting("night_day_shift_level", 40)
            assert _st(driver, "night_day_shift_level") == 40
            await driver.set_device_setting("night_day_dwell_time", 20)
            assert sim._daynight["NightDayDwellTime"] == 20
            with pytest.raises(DeviceSettingValueError):
                await driver.set_device_setting("night_filter", "irpass")
            # Audio.
            await driver.set_device_setting("audio_enabled", True)
            assert sim._param("Audio.A0.Enabled") == "yes"
            assert _st(driver, "audio_enabled") is True
            await driver.set_device_setting("audio_input_gain", 6)
            assert sim._param("AudioSource.A0.InputGain") == "6"
            assert _st(driver, "audio_input_gain") == 6.0
        finally:
            await driver.disconnect()

    _run(scenario())


def test_settings_a_camera_lacks_are_refused_with_a_sentence():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True, "audio": False, "optics": False, "lights": False})
        try:
            assert _st(driver, "audio_supported") is False
            assert _st(driver, "light_count") == 0
            assert driver.list_children("light") == []
            before = sim.calls.count("/axis-cgi/lightcontrol.cgi")
            await driver.poll()
            assert sim.calls.count("/axis-cgi/lightcontrol.cgi") == before  # asked once, not every poll
            with pytest.raises(DRV.VapixCommandError, match="Pick an illuminator"):
                await driver.send_command("light_on", {"light": "led0"})
            with pytest.raises(DeviceSettingValueError, match="no audio"):
                await driver.set_device_setting("audio_enabled", True)
            assert _st(driver, "zoom_supported") is False
            with pytest.raises(DRV.VapixCommandError, match="no remote zoom"):
                await driver.send_command("zoom_set", {"magnification": 2})
            # Without optics control the IR cut filter is the DayNight parameter.
            assert _st(driver, "ir_cut_supported") is True
            await driver.send_command("ir_cut_off", {})
            assert sim._param("ImageSource.I0.DayNight.IrCutFilter") == "no"
            assert _st(driver, "ir_cut_filter") == "off"
        finally:
            await driver.disconnect()

    _run(scenario())


# ── I/O, illuminators, overlays ──────────────────────────────────────────────


def test_ports_set_pulse_and_input_events():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True})
        try:
            await driver.send_command("port_on", {"port": "1"})
            assert sim._ports["1"]["state"] == "closed"
            assert _child(driver, "port", "1", "active") is True
            assert _child(driver, "port", "1", "state") == "closed"
            await driver.send_command("port_off", {"port": "1"})
            assert _child(driver, "port", "1", "active") is False
            with pytest.raises(DRV.VapixCommandError, match="input"):
                await driver.send_command("port_on", {"port": "0"})
            await driver.send_command("port_pulse", {"port": "1", "duration": 60})
            await _settle(0.02)
            assert sim._ports["1"]["state"] == "closed"
            await _settle(0.1)
            assert sim._ports["1"]["state"] == "open"
            # The output event arrived both times, so the child tracked the pulse.
            assert _child(driver, "port", "1", "active") is False
            # A wired input toggling reaches the driver push-only (polling is off).
            sim.set_state("input_0", True)
            await _settle()
            assert _child(driver, "port", "0", "active") is True
            sim.set_state("input_0", False)
            await _settle()
            assert _child(driver, "port", "0", "active") is False
            # Virtual inputs and the manual trigger.
            await driver.send_command("virtual_input_on", {"input": SIM.MANUAL_TRIGGER_NBR})
            await _settle()
            assert _st(driver, "manual_trigger") is True
            await driver.send_command("virtual_input_off", {"input": SIM.MANUAL_TRIGGER_NBR})
            await _settle()
            assert _st(driver, "manual_trigger") is False
        finally:
            await driver.disconnect()

    _run(scenario())


def test_analytics_tamper_and_stream_events_arrive_push_only():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True})
        try:
            sim.set_state("motion", True)
            await _settle()
            assert _st(driver, "motion") is True
            assert _st(driver, "motion_source") == "CameraApplicationPlatform/VMD/Camera1ProfileANY"
            sim.set_state("motion", False)
            await _settle()
            assert _st(driver, "motion") is False
            sim.set_state("tamper", True)
            await _settle()
            assert _st(driver, "tamper_count") == 1
            assert _st(driver, "tamper_last")
            assert sim.get_state("tamper") is False
            sim.set_state("stream_accessed", True)
            await _settle()
            assert _st(driver, "stream_accessed") is True
        finally:
            await driver.disconnect()

    _run(scenario())


def test_illuminator_commands_round_trip():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True})
        try:
            await driver.send_command("light_on", {"light": "led0"})
            assert sim.get_state("light_on") is True
            assert _child(driver, "light", "led0", "on") is True
            assert _child(driver, "light", "led0", "intensity") == 50
            await driver.send_command("light_intensity", {"light": "led0", "intensity": 30})
            assert _child(driver, "light", "led0", "intensity") == 30
            assert _child(driver, "light", "led0", "auto_intensity") is False
            with pytest.raises(DRV.VapixCommandError, match="0 to 100"):
                await driver.send_command("light_intensity", {"light": "led0", "intensity": 120})
            await driver.send_command("light_auto_intensity", {"light": "led0", "enabled": True})
            assert _child(driver, "light", "led0", "auto_intensity") is True
            await driver.send_command("light_off", {"light": "led0"})
            assert _child(driver, "light", "led0", "on") is False
            with pytest.raises(DRV.VapixCommandError, match="Pick an illuminator"):
                await driver.send_command("light_on", {"light": "led9"})
        finally:
            await driver.disconnect()

    _run(scenario())


def test_overlays_add_change_and_remove():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True})
        try:
            identity = await driver.send_command("overlay_add_text", {
                "text": "Room 101", "position": "bottomRight", "font_size": 24, "text_color": "white",
                "background_color": "black",
            })
            assert identity == 1  # the camera numbers overlays from 1
            assert driver.list_children("overlay") == ["1"]
            assert _child(driver, "overlay", "1", "kind") == "text"
            assert _child(driver, "overlay", "1", "text") == "Room 101"
            assert _child(driver, "overlay", "1", "position") == "bottomRight"
            assert _child(driver, "overlay", "1", "font_size") == 24
            assert _child(driver, "overlay", "1", "background_color") == "black"
            await driver.send_command("overlay_set_text", {"overlay": "1", "text": "Room 102"})
            assert sim._overlays[1]["text"] == "Room 102"
            assert _child(driver, "overlay", "1", "text") == "Room 102"
            await driver.send_command("overlay_set_position", {"overlay": "1", "position": "top"})
            assert _child(driver, "overlay", "1", "position") == "top"
            image = await driver.send_command("overlay_add_image", {"image": "/etc/overlays/logo.ovl",
                                                                    "position": "bottomLeft"})
            assert image == 2
            assert _child(driver, "overlay", "2", "kind") == "image"
            assert _child(driver, "overlay", "2", "image_path") == "/etc/overlays/logo.ovl"
            with pytest.raises(DRV.VapixCommandError, match="image"):
                await driver.send_command("overlay_set_text", {"overlay": "2", "text": "x"})
            with pytest.raises(DRV.VapixCommandError):
                await driver.send_command("overlay_add_image", {"image": "missing.ovl"})
            await driver.send_command("overlay_remove", {"overlay": "1"})
            assert driver.list_children("overlay") == ["2"]
            counts = await driver.refresh_children()
            assert counts == {"views": 2, "ports": 2, "lights": 1, "overlays": 1}
        finally:
            await driver.disconnect()

    _run(scenario())


# ── PTZ model ────────────────────────────────────────────────────────────────


def test_ptz_model_drives_presets_and_guard_tours():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True, "ptz": True})
        try:
            assert _st(driver, "ptz_supported") is True
            assert _st(driver, "ptz_driver") == "Axis PTZ driver"
            assert _st(driver, "zoom_supported") is False
            assert _st(driver, "ir_cut_supported") is True
            assert _st(driver, "pan_position") == 0.0
            assert _st(driver, "zoom_level") == 1
            assert _st(driver, "autofocus") is True
            assert _st(driver, "preset_count") == 2
            options = json.loads(_st(driver, "preset_options"))
            assert options == [{"value": "1", "label": "Home (1)"}, {"value": "2", "label": "Lectern (2)"}]
            assert json.loads(_st(driver, "guard_tour_options")) == [{"value": "G0", "label": "Lobby sweep"}]
            assert _st(driver, "ptz_ready") is True
            with pytest.raises(DRV.VapixCommandError, match="no remote zoom"):
                await driver.send_command("zoom_set", {"magnification": 2})
            # Continuous drive integrates over time; stop reads the position back.
            await driver.send_command("pt_drive", {"pan": 50, "tilt": 0})
            sim.tick(1.0)
            await driver.send_command("pt_stop", {})
            assert _st(driver, "pan_position") == pytest.approx(30.0, abs=0.01)
            assert sim._velocity == [0.0, 0.0, 0.0]
            await driver.send_command("pt_right", {"speed": 20})
            assert sim._velocity[0] == 20.0
            await driver.send_command("pt_stop", {})
            await driver.send_command("pt_absolute", {"pan": 10, "tilt": -5, "speed": 80})
            assert _st(driver, "pan_position") == 10.0
            assert _st(driver, "tilt_position") == -5.0
            await driver.send_command("pt_relative", {"pan": 5, "tilt": 0})
            assert _st(driver, "pan_position") == 15.0
            await driver.send_command("ptz_zoom_absolute", {"zoom": 3000})
            assert _st(driver, "zoom_level") == 3000
            await driver.send_command("ptz_zoom_relative", {"zoom": -500})
            assert _st(driver, "zoom_level") == 2500
            await driver.send_command("ptz_zoom_in", {"speed": 40})
            assert sim._velocity[2] == 40.0
            await driver.send_command("ptz_zoom_stop", {})
            assert sim._velocity[2] == 0.0
            await driver.send_command("ptz_focus_near", {})
            assert _st(driver, "autofocus") is False or sim.get_state("autofocus") is False
            await driver.send_command("ptz_focus_stop", {})
            await driver.send_command("ptz_autofocus_on", {})
            assert _st(driver, "autofocus") is True
            # Presets by name and by number, with the reached event.
            await driver.send_command("preset_recall", {"preset": "Lectern"})
            await _settle()
            assert sim.get_state("pan") == 30.0
            assert _st(driver, "preset_last") == "Lectern (2)"
            await driver.send_command("preset_recall", {"preset": "1"})
            assert sim.get_state("pan") == 0.0
            await driver.send_command("pt_absolute", {"pan": 45, "tilt": 0})
            await driver.send_command("preset_save", {"name": "Stage"})
            assert _st(driver, "preset_count") == 3
            assert sim._presets["3"]["pan"] == 45.0
            await driver.send_command("preset_delete", {"preset": "Stage"})
            assert _st(driver, "preset_count") == 2
            with pytest.raises(DRV.VapixCommandError):
                await driver.send_command("preset_recall", {"preset": "Nowhere"})
            await driver.send_command("set_home", {})
            assert sim._presets[sim._home]["pan"] == 45.0
            await driver.send_command("pt_home", {})
            assert sim.get_state("pan") == 45.0
            await driver.send_command("center", {"x": 1280, "y": 540, "width": 1920, "height": 1080})
            assert sim.get_state("pan") == pytest.approx(55.0)
            await driver.send_command("ptz_zoom_absolute", {"zoom": 2500})
            await driver.send_command("area_zoom", {"x": 960, "y": 540, "zoom": 200})
            assert sim.get_state("zoom") == 5000
            # Guard tour through the GuardTour parameters.
            await driver.send_command("guard_tour_start", {"tour": "Lobby sweep"})
            assert sim._param("GuardTour.G0.Running") == "yes"
            assert _st(driver, "guard_tour_running") == "Lobby sweep"
            await driver.send_command("guard_tour_stop", {})
            assert _st(driver, "guard_tour_running") == ""
            # IR cut filter through ptz.cgi on a PTZ model.
            await driver.send_command("ir_cut_off", {})
            await _settle()
            assert sim._param("PTZ.Various.V1.IrCutFilter") == "off"
            assert _st(driver, "ir_cut_filter") == "off"
            assert _st(driver, "day_mode") is False
            await driver.send_command("aux_command", {"function": "wiper"})
            assert sim.get_state("last_aux") == "wiper"
        finally:
            await driver.disconnect()

    _run(scenario())


# ── Older firmware, events off, mid-session errors ───────────────────────────


def test_older_camera_without_the_json_apis():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True, "legacy": True})
        try:
            assert _st(driver, "model") == "P3265-V"
            assert _st(driver, "product_name") == "AXIS P3265-V Dome Camera"
            assert _st(driver, "firmware_version") == "10.12.165"
            assert _st(driver, "serial_number") == SIM.SERIAL
            # No API discovery: what the Properties group reveals is all there is.
            assert _st(driver, "api_list") == "light-control, ptz-control"
            assert "/axis-cgi/apidiscovery.cgi" in sim.calls
            assert driver._port_mgmt is False
            assert driver.list_children("port") == ["0", "1"]
            assert _child(driver, "port", "1", "direction") == "output"
            await driver.send_command("port_on", {"port": "1"})
            assert sim._ports["1"]["state"] == "closed"
            assert _child(driver, "port", "1", "active") is True
            await driver.send_command("port_off", {"port": "1"})
            assert _child(driver, "port", "1", "active") is False
            await driver.send_command("reboot", {})
            assert sim.get_state("rebooted") is True
            assert "/axis-cgi/restart.cgi" in sim.calls
        finally:
            await driver.disconnect()

    _run(scenario())


def test_events_off_still_polls_the_ports():
    async def scenario():
        driver, sim, attempts = await _connected({"require_auth": True}, {"events": False})
        try:
            assert _st(driver, "events_active") is False
            assert attempts["connect"] == 0
            sim._ports["0"]["state"] = "closed"
            assert _child(driver, "port", "0", "active") is False
            await driver.poll()
            assert _child(driver, "port", "0", "active") is True
        finally:
            await driver.disconnect()

    _run(scenario())


def test_a_camera_error_mid_session_lands_in_last_error_not_offline():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True})
        try:
            sim._optics = False  # the optics CGI answers 404 from now on
            await driver.poll()
            assert _st(driver, "connected") is True
            assert "404" in _st(driver, "last_error")
            await driver.send_command("reboot", {})
            assert sim.get_state("rebooted") is True
            assert "/axis-cgi/firmwaremanagement.cgi" in sim.calls
        finally:
            await driver.disconnect()

    _run(scenario())



def test_digital_ptz_setting_turns_the_fixed_dome_ptz_on_and_off():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True})
        try:
            with pytest.raises(DRV.VapixCommandError, match="no pan/tilt/zoom"):
                await driver.send_command("pt_home", {})
            await driver.set_device_setting("digital_ptz", True)
            assert sim._param("PTZ.ImageSource.I0.PTZEnabled") == "true"
            assert sim._param("PTZ.Various.V1.Locked") == "false"
            assert _st(driver, "ptz_enabled") is True
            assert _st(driver, "ptz_supported") is True
            assert _st(driver, "ptz_driver") == "Digital PTZ"
            await driver.send_command("pt_absolute", {"pan": 12, "tilt": -3})
            assert _st(driver, "pan_position") == 12.0
            assert _st(driver, "tilt_position") == -3.0
            await driver.send_command("ptz_zoom_absolute", {"zoom": 4000})
            assert _st(driver, "zoom_level") == 4000
            await driver.set_device_setting("digital_ptz", False)
            assert _st(driver, "ptz_supported") is False
            assert _st(driver, "ptz_driver") == "PTZ disabled"
            with pytest.raises(DRV.VapixCommandError, match="no pan/tilt/zoom"):
                await driver.send_command("pt_absolute", {"pan": 0, "tilt": 0})
        finally:
            await driver.disconnect()

    _run(scenario())


def test_without_the_daynight_api_the_shift_level_is_the_sensor_parameter():
    async def scenario():
        driver, sim, _ = await _connected({"require_auth": True, "daynight_api": False})
        try:
            assert "daynight" not in _st(driver, "api_list")
            assert _st(driver, "day_night_shift_level") == 50
            assert _st(driver, "day_night_dwell_time") is None
            await driver.set_device_setting("day_night_shift_level", 80)
            assert sim._param("ImageSource.I0.DayNight.ShiftLevel") == "80"
            assert _st(driver, "day_night_shift_level") == 80
            with pytest.raises(DeviceSettingValueError, match="only the day to night level"):
                await driver.set_device_setting("day_night_dwell_time", 5)
            # The IR cut filter still goes through the optics API on this camera.
            await driver.send_command("ir_cut_off", {})
            assert sim.get_state("ir_cut_filter") == "off"
        finally:
            await driver.disconnect()

    _run(scenario())
