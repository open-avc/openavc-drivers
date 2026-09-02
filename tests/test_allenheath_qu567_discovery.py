"""Tests for the Allen & Heath Qu-5/6/7 discovery companion.

The companion identifies a console by an ABSENCE, so the case that matters most
is the one it must NOT claim. Every Allen & Heath console on TCP 51325 running
the current NRPN protocol answers a Get for the LR master mute, the SQ family
included -- and the Qu's parameter map is a subset of the SQ's, so nothing a Qu
has identifies it positively. The discriminator is that an SQ also answers for
Ip40 (``00 27``) and a Qu-5/6/7 is silent there.

A declarative ``tcp_probe:`` can only express the positive half, which would
identify every SQ on the network as a Qu-5/6/7 with full confidence -- worse
than the hint-only "possible" it replaced. Hence a Python companion, and hence
these tests: the SQ case is the whole point, so it is exercised against a
loopback console that answers the wider SQ address map.

Loads ``audio/allenheath_qu567_discovery.py`` directly with a stubbed
``openavc.discovery.companion`` so it runs without a real ``openavc`` install.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPANION_PATH = REPO_ROOT / "audio" / "allenheath_qu567_discovery.py"

CH = 0xB0


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

    name = "allenheath_qu567_discovery_under_test"
    spec = importlib.util.spec_from_file_location(name, COMPANION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_companion()


class _RecordingCtx:
    def __init__(self, hosts_by_open_port: dict[int, tuple[str, ...]]) -> None:
        self.source_ip = ""
        self.hosts_by_open_port = hosts_by_open_port
        self.timeout_seconds = 5.0
        self.log = logging.getLogger("test.allenheath_qu567_discovery")
        self.emitted: list[dict] = []

    async def emit_active(self, host, response, *, probe_id=None, port=None,
                          matched_pattern=None):
        self.emitted.append({
            "host": host, "response": response, "probe_id": probe_id,
            "port": port, "matched_pattern": matched_pattern,
        })


def _reply(msb: int, lsb: int) -> bytes:
    return bytes([CH, 0x63, msb, CH, 0x62, lsb, CH, 0x06, 0x00, CH, 0x26, 0x00])


async def _serve(answers_lsb) -> tuple[asyncio.AbstractServer, int]:
    """A console that answers a Get only for the addresses ``answers_lsb`` says.

    ``answers_lsb(msb, lsb) -> bool``. Silence for anything else, which is what
    a real console does for a parameter it does not have.
    """
    async def handle(reader, writer):
        try:
            while True:
                data = await asyncio.wait_for(reader.read(64), timeout=2.0)
                if not data:
                    break
                i = 0
                while i + 9 <= len(data):
                    if data[i + 1] == 0x63 and data[i + 7] == 0x60:
                        msb, lsb = data[i + 2], data[i + 5]
                        if answers_lsb(msb, lsb):
                            writer.write(_reply(msb, lsb))
                            await writer.drain()
                        i += 9
                    else:
                        i += 1
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError,
                OSError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# A Qu-5/6/7: 32 mono inputs, ST1/ST2/USB, no Ip33-48.
def _qu_roster(msb: int, lsb: int) -> bool:
    if msb != 0x00:
        return False
    return lsb <= 0x20 or lsb in (0x22, 0x24) or 0x3C <= lsb <= 0x55 or lsb == 0x57


# An SQ: 48 inputs, so Ip40 (0x27) answers.
def _sq_roster(msb: int, lsb: int) -> bool:
    return msb == 0x00 and lsb <= 0x57


def test_a_qu_console_is_identified():
    async def main():
        server, port = await _serve(_qu_roster)
        _mod.QU_PORT = port
        ctx = _RecordingCtx({port: ("127.0.0.1",)})
        try:
            await _mod.probe(ctx)
        finally:
            server.close()
            await server.wait_closed()
        assert len(ctx.emitted) == 1, ctx.emitted
        hit = ctx.emitted[0]
        assert hit["host"] == "127.0.0.1"
        assert hit["port"] == port
        assert hit["response"]["sq_only_address_silent"] is True
        assert "Ip40 silent" in hit["matched_pattern"]
    _run(main())


def test_an_sq_console_is_not_claimed():
    """The regression this companion exists to prevent. An SQ answers the same
    LR-mute Get, so a positive-only fingerprint would identify it as a Qu with
    full confidence — a wrong answer stated firmly."""
    async def main():
        server, port = await _serve(_sq_roster)
        _mod.QU_PORT = port
        ctx = _RecordingCtx({port: ("127.0.0.1",)})
        try:
            await _mod.probe(ctx)
        finally:
            server.close()
            await server.wait_closed()
        assert ctx.emitted == [], "an SQ must not be identified as a Qu-5/6/7"
    _run(main())


def test_a_console_that_answers_nothing_is_not_claimed():
    """An original Qu-16 ignores the NRPN Get entirely (it answers only its own
    SysEx All-Call), as does anything else that happens to hold 51325."""
    async def main():
        server, port = await _serve(lambda msb, lsb: False)
        _mod.QU_PORT = port
        ctx = _RecordingCtx({port: ("127.0.0.1",)})
        try:
            await _mod.probe(ctx)
        finally:
            server.close()
            await server.wait_closed()
        assert ctx.emitted == []
    _run(main())


def test_nothing_listening_is_handled_quietly():
    async def main():
        # Bind and immediately close, so the port is almost certainly dead.
        server, port = await _serve(_qu_roster)
        server.close()
        await server.wait_closed()
        _mod.QU_PORT = port
        ctx = _RecordingCtx({port: ("127.0.0.1",)})
        await _mod.probe(ctx)
        assert ctx.emitted == []
    _run(main())


def test_no_hosts_on_the_port_does_no_work():
    async def main():
        ctx = _RecordingCtx({})
        await _mod.probe(ctx)
        assert ctx.emitted == []
    _run(main())


def test_a_reply_is_matched_by_its_echoed_parameter_number():
    """Replies carry no tag, so the companion matches on the address echoed
    back. Answering a different question must not count."""
    wanted = _mod.COMMON_ADDR
    assert _mod._reply_for(_reply(*wanted), *wanted) is not None
    assert _mod._reply_for(_reply(0x00, 0x01), *wanted) is None
    assert _mod._reply_for(b"", *wanted) is None
    assert _mod._reply_for(b"\x00" * 40, *wanted) is None


def test_the_two_probe_addresses_are_the_documented_ones():
    assert _mod.COMMON_ADDR == (0x00, 0x44)      # LR master mute
    assert _mod.SQ_ONLY_ADDR == (0x00, 0x27)     # Ip40 — SQ only
