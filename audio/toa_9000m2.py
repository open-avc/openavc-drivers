"""
OpenAVC TOA 9000M2 Series driver.

Controls the TOA M-9000M2 Digital Matrix Mixer (and the protocol-compatible
A-9000M2 Series mixer/amplifiers) over RS-232C. 8 mixing inputs x 8 outputs.

Protocol (9000M2 Series RS-232C Protocol Manual, Ver 2.00A):
    Every message is a binary frame:  <command 0x80-0xFF> <length N> <N data
    bytes 0x00-0x7F>.  The command byte always has the high bit set; data bytes
    never do, which makes framing and resync unambiguous (a runaway or partial
    frame resyncs on the next high-bit byte).  9600/19200/38400/57600 8N1.

Feedback model (important):
    This device is write-then-echo.  When it accepts a command it echoes the
    exact bytes back; an out-of-range channel value comes back inverted.  There
    is NO "read current value" for faders, mutes, crosspoints, EQ, tone, etc.
    The only true device->controller reads in the whole protocol are the
    channel name (F0 -> C0) and the ANC reference level (F3 -> C2).  So the
    driver tracks state OPTIMISTICALLY from the echoes it gets back (confirmed-
    accepted state), reads names + ANC reference for real, and treats the fader
    step command (0x93), which echoes the resulting absolute position, as true
    fader feedback.  Changes made at the device's front panel or by a scene
    recall to unreadable parameters cannot be reflected back -- a limitation of
    the hardware protocol, not the driver.

Source:
    https://www.toaelectronics.com/toa-assets/9000M2_PROTOCOL_MANUAL_EN.pdf
"""

from __future__ import annotations

from collections import deque
from typing import Any

from server.drivers.base import BaseDriver, CommandParamError
from server.transport.frame_parsers import CallableFrameParser, FrameParser
from server.utils.logger import get_logger

log = get_logger(__name__)

NUM_INPUTS = 8
NUM_OUTPUTS = 8

# Sentinel dB for a fader / crosspoint at -infinity (silent).  The protocol's
# position 0 means -inf; we surface it as a finite number so it round-trips
# through a UI slider and JSON state (float('-inf') is not valid JSON).
MUTE_DB = -80.0


# ── Command bytes ───────────────────────────────────────────────────────────

CMD_FADER_POSITION = 0x91       # input/output fader gain (absolute position)
CMD_PAGING_FADER_POSITION = 0x96  # paging output fader gain (absolute position)
CMD_FADER_STEP = 0x93           # input/output fader gain (step; echoes result)
CMD_CROSSPOINT = 0x95           # crosspoint gain (absolute or step)
CMD_CHANNEL_ONOFF = 0x92        # channel on/off
CMD_POWER = 0xF4                # power on/off
CMD_TONE = 0xAA                 # bass/treble
CMD_EQ = 0xA1                   # parametric EQ
CMD_LOUDNESS = 0xAB             # loudness compensation
CMD_FILTER = 0xA2               # HPF / LPF
CMD_INPUT_SENSITIVITY = 0xAC    # input sensitivity (input only)
CMD_PHANTOM = 0x87              # phantom power (input only)
CMD_PRESET_RECALL = 0xF1        # preset / scene / event recall
CMD_PAGING_EVENT = 0xF2         # paging event start/stop
CMD_SPEAKER_PRESET = 0xAD       # speaker EQ preset (output only)
CMD_ANC_ADJUST = 0xAE           # ANC reference adjust
CMD_ANC_REFERENCE = 0xF3        # ANC get reference
CMD_NAME_REQUEST = 0xF0         # channel name request

# Response codes (device -> controller for the readable values)
RESP_NAME = 0xC0                # channel name response
RESP_ANC_ADJUST = 0xC1         # ANC adjust OK/NG
RESP_ANC_REFERENCE = 0xC2      # ANC reference level

ATTR_INPUT = 0x00
ATTR_OUTPUT = 0x01

NAME_REQUEST_SUBCODE = 0x40     # F0 sub-code that selects "channel name"


# ── Value <-> byte lookup tables ────────────────────────────────────────────

def _build_fader_db_table() -> dict[int, float]:
    """Position (0x00-0x7E) -> dB, per the manual's POSITION V/S GAIN TABLE.

    0x00 = -inf; 0x01 = -70; 0x02-0x06 step 2 dB up to -60; 0x06-0x1A step
    1 dB up to -40; 0x1A-0x7E step 0.5 dB up to +10.
    """
    table: dict[int, float] = {0x00: MUTE_DB, 0x01: -70.0}
    for pos in range(0x02, 0x07):        # -68, -66, -64, -62, -60
        table[pos] = -60.0 - (0x06 - pos) * 2.0
    for pos in range(0x07, 0x1B):        # -59 .. -40 (1 dB)
        table[pos] = -59.0 + (pos - 0x07) * 1.0
    for pos in range(0x1B, 0x7F):        # -39.5 .. +10 (0.5 dB)
        table[pos] = -40.0 + (pos - 0x1A) * 0.5
    return table


_FADER_DB_BY_POS = _build_fader_db_table()
# Nearest-position lookup uses only the finite positions (skip -inf sentinel).
_FADER_FINITE = {p: db for p, db in _FADER_DB_BY_POS.items() if p != 0x00}
_FADER_MIN_FINITE_DB = min(_FADER_FINITE.values())   # -70.0


def fader_db_to_pos(db: float) -> int:
    """dB -> nearest fader position byte. Below the lowest finite step -> -inf."""
    if db < _FADER_MIN_FINITE_DB:
        return 0x00
    return min(_FADER_FINITE, key=lambda p: abs(_FADER_FINITE[p] - db))


def fader_pos_to_db(pos: int) -> float:
    """Fader position byte -> dB (MUTE_DB for -inf; MUTE_DB for anything out of range)."""
    return _FADER_DB_BY_POS.get(pos, MUTE_DB)


# Fader step (0x93): 0x41-0x5F = +0.5 .. +15.5 dB, 0x61-0x7F = -0.5 .. -15.5 dB.
def fader_step_to_byte(step_db: float) -> int:
    steps = int(round(step_db / 0.5))
    if steps == 0:
        raise CommandParamError("step_db must be a non-zero multiple of 0.5 dB")
    if steps > 0:
        if steps > 31:
            steps = 31
        return 0x40 + steps
    steps = -steps
    if steps > 31:
        steps = 31
    return 0x60 + steps


# Crosspoint gain (0x95): 0x00 = -inf, 0x01-0x51 = -70 .. +10 dB (1 dB).
def xpt_db_to_pos(db: float) -> int:
    if db < -70.0:
        return 0x00
    pos = int(round(db)) + 71
    return max(0x01, min(0x51, pos))


def xpt_pos_to_db(pos: int) -> float:
    if pos == 0x00:
        return MUTE_DB
    if 0x01 <= pos <= 0x51:
        return float(pos - 71)
    return MUTE_DB


# Crosspoint step: 0x60-0x6F = -1..-16 dB, 0x70-0x7F = +1..+16 dB.
def xpt_step_to_byte(step_db: int) -> int:
    steps = int(round(step_db))
    if steps == 0:
        raise CommandParamError("step_db must be a non-zero integer number of dB")
    if steps > 0:
        return 0x70 + min(steps, 16) - 1
    return 0x60 + min(-steps, 16) - 1


# Tone / EQ gain (0x00-0x18 = -12 .. +12 dB, 1 dB steps; 0x0C = 0).
def gain_db_to_byte(db: float) -> int:
    return max(0x00, min(0x18, int(round(db)) + 12))


def gain_byte_to_db(byte: int) -> float:
    return float(byte - 12)


# ANC adjust (0x00-0x14 = -10 .. +10 dB).
def anc_db_to_byte(db: float) -> int:
    return max(0x00, min(0x14, int(round(db)) + 10))


# ANC reference level (0x00-0x5B = -50 .. +41 dB).
def anc_ref_byte_to_db(byte: int) -> float:
    return float(byte - 50)


# EQ Q values (index = byte).
EQ_Q_VALUES = ["0.3", "0.5", "0.7", "1", "1.5", "2", "3", "5"]

# 1/3-octave center frequencies for EQ (byte 0x00-0x1E).
EQ_FREQ_VALUES = [
    "20", "25", "31.5", "40", "50", "63", "80", "100", "125", "160", "200",
    "250", "315", "400", "500", "630", "800", "1000", "1250", "1600", "2000",
    "2500", "3150", "4000", "5000", "6300", "8000", "10000", "12500", "16000",
    "20000",
]

# High-pass filter frequencies (byte 0x00-0x1F). 0x00 = OFF.
FILTER_HPF_VALUES = [
    "OFF", "20", "25", "31.5", "40", "50", "63", "80", "100", "125", "160",
    "200", "250", "315", "400", "500", "630", "800", "1000", "1250", "1600",
    "2000", "2500", "3150", "4000", "5000", "6300", "8000", "10000", "12500",
    "16000", "20000",
]

# Low-pass filter frequencies (byte 0x00-0x1F). 0x1F = OFF.
FILTER_LPF_VALUES = [
    "20", "25", "31.5", "40", "50", "63", "80", "100", "125", "160", "200",
    "250", "315", "400", "500", "630", "800", "1000", "1250", "1600", "2000",
    "2500", "3150", "4000", "5000", "6300", "8000", "10000", "12500", "16000",
    "20000", "OFF",
]

# Input sensitivity (byte 0x00-0x08), dB.
SENSITIVITY_VALUES = ["-10", "-18", "-24", "-30", "-36", "-42", "-48", "-54", "-60"]

# Speaker EQ presets (byte 0x00-0x1E).
SPEAKER_PRESET_VALUES = [
    "ALL FLAT", "F-122", "F-122 LOWCUT", "H-1", "H-1 LOWCUT", "H-2",
    "H-2 LOWCUT", "H-3", "H-3 LOWCUT", "HB-1", "FB-100", "SW FOR F-122",
    "SR-S4", "HX-5", "HX-5 LOWCUT", "FB-120", "F-1522", "FB-2322", "FB-2352",
    "FB-2852", "SR-H2S", "SR-H2L", "SR-H3S", "SR-H3L", "HS-120", "HS-150",
    "HS-1200", "HS-1500", "F-1000 LOWCUT", "F-1300 LOWCUT", "F-2000 LOWCUT",
]

# ASCII bytes that appear in channel names (per the manual's name table). Any
# NUL byte terminates / pads the 7-byte name field.
_NAME_ENCODING = "ascii"


# ── Frame helpers ───────────────────────────────────────────────────────────

def build_frame(command: int, *data: int) -> bytes:
    """Assemble a protocol frame: command, length, data..."""
    return bytes([command & 0xFF, len(data), *[d & 0x7F for d in data]])


def parse_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """CallableFrameParser function.

    Resyncs by skipping any leading bytes that aren't a command (high bit
    clear), then reads a full <command><len N><N data> frame. Returns
    (frame, remaining) when a whole frame is present, else (None, buffer).
    """
    start = 0
    while start < len(buffer) and not (buffer[start] & 0x80):
        start += 1
    if start:
        buffer = buffer[start:]
    if len(buffer) < 2:
        return None, buffer
    length = buffer[1]
    total = 2 + length
    if len(buffer) < total:
        return None, buffer
    return buffer[:total], buffer[total:]


# ── State variables ─────────────────────────────────────────────────────────

def _db_var(label: str, help_: str) -> dict[str, Any]:
    return {"type": "number", "label": label, "min": MUTE_DB, "max": 10.0, "help": help_}


def _build_state_vars() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        "power": {"type": "boolean", "label": "Power", "help": "True when the amplifier power is on."},
        "last_preset": {
            "type": "integer", "label": "Last Recalled Preset", "min": 0, "max": 32,
            "help": "Number of the most recently recalled preset (scene/event), 1-32.",
        },
    }
    for i in range(1, NUM_INPUTS + 1):
        out[f"input_{i}_name"] = {"type": "string", "label": f"Input {i} Name", "help": "Channel name read from the device."}
        out[f"input_{i}_fader_db"] = _db_var(f"Input {i} Gain (dB)", "Input fader gain. -80 dB = -inf.")
        out[f"input_{i}_on"] = {"type": "boolean", "label": f"Input {i} On"}
        out[f"input_{i}_phantom"] = {"type": "boolean", "label": f"Input {i} Phantom (+48V)"}
        out[f"input_{i}_loudness"] = {"type": "boolean", "label": f"Input {i} Loudness"}
        out[f"input_{i}_eq_on"] = {"type": "boolean", "label": f"Input {i} EQ On"}
        out[f"input_{i}_bass_db"] = {"type": "number", "label": f"Input {i} Bass (dB)", "min": -12.0, "max": 12.0}
        out[f"input_{i}_treble_db"] = {"type": "number", "label": f"Input {i} Treble (dB)", "min": -12.0, "max": 12.0}
        out[f"input_{i}_hpf_hz"] = {"type": "number", "label": f"Input {i} HPF (Hz)", "min": 0.0, "max": 20000.0, "help": "High-pass filter corner; 0 = off."}
        out[f"input_{i}_lpf_hz"] = {"type": "number", "label": f"Input {i} LPF (Hz)", "min": 0.0, "max": 20000.0, "help": "Low-pass filter corner; 0 = off."}
        out[f"input_{i}_sensitivity_db"] = {"type": "number", "label": f"Input {i} Sensitivity (dB)", "min": -60.0, "max": -10.0}
    for o in range(1, NUM_OUTPUTS + 1):
        out[f"output_{o}_name"] = {"type": "string", "label": f"Output {o} Name", "help": "Channel name read from the device."}
        out[f"output_{o}_fader_db"] = _db_var(f"Output {o} Gain (dB)", "Output fader gain. -80 dB = -inf.")
        out[f"output_{o}_on"] = {"type": "boolean", "label": f"Output {o} On"}
        out[f"output_{o}_loudness"] = {"type": "boolean", "label": f"Output {o} Loudness"}
        out[f"output_{o}_eq_on"] = {"type": "boolean", "label": f"Output {o} EQ On"}
        out[f"output_{o}_bass_db"] = {"type": "number", "label": f"Output {o} Bass (dB)", "min": -12.0, "max": 12.0}
        out[f"output_{o}_treble_db"] = {"type": "number", "label": f"Output {o} Treble (dB)", "min": -12.0, "max": 12.0}
        out[f"output_{o}_hpf_hz"] = {"type": "number", "label": f"Output {o} HPF (Hz)", "min": 0.0, "max": 20000.0, "help": "High-pass filter corner; 0 = off."}
        out[f"output_{o}_lpf_hz"] = {"type": "number", "label": f"Output {o} LPF (Hz)", "min": 0.0, "max": 20000.0, "help": "Low-pass filter corner; 0 = off."}
        out[f"output_{o}_speaker_preset"] = {"type": "string", "label": f"Output {o} Speaker Preset"}
        out[f"paging_output_{o}_fader_db"] = _db_var(f"Paging Output {o} Gain (dB)", "Paging output fader gain. -80 dB = -inf.")
    for i in range(1, NUM_INPUTS + 1):
        for o in range(1, NUM_OUTPUTS + 1):
            out[f"xpt_i{i}_o{o}_db"] = _db_var(
                f"Crosspoint In {i} -> Out {o} (dB)", "Crosspoint gain. -80 dB = -inf.",
            )
    for i in range(1, NUM_INPUTS + 1):
        out[f"anc_{i}_reference_db"] = {"type": "number", "label": f"ANC {i} Reference (dB)", "min": -50.0, "max": 41.0, "help": "ANC reference level read from the device."}
    return out


# ── Command catalog ─────────────────────────────────────────────────────────

def _channel_param(count: int, label: str = "Channel") -> dict[str, Any]:
    return {"type": "integer", "required": True, "label": label, "min": 1, "max": count}


def _onoff_param(label: str) -> dict[str, Any]:
    return {"type": "enum", "required": True, "label": label, "values": ["on", "off"], "default": "on"}


def _gain_db_param() -> dict[str, Any]:
    return {"type": "number", "required": True, "label": "Gain (dB)", "min": MUTE_DB, "max": 10.0,
            "help": "Snapped to the nearest step. -80 dB or below = -inf (silent)."}


def _tone_db_param() -> dict[str, Any]:
    return {"type": "number", "required": True, "label": "Gain (dB)", "min": -12.0, "max": 12.0}


def _build_commands() -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}

    cmds["set_input_gain"] = {
        "label": "Set Input Gain",
        "params": {"channel": _channel_param(NUM_INPUTS), "db": _gain_db_param()},
        "help": "Set an input channel fader to an absolute gain.",
    }
    cmds["set_output_gain"] = {
        "label": "Set Output Gain",
        "params": {"channel": _channel_param(NUM_OUTPUTS), "db": _gain_db_param()},
        "help": "Set an output channel fader to an absolute gain.",
    }
    cmds["set_paging_output_gain"] = {
        "label": "Set Paging Output Gain",
        "params": {"channel": _channel_param(NUM_OUTPUTS), "db": _gain_db_param()},
        "help": "Set a paging output channel fader to an absolute gain.",
    }
    cmds["step_input_gain"] = {
        "label": "Nudge Input Gain",
        "params": {"channel": _channel_param(NUM_INPUTS),
                   "step_db": {"type": "number", "required": True, "label": "Step (dB)", "min": -15.5, "max": 15.5,
                               "help": "Relative change in 0.5 dB steps. The device replies with the resulting level."}},
        "help": "Nudge an input fader up or down. The resulting absolute level is read back.",
    }
    cmds["step_output_gain"] = {
        "label": "Nudge Output Gain",
        "params": {"channel": _channel_param(NUM_OUTPUTS),
                   "step_db": {"type": "number", "required": True, "label": "Step (dB)", "min": -15.5, "max": 15.5,
                               "help": "Relative change in 0.5 dB steps. The device replies with the resulting level."}},
        "help": "Nudge an output fader up or down. The resulting absolute level is read back.",
    }
    cmds["set_crosspoint"] = {
        "label": "Set Crosspoint Gain",
        "params": {"input": _channel_param(NUM_INPUTS, "Input"),
                   "output": _channel_param(NUM_OUTPUTS, "Output"),
                   "db": _gain_db_param()},
        "help": "Set the matrix crosspoint gain from an input to an output.",
    }
    cmds["step_crosspoint"] = {
        "label": "Nudge Crosspoint Gain",
        "params": {"input": _channel_param(NUM_INPUTS, "Input"),
                   "output": _channel_param(NUM_OUTPUTS, "Output"),
                   "step_db": {"type": "integer", "required": True, "label": "Step (dB)", "min": -16, "max": 16,
                               "help": "Relative change in 1 dB steps (echoes the step, not the result)."}},
        "help": "Nudge a crosspoint gain up or down in 1 dB steps.",
    }
    cmds["set_input_channel"] = {
        "label": "Set Input On/Off",
        "params": {"channel": _channel_param(NUM_INPUTS), "state": _onoff_param("State")},
    }
    cmds["set_output_channel"] = {
        "label": "Set Output On/Off",
        "params": {"channel": _channel_param(NUM_OUTPUTS), "state": _onoff_param("State")},
    }
    cmds["power"] = {
        "label": "Power On/Off",
        "params": {"state": _onoff_param("Power")},
    }
    cmds["set_input_tone"] = {
        "label": "Set Input Tone",
        "params": {"channel": _channel_param(NUM_INPUTS),
                   "band": {"type": "enum", "required": True, "label": "Band", "values": ["bass", "treble"], "default": "bass"},
                   "db": _tone_db_param()},
        "help": "Set input bass or treble gain (-12 to +12 dB).",
    }
    cmds["set_output_tone"] = {
        "label": "Set Output Tone",
        "params": {"channel": _channel_param(NUM_OUTPUTS),
                   "band": {"type": "enum", "required": True, "label": "Band", "values": ["bass", "treble"], "default": "bass"},
                   "db": _tone_db_param()},
        "help": "Set output bass or treble gain (-12 to +12 dB).",
    }
    eq_params = {
        "state": _onoff_param("EQ"),
        "band": {"type": "integer", "required": False, "label": "Band", "min": 1, "max": 10, "default": 1},
        "gain_db": {"type": "number", "required": False, "label": "Gain (dB)", "min": -12.0, "max": 12.0, "default": 0.0},
        "q": {"type": "enum", "required": False, "label": "Q", "values": EQ_Q_VALUES, "default": "1"},
        "freq_hz": {"type": "enum", "required": False, "label": "Frequency (Hz)", "values": EQ_FREQ_VALUES, "default": "1000"},
    }
    cmds["set_input_eq"] = {
        "label": "Set Input EQ",
        "params": {"channel": _channel_param(NUM_INPUTS), **eq_params},
        "help": "Turn a parametric EQ band on/off and set its band number, gain, Q, and center frequency.",
    }
    cmds["set_output_eq"] = {
        "label": "Set Output EQ",
        "params": {"channel": _channel_param(NUM_OUTPUTS), **eq_params},
        "help": "Turn a parametric EQ band on/off and set its band number, gain, Q, and center frequency.",
    }
    cmds["set_input_loudness"] = {
        "label": "Set Input Loudness",
        "params": {"channel": _channel_param(NUM_INPUTS), "state": _onoff_param("Loudness")},
    }
    cmds["set_output_loudness"] = {
        "label": "Set Output Loudness",
        "params": {"channel": _channel_param(NUM_OUTPUTS), "state": _onoff_param("Loudness")},
    }
    cmds["set_input_filter"] = {
        "label": "Set Input Filter",
        "params": {"channel": _channel_param(NUM_INPUTS),
                   "filter": {"type": "enum", "required": True, "label": "Filter", "values": ["hpf", "lpf"], "default": "hpf"},
                   "freq_hz": {"type": "enum", "required": True, "label": "Frequency (Hz)", "values": FILTER_HPF_VALUES, "default": "OFF"}},
        "help": "Set the high-pass or low-pass filter corner frequency (OFF disables it).",
    }
    cmds["set_output_filter"] = {
        "label": "Set Output Filter",
        "params": {"channel": _channel_param(NUM_OUTPUTS),
                   "filter": {"type": "enum", "required": True, "label": "Filter", "values": ["hpf", "lpf"], "default": "hpf"},
                   "freq_hz": {"type": "enum", "required": True, "label": "Frequency (Hz)", "values": FILTER_HPF_VALUES, "default": "OFF"}},
        "help": "Set the high-pass or low-pass filter corner frequency (OFF disables it).",
    }
    cmds["set_input_sensitivity"] = {
        "label": "Set Input Sensitivity",
        "params": {"channel": _channel_param(NUM_INPUTS),
                   "sensitivity_db": {"type": "enum", "required": True, "label": "Sensitivity (dB)", "values": SENSITIVITY_VALUES, "default": "-24"}},
        "help": "Set input sensitivity (only effective on channels using a D-001T / AN-001T input module).",
    }
    cmds["set_input_phantom"] = {
        "label": "Set Input Phantom Power",
        "params": {"channel": _channel_param(NUM_INPUTS), "state": _onoff_param("Phantom (+48V)")},
    }
    cmds["recall_preset"] = {
        "label": "Recall Preset",
        "params": {"preset": {"type": "integer", "required": True, "label": "Preset", "min": 1, "max": 32,
                              "help": "Preset memory 1-32 (a scene in mixer mode, an event in matrix mode)."}},
    }
    cmds["paging_event"] = {
        "label": "Paging Event",
        "params": {"event": {"type": "integer", "required": True, "label": "Event", "min": 1, "max": 32},
                   "control": {"type": "enum", "required": True, "label": "Control", "values": ["start", "stop"], "default": "start"}},
        "help": "Start or stop a paging event.",
    }
    cmds["set_speaker_preset"] = {
        "label": "Set Speaker Preset",
        "params": {"channel": _channel_param(NUM_OUTPUTS, "Output"),
                   "preset": {"type": "enum", "required": True, "label": "Speaker Preset", "values": SPEAKER_PRESET_VALUES, "default": "ALL FLAT"}},
        "help": "Recall a TOA speaker EQ preset on an output channel.",
    }
    cmds["set_anc_adjust"] = {
        "label": "Set ANC Reference Adjust",
        "params": {"channel": _channel_param(NUM_INPUTS, "AN Channel"),
                   "db": {"type": "number", "required": True, "label": "Adjust (dB)", "min": -10.0, "max": 10.0}},
        "help": "Adjust the ambient-noise-compensation reference level for an AN channel.",
    }
    cmds["get_anc_reference"] = {
        "label": "Read ANC Reference",
        "params": {"channel": _channel_param(NUM_INPUTS, "AN Channel")},
        "help": "Read the current ANC reference level for an AN channel into anc_N_reference_db.",
    }
    cmds["request_channel_name"] = {
        "label": "Read Channel Name",
        "params": {"kind": {"type": "enum", "required": True, "label": "Channel Type", "values": ["input", "output"], "default": "input"},
                   "channel": _channel_param(NUM_INPUTS)},
        "help": "Read a channel's name from the device into input_N_name / output_N_name.",
    }
    return cmds


def _as_state(value: str) -> bool:
    return str(value).lower() in ("on", "1", "true", "yes")


class TOA9000M2Driver(BaseDriver):
    """TOA 9000M2 Series matrix mixer / mixer-amplifier (RS-232C)."""

    DRIVER_INFO = {
        "id": "toa_9000m2",
        "name": "TOA 9000M2 Matrix Mixer",
        "manufacturer": "TOA",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls the TOA M-9000M2 Digital Matrix Mixer and protocol-"
            "compatible A-9000M2 Series mixer/amplifiers over RS-232C. 8 "
            "mixing inputs x 8 outputs: input/output fader gain, the 8x8 "
            "crosspoint matrix, channel on/off, tone, parametric EQ, "
            "loudness, HPF/LPF, input sensitivity, phantom power, preset / "
            "paging-event recall, speaker presets, and ANC. The device is "
            "write-then-echo with no live readback except channel names and "
            "ANC reference, so state is tracked optimistically from the "
            "confirmations it returns."
        ),
        "source_url": "https://www.toaelectronics.com/toa-assets/9000M2_PROTOCOL_MANUAL_EN.pdf",
        "tags": ["dsp", "matrix-mixer", "mixer-amplifier", "paging", "install-audio", "serial", "rs232"],
        "verified": False,
        "simulated": True,
        "protocols": ["toa_9000m2"],
        "transport": "serial",
        "compatible_models": [
            {
                "manufacturer": "TOA",
                "models": ["M-9000M2"],
                "confidence": "untested",
                "notes": "Digital Matrix Mixer. 8 mic/line inputs x 8 outputs. RS-232C, D-sub 9-pin, 9600-57600 8N1, straight cable.",
            },
            {
                "manufacturer": "TOA",
                "models": ["A-9060DIL", "A-9120DIL", "A-9240DIL", "A-9060DHM2", "A-9120DHM2", "A-9240DHM2"],
                "confidence": "untested",
                "notes": "9000M2 Series mixer/amplifiers share this RS-232C protocol. Power, phantom, and speaker-preset commands are most relevant to these amp models.",
            },
        ],
        "help": {
            "overview": (
                "The TOA 9000M2 Series speaks a compact binary RS-232C protocol: every message is "
                "<command byte> <length> <data...>. This driver exposes the full control surface for the "
                "8x8 matrix mixer -- faders, the crosspoint matrix, mutes, tone, parametric EQ, filters, "
                "loudness, input sensitivity, phantom power, presets, paging events, speaker presets, and "
                "ANC.\n\n"
                "IMPORTANT -- feedback is limited. The hardware confirms each command by echoing it back, "
                "so the driver knows a change was accepted and mirrors it in state. But the protocol has NO "
                "way to read most current values back; the only true reads are channel names and the ANC "
                "reference level. As a result, changes made at the unit's front panel or by a scene recall "
                "will not appear in OpenAVC. Drive the mixer entirely from OpenAVC (or re-send known values "
                "on connect) for state to stay accurate. Fader 'nudge' commands are the exception -- the "
                "device returns the resulting level, which is read back exactly."
            ),
            "setup": (
                "1. Wire RS-232C to the unit's D-sub 9-pin port with a STRAIGHT cable "
                "(pin 2 = TX, pin 3 = RX, pin 5 = GND).\n"
                "2. Set the serial port path in the device config (e.g. COM3 on Windows, "
                "/dev/ttyUSB0 on Linux) and pick the baud rate that matches the unit "
                "(9600 / 19200 / 38400 / 57600; 8 data bits, no parity, 1 stop bit).\n"
                "3. To route the mixer over the network, connect it to an IP-to-serial "
                "bridge (e.g. Global Cache iTach) and bind this device to the bridge's "
                "serial port -- OpenAVC carries the bytes through transparently.\n\n"
                "Commands cannot be received while the unit is powered off (send Power On first)."
            ),
        },
        "default_config": {
            "port": "",
            "baudrate": 9600,
            "poll_interval": 30,
            "inter_command_delay": 0.05,
        },
        "config_schema": {
            "port": {"type": "string", "required": True, "label": "Serial Port",
                     "description": "Serial port path, e.g. COM3 (Windows) or /dev/ttyUSB0 (Linux)."},
            "baudrate": {"type": "enum", "default": 9600, "label": "Baud Rate", "values": [9600, 19200, 38400, 57600],
                         "description": "Must match the unit's RS-232C setting."},
            "poll_interval": {"type": "integer", "default": 30, "min": 0, "label": "Poll Interval (sec)",
                              "description": "Liveness check + channel-name refresh. 0 disables polling."},
            "inter_command_delay": {"type": "number", "default": 0.05, "min": 0.0, "label": "Inter-Command Delay (sec)"},
        },
        # populated per-instance in __init__ by _build_commands()
        "commands": {},
        "state_variables": {},
    }

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        self.DRIVER_INFO = {
            **type(self).DRIVER_INFO,
            "commands": _build_commands(),
            "state_variables": _build_state_vars(),
        }
        super().__init__(device_id, config, state, events)
        self._poll_index = 0
        # The ANC reference response (C2) carries only the level, not the
        # channel. Serial responses are ordered, so we queue each requested
        # channel (0-based) here and pair it with the next C2 that arrives.
        self._anc_pending: deque[int] = deque()

    # --- Framing ---

    def _create_frame_parser(self) -> FrameParser | None:
        return CallableFrameParser(parse_frame)

    # --- Sending ---

    async def _send(self, frame: bytes) -> None:
        if not self.transport or not getattr(self.transport, "connected", False):
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(frame)

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        handler = _COMMAND_HANDLERS.get(command)
        if handler is None:
            raise ValueError(f"Unknown command: {command}")
        return await handler(self, params)

    # --- Param helpers ---

    @staticmethod
    def _chan0(params: dict[str, Any], key: str, count: int) -> int:
        """Return a validated 0-based channel index from a 1-based param."""
        try:
            ch = int(params[key])
        except (KeyError, TypeError, ValueError):
            raise CommandParamError(f"'{key}' must be an integer 1-{count}")
        if not 1 <= ch <= count:
            raise CommandParamError(f"'{key}' must be between 1 and {count}")
        return ch - 1

    @staticmethod
    def _enum_index(params: dict[str, Any], key: str, values: list[str]) -> int:
        raw = str(params.get(key, ""))
        if raw not in values:
            raise CommandParamError(f"'{key}' must be one of: {', '.join(values)}")
        return values.index(raw)

    # --- Command handlers ---

    async def _cmd_fader(self, params, attr, count, command):
        ch = self._chan0(params, "channel", count)
        pos = fader_db_to_pos(float(params["db"]))
        await self._send(build_frame(command, attr, ch, pos))
        return True

    async def _cmd_set_input_gain(self, params):
        return await self._cmd_fader(params, ATTR_INPUT, NUM_INPUTS, CMD_FADER_POSITION)

    async def _cmd_set_output_gain(self, params):
        return await self._cmd_fader(params, ATTR_OUTPUT, NUM_OUTPUTS, CMD_FADER_POSITION)

    async def _cmd_set_paging_output_gain(self, params):
        return await self._cmd_fader(params, ATTR_OUTPUT, NUM_OUTPUTS, CMD_PAGING_FADER_POSITION)

    async def _cmd_step_fader(self, params, attr, count):
        ch = self._chan0(params, "channel", count)
        byte = fader_step_to_byte(float(params["step_db"]))
        await self._send(build_frame(CMD_FADER_STEP, attr, ch, byte))
        return True

    async def _cmd_step_input_gain(self, params):
        return await self._cmd_step_fader(params, ATTR_INPUT, NUM_INPUTS)

    async def _cmd_step_output_gain(self, params):
        return await self._cmd_step_fader(params, ATTR_OUTPUT, NUM_OUTPUTS)

    async def _cmd_set_crosspoint(self, params):
        src = self._chan0(params, "input", NUM_INPUTS)
        dst = self._chan0(params, "output", NUM_OUTPUTS)
        pos = xpt_db_to_pos(float(params["db"]))
        await self._send(build_frame(CMD_CROSSPOINT, ATTR_INPUT, src, ATTR_OUTPUT, dst, pos))
        return True

    async def _cmd_step_crosspoint(self, params):
        src = self._chan0(params, "input", NUM_INPUTS)
        dst = self._chan0(params, "output", NUM_OUTPUTS)
        byte = xpt_step_to_byte(int(params["step_db"]))
        await self._send(build_frame(CMD_CROSSPOINT, ATTR_INPUT, src, ATTR_OUTPUT, dst, byte))
        return True

    async def _cmd_channel_onoff(self, params, attr, count):
        ch = self._chan0(params, "channel", count)
        on = 1 if _as_state(params.get("state", "on")) else 0
        await self._send(build_frame(CMD_CHANNEL_ONOFF, attr, ch, on))
        return True

    async def _cmd_set_input_channel(self, params):
        return await self._cmd_channel_onoff(params, ATTR_INPUT, NUM_INPUTS)

    async def _cmd_set_output_channel(self, params):
        return await self._cmd_channel_onoff(params, ATTR_OUTPUT, NUM_OUTPUTS)

    async def _cmd_power(self, params):
        on = 1 if _as_state(params.get("state", "on")) else 0
        await self._send(build_frame(CMD_POWER, on))
        return True

    async def _cmd_tone(self, params, attr, count):
        ch = self._chan0(params, "channel", count)
        band = 0x01 if str(params.get("band", "bass")).lower() == "treble" else 0x00
        val = gain_db_to_byte(float(params["db"]))
        await self._send(build_frame(CMD_TONE, attr, ch, band, val))
        return True

    async def _cmd_set_input_tone(self, params):
        return await self._cmd_tone(params, ATTR_INPUT, NUM_INPUTS)

    async def _cmd_set_output_tone(self, params):
        return await self._cmd_tone(params, ATTR_OUTPUT, NUM_OUTPUTS)

    async def _cmd_eq(self, params, attr, count):
        ch = self._chan0(params, "channel", count)
        on = 1 if _as_state(params.get("state", "on")) else 0
        band = int(params.get("band", 1) or 1)
        if not 1 <= band <= 10:
            raise CommandParamError("'band' must be between 1 and 10")
        gain = gain_db_to_byte(float(params.get("gain_db", 0.0) or 0.0))
        q = self._enum_index(params, "q", EQ_Q_VALUES) if params.get("q") is not None else EQ_Q_VALUES.index("1")
        freq = self._enum_index(params, "freq_hz", EQ_FREQ_VALUES) if params.get("freq_hz") is not None else EQ_FREQ_VALUES.index("1000")
        await self._send(build_frame(CMD_EQ, attr, ch, on, band - 1, gain, q, freq))
        return True

    async def _cmd_set_input_eq(self, params):
        return await self._cmd_eq(params, ATTR_INPUT, NUM_INPUTS)

    async def _cmd_set_output_eq(self, params):
        return await self._cmd_eq(params, ATTR_OUTPUT, NUM_OUTPUTS)

    async def _cmd_loudness(self, params, attr, count):
        ch = self._chan0(params, "channel", count)
        on = 1 if _as_state(params.get("state", "on")) else 0
        await self._send(build_frame(CMD_LOUDNESS, attr, ch, on))
        return True

    async def _cmd_set_input_loudness(self, params):
        return await self._cmd_loudness(params, ATTR_INPUT, NUM_INPUTS)

    async def _cmd_set_output_loudness(self, params):
        return await self._cmd_loudness(params, ATTR_OUTPUT, NUM_OUTPUTS)

    async def _cmd_filter(self, params, attr, count):
        ch = self._chan0(params, "channel", count)
        is_lpf = str(params.get("filter", "hpf")).lower() == "lpf"
        values = FILTER_LPF_VALUES if is_lpf else FILTER_HPF_VALUES
        freq = self._enum_index(params, "freq_hz", values)
        await self._send(build_frame(CMD_FILTER, attr, ch, 0x01 if is_lpf else 0x00, freq))
        return True

    async def _cmd_set_input_filter(self, params):
        return await self._cmd_filter(params, ATTR_INPUT, NUM_INPUTS)

    async def _cmd_set_output_filter(self, params):
        return await self._cmd_filter(params, ATTR_OUTPUT, NUM_OUTPUTS)

    async def _cmd_set_input_sensitivity(self, params):
        ch = self._chan0(params, "channel", NUM_INPUTS)
        val = self._enum_index(params, "sensitivity_db", SENSITIVITY_VALUES)
        await self._send(build_frame(CMD_INPUT_SENSITIVITY, ch, val))
        return True

    async def _cmd_set_input_phantom(self, params):
        ch = self._chan0(params, "channel", NUM_INPUTS)
        on = 1 if _as_state(params.get("state", "on")) else 0
        await self._send(build_frame(CMD_PHANTOM, ch, on))
        return True

    async def _cmd_recall_preset(self, params):
        try:
            preset = int(params["preset"])
        except (KeyError, TypeError, ValueError):
            raise CommandParamError("'preset' must be an integer 1-32")
        if not 1 <= preset <= 32:
            raise CommandParamError("'preset' must be between 1 and 32")
        await self._send(build_frame(CMD_PRESET_RECALL, 0x00, preset - 1))
        return True

    async def _cmd_paging_event(self, params):
        try:
            event = int(params["event"])
        except (KeyError, TypeError, ValueError):
            raise CommandParamError("'event' must be an integer 1-32")
        if not 1 <= event <= 32:
            raise CommandParamError("'event' must be between 1 and 32")
        control = 1 if str(params.get("control", "start")).lower() == "start" else 0
        await self._send(build_frame(CMD_PAGING_EVENT, event - 1, control))
        return True

    async def _cmd_set_speaker_preset(self, params):
        ch = self._chan0(params, "channel", NUM_OUTPUTS)
        preset = self._enum_index(params, "preset", SPEAKER_PRESET_VALUES)
        await self._send(build_frame(CMD_SPEAKER_PRESET, ch, preset))
        return True

    async def _cmd_set_anc_adjust(self, params):
        ch = self._chan0(params, "channel", NUM_INPUTS)
        val = anc_db_to_byte(float(params["db"]))
        await self._send(build_frame(CMD_ANC_ADJUST, ch, val))
        return True

    async def _cmd_get_anc_reference(self, params):
        ch = self._chan0(params, "channel", NUM_INPUTS)
        self._anc_pending.append(ch)
        await self._send(build_frame(CMD_ANC_REFERENCE, ch))
        return True

    async def _cmd_request_channel_name(self, params):
        count = NUM_OUTPUTS if str(params.get("kind", "input")).lower() == "output" else NUM_INPUTS
        attr = ATTR_OUTPUT if str(params.get("kind", "input")).lower() == "output" else ATTR_INPUT
        ch = self._chan0(params, "channel", count)
        await self._send(build_frame(CMD_NAME_REQUEST, NAME_REQUEST_SUBCODE, attr, ch))
        return True

    # --- Session setup + polling ---

    async def _post_connect(self) -> None:
        """Read every channel name (and ANC reference) so the UI populates."""
        for attr in (ATTR_INPUT, ATTR_OUTPUT):
            for ch in range(NUM_INPUTS):
                try:
                    await self._send(build_frame(CMD_NAME_REQUEST, NAME_REQUEST_SUBCODE, attr, ch))
                except ConnectionError:
                    return
        for ch in range(NUM_INPUTS):
            try:
                self._anc_pending.append(ch)
                await self._send(build_frame(CMD_ANC_REFERENCE, ch))
            except ConnectionError:
                return

    async def poll(self) -> None:
        """Liveness probe + name refresh.

        Rotates a channel-name request across the 16 name slots and waits for
        the reply. The device answers every name request, so a timeout means
        it's gone -- that propagates to the missed-poll watchdog. The reply is
        also parsed into state by on_data_received.
        """
        if not self.transport or not getattr(self.transport, "connected", False):
            raise ConnectionError(f"[{self.device_id}] Not connected")
        slot = self._poll_index % (NUM_INPUTS + NUM_OUTPUTS)
        self._poll_index += 1
        attr = ATTR_INPUT if slot < NUM_INPUTS else ATTR_OUTPUT
        ch = slot % NUM_INPUTS
        frame = build_frame(CMD_NAME_REQUEST, NAME_REQUEST_SUBCODE, attr, ch)
        timeout = float(self.config.get("timeout", 5.0))
        await self.transport.send_and_wait(frame, timeout=timeout)

    # --- Response parsing ---

    async def on_data_received(self, data: bytes) -> None:
        """Parse one complete frame and mirror it into state.

        Handles both command echoes (write confirmations we track
        optimistically) and the readable responses (name, ANC).
        """
        if len(data) < 2:
            return
        command = data[0]
        length = data[1]
        body = data[2:2 + length]
        try:
            self._apply_frame(command, body)
        except Exception:
            log.debug("[%s] Failed to parse frame %s", self.device_id, data.hex(), exc_info=True)

    def _apply_frame(self, command: int, body: bytes) -> None:
        if command == CMD_FADER_POSITION and len(body) == 3:
            attr, ch, val = body
            prefix = "input" if attr == ATTR_INPUT else "output"
            self._set_channel(prefix, ch, "fader_db", fader_pos_to_db(val))
        elif command == CMD_PAGING_FADER_POSITION and len(body) == 3:
            _attr, ch, val = body
            self._set_channel("paging_output", ch, "fader_db", fader_pos_to_db(val))
        elif command == CMD_FADER_STEP and len(body) == 3:
            # The step command echoes the RESULTING absolute position.
            attr, ch, val = body
            prefix = "input" if attr == ATTR_INPUT else "output"
            self._set_channel(prefix, ch, "fader_db", fader_pos_to_db(val))
        elif command == CMD_CROSSPOINT and len(body) == 5:
            _sa, src, _da, dst, val = body
            if val <= 0x51:
                self._set_xpt(src, dst, xpt_pos_to_db(val))
            else:
                self._apply_xpt_step(src, dst, val)
        elif command == CMD_CHANNEL_ONOFF and len(body) == 3:
            attr, ch, on = body
            prefix = "input" if attr == ATTR_INPUT else "output"
            self._set_channel(prefix, ch, "on", bool(on))
        elif command == CMD_POWER and len(body) == 1:
            self.set_state("power", bool(body[0]))
        elif command == CMD_TONE and len(body) == 4:
            attr, ch, band, val = body
            if val <= 0x18:
                prefix = "input" if attr == ATTR_INPUT else "output"
                prop = "treble_db" if band == 0x01 else "bass_db"
                self._set_channel(prefix, ch, prop, gain_byte_to_db(val))
        elif command == CMD_EQ and len(body) == 7:
            attr, ch, on = body[0], body[1], body[2]
            prefix = "input" if attr == ATTR_INPUT else "output"
            self._set_channel(prefix, ch, "eq_on", bool(on))
        elif command == CMD_LOUDNESS and len(body) == 3:
            attr, ch, on = body
            prefix = "input" if attr == ATTR_INPUT else "output"
            self._set_channel(prefix, ch, "loudness", bool(on))
        elif command == CMD_FILTER and len(body) == 4:
            attr, ch, kind, freq = body
            prefix = "input" if attr == ATTR_INPUT else "output"
            values = FILTER_LPF_VALUES if kind == 0x01 else FILTER_HPF_VALUES
            hz = self._freq_value_to_hz(values[freq]) if freq < len(values) else 0.0
            self._set_channel(prefix, ch, "lpf_hz" if kind == 0x01 else "hpf_hz", hz)
        elif command == CMD_INPUT_SENSITIVITY and len(body) == 2:
            ch, val = body
            if val < len(SENSITIVITY_VALUES):
                self._set_channel("input", ch, "sensitivity_db", float(SENSITIVITY_VALUES[val]))
        elif command == CMD_PHANTOM and len(body) == 2:
            ch, on = body
            self._set_channel("input", ch, "phantom", bool(on))
        elif command == CMD_PRESET_RECALL and len(body) == 2 and body[0] == 0x00:
            self.set_state("last_preset", body[1] + 1)
        elif command == CMD_SPEAKER_PRESET and len(body) == 2:
            ch, val = body
            if val < len(SPEAKER_PRESET_VALUES):
                self._set_channel("output", ch, "speaker_preset", SPEAKER_PRESET_VALUES[val])
        elif command == RESP_NAME and len(body) >= 2:
            attr, ch = body[0], body[1]
            name = bytes(body[2:]).split(b"\x00", 1)[0].decode(_NAME_ENCODING, errors="replace")
            prefix = "input" if attr == ATTR_INPUT else "output"
            self._set_channel(prefix, ch, "name", name)
        elif command == RESP_ANC_REFERENCE and len(body) == 1:
            # C2 carries only the reference level; pair it with the channel of
            # the oldest outstanding request (serial replies are in order).
            if self._anc_pending:
                ch = self._anc_pending.popleft()
                self.set_state(f"anc_{ch + 1}_reference_db", anc_ref_byte_to_db(body[0]))

    # --- State helpers ---

    def _set_channel(self, prefix: str, ch0: int, prop: str, value: Any) -> None:
        if 0 <= ch0 < max(NUM_INPUTS, NUM_OUTPUTS):
            self.set_state(f"{prefix}_{ch0 + 1}_{prop}", value)

    def _set_xpt(self, src0: int, dst0: int, db: float) -> None:
        if 0 <= src0 < NUM_INPUTS and 0 <= dst0 < NUM_OUTPUTS:
            self.set_state(f"xpt_i{src0 + 1}_o{dst0 + 1}_db", db)

    def _apply_xpt_step(self, src0: int, dst0: int, byte: int) -> None:
        """A crosspoint step echoes the step byte (not the result), so apply it
        to the tracked value optimistically."""
        if not (0 <= src0 < NUM_INPUTS and 0 <= dst0 < NUM_OUTPUTS):
            return
        key = f"xpt_i{src0 + 1}_o{dst0 + 1}_db"
        current = self.get_state(key)
        if not isinstance(current, (int, float)):
            return
        if 0x70 <= byte <= 0x7F:
            delta = byte - 0x70 + 1
        elif 0x60 <= byte <= 0x6F:
            delta = -(byte - 0x60 + 1)
        else:
            return
        new_db = max(-70.0, min(10.0, current + delta))
        self.set_state(key, new_db)

    @staticmethod
    def _freq_value_to_hz(value: str) -> float:
        return 0.0 if value == "OFF" else float(value)


# Command dispatch table: command id -> unbound handler coroutine.
_COMMAND_HANDLERS = {
    "set_input_gain": TOA9000M2Driver._cmd_set_input_gain,
    "set_output_gain": TOA9000M2Driver._cmd_set_output_gain,
    "set_paging_output_gain": TOA9000M2Driver._cmd_set_paging_output_gain,
    "step_input_gain": TOA9000M2Driver._cmd_step_input_gain,
    "step_output_gain": TOA9000M2Driver._cmd_step_output_gain,
    "set_crosspoint": TOA9000M2Driver._cmd_set_crosspoint,
    "step_crosspoint": TOA9000M2Driver._cmd_step_crosspoint,
    "set_input_channel": TOA9000M2Driver._cmd_set_input_channel,
    "set_output_channel": TOA9000M2Driver._cmd_set_output_channel,
    "power": TOA9000M2Driver._cmd_power,
    "set_input_tone": TOA9000M2Driver._cmd_set_input_tone,
    "set_output_tone": TOA9000M2Driver._cmd_set_output_tone,
    "set_input_eq": TOA9000M2Driver._cmd_set_input_eq,
    "set_output_eq": TOA9000M2Driver._cmd_set_output_eq,
    "set_input_loudness": TOA9000M2Driver._cmd_set_input_loudness,
    "set_output_loudness": TOA9000M2Driver._cmd_set_output_loudness,
    "set_input_filter": TOA9000M2Driver._cmd_set_input_filter,
    "set_output_filter": TOA9000M2Driver._cmd_set_output_filter,
    "set_input_sensitivity": TOA9000M2Driver._cmd_set_input_sensitivity,
    "set_input_phantom": TOA9000M2Driver._cmd_set_input_phantom,
    "recall_preset": TOA9000M2Driver._cmd_recall_preset,
    "paging_event": TOA9000M2Driver._cmd_paging_event,
    "set_speaker_preset": TOA9000M2Driver._cmd_set_speaker_preset,
    "set_anc_adjust": TOA9000M2Driver._cmd_set_anc_adjust,
    "get_anc_reference": TOA9000M2Driver._cmd_get_anc_reference,
    "request_channel_name": TOA9000M2Driver._cmd_request_channel_name,
}
