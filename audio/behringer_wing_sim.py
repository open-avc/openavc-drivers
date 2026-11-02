"""
Behringer WING — Simulator.

Emulates the WING's OSC server (UDP 2223) as "WING Remote Protocols" V3.1.0
describes it, for the parts the ``behringer_wing`` driver uses:

- Reads: an address with no argument answers in the console's own form, a
  float as (text, raw 0..1, value), an integer as (text, raw, value), a
  string alone.
- Writes: an address with an argument sets it and answers nothing, to anyone
  ("WING does not echo data sent over UDP by the client application"). An
  integer 0..1 parameter sent -1 toggles. A float takes a float or a string.
- ``/*s``: subscribes the sender to change events (triplets, like a read),
  for 10 seconds; one subscriber, the last to ask. A change made by the
  subscriber itself is not sent back to it (whether the console does is not
  stated, so the simulator takes the strict reading: the driver must read
  its own writes back).
- ``/?``: the console's identity, ``WING,<ip>,<name>,<model>,<serial>,<fw>``.
- ``WING?`` (raw, not OSC): the same identity, as the console answers on its
  native port 2222. The simulator answers it on its one port so a discovery
  probe can be checked against it.
- Scenes (``/$ctl/lib/$actionidx`` + ``/$ctl/lib/$action`` GO / GOTAG /
  GONEXT / GOPREV) over a five-scene show, the USB player and recorder
  actions, and the OSC Remote Lock (``$ctl/OSC/ronly``, set from the
  Simulator UI), which makes every write a silent no-op.

State keys are the console's addresses without the leading slash
(``ch/1/fdr``), so a change from the Simulator UI is pushed to the
subscriber under the right address.

Driver side: ``audio/behringer_wing.py``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from openavc.simulator.osc_simulator import OSCSimulator
from openavc.transport.osc_codec import osc_encode_message

logger = logging.getLogger(__name__)

_SUBSCRIPTION_S = 10.0
_IP = "127.0.0.1"

# root -> roster size, and which leaves each strip of that root has
_ROOTS = {"ch": 40, "aux": 8, "bus": 16, "main": 4, "mtx": 8, "dca": 16, "mgrp": 8}
_STRIP = ("name", "mute", "fdr", "pan", "$solo")
_LEAVES = {"dca": ("name", "mute", "fdr", "$solo"), "mgrp": ("name", "mute")}

# Parameter kinds by leaf name: (kind, min, max). Everything else is a string.
_FLOATS = {"fdr": (-144.0, 10.0), "pan": (-100.0, 100.0), "lvl": (-144.0, 10.0),
           "$time": (0.0, 35999.0)}
_INTS = {"mute": (0, 1), "$solo": (0, 1), "on": (0, 1), "ronly": (0, 1), "lock": (0, 1),
         "solo": (0, 1), "repeat": (0, 1), "$actidx": (0, 16384), "$activeid": (0, 128),
         "$actionidx": (0, 16384)}

_SHOW = "I:SHOW1"
_SCENES = ["Preshow", "Welcome", "Worship", "Message", "Postlude"]
_SONGS = [("Walk In", "House Band"), ("Offertory", "House Band"), ("Postlude", "Organ")]
_NAMES = {"ch/1/name": "Lead Vox", "ch/2/name": "Pastor", "ch/3/name": "Keys",
          "ch/4/name": "Guitar", "main/1/name": "Main LR", "dca/1/name": "Vocals",
          "dca/2/name": "Band", "mgrp/1/name": "Band", "bus/1/name": "Stream"}


def _initial_state() -> dict[str, Any]:
    state: dict[str, Any] = {}
    for root, count in _ROOTS.items():
        for n in range(1, count + 1):
            for leaf in _LEAVES.get(root, _STRIP):
                key = f"{root}/{n}/{leaf}"
                if leaf == "name":
                    state[key] = _NAMES.get(key, "")
                elif leaf == "fdr":
                    unity = (root == "ch" and n <= 8) or root in ("main", "dca", "bus")
                    state[key] = 0.0 if unity else -144.0
                elif leaf == "pan":
                    state[key] = 0.0
                else:
                    state[key] = 0
    state.update({
        "console_name": "WING-SIM",
        "model": "ngc-full",
        "serial": "S2400000001",
        "firmware": "3.1.0-0-g1a2b3c4d:release",
        "$ctl/OSC/ronly": 0,
        "$ctl/lib/$actidx": 1,
        "$ctl/lib/$actionidx": 0,
        "$ctl/lib/$active": f"{_SHOW}/{_SCENES[0]}.snap",
        "$ctl/lib/$activeid": 1,
        "$ctl/lib/$actshow": _SHOW,
        "$stat/solo": 0,
        "$stat/lock": 1,
        "$stat/A/stat": "OK",
        "$stat/B/stat": "-",
        "$stat/C/stat": "-",
        "play/$actstate": "STOP",
        "play/$song": _SONGS[0][0],
        "play/$artist": _SONGS[0][1],
        "play/repeat": 0,
        "rec/$actstate": "STOP",
        "rec/$actfile": "",
        "rec/$time": 0.0,
    })
    return state


def _kind(key: str) -> tuple[str, float, float]:
    leaf = key.rsplit("/", 1)[-1]
    if leaf in _FLOATS:
        return ("f", *_FLOATS[leaf])
    if leaf in _INTS:
        return ("i", *_INTS[leaf])
    return ("s", 0.0, 0.0)


def _fader_raw(db: float) -> float:
    """Roughly where a fader level sits on the console's 0..1 scale (0.75 is
    0 dB and 1.0 is +10, as the document's examples show; below -10 dB the
    real curve is not given, so this is a straight line to 0)."""
    if db <= -144.0:
        return 0.0
    if db >= -10.0:
        return 0.75 + db * 0.025
    return max(0.0, 0.5 * (db + 144.0) / 134.0)


class BehringerWingSimulator(OSCSimulator):
    """Simulates a Behringer WING console's OSC server."""

    SIMULATOR_INFO = {
        "driver_id": "behringer_wing",
        "name": "Behringer WING Simulator",
        "category": "audio",
        "transport": "osc",
        "default_port": 2223,
        "initial_state": _initial_state(),
        "controls": [
            {"type": "group", "label": "Console", "controls": [
                {"type": "toggle", "key": "$ctl/OSC/ronly", "label": "OSC Remote Lock"},
                {"type": "toggle", "key": "$stat/lock", "label": "Clock Locked"},
                {"type": "select", "key": "$stat/A/stat", "label": "AES50 A",
                 "options": ["-", "OK", "ERR", "UPD"]},
                {"type": "indicator", "key": "$ctl/lib/$active", "label": "Scene"},
            ]},
            {"type": "group", "label": "Channels 1-4", "controls": [
                {"type": "toggle", "key": "ch/1/mute", "label": "Ch 1 Mute"},
                {"type": "slider", "key": "ch/1/fdr", "label": "Ch 1 Fader",
                 "min": -144, "max": 10, "step": 0.5, "unit": "dB"},
                {"type": "toggle", "key": "ch/2/mute", "label": "Ch 2 Mute"},
                {"type": "slider", "key": "ch/2/fdr", "label": "Ch 2 Fader",
                 "min": -144, "max": 10, "step": 0.5, "unit": "dB"},
                {"type": "toggle", "key": "ch/3/mute", "label": "Ch 3 Mute"},
                {"type": "toggle", "key": "ch/4/mute", "label": "Ch 4 Mute"},
            ]},
            {"type": "group", "label": "Main and DCAs", "controls": [
                {"type": "toggle", "key": "main/1/mute", "label": "Main 1 Mute"},
                {"type": "slider", "key": "main/1/fdr", "label": "Main 1 Fader",
                 "min": -144, "max": 10, "step": 0.5, "unit": "dB"},
                {"type": "toggle", "key": "dca/1/mute", "label": "DCA 1 Mute"},
                {"type": "toggle", "key": "mgrp/1/mute", "label": "Mute Group 1"},
            ]},
            {"type": "group", "label": "USB", "controls": [
                {"type": "select", "key": "play/$actstate", "label": "Player",
                 "options": ["STOP", "PLAY", "PAUSE"]},
                {"type": "select", "key": "rec/$actstate", "label": "Recorder",
                 "options": ["STOP", "REC", "PAUSE"]},
            ]},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        self._wing_subscriber: tuple[str, int] | None = None
        self._wing_subscribed_until = 0.0
        # The client whose write is being applied, so its own change is not
        # sent back to it.
        self._wing_writer: tuple[str, int] | None = None
        self._wing_peer: tuple[str, int] | None = None
        self._wing_recording = 0

    # ── Transport ──

    async def dispatch_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if data.rstrip(b"\x00") == b"WING?":
            self.send_datagram(self._identity().encode(), addr)
            return
        self._wing_peer = addr
        try:
            await super().dispatch_datagram(data, addr)
        finally:
            self._wing_peer = None

    def set_state(self, key: str, value: Any) -> None:
        old = self._state.get(key)
        super().set_state(key, value)
        if old != value:
            self._wing_notify(key)

    def _wing_notify(self, key: str) -> None:
        if self._wing_subscriber is None or time.monotonic() > self._wing_subscribed_until:
            return
        if self._wing_subscriber == self._wing_writer:
            return
        if not self._is_parameter(key):
            return
        # push_datagram is OpenAVC 0.37.0 and later; send_datagram before it.
        push = getattr(self, "push_datagram", None) or self.send_datagram
        push(osc_encode_message("/" + key, self._reply_args(key)), self._wing_subscriber)

    # ── Values ──

    def _identity(self) -> str:
        s = self._state
        return (f"WING,{_IP},{s.get('console_name')},{s.get('model')},"
                f"{s.get('serial')},{s.get('firmware')}")

    def _is_parameter(self, key: str) -> bool:
        return "/" in key

    def _reply_args(self, key: str) -> list[tuple[str, Any]]:
        tag, lo, hi = _kind(key)
        value = self._state.get(key)
        if tag == "f":
            number = float(value or 0.0)
            if key.endswith("/fdr") or key.endswith("/lvl"):
                text = "-oo" if number <= -144.0 else f"{number:.1f}"
                raw = _fader_raw(number)
            else:
                text = f"{number:.1f}"
                raw = (number - lo) / (hi - lo) if hi > lo else 0.0
            return [("s", text), ("f", raw), ("f", number)]
        if tag == "i":
            # A Simulator UI toggle stores a real boolean.
            number = int(value or 0)
            raw = number / hi if hi else 0.0
            return [("s", str(number)), ("f", raw), ("i", number)]
        return [("s", "" if value is None else str(value))]

    def _coerce(self, key: str, arg: tuple[str, Any]) -> Any:
        tag, lo, hi = _kind(key)
        _arg_tag, raw = arg
        if tag == "s":
            return str(raw)
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        if tag == "i":
            if int(number) == -1 and (lo, hi) == (0, 1):
                return 1 - int(self._state.get(key) or 0)
            return int(max(lo, min(hi, round(number))))
        return max(lo, min(hi, number))

    # ── Dispatch ──

    def handle_message(
        self, address: str, args: list[tuple[str, Any]]
    ) -> list[tuple[str, list[tuple[str, Any]]]] | None:
        if address == "/?":
            return [("/?", [("s", self._identity())])]
        if address == "/*s":
            self._wing_subscriber = self._wing_peer
            self._wing_subscribed_until = time.monotonic() + _SUBSCRIPTION_S
            return None
        key = address.lstrip("/")
        if not args:
            if key in self._state and self._is_parameter(key):
                return [(address, self._reply_args(key))]
            return None
        self._write(key, args[0])
        return None

    def _write(self, key: str, arg: tuple[str, Any]) -> None:
        if int(self._state.get("$ctl/OSC/ronly") or 0):
            return
        self._wing_writer = self._wing_peer
        try:
            if key == "$ctl/lib/$action":
                self._scene_action(str(arg[1]).upper())
            elif key == "play/$action":
                self._player_action(str(arg[1]).upper())
            elif key == "rec/$action":
                self._recorder_action(str(arg[1]).upper())
            elif key in self._state and self._is_parameter(key):
                value = self._coerce(key, arg)
                if value is not None:
                    self.set_state(key, value)
            elif self._is_send(key):
                pass   # sends are accepted and not modeled
        finally:
            self._wing_writer = None

    @staticmethod
    def _is_send(key: str) -> bool:
        parts = key.split("/")
        return len(parts) == 5 and parts[0] in ("ch", "aux") and parts[2] == "send"

    def _load_scene(self, index: int) -> None:
        index = max(1, min(len(_SCENES), index))
        self.set_state("$ctl/lib/$actidx", index)
        self.set_state("$ctl/lib/$activeid", index)
        self.set_state("$ctl/lib/$active", f"{_SHOW}/{_SCENES[index - 1]}.snap")

    def _scene_action(self, action: str) -> None:
        current = int(self._state.get("$ctl/lib/$actidx") or 0)
        chosen = int(self._state.get("$ctl/lib/$actionidx") or 0)
        if action in ("GO", "GOTAG"):
            # Scene n carries tag n in this show, so GOTAG and GO agree.
            self._load_scene(chosen or current)
        elif action == "GONEXT":
            self._load_scene(current + 1)
        elif action == "GOPREV":
            self._load_scene(current - 1)

    def _player_action(self, action: str) -> None:
        if action in ("PLAY", "PAUSE", "STOP"):
            self.set_state("play/$actstate", action)
        elif action in ("NEXT", "PREV"):
            names = [song for song, _artist in _SONGS]
            current = str(self._state.get("play/$song"))
            i = names.index(current) if current in names else 0
            i = (i + (1 if action == "NEXT" else -1)) % len(_SONGS)
            self.set_state("play/$song", _SONGS[i][0])
            self.set_state("play/$artist", _SONGS[i][1])

    def _recorder_action(self, action: str) -> None:
        if action in ("REC", "PAUSE", "STOP"):
            if action == "REC" and not self._state.get("rec/$actfile"):
                self._recorder_action("NEWFILE")
            self.set_state("rec/$actstate", action)
        elif action == "NEWFILE":
            self._wing_recording += 1
            self.set_state("rec/$actfile", f"R_{self._wing_recording:03d}.wav")
            self.set_state("rec/$time", 0.0)
