"""Self-contained contract test for lea_connect's declared command effects.

A device audit confirms a command by reading back the value its ``sets``
names; with none declared, every command read "Not confirmed" beside its own
"Mute went from false to true". Each command that writes a value the amplifier
reports declares it here: on the channel (or analog input) its ``child_id``
parameter addresses, or on the amplifier itself. The output EQ bands are the
exception, because the driver keeps no EQ state to read back.

Runs with PyYAML only, like the rest of the community CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

DRIVER_PATH = (
    Path(__file__).resolve().parent.parent / "audio" / "lea_connect.avcdriver"
)
INFO = yaml.safe_load(DRIVER_PATH.read_text(encoding="utf-8"))
NO_STATE = {"set_eq_enable", "set_eq_type", "set_eq_gain", "set_eq_frequency", "set_eq_q"}


def test_version_bumped():
    assert str(INFO["version"]) == "1.1.1"
    assert str(INFO["min_platform_version"]) == "0.37.0"
    # The channel count the amp reports is saved into the Channels setting.
    assert INFO["config_schema"]["channel_count"]["learned_from"] == "num_outputs"


def test_every_command_says_what_it_sets():
    device_vars = set(INFO["state_variables"])
    for name, cdef in INFO["commands"].items():
        if name in NO_STATE:
            assert "sets" not in cdef, name
            continue
        sets = cdef.get("sets")
        assert sets, f"{name} declares no sets"
        params = cdef.get("params") or {}
        child_types = [
            p["child_type"] for p in params.values() if p.get("type") == "child_id"
        ]
        if child_types:
            declared = set(INFO["child_entity_types"][child_types[0]]["state_variables"])
        else:
            declared = device_vars
        for var, value in sets.items():
            assert var in declared, (name, var)
            if isinstance(value, str):
                assert value.startswith("{") and value.strip("{}") in params, (name, value)


def test_mute_and_level_say_exactly_what_they_set():
    commands = INFO["commands"]
    assert commands["mute_on"]["sets"] == {"mute": True}
    assert commands["mute_off"]["sets"] == {"mute": False}
    assert commands["set_fader"]["sets"] == {"fader": "{level}"}
    assert commands["set_input_sensitivity"]["sets"] == {"sensitivity": "{sensitivity}"}


# The amp's answer to the discovery probe, as a Connect 352D sends it.
PROBE_REPLY = '/amp/deviceInfo/modelID "Connect Series Model 352D"\n'


def test_the_probe_reply_names_the_model():
    probe = INFO["discovery"]["tcp_probe"]
    assert re.search(probe["expect_regex"], PROBE_REPLY)
    rule = probe["extract"]["model"]
    found = re.search(rule["regex"], PROBE_REPLY)
    assert found and found.group(rule.get("group", 1)) == "Connect Series Model 352D"


def test_the_line_voltage_flag_reads_as_the_warning_it_is():
    # The amp reports lineWarning false while its supply is healthy, so the
    # value is a warning, not an "OK": a label saying OK read "No" at 121 V.
    var = INFO["state_variables"]["psu_line_warning"]
    assert var["label"] == "AC Line Voltage Warning"
    assert "OK" not in var["label"]



# ── The simulator ──
#
# The simulator's one handler answers the whole protocol. It runs here the way
# the auto-generated simulator runs it (exec, with match/state/config/respond),
# against a state built the way the simulator builds one: a type default for
# every declared amplifier value, then the simulator's initial_state.

SIM = INFO["simulator"]
HANDLER = SIM["command_handlers"][0]["handler"]
TYPE_DEFAULTS = {"string": "", "enum": "", "integer": 0, "number": 0.0, "boolean": False}


def _sim_state():
    state = {key: TYPE_DEFAULTS.get(var.get("type"), "")
             for key, var in INFO["state_variables"].items()}
    state.update(SIM.get("initial_state") or {})
    return state


def _ask(line, state, channels=2):
    out = []
    ns = {"match": re.match(r"(?s:.+)", line), "state": state,
          "config": {"channel_count": str(channels)}, "respond": out.append, "re": re}
    exec(HANDLER, ns, ns)
    return "".join(out)


def _rules():
    """(url, state key, response regex) for every amplifier and channel 1 rule."""
    for rule in INFO["responses"]:
        pattern = rule["match"]
        url = pattern.lstrip("^").split(" ")[0]
        if not url.startswith("/amp/"):
            continue
        for key in (rule.get("set") or {}):
            yield url, key, pattern
        for child in rule.get("child_set") or []:
            for prop in child["state"]:
                cid = "1"
                yield (url.replace("(\\d+)", cid),
                       f"{child['type']}.{cid}.{prop}", pattern)


def test_the_simulator_pushes_what_the_simulator_ui_changes():
    # Without push_state a change made in the Simulator UI never reaches the
    # driver, and the card says "Poll-only (no push)".
    assert SIM["push_state"] is True


def test_the_simulator_keeps_its_values_under_the_drivers_state_keys():
    state = _sim_state()
    assert _ask("subscribe /amp/deviceInfo/numOutputs", state) == (
        "/amp/deviceInfo/numOutputs 2.0\n")
    assert state["num_outputs"] == 2
    assert state["model_id"] == "Connect Series Model 352D"
    assert state["psu_power_ok"] is True
    assert state["psu_line_warning"] is False
    assert state["channel.1.name"] == "Channel 1"
    assert state["channel.2.enable"] is True
    # A value the Simulator UI sets is what the next query reads.
    state["psu_fault"] = True
    assert _ask("get /amp/powerSupply/fault", state) == "/amp/powerSupply/fault true\n"
    state["channel.1.mute"] = True
    assert _ask("get /amp/channels/1/output/mute", state) == (
        "/amp/channels/1/output/mute true\n")
    # A set the driver sends lands on the same key.
    _ask("set /amp/channels/2/output/fader -12.5", state)
    assert state["channel.2.fader"] == -12.5
    assert not any(key.startswith("/amp/") for key in state)


def test_every_response_rule_reads_back_what_the_simulator_holds():
    # Each value the driver parses comes from the state key the driver puts it
    # in, so the card, the controls and the answers are the same value.
    samples = {str: "Probe Text", bool: True, int: 7, float: -7.5}
    state = _sim_state()
    _ask("get /amp/deviceInfo/modelID", state)        # seeds the channels
    for url, key, pattern in _rules():
        sample = samples[type(state[key])]
        state[key] = sample
        line = _ask("get " + url, state).rstrip("\n")
        found = re.match(pattern, line)
        assert found, (url, key, line)
        got = found.group(found.re.groups)
        assert got.strip('"') in (str(sample), str(sample).lower(), f"{sample:.1f}"
                                  if isinstance(sample, (int, float)) else ""), (url, key, line)


def test_initial_state_covers_every_amplifier_value():
    # The amplifier-level defaults are in initial_state, so the card shows them
    # before a driver connects; each must match what the amp answers.
    typed = _sim_state()
    for url, key, _ in _rules():
        if url.startswith("/amp/channels/") or url.startswith("/amp/inputs/"):
            continue
        assert _ask("get " + url, typed) == _ask("get " + url, {}), (url, key)
