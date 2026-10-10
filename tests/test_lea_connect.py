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

from pathlib import Path

import yaml

DRIVER_PATH = (
    Path(__file__).resolve().parent.parent / "audio" / "lea_connect.avcdriver"
)
INFO = yaml.safe_load(DRIVER_PATH.read_text(encoding="utf-8"))
NO_STATE = {"set_eq_enable", "set_eq_type", "set_eq_gain", "set_eq_frequency", "set_eq_q"}


def test_version_bumped():
    assert str(INFO["version"]) == "1.1.0"
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
