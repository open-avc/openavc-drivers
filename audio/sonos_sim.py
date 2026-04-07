"""
Sonos Speaker — Simulator

Simulates a Sonos speaker via the local UPnP/SOAP API on port 1400.
Handles AVTransport, RenderingControl, and DeviceProperties service
endpoints with proper SOAP envelope formatting.

Driver: sonos
Transport: http
"""

import re

from simulator.http_simulator import HTTPSimulator

# Service endpoints
_AV_TRANSPORT = "/MediaRenderer/AVTransport/Control"
_RENDERING_CONTROL = "/MediaRenderer/RenderingControl/Control"
_DEVICE_PROPERTIES = "/DeviceProperties/Control"

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

# DIDL-Lite metadata template for track info
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
    # Extract service name and action from the URN
    # Format: urn:schemas-upnp-org:service:{Service}:1#{Action}
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

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
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

    # ── AVTransport handlers ──

    # Map internal lowercase state → SOAP protocol uppercase
    _STATE_TO_SOAP = {
        "playing": "PLAYING",
        "paused": "PAUSED_PLAYBACK",
        "stopped": "STOPPED",
        "transitioning": "TRANSITIONING",
    }

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

        if action == "GetTransportInfo":
            state = self._STATE_TO_SOAP.get(
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
                level = max(0, min(100, int(desired)))
                self.set_state("volume", level)
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
            mute_val = "1" if mute else "0"
            fields = f"<CurrentMute>{mute_val}</CurrentMute>"
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
