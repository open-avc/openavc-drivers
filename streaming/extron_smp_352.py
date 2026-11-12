"""Extron SMP 352 lecture capture, SIS reference 68-2238-01 Rev. R.

Python reconciles the hardware and encoder-mode rosters and separates session
notifications from solicited replies, including two-destination recording times.
"""

from __future__ import annotations

import asyncio
import json
import re
from ipaddress import IPv4Address
from urllib.parse import quote

from openavc.core.connection_fault import ConnectionFaultError
from openavc.drivers.base import BaseDriver

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:
    CommandRefusedError = RuntimeError


ESC = "\x1b"
PARTS = {"60-1634-11": "SMP 352", "60-1634-12": "SMP 352 3G-SDI"}
RECORD = ("Stopped", "Recording", "Paused")
STREAMS = {1: "Archive A", 2: "Archive B", 3: "Confidence"}
HDMI = (1, 2, 4)
CLOCK = r"\d+:[0-5]\d:[0-5]\d"
ERRORS = {
    "E10": "unrecognized command",
    "E12": "invalid input or output",
    "E13": "invalid parameter",
    "E14": "unavailable in this configuration",
    "E17": "unavailable for this signal",
    "E18": "operation timed out",
    "E22": "recorder is busy",
    "E24": "account lacks permission",
    "E26": "connection limit reached",
    "E28": "file not found",
}


class ExtronSmp352(BaseDriver):
    DRIVER_INFO = {
        "id": "extron_smp_352",
        "name": "Extron SMP 352",
        "manufacturer": "Extron",
        "category": "streaming",
        "version": "1.0.0",
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "source_url": "https://media.extron.com/public/download/files/userman/smp_300_series_68-2238-01_R.pdf",
        "description": "Control lecture recording and streaming. Detects the recorder model, reads saved "
        "input and layout names, and reports signal, storage and recording status.",
        "ports": [22023],
        "protocols": ["extron_sis"],
        "tags": ["lecture-capture", "recorder", "streaming", "sis"],
        "verified": False,
        "simulated": True,
        "compatible_models": [
            {
                "manufacturer": "Extron",
                "models": ["SMP 352", "SMP 352 3G-SDI"],
                "confidence": "untested",
                "notes": "Standard and 3G-SDI 400 GB models. Both exercised in simulation "
                "using the SMP 300 Series User Guide, revision R.",
            }
        ],
        "transport": "ssh",
        "transports": ["ssh", "serial"],
        "default_config": {
            "confidence_preview": "Off",
            "host": "",
            "port": 22023,
            "username": "admin",
            "ssh_auth_method": "password",
            "baudrate": 9600,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "poll_interval": 5,
            "model": "SMP 352",
        },
        "config_schema": {
            "confidence_preview": {
                "type": "enum",
                "label": "Confidence preview in Video Panel",
                "default": "Off",
                "values": ["Off", "RTSP unicast pull", "RTSP multicast pull"],
                "description": "If the confidence encoder is configured for RTSP pull, select its method. OpenAVC reads the recorder’s address and stream name. Leave off for push streams.",
            },
            "host": {"type": "string", "label": "Recorder address", "required": True},
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
                "label": "Recorder model for simulation",
                "default": "SMP 352",
                "values": ["SMP 352", "SMP 352 3G-SDI"],
                "learned_from": "model",
                "description": "Choose a model to simulate. A connected recorder "
                "identifies itself.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status refresh (seconds)",
                "default": 5,
                "min": 0,
                "description": "Seconds between updates to signal, recording "
                "time, storage and stream health. Zero "
                "selects manual refresh. Recording changes "
                "also arrive from the recorder.",
            },
        },
        "help": {
            "overview": "Operate a commissioned lecture recorder, recall its saved layouts and see "
            "whether its inputs have signal. Recording and streaming are independent.",
            "setup": "Enter the recorder address and SIS login. SSH normally uses port 22023. For "
            "RS-232 choose a local serial port or Through a bridge, at 9600 baud, 8 data "
            "bits, no parity and one stop bit. The recorder identifies its inputs on "
            "connection. Choose Refresh names after renaming sources or layouts in the "
            "Extron web interface.",
            "connection": "Use Read recorder status before class. Check the selected input, "
            "available internal recording time and recording destination. Recording "
            "time remaining refers to an active recording. RTMP Live reports whether "
            "the recorder has connected to its configured destination. Set up "
            "destinations and schedules in the Extron web interface.",
        },
        "discovery": {
            "tcp_probe": {
                "port": 23,
                "expect_regex": "(?i)Extron Electronics,\\s*SMP 352(?: 3G-SDI)?\\s*,",
                "extract_manufacturer": "Extron",
                "extract": {"model": {"regex": "(?i)(SMP 352(?: 3G-SDI)?)\\s*,"}},
            },
            "oui": ["00:05:a6"],
            "hostname": ["(?i)^smp-?352(?:-|$)"],
            "manufacturer_alias": ["extron"],
        },
        "state_variables": {
            "model": {"type": "string", "label": "Reported model"},
            "part_number": {"type": "string", "label": "Part number"},
            "firmware": {"type": "string", "label": "Firmware version"},
            "record_state": {
                "type": "string",
                "label": "Recording status",
                "cloud_priority": "high",
            },
            "preview_url": {"type": "string", "label": "Confidence preview URL"},
            "preview_format": {"type": "string", "label": "Confidence preview format"},
            "recording": {
                "type": "boolean",
                "label": "Recording indicator",
                "cloud_priority": "high",
                "help": "On only while the recorder reports active recording. Off while stopped or paused.",
            },
            "internal_minutes": {
                "type": "number",
                "label": "Internal recording time available",
                "min": 0,
                "unit": "min",
                "cloud_priority": "low",
                "help": "Use for a low-storage monitor. Based on the recorder’s estimate for internal storage.",
            },
            "has_alarm": {
                "type": "boolean",
                "label": "Recorder has active alarms",
                "cloud_priority": "high",
            },
            "elapsed": {
                "type": "string",
                "label": "Recording elapsed",
                "cloud_priority": "low",
            },
            "remaining": {
                "type": "string",
                "label": "Time remaining by recording destination",
                "cloud_priority": "low",
            },
            "destination": {"type": "string", "label": "Recording destination"},
            "internal_time": {
                "type": "string",
                "label": "Available recording time on internal storage",
            },
            "encoder_mode": {"type": "string", "label": "Encoding mode"},
            "layout_options": {"type": "string", "label": "Layout choices"},
            "layout": {
                "type": "integer",
                "label": "Current layout number",
                "min": 0,
                "max": 16,
            },
            "layout_name": {"type": "string", "label": "Current layout"},
            "alarms": {
                "type": "string",
                "label": "Active recorder alarms",
                "cloud_priority": "high",
            },
        },
        "child_entity_types": {
            "input": {
                "label": "Capture input",
                "label_plural": "Capture inputs",
                "id_format": {"type": "integer", "min": 1, "max": 5},
                "state_variables": {
                    "selected": {
                        "type": "boolean",
                        "label": "Selected",
                        "control": True,
                    },
                    "signal": {"type": "boolean", "label": "Signal present"},
                    "format": {"type": "string", "label": "Selected video format"},
                    "hdcp": {"type": "string", "label": "HDMI protection"},
                },
                "summary_fields": ["selected", "signal"],
            },
            "stream": {
                "label": "Stream output",
                "label_plural": "Stream outputs",
                "id_format": {"type": "integer", "min": 1, "max": 3},
                "state_variables": {
                    "enabled": {
                        "type": "boolean",
                        "label": "Streaming enabled",
                        "control": True,
                    },
                    "rtmp_enabled": {
                        "type": "boolean",
                        "label": "RTMP publishing enabled",
                        "control": True,
                    },
                    "primary_live": {
                        "type": "boolean",
                        "label": "Primary RTMP live",
                        "cloud_priority": "high",
                    },
                    "backup_live": {"type": "boolean", "label": "Backup RTMP live"},
                },
                "summary_fields": ["enabled", "primary_live"],
            },
        },
        "commands": {
            "read_status": {
                "label": "Read recorder status",
                "help": "Read the recording state, times, sources, storage and "
                "stream status.",
                "query_for": "record_state",
            },
            "start_recording": {
                "label": "Start or resume recording",
                "help": "Start capture, or resume a paused recording.",
                "sets": {"record_state": "Recording"},
            },
            "pause_recording": {
                "label": "Pause recording",
                "help": "Pause the current recording.",
                "sets": {"record_state": "Paused"},
            },
            "stop_recording": {
                "label": "Stop recording",
                "help": "Finish the current recording and close its file.",
                "sets": {"record_state": "Stopped"},
            },
            "add_marker": {
                "label": "Add chapter marker",
                "help": "Place a chapter marker in the current recording.",
            },
            "select_input": {
                "label": "Select capture input",
                "help": "Select this input for its assigned capture channel. "
                "Inputs 1 and 2 feed A. The remaining inputs feed B.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Capture input",
                        "required": True,
                        "child_type": "input",
                    }
                },
                "sets": {"selected": True},
            },
            "recall_layout": {
                "label": "Recall layout, keep sources",
                "help": "Apply the composite layout and keep both current input "
                "selections.",
                "params": {
                    "preset": {
                        "type": "string",
                        "label": "Saved layout",
                        "required": True,
                        "options_state": "layout_options",
                    }
                },
                "sets": {"layout": "{preset}"},
            },
            "recall_layout_with_inputs": {
                "label": "Recall layout and saved sources",
                "help": "Apply the composite layout and switch both "
                "channels to the sources saved with it.",
                "params": {
                    "preset": {
                        "type": "string",
                        "label": "Saved layout",
                        "required": True,
                        "options_state": "layout_options",
                    }
                },
                "sets": {"layout": "{preset}"},
            },
            "set_stream": {
                "label": "Set stream output",
                "help": "Enable or disable the selected configured stream.",
                "params": {
                    "stream": {
                        "type": "child_id",
                        "label": "Stream output",
                        "required": True,
                        "child_type": "stream",
                    },
                    "enabled": {
                        "type": "boolean",
                        "label": "Enabled",
                        "required": True,
                    },
                },
                "sets": {"enabled": "{enabled}"},
            },
            "set_rtmp": {
                "label": "Set RTMP publishing",
                "help": "Enable or disable publishing to the RTMP destinations "
                "configured on the recorder. Check Primary RTMP live for "
                "delivery.",
                "params": {
                    "stream": {
                        "type": "child_id",
                        "label": "Stream output",
                        "required": True,
                        "child_type": "stream",
                    },
                    "enabled": {
                        "type": "boolean",
                        "label": "Enabled",
                        "required": True,
                    },
                },
                "sets": {"rtmp_enabled": "{enabled}"},
            },
            "refresh_names": {
                "label": "Refresh names and layouts",
                "help": "Read input names and saved composite layouts from this "
                "recorder.",
                "query_for": "layout_options",
            },
        },
        "actions": [
            {"id": "read_status"},
            {"id": "start_recording"},
            {"id": "pause_recording"},
            {"id": "add_marker"},
            {"id": "stop_recording"},
            {"id": "refresh_names"},
        ],
        "quick_actions": ["read_status", "select_input", "recall_layout"],
        "web_ui": True,
    }

    HEALTH_INTERVAL_S = 45
    HEALTH_TIMEOUT_S = 6
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The recorder stopped answering SIS queries."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wire = asyncio.Lock()
        self._sync = asyncio.Lock()
        self._rx = b""
        self._pending = None
        self._inputs = ()
        self._labels = {}
        self._layouts = {}
        self._mode = None

    def _resolve_delimiter(self):
        return None  # SSH, local serial and bridged serial share the SIS framer.

    async def _ask(self, cmd, pattern):
        async with self._wire:
            if self.transport is None:
                raise ConnectionError("Recorder is offline.")
            future = asyncio.get_running_loop().create_future()
            self._pending = cmd, re.compile(pattern, re.I), future
            try:
                await self.transport.send((cmd + "\r").encode("ascii"))
                return await asyncio.wait_for(future, 3)
            finally:
                self._pending = None
                future.cancel()

    async def on_data_received(self, data):
        self._rx += data
        while b"\n" in self._rx:
            line, _, self._rx = self._rx.partition(b"\n")
            self._receive(line.rstrip(b"\r").decode("latin-1"))
        if len(self._rx) > 16384:
            self._rx = b""

    def _receive(self, line):
        if self._pending:
            cmd, pattern, future = self._pending
            if line in (cmd, cmd.replace(ESC, "^[")):
                return
            self._answer(line, pattern, future)
        self._notice(line)

    @staticmethod
    def _answer(line, pattern, future):
        if future.done():
            return
        if re.fullmatch(r"E\d\d", line):
            reason = ERRORS.get(line, "device error")
            future.set_exception(
                CommandRefusedError(f"Recorder refused the command ({line}, {reason}).")
            )
        elif match := pattern.fullmatch(line):
            future.set_result(match)

    async def _post_connect(self):
        self._rx = b""
        # N is a read-only identity probe. A model selected for simulation never
        # licenses an input on the live recorder.
        part = (await self._ask("N", r"(?:Pno\s*\*?)?(60-\d{4}-\d{2})"))[1]
        if part not in PARTS:
            raise ConnectionFaultError(
                f"Part number {part} does not identify an SMP 352. Check the recorder address.",
                code="invalid_config",
            )
        self._inputs = tuple(range(1, 6 if part.endswith("-12") else 5))
        self.set_states({"part_number": part, "model": PARTS[part]})
        # Mode 1 pushes front-panel and other controller changes, and leaves
        # read replies untagged (including commissioning names and elapsed time).
        await self._ask(ESC + "1CV", r"Vrb0?1")

    async def _close_session(self):
        if self._pending and not self._pending[2].done():
            self._pending[2].set_exception(ConnectionError("Recorder disconnected."))
        self._rx = b""
        self._inputs = ()
        self._mode = None
        self._layouts.clear()
        self._clear_preview()
        self.set_states(
            {"record_state": None, "recording": None, "layout_options": "[]"}
        )

    async def _initial_sync(self):
        await self._liveness_probe()
        await self.refresh_children()

    async def _liveness_probe(self):
        value = await self._ask("Q", r"(?:Ver(?:01)?\*?)?(\d+\.\d+(?:\.[\w-]+)?)")
        self.set_state("firmware", value[1])

    def _label(self, kind, slot, name):
        current = self.get_child_state(kind, slot).get("label")
        if not current or current == self._labels.get((kind, slot)):
            self.set_child_state(kind, slot, "label", name)
            self._labels[kind, slot] = name

    def _roster(self, kind, ids):
        for old in set(self.list_children(kind)).difference(ids):
            self.deregister_child(kind, old)
        for slot in ids:
            self.register_child(kind, slot)

    def _name_pattern(self, tag):
        # A name can be blank. Never let a tagged SIS change, banner, command
        # echo or data from another query stand in for the requested name.
        return rf"(?:{tag})?((?!Rcdr[YDEBPX]\d*$|Encm\d+\*|Rtmp[ES]\d+\*|Strc\d+\*|Nmi\d+,|Pnam\d+\*|In\d+\*|[78]Rpr\d+$|Tke$|Vrb\d+$|Alrm[ *]|RecStart\d+$|Trgr[SX]$|E\d\d$)[^\x00-\x1f]{{0,16}})"

    async def _input_names(self):
        self._roster("input", self._inputs)
        for slot in self._inputs:
            result = await self._ask(
                ESC + f"{slot}NI", self._name_pattern(rf"Nmi0*{slot},")
            )
            self._label("input", slot, result[1].strip() or f"Input {slot}")

    async def _layout_names(self):
        choices = {}
        if self._mode == 0:
            for n in range(1, 17):
                m = await self._ask(
                    ESC + f"7*{n}PNAM", self._name_pattern(rf"Pnam7\*0*{n},")
                )
                choices[n] = m[1].strip() or f"Layout {n}"
        self._layouts = choices
        self.set_state(
            "layout_options",
            json.dumps(
                [
                    {"value": str(n), "label": f"{n} · {name}"}
                    for n, name in choices.items()
                ]
            ),
        )

    def _apply_mode(self, mode):
        if self._mode == mode:
            return
        self._mode = mode
        self.set_state("encoder_mode", ("Composite", "Dual channel")[mode])
        outputs = (1, 3) if mode == 0 else (1, 2, 3)
        self._roster("stream", outputs)
        for slot in outputs:
            self._label("stream", slot, STREAMS[slot])
        if mode:
            self._layouts.clear()
            self.set_states(
                {"layout_options": "[]", "layout": None, "layout_name": None}
            )

    async def _read_mode(self):
        mode = await self._ask(ESC + "1ENCM", r"([01])")
        self._apply_mode(int(mode[1]))

    def _selection(self, channel, chosen):
        bank = (1, 2) if channel == 1 else self._inputs[2:]
        for slot in bank:
            if not self.is_child_registered("input", slot):
                continue
            selected = slot == chosen
            self.set_child_state("input", slot, "selected", selected)
            if selected:
                continue
            self.set_child_state("input", slot, "format", None)
            if slot not in HDMI:
                self.set_child_state("input", slot, "signal", None)

    def _stream_value(self, slot, prop, enabled):
        active = bool(int(enabled))
        if self.is_child_registered("stream", slot):
            self.set_child_state("stream", slot, prop, active)
        if (slot, prop, active) == (3, "enabled", False):
            self._clear_preview()

    def _notice(self, line):
        if match := re.fullmatch(r"RcdrY0?([012])", line, re.I):
            self._record_feedback(int(match[1]))
        elif match := re.fullmatch(r"In0*([1-5])\*0*([12])", line, re.I):
            self._selection(int(match[2]), int(match[1]))
        elif match := re.fullmatch(r"Encm0*1\*0?([01])", line, re.I):
            self._apply_mode(int(match[1]))
        elif match := re.fullmatch(r"([78])Rpr0*(\d+)", line, re.I):
            slot = int(match[2])
            self.set_states({"layout": slot, "layout_name": self._layouts.get(slot)})
        elif match := re.fullmatch(r"(Strc|RtmpE)0*([123])\*0?([01])", line, re.I):
            prop = "enabled" if match[1].lower() == "strc" else "rtmp_enabled"
            self._stream_value(int(match[2]), prop, match[3])
        elif match := re.fullmatch(r"RtmpS([12])\*0*([123])\*([01])", line, re.I):
            self._stream_value(
                int(match[2]),
                ("primary_live", "backup_live")[int(match[1]) - 1],
                match[3],
            )

    async def refresh_children(self):
        async with self._sync:
            await self._input_names()
            await self._read_mode()
            await self._layout_names()
            await self._read_status()

    def _record_feedback(self, state):
        self.set_states({"record_state": RECORD[state], "recording": state == 1})

    async def _read_record(self):
        m = await self._ask(ESC + "YRCDR", r"0?([012])")
        self._record_feedback(int(m[1]))

    async def _read_times(self):
        elapsed = await self._ask("35I", rf"(?:Inf35\*)?({CLOCK})")
        remaining = await self._ask(
            "36I", rf"(?:Inf36\*)?([\w-]+\s+{CLOCK}(?:\s*\*\s*[\w-]+\s+{CLOCK})?)"
        )
        destination = await self._ask(
            "37I",
            r"(?:Inf37\*)?((?:internal|auto|usbfront|usbrear|usbrcp)(?:\s*\*\s*(?:auto|usbfront|usbrear|usbrcp))?)",
        )
        self.set_states(
            {
                "elapsed": elapsed[1],
                "remaining": remaining[1].replace("*", " / "),
                "destination": destination[1].replace("*", " / "),
            }
        )

    async def _read_storage(self):
        m = await self._ask(
            "55I", r"(?:Inf55\*)?internal\*[^*]+\*[^*]+\*[^*]+\*([^*]+)\*[012]"
        )
        estimate = m[1].strip()
        minutes = None
        if re.fullmatch(CLOCK, estimate):
            hours, mins, secs = map(int, estimate.split(":"))
            minutes = round(hours * 60 + mins + secs / 60, 2)
        self.set_states({"internal_time": estimate, "internal_minutes": minutes})

    async def _read_layout(self):
        if self._mode != 0:
            return
        m = await self._ask("49I", r"(?:Inf49\*)?\d+\*.{0,16},(\d+)\*(.*)")
        self.set_states({"layout": int(m[1]), "layout_name": m[2]})

    async def _read_inputs(self):
        chosen = await self._ask("32I", r"(?:Inf32\*)?ChA0*([12])\*ChB0*([345])")
        slots = (int(chosen[1]), int(chosen[2]))
        for channel, slot in enumerate(slots, 1):
            self._selection(channel, slot)
        # 42I sometimes omits the input number for a channel without signal.
        # 32I independently identifies its selected connector.
        part = r"Ch([AB])([0-5]*)\*.{0,16}?\*([^*,]+)\*([^*,]+)\*[01]"
        status = await self._ask("42I", rf"(?:Inf42\*)?({part},{part})")
        for bank, raw, resolution, rate in re.findall(part, status[1], re.I):
            slot = int(raw) if raw else slots[0 if bank.upper() == "A" else 1]
            self._input_format(slot, resolution, rate)
        await self._read_hdmi()

    def _input_format(self, slot, resolution, rate):
        if not self.is_child_registered("input", slot):
            return
        signal = resolution.strip().lower() not in ("n/a", "none", "0x0")
        value = f"{resolution.strip()} @ {rate.strip()} Hz" if signal else None
        self.set_child_state("input", slot, "format", value)
        self.set_child_state("input", slot, "signal", signal)

    async def _read_hdmi(self):
        for slot in HDMI:
            m = await self._ask(ESC + f"I{slot}HDCP", r"([012])")
            self.set_child_state("input", slot, "signal", m[1] != "0")
            self.set_child_state(
                "input",
                slot,
                "hdcp",
                ("No source", "Protected", "Unprotected")[int(m[1])],
            )

    async def _read_stream_value(self, slot, prop, query):
        # Mode 1 answers the query with a bare value. Tagged status changes
        # between the request and its answer still go to their own stream.
        m = await self._ask(ESC + query, r"([01])")
        self._stream_value(slot, prop, m[1])
        return m[1]

    async def _read_streams(self):
        for slot in self.list_children("stream"):
            await self._read_stream_value(slot, "enabled", f"{slot}STRC")
            await self._read_stream_value(slot, "rtmp_enabled", f"E{slot}RTMP")
            for dest, prop in enumerate(("primary_live", "backup_live"), 1):
                await self._read_stream_value(slot, prop, f"S{dest}*{slot}RTMP")

    def _clear_preview(self):
        self.set_states({"preview_url": "", "preview_format": ""})

    async def _read_preview(self):
        method = self.config.get("confidence_preview", "Off")
        if method == "Off" or not self.get_child_state("stream", 3).get("enabled"):
            self._clear_preview()
            return
        # SIS exposes the stream name and enabled flag, but not the pull method.
        # Read the recorder's own LAN IP, including when attached via serial.
        name = (await self._ask(ESC + "N3STRC", self._name_pattern(r"StrcN3\*")))[1]
        address = (await self._ask(ESC + "CI", r"(?:Ipi\s*)?(\d+(?:\.\d+){3})"))[1]
        path = quote(name.strip(), safe="")
        ip = IPv4Address(".".join(str(int(octet)) for octet in address.split(".")))
        if not path or ip.is_unspecified:
            self._clear_preview()
            return
        suffix = "/multicast" if method == "RTSP multicast pull" else ""
        self.set_states(
            {"preview_url": f"rtsp://{ip}/{path}{suffix}", "preview_format": "rtsp"}
        )

    async def _read_status(self):
        await self._read_record()
        await self._read_mode()
        if self._mode == 0 and not self._layouts:
            await self._layout_names()
        await self._read_times()
        await self._read_storage()
        await self._read_inputs()
        await self._read_layout()
        await self._read_streams()
        await self._read_preview()
        alarms = await self._ask("39I", r"(?:Inf39\*)?(None active|\[?name:.*)")
        self.set_states(
            {"alarms": alarms[1], "has_alarm": alarms[1].lower() != "none active"}
        )

    async def poll(self):
        async with self._sync:
            await self._liveness_probe()
            await self._read_status()

    @staticmethod
    def _choice(raw, available, label):
        if str(raw).isdigit() and int(raw) in available:
            return int(raw)
        raise CommandRefusedError(f"Select an available {label} on this recorder.")

    async def _record(self, status):
        await self._ask(ESC + f"Y{status}RCDR", rf"RcdrY0?{status}")
        await self._read_record()
        if self.get_state("record_state") != RECORD[status]:
            raise CommandRefusedError(
                "The recorder reported a different recording state."
            )

    async def _source(self, params):
        n = self._choice(params.get("input"), self._inputs, "input")
        channel = 1 if n <= 2 else 2
        await self._ask(f"{n}*{channel}!", rf"In0*{n}\*0*{channel}")
        await self._read_inputs()
        if not self.get_child_state("input", n).get("selected"):
            raise CommandRefusedError("The recorder did not keep the selected source.")

    async def _layout(self, params, opcode):
        await self._read_mode()
        if self._mode != 0:
            raise CommandRefusedError(
                "Saved recording layouts are available in composite mode."
            )
        n = self._choice(params.get("preset"), range(1, 17), "layout")
        await self._ask(f"{opcode}*{n}.", rf"{opcode}Rpr0*{n}")
        await self._read_layout()
        await self._read_inputs()
        if self.get_state("layout") != n:
            raise CommandRefusedError("The recorder reported a different layout.")

    async def _stream_command(self, params, rtmp):
        await self._read_mode()
        n = self._choice(params.get("stream"), self.list_children("stream"), "stream")
        if not isinstance(params.get("enabled"), bool):
            raise CommandRefusedError("Choose enabled or disabled.")
        enabled = int(params["enabled"])
        lead, tail, tag, prop = (
            ("E", "RTMP", "RtmpE", "rtmp_enabled")
            if rtmp
            else ("", "STRC", "Strc", "enabled")
        )
        prefix = rf"{tag}0*{n}\*"
        await self._ask(ESC + f"{lead}{n}*{enabled}{tail}", prefix + str(enabled))
        actual = await self._read_stream_value(n, prop, f"{lead}{n}{tail}")
        if n == 3 and not rtmp:
            await self._read_preview()
        if int(actual) != enabled:
            raise CommandRefusedError(
                "The recorder reported a different stream setting."
            )

    async def send_command(self, command, params=None):
        params = params or {}
        if command == "refresh_names":
            return await self.refresh_children()
        if command == "read_status":
            return await self.poll()
        calls = {
            "start_recording": (self._record, 1),
            "stop_recording": (self._record, 0),
            "pause_recording": (self._record, 2),
            "add_marker": (self._ask, ESC + "BRCDR", r"RcdrB"),
            "select_input": (self._source, params),
            "recall_layout": (self._layout, params, 8),
            "recall_layout_with_inputs": (self._layout, params, 7),
            "set_stream": (self._stream_command, params, False),
            "set_rtmp": (self._stream_command, params, True),
        }
        if command not in calls:
            raise CommandRefusedError("Unknown recorder command.")
        func, *args = calls[command]
        async with self._sync:
            await func(*args)
