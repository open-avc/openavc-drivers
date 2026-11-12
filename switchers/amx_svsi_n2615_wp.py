"""AMX N2615 wallplate encoders. Direct Control reports input policy, physical
selection and playback separately. HControl input events trigger a fresh status
read and its property reads verify transmit and snapshot commands.
"""

from __future__ import annotations
import asyncio
import json
import re
from copy import deepcopy
from openavc.core.device_traffic import get_traffic_recorder
from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError

STATUS = "SVSI_TXGEN2"
NETWORK = "SVSI_NETSTATS"
BIT = {"0": False, "1": True}
CABLE = {"connected": True, "disconnected": False}
INPUTS = {"hdmi": (1, "HDMI"), "usbc": (2, "USB-C")}
POLICY = {"auto": "auto", "hdmi": "hdmi", "usbc": "usbc"}
FIELDS = {
    STATUS: ("identity", str),
    "NAME": ("device_name", str),
    "SWVER": ("firmware", str),
    "MAC": ("mac", str),
    "IP": ("ip", str),
    "CHASSISID": ("switch_chassis", str),
    "SYSNAME": ("switch_name", str),
    "PORTID": ("switch_port", str),
    "PORTDESCR": ("switch_port_description", str),
    "VSRC": ("selection_mode", POLICY),
    "VSTS": ("active_input", {"1": "hdmi", "2": "usbc"}),
    "PLAYMODE": ("play_mode", {"local": "local", "live": "live"}),
    "PLAYLIST": ("playlist", int),
    "STREAM": ("stream_number", int),
    "ID": ("identified", {"0": True, "1": False}),
    "MUTE": ("audio_muted", BIT),
    "VMUTE": ("video_muted", BIT),
    "HDMIAUDIO": ("audio_source", {"on": "HDMI", "off": "Analog"}),
    "SAMPLE": ("sample_rate", str),
    "KVMENABLESTATUS": ("kvm_enabled", BIT),
    "USB20ENABLESTATUS": ("usb_enabled", BIT),
}
INPUT_FIELDS = {
    "HDMIINPUT": (1, "connected", CABLE),
    "INPUTRES": (1, "resolution", str),
    "USBINPUT": (2, "connected", CABLE),
    "USBINPUTRES": (2, "resolution", str),
}
EVENTS = {
    "/encoder/systemStatus/" + event
    for event in (
        "hdmiCableStatusChanged",
        "hdmiResolutionChanged",
        "usbCCableStatusChanged",
    )
}
HC_FLAGS = {
    "mwc_tx": "/encoder/streamSetup/mwcStream/txEnable",
    "image_enabled": "/encoder/streamSetup/mwcStream/previewImage",
}
TOGGLES = {
    "audio_muted": ("unmute", "mute"),
    "video_muted": ("vunmute", "vmute"),
    "usb_enabled": ("setSettings:usbEnable:off", "setSettings:usbEnable:on"),
    "kvm_enabled": ("setSettings:kvmEnable:off", "setSettings:kvmEnable:on"),
    "mwc_tx": ("txdisable", "txenable"),
    "image_enabled": ("setSettings:Preview:off", "setSettings:Preview:on"),
}
ENCODER_MODEL = re.compile(
    r"(?:NMX-ENC-)?N2615D?(?:-?WP(?:-(?:NA|EK))?|-(?:NA|EK))(?=\d|[ :\t]|-\d|$)", re.I
)


def convert(reader, value):
    return reader[value.lower()] if isinstance(reader, dict) else reader(value)


def split_field(line):
    # The N2615 table prints the TX header joined to the identity. Other
    # N-Series Direct Control examples put a colon between them.
    for header in (STATUS, NETWORK):
        if line.upper().startswith(header):
            return header, line[len(header) :].lstrip(": \t")
    wire, _, value = line.partition(":")
    return wire.upper(), value.strip()


def number(value, low, high):
    if not re.fullmatch(r"[0-9]+", str(value)) or not low <= int(value) <= high:
        raise CommandRefusedError(f"Enter a whole number from {low} to {high}.")
    return int(value)


def flag(value):
    if isinstance(value, bool):
        return value
    values = {"true": True, "false": False, "1": True, "0": False}
    try:
        return values[str(value).lower()]
    except KeyError:
        raise CommandRefusedError("Choose on or off.") from None


def decode_value(reader, value):
    try:
        return convert(reader, value)
    except (KeyError, ValueError):
        return None


def policy(value):
    if value not in POLICY:
        raise CommandRefusedError("Choose HDMI, USB-C or Auto.")
    return value


def operation(command, key, value):
    if key in TOGGLES:
        enabled = flag(value)
        return TOGGLES[key][enabled], {key: enabled}
    if key == "selection_mode":
        return "vidsrc:" + policy(value), {key: value}
    if key == "audio_source":
        if value not in ("HDMI", "Analog"):
            raise CommandRefusedError("Choose HDMI or Analog.")
        return "setSettings:hdmiAudio:" + ("on" if value == "HDMI" else "off"), {
            key: value
        }
    if key == "stream_number":
        stream = number(value, 1, 32511)
        return f"setSettings:setStream:{stream}", {key: stream}
    return playback_or_identify(command, value)


def playback_or_identify(command, value):
    if command == "play_local":
        playlist = number(value, 1, 8)
        return f"local:{playlist}", {"play_mode": "local", "playlist": playlist - 1}
    if command == "play_live":
        return "live", {"play_mode": "live"}
    if command == "identify":
        return "idOn", {"identified": True}
    raise CommandRefusedError("Unknown wallplate command.")


class AMXN2615WPDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "amx_svsi_n2615_wp",
        "name": "AMX SVSI N2615 HDMI / USB-C Wallplate Encoders",
        "manufacturer": "AMX",
        "author": "OpenAVC",
        "category": "switcher",
        "version": "1.0.0",
        "transport": "tcp",
        "ports": [50002, 50102],
        "web_ui": "{web_scheme}://{host}",
        "delimiter": "\r",
        "simulated": True,
        "verified": False,
        "min_platform_version": "0.37.2",
        "protocols": ["amx-svsi-direct-control", "hcontrol"],
        "tags": ["av-over-ip", "encoder", "wallplate", "usb-c", "svsi", "dante-av-a"],
        "source_url": "https://www.amx.com/en-US/products/nmx-enc-n2615d-wp",
        "description": "Switch HDMI and USB-C or let the last-connected source win. Reports active input, "
        "cable presence and incoming resolution, and controls SVSI streaming, audio, USB "
        "and KVM.",
        "compatible_models": [
            {
                "manufacturer": "AMX",
                "models": [
                    "NMX-ENC-N2615D-WP-NA",
                    "NMX-ENC-N2615D-WP-EK",
                    "NMX-ENC-N2615-WP-NA",
                    "NMX-ENC-N2615-WP-EK",
                ],
                "confidence": "untested",
                "notes": "US and EU/UK N2615 wallplate encoder versions. D and non-D "
                "hardware share firmware; Dante licensing and routing mode are "
                "commissioned separately. Simulator-tested, awaiting physical "
                "qualification.",
            }
        ],
        "help": {
            "overview": "One wallplate takes HDMI and USB-C video. In Auto, the latest connected "
            "source wins. Check the active input, cable and resolution separately. USB-C "
            "video requires a DisplayPort Alt Mode capable source and cable.",
            "setup": "1. Enter the address of the individual wallplate. Use ordinary TCP 50002, or "
            "TLS 50102 with its command password and trusted certificate. Each Direct "
            "Control port accepts one controller at a time.\n"
            "2. Match Routing configured on encoder to the wallplate. Leave network stream "
            "assignment with Dante when Dante AV-A mode is enabled.\n"
            "3. In Matrix, select HDMI, USB-C or Auto. Local playlist playback clears the "
            "live-source highlight. Device Settings can change the switching policy while "
            "local content plays.\n"
            "4. Ordinary HControl on TCP 4197 adds live cable events and HDMI resolution "
            "events. Keep periodic status refresh enabled for changes without a documented "
            "event, including USB-C timing changes. The snapshot URL is published when web "
            "preview is confirmed enabled.",
            "connection": "Check control port, TLS, command password and whether another controller "
            "holds the port. Check wallplate power, cable detection, incoming timing, "
            "playback mode and mutes when video is missing.",
        },
        "default_config": {
            "host": "",
            "port": 50002,
            "ssl": False,
            "verify_ssl": True,
            "password": "",
            "hdmi_events": True,
            "poll_interval": 10,
            "routing_owner": "SVSI",
            "web_scheme": "https",
            "simulation_variant": "NMX-ENC-N2615D-WP-NA",
        },
        "config_schema": {
            "host": {"type": "string", "label": "Encoder address", "required": True},
            "port": {
                "type": "integer",
                "label": "Control port",
                "default": 50002,
                "min": 1,
                "max": 65535,
                "help": "50002 for ordinary control or 50102 for secure control.",
            },
            "ssl": {
                "type": "boolean",
                "label": "Secure Direct Control (TLS)",
                "default": False,
            },
            "verify_ssl": {
                "type": "boolean",
                "label": "Verify encoder certificate",
                "default": True,
                "advanced": True,
            },
            "password": {
                "type": "string",
                "label": "Command password",
                "default": "",
                "secret": True,
                "help": "Required for secure control. Enter the command password "
                "configured on the encoder.",
            },
            "hdmi_events": {
                "type": "boolean",
                "label": "Listen for wallplate input changes",
                "default": True,
                "help": "Use HControl on ordinary TCP 4197 for input events and "
                "transmitter and preview status. Secure connections use "
                "Direct Control replies and polling.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status refresh (seconds)",
                "default": 10,
                "min": 0,
                "advanced": True,
            },
            "routing_owner": {
                "type": "enum",
                "label": "Routing configured on encoder",
                "values": ["SVSI", "Dante AV-A"],
                "default": "SVSI",
                "help": "Match the network routing mode commissioned on the "
                "wallplate. A Dante license and an enabled Dante "
                "routing mode are separate. Local input selection is "
                "available in either profile.",
            },
            "web_scheme": {
                "type": "enum",
                "label": "Encoder web preview protocol",
                "values": ["https", "http"],
                "default": "https",
                "advanced": True,
                "help": "Match the encoder web service for its snapshot URL. The "
                "driver never changes the web service.",
            },
            "simulation_variant": {
                "type": "enum",
                "label": "Wallplate model for simulation",
                "values": [
                    "NMX-ENC-N2615D-WP-NA",
                    "NMX-ENC-N2615D-WP-EK",
                    "NMX-ENC-N2615-WP-NA",
                    "NMX-ENC-N2615-WP-EK",
                ],
                "default": "NMX-ENC-N2615D-WP-NA",
                "advanced": True,
                "help": "Live connections identify the N2615 wallplate "
                "from its own response.",
            },
        },
        "discovery": {
            "port_open": [50002, 50102],
            "manufacturer_alias": ["amx", "svsi"],
            "tcp_probe": {
                "port": 50002,
                "send_ascii": "getStatus\r",
                "expect_regex": "(?i)SVSI_TXGEN2\\s*:?\\s*(?:NMX-ENC-)?N2615D?(?:-?WP(?:-(?:NA|EK))?|-(?:NA|EK))(?=\\d|[\\s:]|-\\d|$)",
                "timeout_ms": 1500,
            },
        },
        "state_variables": {
            "identity": {
                "type": "string",
                "label": "Encoder identifier",
                "cloud_priority": "low",
            },
            "device_name": {"type": "string", "label": "Device name"},
            "firmware": {
                "type": "string",
                "label": "Firmware version",
                "cloud_priority": "low",
            },
            "mac": {"type": "string", "label": "MAC address", "cloud_priority": "low"},
            "ip": {"type": "string", "label": "IP address", "cloud_priority": "low"},
            "switch_name": {
                "type": "string",
                "label": "Connected switch",
                "cloud_priority": "low",
            },
            "switch_port": {
                "type": "string",
                "label": "Connected switch port",
                "cloud_priority": "low",
            },
            "switch_chassis": {
                "type": "string",
                "label": "Connected switch chassis ID",
                "cloud_priority": "low",
            },
            "switch_port_description": {
                "type": "string",
                "label": "Switch port description",
                "cloud_priority": "low",
            },
            "selection_mode": {
                "type": "string",
                "label": "Input switching policy",
                "control": True,
                "cloud_priority": "high",
            },
            "audio_source": {
                "type": "enum",
                "label": "Audio input",
                "values": ["HDMI", "Analog"],
                "control": True,
            },
            "stream_number": {
                "type": "integer",
                "label": "MWC video stream number",
                "min": 1,
                "max": 32511,
                "control": True,
            },
            "audio_muted": {"type": "boolean", "label": "Audio muted", "control": True},
            "video_muted": {"type": "boolean", "label": "Video muted", "control": True},
            "usb_enabled": {
                "type": "boolean",
                "label": "USB 2.0 enabled",
                "control": True,
            },
            "kvm_enabled": {"type": "boolean", "label": "KVM enabled", "control": True},
            "play_mode": {"type": "string", "label": "Playback mode"},
            "playlist": {"type": "integer", "label": "Reported local playlist index"},
            "sample_rate": {"type": "string", "label": "Reported audio sample rate"},
            "identified": {
                "type": "boolean",
                "label": "Identification active",
                "control": False,
            },
            "mwc_tx": {
                "type": "boolean",
                "label": "MWC transmit enabled",
                "help": "Reported through HControl on ordinary connections.",
            },
            "image_enabled": {
                "type": "boolean",
                "label": "Web snapshot enabled",
                "help": "Reported through HControl on ordinary connections.",
            },
            "snapshot_url": {
                "type": "string",
                "label": "Wallplate snapshot URL",
                "cloud_priority": "low",
            },
            "active_input": {
                "type": "string",
                "label": "Physical input selected by wallplate",
                "cloud_priority": "high",
            },
            "live_source": {
                "type": "string",
                "label": "Selected live input",
                "control": True,
                "cloud_priority": "high",
            },
            "input_feedback": {"type": "string", "label": "Input feedback connection"},
        },
        "child_entity_types": {
            "input": {
                "label": "Wallplate input",
                "label_plural": "Wallplate inputs",
                "id_format": {"type": "integer", "min": 1, "max": 2},
                "instances": {"count": 2},
                "state_variables": {
                    "connected": {
                        "type": "boolean",
                        "label": "Cable detected",
                        "cloud_priority": "high",
                    },
                    "resolution": {"type": "string", "label": "Input resolution"},
                    "selected": {
                        "type": "boolean",
                        "label": "Active input",
                        "cloud_priority": "high",
                    },
                },
                "summary_fields": ["connected", "resolution", "selected"],
            }
        },
        "routing": {
            "source_param": "source",
            "planes": [
                {
                    "label": "Wallplate video input",
                    "route_property": "live_source",
                    "command": "route_input",
                }
            ],
        },
        "commands": {
            "refresh_status": {
                "label": "Read wallplate status",
                "query_for": "identity",
            },
            "route_input": {
                "label": "Route HDMI, USB-C or Auto",
                "params": {
                    "source": {
                        "type": "enum",
                        "label": "Input",
                        "values": [
                            {"value": "hdmi", "label": "HDMI"},
                            {"value": "usbc", "label": "USB-C"},
                            {"value": "auto", "label": "Auto (last connected)"},
                        ],
                        "required": True,
                    }
                },
                "sets": {"selection_mode": "{source}", "play_mode": "live"},
                "help": "Return to live input and select HDMI, USB-C or "
                "last-connected Auto. The Matrix highlight follows the "
                "physical input reported by the wallplate.",
            },
            "set_selection_mode": {
                "label": "Set input switching policy",
                "params": {
                    "value": {
                        "type": "enum",
                        "label": "Policy",
                        "required": True,
                        "values": [
                            {"value": "hdmi", "label": "HDMI"},
                            {"value": "usbc", "label": "USB-C"},
                            {"value": "auto", "label": "Auto (last connected)"},
                        ],
                    }
                },
                "sets": {"selection_mode": "{value}"},
                "help": "Select an input or Auto without changing live or "
                "local playback.",
            },
            "play_live": {
                "label": "Return to live input",
                "sets": {"play_mode": "live"},
            },
            "play_local": {
                "label": "Play local playlist",
                "params": {
                    "value": {
                        "type": "integer",
                        "label": "Playlist",
                        "required": True,
                        "min": 1,
                        "max": 8,
                    }
                },
                "sets": {"play_mode": "local"},
                "help": "Choose playlist 1–8. The status packet reports its own "
                "playlist index, 0–7.",
            },
            "set_stream_number": {
                "label": "Assign SVSI stream number",
                "params": {
                    "value": {
                        "type": "integer",
                        "label": "SVSI stream",
                        "required": True,
                        "min": 1,
                        "max": 32511,
                    }
                },
                "sets": {"stream_number": "{value}"},
            },
            "set_audio_source": {
                "label": "Select audio input",
                "params": {
                    "value": {
                        "type": "enum",
                        "label": "Audio source",
                        "required": True,
                        "values": ["HDMI", "Analog"],
                    }
                },
                "sets": {"audio_source": "{value}"},
            },
            "identify": {
                "label": "Identify the wallplate",
                "sets": {"identified": True},
            },
            "set_audio_muted": {
                "label": "Audio muted",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"audio_muted": "{value}"},
            },
            "set_video_muted": {
                "label": "Video muted",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"video_muted": "{value}"},
            },
            "set_usb_enabled": {
                "label": "USB 2.0 enabled",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"usb_enabled": "{value}"},
            },
            "set_kvm_enabled": {
                "label": "KVM enabled",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"kvm_enabled": "{value}"},
            },
            "set_mwc_tx": {
                "label": "MWC transmit enabled",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"mwc_tx": "{value}"},
                "help": "Secure control acknowledges this command. Independent enable "
                "feedback requires ordinary HControl.",
            },
            "set_image_enabled": {
                "label": "Web snapshot enabled",
                "params": {
                    "value": {"type": "boolean", "label": "Enabled", "required": True}
                },
                "sets": {"image_enabled": "{value}"},
                "help": "Secure control acknowledges this command. Independent "
                "enable feedback requires ordinary HControl.",
            },
            "refresh_network": {
                "label": "Read connected switch and port",
                "query_for": "switch_port",
            },
            "read_selection_mode": {
                "label": "Read input switching policy",
                "query_for": "selection_mode",
            },
            "read_active_input": {
                "label": "Read physical input selected by wallplate",
                "query_for": "active_input",
            },
            "read_live_source": {
                "label": "Read selected live input",
                "query_for": "live_source",
            },
            "read_stream_number": {
                "label": "Read mwc video stream number",
                "query_for": "stream_number",
            },
            "read_audio_source": {
                "label": "Read audio input",
                "query_for": "audio_source",
            },
            "read_audio_muted": {
                "label": "Read audio muted",
                "query_for": "audio_muted",
            },
            "read_video_muted": {
                "label": "Read video muted",
                "query_for": "video_muted",
            },
            "read_usb_enabled": {
                "label": "Read usb 2.0 enabled",
                "query_for": "usb_enabled",
            },
            "read_kvm_enabled": {
                "label": "Read kvm enabled",
                "query_for": "kvm_enabled",
            },
            "read_play_mode": {"label": "Read playback mode", "query_for": "play_mode"},
            "read_playlist": {
                "label": "Read reported local playlist index",
                "query_for": "playlist",
            },
            "read_sample_rate": {
                "label": "Read reported audio sample rate",
                "query_for": "sample_rate",
            },
            "read_identified": {
                "label": "Read identification active",
                "query_for": "identified",
            },
            "read_mwc_tx": {
                "label": "Read mwc transmit enabled",
                "query_for": "mwc_tx",
            },
            "read_image_enabled": {
                "label": "Read web snapshot enabled",
                "query_for": "image_enabled",
            },
        },
        "device_settings": {
            "usb_enabled": {
                "type": "boolean",
                "label": "USB 2.0 enabled",
                "state_key": "usb_enabled",
                "setup": False,
            },
            "kvm_enabled": {
                "type": "boolean",
                "label": "KVM enabled",
                "state_key": "kvm_enabled",
                "setup": False,
            },
            "audio_source": {
                "type": "enum",
                "label": "Audio input",
                "state_key": "audio_source",
                "setup": False,
                "values": ["HDMI", "Analog"],
            },
            "selection_mode": {
                "type": "enum",
                "label": "Input switching policy",
                "state_key": "selection_mode",
                "setup": False,
                "values": ["hdmi", "usbc", "auto"],
            },
            "stream_number": {
                "type": "integer",
                "label": "MWC video stream number",
                "state_key": "stream_number",
                "setup": False,
                "min": 1,
                "max": 32511,
            },
        },
        "actions": [
            {"id": "identify"},
            {"id": "play_local"},
            {"id": "play_live"},
            {"id": "refresh_network"},
        ],
        "quick_actions": [
            "route_input",
            "set_audio_muted",
            "set_video_muted",
            "refresh_status",
        ],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._secure = False
        self._lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._hc_lock = asyncio.Lock()
        self._header = None
        self._reply = None
        self._in_packet = False
        self._fresh = {}
        self._needed = set()
        self._route_fields = {}
        self._missing = set()
        self._watch = self._event_refresh = None
        self._status_dirty = False
        self._writer = self._hc_path = self._hc_reply = None
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        if self.config.get("routing_owner") == "Dante AV-A":
            self.DRIVER_INFO["commands"].pop("set_stream_number")
            self.DRIVER_INFO["device_settings"].pop("stream_number")
        if not self._uses_hcontrol():
            for key in HC_FLAGS:
                self.DRIVER_INFO["commands"]["set_" + key].pop("sets")
                self.DRIVER_INFO["commands"].pop("read_" + key)
        self._register_inputs()

    def _uses_hcontrol(self):
        return not self.config.get("ssl") and self.config.get("hdmi_events", True)

    def _register_inputs(self):
        for n, label in INPUTS.values():
            self.register_child("input", n, {"label": label})

    async def _post_connect(self):
        self._register_inputs()
        report = await self._request("getStatus", {"identity", "firmware"})
        if not ENCODER_MODEL.match(report["identity"].strip()):
            raise ConnectionFaultError(
                "The device did not identify as an N2615 wallplate encoder.",
                code="invalid_config",
            )

    async def _initial_sync(self):
        await self._request("getNetStatus", {"mac"}, header=NETWORK)
        self.set_state("input_feedback", "Direct Control")
        if self._uses_hcontrol():
            self._watch = asyncio.create_task(self._watch_hcontrol())

    async def _close_session(self):
        if self._waiting_for_reply():
            self._reply.set_exception(ConnectionError("Wallplate disconnected."))
        tasks = [
            task for task in (self._watch, self._event_refresh) if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._watch = self._event_refresh = None
        self._missing.clear()
        self._route_fields.clear()
        self._clear_hcontrol("Disconnected")
        for key in ("selection_mode", "play_mode", "active_input", "live_source"):
            self.set_state(key, None)
        for n, _ in INPUTS.values():
            for key in ("selected", "connected", "resolution"):
                self.set_child_state("input", n, key, None)

    def _begin_packet(self, wire):
        # Fields end in CR with no packet terminator. The next header
        # is the first definitive boundary for a short status response.
        self._clear_missing()
        if wire == STATUS:
            self._missing = {
                "selection_mode",
                "play_mode",
                "live_source",
                "active_input",
            }
            self._missing.update((n, key) for n, key, _ in INPUT_FIELDS.values())
        self._in_packet = wire == self._header
        self._fresh = {}
        self._route_fields = {}

    def _receive(self, wire, value):
        if wire in (STATUS, NETWORK):
            self._begin_packet(wire)
        if wire in INPUT_FIELDS:
            n, key, reader = INPUT_FIELDS[wire]
            reading = decode_value(reader, value)
            self.set_child_state("input", n, key, reading)
            self._complete_read({(n, key): reading})
        elif wire in FIELDS:
            key, reader = FIELDS[wire]
            reading = decode_value(reader, value)
            self.set_state(key, reading)
            update = {key: reading}
            if key in ("active_input", "play_mode"):
                update.update(self._update_route(key, reading))
            self._complete_read(update)

    def _select_children(self, reading):
        for source, (n, _) in INPUTS.items():
            self.set_child_state(
                "input",
                n,
                "selected",
                None if reading is None else reading == source,
            )

    def _update_route(self, key, reading):
        self._route_fields[key] = reading
        if key == "active_input":
            self._select_children(reading)
        if self._route_fields.get("play_mode") == "local":
            self.set_state("live_source", None)
            return {"live_source": None}
        if "play_mode" in self._route_fields and "active_input" in self._route_fields:
            mode = self._route_fields["play_mode"]
            live = self._route_fields["active_input"] if mode == "live" else None
            self.set_state("live_source", live)
            return {"live_source": live}
        return {}

    def _clear_missing(self):
        for key in self._missing:
            if isinstance(key, tuple):
                self.set_child_state("input", *key, None)
            else:
                self.set_state(key, None)
        if "active_input" in self._missing:
            self._select_children(None)
        self._missing.clear()

    def _complete_read(self, update):
        self._missing.difference_update(update)
        if self._in_packet and self._waiting_for_reply():
            self._fresh.update(update)
            if self._needed.issubset(self._fresh):
                self._reply.set_result(dict(self._fresh))

    async def poll(self):
        await self._request("getStatus", {"selection_mode", "live_source"})

    def _clear_hcontrol(self, detail):
        self.set_state("input_feedback", detail)
        for key in HC_FLAGS:
            self.set_state(key, None)
        self.set_state("snapshot_url", "")

    async def _hc_message(self, writer, verb, packet, subscribed):
        if not isinstance(packet, dict):
            return
        path = packet.get("path")
        if verb == "@get":
            self._hc_value(path, packet.get("value"))
        elif isinstance(path, str) and path in EVENTS:
            if verb == "@subscribeevent":
                subscribed.add(path)
                if subscribed != EVENTS:
                    return
                self.set_state("input_feedback", "Live HDMI and USB-C events")
            self._queue_status()

    def _hc_value(self, path, value):
        if self._hc_reply is None:
            return
        if path != self._hc_path or not isinstance(value, bool):
            return
        key = next(k for k, p in HC_FLAGS.items() if p == path)
        self.set_state(key, value)
        if key == "image_enabled":
            self.set_state("snapshot_url", self._snapshot_url())
        if not self._hc_reply.done():
            self._hc_reply.set_result(value)

    async def send_command(self, command, params=None):
        spec = self.DRIVER_INFO["commands"].get(command)
        if spec is None:
            raise CommandRefusedError("Unavailable for this wallplate configuration.")
        if "query_for" in spec:
            return await self._query(spec["query_for"])
        async with self._operation_lock:
            return await self._control(command, params or {}, spec)

    async def _query(self, key):
        if key == "switch_port":
            return await self._request("getNetStatus", {key}, header=NETWORK)
        if key in HC_FLAGS:
            return await self._read_flag(key)
        return await self._request("getStatus", {key})

    async def _control(self, command, params, spec):
        if command == "route_input":
            return await self._route(params.get("source"))
        key = command.removeprefix("set_")
        value = params.get("value")
        if key in HC_FLAGS:
            return await self._set_flag(key, flag(value), spec)
        wire, expected = operation(command, key, value)
        return await self._confirm(wire, expected)

    async def _route(self, source):
        source = policy(source)
        # While a local playlist is displayed, VSTS may lag the configured policy.
        first = await self._request("vidsrc:" + source, {"selection_mode", "play_mode"})
        if first["selection_mode"] != source:
            raise CommandRefusedError(
                "The wallplate declined the requested input policy."
            )
        expected = {"selection_mode": source, "play_mode": "live"}
        if source != "auto":
            expected["live_source"] = source
        command = "getStatus" if first["play_mode"] == "live" else "live"
        await self._confirm(command, expected)

    async def _pre_connect(self):
        port = int(self.config.get("port", 50002))
        self._secure = secure = bool(self.config.get("ssl"))
        wrong_ports = (50001, 50002) if secure else (50101, 50102)
        if port in wrong_ports:
            raise ConnectionFaultError(
                "Match the control port and TLS setting: ordinary 50002 or secure 50102.",
                code="invalid_config",
            )
        if secure:
            self._check_password()

    def _check_password(self):
        password = self.config.get("password") or ""
        if not password or re.search(r"[\r\n\x00]", password):
            raise ConnectionFaultError(
                "Enter the encoder command password without line breaks.",
                code="invalid_config",
            )

    async def _send(self, wire):
        if self.transport is None:
            raise ConnectionError("Encoder is disconnected.")
        command = (wire + "\r").encode("ascii")
        if not self._secure:
            await self.transport.send(command)
            return
        credential = (self.config["password"] + "\r").encode()
        with get_traffic_recorder().hold(self.device_id, tx=True):
            await self.transport.send(credential + command)
        self.record_traffic(
            "tx",
            command,
            meta={
                "protocol": "Secure Direct Control",
                "password_prefix_withheld": True,
            },
        )

    async def _request(self, wire, keys, header=STATUS):
        async with self._lock:
            self._header = header
            self._in_packet = False
            self._needed = set(keys)
            self._fresh = {}
            self._reply = asyncio.get_running_loop().create_future()
            try:
                await self._send(wire)
                return await asyncio.wait_for(self._reply, 4)
            except TimeoutError:
                # Direct Control has no transaction IDs. A late reply must never
                # satisfy another write on the same session.
                if self._in_packet:
                    self._clear_missing()
                    raise CommandRefusedError(
                        "The encoder answered but omitted a requested status field."
                    ) from None
                if self.connected:
                    self._force_disconnect(
                        message="No complete reply from the encoder."
                    )
                raise ConnectionError(
                    "Encoder did not return the requested status fields."
                ) from None
            finally:
                self._reply.cancel()
                self._reply = None

    def _waiting_for_reply(self):
        return self._reply is not None and not self._reply.done()

    async def _liveness_probe(self):
        await self._request("getStatus", {"identity"})

    async def _confirm(self, wire, expected):
        fresh = await self._request(wire, set(expected))
        if any(fresh[key] != value for key, value in expected.items()):
            raise CommandRefusedError(
                "The encoder reported different settings. Check its routing mode, permissions and signal format."
            )

    async def on_data_received(self, data):
        for line in data.decode("utf-8", "replace").splitlines():
            wire, value = split_field(line.strip())
            self._receive(wire, value)

    async def refresh_children(self):
        self._register_inputs()
        keys = {(n, key) for n, key, _ in INPUT_FIELDS.values()}
        await self._request("getStatus", keys | {"active_input"})

    def _snapshot_url(self):
        if not self.get_state("image_enabled") or self.udp_redirect is not None:
            return ""
        host = self.config["host"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        scheme = self.config.get("web_scheme", "https")
        return f"{scheme}://{host}/snapshot.jpg"

    async def _watch_hcontrol(self):
        while True:
            try:
                await self._hcontrol_session()
            except (OSError, ValueError, TimeoutError):
                self._clear_hcontrol("Direct Control; HControl unavailable")
            await asyncio.sleep(15)

    async def _hcontrol_session(self):
        host, port = self.udp_redirect or (self.config["host"], 4197)
        ip = self.local_address(host)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, local_addr=(ip, 0) if ip else None), 3
        )
        self._writer = writer
        monitor = asyncio.create_task(self._read_hcontrol(reader, writer))
        try:
            for path in EVENTS:
                await self._hc_send(writer, "subscribeevent", path)
            await self._monitor_flags(monitor)
        finally:
            self._writer = None
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            writer.close()
            await writer.wait_closed()

    async def _monitor_flags(self, monitor):
        while not monitor.done():
            for key in HC_FLAGS:
                await self._read_flag(key)
            await asyncio.wait({monitor}, timeout=30)
        await monitor

    async def _hc_send(self, writer, verb, path):
        message = verb + " " + json.dumps({"path": path}) + "\n"
        writer.write(message.encode())
        await writer.drain()
        self.record_traffic("tx", message, meta={"protocol": "HControl"})

    async def _read_flag(self, key):
        async with self._hc_lock:
            writer = self._writer
            if writer is None:
                raise ConnectionError("HControl feedback is unavailable.")
            self._hc_path = HC_FLAGS[key]
            future = asyncio.get_running_loop().create_future()
            self._hc_reply = future
            try:
                await self._hc_send(writer, "get", self._hc_path)
                return await asyncio.wait_for(future, 3)
            except TimeoutError:
                writer.close()
                raise
            finally:
                self._hc_reply = None
                future.cancel()

    async def _read_hcontrol(self, reader, writer):
        subscribed = set()
        while line := await reader.readline():
            self.record_traffic("rx", line, meta={"protocol": "HControl"})
            verb, _, body = line.decode("utf8", "replace").strip().partition(" ")
            if verb in ("@get", "event", "@subscribeevent"):
                await self._hc_message(writer, verb, json.loads(body), subscribed)
        raise ConnectionError("HControl connection closed.")

    def _queue_status(self):
        # A cable event can switch the active input in Auto. Direct Control
        # reports the actual selection, including changes made by another user.
        self._status_dirty = True
        if self._event_refresh is None or self._event_refresh.done():
            self._event_refresh = asyncio.create_task(self._refresh_on_event())

    async def _refresh_on_event(self):
        try:
            while self._status_dirty:
                self._status_dirty = False
                await self.poll()
        except (OSError, CommandRefusedError):
            return

    async def _set_flag(self, key, value, spec):
        verify = "sets" in spec
        if verify and self._writer is None:
            raise CommandRefusedError(
                "HControl readback is unavailable. Check the encoder feedback connection."
            )
        await self._request(TOGGLES[key][value], {"identity"})
        if verify and await self._read_flag(key) != value:
            raise CommandRefusedError(
                "The encoder reported a different transmit or preview setting."
            )

    async def set_device_setting(self, key, value):
        if key not in self.DRIVER_INFO["device_settings"]:
            raise CommandRefusedError("Unknown encoder setting.")
        await self.send_command("set_" + key, {"value": value})
