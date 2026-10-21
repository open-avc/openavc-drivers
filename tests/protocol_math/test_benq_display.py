"""The framed packet benq_display builds (length byte, ID, type, code, value,
CR), checked against BenQ's "RM6503/RM7503/RM8603/RM8603T RS232 & LAN
Protocol Installation Guide": its Set-function worked example, and the Len
column of its Get-function table for the longer packets.
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("displays/benq_display.py")


def test_packet_build_matches_manual_examples():
    """BenQ RM6503 RS232 & LAN Protocol Installation Guide: Set-function Example 1, and the Get-function table's Operation Time and Model Info rows."""
    from openavc.core.event_bus import EventBus
    from openavc.core.state_store import StateStore

    cfg = {"host": "10.0.0.60", "port": 4660, "monitor_id": 1, "poll_interval": 0}
    driver = DRV.BenqDisplayDriver("ifp1", cfg, StateStore(), EventBus())
    # Manual example: Set Brightness 76 for ID 01 ->
    # 38 30 31 73 24 30 37 36 0D
    assert driver._packet("s", 0x24, b"076") == b"801s\x24076\r"
    # Get with a five-digit value field is a 10-byte packet: length ':'.
    # The Get-function table gives Operation Time (g 76) Len 10, and the
    # Protocol Command Description defines Length as the total bytes
    # excluding CR. The guide's own Get Example 5 (Operation time) prints
    # 0x38 here, contradicting that table; the test asserts the table.
    assert driver._packet("g", 0x76, b"00000") == b":01g\x7600000\r"
    # Model-info get carries a binary selector + NUL padding: length 'D' (20).
    # Get-function table: Model Info (g 20) Len 20, a 15-byte input value
    # whose Byte1=0x02 is Get Customer Model Name.
    packet = driver._packet("g", 0x20, b"\x02" + b"\x00" * 14)
    assert packet[0:1] == b"D" and len(packet) == 21 and packet.endswith(b"\r")
