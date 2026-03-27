"""
OpenAVC Sonos Speaker Driver.

Controls Sonos speakers via the local UPnP/SOAP API on port 1400.
Works with all Sonos models (S1 and S2 firmware). No authentication
required — any device on the same network can control the speaker.

Protocol: HTTP POST with SOAP/XML payloads to port 1400.
Services used:
  - AVTransport: play, pause, stop, next, previous, track info
  - RenderingControl: volume, mute, bass, treble
  - DeviceProperties: speaker name, LED state

Reference: https://sonos.svrooij.io/services/
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)

# SOAP envelope template
_SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>"
    '<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
    "{params}"
    "</u:{action}>"
    "</s:Body>"
    "</s:Envelope>"
)

# Service endpoints
_AV_TRANSPORT = "/MediaRenderer/AVTransport/Control"
_RENDERING_CONTROL = "/MediaRenderer/RenderingControl/Control"
_DEVICE_PROPERTIES = "/DeviceProperties/Control"

# Service type URNs
_AV_TRANSPORT_URN = "AVTransport"
_RENDERING_CONTROL_URN = "RenderingControl"
_DEVICE_PROPERTIES_URN = "DeviceProperties"

# Transport state mapping
_TRANSPORT_STATES = {
    "PLAYING": "playing",
    "PAUSED_PLAYBACK": "paused",
    "STOPPED": "stopped",
    "TRANSITIONING": "transitioning",
    "NO_MEDIA_PRESENT": "stopped",
}


def _build_soap(service: str, action: str, **params: str) -> tuple[str, str]:
    """Build a SOAP request body and SOAPAction header.

    Returns (body_xml, soap_action_header).
    """
    param_xml = ""
    for key, val in params.items():
        # Escape XML special characters
        escaped = (
            str(val)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        param_xml += f"<{key}>{escaped}</{key}>"

    body = _SOAP_ENVELOPE.format(
        service=service, action=action, params=param_xml
    )
    soap_action = f'"urn:schemas-upnp-org:service:{service}:1#{action}"'
    return body, soap_action


def _parse_xml_value(xml_text: str, tag: str) -> str | None:
    """Extract a value from a SOAP XML response by tag name."""
    # Simple tag extraction — works for flat SOAP responses
    start = xml_text.find(f"<{tag}>")
    if start == -1:
        # Try with namespace prefix
        for prefix in ("u:", ""):
            start = xml_text.find(f"<{prefix}{tag}>")
            if start != -1:
                end_tag = f"</{prefix}{tag}>"
                end = xml_text.find(end_tag, start)
                if end != -1:
                    value_start = start + len(f"<{prefix}{tag}>")
                    return xml_text[value_start:end]
        return None
    end_tag = f"</{tag}>"
    end = xml_text.find(end_tag, start)
    if end == -1:
        return None
    value_start = start + len(f"<{tag}>")
    return xml_text[value_start:end]


def _parse_didl_metadata(metadata_xml: str) -> dict[str, str | None]:
    """Parse DIDL-Lite metadata XML for track info.

    Sonos returns track metadata as escaped XML inside the SOAP response.
    """
    result: dict[str, str | None] = {
        "title": None,
        "artist": None,
        "album": None,
        "album_art": None,
    }

    if not metadata_xml or metadata_xml == "NOT_IMPLEMENTED":
        return result

    # Unescape the XML (it's often HTML-escaped inside the SOAP response)
    unescaped = (
        metadata_xml.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )

    try:
        root = ET.fromstring(unescaped)
        for item in root.iter():
            tag = item.tag.split("}")[-1] if "}" in item.tag else item.tag
            if tag == "title" and not result["title"]:
                result["title"] = item.text
            elif tag == "creator" and not result["artist"]:
                result["artist"] = item.text
            elif tag == "album" and not result["album"]:
                result["album"] = item.text
            elif tag == "albumArtURI" and not result["album_art"]:
                result["album_art"] = item.text
    except ET.ParseError:
        pass

    return result


class SonosDriver(BaseDriver):
    """Sonos speaker control driver via local UPnP/SOAP API."""

    DRIVER_INFO = {
        "id": "sonos",
        "name": "Sonos Speaker",
        "manufacturer": "Sonos",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls Sonos speakers via the local UPnP API. "
            "Play/pause, volume, mute, track info, and transport control."
        ),
        "transport": "http",
        "help": {
            "overview": (
                "Controls any Sonos speaker on the local network via UPnP. "
                "Works with all Sonos models including One, Five, Beam, Arc, "
                "Era, Move, Roam, Port, Amp, and legacy Play/Connect models. "
                "No cloud account or API key required."
            ),
            "setup": (
                "1. Ensure the Sonos speaker is on the same network\n"
                "2. Enter the speaker's IP address (find it in the Sonos app "
                "under Settings > System > About)\n"
                "3. Default port is 1400 (do not change)\n"
                "4. UPnP control must be enabled on the speaker (on by default)"
            ),
        },
        "default_config": {
            "host": "",
            "port": 1400,
            "poll_interval": 2,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 1400, "label": "Port"},
            "poll_interval": {
                "type": "integer",
                "default": 2,
                "min": 1,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "transport_state": {
                "type": "enum",
                "values": ["playing", "paused", "stopped", "transitioning"],
                "label": "Transport State",
            },
            "volume": {"type": "integer", "label": "Volume"},
            "mute": {"type": "boolean", "label": "Mute"},
            "track_title": {"type": "string", "label": "Track Title"},
            "track_artist": {"type": "string", "label": "Track Artist"},
            "track_album": {"type": "string", "label": "Track Album"},
            "track_duration": {"type": "string", "label": "Track Duration"},
            "track_position": {"type": "string", "label": "Track Position"},
            "speaker_name": {"type": "string", "label": "Speaker Name"},
        },
        "commands": {
            "play": {
                "label": "Play",
                "params": {},
                "help": "Start or resume playback.",
            },
            "pause": {
                "label": "Pause",
                "params": {},
                "help": "Pause playback.",
            },
            "stop": {
                "label": "Stop",
                "params": {},
                "help": "Stop playback.",
            },
            "next_track": {
                "label": "Next Track",
                "params": {},
                "help": "Skip to the next track.",
            },
            "previous_track": {
                "label": "Previous Track",
                "params": {},
                "help": "Skip to the previous track.",
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Volume level 0-100.",
                    },
                },
                "help": "Set the speaker volume.",
            },
            "volume_up": {
                "label": "Volume Up",
                "params": {},
                "help": "Increase volume by 5.",
            },
            "volume_down": {
                "label": "Volume Down",
                "params": {},
                "help": "Decrease volume by 5.",
            },
            "mute_on": {
                "label": "Mute",
                "params": {},
                "help": "Mute the speaker.",
            },
            "mute_off": {
                "label": "Unmute",
                "params": {},
                "help": "Unmute the speaker.",
            },
        },
        "discovery": {
            "ports": [1400],
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._base_url: str = ""

    async def connect(self) -> None:
        """Connect to the Sonos speaker."""
        host = self.config.get("host", "")
        port = self.config.get("port", 1400)
        self._base_url = f"http://{host}:{port}"

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=5.0,
        )

        # Verify connection by querying device properties
        try:
            name = await self._get_speaker_name()
            if name:
                self.set_state("speaker_name", name)
                log.info(f"[{self.device_id}] Speaker name: {name}")
        except Exception as e:
            if self._client:
                await self._client.aclose()
                self._client = None
            raise ConnectionError(
                f"Failed to connect to Sonos at {host}:{port}: {e}"
            )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to Sonos at {host}:{port}")

        # Initial status poll
        await self.poll()

        # Start polling
        poll_interval = self.config.get("poll_interval", 2)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        """Disconnect from the Sonos speaker."""
        await self.stop_polling()
        if self._client:
            await self._client.aclose()
            self._client = None
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a command to the Sonos speaker."""
        params = params or {}

        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match command:
            case "play":
                await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "Play",
                    InstanceID="0", Speed="1",
                )
            case "pause":
                await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "Pause",
                    InstanceID="0",
                )
            case "stop":
                await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "Stop",
                    InstanceID="0",
                )
            case "next_track":
                await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "Next",
                    InstanceID="0",
                )
            case "previous_track":
                await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "Previous",
                    InstanceID="0",
                )
            case "set_volume":
                level = max(0, min(100, int(params.get("level", 50))))
                await self._soap_action(
                    _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "SetVolume",
                    InstanceID="0", Channel="Master",
                    DesiredVolume=str(level),
                )
                self.set_state("volume", level)
            case "volume_up":
                current = self.get_state("volume") or 0
                new_level = min(100, current + 5)
                await self._soap_action(
                    _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "SetVolume",
                    InstanceID="0", Channel="Master",
                    DesiredVolume=str(new_level),
                )
                self.set_state("volume", new_level)
            case "volume_down":
                current = self.get_state("volume") or 0
                new_level = max(0, current - 5)
                await self._soap_action(
                    _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "SetVolume",
                    InstanceID="0", Channel="Master",
                    DesiredVolume=str(new_level),
                )
                self.set_state("volume", new_level)
            case "mute_on":
                await self._soap_action(
                    _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "SetMute",
                    InstanceID="0", Channel="Master", DesiredMute="1",
                )
                self.set_state("mute", True)
            case "mute_off":
                await self._soap_action(
                    _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "SetMute",
                    InstanceID="0", Channel="Master", DesiredMute="0",
                )
                self.set_state("mute", False)
            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def poll(self) -> None:
        """Query transport state, volume, mute, and track info."""
        if not self._client:
            return

        try:
            # Transport state
            resp = await self._soap_action(
                _AV_TRANSPORT, _AV_TRANSPORT_URN, "GetTransportInfo",
                InstanceID="0",
            )
            if resp:
                raw_state = _parse_xml_value(resp, "CurrentTransportState")
                if raw_state:
                    state = _TRANSPORT_STATES.get(raw_state, "stopped")
                    old = self.get_state("transport_state")
                    self.set_state("transport_state", state)
                    if state != old:
                        log.info(f"[{self.device_id}] Transport: {state}")

            # Volume
            resp = await self._soap_action(
                _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "GetVolume",
                InstanceID="0", Channel="Master",
            )
            if resp:
                vol_str = _parse_xml_value(resp, "CurrentVolume")
                if vol_str and vol_str.isdigit():
                    self.set_state("volume", int(vol_str))

            # Mute
            resp = await self._soap_action(
                _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "GetMute",
                InstanceID="0", Channel="Master",
            )
            if resp:
                mute_str = _parse_xml_value(resp, "CurrentMute")
                if mute_str is not None:
                    self.set_state("mute", mute_str == "1")

            # Track info (only when playing/paused)
            transport = self.get_state("transport_state")
            if transport in ("playing", "paused"):
                resp = await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "GetPositionInfo",
                    InstanceID="0",
                )
                if resp:
                    duration = _parse_xml_value(resp, "TrackDuration")
                    position = _parse_xml_value(resp, "RelTime")
                    if duration:
                        self.set_state("track_duration", duration)
                    if position:
                        self.set_state("track_position", position)

                    # Parse track metadata
                    metadata = _parse_xml_value(resp, "TrackMetaData")
                    if metadata:
                        info = _parse_didl_metadata(metadata)
                        if info["title"]:
                            old_title = self.get_state("track_title")
                            self.set_state("track_title", info["title"])
                            if info["title"] != old_title:
                                log.info(
                                    f"[{self.device_id}] Now playing: "
                                    f"{info['artist'] or 'Unknown'} "
                                    f"- {info['title']}"
                                )
                        if info["artist"]:
                            self.set_state("track_artist", info["artist"])
                        if info["album"]:
                            self.set_state("track_album", info["album"])
            else:
                # Clear track info when stopped
                self.set_state("track_title", None)
                self.set_state("track_artist", None)
                self.set_state("track_album", None)
                self.set_state("track_duration", None)
                self.set_state("track_position", None)

        except (httpx.ConnectError, httpx.TimeoutException):
            log.warning(
                f"[{self.device_id}] Poll failed — speaker not responding"
            )
        except Exception:
            log.exception(f"[{self.device_id}] Poll error")

    # --- Internal helpers ---

    async def _soap_action(
        self,
        endpoint: str,
        service: str,
        action: str,
        **params: str,
    ) -> str | None:
        """Send a SOAP request and return the response body text."""
        if not self._client:
            return None

        body, soap_action = _build_soap(service, action, **params)

        try:
            log.info(f"[{self.device_id}] SOAP {action}")
            resp = await self._client.post(
                endpoint,
                content=body.encode("utf-8"),
                headers={
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "SOAPAction": soap_action,
                },
            )
            log.info(
                f"[{self.device_id}] SOAP {action} -> {resp.status_code}"
            )

            if resp.status_code == 200:
                return resp.text
            else:
                log.warning(
                    f"[{self.device_id}] SOAP {action} failed: "
                    f"HTTP {resp.status_code}"
                )
                return None
        except httpx.TimeoutException:
            log.warning(f"[{self.device_id}] SOAP {action} timeout")
            return None
        except httpx.ConnectError:
            log.warning(f"[{self.device_id}] SOAP {action} connection error")
            return None

    async def _get_speaker_name(self) -> str | None:
        """Query the speaker name via DeviceProperties."""
        resp = await self._soap_action(
            _DEVICE_PROPERTIES, _DEVICE_PROPERTIES_URN, "GetZoneAttributes",
        )
        if resp:
            return _parse_xml_value(resp, "CurrentZoneName")
        return None
