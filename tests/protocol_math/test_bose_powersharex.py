"""The arithmetic bose_powersharex does on the wire: hex levels in 0.5 dB steps
with the amplifier's 0 dB ceiling, signal-level meters, the PowerShareX Matrix
Mixer's cross-point index, the delay and align delay in samples, the parameter
set and group ranges, and the Input Backup Strategy's source numbers, checked
against the worked examples and tables in Bose Professional's ControlSpace
Serial Control Protocol v5.15.
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("audio/bose_powersharex.py")


def test_levels_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15: the §4.2 SG and §5.1 SV / GV examples."""
    assert DRV.hex_level_to_db(0x78) == 0.0          # SG 2,78 and GV 2,1,78: 0 dB
    assert DRV.hex_level_to_db(0x50) == -20.0        # SV 1,3,50: -20 dB
    assert DRV.hex_level_to_db(0xFF) == -60.5        # FFh: -60.5 dB / off
    # §4.2 / §5.1: "or PM, PS, and PSX, 0h (-60dB) to 78h (0dB)", so a level
    # above 0 dB goes out as 78h.
    assert DRV.db_to_hex_level(0) == "78"
    assert DRV.db_to_hex_level(6) == "78"
    assert DRV.db_to_hex_level(-20) == "50"
    assert DRV.db_to_hex_level(-60.5) == "ff"


def test_meters_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15, §5.4 Get Signal Level examples."""
    # GL 1 [78,1,40,64]: 0.0, -59.5, -28.0, -10.0
    assert [DRV.meter_to_db(v) for v in (0x78, 0x1, 0x40, 0x64)] == [0.0, -59.5, -28.0, -10.0]
    # §5.4: "or PSX, 1: Analog Input, 2: Output, 3: Dante Input"
    assert (DRV.GL_ANALOG, DRV.GL_OUTPUTS, DRV.GL_DANTE) == ("1", "2", "3")


def test_crosspoint_index_follows_the_document():
    """ControlSpace Serial Control Protocol v5.15, §6.2.4 Matrix Mixer: "or PSX Index 2 = (Input # -1) x 4 + Output #", Cross Point(4,4) = 16."""
    assert DRV.crosspoint_index(1, 1) == 1 and DRV.crosspoint_index(1, 2) == 2
    assert DRV.crosspoint_index(4, 4) == 16          # "16 Cross Point(4,4)"
    assert DRV.crosspoint_index(2, 1) == 5
    assert DRV.crosspoint_of(16) == (4, 4)
    assert DRV.crosspoint_of(5) == (2, 1)
    assert DRV.crosspoint_of(17) is None             # a 4 x 4 matrix ends at 16
    assert sorted(DRV.crosspoint_index(i, o) for i in range(1, 5) for o in range(1, 5)) == list(range(1, 17))


def test_delay_samples_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15, §6.2.10 Delay: 48,000 samples a second, PowerShareX delay 0-96000 samples and align delay 0-4800."""
    assert DRV.ms_to_samples(54) == 2592             # "54msec ... 0.054x48000 = 2592 samples"
    assert DRV.samples_to_ms(9600) == 200.0          # "9600 samples, 9600/48000 = 0.2sec = 200msec"
    assert DRV.ms_to_samples(2000) == DRV.DELAY_MAX_SAMPLES == 96000
    assert DRV.ms_to_samples(100) == DRV.ALIGN_MAX_SAMPLES == 4800


def test_ranges_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15: §4.1 "or PSX, 1 - 40h (1-64 decimal)", §4.2-4.4 "or PSX, 1 - 3Fh (1-63 decimal)"."""
    assert DRV.PARAMETER_SET_MAX == 0x40 == 64
    assert DRV.GROUP_MAX == 0x3F == 63
    assert format(DRV.PARAMETER_SET_MAX, "x") == "40"


def test_backup_sources_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15, §6.2.3 Input Backup Strategy: "0=None, 1-4=Analog 1-4, 5-8=DDigital 1-4", and "SA"Backup Strategy 1">3>2=3" sets Analog 3."""
    assert DRV.source_wire("analog_3") == "3"
    assert DRV.source_wire("none") == "0"
    assert DRV.source_wire("dante_1") == "5"
    assert DRV.source_wire("dante_4") == "8"
    assert DRV.source_name("3") == "analog_3"
    assert DRV.source_name("8") == "dante_4"
    assert DRV.source_name("9") is None
