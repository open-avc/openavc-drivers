"""Integration tests for the vMix driver against its TCP simulator.

Lives next to the driver (``video/vmix.py``): it loads the driver plus a
standalone vMix TCP simulator (``tests/vmix_simulator.py``) and exercises the
real connect -> command -> state path, plus the frame parser directly.

This drives the actual platform runtime (StateStore, EventBus, BaseDriver,
TCPTransport), so it needs ``openavc`` importable. ``vmix.py`` also imports
``server.*`` at module load, so the whole module skips together when the
platform isn't present: in this repo's isolated CI (stdlib + pyyaml + pydantic)
it skips cleanly; it runs in the workspace where openavc is installed alongside.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent

_platform_cache: dict = {}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _platform() -> dict:
    """The real platform, vMix driver, and simulator, loaded on first use.

    Loaded lazily (not at import) so test collection never fails: this is a
    real-runtime integration test and only runs where ``openavc`` is installed
    (the workspace) — in this repo's isolated CI it skips, one test at a time.

    The sibling driver tests (chazy/darwin) install *partial* ``server`` /
    ``simulator`` stubs in sys.modules at import time and never remove them.
    Those shadow the real platform modules this test needs (they leave, e.g., a
    stub ``server.transport`` with no ``__path__`` that hides
    ``server.transport.frame_parsers``). Purge any such stub first so the real,
    editable-installed package imports cleanly. The other tests captured what
    they need from their stubs at their own import time, so dropping the stubs
    here doesn't affect them.
    """
    if _platform_cache:
        return _platform_cache
    for name in list(sys.modules):
        if name.split(".", 1)[0] in ("server", "simulator"):
            del sys.modules[name]
    try:
        from server.core.event_bus import EventBus
        from server.core.state_store import StateStore
    except ModuleNotFoundError:
        pytest.skip("vMix integration test requires the openavc platform")
    driver_mod = _load_module("_vmix_driver", REPO_ROOT / "video" / "vmix.py")
    sim_mod = _load_module("_vmix_simulator", TESTS_DIR / "vmix_simulator.py")
    _platform_cache.update(
        EventBus=EventBus,
        StateStore=StateStore,
        VMixDriver=driver_mod.VMixDriver,
        parse_frame=driver_mod._parse_vmix_frame,
        xml_prefix=driver_mod._XML_BODY_PREFIX,
        VMixSimulator=sim_mod.VMixSimulator,
    )
    return _platform_cache


# --- Frame parser unit tests ---


def test_parse_normal_crlf():
    """Normal CRLF-delimited message."""
    parse = _platform()["parse_frame"]
    msg, remaining = parse(b"FUNCTION OK\r\n")
    assert msg == b"FUNCTION OK"
    assert remaining == b""


def test_parse_incomplete():
    """Incomplete message (no CRLF) returns None."""
    parse = _platform()["parse_frame"]
    msg, remaining = parse(b"FUNCTION OK")
    assert msg is None
    assert remaining == b"FUNCTION OK"


def test_parse_multiple_messages():
    """Multiple CRLF messages in one buffer."""
    parse = _platform()["parse_frame"]
    buffer = b"FUNCTION OK\r\nTALLY OK 1200\r\n"
    msg1, remaining = parse(buffer)
    assert msg1 == b"FUNCTION OK"
    msg2, remaining = parse(remaining)
    assert msg2 == b"TALLY OK 1200"
    assert remaining == b""


def test_parse_xml_response():
    """XML response with length-prefixed body."""
    p = _platform()
    parse, xml_prefix = p["parse_frame"], p["xml_prefix"]
    xml_body = b"<vmix><recording>True</recording></vmix>"
    header = f"XML {len(xml_body)}\r\n".encode()
    buffer = header + xml_body

    msg, remaining = parse(buffer)
    assert msg is not None
    assert msg.startswith(xml_prefix)
    body = msg[len(xml_prefix):]
    assert body == xml_body
    assert remaining == b""


def test_parse_incomplete_xml():
    """Incomplete XML body — parser waits for more data."""
    parse = _platform()["parse_frame"]
    xml_body = b"<vmix><recording>True</recording></vmix>"
    header = f"XML {len(xml_body)}\r\n".encode()
    # Send only half the body
    buffer = header + xml_body[:10]

    msg, remaining = parse(buffer)
    assert msg is None
    assert remaining == buffer


def test_parse_invalid_xml_length():
    """Non-numeric XML length treated as normal message."""
    parse = _platform()["parse_frame"]
    buffer = b"XML notanumber\r\n"
    msg, remaining = parse(buffer)
    assert msg == b"XML notanumber"
    assert remaining == b""


def test_parse_mixed_messages():
    """Mix of normal and XML messages in one buffer."""
    p = _platform()
    parse, xml_prefix = p["parse_frame"], p["xml_prefix"]
    xml_body = b"<vmix/>"
    buffer = b"TALLY OK 12\r\n" + f"XML {len(xml_body)}\r\n".encode() + xml_body + b"FUNCTION OK\r\n"

    msg1, remaining = parse(buffer)
    assert msg1 == b"TALLY OK 12"

    msg2, remaining = parse(remaining)
    assert msg2.startswith(xml_prefix)
    assert msg2[len(xml_prefix):] == xml_body

    msg3, remaining = parse(remaining)
    assert msg3 == b"FUNCTION OK"
    assert remaining == b""


# --- Integration scenario runner ---
#
# This repo's test suite has no pytest-asyncio (CI installs only stdlib +
# pyyaml + pydantic + pytest), so async work runs via asyncio.run() inside a
# sync test — matching the chazy/darwin driver tests. Each scenario gets a
# freshly started simulator + connected driver and tears both down after.


async def _run_scenario(p, scenario):
    """Start a sim, connect a driver, run ``await scenario(driver, state, sim)``,
    then tear everything down. Returns whatever the scenario returns."""
    sim = p["VMixSimulator"](port=18099)
    await sim.start()
    state = p["StateStore"]()
    events = p["EventBus"]()
    state.set_event_bus(events)
    d = p["VMixDriver"](
        device_id="vmix_test",
        config={
            "host": "127.0.0.1",
            "port": 18099,
            "poll_interval": 0,
            "subscribe_tally": True,
            "subscribe_acts": False,
        },
        state=state,
        events=events,
    )
    await d.connect()
    await asyncio.sleep(0.1)  # Let tally subscription arrive
    try:
        return await scenario(d, state, sim)
    finally:
        try:
            await d.disconnect()
        except Exception:
            pass
        await sim.stop()


def _scenario(scenario):
    """Run an async scenario(driver, state, sim) to completion as a sync test."""
    p = _platform()  # skips here (sync context) when openavc isn't installed
    asyncio.run(_run_scenario(p, scenario))


# --- Integration tests ---


def test_connect():
    """Driver connects and receives initial tally."""
    async def s(d, state, sim):
        assert state.get("device.vmix_test.connected") is True
    _scenario(s)


def test_initial_tally():
    """After connect, tally subscription provides active/preview."""
    async def s(d, state, sim):
        # Simulator starts with active=1, preview=2
        assert state.get("device.vmix_test.active") == 1
        assert state.get("device.vmix_test.preview") == 2
    _scenario(s)


def test_cut():
    """Cut swaps active and preview."""
    async def s(d, state, sim):
        await d.send_command("cut")
        await asyncio.sleep(0.15)
        # After cut: active becomes 2, preview becomes 1
        assert state.get("device.vmix_test.active") == 2
        assert state.get("device.vmix_test.preview") == 1
    _scenario(s)


def test_fade():
    """Fade swaps active and preview."""
    async def s(d, state, sim):
        await d.send_command("fade")
        await asyncio.sleep(0.15)
        assert state.get("device.vmix_test.active") == 2
        assert state.get("device.vmix_test.preview") == 1
    _scenario(s)


def test_preview_input():
    """Preview input changes preview."""
    async def s(d, state, sim):
        await d.send_command("preview_input", {"input": "3"})
        await asyncio.sleep(0.15)
        assert state.get("device.vmix_test.preview") == 3
    _scenario(s)


def test_tally_subscription():
    """Tally updates push after input change."""
    async def s(d, state, sim):
        await d.send_command("cut_direct", {"input": "3"})
        await asyncio.sleep(0.15)
        assert state.get("device.vmix_test.active") == 3
        assert state.get("device.vmix_test.tally.3") == 1
    _scenario(s)


def test_recording():
    """Start and stop recording via XML poll."""
    async def s(d, state, sim):
        await d.send_command("start_recording")
        await asyncio.sleep(0.1)
        await d.poll()
        await asyncio.sleep(0.2)
        assert state.get("device.vmix_test.recording") is True

        await d.send_command("stop_recording")
        await asyncio.sleep(0.1)
        await d.poll()
        await asyncio.sleep(0.2)
        assert state.get("device.vmix_test.recording") is False
    _scenario(s)


def test_streaming():
    """Start and stop streaming."""
    async def s(d, state, sim):
        await d.send_command("start_streaming")
        await asyncio.sleep(0.1)
        await d.poll()
        await asyncio.sleep(0.2)
        assert state.get("device.vmix_test.streaming") is True

        await d.send_command("stop_streaming")
        await asyncio.sleep(0.1)
        await d.poll()
        await asyncio.sleep(0.2)
        assert state.get("device.vmix_test.streaming") is False
    _scenario(s)


def test_set_volume():
    """Set volume on an input."""
    async def s(d, state, sim):
        result = await d.send_command("set_volume", {"input": "1", "value": 50})
        assert result == "FUNCTION OK"
        # Volume update is in simulator state, verify via XML poll
        await d.poll()
        await asyncio.sleep(0.2)
        # The XML poll doesn't include volume in our simple XML, but the command succeeded
        assert sim.inputs[0]["volume"] == 50
    _scenario(s)


def test_overlay():
    """Overlay input in/out."""
    async def s(d, state, sim):
        await d.send_command("overlay_input_in", {"input": "3", "value": 1})
        await asyncio.sleep(0.1)
        assert sim.overlays["1"] == 3

        await d.send_command("overlay_input_off", {"value": 1})
        await asyncio.sleep(0.1)
        assert sim.overlays["1"] == 0
    _scenario(s)


def test_xml_poll():
    """XML poll retrieves full state."""
    async def s(d, state, sim):
        await d.poll()
        await asyncio.sleep(0.3)
        assert state.get("device.vmix_test.version") == "29.0.0.1"
        assert state.get("device.vmix_test.input_count") == 4
        assert state.get("device.vmix_test.input.1.title") == "Camera 1"
        assert state.get("device.vmix_test.input.4.type") == "Video"
    _scenario(s)


def test_set_text():
    """SetText command sends correctly."""
    async def s(d, state, sim):
        result = await d.send_command("set_text", {
            "input": "1",
            "selectedName": "Title",
            "value": "Hello World",
        })
        assert result == "FUNCTION OK"
    _scenario(s)


def test_raw_function():
    """raw_function sends arbitrary vMix function."""
    async def s(d, state, sim):
        result = await d.send_command("raw_function", {
            "function": "PreviewInput",
            "query": "Input=4",
        })
        assert result == "FUNCTION OK"
        await asyncio.sleep(0.15)
        assert state.get("device.vmix_test.preview") == 4
    _scenario(s)


def test_disconnect():
    """Disconnect cleans up state."""
    async def s(d, state, sim):
        await d.disconnect()
        assert state.get("device.vmix_test.connected") is False
    _scenario(s)


def test_fade_to_black():
    """Fade to black toggle."""
    async def s(d, state, sim):
        await d.send_command("fade_to_black")
        await asyncio.sleep(0.1)
        await d.poll()
        await asyncio.sleep(0.2)
        assert state.get("device.vmix_test.fadeToBlack") is True
    _scenario(s)
