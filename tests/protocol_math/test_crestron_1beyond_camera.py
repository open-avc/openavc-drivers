"""The lightbar bytes crestron_1beyond_camera sends (four segment bytes, each
a brightness code then a colour code), checked against the tables and the
worked example on the VISCA Lightbar Commands page of Crestron's "IV-CAM
Series Manual" (Doc. 9440).
"""

from __future__ import annotations

import pytest
from _platform_probe import load_driver

DRV = load_driver("cameras/crestron_1beyond_camera.py")


def h(text: str) -> bytes:
    return bytes.fromhex(text)


LIGHTBAR_ROWS = [
    ("off", "green", "bright", "00 00 00 00"),
    ("full", "green", "bright", "0C 0C 0C 0C"),
    ("full", "green", "medium", "08 08 08 08"),
    ("full", "green", "dim", "04 04 04 04"),
    ("full", "yellow", "bright", "0F 0F 0F 0F"),
    ("full", "yellow", "medium", "0B 0B 0B 0B"),
    ("full", "yellow", "dim", "07 07 07 07"),
    ("full", "red", "bright", "0D 0D 0D 0D"),
    ("full", "red", "medium", "09 09 09 09"),
    ("full", "red", "dim", "05 05 05 05"),
    ("half", "green", "bright", "00 0C 0C 00"),
    ("half", "green", "medium", "00 08 08 00"),
    ("half", "green", "dim", "00 04 04 00"),
    ("half", "yellow", "bright", "03 0F 0F 03"),
    ("half", "yellow", "medium", "03 0B 0B 03"),
    ("half", "yellow", "dim", "03 07 07 03"),
    ("half", "red", "bright", "01 0D 0D 01"),
    ("half", "red", "medium", "01 09 09 01"),
    ("half", "red", "dim", "01 05 05 01"),
]


@pytest.mark.parametrize("width,color,level,expected", LIGHTBAR_ROWS)
def test_lightbar_rows_match_the_manuals_table(width, color, level, expected):
    """IV-CAM Series Manual (Doc. 9440), VISCA Lightbar Commands: the Common VISCA Lightbar Commands table, one row per case."""
    assert DRV.lightbar_bytes(width, color, level) == h(expected)


def test_the_default_status_colours_are_rows_of_the_same_table():
    """IV-CAM Series Manual (Doc. 9440), VISCA Lightbar Commands: the default status-colour table at the top of the page."""
    # Full green (tracking), half green (live), full yellow (update), half red (privacy).
    assert DRV.lightbar_bytes("full", "green", "bright") == h("0C 0C 0C 0C")
    assert DRV.lightbar_bytes("half", "green", "bright") == h("00 0C 0C 00")
    assert DRV.lightbar_bytes("full", "yellow", "bright") == h("0F 0F 0F 0F")
    assert DRV.lightbar_bytes("half", "red", "bright") == h("01 0D 0D 01")


def test_a_custom_segment_choice_is_brightness_then_colour():
    """IV-CAM Series Manual (Doc. 9440), VISCA Lightbar Commands: the Brightness Codes and Color Codes tables and the Bright Green Half-Width example (1100 = 0C)."""
    assert DRV.segment_from_choice("green_bright") == 0x0C  # 11 00
    assert DRV.segment_from_choice("red_dim") == 0x05       # 01 01
    assert DRV.segment_from_choice("yellow_medium") == 0x0B  # 10 11
    assert DRV.segment_from_choice("off") == 0x00
    with pytest.raises(ValueError):
        DRV.segment_from_choice("blue_bright")
