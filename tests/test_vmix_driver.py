"""Integration tests for the vMix driver against its TCP simulator.

Lives next to the driver (``video/vmix.py``): it loads the driver plus a
standalone vMix TCP simulator (``tests/vmix_simulator.py``) and exercises the
real connect -> command -> state path, plus the frame parser directly.

This drives the actual platform runtime (StateStore, EventBus, BaseDriver,
TCPTransport), so it needs ``openavc`` importable. ``vmix.py`` also imports
``openavc.*`` at module load, so the whole module skips together when the
platform isn't present: in this repo's isolated CI (stdlib + pyyaml + pydantic)
it skips cleanly; it runs in the workspace where openavc is installed alongside.

Other tests' leaked ``openavc`` stubs are handled centrally in conftest.py, so
this module imports the real platform normally.

Integration tests use ``asyncio.run()`` in a sync test, matching the
chazy/darwin driver tests (this repo has no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    from openavc.core.event_bus import EventBus
    from openavc.core.state_store import StateStore
    # vmix.py imports openavc.* at module load, so this also requires the platform.
    _driver_mod = _load_module("_vmix_driver", REPO_ROOT / "video" / "vmix.py")
    _sim_mod = _load_module("_vmix_simulator", TESTS_DIR / "vmix_simulator.py")
except ModuleNotFoundError:
    pytest.skip(
        "vMix integration test requires the openavc platform "
        "(run from the workspace with openavc installed)",
        allow_module_level=True,
    )

_parse_vmix_frame = _driver_mod._parse_vmix_frame
_XML_BODY_PREFIX = _driver_mod._XML_BODY_PREFIX
VMixDriver = _driver_mod.VMixDriver
VMixSimulator = _sim_mod.VMixSimulator


# --- Frame parser unit tests ---


def test_parse_normal_crlf():
    """Normal CRLF-delimited message."""
    msg, remaining = _parse_vmix_frame(b"FUNCTION OK\r\n")
    assert msg == b"FUNCTION OK"
    assert remaining == b""


def test_parse_incomplete():
    """Incomplete message (no CRLF) returns None."""
    msg, remaining = _parse_vmix_frame(b"FUNCTION OK")
    assert msg is None
    assert remaining == b"FUNCTION OK"


def test_parse_multiple_messages():
    """Multiple CRLF messages in one buffer."""
    buffer = b"FUNCTION OK\r\nTALLY OK 1200\r\n"
    msg1, remaining = _parse_vmix_frame(buffer)
    assert msg1 == b"FUNCTION OK"
    msg2, remaining = _parse_vmix_frame(remaining)
    assert msg2 == b"TALLY OK 1200"
    assert remaining == b""


def test_parse_xml_response():
    """XML response with length-prefixed body."""
    xml_body = b"<vmix><recording>True</recording></vmix>"
    header = f"XML {len(xml_body)}\r\n".encode()
    buffer = header + xml_body

    msg, remaining = _parse_vmix_frame(buffer)
    assert msg is not None
    assert msg.startswith(_XML_BODY_PREFIX)
    body = msg[len(_XML_BODY_PREFIX):]
    assert body == xml_body
    assert remaining == b""


def test_parse_incomplete_xml():
    """Incomplete XML body — parser waits for more data."""
    xml_body = b"<vmix><recording>True</recording></vmix>"
    header = f"XML {len(xml_body)}\r\n".encode()
    # Send only half the body
    buffer = header + xml_body[:10]

    msg, remaining = _parse_vmix_frame(buffer)
    assert msg is None
    assert remaining == buffer


def test_parse_invalid_xml_length():
    """Non-numeric XML length treated as normal message."""
    buffer = b"XML notanumber\r\n"
    msg, remaining = _parse_vmix_frame(buffer)
    assert msg == b"XML notanumber"
    assert remaining == b""


def test_parse_mixed_messages():
    """Mix of normal and XML messages in one buffer."""
    xml_body = b"<vmix/>"
    buffer = b"TALLY OK 12\r\n" + f"XML {len(xml_body)}\r\n".encode() + xml_body + b"FUNCTION OK\r\n"

    msg1, remaining = _parse_vmix_frame(buffer)
    assert msg1 == b"TALLY OK 12"

    msg2, remaining = _parse_vmix_frame(remaining)
    assert msg2.startswith(_XML_BODY_PREFIX)
    assert msg2[len(_XML_BODY_PREFIX):] == xml_body

    msg3, remaining = _parse_vmix_frame(remaining)
    assert msg3 == b"FUNCTION OK"
    assert remaining == b""


# --- Integration scenario runner ---
#
# This repo's test suite has no pytest-asyncio, so async work runs via
# asyncio.run() inside a sync test — matching the chazy/darwin driver tests.
# Each scenario gets a freshly started simulator + connected driver and tears
# both down after.


async def _run_scenario(scenario):
    """Start a sim, connect a driver, run ``await scenario(driver, state, sim)``,
    then tear everything down. Returns whatever the scenario returns."""
    sim = VMixSimulator(port=18099)
    await sim.start()
    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
    d = VMixDriver(
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
    asyncio.run(_run_scenario(scenario))


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
        assert state.get("device.vmix_test.input.3.tally") == 1
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


# --- Input list (options_state picker feed) + pruning ---


def test_input_list_published():
    """The XML poll publishes input_list as a JSON {value,label} list."""
    import json

    async def s(d, state, sim):
        await d.poll()
        await asyncio.sleep(0.3)
        raw = state.get("device.vmix_test.input_list")
        assert isinstance(raw, str) and raw
        options = json.loads(raw)
        assert [o["value"] for o in options] == ["1", "2", "3", "4"]
        assert options[0]["label"] == "1: Camera 1"
        assert options[2]["label"] == "3: Slides"
    _scenario(s)


def test_input_removed_prunes_state():
    """An input removed from the production leaves the picker AND its
    per-input state keys are deleted (not left stale)."""
    import json

    async def s(d, state, sim):
        await d.poll()
        await asyncio.sleep(0.3)
        assert state.get("device.vmix_test.input.4.title") == "Video Clip"

        # Operator deletes input 4 in vMix
        sim.inputs = [i for i in sim.inputs if i["number"] != "4"]
        await d.poll()
        await asyncio.sleep(0.3)

        options = json.loads(state.get("device.vmix_test.input_list"))
        assert [o["value"] for o in options] == ["1", "2", "3"]
        assert state.get("device.vmix_test.input_count") == 3
        # Deregistering the child clears every key under it, including the
        # platform-managed online/label pair.
        for prop in ("title", "type", "state", "muted", "loop",
                     "position", "duration", "tally", "online", "label"):
            key = f"device.vmix_test.input.4.{prop}"
            assert state.get(key) is None
        assert 4 not in d.list_children("input")
    _scenario(s)


def test_input_added_appears_in_list():
    """An input added mid-show appears in the picker on the next poll."""
    import json

    async def s(d, state, sim):
        sim.inputs.append({
            "number": "5", "title": "Late Guest", "type": "Capture",
            "state": "Running", "muted": False, "loop": False,
            "position": 0, "duration": 0, "volume": 100,
        })
        await d.poll()
        await asyncio.sleep(0.3)
        options = json.loads(state.get("device.vmix_test.input_list"))
        assert {"value": "5", "label": "5: Late Guest"} in options
    _scenario(s)


# --- Quick actions + picker metadata integrity (static) ---


def test_actions_reference_real_commands():
    """Every declared quick action promotes an existing command and uses
    only state vars the driver declares in its visible_when keys."""
    info = VMixDriver.DRIVER_INFO
    commands = info["commands"]
    state_vars = info["state_variables"]
    actions = info["actions"]
    assert actions, "driver declares a quick-action strip"
    for entry in actions:
        assert entry["id"] in commands, entry["id"]
        assert entry["kind"] == "command"
        vw = entry.get("visible_when")
        if vw:
            key = vw["key"]
            assert key.startswith("device.$id.")
            assert key.split("device.$id.")[1] in state_vars, key


def test_every_input_param_has_options_state():
    """Every command param named 'input' offers the live input picker."""
    info = VMixDriver.DRIVER_INFO
    missing = [
        cmd_id
        for cmd_id, cmd in info["commands"].items()
        if "input" in cmd.get("params", {})
        and cmd["params"]["input"].get("options_state") != "input_list"
    ]
    assert missing == [], f"input params without options_state: {missing}"
    assert "input_list" in info["state_variables"]
