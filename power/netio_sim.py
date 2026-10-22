"""NETIO simulator: the JSON M2M API and the web login, on one port.

Serves ``/netio.json`` the way a NETIO unit does:

  GET  /netio.json   the status document (Agent, GlobalMeasure, Outputs,
                     Inputs, PAB, Watchdogs, Rules)
  POST /netio.json   a partial document with output changes; answered with
                     the whole status document again

and the web login, the WebSocket API at ``/emweb`` (``NetioWebSession``): HELO,
a hashed sign-in (AUTH), SUBSCRIBE / UNSUBSCRIBE (GET from firmware 5.4), SET
on an output's ctrl, settings and energy counter, and PING from 5.2. A
subscribed outputs/measure and outputs/globalmeasure are pushed every 0.3 s,
as the demo units push them; a subscribed list is pushed when it changes.
Built from the replies NETIO's public demo units gave on 2026-10-16 and from
PyNetioConf (NETIO's library) for what the demos could not show: the 5.4
dialect (reads with GET, output states in outputs/state and not in
outputs/measure) and a refused sign-in, whose wording is not known (its code
is PyNetioConf's). The web login's sign-in refuses the user ``invalid`` and
the password ``invalid``, so the refused-login path can be run.

The model is a choice in the Simulator UI. It sets how many outputs and inputs
there are and which of them are metered, laid out the way NETIO's public demo
units answered on firmware 5.3.1 and 5.3.2 (a K model meters every output and
reports each one's voltage and frequency; an 8QS meters the whole unit and
output 1; a PowerDIN 4PZ outputs 1 and 2; a P model none).

Logins: off by default, so a simulated unit answers whatever is entered (a
real unit can read and write with no login, and the demo units read with
none). Switch "Login required to read" or "to write" on to model NETIO's
factory setup; then no login, or the login ``invalid``, is refused with 401.

Refusals: writing switched off answers 403; the JSON API switched off
answers 404; a malformed or impossible write answers 400 with the error list,
no enclosing braces and no closing bracket, as a PowerPDU 4KB does. The web server restart that
follows saving the JSON API settings is the ``web_server_restarting`` error
mode (500 to everything).

The online firmware update, over the web login, follows PyNetioConf: a check
(SET system/firmwarelist with getFirmwareList) holds system/lock locked while
the unit asks NETIO's update server, then fills fwupdate/list; an update (SET
with updateFw set to an offered link) locks it while the firmware downloads,
then the unit restarts. NETIO's update server is not reached: what it answers
is a choice in the Simulator UI, and the download takes about ten seconds.
While the unit restarts every web login is closed and it answers 503 to
everything. A failed download restarts it on the old firmware and, unless
"Unit tries a failed download again" is off, it tries again, three attempts in
all. Until its first check since it started, fwupdate/list holds one item with
every field empty, as every demo unit's did.

From a PowerPDU 4KB on the bench (firmware 5.2.4 and 5.3.2, 2026-10-21): the
first firmware check after the unit starts gets no answer (switch "First check
after a start gets no answer" off for one that does); the JSON API refuses a
schedule action with 400 for an output with no schedule assigned (here every
output but output 1), while the web login takes the flag; and on firmware
before 5.3 the JSON API answers an energy counter reset with 400 "Internal
error".

Files and restarts, over the web login, as the 4KB's own web interface (5.3.2)
does them: SET system/reset restarts the unit; SET on system/srvkeyupload,
system/srvcertupload, system/fwupdate or system/cfgimport opens an upload, which
from firmware 5.2 (the greeting's serverApiVersion 2.0.0) arrives in chunks on
upload/chunk and before it as one HTTP POST to the uploadPath answered (a 5.1.3
greeting carries no serverApiVersion, which is what the web interface expects
of an older unit). Once a file is in: a key or certificate is kept (the
certificate's fingerprint shows as HTTPS certificate), firmware restarts the
unit a few seconds later, a configuration sets the device name and output
names it holds and restarts the unit. Writing network/servers/config restarts
it too (NETIO's word for 5.1.x). Export Config is the twelve
export/config/<section> topics from 5.2, system/cfgexport before it. A refused
chunk answers with the code the demo units used for a bad parameter; the
unit's own codes for these were not seen.
"""

import asyncio
import base64
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone

from openavc.simulator.http_simulator import HTTPSimulator

# model -> (outputs, inputs, metering)
#   "k"    every output metered, with its own voltage and frequency
#   "q"    the whole unit and output 1
#   "din"  the whole unit and outputs 1-2; 3-4 are relay contacts
#   "none" no metering at all
MODELS = {
    "4KS": (4, 1, "k"),
    "4KB": (4, 1, "k"),
    "8KS": (8, 1, "k"),
    "8KB": (8, 1, "k"),
    "4KF": (4, 0, "k"),
    "FK6": (6, 2, "k"),
    "8QS": (8, 1, "q"),
    "4PS": (4, 0, "none"),
    "2PZ": (2, 0, "none"),
    "4PZ": (4, 2, "din"),
}

VALID_ACTIONS = {0, 1, 2, 3, 4, 5, 6, 20, 21}
MAX_OUTPUTS = 8
SENTINEL = 999.0

# The product family each model's web login names it with (system/info
# brandName, the HELO's devtype).
FAMILY = {
    "4KS": "PowerPDU", "4KB": "PowerPDU", "8KS": "PowerPDU", "8KB": "PowerPDU",
    "FK6": "PowerPDU", "8QS": "PowerPDU", "4PS": "PowerPDU",
    "4KF": "PowerBOX", "2PZ": "PowerCable", "4PZ": "PowerDIN",
}
FIRMWARES = ["5.3.1", "5.4.0", "5.2.4", "5.1.3"]
# A JSON API energy counter reset answers 400 "Internal error" on a PowerPDU 4KB
# on 5.2.4 and works on 5.3.2 (2026-10-21).
JSON_RESET_SINCE = (5, 3, 0)

WEB_PATH = "/emweb"
WEB_PUSH_S = 0.3
WEB_PUBLIC_KEY = "openavc-sim-public-key"
# ctrl request -> the JSON API action it is
WEB_REQUESTS = {"off": 0, "on": 1, "soff": 2, "son": 3, "toggle": 4}
WEB_LIST_TOPICS = ("outputs/list", "inputs/list")
WEB_PUSHED_TOPICS = ("outputs/measure", "outputs/globalmeasure", "outputs/state")

# The online firmware update. The unit rebuilds fwupdate/list about 0.4 s after
# a check unlocks system/lock (PyNetioConf).
FW_NEWER = "5.4.0"
FW_CHECK_S = 1.0
FW_REBUILD_S = 0.4
# Long enough that two restarts in a row are further apart than a driver can
# mistake for one, as on a unit, where each attempt takes minutes.
FW_DOWNLOAD_S = 12.0
FW_RESTART_S = 3.0
FW_RETRY_DELAY_S = 1.0
FW_ATTEMPTS = 3
UPDATE_SERVER = ["offers newer firmware", "offers nothing newer", "does not answer"]
FW_BLANK_ITEM = {"name": "", "version": "", "downloadUrl": "", "downloadUrlNoMcu": "", "releaseNoteEn": ""}

# Files to the unit and restarts. The chunked upload and the twelve export
# sections arrived in 5.2 (the greeting's serverApiVersion 2.0.0).
CHUNKED_SINCE = (5, 2, 0)
UPLOAD_TOPICS = {
    "system/srvkeyupload": "key",
    "system/srvcertupload": "certificate",
    "system/fwupdate": "firmware",
    "system/cfgimport": "configuration",
}
UPLOAD_PATH = "/upload/"
EXPORT_SECTIONS = (
    "inputs", "network", "outputs", "PAB", "protocols", "rules", "schedules",
    "system", "users", "wdtpingers", "mqtt", "nbus",
)
# How long after the last chunk the unit restarts to install firmware.
FW_FILE_INSTALL_S = 2.0


def _version(text):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(text or ""))
    return tuple(int(part) for part in match.groups()) if match else (5, 3, 1)


def _fingerprint(pem):
    """A PEM certificate's SHA-256 fingerprint, AB:CD:..., or the file's own
    SHA-256 when it does not parse."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        der = x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)
    except Exception:  # noqa: BLE001 - not a certificate: its bytes then
        der = pem
    text = hashlib.sha256(der).hexdigest().upper()
    return ":".join(text[i:i + 2] for i in range(0, len(text), 2))


def _token(username, password, public_key, stamp, salt):
    """The sign-in token as NETIO's library builds it (see the driver)."""
    password_hash = base64.b64encode(
        hashlib.sha256(f"{username}{public_key}{password}".encode()).digest()
    ).decode()
    password_token = hashlib.sha256(f"{salt}{password_hash}".encode()).hexdigest()
    mark = str(math.floor(stamp / 10))
    return f"{salt}." + hashlib.sha256(f"{mark}{password_token}".encode()).hexdigest()


class NetioWebSession:
    """One web-login connection: an answer for each request, and the events
    due on each push tick. No aiohttp here, so it runs in tests as well."""

    def __init__(self, sim):
        self.sim = sim
        self.authed = False
        self.stamp = None
        self.subscribed = set()
        self._sent_lists = {}
        self._generation = sim.generation

    def _fw(self):
        return _version(self.sim.get_state("firmware", "5.3.1"))

    def ended(self):
        """Whether the unit restarted since this connection opened."""
        self.sim.fw_tick()
        return self.sim.generation != self._generation

    def handle(self, text):
        try:
            frame = json.loads(text)
        except ValueError:
            return []
        if not isinstance(frame, dict):
            return []
        kind = frame.get("type")
        rid = frame.get("reqId")
        topic = frame.get("topic")

        def ok(data=None):
            reply = {"type": "REPLY", "reqId": rid}
            if data is not None:
                reply["data"] = data
            reply["status"] = "OK"
            return [reply]

        def error(code, message):
            return [{"type": "REPLY", "reqId": rid, "status": "ERROR",
                     "error": {"code": code, "message": f"{message} : "}}]

        if kind == "HELO":
            self.stamp = int(time.time())
            greeting = {
                "protocolVersion": "1.0.0",
                "localTimestamp": self.stamp,
                "publicKey": WEB_PUBLIC_KEY,
                "serverApiVersion": "2.0.0",
                "device": self.sim.web_device(),
            }
            if self._fw() < CHUNKED_SINCE:
                del greeting["serverApiVersion"]
            return ok(greeting)
        if kind == "AUTH":
            return self._auth(frame, ok, error)
        if not self.authed:
            return error("Unauthorized", "Client is not authorized to perform this operation.")
        if kind == "PING":
            if self._fw() < (5, 2, 0):
                return error("UnknownRequestType", "Unknown request type")
            return [{"type": "PONG", "reqId": rid, "status": "OK"}]
        if kind == "GET" and self._fw() < (5, 4, 0):
            return error("UnknownRequestType", "Unknown request type")
        if kind in ("SUBSCRIBE", "GET"):
            data = self.sim.web_topic(str(topic or ""))
            if data is None:
                # The demo units spell it so.
                return error("TopicNotFonud", "Topic not found")
            if kind == "SUBSCRIBE":
                self.subscribed.add(topic)
                if topic in WEB_LIST_TOPICS:
                    self._sent_lists[topic] = json.dumps(data, sort_keys=True)
            return ok(data)
        if kind == "UNSUBSCRIBE":
            self.subscribed.discard(topic)
            return ok()
        if kind == "SET":
            return self.sim.web_set(str(topic or ""), frame.get("data"), ok, error)
        return error("UnknownRequestType", "Unknown request type")

    def _auth(self, frame, ok, error):
        username = str(frame.get("username") or "")
        token = str(frame.get("token") or "")
        salt = token.split(".", 1)[0]
        refused = (
            self.stamp is None
            or "." not in token
            or username == "invalid"
            or token == _token(username, "invalid", WEB_PUBLIC_KEY, self.stamp, salt)
        )
        if refused:
            return error("InvalidCredentials", "Invalid credentials")
        self.authed = True
        return ok({"protocolVersion": "1.0.0", "localTimestamp": int(time.time())})

    def events(self):
        """The events a push tick sends: the metering topics every tick, as the
        units send them, and a list only when it changed."""
        if not self.authed:
            return []
        self.sim._settle()
        frames = []
        for topic in WEB_PUSHED_TOPICS:
            if topic in self.subscribed:
                data = self.sim.web_topic(topic)
                if data is not None:
                    frames.append({"type": "EVENT", "topic": topic, "data": data})
        for topic in WEB_LIST_TOPICS:
            if topic in self.subscribed:
                data = self.sim.web_topic(topic)
                text = json.dumps(data, sort_keys=True)
                if text != self._sent_lists.get(topic):
                    self._sent_lists[topic] = text
                    frames.append({"type": "EVENT", "topic": topic, "data": data})
        return frames


def _output_controls():
    groups = []
    for n in range(1, MAX_OUTPUTS + 1):
        groups.append({
            "type": "group",
            "label": f"Output {n}",
            "controls": [
                {"type": "toggle", "key": f"out{n}_on", "label": "On"},
                {"type": "slider", "key": f"out{n}_load_w", "label": "Load when on",
                 "min": 0, "max": 2000, "step": 5, "unit": "W"},
            ],
        })
    return groups


class NetioSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "netio",
        "name": "NETIO Simulator",
        "category": "power",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "model": "4KS",
            "device_name": "PowerPDU-SIM",
            "firmware": "5.3.1",
            "voltage": 230.0,
            "json_api_enabled": True,
            "read_login_required": False,
            "write_login_required": False,
            "write_enabled": True,
            "web_login_api": True,
            "web_settings_allowed": True,
            "update_server": UPDATE_SERVER[0],
            "update_download_fails": False,
            "update_retries": True,
            # Which update was last started: "full" or "keep outlets on".
            "last_update": "",
            # The SHA-256 fingerprint of the last certificate uploaded, and the
            # last file a command sent (what it was and its size).
            "https_certificate": "",
            "last_file": "",
            "sensor_connected": False,
            "in1_temperature_c": 22.5,
            "in1_humidity": 40.0,
            "watchdog_failed": False,
            "rule_result": True,
            "out1_on": True,
            "out1_name": "Power output 1",
            "out1_load_w": 40.0,
            "out1_delay_ms": 5000,
            "out1_energy_wh": 0.0,
            "out1_energy_nr_wh": 100.0,
            "out1_energy_start": "1970-01-01T00:00:00+0000",
            "out1_schedule": False,
            "out2_on": True,
            "out2_name": "Power output 2",
            "out2_load_w": 40.0,
            "out2_delay_ms": 5000,
            "out2_energy_wh": 0.0,
            "out2_energy_nr_wh": 200.0,
            "out2_energy_start": "1970-01-01T00:00:00+0000",
            "out2_schedule": False,
            "out3_on": True,
            "out3_name": "Power output 3",
            "out3_load_w": 0.0,
            "out3_delay_ms": 5000,
            "out3_energy_wh": 0.0,
            "out3_energy_nr_wh": 300.0,
            "out3_energy_start": "1970-01-01T00:00:00+0000",
            "out3_schedule": False,
            "out4_on": True,
            "out4_name": "Power output 4",
            "out4_load_w": 0.0,
            "out4_delay_ms": 5000,
            "out4_energy_wh": 0.0,
            "out4_energy_nr_wh": 400.0,
            "out4_energy_start": "1970-01-01T00:00:00+0000",
            "out4_schedule": False,
            "out5_on": True,
            "out5_name": "Power output 5",
            "out5_load_w": 0.0,
            "out5_delay_ms": 5000,
            "out5_energy_wh": 0.0,
            "out5_energy_nr_wh": 500.0,
            "out5_energy_start": "1970-01-01T00:00:00+0000",
            "out5_schedule": False,
            "out6_on": True,
            "out6_name": "Power output 6",
            "out6_load_w": 0.0,
            "out6_delay_ms": 5000,
            "out6_energy_wh": 0.0,
            "out6_energy_nr_wh": 600.0,
            "out6_energy_start": "1970-01-01T00:00:00+0000",
            "out6_schedule": False,
            "out7_on": True,
            "out7_name": "Power output 7",
            "out7_load_w": 0.0,
            "out7_delay_ms": 5000,
            "out7_energy_wh": 0.0,
            "out7_energy_nr_wh": 700.0,
            "out7_energy_start": "1970-01-01T00:00:00+0000",
            "out7_schedule": False,
            "out8_on": True,
            "out8_name": "Power output 8",
            "out8_load_w": 0.0,
            "out8_delay_ms": 5000,
            "out8_energy_wh": 0.0,
            "out8_energy_nr_wh": 800.0,
            "out8_energy_start": "1970-01-01T00:00:00+0000",
            "out8_schedule": False,
            "in1_closed": False,
            "in1_s0": 0,
            "in2_closed": False,
            "in2_s0": 0,
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {
                "type": "select", "key": "model", "label": "Model",
                "options": list(MODELS),
            },
            {
                "type": "select", "key": "firmware", "label": "Firmware",
                "options": FIRMWARES,
            },
            {"type": "toggle", "key": "json_api_enabled", "label": "JSON API enabled"},
            {"type": "toggle", "key": "write_enabled", "label": "Writing enabled"},
            {"type": "toggle", "key": "read_login_required", "label": "Login required to read"},
            {"type": "toggle", "key": "write_login_required", "label": "Login required to write"},
            {"type": "toggle", "key": "web_login_api", "label": "Web login available"},
            {"type": "toggle", "key": "web_settings_allowed",
             "label": "Web login may change settings"},
            {
                "type": "group",
                "label": "Firmware update",
                "controls": [
                    {"type": "select", "key": "update_server", "label": "NETIO's update server",
                     "options": UPDATE_SERVER},
                    {"type": "toggle", "key": "update_first_check_unanswered",
                     "label": "First check after a start gets no answer"},
                    {"type": "toggle", "key": "update_download_fails", "label": "Firmware download fails"},
                    {"type": "toggle", "key": "update_retries",
                     "label": "Unit tries a failed download again"},
                ],
            },
            {
                "type": "group",
                "label": "Inputs",
                "controls": [
                    {"type": "toggle", "key": "in1_closed", "label": "Input 1 closed"},
                    {"type": "toggle", "key": "in2_closed", "label": "Input 2 closed"},
                    {"type": "toggle", "key": "sensor_connected", "label": "Sensor on input 1"},
                    {"type": "slider", "key": "in1_temperature_c", "label": "Temperature",
                     "min": -20, "max": 80, "step": 0.5, "unit": "°C"},
                    {"type": "slider", "key": "in1_humidity", "label": "Humidity",
                     "min": 0, "max": 100, "step": 1, "unit": "%"},
                ],
            },
            {"type": "toggle", "key": "watchdog_failed", "label": "Watchdog check failing"},
            {"type": "toggle", "key": "rule_result", "label": "Rule result"},
            *_output_controls(),
        ],
        "error_modes": {
            "web_server_restarting": {
                "description": "The web server restarts after the JSON API settings are saved and "
                               "answers HTTP 500 meanwhile",
                "behavior": "custom",
            },
        },
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        # A unit that has been up for a while, as a real one in a rack has.
        self._started = time.monotonic() - 3 * 86400
        self._metered_at = time.monotonic()
        # output -> (monotonic deadline, on afterwards) for a short on/off in progress
        self._pending = {}
        # The online firmware update: fwupdate/list, a check in progress (when
        # it answers, and the list it answers with or None for no answer), the
        # update in progress, and the restart that ends a download (every web
        # login opened before it is closed; 503 until it is over).
        self._fw_list = [dict(FW_BLANK_ITEM)]
        self._fw_check = None
        self._fw_job = None
        self._fw_asked = False
        self._down_until = 0.0
        self.generation = 0
        # A restart due later (the install after a firmware file), and the
        # uploads open: token -> what, chunks expected, the next index, bytes.
        self._restart_due = None
        self._uploads = {}
        self._upload_seq = 0
        self._https_key = b""
        self._servers = {"httpEnable": True, "httpPort": 80, "httpsEnable": False, "httpsPort": 443}

    # ── Model ──

    def _model(self):
        return MODELS.get(str(self.get_state("model", "4KS")), MODELS["4KS"])

    @staticmethod
    def _schedule_id(n):
        """The schedule assigned to an output: output 1 has one, as the 4KF demo
        unit's did; the rest have none (0)."""
        return 1 if n == 1 else 0

    def _output_metered(self, n):
        outputs, _inputs, metering = self._model()
        if metering == "k":
            return True
        if metering == "q":
            return n == 1
        if metering == "din":
            return n in (1, 2)
        return False

    # ── Requests ──

    def handle_request(self, method, path, headers, body):
        if self.restarting():
            return 503, "Service Unavailable"
        if "web_server_restarting" in self.active_errors:
            return 500, '"errors":[{"code":500,"message":"Internal Server Error"}]'
        clean = path.split("?")[0]
        if method == "HEAD":
            return 200, ""
        if clean != "/netio.json":
            if method == "GET" and clean in ("", "/"):
                return 200, "<!DOCTYPE html><title>PDU Web Config</title>"
            return 404, "Not Found"
        if not self.get_state("json_api_enabled", True):
            return 404, '"errors":[{"code":404,"message":"Not found"}]'
        login = self._login(headers)
        self._settle()
        if method == "GET":
            if self.get_state("read_login_required", False) and not login:
                return self._unauthorized()
            return 200, self._document()
        if method == "POST":
            if self.get_state("write_login_required", False) and not login:
                return self._unauthorized()
            if not self.get_state("write_enabled", True):
                return 403, '"errors":[{"code":403,"message":"Forbidden"}]'
            return self._write(body)
        return 405, "Method Not Allowed"

    @staticmethod
    def _unauthorized():
        # As the demo units answer (text/html there).
        return 401, "401 Unauthorized", {"WWW-Authenticate": 'Basic realm="Login Required"'}

    @staticmethod
    def _login(headers):
        """True for a Basic login other than the designated bad one, ``invalid``."""
        value = ""
        for key, val in headers.items():
            if key.lower() == "authorization":
                value = str(val)
        if not value.lower().startswith("basic "):
            return False
        try:
            user, _, password = base64.b64decode(value[6:].strip()).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return user != "invalid" and password != "invalid"

    def _write(self, body):
        # As a PowerPDU 4KB answers (5.2.4, 5.3.2): no enclosing braces, and
        # the list's closing bracket left off too.
        bad = (400, '"errors":[{"code":400,"message":"Bad request"}')
        try:
            doc = json.loads(body or "")
        except ValueError:
            return bad
        entries = doc.get("Outputs") if isinstance(doc, dict) else None
        if not isinstance(entries, list) or not entries:
            return bad
        outputs = self._model()[0]
        checked = []
        for entry in entries:
            if not isinstance(entry, dict):
                return bad
            n = entry.get("ID")
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= outputs:
                return bad
            action = entry.get("Action", 6)
            if isinstance(action, bool) or not isinstance(action, int) or action not in VALID_ACTIONS:
                return bad
            # A schedule action for an output with no schedule assigned is
            # refused (a PowerPDU 4KB, 5.2.4 and 5.3.2).
            if action in (20, 21) and not self._schedule_id(n):
                return bad
            if entry.get("ResetEnergyCounter") is True and _version(self.get_state("firmware")) < JSON_RESET_SINCE:
                return 400, '"errors":[{"code":400,"message":"Internal error"}'
            delay = entry.get("Delay")
            if delay is not None and (isinstance(delay, bool) or not isinstance(delay, int) or delay <= 0):
                return bad
            checked.append((n, action, delay, entry))
        for n, action, delay, entry in checked:
            self._apply(n, action, delay, entry)
        # A unit answers a switch with a shorter status than a read (see
        # _document); a rename or counter reset gets the full one.
        switched = any(action in (0, 1, 2, 3, 4) for _n, action, _d, _e in checked)
        return 200, self._document(after_switch=switched)

    def _apply(self, n, action, delay, entry):
        if "Name" in entry and str(entry["Name"]).strip():
            self.set_state(f"out{n}_name", str(entry["Name"]))
        if entry.get("ResetEnergyCounter") is True:
            self._meter()
            self.set_state(f"out{n}_energy_wh", 0.0)
            self.set_state(f"out{n}_energy_start", self._now())
        key = f"out{n}_on"
        if action == 0:
            self.set_state(key, False)
        elif action == 1:
            self.set_state(key, True)
        elif action == 4:
            self.set_state(key, not self.get_state(key, False))
        elif action in (2, 3):
            # Short off / short on: switch now, switch back after the time
            # given with the command, else the output's own.
            ms = delay or int(self.get_state(f"out{n}_delay_ms", 5000))
            self.set_state(key, action == 3)
            self._schedule_back(n, action == 2, ms / 1000.0)
        elif action == 20:
            self.set_state(f"out{n}_schedule", False)
        elif action == 21:
            self.set_state(f"out{n}_schedule", True)
        elif action == 6 and "State" in entry:
            self.set_state(key, entry["State"] == 1)

    # ── Short on/off timing ──

    def _schedule_back(self, n, final_on, seconds):
        self._pending[n] = (time.monotonic() + seconds, final_on)
        try:
            asyncio.get_running_loop().call_later(seconds, self._settle)
        except RuntimeError:
            pass  # no loop running: the next request settles it

    def _settle(self):
        now = time.monotonic()
        for n, (deadline, final_on) in list(self._pending.items()):
            if now >= deadline:
                del self._pending[n]
                self.set_state(f"out{n}_on", bool(final_on))

    # ── Document ──

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")

    def _meter(self):
        """Count energy since the last reading: each metered output on draws its load."""
        now = time.monotonic()
        hours = (now - self._metered_at) / 3600.0
        self._metered_at = now
        if hours <= 0:
            return
        for n in range(1, MAX_OUTPUTS + 1):
            if self.get_state(f"out{n}_on") and self._output_metered(n):
                wh = float(self.get_state(f"out{n}_load_w", 0.0)) * hours
                self._state[f"out{n}_energy_wh"] = float(self.get_state(f"out{n}_energy_wh", 0.0)) + wh
                self._state[f"out{n}_energy_nr_wh"] = float(self.get_state(f"out{n}_energy_nr_wh", 0.0)) + wh

    def _load(self, n):
        if not self.get_state(f"out{n}_on"):
            return 0.0
        return float(self.get_state(f"out{n}_load_w", 0.0))

    def _document(self, after_switch=False):
        """The status document. ``after_switch`` renders the answer to a switch
        the way the 4KS demo (5.3.2) sends it: each metered output without its
        own Voltage, Frequency, EnergyNR, ReverseEnergyNR and EnergyStart, and
        PowerFactor 0 where a read says 1.0 for an idle output."""
        self._meter()
        outputs, inputs, metering = self._model()
        voltage = float(self.get_state("voltage", 230.0))
        model = str(self.get_state("model", "4KS"))
        doc = {
            "Agent": {
                "Model": model,
                "DeviceName": str(self.get_state("device_name", "PowerPDU-SIM")),
                "MAC": "24:A4:2C:00:00:01",
                "SerialNumber": "24A42C000001",
                "JSONVer": "2.6",
                "Time": self._now(),
                "Uptime": int(time.monotonic() - self._started),
                "Version": str(self.get_state("firmware", "5.3.1")),
                "OemID": 700,
                "VendorID": 0,
                "NumOutputs": outputs,
                "NumInputs": inputs,
            },
        }
        if metering != "none":
            if metering == "q":
                total_load = sum(self._load(n) for n in range(1, outputs + 1))
            else:
                total_load = sum(self._load(n) for n in range(1, outputs + 1) if self._output_metered(n))
            total_ma = int(round(total_load / voltage * 1000)) if voltage else 0
            pf = 0.95 if total_load else SENTINEL
            measure = {
                "Voltage": round(voltage, 2),
                "TotalCurrent": total_ma,
                "OverallPowerFactor": pf,
                "TotalPowerFactor": pf,
                "OverallPhase": 0.0 if total_load else SENTINEL,
                "TotalPhase": 0.0 if total_load else SENTINEL,
            }
            # The 8QS and PowerDIN demo units leave Frequency out of this block.
            if metering == "k":
                measure["Frequency"] = 50.0
            energy = sum(float(self.get_state(f"out{n}_energy_wh", 0.0)) for n in range(1, outputs + 1)
                         if self._output_metered(n))
            energy_nr = sum(float(self.get_state(f"out{n}_energy_nr_wh", 0.0)) for n in range(1, outputs + 1)
                            if self._output_metered(n))
            measure.update({
                "TotalEnergy": int(energy),
                "TotalReverseEnergy": 0,
                "TotalEnergyNR": int(energy_nr),
                "TotalReverseEnergyNR": 0,
                "TotalLoad": int(round(total_load)),
                "EnergyStart": "1970-01-01T00:00:00+0000",
            })
            doc["GlobalMeasure"] = measure
        out_list = []
        for n in range(1, outputs + 1):
            entry = {
                "ID": n,
                "Name": str(self.get_state(f"out{n}_name", f"Power output {n}")),
                "State": 1 if self.get_state(f"out{n}_on") else 0,
                "Action": 6,
                "Delay": int(self.get_state(f"out{n}_delay_ms", 5000)),
            }
            if self._output_metered(n):
                load = self._load(n)
                if after_switch:
                    entry.update({
                        "Current": int(round(load / voltage * 1000)) if voltage else 0,
                        "Load": int(round(load)),
                        "PowerFactor": 0.95 if load else 0,
                        "Phase": 0,
                        "ReverseEnergy": 0,
                        "Energy": int(float(self.get_state(f"out{n}_energy_wh", 0.0))),
                    })
                    out_list.append(entry)
                    continue
                if metering == "k":
                    entry["Voltage"] = round(voltage, 2)
                entry.update({
                    "Current": int(round(load / voltage * 1000)) if voltage else 0,
                    "PowerFactor": 0.95 if load else 1.0,
                    "Phase": 0.0,
                    "Energy": int(float(self.get_state(f"out{n}_energy_wh", 0.0))),
                    "ReverseEnergy": 0,
                    "EnergyNR": int(float(self.get_state(f"out{n}_energy_nr_wh", 0.0))),
                    "ReverseEnergyNR": 0,
                    "Load": int(round(load)),
                })
                if metering == "k":
                    entry["Frequency"] = 50.0
                entry["EnergyStart"] = str(self.get_state(f"out{n}_energy_start", "1970-01-01T00:00:00+0000"))
            out_list.append(entry)
        doc["Outputs"] = out_list
        if inputs:
            in_list = []
            for n in range(1, inputs + 1):
                sensor = n == 1 and bool(self.get_state("sensor_connected"))
                temp_c = float(self.get_state("in1_temperature_c", 22.5)) if sensor else SENTINEL
                in_list.append({
                    "ID": n,
                    "Name": f"Input {n}",
                    "State": 1 if self.get_state(f"in{n}_closed") else 0,
                    "S0Counter": int(self.get_state(f"in{n}_s0", 0)),
                    "TemperatureC": round(temp_c, 1),
                    "TemperatureF": round(temp_c * 9 / 5 + 32, 1) if sensor else SENTINEL,
                    "Humidity": int(self.get_state("in1_humidity", 40.0)) if sensor else 999,
                })
            doc["Inputs"] = in_list
        failed = bool(self.get_state("watchdog_failed"))
        doc["PAB"] = [
            {"Type": "RANGE", "Name": "CR03_PAB", "Enabled": True, "In": self._load(1) > 0},
            {"Type": "ZONES", "Name": "Falcon", "Enabled": False, "Zone": 0},
        ]
        doc["Watchdogs"] = [
            {"Name": "google_pinger", "Enabled": True, "Fail": failed, "LastStatus": not failed,
             "Timestamp": int(time.time())},
            {"Name": "CR01_WDT", "Enabled": False, "Fail": False, "LastStatus": False, "Timestamp": 0},
        ]
        doc["Rules"] = [
            {"Name": "CR01_RULE", "Enabled": True, "Result": bool(self.get_state("rule_result"))},
            {"Name": "CR02_RULE", "Enabled": False, "Result": None},
            {"Name": "2 OFFafter10Sec", "Enabled": False, "Result": None},
        ]
        return doc

    # ── Web login ──

    async def _handle_http_request(self, request):
        """The web login's socket on the same port as the JSON API, as on a unit."""
        if request.path == WEB_PATH and request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._serve_web_login(request)
        if request.method == "POST" and request.path.startswith(UPLOAD_PATH) and not self.restarting():
            return await self._serve_upload_post(request)
        return await super()._handle_http_request(request)

    async def _serve_web_login(self, request):
        from aiohttp import WSMsgType, web

        if self.restarting():
            self.log_protocol("in", f"GET {WEB_PATH} (restarting)")
            return web.Response(status=503, text="Service Unavailable")
        if not self.get_state("web_login_api", True):
            self.log_protocol("in", f"GET {WEB_PATH} (no web login on this firmware)")
            return web.Response(status=404, text="Not Found")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        session = NetioWebSession(self)
        self.log_protocol("in", f"GET {WEB_PATH} (web login opened)")

        async def push():
            while not ws.closed:
                await asyncio.sleep(WEB_PUSH_S)
                if session.ended():
                    self.log_protocol("out", f"{WEB_PATH} closed (the unit restarts)")
                    await ws.close()
                    return
                if self.has_error_behavior("no_response"):
                    continue
                try:
                    for frame in session.events():
                        await ws.send_str(json.dumps(frame))
                except (ConnectionError, RuntimeError):
                    return

        pusher = asyncio.create_task(push())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT or self.has_error_behavior("no_response"):
                    continue
                if session.ended():
                    await ws.close()
                    break
                self.log_protocol("in", msg.data[:300])
                for frame in session.handle(msg.data):
                    text = json.dumps(frame)
                    self.log_protocol("out", text[:300])
                    await ws.send_str(text)
        finally:
            pusher.cancel()
        return ws

    # ── Online firmware update ──

    def restarting(self):
        """Whether the unit is restarting after a firmware download."""
        self.fw_tick()
        return time.monotonic() < self._down_until

    def _go_down(self, at):
        """The unit restarts at ``at``: every web login open then is closed,
        and it answers 503 until it is back."""
        self.generation += 1
        self._down_until = at + FW_RESTART_S
        self._started = self._down_until
        self._fw_list = [dict(FW_BLANK_ITEM)]
        self._fw_check = None
        self._fw_asked = False
        self._uploads.clear()

    def fw_tick(self):
        """Move a check, an update or a restart due on to where the clock says
        it is."""
        now = time.monotonic()
        if self._restart_due is not None and now >= self._restart_due:
            due, self._restart_due = self._restart_due, None
            self._go_down(due)
        check = self._fw_check
        if check is not None and now >= check[0] + FW_REBUILD_S:
            if check[1] is not None:
                self._fw_list = check[1]
            self._fw_check = None
        job = self._fw_job
        if job is None or now < job["restart_at"]:
            return
        # The download ended, and the unit restarts with whatever it got.
        self._go_down(job["restart_at"])
        if not self.get_state("update_download_fails", False):
            self.set_state("firmware", job["target"])
            self._fw_job = None
            return
        job["attempt"] += 1
        if job["attempt"] > FW_ATTEMPTS or not self.get_state("update_retries", True):
            self._fw_job = None
            return
        job["download_from"] = self._down_until + FW_RETRY_DELAY_S
        job["restart_at"] = job["download_from"] + FW_DOWNLOAD_S

    def _fw_locked(self):
        now = time.monotonic()
        if self._fw_check is not None and now < self._fw_check[0]:
            return True
        job = self._fw_job
        return job is not None and job["download_from"] <= now < job["restart_at"]

    def _fw_offer(self):
        return {
            "name": f"NETIO firmware {FW_NEWER}",
            "version": FW_NEWER,
            "downloadUrl": f"https://firmware.invalid/netio/{FW_NEWER}/full.package",
            "downloadUrlNoMcu": f"https://firmware.invalid/netio/{FW_NEWER}/app.package",
            "releaseNoteEn": "Simulated release.",
        }

    def _fw_set(self, data, ok, error):
        if not self.get_state("web_settings_allowed", True):
            return error("Unauthorized", "Client is not authorized to perform this operation.")
        data = data if isinstance(data, dict) else {}
        self.fw_tick()
        now = time.monotonic()
        if data.get("getFirmwareList"):
            server = str(self.get_state("update_server", UPDATE_SERVER[0]))
            # The first check after a PowerPDU 4KB started got no answer in two
            # of three tries (2026-10-21); the next check always did.
            first = not self._fw_asked
            self._fw_asked = True
            if server == UPDATE_SERVER[2] or (first and self.get_state("update_first_check_unanswered", True)):
                answer = None
            elif server == UPDATE_SERVER[0] and _version(self.get_state("firmware")) < _version(FW_NEWER):
                answer = [self._fw_offer()]
            else:
                answer = [{**FW_BLANK_ITEM, "name": "No new firmware available"}]
            self._fw_check = (now + FW_CHECK_S, answer)
            return ok()
        link = str(data.get("updateFw") or "")
        for item in self._fw_list:
            if link and link in (item.get("downloadUrl"), item.get("downloadUrlNoMcu")):
                self.set_state("last_update", "full" if link == item["downloadUrl"] else "keep outlets on")
                self._fw_job = {
                    "target": str(item["version"]).split()[0],
                    "attempt": 1,
                    "download_from": now,
                    "restart_at": now + FW_DOWNLOAD_S,
                }
                return ok()
        return error("MissingParameter", "Missing request parameter")

    # ── Files and restarts ──

    def _chunked(self):
        return _version(self.get_state("firmware", "5.3.1")) >= CHUNKED_SINCE

    def _settings_set(self, topic, data, ok, error):
        self.fw_tick()
        now = time.monotonic()
        if topic == "system/reset":
            if not (isinstance(data, dict) and data.get("reset") is True):
                return error("MissingParameter", "Missing request parameter")
            self._go_down(now)
            return ok()
        if topic == "network/servers/config":
            if isinstance(data, dict):
                self._servers.update({k: v for k, v in data.items() if k in self._servers})
            self._go_down(now)
            return ok()
        if topic in UPLOAD_TOPICS:
            self._upload_seq += 1
            token = f"sim{self._upload_seq:04d}{int(now * 1000) % 100000:05d}"
            self._uploads[token] = {"what": UPLOAD_TOPICS[topic], "next": 0, "data": bytearray()}
            if self._chunked():
                return ok({"uploadId": token})
            return ok({"uploadPath": UPLOAD_PATH + token})
        if topic == "upload/chunk":
            return self._chunk(data, ok, error)
        if topic == "system/cfgexport":
            if self._chunked():
                return error("TopicNotFonud", "Topic not found")
            return ok({"config": json.dumps(self._export(), indent=2)})
        section = topic.split("/", 2)[2]
        if not self._chunked() or section not in EXPORT_SECTIONS:
            return error("TopicNotFonud", "Topic not found")
        return ok(self._export()[section])

    def _chunk(self, data, ok, error):
        """One chunk of an upload, in order; the last one delivers the file."""
        data = data if isinstance(data, dict) else {}
        upload = self._uploads.get(str(data.get("uploadId") or ""))
        if not self._chunked() or upload is None:
            return error("MissingParameter", "Missing request parameter")
        index = data.get("chunkIndex")
        if index != upload["next"] or data.get("bytesFrom") != len(upload["data"]):
            return error("MissingParameter", "Missing request parameter")
        try:
            piece = base64.b64decode(str(data.get("b64Data") or ""), validate=True)
        except ValueError:
            return error("MissingParameter", "Missing request parameter")
        if data.get("bytesTo") != len(upload["data"]) + len(piece):
            return error("MissingParameter", "Missing request parameter")
        upload["data"].extend(piece)
        upload["next"] += 1
        if data.get("complete"):
            token = str(data.get("uploadId"))
            self._uploads.pop(token, None)
            if len(upload["data"]) != data.get("bytesTotal") or upload["next"] != data.get("chunksTotal"):
                return error("MissingParameter", "Missing request parameter")
            self._deliver(upload["what"], bytes(upload["data"]))
        return ok()

    def _deliver(self, what, payload):
        """A whole file the unit took."""
        self.set_state("last_file", f"{what} ({len(payload)} bytes)")
        if what == "key":
            self._https_key = payload
        elif what == "certificate":
            self.set_state("https_certificate", _fingerprint(payload))
        elif what == "firmware":
            self._restart_due = time.monotonic() + FW_FILE_INSTALL_S
        elif what == "configuration":
            self._import(payload)
            self._go_down(time.monotonic())

    def _export(self):
        """Export Config's sections, as the unit answers each one: a dict
        holding the section's own name."""
        outputs = self._model()[0]
        name = str(self.get_state("device_name", "PowerPDU-SIM"))
        sections = {section: {section: {}} for section in EXPORT_SECTIONS}
        sections["nbus"] = {}
        sections["system"] = {"system": {"settings": {"alias": name, "hostname": name}}}
        sections["outputs"] = {"outputs": {"outputs": [
            {"id": n, "name": str(self.get_state(f"out{n}_name", f"Power output {n}"))}
            for n in range(1, outputs + 1)
        ]}}
        sections["users"] = {"users": {"users": [{"name": "admin", "privileges": 4095}]}}
        return sections

    def _import(self, payload):
        """What a configuration file sets here: the device name and the output
        names, in either form the export writes."""
        try:
            doc = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(doc, dict):
            return
        system = (doc.get("system") or {}).get("system") or {}
        alias = ((system.get("settings") or {}) if isinstance(system, dict) else {}).get("alias")
        if isinstance(alias, str) and alias.strip():
            self.set_state("device_name", alias)
        outputs = (doc.get("outputs") or {}).get("outputs") or {}
        items = outputs.get("outputs") if isinstance(outputs, dict) else None
        for item in items if isinstance(items, list) else []:
            n = item.get("id") if isinstance(item, dict) else None
            if isinstance(n, int) and 1 <= n <= MAX_OUTPUTS and isinstance(item.get("name"), str):
                self.set_state(f"out{n}_name", item["name"])

    async def _serve_upload_post(self, request):
        """Before 5.2: the file POSTed as the multipart field ``file`` to the
        upload path the unit answered. Over HTTPS 5.1.3 takes neither firmware
        nor a configuration this way (NETIO's word); the driver does not try."""
        from aiohttp import web

        token = request.path[len(UPLOAD_PATH):]
        upload = self._uploads.pop(token, None)
        if upload is None or self._chunked():
            return web.json_response({"code": "NotFound", "message": "Upload not found"}, status=404)
        form = await request.post()
        field = form.get("file")
        payload = field.file.read() if hasattr(field, "file") else None
        if payload is None:
            return web.json_response({"code": "MissingParameter", "message": "No file"}, status=400)
        self.log_protocol("in", f"POST {request.path} ({len(payload)} bytes)")
        self._deliver(upload["what"], payload)
        return web.json_response({"status": "OK"})

    def _brand(self):
        model = str(self.get_state("model", "4KS"))
        return model, f"{FAMILY.get(model, 'PowerPDU')} {model}"

    def web_device(self):
        _model, brand = self._brand()
        return {
            "fwVersion": f"{self.get_state('firmware', '5.3.1')} - 1.57(1.53) - 0  4k",
            "devtype": brand,
            "deviceSN": "24A42C000001",
        }

    @staticmethod
    def _epoch(text):
        """An EnergyStart string as the web login's timestamp; 0 for never."""
        try:
            stamp = int(datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%S%z").timestamp())
        except ValueError:
            return 0
        return max(stamp, 0)

    def _web_output_measure(self, n, voltage):
        load = self._load(n)
        return {
            "voltage": round(voltage, 2),
            "power": round(load, 3),
            "current": round(load / voltage, 3) if voltage else 0.0,
            "energy": round(float(self.get_state(f"out{n}_energy_wh", 0.0)), 1),
            "reverseEnergy": 0.0,
            "energyNR": round(float(self.get_state(f"out{n}_energy_nr_wh", 0.0)), 1),
            "reverseEnergyNR": 0.0,
            "frequency": 50.0,
            "phase": 0.0,
            "powerFactor": 0.95 if load else 1.0,
            "timestamp": self._epoch(self.get_state(f"out{n}_energy_start")),
        }

    def web_topic(self, topic):
        """A topic's data as the web login answers it, or None for a topic
        this firmware does not have."""
        self._settle()
        self._meter()
        self.fw_tick()
        outputs, inputs, metering = self._model()
        fw = _version(self.get_state("firmware", "5.3.1"))
        voltage = float(self.get_state("voltage", 230.0))
        if topic == "system/lock":
            # The demo units' reason, read while unlocked.
            return {"op": "lock" if self._fw_locked() else "unlock",
                    "reason": "Communicating with cloud server", "timeoutMs": 30000}
        if topic == "fwupdate/list":
            return {"items": [dict(item) for item in self._fw_list]}
        if topic == "system/info":
            model, brand = self._brand()
            return {
                "fwVersion": self.web_device()["fwVersion"],
                "fwRevision": "00000000",
                "fwBuildDate": "2026-09-09",
                "versionNote": "",
                "brandModel": model,
                "brandName": brand,
                "deviceSN": "24A42C000001",
                "deviceName": str(self.get_state("device_name", "PowerPDU-SIM")),
                "inputCount": inputs,
                "outputCount": outputs,
                "measureMask": sum(1 << (n - 1) for n in range(1, outputs + 1) if self._output_metered(n)),
                "supportedProtocols": {"json": True, "xml": True, "url": True, "mqtt": True},
                "wifiSupport": False,
                "ethSupport": True,
                "installed": True,
            }
        if topic == "system/uptime":
            return {"uptime": int(time.monotonic() - self._started)}
        if topic == "network/servers/config":
            return dict(self._servers)
        if topic == "system/datetime":
            return {"ntpEnabled": True, "ntpServer": "pool.ntp.org", "timezone": "UTC",
                    "time": int(time.time())}
        if topic == "outputs/list":
            return {"items": [
                {
                    "id": n, "socketType": "S",
                    "name": str(self.get_state(f"out{n}_name", f"Power output {n}")),
                    "scheduleId": self._schedule_id(n),
                    "scheduleName": "Workdays" if n == 1 else "N/A",
                    "scheduleOn": False,
                }
                for n in range(1, outputs + 1)
            ]}
        if topic == "outputs/measure":
            items = []
            for n in range(1, outputs + 1):
                item = {"id": n}
                # From 5.4 the states are in outputs/state (PyNetioConf).
                if fw < (5, 4, 0):
                    item["state"] = "on" if self.get_state(f"out{n}_on") else "off"
                item["isScheduleActive"] = bool(self.get_state(f"out{n}_schedule"))
                if self._output_metered(n):
                    item["measure"] = self._web_output_measure(n, voltage)
                items.append(item)
            return {"items": items}
        if topic == "outputs/state":
            if fw < (5, 4, 0):
                return None
            return {"items": [
                {"id": n, "state": "on" if self.get_state(f"out{n}_on") else "off"}
                for n in range(1, outputs + 1)
            ]}
        if topic == "outputs/globalmeasure":
            if metering == "none":
                # An unmetered unit (the 4PS demo) answers with zeros.
                return {"measure": {
                    "voltage": 0.0, "power": 0.0, "current": 0.0, "energy": 0.0,
                    "reverseEnergy": 0.0, "energyNR": 0.0, "reverseEnergyNR": 0.0,
                    "frequency": 0.0, "phase": SENTINEL, "powerFactor": SENTINEL, "timestamp": 0,
                }}
            metered = [n for n in range(1, outputs + 1) if self._output_metered(n)]
            counted = range(1, outputs + 1) if metering == "q" else metered
            total = sum(self._load(n) for n in counted)
            return {"measure": {
                "voltage": round(voltage, 2),
                "power": round(total, 3),
                "current": round(total / voltage, 3) if voltage else 0.0,
                "energy": round(sum(float(self.get_state(f"out{n}_energy_wh", 0.0)) for n in metered), 1),
                "reverseEnergy": 0.0,
                "energyNR": round(sum(float(self.get_state(f"out{n}_energy_nr_wh", 0.0)) for n in metered), 1),
                "reverseEnergyNR": 0.0,
                "frequency": 50.0,
                "phase": 0.0 if total else SENTINEL,
                "powerFactor": 0.95 if total else SENTINEL,
                "timestamp": 0,
            }}
        if topic == "inputs/list":
            items = []
            for n in range(1, inputs + 1):
                sensor = n == 1 and bool(self.get_state("sensor_connected"))
                items.append({
                    "id": n,
                    "name": f"Input {n}",
                    "s0Counter": int(self.get_state(f"in{n}_s0", 0)),
                    "state": "on" if self.get_state(f"in{n}_closed") else "off",
                    "temperature": round(float(self.get_state("in1_temperature_c", 22.5)), 1)
                    if sensor else SENTINEL,
                    "temperatureUnit": "C",
                    "humidity": int(self.get_state("in1_humidity", 40.0)) if sensor else 999,
                })
            return {"items": items}
        if topic == "wdtpingers/list":
            return {"items": [{"name": "google_pinger", "enabled": True},
                              {"name": "CR01_WDT", "enabled": False}]}
        if topic == "rules/list":
            return {"items": [{"name": "CR01_RULE", "enabled": True},
                              {"name": "CR02_RULE", "enabled": False},
                              {"name": "2 OFFafter10Sec", "enabled": False}]}
        if topic == "pab/list":
            return {"items": [{"name": "CR03_PAB", "enabled": True},
                              {"name": "Falcon", "enabled": False}]}
        match = re.fullmatch(r"outputs/id/(\d+)/config", topic)
        if match and 1 <= int(match.group(1)) <= outputs:
            n = int(match.group(1))
            return {
                "name": str(self.get_state(f"out{n}_name", f"Power output {n}")),
                "default": "last",
                "resetDelay": int(self.get_state(f"out{n}_delay_ms", 5000)),
                "powerUpInterval": 1000,
                "isScheduleActive": bool(self.get_state(f"out{n}_schedule")),
                "scheduleId": self._schedule_id(n),
            }
        return None

    def web_set(self, topic, data, ok, error):
        if topic == "system/firmwarelist":
            return self._fw_set(data, ok, error)
        if topic in ("system/reset", "network/servers/config", "upload/chunk", "system/cfgexport") \
                or topic in UPLOAD_TOPICS or topic.startswith("export/config/"):
            if not self.get_state("web_settings_allowed", True):
                return error("Unauthorized", "Client is not authorized to perform this operation.")
            return self._settings_set(topic, data, ok, error)
        match = re.fullmatch(r"outputs/id/(\d+)/(ctrl|config|resetOutputConsumption)", topic)
        if not match:
            return error("TopicNotFonud", "Topic not found")
        n, what = int(match.group(1)), match.group(2)
        if not 1 <= n <= self._model()[0]:
            # As the 4KS demo answered a ctrl for its output 9.
            return error("MissingParameter", "Missing request parameter")
        data = data if isinstance(data, dict) else {}
        self._settle()
        if what == "ctrl":
            request = data.get("request")
            if request not in WEB_REQUESTS:
                return error("MissingParameter", "Missing request parameter")
            self._apply(n, WEB_REQUESTS[request], None, {})
            return ok()
        if not self.get_state("web_settings_allowed", True):
            # As the demo units answer a user without the right.
            return error("Unauthorized", "Client is not authorized to perform this operation.")
        if what == "resetOutputConsumption":
            self._apply(n, 5, None, {"ResetEnergyCounter": True})
            return ok()
        if str(data.get("name", "")).strip():
            self.set_state(f"out{n}_name", str(data["name"]))
        if "isScheduleActive" in data:
            self.set_state(f"out{n}_schedule", bool(data["isScheduleActive"]))
        delay = data.get("resetDelay")
        if isinstance(delay, int) and not isinstance(delay, bool) and delay > 0:
            self.set_state(f"out{n}_delay_ms", delay)
        return ok()
