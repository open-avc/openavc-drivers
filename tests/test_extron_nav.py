"""Driver + simulator tests for extron_nav (Extron NAVigator System Manager).

No NAV hardware on hand, so correctness is proven three ways: unit tests on the
pieces the user guide is specific about, a dual-proof round trip wiring the real
driver to the real simulator through an in-memory link, and command-surface
consistency (every declared command has a branch in ``send_command`` -- a Python
driver that falls through returns success indistinguishable from a command that
worked, which no gate catches).

What is specific to THIS device, and so is what these tests are mostly about:

  - **Two session settings decide whether anything else works.** Echo is ON at
    connect, so until the driver turns it off the NAVigator interleaves a copy
    of every command with its reply. Verbose starts at 0, where an endpoint
    notice is a bare ``1`` -- indistinguishable from the answer to a question.
    Both are asserted on the simulator, not just on the driver.
  - **The roster is a 4096-position digit string**, sparse in practice. A
    driver that read it as a dense 1..N list would pass against a full system
    and mis-map every endpoint on a real one, so the simulator's roster has
    gaps (encoders 1, 2, 3, 17, 101).
  - **The inventory digits carry two different kinds of down** -- offline, and
    present-but-not-connected -- which map onto two different child fault
    codes rather than one boolean.
  - **An unsolicited frame must never satisfy a request.** The NAVigator pushes
    endpoint notices on the control connection, so one will eventually land
    between a request and its reply.
  - **Encapsulation carries an embedded CR** inside its braces, which is why
    neither side can frame on CR alone.

The driver and simulator are loaded with the ``openavc.*`` imports stubbed so
the community CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubBaseDriver,
    StubEvents,
    StubState,
    StubTCPSimulator,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "extron_nav.py"
SIM_PATH = REPO_ROOT / "switchers" / "extron_nav_sim.py"

ESC = "\x1b"
CR = "\r"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect for a raw-pipe driver: the transport
    is the in-memory link the test supplies; state, children and the watchdog
    come from the shared stubs."""

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0
        self.transport = None
        self._connected = False
        self.transport_factory = None

    async def _pre_connect(self):
        return None

    async def _post_connect(self):
        return None

    async def _initial_sync(self):
        return None

    async def _close_session(self):
        return None

    async def connect(self):
        await self._close_session()
        await self._pre_connect()
        self.transport = self.transport_factory(self)
        try:
            await self._post_connect()
        except Exception:
            transport, self.transport = self.transport, None
            if transport is not None:
                await transport.close()
            await self._close_session()
            self._connected = False
            raise
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        await self._initial_sync()

    async def disconnect(self):
        self._stop_health_loop()
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
        self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")

    def _handle_transport_disconnect(self):
        self._connected = False
        self.set_state("connected", False)


class _FakeTCPSimulator(StubTCPSimulator):
    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self.name = "sim"


install_stubs(
    {"openavc.simulator.tcp_simulator": {"TCPSimulator": _FakeTCPSimulator}},
    base_driver=_FakeBaseDriver,
)
DRV = load_module("extron_nav_under_test", DRIVER_PATH)
SIMM = load_module("extron_nav_sim_under_test", SIM_PATH)

Driver = DRV.ExtronNavDriver
Simulator = SIMM.ExtronNavSimulator


# ── In-memory link ──────────────────────────────────────────────────────────

class _Link:
    """Stands in for the SSH/TCP byte pipe: bytes the driver sends reach the
    simulator's handle_command, and whatever it returns -- or pushes -- goes
    back through the driver's own ``on_data_received``, which is what puts the
    line framing under test rather than around it.
    """

    def __init__(self, driver, sim, *, silent=False, drip=False):
        self.driver = driver
        self.sim = sim
        self.connected = True
        self.sent: list[str] = []
        self.silent = silent
        self.drip = drip
        sim.push_targets.append(self)

    async def send(self, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError("link closed")
        self.sent.append(bytes(data).decode("latin-1"))
        if self.silent:
            return
        reply = self.sim.handle_command(bytes(data))
        if reply:
            await self.deliver(reply)

    async def deliver(self, data: bytes) -> None:
        if not self.connected:
            return
        chunks = ([data[i:i + 1] for i in range(len(data))] if self.drip
                  else [data])
        for chunk in chunks:
            await self.driver.on_data_received(chunk)

    async def close(self) -> None:
        self.connected = False


async def _pair(*, silent=False, drip=False, config=None):
    """A connected driver + simulator sharing one in-memory link."""
    sim = Simulator("sim-nav", {})
    state, events = StubState(), StubEvents()
    cfg = {"host": "127.0.0.1", "port": 22023, "transport": "tcp",
           "poll_interval": 10, "detail_poll_interval": 120,
           "read_endpoint_names": True, "command_timeout": 5}
    cfg.update(config or {})
    drv = Driver("nav1", cfg, state, events)

    link_box = {}

    def factory(driver):
        link = _Link(driver, sim, silent=silent, drip=drip)
        link_box["link"] = link
        # The SSH ident a raw socket meets on 22023. A real driver never sees
        # it (the SSH transport eats it), but the simulator serves it, so the
        # driver has to tolerate a line it did not ask for before its first
        # reply -- which is the same class of problem as the echo.
        asyncio.get_running_loop().create_task(
            link.deliver(b"SSH-2.0-OpenSSH_9.6\r\n"))
        return link

    drv.transport_factory = factory
    await drv.connect()
    return drv, sim, state, link_box["link"]


def _run(coro):
    return asyncio.run(coro)


def _cs(state, ctype, cid, prop):
    return state.data.get(f"device.nav1.{ctype}.{cid:04d}.{prop}")


def _s(state, key):
    return state.data.get(f"device.nav1.{key}")


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_transport_is_ssh_on_the_navigators_own_port():
    info = Driver.DRIVER_INFO
    assert info["transport"] == "ssh"
    # 22023, not 22 -- the guide is explicit, and it is the single easiest
    # thing to get wrong about this device.
    assert info["default_config"]["port"] == 22023
    assert info["ports"] == [22023]
    # tcp is offered so the same code runs against the simulator.
    assert set(info["transports"]) == {"ssh", "tcp"}


def test_routing_planes_name_real_commands_and_real_child_props():
    info = Driver.DRIVER_INFO
    routing = info["routing"]
    dec_props = info["child_entity_types"]["decoder"]["state_variables"]
    assert routing["destination_child_type"] == "decoder"
    assert routing["source_child_type"] == "encoder"
    for plane in routing["planes"]:
        assert plane["command"] in info["commands"]
        assert plane["route_property"] in dec_props
    # USB is deliberately NOT a plane: a USB tie is any-to-any on this system,
    # so it cannot be expressed as encoder -> decoder.
    assert [p["label"] for p in routing["planes"]] == ["Video", "Audio"]
    assert "tie_usb" in info["commands"]


def test_the_factory_reset_is_confirmed_and_declares_its_restart_window():
    info = Driver.DRIVER_INFO
    # ESC-Z-Q-Q-Q wipes the unit; it is not a reboot, and there is no reboot
    # in the SIS command set at all.
    assert info["commands"]["factory_reset"]["restarts_device_for"] > 0
    action = next(a for a in info["actions"] if a["id"] == "factory_reset")
    assert isinstance(action["confirm"], str) and len(action["confirm"]) > 40
    # And it is not one click away on the device page.
    assert "factory_reset" not in info["quick_actions"]


def test_the_linklicense_requirement_is_on_the_offline_banner():
    # Without the LinkLicense there is no SSH interface, so the driver cannot
    # connect -- which means the place this has to be said is the banner
    # somebody reads when it will not connect.
    connection = Driver.DRIVER_INFO["help"]["connection"]
    assert "LinkLicense" in connection
    assert "LinkLicense" in Driver.DRIVER_INFO["help"]["setup"]


def test_prefix_to_mask():
    assert DRV._prefix_to_mask(24) == "255.255.255.0"
    assert DRV._prefix_to_mask(16) == "255.255.0.0"
    assert DRV._prefix_to_mask(32) == "255.255.255.255"
    assert DRV._prefix_to_mask(0) == "0.0.0.0"


def test_inventory_parsing_keeps_position_as_the_endpoint_number():
    # Position is the endpoint number; '0' is an unassigned slot, which is the
    # absence of a child rather than a child that is down.
    assert DRV._parse_inventory("1020003") == {1: "1", 3: "2", 7: "3"}
    assert DRV._parse_inventory("") == {}
    assert DRV._parse_inventory("000") == {}


def test_tie_cell_dashes_mean_untied():
    assert DRV._tie_value("12") == 12
    assert DRV._tie_value("---") == 0
    assert DRV._tie_value("----") == 0
    assert DRV._tie_value("") == 0


def test_async_frame_families_are_matched_structurally():
    # Enumerate what you handle; DETECT what you divert. An undocumented
    # sibling notice must fall into the event path, never into a reply.
    for line in ("DevpA*3i*1", "DevpC*12o*0", "DevpP*101i*1",
                 "HkdmP*4i", "HkdmK*4i"):
        assert DRV._ASYNC_RE.match(line), line
    for line in ("Out03 In01 All", "Vrb3", "Echo0", "E13", "1.01"):
        assert not DRV._ASYNC_RE.match(line), line


# ── Round trip: the connect ceremony ────────────────────────────────────────

def test_connect_turns_echo_off_and_verbose_tagging_on():
    async def go():
        drv, sim, state, link = await _pair()
        # Asserted on the SIMULATOR: the driver actually changed the device,
        # rather than merely believing it had.
        assert sim.state.get("echo") == 0
        assert sim.state.get("verbose") == 3
        # And the echo-disable went first, because everything after it depends
        # on replies not being interleaved with command copies.
        assert link.sent[0] == f"{ESC}0ECHO{CR}"
        await drv.disconnect()
    _run(go())


def test_the_driver_survives_the_echo_of_its_own_first_command():
    async def go():
        drv, sim, state, link = await _pair()
        # Echo is on until the first command lands, so the reply to that
        # command arrives behind a copy of the command itself. Getting this
        # wrong means the connect ceremony never completes.
        assert drv._connected
        assert _s(state, "model") == "NAVigator"
        await drv.disconnect()
    _run(go())


def test_a_fragmented_byte_stream_still_frames():
    async def go():
        drv, sim, state, link = await _pair(drip=True)
        assert drv._connected
        assert _s(state, "serial_number") == "A1PC690"
        await drv.disconnect()
    _run(go())


# ── Round trip: identity and detail ─────────────────────────────────────────

def test_identity_is_read_from_the_device():
    async def go():
        drv, sim, state, link = await _pair()
        assert _s(state, "model") == "NAVigator"
        assert _s(state, "model_description") == "NAV System Manager"
        assert _s(state, "part_number") == "60-1534-01"
        assert _s(state, "serial_number") == "A1PC690"
        # One firmware string, three granularities.
        assert _s(state, "firmware_version") == "1.01"
        assert _s(state, "firmware_full") == "1.01.0000"
        assert _s(state, "firmware_advanced") == "1.01.0000-b088"
        assert _s(state, "mac_address") == "00-05-A6-13-9C-32"
        assert _s(state, "device_name") == "NAVigator-13-9C-31"
        await drv.disconnect()
    _run(go())


def test_detail_reads_licensing_temperature_and_both_interfaces():
    async def go():
        drv, sim, state, link = await _pair()
        assert _s(state, "temperature_c") == 39
        assert _s(state, "temperature_f") == 103
        assert _s(state, "connected_users") == 2
        assert _s(state, "license_endpoints") == 48
        assert "Third Party Control" in _s(state, "license_summary")
        # The NAVigator reports a prefix; the driver publishes a mask.
        assert _s(state, "oob_ip_address") == "192.168.253.254"
        assert _s(state, "oob_subnet_mask") == "255.255.255.0"
        assert _s(state, "nav_gateway") == "192.168.1.1"
        await drv.disconnect()
    _run(go())


# ── Round trip: the roster ──────────────────────────────────────────────────

def test_the_roster_is_enumerated_from_the_device_and_is_sparse():
    async def go():
        drv, sim, state, link = await _pair()
        # Non-contiguous on purpose: a driver that assumed 1..N would pass
        # against a dense system and mis-map every endpoint here.
        assert sorted(drv.list_children("encoder")) == [1, 2, 3, 17, 101]
        assert sorted(drv.list_children("decoder")) == [1, 2, 3, 4, 201, 202]
        await drv.disconnect()
    _run(go())


def test_the_two_kinds_of_down_get_two_different_fault_codes():
    async def go():
        drv, sim, state, link = await _pair()
        # Online: no fault claimed at all.
        assert _cs(state, "encoder", 1, "online") is True
        assert _cs(state, "encoder", 1, "offline_reason") is None
        # Offline (digit 2).
        assert _cs(state, "encoder", 17, "online") is False
        assert _cs(state, "encoder", 17, "offline_reason") == "not_responding"
        # Present but NOT connected (digit 3) -- a different job for whoever
        # has to fix it, so it must not flatten into the same answer.
        assert _cs(state, "encoder", 101, "offline_reason") == "service_fault"
        assert "unicast" in _cs(state, "encoder", 101, "offline_detail")
        assert _cs(state, "encoder", 101, "connected") is False
        await drv.disconnect()
    _run(go())


def test_endpoint_names_come_back_through_encapsulation():
    async def go():
        drv, sim, state, link = await _pair()
        assert _cs(state, "encoder", 1, "name") == "NAV-E-Podium-PC"
        assert _cs(state, "encoder", 101, "name") == "NAV-E-Lecture-Capture"
        assert _cs(state, "decoder", 201, "name") == "NAV-SD-Atrium-Left"
        # The encapsulated command carries an embedded CR inside its braces,
        # which is why neither side can frame on CR alone.
        assert any(s.startswith("{1I:") and ESC + "CN" + CR in s
                   for s in link.sent)
        await drv.disconnect()
    _run(go())


def test_names_are_not_re_read_on_every_poll():
    async def go():
        drv, sim, state, link = await _pair()
        before = sum(1 for s in link.sent if s.startswith("{"))
        await drv.poll()
        after = sum(1 for s in link.sent if s.startswith("{"))
        # One request per endpoint is fine once; per poll forever is not.
        assert after == before
        await drv.disconnect()
    _run(go())


def test_read_endpoint_names_can_be_turned_off():
    async def go():
        drv, sim, state, link = await _pair(
            config={"read_endpoint_names": False})
        assert not any(s.startswith("{") for s in link.sent)
        assert sorted(drv.list_children("encoder")) == [1, 2, 3, 17, 101]
        await drv.disconnect()
    _run(go())


def test_a_deassigned_endpoint_is_deregistered_on_the_next_poll():
    async def go():
        drv, sim, state, link = await _pair()
        await sim.notify_endpoint(101, "i", "A", 0)
        await drv.poll()
        assert 101 not in drv.list_children("encoder")
        await drv.disconnect()
    _run(go())


# ── Round trip: ties ────────────────────────────────────────────────────────

def test_ties_are_read_from_the_report_including_a_broken_away_audio():
    async def go():
        drv, sim, state, link = await _pair()
        assert _cs(state, "decoder", 1, "source_video") == 1
        assert _cs(state, "decoder", 2, "source_video") == 3
        # Audio broken away from video is the case that makes the "view AV
        # tie" query answer E13, and the case a single source property would
        # quietly lose.
        assert _cs(state, "decoder", 3, "source_video") == 1
        assert _cs(state, "decoder", 3, "source_audio") == 17
        # Dashes in the report mean untied.
        assert _cs(state, "decoder", 4, "source_video") == 0
        assert _cs(state, "decoder", 201, "source_video") == 101
        assert _cs(state, "decoder", 1, "usb_host") == "17i"
        await drv.disconnect()
    _run(go())


@pytest.mark.parametrize("command,expect", [
    ("tie_av", [2, 2]),
    ("tie_video", [2, 1]),
    ("tie_audio", [1, 2]),
])
def test_each_tie_touches_only_its_own_plane(command, expect):
    async def go():
        drv, sim, state, link = await _pair()
        sim._ties[4] = [1, 1]
        drv.set_child_state_batch("decoder", 4,
                                  {"source_video": 1, "source_audio": 1})
        await drv.send_command(command, {"input": 2, "output": 4})
        assert sim._ties[4] == expect
        # The acknowledged value is written through, so the crosspoint lights
        # now rather than on the next poll. That is a readback of the
        # NAVigator's own reply, not an optimistic guess.
        assert _cs(state, "decoder", 4, "source_video") == expect[0]
        assert _cs(state, "decoder", 4, "source_audio") == expect[1]
        await drv.disconnect()
    _run(go())


def test_untie_output_clears_both_planes():
    async def go():
        drv, sim, state, link = await _pair()
        await drv.send_command("untie_output", {"output": 1})
        assert sim._ties[1] == [0, 0]
        assert _cs(state, "decoder", 1, "source_video") == 0
        assert _cs(state, "decoder", 1, "source_audio") == 0
        await drv.disconnect()
    _run(go())


def test_tie_to_all_outputs_then_untie_that_input():
    async def go():
        drv, sim, state, link = await _pair()
        await drv.send_command("tie_av_all", {"input": 2})
        assert all(v == [2, 2] for v in sim._ties.values())
        # And the driver re-read the report rather than assuming.
        assert _cs(state, "decoder", 201, "source_video") == 2
        await drv.send_command("untie_input", {"input": 2})
        assert all(v == [0, 0] for v in sim._ties.values())
        await drv.disconnect()
    _run(go())


def test_a_tie_to_an_endpoint_the_navigator_does_not_have_is_refused():
    async def go():
        drv, sim, state, link = await _pair()
        with pytest.raises(ValueError) as e:
            await drv.send_command("tie_av", {"input": 99, "output": 1})
        # The device's own reason, not a generic failure.
        assert "E25" in str(e.value)
        assert "not present" in str(e.value).lower()
        await drv.disconnect()
    _run(go())


def test_a_usb_reference_without_an_i_or_o_suffix_is_refused_not_sent():
    async def go():
        drv, sim, state, link = await _pair()
        before = len(link.sent)
        with pytest.raises(ValueError) as e:
            await drv.send_command("tie_usb", {"host": "2", "device": "3o"})
        assert "suffix" in str(e.value)
        # An input and an output may share a number, so a bare number is
        # ambiguous. Refusing beats mis-tying.
        assert len(link.sent) == before
        # The unambiguous form goes through.
        await drv.send_command("tie_usb", {"host": "2i", "device": "3o"})
        assert sim._usb["3o"] == "2i"
        await drv.disconnect()
    _run(go())


# ── Round trip: WindoWall, KVM, alarms ──────────────────────────────────────

def test_windowall_and_kvm_presets():
    async def go():
        drv, sim, state, link = await _pair()
        await drv.send_command("recall_windowall_preset",
                               {"canvas": 2, "preset": 5})
        assert sim._canvas_preset[2] == 5
        await drv.send_command("select_window_input",
                               {"canvas": 1, "window": 3, "input": 17})
        assert sim._window_input[(1, 3)] == 17
        await drv.send_command("recall_workstation_preset",
                               {"workstation": 4, "preset": 9})
        assert sim._workstation_preset[4] == 9
        await drv.disconnect()
    _run(go())


def test_window_mute_is_sent_without_an_escape_prefix():
    async def go():
        drv, sim, state, link = await _pair()
        await drv.send_command("mute_window", {"canvas": 1, "window": 3})
        assert sim._window_mute[(1, 3)] == 1
        # The trailing B terminates this one; the guide writes it with no ESC,
        # and an ESC would make it a different command.
        assert f"1*3*1B{CR}" in link.sent
        await drv.send_command("unmute_window", {"canvas": 1, "window": 3})
        assert sim._window_mute[(1, 3)] == 0
        await drv.disconnect()
    _run(go())


def test_alarms_report_the_worst_severity_present():
    async def go():
        drv, sim, state, link = await _pair()
        assert _s(state, "alarm_count") == 2
        assert _s(state, "alarm_active") is True
        # warning outranks info -- and BOTH alarms have to be parsed to know
        # that, which is the bug this catches: the alarm list has no header
        # line, so a collector expecting one eats the first alarm.
        assert _s(state, "alarm_worst_severity") == "warning"
        assert "17i" in _s(state, "alarm_summary")
        assert "202o" in _s(state, "alarm_summary")
        await drv.disconnect()
    _run(go())


def test_clearing_alarms_re_reads_them():
    async def go():
        drv, sim, state, link = await _pair()
        await drv.send_command("clear_alarms", {})
        assert sim._alarms == []
        assert _s(state, "alarm_count") == 0
        assert _s(state, "alarm_active") is False
        assert _s(state, "alarm_worst_severity") == "none"
        await drv.disconnect()
    _run(go())


# ── Round trip: push on the control connection ──────────────────────────────

def test_an_unsolicited_notice_updates_state_with_no_request_in_flight():
    async def go():
        drv, sim, state, link = await _pair()
        await sim.notify_endpoint(1, "i", "P", 0)
        await asyncio.sleep(0.05)
        assert _cs(state, "encoder", 1, "online") is False
        assert _cs(state, "encoder", 1, "offline_reason") == "not_responding"
        # Recovery must clear it, or one transient failure looks permanent
        # for as long as the device stays up.
        await sim.notify_endpoint(1, "i", "P", 1)
        await asyncio.sleep(0.05)
        assert _cs(state, "encoder", 1, "online") is True
        assert _cs(state, "encoder", 1, "offline_reason") is None
        await drv.disconnect()
    _run(go())


def test_a_notice_arriving_mid_request_is_not_taken_as_the_reply():
    async def go():
        drv, sim, state, link = await _pair()

        async def interleave():
            await asyncio.sleep(0.005)
            await sim.notify_endpoint(2, "i", "C", 0)

        task = asyncio.create_task(interleave())
        await drv.send_command("tie_av", {"input": 3, "output": 2})
        await task
        await asyncio.sleep(0.05)
        # The command still got its own answer...
        assert sim._ties[2] == [3, 3]
        assert _cs(state, "decoder", 2, "source_video") == 3
        # ...and the notice was still applied rather than swallowed.
        assert _cs(state, "encoder", 2, "connected") is False
        await drv.disconnect()
    _run(go())


def test_a_hot_key_reaches_state_and_emits_an_event():
    async def go():
        drv, sim, state, link = await _pair()
        await sim.notify_hotkey(17, "i", "P")
        await asyncio.sleep(0.05)
        assert _s(state, "last_hotkey") == "17i (Ctrl+Ctrl)"
        # A hot key is a transient: a macro has to be able to trigger on it,
        # so it is an event as well as a state value.
        payload = dict(drv.events.payloads)["device.hotkey.nav1"]
        assert payload["endpoint"] == "17i"
        assert payload["combination"] == "Ctrl+Ctrl"
        await drv.disconnect()
    _run(go())


# ── Round trip: settings, pickers, encapsulation, liveness ──────────────────

def test_the_device_name_setting_round_trips():
    async def go():
        drv, sim, state, link = await _pair()
        await drv.set_device_setting("device_name", "NAVigator-Lecture-Hall")
        assert sim.state.get("unit_name") == "NAVigator-Lecture-Hall"
        assert _s(state, "device_name") == "NAVigator-Lecture-Hall"
        await drv.disconnect()
    _run(go())


def test_endpoint_options_is_a_json_picker_list_carrying_names():
    async def go():
        drv, sim, state, link = await _pair()
        options = json.loads(_s(state, "endpoint_options"))
        assert len(options) == 11
        by_value = {o["value"]: o["label"] for o in options}
        assert "1i" in by_value and "201o" in by_value
        assert "NAV-E-Podium-PC" in by_value["1i"]
        # The command that consumes it declares it.
        usb = Driver.DRIVER_INFO["commands"]["tie_usb"]["params"]
        assert usb["host"]["options_state"] == "endpoint_options"
        await drv.disconnect()
    _run(go())


def test_encapsulation_passthrough_returns_the_endpoints_own_reply():
    async def go():
        drv, sim, state, link = await _pair()
        reply = await drv.send_command(
            "send_endpoint_command", {"endpoint": "1i", "command": "<ESC>CN"})
        assert "NAV-E-Podium-PC" in reply
        reply = await drv.send_command(
            "send_endpoint_command", {"endpoint": "1i", "command": "1B"})
        assert reply == "Vmt1"
        # An endpoint's refusal surfaces as the endpoint's own code.
        with pytest.raises(ValueError) as e:
            await drv.send_command(
                "send_endpoint_command", {"endpoint": "1i", "command": "ZZZZ"})
        assert "E10" in str(e.value)
        await drv.disconnect()
    _run(go())


def test_the_liveness_probe_awaits_an_answer():
    async def go():
        drv, sim, state, link = await _pair()
        await drv._liveness_probe()          # answered
        link.silent = True                   # device goes quiet, socket open
        drv.config["command_timeout"] = 1
        with pytest.raises(TimeoutError):
            await drv._liveness_probe()
        # A fire-and-forget probe would have succeeded here, and the platform
        # would never have learned the device stopped answering.
        await drv.disconnect()
    _run(go())


def test_refresh_children_re_reads_the_roster_and_the_names():
    async def go():
        drv, sim, state, link = await _pair()
        result = await drv.refresh_children()
        assert result == {"encoders": 5, "decoders": 6}
        assert _cs(state, "encoder", 1, "name") == "NAV-E-Podium-PC"
        await drv.disconnect()
    _run(go())


# ── Command-surface consistency ─────────────────────────────────────────────

def _sample_params(name: str) -> dict:
    spec = Driver.DRIVER_INFO["commands"][name].get("params", {})
    out: dict = {}
    for pname, pdef in spec.items():
        if pdef.get("type") == "child_id":
            out[pname] = 1
        elif pdef.get("type") == "integer":
            out[pname] = int(pdef.get("min", 1))
        elif pname in ("host", "device", "endpoint"):
            out[pname] = "1i"
        elif pname == "command":
            out[pname] = "1B"
        else:
            out[pname] = "1"
    return out


def test_every_declared_command_has_a_branch_in_send_command():
    """A Python driver whose send_command falls through returns success that is
    byte-identical to a command that worked. No gate catches it, so the button
    sits there looking fine and does nothing, forever.
    """
    async def go():
        drv, sim, state, link = await _pair()
        fell_through = []
        for name in Driver.DRIVER_INFO["commands"]:
            if name == "factory_reset":
                continue                      # destructive; covered below
            result = await drv.send_command(name, _sample_params(name))
            if result is False:
                fell_through.append(name)
        assert not fell_through
        await drv.disconnect()
    _run(go())


def test_an_unknown_command_returns_false_rather_than_success():
    async def go():
        drv, sim, state, link = await _pair()
        assert await drv.send_command("no_such_command", {}) is False
        await drv.disconnect()
    _run(go())


def test_the_factory_reset_is_acknowledged_and_takes_the_system_with_it():
    async def go():
        drv, sim, state, link = await _pair()
        await drv.send_command("factory_reset", {})
        # It erases the unit; the simulator models that rather than pretending
        # it is a reboot.
        assert sim._encoders == {}
        assert sim._decoders == {}
        await drv.disconnect()
    _run(go())
