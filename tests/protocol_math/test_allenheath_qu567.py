"""Protocol arithmetic of allenheath_qu567, checked against Allen & Heath's
MIDI Protocol Issue 2 for the Qu-5/6/7 (firmware V1.1 or later): the scene
change examples, the soft key notes, and the reference tables for both fader
tapers and for pan. The NRPN address map is the exception: it was read back
off a real Qu-5, not taken from the document.
"""

from __future__ import annotations

from _platform_probe import load_driver

qu = load_driver("audio/allenheath_qu567.py")


def test_address_map_matches_the_console():
    """Read back off a real Qu-5 (2026-09-01), not taken from the manual: an NRPN Get
    sweep, each reply matched by the parameter number it echoed. The console's
    firmware version was not recorded."""
    cases = [
        ("Ip1 mute",       qu.mute_addr(qu._src_input(1)),               (0x00, 0x00)),
        ("Ip32 mute",      qu.mute_addr(qu._src_input(32)),              (0x00, 0x1F)),
        ("ST1 mute",       qu.mute_addr(qu._src_stereo(1)),              (0x00, 0x20)),
        ("ST2 mute",       qu.mute_addr(qu._src_stereo(2)),              (0x00, 0x22)),
        ("USB mute",       qu.mute_addr(qu.SRC_USB),                     (0x00, 0x24)),
        ("FX1Rtn mute",    qu.mute_addr(qu._src_fx_return(1)),           (0x00, 0x3C)),
        ("FX6Rtn mute",    qu.mute_addr(qu._src_fx_return(6)),           (0x00, 0x41)),
        ("LR mute",        qu.mute_addr(qu.SRC_LR),                      (0x00, 0x44)),
        ("MIX1 mute",      qu.mute_addr(qu._src_mix(1)),                 (0x00, 0x45)),
        ("MIX12 mute",     qu.mute_addr(qu._src_mix(12)),                (0x00, 0x50)),
        ("FX1Snd mute",    qu.mute_addr(qu._src_fx_send(1)),             (0x00, 0x51)),
        ("Mtx1 mute",      qu.mute_addr(qu._src_matrix(1)),              (0x00, 0x55)),
        ("Mtx3 mute",      qu.mute_addr(qu._src_matrix(3)),              (0x00, 0x57)),
        ("DCA1 mute",      qu.mute_dca(1),                               (0x02, 0x00)),
        ("DCA8 mute",      qu.mute_dca(8),                               (0x02, 0x07)),
        ("MGRP1 mute",     qu.mute_mgrp(1),                              (0x04, 0x00)),
        ("MGRP8 mute",     qu.mute_mgrp(8),                              (0x04, 0x07)),
        ("Ip1->LR",        qu.to_lr(qu._src_input(1)),                   (0x40, 0x00)),
        ("Ip1->MIX1",      qu.to_mix(qu._src_input(1), 1),               (0x40, 0x44)),
        ("Ip2->MIX1",      qu.to_mix(qu._src_input(2), 1),               (0x40, 0x50)),
        ("ST1->MIX1",      qu.to_mix(qu._src_stereo(1), 1),              (0x43, 0x44)),
        ("ST2->MIX1",      qu.to_mix(qu._src_stereo(2), 1),              (0x43, 0x5C)),
        ("USB->MIX1",      qu.to_mix(qu.SRC_USB, 1),                     (0x43, 0x74)),
        ("Grp1->MIX1",     qu.to_mix(qu._src_group(1), 1),               (0x45, 0x04)),
        ("FX1Rtn->MIX1",   qu.to_mix(qu._src_fx_return(1), 1),           (0x46, 0x14)),
        ("Ip1->FX1Snd",    qu.to_fx_send(qu._src_input(1), 1),           (0x4C, 0x14)),
        ("Ip25->FX1Snd",   qu.to_fx_send(qu._src_input(25), 1),          (0x4C, 0x74)),
        ("ST1->FX1Snd",    qu.to_fx_send(qu._src_stereo(1), 1),          (0x4D, 0x14)),
        ("Grp1->FX1Snd",   qu.to_fx_send(qu._src_group(1), 1),           (0x4D, 0x54)),
        ("FX1Rtn->FX1Snd", qu.to_fx_send(qu._src_fx_return(1), 1),       (0x4E, 0x04)),
        ("LR->Mtx1",       qu.to_matrix(0, 1),                           (0x4E, 0x24)),
        ("MIX1->Mtx1",     qu.to_matrix(1, 1),                           (0x4E, 0x27)),
        ("MIX12->Mtx3",    qu.to_matrix(12, 3),                          (0x4E, 0x4A)),
        ("LR master",      qu.master_lr(),                               (0x4F, 0x00)),
        ("MIX1 master",    qu.master_mix(1),                             (0x4F, 0x01)),
        ("MIX12 master",   qu.master_mix(12),                            (0x4F, 0x0C)),
        ("FX1Snd master",  qu.master_fx_send(1),                         (0x4F, 0x0D)),
        ("Mtx1 master",    qu.master_matrix(1),                          (0x4F, 0x11)),
        ("Mtx3 master",    qu.master_matrix(3),                          (0x4F, 0x13)),
        ("DCA1 master",    qu.master_dca(1),                             (0x4F, 0x20)),
        ("DCA8 master",    qu.master_dca(8),                             (0x4F, 0x27)),
    ]
    for label, got, want in cases:
        assert got == want, f"{label}: {got[0]:02X} {got[1]:02X} != {want[0]:02X} {want[1]:02X}"


def test_taper_tables_hit_the_published_values():
    """Qu-5/6/7 MIDI Protocol Issue 2 §4, Approximate Audio Taper and Example Linear Taper Level Values (VC/VF)."""
    # The Audio Taper 0 dB point was also read off the Qu-5 above: its fader at
    # unity reported 62 00.
    for law, db, vc, vf in [
        ("audio", 0, 0x62, 0x00), ("audio", -20, 0x2E, 0x40),
        ("audio", -10, 0x3E, 0x00), ("audio", 10, 0x7F, 0x40),
        ("linear", 0, 0x76, 0x5C), ("linear", -20, 0x64, 0x16),
        ("linear", -10, 0x6D, 0x39), ("linear", 10, 0x7F, 0x7F),
    ]:
        assert qu.db_to_raw(db, law) == (vc << 7) | vf, f"{law} {db} dB"


def test_pan_hits_the_documented_anchors():
    """Qu-5/6/7 MIDI Protocol Issue 2 §3.6, Absolute Panning Control: 00 00 full left, 3F 7F centre, 7F 7F full right."""
    assert qu.pan_to_vcvf(-1.0) == (0x00, 0x00)
    assert qu.pan_to_vcvf(0.0) == (0x3F, 0x7F)
    assert qu.pan_to_vcvf(1.0) == (0x7F, 0x7F)


def test_scene_and_softkey_encoding():
    """Qu-5/6/7 MIDI Protocol Issue 2 §3.1 Scene change examples and §3.3 Soft Keys note table."""
    # §3.1: scene 7 = B0 00 00 C0 06, scene 120 = B0 00 00 C0 77, scene 156 =
    # B0 00 01 C0 1B, and "scene 264 is bank change 02 (3) and program change 07 (8)".
    assert qu.scene_to_bank_program(7) == (0, 6)
    assert qu.scene_to_bank_program(120) == (0, 119)
    assert qu.scene_to_bank_program(156) == (1, 27)
    assert qu.scene_to_bank_program(264) == (2, 7)
    # §3.3: Soft Key 1 = C3 (30) ... Soft Key 16 = D#4 (3F).
    assert qu.softkey_to_note(1) == 0x30
    assert qu.softkey_to_note(16) == 0x3F
