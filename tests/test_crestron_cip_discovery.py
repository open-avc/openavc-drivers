"""Parser unit tests for the Crestron CIP discovery companion.

Loads ``utility/crestron_cip_discovery.py`` directly so its
``parse_crestron_cip`` helper can be exercised without a real network.
Mirrors the pattern used by ``test_pjlink_class1_discovery.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPANION_PATH = REPO_ROOT / "utility" / "crestron_cip_discovery.py"


def _load_companion_module() -> ModuleType:
    if "server.discovery.companion" not in sys.modules:
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

    module_name = "crestron_cip_discovery_under_test"
    spec = importlib.util.spec_from_file_location(module_name, COMPANION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_cip = _load_companion_module()


def _build_reply(
    hostname: str = "DIN-AP-7F74F65F",
    model: str = "DIN-AP3",
    firmware: str = "v1.502.0058.001",
) -> bytes:
    """Synthesize a minimal but realistic CIP discovery response."""
    hostname_bytes = hostname.encode("ascii")[:16].ljust(16, b"\x00")
    header = bytes([0x15]) + b"\x00" * 9
    tail = (
        b"\x00" * 4
        + model.encode("ascii") + b"\x00"
        + b"\x00" * 8
        + firmware.encode("ascii") + b"\x00"
        + b"2024-03-15\x00"
        + b"SN-12345\x00"
    )
    return header + hostname_bytes + tail


class TestParseCrestronCIP:
    def test_parses_hostname(self):
        data = _build_reply()
        reply = _cip.parse_crestron_cip(data, "192.168.1.50")
        assert reply is not None
        assert reply.ip == "192.168.1.50"
        assert reply.hostname == "DIN-AP-7F74F65F"

    def test_extracts_model_and_firmware(self):
        data = _build_reply()
        reply = _cip.parse_crestron_cip(data, "192.168.1.50")
        assert reply is not None
        assert reply.model == "DIN-AP3"
        assert reply.firmware == "1.502.0058.001"

    def test_returns_none_without_magic(self):
        # Random UDP traffic on the listening port must not be mis-parsed.
        assert _cip.parse_crestron_cip(b"", "192.168.1.50") is None
        assert _cip.parse_crestron_cip(b"\x00garbage", "192.168.1.50") is None
        # 0x14 is the request, not the response.
        assert _cip.parse_crestron_cip(b"\x14response", "192.168.1.50") is None

    def test_short_response_still_parses(self):
        # Some non-controller endpoints (DM-NVX, TSW) return a shorter
        # payload. Verify we don't crash and at least extract what we can.
        short = bytes([0x15]) + b"\x00" * 5
        reply = _cip.parse_crestron_cip(short, "192.168.1.50")
        # Magic matched -> returns a reply object even if metadata empty.
        assert reply is not None
        assert reply.ip == "192.168.1.50"

    def test_constants(self):
        assert _cip.CRESTRON_CIP_PORT == 41794
        assert _cip.CRESTRON_CIP_PROBE == b"\x14"
