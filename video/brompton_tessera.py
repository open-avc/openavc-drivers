"""Tessera SX40/S8 control using Brompton's public 3.5.2 HTTP API.

Python reconciles sparse project-owned groups, presets and stored frames, and
associates repeated leaf names with their queried branch. The standard platform
HTTP transport owns connections and simulation. No control-state subscription
is documented: small control reads are paced; rosters and health are slower.
The API returns the actual new value on PUT; command state is never synthesized.
Source: Tessera IP Control API 3.5.2 revision 2, pages 7, 17-20, 35-39, 51-53,
64-71. No firmware, calibration or wall-layout authoring is implemented.
"""

from __future__ import annotations

import asyncio
import math
import time

from openavc.drivers.base import BaseDriver

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:
    CommandRefusedError = RuntimeError


class BromptonTesseraDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "brompton_tessera",
        "name": "Brompton Tessera SX40 / S8",
        "manufacturer": "Brompton Technology",
        "category": "video",
        "version": "1.0.0",
        "author": "OpenAVC",
        "transport": "http",
        "description": "Control Tessera LED walls with named presets and inputs, brightness and group "
        "controls, blackout, freeze and stored frames. Monitor processor temperatures, fans and "
        "redundancy.",
        "source_url": "https://dl.bromptontech.com/tessera/docs/TesseraIPControlAPI3_5_2_r2.pdf",
        "ports": [80],
        "protocols": ["brompton-tessera-ip"],
        "simulated": True,
        "verified": False,
        "web_ui": False,
        "min_platform_version": "0.36.0",
        "tags": ["led-wall", "video-wall", "presets", "redundancy"],
        "compatible_models": [
            {
                "manufacturer": "Brompton Technology",
                "models": ["Tessera SX40", "Tessera S8"],
                "confidence": "untested",
                "notes": "Published Tessera 3.5.2 API; not verified on hardware. SQ200 and "
                "legacy models are not included.",
            }
        ],
        "help": {
            "overview": "Run day-to-day LED wall controls and monitor the processor. Presets, inputs, "
            "groups and stored frames are read from the loaded Tessera project. Brightness is "
            "luminance in nits; intensity is a separate percentage gain.",
            "setup": "Create the wall and its presets in Tessera first. Enable IP Control in the Live "
            "Control tile, connect the management Ethernet port, and enter its IP address. Use "
            "firmware 3.5.2 or later with this API. No API login is documented. Project and "
            "calibration authoring remain in Tessera Remote.",
            "connection": "The public API uses HTTP on port 80. Project-dependent controls need a loaded "
            "project. Status is polled every 10 seconds and project rosters and health "
            "every 60 seconds; Refresh Status reads both immediately. Syslog is a separate "
            "device log, not a documented control-state subscription. Blackout and freeze "
            "may trigger processor failover when that mode is enabled.",
        },
        "default_config": {
            "host": "",
            "port": 80,
            "poll_interval": 10,
            "detail_poll_interval": 60,
            "inter_command_delay": 0.1,
            "model": "sx40",
        },
        "config_schema": {
            "host": {"type": "string", "label": "IP Address", "required": True},
            "port": {
                "type": "integer",
                "label": "HTTP Port",
                "default": 80,
                "min": 1,
                "max": 65535,
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status Refresh Interval (seconds)",
                "default": 10,
                "min": 5,
                "max": 300,
                "advanced": True,
            },
            "detail_poll_interval": {
                "type": "integer",
                "label": "Project and Health Refresh (seconds)",
                "default": 60,
                "min": 30,
                "max": 3600,
                "advanced": True,
            },
            "inter_command_delay": {
                "type": "number",
                "label": "Request Spacing (seconds)",
                "default": 0.1,
                "min": 0.05,
                "max": 1,
                "advanced": True,
            },
            "model": {
                "type": "enum",
                "label": "Processor Model",
                "values": [
                    {"value": "sx40", "label": "Tessera SX40"},
                    {"value": "s8", "label": "Tessera S8"},
                ],
                "default": "sx40",
                "learned_from": "model",
                "help": "Filled from the processor when connected. Also selects the "
                "simulated hardware; it does not override a real processor.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Processor Model"},
            "processor_name": {"type": "string", "label": "Processor Name"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "firmware": {"type": "string", "label": "Firmware"},
            "uptime": {"type": "string", "label": "Uptime", "cloud_priority": "low"},
            "project_name": {"type": "string", "label": "Project Name"},
            "project_loaded": {
                "type": "boolean",
                "label": "Project Loaded",
                "cloud_priority": "high",
            },
            "active_preset": {
                "type": "integer",
                "label": "Active Preset",
                "min": 1,
                "max": 128,
                "control": True,
                "step": 1,
            },
            "active_preset_name": {"type": "string", "label": "Active Preset Name"},
            "active_input": {
                "type": "string",
                "label": "Active Input",
                "control": True,
            },
            "blackout": {"type": "boolean", "label": "Blackout", "control": True},
            "freeze": {"type": "boolean", "label": "Freeze", "control": True},
            "test_pattern_enabled": {
                "type": "boolean",
                "label": "Test Pattern Enabled",
                "control": True,
            },
            "test_pattern": {
                "type": "string",
                "label": "Test Pattern or Frame",
                "control": True,
            },
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "cloud_priority": "high",
            },
            "associated_count": {
                "type": "integer",
                "label": "Associated Devices",
                "min": 0,
                "max": 2200,
                "step": 1,
            },
            "online_count": {
                "type": "integer",
                "label": "Online Devices",
                "min": 0,
                "max": 2048,
                "step": 1,
            },
            "error_count": {
                "type": "integer",
                "label": "Devices Reporting Errors",
                "min": 0,
                "max": 2048,
                "cloud_priority": "high",
                "step": 1,
            },
            "failover_enabled": {
                "type": "boolean",
                "label": "Processor Redundancy Enabled",
            },
            "failover_active": {
                "type": "boolean",
                "label": "Failover Active",
                "cloud_priority": "high",
            },
            "failover_role": {
                "type": "enum",
                "label": "Redundancy Role",
                "values": ["primary", "backup"],
            },
            "partner_present": {
                "type": "boolean",
                "label": "Redundancy Partner Present",
                "cloud_priority": "high",
            },
            "partner_name": {"type": "string", "label": "Redundancy Partner Name"},
            "partner_serial": {"type": "string", "label": "Redundancy Partner Serial"},
            "partner_absence": {"type": "string", "label": "Partner Absence Duration"},
            "partner_video_absence": {
                "type": "string",
                "label": "Partner Video Absence Duration",
            },
            "brightness": {
                "type": "integer",
                "label": "Brightness",
                "min": 0,
                "max": 10000,
                "control": True,
                "unit": "nits",
                "step": 1,
            },
            "colour_temperature": {
                "type": "integer",
                "label": "Color Temperature",
                "min": 2000,
                "max": 11000,
                "control": True,
                "unit": "K",
                "step": 1,
            },
            "gamma": {
                "type": "float",
                "label": "Gamma",
                "min": 0.2,
                "max": 4,
                "step": 0.01,
                "control": True,
            },
            "intensity": {
                "type": "float",
                "label": "Intensity Gain",
                "min": 0,
                "max": 100,
                "step": 0.01,
                "control": True,
                "unit": "%",
            },
            "brightness_limited": {
                "type": "boolean",
                "label": "Brightness Limit Enabled",
            },
            "brightness_limit": {
                "type": "integer",
                "label": "Brightness Limit",
                "min": 0,
                "max": 10000,
                "unit": "nits",
                "step": 1,
            },
            "blackout_fade": {
                "type": "float",
                "label": "Blackout Fade Time",
                "min": 0,
                "max": 10,
                "step": 0.1,
                "unit": "s",
            },
            "temperature_ambient": {
                "type": "float",
                "label": "AMBIENT Temperature",
                "min": 0,
                "max": 200,
                "cloud_priority": "low",
                "unit": "°C",
            },
            "temperature_cpu": {
                "type": "float",
                "label": "CPU Temperature",
                "min": 0,
                "max": 200,
                "cloud_priority": "low",
                "unit": "°C",
            },
            "temperature_fpga": {
                "type": "float",
                "label": "FPGA Temperature",
                "min": 0,
                "max": 200,
                "cloud_priority": "low",
                "unit": "°C",
            },
            "temperature_gpu": {
                "type": "float",
                "label": "GPU Temperature",
                "min": 0,
                "max": 200,
                "cloud_priority": "low",
                "unit": "°C",
            },
            "temperature_main": {
                "type": "float",
                "label": "MAIN Temperature",
                "min": 0,
                "max": 200,
                "cloud_priority": "low",
                "unit": "°C",
            },
            "temperature_psu": {
                "type": "float",
                "label": "PSU Temperature",
                "min": 0,
                "max": 200,
                "cloud_priority": "low",
                "unit": "°C",
            },
            "fan_case_1_speed": {
                "type": "float",
                "label": "Case 1 Fan Speed",
                "min": 0,
                "max": 5000,
                "cloud_priority": "low",
                "unit": "RPM",
            },
            "fan_case_1_active": {
                "type": "boolean",
                "label": "Case 1 Fan Active",
                "help": "Activation state reported by Tessera; inactive "
                "alone does not mean a fan fault.",
            },
            "fan_case_2_speed": {
                "type": "float",
                "label": "Case 2 Fan Speed",
                "min": 0,
                "max": 5000,
                "cloud_priority": "low",
                "unit": "RPM",
            },
            "fan_case_2_active": {
                "type": "boolean",
                "label": "Case 2 Fan Active",
                "help": "Activation state reported by Tessera; inactive "
                "alone does not mean a fan fault.",
            },
            "fan_fpga_speed": {
                "type": "float",
                "label": "Fpga Fan Speed",
                "min": 0,
                "max": 5000,
                "cloud_priority": "low",
                "unit": "RPM",
            },
            "fan_fpga_active": {
                "type": "boolean",
                "label": "Fpga Fan Active",
                "help": "Activation state reported by Tessera; inactive alone "
                "does not mean a fan fault.",
            },
            "failover_on_button": {
                "type": "boolean",
                "label": "Fail Over on Blackout / Freeze",
            },
            "failover_on_partner": {
                "type": "boolean",
                "label": "Fail Over on Partner Failure",
            },
            "failover_on_video": {
                "type": "boolean",
                "label": "Fail Over on Partner Video Loss",
            },
            "failover_prefer_primary": {
                "type": "boolean",
                "label": "Prefer Primary Processor",
            },
        },
        "child_entity_types": {
            "group": {
                "label": "Group",
                "label_plural": "Groups",
                "id_format": {"type": "string"},
                "label_field": "name",
                "summary_fields": ["brightness", "intensity", "global_colour_override"],
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "brightness": {
                        "type": "integer",
                        "label": "Brightness",
                        "min": 0,
                        "max": 10000,
                        "control": True,
                        "unit": "nits",
                        "step": 1,
                    },
                    "colour_temperature": {
                        "type": "integer",
                        "label": "Color Temperature",
                        "min": 2000,
                        "max": 11000,
                        "control": True,
                        "unit": "K",
                        "step": 1,
                    },
                    "gamma": {
                        "type": "float",
                        "label": "Gamma",
                        "min": 0.2,
                        "max": 4,
                        "step": 0.01,
                        "control": True,
                    },
                    "intensity": {
                        "type": "float",
                        "label": "Intensity Gain",
                        "min": 0,
                        "max": 100,
                        "step": 0.01,
                        "control": True,
                        "unit": "%",
                    },
                    "brightness_limited": {
                        "type": "boolean",
                        "label": "Brightness Limit Enabled",
                    },
                    "brightness_limit": {
                        "type": "integer",
                        "label": "Brightness Limit",
                        "min": 0,
                        "max": 10000,
                        "unit": "nits",
                        "step": 1,
                    },
                    "global_colour_override": {
                        "type": "boolean",
                        "label": "Override Global Color",
                        "control": True,
                        "help": "Enable to use "
                        "this group’s "
                        "own "
                        "brightness, "
                        "color "
                        "temperature "
                        "and gamma.",
                    },
                    "global_gains_override": {
                        "type": "boolean",
                        "label": "Override Global Gains",
                        "control": True,
                        "help": "Enable to use "
                        "this group’s "
                        "own intensity "
                        "and color "
                        "gains.",
                    },
                },
            },
            "preset": {
                "label": "Preset",
                "label_plural": "Presets",
                "id_format": {"type": "integer", "min": 1, "max": 128},
                "label_field": "name",
                "summary_fields": ["active"],
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "active": {"type": "boolean", "label": "Active", "control": True},
                },
            },
            "frame": {
                "label": "Stored Frame",
                "label_plural": "Stored Frames",
                "id_format": {"type": "integer", "min": 1, "max": 50},
                "label_field": "name",
                "summary_fields": ["active"],
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "active": {
                        "type": "boolean",
                        "label": "Displayed",
                        "control": True,
                    },
                },
            },
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "string"},
                "label_field": "name",
                "summary_fields": ["active", "width", "height", "refresh_rate"],
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "active": {"type": "boolean", "label": "Active", "control": True},
                    "width": {"type": "integer", "label": "Signal Width", "unit": "px"},
                    "height": {
                        "type": "integer",
                        "label": "Signal Height",
                        "unit": "px",
                    },
                    "refresh_rate": {
                        "type": "float",
                        "label": "Refresh Rate",
                        "unit": "Hz",
                    },
                },
            },
            "loop": {
                "label": "Redundant Cable Loop",
                "label_plural": "Redundant Cable Loops",
                "id_format": {"type": "string"},
                "summary_fields": ["state"],
                "state_variables": {
                    "state": {
                        "type": "string",
                        "label": "Cable Redundancy State",
                        "cloud_priority": "high",
                    }
                },
            },
        },
        "device_settings": {
            "blackout_fade": {
                "type": "number",
                "label": "Blackout Fade Time (seconds)",
                "help": "Fade from zero (snap) to ten seconds.",
                "min": 0,
                "max": 10,
                "state_key": "blackout_fade",
            },
            "failover_enabled": {
                "type": "boolean",
                "label": "Processor Redundancy",
                "help": "Changes the live processor redundancy configuration. "
                "Commission and test both processors in Tessera "
                "first.",
                "state_key": "failover_enabled",
            },
            "failover_on_button": {
                "type": "boolean",
                "label": "Fail Over on Blackout / Freeze",
                "help": "Changes the live processor redundancy "
                "configuration. Commission and test both processors "
                "in Tessera first.",
                "state_key": "failover_on_button",
            },
            "failover_on_partner": {
                "type": "boolean",
                "label": "Fail Over on Partner Failure",
                "help": "Changes the live processor redundancy "
                "configuration. Commission and test both "
                "processors in Tessera first.",
                "state_key": "failover_on_partner",
            },
            "failover_on_video": {
                "type": "boolean",
                "label": "Fail Over on Partner Video Loss",
                "help": "Changes the live processor redundancy "
                "configuration. Commission and test both processors "
                "in Tessera first.",
                "state_key": "failover_on_video",
            },
            "failover_prefer_primary": {
                "type": "boolean",
                "label": "Prefer Primary Processor",
                "help": "Changes the live processor redundancy "
                "configuration. Commission and test both "
                "processors in Tessera first.",
                "state_key": "failover_prefer_primary",
            },
        },
        "commands": {
            "refresh": {
                "label": "Refresh Status and Project",
                "help": "Read the processor and reconcile its named inputs, groups, presets "
                "and stored frames.",
                "params": {},
                "query_for": "brightness",
            },
            "blackout_on": {
                "label": "Blackout On",
                "help": "May also trigger failover if the processor’s button-press "
                "failover mode is enabled.",
                "params": {},
                "sets": {"blackout": True},
            },
            "blackout_off": {
                "label": "Blackout Off",
                "help": "May also trigger failover if the processor’s button-press "
                "failover mode is enabled.",
                "params": {},
                "sets": {"blackout": False},
            },
            "freeze_on": {
                "label": "Freeze On",
                "help": "May also trigger failover if the processor’s button-press failover "
                "mode is enabled.",
                "params": {},
                "sets": {"freeze": True},
            },
            "freeze_off": {
                "label": "Freeze Off",
                "help": "May also trigger failover if the processor’s button-press "
                "failover mode is enabled.",
                "params": {},
                "sets": {"freeze": False},
            },
            "test_pattern_off": {
                "label": "Return to Live Video",
                "help": "Disable the test pattern or stored frame. Blackout and "
                "freeze are independent.",
                "params": {},
                "sets": {"test_pattern_enabled": False},
            },
            "show_test_pattern": {
                "label": "Show Test Pattern",
                "help": "Select and enable a test pattern. Custom patterns use the "
                "content configured in Tessera.",
                "params": {
                    "pattern": {
                        "type": "enum",
                        "label": "Pattern",
                        "required": True,
                        "values": [
                            {"value": "brompton", "label": "Brompton"},
                            {"value": "red", "label": "Red"},
                            {"value": "green", "label": "Green"},
                            {"value": "blue", "label": "Blue"},
                            {"value": "cyan", "label": "Cyan"},
                            {"value": "magenta", "label": "Magenta"},
                            {"value": "yellow", "label": "Yellow"},
                            {"value": "white", "label": "White"},
                            {"value": "black", "label": "Black"},
                            {"value": "grid", "label": "Grid"},
                            {"value": "scrolling-grid", "label": "Scrolling Grid"},
                            {"value": "checkerboard", "label": "Checkerboard"},
                            {
                                "value": "scrolling-checkerboard",
                                "label": "Scrolling Checkerboard",
                            },
                            {"value": "colour-bars", "label": "Colour Bars"},
                            {
                                "value": "scrolling-colour-bars",
                                "label": "Scrolling Colour Bars",
                            },
                            {"value": "gradient", "label": "Gradient"},
                            {
                                "value": "scrolling-gradient",
                                "label": "Scrolling Gradient",
                            },
                            {"value": "strobe", "label": "Strobe"},
                            {"value": "smpte-bars", "label": "Smpte Bars"},
                            {
                                "value": "scrolling-smpte-bars",
                                "label": "Scrolling Smpte Bars",
                            },
                            {"value": "custom-colour", "label": "Custom Colour"},
                            {"value": "custom", "label": "Custom"},
                            {
                                "value": "forty-five-degree-grid",
                                "label": "Forty Five Degree Grid",
                            },
                            {
                                "value": "scrolling-forty-five-degree-grid",
                                "label": "Scrolling Forty Five Degree Grid",
                            },
                            {"value": "custom-gradient", "label": "Custom Gradient"},
                            {
                                "value": "scrolling-custom-gradient",
                                "label": "Scrolling Custom Gradient",
                            },
                        ],
                    }
                },
                "sets": {"test_pattern": "{pattern}", "test_pattern_enabled": True},
            },
            "select_input": {
                "label": "Select Input",
                "params": {
                    "input": {
                        "type": "child_id",
                        "label": "Input",
                        "child_type": "input",
                        "required": True,
                    }
                },
                "sets": {"active_input": "{input}"},
            },
            "recall_preset": {
                "label": "Recall Preset",
                "params": {
                    "preset": {
                        "type": "child_id",
                        "label": "Preset",
                        "child_type": "preset",
                        "required": True,
                    }
                },
                "sets": {"active_preset": "{preset}"},
            },
            "show_stored_frame": {
                "label": "Show Stored Frame",
                "params": {
                    "frame": {
                        "type": "child_id",
                        "label": "Stored Frame",
                        "child_type": "frame",
                        "required": True,
                    }
                },
                "sets": {"test_pattern": "{frame}", "test_pattern_enabled": True},
            },
            "set_brightness": {
                "label": "Set Brightness",
                "params": {
                    "value": {
                        "type": "integer",
                        "label": "Brightness",
                        "min": 0,
                        "max": 10000,
                        "unit": "nits",
                        "required": True,
                    }
                },
                "sets": {"brightness": "{value}"},
            },
            "set_group_brightness": {
                "label": "Set Group Brightness",
                "help": "The group uses this value when its corresponding global "
                "override is enabled.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "label": "Group",
                        "child_type": "group",
                        "required": True,
                    },
                    "value": {
                        "type": "integer",
                        "label": "Brightness",
                        "min": 0,
                        "max": 10000,
                        "unit": "nits",
                        "required": True,
                    },
                },
                "sets": {"brightness": "{value}"},
            },
            "set_intensity": {
                "label": "Set Intensity Gain",
                "params": {
                    "value": {
                        "type": "number",
                        "label": "Intensity Gain",
                        "min": 0,
                        "max": 100,
                        "unit": "%",
                        "required": True,
                        "decimals": 2,
                    }
                },
                "sets": {"intensity": "{value}"},
            },
            "set_group_intensity": {
                "label": "Set Group Intensity Gain",
                "help": "The group uses this value when its corresponding global "
                "override is enabled.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "label": "Group",
                        "child_type": "group",
                        "required": True,
                    },
                    "value": {
                        "type": "number",
                        "label": "Intensity Gain",
                        "min": 0,
                        "max": 100,
                        "unit": "%",
                        "required": True,
                        "decimals": 2,
                    },
                },
                "sets": {"intensity": "{value}"},
            },
            "set_colour_temperature": {
                "label": "Set Color Temperature",
                "params": {
                    "value": {
                        "type": "integer",
                        "label": "Color Temperature",
                        "min": 2000,
                        "max": 11000,
                        "unit": "K",
                        "required": True,
                    }
                },
                "sets": {"colour_temperature": "{value}"},
            },
            "set_group_colour_temperature": {
                "label": "Set Group Color Temperature",
                "help": "The group uses this value when its "
                "corresponding global override is enabled.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "label": "Group",
                        "child_type": "group",
                        "required": True,
                    },
                    "value": {
                        "type": "integer",
                        "label": "Color Temperature",
                        "min": 2000,
                        "max": 11000,
                        "unit": "K",
                        "required": True,
                    },
                },
                "sets": {"colour_temperature": "{value}"},
            },
            "set_gamma": {
                "label": "Set Gamma",
                "params": {
                    "value": {
                        "type": "number",
                        "label": "Gamma",
                        "min": 0.2,
                        "max": 4,
                        "required": True,
                        "decimals": 2,
                    }
                },
                "sets": {"gamma": "{value}"},
            },
            "set_group_gamma": {
                "label": "Set Group Gamma",
                "help": "The group uses this value when its corresponding global "
                "override is enabled.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "label": "Group",
                        "child_type": "group",
                        "required": True,
                    },
                    "value": {
                        "type": "number",
                        "label": "Gamma",
                        "min": 0.2,
                        "max": 4,
                        "required": True,
                        "decimals": 2,
                    },
                },
                "sets": {"gamma": "{value}"},
            },
            "set_group_global_colour_override": {
                "label": "Override Global Color",
                "help": "Enable to use this group’s own brightness, "
                "color temperature and gamma.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "label": "Group",
                        "child_type": "group",
                        "required": True,
                    },
                    "value": {"type": "boolean", "label": "Enabled", "required": True},
                },
                "sets": {"global_colour_override": "{value}"},
            },
            "set_group_global_gains_override": {
                "label": "Override Global Gains",
                "help": "Enable to use this group’s own intensity and color gains.",
                "params": {
                    "group": {
                        "type": "child_id",
                        "label": "Group",
                        "child_type": "group",
                        "required": True,
                    },
                    "value": {"type": "boolean", "label": "Enabled", "required": True},
                },
                "sets": {"global_gains_override": "{value}"},
            },
        },
        "quick_actions": [
            "blackout_on",
            "blackout_off",
            "freeze_on",
            "freeze_off",
            "test_pattern_off",
            "refresh",
        ],
        "discovery": {
            "tcp_probe": {
                "port": 80,
                "send_ascii": "GET /api/system/processor-type HTTP/1.0\r\n\r\n",
                "expect_regex": '"processor-type"\\s*:\\s*"(?:sx40|s8)"',
                "extract": {"model": {"regex": '"processor-type"\\s*:\\s*"(sx40|s8)"'}},
            },
            "manufacturer_alias": ["Brompton Technology", "Brompton"],
        },
    }

    HEALTH_INTERVAL_S = 30
    HEALTH_TIMEOUT_S = 5
    HEALTH_MAX_FAILURES = 3
    HEALTH_FAULT_MESSAGE = (
        "The Tessera processor stopped answering IP control requests."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._exchange = asyncio.Lock()
        self._detail_due = 0.0
        self._read_errors = {}
        self._input_addresses = {}

    async def _post_connect(self):
        self._detail_due = 0.0
        self._read_errors.clear()
        await self._identify()

    async def _initial_sync(self):
        await self.poll()

    async def _identify(self):
        async with self._exchange:
            model = await self._get("system/processor-type")
        if model not in ("sx40", "s8"):
            raise ConnectionError(
                "This driver requires a Tessera SX40 or S8 processor."
            )
        self.set_state("model", model)

    async def _liveness_probe(self):
        async with self._exchange:
            try:
                await self._get("system/processor-type")
            except TesseraRefused:
                pass  # An API error is still a reply from the processor.

    async def _get(self, path):
        response = await self.transport.get("/api/" + path)
        return _response_value(path, response)

    async def _put(self, path, value):
        response = await self.transport.put("/api/" + path, body={"data": value})
        return _response_value(path, response)

    async def _read_branch(self, path):
        async with self._exchange:
            try:
                value = await self._get(path)
                self._publish(path, value)
                self._read_errors.pop(path, None)
                return True
            except TesseraRefused as exc:
                self._read_errors[path] = str(exc)
                self._clear_branch(path)
                if exc.code == "No project loaded":
                    self._clear_project()
                return False

    def _publish(self, path, value):
        if path in ROSTERS:
            return self._publish_roster(ROSTERS[path], value)
        if path == "input/ports":
            return self._publish_inputs(value)
        if path == "input/active/source":
            return self._publish_source(value)
        if path == "presets/active":
            self._publish_preset(value)
        self.set_states(_flatten_read(value, READS[path]))
        if path == "project/name":
            if self.get_state("project_loaded") is not True:
                self._detail_due = 0.0
            self.set_state("project_loaded", True)
        if path in ("override/test-pattern/type", "override/test-pattern/enabled"):
            self._update_frame_activity()

    def _publish_roster(self, kind, rows):
        if not isinstance(rows, dict):
            raise TesseraRefused("Malformed project roster")
        checked = _checked_roster(kind, rows)
        for child_id, data in checked.items():
            values = _flatten_read(data, CHILD_READS[kind])
            values["online"] = True
            self.register_child(kind, child_id, values)
            self.set_child_state_batch(kind, child_id, values)
        for removed in set(self.list_children(kind)) - checked.keys():
            self.deregister_child(kind, removed)
        if kind == "frame":
            self._update_frame_activity()

    def _publish_inputs(self, ports):
        if not isinstance(ports, dict):
            raise TesseraRefused("Malformed input roster")
        addresses = {}
        for port_type in ("hdmi", "sdi"):
            for port, data in _input_rows(ports.get(port_type, {})):
                child_id = f"{port_type}_{port}"
                addresses[child_id] = {"port-type": port_type, "port-number": port}
                values = _flatten_read(data, INPUT_READS)
                values.update(name=f"{port_type.upper()} {port}", online=True)
                self.register_child("input", child_id, values)
                self.set_child_state_batch("input", child_id, values)
        for removed in set(self.list_children("input")) - addresses.keys():
            self.deregister_child("input", removed)
        self._input_addresses = addresses
        self._update_input_activity()

    def _publish_source(self, data):
        if (
            not isinstance(data, dict)
            or not {"port-type", "port-number"} <= data.keys()
        ):
            raise TesseraRefused("Malformed active input")
        self.set_state("active_input", f"{data['port-type']}_{data['port-number']}")
        self._update_input_activity()

    def _update_input_activity(self):
        selected = self.get_state("active_input")
        for child_id in self.list_children("input"):
            self.set_child_state(
                "input",
                child_id,
                "active",
                None if selected is None else selected == child_id,
            )

    def _publish_preset(self, data):
        if not isinstance(data, dict):
            raise TesseraRefused("Malformed active preset")
        number = data.get("number")
        for child_id in self.list_children("preset"):
            self.set_child_state(
                "preset",
                child_id,
                "active",
                None if number is None else number == child_id,
            )

    def _update_frame_activity(self):
        selected = self.get_state("test_pattern")
        enabled = self.get_state("test_pattern_enabled")
        for child_id in self.list_children("frame"):
            active = (
                None
                if selected is None or enabled is None
                else enabled and selected == str(child_id)
            )
            self.set_child_state("frame", child_id, "active", active)

    def _clear_branch(self, path):
        fields = READS.get(path, {})
        self.set_states({name: None for name in fields.values()})
        self._clear_child_readings(ROSTERS.get(path))
        if path == "input/active/source":
            self.set_state("active_input", None)
            self._update_input_activity()
        if path in ("override/test-pattern/type", "override/test-pattern/enabled"):
            self._update_frame_activity()
        if path == "presets/active":
            self._publish_preset({})

    def _clear_child_readings(self, kind):
        if not kind:
            return
        values = {key: None for key in CHILD_READS[kind].values()}
        for child_id in self.list_children(kind):
            self.set_child_state_batch(kind, child_id, values)

    def _clear_project(self):
        self.set_state("project_loaded", False)
        for path in PROJECT_READS:
            self._clear_branch(path)
        for kind in self.DRIVER_INFO["child_entity_types"]:
            for child_id in self.list_children(kind):
                self.deregister_child(kind, child_id)
        self._input_addresses.clear()
        self._detail_due = 0.0

    async def poll(self):
        # Each exchange releases the lock: a button waits for one request,
        # never the entire project/health sweep. Transport silence propagates.
        await self._identify()
        loaded = await self._read_branch("project/name")
        slow = time.monotonic() >= self._detail_due
        if loaded:
            await self._poll_paths(FAST_READS)
            await self._poll_project_detail(slow)
        if slow:
            await self._poll_paths(SYSTEM_DETAIL)
            self._detail_due = time.monotonic() + max(
                30, self.config.get("detail_poll_interval", 60)
            )
        self.set_state("last_error", "; ".join(self._read_errors.values()))

    async def _poll_paths(self, paths):
        for path in paths:
            await self._read_branch(path)

    async def _poll_project_detail(self, due):
        if due:
            await self._poll_paths(PROJECT_DETAIL)

    async def refresh_children(self):
        self._detail_due = 0.0
        await self.poll()
        return {
            kind: len(self.list_children(kind))
            for kind in self.DRIVER_INFO["child_entity_types"]
        }

    async def send_command(self, command, params=None):
        if command == "refresh":
            return await self.refresh_children()
        async with self._exchange:
            try:
                await self._run_command(command, params or {})
                self.set_state("last_error", "; ".join(self._read_errors.values()))
            except TesseraRefused as exc:
                self.set_state("last_error", str(exc))
                raise CommandRefusedError(str(exc)) from exc
        if command == "recall_preset":
            await self.refresh_children()

    async def _run_command(self, command, params):
        if command in SWITCHES:
            path, value = SWITCHES[command]
            return await self._write_leaf(path, value)
        if command in COLOUR_COMMANDS:
            return await self._write_colour(command, params)
        if command in ("select_input", "recall_preset"):
            return await self._select(command, params)
        if command in ("show_test_pattern", "show_stored_frame"):
            return await self._show_pattern(command, params)
        raise ValueError(f"Unknown command: {command}")

    async def _write_leaf(self, path, value):
        actual = await self._put(path, value)
        self._publish(path, actual)
        _require_applied(value, actual)
        self._read_errors.pop(path, None)

    async def _write_colour(self, command, params):
        field_path = COLOUR_COMMANDS[command]
        definition = self.DRIVER_INFO["commands"][command]["params"]["value"]
        value = _validated_value(definition, params.get("value"))
        if command.startswith("set_group_"):
            child_id = self._require_child("group", params.get("group"))
            path = f"groups/items/{child_id}/{field_path}"
            actual = await self._put(path, value)
            property_name = CHILD_READS["group"][field_path]
            self.set_child_state("group", child_id, property_name, actual)
            _require_applied(value, actual)
            return
        path = "output/global-colour/" + field_path
        actual = await self._put(path, value)
        property_name = READS["output/global-colour"][field_path]
        self.set_state(property_name, actual)
        _require_applied(value, actual)

    def _require_child(self, kind, requested):
        for child_id in self.list_children(kind):
            if str(child_id) == str(requested):
                return child_id
        raise TesseraRefused(
            f"Choose a {kind} listed in the current project. Refresh if the project changed."
        )

    async def _select(self, command, params):
        if command == "select_input":
            child_id = self._require_child("input", params.get("input"))
            value = self._input_addresses[child_id]
            path = "input/active/source"
        else:
            value = self._require_child("preset", params.get("preset"))
            path = "presets/active/number"
        actual = await self._put(path, value)
        _require_applied(value, actual)
        read_path = (
            "input/active/source" if command == "select_input" else "presets/active"
        )
        self._publish(read_path, await self._get(read_path))

    async def _show_pattern(self, command, params):
        if command == "show_stored_frame":
            value = self._require_child("frame", params.get("frame"))
        else:
            definition = self.DRIVER_INFO["commands"][command]["params"]["pattern"]
            value = _validated_value(definition, params.get("pattern"))
        expected = {"type": value, "enabled": True}
        actual = await self._put("override/test-pattern", expected)
        if isinstance(actual, dict):
            self._publish("override/test-pattern/type", actual.get("type"))
            self._publish("override/test-pattern/enabled", actual.get("enabled"))
        _require_applied(expected, actual)

    async def set_device_setting(self, key, value):
        if key not in SETTINGS:
            raise ValueError(f"Unknown device setting: {key}")
        definition = self.DRIVER_INFO["device_settings"][key]
        value = _validated_value(definition, value)
        async with self._exchange:
            try:
                actual = await self._put(SETTINGS[key], value)
                self.set_state(key, actual)
                _require_applied(value, actual)
                self.set_state("last_error", "; ".join(self._read_errors.values()))
            except TesseraRefused as exc:
                self.set_state("last_error", str(exc))
                raise CommandRefusedError(str(exc)) from exc


class TesseraRefused(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("Tessera: " + code)


def _response_value(path, response):
    body = response.json_data
    if not isinstance(body, dict):
        raise TesseraRefused(
            f"HTTP {response.status_code}: expected an IP control JSON reply."
        )
    if "response-code" in body:
        # Report only the documented error category, never arbitrary response data.
        code = body["response-code"]
        raise TesseraRefused(
            code if code in ERROR_CODES else "The processor refused the request."
        )
    key = path.rsplit("/", 1)[-1]
    if response.status_code != 200 or key not in body:
        raise TesseraRefused(
            f"HTTP {response.status_code}: the requested value was not returned."
        )
    return body[key]


def _nested_value(value, path):
    for key in path.split("/") if path else ():
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _flatten_read(data, mapping):
    result = {name: _nested_value(data, path) for path, name in mapping.items()}
    if result.get("test_pattern") is not None:
        result["test_pattern"] = str(result["test_pattern"])
    return result


def _checked_roster(kind, rows):
    checked = {}
    for key, value in rows.items():
        if not str(key).isdigit() or not isinstance(value, dict):
            raise TesseraRefused("Malformed project roster")
        child_id = _roster_id(kind, key)
        checked[child_id] = value
    return checked


def _roster_id(kind, key):
    if kind in ("group", "loop"):
        return str(key)
    number = int(key)
    maximum = {"preset": 128, "frame": 50}[kind]
    if not 1 <= number <= maximum:
        raise TesseraRefused(f"{kind.title()} outside the documented range")
    return number


def _input_rows(rows):
    if not isinstance(rows, dict):
        raise TesseraRefused("Malformed input roster")
    result = []
    for key, value in rows.items():
        if str(key) not in ("1", "2") or not isinstance(value, dict):
            raise TesseraRefused("Input outside the documented range")
        result.append((int(key), value))
    return result


def _require_applied(expected, actual):
    if not _values_match(expected, actual):
        raise TesseraRefused(
            "The processor did not report the requested value. Check its current state."
        )


def _values_match(expected, actual):
    if isinstance(expected, dict):
        return _fields_match(expected, actual)
    if type(expected) in (int, float):
        return type(actual) in (int, float) and math.isclose(
            expected, actual, abs_tol=0.005
        )
    return type(expected) is type(actual) and expected == actual


def _fields_match(expected, actual):
    if not isinstance(actual, dict):
        return False
    return all(_values_match(value, actual.get(key)) for key, value in expected.items())


def _validated_value(definition, value):
    validators = {"boolean": _boolean_value, "enum": _enum_value}
    return validators.get(definition["type"], _number_value)(definition, value)


def _boolean_value(definition, value):
    if type(value) is bool:
        return value
    if value in ("true", "false"):
        return value == "true"
    raise ValueError("Choose Enabled or Disabled.")


def _enum_value(definition, value):
    if value not in [entry["value"] for entry in definition["values"]]:
        raise ValueError("Choose one of the listed values.")
    return value


def _number_value(definition, value):
    if isinstance(value, bool):
        raise ValueError("Enter a number.")
    number = float(value)
    if (
        not math.isfinite(number)
        or not definition["min"] <= number <= definition["max"]
    ):
        raise ValueError("Enter a number within the displayed range.")
    if definition["type"] == "integer":
        return _whole_number(number)
    return round(number, definition.get("decimals", 2))


def _whole_number(number):
    if number != int(number):
        raise ValueError("Enter a whole number.")
    return int(number)


ERROR_CODES = {
    "Path not found",
    "Bad operation",
    "Not supported",
    "Missing input parameter",
    "Bad input parameter type",
    "Bad input parameter value",
    "Access denied",
    "No project loaded",
    "Object not found",
    "Operation failed",
}
COLOUR_READS = {
    "brightness": "brightness",
    "colour-temperature": "colour_temperature",
    "gamma": "gamma",
    "gains/intensity": "intensity",
    "brightness-limit/enabled": "brightness_limited",
    "brightness-limit/value": "brightness_limit",
}
READS = {
    "system/processor-name": {"": "processor_name"},
    "system/serial-number": {"": "serial_number"},
    "system/software-version": {"": "firmware"},
    "system/uptime": {"": "uptime"},
    "project/name": {"": "project_name"},
    "output/global-colour": COLOUR_READS,
    "override/blackout": {"enabled": "blackout", "fade-time": "blackout_fade"},
    "override/blackout/enabled": {"": "blackout"},
    "override/freeze/enabled": {"": "freeze"},
    "override/test-pattern/enabled": {"": "test_pattern_enabled"},
    "override/test-pattern/type": {"": "test_pattern"},
    "presets/active": {"name": "active_preset_name", "number": "active_preset"},
    "devices/statistics": {
        "associated-count": "associated_count",
        "online-count": "online_count",
        "error-count": "error_count",
    },
    "system/temperature": {
        "ambient": "temperature_ambient",
        "cpu": "temperature_cpu",
        "fpga": "temperature_fpga",
        "gpu": "temperature_gpu",
        "main": "temperature_main",
        "psu": "temperature_psu",
    },
    "system/fan": {
        "case/one/speed": "fan_case_1_speed",
        "case/one/status": "fan_case_1_active",
        "case/two/speed": "fan_case_2_speed",
        "case/two/status": "fan_case_2_active",
        "fpga/speed": "fan_fpga_speed",
        "fpga/status": "fan_fpga_active",
    },
    "output/network/failover": {
        "settings/enabled": "failover_enabled",
        "settings/role": "failover_role",
        "settings/modes/on-button-press": "failover_on_button",
        "settings/modes/on-partner-fail": "failover_on_partner",
        "settings/modes/on-partner-video-fail": "failover_on_video",
        "settings/modes/prefer-primary": "failover_prefer_primary",
        "state/is-active": "failover_active",
        "state/is-partner-present": "partner_present",
        "state/partner-name": "partner_name",
        "state/partner-serial": "partner_serial",
        "state/partner-absence-duration": "partner_absence",
        "state/partner-video-absence-duration": "partner_video_absence",
    },
}
CHILD_READS = {
    "group": {
        **COLOUR_READS,
        "name": "name",
        "global-colour-override": "global_colour_override",
        "global-gains-override": "global_gains_override",
    },
    "preset": {"name": "name", "status": "active"},
    "frame": {"name": "name"},
    "loop": {"state": "state"},
}
INPUT_READS = {
    "meta-data/resolution/width": "width",
    "meta-data/resolution/height": "height",
    "meta-data/refresh-rate": "refresh_rate",
}
ROSTERS = {
    "groups/items": "group",
    "presets/items": "preset",
    "override/test-pattern/frame-store/frames": "frame",
    "output/network/cable-redundancy/loops": "loop",
}
FAST_READS = (
    "output/global-colour",
    "override/blackout",
    "override/freeze/enabled",
    "override/test-pattern/enabled",
    "override/test-pattern/type",
    "presets/active",
    "input/active/source",
    "devices/statistics",
    "output/network/failover",
)
PROJECT_DETAIL = (*ROSTERS, "input/ports")
PROJECT_READS = ("project/name", *FAST_READS, *PROJECT_DETAIL)
SYSTEM_DETAIL = (
    "system/processor-name",
    "system/serial-number",
    "system/software-version",
    "system/uptime",
    "system/temperature",
    "system/fan",
)
SWITCHES = {
    "blackout_on": ("override/blackout/enabled", True),
    "blackout_off": ("override/blackout/enabled", False),
    "freeze_on": ("override/freeze/enabled", True),
    "freeze_off": ("override/freeze/enabled", False),
    "test_pattern_off": ("override/test-pattern/enabled", False),
}
COLOUR_COMMANDS = {
    "set_brightness": "brightness",
    "set_colour_temperature": "colour-temperature",
    "set_gamma": "gamma",
    "set_intensity": "gains/intensity",
    "set_group_brightness": "brightness",
    "set_group_colour_temperature": "colour-temperature",
    "set_group_gamma": "gamma",
    "set_group_intensity": "gains/intensity",
    "set_group_global_colour_override": "global-colour-override",
    "set_group_global_gains_override": "global-gains-override",
}
SETTINGS = {
    "blackout_fade": "override/blackout/fade-time",
    "failover_enabled": "output/network/failover/settings/enabled",
    "failover_on_button": "output/network/failover/settings/modes/on-button-press",
    "failover_on_partner": "output/network/failover/settings/modes/on-partner-fail",
    "failover_on_video": "output/network/failover/settings/modes/on-partner-video-fail",
    "failover_prefer_primary": "output/network/failover/settings/modes/prefer-primary",
}
