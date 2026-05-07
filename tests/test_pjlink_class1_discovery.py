"""Parser unit tests for the PJLink Class 1 + Class 2 discovery companion.

Loads ``projectors/pjlink_class1_discovery.py`` directly so the parser
helpers (``parse_pjlink_ackn``, ``parse_pjlink_info_responses``) can be
exercised without a real network. Mirrors the pattern community-driver
test files use (``test_build_index.py`` etc.).

The companion's network entrypoint (``probe(ctx)``) is exercised via
the openavc test suite's Phase 9.7 integration tests; this file pins
the load-bearing parsers — bad parser regressions silently break PJLink
identification across the whole projector category.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPANION_PATH = REPO_ROOT / "projectors" / "pjlink_class1_discovery.py"


def _load_companion_module() -> ModuleType:
    """Import the companion file in isolation.

    The companion imports ``server.discovery.companion`` which is part
    of the openavc platform — so we stub a minimal ``ProbeContext``
    placeholder before exec so the import succeeds in the community-
    repo test environment (which doesn't have openavc on its path).
    """
    if "server.discovery.companion" not in sys.modules:
        # Stub the openavc symbol the companion imports.
        stub_pkg = ModuleType("server")
        stub_pkg.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault("server", stub_pkg)
        stub_disc = ModuleType("server.discovery")
        stub_disc.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault("server.discovery", stub_disc)
        stub_comp = ModuleType("server.discovery.companion")

        class _StubProbeContext:  # noqa: D401 — test stub
            """Placeholder so the companion's type annotation imports."""

        stub_comp.ProbeContext = _StubProbeContext
        sys.modules["server.discovery.companion"] = stub_comp

    module_name = "pjlink_class1_discovery_under_test"
    spec = importlib.util.spec_from_file_location(module_name, COMPANION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass resolution walks sys.modules[__module__].__dict__ during
    # @dataclass decoration; register before exec.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_pjlink = _load_companion_module()


# ===== Class 2 SRCH ACKN parser =====


class TestParsePJLinkAckn:
    def test_standard_response(self):
        reply = _pjlink.parse_pjlink_ackn(b"%2ACKN=001122aabbcc\r", "10.0.0.50")
        assert reply is not None
        assert reply.ip == "10.0.0.50"
        assert reply.mac == "001122aabbcc"

    def test_uppercase_mac_normalized_to_lowercase(self):
        reply = _pjlink.parse_pjlink_ackn(b"%2ACKN=001122AABBCC\r", "10.0.0.50")
        assert reply is not None
        assert reply.mac == "001122aabbcc"

    def test_trailing_whitespace_tolerated(self):
        reply = _pjlink.parse_pjlink_ackn(b"%2ACKN=001122aabbcc \r\n", "10.0.0.50")
        assert reply is not None
        assert reply.mac == "001122aabbcc"

    def test_invalid_payload_returns_none(self):
        for bogus in (
            b"",
            b"random garbage",
            b"%1ACKN=001122aabbcc",       # Class 1 has no SRCH
            b"%2ACKN=GGGGGGGGGGGG",       # non-hex
            b"%2ACKN=00112233",           # too short
        ):
            assert _pjlink.parse_pjlink_ackn(bogus, "10.0.0.50") is None


class TestFormatMac:
    def test_standard_format(self):
        assert _pjlink.format_mac("001122aabbcc") == "00:11:22:aa:bb:cc"

    def test_short_input_passthrough(self):
        # Defensive — short input is returned as-is rather than blowing up.
        assert _pjlink.format_mac("00") == "00"


# ===== Class 1 INFO query parser =====


def _info_responses(
    greeting: bytes = b"PJLINK 0\r",
    cls: bytes = b"%1CLSS=1\r",
    inf1: bytes = b"%1INF1=NEC\r",
    inf2: bytes = b"%1INF2=PA1004UL\r",
    name: bytes = b"%1NAME=Room 101\r",
    lamp: bytes = b"%1LAMP=12345 1\r",
) -> list[bytes | None]:
    return [greeting, cls, inf1, inf2, name, lamp]


class TestParsePJLinkInfoResponses:
    def test_full_response(self):
        info = _pjlink.parse_pjlink_info_responses(
            "10.0.0.50", _info_responses(),
        )
        assert info is not None
        assert info.ip == "10.0.0.50"
        assert info.pjlink_class == "1"
        assert info.manufacturer == "NEC"
        assert info.product_name == "PA1004UL"
        assert info.device_name == "Room 101"
        assert info.lamp_hours == 12345

    def test_missing_greeting_returns_none(self):
        # Random TCP traffic on 4352 must not produce a false positive.
        info = _pjlink.parse_pjlink_info_responses(
            "10.0.0.50", _info_responses(greeting=b"junk\r"),
        )
        assert info is None

    def test_empty_responses_returns_none(self):
        assert _pjlink.parse_pjlink_info_responses("10.0.0.50", []) is None

    def test_auth_required_greeting_still_parses(self):
        # PJLINK 1 <random> means auth is required — the companion still
        # identifies the device as PJLink even though the INF responses
        # come back as ERRA.
        responses = _info_responses(
            greeting=b"PJLINK 1 abcdef\r",
            cls=b"%1CLSS=ERRA\r",
            inf1=b"%1INF1=ERRA\r",
            inf2=b"%1INF2=ERRA\r",
            name=b"%1NAME=ERRA\r",
            lamp=b"%1LAMP=ERRA\r",
        )
        info = _pjlink.parse_pjlink_info_responses("10.0.0.50", responses)
        assert info is not None
        assert info.manufacturer is None  # ERRA filtered
        assert info.pjlink_class is None

    def test_partial_responses(self):
        # Some firmware times out on later queries — earlier fields
        # should still populate.
        responses = _info_responses() + []
        responses[4] = None  # NAME timed out
        responses[5] = None  # LAMP timed out
        info = _pjlink.parse_pjlink_info_responses("10.0.0.50", responses)
        assert info is not None
        assert info.manufacturer == "NEC"
        assert info.product_name == "PA1004UL"
        assert info.device_name is None
        assert info.lamp_hours is None

    def test_lamp_hours_not_a_number(self):
        responses = _info_responses(lamp=b"%1LAMP=NOT_A_NUMBER\r")
        info = _pjlink.parse_pjlink_info_responses("10.0.0.50", responses)
        assert info is not None
        assert info.lamp_hours is None
