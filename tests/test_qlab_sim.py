"""Unit tests for the QLab simulator (``video/qlab_sim.py``).

Loads the simulator directly, stubbing the ``openavc.simulator.osc_simulator`` base it
imports, so the community repo's test suite stays self-contained (no openavc
install needed — mirrors test_chazy_control_sim.py).

These assert the simulator emits QLab's wire shape that the ``qlab`` driver
parses: ``/reply/<the address invoked>`` carrying a JSON string with a
``data`` field, and an unsolicited
``/update/workspace/<id>/cueList/<id>/playbackPosition`` push when the playhead
moves. Both rootless and ``/workspace/<id>/...`` addressing are accepted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SIM_PATH = REPO_ROOT / "video" / "qlab_sim.py"


def _install_simulator_stub() -> None:
    """Minimal stand-in for openavc.simulator.osc_simulator.OSCSimulator covering the
    parts of BaseSimulator the QLab simulator relies on (state)."""
    if "openavc.simulator.osc_simulator" in sys.modules:
        return
    pkg = ModuleType("openavc.simulator")
    pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("openavc.simulator", pkg)
    mod = ModuleType("openavc.simulator.osc_simulator")

    class _OSCSimulator:
        SIMULATOR_INFO: dict = {}

        def __init__(self, device_id, config=None):
            self.device_id = device_id
            self.config = config or {}
            self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

        @property
        def state(self):
            return dict(self._state)

        def set_state(self, key, value):
            self._state[key] = value

        def get_state(self, key, default=None):
            return self._state.get(key, default)

    mod.OSCSimulator = _OSCSimulator
    sys.modules["openavc.simulator.osc_simulator"] = mod


def _load_sim_class():
    _install_simulator_stub()
    spec = importlib.util.spec_from_file_location("qlab_sim", SIM_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["qlab_sim"] = module
    spec.loader.exec_module(module)
    return module.QLabSimulator


@pytest.fixture
def sim():
    return _load_sim_class()("qlab-test")


def _reply_data(responses, expect_addr):
    """Find the reply whose address == expect_addr; return its JSON 'data'."""
    for addr, args in responses:
        if addr == expect_addr:
            assert args and args[0][0] == "s", "reply arg must be an OSC string"
            payload = json.loads(args[0][1])
            assert {"workspace_id", "address", "status", "data"} <= payload.keys()
            return payload["data"]
    raise AssertionError(f"no reply at {expect_addr} in {[a for a, _ in responses]}")


# ── Identity / session ──

def test_version_reply_rootless(sim):
    resp = sim.handle_message("/version", [])
    assert _reply_data(resp, "/reply/version") == "5.4.5"


def test_connect_reply_ok(sim):
    resp = sim.handle_message("/workspace/ABC/connect", [("s", "")])
    # Real QLab returns the granted permissions on success, not a bare "ok".
    assert _reply_data(resp, "/reply/workspace/ABC/connect") == "ok:view|edit|control"


# ── Reply address echoes the invoked address (rootless and scoped) ──

def test_playhead_name_reply_rootless(sim):
    resp = sim.handle_message("/cue/playhead/displayName", [])
    assert _reply_data(resp, "/reply/cue/playhead/displayName") == "Preshow Music"


def test_playhead_number_reply_workspace_scoped(sim):
    resp = sim.handle_message("/workspace/ABC/cue/playhead/number", [])
    addr = "/reply/workspace/ABC/cue/playhead/number"
    assert _reply_data(resp, addr) == "1"


def test_playhead_uniqueid_reply(sim):
    # The driver polls /cue/playhead/uniqueID for current_cue_id (QLab 5's
    # playback-position push is value-less), so the sim must answer it.
    resp = sim.handle_message("/cue/playhead/uniqueID", [])
    assert _reply_data(resp, "/reply/cue/playhead/uniqueID") == "cue-1"


# ── Running state is a JSON array (driver coerces length -> boolean) ──

def test_running_cues_empty_until_go(sim):
    resp = sim.handle_message("/runningOrPausedCues", [])
    assert _reply_data(resp, "/reply/runningOrPausedCues") == []


def test_go_sets_running_and_pushes_valueless_playback_position(sim):
    sim.handle_message("/workspace/ABC/updates", [("i", 1)])
    resp = sim.handle_message("/workspace/ABC/go", [])
    update = "/update/workspace/SIMWS/cueList/CL1/playbackPosition"
    assert update in [a for a, _ in resp]
    # As on real QLab 5, the push is value-less — it signals "re-query" only.
    assert dict(resp)[update] == []
    # The driver reads the new playhead via /cue/playhead/uniqueID; GO advanced 1->2.
    uid = sim.handle_message("/cue/playhead/uniqueID", [])
    assert _reply_data(uid, "/reply/cue/playhead/uniqueID") == "cue-2"
    # Now something is running.
    run = sim.handle_message("/runningOrPausedCues", [])
    assert _reply_data(run, "/reply/runningOrPausedCues") != []


def test_go_advances_playhead_name(sim):
    sim.handle_message("/go", [])
    resp = sim.handle_message("/cue/playhead/displayName", [])
    assert _reply_data(resp, "/reply/cue/playhead/displayName") == "Houselights to Half"


# ── Cue targeting by number and by unique id ──

def test_start_cue_by_number_moves_playhead(sim):
    sim.handle_message("/workspace/ABC/updates", [("i", 1)])
    resp = sim.handle_message("/workspace/ABC/cue/4/start", [])
    update = "/update/workspace/SIMWS/cueList/CL1/playbackPosition"
    assert dict(resp)[update] == []  # value-less push
    num = sim.handle_message("/cue/playhead/number", [])
    assert _reply_data(num, "/reply/cue/playhead/number") == "4"


def test_start_cue_by_id_moves_playhead(sim):
    sim.handle_message("/updates", [("i", 1)])
    resp = sim.handle_message("/cue_id/cue-3/start", [])
    update = "/update/workspace/SIMWS/cueList/CL1/playbackPosition"
    assert dict(resp)[update] == []  # value-less push
    uid = sim.handle_message("/cue/playhead/uniqueID", [])
    assert _reply_data(uid, "/reply/cue/playhead/uniqueID") == "cue-3"


def test_reset_returns_to_top(sim):
    sim.handle_message("/updates", [("i", 1)])
    sim.handle_message("/cue/5/start", [])
    resp = sim.handle_message("/reset", [])
    update = "/update/workspace/SIMWS/cueList/CL1/playbackPosition"
    assert dict(resp)[update] == []  # value-less push
    uid = sim.handle_message("/cue/playhead/uniqueID", [])
    assert _reply_data(uid, "/reply/cue/playhead/uniqueID") == "cue-1"


def test_no_playback_push_until_subscribed(sim):
    # Without /updates 1, GO must not emit an unsolicited update.
    resp = sim.handle_message("/go", [])
    assert all("playbackPosition" not in a for a, _ in resp)
