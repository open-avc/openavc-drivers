"""
QLab (Figure 53) — Simulator.

Emulates QLab's OSC dictionary over UDP for developing and regression-testing
the ``qlab`` driver with no Mac required. Models a small fake cue list with a
playhead and running state, and speaks the parts of the dictionary the driver
exercises:

- Transport: /go (fire + advance playhead), /stop, /hardStop, /pause,
  /resume, /panic, /reset, /playhead/next, /playhead/previous,
  /playhead/<number>, /select/<number>.
- Per-cue: /cue/<number>/start (and stop/load/preview/panic/loadAt/...),
  /cue_id/<id>/start.
- Session: /connect [passcode], /alwaysReply, /updates, /thump (the
  heartbeat, which always answers), /version.
- Feedback: /reply/<address> carrying QLab's JSON shape
  ({"workspace_id","address","status","data"}) for playhead displayName /
  number / uniqueID and runningOrPausedCues; and an unsolicited, value-less
  /update/workspace/<id>/cueList/<id>/playbackPosition push whenever the
  playhead moves (driven by /go, /reset, /playhead/*, cue starts). As on real
  QLab 5, that push carries no argument: it only signals "re-query", so the
  driver reads the new playhead from /cue/playhead/uniqueID.

Logging in, as QLab does it:

- The workspace has an OSC passcode when the device config has one (the
  simulator is handed the device's own config). /connect with it answers
  "ok:view|edit|control"; with another one, none, or the rejected-credential
  sentinel ``invalid``, "badpass". Until a login succeeds, every workspace
  message answers ``"status": "denied"``. With no passcode, nothing is denied.
- The open workspace is the configured Workspace ID (``SIMWS`` when blank). A
  message to another workspace ID answers ``"status": "error"``, which for
  /connect is QLab's "no open workspace with that ID".
- Error modes for the Simulator UI: ``qlab_restarted`` is a one-shot restart
  (the login, /alwaysReply and /updates are forgotten, so the next workspace
  message is denied), ``workspace_closed`` closes the show (every workspace
  message errors until it is cleared), ``communication_timeout`` stops
  answering (QLab quit).

Addressing: incoming messages may be rootless ("/go") or workspace-scoped
("/workspace/<id>/go"). Replies echo the address they answer prefixed with
/reply, exactly as QLab does.

TCP/SLIP transport is not simulated here (the OSC simulator base is UDP-only);
the driver's UDP path and JSON feedback parsing are fully exercised. The TCP
SLIP framing is covered by the platform's frame-parser unit tests and a final
pass against real QLab.

Driver side: ``video/qlab.py``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openavc.simulator.osc_simulator import OSCSimulator

logger = logging.getLogger(__name__)

# The workspace the simulator has open when the device config names none. It
# is also the id it reports in pushes and reply payloads.
_DEFAULT_WORKSPACE_ID = "SIMWS"
_CUELIST_ID = "CL1"
_VERSION = "5.4.5"
# QLab returns the granted permissions on a successful /connect.
_CONNECT_OK = "ok:view|edit|control"
# The rejected-credential sentinel every simulator honors, so a driver's
# refused-login path is testable against a simulator handed the device's own
# config.
_INVALID = "invalid"

# Messages QLab answers without a login: the application's own, not a
# workspace's.
_APPLICATION_METHODS = {"/version", "/alwaysReply", "/connect"}


class QLabSimulator(OSCSimulator):
    """Simulates Figure 53's QLab show-control app over OSC/UDP."""

    SIMULATOR_INFO = {
        "driver_id": "qlab",
        "name": "QLab Simulator",
        "category": "video",
        "transport": "osc",
        "default_port": 53000,
        "initial_state": {
            "qlab_version": _VERSION,
            "current_cue_id": "cue-1",
            "current_cue_number": "1",
            "current_cue_name": "Preshow Music",
            "is_running": False,
            "connected_ok": "",
        },
        "error_modes": {
            "communication_timeout": {
                "description": "QLab quit (stops answering)",
                "behavior": "no_response",
            },
            "qlab_restarted": {
                "description": "QLab restarted: it forgets the login, the "
                               "reply mode and the update subscription",
                "behavior": "qlab_restarted",
            },
            "workspace_closed": {
                "description": "The show's workspace is closed",
                "behavior": "workspace_closed",
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        cfg = config or {}
        self._workspace_id = str(cfg.get("workspace_id") or "").strip() or _DEFAULT_WORKSPACE_ID
        self._passcode = str(cfg.get("passcode") or "")
        self._logged_in = False
        # A small representative show. number is the operator-visible cue
        # number; id is the stable unique id.
        self._cues: list[dict[str, str]] = [
            {"id": "cue-1", "number": "1", "name": "Preshow Music"},
            {"id": "cue-2", "number": "2", "name": "Houselights to Half"},
            {"id": "cue-3", "number": "3", "name": "Walk-In Video"},
            {"id": "cue-4", "number": "4", "name": "Welcome Announcement"},
            {"id": "cue-5", "number": "5", "name": "Blackout"},
        ]
        self._playhead = 0
        self._running = False
        self._always_reply = False
        self._updates = False
        self._refresh_state()

    # ── Helpers ──

    def _current_cue(self) -> dict[str, str]:
        idx = max(0, min(self._playhead, len(self._cues) - 1))
        return self._cues[idx]

    def _cue_by_number(self, number: str) -> int | None:
        for i, cue in enumerate(self._cues):
            if cue["number"] == str(number):
                return i
        return None

    def _cue_index_by_id(self, cue_id: str) -> int | None:
        for i, cue in enumerate(self._cues):
            if cue["id"] == str(cue_id):
                return i
        return None

    def _refresh_state(self) -> None:
        """Mirror the current cue + running flag into sim state (for the UI)."""
        cue = self._current_cue()
        self.set_state("current_cue_id", cue["id"])
        self.set_state("current_cue_number", cue["number"])
        self.set_state("current_cue_name", cue["name"])
        self.set_state("is_running", self._running)

    @staticmethod
    def _argval(args: list[tuple[str, Any]], index: int = 0) -> Any:
        return args[index][1] if args and len(args) > index else None

    @staticmethod
    def _split(address: str) -> tuple[str | None, str]:
        """(workspace id or None when rootless, the method path)."""
        if address.startswith("/workspace/"):
            parts = address.split("/")  # ['', 'workspace', '<id>', *rest]
            return parts[2], "/" + "/".join(parts[3:])
        return None, address

    def _reply(self, address: str, data: Any = None, status: str = "ok") -> tuple[str, list[tuple[str, Any]]]:
        """Build a QLab-shaped /reply for the address that was invoked. A
        refusal ("denied", "error") carries no data, as QLab's do."""
        body: dict[str, Any] = {
            "workspace_id": self._workspace_id,
            "address": address,
            "status": status,
        }
        if status == "ok":
            body["data"] = data
        return ("/reply" + address, [("s", json.dumps(body))])

    def _playback_update(self) -> tuple[str, list[tuple[str, Any]]]:
        """Build the unsolicited playhead-moved push.

        As on real QLab 5, this push is value-less: it signals the playhead
        moved but carries no cue ID. The driver reacts by polling
        /cue/playhead/uniqueID for the new cue.
        """
        addr = (
            f"/update/workspace/{self._workspace_id}/cueList/{_CUELIST_ID}"
            f"/playbackPosition"
        )
        return (addr, [])

    def _moved_playhead(self, new_index: int) -> list[tuple[str, list]]:
        self._playhead = max(0, min(new_index, len(self._cues) - 1))
        self._refresh_state()
        return [self._playback_update()] if self._updates else []

    def _ack(self, address: str) -> list[tuple[str, list]]:
        return [self._reply(address, None)] if self._always_reply else []

    def _restart(self) -> None:
        """QLab relaunched: every client has to log in and arm again."""
        self._logged_in = False
        self._always_reply = False
        self._updates = False

    # ── Dispatch ──

    def handle_message(
        self, address: str, args: list[tuple[str, Any]]
    ) -> list[tuple[str, list[tuple[str, Any]]]] | None:
        if self.has_error_behavior("qlab_restarted"):
            self._restart()
            self.clear_error("qlab_restarted")

        ws_id, method = self._split(address)
        workspace_open = not self.has_error_behavior("workspace_closed")

        if method == "/connect":
            if ws_id not in (None, self._workspace_id) or not workspace_open:
                return [self._reply(address, status="error")]
            got = str(self._argval(args) or "")
            if self._passcode and (got != self._passcode or got == _INVALID):
                self._logged_in = False
                self.set_state("connected_ok", "badpass")
                return [self._reply(address, "badpass")]
            if got == _INVALID:
                self.set_state("connected_ok", "badpass")
                return [self._reply(address, "badpass")]
            self._logged_in = True
            self.set_state("connected_ok", _CONNECT_OK)
            return [self._reply(address, _CONNECT_OK)]

        if method not in _APPLICATION_METHODS and not (ws_id is None and method == "/thump"):
            if ws_id not in (None, self._workspace_id) or not workspace_open:
                return [self._reply(address, status="error")]
            if self._passcode and not self._logged_in:
                return [self._reply(address, status="denied")]

        out: list[tuple[str, list]] = []

        # ── Session / application ──
        if method == "/version":
            return [self._reply(address, _VERSION)]
        if method == "/thump":
            return [self._reply(address, "thump")]
        if method == "/alwaysReply":
            self._always_reply = bool(self._argval(args))
            return []
        if method == "/updates":
            self._updates = bool(self._argval(args))
            return self._ack(address)

        # ── Feedback queries (GET — answered regardless of alwaysReply) ──
        if method == "/cue/playhead/displayName":
            return [self._reply(address, self._current_cue()["name"])]
        if method == "/cue/playhead/number":
            return [self._reply(address, self._current_cue()["number"])]
        if method == "/cue/playhead/uniqueID":
            return [self._reply(address, self._current_cue()["id"])]
        if method == "/runningOrPausedCues":
            data = [self._current_cue()] if self._running else []
            return [self._reply(address, data)]

        # ── Transport ──
        if method == "/go":
            self._running = True
            out += self._moved_playhead(self._playhead + 1)
            return out + self._ack(address)
        if method in ("/stop", "/hardStop", "/panic"):
            self._running = False
            self._refresh_state()
            return self._ack(address)
        if method == "/pause":
            self._refresh_state()
            return self._ack(address)
        if method == "/resume":
            self._running = True
            self._refresh_state()
            return self._ack(address)
        if method == "/reset":
            self._running = False
            out += self._moved_playhead(0)
            return out + self._ack(address)
        if method == "/playhead/next":
            return self._moved_playhead(self._playhead + 1) + self._ack(address)
        if method == "/playhead/previous":
            return self._moved_playhead(self._playhead - 1) + self._ack(address)

        # /playhead/<number>
        m = re.match(r"^/playhead/(.+)$", method)
        if m:
            idx = self._cue_by_number(m.group(1))
            if idx is not None:
                out += self._moved_playhead(idx)
            return out + self._ack(address)

        # /select/<number> — selects without firing or moving the playhead
        if method.startswith("/select/"):
            return self._ack(address)

        # /cue/<number>/<verb...>
        m = re.match(r"^/cue/([^/]+)/(\w+)", method)
        if m:
            number, verb = m.group(1), m.group(2)
            idx = self._cue_by_number(number)
            if idx is None:
                # No cue with that number: QLab answers an error.
                return [self._reply(address, status="error")] if self._always_reply else []
            if verb == "start":
                self._running = True
                out += self._moved_playhead(idx)
            return out + self._ack(address)

        # /cue_id/<id>/<verb...>
        m = re.match(r"^/cue_id/([^/]+)/(\w+)", method)
        if m:
            cue_id, verb = m.group(1), m.group(2)
            idx = self._cue_index_by_id(cue_id)
            if idx is None:
                return [self._reply(address, status="error")] if self._always_reply else []
            if verb == "start":
                self._running = True
                out += self._moved_playhead(idx)
            return out + self._ack(address)

        # Unknown/unhandled — ack if the client asked for replies to everything.
        return self._ack(address)
