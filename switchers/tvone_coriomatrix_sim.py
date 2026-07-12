"""
tvONE CORIOmatrix Simulator.

Implements the CORIOmax command-line API subset the driver exercises:

  - login(<user>,<pass>) gate — every other command answers !Failed until a
    login succeeds. Any credentials are accepted EXCEPT a username or
    password of "invalid" (the platform's designated bad-credential
    sentinel), which is rejected so the driver's auth-failure path is
    testable.
  - Slot enumeration ("Slots" + per-slot dumps) for a 4x4 build: two 2-in
    cards in slots 1-2, two 2-out cards in slots 13-14.
  - Per-port dumps and property reads/writes: Status, InsList, CutToBlack.
  - Routing ("Slot1.In1 > Slot13.Out1", long or S<n>I<n>/S<n>O<n> alias
    form, case-insensitive) with per-output state.
  - Preset.Take (read/write), Preset.Read / NameRead / SaveRead(), and
    Preset.PresetList().
  - System.Status / System.API_Version / System.Unit_Description.

Replies use the documented shapes: long-form property paths, "!Done <echo>"
success terminals, "!Failed <echo>" failure terminals, and the login
confirmation "!Info : User <name> Logged In".
"""

from __future__ import annotations

import logging
import re

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

# Installed cards: slot -> (card type, port kind, port count)
CARDS: dict[int, tuple[str, str, int]] = {
    1: ("DVI_U 2-in", "in", 2),
    2: ("HDMI 2-in", "in", 2),
    13: ("DVI 2-out", "out", 2),
    14: ("HDMI 2-out", "out", 2),
}
SLOT_COUNT = 16

_RE_LOGIN = re.compile(r"(?i)^login\(([^,]*),(.*)\)$")
_RE_ROUTE = re.compile(
    r"(?i)^(?:(?:slots\.)?slot(\d+)\.in(\d+)|s(\d+)i(\d+))\s*>\s*"
    r"(?:(?:slots\.)?slot(\d+)\.out(\d+)|s(\d+)o(\d+))$"
)
_RE_PORT = re.compile(r"(?i)^(?:(?:slots\.)?slot(\d+)\.(in|out)(\d+)|s(\d+)(i|o)(\d+))")


def _port_key(slot: int, kind: str, port: int) -> str:
    return f"{'in' if kind.startswith('i') else 'out'}_{slot}_{port}"


class TvoneCoriomatrixSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "tvone_coriomatrix",
        "name": "tvONE CORIOmatrix Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 10001,
        "delimiter": "\r\n",
        "initial_state": {
            "logged_in": False,
            "unit_description": "CORIOmatrix",
            "api_version": "3.1.4386",
            "system_status": "Serving",
            "active_preset": 1,
            # Routing: output port -> input alias ("" = nothing routed)
            "route_out_13_1": "s1i1",
            "route_out_13_2": "s1i2",
            "route_out_14_1": "s2i1",
            "route_out_14_2": "s2i2",
            "ctb_out_13_1": False,
            "ctb_out_13_2": False,
            "ctb_out_14_1": False,
            "ctb_out_14_2": False,
            "sig_in_1_1": "OK",
            "sig_in_1_2": "NO_SIGNAL",
            "sig_in_2_1": "OK",
            "sig_in_2_2": "NO_SIGNAL",
        },
        "controls": [
            {"type": "indicator", "key": "unit_description", "label": "Unit Name"},
            {"type": "indicator", "key": "active_preset", "label": "Active Preset"},
            {"type": "toggle", "key": "logged_in", "label": "Session Logged In"},
            {"type": "toggle", "key": "ctb_out_13_1", "label": "Out s13o1 Cut to Black"},
            {"type": "select", "key": "sig_in_1_1", "label": "Input s1i1 Signal",
             "options": ["OK", "NO_SIGNAL"]},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._line_mode = True
        # Instance copy of the card table so a test (or future UI control)
        # can add/remove a card and exercise roster reconciliation.
        self._cards: dict[int, tuple[str, str, int]] = dict(CARDS)
        # Preset store: id -> {"name": str, "routing": {out_key: source}}
        self._presets: dict[int, dict] = {
            1: {"name": "start", "routing": self._routing_snapshot(initial=True)},
            2: {"name": "side_by_side", "routing": {
                "route_out_13_1": "s2i1", "route_out_13_2": "s2i2",
                "route_out_14_1": "s1i1", "route_out_14_2": "s1i2",
            }},
        }
        self._edit_preset: int = 1

    # ── Helpers ──

    def _routing_snapshot(self, initial: bool = False) -> dict[str, str]:
        src = self.SIMULATOR_INFO["initial_state"] if initial else self.state
        return {
            k: src[k]
            for k in (
                "route_out_13_1", "route_out_13_2",
                "route_out_14_1", "route_out_14_2",
            )
        }

    @staticmethod
    def _done(echo: str) -> bytes:
        return f"!Done {echo}\r\n".encode()

    @staticmethod
    def _failed(echo: str) -> bytes:
        return f"!Failed {echo}\r\n".encode()

    def _reply(self, lines: list[str], echo: str) -> bytes:
        body = "".join(f"{ln}\r\n" for ln in lines)
        return body.encode() + self._done(echo)

    # ── Dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", errors="replace").strip()
        if not line:
            return None

        m = _RE_LOGIN.match(line)
        if m:
            user, pw = m.group(1).strip(), m.group(2).strip()
            if user.lower() == "invalid" or pw.lower() == "invalid" or not pw:
                self.set_state("logged_in", False)
                return self._failed(line)
            self.set_state("logged_in", True)
            return f"!Info : User {user} Logged In\r\n".encode()

        if not self.state["logged_in"]:
            return self._failed(line)

        return self._dispatch(line)

    def _dispatch(self, line: str) -> bytes:
        low = line.lower().strip()

        # ── Routing set ──
        m = _RE_ROUTE.match(line)
        if m:
            in_slot = int(m.group(1) or m.group(3))
            in_port = int(m.group(2) or m.group(4))
            out_slot = int(m.group(5) or m.group(7))
            out_port = int(m.group(6) or m.group(8))
            if not self._port_exists("in", in_slot, in_port) or not self._port_exists(
                "out", out_slot, out_port
            ):
                return self._failed(line)
            self.set_state(
                f"route_out_{out_slot}_{out_port}", f"s{in_slot}i{in_port}"
            )
            return self._done(line)

        # ── Slots enumeration ──
        if low == "slots":
            lines = []
            for n in range(1, SLOT_COUNT + 1):
                if n in self._cards:
                    lines.append(f"Slots.Slot{n} = <...>")
                else:
                    lines.append(f"Slots.Slot{n} = NO CARD")
            return self._reply(lines, "Slots")

        # ── Slot dump: "Slot3" / "slots.slot3" ──
        m = re.fullmatch(r"(?i)(?:slots\.)?slot(\d+)", low)
        if m:
            n = int(m.group(1))
            if n not in self._cards:
                return self._failed(line)
            card_type, kind, count = self._cards[n]
            word = "In" if kind == "in" else "Out"
            lines = [f"Slot{n}.Cardtype = {card_type}"]
            for p in range(1, count + 1):
                lines.append(f"Slot{n}.{word}{p} = <...>")
            return self._reply(lines, f"Slot{n}")

        # ── Port dump / port property reads and writes ──
        m = _RE_PORT.match(line)
        if m:
            if m.group(1) is not None:
                slot, kind, port = int(m.group(1)), m.group(2).lower(), int(m.group(3))
            else:
                slot, kind, port = int(m.group(4)), m.group(5).lower(), int(m.group(6))
            kind = "in" if kind.startswith("i") else "out"
            if not self._port_exists(kind, slot, port):
                return self._failed(line)
            rest = line[m.end():].strip()
            return self._port_request(line, slot, kind, port, rest)

        # ── Presets ──
        m = re.fullmatch(r"(?i)(?:routing\.)?preset\.take\s*=\s*(\d+)", line.strip())
        if m:
            pid = int(m.group(1))
            preset = self._presets.get(pid)
            if preset is None:
                return self._failed(line)
            for key, value in preset["routing"].items():
                self.set_state(key, value)
            self.set_state("active_preset", pid)
            return self._done(line)
        if low in ("preset.take", "routing.preset.take"):
            return self._reply(
                [f"Preset.Take = {self.state['active_preset']}"], "Preset.Take"
            )
        m = re.fullmatch(r"(?i)(?:routing\.)?preset\.read\s*=\s*(\d+)", line.strip())
        if m:
            pid = int(m.group(1))
            if not 1 <= pid <= 49:
                return self._failed(line)
            self._edit_preset = pid
            return self._done(line)
        m = re.fullmatch(r"(?i)(?:routing\.)?preset\.nameread\s*=\s*(\S+)", line.strip())
        if m:
            entry = self._presets.setdefault(
                self._edit_preset, {"name": "", "routing": {}}
            )
            entry["name"] = m.group(1)
            return self._done(line)
        if low in ("preset.saveread()", "routing.preset.saveread()"):
            entry = self._presets.setdefault(
                self._edit_preset, {"name": "", "routing": {}}
            )
            entry["routing"] = self._routing_snapshot()
            return "// Preset(s) saved.\r\n".encode() + self._done("Preset.SaveRead()")
        if low in ("preset.presetlist()", "routing.preset.presetlist()"):
            lines = [
                f"Routing.Preset.PresetList[{pid}]={info['name']},Canvas1,1000"
                for pid, info in sorted(self._presets.items())
            ]
            return self._reply(lines, "Preset.PresetList()")

        # ── System ──
        if low == "system.status":
            return self._reply(
                [f"System.Status = {self.state['system_status']}"], "System.Status"
            )
        if low == "system.api_version":
            return self._reply(
                [f"System.API_Version = {self.state['api_version']}"],
                "System.API_Version",
            )
        if low == "system.unit_description":
            return self._reply(
                [f'System.Unit_Description = "{self.state["unit_description"]}"'],
                "System.Unit_Description",
            )
        m = re.fullmatch(
            r'(?i)system\.unit_description\s*=\s*"?([^"]{0,32})"?', line.strip()
        )
        if m:
            self.set_state("unit_description", m.group(1))
            return self._done(line)

        if low == "logout":
            self.set_state("logged_in", False)
            return "!Info : User admin Logged Out\r\n".encode()

        return self._failed(line)

    # ── Port handling ──

    def _port_exists(self, kind: str, slot: int, port: int) -> bool:
        card = self._cards.get(slot)
        return card is not None and card[1] == kind and 1 <= port <= card[2]

    def _port_request(
        self, line: str, slot: int, kind: str, port: int, rest: str
    ) -> bytes:
        word = "In" if kind == "in" else "Out"
        base = f"Slot{slot}.{word}{port}"

        if kind == "in":
            props = {
                "FullName": f"{word}{port}",
                "Status": self.state[f"sig_in_{slot}_{port}"],
                "Alias": f"s{slot}i{port}",
            }
        else:
            props = {
                "FullName": f"{word}{port}",
                "Status": "OK",
                "Alias": f"s{slot}o{port}",
                "InsList": self._inslist_value(slot, port),
                "CutToBlack": "On" if self.state[f"ctb_out_{slot}_{port}"] else "Off",
            }

        if not rest:
            # Full port dump.
            lines = [f"{base}.{k} = {v}" for k, v in props.items()]
            return self._reply(lines, base)

        # Property write: ".CutToBlack = On"
        m = re.fullmatch(r"(?i)\.cuttoblack\s*=\s*(on|off)", rest)
        if m and kind == "out":
            self.set_state(f"ctb_out_{slot}_{port}", m.group(1).lower() == "on")
            return self._done(line)

        # Property read: ".InsList", ".Status", ...
        m = re.fullmatch(r"\.(\w+)", rest)
        if m:
            for k, v in props.items():
                if k.lower() == m.group(1).lower():
                    return self._reply([f"{base}.{k} = {v}"], f"{base}.{k}")
            return self._failed(line)

        return self._failed(line)

    def _inslist_value(self, slot: int, port: int) -> str:
        source = self.state[f"route_out_{slot}_{port}"]
        if not source:
            return "NULL"
        m = re.fullmatch(r"s(\d+)i(\d+)", source)
        return f"Slot{m.group(1)}.In{m.group(2)}" if m else source
