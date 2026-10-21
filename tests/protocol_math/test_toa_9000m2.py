"""Protocol math for the TOA 9000M2 driver.

Checks the frame length byte, the fader, crosspoint, tone and ANC value
tables, and the bytes of each command and echo against the worked examples
and tables in TOA's "9000M2 Series Mixer/Amplifiers RS-232C Protocol Manual"
Ver.2.00A (2011/11/30). Section numbers below are that manual's.
"""

from __future__ import annotations

import asyncio

import pytest

from _platform_probe import load_driver

toa = load_driver("audio/toa_9000m2.py")

from openavc.core.event_bus import EventBus  # noqa: E402
from openavc.core.state_store import StateStore  # noqa: E402


class _FakeTransport:
    """Stands in for the serial line: records each frame sent."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.connected = True

    async def send(self, data):
        self.sent.append(bytes(data))


def _driver():
    d = toa.TOA9000M2Driver(
        "toa", {"port": "/dev/ttyUSB0", "baudrate": 9600},
        StateStore(), EventBus(),
    )
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


# ── Framing ─────────────────────────────────────────────────────────────────

def test_build_frame_sets_length():
    """9000M2 manual 2.00A sec 3 layout; examples 91 03 00 00 6A (sec 5.1.1, sec 10) and F4 01 01 (sec 5.5)."""
    assert toa.build_frame(0x91, 0x00, 0x00, 0x6A) == _hx("910300006A")
    assert toa.build_frame(0xF4, 0x01) == _hx("F40101")


def test_parse_single_frame():
    """9000M2 manual 2.00A sec 3 (length counts the data bytes); frame 91 03 00 00 6A from sec 5.1.1."""
    frame, rest = toa.parse_frame(_hx("910300006A"))
    assert frame == _hx("910300006A")
    assert rest == b""


def test_parse_two_concatenated_frames():
    """9000M2 manual 2.00A frames 91 03 00 00 6A (sec 5.1.1) and F4 01 01 (sec 5.5) back to back."""
    frame, rest = toa.parse_frame(_hx("910300006A") + _hx("F40101"))
    assert frame == _hx("910300006A")
    frame2, rest2 = toa.parse_frame(rest)
    assert frame2 == _hx("F40101")
    assert rest2 == b""


# ── Value tables ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pos,db", [
    (0x00, toa.MUTE_DB), (0x01, -70.0), (0x06, -60.0), (0x07, -59.0),
    (0x1A, -40.0), (0x6A, 0.0), (0x6C, 1.0), (0x7E, 10.0),
])
def test_fader_position_to_db(pos, db):
    """9000M2 manual 2.00A sec 5.1.1 CHANNEL FADER GAIN - POSITION V/S GAIN TABLE (page 6); 00H is -inf, reported as MUTE_DB."""
    assert toa.fader_pos_to_db(pos) == db


@pytest.mark.parametrize("db,pos", [
    (0.0, 0x6A), (10.0, 0x7E), (-40.0, 0x1A), (-60.0, 0x06), (-70.0, 0x01),
    (-80.0, 0x00), (1.0, 0x6C),
])
def test_fader_db_to_position(db, pos):
    """9000M2 manual 2.00A sec 5.1.1 POSITION V/S GAIN TABLE (page 6); -80.0 is the driver's MUTE_DB for the table's -inf (00H)."""
    assert toa.fader_db_to_pos(db) == pos


@pytest.mark.parametrize("step_db,byte", [
    (0.5, 0x41), (1.0, 0x42), (15.5, 0x5F), (-0.5, 0x61), (-1.0, 0x62), (-15.5, 0x7F),
])
def test_fader_step_to_byte(step_db, byte):
    """9000M2 manual 2.00A sec 5.1.3 STEP UP / STEP DOWN TABLE (41H +0.5 .. 5FH +15.5, 61H -0.5 .. 7FH -15.5)."""
    assert toa.fader_step_to_byte(step_db) == byte


@pytest.mark.parametrize("db,pos", [
    (0.0, 0x47), (10.0, 0x51), (-20.0, 0x33), (-70.0, 0x01), (-80.0, 0x00),
])
def test_crosspoint_db_to_position(db, pos):
    """9000M2 manual 2.00A sec 5.2 CROSSPOINT GAIN TABLE (01H -70, 33H -20, 47H 0, 51H +10); -80.0 is MUTE_DB for 00H (-inf)."""
    assert toa.xpt_db_to_pos(db) == pos


@pytest.mark.parametrize("pos,db", [(0x47, 0.0), (0x51, 10.0), (0x33, -20.0), (0x01, -70.0)])
def test_crosspoint_position_to_db(pos, db):
    """9000M2 manual 2.00A sec 5.2 CROSSPOINT GAIN TABLE (page 12)."""
    assert toa.xpt_pos_to_db(pos) == db


@pytest.mark.parametrize("step,byte", [(1, 0x70), (16, 0x7F), (-1, 0x60), (-16, 0x6F)])
def test_crosspoint_step_to_byte(step, byte):
    """9000M2 manual 2.00A sec 5.2 <Value>: 60-6FH 1-16 dB step down, 70-7FH 1-16 dB step up (CROSSPOINT GAIN TABLE)."""
    assert toa.xpt_step_to_byte(step) == byte


@pytest.mark.parametrize("db,byte", [(-12.0, 0x00), (0.0, 0x0C), (12.0, 0x18), (5.0, 0x11)])
def test_tone_gain_db_to_byte(db, byte):
    """9000M2 manual 2.00A sec 5.6 TONE CONTROL GAIN POSITION TABLE (00H -12, 0CH 0, 11H +5, 18H +12)."""
    assert toa.gain_db_to_byte(db) == byte


def test_anc_conversions():
    """9000M2 manual 2.00A sec 8.1 ANC Adjust Value Table and sec 10 ANC Get Reference response."""
    assert toa.anc_db_to_byte(-9.0) == 0x01     # sec 8.1 example: ch1 to -9 dB = value 01
    assert toa.anc_ref_byte_to_db(0x32) == 0.0  # sec 10: C2 01 32 = 0 dB


# ── Command encoding ────────────────────────────────────────────────────────

def test_set_input_gain_0db():
    """9000M2 manual 2.00A sec 5.1.1 example / sec 10: input ch1 fader 0 dB = 91 03 00 00 6A."""
    assert _send(_driver(), "set_input_gain", channel=1, db=0.0) == _hx("910300006A")


def test_set_output_gain_minus_inf():
    """9000M2 manual 2.00A sec 10: output ch1 fader -inf = 91 03 01 00 00."""
    assert _send(_driver(), "set_output_gain", channel=1, db=-80.0) == _hx("9103010000")


def test_set_paging_output_gain():
    """9000M2 manual 2.00A sec 5.1.2 example: paging output ch1 0 dB = 96 03 01 00 6A (sec 10's table prints 96 03 01 01 6A)."""
    assert _send(_driver(), "set_paging_output_gain", channel=1, db=0.0) == _hx("960301006A")


def test_step_input_gain_up():
    """9000M2 manual 2.00A sec 5.1.3 example / sec 10: input ch1 +0.5 dB step = 93 03 00 00 41."""
    assert _send(_driver(), "step_input_gain", channel=1, step_db=0.5) == _hx("9303000041")


def test_set_crosspoint_0db():
    """9000M2 manual 2.00A sec 5.2 Example 1 / sec 10: In1 -> Out1 at 0 dB = 95 05 00 00 01 00 47."""
    assert _send(_driver(), "set_crosspoint", input=1, output=1, db=0.0) == _hx("95050000010047")


def test_set_crosspoint_manual_example_minus20():
    """9000M2 manual 2.00A sec 10: In3 -> Out5 at -20 dB = 95 05 00 02 01 04 33."""
    assert _send(_driver(), "set_crosspoint", input=3, output=5, db=-20.0) == _hx("9505000201 0433".replace(" ", ""))


def test_step_crosspoint_manual_example():
    """9000M2 manual 2.00A sec 10: In4 -> Out1, 1 step up = 95 05 00 03 01 00 70."""
    assert _send(_driver(), "step_crosspoint", input=4, output=1, step_db=1) == _hx("9505000301 0070".replace(" ", ""))


def test_tone_manual_example():
    """9000M2 manual 2.00A sec 10: input ch1 bass -5 dB = AA 04 00 00 00 07."""
    assert _send(_driver(), "set_input_tone", channel=1, band="bass", db=-5.0) == _hx("AA0400000007")


def test_eq_manual_example():
    """9000M2 manual 2.00A sec 5.7 example: input ch1 EQ on, band 1, +2 dB, Q 0.7, 40 Hz = A1 07 00 00 01 00 0E 02 03."""
    frame = _send(_driver(), "set_input_eq", channel=1, state="on", band=1,
                  gain_db=2.0, q="0.7", freq_hz="40")
    assert frame == _hx("A10700000100 0E0203".replace(" ", ""))


def test_filter_manual_example():
    """9000M2 manual 2.00A sec 5.9 example: input ch3 HPF 31.5 Hz = A2 04 00 02 00 03."""
    assert _send(_driver(), "set_input_filter", channel=3, filter="hpf", freq_hz="31.5") == _hx("A20400020003")


def test_input_sensitivity_manual_example():
    """9000M2 manual 2.00A sec 5.10 example: input ch5 -24 dB = AC 02 04 02."""
    assert _send(_driver(), "set_input_sensitivity", channel=5, sensitivity_db="-24") == _hx("AC020402")


def test_anc_adjust_manual_example():
    """9000M2 manual 2.00A sec 8.1 example: ANC ch1 to -9 dB = AE 02 00 01."""
    assert _send(_driver(), "set_anc_adjust", channel=1, db=-9.0) == _hx("AE020001")


# ── Echo parsing ────────────────────────────────────────────────────────────

def test_parse_fader_echo():
    """9000M2 manual 2.00A sec 10 response 91 03 00 00 6A: input ch1 at 0 dB."""
    d = _driver()
    _feed(d, _hx("910300006A"))
    assert d.get_state("input_1_fader_db") == 0.0


def test_parse_fader_minus_inf():
    """9000M2 manual 2.00A sec 10 response 91 03 01 00 00: output ch1 at -inf (MUTE_DB)."""
    d = _driver()
    _feed(d, _hx("9103010000"))
    assert d.get_state("output_1_fader_db") == toa.MUTE_DB


def test_parse_step_result_is_absolute():
    """9000M2 manual 2.00A sec 5.1.3 / sec 10: after +0.5 step the amp replies 93 03 00 00 6C (= +1.0 dB result)."""
    d = _driver()
    _feed(d, _hx("9303 00006C".replace(" ", "")))
    assert d.get_state("input_1_fader_db") == 1.0


def test_parse_tone_echo():
    """9000M2 manual 2.00A sec 10 response AA 04 00 00 00 07: input ch1 bass -5 dB."""
    d = _driver()
    _feed(d, _hx("AA0400000007"))
    assert d.get_state("input_1_bass_db") == -5.0


def test_parse_crosspoint_echo():
    """9000M2 manual 2.00A sec 10 response 95 05 00 02 01 04 33: In3 -> Out5 at -20 dB."""
    d = _driver()
    _feed(d, _hx("9505000201043 3".replace(" ", "")))
    assert d.get_state("xpt_i3_o5_db") == -20.0
