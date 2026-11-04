"""The WolfProt frames wolfvision_cynap builds and splits, checked against the
worked examples in WolfVision's WolfProt command reference (Cynap firmware
1.78a): the basic Boxname examples, the Cynap login with correct and wrong
credentials, and the dynamic-length Get Picture example's 1-, 2- and 4-byte
lengths (big-endian: 15 360 is written ``3C00``).
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("streaming/wolfvision_cynap.py")


def _hex(text: str) -> bytes:
    return bytes.fromhex(text)


def test_basic_examples():
    assert DRV.build_frame(DRV.BOXNAME) == _hex("08 CB03 00")
    assert DRV.build_frame(DRV.BOXNAME, b"Cynap1", is_set=True) == _hex("09 CB03 06 43796E617031")
    reply = DRV.decode_frame(_hex("08 CB03 06 43796E617031"))
    assert reply == DRV.Frame(False, DRV.BOXNAME, b"Cynap1", None)
    assert DRV.decode_frame(_hex("09 CB03 00")) == DRV.Frame(True, DRV.BOXNAME, b"", None)


def test_cynap_login_examples():
    good = DRV.login_payload(0x02, "Pandora1")
    assert DRV.build_frame(DRV.LOGIN, good, is_set=True) == _hex(
        "09 CB42 0B 02 08 50616e646f726131 00")
    wrong = DRV.login_payload(0x02, "Padora1")
    assert DRV.build_frame(DRV.LOGIN, wrong, is_set=True) == _hex(
        "09 CB42 0A 02 07 5061646f726131 00")
    # Wrong credentials: the error bit, and the length byte is the error code.
    assert DRV.decode_frame(_hex("89 CB42 07")) == DRV.Frame(True, DRV.LOGIN, b"", 0x07)


def test_length_widths_are_big_endian():
    # Get Picture (Visualizer): 250 bytes in one byte, 15 Ki in two.
    assert DRV.frame_size(_hex("08 4117 FA") + bytes(250)) == 4 + 250
    frame = _hex("0C 4117 3C00") + bytes(15360)
    assert DRV.frame_size(frame) == 5 + 15360
    assert len(DRV.decode_frame(frame).payload) == 15360
    # A four-byte length rides the extended header: 0A 01 (GET), 0B 01 (SET).
    assert DRV.build_frame(0xCD66, b"{}", is_set=True, length_width=4) == _hex(
        "0B01 CD66 00000002 7B7D")
    four = _hex("0A01 4117 00000003 010203")
    assert DRV.decode_frame(four) == DRV.Frame(False, 0x4117, b"\x01\x02\x03", None)


def test_fixed_two_byte_headers_from_the_reference():
    # Content Per Output: 0C CD5D 0003 o0..o1 ext (HDMI1, no extended info).
    assert DRV.build_frame(0xCD5D, _hex("0100 00"), length_width=2) == _hex("0C CD5D 0003 010000")
    # Subscribe: 0D CD40 ab cd, each subscribed command as its GET request.
    payload = DRV.subscribe_payload((DRV.POWER, DRV.VOLUME))
    assert payload == _hex("08CB0C00 08CB5600")
    assert DRV.build_frame(DRV.SUBSCRIBE, payload, is_set=True, length_width=2) == _hex(
        "0D CD40 0008 08CB0C00 08CB5600")


def test_split_and_resync():
    stream = _hex("08 CB0C 01 01") + _hex("88 CB56 07") + _hex("08 CB56")
    first, rest = DRV.parse_frame(stream)
    assert first == _hex("08 CB0C 01 01")
    second, rest = DRV.parse_frame(rest)
    assert DRV.decode_frame(second) == DRV.Frame(False, DRV.VOLUME, b"", 0x07)
    assert DRV.parse_frame(rest) == (None, rest)       # incomplete: kept
    assert DRV.parse_frame(_hex("70 08 CB0C 01 01"))[1] == _hex("08 CB0C 01 01")  # reserved bits: dropped


def test_window_blocks_walk_by_length():
    payload = _hex("03 020100 03 000000 04 0D0001FF 03 0E0000")
    assert DRV.windows_blocks(payload) == [_hex("020100"), _hex("000000"), _hex("0D0001FF"), _hex("0E0000")]
