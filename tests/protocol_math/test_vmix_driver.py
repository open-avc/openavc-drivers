"""The volume scale the vmix driver converts (fader position written,
amplitude read back) and the length vMix announces for an XML reply, both
measured on vMix 29.0.0.49. vMix's documentation calls both volume scales
"Volume 0-100" and says nothing about the trailing CRLF, so the values here
come from that vMix build, not from a document.
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("video/vmix.py")

_parse_vmix_frame = DRV._parse_vmix_frame
_XML_BODY_PREFIX = DRV._XML_BODY_PREFIX


def test_parse_xml_length_includes_the_trailing_crlf():
    """vMix counts the CRLF it appends in the length it announces.

    Measured on vMix 29.0.0.49: a 1888-byte document is announced as
    "XML 1890". The body therefore arrives with a trailer, and the parser
    must consume exactly what was promised or every frame after it is offset
    by two bytes. The document below is synthetic; the rule is the measured one.
    """
    document = b"<vmix><version>29.0.0.49</version></vmix>"
    payload = document + b"\r\n"
    buffer = f"XML {len(payload)}\r\n".encode() + payload + b"TALLY OK 12\r\n"

    msg, remaining = _parse_vmix_frame(buffer)
    assert msg[len(_XML_BODY_PREFIX):] == payload
    # The next frame must still line up.
    msg2, remaining = _parse_vmix_frame(remaining)
    assert msg2 == b"TALLY OK 12"
    assert remaining == b""


def test_fader_and_amplitude_are_a_fourth_power_apart():
    """The write scale and the read scale differ, and by exactly this much.

    Measured on vMix 29.0.0.49: writing 50 reads back 6.25, 75 reads back
    31.64063, 90 reads back 65.60999. The 0, 25 and 100 cases are the same
    fourth-power law at the ends and the quarter point.
    """
    to_fader = DRV.amplitude_to_fader
    assert to_fader(0) == 0.0
    assert to_fader(0.390625) == 25.0
    assert to_fader(6.25) == 50.0
    assert to_fader(31.64063) == 75.0
    assert to_fader(65.60999) == 90.0
    assert to_fader(100) == 100.0
