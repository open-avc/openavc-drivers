"""Byte-exact unit tests for the toa_9000m2 driver + simulator.

Loads ``audio/toa_9000m2.py`` and ``audio/toa_9000m2_sim.py`` directly, stubbing
the ``server.*`` / ``simulator.*`` imports they need so the community repo's test
suite stays self-contained (stdlib only).

Every command's on-the-wire bytes and every parsed response are checked against
the worked examples in the "9000M2 Series RS-232C Protocol Manual" (Ver 2.00A) --
e.g. the manual states input ch1 at 0 dB is ``91 03 00 00 6A``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "toa_9000m2.py"
SIM_PATH = REPO_ROOT / "audio" / "toa_9000m2_sim.py"


# ── Platform stubs ──────────────────────────────────────────────────────────

def _install_stubs() -> None:
    if "server.drivers.base" not in sys.modules:
        server = ModuleType("server")
        server.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault("server", server)

        drivers = ModuleType("server.drivers")
        drivers.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault("server.drivers", drivers)

        base = ModuleType("server.drivers.base")

        class CommandParamError(ValueError):
            pass

        class _BaseDriver:
            DRIVER_INFO: dict = {}

            def __init__(self, device_id, config, state, events):
                self.device_id = device_id
                self.config = config
                self.state = state
                self.events = events
                self.transport = None
                self._connected = False
                self._states: dict = {}

            def set_state(self, key, value):
                self._states[key] = value

            def get_state(self, key):
                return self._states.get(key)

        base.BaseDriver = _BaseDriver
        base.CommandParamError = CommandParamError
        sys.modules["server.drivers.base"] = base

        transport = ModuleType("server.transport")
        transport.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault("server.transport", transport)
        fp = ModuleType("server.transport.frame_parsers")

        class FrameParser:
            pass

        class CallableFrameParser(FrameParser):
            def __init__(self, parse_fn, max_buffer=65536):
                self._parse_fn = parse_fn
                self._buffer = b""

            def feed(self, data):
                self._buffer += data
                out = []
                while True:
                    before = len(self._buffer)
                    msg, remaining = self._parse_fn(self._buffer)
                    # The returned buffer is authoritative on BOTH branches: a
                    # parse function drops garbage or resyncs past a corrupt
                    # frame by returning less buffer with no message.
                    self._buffer = remaining
                    if msg is None:
                        if len(remaining) >= before:
                            break      # nothing parsed, nothing consumed
                        continue       # bytes dropped: retry on the remainder
                    out.append(msg)
                    if len(remaining) >= before:
                        break          # no forward progress guard
                return out

            def reset(self):
                self._buffer = b""

        fp.FrameParser = FrameParser
        fp.CallableFrameParser = CallableFrameParser
        sys.modules["server.transport.frame_parsers"] = fp

        utils = ModuleType("server.utils")
        utils.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault("server.utils", utils)
        logger = ModuleType("server.utils.logger")

        class _Log:
            def __getattr__(self, _):
                return lambda *a, **k: None
        logger.get_logger = lambda *_a, **_k: _Log()
        sys.modules["server.utils.logger"] = logger

    if "simulator.tcp_simulator" not in sys.modules:
        simulator = ModuleType("simulator")
        simulator.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault("simulator", simulator)
        tcp_sim = ModuleType("simulator.tcp_simulator")

        class _TCPSimulator:
            SIMULATOR_INFO: dict = {}

            def __init__(self, device_id, config=None):
                self.device_id = device_id
                self.config = config or {}
                self.state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

            def set_state(self, key, value):
                self.state[key] = value

        tcp_sim.TCPSimulator = _TCPSimulator
        sys.modules["simulator.tcp_simulator"] = tcp_sim


def _load(name, path):
    _install_stubs()
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


toa = _load("toa_9000m2", DRIVER_PATH)
sim_mod = _load("toa_9000m2_sim", SIM_PATH)


# ── Test harness ────────────────────────────────────────────────────────────

class _FakeTransport:
    def __init__(self):
        self.sent: list[bytes] = []
        self.connected = True

    async def send(self, data):
        self.sent.append(bytes(data))


def _driver():
    d = toa.TOA9000M2Driver("toa", {"port": "/dev/ttyUSB0", "baudrate": 9600}, object(), object())
    d.transport = _FakeTransport()
    return d


def _send(driver, command, **params):
    """Run a command and return the single frame it put on the wire."""
    asyncio.run(driver.send_command(command, params))
    assert len(driver.transport.sent) == 1, driver.transport.sent
    return driver.transport.sent[0]


def _feed(driver, frame: bytes):
    asyncio.run(driver.on_data_received(frame))


def _hx(s: str) -> bytes:
    return bytes.fromhex(s)


# ── Pure helpers: framing ───────────────────────────────────────────────────

def test_build_frame_sets_length():
    assert toa.build_frame(0x91, 0x00, 0x00, 0x6A) == _hx("910300006A")
    assert toa.build_frame(0xF4, 0x01) == _hx("F40101")


def test_parse_single_frame():
    frame, rest = toa.parse_frame(_hx("910300006A"))
    assert frame == _hx("910300006A")
    assert rest == b""


def test_parse_two_concatenated_frames():
    frame, rest = toa.parse_frame(_hx("910300006A") + _hx("F40101"))
    assert frame == _hx("910300006A")
    frame2, rest2 = toa.parse_frame(rest)
    assert frame2 == _hx("F40101")
    assert rest2 == b""


def test_parse_incomplete_frame_waits():
    frame, rest = toa.parse_frame(_hx("910300"))  # says 3 data bytes, only 1 present
    assert frame is None
    assert rest == _hx("910300")


def test_parse_resyncs_past_leading_garbage():
    # Leading data-range bytes (high bit clear) are not commands; skip them.
    frame, rest = toa.parse_frame(_hx("0001") + _hx("F40101"))
    assert frame == _hx("F40101")
    assert rest == b""


def test_callable_frame_parser_streams_frames():
    parser = toa.CallableFrameParser(toa.parse_frame)
    # Split one frame across two feeds, then a whole extra frame.
    assert parser.feed(_hx("9103")) == []
    msgs = parser.feed(_hx("00006A") + _hx("F40101"))
    assert msgs == [_hx("910300006A"), _hx("F40101")]


# ── Pure helpers: value tables ──────────────────────────────────────────────

@pytest.mark.parametrize("pos,db", [
    (0x00, toa.MUTE_DB), (0x01, -70.0), (0x06, -60.0), (0x07, -59.0),
    (0x1A, -40.0), (0x6A, 0.0), (0x6C, 1.0), (0x7E, 10.0),
])
def test_fader_position_to_db(pos, db):
    assert toa.fader_pos_to_db(pos) == db


@pytest.mark.parametrize("db,pos", [
    (0.0, 0x6A), (10.0, 0x7E), (-40.0, 0x1A), (-60.0, 0x06), (-70.0, 0x01),
    (-80.0, 0x00), (1.0, 0x6C),
])
def test_fader_db_to_position(db, pos):
    assert toa.fader_db_to_pos(db) == pos


def test_fader_db_snaps_to_nearest():
    assert toa.fader_pos_to_db(toa.fader_db_to_pos(0.3)) == 0.5  # 0.3 -> +0.5 dB


@pytest.mark.parametrize("step_db,byte", [
    (0.5, 0x41), (1.0, 0x42), (15.5, 0x5F), (-0.5, 0x61), (-1.0, 0x62), (-15.5, 0x7F),
])
def test_fader_step_to_byte(step_db, byte):
    assert toa.fader_step_to_byte(step_db) == byte


@pytest.mark.parametrize("db,pos", [
    (0.0, 0x47), (10.0, 0x51), (-20.0, 0x33), (-70.0, 0x01), (-80.0, 0x00),
])
def test_crosspoint_db_to_position(db, pos):
    assert toa.xpt_db_to_pos(db) == pos


@pytest.mark.parametrize("pos,db", [(0x47, 0.0), (0x51, 10.0), (0x33, -20.0), (0x01, -70.0)])
def test_crosspoint_position_to_db(pos, db):
    assert toa.xpt_pos_to_db(pos) == db


@pytest.mark.parametrize("step,byte", [(1, 0x70), (16, 0x7F), (-1, 0x60), (-16, 0x6F)])
def test_crosspoint_step_to_byte(step, byte):
    assert toa.xpt_step_to_byte(step) == byte


@pytest.mark.parametrize("db,byte", [(-12.0, 0x00), (0.0, 0x0C), (12.0, 0x18), (5.0, 0x11)])
def test_tone_gain_db_to_byte(db, byte):
    assert toa.gain_db_to_byte(db) == byte


def test_anc_conversions():
    assert toa.anc_db_to_byte(-9.0) == 0x01     # manual: ch1 to -9 dB = value 01
    assert toa.anc_ref_byte_to_db(0x32) == 0.0  # manual: C2 01 32 = 0 dB


# ── Command encoding (each asserted against a manual example) ────────────────

def test_set_input_gain_0db():
    assert _send(_driver(), "set_input_gain", channel=1, db=0.0) == _hx("910300006A")


def test_set_output_gain_minus_inf():
    # Manual: Output ch1 Fader gain = -inf -> 91 03 01 00 00
    assert _send(_driver(), "set_output_gain", channel=1, db=-80.0) == _hx("9103010000")


def test_set_paging_output_gain():
    assert _send(_driver(), "set_paging_output_gain", channel=1, db=0.0) == _hx("960301006A")


def test_step_input_gain_up():
    # Manual: input ch1 +0.5 dB step -> 93 03 00 00 41
    assert _send(_driver(), "step_input_gain", channel=1, step_db=0.5) == _hx("9303000041")


def test_set_crosspoint_0db():
    # Manual: In1 -> Out1 at 0 dB -> 95 05 00 00 01 00 47
    assert _send(_driver(), "set_crosspoint", input=1, output=1, db=0.0) == _hx("95050000010047")


def test_set_crosspoint_manual_example_minus20():
    # Manual: In3 -> Out5 at -20 dB -> 95 05 00 02 01 04 33
    assert _send(_driver(), "set_crosspoint", input=3, output=5, db=-20.0) == _hx("9505000201 0433".replace(" ", ""))


def test_step_crosspoint_manual_example():
    # Manual: In4 -> Out1, 1 step up -> 95 05 00 03 01 00 70
    assert _send(_driver(), "step_crosspoint", input=4, output=1, step_db=1) == _hx("9505000301 0070".replace(" ", ""))


def test_channel_onoff():
    assert _send(_driver(), "set_input_channel", channel=1, state="off") == _hx("9203000000")
    assert _send(_driver(), "set_output_channel", channel=2, state="on") == _hx("9203010101")


def test_power():
    assert _send(_driver(), "power", state="off") == _hx("F40100")
    assert _send(_driver(), "power", state="on") == _hx("F40101")


def test_tone_manual_example():
    # Manual: Input ch1 Bass -5 dB -> AA 04 00 00 00 07
    assert _send(_driver(), "set_input_tone", channel=1, band="bass", db=-5.0) == _hx("AA0400000007")


def test_eq_manual_example():
    # Manual: Input ch1 EQ ON, Band01, Gain +2 dB, Q 0.7, Freq 40 Hz
    #         -> A1 07 00 00 01 00 0E 02 03
    frame = _send(_driver(), "set_input_eq", channel=1, state="on", band=1,
                  gain_db=2.0, q="0.7", freq_hz="40")
    assert frame == _hx("A10700000100 0E0203".replace(" ", ""))


def test_loudness_manual_example():
    # Manual: Input ch4 Loudness ON -> AB 03 00 03 01
    assert _send(_driver(), "set_input_loudness", channel=4, state="on") == _hx("AB03000301")


def test_filter_manual_example():
    # Manual: Input ch3 HPF 31.5 Hz -> A2 04 00 02 00 03
    assert _send(_driver(), "set_input_filter", channel=3, filter="hpf", freq_hz="31.5") == _hx("A20400020003")


def test_input_sensitivity_manual_example():
    # Manual: Input ch5 -24 dB -> AC 02 04 02
    assert _send(_driver(), "set_input_sensitivity", channel=5, sensitivity_db="-24") == _hx("AC020402")


def test_phantom_manual_example():
    # Manual: Input ch1 Phantom ON -> 87 02 00 01
    assert _send(_driver(), "set_input_phantom", channel=1, state="on") == _hx("87020001")


def test_recall_preset():
    assert _send(_driver(), "recall_preset", preset=1) == _hx("F1020000")
    assert _send(_driver(), "recall_preset", preset=2) == _hx("F1020001")


def test_paging_event():
    assert _send(_driver(), "paging_event", event=1, control="start") == _hx("F2020001")


def test_speaker_preset_manual_example():
    # Manual: Output ch2 Speaker Preset F-122 -> AD 02 01 01
    assert _send(_driver(), "set_speaker_preset", channel=2, preset="F-122") == _hx("AD020101")


def test_anc_adjust_manual_example():
    # Manual: ANC ch1 to -9 dB -> AE 02 00 01
    assert _send(_driver(), "set_anc_adjust", channel=1, db=-9.0) == _hx("AE020001")


def test_get_anc_reference():
    assert _send(_driver(), "get_anc_reference", channel=1) == _hx("F3010 0".replace(" ", ""))


def test_request_channel_name():
    # Manual: Input ch1 name request -> F0 03 40 00 00
    assert _send(_driver(), "request_channel_name", kind="input", channel=1) == _hx("F003400000")


# ── Param validation ────────────────────────────────────────────────────────

def test_bad_channel_raises():
    with pytest.raises(toa.CommandParamError):
        _send(_driver(), "set_input_gain", channel=9, db=0.0)


def test_bad_preset_raises():
    with pytest.raises(toa.CommandParamError):
        _send(_driver(), "recall_preset", preset=99)


# ── Response parsing (echoes + real reads) ──────────────────────────────────

def test_parse_fader_echo():
    d = _driver()
    _feed(d, _hx("910300006A"))
    assert d.get_state("input_1_fader_db") == 0.0


def test_parse_fader_minus_inf():
    d = _driver()
    _feed(d, _hx("9103010000"))
    assert d.get_state("output_1_fader_db") == toa.MUTE_DB


def test_parse_step_result_is_absolute():
    # Manual: after +0.5 step the amp replies 93 03 00 00 6C (= +1.0 dB result).
    d = _driver()
    _feed(d, _hx("9303 00006C".replace(" ", "")))
    assert d.get_state("input_1_fader_db") == 1.0


def test_parse_channel_onoff():
    d = _driver()
    _feed(d, _hx("9203010101"))
    assert d.get_state("output_2_on") is True


def test_parse_power():
    d = _driver()
    _feed(d, _hx("F40101"))
    assert d.get_state("power") is True


def test_parse_tone_echo():
    d = _driver()
    _feed(d, _hx("AA0400000007"))
    assert d.get_state("input_1_bass_db") == -5.0


def test_parse_crosspoint_echo():
    d = _driver()
    _feed(d, _hx("9505000201043 3".replace(" ", "")))
    assert d.get_state("xpt_i3_o5_db") == -20.0


def test_parse_speaker_preset_echo():
    d = _driver()
    _feed(d, _hx("AD020101"))
    assert d.get_state("output_2_speaker_preset") == "F-122"


def test_parse_channel_name():
    # Manual: Input ch1 name "INPUT1" -> C0 09 00 00 49 4E 50 55 54 31 00
    d = _driver()
    _feed(d, _hx("C009000049 4E505554 3100".replace(" ", "")))
    assert d.get_state("input_1_name") == "INPUT1"


def test_parse_anc_reference_uses_pending_channel():
    d = _driver()
    asyncio.run(d.send_command("get_anc_reference", {"channel": 3}))
    _feed(d, _hx("C20132"))  # C2 01 32 = 0 dB
    assert d.get_state("anc_3_reference_db") == 0.0


def test_crosspoint_step_applies_delta_optimistically():
    d = _driver()
    d.set_state("xpt_i4_o1_db", 0.0)
    _feed(d, _hx("9505000301 0072".replace(" ", "")))  # +3 dB step up (0x72)
    assert d.get_state("xpt_i4_o1_db") == 3.0


# ── Simulator ───────────────────────────────────────────────────────────────

def _sim():
    return sim_mod.TOA9000M2Simulator("toa-sim")


def test_sim_echoes_writes():
    resp = _sim().handle_command(_hx("910300006A"))
    assert resp == _hx("910300006A")


def test_sim_name_response():
    resp = _sim().handle_command(_hx("F003400000"))
    # C0 09 00 00 "INPUT1\0" — 7-byte NUL-padded name field.
    assert resp == _hx("C0090000" + b"INPUT1\x00".hex())


def test_sim_step_returns_resulting_position():
    s = _sim()
    s.handle_command(_hx("910300006A"))          # set input ch1 to 0 dB (0x6A)
    resp = s.handle_command(_hx("9303000042"))   # +1.0 dB step (0x42)
    assert resp == _hx("9303 00006C".replace(" ", ""))  # result 0x6C = +1.0 dB


def test_sim_anc_reference_default_0db():
    resp = _sim().handle_command(_hx("F30100"))
    assert resp == _hx("C20132")


def test_sim_buffers_split_and_concatenated_frames():
    s = _sim()
    assert s.handle_command(_hx("9103")) is None            # partial: no reply yet
    resp = s.handle_command(_hx("00006A") + _hx("F40101"))  # completes + a whole frame
    assert resp == _hx("910300006A") + _hx("F40101")


# ── Driver <-> simulator round trip (encode -> sim -> decode) ────────────────

def test_round_trip_input_gain_through_sim():
    d = _driver()
    s = _sim()
    wire = _send(d, "set_input_gain", channel=1, db=-10.0)
    reply = s.handle_command(wire)
    _feed(d, reply)
    assert d.get_state("input_1_fader_db") == -10.0


def test_round_trip_name_through_sim():
    d = _driver()
    s = _sim()
    wire = _send(d, "request_channel_name", kind="output", channel=3)
    reply = s.handle_command(wire)
    _feed(d, reply)
    assert d.get_state("output_3_name") == "OUTPUT3"
