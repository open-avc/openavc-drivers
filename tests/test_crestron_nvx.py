"""Driver + simulator tests for crestron_nvx (Crestron DM NVX, CresNext REST).

No NVX needed for CI: the real driver's httpx client (built by the driver's own
_create_transport, so its redirect handling is the real one) is wired to the
real simulator via httpx.MockTransport, cookies included, and the long poll is
served by the simulator's own held request, so the driver's long-poll loop runs
in-test. A command POSTs to /Device, the sim mutates, and the driver reads it
back — asserted on both sides. Run for both roles (Transmitter/Receiver),
since the driver adapts its surface to the device's reported DeviceMode.

Push: changes reach the driver on the long poll with polling off; a change in
an array re-reads that object; the other role's objects are ignored; a long
poll whose session was refused signs in again once; failures back off and
never take the device down. The D30's own long-poll answers are replayed
byte-exact from tests/fixtures/crestron_nvx/.

openavc.* are stubbed so the community CI stays self-contained
(conftest.py rolls the stubs back after collection). httpx is a real dependency.

Live hardware verification (E20 encoder + D200 decoder, firmware 7.1.5259;
the long poll on a D30, firmware 7.0.5057) and the first-boot setup wizard are
recorded in driver-roadmap/shipped/crestron_nvx.md.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    ConnectionFaultError as _ConnectionFaultError,
    StubBaseDriver,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "crestron_nvx.py"
SIM_PATH = REPO_ROOT / "switchers" / "crestron_nvx_sim.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "crestron_nvx"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven lifecycle for a driver that owns its
    session; state comes from the shared stubs."""

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0
        self._last_fault = None
        self.forced: list[tuple[str, str]] = []

    def _stash_fault(self, code, message=""):
        self._last_fault = (code, message)

    async def _verify_reachable(self, *_a, **_k):
        return True

    def _force_disconnect(self, code="no_response", message=""):
        self.forced.append((code, message))
        self._connected = False
        self.set_state("connected", False)

    async def disconnect(self):
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)


install_stubs(base_driver=_FakeBaseDriver)
DRV = load_module("crestron_nvx_under_test", DRIVER_PATH)
SIM = load_module("crestron_nvx_sim_under_test", SIM_PATH)


# ── Harness ─────────────────────────────────────────────────────────────────

class _Link:
    """The wire between the driver's client and the simulator: a log of every
    answered request (method, path, status) and a switch that makes the unit
    unreachable."""

    def __init__(self, sim, log=None):
        self.sim = sim
        self.log = log if log is not None else []
        self.reachable = True
        self.refused = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            self.refused += 1
            raise httpx.ConnectError("Connection refused")
        path = request.url.path
        headers = dict(request.headers)
        body = request.content.decode() if request.content else ""
        if request.method == "GET" and path == "/Device/Longpoll":
            status, payload, extra, cookies = await self.sim.longpoll(headers)
        else:
            status, payload, extra, cookies = self.sim.dispatch(
                request.method, path, headers, body,
            )
        self.log.append((request.method, path, status))
        out = list((extra or {}).items())
        for name, value in cookies:
            out.append(("set-cookie", f"{name}={value}; Path=/; Secure; HttpOnly"))
        if isinstance(payload, dict):
            return httpx.Response(status, json=payload, headers=out)
        return httpx.Response(status, text=str(payload), headers=out)


def _client_through(link_or_handler):
    """Build the driver's client with its own _create_transport, routed to a
    handler instead of the network."""
    handler = getattr(link_or_handler, "handle", link_or_handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    return original, factory


async def _driver_with(handler, config=None):
    cfg = {"host": "sim", "port": 443, "username": "admin", "password": "x",
           "poll_interval": 0}
    cfg.update(config or {})
    d = DRV.CrestronNVXDriver("nvx", cfg, _FakeState(), _FakeEvents())
    d._base_url = "https://sim"
    original, factory = _client_through(handler)
    httpx.AsyncClient = factory
    try:
        await d._create_transport("http")
    finally:
        httpx.AsyncClient = original
    return d


async def _make_driver(sim, config=None, log=None):
    link = _Link(sim, log)
    d = await _driver_with(link, config)
    d.link = link
    return d


async def _connect(driver, *, longpoll=False):
    """Run the driver's connect hooks against the mock-transport client. With
    ``longpoll`` the driver's own _initial_sync runs, which starts the long
    poll; without it the role state is read and nothing is held open."""
    await driver._post_connect()
    driver._connected = True
    driver.set_state("connected", True)
    if longpoll:
        await driver._initial_sync()
    else:
        await driver._refresh_role_state()


async def _until(check, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.01)
    return check()


def _has(driver, prop):
    return driver.state.has(f"device.{driver.device_id}.{prop}")


def _without_longpoll(log):
    return [row for row in log if row[1] != "/Device/Longpoll"]


# ── Metadata ────────────────────────────────────────────────────────────────

def test_metadata_shape():
    info = DRV.CrestronNVXDriver.DRIVER_INFO
    assert info["version"] == "2.2.0"
    assert info["min_platform_version"] == "0.27.0"
    assert info["category"] == "switcher"
    assert info["web_ui"] is True
    assert info["transport"] == "http"
    # Changes arrive on the long poll; the poll is the backstop.
    assert info["default_config"]["poll_interval"] == 30
    assert info["config_schema"]["poll_interval"]["default"] == 30
    # The class declares the whole line's surface; instances narrow to their
    # role once the device reports DeviceMode (see the role-pruning tests).
    for cmd in ("route_stream", "set_bitrate", "start_stream", "reboot"):
        assert cmd in info["commands"]
    assert any(a["id"] == "set_up_nvx" and a["kind"] == "setup" for a in info["actions"])


def test_the_client_does_not_follow_redirects():
    """The NVX redirects a request without a live session to its sign-in
    page; followed, that is a 200 page and the refusal is lost."""
    async def go():
        d = await _make_driver(SIM.CrestronNvxSimulator("s", {}))
        try:
            assert d._client.follow_redirects is False
        finally:
            await d._close_session()

    asyncio.run(go())


# ── Decoder role ────────────────────────────────────────────────────────────

def test_decoder_connect_and_route():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})  # defaults to Receiver / D200
        d = await _make_driver(sim)
        try:
            await _connect(d)
            assert d.get_state("device_mode") == "Receiver"
            assert d.get_state("model") == "DM-NVX-D200"

            await d.send_command("route_stream", {"encoder": "192.168.1.50"})
            await d.poll()
            assert d.get_state("stream_location") == "rtsp://192.168.1.50:554/live.sdp"
            assert d.get_state("stream_status") == "Stream started"
            assert d.get_state("video_source") == "Stream"

            await d.send_command("stop_stream")
            await d.poll()
            assert d.get_state("stream_status") == "Stream stopped"
        finally:
            await d._close_session()

    asyncio.run(go())


def test_decoder_device_setting_round_trip():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)
        try:
            await _connect(d)
            await d.set_device_setting("leds", False)
            await d.poll()
            assert d.get_state("leds_enabled") is False
            await d.set_device_setting("front_panel_lock", True)
            await d.poll()
            assert d.get_state("front_panel_locked") is True
        finally:
            await d._close_session()

    asyncio.run(go())


# ── Encoder role ────────────────────────────────────────────────────────────

def test_encoder_connect_bitrate_and_preview():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.set_state("device_mode", "Transmitter")
        sim.set_state("model", "DM-NVX-E20")
        d = await _make_driver(sim)
        try:
            await _connect(d)
            assert d.get_state("device_mode") == "Transmitter"
            assert d.get_state("model") == "DM-NVX-E20"

            await d.send_command("set_transmit_multicast", {"address": "239.5.5.5"})
            await d.send_command("set_bitrate", {"mbps": 500, "mode": "Fixed"})
            await d.send_command("start_stream")
            await d.poll()
            assert d.get_state("bitrate") == 500
            assert d.get_state("stream_multicast") == "239.5.5.5"
            assert d.get_state("stream_status") == "Stream started"
            # Preview convention: an encoder publishes its RTSP stream.
            assert d.get_state("preview_format") == "rtsp"
            assert d.get_state("preview_url").startswith("rtsp://")
        finally:
            await d._close_session()

    asyncio.run(go())


def test_wrong_role_object_is_ignored():
    """A decoder must not choke on StreamTransmit answering 'UNSUPPORTED'."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})  # Receiver
        d = await _make_driver(sim)
        try:
            await _connect(d)
            # The receiver never queries StreamTransmit; asking directly returns
            # the unsupported sentinel string, which _first_stream tolerates.
            data = await d._api_get("/Device/StreamTransmit")
            assert d._first_stream(data, "StreamTransmit") is None
        finally:
            await d._close_session()

    asyncio.run(go())


# ── Role pruning ────────────────────────────────────────────────────────────


def test_encoder_surface_is_pruned_to_transmitter():
    """A connected transmitter presents only the encoder surface: no decoder
    commands in the Send Command list, no decoder state keys, and a decoder
    command sent anyway (macro/script) is rejected with a clear reason."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.set_state("device_mode", "Transmitter")
        sim.set_state("model", "DM-NVX-E20")
        d = await _make_driver(sim)
        try:
            await _connect(d)

            cmds = d.DRIVER_INFO["commands"]
            for cmd in ("set_transmit_multicast", "set_bitrate", "start_stream", "reboot"):
                assert cmd in cmds
            for cmd in ("route_stream", "set_scaler_resolution"):
                assert cmd not in cmds

            svars = d.DRIVER_INFO["state_variables"]
            assert "bitrate" in svars and "preview_url" in svars
            for var in ("scaler_resolution", "video_wall_mode", "rx_initiator",
                        "output_connected", "output_resolution", "output_hdcp"):
                assert var not in svars
                assert not _has(d, var)  # seeded key deleted

            # The quick-action strip loses the decoder's headline action too.
            assert not any(a["id"] == "route_stream" for a in d.DRIVER_INFO["actions"])

            with pytest.raises(RuntimeError, match="route_stream"):
                await d.send_command("route_stream", {"encoder": "192.168.1.50"})

            # The class-level declaration (catalog surface) stays complete.
            assert "route_stream" in DRV.CrestronNVXDriver.DRIVER_INFO["commands"]
        finally:
            await d._close_session()

    asyncio.run(go())


def test_decoder_surface_is_pruned_to_receiver():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})  # defaults to Receiver / D200
        d = await _make_driver(sim)
        try:
            await _connect(d)

            cmds = d.DRIVER_INFO["commands"]
            for cmd in ("route_stream", "set_scaler_resolution", "stop_stream"):
                assert cmd in cmds
            for cmd in ("set_transmit_multicast", "set_bitrate"):
                assert cmd not in cmds

            svars = d.DRIVER_INFO["state_variables"]
            assert "scaler_resolution" in svars and "rx_initiator" in svars
            for var in ("bitrate", "active_bitrate", "bitrate_mode", "input_sync",
                        "input_resolution", "input_hdcp", "preview_url", "preview_format"):
                assert var not in svars
                assert not _has(d, var)

            assert not any(a["id"] == "set_bitrate" for a in d.DRIVER_INFO["actions"])

            with pytest.raises(RuntimeError, match="set_bitrate"):
                await d.send_command("set_bitrate", {"mbps": 500})
        finally:
            await d._close_session()

    asyncio.run(go())


# ── Push: the long poll ─────────────────────────────────────────────────────
# Polling is off in every test below (poll_interval 0, and nothing calls
# poll()): whatever reaches the driver's state came on the long poll.


def test_a_change_on_the_unit_arrives_with_polling_off():
    """The video source changed at the unit (its web page, the front panel):
    the held long poll answers with it and the driver's state follows, with no
    poll and no re-read (DeviceSpecific is flat, so the partial applies)."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)
        try:
            await _connect(d, longpoll=True)
            assert d.get_state("video_source") == "Stream"
            mark = len(d.link.log)
            sim.set_state("video_source", "Input1")
            assert await _until(lambda: d.get_state("video_source") == "Input1")
            sim.set_state("leds_enabled", False)
            assert await _until(lambda: d.get_state("leds_enabled") is False)
            # Only long polls went out: no poll, no re-read.
            assert _without_longpoll(d.link.log[mark:]) == []
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_partial_writes_only_the_properties_present():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)
        try:
            await _connect(d)
            d._apply_change({"DeviceInfo": {"Name": "Lobby Decoder"}})
            assert d.get_state("device_name") == "Lobby Decoder"
            # The rest of the identity is untouched by a partial.
            assert d.get_state("model") == "DM-NVX-D200"
            assert d.get_state("serial_number") == "SIM00000001"
            assert d.get_state("firmware") == "7.1.5259.00090"
            d._apply_change({"DeviceSpecific": {"LedsEnabled": False}})
            assert d.get_state("leds_enabled") is False
            assert d.get_state("video_source") == "Stream"
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_stream_change_reads_the_stream_object_again():
    """A change inside the stream list arrives as a one-element array with no
    index; the driver reads StreamReceive again rather than guessing which
    stream it was."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)
        try:
            await _connect(d, longpoll=True)
            mark = len(d.link.log)
            sim.set_state("stream_location", "rtsp://10.0.0.9:554/live.sdp")
            assert await _until(
                lambda: d.get_state("stream_location") == "rtsp://10.0.0.9:554/live.sdp")
            assert ("GET", "/Device/StreamReceive", 200) in d.link.log[mark:]
        finally:
            await d._close_session()

    asyncio.run(go())


def test_an_output_change_reads_the_av_object_again():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)
        try:
            await _connect(d, longpoll=True)
            sim.set_state("output_connected", True)
            assert await _until(lambda: d.get_state("output_connected") is True)
            sim.set_state("scaler_resolution", "1920x1080@60")
            assert await _until(lambda: d.get_state("scaler_resolution") == "1920x1080@60")
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_burst_of_stream_changes_is_read_again_coalesced():
    """A stream connecting changes several times in a second; the object is
    read once at once and at most once more for the rest of the burst."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)
        try:
            await _connect(d, longpoll=True)
            mark = len(d.link.log)
            for status in ("Connecting", "Stream stopped", "Connecting", "Stream started"):
                sim.set_state("stream_status", status)
                await asyncio.sleep(0.05)
            assert await _until(lambda: d.get_state("stream_status") == "Stream started",
                                timeout=3.0)
            reads = [r for r in d.link.log[mark:] if r[1] == "/Device/StreamReceive"]
            assert 1 <= len(reads) <= 2
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_transmitter_ignores_receiver_objects_in_a_partial():
    """Role narrowing holds for partials: a transmitter never writes a
    receiver key (strict mode would raise) and never reads StreamReceive."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.set_state("device_mode", "Transmitter")
        sim.set_state("model", "DM-NVX-E30")
        d = await _make_driver(sim)
        try:
            await _connect(d)
            mark = len(d.link.log)
            d._apply_change({
                "DeviceSpecific": {"VideoWallMode": 1, "LedsEnabled": False},
                "StreamReceive": {"Streams": [{"Status": "Stream started"}]},
            })
            assert d.get_state("leds_enabled") is False
            await asyncio.sleep(0.1)
            for var in ("video_wall_mode", "rx_initiator", "output_connected",
                        "scaler_resolution"):
                assert not _has(d, var)
            assert not any(r[1] == "/Device/StreamReceive" for r in d.link.log[mark:])
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_role_change_on_the_long_poll_narrows_and_reads_the_new_role():
    """A combo unit that changes role: the surface narrows to the new role and
    the new role's objects are read."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)
        try:
            await _connect(d, longpoll=True)
            assert "route_stream" in d.DRIVER_INFO["commands"]
            sim.set_state("bitrate", 400)
            sim.set_state("device_mode", "Transmitter")
            assert await _until(lambda: d.get_state("bitrate") == 400)
            assert d.get_state("device_mode") == "Transmitter"
            assert "route_stream" not in d.DRIVER_INFO["commands"]
            assert not _has(d, "scaler_resolution")
        finally:
            await d._close_session()

    asyncio.run(go())


def test_the_timeout_answer_is_asked_again_without_a_tight_loop():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.longpoll_hold_s = 0.05
        d = await _make_driver(sim)
        try:
            await _connect(d, longpoll=True)
            mark = len(d.link.log)
            await asyncio.sleep(1.5)
            polls = [r for r in d.link.log[mark:] if r[1] == "/Device/Longpoll"]
            # Each timeout came back at once, so the driver waited a second.
            assert 1 <= len(polls) <= 3
            sim.set_state("video_source", "Input2")
            assert await _until(lambda: d.get_state("video_source") == "Input2", timeout=3.0)
        finally:
            await d._close_session()

    asyncio.run(go())


def test_the_d30s_own_answers_apply():
    """The D30's long-poll answers, byte-exact: the timeout changes nothing,
    the flat partials apply, and the stream and port partials read the object
    again."""
    answers = [
        "longpoll_timeout.txt", "longpoll_leds_off.txt", "longpoll_video_source.txt",
        "longpoll_active_sources.txt", "longpoll_scaler_resolution.txt",
        "longpoll_stream_route.txt", "longpoll_stream_connecting.txt",
    ]
    bodies = [(FIXTURES / name).read_bytes() for name in answers]

    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        link = _Link(sim)
        queue = list(bodies)

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/Device/Longpoll":
                link.log.append(("GET", "/Device/Longpoll", 200))
                if queue:
                    return httpx.Response(
                        200, content=queue.pop(0), headers={"Content-Type": "text/plain"})
                await asyncio.sleep(5)
                return httpx.Response(
                    200, content=bodies[0], headers={"Content-Type": "text/plain"})
            return await link.handle(request)

        d = await _driver_with(handler)
        d.link = link
        try:
            await _connect(d, longpoll=True)
            mark = len(link.log)
            assert await _until(lambda: not queue and d.get_state("active_video_source") == "Stream",
                                timeout=3.0)
            assert d.get_state("leds_enabled") is False
            assert d.get_state("video_source") == "Stream"
            assert d.get_state("active_audio_source") == "PrimaryStreamAudio"
            assert await _until(lambda: any(
                r[1] == "/Device/StreamReceive" for r in link.log[mark:]))
            assert await _until(lambda: any(
                r[1] == "/Device/AudioVideoInputOutput" for r in link.log[mark:]))
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_long_poll_on_an_ended_session_signs_in_again():
    """The session ends (it timed out): the next long poll is redirected to
    the sign-in page, the driver signs in once, and changes keep arriving."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.longpoll_hold_s = 0.1
        d = await _make_driver(sim)
        try:
            await _connect(d, longpoll=True)
            mark = len(d.link.log)
            sim.end_session()
            assert await _until(lambda: ("POST", "/userlogin.html", 200) in d.link.log[mark:],
                                timeout=3.0)
            assert ("GET", "/Device/Longpoll", 301) in d.link.log[mark:]
            sim.set_state("video_source", "Input1")
            assert await _until(lambda: d.get_state("video_source") == "Input1", timeout=3.0)
            sign_ins = [r for r in d.link.log[mark:] if r[:2] == ("POST", "/userlogin.html")]
            assert sign_ins == [("POST", "/userlogin.html", 200)]
            assert d.forced == []
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_change_made_while_the_session_was_down_is_read_after_signing_in():
    """A change the unit made while no session was live may never be
    reported on the new one; once the long poll has signed in again the
    driver reads everything, with polling off."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.longpoll_hold_s = 0.1
        d = await _make_driver(sim)
        try:
            await _connect(d, longpoll=True)
            sim.end_session()
            sim._state["video_source"] = "Input2"  # changed, never reported
            sim._state["stream_status"] = "Stream started"
            assert await _until(lambda: d.get_state("video_source") == "Input2"
                                and d.get_state("stream_status") == "Stream started",
                                timeout=3.0)
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_refused_sign_in_from_the_long_poll_drops_the_device_typed():
    """The password changed on the unit while connected: the long poll's next
    request is refused, the one sign-in again is refused, and the device goes
    offline as auth_failed with no further sign-in."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.longpoll_hold_s = 0.1
        d = await _make_driver(sim)
        try:
            await _connect(d, longpoll=True)
            mark = len(d.link.log)
            sim.inject_error("wrong_password")
            assert await _until(lambda: d.forced, timeout=3.0)
            assert d.forced[0][0] == "auth_failed"
            assert "username and password" in d.forced[0][1]
            await asyncio.sleep(0.3)
            sign_ins = [r for r in d.link.log[mark:] if r[:2] == ("POST", "/userlogin.html")]
            assert sign_ins == [("POST", "/userlogin.html", 403)]
        finally:
            await d._close_session()

    asyncio.run(go())


def test_long_poll_failures_back_off_and_keep_the_device_up():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.longpoll_hold_s = 0.05
        d = await _make_driver(sim)
        try:
            await _connect(d, longpoll=True)
            d.link.reachable = False
            assert await _until(lambda: d.link.refused >= 1, timeout=3.0)
            await asyncio.sleep(0.5)
            assert d.forced == []
            assert d._longpoll_task is not None and not d._longpoll_task.done()
            d.link.reachable = True
            sim.set_state("video_source", "Input2")
            assert await _until(lambda: d.get_state("video_source") == "Input2", timeout=6.0)
        finally:
            await d._close_session()

    asyncio.run(go())


def test_closing_the_session_cancels_the_long_poll():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)
        await _connect(d, longpoll=True)
        task = d._longpoll_task
        assert task is not None and not task.done()
        sim.set_state("stream_status", "Stream started")  # leaves a re-read running
        await asyncio.sleep(0.05)
        await d._close_session()
        assert task.done()
        assert d._longpoll_task is None and d._reread_tasks == {}
        assert d._client is None

    asyncio.run(go())


# ── A session the unit has ended ────────────────────────────────────────────
# The API document answers a timed-out session 403. A D30 on firmware 7.0.5057
# redirects it to the sign-in page (301, Location /userlogin.html) instead.


def test_a_poll_on_an_ended_session_signs_in_again():
    """The redirect is a refused session: sign in once and read the unit.
    Followed, it was a 200 page with no JSON, and every value froze."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)
        try:
            await _connect(d)
            sim.end_session()
            sim.set_state("video_source", "Input1")
            mark = len(d.link.log)
            await d.poll()
            assert d.get_state("video_source") == "Input1"
            assert d.link.log[mark] == ("GET", "/Device/DeviceSpecific", 301)
            sign_ins = [r for r in d.link.log[mark:] if r[:2] == ("POST", "/userlogin.html")]
            assert sign_ins == [("POST", "/userlogin.html", 200)]
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_write_on_an_ended_session_signs_in_again_and_applies():
    """Followed, the redirect made a write report success with nothing
    applied."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)
        try:
            await _connect(d)
            sim.end_session()
            await d.send_command("set_video_source", {"source": "Input1"})
            assert sim.get_state("video_source") == "Input1"
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_write_answered_with_an_error_is_not_reported_as_sent():
    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/Device":
                return httpx.Response(500, text="Internal Server Error")
            return httpx.Response(200, text="")

        d = await _driver_with(handler)
        try:
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await d.send_command("set_video_source", {"source": "Input1"})
        finally:
            await d._close_session()

    asyncio.run(go())


def test_the_documents_403_still_signs_in_again():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.ended_session_status = 403
        d = await _make_driver(sim)
        try:
            await _connect(d)
            sim.end_session()
            sim.set_state("video_source", "Input1")
            mark = len(d.link.log)
            await d.poll()
            assert d.get_state("video_source") == "Input1"
            assert d.link.log[mark] == ("GET", "/Device/DeviceSpecific", 403)
        finally:
            await d._close_session()

    asyncio.run(go())


def test_a_sign_in_redirected_back_to_the_sign_in_page_is_refused():
    """Never count a refused password as accepted: a sign-in answered with a
    redirect back to the sign-in page is auth_failed, sent once."""
    async def go():
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posts.append(request.url.path)
                return httpx.Response(302, headers={"Location": "/userlogin.html"})
            return httpx.Response(200, text="login page")

        d = await _driver_with(handler)
        try:
            with pytest.raises(_ConnectionFaultError) as exc:
                await d._authenticate()
            assert exc.value.fault_code == "auth_failed"
            assert posts == ["/userlogin.html"]
        finally:
            await d._close_session()

    asyncio.run(go())


# ── Lockout protection ──────────────────────────────────────────────────────
# The NVX blocks the controller's source IP after a few failed logins, so the
# driver must never send a login it knows can't succeed.


def test_no_login_post_when_password_empty():
    """A discovery-added NVX starts with an empty password: connect must fail
    with a typed auth fault BEFORE any POST reaches /userlogin.html, so not
    one of the device's failed-login lockout attempts is burned."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        log = []
        d = await _make_driver(sim, {"password": ""}, log=log)
        try:
            with pytest.raises(DRV.ConnectionFaultError) as exc:
                await d._authenticate()
            assert exc.value.fault_code == "auth_failed"
            assert not any(m == "POST" for m, _p, _s in log)  # no login attempt
        finally:
            await d._close_session()

    asyncio.run(go())


def test_factory_fresh_detected_from_redirect_header_without_following():
    """A still-initializing unit answers the /userlogin.html 301 fast but can
    stall for seconds serving the createUser page body. The driver must detect
    the factory 'create admin' state from the 301 Location header WITHOUT
    following it — otherwise the slow page turns a clean auth_failed into a read
    timeout, which classifies as a transient fault and the reconnect loop never
    pauses on auth."""
    async def go():
        followed = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/userlogin.html":
                return httpx.Response(301, headers={"Location": "/createUser.html"})
            if path == "/createUser.html":
                followed.append(path)  # the driver must never reach the slow page
                return httpx.Response(200, text="create admin page")
            return httpx.Response(404)

        cfg = {"host": "sim", "port": 443, "username": "admin", "password": "x",
               "poll_interval": 0}
        d = DRV.CrestronNVXDriver("nvx", cfg, _FakeState(), _FakeEvents())
        d._base_url = "https://sim"
        # A client that follows redirects: the per-call override in
        # _authenticate is what must prevent the follow.
        d._client = httpx.AsyncClient(base_url="https://sim", follow_redirects=True,
                                      transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(DRV.ConnectionFaultError) as exc:
                await d._authenticate()
            assert exc.value.fault_code == "auth_failed"
            assert followed == []  # never followed into the slow createUser page
        finally:
            await d._close_session()

    asyncio.run(go())


def test_reauth_still_401_raises_typed_fault():
    """Credentials that stop working mid-run: after one re-auth retry the
    driver must raise a typed auth fault (device goes offline; the platform
    pauses reconnect) instead of silently returning None and re-attempting a
    login on every poll cycle."""
    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/Device"):
                return httpx.Response(401, text="Unauthorized")
            return httpx.Response(200, text="login page")

        d = await _driver_with(handler)
        try:
            with pytest.raises(_ConnectionFaultError) as exc:
                await d._api_get("/Device/DeviceSpecific")
            assert exc.value.fault_code == "auth_failed"
            # Stashed for the poll-watchdog path, where the raised exception
            # itself isn't what gets classified.
            assert d._last_fault == (
                "auth_failed", "The NVX rejected the configured credentials.")
        finally:
            await d._close_session()

    asyncio.run(go())


# ── Auth fault ──────────────────────────────────────────────────────────────

def test_connect_auth_failure_raises_typed_fault():
    """If DeviceInfo never returns the Device tree, connect raises a typed
    auth fault rather than declaring a phantom connection."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        d = await _make_driver(sim)

        # Make DeviceInfo look like an un-logged-in response (no Device tree).
        async def _empty_get(path):
            if path == "/Device/DeviceInfo":
                return {}
            return await DRV.CrestronNVXDriver._api_get(d, path)
        d._api_get = _empty_get

        try:
            with pytest.raises(_ConnectionFaultError) as exc:
                await d._post_connect()
            assert exc.value.fault_code == "auth_failed"
        finally:
            await d._close_session()

    asyncio.run(go())


# ── Refused sign-ins ────────────────────────────────────────────────────────
# The API's Authentication topic: a valid sign-in answers 200, and invalid
# credentials answer 403 Forbidden. A unit that has blocked the controller's
# address answers the sign-in 403 with "Your IP address is blocked" (seen on
# the bench), even with the right password.


def _sign_ins(log):
    return [row for row in log if row[:2] == ("POST", "/userlogin.html")]


def test_refused_sign_in_raises_auth_failed():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.inject_error("wrong_password")
        log = []
        d = await _make_driver(sim, log=log)
        try:
            with pytest.raises(_ConnectionFaultError) as exc:
                await d._authenticate()
            assert exc.value.fault_code == "auth_failed"
            assert "username and password" in str(exc.value)
            assert _sign_ins(log) == [("POST", "/userlogin.html", 403)]
        finally:
            await d._close_session()

    asyncio.run(go())


def test_blocked_address_reports_the_units_own_sentence():
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.inject_error("ip_blocked")
        d = await _make_driver(sim)
        try:
            with pytest.raises(_ConnectionFaultError) as exc:
                await d._authenticate()
            assert exc.value.fault_code == "auth_failed"
            assert "Your IP address is blocked" in str(exc.value)
            assert "rejected the configured credentials" not in str(exc.value)
        finally:
            await d._close_session()

    asyncio.run(go())


def test_blocked_sentence_is_read_out_of_an_html_page():
    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    403,
                    text="<html><head><title>403 Forbidden</title></head><body>"
                         "<h1>Forbidden</h1><p>Your IP address is blocked.</p>"
                         "</body></html>",
                    headers={"Content-Type": "text/html"},
                )
            return httpx.Response(200, text="login page")

        d = await _driver_with(handler)
        try:
            with pytest.raises(_ConnectionFaultError) as exc:
                await d._authenticate()
            assert "Your IP address is blocked" in str(exc.value)
            assert "<" not in str(exc.value)
        finally:
            await d._close_session()

    asyncio.run(go())


def test_password_changed_mid_session_is_auth_failed_after_one_sign_in():
    """The password is changed on the unit while connected: the poll's
    refused request signs in again once, the sign-in is refused, and the poll
    raises auth_failed, which drops the connection on the first one."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        log = []
        d = await _make_driver(sim, log=log)
        try:
            await _connect(d)
            sim.inject_error("wrong_password")
            mark = len(log)
            with pytest.raises(_ConnectionFaultError) as exc:
                await d.poll()
            assert exc.value.fault_code == "auth_failed"
            assert _without_longpoll(log[mark:]) == [
                ("GET", "/Device/DeviceSpecific", 301),
                ("GET", "/userlogin.html", 200),
                ("POST", "/userlogin.html", 403),
            ]
        finally:
            await d._close_session()

    asyncio.run(go())


def test_expired_session_signs_in_again_and_reads_the_unit():
    """A session that times out (the password still good) is not a refused
    credential: the driver signs in again once and reads on."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        log = []
        d = await _make_driver(sim, log=log)
        try:
            await _connect(d)
            sim.inject_error("session_timeout")
            sim.set_state("video_source", "Input1")
            mark = len(log)
            await d.poll()
            assert d.get_state("video_source") == "Input1"
            assert _sign_ins(log[mark:]) == [("POST", "/userlogin.html", 200)]
        finally:
            await d._close_session()

    asyncio.run(go())


def test_refused_sign_in_after_a_401_is_typed():
    """The 401 path: the session is refused, the driver signs in again, and
    the unit refuses the sign-in. That is auth_failed at once, without a
    second request on the refused session."""
    async def go():
        log = []

        def handler(request: httpx.Request) -> httpx.Response:
            log.append((request.method, request.url.path))
            if request.url.path.startswith("/Device"):
                return httpx.Response(401, text="Unauthorized")
            if request.method == "POST":
                return httpx.Response(403, text="Forbidden")
            return httpx.Response(200, text="login page")

        d = await _driver_with(handler)
        try:
            with pytest.raises(_ConnectionFaultError) as exc:
                await d._api_get("/Device/DeviceSpecific")
            assert exc.value.fault_code == "auth_failed"
            assert "username and password" in str(exc.value)
            assert log == [
                ("GET", "/Device/DeviceSpecific"),
                ("GET", "/userlogin.html"),
                ("POST", "/userlogin.html"),
            ]
        finally:
            await d._close_session()

    asyncio.run(go())


def test_password_with_form_characters_reaches_the_unit_intact():
    """The Authentication topic sends the credentials "in URL-encoded format".
    A password holding the form's own characters must arrive as typed and
    sign in."""
    async def go():
        password = "a&b+c%d=e f"
        sim = SIM.CrestronNvxSimulator("s", {"password": password})
        log = []
        d = await _make_driver(sim, {"password": password}, log=log)
        try:
            await d._authenticate()
            assert sim._session is not None
            assert _sign_ins(log) == [("POST", "/userlogin.html", 200)]
        finally:
            await d._close_session()

    asyncio.run(go())


# ── Simulator ───────────────────────────────────────────────────────────────


def test_simulator_answers_like_the_d30():
    """Sign-in sets the five session cookies once and issues no token; an
    absent session is sent to the sign-in page; the timeout answer is the
    D30's."""
    async def go():
        sim = SIM.CrestronNvxSimulator("s", {})
        sim.longpoll_hold_s = 0.01
        status, body, headers, cookies = sim.dispatch(
            "POST", "/userlogin.html", {}, "login=admin&passwd=x")
        assert (status, body) == (200, "")
        assert "CREST-XSRF-TOKEN" not in headers
        assert [name for name, _v in cookies] == ["userstr", "userid", "iv", "tag", "AuthByPasswd"]
        jar = "; ".join(f"{n}={v}" for n, v in cookies)
        status, *_ = sim.dispatch("GET", "/Device/DeviceSpecific", {"cookie": jar}, "")
        assert status == 200
        assert sim.dispatch("GET", "/Device/DeviceSpecific", {}, "")[:3] == (
            301, "", {"Location": "/userlogin.html"})
        assert await sim.longpoll({"cookie": jar}) == (
            200, {"Device": "Response Timeout"}, {}, [])
        sim.set_state("leds_enabled", False)
        sim.set_state("stream_status", "Stream started")
        assert await sim.longpoll({"cookie": jar}) == (200, {"Device": {
            "DeviceSpecific": {"LedsEnabled": False},
            "StreamReceive": {"Streams": [{"Status": "Stream started",
                                            "Start": True, "Stop": False}]},
        }}, {}, [])

    asyncio.run(go())
