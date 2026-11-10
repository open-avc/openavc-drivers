"""Somfy SDN bus simulator, based on DOC155888/004.

Models three Sonesse 30 RS485 motors, or the configured motor roster. Replies
are requested, not pushed; movement progresses over two seconds. The undefined
NODE_IS_LOCKED, LOW_PRIORITY and IP_NOT_SET numeric codes are represented by
synthetic 0x7E to exercise unknown-refusal handling, not as a manufacturer code.
This simulation does not establish physical bus timing or collision behavior.
"""

from __future__ import annotations

import asyncio
import struct
import time
import weakref
from dataclasses import dataclass

from openavc.simulator.tcp_simulator import TCPSimulator


@dataclass(frozen=True)
class Request:
    message: int
    flags: int
    source: int
    address: int
    data: bytes

    @property
    def expects_reply(self):
        queries = {0x40, 0x45, 0x74, 0x4C, 0x41, 0x0C, 0x0E, 0x25, 0x23, 0x26, 0x27}
        return bool(self.flags & 128) or self.message in queries


class Refused(Exception):
    def __init__(self, code):
        self.code = code


def packet(message, source, destination, data=b""):
    header = bytes([message, 11 + len(data), 0x20])
    addresses = source.to_bytes(3, "little") + destination.to_bytes(3, "little")
    body = bytes(value ^ 255 for value in header + addresses + data)
    return body + sum(body).to_bytes(2, "big")


def take_frame(buffer):
    """Keep partial input per client; discard malformed frames without replying."""
    while len(buffer) >= 2:
        length = (buffer[1] ^ 255) & 0x3F
        if not 11 <= length <= 32:
            del buffer[0]
            continue
        if len(buffer) < length:
            return None
        frame = bytes(buffer[:length])
        if sum(frame[:-2]) != int.from_bytes(frame[-2:], "big"):
            del buffer[0]
            continue
        del buffer[:length]
        return bytes(value ^ 255 for value in frame[:-2])
    return None


def new_motor(label):
    return {
        "label": label[:16],
        "position": 25,
        "angle": 0,
        "ip": 255,
        "status": 0,
        "direction": 255,
        "source": 0,
        "cause": 255,
        "speeds": [25, 25, 10],
        "groups": [0xFFFFFF] * 16,
        "ips": {},
        "ui": {},
        "lock": [0, 0, 0, 0],
        "motion": None,
    }


class SomfySDNSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "somfy_sdn",
        "name": "Somfy SDN Shades Simulator",
        "category": "utility",
        "transport": "tcp",
        "default_port": 0,
        "delimiter": None,
        "initial_state": {"shade_count": 0},
        "delays": {"command_response": 0.01},
        "error_modes": {
            "communication_timeout": {
                "description": "Device stops responding",
                "behavior": "no_response",
            },
            "busy": {"description": "Motor refuses commands as busy"},
            "obstacle": {"description": "An obstacle blocks motion"},
            "unknown_refusal": {"description": "Synthetic unmapped refusal code 0x7E"},
        },
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._buffers = weakref.WeakKeyDictionary()
        self._direct_buffer = bytearray()
        rows = self.config.get("motors") or [
            {"address": "A1B201", "label": "Window Left"},
            {"address": "A1B202", "label": "Window Center"},
            {"address": "A1B203", "label": "Window Right"},
        ]
        self._motors = {
            int(str(row["address"]).replace(":", ""), 16): new_motor(
                str(row.get("label") or row["address"])
            )
            for row in rows
        }
        self.set_state("shade_count", len(self._motors))
        for address, motor in self._motors.items():
            self.set_state(f"shade.{address:06X}.position", motor["position"])
            self.set_state(f"shade.{address:06X}.status", "stopped")
        controls = []
        for address, motor in self._motors.items():
            prefix = f"shade.{address:06X}"
            controls.extend(
                [
                    {
                        "type": "slider",
                        "key": f"{prefix}.position",
                        "label": motor["label"],
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "unit": "%",
                    },
                    {
                        "type": "indicator",
                        "key": f"{prefix}.status",
                        "label": "Motor Status",
                    },
                ]
            )
        self.SIMULATOR_INFO = {**type(self).SIMULATOR_INFO, "controls": controls}
        self._handlers = {
            0x40: self._identity,
            0x45: self._label,
            0x55: self._label,
            0x74: self._firmware,
            0x4C: self._serial,
            0x41: self._group,
            0x51: self._group,
            0x0C: self._position,
            0x0E: self._status,
            0x03: self._move,
            0x02: self._stop,
            0x05: self._wink,
            0x25: self._ip_read,
            0x15: self._ip_write,
            0x23: self._speed,
            0x13: self._speed,
            0x26: self._lock,
            0x16: self._lock,
            0x27: self._local_ui,
            0x17: self._local_ui,
            0x1F: self._reset,
        }

    def handle_command(self, data):
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        buffer = (
            self._buffers.setdefault(task, bytearray()) if task else self._direct_buffer
        )
        buffer.extend(data)
        replies = bytearray()
        while (frame := take_frame(buffer)) is not None:
            replies.extend(self._frame(frame))
        return bytes(replies) or None

    def _frame(self, frame):
        message, flags, types = frame[:3]
        source = int.from_bytes(frame[3:6], "little")
        destination = int.from_bytes(frame[6:9], "little")
        if types & 15 not in (0, 2):
            return b""
        output = bytearray()
        for address, motor in self._motors.items():
            addressed = (
                destination in (address, 0xFFFFFF)
                or destination == 0
                and source in motor["groups"]
            )
            if addressed:
                output.extend(
                    self._answer(
                        Request(message, flags, source, address, frame[9:]), motor
                    )
                )
        return bytes(output)

    def _execute(self, request, motor):
        self._advance(request.address, motor)
        if "busy" in self.active_errors:
            raise Refused(255)
        if "unknown_refusal" in self.active_errors:
            raise Refused(0x7E)
        handler = self._handlers.get(request.message)
        if handler is None:
            raise Refused(0x10)
        reply = handler(request, motor)
        if reply:
            return packet(reply[0], request.address, request.source, reply[1])
        return (
            packet(0x7F, request.address, request.source)
            if request.flags & 128
            else b""
        )

    @staticmethod
    def _refusal(request, code):
        if request.expects_reply:
            return packet(0x6F, request.address, request.source, bytes([code]))
        return b""

    def _answer(self, request, motor):
        try:
            return self._execute(request, motor)
        except Refused as exc:
            return self._refusal(request, exc.code)
        except (IndexError, struct.error):
            return self._refusal(request, 0x11)

    @staticmethod
    def _identity(request, motor):
        return 0x60, b""

    @staticmethod
    def _firmware(request, motor):
        return 0x75, bytes.fromhex("4D433E410200")

    @staticmethod
    def _serial(request, motor):
        address = request.address
        return 0x6C, f"{address:06X}AA2620".encode()

    @staticmethod
    def _label(request, motor):
        message = request.message
        data = request.data
        if message == 0x45:
            return 0x65, motor["label"].encode().ljust(16, b" ")
        if len(data) != 16:
            raise Refused(0x11)
        motor["label"] = data.decode("ascii", errors="replace").rstrip()

    @staticmethod
    def _group(request, motor):
        message = request.message
        data = request.data
        index = data[0]
        if index > 15:
            raise Refused(1)
        if message == 0x41:
            return 0x61, bytes([index]) + motor["groups"][index].to_bytes(3, "little")
        if len(data) != 4:
            raise Refused(0x11)
        motor["groups"][index] = int.from_bytes(data[1:4], "little")

    def _advance(self, address, motor):
        motion = motor["motion"]
        if not motion:
            motor["position"] = int(
                self.get_state(f"shade.{address:06X}.position", motor["position"])
            )
            return
        start, target, began = motion
        progress = min(1, (time.monotonic() - began) / 2)
        motor["position"] = round(start + (target - start) * progress)
        if progress == 1:
            motor.update(status=0, cause=0, motion=None)
        self.set_state(f"shade.{address:06X}.position", motor["position"])
        self.set_state(
            f"shade.{address:06X}.status",
            "running" if motor["status"] == 1 else "stopped",
        )

    @staticmethod
    def _position(request, motor):
        position = motor["position"]
        percent_angle = round(motor["angle"] * 100 / 180)
        # The guide reports the first matching IP. Exact position matching
        # is the simulator convention; hardware tolerance varies by firmware.
        current_ip = next(
            (
                index
                for index, saved in sorted(motor["ips"].items())
                if saved[0] == position
            ),
            255,
        )
        return 0x0D, struct.pack(
            "<HBBBHHH",
            position * 100,
            position,
            percent_angle,
            current_ip,
            0,
            motor["angle"],
            0,
        )

    @staticmethod
    def _status(request, motor):
        status = 3 if motor["lock"][0] else motor["status"]
        return 0x0F, bytes(
            [status, motor["direction"], motor["source"], motor["cause"]]
        )

    @staticmethod
    def _require_unlocked(motor):
        if motor["lock"][0]:
            raise Refused(
                0x7E
            )  # Synthetic: guide names NODE_IS_LOCKED without a value.

    @staticmethod
    def _saved_target(position, motor):
        preset = motor["ips"].get(position + 1)
        if preset is None:
            raise Refused(0x7E)  # Synthetic IP_NOT_SET.
        angle = motor["angle"] if preset[1] == 0x8000 else preset[1]
        return preset[0], angle

    def _target(self, function, position, motor):
        if function == 2:
            return self._saved_target(position, motor)
        targets = {0: 100, 1: 0, 0x0F: motor["position"], 0x10: motor["position"]}
        return targets.get(function, position), motor["angle"]

    @staticmethod
    def _angle(data, degrees, fallback):
        if len(data) < 6:
            return fallback
        raw = struct.unpack("<h", data[4:6])[0]
        low, high = (-90, 90) if degrees else (0, 100)
        if not low <= raw <= high:
            raise Refused(1)
        # Illustrative simulator mapping. The guide does not define the
        # relationship between signed targets and 0..180 feedback angles.
        return raw + 90 if degrees else round(raw * 180 / 100)

    def _move(self, request, motor):
        self._require_unlocked(motor)
        function, position, _ = struct.unpack("<BHB", request.data[:4])
        if function not in (0, 1, 2, 4, 0x0C, 0x0D, 0x0F, 0x10):
            raise Refused(1)
        target, target_angle = self._target(function, position, motor)
        if not 0 <= target <= 100:
            raise Refused(1)
        motor["angle"] = self._angle(
            request.data, function in (0x0D, 0x10), target_angle
        )
        if "obstacle" in self.active_errors:
            motor.update(status=2, cause=0x20, source=1, motion=None)
            return
        motor.update(
            status=1,
            cause=1,
            source=1,
            ip=255,
            direction=int(target < motor["position"]),
            motion=(motor["position"], target, time.monotonic()),
        )

    def _stop(self, request, motor):
        data = request.data
        self._require_unlocked(motor)
        if len(data) != 1:
            raise Refused(0x11)
        motor.update(status=0, cause=1, source=1, motion=None)

    def _wink(self, request, motor):
        self._require_unlocked(motor)
        motor.update(cause=2, source=1)

    @staticmethod
    def _ip_read(request, motor):
        data = request.data
        index = data[0]
        if not 1 <= index <= 16:
            raise Refused(1)
        position, angle = motor["ips"].get(index, (255, 0x8000))
        return 0x35, bytes([index, 0, 0, position, 0, 0, 0]) + angle.to_bytes(
            2, "little"
        )

    @staticmethod
    def _space_positions(motor, count):
        if not 1 <= count <= 16:
            raise Refused(1)
        for index in range(1, count + 1):
            motor["ips"][index] = (round(index * 100 / (count + 1)), 0x8000)

    @staticmethod
    def _delete_position(motor, index):
        if index not in motor["ips"]:
            raise Refused(0x7E)
        del motor["ips"][index]

    def _save_position(self, motor, request, values):
        function, index, position = values
        if function in (1, 5):
            position = motor["position"]
        if function not in (1, 3, 5, 0x0A, 0x0B) or not 0 <= position <= 100:
            raise Refused(1)
        fallback = motor["angle"] if function == 5 else 0x8000
        angle = self._angle(request.data, function == 0x0B, fallback)
        motor["ips"][index] = (position, angle)

    def _ip_write(self, request, motor):
        values = struct.unpack("<BBH", request.data[:4])
        function, index, position = values
        if function == 4:
            return self._space_positions(motor, position)
        if not 1 <= index <= 16:
            raise Refused(1)
        if function == 0:
            return self._delete_position(motor, index)
        return self._save_position(motor, request, values)

    @staticmethod
    def _speed(request, motor):
        message = request.message
        data = request.data
        if message == 0x23:
            return 0x33, bytes(motor["speeds"])
        if len(data) != 3:
            raise Refused(0x11)
        if not all(10 <= value <= 28 for value in data):
            raise Refused(1)
        motor["speeds"] = list(data)

    @staticmethod
    def _lock(request, motor):
        message = request.message
        data = request.data
        source = request.source
        locked, owner, priority, saved = motor["lock"]
        if message == 0x26:
            return 0x36, bytes([locked]) + owner.to_bytes(3, "little") + bytes(
                [priority, saved]
            )
        function, requested = data[:2]
        if function in (3, 4):
            motor["lock"][3] = int(function == 3)
            return
        if function not in (0, 1):
            raise Refused(1)
        if locked and requested < priority:
            raise Refused(0x7E)  # Synthetic LOW_PRIORITY.
        motor["lock"] = [
            function,
            source if function else 0,
            requested if function else 0,
            saved,
        ]
        motor.update(motion=None, status=0)

    @staticmethod
    def _local_ui(request, motor):
        message = request.message
        data = request.data
        source = request.source
        if message == 0x27:
            status, owner, priority = motor["ui"].get(data[0], (0, 0, 0))
            return 0x37, bytes([status]) + owner.to_bytes(3, "little") + bytes(
                [priority]
            )
        function, index, priority = data[:3]
        if function not in (0, 1) or index > 5:
            raise Refused(1)
        for ui in range(1, 6) if index == 0 else [index]:
            if priority < motor["ui"].get(ui, (0, 0, 0))[2]:
                raise Refused(0x7E)
            motor["ui"][ui] = (
                function,
                source if function else 0,
                priority if function else 0,
            )

    @staticmethod
    def _reset(request, motor):
        data = request.data
        function = data[0]
        if function == 0:
            motor.clear()
            motor.update(new_motor("Uncommissioned"))
        elif function == 1:
            motor["groups"] = [0xFFFFFF] * 16
        elif function == 0x15:
            motor["ips"].clear()
        elif function == 0x17:
            motor.update(ui={}, lock=[0, 0, 0, 0])
        else:
            raise Refused(1)
