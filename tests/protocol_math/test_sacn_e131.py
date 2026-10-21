"""Protocol math for the sACN / E1.31 driver.

Checks the E1.31 Data Packet the driver builds (the three flags-and-length
fields, vectors and DMP header) and the universe-to-multicast-group mapping
against ANSI E1.31-2018 (ESTA): Table 4-1, Section 9.3.1 / Table 9-1, and
the worked example in Appendix B.
"""

from __future__ import annotations

import pytest

from _platform_probe import load_driver

sacn = load_driver("lighting/sacn_e131.py")

build_packet = sacn.build_e131_data_packet
universe_to_multicast = sacn.universe_to_multicast
_derive_cid = sacn._derive_cid


def test_full_packet_byte_layout():
    """ANSI E1.31-2018 Table 4-1; the fixed fields match the Appendix B.1 example packet (Table B-13)."""
    # Sequence 7, priority 100, universe 1 and the CID are this test's own
    # inputs; the flags/length words 0x726e / 0x7258 / 0x720b, 0xa1 and the
    # 513 (0x0201) property count are the example's, for a full universe.
    cid = _derive_cid("dev-a")
    dmx = bytes(range(256)) * 2  # 512 bytes
    pkt = build_packet(
        cid=cid, source_name="OpenAVC", priority=100, sequence=7,
        universe=1, dmx_data=dmx,
    )
    assert len(pkt) == 638
    # Root layer
    assert pkt[0:2] == b"\x00\x10"                # preamble size
    assert pkt[2:4] == b"\x00\x00"                # post-amble size
    assert pkt[4:16] == b"ASC-E1.17\x00\x00\x00"  # ACN packet identifier
    assert pkt[16:18] == b"\x72\x6e"              # root flags/length
    assert pkt[18:22] == b"\x00\x00\x00\x04"      # VECTOR_ROOT_E131_DATA
    assert pkt[22:38] == cid
    # Framing layer
    assert pkt[38:40] == b"\x72\x58"              # framing flags/length
    assert pkt[40:44] == b"\x00\x00\x00\x02"      # VECTOR_E131_DATA_PACKET
    assert pkt[44:51] == b"OpenAVC" and pkt[51] == 0x00  # NUL-terminated name
    assert pkt[108] == 100                        # priority
    assert pkt[109:111] == b"\x00\x00"            # sync address
    assert pkt[111] == 7                          # sequence
    assert pkt[112] == 0x00                       # options
    assert pkt[113:115] == b"\x00\x01"            # universe (big-endian)
    # DMP layer
    assert pkt[115:117] == b"\x72\x0b"            # dmp flags/length
    assert pkt[117] == 0x02                       # VECTOR_DMP_SET_PROPERTY
    assert pkt[118] == 0xA1                       # address type & data type
    assert pkt[119:121] == b"\x00\x00"            # first property address
    assert pkt[121:123] == b"\x00\x01"            # address increment
    assert pkt[123:125] == b"\x02\x01"            # property value count = 513
    assert pkt[125] == 0x00                       # START code
    assert pkt[126:638] == dmx                    # 512 DMX slots verbatim


@pytest.mark.parametrize("universe,addr", [
    (1, "239.255.0.1"),
    (256, "239.255.1.0"),
    (7962, "239.255.31.26"),   # spec Appendix B.2 worked example
    (63999, "239.255.249.255"),
])
def test_universe_to_multicast(universe, addr):
    """ANSI E1.31-2018 sec 9.3.1 Table 9-1: 239.255.<universe hi byte>.<universe lo byte>."""
    assert universe_to_multicast(universe) == addr
