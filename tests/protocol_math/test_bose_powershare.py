"""The arithmetic bose_powershare does on the wire: hex levels in 0.5 dB steps
with the amplifier's 0 dB ceiling, signal-level meters, the Matrix Mixer's
cross-point index and the delay in samples, checked against the worked
examples and tables in Bose Professional's ControlSpace Serial Control
Protocol v5.15.
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("audio/bose_powershare.py")


def test_levels_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15: the §4.2 SG and §5.1 SV / GV examples and Appendix B's Volume Reference."""
    assert DRV.hex_level_to_db(0x78) == 0.0          # SG 2,78 and GV 2,1,78: 0 dB
    assert DRV.hex_level_to_db(0x50) == -20.0        # SV 1,3,50: -20 dB
    assert DRV.hex_level_to_db(0x7A) == 1.0          # Appendix B: 1 dB = 7A
    assert DRV.hex_level_to_db(0xFF) == -60.5        # FFh: -60.5 dB / off
    # §4.2: "PowerMatch and PowerShare Amplifier outputs ... have a maximum
    # level of 0dB (78h)", so a level above 0 dB goes out as 78h.
    assert DRV.db_to_hex_level(0) == "78"
    assert DRV.db_to_hex_level(6) == "78"
    assert DRV.db_to_hex_level(-20) == "50"
    assert DRV.db_to_hex_level(-60.5) == "ff"


def test_meters_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15, §5.4 Get Signal Level examples."""
    # GL 1 [78,1,40,64]: 0.0, -59.5, -28.0, -10.0 dBFS
    assert [DRV.meter_to_db(v) for v in (0x78, 0x1, 0x40, 0x64)] == [0.0, -59.5, -28.0, -10.0]
    # GL 2 [...,64,64]: the amplifier's output -10.0 dBV max
    assert DRV.meter_to_db(0x64) == -10.0


def test_crosspoint_index_follows_the_document():
    """ControlSpace Serial Control Protocol v5.15, §6.2.4 Matrix Mixer: Index 2 = (Input # - 1) x 8 + Output #, and its examples."""
    assert DRV.crosspoint_index(4, 2) == 26          # "cross-point 26 (Input D, Output2)"
    assert DRV.crosspoint_index(1, 4) == 4           # "cross-point 4 (Input A, Output 4)"
    assert DRV.crosspoint_index(1, 1) == 1 and DRV.crosspoint_index(1, 2) == 2
    assert DRV.crosspoint_of(26) == (4, 2)
    assert DRV.crosspoint_of(9) == (2, 1)
    assert DRV.crosspoint_of(5) is None              # outputs 5-8 do not exist on a PowerShare


def test_delay_samples_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15, §6.2.10 Delay: 48,000 samples a second and the 50 ms PowerShare ceiling."""
    assert DRV.ms_to_samples(54) == 2592             # "54msec ... 0.054x48000 = 2592 samples"
    assert DRV.samples_to_ms(9600) == 200.0          # "9600 samples, 9600/48000 = 0.2sec = 200msec"
    assert DRV.ms_to_samples(50) == DRV.DELAY_MAX_SAMPLES == 2400
