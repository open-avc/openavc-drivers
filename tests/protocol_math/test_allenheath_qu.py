"""Value encoding of allenheath_qu, checked against Allen & Heath's Qu Mixer
MIDI Protocol, Firmware V1.9+: the Reference Tables' Fader / Send Level and
Local Gain columns, and the range the NRPN Pan message gives.
"""

from __future__ import annotations

from _platform_probe import load_driver

qu = load_driver("audio/allenheath_qu.py")


def test_fader_table():
    """Qu MIDI Protocol V1.9+, Reference Tables, Fader / Send Level: +10 dB = 7F, 0 dB = 62, -10 dB = 3F, -inf = 00."""
    assert qu.fader_db_to_byte(0.0) == 0x62
    assert qu.fader_db_to_byte(10.0) == 0x7F
    assert qu.fader_db_to_byte(-10.0) == 0x3F
    assert qu.fader_db_to_byte(-90.0) == 0x00
    assert qu.fader_pos_to_byte(0.0) == 0x00
    assert qu.fader_pos_to_byte(1.0) == 0x7F


def test_gain_table():
    """Qu MIDI Protocol V1.9+, Reference Tables, Local Gain (ID 19): +60 dB = 7F, 0 dB = 0A, -5 dB = 00."""
    assert qu.gain_db_to_byte(0.0) == 0x0A
    assert qu.gain_db_to_byte(60.0) == 0x7F
    assert qu.gain_db_to_byte(-5.0) == 0x00


def test_pan_table():
    """Qu MIDI Protocol V1.9+, NRPN Parameter control, Pan (ID 16): Full Left = 00, Centre = 25, Full Right = 4A."""
    assert qu.pan_to_byte(-1.0) == 0x00
    assert qu.pan_to_byte(0.0) == 0x25
    assert qu.pan_to_byte(1.0) == 0x4A
    assert abs(qu.byte_to_pan(qu.pan_to_byte(0.3)) - 0.3) < 0.03
