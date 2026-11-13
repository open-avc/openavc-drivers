"""Allen & Heath AHM processor simulator with per-connection MIDI state."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hmac

from openavc.simulator.self_signed_tls import build_optional_tls, remove_cert_files
from openavc.simulator.tcp_simulator import TCPSimulator

PREFIX = bytes.fromhex("f0 00 00 1a 50 12 01 00")
KINDS = ("input", "zone", "group", "room")
TITLES = ("Lectern mic", "Lecture hall", "Room master", "Lecture room")
SELECTORS = {
    0: ("Lectern", "Wireless mic", "Background music"),
    1: ("Music", "Presentation"),
}
PARAM = {23: "level", 24: "trim", 25: "gain", 26: "pad", 27: "phantom"}
DEFAULT = {
    "level": 105,
    "muted": False,
    "trim": 64,
    "gain": 0,
    "pad": False,
    "phantom": False,
}
SESSION = ContextVar("ahm_simulator_session")


def sx(*data):
    return PREFIX + bytes(data) + b"\xf7"


def level_frame(n, ch, param, val):
    return bytes((176 + n, 99, ch, 176 + n, 98, param, 176 + n, 6, val))


def mute_frame(n, ch, muted):
    return bytes((144 + n, ch, 127 if muted else 63, 144 + n, ch, 0))


def nudged(current, direction):
    return max(0, min(127, current + (1 if direction == 127 else -1)))


def parameter_value(prop, value):
    if prop in ("pad", "phantom"):
        return 127 if value else 0
    return int(value)


def source_event(n, ch, slot):
    names = SELECTORS.get(ch, ())
    title = names[slot] if 0 <= slot < len(names) else f"Source {slot + 1}"
    return sx(0, 8 if n == 1 else 13, ch, slot, (slot % 7) + 1, *title.encode("ascii"))


class Cable:
    """Retain incomplete messages and running status for this client only."""

    def __init__(self):
        self.status = 0
        self.buf = bytearray()
        self.controllers = {}
        self.bank = 0

    def frames(self, data):
        for b in data:
            if b < 128:
                msg = self._data(b)
            elif b < 248:
                msg = self._status(b)
            else:
                continue
            if msg:
                yield msg

    def _status(self, b):
        msg = bytes(self.buf) + b"\xf7" if b == 247 and self.status == 240 else None
        self.status = b if b <= 240 else 0
        self.buf = bytearray((self.status,))
        return msg

    def _data(self, b):
        if not self.status:
            return None
        self.buf.append(b)
        if self.status == 240:
            if len(self.buf) > 4096:
                self._status(247)
            return None
        length = 2 if self.status >> 4 in (12, 13) else 3
        if len(self.buf) == length:
            result = bytes(self.buf)
            self.buf = bytearray((self.status,))
            return result
        return None


class AllenHeathAHMSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "allenheath_ahm",
        "name": "Allen & Heath AHM",
        "transport": "tcp",
        "default_port": 51327,
        "initial_state": {
            "last_preset": None,
            "zone_1_level": 105,
            "zone_1_muted": False,
            "zone_1_source": 1,
            "room_1_source": 1,
            "rooms_1_2_combined": False,
        },
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._line_mode = False
        self._delimiter = None
        self._size = {"AHM-16": 16, "AHM-32": 32, "AHM-64": 64}.get(
            self.config.get("model"), 16
        )
        self._secure = bool(self.config.get("ssl", config is not None))
        self.SIMULATOR_INFO = {**self.SIMULATOR_INFO, "tls": self._secure}
        self._cert = None
        self._crosspoints = {}
        self._push_tasks = set()

    async def start(self, port):
        if not self._secure:
            return await super().start(port)
        ctx, self._cert = build_optional_tls(
            self.SIMULATOR_INFO, self.config, self.name
        )
        if ctx is None:
            raise RuntimeError("The simulator could not create its TLS certificate.")
        try:
            self._server = await asyncio.start_server(
                self._handle_client, "127.0.0.1", port, ssl=ctx
            )
            self._port = self._server.sockets[0].getsockname()[1]
            self._running = True
        except BaseException:
            remove_cert_files(self._cert)
            raise

    async def stop(self):
        await super().stop()
        if self._push_tasks:
            await asyncio.gather(*self._push_tasks, return_exceptions=True)
        remove_cert_files(self._cert)

    async def _handle_client(self, reader, writer):
        token = SESSION.set(Cable())
        try:
            await super()._handle_client(reader, writer)
        finally:
            SESSION.reset(token)

    async def authenticate_client(self, reader, writer, client_id):
        if not self._secure:
            return True
        expected = bytes((int(self.config.get("profile", 0)),)) + (
            (self.config.get("password") or "").encode("ascii") or b"\x00"
        )
        try:
            received = await asyncio.wait_for(reader.readexactly(len(expected)), 2)
            try:
                extra = await asyncio.wait_for(reader.read(1), 0.03)
            except TimeoutError:
                extra = b""
            received += extra
        except (TimeoutError, asyncio.IncompleteReadError):
            return False
        if not hmac.compare_digest(received, expected):
            return False
        writer.write(b"AuthOK")
        await writer.drain()
        return True

    def handle_command(self, data):
        cable = SESSION.get()
        return b"".join(self._handle(frame, cable) for frame in cable.frames(data))

    def _handle(self, frame, cable):
        status = frame[0]
        if status == 240:
            return (
                self._sysex(frame[len(PREFIX) : -1])
                if frame.startswith(PREFIX)
                else b""
            )
        n = status & 15
        if status == 192:
            self._recall(cable.bank, frame[1])
        elif status >> 4 == 9:
            self._mute(n, *frame[1:])
        elif status >> 4 == 11:
            self._cc(cable, n, *frame[1:])
        return b""

    def _recall(self, bank, slot):
        number = 128 * bank + slot + 1
        if number <= 500:
            self.set_state("last_preset", number)

    def _mute(self, n, ch, velocity):
        if velocity and self._valid(n, ch):
            self.set_state(self._key(n, ch, "muted"), velocity >= 64)

    def _cc(self, cable, n, cc, value):
        if n == 0 and cc == 0:
            cable.bank = value
            return
        ctrl = cable.controllers.setdefault(n, {})
        if cc in (98, 99):
            ctrl[cc] = value
        if cc != 6 or not self._valid(n, ctrl.get(99, 127)):
            return
        if ctrl.get(98) == 32:
            key = self._key(n, ctrl[99], "level")
            self.set_state(key, nudged(self.get_state(key, 105), value))
            return
        prop = PARAM.get(ctrl.get(98))
        if prop:
            val = value >= 64 if prop in ("pad", "phantom") else value
            self.set_state(self._key(n, ctrl[99], prop), val)

    def _valid(self, n, ch):
        return n < 4 and ch < (self._size, self._size, 32, 16)[n]

    def _key(self, n, ch, prop):
        return f"{KINDS[n]}_{ch + 1}_{prop}"

    def _name(self, n, ch):
        default = TITLES[n] if ch == 0 else f"{KINDS[n].title()} {ch + 1}"
        return self.get_state(self._key(n, ch, "name"), default)

    def _sysex(self, d):
        if len(d) < 3:
            return b""
        n, op, ch = d[:3]
        if op == 1:
            return self._get(n, d[2:])
        if op in (9, 11) and self._valid(n, ch):
            return (
                sx(n, 10, ch, *self._name(n, ch).encode("ascii"))
                if op == 9
                else sx(n, 12, ch, 4)
            )
        if op in (2, 3, 4) and len(d) == 6:
            return self._write_send(d)
        if n == 0:
            self._operation(op, ch, d[3:])
        return b""

    def _get(self, n, d):
        if len(d) < 2:
            return b""
        code, slot = d[:2]
        if code == 15:
            return self._get_extended(n, d[1:])
        if code == 9 and self._valid(n, slot):
            return mute_frame(
                n, slot, self.get_state(self._key(n, slot, "muted"), False)
            )
        if code == 11 and len(d) == 3:
            return self._level(n, slot, d[2])
        return b""

    def _level(self, n, param, ch):
        if param not in PARAM or not self._valid(n, ch):
            return b""
        prop = PARAM[param]
        val = self.get_state(self._key(n, ch, prop), DEFAULT[prop])
        return level_frame(n, ch, param, parameter_value(prop, val))

    def _get_extended(self, n, data):
        if len(data) < 2:
            return b""
        op, ch = data[:2]
        if not self._valid(n, ch):
            return b""
        if op == 8 and n == 1:
            return self._selector(ch)
        if op in (2, 3) and len(data) == 4:
            target, dst = data[2:]
            val = self._crosspoints.get((n, ch, target, dst, op), 105 if op == 2 else 0)
            return sx(n, op, ch, target, dst, val)
        return b""

    def _write_send(self, data):
        n, op, ch, target, dst, val = data
        if target != 1 or n > 1:
            return b""
        if self._valid(n, ch) and self._valid(target, dst):
            if op == 4:
                op = 2
                val = nudged(self._crosspoints.get((n, ch, target, dst, op), 105), val)
            self._crosspoints[n, ch, target, dst, op] = val
            self._notify(sx(n, op, ch, target, dst, val))
        return b""

    def _selector(self, ch):
        names = SELECTORS.get(ch, ())
        selected = self.get_state(self._key(1, ch, "source"), 1) - 1
        labels = b"".join(name.encode("ascii") + b"\0" for name in names)
        return sx(0, 8, ch, len(names), selected, *range(1, len(names) + 1), *labels)

    def _operation(self, op, ch, args):
        if op == 14:
            self._divider(ch, args)
            return
        if len(args) != 1:
            return
        value = args[0]
        if op == 6:
            self._play(ch, value)
        else:
            self._select_source(op, ch, value)

    def _play(self, ch, value):
        if ch < 3:
            super().set_state("playback_channel", ch)
            super().set_state("playback_track", value + 1)

    def _select_source(self, op, ch, value):
        if op == 8 and value < len(SELECTORS.get(ch, ())):
            self.set_state(self._key(1, ch, "source"), value + 1)
        elif op == 13 and ch < 16:
            self.set_state(self._key(3, ch, "source"), value + 1)

    def _divider(self, ch, args):
        if len(args) == 2 and max(ch, args[0]) < 16:
            a, b = sorted((ch + 1, args[0] + 1))
            self.set_state(f"rooms_{a}_{b}_combined", args[1] < 64)

    def set_state(self, key, value):
        super().set_state(key, value)
        msg = self._change(key, value)
        if msg:
            self._notify(msg)

    def _notify(self, msg):
        task = asyncio.create_task(self.push(msg))
        self._push_tasks.add(task)
        task.add_done_callback(self._push_tasks.discard)

    def _change(self, key, value):
        if key == "last_preset" and value is not None:
            bank, slot = divmod(int(value) - 1, 128)
            return bytes((176, 0, bank, 192, slot))
        parts = key.split("_")
        if len(parts) == 4 and parts[0] == "rooms":
            return sx(0, 14, int(parts[1]) - 1, int(parts[2]) - 1, 0 if value else 127)
        if len(parts) != 3 or parts[0] not in KINDS:
            return None
        return self._channel_change(
            KINDS.index(parts[0]), int(parts[1]) - 1, parts[2], value
        )

    def _channel_change(self, n, ch, prop, value):
        if prop in PARAM.values():
            param = next(code for code, name in PARAM.items() if name == prop)
            return level_frame(n, ch, param, parameter_value(prop, value))
        if prop == "muted":
            return mute_frame(n, ch, value)
        if prop == "name":
            return sx(n, 10, ch, *str(value).encode("ascii"))
        if prop == "source":
            return source_event(n, ch, int(value) - 1)
        return None


SIMULATOR_CLASS = AllenHeathAHMSimulator
