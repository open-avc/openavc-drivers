"""WolfProt Visualizer 1.86d, worked examples 5–7 (picture length and login)."""

from _platform_probe import load_driver

DRIVER = load_driver("cameras/wolfvision_visualizer.py")


def test_visualizer_login_level_and_refusal():
    assert DRIVER.message(DRIVER.LOGIN) == bytes.fromhex("08 400B 00")
    denied = bytes.fromhex("89 400B 07")
    level = bytes.fromhex("08 400B 01 01")
    assert DRIVER.split_frame(denied + level) == (denied, level)
    assert DRIVER.split_frame(level) == (level, b"")


def test_two_byte_length_from_the_get_picture_example():
    header = bytes.fromhex("0C 4117 3C00")
    assert DRIVER.frame_parts(header) == (5, 5 + 15360)
    assert DRIVER.split_frame(header) == (None, header)
