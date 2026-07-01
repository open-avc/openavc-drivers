"""
Simulator for the TOA 9000M2 Series matrix mixer (toa_9000m2 driver).

The 9000M2 is write-then-echo: it confirms almost every command by echoing the
received frame back verbatim. The exceptions this simulator reproduces:

  * Fader step (0x93) echoes the RESULTING absolute position, not the step, so
    the sim tracks each fader's position and returns the new one.
  * Channel name request (F0) returns a C0 name response.
  * ANC get-reference (F3) returns a C2 level response.
  * ANC adjust (AE) returns a C1 OK/NG response.

The protocol is binary with no delimiter, so this simulator buffers the raw TCP
byte stream and extracts complete <command><length><data...> frames itself.
"""

from __future__ import annotations

from simulator.tcp_simulator import TCPSimulator

NUM_INPUTS = 8
NUM_OUTPUTS = 8

CMD_FADER_POSITION = 0x91
CMD_PAGING_FADER_POSITION = 0x96
CMD_FADER_STEP = 0x93
CMD_NAME_REQUEST = 0xF0
CMD_ANC_REFERENCE = 0xF3
CMD_ANC_ADJUST = 0xAE

RESP_NAME = 0xC0
RESP_ANC_ADJUST = 0xC1
RESP_ANC_REFERENCE = 0xC2

ATTR_INPUT = 0x00
ATTR_OUTPUT = 0x01

FADER_0DB = 0x6A       # position for 0 dB
ANC_REF_0DB = 0x32     # reference level byte for 0 dB


def _build_frame(command: int, *data: int) -> bytes:
    return bytes([command & 0xFF, len(data), *[d & 0x7F for d in data]])


def _parse_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Extract one <command><len N><N data> frame; resync on the high-bit rule."""
    start = 0
    while start < len(buffer) and not (buffer[start] & 0x80):
        start += 1
    if start:
        buffer = buffer[start:]
    if len(buffer) < 2:
        return None, buffer
    total = 2 + buffer[1]
    if len(buffer) < total:
        return None, buffer
    return buffer[:total], buffer[total:]


def _build_fader_db_table() -> dict[int, float]:
    table: dict[int, float] = {0x00: float("-inf"), 0x01: -70.0}
    for pos in range(0x02, 0x07):
        table[pos] = -60.0 - (0x06 - pos) * 2.0
    for pos in range(0x07, 0x1B):
        table[pos] = -59.0 + (pos - 0x07) * 1.0
    for pos in range(0x1B, 0x7F):
        table[pos] = -40.0 + (pos - 0x1A) * 0.5
    return table


_FADER_DB = _build_fader_db_table()
_FADER_FINITE = {p: db for p, db in _FADER_DB.items() if p != 0x00}


def _fader_step_delta_db(step_byte: int) -> float:
    """0x41-0x5F -> +0.5..+15.5 dB, 0x61-0x7F -> -0.5..-15.5 dB."""
    if 0x41 <= step_byte <= 0x5F:
        return (step_byte - 0x40) * 0.5
    if 0x61 <= step_byte <= 0x7F:
        return -(step_byte - 0x60) * 0.5
    return 0.0


def _fader_apply_step(current_pos: int, step_byte: int) -> int:
    """Return the resulting position after applying a step to current_pos."""
    if current_pos == 0x00:
        current_db = _FADER_FINITE[0x01]
    else:
        current_db = _FADER_DB.get(current_pos, 0.0)
    target = current_db + _fader_step_delta_db(step_byte)
    if target < _FADER_FINITE[0x01]:
        return 0x00
    return min(_FADER_FINITE, key=lambda p: abs(_FADER_FINITE[p] - target))


class TOA9000M2Simulator(TCPSimulator):
    """Simulates a TOA 9000M2 matrix mixer over TCP (serial drivers are
    simulated over a TCP loopback stand-in)."""

    SIMULATOR_INFO = {
        "driver_id": "toa_9000m2",
        "name": "TOA 9000M2 Matrix Mixer Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 0,
        # No delimiter: binary framing is handled in handle_command().
        "initial_state": {
            f"input_{i}_name": f"INPUT{i}" for i in range(1, NUM_INPUTS + 1)
        } | {
            f"output_{o}_name": f"OUTPUT{o}" for o in range(1, NUM_OUTPUTS + 1)
        } | {"power": True},
        "controls": [
            {"type": "indicator", "key": "power", "label": "Power"},
        ] + [
            {"type": "indicator", "key": f"input_{i}_name", "label": f"Input {i} Name"}
            for i in range(1, NUM_INPUTS + 1)
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        self._rx = bytearray()
        # Fader positions default to 0 dB.
        self._fader = {
            (ATTR_INPUT, ch): FADER_0DB for ch in range(NUM_INPUTS)
        }
        self._fader.update({(ATTR_OUTPUT, ch): FADER_0DB for ch in range(NUM_OUTPUTS)})
        self._paging_fader = {ch: FADER_0DB for ch in range(NUM_OUTPUTS)}
        self._anc_ref = {ch: ANC_REF_0DB for ch in range(NUM_INPUTS)}

    def handle_command(self, data: bytes) -> bytes | None:
        """Buffer the raw byte stream, answer every complete frame."""
        self._rx.extend(data)
        out = bytearray()
        while True:
            frame, remaining = _parse_frame(bytes(self._rx))
            self._rx = bytearray(remaining)
            if frame is None:
                break
            resp = self._handle_frame(frame)
            if resp:
                out.extend(resp)
        return bytes(out) if out else None

    def _handle_frame(self, frame: bytes) -> bytes | None:
        command = frame[0]
        length = frame[1]
        body = frame[2:2 + length]

        if command == CMD_FADER_POSITION and len(body) == 3:
            attr, ch, val = body
            if (attr, ch) in self._fader:
                self._fader[(attr, ch)] = val
            return frame  # echo

        if command == CMD_PAGING_FADER_POSITION and len(body) == 3:
            _attr, ch, val = body
            if ch in self._paging_fader:
                self._paging_fader[ch] = val
            return frame

        if command == CMD_FADER_STEP and len(body) == 3:
            attr, ch, step = body
            key = (attr, ch)
            if key in self._fader:
                new_pos = _fader_apply_step(self._fader[key], step)
                self._fader[key] = new_pos
                return _build_frame(CMD_FADER_STEP, attr, ch, new_pos)
            return frame

        if command == CMD_NAME_REQUEST and len(body) >= 3:
            # F0 40 <attr> <ch> -> C0 09 <attr> <ch> <7 ASCII bytes, NUL-padded>
            attr, ch = body[1], body[2]
            prefix = "input" if attr == ATTR_INPUT else "output"
            name = str(self.state.get(f"{prefix}_{ch + 1}_name") or "")
            encoded = name.encode("ascii", errors="replace")[:7]
            encoded = encoded + b"\x00" * (7 - len(encoded))
            return _build_frame(RESP_NAME, attr, ch, *encoded)

        if command == CMD_ANC_REFERENCE and len(body) == 1:
            ch = body[0]
            level = self._anc_ref.get(ch, ANC_REF_0DB)
            return _build_frame(RESP_ANC_REFERENCE, level)

        if command == CMD_ANC_ADJUST and len(body) == 2:
            # Acknowledge with OK (0x00).
            return _build_frame(RESP_ANC_ADJUST, 0x00)

        # Everything else: the device confirms by echoing the frame back.
        return frame
