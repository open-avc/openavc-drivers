"""Driver + simulator tests for epiphan_pearl (Epiphan Pearl over the REST API v2.0).

Dual-proof round trip: the real driver is wired to the real simulator over an
in-memory httpx transport, so the simulator renders what a Pearl sends and the
driver parses it, both sides asserted.

Covers:
  - the connect sequence: firmware, identity, channels with their publishers,
    recorders, inputs with a per-input schema built from the settings each one
    reports, outputs, storages, file upload, the one-touch control, presets,
    events, connectivity;
  - HTTP Basic on every request, a wrong password as a typed auth_failed, a
    blank host and a blank username refused before any request;
  - recording and streaming: the asynchronous starting -> started settle, an
    SRT listener's listening state, a disabled stream that will not start,
    the whole unit, a channel, one stream; the archive's newest file;
  - stream settings read without their secrets, rename, destination, stream
    key, delete, and adding RTMP, SRT and NDI streams;
  - layouts, channel rename, bookmarks refused while not recording;
  - inputs: analog, HDMI, SDI, SRT, RTSP schemas; the documented 405 for USB,
    NDI, web graphics and the second HDMI; mute and gain routed through the
    block each input reported; the generic setting write by name and by label;
    adding network inputs;
  - the HDMI output source and its options list; storage eject; file upload
    states; the one-touch toggle; CMS events through the alias identifiers and
    the ad-hoc session; presets; the speed test; reboot and shutdown;
  - previews: needs_setup until the RTSP port is entered, the Nano's fixed
    port, stream credentials embedded on request;
  - a transport error in poll propagating as ConnectionError, a device error
    landing in last_error, the slow-cadence reads, refresh_children, the
    liveness probe, and no secret ever reaching state.

The driver is loaded with the ``openavc.*`` imports stubbed so the community
CI stays self-contained (conftest.py rolls the stubs back).
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
    StubBaseDriver,
    StubEvents,
    StubState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "streaming" / "epiphan_pearl.py"
SIM_PATH = REPO_ROOT / "streaming" / "epiphan_pearl_sim.py"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect lifecycle for a driver that owns its
    session; state, children and the watchdog come from the shared stubs."""

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0

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

    async def disconnect(self):
        await self._stop_push()
        await self.stop_polling()
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


install_stubs(base_driver=_FakeBaseDriver)
DRV = load_module("epiphan_pearl_under_test", DRIVER_PATH)
SIM = load_module("epiphan_pearl_sim_under_test", SIM_PATH)


# ── Harness ──────────────────────────────────────────────────────────────────


def _make(sim_config=None, driver_config=None, *, fail_paths=None):
    sim = SIM.EpiphanPearlSimulator("pearl-sim", sim_config or {})
    fail_paths = fail_paths if fail_paths is not None else {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        headers = dict(request.headers)
        path = request.url.raw_path.decode("ascii")
        for needle, outcome in fail_paths.items():
            if needle in path:
                if isinstance(outcome, Exception):
                    raise outcome
                status, text = outcome
                return httpx.Response(status, text=text)
        result = sim.handle_request(request.method, path, headers, body)
        if len(result) == 3:
            status, text, resp_headers = result
        else:
            status, text = result
            resp_headers = {}
        return httpx.Response(status, text=str(text), headers=resp_headers)

    cfg = {
        "host": "10.0.0.9", "port": 80, "username": "admin", "password": "secret",
        "poll_interval": 0, "detail_poll_every": 6,
    }
    cfg.update(driver_config or {})
    driver = DRV.EpiphanPearlDriver("pearl1", cfg, StubState(), StubEvents())
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


async def _connected(sim_config=None, driver_config=None, **kw):
    driver, sim, handler = _make(sim_config, driver_config, **kw)
    await _connect(driver, handler)
    return driver, sim


def _run(coro):
    return asyncio.run(coro)


def _st(driver, key):
    return driver.state.get(f"device.{driver.device_id}.{key}")


def _child(driver, child_type, local_id, key):
    return driver.state.get(f"device.{driver.device_id}.{child_type}.{local_id}.{key}")


async def _polls(driver, n=1):
    for _ in range(n):
        await driver.poll()


@pytest.fixture(autouse=True)
def _instant_settle(monkeypatch):
    """Start operations settle on the next read unless a test says otherwise."""
    monkeypatch.setattr(SIM, "SETTLE_S", 0.0)


# ── Metadata / shape ─────────────────────────────────────────────────────────


def test_every_declared_command_has_a_dispatch_branch():
    declared = set(DRV.EpiphanPearlDriver.DRIVER_INFO["commands"])
    assert set(DRV.EpiphanPearlDriver._DISPATCH) == declared


def test_actions_pickers_and_child_params_reference_declared_things():
    info = DRV.EpiphanPearlDriver.DRIVER_INFO
    assert info["transport"] == "http"
    assert info["category"] == "streaming"
    for action in info["actions"]:
        assert action["command" if "command" in action else "id"] in info["commands"]
    child_types = set(info["child_entity_types"])
    for cname, cmd in info["commands"].items():
        for pname, param in cmd.get("params", {}).items():
            if param.get("type") == "child_id":
                assert param["child_type"] in child_types, (cname, pname)
            if "options_state" in param:
                assert param["options_state"] in info["state_variables"], (cname, pname)
            if "options_from" in param:
                assert param["options_from"]["param"] in cmd["params"], (cname, pname)
            if "type_from" in param:
                assert param["type_from"]["param"] in cmd["params"], (cname, pname)
    assert info["child_entity_types"]["input"]["dynamic"] is True
    for name, var in info["state_variables"].items():
        assert var.get("label"), name


def test_defaults_agree_with_the_schema():
    info = DRV.EpiphanPearlDriver.DRIVER_INFO
    for key, entry in info["config_schema"].items():
        if "default" in entry:
            assert info["default_config"][key] == entry["default"], key


# ── Connect ──────────────────────────────────────────────────────────────────


def test_connect_reads_identity_and_registers_every_roster():
    async def scenario():
        driver, sim = await _connected()
        assert _st(driver, "connected") is True
        assert _st(driver, "product_name") == "Pearl Mini"
        assert _st(driver, "product_id") == 44
        assert _st(driver, "firmware_version") == "4.24.1"
        assert _st(driver, "device_location") == "Room 101"
        assert set(driver.list_children("channel")) == {"1", "2"}
        assert set(driver.list_children("publisher")) == {"1-0", "1-1", "1-2", "2-0"}
        assert set(driver.list_children("recorder")) == {"1", "2", "3"}
        assert set(driver.list_children("input")) == {
            "hdmi-a", "hdmi-b", "sdi", "analog-a", "analog-b", "USBA", "RTSP1", "SRT1", "NDI1", "WEBG1",
        }
        assert set(driver.list_children("output")) == {"D1"}
        assert set(driver.list_children("storage")) == {"main", "external", "maintenance"}
        assert set(driver.list_children("afu")) == {"0"}
        assert set(driver.list_children("single_touch")) == {"0"}
        assert _st(driver, "channel_count") == 2
        assert _st(driver, "publisher_count") == 4
        assert _st(driver, "recorder_count") == 3
        assert _st(driver, "input_count") == 10
        assert _st(driver, "recording") is False
        assert _st(driver, "streaming") is False
        assert _child(driver, "channel", "1", "name") == "HDMI-A"
        assert _child(driver, "channel", "1", "label") == "HDMI-A"
        assert _child(driver, "channel", "1", "active_layout_name") == "Default"
        assert _child(driver, "channel", "1", "video_resolution") == "1920x1080"
        assert _child(driver, "channel", "1", "video_bitrate_kbps") == 3000
        assert _child(driver, "channel", "1", "audio_channels") == 2
        assert _child(driver, "channel", "1", "active_layout_sources") == "HDMI-A, HDMI-A Audio"
        assert _child(driver, "publisher", "1-1", "type") == "srt"
        assert _child(driver, "publisher", "1-1", "state") == "stopped"
        assert _child(driver, "publisher", "1-1", "is_configured") is True
        assert _child(driver, "recorder", "3", "multisource") is True
        assert _child(driver, "recorder", "1", "state") == "stopped"
        assert _child(driver, "output", "D1", "name") == "HDMI"
        assert _child(driver, "storage", "main", "state") == "ready"
        assert _child(driver, "storage", "main", "free_percent") == 75
        assert _child(driver, "single_touch", "0", "pressed") is False
        assert _child(driver, "single_touch", "0", "recorders_total") == 3
        assert _child(driver, "afu", "0", "state") == "idle"
        assert _child(driver, "afu", "0", "protocol") == "webdav"
        assert json.loads(_st(driver, "preset_options")) == ["Default", "Lecture", "Meeting"]
        assert _st(driver, "event_upcoming_title") == "Physics 101"
        assert _st(driver, "event_ongoing_title") == ""
        assert _st(driver, "cpu_load_percent") == 25
        assert _st(driver, "cpu_temp_high") is False
        assert _st(driver, "icmp_status") == "error"
        assert _st(driver, "mdns_name") == "GSAA495529"
        assert _st(driver, "adhoc_user_id") == ""
    _run(scenario())


def test_blank_host_and_blank_username_are_refused_before_any_request():
    async def scenario():
        driver, sim, handler = _make(driver_config={"host": ""})
        with pytest.raises(ConnectionFaultError) as exc:
            await _connect(driver, handler)
        assert exc.value.fault_code == "invalid_config"
        assert sim.calls == []
        driver, sim, handler = _make(driver_config={"username": ""})
        with pytest.raises(ConnectionFaultError) as exc:
            await _connect(driver, handler)
        assert exc.value.fault_code == "auth_failed"
        assert sim.calls == []
    _run(scenario())


def test_wrong_password_is_a_typed_auth_failure_and_the_right_one_connects():
    async def scenario():
        driver, sim, handler = _make({"require_auth": True, "password": "pearlpw"},
                                     {"password": "wrong"})
        with pytest.raises(ConnectionFaultError) as exc:
            await _connect(driver, handler)
        assert exc.value.fault_code == "auth_failed"
        assert "username and password" in str(exc.value)
        assert _st(driver, "connected") is not True
        driver, sim = await _connected({"require_auth": True, "password": "pearlpw"},
                                       {"password": "pearlpw"})
        assert _st(driver, "connected") is True
        # A blank password is attempted (older firmware allowed it) and the
        # refusal says so.
        driver, sim, handler = _make({"require_auth": True, "password": "pearlpw"}, {"password": ""})
        with pytest.raises(ConnectionFaultError) as exc:
            await _connect(driver, handler)
        assert "no password is entered" in str(exc.value)
    _run(scenario())


# ── Recording ────────────────────────────────────────────────────────────────


def test_recorder_start_settles_through_starting_and_the_archive_follows(monkeypatch):
    async def scenario():
        monkeypatch.setattr(SIM, "SETTLE_S", 0.2)
        driver, sim = await _connected()
        assert _child(driver, "recorder", "1", "latest_recording_name") == "HDMI-A_Dec11_17-32-30"
        await driver.send_command("start_recorder", {"recorder": "1"})
        await _polls(driver)
        assert _child(driver, "recorder", "1", "state") == "starting"
        assert _child(driver, "recorder", "1", "recording") is False
        await asyncio.sleep(0.25)
        await _polls(driver)
        assert _child(driver, "recorder", "1", "state") == "started"
        assert _child(driver, "recorder", "1", "recording") is True
        assert _child(driver, "recorder", "1", "active") == "1"
        assert _st(driver, "recording") is True
        assert _st(driver, "recorders_active") == 1
        await driver.send_command("stop_recorder", {"recorder": "1"})
        await _polls(driver)
        assert _child(driver, "recorder", "1", "state") == "stopped"
        assert _st(driver, "recording") is False
        # The stop marked the archive dirty; the detail cycle re-reads it and
        # the newest file (by creation time) is the one just finished.
        driver._poll_count = driver._detail_every - 1
        await _polls(driver)
        assert _child(driver, "recorder", "1", "latest_recording_name").startswith("HDMI-A_")
        assert _child(driver, "recorder", "1", "latest_recording_name") != "HDMI-A_Dec11_17-32-30"
        assert _child(driver, "recorder", "1", "latest_recording_in_progress") is False
    _run(scenario())


def test_start_and_stop_all_recorders():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("start_all_recorders", {})
        await _polls(driver)
        assert _st(driver, "recorders_active") == 3
        assert all(_child(driver, "recorder", rid, "state") == "started" for rid in ("1", "2", "3"))
        await driver.send_command("stop_all_recorders", {})
        await _polls(driver)
        assert _st(driver, "recorders_active") == 0
    _run(scenario())


def test_bookmark_is_refused_while_not_recording_and_accepted_while_recording():
    async def scenario():
        driver, sim = await _connected()
        with pytest.raises(ValueError) as exc:
            await driver.send_command("add_bookmark", {"channel": "1", "text": "Question"})
        assert "not being recorded" in str(exc.value)
        assert "not being recorded" in _st(driver, "last_error")
        await driver.send_command("start_recorder", {"recorder": "1"})
        await driver.send_command("add_bookmark", {"channel": "1", "text": "Question"})
        assert sim._recorders["1"]["bookmarks"] == ["Question"]
    _run(scenario())


# ── Streaming ────────────────────────────────────────────────────────────────


def test_publisher_start_stop_listener_state_and_statistics():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("start_publisher", {"publisher": "1-0"})
        await driver.send_command("start_publisher", {"publisher": "1-1"})
        await _polls(driver)
        assert _child(driver, "publisher", "1-0", "state") == "started"
        assert _child(driver, "publisher", "1-0", "started") is True
        assert _child(driver, "publisher", "1-1", "state") == "listening"
        assert _child(driver, "publisher", "1-1", "started") is True
        assert _child(driver, "channel", "1", "streaming") is True
        assert _child(driver, "channel", "1", "publishers_active") == 2
        assert _st(driver, "streaming") is True
        assert _st(driver, "publishers_active") == 2
        await driver.send_command("stop_publisher", {"publisher": "1-0"})
        await _polls(driver)
        assert _child(driver, "publisher", "1-0", "state") == "stopped"
        assert _child(driver, "publisher", "1-0", "started") is False
        assert _st(driver, "publishers_active") == 1
    _run(scenario())


def test_srt_caller_statistics_are_published():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("add_srt_publisher", {
            "channel": "2", "name": "To campus", "mode": "caller", "url": "srt://10.1.1.5:9000",
            "latency_ms": 200, "enabled": True,
        })
        assert "2-1" in driver.list_children("publisher")
        await driver.send_command("start_publisher", {"publisher": "2-1"})
        await _polls(driver)
        assert _child(driver, "publisher", "2-1", "state") == "started"
        assert _child(driver, "publisher", "2-1", "send_rate") == pytest.approx(3.61)
        assert _child(driver, "publisher", "2-1", "rtt") == 342
        assert _child(driver, "publisher", "2-1", "srt_latency_ms") == 200
    _run(scenario())


def test_channel_and_unit_wide_stream_control():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("start_channel_streams", {"channel": "1"})
        await _polls(driver)
        # Stream 1-2 is disabled on the device and does not start.
        assert _child(driver, "publisher", "1-0", "started") is True
        assert _child(driver, "publisher", "1-1", "started") is True
        assert _child(driver, "publisher", "1-2", "started") is False
        assert _child(driver, "publisher", "2-0", "started") is False
        await driver.send_command("start_all_streams", {})
        await _polls(driver)
        assert _child(driver, "publisher", "2-0", "started") is True
        await driver.send_command("stop_all_streams", {})
        await _polls(driver)
        assert _st(driver, "publishers_active") == 0
        await driver.send_command("stop_channel_streams", {"channel": "2"})
    _run(scenario())


def test_publisher_settings_are_read_without_secrets_and_enable_toggles():
    async def scenario():
        driver, sim = await _connected()
        await driver.refresh_children()
        assert _child(driver, "publisher", "1-0", "url") == "rtmp://192.168.86.51/live"
        assert _child(driver, "publisher", "1-0", "enabled") is True
        assert _child(driver, "publisher", "1-0", "single_touch") is True
        assert _child(driver, "publisher", "1-1", "srt_mode") == "listener"
        assert _child(driver, "publisher", "1-1", "srt_port") == 1029
        assert _child(driver, "publisher", "1-2", "enabled") is False
        assert _child(driver, "publisher", "1-2", "ndi_name") == "Pearl HDMI-A"
        await driver.send_command("start_publisher", {"publisher": "1-2"})
        await _polls(driver)
        assert _child(driver, "publisher", "1-2", "started") is False
        await driver.send_command("set_publisher_enabled", {"publisher": "1-2", "enabled": True})
        assert _child(driver, "publisher", "1-2", "enabled") is True
        await driver.send_command("start_publisher", {"publisher": "1-2"})
        await _polls(driver)
        assert _child(driver, "publisher", "1-2", "started") is True
        await driver.send_command("set_publisher_single_touch", {"publisher": "1-2", "included": True})
        assert sim._channels["1"]["publishers"]["2"]["settings"]["common"]["single_touch"] is True
        assert _child(driver, "publisher", "1-2", "single_touch") is True
    _run(scenario())


def test_publisher_rename_destination_key_and_delete():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("rename_publisher", {"publisher": "1-0", "name": "YouTube"})
        assert sim._channels["1"]["publishers"]["0"]["name"] == "YouTube"
        assert _child(driver, "publisher", "1-0", "name") == "YouTube"
        await driver.send_command("set_publisher_url", {"publisher": "1-0", "url": "rtmp://a.rtmp.youtube.com/live2"})
        assert sim._channels["1"]["publishers"]["0"]["settings"]["rtmp"]["url"] == "rtmp://a.rtmp.youtube.com/live2"
        assert _child(driver, "publisher", "1-0", "url") == "rtmp://a.rtmp.youtube.com/live2"
        await driver.send_command("set_rtmp_stream_key", {"publisher": "1-0", "stream_key": "abcd-1234"})
        assert sim._channels["1"]["publishers"]["0"]["settings"]["rtmp"]["stream"] == "abcd-1234"
        with pytest.raises(ValueError):
            await driver.send_command("set_rtmp_stream_key", {"publisher": "1-1", "stream_key": "x"})
        await driver.refresh_children()
        with pytest.raises(ValueError) as exc:
            await driver.send_command("set_publisher_url", {"publisher": "1-1", "url": "srt://x:1"})
        assert "listener" in str(exc.value)
        with pytest.raises(ValueError):
            await driver.send_command("set_publisher_url", {"publisher": "1-2", "url": "ndi://x"})
        await driver.send_command("delete_publisher", {"publisher": "1-2"})
        assert "1-2" not in driver.list_children("publisher")
        assert "2" not in sim._channels["1"]["publishers"]
        assert _child(driver, "channel", "1", "publisher_count") == 2
    _run(scenario())


def test_adding_streams_registers_them():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("add_rtmp_publisher", {
            "channel": "1", "name": "Facebook", "url": "rtmps://live-api-s.facebook.com:443/rtmp/",
            "stream_key": "FB-KEY", "enabled": True,
        })
        await driver.send_command("add_ndi_publisher", {"channel": "1", "name": "NDI out", "ndi_name": "Pearl 2"})
        await driver.send_command("add_srt_publisher", {
            "channel": "1", "name": "Listener", "mode": "listener", "port": 5000, "passphrase": "0123456789abc",
        })
        assert {"1-3", "1-4", "1-5"} <= set(driver.list_children("publisher"))
        assert _child(driver, "publisher", "1-3", "type") == "rtmp"
        assert _child(driver, "publisher", "1-3", "url") == "rtmps://live-api-s.facebook.com:443/rtmp/"
        assert _child(driver, "publisher", "1-4", "type") == "ndi"
        assert _child(driver, "publisher", "1-5", "srt_port") == 5000
        assert sim._channels["1"]["publishers"]["5"]["settings"]["srt"]["encryption"]["passphrase"] == "0123456789abc"
        with pytest.raises(ValueError):
            await driver.send_command("add_srt_publisher", {"channel": "1", "name": "Bad", "mode": "caller"})
        with pytest.raises(ValueError):
            await driver.send_command("add_srt_publisher", {"channel": "1", "name": "Bad", "mode": "listener",
                                                            "port": 5001, "passphrase": "short"})
    _run(scenario())


# ── Channels ─────────────────────────────────────────────────────────────────


def test_layout_switch_and_channel_rename():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("set_channel_layout", {"channel": "1", "layout": "2"})
        assert _child(driver, "channel", "1", "active_layout_id") == "2"
        assert _child(driver, "channel", "1", "active_layout_name") == "Picture in Picture"
        with pytest.raises(ValueError) as exc:
            await driver.send_command("set_channel_layout", {"channel": "1", "layout": "9"})
        assert "Layout not found" in str(exc.value)
        await driver.send_command("rename_channel", {"channel": "2", "name": "Lectern"})
        assert _child(driver, "channel", "2", "name") == "Lectern"
        assert sim._channels["2"]["name"] == "Lectern"
        assert json.loads(_st(driver, "output_source_options"))[0]["label"] == "Channel 1: HDMI-A"
    _run(scenario())


# ── Inputs ───────────────────────────────────────────────────────────────────


def test_inputs_carry_a_schema_built_from_what_each_one_reports():
    async def scenario():
        driver, sim = await _connected()
        analog = driver.get_child_schema("input", "analog-a")
        assert {"gain", "mute", "phantom_power", "stereo_pair", "channel_a_gain", "channel_b_mute", "audio_delay_ms"} <= set(analog)
        assert analog["gain"]["control"] is True
        assert analog["phantom_power"]["label"] == "Phantom Power (48 V)"
        assert _child(driver, "input", "analog-a", "gain") == 27
        assert _child(driver, "input", "analog-a", "phantom_power") is False
        assert _child(driver, "input", "analog-a", "settings_supported") is True
        assert _child(driver, "input", "analog-a", "type") == "embedded"
        assert _child(driver, "input", "analog-b", "input_type") == "RCA+3.5mm"
        hdmi = driver.get_child_schema("input", "hdmi-a")
        assert {"audio_mute", "hdmi_audio_delay_ms", "deinterlacing", "nosignal_timeout_s"} <= set(hdmi)
        assert "gain" not in hdmi
        assert _child(driver, "input", "hdmi-b", "settings_supported") is False
        assert "audio_mute" not in driver.get_child_schema("input", "hdmi-b")
        sdi = driver.get_child_schema("input", "sdi")
        assert "audio_mute" in sdi and "scaling" in sdi
        srt = driver.get_child_schema("input", "SRT1")
        assert {"srt_mode", "srt_latency_ms", "srt_port", "srt_key_length", "hwaccel_decoding"} <= set(srt)
        assert not any("passphrase" in k for k in srt)
        assert _child(driver, "input", "SRT1", "srt_port") == 1024
        rtsp = driver.get_child_schema("input", "RTSP1")
        assert "rtsp_url" in rtsp and "rtsp_transport" in rtsp
        assert not any("password" in k for k in rtsp)
        for unsupported in ("USBA", "NDI1", "WEBG1"):
            assert _child(driver, "input", unsupported, "settings_supported") is False
        assert _child(driver, "input", "USBA", "type") == "usb"
        assert _child(driver, "input", "WEBG1", "type") == "web-graphics"
        assert _child(driver, "input", "hdmi-a", "snapshot_url") == "http://10.0.0.9:80/api/v2.0/inputs/hdmi-a/preview?format=jpg"
    _run(scenario())


def test_mute_gain_and_delay_route_through_the_block_each_input_reported():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("mute_input", {"input": "analog-a"})
        assert sim._inputs["analog-a"]["settings"]["local_audio"]["mute"] is True
        assert _child(driver, "input", "analog-a", "mute") is True
        await driver.send_command("unmute_input", {"input": "analog-a"})
        assert _child(driver, "input", "analog-a", "mute") is False
        await driver.send_command("mute_input", {"input": "hdmi-a"})
        assert sim._inputs["hdmi-a"]["settings"]["hdmi"]["audio"]["mute"] is True
        assert _child(driver, "input", "hdmi-a", "audio_mute") is True
        await driver.send_command("mute_input", {"input": "sdi"})
        assert sim._inputs["sdi"]["settings"]["sdi"]["audio"]["mute"] is True
        with pytest.raises(ValueError) as exc:
            await driver.send_command("mute_input", {"input": "hdmi-b"})
        assert "no mute setting" in str(exc.value)
        await driver.send_command("set_input_gain", {"input": "analog-a", "gain": 31})
        assert sim._inputs["analog-a"]["settings"]["local_audio"]["gain"] == 31
        assert _child(driver, "input", "analog-a", "gain") == 31
        with pytest.raises(ValueError):
            await driver.send_command("set_input_gain", {"input": "hdmi-a", "gain": 1})
        await driver.send_command("set_input_audio_delay", {"input": "hdmi-a", "delay_ms": 40})
        assert sim._inputs["hdmi-a"]["settings"]["hdmi"]["audio"]["delay"] == 40
        assert _child(driver, "input", "hdmi-a", "hdmi_audio_delay_ms") == 40
        await driver.send_command("set_input_audio_delay", {"input": "analog-a", "delay_ms": -20})
        assert sim._inputs["analog-a"]["settings"]["audio"]["delay"] == -20
        with pytest.raises(ValueError):
            await driver.send_command("set_input_audio_delay", {"input": "analog-a", "delay_ms": 500})
    _run(scenario())


def test_generic_setting_write_by_name_and_by_label_with_typing():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("set_input_setting", {"input": "analog-a", "setting": "phantom_power", "value": "true"})
        assert sim._inputs["analog-a"]["settings"]["local_audio"]["phantom_power"] is True
        assert _child(driver, "input", "analog-a", "phantom_power") is True
        await driver.send_command("set_input_setting", {"input": "analog-a", "setting": "Phantom Power (48 V)", "value": "false"})
        assert sim._inputs["analog-a"]["settings"]["local_audio"]["phantom_power"] is False
        await driver.send_command("set_input_setting", {"input": "SRT1", "setting": "srt_latency_ms", "value": "150"})
        assert sim._inputs["SRT1"]["settings"]["srt"]["latency"] == 150
        assert _child(driver, "input", "SRT1", "srt_latency_ms") == 150
        await driver.send_command("set_input_setting", {"input": "analog-b", "setting": "input_type", "value": "3.5mm"})
        assert sim._inputs["analog-b"]["settings"]["local_audio"]["input_type"] == "3.5mm"
        await driver.send_command("set_input_setting", {"input": "analog-a", "setting": "channel_a_gain", "value": 12})
        assert sim._inputs["analog-a"]["settings"]["local_audio"]["channels"]["channelA"]["gain"] == 12
        with pytest.raises(ValueError):
            await driver.send_command("set_input_setting", {"input": "analog-a", "setting": "phantom_power", "value": "maybe"})
        with pytest.raises(ValueError):
            await driver.send_command("set_input_setting", {"input": "analog-b", "setting": "input_type", "value": "DANTE"})
        with pytest.raises(ValueError) as exc:
            await driver.send_command("set_input_setting", {"input": "hdmi-b", "setting": "audio_mute", "value": "true"})
        assert "has no setting" in str(exc.value)
        with pytest.raises(ValueError):
            await driver.send_command("set_input_setting", {"input": "nope", "setting": "gain", "value": 1})
    _run(scenario())


def test_adding_network_inputs():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("add_rtsp_input", {"name": "Lobby cam", "url": "rtsp://10.0.0.20/live",
                                                     "username": "u", "password": "p", "transport": "tcp"})
        assert "RTSP2" in driver.list_children("input")
        assert _child(driver, "input", "RTSP2", "rtsp_url") == "rtsp://10.0.0.20/live"
        assert _child(driver, "input", "RTSP2", "rtsp_transport") == "tcp"
        assert "rtsp_password" not in driver.get_child_schema("input", "RTSP2")
        await driver.send_command("add_srt_input", {"name": "Remote", "mode": "listener", "port": 7000, "latency_ms": 120})
        assert _child(driver, "input", "SRT2", "srt_port") == 7000
        assert sim._inputs["SRT2"]["settings"]["srt"]["encryption"] is None
        await driver.send_command("add_srt_input", {"name": "Remote 2", "mode": "caller", "url": "srt://10.0.0.30:7001",
                                                    "passphrase": "0123456789ab"})
        assert sim._inputs["SRT3"]["settings"]["srt"]["url"] == "srt://10.0.0.30:7001"
        await driver.send_command("add_ndi_input", {"name": "Studio", "ndi_name": "STUDIO (Cam 1)"})
        assert _child(driver, "input", "NDI2", "settings_supported") is False
        assert sim._inputs["NDI2"]["created"]["ndi"]["name"] == "STUDIO (Cam 1)"
        await driver.send_command("add_web_graphics_input", {"name": "Scores", "url": "https://example.com",
                                                             "resolution": "1920x1080", "fps": 10})
        assert sim._inputs["WEBG2"]["created"]["web_graphics"] == {"url": "https://example.com", "resolution": "1920x1080", "fps": 10}
        assert _st(driver, "input_count") == 15
        options = json.loads(_st(driver, "output_source_options"))
        assert {"value": "RTSP2", "label": "Input: Lobby cam"} in options
        with pytest.raises(ValueError):
            await driver.send_command("add_rtsp_input", {"name": "No URL"})
        # The device's own cap (a 409) is a refusal that names the reason.
        driver, sim = await _connected({"max_network_inputs": 4})
        with pytest.raises(ValueError) as exc:
            await driver.send_command("add_ndi_input", {"name": "One more", "ndi_name": "X"})
        assert "maximum number" in str(exc.value)
    _run(scenario())


# ── Outputs, storage, uploads, one-touch ─────────────────────────────────────


def test_output_source_and_its_options():
    async def scenario():
        driver, sim = await _connected()
        options = json.loads(_st(driver, "output_source_options"))
        values = [o["value"] for o in options]
        assert values[:2] == ["1", "2"]
        assert "hdmi-a" in values and "multiview" in values and "console" in values
        await driver.send_command("set_output_source", {"output": "D1", "source": "multiview"})
        assert sim._outputs["D1"]["source"] == "multiview"
        await driver.send_command("set_output_source", {"output": "D1", "source": "hdmi-a"})
        assert sim._outputs["D1"]["source"] == "hdmi-a"
        with pytest.raises(ValueError):
            await driver.send_command("set_output_source", {"output": "D1", "source": "nothing"})
        with pytest.raises(ValueError):
            await driver.send_command("set_output_source", {"output": "D9", "source": "1"})
    _run(scenario())


def test_storage_status_transfer_and_eject():
    async def scenario():
        driver, sim = await _connected()
        assert _child(driver, "storage", "external", "state") == "ready"
        assert _child(driver, "storage", "external", "transfer_state") == "completed"
        assert _child(driver, "storage", "external", "transfer_total_count") == 3
        assert _child(driver, "storage", "main", "transfer_state") is None
        assert _child(driver, "storage", "maintenance", "state") == "nodev"
        await driver.send_command("eject_storage", {"storage": "external"})
        assert _child(driver, "storage", "external", "state") == "nodev"
        with pytest.raises(ValueError) as exc:
            await driver.send_command("eject_storage", {"storage": "main"})
        assert "does not support eject" in str(exc.value)
        # The 405 on main's transfer status was learned once, not asked again.
        before = sim.calls.count("GET /system/storages/main/transfer/status")
        await driver.refresh_children()
        assert sim.calls.count("GET /system/storages/main/transfer/status") == before
    _run(scenario())


def test_file_upload_states():
    async def scenario():
        driver, sim = await _connected()
        sim.set_state("afu_state", "uploading")
        await _polls(driver)
        assert _child(driver, "afu", "0", "state") == "uploading"
        assert _child(driver, "afu", "0", "queue_files") == 1
        assert _child(driver, "afu", "0", "uploading_file") == "VGA.1736858583.HDMI-A.mp4"
        assert _child(driver, "afu", "0", "uploaded_bytes") == 42000000
        sim.set_state("afu_state", "error")
        await _polls(driver)
        assert _child(driver, "afu", "0", "state") == "error"
        assert _child(driver, "afu", "0", "error_message") == "Server refused the connection"
        sim.set_state("afu_state", "idle")
        await _polls(driver)
        assert _child(driver, "afu", "0", "error_message") == ""
    _run(scenario())


def test_single_touch_toggle_starts_and_stops_what_it_includes():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("toggle_single_touch", {"control": "0"})
        await _polls(driver)
        assert _child(driver, "single_touch", "0", "pressed") is True
        assert _child(driver, "single_touch", "0", "recorders_active") == 3
        assert _child(driver, "single_touch", "0", "publishers_total") == 3
        # 1-2 is not in the one-touch set (single_touch off); the other three are.
        assert _child(driver, "single_touch", "0", "publishers_active") == 3
        assert _child(driver, "single_touch", "0", "status") is True
        assert _child(driver, "publisher", "1-2", "started") is False
        assert _st(driver, "recording") is True
        assert _child(driver, "publisher", "2-0", "started") is True
        await driver.send_command("toggle_single_touch", {"control": "0"})
        await _polls(driver)
        assert _child(driver, "single_touch", "0", "pressed") is False
        assert _st(driver, "recording") is False
        assert _st(driver, "streaming") is False
    _run(scenario())


# ── Events, presets, system ──────────────────────────────────────────────────


def test_events_through_the_alias_identifiers():
    async def scenario():
        driver, sim = await _connected()
        with pytest.raises(ValueError):
            await driver.send_command("stop_ongoing_event", {})
        await driver.send_command("start_upcoming_event", {})
        assert _st(driver, "event_ongoing_title") == "Physics 101"
        assert _st(driver, "event_ongoing_status") == "running"
        assert _st(driver, "event_upcoming_title") == ""
        finish = _st(driver, "event_ongoing_finish")
        await driver.send_command("pause_event", {})
        assert _st(driver, "event_ongoing_status") == "paused"
        with pytest.raises(ValueError):
            await driver.send_command("pause_event", {})
        await driver.send_command("resume_event", {})
        assert _st(driver, "event_ongoing_status") == "running"
        await driver.send_command("extend_event", {"minutes": 5})
        assert _st(driver, "event_ongoing_finish") == finish + 300
        await driver.send_command("stop_ongoing_event", {})
        assert _st(driver, "event_ongoing_title") == ""
        assert _st(driver, "event_ongoing_status") == "finished"
        assert sim._events["782ec0f4bbcc48e2a42a44eea5e69dc5"]["status"] == "finished"
    _run(scenario())


def test_adhoc_session_and_event_creation():
    async def scenario():
        driver, sim = await _connected()
        with pytest.raises(ValueError) as exc:
            await driver.send_command("create_adhoc_event", {"cms": "kaltura", "title": "Office hours", "duration_minutes": 30})
        assert "No ad-hoc session" in str(exc.value)
        with pytest.raises(ValueError):
            await driver.send_command("adhoc_login", {"cms": "panopto", "user_id": "jdoe"})
        await driver.send_command("adhoc_login", {"cms": "kaltura", "user_id": "jdoe"})
        assert _st(driver, "adhoc_user_id") == "jdoe"
        assert _st(driver, "adhoc_user_name") == "User jdoe"
        assert _st(driver, "adhoc_session_expires") > 0
        result = await driver.send_command("create_adhoc_event", {
            "cms": "kaltura", "title": "Office hours", "duration_minutes": 30, "type": "vod-live",
            "description": "Weekly", "start_in_minutes": 0,
        })
        assert result["id"].startswith("adhoc")
        assert _st(driver, "event_ongoing_title") == "Office hours"
        assert sim._events[result["id"]]["finish"] - sim._events[result["id"]]["start"] == 1800
        await driver.send_command("create_adhoc_event", {
            "cms": "opencast", "title": "Later", "duration_minutes": 60, "start_in_minutes": 120,
        })
        # Two hours out, so Physics 101 (one hour out) is still the next one.
        assert _st(driver, "event_upcoming_title") == "Physics 101"
        assert any(e["title"] == "Later" and e["status"] == "scheduled" for e in sim._events.values())
        await driver.send_command("adhoc_logout", {})
        assert _st(driver, "adhoc_user_id") == ""
        await driver.send_command("adhoc_login", {"cms": "panopto", "user_id": "jdoe", "password": "pw"})
        assert _st(driver, "adhoc_user_id") == "jdoe"
        with pytest.raises(ValueError):
            await driver.send_command("create_adhoc_event", {"cms": "moodle", "title": "x", "duration_minutes": 5})
    _run(scenario())


def test_presets_list_and_apply():
    async def scenario():
        driver, sim = await _connected()
        result = await driver.send_command("apply_preset", {"preset": "Lecture"})
        assert result == {"reboot": True}
        assert sim._applied_preset == "Lecture"
        result = await driver.send_command("apply_preset", {"preset": "Meeting", "sections": "channels, sources"})
        assert result == {"reboot": False}
        with pytest.raises(ValueError) as exc:
            await driver.send_command("apply_preset", {"preset": "Nope"})
        assert "not found" in str(exc.value)
    _run(scenario())


def test_speed_test_and_system_status():
    async def scenario():
        driver, sim = await _connected()
        result = await driver.send_command("run_speed_test", {"mode": "downlink", "protocol": "udp", "timeout_s": 5})
        assert result["bandwidth"] == 91318568
        assert _st(driver, "speedtest_bandwidth_bps") == 91318568
        assert _st(driver, "speedtest_mode") == "downlink"
        assert _st(driver, "speedtest_protocol") == "udp"
        assert _st(driver, "speedtest_udp_loss") == 2
        sim.set_state("cpu_temp", 75)
        sim.set_state("cpu_load", 95)
        await _polls(driver)
        assert _st(driver, "cpu_temp_c") == 75
        assert _st(driver, "cpu_temp_high") is True
        assert _st(driver, "cpu_load_high") is True
        assert _st(driver, "uptime_seconds") >= 5490
    _run(scenario())


def test_reboot_and_shutdown_reach_the_device():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("reboot", {})
        assert sim.get_state("rebooted") is True
        await driver.send_command("shutdown", {})
        assert sim.get_state("shutdown") is True
        with pytest.raises(ValueError):
            await driver.send_command("no_such_command", {})
    _run(scenario())


# ── Previews ─────────────────────────────────────────────────────────────────


def test_channel_preview_needs_the_rtsp_port_until_it_is_entered():
    async def scenario():
        driver, sim = await _connected()
        assert _child(driver, "channel", "1", "preview_status") == "needs_setup"
        assert _child(driver, "channel", "1", "preview_setup_field") == "channel_rtsp_ports"
        assert "Channel RTSP Ports" in _child(driver, "channel", "1", "preview_status_detail")
        assert _child(driver, "channel", "1", "preview_url") == ""
        assert _child(driver, "channel", "1", "snapshot_url") == "http://10.0.0.9:80/api/v2.0/channels/1/preview?format=jpg"
        driver, sim = await _connected(driver_config={
            "channel_rtsp_ports": [{"channel": "1", "port": 554}, {"channel": "2", "port": "555"}],
        })
        assert _child(driver, "channel", "1", "preview_url") == "rtsp://10.0.0.9:554/stream.sdp"
        assert _child(driver, "channel", "1", "preview_format") == "rtsp"
        assert _child(driver, "channel", "1", "preview_status") == ""
        assert _child(driver, "channel", "2", "preview_url") == "rtsp://10.0.0.9:555/stream.sdp"
        driver, sim = await _connected(driver_config={
            "channel_rtsp_ports": [{"channel": "1", "port": 554}],
            "stream_username": "viewer", "stream_password": "p@ss w",
        })
        assert _child(driver, "channel", "1", "preview_url") == "rtsp://viewer:p%40ss%20w@10.0.0.9:554/stream.sdp"
        assert _child(driver, "channel", "1", "snapshot_url").startswith("http://viewer:p%40ss%20w@10.0.0.9:80/")
    _run(scenario())


def test_pearl_nano_has_one_channel_and_a_fixed_rtsp_port():
    async def scenario():
        driver, sim = await _connected({"model": "nano"})
        assert _st(driver, "product_name") == "Pearl Nano"
        assert driver.list_children("channel") == ["1"]
        assert driver.list_children("recorder") == ["1"]
        assert set(driver.list_children("input")) == {"hdmi", "sdi", "analog"}
        assert _child(driver, "channel", "1", "preview_url") == "rtsp://10.0.0.9:554/stream.sdp"
        assert _child(driver, "channel", "1", "preview_status") == ""
        assert _child(driver, "input", "hdmi", "settings_supported") is False
        assert "input_type" in driver.get_child_schema("input", "analog")
    _run(scenario())


# ── Poll contract, cadence, refresh, liveness, secrets ───────────────────────


def test_poll_transport_error_propagates_and_device_error_lands_in_last_error():
    async def scenario():
        driver, sim = await _connected()
        driver._client._transport = httpx.MockTransport(  # type: ignore[attr-defined]
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom", request=request))
        )
        with pytest.raises(ConnectionError):
            await driver.poll()
        failures: dict = {}
        driver, sim, handler = _make(fail_paths=failures)
        await _connect(driver, handler)
        failures["/recorders/status"] = (500, json.dumps({"status": "error", "message": "Recorder service down"}))
        await driver.poll()
        assert _st(driver, "connected") is True
        assert _st(driver, "last_error") == "Recorder service down"
        failures.clear()
        failures["/system/status"] = (401, "Unauthorized")
        with pytest.raises(ConnectionFaultError) as exc:
            await driver.poll()
        assert exc.value.fault_code == "auth_failed"
    _run(scenario())


def test_detail_reads_run_on_their_own_cadence():
    async def scenario():
        driver, sim = await _connected(driver_config={"detail_poll_every": 2})
        sim.calls.clear()
        await driver.poll()
        assert "GET /inputs" not in sim.calls
        assert "GET /recorders/status" in sim.calls
        await driver.poll()
        assert "GET /inputs" in sim.calls
        assert "GET /system/presets" in sim.calls
    _run(scenario())


def test_refresh_children_reconciles_and_reports_counts():
    async def scenario():
        driver, sim = await _connected()
        del sim._channels["2"]
        del sim._recorders["2"]
        sim._inputs["RTSP9"] = {"name": "Late", "real": "Late", "audio": True, "video": True, "type": "rtsp",
                                "settings": {"rtsp": {"url": "rtsp://x", "transport": "udp"}}}
        counts = await driver.refresh_children()
        assert counts == {"channels": 1, "publishers": 3, "recorders": 2, "inputs": 11, "outputs": 1, "storages": 3}
        assert "2" not in driver.list_children("channel")
        assert "2-0" not in driver.list_children("publisher")
        assert "RTSP9" in driver.list_children("input")
        assert _st(driver, "channel_count") == 1
    _run(scenario())


def test_liveness_probe_reads_the_firmware_version():
    async def scenario():
        driver, sim = await _connected()
        sim.calls.clear()
        await driver._liveness_probe()
        assert sim.calls == ["GET /system/firmware/version"]
        await driver.disconnect()
        with pytest.raises(ConnectionError):
            await driver._liveness_probe()
    _run(scenario())


def test_publisher_error_mode_is_reported():
    async def scenario():
        driver, sim = await _connected()
        sim.inject_error("publisher_error")
        await _polls(driver)
        assert _child(driver, "publisher", "1-0", "state") == "error"
        assert _child(driver, "publisher", "1-0", "state_detail") == "Connection refused by the RTMP server"
        sim.clear_error("publisher_error")
        await _polls(driver)
        assert _child(driver, "publisher", "1-0", "state") == "stopped"
        assert _child(driver, "publisher", "1-0", "state_detail") == ""
    _run(scenario())


def test_no_secret_reaches_state():
    async def scenario():
        driver, sim = await _connected()
        await driver.refresh_children()
        await driver.send_command("set_rtmp_stream_key", {"publisher": "1-0", "stream_key": "lecture-key-2"})
        await driver.send_command("add_rtsp_input", {"name": "Cam", "url": "rtsp://10.0.0.20/live",
                                                     "username": "u", "password": "camera-pw"})
        blob = json.dumps(driver.state.snapshot())
        for secret in ("lecture-key", "AnyPassphrase", "111111111111111", "camera-pw", "secret"):
            assert secret not in blob, secret
    _run(scenario())


def test_dotted_device_ids_become_safe_child_ids():
    async def scenario():
        driver, sim = await _connected()
        sim._inputs["D2P496187.hdmi-a"] = sim._inputs.pop("hdmi-a")
        await driver.refresh_children()
        assert "D2P496187_hdmi-a" in driver.list_children("input")
        assert "hdmi-a" not in driver.list_children("input")
        await driver.send_command("mute_input", {"input": "D2P496187_hdmi-a"})
        assert sim._inputs["D2P496187.hdmi-a"]["settings"]["hdmi"]["audio"]["mute"] is True
    _run(scenario())
