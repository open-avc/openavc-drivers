"""The ActivPanel control-code frame promethean_activpanel_le builds and splits:
``F6 <command> <type> <data...> <checksum> 6F``, the checksum the sum of every
byte before it, checked against the codes and worked examples in Promethean's
"RS232 Control Codes for ActivPanel LE" (support article 2905), and the LE's
own input table against the article's Source rows.
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("displays/promethean_activpanel_le.py")


def _hex(text: str) -> bytes:
    return bytes.fromhex(text)


def test_frames_match_the_articles_codes():
    """Article 2905, System Commands table and the volume worked example."""
    cases = {
        "01 01 01": "F6 01 01 01 F9 6F",   # Power On
        "01 01 00": "F6 01 01 00 F8 6F",   # Set Standby
        "01 05 00": "F6 01 05 00 FC 6F",   # Set Shutdown (Sleep)
        "01 02 00": "F6 01 02 00 F9 6F",   # Get Power ON/OFF status
        "03 06 00": "F6 03 06 00 FF 6F",   # Set Factory Reset
        "3B 01 01": "F6 3B 01 01 33 6F",   # Reset Apps And Data (sum wraps)
        "03 01 00": "F6 03 01 00 FA 6F",   # Get Firmware Version Scaler
        "03 02 00": "F6 03 02 00 FB 6F",   # Get Firmware Version Touch
        "03 04 00": "F6 03 04 00 FD 6F",   # Get Panel Serial No
        "03 05 00": "F6 03 05 00 FE 6F",   # Get Model Number
        "0C 00 01": "F6 0C 00 01 03 6F",   # Volume Up
        "0C 00 00": "F6 0C 00 00 02 6F",   # Volume Down
        "0C 01 00": "F6 0C 01 00 03 6F",   # Set Volume to zero, the worked example
        "0C 01 32": "F6 0C 01 32 35 6F",   # Set Volume 50%, the worked example
        "0C 02 00": "F6 0C 02 00 04 6F",   # Get Volume status
        "02 00 01": "F6 02 00 01 F9 6F",   # Mute On
        "02 00 00": "F6 02 00 00 F8 6F",   # Mute Off
        "02 02 00": "F6 02 02 00 FA 6F",   # Get Mute ON/OFF status
        "30 02 00": "F6 30 02 00 28 6F",   # Get Input Source status
    }
    for body, frame in cases.items():
        assert DRV.build_frame(_hex(body)) == _hex(frame), body


# Article 2905, Source rows: the set code, and the status reply for that input.
ARTICLE_INPUTS = {
    "hdmi_front": ("F6 30 01 09 30 6F", "F6 30 02 09 31 6F"),
    "hdmi_1": ("F6 30 01 0A 31 6F", "F6 30 02 0A 32 6F"),
    "hdmi_2": ("F6 30 01 13 3A 6F", "F6 30 02 13 3B 6F"),
    "usb_c_front": ("F6 30 01 0C 33 6F", "F6 30 02 0C 34 6F"),
    "usb_c_2": ("F6 30 01 0D 34 6F", "F6 30 02 0D 35 6F"),
    "displayport": ("F6 30 01 14 3B 6F", "F6 30 02 14 3C 6F"),
    "ops": ("F6 30 01 12 39 6F", "F6 30 02 12 3A 6F"),
    "android": ("F6 30 01 0B 32 6F", "F6 30 02 0B 33 6F"),
}


def test_the_input_table_is_the_articles():
    assert set(DRV.INPUT_CODES) == set(ARTICLE_INPUTS)
    for name, (set_code, reply) in ARTICLE_INPUTS.items():
        assert DRV.build_frame(bytes([0x30, 0x01, DRV.INPUT_CODES[name]])) == _hex(set_code), name
        frame = _hex(reply)
        assert DRV.parse_frame(frame) == (frame, b"")
        command, ftype, data = DRV.frame_parts(frame)
        assert (command, ftype) == (0x30, 0x02)
        assert DRV.INPUT_NAMES[data[0]] == name


def test_power_replies_from_the_article_read():
    """Get Power ON/OFF status: 01 on, 02 standby, 03 sleep."""
    replies = {
        "F6 01 02 01 FA 6F": "on",
        "F6 01 02 02 FB 6F": "standby",
        "F6 01 02 03 FC 6F": "sleep",
    }
    for reply, state in replies.items():
        frame = _hex(reply)
        assert DRV.parse_frame(frame) == (frame, b"")
        command, ftype, data = DRV.frame_parts(frame)
        assert (command, ftype) == (0x01, 0x02)
        assert DRV.POWER_STATES[data[0]] == state


def test_the_notes_reply_examples_split_and_read():
    """The article's Notes: the volume status example and the scaler firmware text."""
    volume = _hex("F6 0C 02 32 36 6F")
    assert DRV.parse_frame(volume) == (volume, b"")
    assert DRV.frame_parts(volume) == (0x0C, 0x02, b"\x32")
    firmware = _hex("F6 03 01 33 2E 32 2E 30 2E 31 34 7E 6F")
    assert DRV.parse_frame(firmware) == (firmware, b"")
    assert DRV.frame_parts(firmware) == (0x03, 0x01, b"3.2.0.14")


def test_text_holding_the_end_code_does_not_cut_the_frame():
    """A text reply can hold 0x6F (a lowercase 'o') anywhere, and can checksum
    to 0x6F; either way the frame ends at its own end code."""
    for text in (b"o", b"oo", b"Promethean LE pro", b"1.0.o.2"):
        frame = DRV.build_frame(bytes([0x03, 0x05]) + text)
        assert DRV.parse_frame(frame + frame) == (frame, frame), text
    # Text whose checksum is itself 0x6F: F6 03 05 + "q" sums to 0x16F, and
    # F6 03 05 + "oVVV" to 0x26F with an 'o' inside as well.
    for text in (b"q", b"oVVV"):
        frame = DRV.build_frame(bytes([0x03, 0x05]) + text)
        assert frame[-2:] == b"\x6f\x6f", text
        assert DRV.parse_frame(frame + frame) == (frame, frame), text


def test_every_volume_value_splits_whole_back_to_back():
    """Every 0-100 value of Set Volume and a Get Volume reply, two frames per
    read, with noise ahead of them and a partial frame behind."""
    for ftype in (0x01, 0x02):
        for value in range(101):
            frame = DRV.build_frame(bytes([0x0C, ftype, value]))
            other = DRV.build_frame(bytes([0x01, 0x02, 0x01]))
            stream = b"\x00\x6f" + frame + other + frame[:3]
            got, rest = DRV.parse_frame(stream)
            assert got == frame, (hex(ftype), value)
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
