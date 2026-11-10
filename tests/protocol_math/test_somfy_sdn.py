"""Wire arithmetic against Somfy's published Frame Builder examples.

DOC155888/000 p.4: Glydea, broadcast, source 000001, IP value 3.
https://service.somfy.com/downloads/bui_v4/sdn-integration-guide--preliminary.pdf
DOC155888/002 p.4: 30 DC, source 000001, destination 000002, down.
https://service.somfy.com/downloads/bui_v4/sdn-integration-guide-rev.002.pdf
DOC155888/004 pp.11,17: address ordering and firmware reference example.
"""

from _platform_probe import load_driver

DRV = load_driver("utility/somfy_sdn.py")


def test_manufacturer_glydea_frame_builder_example():
    # The screenshot's raw position value is 3; its six data bytes include
    # zero reserved and unused tilt bytes. Compare the whole wire frame.
    expected = bytes.fromhex("FC EE F9 FE FF FF 00 00 00 FD FC FF FF FF FF 0B D4")
    data = DRV.move_data("recall_ip", {"ip": 4, "angle": 0})
    assert DRV.Frame(3, "000001", "FFFFFF", data, node_type=6).encode() == expected
    assert DRV.parse_frame(expected) == (expected, b"")


def test_manufacturer_30dc_frame_builder_example():
    expected = bytes.fromhex("FC F0 FD FE FF FF FD FF FF FF FF FF 00 0B DD")
    assert (
        DRV.Frame(3, "000001", "000002", b"\x00\x00\x00\xff", node_type=2).encode()
        == expected
    )


def test_document_address_and_firmware_examples():
    packet = DRV.Frame(0x40, "050403", "000102").encode()
    assert bytes(value ^ 255 for value in packet[3:9]) == bytes.fromhex(
        "03 04 05 02 01 00"
    )
    assert DRV.simple_reply_state(0x75, bytes.fromhex("4D433E410200")) == {
        "firmware": "5063486A02"
    }
