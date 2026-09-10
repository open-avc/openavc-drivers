"""Driver + simulator tests for brightsign_player (BrightSign Local DWS).

Dual-proof round trip: the real driver is wired to the real simulator over an
in-memory httpx transport, so the simulator renders what a player sends and
the driver parses it, both sides asserted.

Covers:
  - the connect sequence: player information, the HDMI output roster (one
    output on an XD5, four on an XC4055), the Moka display probe, the clock,
    video mode, log level and DWS state, health;
  - HTTP Digest on every request, a wrong password as a typed auth_failed,
    an open player with a blank password, the polls' detail cadence;
  - HDMI power-save (Display Sleep / Wake) read back from the player, a
    refused output number;
  - reboot, the autorun and factory-reset variants, and the poll while the
    player is down;
  - the snapshot, sendCecX and its validation, the registry round trip, the
    supervisor log level as a device setting, the clock set and sync, the
    network diagnostics;
  - the Moka display: power actions, every display setting including the
    three-value white balance write, and the refusal on a plain player;
  - presentation UDP: the message and <variable>:<value> forms, the port
    from config, and the simulator's own UDP receiver;
  - a player error landing in last_error and the user's exception;
  - every declared command has a dispatch branch; actions and settings point
    at declared commands and state.

The driver is loaded with the ``openavc.*`` imports stubbed so the community
CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
import hashlib
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
DRIVER_PATH = REPO_ROOT / "streaming" / "brightsign_player.py"
SIM_PATH = REPO_ROOT / "streaming" / "brightsign_player_sim.py"


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

    def _handle_transport_disconnect(self):
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
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise
        interval = self.config.get("poll_interval", 0)
        if interval > 0:
            await self.start_polling(interval)

    async def disconnect(self):
        await self.stop_polling()
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


UDP_SENT: list[tuple[str, int, bytes]] = []


class _FakeUDPTransport:
    """Records what the driver would put on the wire."""

    def __init__(self, name="udp", **kwargs):
        self.name = name
        self.opened = False

    async def open(self, allow_broadcast=True, local_addr=None):
        self.opened = True

    async def send_to(self, data, host, port):
        assert self.opened
        UDP_SENT.append((host, port, data))

    async def close(self):
        self.opened = False


install_stubs(
    {"openavc.transport.udp": {"UDPTransport": _FakeUDPTransport}},
    base_driver=_FakeBaseDriver,
)
DRV = load_module("brightsign_player_under_test", DRIVER_PATH)
SIM = load_module("brightsign_player_sim_under_test", SIM_PATH)


# ── Harness ──────────────────────────────────────────────────────────────────


def _make(sim_config=None, driver_config=None):
    sim = SIM.BrightSignPlayerSimulator("bs-sim", sim_config or {})

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
        if isinstance(text, (dict, list)):
            return httpx.Response(status, json=text, headers=resp_headers)
        return httpx.Response(status, text=str(text), headers=resp_headers)

    cfg = {
        "host": "10.0.0.20", "port": 443, "ssl": True, "verify_ssl": False,
        "username": "admin", "password": "", "udp_port": 5000,
        "poll_interval": 0, "detail_poll_every": 3,
    }
    cfg.update(driver_config or {})
    driver = DRV.BrightSignPlayerDriver("sign1", cfg, StubState(), StubEvents())
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


async def _connected(sim_config=None, driver_config=None):
    driver, sim, handler = _make(sim_config, driver_config)
    await _connect(driver, handler)
    return driver, sim


def _run(coro):
    return asyncio.run(coro)


def _st(driver, key):
    return driver.state.get(f"device.{driver.device_id}.{key}")


def _child(driver, local_id, key):
    return driver.state.get(f"device.{driver.device_id}.hdmi_output.{local_id}.{key}")


@pytest.fixture(autouse=True)
def _clear_udp():
    UDP_SENT.clear()
    yield
    UDP_SENT.clear()


# ── Metadata / shape ─────────────────────────────────────────────────────────


def test_every_declared_command_has_a_dispatch_branch():
    declared = set(DRV.BrightSignPlayerDriver.DRIVER_INFO["commands"])
    assert set(DRV.BrightSignPlayerDriver._DISPATCH) == declared


def test_actions_and_settings_point_at_declared_things():
    info = DRV.BrightSignPlayerDriver.DRIVER_INFO
    assert info["transport"] == "http"
    for action in info["actions"]:
        assert action.get("command", action["id"]) in info["commands"]
    gates = {a["id"]: a.get("visible_when", {}).get("key") for a in info["actions"]}
    assert gates["display_power_on"] == "device.$id.display_control_supported"
    assert gates["display_power_standby"] == "device.$id.display_control_supported"
    assert gates["reboot"] is None
    for key, setting in info["device_settings"].items():
        assert setting["state_key"] in info["state_variables"], key
    for name, cmd in info["commands"].items():
        for pname, pdef in cmd["params"].items():
            if pdef.get("child_type"):
                assert pdef["child_type"] in info["child_entity_types"], (name, pname)


def test_discovery_is_hint_only_with_the_brightsign_oui():
    disc = DRV.BrightSignPlayerDriver.DRIVER_INFO["discovery"]
    assert disc["oui"] == ["90:ac:3f"]
    assert disc["hostname"] == ["^brightsign-"]
    assert "tcp_probe" not in disc and "port_open" not in disc


# ── Reply parsing ────────────────────────────────────────────────────────────


def test_unwrap_result_returns_the_result_and_raises_the_players_sentence():
    assert DRV.unwrap_result({"data": {"result": {"a": 1}}}) == {"a": 1}
    assert DRV.unwrap_result({"data": {"result": True}}) is True
    with pytest.raises(DRV.DwsError) as info:
        DRV.unwrap_result({"data": {"error": {"status": 400, "message": "Storage is already encrypted."}}}, http_status=400)
    assert "already encrypted" in str(info.value) and info.value.http_status == 400
    with pytest.raises(DRV.DwsError):
        DRV.unwrap_result({"data": {"result": {"success": False, "error": "no"}}})
    with pytest.raises(DRV.DwsError) as info:
        DRV.unwrap_result("Not Found", http_status=404)
    assert info.value.http_status == 404


def test_parse_info_reads_the_reference_example_shape():
    result = {
        "serial": "RE433D006644", "upTime": "30 minutes", "upTimeSeconds": 1832, "model": "XD1035",
        "FWVersion": "9.0.97", "bootVersion": "9.0.85", "family": "cobra",
        "power": {"result": {"battery": "absent", "source": "AC", "switch_mode": "hard"}},
        "poe": {"result": {"status": "inactive"}},
        "networking": {"result": {"description": "Test_player", "name": "XD5-RE433D006644"}},
        "hardware_features": {"cec": True, "wifi": False},
        "connectionType": "eth0",
        "ethernet": [{"interfaceName": "eth0", "IPv4": [{"address": "192.168.1.174", "mac": "90:AC:3F:2A:01:79", "internal": False}]}],
    }
    out = DRV.parse_info(result)
    assert out["serial"] == "RE433D006644" and out["model"] == "XD1035"
    assert out["firmware_version"] == "9.0.97" and out["uptime_seconds"] == 1832
    assert out["device_name"] == "XD5-RE433D006644" and out["power_source"] == "AC"
    assert out["ip_address"] == "192.168.1.174" and out["mac_address"] == "90:ac:3f:2a:01:79"
    assert out["cec_supported"] is True and out["wifi_present"] is False
    # The prose spelling of the firmware field works too.
    assert DRV.parse_info({"fwVersion": "9.1.2"})["firmware_version"] == "9.1.2"


def test_parse_video_mode_prefers_the_numbers_inside_mode():
    out = DRV.parse_video_mode({
        "isAutoMode": False, "name": "640x480x60p", "width": "640", "height": "480", "frames": "60", "scan": "p",
        "mode": {"colorDepth": "8bit", "colorSpace": "rgb", "frequency": 60, "height": 480, "width": 640, "interlaced": False},
    })
    assert out == {
        "video_mode": "640x480x60p", "video_mode_auto": False, "video_width": 640, "video_height": 480,
        "video_frame_rate": 60, "video_interlaced": False, "video_color_space": "rgb", "video_color_depth": "8bit",
    }


def test_parse_output_reads_status_and_active_mode():
    out = DRV.parse_output({
        "status": {"audioFormat": "PCM", "audioChannelCount": 2, "audioSampleRate": 48000, "eotf": "SDR (GAMMA)",
                   "outputPowered": True, "outputPresent": True, "unstable": False},
        "activeMode": {"modeName": "1920x1080x60p", "width": 1920, "height": 1080, "frequency": 60, "colorSpace": "rgb", "colorDepth": "8bit"},
        "configuredMode": {"modeName": "auto"}, "bestMode": "3840x2160x60p", "powerSaveStatus": False,
    })
    assert out["display_connected"] and out["display_powered"] and not out["signal_unstable"]
    assert out["mode"] == "1920x1080x60p" and out["configured_mode"] == "auto" and out["best_mode"] == "3840x2160x60p"
    assert out["width"] == 1920 and out["frame_rate"] == 60 and out["audio_sample_rate"] == 48000


def test_parse_diagnostics_keeps_the_players_verdicts():
    out = DRV.parse_diagnostics({"ethernet": {"diagnosis": "OK", "ok": True}, "wifi": {"diagnosis": "WiFi interface not present", "ok": False},
                                 "internet": {"diagnosis": "OK", "ok": True}})
    assert out["diag_ethernet_ok"] and not out["diag_wifi_ok"] and out["diag_internet"] == "OK"


# ── Connect ──────────────────────────────────────────────────────────────────


def test_connect_reads_identity_outputs_and_detail():
    async def scenario():
        driver, sim = await _connected()
        assert _st(driver, "connected") is True
        assert _st(driver, "serial") == SIM.SERIAL and _st(driver, "model") == "XD1035"
        assert _st(driver, "firmware_version") == "9.1.100"
        assert _st(driver, "device_name") == f"XD5-{SIM.SERIAL}"
        assert _st(driver, "ip_address") == "192.168.1.174"
        assert _st(driver, "health") == "active" and _st(driver, "health_time")
        assert _st(driver, "video_mode") == "1920x1080x60p" and _st(driver, "video_width") == 1920
        assert _st(driver, "log_level") == "info"
        assert _st(driver, "local_dws_enabled") is True
        assert _st(driver, "timezone") == "America/New_York" and "EST" in _st(driver, "player_time")
        assert _st(driver, "display_control_supported") is False
        # One HDMI output on an XD5.
        assert _st(driver, "output_count") == 1
        assert driver.list_children("hdmi_output") == [0]
        assert _child(driver, 0, "display_connected") is True
        assert _child(driver, 0, "display_powered") is True
        assert _child(driver, 0, "mode") == "1920x1080x60p"
        assert _child(driver, 0, "audio_format") == "PCM"
        assert _child(driver, 0, "power_save") is False
        assert _child(driver, 0, "label") == "HDMI 1"
        # The player's own "not found" for output 1 ended the roster.
        assert "GET /video/hdmi/output/1" in sim.calls
        assert "GET /video/hdmi/output/2" not in sim.calls
        await driver.disconnect()
        assert driver._client is None
    _run(scenario())


def test_four_output_player_registers_four_children():
    async def scenario():
        driver, sim = await _connected({"outputs": 4, "model": "XC4055"})
        assert _st(driver, "output_count") == 4
        assert driver.list_children("hdmi_output") == [0, 1, 2, 3]
        assert _child(driver, 1, "display_connected") is True
        assert _child(driver, 2, "display_connected") is False
        assert _child(driver, 3, "display_powered") is False
        assert _child(driver, 3, "online") is True
    _run(scenario())


def test_the_model_table_sizes_the_simulator_roster():
    sim = SIM.BrightSignPlayerSimulator("s", {})
    assert sim._outputs == 1
    sim = SIM.BrightSignPlayerSimulator("s", {"model": "XT2145"})
    sim.set_state("model", "XT2145")
    assert SIM.OUTPUT_COUNTS["XT2145"] == 2


# ── Authentication ───────────────────────────────────────────────────────────


def test_digest_login_is_sent_and_accepted():
    async def scenario():
        driver, sim = await _connected({"password": "Sup3r-secret!"}, {"password": "Sup3r-secret!"})
        assert _st(driver, "connected") is True
        assert _st(driver, "serial") == SIM.SERIAL
    _run(scenario())


def test_wrong_password_is_a_typed_auth_failure():
    async def scenario():
        driver, sim, handler = _make({"password": "Sup3r-secret!"}, {"password": "wrong"})
        with pytest.raises(ConnectionFaultError) as info:
            await _connect(driver, handler)
        assert info.value.fault_code == "auth_failed"
        assert "always admin" in str(info.value)
        assert _st(driver, "connected") is not True
        assert driver._client is None
    _run(scenario())


def test_blank_password_against_a_locked_player_names_the_default():
    async def scenario():
        driver, sim, handler = _make({"password": SIM.SERIAL}, {"password": ""})
        with pytest.raises(ConnectionFaultError) as info:
            await _connect(driver, handler)
        assert info.value.fault_code == "auth_failed"
        assert "serial number" in str(info.value)
    _run(scenario())


def test_the_simulator_verifies_the_digest_itself():
    sim = SIM.BrightSignPlayerSimulator("s", {"password": "pw"})
    status, _, headers = sim.handle_request("GET", "/api/v1/info", {}, "")
    assert status == 401 and headers["WWW-Authenticate"].startswith("Digest ")
    nonce = headers["WWW-Authenticate"].split('nonce="')[1].split('"')[0]
    ha1 = hashlib.md5(b"admin:BrightSign:pw").hexdigest()
    ha2 = hashlib.md5(b"GET:/api/v1/info").hexdigest()
    response = hashlib.md5(f"{ha1}:{nonce}:00000001:abc:auth:{ha2}".encode()).hexdigest()
    auth = (f'Digest username="admin", realm="BrightSign", nonce="{nonce}", uri="/api/v1/info", '
            f'qop=auth, nc=00000001, cnonce="abc", response="{response}"')
    status, body = sim.handle_request("GET", "/api/v1/info", {"Authorization": auth}, "")
    assert status == 200 and body["data"]["result"]["serial"] == SIM.SERIAL
    # A different user is refused even with the right password.
    bad = auth.replace('username="admin"', 'username="root"')
    assert sim.handle_request("GET", "/api/v1/info", {"Authorization": bad}, "")[0] == 401


# ── Polling ──────────────────────────────────────────────────────────────────


def test_poll_reads_health_and_outputs_every_time_and_detail_on_the_cadence():
    async def scenario():
        driver, sim = await _connected()
        sim.calls.clear()
        sim.set_state("output_0_powered", False)
        await driver.poll()
        assert _child(driver, 0, "display_powered") is False
        assert "GET /health" in sim.calls and "GET /video/hdmi/output/0" in sim.calls
        assert "GET /info" not in sim.calls
        sim.calls.clear()
        await driver.poll()
        await driver.poll()  # the third poll re-reads the detail
        assert "GET /info" in sim.calls and "GET /time" in sim.calls and "GET /video-mode" in sim.calls
    _run(scenario())


def test_poll_propagates_a_dead_link():
    async def scenario():
        driver, sim = await _connected()

        async def dead(request):
            raise httpx.ConnectError("boom", request=request)

        driver._client = httpx.AsyncClient(base_url="https://10.0.0.20:443", transport=httpx.MockTransport(dead))
        with pytest.raises(ConnectionError):
            await driver.poll()
        await driver._close_session()
    _run(scenario())


def test_liveness_probe_types_a_player_error_as_no_response():
    async def scenario():
        driver, sim = await _connected()
        sim._down_until = 10 ** 12
        with pytest.raises(ConnectionFaultError) as info:
            await driver._liveness_probe()
        assert info.value.fault_code == "no_response"
        sim._down_until = 0
        await driver._liveness_probe()
        await driver.disconnect()
    _run(scenario())


# ── HDMI power save ──────────────────────────────────────────────────────────


def test_display_sleep_and_wake_read_back_from_the_player():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("display_sleep", {"output": 0})
        assert sim.state["output_0_power_save"] is True
        assert _child(driver, 0, "power_save") is True
        assert _child(driver, 0, "display_powered") is False
        await driver.send_command("display_wake", {"output": "0"})
        assert sim.state["output_0_power_save"] is False
        assert _child(driver, 0, "power_save") is False
        assert _child(driver, 0, "display_powered") is True
        with pytest.raises(ValueError) as info:
            await driver.send_command("display_sleep", {"output": 1})
        assert "no HDMI output 1" in str(info.value)
        with pytest.raises(ValueError):
            await driver.send_command("display_sleep", {"output": 9})
    _run(scenario())


# ── Reboot ───────────────────────────────────────────────────────────────────


def test_reboot_variants_reach_the_player_and_the_poll_sees_it_down():
    async def scenario():
        driver, sim = await _connected({"reboot_downtime": 0.3})
        result = await driver.send_command("reboot")
        assert result["success"] is True and sim.state["last_reboot_kind"] == "reboot"
        # While it reboots the player answers nothing useful.
        await driver.poll()
        assert "503" in (_st(driver, "last_error") or "") or _st(driver, "last_error")
        await asyncio.sleep(0.35)
        await driver.send_command("reboot_disable_autorun")
        assert sim.state["last_reboot_kind"] == "autorun_disabled"
        await asyncio.sleep(0.35)
        sim._registry["html"]["custom"] = "x"
        await driver.send_command("factory_reset")
        assert sim.state["last_reboot_kind"] == "factory_reset"
        assert "custom" not in sim._registry["html"]
        assert sim.state["reboot_count"] == 3
    _run(scenario())


# ── Snapshot, CEC, registry, log level, clock, diagnostics ───────────────────


def test_snapshot_reports_the_file_without_the_thumbnail():
    async def scenario():
        driver, sim = await _connected()
        result = await driver.send_command("take_snapshot", {"width": 640, "height": 480})
        assert "remoteSnapshotThumbnail" not in result
        assert result["width"] == 640
        assert _st(driver, "last_snapshot_file").startswith("/sd/remote_snapshots/img-")
        assert _st(driver, "last_snapshot_file") == sim.state["last_snapshot_file"]
        assert _st(driver, "last_snapshot_time")
    _run(scenario())


def test_send_cec_validates_and_forwards_the_payload():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("send_cec", {"hex_command": "4F 36"})
        assert sim.state["last_cec_command"] == "4f36"
        for bad in ("", "4f3", "zz", "4f:3"):
            with pytest.raises(ValueError):
                await driver.send_command("send_cec", {"hex_command": bad})
    _run(scenario())


def test_registry_round_trip():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("read_registry_key", {"section": "networking", "key": "un"})
        assert _st(driver, "registry_value") == "XD5"
        assert _st(driver, "registry_section") == "networking" and _st(driver, "registry_key") == "un"
        await driver.send_command("set_registry_key", {"section": "html", "key": "use-brightsign-media-player", "value": "1"})
        assert sim._registry["html"]["use-brightsign-media-player"] == "1"
        assert _st(driver, "registry_value") == "1"
        await driver.send_command("delete_registry_key", {"section": "html", "key": "use-brightsign-media-player"})
        assert "use-brightsign-media-player" not in sim._registry["html"]
        assert (await driver.send_command("flush_registry"))["success"] is True
        with pytest.raises(ValueError) as info:
            await driver.send_command("read_registry_key", {"section": "html", "key": "nope"})
        assert "not found" in str(info.value)
        assert "not found" in _st(driver, "last_error")
        with pytest.raises(ValueError):
            await driver.send_command("read_registry_key", {"section": "", "key": "x"})
    _run(scenario())


def test_log_level_setting_writes_and_reads_back():
    async def scenario():
        driver, sim = await _connected()
        await driver.set_device_setting("log_level", "trace")
        assert sim.state["log_level"] == 3
        assert _st(driver, "log_level") == "trace"
        with pytest.raises(ValueError):
            await driver.set_device_setting("log_level", "loud")
        with pytest.raises(ValueError):
            await driver.set_device_setting("bogus", 1)
    _run(scenario())


def test_set_time_and_sync_time_move_the_players_clock():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("set_time", {"date": "2030-01-02", "time": "03:04"})
        assert sim.state["clock_offset_s"] > 0
        assert _st(driver, "player_time").startswith("2030-01-02 03:04")
        await driver.send_command("sync_time")
        assert abs(sim.state["clock_offset_s"]) <= 1
        with pytest.raises(ValueError):
            await driver.send_command("set_time", {"date": "2030/01/02", "time": "03:04"})
        with pytest.raises(ValueError):
            await driver.send_command("set_time", {"date": "2030-01-02", "time": "3pm"})
    _run(scenario())


def test_the_time_body_is_flat_like_brightsigns_own_cli():
    async def scenario():
        driver, sim = await _connected()
        seen = {}
        original = driver._request

        async def spy(method, path, **kw):
            if path == "/time" and method == "PUT":
                seen.update(kw.get("json_body") or {})
            return await original(method, path, **kw)

        driver._request = spy
        await driver.send_command("set_time", {"date": "2030-01-02", "time": "03:04:05", "apply_timezone": False})
        assert seen == {"date": "2030-01-02", "time": "03:04:05", "applyTimezone": False}
    _run(scenario())


def test_network_diagnostics_land_in_state():
    async def scenario():
        driver, sim = await _connected()
        await driver.send_command("run_network_diagnostics")
        assert _st(driver, "diag_ethernet_ok") is True and _st(driver, "diag_internet_ok") is True
        assert _st(driver, "diag_wifi") == "WiFi interface not present"
        sim.set_state("internet_ok", False)
        await driver.send_command("run_network_diagnostics")
        assert _st(driver, "diag_internet_ok") is False
        assert "failed" in _st(driver, "diag_internet")
    _run(scenario())


# ── Moka display ─────────────────────────────────────────────────────────────


def test_moka_display_is_detected_and_controlled():
    async def scenario():
        driver, sim = await _connected({"moka": True})
        assert _st(driver, "display_control_supported") is True
        assert _st(driver, "display_power") == "on" and _st(driver, "display_volume") == 50
        assert _st(driver, "display_contrast") == 45 and _st(driver, "display_video_output") == "HDMI1"
        assert _st(driver, "display_always_on") is False and _st(driver, "display_always_connected") is True
        assert _st(driver, "display_serial") == "1234567890"
        await driver.send_command("display_power_standby")
        assert sim.state["display_power"] == "standby" and _st(driver, "display_power") == "standby"
        await driver.send_command("display_power_on")
        assert _st(driver, "display_power") == "on"
        await driver.set_device_setting("display_volume", 80)
        assert sim.state["display_volume"] == 80 and _st(driver, "display_volume") == 80
        await driver.set_device_setting("display_brightness", 70)
        await driver.set_device_setting("display_contrast", 60)
        assert sim.state["display_brightness"] == 70 and _st(driver, "display_contrast") == 60
        await driver.set_device_setting("display_standby_timeout", 120)
        assert _st(driver, "display_standby_timeout") == 120
        await driver.set_device_setting("display_video_output", "HDMI2")
        assert sim.state["display_video_output"] == "HDMI2" and _st(driver, "display_video_output") == "HDMI2"
        await driver.set_device_setting("display_always_on", True)
        assert sim.state["display_always_on"] is True and _st(driver, "display_always_on") is True
        await driver.set_device_setting("display_always_connected", False)
        assert _st(driver, "display_always_connected") is False
        # White balance: one colour changes, the other two ride along unchanged.
        await driver.set_device_setting("display_white_balance_green", 90)
        assert (sim.state["display_wb_red"], sim.state["display_wb_green"], sim.state["display_wb_blue"]) == (120, 90, 120)
        assert _st(driver, "display_white_balance_green") == 90 and _st(driver, "display_white_balance_red") == 120
        # The display's own refusal reaches the caller.
        with pytest.raises(ValueError) as info:
            await driver.set_device_setting("display_volume", 500)
        assert "between 0 and 100" in str(info.value)
        # The slow poll re-reads the display.
        sim.set_state("display_volume", 33)
        for _ in range(3):
            await driver.poll()
        assert _st(driver, "display_volume") == 33
    _run(scenario())


def test_display_control_is_refused_on_a_plain_player():
    async def scenario():
        driver, sim = await _connected()
        with pytest.raises(ValueError) as info:
            await driver.send_command("display_power_on")
        assert "Moka" in str(info.value)
        with pytest.raises(ValueError):
            await driver.set_device_setting("display_volume", 10)
    _run(scenario())


# ── Presentation UDP ─────────────────────────────────────────────────────────


def test_udp_message_and_variable_go_to_the_configured_port():
    async def scenario():
        driver, sim = await _connected(driver_config={"udp_port": 5100})
        await driver.send_command("send_udp_message", {"message": "play_intro "})
        await driver.send_command("set_presentation_variable", {"name": "room", "value": "B 12"})
        # The platform's send_udp, one datagram each to the player's host.
        assert driver.udp_sent == [(b"play_intro ", "10.0.0.20", 5100), (b"room:B 12", "10.0.0.20", 5100)]
        with pytest.raises(ValueError):
            await driver.send_command("set_presentation_variable", {"name": "a:b", "value": "1"})
        with pytest.raises(ValueError):
            await driver.send_command("send_udp_message", {"message": ""})
    _run(scenario())


def test_udp_is_refused_until_a_port_is_set():
    async def scenario():
        driver, sim = await _connected(driver_config={"udp_port": 0})
        with pytest.raises(ValueError) as info:
            await driver.send_command("send_udp_message", {"message": "x"})
        assert "UDP Receiver Port" in str(info.value)
        assert driver.udp_sent == []
    _run(scenario())


def test_a_held_udp_port_does_not_stop_the_simulator():
    """The platform passes the device config to the simulator, so udp_port is
    the port the driver sends to; if something else holds it, the player must
    still answer HTTP."""
    import socket

    async def scenario():
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        sim = SIM.BrightSignPlayerSimulator("s", {"udp_port": port})
        started = {"http": False}

        async def fake_http_start(p):
            started["http"] = True

        sim.start_http_server = fake_http_start
        sim.stop_http_server = _noop
        try:
            await sim.start(19999)
            assert started["http"] and sim._udp_transport is None
            await sim.stop()
        finally:
            holder.close()
        # With the port free the receiver comes up and hears a datagram.
        sim2 = SIM.BrightSignPlayerSimulator("s2", {"udp_port": port})
        sim2.start_http_server = fake_http_start
        sim2.stop_http_server = _noop
        await sim2.start(19999)
        assert sim2._udp_transport is not None
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"room:B 12", ("127.0.0.1", port))
        sender.close()
        await asyncio.sleep(0.1)
        assert sim2.state["last_udp_message"] == "room:B 12"
        await sim2.stop()
    _run(scenario())


async def _noop(*args, **kwargs):
    return None


def test_the_simulator_records_a_presentation_udp_message():
    sim = SIM.BrightSignPlayerSimulator("s", {})
    sim.udp_received(b"room:B 12")
    assert sim.state["last_udp_message"] == "room:B 12"
    assert sim.udp_messages == ["room:B 12"]


# ── Errors and misc ──────────────────────────────────────────────────────────


def test_unknown_command_and_not_connected_are_refused():
    async def scenario():
        driver, sim, handler = _make()
        with pytest.raises(ConnectionError):
            await driver.send_command("reboot")
        await _connect(driver, handler)
        with pytest.raises(ValueError):
            await driver.send_command("levitate")
    _run(scenario())


def test_refresh_children_re_enumerates_the_outputs():
    async def scenario():
        driver, sim = await _connected()
        sim._outputs = 2
        result = await driver.refresh_children()
        assert result == {"hdmi_output": [0, 1]}
        assert driver.list_children("hdmi_output") == [0, 1]
        sim._outputs = 1
        await driver.refresh_children()
        assert driver.list_children("hdmi_output") == [0]
    _run(scenario())


def test_the_route_list_is_served():
    sim = SIM.BrightSignPlayerSimulator("s", {})
    status, body = sim.handle_request("GET", "/api/v1/", {}, "")
    routes = {r["route"] for r in body["data"]["result"]["routes"]}
    assert status == 200 and "/api/v1/health" in routes and "/api/v1/control/reboot" in routes
    assert sim.handle_request("GET", "/api/v1/nope", {}, "")[0] == 404
    assert sim.handle_request("PUT", "/api/v1/time", {}, "not json")[0] == 400
    assert json.loads(json.dumps(sim._info()))["serial"] == SIM.SERIAL
