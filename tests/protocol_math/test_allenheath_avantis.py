"""Protocol arithmetic of allenheath_avantis, checked against Allen & Heath's
Avantis MIDI TCP/IP Protocol: the Fader Level table, the Scene Recall table
and the document's MIDI Running Status example.
"""

from __future__ import annotations

from _platform_probe import load_driver

av = load_driver("audio/allenheath_avantis.py")

from openavc.core.event_bus import EventBus  # noqa: E402
from openavc.core.state_store import StateStore  # noqa: E402


def test_fader_codec_anchors():
    """Avantis MIDI TCP/IP Protocol, Fader Level table: +10 dB = 7F, 0 dB = 6B, -5 dB = 61, -inf = 00."""
    assert av.db_to_lv(10.0) == 0x7F
    assert av.db_to_lv(0.0) == 0x6B
    assert av.db_to_lv(-5.0) == 0x61
    assert av.db_to_lv(-54.0) == 0x00
    assert av.db_to_lv(-100.0) == 0x00
    assert av.level_to_lv(0.0) == 0x00
    assert av.level_to_lv(1.0) == 0x7F
    assert abs(av.lv_to_level(0x7F) - 1.0) < 0.001


def test_running_status_push_spec_example():
    """Avantis MIDI TCP/IP Protocol, MIDI Running Status: 9B 00 7F 01 7F 02 7F mutes Inputs 1-3 on MIDI channel 12."""
    driver = av.AllenHeathAvantisDriver(
        "avantis1",
        {"host": "10.0.0.9", "port": 51325, "base_midi_channel": 12},
        StateStore(),
        EventBus(),
    )
    # The channel roster connect() registers before anything is read.
    driver._register_topology()
    driver.on_data_received(bytes([0x9B, 0x00, 0x7F, 0x01, 0x7F, 0x02, 0x7F]))
    for n in (1, 2, 3):
        assert driver.get_child_state("input", f"in{n:02d}")["mute"] is True


def test_scene_banking():
    """Avantis MIDI TCP/IP Protocol, Scene Recall: 500 scenes in 4 banks of 128; scene 500 = bank 03, SS 73."""
    assert av.scene_to_bank_program(1) == (0, 0)
    assert av.scene_to_bank_program(129) == (1, 0)
    assert av.scene_to_bank_program(500) == (3, 0x73)
    for bad in (0, 501):
        try:
            av.scene_to_bank_program(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
