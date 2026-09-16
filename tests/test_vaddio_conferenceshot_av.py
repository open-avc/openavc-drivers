"""vaddio_conferenceshot_av — the Telnet reply parser and the Enable Telnet wizard.

Two things are worth pinning here, and they are the two the driver exists for.

**Reply parsing.** The camera's shell answers a command with an echo, a body,
and usually but not always a terminator, and the replies do not say what they
answer: all seven audio channels reply ``volume: -9.0 dB`` and ``mute: off``,
``video mute get`` is byte-identical to an audio mute, and the single-axis
position reads are bare numbers. The parser's job is to turn one block into a
verdict plus its body lines, so the caller can attach it to the question it
asked. Both failure shapes are real captures from firmware 1.7.2: a syntax
error carries no ``ERROR`` token, and a refusal carries a sentence before one.

**The Enable Telnet wizard.** Out of the box this camera has Telnet switched
off, so the driver cannot connect at all until somebody turns it on. The
action does what the Security page does. Every one of its unhappy paths is
pinned below, because a wizard that fails without saying what to do next is
worse than no wizard: the operator is then stuck with a button that did
nothing and no idea why. Each message has to name what happened AND leave them
able to finish by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _platform_stubs import (
    StubEvents,
    StubState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

install_stubs()
DRV = load_module(
    "vaddio_conferenceshot_av_under_test",
    REPO_ROOT / "cameras" / "vaddio_conferenceshot_av.py",
)

HOST = "10.1.2.3"


def make_driver(**config):
    cfg = {"host": HOST, "port": 23, "username": "admin", "password": "pw"}
    cfg.update(config)
    return DRV.VaddioConferenceShotAVDriver(
        device_id="cam", config=cfg, state=StubState(), events=StubEvents(),
    )


async def _noop_progress(*args, **kwargs):
    return None


# --------------------------------------------------------------------------
# Reply parsing — the shapes the camera actually sends
# --------------------------------------------------------------------------


def test_ok_reply_drops_the_echo_and_keeps_the_body():
    reply = DRV.parse_reply(
        "camera led get\r\nLED:    off\r\nOK", "camera led get"
    )
    assert reply.ok
    assert reply.lines == ["LED:    off"]


def test_a_syntax_error_is_a_failure_even_though_it_carries_no_ERROR_token():
    # Captured from 1.7.2. This is what every command the camera does not have
    # answers, and a fire-and-forget send reports it as success.
    reply = DRV.parse_reply(
        "camera ccu scene recall factory 1\r\n"
        "Syntax error: Unknown or incomplete command\r\n",
        "camera ccu scene recall factory 1",
    )
    assert not reply.ok
    assert "Syntax error" in reply.error


def test_a_refusal_keeps_the_camera_s_own_sentence_as_the_reason():
    reply = DRV.parse_reply(
        "camera focus near 4\r\n"
        "cannot perform manual focus operation while auto focus enabled\r\n"
        "ERROR",
        "camera focus near 4",
    )
    assert not reply.ok
    assert reply.error == (
        "cannot perform manual focus operation while auto focus enabled"
    )


def test_a_reply_with_no_terminator_at_all_is_a_success():
    # `camera sensor get` and `system serial-number` answer neither OK nor
    # ERROR. Treating a missing terminator as failure would break both.
    reply = DRV.parse_reply('camera sensor get\r\n"So7100"', "camera sensor get")
    assert reply.ok
    assert reply.lines == ['"So7100"']


def test_the_echo_is_dropped_even_when_tab_completion_mangled_it():
    # The shell eats the spaces between tokens it could not complete, so the
    # echo is not a faithful copy of what was sent.
    reply = DRV.parse_reply(
        "camera ccu scenerecallfactory1\r\n"
        "Syntax error: Unknown or incomplete command\r\n",
        "camera ccu scene recall factory 1",
    )
    assert not reply.ok


def test_telnet_negotiation_and_ansi_colour_are_stripped_before_parsing():
    raw = (
        b"\xff\xfd\x01\xff\xfb\x03"          # IAC DO ECHO / WILL SGA
        b"\x1b[1;31mstandby:        off\x1b[0m\x07"
    )
    assert DRV._strip_telnet(raw) == b"standby:        off"


def test_a_block_is_framed_on_the_prompt_not_the_line_delimiter():
    buf = b"camera led get\r\nLED:    off\r\nOK\r\n> camera standby get\r\n"
    block, remaining = DRV._frame_on_prompt(buf)
    assert block == b"camera led get\r\nLED:    off\r\nOK"
    assert remaining == b"camera standby get\r\n"


def test_an_incomplete_block_yields_nothing_and_keeps_the_buffer():
    buf = b"camera led get\r\nLED:    off\r\n"
    block, remaining = DRV._frame_on_prompt(buf)
    assert block is None
    assert remaining == buf


# --------------------------------------------------------------------------
# Enable Telnet — every failure has to say what to do next
# --------------------------------------------------------------------------


def _assert_actionable(message: str) -> None:
    """Every failure names the manual route, so the operator is never stuck.

    The wizard is a convenience over a setting on a web page. If it cannot do
    the job, the message has to hand back the job.
    """
    assert "Security" in message, message
    assert HOST in message, message
    assert "Telnet" in message, message


@pytest.mark.asyncio
async def test_no_ip_configured_says_so_and_asks_for_one():
    drv = make_driver(host="")
    res = await drv.run_setup_action("enable_telnet", {"password": "pw"},
                                     _noop_progress)
    assert res["success"] is False
    assert "no IP address" in res["message"]


@pytest.mark.asyncio
async def test_a_blank_password_is_refused_before_any_network_call():
    drv = make_driver()

    async def _fail(*a, **k):
        raise AssertionError("must not touch the network with no password")

    drv._find_web_interface = _fail
    res = await drv.run_setup_action("enable_telnet", {"username": "admin",
                                                       "password": ""},
                                     _noop_progress)
    assert res["success"] is False
    _assert_actionable(res["message"])


@pytest.mark.asyncio
async def test_an_unreachable_camera_names_the_address_and_dhcp():
    drv = make_driver()

    async def _none(host):
        return None

    drv._find_web_interface = _none
    res = await drv.run_setup_action("enable_telnet", {"username": "admin",
                                                       "password": "pw"},
                                     _noop_progress)
    assert res["success"] is False
    assert HOST in res["message"]
    assert "DHCP" in res["message"]


def _wire(drv, *, login=None, patch=None, port_open=True):
    """Stand in for the camera's web interface and its control port.

    ``port_open`` may be a list to answer differently across calls, which is
    how the real sequence looks: shut before the change, open after it.
    """
    async def _find(host):
        return "https"

    async def _request(method, scheme, host, path, body=None, cookie=""):
        if path.endswith("/session"):
            return dict(login or {"status": 201, "cookie": "session=1"})
        return dict(patch or {"status": 204})

    answers = list(port_open) if isinstance(port_open, list) else None

    async def _answers(host, port, timeout=5.0):
        if answers is not None:
            return answers.pop(0) if answers else True
        return port_open

    drv._find_web_interface = _find
    drv._web_request = _request
    drv._port_answers = _answers


@pytest.mark.asyncio
async def test_wrong_credentials_point_at_the_web_interface_account():
    drv = make_driver()
    _wire(drv, login={"status": 401})
    res = await drv.run_setup_action("enable_telnet", {"username": "admin",
                                                       "password": "nope"},
                                     _noop_progress)
    assert res["success"] is False
    assert "username and password" in res["message"]
    _assert_actionable(res["message"])


@pytest.mark.asyncio
async def test_a_non_admin_account_is_told_which_account_to_use():
    drv = make_driver()
    _wire(drv, patch={"status": 403})
    res = await drv.run_setup_action("enable_telnet", {"username": "user",
                                                       "password": "pw"},
                                     _noop_progress)
    assert res["success"] is False
    assert "admin account" in res["message"]
    _assert_actionable(res["message"])


@pytest.mark.asyncio
async def test_firmware_without_the_endpoint_hands_the_job_back():
    drv = make_driver()
    _wire(drv, patch={"status": 404})
    res = await drv.run_setup_action("enable_telnet", {"username": "admin",
                                                       "password": "pw"},
                                     _noop_progress)
    assert res["success"] is False
    assert "firmware" in res["message"]
    _assert_actionable(res["message"])


@pytest.mark.asyncio
async def test_a_transport_level_failure_reports_the_reason_it_was_given():
    drv = make_driver()
    _wire(drv, login={"error": "certificate verify failed"})
    res = await drv.run_setup_action("enable_telnet", {"username": "admin",
                                                       "password": "pw"},
                                     _noop_progress)
    assert res["success"] is False
    assert "certificate verify failed" in res["message"]
    _assert_actionable(res["message"])


@pytest.mark.asyncio
async def test_accepted_but_port_still_shut_blames_the_path_not_the_camera():
    # The camera said yes and 23 is still closed, so the remaining suspect is
    # something between this server and the camera. Saying "enable Telnet"
    # again here would send the operator back to a switch already set.
    drv = make_driver()
    _wire(drv, port_open=False)
    res = await drv.run_setup_action("enable_telnet", {"username": "admin",
                                                       "password": "pw"},
                                     _noop_progress)
    assert res["success"] is False
    assert "blocking port 23" in res["message"]
    assert "accepted the change" in res["message"]


@pytest.mark.asyncio
async def test_the_happy_path_reconnects_and_says_what_happened():
    drv = make_driver()
    _wire(drv, port_open=[False, True])
    reconnected = []
    drv.request_reconnect = lambda: reconnected.append(True)

    async def _reconnect():
        reconnected.append(True)

    drv.request_reconnect = _reconnect
    res = await drv.run_setup_action("enable_telnet", {"username": "admin",
                                                       "password": "pw"},
                                     _noop_progress)
    assert res["success"] is True
    assert "port 23" in res["message"]
    assert reconnected == [True]


@pytest.mark.asyncio
async def test_pressing_it_when_telnet_is_already_on_does_not_claim_a_change():
    # Reporting "Telnet is on" here would read as "I fixed it" and send the
    # operator looking for a change that never happened. The camera is offline
    # for some other reason and the message has to say so.
    drv = make_driver()
    _wire(drv)

    async def _reconnect():
        return None

    drv.request_reconnect = _reconnect
    res = await drv.run_setup_action("enable_telnet", {"username": "admin",
                                                       "password": "pw"},
                                     _noop_progress)
    assert res["success"] is True
    assert "already on" in res["message"]
    assert "Nothing needed changing" in res["message"]


@pytest.mark.asyncio
async def test_progress_is_reported_so_the_wizard_is_not_a_blank_wait():
    drv = make_driver()
    _wire(drv)

    async def _reconnect():
        return None

    drv.request_reconnect = _reconnect
    steps = []

    async def _progress(step, pct=None):
        steps.append(step)

    await drv.run_setup_action("enable_telnet", {"username": "admin",
                                                 "password": "pw"}, _progress)
    assert len(steps) >= 3


@pytest.mark.asyncio
async def test_an_unknown_action_id_is_not_swallowed():
    drv = make_driver()
    with pytest.raises(NotImplementedError):
        await drv.run_setup_action("something_else", {}, _noop_progress)


# --------------------------------------------------------------------------
# The offline reason a closed port 23 produces
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_closed_control_port_blames_telnet_and_names_the_button():
    drv = make_driver()

    async def _shut(host, port, timeout=5.0):
        return False

    drv._port_answers = _shut
    with pytest.raises(Exception) as excinfo:
        await drv._pre_connect()
    message = str(excinfo.value)
    assert "port 23" in message
    assert "Enable Telnet button" in message
    _assert_actionable(message)


@pytest.mark.asyncio
async def test_a_blank_credential_is_an_auth_failure_not_a_network_one():
    drv = make_driver(password="")
    with pytest.raises(Exception) as excinfo:
        await drv._pre_connect()
    assert getattr(excinfo.value, "fault_code", "") == "auth_failed"


@pytest.mark.asyncio
async def test_an_open_control_port_lets_the_connect_proceed():
    drv = make_driver()

    async def _open(host, port, timeout=5.0):
        return True

    drv._port_answers = _open
    await drv._pre_connect()   # must not raise
