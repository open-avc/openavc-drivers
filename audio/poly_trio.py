"""
OpenAVC Poly Trio Driver (Poly Voice Software REST API).

Controls Poly (HP) Trio C60 and Trio 8300 conference phones through the REST
API the phone serves on HTTPS: place, answer, decline, hold, resume, transfer
and end calls, send DTMF, mute and unmute, step the volume, press the phone's
keys, and read who is calling, the call in progress, the line's registration
and do-not-disturb, the display's power state, the phone's own status and its
identity.

What it does
------------
Calls: the phone's call list (``/api/v2/webCallControl/callStatus``) becomes
the room's call state: ``ringing`` while a call rings in, ``in_call`` while
any other call is up, and the caller or far end, direction, duration, line
and the phone's own word for the call's state, for the call that matters (the
one ringing in, else the active one, else the first). Answer, Decline,
Ignore, Hold, Resume, Transfer and Hang Up act on that call by the handle the
list gives it, so a panel button needs no parameter. Mute is the phone's
microphone mute (``communicationInfo`` ``PhoneMuteState``); volume has no
reading in the API, only the VolUp / VolDown keys.

Push vs poll
------------
Poll only. The REST API Reference Manual (July 2026) documents no
subscription, event stream or callback, and the Trio C60 Administrator Guide
9.3.0 documents no event notification either (the telephony event
notifications of the older UC Software phones are not in it). The call list
and mute are read every ``poll_interval`` (default 3 s, so a call ringing in
shows within about three seconds); the line, the display, the phone's status,
the transfer type and the identity every 30 s.

Why Python
----------
Hang Up, Answer, Decline, Ignore, Hold, Resume and Transfer name a call by the
handle the call list gives it, so each one reads the list first and then acts
on the call it picked, which a YAML command (a fixed body, no state in it)
cannot do. The call list is an array whose members become one set of room
values chosen by rule (the call ringing in, else the active one). Every reply
carries the phone's own result code in its body beside the HTTP status
(``"Status": "4007"`` is "call does not exist"), and that code decides whether
a command worked. The phone answers Digest or Basic sign-in depending on its
software, and the driver answers whichever challenge the phone sends.

Authentication and transport
----------------------------
The REST API's username is always ``Polycom``; the password is the phone's
admin password. The API is off until ``apps.restapi.enabled`` is 1, and the
phone refuses it (code 4010) while the admin password is still the default.
PVOS 9.2.0 and later turn Basic sign-in off and offer Digest (MD5 and
SHA-256); earlier PVOS takes Basic only. The driver sends the first request
unsigned, reads the phone's challenge, and keeps the scheme it asked for.
Session tokens (PVOS 9.2.0+) are not used: Digest on every request needs no
session, so none of the phone's ten session slots is taken.

The phone handles one request at a time and answers a second one sent while
it is busy with 403 (or queues it), so every request here goes through one
lock, and a 403 is read as "busy" (tried once more after a short wait), never
as a refused password.

Source:
    Poly UC Software and Poly Voice Software REST API Reference Manual
    (HP, document Q35596-001, July 2026):
    https://kaas.hpcloud.hp.com/pdf-public/pdf_14672705_en-US-1.pdf
"""

from __future__ import annotations

import asyncio
import copy
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

# The REST API's username, fixed by the manual ("The administrative username
# for all REST API requests is Polycom (case-sensitive)").
USERNAME = "Polycom"

# The phone's result codes (manual Table 5-1), as a person reads them.
RESULT_CODES = {
    "4000": "invalid input parameters",
    "4001": "the phone is busy",
    "4002": "the line is not registered",
    "4003": "the phone does not allow that now",
    "4004": "the phone does not support that",
    "4005": "the line does not exist",
    "4006": "no URL is configured for that",
    "4007": "there is no such call",
    "4008": "the configuration export failed",
    "4009": "the request is larger than the phone accepts",
    "4010": "the phone still has its default admin password",
    "4011": "the contact was not found",
    "4400": "bad request",
    "4403": "forbidden",
    "5000": "the phone failed to process the request",
    "5500": "internal error on the phone",
}

# How long to wait before sending a request again that the phone answered 403
# ("Parallel processing is not allowed ... the second request will receive an
# HTTP 403 error"). One more try, then the request is reported as refused.
BUSY_RETRY_S = 0.5

# The slower reads (line, display, status, transfer type, identity) run this
# often, whatever the poll interval.
DETAIL_INTERVAL_S = 30.0

# PowerStatus CurrentState -> display_state.
DISPLAY_STATES = {
    "poweron": "on",
    "poweridle": "idle",
    "poweroff": "off",
    "powerincall": "in_call",
}

TRANSFER_TYPES = {"consultative": "Consultative", "blind": "Blind", "safe": "Safe"}

# The keys SimulateKeyEvent takes, per phone (manual pages 46-48). The Trio C60
# takes three; the Trio 8300 the long list.
_KEYS_C60 = ["MicMute", "VolDown", "VolUp"]
_KEYS_8300 = (
    [f"SoftKey{n}" for n in range(1, 6)]
    + [f"Line{n}" for n in range(1, 13)]
    + ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Select", "Delete"]
    + [f"Dialpad{n}" for n in range(10)]
    + ["DialpadStar", "DialpadPound", "Handsfree", "Headset", "MicMute",
       "Transfer", "VolDown", "VolUp", "Menu", "Redial", "Applications"]
)

INFO_V2 = "/api/v2/mgmt/device/info"
# Deprecated, read only from a phone that has no v2.
INFO_V1 = "/api/v1/mgmt/device/info"
CALLS = "/api/v2/webCallControl/callStatus"

# The reads the poll makes, by the name last_error uses for each.
READ_CALLS = "call status"
READ_MUTE = "mute"
READ_LINE = "line"
READ_DISPLAY = "display power"
READ_STATUS = "phone status"
READ_TRANSFER = "transfer type"
READ_INFO = "device information"


class PhoneError(Exception):
    """The phone answered, and the answer was not a success. ``code`` is the
    phone's own result code when the body carried one."""

    def __init__(self, message: str, *, http_status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code

    @property
    def busy(self) -> bool:
        return self.http_status == 403 and self.code in ("", "4403")

    @property
    def unsupported(self) -> bool:
        """The phone has no such endpoint, or says it does not support it."""
        return self.http_status == 404 or self.code == "4004"


class _PhoneAuth(httpx.Auth):
    """Answer whichever challenge the phone sends.

    A phone on PVOS 9.2.0 or later asks for Digest (MD5 or SHA-256; httpx
    answers either and takes the first Digest challenge when the phone offers
    both); an earlier one asks for Basic. The first 401 decides, and the
    scheme is kept for the connection.
    """

    def __init__(self, username: str, password: str) -> None:
        self._digest = httpx.DigestAuth(username, password)
        self._basic = httpx.BasicAuth(username, password)
        self.scheme = ""

    def auth_flow(self, request: httpx.Request):
        if self.scheme == "basic":
            yield from self._basic.auth_flow(request)
            return
        flow = self._digest.auth_flow(request)
        request = next(flow)
        response = yield request
        if response.status_code == 401 and not self.scheme:
            offered = {
                h.split(" ", 1)[0].strip().lower()
                for h in response.headers.get_list("www-authenticate")
            }
            if "digest" not in offered and "basic" in offered:
                self.scheme = "basic"
                yield from self._basic.auth_flow(request)
                return
        try:
            request = flow.send(response)
        except StopIteration:
            return
        response = yield request
        if response.status_code != 401:
            self.scheme = "digest"


# ── Reply parsing (pure functions) ──────────────────────────────────────────


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _mac(value: Any) -> str:
    """The phone writes a MAC as 12 hex digits (``54E1AD1F2ECD`` in the
    manual's PC port example); show it the usual way."""
    text = str(value or "").strip()
    bare = text.replace(":", "").replace("-", "").lower()
    if len(bare) == 12 and all(c in "0123456789abcdef" for c in bare):
        return ":".join(bare[i:i + 2] for i in range(0, 12, 2))
    return text


def _data(payload: Any) -> Any:
    return payload.get("data") if isinstance(payload, dict) else None


def _calls(payload: Any) -> list[dict]:
    """The call list from callStatus v2 (an array; a lone object is taken as
    one call)."""
    data = _data(payload)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict)]


def _ringing_in(call: dict) -> bool:
    return (
        str(call.get("Type", "")).strip().lower() == "incoming"
        and _as_bool(call.get("Ringing", "0"))
    )


def _active(call: dict) -> bool:
    # "During an active call, a "*" will be appended to the UIAppearanceIndex
    # number." (manual page 63).
    return str(call.get("UIAppearanceIndex", "")).strip().endswith("*")


def _focus_call(calls: list[dict]) -> dict | None:
    """The call a room shows: the one ringing in, else the active one, else
    the first."""
    for call in calls:
        if _ringing_in(call):
            return call
    for call in calls:
        if _active(call):
            return call
    return calls[0] if calls else None


def _call_in_progress(calls: list[dict]) -> dict | None:
    """The call Hold, Transfer and DTMF act on: the active one, else the first
    that is not ringing in."""
    up = [c for c in calls if not _ringing_in(c)]
    for call in up:
        if _active(call):
            return call
    return up[0] if up else None


def _held_call(calls: list[dict]) -> dict | None:
    """The call Resume acts on: one that is up and not the active one. The
    manual gives no state word for a held call (callStatus lists only
    "Connected"), so a held call is told apart by the active marker."""
    for call in calls:
        if not _ringing_in(call) and not _active(call):
            return call
    return None


def _handle(call: dict) -> str:
    return str(call.get("CallHandle", "") or "").strip()


def _uptime_seconds(up: Any) -> int | None:
    if not isinstance(up, dict):
        return None
    return (
        _as_int(up.get("Days")) * 86400
        + _as_int(up.get("Hours")) * 3600
        + _as_int(up.get("Minutes")) * 60
        + _as_int(up.get("Seconds"))
    )


def _keys_for(model: str) -> list[str] | None:
    """The keys this phone takes, from the model it reports; None keeps every
    key either phone takes."""
    text = (model or "").upper()
    if "C60" in text:
        return list(_KEYS_C60)
    if "8300" in text:
        return list(_KEYS_8300)
    return None


def _key_values(keys: list[str]) -> list[dict[str, str]]:
    labels = {
        "MicMute": "Mute", "VolDown": "Volume Down", "VolUp": "Volume Up",
        "DialpadStar": "Dial Pad *", "DialpadPound": "Dial Pad #",
        "ArrowUp": "Arrow Up", "ArrowDown": "Arrow Down",
        "ArrowLeft": "Arrow Left", "ArrowRight": "Arrow Right",
    }
    out = []
    for key in keys:
        label = labels.get(key)
        if label is None:
            if key.startswith("SoftKey"):
                label = f"Soft Key {key[7:]}"
            elif key.startswith("Dialpad"):
                label = f"Dial Pad {key[7:]}"
            elif key.startswith("Line"):
                label = f"Line {key[4:]}"
            else:
                label = key
        out.append({"value": key, "label": label})
    return out


class PolyTrioDriver(BaseDriver):
    """Poly Trio C60 / Trio 8300 over the Poly Voice Software REST API."""

    DRIVER_INFO = {
        "id": "poly_trio",
        "name": "Poly Trio Conference Phone",
        "manufacturer": "Poly",
        "category": "audio",
        "version": "1.0.0",
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls Poly (HP) Trio C60 and Trio 8300 conference phones over the "
            "phone's REST API: dial, answer, decline, hold, resume, transfer and "
            "hang up calls, send DTMF, mute, step the volume, and show who is "
            "calling, the call in progress, line registration and do-not-disturb."
        ),
        "source_url": "https://kaas.hpcloud.hp.com/pdf-public/pdf_14672705_en-US-1.pdf",
        "tags": ["poly", "hp", "trio", "conference-phone", "sip", "rest"],
        "verified": False,
        "simulated": True,
        "protocols": ["poly_voice_rest"],
        "ports": [443],
        "transport": "http",
        "discovery": {
            # Polycom's and Poly's IEEE blocks, and two held by companies
            # Polycom acquired (Obihai, ViaVideo); the same audited list as
            # poly_studio, so a Poly MAC offers both drivers as possible.
            "oui": [
                "00:04:f2", "64:16:7f", "48:25:67",
                "9c:ad:ef", "00:e0:db",
            ],
            "manufacturer_alias": ["poly", "polycom", "hp", "plantronics"],
        },
        "compatible_models": [
            {
                "manufacturer": "Poly",
                "models": ["Trio C60", "Trio 8300"],
                "confidence": "untested",
                "notes": (
                    "Poly Voice Software 8.0 or later with the REST API turned on "
                    "(apps.restapi.enabled). The Trio C60 takes three keys "
                    "(Mute, Volume Up, Volume Down); the Trio 8300 its soft keys, "
                    "line keys, arrows and dial pad too."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls a Poly Trio C60 or Trio 8300 conference phone through its "
                "REST API: dial, answer, decline, ignore, hold, resume, transfer and "
                "hang up, send DTMF tones, mute and unmute, and step the volume. "
                "Shows when a call is ringing in and who is calling, the call in "
                "progress and how long it has run, whether the line is registered, "
                "do-not-disturb, mute, the display's power state and the phone's "
                "model, software and serial number. The phone has no volume "
                "reading, so volume is Volume Up and Volume Down only."
            ),
            "setup": (
                "1. Change the phone's admin password from the default. The REST "
                "API refuses every request while it is still the default.\n"
                "2. Turn on the REST API: set the configuration parameter "
                "apps.restapi.enabled to 1, from your provisioning server or the "
                "phone's web interface.\n"
                "3. In OpenAVC, enter the phone's IP address and its admin "
                "password. The REST API's username is always Polycom, so there is "
                "no username to enter.\n"
                "4. Leave Use HTTPS on and the port at 443 unless the phone has "
                "been set up otherwise."
            ),
        },
        "default_config": {
            "host": "",
            "port": 443,
            "ssl": True,
            "verify_ssl": False,
            "password": "",
            "poll_interval": 3,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 443, "min": 1, "max": 65535, "label": "Port"},
            "ssl": {
                "type": "boolean",
                "default": True,
                "label": "Use HTTPS",
                "description": "The phone's REST API is meant to be reached over HTTPS.",
            },
            "verify_ssl": {
                "type": "boolean",
                "default": False,
                "label": "Verify SSL Certificate",
                "description": (
                    "Phones ship with a self-signed certificate. Leave this off "
                    "unless the phone has a certificate this computer trusts."
                ),
            },
            "password": {
                "type": "string",
                "default": "",
                "required": True,
                "label": "Admin Password",
                "secret": True,
                "description": "The phone's admin password. The username is always Polycom.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 3,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "How often the call list and mute are read. A call ringing in "
                    "shows within this long. 0 stops polling."
                ),
            },
        },
        "state_variables": {
            "ringing": {
                "type": "boolean",
                "label": "Call Ringing In",
                "help": "True while a call is ringing in and has not been answered.",
                "control": True,
                "cloud_priority": "high",
            },
            "in_call": {
                "type": "boolean",
                "label": "In a Call",
                "help": "True while any call other than one ringing in is up: connected, dialing or on hold.",
                "control": True,
                "cloud_priority": "high",
            },
            "call_count": {"type": "integer", "label": "Calls", "help": "How many calls the phone has, ringing ones included."},
            "call_state": {
                "type": "string",
                "label": "Call State",
                "help": "The phone's own word for the shown call's state (for example Connected); empty with no call.",
            },
            "call_direction": {
                "type": "string",
                "label": "Call Direction",
                "help": "incoming or outgoing; empty with no call.",
            },
            "remote_party_name": {"type": "string", "label": "Caller / Far End Name"},
            "remote_party_number": {"type": "string", "label": "Caller / Far End Number"},
            "call_duration": {
                "type": "integer",
                "label": "Call Duration",
                "unit": "s",
                "cloud_priority": "low",
            },
            "call_line": {"type": "integer", "label": "Call Line"},
            "call_handle": {
                "type": "string",
                "label": "Call Handle",
                "help": "The phone's handle for the shown call.",
            },
            "mute": {
                "type": "boolean",
                "label": "Microphone Muted",
                "control": True,
                "cloud_priority": "high",
            },
            "registered": {
                "type": "boolean",
                "label": "Line Registered",
                "help": "Whether line 1 is registered with its call server.",
            },
            "line_label": {"type": "string", "label": "Line Label"},
            "line_user": {"type": "string", "label": "Line User", "help": "Line 1's user name or number."},
            "do_not_disturb": {"type": "boolean", "label": "Do Not Disturb", "help": "Line 1's do-not-disturb."},
            "display_state": {
                "type": "enum",
                "values": ["on", "idle", "off", "in_call"],
                "label": "Display",
                "help": "The display's power state: on, idle after the inactivity timeout, off (power saving) or in a call. PVOS 9.0 and later.",
            },
            "phone_status": {
                "type": "enum",
                "values": ["idle", "active", "error"],
                "label": "Phone Status",
            },
            "phone_status_detail": {
                "type": "string",
                "label": "Phone Status Detail",
                "help": "With Phone Status error, what the phone says is wrong; idle, when the last call was; active, how long the call has run.",
            },
            "transfer_type": {
                "type": "enum",
                "values": ["consultative", "blind", "safe"],
                "label": "Transfer Type",
            },
            "model": {"type": "string", "label": "Model"},
            "firmware_version": {"type": "string", "label": "Software Version"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "mac_address": {"type": "string", "label": "MAC Address"},
            "system_name": {"type": "string", "label": "System Name"},
            "base_profile": {"type": "string", "label": "Base Profile", "help": "The phone's base profile (provider)."},
            "app_state": {"type": "string", "label": "Application State"},
            "ready": {"type": "boolean", "label": "Ready to Use"},
            "uptime_seconds": {"type": "integer", "label": "Uptime", "unit": "s", "cloud_priority": "low"},
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "help": "A read the phone did not answer on the last poll; cleared by the next clean poll.",
            },
        },
        "device_settings": {
            "transfer_type": {
                "type": "enum",
                "values": [
                    {"value": "consultative", "label": "Consultative"},
                    {"value": "blind", "label": "Blind"},
                    {"value": "safe", "label": "Safe"},
                ],
                "label": "Transfer Type",
                "state_key": "transfer_type",
                "default": "consultative",
                "setup": False,
                "help": "How the phone's own Transfer works: consultative (talk to the new party first), blind, or safe.",
            },
        },
        "commands": {
            "hang_up": {
                "label": "Hang Up",
                "params": {},
                "help": "Ends every call on the phone. A call still ringing in is declined. Does nothing with no call.",
            },
            "answer": {
                "label": "Answer",
                "params": {},
                "help": "Answers the call ringing in.",
            },
            "dial": {
                "label": "Dial",
                "params": {
                    "number": {
                        "type": "string",
                        "required": True,
                        "label": "Number or SIP Address",
                    },
                    "line": {
                        "type": "integer",
                        "required": False,
                        "min": 1,
                        "label": "Line",
                        "help": "The line to call from. Line 1 when left empty.",
                    },
                    "protocol": {
                        "type": "enum",
                        "required": False,
                        "label": "Call Type",
                        "values": [
                            {"value": "TEL", "label": "Phone number (TEL)"},
                            {"value": "SIP", "label": "SIP address"},
                            {"value": "H323", "label": "H.323"},
                        ],
                        "help": "Phone number when left empty.",
                    },
                },
                "help": "Places a call to the number or address.",
            },
            "reject": {
                "label": "Decline",
                "params": {},
                "help": "Declines the call ringing in.",
            },
            "ignore": {
                "label": "Ignore",
                "params": {},
                "help": "Stops the call ringing in from ringing on this phone without declining it.",
            },
            "hold": {
                "label": "Hold",
                "params": {},
                "help": "Puts the call in progress on hold.",
            },
            "resume": {
                "label": "Resume",
                "params": {},
                "help": "Takes the call on hold off hold.",
            },
            "mute": {
                "label": "Mute",
                "params": {},
                "sets": {"mute": True},
                "help": "Mutes the phone's microphones.",
            },
            "unmute": {
                "label": "Unmute",
                "params": {},
                "sets": {"mute": False},
                "help": "Unmutes the phone's microphones.",
            },
            "toggle_mute": {
                "label": "Toggle Mute",
                "params": {},
                "help": "Presses the phone's mute key: mutes the microphones, or unmutes them.",
            },
            "volume_up": {
                "label": "Volume Up",
                "params": {},
                "help": "Presses the phone's Volume Up key once: the loudspeaker gets one step louder.",
            },
            "volume_down": {
                "label": "Volume Down",
                "params": {},
                "help": "Presses the phone's Volume Down key once: the loudspeaker gets one step quieter.",
            },
            "send_dtmf": {
                "label": "Send DTMF",
                "params": {
                    "digits": {
                        "type": "string",
                        "required": True,
                        "label": "Digits",
                        "pattern": "^[0-9*#A-Da-d]+$",
                        "help": "The digits to send, for example 1234#.",
                    },
                },
                "help": "Sends the tones for the digits to the far end of the call in progress, as if pressed on the dial pad.",
            },
            "transfer": {
                "label": "Transfer",
                "params": {
                    "destination": {
                        "type": "string",
                        "required": True,
                        "label": "Transfer To",
                    },
                },
                "help": "Transfers the call in progress to the number (a blind transfer: the phone leaves the call).",
            },
            "press_key": {
                "label": "Press Key",
                "params": {
                    "key": {
                        "type": "enum",
                        "required": True,
                        "label": "Key",
                        "values": [
                            {"value": "SoftKey1", "label": "Soft Key 1"},
                            {"value": "SoftKey2", "label": "Soft Key 2"},
                            {"value": "SoftKey3", "label": "Soft Key 3"},
                            {"value": "SoftKey4", "label": "Soft Key 4"},
                            {"value": "SoftKey5", "label": "Soft Key 5"},
                            {"value": "Line1", "label": "Line 1"},
                            {"value": "Line2", "label": "Line 2"},
                            {"value": "Line3", "label": "Line 3"},
                            {"value": "Line4", "label": "Line 4"},
                            {"value": "Line5", "label": "Line 5"},
                            {"value": "Line6", "label": "Line 6"},
                            {"value": "Line7", "label": "Line 7"},
                            {"value": "Line8", "label": "Line 8"},
                            {"value": "Line9", "label": "Line 9"},
                            {"value": "Line10", "label": "Line 10"},
                            {"value": "Line11", "label": "Line 11"},
                            {"value": "Line12", "label": "Line 12"},
                            {"value": "ArrowUp", "label": "Arrow Up"},
                            {"value": "ArrowDown", "label": "Arrow Down"},
                            {"value": "ArrowLeft", "label": "Arrow Left"},
                            {"value": "ArrowRight", "label": "Arrow Right"},
                            {"value": "Select", "label": "Select"},
                            {"value": "Delete", "label": "Delete"},
                            {"value": "Dialpad0", "label": "Dial Pad 0"},
                            {"value": "Dialpad1", "label": "Dial Pad 1"},
                            {"value": "Dialpad2", "label": "Dial Pad 2"},
                            {"value": "Dialpad3", "label": "Dial Pad 3"},
                            {"value": "Dialpad4", "label": "Dial Pad 4"},
                            {"value": "Dialpad5", "label": "Dial Pad 5"},
                            {"value": "Dialpad6", "label": "Dial Pad 6"},
                            {"value": "Dialpad7", "label": "Dial Pad 7"},
                            {"value": "Dialpad8", "label": "Dial Pad 8"},
                            {"value": "Dialpad9", "label": "Dial Pad 9"},
                            {"value": "DialpadStar", "label": "Dial Pad *"},
                            {"value": "DialpadPound", "label": "Dial Pad #"},
                            {"value": "Handsfree", "label": "Handsfree"},
                            {"value": "Headset", "label": "Headset"},
                            {"value": "MicMute", "label": "Mute"},
                            {"value": "Transfer", "label": "Transfer"},
                            {"value": "VolDown", "label": "Volume Down"},
                            {"value": "VolUp", "label": "Volume Up"},
                            {"value": "Menu", "label": "Menu"},
                            {"value": "Redial", "label": "Redial"},
                            {"value": "Applications", "label": "Applications"},
                        ],
                    },
                },
                "help": "Presses a key on the phone. Once the phone has reported its model, the list holds the keys that phone takes: Mute, Volume Up and Volume Down on a Trio C60.",
            },
            "update_configuration": {
                "label": "Update Configuration",
                "params": {},
                "help": "Makes the phone fetch its configuration from the provisioning server now.",
            },
            "set_config": {
                "label": "Set Configuration Parameter",
                "params": {
                    "parameter": {
                        "type": "string",
                        "required": True,
                        "label": "Parameter",
                        "pattern": "^[A-Za-z0-9_.-]+$",
                        "help": "The parameter's name, as Poly's parameter reference for the phone's software gives it.",
                    },
                    "value": {
                        "type": "string",
                        "required": True,
                        "label": "Value",
                    },
                },
                "help": "Writes one configuration parameter on the phone. The value is kept on the phone and overrides the provisioning server's.",
            },
            "restart": {
                "label": "Restart Phone Software",
                "params": {},
                "confirm": "Ends any call in progress and restarts the phone's software. The phone does not answer until it has started again.",
                "help": "Ends any call in progress, then restarts the phone's software.",
            },
            "reboot": {
                "label": "Reboot",
                "params": {},
                "confirm": "Ends any call in progress and reboots the phone. It is off the network until it has started again.",
                "help": "Ends any call in progress, then reboots the phone.",
            },
            "reset_configuration": {
                "label": "Reset Configuration",
                "params": {
                    "scope": {
                        "type": "enum",
                        "required": True,
                        "label": "What to Reset",
                        "values": [
                            {"value": "all", "label": "All settings"},
                            {"value": "local", "label": "Settings made on the phone"},
                            {"value": "web", "label": "Settings made in the web interface"},
                            {"value": "cloud", "label": "Settings from the cloud"},
                            {"value": "device", "label": "Device settings"},
                        ],
                    },
                },
                "confirm": "Erases the chosen settings on the phone and returns them to what the provisioning server or the factory gives.",
                "help": "Erases the chosen group of the phone's settings.",
            },
            "factory_reset": {
                "label": "Factory Reset",
                "params": {},
                "confirm": "Erases every setting on the phone, the admin password and the REST API switch included, and restarts it. It has to be set up again before this driver can reach it.",
                "help": "Returns the phone to its factory settings.",
            },
        },
        "actions": [
            {"id": "answer", "kind": "command", "icon": "phone-incoming"},
            {"id": "hang_up", "kind": "command", "icon": "phone-off"},
            {"id": "mute", "kind": "command", "icon": "mic-off"},
            {"id": "unmute", "kind": "command", "icon": "mic"},
            {"id": "volume_down", "kind": "command", "icon": "volume-1"},
            {"id": "volume_up", "kind": "command", "icon": "volume-2"},
        ],
    }

    HEALTH_FAULT_MESSAGE = "The phone stopped answering its REST API."
    # A probe waits behind a poll for the one-request-at-a-time lock, so it is
    # given time for that wait as well as its own request.
    HEALTH_TIMEOUT_S = 20.0

    _client: httpx.AsyncClient | None = None

    def __init__(self, device_id: str, config: dict[str, Any], state: Any, events: Any) -> None:
        super().__init__(device_id, config, state, events)
        self._client = None
        self._auth: _PhoneAuth | None = None
        self._lock = asyncio.Lock()
        self._info_path = INFO_V2
        self._detail_at = 0.0
        # Reads this phone has no endpoint for; not asked again on this connection.
        self._unsupported: set[str] = set()
        # Reads the phone did not answer on this poll: label -> why.
        self._refused: dict[str, str] = {}
        self._model = ""

    # ── Config accessors ──

    @property
    def _host(self) -> str:
        return str(self.config.get("host", "") or "").strip()

    @property
    def _port(self) -> int:
        return _as_int(self.config.get("port"), 443) or 443

    @property
    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    def _base_url(self) -> str:
        scheme = "https" if _as_bool(self.config.get("ssl", True)) else "http"
        return f"{scheme}://{self._host}:{self._port}"

    def _auth_fault(self, code: str = "") -> ConnectionFaultError:
        if code == "4010":
            message = (
                "The phone refuses its REST API while the admin password is still "
                "the default. Change the password on the phone first."
            )
        elif not self._password:
            message = "The phone's REST API needs the admin password and none is entered."
        else:
            message = "The phone refused the admin password (the username is always Polycom)."
        return ConnectionFaultError(message, code="auth_failed")

    # ── Connection lifecycle ──

    async def _create_transport(self, transport_type: str) -> None:
        if not self._host:
            raise ConnectionFaultError("No IP address is entered.", code="invalid_config")
        if not self._password:
            # Never send a sign-in that cannot succeed: the API refuses the
            # default password, and a blank one is never the admin password.
            raise self._auth_fault()
        if not await self._verify_reachable(self._host, self._port):
            raise ConnectionError(f"{self._host}:{self._port} is not responding")
        self._auth = _PhoneAuth(USERNAME, self._password)
        self._client = httpx.AsyncClient(
            base_url=self._base_url(),
            verify=_as_bool(self.config.get("verify_ssl", False)),
            timeout=httpx.Timeout(8.0, connect=5.0),
            # The manual's request example carries it on a GET too, and Basic
            # sign-in requires it on every POST.
            headers={"Content-Type": "application/json"},
            follow_redirects=False,
        )

    async def _post_connect(self) -> None:
        self._unsupported = set()
        self._detail_at = 0.0
        self._info_path = INFO_V2
        try:
            await self._read_info()
        except PhoneError as exc:
            if exc.unsupported:
                raise ConnectionFaultError(
                    "The phone answered, but not its REST API. Turn the REST API on "
                    "(configuration parameter apps.restapi.enabled set to 1).",
                    code="invalid_config",
                ) from exc
            if exc.busy:
                raise ConnectionError(
                    "The phone refused the request (HTTP 403). It answers one request "
                    "at a time, so another system may be using its REST API."
                ) from exc
            if 300 <= exc.http_status < 400:
                raise ConnectionFaultError(
                    f"The phone redirected the request ({exc}). Check Use HTTPS and the port.",
                    code="invalid_config",
                ) from exc
            raise ConnectionError(f"The phone answered with an error: {exc}") from exc
        log.info(
            f"[{self.device_id}] Connected to Poly {self.get_state('model') or 'phone'} "
            f"{self.get_state('serial_number') or ''} at {self._host}:{self._port} "
            f"(software {self.get_state('firmware_version') or 'unknown'}, "
            f"{self._auth.scheme if self._auth else ''} sign-in)"
        )

    async def _initial_sync(self) -> None:
        await self.poll()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        try:
            await self._call("GET", self._info_path)
        except PhoneError:
            return  # it answered: the link is up, and poll reports what it refuses

    # ── HTTP plumbing ──

    async def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict:
        """One request, under the lock the phone needs (one request at a
        time). Returns the reply body. A 401, or the default-password code,
        raises the typed ``auth_failed``; any other answer that is not a
        success raises PhoneError; a dead link raises ConnectionError."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        kwargs: dict[str, Any] = {"auth": self._auth}
        if body is not None:
            kwargs["json"] = body
        if params:
            kwargs["params"] = params
        async with self._lock:
            resp = None
            for attempt in (1, 2):
                try:
                    resp = await client.request(method, path, **kwargs)
                except httpx.TransportError as exc:
                    raise ConnectionError(f"{self._host} is not responding: {exc}") from exc
                if resp.status_code != 403 or attempt == 2:
                    break
                await asyncio.sleep(BUSY_RETRY_S)
        assert resp is not None
        if resp.status_code == 401:
            raise self._auth_fault()
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        code = ""
        if isinstance(payload, dict) and payload.get("Status") is not None:
            code = str(payload.get("Status")).strip()
        if code == "4010":
            raise self._auth_fault(code)
        if code and code != "2000":
            reason = RESULT_CODES.get(code, "the phone refused it")
            raise PhoneError(f"{reason} ({code})", http_status=resp.status_code, code=code)
        if 300 <= resp.status_code < 400:
            where = resp.headers.get("location", "")
            raise PhoneError(f"HTTP {resp.status_code} to {where}".strip(), http_status=resp.status_code)
        if resp.status_code >= 400:
            raise PhoneError(f"HTTP {resp.status_code}", http_status=resp.status_code)
        return payload if isinstance(payload, dict) else {}

    # ── Reads ──

    async def _read(self, label: str, path: str, sink: dict[str, str] | None = None) -> dict | None:
        """A GET for the poll: None when this phone has no such read, or when
        it did not answer this one (named in ``sink``, the poll's own record of
        refused reads unless a command passes its own)."""
        if path in self._unsupported:
            return None
        try:
            return await self._call("GET", path)
        except PhoneError as exc:
            if exc.unsupported:
                self._unsupported.add(path)
                return None
            if label == READ_CALLS and exc.code == "4007":
                return {"data": []}  # no call: "Call does not exist"
            (self._refused if sink is None else sink)[label] = (
                "the phone was busy" if exc.busy else str(exc)
            )
            return None

    async def _read_info(self) -> None:
        """Identity. Device Info v2, or the deprecated v1 on a phone without
        it. Raises PhoneError: this is the read that proves the connection."""
        try:
            payload = await self._call("GET", self._info_path)
        except PhoneError as exc:
            if not exc.unsupported or self._info_path == INFO_V1:
                raise
            payload = await self._call("GET", INFO_V1)
            self._info_path = INFO_V1
        data = _data(payload)
        if not isinstance(data, dict):
            return
        # v1 writes one key with a trailing space ("ModelNumber ").
        data = {str(k).strip(): v for k, v in data.items()}
        firmware = data.get("Firmware")
        version = firmware.get("Application") if isinstance(firmware, dict) else data.get("FirmwareRelease")
        values = {
            "model": data.get("ModelNumber"),
            "firmware_version": version,
            "serial_number": data.get("SerialNumber"),
            "system_name": data.get("SystemName"),
            "base_profile": data.get("Provider"),
            "app_state": data.get("AppState"),
        }
        for key, value in values.items():
            if value is not None:
                self.set_state(key, str(value).strip())
        if data.get("MACAddress"):
            self.set_state("mac_address", _mac(data.get("MACAddress")))
        if data.get("ReadyToUse") is not None:
            self.set_state("ready", _as_bool(data.get("ReadyToUse")))
        uptime = _uptime_seconds(data.get("UpTime"))
        if uptime is not None:
            self.set_state("uptime_seconds", uptime)
        model = str(data.get("ModelNumber") or "").strip()
        if model != self._model:
            self._model = model
            self._narrow()

    async def _read_calls(self, sink: dict[str, str] | None = None) -> list[dict] | None:
        payload = await self._read(READ_CALLS, CALLS, sink)
        if payload is None:
            return None
        return self._apply_calls(payload)

    def _apply_calls(self, payload: dict) -> list[dict]:
        calls = _calls(payload)
        focus = _focus_call(calls)
        self.set_state("call_count", len(calls))
        self.set_state("ringing", any(_ringing_in(c) for c in calls))
        self.set_state("in_call", any(not _ringing_in(c) for c in calls))
        if focus is None:
            for key in ("call_state", "call_direction", "remote_party_name", "remote_party_number", "call_handle"):
                self.set_state(key, "")
            self.set_state("call_duration", 0)
            self.set_state("call_line", 0)
            return calls
        direction = str(focus.get("Type", "")).strip().lower()
        self.set_state("call_state", str(focus.get("CallState", "") or "").strip())
        self.set_state("call_direction", direction if direction in ("incoming", "outgoing") else "")
        self.set_state("remote_party_name", str(focus.get("RemotePartyName", "") or "").strip())
        self.set_state("remote_party_number", str(focus.get("RemotePartyNumber", "") or "").strip())
        self.set_state("call_handle", _handle(focus))
        # The manual's v2 example names it DurationSeconds, and its paging
        # example DurationInSeconds; either is read.
        duration = focus.get("DurationSeconds", focus.get("DurationInSeconds"))
        self.set_state("call_duration", _as_int(duration))
        self.set_state("call_line", _as_int(focus.get("LineID")))
        return calls

    async def _read_mute(self, sink: dict[str, str] | None = None) -> None:
        payload = await self._read(READ_MUTE, "/api/v1/mgmt/media/communicationInfo", sink)
        data = _data(payload)
        if isinstance(data, dict) and data.get("PhoneMuteState") is not None:
            self.set_state("mute", _as_bool(data.get("PhoneMuteState")))

    async def _read_line(self) -> None:
        payload = await self._read(READ_LINE, "/api/v2/mgmt/lineInfo")
        data = _data(payload)
        lines = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        line = next((x for x in lines if isinstance(x, dict) and str(x.get("ID", "")).strip() == "1"), None)
        if line is None:
            line = next((x for x in lines if isinstance(x, dict)), None)
        if line is None:
            return
        self.set_state("registered", str(line.get("RegistrationStatus", "")).strip().lower() == "registered")
        self.set_state("line_label", str(line.get("Label", "") or "").strip())
        self.set_state("line_user", str(line.get("Username", "") or "").strip())
        if line.get("DoNotDisturb") is not None:
            self.set_state("do_not_disturb", _as_bool(line.get("DoNotDisturb")))

    async def _read_display(self) -> None:
        payload = await self._read(READ_DISPLAY, "/api/v1/mgmt/powerStatus")
        if payload is None:
            return
        # The manual's reply has CurrentState beside Status, not under data.
        data = _data(payload)
        raw = payload.get("CurrentState")
        if raw is None and isinstance(data, dict):
            raw = data.get("CurrentState")
        state = DISPLAY_STATES.get(str(raw or "").strip().lower())
        if state:
            self.set_state("display_state", state)

    async def _read_status(self) -> None:
        payload = await self._read(READ_STATUS, "/api/v1/mgmt/pollForStatus")
        data = _data(payload)
        if not isinstance(data, dict):
            return
        status = str(data.get("State", "") or "").strip().lower()
        if status in ("idle", "active", "error"):
            self.set_state("phone_status", status)
        self.set_state("phone_status_detail", str(data.get("StateData", "") or "").strip())

    async def _read_transfer_type(self, sink: dict[str, str] | None = None) -> None:
        payload = await self._read(READ_TRANSFER, "/api/v1/mgmt/transferType/get", sink)
        data = _data(payload)
        if isinstance(data, dict):
            kind = str(data.get("Type", "") or "").strip().lower()
            if kind in TRANSFER_TYPES:
                self.set_state("transfer_type", kind)

    async def _read_detail(self) -> None:
        try:
            await self._read_info()
        except PhoneError as exc:
            self._refused[READ_INFO] = "the phone was busy" if exc.busy else str(exc)
        await self._read_line()
        await self._read_display()
        await self._read_status()
        await self._read_transfer_type()

    async def poll(self) -> None:
        """The call list and mute every poll, the rest every 30 s. A dead
        link raises (the platform counts it); a read the phone does not
        answer is skipped and named in last_error, written again by every
        poll while it holds."""
        if self._client is None:
            return
        self._refused = {}
        await self._read_calls()
        await self._read_mute()
        now = time.monotonic()
        if now - self._detail_at >= DETAIL_INTERVAL_S:
            self._detail_at = now
            await self._read_detail()
        if self._refused:
            what = ", ".join(f"{label} ({why})" for label, why in self._refused.items())
            self.set_state("last_error", f"The phone did not answer the {what} read; those values are not updated.")

    async def _refresh_calls(self) -> list[dict]:
        """Read the call list for a command. A list the phone will not give
        is the command's refusal."""
        try:
            payload = await self._call("GET", CALLS)
        except PhoneError as exc:
            if exc.code != "4007":
                raise self._refusal(exc, "give its call list, so nothing was sent") from exc
            payload = {"data": []}  # no call: "Call does not exist"
        return self._apply_calls(payload)

    async def _after_call_command(self) -> None:
        """Read the calls and mute again so the state follows the command.
        What the phone does not answer here is left to the next poll."""
        scratch: dict[str, str] = {}
        await self._read_calls(scratch)
        await self._read_mute(scratch)

    # ── Model narrowing ──

    def _narrow(self) -> None:
        """Offer the keys this phone takes. An unknown model keeps them all."""
        base = type(self).DRIVER_INFO
        keys = _keys_for(self._model)
        if keys is None:
            self.DRIVER_INFO = base
            return
        commands = copy.deepcopy(base["commands"])
        commands["press_key"]["params"]["key"]["values"] = _key_values(keys)
        self.DRIVER_INFO = {**base, "commands": commands}

    # ── Commands ──

    def _refusal(self, exc: PhoneError, what: str) -> CommandRefusedError:
        if exc.busy:
            return CommandRefusedError(f"The phone was busy with another request and refused to {what}. Try again.")
        return CommandRefusedError(f"The phone refused to {what}: {exc}.")

    async def _send(self, what: str, method: str, path: str, body: Any = None) -> dict:
        try:
            return await self._call(method, path, body)
        except PhoneError as exc:
            raise self._refusal(exc, what) from exc

    async def _key(self, key: str) -> None:
        await self._send(
            "press the key", "POST", "/api/v1/mgmt/simulateKeyEvent",
            {"data": {"Type": "Tap", "KeyName": key}},
        )

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        params = params or {}

        if command == "hang_up":
            calls = await self._refresh_calls()
            up = [c for c in calls if not _ringing_in(c) and _handle(c)]
            ringing = [c for c in calls if _ringing_in(c) and _handle(c)]
            for call in up:
                await self._end(call, "/api/v1/callctrl/endCall", "end the call")
            for call in ringing:
                await self._end(call, "/api/v1/callctrl/rejectCall", "decline the call")
            await self._after_call_command()
            return None

        if command in ("answer", "reject", "ignore"):
            calls = await self._refresh_calls()
            call = next((c for c in calls if _ringing_in(c) and _handle(c)), None)
            if call is None:
                raise CommandRefusedError("No call is ringing in.")
            path, what = {
                "answer": ("/api/v1/callctrl/answerCall", "answer the call"),
                "reject": ("/api/v1/callctrl/rejectCall", "decline the call"),
                "ignore": ("/api/v1/callctrl/ignoreCall", "ignore the call"),
            }[command]
            await self._send(what, "POST", path, {"data": {"Ref": _handle(call)}})
            await self._after_call_command()
            return None

        if command in ("hold", "transfer"):
            calls = await self._refresh_calls()
            call = _call_in_progress(calls)
            if call is None or not _handle(call):
                raise CommandRefusedError("No call is in progress.")
            if command == "hold":
                await self._send("put the call on hold", "POST", "/api/v1/callctrl/holdCall",
                                 {"data": {"Ref": _handle(call)}})
            else:
                await self._send("transfer the call", "POST", "/api/v1/callctrl/transferCall",
                                 {"data": {"Ref": _handle(call), "TransferDest": str(params["destination"])}})
            await self._after_call_command()
            return None

        if command == "resume":
            calls = await self._refresh_calls()
            call = _held_call(calls)
            if call is None or not _handle(call):
                raise CommandRefusedError("No call is on hold.")
            await self._send("take the call off hold", "POST", "/api/v1/callctrl/resumeCall",
                             {"data": {"Ref": _handle(call)}})
            await self._after_call_command()
            return None

        if command == "dial":
            data: dict[str, Any] = {"Dest": str(params["number"]).strip()}
            if params.get("line") not in (None, ""):
                data["Line"] = str(int(params["line"]))
            if params.get("protocol"):
                data["Type"] = str(params["protocol"])
            try:
                await self._call("POST", "/api/v1/callctrl/dial", {"data": data})
            except PhoneError as exc:
                if exc.code == "4002":
                    raise CommandRefusedError("The phone's line is not registered, so it cannot place a call (4002).") from exc
                raise self._refusal(exc, "place the call") from exc
            await self._after_call_command()
            return None

        if command in ("mute", "unmute"):
            await self._set_mute(command == "mute")
            return None

        if command == "toggle_mute":
            await self._key("MicMute")
            await self._after_call_command()
            return None

        if command == "volume_up":
            await self._key("VolUp")
            return None

        if command == "volume_down":
            await self._key("VolDown")
            return None

        if command == "press_key":
            await self._key(str(params["key"]))
            return None

        if command == "send_dtmf":
            # "The speed of the request execution is improved when the phone
            # receives a consecutive series of SendDTMF API requests that each
            # include just a single digit" (manual page 73).
            for digit in str(params["digits"]).strip():
                try:
                    await self._call("POST", "/api/v1/callctrl/sendDTMF", {"data": {"Digits": digit}})
                except PhoneError as exc:
                    if exc.code == "4007":
                        raise CommandRefusedError("No call is in progress to send the tones to.") from exc
                    raise self._refusal(exc, "send the tones") from exc
            return None

        if command == "update_configuration":
            payload = await self._send("update its configuration", "POST", "/api/v1/mgmt/updateConfiguration")
            data = _data(payload)
            return data.get("ProvisioningState") if isinstance(data, dict) else None

        if command == "set_config":
            name = str(params["parameter"]).strip()
            try:
                await self._call("POST", "/api/v1/mgmt/config/set", {"data": {name: str(params["value"])}})
            except PhoneError as exc:
                if exc.code == "4001":
                    raise CommandRefusedError(
                        "The phone is busy starting up or registering its line and refused the setting (4001). Try again shortly."
                    ) from exc
                raise self._refusal(exc, f"set {name}") from exc
            return None

        if command == "restart":
            await self._send("restart", "POST", "/api/v1/mgmt/safeRestart")
            return None

        if command == "reboot":
            await self._send("reboot", "POST", "/api/v1/mgmt/safeReboot")
            return None

        if command == "reset_configuration":
            scope = str(params["scope"])
            path = "/api/v1/mgmt/configReset" if scope == "all" else f"/api/v1/mgmt/configReset/{scope}"
            await self._send("reset its configuration", "POST", path)
            return None

        if command == "factory_reset":
            await self._send("factory reset", "POST", "/api/v1/mgmt/factoryReset")
            return None

        raise ValueError(f"Unknown command: {command}")

    async def _end(self, call: dict, path: str, what: str) -> None:
        try:
            await self._call("POST", path, {"data": {"Ref": _handle(call)}})
        except PhoneError as exc:
            if exc.code == "4007":
                return  # it ended on its own meanwhile
            raise self._refusal(exc, what) from exc

    async def _set_mute(self, muted: bool) -> None:
        """Mute through call control. The phone answers 4003 when the mute
        state would not change, and 4007 with no call; with no call, its mute
        key is pressed instead when the reading says the state differs."""
        try:
            await self._call("POST", "/api/v1/callctrl/mute", {"data": {"state": "1" if muted else "0"}})
        except PhoneError as exc:
            if exc.code not in ("4003", "4007"):
                raise self._refusal(exc, "mute" if muted else "unmute") from exc
            await self._read_mute({})
            current = self.get_state("mute")
            if current is not None and bool(current) != muted and exc.code == "4007":
                await self._key("MicMute")
            elif current is not None and bool(current) != muted:
                raise self._refusal(exc, "mute" if muted else "unmute") from exc
        await self._after_call_command()

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if key != "transfer_type":
            raise ValueError(f"Unknown device setting: {key}")
        word = TRANSFER_TYPES.get(str(value).strip().lower())
        if word is None:
            raise ValueError(f"Unknown transfer type: {value}")
        await self._send("set the transfer type", "POST", "/api/v1/mgmt/transferType/set", {"data": {"Type": word}})
        await self._read_transfer_type({})
