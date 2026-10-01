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
            self._error_modes = dict(self.SIMULATOR_INFO.get("error_modes", {}))
            self._active_errors = set()

        @property
        def state(self):
            return dict(self._state)

        def set_state(self, key, value):
            self._state[key] = value

        def get_state(self, key, default=None):
            return self._state.get(key, default)

        # Error injection, as BaseSimulator does it.
        def inject_error(self, mode):
            if mode in self._error_modes:
                self._active_errors.add(mode)

        def clear_error(self, mode):
            self._active_errors.discard(mode)

        def has_error_behavior(self, behavior):
            return any(
                self._error_modes.get(m, {}).get("behavior") == behavior
                for m in self._active_errors
            )

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
    resp = sim.handle_message("/workspace/SIMWS/connect", [("s", "")])
    # Real QLab returns the granted permissions on success, not a bare "ok".
    assert _reply_data(resp, "/reply/workspace/SIMWS/connect") == "ok:view|edit|control"


# ── Reply address echoes the invoked address (rootless and scoped) ──

def test_playhead_name_reply_rootless(sim):
    resp = sim.handle_message("/cue/playhead/displayName", [])
    assert _reply_data(resp, "/reply/cue/playhead/displayName") == "Preshow Music"


def test_playhead_number_reply_workspace_scoped(sim):
    resp = sim.handle_message("/workspace/SIMWS/cue/playhead/number", [])
    addr = "/reply/workspace/SIMWS/cue/playhead/number"
    assert _reply_data(resp, addr) == "1"


def test_playhead_uniqueid_reply(sim):
    # The driver polls /cue/playhead/uniqueID for current_cue_id, so the sim
    # must answer it.
    resp = sim.handle_message("/cue/playhead/uniqueID", [])
    assert _reply_data(resp, "/reply/cue/playhead/uniqueID") == "cue-1"


# ── Running state is a JSON array (driver coerces length -> boolean) ──

def test_running_cues_empty_until_go(sim):
    resp = sim.handle_message("/runningOrPausedCues", [])
    assert _reply_data(resp, "/reply/runningOrPausedCues") == []


def test_go_sets_running_and_pushes_the_new_playhead_cue(sim):
    sim.handle_message("/updates", [("i", 1)])
    resp = sim.handle_message("/workspace/SIMWS/go", [])
    update = "/update/workspace/SIMWS/cueList/CL1/playbackPosition"
    assert update in [a for a, _ in resp]
    # As on QLab 5.6.1, the push carries the new playhead cue's unique ID.
    assert dict(resp)[update] == [("s", "cue-2")]
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
    sim.handle_message("/updates", [("i", 1)])
    resp = sim.handle_message("/workspace/SIMWS/cue/4/start", [])
    update = "/update/workspace/SIMWS/cueList/CL1/playbackPosition"
    assert dict(resp)[update] == [("s", "cue-4")]
    num = sim.handle_message("/cue/playhead/number", [])
    assert _reply_data(num, "/reply/cue/playhead/number") == "4"


def test_start_cue_by_id_moves_playhead(sim):
    sim.handle_message("/updates", [("i", 1)])
    resp = sim.handle_message("/cue_id/cue-3/start", [])
    update = "/update/workspace/SIMWS/cueList/CL1/playbackPosition"
    assert dict(resp)[update] == [("s", "cue-3")]
    uid = sim.handle_message("/cue/playhead/uniqueID", [])
    assert _reply_data(uid, "/reply/cue/playhead/uniqueID") == "cue-3"


def test_reset_returns_to_top(sim):
    sim.handle_message("/updates", [("i", 1)])
    sim.handle_message("/cue/5/start", [])
    resp = sim.handle_message("/reset", [])
    update = "/update/workspace/SIMWS/cueList/CL1/playbackPosition"
    assert dict(resp)[update] == [("s", "cue-1")]
    uid = sim.handle_message("/cue/playhead/uniqueID", [])
    assert _reply_data(uid, "/reply/cue/playhead/uniqueID") == "cue-1"


def test_the_update_subscription_is_application_wide(sim):
    """QLab 5.6.1 refuses /workspace/<id>/updates; /updates is the form."""
    resp = sim.handle_message("/workspace/SIMWS/updates", [("i", 1)])
    assert _status(resp, "/reply/workspace/SIMWS/updates") == "error"
    resp = sim.handle_message("/go", [])
    assert all("playbackPosition" not in a for a, _ in resp)


def test_no_playback_push_until_subscribed(sim):
    # Without /updates 1, GO must not emit an unsolicited update.
    resp = sim.handle_message("/go", [])
    assert all("playbackPosition" not in a for a, _ in resp)


# ── Logging in, the open workspace, and a restart ──

def _status(responses, expect_addr):
    for addr, args in responses:
        if addr == expect_addr:
            return json.loads(args[0][1])["status"]
    raise AssertionError(f"no reply at {expect_addr} in {[a for a, _ in responses]}")


@pytest.fixture
def locked():
    """A workspace with an OSC passcode, as the device config sets it."""
    return _load_sim_class()("qlab-test", {"passcode": "5775"})


def test_a_locked_workspace_denies_until_logged_in(locked):
    resp = locked.handle_message("/workspace/SIMWS/go", [])
    assert _status(resp, "/reply/workspace/SIMWS/go") == "denied"
    ok = locked.handle_message("/workspace/SIMWS/connect", [("s", "5775")])
    assert _reply_data(ok, "/reply/workspace/SIMWS/connect") == "ok:view|edit|control"
    assert locked.state["connected_ok"] == "ok:view|edit|control"
    num = locked.handle_message("/workspace/SIMWS/cue/playhead/number", [])
    assert _reply_data(num, "/reply/workspace/SIMWS/cue/playhead/number") == "1"


def test_a_wrong_passcode_is_badpass(locked):
    for args in ([("s", "1234")], [("s", "invalid")]):
        resp = locked.handle_message("/connect", args)
        assert _reply_data(resp, "/reply/connect") == "badpass"
    # Still refused.
    resp = locked.handle_message("/cue/playhead/number", [])
    assert _status(resp, "/reply/cue/playhead/number") == "denied"


def test_no_passcode_on_a_locked_workspace_lets_in_with_no_permissions(locked):
    """QLab 5.6.1 answers "ok:" here, not "badpass", and then denies."""
    resp = locked.handle_message("/connect", [])
    assert _reply_data(resp, "/reply/connect") == "ok:"
    resp = locked.handle_message("/go", [])
    assert _status(resp, "/reply/go") == "denied"


def test_the_rootless_heartbeat_is_denied_before_a_login(locked):
    """Rootless, the heartbeat is the front workspace's (QLab 5.6.1)."""
    resp = locked.handle_message("/thump", [])
    assert _status(resp, "/reply/thump") == "denied"


def test_the_invalid_sentinel_is_refused_even_with_no_passcode(sim):
    resp = sim.handle_message("/connect", [("s", "invalid")])
    assert _reply_data(resp, "/reply/connect") == "badpass"


def test_another_workspace_id_is_an_error(sim):
    # /connect answers "ok" with "error" as its data; anything else errors.
    resp = sim.handle_message("/workspace/NOPE/connect", [])
    assert _reply_data(resp, "/reply/workspace/NOPE/connect") == "error"
    resp = sim.handle_message("/workspace/NOPE/thump", [])
    assert _status(resp, "/reply/workspace/NOPE/thump") == "error"


def test_the_configured_workspace_is_the_open_one():
    sim = _load_sim_class()("qlab-test", {"workspace_id": "1E37BBC6"})
    resp = sim.handle_message("/workspace/1E37BBC6/connect", [])
    assert _reply_data(resp, "/reply/workspace/1E37BBC6/connect") == "ok:view|edit|control"


def test_the_heartbeat_answers_without_always_reply(sim):
    resp = sim.handle_message("/workspace/SIMWS/thump", [])
    assert _reply_data(resp, "/reply/workspace/SIMWS/thump") == "thump"


def test_a_restart_forgets_the_login_the_reply_mode_and_the_updates(locked):
    locked.handle_message("/connect", [("s", "5775")])
    locked.handle_message("/alwaysReply", [("i", 1)])
    locked.handle_message("/updates", [("i", 1)])
    locked.inject_error("qlab_restarted")
    resp = locked.handle_message("/workspace/SIMWS/thump", [])
    assert _status(resp, "/reply/workspace/SIMWS/thump") == "denied"
    # One shot: logging in again restores service.
    locked.handle_message("/connect", [("s", "5775")])
    resp = locked.handle_message("/workspace/SIMWS/thump", [])
    assert _reply_data(resp, "/reply/workspace/SIMWS/thump") == "thump"
    # /alwaysReply and /updates were forgotten too.
    assert locked.handle_message("/go", []) == []


def test_a_closed_workspace_errors_until_reopened(sim):
    sim.inject_error("workspace_closed")
    resp = sim.handle_message("/workspace/SIMWS/thump", [])
    assert _status(resp, "/reply/workspace/SIMWS/thump") == "error"
    resp = sim.handle_message("/connect", [])
    assert _reply_data(resp, "/reply/connect") == "error"
    # The application itself still answers.
    assert _reply_data(sim.handle_message("/version", []), "/reply/version") == "5.4.5"
    sim.clear_error("workspace_closed")
    resp = sim.handle_message("/workspace/SIMWS/thump", [])
    assert _reply_data(resp, "/reply/workspace/SIMWS/thump") == "thump"


def test_a_cue_number_that_does_not_exist_is_an_error(sim):
    sim.handle_message("/alwaysReply", [("i", 1)])
    resp = sim.handle_message("/cue/99/start", [])
    assert _status(resp, "/reply/cue/99/start") == "error"
