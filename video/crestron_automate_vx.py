"""
OpenAVC Crestron Automate VX Driver (Automate VX API).

Controls a Crestron 1 Beyond Automate VX voice-activated camera switching
system (IV-SAM-VX2-S, and the rest of the Automate VX series the API covers)
through the REST API its server publishes: wake and sleep, AutoSwitch,
recording, streaming and live output, layouts, room configurations, scenarios
and the home shot, manual camera selection, camera presets, pan, tilt and zoom,
copying recordings off the unit, and the system's health report. Every camera
the current room configuration lists is a child entity, with its model,
address, whether it is on the live shot and whether the Automate VX can reach
it.

Push vs poll
------------
Poll only. The API documents no subscription, event or callback. Every
``poll_interval`` (default 5 s) the driver asks GetAllStatus (AutoSwitch,
recording, ISO recording, streaming, output, copying, recording space, the
camera list, the live camera, the layouts and the active layout, the room
configurations and the active one, in one request), ShotStatus and
ScenarioStatus; every 30 s GetScenarios and HealthStatus. Every command reads
what it changed back at once, and a change the system accepted and does not
show yet is read again every second for up to 10 s.

Why Python
----------
Sign-in is a token the system issues: ``POST /get-token`` with the user name
and password base64-encoded in a bare ``Authorization`` header (no ``Basic``
word), then that token as the bare ``Authorization`` header of every call. No
YAML ``auth`` type obtains a token. The four status calls answer the same body
(``{"status": "OK", "results": true, "message": ...}``), and a YAML HTTP
response rule does not know which path it answers. A refusal comes back in
the body (``{"status": "Error", "err": ...}``); the document gives no HTTP
status for it, and a YAML command reports success for any 2xx. A command is
answered with a message and never the new value, and the system reports
nothing by itself, so every write is read back by a second call. GetAllStatus
nests each status as a JSON string to decode, and the camera list becomes a
child roster that follows the room configuration.

Protocol
--------
``https://<server>:4443/api/<Call>``, every call a ``POST`` with
``Content-Type: application/json`` and a JSON body (empty when the call takes
no parameters). Values in a body are sent as JSON strings, as the document's
body examples write them; its parameter tables call the numeric ones
integers. ``/get-token`` sits at the root, not under ``/api``. Tokens do not
expire but are bound to the address that asked, so a refused token is
replaced once by signing in again.

Firmware 6.4.2 moved the API to port 4443 over HTTPS (the document's What's
New; the product manual opens the web interface at ``https://<ip>:4443``).
Earlier firmware served it over HTTP: turn Use HTTPS off and set the port the
unit's web page opens on (the product manual's System Configuration topic
gives 3579).

The document and its own examples disagree on several reply shapes, and the
driver reads both forms: LayoutStatus's ``layout`` as an object or a
one-element list, RoomConfigStatus's ``roomConfig`` or ``roomConfigs``, a
camera's ``name`` or ``model``, numbers as JSON numbers or strings. It reads
the ``results`` booleans, never the message wording, which differs between
the call's own topic and the GetAllStatus example.

Source:
    API for 1 Beyond Automate VX Series (Crestron), current as of firmware
    6.4.1.8: https://sdkcon78221.crestron.com/sdk/Automate-VX-API/Content/Topics/Automate-API/Home.htm
    IV-SAM-VX and IV-SAM-VX2 Series product manual (Crestron doc 9487, current
    as of 6.7): https://docs.crestron.com/en-us/9487/Content/Topics/Home.htm
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1
    CommandRefusedError = RuntimeError

log = get_logger(__name__)

TOKEN_PATH = "/get-token"
API = "/api/"

# The read every poll makes first, at connect too. A unit whose firmware has
# no GetAllStatus (it arrived in 5.8) is read call by call instead.
ALL_STATUS = "GetAllStatus"
FAST = ("ShotStatus", "ScenarioStatus")
SLOW = ("GetScenarios", "HealthStatus")
# GetAllStatus's thirteen calls, asked one at a time when it is missing.
ALL_STATUS_PARTS_FAST = (
    "AutoSwitchStatus", "RecordStatus", "StreamStatus", "OutputStatus",
    "CameraStatus", "LayoutStatus", "RoomConfigStatus",
)
ALL_STATUS_PARTS_SLOW = ("GetCameras", "GetLayouts", "GetRoomConfigs", "RecordingSpaceAvail")
SLOW_INTERVAL_S = 30.0
# The cheap read the liveness probe makes.
PROBE = "AutoSwitchStatus"

# After a command the system accepted, read again this often for this long
# until it shows the change.
WATCH_STEP_S = 1.0
WATCH_S = 10.0

# What a refused token says. The document says only that "an error message
# [is] returned indicating that the credentials are not valid".
_TOKEN_REFUSED = re.compile(r"credential|token|unauthori[sz]ed|not authori[sz]ed", re.IGNORECASE)

API_LABELS = {
    "AutoSwitchStatus": "the AutoSwitch status",
    "RecordStatus": "the recording status",
    "ISORecordStatus": "the ISO recording status",
    "StreamStatus": "the streaming status",
    "OutputStatus": "the output status",
    "CopyStatus": "the copy status",
    "RecordingSpaceAvail": "the recording space",
    "GetCameras": "the camera list",
    "CameraStatus": "the live camera",
    "GetLayouts": "the layout list",
    "LayoutStatus": "the active layout",
    "GetRoomConfigs": "the room configuration list",
    "RoomConfigStatus": "the active room configuration",
    "ShotStatus": "the shot status",
    "ScenarioStatus": "the active scenario",
    "GetScenarios": "the scenario list",
    "HealthStatus": "the health report",
    "GetAllStatus": "its status",
}

PT_DIRECTIONS = [
    {"value": "0", "label": "Up"},
    {"value": "1", "label": "Up Right"},
    {"value": "2", "label": "Right"},
    {"value": "3", "label": "Down Right"},
    {"value": "4", "label": "Down"},
    {"value": "5", "label": "Down Left"},
    {"value": "6", "label": "Left"},
    {"value": "7", "label": "Up Left"},
]
ZOOM_DIRECTIONS = [
    {"value": "0", "label": "In"},
    {"value": "1", "label": "Out"},
]

# Commands with no parameters: (API call, label, read back, expected value).
SIMPLE: dict[str, tuple[str, str, tuple[str, ...], tuple[str, Any] | None]] = {
    "wake": ("Wake", "Wake", (ALL_STATUS,), ("output", True)),
    "sleep": ("Sleep", "Sleep", (ALL_STATUS,), ("output", False)),
    "start_autoswitch": ("StartAutoSwitch", "Start AutoSwitch", ("AutoSwitchStatus",), ("autoswitch", True)),
    "stop_autoswitch": ("StopAutoSwitch", "Stop AutoSwitch", ("AutoSwitchStatus",), ("autoswitch", False)),
    "start_recording": ("StartRecord", "Start Recording", ("RecordStatus",), ("recording", True)),
    "stop_recording": ("StopRecord", "Stop Recording", ("RecordStatus",), ("recording", False)),
    # Whether RecordStatus reads true while paused is not documented.
    "pause_recording": ("PauseRecord", "Pause Recording", ("RecordStatus",), None),
    "start_streaming": ("StartStream", "Start Streaming", ("StreamStatus",), ("streaming", True)),
    "stop_streaming": ("StopStream", "Stop Streaming", ("StreamStatus",), ("streaming", False)),
    "start_output": ("StartOutput", "Start Output", ("OutputStatus",), ("output", True)),
    "stop_output": ("StopOutput", "Stop Output", ("OutputStatus",), ("output", False)),
    "go_home": ("GoHome", "Go Home", (ALL_STATUS, "ShotStatus", "ScenarioStatus"), None),
    "export_camera_presets": ("ExportCameraPresets", "Export Camera Presets", (), None),
    "import_camera_presets": ("ImportCameraPresets", "Import Camera Presets", (), None),
    "restart_wirecast": ("CloseWirecast", "Restart Wirecast", (ALL_STATUS,), None),
    "restart": ("Restart", "Restart", (), None),
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _as_float(value: Any) -> float | None:
    """A number, or the number at the start of text such as "25.33%" or
    "316.98 GB"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(value or ""))
    return float(match.group(1)) if match else None


def _one(value: Any) -> dict[str, Any] | None:
    """An object, or the first object of a list (the document shows both)."""
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _options(items: list[tuple[Any, str]]) -> str:
    return json.dumps([{"value": value, "label": label} for value, label in items])


class ApiRefused(Exception):
    """The system answered a call with an error, or with something that is
    not the API."""

    def __init__(self, message: str, *, http_status: int = 0) -> None:
        super().__init__(message)
        self.http_status = http_status


class _TokenRefused(Exception):
    """The system refused the token a call carried."""


class CrestronAutomateVXDriver(BaseDriver):
    """Crestron 1 Beyond Automate VX over the Automate VX API (token sign-in)."""

    DRIVER_INFO = {
        "id": "crestron_automate_vx",
        "name": "Crestron Automate VX",
        "manufacturer": "Crestron",
        "category": "video",
        "version": "1.0.0",
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls a Crestron 1 Beyond Automate VX voice-activated camera "
            "switching system over its REST API: wake and sleep, AutoSwitch, "
            "recording, streaming and output, layouts, room configurations and "
            "scenarios, manual camera selection, camera presets and pan, tilt and "
            "zoom, and the system's health report. Each camera in the current room "
            "configuration appears as a child with its model, address and status."
        ),
        "source_url": "https://sdkcon78221.crestron.com/sdk/Automate-VX-API/Content/Topics/Automate-API/Home.htm",
        "tags": [
            "crestron", "1-beyond", "automate-vx", "camera-switching", "voice-tracking",
            "auto-switching", "recording", "streaming", "lecture-capture", "rest",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["crestron_automate_vx_api"],
        "ports": [4443],
        "transport": "http",
        "discovery": {
            # The API's port. No identifying probe: every call but the sign-in
            # needs a token, and no document shows what the server answers
            # without one or names its certificate. The server is PC hardware,
            # so no MAC block is the manufacturer's to claim.
            "port_open": [4443],
        },
        "compatible_models": [
            {
                "manufacturer": "Crestron",
                "models": ["IV-SAM-VX2-S", "IV-SAM-VX2-P"],
                "confidence": "untested",
                "notes": (
                    "Automate VX Series 2 (Standard and Pro), firmware 6.4.2 or newer: "
                    "the API on HTTPS port 4443, as Crestron's API reference (current "
                    "as of 6.4.1.8) and the product manual (current as of 6.7) give "
                    "it. Not yet tested on this hardware."
                ),
            },
            {
                "manufacturer": "Crestron",
                "models": ["IV-SAM-VXP-1B", "IV-SAM-VSX-1B"],
                "confidence": "untested",
                "notes": (
                    "The first-generation Automate VX systems the API reference names, "
                    "on the same API. Firmware before 6.4.2, on any Automate VX, serves "
                    "the API over HTTP: turn Use HTTPS off and enter the port its web "
                    "page opens on (3579 in the product manual). Not yet tested on this "
                    "hardware."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls a Crestron Automate VX camera switching system: wake and "
                "sleep, AutoSwitch, recording, streaming and output, layouts, room "
                "configurations, scenarios and the home shot, choosing a camera by "
                "hand, camera presets, and pan, tilt and zoom. Each camera in the "
                "current room configuration appears as a child entity with its "
                "model, address, whether it is on the live shot and whether the "
                "Automate VX can reach it. The health report (camera, microphone "
                "and switcher reachability, camera firmware, processor use and free "
                "storage) is read every 30 seconds. The Automate VX reports no sleep "
                "state: after Sleep, output, AutoSwitch, recording and streaming "
                "read off."
            ),
            "setup": (
                "1. Sign in to the Automate VX's web page (https://<address>:4443) "
                "once as admin. A unit in its factory state asks for a new password "
                "at the first sign-in.\n"
                "2. Enter the Automate VX's IP address here with that user name and "
                "password. Another user works too, but only for what its "
                "permissions allow.\n"
                "3. Leave Use HTTPS on and Port at 4443 for firmware 6.4.2 and "
                "newer. Older firmware serves the API over HTTP: turn Use HTTPS off "
                "and enter the port the web page opens on (3579 in Crestron's "
                "manual).\n"
                "4. Leave Verify SSL Certificate off unless the Automate VX has a "
                "certificate this computer trusts.\n"
                "5. Keep the OpenAVC server's address fixed: the Automate VX ties "
                "its sign-in to the address that asked, and the driver signs in "
                "again if that changes."
            ),
        },
        "default_config": {
            "host": "",
            "port": 4443,
            "ssl": True,
            "verify_ssl": False,
            "username": "admin",
            "password": "",
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer",
                "default": 4443,
                "min": 1,
                "max": 65535,
                "label": "Port",
                "description": "The API port: 4443 on firmware 6.4.2 and newer.",
            },
            "ssl": {
                "type": "boolean",
                "default": True,
                "label": "Use HTTPS",
                "description": "On for firmware 6.4.2 and newer. Older firmware serves the API over HTTP.",
            },
            "verify_ssl": {
                "type": "boolean",
                "default": False,
                "label": "Verify SSL Certificate",
                "description": "Leave off unless the Automate VX has a certificate this computer trusts.",
            },
            "username": {
                "type": "string",
                "default": "admin",
                "required": True,
                "label": "Username",
            },
            "password": {
                "type": "string",
                "default": "",
                "required": True,
                "label": "Password",
                "secret": True,
                "description": "The password of that user on the Automate VX's web page.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 2,
                "max": 300,
                "label": "Poll Interval (s)",
                "description": "How often the status is read. The health report and the scenario list are read every 30 s.",
            },
        },
        "state_variables": {
            "autoswitch": {
                "type": "boolean",
                "label": "AutoSwitch",
                "help": "True while the Automate VX switches cameras by itself, following whoever is speaking.",
                "control": True,
                "cloud_priority": "high",
            },
            "recording": {
                "type": "boolean",
                "label": "Recording",
                "help": "True while the Automate VX records the program.",
                "control": True,
                "cloud_priority": "high",
            },
            "iso_recording": {
                "type": "boolean",
                "label": "ISO Recording",
                "help": "True while the Automate VX records each camera on its own (ISO recordings).",
            },
            "streaming": {
                "type": "boolean",
                "label": "Streaming",
                "help": "True while Wirecast streams to its configured destination.",
                "control": True,
                "cloud_priority": "high",
            },
            "output": {
                "type": "boolean",
                "label": "Output",
                "help": "True while the Automate VX sends the camera feed out through Wirecast.",
                "control": True,
                "cloud_priority": "high",
            },
            "copying": {
                "type": "boolean",
                "label": "Copying Recordings",
                "help": "True while recordings are being copied off the Automate VX.",
            },
            "live_camera": {
                "type": "integer",
                "label": "Live Camera",
                "help": "The camera on the live shot (its number in the room configuration).",
                "min": 1,
                "max": 255,
                "cloud_priority": "high",
            },
            "live_cameras": {
                "type": "string",
                "label": "Live Cameras",
                "help": "Every camera on the live shot, comma-separated (two in a side-by-side shot).",
            },
            "shot_camera_1": {
                "type": "integer",
                "label": "Shot Camera 1",
                "help": "The first camera ShotStatus reports for the current shot; 0 when it reports none.",
            },
            "shot_camera_2": {
                "type": "integer",
                "label": "Shot Camera 2",
                "help": "The second camera of a side-by-side shot, as ShotStatus reports it; 0 when there is none.",
            },
            "shot_name": {
                "type": "string",
                "label": "Shot Name",
                "help": "The shot name ShotStatus reports; empty when it reports none.",
            },
            "stored_layout": {
                "type": "string",
                "label": "Stored Layout",
                "help": "The layout ShotStatus reports as stored (the previous layout, for side-by-side shots).",
            },
            "layout": {
                "type": "string",
                "label": "Layout",
                "help": "The active layout's letter (A to Z).",
                "control": True,
            },
            "layout_name": {"type": "string", "label": "Layout Name"},
            "room_config": {
                "type": "integer",
                "label": "Room Configuration",
                "help": "The active room configuration's number (1 to 99).",
                "min": 1,
                "max": 99,
                "control": True,
            },
            "room_config_name": {"type": "string", "label": "Room Configuration Name"},
            "scenario": {
                "type": "integer",
                "label": "Scenario",
                "help": "The number of the scenario last called.",
                "control": True,
            },
            "scenario_name": {"type": "string", "label": "Scenario Name"},
            "layout_options": {
                "type": "string",
                "label": "Layouts",
                "help": "The layouts stored on the Automate VX, for the Change Layout picker.",
            },
            "room_config_options": {
                "type": "string",
                "label": "Room Configurations",
                "help": "The room configurations stored on the Automate VX, for the Change Room Configuration picker.",
            },
            "scenario_options": {
                "type": "string",
                "label": "Scenarios",
                "help": "The scenarios on the Automate VX, for the Go To Scenario picker.",
            },
            "camera_count": {
                "type": "integer",
                "label": "Cameras",
                "help": "How many cameras the current room configuration lists.",
            },
            "recording_space_gb": {
                "type": "integer",
                "label": "Recording Space Free",
                "help": "Free recording space, rounded down to the gigabyte.",
                "unit": "GB",
            },
            "recording_space_total_gb": {
                "type": "integer",
                "label": "Recording Space Total",
                "unit": "GB",
            },
            "health": {
                "type": "string",
                "label": "Health",
                "help": "The health report's overall status (Healthy when nothing is wrong).",
                "cloud_priority": "high",
            },
            "health_message": {"type": "string", "label": "Health Message"},
            "autoswitch_health": {
                "type": "string",
                "label": "AutoSwitch Health",
                "help": "The health report's AutoSwitch status.",
            },
            "device_health": {
                "type": "string",
                "label": "Device Health",
                "help": "The health report's status for the cameras, microphones and production switchers.",
                "cloud_priority": "high",
            },
            "cameras_unreachable": {
                "type": "integer",
                "label": "Cameras Unreachable",
                "help": "Cameras in the room configurations that the Automate VX cannot ping.",
                "cloud_priority": "high",
            },
            "microphones_unreachable": {
                "type": "integer",
                "label": "Microphones Unreachable",
                "help": "Microphone devices in the room configurations that the Automate VX cannot ping.",
                "cloud_priority": "high",
            },
            "switchers_unreachable": {
                "type": "integer",
                "label": "Switchers Unreachable",
                "help": "Production switchers in the room configurations that the Automate VX cannot ping.",
            },
            "config_health": {
                "type": "string",
                "label": "Configuration Health",
                "help": "The health report's status for the room configurations.",
            },
            "camera_firmware_health": {
                "type": "string",
                "label": "Camera Firmware Health",
                "help": "The health report's status for the cameras' firmware.",
            },
            "cameras_needing_update": {
                "type": "integer",
                "label": "Cameras Needing Firmware",
                "help": "Cameras the health report says need a firmware update.",
            },
            "system_errors": {
                "type": "string",
                "label": "System Errors",
                "help": "The errors the health report lists, or what it says when there are none.",
            },
            "hardware_health": {
                "type": "string",
                "label": "Hardware Health",
                "help": "The health report's status for the processor and storage.",
            },
            "cpu_utilization": {
                "type": "number",
                "label": "Processor Use",
                "min": 0,
                "max": 100,
                "unit": "%",
                "cloud_priority": "low",
            },
            "storage_available_gb": {
                "type": "number",
                "label": "Storage Free (D:)",
                "help": "Free space on the Automate VX's D: drive, where recordings are kept.",
                "unit": "GB",
                "cloud_priority": "low",
            },
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "camera": {
                "label": "Camera",
                "label_plural": "Cameras",
                "id_format": {"type": "integer", "min": 1, "max": 255},
                "state_variables": {
                    "model": {
                        "type": "string",
                        "label": "Model",
                        "help": "The camera model the Automate VX lists.",
                    },
                    "address": {
                        "type": "string",
                        "label": "Address",
                        "help": "The camera's IP address, when the Automate VX lists it.",
                    },
                    "live": {
                        "type": "boolean",
                        "label": "Live",
                        "help": "True while this camera is on the live shot.",
                        "cloud_priority": "high",
                    },
                    "reachable": {
                        "type": "boolean",
                        "label": "Reachable",
                        "help": "Whether the Automate VX can ping this camera, from its health report.",
                    },
                    "needs_update": {
                        "type": "boolean",
                        "label": "Needs Firmware Update",
                        "help": "Whether the health report says this camera needs a firmware update.",
                    },
                },
                "summary_fields": ["model", "address", "live", "reachable"],
            },
        },
        "commands": {
            "wake": {
                "label": "Wake",
                "help": (
                    "Wakes the Automate VX from sleep: output starts, and the unit runs "
                    "the wake action set on it (cameras forward, AutoSwitch, AutoTrack "
                    "or a scenario)."
                ),
                "sets": {"output": True},
            },
            "sleep": {
                "label": "Sleep",
                "help": (
                    "Puts the Automate VX to sleep: output, AutoSwitch, recording, "
                    "streaming and camera tracking stop, and the unit runs the sleep "
                    "action set on it (cameras back or a scenario)."
                ),
                "sets": {"output": False, "autoswitch": False, "recording": False, "streaming": False},
            },
            "start_autoswitch": {
                "label": "Start AutoSwitch",
                "help": "Starts automatic switching: the live shot follows whoever is speaking.",
                "sets": {"autoswitch": True},
            },
            "stop_autoswitch": {
                "label": "Stop AutoSwitch",
                "help": "Stops automatic switching; the live shot stays where it is.",
                "sets": {"autoswitch": False},
            },
            "start_recording": {
                "label": "Start Recording",
                "help": "Starts recording the program on the Automate VX (D:\\Recordings\\VX).",
                "sets": {"recording": True},
            },
            "stop_recording": {
                "label": "Stop Recording",
                "help": "Stops the recording and saves it on the Automate VX.",
                "sets": {"recording": False},
            },
            "pause_recording": {
                "label": "Pause Recording",
                "help": (
                    "Pauses the recording. Pausing must be enabled in the unit's record "
                    "settings; Start Recording carries on."
                ),
            },
            "start_streaming": {
                "label": "Start Streaming",
                "help": "Starts the Wirecast stream to its configured destination (set up in Wirecast first).",
                "sets": {"streaming": True},
            },
            "stop_streaming": {
                "label": "Stop Streaming",
                "help": "Stops the Wirecast stream.",
                "sets": {"streaming": False},
            },
            "start_output": {
                "label": "Start Output",
                "help": "Starts sending the camera feed out through Wirecast.",
                "sets": {"output": True},
            },
            "stop_output": {
                "label": "Stop Output",
                "help": "Stops sending the camera feed out.",
                "sets": {"output": False},
            },
            "change_layout": {
                "label": "Change Layout",
                "help": "Changes the layout AutoSwitch uses (layouts apply while AutoSwitch is on).",
                "params": {
                    "layout": {
                        "type": "string",
                        "required": True,
                        "label": "Layout",
                        "help": "A layout letter, A to Z.",
                        "pattern": "^[A-Z]$",
                        "options_state": "layout_options",
                    },
                },
                "sets": {"layout": "{layout}"},
            },
            "change_room_config": {
                "label": "Change Room Configuration",
                "help": "Changes the active room configuration (its cameras, microphones and shots).",
                "params": {
                    "config": {
                        "type": "integer",
                        "required": True,
                        "label": "Room Configuration",
                        "min": 1,
                        "max": 99,
                        "options_state": "room_config_options",
                    },
                },
                "sets": {"room_config": "{config}"},
            },
            "force_change_room_config": {
                "label": "Change Room Configuration During AutoSwitch",
                "help": (
                    "Changes the active room configuration while AutoSwitch is running: "
                    "AutoSwitch stops, the configuration changes, and AutoSwitch starts "
                    "again."
                ),
                "params": {
                    "config": {
                        "type": "integer",
                        "required": True,
                        "label": "Room Configuration",
                        "min": 1,
                        "max": 99,
                        "options_state": "room_config_options",
                    },
                },
                "sets": {"room_config": "{config}"},
            },
            "go_to_scenario": {
                "label": "Go To Scenario",
                "help": "Calls a scenario: the live shot changes to the scenario's shot and it runs the scenario's actions.",
                "params": {
                    "scenario": {
                        "type": "integer",
                        "required": True,
                        "label": "Scenario",
                        "min": 0,
                        "options_state": "scenario_options",
                    },
                },
                "sets": {"scenario": "{scenario}"},
            },
            "go_home": {
                "label": "Go Home",
                "help": "Switches to the home shot and calls its camera presets.",
            },
            "switch_camera": {
                "label": "Switch Camera",
                "help": (
                    "Puts a camera on the live shot. While AutoSwitch is on, it may "
                    "switch away again when someone speaks."
                ),
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "required": True, "label": "Camera"},
                },
                "sets": {"live_camera": "{camera}"},
            },
            "recall_camera_preset": {
                "label": "Recall Camera Preset",
                "help": "Moves a camera to one of its presets.",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "required": True, "label": "Camera"},
                    "preset": {"type": "integer", "required": True, "label": "Preset", "min": 1, "max": 255},
                },
            },
            "save_camera_preset": {
                "label": "Save Camera Preset",
                "help": "Saves a camera's current position as one of its presets, replacing what that preset held.",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "required": True, "label": "Camera"},
                    "preset": {"type": "integer", "required": True, "label": "Preset", "min": 1, "max": 255},
                },
            },
            "pan_tilt": {
                "label": "Pan/Tilt",
                "help": "Starts a camera moving in a direction until Pan/Tilt Stop.",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "required": True, "label": "Camera"},
                    "direction": {
                        "type": "enum",
                        "required": True,
                        "label": "Direction",
                        "values": [
                            {"value": "0", "label": "Up"},
                            {"value": "1", "label": "Up Right"},
                            {"value": "2", "label": "Right"},
                            {"value": "3", "label": "Down Right"},
                            {"value": "4", "label": "Down"},
                            {"value": "5", "label": "Down Left"},
                            {"value": "6", "label": "Left"},
                            {"value": "7", "label": "Up Left"},
                        ],
                    },
                },
            },
            "pan_tilt_stop": {
                "label": "Pan/Tilt Stop",
                "help": "Stops a camera's pan and tilt.",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "required": True, "label": "Camera"},
                },
            },
            "zoom": {
                "label": "Zoom",
                "help": "Starts a camera zooming in or out until Zoom Stop.",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "required": True, "label": "Camera"},
                    "direction": {
                        "type": "enum",
                        "required": True,
                        "label": "Direction",
                        "values": [
                            {"value": "0", "label": "In"},
                            {"value": "1", "label": "Out"},
                        ],
                    },
                },
            },
            "zoom_stop": {
                "label": "Zoom Stop",
                "help": "Stops a camera's zoom.",
                "params": {
                    "camera": {"type": "child_id", "child_type": "camera", "required": True, "label": "Camera"},
                },
            },
            "export_camera_presets": {
                "label": "Export Camera Presets",
                "help": "Exports every configured camera's presets to the Automate VX.",
            },
            "import_camera_presets": {
                "label": "Import Camera Presets",
                "help": "Writes the presets exported on the Automate VX back to every configured camera.",
                "confirm": (
                    "Writes the exported presets to every configured camera, replacing "
                    "the presets the cameras hold now."
                ),
            },
            "copy_recordings": {
                "label": "Copy Recordings",
                "help": (
                    "Copies the recordings in D:\\Recordings on the Automate VX to a "
                    "folder: a local one (D:\\Copies) or a network share "
                    "(\\\\server\\share\\folder). The recordings stay on the unit."
                ),
                "params": {
                    "destination": {
                        "type": "string",
                        "required": True,
                        "label": "Destination Folder",
                        "help": "Where the recordings go, as the Automate VX sees it.",
                    },
                    "log_destination": {
                        "type": "string",
                        "label": "Log Folder",
                        "help": "Where to write a log of the copy. Leave blank for no log.",
                        "default": "",
                    },
                },
            },
            "move_recordings": {
                "label": "Move Recordings",
                "help": (
                    "Copies the recordings in D:\\Recordings on the Automate VX to a "
                    "folder, then deletes them from the unit."
                ),
                "confirm": (
                    "Copies every recording to the destination folder, then deletes "
                    "them from the Automate VX."
                ),
                "params": {
                    "destination": {
                        "type": "string",
                        "required": True,
                        "label": "Destination Folder",
                        "help": "Where the recordings go, as the Automate VX sees it.",
                    },
                    "log_destination": {
                        "type": "string",
                        "label": "Log Folder",
                        "help": "Where to write a log of the copy. Leave blank for no log.",
                        "default": "",
                    },
                },
            },
            "restart_wirecast": {
                "label": "Restart Wirecast",
                "help": "Closes Wirecast on the Automate VX and starts it again.",
                "confirm": "Closes Wirecast and starts it again. Output and streaming stop while it restarts.",
            },
            "restart": {
                "label": "Restart",
                "help": "Restarts the Automate VX.",
                "confirm": (
                    "Restarts the Automate VX. Camera switching, recording and streaming "
                    "stop until it is back."
                ),
                # Not measured: the Automate VX is a PC, and how long it takes to
                # restart and answer the API again waits for a unit on the bench.
                "restarts_device_for": 300,
            },
        },
        "quick_actions": [
            "wake", "sleep", "start_autoswitch", "stop_autoswitch", "start_recording", "stop_recording",
        ],
        "actions": [
            {"id": "wake", "kind": "command", "icon": "sun"},
            {"id": "sleep", "kind": "command", "icon": "moon"},
            {"id": "start_autoswitch", "kind": "command", "icon": "play"},
            {"id": "stop_autoswitch", "kind": "command", "icon": "square"},
            {"id": "start_recording", "kind": "command", "icon": "video"},
            {"id": "stop_recording", "kind": "command", "icon": "video-off"},
        ],
    }

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        super().__init__(device_id, config, state, events)
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._sign_in_lock = asyncio.Lock()
        self._token = ""
        # Raised once a sign-in on this connection was refused: nothing sends
        # the password again until the device reconnects.
        self._sign_in_refused: ConnectionFaultError | None = None
        self._all_status = True
        self._slow_due = 0.0
        # Calls this firmware answered 404; not asked again on this connection.
        self._unsupported: set[str] = set()
        # Reads the system refused, by call: named in Last Error by every poll
        # until the call answers again.
        self._refused: dict[str, str] = {}
        # Camera id -> address, from the camera list, for the health report.
        self._camera_addresses: dict[int, str] = {}
        self._live: set[int] = set()
        self._addresses_listed = False
        self._watches: dict[str, asyncio.Task] = {}

    # ── Config accessors ──

    @property
    def _host(self) -> str:
        return _text(self.config.get("host", ""))

    @property
    def _port(self) -> int:
        return _as_int(self.config.get("port")) or 4443

    @property
    def _https(self) -> bool:
        return _as_bool(self.config.get("ssl", True))

    def _auth_fault(self, detail: str = "") -> ConnectionFaultError:
        if not _text(self.config.get("password", "")):
            return ConnectionFaultError(
                "The Automate VX needs the user's password and none is entered.", code="auth_failed",
            )
        sentence = "The Automate VX refused the user name and password"
        return ConnectionFaultError(f"{sentence} ({detail})." if detail else f"{sentence}.", code="auth_failed")

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        if not self._host:
            raise ConnectionFaultError("No IP address is entered.", code="invalid_config")
        if not _text(self.config.get("username", "")) or not _text(self.config.get("password", "")):
            # Never send a sign-in that cannot succeed: every account on the
            # Automate VX has a password.
            raise self._auth_fault()
        if not await self._verify_reachable(self._host, self._port):
            raise ConnectionError(f"{self._host}:{self._port} is not responding")
        scheme = "https" if self._https else "http"
        self._client = httpx.AsyncClient(
            base_url=f"{scheme}://{self._host}:{self._port}",
            timeout=httpx.Timeout(10.0, connect=5.0),
            verify=_as_bool(self.config.get("verify_ssl", False)) if self._https else True,
            follow_redirects=False,
        )

    async def _post_connect(self) -> None:
        self._token = ""
        self._sign_in_refused = None
        self._all_status = True
        self._unsupported = set()
        self._refused = {}
        self._camera_addresses = {}
        self._live = set()
        self._slow_due = 0.0
        await self._sign_in()
        try:
            await self._read(ALL_STATUS)
        except ApiRefused as exc:
            if exc.http_status == 404 and not self._all_status:
                raise ConnectionFaultError(
                    f"The device at {self._host} answered, but not with the Automate VX API ({exc}).",
                    code="invalid_config",
                ) from exc
            raise ConnectionError(f"The Automate VX answered its status read with {exc}.") from exc
        log.info(f"[{self.device_id}] Connected to Crestron Automate VX at {self._host}:{self._port}")

    async def _initial_sync(self) -> None:
        await self.poll()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        for key in list(self._watches):
            self._cancel_watch(key)
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        # The platform holds the deadline: on a reconnect it waits up to
        # HEALTH_INTERVAL_S for a server still starting, so the request is
        # given as long. Any answer counts, a refusal included; the poll signs
        # in again when the token is refused.
        try:
            await self._request(PROBE, timeout=self.HEALTH_INTERVAL_S)
        except (ApiRefused, _TokenRefused):
            return

    # ── HTTP plumbing ──

    async def _post(
        self, path: str, body: dict[str, Any] | None, authorization: str, timeout: float | None
    ) -> httpx.Response:
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        kwargs: dict[str, Any] = {
            "headers": {"Authorization": authorization, "Content-Type": "application/json"},
            # An empty body for a call without parameters, as the document's
            # cURL commands send (--data "").
            "content": json.dumps(body) if body is not None else "",
        }
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=5.0)
        async with self._lock:
            try:
                return await client.post(path, **kwargs)
            except httpx.TransportError as exc:
                if self._https and "ssl" in str(exc).lower():
                    raise ConnectionError(
                        f"{self._host}:{self._port} did not answer HTTPS ({exc}). Firmware before "
                        f"6.4.2 serves the API over HTTP: turn Use HTTPS off for it."
                    ) from exc
                raise ConnectionError(f"{self._host} is not responding: {exc}") from exc

    @staticmethod
    def _payload(resp: httpx.Response) -> dict[str, Any] | None:
        try:
            payload = resp.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    async def _sign_in(self) -> None:
        """Ask the system for a token with the configured user name and
        password. A refusal is auth_failed, and nothing sends the password
        again on this connection."""
        if self._sign_in_refused is not None:
            raise self._sign_in_refused
        user = _text(self.config.get("username", ""))
        password = str(self.config.get("password", "") or "")
        credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
        resp = await self._post(TOKEN_PATH, None, credentials, None)
        payload = self._payload(resp)
        status = _text((payload or {}).get("status")).lower()
        if resp.status_code in (401, 403) or status == "error":
            fault = self._auth_fault(_text((payload or {}).get("err") or (payload or {}).get("message")))
            self._sign_in_refused = fault
            raise fault
        if 300 <= resp.status_code < 400:
            raise ConnectionFaultError(
                f"The device at {self._host}:{self._port} redirected the sign-in (HTTP "
                f"{resp.status_code}). Check the port and the Use HTTPS setting.",
                code="invalid_config",
            )
        if resp.status_code == 404 or (200 <= resp.status_code < 300 and payload is None):
            raise ConnectionFaultError(
                f"The device at {self._host}:{self._port} answered, but not with the Automate VX "
                f"API (HTTP {resp.status_code} for {TOKEN_PATH}). Check the port and the Use HTTPS setting.",
                code="invalid_config",
            )
        token = (payload or {}).get("token")
        if not 200 <= resp.status_code < 300 or token in (None, ""):
            raise ConnectionError(f"The Automate VX answered its sign-in with HTTP {resp.status_code}.")
        self._token = str(token)

    async def _sign_in_again(self, stale: str) -> None:
        """Replace a refused token, once for every call that saw it."""
        async with self._sign_in_lock:
            if self._token != stale:
                return  # another call already signed in again
            log.info(f"[{self.device_id}] The Automate VX refused the token; signing in again")
            await self._sign_in()

    async def _request(
        self, call: str, body: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        """One call with the current token. Returns the answer's JSON object.
        A refused token raises _TokenRefused; an error answer, or one that is
        not the API, raises ApiRefused; a dead link raises ConnectionError."""
        resp = await self._post(API + call, body, self._token, timeout)
        payload = self._payload(resp)
        status = _text((payload or {}).get("status")).lower()
        why = _text((payload or {}).get("err") or (payload or {}).get("message"))
        if resp.status_code in (401, 403):
            raise _TokenRefused(why or f"HTTP {resp.status_code}")
        if status == "error":
            if _TOKEN_REFUSED.search(why):
                raise _TokenRefused(why)
            raise ApiRefused(why or "an error with no reason", http_status=resp.status_code)
        if payload is None or not 200 <= resp.status_code < 300:
            text = resp.text.strip()
            detail = f": {text}" if text and len(text) <= 160 and not text.startswith("<") else ""
            raise ApiRefused(f"HTTP {resp.status_code}{detail}", http_status=resp.status_code)
        return payload

    async def _call(
        self, call: str, body: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        """A call, signing in again once if the system refuses the token. A
        refusal straight after a fresh sign-in is auth_failed."""
        token = self._token
        try:
            return await self._request(call, body, timeout)
        except _TokenRefused:
            await self._sign_in_again(token)
        try:
            return await self._request(call, body, timeout)
        except _TokenRefused as exc:
            raise ConnectionFaultError(
                f"The Automate VX refused a new sign-in for {call} ({exc}).", code="auth_failed",
            ) from exc

    # ── Reads ──

    async def _read(self, call: str) -> dict[str, Any] | None:
        """Read one status call into state. None when this firmware has no
        such call (it is not asked again on this connection)."""
        if call == ALL_STATUS and not self._all_status:
            # The lists are read on the slow cycle (poll) and after a change
            # that moves them.
            for part in ALL_STATUS_PARTS_FAST:
                try:
                    await self._read(part)
                except ApiRefused as exc:
                    if part == ALL_STATUS_PARTS_FAST[0]:
                        raise
                    self._refused[part] = str(exc)
            return None
        if call in self._unsupported:
            return None
        try:
            payload = await self._call(call)
        except ApiRefused as exc:
            if exc.http_status == 404 and call == ALL_STATUS:
                log.info(f"[{self.device_id}] No GetAllStatus on this firmware; reading each status")
                self._all_status = False
                return await self._read(ALL_STATUS)
            if exc.http_status == 404 and call != ALL_STATUS_PARTS_FAST[0]:
                log.info(f"[{self.device_id}] This firmware has no {call}; not asked again")
                self._unsupported.add(call)
                self._refused.pop(call, None)
                return None
            raise
        self._refused.pop(call, None)
        self._apply(call, payload)
        return payload

    def _apply(self, call: str, body: dict[str, Any]) -> None:
        if call == ALL_STATUS:
            self._apply_all_status(body)
            return
        handler = self._APPLY.get(call)
        if handler is not None:
            handler(self, body)

    def _apply_all_status(self, body: dict[str, Any]) -> None:
        """GetAllStatus nests each call's answer as a JSON string under
        api_call_0, api_call_1, ...; read each by the call it names."""
        entries = [v for v in body.values() if isinstance(v, dict) and v.get("request_api")]
        entries.sort(key=lambda e: _text(e.get("request_api")) != "GetCameras")  # the roster first
        for entry in entries:
            call = _text(entry.get("request_api"))
            raw = entry.get("response_body")
            if isinstance(raw, str):
                try:
                    inner = json.loads(raw)
                except ValueError:
                    inner = None
            else:
                inner = raw
            code = _as_int(entry.get("response_code"))
            if not isinstance(inner, dict):
                self._refused[call] = f"HTTP {code}" if code else "an answer that is not JSON"
                continue
            if _text(inner.get("status")).lower() == "error" or (code is not None and code != 200):
                self._refused[call] = _text(inner.get("err") or inner.get("message")) or f"HTTP {code}"
                continue
            self._refused.pop(call, None)
            self._apply(call, inner)

    def _apply_results(self, key: str, body: dict[str, Any]) -> None:
        if "results" in body:
            self.set_state(key, _as_bool(body["results"]))

    def _apply_AutoSwitchStatus(self, body: dict[str, Any]) -> None:
        self._apply_results("autoswitch", body)

    def _apply_RecordStatus(self, body: dict[str, Any]) -> None:
        self._apply_results("recording", body)

    def _apply_ISORecordStatus(self, body: dict[str, Any]) -> None:
        self._apply_results("iso_recording", body)

    def _apply_StreamStatus(self, body: dict[str, Any]) -> None:
        self._apply_results("streaming", body)

    def _apply_OutputStatus(self, body: dict[str, Any]) -> None:
        self._apply_results("output", body)

    def _apply_CopyStatus(self, body: dict[str, Any]) -> None:
        if "copy_underway" in body:
            self.set_state("copying", _as_bool(body["copy_underway"]))

    def _apply_RecordingSpaceAvail(self, body: dict[str, Any]) -> None:
        updates = {}
        free = _as_int(body.get("available_gigabytes"))
        total = _as_int(body.get("total_gigabytes"))
        if free is not None:
            updates["recording_space_gb"] = free
        if total is not None:
            updates["recording_space_total_gb"] = total
        if updates:
            self.set_states(updates)

    def _apply_GetCameras(self, body: dict[str, Any]) -> None:
        """Reconcile the camera children with the cameras the current room
        configuration lists. Cameras it no longer lists are removed."""
        cameras = body.get("cameras")
        if not isinstance(cameras, list):
            return
        roster: dict[int, dict[str, Any]] = {}
        for camera in cameras:
            if not isinstance(camera, dict):
                continue
            cam_id = _as_int(camera.get("id"))
            if cam_id is None or not 1 <= cam_id <= 255:
                continue
            model = camera.get("name", camera.get("model"))
            roster[cam_id] = {"model": _text(model), "address": _text(camera.get("ip"))}
        known = set(self.list_children("camera"))
        for stale in known - set(roster):
            self.deregister_child("camera", stale)
        if any(values["address"] for values in roster.values()):
            self._camera_addresses = {
                cam_id: values["address"] for cam_id, values in roster.items() if values["address"]
            }
        for cam_id, values in roster.items():
            values = dict(values, live=cam_id in self._live)
            if not values["address"]:
                values.pop("address")  # the GetAllStatus form gives none
            if cam_id in known:
                model = _text(self.get_child_state("camera", cam_id).get("model"))
                if model and values["model"] and model != values["model"]:
                    # Cameras are numbered per room configuration: another
                    # model under the same number is another camera, so
                    # nothing the old one reported carries over.
                    self.deregister_child("camera", cam_id)
                    known.discard(cam_id)
                    if "address" not in values:
                        self._camera_addresses.pop(cam_id, None)
            if cam_id in known:
                self.set_child_state_batch("camera", cam_id, values)
            else:
                self.register_child("camera", cam_id, initial_state=values)
        self.set_state("camera_count", len(roster))

    def _set_live(self, live: set[int]) -> None:
        self._live = live
        for cam_id in self.list_children("camera"):
            self.set_child_state("camera", cam_id, "live", cam_id in live)

    def _apply_CameraStatus(self, body: dict[str, Any]) -> None:
        if "address" not in body and "addresses" not in body:
            return
        address = _as_int(body.get("address"))
        addresses = body.get("addresses")
        live = {_as_int(a) for a in addresses} if isinstance(addresses, list) else set()
        if address is not None:
            live.add(address)
        live = {a for a in live if a is not None and 1 <= a <= 255}
        # The call's own topic gives one address; GetAllStatus's example adds
        # every camera on the shot. Without that list ShotStatus names the
        # second camera of a side-by-side shot.
        self._addresses_listed = isinstance(addresses, list)
        self.set_states({
            # None while the system reports no camera on the live shot.
            "live_camera": address if address is not None and 1 <= address <= 255 else None,
            "live_cameras": ",".join(str(a) for a in sorted(live)),
        })
        self._set_live(live)

    def _apply_ShotStatus(self, body: dict[str, Any]) -> None:
        updates: dict[str, Any] = {}
        for field, key in (("cam1", "shot_camera_1"), ("cam2", "shot_camera_2")):
            if field in body:
                value = _as_int(body.get(field))
                updates[key] = value if value is not None and value > 0 else 0
        if "shotname" in body:
            name = body.get("shotname")
            updates["shot_name"] = "" if name is None or _as_int(name) == -1 else _text(name)
        if "storedLayout" in body:
            updates["stored_layout"] = _text(body.get("storedLayout"))
        if updates:
            self.set_states(updates)
        first = updates.get("shot_camera_1", 0)
        if not self._addresses_listed and first and first == self.get_state("live_camera"):
            live = {first} | ({updates["shot_camera_2"]} if updates.get("shot_camera_2") else set())
            self.set_state("live_cameras", ",".join(str(a) for a in sorted(live)))
            self._set_live(live)

    def _apply_GetLayouts(self, body: dict[str, Any]) -> None:
        layouts = body.get("layouts")
        if not isinstance(layouts, list):
            return
        items = []
        for layout in layouts:
            if isinstance(layout, dict) and _text(layout.get("id")):
                lid = _text(layout.get("id")).upper()
                name = _text(layout.get("name"))
                items.append((lid, f"{lid}: {name}" if name else lid))
        self.set_state("layout_options", _options(items))

    def _apply_LayoutStatus(self, body: dict[str, Any]) -> None:
        layout = _one(body.get("layout"))
        if layout is None:
            return
        self.set_states({
            "layout": _text(layout.get("id")).upper(),
            "layout_name": _text(layout.get("name")),
        })

    def _apply_GetRoomConfigs(self, body: dict[str, Any]) -> None:
        configs = body.get("roomConfigs")
        if not isinstance(configs, list):
            return
        items = []
        for config in configs:
            cid = _as_int(config.get("id")) if isinstance(config, dict) else None
            if cid is not None:
                name = _text(config.get("name"))
                items.append((cid, f"{cid}: {name}" if name else str(cid)))
        self.set_state("room_config_options", _options(items))

    def _apply_RoomConfigStatus(self, body: dict[str, Any]) -> None:
        config = _one(body.get("roomConfig", body.get("roomConfigs")))
        cid = _as_int(config.get("id")) if config else None
        if cid is None:
            return
        previous = self.get_state("room_config")
        if previous is not None and previous != cid:
            # Cameras are numbered per room configuration: the addresses
            # learned for the last one no longer apply, and the health report
            # and scenario list are read again at the next poll.
            self._camera_addresses = {}
            self._slow_due = 0.0
        self.set_states({"room_config": cid, "room_config_name": _text(config.get("name"))})

    def _apply_GetScenarios(self, body: dict[str, Any]) -> None:
        scenarios = body.get("scenarios")
        if not isinstance(scenarios, list):
            return
        items = []
        for scenario in scenarios:
            sid = _as_int(scenario.get("id")) if isinstance(scenario, dict) else None
            if sid is not None:
                name = _text(scenario.get("name"))
                items.append((sid, f"{sid}: {name}" if name else str(sid)))
        self.set_state("scenario_options", _options(items))

    def _apply_ScenarioStatus(self, body: dict[str, Any]) -> None:
        scenario = _one(body.get("scenario"))
        sid = _as_int(scenario.get("id")) if scenario else None
        if sid is None:
            return
        self.set_states({"scenario": sid, "scenario_name": _text(scenario.get("name"))})

    def _health_camera_ids(self, devices: Any, firmware: Any, current: str) -> dict[str, int]:
        """Each camera address the health report names -> its number in the
        current room configuration. A camera is numbered per configuration,
        so the same number in another configuration is another camera: an
        address the report places only in other configurations maps to
        nothing."""
        ids: dict[str, int] = {}
        elsewhere: set[str] = set()
        entries = devices.get("cameras") if isinstance(devices, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            address = _text(entry.get("address"))
            configs = [c for c in entry.get("configs") or [] if isinstance(c, dict)]
            here = [_as_int(c.get("deviceID")) for c in configs if current and _text(c.get("configID")) == current]
            here = [cam_id for cam_id in here if cam_id is not None]
            if here:
                ids[address] = here[0]
            elif configs:
                elsewhere.add(address)
        for cam_id, address in self._camera_addresses.items():
            if address not in elsewhere:
                ids.setdefault(address, cam_id)
        # Last, the firmware list's own ids, for an address nothing else
        # places (the document does not say which configuration they count in).
        cams = firmware.get("cameras") if isinstance(firmware, dict) else None
        if isinstance(cams, dict):
            for address, entry in cams.items():
                cam_id = _as_int(entry.get("id")) if isinstance(entry, dict) else None
                if cam_id is not None and _text(address) not in elsewhere:
                    ids.setdefault(_text(address), cam_id)
        return ids

    def _apply_HealthStatus(self, body: dict[str, Any]) -> None:
        updates: dict[str, Any] = {
            "health": _text(body.get("status")),
            "health_message": _text(body.get("message")),
        }
        autoswitch = body.get("autoSwitchHealth")
        if isinstance(autoswitch, dict):
            updates["autoswitch_health"] = _text(autoswitch.get("status"))
        config = body.get("configHealth")
        current = ""
        if isinstance(config, dict):
            updates["config_health"] = _text(config.get("status"))
            current = _text(config.get("currentConfig"))
        devices = body.get("deviceHealth")
        firmware = body.get("cameraFirmware")
        ids = self._health_camera_ids(devices, firmware, current)
        needs_update: dict[int, bool] = {}
        if isinstance(firmware, dict):
            updates["camera_firmware_health"] = _text(firmware.get("status"))
            cams = firmware.get("cameras")
            if isinstance(cams, dict):
                flags = {_text(a): _as_bool(e.get("requiresUpdate")) for a, e in cams.items() if isinstance(e, dict)}
                updates["cameras_needing_update"] = sum(1 for flag in flags.values() if flag)
                for address, flag in flags.items():
                    if address in ids:
                        needs_update[ids[address]] = flag
        reachable: dict[int, tuple[bool, str]] = {}
        if isinstance(devices, dict):
            updates["device_health"] = _text(devices.get("status"))
            for kind, key in (("cameras", "cameras_unreachable"), ("mics", "microphones_unreachable"),
                              ("vps", "switchers_unreachable")):
                entries = devices.get(kind)
                if isinstance(entries, list):
                    updates[key] = sum(1 for e in entries if isinstance(e, dict) and not _as_bool(e.get("pingable")))
            cameras = devices.get("cameras")
            for entry in cameras if isinstance(cameras, list) else []:
                address = _text(entry.get("address")) if isinstance(entry, dict) else ""
                if address in ids:
                    reachable[ids[address]] = (_as_bool(entry.get("pingable")), address)
        errors = body.get("errors")
        if isinstance(errors, dict):
            updates["system_errors"] = _text(errors.get("errors"))
        hardware = body.get("hardwareHealth")
        if isinstance(hardware, dict):
            updates["hardware_health"] = _text(hardware.get("status"))
            data = hardware.get("hardwareData")
            if isinstance(data, dict):
                cpu_entry, drive = data.get("cpu"), data.get("dDrive")
                cpu = _as_float(cpu_entry.get("utilization")) if isinstance(cpu_entry, dict) else None
                storage = _as_float(drive.get("available_storage")) if isinstance(drive, dict) else None
                if cpu is not None:
                    updates["cpu_utilization"] = cpu
                if storage is not None:
                    updates["storage_available_gb"] = storage
        self.set_states(updates)
        addresses = {cam_id: address for address, cam_id in ids.items()}
        for cam_id in self.list_children("camera"):
            values: dict[str, Any] = {}
            if cam_id in needs_update:
                values["needs_update"] = needs_update[cam_id]
            if cam_id not in self._camera_addresses and addresses.get(cam_id):
                # GetAllStatus lists a camera's model and no address; the
                # health report names it.
                values["address"] = addresses[cam_id]
            if cam_id in reachable:
                ok, address = reachable[cam_id]
                values["reachable"] = ok
                values.update(self.child_fault() if ok else self.child_fault(
                    "not_responding", f"The Automate VX cannot reach this camera at {address}.",
                ))
            if values:
                self.set_child_state_batch("camera", cam_id, values)

    async def poll(self) -> None:
        """GetAllStatus, the shot and the scenario every poll; the scenario
        list and the health report every 30 s. A dead link, or a refused
        status read, raises (the platform counts it); any other read the
        system refuses is named in last_error, written again by every poll
        while it holds."""
        if self._client is None:
            return
        now = time.monotonic()
        slow = now >= self._slow_due
        try:
            await self._read(ALL_STATUS)
        except ApiRefused as exc:
            raise ConnectionError(f"The Automate VX answered its status read with {exc}.") from exc
        calls = FAST + SLOW if slow else FAST
        if slow and not self._all_status:
            calls = ALL_STATUS_PARTS_SLOW + calls
        for call in calls:
            try:
                await self._read(call)
            except ApiRefused as exc:
                self._refused[call] = str(exc)
        if slow:
            self._slow_due = now + SLOW_INTERVAL_S
        if self._refused:
            what = "; ".join(f"{API_LABELS.get(call, call)} ({why})" for call, why in self._refused.items())
            self.set_state("last_error", f"The Automate VX did not answer {what}. Those values are not updated.")

    async def refresh_children(self) -> dict[str, Any]:
        await self._read("GetCameras")
        return {"cameras": len(self.list_children("camera"))}

    _APPLY = {
        "AutoSwitchStatus": _apply_AutoSwitchStatus,
        "RecordStatus": _apply_RecordStatus,
        "ISORecordStatus": _apply_ISORecordStatus,
        "StreamStatus": _apply_StreamStatus,
        "OutputStatus": _apply_OutputStatus,
        "CopyStatus": _apply_CopyStatus,
        "RecordingSpaceAvail": _apply_RecordingSpaceAvail,
        "GetCameras": _apply_GetCameras,
        "CameraStatus": _apply_CameraStatus,
        "ShotStatus": _apply_ShotStatus,
        "GetLayouts": _apply_GetLayouts,
        "LayoutStatus": _apply_LayoutStatus,
        "GetRoomConfigs": _apply_GetRoomConfigs,
        "RoomConfigStatus": _apply_RoomConfigStatus,
        "GetScenarios": _apply_GetScenarios,
        "ScenarioStatus": _apply_ScenarioStatus,
        "HealthStatus": _apply_HealthStatus,
    }

    # ── Writes ──

    def _cancel_watch(self, key: str) -> None:
        task = self._watches.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    def _watch(self, call: str, key: str, wanted: Any, what: str) -> None:
        """Read ``call`` every second until ``key`` reads ``wanted``. A
        change the system accepted and never shows is named in Last Error."""
        self._cancel_watch(key)

        async def _run() -> None:
            deadline = time.monotonic() + WATCH_S
            while time.monotonic() < deadline:
                await asyncio.sleep(WATCH_STEP_S)
                try:
                    await self._read(call)
                except Exception as exc:  # the poll reports a dead link
                    log.debug(f"[{self.device_id}] Read-back of {call} stopped: {exc}")
                    return
                if self.get_state(key) == wanted:
                    return
            label = self.DRIVER_INFO["state_variables"][key]["label"]
            shown = "on" if self.get_state(key) is True else "off" if self.get_state(key) is False else self.get_state(key)
            self.set_state(
                "last_error",
                f"The Automate VX accepted {what}, but {int(WATCH_S)} s later it still reports {label} {shown}.",
            )

        self._watches[key] = asyncio.create_task(_run())

    async def _write(
        self,
        call: str,
        body: dict[str, Any] | None,
        what: str,
        read_back: tuple[str, ...] = (),
        expect: tuple[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a call, then read back what it changed. With ``expect`` (a
        state key and the value it should read) a change the system has not
        applied yet is read again until it shows."""
        try:
            answer = await self._call(call, body)
        except ApiRefused as exc:
            raise CommandRefusedError(f"The Automate VX refused {what}: {exc}.") from exc
        for status in read_back:
            try:
                await self._read(status)
            except ApiRefused as exc:
                log.debug(f"[{self.device_id}] Read-back of {status} not answered: {exc}")
        if expect is not None and read_back:
            key, wanted = expect
            if self.get_state(key) != wanted:
                self._watch(read_back[0], key, wanted, what)
            else:
                self._cancel_watch(key)
        return answer

    @staticmethod
    def _camera(params: dict[str, Any]) -> str:
        cam = _as_int(params.get("camera"))
        if cam is None or not 1 <= cam <= 255:
            raise ValueError(f"Camera must be 1 to 255, got {params.get('camera')!r}")
        return str(cam)

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        if command in SIMPLE:
            call, what, read_back, expect = SIMPLE[command]
            await self._write(call, None, what, read_back, expect)
            return None

        if command == "change_layout":
            layout = _text(params.get("layout")).upper()
            if not re.fullmatch(r"[A-Z]", layout):
                raise ValueError(f"Layout must be a letter A to Z, got {params.get('layout')!r}")
            await self._write("ChangeLayout", {"id": layout}, f"Change Layout {layout}",
                              ("LayoutStatus", "ShotStatus"), ("layout", layout))
            return None

        if command in ("change_room_config", "force_change_room_config"):
            config = _as_int(params.get("config"))
            if config is None or not 1 <= config <= 99:
                raise ValueError(f"Room configuration must be 1 to 99, got {params.get('config')!r}")
            call = "ChangeRoomConfiguration" if command == "change_room_config" else "ForceChangeRoomConfig"
            # The camera list follows the room configuration.
            await self._write(call, {"id": str(config)}, f"Change Room Configuration {config}",
                              (ALL_STATUS, "GetCameras"), ("room_config", config))
            return None

        if command == "go_to_scenario":
            scenario = _as_int(params.get("scenario"))
            if scenario is None or scenario < 0:
                raise ValueError(f"Scenario must be a scenario number, got {params.get('scenario')!r}")
            await self._write("GoToScenario", {"id": str(scenario)}, f"Go To Scenario {scenario}",
                              ("ScenarioStatus", ALL_STATUS, "ShotStatus"), ("scenario", scenario))
            return None

        if command == "switch_camera":
            cam = self._camera(params)
            # AutoSwitch may switch away again at once, so only a switch made
            # with AutoSwitch off is expected to stay.
            expect = None if self.get_state("autoswitch") else ("live_camera", int(cam))
            await self._write("ManualSwitchCamera", {"address": cam}, f"Switch Camera {cam}",
                              (ALL_STATUS, "ShotStatus"), expect)
            return None

        if command in ("recall_camera_preset", "save_camera_preset"):
            cam = self._camera(params)
            preset = _as_int(params.get("preset"))
            if preset is None or not 1 <= preset <= 255:
                raise ValueError(f"Preset must be 1 to 255, got {params.get('preset')!r}")
            call = "CallCameraPreset" if command == "recall_camera_preset" else "SaveCameraPreset"
            what = "Recall" if command == "recall_camera_preset" else "Save"
            await self._write(call, {"cam": cam, "pre": str(preset)}, f"{what} Camera {cam} Preset {preset}")
            return None

        if command == "pan_tilt":
            cam = self._camera(params)
            direction = _text(params.get("direction"))
            if direction not in {d["value"] for d in PT_DIRECTIONS}:
                raise ValueError(f"Direction must be 0 to 7, got {params.get('direction')!r}")
            await self._write("StartPT", {"cam": cam, "ptDir": direction}, f"Pan/Tilt camera {cam}")
            return None

        if command == "zoom":
            cam = self._camera(params)
            direction = _text(params.get("direction"))
            if direction not in {d["value"] for d in ZOOM_DIRECTIONS}:
                raise ValueError(f"Direction must be 0 (in) or 1 (out), got {params.get('direction')!r}")
            await self._write("StartZ", {"cam": cam, "zDir": direction}, f"Zoom camera {cam}")
            return None

        if command in ("pan_tilt_stop", "zoom_stop"):
            cam = self._camera(params)
            call = "StopPT" if command == "pan_tilt_stop" else "StopZ"
            what = "Pan/Tilt Stop" if command == "pan_tilt_stop" else "Zoom Stop"
            await self._write(call, {"cam": cam}, f"{what} on camera {cam}")
            return None

        if command in ("copy_recordings", "move_recordings"):
            destination = _text(params.get("destination"))
            if not destination:
                raise ValueError("A destination folder is required")
            body = {
                "destination": destination,
                "logDestination": _text(params.get("log_destination")),
                "deleteSource": command == "move_recordings",
            }
            what = "Copy Recordings" if command == "copy_recordings" else "Move Recordings"
            await self._write("CopyFiles", body, what, (ALL_STATUS,))
            return None

        raise ValueError(f"Unknown command: {command}")
