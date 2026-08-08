"""Tests for the Chazy Control Pro discovery companion.

Loads ``switchers/chazy_control_pro_discovery.py`` directly with a stubbed
``openavc.discovery.companion`` so it runs without a real ``openavc`` install
(CI runs the community repo with openavc imports blocked). Exercises both
the pure ``parse_welcome`` helper and the full ``probe`` path against a
loopback telnet server that replays the live IAC-then-banner segmentation.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPANION_PATH = REPO_ROOT / "switchers" / "chazy_control_pro_discovery.py"

# Live-hardware IAC negotiation prefix (the controller sends this first, in
# its own TCP segment, before the banner).
IAC_PREFIX = bytes([0xFF, 0xFB, 0x03, 0xFF, 0xFB, 0x01, 0xFF, 0xFD, 0x00])

PRO_BANNER = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To TAV-CHAZY-CLTPRO Terminal Control System\r\n"
    "FW Version: 1.10.11\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
    "CONTROLLER> "
)

CONTROL_BANNER = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To CHAZY CONTROL Terminal Control System\r\n"
    "FW Version: 1.00.17\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
    "CONTROLLER> "
)

# Darwin Control shares the hostname + telnet port; both its welcome tokens
# (verified Controller(h) and the FW 2.03.19 DARWIN CONTROL brand) must be
# rejected by the Pro companion.
DARWIN_BANNER_H = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To Controller(h) Terminal Control System\r\n"
    "FW Version: 1.50.02\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
    "CONTROLLER> "
)
DARWIN_BANNER_BRAND = DARWIN_BANNER_H.replace("Controller(h)", "DARWIN CONTROL")


def _load_companion() -> ModuleType:
    if "openavc.discovery.companion" not in sys.modules:
        stub_pkg = ModuleType("openavc")
        stub_pkg.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault("openavc", stub_pkg)
        stub_disc = ModuleType("openavc.discovery")
        stub_disc.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault("openavc.discovery", stub_disc)
        stub_comp = ModuleType("openavc.discovery.companion")

        class _StubProbeContext:  # noqa: D401 — test stub
            """Placeholder so the companion's type annotation imports."""

        stub_comp.ProbeContext = _StubProbeContext
        sys.modules["openavc.discovery.companion"] = stub_comp

    module_name = "chazy_control_pro_discovery_under_test"
    spec = importlib.util.spec_from_file_location(module_name, COMPANION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_companion()


class _RecordingCtx:
    """Minimal ProbeContext stand-in recording emit_active calls."""

    def __init__(self, hosts_by_open_port: dict[int, tuple[str, ...]]) -> None:
        self.source_ip = ""
        self.hosts_by_open_port = hosts_by_open_port
        self.timeout_seconds = 5.0
        self.log = logging.getLogger("test.chazy_control_pro_discovery")
        self.emitted: list[dict] = []

    async def emit_active(self, host, response, *, probe_id=None, port=None,
                          matched_pattern=None):
        self.emitted.append({
            "host": host, "response": response, "probe_id": probe_id,
            "port": port, "matched_pattern": matched_pattern,
        })


async def _serve_once(banner: str):
    """Start a loopback server that replays IAC then the banner, segmented."""

    async def handle(reader, writer):
        try:
            writer.write(IAC_PREFIX)
            await writer.drain()
            await asyncio.sleep(0.05)  # force the banner into a later segment
            writer.write(banner.encode("latin-1"))
            await writer.drain()
            # Hold open until the client (companion) closes after the sentinel.
            try:
                await asyncio.wait_for(reader.read(64), timeout=1.0)
            except (TimeoutError, asyncio.TimeoutError):
                pass
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _run_probe_against(banner: str) -> list[dict]:
    server, port = await _serve_once(banner)
    _mod.CHAZY_TELNET_PORT = port  # align candidate lookup + connect target
    ctx = _RecordingCtx({port: ("127.0.0.1",)})
    try:
        await _mod.probe(ctx)
    finally:
        server.close()
        await server.wait_closed()
    return ctx.emitted


# --- parse_welcome ---------------------------------------------------------


def test_parse_welcome_pro():
    model, fw = _mod.parse_welcome(PRO_BANNER)
    assert model == "TAV-CHAZY-CLTPRO"
    assert fw == "1.10.11"


def test_parse_welcome_with_iac_noise():
    text = IAC_PREFIX.decode("latin-1") + PRO_BANNER
    model, fw = _mod.parse_welcome(text)
    assert model == "TAV-CHAZY-CLTPRO"
    assert fw == "1.10.11"


def test_parse_welcome_non_chazy_returns_none():
    model, fw = _mod.parse_welcome("login: \r\nPassword: \r\n")
    assert model is None
    assert fw is None


# --- probe end-to-end ------------------------------------------------------


def test_probe_identifies_pro_controller():
    emitted = asyncio.run(_run_probe_against(PRO_BANNER))
    assert len(emitted) == 1
    rec = emitted[0]
    assert rec["host"] == "127.0.0.1"
    assert rec["port"] == _mod.CHAZY_TELNET_PORT
    resp = rec["response"]
    assert resp["manufacturer"] == "TurtleAV"
    assert resp["model"] == "TAV-CHAZY-CLTPRO"
    assert resp["firmware"] == "1.10.11"
    assert resp["protocols"] == ["chazy_telnet"]
    assert "TAV-CHAZY-CLTPRO" in rec["matched_pattern"]


def test_probe_ignores_standard_control_banner():
    # The standard Control's banner must NOT be claimed by the Pro companion.
    emitted = asyncio.run(_run_probe_against(CONTROL_BANNER))
    assert emitted == []


def test_probe_ignores_darwin_banners():
    # Darwin shares the default hostname + telnet port; neither of its welcome
    # tokens may be claimed by the Pro companion.
    assert asyncio.run(_run_probe_against(DARWIN_BANNER_H)) == []
    assert asyncio.run(_run_probe_against(DARWIN_BANNER_BRAND)) == []


def test_is_pro_token_accepts_rebrands_rejects_siblings():
    assert _mod.is_pro_token("TAV-CHAZY-CLTPRO")
    assert _mod.is_pro_token("CHAZY CONTROL PRO")    # hypothetical rebrand
    assert not _mod.is_pro_token("CHAZY CONTROL")    # standard
    assert not _mod.is_pro_token("Controller(h)")    # darwin (verified)
    assert not _mod.is_pro_token("DARWIN CONTROL")   # darwin (branded)
    assert not _mod.is_pro_token(None)


def test_probe_no_candidates_emits_nothing():
    ctx = _RecordingCtx({})
    asyncio.run(_mod.probe(ctx))
    assert ctx.emitted == []


def test_manufacturer_normalizes_to_alias():
    # The reserved manufacturer string must normalize to a declared
    # manufacturer_alias ("turtleav") so vendor-string narrowing fires.
    assert _mod.MANUFACTURER.strip().lower() == "turtleav"
