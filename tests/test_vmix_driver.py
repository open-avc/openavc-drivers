"""Integration tests for the vMix driver against its simulator.

Lives next to the driver (``video/vmix.py``): it loads the driver plus the
shipped simulator (``video/vmix_sim.py``) and exercises the real connect ->
command -> state path, plus the frame parser directly.

This drives the actual platform runtime (StateStore, EventBus, BaseDriver,
TCPTransport), so it needs ``openavc`` importable. ``vmix.py`` also imports
``openavc.*`` at module load, so the whole module skips together when the
platform isn't present: in this repo's isolated CI (stdlib + pyyaml + pydantic)
it skips cleanly; it runs in the workspace where openavc is installed alongside.

Other tests' leaked ``openavc`` stubs are handled centrally in conftest.py, so
this module imports the real platform normally.

Integration tests use ``asyncio.run()`` in a sync test, matching the
chazy/darwin driver tests (this repo has no pytest-asyncio).

One test here does not use the simulator at all, and it is the important one.
``test_every_function_name_is_real`` checks every shortcut function the driver
can put on the wire against the vendor's published function list. vMix answers
"FUNCTION OK Completed" for a function name it has never heard of, so no
simulator and no round-trip test can catch a misspelled one — six commands in
this driver were dead for months while every gate stayed green. The simulator
reproduces that forgiving behaviour on purpose, so the name check has to be a
separate, static one.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
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
    _sim_mod = _load_module("_vmix_simulator", REPO_ROOT / "video" / "vmix_sim.py")
except ModuleNotFoundError:
    pytest.skip(
        "vMix integration test requires the openavc platform "
        "(run from the workspace with openavc installed)",
        allow_module_level=True,
    )

_parse_vmix_frame = _driver_mod._parse_vmix_frame
_XML_BODY_PREFIX = _driver_mod._XML_BODY_PREFIX
VMixDriver = _driver_mod.VMixDriver
VmixSimulator = _sim_mod.VmixSimulator

DEV = "device.vmix_test."


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


def test_parse_xml_length_includes_the_trailing_crlf():
    """vMix counts the CRLF it appends in the length it announces.

    Measured on vMix 29: a 1888-byte document is announced as "XML 1890". The
    body therefore arrives with a trailer, and the parser must consume exactly
    what was promised or every frame after it is offset by two bytes.
    """
    document = b"<vmix><version>29.0.0.49</version></vmix>"
    payload = document + b"\r\n"
    buffer = f"XML {len(payload)}\r\n".encode() + payload + b"TALLY OK 12\r\n"

    msg, remaining = _parse_vmix_frame(buffer)
    assert msg[len(_XML_BODY_PREFIX):] == payload
    # The next frame must still line up.
    msg2, remaining = _parse_vmix_frame(remaining)
    assert msg2 == b"TALLY OK 12"
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


# --- The volume scale ---


def test_fader_and_amplitude_are_a_fourth_power_apart():
    """The write scale and the read scale differ, and by exactly this much.

    Measured against vMix 29: writing 50 reads back 6.25, 75 reads back
    31.64063, 90 reads back 65.60999.
    """
    to_fader = _driver_mod.amplitude_to_fader
    assert to_fader(0) == 0.0
    assert to_fader(0.390625) == 25.0
    assert to_fader(6.25) == 50.0
    assert to_fader(31.64063) == 75.0
    assert to_fader(65.60999) == 90.0
    assert to_fader(100) == 100.0


def test_fader_conversion_round_trips_with_the_simulator():
    """What the simulator reports for a fader position converts back to it."""
    to_amp = _sim_mod.fader_to_amplitude
    to_fader = _driver_mod.amplitude_to_fader
    for fader in (0, 10, 25, 50, 75, 90, 100):
        assert to_fader(to_amp(fader)) == float(fader)


def test_unit_scale_activator_values_convert_too():
    """ACTS reports the same amplitude as a 0-1 float."""
    assert _driver_mod.unit_to_fader(0.0625) == 50.0
    assert _driver_mod.unit_to_fader(1) == 100.0
    assert _driver_mod.unit_to_fader(0) == 0.0


# --- Static contract checks (no simulator: see the module docstring) ---


def test_every_function_name_is_real():
    """Every shortcut function this driver can emit exists in vMix.

    This is the check nothing else can do. vMix answers OK for any function
    name at all over TCP, so a typo reaches the panel as a button that reports
    success and does nothing.
    """
    fixture = json.loads(
        (TESTS_DIR / "fixtures" / "vmix_shortcut_functions.json").read_text()
    )
    known = set(fixture["documented"]) | set(fixture["verified_live"])

    # Fill each {placeholder} with every value the command's own parameter
    # allows, because the channel, bus, number and effect are part of the
    # function name. Reading the declared bounds rather than a fixed range is
    # the point: a command that offers a channel vMix has no function for is
    # exactly the bug this test exists to catch, and widening the bound is how
    # someone would reintroduce it.
    def allowed(command, param):
        spec = VMixDriver.DRIVER_INFO["commands"][command]["params"][param]
        if spec.get("values"):
            return [str(v) for v in spec["values"]]
        return [str(n) for n in range(int(spec["min"]), int(spec["max"]) + 1)]

    missing = []
    for command, spec in VMixDriver._COMMANDS.items():
        candidates = [spec["fn"]]
        for param in ("channel", "number", "bus", "effect"):
            token = "{" + param + "}"
            if any(token in c for c in candidates):
                values = allowed(command, param)
                candidates = [c.replace(token, v) for c in candidates for v in values]
        for name in candidates:
            if name not in known:
                missing.append((command, name))

    assert not missing, (
        "these commands would send a function vMix does not have, and vMix "
        f"would answer OK anyway: {missing}"
    )


def test_stinger_number_bound_matches_the_functions_that_exist():
    """The stinger parameter must not offer a number with no function behind it."""
    fixture = json.loads(
        (TESTS_DIR / "fixtures" / "vmix_shortcut_functions.json").read_text()
    )
    known = set(fixture["documented"]) | set(fixture["verified_live"])
    bound = VMixDriver.DRIVER_INFO["commands"]["stinger"]["params"]["number"]
    assert f"Stinger{bound['max']}" in known
    assert f"Stinger{bound['max'] + 1}" not in known


def test_every_declared_command_is_dispatchable():
    """A declared command with nowhere to go returns success and does nothing."""
    declared = set(VMixDriver.DRIVER_INFO["commands"])
    handled = set(VMixDriver._COMMANDS) | {"raw_function"}
    assert declared == handled


def test_overlay_state_is_declared_for_every_addressable_channel():
    """vMix lists sixteen overlays but addresses eight; publish exactly eight."""
    declared = {
        k for k in VMixDriver.DRIVER_INFO["state_variables"] if k.startswith("overlay.")
    }
    assert declared == {
        f"overlay.{n}" for n in range(1, _driver_mod.OVERLAY_CHANNELS + 1)
    }


def test_actions_reference_real_commands():
    """Every quick action names a command that exists."""
    commands = set(VMixDriver.DRIVER_INFO["commands"])
    for action in VMixDriver.DRIVER_INFO["actions"]:
        assert action["id"] in commands, f"action {action['id']} has no command"
        key = action.get("visible_when", {}).get("key", "")
        if key:
            prop = key.replace("device.$id.", "")
            assert prop in VMixDriver.DRIVER_INFO["state_variables"], (
                f"action {action['id']} keys off undeclared state {prop}"
            )


def test_every_input_param_is_a_picker():
    """Input arguments are pickers, not free text.

    Two forms count: the live input_list dropdown most commands use, and the
    child picker the title commands need so the field name can cascade off it.
    """
    for name, spec in VMixDriver.DRIVER_INFO["commands"].items():
        params = spec.get("params") or {}
        if "input" not in params:
            continue
        param = params["input"]
        picks_from_list = param.get("options_state") == "input_list"
        picks_a_child = (
            param.get("type") == "child_id" and param.get("child_type") == "input"
        )
        assert picks_from_list or picks_a_child, (
            f"{name}.input is free text; it should offer the live input list "
            f"or pick an input child"
        )


def test_activator_maps_only_write_declared_state():
    """Every activator this driver maps writes a state variable it declares."""
    declared = set(VMixDriver.DRIVER_INFO["state_variables"])
    for _name, (key, _kind) in VMixDriver._GLOBAL_ACTS.items():
        assert key in declared, f"activator writes undeclared state {key}"
    child = set(
        VMixDriver.DRIVER_INFO["child_entity_types"]["input"]["state_variables"]
    )
    for _name, (prop, _kind) in VMixDriver._INPUT_ACTS.items():
        assert prop in child, f"activator writes undeclared child state {prop}"


def test_discovery_probe_matches_the_greeting_vmix_sends():
    """The discovery fingerprint has to match what vMix actually says."""
    probe = VMixDriver.DRIVER_INFO["discovery"]["tcp_probe"]
    greeting = "VERSION OK 29.0.0.49"
    assert re.search(probe["expect_regex"], greeting)
    extract = probe["extract"]["firmware"]
    assert re.search(extract["regex"], greeting).group(extract["group"]) == "29.0.0.49"


def test_discovery_extracts_only_names_the_scan_will_keep():
    """A probe may extract anything; the scan keeps only these names.

    Everything else is recorded in the evidence and then dropped, so the
    device card shows a vMix with no version and nobody finds out. This
    driver shipped `firmware_version` and lost the value that way.
    """
    # openavc/discovery/result.py, _PROBE_DEVICE_INFO_KEYS.
    reserved = {
        "mac", "hostname", "manufacturer", "model", "device_name",
        "firmware", "serial_number", "category",
    }
    probe = VMixDriver.DRIVER_INFO["discovery"]["tcp_probe"]
    for field in probe.get("extract", {}):
        assert field in reserved, (
            f"discovery extract '{field}' is not a name the scan lifts onto "
            f"the device record; use one of {sorted(reserved)}"
        )


# --- Integration scenario runner ---
#
# This repo's test suite has no pytest-asyncio, so async work runs via
# asyncio.run() inside a sync test — matching the chazy/darwin driver tests.
# Each scenario gets a freshly started simulator + connected driver and tears
# both down after.


async def _run_scenario(scenario, *, subscribe_acts=True, poll_interval=0):
    """Start a sim, connect a driver, run ``await scenario(driver, state, sim)``,
    then tear everything down. Returns whatever the scenario returns."""
    sim = VmixSimulator("vmix_sim")
    await sim.start(18099)
    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
    d = VMixDriver(
        device_id="vmix_test",
        config={
            "host": "127.0.0.1",
            "port": 18099,
            "poll_interval": poll_interval,
            "subscribe_tally": True,
            "subscribe_acts": subscribe_acts,
        },
        state=state,
        events=events,
    )
    await d.connect()
    await asyncio.sleep(0.2)  # greeting + subscriptions
    try:
        return await scenario(d, state, sim)
    finally:
        try:
            await d.disconnect()
        except Exception:
            pass
        await sim.stop()


def _scenario(scenario, **kwargs):
    """Run an async scenario(driver, state, sim) to completion as a sync test."""
    asyncio.run(_run_scenario(scenario, **kwargs))


async def _settle(seconds=0.35):
    await asyncio.sleep(seconds)


# --- Integration tests ---


def test_connect():
    """Driver connects and receives initial tally."""
    async def s(d, state, sim):
        assert state.get(DEV + "connected") is True
    _scenario(s)


def test_greeting_publishes_the_version():
    """vMix announces its version on connect, before anything is asked."""
    async def s(d, state, sim):
        assert state.get(DEV + "version") == "29.0.0.49"
    _scenario(s)


def test_initial_tally():
    """After connect, tally subscription provides per-input tally."""
    async def s(d, state, sim):
        assert state.get(DEV + "input.1.tally") == 1
        assert state.get(DEV + "input.2.tally") == 2
    _scenario(s)


def test_xml_poll_reads_elements_not_attributes():
    """active, preview, version and edition are child elements of <vmix>.

    The <vmix> root carries no attributes at all, so a driver reading them as
    attributes silently leaves program and preview at zero. That is exactly
    what happened here, and only showed up with the tally subscription off.
    """
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        assert state.get(DEV + "active") == 1
        assert state.get(DEV + "preview") == 2
        assert state.get(DEV + "version") == "29.0.0.49"
        assert state.get(DEV + "edition") == "4K"
    _scenario(s, subscribe_acts=False)


def test_program_and_preview_arrive_without_any_subscription():
    """With both subscriptions off, the poll alone must still answer."""
    async def s(d, state, sim):
        sim.set_state("active", 3)
        sim.set_state("preview", 4)
        await d.poll()
        await _settle()
        assert state.get(DEV + "active") == 3
        assert state.get(DEV + "preview") == 4
    asyncio.run(_run_scenario_no_subs(s))


async def _run_scenario_no_subs(scenario):
    sim = VmixSimulator("vmix_sim")
    await sim.start(18099)
    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
    d = VMixDriver(
        device_id="vmix_test",
        config={"host": "127.0.0.1", "port": 18099, "poll_interval": 0,
                "subscribe_tally": False, "subscribe_acts": False},
        state=state, events=events,
    )
    await d.connect()
    await asyncio.sleep(0.2)
    try:
        return await scenario(d, state, sim)
    finally:
        try:
            await d.disconnect()
        except Exception:
            pass
        await sim.stop()


def test_cut():
    """Cut switches program."""
    async def s(d, state, sim):
        await d.send_command("cut", {"input": "3"})
        await _settle()
        assert sim.state.get("active") == 3
        assert state.get(DEV + "active") == 3
    _scenario(s)


def test_cut_direct_leaves_preview_alone():
    async def s(d, state, sim):
        before = sim.state.get("preview")
        await d.send_command("cut_direct", {"input": "3"})
        await _settle()
        assert sim.state.get("active") == 3
        assert sim.state.get("preview") == before
    _scenario(s)


def test_transition_sends_the_effect_as_the_function():
    """A named transition is its own function; there is no "Transition" one."""
    sent = []

    async def s(d, state, sim):
        original = d._send_function

        async def spy(function, query=""):
            sent.append((function, query))
            return await original(function, query)

        d._send_function = spy
        await d.send_command("transition", {"effect": "CubeZoom", "input": "3", "duration": 750})
        await _settle()
        assert sent[-1][0] == "CubeZoom"
        assert "Input=3" in sent[-1][1]
        assert "Duration=750" in sent[-1][1]
        assert sim.state.get("active") == 3
    _scenario(s)


def test_transition_button_and_stinger_bake_the_number_into_the_name():
    sent = []

    async def s(d, state, sim):
        async def spy(function, query=""):
            sent.append((function, query))
            return "FUNCTION OK Completed"

        d._send_function = spy
        await d.send_command("transition_button", {"number": 2})
        await d.send_command("stinger", {"number": 3, "input": "2"})
        assert sent[0] == ("Transition2", "")
        assert sent[1] == ("Stinger3", "Input=2")
    _scenario(s)


def test_preview_input():
    async def s(d, state, sim):
        await d.send_command("preview_input", {"input": "4"})
        await _settle()
        assert sim.state.get("preview") == 4
        assert state.get(DEV + "input.4.tally") == 2
    _scenario(s)


def test_overlay_round_trip():
    """The channel goes in the function name, and the state follows."""
    async def s(d, state, sim):
        await d.send_command("overlay_input", {"channel": 3, "input": "2"})
        await _settle()
        assert sim._overlays[3] == 2
        assert state.get(DEV + "overlay.3") == 2

        await d.send_command("overlay_input_off", {"channel": 3})
        await _settle()
        assert sim._overlays[3] == 0
        assert state.get(DEV + "overlay.3") == 0
    _scenario(s)


def test_overlay_command_names_carry_the_channel():
    """Guards the exact bug that shipped: OverlayInput instead of OverlayInput3."""
    sent = []

    async def s(d, state, sim):
        async def spy(function, query=""):
            sent.append(function)
            return "FUNCTION OK Completed"

        d._send_function = spy
        await d.send_command("overlay_input", {"channel": 3, "input": "2"})
        await d.send_command("overlay_input_in", {"channel": 1, "input": "2"})
        await d.send_command("overlay_input_out", {"channel": 2})
        await d.send_command("overlay_input_off", {"channel": 8})
        await d.send_command("overlay_input_zoom", {"channel": 4})
        assert sent == [
            "OverlayInput3", "OverlayInput1In", "OverlayInput2Out",
            "OverlayInput8Off", "OverlayInput4Zoom",
        ]
    _scenario(s)


def test_overlay_beyond_the_addressable_range_is_ignored():
    """The XML lists sixteen; only the eight vMix can drive get published."""
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        assert state.get(DEV + "overlay.8") == 0
        assert not state.has(DEV + "overlay.9")
        assert not state.has(DEV + "overlay.16")
    _scenario(s)


def test_recording_and_streaming():
    async def s(d, state, sim):
        await d.send_command("start_recording", {})
        await _settle()
        assert sim.state.get("recording") is True
        assert state.get(DEV + "recording") is True

        await d.send_command("stop_recording", {})
        await _settle()
        assert state.get(DEV + "recording") is False

        await d.send_command("start_streaming", {})
        await _settle()
        assert state.get(DEV + "streaming") is True
    _scenario(s)


def test_fade_to_black():
    async def s(d, state, sim):
        await d.send_command("fade_to_black", {})
        await _settle()
        assert sim.state.get("fade_to_black") is True
        assert state.get(DEV + "fade_to_black") is True
    _scenario(s)


def test_set_volume_publishes_the_fader_position_it_was_given():
    """Write 50, read 50 — not the 6.25 amplitude vMix reports internally."""
    async def s(d, state, sim):
        await d.send_command("set_volume", {"input": "2", "value": 50})
        await _settle()
        assert sim._input_audio[2]["volume"] == 50.0
        assert state.get(DEV + "input.2.volume") == 50.0
        await d.poll()
        await _settle()
        # And the poll, which reads the amplitude, agrees with the push.
        assert state.get(DEV + "input.2.volume") == 50.0
    _scenario(s)


def test_set_volume_fade_joins_its_two_values():
    """vMix wants "volume,milliseconds" in one Value and rejects anything else."""
    sent = []

    async def s(d, state, sim):
        original = d._send_function

        async def spy(function, query=""):
            sent.append((function, query))
            return await original(function, query)

        d._send_function = spy
        await d.send_command("set_volume_fade", {"input": "2", "value": 25, "duration": 1000})
        await _settle()
        assert sent[-1] == ("SetVolumeFade", "Input=2&Value=25%2C1000")
        assert sim._input_audio[2]["volume"] == 25.0
    _scenario(s)


def test_audio_mute_state():
    async def s(d, state, sim):
        await d.send_command("audio_off", {"input": "2"})
        await _settle()
        assert state.get(DEV + "input.2.muted") is True
        await d.send_command("audio_on", {"input": "2"})
        await _settle()
        assert state.get(DEV + "input.2.muted") is False
    _scenario(s)


def test_master_audio_activator_is_the_inverse_of_muted():
    """The vMix audio button reads "on" when the bus is audible."""
    async def s(d, state, sim):
        await d.send_command("master_audio_off", {})
        await _settle()
        assert sim.state.get("master_muted") is True
        assert state.get(DEV + "master_muted") is True
        await d.send_command("master_audio_on", {})
        await _settle()
        assert state.get(DEV + "master_muted") is False
    _scenario(s)


def test_master_volume_round_trip():
    async def s(d, state, sim):
        await d.send_command("set_master_volume", {"value": 75})
        await _settle()
        assert state.get(DEV + "master_volume") == 75.0
        await d.poll()
        await _settle()
        assert state.get(DEV + "master_volume") == 75.0
    _scenario(s)


def test_an_input_with_no_audio_reports_none():
    """A Colour input has no audio attributes at all, and must not fake them."""
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        # Input 1 is a Colour input in the simulated production.
        assert state.get(DEV + "input.1.audio_busses") in (None, "")
        assert state.get(DEV + "input.2.audio_busses") == "M"
        # Balance starts centred rather than at its declared minimum.
        assert state.get(DEV + "input.1.balance") == 0.0
    _scenario(s)


def test_values_are_percent_encoded():
    """A raw & truncates the value and a raw + arrives as a space."""
    sent = []

    async def s(d, state, sim):
        async def spy(function, query=""):
            sent.append(query)
            return "FUNCTION OK Completed"

        d._send_function = spy
        await d.send_command("set_text", {"input": "4", "selected_name": "Headline", "value": "A&B +C"})
        assert sent[-1] == "Input=4&SelectedName=Headline&Value=A%26B%20%2BC"
    _scenario(s)


def test_browser_navigate_survives_a_real_url():
    """A URL is mostly reserved characters; unencoded it loses everything
    after the first ampersand."""
    sent = []

    async def s(d, state, sim):
        async def spy(function, query=""):
            sent.append(query)
            return "FUNCTION OK Completed"

        d._send_function = spy
        url = "https://example.com/live?room=2&mode=full"
        await d.send_command("browser_navigate", {"input": "3", "value": url})
        from urllib.parse import parse_qs
        assert parse_qs(sent[-1])["Value"] == [url]
    _scenario(s)


def test_raw_function_passes_the_query_through_untouched():
    """The escape hatch sends what the user typed, already encoded."""
    sent = []

    async def s(d, state, sim):
        async def spy(function, query=""):
            sent.append((function, query))
            return "FUNCTION OK Completed"

        d._send_function = spy
        await d.send_command("raw_function", {"function": "SetText", "query": "Input=1&Value=Hi%20there"})
        assert sent[-1] == ("SetText", "Input=1&Value=Hi%20there")
    _scenario(s)


def test_set_text():
    async def s(d, state, sim):
        result = await d.send_command(
            "set_text", {"input": "4", "selected_name": "Headline", "value": "Hello world"}
        )
        assert "OK" in result
    _scenario(s)


def test_input_list_published():
    """The picker list is rebuilt from the production on every poll."""
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        entries = json.loads(state.get(DEV + "input_list"))
        assert entries[0] == {"value": "1", "label": "1: Colour"}
        assert len(entries) == 4
        assert state.get(DEV + "input_count") == 4
    _scenario(s)


def test_input_removed_prunes_state():
    """An input that leaves the production takes its state with it."""
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        assert state.has(DEV + "input.4.title")

        removed = sim._INPUTS.pop()
        try:
            await d.poll()
            await _settle()
            assert not state.has(DEV + "input.4.title")
            entries = json.loads(state.get(DEV + "input_list"))
            assert [e["value"] for e in entries] == ["1", "2", "3"]
        finally:
            sim._INPUTS.append(removed)
    _scenario(s)


def test_input_added_appears_in_list():
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        sim._INPUTS.append(
            {"number": 5, "title": "VT Roll", "type": "Video", "key": "new-key", "audio": True}
        )
        sim._input_audio[5] = {
            "muted": False, "volume": 100.0, "balance": 0.0, "gain_db": 0.0,
            "solo": False, "solo_pfl": False, "busses": "M",
        }
        try:
            await d.poll()
            await _settle()
            entries = json.loads(state.get(DEV + "input_list"))
            assert {"value": "5", "label": "5: VT Roll"} in entries
            assert state.get(DEV + "input.5.title") == "VT Roll"
        finally:
            sim._INPUTS.pop()
            sim._input_audio.pop(5, None)
    _scenario(s)


def test_input_key_and_short_title_are_published():
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        assert state.get(DEV + "input.2.key") == "be15de8a-8d3e-41a6-b82f-cfb54bee6f8f"
        assert state.get(DEV + "input.2.short_title") == "Camera 2"
    _scenario(s)


def test_transitions_published():
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        assert state.get(DEV + "transition.1.effect") == "Fade"
        assert state.get(DEV + "transition.1.duration") == 500
        assert state.get(DEV + "transition.4.effect") == "CubeZoom"
    _scenario(s)


def test_acts_push_updates_state_without_a_poll():
    """The activators are the whole point: state moves with no poll at all."""
    async def s(d, state, sim):
        # Nothing has been polled; drive the simulator directly so the only
        # path to the driver is the push channel.
        sim._execute_function("FadeToBlack", {})
        await _settle()
        assert state.get(DEV + "fade_to_black") is True

        sim._execute_function("OverlayInput2", {"Input": "3"})
        await _settle()
        assert state.get(DEV + "overlay.2") == 3

        sim._execute_function("StartRecording", {})
        await _settle()
        assert state.get(DEV + "recording") is True

        sim._execute_function("SetVolume", {"Input": "2", "Value": "75"})
        await _settle()
        assert state.get(DEV + "input.2.volume") == 75.0
    _scenario(s)


def test_tally_defers_to_the_activators_for_program():
    """An overlay puts its input into program too, so the first "1" in the
    tally string is not reliably the program input."""
    async def s(d, state, sim):
        sim._execute_function("PreviewInput", {"Input": "4"})
        await _settle()
        assert state.get(DEV + "preview") == 4
        # Input 1 is program; put input 3 on an overlay, which makes it live
        # as well, so the tally string now starts "1" for two inputs.
        sim._execute_function("OverlayInput1", {"Input": "3"})
        await _settle()
        assert state.get(DEV + "input.3.tally") == 1
        # Program is still input 1, which only the activator knows.
        assert state.get(DEV + "active") == 1
    _scenario(s)


def test_tally_drives_program_when_the_activators_are_off():
    async def s(d, state, sim):
        sim._execute_function("Cut", {"Input": "3"})
        await _settle()
        assert state.get(DEV + "active") == 3
    _scenario(s, subscribe_acts=False)


def test_a_rejected_command_lands_in_last_error():
    """vMix answers OK for a function it doesn't know, so an ER is always real."""
    async def s(d, state, sim):
        # Input 1 has no audio, which the simulator refuses the way vMix does.
        await d.send_command("set_volume", {"input": "1", "value": 50})
        await _settle()
        assert "SetVolume" in state.get(DEV + "last_error")
    _scenario(s)


def test_a_late_reply_cannot_answer_the_next_command():
    """A timed-out reply left queued makes every answer after it belong to the
    question before."""
    async def s(d, state, sim):
        d._cmd_response.put_nowait("FUNCTION OK Stale")
        result = await d.send_command("cut", {"input": "2"})
        assert result != "FUNCTION OK Stale"
    _scenario(s)


def test_liveness_probe_answers():
    async def s(d, state, sim):
        await asyncio.wait_for(d._liveness_probe(), timeout=3)
    _scenario(s)


def test_disconnect():
    async def s(d, state, sim):
        await d.disconnect()
        assert state.get(DEV + "connected") is False
    _scenario(s)


# --- Mixes ---


def test_mix_number_converts_to_the_zero_based_wire_argument():
    """vMix's mix 2 travels as Mix=1, and the main mix sends nothing.

    Measured on vMix 29: ActiveInput Input=1&Mix=1 moved <mix number="2">
    while Mix=0 moved the main program.
    """
    to_wire = _driver_mod.mix_to_wire
    assert to_wire(1) is None          # main mix — no argument at all
    assert to_wire("1") is None
    assert to_wire(2) == "1"
    assert to_wire(3) == "2"
    assert to_wire("") is None
    assert to_wire("not a number") is None


def test_mix_param_is_offered_only_where_it_works():
    """Two commands look like they take Mix and do not."""
    commands = VMixDriver.DRIVER_INFO["commands"]
    for name in ("cut", "fade", "transition", "stinger", "preview_input",
                 "active_input", "overlay_input", "overlay_input_in"):
        assert "mix" in commands[name]["params"], f"{name} should offer mix"
    # Hardware-measured: both moved the main mix whatever Mix was set to.
    for name in ("cut_direct", "quick_play"):
        assert "mix" not in commands[name]["params"], (
            f"{name} does not honour Mix on the device; offering it would be a "
            f"control that silently acts on the wrong mix"
        )


def test_mix_command_sends_the_converted_argument():
    sent = []

    async def s(d, state, sim):
        async def spy(function, query=""):
            sent.append((function, query))
            return "FUNCTION OK Completed"

        d._send_function = spy
        await d.send_command("cut", {"input": "2", "mix": "2"})
        await d.send_command("cut", {"input": "2", "mix": "1"})
        await d.send_command("cut", {"input": "2"})
        assert sent[0] == ("Cut", "Input=2&Mix=1")   # vMix mix 2 -> wire 1
        assert sent[1] == ("Cut", "Input=2")         # main mix -> no argument
        assert sent[2] == ("Cut", "Input=2")
    _scenario(s)


def test_mixes_appear_as_children_with_vmix_numbering():
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        assert state.get(DEV + "mix.2.active") == 0
        assert state.get(DEV + "mix.3.active") == 0
        # There is no mix.1: the main mix is the device's own active/preview.
        assert not state.has(DEV + "mix.1.active")
    _scenario(s)


def test_mix_list_picker_offers_main_plus_the_real_mixes():
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        entries = json.loads(state.get(DEV + "mix_list"))
        assert entries[0] == {"value": "1", "label": "Main"}
        assert {"value": "2", "label": "Mix 2"} in entries
        assert [e["value"] for e in entries] == ["1", "2", "3"]
    _scenario(s)


def test_switching_on_a_mix_leaves_the_main_mix_alone():
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        main_before = state.get(DEV + "active")
        await d.send_command("active_input", {"input": "3", "mix": "2"})
        await _settle()
        await d.poll()
        await _settle()
        assert state.get(DEV + "mix.2.active") == 3
        assert state.get(DEV + "active") == main_before
        assert state.get(DEV + "mix.3.active") == 0
    _scenario(s)


def test_a_mix_that_leaves_the_production_is_deregistered():
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        assert state.has(DEV + "mix.3.active")
        removed = sim._mixes.pop(3)
        try:
            await d.poll()
            await _settle()
            assert not state.has(DEV + "mix.3.active")
            assert state.has(DEV + "mix.2.active")
        finally:
            sim._mixes[3] = removed
    _scenario(s)


# --- Title text fields ---


def test_title_fields_become_state_on_the_input():
    """A title's text is readable, not just writable."""
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        # Input 4 is the Lower Third in the simulated production.
        assert state.get(DEV + "input.4.Headline") == "Welcome"
        assert state.get(DEV + "input.4.Description") == "Room 101"
    _scenario(s)


def test_a_field_named_like_a_built_in_property_does_not_clobber_it():
    """The title's own "title" field must not overwrite the input's title."""
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        assert state.get(DEV + "input.4.title") == "Lower Third"
    _scenario(s)


def test_set_text_field_picker_cascades_off_the_chosen_input():
    """Picking the title populates the field list from that title."""
    params = VMixDriver.DRIVER_INFO["commands"]["set_text"]["params"]
    assert params["input"]["type"] == "child_id"
    assert params["input"]["child_type"] == "input"
    assert params["selected_name"]["options_from"] == {
        "param": "input", "source": "child_schema",
    }
    # Free text still allowed: a GT title's "Headline.Text" is never reported.
    assert params["selected_name"]["type"] == "string"


def test_set_text_round_trip():
    async def s(d, state, sim):
        await d.send_command(
            "set_text", {"input": "4", "selected_name": "Headline", "value": "On Now"}
        )
        await _settle()
        await d.poll()
        await _settle()
        assert state.get(DEV + "input.4.Headline") == "On Now"
    _scenario(s)


def test_a_title_swapped_for_another_replaces_its_fields():
    """Field names change only when the title is swapped, and the child's
    schema can only move by re-registering."""
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        assert state.has(DEV + "input.4.Headline")

        entry = next(e for e in sim._INPUTS if e["number"] == 4)
        original = entry["texts"]
        entry["texts"] = [("Caption", "Different title")]
        try:
            await d.poll()
            await _settle()
            assert state.get(DEV + "input.4.Caption") == "Different title"
            assert not state.has(DEV + "input.4.Headline")
        finally:
            entry["texts"] = original
    _scenario(s)


def test_retyping_a_field_does_not_churn_the_child():
    """Only a NAME change may re-register; a value change must not."""
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        registered = []
        original = d.register_child

        def spy(child_type, local_id, *a, **kw):
            registered.append((child_type, local_id))
            return original(child_type, local_id, *a, **kw)

        d.register_child = spy
        entry = next(e for e in sim._INPUTS if e["number"] == 4)
        entry["texts"] = [(n, "changed") for n, _v in entry["texts"]]
        await d.poll()
        await _settle()
        assert ("input", 4) not in registered
        assert state.get(DEV + "input.4.Headline") == "changed"
    _scenario(s)


def test_an_input_with_no_title_has_no_text_variables():
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        assert not state.has(DEV + "input.1.Headline")
    _scenario(s)


def test_the_field_picker_offers_only_the_titles_fields():
    """`control` scopes both the value picker and the Set Text cascade, so a
    title must not offer its fader as somewhere to write a headline."""
    async def s(d, state, sim):
        await d.poll()
        await _settle()
        # Input 4 is the title in the simulated production.
        title_schema = d._effective_child_schema("input", 4)
        offered = {n for n, v in title_schema.items() if v.get("control")}
        assert "Headline" in offered and "Description" in offered
        assert not offered & {"muted", "volume", "balance"}

        # An ordinary audio input keeps its value-picker flags.
        audio_schema = d._effective_child_schema("input", 2)
        audio_offered = {n for n, v in audio_schema.items() if v.get("control")}
        assert {"muted", "volume", "balance"} <= audio_offered
    _scenario(s)
