"""Protocol arithmetic of allenheath_sq, checked against Allen & Heath's SQ
MIDI Protocol Issue 5 (SQ firmware V1.5.0 or later): the worked examples in
section 3 and the parameter-number and value tables in section 4.
"""

from __future__ import annotations

from _platform_probe import load_driver

sq = load_driver("audio/allenheath_sq.py")


def test_level_addresses_match_spec_examples():
    """SQ MIDI Protocol Issue 5 §3.4 Levels examples and the §4 Level Parameter Numbers, Master Sends table."""
    # §3.4: Ip1→LR = 40 00, Ip40→LR = 40 27, Ip40→Aux5 = 44 1C,
    # Grp4→Aux8 = 45 2F, Ip36→FX3 = 4D 22. §4: LR = 4F 00, DCA8 = 4F 27.
    assert sq.level_input_to_lr(1) == (0x40, 0x00)
    assert sq.level_input_to_lr(40) == (0x40, 0x27)
    assert sq.level_input_to_aux(40, 5) == (0x44, 0x1C)
    assert sq.level_group_to_aux(4, 8) == (0x45, 0x2F)
    assert sq.level_input_to_fx_send(36, 3) == (0x4D, 0x22)
    assert sq.level_lr_master() == (0x4F, 0x00)
    assert sq.level_dca(8) == (0x4F, 0x27)


def test_pan_addresses_match_spec_examples():
    """SQ MIDI Protocol Issue 5 §3.7 Getting values examples and the §4 Balance Parameter Numbers, Master Sends table."""
    # §3.7: Ip30→Aux5 pan = 53 24, Aux7→Mtx1 balance = 5E 39.
    # §4: LR = 5F 00, Aux1-12 = 5F 01-0C, Mtx1-3 = 5F 11-13.
    assert sq.pan_input_to_aux(30, 5) == (0x53, 0x24)
    assert sq.pan_aux_to_mtx(7, 1) == (0x5E, 0x39)
    assert sq.pan_lr_master() == (0x5F, 0x00)
    assert sq.pan_aux_master(12) == (0x5F, 0x0C)
    assert sq.pan_mtx_master(3) == (0x5F, 0x13)


def test_assign_addresses_roll_lsb_into_next_msb():
    """SQ MIDI Protocol Issue 5 §4, Assignment Parameter Numbers, Inputs to LR/Aux."""
    # Ip1→LR = 60 00, Ip1→Aux1 = 60 44, Ip5→Aux12 = 60 7F, Ip6→Aux1 = 61 00
    # (the LSB rollover).
    assert sq.assign_input_to_lr(1) == (0x60, 0x00)
    assert sq.assign_input_to_aux(1, 1) == (0x60, 0x44)
    assert sq.assign_input_to_aux(5, 12) == (0x60, 0x7F)
    assert sq.assign_input_to_aux(6, 1) == (0x61, 0x00)


def test_value_codecs():
    """SQ MIDI Protocol Issue 5: §4 level and pan value tables, §3.1 Scene change, §3.2 Soft Keys."""
    # §4 Example Linear Taper Level Values: -inf = 00 00, +10 dB = 7F 7F
    # (the bottom and top of the fader).
    assert sq.level_to_vcvf(0.0) == (0x00, 0x00)
    assert sq.level_to_vcvf(1.0) == (0x7F, 0x7F)
    # §4 Example Pan/Balance Values: L100% = 00 00, CTR = 3F 7F, R100% = 7F 7F.
    assert sq.pan_to_vcvf(-1.0) == (0x00, 0x00)
    assert sq.pan_to_vcvf(0.0) == (0x3F, 0x7F)
    assert sq.pan_to_vcvf(1.0) == (0x7F, 0x7F)
    assert abs(sq.vcvf_to_pan(0x3F, 0x7F)) < 0.001
    # §3.1: scene 96 = bank 00 program 5F; 156 = 01 1B; scenes 257-300 are
    # bank 02, so 300 = 02 2B, and nothing outside 1-300 is a scene.
    assert sq.scene_to_bank_program(96) == (0, 0x5F)
    assert sq.scene_to_bank_program(156) == (1, 0x1B)
    assert sq.scene_to_bank_program(300) == (2, 43)
    for bad in (0, 301):
        try:
            sq.scene_to_bank_program(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    # §3.2: SoftKey 1 = C3 (30) ... SoftKey 16 = D#4 (3F).
    assert sq.softkey_to_note(1) == 0x30
    assert sq.softkey_to_note(16) == 0x3F
