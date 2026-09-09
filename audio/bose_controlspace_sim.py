"""
Bose Professional ControlSpace — Serial Control Protocol simulator.

A ControlSpace EX / ESP processor on TCP 10055 as the v5.13 protocol document
describes it: CR-terminated ASCII, module commands (SA / GA / MA) answered
with a bare ACK (0x06) or ``NAK nn``, system and device commands (SS / GS,
SG / GG / SH, SN / GN, SRC / GRC, SV / GV / SI, SM / GM, GL, IP, NP, RESET)
that answer only when they are queries, hexadecimal values on those and
decimal dB on the module commands, several module commands on one line
separated by semicolons, and the SUB / UNS subscription commands that send a
value at once and again on every change.

The design it holds is the driver's own tables: the device config's
``modules``, ``groups``, ``parameter_sets`` and ``room_combine_groups`` (or
the driver's defaults when a device has none), so the simulator answers
exactly what the driver asks for and refuses, with the documented NAK codes,
what a real processor refuses: a label that is not in the design (01), an
index the module does not have (02), a value out of range (03).

Two behaviours the document states and this models on purpose:

- A change made by serial command is not notified back to the serial
  connection that made it (section 6, "Automatic notification"), so the
  driver's read-back after every write is what a test proves. Changes from
  any other source (the Simulator UI, ``set_value``, another client) are
  pushed to every subscriber.
- Subscriptions die with the connection; a reconnecting driver starts with
  none.

Two sim-only config keys: ``push_supported`` (default True; False makes the
processor ignore ``SUB``, the way an older ESP-00 might) and ``echo_own_changes``
(default False; True notifies the writer too, for a processor that turns out
to do that).

Driver side: ``audio/bose_controlspace.py`` — the module tables and the
value codec are imported from it so the two cannot drift.
"""


import asyncio
import importlib.util
import logging
import random
import re
import sys
from pathlib import Path
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


def _load_driver_module():
    name = "openavc_sim_bose_controlspace_driver"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("bose_controlspace.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_d = _load_driver_module()

ACK, NAK = _d.ACK, _d.NAK
FMT_LEVEL, FMT_ONOFF, FMT_LOGIC, FMT_INT, FMT_NUMBER, FMT_ENUM, FMT_STRING, FMT_ROUTING = (
    _d.FMT_LEVEL, _d.FMT_ONOFF, _d.FMT_LOGIC, _d.FMT_INT, _d.FMT_NUMBER, _d.FMT_ENUM,
    _d.FMT_STRING, _d.FMT_ROUTING,
)

Key = tuple[str, tuple[str, ...]]     # (module label, read index path)

_SA_RE = re.compile(r'^SA\s*(?:@\s*"([^"]*)"\s*)?"([^"]*)"((?:>[^>=]*)+)=(.*)$')
_GA_RE = re.compile(r'^GA\s*(?:@\s*"([^"]*)"\s*)?"([^"]*)"((?:>[^>=]*)+)$')
_MA_RE = re.compile(r'^MA\s*(?:@\s*"([^"]*)"\s*)?"([^"]*)">(\d+)(?:="?([^"]*)"?)?$')
_SUB_RE = re.compile(r'^(SUB|UNS)\s*"(.*)"$')
_HEX = r"([0-9a-fA-F]+)"

_CALL_STATUS_ROWS = {"pstn_input": ("0", "1"), "voip_input": ("0", "1")}
_CALL_ACTIVE_ROWS = {"pstn_input": ("0", "8"), "voip_input": ("0", "6")}


class BoseControlSpaceSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "bose_controlspace",
        "name": "Bose ControlSpace Processor Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": _d.DEFAULT_PORT,
        "delimiter": "\r",
        "initial_state": {
            "model": "EX-1280C",
            "ip_address": "192.168.0.160",
            "parameter_set": 0,
            "subscriptions": 0,
            "push_supported": True,
            "main_volume_db": 0.0,
            "main_volume_mute": False,
            "selector_source": 1,
            "input_1_mute": False,
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "ip_address", "label": "IP Address"},
            {"type": "indicator", "key": "parameter_set", "label": "Last Parameter Set"},
            {"type": "indicator", "key": "subscriptions", "label": "Subscriptions"},
            {"type": "toggle", "key": "push_supported", "label": "Answers SUB (subscriptions)"},
            # The default module table's wall-controller-like controls: moving
            # one here pushes the new value to every subscriber, as a CC-16
            # or CC-64 would.
            {"type": "slider", "key": "main_volume_db", "label": "Main Volume (dB)",
             "min": -60.5, "max": 12, "step": 0.5},
            {"type": "toggle", "key": "main_volume_mute", "label": "Main Volume Mute"},
            {"type": "slider", "key": "selector_source", "label": "Selector 1 Source",
             "min": 1, "max": 4, "step": 1},
            {"type": "toggle", "key": "input_1_mute", "label": "Input 1 Mute"},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        modules, problems = _d.parse_modules_config(cfg.get("modules") or _d.DEFAULT_MODULES)
        for p in problems:
            logger.warning("%s: module table: %s", self.name, p)
        self._modules = modules
        self._by_name: dict[str, Any] = {m.name.lower(): m for m in modules}
        groups, _ = _d.parse_groups_config(cfg.get("groups") or [])
        self._groups = {g.number: g for g in groups}
        try:
            self._rc_count = max(0, min(_d.ROOM_COMBINE_MAX, int(cfg.get("room_combine_groups", 0) or 0)))
        except (TypeError, ValueError):
            self._rc_count = 0
        # The design: every readable parameter with its control and value.
        self._ctl: dict[Key, Any] = {}          # read path -> ControlDef
        self._write: dict[Key, Key] = {}        # write path -> read path
        self._values: dict[Key, Any] = {}
        for m in modules:
            if m.signal_level is not None:
                continue
            for ctl in m.controls.values():
                if not ctl.idx:
                    continue
                key = (m.name.lower(), ctl.idx)
                self._ctl[key] = ctl
                self._write[(m.name.lower(), ctl.write_idx or ctl.idx)] = key
                self._values[key] = self._seed(m, ctl)
        # Groups (hex level 0..144 or a selector channel), parameter set,
        # room combine, slot I/O, meters, network.
        self._group_level: dict[int, int] = {n: 0x78 for n in self._groups}
        self._group_mute: dict[int, bool] = {n: False for n in self._groups}
        self._group_source: dict[int, int] = {n: 1 for n in self._groups}
        self._parameter_set = 0
        self._rc: dict[int, list[set[int]]] = {n: [{r} for r in range(1, _d.ROOM_MAX + 1)]
                                              for n in range(1, self._rc_count + 1)}
        self._io_level: dict[tuple[str, str], int] = {}
        self._io_mute: dict[tuple[str, str], bool] = {}
        self._meters: dict[tuple[str, str | None], list[int]] = {}
        for m in modules:
            if m.signal_level is not None:
                spec = m.signal_level
                n = spec.channels or 4
                self._meters[(spec.slot, spec.param)] = [0x40] * n
        self._network = {"ip": "192.168.0.160", "mask": "255.255.255.0", "gateway": "192.168.0.1",
                         "addressing": "S"}
        self._subs: set[str] = set()
        self._push_ok = bool(cfg.get("push_supported", True))
        self._echo_own = bool(cfg.get("echo_own_changes", False))
        self._meter_task: asyncio.Task | None = None
        # Simulator-UI keys -> (module label, prop) for the default table.
        self._ui_map: dict[str, tuple[str, str]] = {
            "main_volume_db": ("Main Volume", "level"),
            "main_volume_mute": ("Main Volume", "mute"),
            "selector_source": ("Selector 1", "source"),
            "input_1_mute": ("Input 1", "mute"),
        }
        super().set_state("push_supported", self._push_ok)
        super().set_state("ip_address", self._network["ip"])

    # ── Seeds ──

    @staticmethod
    def _seed(m: Any, ctl: Any) -> Any:
        if ctl.fmt == FMT_LEVEL:
            return 0.0 if (ctl.max is None or ctl.max >= 0) else float(ctl.max)
        if ctl.fmt in (FMT_ONOFF, FMT_LOGIC):
            if m.type_id == "standard_mixer" and ctl.prop.startswith("xp_"):
                _, i, o = ctl.prop.split("_")
                return i == o
            return False
        if ctl.fmt == FMT_INT:
            return int(ctl.min) if ctl.min is not None else 1
        if ctl.fmt == FMT_NUMBER:
            return float(ctl.min) if ctl.min is not None else 0.0
        if ctl.fmt == FMT_ENUM:
            return ctl.values[0][0] if ctl.values else ""
        if ctl.fmt == FMT_STRING:
            if ctl.prop == "call_status":
                return "HANGUP"
            if ctl.prop == "account_status":
                return "PROXY_REGISTERED"
            if ctl.prop == "output_format":
                return "PCM16"
            return ""
        return None

    # ── Lookups (test and UI hooks) ──

    def _find(self, name: str, prop: str) -> Key | None:
        m = self._by_name.get(name.lower())
        if m is None:
            for cand in self._modules:
                if cand.cid == name:
                    m = cand
                    break
        if m is None:
            return None
        ctl = m.controls.get(prop)
        if ctl is None or not ctl.idx:
            return None
        return (m.name.lower(), ctl.idx)

    def value_of(self, name: str, prop: str) -> Any:
        key = self._find(name, prop)
        if key is None:
            return None
        ctl = self._ctl.get(key)
        if ctl is not None and ctl.fmt == FMT_ROUTING:
            return self._routing_mask(key)
        return self._values.get(key)

    def is_subscribed(self, get_text: str) -> bool:
        return _norm(get_text) in self._subs

    @property
    def subscription_count(self) -> int:
        return len(self._subs)

    @property
    def parameter_set(self) -> int:
        return self._parameter_set

    def group_level_raw(self, number: int) -> int | None:
        return self._group_level.get(number)

    def joined_rooms(self, group: int) -> list[set[int]]:
        return [set(g) for g in self._rc.get(group, [])]

    # ── Framing ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("ascii", errors="replace").strip("\r\n")
        if not line.strip():
            return None
        out = bytearray()
        for cmd in _split_commands(line):
            reply = self._dispatch(cmd.strip())
            if reply:
                out += reply
        return bytes(out) if out else None

    def _dispatch(self, cmd: str) -> bytes:
        if not cmd:
            return b""
        upper = cmd.upper()
        # Module commands (ACK / NAK).
        m = _SA_RE.match(cmd)
        if m:
            return self._set_module(m.group(2), _split_path(m.group(3)), m.group(4).strip())
        m = _GA_RE.match(cmd)
        if m:
            return self._get_module(m.group(2), _split_path(m.group(3)))
        m = _MA_RE.match(cmd)
        if m:
            return self._module_action(m.group(2), m.group(3), m.group(4))
        # Subscriptions.
        if upper == "SUB":
            return b"SUB yes\r" if self._push_ok else b""
        m = _SUB_RE.match(cmd)
        if m:
            return self._subscribe(m.group(1).upper(), m.group(2))
        # System commands.
        m = re.match(rf"^SS\s*{_HEX}$", cmd)
        if m:
            n = int(m.group(1), 16)
            if 1 <= n <= _d.PARAMETER_SET_MAX:
                self._parameter_set = n
                super().set_state("parameter_set", n)
                self._notify("GS", own=True)
            return b""
        if upper == "GS":
            return f"S {self._parameter_set:x}\r".encode()
        m = re.match(rf"^SG\s*{_HEX},{_HEX}$", cmd)
        if m:
            n, value = int(m.group(1), 16), int(m.group(2), 16)
            g = self._groups.get(n)
            if g is not None:
                if g.kind == "selector":
                    if 1 <= value <= 32:
                        self._group_source[n] = value
                else:
                    self._group_level[n] = value if value == 0xFF else max(0, min(_d.HEX_LEVEL_MAX, value))
                self._notify(f"GG {n:x}", own=True)
            return b""
        m = re.match(rf"^GG\s*{_HEX}$", cmd)
        if m:
            return self._gg_line(int(m.group(1), 16))
        m = re.match(rf"^SH\s*{_HEX},([01]),{_HEX}$", cmd)
        if m:
            n, up, steps = int(m.group(1), 16), m.group(2) == "1", int(m.group(3), 16)
            g = self._groups.get(n)
            if g is not None and g.kind == "level":
                cur = self._group_level[n]
                cur = 0 if cur == 0xFF else cur
                self._group_level[n] = max(0, min(_d.HEX_LEVEL_MAX, cur + (steps if up else -steps)))
                self._notify(f"GG {n:x}", own=True)
            return b""
        m = re.match(rf"^SN\s*{_HEX},([MUTmut])$", cmd)
        if m:
            n, s = int(m.group(1), 16), m.group(2).upper()
            if n in self._groups:
                self._group_mute[n] = {"M": True, "U": False}.get(s, not self._group_mute[n])
                self._notify(f"GN {n:x}", own=True)
            return b""
        m = re.match(rf"^GN\s*{_HEX}$", cmd)
        if m:
            return self._gn_line(int(m.group(1), 16))
        m = re.match(r"^SRC\s*(\d+),(\d+),(\d+),([JSjs])$", cmd)
        if m:
            n, a, b, s = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).upper()
            if n in self._rc and 1 <= a <= _d.ROOM_MAX and 1 <= b <= _d.ROOM_MAX and a != b:
                self._room_combine(n, a, b, s == "J")
            return b""
        m = re.match(r"^GRC\s*(\d+)(?:,(\d+),(\d+))?$", cmd)
        if m:
            n = int(m.group(1))
            if n not in self._rc:
                return b""
            if m.group(2):
                a, b = int(m.group(2)), int(m.group(3))
                joined = any(a in grp and b in grp for grp in self._rc[n])
                return f"GRC {n},{a},{b},{'J' if joined else 'S'}\r".encode()
            body = "".join("[" + ",".join(str(r) for r in sorted(grp)) + "]"
                           for grp in sorted(self._rc[n], key=lambda g: min(g)))
            return f"GRC {n},{body}\r".encode()
        # Device commands.
        m = re.match(rf"^SV\s*{_HEX},{_HEX},{_HEX}$", cmd)
        if m:
            key = (m.group(1).lower(), m.group(2).lower())
            if not self._io_mute.get(key, False):
                v = int(m.group(3), 16)
                self._io_level[key] = v if v == 0xFF else max(0, min(_d.HEX_LEVEL_MAX, v))
                self._notify(f"GV {key[0]},{key[1]}", own=True)
            return b""
        m = re.match(rf"^GV\s*{_HEX},{_HEX}$", cmd)
        if m:
            key = (m.group(1).lower(), m.group(2).lower())
            return f"GV {key[0]},{key[1]},{self._io_level.get(key, 0x78):x}\r".encode()
        m = re.match(rf"^SI\s*{_HEX},{_HEX},([01]),{_HEX}$", cmd)
        if m:
            key = (m.group(1).lower(), m.group(2).lower())
            if not self._io_mute.get(key, False):
                cur = self._io_level.get(key, 0x78)
                cur = 0 if cur == 0xFF else cur
                delta = int(m.group(4), 16) * (1 if m.group(3) == "1" else -1)
                self._io_level[key] = max(0, min(_d.HEX_LEVEL_MAX, cur + delta))
                self._notify(f"GV {key[0]},{key[1]}", own=True)
            return b""
        m = re.match(rf"^SM\s*{_HEX},{_HEX},([MUTmut])$", cmd)
        if m:
            key = (m.group(1).lower(), m.group(2).lower())
            s = m.group(3).upper()
            self._io_mute[key] = {"M": True, "U": False}.get(s, not self._io_mute.get(key, False))
            self._notify(f"GM {key[0]},{key[1]}", own=True)
            return b""
        m = re.match(rf"^GM\s*{_HEX},{_HEX}$", cmd)
        if m:
            key = (m.group(1).lower(), m.group(2).lower())
            return f"GM {key[0]},{key[1]},{'M' if self._io_mute.get(key, False) else 'U'}\r".encode()
        m = re.match(rf"^GL\s*{_HEX}(?:,{_HEX})?$", cmd)
        if m:
            key = (m.group(1).lower(), m.group(2).lower() if m.group(2) else None)
            levels = self._meters.get(key)
            if levels is None:
                return b""
            self._wander(levels)
            head = f"GL {key[0]},{key[1]}" if key[1] else f"GL {key[0]}"
            return f"{head} [{','.join(format(v, 'x') for v in levels)}]\r".encode()
        if upper == "IP":
            return f"IP {self._network['ip']}\r".encode()
        m = re.match(r"^IP\s+(\d{1,3}(?:\.\d{1,3}){3})$", cmd)
        if m:
            self._network["ip"] = m.group(1)      # applies after a reboot on the real unit
            return b""
        m = re.match(r"^NP\s*([TMGtmg])$", cmd)
        if m:
            p = m.group(1).upper()
            v = {"T": self._network["addressing"], "M": self._network["mask"],
                 "G": self._network["gateway"]}[p]
            return f"NP {p},{v}\r".encode()
        m = re.match(r"^NP\s*([TMGtmg]),(.+)$", cmd)
        if m:
            p, v = m.group(1).upper(), m.group(2).strip()
            if p == "T" and v.upper()[:1] in ("D", "S"):
                self._network["addressing"] = v.upper()[:1]
            elif p == "M":
                self._network["mask"] = v
            elif p == "G":
                self._network["gateway"] = v
            return b""
        if upper == "NP F":
            self._network = {"ip": "169.254.0.1", "mask": "255.255.0.0", "gateway": "0.0.0.0",
                             "addressing": "D"}
            return b""
        if upper == "RESET":
            self._parameter_set = 0
            super().set_state("parameter_set", 0)
            self._subs.clear()
            super().set_state("subscriptions", 0)
            return b""
        # A module-grammar command the design does not know.
        if upper[:2] in ("SA", "GA", "MA"):
            return _nak("99")
        return b""

    # ── Module commands ──

    def _module(self, name: str) -> Any:
        return self._by_name.get(name.lower())

    def _set_module(self, name: str, path: tuple[str, ...], raw: str) -> bytes:
        m = self._module(name)
        if m is None or m.signal_level is not None:
            return _nak("01")
        key = self._write.get((m.name.lower(), path))
        if key is None:
            return _nak("02")
        ctl = self._ctl[key]
        if not ctl.writable:
            return _nak("02")
        try:
            value = self._apply_raw(m, key, ctl, raw)
        except ValueError:
            return _nak("03")
        if value is not None:
            self._store(key, value, own=True)
        return bytes([ACK])

    def _apply_raw(self, m: Any, key: Key, ctl: Any, raw: str) -> Any:
        r = raw.strip().strip('"')
        if ctl.fmt in (FMT_ONOFF, FMT_LOGIC):
            u = r.upper()
            if u == "O":
                return True
            if u == "F":
                return False
            if u == "T":
                return not bool(self._values.get(key))
            if u == "P" and ctl.fmt == FMT_LOGIC:
                return False          # a pulse ends where it started
            raise ValueError(r)
        if ctl.fmt in (FMT_LEVEL, FMT_NUMBER):
            v = float(r)
            if (ctl.min is not None and v < ctl.min) or (ctl.max is not None and v > ctl.max):
                raise ValueError(r)
            return v
        if ctl.fmt == FMT_INT:
            v = int(float(r))
            if (ctl.min is not None and v < ctl.min) or (ctl.max is not None and v > ctl.max):
                raise ValueError(r)
            return v
        if ctl.fmt == FMT_ENUM:
            if r not in [w for w, _ in ctl.values]:
                raise ValueError(r)
            return r
        if ctl.fmt == FMT_ROUTING:
            # Routing A written directly: fan the mask out to the cross-points.
            i = ctl.prop.split("_")[1]
            for o, on in _d.routing_mask_to_outputs(r, ctl.fanout).items():
                xp = self._find(m.name, f"xp_{i}_{o}")
                if xp is not None:
                    self._values[xp] = on
            return None
        if ctl.fmt == FMT_STRING:
            return r
        raise ValueError(r)

    def _get_module(self, name: str, path: tuple[str, ...]) -> bytes:
        m = self._module(name)
        if m is None or m.signal_level is not None:
            return _nak("01")
        key = (m.name.lower(), path)
        if key not in self._ctl:
            return _nak("02")
        return self._value_line(key)

    def _module_action(self, name: str, index: str, parameter: str | None) -> bytes:
        m = self._module(name)
        if m is None:
            return _nak("01")
        if m.type_id not in _CALL_STATUS_ROWS or index not in ("1", "2", "3", "4", "5"):
            return _nak("02")
        if index == "5" and m.type_id != "voip_input":
            return _nak("02")
        status = self._find(m.name, "call_status")
        active = self._find(m.name, "call_active")
        caller = self._find(m.name, "caller_id")
        if index == "2":
            self._store(status, "ACTIVE", own=True)
            self._store(active, True, own=True)
            if caller is not None:
                self._store(caller, parameter or "", own=True)
        elif index in ("3", "5"):
            self._store(status, "HANGUP", own=True)
            self._store(active, False, own=True)
        elif index == "4":
            self._store(status, "ACTIVE", own=True)
            self._store(active, True, own=True)
        elif index == "1" and not self._values.get(active):
            return _nak("02")     # a dial key is available only during a call
        return bytes([ACK])

    def _value_line(self, key: Key) -> bytes:
        ctl = self._ctl[key]
        name = next(m.name for m in self._modules if m.name.lower() == key[0])
        return f'GA "{name}">{">".join(key[1])}={self._render(key, ctl)}\r'.encode()

    def _render(self, key: Key, ctl: Any) -> str:
        value = self._values.get(key)
        if ctl.fmt in (FMT_ONOFF, FMT_LOGIC):
            return "O" if value else "F"
        if ctl.fmt in (FMT_LEVEL, FMT_NUMBER):
            return _d.format_number(float(value or 0.0))
        if ctl.fmt == FMT_INT:
            return str(int(value or 0))
        if ctl.fmt == FMT_ROUTING:
            return self._routing_mask(key)
        if ctl.fmt == FMT_STRING:
            text = str(value or "")
            return f'"{text}"' if ctl.prop in ("call_status", "account_status") else text
        return str(value if value is not None else "")

    def _routing_mask(self, key: Key) -> str:
        ctl = self._ctl[key]
        i = ctl.prop.split("_")[1]
        name = key[0]
        outputs: dict[int, bool] = {}
        for o in range(1, ctl.fanout + 1):
            xp = (name, ("4", f"({i},{o})"))
            outputs[o] = bool(self._values.get(xp, False))
        return _d.outputs_to_routing_mask(outputs)

    # ── Subscriptions and pushes ──

    def _subscribe(self, verb: str, get_text: str) -> bytes:
        if not self._push_ok:
            return b""
        norm = _norm(get_text)
        value = self._answer(get_text)
        if value is None:
            return f'{verb} "{get_text}",no\r'.encode()
        if verb == "SUB":
            self._subs.add(norm)
            super().set_state("subscriptions", len(self._subs))
            return f'SUB "{get_text}",yes\r'.encode() + value
        self._subs.discard(norm)
        super().set_state("subscriptions", len(self._subs))
        return f'UNS "{get_text}",yes\r'.encode()

    def _answer(self, get_text: str) -> bytes | None:
        """The current value a GET produces, or None when the GET is not one
        the processor subscribes (section 9's tables)."""
        text = get_text.strip()
        m = _GA_RE.match(text)
        if m:
            reply = self._get_module(m.group(2), _split_path(m.group(3)))
            return reply if reply and reply[0] != NAK else None
        if text.upper() == "GS":
            return f"S {self._parameter_set:x}\r".encode()
        m = re.match(rf"^GG\s*{_HEX}$", text)
        if m:
            reply = self._gg_line(int(m.group(1), 16))
            return reply or None
        m = re.match(rf"^GN\s*{_HEX}$", text)
        if m:
            reply = self._gn_line(int(m.group(1), 16))
            return reply or None
        m = re.match(rf"^GV\s*{_HEX},{_HEX}$", text)
        if m:
            key = (m.group(1).lower(), m.group(2).lower())
            return f"GV {key[0]},{key[1]},{self._io_level.get(key, 0x78):x}\r".encode()
        m = re.match(rf"^GM\s*{_HEX},{_HEX}$", text)
        if m:
            key = (m.group(1).lower(), m.group(2).lower())
            return f"GM {key[0]},{key[1]},{'M' if self._io_mute.get(key, False) else 'U'}\r".encode()
        return None

    def _gg_line(self, n: int) -> bytes:
        g = self._groups.get(n)
        if g is None:
            return b""
        value = self._group_source[n] if g.kind == "selector" else self._group_level[n]
        return f"GG {n:x},{value:x}\r".encode()

    def _gn_line(self, n: int) -> bytes:
        g = self._groups.get(n)
        if g is None or g.kind != "level":
            return b""
        return f"GN {n:x},{'M' if self._group_mute[n] else 'U'}\r".encode()

    def _store(self, key: Key | None, value: Any, own: bool) -> None:
        if key is None:
            return
        changed = self._values.get(key) != value
        self._values[key] = value
        self._mirror_to_ui(key, value)
        if changed:
            ctl = self._ctl[key]
            name = next(m.name for m in self._modules if m.name.lower() == key[0])
            self._notify(f'GA "{name}">{">".join(key[1])}', own=own)
            if ctl.fmt == FMT_ONOFF and len(key[1]) == 2 and key[1][0] == "4" and key[1][1].startswith("("):
                # A cross-point change shows up on its input's routing mask.
                i = key[1][1].strip("()").split(",")[0]
                self._notify(f'GA "{name}">3>{i}', own=own)

    def _notify(self, get_text: str, own: bool) -> None:
        """Push the GET's current value to subscribers. ``own`` marks a change
        made by serial command, which the document says is not notified."""
        if own and not self._echo_own:
            return
        if _norm(get_text) not in self._subs:
            return
        value = self._answer(get_text)
        if value:
            self._schedule_push(value)

    def _schedule_push(self, data: bytes) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._push_later(data))

    async def _push_later(self, data: bytes) -> None:
        await asyncio.sleep(0)  # let the ACK / reply go out first
        await self.push(data)

    # ── Room combine ──

    def _room_combine(self, n: int, a: int, b: int, join: bool) -> None:
        groups = self._rc[n]
        ga = next(g for g in groups if a in g)
        gb = next(g for g in groups if b in g)
        if join:
            if ga is not gb:
                ga |= gb
                groups.remove(gb)
        elif ga is gb:
            ga.discard(b)
            groups.append({b})
        self._rc[n] = sorted(groups, key=lambda g: min(g))

    def join_rooms(self, n: int, a: int, b: int) -> None:
        """Test / UI hook: a partition moved at the wall controller."""
        self._room_combine(n, a, b, True)

    # ── Meters ──

    @staticmethod
    def _wander(levels: list[int]) -> None:
        for i, v in enumerate(levels):
            levels[i] = max(0, min(0x78, v + random.randint(-4, 4)))

    # ── Simulator UI bridge and test hooks ──

    def _mirror_to_ui(self, key: Key, value: Any) -> None:
        for ui_key, (name, prop) in self._ui_map.items():
            if self._find(name, prop) == key:
                super().set_state(ui_key, value)

    def set_state(self, key: str, value: Any) -> None:
        target = getattr(self, "_ui_map", {}).get(key)
        if target is not None:
            dkey = self._find(*target)
            if dkey is not None:
                super().set_state(key, value)
                ctl = self._ctl[dkey]
                if ctl.fmt in (FMT_ONOFF, FMT_LOGIC):
                    value = bool(value)
                elif ctl.fmt == FMT_INT:
                    value = int(value)
                elif ctl.fmt in (FMT_LEVEL, FMT_NUMBER):
                    value = float(value)
                self._store(dkey, value, own=False)
                return
        if key == "push_supported":
            self._push_ok = bool(value)
        super().set_state(key, value)

    def set_value(self, name: str, prop: str, value: Any) -> bool:
        """Test hook: a change made at the processor (Designer, a CC-64);
        subscribers hear about it."""
        key = self._find(name, prop)
        if key is None:
            return False
        self._store(key, value, own=False)
        return True

    def set_parameter_set(self, n: int) -> None:
        """Test hook: a parameter set recalled from a wall controller."""
        self._parameter_set = n
        super().set_state("parameter_set", n)
        self._notify("GS", own=False)

    def set_group_level(self, n: int, raw: int) -> None:
        if n in self._group_level:
            self._group_level[n] = raw
            self._notify(f"GG {n:x}", own=False)

    def set_group_mute(self, n: int, muted: bool) -> None:
        if n in self._group_mute:
            self._group_mute[n] = muted
            self._notify(f"GN {n:x}", own=False)

    async def on_client_connected(self, client_id: str) -> bytes | None:
        return None

    async def stop(self) -> None:
        await super().stop()


def _nak(code: str) -> bytes:
    return bytes([NAK]) + code.encode()


def _norm(get_text: str) -> str:
    return re.sub(r"\s+", " ", get_text.strip()).lower()


def _split_path(text: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in text.split(">") if p.strip())


def _split_commands(line: str) -> list[str]:
    """Several module commands on one line are separated by semicolons
    (section 3), which may also appear inside a quoted label."""
    out: list[str] = []
    cur: list[str] = []
    in_quote = False
    for ch in line:
        if ch == '"':
            in_quote = not in_quote
        if ch == ";" and not in_quote:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out
