"""Tests for the Chazy Control (standard) discovery companion.

Loads ``switchers/chazy_control_discovery.py`` directly with a stubbed
``server.discovery.companion`` so it runs without a real ``openavc`` install.
Exercises ``parse_welcome`` and the full ``probe`` path against a loopback
telnet server, including rejecting the Pro's banner.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPANION_PATH = REPO_ROOT / "switchers" / "chazy_control_discovery.py"

IAC_PREFIX = bytes([0xFF, 0xFB, 0x03, 0xFF, 0xFB, 0x01, 0xFF, 0xFD, 0x00])

CONTROL_BANNER = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To CHAZY CONTROL Terminal Control System\r\n"
    "FW Version: 1.00.17\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
    "CONTROLLER> "
)

PRO_BANNER = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To TAV-CHAZY-CLTPRO Terminal Control System\r\n"
    "FW Version: 1.10.11\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
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

    module_name = "chazy_control_discovery_under_test"
    spec = importlib.util.spec_from_file_location(module_name, COMPANION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_companion()


class _RecordingCtx:
    def __init__(self, hosts_by_open_port: dict[int, tuple[str, ...]]) -> None:
        self.source_ip = ""
        self.hosts_by_open_port = hosts_by_open_port
        self.timeout_seconds = 5.0
        self.log = logging.getLogger("test.chazy_control_discovery")
        self.emitted: list[dict] = []

    async def emit_active(self, host, response, *, probe_id=None, port=None,
                          matched_pattern=None):
        self.emitted.append({
            "host": host, "response": response, "probe_id": probe_id,
            "port": port, "matched_pattern": matched_pattern,
        })


async def _serve_once(banner: str):
    async def handle(reader, writer):
        try:
            writer.write(IAC_PREFIX)
            await writer.drain()
            await asyncio.sleep(0.05)
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
    _mod.CHAZY_TELNET_PORT = port
    ctx = _RecordingCtx({port: ("127.0.0.1",)})
    try:
        await _mod.probe(ctx)
    finally:
        server.close()
        await server.wait_closed()
    return ctx.emitted


# --- parse_welcome ---------------------------------------------------------


def test_parse_welcome_standard_control():
    model, fw = _mod.parse_welcome(CONTROL_BANNER)
    assert model == "CHAZY CONTROL"
    assert fw == "1.00.17"


def test_parse_welcome_non_chazy_returns_none():
    model, fw = _mod.parse_welcome("garbage banner\r\nno match here\r\n")
    assert model is None
    assert fw is None


# --- probe end-to-end ------------------------------------------------------


def test_probe_identifies_standard_control():
    emitted = asyncio.run(_run_probe_against(CONTROL_BANNER))
    assert len(emitted) == 1
    resp = emitted[0]["response"]
    assert resp["manufacturer"] == "TurtleAV"
    assert resp["model"] == "CHAZY CONTROL"
    assert resp["firmware"] == "1.00.17"
    assert resp["protocols"] == ["chazy_telnet"]
    assert "CHAZY CONTROL" in emitted[0]["matched_pattern"]


def test_probe_ignores_pro_banner():
    # The Pro's banner must NOT be claimed by the standard-Control companion.
    emitted = asyncio.run(_run_probe_against(PRO_BANNER))
    assert emitted == []


def test_probe_no_candidates_emits_nothing():
    ctx = _RecordingCtx({})
    asyncio.run(_mod.probe(ctx))
    assert ctx.emitted == []


def test_manufacturer_normalizes_to_alias():
    assert _mod.MANUFACTURER.strip().lower() == "turtleav"
