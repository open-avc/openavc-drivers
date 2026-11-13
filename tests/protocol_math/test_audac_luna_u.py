"""AUDAC LUNA-U ASCII v1.6.3, CRC section, CRC16-ARC and CRC32 examples.

https://audac.eu/eu/products/d/luna-u---unified-audio-matrix-processor/command-manual/pdf/command-manual.pdf
"""

import pytest

from _platform_probe import load_driver

DRV = load_driver("audio/audac_luna_u.py")


@pytest.mark.parametrize("checksum", ["C06C", "D887125C"])
def test_audac_checksum_and_delimiters(checksum):
    frame = f"#|ALL||SET_REQ^INPUT_LINE>1^VOLUME|0|{checksum}|"
    assert DRV.checksum_ok(frame, checksum)
    assert not DRV.checksum_ok(frame.replace("|0|", "|1|"), checksum)
