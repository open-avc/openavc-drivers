"""Lightware MMX2-4x3-H20, LW3 control and native two-way serial bridge.

Source: https://assets.prod.pim.lightware.com/assets/File-Downloads/Guides-and-Manuals/User-Manual/MMX2_series_UserManual.pdf
MMX2 series manual rev. 3 (2026-09-01), chapters 4.11 and 7.
Python applies downstream line settings through prepare_bridge_port, correlates
and checks device replies, and locates the documented O2/O4 analog audio port
from a successful read instead of guessing from inconsistent manual examples.
OPEN subscriptions are renewed on every connection; a full poll is a backstop.
Transparent serial bytes never pass through this command parser.
No hardware verification. Simulator behavior is document-derived.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from openavc.drivers.base import BaseDriver

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:
    CommandRefusedError = RuntimeError

OUTPUTS = 3
SERIALS = 2
WIRES = {
    "identify": "CALL /V1/MANAGEMENT/UI:identifyMe()",
    "reboot": "CALL /V1/SYS:restart()",
    "set_device_label": "SET /V1/MANAGEMENT/LABEL.DeviceLabel={label}",
    "set_video_route": "CALL /V1/MEDIA/VIDEO/XP:switch(I{input}:O{output})",
    "disconnect_video_output": "CALL /V1/MEDIA/VIDEO/XP:switch(0:O{output})",
    "set_video_route_all": "CALL /V1/MEDIA/VIDEO/XP:switchAll(I{input})",
    "set_video_mute": "SET /V1/MEDIA/VIDEO/XP/O{output}.Mute={state}",
    "set_video_lock": "SET /V1/MEDIA/VIDEO/XP/O{output}.Lock={state}",
    "set_output_5v": "SET /V1/MEDIA/VIDEO/O{output}.Output5VMode={state}",
    "set_input_hdcp": "SET /V1/MEDIA/VIDEO/I{input}/HDCP.AllowedHdcpVersion={version}",
    "set_output_hdcp_mode": "SET /V1/MEDIA/VIDEO/O{output}.HdcpMode={mode}",
    "set_output_signal_type": "SET /V1/MEDIA/VIDEO/O{output}.OutputSignalType={signal_type}",
    "set_embedded_audio_mute": "SET /V1/MEDIA/VIDEO/O{output}.EmbeddedAudioMute={state}",
    "set_audio_route": "CALL /V1/MEDIA/AUDIO/XP:switch(I{input}:{analog})",
    "disconnect_audio_output": "CALL /V1/MEDIA/AUDIO/XP:switch(0:{analog})",
    "set_analog_volume_percent": "SET /V1/MEDIA/AUDIO/{analog}.VolumePercent={level}",
    "set_analog_volume_db": "SET /V1/MEDIA/AUDIO/{analog}.VolumedB={level}",
    "set_analog_balance": "SET /V1/MEDIA/AUDIO/{analog}.Balance={level}",
    "step_analog_volume_db": "CALL /V1/MEDIA/AUDIO/{analog}:stepVolumedB({step})",
    "step_analog_volume_percent": "CALL /V1/MEDIA/AUDIO/{analog}:stepVolumePercent({step})",
    "set_analog_mute": "SET /V1/MEDIA/AUDIO/{analog}.Mute={state}",
    "set_edid": "CALL /V1/EDID:switch({source}:E{input})",
    "set_edid_all": "CALL /V1/EDID:switchAll({source})",
    "copy_edid": "CALL /V1/EDID:copy({source}:U{slot})",
    "reset_edid": "CALL /V1/EDID:reset()",
    "cec_send": "CALL /V1/MEDIA/VIDEO/O{output}/CEC:send({command})",
    "cec_user_control": "CALL /V1/MEDIA/VIDEO/O{output}/CEC:sendUserControlClick({command})",
}
VERSION = "/V1/MANAGEMENT/UID/PACKAGE.Version"
SERIAL_FIELDS = {
    "baudrate": "Baudrate",
    "parity": "Parity",
    "stopbits": "StopBits",
    "databits": "DataBits",
}
VIDEO_FIELDS = {
    "ConnectedSource": ("source", "source"),
    "Mute": ("mute", "bool"),
    "Lock": ("lock", "bool"),
}
OUTPUT_FIELDS = {
    "HdcpMode": ("hdcp_mode", "str"),
    "Output5VMode": ("output_5v", "str"),
    "OutputSignalType": ("signal_type", "str"),
    "EmbeddedAudioMute": ("embedded_audio_mute", "bool"),
}
AUDIO_FIELDS = {
    "VolumePercent": ("audio_volume_percent", "float"),
    "VolumedB": ("audio_volume_db", "float"),
    "Balance": ("audio_balance", "int"),
    "Mute": ("audio_mute", "bool"),
}


def _escape(value: Any) -> str:
    return "".join("\\" + c if c in "\\{}#%()\r\n\t" else c for c in str(value))


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _value(value: str, kind: str) -> Any:
    if kind == "bool":
        if value not in ("true", "false"):
            raise ValueError("Expected true or false")
        return value == "true"
    if kind == "source":
        return int(value.removeprefix("I"))
    return {"float": float, "int": int, "str": _unescape}[kind](value)


class LightwareMMX2Driver(BaseDriver):
    DRIVER_INFO = {
        "manufacturer": "Lightware",
        "category": "switcher",
        "author": "OpenAVC",
        "transport": "tcp",
        "protocols": ["lightware_lw3"],
        "id": "lightware_mmx2_4x3_h20",
        "name": "Lightware MMX2-4x3-H20",
        "version": "1.0.0",
        "min_platform_version": "0.36.0",
        "source_url": "https://assets.prod.pim.lightware.com/assets/File-Downloads/Guides-and-Manuals/User-Manual/MMX2_series_UserManual.pdf",
        "verified": False,
        "simulated": True,
        "delimiter": "\r\n",
        "description": "Controls the MMX2-4x3-H20 HDMI switcher and its analog audio output. Provides 2 "
        "two-way RS-232 bridge port(s) for connected equipment, with automatic serial line "
        "setup, live feedback and connection monitoring.",
        "tags": ["matrix-switcher", "hdmi", "lw3", "serial-bridge", "rs232", "cec"],
        "ports": [6107],
        "compatible_models": [
            {
                "manufacturer": "Lightware",
                "models": ["MMX2-4x3-H20"],
                "confidence": "untested",
                "notes": "Built from the manufacturer manual. Serial-over-IP must be "
                "enabled in Lightware Device Controller. The downstream driver "
                "supplies baud rate, parity and stop bits; hardware testing is "
                "outstanding.",
            }
        ],
        "bridge": {
            "ports": [
                {
                    "id": "serial:1",
                    "kind": "serial",
                    "label": "RS-232 1 (P1)",
                    "passthrough_port": 8001,
                },
                {
                    "id": "serial:2",
                    "kind": "serial",
                    "label": "RS-232 2 (P2)",
                    "passthrough_port": 8002,
                },
            ]
        },
        "default_config": {"host": "", "port": 6107, "poll_interval": 60},
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "default": 6107,
                "label": "LW3 TCP Port",
                "min": 1,
                "max": 65535,
            },
            "poll_interval": {
                "type": "integer",
                "default": 60,
                "min": 0,
                "label": "Status Refresh (sec)",
                "description": "How often every value is checked again. "
                "Changes are also reported as they happen.",
            },
        },
        "help": {
            "overview": "Routes four HDMI inputs to 3 HDMI output(s), controls the analog audio "
            "output and provides 2 RS-232 bridge port(s). Add each connected serial "
            "device with its own driver and choose Through a bridge.",
            "setup": "1. Connect the MMX2-4x3-H20 to the control network and enter its IP address. "
            "Enable LW3 on TCP 6107 in Lightware Device Controller (LDC). This driver uses "
            "the raw LW3 connection; it does not sign in to the web interface.\n"
            "2. In LDC, open Settings > Network and enable Serial over IP for each RS-232 "
            "port you will use. Allow TCP 6107 and the selected serial ports (8001 for P1, "
            "8002 for P2) from the OpenAVC server.\n"
            "3. Connect TX to the serial device's RX, RX to TX, and ground to ground. Use "
            "an eight-data-bit device; hardware flow control is not available on the "
            "three-pin connector.\n"
            "4. Add the connected equipment with its own serial driver. Choose Through a "
            "bridge, select this MMX2-4x3-H20, then select its RS-232 connector. Set the "
            "equipment's baud rate, parity and stop bits in that device's connection "
            "settings. The bridge applies changed values and checks the device's reply "
            "before the downstream connection opens.\n"
            "5. Check Serial Setup for that port on the MMX2-4x3-H20 device page. If it "
            "reports a mismatch or failed setup, correct the settings and reconnect the "
            "downstream device. OpenAVC may still open its network connection after a setup "
            "failure; that alone does not prove serial communication works.\n"
            "6. Use one controlling driver per serial connector. Disable any LARA "
            "automation that sends commands to the same serial equipment. Test both a "
            "command and the equipment's returned status.\n"
            "Serial replies require an open downstream connection and are not buffered "
            "while it is closed. The downstream driver refreshes its readings after "
            "reconnecting. Serial settings are checked again whenever the bridge "
            "reconnects. A device using software flow control must have that function "
            "disabled.\n"
            "Simulation covers the switcher and serial setup. It does not create the "
            "physical serial network ports; test a connected serial device separately.",
            "connection": "Check the IP address and LW3 service. For serial equipment, check Serial "
            "Setup, Serial over IP in LDC, the selected connector and the TX/RX "
            "wiring. A network connection does not prove a serial reply was received.",
        },
        "discovery": {
            "oui": ["a8:d2:36"],
            "port_open": [6107],
            "manufacturer_alias": ["lightware", "lightware visual engineering"],
        },
        "state_variables": {
            "device_label": {
                "type": "string",
                "label": "Device Label",
                "help": "Custom label set on the unit (DeviceLabel property).",
            },
            "firmware": {
                "type": "string",
                "label": "Firmware Version",
                "help": "Package firmware version reported by "
                "/V1/MANAGEMENT/UID/PACKAGE.Version.",
            },
            "audio_volume_percent": {
                "type": "number",
                "label": "Analog Audio Out Volume (%)",
                "help": "Analog audio output volume as a 0–100% scale.",
                "min": 0,
                "max": 100,
                "unit": "%",
                "control": True,
            },
            "audio_volume_db": {
                "type": "number",
                "label": "Analog Audio Out Volume (dB)",
                "help": "Analog audio output volume in dB.",
                "min": -95.62,
                "max": 0,
                "unit": "dB",
                "control": True,
            },
            "audio_mute": {
                "type": "boolean",
                "label": "Analog Audio Out Mute",
                "help": "True when the analog audio output is muted.",
                "control": True,
            },
            "last_error": {"type": "string", "label": "Last Error"},
            "analog_port": {"type": "string", "label": "Analog Audio Port"},
            "audio_source": {
                "type": "integer",
                "label": "Analog Audio Source",
                "min": 0,
                "max": 4,
                "control": True,
            },
            "audio_balance": {
                "type": "integer",
                "label": "Analog Audio Balance",
                "min": -100,
                "max": 100,
                "control": True,
            },
            "audio_crosspoint_mute": {
                "type": "boolean",
                "label": "Analog Audio Route Muted",
                "control": True,
            },
            "edid_status": {"type": "string", "label": "Emulated EDIDs"},
            "serial_1_baudrate": {
                "type": "enum",
                "label": "RS-232 1 Baud Rate",
                "values": ["9600", "19200", "38400", "57600", "115200"],
            },
            "serial_1_parity": {
                "type": "enum",
                "label": "RS-232 1 Parity",
                "values": ["None", "Odd", "Even"],
            },
            "serial_1_stopbits": {
                "type": "enum",
                "label": "RS-232 1 Stop Bits",
                "values": ["1", "2"],
            },
            "serial_1_databits": {"type": "integer", "label": "RS-232 1 Data Bits"},
            "serial_1_tcp_port": {"type": "integer", "label": "RS-232 1 Network Port"},
            "serial_1_setup": {
                "type": "string",
                "label": "RS-232 1 Serial Setup",
                "cloud_priority": "high",
            },
            "serial_2_baudrate": {
                "type": "enum",
                "label": "RS-232 2 Baud Rate",
                "values": ["9600", "19200", "38400", "57600", "115200"],
            },
            "serial_2_parity": {
                "type": "enum",
                "label": "RS-232 2 Parity",
                "values": ["None", "Odd", "Even"],
            },
            "serial_2_stopbits": {
                "type": "enum",
                "label": "RS-232 2 Stop Bits",
                "values": ["1", "2"],
            },
            "serial_2_databits": {"type": "integer", "label": "RS-232 2 Data Bits"},
            "serial_2_tcp_port": {"type": "integer", "label": "RS-232 2 Network Port"},
            "serial_2_setup": {
                "type": "string",
                "label": "RS-232 2 Serial Setup",
                "cloud_priority": "high",
            },
        },
        "child_entity_types": {
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "integer", "min": 1, "max": 3},
                "state_variables": {
                    "source": {
                        "type": "integer",
                        "label": "Connected Input",
                        "min": 0,
                        "max": 4,
                        "control": True,
                        "cloud_priority": "high",
                        "help": "HDMI input number; 0 means disconnected.",
                    },
                    "mute": {
                        "type": "boolean",
                        "label": "Video Muted",
                        "help": "True when this output's "
                        "video path is muted "
                        "(image blanked).",
                        "cloud_priority": "high",
                    },
                    "lock": {
                        "type": "boolean",
                        "label": "Crosspoint Locked",
                        "help": "True when this output's "
                        "crosspoint is locked "
                        "against route changes.",
                    },
                    "hdcp_mode": {
                        "type": "string",
                        "label": "HDCP Mode",
                        "help": "Output HDCP mode "
                        "(Auto encrypts only "
                        "when the input is "
                        "encrypted; Always "
                        "forces HDCP).",
                    },
                    "output_5v": {
                        "type": "string",
                        "label": "5V Mode",
                        "help": "HDMI +5V power mode on this output (On / Auto / Off).",
                    },
                    "signal_type": {
                        "type": "string",
                        "label": "Signal Type",
                        "help": "Forced output signal type (Auto or DVI).",
                    },
                    "embedded_audio_mute": {
                        "type": "boolean",
                        "label": "HDMI Audio Muted",
                        "control": True,
                    },
                },
                "summary_fields": ["source", "mute"],
                "instances": {"count": 3, "label": "HDMI Output {id}"},
            },
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "state_variables": {
                    "signal": {
                        "type": "boolean",
                        "label": "Signal Present",
                        "help": "True when this input "
                        "reports a valid "
                        "sync/signal "
                        "(SignalPresent).",
                        "cloud_priority": "high",
                    },
                    "hdcp_version": {
                        "type": "string",
                        "label": "Allowed HDCP Version",
                        "help": "HDCP version this "
                        "input advertises "
                        "to sources (HDCP "
                        "1.4 / HDCP 2.2 / "
                        "Off).",
                    },
                },
                "summary_fields": ["signal"],
                "instances": {"count": 4, "label": "HDMI Input {id}"},
            },
        },
        "routing": {
            "destination_child_type": "output",
            "source_child_type": "input",
            "command": "set_video_route",
            "planes": [{"label": "Video", "route_property": "source"}],
        },
        "commands": {
            "identify": {
                "label": "Identify (Flash LEDs)",
                "help": "Flash the front-panel LEDs for 10 seconds to physically locate "
                "this unit.",
                "params": {},
            },
            "reboot": {
                "label": "Reboot",
                "help": "Restart the unit. Active connections will be terminated.",
                "params": {},
                "confirm": "Restart this switcher? Video and serial connections will be "
                "interrupted.",
            },
            "set_device_label": {
                "label": "Set Device Label",
                "help": "Set a custom device label (max 49 ASCII characters).",
                "params": {
                    "label": {
                        "type": "string",
                        "required": True,
                        "label": "Device Label",
                        "pattern": "^[\\x20-\\x7e]{1,49}$",
                    }
                },
                "sets": {"device_label": "{label}"},
            },
            "set_video_route": {
                "label": "Route Video Source to Output",
                "help": "Route an HDMI input to an HDMI output.",
                "params": {
                    "input": {
                        "type": "enum",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Input",
                        "max": 4,
                        "values": [
                            {"value": 1, "label": "HDMI Input 1"},
                            {"value": 2, "label": "HDMI Input 2"},
                            {"value": 3, "label": "HDMI Input 3"},
                            {"value": 4, "label": "HDMI Input 4"},
                        ],
                    },
                    "output": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Output",
                        "child_type": "output",
                        "max": 3,
                    },
                },
                "sets": {"source": "{input}"},
            },
            "disconnect_video_output": {
                "label": "Disconnect Video Output",
                "help": "Disconnect any video source from the specified output.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Output",
                        "child_type": "output",
                        "max": 3,
                    }
                },
                "sets": {"source": 0},
            },
            "set_video_route_all": {
                "label": "Route Video to All Outputs",
                "help": "Route a single input to every video output.",
                "params": {
                    "input": {
                        "type": "enum",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Input",
                        "max": 4,
                        "values": [
                            {"value": 1, "label": "HDMI Input 1"},
                            {"value": 2, "label": "HDMI Input 2"},
                            {"value": 3, "label": "HDMI Input 3"},
                            {"value": 4, "label": "HDMI Input 4"},
                        ],
                    }
                },
                "confirm": "Route this input to every HDMI output?",
            },
            "set_video_mute": {
                "label": "Set Video Mute",
                "help": "Mute or unmute the specified video output port.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Output",
                        "child_type": "output",
                        "max": 3,
                    },
                    "state": {
                        "type": "boolean",
                        "required": True,
                        "label": "Mute State",
                        "default": False,
                    },
                },
                "sets": {"mute": "{state}"},
            },
            "set_video_lock": {
                "label": "Lock Video Output",
                "help": "Lock the video output crosspoint to prevent route changes.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Output",
                        "child_type": "output",
                        "max": 3,
                    },
                    "state": {
                        "type": "boolean",
                        "required": True,
                        "label": "Lock State",
                        "default": False,
                    },
                },
                "sets": {"lock": "{state}"},
            },
            "set_output_5v": {
                "label": "Set HDMI Output +5V",
                "help": "Enable or disable the HDMI +5V power on the output port.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Output",
                        "child_type": "output",
                        "max": 3,
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "values": ["On", "Auto", "Off", "Follow signal present"],
                        "label": "5V Mode",
                    },
                },
                "sets": {"output_5v": "{state}"},
            },
            "set_input_hdcp": {
                "label": "Set Input HDCP Allowed Version",
                "help": "Choose which HDCP version the input port advertises to "
                "sources.",
                "params": {
                    "input": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Input",
                        "max": 4,
                        "child_type": "input",
                    },
                    "version": {
                        "type": "enum",
                        "required": True,
                        "values": ["HDCP 1.4", "HDCP 2.2", "Off"],
                        "label": "HDCP Version",
                    },
                },
                "sets": {"hdcp_version": "{version}"},
            },
            "set_output_hdcp_mode": {
                "label": "Set Output HDCP Mode",
                "help": "Auto encrypts only when the input is encrypted; "
                "Always forces HDCP.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Output",
                        "child_type": "output",
                        "max": 3,
                    },
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "values": ["Auto", "Always"],
                        "label": "HDCP Mode",
                    },
                },
                "sets": {"hdcp_mode": "{mode}"},
            },
            "set_output_signal_type": {
                "label": "Set Output Signal Type",
                "help": "Force the output signal type (HDMI auto-detect or DVI).",
                "params": {
                    "output": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Output",
                        "child_type": "output",
                        "max": 3,
                    },
                    "signal_type": {
                        "type": "enum",
                        "required": True,
                        "values": ["Auto", "DVI"],
                        "label": "Signal Type",
                    },
                },
                "sets": {"signal_type": "{signal_type}"},
            },
            "set_embedded_audio_mute": {
                "label": "Set Embedded Audio Mute",
                "help": "Mute or unmute the audio embedded in the HDMI output.",
                "params": {
                    "output": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Output",
                        "child_type": "output",
                        "max": 3,
                    },
                    "state": {
                        "type": "boolean",
                        "required": True,
                        "label": "Mute State",
                        "default": False,
                    },
                },
                "sets": {"embedded_audio_mute": "{state}"},
            },
            "set_audio_route": {
                "label": "Route Audio Source to Output",
                "help": "Route an audio input to an audio output (analog or "
                "de-embedded).",
                "params": {
                    "input": {
                        "type": "enum",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Input",
                        "max": 4,
                        "values": [
                            {"value": 1, "label": "HDMI Input 1"},
                            {"value": 2, "label": "HDMI Input 2"},
                            {"value": 3, "label": "HDMI Input 3"},
                            {"value": 4, "label": "HDMI Input 4"},
                        ],
                    }
                },
                "sets": {"audio_source": "{input}"},
            },
            "disconnect_audio_output": {
                "label": "Disconnect Audio Output",
                "help": "Disconnect any audio source from the specified audio output.",
                "params": {},
                "sets": {"audio_source": 0},
            },
            "set_analog_volume_percent": {
                "label": "Set Analog Output Volume (%)",
                "help": "Set the analog audio output level as a percentage (0–100).",
                "params": {
                    "level": {
                        "type": "number",
                        "required": True,
                        "min": 0,
                        "max": 100,
                        "label": "Volume (%)",
                    }
                },
                "sets": {"audio_volume_percent": "{level}"},
            },
            "set_analog_volume_db": {
                "label": "Set Analog Output Volume (dB)",
                "help": "Set the analog audio output level in dB (-95.62 to "
                "0 in 0.375 dB steps).",
                "params": {
                    "level": {
                        "type": "number",
                        "required": True,
                        "min": -95.62,
                        "max": 0,
                        "label": "Volume (dB)",
                    }
                },
                "sets": {"audio_volume_db": "{level}"},
            },
            "set_analog_balance": {
                "label": "Set Analog Output Balance",
                "help": "Set the analog audio balance (-100 = full left, 0 = "
                "center, 100 = full right).",
                "params": {
                    "level": {
                        "type": "integer",
                        "required": True,
                        "min": -100,
                        "max": 100,
                        "label": "Balance",
                    }
                },
                "sets": {"audio_balance": "{level}"},
            },
            "step_analog_volume_db": {
                "label": "Step Analog Output Volume (dB)",
                "help": "Increment or decrement the analog output volume "
                "by the given dB step.",
                "params": {
                    "step": {
                        "type": "number",
                        "required": True,
                        "default": -1,
                        "label": "Step (dB)",
                    }
                },
            },
            "step_analog_volume_percent": {
                "label": "Step Analog Output Volume (%)",
                "help": "Increment or decrement the analog output "
                "volume by the given percentage step.",
                "params": {
                    "step": {
                        "type": "number",
                        "required": True,
                        "default": 5,
                        "label": "Step (%)",
                        "min": -100,
                        "max": 100,
                    }
                },
            },
            "set_analog_mute": {
                "label": "Set Analog Output Mute",
                "help": "Mute or unmute the analog audio output (Mute property "
                "on the port itself).",
                "params": {
                    "state": {
                        "type": "boolean",
                        "required": True,
                        "label": "Mute State",
                        "default": False,
                    }
                },
                "sets": {"audio_mute": "{state}"},
            },
            "set_edid": {
                "label": "Emulate EDID on Input Port",
                "help": "Emulate an EDID memory slot on a given input. Source examples: "
                "F49 (Universal HDMI), F89, U2 (User EDID 2), D1 (Dynamic ; "
                "last sink).\n",
                "params": {
                    "source": {
                        "type": "string",
                        "required": True,
                        "default": "F49",
                        "label": "EDID Source",
                        "description": "Factory F1-F159, user U1-U100 or "
                        "dynamic D1-D3.",
                        "pattern": "^(?:F(?:[1-9]|[1-9][0-9]|1[0-4][0-9]|15[0-9])|U(?:[1-9]|[1-9][0-9]|100)|D[1-3])$",
                    },
                    "input": {
                        "type": "enum",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Input",
                        "max": 4,
                        "values": [
                            {"value": 1, "label": "HDMI Input 1"},
                            {"value": 2, "label": "HDMI Input 2"},
                            {"value": 3, "label": "HDMI Input 3"},
                            {"value": 4, "label": "HDMI Input 4"},
                        ],
                    },
                },
            },
            "set_edid_all": {
                "label": "Emulate EDID on All Inputs",
                "help": "Emulate the same EDID source on every input port.",
                "params": {
                    "source": {
                        "type": "string",
                        "required": True,
                        "default": "F49",
                        "label": "EDID Source",
                        "pattern": "^(?:F(?:[1-9]|[1-9][0-9]|1[0-4][0-9]|15[0-9])|U(?:[1-9]|[1-9][0-9]|100)|D[1-3])$",
                        "description": "Factory F1-F159, user U1-U100 "
                        "or dynamic D1-D3.",
                    }
                },
                "confirm": "Replace the emulated EDID on every HDMI input?",
            },
            "copy_edid": {
                "label": "Copy EDID to User Memory",
                "help": "Copy an EDID into a user memory slot (U1..U100).",
                "params": {
                    "source": {
                        "type": "string",
                        "required": True,
                        "label": "Source EDID",
                        "pattern": "^(?:F(?:[1-9]|[1-9][0-9]|1[0-4][0-9]|15[0-9])|U(?:[1-9]|[1-9][0-9]|100)|D[1-3])$",
                        "description": "Factory F1-F159, user U1-U100 or "
                        "dynamic D1-D3.",
                    },
                    "slot": {
                        "type": "integer",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "max": 100,
                        "label": "User Slot",
                    },
                },
                "confirm": "Replace the EDID stored in this user slot?",
            },
            "reset_edid": {
                "label": "Reset Emulated EDIDs",
                "confirm": "Replaces every emulated EDID with the factory default.",
                "help": "Reset all emulated EDIDs to factory defaults.",
                "params": {},
            },
            "cec_send": {
                "label": "Send CEC Command",
                "help": "Send a raw CEC opcode on the HDMI output (e.g. 446D=Power on, "
                "446C=Power off, 36=Standby, 446B=Power toggle, 4441=Volume up, "
                "4442=Volume down, 4443=Mute toggle, 4444=Play, 4445=Stop, "
                "4446=Pause).\n",
                "params": {
                    "output": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Output",
                        "child_type": "output",
                        "max": 3,
                    },
                    "command": {
                        "type": "string",
                        "required": True,
                        "default": "446D",
                        "label": "CEC Hex Code",
                        "pattern": "^(?:[0-9A-Fa-f]{2}){1,16}$",
                    },
                },
            },
            "cec_user_control": {
                "label": "Send CEC User Control Click",
                "help": "Send a CEC push-button-style command (volume, "
                "navigation, etc.).",
                "params": {
                    "output": {
                        "type": "child_id",
                        "required": True,
                        "default": 1,
                        "min": 1,
                        "label": "Output",
                        "child_type": "output",
                        "max": 3,
                    },
                    "command": {
                        "type": "string",
                        "required": True,
                        "default": "4441",
                        "label": "CEC Hex Code",
                        "pattern": "^(?:[0-9A-Fa-f]{2}){1,16}$",
                    },
                },
            },
            "refresh": {
                "label": "Refresh Status",
                "help": "Read the switcher and serial settings again.",
                "params": {},
            },
        },
        "actions": [
            {"id": "set_video_route"},
            {"id": "set_video_mute"},
            {"id": "set_analog_volume_percent"},
            {"id": "set_analog_mute"},
            {"id": "identify"},
        ],
        "device_settings": {
            "device_label": {
                "type": "string",
                "label": "Device Label",
                "state_key": "device_label",
                "default": "Lightware",
                "setup": False,
                "help": "The name shown in Lightware Device Controller. Up "
                "to 49 ASCII characters.",
                "regex": "^[\\x20-\\x7e]{1,49}$",
            },
            "serial_1_baudrate": {
                "type": "enum",
                "label": "RS-232 1 Baud Rate",
                "state_key": "serial_1_baudrate",
                "values": ["9600", "19200", "38400", "57600", "115200"],
                "default": "9600",
                "setup": False,
                "help": "For a device connected Through a bridge, "
                "change this in the downstream device "
                "connection settings. Its settings are applied "
                "again on reconnect.",
            },
            "serial_1_parity": {
                "type": "enum",
                "label": "RS-232 1 Parity",
                "state_key": "serial_1_parity",
                "values": ["None", "Odd", "Even"],
                "default": "None",
                "setup": False,
                "help": "For a device connected Through a bridge, change "
                "this in the downstream device connection "
                "settings. Its settings are applied again on "
                "reconnect.",
            },
            "serial_1_stopbits": {
                "type": "enum",
                "label": "RS-232 1 Stop Bits",
                "state_key": "serial_1_stopbits",
                "values": ["1", "2"],
                "default": "1",
                "setup": False,
                "help": "For a device connected Through a bridge, "
                "change this in the downstream device "
                "connection settings. Its settings are applied "
                "again on reconnect.",
            },
            "serial_2_baudrate": {
                "type": "enum",
                "label": "RS-232 2 Baud Rate",
                "state_key": "serial_2_baudrate",
                "values": ["9600", "19200", "38400", "57600", "115200"],
                "default": "9600",
                "setup": False,
                "help": "For a device connected Through a bridge, "
                "change this in the downstream device "
                "connection settings. Its settings are applied "
                "again on reconnect.",
            },
            "serial_2_parity": {
                "type": "enum",
                "label": "RS-232 2 Parity",
                "state_key": "serial_2_parity",
                "values": ["None", "Odd", "Even"],
                "default": "None",
                "setup": False,
                "help": "For a device connected Through a bridge, change "
                "this in the downstream device connection "
                "settings. Its settings are applied again on "
                "reconnect.",
            },
            "serial_2_stopbits": {
                "type": "enum",
                "label": "RS-232 2 Stop Bits",
                "state_key": "serial_2_stopbits",
                "values": ["1", "2"],
                "default": "1",
                "setup": False,
                "help": "For a device connected Through a bridge, "
                "change this in the downstream device "
                "connection settings. Its settings are applied "
                "again on reconnect.",
            },
        },
    }
    HEALTH_INTERVAL_S = 30
    HEALTH_TIMEOUT_S = 5
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The MMX2 stopped answering status requests."

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_lock = asyncio.Lock()
        self._serial_lock = asyncio.Lock()
        self._pending: asyncio.Future | None = None
        self._signature = 0
        self._response_signature = None
        self._response_lines: list[str] = []
        self._analog = ""
        self._paths: dict[str, tuple[str, str]] = {}
        self._standing_error = ""
        self._declare_ports()

    def _declare_ports(self) -> None:
        for kind, count in (("input", 4), ("output", OUTPUTS)):
            self._register_children(kind, count)
        self._paths = {
            VERSION: ("firmware", "str"),
            "/V1/MANAGEMENT/LABEL.DeviceLabel": ("device_label", "str"),
            "/V1/EDID.EdidStatus": ("edid_status", "str"),
        }
        self._video_paths()
        for n in range(1, SERIALS + 1):
            self._serial_paths(n)

    def _register_children(self, kind: str, count: int) -> None:
        for n in range(1, count + 1):
            self.register_child(kind, n, {"label": f"HDMI {kind.title()} {n}"})

    def _add_paths(self, node: str, prefix: str, fields: dict) -> None:
        for prop, (key, kind) in fields.items():
            self._paths[f"{node}.{prop}"] = (prefix + key, kind)

    def _video_paths(self) -> None:
        for n in range(1, OUTPUTS + 1):
            self._add_paths(f"/V1/MEDIA/VIDEO/XP/O{n}", f"output.{n}.", VIDEO_FIELDS)
            self._add_paths(f"/V1/MEDIA/VIDEO/O{n}", f"output.{n}.", OUTPUT_FIELDS)
        for n in range(1, 5):
            self._paths[f"/V1/MEDIA/VIDEO/I{n}.SignalPresent"] = (
                f"input.{n}.signal",
                "bool",
            )
            self._paths[f"/V1/MEDIA/VIDEO/I{n}/HDCP.AllowedHdcpVersion"] = (
                f"input.{n}.hdcp_version",
                "str",
            )

    def _serial_paths(self, n: int) -> None:
        for key, prop in SERIAL_FIELDS.items():
            self._paths[f"/V1/MEDIA/SERIAL/P{n}.{prop}"] = (
                f"serial_{n}_{key}",
                "int" if key == "databits" else "str",
            )
        self._paths[f"/V1/MANAGEMENT/NETWORK/SERVICES/UART{n}.Port"] = (
            f"serial_{n}_tcp_port",
            "int",
        )
        self.set_state(
            f"serial_{n}_setup", "No downstream serial settings checked yet."
        )

    async def _post_connect(self) -> None:
        self._response_signature = None
        self._response_lines = []
        await self._get(VERSION)

    async def _initial_sync(self) -> None:
        self._standing_error = ""
        await self._find_analog()
        nodes = sorted({path.rsplit(".", 1)[0] for path in self._paths})
        for node in nodes:
            await self._request("OPEN " + node)
        await self.poll()

    async def _close_session(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(ConnectionError("The MMX2 connection closed."))
        self._response_signature = None
        self._response_lines = []

    async def _request(self, command: str) -> list[str]:
        async with self._request_lock:
            if self.transport is None:
                raise ConnectionError("The MMX2 is not connected.")
            self._signature = self._signature % 65535 + 1
            self._pending = asyncio.get_running_loop().create_future()
            try:
                await self.transport.send(
                    f"{self._signature:04X}#{command}\r\n".encode("ascii")
                )
                lines = await asyncio.wait_for(self._pending, 4)
            finally:
                self._pending.cancel()
                self._pending = None
            errors = [line for line in lines if re.match(r"^(?:nE|pE|mE|mF) ", line)]
            if errors:
                raise CommandRefusedError(
                    "MMX2 refused the request: " + "; ".join(errors)
                )
            return lines

    async def _get(self, path: str) -> str:
        lines = await self._request("GET " + path)
        for line in lines:
            match = re.fullmatch(r"(?:pr|pw) " + re.escape(path) + r"=(.*)", line)
            if match:
                return _unescape(match[1])
        raise CommandRefusedError("MMX2 returned no value for " + path)

    async def _write(self, path: str, value: Any) -> None:
        word = str(value).lower() if isinstance(value, bool) else str(value)
        await self._request(f"SET {path}={_escape(word)}")
        actual = await self._get(path)
        if path.endswith((".VolumePercent", ".VolumedB")):
            return  # The device can round a requested level; publish its actual reading.
        if actual != word:
            try:
                equal = abs(float(actual) - float(word)) < 0.01
            except ValueError:
                equal = False
            if not equal:
                raise CommandRefusedError(
                    f"MMX2 kept {path} at {actual}; requested {word}."
                )

    async def on_data_received(self, data: bytes) -> None:
        # No await: older platforms may dispatch consecutive frames concurrently.
        for line in data.decode("ascii", "replace").splitlines():
            self._receive_line(line.strip())

    def _receive_line(self, line: str) -> None:
        if re.fullmatch(r"\{[0-9A-Fa-f]{4}", line):
            self._response_signature = int(line[1:], 16)
            self._response_lines = []
            return
        if line == "}":
            self._finish_response()
            return
        if self._response_signature is not None and not line.startswith("CHG "):
            self._response_lines.append(line)
        self._read_property(line)

    def _finish_response(self) -> None:
        if self._response_signature == self._signature:
            self._resolve_pending()
        self._response_signature = None
        self._response_lines = []

    def _resolve_pending(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_result(self._response_lines[:])

    def _read_property(self, line: str) -> None:
        match = re.fullmatch(r"(?:pr|pw|CHG) ([^=]+)=(.*)", line)
        if not match or match[1] not in self._paths:
            return
        key, kind = self._paths[match[1]]
        try:
            self._publish(key, _value(match[2], kind))
        except ValueError:
            self.set_state(
                "last_error", "MMX2 returned an invalid value for " + match[1]
            )

    def _publish(self, key: str, value: Any) -> None:
        if key.startswith(("input.", "output.")):
            kind, child, prop = key.split(".", 2)
            self.set_child_state(kind, int(child), prop, value)
        else:
            self.set_state(key, value)

    async def _find_analog(self) -> None:
        self._paths = {
            p: v for p, v in self._paths.items() if not p.startswith("/V1/MEDIA/AUDIO/")
        }
        found = []
        for port in ("O2", "O4"):
            try:
                float(await self._get(f"/V1/MEDIA/AUDIO/{port}.VolumePercent"))
                found.append(port)
            except CommandRefusedError:
                continue
        self._analog = found[0] if len(found) == 1 else ""
        self.set_state("analog_port", self._analog)
        if not self._analog:
            self._standing_error = "The MMX2 did not identify one analog audio output."
            return
        for prop, info in AUDIO_FIELDS.items():
            self._paths[f"/V1/MEDIA/AUDIO/{self._analog}.{prop}"] = info
        self._paths[f"/V1/MEDIA/AUDIO/XP/{self._analog}.ConnectedSource"] = (
            "audio_source",
            "source",
        )
        self._paths[f"/V1/MEDIA/AUDIO/XP/{self._analog}.Mute"] = (
            "audio_crosspoint_mute",
            "bool",
        )

    async def poll(self) -> None:
        errors = []
        for path in self._paths:
            try:
                await self._get(path)
            except CommandRefusedError as exc:
                errors.append(str(exc))
        self.set_state("last_error", self._standing_error or "; ".join(errors[:3]))

    async def refresh_children(self) -> None:
        await self.poll()

    async def _liveness_probe(self) -> None:
        try:
            await self._get(VERSION)
        except CommandRefusedError:
            pass  # An error reply also proves the control connection is answering.

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        if command == "refresh":
            await self.poll()
            return
        if command not in WIRES:
            raise CommandRefusedError("Unknown MMX2 command: " + command)
        template = WIRES[command]
        values = self._command_values(template, params)
        wire = template.format(**values)
        await self._send_wire(wire)
        if command != "reboot":
            await self._read_after(command, values)

    def _command_values(self, template: str, params: dict | None) -> dict:
        values = {
            k: str(v).lower() if isinstance(v, bool) else v
            for k, v in (params or {}).items()
        }
        if "{analog}" in template:
            if not self._analog:
                raise CommandRefusedError(
                    "The analog audio output has not answered yet."
                )
            values["analog"] = self._analog
        if "label" in values:
            values["label"] = _escape(values["label"])
        return values

    async def _send_wire(self, wire: str) -> None:
        if wire.startswith("SET "):
            path, value = wire[4:].split("=", 1)
            await self._write(path, _unescape(value))
        else:
            await self._request(wire)

    async def _read_after(self, command: str, values: dict[str, Any]) -> None:
        for path in self._readback_paths(command, values):
            await self._get(path)

    def _readback_paths(self, command: str, values: dict) -> list[str]:
        if command == "set_video_route_all":
            return [
                f"/V1/MEDIA/VIDEO/XP/O{n}.ConnectedSource"
                for n in range(1, OUTPUTS + 1)
            ]
        if command in ("set_video_route", "disconnect_video_output"):
            return [f"/V1/MEDIA/VIDEO/XP/O{values['output']}.ConnectedSource"]
        if command in ("set_audio_route", "disconnect_audio_output"):
            return [f"/V1/MEDIA/AUDIO/XP/{self._analog}.ConnectedSource"]
        if "analog_volume" in command:
            return [
                f"/V1/MEDIA/AUDIO/{self._analog}.VolumePercent",
                f"/V1/MEDIA/AUDIO/{self._analog}.VolumedB",
            ]
        if "edid" in command:
            return ["/V1/EDID.EdidStatus"]
        return []

    async def set_device_setting(self, name: str, value: Any) -> None:
        if name == "device_label":
            await self._write("/V1/MANAGEMENT/LABEL.DeviceLabel", value)
            return
        match = re.fullmatch(r"serial_(\d+)_(baudrate|parity|stopbits)", name)
        if not match or name not in self.DRIVER_INFO["device_settings"]:
            raise CommandRefusedError("Unknown MMX2 setting: " + name)
        await self._write(
            f"/V1/MEDIA/SERIAL/P{match[1]}.{SERIAL_FIELDS[match[2]]}", value
        )

    async def prepare_bridge_port(self, port_id: str, params: dict[str, Any]) -> None:
        ports = {
            p["id"]: n for n, p in enumerate(self.DRIVER_INFO["bridge"]["ports"], 1)
        }
        if port_id not in ports:
            raise CommandRefusedError("This MMX2 has no serial port " + port_id)
        n = ports[port_id]
        async with self._serial_lock:
            try:
                desired = self._serial_values(params)
                await self._prepare_serial(n, desired)
            except (
                CommandRefusedError,
                ConnectionError,
                TimeoutError,
                ValueError,
            ) as exc:
                self.set_state(f"serial_{n}_setup", "Setup failed: " + str(exc))
                raise

    def _serial_values(self, params: dict[str, Any]) -> dict[str, str]:
        baud = str(int(params.get("baudrate", 9600)))
        parity = {"N": "None", "O": "Odd", "E": "Even"}.get(
            str(params.get("parity", "N")).upper()
        )
        stop = float(params.get("stopbits", 1))
        allowed = (
            baud in ("9600", "19200", "38400", "57600", "115200"),
            parity is not None,
            stop in (1, 2),
        )
        if not all(allowed):
            raise CommandRefusedError(
                "Use a supported baud rate, None/Odd/Even parity and one or two stop bits."
            )
        self._check_serial_format(params)
        return {"baudrate": baud, "parity": parity, "stopbits": str(int(stop))}

    def _check_serial_format(self, params: dict) -> None:
        flow_control = any(
            params.get(k, False) for k in ("xonxoff", "rtscts", "dsrdtr")
        )
        if int(params.get("bytesize", 8)) != 8 or flow_control:
            raise CommandRefusedError(
                "MMX2 requires eight data bits and no flow control."
            )

    async def _prepare_serial(self, n: int, desired: dict[str, str]) -> None:
        actual_port = int(
            await self._get(f"/V1/MANAGEMENT/NETWORK/SERVICES/UART{n}.Port")
        )
        if actual_port != 8000 + n:
            raise CommandRefusedError(
                f"RS-232 {n} reports network port {actual_port}; expected {8000 + n}."
            )
        bits = await self._get(f"/V1/MEDIA/SERIAL/P{n}.DataBits")
        if bits != "8":
            raise CommandRefusedError(
                f"RS-232 {n} reports {bits} data bits; eight are required."
            )
        for field, wanted in desired.items():
            path = f"/V1/MEDIA/SERIAL/P{n}.{SERIAL_FIELDS[field]}"
            if await self._get(path) != wanted:
                await self._write(path, wanted)
        self.set_state(
            f"serial_{n}_setup",
            f"Line settings confirmed: {desired['baudrate']}, 8, {desired['parity']}, {desired['stopbits']}. Test the downstream reply.",
        )
