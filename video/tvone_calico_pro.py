"""tvONE CALICO PRO C7-PRO-1200 / C7-PRO-2200 control.

Source: tvONE CLI document 16005.1, API 1.6.0+, firmware 1.6.0.5+.
Python supports the gated Login exchange, device-enumerated child rosters and
asynchronous !Event messages interleaved with !Done/!Failed command replies.
The published CLI provides control and native feedback on one TCP connection.
Reads reconcile the events and supply values the processor does not push.

No optimistic state: writes are read back. Detailed wall design, keyframes,
calculated edge blending and system administration remain in CALICO Studio.
RS-232 is diagnostic-only in the manufacturer document, so this driver offers
Ethernet only. A simulator is protocol-derived, not hardware qualification.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time

from openavc.core.connection_fault import ConnectionFaultError
from openavc.drivers.base import BaseDriver

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:
    CommandRefusedError = RuntimeError

from openavc.utils.logger import get_logger

log = get_logger(__name__)


class TvoneCalicoProDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "tvone_calico_pro",
        "name": "tvONE CALICO PRO",
        "manufacturer": "tvONE",
        "category": "video",
        "version": "1.0.0",
        "author": "OpenAVC",
        "transport": "tcp",
        "delimiter": "\r\n",
        "description": "Control CALICO PRO video walls with named presets, window sources, canvas audio, blanking "
        "and media playback, with live feedback and signal and system diagnostics.",
        "source_url": "https://api.tvone.com/products/c7-series/c7-pro-2200/tvONE%20CALICO%20PRO%20C7-PRO-2200%20Commands_current.pdf",
        "ports": [10001],
        "protocols": ["calico-cli"],
        "simulated": True,
        "verified": False,
        "min_platform_version": "0.36.0",
        "tags": ["video-wall", "presets", "multiviewer", "hdr"],
        "compatible_models": [
            {
                "manufacturer": "tvONE",
                "models": ["C7-PRO-1200", "C7-PRO-2200"],
                "confidence": "untested",
                "notes": "Documented for System API 1.6.0 and firmware 1.6.0.5 or later. No hardware "
                "qualification.",
            }
        ],
        "help": {
            "overview": "Control a CALICO Studio design through its named canvases, windows, inputs, outputs "
            "and saved presets. Live feedback follows source, preset, audio and signal changes.",
            "setup": "Use Ethernet port 10001 and a CALICO account. Only one CLI connection is supported. "
            "Create the wall design and media queues in CALICO Studio, then connect or choose Refresh "
            "from Device to load their names. RS-232 is diagnostic-only. Configure output mapping, "
            "edge blending, animations and firmware in CALICO Studio. Blacking an output also mutes "
            "its audio. Firmware 1.6.2.5 fixes brightness control for 8-bit and 10-bit video; "
            "use that version or later when adjusting brightness.",
            "connection": "The command-line interface is unencrypted. Use the processor on the control "
            "network. Credentials containing CLI delimiters (commas, parentheses, quotes or line "
            "breaks) cannot be represented by the documented login form.",
        },
        "discovery": {
            "tcp_probe": {
                "port": 10001,
                "expect_regex": "(?m)^//\\s*CALICO PRO\\s*-",
                "timeout_ms": 1800,
            }
        },
        "default_config": {
            "host": "",
            "port": 10001,
            "username": "admin",
            "password": "",
            "poll_interval": 15,
            "inter_command_delay": 0.02,
            "request_timeout": 5,
            "roster_refresh_interval": 120,
            "model": "C7-PRO-2200",
        },
        "config_schema": {
            "host": {"type": "string", "label": "IP Address", "required": True},
            "port": {
                "type": "integer",
                "label": "Port",
                "default": 10001,
                "min": 1,
                "max": 65535,
            },
            "username": {
                "type": "string",
                "label": "Username",
                "required": True,
                "default": "admin",
            },
            "password": {
                "type": "string",
                "label": "Password",
                "required": True,
                "secret": True,
                "default": "",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status Refresh Interval (seconds)",
                "default": 15,
                "min": 5,
                "max": 300,
                "advanced": True,
            },
            "inter_command_delay": {
                "type": "number",
                "label": "Command Spacing (seconds)",
                "default": 0.02,
                "min": 0,
                "max": 1,
                "advanced": True,
            },
            "request_timeout": {
                "type": "number",
                "label": "Reply Timeout (seconds)",
                "default": 5,
                "min": 1,
                "max": 30,
                "advanced": True,
            },
            "roster_refresh_interval": {
                "type": "integer",
                "label": "Configuration Refresh Interval (seconds)",
                "default": 120,
                "min": 30,
                "max": 3600,
                "advanced": True,
            },
            "model": {
                "type": "enum",
                "label": "Model",
                "values": ["C7-PRO-1200", "C7-PRO-2200"],
                "default": "C7-PRO-2200",
                "learned_from": "model",
                "help": "Sets the simulated model. A connected processor reports and saves its "
                "actual model.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "firmware": {"type": "string", "label": "Firmware"},
            "api_version": {"type": "string", "label": "API Version"},
            "unit_name": {"type": "string", "label": "Device Name"},
            "status": {"type": "string", "label": "System Status"},
            "alerts": {
                "type": "string",
                "label": "System Alerts",
                "cloud_priority": "high",
            },
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "cloud_priority": "high",
            },
            "input_options": {
                "type": "string",
                "label": "Source Choices",
                "cloud_priority": "low",
            },
            "media_input_options": {
                "type": "string",
                "label": "Media Input Choices",
                "cloud_priority": "low",
            },
            "feedback_subscriptions": {
                "type": "string",
                "label": "Live Feedback Categories",
                "cloud_priority": "low",
            },
            "lock_mode": {"type": "string", "label": "Synchronization Mode"},
            "lock_source": {"type": "string", "label": "Synchronization Source"},
            "lock_status": {"type": "string", "label": "Synchronization Status"},
            "lock_accuracy": {"type": "integer", "label": "Synchronization Accuracy"},
            "backplane_temperature": {
                "type": "float",
                "label": "Backplane Temperature",
                "unit": "°C",
                "cloud_priority": "low",
            },
            "fpga_temperature": {
                "type": "float",
                "label": "FPGA Temperature",
                "unit": "°C",
                "cloud_priority": "low",
            },
            "fan_0_rpm": {
                "type": "integer",
                "label": "Chassis Fan 0",
                "unit": "RPM",
                "cloud_priority": "low",
            },
            "fan_1_rpm": {
                "type": "integer",
                "label": "Chassis Fan 1",
                "unit": "RPM",
                "cloud_priority": "low",
            },
            "fan_2_rpm": {
                "type": "integer",
                "label": "Chassis Fan 2",
                "unit": "RPM",
                "cloud_priority": "low",
            },
            "psu_1_temperature": {
                "type": "float",
                "label": "Power Supply 1 Temperature",
                "unit": "°C",
                "cloud_priority": "low",
            },
            "psu_2_temperature": {
                "type": "float",
                "label": "Power Supply 2 Temperature",
                "unit": "°C",
                "cloud_priority": "low",
            },
        },
        "child_entity_types": {
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "string", "max_length": 24},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "status": {"type": "string", "label": "Status"},
                    "signal_present": {"type": "boolean", "label": "Signal Present"},
                    "resolution": {"type": "string", "label": "Detected Resolution"},
                    "hdcp_status": {"type": "string", "label": "HDCP Status"},
                    "hdcp_required": {"type": "string", "label": "HDCP Required"},
                    "audio_status": {"type": "string", "label": "Audio Signal"},
                    "colour_range": {"type": "string", "label": "Active Color Range"},
                    "blackout": {
                        "type": "boolean",
                        "label": "Blackout",
                        "control": True,
                    },
                    "brightness": {
                        "type": "integer",
                        "label": "Brightness",
                        "control": True,
                        "min": -30,
                        "max": 30,
                        "step": 1,
                    },
                    "contrast": {
                        "type": "integer",
                        "label": "Contrast",
                        "control": True,
                        "min": 30,
                        "max": 130,
                        "step": 1,
                        "unit": "%",
                    },
                },
                "summary_fields": ["signal_present", "resolution", "hdcp_status"],
                "dynamic": True,
            },
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "string", "max_length": 24},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "status": {"type": "string", "label": "Status"},
                    "resolution": {"type": "string", "label": "Output Resolution"},
                    "layout": {"type": "integer", "label": "Layout"},
                    "hdcp_status": {"type": "string", "label": "HDCP Status"},
                    "hdcp_active": {"type": "string", "label": "HDCP Active"},
                    "sink_present": {"type": "boolean", "label": "Display Connected"},
                    "lock_status": {"type": "string", "label": "Frame Lock"},
                    "blackout": {
                        "type": "boolean",
                        "label": "Blackout",
                        "control": True,
                        "help": "Blacks the output and mutes its audio.",
                    },
                    "audio_mute": {
                        "type": "boolean",
                        "label": "Audio Mute",
                        "control": True,
                    },
                    "audio_enabled": {
                        "type": "boolean",
                        "label": "Embedded Audio Enabled",
                        "control": True,
                    },
                    "eco_mode": {
                        "type": "boolean",
                        "label": "Display Standby",
                        "control": True,
                        "help": "Allows the attached display to "
                        "enter standby. Does not persist "
                        "after a power cycle.",
                    },
                },
                "summary_fields": ["resolution", "blackout", "audio_mute"],
                "dynamic": True,
            },
            "window": {
                "label": "Window",
                "label_plural": "Windows",
                "id_format": {"type": "integer", "min": 1},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "input": {"type": "string", "label": "Source", "control": True},
                    "canvas": {"type": "integer", "label": "Canvas"},
                    "muted": {
                        "type": "boolean",
                        "label": "Hide Window",
                        "control": True,
                    },
                    "width": {
                        "type": "integer",
                        "label": "Width",
                        "control": True,
                        "min": 0,
                        "max": 16383,
                        "step": 1,
                    },
                    "height": {
                        "type": "integer",
                        "label": "Height",
                        "control": True,
                        "min": 0,
                        "max": 16383,
                        "step": 1,
                    },
                    "center_x": {
                        "type": "integer",
                        "label": "Center X",
                        "control": True,
                        "min": -8192,
                        "max": 8191,
                        "step": 1,
                    },
                    "center_y": {
                        "type": "integer",
                        "label": "Center Y",
                        "control": True,
                        "min": -8192,
                        "max": 8191,
                        "step": 1,
                    },
                    "z_order": {
                        "type": "integer",
                        "label": "Layer",
                        "control": True,
                        "min": 0,
                        "max": 15,
                        "step": 1,
                    },
                    "rotation": {
                        "type": "float",
                        "label": "Rotation",
                        "control": True,
                        "step": 0.01,
                        "unit": "degrees",
                    },
                    "fade_to_black": {
                        "type": "float",
                        "label": "Fade to Black",
                        "control": True,
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "unit": "%",
                        "help": "0% is full brightness; 100% is black.",
                    },
                    "horizontal_flip": {
                        "type": "boolean",
                        "label": "Horizontal Flip",
                        "control": True,
                    },
                    "vertical_flip": {
                        "type": "boolean",
                        "label": "Vertical Flip",
                        "control": True,
                    },
                    "fade_through_black": {
                        "type": "boolean",
                        "label": "Fade Through Black",
                        "control": True,
                    },
                    "crossfade": {
                        "type": "boolean",
                        "label": "Crossfade",
                        "control": True,
                    },
                },
                "summary_fields": ["input", "canvas", "muted"],
                "dynamic": True,
            },
            "canvas": {
                "label": "Canvas",
                "label_plural": "Canvases",
                "id_format": {"type": "integer", "min": 1},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "current_preset": {
                        "type": "integer",
                        "label": "Current Preset",
                        "control": True,
                    },
                    "current_preset_name": {
                        "type": "string",
                        "label": "Current Preset Name",
                    },
                    "windows": {"type": "string", "label": "Windows"},
                    "layouts": {"type": "string", "label": "Layouts"},
                    "audio_source": {"type": "string", "label": "Fixed Audio Source"},
                    "audio_mode": {"type": "string", "label": "Audio Mode"},
                    "audio_follow_window": {
                        "type": "integer",
                        "label": "Audio Follow Window",
                    },
                    "audio_volume": {
                        "type": "integer",
                        "label": "Volume",
                        "control": True,
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "unit": "%",
                    },
                    "audio_mute": {
                        "type": "boolean",
                        "label": "Audio Mute",
                        "control": True,
                    },
                    "blackout": {
                        "type": "boolean",
                        "label": "Blackout",
                        "control": True,
                    },
                },
                "summary_fields": ["current_preset_name", "audio_volume", "blackout"],
            },
            "layout": {
                "label": "Layout",
                "label_plural": "Layouts",
                "id_format": {"type": "integer", "min": 1},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "canvas": {"type": "integer", "label": "Canvas"},
                    "outputs": {"type": "string", "label": "Outputs"},
                    "frame_rate": {"type": "string", "label": "Frame Rate"},
                    "locked": {"type": "boolean", "label": "Lock Enabled"},
                    "animating": {
                        "type": "boolean",
                        "label": "Preset Transition Active",
                    },
                },
                "summary_fields": ["canvas", "frame_rate", "locked"],
            },
            "preset": {
                "label": "Preset",
                "label_plural": "Presets",
                "id_format": {"type": "integer", "min": 1},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "canvas": {"type": "integer", "label": "Canvas"},
                    "duration_ms": {
                        "type": "integer",
                        "label": "Transition Time",
                        "min": 0,
                        "unit": "ms",
                    },
                    "current": {"type": "boolean", "label": "Current", "control": True},
                },
                "summary_fields": ["canvas", "current"],
            },
            "module": {
                "label": "Module",
                "label_plural": "Modules",
                "id_format": {"type": "integer", "min": 1},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "card_type": {"type": "string", "label": "Module Type"},
                    "status": {"type": "string", "label": "Status"},
                    "temperature_alert": {
                        "type": "string",
                        "label": "Temperature Alert",
                    },
                    "temperature": {
                        "type": "float",
                        "label": "Core Temperature",
                        "unit": "°C",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["card_type", "status"],
            },
        },
        "device_settings": {
            "unit_name": {
                "type": "string",
                "label": "Device Name",
                "state_key": "unit_name",
                "regex": '^[^"\\r\\n]{0,32}$',
                "help": "Name shown by CALICO. Saved to the processor and read back.",
            }
        },
        "commands": {
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Read the current configuration, names, signals and health from CALICO.",
            },
            "recall_preset": {
                "label": "Recall Preset",
                "params": {
                    "preset": {
                        "type": "child_id",
                        "child_type": "preset",
                        "required": True,
                        "label": "Preset",
                    }
                },
                "help": "Recall a saved CALICO preset on its assigned canvas.",
            },
            "set_window_input": {
                "label": "Route Window Source",
                "params": {
                    "window": {
                        "type": "child_id",
                        "child_type": "window",
                        "required": True,
                        "label": "Window",
                    },
                    "input": {
                        "type": "string",
                        "label": "Source",
                        "required": True,
                        "options_state": "input_options",
                    },
                },
                "help": "Choose a named input for an existing window.",
                "sets": {"input": "{input}"},
            },
            "set_window_canvas": {
                "label": "Assign Window to Canvas",
                "params": {
                    "window": {
                        "type": "child_id",
                        "child_type": "window",
                        "required": True,
                        "label": "Window",
                    },
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                },
                "help": "Move an existing window to another canvas.",
            },
            "set_canvas_volume": {
                "label": "Set Canvas Volume",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                    "level": {
                        "type": "integer",
                        "label": "Volume",
                        "required": True,
                        "min": 0,
                        "max": 100,
                        "unit": "%",
                    },
                },
                "help": "Set the canvas audio level from 0 to 100%.",
                "sets": {"audio_volume": "{level}"},
            },
            "set_canvas_mute": {
                "label": "Set Canvas Audio Mute",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                    "muted": {"type": "boolean", "label": "Muted", "required": True},
                },
                "help": "Mute or unmute audio on a canvas.",
                "sets": {"audio_mute": "{muted}"},
            },
            "set_canvas_blackout": {
                "label": "Set Canvas Blackout",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                    "enabled": {
                        "type": "boolean",
                        "label": "Blackout",
                        "required": True,
                    },
                },
                "help": "Black or restore every output and output map on a canvas.",
                "sets": {"blackout": "{enabled}"},
            },
            "set_canvas_audio_input": {
                "label": "Route Canvas Audio",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                    "input": {
                        "type": "string",
                        "label": "Source",
                        "required": True,
                        "options_state": "input_options",
                    },
                },
                "help": "Select a fixed audio source and switch the canvas to From "
                "Source mode.",
                "sets": {"audio_source": "{input}", "audio_mode": "FromSource"},
            },
            "set_canvas_audio_window": {
                "label": "Follow Window Audio",
                "params": {
                    "canvas": {
                        "type": "child_id",
                        "child_type": "canvas",
                        "required": True,
                        "label": "Canvas",
                    },
                    "window": {
                        "type": "child_id",
                        "child_type": "window",
                        "required": True,
                        "label": "Window",
                    },
                },
                "help": "Follow the audio of a window assigned to this canvas.",
            },
            "save_configuration": {
                "label": "Save Configuration to Storage",
                "params": {},
                "help": "Save the current system, routing and resource settings for the "
                "next power-on.",
                "confirm": "Replace the saved configuration with the current CALICO "
                "settings?",
            },
            "adjust_window": {
                "label": "Adjust Window",
                "params": {
                    "window": {
                        "type": "child_id",
                        "child_type": "window",
                        "required": True,
                        "label": "Window",
                    },
                    "control": {
                        "type": "string",
                        "label": "Control",
                        "required": True,
                        "options_from": {
                            "param": "window",
                            "source": "child_schema",
                            "types": ["boolean", "integer", "float"],
                        },
                    },
                    "value": {
                        "type": "string",
                        "label": "Value",
                        "required": True,
                        "type_from": {"param": "control"},
                    },
                },
                "help": "Change a supported window control. The available controls and "
                "ranges follow the selected window. tvONE lists incorrect window "
                "placement after resizing as an outstanding issue in firmware "
                "1.6.2.5. Verify geometry in CALICO Studio; use saved presets for "
                "repeatable layouts.",
            },
            "adjust_input": {
                "label": "Adjust Input",
                "params": {
                    "input": {
                        "type": "child_id",
                        "child_type": "input",
                        "required": True,
                        "label": "Input",
                    },
                    "control": {
                        "type": "string",
                        "label": "Control",
                        "required": True,
                        "options_from": {
                            "param": "input",
                            "source": "child_schema",
                            "types": ["boolean", "integer", "float"],
                        },
                    },
                    "value": {
                        "type": "string",
                        "label": "Value",
                        "required": True,
                        "type_from": {"param": "control"},
                    },
                },
                "help": "Change a supported input control. The available controls and ranges "
                "follow the selected input.",
            },
            "adjust_output": {
                "label": "Adjust Output",
                "params": {
                    "output": {
                        "type": "child_id",
                        "child_type": "output",
                        "required": True,
                        "label": "Output",
                    },
                    "control": {
                        "type": "string",
                        "label": "Control",
                        "required": True,
                        "options_from": {
                            "param": "output",
                            "source": "child_schema",
                            "types": ["boolean", "integer", "float"],
                        },
                    },
                    "value": {
                        "type": "string",
                        "label": "Value",
                        "required": True,
                        "type_from": {"param": "control"},
                    },
                },
                "help": "Change a supported output control. The available controls and "
                "ranges follow the selected output.",
            },
            "media_play": {
                "label": "Play Media",
                "params": {
                    "input": {
                        "type": "string",
                        "label": "Media Input",
                        "required": True,
                        "options_state": "media_input_options",
                    }
                },
                "help": "Start or resume the configured play queue.",
            },
            "media_pause": {
                "label": "Pause Media",
                "params": {
                    "input": {
                        "type": "string",
                        "label": "Media Input",
                        "required": True,
                        "options_state": "media_input_options",
                    }
                },
                "help": "Pause the configured play queue.",
            },
            "media_stop": {
                "label": "Stop Media",
                "params": {
                    "input": {
                        "type": "string",
                        "label": "Media Input",
                        "required": True,
                        "options_state": "media_input_options",
                    }
                },
                "help": "Stop playback and return to the first queue item.",
            },
            "media_next": {
                "label": "Next Media Item",
                "params": {
                    "input": {
                        "type": "string",
                        "label": "Media Input",
                        "required": True,
                        "options_state": "media_input_options",
                    }
                },
                "help": "Advance to the next queue item.",
            },
            "media_previous": {
                "label": "Previous Media Item",
                "params": {
                    "input": {
                        "type": "string",
                        "label": "Media Input",
                        "required": True,
                        "options_state": "media_input_options",
                    }
                },
                "help": "Go to the previous queue item; CALICO can refuse this at the "
                "beginning.",
            },
            "set_media_repeat": {
                "label": "Set Media Repeat",
                "params": {
                    "input": {
                        "type": "string",
                        "label": "Media Input",
                        "required": True,
                        "options_state": "media_input_options",
                    },
                    "repeat": {"type": "boolean", "label": "Repeat", "required": True},
                },
                "help": "Choose whether the configured queue plays once or loops.",
            },
        },
        "quick_actions": [
            "recall_preset",
            "set_window_input",
            "set_canvas_blackout",
            "set_canvas_volume",
        ],
        "routing": {
            "destination_child_type": "window",
            "source_child_type": "input",
            "command": "set_window_input",
            "destination_param": "window",
            "source_param": "input",
            "planes": [{"label": "Window Video", "route_property": "input"}],
        },
    }

    HEALTH_INTERVAL_S = 20
    HEALTH_TIMEOUT_S = 8
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "CALICO stopped answering status queries."

    def __init__(self, device_id, config, state, events):
        self._request_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._waiter = None
        self._pending = ""
        self._lines = []
        self._cache = {}
        self._paths = {kind: {} for kind in self.DRIVER_INFO["child_entity_types"]}
        self._schemas = {}
        self._media_inputs = set()
        self._authenticated = False
        self._syncing = False
        self._event_task = None
        self._dirty_live = False
        self._dirty_roster = False
        self._last_roster = 0.0
        self._last_send = 0.0
        super().__init__(device_id, config, state, events)

    def _transport_kwargs(self, transport_type, kwargs):
        kwargs["delimiter"] = b"\n"
        return kwargs

    async def _pre_connect(self):
        self._authenticated = False
        self._cache.clear()
        for key in ("username", "password"):
            text = str(self.config.get(key, ""))
            if not text:
                raise ConnectionFaultError(
                    f"CALICO {key} is missing.", code="auth_failed"
                )
            if any(char in text for char in ',()"\r\n'):
                raise ConnectionFaultError(
                    f"The CALICO {key} contains a delimiter unsupported by its documented CLI login.",
                    code="invalid_config",
                )

    async def _post_connect(self):
        await self._login()
        await self._verify_identity()
        for category in _EVENT_CATEGORIES:
            await self._request(f"AddEvents({category})")
        self.set_state("feedback_subscriptions", ", ".join(_EVENT_CATEGORIES))

    async def _login(self):
        user = self.config["username"]
        password = self.config["password"]
        try:
            lines = await self._request(f"Login({user},{password})", login=True)
        except CommandRefusedError as exc:
            raise ConnectionFaultError(
                "CALICO refused the login.", code="auth_failed"
            ) from exc
        if not any(
            re.match(r"!Info\s*:\s*User .+ Logged In", line, re.I) for line in lines
        ):
            raise ConnectionFaultError(
                "CALICO did not confirm the login.", code="auth_failed"
            )
        self._authenticated = True

    async def _verify_identity(self):
        await self._read_object("Device")
        if self.get_state("model") not in ("C7-PRO-1200", "C7-PRO-2200"):
            raise ConnectionFaultError(
                "The connected device is not a supported CALICO PRO model.",
                code="invalid_config",
            )
        await self._request("System.APIVersion")
        version = tuple(
            int(n) for n in re.findall(r"\d+", str(self.get_state("api_version")))[:3]
        )
        if version < (1, 6, 0):
            raise ConnectionFaultError(
                "CALICO System API 1.6.0 or later is required.", code="invalid_config"
            )

    async def _initial_sync(self):
        await self._refresh_all()

    async def _close_session(self):
        self._authenticated = False
        task, self._event_task = self._event_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        waiter, self._waiter = self._waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_exception(ConnectionError("CALICO connection closed."))

    async def _request(self, command, *, login=False):
        async with self._request_lock:
            if not self.transport or not self.transport.connected:
                raise ConnectionError("CALICO is not connected.")
            gap = float(self.config.get("inter_command_delay", 0.02))
            await asyncio.sleep(max(0, gap - (time.monotonic() - self._last_send)))
            ok, lines = await self._exchange(command)
            if not ok:
                message = "CALICO refused the login." if login else self._refusal(lines)
                self.set_state("last_error", message)
                raise CommandRefusedError(message)
            return lines

    async def _exchange(self, command):
        future = asyncio.get_running_loop().create_future()
        self._waiter, self._pending, self._lines = future, command, []
        try:
            await self.transport.send((command + "\r\n").encode("utf-8"))
            self._last_send = time.monotonic()
            ok = await asyncio.wait_for(
                future, float(self.config.get("request_timeout", 5))
            )
            return ok, list(self._lines)
        except asyncio.TimeoutError as exc:
            # A late terminal must never complete the next command. End this
            # session before allowing another exchange onto its byte stream.
            self._stash_fault("no_response", self.HEALTH_FAULT_MESSAGE)
            self._handle_transport_disconnect()
            raise ConnectionFaultError(
                self.HEALTH_FAULT_MESSAGE, code="no_response"
            ) from exc
        except asyncio.CancelledError:
            if self._authenticated:
                self._handle_transport_disconnect()
            raise
        finally:
            if self._waiter is future:
                self._waiter = None

    def _refusal(self, lines):
        errors = [line for line in lines if line.lower().startswith("!error")]
        message = " ".join(errors)[:300] or "CALICO refused the command."
        for key in ("username", "password"):
            secret = str(self.config.get(key, ""))
            if secret:
                message = message.replace(secret, "[hidden]")
        return message

    async def on_data_received(self, data):
        # Apply each frame before yielding: the transport delivers frames in
        # separate tasks, and a paused handler can reverse their order.
        line = data.decode("utf-8", errors="replace").strip()
        if not line:
            return
        if line.lower().startswith("!event "):
            self._event(line[7:].strip())
            return
        if self._session_ended(line):
            self._authenticated = False
            self._handle_transport_disconnect()
            return
        match = re.fullmatch(r"([\w.]+)\s*=\s*(.*)", line)
        if match:
            self._property(match[1], match[2])
        self._receive_reply(line)

    def _session_ended(self, line):
        if not self._authenticated:
            return False
        return "please login" in line.lower() or "logged out" in line.lower()

    def _receive_reply(self, line):
        waiter = self._waiter
        if waiter is None or waiter.done():
            return
        self._lines.append(line)
        terminal = re.match(r"!(Done|Failed)\s+(.*)", line, re.I)
        if terminal and _same_echo(terminal[2], self._pending):
            waiter.set_result(terminal[1].lower() == "done")

    def _property(self, path, value):
        key = _normal_path(path)
        self._cache[key] = value
        if key in _PARENT_FIELDS:
            name, convert = _PARENT_FIELDS[key]
            result = _convert(convert, value)
            if result is not _BAD:
                self.set_state(name, result)
            return
        parsed = _object_key(key)
        if parsed is None:
            return
        kind, local_id, prop = parsed
        if local_id not in self._paths[kind]:
            return
        self._apply_fields(kind, local_id, {prop: value})

    def _apply_fields(self, kind, local_id, properties):
        updates = self._field_updates(kind, local_id, properties)
        if not updates:
            return
        if kind == "module" and "status" in updates:
            fault = updates["status"].upper() in {
                "BOOTFAILED",
                "UPDATEFAILED",
                "CARDFAILED",
            }
            health = (
                self.child_fault(
                    "service_fault", "The CALICO media module reports a failure."
                )
                if fault
                else self.child_fault()
            )
            updates.update(health)
        self.set_child_state_batch(kind, local_id, updates)
        if kind == "canvas" and "current_preset" in updates:
            self._current_preset(local_id, updates["current_preset"])

    def _field_updates(self, kind, local_id, properties):
        updates = {}
        schema = self._schemas.get(
            (kind, local_id),
            self.DRIVER_INFO["child_entity_types"][kind]["state_variables"],
        )
        for prop, value in properties.items():
            field = _FIELDS[kind].get(prop.lower())
            if field is None or field[0] not in schema:
                continue
            result = _convert(field[1], value)
            if result is not _BAD:
                updates[field[0]] = result
        if updates.get("name") == "":
            updates["name"] = self._paths[kind][local_id]
        return updates

    def _current_preset(self, canvas, preset):
        name = ""
        for number in self._paths["preset"]:
            if self.get_child_state("preset", number).get("canvas") == canvas:
                self.set_child_state("preset", number, "current", number == preset)
            if number == preset:
                name = (
                    self.get_child_state("preset", number).get("name")
                    or f"Preset {number}"
                )
        self.set_child_state(
            "canvas", canvas, "current_preset_name", name if preset else ""
        )

    async def _read_object(self, path):
        prefix = _normal_path(path) + "."
        for key in [key for key in self._cache if key.startswith(prefix)]:
            del self._cache[key]
        lines = await self._request(path)
        result = {}
        raw_prefix = re.sub(r"^routing\.", "", path.lower()) + "."
        for line in lines:
            match = re.fullmatch(r"([\w.]+)\s*=\s*(.*)", line)
            if match is None:
                continue
            raw = re.sub(r"^routing\.", "", match[1].lower())
            normalized = _normal_path(match[1])
            if raw.startswith(raw_prefix):
                result[raw[len(raw_prefix) :]] = match[2]
            elif normalized.startswith(prefix):
                result[normalized[len(prefix) :]] = match[2]
        return result

    def _register(self, kind, local_id, path, properties, *, extra_schema=None):
        declared = self.DRIVER_INFO["child_entity_types"][kind]
        schema = self._child_schema(kind, properties, extra_schema)
        if declared.get("dynamic") and self._schemas.get((kind, local_id)) != schema:
            self.deregister_child(kind, local_id)
        self._schemas[kind, local_id] = schema
        kwargs = {"schema": schema} if declared.get("dynamic") else {}
        self.register_child(kind, local_id, initial_state={"name": path}, **kwargs)
        self._paths[kind][local_id] = path
        self.set_child_state(kind, local_id, "online", True)
        self._apply_fields(kind, local_id, properties)

    def _child_schema(self, kind, properties, extra_schema):
        declared = self.DRIVER_INFO["child_entity_types"][kind]
        schema = dict(declared["state_variables"])
        schema.update(extra_schema or {})
        if declared.get("dynamic"):
            present = {
                entry[0] for prop, entry in _FIELDS[kind].items() if prop in properties
            }
            schema = {
                key: entry
                for key, entry in schema.items()
                if key == "name" or key in present
            }
        return schema

    def _prune(self, kind, wanted):
        for local_id in set(self._paths[kind]) - set(wanted):
            self.deregister_child(kind, local_id)
            del self._paths[kind][local_id]
            self._schemas.pop((kind, local_id), None)

    async def _enumerate_slots(self):
        slots = await self._read_object("Slots")
        found = {"module": set(), "input": set(), "output": set()}
        media = set()
        for key, value in slots.items():
            match = re.fullmatch(r"slot(\d+)", key)
            if match is None or "NO CARD" in value.upper():
                continue
            slot = int(match[1])
            path = f"Slot{slot}"
            props = await self._read_object(path)
            self._register("module", slot, path, props)
            found["module"].add(slot)
            await self._enumerate_ports(slot, props, found, media)
        for kind, ids in found.items():
            self._prune(kind, ids)
        self._media_inputs = media
        self._publish_source_choices()

    async def _enumerate_ports(self, slot, props, found, media):
        for name in props:
            match = re.fullmatch(r"(in|out)(\d+)", name)
            if match is None:
                continue
            kind = "input" if match[1] == "in" else "output"
            local_id = f"s{slot}{'i' if kind == 'input' else 'o'}{int(match[2])}"
            path = f"Slot{slot}.{match[1].title()}{int(match[2])}"
            values = await self._read_object(path)
            extra = {}
            if kind == "input" and "activequeue" in values:
                queue = await self._read_object(path + ".ActiveQueue")
                values.update({"activequeue." + key: val for key, val in queue.items()})
                extra = _MEDIA_SCHEMA
                media.add(local_id)
            self._register(kind, local_id, path, values, extra_schema=extra)
            found[kind].add(local_id)

    async def _enumerate_presets(self):
        lines = await self._request("Preset.PresetList()")
        found = set()
        for line in lines:
            match = re.match(
                r"(?:Routing\.)?Preset\.PresetList\[(\d+)\]\s*=\s*(.*)", line, re.I
            )
            if match is None:
                continue
            parts = match[2].rsplit(",", 2)
            if len(parts) != 3:
                continue
            number = int(match[1])
            duration = _convert(int, parts[2])
            if duration is _BAD:
                continue
            props = {"name": parts[0], "canvas": parts[1], "duration": str(duration)}
            self._register("preset", number, f"Stbds.Stbd{number}", props)
            found.add(number)
        self._prune("preset", found)

    async def _enumerate_numbered(self, kind, root, prefix):
        properties = await self._read_object(root)
        found = set()
        for key in properties:
            match = re.fullmatch(prefix.lower() + r"(\d+)", key)
            if match is None:
                continue
            number = int(match[1])
            path = f"{prefix}{number}"
            values = await self._read_object(path)
            self._register(kind, number, path, values)
            found.add(number)
        self._prune(kind, found)

    async def _sync_windows(self):
        windows = set()
        for number, path in list(self._paths["canvas"].items()):
            values = await self._read_object(path)
            self._apply_fields("canvas", number, values)
            windows.update(
                int(n)
                for n in re.findall(
                    r"Window(\d+)\b", values.get("windowlist", ""), re.I
                )
            )
        for number in sorted(windows):
            path = f"Window{number}"
            values = await self._read_object(path)
            self._register("window", number, path, values)
        self._prune("window", windows)

    def _publish_source_choices(self):
        for state_key, ids in (
            ("input_options", self._paths["input"]),
            ("media_input_options", self._media_inputs),
        ):
            choices = [
                {
                    "value": item,
                    "label": self.get_child_state("input", item).get("name") or item,
                }
                for item in sorted(ids)
            ]
            self.set_state(state_key, json.dumps(choices, ensure_ascii=False))

    async def _refresh_all(self):
        async with self._sync_lock:
            self._syncing = True
            try:
                await self._enumerate_slots()
                await self._enumerate_presets()
                await self._enumerate_numbered("canvas", "Routing.Canvases", "Canvas")
                await self._enumerate_numbered("layout", "Routing.Layouts", "Layout")
                await self._sync_windows()
                await self._read_health()
                self._last_roster = time.monotonic()
                self._dirty_roster = self._dirty_live = False
            finally:
                self._syncing = False

    async def _refresh_live(self):
        async with self._sync_lock:
            self._syncing = True
            self._dirty_live = False
            try:
                await self._sync_windows()
                for kind in ("input", "output", "layout"):
                    for local_id, path in list(self._paths[kind].items()):
                        values = await self._read_object(path)
                        self._apply_fields(kind, local_id, values)
                for local_id in self._media_inputs:
                    await self._read_object(
                        self._paths["input"][local_id] + ".ActiveQueue"
                    )
                await self._read_health()
                self._publish_source_choices()
            finally:
                self._syncing = False

    async def _read_health(self):
        for path in (
            "System.Status",
            "System.Alerts",
            "System.UnitDescription",
            "System.Sensors",
            "System.TemperatureControl",
            "Routing.Layouts.OutputsSync",
        ):
            await self._request(path)
        alerts = self.get_state("alerts")
        self.set_state(
            "last_error",
            f"CALICO system alert: {alerts}" if alerts and alerts != "OK" else "",
        )

    async def poll(self):
        interval = float(self.config.get("roster_refresh_interval", 120))
        if self._dirty_roster or time.monotonic() - self._last_roster >= interval:
            await self._refresh_all()
        else:
            await self._refresh_live()

    async def _liveness_probe(self):
        try:
            await self._request("Device.ModelNumber")
        except CommandRefusedError:
            pass  # An explicit refusal still proves the processor answered.

    async def refresh_children(self):
        await self._refresh_all()
        return {kind: len(paths) for kind, paths in self._paths.items()}

    def _event(self, body):
        parts = [part.strip() for part in body.split(",")]
        if len(parts) < 2:
            return
        category, event = parts[:2]
        key = category.upper(), event.upper()
        if key in _VALUE_EVENTS:
            self._event_value(_VALUE_EVENTS[key], parts[2:])
        handler = _EVENT_HANDLERS.get(key)
        if handler:
            getattr(self, handler)(parts[2:])
        if key in _LIVE_EVENTS:
            self._dirty_live = True
        if key in _ROSTER_EVENTS:
            self._dirty_roster = True
        self._schedule_event_refresh()

    def _event_property(self, args):
        if len(args) >= 3:
            self._property(args[0] + "." + args[1], ",".join(args[2:]))

    def _event_sync(self, args):
        if len(args) >= 2:
            self._property("Routing.Layouts.OutputsSync." + args[0], args[1])

    def _event_value(self, prop, args):
        if len(args) >= 2:
            self._property(args[0] + "." + prop, args[1])

    def _event_media(self, args):
        if len(args) >= 3:
            self._property(args[0] + ".ActiveQueue.Status", args[1])
            self._property(args[0] + ".ActiveQueue.CurrentIndex", args[2])

    def _event_temperature(self, args):
        # Temperature changes carry slot,value; alerts carry slot,status,value.
        if len(args) not in (2, 3):
            return
        self._property(args[0] + ".CoreTemperature", args[-1])
        if len(args) == 3:
            self._property(args[0] + ".TemperatureAlert", args[1])

    def _schedule_event_refresh(self):
        if self._syncing or not self.connected:
            return
        if not (self._dirty_live or self._dirty_roster):
            return
        if self._event_task is None or self._event_task.done():
            # One owned, coalesced follow-up. _close_session cancels and awaits it.
            self._event_task = asyncio.ensure_future(self._refresh_event())

    async def _refresh_event(self):
        try:
            await asyncio.sleep(0.15)
            if self._dirty_roster:
                await self._refresh_all()
            elif self._dirty_live:
                await self._refresh_live()
        except (ConnectionFaultError, OSError, CommandRefusedError) as exc:
            log.warning("[%s] Event refresh failed: %s", self.device_id, exc)
        finally:
            self._event_task = None

    def _target(self, kind, value):
        local_id = str(value) if kind in ("input", "output") else int(value)
        if local_id not in self._paths[kind]:
            raise CommandRefusedError(
                f"The selected {kind} is no longer in the CALICO configuration."
            )
        return local_id, self._paths[kind][local_id]

    async def _write(self, path, value):
        await self._request(f"{path} = {value}")
        self._cache.pop(_normal_path(path), None)
        await self._request(path)
        actual = self._cache.get(_normal_path(path))
        if actual is not None and path.lower().endswith(".audiofollowwindow"):
            actual = str(_convert(_number, actual))
        if actual is None or not _wire_equal(actual, str(value)):
            raise CommandRefusedError(
                "CALICO did not report the requested setting after the command."
            )

    async def send_command(self, command, params=None):
        values = params or {}
        async with self._operation_lock:
            await self._dispatch_command(command, values)

    async def _dispatch_command(self, command, values):
        if command in _PROPERTY_COMMANDS:
            await self._property_command(command, values)
        elif command in ("adjust_window", "adjust_input", "adjust_output"):
            await self._adjust(command[7:], values)
        elif command in _MEDIA_METHODS:
            await self._media_command(command, values)
        elif command in _COMMAND_HANDLERS:
            await getattr(self, _COMMAND_HANDLERS[command])(values)
        else:
            raise ValueError(f"Unknown CALICO command: {command}")

    async def _property_command(self, command, params):
        kind, prop, parameter, convert = _PROPERTY_COMMANDS[command]
        _, path = self._target(kind, params[kind])
        declaration = self.DRIVER_INFO["commands"][command]["params"][parameter]
        value = convert(_checked_value(params[parameter], declaration))
        await self._write(path + "." + prop, value)

    async def _adjust(self, kind, params):
        local_id, path = self._target(kind, params[kind])
        control = str(params["control"])
        declaration = self._schemas[kind, local_id].get(control)
        if not declaration or control not in _CONTROLS[kind]:
            raise CommandRefusedError(
                "That control is not available on the selected CALICO object."
            )
        if not declaration.get("control"):
            raise CommandRefusedError(
                "That control is not available on the selected CALICO object."
            )
        value = _checked_value(params["value"], declaration)
        prop, convert = _CONTROLS[kind][control]
        await self._write(path + "." + prop, convert(value))

    async def _command_refresh(self, params):
        await self._refresh_all()

    async def _recall(self, params):
        _, path = self._target("preset", params["preset"])
        await self._request(path + ".Take()")
        await self._refresh_live()

    async def _route_window(self, params):
        _, window = self._target("window", params["window"])
        _, source = self._target("input", params["input"])
        await self._write(window + ".Input", source)

    async def _window_canvas(self, params):
        _, window = self._target("window", params["window"])
        _, canvas = self._target("canvas", params["canvas"])
        await self._write(window + ".Canvas", canvas)
        await self._refresh_live()

    async def _canvas_input(self, params):
        _, canvas = self._target("canvas", params["canvas"])
        _, source = self._target("input", params["input"])
        await self._write(canvas + ".AudioSource", source)
        await self._write(canvas + ".AudioMode", "FromSource")

    async def _canvas_window(self, params):
        canvas_id, canvas = self._target("canvas", params["canvas"])
        window_id, _ = self._target("window", params["window"])
        if self.get_child_state("window", window_id).get("canvas") != canvas_id:
            raise CommandRefusedError(
                "The audio-follow window must belong to the selected canvas."
            )
        await self._write(canvas + ".AudioFollowWindow", window_id)
        await self._write(canvas + ".AudioMode", "FollowWindow")

    def _media_target(self, params):
        local_id, path = self._target("input", params["input"])
        if local_id not in self._media_inputs:
            raise CommandRefusedError("The selected input has no media play queue.")
        return path + ".ActiveQueue"

    async def _media_command(self, command, params):
        path = self._media_target(params)
        await self._request(path + "." + _MEDIA_METHODS[command] + "()")
        await self._read_object(path)

    async def _media_repeat(self, params):
        path = self._media_target(params)
        await self._write(
            path + ".PlayMode", "Repeat" if _bool(params["repeat"]) else "Single"
        )

    async def _save_configuration(self, params):
        await self._request("System.SaveAllSettings()")

    async def set_device_setting(self, setting, value):
        if setting != "unit_name":
            raise ValueError("Unknown CALICO device setting.")
        name = str(value)
        if len(name) > 32 or any(char in name for char in '"\r\n'):
            raise ValueError(
                "Device Name accepts up to 32 characters without quotes or line breaks."
            )
        async with self._operation_lock:
            await self._write("System.UnitDescription", '"' + name + '"')
            await self._request("System.SaveSystemSettings()")


_BAD = object()


def _text(value):
    text = str(value).strip().strip('"')
    return "" if text.upper() == "NULL" else text


def _bool(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in ("on", "yes", "true", "1", "found"):
        return True
    if text in ("off", "no", "false", "0", "not_found"):
        return False
    raise ValueError("Not a CALICO boolean.")


def _number(value):
    text = _text(value)
    if not text:
        return 0
    match = re.fullmatch(r"(?:Canvas|Window|Layout|(?:Stbds\.)?Stbd)?(\d+)", text, re.I)
    if match is None:
        raise ValueError("Not a CALICO object reference.")
    return int(match[1])


def _source(value):
    text = _text(value)
    match = re.fullmatch(r"(?:Slots\.)?Slot(\d+)\.(In|Out)(\d+)", text, re.I)
    if match:
        return f"s{int(match[1])}{match[2][0].lower()}{int(match[3])}"
    return text.lower() if re.fullmatch(r"s\d+[io]\d+", text, re.I) else text


def _normal_path(path):
    text = path.strip().lower()
    for prefix in ("routing.", "slots.", "windows.", "canvases.", "layouts.", "stbds."):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def _object_key(path):
    port = re.fullmatch(r"slot(\d+)\.(in|out)(\d+)\.(.+)", path)
    if port:
        kind = "input" if port[2] == "in" else "output"
        return kind, f"s{int(port[1])}{port[2][0]}{int(port[3])}", port[4]
    match = re.fullmatch(r"(window|canvas|layout|stbd|slot)(\d+)\.(.+)", path)
    if match:
        return (
            {"stbd": "preset", "slot": "module"}.get(match[1], match[1]),
            int(match[2]),
            match[3],
        )
    return None


def _convert(convert, value):
    try:
        result = convert(value)
        return (
            _BAD if isinstance(result, float) and not math.isfinite(result) else result
        )
    except (TypeError, ValueError, OverflowError):
        return _BAD


def _same_echo(actual, expected):
    if expected.lower().startswith("login("):
        return actual.lower().startswith("login(")
    return (
        re.sub(r"\s+", "", actual).casefold() == re.sub(r"\s+", "", expected).casefold()
    )


def _wire_equal(actual, expected):
    if _source(actual).casefold() == _source(expected).casefold():
        return True
    try:
        return math.isfinite(float(actual)) and float(actual) == float(expected)
    except ValueError:
        return False


def _on_off(value):
    return "On" if _bool(value) else "Off"


def _fade_read(value):
    return int(value) * 100.0 / 256


def _fade_write(value):
    return round(float(value) * 256 / 100)


def _rotation_write(value):
    return f"{value:.2f}"


def _checked_value(value, declaration):
    if declaration["type"] == "boolean":
        return _bool(value)
    return _checked_number(value, declaration)


def _checked_number(value, declaration):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("The control value must be finite.")
    if (
        not declaration.get("min", -math.inf)
        <= number
        <= declaration.get("max", math.inf)
    ):
        raise ValueError("The control value is outside its documented range.")
    if declaration["type"] == "integer":
        if not number.is_integer():
            raise ValueError("The selected control needs a whole number.")
        return int(number)
    return number


_EVENT_CATEGORIES = (
    "WINDOW",
    "CANVAS",
    "LAYOUT",
    "OUTPUTS_SYNC",
    "OUTPUT",
    "INPUT",
    "HDMI",
    "STBD",
    "PRESET",
    "MEDIA_PLAYER",
    "MODULE",
    "MODULE_CORE_TEMPERATURE",
)
_EVENT_HANDLERS = {
    ("CANVAS", "PROPERTY_CHANGED"): "_event_property",
    ("LAYOUT", "PROPERTY_CHANGED"): "_event_property",
    ("OUTPUTS_SYNC", "PROPERTY_CHANGED"): "_event_sync",
    ("OUTPUT", "PROPERTY_CHANGED"): "_event_property",
    ("OUTPUT", "STATUS_GROUP"): "_event_property",
    ("INPUT", "STATUS_GROUP"): "_event_property",
    ("MEDIA_PLAYER", "STATUS_UPDATE"): "_event_media",
    ("MODULE", "CORE_TEMPERATURE_ALERT"): "_event_temperature",
    ("MODULE_CORE_TEMPERATURE", "CHANGED"): "_event_temperature",
}
_VALUE_EVENTS = {
    ("WINDOW", "INPUT"): "Input",
    ("CANVAS", "STBDCURRENT_CHANGED"): "StbdCurrent",
    ("STBD", "ISCURRENT_CHANGED"): "IsCurrent",
    ("MODULE", "STATUS"): "Status",
}
_LIVE_EVENTS = {
    ("CANVAS", "STBDCURRENT_CHANGED"),
    ("STBD", "ISCURRENT_CHANGED"),
    ("PRESET", "TAKE"),
    ("PRESET", "COMPLETE"),
    ("HDMI", "SINK_ATTACHED"),
    ("HDMI", "SINK_UNPLUGGED"),
    ("MEDIA_PLAYER", "ITEM_STATUS_CHANGED"),
}
_ROSTER_EVENTS = {("PRESET", "SAVE"), ("PRESET", "REMOVE"), ("MODULE", "STATUS")}
_PARENT_FIELDS = {
    "device.modelnumber": ("model", _text),
    "device.serialnumber": ("serial_number", _text),
    "device.softwareversion": ("firmware", _text),
    "system.apiversion": ("api_version", _text),
    "system.unitdescription": ("unit_name", _text),
    "system.status": ("status", _text),
    "system.alerts": ("alerts", _text),
    "outputssync.lockmode": ("lock_mode", _text),
    "outputssync.locksource": ("lock_source", _source),
    "outputssync.lockstatus": ("lock_status", _text),
    "outputssync.lockaccuracy": ("lock_accuracy", int),
    "system.sensors.bptemperature_degc": ("backplane_temperature", float),
    "system.sensors.bpfpgacore_degc": ("fpga_temperature", float),
    "system.sensors.psu1_degc": ("psu_1_temperature", float),
    "system.sensors.psu2_degc": ("psu_2_temperature", float),
    "system.temperaturecontrol.faninlet0_rpm": ("fan_0_rpm", int),
    "system.temperaturecontrol.faninlet1_rpm": ("fan_1_rpm", int),
    "system.temperaturecontrol.faninlet2_rpm": ("fan_2_rpm", int),
}
_FIELDS = {
    "input": {
        "alias": ("name", _text),
        "status": ("status", _text),
        "hdmi": ("signal_present", _bool),
        "measuredresolution": ("resolution", _text),
        "hdcpstatus": ("hdcp_status", _text),
        "hdcprequired": ("hdcp_required", _text),
        "audio": ("audio_status", _text),
        "colourrangeactive": ("colour_range", _text),
        "cuttoblack": ("blackout", _bool),
        "brightness": ("brightness", int),
        "contrast": ("contrast", int),
        "activequeue.status": ("media_status", _text),
        "activequeue.currentindex": ("media_index", int),
        "activequeue.playmode": ("media_mode", _text),
    },
    "output": {
        "alias": ("name", _text),
        "status": ("status", _text),
        "resolution": ("resolution", _text),
        "layout": ("layout", _number),
        "hdcpactive": ("hdcp_active", _text),
        "hdcpstatus": ("hdcp_status", _text),
        "hdmi": ("sink_present", _bool),
        "framelockstatus": ("lock_status", _text),
        "cuttoblack": ("blackout", _bool),
        "audiomute": ("audio_mute", _bool),
        "audioenable": ("audio_enabled", _bool),
        "ecomode": ("eco_mode", _bool),
    },
    "window": {
        "alias": ("name", _text),
        "input": ("input", _source),
        "canvas": ("canvas", _number),
        "mute": ("muted", _bool),
        "width": ("width", int),
        "height": ("height", int),
        "centerx": ("center_x", int),
        "centery": ("center_y", int),
        "zorder": ("z_order", int),
        "rotation": ("rotation", float),
        "ftb": ("fade_to_black", _fade_read),
        "hflip": ("horizontal_flip", _bool),
        "vflip": ("vertical_flip", _bool),
        "scftb": ("fade_through_black", _bool),
        "sccrossfade": ("crossfade", _bool),
    },
    "canvas": {
        "alias": ("name", _text),
        "stbdcurrent": ("current_preset", _number),
        "windowlist": ("windows", _text),
        "layoutlist": ("layouts", _text),
        "audiosource": ("audio_source", _source),
        "audiomode": ("audio_mode", _text),
        "audiofollowwindow": ("audio_follow_window", _number),
        "audiovolume": ("audio_volume", int),
        "audiomute": ("audio_mute", _bool),
        "cuttoblack": ("blackout", _bool),
    },
    "layout": {
        "alias": ("name", _text),
        "canvas": ("canvas", _number),
        "outputlist": ("outputs", _text),
        "framerate": ("frame_rate", _text),
        "lock": ("locked", _bool),
        "stbdactive": ("animating", _bool),
    },
    "preset": {
        "name": ("name", _text),
        "canvas": ("canvas", _number),
        "duration": ("duration_ms", int),
        "iscurrent": ("current", _bool),
    },
    "module": {
        "cardtype": ("card_type", _text),
        "status": ("status", _text),
        "coretemperature": ("temperature", float),
        "temperaturealert": ("temperature_alert", _text),
    },
}
_MEDIA_SCHEMA = {
    "media_status": {"type": "string", "label": "Playback Status"},
    "media_index": {"type": "integer", "label": "Current Queue Item", "min": 0},
    "media_mode": {"type": "string", "label": "Playback Mode"},
}
_CONTROLS = {
    "window": {
        "muted": ("Mute", _on_off),
        "width": ("Width", int),
        "height": ("Height", int),
        "center_x": ("CenterX", int),
        "center_y": ("CenterY", int),
        "z_order": ("Zorder", int),
        "rotation": ("Rotation", _rotation_write),
        "fade_to_black": ("FTB", _fade_write),
        "horizontal_flip": ("HFlip", _on_off),
        "vertical_flip": ("VFlip", _on_off),
        "fade_through_black": ("SCFTB", _on_off),
        "crossfade": ("SCCrossFade", _on_off),
    },
    "input": {
        "blackout": ("CutToBlack", _on_off),
        "brightness": ("Brightness", int),
        "contrast": ("Contrast", int),
    },
    "output": {
        "blackout": ("CutToBlack", _on_off),
        "audio_mute": ("AudioMute", _on_off),
        "audio_enabled": ("AudioEnable", _on_off),
        "eco_mode": ("EcoMode", _on_off),
    },
}
_PROPERTY_COMMANDS = {
    "set_canvas_volume": ("canvas", "AudioVolume", "level", int),
    "set_canvas_mute": ("canvas", "AudioMute", "muted", _on_off),
    "set_canvas_blackout": ("canvas", "CutToBlack", "enabled", _on_off),
}
_MEDIA_METHODS = {
    "media_play": "Play",
    "media_pause": "Pause",
    "media_stop": "Stop",
    "media_next": "SkipForward",
    "media_previous": "SkipBackward",
}
_COMMAND_HANDLERS = {
    "refresh": "_command_refresh",
    "recall_preset": "_recall",
    "set_window_input": "_route_window",
    "set_window_canvas": "_window_canvas",
    "set_canvas_audio_input": "_canvas_input",
    "set_canvas_audio_window": "_canvas_window",
    "set_media_repeat": "_media_repeat",
    "save_configuration": "_save_configuration",
}
