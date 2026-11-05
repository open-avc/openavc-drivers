"""
OpenAVC Blackmagic Streaming Encoder driver.

Controls the Blackmagic Streaming Encoder HD and 4K (sold before 2025 as the
Blackmagic Web Presenter HD and 4K) over the Blackmagic Streaming Encoder
Ethernet Protocol v1.2, a text protocol on TCP 9977. Source: "Blackmagic
Streaming Encoder Ethernet Protocol" v1.2, Blackmagic Design developer
information, January 2026. The Web Presenter manual (May 2024) carries the
same v1.2 protocol under the Web Presenter name.

Why Python: the encoder reports its state in blocks, a header ending in a
colon, ``Key: Value`` lines and a blank line, and the keys repeat from block
to block (each ``NETWORK INTERFACE n`` block has its own ``Name``, ``MAC
Address`` and ``Dynamic IP``), so a YAML line rule cannot tell which block a
line belongs to, and a delimiter parser drops the blank line that ends one.
Every request is answered ``ACK`` or ``NACK`` with nothing to say which
request it answers, so requests go one at a time. And an ``ACK`` is not an
application: a value the encoder does not take is still answered ``ACK``,
with the old value reported back, so every write is checked against what the
encoder reports afterwards and refused when it differs. The lists the pickers
offer are comma-separated with ``\\,`` and ``\\\\`` escapes.

Push and poll: on connecting the encoder sends its whole state, ending with
``END PRELUDE:``, and afterwards resends a block whenever anything in it
changes, whoever changed it (Blackmagic Streaming Setup, the front panel, a
tethered phone, a streaming service that drops). The stream's bitrate,
duration and cache use are not reported as they change; the document says to
poll them, so ``STREAM STATE`` is read every poll. The other blocks, except
``STREAM SETTINGS`` (it carries the stream key), are read again every twelfth
poll in case a change report was lost. A ``STREAM STATE`` request is also the
liveness check: the protocol has no ping.

Wire rules from the document, as built:
- The encoder ends lines with LF; the client may send LF or CR LF.
- A block the driver does not recognise is skipped up to its blank line, and
  a key it does not recognise is ignored.
- A request is a block: a header, the keys to change, a blank line. A header
  and a blank line alone asks for that block's complete state.
- A key the encoder does not know is ignored, and an invalid value for a key
  it knows leaves the key as it was; both are still answered ACK.
- ``SHUTDOWN`` with ``Action: Reboot`` restarts the encoder and closes every
  connection.

The stream key and the SRT passphrase are never written into state: the
driver reports whether each is set, and masks both in the device's log and
traffic.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1
    CommandRefusedError = RuntimeError

log = get_logger(__name__)

DEFAULT_PORT = 9977
# The encoder sends its whole state the moment the connection opens.
PRELUDE_TIMEOUT_S = 5.0
REQUEST_TIMEOUT_S = 5.0
# After an ACK, the block the encoder resends follows at once. A write that
# changed nothing may draw none, and is then read back.
BLOCK_TIMEOUT_S = 2.0
# A reply that arrives after its request timed out is waited for this long
# before the next request goes out; past it the connection is reopened, since
# every later ACK would answer the wrong request.
LATE_REPLY_S = 3.0
FULL_READ_EVERY = 12
INTERFACE_MAX = 8
# A line longer than this is not the protocol; the buffer is dropped.
RX_LIMIT = 1 << 20

H_PREAMBLE = "PROTOCOL PREAMBLE"
H_END = "END PRELUDE"
H_IDENTITY = "IDENTITY"
H_VERSION = "VERSION"
H_NETWORK = "NETWORK"
H_UI = "UI SETTINGS"
H_STREAM = "STREAM SETTINGS"
H_XML = "STREAM XML"
H_STATE = "STREAM STATE"
H_AUDIO = "AUDIO SETTINGS"
H_SHUTDOWN = "SHUTDOWN"
_INTERFACE = re.compile(r"^NETWORK INTERFACE (\d+)$")
KEY_VALUE_HEADERS = frozenset({
    H_PREAMBLE, H_END, H_IDENTITY, H_VERSION, H_NETWORK, H_UI, H_STREAM,
    H_XML, H_STATE, H_AUDIO,
})

# The key only a block's complete state carries, so a read is answered by the
# dump and not by a change report of the same block.
DUMP_KEY = {
    H_IDENTITY: "model",
    H_VERSION: "product id",
    H_NETWORK: "interface count",
    H_UI: "available locales",
    H_STREAM: "available video modes",
    H_XML: "files",
    H_STATE: "bitrate",
    H_AUDIO: "available monitor out audio sources",
}
INTERFACE_DUMP_KEY = "name"
# Read again every FULL_READ_EVERY polls. Not STREAM SETTINGS: it carries the
# stream key, and the encoder reports every change to it.
RESYNC_READS = (H_IDENTITY, H_VERSION, H_NETWORK, H_UI, H_AUDIO, H_XML)

STREAM_STATUSES = ("idle", "connecting", "streaming", "flushing", "interrupted")
# Starting, live, or retrying an interrupted stream.
ON_AIR_STATUSES = frozenset({"connecting", "streaming", "interrupted"})

# Picker command -> (block, key as sent, parameter, what the refusal names,
# what to do when the encoder keeps its old value).
PICKER_COMMANDS = {
    "set_video_mode": (
        H_STREAM, "Video Mode", "mode", "the video mode",
        "Choose one of the modes it lists.",
    ),
    "select_platform": (
        H_STREAM, "Current Platform", "platform", "the platform",
        "Choose one of the platforms it lists.",
    ),
    "select_server": (
        H_STREAM, "Current Server", "server", "the server",
        "The servers depend on the platform: choose one it lists for the current platform.",
    ),
    "select_quality": (
        H_STREAM, "Current Quality Level", "quality", "the quality level",
        "The quality levels depend on the platform: choose one it lists for the current platform.",
    ),
    "set_monitor_audio": (
        H_AUDIO, "Current Monitor Out Audio Source", "source", "the monitor audio source",
        "Choose one of the sources it lists.",
    ),
    "set_audio_meter": (
        H_UI, "Current Audio Meter", "meter", "the audio meter",
        "Choose one of the meters it lists.",
    ),
    "set_language": (
        H_UI, "Current Locale", "locale", "the language",
        "Choose one of the languages it lists.",
    ),
}


@dataclass
class _Pending:
    """One request: its ACK or NACK, then the block that answers it."""

    text: str
    header: str
    keys: frozenset[str]
    answer: asyncio.Future = field(repr=False)
    block: asyncio.Future | None = field(default=None, repr=False)
    armed: bool = False


class BlackmagicStreamingEncoderDriver(BaseDriver):
    """Blackmagic Streaming Encoder Ethernet Protocol driver."""

    DRIVER_INFO = {
        "id": "blackmagic_streaming_encoder",
        "name": "Blackmagic Streaming Encoder",
        "manufacturer": "Blackmagic Design",
        "category": "streaming",
        "version": "1.0.0",
        "author": "OpenAVC",
        # Computed: the confirm on Restart and Factory Reset needs 0.36.0.
        # Everything newer the code calls is looked up first
        # (CommandRefusedError, local_address).
        "min_platform_version": "0.36.0",
        "description": (
            "Controls the Blackmagic Streaming Encoder HD and 4K (formerly Web "
            "Presenter HD and 4K) over the Blackmagic Streaming Encoder "
            "Ethernet Protocol (TCP 9977): start and stop the live stream, "
            "choose the platform, server, quality and video mode, set the "
            "stream key, and see the stream's status, bitrate and duration as "
            "the encoder reports them."
        ),
        "source_url": "https://documents.blackmagicdesign.com/DeveloperManuals/WebPresenterEthernetProtocol.pdf",
        "tags": ["streaming", "encoder", "live-stream", "rtmp", "srt", "web-presenter", "blackmagic"],
        "verified": False,
        "simulated": True,
        "protocols": ["blackmagic_streaming"],
        "ports": [9977],
        "transport": "tcp",
        "discovery": {
            # The encoder sends its whole state, the IDENTITY block with its
            # model among it, the moment a client connects, so a connect-only
            # probe reads the model. No document names an mDNS service.
            # Blackmagic's IEEE blocks cover every Blackmagic product (ATEM,
            # Videohub, HyperDeck, cameras), so no oui.
            "port_open": [9977],
            "manufacturer_alias": ["blackmagic", "blackmagic design", "bmd"],
            "tcp_probe": {
                "port": 9977,
                "expect_regex": r"Model: Blackmagic (?:Streaming Encoder|Web Presenter)",
                "extract_manufacturer": "Blackmagic Design",
                "extract": {
                    "model": {"regex": r"Model:\s*([^\r\n]+)"},
                },
                "timeout_ms": 3000,
            },
        },
        "compatible_models": [
            {
                "manufacturer": "Blackmagic Design",
                "models": ["Blackmagic Streaming Encoder HD", "Blackmagic Streaming Encoder 4K"],
                "confidence": "untested",
                "notes": (
                    "Built from the Blackmagic Streaming Encoder Ethernet "
                    "Protocol v1.2 (January 2026). The 4K streams Ultra HD; "
                    "the HD streams up to 1080p. The encoder lists its own "
                    "video modes, platforms, servers and quality levels."
                ),
            },
            {
                "manufacturer": "Blackmagic Design",
                "models": ["Blackmagic Web Presenter HD", "Blackmagic Web Presenter 4K"],
                "confidence": "untested",
                "notes": (
                    "The same units under their name before 2025; the Web "
                    "Presenter manual (May 2024) documents the same v1.2 "
                    "protocol. Not the original Web Presenter, which has no "
                    "streaming."
                ),
            },
        ],
        "help": {
            "overview": (
                "Blackmagic Streaming Encoder HD and 4K (formerly Web "
                "Presenter HD and 4K) over TCP 9977. Start and stop the live "
                "stream; choose the streaming platform, server, quality and "
                "video mode from the lists the encoder offers; set the stream "
                "key, passphrase or URL; choose the monitor output's audio and "
                "which connection streams when a phone is tethered. The "
                "encoder reports every change as it happens; the bitrate, "
                "duration and cache use are read every poll."
            ),
            "setup": (
                "1. Give the encoder a fixed IP address (in Blackmagic "
                "Streaming Setup, on the Setup tab) and enter it here. Control "
                "uses TCP 9977, which needs no login.\n"
                "2. Platforms loaded from a streaming XML file appear in the "
                "platform list beside the built-in ones; load them with "
                "Blackmagic Streaming Setup.\n"
                "3. The stream key is never shown here: the device page says "
                "only whether one is set."
            ),
        },
        "default_config": {
            "host": "",
            "port": 9977,
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer", "default": 9977, "label": "TCP Port",
                "description": "Streaming Encoder Ethernet Protocol port, 9977.",
            },
            "poll_interval": {
                "type": "integer", "default": 5, "min": 1, "label": "Poll Interval (sec)",
                "description": "How often the stream's bitrate, duration and cache use are read.",
            },
        },
        "state_variables": {
            "stream_status": {
                "type": "enum", "label": "Stream Status",
                "values": ["idle", "connecting", "streaming", "flushing", "interrupted"],
                "control": True, "cloud_priority": "high",
                "help": (
                    "Idle: not streaming. Connecting: starting. Streaming: live. "
                    "Interrupted: the connection to the platform dropped and the "
                    "encoder is retrying. Flushing: sending what it holds before "
                    "it stops."
                ),
            },
            "on_air": {
                "type": "boolean", "label": "On Air", "control": True, "cloud_priority": "high",
                "help": "On while a stream is starting, live, or retrying after an interruption.",
            },
            "bitrate": {
                "type": "integer", "label": "Bitrate", "min": 0, "unit": "bps", "cloud_priority": "low",
            },
            "duration": {
                "type": "string", "label": "Duration", "cloud_priority": "low",
                "help": "How long the stream has run, as days:hours:minutes:seconds.",
            },
            "cache_used": {
                "type": "integer", "label": "Cache Used", "min": 0, "max": 100, "unit": "%",
                "cloud_priority": "low",
                "help": "How full the stream's cache is. A rising value means the network is not keeping up.",
            },
            "video_mode": {
                "type": "string", "label": "Video Mode", "control": True,
                "help": "The stream's video standard, or Auto to follow the input.",
            },
            "video_mode_options": {
                "type": "string", "label": "Video Modes", "cloud_priority": "low",
                "help": "The video modes this encoder offers, as a picker list.",
            },
            "platform": {"type": "string", "label": "Platform", "control": True},
            "platform_options": {
                "type": "string", "label": "Platforms", "cloud_priority": "low",
                "help": "The built-in platforms and any loaded from a streaming XML file, as a picker list.",
            },
            "server": {"type": "string", "label": "Server"},
            "server_options": {
                "type": "string", "label": "Servers", "cloud_priority": "low",
                "help": "The current platform's servers, as a picker list.",
            },
            "quality": {"type": "string", "label": "Quality"},
            "quality_options": {
                "type": "string", "label": "Quality Levels", "cloud_priority": "low",
                "help": "The current platform's quality levels, as a picker list.",
            },
            "stream_key_set": {
                "type": "boolean", "label": "Stream Key Set",
                "help": "Whether the encoder holds a stream key. The key itself is not shown.",
            },
            "passphrase_set": {
                "type": "boolean", "label": "Passphrase Set",
                "help": "Whether the encoder holds an SRT passphrase. The passphrase is not shown.",
            },
            "url": {"type": "string", "label": "Stream URL"},
            "url_customizable": {
                "type": "boolean", "label": "URL Can Be Changed",
                "help": "Whether the current platform takes a URL of your own.",
            },
            "monitor_audio_source": {"type": "string", "label": "Monitor Audio"},
            "monitor_audio_source_options": {
                "type": "string", "label": "Monitor Audio Sources", "cloud_priority": "low",
            },
            "audio_meter": {"type": "string", "label": "Audio Meter"},
            "audio_meter_options": {"type": "string", "label": "Audio Meters", "cloud_priority": "low"},
            "locale": {"type": "string", "label": "Language"},
            "locale_options": {"type": "string", "label": "Languages", "cloud_priority": "low"},
            "custom_platform_files": {
                "type": "string", "label": "Streaming XML Files", "cloud_priority": "low",
                "help": "The streaming XML files loaded on the encoder.",
            },
            "interface_count": {"type": "integer", "label": "Network Interfaces"},
            "default_interface": {
                "type": "integer", "label": "Default Interface", "min": 0,
                "help": "The network interface the encoder streams over.",
            },
            "default_interface_name": {
                "type": "string", "label": "Streaming Over",
                "help": "The name of the network interface the encoder streams over.",
            },
            "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
            "label": {"type": "string", "label": "Name", "cloud_priority": "low"},
            "unique_id": {"type": "string", "label": "Unique ID", "cloud_priority": "low"},
            "product_id": {"type": "string", "label": "Product ID", "cloud_priority": "low"},
            "hardware_version": {"type": "string", "label": "Hardware Version", "cloud_priority": "low"},
            "software_version": {"type": "string", "label": "Software Version", "cloud_priority": "low"},
            "software_release": {"type": "string", "label": "Software Release", "cloud_priority": "low"},
            "protocol_version": {"type": "string", "label": "Protocol Version", "cloud_priority": "low"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "network_interface": {
                "label": "Network Interface",
                "label_plural": "Network Interfaces",
                "id_format": {"type": "integer", "min": 0, "max": 7},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "priority": {
                        "type": "integer", "label": "Priority", "min": 0,
                        "help": "With more than one connection, the one with the higher number streams.",
                    },
                    "has_address": {
                        "type": "boolean", "label": "Connected", "cloud_priority": "high",
                        "help": "On while the interface has an IP address (a tethered phone, a connected cable).",
                    },
                    "mac_address": {"type": "string", "label": "MAC Address", "cloud_priority": "low"},
                    "dhcp": {"type": "boolean", "label": "DHCP"},
                    "ip_address": {"type": "string", "label": "IP Address"},
                    "subnet_mask": {"type": "string", "label": "Subnet Mask", "cloud_priority": "low"},
                    "gateway": {"type": "string", "label": "Gateway", "cloud_priority": "low"},
                    "dns_servers": {"type": "string", "label": "DNS Servers", "cloud_priority": "low"},
                    "static_address": {"type": "string", "label": "Static IP Address", "cloud_priority": "low"},
                    "static_subnet_mask": {"type": "string", "label": "Static Subnet Mask", "cloud_priority": "low"},
                    "static_gateway": {"type": "string", "label": "Static Gateway", "cloud_priority": "low"},
                    "static_dns_servers": {"type": "string", "label": "Static DNS Servers", "cloud_priority": "low"},
                },
                "summary_fields": ["has_address", "ip_address", "priority"],
                "label_field": "name",
            },
        },
        "device_settings": {
            "label": {
                "type": "string", "label": "Name", "state_key": "label",
                "default": "", "setup": False, "regex": r"^[^\r\n]*$",
                "help": "The name the encoder shows in Blackmagic Streaming Setup and on its display.",
            },
        },
        "quick_actions": ["start_stream", "stop_stream"],
        "actions": [
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection",
                "icon": "search",
                "availability": "always",
            },
        ],
        "commands": {
            "start_stream": {
                "label": "Start Streaming",
                "params": {},
                "sets": {"on_air": True},
                "help": "Goes on air: the encoder connects to the platform and starts the live stream.",
            },
            "stop_stream": {
                "label": "Stop Streaming",
                "params": {},
                "sets": {"on_air": False},
                "help": "Ends the live stream.",
            },
            "set_video_mode": {
                "label": "Set Video Mode",
                "params": {
                    "mode": {
                        "type": "string", "label": "Video Mode", "required": True,
                        "options_state": "video_mode_options",
                        "help": "One of the modes the encoder lists, or Auto to follow the input.",
                    },
                },
                "sets": {"video_mode": "{mode}"},
                "help": "Sets the stream's video standard.",
            },
            "select_platform": {
                "label": "Select Platform",
                "params": {
                    "platform": {
                        "type": "string", "label": "Platform", "required": True,
                        "options_state": "platform_options",
                    },
                },
                "sets": {"platform": "{platform}"},
                "help": (
                    "Chooses the streaming platform. Its servers and quality "
                    "levels then replace the lists offered."
                ),
            },
            "select_server": {
                "label": "Select Server",
                "params": {
                    "server": {
                        "type": "string", "label": "Server", "required": True,
                        "options_state": "server_options",
                    },
                },
                "sets": {"server": "{server}"},
                "help": "Chooses the platform server the stream goes to.",
            },
            "select_quality": {
                "label": "Select Quality",
                "params": {
                    "quality": {
                        "type": "string", "label": "Quality", "required": True,
                        "options_state": "quality_options",
                    },
                },
                "sets": {"quality": "{quality}"},
                "help": "Chooses the stream's quality level, which sets its bitrate.",
            },
            "set_stream_key": {
                "label": "Set Stream Key",
                "params": {
                    "key": {
                        "type": "string", "label": "Stream Key", "required": True, "secret": True,
                        "pattern": r"^[^\r\n]+$",
                        "help": "The key the streaming platform gave for this broadcast.",
                    },
                },
                "sets": {"stream_key_set": True},
                "help": "Gives the encoder the stream key for the current platform.",
            },
            "set_passphrase": {
                "label": "Set SRT Passphrase",
                "params": {
                    "passphrase": {
                        "type": "string", "label": "Passphrase", "secret": True,
                        "pattern": r"^[^\r\n]*$",
                        "help": "The passphrase for an encrypted SRT stream. Blank clears it.",
                    },
                },
                "help": "Sets the passphrase an encrypted SRT stream uses.",
            },
            "set_stream_url": {
                "label": "Set Stream URL",
                "params": {
                    "url": {
                        "type": "string", "label": "URL", "required": True,
                        "pattern": r"^[^\r\n]+$",
                        "help": "The rtmp:// or srt:// address the stream goes to.",
                    },
                },
                "sets": {"url": "{url}"},
                "help": (
                    "Sends the stream to an address of your own. Only a platform "
                    "whose URL can be changed takes one (URL Can Be Changed)."
                ),
            },
            "set_up_stream": {
                "label": "Set Up Stream",
                "params": {
                    "platform": {
                        "type": "string", "label": "Platform", "required": True,
                        "options_state": "platform_options",
                    },
                    "server": {
                        "type": "string", "label": "Server",
                        "help": "Blank keeps the platform's first server. The list offered is the current platform's.",
                    },
                    "quality": {
                        "type": "string", "label": "Quality",
                        "help": "Blank keeps the encoder's choice. The list offered is the current platform's.",
                    },
                    "key": {
                        "type": "string", "label": "Stream Key", "secret": True,
                        "pattern": r"^[^\r\n]*$",
                        "help": "Blank keeps the stream key the encoder holds.",
                    },
                },
                "sets": {"platform": "{platform}"},
                "help": (
                    "Sets the platform, server, quality and stream key in one "
                    "step, for a macro that prepares a broadcast."
                ),
            },
            "set_monitor_audio": {
                "label": "Set Monitor Audio",
                "params": {
                    "source": {
                        "type": "string", "label": "Source", "required": True,
                        "options_state": "monitor_audio_source_options",
                        "help": (
                            "Auto: talkback from an ATEM Streaming Bridge when "
                            "there is any, else the SDI input."
                        ),
                    },
                },
                "sets": {"monitor_audio_source": "{source}"},
                "help": "Chooses the audio on the encoder's SDI and HDMI monitor outputs.",
            },
            "set_audio_meter": {
                "label": "Set Audio Meter",
                "params": {
                    "meter": {
                        "type": "string", "label": "Meter", "required": True,
                        "options_state": "audio_meter_options",
                    },
                },
                "sets": {"audio_meter": "{meter}"},
                "help": "Chooses the audio meters shown on the encoder's display and monitor output.",
            },
            "set_language": {
                "label": "Set Language",
                "params": {
                    "locale": {
                        "type": "string", "label": "Language", "required": True,
                        "options_state": "locale_options",
                    },
                },
                "sets": {"locale": "{locale}"},
                "help": "Chooses the language of the encoder's display.",
            },
            "set_connection_priority": {
                "label": "Set Connection Priority",
                "params": {
                    "interface": {
                        "type": "child_id", "child_type": "network_interface", "label": "Interface",
                        "required": True,
                        "help": "The connection to stream over: the Ethernet port, or a phone tethered over USB.",
                    },
                },
                "help": (
                    "Chooses which connection streams when Ethernet and a "
                    "tethered phone are both connected. Changing network "
                    "settings can drop the control connection for a moment."
                ),
            },
            "reboot": {
                "label": "Restart Streaming Encoder",
                "params": {},
                "confirm": "Restarts the encoder. A live stream stops, and the encoder is offline until it has started.",
                "help": "Restarts the encoder. It is offline until it has started.",
            },
            "factory_reset": {
                "label": "Factory Reset",
                "params": {},
                "confirm": (
                    "Returns every setting to its factory default: the platform, "
                    "stream key, network settings and name are lost, and the "
                    "encoder may come back at a different IP address."
                ),
                "help": "Returns every setting on the encoder to its factory default.",
            },
        },
    }

    # The stream state on the BaseDriver cadence (every 30 s). The deadline
    # covers waiting behind a write in progress (its ACK 5 s, the reported
    # block 2 s, a read back 7 s) and the read itself.
    HEALTH_TIMEOUT_S = 20.0
    HEALTH_FAULT_MESSAGE = "The Streaming Encoder stopped answering."

    # ── Lifecycle ──

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._rx = b""
        self._block: tuple[str, list[str]] | None = None
        self._prelude: asyncio.Future | None = None
        self._preamble_seen = False
        self._pending: _Pending | None = None
        self._lock = asyncio.Lock()
        self._session_up = False
        self._cycle = 0
        # The encoder's last reported value of every key, by block. The
        # stream key and passphrase live only here, never in state.
        self._reported: dict[str, dict[str, str]] = {}
        self._interface_names: dict[int, str] = {}
        self._platforms: dict[str, list[str]] = {"default": [], "custom": []}
        self._read_problems: dict[str, str] = {}
        self._standing: dict[str, str] = {}
        super().__init__(device_id, config, state, events)

    def _transport_kwargs(self, transport_type: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        # Raw bytes: the driver splits lines itself and keeps the blank line
        # that ends a block, which a delimiter parser would drop.
        kwargs["delimiter"] = None
        kwargs["frame_parser"] = None
        return kwargs

    async def _pre_connect(self) -> None:
        # Before the socket opens: the encoder sends its state at once, and
        # the end of it must find a waiter.
        self._rx = b""
        self._block = None
        self._pending = None
        self._session_up = False
        self._preamble_seen = False
        self._prelude = asyncio.get_running_loop().create_future()

    async def _post_connect(self) -> None:
        prelude = self._prelude
        assert prelude is not None
        try:
            await asyncio.wait_for(asyncio.shield(prelude), PRELUDE_TIMEOUT_S)
        except asyncio.TimeoutError:
            port = self.config.get("port", DEFAULT_PORT)
            if self._preamble_seen:
                raise ConnectionError(
                    f"The Streaming Encoder on port {port} began its status "
                    f"report and did not finish it within {PRELUDE_TIMEOUT_S:.0f} s."
                ) from None
            raise ConnectionError(
                f"The device on port {port} did not send a Streaming Encoder "
                f"status report within {PRELUDE_TIMEOUT_S:.0f} s. Check this is "
                "a Blackmagic Streaming Encoder or Web Presenter's address."
            ) from None
        self._session_up = True

    async def _initial_sync(self) -> None:
        # The status report on connecting is the first full read.
        self._cycle = 0
        self._write_last_error()

    async def _close_session(self) -> None:
        self._session_up = False
        pending, self._pending = self._pending, None
        if pending is not None:
            for future in (pending.answer, pending.block):
                if future is not None and not future.done():
                    future.set_exception(ConnectionError("connection closed"))
                    future.exception()
        prelude = self._prelude
        if prelude is not None and not prelude.done():
            prelude.cancel()
        self._rx = b""
        self._block = None

    # ── Requests (one at a time) ──

    async def _exchange(
        self, header: str, lines: list[str], keys: frozenset[str], timeout: float = REQUEST_TIMEOUT_S,
    ) -> _Pending:
        """Send one block and wait for its ACK or NACK. Silence raises."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        loop = asyncio.get_running_loop()
        text = header + ":\n" + "".join(line + "\n" for line in lines) + "\n"
        pending = _Pending(
            text=text, header=header, keys=keys, answer=loop.create_future(),
            block=loop.create_future() if keys else None,
        )
        self._pending = pending
        await self.transport.send(text.encode("utf-8"))
        try:
            await asyncio.wait_for(asyncio.shield(pending.answer), timeout)
        except asyncio.TimeoutError:
            # Left pending, so a late ACK lands on it and not on the next
            # request.
            raise ConnectionError(
                f"[{self.device_id}] The Streaming Encoder did not answer '{header}' within {timeout:.0f} s"
            ) from None
        return pending

    async def _await_block(self, pending: _Pending, timeout: float) -> bool:
        """Whether the block answering an ACKed request arrived in time."""
        try:
            if pending.block is None or not pending.answer.result():
                return False
            await asyncio.wait_for(asyncio.shield(pending.block), timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            if self._pending is pending:
                self._pending = None

    async def _settle_late_reply(self) -> None:
        stale = self._pending
        if stale is None:
            return
        if not stale.answer.done():
            try:
                await asyncio.wait_for(asyncio.shield(stale.answer), LATE_REPLY_S)
            except asyncio.TimeoutError:
                self._force_disconnect(
                    "no_response", f"The Streaming Encoder stopped answering ('{stale.header}' got no reply).",
                )
                raise ConnectionError(f"[{self.device_id}] The Streaming Encoder stopped answering") from None
            except Exception:
                pass
        if self._pending is stale:
            self._pending = None

    async def _read(self, header: str, timeout: float = REQUEST_TIMEOUT_S) -> bool:
        """Ask for a block's complete state.

        The ACK (or NACK) is the answer that proves the encoder is there, and
        silence raises; the block itself is waited for briefly and applied
        whenever it arrives.
        """
        async with self._lock:
            await self._settle_late_reply()
            pending = await self._exchange(header, [], _dump_keys(header), timeout)
            if not pending.answer.result():
                if self._pending is pending:
                    self._pending = None
                self._read_problems[header] = f"The Streaming Encoder refused to report {header} (NACK)."
                return False
            self._read_problems.pop(header, None)
            await self._await_block(pending, BLOCK_TIMEOUT_S)
            return True

    async def _write(
        self, header: str, values: list[tuple[str, str]], label: str,
        hint: str = "", secret: bool = False, verify: bool = True,
    ) -> bool:
        """Send changed keys and check the encoder took them.

        The encoder ACKs a value it does not take and reports the old one
        back, so the keys it reports after the ACK are compared with what was
        sent. When it reports nothing (the value was already set), the block
        is read back.
        """
        for _key, value in values:
            if "\r" in value or "\n" in value:
                raise ValueError(f"{label[0].upper() + label[1:]} is one line of text")
        keys = frozenset(key.lower() for key, _value in values)
        async with self._lock:
            await self._settle_late_reply()
            pending = await self._exchange(header, [f"{key}: {value}" for key, value in values], keys)
            if not pending.answer.result():
                if self._pending is pending:
                    self._pending = None
                raise CommandRefusedError(f"The Streaming Encoder refused {label} (NACK).")
            if not verify:
                if self._pending is pending:
                    self._pending = None
                return True
            if not await self._await_block(pending, BLOCK_TIMEOUT_S):
                read = await self._exchange(header, [], keys | _dump_keys(header))
                await self._await_block(read, BLOCK_TIMEOUT_S)
        reported = self._reported.get(header, {})
        for key, value in values:
            now = reported.get(key.lower())
            if now is None:
                continue
            if secret:
                # Compared only for being set or not: a firmware that reports
                # the key masked would otherwise read as refusing every key.
                if bool(now.strip()) != bool(value.strip()):
                    raise CommandRefusedError(f"The Streaming Encoder did not take {label}. {hint}".strip())
                continue
            if _same(now, value):
                continue
            raise CommandRefusedError(
                f"The Streaming Encoder did not take {label} '{value}': it still "
                f"reports '{_unescape(now)}'. {hint}".strip()
            )
        return True

    # ── Receiving ──

    def on_data_received(self, data: bytes) -> None:
        # Synchronous: the transport runs a plain handler inline, in the order
        # the bytes arrive, so a block is never applied after a later ACK.
        self._rx += data
        while True:
            end = self._rx.find(b"\n")
            if end < 0:
                break
            raw, self._rx = self._rx[:end], self._rx[end + 1:]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            self._line(raw.decode("utf-8", errors="replace"))
        if len(self._rx) > RX_LIMIT:
            log.warning(f"[{self.device_id}] Dropped {len(self._rx)} bytes with no line ending")
            self._rx = b""

    def _line(self, line: str) -> None:
        block = self._block
        text = line.strip()
        if block is not None:
            if text and not (text in ("ACK", "NACK") and _known(block[0])):
                block[1].append(line)
                return
            self._block = None
            self._finish_block(block[0], block[1])
            if not text:
                return
        if not text:
            return
        if text in ("ACK", "NACK"):
            self._answer(text == "ACK")
            return
        if text.endswith(":"):
            # A block: its lines follow, up to a blank line. Inside one,
            # nothing is read as a header ("Files:" is a key with no value).
            self._block = (text[:-1].strip(), [])
            return
        log.debug(f"[{self.device_id}] Unexpected line: {line!r}")

    def _answer(self, ack: bool) -> None:
        pending = self._pending
        if pending is not None and not pending.answer.done():
            pending.answer.set_result(ack)
            pending.armed = ack
        else:
            log.debug(f"[{self.device_id}] {'ACK' if ack else 'NACK'} with no request")

    def _finish_block(self, header: str, lines: list[str]) -> None:
        if not _known(header):
            # Not a block this driver reads (a streaming XML file's contents,
            # a block a later firmware adds): skipped whole.
            return
        params = _params(lines)
        reported = self._reported.setdefault(header, {})
        reported.update(params)
        interface = _INTERFACE.match(header)
        if header == H_PREAMBLE:
            self._apply_preamble(params)
        elif header == H_END:
            prelude = self._prelude
            if prelude is not None and not prelude.done():
                prelude.set_result(True)
        elif header == H_IDENTITY:
            self._apply_identity(params)
        elif header == H_VERSION:
            self._apply_version(params)
        elif header == H_NETWORK:
            self._apply_network(params)
        elif interface:
            self._apply_interface(int(interface.group(1)), params)
        elif header == H_UI:
            self._apply_ui(params)
        elif header == H_STREAM:
            self._apply_stream_settings(params)
        elif header == H_XML:
            if "files" in params:
                self.set_state("custom_platform_files", ", ".join(_split_list(params["files"])))
        elif header == H_STATE:
            self._apply_stream_state(params)
        elif header == H_AUDIO:
            self._apply_audio(params)
        pending = self._pending
        if (
            pending is not None and pending.armed and pending.block is not None
            and not pending.block.done() and pending.header == header
            and pending.keys <= params.keys()
        ):
            pending.block.set_result(True)

    # ── What each block reports ──

    def _apply_preamble(self, p: dict[str, str]) -> None:
        self._preamble_seen = True
        version = p.get("version")
        if version is None:
            return
        self.set_state("protocol_version", version)
        if version.split(".", 1)[0].strip() != "1":
            self._standing["protocol"] = (
                f"The encoder reports protocol {version}; this driver was built "
                "for protocol 1, so some values may not be read."
            )
        else:
            self._standing.pop("protocol", None)

    def _apply_identity(self, p: dict[str, str]) -> None:
        for wire, key in (("model", "model"), ("label", "label"), ("unique id", "unique_id")):
            if wire in p:
                self.set_state(key, _unescape(p[wire]))

    def _apply_version(self, p: dict[str, str]) -> None:
        for wire, key in (
            ("product id", "product_id"), ("hardware version", "hardware_version"),
            ("software version", "software_version"), ("software release", "software_release"),
        ):
            if wire in p:
                self.set_state(key, p[wire])

    def _apply_network(self, p: dict[str, str]) -> None:
        count = _int(p.get("interface count"))
        if count is not None:
            count = max(0, min(count, INTERFACE_MAX))
            self.set_state("interface_count", count)
            want = set(range(count))
            have = set(self.list_children("network_interface"))
            for iid in sorted(want - have):
                self.register_child("network_interface", iid)
            for iid in have - want:
                self.deregister_child("network_interface", iid)
                self._interface_names.pop(iid, None)
        default = _int(p.get("default interface"))
        if default is not None:
            self.set_state("default_interface", default)
        self._write_default_name()

    def _apply_interface(self, iid: int, p: dict[str, str]) -> None:
        if not 0 <= iid < INTERFACE_MAX:
            return
        if not self.is_child_registered("network_interface", iid):
            self.register_child("network_interface", iid)
        values: dict[str, Any] = {}
        if "name" in p:
            values["name"] = _unescape(p["name"])
            self._interface_names[iid] = values["name"]
        if "priority" in p:
            values["priority"] = _int(p["priority"])
        if "mac address" in p:
            values["mac_address"] = p["mac address"]
        if "dynamic ip" in p:
            values["dhcp"] = _bool(p["dynamic ip"])
        if "current addresses" in p:
            address, mask = _address(p["current addresses"])
            values["ip_address"] = address
            values["subnet_mask"] = mask
            values["has_address"] = address not in ("", "0.0.0.0")
        if "current gateway" in p:
            values["gateway"] = p["current gateway"]
        if "current dns servers" in p:
            values["dns_servers"] = ", ".join(_split_list(p["current dns servers"]))
        if "static addresses" in p:
            address, mask = _address(p["static addresses"])
            values["static_address"] = address
            values["static_subnet_mask"] = mask
        if "static gateway" in p:
            values["static_gateway"] = p["static gateway"]
        if "static dns servers" in p:
            values["static_dns_servers"] = ", ".join(_split_list(p["static dns servers"]))
        if values:
            self.set_children_state_batch([("network_interface", iid, values)])
        self._write_default_name()

    def _write_default_name(self) -> None:
        default = self.get_state("default_interface")
        if isinstance(default, int) and default in self._interface_names:
            self.set_state("default_interface_name", self._interface_names[default])

    def _apply_ui(self, p: dict[str, str]) -> None:
        if "available locales" in p:
            self.set_state("locale_options", json.dumps(_split_list(p["available locales"])))
        if "current locale" in p:
            self.set_state("locale", _unescape(p["current locale"]))
        if "available audio meters" in p:
            self.set_state("audio_meter_options", json.dumps(_split_list(p["available audio meters"])))
        if "current audio meter" in p:
            self.set_state("audio_meter", _unescape(p["current audio meter"]))

    def _apply_stream_settings(self, p: dict[str, str]) -> None:
        # The secrets first, so they are masked before anything is logged.
        for wire, key in (("stream key", "stream_key_set"), ("password", "passphrase_set")):
            if wire in p:
                secret = p[wire]
                if secret:
                    self.redact_in_log(secret)
                self.set_state(key, bool(secret))
        if "available video modes" in p:
            self.set_state("video_mode_options", json.dumps(_split_list(p["available video modes"])))
        for wire, key in (
            ("video mode", "video_mode"), ("current platform", "platform"),
            ("current server", "server"), ("current quality level", "quality"),
            ("current url", "url"),
        ):
            if wire in p:
                self.set_state(key, _unescape(p[wire]))
        if "customizable url" in p:
            self.set_state("url_customizable", _bool(p["customizable url"]))
        changed = False
        for wire, which in (("available default platforms", "default"), ("available custom platforms", "custom")):
            if wire in p:
                self._platforms[which] = _split_list(p[wire])
                changed = True
        if changed:
            merged: list[str] = []
            for name in self._platforms["default"] + self._platforms["custom"]:
                if name not in merged:
                    merged.append(name)
            self.set_state("platform_options", json.dumps(merged))
        if "available servers" in p:
            self.set_state("server_options", json.dumps(_split_list(p["available servers"])))
        if "available quality levels" in p:
            self.set_state("quality_options", json.dumps(_split_list(p["available quality levels"])))

    def _apply_stream_state(self, p: dict[str, str]) -> None:
        if "status" in p:
            status = p["status"].strip().lower()
            if status not in STREAM_STATUSES:
                log.info(f"[{self.device_id}] Stream status not in the document: {p['status']!r}")
            self.set_state("stream_status", status)
            self.set_state("on_air", status in ON_AIR_STATUSES)
        if "bitrate" in p:
            self.set_state("bitrate", _int(p["bitrate"]))
        if "duration" in p:
            self.set_state("duration", p["duration"])
        if "cache used" in p:
            self.set_state("cache_used", _int(p["cache used"]))

    def _apply_audio(self, p: dict[str, str]) -> None:
        if "available monitor out audio sources" in p:
            self.set_state(
                "monitor_audio_source_options",
                json.dumps(_split_list(p["available monitor out audio sources"])),
            )
        if "current monitor out audio source" in p:
            self.set_state("monitor_audio_source", _unescape(p["current monitor out audio source"]))

    # ── Polling ──

    async def poll(self) -> None:
        self._cycle += 1
        await self._read(H_STATE)
        if self._cycle % FULL_READ_EVERY == 0:
            for header in RESYNC_READS:
                await self._read(header)
            for iid in sorted(self.list_children("network_interface")):
                await self._read(f"NETWORK INTERFACE {iid}")
        self._write_last_error()

    def _write_last_error(self) -> None:
        # Standing conditions are written again on every poll while they hold.
        problems = list(self._standing.values()) + list(self._read_problems.values())
        if problems:
            self.set_state("last_error", " ".join(problems))

    async def refresh_children(self) -> dict[str, Any]:
        await self._read(H_NETWORK)
        for iid in sorted(self.list_children("network_interface")):
            await self._read(f"NETWORK INTERFACE {iid}")
        return {"network_interfaces": len(self.list_children("network_interface"))}

    async def _liveness_probe(self) -> None:
        # Any answer counts, a NACK included: the request only asks whether
        # the encoder is answering. The platform's deadline governs the wait
        # (on a reconnect held offline it is HEALTH_INTERVAL_S, so an encoder
        # still starting comes online at its late answer); the request's own
        # timeout is longer than either deadline.
        await self._read(H_STATE, timeout=float(self.HEALTH_INTERVAL_S) + float(self.HEALTH_TIMEOUT_S))

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if command == "start_stream":
            # The encoder reports Connecting, then Streaming, as it goes.
            return await self._write(H_STATE, [("Action", "Start")], "Start Streaming", verify=False)
        if command == "stop_stream":
            return await self._write(H_STATE, [("Action", "Stop")], "Stop Streaming", verify=False)
        if command in PICKER_COMMANDS:
            header, key, param, label, hint = PICKER_COMMANDS[command]
            value = _required_text(params.get(param), label)
            return await self._write(header, [(key, value)], label, hint)
        if command == "set_stream_key":
            key = _required_text(params.get("key"), "the stream key")
            self.redact_in_log(key)
            return await self._write(H_STREAM, [("Stream Key", key)], "the stream key", secret=True)
        if command == "set_passphrase":
            passphrase = str(params.get("passphrase") or "").strip()
            if passphrase:
                self.redact_in_log(passphrase)
            return await self._write(H_STREAM, [("Password", passphrase)], "the passphrase", secret=True)
        if command == "set_stream_url":
            url = _required_text(params.get("url"), "the URL")
            hint = "" if self.get_state("url_customizable") is not False else (
                "The current platform's URL cannot be changed: choose a platform whose URL can be."
            )
            return await self._write(H_STREAM, [("Current URL", url)], "the URL", hint)
        if command == "set_up_stream":
            return await self._set_up_stream(params)
        if command == "set_connection_priority":
            return await self._set_connection_priority(params)
        if command == "reboot":
            # ACKed, then the encoder closes every connection and restarts;
            # the device reconnects when it is back.
            return await self._write(H_SHUTDOWN, [("Action", "Reboot")], "Restart", verify=False)
        if command == "factory_reset":
            return await self._write(H_SHUTDOWN, [("Action", "Factory Reset")], "Factory Reset", verify=False)
        raise ValueError(f"Unknown command '{command}'")

    async def _set_up_stream(self, params: dict[str, Any]) -> bool:
        values = [("Current Platform", _required_text(params.get("platform"), "the platform"))]
        server = str(params.get("server") or "").strip()
        if server:
            values.append(("Current Server", server))
        quality = str(params.get("quality") or "").strip()
        if quality:
            values.append(("Current Quality Level", quality))
        key = str(params.get("key") or "").strip()
        if key:
            self.redact_in_log(key)
        # One block, in the document's own order: the platform first, so the
        # server and quality are read against its lists.
        await self._write(
            H_STREAM, values, "the stream set-up",
            "Choose a platform it lists, then a server and quality it lists for that platform.",
        )
        if key:
            await self._write(H_STREAM, [("Stream Key", key)], "the stream key", secret=True)
        return True

    async def _set_connection_priority(self, params: dict[str, Any]) -> bool:
        chosen = _int(params.get("interface"))
        interfaces = sorted(self.list_children("network_interface"))
        if chosen is None or chosen not in interfaces:
            raise ValueError(f"The encoder has no network interface {params.get('interface')!r}")
        name = self._interface_names.get(chosen, f"interface {chosen}")

        def priority(iid: int) -> int:
            return _int(self._reported.get(f"NETWORK INTERFACE {iid}", {}).get("priority")) or 0

        # The higher number streams. One write, the chosen interface above
        # every other: a network write can drop the connection, and a second
        # write that never went would leave two interfaces tied.
        highest_other = max((priority(other) for other in interfaces if other != chosen), default=-1)
        if priority(chosen) > highest_other:
            return True
        await self._write(
            f"NETWORK INTERFACE {chosen}", [("Priority", str(highest_other + 1))], f"the priority of {name}",
        )
        return True

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key == "label":
            text = str(value if value is not None else "").strip()
            return await self._write(H_IDENTITY, [("Label", text)], "the name")
        raise ValueError(f"Unknown device setting '{key}'")

    # ── Setup action ──

    async def run_setup_action(self, action_id: str, params: dict[str, Any], progress: Any) -> dict[str, Any]:
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")
        host = str(self.config.get("host") or "").strip()
        port = int(self.config.get("port") or DEFAULT_PORT)
        if not host:
            raise ValueError("No IP address is entered")
        await progress(f"Connecting to {host}:{port}", 20)
        local = getattr(self, "local_address", None)  # OpenAVC 0.37.0 and later
        local_ip = local(host) if callable(local) else None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, local_addr=(local_ip, 0) if local_ip else None),
                timeout=5.0,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach {host}:{port} ({exc or type(exc).__name__}). Check the IP "
                "address and that the encoder is on the network."
            ) from exc
        await progress("Reading the status report", 60)
        text = ""
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 4.0
            while loop.time() < deadline:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=deadline - loop.time())
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                text += chunk.decode("utf-8", errors="replace")
                if re.search(r"^END PRELUDE:", text, re.M):
                    break
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass
        if not re.search(r"^PROTOCOL PREAMBLE:", text, re.M):
            raise ConnectionError(
                f"{host}:{port} answered but sent no Streaming Encoder status report. "
                "Check this is a Blackmagic Streaming Encoder or Web Presenter."
            )
        model = _grep(text, r"^Model:[ \t]*([^\r\n]+)") or "Streaming Encoder"
        name = _grep(text, r"^Label:[ \t]*([^\r\n]+)")
        release = _grep(text, r"^Software Release:[ \t]*([^\r\n]+)")
        version = _grep(text, r"^Version:[ \t]*([^\r\n]+)")
        await progress("Connected", 100)
        parts = [f"'{name}'" if name and name != model else "", f"software {release}" if release else "",
                 f"protocol {version}" if version else ""]
        details = ", ".join(part for part in parts if part)
        return {
            "success": True,
            "message": f"Reached {model}{' (' + details + ')' if details else ''} on {host}:{port}.",
        }


# ── Helpers ──


def _known(header: str) -> bool:
    return header in KEY_VALUE_HEADERS or bool(_INTERFACE.match(header))


def _dump_keys(header: str) -> frozenset[str]:
    if _INTERFACE.match(header):
        return frozenset({INTERFACE_DUMP_KEY})
    key = DUMP_KEY.get(header)
    return frozenset({key}) if key else frozenset()


def _params(lines: list[str]) -> dict[str, str]:
    """A block's "Key: Value" lines; keys lowercased, values trimmed.

    The first colon divides, so a value keeps its own ("Current Server: US
    West: Los Angeles, CA", a URL).
    """
    out: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip().lower()] = value.strip()
    return out


def _split_list(value: str) -> list[str]:
    """A comma-separated value, with the protocol's ``\\,`` and ``\\\\`` escapes."""
    items: list[str] = []
    current: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ",":
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    items.append("".join(current).strip())
    return [item for item in items if item]


def _unescape(value: str) -> str:
    # A single value is not escaped by the document; an escaped comma in one
    # is read as the comma it stands for.
    if "\\" not in value:
        return value
    return value.replace("\\\\", "\x00").replace("\\,", ",").replace("\x00", "\\")


def _same(reported: str, sent: str) -> bool:
    return _unescape(reported).strip().casefold() == sent.strip().casefold()


def _address(value: str) -> tuple[str, str]:
    """``192.168.1.10/255.255.255.0`` (the first, if several) -> address, mask."""
    first = (_split_list(value) or [""])[0]
    address, _sep, mask = first.partition("/")
    return address.strip(), mask.strip()


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _required_text(value: Any, label: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"{label[0].upper() + label[1:]} is required")
    return text


def _grep(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.M)
    return match.group(1).strip() if match else None
