"""An EchoVideo classroom recorder with an independent schedule, input signals and upload queue."""

import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from xml.etree import ElementTree as ET

from aiohttp import web
from openavc.simulator.http_simulator import HTTPSimulator

PROFILES = ("Camera & slides", "Slides only", "Audio only")
MODELS = ("Classroom (Windows)", "Classroom (Mac)", "EchoVideo Pro", "EchoVideo Pod")
AUTH = "Basic " + base64.b64encode(b"instructor:simulation").decode()
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAAJABADAREAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAb/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCfFahAAAH/2Q=="
)


def now():
    return datetime.now(timezone.utc)


def date(instant):
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def tag(node, key, val=None):
    element = ET.SubElement(node, key)
    if val is not None:
        element.text = str(val)
    return element


def xml(root):
    return 200, ET.tostring(root, encoding="unicode")


def ack():
    return xml(ET.Element("ok", text="Command submitted"))


def lecture(title, start, length, profile):
    return dict(title=title, start=start, duration=length, profile=profile, section="")


def schedule(node, entry):
    tag(node, "type", "media")
    tag(node, "start-time", date(entry["start"]))
    tag(node, "duration", entry["duration"])
    params = tag(node, "parameters")
    tag(params, "title", entry["title"])
    tag(params, "section", entry["section"])
    if entry["section"]:
        tag(tag(params, "presenters"), "presenter", "Dr Nguyen")
    profile = tag(params, "capture-profile")
    tag(profile, "name", entry["profile"])
    tag(profile, "output-type", "archive")


class EchoSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "echo360_echovideo",
        "name": "Echo360 EchoVideo Capture",
        "category": "streaming",
        "transport": "http",
        "default_port": 8443,
        "tls": True,
        "initial_state": {
            "model": "Classroom (Windows)",
            "local_capture": "Idle",
            "next_class": "None",
            "distinct_feedback": False,
            "camera_signal": True,
            "uploads_pending": 0,
        },
        "controls": [
            {"key": "model", "type": "select", "label": "Managed recorder", "options": list(MODELS)},
            {
                "key": "local_capture",
                "type": "select",
                "label": "Local capture control",
                "options": ["Idle", "Recording", "Paused"],
            },
            {
                "key": "next_class",
                "type": "select",
                "label": "Next scheduled class",
                "options": ["None", "In 3 minutes", "In 25 minutes"],
            },
            {
                "key": "distinct_feedback",
                "type": "toggle",
                "label": "Report explicit recording and pause states",
            },
            {"key": "camera_signal", "type": "toggle", "label": "Camera signal present"},
            {
                "key": "uploads_pending",
                "type": "slider",
                "label": "Captures awaiting upload",
                "min": 0,
                "max": 10,
                "step": 1,
            },
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self.current = None
        self.upcoming = None

    def set_state(self, key, val):
        super().set_state(key, val)
        if not hasattr(self, "current"):
            return
        if key == "next_class":
            minutes = {"In 3 minutes": 3, "In 25 minutes": 25}.get(val)
            self.upcoming = self.booking(minutes)
        if key == "local_capture":
            if val == "Idle":
                self.current = None
            elif not self.current:
                self.current = lecture("Lecture started at the lectern", now(), 3600, PROFILES[0])

    @staticmethod
    def booking(minutes):
        if not minutes:
            return None
        upcoming = lecture("Engineering seminar", now() + timedelta(minutes=minutes), 3000, PROFILES[0])
        upcoming["section"] = "Engineering 201"
        return upcoming

    def update_clock(self):
        if self.current and now() >= self.current["start"] + timedelta(seconds=self.current["duration"]):
            self.finish()
        if self.upcoming and now() >= self.upcoming["start"]:
            self.begin_scheduled()

    def begin_scheduled(self):
        self.current, self.upcoming = self.upcoming, None
        self.current["duration"] += max(0, int((self.current["start"] - now()).total_seconds()))
        self.current["start"] = now()
        super().set_state("local_capture", "Recording")
        super().set_state("next_class", "None")

    def finish(self):
        self.current = None
        super().set_state("local_capture", "Idle")
        super().set_state("uploads_pending", int(self.get_state("uploads_pending") or 0) + 1)

    def system(self):
        root = self.status_root()
        for name in PROFILES:
            tag(root.find("capture-profiles"), "capture-profile", name)
        model = self.get_state("model")
        for key, v in {
            "location": "Science Hall 101",
            "host-address": "lectern-101",
            "serial-number": "02-00-00-00-01-01",
            "system-version": f"Simulated {model}",
            "last-sync": date(now()),
        }.items():
            tag(root, key, v)
        content = tag(root, "content")
        tag(content, "state", "active" if self.get_state("uploads_pending") else "idle")
        tag(content, "uploads-pending", self.get_state("uploads_pending"))
        tag(content, "archive-space-usage", 18.5)
        return xml(root)

    @staticmethod
    def status_root():
        root = ET.Element("status")
        tag(root, "wall-clock-time", date(now()))
        tag(tag(root, "api-versions"), "api-version", "3.0")
        tag(root, "capture-profiles")
        return root

    def add_sources(self, container):
        sources = tag(container, "sources")
        audio = tag(sources, "source")
        for key, val in {
            "class": "audio",
            "name": "Lectern microphone",
            "format": "pcm",
            "signal-present": "true",
        }.items():
            tag(audio, key, val)
        channels = tag(audio, "channels")
        for position, peak in (("left", 5), ("right", 4)):
            channel = tag(channels, "channel")
            for key, val in (("position", position), ("average", 1), ("peak", peak)):
                tag(channel, key, val)
        if self.current["profile"] != "Audio only":
            self.visual(sources, "display", "Lectern display", True)
        if self.current["profile"] == PROFILES[0]:
            self.visual(sources, "video", "Lectern camera", self.get_state("camera_signal"))

    @staticmethod
    def visual(sources, kind, name, present):
        row = tag(sources, "source")
        for key, val in {
            "class": kind,
            "name": name,
            "signal-present": str(bool(present)).lower(),
            "thumbnail": kind + ".jpg",
            "format": "1080p",
        }.items():
            tag(row, key, val)

    def captures(self, which):
        root = self.status_root()
        if which == "next_capture":
            upcoming = tag(root, "next")
            if self.upcoming:
                schedule(upcoming, self.upcoming)
            return xml(root)
        self.current_status(root if which == "monitoring" else tag(root, "current"))
        return xml(root)

    def current_status(self, current):
        scheduled = tag(current, "schedule")
        if self.current:
            schedule(scheduled, self.current)
            raw = self.get_state("local_capture").lower() if self.get_state("distinct_feedback") else "active"
            tag(current, "state", raw)
            tag(current, "start-time", date(self.current["start"]))
            tag(current, "duration", self.current["duration"])
            tag(current, "output-type", "archive")
            self.add_sources(current)

    def new_lecture(self, fields):
        duration = int(fields.get("duration", 0))
        profile = fields.get("capture_profile_name")
        valid = all((0 < duration <= 28800, profile in PROFILES, fields.get("description")))
        if self.current or not valid:
            return 409, ""
        if self.upcoming and now() + timedelta(seconds=duration) > self.upcoming["start"]:
            return 409, ""
        self.current = lecture(fields["description"], now(), duration, profile)
        super().set_state("local_capture", "Recording")
        return ack()

    def record(self):
        if self.current and self.get_state("local_capture") == "Paused":
            super().set_state("local_capture", "Recording")
        elif self.early_booking():
            self.begin_scheduled()
        return ack()

    def early_booking(self):
        if self.current or not self.upcoming:
            return False
        return 0 <= (self.upcoming["start"] - now()).total_seconds() <= 300

    def extend(self, fields):
        extra = int(fields.get("duration", 0))
        length = self.current["duration"] + extra
        if self.upcoming:
            length = min(length, int((self.upcoming["start"] - self.current["start"]).total_seconds()))
        self.current["duration"] = max(self.current["duration"], length)
        return ack()

    def command(self, op, fields):
        if op == "new_capture":
            return self.new_lecture(fields)
        if op == "record":
            return self.record()
        if not self.current:
            return 409, ""
        if op == "extend":
            return self.extend(fields)
        if op == "pause":
            super().set_state("local_capture", "Paused")
        elif op == "stop":
            self.finish()
        else:
            return 404, ""
        return ack()

    def handle_request(self, method, path, headers, body):
        if headers.get("Authorization") != AUTH:
            return 401, ""
        self.update_clock()
        if method == "GET":
            return self.read_status(path)
        if method == "POST" and path.startswith("/capture/"):
            return self.command(path.split("/")[-1], {k: v[0] for k, v in parse_qs(body).items()})
        return 404, ""

    def read_status(self, path):
        if path == "/status/system":
            return self.system()
        if path in ("/status/current_capture", "/status/next_capture", "/status/monitoring"):
            return self.captures(path.split("/")[-1])
        return 404, ""

    async def respond_http(self, request, method, path, *payload):
        headers, body = payload
        if method == "GET" and path in ("/monitoring/video.jpg", "/monitoring/display.jpg"):
            if headers.get("Authorization") != AUTH:
                return web.Response(status=401)
            if not self.current:
                return web.Response(status=404)
            return web.Response(body=JPEG, content_type="image/jpeg")
        return await super().respond_http(request, method, path, headers, body)
