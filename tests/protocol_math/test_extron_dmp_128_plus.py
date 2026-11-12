"""Gain encoding from the Extron DMP 128 Plus User Guide, Rev. N, page 47."""

from _platform_probe import load_driver

DRV = load_driver("audio/extron_dmp_128_plus.py")


def test_signed_tenths_from_the_audio_level_table():
    assert DRV.tenths(12.0) == 120
    assert DRV.tenths(-0.8) == -8
    assert DRV.tenths(10.4) == 104
    assert DRV.tenths(-3.2) == -32
