"""Protocol math for the Global Cache iTach IP2IR driver.

Checks the expansion of a sendir code written in Global Cache's compressed
IR format against the worked example in the "Global Cache Unified TCP API"
Version 1.1.2, Appendix A.
"""

from __future__ import annotations

from _platform_probe import load_driver

drv = load_driver("utility/globalcache_itach_ip2ir.py")


def test_decompress_compressed_example_from_spec():
    """Unified TCP API 1.1.2, Appendix A, Global Cache Compressed IR Format example (page 33)."""
    # The documented example "4,5A8,9ABB": A=(4,5), B=(8,9). The spec prints
    # the uncompressed form it came from: 4,5,4,5,8,9,4,5,8,9,8,9.
    assert drv.decompress_sendir_data(["4", "5A8", "9ABB"]) == [
        4, 5, 4, 5, 8, 9, 4, 5, 8, 9, 8, 9,
    ]
