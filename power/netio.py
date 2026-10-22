"""NETIO PowerPDU, PowerBOX, PowerCable and PowerDIN driver.

NETIO's networked power sockets, rack PDUs and DIN-rail units all run the same
"universal" firmware, and this driver reaches them two ways, chosen with the
Connect With setting:

  JSON API     NETIO's JSON M2M API: one status document, ``netio.json``, read
               with an HTTP GET and written with an HTTP POST of the same
               document carrying only the changes. A write is answered with the
               whole status document again. Turned on and given its own logins
               on the unit's M2M API page; every firmware has it.
  Web login    The WebSocket API at ``/emweb`` that the unit's own web
               interface uses, signed in with a web user. Firmware 5.1.3 and
               later. Nothing to turn on, and the unit pushes an output
               switching the moment it happens.

What the JSON document carries:

  Agent          model, name, MAC, serial number, firmware, JSON version,
                 clock, uptime, output and input counts
  GlobalMeasure  voltage, frequency, total current, load, power factor and
                 phase, energy counters (metered models only)
  Outputs        name, on/off, short on/off time, and on a metered output its
                 current, load, power factor, phase, voltage, frequency and
                 energy counters
  Inputs         open/closed, S0 pulse counter, temperature and humidity from
                 a NETIO sensor
  PAB, Watchdogs, Rules
                 the unit's own automation (power analysis blocks, IP
                 watchdogs, condition rules): name, enabled, latest result

Outputs are switched on, off and over (toggle), rebooted (short off), switched
on for a time (short on), renamed, have their energy counter reset and their
schedule enabled or disabled. That is every write the document defines.

The web login carries the same outputs, metering and inputs, plus each output's
schedule (whether it is on, and its name). Its watchdog, rule and power
analysis block lists carry each entry's name and whether it is enabled, but not
its result, so those results are reported over the JSON API only.

Over the web login the unit also checks NETIO's update server for newer
firmware and installs it, as "Check for update", "Update device firmware" and
"Update incl. low-level system" do in its web interface. One command checks;
two install the newest firmware offered, one with the outlet controller's own
firmware (every output switches off for a few seconds) and one without it. The
unit downloads the firmware itself and restarts, and tries again after a failed
download, three attempts in all; the driver follows it through the restarts and
reports how the update went.

Also over the web login, as the unit's own web interface does them: Restart
Device (the outputs stay as they are), and, on OpenAVC 0.37.2 and later, the
commands that take a file or hand one back: an HTTPS certificate and its key,
firmware from a file, and the configuration backed up and restored. A file goes
to the unit in chunks over the web login (API 2, firmware 5.2.0 and later) or
by one HTTP POST before that. The certificate the unit serves over HTTPS, its
fingerprint and expiry, is read from the web login's own handshake.

**Push and polling.** Over the JSON API the driver polls: NETIO's push
protocols do not report an output switching (Netio Push sends this same
document on a fixed period, 60 s by default, and at once only when current
moves past a set delta; MQTT-flex publishes to a broker the site has to run),
and both are set up only in the unit's own web interface. One GET returns
everything, so a poll every few seconds carries the same information on the
same schedule. Over the web login the driver subscribes: the unit pushes its
output metering (which carries each output's on/off) about three times a
second, and an output's change arrives within about 0.3 s of a command. On/off
and schedule changes are written at once; metering at most once per poll
interval. The poll re-reads the lists and settings the unit does not push.

**Why Python, not YAML.**
  * The JSON API takes one login for reading and another for writing (its
    settings page has both, and NETIO's public demo units read with no login
    and write only with one). A YAML HTTP driver has one login for every
    request.
  * The unit's own automation is a list of named entries the unit decides
    (watchdogs, rules, power analysis blocks). They are child entities keyed
    by name here; YAML cannot build a roster the device enumerates.
  * A write with its own short on/off time ends its HTTP connection, as the
    document asks (see ``_write``), and a refused write's error body is not
    valid JSON.
  * The web login is a WebSocket session with a hashed sign-in, request ids
    and pushed events on the one socket.

Sources: NETIO "JSON over HTTP NETIO M2M API protocols", JSON version 2.6,
published 2026-02-24, for the JSON API. For the web login NETIO publishes no
protocol document; NETIO's own MIT-licensed library, PyNetioConf (0.3.1,
2026-10-15), is the reference NETIO pointed to, and its author confirmed the
API is the one every 5.1.3+ unit's web interface runs on. Both were checked
against NETIO's public demo units (PowerPDU 8QS, 4PS and 4KS, PowerDIN 4PZ,
PowerCable 2PZ, PowerBOX 4KF, on firmware 5.3.1 and 5.3.2): the JSON API on
2026-10-13, the web login on 2026-10-16. Then on a PowerPDU 4KB that NETIO sent
(2026-10-21): both, on firmware 5.2.4 and 5.3.2, over HTTP and HTTPS, and the
online firmware update between them. The files and Restart Device follow the
code of the web interface that unit serves (5.3.2), which differs from the
demo units' older one. Where the units and a source disagree the code follows
the units, and says so where it reads the value.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import secrets
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import websockets

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # OpenAVC before 0.37.1 has no typed command refusal
    CommandRefusedError = RuntimeError

try:
    from openavc.drivers.command_files import CommandFile, DeviceFile
except ImportError:  # OpenAVC before 0.37.2 gives no command a file (see FILE_COMMANDS)
    CommandFile = DeviceFile = None

try:
    from openavc.discovery.certificates import certificate_details
except ImportError:  # the platform's one certificate reader, from 0.36.0
    certificate_details = None

log = get_logger(__name__)

STATUS_PATH = "/netio.json"
# How long a connection over HTTPS may take, its TLS handshake included: the
# JSON API's (see _create_transport) and the web login's over wss (_open_web).
HTTPS_CONNECT_S = 15.0

CONNECT_JSON = "json"
CONNECT_WEB = "web"

# ── Web login (WebSocket API) ──
WEB_PATH = "/emweb"
# PyNetioConf pauses 35 ms after every message it sends, "to give breathing
# room to the internal device timers and reduce unwanted restarts".
WEB_SEND_GAP_S = 0.035
WEB_OPEN_TIMEOUT_S = 8.0
WEB_REPLY_TIMEOUT_S = 10.0
# How long a command waits for the unit to push the output state it caused.
# The demo units pushed it 0.29 to 0.49 s after the command (2026-10-16).
WEB_CONFIRM_S = 2.0
# The web login API arrived in 5.1.3; PING in 5.2 (PyNetioConf: "not supported
# on 5.0.x to 5.1.x"); 5.4 reads with GET instead of SUBSCRIBE and reports
# output states in outputs/state rather than in outputs/measure.
WEB_API_SINCE = (5, 1, 3)
WEB_PING_SINCE = (5, 2, 0)
WEB_GET_SINCE = (5, 4, 0)
# Topics held subscribed for their pushed changes. outputs/measure and
# outputs/globalmeasure push about three times a second whatever changes; the
# two lists are expected to push on a change (unverified: nothing changed them
# on the demo units) and are re-read every poll as well.
WEB_HELD_TOPICS = ("outputs/measure", "outputs/globalmeasure", "outputs/list", "inputs/list")
WEB_STATE_TOPIC = "outputs/state"
# A web user's sign-in refused for its credentials (PyNetioConf names both).
WEB_AUTH_REFUSALS = ("InvalidCredentials", "TooManyFailLogin", "Unauthorized")
# The fields of an output's settings, all sent back on every write of them as
# PyNetioConf does.
WEB_OUTPUT_CONFIG_KEYS = (
    "default", "isScheduleActive", "name", "powerUpInterval", "resetDelay", "scheduleId",
)

# ── Online firmware update (web login) ──
# The timings are PyNetioConf's (get_firmware_updates_list and
# update_firmware_online). A check: SET system/firmwarelist with
# getFirmwareList, then the unit asks NETIO's update server (a few seconds),
# holding system/lock locked meanwhile, and rebuilds fwupdate/list after it
# unlocks (0.84 s on a PowerPDU 4KB; PyNetioConf says about 0.4 s).
FW_LIST_TIMEOUT_S = 60.0
FW_LIST_INTERVAL_S = 0.2
# A list that did not change counts as the answer once it stayed the same this
# long after the unlock.
FW_LIST_SETTLE_S = 2.0
# An update right after a check reuses its answer rather than asking again.
FW_LIST_REUSE_S = 120.0
# A check that ends with no answer from the update server is asked this many
# times in all (see _fw_check).
FW_CHECK_ATTEMPTS = 2
FW_LIST_FIELDS = ("name", "version", "downloadUrl", "downloadUrlNoMcu", "releaseNoteEn")
# An update: SET system/firmwarelist with updateFw set to an offer's link. The
# unit locks system/lock, downloads, and restarts when the download ends, a
# failed one included; it tries three times in all, but PyNetioConf saw a 5.4.0
# unit with NETIO Cloud off not try again, so a unit that does not start the
# next attempt within FW_RETRY_WAIT_S of coming back has failed.
FW_START_TIMEOUT_S = 10.0
FW_UPDATE_TIMEOUT_S = 900.0
FW_ATTEMPTS = 3
FW_RETRY_WAIT_S = 60.0
FW_RETRY_INTERVAL_S = 1.0
# A restart shows as a boot time (now minus the uptime) that moved; a busy unit
# reports its uptime a few seconds late, so it has to move by more than this.
FW_RESTART_SLACK_S = 10.0

# ── Files to and from the unit (web login) ──
# As the unit's own web interface does it (served by a PowerPDU 4KB on 5.3.2,
# 2026-10-21). SET on an upload topic opens the upload; the API version in the
# HELO answer decides how the file follows: from 2, in chunks over the web
# login (`upload/chunk`, each answered before the next, tried three times, no
# pause between them); before 2, one HTTP POST of the file as the multipart
# field `file` to the `uploadPath` the unit answered, which needs no login of
# its own.
WEB_CHUNKED_API = 2
UPLOAD_CHUNK_BYTES = 2048
UPLOAD_CHUNK_TIMEOUT_S = 30.0
UPLOAD_CHUNK_TRIES = 3
UPLOAD_POST_TIMEOUT_S = 300.0
UPLOAD_KEY_TOPIC = "system/srvkeyupload"
UPLOAD_CERT_TOPIC = "system/srvcertupload"
UPLOAD_FIRMWARE_TOPIC = "system/fwupdate"
UPLOAD_CONFIG_TOPIC = "system/cfgimport"
# The web interface's own limits (its maxSizeBytes).
FIRMWARE_MAX_BYTES = 10 * 1024 * 1024
CONFIG_MAX_BYTES = 10 * 1024 * 1024
CERT_MAX_BYTES = 8192
# Export Config from API 2: one SET {} on each section's topic, written as
# {section: reply data} with a two-space indent. Before API 2 it is one SET on
# system/cfgexport, whose reply carries the file's text as `config`.
EXPORT_SECTIONS = (
    "inputs", "network", "outputs", "PAB", "protocols", "rules", "schedules",
    "system", "users", "wdtpingers", "mqtt", "nbus",
)
EXPORT_LEGACY_TOPIC = "system/cfgexport"
EXPORT_TIMEOUT_S = 30.0
# Restart: system/reset from 5.2.0 (PyNetioConf). On 5.1.x the unit restarts
# when its web server settings are written, the current ones included (Jakub
# Sperl, NETIO, 2026-10-21).
WEB_RESET_SINCE = (5, 2, 0)
WEB_SERVERS_TOPIC = "network/servers/config"

# The commands that take a file or hand one back. OpenAVC gives a command a
# file from 0.37.2; on an older release the driver has none of these, and
# every other command works as before (they are added in __init__).
FILE_COMMANDS: dict[str, dict[str, Any]] = {
    "upload_https_certificate": {
        "label": "Upload HTTPS Certificate",
        "params": {
            "private_key": {
                "type": "file", "label": "Private Key", "required": True,
                "max_bytes": CERT_MAX_BYTES,
                "help": "The certificate's private key, PEM encoded.",
            },
            "certificate": {
                "type": "file", "label": "Certificate", "required": True,
                "max_bytes": CERT_MAX_BYTES,
                "help": "The certificate the NETIO serves over HTTPS, PEM encoded.",
            },
        },
        "confirm": "Replaces the certificate the NETIO serves over HTTPS, then restarts it. Its "
                   "outputs stay as they are.",
        # A PowerPDU 4KB on 5.3.2 over HTTPS (2026-10-21) took TLS again 15 s
        # after the upload, then left reads unanswered for a while: the web
        # login was back 82 s after it. Ended early once the unit is back.
        "restarts_device_for": 120,
        "help": "Installs a certificate and its private key for the NETIO's HTTPS server, as its "
                "web interface does under HTTP/HTTPS, then restarts it so it serves the new one. "
                "The key must belong to the certificate. Web login only, with a user that may "
                "change the NETIO's settings.",
    },
    # restarts_device_for opens when the file is in, so it covers the install
    # and both restarts of a package with the outlet controller's firmware.
    "update_firmware_from_file": {
        "label": "Update Firmware From a File",
        "params": {
            "firmware": {
                "type": "file", "label": "Firmware Package", "required": True,
                "max_bytes": FIRMWARE_MAX_BYTES,
                "help": "A NETIO firmware package, as NETIO's firmware download page offers it.",
            },
        },
        "confirm": "Installs the firmware in this file. The NETIO restarts once it is in, and if "
                   "the package includes the outlet controller's firmware every output switches "
                   "off for a few seconds.",
        "restarts_device_for": 600,
        "help": "Installs a firmware package from a file, as Install Firmware does in the NETIO's "
                "web interface, for a NETIO that cannot reach NETIO's update server. Web login "
                "only, with a user that may change the NETIO's settings.",
    },
    "back_up_configuration": {
        "label": "Back Up Configuration",
        "params": {},
        "returns_file": True,
        "help": "Downloads the NETIO's settings as a file, the one Export Config saves in its web "
                "interface, so Restore Configuration or the web interface can put them back. The "
                "file holds the web users' password hashes: keep it somewhere safe. Web login "
                "only, with a user that may change the NETIO's settings.",
    },
    "restore_configuration": {
        "label": "Restore Configuration",
        "params": {
            "configuration": {
                "type": "file", "label": "Configuration File", "required": True,
                "accept": [".json"], "max_bytes": CONFIG_MAX_BYTES,
                "help": "A file from Back Up Configuration, or from Export Config in the NETIO's "
                        "web interface.",
            },
        },
        "confirm": "Replaces every setting on this NETIO with the ones in the file, its network "
                   "address and web users included, then restarts it. If the file gives it "
                   "another address or another password for this login, change them on this "
                   "device afterwards.",
        # A PowerPDU 4KB on 5.3.2 (2026-10-21) restarted 2 s after the file
        # was in and was back 18 s after it.
        "restarts_device_for": 60,
        "help": "Puts back the settings in a configuration file, as Import Config does in the "
                "NETIO's web interface, then the NETIO restarts. Web login only, with a user that "
                "may change the NETIO's settings.",
    },
}

# "Not available": a sensor that is not connected, a figure the unit does not
# measure (a power factor with no load on a 4KS reads 999.00).
SENTINEL = 999.0
# Humidity with no sensor reads 999 on the demo units; the document's own
# listings show 9990. Both mean the same thing.
HUMIDITY_ABSENT = (999.0, 9990.0)

# Output actions (document, "Output actions"). 6 is what a read returns
# ("ignored"), never sent.
ACTION_OFF = 0
ACTION_ON = 1
ACTION_SHORT_OFF = 2
ACTION_SHORT_ON = 3
ACTION_TOGGLE = 4
ACTION_NO_CHANGE = 5
ACTION_SCHEDULE_OFF = 20
ACTION_SCHEDULE_ON = 21

# The same actions as the web login's outputs/id/<n>/ctrl requests take them.
# A short off / short on there runs for the output's own short on/off time;
# the request has no time of its own.
WEB_REQUESTS = {
    ACTION_OFF: "off",
    ACTION_ON: "on",
    ACTION_SHORT_OFF: "soff",
    ACTION_SHORT_ON: "son",
    ACTION_TOGGLE: "toggle",
}

# The largest NETIO unit has eight outputs and two inputs; these leave room for
# a model that does not exist yet without accepting nonsense.
MAX_OUTPUTS = 24
MAX_INPUTS = 8

# A string child id takes letters, digits, '_' and '-' only.
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


class NetioHTTPError(Exception):
    """The unit answered with an HTTP error status."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        self.message = message
        super().__init__(f"HTTP {status}" + (f" ({message})" if message else ""))


class NetioReplyError(Exception):
    """The unit answered 200 with something that is not its status document."""


class NetioCommandError(CommandRefusedError):
    """A command the unit refused, or one that cannot be sent to this unit.

    Every message is a sentence written for the person who sent the command,
    with no login in it: from OpenAVC 0.37.1 Send Command and a Quick Action
    show it. Before 0.37.1 this is a plain RuntimeError, which they report as a
    command that could not run, with the sentence in the server log.
    """


class NetioFirmwareNoAnswer(NetioCommandError):
    """A firmware check the unit finished with no answer from NETIO's server."""


class NetioWebError(Exception):
    """The web login answered a request with ``"status": "ERROR"``."""

    def __init__(self, code: str, message: str = "", topic: str = "") -> None:
        self.code = code
        self.message = message
        self.topic = topic
        super().__init__(f"{code}" + (f" ({message})" if message else ""))


# ── Value readers ────────────────────────────────────────────────────────────


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _measure(value: Any) -> float | None:
    """A measured figure, or None where the unit reports 999 (not available)."""
    number = _as_float(value)
    if number is None or number == SENTINEL:
        return None
    return number


def _milliamps(value: Any) -> float | None:
    """The unit reports current in mA; the state is amps."""
    number = _as_float(value)
    if number is None:
        return None
    return round(number / 1000.0, 3)


def _bit(value: Any) -> bool | None:
    """An output or input State: 1 or 0."""
    number = _as_int(value)
    if number is None:
        return None
    return number == 1


def _timestamp(value: Any) -> str | None:
    """A watchdog's Timestamp as the unit's own clock reads it; 0 means never
    checked.

    The document calls it a "Linux timestamp", but on the demo units it counts
    the unit's local wall-clock time as if it were UTC: a check made at 16:06
    UTC on a unit whose clock read 18:06 carried 18:06. So it is shown as a
    clock reading with no zone, comparable with the unit's own Device Clock.
    """
    seconds = _as_int(value)
    if not seconds or seconds < 0:
        return None
    try:
        moment = datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _energy_start(value: Any) -> str | None:
    """An EnergyStart as the unit sends it, or None for a counter never reset.

    A counter never reset reports the start of 1970 in the unit's own zone
    (``1970-01-01T00:00:00+0100`` on a PowerPDU 4KB set to Prague; a unit west
    of Greenwich reads 1969). The web login reports the same counter as 0.
    """
    text = _as_str(value)
    if text is None:
        return None
    match = re.match(r"(\d{4})-", text)
    if match is not None and int(match.group(1)) <= 1970:
        return None
    return text


def child_key(name: str, taken: set[str]) -> str:
    """The child id for a named entry: its name with anything a child id cannot
    hold turned into '_', made unique against ``taken``."""
    base = _ID_UNSAFE.sub("_", str(name).strip()).strip("_")[:60] or "entry"
    key = base
    n = 2
    while key in taken:
        key = f"{base}-{n}"
        n += 1
    taken.add(key)
    return key


# ── Reply parsing ────────────────────────────────────────────────────────────


def parse_error(text: str) -> str:
    """The message in an error answer, or "".

    The document shows ``{"errors": [{"code", "severity", "message"}]}``. The
    demo units answer a refused write with the same list but no enclosing
    braces (``"errors":[{"code":400,"message":"Bad request"}]``), which is not
    valid JSON on its own, and a PowerPDU 4KB (5.2.4 and 5.3.2) leaves the
    list's closing bracket off too, its Content-Length counting exactly that
    (``"errors":[{"code":400,"message":"Bad request"}``). All three are read.
    """
    text = (text or "").strip()
    if not text:
        return ""
    doc: Any = None
    for candidate in (text, "{" + text + "}"):
        try:
            doc = json.loads(candidate)
            break
        except ValueError:
            continue
    if isinstance(doc, dict) and isinstance(doc.get("errors"), list):
        messages = [
            str(e.get("message")).strip()
            for e in doc["errors"]
            if isinstance(e, dict) and e.get("message")
        ]
        return "; ".join(m for m in messages if m)
    found = [m.strip() for m in re.findall(r'"message"\s*:\s*"([^"]*)"', text) if m.strip()]
    if found:
        return "; ".join(found)
    # A plain-text refusal ("401 Unauthorized") is short; keep it.
    if len(text) <= 80 and "<" not in text:
        return text
    return ""


def parse_status(text: str) -> dict[str, Any]:
    """The status document as a dict. Raises NetioReplyError when the reply is
    not JSON or carries no Agent block (something other than a NETIO answered,
    or the request reached the wrong service on the port)."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise NetioReplyError("the reply was not JSON") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("Agent"), dict):
        raise NetioReplyError("the reply has no Agent block")
    return doc


def parse_agent(agent: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """Device identity. NumInputs is absent on the oldest firmware (a PowerBOX
    3Px on JSON 2.3), where the Inputs list decides."""
    inputs = doc.get("Inputs")
    outputs = doc.get("Outputs")
    num_outputs = _as_int(agent.get("NumOutputs"))
    num_inputs = _as_int(agent.get("NumInputs"))
    return {
        "model": _as_str(agent.get("Model")),
        "device_name": _as_str(agent.get("DeviceName")),
        "mac_address": _as_str(agent.get("MAC")),
        # The document: "When both tags available, use the SerialNumber, it's
        # identical with printed label on the device." Old firmware has MAC only.
        "serial_number": _as_str(agent.get("SerialNumber") or agent.get("MAC")),
        "firmware": _as_str(agent.get("Version")),
        "json_version": _as_str(agent.get("JSONVer")),
        "device_time": _as_str(agent.get("Time")),
        "uptime_s": _as_int(agent.get("Uptime")),
        "output_count": num_outputs if num_outputs is not None
        else (len(outputs) if isinstance(outputs, list) else 0),
        "input_count": num_inputs if num_inputs is not None
        else (len(inputs) if isinstance(inputs, list) else 0),
    }


def parse_global(measure: Any) -> dict[str, Any]:
    """Whole-unit metering. Absent on an unmetered model (4PS, 2PZ, 3Px).

    Frequency is absent from this block on the 8QS and the PowerDIN 4PZ
    (firmware 5.3.1), present on the 4KS and 4KF; the document's 8QS listing
    has it. The Total* power factor and phase replace the Overall* ones, which
    the document keeps "for historical compatibility only"; the oldest firmware
    has only OverallPowerFactor and Phase, read as a fallback.
    """
    if not isinstance(measure, dict):
        return {
            "metered": False,
            "voltage_v": None, "frequency_hz": None, "total_current_a": None,
            "total_load_w": None, "total_power_factor": None,
            "total_phase_deg": None, "total_energy_wh": None,
            "total_energy_nr_wh": None, "total_reverse_energy_wh": None,
            "total_reverse_energy_nr_wh": None, "energy_start": None,
        }
    power_factor = measure.get("TotalPowerFactor", measure.get("OverallPowerFactor"))
    phase = measure.get("TotalPhase", measure.get("Phase"))
    return {
        "metered": True,
        "voltage_v": _measure(measure.get("Voltage")),
        "frequency_hz": _measure(measure.get("Frequency")),
        "total_current_a": _milliamps(measure.get("TotalCurrent")),
        "total_load_w": _measure(measure.get("TotalLoad")),
        "total_power_factor": _measure(power_factor),
        "total_phase_deg": _measure(phase),
        "total_energy_wh": _as_int(measure.get("TotalEnergy")),
        "total_energy_nr_wh": _as_int(measure.get("TotalEnergyNR")),
        "total_reverse_energy_wh": _as_int(measure.get("TotalReverseEnergy")),
        "total_reverse_energy_nr_wh": _as_int(measure.get("TotalReverseEnergyNR")),
        "energy_start": _energy_start(measure.get("EnergyStart")),
    }


def parse_output(entry: dict[str, Any]) -> dict[str, Any]:
    """One output. Metering keys are present only on a metered output: every
    output of a K model, output 1 of an 8QS, outputs 1 and 2 of a PowerDIN 4PZ
    (its 3 and 4 are relay contacts). The K models also report the output's
    own Voltage and Frequency, which the document's table leaves out."""
    metered = "Current" in entry or "Load" in entry
    values: dict[str, Any] = {
        "name": _as_str(entry.get("Name")),
        "state": _bit(entry.get("State")),
        "delay_ms": _as_int(entry.get("Delay")),
        "metered": metered,
        "voltage_v": None, "frequency_hz": None, "current_a": None,
        "load_w": None, "power_factor": None, "phase_deg": None,
        "energy_wh": None, "energy_nr_wh": None, "reverse_energy_wh": None,
        "reverse_energy_nr_wh": None, "energy_start": None,
    }
    if metered:
        values.update({
            "voltage_v": _measure(entry.get("Voltage")),
            "frequency_hz": _measure(entry.get("Frequency")),
            "current_a": _milliamps(entry.get("Current")),
            "load_w": _measure(entry.get("Load")),
            "power_factor": _measure(entry.get("PowerFactor")),
            "phase_deg": _measure(entry.get("Phase")),
            "energy_wh": _as_int(entry.get("Energy")),
            "energy_nr_wh": _as_int(entry.get("EnergyNR")),
            "reverse_energy_wh": _as_int(entry.get("ReverseEnergy")),
            "reverse_energy_nr_wh": _as_int(entry.get("ReverseEnergyNR")),
            "energy_start": _energy_start(entry.get("EnergyStart")),
        })
    return values


def parse_input(entry: dict[str, Any]) -> dict[str, Any]:
    """One digital input. State 1 is a closed contact and 0 an open one (both
    places the document defines it agree on that, and disagree on which one it
    calls ON). One of the document's listings spells TempertureC; the units send
    TemperatureC, and either is read."""
    humidity = _as_float(entry.get("Humidity"))
    if humidity in HUMIDITY_ABSENT:
        humidity = None
    return {
        "name": _as_str(entry.get("Name")),
        "closed": _bit(entry.get("State")),
        "s0_counter": _as_int(entry.get("S0Counter")),
        "temperature_c": _measure(entry.get("TemperatureC", entry.get("TempertureC"))),
        "temperature_f": _measure(entry.get("TemperatureF")),
        "humidity": humidity,
    }


def parse_watchdog(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _as_str(entry.get("Name")),
        "enabled": _as_bool(entry.get("Enabled")),
        "failed": _as_bool(entry.get("Fail")),
        "last_ok": _as_bool(entry.get("LastStatus")),
        "last_check": _timestamp(entry.get("Timestamp")),
    }


def parse_rule(entry: dict[str, Any]) -> dict[str, Any]:
    """Result is null until the rule has been evaluated (a disabled rule)."""
    return {
        "name": _as_str(entry.get("Name")),
        "enabled": _as_bool(entry.get("Enabled")),
        "result": _as_bool(entry.get("Result")),
    }


def parse_pab(entry: dict[str, Any]) -> dict[str, Any]:
    """A power analysis block. The document defines Type RANGE with an In
    result; the 4KS demo also carries Type ZONES, which reports a Zone number
    instead."""
    return {
        "name": _as_str(entry.get("Name")),
        "pab_type": _as_str(entry.get("Type")),
        "enabled": _as_bool(entry.get("Enabled")),
        "in_range": _as_bool(entry.get("In")) if "In" in entry else None,
        "zone": _as_int(entry.get("Zone")) if "Zone" in entry else None,
    }


def numbered(entries: Any, limit: int, parse) -> dict[int, dict[str, Any]]:
    """Outputs or inputs by their ID, in order. An entry with no usable ID, or
    one past ``limit``, is left out."""
    found: dict[int, dict[str, Any]] = {}
    if not isinstance(entries, list):
        return found
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        local_id = _as_int(entry.get("ID"))
        if local_id is None or not 1 <= local_id <= limit:
            continue
        found[local_id] = parse(entry)
    return dict(sorted(found.items()))


def named(entries: Any, parse) -> dict[str, dict[str, Any]]:
    """Watchdogs, rules or PABs keyed by a child id made from each name."""
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(entries, list):
        return found
    taken: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("Name"):
            continue
        found[child_key(str(entry["Name"]), taken)] = parse(entry)
    return found


def summarize(doc: dict[str, Any]) -> dict[str, Any]:
    """Everything the document reports, split by where it goes."""
    device = parse_agent(doc["Agent"], doc)
    device.update(parse_global(doc.get("GlobalMeasure")))
    return {
        "device": device,
        "output": numbered(doc.get("Outputs"), MAX_OUTPUTS, parse_output),
        "input": numbered(doc.get("Inputs"), MAX_INPUTS, parse_input),
        "watchdog": named(doc.get("Watchdogs"), parse_watchdog),
        "rule": named(doc.get("Rules"), parse_rule),
        "pab": named(doc.get("PAB"), parse_pab),
    }


# ── Web login (WebSocket API) ────────────────────────────────────────────────


def web_login_token(
    username: str, password: str, public_key: str, local_timestamp: int, salt: str | None = None,
) -> str:
    """The AUTH token a web user signs in with (PyNetioConf ``ws_api``).

    The password never travels: it is hashed with the user name and the key
    the unit sent in its HELO answer, salted, and tied to the unit's clock in
    ten-second steps, so a token is good for that window only.
    """
    if salt is None:
        salt = hashlib.sha256(str(secrets.randbits(32)).encode()).hexdigest()
    password_hash = base64.b64encode(
        hashlib.sha256(f"{username}{public_key}{password}".encode()).digest()
    ).decode()
    password_token = hashlib.sha256(f"{salt}{password_hash}".encode()).hexdigest()
    time_mark = str(math.floor(local_timestamp / 10))
    return f"{salt}.{hashlib.sha256(f'{time_mark}{password_token}'.encode()).hexdigest()}"


def firmware_version(text: Any) -> tuple[int, int, int] | None:
    """(major, minor, patch) from a version string such as
    ``5.3.2 - 1.57(1.53) - 0  4k``, or None."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(text or ""))
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def web_api_major(text: Any, firmware: tuple[int, int, int] | None) -> int | None:
    """The major of the web login's serverApiVersion (the HELO answer).

    A greeting without one is from before the chunked upload (the unit's own
    web interface then needs a default), and 5.2.0 is where NETIO's library
    starts sending files in chunks, so the firmware decides it then.
    """
    match = re.match(r"\s*(\d+)\.", str(text or ""))
    if match:
        return int(match.group(1))
    if firmware is None:
        return None
    return WEB_CHUNKED_API if firmware >= WEB_RESET_SINCE else WEB_CHUNKED_API - 1


def certificate_fingerprint(sha256_hex: str) -> str:
    """A SHA-256 fingerprint the way a browser shows one: AB:CD:..."""
    text = sha256_hex.strip().upper()
    return ":".join(text[i:i + 2] for i in range(0, len(text), 2))


def backup_name(device_name: Any, day: datetime) -> str:
    """The backup's file name: the unit's name and the day."""
    name = _ID_UNSAFE.sub("-", str(device_name or "").strip()).strip("-") or "netio"
    return f"{name}-config-{day:%Y-%m-%d}.json"


def configuration_problem(name: str, data: bytes, sectioned: bool) -> str | None:
    """Why ``data`` cannot be a NETIO configuration file, or None.

    ``sectioned``: the unit exports in sections (web login API 2 and later),
    so a file of its has at least one of them at the top.
    """
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        return f"{name} is not a NETIO configuration file. Back Up Configuration makes one."
    if sectioned and not any(section in doc for section in EXPORT_SECTIONS):
        return (
            f"{name} is not a NETIO configuration file: it has none of the sections Back Up "
            f"Configuration writes."
        )
    return None


def certificate_pair_problem(key: bytes, cert: bytes, key_name: str, cert_name: str) -> str | None:
    """Why this private key and certificate cannot be served together, or None.

    Checked before either goes to the unit: with HTTPS on, the unit's HTTP
    server is off, so a certificate it cannot serve would leave it answering
    nothing at all. ``cryptography`` ships with every OpenAVC that has the
    upload command.
    """
    try:
        from cryptography import x509
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return None
    try:
        certificate = x509.load_pem_x509_certificate(cert)
    except ValueError:
        return f"{cert_name} is not a PEM certificate."
    try:
        private_key = serialization.load_pem_private_key(key, password=None)
    except TypeError:
        return f"{key_name} is protected by a passphrase; the NETIO takes a key without one."
    except (ValueError, UnsupportedAlgorithm):
        return f"{key_name} is not a PEM private key."
    spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if private_key.public_key().public_bytes(*spki) != certificate.public_key().public_bytes(*spki):
        return f"{key_name} is not the private key of {cert_name}."
    return None


async def _pieces(data: bytes) -> Any:
    """``data`` in upload chunks, for a file already read whole."""
    for start in range(0, len(data), UPLOAD_CHUNK_BYTES):
        yield data[start:start + UPLOAD_CHUNK_BYTES]


def _firmware_text(text: Any) -> str | None:
    """The firmware as the JSON API's Agent reports it: ``5.3.2``, the part
    before the low-level (MCU) version."""
    if not text:
        return None
    return str(text).split("-")[0].strip() or None


def web_identity(info: dict[str, Any] | None, hello_device: dict[str, Any]) -> dict[str, Any]:
    """Device identity from system/info, else what the HELO answer carries
    (the type, firmware and serial number are there before signing in).

    The web login reports no MAC address and no JSON version; both stay unset.
    """
    info = info or {}
    devtype = str(hello_device.get("devtype") or "")
    model = info.get("brandModel") or (devtype.split()[-1] if devtype else None)
    values: dict[str, Any] = {
        "model": _as_str(model),
        "device_name": _as_str(info.get("deviceName")),
        "serial_number": _as_str(info.get("deviceSN") or hello_device.get("deviceSN")),
        "firmware": _firmware_text(info.get("fwVersion") or hello_device.get("fwVersion")),
    }
    for key, source in (("output_count", "outputCount"), ("input_count", "inputCount")):
        count = _as_int(info.get(source))
        if count is not None:
            values[key] = count
    return values


def web_measure_mask(info: dict[str, Any] | None) -> int | None:
    """Which outputs the unit meters, one bit per output (4KS 15, 8QS 1,
    4PS 0), or None when system/info was not read."""
    if not isinstance(info, dict):
        return None
    return _as_int(info.get("measureMask"))


def web_global(measure: Any, metered: bool) -> dict[str, Any]:
    """Whole-unit metering from outputs/globalmeasure, in the units the JSON
    API's state uses. An unmetered unit (measureMask 0, the 4PS) still answers
    with zeros, so ``metered`` decides, not the block's presence. Current is
    in amps here (the JSON API counts mA), power in watts with decimals,
    energy in watt-hours with decimals (read beside netio.json on the 4KF and
    4KS demos, 2026-10-16)."""
    blank = parse_global(None)
    if not metered or not isinstance(measure, dict):
        return blank
    return {
        "metered": True,
        "voltage_v": _measure(measure.get("voltage")),
        "frequency_hz": _measure(measure.get("frequency")),
        "total_current_a": _amps(measure.get("current")),
        "total_load_w": _measure(measure.get("power")),
        "total_power_factor": _measure(measure.get("powerFactor")),
        "total_phase_deg": _measure(measure.get("phase")),
        "total_energy_wh": _as_int(measure.get("energy")),
        "total_energy_nr_wh": _as_int(measure.get("energyNR")),
        "total_reverse_energy_wh": _as_int(measure.get("reverseEnergy")),
        "total_reverse_energy_nr_wh": _as_int(measure.get("reverseEnergyNR")),
        "energy_start": _timestamp(measure.get("timestamp")),
    }


def _amps(value: Any) -> float | None:
    number = _as_float(value)
    if number is None or number == SENTINEL:
        return None
    return round(number, 3)


def web_output_metering(item: dict[str, Any]) -> dict[str, Any]:
    """One output's metering from an outputs/measure item. An unmetered
    output's item has no ``measure`` block at all. ``timestamp`` is when its
    energy counter was last reset: the unit's local clock counted as if it
    were UTC (1791915791 on the 4KS demo is 18:23:11, the reset made at 18:23
    on its clock), so it is shown as a clock reading with no zone; 0 means
    never reset."""
    measure = item.get("measure")
    if not isinstance(measure, dict):
        return {
            "metered": False,
            "voltage_v": None, "frequency_hz": None, "current_a": None,
            "load_w": None, "power_factor": None, "phase_deg": None,
            "energy_wh": None, "energy_nr_wh": None, "reverse_energy_wh": None,
            "reverse_energy_nr_wh": None, "energy_start": None,
        }
    return {
        "metered": True,
        "voltage_v": _measure(measure.get("voltage")),
        "frequency_hz": _measure(measure.get("frequency")),
        "current_a": _amps(measure.get("current")),
        "load_w": _measure(measure.get("power")),
        "power_factor": _measure(measure.get("powerFactor")),
        "phase_deg": _measure(measure.get("phase")),
        "energy_wh": _as_int(measure.get("energy")),
        "energy_nr_wh": _as_int(measure.get("energyNR")),
        "reverse_energy_wh": _as_int(measure.get("reverseEnergy")),
        "reverse_energy_nr_wh": _as_int(measure.get("reverseEnergyNR")),
        "energy_start": _timestamp(measure.get("timestamp")),
    }


def _on_off(value: Any) -> bool | None:
    """The web login's "on" / "off"."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text == "on":
        return True
    if text == "off":
        return False
    return None


def web_output_live(item: dict[str, Any]) -> dict[str, Any]:
    """What an outputs/measure (or, from 5.4, outputs/state) item says that is
    written the moment it changes: on/off and whether the schedule is on."""
    values: dict[str, Any] = {}
    state = _on_off(item.get("state"))
    if state is not None:
        values["state"] = state
    if "isScheduleActive" in item:
        values["schedule_enabled"] = _as_bool(item.get("isScheduleActive"))
    return values


def web_output_listed(item: dict[str, Any]) -> dict[str, Any]:
    """An outputs/list item: the name and the assigned schedule's name
    (``N/A`` with scheduleId 0 when none is assigned)."""
    schedule = item.get("scheduleName")
    if _as_int(item.get("scheduleId")) == 0 or str(schedule or "").strip() in ("", "N/A"):
        schedule = None
    return {"name": _as_str(item.get("name")), "schedule_name": _as_str(schedule)}


def web_output_config(config: Any) -> dict[str, Any]:
    """An output's settings (outputs/id/<n>/config): its short on/off time
    and whether its schedule is on."""
    if not isinstance(config, dict):
        return {}
    values: dict[str, Any] = {}
    delay = _as_int(config.get("resetDelay"))
    if delay is not None:
        values["delay_ms"] = delay
    if "isScheduleActive" in config:
        values["schedule_enabled"] = _as_bool(config.get("isScheduleActive"))
    return values


def web_input(item: dict[str, Any]) -> dict[str, Any]:
    """An inputs/list item. "on" is the JSON API's State 1, a closed contact
    (the 4KS demo's open input read State 0 and "off" side by side). The
    temperature comes in the unit the NETIO is set to; both are reported."""
    temperature = _measure(item.get("temperature"))
    unit = str(item.get("temperatureUnit") or "C").strip().upper()
    temp_c = temp_f = None
    if temperature is not None:
        if unit == "F":
            temp_f, temp_c = temperature, round((temperature - 32) * 5 / 9, 1)
        else:
            temp_c, temp_f = temperature, round(temperature * 9 / 5 + 32, 1)
    humidity = _as_float(item.get("humidity"))
    if humidity in HUMIDITY_ABSENT:
        humidity = None
    return {
        "name": _as_str(item.get("name")),
        "closed": _on_off(item.get("state")),
        "s0_counter": _as_int(item.get("s0Counter")),
        "temperature_c": temp_c,
        "temperature_f": temp_f,
        "humidity": humidity,
    }


def web_numbered(items: Any, limit: int, parse) -> dict[int, dict[str, Any]]:
    """outputs/... or inputs/list items by their id, in order."""
    found: dict[int, dict[str, Any]] = {}
    if not isinstance(items, list):
        return found
    for item in items:
        if not isinstance(item, dict):
            continue
        local_id = _as_int(item.get("id"))
        if local_id is None or not 1 <= local_id <= limit:
            continue
        found[local_id] = parse(item)
    return dict(sorted(found.items()))


def web_named(items: Any, blank: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A wdtpingers/, rules/ or pab/list keyed by a child id made from each
    name, the same ids the JSON API's lists give. The web login's lists carry
    a name and whether the entry is enabled, nothing of its result."""
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(items, list):
        return found
    taken: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        values = dict(blank)
        values["name"] = _as_str(item.get("name"))
        values["enabled"] = _as_bool(item.get("enabled"))
        found[child_key(str(item["name"]), taken)] = values
    return found


WEB_BLANK = {
    "watchdog": {"name": None, "enabled": None, "failed": None, "last_ok": None, "last_check": None},
    "rule": {"name": None, "enabled": None, "result": None},
    "pab": {"name": None, "pab_type": None, "enabled": None, "in_range": None, "zone": None},
}
WEB_LISTS = {"watchdog": "wdtpingers/list", "rule": "rules/list", "pab": "pab/list"}


def _items(data: Any) -> Any:
    return data.get("items") if isinstance(data, dict) else None


def firmware_list_answered(items: Any) -> bool:
    """Whether fwupdate/list holds an answer from NETIO's update server.

    Until the first check since the unit started, it holds one item with every
    field empty (every demo unit, 2026-10-17). An answer names each item;
    with nothing newer it is one item named "No new firmware available" with
    no version or links (PyNetioConf).
    """
    if not isinstance(items, list) or not items:
        return False
    return all(
        isinstance(item, dict) and all(field in item for field in FW_LIST_FIELDS) and item.get("name")
        for item in items
    )


def firmware_offers(items: Any) -> list[dict[str, str]]:
    """The firmware an answer offers, newest first (the server lists it so).
    ``full`` is the link with the outlet controller's firmware, ``app`` the
    one without it."""
    offers = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        version = str(item.get("version") or "").split()
        full = str(item.get("downloadUrl") or "").strip()
        app = str(item.get("downloadUrlNoMcu") or "").strip()
        if not version and not full and not app:
            continue
        offers.append({
            "name": str(item.get("name") or "").strip(),
            "version": version[0] if version else "",
            "full": full,
            "app": app,
        })
    return offers


# ── Driver ───────────────────────────────────────────────────────────────────


CHILD_TYPES = ("output", "input", "watchdog", "rule", "pab")



class NetioDriver(BaseDriver):
    """NETIO power units over the JSON M2M API."""

    DRIVER_INFO = {
        "id": "netio",
        "name": "NETIO PowerPDU, PowerBOX, PowerCable and PowerDIN",
        "manufacturer": "NETIO",
        "category": "power",
        "version": "1.4.0",
        "author": "OpenAVC",
        # confirm on the commands that reset, switch everything, restart or
        # update the firmware needs 0.36.0; restarts_device_for on the updates
        # and the restart, 0.34.0; the commands that take or hand back a file
        # appear from 0.37.2 (FILE_COMMANDS, added in __init__);
        # the discovery probe's then: step, 0.32.0. The web login follows
        # Reach devices through where the platform can (0.37.0 and later; see
        # _open_web) and runs without it before that. A refused command's
        # sentence reaches Send Command from 0.37.1 (NetioCommandError) and the
        # server log before it. The probe's model, firmware and serial reach
        # the scan from 0.37.1; before it the unit is identified without them.
        "min_platform_version": "0.36.0",
        "description": (
            "Controls NETIO rack PDUs, smart power strips, power cables and DIN-rail units over "
            "the unit's web login (firmware 5.1.3 and later) or NETIO's JSON API: switch each "
            "output on or off, toggle it, reboot it or turn it on for a set time, and read each "
            "output's load, current, power factor and energy on metered models. Digital inputs, "
            "NETIO temperature and humidity sensors, each output's schedule and the unit's own "
            "watchdogs, rules and power analysis blocks are reported as live values. Over the web "
            "login the unit's firmware can be checked and updated from NETIO's update server and "
            "the unit restarted; on OpenAVC 0.37.2 and later, its HTTPS certificate installed, "
            "firmware installed from a file, and its configuration backed up and restored."
        ),
        "source_url": "https://www.netio-products.com/files/M2M-HTTP-JSON_NETIO-API-protocol_v2-6_.pdf",
        "tags": ["pdu", "power", "outlet-control", "metering", "energy", "rack", "power-strip", "din-rail"],
        "verified": True,
        "simulated": True,
        "protocols": ["netio-json", "netio-websocket"],
        "ports": [80],
        "transport": "http",
        "discovery": {
            # The web login's greeting. Once the WebSocket upgrade on /emweb is
            # answered, HELO is answered with the unit's type, firmware and
            # serial number, with no login and whatever its JSON API is set to
            # (firmware 5.1.3 and later). A frame the client sends must be
            # masked; this one's masking key is zero, so its bytes after the
            # six-byte header are the text {"type":"HELO","reqId":1}. It is the
            # probe's second step because the unit drops a frame sent together
            # with the upgrade (a PowerPDU 4KB on 5.2.4). A unit serving HTTPS
            # only (port 80 closes) or older firmware stays a possible NETIO by
            # its MAC block below.
            "tcp_probe": {
                "port": 80,
                "send_ascii": (
                    "GET /emweb HTTP/1.1\r\nHost: netio\r\nUpgrade: websocket\r\n"
                    "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ),
                "expect_regex": r"^HTTP/1\.1 101",
                "timeout_ms": 2000,
                "then": {
                    "send_hex": (
                        "81 99 00 00 00 00 7B 22 74 79 70 65 22 3A 22 48 45 4C 4F 22 2C 22 72 65 "
                        "71 49 64 22 3A 31 7D"
                    ),
                    "expect_regex": r'"devtype"\s*:\s*"[^"]+"',
                    "timeout_ms": 2000,
                },
                "extract": {
                    "model": {"regex": r'"devtype"\s*:\s*"([^"]+)"'},
                    "firmware": {"regex": r'"fwVersion"\s*:\s*"(\d+\.\d+\.\d+)'},
                    "serial_number": {"regex": r'"deviceSN"\s*:\s*"([^"]+)"'},
                },
            },
            # NETIO products a.s. (IEEE MA-L 24A42C). Every demo unit's MAC and
            # the bench PowerPDU 4KB's are in it.
            "oui": ["24:a4:2c"],
            "manufacturer_alias": ["netio", "netio products", "netio products a.s."],
        },
        "compatible_models": [
            {
                "manufacturer": "NETIO",
                "models": ["PowerPDU 4KB"],
                "confidence": "full",
                "notes": (
                    "Tested on a unit on firmware 5.2.4 and 5.3.2, over both the web login and the "
                    "JSON API, HTTP and HTTPS: every command, metering with a load plugged in, a "
                    "NETIO temperature and humidity sensor, outputs switched from elsewhere showing "
                    "at once over the web login, and the full firmware update from 5.2.4 to 5.3.2. "
                    "Over HTTPS the JSON API answers each request in about 4 seconds; the web login "
                    "is quick. Resetting an output's energy counter over the JSON API needs firmware "
                    "5.3; on 5.2.4 use the web login. Turning an output's schedule on or off needs a "
                    "schedule assigned to it in the NETIO's web interface. The first firmware check "
                    "after the NETIO starts sometimes gets no answer from NETIO's server; the driver "
                    "asks once more. On 5.3.2: Restart Device over HTTP and HTTPS (the outputs stay "
                    "on), an HTTPS certificate installed over HTTPS and then served, the "
                    "configuration backed up and restored, and firmware installed from NETIO's 1.6 MB "
                    "package in about 5 minutes (every output off for a moment during the install). "
                    "A configuration file goes in more slowly, about 1.6 seconds for each 2 KB."
                ),
            },
            {
                "manufacturer": "NETIO",
                "models": [
                    "PowerPDU 8QS", "PowerPDU 4PS", "PowerPDU 4KS", "PowerDIN 4PZ",
                    "PowerCable 2PZ", "PowerBOX 4KF",
                ],
                "confidence": "partial",
                "notes": (
                    "Run against NETIO's public demo units on firmware 5.3.1 and 5.3.2. JSON API: "
                    "identity, outputs, inputs, metering, watchdogs, rules and power analysis blocks "
                    "read; outputs switched, toggled, rebooted, switched on for a time and renamed. "
                    "Web login: the same reads, each output's schedule, and the outputs' switching "
                    "pushed as it happened; outputs switched, toggled, rebooted and switched on for "
                    "a time (the demo login may not rename outputs, change their schedules or reset "
                    "energy counters, so those were not run over the web login). The firmware, "
                    "certificate, configuration and restart commands were not run on these units: "
                    "the demo login may not change settings."
                ),
            },
            {
                "manufacturer": "NETIO",
                "models": [
                    "PowerPDU 4PB", "PowerPDU 4QV", "PowerPDU 8QB", "PowerPDU 8QV",
                    "PowerPDU 8KS", "PowerPDU 8KF", "PowerPDU 8KB", "PowerPDU FK6", "PowerPDU VK6",
                    "PowerBOX 3PF", "PowerBOX 3PE", "PowerBOX 3PG", "PowerBOX 4KE", "PowerBOX 4KG",
                    "PowerCable REST 101x", "PowerCable 1Kx", "PowerCable 2KZ", "PowerCable 2PF",
                    "PowerCable 2KF", "PowerCable 2PE", "PowerCable 2KE", "PowerCable 2PG",
                    "PowerCable 2KG", "PowerCable 2PS", "PowerCable 2KS", "PowerCable 2PB",
                    "PowerCable 2KB", "PowerDIN ZP3", "PowerDIN ZK3",
                ],
                "confidence": "untested",
                "notes": (
                    "Same universal firmware, JSON API and web login as the units above; the outlet "
                    "type in the model name (S, B, F, E, G, Z) does not change the API. Metering "
                    "follows the model: K models meter every output, Q models the whole unit and "
                    "output 1, P models switch without metering. Firmware 5.4 reads its outputs' "
                    "states from a topic of its own over the web login; that path follows NETIO's "
                    "library and has not been run on a 5.4 unit."
                ),
            },
        ],
        "help": {
            "overview": (
                "NETIO makes networked rack PDUs, power strips, power cables and DIN-rail units. "
                "Each output is a child entity here, so a macro, schedule or panel button can switch "
                "one output, reboot a frozen device, or switch an output on for a set time. On "
                "metered models each output's load and energy are reported too. Connected with the "
                "web login, an output switching shows the moment it happens and each output's "
                "schedule is reported, the unit's firmware can be checked and updated from NETIO's "
                "update server, and the unit can be restarted. On OpenAVC 0.37.2 and later Send "
                "Command also installs an HTTPS certificate, installs firmware from a file, and "
                "backs up and restores the unit's configuration. The results of the unit's own IP "
                "watchdogs, rules and power analysis blocks are reported with the JSON API only."
            ),
            "setup": (
                "1. Choose Connect With. The web login (firmware 5.1.3 and later) needs nothing "
                "turned on: enter a user from the NETIO's web interface as Username and Password. A "
                "user allowed to control outputs can switch them. Renaming an output, turning its "
                "schedule on or off and resetting its energy counter need a user that may change "
                "output settings. Checking for and installing firmware updates, restarting the unit, "
                "its HTTPS certificate and backing up or restoring its configuration need a user "
                "that may change the unit's settings, such as an administrator.\n"
                "2. For the JSON API instead, open M2M API Protocols in the NETIO's web interface, "
                "then the JSON API tab. Turn on Enable JSON API, Enable READ-ONLY and Enable WRITE, "
                "and save. The unit restarts its web server, so it answers errors for a few "
                "seconds. The tab has a username and password for reading and another pair for "
                "writing; NETIO ships both as netio / netio. Enter the reading login as Username "
                "and Password here, and the writing login under Advanced only when it is different.\n"
                "3. Port: for the web login, the port the web interface answers on (80, or 443 with "
                "Use HTTPS on); for the JSON API, the port on its tab (80 unless it was changed). "
                "The JSON API serves HTTPS from firmware 5.0.3, but slowly: the NETIO takes about 4 "
                "seconds to open each secure connection and the JSON API opens one per request, so "
                "with HTTPS on, use the web login.\n"
                "4. Outputs, inputs, watchdogs, rules and power analysis blocks appear as children "
                "once connected."
            ),
        },
        "default_config": {
            "connection": "json",
            "host": "",
            "port": 80,
            "ssl": False,
            "verify_ssl": False,
            "username": "netio",
            "password": "",
            "write_username": "",
            "write_password": "",
            "poll_interval": 10,
        },
        "config_schema": {
            "connection": {
                "type": "enum", "default": "json", "label": "Connect With",
                "values": [
                    {"value": "web", "label": "Web login (firmware 5.1.3 and later)"},
                    {"value": "json", "label": "JSON API"},
                ],
                "description": "The web login signs in as a user of the NETIO's web interface and "
                               "shows an output switching the moment it happens. The JSON API has "
                               "to be turned on in the NETIO first, and also reports the results of "
                               "its watchdogs, rules and power analysis blocks.",
            },
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "description": "The NETIO unit's IP address or hostname.",
            },
            "port": {
                "type": "integer", "default": 80, "label": "Port", "min": 1, "max": 65535,
                "description": "For the web login, the port the NETIO's web interface answers on (80, "
                               "or 443 with HTTPS). For the JSON API, the port set on its tab, 80 "
                               "unless it was changed.",
            },
            "ssl": {
                "type": "boolean", "default": False, "label": "Use HTTPS",
                "description": "Connects over HTTPS. The JSON API serves it from firmware 5.0.3.",
            },
            "verify_ssl": {
                "type": "boolean", "default": False, "label": "Verify HTTPS Certificate",
                "description": "Off accepts the unit's own self-signed certificate.",
            },
            "username": {
                "type": "string", "default": "netio", "label": "Username",
                "description": "For the web login, a user of the NETIO's web interface. For the JSON "
                               "API, its login for reading; leave Username and Password blank if the "
                               "unit reads with no login.",
            },
            "password": {
                "type": "string", "secret": True, "label": "Password",
                "description": "The password for that user. NETIO ships the JSON API's as netio.",
            },
            "write_username": {
                "type": "string", "default": "", "label": "Write Username", "advanced": True,
                "description": "JSON API only: its login for writing, when it differs from the one "
                               "for reading. Blank uses the reading login.",
            },
            "write_password": {
                "type": "string", "secret": True, "label": "Write Password", "advanced": True,
                "description": "JSON API only: its password for writing, when it differs from the "
                               "one for reading.",
            },
            "poll_interval": {
                "type": "integer", "default": 10, "label": "Poll Interval (sec)", "min": 0,
                "description": "How often to read the unit's status. With the web login, outputs "
                               "switching show at once and metering is updated at this interval. "
                               "0 turns polling off.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "device_name": {"type": "string", "label": "Device Name"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "mac_address": {"type": "string", "label": "MAC Address", "cloud_priority": "low"},
            "firmware": {"type": "string", "label": "Firmware Version"},
            "json_version": {"type": "string", "label": "JSON API Version", "cloud_priority": "low"},
            "device_time": {"type": "string", "label": "Device Clock", "cloud_priority": "low"},
            "uptime_s": {"type": "integer", "label": "Uptime", "unit": "s", "min": 0, "cloud_priority": "low"},
            "output_count": {"type": "integer", "label": "Outputs", "min": 0},
            "input_count": {"type": "integer", "label": "Inputs", "min": 0},
            "metered": {"type": "boolean", "label": "Metered"},
            "voltage_v": {
                "type": "number", "label": "Voltage", "unit": "V", "min": 0, "max": 300, "step": 0.01,
                "cloud_priority": "low",
            },
            "frequency_hz": {
                "type": "number", "label": "Frequency", "unit": "Hz", "min": 0, "max": 70, "step": 0.01,
                "cloud_priority": "low",
            },
            "total_current_a": {
                "type": "number", "label": "Total Current", "unit": "A", "min": 0, "max": 20, "step": 0.001,
                "cloud_priority": "high",
            },
            "total_load_w": {
                "type": "number", "label": "Total Load", "unit": "W", "min": 0, "cloud_priority": "high",
            },
            "total_power_factor": {
                "type": "number", "label": "Total Power Factor", "min": 0, "max": 1, "step": 0.01,
                "cloud_priority": "low",
            },
            "total_phase_deg": {
                "type": "number", "label": "Total Phase", "unit": "°", "min": 0, "max": 360, "step": 0.01,
                "cloud_priority": "low",
            },
            "total_energy_wh": {
                "type": "integer", "label": "Total Energy", "unit": "Wh", "min": 0, "cloud_priority": "low",
            },
            "total_energy_nr_wh": {
                "type": "integer", "label": "Total Energy (not resettable)", "unit": "Wh", "min": 0,
                "cloud_priority": "low",
            },
            "total_reverse_energy_wh": {
                "type": "integer", "label": "Total Reverse Energy", "unit": "Wh", "min": 0,
                "cloud_priority": "low",
            },
            "total_reverse_energy_nr_wh": {
                "type": "integer", "label": "Total Reverse Energy (not resettable)", "unit": "Wh", "min": 0,
                "cloud_priority": "low",
            },
            "energy_start": {"type": "string", "label": "Energy Counted Since", "cloud_priority": "low"},
            "firmware_update_available": {
                "type": "boolean", "label": "Firmware Update Available", "cloud_priority": "low",
                "help": "Whether NETIO's update server offered newer firmware at the last Check "
                        "for Firmware Update.",
            },
            "firmware_update_version": {
                "type": "string", "label": "Firmware Update Offered", "cloud_priority": "low",
                "help": "The newest firmware NETIO's update server offered at the last check. "
                        "Blank when it offered nothing newer.",
            },
            "firmware_update_status": {
                "type": "string", "label": "Firmware Update Status", "cloud_priority": "low",
                "help": "How the last firmware update went: still updating, updated, or why it "
                        "failed.",
            },
            "https_certificate_fingerprint": {
                "type": "string", "label": "HTTPS Certificate Fingerprint", "cloud_priority": "low",
                "help": "The SHA-256 fingerprint of the certificate the NETIO serves over HTTPS. "
                        "Reported with the web login and Use HTTPS on.",
            },
            "https_certificate_expires": {
                "type": "string", "label": "HTTPS Certificate Expires", "cloud_priority": "low",
                "help": "When the certificate the NETIO serves over HTTPS expires (UTC). Reported "
                        "with the web login and Use HTTPS on.",
            },
            "https_certificate_days_left": {
                "type": "integer", "label": "HTTPS Certificate Days Left", "cloud_priority": "low",
                "help": "Days until the certificate the NETIO serves over HTTPS expires, below 0 "
                        "once it has. Reported with the web login and Use HTTPS on.",
            },
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "output": {
                "label": "Output",
                "label_plural": "Outputs",
                "id_format": {"type": "integer", "min": 1, "max": 24},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "state": {"type": "boolean", "label": "On", "control": True, "cloud_priority": "high"},
                    "delay_ms": {
                        "type": "integer", "label": "Short On/Off Time", "unit": "ms", "min": 0,
                        "cloud_priority": "low",
                    },
                    "metered": {"type": "boolean", "label": "Metered", "cloud_priority": "low"},
                    "voltage_v": {
                        "type": "number", "label": "Voltage", "unit": "V", "min": 0, "max": 300, "step": 0.01,
                        "cloud_priority": "low",
                    },
                    "frequency_hz": {
                        "type": "number", "label": "Frequency", "unit": "Hz", "min": 0, "max": 70, "step": 0.01,
                        "cloud_priority": "low",
                    },
                    "current_a": {
                        "type": "number", "label": "Current", "unit": "A", "min": 0, "max": 20, "step": 0.001,
                        "cloud_priority": "low",
                    },
                    "load_w": {"type": "number", "label": "Load", "unit": "W", "min": 0, "cloud_priority": "low"},
                    "power_factor": {
                        "type": "number", "label": "Power Factor", "min": 0, "max": 1, "step": 0.01,
                        "cloud_priority": "low",
                    },
                    "phase_deg": {
                        "type": "number", "label": "Phase", "unit": "°", "min": 0, "max": 360, "step": 0.01,
                        "cloud_priority": "low",
                    },
                    "energy_wh": {"type": "integer", "label": "Energy", "unit": "Wh", "min": 0, "cloud_priority": "low"},
                    "energy_nr_wh": {
                        "type": "integer", "label": "Energy (not resettable)", "unit": "Wh", "min": 0,
                        "cloud_priority": "low",
                    },
                    "reverse_energy_wh": {
                        "type": "integer", "label": "Reverse Energy", "unit": "Wh", "min": 0, "cloud_priority": "low",
                    },
                    "reverse_energy_nr_wh": {
                        "type": "integer", "label": "Reverse Energy (not resettable)", "unit": "Wh", "min": 0,
                        "cloud_priority": "low",
                    },
                    "energy_start": {"type": "string", "label": "Energy Counted Since", "cloud_priority": "low"},
                    "schedule_enabled": {
                        "type": "boolean", "label": "Schedule On", "cloud_priority": "low",
                        "help": "Whether the schedule assigned to this output switches it. Reported "
                                "with the web login.",
                    },
                    "schedule_name": {
                        "type": "string", "label": "Schedule", "cloud_priority": "low",
                        "help": "The schedule assigned to this output in the NETIO's web interface. "
                                "Reported with the web login.",
                    },
                },
                "summary_fields": ["name", "state", "load_w"],
                "label_field": "name",
            },
            "input": {
                "label": "Input",
                "label_plural": "Inputs",
                "id_format": {"type": "integer", "min": 1, "max": 8},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "closed": {"type": "boolean", "label": "Contact Closed", "cloud_priority": "high"},
                    "s0_counter": {"type": "integer", "label": "S0 Pulse Count", "min": 0, "cloud_priority": "low"},
                    "temperature_c": {
                        "type": "number", "label": "Temperature", "unit": "°C", "min": -40, "max": 125, "step": 0.1,
                        "cloud_priority": "high",
                    },
                    "temperature_f": {
                        "type": "number", "label": "Temperature (F)", "unit": "°F", "min": -40, "max": 257, "step": 0.1,
                        "cloud_priority": "low",
                    },
                    "humidity": {
                        "type": "number", "label": "Humidity", "unit": "%", "min": 0, "max": 100, "step": 1,
                        "cloud_priority": "high",
                    },
                },
                "summary_fields": ["name", "closed", "temperature_c"],
                "label_field": "name",
            },
            "watchdog": {
                "label": "Watchdog",
                "label_plural": "Watchdogs",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "enabled": {"type": "boolean", "label": "Enabled", "cloud_priority": "low"},
                    "failed": {"type": "boolean", "label": "Failed", "cloud_priority": "high"},
                    "last_ok": {"type": "boolean", "label": "Last Check Passed", "cloud_priority": "high"},
                    "last_check": {"type": "string", "label": "Last Checked (device clock)", "cloud_priority": "low"},
                },
                "summary_fields": ["name", "enabled", "failed"],
                "label_field": "name",
            },
            "rule": {
                "label": "Rule",
                "label_plural": "Rules",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "enabled": {"type": "boolean", "label": "Enabled", "cloud_priority": "low"},
                    "result": {"type": "boolean", "label": "Result", "cloud_priority": "high"},
                },
                "summary_fields": ["name", "enabled", "result"],
                "label_field": "name",
            },
            "pab": {
                "label": "Power Analysis Block",
                "label_plural": "Power Analysis Blocks",
                "id_format": {"type": "string"},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "pab_type": {"type": "string", "label": "Type", "cloud_priority": "low"},
                    "enabled": {"type": "boolean", "label": "Enabled", "cloud_priority": "low"},
                    "in_range": {"type": "boolean", "label": "In Range", "cloud_priority": "high"},
                    "zone": {"type": "integer", "label": "Zone", "min": 0, "cloud_priority": "high"},
                },
                "summary_fields": ["name", "enabled", "in_range"],
                "label_field": "name",
            },
        },
        "quick_actions": ["output_on", "output_off", "output_reboot", "refresh"],
        "actions": [
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "all_outputs_off",
                "kind": "command",
                "icon": "power-off",
                "confirm": "Switch off every output on this NETIO. Everything plugged into it loses power. Continue?",
            },
            {
                "id": "all_outputs_reboot",
                "kind": "command",
                "icon": "rotate-ccw",
                "confirm": "Reboot every output on this NETIO. Everything plugged into it loses power and comes back. Continue?",
            },
        ],
        "commands": {
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Reads every value from the NETIO now.",
            },
            "output_on": {
                "label": "Turn Output On",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "sets": {"state": True},
                "help": "Switches the output on.",
            },
            "output_off": {
                "label": "Turn Output Off",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "sets": {"state": False},
                "help": "Switches the output off.",
            },
            "output_toggle": {
                "label": "Toggle Output",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "help": "Switches the output to the opposite of what it is now.",
            },
            "output_reboot": {
                "label": "Reboot Output",
                "params": {
                    "output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"},
                    "seconds": {
                        "type": "number", "required": False, "label": "Off Time (sec)", "min": 0.1,
                        "help": "How long the output stays off. Blank uses the output's own short "
                                "on/off time, set in the NETIO's web interface. JSON API only: with "
                                "the web login the output's own time is always used.",
                    },
                },
                "help": "Switches the output off, then on again after its short on/off time "
                        "(or the time given).",
            },
            "output_on_for": {
                "label": "Turn Output On for a Time",
                "params": {
                    "output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"},
                    "seconds": {
                        "type": "number", "required": False, "label": "On Time (sec)", "min": 0.1,
                        "help": "How long the output stays on. Blank uses the output's own short "
                                "on/off time, set in the NETIO's web interface. JSON API only: with "
                                "the web login the output's own time is always used.",
                    },
                },
                "help": "Switches the output on, then off again after its short on/off time "
                        "(or the time given).",
            },
            "all_outputs_on": {
                "label": "Turn All Outputs On",
                "params": {},
                "help": "Switches every output on.",
            },
            "all_outputs_off": {
                "label": "Turn All Outputs Off",
                "params": {},
                "confirm": "Switches off every output on this NETIO. Everything plugged into it loses power.",
                "help": "Switches every output off.",
            },
            "all_outputs_reboot": {
                "label": "Reboot All Outputs",
                "params": {},
                "confirm": "Reboots every output on this NETIO. Everything plugged into it loses power "
                           "and comes back.",
                "help": "Switches every output off, then on again, each after its own short on/off time.",
            },
            "rename_output": {
                "label": "Rename Output",
                "params": {
                    "output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"},
                    "name": {"type": "string", "required": True, "label": "Name"},
                },
                "sets": {"name": "{name}"},
                "help": "Names the output, so the NETIO's web interface and this device page both "
                        "show what is plugged into it.",
            },
            "reset_output_energy": {
                "label": "Reset Output Energy Counter",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "confirm": "Zeroes this output's energy counter. The counted total cannot be recovered; "
                           "the not-resettable counter keeps counting.",
                "help": "Zeroes the output's resettable energy counter and restarts it from now. "
                        "Metered outputs only.",
            },
            "enable_output_schedule": {
                "label": "Enable Output Schedule",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "help": "Turns on the schedule assigned to this output in the NETIO's web interface, "
                        "so the output is switched at its scheduled times again.",
            },
            "disable_output_schedule": {
                "label": "Disable Output Schedule",
                "params": {"output": {"type": "child_id", "child_type": "output", "required": True, "label": "Output"}},
                "help": "Turns off the schedule assigned to this output, so the output stays as it "
                        "is until it is switched by hand or by OpenAVC.",
            },
            "check_firmware_update": {
                "label": "Check for Firmware Update",
                "params": {},
                "help": "Has the NETIO ask NETIO's update server whether newer firmware is "
                        "available, and shows the answer in Firmware Update Available. Takes a "
                        "few seconds. Web login only, with a user that may change the NETIO's "
                        "settings.",
            },
            # Two commands rather than one with a choice, so nothing is chosen
            # for the person: each says what happens to the outputs.
            #
            # restarts_device_for opens when the update starts, so it covers
            # the download as well as the restarts. Measured on a PowerPDU 4KB,
            # 5.2.4 to 5.3.2, full update (2026-10-21): it downloaded for 218 s
            # while still answering, then restarted twice (the outlet
            # controller's update is the second), and was back 298 s after the
            # command. The download depends on the site's internet link, so the
            # window is twice that. It ends as soon as the unit is back.
            "update_firmware_full": {
                "label": "Update Firmware: Full (Outlets Off Briefly)",
                "params": {},
                "confirm": "Installs the newest firmware from NETIO's update server, the outlet "
                           "controller's included. The NETIO downloads it first, which can take a "
                           "few minutes, then restarts, and every output switches off for a few "
                           "seconds.",
                "restarts_device_for": 600,
                "help": "Installs the newest firmware NETIO's update server offers, the outlet "
                        "controller's included, as Update incl. low-level system does in the "
                        "NETIO's web interface. Every output switches off for a few seconds. "
                        "NETIO recommends this update unless something plugged in cannot lose "
                        "power. Web login only, with a user that may change the NETIO's settings.",
            },
            "update_firmware_keep_outlets": {
                "label": "Update Firmware: Keep Outlets On",
                "params": {},
                "confirm": "Installs the newest firmware from NETIO's update server without the "
                           "outlet controller's, so the outputs stay as they are. The NETIO "
                           "downloads it first, which can take a few minutes, then restarts.",
                "restarts_device_for": 600,
                "help": "Installs the newest firmware NETIO's update server offers without the "
                        "outlet controller's, as Update device firmware does in the NETIO's web "
                        "interface, so the outputs stay as they are. Web login only, with a user "
                        "that may change the NETIO's settings.",
            },
            # A PowerPDU 4KB on 5.3.2 answered again 21.8 s after system/reset
            # over HTTP and the web login was back 22 s after it over HTTPS
            # (2026-10-21); the window ends as soon as it is back.
            "restart_device": {
                "label": "Restart Device",
                "params": {},
                "confirm": "Restarts the NETIO. Its outputs stay as they are; it answers nothing "
                           "for about half a minute.",
                "restarts_device_for": 60,
                "help": "Restarts the NETIO, as Restart device does in its web interface. The "
                        "outputs stay as they are. Web login only, with a user that may change "
                        "the NETIO's settings.",
            },
        },
    }

    # The driver owns its session (an HTTP client, or the web login's
    # WebSocket), so the platform's silence check does not watch it: this
    # probe does, alongside the poll and while polling is off.
    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 10.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "The NETIO stopped answering."

    _client: httpx.AsyncClient | None = None

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        super().__init__(device_id, config, state, events)
        if CommandFile is not None:
            # Every door reads this instance's table, so the file commands
            # appear where the platform can run them; the catalog's floor comes
            # from the class's table and stays where it is.
            base = type(self).DRIVER_INFO
            self.DRIVER_INFO = {**base, "commands": {**base["commands"], **FILE_COMMANDS}}
        self._client = None
        self._connect_doc: dict[str, Any] | None = None
        self._rosters: dict[str, list[Any]] = {t: [] for t in CHILD_TYPES}
        for key in ("password", "write_password"):
            secret = str(config.get(key, "") or "")
            if secret:
                self.redact_in_log(secret)
        # The web login's session.
        self._ws: Any = None
        self._ws_reader: asyncio.Task | None = None
        self._ws_pending: dict[int, asyncio.Future] = {}
        self._ws_req_id = 0
        self._ws_send_lock = asyncio.Lock()
        self._ws_last_send = 0.0
        self._ws_closing = False
        self._fw: tuple[int, int, int] | None = None
        # The major of the web login's serverApiVersion (HELO), which decides
        # how a file goes to the unit.
        self._api_major: int | None = None
        # The certificate the web login was served over wss: its fingerprint,
        # and when it expires, for the days left.
        self._cert_fingerprint: str | None = None
        self._cert_expires: datetime | None = None
        # Files being sent to the unit: the poll and the liveness check wait
        # (see _web_poll).
        self._sending_files = 0
        self._hello_device: dict[str, Any] = {}
        self._web_info: dict[str, Any] | None = None
        self._web_metered: bool | None = None
        self._web_held: set[str] = set()
        # output -> its on/off and schedule as last written, so a pushed event
        # writes only what changed.
        self._web_live: dict[int, dict[str, Any]] = {}
        # topic -> when its pushed metering is next written.
        self._web_meter_next: dict[str, float] = {}
        self._web_connect: dict[str, Any] | None = None
        # The online firmware update: the last check's offers and when they
        # were read, the update in progress (it outlives the restarts it
        # causes), and the wait for the unit's next attempt after a failed one.
        self._fw_offers: list[dict[str, str]] | None = None
        self._fw_offers_at = 0.0
        self._fw_update: dict[str, Any] | None = None
        self._fw_retry_watch: asyncio.Task | None = None

    # ── Config ──

    @property
    def _web(self) -> bool:
        connection = str(self.config.get("connection", CONNECT_JSON) or CONNECT_JSON)
        return connection.strip().lower() == CONNECT_WEB

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

    @property
    def _port(self) -> int:
        port = _as_int(self.config.get("port"))
        return port if port and 0 < port < 65536 else 80

    @property
    def _scheme(self) -> str:
        return "https" if _as_bool(self.config.get("ssl")) else "http"

    @property
    def _username(self) -> str:
        return str(self.config.get("username", "") or "").strip()

    @property
    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    def _read_auth(self) -> httpx.BasicAuth | None:
        # NETIO: "Empty Username and Password means no authentication."
        if not self._username and not self._password:
            return None
        return httpx.BasicAuth(self._username, self._password)

    def _write_auth(self) -> httpx.BasicAuth | None:
        username = str(self.config.get("write_username", "") or "").strip()
        password = str(self.config.get("write_password", "") or "")
        if not username and not password:
            return self._read_auth()
        return httpx.BasicAuth(username, password)

    def _where(self) -> str:
        return f"{self._host}:{self._port}"

    def _not_responding(self, exc: Exception) -> ConnectionError:
        # A timeout's own text is empty; its name says what happened.
        detail = str(exc).strip() or type(exc).__name__
        return ConnectionError(f"{self._where()} is not responding: {detail}")

    # ── Sentences ──

    def _read_refused(self) -> ConnectionFaultError:
        message = "The NETIO refused the JSON API login for reading (HTTP 401)."
        if not self._password:
            message += " No password is entered; NETIO ships the reading login as netio / netio."
        return ConnectionFaultError(message, code="auth_failed")

    def _api_off(self) -> str:
        return (
            f"The device at {self._where()} answered, but not its JSON API (HTTP 404). Turn on "
            f"JSON API under M2M API Protocols on the NETIO, and check the port set there."
        )

    def _web_absent(self) -> str:
        return (
            f"The device at {self._where()} answered, but has no web login (HTTP 404). The web "
            f"login needs NETIO firmware 5.1.3 or later: update the firmware, or set Connect With "
            f"to JSON API."
        )

    @staticmethod
    def _write_refused(exc: NetioHTTPError) -> str:
        if exc.status == 401:
            return "The NETIO refused the JSON API login for writing (HTTP 401), so nothing changed."
        if exc.status == 403:
            return "The NETIO takes reads only (HTTP 403): writing is off on its JSON API tab."
        if exc.status == 404:
            return "The NETIO's JSON API is off (HTTP 404), so nothing changed."
        if exc.status == 400:
            detail = f": {exc.message}" if exc.message else ""
            return f"The NETIO refused the command (HTTP 400{detail})."
        detail = f" ({exc.message})" if exc.message else ""
        return f"The NETIO answered the command with HTTP {exc.status}{detail}."

    def _web_refused(self, exc: NetioWebError, what: str) -> NetioCommandError:
        if exc.code == "Unauthorized":
            message = f"The NETIO refused to {what}: this login is not allowed to."
        else:
            detail = f": {exc.message}" if exc.message else ""
            message = f"The NETIO refused to {what} ({exc.code}{detail})."
        self.set_state("last_error", message)
        return NetioCommandError(message)

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        if not self._host:
            raise ConnectionFaultError("No IP address configured", code="invalid_config")
        if self._web and (not self._username or not self._password):
            # A sign-in that cannot succeed is never sent: failed ones count
            # toward the unit's lockout.
            raise ConnectionFaultError(
                "No web login is entered: the web login needs a user of the NETIO's web interface "
                "and its password.",
                code="auth_failed",
            )
        if not await self._verify_reachable(self._host, self._port):
            raise ConnectionError(f"{self._where()} is not responding")
        if self._web:
            await self._open_web()
            return
        self._client = httpx.AsyncClient(
            base_url=f"{self._scheme}://{self._where()}",
            verify=bool(_as_bool(self.config.get("verify_ssl"))),
            # The unit answers every request with Connection: close, so over
            # HTTPS each one starts with a TLS handshake, and a PowerPDU 4KB
            # takes 4.0 s over one (8 of 8 between 3.89 and 4.14 s), longer
            # while another client is negotiating with it.
            timeout=httpx.Timeout(10.0, connect=HTTPS_CONNECT_S if self._scheme == "https" else 5.0),
            # A refusal must arrive as itself, never as a page a redirect led to.
            follow_redirects=False,
        )

    async def _post_connect(self) -> None:
        if self._web:
            await self._web_handshake()
            return
        try:
            doc = await self._read()
        except NetioHTTPError as exc:
            if exc.status == 401:
                raise self._read_refused() from exc
            if exc.status == 403:
                # A refusal on the core read at connect: retrying cannot help
                # until someone changes the unit, so it pauses like a refused login.
                raise ConnectionFaultError(
                    "The NETIO refused to share its status (HTTP 403): reading is off on its "
                    "JSON API tab.",
                    code="auth_failed",
                ) from exc
            if exc.status == 404:
                raise ConnectionFaultError(self._api_off(), code="invalid_config") from exc
            raise ConnectionError(f"The NETIO answered the status read with {exc}") from exc
        except NetioReplyError as exc:
            raise ConnectionFaultError(
                f"The device at {self._where()} answered, but not with NETIO's JSON status: {exc}.",
                code="invalid_config",
            ) from exc
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc
        self._connect_doc = doc
        agent = doc.get("Agent") or {}
        log.info(
            f"[{self.device_id}] Connected to NETIO {agent.get('Model', '?')} "
            f"{agent.get('DeviceName', '')} at {self._where()} (firmware {agent.get('Version', '?')})"
        )

    async def _initial_sync(self) -> None:
        if self._web:
            snapshot, self._web_connect = self._web_connect, None
            if snapshot is not None:
                self._apply_web(snapshot)
                self._fw_follow(snapshot)
            return
        doc, self._connect_doc = self._connect_doc, None
        if doc is not None:
            self._apply(doc)

    def _link_alive(self) -> bool:
        if self._web:
            return self._ws is not None and _ws_open(self._ws)
        return self._client is not None

    async def _close_session(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        await self._fw_stop_retry_watch()
        await self._close_web()

    async def _liveness_probe(self) -> None:
        """The JSON API's status read, or the web login's PING. Any answer is a
        live unit; only a refused reading login is a fault of its own."""
        if self._web:
            await self._web_probe()
            return
        try:
            await self._read()
        except NetioHTTPError as exc:
            if exc.status == 401:
                raise self._read_refused() from exc
            return
        except NetioReplyError:
            return
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc

    # ── HTTP ──

    def _require_client(self) -> httpx.AsyncClient:
        client = self._client
        if client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        return client

    @staticmethod
    def _status_from(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code != 200:
            raise NetioHTTPError(resp.status_code, parse_error(resp.text))
        return parse_status(resp.text)

    async def _read(self) -> dict[str, Any]:
        client = self._require_client()
        resp = await client.get(STATUS_PATH, auth=self._read_auth())
        return self._status_from(resp)

    async def _write(
        self, outputs: list[dict[str, Any]], *, end_session: bool = False, bad_request: str = "",
    ) -> None:
        """POST the changes and take the status the unit answers with.

        ``end_session`` closes the HTTP connection after the answer. A short
        on/off time sent with a command "is valid only for a single protocol
        session", and the unit goes back to each output's own time "when the
        connection is closed" (document, "Output actions"). On a PowerPDU 4KB
        (5.2.4) a time did not carry over to the next request on one held
        connection, and the unit answers every request with Connection: close
        anyway; the close stays, as the document asks for it.

        ``bad_request`` is the sentence for an HTTP 400, when the command has a
        likelier reason for one than a malformed request.
        """
        client = self._require_client()
        headers = {"Connection": "close"} if end_session else None
        try:
            resp = await client.post(
                STATUS_PATH, json={"Outputs": outputs}, auth=self._write_auth(), headers=headers,
            )
            doc = self._status_from(resp)
        except NetioHTTPError as exc:
            message = bad_request if exc.status == 400 and bad_request else self._write_refused(exc)
            self.set_state("last_error", message)
            raise NetioCommandError(message) from exc
        except NetioReplyError as exc:
            message = f"The NETIO answered the command with something other than its status: {exc}."
            self.set_state("last_error", message)
            raise NetioCommandError(message) from exc
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc
        self._apply_write_reply(doc)

    # ── Web login: the socket ──

    def _web_url(self) -> str:
        scheme = "wss" if self._scheme == "https" else "ws"
        return f"{scheme}://{self._where()}{WEB_PATH}"

    def _web_ssl(self) -> ssl.SSLContext | None:
        if self._scheme != "https":
            return None
        context = ssl.create_default_context()
        if not _as_bool(self.config.get("verify_ssl")):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def _open_web(self) -> None:
        kwargs: dict[str, Any] = {
            # Over wss the unit's TLS handshake comes first: 4 s on a PowerPDU
            # 4KB, 8 to 12 s while another client is negotiating with it.
            "open_timeout": HTTPS_CONNECT_S if self._scheme == "https" else WEB_OPEN_TIMEOUT_S,
            "close_timeout": 2.0,
            "max_size": None,
            # Liveness is the protocol's own PING (_web_probe).
            "ping_interval": None,
        }
        context = self._web_ssl()
        if context is not None:
            kwargs["ssl"] = context
        # From the adapter Settings > Network > Reach devices through chose.
        # The platform names it from 0.37.0; on 0.36.0 the computer picks, as
        # it does for every driver's own connection there.
        local_address = getattr(self, "local_address", None)
        ip = local_address(self._host) if callable(local_address) else None
        if ip:
            kwargs["local_addr"] = (ip, 0)
        self._ws_closing = False
        try:
            ws = await websockets.connect(self._web_url(), **kwargs)
        except websockets.exceptions.InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404:
                raise ConnectionFaultError(self._web_absent(), code="invalid_config") from exc
            raise ConnectionError(
                f"{self._where()} refused the web login's connection (HTTP {status})."
            ) from exc
        except websockets.exceptions.InvalidHandshake as exc:
            raise ConnectionError(
                f"{self._where()} did not open the web login's connection: {exc}"
            ) from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise self._not_responding(exc) from exc
        self._ws = ws
        self._note_certificate(ws)
        self._ws_reader = asyncio.create_task(self._web_read_loop(ws))

    def _note_certificate(self, ws: Any) -> None:
        """The certificate this connection was served over wss: its
        fingerprint and expiry, read from the handshake already made (a
        second TLS connection costs the unit seconds, and two at once once
        stopped its HTTPS server answering)."""
        self._cert_fingerprint = None
        self._cert_expires = None
        if self._scheme != "https" or certificate_details is None:
            return
        transport = getattr(ws, "transport", None)
        ssl_object = transport.get_extra_info("ssl_object") if transport is not None else None
        try:
            der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        except (ValueError, ssl.SSLError):
            der = None
        details = certificate_details(der) if der else None
        if not details:
            return
        self._cert_fingerprint = certificate_fingerprint(str(details.get("sha256") or ""))
        try:
            self._cert_expires = datetime.fromisoformat(str(details.get("not_after")))
        except ValueError:
            self._cert_expires = None

    def _certificate_states(self) -> dict[str, Any]:
        expires = self._cert_expires
        days = None
        if expires is not None:
            days = math.floor((expires - datetime.now(timezone.utc)).total_seconds() / 86400)
        return {
            "https_certificate_fingerprint": self._cert_fingerprint,
            "https_certificate_expires": expires.strftime("%Y-%m-%dT%H:%M:%SZ") if expires else None,
            "https_certificate_days_left": days,
        }

    async def _close_web(self) -> None:
        ws, self._ws = self._ws, None
        reader, self._ws_reader = self._ws_reader, None
        self._ws_closing = True
        if ws is not None:
            try:
                # The unit keeps a session for every open socket and has room
                # for only a few (PyNetioConf), so a socket is closed, never
                # left behind.
                await ws.close()
            except Exception:  # noqa: BLE001 - a socket that cannot close cleanly is gone
                pass
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.wait({reader})
        self._web_fail_pending(
            ConnectionError(f"The connection to the NETIO at {self._where()} was closed.")
        )
        self._web_held.clear()

    def _web_fail_pending(self, exc: Exception) -> None:
        pending, self._ws_pending = self._ws_pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    async def _web_read_loop(self, ws: Any) -> None:
        """Every frame the unit sends: a reply goes to the request with its
        reqId, an EVENT to the state it reports. Replies are never matched by
        arrival, since events arrive between them all the time."""
        try:
            async for raw in ws:
                text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
                self.record_traffic("rx", text)
                try:
                    frame = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "EVENT":
                    try:
                        self._on_web_event(str(frame.get("topic") or ""), frame.get("data"))
                    except Exception:  # noqa: BLE001 - one bad event never ends the session
                        log.debug(f"[{self.device_id}] Could not apply an event", exc_info=True)
                    continue
                req_id = frame.get("reqId")
                if isinstance(req_id, int) and not isinstance(req_id, bool):
                    fut = self._ws_pending.get(req_id)
                    if fut is not None and not fut.done():
                        fut.set_result(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any end of the socket is a dropped link
            log.debug(f"[{self.device_id}] The web login's connection ended: {exc}")
        if self._ws is ws and not self._ws_closing:
            self._ws = None
            self._ws_reader = None
            self._web_fail_pending(
                ConnectionError(f"The connection to the NETIO at {self._where()} dropped.")
            )
            self._stash_fault("transport_disconnected", "The connection to the NETIO dropped.")
            self._handle_transport_disconnect()

    async def _web_send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        text = json.dumps(frame, ensure_ascii=False)
        loop = asyncio.get_running_loop()
        async with self._ws_send_lock:
            wait = self._ws_last_send + WEB_SEND_GAP_S - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await ws.send(text)
            except websockets.exceptions.ConnectionClosed as exc:
                raise ConnectionError(
                    f"The connection to the NETIO at {self._where()} dropped."
                ) from exc
            self._ws_last_send = loop.time()
        self.record_traffic("tx", text)

    async def _web_call(
        self,
        kind: str,
        topic: str | None = None,
        data: Any = None,
        *,
        extra: dict[str, Any] | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """One request and its reply. A reply with ``"status": "ERROR"`` raises
        NetioWebError unless ``check`` is off; no reply raises ConnectionError."""
        req_id = self._ws_req_id
        self._ws_req_id += 1
        frame: dict[str, Any] = {"type": kind, "reqId": req_id}
        if topic:
            frame["topic"] = topic
        if data is not None:
            frame["data"] = data
        if extra:
            frame.update(extra)
        fut = asyncio.get_running_loop().create_future()
        self._ws_pending[req_id] = fut
        try:
            await self._web_send(frame)
            reply = await asyncio.wait_for(fut, timeout or WEB_REPLY_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            what = f"{kind} {topic}" if topic else kind
            raise ConnectionError(f"{self._where()} did not answer {what}.") from exc
        finally:
            self._ws_pending.pop(req_id, None)
            # A drop while the frame waited its turn fails the reply too, and
            # the send's own error is the one raised.
            if fut.done() and not fut.cancelled():
                fut.exception()
        if check and str(reply.get("status") or "OK") != "OK":
            error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
            raise NetioWebError(
                str(error.get("code") or reply.get("status")),
                str(error.get("message") or "").strip().rstrip(":").strip(),
                topic or "",
            )
        return reply

    def _web_reads_with_get(self) -> bool:
        return self._fw is not None and self._fw >= WEB_GET_SINCE

    async def _web_read(self, topic: str, *, hold: bool = False) -> Any:
        """A topic's current data.

        A held topic is subscribed and stays so, for its pushed changes; read
        again it is subscribed again before 5.4 (the unit answers with the data
        and does not double the events, checked on the 4KS demo) and read with
        GET from 5.4. Any other topic is read with SUBSCRIBE and UNSUBSCRIBE
        before 5.4, as PyNetioConf does, and with GET from 5.4.
        """
        hold = hold or topic in self._web_held
        if self._web_reads_with_get() and (topic in self._web_held or not hold):
            return (await self._web_call("GET", topic)).get("data")
        reply = await self._web_call("SUBSCRIBE", topic)
        if hold:
            self._web_held.add(topic)
        else:
            await self._web_call("UNSUBSCRIBE", topic, check=False)
        return reply.get("data")

    async def _web_optional(self, topic: str, refusals: list[str], *, hold: bool = False) -> Any:
        """A read the unit may refuse this user: the refusal is noted, not raised."""
        try:
            return await self._web_read(topic, hold=hold)
        except NetioWebError as exc:
            refusals.append(f"{topic} ({exc.code})")
            return None

    # ── Web login: signing in and reading ──

    async def _web_handshake(self) -> None:
        try:
            hello = await self._web_call("HELO")
        except NetioWebError as exc:
            raise ConnectionFaultError(
                f"The device at {self._where()} refused the web login's greeting ({exc}).",
                code="invalid_config",
            ) from exc
        data = hello.get("data") if isinstance(hello.get("data"), dict) else {}
        public_key = data.get("publicKey")
        stamp = _as_int(data.get("localTimestamp"))
        if not public_key or stamp is None:
            raise ConnectionFaultError(
                f"The device at {self._where()} answered, but not with NETIO's web login greeting.",
                code="invalid_config",
            )
        device = data.get("device") if isinstance(data.get("device"), dict) else {}
        self._hello_device = device
        self._fw = firmware_version(device.get("fwVersion"))
        self._api_major = web_api_major(data.get("serverApiVersion"), self._fw)
        if self._fw is not None and self._fw < WEB_API_SINCE:
            raise ConnectionFaultError(
                f"The NETIO runs firmware {_firmware_text(device.get('fwVersion'))}; the web login "
                f"needs 5.1.3 or later. Update the firmware, or set Connect With to JSON API.",
                code="invalid_config",
            )
        await self._web_sign_in(str(public_key), stamp)
        self._web_connect = await self._web_snapshot(connecting=True)
        info = self._web_connect.get("system/info") or {}
        log.info(
            f"[{self.device_id}] Connected to NETIO {info.get('brandName') or device.get('devtype', '?')} "
            f"{info.get('deviceName', '')} at {self._where()} over the web login "
            f"(firmware {_firmware_text(device.get('fwVersion')) or '?'})"
        )

    async def _web_sign_in(self, public_key: str, stamp: int) -> None:
        user = self._username
        token = web_login_token(user, self._password, public_key, stamp)
        self.redact_in_log(token)
        reply = await self._web_call("AUTH", extra={"username": user, "token": token}, check=False)
        status = str(reply.get("status") or "OK")
        if status == "OK":
            return
        error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
        code = str(error.get("code") or status)
        if code == "TooManyFailLogin":
            raise ConnectionFaultError(
                f"The NETIO is refusing sign-ins for now, after too many failed ones ({code}).",
                code="auth_failed",
            )
        if code in WEB_AUTH_REFUSALS:
            raise ConnectionFaultError(
                f"The NETIO refused the web login for {user} ({code}).", code="auth_failed",
            )
        raise ConnectionError(f"The NETIO answered the web login's sign-in with {code}.")

    async def _web_snapshot(self, *, connecting: bool = False, configs: bool | None = None) -> dict[str, Any]:
        """Everything the driver reads over the web login.

        outputs/measure is the core read (it carries every output's on/off).
        Refused at connect, the login cannot run this unit; refused later, or
        any other read refused, it is named in last_error. Each output's own
        settings (its short on/off time) are read at connect and on a refresh,
        not every poll.
        """
        refusals: list[str] = []
        snap: dict[str, Any] = {}
        try:
            snap["outputs/measure"] = await self._web_read("outputs/measure", hold=True)
        except NetioWebError as exc:
            if connecting and exc.code == "Unauthorized":
                raise ConnectionFaultError(
                    f"The NETIO refused this login access to its outputs ({exc.code}).",
                    code="auth_failed",
                ) from exc
            if connecting:
                raise ConnectionFaultError(
                    f"The NETIO at {self._where()} did not report its outputs over the web login "
                    f"({exc.code}). Set Connect With to JSON API.",
                    code="invalid_config",
                ) from exc
            refusals.append(f"outputs/measure ({exc.code})")
            snap["outputs/measure"] = None
        held = [topic for topic in WEB_HELD_TOPICS if topic != "outputs/measure"]
        if self._web_reads_with_get():
            held.append(WEB_STATE_TOPIC)
        for topic in held:
            snap[topic] = await self._web_optional(topic, refusals, hold=True)
        for topic in ("system/info", "system/uptime", "system/datetime", *WEB_LISTS.values()):
            snap[topic] = await self._web_optional(topic, refusals)
        snap["configs"] = {}
        if connecting if configs is None else configs:
            listed = _items(snap.get("outputs/list")) or _items(snap.get("outputs/measure"))
            for local_id in web_numbered(listed, MAX_OUTPUTS, lambda item: item):
                snap["configs"][local_id] = await self._web_optional(
                    f"outputs/id/{local_id}/config", refusals,
                )
        snap["refusals"] = refusals
        return snap

    def _apply_web(self, snap: dict[str, Any]) -> None:
        info = snap.get("system/info")
        if isinstance(info, dict):
            self._web_info = info
        measure_items = _items(snap.get("outputs/measure"))
        mask = web_measure_mask(self._web_info)
        if mask is not None:
            metered = mask != 0
        else:
            metered = any(
                isinstance(item, dict) and isinstance(item.get("measure"), dict)
                for item in measure_items or []
            )
        self._web_metered = metered
        device = web_identity(self._web_info, self._hello_device)
        # Reported by the JSON API only; cleared so a unit switched from it
        # does not keep stale values.
        device["mac_address"] = None
        device["json_version"] = None
        uptime = snap.get("system/uptime")
        if isinstance(uptime, dict):
            device["uptime_s"] = _as_int(uptime.get("uptime"))
        clock = snap.get("system/datetime")
        if isinstance(clock, dict):
            device["device_time"] = _timestamp(clock.get("time"))
        glob = snap.get("outputs/globalmeasure")
        if glob is not None or not metered:
            device.update(web_global(glob.get("measure") if isinstance(glob, dict) else None, metered))
        device.update(self._certificate_states())

        listed = web_numbered(_items(snap.get("outputs/list")), MAX_OUTPUTS, web_output_listed)
        measured = web_numbered(
            measure_items, MAX_OUTPUTS,
            lambda item: {**web_output_live(item), **web_output_metering(item)},
        )
        states = web_numbered(_items(snap.get(WEB_STATE_TOPIC)), MAX_OUTPUTS, web_output_live)
        configs = snap.get("configs") or {}
        outputs: dict[int, dict[str, Any]] = {}
        for local_id in sorted(set(listed) | set(measured)):
            values: dict[str, Any] = {}
            values.update(listed.get(local_id, {}))
            values.update(measured.get(local_id, {}))
            values.update(states.get(local_id, {}))
            values.update(web_output_config(configs.get(local_id)))
            outputs[local_id] = values
        inputs = web_numbered(_items(snap.get("inputs/list")), MAX_INPUTS, web_input)
        if snap.get("outputs/list") is not None or measure_items is not None:
            device.setdefault("output_count", len(outputs))
        if snap.get("inputs/list") is not None:
            device.setdefault("input_count", len(inputs))
        self.set_states(device)

        # A roster is changed only from a list the unit answered.
        if snap.get("outputs/list") is not None or measure_items is not None:
            self._sync_children("output", outputs)
            self._web_live = {
                local_id: {k: v for k, v in values.items() if k in ("state", "schedule_enabled")}
                for local_id, values in outputs.items()
            }
        if snap.get("inputs/list") is not None:
            self._sync_children("input", inputs)
        for kind, topic in WEB_LISTS.items():
            if snap.get(topic) is not None:
                self._sync_children(kind, web_named(_items(snap[topic]), WEB_BLANK[kind]))

        # Metering was just written from the reads; pushed metering resumes
        # one interval from now.
        now = time.monotonic()
        interval = self._web_meter_interval()
        for topic in ("outputs/measure", "outputs/globalmeasure"):
            self._web_meter_next[topic] = now + interval
        refusals = snap.get("refusals") or []
        if refusals:
            self.set_state(
                "last_error",
                "The NETIO refused this login some of its status: " + ", ".join(refusals) + ".",
            )

    # ── Web login: pushed events ──

    def _web_meter_interval(self) -> float:
        interval = _as_float(self.config.get("poll_interval"))
        return interval if interval and interval > 0 else 10.0

    def _web_meter_due(self, topic: str) -> bool:
        now = time.monotonic()
        if now < self._web_meter_next.get(topic, 0.0):
            return False
        self._web_meter_next[topic] = now + self._web_meter_interval()
        return True

    def _web_live_update(self, local_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """The on/off and schedule values that differ from what was last written."""
        live = self._web_live.setdefault(local_id, {})
        changed = {k: v for k, v in values.items() if live.get(k) != v}
        live.update(values)
        return changed

    def _on_web_event(self, topic: str, data: Any) -> None:
        items = _items(data)
        roster = set(self._rosters["output"])
        if topic in ("outputs/measure", WEB_STATE_TOPIC):
            due = topic == "outputs/measure" and self._web_meter_due(topic)
            for local_id, item in web_numbered(items, MAX_OUTPUTS, lambda i: i).items():
                if local_id not in roster:
                    continue
                changed = self._web_live_update(local_id, web_output_live(item))
                if due:
                    changed.update(web_output_metering(item))
                if changed:
                    self.set_child_state_batch("output", local_id, changed)
        elif topic == "outputs/globalmeasure":
            if self._web_metered and self._web_meter_due(topic) and isinstance(data, dict):
                self.set_states(web_global(data.get("measure"), True))
        elif topic == "outputs/list":
            for local_id, values in web_numbered(items, MAX_OUTPUTS, web_output_listed).items():
                if local_id in roster:
                    self.set_child_state_batch("output", local_id, values)
        elif topic == "inputs/list":
            inputs = set(self._rosters["input"])
            for local_id, values in web_numbered(items, MAX_INPUTS, web_input).items():
                if local_id in inputs:
                    self.set_child_state_batch("input", local_id, values)

    # ── Web login: liveness, poll ──

    async def _web_probe(self) -> None:
        """PING from 5.2; before it, a read of the uptime. Any answer, an error
        included, is a live unit. While a file is going to it, every chunk
        answered is that answer."""
        if self._sending_files:
            return
        if self._fw is None or self._fw >= WEB_PING_SINCE:
            await self._web_call("PING", check=False)
            return
        await self._web_call("SUBSCRIBE", "system/uptime", check=False)
        await self._web_call("UNSUBSCRIBE", "system/uptime", check=False)

    async def _web_poll(self) -> None:
        if self._ws is None:
            return
        if self._fw_update is not None:
            # Updating: the unit is left to it between restarts, as NETIO's
            # library leaves it, and read in full when it reconnects. Outputs
            # switching are still pushed.
            self._fw_follow({})
            return
        if self._sending_files:
            # A PowerPDU 4KB taking firmware chunks left a poll's read
            # unanswered (2026-10-21), so the unit is left to the file; outputs
            # switching are still pushed.
            return
        snapshot = await self._web_snapshot()
        self._apply_web(snapshot)
        self._fw_follow(snapshot)

    # ── State ──

    def _apply(self, doc: dict[str, Any]) -> None:
        summary = summarize(doc)
        # The web login reports the HTTPS certificate; blank over the JSON API.
        self.set_states({**summary["device"], **self._certificate_states()})
        for child_type in CHILD_TYPES:
            self._sync_children(child_type, summary[child_type])

    def _apply_write_reply(self, doc: dict[str, Any]) -> None:
        """Take what a write changes from its answer, and nothing else.

        The answer to a switch is a shorter status than a read (the 4KS demo on
        5.3.2): each output leaves out its own voltage, frequency,
        not-resettable counters and EnergyStart, and reports power factor 0
        where a read of the same idle output says 1.0. So only each output's
        name, on/off and short on/off time are taken from it; metering comes
        from the next read.
        """
        for local_id, values in numbered(doc.get("Outputs"), MAX_OUTPUTS, parse_output).items():
            if local_id in self._rosters["output"]:
                self.set_child_state_batch(
                    "output", local_id,
                    {key: values[key] for key in ("name", "state", "delay_ms")},
                )

    def _sync_children(self, child_type: str, entries: dict[Any, dict[str, Any]]) -> None:
        """The unit is the authority on which children exist: a new one is
        registered, one it no longer reports is removed."""
        known = set(self._rosters[child_type])
        for local_id, values in entries.items():
            if local_id in known:
                self.set_child_state_batch(child_type, local_id, values)
            else:
                self.register_child(child_type, local_id, initial_state=values)
        for local_id in known - set(entries):
            self.deregister_child(child_type, local_id)
        self._rosters[child_type] = list(entries)

    # ── Polling ──

    async def poll(self) -> None:
        """One status read. A refused reading login drops the connection; any
        other refusal leaves the unit connected and named in last_error, written
        again by every poll while it holds."""
        if self._web:
            await self._web_poll()
            return
        if self._client is None:
            return
        try:
            doc = await self._read()
        except NetioHTTPError as exc:
            if exc.status == 401:
                raise self._read_refused() from exc
            if exc.status == 404:
                self.set_state("last_error", self._api_off())
            elif exc.status == 403:
                self.set_state("last_error", "The NETIO refused to share its status (HTTP 403): "
                                             "reading is off on its JSON API tab.")
            else:
                self.set_state("last_error", f"The NETIO answered the status read with {exc}.")
            return
        except NetioReplyError as exc:
            self.set_state("last_error", f"The NETIO's status could not be read: {exc}.")
            return
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc
        self._apply(doc)

    async def refresh_children(self) -> Any:
        if self._web:
            if self._ws is None:
                raise ConnectionError(f"[{self.device_id}] Not connected")
            self._apply_web(await self._web_snapshot(configs=True))
            return {t: list(ids) for t, ids in self._rosters.items()}
        try:
            doc = await self._read()
        except (NetioHTTPError, NetioReplyError) as exc:
            raise NetioCommandError(f"The NETIO's status could not be read: {exc}") from exc
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc
        self._apply(doc)
        return {t: list(ids) for t, ids in self._rosters.items()}

    # ── Web login: online firmware update ──

    def _fw_running(self) -> str:
        return _firmware_text(self._hello_device.get("fwVersion")) or "an unknown version"

    def _need_web(self, what: str) -> None:
        """Refuse a command the JSON API cannot carry, and any while a firmware
        update is under way."""
        if not self._web:
            raise NetioCommandError(
                f"{what} needs the web login: set Connect With to Web login, with a user that "
                f"may change the NETIO's settings."
            )
        pending = self._fw_update
        if pending is not None:
            raise NetioCommandError(f"The NETIO is {pending['doing']}.")

    def _fw_need_web(self) -> None:
        self._need_web("Checking for and installing firmware updates")

    async def _fw_lock(self) -> str:
        data = await self._web_read("system/lock")
        return str(data.get("op") or "") if isinstance(data, dict) else ""

    async def _fw_list(self) -> Any:
        return _items(await self._web_read("fwupdate/list"))

    async def _fw_check(self, *, reuse: bool) -> list[dict[str, str]]:
        """Have the unit ask NETIO's update server for newer firmware, wait
        for the answer, and write it to state. ``reuse`` takes an answer from
        the last FW_LIST_REUSE_S instead of asking again."""
        if reuse and self._fw_offers is not None and time.monotonic() - self._fw_offers_at < FW_LIST_REUSE_S:
            return self._fw_offers
        # The first check after a PowerPDU 4KB started got nothing from the
        # update server in two of three tries (2026-10-21; it held the lock
        # about 7 s and left the list empty), and the next check always got the
        # answer. So a check that ends with no answer at all asks once more.
        for attempt in range(1, FW_CHECK_ATTEMPTS + 1):
            try:
                before = await self._fw_list()
                await self._web_call(
                    "SET", "system/firmwarelist", {"getFirmwareList": True, "updateFw": ""},
                )
                items = await self._fw_wait_for_answer(before)
                break
            except NetioFirmwareNoAnswer:
                if attempt == FW_CHECK_ATTEMPTS:
                    raise
                log.info(
                    f"[{self.device_id}] NETIO's update server sent the NETIO no answer; asking again"
                )
            except NetioWebError as exc:
                raise self._web_refused(exc, "check for a firmware update") from exc
        offers = firmware_offers(items)
        self._fw_offers, self._fw_offers_at = offers, time.monotonic()
        self.set_states({
            "firmware_update_available": bool(offers),
            "firmware_update_version": offers[0]["version"] if offers else "",
        })
        return offers

    async def _fw_wait_for_answer(self, before: Any) -> Any:
        """fwupdate/list once it holds the update server's answer.

        The unit rebuilds the list shortly after it unlocks, so a read
        around the unlock can get the old list or part of the new one. A list
        that differs from ``before`` counts once two reads in a row agree; one
        that does not differ counts once it stayed so for FW_LIST_SETTLE_S
        after the unlock (PyNetioConf).
        """
        deadline = time.monotonic() + FW_LIST_TIMEOUT_S
        locked = False
        unlocked_at: float | None = None
        previous: Any = None
        while time.monotonic() < deadline:
            await asyncio.sleep(FW_LIST_INTERVAL_S)
            if await self._fw_lock() == "lock":
                locked = True
            elif locked and unlocked_at is None:
                unlocked_at = time.monotonic()
            items = await self._fw_list()
            answered = firmware_list_answered(items)
            if answered and items != before:
                if items == previous:
                    return items
                previous = items
                continue
            previous = None
            if unlocked_at is not None and time.monotonic() - unlocked_at >= FW_LIST_SETTLE_S:
                if answered:
                    return items
                raise NetioFirmwareNoAnswer(
                    "The NETIO asked NETIO's update server and got no answer. Check that the NETIO "
                    "can reach the internet, then check again."
                )
        raise NetioCommandError(
            f"The NETIO had no answer from NETIO's update server within {int(FW_LIST_TIMEOUT_S)} "
            f"seconds. Check that the NETIO can reach the internet, then check again."
        )

    async def _fw_install(self, full: bool) -> None:
        """Start the update to the newest firmware offered. It returns once
        the unit has started it; the unit then downloads and restarts, and
        _fw_follow takes it from there."""
        self._fw_need_web()
        offers = await self._fw_check(reuse=True)
        running = self._fw_running()
        if not offers:
            raise NetioCommandError(
                f"NETIO's update server offers nothing newer than the firmware this NETIO runs "
                f"({running}), so nothing was installed."
            )
        offer = offers[0]
        link = offer["full"] if full else offer["app"]
        if not link:
            raise NetioCommandError(
                f"NETIO's update server lists firmware {offer['version']} without a download for "
                f"this kind of update, so nothing was installed. Try the other Update Firmware "
                f"command."
            )
        try:
            uptime = await self._web_read("system/uptime")
            await self._web_call(
                "SET", "system/firmwarelist", {"getFirmwareList": False, "updateFw": link},
            )
            await self._fw_wait_for_start()
        except NetioWebError as exc:
            raise self._web_refused(exc, "update its firmware") from exc
        now = time.monotonic()
        seconds = _as_int(uptime.get("uptime")) if isinstance(uptime, dict) else None
        self._fw_update = {
            "doing": f"updating to firmware {offer['version']}",
            "target": offer["version"],
            "target_v": firmware_version(offer["version"]),
            "started": now,
            # When the unit last started, to tell its restarts apart from a
            # dropped connection.
            "booted": now - (seconds or 0),
            "restarts": 0,
        }
        # They offered an update to the firmware being replaced.
        self._fw_offers = None
        self.set_state("firmware_update_status", f"Updating to {offer['version']}")
        log.info(
            f"[{self.device_id}] Updating the NETIO from firmware {running} to {offer['version']}, "
            f"{'with' if full else 'without'} the outlet controller's"
        )

    async def _fw_wait_for_start(self) -> None:
        """The unit locks system/lock once it starts the update. One that is
        gone before it does has started it and is restarting."""
        deadline = time.monotonic() + FW_START_TIMEOUT_S
        try:
            while await self._fw_lock() != "lock":
                if time.monotonic() >= deadline:
                    raise NetioCommandError("The NETIO did not start the firmware update.")
                await asyncio.sleep(FW_LIST_INTERVAL_S)
        except ConnectionError:
            return

    def _fw_follow(self, snap: dict[str, Any]) -> None:
        """How the update in progress is going, on every connect and poll.
        Updated once the unit runs the new firmware. Back on the old one, it
        restarted after a failed attempt: failed after the last attempt, else
        watched for the next one. Failed if it has not finished
        FW_UPDATE_TIMEOUT_S after it started."""
        pending = self._fw_update
        if pending is None:
            return
        if "file" in pending:
            self._fw_follow_file(pending, snap)
            return
        target = pending["target"]
        running = self._fw_running()
        target_v = pending["target_v"]
        if (self._fw is not None and target_v is not None and self._fw >= target_v) or running == target:
            self._fw_finish()
            self.set_states({
                "firmware_update_status": f"Updated to {target}",
                "firmware_update_available": False,
                "firmware_update_version": "",
            })
            log.info(f"[{self.device_id}] The NETIO runs firmware {target} now")
            return
        now = time.monotonic()
        uptime = snap.get("system/uptime")
        seconds = _as_int(uptime.get("uptime")) if isinstance(uptime, dict) else None
        if seconds is not None and now - seconds - pending["booted"] > FW_RESTART_SLACK_S:
            pending["booted"] = now - seconds
            pending["restarts"] += 1
            if pending["restarts"] >= FW_ATTEMPTS:
                self._fw_failed(
                    f"The update to {target} failed: the NETIO restarted on firmware {running} "
                    f"after {pending['restarts']} attempts."
                )
                return
            self._fw_stop_watch_now()
            self._fw_retry_watch = asyncio.create_task(self._fw_watch_retry(pending))
            return
        if now - pending["started"] > FW_UPDATE_TIMEOUT_S:
            self._fw_failed(
                f"The update to {target} did not finish: the NETIO still runs firmware {running} "
                f"{int(FW_UPDATE_TIMEOUT_S // 60)} minutes after it started."
            )

    async def _fw_watch_retry(self, pending: dict[str, Any]) -> None:
        """After a failed attempt, whether the unit starts the next one within
        FW_RETRY_WAIT_S."""
        attempt = pending["restarts"] + 1
        deadline = time.monotonic() + FW_RETRY_WAIT_S
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(FW_RETRY_INTERVAL_S)
                if self._fw_update is not pending:
                    return
                if await self._fw_lock() == "lock":
                    self.set_state(
                        "firmware_update_status",
                        f"Updating to {pending['target']}: attempt {attempt} of {FW_ATTEMPTS}",
                    )
                    return
        except (ConnectionError, NetioWebError):
            # Gone again, so restarting: the next connect follows it.
            return
        if self._fw_update is pending:
            self._fw_failed(
                f"The update to {pending['target']} failed: the NETIO restarted on firmware "
                f"{self._fw_running()} and did not try again."
            )

    def _fw_follow_file(self, pending: dict[str, Any], snap: dict[str, Any]) -> None:
        """A firmware file's install: done once the unit has restarted (its
        boot time moved), with the firmware it then runs. A package with the
        outlet controller's firmware restarts it a second time, inside the
        command's restart window. Failed if it has not restarted
        FW_UPDATE_TIMEOUT_S after the file went in."""
        now = time.monotonic()
        uptime = snap.get("system/uptime")
        seconds = _as_int(uptime.get("uptime")) if isinstance(uptime, dict) else None
        if seconds is not None and now - seconds - pending["booted"] > FW_RESTART_SLACK_S:
            self._fw_finish()
            # The last check's answer was about the firmware just replaced.
            self.set_states({
                "firmware_update_status": (
                    f"Installed the firmware in {pending['file']}: the NETIO runs "
                    f"{self._fw_running()}"
                ),
                "firmware_update_available": None,
                "firmware_update_version": None,
            })
            log.info(f"[{self.device_id}] The NETIO runs firmware {self._fw_running()} now")
            return
        if now - pending["started"] > FW_UPDATE_TIMEOUT_S:
            self._fw_failed(
                f"The NETIO took the firmware in {pending['file']} but had not restarted to "
                f"install it {int(FW_UPDATE_TIMEOUT_S // 60)} minutes later."
            )

    def _fw_finish(self) -> None:
        self._fw_update = None
        self._fw_stop_watch_now()

    def _fw_failed(self, message: str) -> None:
        self._fw_finish()
        self.set_states({"firmware_update_status": message, "last_error": message})
        log.warning(f"[{self.device_id}] {message}")

    def _fw_stop_watch_now(self) -> None:
        watch, self._fw_retry_watch = self._fw_retry_watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()

    async def _fw_stop_retry_watch(self) -> None:
        watch, self._fw_retry_watch = self._fw_retry_watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
            await asyncio.wait({watch})

    # ── Web login: files and restarts ──

    def _chunked(self) -> bool:
        """Whether files go to the unit in chunks over the web login (API 2 and
        later) rather than by HTTP POST."""
        return self._api_major is not None and self._api_major >= WEB_CHUNKED_API

    def _dropped(self) -> bool:
        return self._ws is None or not _ws_open(self._ws)

    def _https_post_refused(self, what: str) -> None:
        """Firmware 5.1.3 cannot import a configuration or take firmware over
        HTTPS (NETIO, 2026-10-14), the two its web interface sends by POST."""
        if self._scheme == "https" and not self._chunked():
            raise NetioCommandError(
                f"This NETIO, on firmware {self._fw_running()}, takes {what} over HTTP only. Turn "
                f"HTTPS off in its web interface first, or update its firmware to 5.2.0 or later."
            )

    async def _web_upload(
        self, topic: str, file: Any, what: str, *, data: bytes | None = None, restarts: bool = False,
    ) -> None:
        """Send ``file`` (the command's CommandFile) to the unit on ``topic``,
        the way its own web interface does. ``data`` is the file's bytes when
        they were read already. ``restarts``: the unit restarts once the file
        is in, so a connection gone at the last chunk is that restart."""
        try:
            reply = await self._web_call("SET", topic, {})
        except NetioWebError as exc:
            raise self._web_refused(exc, what) from exc
        opened = reply.get("data") if isinstance(reply.get("data"), dict) else {}
        self._sending_files += 1
        try:
            if self._chunked():
                upload_id = opened.get("uploadId")
                if not upload_id:
                    raise NetioCommandError(f"The NETIO did not open an upload to {what}.")
                pieces = _pieces(data) if data is not None else file.chunks(UPLOAD_CHUNK_BYTES)
                await self._web_send_chunks(str(upload_id), pieces, file.size, what, restarts=restarts)
                return
            path = opened.get("uploadPath")
            if not path:
                raise NetioCommandError(f"The NETIO did not open an upload to {what}.")
            body = data if data is not None else await file.read()
            await self._http_upload(str(path), file.name, body, what)
        finally:
            self._sending_files -= 1

    async def _web_send_chunks(
        self, upload_id: str, pieces: Any, total: int, what: str, *, restarts: bool,
    ) -> None:
        """Each chunk answered before the next; a chunk refused or unanswered
        is sent again, UPLOAD_CHUNK_TRIES times in all, as the unit's web
        interface does."""
        count = max(1, math.ceil(total / UPLOAD_CHUNK_BYTES))
        index = offset = 0
        async for piece in pieces:
            last = index == count - 1
            chunk = {
                "uploadId": upload_id,
                "chunkIndex": index,
                "chunksTotal": count,
                "bytesFrom": offset,
                "bytesTo": offset + len(piece),
                "bytesTotal": total,
                "b64Data": base64.b64encode(piece).decode("ascii"),
                "complete": last,
            }
            for attempt in range(1, UPLOAD_CHUNK_TRIES + 1):
                try:
                    await self._web_call("SET", "upload/chunk", chunk, timeout=UPLOAD_CHUNK_TIMEOUT_S)
                    break
                except NetioWebError as exc:
                    if attempt == UPLOAD_CHUNK_TRIES:
                        raise self._web_refused(exc, what) from exc
                except ConnectionError:
                    if self._dropped():
                        if last and restarts:
                            return
                        raise
                    if attempt == UPLOAD_CHUNK_TRIES:
                        raise
            index += 1
            offset += len(piece)

    async def _http_upload(self, path: str, name: str, body: bytes, what: str) -> None:
        """Before API 2: the file as the multipart field ``file``, POSTed to
        the path the unit opened, on its own scheme and port. The path is the
        authority; the unit's web interface sends nothing else."""
        url = f"{self._scheme}://{self._where()}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(
                verify=bool(_as_bool(self.config.get("verify_ssl"))),
                timeout=httpx.Timeout(UPLOAD_POST_TIMEOUT_S, connect=HTTPS_CONNECT_S),
                follow_redirects=False,
            ) as client:
                resp = await client.post(url, files={"file": (name, body)})
        except httpx.TransportError as exc:
            raise self._not_responding(exc) from exc
        if 200 <= resp.status_code < 300:
            return
        try:
            error = resp.json()
        except ValueError:
            error = None
        error = error if isinstance(error, dict) else {}
        code = str(error.get("code") or f"HTTP {resp.status_code}")
        detail = str(error.get("message") or "").strip()
        message = f"The NETIO refused to {what} ({code}{': ' + detail if detail else ''})."
        self.set_state("last_error", message)
        raise NetioCommandError(message)

    async def _web_restart(self) -> None:
        """Restart the unit: system/reset from 5.2.0; on 5.1.x its web server
        settings written back as they are. A connection gone before the
        answer is the restart under way."""
        try:
            if self._fw is not None and self._fw < WEB_RESET_SINCE:
                current = await self._web_read(WEB_SERVERS_TOPIC)
                if not isinstance(current, dict):
                    raise NetioCommandError("The NETIO did not report its web server settings.")
                keys = ("httpEnable", "httpPort", "httpsEnable", "httpsPort")
                await self._web_call("SET", WEB_SERVERS_TOPIC, {k: current.get(k) for k in keys})
            else:
                await self._web_call("SET", "system/reset", {"reset": True})
        except NetioWebError as exc:
            raise self._web_refused(exc, "restart") from exc
        except ConnectionError:
            if not self._dropped():
                raise

    async def _web_export(self) -> bytes:
        """The unit's settings, as its web interface's Export Config saves them."""
        if not self._chunked():
            reply = await self._web_call(
                "SET", EXPORT_LEGACY_TOPIC, extra={"data": None}, timeout=EXPORT_TIMEOUT_S,
            )
            data = reply.get("data") if isinstance(reply.get("data"), dict) else {}
            config = data.get("config")
            if config is None:
                raise NetioCommandError("The NETIO sent back no configuration.")
            text = config if isinstance(config, str) else json.dumps(config, indent=2, ensure_ascii=False)
            return text.encode("utf-8")
        sections: dict[str, Any] = {}
        for section in EXPORT_SECTIONS:
            reply = await self._web_call(
                "SET", f"export/config/{section}", {}, timeout=EXPORT_TIMEOUT_S,
            )
            sections[section] = reply.get("data")
        return json.dumps(sections, indent=2, ensure_ascii=False).encode("utf-8")

    # ── Commands ──

    def _output_id(self, params: dict[str, Any]) -> int:
        output = _as_int(params.get("output"))
        roster = self._rosters["output"]
        if output is None:
            raise NetioCommandError("No output was given.")
        if roster and output not in roster:
            raise NetioCommandError(
                f"This NETIO has no output {output}; its outputs are {min(roster)} to {max(roster)}."
            )
        return output

    @staticmethod
    def _delay_ms(params: dict[str, Any]) -> int | None:
        seconds = _as_float(params.get("seconds"))
        if seconds is None:
            return None
        if seconds <= 0:
            raise NetioCommandError("The time must be more than 0 seconds.")
        return int(round(seconds * 1000))

    def _web_expected(self, local_id: int, action: int) -> bool | None:
        """The state an output reads right after an action (a short off reads
        off until its time is up)."""
        if action in (ACTION_ON, ACTION_SHORT_ON):
            return True
        if action in (ACTION_OFF, ACTION_SHORT_OFF):
            return False
        if action == ACTION_TOGGLE:
            current = self._web_live.get(local_id, {}).get("state")
            return None if current is None else not current
        return None

    async def _web_switch(self, outputs: list[int], action: int) -> None:
        """outputs/id/<n>/ctrl for each output, then a wait (bounded) for the
        unit to push the state it caused, so it reads back as the command
        returns. The reply to a ctrl request is a bare OK."""
        expected = {n: self._web_expected(n, action) for n in outputs}
        for local_id in outputs:
            try:
                await self._web_call(
                    "SET", f"outputs/id/{local_id}/ctrl", {"request": WEB_REQUESTS[action]},
                )
            except NetioWebError as exc:
                raise self._web_refused(exc, f"switch output {local_id}") from exc
        await self._web_confirm({n: want for n, want in expected.items() if want is not None})

    async def _web_confirm(self, outputs: dict[int, bool]) -> None:
        if not outputs:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WEB_CONFIRM_S
        while loop.time() < deadline:
            if all(self._web_live.get(n, {}).get("state") == want for n, want in outputs.items()):
                return
            await asyncio.sleep(0.05)

    async def _web_output_settings(
        self, local_id: int, changes: dict[str, Any], what: str, *, needs_schedule: bool = False,
    ) -> None:
        """Change an output's own settings: read them, write them all back
        with the changes (PyNetioConf sends all six), read them again.

        ``needs_schedule`` refuses the change for an output with no schedule
        assigned: the unit accepts its schedule turned on with none, and
        nothing then switches it.
        """
        topic = f"outputs/id/{local_id}/config"
        try:
            current = await self._web_read(topic)
            if not isinstance(current, dict) or not current:
                raise NetioCommandError(
                    f"The NETIO sent no settings for output {local_id}, so nothing changed."
                )
            if needs_schedule and not _as_int(current.get("scheduleId")):
                message = self._no_schedule(local_id)
                self.set_state("last_error", message)
                raise NetioCommandError(message)
            body = {key: current[key] for key in WEB_OUTPUT_CONFIG_KEYS if key in current}
            body.update(changes)
            await self._web_call("SET", topic, body)
            after = await self._web_read(topic)
        except NetioWebError as exc:
            raise self._web_refused(exc, what) from exc
        values = web_output_config(after)
        if isinstance(after, dict) and after.get("name") is not None:
            values["name"] = str(after["name"])
        if "schedule_enabled" in values:
            self._web_live.setdefault(local_id, {})["schedule_enabled"] = values["schedule_enabled"]
        self.set_child_state_batch("output", local_id, values)

    @staticmethod
    def _no_schedule(local_id: int) -> str:
        return (
            f"Output {local_id} has no schedule. Assign one to it in the NETIO's web interface "
            f"first."
        )

    async def _set_action(self, params: dict[str, Any], action: int) -> None:
        local_id = self._output_id(params)
        schedule = action in (ACTION_SCHEDULE_ON, ACTION_SCHEDULE_OFF)
        if self._web:
            if schedule:
                on = action == ACTION_SCHEDULE_ON
                await self._web_output_settings(
                    local_id, {"isScheduleActive": on},
                    "turn the output's schedule " + ("on" if on else "off"),
                    needs_schedule=on,
                )
                return
            await self._web_switch([local_id], action)
            return
        # The JSON API answers a schedule action for an output with no schedule
        # assigned with HTTP 400 (a PowerPDU 4KB on 5.2.4 and 5.3.2; with its
        # "Always" schedule assigned, 200 and the schedule follows).
        await self._write(
            [{"ID": local_id, "Action": action}],
            bad_request=self._no_schedule(local_id) if schedule else "",
        )

    async def _short(self, params: dict[str, Any], action: int) -> None:
        local_id = self._output_id(params)
        delay = self._delay_ms(params)
        if self._web:
            if delay is not None:
                raise NetioCommandError(
                    "With the web login, a NETIO uses the output's own short on/off time, set in "
                    "its web interface. Leave the time blank, or set Connect With to JSON API to "
                    "give a time with the command."
                )
            await self._web_switch([local_id], action)
            return
        entry: dict[str, Any] = {"ID": local_id, "Action": action}
        if delay is not None:
            entry["Delay"] = delay
        await self._write([entry], end_session=delay is not None)

    async def _all(self, action: int) -> None:
        outputs = self._rosters["output"]
        if not outputs:
            raise NetioCommandError("The NETIO has not reported its outputs yet.")
        if self._web:
            await self._web_switch(list(outputs), action)
            return
        await self._write([{"ID": output, "Action": action} for output in outputs])

    async def _cmd_refresh(self, params: dict[str, Any]) -> None:
        await self.poll()

    async def _cmd_output_on(self, params: dict[str, Any]) -> None:
        await self._set_action(params, ACTION_ON)

    async def _cmd_output_off(self, params: dict[str, Any]) -> None:
        await self._set_action(params, ACTION_OFF)

    async def _cmd_output_toggle(self, params: dict[str, Any]) -> None:
        await self._set_action(params, ACTION_TOGGLE)

    async def _cmd_output_reboot(self, params: dict[str, Any]) -> None:
        await self._short(params, ACTION_SHORT_OFF)

    async def _cmd_output_on_for(self, params: dict[str, Any]) -> None:
        await self._short(params, ACTION_SHORT_ON)

    async def _cmd_all_outputs_on(self, params: dict[str, Any]) -> None:
        await self._all(ACTION_ON)

    async def _cmd_all_outputs_off(self, params: dict[str, Any]) -> None:
        await self._all(ACTION_OFF)

    async def _cmd_all_outputs_reboot(self, params: dict[str, Any]) -> None:
        await self._all(ACTION_SHORT_OFF)

    async def _cmd_rename_output(self, params: dict[str, Any]) -> None:
        name = str(params.get("name", "") or "").strip()
        if not name:
            raise NetioCommandError("The output's name cannot be blank.")
        local_id = self._output_id(params)
        if self._web:
            await self._web_output_settings(local_id, {"name": name}, "rename the output")
            return
        await self._write([{"ID": local_id, "Name": name, "Action": ACTION_NO_CHANGE}])

    async def _cmd_reset_output_energy(self, params: dict[str, Any]) -> None:
        local_id = self._output_id(params)
        if self._web:
            try:
                await self._web_call(
                    "SET", f"outputs/id/{local_id}/resetOutputConsumption", {"request": True},
                )
            except NetioWebError as exc:
                raise self._web_refused(exc, "reset the output's energy counter") from exc
            # The next pushed metering is written at once.
            self._web_meter_next.pop("outputs/measure", None)
            return
        # The document spells the key two ways (resetEnergyCounter in its list
        # of commands, ResetEnergyCounter in its full example). The 4KS demo
        # took ResetEnergyCounter: the output's EnergyStart moved to the time
        # of the write (2026-10-13). A read follows, since the write's answer
        # is not where metering is taken from.
        await self._write([
            {"ID": local_id, "ResetEnergyCounter": True, "Action": ACTION_NO_CHANGE}
        ])
        await self.poll()

    async def _cmd_enable_output_schedule(self, params: dict[str, Any]) -> None:
        await self._set_action(params, ACTION_SCHEDULE_ON)

    async def _cmd_disable_output_schedule(self, params: dict[str, Any]) -> None:
        await self._set_action(params, ACTION_SCHEDULE_OFF)

    async def _cmd_check_firmware_update(self, params: dict[str, Any]) -> None:
        self._fw_need_web()
        await self._fw_check(reuse=False)

    async def _cmd_update_firmware_full(self, params: dict[str, Any]) -> None:
        await self._fw_install(full=True)

    async def _cmd_update_firmware_keep_outlets(self, params: dict[str, Any]) -> None:
        await self._fw_install(full=False)

    async def _cmd_restart_device(self, params: dict[str, Any]) -> None:
        self._need_web("Restarting the NETIO")
        await self._web_restart()
        log.info(f"[{self.device_id}] Restarting the NETIO")

    async def _cmd_upload_https_certificate(self, params: dict[str, Any]) -> None:
        self._need_web("Uploading an HTTPS certificate")
        key_file, cert_file = params["private_key"], params["certificate"]
        key, cert = await key_file.read(), await cert_file.read()
        problem = certificate_pair_problem(key, cert, key_file.name, cert_file.name)
        if problem:
            raise NetioCommandError(problem)
        await self._web_upload(UPLOAD_KEY_TOPIC, key_file, "take the private key", data=key)
        await self._web_upload(UPLOAD_CERT_TOPIC, cert_file, "take the certificate", data=cert)
        # The unit serves a new certificate only once it has restarted.
        await self._web_restart()
        log.info(
            f"[{self.device_id}] Installed the HTTPS certificate {cert_file.name}; restarting the NETIO"
        )

    async def _cmd_update_firmware_from_file(self, params: dict[str, Any]) -> None:
        self._need_web("Installing firmware from a file")
        file = params["firmware"]
        self._https_post_refused("firmware")
        try:
            uptime = await self._web_read("system/uptime")
        except NetioWebError as exc:
            raise self._web_refused(exc, "update its firmware") from exc
        await self._web_upload(UPLOAD_FIRMWARE_TOPIC, file, "take the firmware", restarts=True)
        now = time.monotonic()
        seconds = _as_int(uptime.get("uptime")) if isinstance(uptime, dict) else None
        self._fw_update = {
            "doing": f"installing the firmware in {file.name}",
            "file": file.name,
            "started": now,
            # When the unit last started, to tell its restart apart from a
            # dropped connection.
            "booted": now - (seconds or 0),
        }
        self._fw_offers = None
        self.set_state("firmware_update_status", f"Installing the firmware in {file.name}")
        log.info(
            f"[{self.device_id}] Installing the firmware in {file.name} on the NETIO "
            f"(now {self._fw_running()})"
        )

    async def _cmd_back_up_configuration(self, params: dict[str, Any]) -> Any:
        self._need_web("Backing up the configuration")
        try:
            data = await self._web_export()
        except NetioWebError as exc:
            raise self._web_refused(exc, "export its configuration") from exc
        return DeviceFile(
            backup_name(self.get_state("device_name"), datetime.now()), data, "application/json",
        )

    async def _cmd_restore_configuration(self, params: dict[str, Any]) -> None:
        self._need_web("Restoring a configuration")
        file = params["configuration"]
        self._https_post_refused("a configuration file")
        data = await file.read()
        problem = configuration_problem(file.name, data, self._chunked())
        if problem:
            raise NetioCommandError(problem)
        await self._web_upload(
            UPLOAD_CONFIG_TOPIC, file, "import the configuration", data=data, restarts=True,
        )
        log.info(f"[{self.device_id}] Restored the configuration in {file.name}; the NETIO restarts")

    _DISPATCH = {
        "refresh": _cmd_refresh,
        "output_on": _cmd_output_on,
        "output_off": _cmd_output_off,
        "output_toggle": _cmd_output_toggle,
        "output_reboot": _cmd_output_reboot,
        "output_on_for": _cmd_output_on_for,
        "all_outputs_on": _cmd_all_outputs_on,
        "all_outputs_off": _cmd_all_outputs_off,
        "all_outputs_reboot": _cmd_all_outputs_reboot,
        "rename_output": _cmd_rename_output,
        "reset_output_energy": _cmd_reset_output_energy,
        "enable_output_schedule": _cmd_enable_output_schedule,
        "disable_output_schedule": _cmd_disable_output_schedule,
        "check_firmware_update": _cmd_check_firmware_update,
        "update_firmware_full": _cmd_update_firmware_full,
        "update_firmware_keep_outlets": _cmd_update_firmware_keep_outlets,
        "restart_device": _cmd_restart_device,
        "upload_https_certificate": _cmd_upload_https_certificate,
        "update_firmware_from_file": _cmd_update_firmware_from_file,
        "back_up_configuration": _cmd_back_up_configuration,
        "restore_configuration": _cmd_restore_configuration,
    }

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        handler = self._DISPATCH.get(command)
        if handler is None:
            raise ValueError(f"Unknown command: {command}")
        if (self._ws if self._web else self._client) is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        return await handler(self, params or {})


def _ws_open(ws: Any) -> bool:
    """True while a websockets connection is open (websockets 16 exposes
    ``state``; older releases a ``closed`` flag)."""
    state = getattr(ws, "state", None)
    if state is not None:
        return getattr(state, "name", "") == "OPEN"
    return not getattr(ws, "closed", False)
