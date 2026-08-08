"""
OpenAVC Shure Networked Audio Driver.

Controls Shure networked microphones, interfaces, and processors over the
Shure Device Control Strings (DCS) protocol on TCP port 2202. Covers the
MXA910 / MXA920 / MXA710 Microflex ceiling arrays, the ANI networked audio
interfaces, the P300 IntelliMix processor, and the SCM820 automixer.

Protocol summary (all models share one grammar):
    Command : ``< [GET|SET] [index] PROPERTY [value] >``
    Reply   : ``< REP [index] PROPERTY value >``  (``< REP ERR >`` on error)
    - The angle brackets frame every message; ``>`` is the delimiter. Spaces
      separate fields. ``index`` is the channel; index 0 addresses all
      channels and, on a GET, fans one query out into one REP per channel.
    - String values (device / channel names, firmware) are brace-wrapped and
      space-padded to a fixed width (``{Podium Mic}``); the parser strips both.
    - The device sends an unsolicited REP whenever a value changes — from any
      controller, its own web UI, or the front panel — so control is
      push-driven: the driver seeds state on connect and then mirrors pushes.

Per-channel state is modelled with child entities. Every covered model
exposes the same per-channel surface through the shared grammar — mute
(``AUDIO_MUTE``), digital gain (``AUDIO_GAIN_HI_RES``), channel name
(``CHAN_NAME``, read-only), and live metering (``METER_RATE`` subscription →
``SAMPLE`` frames) — even though the channels mean different things per model
(coverage areas / lobes on the MXA arrays, inputs and mixes on the SCM820,
the P300's input/output matrix, the ANI interface ports). Channel counts and
meanings are model-dependent and the protocol has no channel-enumeration
query — a ``GET 0`` fan-out returns a count that varies with the device's
operating mode — so the roster is sized from a ``channel_count`` config field
(the samsung_mdc / atlasied config-sized-roster pattern), and the child
labels are seeded from the device's own ``CHAN_NAME`` reports.

Python (not YAML) because two things the YAML runtime can't express carry
their weight here: the gain child prop is a real dB value translated to and
from the device's 0-1400 wire scale (arithmetic YAML has no way to do), and
one ``SAMPLE`` frame fans a whole row of level values out into N per-channel
meter props (a one-line-to-many-keys fan-out YAML's per-line dispatch can't
do). Device-level mute / preset / LED / firmware stay flat state on the
device itself.

Source: https://pubs.shure.com/command-strings/  (per-model command-strings
references — MXA920, MXA910, P300, SCM820, ANI4IN cross-read; the pubs site
renders client-side, so the wire formats were read from the rendered pages).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)


DEFAULT_CHANNEL_COUNT = 8

# Digital gain (AUDIO_GAIN_HI_RES) is carried on the wire as an integer 0-1400
# that maps linearly to -110.0 .. +30.0 dB in 0.1 dB steps (1100 = 0 dB).
_GAIN_WIRE_MIN = 0
_GAIN_WIRE_MAX = 1400
_GAIN_DB_MIN = -110.0
_GAIN_DB_MAX = 30.0

# LED brightness: 0 = disabled, then 1-5 = 20/40/60/80/100% on the MXA920 and
# MXA910 (fw > v3.0). Older MXA910 / P300 / ANI firmware only honor 0-2; those
# devices answer a benign ``REP ERR`` to 3-5, so the widest documented range
# is the right bound (a low-tier device rejects the excess itself).
_LED_BRIGHTNESS_MAX = 5

# A meter SAMPLE value is 000-060, representing -60 .. 0 dBFS.
_METER_DBFS_OFFSET = 60


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _db_to_wire(db: float) -> int:
    """dB (-110..+30) -> wire units (0..1400), clamped."""
    wire = round((_clamp(db, _GAIN_DB_MIN, _GAIN_DB_MAX) + 110.0) * 10.0)
    return int(_clamp(wire, _GAIN_WIRE_MIN, _GAIN_WIRE_MAX))


def _wire_to_db(wire: int) -> float:
    """Wire units (0..1400) -> dB (-110..+30), rounded to 0.1 dB."""
    return round(wire / 10.0 - 110.0, 1)


def _strip_string_value(raw: str) -> str:
    """Unwrap a brace-wrapped, space-padded DCS string value.

    Names arrive as ``{Podium Mic}`` padded to a fixed width; firmware as
    ``{4.6.11}``. Take the brace contents when present, else the raw token,
    and trim the padding.
    """
    raw = raw.strip()
    m = re.search(r"\{(.*)\}", raw)
    if m:
        return m.group(1).strip()
    return raw.strip()


# ── Child entity type ────────────────────────────────────────────────────────
#
# One `channel` type covers every model's per-channel surface. Mute relays at
# high cloud priority (a hot-mic'd or muted podium is operationally
# significant); gain, name, and the continuous meter are low. Local ids are
# 1-based to match the device's own channel numbering (channel 0 is the
# "all channels" broadcast/query index, not an addressable entity).

CHILD_ENTITY_TYPES: dict[str, dict[str, Any]] = {
    "channel": {
        "label": "Channel",
        "label_plural": "Channels",
        "id_format": {"type": "integer", "min": 1, "pad_width": 2},
        "state_variables": {
            "mute": {
                "type": "boolean",
                "label": "Mute",
                "cloud_priority": "high",
                "control": True,
            },
            "gain_db": {
                "type": "number",
                "label": "Gain",
                "min": _GAIN_DB_MIN,
                "max": _GAIN_DB_MAX,
                "step": 0.1,
                "unit": "dB",
                "cloud_priority": "low",
                "control": True,
            },
            # CHAN_NAME is GET/REP only — the device name is set in Shure
            # Designer, not over third-party control — so no control flag.
            "name": {
                "type": "string",
                "label": "Name",
                "cloud_priority": "low",
            },
            # Live level from the SAMPLE stream; only populated while metering
            # is enabled (meter_interval_ms > 0). Read-only telemetry.
            "meter": {
                "type": "number",
                "label": "Level",
                "min": -60,
                "max": 0,
                "unit": "dBFS",
                "cloud_priority": "low",
            },
        },
        "summary_fields": ["mute", "gain_db"],
        "label_field": "name",
    },
}


_LED_COLORS = [
    "RED", "ORANGE", "GOLD", "YELLOW", "YELLOWGREEN", "GREEN", "TURQUOISE",
    "POWDERBLUE", "CYAN", "SKYBLUE", "BLUE", "PURPLE", "LIGHTPURPLE",
    "VIOLET", "ORCHID", "PINK", "WHITE",
]


def _channel_param() -> dict[str, Any]:
    return {"type": "child_id", "child_type": "channel", "required": True,
            "label": "Channel"}


class ShureNetworkDriver(BaseDriver):
    """Shure networked audio (MXA / ANI / P300 / SCM820) driver."""

    DRIVER_INFO = {
        "id": "shure_network",
        "name": "Shure Networked Devices",
        "manufacturer": "Shure",
        "category": "audio",
        "version": "2.0.2",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls Shure networked audio devices over the Device Control "
            "Strings protocol on TCP port 2202 — MXA910 / MXA920 / MXA710 "
            "ceiling arrays, ANI interfaces, the P300 IntelliMix processor, "
            "and the SCM820 automixer. Each audio channel is a child entity "
            "with mute, gain, name, and live metering; device-level mute, "
            "presets, and status LED are controlled directly. Channel state "
            "pushes live as it changes on the device or another controller."
        ),
        "source_url": "https://pubs.shure.com/command-strings/",
        "tags": ["ceiling-mic", "microphone", "automixer", "intellimix", "dsp"],
        "verified": False,
        "simulated": True,
        "protocols": ["shure_dcs"],
        "ports": [2202],
        "transport": "tcp",
        "discovery": {
            # Shure Web Device Discovery / Designer enumerate devices over
            # standard mDNS (`_http._tcp`) plus hostname/TXT filters — there
            # is no registered `_shure._tcp` service type. We do an active
            # DCS query on TCP/2202 (DEVICE_ID answers on every covered model)
            # plus OUI / hostname / manufacturer-alias hints.
            #   Common IP ports: content-files.shure.com/FileRepository/common-ip-ports-v2.pdf
            "tcp_probe": {
                "port": 2202,
                "send_ascii": "< GET DEVICE_ID >\r\n",
                "expect_regex": r"<\s*REP\s+DEVICE_ID",
                "extract_manufacturer": "Shure",
                "extract": {
                    "device_name": {
                        "regex": r"<\s*REP\s+DEVICE_ID\s+\{?\s*([^{}>]+?)\s*\}?\s*>",
                    },
                },
            },
            "oui": [
                "00:0e:dd",   # Shure Incorporated (legacy MA-L)
                "d8:34:ee",   # Shure Incorporated (current MXA / AD blocks)
            ],
            "hostname": ["^MXA", "^ANI", "^AD4", "^ULXD", "^MXW", "^P300", "^IMX"],
            "port_open": [2202],
            "manufacturer_alias": ["shure", "shure incorporated"],
        },
        "compatible_models": [
            {
                "manufacturer": "Shure",
                "models": [
                    "MXA910", "MXA920", "MXA710",
                    "ANI22", "ANI4IN", "ANI4OUT",
                    "P300 IntelliMix", "SCM820",
                ],
                "confidence": "untested",
                "notes": (
                    "All speak the same Device Control Strings grammar. "
                    "Channel counts differ by model — set Channel Count to "
                    "match: MXA arrays have 8 mic channels plus the automixer "
                    "output (9), the ANI4IN/4OUT have 4, the SCM820 has 8 "
                    "inputs plus mixes, and the P300 has a large input/output "
                    "matrix. Device mute and presets are MXA / P300 / ANI; "
                    "the SCM820 mutes per channel and has no presets — "
                    "unsupported properties answer a benign REP ERR."
                ),
            }
        ],
        "help": {
            "overview": (
                "Shure networked microphones, interfaces, and processors "
                "speak the Device Control Strings (DCS) protocol on TCP port "
                "2202. This driver models each audio channel as a child "
                "entity carrying its mute, gain (in dB), name, and live "
                "level, and controls device-level mute, presets, and the "
                "status LED directly.\n\n"
                "State is push-driven: after connecting, the driver seeds "
                "every channel from the device, then mirrors the REP messages "
                "the device sends whenever anything changes — including "
                "changes made on the front panel or in Shure Designer.\n\n"
                "Channel names are read-only over third-party control — set "
                "them in Shure Designer, and they appear here as the channel "
                "labels."
            ),
            "setup": (
                "1. In Shure Designer (or the device web UI), the third-party "
                "control interface on TCP 2202 is enabled by default.\n"
                "2. Give the device a static IP or a DHCP reservation.\n"
                "3. In OpenAVC, enter the IP. Port 2202 is fixed.\n"
                "4. Set Channel Count to match the device (e.g. 9 for an MXA "
                "array's 8 lobes plus the automixer output, 4 for an ANI4IN). "
                "Over-sizing is harmless — extra channels just answer REP "
                "ERR — but clutters the channel list.\n"
                "5. To see live level meters, set Meter Interval to a value "
                "like 500 ms (0 disables metering)."
            ),
        },
        "default_config": {
            "host": "",
            "port": 2202,
            "channel_count": DEFAULT_CHANNEL_COUNT,
            "poll_interval": 10,
            "meter_interval_ms": 0,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 2202,
                "label": "Port",
                "description": "Shure DCS control port — always 2202.",
            },
            "channel_count": {
                "type": "integer",
                "default": DEFAULT_CHANNEL_COUNT,
                "min": 0,
                "label": "Channel Count",
                "description": (
                    "Number of audio channels to model as children. Set to "
                    "match the device: MXA arrays 9 (8 lobes + automixer), "
                    "ANI4IN/4OUT 4, SCM820 8. There is no enumeration query, "
                    "so this is configured, not discovered."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 10,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often to refresh device-level state. Channel state "
                    "is push-driven, so this can be gentle."
                ),
            },
            "meter_interval_ms": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "label": "Meter Interval (ms)",
                "description": (
                    "Level-meter update rate in milliseconds (0 = off, valid "
                    "range 100-99999). Metering streams SAMPLE frames "
                    "continuously, so leave it off unless a panel shows live "
                    "levels."
                ),
            },
        },
        "state_variables": {
            "device_name": {
                "type": "string",
                "label": "Device Name",
                "help": "The device's friendly name (the DEVICE_ID property).",
            },
            "mute": {
                "type": "boolean",
                "label": "Device Mute",
                "help": (
                    "Device-level mute (DEVICE_AUDIO_MUTE). MXA / P300 / ANI; "
                    "the SCM820 mutes per channel instead."
                ),
            },
            "audio_mute": {
                "type": "boolean",
                "label": "Audio Mute",
                "help": "Mirror of Device Mute, for templates that bind audio_mute.",
            },
            "led_brightness": {
                "type": "integer",
                "label": "LED Brightness",
                "help": (
                    "Status LED brightness: 0 = off; 1-5 = 20/40/60/80/100% "
                    "(MXA920 / MXA910 fw > 3.0). Older MXA910 / P300 / ANI "
                    "only honor 0-2. Not reported by every model."
                ),
            },
            "firmware": {
                "type": "string",
                "label": "Firmware Version",
                "help": "Firmware version reported by the device (FW_VER).",
            },
        },
        "child_entity_types": CHILD_ENTITY_TYPES,
        "device_settings": {
            "led_brightness": {
                "type": "integer",
                "label": "LED Brightness",
                "help": (
                    "Status LED brightness: 0 = off, 1-5 = dim..full "
                    "(models vary; older firmware caps at 2)."
                ),
                "state_key": "led_brightness",
                "default": 2,
                "min": 0,
                "max": _LED_BRIGHTNESS_MAX,
                "setup": False,
            },
        },
        "commands": {
            "mute_on": {"label": "Mute"},
            "mute_off": {"label": "Unmute"},
            "mute_toggle": {"label": "Toggle Mute"},
            "set_channel_mute": {
                "label": "Set Channel Mute",
                "params": {
                    "channel": _channel_param(),
                    "mute": {"type": "boolean", "required": True},
                },
                "help": "Mute or unmute one channel.",
            },
            "toggle_channel_mute": {
                "label": "Toggle Channel Mute",
                "params": {"channel": _channel_param()},
            },
            "set_channel_gain": {
                "label": "Set Channel Gain",
                "params": {
                    "channel": _channel_param(),
                    "gain_db": {
                        "type": "number",
                        "required": True,
                        "min": _GAIN_DB_MIN,
                        "max": _GAIN_DB_MAX,
                        "help": "Digital gain in dB (-110 to +30).",
                    },
                },
            },
            "step_channel_gain": {
                "label": "Nudge Channel Gain",
                "params": {
                    "channel": _channel_param(),
                    "delta_db": {
                        "type": "number",
                        "required": True,
                        "help": "Gain change in dB (positive or negative).",
                    },
                },
                "help": "Adjust a channel's gain by a relative amount.",
            },
            "set_led_brightness": {
                "label": "Set LED Brightness",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": _LED_BRIGHTNESS_MAX,
                        "label": "Brightness (0=off .. 5=full)",
                    },
                },
            },
            "set_led_state": {
                "label": "Set LED State",
                "params": {
                    "which": {
                        "type": "enum",
                        "values": ["MUTED", "UNMUTED"],
                        "required": True,
                        "label": "Mute State to Configure",
                    },
                    "state": {
                        "type": "enum",
                        "values": ["ON", "OFF", "FLASHING"],
                        "required": True,
                        "label": "LED State",
                    },
                },
                "help": "Configure the LED look for the muted or unmuted state.",
            },
            "set_led_color": {
                "label": "Set LED Color",
                "params": {
                    "which": {
                        "type": "enum",
                        "values": ["MUTED", "UNMUTED"],
                        "required": True,
                        "label": "Mute State to Configure",
                    },
                    "color": {
                        "type": "enum",
                        "values": list(_LED_COLORS),
                        "required": True,
                        "label": "LED Color",
                    },
                },
            },
            "flash_on": {"label": "Flash LEDs (Identify)"},
            "flash_off": {"label": "Stop Flashing"},
            "recall_preset": {
                "label": "Recall Preset",
                "params": {
                    "preset": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "max": 10,
                        "label": "Preset Number",
                        "description": (
                            "Presets 1-10 (MXA, P300, ANI; the SCM820 has "
                            "no presets)."
                        ),
                    },
                },
            },
            "raw_command": {
                "label": "Raw Command",
                "params": {
                    "command": {
                        "type": "string",
                        "required": True,
                        "label": "DCS Command",
                        "help": "A raw DCS command, e.g. < GET SERIAL_NUM >.",
                    },
                },
                "help": (
                    "Send a raw Device Control Strings command for surfaces "
                    "this driver does not model (coverage-area / post-gate "
                    "controls, per-channel solo, P300 output channels)."
                ),
            },
        },
        "quick_actions": ["mute_on", "mute_off", "flash_on", "recall_preset"],
        "actions": [
            {"id": "mute_on", "kind": "command", "icon": "mic-off"},
            {"id": "mute_off", "kind": "command", "icon": "mic"},
            {"id": "flash_on", "kind": "command", "icon": "sun"},
            {"id": "recall_preset", "kind": "command", "icon": "bookmark"},
        ],
    }

    # The device pushes REP lines only for changes made elsewhere, so an idle,
    # untouched system exchanges no traffic and a silently dropped link (no
    # FIN) is invisible until the next command fails. Probe DEVICE_ID (present
    # on every covered model) as a heartbeat — a mute push or a meter sample
    # can't satisfy a DEVICE_ID probe, so it truly proves the link.
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the device stopped answering DEVICE_ID probes."
    )

    # ── Response patterns ──
    _RE_DEVICE_ID = re.compile(r"^REP\s+DEVICE_ID\s+(.+?)\s*$")
    _RE_DEVICE_MUTE = re.compile(r"^REP\s+DEVICE_AUDIO_MUTE\s+(ON|OFF)\b", re.I)
    _RE_FW = re.compile(r"^REP\s+FW_VER\s+(.+?)\s*$")
    _RE_LED_BRIGHT = re.compile(r"^REP\s+LED_BRIGHTNESS\s+(\d)")
    _RE_CH_MUTE = re.compile(r"^REP\s+(\d+)\s+AUDIO_MUTE\s+(ON|OFF)\b", re.I)
    _RE_CH_NAME = re.compile(r"^REP\s+(\d+)\s+CHAN_NAME\s+(.+?)\s*$")
    _RE_CH_GAIN = re.compile(r"^REP\s+(\d+)\s+AUDIO_GAIN_HI_RES\s+(-?\d+)\b")
    _RE_SAMPLE = re.compile(r"^SAMPLE(?:_IN|_OUT)?\s+(.+?)\s*$")

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        # Resolved when a DEVICE_ID REP arrives (see _liveness_probe).
        self._probe_fut = None
        self._channel_count = max(0, int(config.get("channel_count",
                                                     DEFAULT_CHANNEL_COUNT)))
        self._meter_ms = max(0, int(config.get("meter_interval_ms", 0)))
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        if not self.config.get("host", ""):
            raise ConnectionError(f"[{self.device_id}] Shure host is required")

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # Every DCS reply ends at its closing ">" — frame on that instead
        # of the platform's CR default.
        kwargs["delimiter"] = b">"
        return kwargs

    async def _initial_sync(self) -> None:
        # Seed the roster and every device / channel value; channels stay
        # fresh via push from here on (polling only refreshes device-level
        # state).
        self._register_topology()
        await self._sync_all()

    async def _close_session(self) -> None:
        # Runs on every teardown path: unblock a heartbeat probe still
        # waiting on a dead link.
        fut = self._probe_fut
        if fut is not None and not fut.done():
            fut.set_exception(ConnectionError("connection closed"))
        self._probe_fut = None

    # ── Topology ──

    def _register_topology(self) -> None:
        """Register the config-sized channel roster. Idempotent, and a
        driver-seeded placeholder label never overrides one the user set in
        the project; the device's CHAN_NAME then displays via label_field."""
        for n in range(1, self._channel_count + 1):
            initial = None
            padded = f"{n:02d}"
            project = self._project_child_entities.get("channel", {}).get(padded)
            if not (project and project.get("label")):
                initial = {"label": f"Channel {n}"}
            self.register_child("channel", n, initial_state=initial)
        # Reconcile a shrunk count.
        for existing in list(self.list_children("channel")):
            if not (1 <= int(existing) <= self._channel_count):
                self.deregister_child("channel", existing)

    async def refresh_children(self) -> dict[str, Any]:
        """IDE 'Refresh from Device': re-register and re-read everything."""
        if not (self.transport and self.transport.connected):
            raise ConnectionError(f"[{self.device_id}] Not connected")
        self._register_topology()
        await self._sync_all()
        return {"channel": self._channel_count}

    async def _sync_all(self) -> None:
        """Seed device + channel state from the device on connect / refresh."""
        try:
            # Device-level values.
            for prop in ("DEVICE_ID", "DEVICE_AUDIO_MUTE", "FW_VER",
                         "LED_BRIGHTNESS"):
                await self._send(f"< GET {prop} >")
            # Channel names + mutes fan out from the index-0 broadcast query;
            # gain is per-channel (index 0 is not valid for a gain query).
            if self._channel_count:
                await self._send("< GET 0 CHAN_NAME >")
                await self._send("< GET 0 AUDIO_MUTE >")
                for n in range(1, self._channel_count + 1):
                    await self._send(f"< GET {n} AUDIO_GAIN_HI_RES >")
            # Metering subscription (opt-in). The command name differs across
            # models (MXA/ANI/SCM use METER_RATE; the P300 uses
            # METER_RATE_IN); send both and let the unsupported one REP ERR.
            if self._meter_ms > 0:
                for cmd in ("METER_RATE", "METER_RATE_IN"):
                    await self._send(f"< SET {cmd} {self._meter_ms} >")
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial sync failed")

    # ── Sending ──

    async def _send(self, message: str) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(message.encode("ascii"))

    async def _liveness_probe(self) -> None:
        """Send `< GET DEVICE_ID >` and await its REP (correlated by param).

        A mute push or a meter SAMPLE does not resolve the future — only a
        DEVICE_ID REP does — so the probe genuinely proves the link is alive.
        """
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._probe_fut = fut
        try:
            await self._send("< GET DEVICE_ID >")
            await fut
        finally:
            self._probe_fut = None

    # ── Commands ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "mute_on":
            await self._send("< SET DEVICE_AUDIO_MUTE ON >")
        elif command == "mute_off":
            await self._send("< SET DEVICE_AUDIO_MUTE OFF >")
        elif command == "mute_toggle":
            await self._send("< SET DEVICE_AUDIO_MUTE TOGGLE >")
        elif command == "set_channel_mute":
            ch = int(params["channel"])
            state = "ON" if params["mute"] else "OFF"
            await self._send(f"< SET {ch} AUDIO_MUTE {state} >")
        elif command == "toggle_channel_mute":
            ch = int(params["channel"])
            await self._send(f"< SET {ch} AUDIO_MUTE TOGGLE >")
        elif command == "set_channel_gain":
            ch = int(params["channel"])
            wire = _db_to_wire(float(params["gain_db"]))
            await self._send(f"< SET {ch} AUDIO_GAIN_HI_RES {wire} >")
        elif command == "step_channel_gain":
            ch = int(params["channel"])
            delta = float(params["delta_db"])
            step = abs(round(delta * 10))
            verb = "inc" if delta >= 0 else "dec"
            await self._send(f"< SET {ch} AUDIO_GAIN_HI_RES {verb} {step} >")
        elif command == "set_led_brightness":
            level = int(params["level"])
            await self._send(f"< SET LED_BRIGHTNESS {level} >")
        elif command == "set_led_state":
            await self._send(
                f"< SET LED_STATE_{params['which']} {params['state']} >"
            )
        elif command == "set_led_color":
            await self._send(
                f"< SET LED_COLOR_{params['which']} {params['color']} >"
            )
        elif command == "flash_on":
            await self._send("< SET FLASH ON >")
        elif command == "flash_off":
            await self._send("< SET FLASH OFF >")
        elif command == "recall_preset":
            await self._send(f"< SET PRESET {int(params['preset'])} >")
        elif command == "raw_command":
            await self._send(str(params["command"]))
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "led_brightness":
            await self._send(f"< SET LED_BRIGHTNESS {int(value)} >")
            return
        raise ValueError(f"[{self.device_id}] Unknown device setting: {key}")

    async def poll(self) -> None:
        # Device-level heartbeat/refresh; channels are push-driven.
        await self._send("< GET DEVICE_AUDIO_MUTE >")
        await self._send("< GET LED_BRIGHTNESS >")

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        # The transport frames on ">", delivering one message body (delimiter
        # stripped) per call. Split defensively in case a chunk carries more
        # than one ">"; _handle_frame normalizes the leading "<" and any CR/LF
        # that rode along from the prior line.
        for part in data.split(b">"):
            self._handle_frame(part)

    def _handle_frame(self, raw: bytes) -> None:
        text = raw.decode("ascii", errors="replace").strip()
        if not text:
            return
        # Drop the leading "<" (and any leading CR/LF already stripped).
        text = text.lstrip("<").strip()
        if not text or text.upper().startswith("REP ERR"):
            return

        # Device-level.
        m = self._RE_DEVICE_ID.match(text)
        if m:
            name = _strip_string_value(m.group(1))
            self.set_state("device_name", name)
            fut = self._probe_fut
            if fut is not None and not fut.done():
                fut.set_result(None)
            return

        m = self._RE_DEVICE_MUTE.match(text)
        if m:
            muted = m.group(1).upper() == "ON"
            self.set_state("mute", muted)
            self.set_state("audio_mute", muted)
            return

        m = self._RE_FW.match(text)
        if m:
            self.set_state("firmware", _strip_string_value(m.group(1)))
            return

        m = self._RE_LED_BRIGHT.match(text)
        if m:
            self.set_state("led_brightness", int(m.group(1)))
            return

        # Metering SAMPLE frames fan out into per-channel meter props.
        m = self._RE_SAMPLE.match(text)
        if m:
            self._apply_sample(m.group(1))
            return

        # Channel-indexed.
        m = self._RE_CH_MUTE.match(text)
        if m:
            self._set_channel(int(m.group(1)), "mute",
                              m.group(2).upper() == "ON")
            return

        m = self._RE_CH_NAME.match(text)
        if m:
            self._set_channel(int(m.group(1)), "name",
                              _strip_string_value(m.group(2)))
            return

        m = self._RE_CH_GAIN.match(text)
        if m:
            self._set_channel(int(m.group(1)), "gain_db",
                              _wire_to_db(int(m.group(2))))
            return

    def _set_channel(self, idx: int, prop: str, value: Any) -> None:
        # Index 0 is the "all channels" broadcast — a fan-out never REPs it as
        # its own line, but guard anyway.
        if 1 <= idx <= self._channel_count:
            self.set_child_state("channel", idx, prop, value)

    def _apply_sample(self, values_str: str) -> None:
        """Map a SAMPLE row positionally onto channel meter props.

        Values are ``000-060`` per channel, in the device's channel order,
        representing -60..0 dBFS. A single-value SAMPLE from an MXA array in
        automatic-coverage mode carries only the automixer output and can't be
        positionally attributed, so it's skipped when more than one channel is
        configured.
        """
        tokens = values_str.split()
        if not tokens:
            return
        if len(tokens) == 1 and self._channel_count != 1:
            return
        for i, tok in enumerate(tokens):
            idx = i + 1
            if idx > self._channel_count:
                break
            try:
                raw = int(tok)
            except ValueError:
                continue
            self._set_channel(idx, "meter", float(raw - _METER_DBFS_OFFSET))
