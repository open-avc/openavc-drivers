"""The level and address encoding etc_paradigm does on the wire, checked
against the worked examples in ETC's "Unison Paradigm Serial Access Protocol
(PSAP) 6.0.0 Configuration Guide" (7180M1250-6.0.0 Rev A).
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("lighting/etc_paradigm.py")


def test_a_reported_level_is_read_as_a_percentage():
    """Control Channel Commands: "chan int:128 Zone 1" sets Zone 1 to 50%;
    Status Commands: levels are always returned as 0-255."""
    status = DRV.parse_line("chan int:128 Zone 1, Primary Space 1")
    assert (status.kind, status.level, status.name, status.space) == (
        "channel", 128, "Zone 1", "Primary Space 1")
    assert DRV.percent_from_raw(128) == 50
    assert DRV.percent_from_raw(0) == 0 and DRV.percent_from_raw(255) == 100


def test_names_spaces_and_fade_times_follow_the_examples():
    """Control Channel Commands: "chan int:75% Dimmer 2, Primary Space 1";
    Preset Commands: "pst act Preset 1, Primary Space 1,5" (five second fade);
    Space, Wall, Macro and Override Commands: the name alone."""
    assert DRV.format_address("Dimmer 2", "Primary Space 1") == "Dimmer 2, Primary Space 1"
    assert DRV.format_address("Preset 1", "Primary Space 1", 5) == "Preset 1, Primary Space 1,5"
    assert DRV.format_address("Primary Space 1") == "Primary Space 1"
    # "Fade time format is x.y seconds, with support for tenths of seconds."
    assert DRV.format_fade(2.5) == "2.5" and DRV.format_fade(3.0) == "3"


def test_status_answers_in_the_guides_forms():
    """PSAP Status Commands: the group answer carries a comma after the level
    ("grp int:level, grpname, spacename"), the macro and wall answers are
    written "Macro on macroname" and "Wall open wallname, spacename"."""
    grp = DRV.parse_line("grp int:128, Group 1, Primary Space 1")
    assert (grp.kind, grp.level, grp.name, grp.space) == ("group", 128, "Group 1", "Primary Space 1")
    macro = DRV.parse_line("Macro running Macro 1")
    assert (macro.kind, macro.verb, macro.name) == ("macro", "running", "Macro 1")
    wall = DRV.parse_line("Wall close Wall 1, Primary Space 1")
    assert (wall.kind, wall.verb, wall.name, wall.space) == ("wall", "close", "Wall 1", "Primary Space 1")
    pst = DRV.parse_line("pst alth Preset 1, Primary Space 1")
    assert (pst.kind, pst.verb) == ("preset", "alth")
    # P-ACP Mk2 6.1.0 release notes, PDGM-22773: a get answered "error invalid".
    assert DRV.parse_line("error invalid").kind == "error"
