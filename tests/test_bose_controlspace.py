"""Driver + simulator tests for bose_controlspace (Bose Professional
ControlSpace ESP / EX / CSP, Serial Control Protocol).

No ControlSpace hardware on hand, so correctness is a dual-proof round trip:
the real driver wired to the real simulator over an in-memory transport that
runs the driver's own frame parser, so what the simulator renders is what the
driver parses, and both sides are asserted.

Covers:
  - the frame parser: CR and LF lines, a bare ACK, ``NAK nn`` with and without
    a terminator and split across reads, an ACK glued to a line;
  - the codec against the document's own examples: hex levels (SG 2,78 = 0 dB,
    GG 2,80 = +1 dB, SV 1,3,50 = -20 dB, FF = off), the Get Signal Level
    example [78,1,40,64], the Standard Mixer routing mask 84924F3A, the GA
    reply forms with one, two and cross-point indices and a quoted value;
  - the module table: every type's index map at the document's worked
    examples, signal-level and custom sizes, bad rows reported not fatal;
  - connect: SUB yes, one child per module and group, a subscription per
    control, the immediate replies populating child state, the network
    reads, parameter set names and picker options;
  - a processor that ignores SUB being polled instead;
  - set / toggle / pulse / step on the wire with the read-back the driver
    sends after every write (the simulator does not echo its own writer's
    change), NAK codes surfacing as errors, cross-points read back through
    the routing mask;
  - a change at the processor reaching child state by push;
  - groups (hex levels, mute, toggle, step, selector), parameter sets, room
    combine, signal levels with a learned channel count and a -35 dBu floor,
    telephone calls;
  - the liveness probe resolved by ``S n`` and timing out on a dead unit;
  - an unsolicited NAK in last_error; reconnect re-subscribing; poll
    resyncing; Test Connection over a real socket; the catalog surface and a
    send_command branch for every declared command.

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
    CallableFrameParser,
    StubBaseDriver,
    StubEvents,
    StubState,
    StubTCPSimulator,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "bose_controlspace.py"
SIM_PATH = REPO_ROOT / "audio" / "bose_controlspace_sim.py"


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
DRV = load_module("bose_controlspace_under_test", DRIVER_PATH)
SIMM = load_module("bose_controlspace_sim_under_test", SIM_PATH)


# ── In-memory link ──────────────────────────────────────────────────────────

class _Link:
    """Stands in for TCPTransport: each CR-terminated line the driver sends
    goes to the simulator's handle_command (the real TCP simulator splits
    lines the same way); whatever it returns, and whatever it pushes, come
    back through the driver's own frame parser to on_data_received."""

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
        for line in data.split(b"\r"):
            if not line:
                continue
            reply = self.sim.handle_command(line)
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

    def lines(self) -> list[str]:
        return [chunk.decode().rstrip("\r") for chunk in self.sent]


MODULES = [
    {"name": "Main Volume", "type": "gain"},
    {"name": "Input 1", "type": "input"},
    {"name": "Output 1", "type": "output"},
    {"name": "Selector 1", "type": "source_selector", "size": "4"},
    {"name": "Mix", "type": "standard_mixer", "size": "2x2"},
    {"name": "Matrix 1", "type": "matrix_mixer", "size": "2x2"},
    {"name": "Room EQ", "type": "parametric_eq", "size": "2"},
    {"name": "PSTN In 1", "type": "pstn_input"},
    {"name": "Logic Input 1", "type": "logic_input", "size": "2"},
    {"name": "Scenes", "type": "parameter_set_list"},
    {"name": "Inputs", "type": "signal_level", "size": "1 4"},
    {"name": "AEC ERLE", "type": "signal_level", "size": "8,3"},
    {"name": "Analog Out", "type": "signal_level", "size": "2 4 -35"},
    {"name": "Hook", "type": "custom", "size": "0>9 onoff"},
]
GROUPS = [
    {"number": 1, "name": "Lobby", "kind": "level"},
    {"number": 2, "name": "Bar Source", "kind": "selector"},
]
PARAMETER_SETS = [{"number": 11, "name": "Meeting"}, {"number": 2, "name": "Party"}]


def _config(**extra):
    cfg = {"host": "10.0.0.5", "port": 10055,
           "modules": [dict(r) for r in MODULES], "groups": [dict(r) for r in GROUPS],
           "parameter_sets": [dict(r) for r in PARAMETER_SETS], "room_combine_groups": 1,
           "enable_meters": False, "meter_interval_s": 1.0, "poll_interval": 0,
           "inter_command_delay": 0}
    cfg.update(extra)
    return cfg


def _make(sim_config=None, **extra):
    cfg = _config(**extra)
    sim = SIMM.BoseControlSpaceSimulator("sim", sim_config if sim_config is not None else dict(cfg))
    drv = DRV.BoseControlSpaceDriver("esp", cfg, StubState(), StubEvents())
    drv.ACK_TIMEOUT_S = drv.QUERY_TIMEOUT_S = drv.SUB_TIMEOUT_S = 0.05
    drv.PROBE_TIMEOUT_S = 0.05
    drv.transport_factory = lambda d: _Link(d, sim)
    return drv, sim


def _child(drv, cid, prop, child_type="module"):
    return drv.state.data.get(f"device.esp.{child_type}.{cid}.{prop}")


def _dev(drv, prop):
    return drv.state.data.get(f"device.esp.{prop}")


async def _settle():
    for _ in range(4):
        await asyncio.sleep(0)


# ── Frame parser and codec ──────────────────────────────────────────────────

def test_frame_parser_lines_ack_and_nak():
    parser = CallableFrameParser(DRV.parse_controlspace_stream)
    frames = [f for f in parser.feed(b'GA "Main L">1=-6\r\n\x06\x1501\rS 5\r') if f]
    assert frames == [b'GA "Main L">1=-6', b"\x06", b"\x1501", b"S 5"]
    # A NAK code split across two reads is held until it is complete.
    assert [f for f in parser.feed(b"\x150") if f] == []
    assert [f for f in parser.feed(b"3") if f] == [b"\x1503"]
    # An ACK glued to the end of an unterminated line ends that line.
    assert [f for f in parser.feed(b"GG 2,80\x06") if f] == [b"GG 2,80", b"\x06"]
    # A NAK with a space and no terminator, followed by another frame.
    assert [f for f in parser.feed(b"\x15 01GS\r") if f] == [b"\x15 01", b"GS"]


def test_levels_and_meters_follow_the_document():
    assert DRV.hex_level_to_db(0x78) == 0.0          # SG 2,78: 0 dB
    # The document's "GG 2,80 = 1 dB" example contradicts its own formula
    # (0h = -60 dB in 0.5 dB steps), which its other two examples follow;
    # the formula wins: 80h is +4 dB.
    assert DRV.hex_level_to_db(0x80) == 4.0
    assert DRV.hex_level_to_db(0x50) == -20.0        # SV 1,3,50: -20 dB
    assert DRV.hex_level_to_db(0x90) == 12.0
    assert DRV.hex_level_to_db(0xFF) == -60.5
    assert DRV.db_to_hex_level(0) == "78" and DRV.db_to_hex_level(1) == "7a"
    assert DRV.db_to_hex_level(-20) == "50" and DRV.db_to_hex_level(-60.5) == "ff"
    assert DRV.db_to_hex_level(40) == "90"           # clamped at +12 dB
    # GL 1 [78,1,40,64]: 0.0, -59.5, -28.0, -10.0 dBFS
    assert [DRV.meter_to_db(v) for v in (0x78, 0x1, 0x40, 0x64)] == [0.0, -59.5, -28.0, -10.0]
    assert DRV.meter_to_db(0x28) == -40.0            # AEC ERLE example
    assert DRV.meter_to_db(0x78, -35.0) == 25.0      # a fixed-I/O analog output's top
    assert DRV.format_number(-3.5) == "-3.5" and DRV.format_number(-21.0) == "-21"
    assert DRV.format_number(4.08) == "4.08"


def test_routing_mask_follows_the_worked_example():
    on = {1, 6, 9, 12, 15, 18, 21, 22, 23, 24, 27, 28, 29, 31}
    outputs = DRV.routing_mask_to_outputs("84924F3A", 32)
    assert {o for o, v in outputs.items() if v} == on
    assert DRV.outputs_to_routing_mask({o: o in on for o in range(1, 33)}) == "84924F3A"
    assert DRV.routing_mask_to_outputs("C0000000", 4) == {1: True, 2: True, 3: False, 4: False}
    assert DRV.routing_mask_to_outputs("03000000", 8)[7] and DRV.routing_mask_to_outputs("03000000", 8)[8]


def test_ga_reply_forms():
    r = DRV.parse_ga_reply('GA"Main L">1=-6')
    assert (r.name, r.idx, r.value) == ("Main L", ("1",), "-6")
    r = DRV.parse_ga_reply('GA "Gain 1">2=O')
    assert (r.name, r.idx, r.value) == ("Gain 1", ("2",), "O")
    r = DRV.parse_ga_reply('GA"Theatre">4>(6,8)=F')
    assert (r.name, r.idx, r.value) == ("Theatre", ("4", "(6,8)"), "F")
    r = DRV.parse_ga_reply('GA"VoIP In 1">0>1="ACTIVE"')
    assert (r.name, r.idx, r.value) == ("VoIP In 1", ("0", "1"), '"ACTIVE"')
    r = DRV.parse_ga_reply('GA"Logic Block 1">4>3>1=F')
    assert r.idx == ("4", "3", "1")
    assert DRV.parse_ga_reply("GG 2,80") is None
    assert DRV.parse_grc_joined('GRC "Ground Floor",[2,4,5][1,3]') == ("Ground Floor", [{2, 4, 5}, {1, 3}])
    assert DRV.parse_grc_joined("GRC 1,2,4,S") is None
    assert DRV.sa_line("Input 1", "3", "-21") == 'SA "Input 1">3=-21'
    assert DRV.sa_line("Gain", "1", "-3", "ESP 2") == 'SA @ "ESP 2" "Gain">1=-3'
    assert DRV.ma_line("PSTN In 1", "2", "08707414500") == 'MA "PSTN In 1">2="08707414500"'
    assert DRV.ma_line("PSTN In 1", "3", None) == 'MA "PSTN In 1">3'
    assert DRV.sub_line('GA "Gain 1">2') == 'SUB "GA "Gain 1">2"'


# ── The module table ────────────────────────────────────────────────────────

def test_module_types_match_the_document():
    mods, problems = DRV.parse_modules_config([
        {"name": "In", "type": "input"}, {"name": "Out", "type": "output"},
        {"name": "G", "type": "gain"}, {"name": "AEC", "type": "aec"},
        {"name": "AGC 1", "type": "agc", "size": "2"}, {"name": "AMM 1", "type": "amm_gain_sharing", "size": "6"},
        {"name": "AMM L", "type": "amm_gated_legacy", "size": "4"}, {"name": "AMM E", "type": "amm_gated", "size": "4"},
        {"name": "Matrix", "type": "matrix_mixer", "size": "4x4"}, {"name": "Mix", "type": "standard_mixer", "size": "8x8"},
        {"name": "PEQ", "type": "parametric_eq", "size": "5"}, {"name": "GEQ", "type": "graphic_eq"},
        {"name": "Delay 1", "type": "delay", "size": "4"}, {"name": "Logic", "type": "logic_input", "size": "10"},
        {"name": "CRR 1", "type": "conference_room_router", "size": "4"}, {"name": "X-Over 2", "type": "crossover", "size": "4"},
        {"name": "PFS 1", "type": "pfs", "size": "2"}, {"name": "SPEQ 1", "type": "speaker_peq"},
        {"name": "Select", "type": "router", "size": "4"}, {"name": "Bar", "type": "source_selector", "size": "16"},
        {"name": "Sine 1", "type": "signal_generator", "size": "sine"}, {"name": "Noise 1", "type": "signal_generator", "size": "noise"},
        {"name": "Sweep 1", "type": "signal_generator", "size": "sweep"}, {"name": "GP Out", "type": "gpo", "size": "5"},
        {"name": "USB In 1", "type": "usb_input"}, {"name": "PSTN In 1", "type": "pstn_input"},
        {"name": "VoIP In 1", "type": "voip_input"}, {"name": "StRC 1", "type": "standard_room_combiner"},
        {"name": "List", "type": "parameter_set_list"}, {"name": "Hall AV", "type": "listening_area_av"},
        {"name": "Ducker 1", "type": "ducker"}, {"name": "Gate 1", "type": "gate"},
        {"name": "CompLim 1", "type": "compressor"}, {"name": "Limiter 1", "type": "peak_rms_limiter"},
        {"name": "Array EQ 1", "type": "array_eq"}, {"name": "ToneEQ L", "type": "tone_eq"},
        {"name": "Surround 1", "type": "surround_input"}, {"name": "Block", "type": "logic_block", "size": "3x2"},
        {"name": "C", "type": "custom", "size": "3>(1,1) onoff"}, {"name": "L", "type": "custom", "size": "2 level -60.5 0"},
    ])
    assert problems == []
    by = {m.name: m for m in mods}
    idx = lambda n, p: by[n].controls[p].idx  # noqa: E731
    assert idx("In", "level") == ("3",) and idx("In", "phantom") == ("5",)            # SA"Input 1">3=-21, GA"Input 2">5
    assert idx("Out", "mute") == ("2",) and idx("G", "level") == ("1",)              # SA"Output L">2=F, GA"Gain 4">1
    assert idx("AEC", "ch_6_internal_mute") == ("6", "5") and idx("AEC", "ch_8_nr_level") == ("8", "9")
    assert idx("AGC 1", "max_total_gain") == ("0", "1") and idx("AGC 1", "in_2_target_min") == ("2", "2")
    assert idx("AMM 1", "slope") == ("0", "3") and idx("AMM 1", "in_6_gain") == ("6", "1") and idx("AMM 1", "in_4_priority") == ("4", "3")
    assert idx("AMM L", "in_4_detection") == ("4", "3") and idx("AMM L", "in_3_priority") == ("3", "1")
    assert idx("AMM E", "in_3_priority") == ("3", "1") and idx("AMM E", "nom_limit") == ("0", "4")
    assert idx("Matrix", "xp_1_2") == ("1", "2") and idx("Matrix", "xp_2_4_level") == ("2", "8")   # SA"Mix">2>8 (in2,out4 for 4x4)
    assert idx("Matrix", "xp_4_4") == ("1", "16") and idx("Matrix", "input_3_mute") == ("3", "3")
    assert idx("Mix", "input_1_level") == ("1", "1") and idx("Mix", "output_8_mute") == ("2", "16")  # SA"My Mixer">2>16
    assert idx("Mix", "input_2_routing") == ("3", "2") and idx("Mix", "xp_4_5") == ("4", "(4,5)")   # SA"Theatre">4>(4,5)
    assert by["Mix"].controls["input_2_routing"].fanout == 8
    assert not by["Mix"].controls["xp_4_5"].subscribe and by["Mix"].controls["input_2_routing"].subscribe
    assert idx("PEQ", "band_2_type") == ("2", "5") and idx("PEQ", "band_5_gain") == ("5", "3")      # SA"Room EQ">2>5=LC
    assert idx("GEQ", "band_18") == ("18",) and by["GEQ"].controls["band_18"].label == "1 kHz"     # SA"GEQ 1">18=-3.5
    assert idx("GEQ", "bypass_all") == ("32",)
    assert idx("Delay 1", "tap_4_delay") == ("4", "1") and by["Delay 1"].controls["tap_4_delay"].max == 144000
    assert idx("Logic", "pin_10") == ("10", "1") and by["Logic"].controls["pin_10"].fmt == DRV.FMT_LOGIC
    assert idx("CRR 1", "master_mute") == ("1", "2") and idx("CRR 1", "program_level") == ("2", "1")
    # Far End k is at 2k+1 / 2k+2 per the table (Far End 1 Level = 3); the
    # document's "SA"Room 2">2>12=O mutes Far End 4" example contradicts its
    # own table, which puts 12 on Far End 5. The table wins.
    assert idx("CRR 1", "far_end_4_mute") == ("2", "10") and "far_end_5_level" not in by["CRR 1"].controls
    assert idx("X-Over 2", "high_mute") == ("4", "5") and idx("X-Over 2", "lo_mid_hpf_type") == ("2", "1")   # GA"X-Over 2">4>5
    assert idx("PFS 1", "release_time") == ("1", "1") and idx("PFS 1", "system_gain") == ("19", "0")       # SA"PFS 1">1>1, GA"PFS 1">19>0
    assert idx("PFS 1", "filter_1_gain") == ("2", "1") and idx("PFS 1", "clear_dynamic_filters") == ("20", "4")
    assert idx("SPEQ 1", "high_type") == ("0", "5") and idx("SPEQ 1", "band_2_frequency") == ("2", "1")     # SA"SPEQ 1">0>5=Bes36
    assert idx("Select", "output_4_source") == ("4",) and idx("Bar", "source") == ("1",)
    assert by["Bar"].controls["source"].max == 16
    assert idx("Sine 1", "frequency") == ("1", "1") and idx("Noise 1", "noise_type") == ("1",)
    assert idx("Noise 1", "pink_mute") == ("3", "2") and idx("Sweep 1", "speed") == ("4", "2")             # GA"Sweep 1">4>2
    assert idx("GP Out", "pin_2") == ("2",) and "pin_6" not in by["GP Out"].controls
    assert idx("USB In 1", "ch_2_mute") == ("2", "2")                                                      # GA"USB In 1">2>2
    assert idx("PSTN In 1", "ring_level") == ("0", "3") and idx("PSTN In 1", "mute") == ("1", "2")        # SA"PSTN In 1">0>3, GA"PSTN In 1">1>2
    assert idx("VoIP In 1", "call_status") == ("0", "1") and idx("VoIP In 1", "auto_answer") == ("0", "7")
    assert idx("StRC 1", "bgm_source") == ("1", "1") and idx("StRC 1", "main_input_mute") == ("1", "5")
    assert by["List"].controls["selection"].idx == ("2",) and by["List"].controls["selection"].write_idx == ("1",)
    assert idx("Hall AV", "auto_volume") == ("1",)
    assert idx("Ducker 1", "range") == ("3",) and idx("Gate 1", "decay") == ("6",)
    assert idx("CompLim 1", "detect_input") == ("1",) and idx("Limiter 1", "rms_threshold") == ("7",)
    assert idx("Array EQ 1", "modules") == ("1", "7") and idx("ToneEQ L", "mid_gain") == ("3",)
    assert idx("Surround 1", "center_level") == ("8",) and not by["Surround 1"].controls["output_format"].writable
    assert idx("Block", "input_3") == ("1", "3") and idx("Block", "output_2") == ("2", "2")
    assert by["C"].controls["value"].idx == ("3", "(1,1)") and by["C"].controls["value"].fmt == DRV.FMT_ONOFF
    assert by["L"].controls["value"].fmt == DRV.FMT_LEVEL and by["L"].controls["value"].max == 0.0
    assert by["In"].controls["level"].schema() == {"label": "Level", "type": "number", "unit": "dB",
                                                   "min": -60.5, "max": 12.0, "step": 0.5, "control": True}
    assert by["PEQ"].controls["band_1_type"].schema()["values"][3] == {"value": "HC", "label": "High Cut (Low Pass)"}


def test_signal_level_and_bad_rows():
    spec = DRV.parse_signal_level_size("1")
    assert (spec.slot, spec.param, spec.channels, spec.floor_db, spec.query) == ("1", None, 0, -60.0, "GL 1")
    spec = DRV.parse_signal_level_size("8,3 12")
    assert (spec.slot, spec.param, spec.channels, spec.query) == ("8", "3", 12, "GL 8,3")
    spec = DRV.parse_signal_level_size("A 4 -36")
    assert (spec.slot, spec.channels, spec.floor_db) == ("a", 4, -36.0)
    mods, problems = DRV.parse_modules_config([
        {"name": "Good", "type": "gain"},
        {"name": "", "type": "gain"},
        {"name": "Typo", "type": "gian"},
        {"name": "Big", "type": "amm_gated_legacy", "size": "9"},
        {"name": "Good", "type": "output"},
        {"name": 'Say "hi"', "type": "gain"},
        {"name": "NoFmt", "type": "custom", "size": "1"},
        {"name": "Meter", "type": "signal_level", "size": "zz"},
        {"name": "Ways", "type": "crossover", "size": "1"},
    ])
    assert [m.name for m in mods] == ["Good"]
    assert len(problems) == 8 and "duplicates" in problems[3]
    mods, _ = DRV.parse_modules_config("Prog | gain\nMics | amm_gain_sharing | 4\n")
    assert [(m.name, len(m.controls)) for m in mods] == [("Prog", 2), ("Mics", 29)]
    groups, problems = DRV.parse_groups_config([{"number": 1, "name": "A", "kind": "level"},
                                                {"number": 65, "name": "B", "kind": "level"},
                                                {"number": 1, "name": "C", "kind": "selector"},
                                                {"number": 2, "name": "D", "kind": "sideways"}])
    assert [g.number for g in groups] == [1] and len(problems) == 3
    names, problems = DRV.parse_parameter_sets_config([{"number": 5, "name": "X"}, {"number": 0, "name": "Y"}])
    assert names == {5: "X"} and len(problems) == 1


# ── Connect and subscribe ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_subscribes_and_populates():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    assert _dev(drv, "connected") is True
    assert _dev(drv, "push_supported") is True
    assert _dev(drv, "modules_declared") == 14
    assert sorted(drv.list_children("module")) == sorted(
        ["Main_Volume", "Input_1", "Output_1", "Selector_1", "Mix", "Matrix_1", "Room_EQ", "PSTN_In_1",
         "Logic_Input_1", "Scenes", "Inputs", "AEC_ERLE", "Analog_Out", "Hook"])
    assert drv.list_children("group") == [1, 2] and drv.list_children("room_combine") == [1]
    lines = drv.transport.lines()
    assert lines[0] == "SUB" and lines[1:5] == ["IP", "NP T", "NP M", "NP G"]
    subs = [ln for ln in lines if ln.startswith("SUB ") and ln != "SUB"]
    expected = sum(1 for m in drv._modules if m.signal_level is None
                   for c in m.controls.values() if c.subscribe and c.idx)
    assert len(subs) == expected + 3 + 1          # GG 1, GN 1, GG 2, GS
    assert sim.subscription_count == len(subs)
    assert 'SUB "GA "Main Volume">1"' in subs and 'SUB "GG 1"' in subs and 'SUB "GS"' in subs
    assert not any('">4>(' in ln for ln in subs)   # cross-points ride the routing mask
    # The immediate replies populated the children.
    assert _child(drv, "Main_Volume", "level") == 0.0 and _child(drv, "Main_Volume", "mute") is False
    assert _child(drv, "Input_1", "gain") == "0" and _child(drv, "Input_1", "phantom") is False
    assert _child(drv, "Selector_1", "source") == 1
    assert _child(drv, "Mix", "input_1_routing") == "80000000" and _child(drv, "Mix", "xp_1_1") is True
    assert _child(drv, "Mix", "xp_1_2") is False and _child(drv, "Mix", "xp_2_2") is True
    assert _child(drv, "Room_EQ", "band_2_type") == "B"
    assert _child(drv, "PSTN_In_1", "call_status") == "HANGUP" and _child(drv, "PSTN_In_1", "call_active") is False
    assert _child(drv, "Scenes", "selection") == 1
    assert _child(drv, "Hook", "value") is False
    assert _child(drv, "Main_Volume", "responding") is True and _child(drv, "Inputs", "responding") is False
    assert _dev(drv, "modules_responding") == 11
    assert _child(drv, "Main_Volume", "module_type") == "Gain (also PSTN / VoIP Output, CSP Listening Area Gain)"
    # Groups, parameter set and network.
    assert _child(drv, 1, "level_db", "group") == 0.0 and _child(drv, 1, "mute", "group") is False
    assert _child(drv, 2, "source", "group") == 1 and _child(drv, 2, "responding", "group") is True
    assert _dev(drv, "parameter_set") == 0 and _dev(drv, "parameter_set_name") == ""
    assert json.loads(_dev(drv, "parameter_set_options")) == [{"value": 2, "label": "2: Party"},
                                                              {"value": 11, "label": "11: Meeting"}]
    assert _dev(drv, "ip_address") == "192.168.0.160" and _dev(drv, "subnet_mask") == "255.255.255.0"
    assert _dev(drv, "gateway") == "192.168.0.1" and _dev(drv, "addressing") == "static"
    assert _child(drv, 1, "joined", "room_combine") == "[1][2][3][4][5][6]"
    assert _child(drv, 1, "joined_1_2", "room_combine") is False
    assert _dev(drv, "config_problems") == ""


@pytest.mark.asyncio
async def test_a_processor_without_sub_is_polled():
    drv, sim = _make(sim_config={**_config(), "push_supported": False})
    await drv.connect()
    await _settle()
    assert _dev(drv, "push_supported") is False
    assert sim.subscription_count == 0
    lines = drv.transport.lines()
    assert not any(ln.startswith("SUB ") for ln in lines)
    assert 'GA "Main Volume">1' in lines and "GG 1" in lines and "GN 1" in lines and "GS" in lines
    assert lines[-1] == "GRC 1"
    assert _child(drv, "Main_Volume", "level") == 0.0 and _child(drv, 2, "source", "group") == 1
    sim.set_value("Main Volume", "level", -9.0)      # nothing is pushed without a subscription
    await _settle()
    assert _child(drv, "Main_Volume", "level") == 0.0
    await drv.poll()
    await _settle()
    assert _child(drv, "Main_Volume", "level") == -9.0


# ── Commands ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_toggle_pulse_step_read_back_after_every_write():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    start = len(drv.transport.lines())
    await drv.send_command("set_control", {"module": "Main_Volume", "control": "level", "value": "-12.5"})
    assert drv.transport.lines()[start:] == ['SA "Main Volume">1=-12.5', 'GA "Main Volume">1']
    assert sim.value_of("Main Volume", "level") == -12.5
    assert _child(drv, "Main_Volume", "level") == -12.5
    await drv.send_command("set_control", {"module": "Main Volume", "control": "Mute", "value": "on"})
    assert sim.value_of("Main Volume", "mute") is True and _child(drv, "Main_Volume", "mute") is True
    await drv.send_command("toggle_control", {"module": "Main_Volume", "control": "mute"})
    assert drv.transport.lines()[-2] == 'SA "Main Volume">2=T'
    assert _child(drv, "Main_Volume", "mute") is False
    await drv.send_command("set_control", {"module": "Room_EQ", "control": "band_2_type", "value": "Low Cut (High Pass)"})
    assert drv.transport.lines()[-2] == 'SA "Room EQ">2>5=LC' and _child(drv, "Room_EQ", "band_2_type") == "LC"
    await drv.send_command("set_control", {"module": "Input_1", "control": "gain", "value": "24"})
    assert _child(drv, "Input_1", "gain") == "24"
    await drv.send_command("set_control", {"module": "Selector_1", "control": "source", "value": "3"})
    assert _child(drv, "Selector_1", "source") == 3
    await drv.send_command("set_control", {"module": "Scenes", "control": "selection", "value": "2"})
    assert drv.transport.lines()[-2:] == ['SA "Scenes">1=2', 'GA "Scenes">2']
    assert _child(drv, "Scenes", "selection") == 2
    await drv.send_command("pulse_control", {"module": "Logic_Input_1", "control": "pin_2"})
    assert drv.transport.lines()[-2] == 'SA "Logic Input 1">2>1=P'
    await drv.send_command("step_level", {"module": "Main_Volume", "control": "level", "amount": -3})
    assert _child(drv, "Main_Volume", "level") == -15.5
    await drv.send_command("step_level", {"module": "Main_Volume", "control": "level", "amount": 50})
    assert _child(drv, "Main_Volume", "level") == 12.0          # clamped at +12
    await drv.send_command("set_control", {"module": "Hook", "control": "value", "value": "1"})
    assert drv.transport.lines()[-2] == 'SA "Hook">0>9=O'
    with pytest.raises(ValueError, match="out of range"):
        await drv.send_command("set_module_parameter", {"module_name": "Main Volume", "index": "1", "value": "99"})
    with pytest.raises(ValueError, match="Invalid module name"):
        await drv.send_command("set_module_parameter", {"module_name": "Nope", "index": "1", "value": "0"})
    assert "Invalid module name" in _dev(drv, "last_error")
    with pytest.raises(ValueError, match="Illegal index"):
        await drv.send_command("query_module_parameter", {"module_name": "Main Volume", "index": "9"})
    assert await drv.send_command("query_module_parameter", {"module_name": "Main Volume", "index": "1"}) == "12"
    with pytest.raises(ValueError, match="above the maximum"):
        await drv.send_command("set_control", {"module": "Main_Volume", "control": "level", "value": "13"})
    with pytest.raises(ValueError, match="read-only"):
        await drv.send_command("set_control", {"module": "PSTN_In_1", "control": "call_status", "value": "x"})
    with pytest.raises(ValueError, match="not an on/off"):
        await drv.send_command("toggle_control", {"module": "Main_Volume", "control": "level"})
    with pytest.raises(ValueError, match="not a logic pin"):
        await drv.send_command("pulse_control", {"module": "Main_Volume", "control": "mute"})
    with pytest.raises(ValueError, match="no control named"):
        await drv.send_command("set_control", {"module": "Main_Volume", "control": "nope", "value": "1"})
    with pytest.raises(ValueError, match="not one of the declared"):
        await drv.send_command("set_control", {"module": "Ghost", "control": "level", "value": "1"})


@pytest.mark.asyncio
async def test_cross_points_read_back_through_the_routing_mask():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    start = len(drv.transport.lines())
    await drv.send_command("set_control", {"module": "Mix", "control": "xp_1_2", "value": "on"})
    assert drv.transport.lines()[start:] == ['SA "Mix">4>(1,2)=O', 'GA "Mix">3>1']
    assert _child(drv, "Mix", "xp_1_2") is True and _child(drv, "Mix", "input_1_routing") == "C0000000"
    await drv.send_command("toggle_control", {"module": "Mix", "control": "xp_1_1"})
    assert _child(drv, "Mix", "xp_1_1") is False and _child(drv, "Mix", "input_1_routing") == "40000000"
    await drv.send_command("set_control", {"module": "Matrix_1", "control": "xp_2_1_level", "value": "-20"})
    assert drv.transport.lines()[-2:] == ['SA "Matrix 1">2>3=-20', 'GA "Matrix 1">2>3']
    assert _child(drv, "Matrix_1", "xp_2_1_level") == -20.0
    with pytest.raises(ValueError, match="above the maximum"):
        await drv.send_command("set_control", {"module": "Matrix_1", "control": "xp_2_1_level", "value": "3"})


@pytest.mark.asyncio
async def test_change_at_the_processor_is_pushed():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    assert sim.set_value("Main Volume", "level", -20.0)
    sim.set_state("input_1_mute", True)               # a Simulator-UI control
    sim.set_value("Mix", "xp_2_1", True)              # a cross-point: the mask is pushed
    sim.set_parameter_set(11)
    sim.set_group_level(1, 0x80)
    sim.set_group_mute(1, True)
    await _settle()
    assert _child(drv, "Main_Volume", "level") == -20.0
    assert _child(drv, "Input_1", "mute") is True
    assert _child(drv, "Mix", "xp_2_1") is True and _child(drv, "Mix", "input_2_routing") == "C0000000"
    assert _dev(drv, "parameter_set") == 11 and _dev(drv, "parameter_set_name") == "Meeting"
    assert _child(drv, 1, "level_db", "group") == 4.0 and _child(drv, 1, "mute", "group") is True
    await drv.send_command("resync", {})
    await _settle()
    assert _child(drv, "Main_Volume", "level") == -20.0


@pytest.mark.asyncio
async def test_groups_parameter_sets_and_room_combine():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    await drv.send_command("set_group_level", {"group": 1, "level": -20})
    assert drv.transport.lines()[-2:] == ["SG 1,50", "GG 1"]
    assert sim.group_level_raw(1) == 0x50 and _child(drv, 1, "level_db", "group") == -20.0
    await drv.send_command("step_group_level", {"group": 1, "amount": 3})
    assert drv.transport.lines()[-2] == "SH 1,1,6" and _child(drv, 1, "level_db", "group") == -17.0
    await drv.send_command("step_group_level", {"group": 1, "amount": -1.5})
    assert drv.transport.lines()[-2] == "SH 1,0,3" and _child(drv, 1, "level_db", "group") == -18.5
    await drv.send_command("set_group_level", {"group": 1, "level": -60.5})
    assert drv.transport.lines()[-2] == "SG 1,ff" and _child(drv, 1, "level_db", "group") == -60.5
    await drv.send_command("set_group_mute", {"group": 1, "mute": True})
    assert drv.transport.lines()[-2:] == ["SN 1,M", "GN 1"] and _child(drv, 1, "mute", "group") is True
    await drv.send_command("toggle_group_mute", {"group": 1})
    assert drv.transport.lines()[-2] == "SN 1,T" and _child(drv, 1, "mute", "group") is False
    await drv.send_command("set_group_source", {"group": 2, "channel": 3})
    assert drv.transport.lines()[-2:] == ["SG 2,3", "GG 2"] and _child(drv, 2, "source", "group") == 3
    with pytest.raises(ValueError, match="selector group"):
        await drv.send_command("set_group_level", {"group": 2, "level": 0})
    with pytest.raises(ValueError, match="volume group"):
        await drv.send_command("set_group_source", {"group": 1, "channel": 1})
    with pytest.raises(ValueError, match="not in the Groups table"):
        await drv.send_command("set_group_mute", {"group": 9, "mute": True})
    await drv.send_command("recall_parameter_set", {"number": 11})
    assert drv.transport.lines()[-2:] == ["SS b", "GS"]
    assert sim.parameter_set == 11 and _dev(drv, "parameter_set") == 11 and _dev(drv, "parameter_set_name") == "Meeting"
    await drv.send_command("recall_parameter_set", {"number": 7})
    assert _dev(drv, "parameter_set_name") == "Parameter Set 7"
    await drv.send_command("join_rooms", {"group": 1, "room_a": 2, "room_b": 4})
    assert drv.transport.lines()[-2:] == ["SRC 1,2,4,J", "GRC 1"]
    assert sim.joined_rooms(1)[1] == {2, 4}
    assert _child(drv, 1, "joined", "room_combine") == "[1][2,4][3][5][6]"
    assert _child(drv, 1, "joined_2_4", "room_combine") is True and _child(drv, 1, "joined_1_2", "room_combine") is False
    await drv.send_command("join_rooms", {"group": 1, "room_a": 4, "room_b": 5})
    assert _child(drv, 1, "joined_2_5", "room_combine") is True
    await drv.send_command("split_rooms", {"group": 1, "room_a": 2, "room_b": 4})
    assert drv.transport.lines()[-2] == "SRC 1,2,4,S"
    assert _child(drv, 1, "joined_2_4", "room_combine") is False and _child(drv, 1, "joined_2_5", "room_combine") is True
    with pytest.raises(ValueError, match="not declared"):
        await drv.send_command("join_rooms", {"group": 2, "room_a": 1, "room_b": 2})
    sim.join_rooms(1, 1, 3)                            # a partition moved at the wall controller
    await drv.poll()
    await _settle()
    assert _child(drv, 1, "joined_1_3", "room_combine") is True


@pytest.mark.asyncio
async def test_slot_commands_and_network():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    result = await drv.send_command("set_io_level", {"slot": "1", "channel": "3", "level": -20})
    assert drv.transport.lines()[-2:] == ["SV 1,3,50", "GV 1,3"] and result["level_db"] == -20.0
    result = await drv.send_command("step_io_level", {"slot": "2", "channel": "3", "amount": 3})
    assert drv.transport.lines()[-2] == "SI 2,3,1,6" and result["level_db"] == 3.0
    result = await drv.send_command("set_io_mute", {"slot": "2", "channel": "1", "state": "M"})
    assert drv.transport.lines()[-2:] == ["SM 2,1,M", "GM 2,1"] and result["muted"] is True
    with pytest.raises(ValueError):
        await drv.send_command("set_io_level", {"slot": "zz", "channel": "1", "level": 0})
    await drv.send_command("set_ip_address", {"address": "192.168.1.160"})
    await drv.send_command("set_network_parameter", {"parameter": "T", "value": "d"})
    await drv.send_command("set_network_parameter", {"parameter": "G", "value": "192.168.1.2"})
    assert drv.transport.lines()[-3:] == ["IP 192.168.1.160", "NP T,D", "NP G,192.168.1.2"]
    with pytest.raises(ValueError):
        await drv.send_command("set_network_parameter", {"parameter": "M", "value": "wide"})
    await drv.send_command("reboot", {})
    assert drv.transport.lines()[-1] == "RESET"
    await drv.send_command("reset_network_defaults", {})
    assert drv.transport.lines()[-1] == "NP F"


@pytest.mark.asyncio
async def test_signal_levels_learn_their_channel_count():
    drv, sim = _make(enable_meters=True)
    await drv.connect()
    await _settle()
    assert drv._meter_task is not None
    assert _child(drv, "Inputs", "level_4") is not None          # the loop's first read
    assert _child(drv, "Inputs", "level_1") <= 0.0 and _child(drv, "Inputs", "level_1") >= -60.0
    assert _child(drv, "AEC_ERLE", "level_4") is not None         # 4 channels learned from the reply
    assert _child(drv, "AEC_ERLE", "level_5") is None
    assert drv._by_cid["AEC_ERLE"].signal_level.channels == 4
    assert _child(drv, "Analog_Out", "level_1") >= -35.0 and _child(drv, "Analog_Out", "level_1") <= 25.0
    assert _child(drv, "Inputs", "responding") is True
    queries = [ln for ln in drv.transport.lines() if ln.startswith("GL")]
    assert queries[:3] == ["GL 1", "GL 8,3", "GL 2"]
    schema = drv.get_child_schema("module", "Analog_Out")
    assert schema["level_1"]["unit"] == "dBu" and schema["level_1"]["min"] == -35.0
    assert schema["level_1"]["cloud_priority"] == "low"
    assert drv.get_child_schema("module", "Inputs")["level_1"]["unit"] == "dBFS"
    await drv.disconnect()
    assert drv._meter_task is None
    drv2, _ = _make()
    await drv2.connect()
    await _settle()
    assert _child(drv2, "Inputs", "level_1") is None              # off by default
    assert await drv2.read_meters() == 3
    assert _child(drv2, "Inputs", "level_1") is not None


@pytest.mark.asyncio
async def test_telephone_calls():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    await drv.send_command("make_call", {"module": "PSTN_In_1", "number": "08707414500"})
    assert drv.transport.lines()[-4] == 'MA "PSTN In 1">2="08707414500"'
    assert _child(drv, "PSTN_In_1", "call_status") == "ACTIVE" and _child(drv, "PSTN_In_1", "call_active") is True
    assert _child(drv, "PSTN_In_1", "caller_id") == "08707414500"
    await drv.send_command("dial_key", {"module": "PSTN_In_1", "key": "#"})
    assert drv.transport.lines()[-4] == 'MA "PSTN In 1">1="#"'
    await drv.send_command("end_call", {"module": "PSTN_In_1"})
    assert drv.transport.lines()[-4] == 'MA "PSTN In 1">3'
    assert _child(drv, "PSTN_In_1", "call_status") == "HANGUP" and _child(drv, "PSTN_In_1", "call_active") is False
    with pytest.raises(ValueError, match="Illegal index"):
        await drv.send_command("dial_key", {"module": "PSTN_In_1", "key": "1"})
    with pytest.raises(ValueError, match="only a VoIP"):
        await drv.send_command("transfer_call", {"module": "PSTN_In_1", "number": "101"})
    with pytest.raises(ValueError, match="not a PSTN or VoIP"):
        await drv.send_command("answer_call", {"module": "Main_Volume"})
    await drv.send_command("invoke_module_action", {"module_name": "PSTN In 1", "index": 4})
    assert drv.transport.lines()[-1] == 'MA "PSTN In 1">4'


# ── Liveness, NAK, reconnect ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_liveness_probe_resolves_and_times_out():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    await drv._liveness_probe()
    assert drv.transport.lines()[-1] == "GS"
    drv.transport.silent = True
    with pytest.raises(TimeoutError):
        await drv._liveness_probe()
    assert drv._waiters == []


@pytest.mark.asyncio
async def test_unsolicited_nak_lands_in_last_error():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    await drv.transport.deliver(b"\x1502\r")
    assert "Illegal index" in _dev(drv, "last_error")
    await drv.transport.deliver(b"\x06")               # a stray ACK is ignored
    await drv.transport.deliver(b'GA "Ghost">1=5\r')  # an undeclared module is ignored
    await drv.transport.deliver(b"Ready\r")


@pytest.mark.asyncio
async def test_reconnect_resubscribes_and_poll_resyncs():
    drv, sim = _make()
    await drv.connect()
    await _settle()
    first = len(drv.transport.lines())
    await drv.disconnect()
    assert _dev(drv, "connected") is False
    await drv.connect()
    await _settle()
    assert len(drv.transport.lines()) == first
    assert sim.subscription_count > 0
    sim._values[sim._find("Main Volume", "level")] = -7.0     # changed while nobody listened
    await drv.poll()
    await _settle()
    assert _child(drv, "Main_Volume", "level") == -7.0
    assert drv.transport.lines()[-1] == "GRC 1"


# ── Test Connection over a real socket ──────────────────────────────────────

class _SocketSim:
    def __init__(self, sim):
        self.sim = sim
        self.server = None

    async def __aenter__(self):
        async def handle(reader, writer):
            buf = b""
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buf += data
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    reply = self.sim.handle_command(line)
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
async def test_test_connection_reports_answered_rejected_and_silent():
    rows = [dict(r) for r in MODULES] + [{"name": "Ghost", "type": "gain"}]
    drv, sim = _make(sim_config=_config(), modules=rows)
    drv.PROBE_TIMEOUT_S = 0.3
    steps = []

    async def progress(msg, pct):
        steps.append((msg, pct))

    async with _SocketSim(sim) as port:
        drv.config["host"], drv.config["port"] = "127.0.0.1", port
        result = await drv.run_setup_action("test_connection", {}, progress)
    assert result["ok"] is False
    assert result["push_supported"] is True and result["ip_address"] == "192.168.0.160"
    assert result["parameter_set"] == 0
    assert result["rejected"] == ["Ghost (Invalid module name: no module in the loaded design has that "
                                  "label, or two modules share it)"]
    assert result["silent"] == []
    assert "Lobby (group 1)" in result["answered"] and "Inputs (GL 1)" in result["answered"]
    assert "16 of 17 answered" in result["message"]      # 12 GA probes + 3 GL + 2 groups, one rejected
    assert steps[-1][1] == 100

    drv2, sim2 = _make(sim_config={**_config(), "push_supported": False})
    drv2.PROBE_TIMEOUT_S = 0.3
    async with _SocketSim(sim2) as port:
        drv2.config["host"], drv2.config["port"] = "127.0.0.1", port
        result = await drv2.run_setup_action("test_connection", {}, progress)
    assert result["ok"] is True and result["push_supported"] is False
    assert "polled" in result["message"]

    drv3, _ = _make()
    drv3.config["host"], drv3.config["port"] = "127.0.0.1", 1
    with pytest.raises(ConnectionError):
        await drv3.run_setup_action("test_connection", {}, progress)


# ── Catalog surface ─────────────────────────────────────────────────────────

def test_an_empty_table_says_where_the_rows_go():
    drv = DRV.BoseControlSpaceDriver("esp", _config(modules=[], groups=[], room_combine_groups=0),
                                     StubState(), StubEvents())
    assert drv._modules == [] and any("Modules table" in p for p in drv._problems)
    drv2 = DRV.BoseControlSpaceDriver("esp", _config(), StubState(), StubEvents())
    assert drv2._problems == []
    drv3 = DRV.BoseControlSpaceDriver("esp", _config(modules=[]), StubState(), StubEvents())
    assert drv3._problems == []                      # groups alone are a valid device


def test_catalog_surface():
    info = DRV.BoseControlSpaceDriver.DRIVER_INFO
    assert info["id"] == "bose_controlspace"
    assert set(info["quick_actions"]) <= set(info["commands"])
    for a in info["actions"]:
        if a["kind"] == "command":
            assert a["id"] in info["commands"]
    assert info["child_entity_types"]["module"]["dynamic"] is True
    assert info["child_entity_types"]["group"]["dynamic"] is True
    assert "dynamic" not in info["child_entity_types"]["room_combine"]
    assert info["transports"] == ["tcp", "serial"]
    assert info["discovery"]["tcp_probe"]["send_ascii"] == "IP\r"
    assert "Nothing on the processor changes" in next(a for a in info["actions"] if a["id"] == "test_connection")["confirm"]
    assert info["commands"]["recall_parameter_set"]["params"]["number"]["options_state"] == "parameter_set_options"
    assert len(info["discovery"]["oui"]) == 14


@pytest.mark.asyncio
async def test_every_declared_command_has_a_branch():
    """A declared command that send_command never handles answers success
    and does nothing; the fall-through here is an 'Unknown command' error,
    so every declared name must get past it."""
    drv, sim = _make()
    await drv.connect()
    await _settle()
    for name in DRV.COMMANDS:
        try:
            await drv.send_command(name, {})
        except ValueError as exc:
            assert "Unknown command" not in str(exc), name
        except (KeyError, TypeError):
            pass
