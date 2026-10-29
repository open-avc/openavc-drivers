"""Protocol arithmetic of allenheath_sq, checked against Allen & Heath's SQ
MIDI Protocol Issue 5 (SQ firmware V1.5.0 or later): the worked examples in
section 3 and the parameter-number and value tables in section 4; the SQ MIDI
Protocol for firmware V1.6 (Matrices 4-6, Cue List); and the SQ+ MIDI
Protocol for firmware V2.0.3 (its own master and matrix-send tables, and
RackUltra FX).
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


def test_sq_v1_6_matrices_4_to_6_and_fx_send_balance():
    """SQ MIDI Protocol Firmware V1.6, Mute Parameter Numbers (Mtx1-6 = 00 55-5A) and Mix Sends Output / Balance tables."""
    assert sq.mute_mtx_master(6) == (0x00, 0x5A)
    assert sq.level_mtx_master(4) == (0x4F, 0x14)
    assert sq.level_mtx_master(6) == (0x4F, 0x16)
    assert sq.pan_fx_send_master(1) == (0x5F, 0x0D)
    assert sq.pan_fx_send_master(4) == (0x5F, 0x10)


def test_sq_plus_master_outputs_moved():
    """SQ+ MIDI Protocol Firmware V2.0.3, Level and Balance Parameter Numbers, Mix Sends: the Output and Control columns."""
    plus = sq.MODEL_SQ_PLUS
    assert sq.level_lr_master(plus) == (0x4F, 0x50)
    assert sq.level_aux_master(1, plus) == (0x4F, 0x51)
    assert sq.level_aux_master(12, plus) == (0x4F, 0x5C)
    assert sq.level_fx_send_master(1, plus) == (0x4F, 0x5D)
    assert sq.level_fx_send_master(4, plus) == (0x4F, 0x60)
    assert sq.level_mtx_master(1, plus) == (0x4F, 0x61)
    assert sq.level_mtx_master(6, plus) == (0x4F, 0x66)
    assert sq.level_dca(1, plus) == (0x4F, 0x67)
    assert sq.level_dca(8, plus) == (0x4F, 0x6E)
    assert sq.pan_lr_master(plus) == (0x5F, 0x50)
    assert sq.pan_aux_master(12, plus) == (0x5F, 0x5C)
    assert sq.pan_mtx_master(1, plus) == (0x5F, 0x61)
    assert sq.pan_mtx_master(3, plus) == (0x5F, 0x63)


def test_sq_plus_matrix_sends_six_wide():
    """SQ+ MIDI Protocol Firmware V2.0.3, Level / Balance Parameter Numbers – Mix Sends and Assignment Parameter Numbers – Matrix Sends."""
    # The tables, not the worked examples carried over from the SQ document
    # (its "Aux5 to Mtx1" 5E 33 is Aux2 to Mtx4 in the SQ+ table).
    plus = sq.MODEL_SQ_PLUS
    assert sq.level_lr_to_mtx(1, plus) == (0x4E, 0x24)
    assert sq.level_lr_to_mtx(6, plus) == (0x4E, 0x29)
    assert sq.level_aux_to_mtx(1, 1, plus) == (0x4E, 0x2A)
    assert sq.level_aux_to_mtx(12, 6, plus) == (0x4E, 0x71)
    assert sq.level_group_to_mtx(1, 1, plus) == (0x4E, 0x72)
    assert sq.level_group_to_mtx(3, 3, plus) == (0x4F, 0x00)   # LSB rollover
    assert sq.level_group_to_mtx(12, 6, plus) == (0x4F, 0x39)
    assert sq.pan_aux_to_mtx(5, 1, plus) == (0x5E, 0x42)
    assert sq.pan_aux_to_mtx(7, 1, plus) == (0x5E, 0x4E)
    assert sq.pan_group_to_mtx(12, 6, plus) == (0x5F, 0x39)


def test_sq_plus_rackultra_fx_addresses():
    """SQ+ MIDI Protocol Firmware V2.0.3, Level / Panning/Balance Parameter Numbers – RackUltra FX (on the unit's MIDI channel)."""
    assert sq.level_ufx_send_master() == (0x4F, 0x50)
    assert sq.pan_ufx_send_master() == (0x5F, 0x50)
    assert sq.level_ufx_return_to_lr() == (0x4F, 0x51)
    assert sq.pan_ufx_return_to_lr() == (0x5F, 0x51)
    assert sq.level_ufx_return_to_aux(1) == (0x40, 0x48)
    assert sq.level_ufx_return_to_aux(12) == (0x40, 0x53)
    assert sq.level_ufx_return_to_group(12) == (0x40, 0x5F)
    assert sq.level_ufx_return_to_fx_send(4) == (0x40, 0x63)
    # A source's send to the unit is its LR send address on that channel.
    assert sq.level_input_to_lr(48) == (0x40, 0x2F)
    assert sq.level_fx_return_to_lr(8) == (0x40, 0x43)


def test_cue_list_recall():
    """SQ V1.6 and SQ+ V2.0.3, Scene change (Cue List): Cue 7 = 0A 06, Cue 120 = 0A 77, Cue 156 = 0B 1B; positions 257-300 are bank 0C."""
    assert sq.cue_to_bank_program(7) == (0x0A, 0x06)
    assert sq.cue_to_bank_program(120) == (0x0A, 0x77)
    assert sq.cue_to_bank_program(156) == (0x0B, 0x1B)
    assert sq.cue_to_bank_program(300) == (0x0C, 43)
    for bad in (0, 301):
        try:
            sq.cue_to_bank_program(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
