"""
ETC Paradigm — Simulator.

Serves the Paradigm Serial Access Protocol (PSAP 6.0.0) the way a Paradigm
Processor Mk2 does with its PSAP Input Handler on: one ASCII line per command
or request, ended by the End of Message Char (CR unless the device config's
``end_of_message`` says otherwise), answered with the same ending.

It listens on TCP and UDP on the same port number. A device set to UDP talks
to it directly, and a device set to RS-232 is simulated over TCP (the
simulator has no serial port; simulation connects a serial device over TCP),
which is also how a serial line through a bridge arrives.

The site: every object listed in the device config's Objects table, plus a
small demonstration site (channels, a group, presets, a sequence, two spaces,
a wall, a macro and an override). An object listed without a space is put in
"Main Floor", as a real processor reports the space of every object.

What it answers, as the PSAP 6.0.0 guide gives it: the control commands for
channels, groups, presets, sequences, spaces, walls, macros and overrides,
which answer nothing, and each status request, answered in the guide's own
form (the group answer with a comma after the level, "Macro" and "Wall"
capitalized). "?" and "help" answer the list of commands.

Not modelled, because the documents do not say: what a request for a name
the processor does not have answers (this answers "error invalid", the error
the 6.1.0 release notes quote for a status request, or nothing with the
"Answer error invalid" toggle off), what an unknown command
answers (the same), fade timing (levels change at once), and how presets
share channels (activating an LTP preset makes the space's other LTP presets
inactive, and a channel or group level changed in a space makes its active
LTP presets altered). PSAP Triggers are modelled as one on every object: while the
"PSAP Triggers" toggle is on, every change is sent to each TCP connection as
its status line. Not over UDP: where a processor sends a trigger over UDP is
set in LightDesigner, and the document does not say.

The "Station Preset" buttons act as a button station would: the Nth preset
(the table's first, then the demonstration site's) is activated.

Driver side: ``lighting/etc_paradigm.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from openavc.simulator.base import BaseSimulator
from openavc.simulator.datagram_server import DatagramServerMixin
from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

END_OF_MESSAGE = {"cr": "\r", "crlf": "\r\n", "lf": "\n"}
DEFAULT_SPACE = "Main Floor"
KINDS = {"chan": "channel", "grp": "group", "pst": "preset", "seq": "sequence",
         "spc": "space", "wall": "wall", "macro": "macro", "ovr": "override"}
ROW_KINDS = set(KINDS.values())
SPACED = {"channel", "group", "preset", "sequence", "wall"}

DEMO_SITE: list[tuple[str, str, str]] = [
    ("channel", "Downlights", "Main Floor"),
    ("channel", "Wall Wash", "Main Floor"),
    ("channel", "Stage Wash", "Ballroom"),
    ("group", "House Lights", "Ballroom"),
    ("preset", "Lobby On", "Main Floor"),
    ("preset", "Lobby Evening", "Main Floor"),
    ("preset", "Ballroom Full", "Ballroom"),
    ("sequence", "Color Chase", "Ballroom"),
    ("space", "Main Floor", ""),
    ("space", "Ballroom", ""),
    ("wall", "Divider", "Ballroom"),
    ("macro", "Shutdown", ""),
    ("override", "Cleaning", ""),
]

HELP_LINES = [
    "chan int:level channame[, spacename][, fadetime]",
    "chan ras:amount channame[, spacename][, fadetime]",
    "chan low:amount channame[, spacename][, fadetime]",
    "chan tog channame[, spacename][, fadetime]",
    "chan min:level channame[, spacename]",
    "chan max:level channame[, spacename]",
    "chan get channame[, spacename]",
    "grp int:level grpname[, spacename][, fadetime]",
    "grp tog grpname[, spacename][, fadetime]",
    "grp get grpname[, spacename]",
    "pst act[:priority] presetname[, spacename][, fadetime]",
    "pst dact presetname[, spacename][, fadetime]",
    "pst rec presetname[, spacename]",
    "pst get presetname[, spacename]",
    "seq start[:priority] sequencename[, spacename]",
    "seq get sequencename[, spacename]",
    "spc off spacename[, fadetime]",
    "wall open wallname[, spacename]",
    "wall get wallname[, spacename]",
    "macro on macroname",
    "macro get macroname",
    "ovr enab overridename",
    "ovr get overridename",
]

_CMD_RE = re.compile(r"^([A-Za-z]+)\s+([A-Za-z]+)(?::([^\s]+))?(?:\s+(.*))?$")


def _amount(text: str | None) -> int | None:
    """"50%" or "128" as a 0-255 level."""
    if not text:
        return None
    text = text.strip()
    try:
        if text.endswith("%"):
            return max(0, min(255, round(float(text[:-1]) * 255 / 100)))
        return max(0, min(255, int(float(text))))
    except ValueError:
        return None


class EtcParadigmSimulator(DatagramServerMixin, TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "etc_paradigm",
        "name": "ETC Paradigm Simulator",
        "category": "lighting",
        "transport": "udp",
        "default_port": 4703,
        "delimiter": "\r",
        "initial_state": {
            "triggers": True,
            "station": 0,
            "last_command": "",
            "objects": 0,
            "answer_unknown": True,
        },
        "controls": [
            {"type": "toggle", "key": "triggers", "label": "PSAP Triggers (TCP / serial)"},
            {"type": "presets", "key": "station", "label": "Station Preset", "count": 6},
            {"type": "indicator", "key": "last_command", "label": "Last command"},
            {"type": "indicator", "key": "objects", "label": "Objects on the site"},
            {"type": "toggle", "key": "answer_unknown",
             "label": "Answer \"error invalid\" for an unknown name"},
        ],
        "delays": {"command_response": 0.002},
    }

    # Two servers, one object model: neither the stream nor the datagram
    # capacity model describes it alone.
    CAPACITY_KIND = None

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # (kind, name, space) -> record
        self._site: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._order: list[tuple[str, str, str]] = []
        cfg_objects = self.config.get("objects")
        if isinstance(cfg_objects, list):
            for row in cfg_objects:
                if not isinstance(row, dict):
                    continue
                kind = str(row.get("type") or "").strip().lower()
                kind = KINDS.get(kind, kind)
                name = str(row.get("name") or "").strip()
                space = str(row.get("space") or "").strip()
                if kind in ROW_KINDS and name:
                    self._add(kind, name, space or (DEFAULT_SPACE if kind in SPACED else ""))
        for kind, name, space in DEMO_SITE:
            self._add(kind, name, space)
        self._set_ui("objects", len(self._site))

    def _add(self, kind: str, name: str, space: str) -> None:
        if kind not in SPACED:
            space = ""
        key = (kind, name, space)
        if key in self._site:
            return
        rec: dict[str, Any] = {"kind": kind, "name": name, "space": space}
        if kind in ("channel", "group"):
            rec.update(level=0, min=0, max=255)
        elif kind == "preset":
            rec.update(ltp="dact", htp="dacth")
        elif kind == "sequence":
            rec.update(state="stop")
        elif kind == "wall":
            rec.update(open=False)
        elif kind == "macro":
            rec.update(state="off")
        elif kind == "override":
            rec.update(enabled=False)
        elif kind == "space":
            rec.update(master=255)
        self._site[key] = rec
        self._order.append(key)

    # ── Servers ──

    async def start(self, port: int) -> None:
        await super().start(port)
        try:
            await self.start_datagram_server(self.port)
        except OSError as exc:
            logger.warning("%s: UDP not served on port %d: %s", self.name, self.port, exc)

    async def stop(self) -> None:
        await self.stop_datagram_server()
        await super().stop()

    # ── Simulator UI ──

    def _set_ui(self, key: str, value: Any) -> None:
        # The plain write, without the station hook below.
        BaseSimulator.set_state(self, key, value)

    def set_state(self, key: str, value: Any) -> None:
        super().set_state(key, value)
        if key == "station" and value:
            try:
                n = int(value)
            except (TypeError, ValueError):
                n = 0
            presets = [k for k in self._order if k[0] == "preset"]
            if 1 <= n <= len(presets):
                rec = self._site[presets[n - 1]]
                self._notify(self._activate_preset(rec, htp=False))
            # Leave no button lit, so the same one can be pressed again.
            self._set_ui("station", 0)

    # ── Protocol ──

    def _eom(self) -> str:
        return END_OF_MESSAGE.get(str(self.config.get("end_of_message") or "cr"), "\r")

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("utf-8", errors="replace")
        replies: list[str] = []
        for line in re.split(r"[\r\n]+", text):
            line = line.strip()
            if not line:
                continue
            self._set_ui("last_command", line)
            reply = self._dispatch(line)
            if reply:
                replies.extend(reply)
        if not replies:
            return None
        eom = self._eom()
        return "".join(r + eom for r in replies).encode("utf-8")

    def _find(self, kind: str, name: str, space: str) -> dict[str, Any] | None:
        if kind not in SPACED:
            space = ""
        if space:
            return self._site.get((kind, name, space))
        for key in self._order:
            if key[0] == kind and key[1] == name:
                return self._site[key]
        return None

    def _dispatch(self, line: str) -> list[str] | None:
        if line in ("?", "help"):
            return list(HELP_LINES)
        m = _CMD_RE.match(line)
        if not m:
            return ["error invalid"]
        word, op, arg, rest = m.group(1).lower(), m.group(2).lower(), m.group(3), m.group(4) or ""
        kind = KINDS.get(word)
        if kind is None or not rest.strip():
            return ["error invalid"]
        parts = [p.strip() for p in rest.split(",")]
        name = parts[0]
        if kind in ("macro", "override"):
            space, extra = "", parts[1:]
        elif kind == "space":
            space, extra = "", parts[1:]
        elif kind == "sequence" and op in ("pause", "resume", "ras", "low", "rate"):
            space, extra = "", parts[1:]
        else:
            space, extra = (parts[1] if len(parts) > 1 else ""), parts[2:]
        rec = self._find(kind, name, space) if kind != "space" else self._find("space", name, "")
        if rec is None:
            # Off: the processor stays silent on a name it does not have.
            return ["error invalid"] if self.get_state("answer_unknown") else None
        if op in ("get", "geth"):
            return [self._status(rec, htp=(op == "geth"))]
        changed = self._apply(rec, op, arg, extra)
        if changed is None:
            return ["error invalid"]
        self._notify(changed)
        return None

    def _apply(
        self, rec: dict[str, Any], op: str, arg: str | None, extra: list[str],
    ) -> list[dict[str, Any]] | None:
        """Carry out one control command. Returns the records it changed, or
        None for a command this object does not take."""
        kind = rec["kind"]
        if kind in ("channel", "group"):
            if op in ("int", "ras", "low"):
                amount = _amount(arg)
                if amount is None:
                    return None
                level = rec["level"]
                level = amount if op == "int" else level + amount if op == "ras" else level - amount
                rec["level"] = max(rec["min"], min(rec["max"], level))
                return [rec] + self._alter_presets(rec["space"])
            if op == "tog":
                rec["level"] = 0 if rec["level"] > 0 else rec["max"]
                return [rec] + self._alter_presets(rec["space"])
            if kind == "channel" and op in ("min", "max"):
                amount = _amount(arg)
                if amount is None:
                    return None
                rec[op] = amount
                rec["level"] = max(rec["min"], min(rec["max"], rec["level"]))
                return [rec]
            return None
        if kind == "preset":
            if op in ("act", "acth"):
                return self._activate_preset(rec, htp=(op == "acth"))
            if op == "dact":
                rec["ltp"] = "dact"
                return [rec]
            if op == "dacth":
                rec["htp"] = "dacth"
                return [rec]
            if op == "tog":
                if rec["ltp"] == "dact":
                    return self._activate_preset(rec, htp=False)
                rec["ltp"] = "dact"
                return [rec]
            if op == "togh":
                if rec["htp"] == "dacth":
                    return self._activate_preset(rec, htp=True)
                rec["htp"] = "dacth"
                return [rec]
            if op == "rec":
                if rec["ltp"] == "alt":
                    rec["ltp"] = "act"
                    return [rec]
                return []
            return None
        if kind == "sequence":
            if op == "start":
                rec["state"] = "start"
            elif op == "stop":
                rec["state"] = "stop"
            elif op == "pause":
                if rec["state"] == "start":
                    rec["state"] = "pause"
            elif op == "resume":
                if rec["state"] == "pause":
                    rec["state"] = "start"
            elif op in ("ras", "low", "rate"):
                if arg is None:
                    return None
                return []
            else:
                return None
            return [rec]
        if kind == "space":
            members = [r for r in self._site.values() if r.get("space") == rec["name"]]
            if op == "off":
                changed = []
                for r in members:
                    if r["kind"] in ("channel", "group") and r["level"]:
                        r["level"] = r["min"]
                        changed.append(r)
                    elif r["kind"] == "preset" and (r["ltp"] != "dact" or r["htp"] != "dacth"):
                        r["ltp"], r["htp"] = "dact", "dacth"
                        changed.append(r)
                return changed
            if op in ("ras", "low"):
                amount = _amount(arg)
                if amount is None:
                    return None
                changed = []
                for r in members:
                    if r["kind"] in ("channel", "group"):
                        level = r["level"] + (amount if op == "ras" else -amount)
                        r["level"] = max(r["min"], min(r["max"], level))
                        changed.append(r)
                return changed
            if op == "master":
                amount = _amount(arg)
                if amount is None:
                    return None
                rec["master"] = amount
                return []
            return None
        if kind == "wall":
            if op in ("open", "close", "tog"):
                rec["open"] = (op == "open") or (op == "tog" and not rec["open"])
                return [rec]
            return None
        if kind == "macro":
            if op in ("on", "off"):
                rec["state"] = op
            elif op == "tog":
                rec["state"] = "off" if rec["state"] == "on" else "on"
            elif op == "cancel":
                if rec["state"] == "running":
                    rec["state"] = "off"
            else:
                return None
            return [rec]
        if kind == "override":
            if op in ("enab", "disab"):
                rec["enabled"] = op == "enab"
            elif op == "tog":
                rec["enabled"] = not rec["enabled"]
            else:
                return None
            return [rec]
        return None

    def _alter_presets(self, space: str) -> list[dict[str, Any]]:
        """A level changed by hand in a space alters the space's active LTP
        presets (the guide's "pst alt")."""
        changed = []
        for r in self._site.values():
            if r["kind"] == "preset" and r["space"] == space and r["ltp"] == "act":
                r["ltp"] = "alt"
                changed.append(r)
        return changed

    def _activate_preset(self, rec: dict[str, Any], *, htp: bool) -> list[dict[str, Any]]:
        if htp:
            rec["htp"] = "acth"
            return [rec]
        changed = [rec]
        for other in self._site.values():
            if (other is not rec and other["kind"] == "preset"
                    and other["space"] == rec["space"] and other["ltp"] != "dact"):
                other["ltp"] = "dact"
                changed.append(other)
        rec["ltp"] = "act"
        return changed

    def _status(self, rec: dict[str, Any], *, htp: bool = False) -> str:
        kind, name, space = rec["kind"], rec["name"], rec["space"]
        if kind == "channel":
            return f"chan int:{rec['level']} {name}, {space}"
        if kind == "group":
            # The guide's form for this one has a comma after the level.
            return f"grp int:{rec['level']}, {name}, {space}"
        if kind == "preset":
            return f"pst {rec['htp'] if htp else rec['ltp']} {name}, {space}"
        if kind == "sequence":
            return f"seq {rec['state']} {name}, {space}"
        if kind == "wall":
            return f"Wall {'open' if rec['open'] else 'close'} {name}, {space}"
        if kind == "macro":
            return f"Macro {rec['state']} {name}"
        if kind == "override":
            return f"ovr {'enab' if rec['enabled'] else 'disab'} {name}"
        return "error invalid"

    def _notify(self, changed: list[dict[str, Any]]) -> None:
        """PSAP Triggers: each change goes out as its status line on every TCP
        connection (the serial line), while the toggle is on."""
        if not changed or not self.get_state("triggers"):
            return
        eom = self._eom()
        lines = []
        for rec in changed:
            if rec["kind"] == "space":
                continue
            lines.append(self._status(rec) + eom)
            if rec["kind"] == "preset" and rec["htp"] != "dacth":
                lines.append(self._status(rec, htp=True) + eom)
        if lines:
            asyncio.ensure_future(self.push("".join(lines).encode("utf-8")))
