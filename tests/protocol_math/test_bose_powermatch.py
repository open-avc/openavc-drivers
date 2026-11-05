"""The arithmetic bose_powermatch does on the wire: hex levels in 0.5 dB steps
with the amplifier's 0 dB ceiling, signal-level meters, the slot and channel
each of eight inputs and outputs sits on, the Matrix Mixer's cross-point
index with the signal generator as input 9, the delay and align delay in
samples, the output configuration, the array EQ's M.VVV value and the
parameter set and group ranges, checked against the worked examples and
tables in Bose Professional's ControlSpace Serial Control Protocol v5.15.
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("audio/bose_powermatch.py")


def test_levels_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15: the §4.2 SG and §5.1 SV / GV examples."""
    assert DRV.hex_level_to_db(0x78) == 0.0          # GV 2,1,78: 0 dB
    assert DRV.hex_level_to_db(0x50) == -20.0        # SV 1,3,50: -20 dB
    assert DRV.hex_level_to_db(0xFF) == -60.5        # FFh: -60.5 dB / off
    # §4.2 / §5.1: "or PM, PS, and PSX 0h(-60dB) to 78h(0dB)".
    assert DRV.db_to_hex_level(0) == "78"
    assert DRV.db_to_hex_level(6) == "78"
    assert DRV.db_to_hex_level(-20) == "50"
    assert DRV.db_to_hex_level(-60.5) == "ff"


def test_meters_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15, §5.4 Get Signal Level: "GL 2 [0,0,0,0,0,0,64,64]" is -10.0 dBV max on channels 7 and 8."""
    assert [DRV.meter_to_db(v) for v in (0x78, 0x1, 0x40, 0x64)] == [0.0, -59.5, -28.0, -10.0]
    # The GL indices: a PM8xxxN reads In 1-8 on 1 and Out 1-8 on 2.
    assert (DRV.GL_INPUTS, DRV.GL_OUTPUTS) == ("1", "2")


def test_slots_follow_table_2():
    """ControlSpace Serial Control Protocol v5.15, §5 Table 2: PM8xxxN slot 1 In A-D, 2 Out 1-4, 3 In E-H, 4 Out 5-8; channels 1-4 on each."""
    assert DRV.input_slot(1) == ("1", 1) and DRV.input_slot(4) == ("1", 4)
    assert DRV.input_slot(5) == ("3", 1) and DRV.input_slot(8) == ("3", 4)
    assert DRV.output_slot(1) == ("2", 1) and DRV.output_slot(4) == ("2", 4)
    assert DRV.output_slot(5) == ("4", 1) and DRV.output_slot(8) == ("4", 4)
    assert DRV.channel_of_slot("3", 2) == ("input", 6)
    assert DRV.channel_of_slot("4", 4) == ("output", 8)
    assert DRV.channel_of_slot("2", 5) is None       # channels run 1-4 per slot
    assert [DRV.input_letter(n) for n in (1, 4, 5, 8)] == ["A", "D", "E", "H"]
    assert DRV.peq_label(6) == "PEQ-5band F"         # GA"PEQ-5band F">3>6: band 3 of channel F


def test_crosspoint_index_follows_the_document():
    """ControlSpace Serial Control Protocol v5.15, §6.2.4: "Index 2 = (Input # -1) x 8 + Output #", cross-point 26 is (Input D, Output 2) and 65 is (SigGen, Output 1)."""
    assert DRV.crosspoint_index(4, 2) == 26
    assert DRV.crosspoint_index(DRV.SIGGEN_INPUT, 1) == 65
    assert DRV.crosspoint_index(1, 4) == 4           # "cross-point 4 (Input A, Output 4)"
    assert DRV.crosspoint_index(9, 8) == 72          # Cross Point(9,8), the last
    assert DRV.crosspoint_of(26) == (4, 2)
    assert DRV.crosspoint_of(65) == (9, 1)
    assert DRV.crosspoint_of(73) is None
    assert sorted(DRV.crosspoint_index(i, o) for i in range(1, 10) for o in range(1, 9)) == list(range(1, 73))


def test_configuration_follows_the_document():
    """ControlSpace Serial Control Protocol v5.15, §5.9 Get Configuration: "GC IN,IN,BL,BL,Q7,Q7,Q7,Q7" is channels 1 and 2 independent, 3 and 4 bridged Lo-Z, 5-8 quad 70 V."""
    modes = DRV.parse_configuration("GC IN,IN,BL,BL,Q7,Q7,Q7,Q7")
    assert modes == ["independent", "independent", "bridged_lo_z", "bridged_lo_z",
                     "quad_70v", "quad_70v", "quad_70v", "quad_70v"]
    assert len(DRV.parse_configuration("GC IN,IN,IN,IN")) == 4    # a PM4xxxN
    assert DRV.parse_configuration("GY N") is None


def test_delay_samples_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15, §6.2.10 Delay: 48,000 samples a second, PowerMatch delay 0-144000 samples (3 s) and align delay 0-480."""
    assert DRV.ms_to_samples(54) == 2592             # "54msec ... 0.054x48000 = 2592 samples"
    assert DRV.samples_to_ms(9600) == 200.0          # "9600 samples, 9600/48000 = 0.2sec = 200msec"
    assert DRV.ms_to_samples(3000) == DRV.DELAY_MAX_SAMPLES == 144000
    assert DRV.ms_to_samples(10) == DRV.ALIGN_MAX_SAMPLES == 480


def test_array_value_follows_the_document():
    """ControlSpace Serial Control Protocol v5.15, §6.2.6 Array EQ: "M.VVV, M=2 to 8, VVV=20 to 100, 5 step", and "SA"Array EQ A">6>4=4.080" is four modules at 80 degrees."""
    assert DRV.encode_array("4.080") == "4.080"
    assert DRV.encode_array("4.80") == "4.080"
    assert DRV.encode_array("8.100") == "8.100"
    assert DRV.ARRAY_EQ_BAND == "6"
    for bad in ("1.080", "9.080", "4.085x", "4.015", "4.105", "4.082"):
        try:
            DRV.encode_array(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} was accepted")


def test_ranges_follow_the_document():
    """ControlSpace Serial Control Protocol v5.15: §4.1 "1 - FFh (1-255 decimal)", §4.2-4.4 "1 - 40h (1-64 decimal)", §6.2.9 PowerMatch limiter 71 V peak and 50 V RMS mono, 142 V bridged."""
    assert DRV.PARAMETER_SET_MAX == 0xFF == 255
    assert DRV.GROUP_MAX == 0x40 == 64
    assert (DRV.MONO_PEAK_MAX_V, DRV.MONO_RMS_MAX_V, DRV.BRIDGED_MAX_V) == (71.0, 50.0, 142.0)
