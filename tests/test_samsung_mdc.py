"""Unit tests for the samsung_mdc driver's MDC binary frame helpers.

Loads ``displays/samsung_mdc.py`` directly, stubbing the ``server.*`` imports it
needs (BaseDriver, checksum_sum, frame parsers, get_logger) so the community
repo's test suite stays self-contained — mirrors test_qsc_qrc.py /
test_chazy_control.py. conftest.py rolls the stubs back after this module
imports so they don't leak into later tests.

Covers the byte-exact request framing and the streaming response parser — the
real protocol logic worth locking in. (The stubbed ``checksum_sum`` reproduces
the platform's ``sum(data) & 0xFF`` so the checksum assertions are meaningful.)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "displays" / "samsung_mdc.py"


def _install_server_stubs() -> None:
    """Provide the minimal ``server.*`` surface samsung_mdc.py imports."""
    if "server.drivers.base" in sys.modules:
        return

    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server", server)

    drivers = ModuleType("server.drivers")
    drivers.__path__ = []  # type: ignore[attr-defined]
    base = ModuleType("server.drivers.base")

    class BaseDriver:  # minimal: the driver only subclasses it; no methods run
        pass

    base.BaseDriver = BaseDriver
    sys.modules["server.drivers"] = drivers
    sys.modules["server.drivers.base"] = base

    transport = ModuleType("server.transport")
    transport.__path__ = []  # type: ignore[attr-defined]
    binary_helpers = ModuleType("server.transport.binary_helpers")

    def checksum_sum(data: bytes, mask: int = 0xFF) -> int:
        return sum(data) & mask

    binary_helpers.checksum_sum = checksum_sum

    frame_parsers = ModuleType("server.transport.frame_parsers")

    class CallableFrameParser:  # referenced in the class body, never called here
        def __init__(self, *a, **k):
            pass

    class FrameParser:
        pass

    frame_parsers.CallableFrameParser = CallableFrameParser
    frame_parsers.FrameParser = FrameParser
    sys.modules["server.transport"] = transport
    sys.modules["server.transport.binary_helpers"] = binary_helpers
    sys.modules["server.transport.frame_parsers"] = frame_parsers

    utils = ModuleType("server.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    logger = ModuleType("server.utils.logger")

    def get_logger(_name):
        import logging
        return logging.getLogger("test_samsung_mdc")

    logger.get_logger = get_logger
    sys.modules["server.utils"] = utils
    sys.modules["server.utils.logger"] = logger


_install_server_stubs()

_spec = importlib.util.spec_from_file_location("samsung_mdc", DRIVER_PATH)
_mdc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mdc)

_build_mdc_frame = _mdc._build_mdc_frame
_parse_mdc_frame = _mdc._parse_mdc_frame


# --- Request framing ---


def test_build_frame_power_on():
    frame = _build_mdc_frame(0x11, 1, bytes([1]))
    assert frame[0] == 0xAA  # header
    assert frame[1] == 0x11  # command
    assert frame[2] == 1     # display id
    assert frame[3] == 1     # data length
    assert frame[4] == 1     # data: power on


def test_build_frame_checksum():
    frame = _build_mdc_frame(0x11, 1, bytes([1]))
    # checksum = sum of every byte after the header, masked to 0xFF
    expected_cs = (0x11 + 0x01 + 0x01 + 0x01) & 0xFF
    assert frame[-1] == expected_cs


# --- Streaming response parser ---


def test_parse_frame_complete():
    frame = _build_mdc_frame(0x11, 1, bytes([1]))
    result, remaining = _parse_mdc_frame(frame)
    assert result is not None
    assert result[0] == 0x11  # command (header + checksum stripped)
    assert remaining == b""


def test_parse_frame_incomplete():
    result, remaining = _parse_mdc_frame(b"\xAA\x11")
    assert result is None
    assert remaining == b"\xAA\x11"


def test_parse_frame_no_header():
    # No 0xAA marker -> parser discards the garbage.
    result, remaining = _parse_mdc_frame(b"\x00\x01\x02")
    assert result is None
    assert remaining == b""


def test_parse_frame_skips_garbage_before_header():
    frame = _build_mdc_frame(0x11, 1, bytes([1]))
    result, remaining = _parse_mdc_frame(b"\x00\xFF" + frame)
    assert result is not None
    assert result[0] == 0x11
    assert remaining == b""


def test_parse_frame_multiple():
    frame1 = _build_mdc_frame(0x11, 1, bytes([1]))
    frame2 = _build_mdc_frame(0x12, 1, bytes([50]))
    msg1, rest = _parse_mdc_frame(frame1 + frame2)
    assert msg1 is not None and msg1[0] == 0x11
    msg2, rest = _parse_mdc_frame(rest)
    assert msg2 is not None and msg2[0] == 0x12
    assert rest == b""
