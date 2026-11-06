"""
Sonos Speaker — Simulator

Simulates a Sonos Amp via the local UPnP/SOAP API on port 1400: its device
description, the services the driver uses (AVTransport, RenderingControl,
GroupRenderingControl, DeviceProperties, ZoneGroupTopology, ContentDirectory,
AlarmClock, AudioIn) with proper SOAP envelope formatting, plus UPnP GENA
eventing: SUBSCRIBE / UNSUBSCRIBE on the service Event endpoints, an initial
event on subscribe, and a NOTIFY to every subscriber whenever state changes.

The event payloads mirror a real speaker in shape: a propertyset carrying a
single escaped LastChange document (AVTransport, RenderingControl) or one
property per variable (the other services), Sonos's trailing-slash
namespaces, per-channel Volume/Mute elements, and the X-SONOS-SERVICETYPE
header. The answers follow what a Sonos Amp (S16) and a Sonos One (S18) did on
the bench, see driver-roadmap/reference-docs/sonos-upnp/FACTS.md.

The simulated household has one more room, the Kitchen, so grouping has
somewhere to go. The Kitchen has no speaker of its own: its address is this
simulator's, so a command the driver sends to the Kitchen as the group's
coordinator lands here, and the group shares this simulator's playback.

The speaker starts with a twelve-track queue loaded, one playable favorite (a
station, which allows only Set and Play, as a radio stream does), one Sonos
Radio shortcut favorite (which nothing but the Sonos app can start, so the
driver does not list it), one Sonos playlist, and one alarm for this room.

Driver: sonos
Transport: http
"""

import asyncio
import re
import time
from html import escape

from openavc.simulator.http_simulator import HTTPSimulator

_UUID = "RINCON_SIM0000001400"
_KITCHEN = "RINCON_SIMKITCHEN01400"

# Service name -> (control path, event path, the device it sits on), in the
# layout a Sonos Amp's description gives.
_SERVICES = {
    "AlarmClock": ("/AlarmClock/Control", "/AlarmClock/Event", "root"),
    "DeviceProperties": ("/DeviceProperties/Control", "/DeviceProperties/Event", "root"),
    "ZoneGroupTopology": ("/ZoneGroupTopology/Control", "/ZoneGroupTopology/Event", "root"),
    "AudioIn": ("/AudioIn/Control", "/AudioIn/Event", "root"),
    "HTControl": ("/HTControl/Control", "/HTControl/Event", "root"),
    "ContentDirectory": (
        "/MediaServer/ContentDirectory/Control", "/MediaServer/ContentDirectory/Event", "server",
    ),
    "RenderingControl": (
        "/MediaRenderer/RenderingControl/Control", "/MediaRenderer/RenderingControl/Event", "renderer",
    ),
    "AVTransport": (
        "/MediaRenderer/AVTransport/Control", "/MediaRenderer/AVTransport/Event", "renderer",
    ),
    "GroupRenderingControl": (
        "/MediaRenderer/GroupRenderingControl/Control",
        "/MediaRenderer/GroupRenderingControl/Event", "renderer",
    ),
}
_CONTROL_PATHS = {control: name for name, (control, _, _) in _SERVICES.items()}
_EVENT_PATHS = {event: name for name, (_, event, _) in _SERVICES.items()}


def _service_list(device: str) -> str:
    out = ""
    for name, (control, event, where) in _SERVICES.items():
        if where != device:
            continue
        out += (
            "<service>"
            f"<serviceType>urn:schemas-upnp-org:service:{name}:1</serviceType>"
            f"<serviceId>urn:upnp-org:serviceId:{name}</serviceId>"
            f"<controlURL>{control}</controlURL>"
            f"<eventSubURL>{event}</eventSubURL>"
            f"<SCPDURL>/xml/{name}1.xml</SCPDURL>"
            "</service>"
        )
    return out


# The device description (UPnP DA 2.0 section 2.3), shaped like a Sonos Amp's:
# the ZonePlayer root device with the MediaServer and MediaRenderer embedded.
_DESCRIPTION = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<root xmlns="urn:schemas-upnp-org:device-1-0">'
    "<specVersion><major>1</major><minor>0</minor></specVersion>"
    "<device>"
    "<deviceType>urn:schemas-upnp-org:device:ZonePlayer:1</deviceType>"
    f"<friendlyName>127.0.0.1 - Sonos Amp - {_UUID}</friendlyName>"
    "<manufacturer>Sonos, Inc.</manufacturer>"
    "<manufacturerURL>http://www.sonos.com</manufacturerURL>"
    "<modelNumber>S16</modelNumber>"
    "<modelDescription>Sonos Amp</modelDescription>"
    "<modelName>Sonos Amp</modelName>"
    "<softwareVersion>97.2-81220</softwareVersion>"
    f"<UDN>uuid:{_UUID}</UDN>"
    "<roomName>Living Room</roomName>"
    "<displayName>Amp</displayName>"
    f"<serviceList>{_service_list('root')}</serviceList>"
    "<deviceList>"
    "<device>"
    "<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>"
    "<friendlyName>Living Room - Sonos Amp Media Server</friendlyName>"
    f"<UDN>uuid:{_UUID}_MS</UDN>"
    f"<serviceList>{_service_list('server')}</serviceList>"
    "</device>"
    "<device>"
    "<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
    "<friendlyName>Living Room - Sonos Amp Media Renderer</friendlyName>"
    f"<UDN>uuid:{_UUID}_MR</UDN>"
    f"<serviceList>{_service_list('renderer')}</serviceList>"
    "</device>"
    "</deviceList>"
    "</device>"
    "</root>"
)

# SOAP response wrapper template
_SOAP_RESPONSE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>"
    '<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
    "{fields}"
    "</u:{action}Response>"
    "</s:Body>"
    "</s:Envelope>"
)

# A rejected action: HTTP 500 with a UPnPError, as a real speaker answers.
_SOAP_FAULT = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body><s:Fault><faultcode>s:Client</faultcode>"
    "<faultstring>UPnPError</faultstring><detail>"
    '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
    "<errorCode>{code}</errorCode></UPnPError>"
    "</detail></s:Fault></s:Body></s:Envelope>"
)

_DIDL_NS = (
    'xmlns:dc="http://purl.org/dc/elements/1.1/"'
    ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
    ' xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"'
    ' xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
)

# The playable favorite: a station, as a Sonos Radio stream is (only Set and
# Play allowed, no queue play modes).
_STATION_URI = "x-sonosapi-radio:sim-station?sid=254&flags=0&sn=1"
_STATION_TITLE = "Simulated Station"
_PLAYLIST_RES = "file:///jffs/settings/savedqueues.rsq#{n}"

# Map internal lowercase state → SOAP protocol uppercase
_STATE_TO_SOAP = {
    "playing": "PLAYING",
    "paused": "PAUSED_PLAYBACK",
    "stopped": "STOPPED",
    "transitioning": "TRANSITIONING",
}

_PLAY_MODES = (
    "NORMAL",
    "REPEAT_ALL",
    "REPEAT_ONE",
    "SHUFFLE_NOREPEAT",
    "SHUFFLE",
    "SHUFFLE_REPEAT_ONE",
)

# The home theater EQ types a Sonos Amp with nothing bonded answers. Every
# other EQ type is refused with 402, as the Amp refuses SubEnabled and
# SurroundEnabled.
_EQ = {"NightMode": "night_mode", "DialogLevel": "dialog_level", "AudioDelay": "audio_delay"}

# Which service events each state key belongs to.
_AVT_KEYS = {
    "transport_state", "play_mode", "crossfade", "track_title", "track_artist",
    "track_album", "track_duration", "queue_length", "track_number", "source",
    "source_name", "sleep_timer_generation", "alarm_running", "snooze_running",
}
_RCS_KEYS = {"volume", "mute", "bass", "treble", "loudness", "night_mode", "dialog_level", "audio_delay"}
_GRC_KEYS = {"volume", "mute", "kitchen_volume", "grouped"}
_DP_KEYS = {"speaker_name"}
_AUDIOIN_KEYS = {"line_in_connected"}

_TIME = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$")


def _soap_response(service: str, action: str, fields: str) -> str:
    """Build a complete SOAP response XML."""
    return _SOAP_RESPONSE.format(service=service, action=action, fields=fields)


def _fault(code: int) -> tuple[int, str]:
    return 500, _SOAP_FAULT.format(code=code)


def _extract_soap_action(headers: dict[str, str]) -> tuple[str, str]:
    """Extract (service_urn_fragment, action_name) from the SOAPAction header.

    SOAPAction looks like:
      "urn:schemas-upnp-org:service:AVTransport:1#Play"
    """
    raw = headers.get("SOAPAction", headers.get("soapaction", ""))
    raw = raw.strip('"')
    match = re.search(r"service:(\w+):\d+#(\w+)", raw)
    if match:
        return match.group(1), match.group(2)
    return "", ""


def _extract_xml_value(body: str, tag: str) -> str | None:
    """Extract a simple XML tag value from the SOAP body (unescaped once)."""
    match = re.search(rf"<{tag}>(.*?)</{tag}>", body, re.S)
    if match:
        return (
            match.group(1).replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&amp;", "&")
        )
    if re.search(rf"<{tag}\s*/>", body):
        return ""
    return None


def _header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup (UPnP field names are not case sensitive)."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def _seconds(text: str) -> int | None:
    match = _TIME.match(text or "")
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _hms(total: int) -> str:
    total = max(0, int(total))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _didl_title(text: str | None) -> str | None:
    match = re.search(r"<dc:title>(.*?)</dc:title>", text or "", re.S)
    return match.group(1).strip() if match else None


class SonosSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "sonos",
        "name": "Sonos Speaker Simulator",
        "category": "audio",
        "transport": "http",
        "default_port": 1400,
        "initial_state": {
            "transport_state": "stopped",
            "volume": 25,
            "mute": False,
            "bass": 0,
            "treble": 0,
            "loudness": True,
            "play_mode": "NORMAL",
            "crossfade": False,
            "track_title": "Simulation Track",
            "track_artist": "OpenAVC",
            "track_album": "Test Album",
            "track_duration": "0:03:45",
            "track_position": "0:00:00",
            "queue_length": 12,
            "track_number": 1,
            "source": "queue",
            "source_name": "",
            "sleep_timer_generation": 0,
            "alarm_running": False,
            "snooze_running": False,
            "status_light": True,
            "touch_controls_locked": False,
            "speaker_name": "Living Room",
            "grouped": False,
            "kitchen_volume": 30,
            "line_in_connected": False,
            "night_mode": False,
            "dialog_level": 0,
            "audio_delay": 0,
        },
        "delays": {
            "command_response": 0.05,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Speaker stops responding to commands",
                "behavior": "no_response",
            },
        },
        "controls": [
            {
                "type": "select",
                "key": "transport_state",
                "label": "Transport",
                "options": ["stopped", "playing", "paused", "transitioning"],
                "labels": {
                    "stopped": "Stopped",
                    "playing": "Playing",
                    "paused": "Paused",
                    "transitioning": "Transitioning",
                },
            },
            {"type": "slider", "key": "volume", "label": "Volume", "min": 0, "max": 100},
            {"type": "toggle", "key": "mute", "label": "Mute"},
            {"type": "slider", "key": "bass", "label": "Bass", "min": -10, "max": 10},
            {"type": "slider", "key": "treble", "label": "Treble", "min": -10, "max": 10},
            {"type": "toggle", "key": "loudness", "label": "Loudness"},
            {
                "type": "select",
                "key": "play_mode",
                "label": "Play Mode",
                "options": list(_PLAY_MODES),
            },
            {"type": "toggle", "key": "crossfade", "label": "Crossfade"},
            {"type": "toggle", "key": "night_mode", "label": "Night Sound"},
            {"type": "toggle", "key": "line_in_connected", "label": "Line-In Connected"},
            {"type": "toggle", "key": "alarm_running", "label": "Alarm Running"},
            {"type": "toggle", "key": "status_light", "label": "Status Light"},
            {"type": "toggle", "key": "touch_controls_locked", "label": "Touch Controls Locked"},
            {"type": "indicator", "key": "source", "label": "Source"},
            {"type": "indicator", "key": "track_title", "label": "Track"},
            {"type": "indicator", "key": "queue_length", "label": "Queue Length"},
            {"type": "indicator", "key": "grouped", "label": "Grouped with the Kitchen"},
            {"type": "indicator", "key": "speaker_name", "label": "Speaker Name"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # GENA subscriptions: sid -> {callback, service, seq}
        self._subscriptions: dict[str, dict] = {}
        self._sid_counter = 0
        # The sleep timer: when it ends (time.monotonic()), and its task.
        self._sleep_deadline: float | None = None
        self._sleep_task: asyncio.Task | None = None
        self._ramp_task: asyncio.Task | None = None
        # Who leads the group while grouped: this room or the Kitchen.
        self._group_leader = _UUID
        self._uri = f"x-rincon-queue:{_UUID}#0"
        # The household's content: favorites, playlists, alarms.
        self._favorites = {
            "FV:2/0": {"title": _STATION_TITLE, "res": _STATION_URI, "type": "instantPlay"},
            "FV:2/1": {"title": "Discover Sonos Radio", "res": "", "type": "shortcut"},
        }
        self._playlists = {"SQ:0": {"title": "Simulated Playlist", "tracks": 4}}
        self._alarms = {
            "1": {
                "ID": "1", "StartTime": "07:00:00", "Duration": "01:00:00",
                "Recurrence": "WEEKDAYS", "Enabled": "1", "RoomUUID": _UUID,
                "ProgramURI": "x-rincon-buzzer:0", "ProgramMetaData": "",
                "PlayMode": "SHUFFLE", "Volume": "20", "IncludeLinkedZones": "0",
            },
            "2": {
                "ID": "2", "StartTime": "06:30:00", "Duration": "01:00:00",
                "Recurrence": "DAILY", "Enabled": "0", "RoomUUID": _KITCHEN,
                "ProgramURI": "x-rincon-buzzer:0", "ProgramMetaData": "",
                "PlayMode": "NORMAL", "Volume": "15", "IncludeLinkedZones": "0",
            },
        }
        self._update_ids = {"favorites": 1, "playlists": 1, "alarms": 1}

    # ── Request routing ──

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ):
        if method == "GET" and path.split("?")[0] == "/xml/device_description.xml":
            return 200, _DESCRIPTION

        # GENA eventing lives on the Event endpoints, not the Control ones.
        if method in ("SUBSCRIBE", "UNSUBSCRIBE"):
            return self._handle_gena(method, path, headers)

        if method != "POST":
            return 405, "Method Not Allowed"

        service, action = _extract_soap_action(headers)
        if not service or not action or _CONTROL_PATHS.get(path) != service:
            return 400, "Bad Request: missing or invalid SOAPAction header"

        handler = {
            "AVTransport": self._handle_av_transport,
            "RenderingControl": self._handle_rendering_control,
            "GroupRenderingControl": self._handle_group_rendering_control,
            "DeviceProperties": self._handle_device_properties,
            "ZoneGroupTopology": self._handle_topology,
            "ContentDirectory": self._handle_content_directory,
            "AlarmClock": self._handle_alarm_clock,
            "AudioIn": self._handle_audio_in,
        }.get(service)
        if handler is None:
            return 200, _soap_response(service, action, "")
        return handler(action, body)

    # ── GENA: SUBSCRIBE / UNSUBSCRIBE ──

    def _handle_gena(self, method: str, path: str, headers: dict[str, str]):
        """Subscribe / renew / cancel an event subscription.

        Mirrors UPnP DA 2.0 §4.1: an initial SUBSCRIBE carries CALLBACK + NT
        and gets a fresh SID; a renewal carries SID only (mixing them is a
        400); a SID the speaker doesn't know is a 412. Sonos grants exactly
        the TIMEOUT that was asked for.
        """
        service = _EVENT_PATHS.get(path)
        if service is None:
            return 404, "Not Found"

        sid = _header(headers, "SID")
        callback = _header(headers, "CALLBACK")
        nt = _header(headers, "NT")
        timeout = _header(headers, "TIMEOUT") or "Second-1800"

        if method == "UNSUBSCRIBE":
            if not sid:
                return 412, "Precondition Failed: missing SID"
            if sid not in self._subscriptions:
                return 412, "Precondition Failed: unknown SID"
            del self._subscriptions[sid]
            self.log_protocol("in", f"UNSUBSCRIBE {service} ({sid})")
            return 200, ""

        # SUBSCRIBE — renewal (SID) or new subscription (CALLBACK + NT)
        if sid:
            if callback or nt:
                return 400, "Bad Request: SID with CALLBACK/NT"
            if sid not in self._subscriptions:
                return 412, "Precondition Failed: unknown SID"
            self.log_protocol("in", f"SUBSCRIBE renew {service} ({sid})")
            return 200, "", {"SID": sid, "TIMEOUT": timeout}

        if not callback or nt != "upnp:event":
            return 412, "Precondition Failed: bad CALLBACK/NT"

        # CALLBACK is one or more <url> entries; use the first.
        match = re.search(r"<([^>]+)>", callback)
        if not match:
            return 412, "Precondition Failed: malformed CALLBACK"
        url = match.group(1)

        self._sid_counter += 1
        new_sid = f"uuid:{_UUID}_sub{self._sid_counter:010d}"
        self._subscriptions[new_sid] = {"callback": url, "service": service, "seq": 0}
        self.log_protocol("in", f"SUBSCRIBE {service} -> {url} ({new_sid})")

        # A real publisher sends the initial event (SEQ 0, every evented
        # variable) right after accepting the subscription.
        asyncio.ensure_future(self._send_initial_event(new_sid))

        return 200, "", {"SID": new_sid, "TIMEOUT": timeout}

    # ── GENA: NOTIFY delivery ──

    async def _send_initial_event(self, sid: str) -> None:
        sub = self._subscriptions.get(sid)
        if not sub:
            return
        await self._notify(sid, self._event_body(sub["service"], None))

    def _event_body(self, service: str, keys: set[str] | None) -> str:
        """One service's evented properties; every one when keys is None."""
        if service == "AVTransport":
            return self._last_change_property(service, _AVT_KEYS if keys is None else keys)
        if service == "RenderingControl":
            return self._last_change_property(service, _RCS_KEYS if keys is None else keys)
        if service == "DeviceProperties":
            return self._prop("ZoneName", self.state.get("speaker_name") or "")
        if service == "GroupRenderingControl":
            volume, mute = self._group_volume()
            return (
                self._prop("GroupVolume", str(volume))
                + self._prop("GroupMute", "1" if mute else "0")
                + self._prop("GroupVolumeChangeable", "1")
            )
        if service == "ZoneGroupTopology":
            return self._prop("ZoneGroupState", self._topology())
        if service == "ContentDirectory":
            return (
                self._prop("FavoritesUpdateID", f"{_UUID},{self._update_ids['favorites']}")
                + self._prop("SavedQueuesUpdateID", f"{_UUID},{self._update_ids['playlists']}")
            )
        if service == "AlarmClock":
            return self._prop("AlarmListVersion", f"{_UUID}:{self._update_ids['alarms']}")
        if service == "AudioIn":
            connected = "1" if self.state.get("line_in_connected") else "0"
            return (
                self._prop("AudioInputName", "Audio Component")
                + f'<e:property><LineInConnected Instance="0">{connected}</LineInConnected></e:property>'
            )
        return ""

    @staticmethod
    def _prop(name: str, value: str) -> str:
        return f"<e:property><{name}>{escape(value, quote=False)}</{name}></e:property>"

    async def _notify(self, sid: str, properties: str) -> None:
        """POST one NOTIFY to a subscriber, with the GENA headers."""
        sub = self._subscriptions.get(sid)
        if not sub or not properties:
            return
        seq = sub["seq"]
        sub["seq"] = seq + 1
        body = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"{properties}</e:propertyset>"
        )
        await self.post_http_callback(
            sub["callback"],
            body,
            headers={
                "CONTENT-TYPE": 'text/xml; charset="utf-8"',
                "NT": "upnp:event",
                "NTS": "upnp:propchange",
                "SID": sid,
                "SEQ": str(seq),
                "X-SONOS-SERVICETYPE": sub["service"],
            },
            method="NOTIFY",
        )

    def _event(self, service: str, keys: set[str] | None = None) -> None:
        body = self._event_body(service, keys)
        if not body:
            return
        for sid, sub in list(self._subscriptions.items()):
            if sub["service"] == service:
                asyncio.ensure_future(self._notify(sid, body))

    def _last_change_property(self, service: str, keys: set[str]) -> str:
        last_change = self._last_change(service, keys)
        if not last_change:
            return ""
        return f"<e:property><LastChange>{escape(last_change)}</LastChange></e:property>"

    def _last_change(self, service: str, keys: set[str]) -> str:
        """Render a LastChange document for the given state keys.

        Namespaces carry the trailing slash a real Sonos sends (the spec's
        examples don't); the driver must be namespace-insensitive, and this
        is what proves it.
        """
        ns = (
            'xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"'
            if service == "AVTransport"
            else 'xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/"'
        )
        vars_xml = "".join(self._variable_xml(key) for key in sorted(keys))
        if service == "AVTransport" and keys & {
            "transport_state", "source", "queue_length", "track_number",
        }:
            vars_xml += f'<CurrentTransportActions val="{self._transport_actions()}"/>'
        if service == "RenderingControl" and keys == _RCS_KEYS:
            # A speaker events SubEnabled 1 whether or not a sub is bonded
            # (both bench units); the driver must not read it from here.
            vars_xml += '<SubEnabled val="1"/><SubGain val="0"/>'
        if not vars_xml:
            return ""
        return (
            f"<Event {ns}>"
            f'<InstanceID val="0">{vars_xml}</InstanceID>'
            "</Event>"
        )

    def _transport_actions(self) -> str:
        """What the loaded source allows, as a Sonos Amp lists it."""
        source = self.state.get("source")
        if source == "queue":
            if not self.state.get("queue_length"):
                return "Set"
            actions = ["Set", "Stop", "Pause", "Play", "X_DLNA_SeekTime"]
            number = int(self.state.get("track_number") or 0)
            if number < int(self.state.get("queue_length") or 0):
                actions.append("Next")
            if number > 1:
                actions.append("Previous")
            actions.append("X_DLNA_SeekTrackNr")
            return ", ".join(actions)
        if source == "stream":
            if self._uri.startswith("x-sonosapi-radio:"):
                return "Set, Play"
            return "Set, Stop, Pause, Play, X_DLNA_SeekTime, X_DLNA_SeekTrackNr"
        if source == "line_in":
            return "Set, Stop, Play"
        if source == "tv":
            return "Set, Play"
        return "Set"

    def _variable_xml(self, key: str) -> str:
        """One LastChange variable element for a state key (or "" if it has
        no evented representation)."""
        state = self.state
        if key == "transport_state":
            value = _STATE_TO_SOAP.get(state.get("transport_state", "stopped"), "STOPPED")
            return f'<TransportState val="{value}"/>'
        if key == "play_mode":
            return f'<CurrentPlayMode val="{state.get("play_mode", "NORMAL")}"/>'
        if key == "crossfade":
            return f'<CurrentCrossfadeMode val="{1 if state.get("crossfade") else 0}"/>'
        if key == "track_duration":
            return f'<CurrentTrackDuration val="{state.get("track_duration", "0:00:00")}"/>'
        if key in ("track_title", "track_artist", "track_album"):
            # Title, artist and album all ride in ONE CurrentTrackMetaData blob
            # — the metadata is atomic on a real speaker (a track change events
            # it once, complete). So a change to any of the three emits the
            # whole current blob; editing just the Artist in the Simulator UI
            # still reaches the driver.
            return f'<CurrentTrackMetaData val="{escape(self._track_didl())}"/>'
        if key == "queue_length":
            return f'<NumberOfTracks val="{int(state.get("queue_length") or 0)}"/>'
        if key == "track_number":
            return f'<CurrentTrack val="{int(state.get("track_number") or 0)}"/>'
        if key in ("source", "source_name"):
            return (
                f'<AVTransportURI val="{escape(self._uri)}"/>'
                f'<AVTransportURIMetaData val="{escape(self._source_didl())}"/>'
            )
        if key == "sleep_timer_generation":
            return f'<r:SleepTimerGeneration val="{int(state.get("sleep_timer_generation") or 0)}"/>'
        if key == "alarm_running":
            return f'<r:AlarmRunning val="{1 if state.get("alarm_running") else 0}"/>'
        if key == "snooze_running":
            return f'<r:SnoozeRunning val="{1 if state.get("snooze_running") else 0}"/>'
        if key == "volume":
            # A real speaker reports every channel; only Master is the room
            # level — the driver must ignore LF/RF, and this is what proves it.
            vol = state.get("volume", 25)
            return (
                f'<Volume channel="Master" val="{vol}"/>'
                '<Volume channel="LF" val="100"/>'
                '<Volume channel="RF" val="100"/>'
            )
        if key == "mute":
            muted = "1" if state.get("mute") else "0"
            return (
                f'<Mute channel="Master" val="{muted}"/>'
                '<Mute channel="LF" val="0"/>'
                '<Mute channel="RF" val="0"/>'
            )
        if key == "bass":
            return f'<Bass val="{state.get("bass", 0)}"/>'
        if key == "treble":
            return f'<Treble val="{state.get("treble", 0)}"/>'
        if key == "loudness":
            return f'<Loudness channel="Master" val="{1 if state.get("loudness") else 0}"/>'
        if key == "night_mode":
            return f'<NightMode val="{1 if state.get("night_mode") else 0}"/>'
        if key == "dialog_level":
            return f'<DialogLevel val="{int(state.get("dialog_level") or 0)}"/>'
        if key == "audio_delay":
            return f'<AudioDelay val="{int(state.get("audio_delay") or 0)}"/>'
        return ""

    def _track_didl(self) -> str:
        """The current track's DIDL (empty with nothing loaded). Line-in and
        the TV input give a title of one space, as a Sonos Amp does."""
        state = self.state
        source = state.get("source")
        if source in ("line_in", "tv"):
            return f'<DIDL-Lite {_DIDL_NS}><item id="-1" parentID="-1"><dc:title> </dc:title></item></DIDL-Lite>'
        if source == "none" or (source == "queue" and not state.get("queue_length")):
            return ""
        return (
            f'<DIDL-Lite {_DIDL_NS}><item id="-1" parentID="-1" restricted="true">'
            f"<dc:title>{escape(state.get('track_title') or '')}</dc:title>"
            f"<dc:creator>{escape(state.get('track_artist') or '')}</dc:creator>"
            f"<upnp:album>{escape(state.get('track_album') or '')}</upnp:album>"
            f"<upnp:albumArtURI>/getaa?s=1&amp;u=sim-track-{int(state.get('track_number') or 0)}</upnp:albumArtURI>"
            "</item></DIDL-Lite>"
        )

    def _source_didl(self) -> str:
        title = self.state.get("source_name") or ""
        if not title:
            return ""
        return f'<DIDL-Lite {_DIDL_NS}><item id="-1" parentID="-1"><dc:title>{escape(title)}</dc:title></item></DIDL-Lite>'

    def set_state(self, key: str, value) -> None:
        """Update state and NOTIFY every subscriber of the owning service.

        Real speakers event on change, from any source — the Sonos app, the
        buttons on the unit, or a SOAP command from us. Driving a control in
        the Simulator UI therefore pushes to the driver exactly as hardware
        would.
        """
        old = self.state.get(key)
        super().set_state(key, value)
        if old == value or not self._subscriptions:
            return
        if key in _AVT_KEYS:
            self._event("AVTransport", {key})
        if key in _RCS_KEYS:
            self._event("RenderingControl", {key})
        if key in _GRC_KEYS:
            self._event("GroupRenderingControl")
        if key in _DP_KEYS:
            self._event("DeviceProperties")
        if key in _AUDIOIN_KEYS:
            self._event("AudioIn")
        if key == "grouped":
            self._event("ZoneGroupTopology")

    # ── The household ──

    def _topology(self) -> str:
        def member(uuid: str, name: str) -> str:
            location = f"http://127.0.0.1:{self.port or 1400}/xml/device_description.xml"
            return (
                f'<ZoneGroupMember UUID="{uuid}" Location="{location}" '
                f'ZoneName="{escape(name)}" Icon="" Configuration="1" Invisible="0"/>'
            )

        me = member(_UUID, self.state.get("speaker_name") or "Living Room")
        kitchen = member(_KITCHEN, "Kitchen")
        if self.state.get("grouped"):
            leader = self._group_leader
            members = me + kitchen if leader == _UUID else kitchen + me
            groups = f'<ZoneGroup Coordinator="{leader}" ID="{leader}:1">{members}</ZoneGroup>'
        else:
            groups = (
                f'<ZoneGroup Coordinator="{_UUID}" ID="{_UUID}:1">{me}</ZoneGroup>'
                f'<ZoneGroup Coordinator="{_KITCHEN}" ID="{_KITCHEN}:1">{kitchen}</ZoneGroup>'
            )
        return (
            f"<ZoneGroupState><ZoneGroups>{groups}</ZoneGroups>"
            "<VanishedDevices></VanishedDevices></ZoneGroupState>"
        )

    def _group(self, leader: str | None) -> None:
        if leader is None:
            self._group_leader = _UUID
            self.set_state("grouped", False)
            return
        changed = self.state.get("grouped") and self._group_leader != leader
        self._group_leader = leader
        if changed:
            self._event("ZoneGroupTopology")
        self.set_state("grouped", True)

    def _group_volume(self) -> tuple[int, bool]:
        volume = int(self.state.get("volume") or 0)
        mute = bool(self.state.get("mute"))
        if not self.state.get("grouped"):
            return volume, mute
        return (volume + int(self.state.get("kitchen_volume") or 0)) // 2, mute

    # ── The sleep timer ──

    def _sleep_remaining(self) -> int | None:
        if self._sleep_deadline is None:
            return None
        return max(0, int(round(self._sleep_deadline - time.monotonic())))

    def _configure_sleep_timer(self, seconds: int | None) -> None:
        if self._sleep_task is not None:
            self._sleep_task.cancel()
            self._sleep_task = None
        self._sleep_deadline = None if seconds is None else time.monotonic() + seconds
        if seconds is not None:
            self._sleep_task = asyncio.ensure_future(self._sleep_then_stop(seconds))
        # A cancelled timer reads generation 0 again (as a Sonos Amp does).
        self.set_state(
            "sleep_timer_generation",
            0 if seconds is None else int(self.state.get("sleep_timer_generation") or 0) + 1,
        )

    async def _sleep_then_stop(self, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        self._sleep_deadline = None
        self._sleep_task = None
        self.set_state("transport_state", "stopped")
        self.set_state("sleep_timer_generation", 0)

    # ── Fades ──

    async def _ramp(self, target: int, seconds: float) -> None:
        start = int(self.state.get("volume") or 0)
        steps = max(1, abs(target - start))
        try:
            for index in range(1, steps + 1):
                await asyncio.sleep(seconds / steps)
                self.set_state("volume", start + round((target - start) * index / steps))
        except asyncio.CancelledError:
            return

    # ── AVTransport handlers ──

    def _load(self, uri: str, metadata: str) -> int | None:
        """Point the transport at a URI. Returns a UPnP error code, or None."""
        if uri.startswith("x-rincon:"):
            leader = uri.split(":", 1)[1]
            if leader == _KITCHEN:
                self._group(_KITCHEN)  # this room joins the Kitchen
            elif leader == _UUID:
                self._group(_UUID)  # the Kitchen joins this room
            else:
                return 714
            return None
        if self.state.get("grouped") and self._group_leader == _KITCHEN:
            # This room following the Kitchen picks a source of its own:
            # it leaves the group first.
            self._group(None)
        self._uri = uri
        if uri.startswith("x-rincon-queue:"):
            self.set_state("source_name", "")
            self.set_state("source", "queue")
        elif uri.startswith("x-rincon-stream:"):
            self.set_state("source_name", "")
            self.set_state("source", "line_in")
            self.set_state("transport_state", "paused")
        elif uri.startswith("x-sonos-htastream:"):
            self.set_state("source_name", "")
            self.set_state("source", "tv")
            self.set_state("transport_state", "playing")
        elif uri.startswith(("http://", "https://", "x-sonosapi-radio:")):
            title = _didl_title(metadata) or uri
            self.set_state("source_name", title)
            self.set_state("track_title", title)
            self.set_state("source", "stream")
        elif uri == "":
            self.set_state("source_name", "")
            self.set_state("source", "none")
        else:
            return 714
        # A speaker that changes source events the new source's track with it
        # (on a Sonos Amp, line-in's one-space title, its NumberOfTracks 1).
        self._event("AVTransport", {"track_title", "queue_length", "track_number"})
        return None

    def _handle_av_transport(self, action: str, body: str) -> tuple[int, str]:
        state = self.state
        source = state.get("source")
        actions = self._transport_actions()
        ok = 200, _soap_response("AVTransport", action, "")

        if action == "SetAVTransportURI":
            code = self._load(
                _extract_xml_value(body, "CurrentURI") or "",
                _extract_xml_value(body, "CurrentURIMetaData") or "",
            )
            return _fault(code) if code else ok

        if action == "BecomeCoordinatorOfStandaloneGroup":
            self._group(None)
            return 200, _soap_response(
                "AVTransport", action,
                f"<DelegatedGroupCoordinatorID></DelegatedGroupCoordinatorID><NewGroupID>{_UUID}:2</NewGroupID>",
            )

        if action == "Play":
            if "Play" not in actions:
                return _fault(701)
            self.set_state("transport_state", "playing")
            return ok

        if action == "Pause":
            if "Pause" not in actions:
                return _fault(701)
            self.set_state("transport_state", "paused")
            return ok

        if action == "Stop":
            if "Stop" not in actions:
                return _fault(701)
            self.set_state("transport_state", "stopped")
            if state.get("alarm_running"):
                self.set_state("alarm_running", False)
                self.set_state("snooze_running", False)
            return ok

        if action in ("Next", "Previous"):
            if action not in actions:
                return _fault(701)
            step = 1 if action == "Next" else -1
            self.set_state("track_number", int(state.get("track_number") or 1) + step)
            return ok

        if action == "Seek":
            unit = _extract_xml_value(body, "Unit")
            target = _extract_xml_value(body, "Target") or ""
            if unit == "TRACK_NR":
                if source != "queue":
                    return _fault(710)
                number = int(target) if target.isdigit() else 0
                if not 1 <= number <= int(state.get("queue_length") or 0):
                    return _fault(711)
                self.set_state("track_number", number)
                super().set_state("track_position", "0:00:00")
                return ok
            if unit == "REL_TIME":
                if "X_DLNA_SeekTime" not in actions:
                    return _fault(701)
                seconds = _seconds(target)
                if seconds is None:
                    return _fault(711)
                duration = _seconds(state.get("track_duration") or "") or 0
                if seconds > duration:
                    # Past the end: the speaker runs on to the next track
                    # (a Sonos Amp did not refuse it).
                    length = int(state.get("queue_length") or 0)
                    number = int(state.get("track_number") or 1)
                    if number < length:
                        self.set_state("track_number", number + 1)
                    else:
                        self.set_state("track_number", 1)
                        self.set_state("transport_state", "stopped")
                    super().set_state("track_position", "0:00:00")
                    return ok
                super().set_state("track_position", _hms(seconds))
                return ok
            return _fault(710)

        if action == "AddURIToQueue":
            uri = _extract_xml_value(body, "EnqueuedURI") or ""
            match = re.match(r"^file:///jffs/settings/savedqueues\.rsq#(\d+)$", uri)
            if match:
                playlist = self._playlists.get(f"SQ:{match.group(1)}")
                if playlist is None:
                    return _fault(800)
                added = playlist["tracks"]
            elif uri:
                added = 1
            else:
                return _fault(800)
            first = int(state.get("queue_length") or 0) + 1
            self.set_state("queue_length", first - 1 + added)
            return 200, _soap_response(
                "AVTransport", action,
                f"<FirstTrackNumberEnqueued>{first}</FirstTrackNumberEnqueued>"
                f"<NumTracksAdded>{added}</NumTracksAdded>"
                f"<NewQueueLength>{first - 1 + added}</NewQueueLength>",
            )

        if action == "RemoveAllTracksFromQueue":
            if source == "queue":
                self.set_state("transport_state", "stopped")
            self.set_state("queue_length", 0)
            self.set_state("track_number", 0)
            return ok

        if action == "SaveQueue":
            title = _extract_xml_value(body, "Title") or ""
            number = max((int(key[3:]) for key in self._playlists), default=-1) + 1
            self._playlists[f"SQ:{number}"] = {
                "title": title, "tracks": int(state.get("queue_length") or 0),
            }
            self._content_changed("playlists")
            return 200, _soap_response(
                "AVTransport", action, f"<AssignedObjectID>SQ:{number}</AssignedObjectID>",
            )

        if action == "SetPlayMode":
            mode = _extract_xml_value(body, "NewPlayMode")
            if mode not in _PLAY_MODES:
                return _fault(402)
            if self._uri.startswith("x-sonosapi-radio:") or source in ("line_in", "tv"):
                # A station has no queue to repeat or shuffle (measured on a
                # Sonos Amp: UPnP 712 while a radio stream plays).
                return _fault(712)
            self.set_state("play_mode", mode)
            return ok

        if action == "SetCrossfadeMode":
            mode = _extract_xml_value(body, "CrossfadeMode")
            if mode is None:
                return _fault(402)
            self.set_state("crossfade", mode == "1")
            return ok

        if action == "GetCrossfadeMode":
            mode = "1" if state.get("crossfade") else "0"
            return 200, _soap_response(
                "AVTransport", action, f"<CrossfadeMode>{mode}</CrossfadeMode>"
            )

        if action == "GetTransportSettings":
            fields = (
                f"<PlayMode>{state.get('play_mode', 'NORMAL')}</PlayMode>"
                "<RecQualityMode>NOT_IMPLEMENTED</RecQualityMode>"
            )
            return 200, _soap_response("AVTransport", action, fields)

        if action == "ConfigureSleepTimer":
            duration = _extract_xml_value(body, "NewSleepTimerDuration")
            if duration is None:
                return _fault(402)
            if duration == "":
                self._configure_sleep_timer(None)
            else:
                seconds = _seconds(duration)
                if seconds is None or seconds >= 24 * 3600:
                    return _fault(402)
                self._configure_sleep_timer(seconds)
            return ok

        if action == "GetRemainingSleepTimerDuration":
            remaining = self._sleep_remaining()
            fields = (
                "<RemainingSleepTimerDuration>"
                f"{'' if remaining is None else _hms(remaining)}"
                "</RemainingSleepTimerDuration>"
                "<CurrentSleepTimerGeneration>"
                f"{int(state.get('sleep_timer_generation') or 0)}"
                "</CurrentSleepTimerGeneration>"
            )
            return 200, _soap_response("AVTransport", action, fields)

        if action == "SnoozeAlarm":
            if not state.get("alarm_running"):
                return _fault(701)
            self.set_state("snooze_running", True)
            self.set_state("transport_state", "paused")
            return ok

        if action == "GetCurrentTransportActions":
            return 200, _soap_response("AVTransport", action, f"<Actions>{actions}</Actions>")

        if action == "GetTransportInfo":
            transport = _STATE_TO_SOAP.get(state.get("transport_state", "stopped"), "STOPPED")
            fields = (
                f"<CurrentTransportState>{transport}</CurrentTransportState>"
                "<CurrentTransportStatus>OK</CurrentTransportStatus>"
                "<CurrentSpeed>1</CurrentSpeed>"
            )
            return 200, _soap_response("AVTransport", action, fields)

        if action == "GetMediaInfo":
            fields = (
                f"<NrTracks>{int(state.get('queue_length') or 0) if source == 'queue' else 1}</NrTracks>"
                "<MediaDuration>NOT_IMPLEMENTED</MediaDuration>"
                f"<CurrentURI>{escape(self._uri)}</CurrentURI>"
                f"<CurrentURIMetaData>{escape(self._source_didl())}</CurrentURIMetaData>"
                "<NextURI></NextURI><NextURIMetaData></NextURIMetaData>"
                "<PlayMedium>NETWORK</PlayMedium>"
                "<RecordMedium>NOT_IMPLEMENTED</RecordMedium>"
                "<WriteStatus>NOT_IMPLEMENTED</WriteStatus>"
            )
            return 200, _soap_response("AVTransport", action, fields)

        if action == "GetPositionInfo":
            duration = state.get("track_duration") or "0:00:00"
            position = state.get("track_position") or "0:00:00"
            fields = (
                f"<Track>{int(state.get('track_number') or 0)}</Track>"
                f"<TrackDuration>{duration}</TrackDuration>"
                f"<TrackMetaData>{escape(self._track_didl())}</TrackMetaData>"
                "<TrackURI>x-sonos-http:sim-track.mp3</TrackURI>"
                f"<RelTime>{position}</RelTime>"
                "<AbsTime>NOT_IMPLEMENTED</AbsTime>"
                "<RelCount>2147483647</RelCount>"
                "<AbsCount>2147483647</AbsCount>"
            )
            return 200, _soap_response("AVTransport", action, fields)

        return ok

    # ── RenderingControl handlers ──

    def _handle_rendering_control(self, action: str, body: str) -> tuple[int, str]:
        ok = 200, _soap_response("RenderingControl", action, "")
        if action == "SetVolume":
            desired = _extract_xml_value(body, "DesiredVolume")
            if desired is None or not desired.isdigit():
                return _fault(402)
            self.set_state("volume", max(0, min(100, int(desired))))
            return ok

        if action == "SetRelativeVolume":
            try:
                step = int(_extract_xml_value(body, "Adjustment") or "")
            except ValueError:
                return _fault(402)
            level = max(0, min(100, int(self.get_state("volume", 25)) + step))
            self.set_state("volume", level)
            return 200, _soap_response(
                "RenderingControl", action, f"<NewVolume>{level}</NewVolume>"
            )

        if action == "RampToVolume":
            desired = _extract_xml_value(body, "DesiredVolume") or ""
            ramp = _extract_xml_value(body, "RampType")
            if not desired.isdigit() or ramp not in (
                "SLEEP_TIMER_RAMP_TYPE", "ALARM_RAMP_TYPE", "AUTOPLAY_RAMP_TYPE",
            ):
                return _fault(402)
            if self._ramp_task is not None:
                self._ramp_task.cancel()
            self._ramp_task = asyncio.ensure_future(
                self._ramp(max(0, min(100, int(desired))), 2.0)
            )
            return 200, _soap_response("RenderingControl", action, "<RampTime>8</RampTime>")

        if action == "GetVolume":
            return 200, _soap_response(
                "RenderingControl", action, f"<CurrentVolume>{self.get_state('volume', 25)}</CurrentVolume>"
            )

        if action == "SetMute":
            desired = _extract_xml_value(body, "DesiredMute")
            if desired is not None:
                self.set_state("mute", desired == "1")
            return ok

        if action == "GetMute":
            mute = "1" if self.get_state("mute", False) else "0"
            return 200, _soap_response("RenderingControl", action, f"<CurrentMute>{mute}</CurrentMute>")

        for name, key in (("Bass", "bass"), ("Treble", "treble")):
            if action == f"Set{name}":
                try:
                    self.set_state(key, max(-10, min(10, int(_extract_xml_value(body, f"Desired{name}") or ""))))
                except ValueError:
                    return _fault(402)
                return ok
            if action == f"Get{name}":
                return 200, _soap_response(
                    "RenderingControl", action, f"<Current{name}>{self.get_state(key, 0)}</Current{name}>"
                )

        if action == "SetLoudness":
            desired = _extract_xml_value(body, "DesiredLoudness")
            if desired is not None:
                self.set_state("loudness", desired == "1")
            return ok

        if action == "GetLoudness":
            loud = "1" if self.get_state("loudness", True) else "0"
            return 200, _soap_response("RenderingControl", action, f"<CurrentLoudness>{loud}</CurrentLoudness>")

        if action in ("GetEQ", "SetEQ"):
            key = _EQ.get(_extract_xml_value(body, "EQType") or "")
            if key is None:
                return _fault(402)
            if action == "GetEQ":
                value = self.state.get(key)
                value = (1 if value else 0) if isinstance(value, bool) else int(value or 0)
                return 200, _soap_response("RenderingControl", action, f"<CurrentValue>{value}</CurrentValue>")
            try:
                desired = int(_extract_xml_value(body, "DesiredValue") or "")
            except ValueError:
                return _fault(402)
            if key == "night_mode":
                self.set_state(key, desired != 0)
            elif key == "audio_delay":
                if not 0 <= desired <= 5:
                    # A Sonos Amp refuses 6 with 501.
                    return _fault(501)
                self.set_state(key, desired)
            else:
                self.set_state(key, desired)
            return ok

        if action == "GetRoomCalibrationStatus":
            # A Sonos Amp has no Trueplay tuning (RoomCalibrationAvailable 0).
            return 200, _soap_response(
                "RenderingControl", action,
                "<RoomCalibrationEnabled>0</RoomCalibrationEnabled>"
                "<RoomCalibrationAvailable>0</RoomCalibrationAvailable>",
            )

        return ok

    # ── GroupRenderingControl ──

    def _handle_group_rendering_control(self, action: str, body: str) -> tuple[int, str]:
        """Group volume and mute, as the group's coordinator answers them.

        A real follower refuses these (701, a Sonos One following an Amp);
        the driver asks the coordinator, and in this household the Kitchen's
        address is this simulator's, so every request is answered as the
        coordinator would answer it.
        """
        volume, mute = self._group_volume()
        if action == "GetGroupVolume":
            return 200, _soap_response(
                "GroupRenderingControl", action, f"<CurrentVolume>{volume}</CurrentVolume>"
            )
        if action == "GetGroupMute":
            return 200, _soap_response(
                "GroupRenderingControl", action, f"<CurrentMute>{1 if mute else 0}</CurrentMute>"
            )
        if action in ("SetGroupVolume", "SetRelativeGroupVolume"):
            arg = "DesiredVolume" if action == "SetGroupVolume" else "Adjustment"
            try:
                value = int(_extract_xml_value(body, arg) or "")
            except ValueError:
                return _fault(402)
            target = value if action == "SetGroupVolume" else volume + value
            target = max(0, min(100, target))
            # Keep the rooms' balance: shift both by the same amount.
            shift = target - volume
            if self.state.get("grouped"):
                super().set_state(
                    "kitchen_volume",
                    max(0, min(100, int(self.state.get("kitchen_volume") or 0) + shift)),
                )
            self.set_state("volume", max(0, min(100, int(self.state.get("volume") or 0) + shift)))
            self._event("GroupRenderingControl")
            fields = (
                f"<NewVolume>{self._group_volume()[0]}</NewVolume>"
                if action == "SetRelativeGroupVolume" else ""
            )
            return 200, _soap_response("GroupRenderingControl", action, fields)
        if action == "SetGroupMute":
            self.set_state("mute", (_extract_xml_value(body, "DesiredMute") or "0") == "1")
            return 200, _soap_response("GroupRenderingControl", action, "")
        if action == "SnapshotGroupVolume":
            return 200, _soap_response("GroupRenderingControl", action, "")
        return _fault(401)

    # ── DeviceProperties ──

    def _handle_device_properties(self, action: str, body: str) -> tuple[int, str]:
        if action == "GetZoneAttributes":
            name = self.get_state("speaker_name", "Sonos")
            fields = (
                f"<CurrentZoneName>{escape(str(name))}</CurrentZoneName>"
                "<CurrentIcon></CurrentIcon>"
                "<CurrentConfiguration>1</CurrentConfiguration>"
                "<CurrentTargetRoomName></CurrentTargetRoomName>"
            )
            return 200, _soap_response("DeviceProperties", action, fields)

        if action == "GetZoneInfo":
            fields = (
                "<SerialNumber>00-00-5C-00-14-00:S</SerialNumber>"
                "<SoftwareVersion>97.2-81220</SoftwareVersion>"
                "<DisplaySoftwareVersion>27.1</DisplaySoftwareVersion>"
                "<HardwareVersion>1.24.1.12-1.2</HardwareVersion>"
                "<IPAddress>127.0.0.1</IPAddress>"
                "<MACAddress>00:00:5C:00:14:00</MACAddress>"
                "<CopyrightInfo>© 2003-2026, Sonos, Inc. All rights reserved.</CopyrightInfo>"
                "<ExtraInfo></ExtraInfo>"
                "<HTAudioIn>21</HTAudioIn>"
                "<Flags>0</Flags>"
            )
            return 200, _soap_response("DeviceProperties", action, fields)

        if action == "SetLEDState":
            # A Sonos Amp takes "on" as On, and answers 200 to a word it does
            # not know without changing anything.
            desired = (_extract_xml_value(body, "DesiredLEDState") or "").lower()
            if desired in ("on", "off"):
                self.set_state("status_light", desired == "on")
            return 200, _soap_response("DeviceProperties", action, "")

        if action == "GetLEDState":
            led = "On" if self.get_state("status_light", True) else "Off"
            return 200, _soap_response("DeviceProperties", action, f"<CurrentLEDState>{led}</CurrentLEDState>")

        if action == "SetButtonLockState":
            desired = _extract_xml_value(body, "DesiredButtonLockState")
            if desired not in ("On", "Off"):
                return _fault(402)
            self.set_state("touch_controls_locked", desired == "On")
            return 200, _soap_response("DeviceProperties", action, "")

        if action == "GetButtonLockState":
            lock = "On" if self.get_state("touch_controls_locked", False) else "Off"
            return 200, _soap_response(
                "DeviceProperties", action,
                f"<CurrentButtonLockState>{lock}</CurrentButtonLockState>",
            )

        return 200, _soap_response("DeviceProperties", action, "")

    # ── ZoneGroupTopology ──

    def _handle_topology(self, action: str, body: str) -> tuple[int, str]:
        if action == "GetZoneGroupState":
            return 200, _soap_response(
                "ZoneGroupTopology", action,
                f"<ZoneGroupState>{escape(self._topology(), quote=False)}</ZoneGroupState>",
            )
        return 200, _soap_response("ZoneGroupTopology", action, "")

    # ── ContentDirectory: favorites and playlists ──

    def _content_changed(self, which: str) -> None:
        self._update_ids[which] += 1
        self._event("ContentDirectory")

    def _handle_content_directory(self, action: str, body: str) -> tuple[int, str]:
        if action == "Browse":
            object_id = _extract_xml_value(body, "ObjectID") or ""
            items = ""
            if object_id == "FV:2":
                for fid, fav in self._favorites.items():
                    meta = (
                        f'<DIDL-Lite {_DIDL_NS}><item id="sim-{fid}" parentID="-1" restricted="true">'
                        f"<dc:title>{escape(fav['title'])}</dc:title>"
                        "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
                        "</item></DIDL-Lite>"
                    )
                    items += (
                        f'<item id="{fid}" parentID="FV:2" restricted="false">'
                        f"<dc:title>{escape(fav['title'])}</dc:title>"
                        "<upnp:class>object.itemobject.item.sonos-favorite</upnp:class>"
                        f"<res>{escape(fav['res'])}</res><r:type>{fav['type']}</r:type>"
                        f"<r:resMD>{escape(meta)}</r:resMD></item>"
                    )
            elif object_id == "SQ:":
                for pid, playlist in self._playlists.items():
                    items += (
                        f'<container id="{pid}" parentID="SQ:" restricted="true">'
                        f"<dc:title>{escape(playlist['title'])}</dc:title>"
                        f'<res protocolInfo="file:*:audio/mpegurl:*">{_PLAYLIST_RES.format(n=pid[3:])}</res>'
                        "<upnp:class>object.container.playlistContainer</upnp:class></container>"
                    )
            result = f"<DIDL-Lite {_DIDL_NS}>{items}</DIDL-Lite>"
            count = items.count("<item ") + items.count("<container ")
            return 200, _soap_response(
                "ContentDirectory", action,
                f"<Result>{escape(result, quote=False)}</Result>"
                f"<NumberReturned>{count}</NumberReturned><TotalMatches>{count}</TotalMatches>"
                "<UpdateID>1</UpdateID>",
            )
        if action == "DestroyObject":
            object_id = _extract_xml_value(body, "ObjectID") or ""
            if object_id in self._playlists:
                del self._playlists[object_id]
                self._content_changed("playlists")
            elif object_id in self._favorites:
                del self._favorites[object_id]
                self._content_changed("favorites")
            else:
                return _fault(701)
            return 200, _soap_response("ContentDirectory", action, "")
        return 200, _soap_response("ContentDirectory", action, "")

    # ── AlarmClock ──

    def _alarm_list(self) -> str:
        rows = "".join(
            "<Alarm " + " ".join(f'{k}="{escape(v)}"' for k, v in alarm.items()) + "/>"
            for alarm in self._alarms.values()
        )
        return f"<Alarms>{rows}</Alarms>"

    def _handle_alarm_clock(self, action: str, body: str) -> tuple[int, str]:
        if action == "ListAlarms":
            return 200, _soap_response(
                "AlarmClock", action,
                f"<CurrentAlarmList>{escape(self._alarm_list(), quote=False)}</CurrentAlarmList>"
                f"<CurrentAlarmListVersion>{_UUID}:{self._update_ids['alarms']}</CurrentAlarmListVersion>",
            )
        if action == "UpdateAlarm":
            aid = _extract_xml_value(body, "ID") or ""
            if aid not in self._alarms:
                return _fault(800)
            fields = {
                "StartTime": "StartLocalTime", "Duration": "Duration", "Recurrence": "Recurrence",
                "Enabled": "Enabled", "RoomUUID": "RoomUUID", "ProgramURI": "ProgramURI",
                "ProgramMetaData": "ProgramMetaData", "PlayMode": "PlayMode", "Volume": "Volume",
                "IncludeLinkedZones": "IncludeLinkedZones",
            }
            values = {attr: _extract_xml_value(body, arg) for attr, arg in fields.items()}
            if any(value is None for value in values.values()):
                # Every field must be sent (a Sonos Amp answers 402).
                return _fault(402)
            self._alarms[aid].update(values)
            self._update_ids["alarms"] += 1
            self._event("AlarmClock")
            return 200, _soap_response("AlarmClock", action, "")
        if action == "DestroyAlarm":
            aid = _extract_xml_value(body, "ID") or ""
            if aid not in self._alarms:
                return _fault(800)
            del self._alarms[aid]
            self._update_ids["alarms"] += 1
            self._event("AlarmClock")
            return 200, _soap_response("AlarmClock", action, "")
        return 200, _soap_response("AlarmClock", action, "")

    # ── AudioIn ──

    def _handle_audio_in(self, action: str, body: str) -> tuple[int, str]:
        if action == "GetAudioInputAttributes":
            return 200, _soap_response(
                "AudioIn", action,
                "<CurrentName>Audio Component</CurrentName><CurrentIcon>AudioComponent</CurrentIcon>",
            )
        return 200, _soap_response("AudioIn", action, "")
