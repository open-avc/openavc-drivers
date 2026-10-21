"""Protocol math for the Sharp PN-series display driver.

Checks how the driver writes a number into the 4-character parameter field
against the worked examples in Sharp's PN-L603B/PN-L703B Operation Manual,
"Controlling the Monitor with a PC (LAN)", Communication procedure.
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("displays/sharp_pn_display.py")


def test_parameter_field_formatting():
    """PN-L603B/PN-L703B Operation Manual, Communication procedure, Command format (page 48): VOLM0030, AUTR-005."""
    fmt = DRV.SharpPnDisplayDriver._fmt
    # Manual example: VOLM0030.
    assert fmt(30) == "0030"
    assert fmt(0) == "0000"
    # Manual example: AUTR-005 (three-digit negative numeral).
    assert fmt(-5) == "-005"
    assert fmt(-10) == "-010"
