"""
tvONE CORIOmaster Simulator.

Implements the CORIOmax command-line API subset the driver exercises:

  - login(<user>,<pass>) gate — every other command answers !Failed until a
    login succeeds. Any credentials are accepted EXCEPT a username or
    password of "invalid" (the platform's designated bad-credential
    sentinel), which is rejected so the driver's auth-failure path is
    testable.
  - AddEvents/RemoveEvents/ListEvents — the sim tracks the armed
    categories and emits documented "!Event <category>,<event>,<detail>"
    lines when state changes (window source, preset take, canvas audio,
    output cut-to-black, media transport, storyboard run). Tests can also
    inject events via the emit_* helpers.
  - Slot enumeration for a C3-540-style build: a DVI input card in slot 4,
    a Streaming Media card in slot 5, DVI/HDMI output cards in slots
    13-14; per-port dumps, CutToBlack read/write, ActiveQueue transport.
  - Windows / Canvases dumps and property reads/writes. Window 3 is FREE
    (unconfigured) so the driver's roster filter is testable; canvas 2
    likewise.
  - Preset.Take (read/write), Preset.Read / NameRead / SaveRead(), and
    Preset.PresetList(); Stbds storyboard dump + Take(); playlists.
  - System.Status / API_Version / Unit_Description / StandbyMode and the
    CORIOmax model dump.

Replies use the documented shapes: long-form property paths, "!Done <echo>"
success terminals, "!Failed <echo>" failure terminals, and the login
confirmation "!Info : User <name> Logged In".
"""

from __future__ import annotations

import asyncio
import logging
import re

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

# Installed cards: slot -> (card type, port kind, port count)
CARDS: dict[int, tuple[str, str, int]] = {
    4: ("DVI_U 2-in", "in", 2),
    5: ("MEDIA_4K IN", "in", 2),
    13: ("DVI 2-out", "out", 2),
    14: ("HDMI 2-out", "out", 2),
}
SLOT_COUNT = 16
MEDIA_SLOTS = {5}

# Window table: number -> (status, alias, canvas). FREE rows have no config.
WINDOWS: dict[int, tuple[str, str, str]] = {
    1: ("IN USE", "Presenter", "Canvas1"),
    2: ("IN USE", "NULL", "Canvas1"),
    3: ("FREE", "NULL", "NULL"),
}
CANVASES: dict[int, tuple[str, str]] = {
    1: ("IN USE", "MainWall"),
    2: ("FREE", "NULL"),
}
STORYBOARDS: dict[int, str] = {1: "start", 2: ""}
PLAYLISTS: dict[int, str] = {1: "Loop_A", 2: ""}

_RE_LOGIN = re.compile(r"(?i)^login\(([^,]*),(.*)\)$")
_RE_ADD_EVENTS = re.compile(r"(?i)^addevents\((\w+)\)$")
_RE_REMOVE_EVENTS = re.compile(r"(?i)^removeevents\((\w+)\)$")
_RE_PORT = re.compile(r"(?i)^(?:(?:slots\.)?slot(\d+)\.(in|out)(\d+)|s(\d+)(i|o)(\d+))")
_RE_WINDOW = re.compile(r"(?i)^(?:routing\.)?(?:windows\.)?window(\d+)")
_RE_CANVAS = re.compile(r"(?i)^(?:routing\.)?(?:canvases\.)?canvas(\d+)")


class TvoneCoriomasterSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "tvone_coriomaster",
        "name": "tvONE CORIOmaster Simulator",
        "category": "video",
        "transport": "tcp",
        "default_port": 10001,
        "delimiter": "\r\n",
        "initial_state": {
            "logged_in": False,
            "unit_description": "CORIOmaster",
            "api_version": "4.11.13075",
            "system_status": "Serving",
            "standby": False,
            "active_preset": 1,
            "win_1_input": "s4i1",
            "win_2_input": "s5i1",
            "win_1_x": 0, "win_1_y": 0,
            "win_1_width": 1920, "win_1_height": 1080,
            "win_1_zorder": 1, "win_1_ftb": 0,
            "win_2_x": 960, "win_2_y": 540,
            "win_2_width": 960, "win_2_height": 540,
            "win_2_zorder": 2, "win_2_ftb": 0,
            "cv_1_stbd": "",
            "cv_1_volume": 100,
            "cv_1_mute": False,
            "cv_1_mode": "FromSource",
            "cv_1_source": "Slot4.In1",
            "cv_1_follow": 1,
            "ctb_13_1": False, "ctb_13_2": False,
            "ctb_14_1": False, "ctb_14_2": False,
            "sig_4_1": "OK", "sig_4_2": "INVALID",
            "mq_5_1_status": "Idle", "mq_5_1_item": 1, "mq_5_1_mode": "Single",
            "mq_5_2_status": "Idle", "mq_5_2_item": 1, "mq_5_2_mode": "Single",
        },
        "controls": [
            {"type": "indicator", "key": "unit_description", "label": "Unit Name"},
            {"type": "indicator", "key": "active_preset", "label": "Active Preset"},
            {"type": "toggle", "key": "logged_in", "label": "Session Logged In"},
            {"type": "indicator", "key": "win_1_input", "label": "Window 1 Source"},
            {"type": "toggle", "key": "ctb_13_1", "label": "Out s13o1 Cut to Black"},
            {"type": "select", "key": "sig_4_1", "label": "Input s4i1 Signal",
             "options": ["OK", "INVALID"]},
            {"type": "indicator", "key": "mq_5_1_status", "label": "Media s5i1 Playback"},
            {"type": "toggle", "key": "standby", "label": "Standby"},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._line_mode = True
        self._cards: dict[int, tuple[str, str, int]] = dict(CARDS)
        self._windows: dict[int, tuple[str, str, str]] = dict(WINDOWS)
        self._canvases: dict[int, tuple[str, str]] = dict(CANVASES)
        self._storyboards: dict[int, str] = dict(STORYBOARDS)
        self._playlists: dict[int, str] = dict(PLAYLISTS)
        # Preset store: id -> {"name": str, "windows": {n: input}}
        self._presets: dict[int, dict] = {
            1: {"name": "start", "windows": {1: "s4i1", 2: "s5i1"}},
            2: {"name": "two_up", "windows": {1: "s5i2", 2: "s4i2"}},
        }
        self._edit_preset: int = 1
        # Armed event categories (per the ONE documented control connection).
        self._event_categories: set[str] = set()
        # Test hook: called synchronously with each emitted event's bytes.
        self.on_event = None

    # ── Helpers ──

    @staticmethod
    def _done(echo: str) -> bytes:
        return f"!Done {echo}\r\n".encode()

    @staticmethod
    def _failed(echo: str) -> bytes:
        return f"!Failed {echo}\r\n".encode()

    def _reply(self, lines: list[str], echo: str) -> bytes:
        body = "".join(f"{ln}\r\n" for ln in lines)
        return body.encode() + self._done(echo)

    def _emit(self, category: str, body: str) -> None:
        """Send '!Event <body>' to the armed control connection."""
        if category.upper() not in self._event_categories:
            return
        line = f"!Event {body}\r\n".encode()
        if self.on_event is not None:
            self.on_event(line)
        push = getattr(self, "push", None)
        if push is not None:
            try:
                asyncio.get_running_loop().create_task(push(line))
            except RuntimeError:
                pass

    # ── Test helpers (documented event shapes, injectable directly) ──

    def emit_input_status(self, slot: int, port: int, status: str) -> None:
        self.set_state(f"sig_{slot}_{port}", status)
        self._emit("INPUT", f"INPUT,STATUS_GROUP,Slot{slot}.In{port},Status,{status}")

    def emit_window_input(self, window: int, source_long: str) -> None:
        self._emit("WINDOW", f"WINDOW,INPUT,Window{window},{source_long}")

    def emit_media_status(self, slot: int, port: int, status: str, item: int) -> None:
        self.set_state(f"mq_{slot}_{port}_status", status)
        self.set_state(f"mq_{slot}_{port}_item", item)
        self._emit(
            "MEDIA_PLAYER",
            f"MEDIA_PLAYER,STATUS_UPDATE,Slot{slot}.In{port},{status},{item}",
        )

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

        # ── Events ──
        m = _RE_ADD_EVENTS.match(line)
        if m:
            self._event_categories.add(m.group(1).upper())
            return self._done(line)
        m = _RE_REMOVE_EVENTS.match(line)
        if m:
            self._event_categories.discard(m.group(1).upper())
            return self._done(line)
        if low == "listevents()":
            lines = sorted(self._event_categories)
            return self._reply(lines, "ListEvents()")

        # ── Slots enumeration ──
        if low == "slots":
            lines = []
            for n in range(1, SLOT_COUNT + 1):
                if n in self._cards:
                    lines.append(f"Slots.Slot{n} = <...>")
                else:
                    lines.append(f"Slots.Slot{n} = NO CARD")
            return self._reply(lines, "Slots")

        # ── Slot dump ──
        m = re.fullmatch(r"(?i)(?:slots\.)?slot(\d+)", low)
        if m:
            n = int(m.group(1))
            if n not in self._cards:
                return self._failed(line)
            card_type, kind, count = self._cards[n]
            word = "In" if kind == "in" else "Out"
            lines = [f"Slot{n}.Cardtype = {card_type}"]
            if n in MEDIA_SLOTS:
                lines.append(f"Slot{n}.Status = READY")
                lines.append(f"Slot{n}.OperatingMode = Standard")
            for p in range(1, count + 1):
                lines.append(f"Slot{n}.{word}{p} = <...>")
            return self._reply(lines, f"Slot{n}")

        # ── ActiveQueue (media transport) — before generic port handling ──
        m = re.match(
            r"(?i)^(?:(?:slots\.)?slot(\d+)\.in(\d+)|s(\d+)i(\d+))\.activequeue",
            line,
        )
        if m:
            slot = int(m.group(1) or m.group(3))
            port = int(m.group(2) or m.group(4))
            if slot not in MEDIA_SLOTS or not self._port_exists("in", slot, port):
                return self._failed(line)
            rest = line[m.end():].strip()
            return self._queue_request(line, slot, port, rest)

        # ── Port dump / property reads and writes ──
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

        # ── Windows ──
        if low == "windows":
            lines = [f"Windows.Window{n} = <...>" for n in sorted(self._windows)]
            return self._reply(lines, "Windows")
        m = _RE_WINDOW.match(line)
        if m:
            n = int(m.group(1))
            if n not in self._windows:
                return self._failed(line)
            rest = line[m.end():].strip()
            return self._window_request(line, n, rest)

        # ── Canvases ──
        if low == "canvases":
            lines = [f"Canvases.Canvas{n} = <...>" for n in sorted(self._canvases)]
            return self._reply(lines, "Canvases")
        m = _RE_CANVAS.match(line)
        if m:
            n = int(m.group(1))
            if n not in self._canvases:
                return self._failed(line)
            rest = line[m.end():].strip()
            return self._canvas_request(line, n, rest)

        # ── Storyboards ──
        if low == "stbds":
            lines = []
            for n, name in sorted(self._storyboards.items()):
                lines.append(f"Stbds.Stbd{n}.Name = {name if name else 'NULL'}")
                lines.append(f"Stbds.Stbd{n}.Canvas = Canvas1")
                lines.append(f"Stbds.Stbd{n}.IsCurrent = No")
            return self._reply(lines, "Stbds")
        m = re.fullmatch(r"(?i)(?:routing\.)?stbds\.stbd(\d+)\.take\(\)", line.strip())
        if m:
            n = int(m.group(1))
            if n not in self._storyboards or not self._storyboards[n]:
                return self._failed(line)
            self.set_state("cv_1_stbd", f"Stbd{n}")
            self._emit("CANVAS", f"CANVAS,STBDCURRENT_CHANGED,Canvas1,Stbd{n}")
            return self._done(line)

        # ── Playlists ──
        if low == "resources.playlists":
            lines = [
                f"Resources.Playlists.Playlist{n} = <...>"
                for n in sorted(self._playlists)
            ]
            return self._reply(lines, "Resources.Playlists")
        m = re.fullmatch(
            r"(?i)resources\.playlists\.playlist(\d+)\.name", line.strip()
        )
        if m:
            n = int(m.group(1))
            if n not in self._playlists:
                return self._failed(line)
            name = self._playlists[n]
            return self._reply(
                [f'Resources.Playlists.Playlist{n}.Name = "{name}"' if name
                 else f"Resources.Playlists.Playlist{n}.Name = NULL"],
                f"Resources.Playlists.Playlist{n}.Name",
            )

        # ── Presets ──
        m = re.fullmatch(r"(?i)(?:routing\.)?preset\.take\s*=\s*(\d+)", line.strip())
        if m:
            pid = int(m.group(1))
            preset = self._presets.get(pid)
            if preset is None:
                return self._failed(line)
            for win, source in preset["windows"].items():
                if win in self._windows and self._windows[win][0] != "FREE":
                    self.set_state(f"win_{win}_input", source)
            self.set_state("active_preset", pid)
            self._emit("PRESET", f"PRESET,TAKE,{pid}")
            self._emit("PRESET", f"PRESET,COMPLETE,{pid}")
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
                self._edit_preset, {"name": "", "windows": {}}
            )
            entry["name"] = m.group(1)
            return self._done(line)
        if low in ("preset.saveread()", "routing.preset.saveread()"):
            entry = self._presets.setdefault(
                self._edit_preset, {"name": "", "windows": {}}
            )
            entry["windows"] = {
                n: self.state[f"win_{n}_input"]
                for n, (status, _a, _c) in self._windows.items()
                if status != "FREE"
            }
            self._emit("PRESET", f"PRESET,SAVE,{self._edit_preset}")
            return "// Preset(s) saved.\r\n".encode() + self._done("Preset.SaveRead()")
        if low in ("preset.presetlist()", "routing.preset.presetlist()"):
            lines = [
                f"Routing.Preset.PresetList[{pid}]={info['name']},Canvas1,1000"
                for pid, info in sorted(self._presets.items())
            ]
            return self._reply(lines, "Preset.PresetList()")

        # ── CORIOmax / System ──
        if low == "coriomax":
            return self._reply(
                [
                    "CORIOmax.Model_Name = CORIOmaster",
                    "CORIOmax.Model_Number = C3-540",
                    "CORIOmax.Serial_Number = 2218031005149",
                    "CORIOmax.Software_Name = CORIOmaster",
                    "CORIOmax.Software_Version = M411 Master",
                ],
                "CORIOmax",
            )
        if low == "coriomax.model_name":
            return self._reply(["CORIOmax.Model_Name = CORIOmaster"],
                               "CORIOmax.Model_Name")
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
        if low == "system.standbymode":
            value = "On" if self.state["standby"] else "Off"
            return self._reply(
                [f"System.StandbyMode = {value}"], "System.StandbyMode"
            )
        m = re.fullmatch(r"(?i)system\.standbymode\s*=\s*(on|off)", line.strip())
        if m:
            self.set_state("standby", m.group(1).lower() == "on")
            return self._done(line)

        if low == "logout":
            self.set_state("logged_in", False)
            return "!Info : User admin Logged Out\r\n".encode()

        return self._failed(line)

    # ── Windows / canvases ──

    def _window_request(self, line: str, n: int, rest: str) -> bytes:
        status, alias, canvas = self._windows[n]
        base = f"Window{n}"
        if status == "FREE":
            props = {"FullName": base, "Status": status, "Alias": "NULL"}
        else:
            props = {
                "FullName": base,
                "Status": status,
                "Alias": alias,
                "Input": self._long_input(self.state[f"win_{n}_input"]),
                "Canvas": canvas,
                "CanWidth": self.state[f"win_{n}_width"],
                "CanHeight": self.state[f"win_{n}_height"],
                "CanXCentre": self.state[f"win_{n}_x"],
                "CanYCentre": self.state[f"win_{n}_y"],
                "Zorder": self.state[f"win_{n}_zorder"],
                "FTB": self.state[f"win_{n}_ftb"],
            }

        if not rest:
            lines = [f"{base}.{k} = {v}" for k, v in props.items()]
            return self._reply(lines, base)

        # Property write: ".Input = Slot4.In1", ".CanXCentre = 100", ...
        m = re.fullmatch(r"\.(\w+)\s*=\s*(.+?)\s*", rest)
        if m and status != "FREE":
            prop, value = m.group(1), m.group(2).strip()
            prop_l = prop.lower()
            if prop_l == "input":
                ref = re.fullmatch(
                    r"(?i)(?:slots\.)?slot(\d+)\.in(\d+)", value
                )
                if not ref or not self._port_exists(
                    "in", int(ref.group(1)), int(ref.group(2))
                ):
                    return self._failed(line)
                compact = f"s{int(ref.group(1))}i{int(ref.group(2))}"
                self.set_state(f"win_{n}_input", compact)
                self._emit("WINDOW", f"WINDOW,INPUT,Window{n},{value}")
                return self._done(line)
            int_map = {
                "canxcentre": (f"win_{n}_x", -8192, 8191),
                "canycentre": (f"win_{n}_y", -8192, 8191),
                "canwidth": (f"win_{n}_width", 0, 16383),
                "canheight": (f"win_{n}_height", 0, 16383),
                "zorder": (f"win_{n}_zorder", 0, 15),
                "ftb": (f"win_{n}_ftb", 0, 256),
            }
            entry = int_map.get(prop_l)
            if entry:
                key, lo, hi = entry
                try:
                    ivalue = int(value)
                except ValueError:
                    return self._failed(line)
                if not lo <= ivalue <= hi:
                    return self._failed(line)
                self.set_state(key, ivalue)
                return self._done(line)
            return self._failed(line)

        # Property read: ".Input", ".Status", ...
        m = re.fullmatch(r"\.(\w+)", rest)
        if m:
            for k, v in props.items():
                if k.lower() == m.group(1).lower():
                    return self._reply([f"{base}.{k} = {v}"], f"{base}.{k}")
            return self._failed(line)

        return self._failed(line)

    def _canvas_request(self, line: str, n: int, rest: str) -> bytes:
        status, alias = self._canvases[n]
        base = f"Canvas{n}"
        if status == "FREE":
            props = {"FullName": base, "Status": status, "Alias": "NULL"}
        else:
            stbd = self.state["cv_1_stbd"]
            props = {
                "FullName": base,
                "Status": status,
                "Alias": alias,
                "WindowList": ",".join(
                    f"Window{w}" for w, (st, _a, cv) in sorted(self._windows.items())
                    if st != "FREE" and cv == base
                ),
                "StbdCurrent": stbd if stbd else "NULL",
                "AudioFollowWindow": self.state["cv_1_follow"],
                "AudioMute": "On" if self.state["cv_1_mute"] else "Off",
                "AudioSource": self.state["cv_1_source"],
                "AudioMode": self.state["cv_1_mode"],
                "AudioVolume": self.state["cv_1_volume"],
            }

        if not rest:
            lines = [f"{base}.{k} = {v}" for k, v in props.items()]
            return self._reply(lines, base)

        m = re.fullmatch(r"\.(\w+)\s*=\s*(.+?)\s*", rest)
        if m and status != "FREE":
            prop, value = m.group(1), m.group(2).strip()
            prop_l = prop.lower()
            if prop_l == "audiovolume":
                try:
                    ivalue = int(value)
                except ValueError:
                    return self._failed(line)
                if not 0 <= ivalue <= 100:
                    return self._failed(line)
                self.set_state("cv_1_volume", ivalue)
                self._emit(
                    "CANVAS", f"CANVAS,PROPERTY_CHANGED,{base},AudioVolume,{ivalue}"
                )
                return self._done(line)
            if prop_l == "audiomute":
                if value.lower() not in ("on", "off"):
                    return self._failed(line)
                self.set_state("cv_1_mute", value.lower() == "on")
                self._emit(
                    "CANVAS",
                    f"CANVAS,PROPERTY_CHANGED,{base},AudioMute,{value.title()}",
                )
                return self._done(line)
            if prop_l == "audiomode":
                if value.lower() not in ("fromsource", "followwindow"):
                    return self._failed(line)
                mode = "FromSource" if value.lower() == "fromsource" else "FollowWindow"
                self.set_state("cv_1_mode", mode)
                self._emit(
                    "CANVAS", f"CANVAS,PROPERTY_CHANGED,{base},AudioMode,{mode}"
                )
                return self._done(line)
            if prop_l == "audiosource":
                ref = re.fullmatch(r"(?i)(?:slots\.)?slot(\d+)\.in(\d+)", value)
                if not ref or not self._port_exists(
                    "in", int(ref.group(1)), int(ref.group(2))
                ):
                    return self._failed(line)
                long = f"Slot{int(ref.group(1))}.In{int(ref.group(2))}"
                self.set_state("cv_1_source", long)
                self._emit(
                    "CANVAS", f"CANVAS,PROPERTY_CHANGED,{base},AudioSource,{long}"
                )
                return self._done(line)
            if prop_l == "audiofollowwindow":
                try:
                    win = int(value)
                except ValueError:
                    return self._failed(line)
                self.set_state("cv_1_follow", win)
                self._emit(
                    "CANVAS",
                    f"CANVAS,PROPERTY_CHANGED,{base},AudioFollowWindow,Window{win}",
                )
                return self._done(line)
            return self._failed(line)

        m = re.fullmatch(r"\.(\w+)", rest)
        if m:
            for k, v in props.items():
                if k.lower() == m.group(1).lower():
                    return self._reply([f"{base}.{k} = {v}"], f"{base}.{k}")
            return self._failed(line)

        return self._failed(line)

    # ── Ports ──

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
                "Status": self.state.get(f"sig_{slot}_{port}", "OK"),
                "Alias": f"s{slot}i{port}",
                "Measured_Resolution": "1920x1080p60",
                "HDMI": "Found",
                "Audio": "Found",
            }
        else:
            props = {
                "FullName": f"{word}{port}",
                "Status": "OK",
                "Alias": f"s{slot}o{port}",
                "Resolution": "1920x1080p60",
                "InsList": self._inslist_value(slot, port),
                "CutToBlack": "On" if self.state[f"ctb_{slot}_{port}"] else "Off",
            }

        if not rest:
            lines = [f"{base}.{k} = {v}" for k, v in props.items()]
            return self._reply(lines, base)

        m = re.fullmatch(r"(?i)\.cuttoblack\s*=\s*(on|off)", rest)
        if m and kind == "out":
            on = m.group(1).lower() == "on"
            self.set_state(f"ctb_{slot}_{port}", on)
            self._emit(
                "OUTPUT",
                f"OUTPUT,PROPERTY_CHANGED,Slot{slot}.Out{port},CutToBlack,"
                f"{'On' if on else 'Off'}",
            )
            return self._done(line)

        m = re.fullmatch(r"\.(\w+)", rest)
        if m:
            for k, v in props.items():
                if k.lower() == m.group(1).lower():
                    return self._reply([f"{base}.{k} = {v}"], f"{base}.{k}")
            return self._failed(line)

        return self._failed(line)

    def _inslist_value(self, slot: int, port: int) -> str:
        """Outputs mirror the sources windowed onto canvas 1 (both modeled
        output cards drive that canvas in this build)."""
        sources = []
        for n, (status, _a, canvas) in sorted(self._windows.items()):
            if status != "FREE" and canvas == "Canvas1":
                long = self._long_input(self.state[f"win_{n}_input"])
                if long not in sources:
                    sources.append(long)
        return ",".join(sources) if sources else "NULL"

    @staticmethod
    def _long_input(compact: str) -> str:
        m = re.fullmatch(r"s(\d+)i(\d+)", compact)
        return f"Slot{m.group(1)}.In{m.group(2)}" if m else compact

    # ── Media transport ──

    def _queue_request(self, line: str, slot: int, port: int, rest: str) -> bytes:
        key = f"mq_{slot}_{port}"
        base = f"Slot{slot}.In{port}.ActiveQueue"
        low = rest.lower()

        methods = {
            ".play()": "Playing",
            ".pause()": "Paused",
            ".stop()": "Idle",
        }
        if low in methods:
            status = methods[low]
            if low == ".stop()":
                self.set_state(f"{key}_item", 1)
            self.set_state(f"{key}_status", status)
            self._emit(
                "MEDIA_PLAYER",
                f"MEDIA_PLAYER,STATUS_UPDATE,Slot{slot}.In{port},{status},"
                f"{self.state[f'{key}_item']}",
            )
            return self._done(line)
        if low in (".skipforward()", ".skipbackward()"):
            item = self.state[f"{key}_item"] + (1 if "forward" in low else -1)
            if item < 1:
                return self._failed(line)
            self.set_state(f"{key}_item", item)
            self._emit(
                "MEDIA_PLAYER",
                f"MEDIA_PLAYER,STATUS_UPDATE,Slot{slot}.In{port},"
                f"{self.state[f'{key}_status']},{item}",
            )
            return self._done(line)
        m = re.fullmatch(r'(?i)\.loadplaylist\("([^"]+)"\)', rest)
        if m:
            wanted = m.group(1).lower()
            for n, name in self._playlists.items():
                if name and wanted == f"resources.playlists.playlist{n}":
                    self.set_state(f"{key}_status", "Configured")
                    self.set_state(f"{key}_item", 1)
                    return self._done(line)
            return self._failed(line)
        m = re.fullmatch(r"(?i)\.playmode\s*=\s*(single|repeat)", rest)
        if m:
            self.set_state(f"{key}_mode", m.group(1).title())
            return self._done(line)
        if low == ".status":
            return self._reply(
                [f"{base}.Status = {self.state[f'{key}_status']}"], f"{base}.Status"
            )
        if low == ".currentindex":
            return self._reply(
                [f"{base}.CurrentIndex = {self.state[f'{key}_item']}"],
                f"{base}.CurrentIndex",
            )
        if low == ".playmode":
            return self._reply(
                [f"{base}.PlayMode = {self.state[f'{key}_mode']}"], f"{base}.PlayMode"
            )
        return self._failed(line)
