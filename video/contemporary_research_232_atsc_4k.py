"""Contemporary Research 232-ATSC 4K. May 2026 manual, pages 18–24.

Python collects the counted LC reply across frames before replacing the native
picker. Addressed status frames can arrive between unaddressed station rows.
"""

from __future__ import annotations

import asyncio
import json
import re
from time import monotonic

from openavc.drivers.base import BaseDriver, CommandRefusedError

CHANNEL = re.compile(r"([0-9]{1,4})(?:[-:]([0-9]{1,3}))?")
HEADER = re.compile(r"Start of Channel List\s*-\s*(Air|Cable)\s*:\s*([0-9]+)", re.I)
TUNER = re.compile(r"([UM])([0-9]{3})[UMx][0x]([ACx])([0-4Nx])([0-9]{3}|F[0-9]{2})x0")
AUDIO = re.compile(r"[UM][0-9]{2}([UM])x([0-9]{3})")
CAPTIONS = re.compile(r"([01])[0-9x]{8}xx")
RESOLUTIONS = {"0": "1080i", "1": "720p", "2": "480p", "3": "480i", "4": "1080p"}
QUERY = {
    "power": "ST",
    "current_channel": "ST",
    "signal_present": "ST",
    "received_resolution": "ST",
    "rf_source": "ST",
    "volume": "SV",
    "mute": "SV",
    "captions": "SQ",
}
BUTTON = {
    "channel_up": "TU",
    "channel_down": "TD",
    "last_channel": "TP",
    "power_on": "P1",
    "power_off": "P0",
    "mute_on": "VM",
    "mute_off": "VX",
    "captions_on": "Q0=1",
    "captions_off": "Q0=0",
}
OUTPUT = ("1080i", "720p", "480p", "480i", "1080p", "2160p60", "Auto", "2160p30")
OUTPUT_STATUS = re.compile(r"[0-9x]{7,8}([0-57x])([0-7x])xxxx")
QUERY.update(output_resolution="SS", output_setting="SS")


def output_status(payload):
    match = OUTPUT_STATUS.fullmatch(payload)
    if not match:
        return None
    return {
        key: OUTPUT[int(code)] if code.isdigit() else None
        for key, code in zip(("output_resolution", "output_setting"), match.groups())
    }


LINEUP_INTERVAL = 300
LINEUP_IDLE = 5.0


def channel_number(value):
    match = CHANNEL.fullmatch(str(value).strip())
    if not match:
        raise ValueError("Enter a channel number such as 1032, 6-2 or 32-0.")
    major, minor = match.groups()
    if minor is None:
        return str(int(major))
    if len(major) > 3:
        raise ValueError("Two-part channel numbers have up to three digits per part.")
    return f"{int(major)}-{int(minor)}"


def extended_number(value):
    return bool(re.fullmatch(r"[1-6][0-9]{4}", value)) and int(value) < 64000


def requested_number(value):
    number = str(value).strip()
    if extended_number(number):
        return number
    try:
        return channel_number(number)
    except ValueError as exc:
        raise CommandRefusedError(str(exc)) from exc


def channel_status(payload):
    match = TUNER.fullmatch(payload)
    if not match:
        return None
    power, major, source, resolution, minor = match.groups()
    if minor.startswith("F"):
        channel = str(int(major) + 1000 * int(minor[1:]))
    else:
        channel = f"{int(major)}-{int(minor)}"
    live = power == "U"
    return {
        "power": live,
        "current_channel": channel if live else None,
        "rf_source": {"A": "Air", "C": "Cable"}.get(source),
        "received_resolution": RESOLUTIONS.get(resolution) if live else None,
        "signal_present": resolution != "N" if live and resolution != "x" else None,
    }


def audio_status(payload):
    match = AUDIO.fullmatch(payload)
    if match and int(match[2]) <= 100:
        return {"mute": match[1] == "M", "volume": int(match[2])}
    return None


def caption_status(payload):
    match = CAPTIONS.fullmatch(payload)
    return {"captions": match[1] == "1"} if match else None


def listed_channel(kind, value):
    # The tuner can report five-digit one-part channels, although its ASCII
    # TC command only accepts four. Preserve those records and their names.
    if kind.upper() == "D" and extended_number(value):
        return str(int(value))
    channel = channel_number(value)
    analog = kind.upper() == "A" or (not kind and "-" not in value)
    return channel + "-0" if analog and "-" not in channel else channel


def station_row(line):
    row = re.fullmatch(r"([AD]?)([0-9]+(?:-[0-9]+)?):([^:]+)(?::(.*))?", line, re.I)
    if not row:
        raise ValueError("Incomplete station row")
    kind, value, _, name = row.groups()
    channel = listed_channel(kind, value)
    if kind.upper() == "D" and name is None:
        raise ValueError("Missing digital station name column")
    return channel, (name or "").strip()


def volume_command(value):
    if not re.fullmatch(r"[0-9]{1,3}", str(value)):
        raise CommandRefusedError("Volume must be a whole number from 0 to 100.")
    level = int(value)
    if level > 100:
        raise CommandRefusedError("The maximum volume is 100.")
    return f"VH={level}"


class Lineup:
    """One device reply. A repeated virtual channel cannot be a unique choice."""

    def __init__(self, source, count):
        self.source = source
        self.count = count
        self.stations = {}
        self.valid = True

    def add(self, line):
        try:
            number, name = station_row(line)
        except ValueError:
            self.valid = False
            return
        if number in self.stations or len(self.stations) >= self.count:
            self.valid = False
            return
        self.stations[number] = name

    def complete(self):
        return self.valid and len(self.stations) == self.count


class ContemporaryResearchTuner(BaseDriver):
    DRIVER_INFO = {
        "id": "contemporary_research_232_atsc_4k",
        "name": "Contemporary Research 232-ATSC 4K",
        "manufacturer": "Contemporary Research",
        "category": "video",
        "version": "1.0.0",
        "author": "OpenAVC",
        "min_platform_version": "0.37.1",
        "description": "Control a broadcast or cable TV tuner from its own channel memory. Select named stations, "
        "step through channels, and monitor reception, received resolution, audio and captions.",
        "source_url": "https://bucketeer-3c238a6b-adc2-4051-aeb1-2d2ab9e1e1b4.s3.amazonaws.com/documents/232-ATSC_4K_Product_Manual_052026.pdf",
        "verified": False,
        "simulated": True,
        "compatible_models": [
            {
                "manufacturer": "Contemporary Research",
                "models": ["232-ATSC 4K"],
                "confidence": "untested",
                "notes": "Part 5114-001. Based on the May 2026 product manual, pages 18–24. "
                "Simulator tested.",
            }
        ],
        "tags": ["hdtv-tuner", "atsc", "qam", "cable-tv", "rs232"],
        "protocols": ["cr-232-atsc"],
        "ports": [23],
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "web_ui": "http://{host}",
        "discovery": {
            "manufacturer_alias": ["contemporary research"],
            "tcp_probe": {
                "port": 23,
                "send_ascii": ">ID\r",
                "expect_regex": "(?i)\\b232-ATSC[ -]+4K\\b",
                "extract_manufacturer": "Contemporary Research",
                "extract": {"model": {"regex": "(?i)\\b(232-ATSC[ -]+4K)\\b"}},
                "timeout_ms": 2500,
            },
        },
        "help": {
            "overview": "Pick a channel by its number and station name. Channel, station and reception "
            "reflect tuner reports. A blank signal reading means reception is unknown. A "
            "confirmed loss of reception shows Signal Present as false. Signal strength in dB or "
            "percent is not reported by the documented control protocol. Device Settings lets "
            "the installer select the video output resolution and check the actual output "
            "independently of the received broadcast format.",
            "setup": "Scan or edit the channel memory on the tuner first. Use Refresh Channel List after "
            "edits. A completed empty scan produces an empty picker. Five-digit one-part stations "
            "use the documented remote-control digit keys and Enter. The driver verifies the tuned "
            "number. Direct tuning accepts a one-part number, major-minor for digital subchannels, "
            "or major-0 for analog.",
            "connection": "Enter the tuner address and its Telnet port (default 23), or select a dedicated "
            "RS-232 connection (default 9600 baud, 8N1). Match the unit number to the tuner. "
            "Channel lists are unaddressed on the wire, so use a dedicated link for "
            "enumeration. On each reconnect the driver clears cached feedback and rereads "
            "tuner status and channel memory.",
        },
        "default_config": {
            "host": "",
            "port": 23,
            "baudrate": 9600,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "unit_number": 1,
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {"type": "string", "label": "Tuner Address", "required": True},
            "port": {
                "type": "integer",
                "label": "Telnet Port",
                "default": 23,
                "min": 1,
                "max": 65535,
            },
            "baudrate": {
                "type": "integer",
                "label": "RS-232 Baud Rate",
                "default": 9600,
            },
            "unit_number": {
                "type": "integer",
                "label": "Tuner Unit Number",
                "default": 1,
                "min": 1,
                "max": 9,
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status Refresh Interval (seconds)",
                "default": 10,
                "min": 2,
                "max": 300,
                "advanced": True,
            },
        },
        "state_variables": {
            "power": {
                "type": "boolean",
                "label": "Power On",
                "control": True,
                "cloud_priority": "high",
            },
            "current_channel": {
                "type": "string",
                "label": "Tuned Channel",
                "control": True,
                "cloud_priority": "high",
            },
            "station_name": {"type": "string", "label": "Station Name"},
            "signal_present": {
                "type": "boolean",
                "label": "Signal Present",
                "cloud_priority": "high",
            },
            "received_resolution": {"type": "string", "label": "Received Resolution"},
            "rf_source": {"type": "string", "label": "RF Source"},
            "volume": {
                "type": "integer",
                "label": "Audio Volume",
                "control": True,
                "min": 0,
                "max": 100,
                "unit": "%",
            },
            "mute": {"type": "boolean", "label": "Audio Muted", "control": True},
            "captions": {
                "type": "boolean",
                "label": "Closed Captions On",
                "control": True,
            },
            "channel_options": {
                "type": "string",
                "label": "Channel Choices",
                "cloud_priority": "low",
            },
            "lineup_ready": {"type": "boolean", "label": "Channel List Available"},
            "channel_count": {"type": "integer", "label": "Channels in Memory"},
            "lineup_source": {"type": "string", "label": "Channel Memory Source"},
            "output_resolution": {
                "type": "string",
                "label": "Actual Video Output Resolution",
            },
            "output_setting": {
                "type": "enum",
                "label": "Video Output Setting",
                "values": [
                    "1080i",
                    "720p",
                    "480p",
                    "480i",
                    "1080p",
                    "2160p60",
                    "Auto",
                    "2160p30",
                ],
            },
        },
        "commands": {
            "select_channel": {
                "label": "Select Channel",
                "params": {
                    "channel": {
                        "type": "string",
                        "label": "Channel",
                        "required": True,
                        "options_state": "channel_options",
                    }
                },
                "sets": {"current_channel": "{channel}"},
            },
            "tune_channel_number": {
                "label": "Tune by Number",
                "params": {
                    "channel": {
                        "type": "string",
                        "label": "Channel Number",
                        "required": True,
                        "help": "For example 6-2, 1032, or 32-0 for analog.",
                    }
                },
                "help": "A bare number prefers digital, falling back to analog. The "
                "reported channel shows what the tuner actually selected.",
            },
            "channel_up": {"label": "Next Channel"},
            "channel_down": {"label": "Previous Channel in List"},
            "last_channel": {"label": "Recall Last Channel"},
            "power_on": {"label": "Power On", "sets": {"power": True}},
            "power_off": {"label": "Standby", "sets": {"power": False}},
            "mute_on": {"label": "Mute Audio", "sets": {"mute": True}},
            "mute_off": {"label": "Unmute Audio", "sets": {"mute": False}},
            "captions_on": {
                "label": "Enable Closed Captions",
                "sets": {"captions": True},
            },
            "captions_off": {
                "label": "Disable Closed Captions",
                "sets": {"captions": False},
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "level": {
                        "type": "integer",
                        "label": "Volume",
                        "min": 0,
                        "max": 100,
                        "required": True,
                    }
                },
                "sets": {"volume": "{level}"},
            },
            "read_current_channel": {
                "label": "Read Tuned Channel",
                "query_for": "current_channel",
            },
            "read_signal_present": {
                "label": "Read Signal Present",
                "query_for": "signal_present",
            },
            "read_received_resolution": {
                "label": "Read Received Resolution",
                "query_for": "received_resolution",
            },
            "read_rf_source": {"label": "Read RF Source", "query_for": "rf_source"},
            "read_power": {"label": "Read Power On", "query_for": "power"},
            "read_volume": {"label": "Read Audio Volume", "query_for": "volume"},
            "read_mute": {"label": "Read Audio Muted", "query_for": "mute"},
            "read_captions": {
                "label": "Read Closed Captions On",
                "query_for": "captions",
            },
            "refresh_lineup": {
                "label": "Refresh Channel List",
                "query_for": "channel_count",
            },
            "read_output_resolution": {
                "label": "Read Video Output Resolution",
                "query_for": "output_resolution",
            },
        },
        "actions": [
            {"id": "select_channel", "icon": "list"},
            {"id": "channel_up", "icon": "chevron-up"},
            {"id": "channel_down", "icon": "chevron-down"},
            {"id": "last_channel", "icon": "undo-2"},
            {"id": "set_volume", "icon": "volume-2"},
            {"id": "mute_on", "icon": "volume-x"},
            {"id": "mute_off", "icon": "volume-2"},
            {"id": "power_on", "icon": "power"},
            {"id": "power_off", "icon": "power"},
            {"id": "refresh_lineup", "icon": "refresh-cw"},
        ],
        "quick_actions": [
            "select_channel",
            "channel_up",
            "channel_down",
            "last_channel",
        ],
        "device_settings": {
            "output_setting": {
                "type": "enum",
                "label": "Video Output Resolution",
                "values": [
                    "1080i",
                    "720p",
                    "480p",
                    "480i",
                    "1080p",
                    "2160p60",
                    "Auto",
                    "2160p30",
                ],
                "state_key": "output_setting",
                "help": "Auto follows the display. Actual Video Output Resolution "
                "shows the format the tuner is currently outputting.",
            }
        },
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._unit = int(self.config.get("unit_number", 1))
        if not 1 <= self._unit <= 9:
            raise ValueError("Tuner unit number must be between 1 and 9.")
        self._control_lock = asyncio.Lock()
        self._stations = {}
        self._block = None
        self._lineup_done = None
        self._last_row = 0.0
        self._next_lineup = 0.0
        self._status_waiters = {}

    def _transport_kwargs(self, transport_type, kwargs):
        kwargs.update(delimiter=b"\n", frame_parser=None)
        return kwargs

    def _clear_lineup(self):
        self._stations = {}
        self.set_states(
            {
                "channel_options": "[]",
                "lineup_ready": False,
                "channel_count": None,
                "lineup_source": None,
                "station_name": None,
            }
        )

    async def _close_session(self):
        self._block = None
        self._next_lineup = 0.0
        self._clear_lineup()
        self.set_states(dict.fromkeys(QUERY))
        if self._lineup_done and not self._lineup_done.done():
            self._lineup_done.set_result(False)
        self._lineup_done = None
        for waiters in self._status_waiters.values():
            for waiter in waiters:
                waiter.set_result(False)
        self._status_waiters.clear()
        await super()._close_session()

    async def _write(self, command):
        if not self.transport or not self.transport.connected:
            raise ConnectionError("The tuner is disconnected.")
        await self.transport.send(f">{self._unit}{command}\r".encode("ascii"))

    async def _read(self, command):
        # Concurrent requests for the same status can share the next complete report.
        waiter = asyncio.get_running_loop().create_future()
        waiters = self._status_waiters.setdefault(command, set())
        waiters.add(waiter)
        try:
            await self._write(command)
            if not await asyncio.wait_for(asyncio.shield(waiter), 3):
                raise ConnectionError(
                    "The tuner connection closed during a status read."
                )
        finally:
            waiters.discard(waiter)

    async def _post_connect(self):
        try:
            await self._read("ST")
        except TimeoutError as exc:
            raise ConnectionError(
                "The tuner did not return channel status. Check its address, unit number and port."
            ) from exc

    async def _initial_sync(self):
        await self.poll()

    async def _liveness_probe(self):
        await self._read("ST")

    async def poll(self):
        for code in ("ST", "SV", "SQ", "SS"):
            await self._write(code)
        if monotonic() >= self._next_lineup:
            await self._read_lineup()

    async def _read_lineup(self):
        if self._lineup_done is not None:
            return await asyncio.shield(self._lineup_done)
        done = asyncio.get_running_loop().create_future()
        self._lineup_done = done
        self._block = None
        self._last_row = monotonic()
        try:
            await self._write("LC")
            ok = await self._await_lineup_end(done)
            if not ok and self._lineup_done is done:
                self._clear_lineup()
            return ok
        finally:
            if not done.done():
                done.set_result(False)
            if self._lineup_done is done:
                self._next_lineup = monotonic() + (
                    LINEUP_INTERVAL if done.result() else 5
                )
                self._lineup_done = self._block = None

    async def _await_lineup_end(self, done):
        while not done.done():
            remaining = self._last_row + LINEUP_IDLE - monotonic()
            if remaining <= 0:
                return False
            await asyncio.wait({done}, timeout=remaining)
        return done.result()

    def on_data_received(self, data):
        line = data.decode("utf-8", errors="replace").strip()
        # A serial port with echo enabled can put an echoed command and its
        # first reply in the same LF-delimited frame. Keep the actual reply.
        for part in line.split("\r"):
            self._line(part.strip())

    def _line(self, line):
        if line.startswith(">"):
            return
        if line.startswith("<"):
            self._status(line)
        elif line and self._lineup_done is not None:
            self._lineup_line(line)

    def _status(self, line):
        if not line.startswith(f"<{self._unit}") or len(line) < 4:
            return
        kind = line[2]
        parser = {
            "T": channel_status,
            "V": audio_status,
            "Q": caption_status,
            "S": output_status,
        }.get(kind)
        updates = parser(line[3:]) if parser else None
        if updates is None:
            return
        if kind == "T":
            self._tuned_station(updates)
        self.set_states(updates)
        for waiter in self._status_waiters.pop("S" + kind, ()):
            waiter.set_result(True)

    def _tuned_station(self, updates):
        source = updates["rf_source"]
        if source and self.get_state("lineup_source") not in (None, source):
            self._clear_lineup()
            self._next_lineup = 0.0
        updates["station_name"] = self._stations.get(updates["current_channel"]) or None

    def _lineup_line(self, line):
        header = HEADER.fullmatch(line)
        if header:
            self._block = Lineup(header[1].title(), int(header[2]))
            self._last_row = monotonic()
        elif self._block is not None:
            self._last_row = monotonic()
            if line.casefold() == "end of channel list":
                self._finish_lineup()
            else:
                self._block.add(line)

    def _finish_lineup(self):
        block, self._block = self._block, None
        ok = block.complete() and self.get_state("rf_source") in (None, block.source)
        if ok:
            self._stations = block.stations
            choices = [
                {"value": n, "label": f"{n} · {name}" if name else n}
                for n, name in block.stations.items()
            ]
            self.set_states(
                {
                    "channel_options": json.dumps(choices, ensure_ascii=False),
                    "lineup_ready": True,
                    "channel_count": block.count,
                    "lineup_source": block.source,
                    "station_name": self._stations.get(
                        self.get_state("current_channel")
                    )
                    or None,
                }
            )
        else:
            self._clear_lineup()
        if not self._lineup_done.done():
            self._lineup_done.set_result(ok)

    async def send_command(self, command, params=None):
        params = params or {}
        if command == "refresh_lineup":
            if not await self._read_lineup():
                raise CommandRefusedError(
                    "The tuner did not return a complete channel list. Refresh to retry."
                )
            return True
        if command.startswith("read_") and command[5:] in QUERY:
            await self._read(QUERY[command[5:]])
            return True
        await self._send_control(command, params)
        return True

    async def _send_control(self, command, params):
        async with self._control_lock:
            if command in ("select_channel", "tune_channel_number"):
                await self._tune(params.get("channel", ""), command == "select_channel")
            else:
                await self._write(self._control(command, params))

    def _control(self, command, params):
        if command in BUTTON:
            return BUTTON[command]
        if command == "set_volume":
            return volume_command(params.get("level"))
        raise ValueError(f"Unknown tuner command: {command}")

    async def _tune(self, value, from_picker):
        number = requested_number(value)
        if from_picker and number not in self._stations:
            raise CommandRefusedError(
                "This channel is absent from the tuner's confirmed list. Refresh the list."
            )
        if len(number) <= 4 or "-" in number:
            await self._write(f"TC={number}")
            return
        # TC is limited to four digits. Pages 11 and 22 document one-part
        # direct entry, keys 11..19, and 21 for Enter. The same-model
        # April 2021 manual, page 19, supplies the omitted 10 (zero).
        for digit in number:
            await self._write(f"KK={10 + int(digit)}")
        await self._write("KK=21")
        await self._confirm(
            "ST",
            "current_channel",
            number,
            "The tuner did not confirm the five-digit channel.",
        )

    async def set_device_setting(self, key, value):
        if key != "output_setting" or value not in OUTPUT:
            raise CommandRefusedError("Select a supported video output resolution.")
        async with self._control_lock:
            await self._write(f"KK={141 + OUTPUT.index(value)}")
            await self._confirm(
                "SS",
                "output_setting",
                value,
                "The tuner did not confirm the selected output resolution.",
            )
        return True

    async def _confirm(self, query, key, value, refusal):
        # Batched reports have no request IDs. An earlier SS can arrive after
        # a new resolution change, so wait for a report of the requested setting.
        deadline = monotonic() + 3
        while monotonic() < deadline:
            await self._read(query)
            if self.get_state(key) == value:
                return
            await asyncio.sleep(0.15)
        raise CommandRefusedError(refusal)
