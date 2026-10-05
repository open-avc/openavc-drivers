"""
OpenAVC QLab (Figure 53) driver: show control and playback over OSC.

Controls Figure 53's QLab, the macOS show-playback and show-control app used in
theatre, worship spaces, museums, themed entertainment and corporate stage
shows. An operator builds a list of cues (audio, video, fades, MIDI, light,
network) and fires them with GO; this driver puts GO / STOP / PANIC and per-cue
control on an OpenAVC panel, with live "current cue" feedback.

Protocol: OSC on QLab's port (default 53000), over UDP or over TCP framed with
SLIP. Controlling QLab over OSC is free in QLab 5 on every license tier.

    Reference: QLab v5 OSC Dictionary
    https://qlab.app/docs/v5/scripting/osc-dictionary-v5/

Addressing. A message is rootless ("/go", the front-most workspace) or
workspace-scoped ("/workspace/<id>/go"). With a Workspace ID configured every
workspace message carries it; blank, they go to the front workspace. In QLab 5
every open workspace hears the OSC port, so an explicit ID is recommended on a
Mac with several open.

Replies. QLab answers a message as "/reply" + the address sent, with one string
argument holding JSON: {"workspace_id", "address", "status", "data"}. status is
"ok", "error" (the message was invalid, or something went wrong) or "denied"
(not logged in, or the passcode lacks the permission). /alwaysReply 1 makes it
answer every message, not only the ones that return a value. Over UDP it sends
replies to port 53001 whatever the sending port, so the driver binds a listen
socket there (``listen_port``); over TCP they come back on the connection.

Feedback is polled (the playhead's unique ID, name and number, and whether
anything is running) and refreshed early on QLab's playback-position push.
/updates 1 subscribes this client to /update/... notifications; it is
application-wide and sent rootless, because QLab 5.6.1 answers the
workspace-scoped form with an error. The playback-position push carries the
new playhead cue's unique ID, which the driver takes, then re-reads the name
and number (measured on QLab 5.6.1).

Logging in. /connect [passcode] answers, as data in an "ok" reply (measured on
QLab 5.6.1): "ok:<permissions>" (for example "ok:view|edit|control"),
"badpass" for a wrong passcode, "ok:" with no permissions for no passcode on a
workspace that has one, and "error" when no workspace with that ID is open.
QLab lengthens the delay before it accepts the next /connect after every wrong
passcode, and after QLab restarts it has forgotten every login: with a
passcode set it then answers each message "denied", the heartbeat included.
So:

  * a refused passcode, or a login with no permissions, is ``auth_failed``,
    which stops the platform reconnecting until the passcode is changed: a
    wrong passcode is never sent on a timer;
  * a workspace that is not open is ``no_response`` with a sentence naming the
    workspace. It keeps retrying, because a show the operator closed comes
    back when it is reopened, and a closed workspace answers "error", not
    "badpass", so the retries cost QLab nothing;
  * "denied" logs in again on the connection the driver already has and
    re-arms /alwaysReply and /updates, which QLab also forgot. A denial
    within ``RELOGIN_INTERVAL_S`` of that is the passcode lacking the
    permission, and says so in ``last_error`` instead of logging in again.

Why Python, not YAML: logging in is a request whose answer the driver has to
act on, three ways, and the declarative ``auth:`` block speaks only prompt-driven
Telnet logins. The YAML driver could neither report a refused passcode nor log
back in, so its liveness check dropped the link on "denied" to make the
reconnect re-send the passcode, and with a wrong passcode that meant a wrong
passcode sent every 35 seconds, forever, reported as "stopped answering".

Liveness. The check sends ``{ws}/thump`` (QLab's heartbeat, which always
answers; rootless, it goes to the front workspace) every ``HEALTH_INTERVAL_S``
and counts any reply to it, "denied" and "error" included, as QLab answering:
a denial starts a login and an error means the workspace is not open, which the
driver reports itself. Only silence is a miss. Over UDP nothing else would notice QLab going away: a Mac
reboot or a QLab quit does not close a UDP socket.

Not modeled: enumerating the whole cue list into browsable per-cue entities.
Cues are fired by number or unique ID, the normal way to script QLab.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.transport.osc_codec import osc_decode_bundle, osc_encode_message
from openavc.utils.logger import get_logger

log = get_logger(__name__)

# How long a /connect waits for QLab's answer.
LOGIN_TIMEOUT_S = 5.0
# At most one login a denial starts per this many seconds. A denial inside the
# window after one means QLab accepted the login and still refuses: the
# passcode lacks the permission, not that QLab forgot the session.
RELOGIN_INTERVAL_S = 15.0

# Command name -> (address after the workspace prefix, typed OSC arguments as
# (tag, parameter) pairs). {number} and {cue_id} substitute from the params.
_COMMANDS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "go": ("/go", ()),
    "stop": ("/stop", ()),
    "hard_stop": ("/hardStop", ()),
    "pause": ("/pause", ()),
    "resume": ("/resume", ()),
    "panic": ("/panic", ()),
    "reset": ("/reset", ()),
    "playhead_next": ("/playhead/next", ()),
    "playhead_previous": ("/playhead/previous", ()),
    "start_cue": ("/cue/{number}/start", ()),
    "stop_cue": ("/cue/{number}/stop", ()),
    "load_cue": ("/cue/{number}/load", ()),
    "preview_cue": ("/cue/{number}/preview", ()),
    "panic_cue": ("/cue/{number}/panic", ()),
    "load_cue_at": ("/cue/{number}/loadAt", (("f", "seconds"),)),
    "start_cue_id": ("/cue_id/{cue_id}/start", ()),
    "select_cue": ("/select/{number}", ()),
    "set_playhead": ("/playhead/{number}", ()),
    # QLab 5's audio level addresses need a row index: row 0 is the cue's
    # master level, rows 1+ are individual channels.
    "set_cue_level": ("/cue/{number}/sliderLevel/0", (("f", "level"),)),
    "nudge_cue_level_up": ("/cue/{number}/sliderLevel/0/+", (("f", "delta"),)),
    "nudge_cue_level_down": ("/cue/{number}/sliderLevel/0/-", (("f", "delta"),)),
    "set_cue_armed": ("/cue/{number}/armed", (("i", "value"),)),
    "set_cue_color": ("/cue/{number}/colorName", (("s", "color"),)),
}

# Workspace queries whose answer is state, and the state each one feeds.
_PLAYHEAD_QUERIES: dict[str, str] = {
    "/cue/playhead/uniqueID": "current_cue_id",
    "/cue/playhead/displayName": "current_cue_name",
    "/cue/playhead/number": "current_cue_number",
}
_RUNNING_QUERY = "/runningOrPausedCues"


def _method(address: str) -> str:
    """The address a reply answers, without "/reply" or a workspace prefix."""
    if address.startswith("/reply"):
        address = address[len("/reply"):]
    if address.startswith("/workspace/"):
        parts = address.split("/")  # ['', 'workspace', '<id>', *rest]
        address = "/" + "/".join(parts[3:])
    return address


def _granted(answer: str) -> list[str]:
    """The permissions in a /connect answer: "ok:view|edit" -> [view, edit]."""
    return [p for p in answer.split(":", 1)[1].split("|") if p] if ":" in answer else []


def _osc_value(tag: str, value: Any) -> Any:
    if tag == "f":
        return float(value)
    if tag == "i":
        return int(value)
    return str(value)


class QLabDriver(BaseDriver):
    """Figure 53 QLab over OSC (UDP, or TCP with SLIP framing)."""

    HEALTH_INTERVAL_S = 15.0
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = (
        "Connected, but QLab stopped answering. Check that QLab is still open "
        "on the Mac."
    )

    DRIVER_INFO = {
        "id": "qlab",
        "name": "QLab Show Control",
        "manufacturer": "Figure 53",
        "category": "video",
        "version": "2.0.2",
        "author": "OpenAVC",
        "description": "Controls Figure 53's QLab show-control / playback "
                       "software (macOS) over OSC. GO, STOP, PANIC, "
                       "pause/resume/reset, and per-cue start/stop/load/preview "
                       "by number or unique ID, plus select, playhead, level, "
                       "arm, color, and load-at. Live feedback: current cue "
                       "id/number/name, running state, and QLab version. OSC "
                       "remote control is free in QLab 5. UDP or reliable "
                       "TCP+SLIP transport.",
        "source_url": "https://qlab.app/docs/v5/scripting/osc-dictionary-v5/",
        "tags": ["osc", "show-control", "playback", "theatrical", "cue", "qlab"],
        "verified": True,
        "simulated": True,
        "transport": "osc",
        "ports": [53000],
        # confirm on the commands that erase, delete or reset needs 0.36.0.
        "min_platform_version": "0.36.0",
        "compatible_models": [
            {
                "manufacturer": "Figure 53",
                "models": ["QLab 5"],
                "confidence": "full",
                "notes": "Validated against QLab 5.6.1 over both UDP and TCP: "
                         "login with a passcode, a wrong or missing passcode, "
                         "a workspace that is not open, QLab quitting and "
                         "restarting, GO/STOP/PANIC, cue start by number, and "
                         "live feedback (version, current cue id/number/name, "
                         "running state). OSC remote control is a free feature "
                         "in QLab 5 across all license tiers (Free, Audio, "
                         "Video, Lighting).",
            },
            {
                "manufacturer": "Figure 53",
                "models": ["QLab 4"],
                "confidence": "untested",
                "notes": "Shares the same OSC address grammar as QLab 5; "
                         "video-specific addresses differ. Not tested against "
                         "real QLab 4.",
            },
        ],

        "default_config": {
            "host": "",
            "port": 53000,
            "workspace_id": "",
            "passcode": "",
            "transport_mode": "udp",
            "poll_interval": 5,
            # QLab sends UDP replies and updates to port 53001 on the sender's
            # IP, whatever port the message came from. Unused over TCP.
            "listen_port": 53001,
            # QLab does not answer the generic OSC /info reachability probe,
            # so the pre-connect check is off; the login is the first answer.
            "verify_timeout": 0,
        },

        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "Host (Mac IP Address)",
                "description": "IP address of the Mac running QLab.",
            },
            "port": {
                "type": "integer",
                "default": 53000,
                "label": "OSC Port",
                "description": "QLab's OSC port (Workspace Settings > OSC). "
                               "Default 53000.",
            },
            "workspace_id": {
                "type": "string",
                "label": "Workspace ID",
                "description": "Unique ID of the QLab workspace to control "
                               "(Workspace Settings > OSC). Leave blank to "
                               "control the front-most workspace.",
            },
            "passcode": {
                "type": "string",
                "secret": True,
                "label": "OSC Passcode",
                "description": "Workspace OSC passcode, if one is set in QLab. "
                               "Leave blank if none.",
            },
            "transport_mode": {
                "type": "enum",
                "values": ["udp", "tcp"],
                "default": "udp",
                "label": "Transport",
                "description": "UDP (default) for fire-and-forget control, or "
                               "TCP for the most reliable feedback. QLab "
                               "accepts both on the same port.",
            },
        },

        "state_variables": {
            "qlab_version": {
                "type": "string",
                "label": "QLab Version",
                "help": "QLab application version, reported on connect from "
                        "/version.",
            },
            "connected_ok": {
                "type": "string",
                "label": "Workspace Connection",
                "help": "QLab's answer to logging in to the workspace: "
                        "'ok:' followed by the granted permissions (e.g. "
                        "'ok:view|edit|control'), or 'badpass' when the "
                        "passcode was refused.",
            },
            "current_cue_id": {
                "type": "string",
                "label": "Current Cue ID",
                "help": "Unique ID of the cue at the playhead.",
            },
            "current_cue_number": {
                "type": "string",
                "label": "Current Cue Number",
                "help": "The cue number shown to the operator for the cue at "
                        "the playhead (e.g. 12 or 5.1).",
            },
            "current_cue_name": {
                "type": "string",
                "label": "Current Cue Name",
                "help": "Display name of the cue at the playhead.",
            },
            "is_running": {
                "type": "boolean",
                "label": "Cues Running",
                "help": "True when one or more cues are currently running or "
                        "paused.",
            },
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "help": "The last message QLab refused or could not carry "
                        "out, and why.",
            },
        },

        "commands": {
            "go": {
                "label": "GO",
                "help": "Fire the cue at the playhead and advance to the next "
                        "cue.",
            },
            "stop": {
                "label": "Stop",
                "help": "Stop all running cues (with their stop fades).",
            },
            "hard_stop": {
                "label": "Hard Stop",
                "help": "Stop all running cues immediately, ignoring stop "
                        "fades.",
            },
            "pause": {"label": "Pause", "help": "Pause all running cues."},
            "resume": {"label": "Resume", "help": "Resume all paused cues."},
            "panic": {
                "label": "Panic",
                "help": "Fade everything out over a short time and then stop. "
                        "The big red button.",
            },
            "reset": {
                "label": "Reset Workspace",
                "confirm": "Stops every running cue and moves the playhead back to the top of the cue list.",
                "help": "Stop everything and move the playhead back to the top "
                        "of the cue list.",
            },
            "playhead_next": {
                "label": "Next Cue",
                "help": "Move the playhead to the next cue (does not fire it).",
            },
            "playhead_previous": {
                "label": "Previous Cue",
                "help": "Move the playhead to the previous cue (does not fire "
                        "it).",
            },
            "start_cue": {
                "label": "Start Cue",
                "help": "Start a specific cue by its number.",
                "params": {
                    "number": {
                        "type": "string",
                        "required": True,
                        "label": "Cue Number",
                        "description": "The cue number as shown in QLab (e.g. "
                                       "12 or 5.1).",
                    },
                },
            },
            "stop_cue": {
                "label": "Stop Cue",
                "help": "Stop a specific cue by its number.",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                },
            },
            "load_cue": {
                "label": "Load Cue",
                "help": "Load a cue (arm it at its start, ready to GO) without "
                        "firing it.",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                },
            },
            "preview_cue": {
                "label": "Preview Cue",
                "help": "Preview a cue (fire it in isolation, as the editor's "
                        "preview does).",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                },
            },
            "panic_cue": {
                "label": "Panic Cue",
                "help": "Fade out and stop a single cue.",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                },
            },
            "load_cue_at": {
                "label": "Load Cue At Time",
                "help": "Load a cue and pre-position it to a given time, in "
                        "seconds, from its start.",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                    "seconds": {"type": "number", "required": True,
                                "label": "Time (seconds)", "min": 0},
                },
            },
            "start_cue_id": {
                "label": "Start Cue (by ID)",
                "help": "Start a cue by its unique ID (stable even if cues are "
                        "renumbered).",
                "params": {
                    "cue_id": {"type": "string", "required": True,
                               "label": "Cue Unique ID"},
                },
            },
            "select_cue": {
                "label": "Select Cue",
                "help": "Select a cue by number (highlights it; does not "
                        "fire).",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                },
            },
            "set_playhead": {
                "label": "Set Playhead",
                "help": "Move the playhead to a specific cue number (the cue GO "
                        "will fire next).",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                },
            },
            "set_cue_level": {
                "label": "Set Cue Level",
                "help": "Set a cue's master audio level, in dB (0 = unity "
                        "gain, negative = quieter).",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                    "level": {
                        "type": "number",
                        "required": True,
                        "label": "Level (dB)",
                        "description": "Master audio level in decibels. 0 is "
                                       "unity gain; negative is quieter.",
                    },
                },
            },
            "nudge_cue_level_up": {
                "label": "Nudge Cue Level Up",
                "help": "Raise a cue's master audio level by an amount in dB.",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                    "delta": {"type": "number", "required": True,
                              "label": "Increase By (dB)"},
                },
            },
            "nudge_cue_level_down": {
                "label": "Nudge Cue Level Down",
                "help": "Lower a cue's master audio level by an amount in dB.",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                    "delta": {"type": "number", "required": True,
                              "label": "Decrease By (dB)"},
                },
            },
            "set_cue_armed": {
                "label": "Arm / Disarm Cue",
                "help": "Arm (1) or disarm (0) a cue. A disarmed cue is "
                        "skipped by GO.",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                    "value": {
                        "type": "integer",
                        "required": True,
                        "label": "Armed",
                        "values": [0, 1],
                        "description": "1 = armed, 0 = disarmed.",
                    },
                },
            },
            "set_cue_color": {
                "label": "Set Cue Color",
                "help": "Set a cue's color label.",
                "params": {
                    "number": {"type": "string", "required": True,
                               "label": "Cue Number"},
                    "color": {
                        "type": "enum",
                        "required": True,
                        "label": "Color",
                        "values": ["none", "red", "orange", "green", "blue",
                                   "purple"],
                    },
                },
            },
        },

        "quick_actions": ["go", "stop", "panic"],

        "help": {
            "overview": "Controls QLab, Figure 53's macOS show-playback and "
                        "show-control app, over OSC. Fire and stop cues, PANIC "
                        "(fade everything out and stop), pause, resume, reset, "
                        "select cues, move the playhead, and set per-cue "
                        "level, arm, and color. The current cue (id, number, "
                        "and name), whether anything is running, and the QLab "
                        "version are reported back live. Remote control over "
                        "OSC is a free feature in QLab 5, so no license is "
                        "needed to drive it.",
            "setup": "1. In QLab on the Mac: Workspace Settings > OSC. Turn on "
                     "\"Use OSC controls\" and note the port (default 53000). "
                     "Optionally set a passcode.\n"
                     "2. Enter the Mac's IP address as Host and the port "
                     "(default 53000).\n"
                     "3. Leave Workspace ID blank to control the front-most "
                     "workspace, or enter a specific workspace's unique ID to "
                     "target it (QLab shows the ID in Workspace Settings > "
                     "OSC). On a Mac running more than one workspace, "
                     "targeting an explicit ID is recommended.\n"
                     "4. If the workspace has an OSC passcode, enter it as "
                     "Passcode. Leave blank if none is set. The passcode needs "
                     "control access for GO and the other cue commands.\n"
                     "5. Transport: leave at UDP for normal use. Choose TCP for "
                     "the most reliable feedback (large cue-list replies). "
                     "QLab accepts both on the same port.",
            "connection": "If commands do nothing, confirm \"Use OSC controls\" "
                          "is enabled in QLab's Workspace Settings > OSC and "
                          "that the port matches. A wrong passcode shows as a "
                          "login failure and is not retried until the passcode "
                          "is changed here. A workspace that is not open shows "
                          "as offline until the show is opened in QLab.",
        },
    }

    def __init__(self, device_id: str, config: dict[str, Any], state, events) -> None:
        super().__init__(device_id, config, state, events)
        self._login_waiter: asyncio.Future | None = None
        self._thump_waiters: list[asyncio.Future] = []
        self._relogin_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        # Loop time of the last time a denial started a login (0 = never).
        self._relogin_at = 0.0
        # What the last login's permissions rule out, for last_error ("" =
        # nothing). poll() writes it again while it holds, because the
        # platform clears last_error after a poll that wrote nothing.
        self._permission_note = ""

    # ── Addressing ──

    def _workspace_id(self) -> str:
        return str(self.config.get("workspace_id") or "").strip()

    def _ws(self) -> str:
        """"/workspace/<id>" when a workspace is configured, else "" (rootless)."""
        ws_id = self._workspace_id()
        return f"/workspace/{ws_id}" if ws_id else ""

    async def _send(self, address: str, args: list[tuple[str, Any]] | None = None) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(osc_encode_message(address, args or []))

    # ── Sentences the device card shows ──

    def _no_workspace_message(self) -> str:
        ws_id = self._workspace_id()
        if ws_id:
            return (f"QLab is running, but no workspace with ID {ws_id} is "
                    f"open. Open the show in QLab, or copy the ID from "
                    f"Workspace Settings > OSC.")
        return "QLab is running, but no workspace is open. Open the show in QLab."

    def _no_access_message(self) -> str:
        if str(self.config.get("passcode") or ""):
            return ("QLab accepted the OSC passcode but gives it no access. Turn on "
                    "view, edit and control for it in QLab's Workspace Settings > OSC.")
        return self._badpass_message()

    def _badpass_message(self) -> str:
        if str(self.config.get("passcode") or ""):
            return ("QLab refused the OSC passcode. Enter the passcode from "
                    "QLab's Workspace Settings > OSC.")
        return ("This QLab workspace needs an OSC passcode. Enter the one from "
                "QLab's Workspace Settings > OSC.")

    # ── Connection lifecycle ──

    async def _post_connect(self) -> None:
        """Log in before the device is reported connected, so a refused
        passcode fails the attempt with its own reason."""
        await self._login()

    async def _initial_sync(self) -> None:
        await self._arm_session()

    async def _login(self) -> None:
        """Send /connect and act on QLab's answer; raise a typed fault when
        it refuses."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._login_waiter = fut
        passcode = str(self.config.get("passcode") or "")
        try:
            # With no passcode configured, /connect goes without the argument:
            # QLab then grants whatever it allows a connection with none.
            await self._send(f"{self._ws()}/connect",
                             [("s", passcode)] if passcode else [])
            reply = await asyncio.wait_for(fut, LOGIN_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise ConnectionFaultError(
                "QLab did not answer. Check that QLab is open on the Mac, that "
                "\"Use OSC controls\" is on in Workspace Settings > OSC, and "
                "that the port matches.",
                code="no_response",
            ) from exc
        finally:
            if self._login_waiter is fut:
                self._login_waiter = None

        status = str(reply.get("status") or "")
        data = reply.get("data")
        answer = data if isinstance(data, str) else ""
        if answer.startswith("ok"):
            self.set_state("connected_ok", answer)
            if ":" in answer and not _granted(answer):
                # "ok:" with nothing after it: QLab let the connection in with
                # no permissions (a locked workspace and no passcode, or a
                # passcode with every access box off), so every message after
                # it would be refused. QLab 5.6.1 answers a locked workspace
                # this way rather than with "badpass".
                raise ConnectionFaultError(self._no_access_message(), code="auth_failed")
            self._note_permissions(answer)
            return
        if answer == "badpass" or status == "badpass":
            self.set_state("connected_ok", "badpass")
            raise ConnectionFaultError(self._badpass_message(), code="auth_failed")
        if status == "error" or answer == "error":
            raise ConnectionFaultError(self._no_workspace_message(), code="no_response")
        raise ConnectionFaultError(
            f"QLab did not accept the connection ({answer or status or 'no reason given'}).",
            code="no_response",
        )

    def _note_permissions(self, answer: str) -> None:
        """Say so when the login cannot fire cues ("ok:view" or "ok:view|edit")."""
        self._permission_note = ""
        if ":" not in answer:
            return
        granted = _granted(answer)
        if "control" not in granted:
            allowed = " and ".join(granted)
            self._permission_note = (
                f"QLab allows this connection {allowed} only, so GO and the other "
                f"cue commands will be refused. Give the passcode control access "
                f"in QLab's Workspace Settings > OSC."
            )
            self.set_state(self.LAST_ERROR_PROPERTY, self._permission_note)

    async def _arm_session(self) -> None:
        """Ask for a reply to every message and for change notices, then read
        the current state. QLab forgets all of it when it restarts."""
        await self._send("/alwaysReply", [("i", 1)])
        await self._send("/updates", [("i", 1)])
        await self._send("/version")
        await self._query_state()

    async def _query_state(self) -> None:
        ws = self._ws()
        for query in _PLAYHEAD_QUERIES:
            await self._send(f"{ws}{query}")
        await self._send(f"{ws}{_RUNNING_QUERY}")

    async def poll(self) -> None:
        """The heartbeat (which also keeps QLab from dropping an idle UDP
        client) and the playhead / running state, and the permission note
        again while it holds. Replies arrive through on_data_received; silence
        is the liveness check's to judge."""
        if not self.transport or not self.transport.connected:
            return
        await self._send(f"{self._ws()}/thump")
        await self._query_state()
        if self._permission_note:
            self.set_state(self.LAST_ERROR_PROPERTY, self._permission_note)

    async def _liveness_probe(self) -> None:
        """Send the heartbeat and wait for any reply to it.

        "denied" and "error" are QLab answering too: the reply handler logs in
        again on a denial and reports a workspace that is not open, so this
        only has to know that something came back.
        """
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._thump_waiters.append(fut)
        try:
            await self._send(f"{self._ws()}/thump")
            await fut
        finally:
            if fut in self._thump_waiters:
                self._thump_waiters.remove(fut)

    async def _close_session(self) -> None:
        for task in (self._relogin_task, self._refresh_task):
            if task is not None and not task.done():
                task.cancel()
        self._relogin_task = None
        self._refresh_task = None
        waiters = list(self._thump_waiters)
        if self._login_waiter is not None:
            waiters.append(self._login_waiter)
        for fut in waiters:
            if not fut.done():
                fut.set_exception(ConnectionError("QLab connection closed"))
        self._thump_waiters.clear()
        self._login_waiter = None

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        spec = _COMMANDS.get(command)
        if spec is None:
            raise ValueError(f"Unknown command: {command}")
        template, arg_spec = spec
        address = template
        for name in ("number", "cue_id"):
            token = "{" + name + "}"
            if token in address:
                value = str(params.get(name, "")).strip()
                if not value:
                    raise ValueError(f"{command} needs a {name.replace('_', ' ')}")
                address = address.replace(token, value)
        args = [(tag, _osc_value(tag, params.get(name))) for tag, name in arg_spec]
        await self._send(f"{self._ws()}{address}", args)
        return True

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        try:
            messages = osc_decode_bundle(bytes(data))
        except Exception as exc:  # a malformed packet from the network
            log.debug(f"[{self.device_id}] Undecodable OSC packet: {exc}")
            return
        for address, args in messages:
            if address.startswith("/reply"):
                self._handle_reply(address, args)
            elif address.startswith("/update/") and address.endswith("/playbackPosition"):
                if args and args[0][0] == "s" and args[0][1]:
                    self.set_state("current_cue_id", str(args[0][1]))
                self._schedule_refresh()

    def _handle_reply(self, address: str, args: list[tuple[str, Any]]) -> None:
        payload: dict[str, Any] = {}
        if args and args[0][0] == "s":
            try:
                parsed = json.loads(args[0][1])
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                payload = parsed
        sent = address[len("/reply"):] or "/"
        method = _method(address)
        status = str(payload.get("status") or "")

        if method == "/connect":
            if self._login_waiter is not None and not self._login_waiter.done():
                self._login_waiter.set_result(payload)
            return

        if method == "/thump":
            for fut in list(self._thump_waiters):
                if not fut.done():
                    fut.set_result(payload)

        if status == "denied":
            self._on_denied(sent, method)
            return
        if status == "error":
            self._on_error(sent, method)
            return
        if status != "ok" or "data" not in payload:
            return

        data = payload["data"]
        if method == "/version":
            self.set_state("qlab_version", str(data))
        elif method in _PLAYHEAD_QUERIES and data is not None:
            self.set_state(_PLAYHEAD_QUERIES[method], str(data))
        elif method == _RUNNING_QUERY:
            self.set_state("is_running", bool(data))

    def _on_denied(self, sent: str, method: str) -> None:
        """QLab refused a message: not logged in (it restarted), or not allowed.

        The first denial logs in again. A denial within RELOGIN_INTERVAL_S of
        logging in again means QLab accepted the login and still refuses: the
        passcode lacks the permission, and another login would not change it.
        """
        if self._relogin_task is not None and not self._relogin_task.done():
            return
        is_command = not (method == "/thump" or method == "/version"
                          or method in _PLAYHEAD_QUERIES or method == _RUNNING_QUERY)
        now = asyncio.get_running_loop().time()
        if self._relogin_at and now - self._relogin_at < RELOGIN_INTERVAL_S:
            if is_command:
                self.set_state(
                    self.LAST_ERROR_PROPERTY,
                    f"QLab refused {sent}: this connection is not allowed to do "
                    f"that. Check the passcode's access in QLab's Workspace "
                    f"Settings > OSC.",
                )
            return
        self._relogin_at = now
        if is_command:
            self.set_state(
                self.LAST_ERROR_PROPERTY,
                f"QLab refused {sent} because it no longer had this controller "
                f"logged in (QLab may have restarted). Logged in again; send it "
                f"again.",
            )
        self._relogin_task = asyncio.ensure_future(self._relogin())

    async def _relogin(self) -> None:
        log.info(f"[{self.device_id}] QLab answered 'denied' (it may have "
                 f"restarted); logging in again")
        try:
            await self._login()
        except ConnectionFaultError as exc:
            self._force_disconnect(exc.fault_code, str(exc))
            return
        except (ConnectionError, OSError):
            return  # the link itself went; the platform's disconnect path has it
        try:
            await self._arm_session()
        except (ConnectionError, OSError):
            return

    def _on_error(self, sent: str, method: str) -> None:
        if method == "/thump":
            # The heartbeat goes to the configured workspace, or the front one,
            # and errors only when there is no such workspace open. Offline
            # with the reason, and reconnecting (which logs in) until the show
            # is reopened.
            self._force_disconnect("no_response", self._no_workspace_message())
            return
        if method in _PLAYHEAD_QUERIES or method == _RUNNING_QUERY or method == "/version":
            # A playhead past the end of the list answers "error"; the last
            # value stands.
            return
        self.set_state(
            self.LAST_ERROR_PROPERTY,
            f"QLab could not carry out {sent}. Check the cue number and the value.",
        )

    def _schedule_refresh(self) -> None:
        """Re-read the playhead once per burst of playback-position pushes."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.ensure_future(self._refresh())

    async def _refresh(self) -> None:
        try:
            await self._query_state()
        except (ConnectionError, OSError):
            return


DRIVER_CLASS = QLabDriver
