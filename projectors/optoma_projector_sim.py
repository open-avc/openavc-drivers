"""
Optoma projector simulator (Optoma RS232 / LAN "~XX" protocol, Telnet
port 23).

Implements the projector side of Optoma's ASCII grammar as documented
in the RS232 Protocol Function List:

- Commands are ``~XXnnn v<CR>`` with a two-digit projector ID; the
  simulator answers when addressed as ``00`` (any) or with its own ID
  and stays silent otherwise, like an RS-232 chain.
- Writes answer a bare ``P`` / ``F``; reads answer ``Ok<value>``
  (config ``ok_casing: "OK"`` switches to the older-firmware casing) —
  no command echo, the ambiguity that forces the driver's serialized
  request design.
- Power transitions emit the documented unsolicited INFO lines. Real
  units spread these over seconds (warm-up, then ready); the simulator
  compresses time and appends the full burst to the ack so round trips
  stay deterministic: power on answers ``P`` + ``INFO1`` + ``INFO24``,
  power off answers ``P`` + ``INFO2`` + ``INFO0``. Fault pushes can be
  driven from tests / the wire via ``fault_notice()``.
- ``has_lens`` / ``has_shutter`` / ``has_audio`` config flags answer
  ``F`` for absent hardware, exercising the driver's benign-reject
  path; ``volume_max`` (default 10) mirrors the per-model volume range.
- The input select write codes and the source read codes are different
  spaces on the real wire (write 1 = HDMI1, reads back 7); the
  simulator translates exactly like the protocol document.
- Changing state from the Simulator UI does not emit INFO lines (the
  driver's poll picks the change up), matching a UI edit rather than a
  protocol event.

Driver side: ``projectors/optoma_projector.py``.
"""

from __future__ import annotations

import logging

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

# Input select (~XX12) write code -> source read (~XX121) code.
INPUT_WRITE_TO_READ = {
    1: 7,     # HDMI1
    15: 8,    # HDMI2
    16: 9,    # HDMI3
    2: 1,     # DVI-D
    3: 1,     # DVI-A
    4: 6,     # BNC
    5: 2,     # VGA1
    6: 3,     # VGA2
    9: 4,     # S-Video
    10: 5,    # Video
    11: 10,   # Wireless
    14: 11,   # Component
    17: 12,   # Flash Drive
    18: 13,   # Network Display
    19: 14,   # USB Display
    20: 15,   # DisplayPort
    21: 16,   # HDBaseT
    22: 18,   # 3G-SDI
    23: 17,   # Multimedia
    24: 20,   # Smart TV
}

# Display mode (~XX20) write code -> mode read (~XX123) code. Identity
# except DICOM SIM (write 13, reads back 10).
MODE_WRITE_TO_READ = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 13: 10, 19: 19}

ASPECT_CODES = {1, 2, 3, 7}
LENS_SHIFT_PARAMS = {3, 4, 5, 6}
REMOTE_KEYS = {10, 11, 12, 13, 14, 20}


class OptomaProjectorSimulator(TCPSimulator):
    """Simulates an Optoma projector on the ~XX ASCII protocol."""

    SIMULATOR_INFO = {
        "driver_id": "optoma_projector",
        "name": "Optoma Projector Simulator",
        "delimiter": "\r",
        "initial_state": {
            "power": 1,
            "input_code": 7,        # source read code space (7 = HDMI1)
            "av_mute": 0,
            "audio_mute": 0,
            "shutter_closed": 0,
            "freeze": 0,
            "volume": 5,
            "display_mode_code": 1,
            "aspect_code": 7,
            "brightness": 50,
            "contrast": 50,
            "v_keystone": 0,
            "h_keystone": 0,
            "light_hours_normal": 1234,
            "light_hours_eco": 210,
            "resolution": "1920x1200",
            "model_class": 5,       # Ok5 = WUXGA class
            "firmware": "C01.23",
            "serial_number": "Q8EJ8850001",
            "lens_memory_saved": 0,
        },
        "controls": [
            {"type": "select", "key": "power", "label": "Power (1=on, 0=standby)",
             "options": [0, 1]},
            {"type": "select", "key": "input_code",
             "label": "Source read code (7=HDMI1, 8=HDMI2, 15=DP, 16=HDBaseT)",
             "options": sorted(set(INPUT_WRITE_TO_READ.values()))},
            {"type": "select", "key": "av_mute", "label": "A/V Mute (1=muted)",
             "options": [0, 1]},
            {"type": "select", "key": "audio_mute", "label": "Audio Mute (1=muted)",
             "options": [0, 1]},
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0,
             "max": 15, "step": 1},
            {"type": "slider", "key": "brightness", "label": "Brightness",
             "min": 0, "max": 100, "step": 1},
            {"type": "slider", "key": "contrast", "label": "Contrast",
             "min": 0, "max": 100, "step": 1},
            {"type": "slider", "key": "light_hours_normal",
             "label": "Light Hours (Normal)", "min": 0, "max": 30000, "step": 1},
            {"type": "indicator", "key": "model_class", "label": "Model Class"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        cfg = config or {}
        # 0 = unassigned: the unit answers the "00" broadcast ID only.
        self._projector_id = int(cfg.get("projector_id", 0) or 0)
        self._ok = str(cfg.get("ok_casing", "Ok"))
        self._volume_max = int(cfg.get("volume_max", 10))
        self._has_lens = bool(cfg.get("has_lens", True))
        self._has_shutter = bool(cfg.get("has_shutter", True))
        self._has_audio = bool(cfg.get("has_audio", True))
        # Single total vs the laser generations' "normal/eco" pair.
        self._dual_hours = bool(cfg.get("dual_hours", True))

    # ── Reply helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _line(text: str) -> bytes:
        return (text + "\r").encode("ascii")

    def _value(self, value) -> bytes:
        return self._line(f"{self._ok}{value}")

    _PASS = b"P\r"
    _FAIL = b"F\r"

    def fault_notice(self, code: int) -> bytes:
        """Wire bytes for an unsolicited fault push (use with .push())."""
        return self._line(f"INFO{int(code)}")

    # ── Dispatch ───────────────────────────────────────────────────────────

    def handle_command(self, data: bytes) -> bytes | None:
        frame = data.strip(b"\r\n\x00 ")
        if not frame.startswith(b"~") or len(frame) < 5:
            return self._FAIL
        try:
            text = frame.decode("ascii")
        except UnicodeDecodeError:
            return self._FAIL
        head, _, param = text[1:].partition(" ")
        param = param.strip()
        if len(head) < 4 or not head.isdigit():
            return self._FAIL
        addressed, cmd = head[:2], head[2:]
        # ID gate: 00 addresses any projector; otherwise the IDs must
        # match. A mismatched frame gets no reply at all (RS-232 chain
        # semantics).
        if addressed != "00" and int(addressed) != self._projector_id:
            return None

        if param and (param.lstrip("-").isdigit()):
            return self._dispatch(cmd, int(param))
        return self._FAIL

    def _dispatch(self, cmd: str, value: int) -> bytes:
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            return self._FAIL
        return handler(value)

    # ── Writes ─────────────────────────────────────────────────────────────

    def _cmd_00(self, value: int) -> bytes:  # power
        if value == 1:
            if int(self.state["power"]) != 1:
                self.set_state("power", 1)
                # Compressed warm-up: ack + warming + ready.
                return self._PASS + self._line("INFO1") + self._line("INFO24")
            return self._PASS
        if value in (0, 2):
            if int(self.state["power"]) != 0:
                self.set_state("power", 0)
                return self._PASS + self._line("INFO2") + self._line("INFO0")
            return self._PASS
        return self._FAIL

    def _cmd_01(self, value: int) -> bytes:  # resync
        return self._PASS if value == 1 else self._FAIL

    def _cmd_02(self, value: int) -> bytes:  # AV mute
        if value in (0, 1, 2):
            self.set_state("av_mute", 1 if value == 1 else 0)
            return self._PASS
        return self._FAIL

    def _cmd_325(self, value: int) -> bytes:  # shutter
        if not self._has_shutter:
            return self._FAIL
        if value in (0, 1):
            self.set_state("shutter_closed", value)
            return self._PASS
        return self._FAIL

    def _cmd_80(self, value: int) -> bytes:  # audio mute
        if not self._has_audio:
            return self._FAIL
        if value in (0, 1, 2):
            self.set_state("audio_mute", 1 if value == 1 else 0)
            return self._PASS
        return self._FAIL

    def _cmd_81(self, value: int) -> bytes:  # volume
        if not self._has_audio:
            return self._FAIL
        if 0 <= value <= self._volume_max:
            self.set_state("volume", value)
            return self._PASS
        return self._FAIL

    def _cmd_04(self, value: int) -> bytes:  # freeze
        if value in (0, 1, 2):
            self.set_state("freeze", 1 if value == 1 else 0)
            return self._PASS
        return self._FAIL

    def _cmd_12(self, value: int) -> bytes:  # input select
        read_code = INPUT_WRITE_TO_READ.get(value)
        if read_code is None:
            return self._FAIL
        self.set_state("input_code", read_code)
        return self._PASS

    def _cmd_20(self, value: int) -> bytes:  # display mode
        read_code = MODE_WRITE_TO_READ.get(value)
        if read_code is None:
            return self._FAIL
        self.set_state("display_mode_code", read_code)
        return self._PASS

    def _cmd_60(self, value: int) -> bytes:  # aspect
        if value not in ASPECT_CODES:
            return self._FAIL
        self.set_state("aspect_code", value)
        return self._PASS

    def _cmd_21(self, value: int) -> bytes:  # brightness
        if 0 <= value <= 100:
            self.set_state("brightness", value)
            return self._PASS
        return self._FAIL

    def _cmd_22(self, value: int) -> bytes:  # contrast
        if 0 <= value <= 100:
            self.set_state("contrast", value)
            return self._PASS
        return self._FAIL

    def _cmd_66(self, value: int) -> bytes:  # V keystone
        if -40 <= value <= 40:
            self.set_state("v_keystone", value)
            return self._PASS
        return self._FAIL

    def _cmd_65(self, value: int) -> bytes:  # H keystone
        if -40 <= value <= 40:
            self.set_state("h_keystone", value)
            return self._PASS
        return self._FAIL

    def _cmd_84(self, value: int) -> bytes:  # lens shift (3-6; 1/2 lock)
        if not self._has_lens:
            return self._FAIL
        if value in LENS_SHIFT_PARAMS or value in (1, 2):
            return self._PASS
        return self._FAIL

    def _cmd_307(self, value: int) -> bytes:  # lens zoom
        if self._has_lens and value in (1, 2):
            return self._PASS
        return self._FAIL

    def _cmd_308(self, value: int) -> bytes:  # lens focus
        if self._has_lens and value in (1, 2):
            return self._PASS
        return self._FAIL

    def _cmd_359(self, value: int) -> bytes:  # lens memory apply
        if self._has_lens and 1 <= value <= 10:
            return self._PASS
        return self._FAIL

    def _cmd_360(self, value: int) -> bytes:  # lens memory save
        if self._has_lens and 1 <= value <= 10:
            self.set_state("lens_memory_saved", value)
            return self._PASS
        return self._FAIL

    def _cmd_525(self, value: int) -> bytes:  # lens calibration
        if self._has_lens and value == 1:
            return self._PASS
        return self._FAIL

    def _cmd_140(self, value: int) -> bytes:  # remote key
        return self._PASS if value in REMOTE_KEYS else self._FAIL

    # ── Reads ──────────────────────────────────────────────────────────────

    def _cmd_124(self, value: int) -> bytes:  # power read
        if value != 1:
            return self._FAIL
        return self._value(int(self.state["power"]))

    def _cmd_121(self, value: int) -> bytes:  # source read
        if value != 1:
            return self._FAIL
        return self._value(int(self.state["input_code"]))

    def _cmd_355(self, value: int) -> bytes:  # AV mute read
        if value != 1:
            return self._FAIL
        return self._value(int(self.state["av_mute"]))

    def _cmd_356(self, value: int) -> bytes:  # audio mute read
        if value != 1 or not self._has_audio:
            return self._FAIL
        return self._value(int(self.state["audio_mute"]))

    def _cmd_123(self, value: int) -> bytes:  # display mode read
        if value != 1:
            return self._FAIL
        return self._value(int(self.state["display_mode_code"]))

    def _cmd_127(self, value: int) -> bytes:  # aspect read
        if value != 1:
            return self._FAIL
        return self._value(int(self.state["aspect_code"]))

    def _cmd_125(self, value: int) -> bytes:  # brightness read
        if value != 1:
            return self._FAIL
        return self._value(int(self.state["brightness"]))

    def _cmd_126(self, value: int) -> bytes:  # contrast read
        if value != 1:
            return self._FAIL
        return self._value(int(self.state["contrast"]))

    def _cmd_108(self, value: int) -> bytes:  # light-source hours
        if value != 1:
            return self._FAIL
        normal = int(self.state["light_hours_normal"])
        eco = int(self.state["light_hours_eco"])
        if self._dual_hours:
            return self._value(f"{normal:07d}/{eco:07d}")
        return self._value(normal + eco)

    def _cmd_150(self, value: int) -> bytes:  # information reads
        if value == 4:
            if int(self.state["power"]) != 1:
                return self._FAIL
            return self._value(str(self.state["resolution"]))
        return self._FAIL

    def _cmd_151(self, value: int) -> bytes:  # model name / class
        if value != 1:
            return self._FAIL
        # Older units answer a numeric resolution-class code; newer ones
        # a model string — the UI control can hold either.
        return self._value(self.state["model_class"])

    def _cmd_122(self, value: int) -> bytes:  # firmware
        if value != 1:
            return self._FAIL
        return self._value(str(self.state["firmware"]))

    def _cmd_353(self, value: int) -> bytes:  # serial number
        if value != 1:
            return self._FAIL
        return self._value(str(self.state["serial_number"]))
