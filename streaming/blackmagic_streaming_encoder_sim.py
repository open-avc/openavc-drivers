"""
Blackmagic Streaming Encoder simulator.

The server side of the Blackmagic Streaming Encoder Ethernet Protocol v1.2
(January 2026): the status report on connecting (PROTOCOL PREAMBLE first, END
PRELUDE last), requests as blocks ended by a blank line, ACK or NACK, and a
block resent to every connection, holding only what changed, whenever
something changes.

As the document describes:
  * A header and a blank line alone is answered ACK and the block's complete
    state.
  * A key the encoder does not know is ignored; an invalid value for a key it
    knows leaves the key as it was and is answered ACK with the existing value
    reported back. A value that is already set changes nothing and is
    reported by nothing.
  * The stream's Status is reported when it changes; Bitrate, Duration and
    Cache Used only when STREAM STATE is asked for.
  * Lists are comma-separated, with ``,`` and ``\\`` escaped by ``\\``.
  * ``SHUTDOWN`` / ``Action: Reboot`` is answered ACK and every connection is
    closed.

What the document does not say, modelled as a guess (each is on the driver's
hardware checklist): a platform change moves the server to the new platform's
first one and the quality to Streaming High when the old one is not offered;
Factory Reset restores the defaults and closes every connection, as a restart.

From the device's config (simulator only): ``model`` (the model name the unit
reports, default "Blackmagic Streaming Encoder HD"; a name with 4K offers the
2160p modes).

The Simulator UI's Stream Status select stands in for a platform dropping the
stream, and Phone Tethered for a phone connected over USB. The error mode
"Platform refuses the stream" leaves a started stream Interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

LOCAL = "_local"

HD_MODES = [
    "Auto", "1080p23.98", "1080p24", "1080p25", "1080p29.97", "1080p30", "1080p50",
    "1080p59.94", "1080p60", "720p25", "720p30", "720p50", "720p60",
]
UHD_MODES = HD_MODES[:1] + [
    "2160p23.98", "2160p24", "2160p25", "2160p29.97", "2160p30", "2160p50", "2160p59.94", "2160p60",
] + HD_MODES[1:]
LOCALES = [
    "en_US.UTF-8", "zh_CN.UTF-8", "ja_JP.UTF-8", "ko_KR.UTF-8", "es_ES.UTF-8", "de_DE.UTF-8",
    "fr_FR.UTF-8", "ru_RU.UTF-8", "it_IT.UTF-8", "pt_BR.UTF-8", "tr_TR.UTF-8", "pl_PL.UTF-8",
    "uk_UA.UTF-8",
]
AUDIO_METERS = ["PPM -18dB", "PPM -20dB", "VU -18dB", "VU -20dB"]
MONITOR_SOURCES = ["Auto", "SDI In", "Remote Source"]
ALL_QUALITY = [
    "HyperDeck High", "HyperDeck Medium", "HyperDeck Low",
    "Streaming High", "Streaming Medium", "Streaming Low",
]
STREAMING_QUALITY = ["Streaming High", "Streaming Medium", "Streaming Low"]
# Bits a second at each quality (the manual's HD H.264 table, lower rates).
BITRATES = {
    "HyperDeck High": 45_000_000, "HyperDeck Medium": 25_000_000, "HyperDeck Low": 12_000_000,
    "Streaming High": 6_000_000, "Streaming Medium": 4_500_000, "Streaming Low": 3_000_000,
}

# The document's default platforms. Servers and URLs are the simulator's own.
_GENERIC = {"servers": ["Primary"], "urls": {"Primary": "rtmp://stream.example.com/live"}, "custom_url": False}
PLATFORMS: dict[str, dict[str, Any]] = {
    "YouTube RTMP": {
        "servers": ["Primary", "Secondary"],
        "urls": {"Primary": "rtmp://a.rtmp.youtube.com/live2", "Secondary": "rtmp://b.rtmp.youtube.com/live2"},
        "custom_url": False,
    },
    "YouTube SRT (Beta)": {
        "servers": ["Primary", "Secondary"],
        "urls": {"Primary": "srt://a.srt.youtube.com:2010", "Secondary": "srt://b.srt.youtube.com:2010"},
        "custom_url": False, "quality": STREAMING_QUALITY,
    },
    "Facebook": _GENERIC,
    "Microsoft Teams": {"servers": ["Custom"], "urls": {"Custom": ""}, "custom_url": True},
    "Instagram": {"servers": ["Custom"], "urls": {"Custom": ""}, "custom_url": True},
    "Twitch": {
        # Names with commas: escaped in the list, not in Current Server.
        "servers": ["US West: Los Angeles, CA", "US East: New York, NY", "Europe: London, UK"],
        "urls": {
            "US West: Los Angeles, CA": "rtmp://lax.contribute.live-video.net/app",
            "US East: New York, NY": "rtmp://jfk.contribute.live-video.net/app",
            "Europe: London, UK": "rtmp://lhr.contribute.live-video.net/app",
        },
        "custom_url": False,
    },
    "X (Twitter)": _GENERIC, "Restream.IO": _GENERIC, "Vimeo": _GENERIC, "BoxCast": _GENERIC,
    "Castr": _GENERIC, "AfreecaTV": _GENERIC, "Bilibili": _GENERIC, "DouYu": _GENERIC, "Weibo": _GENERIC,
}
# A streaming XML file loaded at the factory of this simulated unit.
CUSTOM_PLATFORMS: dict[str, dict[str, Any]] = {
    "My Platform": {
        "file": "Custom.xml", "servers": ["My Server"], "urls": {"My Server": "srt://192.168.8.51"},
        "custom_url": True, "quality": STREAMING_QUALITY,
    },
}

DEFAULTS: dict[str, Any] = {
    "label": "",
    "locale": "en_US.UTF-8",
    "audio_meter": "PPM -20dB",
    "video_mode": "Auto",
    "platform": "YouTube RTMP",
    "server": "Primary",
    "quality": "Streaming High",
    "stream_key": "",
    "passphrase": "",
    "url": "rtmp://a.rtmp.youtube.com/live2",
    "monitor_audio_source": "Auto",
    "eth_priority": 1,
    "eth_dhcp": True,
    "eth_static_addresses": "10.0.0.2/255.255.255.0",
    "eth_static_gateway": "10.0.0.1",
    "eth_static_dns": "8.8.8.8, 8.8.4.4",
    "usb_priority": 0,
    "usb_dhcp": True,
    "usb_static_addresses": "10.0.0.2/255.255.255.0",
    "usb_static_gateway": "10.0.0.1",
    "usb_static_dns": "8.8.8.8, 8.8.4.4",
    "custom_files": "Custom.xml",
}

# state key -> (block, key on the wire). Bitrate, Duration and Cache Used are
# not here: they are never reported unasked.
REPORTED = {
    "label": ("IDENTITY", "Label"),
    "locale": ("UI SETTINGS", "Current Locale"),
    "audio_meter": ("UI SETTINGS", "Current Audio Meter"),
    "video_mode": ("STREAM SETTINGS", "Video Mode"),
    "platform": ("STREAM SETTINGS", "Current Platform"),
    "server": ("STREAM SETTINGS", "Current Server"),
    "quality": ("STREAM SETTINGS", "Current Quality Level"),
    "stream_key": ("STREAM SETTINGS", "Stream Key"),
    "passphrase": ("STREAM SETTINGS", "Password"),
    "url": ("STREAM SETTINGS", "Current URL"),
    "monitor_audio_source": ("AUDIO SETTINGS", "Current Monitor Out Audio Source"),
    "stream_status": ("STREAM STATE", "Status"),
    "eth_priority": ("NETWORK INTERFACE 0", "Priority"),
    "eth_dhcp": ("NETWORK INTERFACE 0", "Dynamic IP"),
    "eth_static_addresses": ("NETWORK INTERFACE 0", "Static Addresses"),
    "eth_static_gateway": ("NETWORK INTERFACE 0", "Static Gateway"),
    "eth_static_dns": ("NETWORK INTERFACE 0", "Static DNS Servers"),
    "usb_priority": ("NETWORK INTERFACE 1", "Priority"),
    "usb_dhcp": ("NETWORK INTERFACE 1", "Dynamic IP"),
    "usb_static_addresses": ("NETWORK INTERFACE 1", "Static Addresses"),
    "usb_static_gateway": ("NETWORK INTERFACE 1", "Static Gateway"),
    "usb_static_dns": ("NETWORK INTERFACE 1", "Static DNS Servers"),
}
INTERFACE_PREFIX = {0: "eth", 1: "usb"}
DUMPABLE = (
    "PROTOCOL PREAMBLE", "IDENTITY", "VERSION", "NETWORK", "NETWORK INTERFACE 0",
    "NETWORK INTERFACE 1", "UI SETTINGS", "STREAM SETTINGS", "STREAM XML", "STREAM STATE",
    "AUDIO SETTINGS",
)


def _escape(item: str) -> str:
    return item.replace("\\", "\\\\").replace(",", "\\,")


def _join(items: list[str]) -> str:
    return ", ".join(_escape(item) for item in items)


def _word(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _duration(seconds: int) -> str:
    minutes, ss = divmod(max(0, int(seconds)), 60)
    hours, mm = divmod(minutes, 60)
    days, hh = divmod(hours, 24)
    return f"{days:02d}:{hh:02d}:{mm:02d}:{ss:02d}"


class _Session:
    """One connection's block still being read."""

    def __init__(self) -> None:
        self.block: tuple[str, list[str]] | None = None


class BlackmagicStreamingEncoderSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "blackmagic_streaming_encoder",
        "name": "Blackmagic Streaming Encoder Simulator",
        "category": "streaming",
        "transport": "tcp",
        "default_port": 9977,
        "initial_state": {
            "model": "Blackmagic Streaming Encoder HD",
            "unique_id": "00112233445566778899AABBCCDDEEFF",
            "product_id": "BE73",
            "hardware_version": "0100",
            "software_version": "0123ABCD",
            "software_release": "3.5",
            "protocol_version": "1.2",
            # DEFAULTS, written out so the simulator's checks can read them.
            "label": "Blackmagic Streaming Encoder HD",
            "locale": "en_US.UTF-8",
            "audio_meter": "PPM -20dB",
            "video_mode": "Auto",
            "platform": "YouTube RTMP",
            "server": "Primary",
            "quality": "Streaming High",
            "stream_key": "",
            "passphrase": "",
            "url": "rtmp://a.rtmp.youtube.com/live2",
            "monitor_audio_source": "Auto",
            "eth_priority": 1,
            "eth_dhcp": True,
            "eth_static_addresses": "10.0.0.2/255.255.255.0",
            "eth_static_gateway": "10.0.0.1",
            "eth_static_dns": "8.8.8.8, 8.8.4.4",
            "usb_priority": 0,
            "usb_dhcp": True,
            "usb_static_addresses": "10.0.0.2/255.255.255.0",
            "usb_static_gateway": "10.0.0.1",
            "usb_static_dns": "8.8.8.8, 8.8.4.4",
            "custom_files": "Custom.xml",
            "stream_status": "Idle",
            "bitrate": 0,
            "duration_s": 0,
            "cache_used": 0,
            "phone_tethered": False,
        },
        "controls": [
            {"type": "select", "key": "stream_status", "label": "Stream Status",
             "options": ["Idle", "Connecting", "Streaming", "Interrupted", "Flushing"]},
            {"type": "indicator", "key": "bitrate", "label": "Bitrate (bps)"},
            {"type": "indicator", "key": "duration_s", "label": "Duration (s)"},
            {"type": "indicator", "key": "platform", "label": "Platform"},
            {"type": "select", "key": "video_mode", "label": "Video Mode",
             "options": ["Auto", "1080p23.98", "1080p24", "1080p25", "1080p29.97", "1080p30", "1080p50",
                         "1080p59.94", "1080p60", "720p25", "720p30", "720p50", "720p60"]},
            {"type": "toggle", "key": "phone_tethered", "label": "Phone Tethered"},
        ],
        "error_modes": {
            # Declared here so a platform before 0.37.0, which offers no
            # built-in one, can silence the simulated unit too.
            "communication_timeout": {
                "label": "Stops responding",
                "description": "The encoder stops answering anything, as a unit that has hung.",
                "behavior": "no_response",
            },
            "platform_refuses": {
                "label": "Platform refuses the stream",
                "description": "A started stream goes from Connecting to Interrupted and stays there.",
            },
        },
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._sessions: dict[str, _Session] = {}
        self._task_clients: dict[asyncio.Task, str] = {}
        self._dirty: dict[str, list[str]] = {}
        self._flush_handle: asyncio.Handle | None = None
        self._tick_task: asyncio.Task | None = None
        model = str((config or {}).get("model") or "").strip()
        if model:
            super().set_state("model", model)
            super().set_state("label", model)

    # ── Server ──

    async def start(self, port: int) -> None:
        await super().start(port)
        self._tick_task = asyncio.ensure_future(self._tick_loop())

    async def stop(self) -> None:
        task, self._tick_task = self._tick_task, None
        if task is not None:
            task.cancel()
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        await super().stop()

    async def _read_messages(self, reader, buffer: bytearray | None = None):
        # One line at a time, blank lines kept: a blank line ends a block, and
        # the base's line reader drops it.
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30.0)
        except asyncio.TimeoutError:
            return []
        if not raw:
            return None
        return [raw.rstrip(b"\r\n")]

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
        self._sessions[client_id] = _Session()
        return self._status_report()

    def _session(self) -> _Session:
        try:
            cid = self._task_clients.get(asyncio.current_task(), LOCAL)
        except RuntimeError:
            cid = LOCAL
        session = self._sessions.get(cid)
        if session is None:
            session = _Session()
            self._sessions[cid] = session
        return session

    # ── The unit's lists ──

    def _modes(self) -> list[str]:
        return UHD_MODES if "4K" in str(self.get_state("model")) else HD_MODES

    def _custom(self) -> dict[str, dict[str, Any]]:
        files = {f.strip() for f in str(self.get_state("custom_files") or "").split(",") if f.strip()}
        return {name: spec for name, spec in CUSTOM_PLATFORMS.items() if spec["file"] in files}

    def _platform(self, name: Any = None) -> dict[str, Any]:
        name = self.get_state("platform") if name is None else name
        return PLATFORMS.get(name) or self._custom().get(name) or _GENERIC

    def _qualities(self, name: Any = None) -> list[str]:
        return self._platform(name).get("quality", ALL_QUALITY)

    def _default_interface(self) -> int:
        # The higher priority wins among the interfaces with an address.
        candidates = [(int(self.get_state("eth_priority") or 0), 0, 0)]
        if self.get_state("phone_tethered"):
            candidates.append((int(self.get_state("usb_priority") or 0), -1, 1))
        return max(candidates)[2]

    # ── Blocks ──

    @staticmethod
    def _block(header: str, pairs: list[tuple[str, Any]]) -> bytes:
        return (header + ":\n" + "".join(f"{k}: {_word(v)}\n" for k, v in pairs) + "\n").encode("utf-8")

    def _status_report(self) -> bytes:
        return b"".join(self._full(header) for header in DUMPABLE) + b"END PRELUDE:\n\n"

    def _full(self, header: str) -> bytes:
        g = self.get_state
        if header == "PROTOCOL PREAMBLE":
            return self._block(header, [("Version", g("protocol_version"))])
        if header == "IDENTITY":
            return self._block(header, [("Model", g("model")), ("Label", g("label")), ("Unique ID", g("unique_id"))])
        if header == "VERSION":
            return self._block(header, [
                ("Product ID", g("product_id")), ("Hardware Version", g("hardware_version")),
                ("Software Version", g("software_version")), ("Software Release", g("software_release")),
            ])
        if header == "NETWORK":
            return self._block(header, [("Interface Count", 2), ("Default Interface", self._default_interface())])
        if header.startswith("NETWORK INTERFACE "):
            return self._interface_block(int(header.rsplit(" ", 1)[1]), None)
        if header == "UI SETTINGS":
            return self._block(header, [
                ("Available Locales", _join(LOCALES)), ("Current Locale", g("locale")),
                ("Available Audio Meters", _join(AUDIO_METERS)), ("Current Audio Meter", g("audio_meter")),
            ])
        if header == "STREAM SETTINGS":
            platform = self._platform()
            return self._block(header, [
                ("Available Video Modes", _join(self._modes())),
                ("Video Mode", g("video_mode")),
                ("Current Platform", g("platform")),
                ("Current Server", g("server")),
                ("Current Quality Level", g("quality")),
                ("Stream Key", g("stream_key")),
                ("Password", g("passphrase")),
                ("Current URL", g("url")),
                ("Customizable URL", bool(platform.get("custom_url"))),
                ("Available Default Platforms", _join(list(PLATFORMS))),
                ("Available Custom Platforms", _join(list(self._custom()))),
                ("Available Servers", _join(platform["servers"])),
                ("Available Quality Levels", _join(self._qualities())),
            ])
        if header == "STREAM XML":
            return self._block(header, [("Files", g("custom_files"))])
        if header == "STREAM STATE":
            return self._block(header, [
                ("Status", g("stream_status")), ("Bitrate", g("bitrate")),
                ("Duration", _duration(g("duration_s") or 0)), ("Cache Used", g("cache_used")),
            ])
        if header == "AUDIO SETTINGS":
            return self._block(header, [
                ("Current Monitor Out Audio Source", g("monitor_audio_source")),
                ("Available Monitor Out Audio Sources", _join(MONITOR_SOURCES)),
            ])
        return b""

    def _interface_block(self, iid: int, keys: list[str] | None) -> bytes:
        prefix = INTERFACE_PREFIX[iid]
        g = self.get_state
        if iid == 0:
            name, mac = "Ethernet", "00:11:22:33:44:55"
            current = ("192.168.1.10/255.255.255.0", "192.168.1.1", "192.168.1.1, 8.8.8.8, 8.8.4.4")
        else:
            name, mac = "USBEthernet", "00:00:00:00:00:00"
            current = (
                ("172.20.10.2/255.255.255.240", "172.20.10.1", "172.20.10.1")
                if g("phone_tethered") else ("0.0.0.0/255.255.0.0", "0.0.0.0", "")
            )
        pairs = [
            ("Name", name), ("Priority", g(f"{prefix}_priority")), ("MAC Address", mac),
            ("Dynamic IP", bool(g(f"{prefix}_dhcp"))), ("Current Addresses", current[0]),
            ("Current Gateway", current[1]), ("Current DNS Servers", current[2]),
            ("Static Addresses", g(f"{prefix}_static_addresses")),
            ("Static Gateway", g(f"{prefix}_static_gateway")),
            ("Static DNS Servers", g(f"{prefix}_static_dns")),
        ]
        if keys is not None:
            pairs = [pair for pair in pairs if pair[0] in keys]
        return self._block(f"NETWORK INTERFACE {iid}", pairs)

    # ── Change reports ──

    def set_state(self, key: str, value: Any) -> None:
        old = self.get_state(key)
        super().set_state(key, value)
        if old == value:
            return
        if key in REPORTED:
            header, wire = REPORTED[key]
            self._mark(header, wire)
            if key.endswith("_priority"):
                self._mark("NETWORK", "Default Interface")
            if key == "platform":
                for wire in ("Customizable URL", "Available Servers", "Available Quality Levels"):
                    self._mark("STREAM SETTINGS", wire)
        elif key == "phone_tethered":
            for wire in ("Current Addresses", "Current Gateway", "Current DNS Servers"):
                self._mark("NETWORK INTERFACE 1", wire)
            self._mark("NETWORK", "Default Interface")
        elif key == "custom_files":
            self._mark("STREAM XML", "Files")
            self._mark("STREAM SETTINGS", "Available Custom Platforms")

    def _mark(self, header: str, wire: str) -> None:
        keys = self._dirty.setdefault(header, [])
        if wire not in keys:
            keys.append(wire)
        if self._flush_handle is None:
            try:
                self._flush_handle = asyncio.get_running_loop().call_soon(self._flush)
            except RuntimeError:
                self._dirty.clear()

    def _flush(self) -> None:
        self._flush_handle = None
        dirty, self._dirty = self._dirty, {}
        out = b""
        for header, keys in dirty.items():
            out += self._partial(header, keys)
        if out:
            for cid in list(self._clients):
                asyncio.ensure_future(self.push_to(cid, out))

    def _partial(self, header: str, keys: list[str]) -> bytes:
        if header.startswith("NETWORK INTERFACE "):
            return self._interface_block(int(header.rsplit(" ", 1)[1]), keys)
        full = self._full(header).decode("utf-8").split("\n")[1:]
        pairs = []
        for line in full:
            name, sep, value = line.partition(": ")
            if sep and name in keys:
                pairs.append((name, value))
        return self._block(header, pairs)

    # ── The stream ──

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self._tick()
            except Exception:
                logger.exception("%s: stream tick failed", self.name)

    def _tick(self) -> None:
        status = self.get_state("stream_status")
        if status == "Connecting":
            self.set_state("stream_status", "Interrupted" if "platform_refuses" in self.active_errors else "Streaming")
        elif status == "Flushing":
            self.set_state("stream_status", "Idle")
        if self.get_state("stream_status") == "Streaming":
            nominal = BITRATES.get(str(self.get_state("quality")), 6_000_000)
            super().set_state("duration_s", int(self.get_state("duration_s") or 0) + 1)
            super().set_state("bitrate", int(nominal * random.uniform(0.92, 1.05)))
            super().set_state("cache_used", random.randint(0, 3))
        elif self.get_state("stream_status") == "Idle":
            super().set_state("bitrate", 0)
            super().set_state("cache_used", 0)

    # ── Requests ──

    def handle_command(self, data: bytes) -> bytes | None:
        session = self._session()
        text = data.decode("utf-8", errors="replace").rstrip("\r")
        if session.block is not None:
            if text.strip():
                session.block[1].append(text)
                return None
            header, lines = session.block
            session.block = None
            return self._request(header, lines)
        if not text.strip():
            return None
        stripped = text.strip()
        if stripped.endswith(":"):
            session.block = (stripped[:-1].strip(), [])
            return None
        return b"NACK\n\n"

    def _request(self, header: str, lines: list[str]) -> bytes:
        if header.startswith("STREAM XML ") and lines:
            return self._load_xml(header[len("STREAM XML "):].strip())
        pairs: list[tuple[str, str]] = []
        for line in lines:
            name, sep, value = line.partition(":")
            if not sep:
                return b"NACK\n\n"
            pairs.append((name.strip(), value.strip()))
        if not pairs:
            if header not in DUMPABLE:
                return b"NACK\n\n"
            return b"ACK\n\n" + self._full(header)
        handler = {
            "IDENTITY": self._set_identity, "UI SETTINGS": self._set_ui,
            "STREAM SETTINGS": self._set_stream, "STREAM STATE": self._set_state,
            "AUDIO SETTINGS": self._set_audio, "SHUTDOWN": self._shutdown,
            "STREAM XML": self._set_xml, "NETWORK": lambda pairs: [],
        }.get(header)
        match = re.fullmatch(r"NETWORK INTERFACE ([01])", header)
        if match:
            iid = int(match.group(1))
            return self._answer(header, self._set_interface(iid, pairs))
        if handler is None:
            return b"NACK\n\n"
        return self._answer(header, handler(pairs))

    def _answer(self, header: str, kept: list[str]) -> bytes:
        """ACK, then the existing value of every key whose value was not taken."""
        out = b"ACK\n\n"
        if kept:
            out += self._partial(header, kept)
        return out

    def _choose(self, key: str, wire: str, value: str, allowed: list[str], kept: list[str]) -> None:
        if value in allowed:
            self.set_state(key, value)
        else:
            kept.append(wire)

    def _set_identity(self, pairs):
        kept: list[str] = []
        for name, value in pairs:
            if name == "Label":
                self.set_state("label", value)
        return kept

    def _set_ui(self, pairs):
        kept: list[str] = []
        for name, value in pairs:
            if name == "Current Locale":
                self._choose("locale", name, value, LOCALES, kept)
            elif name == "Current Audio Meter":
                self._choose("audio_meter", name, value, AUDIO_METERS, kept)
        return kept

    def _set_audio(self, pairs):
        kept: list[str] = []
        for name, value in pairs:
            if name == "Current Monitor Out Audio Source":
                self._choose("monitor_audio_source", name, value, MONITOR_SOURCES, kept)
        return kept

    def _set_stream(self, pairs):
        kept: list[str] = []
        for name, value in pairs:
            if name == "Video Mode":
                self._choose("video_mode", name, value, self._modes(), kept)
            elif name == "Current Platform":
                if value not in list(PLATFORMS) + list(self._custom()):
                    kept.append(name)
                    continue
                if value != self.get_state("platform"):
                    spec = self._platform(value)
                    server = spec["servers"][0]
                    self.set_state("platform", value)
                    self.set_state("server", server)
                    self.set_state("url", spec["urls"].get(server, ""))
                    if self.get_state("quality") not in self._qualities(value):
                        self.set_state("quality", "Streaming High")
            elif name == "Current Server":
                spec = self._platform()
                if value in spec["servers"]:
                    self.set_state("server", value)
                    if not spec.get("custom_url"):
                        self.set_state("url", spec["urls"].get(value, ""))
                else:
                    kept.append(name)
            elif name == "Current Quality Level":
                self._choose("quality", name, value, self._qualities(), kept)
            elif name == "Stream Key":
                self.set_state("stream_key", value)
            elif name == "Password":
                self.set_state("passphrase", value)
            elif name == "Current URL":
                if self._platform().get("custom_url"):
                    self.set_state("url", value)
                else:
                    kept.append(name)
        return kept

    def _set_state(self, pairs):
        for name, value in pairs:
            if name != "Action":
                continue
            status = self.get_state("stream_status")
            if value == "Start" and status in ("Idle", "Flushing"):
                super().set_state("duration_s", 0)
                self.set_state("stream_status", "Connecting")
            elif value == "Stop" and status != "Idle":
                self.set_state("stream_status", "Idle")
        return []

    def _set_interface(self, iid: int, pairs):
        prefix = INTERFACE_PREFIX[iid]
        kept: list[str] = []
        for name, value in pairs:
            if name == "Priority":
                if value.isdigit():
                    self.set_state(f"{prefix}_priority", int(value))
                else:
                    kept.append(name)
            elif name == "Dynamic IP":
                if value in ("true", "false"):
                    self.set_state(f"{prefix}_dhcp", value == "true")
                else:
                    kept.append(name)
            elif name in ("Static Addresses", "Static Gateway", "Static DNS Servers"):
                key = {"Static Addresses": "static_addresses", "Static Gateway": "static_gateway",
                       "Static DNS Servers": "static_dns"}[name]
                self.set_state(f"{prefix}_{key}", value)
        return kept

    def _set_xml(self, pairs):
        params = dict(pairs)
        action = params.get("Action", "")
        if action == "Remove All":
            self.set_state("custom_files", "")
        elif action == "Remove":
            gone = {f.strip() for f in params.get("Files", "").split(",")}
            left = [f.strip() for f in str(self.get_state("custom_files") or "").split(",")
                    if f.strip() and f.strip() not in gone]
            self.set_state("custom_files", ", ".join(left))
        self._fix_platform()
        return []

    def _load_xml(self, filename: str) -> bytes:
        files = [f.strip() for f in str(self.get_state("custom_files") or "").split(",") if f.strip()]
        if filename and filename not in files:
            files.append(filename)
            self.set_state("custom_files", ", ".join(files))
        return b"ACK\n\n"

    def _fix_platform(self) -> None:
        if self.get_state("platform") not in list(PLATFORMS) + list(self._custom()):
            self._set_stream([("Current Platform", "YouTube RTMP")])

    def _shutdown(self, pairs):
        action = dict(pairs).get("Action", "")
        if action == "Factory Reset":
            for key, value in DEFAULTS.items():
                self.set_state(key, value)
            self.set_state("label", self.get_state("model"))
        if action in ("Reboot", "Factory Reset"):
            self.set_state("stream_status", "Idle")
            self._close_later()
        return []

    def _close_later(self) -> None:
        """Close every connection once the answer is out, as a restart does."""

        def close() -> None:
            for writer in list(self._clients.values()):
                writer.close()

        try:
            asyncio.get_running_loop().call_later(0.2, close)
        except RuntimeError:
            pass
