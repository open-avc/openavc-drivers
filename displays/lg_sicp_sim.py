"""
LG SICP Display — Simulator

Full LG SICP text protocol simulator with:
  - Multiple displays addressed by Set ID (the ``set_ids`` config, default
    a single display), each with its own state
  - Power, input select, volume, audio mute (inverted on the wire:
    00 = mute), screen off
  - Picture surface: brightness, contrast, sharpness, color, tint, color
    temperature, backlight, picture mode, aspect ratio, energy saving
  - Remote/local key lock, IR key passthrough (mc), signal check (sv)
  - Health block: temperature, elapsed hours, serial number, software
    version
  - Protocol-accurate framing: Set ID travels as HEX (display 10 answers
    to "0A"), all numeric data is two hex digits, acks terminate with 'x'
    and echo only Cmd2 — including the dx ack, which *starts* with 'x'

Request:  [Cmd1][Cmd2] [Set ID] [Data]\\r         e.g.  b"ka 01 FF\\r"
Ack:      [Cmd2] [Set ID] OK|NG[Data]x           e.g.  b"a 01 OK01x"

Broadcast Set ID 00 applies a write to every present display and — per the
manual — sends NO acknowledgement. A request to a Set ID that isn't
present gets no reply, modelling an absent display on the chain. An
unknown command or out-of-range value answers NG, modelling a model that
doesn't support it.

The lowest Set ID is the "primary" display and is backed by the
simulator's ``state`` so the auto-generated UI controls drive it; any
additional Set IDs are backed by an internal per-display map (wire-only,
for exercising a driver's multi-display roster).
"""

from simulator.tcp_simulator import TCPSimulator


INPUT_CODES = {
    "90": "HDMI 1",
    "A0": "HDMI 1 (PC)",
    "91": "HDMI 2 / OPS",
    "A1": "HDMI 2 / OPS (PC)",
    "C0": "DisplayPort",
    "D0": "DisplayPort (PC)",
    "70": "DVI-D (PC)",
    "80": "DVI-D (DTV)",
    "60": "RGB",
    "40": "Component",
}
INPUT_BY_NAME = {v: k for k, v in INPUT_CODES.items()}

PICTURE_MODE_CODES = {
    "00": "Vivid",
    "01": "Standard",
    "02": "Cinema",
    "03": "Sports",
    "04": "Game",
    "05": "Expert 1",
    "06": "Expert 2",
    "08": "APS",
    "09": "Photos",
    "11": "Calibration",
}
PICTURE_MODE_BY_NAME = {v: k for k, v in PICTURE_MODE_CODES.items()}

ASPECT_CODES = {
    "01": "4:3",
    "02": "16:9",
    "04": "Zoom",
    "06": "Set by Program",
    "09": "Just Scan",
}
ASPECT_BY_NAME = {v: k for k, v in ASPECT_CODES.items()}

ENERGY_CODES = {
    "00": "Off",
    "01": "Minimum",
    "02": "Medium",
    "03": "Maximum",
    "04": "Automatic",
    "05": "Screen Off",
}
ENERGY_BY_NAME = {v: k for k, v in ENERGY_CODES.items()}

# Per-command value ceiling (hex data, applied on writes).
_LEVEL_MAX = {
    "kf": 100,  # volume
    "kg": 100,  # contrast
    "kh": 100,  # brightness
    "kk": 50,   # sharpness
    "ki": 100,  # color
    "kj": 100,  # tint
    "xu": 100,  # color temperature
    "mg": 100,  # backlight
}
_LEVEL_FIELD = {
    "kf": "volume",
    "kg": "contrast",
    "kh": "brightness",
    "kk": "sharpness",
    "ki": "color",
    "kj": "tint",
    "xu": "color_temperature",
    "mg": "backlight",
}

DEFAULT_DISPLAY = {
    "power": "off",
    "input": "HDMI 1",
    "volume": 30,
    "mute": False,
    "screen_off": False,
    "signal": "present",
    "brightness": 50,
    "contrast": 70,
    "sharpness": 25,
    "color": 50,
    "tint": 50,
    "color_temperature": 50,
    "backlight": 80,
    "picture_mode": "Standard",
    "aspect_ratio": "16:9",
    "energy_saving": "Off",
    "key_lock": False,
    "temperature": 38,
    "usage_hours": 1234,
    "serial_number": "SIM0LG0000001",
    "software_version": "03.11.20",
}


class LgSicpSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "lg_sicp",
        "name": "LG SICP Display Simulator",
        "category": "display",
        "transport": "tcp",
        "default_port": 9761,
        "delimiter": "\r",
        "initial_state": dict(DEFAULT_DISPLAY),
        "delays": {
            "command_response": 0.025,
        },
        "error_modes": {
            "communication_timeout": {
                "description": (
                    "Display stops responding to all commands (network "
                    "issue or display in deep standby)"
                ),
                "behavior": "no_response",
            },
            "no_signal": {
                "description": (
                    "No signal detected on the current input (source "
                    "unplugged or powered off)"
                ),
                # No "behavior" key on purpose: the sim keeps answering;
                # the visible effect is the state change (sv then reports
                # no signal). Only "no_response" / "corrupt_response" are
                # wired behaviors, and the runtime applies "set_state".
                "set_state": {"signal": "none"},
            },
        },
        "controls": [
            {"type": "power", "key": "power"},
            {
                "type": "select",
                "key": "input",
                "label": "Input",
                "options": list(INPUT_CODES.values()),
            },
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 100},
            {"type": "toggle", "key": "mute", "label": "Audio Mute"},
            {"type": "toggle", "key": "screen_off", "label": "Screen Off"},
            {
                "type": "select",
                "key": "signal",
                "label": "Input Signal",
                "options": ["present", "none"],
            },
            {"type": "slider", "key": "brightness", "label": "Brightness", "min": 0, "max": 100},
            {"type": "slider", "key": "contrast", "label": "Contrast", "min": 0, "max": 100},
            {"type": "slider", "key": "backlight", "label": "Backlight", "min": 0, "max": 100},
            {
                "type": "select",
                "key": "picture_mode",
                "label": "Picture Mode",
                "options": list(PICTURE_MODE_CODES.values()),
            },
            {
                "type": "select",
                "key": "aspect_ratio",
                "label": "Aspect Ratio",
                "options": list(ASPECT_CODES.values()),
            },
            {
                "type": "select",
                "key": "energy_saving",
                "label": "Energy Saving",
                "options": list(ENERGY_CODES.values()),
            },
            {"type": "toggle", "key": "key_lock", "label": "Remote/Key Lock"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        present = self._parse_set_ids(self.config.get("set_ids", "1"))
        self._present_ids = present
        self._primary = present[0]
        # Non-primary displays live in an internal map; the primary is
        # backed by self.state so the UI controls it.
        self._displays: dict[int, dict] = {
            sid: dict(DEFAULT_DISPLAY) for sid in present if sid != self._primary
        }

    @staticmethod
    def _parse_set_ids(raw) -> list[int]:
        ids: list[int] = []
        for part in str(raw).replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= 1000 and n not in ids:
                ids.append(n)
        return sorted(ids) or [1]

    # ── Per-display state access ──

    def _get(self, set_id: int, field: str):
        if set_id == self._primary:
            return self.state.get(field, DEFAULT_DISPLAY.get(field))
        return self._displays.get(set_id, {}).get(field, DEFAULT_DISPLAY.get(field))

    def _put(self, set_id: int, field: str, value) -> None:
        if set_id == self._primary:
            self.set_state(field, value)
        else:
            self._displays.setdefault(set_id, dict(DEFAULT_DISPLAY))[field] = value

    # ── Protocol ──

    def handle_command(self, data: bytes) -> bytes | None:
        """Parse incoming SICP lines and return ack responses.

        The driver sends "[Cmd1][Cmd2] [Set ID hex] [Data]\\r"; several
        lines may arrive in one chunk (the framework usually pre-splits,
        but direct calls in tests may not).
        """
        response = b""
        for line in data.split(b"\r"):
            line = line.strip()
            if not line:
                continue
            resp = self._process_line(line)
            if resp:
                response += resp
        return response or None

    def _process_line(self, line: bytes) -> bytes | None:
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError:
            return None
        parts = text.split(" ", 2)
        if len(parts) < 3 or len(parts[0]) != 2:
            return None
        cmd, sid_text, payload = parts[0], parts[1], parts[2].strip()
        try:
            set_id = int(sid_text, 16)  # Set ID is hex on the wire
        except ValueError:
            return None

        if set_id == 0:
            # Broadcast: apply to every present display, no acks (manual:
            # "each monitor set does not send an acknowledgement").
            for sid in self._present_ids:
                self._apply(cmd, sid, payload)
            return None
        if set_id not in self._present_ids:
            return None  # absent display: silence

        ok, echo = self._apply(cmd, set_id, payload)
        status = b"OK" if ok else b"NG"
        return (
            cmd[1].encode("ascii")
            + b" "
            + sid_text.encode("ascii")  # echo the Set ID as it was addressed
            + b" "
            + status
            + echo.encode("ascii")
            + b"x"
        )

    def _apply(self, cmd: str, set_id: int, payload: str) -> tuple[bool, str]:
        """Apply one command to one display. Returns (ok, echo_data)."""
        data = payload.strip().upper()

        # ── Power (ka) ──
        if cmd == "ka":
            if data == "FF":
                return True, "01" if self._get(set_id, "power") == "on" else "00"
            if data in ("00", "01"):
                self._put(set_id, "power", "on" if data == "01" else "off")
                return True, data
            return False, data

        # ── Select input (xb) ──
        if cmd == "xb":
            if data == "FF":
                return True, INPUT_BY_NAME.get(self._get(set_id, "input"), "90")
            if data in INPUT_CODES:
                self._put(set_id, "input", INPUT_CODES[data])
                return True, data
            return False, data  # unsupported input on this model

        # ── Levels (kf/kg/kh/kk/ki/kj/xu/mg) ──
        if cmd in _LEVEL_FIELD:
            field = _LEVEL_FIELD[cmd]
            if data == "FF":
                return True, format(int(self._get(set_id, field)), "02X")
            try:
                value = int(data, 16)
            except ValueError:
                return False, data
            if not 0 <= value <= _LEVEL_MAX[cmd]:
                return False, data
            self._put(set_id, field, value)
            return True, data

        # ── Audio mute (ke) — INVERTED: 00 = mute, 01 = unmute ──
        if cmd == "ke":
            if data == "FF":
                return True, "00" if self._get(set_id, "mute") else "01"
            if data in ("00", "01"):
                self._put(set_id, "mute", data == "00")
                return True, data
            return False, data

        # ── Screen off (kd) ──
        if cmd == "kd":
            if data == "FF":
                return True, "01" if self._get(set_id, "screen_off") else "00"
            if data in ("00", "01"):
                self._put(set_id, "screen_off", data == "01")
                return True, data
            return False, data

        # ── Picture mode (dx) — the ack's Cmd2 is itself an 'x' ──
        if cmd == "dx":
            if data == "FF":
                return True, PICTURE_MODE_BY_NAME.get(
                    self._get(set_id, "picture_mode"), "01"
                )
            if data in PICTURE_MODE_CODES:
                self._put(set_id, "picture_mode", PICTURE_MODE_CODES[data])
                return True, data
            return False, data

        # ── Aspect ratio (kc), incl. the Cinema Zoom band 10-1F ──
        if cmd == "kc":
            if data == "FF":
                current = self._get(set_id, "aspect_ratio")
                if str(current).startswith("Cinema Zoom "):
                    step = int(str(current).rsplit(" ", 1)[1])
                    return True, format(0x0F + step, "02X")
                return True, ASPECT_BY_NAME.get(current, "02")
            if data in ASPECT_CODES:
                self._put(set_id, "aspect_ratio", ASPECT_CODES[data])
                return True, data
            try:
                value = int(data, 16)
            except ValueError:
                return False, data
            if 0x10 <= value <= 0x1F:
                self._put(set_id, "aspect_ratio", f"Cinema Zoom {value - 0x0F}")
                return True, data
            return False, data

        # ── Energy saving (jq) ──
        if cmd == "jq":
            if data == "FF":
                return True, ENERGY_BY_NAME.get(
                    self._get(set_id, "energy_saving"), "00"
                )
            if data in ENERGY_CODES:
                self._put(set_id, "energy_saving", ENERGY_CODES[data])
                return True, data
            return False, data

        # ── Remote/local key lock (km) ──
        if cmd == "km":
            if data == "FF":
                return True, "01" if self._get(set_id, "key_lock") else "00"
            if data in ("00", "01"):
                self._put(set_id, "key_lock", data == "01")
                return True, data
            return False, data

        # ── IR key passthrough (mc) — ack only, no state model ──
        if cmd == "mc":
            if len(data) == 2:
                return True, data
            return False, data

        # ── Signal check (sv 02 FF) ──
        if cmd == "sv":
            if data.startswith("02"):
                present = self._get(set_id, "signal") == "present"
                return True, "02" + ("01" if present else "00")
            return False, data

        # ── Health block ──
        if cmd == "dn":  # internal temperature, hex Celsius
            if data == "FF":
                return True, format(int(self._get(set_id, "temperature")), "02X")
            return False, data
        if cmd == "dl":  # elapsed hours, hex
            if data == "FF":
                return True, format(int(self._get(set_id, "usage_hours")), "02X")
            return False, data
        if cmd == "fy":  # serial number, ASCII
            if data == "FF":
                return True, str(self._get(set_id, "serial_number"))
            return False, data
        if cmd == "fz":  # software version, ASCII
            if data == "FF":
                return True, str(self._get(set_id, "software_version"))
            return False, data

        # Unknown command: NG (models a display that doesn't support it).
        return False, data
