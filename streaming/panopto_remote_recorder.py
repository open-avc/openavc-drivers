"""Panopto Windows Remote Recorder via the public 4.6 SOAP services.
Python handles paginated rosters, XML namespaces, and session identity across
commands. Recorder status and session identity come from separate vendor calls.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import ssl
import time
from urllib.parse import urlsplit
from uuid import UUID
import xml.etree.ElementTree as ET

import aiohttp
from defusedxml import ElementTree as SafeXML

from openavc.drivers.base import BaseDriver, ConnectionFaultError

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
SERVICE = "http://tempuri.org/"
DATA = "http://schemas.datacontract.org/2004/07/Panopto.Server.Services.PublicAPI.V40"
ARRAY = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
RECORDERS = "RemoteRecorderManagement"
SESSIONS = "SessionManagement"
PAGE = 100
STATES = {
    "Stopped": "Idle",
    "Previewing": "Previewing",
    "Recording": "Recording",
    "Paused": "Paused",
    "Faulted": "Faulted",
    "Disconnected": "Disconnected",
    "Blocked": "Desktop recorder open",
}
ACTIVE = {"Recording", "Paused"}
READY = {"Idle", "Previewing"}
UNKNOWN = {
    "recording": None,
    "recorder_status": "Unknown",
    "lecture_title": "",
    "can_stop": False,
    "can_start": False,
}


class RecorderError(RuntimeError):
    """An actionable refusal or an unusable Panopto reply."""


class Unconfirmed(RecorderError):
    """The site may have applied a request without returning a usable acknowledgement."""


def field(name, value=None, ns=SERVICE):
    node = ET.Element(f"{{{ns}}}{name}")
    if isinstance(value, (list, tuple)):
        node.extend(value)
    elif value is not None:
        node.text = str(value)
    return node


def data(name, value):
    return field(name, value, DATA)


def identifier(value):
    try:
        uid = UUID(str(value))
    except (ValueError, TypeError):
        return ""
    return str(uid) if uid.int else ""


def text(node, name):
    return (node.findtext("{*}" + name) or "").strip()


def ids(node, name):
    return {uid for item in node.findall("{*}" + name + "/{*}guid") if (uid := identifier(item.text))}


def utc():
    return datetime.now(timezone.utc)


def wire_time(instant):
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def valid_time(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date if date.utcoffset() is not None else None
    except (ValueError, AttributeError):
        return None


def paging(page):
    return [data("MaxNumberResults", PAGE), data("PageNumber", page)]


def roster_names(rows):
    found = {}
    for row in rows:
        uid = identifier(text(row, "Id"))
        if uid:
            found[uid] = text(row, "Name") or uid
    return found


def choices(items):
    counts = Counter(items.values())
    return json.dumps(
        [
            {"value": key, "label": name if counts[name] == 1 else f"{name} ({key[:8]})"}
            for key, name in sorted(items.items(), key=lambda kv: (kv[1].casefold(), kv[0]))
        ]
    )


def origin(config):
    host = str(config.get("host") or "").strip()
    parsed = site_address(host)
    name = parsed.hostname
    port = parsed.port or int(config.get("port", 443))
    return f"https://{'[' + name + ']' if ':' in name else name}:{port}"


def site_address(host):
    parsed = urlsplit(host if "://" in host else "https://" + host)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Enter the HTTPS address of the university’s Panopto site.")
    if any((parsed.username, parsed.password, parsed.query, parsed.fragment)):
        raise ValueError("Enter the site's hostname or its /Panopto address.")
    if parsed.path.rstrip("/").lower() not in ("", "/panopto"):
        raise ValueError("Enter the site's hostname or its /Panopto address.")
    return parsed


def login_error():
    return ConnectionFaultError(
        "Panopto declined the login. Check the internal account and password.", code="auth_failed"
    )


def fault_error(fault, action):
    reason = text(fault, "faultstring").lower()
    if any(word in reason for word in ("authenticat", "invalid credential", "invalid login")):
        return login_error()
    if any(word in reason for word in ("access", "permission", "authoriz")):
        return RecorderError(f"Panopto denied {action}. Check the account’s recorder and folder permissions.")
    return RecorderError(f"Panopto declined {action}. Check the recorder, schedule and account permissions.")


def soap_result(raw, action, status):
    try:
        root = SafeXML.fromstring(raw)
    except (ET.ParseError, ValueError) as exc:
        raise Unconfirmed(
            f"Panopto did not return a SOAP reply to {action} (HTTP {status}). Check the site address."
        ) from exc
    fault = root.find(f"{{{SOAP}}}Body/{{{SOAP}}}Fault")
    if fault is not None:
        raise fault_error(fault, action)
    result = root.find(f"{{{SOAP}}}Body/{{{SERVICE}}}{action}Response/{{{SERVICE}}}{action}Result")
    if status != 200 or result is None:
        raise Unconfirmed(f"Panopto did not confirm {action} (HTTP {status}).")
    return result


def check_http(status, action):
    if status == 401:
        raise login_error()
    if status == 403:
        raise RecorderError(
            f"Panopto denied {action}. Give this account access to the lectern and recording folder."
        )
    if status == 429:
        raise ConnectionError("Panopto is limiting requests. Increase the status polling interval.")


async def response_body(response, action):
    check_http(response.status, action)
    raw = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        raw.extend(chunk)
        if len(raw) > 8 * 1024 * 1024:
            raise Unconfirmed("The Panopto response exceeded the page size limit.")
    return raw


class Site:
    def __init__(self, client, base, auth, trace):
        self.client, self.base, self.trace = client, base, trace
        self.auth = auth

    async def call(self, service, action, params):
        try:
            return await self._post(service, action, params)
        except aiohttp.ClientConnectorCertificateError as exc:
            raise ConnectionFaultError(
                "The Panopto site's HTTPS certificate was not trusted.", code="tls_cert_untrusted"
            ) from exc
        except aiohttp.ClientError as exc:
            raise ConnectionError("The connection to the campus Panopto site failed.") from exc

    async def _post(self, service, action, params):
        operation = field(action, [self.auth, *params])
        body = ET.tostring(field("Envelope", [field("Body", [operation], SOAP)], SOAP), encoding="utf-8")
        endpoint = f"{self.base}/Panopto/PublicAPI/4.6/{service}.svc"
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{SERVICE}I{service}/{action}"'}
        if self.trace:
            self.trace("tx", action)
        async with self.client.post(endpoint, headers=headers, data=body, allow_redirects=False) as response:
            if self.trace:
                self.trace("rx", f"{action} HTTP {response.status}")
            raw = await response_body(response, action)
        return soap_result(raw, action, response.status)

    async def lectern_page(self, page):
        reply = await self.call(
            RECORDERS, "ListRecorders", [field("pagination", paging(page)), field("sortBy", "Name")]
        )
        return reply.findall("{*}PagedResults/{*}RemoteRecorder"), text(reply, "TotalResultCount")

    async def folder_page(self, page):
        query = field(
            "request",
            [data("Pagination", paging(page)), data("SortBy", "Name"), data("SortIncreasing", "true")],
        )
        reply = await self.call(SESSIONS, "GetCreatorFoldersList", [query])
        return reply.findall("{*}Results/{*}Folder"), text(reply, "TotalNumberResults")

    @staticmethod
    async def roster(fetch_page):
        found = {}
        for page in range(1000):
            rows, total = await fetch_page(page)
            if not rows:
                return found
            before = len(found)
            found.update(roster_names(rows))
            if total.isdigit() and len(found) >= int(total):
                return found
            if before == len(found):
                raise RecorderError("Panopto repeated a roster page. Refresh the directory and try again.")
        raise RecorderError("The campus directory exceeded the supported page count.")

    async def recorder(self, uid):
        reply = await self.call(
            RECORDERS, "GetRemoteRecordersById", [field("remoteRecorderIds", [field("guid", uid, ARRAY)])]
        )
        return next((r for r in reply.findall("{*}RemoteRecorder") if identifier(text(r, "Id")) == uid), None)

    async def default_folder(self, uid):
        result = await self.call(RECORDERS, "GetDefaultFolderForRecorder", [field("remoteRecorderId", uid)])
        return identifier(result.text)

    async def find_session(self, uid):
        request = field(
            "request",
            [
                data("Pagination", paging(0)),
                data("RemoteRecorderId", uid),
                data("States", [data("SessionState", s) for s in ("Recording", "Broadcasting")]),
            ],
        )
        reply = await self.call(SESSIONS, "GetSessionsList", [request])
        rows = reply.findall("{*}Results/{*}Session")
        if len(rows) != 1 or text(reply, "TotalNumberResults") != "1":
            return None
        return rows[0] if self.is_active(rows[0], uid) else None

    @staticmethod
    def is_active(row, uid):
        if text(row, "State") not in ("Recording", "Broadcasting"):
            return False
        return ids(row, "RemoteRecorderIds") == {uid} and bool(
            identifier(text(row, "Id")) and valid_time(text(row, "StartTime"))
        )

    async def start(self, uid, folder, lecture):
        title, start, end = lecture
        settings = data(
            "RecorderSettings",
            [data("RecorderId", uid), data("SuppressPrimary", "false"), data("SuppressSecondary", "false")],
        )
        params = [
            field("name", title),
            field("folderId", folder),
            field("isBroadcast", "false"),
            field("start", wire_time(start)),
            field("end", wire_time(end)),
            field("recorderSettings", [settings]),
        ]
        reply = await self.call(RECORDERS, "ScheduleRecording", params)
        self.check_conflicts(reply)
        sessions = ids(reply, "SessionIDs")
        if len(sessions) != 1:
            raise Unconfirmed("Panopto did not identify a single new lecture. Refresh recording status.")

    @staticmethod
    def check_conflicts(reply):
        if text(reply, "ConflictsExist").lower() == "true":
            raise RecorderError(
                "This would overlap another recording. Check the room schedule or choose a shorter duration."
            )
        if text(reply, "ConflictsExist").lower() != "false":
            raise Unconfirmed("Panopto did not confirm the schedule change. Refresh recording status.")

    async def stop(self, session):
        result = await self.call(
            RECORDERS,
            "UpdateRecordingTime",
            [
                field("sessionId", identifier(text(session, "Id"))),
                field("start", text(session, "StartTime")),
                field("end", wire_time(utc())),
            ],
        )
        self.check_conflicts(result)


class PanoptoRemoteRecorder(BaseDriver):
    DRIVER_INFO = {
        "id": "panopto_remote_recorder",
        "name": "Panopto Remote Recorder",
        "manufacturer": "Panopto",
        "category": "streaming",
        "author": "OpenAVC",
        "version": "1.0.0",
        "min_platform_version": "0.34.0",
        "description": "Choose a registered Windows lectern by name. Start and stop lectures and read the recorder's actual state through the campus Panopto site.",
        "source_url": "https://demo.hosted.panopto.com/Panopto/PublicAPI/4.6/RemoteRecorderManagement.svc?singleWsdl",
        "transport": "http",
        "protocols": ["https", "soap"],
        "ports": [443],
        "tags": ["lecture-capture", "recording", "software", "education"],
        "verified": False,
        "simulated": True,
        "web_ui": "https://{host}/Panopto",
        "compatible_models": [
            {
                "manufacturer": "Panopto",
                "models": ["Windows Remote Recorder"],
                "confidence": "untested",
                "notes": "Requires a registered recorder on a Panopto site with public SOAP API 4.6. Simulator qualified.",
            }
        ],
        "help": {
            "overview": "One device represents one registered lectern. Bind Start lecture, Stop lecture, Recording and Recorder status in your room project.",
            "setup": "Enter the university’s Panopto site hostname. Use an internal Panopto account with scheduling access to this lectern and Creator access to its recording folder. In Actions, choose Select lectern, pick the PC by name and save. Leave the folder blank to use the recorder’s Panopto default. Use Refresh directory after adding rooms. The simulator supplies sample lecterns and folders.",
            "connection": "A disconnected recorder can still appear in a connected campus site. Check Recorder status and Control status. Stop requires a single active session belonging only to the selected recorder. Panopto can keep reporting Recording during a local pause.",
        },
        "default_config": {"host": "", "port": 443, "ssl": True, "poll_interval": 5, "lecture_minutes": 60},
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "Panopto site",
                "description": "Campus Panopto hostname, such as university.hosted.panopto.com.",
            },
            "port": {
                "type": "integer",
                "default": 443,
                "min": 1,
                "max": 65535,
                "label": "HTTPS port",
                "advanced": True,
            },
            "username": {
                "type": "string",
                "label": "Panopto account",
                "description": "Use the internal service account provided by campus IT.",
            },
            "password": {"type": "password", "secret": True, "label": "Panopto password"},
            "lecture_minutes": {
                "type": "integer",
                "label": "Default lecture length (minutes)",
                "min": 1,
                "max": 1440,
                "default": 60,
                "description": "Panopto ends the lecture after this long if nobody presses Stop.",
            },
            "recorder_id": {
                "type": "string",
                "advanced": True,
                "label": "Saved lectern ID",
                "description": "Filled by Select lectern. The selection survives a recorder rename.",
            },
            "folder_id": {
                "type": "string",
                "advanced": True,
                "label": "Saved folder ID",
                "description": "Filled by Select lectern. Blank uses the recorder's default folder.",
            },
            "poll_interval": {
                "type": "integer",
                "min": 1,
                "default": 5,
                "label": "Status interval (seconds)",
                "advanced": True,
            },
        },
        "state_variables": {
            "recorder_name": {"type": "string", "label": "Selected lectern"},
            "recorder_status": {"type": "string", "label": "Recorder status"},
            "recording": {"type": "boolean", "label": "Recording"},
            "can_start": {"type": "boolean", "label": "Start available"},
            "can_stop": {"type": "boolean", "label": "Stop available"},
            "lecture_title": {"type": "string", "label": "Current lecture"},
            "control_status": {"type": "string", "label": "Control status"},
            "recorder_options": {"type": "string", "label": "Available lecterns", "cloud_priority": "low"},
            "folder_options": {
                "type": "string",
                "label": "Available recording folders",
                "cloud_priority": "low",
            },
        },
        "commands": {
            "read_status": {
                "label": "Read recorder status",
                "help": "Read this lectern's reported state. Recording lights only when Panopto reports a recording.",
                "query_for": "recorder_status",
            },
            "start_lecture": {
                "label": "Start lecture",
                "help": "Start a lecture recording on this lectern using its configured folder and default length. The Recording indicator follows Panopto.",
                "sets": {"recording": True},
            },
            "start_named_lecture": {
                "label": "Start lecture with options",
                "help": "Start on the selected lectern with a lecture title or custom length. Blank fields use the room defaults.",
                "sets": {"recording": True},
                "params": {
                    "title": {
                        "type": "string",
                        "label": "Lecture title",
                        "help": "Leave blank to use the lectern name and start time.",
                    },
                    "minutes": {
                        "type": "integer",
                        "label": "Length (minutes)",
                        "min": 1,
                        "max": 1440,
                        "help": "Leave blank to use the configured default.",
                    },
                },
            },
            "stop_lecture": {
                "label": "Stop lecture",
                "help": "Finish the current single-lectern recording and keep it in Panopto. Its actual state updates on readback.",
                "sets": {"recording": False},
            },
            "refresh_directory": {
                "label": "Refresh directory",
                "help": "Reload the named lecterns and writable recording folders available to this account.",
            },
        },
        "actions": [
            {
                "id": "select_lectern",
                "kind": "setup",
                "availability": "online",
                "label": "Select lectern",
                "icon": "monitor",
                "params": {
                    "lectern": {
                        "type": "string",
                        "label": "Lectern PC",
                        "required": True,
                        "options_state": "recorder_options",
                    },
                    "destination": {
                        "type": "string",
                        "label": "Recording folder",
                        "options_state": "folder_options",
                        "help": "Leave blank for this lectern's Panopto default.",
                    },
                },
            },
            {"id": "refresh_directory"},
            {"id": "read_status"},
            {"id": "start_lecture"},
            {"id": "stop_lecture"},
        ],
        "quick_actions": ["read_status", "start_lecture", "stop_lecture"],
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._site = None
        self._lock = asyncio.Lock()
        self._lecterns = {}
        self._folders = {}
        self._selected = ""
        self._folder = ""
        self._pending = ""
        self._deadline = 0.0
        self._active = None
        self._sim = False

    async def _create_transport(self, transport_type):
        port = int(self.config.get("port") or 443)
        self._sim = self.config.get("host") == "127.0.0.1" and self.udp_redirect == ("127.0.0.1", port)
        try:
            base = origin(self.config)
        except ValueError as exc:
            raise ConnectionFaultError(str(exc), code="invalid_config") from exc
        self._site = self._open_site(base)
        self._selected = identifier(self.config.get("recorder_id"))
        self._folder = identifier(self.config.get("folder_id"))

    def _open_site(self, base):
        credentials = self._credentials()
        local_address = getattr(self, "local_address", None)
        local = local_address() if local_address and not self._sim else None
        connector = aiohttp.TCPConnector(
            ssl=False if self._sim else ssl.create_default_context(), local_addr=(local, 0) if local else None
        )
        client = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=12), trust_env=not self._sim
        )
        return Site(client, base, credentials, getattr(self, "record_traffic", None))

    def _credentials(self):
        user, password = self.config.get("username"), self.config.get("password")
        if self._sim:
            user, password = "simulator", "simulator"
        if not user or not password:
            raise ConnectionFaultError("The Panopto account or password is missing.", code="auth_failed")
        return field("auth", [data("Password", password), data("UserKey", user)])

    def _link_alive(self):
        return self._site is not None and not self._site.client.closed

    async def _post_connect(self):
        await self._directory()

    async def _directory(self):
        self._lecterns = await self._site.roster(self._site.lectern_page)
        self.set_state("recorder_options", choices(self._lecterns))
        try:
            self._folders = await self._site.roster(self._site.folder_page)
        except RecorderError:
            # A recorder's configured default can still be usable without permission to list folders.
            self._folders = {}
        self.set_state("folder_options", choices(self._folders))

    async def _close_session(self):
        site, self._site = self._site, None
        if site:
            await site.client.close()
        self._active = None
        self._lecterns = {}
        self._folders = {}
        self.set_states(
            {**UNKNOWN, "control_status": "Site offline", "recorder_options": "[]", "folder_options": "[]"}
        )

    async def _initial_sync(self):
        await self.poll()

    async def run_setup_action(self, action_id, params, progress):
        if action_id != "select_lectern":
            return await super().run_setup_action(action_id, params, progress)
        async with self._lock:
            try:
                return await self._select(params, progress)
            except RecorderError as exc:
                return {"success": False, "message": str(exc)}

    async def _select(self, params, progress):
        uid = identifier(params.get("lectern"))
        folder = identifier(params.get("destination"))
        if uid not in self._lecterns:
            raise RecorderError("Refresh the directory and choose a listed lectern.")
        if params.get("destination") and folder not in self._folders:
            raise RecorderError("Refresh the directory and choose a listed recording folder.")
        await progress("Checking the selected lectern and recording folder", 30)
        await self._verify_selection(uid, folder)
        await self._save_selection(uid, folder)
        return {"message": f"Selected {self._lecterns[uid]}."}

    async def _verify_selection(self, uid, folder):
        if await self._site.recorder(uid) is None:
            raise RecorderError("Panopto no longer lists that lectern.")
        if not folder and not await self._site.default_folder(uid):
            raise RecorderError(
                "This lectern has no default recording folder. Choose a writable folder by name."
            )

    async def _save_selection(self, uid, folder):
        if not self._sim:
            await self.request_config_update({"recorder_id": uid, "folder_id": folder})
        if self._selected != uid:
            self._pending = ""
        self._selected, self._folder = uid, folder
        await self._refresh()

    async def poll(self):
        async with self._lock:
            await self._refresh()

    async def _refresh(self):
        if not self._selected:
            self.set_states({**UNKNOWN, "recorder_name": "", "control_status": "Choose Select lectern"})
            return "Unknown"
        try:
            return await self._read_recorder()
        except (RecorderError, aiohttp.ClientError, OSError, TimeoutError, ConnectionFaultError):
            self._active = None
            self.set_states({**UNKNOWN, "control_status": "Recorder status unavailable"})
            raise

    async def _read_recorder(self):
        recorder = await self._site.recorder(self._selected)
        if recorder is None:
            self._active = None
            self.set_states(
                {
                    **UNKNOWN,
                    "recorder_name": "",
                    "control_status": "Lectern unavailable. Check its access or select it again.",
                }
            )
            return "Unknown"
        state = STATES.get(text(recorder, "State"), "Unknown")
        recording = True if state == "Recording" else False if state in READY or state == "Paused" else None
        self._active = None
        self.set_states(
            {"recorder_name": text(recorder, "Name"), "recorder_status": state, "recording": recording}
        )
        detail = ""
        if state in ACTIVE:
            try:
                self._active = await self._site.find_session(self._selected)
            except ConnectionFaultError:
                raise
            except (RecorderError, OSError, TimeoutError):
                detail = "Lecture details unavailable"
        self._publish_controls(state, detail)
        return state

    def _publish_controls(self, state, detail):
        if self._pending_satisfied(state):
            self._pending = ""
        can_stop = self._active is not None
        status = self._session_status(state, detail)
        self.set_states(
            {
                "can_start": state in READY and not self._pending,
                "can_stop": can_stop and not self._pending,
                "lecture_title": text(self._active, "Name") if can_stop else "",
                "control_status": self._pending or status,
            }
        )

    def _pending_satisfied(self, state):
        want = {"Start requested": ACTIVE, "Stop requested": READY}
        return state in want.get(self._pending, ()) or time.monotonic() >= self._deadline

    def _session_status(self, state, detail):
        if detail:
            return detail
        if state in ACTIVE and self._active is None:
            return "Recording session not uniquely identified for this lectern"
        return state

    async def send_command(self, command, params=None):
        async with self._lock:
            if command == "read_status":
                await self._refresh()
            elif command == "refresh_directory":
                await self._directory()
                await self._refresh()
            elif command in ("start_lecture", "start_named_lecture", "stop_lecture"):
                await self._operate(command, params or {})
            else:
                raise ValueError(f"Unknown Panopto command: {command}")

    async def _operate(self, command, params):
        state = await self._refresh()
        if self._pending:
            raise RecorderError(f"{self._pending}. Wait for Panopto to report the change.")
        if command == "stop_lecture":
            await self._stop(state)
        elif state not in ACTIVE:
            await self._start(state, params if command == "start_named_lecture" else {})
        await self._refresh()

    async def _stop(self, state):
        if state in READY:
            return
        if self._active is None:
            raise RecorderError("Panopto has not identified one active lecture on this lectern alone.")
        await self._mutate("Stop requested", self._site.stop(self._active))

    def _lecture(self, params):
        duration = params.get("minutes")
        minutes = int(self.config.get("lecture_minutes", 60) if duration in (None, "") else duration)
        if not 1 <= minutes <= 1440:
            raise ValueError("Choose a lecture length from 1 to 1440 minutes.")
        start = utc()
        title = (
            str(params.get("title") or "").strip()
            or f"{self.get_state('recorder_name')} — {start:%Y-%m-%d %H:%M} UTC"
        )
        return title, start, start + timedelta(minutes=minutes)

    async def _start(self, state, params):
        if state not in READY:
            raise RecorderError("Select a lectern and wait for it to report Idle or Previewing.")
        folder = self._folder or await self._site.default_folder(self._selected)
        if not folder:
            raise RecorderError("Select a recording folder for this lectern.")
        await self._mutate("Start requested", self._site.start(self._selected, folder, self._lecture(params)))

    async def _mutate(self, message, operation):
        self._pending, self._deadline = message, time.monotonic() + 45
        self.set_states({"control_status": message, "can_start": False, "can_stop": False})
        try:
            return await operation
        except ConnectionFaultError:
            self._pending = ""
            raise
        except (Unconfirmed, OSError, TimeoutError) as exc:
            raise RecorderError(
                "The request outcome is unknown. Read recorder status before trying again."
            ) from exc
        except RecorderError as exc:
            self._pending = ""
            self._publish_controls(self.get_state("recorder_status"), str(exc))
            raise
