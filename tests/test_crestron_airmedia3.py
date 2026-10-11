"""Driver + simulator tests for crestron_airmedia3 (Crestron AirMedia Series 3
receivers, CresNext API).

Dual-proof round trip: the real driver's httpx client is wired to the real
simulator through httpx.MockTransport, cookies and the anti-forgery header
included, and the long poll is served by the simulator's own held request, so
the driver's long-poll loop runs in-test. Both sides are asserted.

Covers:
  - connect: the URL-encoded sign-in, the session cookies, the
    CREST-XSRF-TOKEN carried on every POST, identity, and the surface narrowed
    to the model the receiver reports (or the one picked before it is reached);
  - every command and every device setting against the simulator, read back
    from the receiver;
  - push: user counts, the USB speaker, the HDMI output, the adaptor list and
    an adaptor's sharing status, the source on the display, the occupancy
    sensor, all arriving on the long poll with polling off;
  - faults: a refused password, a blank one never sent, a blocked address, a
    receiver with no administrator yet, a password changed mid-session, a
    session that timed out, a POST whose token the receiver no longer
    accepts, a refused write, an object this account may not read, and a
    transport error propagating from poll.

openavc.* is stubbed so the community CI stays self-contained (conftest.py
rolls the stubs back). httpx is a real dependency.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import parse_qsl

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
DRIVER_PATH = REPO_ROOT / "streaming" / "crestron_airmedia3.py"
SIM_PATH = REPO_ROOT / "streaming" / "crestron_airmedia3_sim.py"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect lifecycle for a driver that owns its
    session; state and children come from the shared stubs."""

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0
        self._connected = False
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


install_stubs(base_driver=_FakeBaseDriver)
DRV = load_module("crestron_airmedia3_under_test", DRIVER_PATH)
SIM = load_module("crestron_airmedia3_sim_under_test", SIM_PATH)

DRIVER_CLASS = DRV.CrestronAirMedia3Driver
INFO = DRIVER_CLASS.DRIVER_INFO
ADAPTOR_1 = "3eac4f4c-228b-3e02-a621-b82764ab3001"
ADAPTOR_3 = "3eac4f4c-228b-3e02-a621-b82764ab3003"


# ── Harness ──────────────────────────────────────────────────────────────────


class _Link:
    def __init__(self, sim):
        self.sim = sim
        self.reachable = True
        self.requests: list[tuple[str, str, dict, str]] = []

    def posts(self, path: str) -> list[tuple[dict, str]]:
        return [(h, b) for m, p, h, b in self.requests if m == "POST" and p == path]


def _make_handler(link: _Link):
    async def handler(request: httpx.Request) -> httpx.Response:
        if not link.reachable:
            raise httpx.ConnectError("Connection refused")
        path = request.url.path
        headers = dict(request.headers)
        body = request.content.decode("utf-8") if request.content else ""
        link.requests.append((request.method, path, headers, body))
        if request.method == "GET" and path == "/Device/Longpoll":
            status, payload, extra, cookies = await link.sim.longpoll(headers)
        else:
            status, payload, extra, cookies = link.sim.dispatch(
                request.method, path, headers, body,
            )
        out = list((extra or {}).items())
        for name, value in cookies:
            out.append(("set-cookie", f"{name}={value}; Path=/; Secure; HttpOnly"))
        if isinstance(payload, dict):
            return httpx.Response(status, json=payload, headers=out)
        return httpx.Response(status, text=str(payload), headers=out)

    return handler


@pytest.fixture
def mocked_client(monkeypatch):
    """Route every httpx.AsyncClient the driver builds through the link."""
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
    sim = SIM.CrestronAirMedia3Simulator("am-sim", sim_config or {})
    link = _Link(sim)
    cfg = dict(INFO["default_config"])
    cfg.update({"host": "10.0.0.5", "password": "Secret1!", "poll_interval": 0})
    cfg.update(driver_config or {})
    driver = DRIVER_CLASS("am", cfg, StubState(), StubEvents())
    return driver, sim, link


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


async def _connect(driver, link, bind):
    bind(link)
    await driver.connect()
    await _settle()


async def _close(driver, sim):
    await driver.disconnect()
    for event in list(sim._waiters):
        event.set()
    await _settle(3)


def _run(coro):
    asyncio.run(coro)


# ── Declarations ─────────────────────────────────────────────────────────────


def test_metadata_contract():
    for key in ("id", "name", "manufacturer", "category", "version", "author",
                "transport", "description", "source_url"):
        assert INFO.get(key), key
    assert INFO["id"] == "crestron_airmedia3"
    assert INFO["category"] == "streaming"
    assert INFO["min_platform_version"] == "0.37.0"
    assert INFO["verified"] is False
    assert INFO["compatible_models"][0]["confidence"] == "untested"
    assert INFO["config_schema"]["password"]["secret"] is True
    assert INFO["default_config"]["password"] == ""
    assert INFO["config_schema"]["model"]["learned_from"] == "model_family"
    assert "model_family" in INFO["state_variables"]
    for key, sdef in INFO["device_settings"].items():
        assert sdef.get("state_key", key) in INFO["state_variables"], key
    for action in INFO["actions"]:
        assert action.get("command", action["id"]) in INFO["commands"]
    for name, cdef in INFO["commands"].items():
        assert cdef.get("help"), name
        for var in cdef.get("sets", {}):
            assert var in INFO["state_variables"], (name, var)
    # The restart is never one click from a quick action, and asks first.
    assert "reboot" not in {a["id"] for a in INFO["actions"]}
    assert INFO["commands"]["reboot"]["confirm"]


def test_model_family_names():
    family = DRV._model_family
    assert family("AM-3200-WF") == "AM-3200-WF"
    assert family("am-3200-wf-i") == "AM-3200-WF"
    assert family("AM-3200-GV") == "AM-3200"
    assert family("AM-3000-WF-I") == "AM-3000-WF"
    assert family("DM-NVX-D30") == ""
    assert family(None) == ""


# ── Connect ──────────────────────────────────────────────────────────────────


def test_connect_signs_in_reads_identity_and_listens(mocked_client):
    async def go():
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            # One URL-encoded sign-in, after the page that sets TRACKID.
            sign_ins = link.posts("/userlogin.html")
            assert len(sign_ins) == 1
            headers, body = sign_ins[0]
            assert dict(parse_qsl(body)) == {"login": "admin", "passwd": "Secret1!"}
            assert headers["content-type"] == "application/x-www-form-urlencoded"
            assert driver._xsrf == sim._session["xsrf"]
            # Identity and the full AM-3200-WF surface.
            assert driver.get_state("model") == "AM-3200-WF"
            assert driver.get_state("model_family") == "AM-3200-WF"
            assert driver.get_state("firmware") == "3.0800.0157"
            assert driver.get_state("serial_number") == "2234NEJ01234"
            assert driver.get_state("presentation_status") == "Idle"
            assert driver.get_state("connected_users") == 0
            assert driver.get_state("login_code") == "12345678"
            assert driver.get_state("routing_mode") == "Auto"
            assert driver.get_state("peripheral_volume") == 50
            assert driver.get_state("display_connected") is True
            assert driver.get_state("output_disabled") is False
            assert driver.get_state("hdmi_input_sync") is False
            assert driver.get_state("occupancy") == "Vacant"
            assert driver.get_state("paired_adaptors") == 2
            assert sorted(driver.list_children("adaptor")) == sorted(
                [ADAPTOR_1, "3eac4f4c-228b-3e02-a621-b82764ab3002"])
            child = driver.get_child_state("adaptor", ADAPTOR_1)
            assert child["label"] == "AirMedia TX3-1"
            assert child["serial_number"] == "2233GSP00001"
            assert child["connection_status"] == "Online"
            assert child["online"] is True
            # Every request after the sign-in carried the session cookies.
            gets = [h for m, p, h, _ in link.requests if m == "GET" and p.startswith("/Device")]
            assert gets and all("userid=" in h.get("cookie", "") for h in gets)
            # The long poll is open.
            assert await _until(lambda: bool(sim._waiters))
        finally:
            await _close(driver, sim)

    _run(go())


def test_model_narrowing_follows_the_reported_model(mocked_client):
    async def go():
        driver, sim, link = _make(sim_config={"model": "AM-3000-WF"})
        await _connect(driver, link, mocked_client)
        try:
            assert driver.get_state("model_family") == "AM-3000-WF"
            cmds = driver.DRIVER_INFO["commands"]
            assert "set_peripheral_volume" not in cmds
            assert "start_adaptor_pairing" in cmds
            assert "conferencing_enabled" not in driver.DRIVER_INFO["device_settings"]
            assert "peripheral_volume" not in driver.DRIVER_INFO["state_variables"]
            assert "hdmi_input_sync" not in driver.DRIVER_INFO["state_variables"]
            assert {a["id"] for a in driver.DRIVER_INFO["actions"]} == {
                "disconnect_all_users", "disable_output", "enable_output"}
            with pytest.raises(RuntimeError, match="AM-3000-WF has no Mute USB Speaker"):
                await driver.send_command("mute_peripheral")
        finally:
            await _close(driver, sim)

    async def am3200():
        driver, sim, link = _make(sim_config={"model": "AM-3200"})
        await _connect(driver, link, mocked_client)
        try:
            assert "adaptor" not in driver.DRIVER_INFO["child_entity_types"]
            assert "start_adaptor_pairing" not in driver.DRIVER_INFO["commands"]
            assert "paired_adaptors" not in driver.DRIVER_INFO["state_variables"]
            assert "set_peripheral_volume" in driver.DRIVER_INFO["commands"]
            # Nothing asked for the adaptor object this model does not have.
            assert not [p for _m, p, _h, _b in link.requests if p == "/Device/AirMediaTx3"]
        finally:
            await _close(driver, sim)

    _run(go())
    _run(am3200())


def test_picked_model_narrows_before_the_receiver_is_reached():
    driver, _sim, _link = _make(driver_config={"model": "AM-3200"})
    assert "start_adaptor_pairing" not in driver.DRIVER_INFO["commands"]
    assert "adaptor" not in driver.DRIVER_INFO["child_entity_types"]
    unpicked, _s, _l = _make()
    assert unpicked.DRIVER_INFO is INFO


def test_an_unknown_model_keeps_every_command(mocked_client):
    async def go():
        driver, sim, link = _make()
        sim._state["model"] = "AM-9999"
        await _connect(driver, link, mocked_client)
        try:
            assert driver.get_state("model") == "AM-9999"
            assert driver.get_state("model_family") == ""
            assert driver.DRIVER_INFO["commands"].keys() == INFO["commands"].keys()
        finally:
            await _close(driver, sim)

    _run(go())


# ── Commands and settings, read back from the receiver ──────────────────────


def test_every_command_round_trips(mocked_client):
    async def go():
        driver, sim, link = _make()
        sim.set_state("connected_users", 3)
        sim.set_state("presenting_users", 2)
        await _connect(driver, link, mocked_client)
        try:
            assert driver.get_state("presentation_status") == "Presenting"
            assert driver.get_state("presenting_users") == 2

            await driver.send_command("disconnect_all_users")
            assert sim.get_state("connected_users") == 0
            assert driver.get_state("connected_users") == 0
            assert driver.get_state("presenting_users") == 0
            assert driver.get_state("presentation_status") == "Idle"

            await driver.send_command("disable_output")
            assert sim.get_state("output_disabled") is True
            assert driver.get_state("output_disabled") is True
            await driver.send_command("enable_output")
            assert driver.get_state("output_disabled") is False

            await driver.send_command("set_peripheral_volume", {"level": 72})
            assert sim.get_state("peripheral_volume") == 72
            assert driver.get_state("peripheral_volume") == 72
            await driver.send_command("mute_peripheral")
            assert driver.get_state("peripheral_mute") is True
            await driver.send_command("unmute_peripheral")
            assert driver.get_state("peripheral_mute") is False

            await driver.send_command("start_adaptor_pairing")
            assert driver.get_state("adaptor_pairing_status") == "In_Progress"

            await driver.send_command("reboot")
            assert sim.get_state("reboot_reason") == "manual"

            # Every POST carried the session's anti-forgery token.
            posts = link.posts("/Device")
            assert len(posts) == 8
            assert all(h.get("x-crest-xsrf-token") == sim._session["xsrf"] for h, _ in posts)
        finally:
            await _close(driver, sim)

    _run(go())


def test_every_device_setting_round_trips(mocked_client):
    cases = [
        ("login_code_mode", "Fixed", "login_code_mode", "Fixed"),
        ("fixed_login_code", "87654321", "fixed_login_code", "87654321"),
        ("show_login_code", False, "show_login_code", False),
        ("system_mode", "Custom", "system_mode", "Custom"),
        ("routing_mode", "AirMediaOnly", "routing_mode", "AirMediaOnly"),
        ("miracast_enabled", False, "miracast_enabled", False),
        ("canvas_enabled", False, "canvas_enabled", False),
        ("airmedia_discovery", False, "airmedia_discovery", False),
        ("inactivity_timeout", 20, "inactivity_timeout", 20),
        ("conferencing_enabled", False, "conferencing_enabled", False),
    ]
    assert {c[0] for c in cases} == set(INFO["device_settings"])

    async def go():
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            for key, value, state_key, expected in cases:
                await driver.set_device_setting(key, value)
                assert driver.get_state(state_key) == expected, key
            # With nobody connected, the new code and mode are in use at once.
            assert driver.get_state("login_code") == "87654321"
            assert driver.get_state("active_login_code_mode") == "Fixed"
            with pytest.raises(ValueError, match="eight digits"):
                await driver.set_device_setting("fixed_login_code", "1234")
            with pytest.raises(ValueError, match="1 to 60"):
                await driver.set_device_setting("inactivity_timeout", 90)
        finally:
            await _close(driver, sim)

    _run(go())


def test_a_refused_write_raises_the_receivers_reason(mocked_client):
    async def go():
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            with pytest.raises(RuntimeError, match="value out of range"):
                await driver._post({"AirMedia": {"InactivityTimeout": 0}}, "the timeout")
            with pytest.raises(RuntimeError, match="read only"):
                await driver._post({"AirMedia": {"ActiveLoginCode": "1"}}, "the code")
        finally:
            await _close(driver, sim)

    _run(go())


# ── Push on the long poll (polling off) ──────────────────────────────────────


def test_changes_arrive_on_the_long_poll(mocked_client):
    async def go():
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            polls_before = len([p for _m, p, _h, _b in link.requests if p == "/Device/AirMedia"])

            sim.set_state("connected_users", 2)
            sim.set_state("presenting_users", 1)
            assert await _until(lambda: driver.get_state("presenting_users") == 1)
            assert driver.get_state("connected_users") == 2
            assert driver.get_state("presentation_status") == "Presenting"

            sim.set_state("peripheral_volume", 30)
            assert await _until(lambda: driver.get_state("peripheral_volume") == 30)
            sim.set_state("camera_in_use", True)
            assert await _until(lambda: driver.get_state("camera_in_use") is True)

            sim.set_state("output_disabled", True)
            assert await _until(lambda: driver.get_state("output_disabled") is True)
            sim.set_state("display_connected", False)
            assert await _until(lambda: driver.get_state("display_connected") is False)
            sim.set_state("hdmi_input_sync", True)
            assert await _until(lambda: driver.get_state("hdmi_input_sync") is True)

            sim.set_state("active_source", "HDMI")
            assert await _until(lambda: driver.get_state("active_sources") == "Wired")
            sim.set_state("active_source", "AirMedia")
            assert await _until(lambda: driver.get_state("active_sources") == "Presenter 1")

            sim.set_state("adaptor_sharing", 1)
            assert await _until(lambda: driver.get_child_state(
                "adaptor", ADAPTOR_1).get("screen_share_status") == "Playing")
            sim.set_state("adaptor_count", 3)
            assert await _until(lambda: ADAPTOR_3 in driver.list_children("adaptor"))
            assert driver.get_state("paired_adaptors") == 3
            sim.set_state("adaptor_count", 1)
            assert await _until(lambda: driver.list_children("adaptor") == [ADAPTOR_1])

            sim.set_state("occupancy", "Occupied")
            assert await _until(lambda: driver.get_state("occupancy") == "Occupied")

            # None of it came from a poll.
            polls_after = len([p for _m, p, _h, _b in link.requests if p == "/Device/AirMedia"])
            assert polls_after == polls_before
        finally:
            await _close(driver, sim)

    _run(go())


def test_a_long_poll_that_times_out_is_asked_again(mocked_client, monkeypatch):
    async def go():
        monkeypatch.setattr(SIM, "_HOLD_S", 0.05)
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            await asyncio.sleep(1.3)
            longpolls = [p for _m, p, _h, _b in link.requests if p == "/Device/Longpoll"]
            assert len(longpolls) >= 2
            sim.set_state("peripheral_mute", True)
            assert await _until(lambda: driver.get_state("peripheral_mute") is True)
        finally:
            await _close(driver, sim)

    _run(go())


# ── Faults ───────────────────────────────────────────────────────────────────


def test_a_refused_password_is_auth_failed_after_one_sign_in(mocked_client):
    async def go():
        driver, sim, link = _make(sim_config={"password": "Other1!"})
        mocked_client(link)
        with pytest.raises(ConnectionFaultError) as err:
            await driver.connect()
        assert err.value.fault_code == "auth_failed"
        assert str(err.value) == "The AirMedia refused the username and password."
        assert len(link.posts("/userlogin.html")) == 1

    _run(go())


def test_a_blank_password_is_never_sent(mocked_client):
    async def go():
        driver, sim, link = _make(driver_config={"password": ""})
        mocked_client(link)
        with pytest.raises(ConnectionFaultError) as err:
            await driver.connect()
        assert err.value.fault_code == "auth_failed"
        assert link.posts("/userlogin.html") == []

    _run(go())


def test_a_blocked_address_says_so(mocked_client):
    async def go():
        driver, sim, link = _make()
        sim.inject_error("ip_blocked")
        mocked_client(link)
        with pytest.raises(ConnectionFaultError) as err:
            await driver.connect()
        assert err.value.fault_code == "auth_failed"
        assert "Your IP address is blocked" in str(err.value)

    _run(go())


def test_a_receiver_with_no_administrator_yet(mocked_client):
    async def go():
        driver, sim, link = _make()
        sim.inject_error("no_admin_account")
        mocked_client(link)
        with pytest.raises(ConnectionFaultError) as err:
            await driver.connect()
        assert err.value.fault_code == "auth_failed"
        assert "no administrator account yet" in str(err.value)
        assert link.posts("/userlogin.html") == []

    _run(go())


def test_password_changed_mid_session_is_auth_failed(mocked_client):
    async def go():
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            sim.inject_error("wrong_password")
            with pytest.raises(ConnectionFaultError) as err:
                await driver.poll()
            assert err.value.fault_code == "auth_failed"
            # The first sign-in, then exactly one more, refused.
            assert len(link.posts("/userlogin.html")) == 2
            with pytest.raises(ConnectionFaultError):
                await driver.poll()
            assert len(link.posts("/userlogin.html")) == 2
        finally:
            await _close(driver, sim)

    _run(go())


def test_a_timed_out_session_signs_in_again(mocked_client):
    async def go():
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            sim.inject_error("session_timeout")
            sim._state["peripheral_volume"] = 61  # changed without a report
            await driver.poll()
            assert driver.get_state("peripheral_volume") == 61
            assert len(link.posts("/userlogin.html")) == 2
        finally:
            await _close(driver, sim)

    _run(go())


def _redirect_refused_sessions(sim):
    """Refuse a session the way a DM-NVX-D30 on the same web server did: a
    301 to the sign-in page with an empty body, where the API document says
    403."""
    dispatch, longpoll = sim.dispatch, sim.longpoll

    def redirect(answer, path):
        status, payload, extra, cookies = answer
        if status == 403 and path.startswith("/Device"):
            return 301, "", {"Location": "/userlogin.html"}, []
        return answer

    def dispatch_redirecting(method, path, headers, body):
        return redirect(dispatch(method, path, headers, body), path)

    async def longpoll_redirecting(headers):
        return redirect(await longpoll(headers), "/Device/Longpoll")

    sim.dispatch = dispatch_redirecting
    sim.longpoll = longpoll_redirecting


def test_a_session_redirected_to_the_sign_in_page_signs_in_again(mocked_client):
    """Followed, the redirect is a 200 page with no JSON: the values froze
    and nothing signed in again."""
    async def go():
        driver, sim, link = _make()
        _redirect_refused_sessions(sim)
        await _connect(driver, link, mocked_client)
        try:
            sim.inject_error("session_timeout")
            sim._state["peripheral_volume"] = 61  # changed without a report
            await driver.poll()
            assert driver.get_state("peripheral_volume") == 61
            assert len(link.posts("/userlogin.html")) == 2
            assert driver.forced == []
        finally:
            await _close(driver, sim)

    _run(go())


def test_a_write_on_a_redirected_session_signs_in_again_and_applies(mocked_client):
    """Followed, the redirect made the write look sent with nothing applied."""
    async def go():
        driver, sim, link = _make()
        _redirect_refused_sessions(sim)
        await _connect(driver, link, mocked_client)
        try:
            sim.inject_error("session_timeout")
            await driver.send_command("set_peripheral_volume", {"level": 22})
            assert sim.get_state("peripheral_volume") == 22
            assert len(link.posts("/userlogin.html")) == 2
        finally:
            await _close(driver, sim)

    _run(go())


def test_a_long_poll_redirected_to_the_sign_in_page_signs_in_again(mocked_client, monkeypatch):
    async def go():
        monkeypatch.setattr(SIM, "_HOLD_S", 0.05)
        driver, sim, link = _make()
        _redirect_refused_sessions(sim)
        await _connect(driver, link, mocked_client)
        try:
            sim.inject_error("session_timeout")
            assert await _until(lambda: len(link.posts("/userlogin.html")) == 2, timeout=3.0)
            sim.set_state("peripheral_mute", True)
            assert await _until(lambda: driver.get_state("peripheral_mute") is True, timeout=3.0)
            assert driver.forced == []
        finally:
            await _close(driver, sim)

    _run(go())


def test_a_change_made_while_the_session_was_down_is_read_after_signing_in(
        mocked_client, monkeypatch):
    """A new session starts with nothing to report, so a change made while no
    session was live is read once the long poll has signed in again, with
    polling off."""
    async def go():
        monkeypatch.setattr(SIM, "_HOLD_S", 0.05)
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            sim.inject_error("session_timeout")
            sim._state["peripheral_volume"] = 37  # changed, never reported
            assert await _until(lambda: driver.get_state("peripheral_volume") == 37,
                                timeout=3.0)
        finally:
            await _close(driver, sim)

    _run(go())


def test_a_sign_in_redirected_back_to_the_sign_in_page_is_refused(mocked_client):
    async def go():
        driver, sim, link = _make()
        original = sim.dispatch

        def redirect_sign_in(method, path, headers, body):
            if method == "POST" and path == "/userlogin.html":
                return 302, "", {"Location": "/userlogin.html"}, []
            return original(method, path, headers, body)

        sim.dispatch = redirect_sign_in
        mocked_client(link)
        with pytest.raises(ConnectionFaultError) as err:
            await driver.connect()
        assert err.value.fault_code == "auth_failed"
        assert len(link.posts("/userlogin.html")) == 1

    _run(go())


def test_a_post_with_a_stale_token_signs_in_again_and_resends(mocked_client):
    async def go():
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            driver._xsrf = "stale"
            await driver.send_command("set_peripheral_volume", {"level": 15})
            assert sim.get_state("peripheral_volume") == 15
            assert len(link.posts("/userlogin.html")) == 2
            last_headers, _ = link.posts("/Device")[-1]
            assert last_headers["x-crest-xsrf-token"] == sim._session["xsrf"]
        finally:
            await _close(driver, sim)

    _run(go())


def test_an_object_this_account_may_not_read_is_named(mocked_client):
    async def go():
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            original = sim.dispatch

            def refuse_sources(method, path, headers, body):
                if path.startswith("/Device/SourceSelectionConfiguration"):
                    return 403, "Forbidden", {}, []
                return original(method, path, headers, body)

            sim.dispatch = refuse_sources
            await driver.poll()
            assert driver.get_state("last_error") == (
                "This account may not read: SourceSelectionConfiguration.")
            assert driver.get_state("connected") is True
            assert driver.forced == []
        finally:
            await _close(driver, sim)

    _run(go())


def test_a_transport_error_propagates_from_poll(mocked_client):
    async def go():
        driver, sim, link = _make()
        await _connect(driver, link, mocked_client)
        try:
            link.reachable = False
            with pytest.raises(httpx.TransportError):
                await driver.poll()
        finally:
            await _close(driver, sim)

    _run(go())
