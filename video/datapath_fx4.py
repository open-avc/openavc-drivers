"""Datapath Fx4 REST control, from the manufacturer API and SDK reference.

Python associates untagged HTTP replies with their input/output, checks the
API's refusal inside HTTP 200 replies, and preserves the other transform field
when changing rotation or flip. Uses only the platform's HTTP transport.
No documented push: periodic reads and command readback supply actual state.
Settings are live until an explicit SaveSettings; automatic flash writes are
forbidden by the manufacturer's wear warning. Wall design stays in Wall Designer.
"""

from __future__ import annotations

import asyncio
import json
import time

from openavc.drivers.base import BaseDriver

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:
    CommandRefusedError = RuntimeError


class DatapathFx4Driver(BaseDriver):
    DRIVER_INFO = {
        "id": "datapath_fx4",
        "name": "Datapath Fx4",
        "manufacturer": "Datapath",
        "category": "video",
        "version": "1.0.0",
        "author": "OpenAVC",
        "transport": "http",
        "description": "Control Fx4 video walls with named source selection, per-output video, audio, "
        "image and orientation controls, and source and system diagnostics.",
        "source_url": "https://www.datapathsoftware.com/supportdownloads/windows/datapathfx4-sdk/FX4.zip",
        "ports": [80],
        "protocols": ["datapath-fx4-rest"],
        "simulated": True,
        "web_ui": False,
        "verified": False,
        "min_platform_version": "0.36.0",
        "tags": ["video-wall", "scaler", "hdr"],
        "compatible_models": [
            {
                "manufacturer": "Datapath",
                "models": ["Fx4-HDR", "Fx4", "Fx4-SDI"],
                "confidence": "untested",
                "notes": "Based on Datapath’s published Fx4 REST API. Model-specific "
                "feedback fields appear when the controller reports them. Not tested on "
                "hardware.",
            }
        ],
        "help": {
            "overview": "Control the selected source for a Datapath wall and individual output "
            "settings. Preferred Source and Active Source are separate because the "
            "controller can fall back to another input.",
            "setup": "Configure the wall layout in Datapath Wall Designer. Enter the controller’s IP "
            "address, select its model and name the three inputs. Output and input numbers "
            "match the physical 1-based numbering. Enter the control password if one is "
            "set. All changes are live until Save Configuration to Storage is used; that "
            "saves every current setting, including changes made in Wall Designer.",
            "connection": "The REST API uses unencrypted HTTP. Status reads need no password; a "
            "wrong control password prevents changes but does not prevent monitoring. "
            "No event subscription is documented; status is refreshed periodically and "
            "after commands.",
        },
        "default_config": {
            "host": "",
            "port": 80,
            "password": "",
            "poll_interval": 10,
            "inter_command_delay": 0.02,
            "model": "Fx4-HDR",
            "input_1_name": "Input 1",
            "input_2_name": "Input 2",
            "input_3_name": "Input 3",
        },
        "config_schema": {
            "host": {"type": "string", "label": "IP Address", "required": True},
            "port": {"type": "integer", "label": "HTTP Port", "default": 80, "min": 1, "max": 65535},
            "password": {
                "type": "string",
                "label": "Control Password",
                "default": "",
                "secret": True,
                "help": "Leave blank only if the controller has no password. "
                "Status reads do not require it.",
            },
            "model": {
                "type": "enum",
                "label": "Model",
                "default": "Fx4-HDR",
                "values": ["Fx4-HDR", "Fx4", "Fx4-SDI"],
                "help": "Choose the installed model. The REST API does not report its " "model name.",
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status Refresh Interval (seconds)",
                "default": 10,
                "min": 5,
                "max": 300,
                "advanced": True,
            },
            "inter_command_delay": {
                "type": "number",
                "label": "Request Spacing (seconds)",
                "default": 0.02,
                "min": 0,
                "max": 1,
                "advanced": True,
            },
            "input_1_name": {
                "type": "string",
                "label": "Input 1 Name",
                "default": "Input 1",
                "help": "Name used in source choices and feedback.",
            },
            "input_2_name": {
                "type": "string",
                "label": "Input 2 Name",
                "default": "Input 2",
                "help": "Name used in source choices and feedback.",
            },
            "input_3_name": {
                "type": "string",
                "label": "Input 3 Name",
                "default": "Input 3",
                "help": "Name used in source choices and feedback.",
            },
        },
        "state_variables": {
            "serial_number": {"type": "string", "label": "Serial Number"},
            "firmware": {"type": "string", "label": "Firmware"},
            "unit_name": {"type": "string", "label": "Device Name"},
            "preferred_input": {"type": "string", "label": "Preferred Input", "control": True},
            "active_input": {"type": "string", "label": "Active Input", "control": True},
            "active_input_name": {"type": "string", "label": "Active Source"},
            "preferred_input_name": {"type": "string", "label": "Preferred Source"},
            "source_options": {"type": "string", "label": "Source Choices", "cloud_priority": "low"},
            "scaling_mode": {"type": "enum", "label": "Scaling Optimization", "values": ["text", "video"]},
            "disable_without_signal": {"type": "boolean", "label": "Disable Outputs Without Input"},
            "temperature": {"type": "float", "label": "Temperature", "unit": "°C", "cloud_priority": "low"},
            "temperature_min": {
                "type": "float",
                "label": "Minimum Temperature",
                "unit": "°C",
                "cloud_priority": "low",
            },
            "temperature_max": {
                "type": "float",
                "label": "Maximum Temperature",
                "unit": "°C",
                "cloud_priority": "low",
            },
            "psu_io_good": {"type": "boolean", "label": "I/O Supply Good", "cloud_priority": "high"},
            "psu_1v8_good": {"type": "boolean", "label": "1.8 V Supply Good", "cloud_priority": "high"},
            "psu_1v2_good": {"type": "boolean", "label": "1.2 V Supply Good", "cloud_priority": "high"},
            "health": {"type": "string", "label": "Power Supply Health", "cloud_priority": "high"},
            "mac_address": {"type": "string", "label": "MAC Address"},
            "last_error": {"type": "string", "label": "Last Error", "cloud_priority": "high"},
        },
        "child_entity_types": {
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "integer", "min": 1, "max": 3},
                "dynamic": True,
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "width": {"type": "integer", "label": "Signal Width", "unit": "px"},
                    "height": {"type": "integer", "label": "Signal Height", "unit": "px"},
                    "colour_space": {"type": "string", "label": "Color Space"},
                    "colour_range": {"type": "string", "label": "Color Range"},
                    "subsampling": {"type": "string", "label": "Chroma " "Subsampling"},
                    "hdcp_status": {"type": "string", "label": "HDCP Status"},
                    "hdcp_version": {"type": "string", "label": "HDCP Version"},
                    "hdcp_enabled": {"type": "boolean", "label": "HDCP Enabled", "control": True},
                    "hdr_status": {"type": "string", "label": "HDR Status"},
                    "hdr_format": {"type": "string", "label": "HDR Format"},
                    "active": {"type": "boolean", "label": "Active Source", "control": True},
                },
                "summary_fields": ["active", "width", "height", "hdcp_status"],
            },
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "integer", "min": 1, "max": 4},
                "dynamic": True,
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "width": {"type": "integer", "label": "Signal Width", "unit": "px"},
                    "height": {"type": "integer", "label": "Signal Height", "unit": "px"},
                    "colour_space": {"type": "string", "label": "Color Space"},
                    "colour_range": {"type": "string", "label": "Color Range"},
                    "subsampling": {"type": "string", "label": "Chroma " "Subsampling"},
                    "hdcp_status": {"type": "string", "label": "HDCP Status"},
                    "hdcp_version": {"type": "string", "label": "HDCP Version"},
                    "enabled": {"type": "boolean", "label": "Video Enabled", "control": True},
                    "audio_enabled": {"type": "boolean", "label": "Audio Enabled", "control": True},
                    "rotation": {"type": "integer", "label": "Rotation", "unit": "degrees", "control": True},
                    "flip": {
                        "type": "enum",
                        "label": "Flip",
                        "values": ["None", "Horizontal", "Vertical"],
                        "control": True,
                    },
                    "image_source": {
                        "type": "enum",
                        "label": "Image Source",
                        "values": [
                            {"value": "InputCapture", "label": "Live " "Input"},
                            {"value": "Colourbars", "label": "Color " "Bars"},
                            {"value": "Greyscale", "label": "Grayscale"},
                            {"value": "MovingColourbars", "label": "Moving " "Color " "Bars"},
                            {"value": "MovingGreyscale", "label": "Moving " "Grayscale"},
                            {"value": "FlatColour", "label": "Flat " "Color"},
                        ],
                        "control": True,
                    },
                    "timing_source": {"type": "string", "label": "Selected Timing " "Source"},
                    "current_timing_source": {"type": "string", "label": "Active " "Timing " "Source"},
                    "genlock_source": {"type": "string", "label": "Selected " "Genlock " "Source"},
                    "crop_left": {
                        "type": "float",
                        "label": "Crop Left",
                        "help": "Fraction of the "
                        "complete input "
                        "image; 0 is the "
                        "start and 1 is the "
                        "end.",
                    },
                    "crop_top": {
                        "type": "float",
                        "label": "Crop Top",
                        "help": "Fraction of the "
                        "complete input image; "
                        "0 is the start and 1 "
                        "is the end.",
                    },
                    "crop_right": {
                        "type": "float",
                        "label": "Crop Right",
                        "help": "Fraction of the "
                        "complete input "
                        "image; 0 is the "
                        "start and 1 is the "
                        "end.",
                    },
                    "crop_bottom": {
                        "type": "float",
                        "label": "Crop Bottom",
                        "help": "Fraction of the "
                        "complete input "
                        "image; 0 is the "
                        "start and 1 is the "
                        "end.",
                    },
                },
                "summary_fields": ["enabled", "image_source", "width", "height"],
            },
        },
        "device_settings": {
            "unit_name": {
                "type": "string",
                "label": "Device Name",
                "state_key": "unit_name",
                "help": "Name shown by the controller. Choose Save "
                "Configuration to Storage to retain changes after "
                "power-off.",
            },
            "scaling_mode": {
                "type": "enum",
                "label": "Scaling Optimization",
                "state_key": "scaling_mode",
                "values": [{"value": "text", "label": "Text"}, {"value": "video", "label": "Video"}],
                "help": "Optimize scaling for text or video. Save "
                "Configuration to Storage retains the change.",
            },
            "disable_without_signal": {
                "type": "boolean",
                "label": "Disable Outputs Without Input",
                "state_key": "disable_without_signal",
                "help": "Stop output signals when no input is "
                "available. Save Configuration to Storage "
                "retains the change.",
            },
        },
        "commands": {
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "query_for": "active_input",
                "help": "Read the current source, port settings and diagnostics from the " "controller.",
            },
            "set_preferred_input": {
                "label": "Select Preferred Source",
                "params": {
                    "input": {
                        "type": "string",
                        "label": "Source",
                        "required": True,
                        "options_state": "source_options",
                    }
                },
                "sets": {"preferred_input": "{input}"},
                "help": "Prefer this input for the entire wall. If it has no "
                "signal, the controller selects another available "
                "input. Active Source shows what is actually "
                "displayed.",
            },
            "save_configuration": {
                "label": "Save Configuration to Storage",
                "params": {},
                "confirm": "Save all current controller settings for the next " "power-on?",
                "help": "Save all current settings to the controller. Use "
                "after commissioning or an intentional change, not "
                "repeatedly in an automation.",
            },
            "set_output_enabled": {
                "label": "Set Output Video",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "required": True,
                        "child_type": "output",
                    },
                    "value": {"type": "boolean", "label": "Enabled", "required": True},
                },
                "help": "Enable or disable video on one output.",
                "sets": {"enabled": "{value}"},
            },
            "set_output_audio": {
                "label": "Set Output Audio",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "required": True,
                        "child_type": "output",
                    },
                    "value": {"type": "boolean", "label": "Enabled", "required": True},
                },
                "help": "Enable or disable audio on one output, when supported.",
                "sets": {"audio_enabled": "{value}"},
            },
            "set_input_hdcp": {
                "label": "Set Input HDCP",
                "params": {
                    "input": {"type": "child_id", "label": "Input", "required": True, "child_type": "input"},
                    "value": {"type": "boolean", "label": "Enabled", "required": True},
                },
                "help": "Enable or disable HDCP negotiation on one input. "
                "Changing it can interrupt the source.",
                "sets": {"hdcp_enabled": "{value}"},
            },
            "set_output_rotation": {
                "label": "Set Output Rotation",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "required": True,
                        "child_type": "output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "Rotation",
                        "required": True,
                        "values": [
                            {"value": 0, "label": "0°"},
                            {"value": 90, "label": "90°"},
                            {"value": 180, "label": "180°"},
                            {"value": 270, "label": "270°"},
                        ],
                    },
                },
                "help": "Rotate one output clockwise while preserving its " "flip setting.",
                "sets": {"rotation": "{value}"},
            },
            "set_output_flip": {
                "label": "Set Output Flip",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "required": True,
                        "child_type": "output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "Flip",
                        "required": True,
                        "values": [
                            {"value": "None", "label": "None"},
                            {"value": "Horizontal", "label": "Horizontal"},
                            {"value": "Vertical", "label": "Vertical"},
                        ],
                    },
                },
                "help": "Flip one output while preserving its rotation.",
                "sets": {"flip": "{value}"},
            },
            "set_output_image": {
                "label": "Set Output Image",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "required": True,
                        "child_type": "output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "Image",
                        "required": True,
                        "values": [
                            {"value": "InputCapture", "label": "Live Input"},
                            {"value": "Colourbars", "label": "Color Bars"},
                            {"value": "Greyscale", "label": "Grayscale"},
                            {"value": "MovingColourbars", "label": "Moving Color Bars"},
                            {"value": "MovingGreyscale", "label": "Moving Grayscale"},
                            {"value": "FlatColour", "label": "Flat Color"},
                        ],
                    },
                },
                "help": "Show live input or a test pattern on one output. Flat "
                "Color uses the color configured in Wall Designer.",
                "sets": {"image_source": "{value}"},
            },
            "set_output_timing_source": {
                "label": "Set Output Timing Source",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "required": True,
                        "child_type": "output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "Timing Source",
                        "required": True,
                        "values": [
                            {"value": "Monitor", "label": "Connected " "Monitor"},
                            {"value": "Default", "label": "Configured " "Default"},
                        ],
                    },
                },
                "help": "Use the monitor’s preferred timing or the "
                "default timing configured in Wall Designer.",
                "sets": {"timing_source": "{value}"},
            },
            "set_output_genlock": {
                "label": "Set Output Genlock Source",
                "params": {
                    "output": {
                        "type": "child_id",
                        "label": "Output",
                        "required": True,
                        "child_type": "output",
                    },
                    "value": {
                        "type": "enum",
                        "label": "Genlock Source",
                        "required": True,
                        "values": [
                            {"value": "None", "label": "Free Running"},
                            {"value": "Sync", "label": "External Sync"},
                            {"value": "Input", "label": "Active Input"},
                            {"value": "Input0", "label": "Input 1"},
                            {"value": "Input1", "label": "Input 2"},
                            {"value": "Input2", "label": "Input 3"},
                        ],
                    },
                },
                "help": "Choose the reference this output attempts to lock "
                "to. Source availability depends on the model and "
                "connected signals.",
                "sets": {"genlock_source": "{value}"},
            },
        },
        "quick_actions": ["set_preferred_input", "set_output_enabled", "set_output_audio"],
        "discovery": {
            "tcp_probe": {
                "port": 80,
                "send_ascii": "GET /SystemInfo.cgx HTTP/1.0\r\n\r\n",
                "expect_regex": '(?s)"FirmwareVersion"\\s*:\\s*\\d+.*"ConfigVersion"\\s*:\\s*\\d+.*"HardwareVersion"\\s*:\\s*\\d+.*"FPGAVersion"\\s*:\\s*\\d+',
                "timeout_ms": 2000,
            }
        },
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._lock = asyncio.Lock()
        self._slow_due = 0.0
        self._schemas = {}
        self._answers = {}
        self._unsupported = set()
        self._read_errors = {}

    async def _post_connect(self):
        self._answers.clear()
        self._unsupported.clear()
        self._read_errors.clear()
        self._slow_due = 0.0
        answer = await self._request("SerialNumber")
        if not isinstance(answer.get("SerialNumber"), str):
            raise ConnectionError("The address did not answer with an Fx4 serial number.")
        self.set_state("serial_number", answer["SerialNumber"])
        self._register_ports()

    def _register_ports(self):
        for kind, count in (("input", 3), ("output", 4)):
            for port in range(1, count + 1):
                self.deregister_child(kind, port)
                fields = self.DRIVER_INFO["child_entity_types"][kind]["state_variables"]
                schema = {"name": fields["name"]}
                if kind == "input":
                    schema["active"] = fields["active"]
                self._schemas[kind, port] = schema
                self.register_child(kind, port, {"name": self._port_name(kind, port)}, schema=schema)
        choices = [{"value": "auto", "label": "Automatic"}]
        choices.extend({"value": str(n), "label": self._port_name("input", n)} for n in range(1, 4))
        self.set_state("source_options", json.dumps(choices))

    def _port_name(self, kind, port):
        return (
            str(self.config.get(f"input_{port}_name") or f"Input {port}")
            if kind == "input"
            else f"Output {port}"
        )

    async def _initial_sync(self):
        await self.poll()

    async def _liveness_probe(self):
        async with self._lock:
            try:
                await self._request("SerialNumber")
            except Fx4Refused:
                pass  # An API refusal still proves that the controller answered.

    async def _request(self, endpoint, kind=None, port=None, body=None):
        path = f"/{endpoint}.cgx"
        if body is None:
            params = {kind: port - 1} if kind else None
            response = await self.transport.get(path, params=params)
        else:
            response = await self.transport.post(path, body=self._write_body(body))
        return self._checked_response(endpoint, response, writing=body is not None)

    def _write_body(self, body):
        payload = dict(body)
        if self.config.get("password"):
            payload["Password"] = self.config["password"]
        return payload

    def _checked_response(self, endpoint, response, writing):
        data = response.json_data
        if response.status_code != 200 or not isinstance(data, dict):
            raise Fx4Refused(
                f"{endpoint}: HTTP {response.status_code} or invalid JSON.", response.status_code == 404
            )
        if data.get("Success") is False:
            self._raise_refusal(endpoint, data)
        if writing and data.get("Success") is not True:
            raise Fx4Refused(f"{endpoint}: the controller did not confirm the write.")
        return data

    def _raise_refusal(self, endpoint, data):
        # Never echo the response body: it could contain the control password.
        message = str(data.get("ErrorMessage", "The controller refused the request."))
        password = str(self.config.get("password") or "")
        if password:
            message = message.replace(password, "[password]")
        unsupported = "not supported" in message.lower() or "unsupported" in message.lower()
        raise Fx4Refused(f"{endpoint}: {message}", unsupported)

    async def _read(self, endpoint, kind=None, port=None):
        answer = await self._request(endpoint, kind, port)
        key = endpoint, kind, port
        self._answers[key] = answer
        self._read_errors.pop(key, None)
        self._publish(endpoint, answer, kind, port)
        return answer

    async def _optional_read(self, endpoint, kind=None, port=None):
        key = endpoint, kind, port
        if key in self._unsupported:
            return
        try:
            await self._read(endpoint, kind, port)
        except Fx4Refused as exc:
            self._clear_unread_fields(endpoint, kind, port)
            if exc.unsupported:
                self._unsupported.add(key)
            else:
                where = f"{kind} {port}: " if kind else ""
                self._read_errors[key] = where + str(exc)

    def _clear_unread_fields(self, endpoint, kind, port):
        if kind:
            known = self._schemas[kind, port]
            values = {prop: None for prop in PORT_READS[endpoint].values() if prop in known}
            self.set_child_state_batch(kind, port, values)

    def _publish(self, endpoint, answer, kind, port):
        if kind:
            fields = PORT_READS[endpoint]
            values = _mapped_fields(answer, fields)
            self._publish_port(kind, port, values)
        elif endpoint in ("ActiveInput", "PreferredInput"):
            self._publish_source(endpoint, answer)
        elif endpoint == "SystemHealth":
            self._publish_health(answer)
        else:
            self._publish_parent(endpoint, answer)

    def _publish_parent(self, endpoint, answer):
        values = _mapped_fields(answer, PARENT_READS[endpoint])
        if "firmware" in values:
            number = values["firmware"]
            values["firmware"] = f"{(number >> 16) & 255}.{(number >> 8) & 255}.{number & 255}"
        if "scaling_mode" in values:
            values["scaling_mode"] = "video" if values["scaling_mode"] else "text"
        self.set_states(values)

    def _publish_port(self, kind, port, values):
        fields = self.DRIVER_INFO["child_entity_types"][kind]["state_variables"]
        schema = self._schemas[kind, port]
        new_fields = set(values) - schema.keys()
        if new_fields:
            previous = {prop: self.get_child_state(kind, port).get(prop) for prop in schema}
            schema = {**schema, **{prop: fields[prop] for prop in new_fields}}
            self._schemas[kind, port] = schema
            self.deregister_child(kind, port)
            self.register_child(kind, port, {**previous, **values}, schema=schema)
        else:
            self.set_child_state_batch(kind, port, values)

    def _publish_source(self, endpoint, answer):
        if "Input" not in answer:
            return
        raw = answer["Input"]
        if not _input_number_valid(raw):
            raise Fx4Refused(f"{endpoint}: invalid input number.")
        active = endpoint == "ActiveInput"
        key = "active_input" if active else "preferred_input"
        value = str(raw + 1) if raw is not None else ("none" if active else "auto")
        label = (
            self._port_name("input", raw + 1)
            if raw is not None
            else ("No Active Input" if active else "Automatic")
        )
        self.set_states({key: value, f"{key}_name": label})
        if active:
            for port in range(1, 4):
                self.set_child_state("input", port, "active", raw == port - 1)

    def _publish_health(self, answer):
        supplies = answer.get("PowerSupply", {})
        temperatures = answer.get("Temperature", {})
        values = {
            prop: temperatures[field] for field, prop in TEMPERATURE_FIELDS.items() if field in temperatures
        }
        values.update({prop: supplies[field] for field, prop in SUPPLY_FIELDS.items() if field in supplies})
        if all(field in supplies for field in SUPPLY_FIELDS):
            failed = [field for field in SUPPLY_FIELDS if supplies[field] is False]
            values["health"] = "Supply fault: " + ", ".join(failed) if failed else "Power supplies normal"
        self.set_states(values)

    async def poll(self):
        async with self._lock:
            await self._read("SerialNumber")
            slow = time.monotonic() >= self._slow_due
            for endpoint in ("ActiveInput", "PreferredInput", "SystemHealth"):
                await self._optional_read(endpoint)
            if slow:
                for endpoint in (
                    "SystemInfo",
                    "FriendlyName",
                    "NetworkSettings",
                    "Smoothness",
                    "OutputNoSignal",
                ):
                    await self._optional_read(endpoint)
            await self._poll_ports(slow)
            if slow:
                self._slow_due = time.monotonic() + 60
            if self._read_errors:
                self.set_state("last_error", "Status not updated: " + "; ".join(self._read_errors.values()))

    async def _poll_ports(self, slow):
        for kind, count in (("input", 3), ("output", 4)):
            endpoints = _port_queries(kind, slow)
            for port in range(1, count + 1):
                for endpoint in endpoints:
                    await self._optional_read(endpoint, kind, port)

    async def refresh_children(self):
        self._unsupported.clear()
        self._slow_due = 0.0
        await self.poll()

    async def send_command(self, command, params=None):
        params = params or {}
        if command == "refresh":
            return await self.refresh_children()
        async with self._lock:
            try:
                return await self._command(command, params)
            except Fx4Refused as exc:
                self.set_state("last_error", str(exc))
                raise CommandRefusedError(str(exc)) from exc

    async def _command(self, command, params):
        if command == "save_configuration":
            await self._request("SaveSettings", body={})
            return "The controller confirmed that all current settings were saved."
        if command == "set_preferred_input":
            return await self._select_input(params)
        return await self._port_command(command, params)

    async def _select_input(self, params):
        value = str(params.get("input", ""))
        if value not in ("auto", "1", "2", "3"):
            raise ValueError("Choose Automatic or an input from 1 to 3.")
        wire = None if value == "auto" else int(value) - 1
        await self._write_read("PreferredInput", {"Input": wire})
        await self._read("ActiveInput")

    async def _port_command(self, command, params):
        if command not in PORT_WRITES:
            raise ValueError(f"Unknown command: {command}")
        endpoint, field, kind = PORT_WRITES[command]
        port = _port_number(params.get(kind), 3 if kind == "input" else 4)
        value = _command_value(self.DRIVER_INFO["commands"][command]["params"]["value"], params.get("value"))
        body = {kind.capitalize(): port - 1, field: value}
        if endpoint == "OutputTransform":
            body = await self._transform_body(port, field, value)
        await self._write_read(endpoint, body, kind, port)
        if endpoint == "OutputTimingSource":
            await self._optional_read("OutputCurrentTimingSource", kind, port)
            await self._optional_read("OutputCurrentTimings", kind, port)

    async def _transform_body(self, port, field, value):
        current = await self._read("OutputTransform", "output", port)
        if not all(key in current for key in ("Rotation", "Flip")):
            raise Fx4Refused(
                "The controller did not report both rotation and flip; orientation was not changed."
            )
        return {"Output": port - 1, "Rotation": current["Rotation"], "Flip": current["Flip"], field: value}

    async def _write_read(self, endpoint, body, kind=None, port=None):
        await self._request(endpoint, body=body)
        answer = await self._read(endpoint, kind, port)
        expected = {
            key: value for key, value in body.items() if key not in ("Input", "Output") or kind is None
        }
        if any(key not in answer or answer[key] != value for key, value in expected.items()):
            raise Fx4Refused(
                f"{endpoint}: the controller acknowledged the command but did not report the requested value. Check its current state."
            )
        self.set_state("last_error", "")

    async def set_device_setting(self, key, value):
        if key not in SETTINGS:
            raise ValueError(f"Unknown device setting: {key}")
        endpoint, field = SETTINGS[key]
        definition = self.DRIVER_INFO["device_settings"][key]
        value = _command_value(definition, value)
        if key == "scaling_mode":
            value = 1 if value == "video" else 0
        async with self._lock:
            try:
                await self._write_read(endpoint, {field: value})
            except Fx4Refused as exc:
                self.set_state("last_error", str(exc))
                raise CommandRefusedError(str(exc)) from exc


class Fx4Refused(RuntimeError):
    def __init__(self, message, unsupported=False):
        super().__init__(message)
        self.unsupported = unsupported


def _port_number(value, maximum):
    if isinstance(value, bool) or str(value) not in tuple(str(n) for n in range(1, maximum + 1)):
        raise ValueError(f"Choose a port from 1 to {maximum}.")
    return int(value)


def _command_value(definition, value):
    kind = definition["type"]
    if kind == "boolean":
        return _boolean(value)
    if kind == "enum":
        return _choice(definition["values"], value)
    if not isinstance(value, str):
        raise ValueError("Enter a text value.")
    return value


def _boolean(value):
    if type(value) is bool:
        return value
    if value in ("true", "false"):
        return value == "true"
    raise ValueError("Choose Enabled or Disabled.")


def _choice(choices, value):
    allowed = [item["value"] if isinstance(item, dict) else item for item in choices]
    for candidate in allowed:
        if str(value) == str(candidate):
            return candidate
    raise ValueError("Choose one of the listed values.")


def _mapped_fields(answer, fields):
    return {prop: answer[field] for field, prop in fields.items() if field in answer}


def _input_number_valid(value):
    return value is None or type(value) is int and value in range(3)


def _port_queries(kind, slow):
    matching = [endpoint for endpoint in PORT_READS if endpoint.lower().startswith(kind)]
    return matching if slow else [endpoint for endpoint in matching if endpoint in FAST_PORT_READS]


PARENT_READS = {
    "SerialNumber": {"SerialNumber": "serial_number"},
    "SystemInfo": {"FirmwareVersion": "firmware"},
    "FriendlyName": {"FriendlyName": "unit_name"},
    "NetworkSettings": {"MACAddress": "mac_address"},
    "Smoothness": {"Smoothness": "scaling_mode"},
    "OutputNoSignal": {"NoSignal": "disable_without_signal"},
}
TIMING_FIELDS = {
    "HorAddrTime": "width",
    "VerAddrTime": "height",
    "ColourSpace": "colour_space",
    "ColourRange": "colour_range",
    "Subsampling": "subsampling",
}
PORT_READS = {
    "InputCurrentTimings": TIMING_FIELDS,
    "InputHDCPEnable": {"Enable": "hdcp_enabled"},
    "InputHDCPStatus": {"Status": "hdcp_status"},
    "InputHDCPExtStatus": {"Status": "hdcp_version"},
    "InputHDRStatus": {"Status": "hdr_status"},
    "InputHDRVersion": {"Status": "hdr_format"},
    "OutputCurrentTimings": TIMING_FIELDS,
    "OutputEnable": {"Enable": "enabled"},
    "OutputAudioEnable": {"Enable": "audio_enabled"},
    "OutputTransform": {"Rotation": "rotation", "Flip": "flip"},
    "OutputImageSource": {"Source": "image_source"},
    "OutputTimingSource": {"Source": "timing_source"},
    "OutputCurrentTimingSource": {"Source": "current_timing_source"},
    "OutputGenlockSource": {"GenlockSource": "genlock_source"},
    "OutputHDCPStatus": {"Status": "hdcp_status"},
    "OutputHDCPExtStatus": {"Status": "hdcp_version"},
    "OutputCrop": {"X0": "crop_left", "Y0": "crop_top", "X1": "crop_right", "Y1": "crop_bottom"},
}
FAST_PORT_READS = {
    "InputCurrentTimings",
    "InputHDCPStatus",
    "InputHDRStatus",
    "OutputEnable",
    "OutputAudioEnable",
    "OutputImageSource",
    "OutputCurrentTimings",
    "OutputHDCPStatus",
}
TEMPERATURE_FIELDS = {"Average": "temperature", "Minimum": "temperature_min", "Maximum": "temperature_max"}
SUPPLY_FIELDS = {"IO": "psu_io_good", "1.8V": "psu_1v8_good", "1.2V": "psu_1v2_good"}
PORT_WRITES = {
    "set_output_enabled": ("OutputEnable", "Enable", "output"),
    "set_output_audio": ("OutputAudioEnable", "Enable", "output"),
    "set_input_hdcp": ("InputHDCPEnable", "Enable", "input"),
    "set_output_rotation": ("OutputTransform", "Rotation", "output"),
    "set_output_flip": ("OutputTransform", "Flip", "output"),
    "set_output_image": ("OutputImageSource", "Source", "output"),
    "set_output_timing_source": ("OutputTimingSource", "Source", "output"),
    "set_output_genlock": ("OutputGenlockSource", "GenlockSource", "output"),
}
SETTINGS = {
    "unit_name": ("FriendlyName", "FriendlyName"),
    "scaling_mode": ("Smoothness", "Smoothness"),
    "disable_without_signal": ("OutputNoSignal", "NoSignal"),
}
