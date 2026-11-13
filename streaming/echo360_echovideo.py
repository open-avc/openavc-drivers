"""EchoVideo managed classroom capture via the local Capture API 3.0.2.

The current and next XML blocks have different structures. Python reconciles the
recorder's changing input roster and uses its schedule and clock to choose the
right Start operation before posting anything.
"""

import asyncio
import base64
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
import ssl
import time
from urllib.parse import quote

import aiohttp
from defusedxml import ElementTree as XML
from defusedxml.common import DefusedXmlException

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.drivers.command_files import DeviceFile

SOURCE_API = "/status/"
ACTIVE = {"Active", "Recording", "Paused"}
LIVE_STATES = {
    "active": "Active",
    "recording": "Recording",
    "paused": "Paused",
    "waiting": "Preparing",
    "pending": "Preparing",
    "pre-rolling": "Preparing",
    "prerolling": "Preparing",
    "idle": "Idle",
    "stopping": "Finishing",
}
INPUT_CLEAR = {
    "signal_present": None,
    "format": "",
    "left_average": None,
    "left_peak": None,
    "right_average": None,
    "right_peak": None,
    "snapshot_available": False,
}
UNAVAILABLE = {
    "capture_state": "Unknown",
    "reported_state": "",
    "recording": None,
    "paused": None,
    "session_active": None,
    "can_start": False,
    "can_pause": False,
    "can_resume": False,
    "can_finish": False,
    "can_extend": False,
    "current_title": "",
    "current_section": "",
    "current_presenters": "",
    "current_start": "",
    "current_profile": "",
    "output_type": "",
    "duration_seconds": None,
    "remaining_seconds": None,
    "next_title": "",
    "next_section": "",
    "next_start": "",
    "next_duration": None,
    "snapshot_options": "[]",
}
HEALTH_CLEAR = {"transfer_status": "", "archive_usage": None, "uploads_pending": None, "last_sync": ""}


class EchoError(ValueError):
    """The recorder refused a command or a commissioning choice is unavailable."""


def value(node, path):
    return (node.findtext(path) or "").strip() if node is not None else ""


def number(raw):
    try:
        result = float(raw)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def seconds(raw):
    try:
        result = int(raw)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def timestamp(raw):
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return moment if moment.utcoffset() is not None else None


def boolean(raw):
    return {"true": True, "false": False}.get(raw.lower())


def checked_minutes(selection):
    try:
        amount = int(str(selection))
    except (ValueError, TypeError) as exc:
        raise EchoError("Enter a whole number of minutes from 1 to 480.") from exc
    if not 1 <= amount <= 480:
        raise EchoError("Enter a lecture length from 1 to 480 minutes.")
    return amount


def certificate(config, simulated):
    if simulated:
        return False  # Only the platform's own redirected loopback simulator.
    fingerprint = (config.get("certificate_sha256") or "").strip().replace(":", "")
    if not fingerprint:
        return ssl.create_default_context()
    if not re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
        raise ConnectionFaultError(
            "The certificate fingerprint must contain 64 hexadecimal digits.", code="invalid_config"
        )
    return aiohttp.Fingerprint(bytes.fromhex(fingerprint))


def address(config):
    host = str(config.get("host") or "").strip().strip("[]")
    if not re.fullmatch(r"[a-zA-Z0-9.:%_-]+", host):
        raise ConnectionFaultError("Enter the recorder's hostname or IP address.", code="invalid_config")
    return f"https://{'[' + host + ']' if ':' in host else host}:{int(config.get('port') or 8443)}"


def api_error(status):
    if status == 401:
        return ConnectionFaultError("EchoVideo declined the capture account or password.", code="auth_failed")
    if status == 403:
        return EchoError("EchoVideo denied access for this account.")
    if status in range(500, 600) or status == 429:
        return ConnectionError(f"EchoVideo is temporarily unavailable (HTTP {status}).")
    return EchoError(f"The recorder refused the request (HTTP {status}).")


class CaptureAPI:
    def __init__(self, client, base, trace):
        self.client = client
        self.base = base
        self.trace = trace

    async def request(self, path, fields=None):
        method = "GET" if fields is None else "POST"
        self.trace("tx", f"{method} {path}")
        try:
            async with self.client.request(method, self.base + path, data=fields, allow_redirects=False) as r:
                self.trace("rx", f"{path} HTTP {r.status}")
                if r.status != 200:
                    raise api_error(r.status)
                return await self.body(r)
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ServerFingerprintMismatch) as exc:
            raise ConnectionFaultError(
                "The recorder certificate does not match a trusted issuer or the configured fingerprint.",
                code="invalid_config",
            ) from exc
        except aiohttp.ClientError as exc:
            raise ConnectionError("The HTTPS connection to the EchoVideo recorder failed.") from exc

    @staticmethod
    async def body(response):
        result = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            result.extend(chunk)
            if len(result) > 8 * 1024 * 1024:
                raise ConnectionError("The recorder response exceeded the supported size.")
        return bytes(result)

    async def xml(self, path, fields=None):
        raw = await self.request(path, fields)
        try:
            root = XML.fromstring(raw)
        except (XML.ParseError, DefusedXmlException) as exc:
            raise ConnectionError(
                "The address returned an unreadable EchoVideo Capture API response."
            ) from exc
        if root.tag == "error":
            raise EchoError(
                "EchoVideo refused the request. Check the capture status, account and room schedule."
            )
        expected = "status" if fields is None else "ok"
        if root.tag != expected:
            raise ConnectionError("The address did not return the expected EchoVideo Capture API response.")
        return root


def profile_names(root):
    return list(
        dict.fromkeys(
            p.text.strip()
            for p in root.findall("capture-profiles/capture-profile")
            if p.text and p.text.strip()
        )
    )


def identity(root):
    versions = [item.text.strip() for item in root.findall("api-versions/api-version") if item.text]
    if not any(v.split(".")[0] == "3" for v in versions):
        raise ConnectionFaultError(
            "This endpoint did not report EchoVideo Capture API version 3.", code="invalid_config"
        )
    return {
        "api_version": ", ".join(versions),
        "room": value(root, "location"),
        "recorder_host": value(root, "host-address"),
        "serial_number": value(root, "serial-number"),
        "system_version": value(root, "system-version"),
        "last_sync": value(root, "last-sync"),
        "transfer_status": value(root, "content/state"),
        "archive_usage": number(value(root, "content/archive-space-usage")),
        "uploads_pending": seconds(value(root, "content/uploads-pending")),
    }


def details(node):
    start = value(node, "start-time") or value(node, "schedule/start-time")
    length = seconds(value(node, "duration") or value(node, "schedule/duration"))
    params = node.find("schedule/parameters") if node is not None else None
    if params is None and node is not None:
        params = node.find("parameters")
    presenters = (
        [p.text.strip() for p in params.findall("presenters/presenter") if p.text]
        if params is not None
        else []
    )
    return {
        "start": start,
        "duration": length,
        "title": value(params, "title"),
        "section": value(params, "section"),
        "presenters": ", ".join(presenters),
        "profile": value(params, "capture-profile/name"),
        "output": value(node, "output-type") or value(params, "capture-profile/output-type"),
    }


def capture_phase(current):
    raw = value(current, "state")
    if raw:
        return LIVE_STATES.get(raw.lower(), "Unknown"), raw
    if current is not None and any(
        (value(current, "start-time"), value(current, "schedule/start-time"), value(current, "duration"))
    ):
        return "Unknown", ""
    return "Idle", ""


def same_start(first, second):
    left = timestamp(first)
    return left is not None and left == timestamp(second)


def capture_flags(phase, raw):
    return {
        "capture_state": phase,
        "reported_state": raw,
        "session_active": None if phase == "Unknown" else phase in ACTIVE,
        "recording": {"Recording": True, "Paused": False, "Idle": False, "Preparing": False}.get(phase),
        "paused": {"Recording": False, "Paused": True, "Idle": False, "Preparing": False}.get(phase),
    }


def capture_values(current, upcoming, clock):
    phase, raw = capture_phase(current)
    info, following = details(current), details(upcoming)
    start = timestamp(info["start"])
    length = info["duration"]
    remaining = None
    if None not in (start, clock, length):
        remaining = max(0, int((start - clock).total_seconds() + length))
    return {
        **capture_flags(phase, raw),
        "current_title": info["title"],
        "current_section": info["section"],
        "current_start": info["start"],
        "current_profile": info["profile"],
        "current_presenters": info["presenters"],
        "duration_seconds": length,
        "remaining_seconds": remaining,
        "output_type": info["output"],
        "next_title": following["title"],
        "next_section": following["section"],
        "next_start": following["start"],
        "next_duration": following["duration"],
    }


def input_id(name, kind):
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:42] or "source"
    digest = hashlib.sha256(f"{kind}:{name}".encode()).hexdigest()[:12]
    return f"{slug}_{digest}"


def image_name(name):
    # Restrict the recorder-provided thumbnail to one path component.
    valid = re.fullmatch(r"[^/\\?#\x00-\x1f]{1,249}\.jpe?g", name, re.IGNORECASE)
    return name if valid else ""


def audio_levels(row):
    levels = {}
    for channel in row.findall("channels/channel"):
        side = value(channel, "position").lower()
        if side in ("left", "right"):
            levels[side + "_average"] = number(value(channel, "average"))
            levels[side + "_peak"] = number(value(channel, "peak"))
    return levels


def source_rows(current):
    result = {}
    if current is None:
        return result
    for row in current.findall("sources/source"):
        name, kind = value(row, "name"), value(row, "class")
        if not name:
            continue
        props = dict(
            INPUT_CLEAR,
            name=name,
            source_class=kind,
            format=value(row, "format"),
            signal_present=boolean(value(row, "signal-present")),
        )
        props.update(audio_levels(row))
        picture = image_name(value(row, "thumbnail"))
        props["snapshot_available"] = bool(picture)
        result[input_id(name, kind)] = (props, picture)
    return result


class EchoVideo(BaseDriver):
    DRIVER_INFO = {
        "id": "echo360_echovideo",
        "name": "Echo360 EchoVideo Capture",
        "manufacturer": "Echo360",
        "category": "streaming",
        "version": "1.0.0",
        "author": "OpenAVC",
        "min_platform_version": "0.37.2",
        "description": "Control the classroom recorder directly. Start the scheduled class or an unscheduled "
        "lecture, pause, resume, finish and add time. Read the schedule, input signals and uploads.",
        "source_url": "https://support.echo360.com/hc/en-us/articles/360042045412-EchoVideo-Capture-API",
        "transport": "http",
        "protocols": ["https", "rest"],
        "ports": [8443, 443],
        "tags": ["education", "lecture-capture", "recording", "software"],
        "verified": False,
        "simulated": True,
        "compatible_models": [
            {
                "manufacturer": "Echo360",
                "models": [
                    "Universal Capture: Classroom (Windows)",
                    "Universal Capture: Classroom (Mac)",
                    "EchoVideo Pro",
                    "EchoVideo Pod",
                ],
                "confidence": "untested",
                "notes": "Managed recorders using the public Capture API 3.0.2. Documented and "
                "simulated. Field verification pending.",
            }
        ],
        "help": {
            "overview": "One OpenAVC device controls one registered classroom PC, Mac, Pro or Pod through its "
            "local Capture API. Bind the lecture commands and feedback in your room project.",
            "setup": "Enter the recorder’s network address, HTTPS port and capture login. Check the reported "
            "room. Choose Set lecture defaults and select the capture profile by name. A sole profile "
            "is selected automatically. Campus IT can supply the device address and login. Generic "
            "logins may require manual course publishing for unscheduled lectures.",
            "connection": "The controller must reach the recorder directly. Use the HTTPS port configured on "
            "the recorder (often 8443 or 443). A Mac running Classroom requires a logged-in user "
            "with permission to capture. Capture in progress includes an active session with "
            "unreported pause state. Recording and Pause explicitly reported stay unknown when "
            "the recorder reports only Active.",
        },
        "default_config": {"host": "", "port": 8443, "ssl": True, "lecture_minutes": 60, "poll_interval": 5},
        "config_schema": {
            "host": {"type": "string", "label": "Recorder hostname or IP", "required": True},
            "port": {
                "type": "integer",
                "label": "Recorder HTTPS port",
                "default": 8443,
                "min": 1,
                "max": 65535,
            },
            "username": {"type": "string", "label": "Capture account"},
            "password": {"type": "password", "label": "Capture password", "secret": True},
            "certificate_sha256": {
                "type": "string",
                "label": "Recorder certificate fingerprint (SHA-256)",
                "advanced": True,
                "description": "For a private or self-signed certificate, enter the "
                "SHA-256 fingerprint verified by campus IT. Leave "
                "blank to use trusted certificate authorities.",
            },
            "default_profile": {
                "type": "string",
                "label": "Saved profile",
                "advanced": True,
                "description": "Filled by Set lecture defaults.",
            },
            "lecture_minutes": {
                "type": "integer",
                "label": "Default lecture length (minutes)",
                "min": 1,
                "max": 480,
                "default": 60,
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status interval (seconds)",
                "min": 1,
                "default": 5,
                "advanced": True,
            },
        },
        "state_variables": {
            "room": {"type": "string", "label": "Room reported by Echo360"},
            "recorder_host": {"type": "string", "label": "Recorder hostname"},
            "serial_number": {"type": "string", "label": "Recorder identifier"},
            "system_version": {"type": "string", "label": "Recorder software version"},
            "api_version": {"type": "string", "label": "Capture API version"},
            "profile": {"type": "string", "label": "Default capture profile"},
            "capture_state": {"type": "string", "label": "Capture status"},
            "reported_state": {"type": "string", "label": "Status reported by Echo360"},
            "control_status": {"type": "string", "label": "Control status"},
            "current_title": {"type": "string", "label": "Current lecture", "cloud_priority": "low"},
            "current_section": {"type": "string", "label": "Current class section", "cloud_priority": "low"},
            "current_presenters": {"type": "string", "label": "Current presenters", "cloud_priority": "low"},
            "current_profile": {"type": "string", "label": "Current capture profile"},
            "current_start": {"type": "string", "label": "Current start (UTC)"},
            "output_type": {"type": "string", "label": "Capture output"},
            "next_title": {"type": "string", "label": "Next lecture", "cloud_priority": "low"},
            "next_section": {"type": "string", "label": "Next class section", "cloud_priority": "low"},
            "next_start": {"type": "string", "label": "Next start (UTC)"},
            "last_sync": {"type": "string", "label": "Last EchoVideo sync"},
            "transfer_status": {"type": "string", "label": "Upload status"},
            "session_active": {"type": "boolean", "label": "Capture in progress"},
            "recording": {"type": "boolean", "label": "Recording explicitly reported"},
            "paused": {"type": "boolean", "label": "Pause explicitly reported"},
            "can_start": {"type": "boolean", "label": "Start available"},
            "can_pause": {"type": "boolean", "label": "Pause available"},
            "can_resume": {"type": "boolean", "label": "Resume available"},
            "can_finish": {"type": "boolean", "label": "Finish available"},
            "can_extend": {"type": "boolean", "label": "Add time available"},
            "duration_seconds": {"type": "integer", "label": "Current duration (seconds)"},
            "remaining_seconds": {
                "type": "integer",
                "label": "Time until scheduled end (seconds)",
                "cloud_priority": "low",
            },
            "next_duration": {"type": "integer", "label": "Next duration (seconds)"},
            "uploads_pending": {"type": "integer", "label": "Uploads pending"},
            "archive_usage": {"type": "number", "label": "Recording storage used (%)"},
            "profile_options": {
                "type": "string",
                "label": "Available profiles",
                "cloud_priority": "low",
            },
            "snapshot_options": {"type": "string", "label": "Inputs with snapshots", "cloud_priority": "low"},
        },
        "child_entity_types": {
            "input": {
                "label": "Capture input",
                "label_plural": "Capture inputs",
                "label_field": "name",
                "id_format": {"type": "string", "max_length": 64},
                "state_variables": {
                    "name": {"type": "string", "label": "Input name"},
                    "source_class": {"type": "string", "label": "Source type"},
                    "format": {"type": "string", "label": "Signal format"},
                    "signal_present": {"type": "boolean", "label": "Signal present"},
                    "snapshot_available": {"type": "boolean", "label": "Snapshot available"},
                    "left_average": {
                        "type": "number",
                        "label": "Left average level",
                        "cloud_priority": "low",
                    },
                    "left_peak": {"type": "number", "label": "Left peak level", "cloud_priority": "low"},
                    "right_average": {
                        "type": "number",
                        "label": "Right average level",
                        "cloud_priority": "low",
                    },
                    "right_peak": {"type": "number", "label": "Right peak level", "cloud_priority": "low"},
                },
                "summary_fields": ["source_class", "signal_present", "format"],
            }
        },
        "commands": {
            "get_status": {
                "label": "Read recorder status",
                "help": "Read this recorder’s current lecture, next booking, available controls "
                "and capture inputs.",
                "query_for": "capture_state",
            },
            "start_lecture": {
                "label": "Start lecture",
                "help": "Start the booked class in its early-start window. Otherwise start an "
                "unscheduled lecture using the room’s saved profile and length.",
                "sets": {"session_active": True},
            },
            "start_named": {
                "label": "Start an unscheduled lecture",
                "help": "Start a separate lecture with an optional title, length and profile "
                "when the room is free. The recorder must have time before its next "
                "booking.",
                "params": {
                    "title": {"type": "string", "label": "Lecture title"},
                    "minutes": {"type": "integer", "label": "Lecture length (minutes)", "min": 1, "max": 480},
                    "profile": {
                        "type": "string",
                        "label": "Capture profile",
                        "options_state": "profile_options",
                    },
                },
                "sets": {"session_active": True},
            },
            "start_early": {
                "label": "Start scheduled class early",
                "help": "Start the upcoming class during its preparation window and preserve "
                "the scheduled class details.",
                "sets": {"session_active": True},
            },
            "pause": {
                "label": "Pause lecture",
                "help": "Pause the current recording. Check Capture status for the recorder’s response.",
                "sets": {"paused": True},
            },
            "resume": {
                "label": "Resume lecture",
                "help": "Resume the current paused lecture. Check Capture status for the recorder’s "
                "response.",
                "sets": {"paused": False},
            },
            "finish": {
                "label": "Finish lecture",
                "help": "End the current lecture so EchoVideo can process and upload it.",
                "sets": {"session_active": False},
            },
            "add_five_minutes": {
                "label": "Add 5 minutes",
                "help": "Add up to five minutes to this lecture. The recorder protects the next class and reports the duration it accepted.",
            },
            "extend": {
                "label": "Add time",
                "help": "Extend the current lecture, by five minutes by default. Check the revised "
                "end time if the next booking limits the extension.",
                "params": {
                    "minutes": {
                        "type": "integer",
                        "label": "Minutes to add",
                        "default": 5,
                        "min": 1,
                        "max": 480,
                    }
                },
            },
            "get_system": {
                "label": "Refresh recorder details",
                "help": "Read the recorder identity, named profiles, storage and upload queue.",
                "query_for": "room",
            },
            "download_snapshot": {
                "label": "Download input snapshot",
                "help": "Download the current JPEG for a monitored video or display input.",
                "params": {
                    "input": {
                        "type": "string",
                        "label": "Capture input",
                        "required": True,
                        "options_state": "snapshot_options",
                    }
                },
                "returns_file": True,
            },
        },
        "actions": [
            {
                "id": "set_defaults",
                "kind": "setup",
                "availability": "online",
                "label": "Set lecture defaults",
                "confirm": "Choose the profile and length to use when starting an unscheduled lecture in this room.",
                "params": {
                    "profile": {
                        "type": "string",
                        "label": "Capture profile",
                        "required": True,
                        "options_state": "profile_options",
                    },
                    "minutes": {"type": "integer", "label": "Lecture length (minutes)", "min": 1, "max": 480},
                },
            },
            {"id": "get_status"},
            {"id": "start_lecture"},
            {"id": "pause"},
            {"id": "resume"},
            {"id": "finish"},
            {"id": "add_five_minutes"},
        ],
        "quick_actions": ["get_status", "start_lecture", "pause", "resume", "finish", "add_five_minutes"],
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._api = None
        self._lock = asyncio.Lock()
        self._profiles = []
        self._default = ""
        self._minutes = 60
        self._sync_due = 0.0
        self._clock = None
        self._inputs = set()
        self._pictures = {}
        self._pending = ""
        self._before = ()
        self._until = 0.0
        self._early = False
        self._sim = False

    async def _create_transport(self, transport_type):
        base = address(self.config)
        self._sim = self.config.get("host") == "127.0.0.1" and self.udp_redirect == (
            "127.0.0.1",
            int(self.config["port"]),
        )
        username, password = self.config.get("username"), self.config.get("password")
        if self._sim:
            username, password = "instructor", "simulation"
        if not username or not password:
            raise ConnectionFaultError(
                "The EchoVideo capture account or password is missing.", code="auth_failed"
            )
        self._default = str(self.config.get("default_profile") or "")
        self._minutes = checked_minutes(self.config.get("lecture_minutes", 60))
        local = None if self._sim else self.local_address()
        connector = aiohttp.TCPConnector(
            ssl=certificate(self.config, self._sim), local_addr=(local, 0) if local else None
        )
        client = aiohttp.ClientSession(
            connector=connector,
            headers={
                "Authorization": "Basic "
                + base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            },
            timeout=aiohttp.ClientTimeout(total=8),
            trust_env=False,
        )
        self._api = CaptureAPI(client, base, self.record_traffic)

    def _link_alive(self):
        return self._api is not None and not self._api.client.closed

    async def _post_connect(self):
        await self._system()

    async def _initial_sync(self):
        await self.poll()

    async def _close_session(self):
        api, self._api = self._api, None
        if api:
            await api.client.close()
        self._profiles = []
        self._unavailable("Recorder offline")
        self.set_states({**HEALTH_CLEAR, "profile_options": "[]", "profile": ""})

    def _unavailable(self, message):
        self._early = False
        self._clock = None
        self._update_inputs({})
        self._sync_due = 0
        self.set_states({**UNAVAILABLE, **HEALTH_CLEAR, "control_status": message})

    async def _system(self):
        root = await self._api.xml(SOURCE_API + "system")
        values = identity(root)
        previous = self.get_state("serial_number")
        if previous and previous != values["serial_number"]:
            self._forget_inputs()
            self._pending = ""
        self._profiles = profile_names(root)
        self.set_states({**values, "profile_options": json.dumps(self._profiles), "profile": self._profile()})
        self._sync_due = time.monotonic() + 30

    def _profile(self):
        if self._default:
            return self._default if self._default in self._profiles else ""
        return self._profiles[0] if len(self._profiles) == 1 else ""

    async def run_setup_action(self, action_id, params, progress):
        if action_id != "set_defaults":
            return await super().run_setup_action(action_id, params, progress)
        async with self._lock:
            await progress("Reading capture profiles from this recorder", 25)
            await self._system()
            profile = params.get("profile")
            if profile not in self._profiles:
                return {
                    "success": False,
                    "message": "That profile is no longer available. Choose a listed profile.",
                }
            minutes = self._duration(params)
            if not self._sim:
                await self.request_config_update({"default_profile": profile, "lecture_minutes": minutes})
            self._default, self._minutes = profile, minutes
            self.set_state("profile", profile)
            await self._refresh()
            return {"message": f"Selected {profile} for {minutes} minutes."}

    def _forget_inputs(self):
        for key in self._inputs:
            self.deregister_child("input", key)
        self._inputs.clear()
        self._pictures.clear()

    def _update_inputs(self, rows):
        for key in self._inputs.difference(rows):
            self.set_child_state_batch("input", key, INPUT_CLEAR)
        self._pictures = {}
        options = []
        for key, (props, picture) in rows.items():
            self.register_child("input", key)
            self.set_child_state_batch("input", key, props)
            self._inputs.add(key)
            if picture:
                self._pictures[key] = picture
                options.append({"value": key, "label": props["name"]})
        self.set_state("snapshot_options", json.dumps(options))

    async def _block(self, key):
        root = await self._api.xml(SOURCE_API + key + "_capture")
        node = root.find(key)
        if node is None and root.find("api-versions") is None:
            raise ConnectionError(f"EchoVideo did not return a recognizable {key} capture status.")
        return node, timestamp(value(root, "wall-clock-time"))

    async def _read_session(self):
        current, clock = await self._block("current")
        upcoming, _ = await self._block("next")
        states = capture_values(current, upcoming, clock)
        source = await self._source_status(current, states)
        self._update_inputs(source_rows(source))
        self._clock = clock
        self._early = self._early_booking(states)
        self._publish(states)

    @staticmethod
    def _needs_monitor(current, states):
        if current is None or not states["current_start"]:
            return False
        if states["capture_state"] in ("Idle", "Preparing", "Finishing"):
            return False
        explicit = states["capture_state"] in ("Recording", "Paused")
        return not explicit or current.find("sources") is None

    async def _source_status(self, current, states):
        if not self._needs_monitor(current, states):
            return current
        monitor = await self._optional_monitor()
        # An old snapshot from a different lecture cannot supply this one's sources.
        if monitor is None or not same_start(value(monitor, "start-time"), states["current_start"]):
            return current
        phase, raw = capture_phase(monitor)
        if phase in ("Recording", "Paused"):
            states.update(capture_flags(phase, raw))
        return monitor if monitor.find("sources") is not None else current

    async def _optional_monitor(self):
        try:
            return await self._api.xml(SOURCE_API + "monitoring")
        except ConnectionFaultError:
            raise
        except (EchoError, ConnectionError, TimeoutError):
            return None

    def _early_booking(self, states):
        field = {"Preparing": "current_start", "Idle": "next_start"}.get(states["capture_state"])
        if field is None:
            return False
        start = timestamp(states[field])
        if None in (start, self._clock):
            return False
        return 0 <= (start - self._clock).total_seconds() <= 300

    async def _refresh(self):
        try:
            if self._sync_due <= time.monotonic():
                await self._system()
            await self._read_session()
        except (EchoError, ConnectionError, TimeoutError) as exc:
            self._unavailable("Recorder status unavailable")
            if isinstance(exc, EchoError):
                raise ConnectionError(str(exc)) from exc
            raise

    async def poll(self):
        async with self._lock:
            await self._refresh()

    def _start_option(self, states):
        if self._early:
            return True, "Scheduled class ready to start"
        phase = states["capture_state"]
        if phase != "Idle":
            return False, phase
        if not self._profile():
            return False, "Choose a capture profile in Set lecture defaults"
        if self._overlaps(states["next_start"], 60 * self._minutes):
            return False, "Choose a shorter lecture before the next class"
        return True, phase

    def _publish(self, states):
        phase = states["capture_state"]
        observed = (phase, states["current_start"], states["duration_seconds"])
        if self._pending_done(observed):
            self._pending = ""
        start, status = self._start_option(states)
        availability = {
            "can_start": start,
            "can_finish": phase in ACTIVE,
            "can_extend": phase in ACTIVE,
            "can_resume": phase in ("Active", "Paused"),
            "can_pause": phase in ("Active", "Recording"),
        }
        if self._pending:
            availability = dict.fromkeys(availability, False)
        self.set_states({**states, **availability, "control_status": self._pending or status})

    def _pending_done(self, observed):
        if not self._pending or time.monotonic() >= self._until:
            return True
        phase = observed[0]
        if self._pending == "Starting":
            return phase in ACTIVE
        if self._pending == "Finishing":
            return phase == "Idle"
        if self._pending == "Adding time":
            return observed[1:] != self._before[1:]
        return phase in ({"Paused"} if self._pending == "Pausing" else {"Recording"})

    async def _apply(self, path, message, fields=None):
        self._before = tuple(
            self.get_state(k) for k in ("capture_state", "current_start", "duration_seconds")
        )
        self._pending = message
        # Generic Active can persist through a pause or resume. Read back, then
        # allow an explicit complementary control after a short settling period.
        self._until = time.monotonic() + (5 if message in ("Pausing", "Resuming") else 25)
        self.set_states(
            {
                "control_status": message,
                **dict.fromkeys(("can_start", "can_finish", "can_pause", "can_resume", "can_extend"), False),
            }
        )
        try:
            await self._api.xml("/capture/" + path, {} if fields is None else fields)
        except (EchoError, ConnectionFaultError):
            self._pending = ""
            await self._refresh()
            raise
        except (ConnectionError, TimeoutError) as exc:
            self._unavailable("Request outcome unknown. Read recorder status.")
            raise ConnectionError(
                "The recorder did not confirm the request. Read recorder status before repeating it."
            ) from exc
        await self._refresh()

    async def send_command(self, command, params=None):
        async with self._lock:
            if command == "get_system":
                self._sync_due = 0
            await self._refresh()
            if command in ("get_system", "get_status"):
                return
            if command == "download_snapshot":
                return await self._snapshot((params or {}).get("input"))
            if self._pending:
                raise EchoError(f"{self._pending}. Wait for the recorder to update.")
            await self._operate(command, params or {})

    async def _operate(self, command, params):
        if command == "add_five_minutes":
            command, params = "extend", {"minutes": 5}
        if command in ("start_lecture", "start_named", "start_early"):
            await self._start(command, params)
            return
        actions = {
            "pause": ("can_pause", "pause", "Pausing"),
            "resume": ("can_resume", "record", "Resuming"),
            "finish": ("can_finish", "stop", "Finishing"),
            "extend": ("can_extend", "extend", "Adding time"),
        }
        if command not in actions:
            raise EchoError("Unknown EchoVideo command.")
        flag, endpoint, message = actions[command]
        if not self.get_state(flag):
            raise EchoError("That command is unavailable in the recorder's current state.")
        fields = (
            {"duration": str(60 * checked_minutes(params.get("minutes", 5)))} if command == "extend" else {}
        )
        await self._apply(endpoint, message, fields)

    async def _start(self, command, params):
        if self._early and command != "start_named":
            await self._apply("record", "Starting")
            return
        if command == "start_early":
            raise EchoError("There is no scheduled class in its early-start window.")
        if self._early or self.get_state("capture_state") != "Idle":
            raise EchoError("The lectern is occupied by a capture or its scheduled class.")
        await self._new_lecture(params)

    async def _new_lecture(self, params):
        profile = params.get("profile") or self._profile()
        if profile not in self._profiles:
            raise EchoError("Choose a profile reported by this recorder in Set lecture defaults.")
        duration = 60 * self._duration(params)
        if self._overlaps(self.get_state("next_start"), duration):
            raise EchoError("This lecture would overlap the next class. Choose a shorter length.")
        title = self._title(params.get("title"))
        await self._apply(
            "new_capture",
            "Starting",
            {"duration": str(duration), "capture_profile_name": profile, "description": title},
        )

    def _title(self, title):
        room = self.get_state("room") or self.get_state("recorder_host") or "Classroom"
        return str(title or "").strip() or f"{room} lecture"

    def _duration(self, params):
        chosen = params.get("minutes")
        return self._minutes if chosen in (None, "") else checked_minutes(chosen)

    def _overlaps(self, raw, duration):
        if not raw:
            return False
        start = timestamp(raw)
        if None in (self._clock, start):
            return True
        return self._clock + timedelta(seconds=duration) > start

    async def _snapshot(self, key):
        name = self._pictures.get(key)
        if not name:
            raise EchoError("Choose a current input with a snapshot.")
        raw = await self._api.request("/monitoring/" + quote(name, safe=""))
        if not raw.startswith(b"\xff\xd8\xff"):
            raise EchoError("The recorder did not return a JPEG image.")
        return DeviceFile(f"echo-{key}.jpg", raw, "image/jpeg")
