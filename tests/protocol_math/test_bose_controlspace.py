"""The level and routing arithmetic bose_controlspace does on the wire: hex
levels in 0.5 dB steps, signal-level meters, and the Standard Mixer's
eight-digit routing mask, checked against the worked examples in Bose
Professional's ControlSpace Serial Control Protocol v5.13.
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("audio/bose_controlspace.py")


def test_levels_and_meters_follow_the_document():
    """ControlSpace Serial Control Protocol v5.13: the §4.2 SG/GG, §5.1 SV and §5.4 GL examples."""
    assert DRV.hex_level_to_db(0x78) == 0.0          # SG 2,78: 0 dB
    # The document's "GG 2,80 = 1 dB" example contradicts its own formula
    # (0h = -60 dB in 0.5 dB steps), which its other two examples follow;
    # the formula wins: 80h is +4 dB.
    assert DRV.hex_level_to_db(0x80) == 4.0
    assert DRV.hex_level_to_db(0x50) == -20.0        # SV 1,3,50: -20 dB
    # §4.2's stated range ends: 90h = +12 dB, FFh = -60.5 dB.
    assert DRV.hex_level_to_db(0x90) == 12.0
    assert DRV.hex_level_to_db(0xFF) == -60.5
    assert DRV.db_to_hex_level(0) == "78"
    assert DRV.db_to_hex_level(-20) == "50" and DRV.db_to_hex_level(-60.5) == "ff"
    # GL 1 [78,1,40,64]: 0.0, -59.5, -28.0, -10.0 dBFS
    assert [DRV.meter_to_db(v) for v in (0x78, 0x1, 0x40, 0x64)] == [0.0, -59.5, -28.0, -10.0]
    assert DRV.meter_to_db(0x28) == -40.0            # AEC ERLE example


def test_routing_mask_follows_the_worked_example():
    """ControlSpace Serial Control Protocol v5.13, §6.1.40 Standard Mixer Module: the Cross-point Routing A example '84924F3A'."""
    on = {1, 6, 9, 12, 15, 18, 21, 22, 23, 24, 27, 28, 29, 31}
    outputs = DRV.routing_mask_to_outputs("84924F3A", 32)
    assert {o for o, v in outputs.items() if v} == on
    assert DRV.outputs_to_routing_mask({o: o in on for o in range(1, 33)}) == "84924F3A"
