"""Windows Remote Recorder and campus Panopto SOAP simulator. The lecture
schedule and reported recorder state can change independently of the controller.
"""

from datetime import datetime, timezone
from uuid import uuid4
import xml.etree.ElementTree as ET

from defusedxml import ElementTree as SafeXML
from openavc.simulator.http_simulator import HTTPSimulator

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
API = "http://tempuri.org/"
BASE = "http://schemas.datacontract.org/2004/07/Panopto.Server.Services.PublicAPI."
V42, V46 = BASE + "V42.Soap", BASE + "V46.Soap"
ARRAY = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
PC_A = "d034d563-de71-48c3-b591-6154c4cb4432"
PC_B = "ab2fa590-81ee-4c5f-9478-5a5d93816345"
COURSE = "e27aabf7-f318-4400-9978-7d26d1ef60ff"
OTHER = "dcdfa698-16e7-41b5-bf3d-bdd5dcf847bc"
NAMES = {PC_A: "Science Hall 101", PC_B: "Lecture Theatre B"}
FOLDERS = {COURSE: "Class recordings", OTHER: "Faculty workshops"}


def child(parent, name, value=None, ns=V42):
    item = ET.SubElement(parent, f"{{{ns}}}{name}")
    if value is not None:
        item.text = str(value)
    return item


def value(node, path):
    return node.findtext("/".join("{*}" + name for name in path.split("/"))) or ""


def now():
    return datetime.now(timezone.utc)


class PanoptoSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "panopto_remote_recorder",
        "name": "Panopto Remote Recorder",
        "category": "streaming",
        "transport": "http",
        "default_port": 443,
        "tls": True,
        "initial_state": {"lectern_state": "Stopped", "schedule_conflict": False},
        "controls": [
            {
                "key": "lectern_state",
                "type": "select",
                "label": "Science Hall 101 recorder",
                "options": ["Stopped", "Previewing", "Recording", "Disconnected", "Blocked", "Faulted"],
            },
            {"key": "schedule_conflict", "type": "toggle", "label": "Conflicting campus booking"},
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self.sessions = {}
        self.states = {PC_A: "Stopped", PC_B: "Stopped"}
        self.commands = {
            name: callback
            for name, callback in (
                ("ListRecorders", self.recorders),
                ("GetRemoteRecordersById", self.recorder),
                ("GetCreatorFoldersList", self.folders),
                ("GetDefaultFolderForRecorder", self.default_folder),
                ("ScheduleRecording", self.schedule),
                ("UpdateRecordingTime", self.finish),
                ("GetSessionsList", self.active),
            )
        }

    def set_state(self, key, state):
        super().set_state(key, state)
        if key != "lectern_state" or not hasattr(self, "sessions"):
            return
        self.states[PC_A] = state
        if state == "Recording" and PC_A not in self.sessions:
            self.make_session(PC_A, "Campus-scheduled lecture", now().isoformat(), "")
        if state in ("Stopped", "Previewing"):
            self.sessions.pop(PC_A, None)

    def make_session(self, uid, title, start, end):
        self.sessions[uid] = {"Id": str(uuid4()), "Name": title, "StartTime": start, "EndTime": end}
        self.states[uid] = "Recording"
        if uid == PC_A:
            super().set_state("lectern_state", "Recording")
        return self.sessions[uid]["Id"]

    def handle_request(self, method, path, headers, body):
        envelope = ET.Element(f"{{{SOAP}}}Envelope")
        container = child(envelope, "Body", ns=SOAP)
        if method != "POST" or path not in (
            "/Panopto/PublicAPI/4.6/RemoteRecorderManagement.svc",
            "/Panopto/PublicAPI/4.6/SessionManagement.svc",
        ):
            return 404, ""
        op = SafeXML.fromstring(body).find(f"{{{SOAP}}}Body")[0]
        action = op.tag.removeprefix(f"{{{API}}}")
        category = path.rsplit("/", 1)[1].split(".")[0]
        soap_action = next((v.strip('"') for k, v in headers.items() if k.lower() == "soapaction"), "")
        if soap_action != f"{API}I{category}/{action}" or action not in self.commands:
            return 400, ""
        if not value(op, "auth/UserKey") or value(op, "auth/Password") != "simulator":
            return 401, ""
        response = child(container, action + "Response", ns=API)
        result = child(response, action + "Result", ns=API)
        self.finish_due_recordings()
        self.commands[action](op, result)
        return 200, ET.tostring(envelope, encoding="unicode")

    def recorder_row(self, parent, uid):
        row = child(parent, "RemoteRecorder")
        child(row, "Id", uid)
        child(row, "Name", NAMES[uid])
        bookings = child(row, "ScheduledRecordings")
        if uid in self.sessions:
            child(bookings, "guid", self.sessions[uid]["Id"], ARRAY)
        child(row, "State", self.states[uid])

    def recorders(self, op, result):
        rows = child(result, "PagedResults")
        if value(op, "pagination/PageNumber") == "0":
            for uid in NAMES:
                self.recorder_row(rows, uid)
        child(result, "TotalResultCount", len(NAMES))

    def recorder(self, op, result):
        uid = value(op, "remoteRecorderIds/guid")
        if uid in NAMES:
            self.recorder_row(result, uid)

    def folders(self, op, result):
        rows = child(result, "Results", ns=V46)
        if value(op, "request/Pagination/PageNumber") == "0":
            for uid, name in FOLDERS.items():
                row = child(rows, "Folder", ns=V46)
                child(row, "Id", uid, V46)
                child(row, "Name", name, V46)
        child(result, "TotalNumberResults", len(FOLDERS), V46)

    def default_folder(self, op, result):
        if value(op, "remoteRecorderId") in NAMES:
            result.text = COURSE

    def schedule(self, op, result):
        uid = value(op, "recorderSettings/RecorderSettings/RecorderId")
        conflict = self.get_state("schedule_conflict") or uid in self.sessions
        child(result, "ConflictsExist", str(bool(conflict)).lower())
        if conflict:
            return
        if uid in NAMES and value(op, "folderId") in FOLDERS:
            sid = self.make_session(uid, value(op, "name"), value(op, "start"), value(op, "end"))
            child(child(result, "SessionIDs"), "guid", sid, ARRAY)

    def finish(self, op, result):
        sid = value(op, "sessionId")
        uid = next((k for k, v in self.sessions.items() if v["Id"] == sid), None)
        child(result, "ConflictsExist", str(uid is None).lower())
        if uid:
            self.mark_stopped(uid)
            child(child(result, "SessionIDs"), "guid", sid, ARRAY)

    def finish_due_recordings(self):
        for uid, session in list(self.sessions.items()):
            end = session["EndTime"]
            if end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= now():
                self.mark_stopped(uid)

    def mark_stopped(self, uid):
        del self.sessions[uid]
        self.states[uid] = "Stopped"
        if uid == PC_A:
            super().set_state("lectern_state", "Stopped")

    def session_row(self, rows, uid):
        session = self.sessions[uid]
        row = child(rows, "Session")
        for key in ("Id", "Name"):
            child(row, key, session[key])
        child(child(row, "RemoteRecorderIds"), "guid", uid, ARRAY)
        child(row, "StartTime", session["StartTime"])
        child(row, "State", "Recording")

    def active(self, op, result):
        uid = value(op, "request/RemoteRecorderId")
        rows = child(result, "Results", ns=V46)
        if uid in self.sessions:
            self.session_row(rows, uid)
        child(result, "TotalNumberResults", int(uid in self.sessions), V46)
