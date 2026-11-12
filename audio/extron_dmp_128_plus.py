"""Extron DMP 128 Plus room DSP control, SIS reference 68-2826-01 Rev. N.

Python reconciles the DSP's sparse group roster, builds each fader from the
reported soft limits and maps a meter vector to the group's ordered block IDs.
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation

from openavc.core.connection_fault import ConnectionFaultError
from openavc.drivers.base import BaseDriver

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:
    CommandRefusedError = RuntimeError


ESC = "\x1b"
KINDS = {6: "gain_group", 12: "mute_group", 21: "meter_group"}
PORTS = {"input": range(1, 13), "output": range(1, 9)}
GAIN_BASE = {"input": 40100, "output": 60000}
METER_BASE = {"input": 40000, "output": 60000}
ERRORS = {
    "E10": "unknown command",
    "E12": "invalid port",
    "E13": "invalid parameter",
    "E14": "unavailable in this configuration",
    "E22": "DSP busy",
    "E24": "account lacks permission",
    "E25": "device not present",
}


class SisRefusal(CommandRefusedError):
    def __init__(self, code):
        self.code = code
        super().__init__(
            f"Extron refused the command ({code}: {ERRORS.get(code, 'DSP error')})."
        )


def tenths(value):
    try:
        scaled = Decimal(str(value)) * 10
        if scaled.is_finite() and scaled == scaled.to_integral_value():
            return int(scaled)
    except (InvalidOperation, ValueError, OverflowError):
        pass
    raise CommandRefusedError("Enter a level in whole tenths of a decibel.")


def wire_integer(value):
    # The soft-limit key permits either a leading sign or a trailing sign.
    if value[-1:] in ("-", "+"):
        value = value[-1] + value[:-1]
    return int(value)


def number(value, permitted):
    if not re.fullmatch(r"[0-9]+", str(value)) or int(value) not in permitted:
        raise CommandRefusedError("Select an available channel, group or preset.")
    return int(value)


def binary(value):
    if isinstance(value, bool):
        return int(value)
    raise CommandRefusedError("Choose muted or unmuted.")


def unit_query(command):
    return ESC + command


class ExtronDmp128Plus(BaseDriver):
    DRIVER_INFO = {
        "id": "extron_dmp_128_plus",
        "name": "Extron DMP 128 Plus",
        "manufacturer": "Extron",
        "category": "audio",
        "version": "1.0.0",
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "source_url": "https://media.extron.com/public/download/files/userman/dmp_128_plus_68-2826-01_N.pdf",
        "description": "Room audio control for the DMP 128 Plus family. Learns gain and mute groups, "
        "reads fader limits, recalls named presets and shows signal levels.",
        "ports": [22023],
        "protocols": ["extron_sis"],
        "tags": ["dsp", "conferencing", "audio", "sis"],
        "verified": False,
        "simulated": True,
        "compatible_models": [
            {
                "manufacturer": "Extron",
                "models": [
                    "DMP 128 Plus",
                    "DMP 128 Plus C AT",
                    "DMP 128 Plus C",
                    "DMP 128 Plus AT",
                    "DMP 128 Plus C V",
                    "DMP 128 Plus C V AT",
                ],
                "confidence": "untested",
                "notes": "Shared room DSP controls from the 128 Plus User Guide, revision "
                "N. Includes the C AT. Physical qualification pending.",
            }
        ],
        "transport": "ssh",
        "transports": ["ssh", "serial"],
        "default_config": {
            "host": "",
            "port": 22023,
            "username": "admin",
            "ssh_auth_method": "password",
            "baudrate": 38400,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "poll_interval": 30,
            "model": "DMP 128 Plus C AT",
        },
        "config_schema": {
            "host": {
                "type": "string",
                "label": "DSP control LAN address",
                "required": True,
            },
            "port": {
                "type": "integer",
                "label": "SIS over SSH port",
                "min": 1,
                "max": 65535,
                "default": 22023,
            },
            "username": {"type": "string", "label": "SSH username", "default": "admin"},
            "password": {"type": "string", "label": "SSH password", "secret": True},
            "model": {
                "type": "enum",
                "label": "DSP model",
                "default": "DMP 128 Plus C AT",
                "values": [
                    "DMP 128 Plus",
                    "DMP 128 Plus C AT",
                    "DMP 128 Plus C",
                    "DMP 128 Plus AT",
                    "DMP 128 Plus C V",
                    "DMP 128 Plus C V AT",
                ],
                "learned_from": "model",
                "description": "Choose a model for simulation. The live DSP supplies "
                "its own identity.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status refresh (seconds)",
                "default": 30,
                "min": 0,
                "description": "Recheck channel and group readings alongside "
                "change notifications. Zero selects manual "
                "refresh.",
            },
        },
        "help": {
            "overview": "Use the gain and mute groups already designed in DSP Configurator for room "
            "controls. Monitor a meter group or read a single input or output signal "
            "while commissioning.",
            "setup": "Connect to the control LAN and enter the DSP account for SIS over SSH "
            "(normally port 22023). On AT models use the control LAN, not the Dante audio "
            "port. For RS-232 select the local serial port or Through a bridge. Match the "
            "DSP’s serial settings, normally 38400 baud, 8 data bits, no parity, one stop "
            "bit and no flow control. Refresh room controls after editing the DSP project. "
            "Use the pencil beside a group to name it for the room; SIS exposes its type "
            "and members. Bind a panel fader to the gain group’s Level to pick up its "
            "limits.",
            "connection": "A mute group controls every member. Mic/line input levels adjust "
            "pre-mixer gain. Output levels adjust attenuation. Select Monitor a meter "
            "group to see its meters, or Read input signal for a spot check. On "
            "firmware before 1.11.0000-b006 the selection also affects other "
            "controllers. Presets are created and named in DSP Configurator.",
        },
        "discovery": {
            "tcp_probe": {
                "port": 23,
                "expect_regex": "(?i)Extron Electronics[^\\r\\n]*DMP 128 Plus(?: C V "
                "AT| C AT| C V| AT| C)?\\s*,",
                "extract_manufacturer": "Extron",
            },
            "oui": ["00:05:a6"],
            "hostname": ["(?i)^dmp-?128-?plus(?:-|$)"],
            "manufacturer_alias": ["extron"],
        },
        "state_variables": {
            "model": {"type": "string", "label": "Reported DSP model"},
            "firmware": {"type": "string", "label": "Firmware version"},
            "part_number": {"type": "string", "label": "Part number"},
            "group_count": {
                "type": "integer",
                "label": "Configured control and meter groups",
            },
            "active_meter_group": {
                "type": "integer",
                "label": "Monitored meter group",
                "min": 0,
                "max": 64,
                "help": "0 means the DSP reports that streaming is off.",
            },
            "preset_options": {"type": "string", "label": "Preset choices"},
            "last_recalled_preset": {
                "type": "integer",
                "label": "Last acknowledged preset",
                "min": 1,
                "max": 32,
                "help": "Most recent recall observed on this connection.",
            },
        },
        "child_entity_types": {
            "input": {
                "label": "Mic/line input",
                "label_plural": "Mic/line inputs",
                "id_format": {"type": "integer", "min": 1, "max": 12},
                "state_variables": {
                    "level": {
                        "type": "number",
                        "label": "Pre-mixer level",
                        "min": -100,
                        "max": 12,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                    },
                    "muted": {"type": "boolean", "label": "Muted", "control": True},
                    "meter": {
                        "type": "number",
                        "label": "Signal level",
                        "min": -150,
                        "max": 0,
                        "step": 0.1,
                        "unit": "dBFS",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["level", "muted"],
                "instances": {"count": 12, "label": "Mic/line input {id}"},
            },
            "output": {
                "label": "Line output",
                "label_plural": "Line outputs",
                "id_format": {"type": "integer", "min": 1, "max": 8},
                "state_variables": {
                    "level": {
                        "type": "number",
                        "label": "Output attenuation",
                        "min": -100,
                        "max": 0,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                    },
                    "muted": {"type": "boolean", "label": "Muted", "control": True},
                    "meter": {
                        "type": "number",
                        "label": "Signal level",
                        "min": -150,
                        "max": 0,
                        "step": 0.1,
                        "unit": "dBFS",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["level", "muted"],
                "instances": {"count": 8, "label": "Line output {id}"},
            },
            "gain_group": {
                "label": "Gain group",
                "label_plural": "Gain groups",
                "id_format": {"type": "integer", "min": 1, "max": 64},
                "state_variables": {
                    "level": {
                        "type": "number",
                        "label": "Group level",
                        "min": -100,
                        "max": 80,
                        "step": 0.1,
                        "unit": "dB",
                        "control": True,
                    },
                    "members": {"type": "string", "label": "Controlled blocks"},
                    "lower_limit": {
                        "type": "number",
                        "label": "Lower soft limit",
                        "min": -100,
                        "max": 80,
                        "step": 0.1,
                        "unit": "dB",
                    },
                    "upper_limit": {
                        "type": "number",
                        "label": "Upper soft limit",
                        "min": -100,
                        "max": 80,
                        "step": 0.1,
                        "unit": "dB",
                    },
                },
                "summary_fields": ["level", "members"],
                "dynamic": True,
            },
            "mute_group": {
                "label": "Mute group",
                "label_plural": "Mute groups",
                "id_format": {"type": "integer", "min": 1, "max": 64},
                "state_variables": {
                    "muted": {"type": "boolean", "label": "Muted", "control": True},
                    "members": {"type": "string", "label": "Controlled blocks"},
                },
                "summary_fields": ["muted", "members"],
            },
            "meter_group": {
                "label": "Meter group",
                "label_plural": "Meter groups",
                "id_format": {"type": "integer", "min": 1, "max": 64},
                "state_variables": {
                    "members": {"type": "string", "label": "Metered blocks"}
                },
                "summary_fields": ["members"],
            },
            "meter": {
                "label": "Meter",
                "label_plural": "Group meters",
                "id_format": {"type": "integer", "min": 1, "max": 999999},
                "state_variables": {
                    "dbfs": {
                        "type": "number",
                        "label": "Signal level",
                        "min": -150,
                        "max": 0,
                        "step": 0.1,
                        "unit": "dBFS",
                        "cloud_priority": "low",
                    },
                    "oid": {"type": "integer", "label": "DSP block address"},
                },
                "summary_fields": ["dbfs"],
            },
        },
        "commands": {
            "set_input_level": {
                "label": "Set Pre-mixer level",
                "help": "Adjust this input before the mix. Microphone preamp "
                "gain stays at its commissioned setting.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Mic/line input",
                        "child_type": "input",
                        "required": True,
                    },
                    "level": {
                        "type": "number",
                        "label": "Level (dB)",
                        "min": -100,
                        "max": 12,
                        "required": True,
                    },
                },
                "sets": {"level": "{level}"},
            },
            "set_output_level": {
                "label": "Set Output attenuation",
                "help": "Adjust the loudspeaker feed from this output.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Line output",
                        "child_type": "output",
                        "required": True,
                    },
                    "level": {
                        "type": "number",
                        "label": "Level (dB)",
                        "min": -100,
                        "max": 0,
                        "required": True,
                    },
                },
                "sets": {"level": "{level}"},
            },
            "set_gain_group_level": {
                "label": "Set Group level",
                "help": "Adjust the selected room fader within the DSP’s "
                "current soft limits.",
                "params": {
                    "gain_group": {
                        "type": "child_id",
                        "label": "Gain group",
                        "child_type": "gain_group",
                        "required": True,
                    },
                    "level": {
                        "type": "number",
                        "label": "Level (dB)",
                        "min": -100,
                        "max": 80,
                        "required": True,
                    },
                },
                "sets": {"level": "{level}"},
            },
            "set_input_mute": {
                "label": "Set mic/line input mute",
                "help": "Mute or restore audio on the selected mic/line input and "
                "confirm the DSP’s mute status.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "mic/line input",
                        "child_type": "input",
                        "required": True,
                    },
                    "muted": {"type": "boolean", "label": "Mute", "required": True},
                },
                "sets": {"muted": "{muted}"},
            },
            "set_output_mute": {
                "label": "Set line output mute",
                "help": "Mute or restore audio on the selected line output and "
                "confirm the DSP’s mute status.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "line output",
                        "child_type": "output",
                        "required": True,
                    },
                    "muted": {"type": "boolean", "label": "Mute", "required": True},
                },
                "sets": {"muted": "{muted}"},
            },
            "set_mute_group_mute": {
                "label": "Set mute group mute",
                "help": "Mute or restore audio on the selected mute group "
                "and confirm the DSP’s mute status.",
                "params": {
                    "mute_group": {
                        "type": "child_id",
                        "label": "mute group",
                        "child_type": "mute_group",
                        "required": True,
                    },
                    "muted": {"type": "boolean", "label": "Mute", "required": True},
                },
                "sets": {"muted": "{muted}"},
            },
            "read_input_meter": {
                "label": "Read input signal",
                "help": "Read the current signal level in dBFS without changing "
                "the audio path.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "query_for": "meter",
            },
            "read_output_meter": {
                "label": "Read output signal",
                "help": "Read the current signal level in dBFS without "
                "changing the audio path.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "output",
                        "child_type": "output",
                        "required": True,
                    }
                },
                "query_for": "meter",
            },
            "watch_meter_group": {
                "label": "Monitor a meter group",
                "help": "Display the group’s live levels in dBFS. Firmware "
                "before 1.11.0000-b006 shares this selection across "
                "controllers.",
                "params": {
                    "meter_group": {
                        "type": "child_id",
                        "label": "Meter group",
                        "child_type": "meter_group",
                        "required": True,
                    }
                },
                "sets": {"active_meter_group": "{meter_group}"},
            },
            "stop_meters": {
                "label": "Stop monitoring meters",
                "help": "Turn off the selected meter stream. On older firmware the "
                "meter selection is shared.",
                "sets": {"active_meter_group": 0},
            },
            "recall_preset": {
                "label": "Recall a room preset",
                "help": "Apply a preset saved in DSP Configurator and refresh the "
                "affected controls.",
                "params": {
                    "preset": {
                        "type": "string",
                        "label": "Preset",
                        "options_state": "preset_options",
                        "required": True,
                    }
                },
            },
            "refresh_controls": {
                "label": "Refresh room controls",
                "help": "Read channel names, group memberships, soft limits and "
                "preset names after editing the DSP design.",
                "query_for": "group_count",
            },
            "read_status": {
                "label": "Read DSP status",
                "help": "Read the firmware and the current channel and group settings.",
                "query_for": "firmware",
            },
        },
        "actions": [
            {"id": "set_gain_group_level"},
            {"id": "set_mute_group_mute"},
            {"id": "recall_preset"},
            {"id": "watch_meter_group"},
            {"id": "refresh_controls"},
        ],
        "quick_actions": [
            "set_gain_group_level",
            "set_mute_group_mute",
            "recall_preset",
        ],
    }

    HEALTH_INTERVAL_S = 45
    HEALTH_TIMEOUT_S = 6
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The audio processor stopped answering SIS commands."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wire = asyncio.Lock()
        self._inventory = asyncio.Lock()
        self._rx = b""
        self._pending = None
        self._groups = {}
        self._limits = {}
        self._members = {}
        self._names = {}
        self._generated_labels = {}
        self._active = 0
        self._presets = {}

    def _resolve_delimiter(self):
        # SSH delivers arbitrary chunks. Use the same SIS framer on all links.
        return None

    async def _ask(self, command, pattern):
        async with self._wire:
            if self.transport is None:
                raise ConnectionError("The DSP control connection is offline.")
            future = asyncio.get_running_loop().create_future()
            self._pending = (re.compile(pattern, re.I), future)
            try:
                await self.transport.send((command + "\r").encode("ascii"))
                return await asyncio.wait_for(future, 3)
            finally:
                self._pending = None
                future.cancel()

    async def on_data_received(self, data):
        self._rx += data
        while b"\n" in self._rx:
            line, _, self._rx = self._rx.partition(b"\n")
            self._receive(line.rstrip(b"\r").decode("latin-1").strip())
        if len(self._rx) > 16384:
            self._rx = b""

    def _receive(self, line):
        self._feedback(line)
        if not self._pending:
            return
        pattern, future = self._pending
        if future.done():
            return
        if re.fullmatch(r"E\d\d", line):
            future.set_exception(SisRefusal(line))
            return
        match = pattern.fullmatch(line)
        if match:
            future.set_result(match)

    def _feedback(self, line):
        match = re.fullmatch(r"Ds([GM])(\d+)\*([+-]?\d+)", line, re.I)
        if match:
            self._channel_feedback(match[1].upper(), int(match[2]), int(match[3]))
            return
        match = re.fullmatch(r"GrpmD(\d+)\*([+-]?\d+)", line, re.I)
        if match:
            self._group_feedback(int(match[1]), int(match[2]))
            return
        match = re.fullmatch(r"GrpmV(\d+)((?:\*\d+)+)", line, re.I)
        if match:
            self._meter_feedback(
                int(match[1]), [int(n) for n in match[2][1:].split("*")]
            )
            return
        match = re.fullmatch(r"Rpr0*(\d+)", line, re.I)
        if match and 1 <= int(match[1]) <= 32:
            self.set_state("last_recalled_preset", int(match[1]))

    def _channel_feedback(self, op, oid, raw):
        for kind, base in GAIN_BASE.items():
            port = oid - base + 1
            if port in PORTS[kind] and self.is_child_registered(kind, port):
                prop, value = ("muted", bool(raw)) if op == "M" else ("level", raw / 10)
                self.set_child_state(kind, port, prop, value)

    def _group_feedback(self, slot, raw):
        kind = self._groups.get(slot)
        if kind == "gain_group":
            self.set_child_state(kind, slot, "level", raw / 10)
        elif kind == "mute_group":
            self.set_child_state(kind, slot, "muted", bool(raw))

    def _meter_feedback(self, slot, values):
        members = self._members.get(slot, ())
        if slot != self._active or len(members) != len(values):
            return
        for oid, raw in zip(members, values):
            if raw <= 1500 and self.is_child_registered("meter", oid):
                self.set_child_state("meter", oid, "dbfs", -raw / 10)

    async def _close_session(self):
        if self._pending and not self._pending[1].done():
            self._pending[1].set_exception(
                ConnectionError("The DSP connection closed.")
            )
        self._rx = b""
        self._activate(0)
        self.set_state("active_meter_group", None)

    async def _post_connect(self):
        self._rx = b""
        identity = await self._ask("1I", r"(?:Inf0?1\*)?([A-Za-z][A-Za-z0-9 -]{1,70})")
        reported = " ".join(identity[1].split())
        models = self.DRIVER_INFO["compatible_models"][0]["models"]
        model = next((m for m in models if m.casefold() == reported.casefold()), None)
        if model is None:
            raise ConnectionFaultError(
                f"The processor reported {reported}. Select a DMP 128 Plus model.",
                code="invalid_config",
            )
        self.set_state("model", model)
        await self._ask(unit_query("3CV"), r"Vrb0?3")
        self.set_state("last_recalled_preset", None)

    async def _read_firmware(self):
        match = await self._ask("Q", r"(?:Ver(?:0?1)?\*?)?(\d+\.\d+(?:\.[\w-]+)?)")
        self.set_state("firmware", match[1])

    async def _liveness_probe(self):
        await self._read_firmware()

    async def _initial_sync(self):
        await self._read_firmware()
        part = await self._ask("N", r"(?:Pno\s*\*?)?(60-\d{4}-\d{2})")
        self.set_state("part_number", part[1])
        await self.refresh_children()

    async def _name(self, number, suffix, tag):
        # Verbose queries use the echoed name tag. The guide also shows bare
        # names. Reserve SIS report prefixes so a pushed reading cannot name a port.
        plain = r"(?!Ds[GMV]\d|Grpm|Grpu|Rpr\d|Vrb\d|N[mex][iolxg]\d|E\d\d$|.*\*)([^,\r\n\x1b]{0,128})"
        match = await self._ask(
            unit_query(f"{number}{suffix}"), rf"(?:{tag}0*{number},)?{plain}"
        )
        return match[1].strip()

    def _label(self, kind, number, label):
        current = self.get_child_state(kind, number).get("label")
        key = kind, number
        if not current or current == self._generated_labels.get(key):
            self.set_child_state(kind, number, "label", label)
            self._generated_labels[key] = label

    async def _load_ports(self):
        for kind, ports in PORTS.items():
            suffix, tag = ("NI", "Nmi") if kind == "input" else ("NO", "Nmo")
            for port in ports:
                self.register_child(kind, port)
                name = await self._name(port, suffix, tag)
                self._names[kind, port] = name or f"{kind.title()} {port}"
                self._label(kind, port, self._names[kind, port])

    def _block_name(self, oid):
        banks = (
            (40000, "input", "input"),
            (40100, "input", "pre-mixer"),
            (60000, "output", "output"),
            (60100, "output", "post-mixer"),
        )
        for base, kind, stage in banks:
            port = oid - base + 1
            if port in PORTS[kind]:
                return f"{self._names.get((kind, port), f'{kind.title()} {port}')} ({stage})"
        return f"DSP block {oid}"

    async def _group_read(self, op, slot, reply):
        return await self._ask(
            unit_query(f"{op}{slot}GRPM"), rf"Grpm{op}0*{slot}\*{reply}"
        )

    async def _group_kind(self, slot):
        try:
            match = await self._group_read("P", slot, r"(\d+)")
        except SisRefusal as exc:
            if exc.code not in ("E13", "E14", "E25"):
                raise
            return None
        return KINDS.get(int(match[1]))

    async def _soft_limits(self, slot):
        match = await self._group_read("L", slot, r"([+-]?\d+[+-]?)\*([+-]?\d+[+-]?)")
        upper, lower = wire_integer(match[1]), wire_integer(match[2])
        if not -1000 <= lower <= upper <= 800:
            raise CommandRefusedError(
                f"Gain group {slot} reported invalid soft limits."
            )
        return lower, upper

    async def _inventory_group(self, slot, kind):
        reply = await self._ask(
            unit_query(f"O{slot}GRPM"), rf"GrpmO0*{slot}((?:\*\d+)*)"
        )
        members = tuple(sorted(int(n) for n in reply[1].split("*") if n))
        limits = await self._soft_limits(slot) if kind == "gain_group" else None
        return kind, members, limits

    def _install_group(self, slot, kind, members, limits):
        if kind == "gain_group":
            self._install_fader(slot, limits)
        else:
            self.register_child(kind, slot)
        self._groups[slot] = kind
        self._members[slot] = members
        names = [self._block_name(oid) for oid in members]
        self.set_child_state(kind, slot, "members", ", ".join(names))
        label = f"Group {slot}" + (f" · {names[0]}" if names else "")
        self._label(kind, slot, label)

    def _install_fader(self, slot, limits):
        kind = "gain_group"
        schema = self.DRIVER_INFO["child_entity_types"][kind]["state_variables"].copy()
        lower, upper = limits
        schema["level"] = dict(schema["level"], min=lower / 10, max=upper / 10)
        if self.is_child_registered(kind, slot) and self._limits.get(slot) != limits:
            self.deregister_child(kind, slot)
        self.register_child(kind, slot, schema=schema)
        self._limits[slot] = limits
        self.set_child_state(kind, slot, "lower_limit", lower / 10)
        self.set_child_state(kind, slot, "upper_limit", upper / 10)

    async def _load_groups(self):
        roster = {}
        for slot in range(1, 65):
            kind = await self._group_kind(slot)
            if kind:
                roster[slot] = await self._inventory_group(slot, kind)
        for slot, old_kind in tuple(self._groups.items()):
            if slot not in roster or roster[slot][0] != old_kind:
                self.deregister_child(old_kind, slot)
                self._groups.pop(slot)
                self._members.pop(slot, None)
                self._limits.pop(slot, None)
        for slot, group in roster.items():
            self._install_group(slot, *group)
        self.set_state("group_count", len(roster))

    async def _load_presets(self):
        choices = {}
        for slot in range(1, 33):
            try:
                name = await self._name(slot, "NG", "Nmg")
            except SisRefusal as exc:
                if exc.code not in ("E13", "E14", "E25"):
                    raise
                continue
            if name:
                choices[slot] = name
        self._presets = choices
        self.set_state(
            "preset_options",
            json.dumps(
                [
                    {"value": str(n), "label": f"{n} · {name}"}
                    for n, name in choices.items()
                ]
            ),
        )

    async def refresh_children(self):
        async with self._inventory:
            await self._load_ports()
            await self._load_groups()
            await self._load_presets()
            await self.poll()

    def _activate(self, slot):
        members = (
            set(self._members.get(slot, ()))
            if self._groups.get(slot) == "meter_group"
            else set()
        )
        current = set(self.list_children("meter"))
        for oid in current - members:
            self.deregister_child("meter", oid)
        for oid in members:
            self.register_child("meter", oid)
            self._label("meter", oid, self._block_name(oid))
            self.set_child_state("meter", oid, "oid", oid)
            if slot != self._active:
                self.set_child_state("meter", oid, "dbfs", None)
        self._active = slot
        self.set_state("active_meter_group", slot)

    async def _meter_selection(self):
        reply = await self._ask(unit_query("GGRPU"), r"GrpuG0*(\d+)")
        self._activate(int(reply[1]))

    async def _channel_read(self, op, oid):
        return await self._ask(unit_query(f"{op}{oid}AU"), rf"Ds{op}{oid}\*([+-]?\d+)")

    async def poll(self):
        await self._read_firmware()
        for kind, ports in PORTS.items():
            for port in ports:
                oid = GAIN_BASE[kind] + port - 1
                await self._channel_read("G", oid)
                await self._channel_read("M", oid)
        for slot, kind in tuple(self._groups.items()):
            if kind != "meter_group":
                await self._group_read("D", slot, r"([+-]?\d+)")
        await self._meter_selection()

    async def _write_channel(self, kind, prop, params):
        port = number(params.get(kind), PORTS[kind])
        oid = GAIN_BASE[kind] + port - 1
        op = "M" if prop == "mute" else "G"
        raw = binary(params.get("muted")) if op == "M" else tenths(params.get("level"))
        maximum = 120 if kind == "input" else 0
        if op == "G" and not -1000 <= raw <= maximum:
            raise CommandRefusedError("Choose a level within this channel’s range.")
        await self._ask(unit_query(f"{op}{oid}*{raw}AU"), rf"Ds{op}{oid}\*([+-]?\d+)")
        actual = await self._channel_read(op, oid)
        self._confirm(int(actual[1]), raw)

    @staticmethod
    def _confirm(actual, requested):
        if actual != requested:
            raise CommandRefusedError(
                "The DSP reported a different setting after the command."
            )

    async def _write_group(self, kind, params):
        slot = number(params.get(kind), self.list_children(kind))
        if kind == "gain_group":
            raw = tenths(params.get("level"))
            limits = await self._soft_limits(slot)
            self._install_fader(slot, limits)
            if not limits[0] <= raw <= limits[1]:
                raise CommandRefusedError(
                    f"Group {slot} allows {limits[0] / 10:g} to {limits[1] / 10:g} dB."
                )
        else:
            raw = binary(params.get("muted"))
        await self._ask(unit_query(f"D{slot}*{raw}GRPM"), rf"GrpmD0*{slot}\*([+-]?\d+)")
        actual = await self._group_read("D", slot, r"([+-]?\d+)")
        self._confirm(int(actual[1]), raw)

    async def _spot_meter(self, kind, params):
        port = number(params.get(kind), PORTS[kind])
        oid = METER_BASE[kind] + port - 1
        value = await self._ask(unit_query(f"V{oid}AU"), rf"(?:DsV{oid}\*)?[12]\*(\d+)")
        raw = number(value[1], range(1501))
        self.set_child_state(kind, port, "meter", -raw / 10)

    async def _select_meters(self, slot):
        await self._ask(unit_query(f"G{slot}GRPU"), rf"GrpuG0*{slot}")
        self._activate(slot)
        await self._meter_selection()
        self._confirm(self._active, slot)

    async def _recall(self, value):
        slot = number(value, range(1, 33))
        await self._ask(f"{slot}.", rf"Rpr0*{slot}")
        await self.poll()

    async def send_command(self, command, params=None):
        values = params or {}
        if command == "refresh_controls":
            return await self.refresh_children()
        if command == "read_status":
            return await self.poll()
        if command == "recall_preset":
            return await self._recall(values.get("preset"))
        if command == "stop_meters":
            return await self._select_meters(0)
        if command == "watch_meter_group":
            return await self._select_meters(
                number(values.get("meter_group"), self.list_children("meter_group"))
            )
        if command in ("set_gain_group_level", "set_mute_group_mute"):
            return await self._write_group(
                "gain_group" if command == "set_gain_group_level" else "mute_group",
                values,
            )
        return await self._channel_command(command, values)

    async def _channel_command(self, command, params):
        match = re.fullmatch(r"set_(input|output)_(level|mute)", command)
        if match:
            return await self._write_channel(match[1], match[2], params)
        match = re.fullmatch(r"read_(input|output)_meter", command)
        if match:
            return await self._spot_meter(match[1], params)
        raise CommandRefusedError("Unknown DMP room control.")
