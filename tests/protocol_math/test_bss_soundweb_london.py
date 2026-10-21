"""The Direct Inject framing and value laws bss_soundweb_london puts on the
wire (checksum, byte escaping, the percent word, string state variables and
the gain fader law), checked against BSS's "Soundweb London Interface Kit"
rev 2.7 (2013) and the gain range printed in BSS's "Soundweb London Third
Party Control" guide (2016), with the gain words a BLU-100 on firmware
86.4.2 sent as confirmation.
"""

from __future__ import annotations

import pytest
from _platform_probe import load_driver

DRV = load_driver("audio/bss_soundweb_london.py")


def test_toolbar_example_from_the_interface_kit():
    """Soundweb London Interface Kit rev 2.7: the London Architect serial toolbar strings on p.20 and p.11."""
    # Interface Kit p.20: node 0x08AD, VD 3, object 0x11, SV 0, data 0 -> 0x3F.
    assert DRV.build_set(0x08AD, 3, 0x11, 0, 0).hex(" ") == \
        "02 88 08 ad 1b 83 00 00 11 00 00 00 00 00 00 3f 03"
    # p.11: node 0x010F, VD 3, object 0x100, SV 0 -> checksum 0x84.
    assert DRV.build_set(0x010F, 3, 0x100, 0, 0).hex(" ") == \
        "02 88 01 0f 1b 83 00 01 00 00 00 00 00 00 00 84 03"


def test_percent_word_and_reserved_checksum_are_escaped():
    """Soundweb London Interface Kit rev 2.7: Appendix A percentage table (p.24), the special-byte substitutions (p.6) and the checksum note (p.7)."""
    # Appendix A: 12.5 % = 819200 = 00 0C 80 00. The checksum of this body is
    # 0x03 (ETX) and must travel as 1B 83 (p.7). The full frame, checksum
    # byte included, is computed from those two rules.
    frame = DRV.build_set_percent(0, 3, 0x100, 0, 12.5)
    assert frame.hex(" ") == "02 8d 00 00 1b 83 00 01 00 00 00 00 0c 80 00 1b 83 03"
    assert DRV.decode_frame(frame) is not None
    assert DRV.parse_body(DRV.decode_frame(frame)).raw == 819200
    bump = DRV.parse_body(DRV.decode_frame(DRV.build_bump_percent(0, 3, 1, 0, -10)))
    assert bump.raw == -655360  # Appendix A: -10 % = FF F6 00 00


def test_string_sv_follows_appendix_f():
    """Soundweb London Interface Kit rev 2.7, Appendix F (p.30): the worked example "Soundweb London" -> 00 10, 15 bytes, 00."""
    frame = DRV.build_set_string(0, 3, 0x100, 0, "Soundweb London")
    body = DRV.decode_frame(frame)
    assert body[9:11] == bytes.fromhex("0010")
    assert body[11:26] == b"Soundweb London" and body[26] == 0
    assert DRV.parse_body(body).text == "Soundweb London"


def test_fader_law_matches_appendix_a():
    """Soundweb London Third Party Control (2016), Parameter Values: gain -280,617 = -80 dB to 100,000 = +10 dB; the Interface Kit rev 2.7 Appendix A gain formula (p.25)."""
    # Third Party Control: 100,000 is +10 dB and unity gain is 0. Appendix A:
    # linear (dB x 10000) from +10 down to -10 dB.
    assert DRV.gain_db_to_raw(10) == 100000
    assert DRV.gain_db_to_raw(0) == 0
    assert DRV.gain_db_to_raw(-10) == -100000
    # Below -10 dB, Appendix A's logarithmic segment. -160204 and -255630 are
    # the words a BLU-100 on firmware 86.4.2 sent when Architect set -20 and
    # -60 dB (the formula gives -160206 for -20 dB; the unit's -160204
    # decodes to -20.0 within 0.001). -280617 is the -80 dB floor the Third
    # Party Control guide prints, and the same unit stops every gain there.
    assert DRV.gain_db_to_raw(-20) == -160206
    assert DRV.raw_to_gain_db(-160204) == pytest.approx(-20.0, abs=0.001)
    assert DRV.gain_db_to_raw(-60) == -255630
    assert DRV.raw_to_gain_db(-280617) == pytest.approx(-80.0, abs=0.001)
