"""Self-contained contract test for the novastar_h_series .avcdriver liveness check.

The check is a ``liveness:`` block: every ``interval`` seconds the runtime
sends ``send`` and waits for an inbound datagram that ``expect`` matches
(``re.search`` on the decoded text, ``ConfigurableDriver._liveness_note_data``);
``max_failures`` probes with no match drop the connection.

Any reply to the probe proves the link, the error reply included. The H Series
Control Protocol (V1.0.7, section 2) answers a command it cannot carry out --
a screen ID that does not exist among them -- with ``"ack":"Error"`` and the
command echoed, and prints its replies both with and without a space after
the colon. The replies below are that document's shapes.

Runs with PyYAML only, like the rest of the community CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

DRIVER_PATH = (
    Path(__file__).resolve().parent.parent / "video" / "novastar_h_series.avcdriver"
)
INFO = yaml.safe_load(DRIVER_PATH.read_text(encoding="utf-8"))
LIVENESS = INFO["liveness"]
EXPECT = re.compile(str(LIVENESS["expect"]))


def _answers(reply: str) -> bool:
    """Mirror ConfigurableDriver._liveness_note_data: search, not match."""
    return EXPECT.search(reply) is not None


def test_version_bumped():
    assert str(INFO["version"]) == "1.6.1"


def test_the_probe_asks_for_the_configured_screen():
    send = LIVENESS["send"]
    assert '"cmd":"R0401"' in send
    assert "{device_id}" in send and "{screen_id}" in send


def test_a_compact_reply_answers():
    assert _answers('[{"cmd":"R0401","deviceId":0,"ack":"Ok","brightness":100}]')


def test_a_reply_printed_with_spaces_answers():
    """The protocol document prints replies as ``"cmd": "***"``."""
    assert _answers('[{"cmd": "R0401", "ack": "Ok", "deviceId": 0}]')


def test_the_error_reply_for_a_screen_that_does_not_exist_answers():
    """A wrong screen ID is answered with an error: the unit is there."""
    assert _answers('[{"cmd": "R0401", "deviceId":0, "ack":"Error"}]')
    assert _answers('[{"cmd":"R0401","deviceId":0,"ack":"Error"}]')


def test_the_echo_is_matched_in_either_case():
    """The protocol's instruction characters are case insensitive."""
    assert _answers('[{"cmd":"r0401","deviceId":0,"ack":"Ok"}]')


def test_another_commands_reply_does_not_count():
    """expect exists to keep a different reply from standing in for this one."""
    assert not _answers('[{"cmd":"R0501","deviceId":0,"ack":"Ok"}]')
    assert not _answers('[{"cmd":"W0410","deviceId":0,"ack":"Ok"}]')
