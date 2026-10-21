"""Protocol math for the Sharp/NEC large-format display driver.

Checks the packet frame (SOH, XOR block-check code, CR delimiter) and the
Monitor ID to destination-address encoding against NEC's "External Control -
NEC LCD Monitor" manual Rev.2.4 (G2), sections 4.1 to 4.4.
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("displays/sharp_nec_display.py")


def test_the_check_code_example_from_the_manual():
    """NEC External Control Rev.2.4 (G2) sec 4.3: the worked Check code (BCC) example, 77h."""
    packet = DRV._build_packet(1, b"E", bytes([DRV.STX]) + b"00100064" + bytes([DRV.ETX]))
    assert packet == bytes.fromhex("01 30 41 30 45 30 41 02 30 30 31 30 30 30 36 34 03 77 0D")


def test_packet_roundtrip_and_bcc():
    """NEC External Control Rev.2.4 (G2) sec 4.3 check code, 4.4 delimiter, 4.1 Destination Address conversion table."""
    packet = DRV._build_packet(1, b"A", bytes([DRV.STX]) + b"01D6" + bytes([DRV.ETX]))
    # The manual's own worked example ends with CR and carries the XOR
    # of everything between SOH and the check code.
    assert packet[0] == DRV.SOH and packet.endswith(b"\r")
    body = packet[1:-2]
    bcc = 0
    for b in body:
        bcc ^= b
    assert packet[-2] == bcc
    # Monitor ID encoding table anchors: 1='A', 26='Z', 100=0xA4.
    assert DRV._encode_monitor_id(1) == b"A"
    assert DRV._encode_monitor_id(26) == b"Z"
    assert DRV._encode_monitor_id(100) == bytes([0xA4])
