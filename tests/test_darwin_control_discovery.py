"""Tests for the Darwin Control discovery companion.

Loads ``switchers/darwin_control_discovery.py`` directly with a stubbed
``server.discovery.companion`` so it runs without a real ``openavc`` install
(CI runs the community repo with openavc imports blocked). Exercises the pure
``parse_welcome`` / ``is_darwin_token`` helpers and the full ``probe`` path
against a loopback telnet server that replays the live IAC-then-banner
segmentation — and crucially that the companion does NOT claim a Chazy
controller (same hostname/port, different welcome token).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPANION_PATH = REPO_ROOT / "switchers" / "darwin_control_discovery.py"

# Live-hardware IAC negotiation prefix (controller sends this first, in its own
# TCP segment, before the banner): WILL SGA / WILL ECHO / DONT ECHO / DO BINARY.
IAC_PREFIX = bytes([
    0xFF, 0xFB, 0x03, 0xFF, 0xFB, 0x01, 0xFF, 0xFE, 0x01, 0xFF, 0xFD, 0x00,
])

# Verified Darwin welcome banner (FW 1.50.02 — token "Controller(h)").
DARWIN_BANNER = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To Controller(h) Terminal Control System\r\n"
    "FW Version: 1.50.02\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
    "CONTROLLER> "
)

# Documented FW 2.03.19 variant (token "DARWIN CONTROL"; not captured on wire).
DARWIN_FW2_BANNER = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To DARWIN CONTROL Terminal Control System\r\n"
    "FW Version: 2.03.19\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
    "CONTROLLER> "
)

# Chazy controllers share the hostname/port — must NOT be claimed.
CHAZY_PRO_BANNER = (
    "\r\n"
    "Welcome To TAV-CHAZY-CLTPRO Terminal Control System\r\n"
    "FW Version: 1.10.11\r\n"
    "CONTROLLER> "
)
CHAZY_STD_BANNER = (
    "\r\n"
    "Welcome To CHAZY CONTROL Terminal Control System\r\n"
    "FW Version: 1.00.17\r\n"
    "CONTROLLER> "
)


def _load_companion() -> ModuleType:
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

    module_name = "darwin_control_discovery_under_test"
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
        self.log = logging.getLogger("test.darwin_control_discovery")
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
    _mod.DARWIN_TELNET_PORT = port  # align candidate lookup + connect target
    ctx = _RecordingCtx({port: ("127.0.0.1",)})
    try:
        await _mod.probe(ctx)
    finally:
        server.close()
        await server.wait_closed()
    return ctx.emitted


# --- parse_welcome / is_darwin_token ---------------------------------------


def test_parse_welcome_controller_h():
    model, fw = _mod.parse_welcome(DARWIN_BANNER)
    assert model == "Controller(h)"
    assert fw == "1.50.02"


def test_parse_welcome_with_iac_noise():
    text = IAC_PREFIX.decode("latin-1") + DARWIN_BANNER
    model, fw = _mod.parse_welcome(text)
    assert model == "Controller(h)"


def test_parse_welcome_non_controller_returns_none():
    model, fw = _mod.parse_welcome("login: \r\nPassword: \r\n")
    assert model is None and fw is None


def test_is_darwin_token_accepts_controller_h_and_darwin():
    assert _mod.is_darwin_token("Controller(h)") is True
    assert _mod.is_darwin_token("CONTROLLER(H)") is True  # case-insensitive
    assert _mod.is_darwin_token("DARWIN CONTROL") is True


def test_is_darwin_token_rejects_chazy_and_others():
    assert _mod.is_darwin_token("TAV-CHAZY-CLTPRO") is False
    assert _mod.is_darwin_token("CHAZY CONTROL") is False
    assert _mod.is_darwin_token("Controller") is False  # bare, no (h)
    assert _mod.is_darwin_token(None) is False
    assert _mod.is_darwin_token("") is False


# --- probe end-to-end ------------------------------------------------------


def test_probe_identifies_controller_h():
    emitted = asyncio.run(_run_probe_against(DARWIN_BANNER))
    assert len(emitted) == 1
    rec = emitted[0]
    assert rec["host"] == "127.0.0.1"
    assert rec["port"] == _mod.DARWIN_TELNET_PORT
    resp = rec["response"]
    assert resp["manufacturer"] == "TurtleAV"
    assert resp["model"] == "Controller(h)"
    assert resp["firmware"] == "1.50.02"
    assert resp["protocols"] == ["darwin_telnet"]
    assert "Controller(h)" in rec["matched_pattern"]


def test_probe_identifies_darwin_control_fw2():
    emitted = asyncio.run(_run_probe_against(DARWIN_FW2_BANNER))
    assert len(emitted) == 1
    assert emitted[0]["response"]["model"] == "DARWIN CONTROL"
    assert emitted[0]["response"]["firmware"] == "2.03.19"


def test_probe_ignores_chazy_pro_banner():
    assert asyncio.run(_run_probe_against(CHAZY_PRO_BANNER)) == []


def test_probe_ignores_chazy_standard_banner():
    assert asyncio.run(_run_probe_against(CHAZY_STD_BANNER)) == []


def test_probe_no_candidates_emits_nothing():
    ctx = _RecordingCtx({})
    asyncio.run(_mod.probe(ctx))
    assert ctx.emitted == []


def test_manufacturer_normalizes_to_alias():
    assert _mod.MANUFACTURER.strip().lower() == "turtleav"
