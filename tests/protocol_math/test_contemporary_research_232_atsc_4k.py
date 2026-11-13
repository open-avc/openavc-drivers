"""May 2026 product manual, worked channel example on p. 18 and overflow on p. 23."""

from _platform_probe import load_driver

driver = load_driver("video/contemporary_research_232_atsc_4k.py")


def test_manual_channel_examples():
    # The worked tuning example leaves two fields as x.
    status = driver.channel_status("U006Uxx1002x0")
    assert status["current_channel"] == "6-2"
    assert status["received_resolution"] == "720p"

    # The manual's 1032 example encodes a thousands count in the minor field.
    assert driver.channel_status("U032U0C0F01x0")["current_channel"] == "1032"
