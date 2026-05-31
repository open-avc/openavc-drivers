"""
OpenAVC TurtleAV Darwin Control driver.

The Darwin Control (model CTL100AL) is an H.265/H.264, 1080p60 4:4:4
AV-over-IP matrix controller. One controller orchestrates up to 762 video
encoders (TX) and 762 video decoders (RX) on a Video LAN, plus video walls.
Each sub-unit is modelled as an OpenAVC *child entity* of this one device
(state keyed ``device.<id>.<type>.<padded_id>.<prop>``), so the whole matrix
is driven through a single OpenAVC device.

It shares a firmware lineage with the Chazy Control, so the wire protocol is
the same family — but Darwin is a strict simplification: no Dante, no CEC, no
per-endpoint GPIO/relay/PHY, no generation split, and none of the Chazy
Control Pro modules (media / group / event / schedule / preset). It adds a
few endpoint controls Chazy lacks: TX H.264/H.265 + bitrate + main/sub stream
+ PCM/AAC audio encoding, and RX HDCP mode / screen pause / no-signal standby
/ front-panel-button lock / 20-slot KVM hotkeys.

Transport / protocol (confirmed against live hardware, FW 1.50.02, telnet 23):

* On connect the controller sends Telnet IAC option negotiation
  (``IAC WILL SGA / WILL ECHO / DONT ECHO / DO BINARY``) followed by a welcome
  banner (``Welcome To Controller(h) Terminal Control System``) and a
  ``CONTROLLER> `` prompt. No login is required. The driver strips all Telnet
  IAC sequences and never answers them.
* Strictly request/response, one command per line terminated with CRLF. The
  controller echoes the command, emits the response, then re-prints the
  ``CONTROLLER> `` prompt. The prompt is the end-of-response marker.
* Most ``SET ...`` commands reply with a single ``[SUCCESS]<msg>.`` or
  ``[ERROR]<msg>.`` line. ``[ERROR]`` is a deterministic protocol rejection,
  surfaced to the caller, never retried.
* Status queries reply with a multi-line banner wrapped in 64-char ``====``
  sentinel lines. The brand in banner headers is firmware-dependent
  (``Controller(h)`` on 1.50.02, ``DARWIN CONTROL`` on 2.03.19) so parsers key
  off row layout, never the brand. Numeric fields are zero-padded on the wire
  (IP ``169.254.020.001``); the driver normalises IPs back to ``169.254.20.1``.

Child enumeration on connect: encoders and decoders are read from ``GET
STATUS`` and reconciled into the platform child registry; video walls are read
from ``GET WALL STATUS``, so walls already configured in the controller's web
GUI are discovered, not only the ones this driver creates. TX encoding
settings (bitrate / codec / audio format) are *not* reported by the device, so
the driver tracks them write-through from each ``SET`` ack.

License: MIT.
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

# End-of-response marker the controller prints after every reply (and the
# connect banner). No trailing newline.
PROMPT = b"CONTROLLER> "

# Telnet IAC bytes.
_IAC = 0xFF
_SB = 0xFA
_SE = 0xF0
_NEGOTIATE = (0xFB, 0xFC, 0xFD, 0xFE)  # WILL / WONT / DO / DONT (+1 option byte)

ENC_MAX = 762
DEC_MAX = 762
WALL_MAX = 9          # video wall handles 01-09
WALL_PRESET_MAX = 9   # presets per wall 01-09

# Routing signal types (Darwin: no AUDIO / CEC / MEDIA — audio is embedded).
SIGNAL_TYPES = ["ALL", "VIDEO", "IR", "RS232", "USB"]

# Wire-index -> human label maps (for write-through state + IDE help text).
BITRATE_LABELS = {"0": "1Mb", "1": "4Mb", "2": "8Mb", "3": "16Mb", "4": "20Mb"}
CODEC_LABELS = {"0": "h264", "1": "h265"}
ROTATE_DEG = {"0": "0", "1": "90", "2": "180", "3": "270"}
RES_LABELS = {
    "00": "Bypass", "01": "1080p@60", "02": "1080p@50", "03": "1080p@30",
    "04": "1080p@25", "05": "1080p@24", "06": "720p@60", "07": "720p@50",
    "08": "576p@50", "09": "480p@60", "10": "640x480@60", "11": "800x600@60",
    "12": "1024x768@60", "13": "1280x800@60", "14": "1280x1024@60",
    "15": "1366x768@60", "16": "1440x900@60", "17": "1600x1200@60",
    "18": "1680x1050@60", "19": "1920x1200@60",
}
EDID_LABELS = {
    "00": "HDMI 1080p@60 2CH PCM", "01": "HDMI 720p@60 2CH PCM",
    "02": "DVI 1280x1024@60", "03": "DVI 1920x1080@60", "04": "DVI 1920x1200@60",
    "05": "HDMI 1920x1200@60 2CH PCM", "06": "Copy EDID",
    "07": "User EDID 1", "08": "User EDID 2",
}


def _enc_state_vars() -> dict[str, dict[str, Any]]:
    """Per-encoder (TX) state. `online` + `label` are injected by the platform.

    The bitrate / codec keys are write-through (the device reports them in no
    banner) — populated from SET acks. ``audio_format`` IS reported in the
    GET STATUS roster (AudioFormat column), so it is polled there; the SET-ack
    write-through only bridges the window until the next poll.
    """
    return {
        "name": {"type": "string", "label": "Device Name"},
        "firmware": {"type": "string", "label": "Firmware"},
        "net": {"type": "boolean", "label": "Network Link", "cloud_priority": "high"},
        "signal_present": {
            "type": "boolean", "label": "HDMI Signal", "cloud_priority": "high",
        },
        "edid": {"type": "string", "label": "EDID"},
        "audio_input": {
            "type": "enum", "values": ["HDMI", "ANA"], "label": "Audio Input",
        },
        "multicast": {"type": "boolean", "label": "Multicast"},
        # Reported in the GET STATUS roster (AudioFormat column); also write-through.
        "audio_format": {
            "type": "enum", "values": ["PCM", "AAC"], "label": "Audio Format",
            "cloud_priority": "low",
        },
        "fpled": {"type": "string", "label": "Front-Panel LED", "cloud_priority": "low"},
        "guest_enabled": {"type": "boolean", "label": "Serial Guest", "cloud_priority": "low"},
        "guest_baud": {"type": "string", "label": "Guest Baud", "cloud_priority": "low"},
        "guest_framing": {"type": "string", "label": "Guest Framing", "cloud_priority": "low"},
        "mac": {"type": "string", "label": "MAC"},
        "ip": {"type": "string", "label": "IP Address"},
        "gateway": {"type": "string", "label": "Gateway", "cloud_priority": "low"},
        "subnet_mask": {"type": "string", "label": "Subnet Mask", "cloud_priority": "low"},
        # write-through (set-only; device reports these in no banner)
        "bitrate": {"type": "string", "label": "Encode Bitrate", "cloud_priority": "low"},
        "mainstream_codec": {
            "type": "enum", "values": ["h264", "h265"], "label": "Mainstream Codec",
            "cloud_priority": "low",
        },
        "mainstream_audio": {
            "type": "boolean", "label": "Mainstream Audio", "cloud_priority": "low",
        },
        "substream_codec": {
            "type": "enum", "values": ["h264", "h265"], "label": "Substream Codec",
            "cloud_priority": "low",
        },
        "substream_w": {"type": "integer", "label": "Substream Width", "cloud_priority": "low"},
        "substream_h": {"type": "integer", "label": "Substream Height", "cloud_priority": "low"},
        "substream_bitrate": {
            "type": "string", "label": "Substream Bitrate", "cloud_priority": "low",
        },
        "substream_audio": {
            "type": "boolean", "label": "Substream Audio", "cloud_priority": "low",
        },
    }


def _dec_state_vars() -> dict[str, dict[str, Any]]:
    """Per-decoder (RX) state. `online` + `label` are injected by the platform."""
    return {
        "name": {"type": "string", "label": "Device Name"},
        "firmware": {"type": "string", "label": "Firmware"},
        "net": {"type": "boolean", "label": "Network Link", "cloud_priority": "high"},
        "hpd": {"type": "boolean", "label": "Display Connected (HPD)", "cloud_priority": "high"},
        "mode": {"type": "enum", "values": ["MX", "VW"], "label": "Output Mode"},
        "resolution": {"type": "string", "label": "Output Resolution"},
        "rotate": {"type": "enum", "values": ["0", "90", "180", "270"], "label": "Rotation"},
        "source": {"type": "integer", "label": "Source (FromIn)", "cloud_priority": "high"},
        "lock_video": {"type": "integer", "label": "Video Lock", "cloud_priority": "low"},
        "lock_ir": {"type": "integer", "label": "IR Lock", "cloud_priority": "low"},
        "lock_rs232": {"type": "integer", "label": "RS232 Lock", "cloud_priority": "low"},
        "lock_usb": {"type": "integer", "label": "USB Lock", "cloud_priority": "low"},
        "multicast": {"type": "boolean", "label": "Multicast"},
        "video_output": {"type": "boolean", "label": "Output On", "cloud_priority": "high"},
        "video_mute": {"type": "boolean", "label": "Output Muted"},
        "pause": {"type": "boolean", "label": "Output Paused"},
        "auto": {"type": "boolean", "label": "Output Auto", "cloud_priority": "low"},
        "video_lost_timeout": {
            "type": "integer", "label": "No-Signal Standby (min)", "cloud_priority": "low",
        },
        "hdcp": {
            "type": "enum", "values": ["SNK", "SRC", "OFF", "H14", "H22"],
            "label": "HDCP Mode (Osp)", "cloud_priority": "low",
        },
        "aspect": {"type": "string", "label": "Aspect", "cloud_priority": "low"},
        "ir_enabled": {"type": "boolean", "label": "Rear IR", "cloud_priority": "low"},
        "button": {"type": "boolean", "label": "Front-Panel Button", "cloud_priority": "low"},
        "fpled": {"type": "string", "label": "Front-Panel LED", "cloud_priority": "low"},
        "guest_enabled": {"type": "boolean", "label": "Serial Guest", "cloud_priority": "low"},
        "guest_baud": {"type": "string", "label": "Guest Baud", "cloud_priority": "low"},
        "guest_framing": {"type": "string", "label": "Guest Framing", "cloud_priority": "low"},
        "mac": {"type": "string", "label": "MAC"},
        "ip": {"type": "string", "label": "IP Address"},
        "gateway": {"type": "string", "label": "Gateway", "cloud_priority": "low"},
        "subnet_mask": {"type": "string", "label": "Subnet Mask", "cloud_priority": "low"},
    }


class DarwinControlDriver(BaseDriver):
    """TurtleAV Darwin Control AV-over-IP matrix controller."""

    DRIVER_INFO = {
        "id": "darwin_control",
        "name": "TurtleAV Darwin Control",
        "manufacturer": "TurtleAV",
        "category": "switcher",
        "version": "1.1.7",
        "author": "OpenAVC",
        "min_platform_version": "0.13.0",
        "description": (
            "Controls a TurtleAV Darwin Control (CTL100AL) H.265 AV-over-IP "
            "matrix controller and every sub-unit it manages: video encoders "
            "(TX), decoders (RX), and video walls."
        ),
        "source_url": "https://turtleav.com/",
        "tags": ["av-over-ip", "matrix", "encoder", "decoder", "video-wall", "h265"],
        "verified": False,
        "simulated": True,
        "protocols": ["darwin_telnet"],
        "ports": [23],
        "transport": "tcp",
        "discovery": {
            # The controller's only on-wire identity is the telnet connect
            # banner ("Welcome To Controller(h) Terminal Control System" on FW
            # 1.50.02; the brand becomes "DARWIN CONTROL" on FW 2.03.19). The
            # tcp_probe below matches that token, which is what lets an
            # UNINSTALLED Darwin identify straight from the catalog: hostname
            # and port are shared with the Chazy controllers, so they can't
            # disambiguate on their own. The banner arrives after Telnet IAC
            # negotiation in a later TCP segment; the probe runner accumulates
            # segments (it no longer stops at the first read), so this matches
            # the same banner the companion reads. The sibling companion
            # (darwin_control_discovery.py) stays as a belt-and-suspenders path
            # with identical token logic and richer fragmented-banner handling.
            "tcp_probe": {
                "port": 23,
                # "Controller(h)" (FW 1.50.02, verified live) or any
                # "DARWIN"-bearing brand (FW 2.03.19). Never matches the Chazy
                # tokens "CHAZY CONTROL" / "TAV-CHAZY-CLTPRO".
                "expect_regex": r"(?i)Welcome To\s+(?:DARWIN|Controller\(h\))",
                "timeout_ms": 4000,
                "extract_manufacturer": "TurtleAV",
                "extract": {
                    "model": {
                        "regex": r"Welcome To\s+(.+?)\s+Terminal Control System",
                        "group": 1,
                    },
                    "firmware": {
                        "regex": r"FW Version:\s*([0-9][0-9A-Za-z.\-]*)",
                        "group": 1,
                    },
                },
            },
            "python": "./darwin_control_discovery.py",
            "hostname": ["^controller(\\.local)?$"],
            "port_open": [23],
            "manufacturer_alias": ["turtleav", "darwin"],
        },
        "compatible_models": [
            {
                "manufacturer": "TurtleAV",
                "models": ["Darwin Control (CTL100AL)"],
                "confidence": "full",
                "notes": (
                    "Verified end-to-end against live hardware (FW 1.50.02) with a "
                    "live HDMI source on the TX and a display on the RX: connect, "
                    "discovery, child enumeration, status parsing, "
                    "enrolment/idempotency, and the operational SET surface "
                    "(routing, output/mute/pause/OSD/auto, resolution, rotate, "
                    "no-signal standby, HDCP/Osp SNK/SRC/OFF/H14/H22, TX encoding, "
                    "LED/FPLED, audio input, IR, GPIO). OSD, mute, pause, "
                    "resolution, rotation, output on/off, and a single-RX video "
                    "wall tile were confirmed rendering on the attached display; "
                    "the source-binding commands (video-wall class/matrix source, "
                    "KVM hotkey) were accepted with a live source. Decoder "
                    "video-wall mode (MODE VW) requires the RX assigned to a wall "
                    "with an applied class region first. Only reboot/reset/network "
                    "commands were held back on the live unit; their wire formats "
                    "match the manufacturer reference. Multi-endpoint routing and "
                    "multi-screen walls need additional TX/RX units to exercise."
                ),
            },
        ],
        "help": {
            "overview": (
                "The Darwin Control orchestrates an entire Darwin HD AV-over-IP "
                "system. Add it as one device; its encoders, decoders, and "
                "video walls appear as child entities under the Child Entities "
                "tab."
            ),
            "setup": (
                "1. Connect the controller's Control LAN (LAN2) to your "
                "network. The default address is 192.168.6.100; it also "
                "advertises as controller.local.\n"
                "2. Telnet on port 23 is enabled by default with no login.\n"
                "3. Enter the controller's IP in the device config; leave the "
                "port at 23.\n"
                "4. Encoders and decoders are discovered automatically on "
                "connect from GET STATUS. Use the Search command to find new "
                "TX/RX on the Video LAN, then Add Auto All to register them.\n"
                "5. Video walls already configured on the controller are also "
                "listed automatically."
            ),
        },
        "default_config": {
            "host": "",
            "port": 23,
            "poll_interval": 10,
            "detail_poll_interval": 60,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer", "default": 23, "label": "Telnet Port",
                "description": "Darwin Control telnet API port (default 23).",
            },
            "poll_interval": {
                "type": "integer", "default": 10, "min": 0,
                "label": "Status Poll Interval (sec)",
                "description": (
                    "How often to poll GET STATUS for the encoder/decoder "
                    "roster and system state. 0 disables polling."
                ),
            },
            "detail_poll_interval": {
                "type": "integer", "default": 60, "min": 0,
                "label": "Detail Poll Interval (sec)",
                "description": (
                    "How often to refresh full per-encoder/per-decoder state "
                    "(GET ENC/DEC [n] STATUS), batched. 0 disables detail "
                    "polling (roster-only)."
                ),
            },
        },
        "state_variables": {
            "firmware": {"type": "string", "label": "Firmware Version"},
            "power": {"type": "boolean", "label": "Power"},
            "ir": {"type": "boolean", "label": "IR Loop"},
            "rs232_baud": {"type": "string", "label": "RS-232 Baud"},
            "encoder_count": {"type": "integer", "label": "Encoders"},
            "decoder_count": {"type": "integer", "label": "Decoders"},
            "lan1_dhcp": {"type": "boolean", "label": "LAN1 (Video) DHCP"},
            "lan1_ip": {"type": "string", "label": "LAN1 (Video) IP"},
            "lan1_gateway": {"type": "string", "label": "LAN1 Gateway"},
            "lan1_subnet_mask": {"type": "string", "label": "LAN1 Subnet Mask"},
            "lan1_mac": {"type": "string", "label": "LAN1 MAC"},
            "lan2_dhcp": {"type": "boolean", "label": "LAN2 (Control) DHCP"},
            "lan2_ip": {"type": "string", "label": "LAN2 (Control) IP"},
            "lan2_gateway": {"type": "string", "label": "LAN2 Gateway"},
            "lan2_subnet_mask": {"type": "string", "label": "LAN2 Subnet Mask"},
            "lan2_mac": {"type": "string", "label": "LAN2 MAC"},
            "telnet_port": {"type": "string", "label": "Telnet Port"},
            "ssh": {"type": "boolean", "label": "SSH Enabled"},
            "https": {"type": "boolean", "label": "HTTPS Enabled"},
            "web": {"type": "boolean", "label": "Web GUI Enabled"},
            "hostname": {"type": "string", "label": "Hostname"},
            "gpio1_dir": {"type": "string", "label": "GPIO1 Direction"},
            "gpio1_level": {"type": "integer", "label": "GPIO1 Level"},
            "gpio2_dir": {"type": "string", "label": "GPIO2 Direction"},
            "gpio2_level": {"type": "integer", "label": "GPIO2 Level"},
            "gpio3_dir": {"type": "string", "label": "GPIO3 Direction"},
            "gpio3_level": {"type": "integer", "label": "GPIO3 Level"},
            "gpio4_dir": {"type": "string", "label": "GPIO4 Direction"},
            "gpio4_level": {"type": "integer", "label": "GPIO4 Level"},
        },
        "child_entity_types": {
            "encoder": {
                "label": "Encoder",
                "label_plural": "Encoders",
                "id_format": {"type": "integer", "min": 1, "max": ENC_MAX, "pad_width": 3},
                "state_variables": _enc_state_vars(),
                "summary_fields": ["name", "ip", "signal_present", "multicast"],
                "label_field": "name",
            },
            "decoder": {
                "label": "Decoder",
                "label_plural": "Decoders",
                "id_format": {"type": "integer", "min": 1, "max": DEC_MAX, "pad_width": 3},
                "state_variables": _dec_state_vars(),
                "summary_fields": ["name", "ip", "source", "mode", "hpd"],
                "label_field": "name",
            },
            "video_wall": {
                "label": "Video Wall",
                "label_plural": "Video Walls",
                "id_format": {"type": "integer", "min": 1, "max": WALL_MAX, "pad_width": 2},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "columns": {"type": "integer", "label": "Columns"},
                    "rows": {"type": "integer", "label": "Rows"},
                },
                "summary_fields": ["name", "columns", "rows"],
                "label_field": "name",
            },
        },
        # Command surface — see _COMMAND_TEMPLATES for the wire format of each.
        "commands": {},  # populated below by _build_commands()
    }

    def __init__(
        self, device_id: str, config: dict[str, Any], state, events,
    ) -> None:
        self._rx_buffer = b""
        self._iac_state = "normal"
        self._responses: asyncio.Queue[str] = asyncio.Queue()
        self._cmd_lock = asyncio.Lock()
        self._poll_count = 0
        super().__init__(device_id, config, state, events)

    # ── Connection lifecycle ──

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 23))

        self._rx_buffer = b""
        self._iac_state = "normal"
        while not self._responses.empty():
            self._responses.get_nowait()

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=None,
            timeout=5.0,
            name=self.device_id,
        )

        # Consume the connect banner (ends at the first prompt).
        try:
            await asyncio.wait_for(self._responses.get(), timeout=8.0)
        except asyncio.TimeoutError:
            await self.transport.close()
            self.transport = None
            raise ConnectionError(
                f"[{self.device_id}] No banner/prompt from controller at "
                f"{host}:{port}"
            )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to Darwin Control at {host}:{port}")

        # Prime full state + child roster before steady-state polling.
        try:
            await self._poll_status()
        except Exception:
            log.exception(f"[{self.device_id}] Initial enumeration failed")
        try:
            await self.poll_children("encoder", self._fetch_encoder_detail)
            await self.poll_children("decoder", self._fetch_decoder_detail)
            await self._reconcile_walls()
        except Exception:
            log.exception(f"[{self.device_id}] Initial detail poll failed")

        poll_interval = self.config.get("poll_interval", 10)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self.set_state("connected", False)
        self._rx_buffer = b""
        self._iac_state = "normal"
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    # ── Telnet framing ──

    def _filter_telnet(self, data: bytes) -> bytes:
        """Strip Telnet IAC negotiation/subnegotiation, returning clean bytes.

        Stateful across calls so an IAC sequence split across TCP segments is
        handled. The controller only negotiates at connect, but we filter the
        whole stream defensively.
        """
        out = bytearray()
        for b in data:
            st = self._iac_state
            if st == "normal":
                if b == _IAC:
                    self._iac_state = "iac"
                else:
                    out.append(b)
            elif st == "iac":
                if b == _IAC:
                    out.append(_IAC)  # escaped literal 0xFF
                    self._iac_state = "normal"
                elif b == _SB:
                    self._iac_state = "sb"
                elif b in _NEGOTIATE:
                    self._iac_state = "iac_opt"  # one option byte follows
                else:
                    self._iac_state = "normal"  # standalone command (NOP/GA/...)
            elif st == "iac_opt":
                self._iac_state = "normal"  # consume the option byte
            elif st == "sb":
                if b == _IAC:
                    self._iac_state = "sb_iac"
            elif st == "sb_iac":
                self._iac_state = "normal" if b == _SE else "sb"
        return bytes(out)

    async def on_data_received(self, data: bytes) -> None:
        """Accumulate bytes; emit one queue item per prompt-delimited unit."""
        self._rx_buffer += self._filter_telnet(data)
        while True:
            idx = self._rx_buffer.find(PROMPT)
            if idx == -1:
                break
            unit = self._rx_buffer[:idx]
            self._rx_buffer = self._rx_buffer[idx + len(PROMPT):]
            text = unit.decode("latin-1", errors="replace")
            self._responses.put_nowait(text)

    @staticmethod
    def _strip_echo(response: str, sent: str) -> str:
        """Drop the leading echoed command line the controller prepends."""
        lines = response.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and lines[0].strip() == sent.strip():
            lines = lines[1:]
        return "\n".join(lines).strip("\n")

    async def _send_request(self, wire: str, timeout: float = 6.0) -> str:
        """Send one command line and return the controller's response text
        (echo stripped, prompt removed). Serialised so responses correlate.
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._cmd_lock:
            while not self._responses.empty():
                self._responses.get_nowait()
            await self.transport.send(wire.encode("ascii", errors="replace") + b"\r\n")
            try:
                raw = await asyncio.wait_for(self._responses.get(), timeout=timeout)
            except asyncio.TimeoutError as e:
                raise TimeoutError(
                    f"[{self.device_id}] No response to {wire!r}"
                ) from e
            return self._strip_echo(raw, wire)

    async def _send_set(self, wire: str, timeout: float = 6.0) -> str:
        """Send a command expected to ack with [SUCCESS]/[ERROR]. Raises on
        [ERROR] (a deterministic protocol rejection — not retried).
        """
        resp = await self._send_request(wire, timeout=timeout)
        for line in resp.splitlines():
            ls = line.strip()
            if ls.startswith("[ERROR]"):
                raise RuntimeError(ls[len("[ERROR]"):].strip().rstrip("."))
        return resp

    # ── Polling ──

    async def poll(self) -> None:
        await self._poll_status()
        detail_interval = self.config.get("detail_poll_interval", 60)
        poll_interval = self.config.get("poll_interval", 10) or 10
        self._poll_count += 1
        if detail_interval and detail_interval > 0:
            every = max(1, round(detail_interval / poll_interval))
            if self._poll_count % every == 0:
                await self.poll_children("encoder", self._fetch_encoder_detail)
                await self.poll_children("decoder", self._fetch_decoder_detail)
                await self._reconcile_walls()

    # ── send_command dispatch ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = dict(params or {})
        self._coerce_child_ids(command, params)

        if command in _LIFECYCLE_COMMANDS:
            return await self._handle_lifecycle(command, params)
        if command in _RESET_CONFIRM:
            return await self._send_with_confirm(_RESET_CONFIRM[command])
        if command == "search":
            return await self._do_search()
        if command == "add_auto_all":
            resp = await self._send_set("ADD AUTO ALL")
            await self._poll_status()
            return resp
        if command in ("add_dev_enc", "add_dev_dec"):
            # The ADD ack names only the search IP, and id 0 auto-assigns an id
            # the client can't know, so re-read the roster to register the new
            # child immediately under its controller-assigned id.
            resp = await self._send_set(_COMMAND_TEMPLATES[command].format(**params))
            await self._poll_status()
            return resp
        if command == "exit_guest":
            return await self._send_request("EXITGUEST")

        template = _COMMAND_TEMPLATES.get(command)
        if template is None:
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            raise ValueError(f"Unknown command: {command}")
        wire = template.format(**params)
        result = await self._send_set(wire)
        self._writethrough(command, params)
        return result

    def _coerce_child_ids(self, command: str, params: dict[str, Any]) -> None:
        """Coerce any child_id-typed param to a bare int for the wire format
        (the IDE may hand us a zero-padded string from the child picker).
        """
        cmd_def = self.DRIVER_INFO["commands"].get(command, {})
        for pname, pdef in cmd_def.get("params", {}).items():
            if pdef.get("type") == "child_id" and pname in params and params[pname] != "":
                try:
                    params[pname] = int(params[pname])
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"{command}: parameter {pname!r} must be an integer "
                        f"id, got {params[pname]!r}"
                    ) from e

    def _writethrough(self, command: str, params: dict[str, Any]) -> None:
        """Mirror set-only encoder encoding params into child state, since the
        device never reports them in GET ENC STATUS.
        """
        spec = _WRITETHROUGH.get(command)
        if not spec:
            return
        ctype, id_param, fn = spec
        try:
            cid = int(params[id_param])
        except (KeyError, TypeError, ValueError):
            return
        if cid and self.is_child_registered(ctype, cid):
            try:
                self.set_child_state_batch(ctype, cid, fn(params))
            except Exception:
                log.debug(f"[{self.device_id}] write-through failed", exc_info=True)

    async def _handle_lifecycle(self, command: str, params: dict[str, Any]) -> Any:
        """Create/delete/renumber commands: send the wire, then update the
        platform child registry on success.
        """
        spec = _LIFECYCLE_COMMANDS[command]
        wire = spec["template"].format(**params)
        resp = await self._send_set(wire)
        ctype = spec["child_type"]
        action = spec["action"]
        if action == "register":
            lid = int(params[spec["id_param"]])
            initial = {}
            if "name" in params and "name" in self.get_child_entity_types().get(
                ctype, {}
            ).get("state_variables", {}):
                initial["name"] = params["name"]
            self.register_child(ctype, lid, initial_state=initial or None)
        elif action == "deregister":
            lid = int(params[spec["id_param"]])
            self.deregister_child(ctype, lid)
        elif action == "renumber":
            old = int(params[spec["id_param"]])
            new = int(params[spec["new_id_param"]])
            if old != new and self.is_child_registered(ctype, old):
                prev = self.get_child_state(ctype, old)
                self.deregister_child(ctype, old)
                seed = {
                    k: v for k, v in prev.items()
                    if k in self.get_child_entity_types()[ctype]["state_variables"]
                }
                self.register_child(ctype, new, initial_state=seed or None)
        return resp

    async def _do_search(self) -> str:
        """Trigger a Video LAN device search and reconcile the result."""
        resp = await self._send_request("SEARCH", timeout=60.0)
        await self._poll_status()
        return resp

    async def _send_with_confirm(self, base: str) -> str:
        """Two-step confirmable command (factory resets). The controller
        replies to ``base`` with a 'Sure to RESET ...? Type "Yes" ...' question
        (terminated by the normal prompt), then we send ``Yes`` to proceed.
        """
        question = await self._send_request(base)
        if "confirm" not in question.lower() and '"Yes"' not in question:
            return question  # no confirmation requested; nothing more to send
        return await self._send_set("Yes")

    # ── refresh_children (IDE "Refresh from Device") ──

    async def refresh_children(self) -> dict[str, Any]:
        await self._poll_status()
        await self.poll_children("encoder", self._fetch_encoder_detail)
        await self.poll_children("decoder", self._fetch_decoder_detail)
        await self._reconcile_walls()
        return {
            "encoders": len(self.list_children("encoder")),
            "decoders": len(self.list_children("decoder")),
            "video_walls": len(self.list_children("video_wall")),
        }

    # ── Status refresh + child reconciliation ──

    async def _poll_status(self) -> None:
        """GET STATUS once: parse controller system state + the encoder/
        decoder roster, then reconcile the platform child registry. Also
        refreshes the controller's own GPIO state.
        """
        resp = await self._send_request("GET STATUS")
        parsed = _parse_status(resp)

        declared = self.DRIVER_INFO["state_variables"]
        sys_state = {k: v for k, v in parsed["system"].items() if k in declared}
        if sys_state:
            self.set_states(sys_state)

        self._reconcile_roster("encoder", parsed["encoders"])
        self._reconcile_roster("decoder", parsed["decoders"])
        self.set_state("encoder_count", len(self.list_children("encoder")))
        self.set_state("decoder_count", len(self.list_children("decoder")))

        try:
            gpio_resp = await self._send_request("GET GPIO 0 STATUS")
            gpio = {k: v for k, v in _parse_gpio(gpio_resp).items() if k in declared}
            if gpio:
                self.set_states(gpio)
        except Exception:
            log.debug(f"[{self.device_id}] GPIO poll failed", exc_info=True)

    def _reconcile_children(
        self,
        ctype: str,
        parsed_map: dict[int, dict[str, Any]],
        *,
        online_from_net: bool,
    ) -> None:
        """Register newly-seen children, update the light state of known ones,
        and deregister any that the controller no longer reports.
        """
        schema = self.get_child_entity_types()[ctype]["state_variables"]
        current = set(self.list_children(ctype))
        seen: set[int] = set()
        for cid, props in parsed_map.items():
            seen.add(cid)
            clean = {k: v for k, v in props.items() if k in schema}
            clean["online"] = bool(props.get("net", False)) if online_from_net else True
            if cid not in current:
                self.register_child(ctype, cid, initial_state=clean)
            else:
                self.set_child_state_batch(ctype, cid, clean)
        for cid in current - seen:
            self.deregister_child(ctype, cid)

    def _reconcile_roster(
        self, ctype: str, parsed_map: dict[int, dict[str, Any]]
    ) -> None:
        """Encoder/decoder roster reconcile (online derived from the net flag)."""
        self._reconcile_children(ctype, parsed_map, online_from_net=True)

    async def _reconcile_walls(self) -> None:
        """Enumerate video walls from GET WALL STATUS so walls built in the
        controller's web GUI are discovered, not only ones this driver creates.
        """
        try:
            resp = await self._send_request("GET WALL STATUS")
            self._reconcile_children(
                "video_wall", _parse_wall_status(resp), online_from_net=False
            )
        except Exception:
            log.debug(f"[{self.device_id}] wall enumeration failed", exc_info=True)

    async def _fetch_encoder_detail(
        self, ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        schema = self.get_child_entity_types()["encoder"]["state_variables"]
        for eid in ids:
            resp = await self._send_request(f"GET ENC {eid} STATUS")
            if "not exist" in resp or resp.lstrip().startswith("[ERROR]"):
                continue
            props = _parse_encoder_detail(resp)
            clean = {k: v for k, v in props.items() if k in schema}
            if clean:
                clean["online"] = bool(props.get("net", False))
                out[eid] = clean
        return out

    async def _fetch_decoder_detail(
        self, ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        schema = self.get_child_entity_types()["decoder"]["state_variables"]
        for did in ids:
            resp = await self._send_request(f"GET DEC {did} STATUS")
            if "not exist" in resp or resp.lstrip().startswith("[ERROR]"):
                continue
            props = _parse_decoder_detail(resp)
            clean = {k: v for k, v in props.items() if k in schema}
            if clean:
                clean["online"] = bool(props.get("net", False))
                out[did] = clean
        return out


# ── Banner parsers ──
#
# The controller emits fixed-width column banners. Columns can be blank
# (space-filled) when a value is unknown, so data is sliced by header word
# positions, not whitespace-split. All parsers are validated byte-for-byte
# against the live captures in tests/fixtures/darwin_control_banners.py.


def _column_starts(header: str) -> list[int]:
    """Start index of each column header word (each run of non-space)."""
    starts: list[int] = []
    in_word = False
    for i, ch in enumerate(header):
        if ch != " ":
            if not in_word:
                starts.append(i)
                in_word = True
        else:
            in_word = False
    return starts


def _split_columns(header: str, data: str) -> dict[str, str]:
    """Slice ``data`` at the column boundaries implied by ``header`` and return
    ``{header_word: value}``. The final column extends to end of line.
    """
    starts = _column_starts(header)
    words = header.split()
    out: dict[str, str] = {}
    for idx, word in enumerate(words):
        s = starts[idx]
        e = starts[idx + 1] if idx + 1 < len(starts) else len(data)
        out[word] = data[s:e].strip()
    return out


def _norm_ip(value: str) -> str:
    """Strip zero-padding from a dotted IP (169.254.010.001 -> 169.254.10.1)."""
    value = value.strip()
    parts = value.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return ".".join(str(int(p)) for p in parts)
    return value


def _is_on(value: str) -> bool:
    return value.strip() == "On"


def _split_flag_pair(value: str) -> tuple[bool, bool]:
    """Parse an 'X/Y' flag pair like 'On /Off' -> (True, False)."""
    a, _, b = value.strip().partition("/")
    return a.strip() == "On", b.strip() == "On"


def _guest_from_tokens(tokens: list[str]) -> dict[str, Any]:
    """Parse the trailing serial-guest tokens of a >>LED / >>ASPECT value row.

    The wire form is ``<SGEn> /<Br>/<Bit>`` e.g. ``Off /9/8n1`` which splits to
    tokens ``["Off", "/9/8n1"]``.
    """
    out: dict[str, Any] = {}
    if not tokens:
        return out
    out["guest_enabled"] = tokens[0].strip() == "On"
    rest = "".join(tokens[1:]).lstrip("/")
    parts = rest.split("/") if rest else []
    if len(parts) >= 1 and parts[0]:
        out["guest_baud"] = parts[0]
    if len(parts) >= 2 and parts[1]:
        out["guest_framing"] = parts[1]
    return out


def _parse_status(text: str) -> dict[str, Any]:
    """Parse a GET STATUS banner into system state + encoder/decoder rosters."""
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    system: dict[str, Any] = {}
    encoders: dict[int, dict[str, Any]] = {}
    decoders: dict[int, dict[str, Any]] = {}
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        s = line.strip()
        if s.startswith("FW Version:"):
            system["firmware"] = s.split(":", 1)[1].strip()
        elif s.startswith("Power") and "Baud" in s:
            parts = lines[i + 1].split() if i + 1 < n else []
            if len(parts) >= 3:
                system["power"] = parts[0] == "On"
                system["ir"] = parts[1] == "On"
                system["rs232_baud"] = parts[2]
            i += 2
            continue
        elif "EDID" in line and "NET/Sig" in line and "AudioFormat" in line:
            header = line
            i += 1
            while i < n and lines[i].strip() and lines[i].strip() != "NONE":
                cols = _split_columns(header, lines[i])
                eid = cols.get("In", "")
                if eid.isdigit():
                    net, sig = _split_flag_pair(cols.get("NET/Sig", ""))
                    enc = {
                        "edid": cols.get("EDID", ""),
                        "ip": _norm_ip(cols.get("IP", "")),
                        "net": net,
                        "signal_present": sig,
                    }
                    # AudioFormat (PCM/AAC) is the only encoding param the device
                    # reports; include it when present so it self-heals on poll.
                    af = cols.get("AudioFormat", "").strip()
                    if af:
                        enc["audio_format"] = af
                    encoders[int(eid)] = enc
                i += 1
            continue
        elif "FromIn" in line and "NET/HDMI" in line:
            header = line
            i += 1
            while i < n and lines[i].strip() and lines[i].strip() != "NONE":
                cols = _split_columns(header, lines[i])
                did = cols.get("Out", "")
                if did.isdigit():
                    net, hpd = _split_flag_pair(cols.get("NET/HDMI", ""))
                    frm = cols.get("FromIn", "")
                    res = cols.get("Res", "")
                    decoders[int(did)] = {
                        "source": int(frm) if frm.isdigit() else 0,
                        "ip": _norm_ip(cols.get("IP", "")),
                        "net": net,
                        "hpd": hpd,
                        "resolution": RES_LABELS.get(res, res),
                        "mode": cols.get("Mode", ""),
                        "hdcp": cols.get("Osp", ""),
                    }
                i += 1
            continue
        elif line.startswith("LAN") and "DHCP" in line and "SubnetMask" in line:
            header = line
            i += 1
            while i < n and lines[i].strip():
                row = lines[i]
                if row.lstrip().startswith("(static"):
                    i += 1
                    continue
                cols = _split_columns(header, row)
                lan = cols.get("LAN", "")
                pfx = "lan1" if ("POE" in lan or lan.startswith("01")) else (
                    "lan2" if ("CTRL" in lan or lan.startswith("02")) else None)
                if pfx:
                    system[f"{pfx}_dhcp"] = cols.get("DHCP", "") == "On"
                    system[f"{pfx}_ip"] = _norm_ip(cols.get("IP", ""))
                    system[f"{pfx}_gateway"] = _norm_ip(cols.get("Gateway", ""))
                    system[f"{pfx}_subnet_mask"] = _norm_ip(cols.get("SubnetMask", ""))
                i += 1
            continue
        elif line.startswith("Telnet") and "HTTPS" in line:
            parts = lines[i + 1].split() if i + 1 < n else []
            if len(parts) >= 4:
                system["telnet_port"] = parts[0].lstrip("0") or "0"
                system["ssh"] = parts[1] != "Off"
                system["https"] = parts[2] != "Off"
                system["web"] = parts[3] != "Off"
            if len(parts) >= 6:
                system["lan1_mac"] = parts[4]
                system["lan2_mac"] = parts[5]
            i += 2
            continue
        elif s == "Domain Name":
            if i + 1 < n:
                system["hostname"] = lines[i + 1].strip()
            i += 2
            continue
        i += 1
    return {"system": system, "encoders": encoders, "decoders": decoders}


def _parse_gpio(text: str) -> dict[str, Any]:
    """Parse GET GPIO 0 STATUS into gpio{1..4}_dir / gpio{1..4}_level."""
    out: dict[str, Any] = {}
    for ln in text.split("\n"):
        parts = ln.split()
        if len(parts) >= 4 and parts[0].isdigit():
            num = int(parts[0])
            if 1 <= num <= 4:
                out[f"gpio{num}_dir"] = parts[1]
                get = parts[-1]
                out[f"gpio{num}_level"] = int(get) if get.lstrip("-").isdigit() else 0
    return out


def _parse_wall_status(text: str) -> dict[int, dict[str, Any]]:
    """Parse GET WALL STATUS. Each wall is a ``VW Col Row CfgSel Name`` header +
    a data row. An empty controller replies ``No Video Wall`` (parses to {}).
    """
    out: dict[int, dict[str, Any]] = {}
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    for i, ln in enumerate(lines):
        if ln.startswith("VW") and "Col" in ln and "Row" in ln and "Name" in ln:
            if i + 1 >= len(lines):
                continue
            cols = _split_columns(ln, lines[i + 1])
            vid = cols.get("VW", "")
            if not vid.isdigit():
                continue
            name = cols.get("Name", "").strip()
            if name == "NULL":
                name = ""
            col = cols.get("Col", "")
            row = cols.get("Row", "")
            out[int(vid)] = {
                "name": name,
                "columns": int(col) if col.isdigit() else 0,
                "rows": int(row) if row.isdigit() else 0,
            }
    return out


def _detail_sections(text: str) -> tuple[str | None, str, list[str]]:
    """Return (header_line, data_line, body_lines) for a GET ENC/DEC STATUS
    banner — the 'In/Out ... Name' header, the single data row, and the
    indented >> sub-block lines after it (sentinels stripped).
    """
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    for i, ln in enumerate(lines):
        st = ln.lstrip()
        if (st.startswith("In ") or st.startswith("Out ")) and "Name" in ln:
            data = lines[i + 1] if i + 1 < len(lines) else ""
            body = [b for b in lines[i + 2:] if "====" not in b]
            return ln, data, body
    return None, "", []


def _detail_blocks(body: list[str]) -> list[tuple[str, list[str]]]:
    """Group sub-block lines into (label_line, [value_lines]) pairs. Each '>>'
    line starts a block; following non-empty lines are its values.
    """
    blocks: list[tuple[str, list[str]]] = []
    cur: tuple[str, list[str]] | None = None
    for ln in body:
        if ">>" in ln:
            cur = (ln, [])
            blocks.append(cur)
        elif cur is not None and ln.strip():
            cur[1].append(ln)
    return blocks


def _parse_encoder_detail(text: str) -> dict[str, Any]:
    """Parse a GET ENC [n] STATUS banner for a single encoder."""
    header, data, body = _detail_sections(text)
    out: dict[str, Any] = {}
    if header and data:
        c = _split_columns(header, data)
        out["net"] = _is_on(c.get("Net", ""))
        out["signal_present"] = _is_on(c.get("Sig", ""))
        if c.get("Ver"):
            out["firmware"] = c.get("Ver", "")
        out["edid"] = c.get("EDID", "")
        if c.get("Aud"):
            out["audio_input"] = c.get("Aud", "")
        out["multicast"] = _is_on(c.get("MCast", ""))
        if c.get("Name"):
            out["name"] = c.get("Name", "")
    for label, values in _detail_blocks(body):
        v0 = values[0] if values else ""
        if ">>LED" in label:
            toks = v0.split()
            if toks:
                out["fpled"] = toks[0]
                out.update(_guest_from_tokens(toks[1:]))
        elif ">>IP" in label:
            toks = v0.split()
            if len(toks) >= 1:
                out["ip"] = _norm_ip(toks[0])
            if len(toks) >= 2:
                out["gateway"] = _norm_ip(toks[1])
            if len(toks) >= 3:
                out["subnet_mask"] = _norm_ip(toks[2])
        elif ">>MAC" in label:
            if v0.strip():
                out["mac"] = v0.strip()
    return out


def _parse_decoder_detail(text: str) -> dict[str, Any]:
    """Parse a GET DEC [n] STATUS banner for a single decoder."""
    header, data, body = _detail_sections(text)
    out: dict[str, Any] = {}
    if header and data:
        c = _split_columns(header, data)
        out["net"] = _is_on(c.get("Net", ""))
        out["hpd"] = _is_on(c.get("HPD", ""))
        if c.get("Ver"):
            out["firmware"] = c.get("Ver", "")
        out["mode"] = c.get("Mode", "")
        res = c.get("Res", "")
        out["resolution"] = RES_LABELS.get(res, res)
        out["rotate"] = ROTATE_DEG.get(c.get("Rotate", ""), c.get("Rotate", ""))
        if c.get("Name"):
            out["name"] = c.get("Name", "")
    for label, values in _detail_blocks(body):
        v0 = values[0] if values else ""
        toks = v0.split()
        if ">>Fr" in label:
            # "001    000/000/000/000      On"  -> source, locks, mcast
            if toks and toks[0].isdigit():
                out["source"] = int(toks[0])
            if len(toks) >= 2:
                locks = [t for t in toks[1].split("/") if t != ""]
                names = ["lock_video", "lock_ir", "lock_rs232", "lock_usb"]
                for key, val in zip(names, locks):
                    out[key] = int(val) if val.isdigit() else 0
            if len(toks) >= 3:
                out["multicast"] = toks[2] == "On"
        elif ">>ASPECT" in label:
            # "Maintain  SNK  On  On  9  Off /9/8n1"
            if len(toks) >= 1:
                out["aspect"] = toks[0]
            if len(toks) >= 2:
                out["hdcp"] = toks[1]
            if len(toks) >= 3:
                out["ir_enabled"] = toks[2] == "On"
            if len(toks) >= 4:
                out["button"] = toks[3] == "On"
            if len(toks) >= 5:
                out["fpled"] = toks[4]
            out.update(_guest_from_tokens(toks[5:]))
        elif ">>Video" in label:
            # "On  Off  Off  On  0" -> output, mute, pause, auto, lost-timeout
            if len(toks) >= 1:
                out["video_output"] = toks[0] == "On"
            if len(toks) >= 2:
                out["video_mute"] = toks[1] == "On"
            if len(toks) >= 3:
                out["pause"] = toks[2] == "On"
            if len(toks) >= 4:
                out["auto"] = toks[3] == "On"
            if len(toks) >= 5 and toks[4].lstrip("-").isdigit():
                out["video_lost_timeout"] = int(toks[4])
        elif ">>IP" in label:
            if len(toks) >= 1:
                out["ip"] = _norm_ip(toks[0])
            if len(toks) >= 2:
                out["gateway"] = _norm_ip(toks[1])
            if len(toks) >= 3:
                out["subnet_mask"] = _norm_ip(toks[2])
        elif ">>MAC" in label:
            if v0.strip():
                out["mac"] = v0.strip()
    return out


# ── Command surface ──
#
# Plain templated commands: command name -> wire format. Child-id params are
# coerced to bare ints before formatting. Lifecycle (register/deregister),
# reset-confirm, search/add-auto, and exit_guest are handled specially in
# send_command.

_COMMAND_TEMPLATES: dict[str, str] = {
    # System / network
    "set_ir": "SET IR {state}",
    "set_rs232_baud": "SET RS232BAUDRATE {baud}",
    "gpio_dir": "SET GPIO {gpio} DIR {direction}",
    "gpio_level": "SET GPIO {gpio} LEVEL {level}",
    "add_dev_enc": "ADD DEV {dev} ENC {encoder_id}",
    "add_dev_dec": "ADD DEV {dev} DEC {decoder_id}",
    "add_dev_reset": "ADD DEV RESET",
    "search_reset": "SEARCH RESET",
    "net_dhcp": "SET NETWORK {lan} DHCP {state}",
    "net_static_ip": "SET NETWORK {lan} STATIC IP {ip}",
    "net_static_gateway": "SET NETWORK {lan} STATIC GATEWAY {gateway}",
    "net_static_mask": "SET NETWORK {lan} STATIC MASK {mask}",
    "net_reboot": "SET NETWORK REBOOT",
    "net_telnet": "SET NETWORK TELNET {state}",
    "net_telnet_port": "SET NETWORK TELNET PORT {port}",
    "net_ssh": "SET NETWORK SSH {state}",
    "net_ssh_port": "SET NETWORK SSH PORT {port}",
    "net_https": "SET NETWORK HTTPS {state}",
    "net_web": "SET NETWORK WEB {state}",
    "net_hostname": "SET NETWORK DNS {hostname}",

    # Encoder (TX)
    "enc_set_name": "SET ENC {encoder_id} NAME {name}",
    "enc_led": "SET ENC {encoder_id} LED {state}",
    "enc_led_timeout": "SET ENC {encoder_id} LED ON 90",
    "enc_fpled": "SET ENC {encoder_id} FPLED {fl}",
    "enc_audio_input": "SET ENC {encoder_id} AUDIO INPUT {source}",
    "enc_edid_default": "SET ENC {encoder_id} EDID DEFAULT {edid}",
    "enc_edid_copy": "SET ENC {encoder_id} EDID COPY {decoder_id}",
    "enc_ir_vol": "SET ENC {encoder_id} IR VOL {voltage}",
    "enc_stream_bitrate": "SET ENC {encoder_id} STREAM BITRATE {rate}",
    "enc_mainstream": "SET ENC {encoder_id} MAINSTREAM E {type} A {audio}",
    "enc_substream": "SET ENC {encoder_id} SUBSTREAM E {type} H {sh} V {sv} B {rate} A {audio}",
    "enc_audio_format": "SET ENC {encoder_id} AUDIO FORMAT {format}",
    "enc_guest_config": "SET ENC {encoder_id} GUEST {state} BR {baud} BIT {bits}",
    "enc_guest_start": "SET ENC {encoder_id} GUEST",
    "enc_ipmode": "SET ENC {encoder_id} IPMODE {mode}",
    "enc_static_ip": "SET ENC {encoder_id} STATIC IP {ip}",
    "enc_static_gateway": "SET ENC {encoder_id} STATIC GATEWAY {gateway}",
    "enc_static_mask": "SET ENC {encoder_id} STATIC MASK {mask}",
    "enc_network_reboot": "SET ENC {encoder_id} NETWORK REBOOT",
    "enc_reboot": "SET ENC {encoder_id} REBOOT",
    "enc_reset": "SET ENC {encoder_id} RESET",

    # Decoder (RX)
    "dec_set_name": "SET DEC {decoder_id} NAME {name}",
    "dec_switch": "SET DEC {decoder_id} SWITCH {encoder_id} {signal}",
    "dec_output": "SET DEC {decoder_id} OUTPUT {state}",
    "dec_output_mute": "SET DEC {decoder_id} OUTPUT MUTE {state}",
    "dec_output_pause": "SET DEC {decoder_id} OUTPUT PAUSE {state}",
    "dec_output_osd": "SET DEC {decoder_id} OUTPUT OSD {state}",
    "dec_output_osd_90": "SET DEC {decoder_id} OUTPUT OSD ON 90",
    "dec_output_auto": "SET DEC {decoder_id} OUTPUT AUTO {state}",
    "dec_output_resolution": "SET DEC {decoder_id} OUTPUT RESOLUTION {res}",
    "dec_output_rotate": "SET DEC {decoder_id} OUTPUT ROTATE {rotate}",
    "dec_output_lost": "SET DEC {decoder_id} OUTPUT LOST {time}",
    "dec_mode": "SET DEC {decoder_id} MODE {mode}",
    "dec_button": "SET DEC {decoder_id} BUTTON {state}",
    "dec_ir": "SET DEC {decoder_id} IR {state}",
    "dec_ir_vol": "SET DEC {decoder_id} IR VOL {voltage}",
    "dec_stream": "SET DEC {decoder_id} STREAM {mode}",
    "dec_hdcp": "SET DEC {decoder_id} OSP {mode}",
    "dec_led": "SET DEC {decoder_id} LED {state}",
    "dec_led_timeout": "SET DEC {decoder_id} LED ON 90",
    "dec_fpled": "SET DEC {decoder_id} FPLED {fl}",
    "dec_hotkey_set": (
        "SET DEC {decoder_id} HOTKEY {nn} KEY {k0} {k1} ACTION {action} SRC {encoder_id}"
    ),
    "dec_hotkey_del": "SET DEC {decoder_id} HOTKEY {nn} DEL",
    "dec_guest_config": "SET DEC {decoder_id} GUEST {state} BR {baud} BIT {bits}",
    "dec_guest_start": "SET DEC {decoder_id} GUEST",
    "dec_ipmode": "SET DEC {decoder_id} IPMODE {mode}",
    "dec_static_ip": "SET DEC {decoder_id} STATIC IP {ip}",
    "dec_static_gateway": "SET DEC {decoder_id} STATIC GATEWAY {gateway}",
    "dec_static_mask": "SET DEC {decoder_id} STATIC MASK {mask}",
    "dec_network_reboot": "SET DEC {decoder_id} NETWORK REBOOT",
    "dec_reboot": "SET DEC {decoder_id} REBOOT",
    "dec_reset": "SET DEC {decoder_id} RESET",

    # Video wall (non-lifecycle)
    "wall_set_name": "SET WALL {wall_id} NAME {name}",
    "wall_set_size": "SET WALL {wall_id} C {columns} R {rows}",
    "wall_assign_dec": "SET WALL {wall_id} DEC {decoder_id} H {h} V {v}",
    "wall_preset_create": "CREATE WALL {wall_id} PRESET {preset}",
    "wall_preset_delete": "DELETE WALL {wall_id} PRESET {preset}",
    "wall_preset_set_name": "SET WALL {wall_id} PRESET {preset} NAME {name}",
    "wall_preset_apply": "APPLY WALL {wall_id} PRESET {preset}",
    "wall_preset_class": "SET WALL {wall_id} PRESET {preset} CLASS {cls} H {h} V {v}",
    "wall_preset_class_source": (
        "SET WALL {wall_id} PRESET {preset} CLASS {cls} SOURCE {encoder_id}"
    ),
    "wall_preset_matrix": "SET WALL {wall_id} PRESET {preset} MATRIX H {h} V {v}",
    "wall_preset_matrix_source": (
        "SET WALL {wall_id} PRESET {preset} MATRIX H {h} V {v} SOURCE {encoder_id}"
    ),
    "wall_width_bezel": "SET WALL {wall_id} H {h} V {v} WIDTH BEZEL BW {bw} IW {iw}",
    "wall_height_bezel": "SET WALL {wall_id} H {h} V {v} HEIGHT BEZEL BH {bh} IH {ih}",
}

# Lifecycle commands: send the wire, then mutate the platform child registry.
_LIFECYCLE_COMMANDS: dict[str, dict[str, Any]] = {
    "enc_delete": {
        "template": "SET ENC {encoder_id} DELETE", "child_type": "encoder",
        "action": "deregister", "id_param": "encoder_id",
    },
    "enc_set_id": {
        "template": "SET ENC {encoder_id} ID {new_id}", "child_type": "encoder",
        "action": "renumber", "id_param": "encoder_id", "new_id_param": "new_id",
    },
    "dec_delete": {
        "template": "SET DEC {decoder_id} DELETE", "child_type": "decoder",
        "action": "deregister", "id_param": "decoder_id",
    },
    "dec_set_id": {
        "template": "SET DEC {decoder_id} ID {new_id}", "child_type": "decoder",
        "action": "renumber", "id_param": "decoder_id", "new_id_param": "new_id",
    },
    "wall_create": {
        "template": "CREATE WALL HANDLE {wall_id}", "child_type": "video_wall",
        "action": "register", "id_param": "wall_id",
    },
    "wall_delete": {
        "template": "DELETE WALL HANDLE {wall_id}", "child_type": "video_wall",
        "action": "deregister", "id_param": "wall_id",
    },
}

# Destructive commands that require a 'Yes' confirmation.
_RESET_CONFIRM: dict[str, str] = {
    "reset_system_confirm": "SET RESET",
    "reset_network_confirm": "SET RESET NETWORK",
    "reset_all_confirm": "SET RESET ALL",
}

# Write-through: encoder encoding params the device never reports. command ->
# (child_type, id_param, params -> child-state dict).
_WRITETHROUGH: dict[str, tuple[str, str, Any]] = {
    "enc_stream_bitrate": (
        "encoder", "encoder_id",
        lambda p: {"bitrate": BITRATE_LABELS.get(str(p.get("rate")), str(p.get("rate")))},
    ),
    "enc_mainstream": (
        "encoder", "encoder_id",
        lambda p: {
            "mainstream_codec": CODEC_LABELS.get(str(p.get("type")), str(p.get("type"))),
            "mainstream_audio": str(p.get("audio", "")).upper() == "ON",
        },
    ),
    "enc_substream": (
        "encoder", "encoder_id",
        lambda p: {
            "substream_codec": CODEC_LABELS.get(str(p.get("type")), str(p.get("type"))),
            "substream_w": int(p["sh"]) if str(p.get("sh", "")).isdigit() else 0,
            "substream_h": int(p["sv"]) if str(p.get("sv", "")).isdigit() else 0,
            "substream_bitrate": BITRATE_LABELS.get(str(p.get("rate")), str(p.get("rate"))),
            "substream_audio": str(p.get("audio", "")).upper() == "ON",
        },
    ),
    "enc_audio_format": (
        "encoder", "encoder_id", lambda p: {"audio_format": str(p.get("format", ""))},
    ),
}


def _build_commands() -> dict[str, dict[str, Any]]:
    """Build the DRIVER_INFO['commands'] dict (labels, params, help) the IDE
    renders. Wire formats live in _COMMAND_TEMPLATES / _LIFECYCLE_COMMANDS.
    """
    onoff = {"type": "enum", "values": ["ON", "OFF"], "required": True}
    lan = {"type": "enum", "values": ["LAN1", "LAN2"], "required": True,
           "help": "LAN1 = Video LAN (PoE), LAN2 = Control LAN."}
    voltage = {"type": "enum", "values": ["5V", "12V"], "required": True}
    fpled = {"type": "enum", "values": ["0", "9"], "required": True,
             "help": "0 = always on, 9 = on 90s then off."}
    ipmode = {"type": "enum", "values": ["DHCP", "STATIC"], "required": True}
    baud = {"type": "enum", "values": [str(x) for x in range(10)], "required": True,
            "help": "0:300 1:600 2:1200 3:2400 4:4800 5:9600 6:19200 7:38400 8:57600 9:115200"}
    bits = {"type": "string", "required": True, "help": "Data/parity/stop, e.g. 8n1"}
    codec = {"type": "enum", "values": ["0", "1"], "required": True, "help": "0:h264 1:h265"}

    def enc_id(label="Encoder"):
        return {"type": "child_id", "child_type": "encoder", "required": True, "label": label}

    def dec_id(label="Decoder"):
        return {"type": "child_id", "child_type": "decoder", "required": True, "label": label}

    def wall_id():
        return {"type": "child_id", "child_type": "video_wall", "required": True, "label": "Video Wall"}

    res_help = "; ".join(f"{k}:{v}" for k, v in RES_LABELS.items())
    edid_help = "; ".join(f"{k}:{v}" for k, v in EDID_LABELS.items())

    cmds: dict[str, dict[str, Any]] = {
        # ── System ──
        "set_ir": {"label": "Controller: IR Loop", "params": {"state": onoff},
                   "help": "Enable/disable the controller IR loop."},
        "set_rs232_baud": {"label": "Controller: Set RS-232 Baud", "params": {
            "baud": {"type": "enum", "values": ["0", "1", "2", "3", "4"], "required": True,
                     "help": "0:115200 1:57600 2:38400 3:19200 4:9600"}},
            "help": "Set the controller RS-232 baud rate."},
        "reset_system_confirm": {"label": "Factory Reset: System Settings", "params": {},
                                 "help": "Reset controller system settings (auto-confirms with Yes)."},
        "reset_network_confirm": {"label": "Factory Reset: Network Settings", "params": {},
                                  "help": "Reset controller network settings (auto-confirms with Yes)."},
        "reset_all_confirm": {"label": "Factory Reset: System + Network", "params": {},
                              "help": "Reset all controller settings (auto-confirms with Yes)."},
        "gpio_dir": {"label": "GPIO: Direction", "params": {
            "gpio": {"type": "enum", "values": ["1", "2", "3", "4"], "required": True},
            "direction": {"type": "enum", "values": ["IN", "OUT"], "required": True}}},
        "gpio_level": {"label": "GPIO: Output Level", "params": {
            "gpio": {"type": "enum", "values": ["1", "2", "3", "4"], "required": True},
            "level": {"type": "enum", "values": ["Low", "High"], "required": True}},
            "help": "Output level (only valid on output-direction GPIO)."},

        # ── Device management ──
        "search": {"label": "Search Video LAN", "params": {},
                   "help": "Scan the Video LAN for new encoders/decoders."},
        "add_auto_all": {"label": "Add All Found Devices", "params": {},
                         "help": "Enrol every device found by the last Search."},
        "add_dev_enc": {"label": "Add Device as Encoder", "params": {
            "dev": {"type": "integer", "required": True, "min": 1, "label": "Search Index"},
            "encoder_id": {"type": "integer", "required": True, "min": 0, "max": ENC_MAX,
                           "label": "Encoder ID (0 = auto)"}}},
        "add_dev_dec": {"label": "Add Device as Decoder", "params": {
            "dev": {"type": "integer", "required": True, "min": 1, "label": "Search Index"},
            "decoder_id": {"type": "integer", "required": True, "min": 0, "max": DEC_MAX,
                           "label": "Decoder ID (0 = auto)"}}},
        "add_dev_reset": {"label": "Clear All Devices", "params": {},
                          "help": "Remove all encoders/decoders/walls/search from the controller."},
        "search_reset": {"label": "Clear Search Results", "params": {}},

        # ── Network (controller) ──
        "net_dhcp": {"label": "Network: DHCP", "params": {"lan": lan, "state": onoff}},
        "net_static_ip": {"label": "Network: Static IP", "params": {
            "lan": lan, "ip": {"type": "string", "required": True}}},
        "net_static_gateway": {"label": "Network: Static Gateway", "params": {
            "lan": lan, "gateway": {"type": "string", "required": True}}},
        "net_static_mask": {"label": "Network: Static Subnet Mask", "params": {
            "lan": lan, "mask": {"type": "string", "required": True}}},
        "net_reboot": {"label": "Network: Reboot (apply changes)", "params": {}},
        "net_telnet": {"label": "Network: Telnet", "params": {"state": onoff}},
        "net_telnet_port": {"label": "Network: Telnet Port", "params": {
            "port": {"type": "integer", "required": True, "min": 22, "max": 65535}}},
        "net_ssh": {"label": "Network: SSH", "params": {"state": onoff}},
        "net_ssh_port": {"label": "Network: SSH Port", "params": {
            "port": {"type": "integer", "required": True, "min": 22, "max": 65535}}},
        "net_https": {"label": "Network: HTTPS", "params": {"state": onoff}},
        "net_web": {"label": "Network: Web GUI", "params": {"state": onoff}},
        "net_hostname": {"label": "Network: Hostname", "params": {
            "hostname": {"type": "string", "required": True,
                         "help": "Sets <hostname>.local; the controller restarts."}}},

        # ── Encoder (TX) ──
        "enc_set_name": {"label": "Encoder: Set Name", "params": {
            "encoder_id": enc_id(), "name": {"type": "string", "required": True}},
            "help": "Set an encoder's name (max 16 chars)."},
        "enc_set_id": {"label": "Encoder: Renumber", "params": {
            "encoder_id": enc_id(), "new_id": {"type": "integer", "required": True, "min": 1,
                                               "max": ENC_MAX, "label": "New ID"}}},
        "enc_delete": {"label": "Encoder: Delete", "params": {"encoder_id": enc_id()}},
        "enc_led": {"label": "Encoder: Power LED Flash", "params": {
            "encoder_id": enc_id(), "state": onoff}},
        "enc_led_timeout": {"label": "Encoder: Flash LED (90s)", "params": {"encoder_id": enc_id()}},
        "enc_fpled": {"label": "Encoder: Front-Panel LED", "params": {
            "encoder_id": enc_id(), "fl": fpled}},
        "enc_audio_input": {"label": "Encoder: Audio Input", "params": {
            "encoder_id": enc_id(), "source": {"type": "enum", "values": ["HDMI", "ANA"],
                                               "required": True}}},
        "enc_edid_default": {"label": "Encoder: Set Default EDID", "params": {
            "encoder_id": enc_id(), "edid": {"type": "enum", "values": list(EDID_LABELS),
                                             "required": True, "help": edid_help}}},
        "enc_edid_copy": {"label": "Encoder: Copy EDID from Decoder", "params": {
            "encoder_id": enc_id(), "decoder_id": dec_id()}},
        "enc_ir_vol": {"label": "Encoder: IR Voltage", "params": {
            "encoder_id": enc_id(), "voltage": voltage}},
        "enc_stream_bitrate": {"label": "Encoder: Encode Bitrate", "params": {
            "encoder_id": enc_id(), "rate": {"type": "enum", "values": list(BITRATE_LABELS),
                                             "required": True,
                                             "help": "0:1Mb 1:4Mb 2:8Mb 3:16Mb 4:20Mb"}}},
        "enc_mainstream": {"label": "Encoder: Mainstream Encode", "params": {
            "encoder_id": enc_id(), "type": codec, "audio": onoff}},
        "enc_substream": {"label": "Encoder: Substream Encode", "params": {
            "encoder_id": enc_id(), "type": codec,
            "sh": {"type": "integer", "required": True, "min": 320, "max": 640,
                   "label": "Width (even)"},
            "sv": {"type": "integer", "required": True, "min": 180, "max": 540,
                   "label": "Height (even)"},
            "rate": {"type": "enum", "values": list(BITRATE_LABELS), "required": True,
                     "help": "0:1Mb 1:4Mb 2:8Mb 3:16Mb 4:20Mb"},
            "audio": onoff}},
        "enc_audio_format": {"label": "Encoder: Audio Format", "params": {
            "encoder_id": enc_id(), "format": {"type": "enum", "values": ["PCM", "AAC"],
                                               "required": True}}},
        "enc_guest_config": {"label": "Encoder: Serial Guest Config", "params": {
            "encoder_id": enc_id(), "state": onoff, "baud": baud, "bits": bits}},
        "enc_guest_start": {"label": "Encoder: Start Serial Guest", "params": {"encoder_id": enc_id()},
                            "help": "Enter interactive serial guest mode (exit with Exit Serial Guest)."},
        "enc_ipmode": {"label": "Encoder: IP Mode", "params": {
            "encoder_id": enc_id(), "mode": ipmode}},
        "enc_static_ip": {"label": "Encoder: Static IP", "params": {
            "encoder_id": enc_id(), "ip": {"type": "string", "required": True}}},
        "enc_static_gateway": {"label": "Encoder: Static Gateway", "params": {
            "encoder_id": enc_id(), "gateway": {"type": "string", "required": True}}},
        "enc_static_mask": {"label": "Encoder: Static Subnet Mask", "params": {
            "encoder_id": enc_id(), "mask": {"type": "string", "required": True}}},
        "enc_network_reboot": {"label": "Encoder: Network Reboot", "params": {"encoder_id": enc_id()}},
        "enc_reboot": {"label": "Encoder: Reboot", "params": {"encoder_id": enc_id()}},
        "enc_reset": {"label": "Encoder: Factory Reset", "params": {"encoder_id": enc_id()}},

        # ── Decoder (RX) ──
        "dec_set_name": {"label": "Decoder: Set Name", "params": {
            "decoder_id": dec_id(), "name": {"type": "string", "required": True}},
            "help": "Set a decoder's name (max 16 chars)."},
        "dec_set_id": {"label": "Decoder: Renumber", "params": {
            "decoder_id": dec_id(), "new_id": {"type": "integer", "required": True, "min": 1,
                                               "max": DEC_MAX, "label": "New ID"}}},
        "dec_delete": {"label": "Decoder: Delete", "params": {"decoder_id": dec_id()}},
        "dec_switch": {"label": "Decoder: Route Source", "params": {
            "decoder_id": dec_id(), "encoder_id": enc_id("Encoder (0 = clear/unlock)"),
            "signal": {"type": "enum", "values": SIGNAL_TYPES, "required": True,
                       "help": "ALL follows; VIDEO/IR/RS232/USB lock that signal independently."}},
            "help": "Route an encoder to a decoder. encoder 0 clears (ALL) or unlocks (per-signal)."},
        "dec_output": {"label": "Decoder: Output", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_output_mute": {"label": "Decoder: Output Mute", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_output_pause": {"label": "Decoder: Output Pause", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_output_osd": {"label": "Decoder: ID OSD", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_output_osd_90": {"label": "Decoder: ID OSD (90s)", "params": {"decoder_id": dec_id()}},
        "dec_output_auto": {"label": "Decoder: Output Auto", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_output_resolution": {"label": "Decoder: Output Resolution", "params": {
            "decoder_id": dec_id(), "res": {"type": "enum", "values": list(RES_LABELS),
                                            "required": True, "help": res_help}}},
        "dec_output_rotate": {"label": "Decoder: Output Rotate", "params": {
            "decoder_id": dec_id(), "rotate": {"type": "enum", "values": ["0", "1", "2", "3"],
                                               "required": True,
                                               "help": "0:0 1:90 2:180 3:270 degrees"}}},
        "dec_output_lost": {"label": "Decoder: No-Signal Standby", "params": {
            "decoder_id": dec_id(), "time": {"type": "integer", "required": True, "min": 0,
                                             "max": 60, "label": "Minutes (0 = never)"}}},
        "dec_mode": {"label": "Decoder: Output Mode", "params": {
            "decoder_id": dec_id(), "mode": {"type": "enum", "values": ["MX", "VW"],
                                             "required": True}}},
        "dec_button": {"label": "Decoder: Front-Panel Button", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_ir": {"label": "Decoder: Rear IR", "params": {"decoder_id": dec_id(), "state": onoff}},
        "dec_ir_vol": {"label": "Decoder: IR Voltage", "params": {
            "decoder_id": dec_id(), "voltage": voltage}},
        "dec_stream": {"label": "Decoder: Transmission Mode", "params": {
            "decoder_id": dec_id(), "mode": {"type": "enum", "values": ["UNICAST", "MULTICAST"],
                                             "required": True}}},
        "dec_hdcp": {"label": "Decoder: HDCP Mode (Osp)", "params": {
            "decoder_id": dec_id(), "mode": {"type": "enum",
                                             "values": ["SNK", "SRC", "OFF", "H14", "H22"],
                                             "required": True,
                                             "help": "SNK:follow display SRC:follow source "
                                                     "OFF H14:force1.4 H22:force2.2"}}},
        "dec_led": {"label": "Decoder: Power LED Flash", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_led_timeout": {"label": "Decoder: Flash LED (90s)", "params": {"decoder_id": dec_id()}},
        "dec_fpled": {"label": "Decoder: Front-Panel LED", "params": {
            "decoder_id": dec_id(), "fl": fpled}},
        "dec_hotkey_set": {"label": "Decoder: Set KVM Hotkey", "params": {
            "decoder_id": dec_id(),
            "nn": {"type": "integer", "required": True, "min": 1, "max": 20, "label": "Slot (1-20)"},
            "k0": {"type": "enum", "values": [str(x) for x in range(1, 10)], "required": True,
                   "help": "1:LCtrl 2:RCtrl 3:LShift 4:RShift 5:LAlt 6:RAlt "
                           "7:Ctrl+Shift 8:Ctrl+Alt 9:Shift+Alt"},
            "k1": {"type": "integer", "required": True, "label": "ASCII code"},
            "action": {"type": "enum", "values": ["PULL", "PUSH"], "required": True},
            "encoder_id": enc_id("Source Encoder")}},
        "dec_hotkey_del": {"label": "Decoder: Delete KVM Hotkey", "params": {
            "decoder_id": dec_id(),
            "nn": {"type": "integer", "required": True, "min": 1, "max": 20, "label": "Slot (1-20)"}}},
        "dec_guest_config": {"label": "Decoder: Serial Guest Config", "params": {
            "decoder_id": dec_id(), "state": onoff, "baud": baud, "bits": bits}},
        "dec_guest_start": {"label": "Decoder: Start Serial Guest", "params": {"decoder_id": dec_id()},
                            "help": "Enter interactive serial guest mode (exit with Exit Serial Guest)."},
        "dec_ipmode": {"label": "Decoder: IP Mode", "params": {
            "decoder_id": dec_id(), "mode": ipmode}},
        "dec_static_ip": {"label": "Decoder: Static IP", "params": {
            "decoder_id": dec_id(), "ip": {"type": "string", "required": True}}},
        "dec_static_gateway": {"label": "Decoder: Static Gateway", "params": {
            "decoder_id": dec_id(), "gateway": {"type": "string", "required": True}}},
        "dec_static_mask": {"label": "Decoder: Static Subnet Mask", "params": {
            "decoder_id": dec_id(), "mask": {"type": "string", "required": True}}},
        "dec_network_reboot": {"label": "Decoder: Network Reboot", "params": {"decoder_id": dec_id()}},
        "dec_reboot": {"label": "Decoder: Reboot", "params": {"decoder_id": dec_id()}},
        "dec_reset": {"label": "Decoder: Factory Reset", "params": {"decoder_id": dec_id()}},

        # ── Shared ──
        "exit_guest": {"label": "Exit Serial Guest", "params": {},
                       "help": "Exit interactive serial guest mode (encoder or decoder)."},

        # ── Video wall ──
        "wall_create": {"label": "Video Wall: Create", "params": {
            "wall_id": {"type": "integer", "required": True, "min": 1, "max": WALL_MAX,
                        "label": "Wall Handle (1-9)"}}},
        "wall_delete": {"label": "Video Wall: Delete", "params": {"wall_id": wall_id()}},
        "wall_set_name": {"label": "Video Wall: Set Name", "params": {
            "wall_id": wall_id(), "name": {"type": "string", "required": True}}},
        "wall_set_size": {"label": "Video Wall: Set Size", "params": {
            "wall_id": wall_id(),
            "columns": {"type": "integer", "required": True, "min": 1, "max": 9},
            "rows": {"type": "integer", "required": True, "min": 1, "max": 9}}},
        "wall_assign_dec": {"label": "Video Wall: Assign Decoder", "params": {
            "wall_id": wall_id(), "decoder_id": dec_id(),
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"}}},
        "wall_preset_create": {"label": "Video Wall: Create Preset", "params": {
            "wall_id": wall_id(),
            "preset": {"type": "integer", "required": True, "min": 1, "max": WALL_PRESET_MAX}}},
        "wall_preset_delete": {"label": "Video Wall: Delete Preset", "params": {
            "wall_id": wall_id(),
            "preset": {"type": "integer", "required": True, "min": 1, "max": WALL_PRESET_MAX}}},
        "wall_preset_set_name": {"label": "Video Wall: Set Preset Name", "params": {
            "wall_id": wall_id(),
            "preset": {"type": "integer", "required": True, "min": 1, "max": WALL_PRESET_MAX},
            "name": {"type": "string", "required": True}}},
        "wall_preset_apply": {"label": "Video Wall: Apply Preset", "params": {
            "wall_id": wall_id(),
            "preset": {"type": "integer", "required": True, "min": 1, "max": WALL_PRESET_MAX}}},
        "wall_preset_class": {"label": "Video Wall: Set Preset Class Cell", "params": {
            "wall_id": wall_id(),
            "preset": {"type": "integer", "required": True, "min": 1, "max": WALL_PRESET_MAX},
            "cls": {"type": "enum", "values": list("ABCDEFG"), "required": True},
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"}}},
        "wall_preset_class_source": {"label": "Video Wall: Set Preset Class Source", "params": {
            "wall_id": wall_id(),
            "preset": {"type": "integer", "required": True, "min": 1, "max": WALL_PRESET_MAX},
            "cls": {"type": "enum", "values": list("ABCDEFG"), "required": True},
            "encoder_id": enc_id("Source (0 = clear)")}},
        "wall_preset_matrix": {"label": "Video Wall: Set Preset Matrix Cell", "params": {
            "wall_id": wall_id(),
            "preset": {"type": "integer", "required": True, "min": 1, "max": WALL_PRESET_MAX},
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"}}},
        "wall_preset_matrix_source": {"label": "Video Wall: Set Preset Matrix Source", "params": {
            "wall_id": wall_id(),
            "preset": {"type": "integer", "required": True, "min": 1, "max": WALL_PRESET_MAX},
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"},
            "encoder_id": enc_id("Source (0 = clear)")}},
        "wall_width_bezel": {"label": "Video Wall: Width Bezel", "params": {
            "wall_id": wall_id(),
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"},
            "bw": {"type": "integer", "required": True, "min": 100, "max": 1000, "label": "Base Width"},
            "iw": {"type": "integer", "required": True, "min": 100, "max": 1000,
                   "label": "Image Width (<= base)"}}},
        "wall_height_bezel": {"label": "Video Wall: Height Bezel", "params": {
            "wall_id": wall_id(),
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"},
            "bh": {"type": "integer", "required": True, "min": 100, "max": 1000, "label": "Base Height"},
            "ih": {"type": "integer", "required": True, "min": 100, "max": 1000,
                   "label": "Image Height (<= base)"}}},
    }
    return cmds


DarwinControlDriver.DRIVER_INFO["commands"] = _build_commands()
