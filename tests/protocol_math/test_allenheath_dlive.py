"""Protocol arithmetic of allenheath_dlive, checked against Allen & Heath's
dLive MIDI Over TCP/IP Protocol, Firmware V2.0: the Scene Recall and Cue List
Recall bank tables, the MIDI Running Status example, and the Reference
Table's Fader Level, Gain Value and socket numbering columns.
"""

from __future__ import annotations

from _platform_probe import load_driver

dl = load_driver("audio/allenheath_dlive.py")

from openavc.core.event_bus import EventBus  # noqa: E402
from openavc.core.state_store import StateStore  # noqa: E402


def test_socket_banks_and_gain_anchors():
    """dLive V2.0 Reference Table: MixRack Socket 1-64 = MP 00-3F, DX1/2 = 40-5F, DX3/4 = 60-7F; Gain Value +5 dB = 00, +10 = 0C, +30 = 3A, +60 = 7F."""
    assert dl.socket_sid(1) == "skt001"
    assert dl.socket_sid(128) == "skt128"
    assert dl.socket_label(1) == "MixRack Socket 1"
    assert dl.socket_label(65) == "DX1/2 Socket 1"
    assert dl.socket_label(97) == "DX3/4 Socket 1"
    assert dl.gain_to_gv(5.0) == 0x00
    assert dl.gain_to_gv(10.0) == 0x0C
    assert dl.gain_to_gv(30.0) == 0x3A
    assert dl.gain_to_gv(60.0) == 0x7F
    assert abs(dl.gv_to_gain(0x3A) - 30.0) < 0.2


def test_fader_codec_anchors():
    """dLive V2.0 Reference Table, Fader Level: +10 dB = 7F, 0 dB = 6B, -inf = 00."""
    assert dl.db_to_lv(10.0) == 0x7F
    assert dl.db_to_lv(0.0) == 0x6B
    assert dl.db_to_lv(-54.0) == 0x00
    assert dl.db_to_lv(-100.0) == 0x00
    assert dl.level_to_lv(0.0) == 0x00
    assert dl.level_to_lv(1.0) == 0x7F
    assert abs(dl.lv_to_level(0x7F) - 1.0) < 0.001


def test_scene_cue_banking():
    """dLive V2.0 Scene Recall (500 scenes, 4 banks) and Cue List Recall (Recall Id 0-1999, 16 banks) tables."""
    # Scene 129 opens bank 01; "For Scene 385 to 500" is bank 03, SS 00-73.
    assert dl.scene_to_bank_program(1) == (0, 0)
    assert dl.scene_to_bank_program(129) == (1, 0)
    assert dl.scene_to_bank_program(500) == (3, 0x73)
    # Cue N is Recall Id N-1: Recall Id 1152 opens bank 09, and
    # "For Recall Id 1920 to 1999" is bank 0F, SS 00-4F.
    assert dl.cue_to_bank_program(1) == (0, 0)
    assert dl.cue_to_bank_program(1153) == (9, 0)
    assert dl.cue_to_bank_program(2000) == (0x0F, 0x4F)
    for fn, bad in ((dl.scene_to_bank_program, 501),
                    (dl.scene_to_bank_program, 0),
                    (dl.cue_to_bank_program, 2001)):
        try:
            fn(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_running_status_push_spec_example():
    """dLive V2.0, MIDI Running Status: 9B 00 7F 01 7F 02 7F mutes Inputs 1-3 on MIDI channel 12."""
    driver = dl.AllenHeathDLiveDriver(
        "dlive1",
        {"host": "10.0.0.9", "port": 51325, "base_midi_channel": 12},
        StateStore(),
        EventBus(),
    )
    # The channel roster connect() registers before anything is read.
    driver._register_topology()
    driver.on_data_received(bytes([0x9B, 0x00, 0x7F, 0x01, 0x7F, 0x02, 0x7F]))
    for n in (1, 2, 3):
        assert driver.get_child_state("input", f"in{n:03d}")["mute"] is True
