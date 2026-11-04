"""
Roland V-160HD / V-80HD / VR-120HD / VR-6HD / V-1-4K simulator.

The switcher's side of the LAN/RS-232 Basic Control Commands (Roland, eng02):
``CODE:parameter,parameter;`` commands, a query answered with its value and
``ACK;``, a set with ``ACK;``, and ``ERR:0;`` (a command the model does not
take, or the wrong number of parameters) or ``ERR:5;`` (a value out of range).
Replies start with STX and end with a line feed, as the model guides show; a
leading STX on a command (the RS-232 form) is accepted.

From the device's config:
  * ``model``: the model it answers VER; as, and whose inputs, outputs, buses
    and lists it has (V-160HD unless set). It can be changed in the Simulator
    UI; the driver follows at its next connect or Refresh from Device.
  * ``password``: with one set, a connection must send it (and a line feed)
    before anything else; until then the switcher answers nothing. What a real
    unit says to the network password, right or wrong, is not documented, so
    the simulator says nothing to it either.

Program and preset start on INPUT1 and INPUT2; HDMI1 and HDMI2 have a signal.
A transition (cut or auto) swaps program and preset at once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

LOCAL = "_local"


def _seq(prefix: str, last: int, first: int = 1) -> list[str]:
    return [f"{prefix}{n}" for n in range(first, last + 1)]


def _pads(banks: str, per_bank: int) -> list[str]:
    return [f"{bank}-{n}" for bank in banks for n in range(1, per_bank + 1)]


_ALL_PATTERNS = [
    "OFF", "COLORBAR75", "COLORBAR100", "RAMP", "STEP", "HATCH", "DIAMOND", "CIRCLE",
    "COLORBAR75-SP", "COLORBAR100-SP", "RAMP-SP", "STEP-SP", "HATCH-SP",
]

# From the Basic Control Commands' parameter tables (the driver keeps its own
# copy, written from the same tables).
MODELS: dict[str, dict[str, Any]] = {
    "V-160HD": {
        "version": "3.50", "hdmi": 8, "sdi": 8, "stills": 16, "players": False, "channels": 20,
        "assign": _seq("HDMI", 8) + _seq("SDI", 8) + _seq("STILL", 16) + ["N/A"],
        "video_outputs": _seq("HDMI", 3) + _seq("SDI", 3) + ["USB"],
        "output_assign": ["PGM", "SUB", "PVW"] + _seq("AUX", 3) + _seq("DSK", 2)
        + ["MULTIVIEW", "INPUTVIEW1", "STILLVIEW"],
        "aux": 3, "pinp": 4, "pinp_source": True, "dsk": 2, "split": 2, "freeze": True,
        "still_output": True, "roi": 0,
        "audio_inputs": _seq("XLR", 2) + ["RCA1", "USB", "BLUETOOTH"] + _seq("HDMI", 8) + _seq("SDI", 8),
        "stereo_link": _seq("XLR", 2), "voice_changer": _seq("XLR", 2),
        "audio_outputs": ["MAIN"] + _seq("AUX", 3) + ["USB"],
        "audio_jacks": _seq("HDMI", 3) + _seq("SDI", 3) + ["XLR1", "RCA1", "USB", "PHONES"],
        "jack_assign": ["AUTO", "MAIN"] + _seq("AUX", 3),
        "reverb": True, "memories": 30, "gpo": 16, "cameras": 16, "patterns": _ALL_PATTERNS,
        "streaming": False, "pads": [], "macros": True, "fader_max": 2047, "pinp_time": True,
    },
    "V-80HD": {
        "version": "1.30", "hdmi": 4, "sdi": 4, "stills": 32, "players": True, "channels": 16,
        "assign": _seq("HDMI", 4) + _seq("SDI", 4) + _seq("STILL", 32)
        + ["V.PLAYER", "SRT", "STATUS1", "STATUS2", "TIME", "N/A"],
        "video_outputs": _seq("HDMI", 3) + _seq("SDI", 2) + ["USB", "STREAM", "LCD"],
        "output_assign": ["PGM", "SUB", "PVW"] + _seq("AUX", 2) + ["DSK1", "MULTIVIEW"]
        + _seq("INPUTVIEW", 2) + ["QUADVIEW"],
        "aux": 2, "pinp": 2, "pinp_source": True, "dsk": 1, "split": 2, "freeze": True,
        "still_output": True, "roi": 0,
        "audio_inputs": _seq("XLR", 2) + ["RCA1", "USB", "BLUETOOTH", "A.PLAYER"]
        + _seq("HDMI", 4) + _seq("SDI", 4) + ["V.PLAYER", "SRT"],
        "stereo_link": _seq("XLR", 2), "voice_changer": _seq("XLR", 2),
        "audio_outputs": ["MAIN"] + _seq("AUX", 2) + ["USB", "STREAM", "REC"],
        "audio_jacks": _seq("HDMI", 3) + _seq("SDI", 2) + ["XLR1", "USB", "STREAM", "REC", "PHONES"],
        "jack_assign": ["AUTO", "MAIN"] + _seq("AUX", 2) + ["MONITOR"],
        "reverb": True, "memories": 32, "gpo": 16, "cameras": 8, "patterns": _ALL_PATTERNS,
        "streaming": True, "pads": _pads("ABCDEFGH", 8), "macros": True, "fader_max": 2047,
        "pinp_time": True,
    },
    "VR-120HD": {
        "version": "2.20", "hdmi": 6, "sdi": 6, "stills": 16, "players": True, "channels": 8,
        "assign": _seq("HDMI", 6) + _seq("SDI", 6) + _seq("STILL", 16)
        + ["V.PLAYER", "SRT", "STATUS1", "STATUS2", "TIME", "N/A"],
        "video_outputs": _seq("HDMI", 3) + _seq("SDI", 3) + ["USB", "STREAM", "LCD"],
        "output_assign": ["PGM", "SUB", "PVW"] + _seq("AUX", 3) + _seq("DSK", 2)
        + ["MULTIVIEW", "INPUTVIEW1", "STILLVIEW"],
        "aux": 3, "pinp": 4, "pinp_source": True, "dsk": 2, "split": 2, "freeze": True,
        "still_output": True, "roi": 0,
        "audio_inputs": _seq("XLR", 6) + ["RCA1", "RCA2", "USB", "BLUETOOTH", "A.PLAYER"]
        + _seq("HDMI", 6) + _seq("SDI", 6) + ["V.PLAYER", "SRT"],
        "stereo_link": _seq("XLR", 6), "voice_changer": _seq("XLR", 2),
        "audio_outputs": ["MAIN"] + _seq("AUX", 3) + ["USB", "STREAM", "REC"],
        "audio_jacks": _seq("HDMI", 3) + _seq("SDI", 3)
        + ["XLR1", "XLR2", "RCA1", "USB", "STREAM", "REC", "PHONES"],
        "jack_assign": ["MAIN"] + _seq("AUX", 3) + ["MONITOR"],
        "reverb": True, "memories": 32, "gpo": 16, "cameras": 12, "patterns": _ALL_PATTERNS,
        "streaming": True, "pads": _pads("AB", 8), "macros": True, "fader_max": 2047,
        "pinp_time": True,
    },
    "VR-6HD": {
        "version": "2.20", "hdmi": 6, "sdi": 0, "stills": 16, "players": True, "channels": 6,
        "assign": _seq("HDMI", 6) + _seq("STILL", 16) + ["V.PLAYER", "SRT", "N/A"],
        "video_outputs": _seq("HDMI", 3) + ["USB", "STREAM", "LCD"],
        "output_assign": ["PGM", "SUB", "PVW", "AUX1", "DSK1", "MULTIVIEW", "STILLVIEW"],
        "aux": 1, "pinp": 2, "pinp_source": True, "dsk": 1, "split": 1, "freeze": True,
        "still_output": True, "roi": 0,
        "audio_inputs": _seq("XLR", 6) + ["RCA1", "USB", "BLUETOOTH", "A.PLAYER"]
        + _seq("HDMI", 6) + ["V.PLAYER", "SRT"],
        "stereo_link": _seq("XLR", 6), "voice_changer": _seq("XLR", 2),
        "audio_outputs": ["MAIN"] + _seq("AUX", 2) + ["USB", "STREAM", "REC"],
        "audio_jacks": _seq("HDMI", 3) + ["XLR1", "RCA1", "USB", "STREAM", "REC", "PHONES"],
        "jack_assign": ["MAIN"] + _seq("AUX", 2) + ["MONITOR"],
        "reverb": True, "memories": 32, "gpo": 16, "cameras": 6, "patterns": _ALL_PATTERNS,
        "streaming": True, "pads": _pads("AB", 6), "macros": True, "fader_max": 2047,
        "pinp_time": True,
    },
    "V-1-4K": {
        "version": "1.00", "hdmi": 5, "sdi": 0, "stills": 2, "players": False, "channels": 8,
        "assign": ["HDMI", "STILL1", "STILL2", "BLACK", "N/A"],
        "video_outputs": ["HDMI3", "HDMI4"],
        "output_assign": ["PGM", "PVW"] + _seq("INPUT", 8),
        "aux": 0, "pinp": 1, "pinp_source": False, "dsk": 1, "split": 1, "freeze": False,
        "still_output": False, "roi": 4,
        "audio_inputs": ["XLR1", "USB"] + _seq("HDMI", 5),
        "stereo_link": [], "voice_changer": [],
        "audio_outputs": ["MAIN"] + _seq("AUX", 2) + ["USB"],
        "audio_jacks": _seq("HDMI", 5) + ["XLR1", "USB", "PHONES"],
        "jack_assign": ["MAIN"] + _seq("AUX", 2) + ["MONITOR"],
        "reverb": False, "memories": 8, "gpo": 0, "cameras": 5, "patterns": ["OFF", "COLORBAR"],
        "streaming": False, "pads": [], "macros": False, "fader_max": 4095, "pinp_time": False,
    },
}

ONOFF = ("ON", "OFF")
ORDERS = ("NORMAL", "REVERSE", "RANDOM")
# A set that also toggles when its last parameter is left off.
TOGGLES = {
    "FRZ": 0, "FTB": 0, "RVB": 0, "ATM": 0, "ASW": 0,
    "PPS": 1, "PPW": 1, "DSK": 1, "DVW": 1, "SPS": 1, "IAM": 1, "IAS": 1, "STLK": 1, "VOCH": 1,
}
# Query code -> the key its value is kept under, where the two differ.
STORE_CODE = {
    "CAMPTS": "CPTS", "CAMAFC": "CAFC", "CAMAEP": "CAEP", "CAMPR": "CPR",
    "SEQSW": "SQS", "SEQAS": "SQA", "HDCP": "HCP",
}
NO_ACK = {"BSY", "SRTIST"}


class _Refused(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class _Session:
    def __init__(self, signed_in: bool):
        self.signed_in = signed_in


class RolandV160HDSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "roland_v160hd",
        "name": "Roland V-160HD Simulator",
        "category": "video",
        "transport": "tcp",
        "default_port": 8023,
        "delimiter": "\n",
        "initial_state": {"model": "V-160HD"},
        "controls": [
            {"type": "select", "key": "model", "label": "Model",
             "options": ["V-160HD", "V-80HD", "VR-120HD", "VR-6HD", "V-1-4K"]},
            {"type": "indicator", "key": "PGM", "label": "Program"},
            {"type": "indicator", "key": "PST", "label": "Preset"},
            {"type": "select", "key": "FTB", "label": "Fade to Black", "options": ["OFF", "ON"]},
            {"type": "select", "key": "VIST:HDMI1", "label": "HDMI 1 Signal",
             "options": ["DETECTED", "NOSIGNAL", "UNSUPPORTED"]},
            {"type": "select", "key": "VIST:HDMI2", "label": "HDMI 2 Signal",
             "options": ["DETECTED", "NOSIGNAL", "UNSUPPORTED"]},
            {"type": "select", "key": "IAM:XLR1", "label": "XLR 1 Mute", "options": ["OFF", "ON"]},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        self._seeding = False
        super().__init__(device_id, config)
        self._sessions: dict[str, _Session] = {}
        self._task_clients: dict[asyncio.Task, str] = {}
        model = str(self.config.get("model") or "V-160HD")
        self._seed(model if model in MODELS else "V-160HD")

    # ── Model ──

    @property
    def _m(self) -> dict[str, Any]:
        return MODELS.get(self.get_state("model"), MODELS["V-160HD"])

    def set_state(self, key: str, value: Any) -> None:
        old = self.get_state(key)
        super().set_state(key, value)
        if key == "model" and value != old and not self._seeding and value in MODELS:
            self._seed(value)

    def _seed(self, model: str) -> None:
        """Every value a model reports, written once for that model."""
        self._seeding = True
        try:
            for key in [k for k in self.state if k != "model"]:
                super().set_state(key, None)
            super().set_state("model", model)
            m = MODELS[model]
            values: dict[str, Any] = {
                "PGM": "INPUT1", "PST": "INPUT2", "VFL": "0", "ATG": "OFF", "FTB": "OFF",
                "TRS": "MIX", "MEM": "N/A", "ASW": "OFF", "ATM": "OFF", "HCP": "OFF",
                "TPT": "OFF", "TTN": "OFF,1k,1k", "BSY": "OFF",
            }
            if m["freeze"]:
                values["FRZ"] = "OFF"
            if m["reverb"]:
                values["RVB"] = "OFF"
            if m["still_output"]:
                values["STO"] = "OFF"
            if m["roi"]:
                values["ROISW"] = "OFF"
                for n in range(1, m["roi"] + 1):
                    values[f"ROIZM:ROI{n}"] = "1000"
                    values[f"ROIPT:ROI{n}"] = "0,0"
            if m["macros"]:
                values["SQS"] = "OFF"
                values["SQA"] = "OFF"
            physical = _seq("HDMI", m["hdmi"]) + _seq("SDI", m["sdi"])
            for n, wire in enumerate(physical, 1):
                values[f"VIST:{wire}"] = "DETECTED" if n <= 2 else "NOSIGNAL"
            assignable = [a for a in m["assign"] if a != "N/A"]
            for n in range(1, m["channels"] + 1):
                values[f"VIS:INPUT{n}"] = assignable[n - 1] if n <= len(assignable) else "N/A"
            defaults = ["PGM", "PVW", "MULTIVIEW"]
            for n, out in enumerate(m["video_outputs"]):
                wanted = defaults[n] if n < len(defaults) else "PGM"
                values[f"VOS:{out}"] = wanted if wanted in m["output_assign"] else m["output_assign"][0]
            for n in range(1, m["aux"] + 1):
                values[f"AUX:AUX{n}"] = "INPUT1"
            targets = ["MIX", "WIPE"] + (_seq("PinP", m["pinp"]) if m["pinp_time"] else []) + _seq("DSK", m["dsk"])
            for target in targets + ["OUTPUTFADE"]:
                values[f"TIM:{target}"] = "10"
            for n in range(1, m["pinp"] + 1):
                values[f"PPS:PinP{n}"] = "OFF"
                values[f"PPW:PinP{n}"] = "OFF"
                values[f"PIP:PinP{n}"] = "250,250"
                if m["pinp_source"]:
                    values[f"PIS:PinP{n}"] = "INPUT3"
            for n in range(1, m["dsk"] + 1):
                values[f"DSK:DSK{n}"] = "OFF"
                values[f"DVW:DSK{n}"] = "OFF"
                values[f"DSS:DSK{n}"] = "STILL1"
                values[f"KYL:DSK{n}"] = "0"
                values[f"KYG:DSK{n}"] = "0"
            for n in range(1, m["split"] + 1):
                values[f"SPS:SPLIT{n}"] = "OFF"
                values[f"SPT:SPLIT{n}"] = "-250,250,0"
            for wire in m["audio_inputs"]:
                values[f"IAL:{wire}"] = "0" if wire.startswith(("XLR", "HDMI1")) else "-INF"
                for code in ("IAM", "IAS", "HPF", "GATE"):
                    values[f"{code}:{wire}"] = "OFF"
                values[f"ADT:{wire}"] = "0"
            for wire in m["stereo_link"]:
                values[f"STLK:{wire}"] = "OFF"
            for wire in m["voice_changer"]:
                values[f"VOCH:{wire}"] = "OFF"
            for wire in m["audio_outputs"]:
                values[f"OAL:{wire}"] = "0"
                values[f"OAM:{wire}"] = "OFF"
            for jack in m["audio_jacks"]:
                values[f"AOS:{jack}"] = "MAIN" if "MAIN" in m["jack_assign"] else m["jack_assign"][0]
            for n in range(1, m["gpo"] + 1):
                values[f"GPO:GPO{n}"] = "OFF"
            for n in range(1, m["cameras"] + 1):
                values[f"CPTS:CAMERA{n}"] = "12"
                values[f"CAFC:CAMERA{n}"] = "ON"
                values[f"CAEP:CAMERA{n}"] = "ON"
                values[f"CPR:CAMERA{n}"] = "N/A"
            if m["streaming"]:
                values.update({
                    "STRTY": "RTMP", "STRST": "STOP", "STRTM": "0", "SFIM": "OFF", "EXRC": "OFF",
                    "PBTY": "V.PLAYER", "VPS": "STOP", "VPE": "INTERNAL", "VPD": "60000", "VPC": "0",
                    "SRTIST": "STOPPED",
                })
            for key, value in values.items():
                super().set_state(key, value)
        finally:
            self._seeding = False

    def _sources(self) -> list[str]:
        m = self._m
        out = _seq("HDMI", m["hdmi"]) + _seq("SDI", m["sdi"]) + _seq("STILL", m["stills"])
        if m["players"]:
            out += ["V.PLAYER", "SRT"]
        return out + _seq("INPUT", m["channels"])

    def _resolve(self, selection: str) -> str:
        """A selection as the switcher reports it: source, then the input
        channel it was chosen by."""
        if selection.startswith("INPUT"):
            assigned = self.get_state(f"VIS:{selection}") or "N/A"
            return f"{assigned},{selection}"
        return selection

    # ── Connections ──

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
        self._sessions[client_id] = _Session(signed_in=not self._secret())
        return None

    def _secret(self) -> str:
        return str(self.config.get("password") or "")

    def _session(self) -> _Session:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        cid = self._task_clients.get(task, LOCAL) if task is not None else LOCAL
        session = self._sessions.get(cid)
        if session is None:
            session = _Session(signed_in=not self._secret())
            self._sessions[cid] = session
        return session

    # ── Commands ──

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("ascii", errors="replace").replace("\x02", "").strip()
        if not text:
            return None
        session = self._session()
        if not session.signed_in:
            if text == self._secret():
                session.signed_in = True
            return None
        out: list[str] = []
        for part in text.split(";"):
            part = part.strip()
            if part:
                out.append(self._run(part))
        reply = "".join(out)
        return reply.encode("ascii") if reply else None

    def _run(self, command: str) -> str:
        code, _sep, rest = command.partition(":")
        code = code.strip().upper()
        args = [a.strip() for a in rest.split(",")] if rest else []
        try:
            value = self._dispatch(code, args)
        except _Refused as refused:
            return f"\x02ERR:{refused.code};\n"
        if value is None:
            return "\x02ACK;\n"
        if value[0] in NO_ACK:
            return f"\x02{value[0]}:{value[1]};\n"
        return f"\x02{value[0]}:{value[1]};ACK;\n"

    def _dispatch(self, code: str, args: list[str]) -> tuple[str, str] | None:
        m = self._m
        if code == "ACS":
            return None
        if code == "VER":
            return ("VER", f"{self.get_state('model')},{m['version']}")
        if code in ("STRSTB", "VPSTB", "SRTISTB"):
            self._need(m["streaming"])
            return (code, "OK")
        if code.startswith("Q"):
            return self._query(code[1:], args)
        return self._set(code, args)

    def _query(self, code: str, args: list[str]) -> tuple[str, str]:
        m = self._m
        store = STORE_CODE.get(code, code)
        if code in ("PGM", "PST"):
            self._count(args, 0)
            return (code, self._resolve(self.get_state(code)))
        if code == "AUX":
            self._count(args, 1)
            self._member(args[0], _seq("AUX", m["aux"]))
            return ("AUX", f"{args[0]},{self._resolve(self.get_state(f'AUX:{args[0]}'))}")
        if code == "DSS":
            self._count(args, 1)
            self._member(args[0], _seq("DSK", m["dsk"]))
            return ("DSS", f"{args[0]},{self._resolve(self.get_state(f'DSS:{args[0]}'))}")
        if code == "TIM" and args and args[0] == "DSK" and not m["pinp_time"]:
            # The V-1-4K is asked its DSK time as DSK (its QTIM table).
            return ("TIM", f"DSK,{self.get_state('TIM:DSK1')}")
        if len(args) == 1:
            key = f"{store}:{args[0]}"
            value = self.get_state(key)
            if value is None:
                raise _Refused(0 if self._unknown_code(store) else 5)
            return (store, f"{args[0]},{value}")
        self._count(args, 0)
        value = self.get_state(store)
        if value is None:
            raise _Refused(0)
        return (store, str(value))

    def _unknown_code(self, store: str) -> bool:
        prefix = store + ":"
        return not any(
            value is not None and (key == store or key.startswith(prefix))
            for key, value in self.state.items()
        )

    def _set(self, code: str, args: list[str]) -> None:
        m = self._m
        sources = self._sources()
        if code in TOGGLES and len(args) == TOGGLES[code]:
            key = code if not args else f"{code}:{args[0]}"
            current = self.get_state(key)
            if current is None:
                raise _Refused(0 if self._unknown_code(code) else 5)
            self.set_state(key, "OFF" if current in ("ON", "FADEIN") else "ON")
            return None
        if code in ("PGM", "PST"):
            self._count(args, 1)
            self._member(args[0], sources)
            self.set_state(code, args[0])
            return None
        if code in ("CUT", "ATO"):
            if args:
                self._member(args[0], sources)
                if code == "ATO" and len(args) == 2:
                    self._int(args[1], -1, 40)
                elif len(args) != 1:
                    raise _Refused(0)
                self.set_state("PST", self.get_state("PGM"))
                self.set_state("PGM", args[0])
            else:
                pgm, pst = self.get_state("PGM"), self.get_state("PST")
                self.set_state("PGM", pst)
                self.set_state("PST", pgm)
            return None
        if code == "VFL":
            self._count(args, 1)
            self._int(args[0], 0, m["fader_max"])
            self.set_state("VFL", args[0])
            return None
        if code == "AUX":
            self._count(args, 2)
            self._member(args[0], _seq("AUX", m["aux"]))
            self._member(args[1], sources + ["PGMLINK"])
            # Linked to program, the AUX bus reports what program shows.
            self.set_state(f"AUX:{args[0]}", self.get_state("PGM") if args[1] == "PGMLINK" else args[1])
            return None
        if code in ("FRZ", "FTB", "RVB", "ATM", "ASW", "ROISW", "SEQSW", "SEQAS", "HDCP", "EXRC", "SFIM"):
            self._count(args, 1)
            self._member(args[0], ONOFF)
            self._need(self.get_state(STORE_CODE.get(code, code)) is not None)
            self.set_state(STORE_CODE.get(code, code), args[0])
            return None
        if code == "VIS":
            self._count(args, 2)
            self._member(args[0], _seq("INPUT", m["channels"]))
            self._member(args[1], m["assign"])
            self.set_state(f"VIS:{args[0]}", args[1])
            return None
        if code == "VOS":
            self._count(args, 2)
            self._member(args[0], m["video_outputs"])
            self._member(args[1], m["output_assign"])
            self.set_state(f"VOS:{args[0]}", args[1])
            return None
        if code == "TRS":
            self._count(args, 1)
            self._member(args[0], ("MIX", "WIPE"))
            self.set_state("TRS", args[0])
            return None
        if code == "TIM":
            self._count(args, 2)
            key = f"TIM:{args[0]}"
            self._need(self.get_state(key) is not None, 5)
            self._int(args[1], 0, 40)
            self.set_state(key, args[1])
            return None
        if code == "PIS":
            self._need(m["pinp_source"])
            self._count(args, 2)
            self._member(args[0], _seq("PinP", m["pinp"]))
            self._member(args[1], sources)
            self.set_state(f"PIS:{args[0]}", args[1])
            return None
        if code == "GPO" and len(args) == 1:
            # One shot: a pulse, nothing kept.
            self._need(self.get_state(f"GPO:{args[0]}") is not None, 5)
            return None
        if code in ("PPS", "PPW", "DSK", "DVW", "SPS", "IAM", "IAS", "HPF", "GATE", "STLK", "VOCH",
                    "OAM", "GPO", "CAMAFC", "CAMAEP"):
            self._count(args, 2)
            key = f"{STORE_CODE.get(code, code)}:{args[0]}"
            self._need(self.get_state(key) is not None, 5)
            self._member(args[1], ONOFF)
            self.set_state(key, args[1])
            return None
        if code == "PIP":
            self._count(args, 3)
            self._need(self.get_state(f"PIP:{args[0]}") is not None, 5)
            self._int(args[1], -500, 500)
            self._int(args[2], -500, 500)
            self.set_state(f"PIP:{args[0]}", f"{args[1]},{args[2]}")
            return None
        if code == "DSS":
            self._count(args, 2)
            self._member(args[0], _seq("DSK", m["dsk"]))
            self._member(args[1], sources)
            self.set_state(f"DSS:{args[0]}", args[1])
            return None
        if code in ("KYL", "KYG"):
            self._count(args, 2)
            self._need(self.get_state(f"{code}:{args[0]}") is not None, 5)
            self._int(args[1], 0, 255)
            self.set_state(f"{code}:{args[0]}", args[1])
            return None
        if code == "SPT":
            if len(args) not in (3, 4):
                raise _Refused(0)
            key = f"SPT:{args[0]}"
            self._need(self.get_state(key) is not None, 5)
            for value in args[1:]:
                self._int(value, -500, 500)
            current = str(self.get_state(key)).split(",")
            center = args[3] if len(args) == 4 else current[2]
            self.set_state(key, f"{args[1]},{args[2]},{center}")
            return None
        if code == "STO":
            self._need(m["still_output"])
            self._count(args, 1)
            stills = ["OFF"] + _seq("STILL", m["stills"]) + (["V.PLAYER", "SRT"] if m["players"] else [])
            self._member(args[0], stills)
            self.set_state("STO", args[0])
            return None
        if code == "ROIZM":
            self._count(args, 2)
            self._need(self.get_state(f"ROIZM:{args[0]}") is not None, 5)
            self._int(args[1], 1000, 4000)
            self.set_state(f"ROIZM:{args[0]}", args[1])
            return None
        if code == "ROIPT":
            self._count(args, 3)
            self._need(self.get_state(f"ROIPT:{args[0]}") is not None, 5)
            self._int(args[1], -1920, 1920)
            self._int(args[2], -1080, 1080)
            self.set_state(f"ROIPT:{args[0]}", f"{args[1]},{args[2]}")
            return None
        if code == "AOS":
            self._count(args, 2)
            self._member(args[0], m["audio_jacks"])
            self._member(args[1], m["jack_assign"])
            self.set_state(f"AOS:{args[0]}", args[1])
            return None
        if code in ("IAL", "OAL"):
            self._count(args, 2)
            self._need(self.get_state(f"{code}:{args[0]}") is not None, 5)
            if args[1] != "-INF":
                self._int(args[1], -800, 100)
            self.set_state(f"{code}:{args[0]}", args[1])
            return None
        if code == "ADT":
            self._count(args, 2)
            self._need(self.get_state(f"ADT:{args[0]}") is not None, 5)
            self._int(args[1], 0, 5000)
            self.set_state(f"ADT:{args[0]}", args[1])
            return None
        if code == "MEM":
            self._count(args, 1)
            self._member(args[0], _seq("MEMORY", m["memories"]))
            self.set_state("MEM", args[0])
            return None
        if code in ("INSC", "MEMSC"):
            self._count(args, 1)
            self._member(args[0], ORDERS)
            self._need(code == "INSC" or m["macros"])
            return None
        if code in ("PPSC", "DSKSC"):
            self._need(m["macros"])
            self._count(args, 2)
            ids = _seq("PinP", m["pinp"]) if code == "PPSC" else _seq("DSK", m["dsk"])
            self._member(args[0], ids)
            self._member(args[1], ORDERS)
            return None
        if code == "TPT":
            self._count(args, 1)
            self._member(args[0], m["patterns"])
            self.set_state("TPT", args[0])
            return None
        if code == "TTN":
            if len(args) not in (1, 3):
                raise _Refused(0)
            self._member(args[0], ("OFF", "-20", "-10", "0dB"))
            current = str(self.get_state("TTN")).split(",")
            if len(args) == 3:
                self._member(args[1], ("500", "1k", "2kHz"))
                self._member(args[2], ("500", "1k", "2kHz"))
                current[1:] = args[1:]
            self.set_state("TTN", ",".join([args[0]] + current[1:3]))
            return None
        if code in ("CAMPT", "CAMPTS", "CAMZM", "CAMZMR", "CAMFC", "CAMPR"):
            if not args:
                raise _Refused(0)
            self._member(args[0], _seq("CAMERA", m["cameras"]))
            if code == "CAMPT":
                self._count(args, 3)
                self._member(args[1], ("LEFT", "STOP", "RIGHT"))
                self._member(args[2], ("DOWN", "STOP", "UP"))
            elif code == "CAMPTS":
                self._count(args, 2)
                self._int(args[1], 1, 24)
                self.set_state(f"CPTS:{args[0]}", args[1])
            elif code == "CAMZM":
                self._count(args, 2)
                self._member(args[1], ("WIDE_FAST", "WIDE_SLOW", "STOP", "TELE_SLOW", "TELE_FAST"))
            elif code == "CAMZMR":
                self._count(args, 1)
            elif code == "CAMFC":
                self._count(args, 2)
                self._member(args[1], ("NEAR", "STOP", "FAR"))
            elif code == "CAMPR":
                self._count(args, 2)
                self._member(args[1], _seq("PRESET", 100))
                self.set_state(f"CPR:{args[0]}", args[1])
            return None
        if code in ("MCREX", "SEQNX", "SEQPV", "SEQJP"):
            self._need(m["macros"])
            if code == "MCREX":
                self._count(args, 1)
                self._member(args[0], _seq("MACRO", 100))
            elif code == "SEQPV" and args:
                self._count(args, 1)
                self._member(args[0], ("0", "1"))
            elif code == "SEQJP":
                self._count(args, 1)
                self._member(args[0], ["START"] + _seq("SEQ", 1000))
            return None
        if code in ("GPNC", "GPHF", "GPHB", "GPOA"):
            self._count(args, 0)
            return None
        if code == "GPSC":
            self._count(args, 1)
            self._member(args[0], _seq("CONTENT", 124))
            return None
        if code in ("STRTY", "STROA", "SRTOSS", "APL", "APR", "PBTY", "VPL", "SRTISS"):
            self._need(m["streaming"])
            return self._set_streaming(code, args)
        raise _Refused(0)

    def _set_streaming(self, code: str, args: list[str]) -> None:
        m = self._m
        if code == "STRTY":
            self._count(args, 1)
            self._member(args[0], ("RTMP", "SRT"))
            self.set_state("STRTY", args[0])
        elif code in ("STROA", "SRTOSS"):
            self._count(args, 1)
            self._member(args[0], ("START", "STOP"))
            wanted = "RTMP" if code == "STROA" else "SRT"
            self._need(self.get_state("STRTY") == wanted, 4)
            self.set_state("STRST", "ONAIR" if args[0] == "START" else "STOP")
        elif code in ("APL", "APR"):
            self._count(args, 2)
            self._member(args[0], m["pads"])
            self._member(args[1], ("STOP", "PLAY", "PAUSE", "RESUME") if code == "APL" else ONOFF)
        elif code == "PBTY":
            self._count(args, 1)
            self._member(args[0], ("V.PLAYER", "SRT"))
            self.set_state("PBTY", args[0])
        elif code == "VPL":
            self._count(args, 1)
            self._member(args[0], ("STOP", "PLAY", "PAUSE", "RESUME"))
            self._need(self.get_state("PBTY") == "V.PLAYER", 4)
            self.set_state("VPS", {"RESUME": "PLAY"}.get(args[0], args[0]))
        elif code == "SRTISS":
            self._count(args, 1)
            self._member(args[0], ("START", "STOP"))
            self._need(self.get_state("PBTY") == "SRT", 4)
            self.set_state("SRTIST", "STARTED" if args[0] == "START" else "STOPPED")
        return None

    # ── Checks ──

    @staticmethod
    def _count(args: list[str], n: int) -> None:
        if len(args) != n:
            raise _Refused(0)

    @staticmethod
    def _member(value: str, allowed: Any) -> None:
        if value not in allowed:
            raise _Refused(5)

    @staticmethod
    def _need(condition: bool, code: int = 0) -> None:
        if not condition:
            raise _Refused(code)

    @staticmethod
    def _int(value: str, low: int, high: int) -> None:
        try:
            number = int(value)
        except ValueError:
            raise _Refused(5) from None
        if number < low or number > high:
            raise _Refused(5)
