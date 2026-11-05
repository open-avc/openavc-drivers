"""Protocol arithmetic of allenheath_cq, checked against Allen & Heath's CQ MIDI
Protocol, Firmware V1.2, Issue 5: the worked examples of sections 2.1 to 2.6
and the reference tables of section 3. Every expected value below is printed
in that document; none was read off a CQ.
"""

from __future__ import annotations

from _platform_probe import load_driver

cq = load_driver("audio/allenheath_cq.py")


def _hex(data: bytes) -> str:
    return data.hex(" ").upper()


def test_messages_match_the_worked_examples():
    """§2.3 Mutes, §2.4 Levels and §2.6 Getting values: the example messages."""
    ip1 = cq._src_input(1)
    cases = [
        # §2.3: "Ip1, Mute On", "Main LR, Mute Off", "Ip3, Mute Toggle".
        (cq.nrpn_set(*cq.mute_addr(ip1), 0x00, 0x01), "B0 63 00 B0 62 00 B0 06 00 B0 26 01"),
        (cq.nrpn_set(*cq.mute_addr(cq.SRC_LR), 0x00, 0x00), "B0 63 00 B0 62 44 B0 06 00 B0 26 00"),
        (cq.nrpn_increment(*cq.mute_addr(cq._src_input(3))), "B0 63 00 B0 62 02 B0 60 00"),
        # §2.4 Relative Control: "Ip1 to LR, Increment / Decrement".
        (cq.nrpn_increment(*cq.to_lr(ip1)), "B0 63 40 B0 62 00 B0 60 00"),
        (cq.nrpn_decrement(*cq.to_lr(ip1)), "B0 63 40 B0 62 00 B0 61 00"),
        # §2.6: "Ip1, Mute" and "Ip1 to LR, Level".
        (cq.nrpn_get(*cq.mute_addr(ip1)), "B0 63 00 B0 62 00 B0 60 7F"),
        (cq.nrpn_get(*cq.to_lr(ip1)), "B0 63 40 B0 62 00 B0 60 7F"),
    ]
    for got, want in cases:
        assert _hex(got) == want


def test_addresses_match_the_worked_examples_and_tables():
    """§2.4 / §2.5 / §2.6 examples and the §3 parameter-number tables."""
    st1, st2 = cq.SRC_STEREO["st1"], cq.SRC_STEREO["st2"]
    usb, bt = cq.SRC_STEREO["usb"], cq.SRC_STEREO["bt"]
    fx = cq._src_fx_return
    cases = [
        # §2.4 Absolute Control examples.
        ("Ip12 to Out2",       cq.to_output(cq._src_input(12), 2),            (0x41, 0x49)),
        ("Out 5/6 (overall)",  cq.master_output(5),                          (0x4F, 0x05)),
        ("FX2 to Out 1",       cq.to_output(fx(2), 1),                        (0x46, 0x20)),
        ("FX1 to FX2",         cq.to_fx(fx(1), 2),                            (0x4E, 0x05)),
        ("DCA1",               cq.master_dca(1),                              (0x4F, 0x20)),
        # §2.4 Relative Control: "FX3 to Out 3/4".
        ("FX3 to Out 3/4",     cq.to_output(fx(3), 3),                        (0x46, 0x2E)),
        # §2.5 Panning/Balance examples.
        ("Ip1 to LR pan",      cq.to_lr(cq._src_input(1), cq.PLANE_PAN),      (0x50, 0x00)),
        ("Ip3 to Out 5/6 pan", cq.to_output(cq._src_input(3), 5, cq.PLANE_PAN), (0x50, 0x60)),
        ("FX1 to Out 1/2 pan", cq.to_output(fx(1), 1, cq.PLANE_PAN),          (0x56, 0x14)),
        ("Ip10 to Out 1/2",    cq.to_output(cq._src_input(10), 1, cq.PLANE_PAN), (0x51, 0x30)),
        ("BT to Out 5/6",      cq.to_output(bt, 5, cq.PLANE_PAN),            (0x53, 0x30)),
        # §2.6 Getting values: "ST1 to Out 1/2, Pan", "FX2 Overall Input Level".
        ("ST1 to Out 1/2 pan", cq.to_output(st1, 1, cq.PLANE_PAN),           (0x52, 0x64)),
        ("FX2 input level",    cq.master_fx(2),                               (0x4F, 0x0E)),
        # §3 Mute Parameter Numbers.
        ("Ip16 mute",          cq.mute_addr(cq._src_input(16)),              (0x00, 0x0F)),
        ("St1 mute",           cq.mute_addr(st1),                             (0x00, 0x18)),
        ("St2 mute",           cq.mute_addr(st2),                             (0x00, 0x1A)),
        ("USB mute",           cq.mute_addr(usb),                             (0x00, 0x1C)),
        ("BT mute",            cq.mute_addr(bt),                              (0x00, 0x1E)),
        ("Out1 mute",          cq.mute_addr(cq._src_output(1)),              (0x00, 0x45)),
        ("Out6 mute",          cq.mute_addr(cq._src_output(6)),              (0x00, 0x4A)),
        ("FX1 mute",           cq.mute_addr(cq._src_fx_mute(1)),             (0x00, 0x51)),
        ("FX4 mute",           cq.mute_addr(cq._src_fx_mute(4)),             (0x00, 0x54)),
        ("MGRP4 mute",         cq.mute_mgrp(4),                               (0x04, 0x03)),
        ("DCA4 mute",          cq.mute_dca(4),                                (0x02, 0x03)),
        # §3 Level Parameter Numbers: the LSB rollovers and the corners.
        ("Ip6 to Out1",        cq.to_output(cq._src_input(6), 1),            (0x41, 0x00)),
        ("Ip16 to Out6",       cq.to_output(cq._src_input(16), 6),           (0x41, 0x7D)),
        ("ST1 to Out1",        cq.to_output(st1, 1),                          (0x42, 0x64)),
        ("ST2 to Out5",        cq.to_output(st2, 5),                          (0x43, 0x00)),
        ("BT to Out6",         cq.to_output(bt, 6),                           (0x43, 0x31)),
        ("FX4 to LR",          cq.to_lr(fx(4)),                               (0x40, 0x3F)),
        ("FX4 to Out6",        cq.to_output(fx(4), 6),                        (0x46, 0x3D)),
        ("Ip1 to FX1",         cq.to_fx(cq._src_input(1), 1),                (0x4C, 0x14)),
        ("Ip16 to FX4",        cq.to_fx(cq._src_input(16), 4),               (0x4C, 0x53)),
        ("ST2 to FX4",         cq.to_fx(st2, 4),                              (0x4C, 0x7F)),
        ("USB to FX1",         cq.to_fx(usb, 1),                              (0x4D, 0x04)),
        ("BT to FX4",          cq.to_fx(bt, 4),                               (0x4D, 0x0F)),
        ("FX2 to FX1",         cq.to_fx(fx(2), 1),                            (0x4E, 0x08)),
        ("FX4 to FX3",         cq.to_fx(fx(4), 3),                            (0x4E, 0x12)),
        ("Main LR master",     cq.master_lr(),                                (0x4F, 0x00)),
        ("Out6 master",        cq.master_output(6),                          (0x4F, 0x06)),
        ("FX4 unit input",     cq.master_fx(4),                               (0x4F, 0x10)),
        ("DCA4 master",        cq.master_dca(4),                              (0x4F, 0x23)),
        # §3 Pan/Balance Parameter Numbers. The table prints ST2 to Out5/6 as
        # "53 0"; 53 00 is the only reading that fits its row and column.
        ("ST2 to Out5/6 pan",  cq.to_output(st2, 5, cq.PLANE_PAN),           (0x53, 0x00)),
        ("USB to Out3/4 pan",  cq.to_output(usb, 3, cq.PLANE_PAN),           (0x53, 0x16)),
        ("FX4 to LR pan",      cq.to_lr(fx(4), cq.PLANE_PAN),                 (0x50, 0x3F)),
        ("FX4 to Out5/6 pan",  cq.to_output(fx(4), 5, cq.PLANE_PAN),         (0x56, 0x3C)),
    ]
    for label, got, want in cases:
        assert got == want, f"{label}: {got[0]:02X} {got[1]:02X} != {want[0]:02X} {want[1]:02X}"


def test_levels_hit_the_published_values():
    """§2.4 Absolute Control examples and §3 Example Level Values (VC/VF)."""
    for db, vc, vf in [(0, 0x62, 0x00), (-20, 0x2E, 0x40), (-5, 0x4E, 0x40),
                       (5, 0x73, 0x40), (-10, 0x3E, 0x00), (-15, 0x36, 0x00),
                       (-40, 0x0F, 0x40), (-89, 0x01, 0x40), (10, 0x7F, 0x40)]:
        assert cq.db_to_raw(db) == (vc << 7) | vf, f"{db} dB"
        assert cq.raw_to_db((vc << 7) | vf) == db, f"{vc:02X} {vf:02X}"
    # -inf is 00 00.
    assert cq.db_to_raw(cq.DB_MIN) == 0
    assert cq.raw_to_db(0) == cq.DB_MIN


def test_pan_hits_the_documented_anchors():
    """§2.5: "Ranging from 00 00 (full left) to 7F 7F (full right), with centre
    being 40 00"; and the §2.5 / §3 percentage examples.

    The three anchors are exact. The percentage examples sit within one count
    of a straight line either side of centre and are not one formula
    (L30% is 2C 65 = 5733 where the line gives 5734, R10% 46 32 = 9010 where
    it gives 9011), so they are checked to one count.
    """
    assert cq.pan_to_vcvf(-1.0) == (0x00, 0x00)
    assert cq.pan_to_vcvf(0.0) == (0x40, 0x00)
    assert cq.pan_to_vcvf(1.0) == (0x7F, 0x7F)
    assert cq.vcvf_to_pan(0x40, 0x00) == 0.0
    assert cq.vcvf_to_pan(0x00, 0x00) == -1.0
    assert cq.vcvf_to_pan(0x7F, 0x7F) == 1.0
    for pan, vc, vf in [(-0.3, 0x2C, 0x65), (0.1, 0x46, 0x32), (-0.5, 0x1F, 0x7F),
                        (0.5, 0x5F, 0x7F), (-0.9, 0x06, 0x33), (0.9, 0x79, 0x4B)]:
        got = cq.vcvf_to_raw(*cq.pan_to_vcvf(pan))
        assert abs(got - ((vc << 7) | vf)) <= 1, f"pan {pan}: {got}"


def test_scene_and_softkey_encoding():
    """§2.1 Scene change examples and §2.2 / §3 Soft Key notes."""
    assert _hex(cq.scene_message(1)) == "B0 00 00 C0 00"
    assert _hex(cq.scene_message(7)) == "B0 00 00 C0 06"
    assert _hex(cq.scene_message(64)) == "B0 00 00 C0 3F"
    assert [cq.softkey_note(n) for n in (1, 2, 3)] == [0x30, 0x31, 0x32]
