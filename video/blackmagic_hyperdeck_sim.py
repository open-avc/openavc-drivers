"""
Blackmagic HyperDeck simulator.

The server side of the HyperDeck Ethernet Protocol (December 2024): the
``500 connection info:`` greeting, one answer per command (a one-line ``200
ok`` or ``1xx`` refusal, or a ``2xx`` block ended by a blank line), the
multiline command form, and 5xx notices to each connection that asked for them
with ``notify``.

Kept per connection, as the document describes: the notify flags, Remote
Override, the sign-in and a multiline command still being read.

From the device's config:
  * ``username`` / ``password``: with a username set, every command is
    answered ``122 authentication required`` until the connection signs in
    with ``authenticate:``; a wrong username, or ``invalid`` as either, is
    refused ``121 authentication failed``.
  * ``ssl``: served over TLS with a throwaway self-signed certificate.
  * ``max_clients``: a connection beyond it is answered ``120 connection
    failed`` and closed. The error mode "All connections in use" does the same
    for every new connection.

A 1080p30 deck with two SD slots: slot 1 holds four clips, slot 2 none.
Recording and playback move the timecode once a second.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

FPS = 30
SLOTS = 2
# Bytes a second of recording takes (ProRes HQ 1080p30, about 220 Mb/s).
BYTES_PER_SECOND = 27_500_000
SLOT_SIZE = 256_000_000_000

LOCAL = "_local"

TRANSPORT_KEYS = (
    "transport_status", "speed", "active_slot", "clip_id", "single_clip",
    "loop", "video_format", "input_video_format", "dynamic_range",
    "reference_locked",
)
# Moving every second: read by the poll, never pushed (the display timecode
# notice is not turned on by the driver, and its form is not documented).
QUIET_KEYS = ("timecode", "display_timecode")
SLOT_FIELDS = (
    "status", "slot_name", "device_name", "volume_name", "recording_time",
    "video_format", "blocked", "remaining_size", "total_size",
)
QUIET_SLOT_FIELDS = ("recording_time", "remaining_size")

# configuration parameter -> state key, and the values each takes.
CONFIG = {
    "audio input": ("audio_input", ["embedded", "XLR", "RCA"]),
    # A Studio HD Plus has SDI and HDMI inputs.
    "video input": ("video_input", ["SDI", "HDMI"]),
    "file format": ("file_format", [
        "QuickTimeProResHQ", "QuickTimeProRes", "QuickTimeProResLT", "QuickTimeProResProxy",
        "H.264High", "H.264Medium", "H.264Low", "DNxHD220x", "QuickTimeDNxHD220x",
        "DNxHD145", "QuickTimeDNxHD145", "DNxHD45", "QuickTimeDNxHD45",
    ]),
    "audio codec": ("audio_codec", ["PCM", "AAC"]),
    "timecode input": ("timecode_input", ["external", "embedded", "internal", "preset", "clip"]),
    "timecode output": ("timecode_output", ["clip", "timeline"]),
    "timecode preference": ("timecode_preference", ["default", "dropframe", "nondropframe"]),
    "timecode preset": ("timecode_preset", None),
    "audio input channels": ("audio_input_channels", None),
    "record trigger": ("record_trigger", ["none", "recordbit", "timecoderun"]),
    "record prefix": ("record_prefix", None),
    "record cache": ("record_cache", ["true", "false"]),
    "append timestamp": ("append_timestamp", ["true", "false"]),
    "reference source": ("reference_source", ["auto", "input", "external"]),
    "genlock input resync": ("genlock_input_resync", ["true", "false"]),
    "usb spill": ("usb_spill", ["true", "false"]),
    "default standard": ("default_standard", None),
}
CONFIG_KEYS = tuple(key for key, _values in CONFIG.values())
NOTIFY_FLAGS = (
    "transport", "slot", "remote", "configuration", "dropped frames",
    "display timecode", "timeline position", "playrange", "cache",
    "dynamic range", "slate", "clips", "disk", "device info", "nas",
)
# Commands that change nothing, answered with Remote off too.
READ_ONLY = {
    "ping", "quit", "help", "?", "commands", "device info", "transport info",
    "slot info", "clips count", "clips get", "clip info", "disk list",
    "notify", "remote", "watchdog", "authenticate", "uptime",
}
# Every parameter name the server reads, longest first, so "clip id" is
# found before "clip".
PARAM_NAMES = sorted({
    "clip id", "clip", "timeline", "timecode", "slot id", "speed", "loop",
    "single clip", "name", "spill", "enable", "override", "version", "count",
    "device", "video format", "period", "username", "password",
    *NOTIFY_FLAGS, *CONFIG,
}, key=len, reverse=True)
_PARAM = re.compile(
    r"(?:^|(?<=\s))(" + "|".join(re.escape(name) for name in PARAM_NAMES) + r"):(?=\s|$)"
)


def _word(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _tc(frames: int) -> str:
    frames = max(0, int(frames))
    seconds, ff = divmod(frames, FPS)
    minutes, ss = divmod(seconds, 60)
    hh, mm = divmod(minutes, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def _frames(timecode: str) -> int | None:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}):(\d{2})", timecode.strip())
    if not match:
        return None
    hh, mm, ss, ff = (int(g) for g in match.groups())
    if mm > 59 or ss > 59 or ff >= FPS:
        return None
    return ((hh * 60 + mm) * 60 + ss) * FPS + ff


class _Session:
    """One connection's own settings."""

    def __init__(self, signed_in: bool):
        self.notify = {flag: False for flag in NOTIFY_FLAGS}
        self.override = False
        self.signed_in = signed_in
        self.refused = False
        self.multi: tuple[str, dict[str, str]] | None = None


class BlackmagicHyperDeckSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "blackmagic_hyperdeck",
        "name": "Blackmagic HyperDeck Simulator",
        "category": "video",
        "transport": "tcp",
        "default_port": 9993,
        "initial_state": {
            "model": "HyperDeck Studio HD Plus",
            "protocol_version": "1.13",
            "unique_id": "7c2e0d0a1b2c3d4e",
            "software_version": "8.5",
            "device_name": "HyperDeck",
            "transport_status": "preview",
            "speed": 0,
            "active_slot": 1,
            "clip_id": 1,
            "single_clip": False,
            "loop": False,
            "timecode": "00:00:00:00",
            "display_timecode": "00:00:00:00",
            "video_format": "1080p30",
            "input_video_format": "1080p30",
            "dynamic_range": "Rec709",
            "reference_locked": True,
            "remote_enabled": True,
            "identify": False,
            "clip_count": 4,
            "audio_input": "embedded",
            "video_input": "SDI",
            "file_format": "QuickTimeProResHQ",
            "audio_codec": "PCM",
            "timecode_input": "internal",
            "timecode_output": "timeline",
            "timecode_preference": "default",
            "timecode_preset": "00:00:00:00",
            "audio_input_channels": "2",
            "record_trigger": "none",
            "record_prefix": "HyperDeck",
            "record_cache": "false",
            "append_timestamp": "false",
            "reference_source": "auto",
            "genlock_input_resync": "false",
            "usb_spill": "false",
            "default_standard": "1080p30",
            "slot_1_status": "mounted",
            "slot_1_slot_name": "SD1",
            "slot_1_device_name": "sd1",
            "slot_1_volume_name": "Service",
            "slot_1_recording_time": 7200,
            "slot_1_video_format": "1080p30",
            "slot_1_blocked": False,
            "slot_1_remaining_size": 198_000_000_000,
            "slot_1_total_size": SLOT_SIZE,
            "slot_2_status": "mounted",
            "slot_2_slot_name": "SD2",
            "slot_2_device_name": "sd2",
            "slot_2_volume_name": "Spare",
            "slot_2_recording_time": 9300,
            "slot_2_video_format": "1080p30",
            "slot_2_blocked": False,
            "slot_2_remaining_size": SLOT_SIZE,
            "slot_2_total_size": SLOT_SIZE,
        },
        "controls": [
            {"type": "indicator", "key": "transport_status", "label": "Transport"},
            {"type": "indicator", "key": "timecode", "label": "Timecode"},
            {"type": "indicator", "key": "active_slot", "label": "Active Slot"},
            {"type": "indicator", "key": "clip_count", "label": "Clips on Timeline"},
            {"type": "toggle", "key": "remote_enabled", "label": "Remote"},
            {"type": "select", "key": "slot_1_status", "label": "Slot 1",
             "options": ["mounted", "empty", "error"]},
            {"type": "select", "key": "slot_2_status", "label": "Slot 2",
             "options": ["mounted", "empty", "error"]},
        ],
        "error_modes": {
            "connections_full": {
                "label": "All connections in use",
                "description": "New connections are answered 120 connection failed and closed.",
            },
        },
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._sessions: dict[str, _Session] = {}
        self._task_clients: dict[asyncio.Task, str] = {}
        self._clips: dict[int, list[dict[str, Any]]] = {
            1: [
                {"name": "Walk-in Loop.mov", "frames": 120 * FPS},
                {"name": "Announcements.mov", "frames": 95 * FPS},
                {"name": "Sermon 2026-11-01.mov", "frames": 2400 * FPS},
                {"name": "Closing.mov", "frames": 30 * FPS},
            ],
            2: [],
        }
        self._position = 0
        self._recorded = 0
        self._record_name = ""
        self._record_count = 0
        self._tick_task: asyncio.Task | None = None
        self._tls_files: Any = None
        self._dirty: set[tuple[str, int]] = set()
        self._flush_handle: asyncio.Handle | None = None
        self._refresh_clip_count()

    # ── Server ──

    async def start(self, port: int) -> None:
        from openavc.simulator.self_signed_tls import build_optional_tls

        context, files = build_optional_tls({"tls": bool(self.config.get("ssl"))}, None, self.name)
        if context is None:
            await super().start(port)
        else:
            # The base server, over TLS.
            self._tls_files = files
            self._server = await asyncio.start_server(
                self._handle_client, host="127.0.0.1", port=port, ssl=context,
            )
            if port == 0 and self._server.sockets:
                port = self._server.sockets[0].getsockname()[1]
            self._port = port
            self._running = True
            logger.info("%s started on port %d over TLS (driver: %s)", self.name, port, self.driver_id)
        self._tick_task = asyncio.ensure_future(self._tick_loop())

    async def stop(self) -> None:
        task, self._tick_task = self._tick_task, None
        if task is not None:
            task.cancel()
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        await super().stop()
        if self._tls_files:
            from openavc.simulator.self_signed_tls import remove_cert_files

            remove_cert_files(self._tls_files)
            self._tls_files = None

    async def _read_messages(self, reader, buffer: bytearray | None = None):
        # One line at a time, blank lines kept: a blank line ends a multiline
        # command, and the base's line reader drops it.
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30.0)
        except asyncio.TimeoutError:
            return []
        if not raw:
            return None
        return [raw.rstrip(b"\r\n")]

    async def on_client_connected(self, client_id: str) -> bytes | None:
        # Called from the connection's own task, which also runs handle_command.
        for task in [t for t in self._task_clients if t.done()]:
            self._task_clients.pop(task, None)
        for cid in [c for c in self._sessions if c not in self._clients]:
            self._sessions.pop(cid, None)
        try:
            self._task_clients[asyncio.current_task()] = client_id
        except RuntimeError:
            pass
        session = _Session(signed_in=not str(self.config.get("username") or ""))
        limit = int(self.config.get("max_clients") or 0)
        served = sum(1 for s in self._sessions.values() if not s.refused)
        if "connections_full" in self.active_errors or (limit and served >= limit):
            session.refused = True
        self._sessions[client_id] = session
        if session.refused:
            return b"120 connection failed\r\n"
        return self._block(500, "connection info", [
            ("protocol version", self.get_state("protocol_version")),
            ("model", self.get_state("model")),
        ])

    async def authenticate_client(self, reader, writer, client_id: str) -> bool:
        session = self._sessions.get(client_id)
        if session is not None and session.refused:
            # The deck closes a connection it refused.
            return False
        return True

    def _session_id(self) -> str:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return LOCAL
        return self._task_clients.get(task, LOCAL)

    def _session(self) -> _Session:
        cid = self._session_id()
        session = self._sessions.get(cid)
        if session is None:
            session = _Session(signed_in=not str(self.config.get("username") or ""))
            self._sessions[cid] = session
        return session

    # ── Notices ──

    def set_state(self, key: str, value: Any) -> None:
        old = self.get_state(key)
        super().set_state(key, value)
        if old == value:
            return
        if key in TRANSPORT_KEYS:
            self._mark("transport")
        elif key == "remote_enabled":
            self._mark("remote")
        elif key in CONFIG_KEYS:
            self._mark("configuration")
        elif key.startswith("slot_"):
            parts = key.split("_", 2)
            if len(parts) == 3 and parts[1].isdigit() and parts[2] not in QUIET_SLOT_FIELDS:
                if parts[2] == "status":
                    self._refresh_clip_count()
                self._mark("slot", int(parts[1]))

    def _mark(self, group: str, slot: int = 0) -> None:
        self._dirty.add((group, slot))
        if self._flush_handle is None:
            try:
                self._flush_handle = asyncio.get_running_loop().call_soon(self._flush)
            except RuntimeError:
                self._dirty.clear()

    def _flush(self) -> None:
        self._flush_handle = None
        dirty, self._dirty = self._dirty, set()
        for cid, session in list(self._sessions.items()):
            if cid not in self._clients or session.refused or not session.signed_in:
                continue
            out = b""
            for group, slot in sorted(dirty):
                if not session.notify.get(group):
                    continue
                if group == "transport":
                    out += self._transport_block(508)
                elif group == "slot":
                    out += self._slot_block(502, slot)
                elif group == "remote":
                    out += self._remote_block(510, session)
                elif group == "configuration":
                    out += self._configuration_block(511)
            if out:
                asyncio.ensure_future(self.push_to(cid, out))

    # ── Blocks ──

    @staticmethod
    def _block(code: int, title: str, pairs: list[tuple[str, Any]]) -> bytes:
        text = f"{code} {title}:\r\n" + "".join(f"{k}: {_word(v)}\r\n" for k, v in pairs) + "\r\n"
        return text.encode("utf-8")

    @staticmethod
    def _line(code: int, text: str) -> bytes:
        return f"{code} {text}\r\n".encode("utf-8")

    def _device_block(self) -> bytes:
        return self._block(204, "device info", [
            ("protocol version", self.get_state("protocol_version")),
            ("model", self.get_state("model")),
            ("unique id", self.get_state("unique_id")),
            ("slot count", SLOTS),
            ("software version", self.get_state("software_version")),
            ("name", self.get_state("device_name")),
        ])

    def _transport_block(self, code: int) -> bytes:
        slot = self.get_state("active_slot")
        clip = self.get_state("clip_id")
        return self._block(code, "transport info", [
            ("status", self.get_state("transport_status")),
            ("speed", self.get_state("speed")),
            ("slot id", slot if slot else "none"),
            ("slot name", self.get_state(f"slot_{slot}_slot_name", "") if slot else ""),
            ("device name", self.get_state(f"slot_{slot}_device_name", "") if slot else ""),
            ("clip id", clip if clip else "none"),
            ("single clip", self.get_state("single_clip")),
            ("display timecode", self.get_state("display_timecode")),
            ("timecode", self.get_state("timecode")),
            ("video format", self.get_state("video_format")),
            ("loop", self.get_state("loop")),
            ("timeline", 0),
            ("input video format", self.get_state("input_video_format")),
            ("dynamic range", self.get_state("dynamic_range")),
            ("reference locked", self.get_state("reference_locked")),
        ])

    def _slot_block(self, code: int, slot: int) -> bytes:
        return self._block(code, "slot info", [("slot id", slot)] + [
            (field.replace("_", " "), self.get_state(f"slot_{slot}_{field}", ""))
            for field in SLOT_FIELDS
        ])

    def _remote_block(self, code: int, session: _Session) -> bytes:
        return self._block(code, "remote info", [
            ("enabled", bool(self.get_state("remote_enabled"))),
            ("override", session.override),
        ])

    def _configuration_block(self, code: int) -> bytes:
        return self._block(code, "configuration", [
            (wire, self.get_state(key)) for wire, (key, _values) in CONFIG.items()
        ])

    # ── The timeline ──

    def _timeline(self) -> list[dict[str, Any]]:
        slot = self.get_state("active_slot")
        if not slot or self.get_state(f"slot_{slot}_status") != "mounted":
            return []
        return self._clips.get(int(slot), [])

    def _refresh_clip_count(self) -> None:
        super().set_state("clip_count", len(self._timeline()))

    def _clip_bounds(self, index: int) -> tuple[int, int]:
        start = sum(c["frames"] for c in self._timeline()[: index - 1])
        return start, start + self._timeline()[index - 1]["frames"]

    def _seek(self, frames: int) -> None:
        clips = self._timeline()
        total = sum(c["frames"] for c in clips)
        self._position = max(0, min(frames, max(total - 1, 0)))
        clip, start = None, 0
        for index, entry in enumerate(clips, start=1):
            if self._position < start + entry["frames"]:
                clip = index
                break
            start += entry["frames"]
        self.set_state("clip_id", clip)
        self.set_state("timecode", _tc(self._position))
        self.set_state("display_timecode", _tc(self._position))

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self._tick()
            except Exception:
                logger.exception("%s: timecode tick failed", self.name)

    def _tick(self) -> None:
        status = self.get_state("transport_status")
        if status == "record":
            self._recorded += FPS
            self.set_state("timecode", _tc(self._recorded))
            self.set_state("display_timecode", _tc(self._recorded))
            slot = self.get_state("active_slot")
            left = int(self.get_state(f"slot_{slot}_recording_time", 0) or 0)
            free = int(self.get_state(f"slot_{slot}_remaining_size", 0) or 0)
            self.set_state(f"slot_{slot}_recording_time", max(left - 1, 0))
            self.set_state(f"slot_{slot}_remaining_size", max(free - BYTES_PER_SECOND, 0))
        elif status in ("play", "shuttle", "forward", "rewind"):
            clips = self._timeline()
            if not clips:
                self.set_state("transport_status", "stopped")
                return
            low, high = 0, sum(c["frames"] for c in clips)
            clip = self.get_state("clip_id")
            if self.get_state("single_clip") and clip:
                low, high = self._clip_bounds(int(clip))
            target = self._position + int(FPS * int(self.get_state("speed") or 0) / 100)
            if target >= high or target < low:
                if self.get_state("loop"):
                    target = low if target >= high else high - 1
                else:
                    target = min(max(target, low), high - 1)
                    self.set_state("transport_status", "stopped")
                    self.set_state("speed", 0)
            self._seek(target)

    # ── Commands ──

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("utf-8", errors="replace")
        session = self._session()
        if session.multi is not None:
            if text.strip():
                name, _sep, value = text.partition(":")
                session.multi[1][name.strip().lower()] = value.strip()
                return None
            command, params = session.multi
            session.multi = None
            return self._run(session, command, params)
        if not text.strip():
            return None
        stripped = text.strip()
        if stripped.endswith(":") and stripped.count(":") == 1:
            # The multiline form: the parameters follow, one a line, then a
            # blank line.
            session.multi = (stripped[:-1].strip().lower(), {})
            return None
        command, sep, rest = stripped.partition(":")
        params: dict[str, str] = {}
        if sep:
            found = list(_PARAM.finditer(rest))
            if not found or rest[: found[0].start()].strip():
                return self._line(101, "unsupported parameter")
            for index, match in enumerate(found):
                end = found[index + 1].start() if index + 1 < len(found) else len(rest)
                params[match.group(1)] = rest[match.end():end].strip()
        return self._run(session, command.strip().lower(), params)

    def _run(self, session: _Session, command: str, params: dict[str, str]) -> bytes:
        if command == "authenticate":
            return self._authenticate(session, params)
        if not session.signed_in:
            return self._line(122, "authentication required")
        if (
            command not in READ_ONLY
            and not (command == "configuration" and not params)
            and not self.get_state("remote_enabled")
            and not session.override
        ):
            return self._line(111, "remote control disabled")
        handler = getattr(self, "_cmd_" + command.replace(" ", "_"), None)
        if command == "?":
            handler = self._cmd_help
        if handler is None:
            return self._line(100, "syntax error")
        try:
            return handler(session, params)
        except ValueError:
            return self._line(102, "invalid value")

    def _authenticate(self, session: _Session, params: dict[str, str]) -> bytes:
        wanted = str(self.config.get("username") or "")
        user = params.get("username", "")
        password = params.get("password", "")
        if "invalid" in (user, password) or (wanted and user != wanted):
            session.signed_in = False
            return self._line(121, "authentication failed")
        session.signed_in = True
        return self._line(200, "ok")

    def _cmd_ping(self, session, params):
        return self._line(200, "ok")

    def _cmd_quit(self, session, params):
        self._close_later(self._session_id())
        return self._line(200, "ok")

    def _cmd_watchdog(self, session, params):
        return self._line(200, "ok")

    def _cmd_help(self, session, params):
        lines = ["device info", "transport info", "slot info", "play", "record", "stop",
                 "goto", "shuttle", "preview", "notify", "remote", "configuration"]
        return ("201 help:\r\n" + "".join(line + "\r\n" for line in lines) + "\r\n").encode("utf-8")

    def _cmd_device_info(self, session, params):
        return self._device_block()

    def _cmd_transport_info(self, session, params):
        return self._transport_block(208)

    def _cmd_slot_info(self, session, params):
        slot = int(params.get("slot id") or self.get_state("active_slot") or 0)
        if not 1 <= slot <= SLOTS:
            return self._line(102, "invalid value")
        return self._slot_block(202, slot)

    def _cmd_clips_count(self, session, params):
        return self._block(214, "clips count", [("clip count", len(self._timeline()))])

    def _cmd_clips_get(self, session, params):
        version = params.get("version", "1")
        if version not in ("1", "2", "3"):
            raise ValueError(version)
        clips = self._timeline()
        lines = [f"clip count: {len(clips)}"]
        start = 0
        for index, clip in enumerate(clips, start=1):
            duration = _tc(clip["frames"])
            if version == "1":
                lines.append(f"{index}: {clip['name']} {_tc(start)} {duration}")
            else:
                lines.append(f"{index}: {_tc(start)} {duration} {_tc(0)} {duration} {clip['name']}")
            start += clip["frames"]
        return ("205 clips info:\r\n" + "".join(line + "\r\n" for line in lines) + "\r\n").encode("utf-8")

    def _cmd_remote(self, session, params):
        if not params:
            return self._remote_block(210, session)
        if "enable" in params:
            self.set_state("remote_enabled", self._flag(params["enable"]))
        if "override" in params:
            session.override = self._flag(params["override"])
            # Only this connection's own notice changes.
            if session.notify.get("remote"):
                asyncio.ensure_future(self.push_to(self._session_id(), self._remote_block(510, session)))
        return self._line(200, "ok")

    def _cmd_notify(self, session, params):
        if not params:
            return self._block(209, "notify", [(flag, session.notify[flag]) for flag in NOTIFY_FLAGS])
        for name, value in params.items():
            if name not in session.notify:
                return self._line(101, "unsupported parameter")
            session.notify[name] = self._flag(value)
        return self._line(200, "ok")

    def _cmd_configuration(self, session, params):
        if not params:
            return self._configuration_block(211)
        updates = {}
        for wire, value in params.items():
            if wire not in CONFIG:
                return self._line(101, "unsupported parameter")
            key, values = CONFIG[wire]
            if values is not None:
                match = next((v for v in values if v.lower() == value.lower()), None)
                if match is None:
                    return self._line(102, "invalid value")
                value = match
            updates[key] = value
        reboot = "file_format" in updates and updates["file_format"] != self.get_state("file_format")
        for key, value in updates.items():
            self.set_state(key, value)
        if reboot:
            # The document: a file format change may reset the deck, which
            # answers 213 and closes the connection.
            self._close_later(None)
            return self._line(213, "deck rebooting")
        return self._line(200, "ok")

    def _cmd_identify(self, session, params):
        self.set_state("identify", self._flag(params.get("enable", "")))
        return self._line(200, "ok")

    def _cmd_reboot(self, session, params):
        if self.get_state("transport_status") == "record":
            self._finish_recording()
        self.set_state("transport_status", "preview")
        self._close_later(None)
        return self._line(200, "ok")

    # ── Transport ──

    def _cmd_play(self, session, params):
        if self.get_state("transport_status") == "record":
            return self._line(150, "invalid state")
        if not self._timeline():
            return self._line(107, "timeline empty")
        if "clip id" in params:
            clip = int(params["clip id"])
            if not 1 <= clip <= len(self._timeline()):
                return self._line(112, "clip not found")
            self._seek(self._clip_bounds(clip)[0])
        speed = int(params.get("speed", 100))
        if not -5000 <= speed <= 5000:
            return self._line(109, "out of range")
        if "loop" in params:
            self.set_state("loop", self._flag(params["loop"]))
        if "single clip" in params:
            self.set_state("single_clip", self._flag(params["single clip"]))
        self.set_state("speed", speed)
        self.set_state("transport_status", "play")
        return self._line(200, "ok")

    def _cmd_stop(self, session, params):
        status = self.get_state("transport_status")
        if status == "record":
            self._finish_recording()
            self.set_state("transport_status", "preview")
        elif status in ("play", "shuttle", "jog", "forward", "rewind"):
            self.set_state("transport_status", "stopped")
        self.set_state("speed", 0)
        return self._line(200, "ok")

    def _cmd_record(self, session, params):
        if "spill" in params:
            return self._spill(params.get("slot id"))
        slot = self.get_state("active_slot")
        if not slot or self.get_state(f"slot_{slot}_status") != "mounted":
            return self._line(105, "no disk")
        if self.get_state("transport_status") != "record":
            self._record_count += 1
            prefix = self.get_state("record_prefix") or "HyperDeck"
            self._record_name = params.get("name") or f"{prefix}_{self._record_count:04d}.mov"
            self._recorded = 0
            self.set_state("timecode", _tc(0))
            self.set_state("display_timecode", _tc(0))
            self.set_state("speed", 0)
            self.set_state("transport_status", "record")
        return self._line(200, "ok")

    def _cmd_record_spill(self, session, params):
        return self._spill(params.get("slot id"))

    def _spill(self, wanted: str | None) -> bytes:
        if self.get_state("transport_status") != "record":
            return self._line(150, "invalid state")
        current = int(self.get_state("active_slot") or 1)
        target = int(wanted) if wanted else (current % SLOTS) + 1
        if not 1 <= target <= SLOTS or self.get_state(f"slot_{target}_status") != "mounted":
            return self._line(105, "no disk")
        if target != current:
            self._finish_recording()
            self.set_state("active_slot", target)
            self._recorded = 0
        return self._line(200, "ok")

    def _finish_recording(self) -> None:
        slot = int(self.get_state("active_slot") or 1)
        name = self._record_name or f"HyperDeck_{self._record_count:04d}.mov"
        self._clips.setdefault(slot, []).append({"name": name, "frames": max(self._recorded, FPS)})
        self._record_name = ""
        self._refresh_clip_count()

    def _cmd_preview(self, session, params):
        enable = self._flag(params.get("enable", ""))
        status = self.get_state("transport_status")
        if not enable and status == "record":
            return self._line(150, "invalid state")
        if enable and status != "record":
            self.set_state("speed", 0)
            self.set_state("transport_status", "preview")
        elif not enable:
            self.set_state("transport_status", "stopped")
        return self._line(200, "ok")

    def _cmd_goto(self, session, params):
        if len(params) != 1:
            return self._line(101, "unsupported parameter")
        if self.get_state("transport_status") == "record":
            return self._line(150, "invalid state")
        (name, value), = params.items()
        if name == "slot id":
            return self._cmd_slot_select(session, {"slot id": value})
        clips = self._timeline()
        if not clips:
            return self._line(107, "timeline empty")
        current = int(self.get_state("clip_id") or 1)
        if name == "clip id":
            if value in ("start", "end"):
                clip = 1 if value == "start" else len(clips)
            elif value[:1] in "+-":
                clip = current + int(value)
            else:
                clip = int(value)
                if not 1 <= clip <= len(clips):
                    return self._line(112, "clip not found")
            clip = max(1, min(clip, len(clips)))
            self._seek(self._clip_bounds(clip)[0])
        elif name in ("clip", "timeline"):
            low, high = self._clip_bounds(current) if name == "clip" else (0, sum(c["frames"] for c in clips))
            if value == "start":
                target = low
            elif value == "end":
                target = high - 1
            elif value[:1] in "+-":
                target = self._position + int(value)
            else:
                target = low + int(value)
            self._seek(target)
        elif name == "timecode":
            sign = value[:1] if value[:1] in "+-" else ""
            frames = _frames(value[1:] if sign else value)
            if frames is None:
                return self._line(102, "invalid value")
            target = self._position + frames if sign == "+" else self._position - frames if sign == "-" else frames
            self._seek(target)
        else:
            return self._line(101, "unsupported parameter")
        if self.get_state("transport_status") == "preview":
            self.set_state("transport_status", "stopped")
        return self._line(200, "ok")

    def _cmd_shuttle(self, session, params):
        if self.get_state("transport_status") == "record":
            return self._line(150, "invalid state")
        speed = int(params.get("speed", ""))
        if not -5000 <= speed <= 5000:
            return self._line(109, "out of range")
        if not self._timeline():
            return self._line(107, "timeline empty")
        self.set_state("speed", speed)
        self.set_state("transport_status", "shuttle")
        return self._line(200, "ok")

    def _cmd_slot_select(self, session, params):
        if self.get_state("transport_status") == "record":
            return self._line(150, "invalid state")
        if "slot id" not in params:
            return self._line(101, "unsupported parameter")
        slot = int(params["slot id"])
        if not 1 <= slot <= SLOTS:
            return self._line(102, "invalid value")
        if self.get_state("transport_status") in ("play", "shuttle", "jog", "forward", "rewind"):
            self.set_state("transport_status", "stopped")
            self.set_state("speed", 0)
        self.set_state("active_slot", slot)
        self._refresh_clip_count()
        self._seek(0)
        return self._line(200, "ok")

    # ── Helpers ──

    @staticmethod
    def _flag(value: str) -> bool:
        text = str(value).strip().lower()
        if text not in ("true", "false"):
            raise ValueError(value)
        return text == "true"

    def _close_later(self, client_id: str | None) -> None:
        """Close one connection (or all, for a restart) once the answer is out."""

        def close() -> None:
            for cid, writer in list(self._clients.items()):
                if client_id is None or cid == client_id:
                    writer.close()

        try:
            asyncio.get_running_loop().call_later(0.2, close)
        except RuntimeError:
            pass
