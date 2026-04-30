"""
OpenAVC AtlasIED Atmosphere Driver.

Controls AtlasIED Atmosphere AZM4 / AZM8 audio processing and control
systems over the third-party JSON-RPC 2.0 protocol on TCP port 5321.

Protocol summary:
    - Newline-delimited JSON-RPC 2.0 over TCP. Methods sent to the device:
      "set", "bmp" (bump), "sub" (subscribe), "unsub" (unsubscribe), "get".
      Methods sent back to the client: "update", "getResp", "error".
    - Parameter names are dynamically assigned during device configuration
      (e.g., "ZoneGain_0", "SourceMute_3"). The driver subscribes to a
      configurable count of zones, sources, mixes, and groups — controllers
      with more or fewer entities than the configured counts get
      out-of-range subscribes that the device silently rejects.
    - Inactivity drops the TCP connection after ~5 minutes; the driver
      sends a "KeepAlive" get every 4 minutes to hold the connection.
    - Meters (SourceMeter / ZoneMeter / MixMeter / GroupMeter) come over
      UDP port 3131 and are not exposed by this driver — most integration
      use cases don't need realtime level metering, and the TCP control
      surface is fully usable without it.

Source: https://www.atlasied.com/ATS006993-B-AZM4-AZM8-3rd-Party-Control.pdf
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)


KEEPALIVE_INTERVAL = 240.0  # 4 min — under the 5 min device timeout

# Default entity counts, sized for an AZM8. The driver subscribes to
# {Source,Zone,Mix,Group}_0..N-1 — the device drops subscribes for indices
# that aren't configured, so over-sizing is harmless.
DEFAULT_NUM_SOURCES = 8
DEFAULT_NUM_ZONES = 8
DEFAULT_NUM_MIXES = 8
DEFAULT_NUM_GROUPS = 4
DEFAULT_NUM_MESSAGES = 8
DEFAULT_NUM_ROUTINES = 8
DEFAULT_NUM_SCENES = 8
DEFAULT_NUM_GPO_PRESETS = 8
DEFAULT_NUM_GPOS = 4
DEFAULT_NUM_BELL_SCHEDULES = 4


def _build_state_vars(
    num_sources: int,
    num_zones: int,
    num_mixes: int,
    num_groups: int,
    num_messages: int,
    num_routines: int,
    num_scenes: int,
    num_gpo_presets: int,
    num_gpos: int,
    num_bell_schedules: int,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        "firmware_version": {"type": "string", "label": "Firmware Version"},
        "todays_bell_schedule": {
            "type": "integer",
            "label": "Today's Bell Schedule Index",
        },
    }
    for n in range(num_sources):
        out[f"source_{n}_name"] = {
            "type": "string",
            "label": f"Source {n} Name",
        }
        out[f"source_{n}_gain"] = {
            "type": "number",
            "label": f"Source {n} Gain (dB)",
            "min": -80,
            "max": 0,
        }
        out[f"source_{n}_mute"] = {
            "type": "boolean",
            "label": f"Source {n} Mute",
        }
    for n in range(num_zones):
        out[f"zone_{n}_name"] = {
            "type": "string",
            "label": f"Zone {n} Name",
        }
        out[f"zone_{n}_gain"] = {
            "type": "number",
            "label": f"Zone {n} Gain (dB)",
            "min": -80,
            "max": 0,
        }
        out[f"zone_{n}_mute"] = {
            "type": "boolean",
            "label": f"Zone {n} Mute",
        }
        out[f"zone_{n}_source"] = {
            "type": "integer",
            "label": f"Zone {n} Selected Source",
        }
        out[f"zone_{n}_grouped"] = {
            "type": "boolean",
            "label": f"Zone {n} Grouped",
        }
    for n in range(num_mixes):
        out[f"mix_{n}_name"] = {
            "type": "string",
            "label": f"Mix {n} Name",
        }
        out[f"mix_{n}_gain"] = {
            "type": "number",
            "label": f"Mix {n} Gain (dB)",
            "min": -80,
            "max": 0,
        }
        out[f"mix_{n}_mute"] = {
            "type": "boolean",
            "label": f"Mix {n} Mute",
        }
    for n in range(num_groups):
        out[f"group_{n}_name"] = {
            "type": "string",
            "label": f"Group {n} Name",
        }
        out[f"group_{n}_gain"] = {
            "type": "number",
            "label": f"Group {n} Gain (dB)",
            "min": -80,
            "max": 0,
        }
        out[f"group_{n}_mute"] = {
            "type": "boolean",
            "label": f"Group {n} Mute",
        }
        out[f"group_{n}_source"] = {
            "type": "integer",
            "label": f"Group {n} Selected Source",
        }
        out[f"group_{n}_active"] = {
            "type": "boolean",
            "label": f"Group {n} Active (Combined)",
        }
    for n in range(num_messages):
        out[f"message_{n}_name"] = {
            "type": "string",
            "label": f"Message {n} Name",
        }
    for n in range(num_routines):
        out[f"routine_{n}_name"] = {
            "type": "string",
            "label": f"Routine {n} Name",
        }
    for n in range(num_scenes):
        out[f"scene_{n}_name"] = {
            "type": "string",
            "label": f"Scene {n} Name",
        }
    for n in range(num_gpo_presets):
        out[f"gpo_preset_{n}_name"] = {
            "type": "string",
            "label": f"GPO Preset {n} Name",
        }
    for n in range(num_gpos):
        out[f"gpo_{n}_state"] = {
            "type": "boolean",
            "label": f"GPO {n} State",
        }
    for n in range(num_bell_schedules):
        out[f"bell_schedule_{n}_name"] = {
            "type": "string",
            "label": f"Bell Schedule {n} Name",
        }
    return out


class AtlasIEDAtmosphereDriver(BaseDriver):
    """AtlasIED Atmosphere AZM4/AZM8 driver."""

    DRIVER_INFO = {
        "id": "atlasied_atmosphere",
        "name": "AtlasIED Atmosphere",
        "manufacturer": "AtlasIED",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls AtlasIED Atmosphere AZM4 and AZM8 audio processing "
            "and control systems over the third-party JSON-RPC protocol "
            "on TCP port 5321. Covers source / zone / mix / group gain "
            "and mute, zone source select, group combine, message and "
            "routine and scene playback, GPO state, and today's bell "
            "schedule. Subscribes for state updates on connect."
        ),
        "source_url": "https://www.atlasied.com/ATS006993-B-AZM4-AZM8-3rd-Party-Control.pdf",
        "tags": ["atmosphere", "azm4", "azm8", "paging", "install-amp", "json-rpc"],
        "verified": False,
        "simulated": True,
        "protocols": ["atlasied-atmosphere-thirdparty"],
        "ports": [5321],
        "transport": "tcp",
        "discovery": {
            "ports": [5321],
        },
        "compatible_models": [
            {
                "manufacturer": "AtlasIED",
                "models": [
                    "AZM4",
                    "AZM8",
                    "AZMP4",
                    "AZMP8",
                ],
                "confidence": "untested",
                "notes": (
                    "AZM4 / AZM8 are the standalone DSPs; AZMP4 / AZMP8 add "
                    "internal amplification but share the same third-party "
                    "control surface."
                ),
            }
        ],
        "help": {
            "overview": (
                "AtlasIED Atmosphere is a configurable audio DSP / mixer / "
                "paging system widely deployed in restaurants, retail, "
                "fitness, and house-of-worship installs. This driver speaks "
                "the third-party JSON-RPC protocol on TCP port 5321. It "
                "subscribes to entity state on connect, so updates push "
                "live as users change settings on the front panel or in "
                "the web UI.\n\n"
                "Parameter names are assigned during device configuration "
                "(via the Atmosphere web UI). The driver addresses entities "
                "by zero-based index — Source 0, Zone 0, etc. Use the "
                "Atmosphere web UI's Settings → Third Party Control → "
                "Message Table to map names to indices."
            ),
            "setup": (
                "1. In the Atmosphere web UI, go to Settings → Third Party "
                "Control → General and toggle 'Enable' on for the "
                "third-party control API.\n"
                "2. Confirm the Atmosphere has a static IP (or DHCP "
                "reservation).\n"
                "3. In OpenAVC, enter the IP. Port 5321 is fixed.\n"
                "4. Set the entity counts (zones, sources, mixes, groups) "
                "to match the device configuration. Counts default to "
                "AZM8 sizing; over-sizing is harmless but bigger than "
                "needed clutters the state list.\n"
                "5. After connecting, confirm the source / zone / mix / "
                "group names from the Atmosphere config show up in the "
                "device state."
            ),
        },
        "default_config": {
            "host": "",
            "port": 5321,
            "num_sources": DEFAULT_NUM_SOURCES,
            "num_zones": DEFAULT_NUM_ZONES,
            "num_mixes": DEFAULT_NUM_MIXES,
            "num_groups": DEFAULT_NUM_GROUPS,
            "num_messages": DEFAULT_NUM_MESSAGES,
            "num_routines": DEFAULT_NUM_ROUTINES,
            "num_scenes": DEFAULT_NUM_SCENES,
            "num_gpo_presets": DEFAULT_NUM_GPO_PRESETS,
            "num_gpos": DEFAULT_NUM_GPOS,
            "num_bell_schedules": DEFAULT_NUM_BELL_SCHEDULES,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 5321,
                "label": "TCP Port",
                "description": "Atmosphere third-party control port — always 5321.",
            },
            "num_sources": {
                "type": "integer",
                "default": DEFAULT_NUM_SOURCES,
                "min": 0,
                "label": "Source Count",
                "description": (
                    "Number of audio sources defined in the Atmosphere "
                    "configuration. Default is 8."
                ),
            },
            "num_zones": {
                "type": "integer",
                "default": DEFAULT_NUM_ZONES,
                "min": 0,
                "label": "Zone Count",
                "description": (
                    "Number of zones — 4 for AZM4, 8 for AZM8."
                ),
            },
            "num_mixes": {
                "type": "integer",
                "default": DEFAULT_NUM_MIXES,
                "min": 0,
                "label": "Mix Count",
            },
            "num_groups": {
                "type": "integer",
                "default": DEFAULT_NUM_GROUPS,
                "min": 0,
                "label": "Group Count",
            },
            "num_messages": {
                "type": "integer",
                "default": DEFAULT_NUM_MESSAGES,
                "min": 0,
                "label": "Stored Message Count",
            },
            "num_routines": {
                "type": "integer",
                "default": DEFAULT_NUM_ROUTINES,
                "min": 0,
                "label": "Routine Count",
            },
            "num_scenes": {
                "type": "integer",
                "default": DEFAULT_NUM_SCENES,
                "min": 0,
                "label": "Scene Count",
            },
            "num_gpo_presets": {
                "type": "integer",
                "default": DEFAULT_NUM_GPO_PRESETS,
                "min": 0,
                "label": "GPO Preset Count",
            },
            "num_gpos": {
                "type": "integer",
                "default": DEFAULT_NUM_GPOS,
                "min": 0,
                "label": "GPO Count",
            },
            "num_bell_schedules": {
                "type": "integer",
                "default": DEFAULT_NUM_BELL_SCHEDULES,
                "min": 0,
                "label": "Bell Schedule Count",
            },
        },
        "state_variables": _build_state_vars(
            DEFAULT_NUM_SOURCES,
            DEFAULT_NUM_ZONES,
            DEFAULT_NUM_MIXES,
            DEFAULT_NUM_GROUPS,
            DEFAULT_NUM_MESSAGES,
            DEFAULT_NUM_ROUTINES,
            DEFAULT_NUM_SCENES,
            DEFAULT_NUM_GPO_PRESETS,
            DEFAULT_NUM_GPOS,
            DEFAULT_NUM_BELL_SCHEDULES,
        ),
        "commands": {
            "set_source_gain": {
                "label": "Set Source Gain",
                "params": {
                    "source": {"type": "integer", "required": True, "min": 0},
                    "gain_db": {
                        "type": "number",
                        "required": True,
                        "min": -80,
                        "max": 0,
                        "help": "Gain in dB (-80 to 0).",
                    },
                },
                "help": "Set the gain on a source.",
            },
            "set_source_mute": {
                "label": "Set Source Mute",
                "params": {
                    "source": {"type": "integer", "required": True, "min": 0},
                    "mute": {"type": "boolean", "required": True},
                },
            },
            "bump_source_gain": {
                "label": "Bump Source Gain",
                "params": {
                    "source": {"type": "integer", "required": True, "min": 0},
                    "delta_db": {
                        "type": "number",
                        "required": True,
                        "help": "Gain change in dB (positive or negative).",
                    },
                },
                "help": "Adjust source gain by a relative amount.",
            },
            "set_zone_gain": {
                "label": "Set Zone Gain",
                "params": {
                    "zone": {"type": "integer", "required": True, "min": 0},
                    "gain_db": {
                        "type": "number",
                        "required": True,
                        "min": -80,
                        "max": 0,
                    },
                },
            },
            "set_zone_mute": {
                "label": "Set Zone Mute",
                "params": {
                    "zone": {"type": "integer", "required": True, "min": 0},
                    "mute": {"type": "boolean", "required": True},
                },
            },
            "bump_zone_gain": {
                "label": "Bump Zone Gain",
                "params": {
                    "zone": {"type": "integer", "required": True, "min": 0},
                    "delta_db": {"type": "number", "required": True},
                },
            },
            "set_zone_source": {
                "label": "Select Zone Source",
                "params": {
                    "zone": {"type": "integer", "required": True, "min": 0},
                    "source": {
                        "type": "integer",
                        "required": True,
                        "min": -1,
                        "help": "Source index, or -1 for none.",
                    },
                },
                "help": "Select which source feeds a zone.",
            },
            "set_mix_gain": {
                "label": "Set Mix Gain",
                "params": {
                    "mix": {"type": "integer", "required": True, "min": 0},
                    "gain_db": {
                        "type": "number",
                        "required": True,
                        "min": -80,
                        "max": 0,
                    },
                },
            },
            "set_mix_mute": {
                "label": "Set Mix Mute",
                "params": {
                    "mix": {"type": "integer", "required": True, "min": 0},
                    "mute": {"type": "boolean", "required": True},
                },
            },
            "bump_mix_gain": {
                "label": "Bump Mix Gain",
                "params": {
                    "mix": {"type": "integer", "required": True, "min": 0},
                    "delta_db": {"type": "number", "required": True},
                },
            },
            "set_group_gain": {
                "label": "Set Group Gain",
                "params": {
                    "group": {"type": "integer", "required": True, "min": 0},
                    "gain_db": {
                        "type": "number",
                        "required": True,
                        "min": -80,
                        "max": 0,
                    },
                },
            },
            "set_group_mute": {
                "label": "Set Group Mute",
                "params": {
                    "group": {"type": "integer", "required": True, "min": 0},
                    "mute": {"type": "boolean", "required": True},
                },
            },
            "bump_group_gain": {
                "label": "Bump Group Gain",
                "params": {
                    "group": {"type": "integer", "required": True, "min": 0},
                    "delta_db": {"type": "number", "required": True},
                },
            },
            "set_group_source": {
                "label": "Select Group Source",
                "params": {
                    "group": {"type": "integer", "required": True, "min": 0},
                    "source": {
                        "type": "integer",
                        "required": True,
                        "min": -1,
                    },
                },
            },
            "set_group_active": {
                "label": "Combine / Uncombine Group",
                "params": {
                    "group": {"type": "integer", "required": True, "min": 0},
                    "active": {
                        "type": "boolean",
                        "required": True,
                        "help": "True = combine zones, False = uncombine.",
                    },
                },
                "help": (
                    "Activate (combine) or deactivate (uncombine) the zones "
                    "in a group, matching the Combine button in the Zones "
                    "tab of the web UI."
                ),
            },
            "play_message": {
                "label": "Play Message",
                "params": {
                    "message": {"type": "integer", "required": True, "min": 0},
                },
                "help": "Trigger playback of a stored message.",
            },
            "recall_routine": {
                "label": "Recall Routine",
                "params": {
                    "routine": {"type": "integer", "required": True, "min": 0},
                },
            },
            "recall_scene": {
                "label": "Recall Scene",
                "params": {
                    "scene": {"type": "integer", "required": True, "min": 0},
                },
            },
            "recall_gpo_preset": {
                "label": "Recall GPO Preset",
                "params": {
                    "preset": {"type": "integer", "required": True, "min": 0},
                },
            },
            "set_gpo": {
                "label": "Set GPO State",
                "params": {
                    "gpo": {"type": "integer", "required": True, "min": 0},
                    "state": {"type": "boolean", "required": True},
                },
                "help": "Drive a general-purpose output high (true) or low (false).",
            },
            "set_todays_bell_schedule": {
                "label": "Set Today's Bell Schedule",
                "params": {
                    "schedule": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "help": "Bell schedule index to use today.",
                    },
                },
            },
        },
    }

    # ── Lifecycle ──

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._line_buffer = b""
        self._keepalive_task: asyncio.Task | None = None
        self._next_msg_id = 1
        super().__init__(device_id, config, state, events)

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 5321))

        if not host:
            raise ConnectionError(
                f"[{self.device_id}] Atmosphere host is required"
            )

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=None,
            timeout=5.0,
            name=self.device_id,
        )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to AtlasIED Atmosphere at {host}:{port}")

        # Subscribe to all configured params. Out-of-range subscribes
        # are silently ignored by the device, so over-sizing is harmless.
        try:
            await self._subscribe_all()
            await self._send_get("FirmwareVersion", "str")
            await self._send_get("TodaysBellSchedule", "val")
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial subscribe failed")

        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def disconnect(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self._line_buffer = b""
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    # ── Sending ──

    def _next_id(self) -> int:
        n = self._next_msg_id
        self._next_msg_id += 1
        return n

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        await self.transport.send(line.encode("utf-8"))

    async def _send_set(self, param: str, key: str, value: Any) -> None:
        # key is 'val', 'pct', or 'str' — picks how value is interpreted.
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "set",
            "params": {"param": param, key: value},
        })

    async def _send_bump(self, param: str, key: str, value: Any) -> None:
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "bmp",
            "params": {"param": param, key: value},
        })

    async def _send_sub(self, param: str, fmt: str) -> None:
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "sub",
            "params": {"param": param, "fmt": fmt},
        })

    async def _send_get(self, param: str, fmt: str) -> None:
        await self._send_json({
            "jsonrpc": "2.0",
            "method": "get",
            "params": {"param": param, "fmt": fmt},
        })

    async def _subscribe_all(self) -> None:
        cfg = self.config

        # Sources: gain (val), mute (val), name (str)
        for n in range(int(cfg.get("num_sources", DEFAULT_NUM_SOURCES))):
            await self._send_sub(f"SourceGain_{n}", "val")
            await self._send_sub(f"SourceMute_{n}", "val")
            await self._send_sub(f"SourceName_{n}", "str")

        for n in range(int(cfg.get("num_zones", DEFAULT_NUM_ZONES))):
            await self._send_sub(f"ZoneGain_{n}", "val")
            await self._send_sub(f"ZoneMute_{n}", "val")
            await self._send_sub(f"ZoneName_{n}", "str")
            await self._send_sub(f"ZoneSource_{n}", "val")
            await self._send_sub(f"ZoneGrouped_{n}", "val")

        for n in range(int(cfg.get("num_mixes", DEFAULT_NUM_MIXES))):
            await self._send_sub(f"MixGain_{n}", "val")
            await self._send_sub(f"MixMute_{n}", "val")
            await self._send_sub(f"MixName_{n}", "str")

        for n in range(int(cfg.get("num_groups", DEFAULT_NUM_GROUPS))):
            await self._send_sub(f"GroupGain_{n}", "val")
            await self._send_sub(f"GroupMute_{n}", "val")
            await self._send_sub(f"GroupName_{n}", "str")
            await self._send_sub(f"GroupSource_{n}", "val")
            await self._send_sub(f"GroupActive_{n}", "val")

        for n in range(int(cfg.get("num_messages", DEFAULT_NUM_MESSAGES))):
            await self._send_sub(f"MessageName_{n}", "str")

        for n in range(int(cfg.get("num_routines", DEFAULT_NUM_ROUTINES))):
            await self._send_sub(f"RoutineName_{n}", "str")

        for n in range(int(cfg.get("num_scenes", DEFAULT_NUM_SCENES))):
            await self._send_sub(f"SceneName_{n}", "str")

        for n in range(int(cfg.get("num_gpo_presets", DEFAULT_NUM_GPO_PRESETS))):
            await self._send_sub(f"GpoPresetName_{n}", "str")

        for n in range(int(cfg.get("num_gpos", DEFAULT_NUM_GPOS))):
            await self._send_sub(f"GpoState_{n}", "val")

        for n in range(int(cfg.get("num_bell_schedules", DEFAULT_NUM_BELL_SCHEDULES))):
            await self._send_sub(f"BellScheduleName_{n}", "str")

        await self._send_sub("TodaysBellSchedule", "val")
        await self._send_sub("FirmwareVersion", "str")

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if self.transport and self.transport.connected:
                    try:
                        await self._send_get("KeepAlive", "str")
                    except (ConnectionError, OSError):
                        log.warning(f"[{self.device_id}] Keepalive send failed")
                        return
        except asyncio.CancelledError:
            raise

    # ── Commands ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}

        if command == "set_source_gain":
            await self._send_set(
                f"SourceGain_{int(params['source'])}", "val", float(params["gain_db"])
            )
        elif command == "set_source_mute":
            await self._send_set(
                f"SourceMute_{int(params['source'])}",
                "val",
                1 if params["mute"] else 0,
            )
        elif command == "bump_source_gain":
            await self._send_bump(
                f"SourceGain_{int(params['source'])}",
                "val",
                float(params["delta_db"]),
            )
        elif command == "set_zone_gain":
            await self._send_set(
                f"ZoneGain_{int(params['zone'])}", "val", float(params["gain_db"])
            )
        elif command == "set_zone_mute":
            await self._send_set(
                f"ZoneMute_{int(params['zone'])}", "val", 1 if params["mute"] else 0
            )
        elif command == "bump_zone_gain":
            await self._send_bump(
                f"ZoneGain_{int(params['zone'])}", "val", float(params["delta_db"])
            )
        elif command == "set_zone_source":
            await self._send_set(
                f"ZoneSource_{int(params['zone'])}", "val", int(params["source"])
            )
        elif command == "set_mix_gain":
            await self._send_set(
                f"MixGain_{int(params['mix'])}", "val", float(params["gain_db"])
            )
        elif command == "set_mix_mute":
            await self._send_set(
                f"MixMute_{int(params['mix'])}", "val", 1 if params["mute"] else 0
            )
        elif command == "bump_mix_gain":
            await self._send_bump(
                f"MixGain_{int(params['mix'])}", "val", float(params["delta_db"])
            )
        elif command == "set_group_gain":
            await self._send_set(
                f"GroupGain_{int(params['group'])}", "val", float(params["gain_db"])
            )
        elif command == "set_group_mute":
            await self._send_set(
                f"GroupMute_{int(params['group'])}", "val", 1 if params["mute"] else 0
            )
        elif command == "bump_group_gain":
            await self._send_bump(
                f"GroupGain_{int(params['group'])}", "val", float(params["delta_db"])
            )
        elif command == "set_group_source":
            await self._send_set(
                f"GroupSource_{int(params['group'])}", "val", int(params["source"])
            )
        elif command == "set_group_active":
            await self._send_set(
                f"GroupActive_{int(params['group'])}",
                "val",
                1 if params["active"] else 0,
            )
        elif command == "play_message":
            await self._send_set(
                f"PlayMessage_{int(params['message'])}", "val", 1
            )
        elif command == "recall_routine":
            await self._send_set(
                f"RecallRoutine_{int(params['routine'])}", "val", 1
            )
        elif command == "recall_scene":
            await self._send_set(
                f"RecallScene_{int(params['scene'])}", "val", 1
            )
        elif command == "recall_gpo_preset":
            await self._send_set(
                f"RecallGpoPreset_{int(params['preset'])}", "val", 1
            )
        elif command == "set_gpo":
            await self._send_set(
                f"GpoState_{int(params['gpo'])}", "val", 1 if params["state"] else 0
            )
        elif command == "set_todays_bell_schedule":
            await self._send_set(
                "TodaysBellSchedule", "val", int(params["schedule"])
            )
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    # ── Receiving ──

    async def on_data_received(self, data: bytes) -> None:
        self._line_buffer += data
        while b"\n" in self._line_buffer:
            line_bytes, self._line_buffer = self._line_buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.debug(f"[{self.device_id}] Non-JSON line: {line!r}")
            return

        if not isinstance(msg, dict):
            return

        # Responses to messages with "id" come back as {"jsonrpc":"2.0","result":"OK","id":N}
        # — no method, no params. Skip.
        if "method" not in msg:
            return

        method = msg.get("method")
        if method == "error":
            log.warning(f"[{self.device_id}] Device error: {msg}")
            return
        if method not in ("update", "getResp"):
            return

        params = msg.get("params")
        # params may be a single dict or a list of dicts
        if isinstance(params, dict):
            self._dispatch_param_update(params)
        elif isinstance(params, list):
            for item in params:
                if isinstance(item, dict):
                    self._dispatch_param_update(item)

    def _dispatch_param_update(self, item: dict[str, Any]) -> None:
        param = item.get("param", "")
        if not param:
            return

        # Pick whichever value field is present. The device echoes back
        # the same fmt we asked for in the subscribe.
        if "val" in item:
            value: Any = item["val"]
        elif "str" in item:
            value = item["str"]
        elif "pct" in item:
            value = item["pct"]
        else:
            return

        # KeepAlive responses arrive as {"param":"KeepAlive","str":"OK"} —
        # nothing useful to expose; just confirms the link is alive.
        if param == "KeepAlive":
            return

        if param == "FirmwareVersion":
            self.set_state("firmware_version", str(value))
            return

        if param == "TodaysBellSchedule":
            self.set_state("todays_bell_schedule", _to_int(value))
            return

        # SourceGain_3 / ZoneMute_0 / GroupActive_2 / etc.
        prefix, _, idx_str = param.rpartition("_")
        if not prefix or not idx_str.isdigit():
            return
        idx = int(idx_str)

        # Map (prefix, value handler) -> state key
        if prefix == "SourceGain":
            self.set_state(f"source_{idx}_gain", _to_float(value))
        elif prefix == "SourceMute":
            self.set_state(f"source_{idx}_mute", _to_bool(value))
        elif prefix == "SourceName":
            self.set_state(f"source_{idx}_name", str(value))
        elif prefix == "ZoneGain":
            self.set_state(f"zone_{idx}_gain", _to_float(value))
        elif prefix == "ZoneMute":
            self.set_state(f"zone_{idx}_mute", _to_bool(value))
        elif prefix == "ZoneName":
            self.set_state(f"zone_{idx}_name", str(value))
        elif prefix == "ZoneSource":
            self.set_state(f"zone_{idx}_source", _to_int(value))
        elif prefix == "ZoneGrouped":
            self.set_state(f"zone_{idx}_grouped", _to_bool(value))
        elif prefix == "MixGain":
            self.set_state(f"mix_{idx}_gain", _to_float(value))
        elif prefix == "MixMute":
            self.set_state(f"mix_{idx}_mute", _to_bool(value))
        elif prefix == "MixName":
            self.set_state(f"mix_{idx}_name", str(value))
        elif prefix == "GroupGain":
            self.set_state(f"group_{idx}_gain", _to_float(value))
        elif prefix == "GroupMute":
            self.set_state(f"group_{idx}_mute", _to_bool(value))
        elif prefix == "GroupName":
            self.set_state(f"group_{idx}_name", str(value))
        elif prefix == "GroupSource":
            self.set_state(f"group_{idx}_source", _to_int(value))
        elif prefix == "GroupActive":
            self.set_state(f"group_{idx}_active", _to_bool(value))
        elif prefix == "MessageName":
            self.set_state(f"message_{idx}_name", str(value))
        elif prefix == "RoutineName":
            self.set_state(f"routine_{idx}_name", str(value))
        elif prefix == "SceneName":
            self.set_state(f"scene_{idx}_name", str(value))
        elif prefix == "GpoPresetName":
            self.set_state(f"gpo_preset_{idx}_name", str(value))
        elif prefix == "GpoState":
            self.set_state(f"gpo_{idx}_state", _to_bool(value))
        elif prefix == "BellScheduleName":
            self.set_state(f"bell_schedule_{idx}_name", str(value))


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False
