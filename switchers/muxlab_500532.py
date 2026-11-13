"""MuxLab 500532 presentation switcher, 94-001072-A, pages 25–41.

Python associates untagged replies (notably eight arbitrary preset names and
USB mode/host) with the one request awaiting them. A truncated status dump or
late reply expires the connection before an unrelated request can consume it.
"""

from __future__ import annotations

import asyncio
import re
from time import monotonic

from openavc.drivers.base import BaseDriver, CommandRefusedError

SOURCES = ("USB-C 1", "USB-C 2", "HDMI 1", "HDMI 2")
HOSTS = ("USB-C 1", "USB-C 2", "USB host 3", "USB host 4")
MODES = ("Follow video", "Manual switch", "Auto switch")
LAYOUTS = (
    "Auto",
    "Single",
    "Dual-LR",
    "Dual-TB",
    "PIP-LT",
    "PIP-LB",
    "PIP-RT",
    "PIP-RB",
    "Triple-L",
    "Triple-R",
    "Triple-T",
    "Triple-B",
    "Quad-L",
    "Quad-R",
    "Quad-T",
    "Quad-B",
    "Quad-S",
    "Copy other output",
    "Custom 1",
    "Custom 2",
    "Custom 3",
    "Custom 4",
    "Custom 5",
)
CONNECTORS = ("HDMI out 1", "HDMI out 2", "HDBT out 2")
AUDIO = ("HDMI out 1", "HDMI/HDBT out 2", "Line out", "Dante out")
RX_INPUTS = ("HDBT in", "HDMI in")
OUTPUT_NAMES = ("HDMI 1", "HDMI 2 / HDBaseT")
SLOTS = {
    "input": SOURCES,
    "output": OUTPUT_NAMES,
    "connector": CONNECTORS,
    "audio_output": AUDIO,
    "usb_input": HOSTS,
    "preset": tuple(f"Preset {i}" for i in range(1, 9)),
}
TIMEOUT = 4.0
DIVIDER = re.compile(r"^={4,}$")
SOURCE = re.compile(r"USB-C\s*[12]|HDMI\s*[12]", re.I)
MODEL = re.compile(r"500532")
ON_OFF = {"on": True, "off": False}


def choice(value, choices):
    normalized = str(value).strip().casefold()
    for candidate in choices:
        if normalized == candidate.casefold():
            return candidate
    raise ValueError(f"Unsupported selection: {value!r}")


def ordinal(value, choices):
    return choices.index(choice(value, choices)) + 1


def slot(params, key):
    value = str(params.get(key, ""))
    if value not in {str(i) for i in range(1, len(SLOTS[key]) + 1)}:
        raise ValueError(f"Choose a valid {key.replace('_', ' ')}.")
    return int(value)


def level(value):
    if not re.fullmatch(r"[0-9]{1,3}", str(value)) or int(value) > 100:
        raise ValueError("Volume must be a whole number from 0 to 100.")
    return int(value)


def preset_name(value):
    if not isinstance(value, str):
        raise ValueError("Enter a preset name.")
    if not re.fullmatch(r"[ -~]{1,16}", value) or value != value.strip():
        raise ValueError(
            "Enter 1–16 printable ASCII characters without leading or trailing spaces."
        )
    return value


def is_single_view(mode):
    return mode in ("Auto", "Single", "Copy other output")


def source_list(reply):
    parts = SOURCE.findall(reply)
    rest = SOURCE.sub("", reply)
    if not re.fullmatch(r"[\s,/]*", rest) or len(parts) > 4:
        return []
    return [part.upper().replace(" ", "")[:-1] + " " + part[-1] for part in parts]


def layout_value(text):
    text = text.strip()
    if text.lower().startswith("copy"):
        return "Copy other output"
    text = re.sub(r"^custom\s+layout\s+", "Custom ", text, flags=re.I)
    return choice(text.split(" (")[0], LAYOUTS)


def reported_value(raw, choices):
    try:
        return choice(raw, choices)
    except ValueError:
        return None


def connector_row(line):
    match = re.fullmatch(
        r"(USB-C\s*[12]|HDMI\s*[12]|HDMI\s*OUT\s*[12]|HDBT\s*OUT\s*2)\s+(Connected|Disconnected|Not\s+Connected)\s*(.*)",
        line,
        re.I,
    )
    if not match:
        return None
    key = match[1].replace(" ", "").upper()
    present = match[2].lower() == "connected"
    values = match[3].split()
    resolution = (
        values[0]
        if present and values and re.search(r"\d+x\d+", values[0], re.I)
        else None
    )
    hdcp = values[3] if present and len(values) >= 4 else None
    return key, {"cable": present, "resolution": resolution, "hdcp": hdcp}, values


class Reply:
    """Accept one matching line or a framed status report on the current stream."""

    def __init__(self, command, pattern, table):
        self.command = command
        self.pattern = re.compile(pattern, re.I)
        self.table = table
        self.lines = []
        self.started = False
        self.result = asyncio.get_running_loop().create_future()

    def feed(self, line):
        if self.result.done():
            return
        # Preset names are arbitrary user text. "Invalid command" is a legal
        # name on a valid slot; the untagged value belongs to that query.
        if self.command.startswith("get preset ") and self.pattern.fullmatch(line):
            self.result.set_result(line)
        elif line.casefold().startswith(("error,", "error:", "invalid command")):
            self.result.set_exception(CommandRefusedError(line))
        elif self.table:
            self._row(line)
        elif self.pattern.fullmatch(line):
            self.result.set_result(line)

    def _row(self, line):
        if DIVIDER.fullmatch(line):
            if self.started and self.lines:
                self.result.set_result(self.lines)
            self.started = True
            return
        if not self.started:
            return
        if len(self.lines) >= 200:
            self.result.set_exception(
                ConnectionError("The switcher sent an unterminated status report.")
            )
            return
        self.lines.append(line)


class MuxLab500532Driver(BaseDriver):
    DRIVER_INFO = {
        "id": "muxlab_500532",
        "name": "MuxLab 500532 USB-C/HDMI Presentation Switcher with Dante",
        "manufacturer": "MuxLab",
        "category": "switcher",
        "version": "1.0.0",
        "author": "OpenAVC",
        "min_platform_version": "0.37.1",
        "description": "Route the two presentation outputs and shared USB peripherals, select multiview "
        "layouts, recall the eight device-named scenes, and monitor connectors and audio "
        "including Dante.",
        "source_url": "https://muxlab.com/wp-content/uploads/2025/08/SE-001072-A-500532-USB-CHDMI-4x2-Presentation-Switcher-with-Dante-User-Manual_v1.0.pdf",
        "verified": False,
        "simulated": True,
        "tags": [
            "presentation-switcher",
            "usb-c",
            "byod",
            "multiview",
            "dante",
            "hdbaset",
        ],
        "transport": "tcp",
        "transports": ["tcp", "serial"],
        "ports": [8000],
        "protocols": ["muxlab-500532-ascii"],
        "compatible_models": [
            {
                "manufacturer": "MuxLab",
                "models": ["500532"],
                "confidence": "untested",
                "notes": "Exact transmitter and included HDBaseT receiver kit, manual "
                "94-001072-A. Simulator tested.",
            }
        ],
        "web_ui": "http://{host}",
        "discovery": {
            "manufacturer_alias": ["muxlab"],
            "tcp_probe": {
                "port": 8000,
                "send_ascii": "get model\r\n",
                "expect_regex": "(?m)^500532\\r?$",
                "extract_manufacturer": "MuxLab",
                "extract": {"model": {"regex": "(?m)^(500532)\\r?$"}},
                "timeout_ms": 2000,
            },
        },
        "help": {
            "overview": "Two video destinations share four presentation inputs. HDMI 2 and HDBaseT carry "
            "the second layout, with separate connector readings. Select Video Source puts "
            "that destination in Single layout. Set Multiview Sources assigns four windows "
            "without choosing a layout. USB can follow the video output chosen in the web "
            "interface, switch automatically, or run independently in manual mode. Select USB "
            "Host explicitly enters manual mode.",
            "setup": "Enter the switcher address for ASCII control on TCP 8000 or use the transmitter "
            "RS-232 CTL port at 115200 baud, 8N1. Preset choices use the eight names read from "
            "the device. Save Preset overwrites the chosen scene. After recalling one, the driver "
            "reads the resulting routes, audio and layout. The equipment does not identify an "
            "active preset.",
            "connection": "The driver identifies model 500532, reads the current routes and polls for "
            "front-panel and automatic changes. Unconfirmed readings clear on disconnection. "
            "In standby only power is polled. The web interface provides deeper audio "
            "processing, custom layout design and display programming.",
        },
        "default_config": {
            "host": "",
            "port": 8000,
            "baudrate": 115200,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "label": "Switcher Address", "required": True},
            "port": {
                "type": "integer",
                "label": "ASCII TCP Port",
                "default": 8000,
                "min": 1,
                "max": 65535,
            },
            "baudrate": {
                "type": "integer",
                "label": "RS-232 Baud Rate",
                "default": 115200,
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status Refresh Interval (seconds)",
                "default": 5,
                "min": 2,
                "max": 300,
                "advanced": True,
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Reported Model"},
            "firmware": {"type": "string", "label": "Firmware Versions"},
            "power": {"type": "boolean", "label": "Power On", "control": True},
            "usb_mode": {
                "type": "enum",
                "label": "USB Switching Mode",
                "values": ["Follow video", "Manual switch", "Auto switch"],
            },
            "usb_host": {
                "type": "enum",
                "label": "Active USB Host",
                "values": ["USB-C 1", "USB-C 2", "USB host 3", "USB host 4"],
                "control": True,
                "cloud_priority": "high",
            },
            "master_volume": {
                "type": "integer",
                "label": "Level",
                "min": 0,
                "max": 100,
                "control": True,
            },
            "master_mute": {"type": "boolean", "label": "Master Mute", "control": True},
            "temperature": {
                "type": "integer",
                "label": "Internal Temperature",
                "unit": "°C",
            },
            "receiver_input": {
                "type": "enum",
                "label": "Receiver Input",
                "values": ["HDBT in", "HDMI in"],
            },
            "preset_names_ready": {"type": "boolean", "label": "Preset Names Loaded"},
        },
        "child_entity_types": {
            "input": {
                "label": "Video Input",
                "label_plural": "Video Inputs",
                "instances": {"count": 4, "label": "Video Input {id}"},
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "cable": {"type": "boolean", "label": "Cable Connected"},
                    "resolution": {"type": "string", "label": "Signal Resolution"},
                    "hdcp": {"type": "string", "label": "Negotiated HDCP"},
                },
                "summary_fields": ["cable", "resolution"],
            },
            "output": {
                "label": "Video Output",
                "label_plural": "Video Outputs",
                "instances": {"count": 2, "label": "Video Output {id}"},
                "id_format": {"type": "integer", "min": 1, "max": 2},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "source": {
                        "type": "enum",
                        "label": "Single Video Source",
                        "values": ["USB-C 1", "USB-C 2", "HDMI 1", "HDMI 2"],
                        "control": True,
                    },
                    "layout": {
                        "type": "enum",
                        "label": "Display Layout",
                        "values": [
                            "Auto",
                            "Single",
                            "Dual-LR",
                            "Dual-TB",
                            "PIP-LT",
                            "PIP-LB",
                            "PIP-RT",
                            "PIP-RB",
                            "Triple-L",
                            "Triple-R",
                            "Triple-T",
                            "Triple-B",
                            "Quad-L",
                            "Quad-R",
                            "Quad-T",
                            "Quad-B",
                            "Quad-S",
                            "Copy other output",
                            "Custom 1",
                            "Custom 2",
                            "Custom 3",
                            "Custom 4",
                            "Custom 5",
                        ],
                        "control": True,
                    },
                    "window_1": {"type": "string", "label": "Window 1 Source"},
                    "window_2": {"type": "string", "label": "Window 2 Source"},
                    "window_3": {"type": "string", "label": "Window 3 Source"},
                    "window_4": {"type": "string", "label": "Window 4 Source"},
                },
                "summary_fields": ["source", "layout"],
            },
            "connector": {
                "label": "Display Connector",
                "label_plural": "Display Connectors",
                "instances": {"count": 3, "label": "Display Connector {id}"},
                "id_format": {"type": "integer", "min": 1, "max": 3},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "cable": {"type": "boolean", "label": "Cable Connected"},
                    "resolution": {"type": "string", "label": "Signal Resolution"},
                    "hdcp": {"type": "string", "label": "Negotiated HDCP"},
                    "freeze": {"type": "boolean", "label": "Frozen", "control": True},
                    "av_mute": {
                        "type": "boolean",
                        "label": "AV Muted",
                        "control": True,
                    },
                    "enabled": {
                        "type": "boolean",
                        "label": "Output Enabled",
                        "control": True,
                    },
                },
                "summary_fields": ["cable", "resolution"],
            },
            "audio_output": {
                "label": "Audio Output",
                "label_plural": "Audio Outputs",
                "instances": {"count": 4, "label": "Audio Output {id}"},
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "volume": {
                        "type": "integer",
                        "label": "Level",
                        "min": 0,
                        "max": 100,
                    },
                    "mute": {"type": "boolean", "label": "Muted", "control": True},
                },
                "summary_fields": ["volume", "mute"],
            },
            "usb_input": {
                "label": "USB Host",
                "label_plural": "USB Hosts",
                "instances": {"count": 4, "label": "USB Host {id}"},
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "host_5v": {"type": "boolean", "label": "Host 5 V Present"},
                },
                "summary_fields": ["host_5v"],
            },
            "preset": {
                "label": "Preset",
                "label_plural": "Presets",
                "instances": {"count": 8, "label": "Preset {id}"},
                "id_format": {"type": "integer", "min": 1, "max": 8},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "device_name": {"type": "string", "label": "Stored Preset Name"},
                },
                "summary_fields": ["device_name"],
            },
        },
        "commands": {
            "route_video": {
                "label": "Select Video Source",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    },
                    "source": {
                        "type": "enum",
                        "label": "Video Source",
                        "values": ["USB-C 1", "USB-C 2", "HDMI 1", "HDMI 2"],
                        "required": True,
                    },
                },
                "sets": {"source": "{source}"},
                "help": "Selects Single layout on this output and routes the chosen input. "
                "USB follows only when the switcher is in Follow video mode.",
            },
            "route_usb": {
                "label": "Select USB Host",
                "params": {
                    "source": {
                        "type": "enum",
                        "label": "USB Host",
                        "values": ["USB-C 1", "USB-C 2", "USB host 3", "USB host 4"],
                        "required": True,
                    }
                },
                "sets": {"usb_host": "{source}"},
                "help": "Enters Manual switch mode and routes shared USB peripherals without "
                "changing either video output.",
            },
            "set_layout": {
                "label": "Select Display Layout",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    },
                    "layout": {
                        "type": "enum",
                        "label": "Layout",
                        "values": [
                            "Auto",
                            "Single",
                            "Dual-LR",
                            "Dual-TB",
                            "PIP-LT",
                            "PIP-LB",
                            "PIP-RT",
                            "PIP-RB",
                            "Triple-L",
                            "Triple-R",
                            "Triple-T",
                            "Triple-B",
                            "Quad-L",
                            "Quad-R",
                            "Quad-T",
                            "Quad-B",
                            "Quad-S",
                            "Copy other output",
                            "Custom 1",
                            "Custom 2",
                            "Custom 3",
                            "Custom 4",
                            "Custom 5",
                        ],
                        "required": True,
                    },
                },
                "sets": {"layout": "{layout}"},
            },
            "set_windows": {
                "label": "Set Multiview Sources",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    },
                    "window_1": {
                        "type": "enum",
                        "label": "Window 1",
                        "values": ["USB-C 1", "USB-C 2", "HDMI 1", "HDMI 2"],
                        "required": True,
                    },
                    "window_2": {
                        "type": "enum",
                        "label": "Window 2",
                        "values": ["USB-C 1", "USB-C 2", "HDMI 1", "HDMI 2"],
                        "required": True,
                    },
                    "window_3": {
                        "type": "enum",
                        "label": "Window 3",
                        "values": ["USB-C 1", "USB-C 2", "HDMI 1", "HDMI 2"],
                        "required": True,
                    },
                    "window_4": {
                        "type": "enum",
                        "label": "Window 4",
                        "values": ["USB-C 1", "USB-C 2", "HDMI 1", "HDMI 2"],
                        "required": True,
                    },
                },
                "help": "Assign all four source windows for this output in their display "
                "order. Select a multiview layout first.",
            },
            "set_usb_mode": {
                "label": "Select USB Switching Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "label": "Mode",
                        "values": ["Follow video", "Manual switch", "Auto switch"],
                        "required": True,
                    }
                },
                "sets": {"usb_mode": "{mode}"},
            },
            "set_receiver_input": {
                "label": "Select Receiver Input",
                "params": {
                    "source": {
                        "type": "enum",
                        "label": "Receiver Input",
                        "values": ["HDBT in", "HDMI in"],
                        "required": True,
                    }
                },
                "sets": {"receiver_input": "{source}"},
            },
            "recall_preset": {
                "label": "Recall Named Preset",
                "params": {
                    "preset": {
                        "type": "child_id",
                        "label": "Preset",
                        "child_type": "preset",
                        "required": True,
                    }
                },
            },
            "save_preset": {
                "label": "Save Current Settings to Preset",
                "params": {
                    "preset": {
                        "type": "child_id",
                        "label": "Preset",
                        "child_type": "preset",
                        "required": True,
                    }
                },
                "confirm": "Overwrite the settings stored in this preset?",
            },
            "rename_preset": {
                "label": "Rename Preset",
                "params": {
                    "preset": {
                        "type": "child_id",
                        "label": "Preset",
                        "child_type": "preset",
                        "required": True,
                    },
                    "name": {"type": "string", "label": "New Name", "required": True},
                },
                "sets": {"device_name": "{name}"},
                "help": "Use 1–16 printable ASCII characters.",
            },
            "refresh": {"label": "Refresh from Switcher", "params": {}},
            "read_power": {"label": "Read Power", "params": {}, "query_for": "power"},
            "read_usb_host": {
                "label": "Read USB Host",
                "params": {},
                "query_for": "usb_host",
            },
            "read_usb_mode": {
                "label": "Read USB Mode",
                "params": {},
                "query_for": "usb_mode",
            },
            "read_master_volume": {
                "label": "Read Master Volume",
                "params": {},
                "query_for": "master_volume",
            },
            "read_master_mute": {
                "label": "Read Master Mute",
                "params": {},
                "query_for": "master_mute",
            },
            "read_receiver_input": {
                "label": "Read Receiver Input",
                "params": {},
                "query_for": "receiver_input",
            },
            "read_video": {
                "label": "Read Video Route",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    }
                },
                "query_for": "source",
            },
            "read_layout": {
                "label": "Read Layout",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "child_type": "output",
                        "required": True,
                    }
                },
                "query_for": "layout",
            },
            "read_usb_power": {
                "label": "Read USB Host 5 V",
                "params": {
                    "usb_input": {
                        "type": "child_id",
                        "label": "Usb Input",
                        "child_type": "usb_input",
                        "required": True,
                    }
                },
                "query_for": "host_5v",
            },
            "read_preset_name": {
                "label": "Read Preset Name",
                "params": {
                    "preset": {
                        "type": "child_id",
                        "label": "Preset",
                        "child_type": "preset",
                        "required": True,
                    }
                },
                "query_for": "device_name",
            },
            "read_audio_volume": {
                "label": "Read Output Volume",
                "params": {
                    "audio_output": {
                        "type": "child_id",
                        "label": "Audio Output",
                        "child_type": "audio_output",
                        "required": True,
                    }
                },
                "query_for": "volume",
            },
            "read_audio_mute": {
                "label": "Read Output Mute",
                "params": {
                    "audio_output": {
                        "type": "child_id",
                        "label": "Audio Output",
                        "child_type": "audio_output",
                        "required": True,
                    }
                },
                "query_for": "mute",
            },
            "read_input_signal": {
                "label": "Read Input Resolution",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "query_for": "resolution",
            },
            "read_connector": {
                "label": "Read Display Connection",
                "params": {
                    "connector": {
                        "type": "child_id",
                        "label": "Connector",
                        "child_type": "connector",
                        "required": True,
                    }
                },
                "query_for": "cable",
            },
            "power_on": {"label": "Power On", "params": {}, "sets": {"power": True}},
            "power_off": {"label": "Power Off", "params": {}, "sets": {"power": False}},
            "master_mute_on": {
                "label": "Master Mute On",
                "params": {},
                "sets": {"master_mute": True},
            },
            "master_mute_off": {
                "label": "Master Mute Off",
                "params": {},
                "sets": {"master_mute": False},
            },
            "audio_mute_on": {
                "label": "Output Audio Mute On",
                "params": {
                    "audio_output": {
                        "type": "child_id",
                        "label": "Audio Output",
                        "child_type": "audio_output",
                        "required": True,
                    }
                },
                "sets": {"mute": True},
            },
            "audio_mute_off": {
                "label": "Output Audio Mute Off",
                "params": {
                    "audio_output": {
                        "type": "child_id",
                        "label": "Audio Output",
                        "child_type": "audio_output",
                        "required": True,
                    }
                },
                "sets": {"mute": False},
            },
            "freeze_on": {
                "label": "Output Freeze On",
                "params": {
                    "connector": {
                        "type": "child_id",
                        "label": "Connector",
                        "child_type": "connector",
                        "required": True,
                    }
                },
                "sets": {"freeze": True},
            },
            "freeze_off": {
                "label": "Output Freeze Off",
                "params": {
                    "connector": {
                        "type": "child_id",
                        "label": "Connector",
                        "child_type": "connector",
                        "required": True,
                    }
                },
                "sets": {"freeze": False},
            },
            "av_mute_on": {
                "label": "Output AV Mute On",
                "params": {
                    "connector": {
                        "type": "child_id",
                        "label": "Connector",
                        "child_type": "connector",
                        "required": True,
                    }
                },
                "sets": {"av_mute": True},
            },
            "av_mute_off": {
                "label": "Output AV Mute Off",
                "params": {
                    "connector": {
                        "type": "child_id",
                        "label": "Connector",
                        "child_type": "connector",
                        "required": True,
                    }
                },
                "sets": {"av_mute": False},
            },
            "enable_on": {
                "label": "Output Enable On",
                "params": {
                    "connector": {
                        "type": "child_id",
                        "label": "Connector",
                        "child_type": "connector",
                        "required": True,
                    }
                },
                "sets": {"enabled": True},
            },
            "enable_off": {
                "label": "Output Enable Off",
                "params": {
                    "connector": {
                        "type": "child_id",
                        "label": "Connector",
                        "child_type": "connector",
                        "required": True,
                    }
                },
                "sets": {"enabled": False},
            },
            "set_audio_volume": {
                "label": "Set Output Volume",
                "params": {
                    "audio_output": {
                        "type": "child_id",
                        "label": "Audio Output",
                        "child_type": "audio_output",
                        "required": True,
                    },
                    "level": {
                        "type": "integer",
                        "label": "Volume",
                        "min": 0,
                        "max": 100,
                        "required": True,
                    },
                },
                "sets": {"volume": "{level}"},
            },
            "set_master_volume": {
                "label": "Set Master Volume",
                "params": {
                    "level": {
                        "type": "integer",
                        "label": "Volume",
                        "min": 0,
                        "max": 100,
                        "required": True,
                    }
                },
                "sets": {"master_volume": "{level}"},
            },
        },
        "routing": {
            "planes": [
                {
                    "label": "Video",
                    "destination_child_type": "output",
                    "route_property": "source",
                    "command": "route_video",
                    "destination_param": "output",
                    "source_param": "source",
                },
                {
                    "label": "USB Host",
                    "route_property": "usb_host",
                    "command": "route_usb",
                    "source_param": "source",
                },
            ]
        },
        "device_settings": {
            "usb_mode": {
                "type": "enum",
                "label": "USB Switching Mode",
                "values": ["Follow video", "Manual switch", "Auto switch"],
                "state_key": "usb_mode",
            }
        },
        "actions": [
            {"id": "route_video", "icon": "monitor"},
            {"id": "route_usb", "icon": "usb"},
            {"id": "recall_preset", "icon": "list"},
            {"id": "set_layout", "icon": "layout-grid"},
            {"id": "set_master_volume", "icon": "volume-2"},
            {"id": "master_mute_on", "icon": "volume-x"},
            {"id": "master_mute_off", "icon": "volume-2"},
            {"id": "refresh", "icon": "refresh-cw"},
        ],
        "quick_actions": ["route_video", "route_usb", "recall_preset", "set_layout"],
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._io = asyncio.Lock()
        self._pending = None
        self._usable = False
        self._next_names = 0.0
        self._awake = None

    def _transport_kwargs(self, transport_type, kwargs):
        kwargs.update(delimiter=b"\n", frame_parser=None)
        return kwargs

    def _ports(self):
        for kind, names in SLOTS.items():
            for idx, name in enumerate(names, 1):
                self.register_child(kind, idx, {"name": name})

    def _expire(self):
        for kind, definition in self.DRIVER_INFO["child_entity_types"].items():
            empty = dict.fromkeys(definition["state_variables"].keys() - {"name"})
            for idx in self.list_children(kind):
                updates = dict(empty)
                if kind == "preset":
                    updates["name"] = f"Preset {idx}"
                self.set_child_state_batch(kind, idx, updates)
        self._next_names = 0
        self.set_states(
            {
                **dict.fromkeys(
                    self.DRIVER_INFO["state_variables"].keys() - {"model", "power"}
                ),
                "preset_names_ready": False,
            }
        )

    async def _close_session(self):
        self._usable = False
        self._awake = None
        self._expire()
        self.set_states({"power": None, "model": None})
        current, self._pending = self._pending, None
        if current and not current.result.done():
            current.result.set_result(None)
        await super()._close_session()

    def on_data_received(self, data):
        if self._usable and self._pending:
            self._pending.feed(data.decode("ascii", errors="replace").strip("\r\n "))

    async def _ask(self, command, pattern=r".+", *, table=False):
        if not self._usable or not self._link_alive():
            raise ConnectionError("The switcher is disconnected.")
        request = Reply(command, pattern, table)
        self._pending = request
        try:
            answer = await self._exchange(request)
        finally:
            if self._pending is request:
                self._pending = None
        if answer is None:
            raise ConnectionError("Switcher disconnected during readback.")
        return answer

    async def _exchange(self, request):
        try:
            await self.transport.send((request.command + "\r\n").encode("ascii"))
            return await asyncio.wait_for(asyncio.shield(request.result), TIMEOUT)
        except (asyncio.CancelledError, ConnectionError, TimeoutError):
            # An untagged line arriving late must never satisfy a later query.
            if self._pending is request:
                self._usable = False
                self._force_disconnect(
                    message="The control exchange was interrupted. Reopening the switcher connection."
                )
            raise

    async def _post_connect(self):
        self._usable = True
        async with self._io:
            model = await self._ask("get model", r"\S.*")
            if model != "500532":
                raise ConnectionError(
                    "The device did not identify itself as a MuxLab 500532."
                )
            self.set_state("model", model)
            self._ports()

    async def _initial_sync(self):
        await self.poll()

    async def _liveness_probe(self):
        async with self._io:
            await self._ask("get model", MODEL.pattern)

    async def _power(self):
        report = await self._ask("get power", r"power (on|off)")
        awake = report.lower().endswith(" on")
        if self._awake != awake and not awake:
            self._expire()
        self._awake = awake
        self.set_state("power", awake)
        return awake

    async def poll(self):
        async with self._io:
            await self._refresh()

    async def _refresh(self, force=False):
        if not await self._power():
            return
        await self._signals()
        for idx in (1, 2):
            await self._video(idx)
        await self._usb()
        for idx in range(1, 5):
            await self._usb_power(idx)
        await self._master()
        for idx in range(1, 5):
            await self._audio(idx)
        await self._receiver()
        if force or monotonic() >= self._next_names:
            await self._names()

    async def _signals(self):
        lines = await self._ask("status", table=True)
        self._apply_status(lines)

    def _apply_status(self, lines):
        # Omitted rows in a fresh snapshot must not retain the previous reading.
        rows = {}
        firmware = None
        for line in lines:
            parsed = connector_row(line)
            if parsed:
                key, signal, values = parsed
                rows[key] = (signal, values)
            elif line.upper().startswith("MCU "):
                firmware = line
        self.set_state("firmware", firmware)
        self._update_connectors("input", rows)
        self._update_connectors("connector", rows)
        self._temperature(lines)

    def _update_connectors(self, kind, rows):
        for idx, name in enumerate(SLOTS[kind], 1):
            signal, values = rows.get(name.replace(" ", "").upper(), ({}, []))
            updates = {key: signal.get(key) for key in ("cable", "resolution", "hdcp")}
            if kind == "connector":
                updates.update(self._output_flags(values))
            self.set_child_state_batch(kind, idx, updates)

    @staticmethod
    def _output_flags(values):
        flags = values[4:7] if len(values) >= 7 else []
        return {
            key: ON_OFF.get(v.lower())
            for key, v in zip(("freeze", "av_mute", "enabled"), [*flags, "", "", ""])
        }

    def _temperature(self, lines):
        for header, row in zip(lines, lines[1:]):
            if "temp(c)" in header.lower():
                # USB network access mode may contain spaces. Temp precedes uptime.
                match = re.search(r"\s(-?\d+)\s+\d+:\d+:\d+:\d+\s*$", row)
                self.set_state("temperature", int(match[1]) if match else None)
                return
        self.set_state("temperature", None)

    async def _video(self, idx):
        dest = r"(?:HDMI(?:/HDBT)?\s*(?:out(?:put)?\s*)?|HDBT\s*out\s*)" + str(idx)
        layout = await self._ask(f"get output {idx} display", dest + r"\s+display\s+.+")
        try:
            mode = layout_value(re.split(r"\s+display\s+", layout, flags=re.I)[1])
        except ValueError:
            mode = None
        reply = await self._ask(
            f"get output {idx} source", dest + r"\s+video source\b.*"
        )
        sources = source_list(re.split(r"\bvideo source\b", reply, flags=re.I)[1])
        windows = {
            f"window_{n}": source
            for n, source in enumerate((sources + [None] * 4)[:4], 1)
        }
        self.set_child_state_batch(
            "output",
            idx,
            {
                "layout": mode,
                "source": sources[0]
                if len(sources) == 1 and is_single_view(mode)
                else None,
                **windows,
            },
        )

    async def _usb(self):
        mode = reported_value(await self._ask("get usb switch mode"), MODES)
        host = reported_value(await self._ask("get usb switch"), HOSTS)
        self.set_states({"usb_mode": mode, "usb_host": host})

    async def _usb_power(self, idx):
        reply = await self._ask(
            f"get host5v {idx}", re.escape(HOSTS[idx - 1]) + r" (?:on|off)"
        )
        self.set_child_state("usb_input", idx, "host_5v", reply.lower().endswith(" on"))

    async def _master(self):
        vol = await self._ask("get master audio vol", r"master volume [0-9]{1,3}")
        mute = await self._ask("get master audio mute", r"master mute (?:on|off)")
        self.set_states(
            {
                "master_volume": level(vol.split()[-1]),
                "master_mute": mute.lower().endswith(" on"),
            }
        )

    async def _audio(self, idx):
        prefix = r"(?:HDMI/HDBT|HDMI) out 2" if idx == 2 else re.escape(AUDIO[idx - 1])
        vol = await self._ask(
            f"get output {idx} audio vol", prefix + r" volume [0-9]{1,3}"
        )
        mute = await self._ask(
            f"get output {idx} audio mute", prefix + r" mute (?:on|off)"
        )
        self.set_child_state_batch(
            "audio_output",
            idx,
            {"volume": level(vol.split()[-1]), "mute": mute.lower().endswith(" on")},
        )

    async def _receiver(self):
        try:
            answer = await self._ask("get rx input", r"HDBT in|HDMI in")
        except CommandRefusedError:
            self.set_state("receiver_input", None)
            return
        self.set_state("receiver_input", choice(answer, RX_INPUTS))

    async def _name(self, idx):
        value = await self._ask(f"get preset {idx} name", r"[ -~]{0,16}")
        self.set_child_state_batch(
            "preset", idx, {"name": value or f"Preset {idx}", "device_name": value}
        )

    async def _names(self):
        for idx in range(1, 9):
            await self._name(idx)
        self.set_state("preset_names_ready", True)
        self._next_names = monotonic() + 60

    async def refresh_children(self):
        async with self._io:
            await self._refresh(True)
        return {"presets": len(self.list_children("preset"))}

    async def send_command(self, command, params=None):
        async with self._io:
            try:
                return await self._dispatch(command, params or {})
            except ValueError as exc:
                raise CommandRefusedError(str(exc)) from exc

    async def _dispatch(self, command, p):
        if command == "refresh":
            await self._refresh(True)
        elif command in ("power_on", "power_off"):
            await self._set_power(command == "power_on")
        elif command == "read_power":
            await self._power()
        elif not self._awake:
            raise CommandRefusedError(
                "Power on the switcher before using this control."
            )
        else:
            await self._operate(command, p)
        return True

    async def _set_power(self, on):
        if await self._power() == on:
            return
        if on:
            # Its power-on reply includes several boot lines. Finish at the
            # firmware line documented on p. 26, then read back the device.
            await self._ask("power on", r"MCU\s+.+")
        else:
            await self._ask("power off", r"power off")
        await self._refresh(True)

    async def _operate(self, cmd, p):
        if cmd.startswith("read_"):
            await self._read_command(cmd, p)
        elif cmd in ("route_video", "set_layout", "set_windows"):
            await self._set_video(cmd, p)
        elif cmd in ("route_usb", "set_usb_mode"):
            await self._set_usb(cmd, p)
        elif cmd.endswith("_preset"):
            await self._preset(cmd, p)
        elif cmd == "set_receiver_input":
            await self._receiver_control(p)
        elif cmd.startswith(("freeze_", "av_mute_", "enable_")):
            await self._connector_control(cmd, p)
        else:
            await self._audio_control(cmd, p)

    async def _read_command(self, cmd, p):
        if cmd in ("read_input_signal", "read_connector"):
            slot(p, "input" if cmd == "read_input_signal" else "connector")
            await self._signals()
            return
        reads = {
            "read_video": (self._video, "output"),
            "read_layout": (self._video, "output"),
            "read_audio_volume": (self._audio, "audio_output"),
            "read_audio_mute": (self._audio, "audio_output"),
            "read_preset_name": (self._name, "preset"),
            "read_usb_power": (self._usb_power, "usb_input"),
            "read_usb_mode": (self._usb, None),
            "read_usb_host": (self._usb, None),
            "read_master_volume": (self._master, None),
            "read_master_mute": (self._master, None),
            "read_receiver_input": (self._receiver, None),
        }
        if cmd not in reads:
            raise ValueError(f"Unknown query: {cmd}")
        fetch, kind = reads[cmd]
        await fetch(*([slot(p, kind)] if kind else []))

    async def _set_video(self, cmd, p):
        idx = slot(p, "output")
        dest = f"set output {idx}"
        try:
            await self._write_video(cmd, p, dest)
        except CommandRefusedError:
            # The first step of a single-source route can already have selected
            # Single layout when the second step is refused.
            await self._video(idx)
            await self._usb()
            raise
        await self._video(idx)
        await self._usb()

    async def _write_video(self, cmd, p, dest):
        if cmd == "set_layout":
            mode = ordinal(p.get("layout"), LAYOUTS)
            await self._ask(f"{dest} display {mode}", r"set .+ display .+")
        elif cmd == "route_video":
            source = ordinal(p.get("source"), SOURCES)
            await self._ask(f"{dest} display 2", r"set .+ display single")
            await self._ask(f"{dest} source {source}", r"set .+ video source .+")
        else:
            values = [str(ordinal(p.get(f"window_{n}"), SOURCES)) for n in range(1, 5)]
            await self._ask(
                f"{dest} source {' '.join(values)}", r"set .+ video source .+"
            )

    async def _set_usb(self, cmd, p):
        if cmd == "route_usb":
            host = ordinal(p.get("source"), HOSTS)
            await self._ask("set usb switch mode 2", r"set USB switch mode .+")
            try:
                await self._ask(f"set usb manual {host}", r"set USB(?:-C| host) .+")
            except CommandRefusedError:
                await self._usb()
                raise
        else:
            mode = ordinal(p.get("mode"), MODES)
            await self._ask(f"set usb switch mode {mode}", r"set USB switch mode .+")
        await self._usb()

    async def _preset(self, cmd, p):
        idx = slot(p, "preset")
        if cmd == "rename_preset":
            name = preset_name(p.get("name"))
            await self._ask(
                f"set preset {idx} name {name}", rf"set preset {idx} name .+"
            )
            await self._name(idx)
        elif cmd == "save_preset":
            await self._ask(f"set preset save {idx}", rf"set preset {idx} save")
        elif cmd == "recall_preset":
            await self._ask(f"set preset recall {idx}", rf"set preset {idx} recall")
            await self._refresh(True)
        else:
            raise ValueError(f"Unknown preset command: {cmd}")

    async def _receiver_control(self, p):
        value = ordinal(p.get("source"), RX_INPUTS)
        await self._ask(f"set rx input {value}", r"set RX .+ in")
        await self._receiver()

    async def _connector_control(self, cmd, p):
        idx = slot(p, "connector")
        prop, flag = cmd.rsplit("_", 1)
        key = {"av_mute": "avmute", "freeze": "freeze", "enable": "enable"}[prop]
        await self._ask(
            f"set output {idx} {key} {int(flag == 'on')}", rf"set .+ {key} (?:on|off)"
        )
        await self._signals()

    async def _audio_control(self, cmd, p):
        if cmd not in (
            "set_master_volume",
            "master_mute_on",
            "master_mute_off",
            "set_audio_volume",
            "audio_mute_on",
            "audio_mute_off",
        ):
            raise ValueError(f"Unknown control: {cmd}")
        master = "master" in cmd
        idx = None if master else slot(p, "audio_output")
        group = "master" if master else f"output {idx}"
        if cmd.endswith("volume"):
            await self._ask(
                f"set {group} audio vol {level(p.get('level'))}",
                r"(?:set )?.+ volume [0-9]+",
            )
        else:
            await self._ask(
                f"set {group} audio mute {int(cmd.endswith('_on'))}",
                r"(?:set )?.+ mute (?:on|off)",
            )
        if master:
            await self._master()
        else:
            await self._audio(idx)

    async def set_device_setting(self, key, value):
        if key != "usb_mode":
            raise CommandRefusedError("Unknown switcher setting.")
        return await self.send_command("set_usb_mode", {"mode": value})
