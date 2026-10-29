"""The ActivPanel control-code frame promethean_activpanel10 builds and splits:
``F6 <command> <type> <data...> <checksum> 6F``, the checksum the sum of every
byte before it, checked against the worked examples in Promethean's
"RS232 Control Codes for ActivPanel 10" (support article 2752).
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("displays/promethean_activpanel10.py")


def _hex(text: str) -> bytes:
    return bytes.fromhex(text)


def test_frames_match_the_articles_codes():
    """Article 2752, System Commands table and the volume worked example."""
    cases = {
        "01 01 01": "F6 01 01 01 F9 6F",            # Power On
        "01 05 00": "F6 01 05 00 FC 6F",            # Set Shutdown (Sleep)
        "03 07 00": "F6 03 07 00 00 6F",            # Get Firmware Version MCU (sum wraps to 00)
        "0C 01 32": "F6 0C 01 32 35 6F",            # Set Volume 50%, the worked example
        "30 01 13": "F6 30 01 13 3A 6F",            # USB-C 1
        "34 13 02": "F6 34 13 02 3F 6F",            # LD Backlight Mode Theater
        "54 06 02": "F6 54 06 02 52 6F",            # Disable Touch on/off Button
        "80 01 81 0A 11 55 01 FF": "F6 80 01 81 0A 11 55 01 FF 68 6F",  # Turn on Camera
        "80 01 81 0B 01 AA FF": "F6 80 01 81 0B 01 AA FF AD 6F",        # Reset Camera
        "20 09 00": "F6 20 09 00 1F 6F",            # Get MAC Address
    }
    for body, frame in cases.items():
        assert DRV.build_frame(_hex(body)) == _hex(frame), body


def test_replies_from_the_article_split_and_read():
    """The article's reply examples: freeze status and the scaler firmware text."""
    freeze = _hex("F6 32 02 01 2B 6F")
    assert DRV.parse_frame(freeze) == (freeze, b"")
    assert DRV.frame_parts(freeze) == (0x32, 0x02, b"\x01")
    firmware = _hex("F6 03 01 33 2E 32 2E 30 2E 31 34 7E 6F")
    assert DRV.parse_frame(firmware) == (firmware, b"")
    assert DRV.frame_parts(firmware) == (0x03, 0x01, b"3.2.0.14")


def test_a_checksum_equal_to_the_end_code_does_not_cut_the_frame():
    """Set Contrast 65 and a Get Brightness reply of 64 both checksum to 0x6F."""
    for body in ("34 04 41", "34 05 40"):
        frame = DRV.build_frame(_hex(body))
        assert frame[-2:] == b"\x6f\x6f"
        assert DRV.parse_frame(frame + frame) == (frame, frame)


def test_every_level_value_splits_whole_back_to_back():
    """Every 0-100 value of every level set and get, two frames per read,
    with noise ahead of them and a partial frame behind."""
    for command, ftype in ((0x0C, 0x01), (0x0C, 0x02), *((0x34, t) for t in range(0x03, 0x0D))):
        for value in range(101):
            frame = DRV.build_frame(bytes([command, ftype, value]))
            other = DRV.build_frame(bytes([0x01, 0x02, 0x01]))
            stream = b"\x00\x6f" + frame + other + frame[:3]
            got, rest = DRV.parse_frame(stream)
            assert got == frame, (hex(command), hex(ftype), value)
            got, rest = DRV.parse_frame(rest)
            assert got == other
            assert DRV.parse_frame(rest) == (None, frame[:3])


def test_a_corrupt_frame_is_dropped_and_the_next_one_read():
    good = DRV.build_frame(_hex("02 02 01"))
    bad = bytearray(DRV.build_frame(_hex("0C 02 32")))
    bad[-2] ^= 0x01
    buffer = bytes(bad) + good
    frame, buffer = DRV.parse_frame(buffer)
    assert frame is None and buffer == good  # skipped straight to the good frame
    assert DRV.parse_frame(buffer) == (good, b"")
