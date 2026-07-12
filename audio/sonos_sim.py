"""
Sonos Speaker — Simulator

Simulates a Sonos speaker via the local UPnP/SOAP API on port 1400.
Handles AVTransport, RenderingControl, and DeviceProperties service
endpoints with proper SOAP envelope formatting, plus UPnP GENA eventing:
SUBSCRIBE / UNSUBSCRIBE on the service Event endpoints, an initial event on
subscribe, and a NOTIFY to every subscriber whenever state changes.

The event payloads mirror a real speaker byte for byte in shape: a
propertyset carrying a single escaped LastChange document, Sonos's
trailing-slash namespaces, per-channel Volume/Mute elements, and the
X-SONOS-SERVICETYPE header. Captured from a Sonos Amp (S16) on firmware
95.1 — see driver-roadmap/reference-docs/sonos-upnp/.

Driver: sonos
Transport: http
"""

import asyncio
import re
from html import escape

from simulator.http_simulator import HTTPSimulator

# Service control endpoints
_AV_TRANSPORT = "/MediaRenderer/AVTransport/Control"
_RENDERING_CONTROL = "/MediaRenderer/RenderingControl/Control"
_DEVICE_PROPERTIES = "/DeviceProperties/Control"

# Service event endpoints (GENA)
_AV_TRANSPORT_EVENT = "/MediaRenderer/AVTransport/Event"
_RENDERING_CONTROL_EVENT = "/MediaRenderer/RenderingControl/Event"

# Service URN fragments (for matching SOAPAction headers)
_AV_TRANSPORT_URN = "AVTransport"
_RENDERING_CONTROL_URN = "RenderingControl"
_DEVICE_PROPERTIES_URN = "DeviceProperties"

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

# DIDL-Lite metadata template for track info (escaped, as the protocol sends it)
_DIDL_TEMPLATE = (
    '&lt;DIDL-Lite xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot;'
    ' xmlns:upnp=&quot;urn:schemas-upnp-org:metadata-1-0/upnp/&quot;'
    ' xmlns=&quot;urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/&quot;&gt;'
    "&lt;item&gt;"
    "&lt;dc:title&gt;{title}&lt;/dc:title&gt;"
    "&lt;dc:creator&gt;{artist}&lt;/dc:creator&gt;"
    "&lt;upnp:album&gt;{album}&lt;/upnp:album&gt;"
    "&lt;upnp:albumArtURI&gt;http://127.0.0.1:1400/art.jpg&lt;/upnp:albumArtURI&gt;"
    "&lt;/item&gt;"
    "&lt;/DIDL-Lite&gt;"
)

# Which service events each state key belongs to.
_AVT_KEYS = {
    "transport_state",
    "play_mode",
    "track_title",
    "track_artist",
    "track_album",
    "track_duration",
}
_RCS_KEYS = {"volume", "mute", "bass", "treble", "loudness"}

# Map internal lowercase state → SOAP protocol uppercase
_STATE_TO_SOAP = {
    "playing": "PLAYING",
    "paused": "PAUSED_PLAYBACK",
    "stopped": "STOPPED",
    "transitioning": "TRANSITIONING",
}


def _soap_response(service: str, action: str, fields: str) -> str:
    """Build a complete SOAP response XML."""
    return _SOAP_RESPONSE.format(service=service, action=action, fields=fields)


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
    """Extract a simple XML tag value from the SOAP body."""
    match = re.search(rf"<{tag}>(.*?)</{tag}>", body)
    if match:
        return match.group(1)
    return None


def _header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup (UPnP field names are not case sensitive)."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


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
            "track_title": "Simulation Track",
            "track_artist": "OpenAVC",
            "track_album": "Test Album",
            "track_duration": "0:03:45",
            "track_position": "0:00:00",
            "speaker_name": "Living Room",
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
            {
                "type": "slider",
                "key": "volume",
                "label": "Volume",
                "min": 0,
                "max": 100,
            },
            {
                "type": "toggle",
                "key": "mute",
                "label": "Mute",
            },
            {
                "type": "slider",
                "key": "bass",
                "label": "Bass",
                "min": -10,
                "max": 10,
            },
            {
                "type": "slider",
                "key": "treble",
                "label": "Treble",
                "min": -10,
                "max": 10,
            },
            {
                "type": "toggle",
                "key": "loudness",
                "label": "Loudness",
            },
            {
                "type": "select",
                "key": "play_mode",
                "label": "Play Mode",
                "options": [
                    "NORMAL",
                    "REPEAT_ALL",
                    "REPEAT_ONE",
                    "SHUFFLE_NOREPEAT",
                    "SHUFFLE",
                    "SHUFFLE_REPEAT_ONE",
                ],
            },
            {
                "type": "indicator",
                "key": "track_title",
                "label": "Track",
            },
            {
                "type": "indicator",
                "key": "track_artist",
                "label": "Artist",
            },
            {
                "type": "indicator",
                "key": "speaker_name",
                "label": "Speaker Name",
            },
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # GENA subscriptions: sid -> {callback, service, seq}
        self._subscriptions: dict[str, dict] = {}
        self._sid_counter = 0

    # ── Request routing ──

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ):
        # GENA eventing lives on the Event endpoints, not the Control ones.
        if method in ("SUBSCRIBE", "UNSUBSCRIBE"):
            return self._handle_gena(method, path, headers)

        if method != "POST":
            return 405, "Method Not Allowed"

        service, action = _extract_soap_action(headers)
        if not service or not action:
            return 400, "Bad Request: missing or invalid SOAPAction header"

        # --- AVTransport ---
        if path == _AV_TRANSPORT and service == _AV_TRANSPORT_URN:
            return self._handle_av_transport(action, body)

        # --- RenderingControl ---
        if path == _RENDERING_CONTROL and service == _RENDERING_CONTROL_URN:
            return self._handle_rendering_control(action, body)

        # --- DeviceProperties ---
        if path == _DEVICE_PROPERTIES and service == _DEVICE_PROPERTIES_URN:
            return self._handle_device_properties(action, body)

        return 404, "Not Found"

    # ── GENA: SUBSCRIBE / UNSUBSCRIBE ──

    def _handle_gena(self, method: str, path: str, headers: dict[str, str]):
        """Subscribe / renew / cancel an event subscription.

        Mirrors UPnP DA 2.0 §4.1: an initial SUBSCRIBE carries CALLBACK + NT
        and gets a fresh SID; a renewal carries SID only (mixing them is a
        400); a SID the speaker doesn't know is a 412. Sonos grants exactly
        the TIMEOUT that was asked for.
        """
        if path not in (_AV_TRANSPORT_EVENT, _RENDERING_CONTROL_EVENT):
            return 404, "Not Found"

        service = "AVTransport" if path == _AV_TRANSPORT_EVENT else "RenderingControl"
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
        new_sid = f"uuid:RINCON_SIM0000001400_sub{self._sid_counter:010d}"
        self._subscriptions[new_sid] = {
            "callback": url,
            "service": service,
            "seq": 0,
        }
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
        service = sub["service"]
        keys = _AVT_KEYS if service == "AVTransport" else _RCS_KEYS
        await self._notify(sid, self._last_change(service, keys))

    async def _notify(self, sid: str, last_change: str) -> None:
        """POST one NOTIFY to a subscriber, with the GENA headers."""
        sub = self._subscriptions.get(sid)
        if not sub:
            return
        seq = sub["seq"]
        sub["seq"] = seq + 1
        body = (
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{escape(last_change)}</LastChange>"
            "</e:property></e:propertyset>"
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

    def _last_change(self, service: str, keys: set[str]) -> str:
        """Render a LastChange document for the given state keys.

        Namespaces carry the trailing slash a real Sonos sends (the spec's
        examples don't) — the driver must be namespace-insensitive, and this
        is what proves it.
        """
        ns = (
            'xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"'
            if service == "AVTransport"
            else 'xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/"'
        )
        vars_xml = "".join(self._variable_xml(key) for key in sorted(keys))
        if not vars_xml:
            return ""
        return (
            f"<Event {ns}>"
            f'<InstanceID val="0">{vars_xml}</InstanceID>'
            "</Event>"
        )

    def _variable_xml(self, key: str) -> str:
        """One LastChange variable element for a state key (or "" if it has
        no evented representation)."""
        state = self.state
        if key == "transport_state":
            value = _STATE_TO_SOAP.get(state.get("transport_state", "stopped"), "STOPPED")
            return f'<TransportState val="{value}"/>'
        if key == "play_mode":
            return f'<CurrentPlayMode val="{state.get("play_mode", "NORMAL")}"/>'
        if key == "track_duration":
            return f'<CurrentTrackDuration val="{state.get("track_duration", "0:00:00")}"/>'
        if key in ("track_title", "track_artist", "track_album"):
            # Title, artist and album all ride in ONE CurrentTrackMetaData blob
            # — the metadata is atomic on a real speaker (a track change events
            # it once, complete). So a change to any of the three emits the
            # whole current blob; editing just the Artist in the Simulator UI
            # still reaches the driver.
            didl = _DIDL_TEMPLATE.format(
                title=state.get("track_title") or "",
                artist=state.get("track_artist") or "",
                album=state.get("track_album") or "",
            )
            return f'<CurrentTrackMetaData val="{didl}"/>'
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
        return ""

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
            service, keys = "AVTransport", {key}
        elif key in _RCS_KEYS:
            service, keys = "RenderingControl", {key}
        else:
            return  # track_position / speaker_name are not evented variables

        last_change = self._last_change(service, keys)
        if not last_change:
            return
        for sid, sub in list(self._subscriptions.items()):
            if sub["service"] == service:
                asyncio.ensure_future(self._notify(sid, last_change))

    # ── AVTransport handlers ──

    def _handle_av_transport(self, action: str, body: str) -> tuple[int, str]:
        if action == "Play":
            self.set_state("transport_state", "playing")
            return 200, _soap_response(_AV_TRANSPORT_URN, action, "")

        if action == "Pause":
            self.set_state("transport_state", "paused")
            return 200, _soap_response(_AV_TRANSPORT_URN, action, "")

        if action == "Stop":
            self.set_state("transport_state", "stopped")
            return 200, _soap_response(_AV_TRANSPORT_URN, action, "")

        if action in ("Next", "Previous"):
            return 200, _soap_response(_AV_TRANSPORT_URN, action, "")

        if action == "SetPlayMode":
            mode = _extract_xml_value(body, "NewPlayMode")
            if mode:
                self.set_state("play_mode", mode)
            return 200, _soap_response(_AV_TRANSPORT_URN, action, "")

        if action == "GetTransportInfo":
            state = _STATE_TO_SOAP.get(
                self.get_state("transport_state", "stopped"), "STOPPED"
            )
            fields = (
                f"<CurrentTransportState>{state}</CurrentTransportState>"
                "<CurrentTransportStatus>OK</CurrentTransportStatus>"
                "<CurrentSpeed>1</CurrentSpeed>"
            )
            return 200, _soap_response(_AV_TRANSPORT_URN, action, fields)

        if action == "GetPositionInfo":
            title = self.get_state("track_title", "")
            artist = self.get_state("track_artist", "")
            album = self.get_state("track_album", "")
            duration = self.get_state("track_duration", "0:00:00")
            position = self.get_state("track_position", "0:00:00")

            metadata = _DIDL_TEMPLATE.format(
                title=title, artist=artist, album=album,
            )

            fields = (
                "<Track>1</Track>"
                f"<TrackDuration>{duration}</TrackDuration>"
                f"<TrackMetaData>{metadata}</TrackMetaData>"
                f"<TrackURI>x-rincon-stream:RINCON_00000000000001400</TrackURI>"
                f"<RelTime>{position}</RelTime>"
                "<AbsTime>NOT_IMPLEMENTED</AbsTime>"
                "<RelCount>2147483647</RelCount>"
                "<AbsCount>2147483647</AbsCount>"
            )
            return 200, _soap_response(_AV_TRANSPORT_URN, action, fields)

        return 200, _soap_response(_AV_TRANSPORT_URN, action, "")

    # ── RenderingControl handlers ──

    def _handle_rendering_control(self, action: str, body: str) -> tuple[int, str]:
        if action == "SetVolume":
            desired = _extract_xml_value(body, "DesiredVolume")
            if desired is not None and desired.isdigit():
                self.set_state("volume", max(0, min(100, int(desired))))
            return 200, _soap_response(_RENDERING_CONTROL_URN, action, "")

        if action == "GetVolume":
            vol = self.get_state("volume", 25)
            fields = f"<CurrentVolume>{vol}</CurrentVolume>"
            return 200, _soap_response(_RENDERING_CONTROL_URN, action, fields)

        if action == "SetMute":
            desired = _extract_xml_value(body, "DesiredMute")
            if desired is not None:
                self.set_state("mute", desired == "1")
            return 200, _soap_response(_RENDERING_CONTROL_URN, action, "")

        if action == "GetMute":
            mute = self.get_state("mute", False)
            fields = f"<CurrentMute>{'1' if mute else '0'}</CurrentMute>"
            return 200, _soap_response(_RENDERING_CONTROL_URN, action, fields)

        if action == "SetBass":
            desired = _extract_xml_value(body, "DesiredBass")
            if desired is not None:
                try:
                    self.set_state("bass", max(-10, min(10, int(desired))))
                except ValueError:
                    pass
            return 200, _soap_response(_RENDERING_CONTROL_URN, action, "")

        if action == "GetBass":
            fields = f"<CurrentBass>{self.get_state('bass', 0)}</CurrentBass>"
            return 200, _soap_response(_RENDERING_CONTROL_URN, action, fields)

        if action == "SetTreble":
            desired = _extract_xml_value(body, "DesiredTreble")
            if desired is not None:
                try:
                    self.set_state("treble", max(-10, min(10, int(desired))))
                except ValueError:
                    pass
            return 200, _soap_response(_RENDERING_CONTROL_URN, action, "")

        if action == "GetTreble":
            fields = f"<CurrentTreble>{self.get_state('treble', 0)}</CurrentTreble>"
            return 200, _soap_response(_RENDERING_CONTROL_URN, action, fields)

        if action == "SetLoudness":
            desired = _extract_xml_value(body, "DesiredLoudness")
            if desired is not None:
                self.set_state("loudness", desired == "1")
            return 200, _soap_response(_RENDERING_CONTROL_URN, action, "")

        if action == "GetLoudness":
            loud = "1" if self.get_state("loudness", True) else "0"
            fields = f"<CurrentLoudness>{loud}</CurrentLoudness>"
            return 200, _soap_response(_RENDERING_CONTROL_URN, action, fields)

        return 200, _soap_response(_RENDERING_CONTROL_URN, action, "")

    # ── DeviceProperties handlers ──

    def _handle_device_properties(self, action: str, body: str) -> tuple[int, str]:
        if action == "GetZoneAttributes":
            name = self.get_state("speaker_name", "Sonos")
            fields = (
                f"<CurrentZoneName>{name}</CurrentZoneName>"
                "<CurrentIcon>/img/icon-S1.png</CurrentIcon>"
                "<CurrentConfiguration>1</CurrentConfiguration>"
            )
            return 200, _soap_response(_DEVICE_PROPERTIES_URN, action, fields)

        return 200, _soap_response(_DEVICE_PROPERTIES_URN, action, "")
