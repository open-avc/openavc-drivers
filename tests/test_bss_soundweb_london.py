"""Driver + simulator tests for bss_soundweb_london (BSS Soundweb London,
Direct Inject protocol).

The real driver wired to the real simulator over an in-memory transport that
runs the driver's own frame parser, so what the simulator renders is what the
driver parses, and both sides are asserted. The simulator models what a
BLU-100 on firmware 86.4.2 did on the bench where the Interface Kit is silent
or wrong: no ACK / NAK over Ethernet, replies from the unit's own node, no
echo of a write to the session that made it, changes passed on in the form
they were made, gains from -80 to +10 dB, meters in dB x 10000.

Covers:
  - the frame codec against the Interface Kit's own worked examples (the
    London Architect toolbar string, the 12.5 % percent word, the Appendix F
    string), escaping of a reserved checksum, the fader law at both ends of
    its two segments, the other scaling laws round-tripping;
  - address parsing (full HiQnet, object-only hex and decimal, node);
  - the object table: every built-in type's SV map matches Appendix G at the
    documented anchors, sizes and channel specs, a custom row, bad rows
    reported not fatal, duplicate names;
  - connect: one child per object, a subscribe per non-meter control, the
    simulator's immediate replies populating child state, meters left
    unsubscribed until enabled;
  - set / toggle / step / percent / bump / string / raw commands on the wire
    and read back through the subscription echo; a bump followed by a
    re-subscribe because the unit sends no update for it;
  - a change at the unit reaching child state by push;
  - a wrong node and an undeclared object both producing silence, and Test
    Connection reporting exactly which objects answered over a real socket;
  - the liveness probe resolved by the echo, timing out on a dead unit, and
    never dropping a unit that answers nothing;
  - node 0 learned from the reply; own writes read back; another
    controller's percent write and bump read back rather than stored;
  - nothing acknowledged; reconnect re-subscribing; poll resyncing.

The driver and simulator are loaded with the ``openavc.*`` imports stubbed so
the community CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    CallableFrameParser,
    StubBaseDriver,
    StubEvents,
    StubState,
    StubTCPSimulator,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "bss_soundweb_london.py"
SIM_PATH = REPO_ROOT / "audio" / "bss_soundweb_london_sim.py"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect for a TCP driver: the transport is
    the in-memory link the test supplies; state, children and the watchdog
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

    async def _start_push(self):
        return None

    async def _stop_push(self):
        return None

    def _link_alive(self):
        return bool(self.transport and self.transport.connected)

    async def connect(self):
        await self._stop_push()
        await self._close_session()
        await self._pre_connect()
        self.transport = self.transport_factory(self)
        try:
            await self._post_connect()
            self._connected = True
            self.set_state("connected", True)
            await self.events.emit(f"device.connected.{self.device_id}")
        except Exception:
            self.transport = None
            self._connected = False
            raise
        await self._start_push()
        try:
            await self._initial_sync()
        except Exception:
            self._connected = False
            self.set_state("connected", False)
            raise

    async def disconnect(self):
        self._stop_health_loop()
        await self._stop_push()
        await self._close_session()
        if self.transport:
            await self.transport.close()
        self.transport = None
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")

    def _handle_transport_disconnect(self):
        self._connected = False
        self.set_state("connected", False)


class _FakeTCPSimulator(StubTCPSimulator):
    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._running = True
        self._port = 0
        self.name = "sim"

    async def stop(self):
        self._running = False


install_stubs(
    {"openavc.simulator.tcp_simulator": {"TCPSimulator": _FakeTCPSimulator}},
    base_driver=_FakeBaseDriver,
)
DRV = load_module("bss_soundweb_london_under_test", DRIVER_PATH)
SIMM = load_module("bss_soundweb_london_sim_under_test", SIM_PATH)


# ── In-memory link ──────────────────────────────────────────────────────────

class _Link:
    """Stands in for TCPTransport: bytes the driver sends go to the
    simulator's handle_command; whatever it returns, and whatever it pushes,
    come back through the driver's own frame parser to on_data_received."""

    def __init__(self, driver, sim):
        self.driver = driver
        self.sim = sim
        self.connected = True
        self.sent: list[bytes] = []
        self.parser = driver._create_frame_parser()
        self.silent = False
        sim.push_targets.append(self)

    async def send(self, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError("link closed")
        self.sent.append(data)
        if self.silent:
            return
        reply = self.sim.handle_command(data)
        if reply:
            await self.deliver(reply)

    async def deliver(self, data: bytes) -> None:
        if not self.connected:
            return
        for frame in self.parser.feed(data):
            await self.driver.on_data_received(frame)

    async def close(self) -> None:
        self.connected = False
        if self in self.sim.push_targets:
            self.sim.push_targets.remove(self)

    def frames_sent(self) -> list[bytes]:
        out = []
        for chunk in self.sent:
            buf = chunk
            while buf:
                frame, buf = DRV.parse_di_stream(buf)
                if frame and frame[0] == DRV.STX:
                    out.append(frame)
                elif frame is None:
                    break
        return out


CONTROLS = [
    {"name": "Program", "address": "0x100", "type": "gain", "size": ""},
    {"name": "Mics", "address": "0x101", "type": "n_input_gain", "size": "2"},
    {"name": "Source", "address": "0x102", "type": "source_selector", "size": ""},
    {"name": "Matrix", "address": "0x103", "type": "matrix_router", "size": "2x2"},
    {"name": "Input Card A", "address": "0x1", "type": "input_card", "size": "1-2"},
    {"name": "Meter", "address": "0x104", "type": "meter", "size": ""},
    {"name": "Phone", "address": "0x105", "type": "custom", "size": "7 string"},
    {"name": "Delay", "address": "0x106", "type": "custom", "size": "0 delay"},
]


def _config(**extra):
    cfg = {"host": "10.0.0.5", "port": 1023, "node_address": "0x08AD",
           "controls": [dict(r) for r in CONTROLS], "enable_meters": False,
           "meter_rate_ms": 100, "poll_interval": 0, "inter_command_delay": 0}
    cfg.update(extra)
    return cfg


def _make(sim_config=None, **extra):
    cfg = _config(**extra)
    sim = SIMM.BSSSoundwebLondonSimulator("sim", sim_config if sim_config is not None else dict(cfg))
    drv = DRV.BSSSoundwebLondonDriver("blu", cfg, StubState(), StubEvents())
    drv.transport_factory = lambda d: _Link(d, sim)
    return drv, sim


def _child(drv, cid, prop):
    return drv.state.data.get(f"device.blu.object.{cid}.{prop}")


async def _settle():
    for _ in range(4):
        await asyncio.sleep(0)


# ── Codec ───────────────────────────────────────────────────────────────────

def test_toolbar_example_from_the_interface_kit():
    # Interface Kit p.20: node 0x08AD, VD 3, object 0x11, SV 0, data 0 -> 0x3F.
    assert DRV.build_set(0x08AD, 3, 0x11, 0, 0).hex(" ") == \
        "02 88 08 ad 1b 83 00 00 11 00 00 00 00 00 00 3f 03"
    # p.11: node 0x010F, VD 3, object 0x100, SV 0 -> checksum 0x84.
    assert DRV.build_set(0x010F, 3, 0x100, 0, 0).hex(" ") == \
        "02 88 01 0f 1b 83 00 01 00 00 00 00 00 00 00 84 03"


def test_percent_word_and_reserved_checksum_are_escaped():
    # Appendix A: 12.5 % = 819200 = 00 0C 80 00. The checksum of this body is
    # 0x03 (ETX) and must travel as 1B 83 (p.7).
    frame = DRV.build_set_percent(0, 3, 0x100, 0, 12.5)
    assert frame.hex(" ") == "02 8d 00 00 1b 83 00 01 00 00 00 00 0c 80 00 1b 83 03"
    assert DRV.decode_frame(frame) is not None
    assert DRV.parse_body(DRV.decode_frame(frame)).raw == 819200
    bump = DRV.parse_body(DRV.decode_frame(DRV.build_bump_percent(0, 3, 1, 0, -10)))
    assert bump.raw == -655360  # Appendix A: -10 % = FF F6 00 00


def test_string_sv_follows_appendix_f():
    frame = DRV.build_set_string(0, 3, 0x100, 0, "Soundweb London")
    body = DRV.decode_frame(frame)
    assert body[9:11] == bytes.fromhex("0010")
    assert body[11:26] == b"Soundweb London" and body[26] == 0
    assert DRV.parse_body(body).text == "Soundweb London"


def test_bad_checksum_and_stream_resync():
    good = DRV.build_set(0, 3, 0x100, 1, 1)
    bad = good[:-2] + bytes([good[-2] ^ 0x10]) + good[-1:]
    assert DRV.decode_frame(bad) is None
    parser = CallableFrameParser(DRV.parse_di_stream)
    frames = [f for f in parser.feed(b"\x06garbage" + good[:5]) if f]
    assert frames == [b"\x06"]
    frames = [f for f in parser.feed(good[5:] + b"\x15") if f]
    assert frames == [good, b"\x15"]


def test_fader_law_matches_appendix_a():
    assert DRV.gain_db_to_raw(10) == 100000
    assert DRV.gain_db_to_raw(0) == 0
    assert DRV.gain_db_to_raw(-10) == -100000
    # The words a BLU-100 sent when Architect set -20 and -60 dB, and its
    # floor: every gain on the unit stops at -80 dB (-280617).
    assert DRV.gain_db_to_raw(-20) == -160206
    assert DRV.raw_to_gain_db(-160204) == pytest.approx(-20.0, abs=0.001)
    assert DRV.gain_db_to_raw(-60) == -255630
    assert DRV.raw_to_gain_db(-280617) == pytest.approx(-80.0, abs=0.001)
    assert DRV.raw_to_gain_db(-300000) == pytest.approx(-100.0)  # the Kit's law goes lower
    assert DRV.raw_to_gain_db(DRV.gain_db_to_raw(-37.5)) == pytest.approx(-37.5, abs=0.01)
    assert DRV.gain_db_to_raw(-200) == DRV.gain_db_to_raw(-80)  # clamped like the unit


def test_other_scaling_laws_round_trip():
    assert DRV.value_to_raw(DRV.FMT_SCALAR, 1.5) == 15000 and DRV.raw_to_value(DRV.FMT_SCALAR, 15000) == 1.5
    assert DRV.value_to_raw(DRV.FMT_PERCENT, 12.5) == 1250
    assert DRV.value_to_raw(DRV.FMT_DELAY, 10) == 960 and DRV.raw_to_value(DRV.FMT_DELAY, 960) == 10.0
    assert DRV.value_to_raw(DRV.FMT_FREQ, 1000) == 3000000 and DRV.raw_to_value(DRV.FMT_FREQ, 3000000) == 1000.0
    assert DRV.raw_to_value(DRV.FMT_SPEED, DRV.value_to_raw(DRV.FMT_SPEED, 25)) == pytest.approx(25.0)
    with pytest.raises(ValueError):
        DRV.value_to_raw(DRV.FMT_FREQ, 0)
    assert DRV.coerce_user_value(DRV.FMT_BOOL, "off") is False
    assert DRV.coerce_user_value(DRV.FMT_INT, "3") == 3


# ── Addresses and the object table ──────────────────────────────────────────

def test_address_forms():
    assert DRV.parse_node_address("0x08AD") == 0x08AD
    assert DRV.parse_node_address("2221") == 2221
    assert DRV.parse_node_address("") == 0
    assert DRV.parse_object_address("0x083203000100", 7) == (0x0832, 3, 0x100)
    assert DRV.parse_object_address("0x100", 7) == (7, 3, 0x100)
    assert DRV.parse_object_address("256", 7) == (7, 3, 256)
    with pytest.raises(ValueError):
        DRV.parse_object_address("first gain", 7)
    with pytest.raises(ValueError):
        DRV.parse_node_address("0x10000")


def test_object_types_match_appendix_g():
    objs, problems = DRV.parse_controls_config([
        {"name": "G", "address": "0x100", "type": "gain"},
        {"name": "N", "address": "0x101", "type": "n_input_gain", "size": "3"},
        {"name": "M", "address": "0x102", "type": "mixer", "size": "2"},
        {"name": "A", "address": "0x103", "type": "automixer", "size": "2"},
        {"name": "MM", "address": "0x104", "type": "matrix_mixer", "size": "3x2"},
        {"name": "R", "address": "0x105", "type": "matrix_router", "size": "47x2"},
        {"name": "SM", "address": "0x106", "type": "source_matrix", "size": "11"},
        {"name": "S", "address": "0x107", "type": "source_selector"},
        {"name": "Mt", "address": "0x108", "type": "meter"},
        {"name": "In", "address": "0x1", "type": "input_card", "size": "4"},
        {"name": "Out", "address": "0x5", "type": "output_card", "size": "1-4"},
        {"name": "C", "address": "0x109", "type": "custom", "size": "213 mute"},
    ], 0)
    assert problems == []
    by = {o.name: o for o in objs}
    sv = lambda o, p: by[o].controls[p].sv  # noqa: E731
    assert (sv("G", "gain"), sv("G", "mute"), sv("G", "polarity")) == (0, 1, 2)
    assert (sv("N", "input_3_gain"), sv("N", "input_3_mute"), sv("N", "input_3_polarity")) == (2, 34, 66)
    assert (sv("N", "master_gain"), sv("N", "override_mute")) == (96, 97)
    assert (sv("M", "input_2_gain"), sv("M", "input_2_aux_4_send"), sv("M", "input_2_group_1")) == (100, 123, 140)
    assert (sv("M", "aux_b_pre_post"), sv("M", "group_d_mute"), sv("M", "output_mute_right")) == (10010, 11031, 20003)
    assert (sv("A", "input_1_off_gain"), sv("A", "input_2_on"), sv("A", "aux_a_gain")) == (6, 108, 10001)
    assert (sv("A", "output_speed"), sv("A", "output_slope")) == (20004, 20005)
    assert (sv("MM", "xp_1_1_gain"), sv("MM", "xp_1_2_gain"), sv("MM", "xp_3_1_gain")) == (16384, 16512, 16386)
    assert (sv("R", "xp_1_1"), sv("R", "xp_47_1"), sv("R", "xp_47_2")) == (0, 46, 174)
    assert sv("SM", "output_11_source") == 10 and sv("S", "source") == 0
    assert (sv("Mt", "meter"), sv("Mt", "attack"), sv("Mt", "release"), sv("Mt", "reference")) == (0, 1, 2, 3)
    assert (sv("In", "channel_4_meter"), sv("In", "channel_4_gain"), sv("In", "channel_4_phantom")) == (18, 22, 23)
    assert (sv("In", "channel_1_attack"), sv("In", "channel_1_reference")) == (2, 1)
    assert (sv("Out", "channel_3_meter"), sv("Out", "channel_3_release")) == (8, 11)
    assert by["C"].controls["value"].fmt == DRV.FMT_BOOL and sv("C", "value") == 213
    assert by["Out"].address == "0x000003000005"
    assert by["G"].controls["gain"].schema()["unit"] == "dB"
    assert by["Mt"].controls["meter"].schema()["control"] is False
    assert by["Mt"].controls["meter"].schema()["cloud_priority"] == "low"


def test_bad_rows_are_reported_not_fatal():
    objs, problems = DRV.parse_controls_config([
        {"name": "Good", "address": "0x100", "type": "gain"},
        {"name": "", "address": "0x100", "type": "gain"},
        {"name": "Typo", "address": "0x101", "type": "gian"},
        {"name": "Big", "address": "0x102", "type": "n_input_gain", "size": "33"},
        {"name": "Good", "address": "0x103", "type": "gain"},
        {"name": "NoSV", "address": "0x104", "type": "custom", "size": "gain"},
    ], 0)
    assert [o.name for o in objs] == ["Good"]
    assert len(problems) == 5
    assert "duplicates" in problems[3]
    objs, _ = DRV.parse_controls_config("Prog 0x100 gain\nMics 0x101 n_input_gain 4\n", 0)
    assert [(o.name, len(o.controls)) for o in objs] == [("Prog", 3), ("Mics", 14)]


# ── Connect and subscribe ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_registers_objects_and_subscribes():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    assert drv.state.data["device.blu.connected"] is True
    assert drv.state.data["device.blu.objects_declared"] == 8
    assert sorted(drv.list_children("object")) == sorted(
        ["Program", "Mics", "Source", "Matrix", "Input_Card_A", "Meter", "Phone", "Delay"])
    assert _child(drv, "Program", "address") == "0x08AD03000100"
    assert _child(drv, "Program", "object_type") == "Gain"
    # Every non-meter control subscribed with rate 0; meters not at all.
    subs = [DRV.parse_body(DRV.decode_frame(f)) for f in drv.transport.frames_sent()
            if f[1] == DRV.DI_SUBSCRIBESV]
    assert all(m.raw == 0 for m in subs)
    non_meter = sum(1 for o in drv._objects for c in o.controls.values() if c.fmt != DRV.FMT_METER)
    assert len(subs) == non_meter
    assert sim.subscription_count == non_meter
    assert not sim.is_subscribed("Meter", "meter")
    # The immediate replies populated the children.
    assert _child(drv, "Program", "gain") == 0.0
    assert _child(drv, "Program", "mute") is False
    assert _child(drv, "Source", "source") == 1
    assert _child(drv, "Matrix", "xp_2_1") is False
    assert _child(drv, "Input_Card_A", "channel_2_phantom") is False
    assert _child(drv, "Phone", "value") == ""
    assert _child(drv, "Delay", "value") == 0.0
    assert _child(drv, "Meter", "meter") is None
    assert _child(drv, "Meter", "responding") is True  # attack/release answered
    assert drv.state.data["device.blu.objects_responding"] == 8
    # Nothing is acknowledged over Ethernet.
    assert all(chunk[0] == DRV.STX for chunk in drv.transport.sent)
    assert drv.state.data["device.blu.reported_node_address"] == "0x08AD"


@pytest.mark.asyncio
async def test_meters_subscribe_at_the_rate_when_enabled():
    drv, sim = _make(enable_meters=True, meter_rate_ms=130)
    await drv.connect()
    await _settle()
    meter_subs = [DRV.parse_body(DRV.decode_frame(f)) for f in drv.transport.frames_sent()
                  if f[1] == DRV.DI_SUBSCRIBESV and DRV.parse_body(DRV.decode_frame(f)).raw > 0]
    assert len(meter_subs) == 3 and all(m.raw == 100 for m in meter_subs)  # 50 ms granularity
    assert _child(drv, "Meter", "meter") == pytest.approx(-80.0)  # the floor, dB x 10000
    pushed = await sim.tick_meters()
    await _settle()
    assert pushed == 3
    assert _child(drv, "Meter", "meter") is not None
    await drv.disconnect()


# ── Commands ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_control_round_trips_through_the_subscription():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    await drv.send_command("set_control", {"object": "Program", "control": "gain", "value": "-12.5"})
    await _settle()
    assert sim.value_of("Program", "gain") == -12.5
    assert _child(drv, "Program", "gain") == pytest.approx(-12.5)
    written, read = drv.transport.frames_sent()[-2:]
    assert DRV.parse_body(DRV.decode_frame(written)).raw == DRV.gain_db_to_raw(-12.5)
    # The unit never echoes a write to the session that made it; the value
    # came back because the driver asked for it.
    assert read[1] == DRV.DI_SUBSCRIBESV
    await drv.send_command("set_control", {"object": "Program", "control": "Mute", "value": "on"})
    await _settle()
    assert sim.value_of("Program", "mute") is True and _child(drv, "Program", "mute") is True
    await drv.send_command("set_control", {"object": "Source", "control": "source", "value": "3"})
    await drv.send_command("set_control", {"object": "Matrix", "control": "xp_2_1", "value": "true"})
    await drv.send_command("set_control", {"object": "Delay", "control": "value", "value": "12.5"})
    await drv.send_command("set_control", {"object": "Phone", "control": "value", "value": "01onetwo"})
    await _settle()
    assert _child(drv, "Source", "source") == 3
    assert _child(drv, "Matrix", "xp_2_1") is True
    assert _child(drv, "Delay", "value") == 12.5
    assert _child(drv, "Phone", "value") == "01onetwo"
    with pytest.raises(ValueError):
        await drv.send_command("set_control", {"object": "Meter", "control": "meter", "value": "1"})
    with pytest.raises(ValueError):
        await drv.send_command("set_control", {"object": "Program", "control": "nope", "value": "1"})
    with pytest.raises(ValueError):
        await drv.send_command("set_control", {"object": "Program", "control": "mute", "value": "loud"})


@pytest.mark.asyncio
async def test_toggle_step_percent_and_bump():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    await drv.send_command("toggle_control", {"object": "Mics", "control": "input_1_mute"})
    await _settle()
    assert _child(drv, "Mics", "input_1_mute") is True
    await drv.send_command("toggle_control", {"object": "Mics", "control": "input_1_mute"})
    await _settle()
    assert _child(drv, "Mics", "input_1_mute") is False
    with pytest.raises(ValueError):
        await drv.send_command("toggle_control", {"object": "Program", "control": "gain"})
    await drv.send_command("step_gain", {"object": "Program", "control": "gain", "amount": -3})
    await _settle()
    assert _child(drv, "Program", "gain") == pytest.approx(-3.0)
    await drv.send_command("step_gain", {"object": "Program", "control": "gain", "amount": 50})
    await _settle()
    assert _child(drv, "Program", "gain") == pytest.approx(10.0)  # clamped at the law's top
    await drv.send_command("set_percent", {"object": "Program", "control": "gain", "percent": 0})
    await _settle()
    assert _child(drv, "Program", "gain") == pytest.approx(-80.0)
    before = len(drv.transport.frames_sent())
    await drv.send_command("bump_percent", {"object": "Program", "control": "gain", "delta": 10})
    await _settle()
    frames = drv.transport.frames_sent()[before:]
    assert [f[1] for f in frames] == [DRV.DI_BUMPSVPERCENT, DRV.DI_SUBSCRIBESV]
    # Percent of travel is linear in the raw word from -80 dB to +10 dB.
    lo, hi = DRV.gain_db_to_raw(-80), DRV.gain_db_to_raw(10)
    assert _child(drv, "Program", "gain") == pytest.approx(
        DRV.raw_to_gain_db(round(lo + 0.1 * (hi - lo))), abs=0.01)


@pytest.mark.asyncio
async def test_presets_raw_and_string_escape_hatches():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    await drv.send_command("recall_venue_preset", {"preset": 6})
    await drv.send_command("recall_parameter_preset", {"preset": 12})
    assert sim.state["last_venue_preset"] == 6 and sim.state["last_parameter_preset"] == 12
    assert drv.transport.frames_sent()[-1] == DRV.build_preset_recall(DRV.DI_PARAM_PRESET_RECALL, 12)
    await drv.send_command("set_raw_sv", {"address": "0x101", "sv": 33, "value": 1})
    await _settle()
    assert _child(drv, "Mics", "input_2_mute") is True
    await drv.send_command("set_raw_sv", {"address": "0x08AD03000100", "sv": 0, "value": -300000})
    await _settle()
    assert _child(drv, "Program", "gain") == pytest.approx(-80.0)  # the unit clamps
    await drv.send_command("set_string_sv", {"address": "0x105", "sv": 7, "text": "5551234"})
    await _settle()
    assert _child(drv, "Phone", "value") == "5551234"


@pytest.mark.asyncio
async def test_change_at_the_unit_is_pushed():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    assert sim.set_value("Program", "gain", -20.0)
    sim.set_state("mic_2_mute", True)  # a simulator-UI control
    await _settle()
    assert _child(drv, "Program", "gain") == pytest.approx(-20.0)
    assert _child(drv, "Mics", "input_2_mute") is True
    await drv.send_command("resync", {})
    await _settle()
    assert _child(drv, "Program", "gain") == pytest.approx(-20.0)


# ── Silence: wrong node, wrong object, dead unit ────────────────────────────

@pytest.mark.asyncio
async def test_wrong_node_gets_no_readback():
    drv, sim = _make(sim_config={"node_address": "0x0832", "controls": CONTROLS})
    await drv.connect()
    await _settle()
    assert _child(drv, "Program", "gain") is None
    assert drv.state.data["device.blu.objects_responding"] == 0
    assert _child(drv, "Program", "responding") is False
    with pytest.raises(ValueError):
        await drv.send_command("toggle_control", {"object": "Program", "control": "mute"})


@pytest.mark.asyncio
async def test_undeclared_object_stays_silent_and_others_answer():
    rows = [dict(r) for r in CONTROLS] + [{"name": "Ghost", "address": "0x777", "type": "gain"}]
    drv, sim = _make(sim_config={"node_address": "0x08AD", "controls": CONTROLS}, controls=rows)
    await drv.connect()
    await _settle()
    assert _child(drv, "Ghost", "responding") is False and _child(drv, "Ghost", "gain") is None
    assert _child(drv, "Program", "responding") is True
    assert drv.state.data["device.blu.objects_responding"] == 8


@pytest.mark.asyncio
async def test_liveness_probe_resolves_and_times_out():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    drv.LIVENESS_FIRST_WAIT_S = drv.LIVENESS_REST_WAIT_S = 0.05
    await drv._liveness_probe()
    drv.transport.silent = True
    with pytest.raises(TimeoutError):
        await drv._liveness_probe()
    assert drv._waiters == {}


@pytest.mark.asyncio
async def test_liveness_asks_a_control_that_has_answered():
    # The first row names an object the design does not have. The unit says
    # nothing about it, so the probe asks the first control that answered.
    rows = [{"name": "Ghost", "address": "0x777", "type": "gain"}] + [dict(r) for r in CONTROLS]
    drv, sim = _make(sim_config={"node_address": "0x08AD", "controls": CONTROLS}, controls=rows)
    await drv.connect()
    await _settle()
    drv.LIVENESS_FIRST_WAIT_S = drv.LIVENESS_REST_WAIT_S = 0.05
    before = len(drv.transport.frames_sent())
    await drv._liveness_probe()
    asked = DRV.parse_body(DRV.decode_frame(drv.transport.frames_sent()[before]))
    assert asked.key in drv._answered and asked.obj != 0x777
    # The object that answered last is asked first.
    assert asked.key == max(drv._liveness_keys(), key=lambda k: drv._answered[k])
    # A unit that answered and then stops is still a miss.
    drv.transport.silent = True
    with pytest.raises(TimeoutError):
        await drv._liveness_probe()


@pytest.mark.asyncio
async def test_liveness_survives_an_object_leaving_the_design():
    # The object that answered last disappears from the design mid-session
    # (Architect sent a new one); the others still answer, so the link stays.
    drv, sim = _make()
    await drv.connect()
    await _settle()
    drv.LIVENESS_FIRST_WAIT_S = drv.LIVENESS_REST_WAIT_S = drv.PROBE_TIMEOUT_S = 0.05
    before = len(drv.transport.frames_sent())
    # Gone: the first declared object (Program) and the one that answered
    # last on connect (Delay, the last row).
    for gone in (0x100, 0x106):
        for key in [k for k in sim._fmt if k[1] == gone]:
            del sim._fmt[key]
    await drv._liveness_probe()
    asked = [DRV.parse_body(DRV.decode_frame(f)) for f in drv.transport.frames_sent()[before:]]
    assert all(m.cmd == DRV.DI_SUBSCRIBESV and m.raw == 0 for m in asked)
    assert asked[0].obj == 0x106  # the last to answer is asked first
    # Then one control of every other object at once, never a meter (a
    # subscribe at rate 0 would stop its stream).
    assert sorted(m.obj for m in asked[1:]) == [0x1, 0x100, 0x101, 0x102, 0x103, 0x104, 0x105]
    for m in asked:
        cid, prop = drv._route[m.key]
        assert drv._by_cid[cid].controls[prop].fmt != DRV.FMT_METER


@pytest.mark.asyncio
async def test_a_liveness_round_is_one_write_whatever_the_command_delay():
    # The inter-command delay paces each write. Paced frame by frame, a round
    # across many objects would outlast the watchdog and read as a dead unit.
    drv, sim = _make()
    await drv.connect()
    await _settle()
    drv.config["inter_command_delay"] = 0.3
    drv.LIVENESS_FIRST_WAIT_S = drv.LIVENESS_REST_WAIT_S = 0.05
    for key in [k for k in sim._fmt if k[1] == 0x106]:
        del sim._fmt[key]  # the last to answer is gone, so both rounds run
    writes = len(drv.transport.sent)
    started = asyncio.get_running_loop().time()
    await drv._liveness_probe()
    assert len(drv.transport.sent) - writes == 2
    assert asyncio.get_running_loop().time() - started < 1.0


@pytest.mark.asyncio
async def test_a_unit_that_answers_nothing_keeps_its_link_and_says_why():
    # A wrong node (or no design loaded): the unit is up and answers nothing.
    drv, sim = _make(sim_config={"node_address": "0x0832", "controls": CONTROLS})
    drv.HEALTH_INTERVAL_S, drv.HEALTH_TIMEOUT_S, drv.PROBE_TIMEOUT_S = 0.01, 0.5, 0.05
    await drv.connect()
    await _settle()
    before = len(drv.transport.frames_sent())
    drv._start_health_loop()
    await asyncio.sleep(0.3)
    drv._stop_health_loop()
    assert not hasattr(drv, "stashed_fault")
    assert drv.transport.connected and drv.state.data["device.blu.connected"] is True
    assert "none of the 8 declared object(s) has answered" in drv.state.data["device.blu.last_error"]
    assert "Node Address" in drv.state.data["device.blu.last_error"]
    # The probe kept sending, so a unit that went away would still fail the send.
    assert len(drv.transport.frames_sent()) > before
    # The platform clears last_error after a poll that does not write it; a
    # poll while nothing answers writes the sentence again.
    drv.state.data["device.blu.last_error"] = ""
    await drv.poll()
    assert "has answered" in drv.state.data["device.blu.last_error"]
    # The first answer clears the sentence (the design with this node loaded).
    drv.transport.sim = SIMM.BSSSoundwebLondonSimulator("sim", dict(drv.config))
    await drv.poll()
    await _settle()
    assert drv.state.data["device.blu.last_error"] == ""
    assert drv.state.data["device.blu.objects_responding"] == 8


@pytest.mark.asyncio
async def test_nothing_is_acknowledged_either_way():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    sent = len(drv.transport.sent)
    await drv.transport.deliver(b"\x06\x15")
    good = DRV.build_set(0x08AD, 3, 0x100, 1, 1)
    bad = good[:-2] + bytes([good[-2] ^ 0x01]) + good[-1:]
    await drv.transport.deliver(bad)
    assert len(drv.transport.sent) == sent  # no ACK, no NAK
    assert _child(drv, "Program", "mute") is False
    # The simulator drops a bad frame and a stray ACK without a word, as the unit did.
    assert sim.handle_command(bytes([DRV.ACK]) + bad) is None


@pytest.mark.asyncio
async def test_reconnect_resubscribes_and_poll_resyncs():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    first = len(drv.transport.frames_sent())
    await drv.disconnect()
    assert drv.state.data["device.blu.connected"] is False
    await drv.connect()
    await _settle()
    assert len(drv.transport.frames_sent()) == first
    assert sim.subscription_count > 0
    sim._values[sim._find("Program", "gain")] = -7.0  # changed while unsubscribed
    await drv.poll()
    await _settle()
    assert _child(drv, "Program", "gain") == pytest.approx(-7.0)


# ── Test Connection over a real socket ──────────────────────────────────────

class _SocketSim:
    """Serve the simulator on a loopback socket for the setup action, which
    opens its own connection rather than using the driver's transport."""

    def __init__(self, sim):
        self.sim = sim
        self.server = None
        self.clients = 0

    async def __aenter__(self):
        async def handle(reader, writer):
            # The platform names each client from its own task; so does this.
            self.clients += 1
            await self.sim.on_client_connected(f"wizard{self.clients}")
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                reply = self.sim.handle_command(data)
                if reply:
                    writer.write(reply)
                    await writer.drain()
            writer.close()
        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


@pytest.mark.asyncio
async def test_test_connection_reports_which_objects_answered():
    rows = [dict(r) for r in CONTROLS] + [{"name": "Ghost", "address": "0x777", "type": "gain"}]
    drv, sim = _make(sim_config={"node_address": "0x08AD", "controls": CONTROLS}, controls=rows)
    drv.PROBE_TIMEOUT_S = 0.3
    steps = []

    async def progress(msg, pct):
        steps.append((msg, pct))

    async with _SocketSim(sim) as port:
        drv.config["host"], drv.config["port"] = "127.0.0.1", port
        result = await drv.run_setup_action("test_connection", {}, progress)
    assert result["ok"] is False
    assert result["unit_node"] == "0x08AD"
    assert result["answered"] == [r["name"] for r in CONTROLS]
    assert result["silent"] == ["Ghost (0x08AD03000777 sv 0)"]
    assert "8 of 9" in result["message"]
    assert steps[-1][1] == 100
    # The test subscriptions were released, and a disconnected driver does not
    # try to resync (nothing is connected here).
    assert sim.subscription_count == 0

    drv2, sim2 = _make(sim_config={"node_address": "0x0832", "controls": CONTROLS})
    drv2.PROBE_TIMEOUT_S = 0.3
    async with _SocketSim(sim2) as port:
        drv2.config["host"], drv2.config["port"] = "127.0.0.1", port
        result = await drv2.run_setup_action("test_connection", {}, progress)
    # A wrong node: nothing answers at it, the unit answers as node 0, and
    # its reply names the node to use.
    assert result["ok"] is False and result["answered"] == []
    assert result["unit_node"] == "0x0832"
    assert "Set the Node Address to 0x0832, or leave it blank" in result["message"]
    assert sim2.subscription_count == 0

    # Objects the design does not have: silence at every node.
    drv4, sim4 = _make(sim_config={"node_address": "", "controls": CONTROLS[:1]},
                       controls=[{"name": "Ghost", "address": "0.7.119", "type": "gain"}])
    drv4.PROBE_TIMEOUT_S = 0.2
    async with _SocketSim(sim4) as port:
        drv4.config["host"], drv4.config["port"] = "127.0.0.1", port
        result = await drv4.run_setup_action("test_connection", {}, progress)
    assert result["answered"] == [] and result["unit_node"] == ""
    assert "the design is loaded on the unit" in result["message"]

    drv3, _ = _make()
    drv3.config["host"], drv3.config["port"] = "127.0.0.1", 1
    with pytest.raises(ConnectionError):
        await drv3.run_setup_action("test_connection", {}, progress)


@pytest.mark.asyncio
async def test_test_connection_leaves_the_live_session_alone():
    # Subscriptions belong to the session that made them (a BLU-100 kept the
    # live one when another connection released the same SV), so the
    # wizard's releases cannot silence the running driver.
    drv, sim = _make()
    await drv.connect()
    await _settle()
    live = sim.subscription_count
    drv.PROBE_TIMEOUT_S = 0.3
    before = len(drv.transport.frames_sent())
    async with _SocketSim(sim) as port:
        drv.config["host"], drv.config["port"] = "127.0.0.1", port
        result = await drv.run_setup_action("test_connection", {}, lambda m, p: asyncio.sleep(0))
    assert result["ok"] is True and result["unit_node"] == "0x08AD"
    await asyncio.sleep(0.05)
    assert sim.subscription_count == live
    assert drv.transport.frames_sent()[before:] == []
    sim.set_value("Program", "mute", True)
    await _settle()
    assert _child(drv, "Program", "mute") is True


# ── What the BLU-100 showed on the bench ────────────────────────────────────

@pytest.mark.asyncio
async def test_node_zero_is_learned_from_the_reply():
    # A blank Node Address sends to node 0; the unit answers from its own
    # node (a BLU-100's is the end of its MAC), and the rows still fill.
    drv, sim = _make(sim_config={"node_address": "0x6362", "controls": CONTROLS}, node_address="")
    await drv.connect()
    await _settle()
    sent = DRV.parse_body(DRV.decode_frame(drv.transport.frames_sent()[0]))
    assert sent.node == 0
    assert _child(drv, "Program", "gain") == 0.0
    assert drv.state.data["device.blu.objects_responding"] == 8
    assert drv.state.data["device.blu.reported_node_address"] == "0x6362"
    assert _child(drv, "Program", "address") == "0x636203000100"
    await drv.send_command("set_control", {"object": "Program", "control": "gain", "value": "-20"})
    await _settle()
    assert _child(drv, "Program", "gain") == pytest.approx(-20.0)
    drv.PROBE_TIMEOUT_S = 0.05
    await drv._liveness_probe()  # the echo from node 0x6362 answers a node-0 probe


@pytest.mark.asyncio
async def test_another_controllers_percent_write_and_bump_are_read_back():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    # The unit passes a percent write on as a percent, and a bump as only the
    # amount it moved; neither is the value, so the driver asks for it.
    before = len(drv.transport.frames_sent())
    assert sim.write_from_other_session("Program", "gain", percent=50)
    await _settle()
    asked = drv.transport.frames_sent()[before:]
    assert [f[1] for f in asked] == [DRV.DI_SUBSCRIBESV]
    assert _child(drv, "Program", "gain") == pytest.approx(sim.value_of("Program", "gain"))
    assert _child(drv, "Program", "gain") == pytest.approx(-9.03, abs=0.02)  # 50 % on the unit
    assert sim.write_from_other_session("Program", "gain", bump=10)
    await _settle()
    assert _child(drv, "Program", "gain") == pytest.approx(sim.value_of("Program", "gain"))
    assert _child(drv, "Program", "gain") == pytest.approx(-5.23, abs=0.02)  # 60 % on the unit


@pytest.mark.asyncio
async def test_meter_and_input_gain_laws():
    assert DRV.raw_to_value(DRV.FMT_METER, -800000) == -80.0
    assert DRV.raw_to_value(DRV.FMT_METER, -629948) == -62.99
    assert DRV.raw_to_value(DRV.FMT_INPUT_GAIN, 2) == 12
    assert DRV.value_to_raw(DRV.FMT_INPUT_GAIN, 48) == 8
    with pytest.raises(ValueError):
        DRV.value_to_raw(DRV.FMT_INPUT_GAIN, 10)
    drv, sim = _make()
    await drv.connect()
    await _settle()
    schema = drv._by_cid["Input_Card_A"].controls["channel_1_gain"].schema()
    assert (schema["unit"], schema["min"], schema["max"], schema["step"]) == ("dB", 0, 48, 6)
    await drv.send_command("set_control", {"object": "Input_Card_A", "control": "channel_1_gain", "value": "12"})
    await _settle()
    written = [f for f in drv.transport.frames_sent() if f[1] == DRV.DI_SETSV][-1]
    assert DRV.parse_body(DRV.decode_frame(written)).raw == 2
    assert _child(drv, "Input_Card_A", "channel_1_gain") == 12
    await drv.send_command("step_gain", {"object": "Input_Card_A", "control": "channel_1_gain", "amount": 6})
    await _settle()
    assert _child(drv, "Input_Card_A", "channel_1_gain") == 18
    with pytest.raises(ValueError, match="6 dB steps"):
        await drv.send_command("set_control", {"object": "Input_Card_A", "control": "channel_1_gain", "value": "10"})
    with pytest.raises(ValueError, match="Use an amount of 6 or -6"):
        await drv.send_command("step_gain", {"object": "Input_Card_A", "control": "channel_1_gain", "amount": 1})
    with pytest.raises(ValueError, match="is not a level, so it has no dB to step. Use Set Control"):
        await drv.send_command("step_gain", {"object": "Input_Card_A", "control": "channel_1_phantom", "amount": 1})


def test_mixer_shape_matches_what_the_design_has():
    objs, problems = DRV.parse_controls_config([
        {"name": "M", "address": "0.1.3", "type": "mixer", "size": "4 mono aux 0 groups 0"},
        {"name": "S", "address": "0.1.5", "type": "automixer", "size": "2 aux 2"},
        {"name": "Full", "address": "0.1.6", "type": "mixer", "size": "2"},
        {"name": "Bad", "address": "0.1.7", "type": "mixer", "size": "4 quad"},
    ], 0)
    assert len(problems) == 1 and "mono" in problems[0]
    by = {o.name: o for o in objs}
    # The BLU-100's mono mixer answered these and nothing else.
    assert sorted(c.sv for c in by["M"].controls.values()) == [
        0, 1, 3, 4, 100, 101, 103, 104, 200, 201, 203, 204, 300, 301, 303, 304, 20000, 20001]
    assert "output_gain" in by["M"].controls and "input_1_pan" not in by["M"].controls
    s = by["S"].controls
    assert "input_2_aux_2_send" in s and "input_2_aux_3_send" not in s
    assert "aux_b_gain" in s and "aux_c_gain" not in s and "group_d_gain" in s
    assert "output_gain_right" in by["Full"].controls


def test_audio_architect_object_ids():
    assert DRV.parse_object_address("0.1.0", 0) == (0, 3, 0x100)
    assert DRV.parse_object_address("0.1.4", 7) == (7, 3, 0x104)
    assert DRV.parse_object_address("0.0.1", 0) == (0, 3, 1)
    with pytest.raises(ValueError):
        DRV.parse_object_address("0.256.0", 0)
    assert DRV.DEFAULT_CONTROLS[0]["address"] == "0.1.0"


def test_an_empty_object_list_says_where_the_rows_go():
    drv = DRV.BSSSoundwebLondonDriver("blu", _config(controls=[]), StubState(), StubEvents())
    assert drv._objects == []
    assert any("Objects table" in p for p in drv._problems)
    drv2 = DRV.BSSSoundwebLondonDriver("blu", _config(), StubState(), StubEvents())
    assert drv2._problems == []


def test_catalog_surface():
    info = DRV.BSSSoundwebLondonDriver.DRIVER_INFO
    assert info["id"] == "bss_soundweb_london"
    assert set(info["quick_actions"]) <= set(info["commands"])
    for a in info["actions"]:
        if a["kind"] == "command":
            assert a["id"] in info["commands"]
    assert info["child_entity_types"]["object"]["dynamic"] is True
    assert "help" not in info["commands"]["set_control"]["params"]["control"]
    assert "Nothing on the unit changes" in next(a for a in info["actions"] if a["id"] == "test_connection")["confirm"]
    # The Telnet greeting names the unit; port 1023 answers nothing it can match.
    probe = info["discovery"]["tcp_probe"]
    assert probe == {"port": 23, "expect": "BSS Soundweb (London)", "timeout_ms": 1500}
    assert info["discovery"]["port_open"] == [1023]
    blu100 = next(m for m in info["compatible_models"] if "BLU-100" in m["models"])
    assert blu100["models"] == ["BLU-100"]
