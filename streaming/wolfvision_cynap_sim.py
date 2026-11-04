"""
WolfVision Cynap simulator (WolfProt, firmware 1.7x).

Speaks the WolfProt framing of the 1.78a command reference: a header byte
(bit 0 SET, bit 1 extended header, bit 2 two-byte length, bit 3 two-byte
command, bit 7 error), the command, a big-endian length and the payload.
Errors go back as ``<header | 0x80> <command> <code>``.

Each connection has its own login level and its own subscriptions, as the
reference describes. With no moderator password set (``moderator_password``
blank) a connection starts at user level; Login with the password ``invalid``
is refused (the catalog's convention for testing a refused password), any
other password is taken. Admin-only commands answer access denied below admin.

A subscribed value that changes is sent to that connection as the GET reply
for it. The reference never shows a notice; this is the shape the driver
reads. Standby closes every connection, as the Cynap Pro Network Integration
guide says, and a connection opened while in standby is closed at once: turn
the simulated Cynap on with its Power control (Wake-on-LAN cannot reach a
Python simulator).
"""

from __future__ import annotations

import asyncio
import json
import logging

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

LOCAL = "local"

# command -> (state key, user level needed to GET, level needed to SET)
# Levels: 0 none, 1 user, 2 admin.
SIMPLE = {
    0xCB0C: ("power", 0, 1),
    0xCB56: ("volume", 1, 1),
    0xCB58: ("mute", 1, 1),
    0xCB59: ("mic_mute", 1, 1),
    0xCB5A: ("freeze", 1, 1),
    0xCD30: ("presentation", 0, None),
    0xCC76: ("screensaver", 1, None),
    0xCB20: ("streaming", 0, 1),
    0xCB38: ("presentation_mode", 0, 2),
    0xCCC7: ("power_down_mode", 0, 2),
    0xCB5B: ("aux_mode", 0, 2),
    0xCBEA: ("mic_status", 0, None),
}
KEY_TO_CODE = {key: code for code, (key, _g, _s) in SIMPLE.items()}
SUBSCRIBABLE = {0xCB0C, 0xCD30, 0xCC76, 0xCB38, 0xCB5A, 0xCB58, 0xCB59, 0xCB56, 0xCB25, 0xCCC7}


def _frame(command: int, payload: bytes = b"", is_set: bool = False) -> bytes:
    n = len(payload)
    header = 0x08 | (0x01 if is_set else 0)
    if n <= 0xFF:
        return bytes([header]) + command.to_bytes(2, "big") + bytes([n]) + payload
    if n <= 0xFFFF:
        return bytes([header | 0x04]) + command.to_bytes(2, "big") + n.to_bytes(2, "big") + payload
    return bytes([header | 0x02, 0x01]) + command.to_bytes(2, "big") + n.to_bytes(4, "big") + payload


def _error(command: int, code: int, is_set: bool) -> bytes:
    return bytes([0x88 | (0x01 if is_set else 0)]) + command.to_bytes(2, "big") + bytes([code])


class WolfVisionCynapSimulator(TCPSimulator):
    """A WolfVision Cynap answering WolfProt on TCP 50915."""

    SIMULATOR_INFO = {
        "driver_id": "wolfvision_cynap",
        "name": "WolfVision Cynap Simulator",
        "category": "streaming",
        "transport": "tcp",
        "default_port": 50915,
        "initial_state": {
            "power": "on",
            "volume": 50,
            "mute": 0,
            "mic_mute": 0,
            "freeze": 0,
            "presentation": 1,
            "screensaver": 0,
            "recording_state": 2,
            "recording_ms": 0,
            "streaming": 0,
            "presentation_mode": 0,
            "power_down_mode": 0,
            "aux_mode": 0,
            "mic_status": 1,
            "model": "CB1",
            "boxname": "Cynap",
            "serial": 21012345,
            "mac": "00:0C:92:12:34:56",
            "hdmi_1_present": 1,
            "hdmi_2_present": 0,
            "window_1": 2,
            "window_2": 0,
            "window_3": 0,
            "window_4": 0,
            "window_1_fullscreen": 1,
            "window_1_mute": 0,
            "last_command": "",
        },
        "controls": [
            {"type": "power", "key": "power", "label": "Power"},
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 100},
            {"type": "toggle", "key": "mute", "label": "Mute"},
            {"type": "toggle", "key": "mic_mute", "label": "Microphone Mute"},
            {"type": "toggle", "key": "freeze", "label": "Freeze"},
            {"type": "toggle", "key": "presentation", "label": "Presentation Active"},
            {"type": "toggle", "key": "hdmi_1_present", "label": "HDMI In 1 Signal"},
            {"type": "toggle", "key": "hdmi_2_present", "label": "HDMI In 2 Signal"},
            {"type": "indicator", "key": "last_command", "label": "Last command"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._delimiter = None
        self._line_mode = False
        self._rx: dict[str, bytes] = {}
        self._task_sessions: dict[asyncio.Task, str] = {}
        self._levels: dict[str, int] = {}
        self._subs: dict[str, set[int]] = {}

    # ── Sessions ──

    def _start_level(self) -> int:
        return 0 if str(self.config.get("moderator_password") or "") else 1

    async def on_client_connected(self, client_id: str) -> bytes | None:
        # Called from the client's own task, which also runs handle_command.
        try:
            self._task_sessions[asyncio.current_task()] = client_id
        except RuntimeError:
            pass
        self._levels[client_id] = self._start_level()
        self._subs[client_id] = set()
        if self.state.get("power") != "on":
            # A Cynap in standby is off the network.
            writer = self._clients.get(client_id)
            if writer:
                writer.close()
        return None

    def _session(self) -> str:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return LOCAL
        return self._task_sessions.get(task, LOCAL)

    # ── State changes reach subscribers ──

    def set_state(self, key, value):
        old = self.state.get(key)
        super().set_state(key, value)
        if old == value:
            return
        if key == "power" and value != "on":
            # Standby closes every connection (Network Integration guide).
            try:
                asyncio.get_running_loop().call_later(0.2, self._close_all)
            except RuntimeError:
                pass
        code = KEY_TO_CODE.get(key)
        if key in ("recording_state",):
            code = 0xCB25
        if code is None or code not in SUBSCRIBABLE:
            return
        frame = _frame(code, self._get_payload(code))
        for client_id, subs in list(self._subs.items()):
            if code in subs:
                try:
                    asyncio.ensure_future(self.push_to(client_id, frame))
                except RuntimeError:
                    pass

    # ── Framing ──

    def handle_command(self, data: bytes) -> bytes | None:
        session = self._session()
        buf = self._rx.get(session, b"") + data
        out = b""
        while buf:
            size = self._frame_size(buf)
            if size is None:
                break
            if size < 0:
                buf = buf[1:]
                continue
            frame, buf = buf[:size], buf[size:]
            reply = self._answer(session, frame)
            if reply:
                out += reply
        self._rx[session] = buf
        return out or None

    @staticmethod
    def _frame_size(buf: bytes) -> int | None:
        header = buf[0]
        if header & 0xF0:
            return -1
        pos = 1
        ext = 0
        if header & 0x02:
            if len(buf) < 2:
                return None
            ext = buf[1]
            pos = 2
        pos += 2 if header & 0x08 else 1
        width = 4 if (header & 0x02 and ext & 0x01) else 2 if header & 0x04 else 1
        if len(buf) < pos + width:
            return None
        total = pos + width + int.from_bytes(buf[pos:pos + width], "big")
        return total if len(buf) >= total else None

    def _answer(self, session: str, frame: bytes) -> bytes | None:
        header = frame[0]
        is_set = bool(header & 0x01)
        pos = 2 if header & 0x02 else 1
        cwidth = 2 if header & 0x08 else 1
        command = int.from_bytes(frame[pos:pos + cwidth], "big")
        pos += cwidth
        width = 4 if (header & 0x02 and frame[1] & 0x01) else 2 if header & 0x04 else 1
        payload = frame[pos + width:]
        self.set_state("last_command", f"{'SET' if is_set else 'GET'} {command:04X} {payload.hex().upper()}".strip())
        level = self._levels.get(session, self._start_level())
        try:
            if is_set:
                result = self._set(session, level, command, payload)
                return _frame(command, b"", True) if result is None else _error(command, result, True)
            if command == 0xCD3B:
                if level < 1:
                    return _error(command, 0x07, False)
                return _frame(command, self._hdmi_status(payload[0] if payload else 0))
            needed = SIMPLE.get(command, (None, 0, None))[1]
            if command == 0xCB25 or command == 0xCD85:
                needed = 1
            if level < needed:
                return _error(command, 0x07, False)
            body = self._get_payload(command)
            if body is None:
                return _error(command, 0x02, False)
            return _frame(command, body)
        except (IndexError, ValueError):
            return _error(command, 0x03, is_set)

    # ── GET ──

    def _get_payload(self, command: int) -> bytes | None:
        s = self.state
        if command == 0xCB0C:
            return b"\x01" if s.get("power") == "on" else b"\x00"
        if command in SIMPLE:
            return bytes([int(s.get(SIMPLE[command][0], 0)) & 0xFF])
        if command == 0xCB25:
            return bytes([int(s.get("recording_state", 2))]) + int(s.get("recording_ms", 0)).to_bytes(4, "little")
        if command == 0xCB00:
            return str(s.get("model", "")).encode()
        if command == 0xCB03:
            return str(s.get("boxname", "")).encode()
        if command == 0xCB60:
            return int(s.get("serial", 0)).to_bytes(4, "little")
        if command == 0xCB50:
            return json.dumps({"cynap": {"version": "V1.78a", "buildnumber": "20260901000000",
                                         "stage": "release"}}).encode()
        if command == 0xCB09:
            return str(s.get("mac", "")).encode()
        if command == 0xCB55:
            return bytes([self._levels.get(self._session(), self._start_level())])
        if command == 0xCD85:
            out = b""
            for index in range(1, 5):
                block = bytes([
                    int(s.get(f"window_{index}", 0)),
                    int(s.get(f"window_{index}_fullscreen", 0)),
                    int(s.get(f"window_{index}_mute", 0)),
                ])
                out += bytes([len(block)]) + block
            return out
        return None

    def _hdmi_status(self, index: int) -> bytes:
        present = int(self.state.get(f"hdmi_{index + 1}_present", 0))
        width, height = (1920, 1080) if present else (0, 0)
        return bytes([present]) + width.to_bytes(2, "little") + height.to_bytes(2, "little") + b"\x01\x00"

    # ── SET ──

    def _set(self, session: str, level: int, command: int, p: bytes) -> int | None:
        """Apply a SET; return an error code, or None when accepted."""
        if command == 0xCB42:  # Login
            access = p[0]
            pw = p[2:2 + p[1]].decode("utf-8", errors="replace")
            if pw == "invalid" or access not in (1, 2, 3, 4):
                return 0x07
            self._levels[session] = 2 if access == 2 else 1
            return None
        if command == 0xCD40:  # Subscribe
            codes = set()
            pos = 0
            while pos + 4 <= len(p):
                codes.add(int.from_bytes(p[pos + 1:pos + 3], "big"))
                pos += 4
            if not codes <= SUBSCRIBABLE:
                return 0x03
            self._subs.setdefault(session, set()).update(codes)
            return None
        if command == 0xCB01:  # Remote key
            return None if level >= 0 and p[0] <= 0x0C else 0x03
        if command in SIMPLE:
            key, _get, needed = SIMPLE[command]
            if needed is None:
                return 0x02
            if level < needed:
                return 0x07
            value = p[0]
            if command == 0xCB0C:
                self.set_state("power", "on" if value else "off")
                return None
            if value == 2 and command in (0xCB58, 0xCB59, 0xCB5A):
                value = 0 if int(self.state.get(key, 0)) else 1
            if command == 0xCB56 and value > 100:
                return 0x03
            self.set_state(key, value)
            return None
        if command == 0xCB25:  # Recording
            if level < 1:
                return 0x07
            action = p[0]
            current = int(self.state.get("recording_state", 2))
            if action == 0:
                self.set_state("recording_state", 0)
            elif action == 1 and current != 2:
                self.set_state("recording_state", 1 if current == 0 else 0)
            elif action == 2:
                self.set_state("recording_state", 2)
                self.set_state("recording_ms", 0)
            return None
        if command == 0xCB03:  # Boxname
            if level < 2:
                return 0x07
            if not 0 < len(p) <= 32:
                return 0x04
            self.set_state("boxname", p.decode("utf-8", errors="replace"))
            return None
        if command == 0xCB28:  # Window Control
            if level < 0 or p[0] > 3:
                return 0x03
            index = p[0] + 1
            action = p[1]
            if action == 0x00:
                self.set_state(f"window_{index}", 0)
                self.set_state(f"window_{index}_fullscreen", 0)
            elif action in (0x01, 0x02):
                self.set_state(f"window_{index}_fullscreen", 1 if action == 0x01 else 0)
            elif action == 0x03:
                self.set_state(f"window_{index}_mute", p[2])
            elif action != 0x04:
                return 0x03
            return None
        if command == 0xCD60:  # Start Application
            if level < 1:
                return 0x07
            window_type = p[12]
            for index in range(1, 5):
                if not int(self.state.get(f"window_{index}", 0)):
                    self.set_state(f"window_{index}", window_type)
                    return None
            return 0x05
        if command == 0xCB49:  # End Presentation
            if level < 1:
                return 0x07
            for index in range(1, 5):
                self.set_state(f"window_{index}", 0)
            if p[2] == 1:
                self.set_state("power", "off")
            return None
        if command == 0xCD00:  # Screensaver Off
            self.set_state("screensaver", 0)
            return None
        if command == 0xCCF5:  # System Reboot
            return None if level >= 2 else 0x07
        return 0x02

    def _close_all(self) -> None:
        for writer in list(self._clients.values()):
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - a writer already gone
                pass
