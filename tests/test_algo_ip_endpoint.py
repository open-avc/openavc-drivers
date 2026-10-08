"""Driver + simulator tests for algo_ip_endpoint (Algo IP endpoints over the
RESTful API).

Dual-proof round trip: the real driver's httpx client is wired to the real
simulator through httpx.MockTransport, so every request the driver signs is
verified by the simulator the way the device verifies it, and both sides are
asserted.

Covers:
  - the Standard method's signature against the worked examples in Algo's
    RESTful API Guide (the HMAC input, both sample requests, the body MD5);
  - connect: identification from the About page and the per-model narrowing
    of commands, settings, actions and strobe patterns, for a display, a
    speaker, a visual alerter, the 8190S, a paging adapter, the 8063 and the
    8450; firmware too old for the About page keeps the full set;
  - every command each model offers, against the simulator, with the device's
    state read back; strobe brightness on both scales; Stop Tone naming the
    tone on old firmware;
  - device settings written and read back, with the 3 dB step refused;
  - faults: a refused password, a device clock outside the 30-second window
    (named in the message), Basic and None, a method that does not match the
    device, the RESTful API off, a password changed mid-session, a command
    refused for old firmware, poll propagating transport errors.

Loads the driver and simulator with the ``openavc.*`` imports stubbed so the
community CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import re
import time
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
DRIVER_PATH = REPO_ROOT / "audio" / "algo_ip_endpoint.py"
SIM_PATH = REPO_ROOT / "audio" / "algo_ip_endpoint_sim.py"


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
DRV = load_module("algo_ip_endpoint_under_test", DRIVER_PATH)
SIM = load_module("algo_ip_endpoint_sim_under_test", SIM_PATH)

CLASS = DRV.AlgoIpEndpointDriver
INFO = CLASS.DRIVER_INFO


# ── Harness ──────────────────────────────────────────────────────────────────


class _Link:
    def __init__(self, sim):
        self.sim = sim
        self.reachable = True
        self.requests: list[tuple[str, str, dict, bytes]] = []


def _make_handler(link: _Link):
    def handler(request: httpx.Request) -> httpx.Response:
        if not link.reachable:
            raise httpx.ConnectError("Connection refused")
        path = request.url.path
        headers = dict(request.headers)
        link.requests.append((request.method, path, headers, request.content))
        body = request.content.decode("utf-8") if request.content else ""
        status, resp_body, resp_headers = link.sim.handle_request(request.method, path, headers, body)
        if isinstance(resp_body, dict):
            return httpx.Response(status, json=resp_body, headers=resp_headers)
        return httpx.Response(status, text=str(resp_body), headers=resp_headers)

    return handler


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


def _make(sim_config=None, driver_config=None):
    sim_cfg = {"password": "algo"}
    sim_cfg.update(sim_config or {})
    sim = SIM.AlgoIpEndpointSimulator("algo-sim", sim_cfg)
    link = _Link(sim)
    cfg = dict(INFO["default_config"])
    cfg.update({"host": "10.0.0.161", "password": "algo", "poll_interval": 0, "timeout": 2.0})
    cfg.update(driver_config or {})
    driver = CLASS("algo", cfg, StubState(), StubEvents())
    return driver, sim, link


async def _connect(driver, link, bind):
    bind(link)
    await driver.connect()


def _commands(driver) -> set[str]:
    return set(driver.DRIVER_INFO["commands"])


# ── The Standard method's signature (the guide's worked examples) ────────────


def test_hmac_matches_the_guides_payload_example():
    text = DRV.signature_input(
        "POST", "/api/controls/tone/start", 1601312252, "49936",
        "6e43c05d82f71e77c586e29edb93b129",
    )
    assert text == (
        "POST:/api/controls/tone/start:6e43c05d82f71e77c586e29edb93b129:"
        "application/json:1601312252:49936"
    )
    assert DRV.signature("algo", text) == (
        "2e109d7aeed54a1cb04c6b72b1d854f442cf1ca15eb0af32f2512dd77ab6b330"
    )


def test_hmac_matches_both_sample_requests():
    # The sample POST: Date 28 Sep 2020 17:07:18 GMT, nonce 1028014788.
    post = DRV.signature_input(
        "POST", "/api/controls/tone/start", 1601312838, "1028014788",
        "6e43c05d82f71e77c586e29edb93b129",
    )
    assert DRV.signature("algo", post) == (
        "c450024af4493f9cdf582499456bf58d7e134b161566e764e61bd52d24f759dc"
    )
    # The sample GET (no payload): nonce 881767496, signed three seconds
    # before its Date header. The guide's "Example HMAC Input" for this case
    # (nonce 49936) does not produce the digest it prints; this request does.
    get = DRV.signature_input(
        "GET", "/api/settings/audio.page.vol", 1601312835, "881767496", None,
    )
    assert get == "GET:/api/settings/audio.page.vol:1601312835:881767496"
    assert DRV.signature("algo", get) == (
        "c5b349415bce0b9e1b8122829d32fbe0a078791b311c4cf40369c7ab4eb165a8"
    )


def test_content_md5_is_the_md5_of_the_bytes_sent():
    # The sample request's 39-byte body.
    body = b'{"path":"page-notif.wav", "loop":false}'
    assert len(body) == 39
    assert hashlib.md5(body).hexdigest() == "6e43c05d82f71e77c586e29edb93b129"


def test_parsers():
    assert DRV.model_from_product("Algo 8301 IP Paging Adapter & Scheduler") == "8301"
    assert DRV.model_from_product("Algo 8190S IP Speaker - Clock & Visual Alerter") == "8190S"
    assert DRV.model_from_product("Algo 8180G2 IP Audio Alerter") == "8180"
    assert DRV.model_from_product("Something else") == ""
    assert DRV.firmware_tuple("5.5_beta11") == (5, 5)
    assert DRV.firmware_tuple("5.3.4") == (5, 3, 4)
    assert DRV.firmware_tuple("") == ()
    assert DRV.parse_db("-42dB") == -42
    assert DRV.parse_db("0dB") == 0
    assert DRV.parse_db("6dB") == 6
    assert DRV.parse_db("loud") is None


def test_every_declared_command_has_a_model_or_runs_everywhere():
    for command in INFO["commands"]:
        models = DRV._COMMAND_MODELS.get(command)
        assert models is None or models, command
    assert set(DRV._COMMAND_MODELS) <= set(INFO["commands"])
    assert set(DRV._SETTING_PARAMS) == set(INFO["device_settings"])
    for action in INFO["actions"]:
        assert action["id"] in INFO["commands"]


# ── Connect and narrowing ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_identifies_a_display_and_narrows(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    assert driver.get_state("connected") is True
    assert driver.get_state("model") == "8410"
    assert driver.get_state("product_name") == "Algo 8410 IP Display Speaker"
    assert driver.get_state("firmware_version") == "5.7.1"
    assert driver.get_state("mac_address") == "00:22:ee:11:22:33"
    commands = _commands(driver)
    assert {"show_text", "strobe_start", "play_tone", "emergency_alert_start", "page_from_extension"} <= commands
    assert not commands & {"lock_door", "relay_on", "activate_console_button", "skip_scheduled_events"}
    pattern = driver.DRIVER_INFO["commands"]["strobe_start"]["params"]["pattern"]
    assert pattern["type"] == "enum"
    assert {"value": "2", "label": "Strobe Fast"} in pattern["values"]
    assert {"value": "15", "label": "Off"} in pattern["values"]
    # The class declaration is untouched.
    assert INFO["commands"]["strobe_start"]["params"]["pattern"]["type"] == "integer"
    assert set(driver.DRIVER_INFO["device_settings"]) == {
        "page_volume", "ring_volume", "ambient_noise_compensation",
        "noise_max_volume", "microphone_mute",
    }
    assert {a["id"] for a in driver.DRIVER_INFO["actions"]} == {
        "test_start", "test_stop", "play_tone", "stop_tone", "strobe_start",
        "strobe_stop", "show_text", "stop_text",
    }
    # First reads.
    assert json.loads(driver.get_state("tone_options"))[0] == "bell-na.wav"
    assert driver.get_state("device_name") == "hallway-display-1"
    assert driver.get_state("call_status") == "Idle"
    assert driver.get_state("call_active") is False
    assert driver.get_state("multicast_mode") == "Receiver (Idle)"
    assert driver.get_state("multicast_active") is False
    assert driver.get_state("relay_input") == "Idle"
    assert driver.get_state("relay_input_active") is False
    assert driver.get_state("ambient_noise_level") == 54
    assert driver.get_state("temperature") == "41C"
    assert driver.get_state("page_volume") == 0
    assert driver.get_state("ring_volume") == -3
    assert driver.get_state("ambient_noise_compensation") is True
    assert driver.get_state("microphone_mute") is False
    # Every request was signed and verified by the simulator.
    auths = [r[2].get("authorization", "") for r in link.requests]
    assert auths and all(a.startswith("hmac admin:") for a in auths)


@pytest.mark.parametrize(
    "model, present, absent",
    [
        ("8186", {"play_tone", "call_extension", "page_from_extension", "set_page_volume"},
         {"strobe_start", "show_text", "lock_door", "skip_scheduled_events"}),
        ("8138", {"strobe_start", "test_start", "emergency_alert_start"},
         {"play_tone", "call_extension", "show_text", "set_page_volume"}),
        ("8301", {"skip_scheduled_events", "play_tone_multicast", "call_extension", "set_ambient_noise_level"},
         {"strobe_start", "page_from_extension", "show_image", "microphone_mute_on"}),
        ("8063", {"relay_on", "relay_pulse", "aux_24v_on", "unlock_door_momentary", "call_extension"},
         {"play_tone", "strobe_start", "test_start", "emergency_alert_start"}),
        ("8201", {"lock_door", "play_tone", "test_start"},
         {"relay_on", "call_extension", "strobe_start"}),
        ("8450", {"activate_console_button", "stop_console_events", "set_ambient_noise_level"},
         {"play_tone", "start_audio_stream", "strobe_start", "lock_door"}),
    ],
)
@pytest.mark.asyncio
async def test_each_model_is_offered_only_its_commands(mocked_client, model, present, absent):
    driver, sim, link = _make({"model": model})
    await _connect(driver, link, mocked_client)
    assert driver.get_state("model") == model
    commands = _commands(driver)
    assert present <= commands, present - commands
    assert not commands & absent, commands & absent
    # Maintenance commands are on every model.
    assert {"reboot", "factory_reset", "check_firmware"} <= commands


@pytest.mark.asyncio
async def test_settings_follow_the_model(mocked_client):
    driver, sim, link = _make({"model": "8301"})
    await _connect(driver, link, mocked_client)
    assert set(driver.DRIVER_INFO["device_settings"]) == {
        "page_volume", "ring_volume", "ambient_noise_compensation",
        "noise_max_volume", "input_volume",
    }
    assert driver.get_state("input_volume") == 0
    driver, sim, link = _make({"model": "8201"})
    await _connect(driver, link, mocked_client)
    assert set(driver.DRIVER_INFO["device_settings"]) == {"speaker_volume"}
    assert driver.get_state("speaker_volume") == -6


@pytest.mark.asyncio
async def test_firmware_without_the_about_page_keeps_everything(mocked_client):
    driver, sim, link = _make({"firmware_version": "5.3"})
    await _connect(driver, link, mocked_client)
    assert driver.get_state("connected") is True
    assert driver.get_state("model") in (None, "")
    assert driver.DRIVER_INFO is INFO
    # No About, no Status page; the settings still answer.
    assert driver.get_state("device_name") is None
    assert driver.get_state("page_volume") == 0
    # Asked once, remembered.
    await driver.poll()
    assert sum(1 for r in link.requests if r[1] == "/api/info/status") == 1
    # Stop Tone names the tone: firmware too old for the About page is
    # older than 5.5. (Play Tone's own entry gives 5.4 as its floor, so the
    # simulator refuses it here; the tone is named on the command.)
    await driver.send_command("stop_tone", {"tone": "gong.wav"})
    assert json.loads(link.requests[-1][3]) == {"path": "gong.wav"}


# ── Commands ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tones_tests_streams_and_calls(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    await driver.send_command("play_tone", {"tone": "chime.wav", "loop": True, "duration": 10})
    assert sim.get_state("tone_playing") == "chime.wav (loop) for 10s"
    body = json.loads(link.requests[-1][3])
    assert body == {"path": "chime.wav", "loop": True, "duration": 10}
    assert link.requests[-1][2]["content-md5"] == hashlib.md5(link.requests[-1][3]).hexdigest()
    await driver.send_command("stop_tone")
    assert sim.get_state("tone_playing") == ""
    assert link.requests[-1][3] == b""
    await driver.send_command("play_tone_multicast", {
        "tone": "bell-na.wav", "address": "239.0.0.1", "port": 50000,
        "type": "poly", "group": 3, "play_locally": True,
    })
    body = json.loads(link.requests[-1][3])
    assert body["mcast"] is True and body["playback"] is True
    assert body["state"] == {"mode": "sender", "address": "239.0.0.1", "port": "50000", "type": "poly", "group": 3}
    assert sim.get_state("tone_playing") == "bell-na.wav to 239.0.0.1:50000"
    await driver.send_command("test_start")
    assert sim.get_state("test_active") is True
    await driver.send_command("test_stop")
    assert sim.get_state("test_active") is False
    await driver.send_command("start_audio_stream", {"port": 5001})
    assert sim.get_state("audio_stream_port") == 5001
    await driver.send_command("stop_audio_stream")
    assert sim.get_state("audio_stream_port") == 0
    await driver.send_command("set_ambient_noise_level", {"level": 70})
    assert sim.get_state("ambient_noise") == 70
    await driver.send_command("call_extension", {"extension": "123", "tone": "chime.wav", "interval": 3, "max_duration": 60, "dtmf": "1,2"})
    assert json.loads(link.requests[-1][3]) == {
        "extension": "123", "tone": "chime.wav", "interval": "3", "maxdur": "60", "dtmf": "1,2",
    }
    await driver.poll()
    assert driver.get_state("call_status") == "Connected"
    assert driver.get_state("call_active") is True
    await driver.send_command("end_call")
    await driver.poll()
    assert driver.get_state("call_active") is False
    await driver.send_command("page_from_extension", {"extension": "200"})
    assert json.loads(link.requests[-1][3]) == {"extension": "200"}


@pytest.mark.asyncio
async def test_an_unknown_tone_is_refused_with_the_reason(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    with pytest.raises(ValueError, match="refused Play Tone: HTTP 400 \\(Tone nope.wav not found\\)"):
        await driver.send_command("play_tone", {"tone": "nope.wav"})
    assert "Tone nope.wav not found" in driver.get_state("last_error")


@pytest.mark.asyncio
async def test_stop_tone_names_the_tone_on_old_firmware(mocked_client):
    driver, sim, link = _make({"firmware_version": "5.4.2"})
    await _connect(driver, link, mocked_client)
    await driver.send_command("play_tone", {"tone": "gong.wav"})
    await driver.send_command("stop_tone")
    assert json.loads(link.requests[-1][3]) == {"path": "gong.wav"}
    assert sim.get_state("tone_playing") == ""


@pytest.mark.asyncio
async def test_emergency_alerts(mocked_client):
    driver, sim, link = _make({"model": "8138"})
    await _connect(driver, link, mocked_client)
    await driver.send_command("emergency_alert_start", {"announcement": 4})
    assert json.loads(link.requests[-1][3]) == {"announcement": 4}
    assert sim.get_state("emergency_alert") == 4
    await driver.send_command("emergency_alert_stop")
    assert sim.get_state("emergency_alert") == 0


@pytest.mark.parametrize(
    "model, pattern, brightness, level",
    [("8138", "Steady Two-color", "high", 255), ("8138", "9", "low", 0),
     ("8190S", "Sparkle", "high", 3), ("8190S", 4, "medium", 2),
     ("8410", "Inside Out", "medium", 56), ("8128", 15, "high", 255)],
)
@pytest.mark.asyncio
async def test_strobe_patterns_and_brightness_per_model(mocked_client, model, pattern, brightness, level):
    driver, sim, link = _make({"model": model})
    await _connect(driver, link, mocked_client)
    await driver.send_command("strobe_start", {
        "pattern": pattern, "color": "red", "color2": "blue",
        "brightness": brightness, "duration": 5,
    })
    body = json.loads(link.requests[-1][3])
    assert body["ledlvl"] == level
    assert isinstance(body["pattern"], int)
    assert sim.get_state("strobe").endswith(f"at {level} for 5s")
    await driver.send_command("strobe_stop")
    assert sim.get_state("strobe") == ""


@pytest.mark.asyncio
async def test_a_pattern_the_model_lacks_is_refused_by_the_device(mocked_client):
    driver, sim, link = _make({"model": "8128"})
    await _connect(driver, link, mocked_client)
    with pytest.raises(ValueError, match="not available on the 8128"):
        await driver.send_command("strobe_start", {"pattern": 2, "color": "red"})


@pytest.mark.asyncio
async def test_screen_commands(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    await driver.send_command("show_image", {"image": "school.jpeg", "duration": 10})
    assert json.loads(link.requests[-1][3]) == {"type": "image", "image1": "school.jpeg", "stopAfter": 10}
    assert sim.get_state("screen") == "image school.jpeg for 10s"
    await driver.send_command("show_image_with_text", {
        "image": "school.jpeg", "text": "Class is in session", "text_color": "forestgreen",
        "text_position": "middle", "scroll": True, "scroll_speed": 3, "text_size": "medium",
        "font": "inter",
    })
    assert json.loads(link.requests[-1][3]) == {
        "type": "image", "image1": "school.jpeg", "text1": "Class is in session",
        "textColor": "forestgreen", "textPosition": "middle", "textSize": "medium",
        "textFont": "inter", "textScroll": True, "textScrollSpeed": "3",
    }
    await driver.send_command("show_slide", {"name": "Evacuation", "duration": 5})
    assert json.loads(link.requests[-1][3]) == {"screenName": "Evacuation", "stopAfter": 5}
    assert sim.get_state("screen") == "slide Evacuation for 5s"
    await driver.send_command("show_slideshow", {"slides": "slide1,slide2 , slide3", "slide_duration": 10, "override_strobe": True})
    assert json.loads(link.requests[-1][3]) == {
        "duration": 10, "slideNames": "slide1, slide2, slide3", "overrideStrobe": True,
    }
    await driver.send_command("show_clock", {"style": "analogClock", "show_seconds": True, "position": "center"})
    assert json.loads(link.requests[-1][3]) == {"type": "analogClock", "clockSecondsAnalog": True, "clockPosition": "center"}
    assert sim.get_state("screen") == "analog clock"
    await driver.send_command("show_flashing_images", {"image1": "a.jpeg", "image2": "b.jpeg"})
    assert sim.get_state("screen") == "flashing a.jpeg / b.jpeg"
    await driver.send_command("show_template", {"template": "announcement", "text1": "Assembly", "text2": "Gym, 2 pm"})
    assert json.loads(link.requests[-1][3]) == {
        "type": "template", "template": "announcement", "text1": "Assembly", "text2": "Gym, 2 pm",
    }
    await driver.send_command("stop_screen")
    assert sim.get_state("screen") == ""
    await driver.send_command("show_text", {
        "text": "Stand by for instructions", "color": "#FFFFFF", "background": True,
        "background_color": "#000000", "duration": 10, "persistent": True,
    })
    assert json.loads(link.requests[-1][3]) == {
        "textContent": "Stand by for instructions", "textColor": "#FFFFFF",
        "textBg": True, "textBgColor": "#000000", "persistent": True, "duration": 10,
    }
    assert sim.get_state("screen_text") == "Stand by for instructions"
    await driver.send_command("stop_text")
    assert sim.get_state("screen_text") == ""


@pytest.mark.asyncio
async def test_a_command_too_new_for_the_firmware_says_so(mocked_client):
    driver, sim, link = _make({"firmware_version": "5.5"})
    await _connect(driver, link, mocked_client)
    with pytest.raises(ValueError, match="needs firmware 5.7 or newer; this device reports 5.5"):
        await driver.send_command("show_text", {"text": "hello"})


@pytest.mark.asyncio
async def test_doors_and_relays(mocked_client):
    driver, sim, link = _make({"model": "8063"})
    await _connect(driver, link, mocked_client)
    await driver.send_command("unlock_door", {"door": "local"})
    assert sim.get_state("door") == "unlocked (local)"
    await driver.send_command("lock_door", {"door": "netdc1"})
    assert sim.get_state("door") == "locked (netdc1)"
    await driver.send_command("unlock_door_momentary", {"seconds": 10})
    assert json.loads(link.requests[-1][3]) == {"doorid": "local", "duration": "10"}
    assert sim.get_state("door") == "unlocked for 10s (local)"
    await driver.send_command("relay_on")
    assert sim.get_state("relay_output") is True
    await driver.send_command("relay_off")
    assert sim.get_state("relay_output") is False
    await driver.send_command("relay_pulse", {"seconds": 15})
    assert json.loads(link.requests[-1][3]) == {"duration": 15}
    assert sim.get_state("relay_output") is True
    await driver.send_command("aux_24v_on")
    assert sim.get_state("aux_24v") is True
    await driver.send_command("aux_24v_off")
    assert sim.get_state("aux_24v") is False
    sim.set_state("relay_input_1", "active (Normally Open)")
    await driver.poll()
    assert driver.get_state("relay_input_1") == "active (Normally Open)"
    assert driver.get_state("relay_input_1_active") is True
    assert driver.get_state("relay_input_2_active") is False


@pytest.mark.asyncio
async def test_console(mocked_client):
    driver, sim, link = _make({"model": "8450"})
    await _connect(driver, link, mocked_client)
    assert driver.get_state("events_active") is False
    await driver.send_command("activate_console_button", {"button": "lockdown"})
    assert driver.get_state("active_events") == "emergency"
    assert driver.get_state("events_active") is True
    assert driver.get_state("console_status") == "Emergency Alert Active"
    with pytest.raises(ValueError, match="No button with identifier nope"):
        await driver.send_command("activate_console_button", {"button": "nope"})
    await driver.send_command("stop_console_events", {"type": "all"})
    assert driver.get_state("events_active") is False
    assert driver.get_state("console_status") == "Idle"


@pytest.mark.asyncio
async def test_skip_and_restore_a_days_events(mocked_client):
    driver, sim, link = _make({"model": "8301"})
    await _connect(driver, link, mocked_client)
    await driver.send_command("skip_scheduled_events", {"date": "2026-12-01"})
    post = [r for r in link.requests if r[0] == "POST"][-1]
    assert post[1] == "/api/schedules"
    assert json.loads(post[3]) == {"skip": [{"evid": -1, "date": "2026-12-01"}]}
    assert sim.get_state("skipped_dates") == "2026-12-01"
    assert driver.get_state("next_scheduled_event") == "No Events Scheduled"
    await driver.send_command("restore_scheduled_events", {"date": "2026-12-01"})
    assert sim.get_state("skipped_dates") == ""
    await driver.send_command("skip_scheduled_events")
    assert sim.get_state("skipped_dates") == DRV.date.today().isoformat()


@pytest.mark.asyncio
async def test_volume_and_microphone(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    await driver.send_command("set_page_volume", {"volume": -12})
    put = [r for r in link.requests if r[0] == "PUT"][-1]
    assert json.loads(put[3]) == {"audio.page.vol": "-12dB"}
    assert sim.get_state("page_volume_db") == -12
    assert driver.get_state("page_volume") == -12
    await driver.send_command("microphone_mute_on")
    assert sim.get_state("microphone_mute") is True
    assert driver.get_state("microphone_mute") is True
    await driver.send_command("microphone_mute_off")
    assert driver.get_state("microphone_mute") is False
    with pytest.raises(ValueError, match="3 dB steps"):
        await driver.send_command("set_page_volume", {"volume": -10})


@pytest.mark.asyncio
async def test_device_settings_write_and_read_back(mocked_client):
    driver, sim, link = _make({"model": "8301"})
    await _connect(driver, link, mocked_client)
    await driver.set_device_setting("ring_volume", -21)
    assert sim.get_state("ring_volume_db") == -21
    assert driver.get_state("ring_volume") == -21
    await driver.set_device_setting("input_volume", 6)
    assert driver.get_state("input_volume") == 6
    await driver.set_device_setting("ambient_noise_compensation", False)
    assert sim.get_state("noise_compensation") is False
    assert driver.get_state("ambient_noise_compensation") is False
    with pytest.raises(ValueError, match="between -27 dB and 6 dB"):
        await driver.set_device_setting("input_volume", 9)
    with pytest.raises(ValueError, match="Unknown setting"):
        await driver.set_device_setting("microphone_mute", True)
    # A change made on the device shows up at the next settings read.
    sim.set_state("page_volume_db", -30)
    driver._last_settings = float("-inf")
    await driver.poll()
    assert driver.get_state("page_volume") == -30


@pytest.mark.asyncio
async def test_firmware_check_and_maintenance(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    await driver.send_command("check_firmware")
    assert driver.get_state("firmware_available") == "Up to date"
    sim.set_state("firmware_available", "5.8.0")
    await driver.send_command("check_firmware")
    assert driver.get_state("firmware_available") == "5.8.0"
    await driver.send_command("restart_application")
    await driver.send_command("reboot")
    await driver.send_command("factory_reset")
    assert sim.get_state("api_enabled") is False


@pytest.mark.parametrize("model", ["8410", "8186", "8138", "8190S", "8301", "8063", "8201", "8450"])
@pytest.mark.asyncio
async def test_every_offered_command_has_a_branch(mocked_client, model):
    """Each command a model is offered reaches the device (a refusal is fine;
    falling through to "Unknown command" is not)."""
    driver, sim, link = _make({"model": model})
    await _connect(driver, link, mocked_client)
    samples = {
        "tone": "chime.wav", "extension": "100", "port": 5001, "level": 50,
        "announcement": 1, "pattern": 0 if model != "8128" else 1, "color": "red",
        "image": "a.jpeg", "image1": "a.jpeg", "image2": "b.jpeg", "text": "hi",
        "name": "s", "slides": "a", "slide_duration": 5, "template": "announcement",
        "seconds": 5, "button": "class-change", "volume": -3, "address": "239.0.0.1",
    }
    for command, cdef in driver.DRIVER_INFO["commands"].items():
        if command == "factory_reset":
            continue
        params = {name: samples[name] for name, pdef in (cdef.get("params") or {}).items() if pdef.get("required")}
        try:
            await driver.send_command(command, params)
        except ValueError as exc:
            assert "Unknown command" not in str(exc), command
            assert "refused" in str(exc), (command, str(exc))


# ── Faults ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_wrong_password_is_auth_failed(mocked_client):
    driver, sim, link = _make(driver_config={"password": "wrong"})
    bind = mocked_client
    bind(link)
    with pytest.raises(ConnectionFaultError) as excinfo:
        await driver.connect()
    assert excinfo.value.fault_code == "auth_failed"
    assert "refused the RESTful API credentials (HTTP 401)" in str(excinfo.value)
    assert "factory password is algo" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_device_clock_outside_the_window_is_named(mocked_client):
    driver, sim, link = _make({"clock_offset_s": 95})
    mocked_client(link)
    with pytest.raises(ConnectionFaultError) as excinfo:
        await driver.connect()
    assert excinfo.value.fault_code == "auth_failed"
    message = str(excinfo.value)
    # The Date header has one-second resolution.
    assert re.search(r"clock is 9[45] seconds ahead of this server's", message), message
    assert "NTP" in message
    # Within the window it works.
    driver, sim, link = _make({"clock_offset_s": -20})
    await _connect(driver, link, mocked_client)
    assert driver.get_state("connected") is True


@pytest.mark.asyncio
async def test_basic_and_none(mocked_client):
    driver, sim, link = _make({"auth_method": "basic"}, {"auth_method": "basic"})
    await _connect(driver, link, mocked_client)
    assert driver.get_state("connected") is True
    assert link.requests[-1][2]["authorization"] == "Basic YWRtaW46YWxnbw=="
    await driver.send_command("test_start")
    driver, sim, link = _make({"auth_method": "none"}, {"auth_method": "none"})
    await _connect(driver, link, mocked_client)
    assert "authorization" not in link.requests[-1][2]
    await driver.send_command("test_start")
    assert sim.get_state("test_active") is True


@pytest.mark.asyncio
async def test_a_method_that_does_not_match_the_device_is_refused(mocked_client):
    driver, sim, link = _make({"auth_method": "basic"}, {"auth_method": "standard"})
    mocked_client(link)
    with pytest.raises(ConnectionFaultError) as excinfo:
        await driver.connect()
    assert excinfo.value.fault_code == "auth_failed"
    assert "authentication method and password here match" in str(excinfo.value)
    assert "RESTful API is turned on" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_api_turned_off_is_invalid_config(mocked_client):
    driver, sim, link = _make()
    sim.inject_error("api_disabled")
    mocked_client(link)
    with pytest.raises(ConnectionFaultError) as excinfo:
        await driver.connect()
    assert excinfo.value.fault_code == "invalid_config"
    assert "Turn on RESTful API under Advanced Settings > Admin" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_password_changed_mid_session_drops_as_auth_failed(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    sim.inject_error("password_changed")
    with pytest.raises(ConnectionFaultError):
        await driver.poll()
    assert driver.forced and driver.forced[-1][0] == "auth_failed"


@pytest.mark.asyncio
async def test_poll_propagates_transport_errors(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    link.reachable = False
    with pytest.raises(httpx.ConnectError):
        await driver.poll()


@pytest.mark.asyncio
async def test_an_unreachable_host_is_a_connection_error(mocked_client):
    driver, sim, link = _make()
    link.reachable = False
    mocked_client(link)
    with pytest.raises(ConnectionError):
        await driver.connect()


@pytest.mark.asyncio
async def test_liveness_probe_counts_any_answer(mocked_client):
    driver, sim, link = _make()
    await _connect(driver, link, mocked_client)
    driver._last_reply = float("-inf")
    sim.inject_error("password_changed")
    await driver._liveness_probe()   # a 401 is still an answer
    link.reachable = False
    driver._last_reply = float("-inf")
    with pytest.raises(httpx.ConnectError):
        await driver._liveness_probe()


def test_the_simulator_rejects_a_stale_or_tampered_signature():
    sim = SIM.AlgoIpEndpointSimulator("s", {"password": "algo"})
    body = '{"path": "chime.wav", "loop": false}'
    md5 = hashlib.md5(body.encode()).hexdigest()
    now = int(time.time())

    def headers(ts, digest_body=body):
        text = DRV.signature_input("POST", "/api/controls/tone/start", ts, "7", hashlib.md5(digest_body.encode()).hexdigest())
        return {
            "Authorization": f"hmac admin:7:{DRV.signature('algo', text)}",
            "Date": email.utils.formatdate(ts, usegmt=True),
            "Content-MD5": md5, "Content-Type": "application/json",
        }

    ok = sim.handle_request("POST", "/api/controls/tone/start", headers(now), body)
    assert ok[0] == 200 and "Date" in ok[2]
    assert sim.handle_request("POST", "/api/controls/tone/start", headers(now - 60), body)[0] == 401
    assert sim.handle_request("POST", "/api/controls/tone/start", headers(now, '{"path": "gong.wav", "loop": false}'), body)[0] == 401
