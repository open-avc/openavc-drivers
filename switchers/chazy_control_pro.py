"""
OpenAVC TurtleAV Chazy Control Pro driver.

The Chazy Control Pro is an AV-over-IP matrix controller. One controller
orchestrates up to 762 video encoders (TX) and 762 video decoders (RX) on a
Video LAN, plus video walls, decoder groups, outbound events, schedules,
NAS/FTP media sources, Dante presets, and whole-system configuration presets.
Each of those sub-units is modelled as an OpenAVC *child entity* of this one
device (state keyed ``device.<id>.<type>.<padded_id>.<prop>``), so the whole
matrix is driven through a single OpenAVC device.

Transport / protocol (confirmed against live hardware, TAV-CHAZY-CLTPRO
FW 1.10.11, telnet port 23):

* On connect the controller sends Telnet IAC option negotiation
  (``IAC WILL SGA / WILL ECHO / DONT ECHO / DO BINARY``) followed by a
  welcome banner and a ``CONTROLLER> `` prompt. No login/auth is required.
  The driver strips all Telnet IAC sequences and never answers them — the
  controller proceeds regardless.
* The interface is strictly request/response, one command per line
  terminated with CRLF. The controller **echoes** the command back, then
  emits the response, then re-prints the ``CONTROLLER> `` prompt. The prompt
  is the definitive end-of-response marker (more reliable than the
  ``====`` banner sentinels, which only wrap *some* responses).
* Most ``SET ...`` commands reply with a single ``[SUCCESS]<msg>.`` or
  ``[ERROR]<msg>.`` line. ``[ERROR]`` is a deterministic protocol rejection
  (e.g. ``[ERROR]Encoder 001 does not exist.``) — surfaced to the caller, not
  retried.
* Status queries (``GET STATUS``, ``GET ENC/DEC [n] STATUS``, ``GET GROUP
  STATUS`` ...) reply with a multi-line banner wrapped in 64-char ``====``
  sentinel lines. Numeric fields are zero-padded on the wire (IP
  ``192.168.004.188``); the driver normalises IPs back to ``192.168.4.188``.

Child enumeration on connect:

* Encoders and decoders are read from ``GET STATUS`` on connect and reconciled
  into the platform child registry. Groups, events, video walls, and Dante
  presets are enumerated from their own list banners (``GET GROUP STATUS``,
  ``GET EVENT STATUS``, ``GET WALL STATUS``, ``GET DANTE PRESET STATUS``), so
  instances already configured on the controller — built in its web GUI, or
  surviving an OpenAVC restart — are discovered, not only the ones this driver
  creates.
* Media sources, schedules, and configuration presets are the exception: the
  controller returns ``[ERROR]Unknown parameter`` for their ``GET ... STATUS``
  queries (confirmed against FW 1.10.11), so they genuinely cannot be
  re-enumerated. They are tracked only from the moment this driver creates
  them; after a server restart they are not re-discovered until re-created or
  re-added, and one deleted out-of-band (web GUI or a second telnet session)
  lingers as a stale child until this driver restarts. This is a device-API
  limitation, not a driver gap.

License: MIT.
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

# End-of-response marker the controller prints after every reply (and after
# the connect banner). No trailing newline.
PROMPT = b"CONTROLLER> "
# Frame on the CRLF-anchored prompt, not the bare token. The prompt is always
# CRLF-preceded on the wire (verified live: greeting ends ``====\r\nCONTROLLER> ``
# and every response ends ``...\r\nCONTROLLER> ``), so anchoring to the line
# boundary stops an echoed command argument that itself contains the literal
# ``CONTROLLER> `` (e.g. a device name) from false-splitting a response.
_FRAME_DELIM = b"\r\n" + PROMPT

# Telnet IAC bytes.
_IAC = 0xFF
_SB = 0xFA
_SE = 0xF0
_NEGOTIATE = (0xFB, 0xFC, 0xFD, 0xFE)  # WILL / WONT / DO / DONT (+1 option byte)

ENC_MAX = 762
DEC_MAX = 762
HDL_MAX = 256
MEDIA_MAX = 762
CONFIG_PRESET_MAX = 10

SIGNAL_TYPES = ["ALL", "VIDEO", "AUDIO", "IR", "RS232", "USB", "CEC", "MEDIA"]


def _enc_state_vars() -> dict[str, dict[str, Any]]:
    """Per-encoder (TX) state. `online` + `label` are injected by the platform."""
    return {
        "name": {"type": "string", "label": "Device Name"},
        "gen": {"type": "string", "label": "Generation"},
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
        "mainstream_url": {
            "type": "string", "label": "Preview Stream URL", "cloud_priority": "low",
        },
        "substream_url": {
            "type": "string", "label": "Substream URL", "cloud_priority": "low",
        },
        "arc_source": {
            "type": "integer", "label": "ARC Source (Sel)", "cloud_priority": "high",
        },
        "arc_fix": {"type": "integer", "label": "ARC Source (Fix)"},
        "sac": {"type": "enum", "values": ["ARC", "CEC", "OFF"], "label": "Shared Audio Pin"},
        "guest_enabled": {"type": "boolean", "label": "Serial Guest", "cloud_priority": "low"},
        "guest_baud": {"type": "string", "label": "Guest Baud", "cloud_priority": "low"},
        "guest_framing": {"type": "string", "label": "Guest Framing", "cloud_priority": "low"},
        "ip_mode": {"type": "string", "label": "IP Mode"},
        "mac": {"type": "string", "label": "MAC"},
        "ip": {"type": "string", "label": "IP Address"},
        "gateway": {"type": "string", "label": "Gateway", "cloud_priority": "low"},
        "subnet_mask": {"type": "string", "label": "Subnet Mask", "cloud_priority": "low"},
        "io1_dir": {"type": "string", "label": "IO1 Direction", "cloud_priority": "low"},
        "io1_level": {"type": "integer", "label": "IO1 Level", "cloud_priority": "low"},
        "io1_relay": {"type": "string", "label": "Relay 1", "cloud_priority": "low"},
        "io1_phy": {"type": "string", "label": "PHY", "cloud_priority": "low"},
        "io2_dir": {"type": "string", "label": "IO2 Direction", "cloud_priority": "low"},
        "io2_level": {"type": "integer", "label": "IO2 Level", "cloud_priority": "low"},
        "io2_relay": {"type": "string", "label": "Relay 2", "cloud_priority": "low"},
    }


def _dec_state_vars() -> dict[str, dict[str, Any]]:
    """Per-decoder (RX) state. `online` + `label` are injected by the platform."""
    return {
        "name": {"type": "string", "label": "Device Name"},
        "gen": {"type": "string", "label": "Generation"},
        "firmware": {"type": "string", "label": "Firmware"},
        "net": {"type": "boolean", "label": "Network Link", "cloud_priority": "high"},
        "hpd": {"type": "boolean", "label": "Display Connected (HPD)", "cloud_priority": "high"},
        "mode": {"type": "enum", "values": ["MX", "VW"], "label": "Output Mode"},
        "resolution": {"type": "string", "label": "Output Resolution"},
        "rotate": {"type": "enum", "values": ["0", "90", "180", "270"], "label": "Rotation"},
        "source_video": {"type": "integer", "label": "Video Source", "cloud_priority": "high"},
        "source_audio": {"type": "integer", "label": "Audio Source", "cloud_priority": "high"},
        "source_ir": {"type": "integer", "label": "IR Source", "cloud_priority": "low"},
        "source_rs232": {"type": "integer", "label": "RS232 Source", "cloud_priority": "low"},
        "source_usb": {"type": "integer", "label": "USB Source", "cloud_priority": "low"},
        "source_cec": {"type": "integer", "label": "CEC Source", "cloud_priority": "low"},
        "fix_video": {"type": "integer", "label": "Video Route (Fix)", "cloud_priority": "low"},
        "fix_audio": {"type": "integer", "label": "Audio Route (Fix)", "cloud_priority": "low"},
        "fix_ir": {"type": "integer", "label": "IR Route (Fix)", "cloud_priority": "low"},
        "fix_rs232": {"type": "integer", "label": "RS232 Route (Fix)", "cloud_priority": "low"},
        "fix_usb": {"type": "integer", "label": "USB Route (Fix)", "cloud_priority": "low"},
        "fix_cec": {"type": "integer", "label": "CEC Route (Fix)", "cloud_priority": "low"},
        "multicast": {"type": "boolean", "label": "Multicast"},
        "video_output": {"type": "boolean", "label": "Output On", "cloud_priority": "high"},
        "video_mute": {"type": "boolean", "label": "Output Muted"},
        "video_freeze": {"type": "boolean", "label": "Output Frozen"},
        "osd": {"type": "boolean", "label": "ID OSD"},
        "ull": {"type": "boolean", "label": "Ultra Low Latency", "cloud_priority": "low"},
        "sac": {"type": "enum", "values": ["ARC", "CEC", "OFF"], "label": "Shared Audio Pin"},
        "osp": {"type": "integer", "label": "OSP", "cloud_priority": "low"},
        "guest_enabled": {"type": "boolean", "label": "Serial Guest", "cloud_priority": "low"},
        "guest_baud": {"type": "string", "label": "Guest Baud", "cloud_priority": "low"},
        "guest_framing": {"type": "string", "label": "Guest Framing", "cloud_priority": "low"},
        "dante_audio_source": {
            "type": "enum", "values": ["DANTE", "NATIVE"], "label": "Dante Audio Source",
            "cloud_priority": "low",
        },
        "arp": {"type": "enum", "values": ["ARC", "SPDIF"], "label": "Audio Return Path",
                "cloud_priority": "low"},
        "earc_downgrade": {"type": "boolean", "label": "eARC Downgrade", "cloud_priority": "low"},
        "ip_mode": {"type": "string", "label": "IP Mode"},
        "mac": {"type": "string", "label": "MAC"},
        "ip": {"type": "string", "label": "IP Address"},
        "gateway": {"type": "string", "label": "Gateway", "cloud_priority": "low"},
        "subnet_mask": {"type": "string", "label": "Subnet Mask", "cloud_priority": "low"},
        "io1_dir": {"type": "string", "label": "IO1 Direction", "cloud_priority": "low"},
        "io1_level": {"type": "integer", "label": "IO1 Level", "cloud_priority": "low"},
        "io1_relay": {"type": "string", "label": "Relay 1", "cloud_priority": "low"},
        "io1_phy": {"type": "string", "label": "PHY", "cloud_priority": "low"},
        "io2_dir": {"type": "string", "label": "IO2 Direction", "cloud_priority": "low"},
        "io2_level": {"type": "integer", "label": "IO2 Level", "cloud_priority": "low"},
        "io2_relay": {"type": "string", "label": "Relay 2", "cloud_priority": "low"},
    }


class ChazyControlProDriver(BaseDriver):
    """TurtleAV Chazy Control Pro AV-over-IP matrix controller."""

    DRIVER_INFO = {
        "id": "chazy_control_pro",
        "name": "TurtleAV Chazy Control Pro",
        "manufacturer": "TurtleAV",
        "category": "switcher",
        "version": "1.4.4",
        "author": "OpenAVC",
        "min_platform_version": "0.13.0",
        "description": (
            "Controls a TurtleAV Chazy Control Pro AV-over-IP matrix "
            "controller and every sub-unit it manages: video encoders (TX) "
            "and decoders (RX), video walls, decoder groups, events, "
            "schedules, NAS/FTP media sources, Dante presets, and "
            "configuration presets."
        ),
        "source_url": "https://turtleav.com/portfolio/chazy-4k/",
        "tags": ["av-over-ip", "matrix", "encoder", "decoder", "video-wall", "dante"],
        "verified": False,
        "simulated": True,
        "protocols": ["chazy_telnet"],
        "ports": [23],
        "transport": "tcp",
        "discovery": {
            # A.5 resolved against live hardware (FW 1.10.11): the controller
            # advertises exactly one mDNS service, the generic Audinate Dante
            # Conmon service (_netaudio-cmc._udp.local.), whose TXT carries only
            # Dante fields and no vendor string. That is not a Chazy fingerprint
            # (every Dante device emits it), so there is no mDNS service-type to
            # claim here. The on-wire identity is the telnet connect banner
            # ("Welcome To TAV-CHAZY-CLTPRO ..."). The tcp_probe below matches
            # that token so an UNINSTALLED Pro identifies straight from the
            # catalog; hostname and port are shared with the other Chazy/Darwin
            # controllers and cannot disambiguate on their own. The banner
            # follows Telnet IAC negotiation in a later TCP segment, which the
            # probe runner accumulates. The companion stays as a backup path
            # with identical token logic.
            "tcp_probe": {
                "port": 23,
                # Pro model token; never matches "CHAZY CONTROL" (standard) or
                # the Darwin "Controller(h)" / "DARWIN" tokens.
                "expect": "TAV-CHAZY-CLTPRO",
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
            "python": "./chazy_control_pro_discovery.py",
            "hostname": ["^controller(\\.local)?$"],
            "port_open": [23],
            "manufacturer_alias": ["turtleav", "chazy"],
        },
        "compatible_models": [
            {
                "manufacturer": "TurtleAV",
                "models": ["Chazy Control Pro (TAV-CHAZY-CLTPRO)"],
                "confidence": "untested",
                "notes": "Telnet API confirmed against FW 1.10.11.",
            },
        ],
        "help": {
            "overview": (
                "The Chazy Control Pro orchestrates an entire Chazy 4K "
                "AV-over-IP system. Add it as one device; its encoders, "
                "decoders, video walls, groups, events, schedules, media "
                "sources, Dante presets, and configuration presets appear as "
                "child entities under the Child Entities tab."
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
                "5. Video walls, groups, events, and Dante presets already "
                "configured on the controller are also listed automatically. "
                "Media sources, schedules, and configuration presets cannot be "
                "listed by the controller, so they appear only once you create "
                "them here."
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
                "description": "Chazy Control Pro telnet API port (default 23).",
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
                    "(GET ENC/DEC [n] STATUS) and the controller clock, batched. "
                    "0 disables only this heavy detail refresh; the encoder/"
                    "decoder roster and the group/event/wall/dante-preset lists "
                    "still reconcile on every status poll."
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
            "hostname": {"type": "string", "label": "Hostname"},
            "dns_mode": {"type": "string", "label": "DNS Mode"},
            "dns_preferred": {"type": "string", "label": "DNS Preferred"},
            "dns_alternate": {"type": "string", "label": "DNS Alternate"},
            "date": {"type": "string", "label": "Date / Time"},
            "ntp_server": {"type": "string", "label": "NTP Server"},
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
                "summary_fields": ["name", "ip", "gen", "signal_present"],
                "label_field": "name",
            },
            "decoder": {
                "label": "Decoder",
                "label_plural": "Decoders",
                "id_format": {"type": "integer", "min": 1, "max": DEC_MAX, "pad_width": 3},
                "state_variables": _dec_state_vars(),
                "summary_fields": ["name", "ip", "source_video", "mode", "hpd"],
                "label_field": "name",
            },
            "video_wall": {
                "label": "Video Wall",
                "label_plural": "Video Walls",
                "id_format": {"type": "integer", "min": 1, "max": HDL_MAX, "pad_width": 2},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "columns": {"type": "integer", "label": "Columns"},
                    "rows": {"type": "integer", "label": "Rows"},
                },
                "summary_fields": ["name", "columns", "rows"],
                "label_field": "name",
            },
            "group": {
                "label": "Group",
                "label_plural": "Groups",
                "id_format": {"type": "integer", "min": 1, "max": HDL_MAX, "pad_width": 2},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "member_count": {"type": "integer", "label": "Decoders"},
                },
                "summary_fields": ["name", "member_count"],
                "label_field": "name",
            },
            "event": {
                "label": "Event",
                "label_plural": "Events",
                "id_format": {"type": "integer", "min": 1, "max": HDL_MAX, "pad_width": 2},
                # `event_type` and `address` are parsed from GET EVENT STATUS.
                # There is no banner field for a started/stopped flag, so no
                # `running` state var (event_start/event_stop have no read-back).
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "event_type": {"type": "string", "label": "Type"},
                    "address": {"type": "string", "label": "Address"},
                },
                "summary_fields": ["name", "event_type", "address"],
                "label_field": "name",
            },
            "schedule": {
                "label": "Schedule",
                "label_plural": "Schedules",
                "id_format": {"type": "integer", "min": 1, "max": HDL_MAX, "pad_width": 2},
                # Schedules can't be enumerated (GET SCHEDULE STATUS ->
                # [ERROR]Unknown parameter on FW 1.10.11), so only the name we
                # seed at create time is ever known. time_type / running have no
                # read-back and would render permanently blank — omitted.
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                },
                "summary_fields": ["name"],
                "label_field": "name",
            },
            # media / schedule / config_preset cannot be enumerated by the
            # controller (GET ... STATUS returns [ERROR]Unknown parameter on
            # FW 1.10.11). They are registered only when this driver creates
            # them and are not re-discovered after a restart — create-time
            # tracking only.
            "media": {
                "label": "Media Source",
                "label_plural": "Media Sources",
                "id_format": {"type": "integer", "min": 1, "max": MEDIA_MAX, "pad_width": 3},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "media_type": {"type": "enum", "values": ["SAMBA", "NFS", "FTP"], "label": "Type"},
                    "address": {"type": "string", "label": "Address"},
                    "file": {"type": "string", "label": "File"},
                },
                "summary_fields": ["name", "media_type", "address"],
                "label_field": "name",
            },
            "dante_preset": {
                "label": "Dante Preset",
                "label_plural": "Dante Presets",
                "id_format": {"type": "integer", "min": 1, "max": HDL_MAX, "pad_width": 2},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                },
                "summary_fields": ["name"],
                "label_field": "name",
            },
            "config_preset": {
                "label": "Configuration Preset",
                "label_plural": "Configuration Presets",
                "id_format": {
                    "type": "integer", "min": 1, "max": CONFIG_PRESET_MAX, "pad_width": 2,
                },
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                },
                "summary_fields": ["name"],
                "label_field": "name",
            },
        },
        # Command surface — see _COMMAND_TEMPLATES for the wire format of each.
        # Commands that target a sub-unit take a child_id parameter.
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

        # Raw transport: we do our own Telnet IAC stripping and prompt-based
        # framing, and need to see blank lines that a delimiter parser drops.
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
        log.info(f"[{self.device_id}] Connected to Chazy Control Pro at {host}:{port}")

        # Prime full state + child roster before steady-state polling.
        try:
            await self._poll_status()
        except Exception:
            log.exception(f"[{self.device_id}] Initial enumeration failed")
        try:
            await self.poll_children("encoder", self._fetch_encoder_detail)
            await self.poll_children("decoder", self._fetch_decoder_detail)
            await self._reconcile_config_children()
            await self._poll_system_clock()
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
        """Accumulate bytes; emit one queue item per prompt-delimited unit.

        The prompt is matched only when CRLF-preceded (``_FRAME_DELIM``), so an
        echoed command argument containing the literal ``CONTROLLER> `` can't
        false-split a response. The trailing ``\\r\\n`` is consumed as part of
        the delimiter; ``_strip_echo`` then drops the leading echoed command.
        """
        self._rx_buffer += self._filter_telnet(data)
        while True:
            idx = self._rx_buffer.find(_FRAME_DELIM)
            if idx == -1:
                break
            unit = self._rx_buffer[:idx]
            self._rx_buffer = self._rx_buffer[idx + len(_FRAME_DELIM):]
            text = unit.decode("latin-1", errors="replace")
            self._responses.put_nowait(text)

    @staticmethod
    def _strip_echo(response: str, sent: str) -> str:
        """Drop the leading echoed command line the controller prepends."""
        # Normalise CRLF/lone-LF to LF, drop a leading line equal to `sent`.
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
        # Config-child lists (groups/events/walls/dante presets) are cheap
        # single-banner queries and the only steady-state path that picks up
        # children built in the controller's web GUI and drops ones deleted
        # there. Reconcile every poll so they stay in sync even when detail
        # polling (the heavy per-encoder/decoder fetch) is disabled.
        await self._reconcile_config_children()
        detail_interval = self.config.get("detail_poll_interval", 60)
        poll_interval = self.config.get("poll_interval", 10) or 10
        self._poll_count += 1
        if detail_interval and detail_interval > 0:
            every = max(1, round(detail_interval / poll_interval))
            if self._poll_count % every == 0:
                await self.poll_children("encoder", self._fetch_encoder_detail)
                await self.poll_children("decoder", self._fetch_decoder_detail)
                await self._poll_system_clock()

    # ── send_command dispatch ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = dict(params or {})
        self._coerce_child_ids(command, params)

        # Lifecycle commands that register/deregister children, plus the few
        # multi-step flows, are handled explicitly. Everything else maps
        # straight to a wire template.
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

        template = _COMMAND_TEMPLATES.get(command)
        if template is None:
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            raise ValueError(f"Unknown command: {command}")
        wire = template.format(**params)
        resp = await self._send_set(wire)
        # Reflect device settings the controller has no GET read-back for into
        # child state, so the UI shows the commanded value instead of a blank.
        self._apply_post_set_state(command, params)
        # Date / NTP do have a read-back — refresh them after a successful set.
        if command in ("set_date", "set_ntp_server"):
            await self._poll_system_clock()
        return resp

    def _apply_post_set_state(self, command: str, params: dict[str, Any]) -> None:
        """Optimistically write a just-succeeded SET into child state for the
        device settings the controller can't report back via GET (so the IDE
        doesn't show them permanently blank/stale). No-op for any other command.
        """
        spec = _POST_SET_STATE.get(command)
        if not spec:
            return
        ctype, id_param, builder = spec
        try:
            cid = int(params[id_param])
        except (KeyError, TypeError, ValueError):
            return
        if not self.is_child_registered(ctype, cid):
            return
        try:
            updates = builder(params)
        except KeyError:
            return
        if updates:
            self.set_child_state_batch(ctype, cid, updates)

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
                # Carry every effective-schema prop (declared vars + the
                # platform-managed `online` and `label`) so a renumber doesn't
                # silently drop the user's custom label — register_child under
                # the new id would otherwise re-source label from a project
                # entry that doesn't exist yet and reset it to "".
                seed = {
                    k: v for k, v in prev.items()
                    if k in self.get_child_entity_types()[ctype]["state_variables"]
                }
                self.register_child(ctype, new, initial_state=seed or None)
        return resp

    async def _do_search(self) -> str:
        """Trigger a Video LAN device search and reconcile the result.

        The controller returns the whole reply as ONE prompt-delimited unit:
        the ``[SUCCESS]...done.`` progress line and the result banner arrive
        together (confirmed live, FW 1.10.11), so a single long-timeout read
        captures it — no separate GET SEARCH STATUS poll is needed.
        """
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
        await self._reconcile_config_children()
        return {
            "encoders": len(self.list_children("encoder")),
            "decoders": len(self.list_children("decoder")),
            "video_walls": len(self.list_children("video_wall")),
            "groups": len(self.list_children("group")),
            "events": len(self.list_children("event")),
            "dante_presets": len(self.list_children("dante_preset")),
            # Create-tracked types (not re-enumerable) — unchanged by the
            # refresh, but reported so the IDE summary doesn't show 0.
            "schedules": len(self.list_children("schedule")),
            "media": len(self.list_children("media")),
            "config_presets": len(self.list_children("config_preset")),
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

    async def _poll_system_clock(self) -> None:
        """Read the controller clock + NTP server. These have no field in
        GET STATUS and change rarely, so they're polled on a slow cadence
        (and re-read right after a set). Each query is guarded independently.
        """
        declared = self.DRIVER_INFO["state_variables"]
        for wire, key in (("GET DATE", "date"), ("GET NTP SERVER", "ntp_server")):
            if key not in declared:
                continue
            try:
                val = _parse_success_line(await self._send_request(wire))
                if val is not None:
                    self.set_state(key, val)
            except Exception:
                log.debug(f"[{self.device_id}] {wire} poll failed", exc_info=True)

    def _reconcile_children(
        self,
        ctype: str,
        parsed_map: dict[int, dict[str, Any]],
        *,
        online_from_net: bool,
    ) -> None:
        """Register newly-seen children, update the light state of known ones,
        and deregister any that the controller no longer reports.

        ``online_from_net`` derives the platform ``online`` flag from the
        parsed ``net`` link state (encoders/decoders, whose presence and link
        are independent). Config-style children (groups/events/walls/dante
        presets) have no link concept — if the controller lists them they
        exist, so ``online`` is forced True.
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

    async def _reconcile_config_children(self) -> None:
        """Enumerate the queryable non-encoder/decoder child types from their
        list banners and reconcile the platform registry, so a group/event/
        wall/dante-preset built in the controller's web GUI (or surviving an
        OpenAVC restart) is discovered — not only the ones this driver creates.

        Each query is independent; a failure on one type is logged and the rest
        still run. media / schedule / config_preset are intentionally absent
        (the controller cannot enumerate them — create-time tracking only).
        """
        queries = (
            ("group", "GET GROUP STATUS", _parse_group_status),
            ("event", "GET EVENT STATUS", _parse_event_status),
            ("video_wall", "GET WALL STATUS", _parse_wall_status),
            ("dante_preset", "GET DANTE PRESET STATUS", _parse_dante_preset_status),
        )
        for ctype, query, parser in queries:
            try:
                resp = await self._send_request(query)
                self._reconcile_children(
                    ctype, parser(resp), online_from_net=False
                )
            except Exception:
                log.debug(
                    f"[{self.device_id}] {ctype} enumeration failed",
                    exc_info=True,
                )

    async def _fetch_encoder_detail(
        self, ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        schema = self.get_child_entity_types()["encoder"]["state_variables"]
        for eid in ids:
            resp = await self._send_request(f"GET ENC {eid} STATUS")
            if "does not exist" in resp or resp.lstrip().startswith("[ERROR]"):
                continue
            props = _parse_encoder_detail(resp)
            clean = {k: v for k, v in props.items() if k in schema}
            if clean:
                clean["online"] = bool(props.get("net", False))
                # Secondary-stream preview URLs come from a separate query;
                # best-effort so an SS failure never drops the encoder.
                try:
                    ss_resp = await self._send_request(f"GET ENC {eid} SS STATUS")
                    if not ss_resp.lstrip().startswith("[ERROR]"):
                        clean.update(
                            {k: v for k, v in _parse_ss_status(ss_resp).items()
                             if k in schema}
                        )
                except Exception:
                    log.debug(
                        f"[{self.device_id}] ENC {eid} SS poll failed", exc_info=True
                    )
                out[eid] = clean
        return out

    async def _fetch_decoder_detail(
        self, ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        schema = self.get_child_entity_types()["decoder"]["state_variables"]
        for did in ids:
            resp = await self._send_request(f"GET DEC {did} STATUS")
            if "does not exist" in resp or resp.lstrip().startswith("[ERROR]"):
                continue
            props = _parse_decoder_detail(resp)
            clean = {k: v for k, v in props.items() if k in schema}
            if clean:
                clean["online"] = bool(props.get("net", False))
                out[did] = clean
        return out


# ── Banner parsers ──
#
# The controller emits fixed-width column banners. Crucially, columns are
# blank (space-filled) when a value is unknown — e.g. Type/gen and Ver/firmware
# come back blank while a device is offline — so the data must be sliced by the
# header word positions, not whitespace-split. All parsers below are validated
# against byte-exact captures in tests/fixtures/chazy_control_pro_banners.py.


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
    ``{header_word: value}``. The final column extends to end of line. Only
    valid for headers whose column labels are single words (the Telnet/MAC row,
    which has two-word labels, is parsed separately).
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


def _parse_success_line(text: str) -> str | None:
    """Pull the payload out of a single-line ``[SUCCESS]<value>.`` reply.

    Live examples: ``[SUCCESS]2026-05-31 03:55:35 (Australia/Sydney).`` and
    ``[SUCCESS]time.nist.gov.``. Strips the ``[SUCCESS]`` prefix and the single
    trailing period; returns None on an ``[ERROR]`` reply or an empty payload.
    """
    stripped = text.strip()
    line = stripped.splitlines()[0].strip() if stripped else ""
    if line.startswith("[ERROR]"):
        return None
    if line.startswith("[SUCCESS]"):
        line = line[len("[SUCCESS]"):].strip()
    line = line.rstrip(".").strip()
    return line or None


def _split_flag_pair(value: str) -> tuple[bool, bool]:
    """Parse a 'X/Y' flag pair like 'Off/Off' -> (False, False)."""
    a, _, b = value.strip().partition("/")
    return a.strip() == "On", b.strip() == "On"


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
        elif line.startswith("ENC") and "EDID" in line and "NET/Sig" in line:
            i += 1
            while i < n and lines[i].strip() and lines[i].strip() != "NONE":
                cols = _split_columns(line, lines[i])
                eid = cols.get("ENC", "")
                if eid.isdigit():
                    net, sig = _split_flag_pair(cols.get("NET/Sig", ""))
                    encoders[int(eid)] = {
                        "gen": cols.get("Type", ""),
                        "edid": cols.get("EDID", ""),
                        "ip": _norm_ip(cols.get("IP", "")),
                        "net": net,
                        "signal_present": sig,
                    }
                i += 1
            continue
        elif line.startswith("DEC") and "From" in line and "Mode" in line:
            i += 1
            while i < n and lines[i].strip() and lines[i].strip() != "NONE":
                cols = _split_columns(line, lines[i])
                did = cols.get("DEC", "")
                if did.isdigit():
                    net, hpd = _split_flag_pair(cols.get("NET/HDMI", ""))
                    frm = cols.get("From", "")
                    decoders[int(did)] = {
                        "gen": cols.get("Type", ""),
                        "source_video": int(frm) if frm.isdigit() else 0,
                        "ip": _norm_ip(cols.get("IP", "")),
                        "net": net,
                        "hpd": hpd,
                        "resolution": cols.get("Res", ""),
                        "mode": cols.get("Mode", ""),
                    }
                i += 1
            continue
        elif line.startswith("LAN") and "DHCP" in line and "SubnetMask" in line:
            i += 1
            while i < n and lines[i].strip():
                row = lines[i]
                if row.lstrip().startswith("(static"):
                    i += 1
                    continue
                cols = _split_columns(line, row)
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
            if len(parts) >= 3:
                system["telnet_port"] = parts[0].lstrip("0") or "0"
                system["ssh"] = parts[1] != "Off"
                system["https"] = parts[2] != "Off"
            if len(parts) >= 5:
                system["lan1_mac"] = parts[3]
                system["lan2_mac"] = parts[4]
            i += 2
            continue
        elif line.startswith("DNS") and "Preferred" in line:
            cols = _split_columns(line, lines[i + 1]) if i + 1 < n else {}
            if cols.get("Mode"):
                system["dns_mode"] = cols.get("Mode", "")
            if cols.get("Preferred"):
                system["dns_preferred"] = _norm_ip(cols.get("Preferred", ""))
            if cols.get("Alternate"):
                system["dns_alternate"] = _norm_ip(cols.get("Alternate", ""))
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
            n = int(parts[0])
            if 1 <= n <= 4:
                out[f"gpio{n}_dir"] = parts[1]
                get = parts[-1]
                out[f"gpio{n}_level"] = int(get) if get.lstrip("-").isdigit() else 0
    return out


# ── Pre-existing-child enumeration parsers ──
#
# Encoders/decoders come from GET STATUS; the remaining queryable child types
# are enumerated from their own list banners on connect (and on Refresh). All
# four below are validated byte-for-byte against
# tests/fixtures/chazy_control_pro_child_banners.py.
#
# These parsers key off the body/columns, NOT the Info-header line: FW 1.10.11
# mislabels the GET GROUP STATUS and GET EVENT STATUS Info headers as
# "TAV-CHAZY-CLTPRO Dante Preset Info" (a firmware copy-paste bug). Each list
# query returns {local_id: {prop: value}} for the props declared on that child
# type; empty controllers reply with a "No <Type>" body and parse to {}.
#
# media / schedule / config_preset are intentionally absent: the controller
# returns "[ERROR]Unknown parameter" for their GET ... STATUS, so they cannot
# be re-discovered after a restart (create-time tracking only — see the
# child_entity_types comment on the driver class).


def _parse_group_status(text: str) -> dict[int, dict[str, Any]]:
    """Parse GET GROUP STATUS. Each group is ``ID nnn Name: <name>`` followed
    by an indented ``Decoders:  <count>`` line.
    """
    out: dict[int, dict[str, Any]] = {}
    cur: int | None = None
    for ln in text.split("\n"):
        s = ln.strip()
        if s.startswith("ID ") and "Name:" in s:
            head, _, name = s.partition("Name:")
            parts = head.split()
            if len(parts) >= 2 and parts[1].isdigit():
                cur = int(parts[1])
                out[cur] = {"name": name.strip(), "member_count": 0}
            else:
                cur = None
        elif cur is not None and s.startswith("Decoders:"):
            val = s.split(":", 1)[1].strip()
            out[cur]["member_count"] = int(val) if val.isdigit() else 0
    return out


def _parse_event_status(text: str) -> dict[int, dict[str, Any]]:
    """Parse GET EVENT STATUS. Each event is ``ID nnn Name: <name>`` followed
    by indented ``<Label>: <value>`` field lines (Type / Address / Port /
    Interface / Data / Request / Resend Delay / Resending). Only the declared
    props (name, event_type, address) are extracted; the rest are decoration.
    """
    out: dict[int, dict[str, Any]] = {}
    cur: int | None = None
    field_map = {"Type": "event_type", "Address": "address"}
    for ln in text.split("\n"):
        s = ln.strip()
        if s.startswith("ID ") and "Name:" in s:
            head, _, name = s.partition("Name:")
            parts = head.split()
            if len(parts) >= 2 and parts[1].isdigit():
                cur = int(parts[1])
                out[cur] = {"name": name.strip()}
            else:
                cur = None
        elif cur is not None and ":" in s:
            label, _, val = s.partition(":")
            key = field_map.get(label.strip())
            if key:
                out[cur][key] = val.strip()
    return out


def _parse_wall_status(text: str) -> dict[int, dict[str, Any]]:
    """Parse GET WALL STATUS. Each video wall is a ``VW Col Row CfgSel Name``
    header + a single data row, followed by an OutID grid and per-preset class
    detail (decoration the schema doesn't track). The device's ``NULL`` name
    sentinel (an unnamed wall) is normalised to an empty string.
    """
    out: dict[int, dict[str, Any]] = {}
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    for i, ln in enumerate(lines):
        if ln.startswith("VW") and "Col" in ln and "CfgSel" in ln and "Name" in ln:
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


def _parse_dante_preset_status(text: str) -> dict[int, dict[str, Any]]:
    """Parse GET DANTE PRESET STATUS. Each preset is an ``ID    Name`` header +
    a ``nnn   <name>`` data row, then an indented ``>>Dev`` routing block the
    schema doesn't track. The header is distinct from the enc/dec detail header
    (which has a ``Type`` column), so it's matched on its exact two columns.
    """
    out: dict[int, dict[str, Any]] = {}
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    for i, ln in enumerate(lines):
        if ln.strip().split() != ["ID", "Name"]:
            continue
        j = i + 1
        while j < len(lines):
            row = lines[j]
            stripped = row.strip()
            if not stripped or row.lstrip().startswith(">>") or "====" in row:
                break
            # Gate on the first token (robust to any ID pad width), then read
            # the name via column slice so multi-word names survive.
            toks = stripped.split()
            if toks and toks[0].isdigit():
                name = _split_columns(ln, row).get("Name", "").strip()
                out[int(toks[0])] = {"name": name}
            j += 1
    return out


def _detail_sections(text: str) -> tuple[str | None, str, list[str]]:
    """Return (header_line, data_line, body_lines) for a GET ENC/DEC STATUS
    banner — the 'ID ... Name' header, the single data row, and the indented
    >> sub-block lines after it (sentinels stripped).
    """
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("ID") and "Name" in ln:
            data = lines[i + 1] if i + 1 < len(lines) else ""
            body = [b for b in lines[i + 2:] if "====" not in b]
            return ln, data, body
    return None, "", []


def _detail_blocks(body: list[str]) -> list[tuple[str, list[str]]]:
    """Group sub-block lines into (label_line, [value_lines]) pairs. Each '>>'
    line starts a block; following non-empty lines are its values (the Pin
    block can have two rows).
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


def _slash_ints(value: str) -> list[int]:
    """Parse a '/'-separated route row ('001 /001 /001 ...') into ints."""
    out: list[int] = []
    for tok in value.split():
        tok = tok.lstrip("/")
        if tok.isdigit():
            out.append(int(tok))
    return out


def _parse_pin(values: list[str], out: dict[str, Any]) -> None:
    """Parse the >>Pin per-IO rows.

    Row layout per port: ``(N) IOVOL IODIR IODAT IRVOL RLY PHY``. Port 2 omits
    the IRVOL and PHY columns (e.g. ``(2) 12 Out 0 Open``), so fields are found
    by recognising their values rather than by fixed column index. Offline
    devices report a single ``NA`` (skipped). Confirmed against an online TX+RX.
    """
    for ln in values:
        toks = ln.split()
        if not toks or not toks[0].startswith("("):
            continue
        idx = toks[0].strip("()")
        if idx not in ("1", "2"):
            continue
        p = f"io{idx}_"
        for j, tok in enumerate(toks):
            if tok in ("In", "Out"):
                out[p + "dir"] = tok
                nxt = toks[j + 1] if j + 1 < len(toks) else ""
                if nxt.lstrip("-").isdigit():
                    out[p + "level"] = int(nxt)
            elif tok in ("Open", "Close"):
                out[p + "relay"] = tok
            elif tok in ("Copper", "Fiber"):
                out[p + "phy"] = tok


def _parse_sac_guest(value: str, out: dict[str, Any], with_osp: bool) -> None:
    """Parse a >>SAC value row. Encoder: 'ARC Off /9 /8n1'. Decoder adds an OSP
    column: 'ARC 4 Off /9 /8n1'.
    """
    toks = value.split()
    if not toks:
        return
    out["sac"] = toks[0]
    rest = toks[1:]
    if with_osp:
        if rest and rest[0].isdigit():
            out["osp"] = int(rest[0])
        rest = rest[1:]
    rest = [t.lstrip("/") for t in rest]
    if len(rest) >= 1:
        out["guest_enabled"] = rest[0] == "On"
    if len(rest) >= 2:
        out["guest_baud"] = rest[1]
    if len(rest) >= 3:
        out["guest_framing"] = rest[2]


def _parse_ss_status(text: str) -> dict[str, str]:
    """Parse ``GET ENC [n] SS STATUS`` into ``{mainstream_url, substream_url}``.

    The banner carries a ``>>MainStream URL`` / ``>>SubStream URL`` marker, each
    followed by the URL (or ``NA``) on the next line, e.g.::

        ID    WorkMode    Version
        001   NA
            >>MainStream URL
              http://169.254.10.1:8080/?action=stream
            >>SubStream URL
              NA

    ``NA`` (no stream of that kind on this generation) normalises to ``""``.
    """
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    markers = {">>mainstream url": "mainstream_url", ">>substream url": "substream_url"}
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        key = markers.get(line.strip().lower())
        if key is None:
            continue
        for nxt in lines[i + 1:]:
            val = nxt.strip()
            if not val or val.startswith("="):
                continue
            if val.startswith(">>"):
                break  # next marker reached with no value
            out[key] = "" if val.upper() == "NA" else val
            break
    return out


def _parse_encoder_detail(text: str) -> dict[str, Any]:
    """Parse a GET ENC [n] STATUS banner for a single encoder."""
    header, data, body = _detail_sections(text)
    out: dict[str, Any] = {}
    if header and data:
        c = _split_columns(header, data)
        out["gen"] = c.get("Type", "")
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
        v0 = values[0].strip() if values else ""
        if ">>SAC" in label:
            _parse_sac_guest(values[0] if values else "", out, with_osp=False)
        elif ">>Fix" in label:
            if v0.isdigit():
                out["arc_fix"] = int(v0)
        elif ">>Sel" in label:
            if v0.isdigit():
                out["arc_source"] = int(v0)
        elif ">>Pin" in label:
            _parse_pin(values, out)
        elif ">>IM" in label:
            toks = v0.split()
            if toks:
                out["ip_mode"] = toks[0]
            if len(toks) >= 2:
                out["mac"] = toks[1]
        elif ">>IP" in label:
            toks = v0.split()
            if len(toks) >= 1:
                out["ip"] = _norm_ip(toks[0])
            if len(toks) >= 2:
                out["gateway"] = _norm_ip(toks[1])
            if len(toks) >= 3:
                out["subnet_mask"] = _norm_ip(toks[2])
    return out


def _parse_decoder_detail(text: str) -> dict[str, Any]:
    """Parse a GET DEC [n] STATUS banner for a single decoder."""
    header, data, body = _detail_sections(text)
    out: dict[str, Any] = {}
    if header and data:
        c = _split_columns(header, data)
        out["gen"] = c.get("Type", "")
        out["net"] = _is_on(c.get("Net", ""))
        out["hpd"] = _is_on(c.get("HPD", ""))
        if c.get("Ver"):
            out["firmware"] = c.get("Ver", "")
        out["mode"] = c.get("Mode", "")
        out["resolution"] = c.get("Res", "")
        out["rotate"] = c.get("Rotate", "")
        if c.get("Name"):
            out["name"] = c.get("Name", "")
    fix_keys = ["fix_video", "fix_audio", "fix_ir", "fix_rs232", "fix_usb", "fix_cec"]
    sel_keys = ["source_video", "source_audio", "source_ir",
                "source_rs232", "source_usb", "source_cec"]
    for label, values in _detail_blocks(body):
        v0 = values[0] if values else ""
        if ">>Fix" in label:
            toks = v0.split()
            for k, r in zip(fix_keys, toks[:6]):
                r = r.lstrip("/")
                if r.isdigit():
                    out[k] = int(r)
            # MCast/Video/Mute follow the 6 slash-routes; anchor them to the
            # header columns (the label line) rather than positional toks[6:],
            # so an added/reordered route column can't shift these booleans
            # onto the wrong field.
            cols = _split_columns(label, v0)
            if cols.get("MCast"):
                out["multicast"] = cols["MCast"] == "On"
            if cols.get("Video"):
                out["video_output"] = cols["Video"] == "On"
            if cols.get("Mute"):
                out["video_mute"] = cols["Mute"] == "On"
        elif ">>Sel" in label:
            for k, val in zip(sel_keys, _slash_ints(v0)):
                out[k] = val
        elif ">>SAC" in label:
            _parse_sac_guest(v0, out, with_osp=True)
        elif ">>Pin" in label:
            _parse_pin(values, out)
        elif ">>IM" in label:
            toks = v0.split()
            if toks:
                out["ip_mode"] = toks[0]
            if len(toks) >= 2:
                out["mac"] = toks[1]
        elif ">>IP" in label:
            toks = v0.split()
            if len(toks) >= 1:
                out["ip"] = _norm_ip(toks[0])
            if len(toks) >= 2:
                out["gateway"] = _norm_ip(toks[1])
            if len(toks) >= 3:
                out["subnet_mask"] = _norm_ip(toks[2])
    return out


# ── Command templates (wire format per command) ──
#
# {param} placeholders are filled from the command's params. Child-id params
# are coerced to bare ints by _coerce_child_ids before formatting.

_COMMAND_TEMPLATES: dict[str, str] = {
    # System
    "reboot_controller": "SET REBOOT",
    "set_rs232_baud": "SET RS232BAUDRATE {baud}",
    # Encoder
    "enc_set_name": "SET ENC {encoder_id} NAME {name}",
    "enc_switch_arc": "SET ENC {encoder_id} SWITCH {decoder_id} ARC",
    "enc_led": "SET ENC {encoder_id} LED {state}",
    "enc_led_timeout": "SET ENC {encoder_id} LED ON 90",
    "enc_multicast": "SET ENC {encoder_id} MULTICAST {state}",
    "enc_dante_bridge": "SET ENC {encoder_id} DANTE BRIDGE {state}",
    "enc_dante_vlan": "SET ENC {encoder_id} DANTE VLAN {state}",
    "enc_dante_vlan_tag": "SET ENC {encoder_id} DANTE VLAN TAG {tag}",
    "enc_audio_stream": "SET ENC {encoder_id} AUDIO STREAM {stream}",
    "enc_audio_input": "SET ENC {encoder_id} AUDIO INPUT {source}",
    "enc_edid_copy": "SET ENC {encoder_id} EDID COPY {decoder_id}",
    "enc_edid_default": "SET ENC {encoder_id} EDID DEFAULT {edid}",
    "enc_ir_vol": "SET ENC {encoder_id} IR VOL {voltage}",
    "enc_io_vol": "SET ENC {encoder_id} IO VOL {voltage}",
    "enc_io_dir": "SET ENC {encoder_id} IO {port} DIR {direction}",
    "enc_io_out": "SET ENC {encoder_id} IO {port} OUT {level}",
    "enc_relay": "SET ENC {encoder_id} RELAY {relay} {state}",
    "enc_sac": "SET ENC {encoder_id} SAC {mode}",
    "enc_net": "SET ENC {encoder_id} NET {phy}",
    "enc_usbmode": "SET ENC {encoder_id} USBMODE {mode}",
    "enc_source": "SET ENC {encoder_id} SOURCE {source}",
    "enc_source_auto_priority": "SET ENC {encoder_id} SOURCE AUTO {priority}",
    "enc_fan": "SET ENC {encoder_id} FAN {speed}",
    "enc_cec_send": "SET ENC {encoder_id} CEC SEND {data}",
    "enc_ir_send": "SET ENC {encoder_id} IR SEND {data}",
    "enc_guest_config": "SET ENC {encoder_id} GUEST {state} BR {baud} BIT {bits}",
    "enc_guest_start": "SET ENC {encoder_id} GUEST",
    "enc_sendguest_ascii": "SET ENC {encoder_id} SENDGUEST ASCII {message}",
    "enc_sendguest_hex": "SET ENC {encoder_id} SENDGUEST HEX {message}",
    "enc_ipmode": "SET ENC {encoder_id} IPMODE {mode}",
    "enc_static_ip": "SET ENC {encoder_id} STATIC IP {ip}",
    "enc_static_gateway": "SET ENC {encoder_id} STATIC GATEWAY {gateway}",
    "enc_static_mask": "SET ENC {encoder_id} STATIC MASK {mask}",
    "enc_network_reboot": "SET ENC {encoder_id} NETWORK REBOOT",
    "enc_lanmode": "SET ENC {encoder_id} LANMODE {lanmode}",
    "enc_lan2_ipmode": "SET ENC {encoder_id} LAN2 IPMODE {mode}",
    "enc_lan2_static_ip": "SET ENC {encoder_id} LAN2 STATIC IP {ip}",
    "enc_lan2_static_gateway": "SET ENC {encoder_id} LAN2 STATIC GATEWAY {gateway}",
    "enc_lan2_static_mask": "SET ENC {encoder_id} LAN2 STATIC MASK {mask}",
    "enc_preset_ipmode": "SET ENC PRESET IPMODE {mode}",
    "enc_preset_start_ip": "SET ENC PRESET START IP {ip}",
    "enc_preset_end_ip": "SET ENC PRESET END IP {ip}",
    "enc_preset_gw": "SET ENC PRESET GW {gateway}",
    "enc_preset_sm": "SET ENC PRESET SM {mask}",
    "enc_preset_apply": "SET ENC PRESET APPLY",
    "enc_reboot": "SET ENC {encoder_id} REBOOT",
    "enc_reset": "SET ENC {encoder_id} RESET",
    # Decoder
    "dec_set_name": "SET DEC {decoder_id} NAME {name}",
    "dec_route": "SET DEC {decoder_id} SWITCH {encoder_id} {signal}",
    "dec_led": "SET DEC {decoder_id} LED {state}",
    "dec_led_timeout": "SET DEC {decoder_id} LED ON 90",
    "dec_multicast": "SET DEC {decoder_id} MULTICAST {state}",
    "dec_ull": "SET DEC {decoder_id} ULL {state}",
    "dec_audio_stream": "SET DEC {decoder_id} AUDIO STREAM {stream}",
    "dec_dante_bridge": "SET DEC {decoder_id} DANTE BRIDGE {state}",
    "dec_dante_vlan": "SET DEC {decoder_id} DANTE VLAN {state}",
    "dec_dante_vlan_tag": "SET DEC {decoder_id} DANTE VLAN TAG {tag}",
    "dec_dante_audio_source": "SET DEC {decoder_id} DANTE AUDIO SOURCE {source}",
    "dec_output": "SET DEC {decoder_id} OUTPUT {state}",
    "dec_output_freeze": "SET DEC {decoder_id} OUTPUT FREEZE {state}",
    "dec_output_mute": "SET DEC {decoder_id} OUTPUT MUTE {state}",
    "dec_output_osd": "SET DEC {decoder_id} OUTPUT OSD {state}",
    "dec_output_resolution": "SET DEC {decoder_id} OUTPUT RESOLUTION {resolution}",
    "dec_output_rotate": "SET DEC {decoder_id} OUTPUT ROTATE {rotate}",
    "dec_output_flip": "SET DEC {decoder_id} OUTPUT FLIP {flip}",
    "dec_mode": "SET DEC {decoder_id} MODE {mode}",
    "dec_ir_vol": "SET DEC {decoder_id} IR VOL {voltage}",
    "dec_io_vol": "SET DEC {decoder_id} IO VOL {voltage}",
    "dec_io_dir": "SET DEC {decoder_id} IO {port} DIR {direction}",
    "dec_io_out": "SET DEC {decoder_id} IO {port} OUT {level}",
    "dec_relay": "SET DEC {decoder_id} RELAY {relay} {state}",
    "dec_arp": "SET DEC {decoder_id} ARP {path}",
    "dec_earc_downgrade": "SET DEC {decoder_id} EARC DOWNGRADE {state}",
    "dec_sac": "SET DEC {decoder_id} SAC {mode}",
    "dec_net": "SET DEC {decoder_id} NET {phy}",
    "dec_usb_data": "SET DEC {decoder_id} USB DATA {state}",
    "dec_cec_send": "SET DEC {decoder_id} CEC SEND {data}",
    "dec_ir_send": "SET DEC {decoder_id} IR SEND {data}",
    "dec_hotkey": (
        "SET DEC {decoder_id} HOTKEY {hotkey} KEY {k0} {k1} "
        "ACTION {action} SRC {src}"
    ),
    "dec_hotkey_del": "SET DEC {decoder_id} HOTKEY {hotkey} DEL",
    "dec_guest_config": "SET DEC {decoder_id} GUEST {state} BR {baud} BIT {bits}",
    "dec_guest_start": "SET DEC {decoder_id} GUEST",
    "dec_sendguest_ascii": "SET DEC {decoder_id} SENDGUEST ASCII {message}",
    "dec_sendguest_hex": "SET DEC {decoder_id} SENDGUEST HEX {message}",
    "dec_ipmode": "SET DEC {decoder_id} IPMODE {mode}",
    "dec_static_ip": "SET DEC {decoder_id} STATIC IP {ip}",
    "dec_static_gateway": "SET DEC {decoder_id} STATIC GATEWAY {gateway}",
    "dec_static_mask": "SET DEC {decoder_id} STATIC MASK {mask}",
    "dec_network_reboot": "SET DEC {decoder_id} NETWORK REBOOT",
    "dec_lanmode": "SET DEC {decoder_id} LANMODE {lanmode}",
    "dec_lan2_ipmode": "SET DEC {decoder_id} LAN2 IPMODE {mode}",
    "dec_lan2_static_ip": "SET DEC {decoder_id} LAN2 STATIC IP {ip}",
    "dec_lan2_static_gateway": "SET DEC {decoder_id} LAN2 STATIC GATEWAY {gateway}",
    "dec_lan2_static_mask": "SET DEC {decoder_id} LAN2 STATIC MASK {mask}",
    "dec_preset_ipmode": "SET DEC PRESET IPMODE {mode}",
    "dec_preset_start_ip": "SET DEC PRESET START IP {ip}",
    "dec_preset_end_ip": "SET DEC PRESET END IP {ip}",
    "dec_preset_gw": "SET DEC PRESET GW {gateway}",
    "dec_preset_sm": "SET DEC PRESET SM {mask}",
    "dec_preset_apply": "SET DEC PRESET APPLY",
    "dec_reboot": "SET DEC {decoder_id} REBOOT",
    "dec_reset": "SET DEC {decoder_id} RESET",
    "exit_guest": "EXITGUEST",
    # Video wall (non-lifecycle)
    "wall_set_name": "SET WALL {wall_id} NAME {name}",
    "wall_set_size": "SET WALL {wall_id} C {columns} R {rows}",
    "wall_set_dec": "SET WALL {wall_id} DEC {decoder_id} H {h} V {v}",
    "wall_create_preset": "CREATE WALL {wall_id} PRESET {preset}",
    "wall_delete_preset": "DELETE WALL {wall_id} PRESET {preset}",
    "wall_set_preset_name": "SET WALL {wall_id} PRESET {preset} NAME {name}",
    "wall_apply_preset": "APPLY WALL {wall_id} PRESET {preset}",
    "wall_preset_class": "SET WALL {wall_id} PRESET {preset} CLASS {cls} H {h} V {v}",
    "wall_preset_class_source": "SET WALL {wall_id} PRESET {preset} CLASS {cls} SOURCE {encoder_id}",
    "wall_preset_matrix": "SET WALL {wall_id} PRESET {preset} MATRIX H {h} V {v}",
    "wall_preset_matrix_source": "SET WALL {wall_id} PRESET {preset} MATRIX H {h} V {v} SOURCE {encoder_id}",
    "wall_bezel_width": "SET WALL {wall_id} H {h} V {v} WIDTH BEZEL BW {bw} IW {iw}",
    "wall_bezel_height": "SET WALL {wall_id} H {h} V {v} HEIGHT BEZEL BH {bh} IH {ih}",
    # Media (non-lifecycle)
    "media_addr_list": "SET MEDIA ADDR {address} LIST",
    "media_addr_ping": "SET MEDIA ADDR {address} PING",
    "media_set_id": "SET MEDIA {media_id} ID {new_id}",
    "media_set_name": "SET MEDIA {media_id} NAME {name}",
    "media_set_type": "SET MEDIA {media_id} TYPE {media_type}",
    "media_set_addr_file": "SET MEDIA {media_id} ADDR {address} FILE {file}",
    "media_set_user": "SET MEDIA {media_id} USER {user} PWD {password} VER {version}",
    "media_transparency_on": "SET MEDIA {media_id} TRANSPARENCY ON {transparency}",
    "media_transparency_off": "SET MEDIA {media_id} TRANSPARENCY OFF",
    "media_reload": "SET MEDIA {media_id} RELOAD",
    # Group (non-lifecycle)
    "group_set_name": "SET GROUP {group_id} NAME {name}",
    "group_add_dec": "ADD GROUP {group_id} DEC {decoder_id}",
    "group_del_dec": "DELETE GROUP {group_id} DEC {decoder_id}",
    "group_switch": "SET GROUP {group_id} SWITCH {encoder_id} {signal}",
    # Event (non-lifecycle)
    "event_set_name": "SET EVENT {event_id} NAME {name}",
    "event_set_type": "SET EVENT {event_id} TYPE {event_type}",
    "event_set_addr": "SET EVENT {event_id} ADDR {address}",
    "event_set_addr_port": "SET EVENT {event_id} ADDR {address} PORT {port} DEV {dev}",
    "event_set_data": "SET EVENT {event_id} DATA {data}",
    "event_set_data_hex": "SET EVENT {event_id} DATA HEX {data}",
    "event_set_params": "SET EVENT {event_id} PARAMS {params}",
    "event_set_request": "SET EVENT {event_id} REQUEST {request}",
    "event_set_resend_delay": "SET EVENT {event_id} RESEND DELAY {delay}",
    "event_start": "SET EVENT {event_id} START",
    "event_stop": "SET EVENT {event_id} STOP",
    # Schedule (non-lifecycle)
    "schedule_set_name": "SET SCHEDULE {schedule_id} NAME {name}",
    "schedule_set_color": "SET SCHEDULE {schedule_id} COLOR {color}",
    "schedule_set_time_type": "SET SCHEDULE {schedule_id} TIME TYPE {time_type}",
    "schedule_set_week_type": "SET SCHEDULE {schedule_id} WEEK TYPE {week_type} DAY {day}",
    "schedule_set_date": "SET SCHEDULE {schedule_id} DATE S {start} E {end}",
    "schedule_set_time": "SET SCHEDULE {schedule_id} TIME S {start} E {end}",
    "schedule_action_dec_enc": "SET SCHEDULE {schedule_id} ACTION DEC {decoder_id} ENC {encoder_id}",
    "schedule_action_dec_media": "SET SCHEDULE {schedule_id} ACTION DEC {decoder_id} MEDIA {media_id}",
    "schedule_action_group_enc": "SET SCHEDULE {schedule_id} ACTION GROUP {group_id} ENC {encoder_id}",
    "schedule_action_group_media": "SET SCHEDULE {schedule_id} ACTION GROUP {group_id} MEDIA {media_id}",
    "schedule_action_dante_preset": "SET SCHEDULE {schedule_id} ACTION DANTE PRESET {dante_preset_id}",
    "schedule_action_event": "SET SCHEDULE {schedule_id} ACTION EVENT {event_id}",
    "schedule_delete_action": "DELETE SCHEDULE {schedule_id} ACTION",
    "schedule_start": "SET SCHEDULE {schedule_id} START",
    "schedule_stop": "SET SCHEDULE {schedule_id} STOP",
    # Configuration presets (non-lifecycle)
    "config_preset_apply": "APPLY CONFIG PRESET {config_preset_id}",
    # Dante (controller-level, addressed by Dante device name)
    "dante_set_name": "SET DANTE DEV {devname} NAME {name}",
    "dante_set_srate": "SET DANTE DEV {devname} SRATE {rate}",
    "dante_set_encoding": "SET DANTE DEV {devname} ENC {encoding}",
    "dante_set_latency": "SET DANTE DEV {devname} LATENCY {latency}",
    "dante_preferred": "SET DANTE DEV {devname} PREFERRED {state}",
    "dante_aes67": "SET DANTE DEV {devname} AES67 {state}",
    "dante_aes67_prefix": "SET DANTE DEV {devname} AES67 PREFIX {prefix}",
    "dante_reboot": "SET DANTE DEV {devname} REBOOT {mode}",
    "dante_txchn_name": "SET DANTE DEV {devname} {flow} TXCHN {channel} NAME {name}",
    "dante_txflow_add": "SET DANTE DEV {devname} {flow} TXFLOW {name} ID {flow_id} SLOT {slot}",
    "dante_txflow_delete": "SET DANTE DEV {devname} {flow} TXFLOW {flow_id} DELETE",
    "dante_rxchn_name": "SET DANTE DEV {devname} {flow} RXCHN {channel} NAME {name}",
    "dante_rxchn_subscribe": (
        "SET DANTE DEV {devname} {flow} RXCHN {channel} SOURCE {txdev} CHN {src_channel}"
    ),
    "dante_clear_config": "SET DANTE DEV {devname} CLEAR CONFIG {scope}",
    "dante_interface_static": (
        "SET DANTE DEV {devname} INTERFACE {intf} STATIC IP {ip} "
        "MASK {mask} GW {gateway} DNS {dns}"
    ),
    "dante_interface_dynamic": "SET DANTE DEV {devname} INTERFACE {intf} DYNAMIC",
    "dante_search": "DANTE DEV SEARCH",
    "dante_event_clear": "SET DANTE EVENT CLEAR",
    # Dante presets (non-lifecycle)
    "dante_preset_set_name": "SET DANTE PRESET {dante_preset_id} NAME {name}",
    "dante_preset_apply": "APPLY DANTE PRESET {dante_preset_id}",
    # Device management
    "search_reset": "SEARCH RESET",
    "add_dev_enc": "ADD DEV {dev} ENC {encoder_id}",
    "add_dev_dec": "ADD DEV {dev} DEC {decoder_id}",
    "add_dev_reset": "ADD DEV RESET",
    # GPIO (controller)
    "gpio_dir": "SET GPIO {gpio} DIR {direction}",
    "gpio_level": "SET GPIO {gpio} LEVEL {level}",
    # Network (controller)
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
    "net_hostname": "SET NETWORK HOSTNAME {hostname}",
    "net_dns": "SET NETWORK DNS MODE {mode} PREFER {prefer} BACKUP {backup} DEV {lan}",
    # Date / time
    "set_date": "SET DATE {datetime}",
    "set_ntp_server": "SET NTP SERVER {address}",
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
    "group_create": {
        "template": "CREATE GROUP HANDLE {group_id}", "child_type": "group",
        "action": "register", "id_param": "group_id",
    },
    "group_delete": {
        "template": "DELETE GROUP HANDLE {group_id}", "child_type": "group",
        "action": "deregister", "id_param": "group_id",
    },
    "event_create": {
        "template": "CREATE EVENT HANDLE {event_id}", "child_type": "event",
        "action": "register", "id_param": "event_id",
    },
    "event_delete": {
        "template": "DELETE EVENT HANDLE {event_id}", "child_type": "event",
        "action": "deregister", "id_param": "event_id",
    },
    "schedule_create": {
        "template": "CREATE SCHEDULE HANDLE {schedule_id}", "child_type": "schedule",
        "action": "register", "id_param": "schedule_id",
    },
    "schedule_delete": {
        "template": "DELETE SCHEDULE HANDLE {schedule_id}", "child_type": "schedule",
        "action": "deregister", "id_param": "schedule_id",
    },
    "media_add": {
        "template": "ADD MEDIA HANDLE {media_id}", "child_type": "media",
        "action": "register", "id_param": "media_id",
    },
    "media_delete": {
        "template": "SET MEDIA {media_id} DELETE", "child_type": "media",
        "action": "deregister", "id_param": "media_id",
    },
    "dante_preset_create": {
        "template": "CREATE DANTE PRESET HANDLE {dante_preset_id}",
        "child_type": "dante_preset", "action": "register", "id_param": "dante_preset_id",
    },
    "dante_preset_delete": {
        "template": "DELETE DANTE PRESET HANDLE {dante_preset_id}",
        "child_type": "dante_preset", "action": "deregister", "id_param": "dante_preset_id",
    },
    "config_preset_save": {
        "template": "SAVE CONFIG PRESET {config_preset_id} NAME {name}",
        "child_type": "config_preset", "action": "register", "id_param": "config_preset_id",
    },
    "config_preset_delete": {
        "template": "DELETE CONFIG PRESET {config_preset_id}",
        "child_type": "config_preset", "action": "deregister", "id_param": "config_preset_id",
    },
}

# Destructive commands that require a 'Yes' confirmation (handled by
# _send_with_confirm). Mapped to their base wire command.
_RESET_CONFIRM: dict[str, str] = {
    "reset_system_confirm": "SET RESET",
    "reset_network_confirm": "SET RESET NETWORK",
    "reset_all_confirm": "SET RESET ALL",
}

# Wire code -> child enum value for media source type (the SET command takes the
# numeric code; the child state var carries the human label).
_MEDIA_TYPE_CODE = {"01": "SAMBA", "02": "NFS", "03": "FTP"}

# Optimistic child-state writes applied after a SET command succeeds, for the
# device settings the controller has no GET read-back for (so the IDE doesn't
# show them permanently blank). Maps command ->
# (child_type, id_param, builder(params) -> {state_var: value}).
_POST_SET_STATE: dict[str, tuple[str, str, Any]] = {
    "dec_output_freeze": ("decoder", "decoder_id", lambda p: {"video_freeze": p["state"] == "ON"}),
    "dec_output_osd": ("decoder", "decoder_id", lambda p: {"osd": p["state"] == "ON"}),
    "dec_ull": ("decoder", "decoder_id", lambda p: {"ull": p["state"] == "ON"}),
    "dec_dante_audio_source": ("decoder", "decoder_id", lambda p: {"dante_audio_source": p["source"]}),
    "dec_arp": ("decoder", "decoder_id", lambda p: {"arp": p["path"]}),
    "dec_earc_downgrade": ("decoder", "decoder_id", lambda p: {"earc_downgrade": p["state"] == "ON"}),
    "media_set_name": ("media", "media_id", lambda p: {"name": p["name"]}),
    "media_set_type": ("media", "media_id",
                       lambda p: {"media_type": _MEDIA_TYPE_CODE.get(p["media_type"], p["media_type"])}),
    "media_set_addr_file": ("media", "media_id",
                            lambda p: {"address": p["address"], "file": p["file"]}),
}


def _build_commands() -> dict[str, dict[str, Any]]:
    """Build the DRIVER_INFO['commands'] dict (labels, params, help) that the
    IDE renders. Wire formats live in _COMMAND_TEMPLATES / _LIFECYCLE_COMMANDS.
    """
    onoff = {"type": "enum", "values": ["ON", "OFF"], "required": True}

    def enc_id(label="Encoder"):
        return {"type": "child_id", "child_type": "encoder", "required": True, "label": label}

    def dec_id(label="Decoder"):
        return {"type": "child_id", "child_type": "decoder", "required": True, "label": label}

    cmds: dict[str, dict[str, Any]] = {
        # ── System ──
        "reboot_controller": {"label": "Reboot Controller", "params": {},
                              "help": "Reboot the controller."},
        "set_rs232_baud": {
            "label": "Set RS-232 Baud", "params": {
                "baud": {"type": "enum", "values": ["0", "1", "2", "3", "4"], "required": True,
                         "help": "0:115200 1:57600 2:38400 3:19200 4:9600"},
            }, "help": "Set the controller RS-232 baud rate."},
        "reset_system_confirm": {"label": "Factory Reset: System Settings", "params": {},
                                 "help": "Reset controller system settings to default (auto-confirms)."},
        "reset_network_confirm": {"label": "Factory Reset: Network Settings", "params": {},
                                  "help": "Reset controller network settings to default (auto-confirms)."},
        "reset_all_confirm": {"label": "Factory Reset: System + Network", "params": {},
                              "help": "Reset all controller settings to default (auto-confirms)."},

        # ── Encoder ──
        "enc_set_name": {"label": "Encoder: Set Name", "params": {
            "encoder_id": enc_id(), "name": {"type": "string", "required": True}},
            "help": "Set an encoder's name (max 32 chars)."},
        "enc_set_id": {"label": "Encoder: Renumber", "params": {
            "encoder_id": enc_id(), "new_id": {"type": "integer", "required": True, "min": 1,
                                               "max": ENC_MAX, "label": "New ID"}},
            "help": "Change an encoder's index ID."},
        "enc_delete": {"label": "Encoder: Delete", "params": {"encoder_id": enc_id()},
                       "help": "Remove an encoder from the controller config."},
        "enc_switch_arc": {"label": "Encoder: Route ARC", "params": {
            "encoder_id": enc_id(), "decoder_id": dec_id("Decoder (0 = clear)")},
            "help": "Route an encoder's ARC-only signal to a decoder; decoder 0 closes it."},
        "enc_led": {"label": "Encoder: Power LED Flash", "params": {
            "encoder_id": enc_id(), "state": onoff}, "help": "Flash the encoder power LED."},
        "enc_multicast": {"label": "Encoder: Multicast", "params": {
            "encoder_id": enc_id(), "state": onoff}, "help": "Multicast transmit on/off."},
        "enc_dante_bridge": {"label": "Encoder: Dante Bridge", "params": {
            "encoder_id": enc_id(), "state": onoff}},
        "enc_dante_vlan": {"label": "Encoder: Dante VLAN", "params": {
            "encoder_id": enc_id(), "state": onoff}},
        "enc_dante_vlan_tag": {"label": "Encoder: Dante VLAN Tag", "params": {
            "encoder_id": enc_id(), "tag": {"type": "integer", "required": True, "min": 1, "max": 4095}}},
        "enc_audio_stream": {"label": "Encoder: Audio Stream", "params": {
            "encoder_id": enc_id(), "stream": {"type": "enum", "values": ["DANTE", "AES67", "NONE"],
                                               "required": True}}},
        "enc_audio_input": {"label": "Encoder: Audio Input", "params": {
            "encoder_id": enc_id(), "source": {"type": "enum", "values": ["HDMI", "ANA"],
                                               "required": True}}},
        "enc_edid_copy": {"label": "Encoder: Copy EDID from Decoder", "params": {
            "encoder_id": enc_id(), "decoder_id": dec_id()}},
        "enc_edid_default": {"label": "Encoder: Set Default EDID", "params": {
            "encoder_id": enc_id(), "edid": {"type": "string", "required": True,
                                             "help": "EDID preset index (00-27 built-in, 101/102 user)."}}},
        "enc_ir_vol": {"label": "Encoder: IR Voltage", "params": {
            "encoder_id": enc_id(), "voltage": {"type": "enum", "values": ["5V", "12V"],
                                                "required": True}}},
        "enc_io_vol": {"label": "Encoder: IO Voltage", "params": {
            "encoder_id": enc_id(), "voltage": {"type": "enum", "values": ["5V", "12V"],
                                                "required": True}}},
        "enc_io_dir": {"label": "Encoder: IO Direction", "params": {
            "encoder_id": enc_id(), "port": {"type": "enum", "values": ["1", "2"], "required": True},
            "direction": {"type": "enum", "values": ["IN", "OUT"], "required": True}}},
        "enc_io_out": {"label": "Encoder: IO Output Level", "params": {
            "encoder_id": enc_id(), "port": {"type": "enum", "values": ["1", "2"], "required": True},
            "level": {"type": "enum", "values": ["0", "1"], "required": True}}},
        "enc_relay": {"label": "Encoder: Relay", "params": {
            "encoder_id": enc_id(), "relay": {"type": "enum", "values": ["1", "2"], "required": True},
            "state": {"type": "enum", "values": ["OPEN", "CLOSE"], "required": True}}},
        "enc_sac": {"label": "Encoder: Shared Audio Pin", "params": {
            "encoder_id": enc_id(), "mode": {"type": "enum", "values": ["ARC", "CEC", "OFF"],
                                             "required": True}}},
        "enc_net": {"label": "Encoder: Network PHY", "params": {
            "encoder_id": enc_id(), "phy": {"type": "enum", "values": ["FIBER", "COPPER"],
                                            "required": True}}},
        "enc_usbmode": {"label": "Encoder: USB Mode", "params": {
            "encoder_id": enc_id(), "mode": {"type": "enum", "values": ["AUTO", "HOST", "TYPEC"],
                                             "required": True}}},
        "enc_source": {"label": "Encoder: Video Source", "params": {
            "encoder_id": enc_id(), "source": {"type": "enum", "values": ["AUTO", "HDMI", "TYPEC"],
                                               "required": True}}},
        "enc_fan": {"label": "Encoder: Fan Speed", "params": {
            "encoder_id": enc_id(), "speed": {"type": "enum",
                                              "values": ["SILENT", "LOW", "STANDARD", "HIGH", "AUTO"],
                                              "required": True}}},
        "enc_cec_send": {"label": "Encoder: Send CEC", "params": {
            "encoder_id": enc_id(), "data": {"type": "string", "required": True,
                                             "help": "Hex bytes, e.g. '40 04'."}}},
        "enc_ir_send": {"label": "Encoder: Send IR", "params": {
            "encoder_id": enc_id(), "data": {"type": "string", "required": True,
                                             "help": "Hex IR data."}}},
        "enc_sendguest_ascii": {"label": "Encoder: Send Serial (ASCII)", "params": {
            "encoder_id": enc_id(), "message": {"type": "string", "required": True}}},
        "enc_sendguest_hex": {"label": "Encoder: Send Serial (Hex)", "params": {
            "encoder_id": enc_id(), "message": {"type": "string", "required": True}}},
        "enc_led_timeout": {"label": "Encoder: Flash LED (90s)", "params": {"encoder_id": enc_id()}},
        "enc_source_auto_priority": {"label": "Encoder: Auto Source Priority", "params": {
            "encoder_id": enc_id(), "priority": {"type": "enum", "values": ["NONE", "HDMI", "TYPEC"],
                                                 "required": True}}},
        "enc_guest_config": {"label": "Encoder: Serial Guest Config", "params": {
            "encoder_id": enc_id(), "state": onoff, "baud": _baud(),
            "bits": {"type": "string", "required": True, "help": "Data/parity/stop, e.g. 8n1"}}},
        "enc_guest_start": {"label": "Encoder: Start Serial Guest", "params": {"encoder_id": enc_id()},
                            "help": "Enter interactive serial guest mode (exit with Exit Serial Guest)."},
        "enc_ipmode": {"label": "Encoder: IP Mode", "params": {
            "encoder_id": enc_id(), "mode": _ipmode_ds()}},
        "enc_static_ip": {"label": "Encoder: Static IP", "params": {
            "encoder_id": enc_id(), "ip": _ipparam()}},
        "enc_static_gateway": {"label": "Encoder: Static Gateway", "params": {
            "encoder_id": enc_id(), "gateway": _ipparam("Gateway")}},
        "enc_static_mask": {"label": "Encoder: Static Mask", "params": {
            "encoder_id": enc_id(), "mask": _ipparam("Subnet Mask")}},
        "enc_network_reboot": {"label": "Encoder: Reboot NIC", "params": {"encoder_id": enc_id()}},
        "enc_lanmode": {"label": "Encoder: LAN Mode", "params": {
            "encoder_id": enc_id(), "lanmode": {"type": "enum", "values": ["1", "2"], "required": True}}},
        "enc_lan2_ipmode": {"label": "Encoder: LAN2 IP Mode", "params": {
            "encoder_id": enc_id(), "mode": _ipmode_ds()}},
        "enc_lan2_static_ip": {"label": "Encoder: LAN2 Static IP", "params": {
            "encoder_id": enc_id(), "ip": _ipparam()}},
        "enc_lan2_static_gateway": {"label": "Encoder: LAN2 Static Gateway", "params": {
            "encoder_id": enc_id(), "gateway": _ipparam("Gateway")}},
        "enc_lan2_static_mask": {"label": "Encoder: LAN2 Static Mask", "params": {
            "encoder_id": enc_id(), "mask": _ipparam("Subnet Mask")}},
        "enc_preset_ipmode": {"label": "Encoder Preset: IP Mode", "params": {"mode": _ipmode_012()}},
        "enc_preset_start_ip": {"label": "Encoder Preset: Start IP", "params": {"ip": _ipparam()}},
        "enc_preset_end_ip": {"label": "Encoder Preset: End IP", "params": {"ip": _ipparam()}},
        "enc_preset_gw": {"label": "Encoder Preset: Gateway", "params": {"gateway": _ipparam("Gateway")}},
        "enc_preset_sm": {"label": "Encoder Preset: Subnet Mask", "params": {"mask": _ipparam("Subnet Mask")}},
        "enc_preset_apply": {"label": "Encoder Preset: Apply", "params": {}},
        "enc_reboot": {"label": "Encoder: Reboot", "params": {"encoder_id": enc_id()}},
        "enc_reset": {"label": "Encoder: Factory Reset", "params": {"encoder_id": enc_id()}},

        # ── Decoder ──
        "dec_set_name": {"label": "Decoder: Set Name", "params": {
            "decoder_id": dec_id(), "name": {"type": "string", "required": True}}},
        "dec_set_id": {"label": "Decoder: Renumber", "params": {
            "decoder_id": dec_id(), "new_id": {"type": "integer", "required": True, "min": 1,
                                               "max": DEC_MAX, "label": "New ID"}}},
        "dec_delete": {"label": "Decoder: Delete", "params": {"decoder_id": dec_id()}},
        "dec_route": {"label": "Decoder: Route Source", "params": {
            "decoder_id": dec_id(), "encoder_id": enc_id("Encoder (0 = clear/follow)"),
            "signal": {"type": "enum", "values": SIGNAL_TYPES, "required": True}},
            "help": "Route an encoder to a decoder for one signal type. Encoder 0 clears/returns to follow."},
        "dec_led": {"label": "Decoder: Power LED Flash", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_multicast": {"label": "Decoder: Multicast", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_ull": {"label": "Decoder: Ultra Low Latency", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_audio_stream": {"label": "Decoder: Audio Stream", "params": {
            "decoder_id": dec_id(), "stream": {"type": "enum", "values": ["DANTE", "AES67", "NONE"],
                                               "required": True}}},
        "dec_dante_bridge": {"label": "Decoder: Dante Bridge", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_dante_vlan": {"label": "Decoder: Dante VLAN", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_dante_vlan_tag": {"label": "Decoder: Dante VLAN Tag", "params": {
            "decoder_id": dec_id(), "tag": {"type": "integer", "required": True, "min": 1, "max": 4095}}},
        "dec_dante_audio_source": {"label": "Decoder: Dante Audio Source", "params": {
            "decoder_id": dec_id(), "source": {"type": "enum", "values": ["DANTE", "NATIVE"],
                                               "required": True}}},
        "dec_output": {"label": "Decoder: Output", "params": {
            "decoder_id": dec_id(), "state": onoff}, "help": "HDMI output on/off."},
        "dec_output_freeze": {"label": "Decoder: Output Freeze", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_output_mute": {"label": "Decoder: Output Mute", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_output_osd": {"label": "Decoder: ID OSD", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_output_resolution": {"label": "Decoder: Output Resolution", "params": {
            "decoder_id": dec_id(), "resolution": {"type": "string", "required": True,
                                                   "help": "Resolution index (00-17)."}}},
        "dec_output_rotate": {"label": "Decoder: Output Rotate", "params": {
            "decoder_id": dec_id(), "rotate": {"type": "enum", "values": ["0", "1", "2", "3"],
                                               "required": True, "help": "0:0 1:90 2:180 3:270"}}},
        "dec_output_flip": {"label": "Decoder: Output Flip", "params": {
            "decoder_id": dec_id(), "flip": {"type": "enum", "values": ["HOR", "VER", "OFF"],
                                             "required": True}}},
        "dec_mode": {"label": "Decoder: Output Mode", "params": {
            "decoder_id": dec_id(), "mode": {"type": "enum", "values": ["MX", "VW"], "required": True}}},
        "dec_ir_vol": {"label": "Decoder: IR Voltage", "params": {
            "decoder_id": dec_id(), "voltage": {"type": "enum", "values": ["5V", "12V"],
                                                "required": True}}},
        "dec_io_vol": {"label": "Decoder: IO Voltage", "params": {
            "decoder_id": dec_id(), "voltage": {"type": "enum", "values": ["5V", "12V"],
                                                "required": True}}},
        "dec_io_dir": {"label": "Decoder: IO Direction", "params": {
            "decoder_id": dec_id(), "port": {"type": "enum", "values": ["1", "2"], "required": True},
            "direction": {"type": "enum", "values": ["IN", "OUT"], "required": True}}},
        "dec_io_out": {"label": "Decoder: IO Output Level", "params": {
            "decoder_id": dec_id(), "port": {"type": "enum", "values": ["1", "2"], "required": True},
            "level": {"type": "enum", "values": ["0", "1"], "required": True}}},
        "dec_relay": {"label": "Decoder: Relay", "params": {
            "decoder_id": dec_id(), "relay": {"type": "enum", "values": ["1", "2"], "required": True},
            "state": {"type": "enum", "values": ["OPEN", "CLOSE"], "required": True}}},
        "dec_arp": {"label": "Decoder: Audio Return Path", "params": {
            "decoder_id": dec_id(), "path": {"type": "enum", "values": ["ARC", "SPDIF"],
                                             "required": True}}},
        "dec_earc_downgrade": {"label": "Decoder: eARC Downgrade", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_sac": {"label": "Decoder: Shared Audio Pin", "params": {
            "decoder_id": dec_id(), "mode": {"type": "enum", "values": ["ARC", "CEC", "OFF"],
                                             "required": True}}},
        "dec_net": {"label": "Decoder: Network PHY", "params": {
            "decoder_id": dec_id(), "phy": {"type": "enum", "values": ["FIBER", "COPPER"],
                                            "required": True}}},
        "dec_usb_data": {"label": "Decoder: USB Data", "params": {
            "decoder_id": dec_id(), "state": onoff}},
        "dec_cec_send": {"label": "Decoder: Send CEC", "params": {
            "decoder_id": dec_id(), "data": {"type": "string", "required": True}}},
        "dec_ir_send": {"label": "Decoder: Send IR", "params": {
            "decoder_id": dec_id(), "data": {"type": "string", "required": True}}},
        "dec_hotkey_del": {"label": "Decoder: Delete Hotkey", "params": {
            "decoder_id": dec_id(), "hotkey": {"type": "integer", "required": True, "min": 1, "max": 20}}},
        "dec_sendguest_ascii": {"label": "Decoder: Send Serial (ASCII)", "params": {
            "decoder_id": dec_id(), "message": {"type": "string", "required": True}}},
        "dec_sendguest_hex": {"label": "Decoder: Send Serial (Hex)", "params": {
            "decoder_id": dec_id(), "message": {"type": "string", "required": True}}},
        "dec_led_timeout": {"label": "Decoder: Flash LED (90s)", "params": {"decoder_id": dec_id()}},
        "dec_hotkey": {"label": "Decoder: Set KVM Hotkey", "params": {
            "decoder_id": dec_id(),
            "hotkey": {"type": "integer", "required": True, "min": 1, "max": 20, "label": "Hotkey #"},
            "k0": {"type": "enum", "values": ["01", "02", "03", "04", "05", "06", "07", "08", "09"],
                   "required": True, "label": "Modifier",
                   "help": "01:LCtrl 02:RCtrl 03:LShift 04:RShift 05:LAlt 06:RAlt "
                           "07:LCtrl+LShift 08:LCtrl+LAlt 09:LShift+LAlt"},
            "k1": {"type": "string", "required": True, "label": "Key", "help": "ASCII code"},
            "action": {"type": "enum", "values": ["PULL", "PUSH"], "required": True},
            "src": {"type": "integer", "required": True, "label": "Source ID",
                    "help": "Encoder or decoder ID"}}},
        "dec_guest_config": {"label": "Decoder: Serial Guest Config", "params": {
            "decoder_id": dec_id(), "state": onoff, "baud": _baud(),
            "bits": {"type": "string", "required": True, "help": "Data/parity/stop, e.g. 8n1"}}},
        "dec_guest_start": {"label": "Decoder: Start Serial Guest", "params": {"decoder_id": dec_id()},
                            "help": "Enter interactive serial guest mode (exit with Exit Serial Guest)."},
        "dec_ipmode": {"label": "Decoder: IP Mode", "params": {
            "decoder_id": dec_id(), "mode": _ipmode_ds()}},
        "dec_static_ip": {"label": "Decoder: Static IP", "params": {
            "decoder_id": dec_id(), "ip": _ipparam()}},
        "dec_static_gateway": {"label": "Decoder: Static Gateway", "params": {
            "decoder_id": dec_id(), "gateway": _ipparam("Gateway")}},
        "dec_static_mask": {"label": "Decoder: Static Mask", "params": {
            "decoder_id": dec_id(), "mask": _ipparam("Subnet Mask")}},
        "dec_network_reboot": {"label": "Decoder: Reboot NIC", "params": {"decoder_id": dec_id()}},
        "dec_lanmode": {"label": "Decoder: LAN Mode", "params": {
            "decoder_id": dec_id(), "lanmode": {"type": "enum", "values": ["1", "2"], "required": True}}},
        "dec_lan2_ipmode": {"label": "Decoder: LAN2 IP Mode", "params": {
            "decoder_id": dec_id(), "mode": _ipmode_ds()}},
        "dec_lan2_static_ip": {"label": "Decoder: LAN2 Static IP", "params": {
            "decoder_id": dec_id(), "ip": _ipparam()}},
        "dec_lan2_static_gateway": {"label": "Decoder: LAN2 Static Gateway", "params": {
            "decoder_id": dec_id(), "gateway": _ipparam("Gateway")}},
        "dec_lan2_static_mask": {"label": "Decoder: LAN2 Static Mask", "params": {
            "decoder_id": dec_id(), "mask": _ipparam("Subnet Mask")}},
        "dec_preset_ipmode": {"label": "Decoder Preset: IP Mode", "params": {"mode": _ipmode_012()}},
        "dec_preset_start_ip": {"label": "Decoder Preset: Start IP", "params": {"ip": _ipparam()}},
        "dec_preset_end_ip": {"label": "Decoder Preset: End IP", "params": {"ip": _ipparam()}},
        "dec_preset_gw": {"label": "Decoder Preset: Gateway", "params": {"gateway": _ipparam("Gateway")}},
        "dec_preset_sm": {"label": "Decoder Preset: Subnet Mask", "params": {"mask": _ipparam("Subnet Mask")}},
        "dec_preset_apply": {"label": "Decoder Preset: Apply", "params": {}},
        "dec_reboot": {"label": "Decoder: Reboot", "params": {"decoder_id": dec_id()}},
        "dec_reset": {"label": "Decoder: Factory Reset", "params": {"decoder_id": dec_id()}},
        "exit_guest": {"label": "Exit Serial Guest Mode", "params": {},
                       "help": "Exit encoder/decoder RS-232 guest mode."},

        # ── Video wall ──
        "wall_create": {"label": "Video Wall: Create", "params": {
            "wall_id": {"type": "integer", "required": True, "min": 1, "max": HDL_MAX, "label": "Wall"}}},
        "wall_delete": {"label": "Video Wall: Delete", "params": {
            "wall_id": {"type": "integer", "required": True, "min": 1, "max": HDL_MAX, "label": "Wall"}}},
        "wall_set_name": {"label": "Video Wall: Set Name", "params": {
            "wall_id": _wall_id(), "name": {"type": "string", "required": True}}},
        "wall_set_size": {"label": "Video Wall: Set Size", "params": {
            "wall_id": _wall_id(), "columns": {"type": "integer", "required": True, "min": 1, "max": 9},
            "rows": {"type": "integer", "required": True, "min": 1, "max": 9}}},
        "wall_set_dec": {"label": "Video Wall: Place Decoder", "params": {
            "wall_id": _wall_id(), "decoder_id": dec_id("Decoder (0 = remove)"),
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"}}},
        "wall_create_preset": {"label": "Video Wall: Create Preset", "params": {
            "wall_id": _wall_id(), "preset": _preset()}},
        "wall_delete_preset": {"label": "Video Wall: Delete Preset", "params": {
            "wall_id": _wall_id(), "preset": _preset()}},
        "wall_set_preset_name": {"label": "Video Wall: Set Preset Name", "params": {
            "wall_id": _wall_id(), "preset": _preset(), "name": {"type": "string", "required": True}}},
        "wall_apply_preset": {"label": "Video Wall: Apply Preset", "params": {
            "wall_id": _wall_id(), "preset": _preset()}},
        "wall_preset_class": {"label": "Video Wall: Preset Class Cell", "params": {
            "wall_id": _wall_id(), "preset": _preset(), "cls": _cls(),
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"}}},
        "wall_preset_class_source": {"label": "Video Wall: Preset Class Source", "params": {
            "wall_id": _wall_id(), "preset": _preset(), "cls": _cls(), "encoder_id": enc_id()}},
        "wall_preset_matrix": {"label": "Video Wall: Preset Matrix Cell", "params": {
            "wall_id": _wall_id(), "preset": _preset(),
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"}}},
        "wall_preset_matrix_source": {"label": "Video Wall: Preset Matrix Source", "params": {
            "wall_id": _wall_id(), "preset": _preset(),
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"},
            "encoder_id": enc_id()}},
        "wall_bezel_width": {"label": "Video Wall: Bezel Width", "params": {
            "wall_id": _wall_id(),
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"},
            "bw": {"type": "integer", "required": True, "min": 100, "max": 1000, "label": "Base Width"},
            "iw": {"type": "integer", "required": True, "min": 100, "max": 1000, "label": "Image Width"}}},
        "wall_bezel_height": {"label": "Video Wall: Bezel Height", "params": {
            "wall_id": _wall_id(),
            "h": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Column"},
            "v": {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Row"},
            "bh": {"type": "integer", "required": True, "min": 100, "max": 1000, "label": "Base Height"},
            "ih": {"type": "integer", "required": True, "min": 100, "max": 1000, "label": "Image Height"}}},

        # ── Media ──
        "media_add": {"label": "Media: Add Source", "params": {
            "media_id": {"type": "integer", "required": True, "min": 1, "max": MEDIA_MAX, "label": "Media"}}},
        "media_delete": {"label": "Media: Delete Source", "params": {"media_id": _media_id()}},
        "media_addr_list": {"label": "Media: List NAS Files", "params": {
            "address": {"type": "string", "required": True, "label": "NAS Path"}}},
        "media_addr_ping": {"label": "Media: Ping Address", "params": {
            "address": {"type": "string", "required": True, "label": "NAS Path"}}},
        "media_set_id": {"label": "Media: Set ID", "params": {
            "media_id": _media_id(), "new_id": {"type": "integer", "required": True, "min": 1,
                                                "max": MEDIA_MAX, "label": "New ID"}}},
        "media_set_name": {"label": "Media: Set Name", "params": {
            "media_id": _media_id(), "name": {"type": "string", "required": True}}},
        "media_set_type": {"label": "Media: Set Type", "params": {
            "media_id": _media_id(), "media_type": {"type": "enum", "values": ["01", "02", "03"],
                                                    "required": True,
                                                    "help": "01:SAMBA 02:NFS 03:FTP"}}},
        "media_set_addr_file": {"label": "Media: Set Address + File", "params": {
            "media_id": _media_id(), "address": {"type": "string", "required": True},
            "file": {"type": "string", "required": True}}},
        "media_set_user": {"label": "Media: Set NAS Credentials", "params": {
            "media_id": _media_id(), "user": {"type": "string", "required": True},
            "password": {"type": "string", "required": True, "secret": True},
            "version": {"type": "string", "required": True, "label": "Protocol Version"}}},
        "media_transparency_on": {"label": "Media: Transparency On", "params": {
            "media_id": _media_id(), "transparency": {"type": "integer", "required": True,
                                                      "min": 0, "max": 31}}},
        "media_transparency_off": {"label": "Media: Transparency Off", "params": {"media_id": _media_id()}},
        "media_reload": {"label": "Media: Reload", "params": {"media_id": _media_id()}},

        # ── Group ──
        "group_create": {"label": "Group: Create", "params": {
            "group_id": {"type": "integer", "required": True, "min": 1, "max": HDL_MAX, "label": "Group"}}},
        "group_delete": {"label": "Group: Delete", "params": {
            "group_id": {"type": "integer", "required": True, "min": 1, "max": HDL_MAX, "label": "Group"}}},
        "group_set_name": {"label": "Group: Set Name", "params": {
            "group_id": _group_id(), "name": {"type": "string", "required": True}}},
        "group_add_dec": {"label": "Group: Add Decoder", "params": {
            "group_id": _group_id(), "decoder_id": dec_id()}},
        "group_del_dec": {"label": "Group: Remove Decoder", "params": {
            "group_id": _group_id(), "decoder_id": dec_id()}},
        "group_switch": {"label": "Group: Route Source", "params": {
            "group_id": _group_id(), "encoder_id": enc_id("Encoder (0 = clear)"),
            "signal": {"type": "enum", "values": ["ALL", "MEDIA"], "required": True}}},

        # ── Event ──
        "event_create": {"label": "Event: Create", "params": {
            "event_id": {"type": "integer", "required": True, "min": 1, "max": HDL_MAX, "label": "Event"}}},
        "event_delete": {"label": "Event: Delete", "params": {
            "event_id": {"type": "integer", "required": True, "min": 1, "max": HDL_MAX, "label": "Event"}}},
        "event_set_name": {"label": "Event: Set Name", "params": {
            "event_id": _event_id(), "name": {"type": "string", "required": True}}},
        "event_set_type": {"label": "Event: Set Type", "params": {
            "event_id": _event_id(), "event_type": {"type": "enum",
                                                    "values": ["01", "02", "03", "04", "05",
                                                               "06", "07", "08", "09"],
                                                    "required": True,
                                                    "help": "01:IR 02:RS232 03:CEC 04:TCP 05:UDP "
                                                            "06:HTTP GET 07:HTTP POST 08:HTTPS GET "
                                                            "09:HTTPS POST"}}},
        # Bare ADDR is only valid for IR/RS232/CEC events (types 01-03), where
        # `address` is the target TX/RX id. Network events (TCP/UDP/HTTP, types
        # 04-09) require the Address + Port form below — the firmware rejects a
        # bare ADDR on a network event with "[ERROR]EVENT unknow param".
        "event_set_addr": {"label": "Event: Set Address (IR/RS232/CEC)", "params": {
            "event_id": _event_id(), "address": {"type": "string", "required": True,
                "help": "IR/RS232/CEC events only — target TX/RX id. Network events "
                        "(TCP/UDP/HTTP) must use 'Set Address + Port' instead."}}},
        "event_set_addr_port": {"label": "Event: Set Address + Port (network)", "params": {
            "event_id": _event_id(), "address": {"type": "string", "required": True,
                "help": "For network events (TCP/UDP/HTTP). DEV selects the egress "
                        "interface: CLAN = Control LAN, VLAN = Video LAN."},
            "port": {"type": "integer", "required": True},
            "dev": {"type": "enum", "values": ["CLAN", "VLAN"], "required": True}}},
        "event_set_data": {"label": "Event: Set Data (ASCII)", "params": {
            "event_id": _event_id(), "data": {"type": "string", "required": True,
                                              "help": "Max 512 bytes."}}},
        "event_set_data_hex": {"label": "Event: Set Data (Hex)", "params": {
            "event_id": _event_id(), "data": {"type": "string", "required": True}}},
        "event_set_params": {"label": "Event: Set Params", "params": {
            "event_id": _event_id(), "params": {"type": "string", "required": True}}},
        "event_set_request": {"label": "Event: Set Request", "params": {
            "event_id": _event_id(), "request": {"type": "string", "required": True}}},
        "event_set_resend_delay": {"label": "Event: Set Resend Delay", "params": {
            "event_id": _event_id(), "delay": {"type": "integer", "required": True, "min": 0, "max": 100}}},
        "event_start": {"label": "Event: Start", "params": {"event_id": _event_id()}},
        "event_stop": {"label": "Event: Stop", "params": {"event_id": _event_id()}},

        # ── Schedule ──
        "schedule_create": {"label": "Schedule: Create", "params": {
            "schedule_id": {"type": "integer", "required": True, "min": 1, "max": HDL_MAX,
                            "label": "Schedule"}}},
        "schedule_delete": {"label": "Schedule: Delete", "params": {
            "schedule_id": {"type": "integer", "required": True, "min": 1, "max": HDL_MAX,
                            "label": "Schedule"}}},
        "schedule_set_name": {"label": "Schedule: Set Name", "params": {
            "schedule_id": _schedule_id(), "name": {"type": "string", "required": True}}},
        "schedule_set_color": {"label": "Schedule: Set Color", "params": {
            "schedule_id": _schedule_id(), "color": {"type": "integer", "required": True,
                                                     "min": 0, "max": 14}}},
        "schedule_set_time_type": {"label": "Schedule: Set Time Type", "params": {
            "schedule_id": _schedule_id(), "time_type": {"type": "enum", "values": ["00", "01", "02"],
                                                         "required": True,
                                                         "help": "00:all-day 01:event-date 02:recurring"}}},
        "schedule_set_week_type": {"label": "Schedule: Set Week Type", "params": {
            "schedule_id": _schedule_id(), "week_type": {"type": "enum", "values": ["00", "01", "02", "03"],
                                                         "required": True,
                                                         "help": "00:all 01:weekends 02:weekdays 03:days"},
            "day": {"type": "string", "required": True,
                    "help": "Days 0-6 (Sun-Sat), comma separated."}}},
        "schedule_set_date": {"label": "Schedule: Set Date Range", "params": {
            "schedule_id": _schedule_id(), "start": {"type": "string", "required": True,
                                                     "label": "Start (YYYY-mm-dd)"},
            "end": {"type": "string", "required": True, "label": "End (YYYY-mm-dd)"}}},
        "schedule_set_time": {"label": "Schedule: Set Time Range", "params": {
            "schedule_id": _schedule_id(), "start": {"type": "string", "required": True,
                                                     "label": "Start (HH:MM)"},
            "end": {"type": "string", "required": True, "label": "End (HH:MM)"}}},
        "schedule_action_dec_enc": {"label": "Schedule: Action Route Decoder", "params": {
            "schedule_id": _schedule_id(), "decoder_id": dec_id(), "encoder_id": enc_id()}},
        "schedule_action_dec_media": {"label": "Schedule: Action Decoder Media", "params": {
            "schedule_id": _schedule_id(), "decoder_id": dec_id(), "media_id": _media_id()}},
        "schedule_action_group_enc": {"label": "Schedule: Action Route Group", "params": {
            "schedule_id": _schedule_id(), "group_id": _group_id(), "encoder_id": enc_id()}},
        "schedule_action_group_media": {"label": "Schedule: Action Group Media", "params": {
            "schedule_id": _schedule_id(), "group_id": _group_id(), "media_id": _media_id()}},
        "schedule_action_dante_preset": {"label": "Schedule: Action Dante Preset", "params": {
            "schedule_id": _schedule_id(), "dante_preset_id": _dante_preset_id()}},
        "schedule_action_event": {"label": "Schedule: Action Event", "params": {
            "schedule_id": _schedule_id(), "event_id": _event_id()}},
        "schedule_delete_action": {"label": "Schedule: Delete Actions", "params": {
            "schedule_id": _schedule_id()}},
        "schedule_start": {"label": "Schedule: Start", "params": {"schedule_id": _schedule_id()}},
        "schedule_stop": {"label": "Schedule: Stop", "params": {"schedule_id": _schedule_id()}},

        # ── Configuration presets ──
        "config_preset_save": {"label": "Config Preset: Save", "params": {
            "config_preset_id": {"type": "integer", "required": True, "min": 1, "max": CONFIG_PRESET_MAX,
                                 "label": "Preset"},
            "name": {"type": "string", "required": True, "help": "Max 16 chars."}}},
        "config_preset_delete": {"label": "Config Preset: Delete", "params": {
            "config_preset_id": {"type": "integer", "required": True, "min": 1, "max": CONFIG_PRESET_MAX,
                                 "label": "Preset"}}},
        "config_preset_apply": {"label": "Config Preset: Apply", "params": {
            "config_preset_id": _config_preset_id()}},

        # ── Dante ──
        "dante_set_name": {"label": "Dante: Set Name", "params": {
            "devname": _devname(), "name": {"type": "string", "required": True}}},
        "dante_set_srate": {"label": "Dante: Set Sample Rate", "params": {
            "devname": _devname(), "rate": {"type": "string", "required": True}}},
        "dante_set_encoding": {"label": "Dante: Set Encoding", "params": {
            "devname": _devname(), "encoding": {"type": "string", "required": True}}},
        "dante_set_latency": {"label": "Dante: Set Latency", "params": {
            "devname": _devname(), "latency": {"type": "string", "required": True}}},
        "dante_preferred": {"label": "Dante: Preferred Master", "params": {
            "devname": _devname(), "state": onoff}},
        "dante_aes67": {"label": "Dante: AES67", "params": {
            "devname": _devname(), "state": onoff}},
        "dante_aes67_prefix": {"label": "Dante: AES67 Prefix", "params": {
            "devname": _devname(), "prefix": {"type": "integer", "required": True, "min": 0, "max": 255}}},
        "dante_reboot": {"label": "Dante: Reboot", "params": {
            "devname": _devname(), "mode": {"type": "enum", "values": ["SOFT", "FACTORY"],
                                            "required": True}}},
        "dante_txchn_name": {"label": "Dante: TX Channel Name", "params": {
            "devname": _devname(), "flow": _flow(),
            "channel": {"type": "integer", "required": True, "label": "Channel"},
            "name": {"type": "string", "required": True}}},
        "dante_txflow_add": {"label": "Dante: Add TX Flow", "params": {
            "devname": _devname(), "flow": _flow(), "name": {"type": "string", "required": True},
            "flow_id": {"type": "integer", "required": True, "label": "Flow ID"},
            "slot": {"type": "string", "required": True, "label": "Slots",
                     "help": "Transmit channel IDs, e.g. 1:2:3"}}},
        "dante_txflow_delete": {"label": "Dante: Delete TX Flow", "params": {
            "devname": _devname(), "flow": _flow(),
            "flow_id": {"type": "integer", "required": True, "label": "Flow ID"}}},
        "dante_rxchn_name": {"label": "Dante: RX Channel Name", "params": {
            "devname": _devname(), "flow": _flow(),
            "channel": {"type": "integer", "required": True, "label": "Channel"},
            "name": {"type": "string", "required": True}}},
        "dante_rxchn_subscribe": {"label": "Dante: Subscribe RX Channel", "params": {
            "devname": _devname(), "flow": _flow(),
            "channel": {"type": "integer", "required": True, "label": "Channel"},
            "txdev": {"type": "string", "required": True, "label": "Source Device"},
            "src_channel": {"type": "integer", "required": True, "label": "Source Channel"}}},
        "dante_clear_config": {"label": "Dante: Clear Config", "params": {
            "devname": _devname(), "scope": {"type": "enum", "values": ["KEEPIP", "ALL"],
                                             "required": True}}},
        "dante_interface_static": {"label": "Dante: Interface Static IP", "params": {
            "devname": _devname(), "intf": {"type": "string", "required": True, "label": "Interface"},
            "ip": _ipparam(), "mask": _ipparam("Subnet Mask"), "gateway": _ipparam("Gateway"),
            "dns": _ipparam("DNS")}},
        "dante_interface_dynamic": {"label": "Dante: Interface DHCP", "params": {
            "devname": _devname(), "intf": {"type": "string", "required": True, "label": "Interface"}}},
        "dante_search": {"label": "Dante: Search Devices", "params": {}},
        "dante_event_clear": {"label": "Dante: Clear Events", "params": {}},

        # ── Dante presets ──
        "dante_preset_create": {"label": "Dante Preset: Create", "params": {
            "dante_preset_id": {"type": "integer", "required": True, "min": 1, "max": HDL_MAX,
                                "label": "Preset"}}},
        "dante_preset_delete": {"label": "Dante Preset: Delete", "params": {
            "dante_preset_id": {"type": "integer", "required": True, "min": 1, "max": HDL_MAX,
                                "label": "Preset"}}},
        "dante_preset_set_name": {"label": "Dante Preset: Set Name", "params": {
            "dante_preset_id": _dante_preset_id(), "name": {"type": "string", "required": True}}},
        "dante_preset_apply": {"label": "Dante Preset: Apply", "params": {
            "dante_preset_id": _dante_preset_id()}},

        # ── Device management ──
        "search": {"label": "Search for Devices", "params": {},
                   "help": "Search the Video LAN for new encoders/decoders."},
        "search_reset": {"label": "Reset Search Results", "params": {}},
        "add_auto_all": {"label": "Add All New Devices", "params": {},
                         "help": "Add every newly-found encoder/decoder to the system."},
        "add_dev_enc": {"label": "Add Encoder from Search", "params": {
            "dev": {"type": "integer", "required": True, "min": 1, "label": "Search Index"},
            "encoder_id": {"type": "integer", "required": True, "min": 1, "max": ENC_MAX,
                           "label": "Assign ID"}}},
        "add_dev_dec": {"label": "Add Decoder from Search", "params": {
            "dev": {"type": "integer", "required": True, "min": 1, "label": "Search Index"},
            "decoder_id": {"type": "integer", "required": True, "min": 1, "max": DEC_MAX,
                           "label": "Assign ID"}}},
        "add_dev_reset": {"label": "Reset All Devices", "params": {},
                          "help": "Wipe all encoders/decoders/video walls/search from the system."},

        # ── GPIO ──
        "gpio_dir": {"label": "GPIO: Set Direction", "params": {
            "gpio": {"type": "enum", "values": ["1", "2", "3", "4"], "required": True},
            "direction": {"type": "enum", "values": ["IN", "OUT"], "required": True}}},
        "gpio_level": {"label": "GPIO: Set Output Level", "params": {
            "gpio": {"type": "enum", "values": ["1", "2", "3", "4"], "required": True},
            "level": {"type": "enum", "values": ["Low", "High"], "required": True}}},

        # ── Network ──
        "net_dhcp": {"label": "Network: DHCP", "params": {
            "lan": _lan(), "state": onoff}},
        "net_static_ip": {"label": "Network: Static IP", "params": {
            "lan": _lan(), "ip": {"type": "string", "required": True}}},
        "net_static_gateway": {"label": "Network: Static Gateway", "params": {
            "lan": _lan(), "gateway": {"type": "string", "required": True}}},
        "net_static_mask": {"label": "Network: Static Mask", "params": {
            "lan": _lan(), "mask": {"type": "string", "required": True}}},
        "net_reboot": {"label": "Network: Reboot NIC", "params": {}},
        "net_telnet": {"label": "Network: Telnet", "params": {"state": onoff}},
        "net_telnet_port": {"label": "Network: Telnet Port", "params": {
            "port": {"type": "integer", "required": True, "min": 22, "max": 65535}}},
        "net_ssh": {"label": "Network: SSH", "params": {"state": onoff}},
        "net_ssh_port": {"label": "Network: SSH Port", "params": {
            "port": {"type": "integer", "required": True, "min": 22, "max": 65535}}},
        "net_https": {"label": "Network: HTTPS", "params": {"state": onoff}},
        "net_hostname": {"label": "Network: Hostname", "params": {
            "hostname": {"type": "string", "required": True}}},
        "net_dns": {"label": "Network: DNS Servers", "params": {
            "mode": {"type": "enum", "values": ["0", "1"], "required": True,
                     "label": "DNS Mode", "help": "0:Auto 1:Manual"},
            "prefer": _ipparam("Preferred DNS"), "backup": _ipparam("Backup DNS"), "lan": _lan()}},

        # ── Date / time ──
        "set_date": {"label": "Set Date/Time", "params": {
            "datetime": {"type": "string", "required": True, "label": "YYYY-mm-dd HH:MM:SS"}}},
        "set_ntp_server": {"label": "Set NTP Server", "params": {
            "address": {"type": "string", "required": True}}},
    }
    return cmds


def _wall_id():
    return {"type": "child_id", "child_type": "video_wall", "required": True, "label": "Wall"}


def _group_id():
    return {"type": "child_id", "child_type": "group", "required": True, "label": "Group"}


def _event_id():
    return {"type": "child_id", "child_type": "event", "required": True, "label": "Event"}


def _schedule_id():
    return {"type": "child_id", "child_type": "schedule", "required": True, "label": "Schedule"}


def _media_id():
    return {"type": "child_id", "child_type": "media", "required": True, "label": "Media"}


def _dante_preset_id():
    return {"type": "child_id", "child_type": "dante_preset", "required": True, "label": "Preset"}


def _config_preset_id():
    return {"type": "child_id", "child_type": "config_preset", "required": True, "label": "Preset"}


def _preset():
    return {"type": "integer", "required": True, "min": 1, "max": 9, "label": "Preset"}


def _cls():
    return {"type": "enum", "values": ["A", "B", "C", "D", "E", "F", "G"], "required": True,
            "label": "Class"}


def _lan():
    return {"type": "enum", "values": ["LAN1", "LAN2"], "required": True, "label": "LAN"}


def _devname():
    return {"type": "string", "required": True, "label": "Dante Device Name"}


def _flow():
    return {"type": "enum", "values": ["AUDIO", "VIDEO"], "required": True, "label": "Flow"}


def _ipparam(label="IP Address"):
    return {"type": "string", "required": True, "label": label, "help": "Dotted IPv4."}


def _ipmode_ds():
    return {"type": "enum", "values": ["DHCP", "STATIC"], "required": True, "label": "IP Mode"}


def _ipmode_012():
    return {"type": "enum", "values": ["0", "1", "2"], "required": True,
            "label": "IP Mode", "help": "0:AUTOIP 1:DHCP 2:STATIC"}


def _baud():
    return {"type": "enum", "values": [str(i) for i in range(10)], "required": True,
            "label": "Baud",
            "help": "0:300 1:600 2:1200 3:2400 4:4800 5:9600 6:19200 7:38400 8:57600 9:115200"}


ChazyControlProDriver.DRIVER_INFO["commands"] = _build_commands()
