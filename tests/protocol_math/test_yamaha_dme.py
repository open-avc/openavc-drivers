"""Conversions worked from Yamaha DME RCP V3.1.0, pages 8, 18, 28 and 50."""

from _platform_probe import load_driver

DRV = load_driver('audio/yamaha_dme.py')


def test_meter_table_and_clip_bit():
    """Page 50: 00 <= -126, 71 = -13, 7E = 0, 7F = OVER; MSB is clip."""
    assert DRV.meter_value('00') == (-126, False)
    assert DRV.meter_value('71') == (-13, False)
    assert DRV.meter_value('F1') == (-13, True)
    assert DRV.meter_value('7E') == (0, False)
    assert DRV.meter_value('7F') == (None, False)
    assert DRV.meter_value('FF') == (None, True)


def test_raw_gain_and_infinity():
    """Page 18's -7760 is -77.60 dB; page 8 reserves -13801 for infinity."""
    parameter = DRV.Parameter(1, 'Fader/Level', 1, 1, -13801, 1000,
                              'dB', 'integer', 'fader', True, 100)
    assert parameter.decode('-7760') == -77.60
    assert parameter.encode(-77.60) == '-7760'
    assert parameter.decode('-13801') is None
