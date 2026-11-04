"""The signage frame hisense_signage builds and splits:
``DD FF <length:2> <command:4> <monitor ID> <data> <checksum> BB CC`` (replies
``AB AB ... CD CD``), the checksum the XOR of length, command, ID and data,
checked against the worked examples in Hisense's 55OP90FE User Manual
(chapter 8, RS232) and the 54DS User Manual (Serial Protocol).
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("displays/hisense_signage.py")


def _hex(text: str) -> bytes:
    return bytes.fromhex(text)


def test_frames_match_the_manuals_examples():
    cases = {
        ("C1150000", "BBBB"): "DD FF 00 08 C1 15 00 00 01 BB BB DD BB CC",  # Power On
        ("C1150000", "AAAA"): "DD FF 00 08 C1 15 00 00 01 AA AA DD BB CC",  # Power Off
        ("C1310000", "00"): "DD FF 00 07 C1 31 00 00 01 00 F6 BB CC",      # Screen Off
        ("C11E0000", ""): "DD FF 00 06 C1 1E 00 00 01 D8 BB CC",            # Reboot
        ("C1FF0009", "02"): "DD FF 00 07 C1 FF 00 09 01 02 33 BB CC",      # AC power-on: standby
        ("C1080000", "12"): "DD FF 00 07 C1 08 00 00 01 12 DD BB CC",      # HDMI3
        ("C1260000", "01"): "DD FF 00 07 C1 26 00 00 01 01 E0 BB CC",      # Mute (54DS example)
        ("C1320000", "0620"): "DD FF 00 08 C1 32 00 00 01 06 20 DC BB CC",  # Backlight 32
        ("C1360000", "20"): "DD FF 00 07 C1 36 00 00 01 20 D1 BB CC",      # Brightness 32
        ("C1280000", ""): "DD FF 00 06 C1 28 00 00 01 EE BB CC",            # Query TV Status
        ("C16C0000", ""): "DD FF 00 06 C1 6C 00 00 01 AA BB CC",            # Query MAC
        ("C1170000", "0000"): "DD FF 00 08 C1 17 00 00 01 00 00 DF BB CC",  # Remote key: Menu
    }
    for (command, data), frame in cases.items():
        assert DRV.build_frame(_hex(command), 1, _hex(data)) == _hex(frame), command


def test_monitor_id_100_examples():
    """The later query examples address monitor ID 0x64."""
    assert DRV.build_frame(_hex("C16D0000"), 0x64) == _hex("DD FF 00 06 C1 6D 00 00 64 CE BB CC")
    assert DRV.build_frame(_hex("C1FE0002"), 0x64) == _hex("DD FF 00 06 C1 FE 00 02 64 5F BB CC")


def test_over_the_network_the_frame_goes_as_hex_text():
    """54DS: "DDFF0007C12600000101E0BBCC" is Mute On as the IP Control takes it."""
    frame = DRV.build_frame(_hex("C1260000"), 1, b"\x01")
    assert DRV.as_text(frame) == b"DDFF0007C12600000101E0BBCC"


def test_replies_split_in_both_forms():
    status = DRV.build_frame(_hex("C1280000"), 1, _hex("1E0505000001"))
    reply = b"\xab\xab" + status[2:-2] + b"\xcd\xcd"
    assert DRV.parse_frame(reply + b"\xab") == (reply, b"\xab")
    assert DRV.frame_parts(reply) == (_hex("C1280000"), 1, _hex("1E0505000001"))
    spaced = b" ".join(reply.hex(" ").upper().encode().split(b" "))
    assert DRV.parse_frame(spaced) == (reply, b"")
    assert DRV.parse_frame(b"noise" + reply.hex().encode()) == (reply, b"")
    assert DRV.parse_frame(reply.hex().encode()) == (reply, b"")


def test_power_data_containing_the_end_marker_still_splits():
    """Power On's data is BB BB, so only the length can find the frame's end."""
    reply = b"\xab\xab\x00\x08\xc1\x15\x00\x00\x01\xbb\xbb\xdd\xcd\xcd"
    assert DRV.parse_frame(reply) == (reply, b"")


def test_a_corrupt_reply_is_dropped_and_the_next_one_read():
    good = b"\xab\xab\x00\x07\xc1\x26\x00\x00\x01\x01\xe0\xcd\xcd"
    bad = good[:-3] + b"\x00" + good[-2:]
    frame, rest = DRV.parse_frame(bad + good)
    while frame is None and rest:
        frame, rest = DRV.parse_frame(rest)
    assert frame == good
