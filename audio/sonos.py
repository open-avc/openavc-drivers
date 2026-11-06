"""
OpenAVC Sonos Speaker Driver.

Controls Sonos speakers, amps and home theater products through the local
UPnP / SOAP interface every Sonos player serves on port 1400. Works with all
Sonos models (S1 and S2 firmware). No authentication: any device on the same
network can control the speaker.

One OpenAVC device per Sonos room. What it covers:
  - Playback: play, pause, stop, skip, seek, play mode, crossfade, the queue
    (its length and position, play a track from it, clear it), the sleep
    timer, and which of those the current source allows right now.
  - What to play: Sonos favorites, Sonos playlists (play, add to the queue,
    save the queue as one, delete one), a stream URL, the speaker's line-in,
    its TV input.
  - Now playing: title, artist, album, album art, duration, position, what it
    is playing from (the queue, a stream, line-in, TV) and its name.
  - Groups: which rooms play together and which one leads; join a room's group,
    leave the group, add or remove another room; group volume and mute.
  - Volume: level, steps, mute, fades, bass, treble, loudness.
  - Home theater, on a speaker that has it: night sound, speech enhancement,
    audio delay, and the sub and surround levels when those are bonded.
  - Alarms: this room's alarms (turn on or off, delete), whether one is
    playing, snooze.
  - The speaker itself: status light, touch-control lock, Trueplay, model,
    serial number, firmware, MAC address.

  Left out on purpose: anything that changes how the household is set up
  (stereo pairs, bonding a sub or surrounds, renaming a room), firmware
  updates, music-service accounts and diagnostics. The Sonos app does those.

Protocol: HTTP POST with SOAP/XML payloads to port 1400. The services a
speaker offers, and where each one is, come from its own device description
(/xml/device_description.xml, UPnP Device Architecture 2.0 section 2.3), so a
speaker with no line-in is never asked about one (a Sonos One closes the
connection on a service it does not list). The three every Sonos player has
fall back to their known paths if the description cannot be read.

Groups: every room is its own OpenAVC device, and a group is a relationship
between rooms, not a part of one. The household's topology (ZoneGroupTopology)
says which room leads each group. A room that follows another plays what the
leader (the coordinator) plays and refuses transport commands itself (UPnP
701), so this driver sends a follower's transport, queue and play commands to
its coordinator, and shows now playing from the coordinator's own events.
Volume, mute, tone and the speaker's own settings stay with the room.

Push (UPnP GENA):
  The driver SUBSCRIBEs to every service whose state it shows (AVTransport,
  RenderingControl, DeviceProperties, ZoneGroupTopology, ContentDirectory,
  AlarmClock, and AudioIn on a speaker with a line-in) with a CALLBACK URL
  served by the platform's HTTP push listener, plus the coordinator's
  GroupRenderingControl (group volume) and, while following another room, the
  coordinator's AVTransport. The speakers NOTIFY every state change, whether it
  came from OpenAVC, the Sonos app, or the buttons on a speaker. Accepting a
  subscription also delivers an initial event carrying the complete evented
  state, so the driver is in sync the moment it connects.

  Subscriptions expire: a renewal task refreshes each SID at half its granted
  lifetime, and one the speaker no longer recognizes (HTTP 412 after a reboot)
  is re-created from scratch. Polling stays on as the resync baseline and as
  the only source of what the speaker does not event: the track position, the
  sleep timer's remaining time, the status light and the touch-control lock.

Why Python (not YAML):
  SOAP envelopes with per-action SOAPAction headers, the GENA subscription
  lifecycle (SID tracking, renewal timers, UNSUBSCRIBE), subscriptions that
  move to another speaker when the room changes groups, commands sent to the
  group's coordinator, and LastChange payloads that fan a single evented
  variable out into many state keys through two layers of XML escaping. None
  of that fits ConfigurableDriver.

Reference:
  UPnP Device Architecture 2.0 (description, section 2; eventing, section 4)
  and the AVTransport:1 / RenderingControl:1 / ContentDirectory service
  templates, plus the speaker's own SCPD service descriptions for the actions,
  the arguments and the value ranges. Sonos publishes no document for this
  local interface, so what the speaker reports and accepts was read off Sonos
  units and is recorded where each value is used below. Community service docs
  (https://sonos.svrooij.io/services/) cross-check the endpoint paths.
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from openavc.drivers.base import BaseDriver
from openavc.transport import http_listener
from openavc.utils.logger import get_logger

try:  # OpenAVC 0.37.1 and later show the speaker's reason on the screen.
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # pragma: no cover - older platforms
    CommandRefusedError = RuntimeError  # type: ignore[misc,assignment]

log = get_logger(__name__)

# SOAP envelope template. ``service_type`` is the full service type URN the
# speaker's description gives (urn:schemas-upnp-org:service:AVTransport:1).
_SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>"
    '<u:{action} xmlns:u="{service_type}">'
    "{params}"
    "</u:{action}>"
    "</s:Body>"
    "</s:Envelope>"
)

_DESCRIPTION_PATH = "/xml/device_description.xml"

# Where each service is on every Sonos player, used when the speaker's own
# description cannot be read. The description wins when it can.
_DEFAULT_SERVICES: dict[str, dict[str, str]] = {
    "AVTransport": {
        "type": "urn:schemas-upnp-org:service:AVTransport:1",
        "control": "/MediaRenderer/AVTransport/Control",
        "event": "/MediaRenderer/AVTransport/Event",
    },
    "RenderingControl": {
        "type": "urn:schemas-upnp-org:service:RenderingControl:1",
        "control": "/MediaRenderer/RenderingControl/Control",
        "event": "/MediaRenderer/RenderingControl/Event",
    },
    "DeviceProperties": {
        "type": "urn:schemas-upnp-org:service:DeviceProperties:1",
        "control": "/DeviceProperties/Control",
        "event": "/DeviceProperties/Event",
    },
}

# GENA subscriptions on this speaker: label -> service name. The label becomes
# the last segment of the platform's callback path (/api/push/<device>/<label>),
# so each service NOTIFYs to its own URL and the protocol log stays readable. A
# service the speaker's description does not list is not subscribed.
_EVENT_SERVICES = {
    "avt": "AVTransport",
    "rc": "RenderingControl",
    "dp": "DeviceProperties",
    "zgt": "ZoneGroupTopology",
    "cd": "ContentDirectory",
    "alarm": "AlarmClock",
    "audioin": "AudioIn",
}
# Subscriptions on the group's coordinator (this speaker when it leads):
# group volume always, the coordinator's transport while following it.
_GROUP_VOLUME_LABEL = "grc"
_LEADER_TRANSPORT_LABEL = "gavt"

# Requested subscription lifetime. The UPnP spec says a publisher SHOULD grant
# at least 1800 s; Sonos grants exactly what it is asked for (verified on
# hardware), so ask for the spec's floor rather than something exotic.
_SUBSCRIBE_SECONDS = 1800
# Never renew less often than this, whatever the speaker granted.
_MIN_RENEW_SECONDS = 60

# A full resync of the values the speaker events (tone, play mode, crossfade,
# the source, groups, favorites, alarms) every this many polls, so a speaker
# whose events cannot reach OpenAVC (a firewall between them) still shows them
# within a minute or so at the default poll interval. The status light and
# touch-control lock are read on the same cycle: the speaker does not event
# them at all.
_RESYNC_EVERY_POLLS = 12

# Transport state mapping (protocol -> our state)
_TRANSPORT_STATES = {
    "PLAYING": "playing",
    "PAUSED_PLAYBACK": "paused",
    "STOPPED": "stopped",
    "TRANSITIONING": "transitioning",
    "NO_MEDIA_PRESENT": "stopped",
}

# Play modes, from the speaker's AVTransport SCPD allowedValueList.
_PLAY_MODES = [
    "NORMAL",
    "REPEAT_ALL",
    "REPEAT_ONE",
    "SHUFFLE_NOREPEAT",
    "SHUFFLE",
    "SHUFFLE_REPEAT_ONE",
]

# Now-playing fields, cleared together when playback stops.
_TRACK_KEYS = (
    "track_title",
    "track_artist",
    "track_album",
    "track_album_art",
    "track_duration",
    "track_position",
)

# CurrentTransportActions names (a comma list in the AVTransport event) -> the
# state that says the action is available right now. Read off a Sonos Amp: a
# queue in the middle lists "Set, Stop, Pause, Play, X_DLNA_SeekTime, Next,
# Previous, X_DLNA_SeekTrackNr" (Next only before the last track, Previous only
# after the first); a radio stream "Set, Play"; line-in "Set, Stop, Play"; the
# TV input "Set, Play".
_TRANSPORT_ACTION_KEYS = {
    "Pause": "can_pause",
    "Next": "can_skip_next",
    "Previous": "can_skip_previous",
    "X_DLNA_SeekTime": "can_seek",
    "Seek": "can_seek",
}

# What a room is playing from, by the start of its transport URI. Each form
# was read off a speaker (AVTransportURI): the queue, line-in, the TV input
# and a group follower on a Sonos Amp and a Sonos One, a Sonos Radio station
# (x-sonosapi-radio) on the Amp in July, and a plain stream URL. Anything else
# reads "other", never a guess.
_SOURCE_PREFIXES = (
    ("x-rincon-queue:", "queue"),
    ("x-rincon-stream:", "line_in"),
    ("x-sonos-htastream:", "tv"),
    ("x-rincon:", "group"),
    ("x-sonosapi-radio:", "stream"),
    ("http://", "stream"),
    ("https://", "stream"),
)

# The home-theater values a speaker may report, as the RenderingControl EQ
# types its GetEQ / SetEQ take (the names of its own SCPD state variables), ->
# driver state. A speaker answers GetEQ for the ones it has and refuses the
# rest with UPnP 402: a Sonos Amp with nothing bonded answers NightMode,
# DialogLevel and AudioDelay and refuses SubEnabled and SurroundEnabled; a
# Sonos One refuses all of these. The sub and surround levels follow their
# Enabled value, because the speaker reads a level of 0 for a sub or surrounds
# it does not have. (Its LastChange event lists SubEnabled 1 even then, so the
# event is never what decides.)
_HT_EQ = {
    "NightMode": "night_mode",
    "DialogLevel": "speech_enhancement",
    "AudioDelay": "audio_delay",
    "SubEnabled": "sub_enabled",
    "SubGain": "sub_level",
    "SurroundEnabled": "surround_enabled",
    "SurroundLevel": "surround_level",
    "MusicSurroundLevel": "music_surround_level",
    "SurroundMode": "surround_mode",
}
_SUB_EQ = ("SubGain",)
_SURROUND_EQ = ("SurroundLevel", "MusicSurroundLevel", "SurroundMode")
_SURROUND_MODES = {"0": "ambient", "1": "full"}

# The settings and commands that exist only on a speaker that has them.
_HT_SETTINGS = {
    "NightMode": ("night_mode",),
    "DialogLevel": ("speech_enhancement",),
    "AudioDelay": ("audio_delay",),
    "SubEnabled": ("sub_enabled", "sub_level"),
    "SurroundEnabled": (
        "surround_enabled", "surround_level", "music_surround_level", "surround_mode",
    ),
}
_HT_COMMANDS = {
    "NightMode": ("set_night_mode",),
    "DialogLevel": ("set_speech_enhancement",),
}
_SERVICE_COMMANDS = {
    "AudioIn": ("play_line_in",),
    "HTControl": ("play_tv",),
}

# Sonos alarm recurrences, from the AlarmClock SCPD's allowedValueList. A
# custom set of days (the app's ON_<days>) is shown as such.
_RECURRENCES = {
    "ONCE": "once",
    "WEEKDAYS": "weekdays",
    "WEEKENDS": "weekends",
    "DAILY": "every day",
}

# The fields UpdateAlarm takes, as ListAlarms names them. Every one must be
# sent (a speaker answers 402 to an UpdateAlarm with one left out).
_ALARM_FIELDS = (
    ("StartLocalTime", "StartTime"),
    ("Duration", "Duration"),
    ("Recurrence", "Recurrence"),
    ("Enabled", "Enabled"),
    ("RoomUUID", "RoomUUID"),
    ("ProgramURI", "ProgramURI"),
    ("ProgramMetaData", "ProgramMetaData"),
    ("PlayMode", "PlayMode"),
    ("Volume", "Volume"),
    ("IncludeLinkedZones", "IncludeLinkedZones"),
)

_DIDL_OPEN = (
    '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/"'
    ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
    ' xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"'
    ' xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
)

# The most favorites, playlists or alarms a picker lists.
_LIST_LIMIT = 200


def _build_soap(service_type: str, action: str, **params: Any) -> tuple[str, str]:
    """Build a SOAP request body and SOAPAction header.

    Returns (body_xml, soap_action_header).
    """
    param_xml = ""
    for key, val in params.items():
        param_xml += f"<{key}>{_escape(str(val))}</{key}>"

    body = _SOAP_ENVELOPE.format(
        service_type=service_type, action=action, params=param_xml
    )
    soap_action = f'"{service_type}#{action}"'
    return body, soap_action


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _local_name(tag: str) -> str:
    """Strip any XML namespace from a tag ('{urn:...}Volume' -> 'Volume').

    Vendors pick their own namespace prefixes, and Sonos's LastChange
    namespaces even carry a trailing slash the spec examples don't show, so
    every tag test in this driver is namespace-insensitive by construction.
    """
    return tag.split("}")[-1] if "}" in tag else tag


def _parse_xml_value(xml_text: str, tag: str) -> str | None:
    """Extract a value from a SOAP XML response by tag name."""
    # Simple tag extraction: works for flat SOAP responses
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


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def _xml(text: str | None) -> ET.Element | None:
    """Parse an XML value that may arrive escaped once more (a SOAP field)."""
    if not text:
        return None
    body = text if text.lstrip().startswith("<") else _unescape(text)
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None


def _parse_didl_metadata(metadata_xml: str) -> dict[str, str | None]:
    """Parse DIDL-Lite metadata XML for track (or source) info.

    Sonos returns metadata as escaped XML: inside a SOAP response, and escaped
    a second time inside a LastChange event payload. Callers pass the
    already-once-unescaped string either way; a string that is already plain
    XML parses too. A blank title (line-in and the TV input send one space)
    reads as none.
    """
    result: dict[str, str | None] = {
        "title": None,
        "artist": None,
        "album": None,
        "album_art": None,
        "class": None,
    }

    if not metadata_xml or metadata_xml == "NOT_IMPLEMENTED":
        return result

    root = _xml(metadata_xml)
    if root is None:
        return result
    for item in root.iter():
        tag = _local_name(item.tag)
        text = (item.text or "").strip() or None
        if tag == "title" and not result["title"]:
            result["title"] = text
        elif tag == "creator" and not result["artist"]:
            result["artist"] = text
        elif tag == "album" and not result["album"]:
            result["album"] = text
        elif tag == "albumArtURI" and not result["album_art"]:
            result["album_art"] = text
        elif tag == "class" and not result["class"]:
            result["class"] = text
    return result


def _didl_item(title: str, uri: str) -> str:
    """Metadata for a stream URL played by address, so it shows a title."""
    return (
        f"{_DIDL_OPEN}<item id=\"openavc-uri\" parentID=\"-1\" restricted=\"true\">"
        f"<dc:title>{_escape(title)}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f"<res protocolInfo=\"http-get:*:*:*\">{_escape(uri)}</res>"
        "</item></DIDL-Lite>"
    )


def _parse_timeout(header: str, fallback: int) -> int:
    """Parse a GENA TIMEOUT header ('Second-1800') into seconds."""
    value = header.strip()
    if value.lower().startswith("second-"):
        tail = value.split("-", 1)[1]
        if tail.isdigit():
            return int(tail)
    return fallback


def _soap_fault_detail(body: str) -> str:
    """Pull the UPnP error out of a SOAP fault body, for the user-facing error.

    A rejected action answers HTTP 500 with a UPnPError carrying a numeric
    errorCode. That number is the difference between "the speaker is broken"
    and "that action doesn't apply right now" (e.g. setting a queue play mode
    on a speaker playing a radio stream).
    """
    code = _parse_xml_value(body or "", "errorCode")
    if not code:
        return ""
    description = _parse_xml_value(body or "", "errorDescription")
    known = _UPNP_ERRORS.get(code.strip())
    detail = description or known
    return f" (UPnP error {code.strip()}{f': {detail}' if detail else ''})"


# The rejections an AV integrator can actually act on, worded from the
# AVTransport:1 service template's error tables (2.0.3 and earlier gave 710
# and 711 meanings the standard does not). Anything else surfaces with its
# bare code, which is enough to search the UPnP spec for.
_UPNP_ERRORS = {
    "701": "that action is not available for what is playing right now",
    "705": "the transport is locked",
    "710": "that kind of seek is not supported for what is playing",
    "711": "that position or track number is not in what is playing",
    "712": "the requested play mode is not supported by what is playing",
    "714": "the speaker cannot play that kind of media",
    "716": "the speaker could not find that item",
    "718": "invalid instance",
}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _on_off(value: Any) -> str:
    """A DeviceProperties On/Off word for a boolean."""
    return "On" if _as_bool(value) else "Off"


def _absolute_url(base_url: str, uri: str | None) -> str | None:
    """The album art address as one a browser can fetch.

    A speaker gives its own art as a path on itself (``/getaa?s=1&u=...``) and
    a streaming service's as a full URL; the path is completed with the
    speaker's address so either can be shown.
    """
    if not uri:
        return None
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", uri):
        return uri
    if not base_url:
        return uri
    return urljoin(base_url + "/", uri)


def _source_of(uri: str | None) -> str:
    if not uri:
        return "none"
    for prefix, source in _SOURCE_PREFIXES:
        if uri.startswith(prefix):
            return source
    return "other"


def _hms(text: str) -> str:
    """A time as H:MM:SS, whatever the hour's width."""
    parts = text.strip().split(":")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return text.strip()
    return f"{int(parts[0])}:{int(parts[1]):02d}:{int(parts[2]):02d}"


def _parse_description(xml_text: str) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Read a UPnP device description (UPnP DA 2.0 section 2.3).

    Returns (device fields of the root device, services by name), the services
    gathered from the root device and every embedded one. A service's name is
    the next-to-last part of its type (``AVTransport`` from
    ``urn:schemas-upnp-org:service:AVTransport:1``).
    """
    root = ET.fromstring(xml_text)
    device_fields: dict[str, str] = {}
    services: dict[str, dict[str, str]] = {}

    top = next((el for el in root if _local_name(el.tag) == "device"), None)
    if top is None:
        return device_fields, services
    for child in top:
        if child.text and child.text.strip() and len(child) == 0:
            device_fields[_local_name(child.tag)] = child.text.strip()

    for element in root.iter():
        if _local_name(element.tag) != "service":
            continue
        fields = {
            _local_name(child.tag): (child.text or "").strip() for child in element
        }
        service_type = fields.get("serviceType", "")
        parts = service_type.split(":")
        if len(parts) < 2:
            continue
        name = parts[-2]
        # The first description of a service wins: a Sonos player lists the
        # media renderer's ConnectionManager and the media server's under the
        # same name, and neither is one this driver uses.
        services.setdefault(name, {
            "type": service_type,
            "control": fields.get("controlURL", ""),
            "event": fields.get("eventSubURL", ""),
            "scpd": fields.get("SCPDURL", ""),
        })
    return device_fields, services


def _parse_topology(xml_text: str | None) -> list[dict[str, Any]]:
    """The household's groups from a ZoneGroupState document.

    Each group: its coordinator's UUID and its members, each with UUID, room
    name, host (from its description Location) and whether it is a visible
    room. A bonded sub or surround is a member the household hides
    (Invisible="1"), never a room of its own.
    """
    root = _xml(xml_text)
    if root is None:
        return []
    groups: list[dict[str, Any]] = []
    for group in root.iter():
        if _local_name(group.tag) != "ZoneGroup":
            continue
        members = []
        for member in group:
            if _local_name(member.tag) != "ZoneGroupMember":
                continue
            location = member.get("Location") or ""
            members.append({
                "uuid": member.get("UUID") or "",
                "name": member.get("ZoneName") or "",
                "host": urlparse(location).hostname or "",
                "visible": member.get("Invisible", "0") != "1",
            })
        groups.append({"coordinator": group.get("Coordinator") or "", "members": members})
    return groups


class SonosDriver(BaseDriver):
    """Sonos speaker control driver via local UPnP/SOAP API + GENA push."""

    DRIVER_INFO = {
        "id": "sonos",
        "name": "Sonos Speaker",
        "manufacturer": "Sonos",
        "category": "audio",
        "version": "2.1.0",
        "author": "OpenAVC",
        "description": (
            "Controls Sonos speakers, amps and home theater products via the "
            "local UPnP API: playback and the queue, Sonos favorites and "
            "playlists, line-in and TV, groups and group volume, volume and "
            "fades, tone, home theater sound, alarms, now playing with album "
            "art, and the speaker's own settings. The speaker pushes state "
            "changes as they happen (UPnP eventing)."
        ),
        "source_url": "https://sonos.svrooij.io/services/",
        "tags": ["speaker", "wireless-audio", "background-music", "upnp"],
        "verified": True,
        "simulated": True,
        "ports": [1400],
        # Clear Queue, Delete Playlist and Delete Alarm ask before they run
        # (confirm, OpenAVC 0.36.0).
        "min_platform_version": "0.36.0",
        "compatible_models": [
            {
                "manufacturer": "Sonos",
                "models": [
                    "Sonos S1 platform speakers",
                    "Sonos S2 platform speakers",
                ],
                "confidence": "full",
                "notes": (
                    "UPnP API stable across all Sonos models. Control, "
                    "grouping and eventing verified on a Sonos Amp (S16) and a "
                    "Sonos One (S18), S2. Home theater settings appear only on "
                    "a speaker that reports them."
                ),
            },
        ],
        "transport": "http",
        # The speaker POSTs UPnP NOTIFY events to a callback URL the platform
        # assigns. This driver drives the GENA handshake itself (SUBSCRIBE /
        # renew / UNSUBSCRIBE): see _start_push below.
        "push": {"type": "http_listener"},
        "help": {
            "overview": (
                "Controls any Sonos speaker on the local network via UPnP. "
                "Works with all Sonos models including One, Five, Beam, Arc, "
                "Era, Move, Roam, Port, Amp, and legacy Play/Connect models. "
                "No cloud account or API key required. Add one device per "
                "Sonos room. The speaker pushes state changes as they happen, "
                "so transport state, volume, mute and now-playing info update "
                "on panels within about a second, whether the change came "
                "from OpenAVC, the Sonos app, or a button on the speaker "
                "itself. A room grouped with another plays what the group's "
                "lead room plays, and its play, pause and skip commands go to "
                "the lead room."
            ),
            "setup": (
                "1. Ensure the Sonos speaker is on the same network\n"
                "2. Enter the speaker's IP address (find it in the Sonos app "
                "under Settings > System > About)\n"
                "3. Default port is 1400 (do not change)\n"
                "4. UPnP control must be enabled on the speaker (on by "
                "default)\n"
                "5. For instant updates the speaker must be able to reach the "
                "OpenAVC server, because it posts events back to it. If a "
                "firewall sits between them, allow that direction too. "
                "Without it everything still works, just at poll speed."
            ),
        },
        "default_config": {
            "host": "",
            "port": 1400,
            "poll_interval": 5,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 1400, "label": "Port"},
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 1,
                "label": "Poll Interval (sec)",
                "description": (
                    "Resync interval. The speaker pushes state changes "
                    "instantly; polling is the fallback, and the source of "
                    "track position, which Sonos does not push."
                ),
            },
        },
        "state_variables": {
            "transport_state": {
                "type": "enum",
                "values": ["playing", "paused", "stopped", "transitioning"],
                "label": "Transport State",
                "control": True,
                "cloud_priority": "high",
            },
            "volume": {
                "type": "integer",
                "label": "Volume",
                "min": 0,
                "max": 100,
                "step": 1,
                "control": True,
            },
            "mute": {
                "type": "boolean",
                "label": "Mute",
                "control": True,
                "cloud_priority": "high",
            },
            "bass": {
                "type": "integer",
                "label": "Bass",
                "min": -10,
                "max": 10,
                "step": 1,
                "control": True,
                "help": "Bass tone control, -10 to +10 (0 is flat).",
            },
            "treble": {
                "type": "integer",
                "label": "Treble",
                "min": -10,
                "max": 10,
                "step": 1,
                "control": True,
                "help": "Treble tone control, -10 to +10 (0 is flat).",
            },
            "loudness": {
                "type": "boolean",
                "label": "Loudness",
                "help": "Loudness compensation (boosts bass at low volume).",
            },
            "play_mode": {
                "type": "enum",
                "values": [
                    "NORMAL",
                    "REPEAT_ALL",
                    "REPEAT_ONE",
                    "SHUFFLE_NOREPEAT",
                    "SHUFFLE",
                    "SHUFFLE_REPEAT_ONE",
                ],
                "label": "Play Mode",
                "help": "Queue repeat / shuffle mode.",
            },
            "crossfade": {
                "type": "boolean",
                "label": "Crossfade",
                "help": "Fades between tracks of the queue.",
            },
            "source": {
                "type": "enum",
                "values": ["none", "queue", "stream", "line_in", "tv", "group", "other"],
                "label": "Source",
                "control": True,
                "help": (
                    "What the room is playing from: the queue, a stream (a "
                    "radio station, a service's station, a URL), line-in, the "
                    "TV input, or nothing. A room following another shows the "
                    "group's source."
                ),
            },
            "source_name": {
                "type": "string",
                "label": "Playing From",
                "help": "The name of the station, playlist or stream that is loaded, when the speaker gives one.",
            },
            "track_title": {"type": "string", "label": "Track Title"},
            "track_artist": {"type": "string", "label": "Track Artist"},
            "track_album": {"type": "string", "label": "Track Album"},
            "track_album_art": {
                "type": "string",
                "label": "Album Art",
                "help": "The address of the current track's cover picture.",
            },
            "track_duration": {"type": "string", "label": "Track Duration"},
            "track_position": {
                "type": "string",
                "label": "Track Position",
                # Advances on every poll while playing: background telemetry,
                # not something the cloud needs at the high-priority cadence.
                "cloud_priority": "low",
            },
            "track_number": {
                "type": "integer",
                "label": "Queue Position",
                "help": "The current track's number in the queue (0 when the queue is not playing).",
            },
            "queue_length": {
                "type": "integer",
                "label": "Queue Length",
                "help": "How many tracks the queue holds.",
            },
            "can_pause": {
                "type": "boolean",
                "label": "Can Pause",
                "help": "Whether what is playing can be paused right now (a live stream may only stop).",
            },
            "can_skip_next": {
                "type": "boolean",
                "label": "Can Skip Next",
            },
            "can_skip_previous": {
                "type": "boolean",
                "label": "Can Skip Previous",
            },
            "can_seek": {
                "type": "boolean",
                "label": "Can Seek",
            },
            "sleep_timer_remaining": {
                "type": "string",
                "label": "Sleep Timer",
                "help": "Time left on the sleep timer (H:MM:SS); empty when no timer is set.",
                "cloud_priority": "low",
            },
            "grouped": {
                "type": "boolean",
                "label": "Grouped",
                "help": "The room plays together with at least one other room.",
            },
            "is_group_coordinator": {
                "type": "boolean",
                "label": "Leads Its Group",
                "help": "This room leads its group (a room on its own leads itself).",
            },
            "group_coordinator": {
                "type": "string",
                "label": "Group Leader",
                "help": "The room the group follows.",
            },
            "group_members": {
                "type": "string",
                "label": "Group Members",
                "help": "The rooms in this room's group, the leader first.",
            },
            "group_volume": {
                "type": "integer",
                "label": "Group Volume",
                "min": 0,
                "max": 100,
                "step": 1,
                "control": True,
            },
            "group_mute": {
                "type": "boolean",
                "label": "Group Mute",
                "control": True,
            },
            "rooms": {
                "type": "string",
                "label": "Other Rooms",
                "cloud_priority": "low",
                "help": "The household's other Sonos rooms, as a picker list.",
            },
            "favorites": {
                "type": "string",
                "label": "Favorites",
                "cloud_priority": "low",
                "help": "The Sonos favorites this room can start, as a picker list.",
            },
            "playlists": {
                "type": "string",
                "label": "Playlists",
                "cloud_priority": "low",
                "help": "The household's Sonos playlists, as a picker list.",
            },
            "alarms": {
                "type": "string",
                "label": "Alarms",
                "cloud_priority": "low",
                "help": "This room's alarms, as a picker list.",
            },
            "alarm_running": {
                "type": "boolean",
                "label": "Alarm Running",
                "help": "An alarm is playing on this speaker.",
            },
            "snooze_running": {
                "type": "boolean",
                "label": "Alarm Snoozed",
                "help": "A playing alarm is snoozed.",
            },
            "line_in_connected": {
                "type": "boolean",
                "label": "Line-In Connected",
                "help": "Something is plugged into the speaker's line-in.",
            },
            "night_mode": {
                "type": "boolean",
                "label": "Night Sound",
                "help": "Quiets loud sounds and raises quiet ones in TV audio.",
            },
            "speech_enhancement": {
                "type": "boolean",
                "label": "Speech Enhancement",
                "help": "Makes voices in TV audio clearer.",
            },
            "audio_delay": {
                "type": "integer",
                "label": "Audio Delay",
                "min": 0,
                "max": 5,
                "step": 1,
                "help": "Lip sync: how far TV audio is held back, 0 to 5.",
            },
            "sub_enabled": {
                "type": "boolean",
                "label": "Sub",
                "help": "The bonded sub plays.",
            },
            "sub_level": {
                "type": "integer",
                "label": "Sub Level",
                "min": -15,
                "max": 15,
                "step": 1,
                "control": True,
            },
            "surround_enabled": {
                "type": "boolean",
                "label": "Surrounds",
                "help": "The bonded surround speakers play.",
            },
            "surround_level": {
                "type": "integer",
                "label": "Surround Level (TV)",
                "min": -15,
                "max": 15,
                "step": 1,
            },
            "music_surround_level": {
                "type": "integer",
                "label": "Surround Level (Music)",
                "min": -15,
                "max": 15,
                "step": 1,
            },
            "surround_mode": {
                "type": "enum",
                "values": ["ambient", "full"],
                "label": "Music Surround Playback",
                "help": "Ambient plays music quietly from the surrounds; full plays it at full range.",
            },
            "trueplay": {
                "type": "boolean",
                "label": "Trueplay",
                "help": "The room's Trueplay tuning is applied.",
            },
            "status_light": {
                "type": "boolean",
                "label": "Status Light",
                "help": "The status light on the speaker is on.",
            },
            "touch_controls_locked": {
                "type": "boolean",
                "label": "Touch Controls Locked",
                "help": "The buttons on the speaker are locked.",
            },
            "speaker_name": {"type": "string", "label": "Speaker Name"},
            "model_name": {"type": "string", "label": "Model"},
            "model_number": {"type": "string", "label": "Model Number"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "software_version": {"type": "string", "label": "Software Version"},
            "hardware_version": {"type": "string", "label": "Hardware Version"},
            "mac_address": {"type": "string", "label": "MAC Address"},
        },
        # Writable values the speaker persists and reports back (over the event
        # stream, and on poll); the platform gives them an editable field plus
        # the offline pending queue. A home theater setting is offered only on
        # a speaker that reports it (see _narrow).
        "device_settings": {
            "bass": {
                "type": "integer",
                "label": "Bass",
                "state_key": "bass",
                "default": 0,
                "min": -10,
                "max": 10,
                "setup": False,
                "help": "Bass tone control, -10 to +10 (0 is flat).",
            },
            "treble": {
                "type": "integer",
                "label": "Treble",
                "state_key": "treble",
                "default": 0,
                "min": -10,
                "max": 10,
                "setup": False,
                "help": "Treble tone control, -10 to +10 (0 is flat).",
            },
            "loudness": {
                "type": "boolean",
                "label": "Loudness",
                "state_key": "loudness",
                "default": True,
                "setup": False,
                "help": (
                    "Loudness compensation. Boosts bass at low listening "
                    "levels; usually left on for background music."
                ),
            },
            "play_mode": {
                "type": "enum",
                "values": [
                    "NORMAL",
                    "REPEAT_ALL",
                    "REPEAT_ONE",
                    "SHUFFLE_NOREPEAT",
                    "SHUFFLE",
                    "SHUFFLE_REPEAT_ONE",
                ],
                "label": "Play Mode",
                "state_key": "play_mode",
                "default": "NORMAL",
                "setup": False,
                "help": "Queue repeat / shuffle mode.",
            },
            "crossfade": {
                "type": "boolean",
                "label": "Crossfade",
                "state_key": "crossfade",
                "default": False,
                "setup": False,
                "help": "Fade between tracks of the queue.",
            },
            "night_mode": {
                "type": "boolean",
                "label": "Night Sound",
                "state_key": "night_mode",
                "default": False,
                "setup": False,
                "help": "Quiets loud sounds and raises quiet ones in TV audio.",
            },
            "speech_enhancement": {
                "type": "boolean",
                "label": "Speech Enhancement",
                "state_key": "speech_enhancement",
                "default": False,
                "setup": False,
                "help": "Makes voices in TV audio clearer.",
            },
            "audio_delay": {
                "type": "integer",
                "label": "Audio Delay (Lip Sync)",
                "state_key": "audio_delay",
                "default": 0,
                "min": 0,
                "max": 5,
                "setup": False,
                "help": "Holds TV audio back so it lines up with the picture, 0 to 5.",
            },
            "sub_enabled": {
                "type": "boolean",
                "label": "Sub",
                "state_key": "sub_enabled",
                "default": True,
                "setup": False,
                "help": "Turn the bonded sub on or off.",
            },
            "sub_level": {
                "type": "integer",
                "label": "Sub Level",
                "state_key": "sub_level",
                "default": 0,
                "min": -15,
                "max": 15,
                "setup": False,
                "help": "The sub's level, -15 to +15.",
            },
            "surround_enabled": {
                "type": "boolean",
                "label": "Surrounds",
                "state_key": "surround_enabled",
                "default": True,
                "setup": False,
                "help": "Turn the bonded surround speakers on or off.",
            },
            "surround_level": {
                "type": "integer",
                "label": "Surround Level (TV)",
                "state_key": "surround_level",
                "default": 0,
                "min": -15,
                "max": 15,
                "setup": False,
                "help": "The surrounds' level for TV audio, -15 to +15.",
            },
            "music_surround_level": {
                "type": "integer",
                "label": "Surround Level (Music)",
                "state_key": "music_surround_level",
                "default": 0,
                "min": -15,
                "max": 15,
                "setup": False,
                "help": "The surrounds' level for music, -15 to +15.",
            },
            "surround_mode": {
                "type": "enum",
                "values": ["ambient", "full"],
                "label": "Music Surround Playback",
                "state_key": "surround_mode",
                "default": "ambient",
                "setup": False,
                "help": "Ambient plays music quietly from the surrounds; full plays it at full range.",
            },
            "trueplay": {
                "type": "boolean",
                "label": "Trueplay",
                "state_key": "trueplay",
                "default": True,
                "setup": False,
                "help": "Apply the room's Trueplay tuning.",
            },
            "status_light": {
                "type": "boolean",
                "label": "Status Light",
                "state_key": "status_light",
                "default": True,
                "setup": False,
                "help": "Turn the speaker's status light on or off.",
            },
            "touch_controls_locked": {
                "type": "boolean",
                "label": "Lock Touch Controls",
                "state_key": "touch_controls_locked",
                "default": False,
                "setup": False,
                "help": (
                    "Locks the buttons on the speaker, so nobody can change "
                    "the volume or playback from it."
                ),
            },
        },
        "quick_actions": ["play", "pause", "stop", "mute_toggle"],
        "commands": {
            "play": {
                "label": "Play",
                "params": {},
                "help": "Start or resume playback.",
                "sets": {"transport_state": "playing"},
            },
            "pause": {
                "label": "Pause",
                "params": {},
                "help": "Pause playback.",
                "sets": {"transport_state": "paused"},
            },
            "stop": {
                "label": "Stop",
                "params": {},
                "help": "Stop playback.",
                "sets": {"transport_state": "stopped"},
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
            "seek": {
                "label": "Seek",
                "params": {
                    "position": {
                        "type": "string",
                        "required": True,
                        "label": "Position",
                        "pattern": r"^([01]?\d|2[0-3]):[0-5]\d:[0-5]\d$",
                        "help": "Where to jump to in the current track, as H:MM:SS (0:01:30).",
                    },
                },
                "help": "Jump to a point in the current track.",
            },
            "play_queue_track": {
                "label": "Play Track from Queue",
                "params": {
                    "track": {
                        "type": "integer",
                        "required": True,
                        "label": "Track Number",
                        "min": 1,
                        "help": "The track's number in the queue, from 1.",
                    },
                },
                "help": "Play the queue from the track with that number.",
                "sets": {"track_number": "{track}"},
            },
            "clear_queue": {
                "label": "Clear Queue",
                "params": {},
                "help": "Removes every track from the queue.",
                "confirm": "Removes every track from this room's queue.",
                "sets": {"queue_length": 0},
            },
            "play_favorite": {
                "label": "Play Favorite",
                "params": {
                    "favorite": {
                        "type": "string",
                        "required": True,
                        "label": "Favorite",
                        "options_state": "favorites",
                        "help": "One of the household's Sonos favorites.",
                    },
                },
                "help": (
                    "Plays a Sonos favorite. A favorite that is a collection "
                    "(a playlist, an album) replaces the queue. Favorites "
                    "saved as Sonos Radio shortcuts start only from the Sonos "
                    "app and are not listed."
                ),
            },
            "play_playlist": {
                "label": "Play Playlist",
                "params": {
                    "playlist": {
                        "type": "string",
                        "required": True,
                        "label": "Playlist",
                        "options_state": "playlists",
                        "help": "One of the household's Sonos playlists.",
                    },
                },
                "help": "Replaces the queue with a Sonos playlist and plays it from the start.",
            },
            "queue_playlist": {
                "label": "Add Playlist to Queue",
                "params": {
                    "playlist": {
                        "type": "string",
                        "required": True,
                        "label": "Playlist",
                        "options_state": "playlists",
                        "help": "One of the household's Sonos playlists.",
                    },
                },
                "help": "Adds a Sonos playlist's tracks to the end of the queue.",
            },
            "save_queue_as_playlist": {
                "label": "Save Queue as Playlist",
                "params": {
                    "title": {
                        "type": "string",
                        "required": True,
                        "label": "Playlist Name",
                        "help": "The name of the new Sonos playlist.",
                    },
                },
                "help": "Saves the queue as a new Sonos playlist the whole household can play.",
            },
            "delete_playlist": {
                "label": "Delete Playlist",
                "params": {
                    "playlist": {
                        "type": "string",
                        "required": True,
                        "label": "Playlist",
                        "options_state": "playlists",
                        "help": "One of the household's Sonos playlists.",
                    },
                },
                "help": "Deletes a Sonos playlist from the household.",
                "confirm": "Deletes the Sonos playlist from every room in the household.",
            },
            "play_uri": {
                "label": "Play Stream URL",
                "params": {
                    "uri": {
                        "type": "string",
                        "required": True,
                        "label": "URL",
                        "pattern": r"^https?://\S+$",
                        "help": "An http or https address of an audio file or stream the speaker can reach.",
                    },
                    "title": {
                        "type": "string",
                        "label": "Title",
                        "help": (
                            "The name shown as Playing From (the address if "
                            "left out). A file's own tags still name the track."
                        ),
                    },
                },
                "help": "Plays an audio file or internet radio stream by its address.",
            },
            "play_line_in": {
                "label": "Play Line-In",
                "params": {},
                "help": (
                    "Plays what is plugged into this speaker's line-in. A room "
                    "leading a group plays it to the whole group; a room "
                    "following another leaves that group first."
                ),
                "sets": {"source": "line_in"},
            },
            "play_tv": {
                "label": "Play TV",
                "params": {},
                "help": (
                    "Plays the TV input. A room leading a group plays it to "
                    "the whole group; a room following another leaves that "
                    "group first."
                ),
                "sets": {"source": "tv"},
            },
            "join_group": {
                "label": "Join Group",
                "params": {
                    "room": {
                        "type": "string",
                        "required": True,
                        "label": "Room",
                        "options_state": "rooms",
                        "help": "The room whose group this room joins.",
                    },
                },
                "help": "This room joins another room's group and plays what that group plays.",
                "sets": {"grouped": True},
            },
            "leave_group": {
                "label": "Leave Group",
                "params": {},
                "help": "This room leaves its group and plays on its own.",
                "sets": {"grouped": False},
            },
            "add_room_to_group": {
                "label": "Add Room to Group",
                "params": {
                    "room": {
                        "type": "string",
                        "required": True,
                        "label": "Room",
                        "options_state": "rooms",
                        "help": "The room to bring into this room's group.",
                    },
                },
                "help": "Another room joins this room's group.",
            },
            "remove_room_from_group": {
                "label": "Remove Room from Group",
                "params": {
                    "room": {
                        "type": "string",
                        "required": True,
                        "label": "Room",
                        "options_state": "rooms",
                        "help": "A room in this room's group.",
                    },
                },
                "help": "Another room leaves this room's group and plays on its own.",
            },
            "set_group_volume": {
                "label": "Set Group Volume",
                "params": {
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Group volume 0-100.",
                    },
                },
                "help": "Sets the volume of every room in the group, keeping their balance.",
                "sets": {"group_volume": "{level}"},
            },
            "group_volume_up": {
                "label": "Group Volume Up",
                "params": {
                    "step": {
                        "type": "integer",
                        "min": 1,
                        "max": 100,
                        "default": 5,
                        "label": "Step",
                        "help": "How much to raise the group volume by (5 if left out).",
                    },
                },
                "help": "Raises the whole group's volume by a step.",
            },
            "group_volume_down": {
                "label": "Group Volume Down",
                "params": {
                    "step": {
                        "type": "integer",
                        "min": 1,
                        "max": 100,
                        "default": 5,
                        "label": "Step",
                        "help": "How much to lower the group volume by (5 if left out).",
                    },
                },
                "help": "Lowers the whole group's volume by a step.",
            },
            "group_mute_on": {
                "label": "Mute Group",
                "params": {},
                "help": "Mutes every room in the group.",
                "sets": {"group_mute": True},
            },
            "group_mute_off": {
                "label": "Unmute Group",
                "params": {},
                "help": "Unmutes every room in the group.",
                "sets": {"group_mute": False},
            },
            "group_mute_toggle": {
                "label": "Group Mute Toggle",
                "params": {},
                "help": "Toggles the group's mute.",
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
                "sets": {"volume": "{level}"},
            },
            "volume_up": {
                "label": "Volume Up",
                "params": {
                    "step": {
                        "type": "integer",
                        "min": 1,
                        "max": 100,
                        "default": 5,
                        "label": "Step",
                        "help": "How much to raise the volume by (5 if left out).",
                    },
                },
                "help": "Raise the volume by a step.",
            },
            "volume_down": {
                "label": "Volume Down",
                "params": {
                    "step": {
                        "type": "integer",
                        "min": 1,
                        "max": 100,
                        "default": 5,
                        "label": "Step",
                        "help": "How much to lower the volume by (5 if left out).",
                    },
                },
                "help": "Lower the volume by a step.",
            },
            "ramp_to_volume": {
                "label": "Fade to Volume",
                "params": {
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "label": "Volume",
                        "help": "The volume to fade to, 0-100.",
                    },
                    "fade": {
                        "type": "enum",
                        "values": [
                            {"value": "SLEEP_TIMER_RAMP_TYPE", "label": "From the current level"},
                            {"value": "AUTOPLAY_RAMP_TYPE", "label": "From silence, quickly"},
                            {"value": "ALARM_RAMP_TYPE", "label": "From silence, slowly"},
                        ],
                        "required": True,
                        "default": "SLEEP_TIMER_RAMP_TYPE",
                        "label": "Fade",
                        "help": (
                            "From the current level: a steady fade of about "
                            "8 seconds. From silence, quickly: drops to "
                            "silence and is at the level in about 2 seconds. "
                            "From silence, slowly: silent for about half a "
                            "minute, then rises over about 8 seconds."
                        ),
                    },
                },
                "help": "Fade the volume to a level.",
            },
            "mute_on": {
                "label": "Mute",
                "params": {},
                "help": "Mute the speaker.",
                "sets": {"mute": True},
            },
            "mute_off": {
                "label": "Unmute",
                "params": {},
                "help": "Unmute the speaker.",
                "sets": {"mute": False},
            },
            "mute_toggle": {
                "label": "Mute Toggle",
                "params": {},
                "help": "Toggle the speaker's mute.",
            },
            "set_bass": {
                "label": "Set Bass",
                "params": {
                    "level": {
                        "type": "integer",
                        "min": -10,
                        "max": 10,
                        "required": True,
                        "help": "Bass level, -10 to +10.",
                    },
                },
                "help": "Set the bass tone control.",
                "sets": {"bass": "{level}"},
            },
            "set_treble": {
                "label": "Set Treble",
                "params": {
                    "level": {
                        "type": "integer",
                        "min": -10,
                        "max": 10,
                        "required": True,
                        "help": "Treble level, -10 to +10.",
                    },
                },
                "help": "Set the treble tone control.",
                "sets": {"treble": "{level}"},
            },
            "set_loudness": {
                "label": "Set Loudness",
                "params": {
                    "enabled": {
                        "type": "boolean",
                        "required": True,
                        "help": "Loudness compensation on or off.",
                    },
                },
                "help": "Turn loudness compensation on or off.",
                "sets": {"loudness": "{enabled}"},
            },
            "set_night_mode": {
                "label": "Set Night Sound",
                "params": {
                    "enabled": {
                        "type": "boolean",
                        "required": True,
                        "help": "Night sound on or off.",
                    },
                },
                "help": "Turn night sound on or off for TV audio.",
                "sets": {"night_mode": "{enabled}"},
            },
            "set_speech_enhancement": {
                "label": "Set Speech Enhancement",
                "params": {
                    "enabled": {
                        "type": "boolean",
                        "required": True,
                        "help": "Speech enhancement on or off.",
                    },
                },
                "help": "Turn speech enhancement on or off for TV audio.",
                "sets": {"speech_enhancement": "{enabled}"},
            },
            "set_play_mode": {
                "label": "Set Play Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "values": [
                            "NORMAL",
                            "REPEAT_ALL",
                            "REPEAT_ONE",
                            "SHUFFLE_NOREPEAT",
                            "SHUFFLE",
                            "SHUFFLE_REPEAT_ONE",
                        ],
                        "required": True,
                        "help": "Queue repeat / shuffle mode.",
                    },
                },
                "help": "Set the queue's repeat / shuffle mode.",
                "sets": {"play_mode": "{mode}"},
            },
            "set_crossfade": {
                "label": "Set Crossfade",
                "params": {
                    "enabled": {
                        "type": "boolean",
                        "required": True,
                        "help": "Crossfade on or off.",
                    },
                },
                "help": "Turn crossfading between queue tracks on or off.",
                "sets": {"crossfade": "{enabled}"},
            },
            "set_sleep_timer": {
                "label": "Set Sleep Timer",
                "params": {
                    "duration": {
                        "type": "string",
                        "required": True,
                        "label": "Duration",
                        "pattern": r"^([01]?\d|2[0-3]):[0-5]\d:[0-5]\d$",
                        "help": "How long until the speaker stops, as H:MM:SS, up to 23:59:59 (0:30:00 for half an hour).",
                    },
                },
                "help": "Stop playback after a while.",
            },
            "cancel_sleep_timer": {
                "label": "Cancel Sleep Timer",
                "params": {},
                "help": "Turn the sleep timer off.",
                "sets": {"sleep_timer_remaining": ""},
            },
            "enable_alarm": {
                "label": "Turn Alarm On",
                "params": {
                    "alarm": {
                        "type": "string",
                        "required": True,
                        "label": "Alarm",
                        "options_state": "alarms",
                        "help": "One of this room's alarms.",
                    },
                },
                "help": "Turns one of this room's alarms on.",
            },
            "disable_alarm": {
                "label": "Turn Alarm Off",
                "params": {
                    "alarm": {
                        "type": "string",
                        "required": True,
                        "label": "Alarm",
                        "options_state": "alarms",
                        "help": "One of this room's alarms.",
                    },
                },
                "help": "Turns one of this room's alarms off.",
            },
            "delete_alarm": {
                "label": "Delete Alarm",
                "params": {
                    "alarm": {
                        "type": "string",
                        "required": True,
                        "label": "Alarm",
                        "options_state": "alarms",
                        "help": "One of this room's alarms.",
                    },
                },
                "help": "Deletes one of this room's alarms.",
                "confirm": "Deletes the alarm from the Sonos household.",
            },
            "snooze_alarm": {
                "label": "Snooze Alarm",
                "params": {
                    "minutes": {
                        "type": "integer",
                        "required": True,
                        "default": 10,
                        "min": 1,
                        "max": 60,
                        "label": "Minutes",
                        "help": "How long to snooze the playing alarm.",
                    },
                },
                "help": "Pauses the alarm that is playing and plays it again after a while.",
                "sets": {"snooze_running": True},
            },
            "set_status_light": {
                "label": "Set Status Light",
                "params": {
                    "enabled": {
                        "type": "boolean",
                        "required": True,
                        "help": "Status light on or off.",
                    },
                },
                "help": "Turn the speaker's status light on or off.",
                "sets": {"status_light": "{enabled}"},
            },
            "set_touch_controls_lock": {
                "label": "Lock Touch Controls",
                "params": {
                    "locked": {
                        "type": "boolean",
                        "required": True,
                        "help": "Lock (true) or unlock (false) the buttons on the speaker.",
                    },
                },
                "help": "Lock or unlock the buttons on the speaker.",
                "sets": {"touch_controls_locked": "{locked}"},
            },
        },
        "discovery": {
            # Sonos speakers advertise via SSDP using the ZonePlayer URN.
            # Hints are required as a fallback: discovery scans often
            # capture Sonos via mDNS (`_spotify-connect._tcp.local`,
            # `_sonos._tcp.local`) without picking up the SSDP NOTIFY,
            # so an SSDP-only driver wouldn't claim the device. The
            # OUI / hostname / manufacturer_alias / port hints below
            # ensure the driver surfaces as a candidate regardless of
            # which scanner found the speaker.
            "ssdp": [
                "urn:schemas-upnp-org:device:ZonePlayer:1",
            ],
            "oui": [
                "54:2a:1b",   # Sonos (current)
                "b8:e9:37",   # Sonos (legacy)
                "78:28:ca",   # Sonos
                "34:7e:5c",   # Sonos (read off an Amp)
            ],
            "hostname": ["^Sonos-", "^sonos"],
            "port_open": [1400],
            "manufacturer_alias": ["sonos"],
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._base_url: str = ""
        self._uuid: str = ""
        # The services this speaker offers, by name (from its description).
        self._services: dict[str, dict[str, str]] = {
            name: dict(svc) for name, svc in _DEFAULT_SERVICES.items()
        }
        # GENA state per subscription label: the host it is on (None for this
        # speaker), event path, callback URL, SID, granted timeout, last SEQ
        # seen, and for a subscription on another speaker its callback handle.
        self._gena: dict[str, dict[str, Any]] = {}
        self._renew_task: asyncio.Task | None = None
        self._push_started = False
        self._polls = 0
        self._sleep_timer_generation: int | None = None
        # The household: its groups, and this room's group's coordinator
        # ({uuid, host, name}; None while this room leads or is unknown).
        self._groups: list[dict[str, Any]] = []
        self._leader: dict[str, str] | None = None
        self._leader_uuid: str = ""
        self._group_lock = asyncio.Lock()
        # Home theater EQ types this speaker answers (see _HT_EQ).
        self._ht: set[str] = set()
        self._trueplay_available = False
        # Favorites, playlists and alarms by id, from the speaker.
        self._favorites: dict[str, dict[str, str]] = {}
        self._playlists: dict[str, dict[str, str]] = {}
        self._alarms: dict[str, dict[str, str]] = {}
        self._refresh_tasks: dict[str, asyncio.Task] = {}

    async def _create_transport(self, transport_type: str) -> None:
        """Driver-owned session: SOAP over plain HTTP on port 1400.

        No platform transport: the httpx client is the connection, so
        ``self.transport`` stays None and _link_alive()/_close_session()
        report and retire the client instead.
        """
        host = self.config.get("host", "")
        port = self.config.get("port", 1400)
        self._base_url = f"http://{host}:{port}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=5.0,
        )

    async def _post_connect(self) -> None:
        # Verify the speaker actually answers before `connected` is declared:
        # one DeviceProperties read, which also names the device card.
        host = self.config.get("host", "")
        port = self.config.get("port", 1400)
        await self._read_description()
        try:
            name = await self._get_speaker_name()
        except Exception as e:
            raise ConnectionError(
                f"Failed to connect to Sonos at {host}:{port}: {e}"
            )
        if name:
            self.set_state("speaker_name", name)
            log.info(f"[{self.device_id}] Speaker name: {name}")
        try:
            await self._read_zone_info()
            await self._read_capabilities()
            await self._read_topology()
        except httpx.TransportError:
            # The speaker answered the read above; identity is not worth
            # failing the connect over, and the next connect reads it again.
            log.debug(f"[{self.device_id}] identity reads unfinished", exc_info=True)
        self._narrow()

    async def _initial_sync(self) -> None:
        # Push is already subscribed (accepting a GENA subscription delivers
        # an initial event with the full evented state); this first poll fills
        # in what Sonos does not event, starting with the track position, and
        # reads the lists the pickers offer.
        self._polls = 0
        await self.poll()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        for task in self._refresh_tasks.values():
            task.cancel()
        self._refresh_tasks.clear()
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    # --- The speaker's own description and what it has ---

    async def _read_description(self) -> None:
        """Read which services the speaker offers, its model and its UUID.

        Never fails the connect: every Sonos player has the three services the
        defaults name, and the GetZoneAttributes read that follows is what
        proves the speaker answers.
        """
        if not self._client:
            return
        try:
            resp = await self._client.get(_DESCRIPTION_PATH)
        except httpx.TransportError:
            return
        if resp.status_code != 200:
            log.debug(
                f"[{self.device_id}] device description: HTTP {resp.status_code}"
            )
            return
        try:
            fields, services = _parse_description(resp.text)
        except ET.ParseError:
            log.debug(f"[{self.device_id}] device description is not XML")
            return
        for name, svc in services.items():
            if svc.get("control"):
                self._services[name] = svc
        udn = fields.get("UDN", "")
        if udn:
            self._uuid = udn.removeprefix("uuid:")
        if fields.get("modelName"):
            self.set_state("model_name", fields["modelName"])
        if fields.get("modelNumber"):
            self.set_state("model_number", fields["modelNumber"])

    async def _read_zone_info(self) -> None:
        """Serial number, firmware, hardware version and MAC address."""
        resp = await self._soap("DeviceProperties", "GetZoneInfo")
        if not resp:
            return
        updates: dict[str, Any] = {}
        for tag, key in (
            ("SerialNumber", "serial_number"),
            ("HardwareVersion", "hardware_version"),
            ("MACAddress", "mac_address"),
        ):
            value = _parse_xml_value(resp, tag)
            if value:
                updates[key] = value
        # The version the Sonos app shows (DisplaySoftwareVersion) when the
        # speaker gives one, else the build number.
        version = _parse_xml_value(resp, "DisplaySoftwareVersion") or _parse_xml_value(
            resp, "SoftwareVersion"
        )
        if version:
            updates["software_version"] = version
        if updates:
            self.set_states(updates)

    async def _read_capabilities(self) -> None:
        """Which home theater values and Trueplay this speaker has.

        Asked with GetEQ, which a speaker answers for the EQ types it has and
        refuses (UPnP 402) for the rest, and GetRoomCalibrationStatus, whose
        RoomCalibrationAvailable says whether the room has a Trueplay tuning.
        """
        found: set[str] = set()
        updates: dict[str, Any] = {}
        for eq in _HT_EQ:
            resp = await self._soap(
                "RenderingControl", "GetEQ", InstanceID="0", EQType=eq,
            )
            value = _parse_xml_value(resp or "", "CurrentValue")
            if value is None:
                continue
            found.add(eq)
        if "SubEnabled" not in found:
            found.difference_update(_SUB_EQ)
        if "SurroundEnabled" not in found:
            found.difference_update(_SURROUND_EQ)
        self._ht = found
        resp = await self._soap("RenderingControl", "GetRoomCalibrationStatus", InstanceID="0")
        if resp:
            self._trueplay_available = _parse_xml_value(resp, "RoomCalibrationAvailable") == "1"
            if self._trueplay_available:
                updates["trueplay"] = _parse_xml_value(resp, "RoomCalibrationEnabled") == "1"
        if updates:
            self.set_states(updates)
        await self._read_home_theater()

    def _narrow(self) -> None:
        """Offer only the settings and commands this speaker has.

        A Sonos One has no line-in, no TV input and no home theater settings,
        and an Amp with nothing bonded has no sub or surround settings: their
        commands and settings are left off its device page.
        """
        base = type(self).DRIVER_INFO
        drop_settings: set[str] = set()
        drop_commands: set[str] = set()
        for eq, settings in _HT_SETTINGS.items():
            if eq not in self._ht:
                drop_settings.update(settings)
        for eq, commands in _HT_COMMANDS.items():
            if eq not in self._ht:
                drop_commands.update(commands)
        if not self._trueplay_available:
            drop_settings.add("trueplay")
        for service, commands in _SERVICE_COMMANDS.items():
            if service not in self._services:
                drop_commands.update(commands)
        if not drop_settings and not drop_commands:
            self.DRIVER_INFO = base
            return
        self.DRIVER_INFO = {
            **base,
            "commands": {
                key: cdef for key, cdef in base["commands"].items()
                if key not in drop_commands
            },
            "device_settings": {
                key: sdef for key, sdef in base["device_settings"].items()
                if key not in drop_settings
            },
        }

    # --- Push: UPnP GENA subscriptions ---

    async def _start_push(self) -> None:
        """Subscribe to the speaker's service events, and the group's.

        Overrides BaseDriver's generic http_listener registration because GENA
        is a real handshake (SUBSCRIBE returns a SID with a lifetime that must
        be renewed) and one speaker needs a subscription per service. Never
        raises: a speaker whose events can't be subscribed still works at poll
        speed, and the gap is logged.
        """
        handles = []
        host = str(self.config.get("host", "") or "")
        for label, service in _EVENT_SERVICES.items():
            event_path = self._services.get(service, {}).get("event")
            if not event_path:
                continue
            handle = await self._open_callback(label, host)
            if handle is None:
                continue
            handles.append(handle)
            self._gena[label] = {
                "host": None,
                "event_path": event_path,
                "callback_url": http_listener.callback_url(host, handle.path),
                "sid": "",
                "timeout": _SUBSCRIBE_SECONDS,
                "seq": -1,
            }
            await self._gena_subscribe(label)

        if handles:
            self._push_subscription = handles
        self._push_started = True
        await self._follow_group()
        if self._renew_task is None:
            self._renew_task = asyncio.create_task(self._renew_loop())

    async def _open_callback(self, label: str, source_ip: str) -> Any:
        try:
            return await http_listener.subscribe(
                device_id=self.device_id,
                source_ip=source_ip,
                callback=self._handle_notify,
                name=f"{self.device_id}:{label}",
                label=label,
            )
        except Exception:
            log.warning(
                f"[{self.device_id}] push: could not register the {label} callback",
                exc_info=True,
            )
            return None

    async def _stop_push(self) -> None:
        """Cancel renewals, UNSUBSCRIBE from the speakers, drop the callbacks."""
        self._push_started = False
        task = self._renew_task
        self._renew_task = None
        if task is not None:
            task.cancel()

        for label in list(self._gena):
            await self._drop_subscription(label)
        self._gena.clear()

        # Drop the platform-side callback registrations.
        await super()._stop_push()

    async def _drop_subscription(self, label: str) -> None:
        gena = self._gena.pop(label, None)
        if not gena:
            return
        if gena.get("sid"):
            try:
                await self._gena_request(
                    "UNSUBSCRIBE", gena["event_path"], {"SID": gena["sid"]}, gena["host"],
                )
                log.debug(f"[{self.device_id}] push: unsubscribed {label}")
            except Exception:
                # A speaker expires forgotten subscriptions on its own, so a
                # failed goodbye is not worth surfacing.
                log.debug(
                    f"[{self.device_id}] push: UNSUBSCRIBE {label} failed",
                    exc_info=True,
                )
        handle = gena.get("handle")
        if handle is not None:
            await handle.close()

    async def _follow_group(self) -> None:
        """Hold the subscriptions this room's group needs.

        Group volume is evented by the group's coordinator only, and a room
        that follows another reports no track (NOT_IMPLEMENTED), so both are
        subscribed on the coordinator: GroupRenderingControl always, its
        AVTransport while this room follows it. Called whenever the household
        topology changes.
        """
        if not self._push_started:
            return
        async with self._group_lock:
            leader = self._leader
            wanted: dict[str, tuple[str | None, str]] = {}
            grc = self._services.get("GroupRenderingControl", {}).get("event")
            if grc:
                wanted[_GROUP_VOLUME_LABEL] = (leader["host"] if leader else None, grc)
            if leader:
                avt = self._services["AVTransport"]["event"]
                wanted[_LEADER_TRANSPORT_LABEL] = (leader["host"], avt)
            for label in (_GROUP_VOLUME_LABEL, _LEADER_TRANSPORT_LABEL):
                held = self._gena.get(label)
                want = wanted.get(label)
                if held and want and held["host"] == want[0]:
                    continue
                if held:
                    await self._drop_subscription(label)
                if not want:
                    continue
                host, event_path = want
                source_ip = host or str(self.config.get("host", "") or "")
                handle = await self._open_callback(label, source_ip)
                if handle is None:
                    continue
                self._gena[label] = {
                    "host": host,
                    "event_path": event_path,
                    "callback_url": http_listener.callback_url(source_ip, handle.path),
                    "sid": "",
                    "timeout": _SUBSCRIBE_SECONDS,
                    "seq": -1,
                    "handle": handle,
                }
                await self._gena_subscribe(label)

    async def _gena_request(
        self, method: str, path: str, headers: dict[str, str], host: str | None = None,
    ) -> httpx.Response | None:
        """Send one GENA request (SUBSCRIBE / UNSUBSCRIBE) to a speaker."""
        if not self._client:
            return None
        url = f"http://{host}:{self.config.get('port', 1400)}{path}" if host else path
        return await self._client.request(method, url, headers=headers)

    async def _gena_subscribe(self, label: str) -> bool:
        """Create a fresh subscription for ``label``.

        Returns True when the speaker accepted it. The reply's SID identifies
        the subscription on every NOTIFY, and its granted TIMEOUT drives the
        renewal cadence.
        """
        gena = self._gena.get(label)
        if not gena:
            return False
        try:
            resp = await self._gena_request(
                "SUBSCRIBE",
                gena["event_path"],
                {
                    "CALLBACK": f"<{gena['callback_url']}>",
                    "NT": "upnp:event",
                    "TIMEOUT": f"Second-{_SUBSCRIBE_SECONDS}",
                },
                gena["host"],
            )
        except httpx.TransportError as e:
            log.warning(
                f"[{self.device_id}] push: SUBSCRIBE {label} failed: {e}; "
                f"state updates will arrive at poll speed"
            )
            return False
        if resp is None or resp.status_code != 200:
            log.warning(
                f"[{self.device_id}] push: SUBSCRIBE {label} rejected "
                f"(HTTP {resp.status_code if resp else '?'}); state updates "
                f"will arrive at poll speed"
            )
            return False

        gena["sid"] = resp.headers.get("SID", "")
        gena["timeout"] = _parse_timeout(
            resp.headers.get("TIMEOUT", ""), _SUBSCRIBE_SECONDS
        )
        gena["seq"] = -1
        log.info(
            f"[{self.device_id}] push: subscribed to {label} "
            f"(expires in {gena['timeout']}s)"
        )
        return True

    async def _renew_loop(self) -> None:
        """Refresh every subscription at half its granted lifetime.

        A renewal the speaker no longer recognizes (412: it rebooted, or the
        subscription lapsed) is replaced with a fresh SUBSCRIBE, which also
        re-delivers the initial event so nothing is left stale.
        """
        try:
            while True:
                interval = min(
                    (g["timeout"] for g in self._gena.values()),
                    default=_SUBSCRIBE_SECONDS,
                )
                await asyncio.sleep(max(_MIN_RENEW_SECONDS, interval // 2))
                for label, gena in list(self._gena.items()):
                    if not gena.get("sid"):
                        await self._gena_subscribe(label)
                        continue
                    try:
                        resp = await self._gena_request(
                            "SUBSCRIBE",
                            gena["event_path"],
                            {
                                "SID": gena["sid"],
                                "TIMEOUT": f"Second-{_SUBSCRIBE_SECONDS}",
                            },
                            gena["host"],
                        )
                    except httpx.TransportError:
                        # The speaker is unreachable; the poll loop owns
                        # declaring the device offline. Try again next round.
                        continue
                    if resp is not None and resp.status_code == 200:
                        gena["timeout"] = _parse_timeout(
                            resp.headers.get("TIMEOUT", ""), gena["timeout"]
                        )
                        log.debug(
                            f"[{self.device_id}] push: renewed {label} "
                            f"({gena['timeout']}s)"
                        )
                        continue
                    log.info(
                        f"[{self.device_id}] push: {label} subscription no "
                        f"longer valid (HTTP "
                        f"{resp.status_code if resp else '?'}); resubscribing"
                    )
                    gena["sid"] = ""
                    await self._gena_subscribe(label)
        except asyncio.CancelledError:
            return

    async def _handle_notify(self, request: Any) -> None:
        """Handle one inbound UPnP NOTIFY.

        The body is a propertyset. AVTransport and RenderingControl event one
        property, LastChange: an escaped XML document naming every variable
        that changed. The other services event their variables as properties
        of their own. Which callback it came in on says whose it is: this
        speaker's, or the group coordinator's.
        """
        label = getattr(request, "label", "") or ""
        self._note_event_sequence(
            request.headers.get("sid", ""), request.headers.get("seq", "")
        )

        body = request.body.decode("utf-8", errors="replace")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            log.debug(f"[{self.device_id}] push: unparseable NOTIFY body")
            return

        properties: dict[str, str] = {}
        for prop in root:
            for var in prop:
                properties[_local_name(var.tag)] = var.text or ""
        last_change = properties.pop("LastChange", None)
        if last_change:
            self._apply_last_change(last_change, label)
        if properties:
            await self._apply_properties(properties, label)

    def _note_event_sequence(self, sid: str, seq: str) -> None:
        """Track each subscription's event key so a gap is visible in the log.

        A gap means events were missed (a busy speaker, a dropped delivery).
        Nothing to repair (the next poll resyncs), but it explains a state
        value that briefly looked stale.
        """
        if not sid or not seq.isdigit():
            return
        for label, gena in self._gena.items():
            if gena.get("sid") != sid:
                continue
            last, current = gena["seq"], int(seq)
            if last >= 0 and current != last + 1:
                log.debug(
                    f"[{self.device_id}] push: {label} event gap "
                    f"(seq {last} -> {current}); polling will resync"
                )
            gena["seq"] = current
            return

    async def _apply_properties(self, properties: dict[str, str], label: str) -> None:
        """Apply the services' own evented variables (not LastChange)."""
        updates: dict[str, Any] = {}
        name = properties.get("ZoneName")
        if name:
            updates["speaker_name"] = name
        if label == _GROUP_VOLUME_LABEL:
            volume = _as_int(properties.get("GroupVolume"))
            if volume is not None:
                updates["group_volume"] = volume
            if "GroupMute" in properties:
                updates["group_mute"] = properties["GroupMute"] == "1"
        connected = properties.get("LineInConnected")
        if connected is not None:
            updates["line_in_connected"] = connected.strip() == "1"
        if updates:
            self.set_states(updates)
        if properties.get("ZoneGroupState"):
            await self._apply_topology(_parse_topology(properties["ZoneGroupState"]))
        if "FavoritesUpdateID" in properties:
            self._refresh_soon("favorites", self._read_favorites)
        if "SavedQueuesUpdateID" in properties:
            self._refresh_soon("playlists", self._read_playlists)
        if "AlarmListVersion" in properties:
            self._refresh_soon("alarms", self._read_alarms)

    def _refresh_soon(self, key: str, read: Any) -> None:
        """Re-read a list once, however many change notices arrive together."""
        task = self._refresh_tasks.get(key)
        if task is not None and not task.done():
            return
        self._refresh_tasks[key] = asyncio.ensure_future(self._quietly(read()))

    def _apply_last_change(self, xml_text: str, label: str = "") -> None:
        """Apply one LastChange payload to state.

        The payload is `<Event><InstanceID val="0"><Var val="..."/>...`, and
        the spec says element order carries no meaning, so every changed
        variable is collected first and applied as one batch. Sonos is
        single-instance (InstanceID 0), and audio variables carry a `channel`
        attribute of which only Master is the room level (LF/RF are
        per-driver trims).

        While this room follows another, its own AVTransport says only that it
        follows (an x-rincon: URI, no track, "Stop, Play"); what it plays
        comes from the coordinator's (the gavt subscription), so the room's
        own transport event is read for its transport state alone.
        """
        try:
            event = ET.fromstring(xml_text)
        except ET.ParseError:
            log.debug(f"[{self.device_id}] push: unparseable LastChange")
            return

        updates: dict[str, Any] = {}
        for instance in event.iter():
            if _local_name(instance.tag) != "InstanceID":
                continue
            for var in instance:
                value = var.get("val")
                if value is None:
                    continue
                channel = var.get("channel")
                if channel is not None and channel != "Master":
                    continue
                self._collect_variable(_local_name(var.tag), value, updates)

        if label == "avt" and self._leader:
            updates = {k: v for k, v in updates.items() if k == "transport_state"}
        elif label == _LEADER_TRANSPORT_LABEL and not self._leader:
            return
        if label == _LEADER_TRANSPORT_LABEL:
            # The leader's alarm and sleep timer are its own.
            for key in ("alarm_running", "snooze_running", "_sleep_timer_generation"):
                updates.pop(key, None)

        if not updates:
            return

        # Stopped playback clears the now-playing fields, exactly as the poll
        # path does; otherwise push and the next poll would fight over them.
        if updates.get("transport_state") == "stopped":
            for key in _TRACK_KEYS:
                updates[key] = None

        generation = updates.pop("_sleep_timer_generation", None)
        refill = (
            updates.get("transport_state") in ("playing", "paused")
            and "track_title" not in updates
            and self.get_state("track_title") is None
        )
        self.set_states(updates)
        log.debug(f"[{self.device_id}] push: applied {sorted(updates)}")
        if refill:
            # Stopping cleared now playing, and a speaker that starts again
            # on the same track events the transport change alone (its track
            # did not change): read the track rather than wait for a poll.
            asyncio.ensure_future(self._quietly(self._read_position()))
        if generation is not None and generation != self._sleep_timer_generation:
            # The speaker events that its sleep timer changed, never the time
            # left: read it.
            self._sleep_timer_generation = generation
            asyncio.ensure_future(self._quietly(self._read_sleep_timer()))

    def _collect_variable(
        self, name: str, value: str, updates: dict[str, Any]
    ) -> None:
        """Map one LastChange variable onto driver state."""
        if name == "TransportState":
            updates["transport_state"] = _TRANSPORT_STATES.get(value, "stopped")
        elif name == "CurrentPlayMode":
            updates["play_mode"] = value
        elif name == "CurrentCrossfadeMode":
            updates["crossfade"] = value == "1"
        elif name == "Volume":
            if value.isdigit():
                updates["volume"] = int(value)
        elif name == "Mute":
            updates["mute"] = value == "1"
        elif name == "Bass":
            updates["bass"] = _as_int(value)
        elif name == "Treble":
            updates["treble"] = _as_int(value)
        elif name == "Loudness":
            updates["loudness"] = value == "1"
        elif name == "CurrentTrackDuration":
            updates["track_duration"] = value
        elif name == "CurrentTrackMetaData":
            # An empty metadata blob means "no track": clear rather than
            # ignore, or a finished stream leaves its last title on the panel.
            info = _parse_didl_metadata(value)
            updates["track_title"] = info["title"]
            updates["track_artist"] = info["artist"]
            updates["track_album"] = info["album"]
            updates["track_album_art"] = _absolute_url(self._base_url, info["album_art"])
        elif name == "NumberOfTracks":
            number = _as_int(value)
            if number is not None:
                updates["queue_length"] = number
        elif name == "CurrentTrack":
            number = _as_int(value)
            if number is not None:
                updates["track_number"] = number
        elif name == "AVTransportURI":
            updates["source"] = _source_of(value)
        elif name == "AVTransportURIMetaData":
            updates["source_name"] = _parse_didl_metadata(value)["title"]
        elif name == "CurrentTransportActions":
            actions = {part.strip() for part in value.split(",")}
            for action, key in _TRANSPORT_ACTION_KEYS.items():
                updates[key] = updates.get(key, False) or action in actions
        elif name == "SleepTimerGeneration":
            number = _as_int(value)
            if number is not None:
                updates["_sleep_timer_generation"] = number
        elif name == "AlarmRunning":
            updates["alarm_running"] = value == "1"
        elif name == "SnoozeRunning":
            updates["snooze_running"] = value == "1"
        elif name == "SonarEnabled":
            # Trueplay, evented beside SonarCalibrationAvailable; shown only
            # where the room has a tuning.
            if self._trueplay_available:
                updates["trueplay"] = value == "1"
        elif name in _HT_EQ and name in self._ht:
            updates[_HT_EQ[name]] = self._ht_value(name, value)

    def _ht_value(self, eq: str, value: str) -> Any:
        if eq in ("NightMode", "SubEnabled", "SurroundEnabled"):
            return value.strip() not in ("", "0")
        if eq == "DialogLevel":
            # Speech enhancement: 0 off, any other level on (an Amp reads 0
            # or 1; SpeechEnhanceEnabled refuses every write on it).
            return value.strip() not in ("", "0")
        if eq == "SurroundMode":
            return _SURROUND_MODES.get(value.strip())
        return _as_int(value)

    # --- The household and its groups ---

    async def _read_topology(self) -> None:
        resp = await self._soap("ZoneGroupTopology", "GetZoneGroupState")
        if resp is None:
            return
        await self._apply_topology(_parse_topology(_parse_xml_value(resp, "ZoneGroupState")))

    async def _apply_topology(self, groups: list[dict[str, Any]]) -> None:
        """Publish this room's group and the household's rooms."""
        if not groups:
            return
        self._groups = groups
        mine = next(
            (g for g in groups if any(m["uuid"] == self._uuid for m in g["members"])),
            None,
        )
        rooms = [
            {"value": m["uuid"], "label": m["name"]}
            for g in groups for m in g["members"]
            if m["visible"] and m["uuid"] != self._uuid
        ]
        rooms.sort(key=lambda room: room["label"].lower())
        updates: dict[str, Any] = {"rooms": json.dumps(rooms)}
        was_leader = self._leader
        if mine is None:
            self._leader = None
        else:
            visible = [m for m in mine["members"] if m["visible"]]
            leader = next(
                (m for m in mine["members"] if m["uuid"] == mine["coordinator"]), None,
            )
            names = [m["name"] for m in visible]
            if leader and leader["name"] in names:
                names.remove(leader["name"])
                names.insert(0, leader["name"])
            leads = mine["coordinator"] == self._uuid
            self._leader = None if leads or leader is None else {
                "uuid": leader["uuid"], "host": leader["host"], "name": leader["name"],
            }
            updates.update({
                "grouped": len(visible) > 1,
                "is_group_coordinator": leads,
                "group_coordinator": leader["name"] if leader else None,
                "group_members": ", ".join(names),
            })
        self.set_states(updates)
        if (was_leader or {}).get("uuid") != (self._leader or {}).get("uuid"):
            await self._follow_group()
            if not self._leader:
                # Back on its own (or leading): what it plays is its own again.
                asyncio.ensure_future(self._quietly(self._read_transport()))

    def _room(self, uuid: str) -> dict[str, Any] | None:
        for group in self._groups:
            for member in group["members"]:
                if member["uuid"] == uuid:
                    return {**member, "coordinator": group["coordinator"]}
        return None

    def _room_param(self, value: Any) -> dict[str, Any]:
        """A room picked by UUID (or typed by name) -> its topology entry."""
        text = str(value or "").strip()
        room = self._room(text)
        if room is None:
            room = next(
                (
                    {**m, "coordinator": g["coordinator"]}
                    for g in self._groups for m in g["members"]
                    if m["visible"] and m["name"].lower() == text.lower()
                ),
                None,
            )
        if room is None:
            raise CommandRefusedError(f"There is no Sonos room {text!r} in this household")
        if room["uuid"] == self._uuid:
            raise CommandRefusedError("Pick another room: that one is this room")
        return room

    def _leader_host(self) -> str | None:
        """Where transport commands go: the coordinator this room follows."""
        return self._leader["host"] if self._leader else None

    def _queue_uri(self) -> str:
        owner = self._leader["uuid"] if self._leader else self._uuid
        return f"x-rincon-queue:{owner}#0"

    # --- Favorites, playlists and alarms ---

    async def _browse(self, object_id: str) -> list[ET.Element]:
        resp = await self._soap(
            "ContentDirectory", "Browse", ObjectID=object_id,
            BrowseFlag="BrowseDirectChildren", Filter="*", StartingIndex="0",
            RequestedCount=str(_LIST_LIMIT), SortCriteria="",
        )
        root = _xml(_parse_xml_value(resp or "", "Result"))
        if root is None:
            return []
        return [el for el in root if _local_name(el.tag) in ("item", "container")]

    async def _read_favorites(self) -> None:
        """The household's Sonos favorites (ContentDirectory FV:2).

        A favorite carries what to play: its res (the URI) and r:resMD (its
        metadata). One saved as a shortcut (r:type shortcut, Sonos Radio's
        among them) has an empty res, and a speaker given it loads nothing and
        refuses Play (UPnP 701): those are left out of the picker.
        """
        if "ContentDirectory" not in self._services:
            return
        favorites: dict[str, dict[str, str]] = {}
        for element in await self._browse("FV:2"):
            fields = {_local_name(child.tag): (child.text or "") for child in element}
            res = fields.get("res", "").strip()
            title = fields.get("title", "").strip()
            fid = element.get("id") or ""
            if not res or not fid:
                continue
            meta = fields.get("resMD", "")
            favorites[fid] = {
                "title": title or fid,
                "res": res,
                "meta": meta,
                "class": _parse_didl_metadata(meta)["class"] or "",
            }
        self._favorites = favorites
        self.set_state("favorites", json.dumps(
            [{"value": fid, "label": f["title"]} for fid, f in favorites.items()]
        ))

    async def _read_playlists(self) -> None:
        """The household's Sonos playlists (ContentDirectory SQ:)."""
        if "ContentDirectory" not in self._services:
            return
        playlists: dict[str, dict[str, str]] = {}
        for element in await self._browse("SQ:"):
            fields = {_local_name(child.tag): (child.text or "") for child in element}
            pid = element.get("id") or ""
            res = fields.get("res", "").strip()
            if not pid or not res:
                continue
            playlists[pid] = {"title": fields.get("title", "").strip() or pid, "res": res}
        self._playlists = playlists
        self.set_state("playlists", json.dumps(
            [{"value": pid, "label": p["title"]} for pid, p in playlists.items()]
        ))

    async def _read_alarms(self) -> None:
        """This room's alarms (AlarmClock ListAlarms; the list is the household's)."""
        if "AlarmClock" not in self._services:
            return
        resp = await self._soap("AlarmClock", "ListAlarms")
        root = _xml(_parse_xml_value(resp or "", "CurrentAlarmList"))
        if root is None:
            return
        alarms: dict[str, dict[str, str]] = {}
        for element in root:
            if _local_name(element.tag) != "Alarm":
                continue
            attrs = dict(element.attrib)
            if attrs.get("RoomUUID") != self._uuid or not attrs.get("ID"):
                continue
            alarms[attrs["ID"]] = attrs
        self._alarms = alarms
        options = []
        for aid, alarm in sorted(alarms.items(), key=lambda kv: kv[1].get("StartTime", "")):
            recurrence = alarm.get("Recurrence", "")
            days = _RECURRENCES.get(recurrence) or (
                "custom days" if recurrence.startswith("ON_") else recurrence.lower()
            )
            on = "on" if alarm.get("Enabled") == "1" else "off"
            options.append({
                "value": aid,
                "label": f"{alarm.get('StartTime', '')[:5]} {days} ({on})",
            })
        self.set_state("alarms", json.dumps(options))

    def _pick(self, table: dict[str, dict[str, str]], value: Any, what: str) -> tuple[str, dict[str, str]]:
        """An entry picked by id (or typed by its name)."""
        text = str(value or "").strip()
        if text in table:
            return text, table[text]
        for key, entry in table.items():
            if entry.get("title", "").lower() == text.lower():
                return key, entry
        raise CommandRefusedError(f"There is no {what} {text!r} on this speaker")

    async def _alarm(self, value: Any) -> tuple[str, dict[str, str]]:
        text = str(value or "").strip()
        if text not in self._alarms:
            await self._read_alarms()
        if text not in self._alarms:
            raise CommandRefusedError(f"This room has no alarm {text!r}")
        return text, self._alarms[text]

    async def _update_alarm(self, aid: str, alarm: dict[str, str], **changes: str) -> None:
        fields = {arg: alarm.get(attr, "") for arg, attr in _ALARM_FIELDS}
        fields.update(changes)
        await self._soap("AlarmClock", "UpdateAlarm", required=True, ID=aid, **fields)
        await self._read_alarms()

    # --- Commands ---

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a command to the Sonos speaker (or its group's coordinator)."""
        params = params or {}

        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        lead = self._leader_host()
        match command:
            case "play":
                await self._soap(
                    "AVTransport", "Play", required=True, host=lead,
                    InstanceID="0", Speed="1",
                )
            case "pause":
                await self._soap("AVTransport", "Pause", required=True, host=lead, InstanceID="0")
            case "stop":
                await self._soap("AVTransport", "Stop", required=True, host=lead, InstanceID="0")
            case "next_track":
                await self._soap("AVTransport", "Next", required=True, host=lead, InstanceID="0")
            case "previous_track":
                await self._soap("AVTransport", "Previous", required=True, host=lead, InstanceID="0")
            case "seek":
                await self._soap(
                    "AVTransport", "Seek", required=True, host=lead, InstanceID="0",
                    Unit="REL_TIME", Target=_hms(str(params.get("position", ""))),
                )
                await self._read_position()
            case "play_queue_track":
                await self._play_queue_from(int(params.get("track", 1)))
            case "clear_queue":
                await self._soap(
                    "AVTransport", "RemoveAllTracksFromQueue",
                    required=True, host=lead, InstanceID="0",
                )
            case "play_favorite":
                await self._play_favorite(params.get("favorite"))
            case "play_playlist":
                await self._play_playlist(params.get("playlist"), replace=True)
            case "queue_playlist":
                await self._play_playlist(params.get("playlist"), replace=False)
            case "save_queue_as_playlist":
                title = str(params.get("title") or "").strip()
                if not title:
                    raise CommandRefusedError("A playlist needs a name")
                await self._soap(
                    "AVTransport", "SaveQueue", required=True, host=lead,
                    InstanceID="0", Title=title, ObjectID="",
                )
                await self._read_playlists()
            case "delete_playlist":
                pid, _ = self._pick(self._playlists, params.get("playlist"), "playlist")
                await self._soap("ContentDirectory", "DestroyObject", required=True, ObjectID=pid)
                await self._read_playlists()
            case "play_uri":
                uri = str(params.get("uri") or "").strip()
                title = str(params.get("title") or "").strip() or uri
                await self._set_source(uri, _didl_item(title, uri), lead)
            case "play_line_in":
                # Always this room's own transport: a room that follows
                # another leaves that group and plays its line-in on its own
                # (measured on a Sonos Amp); a room that leads plays it to the
                # whole group. Line-in loads paused, so Play follows.
                await self._set_source(f"x-rincon-stream:{self._uuid}", "", None)
            case "play_tv":
                # As line-in, this room's own transport. (Pointing the
                # coordinator at a follower's TV input instead regroups the
                # household around that follower and took about 10 s on a
                # Sonos One.) The TV input starts by itself.
                await self._soap(
                    "AVTransport", "SetAVTransportURI", required=True, InstanceID="0",
                    CurrentURI=f"x-sonos-htastream:{self._uuid}:spdif", CurrentURIMetaData="",
                )
            case "join_group":
                room = self._room_param(params.get("room"))
                if room["coordinator"] in ((self._leader or {}).get("uuid"), self._uuid):
                    # Already playing together.
                    return
                await self._soap(
                    "AVTransport", "SetAVTransportURI", required=True, InstanceID="0",
                    CurrentURI=f"x-rincon:{room['coordinator']}", CurrentURIMetaData="",
                )
                await self._read_topology()
            case "leave_group":
                await self._soap(
                    "AVTransport", "BecomeCoordinatorOfStandaloneGroup",
                    required=True, InstanceID="0",
                )
                await self._read_topology()
            case "add_room_to_group":
                room = self._room_param(params.get("room"))
                leader = self._leader["uuid"] if self._leader else self._uuid
                await self._soap(
                    "AVTransport", "SetAVTransportURI", required=True, host=room["host"],
                    InstanceID="0", CurrentURI=f"x-rincon:{leader}", CurrentURIMetaData="",
                )
                await self._read_topology()
            case "remove_room_from_group":
                room = self._room_param(params.get("room"))
                leader = self._leader["uuid"] if self._leader else self._uuid
                if room["coordinator"] != leader:
                    raise CommandRefusedError(f"{room['name']} is not in this room's group")
                await self._soap(
                    "AVTransport", "BecomeCoordinatorOfStandaloneGroup",
                    required=True, host=room["host"], InstanceID="0",
                )
                await self._read_topology()
            case "set_group_volume":
                level = max(0, min(100, int(params.get("level", 0))))
                await self._soap(
                    "GroupRenderingControl", "SetGroupVolume", required=True,
                    host=lead, InstanceID="0", DesiredVolume=str(level),
                )
                await self._read_group_volume()
            case "group_volume_up" | "group_volume_down":
                step = _as_int(params.get("step")) or 5
                resp = await self._soap(
                    "GroupRenderingControl", "SetRelativeGroupVolume", required=True,
                    host=lead, InstanceID="0",
                    Adjustment=str(step if command == "group_volume_up" else -step),
                )
                level = _as_int(_parse_xml_value(resp or "", "NewVolume"))
                if level is not None:
                    self.set_state("group_volume", level)
            case "group_mute_on" | "group_mute_off" | "group_mute_toggle":
                muted = (
                    command == "group_mute_on" if command != "group_mute_toggle"
                    else not bool(self.get_state("group_mute"))
                )
                await self._soap(
                    "GroupRenderingControl", "SetGroupMute", required=True,
                    host=lead, InstanceID="0", DesiredMute="1" if muted else "0",
                )
                await self._read_group_volume()
            case "set_volume":
                await self._set_volume(
                    max(0, min(100, int(params.get("level", 50))))
                )
            case "volume_up":
                await self._step_volume(_as_int(params.get("step")) or 5)
            case "volume_down":
                await self._step_volume(-(_as_int(params.get("step")) or 5))
            case "ramp_to_volume":
                await self._ramp_to_volume(
                    max(0, min(100, int(params.get("level", 0)))),
                    str(params.get("fade") or "SLEEP_TIMER_RAMP_TYPE"),
                )
            case "mute_on":
                await self._set_mute(True)
            case "mute_off":
                await self._set_mute(False)
            case "mute_toggle":
                await self._set_mute(not bool(self.get_state("mute")))
            case "set_bass":
                level = max(-10, min(10, int(params.get("level", 0))))
                await self._soap(
                    "RenderingControl", "SetBass",
                    required=True, InstanceID="0", DesiredBass=str(level),
                )
                await self._read_bass_treble()
            case "set_treble":
                level = max(-10, min(10, int(params.get("level", 0))))
                await self._soap(
                    "RenderingControl", "SetTreble",
                    required=True, InstanceID="0", DesiredTreble=str(level),
                )
                await self._read_bass_treble()
            case "set_loudness":
                enabled = _as_bool(params.get("enabled", True))
                await self._soap(
                    "RenderingControl", "SetLoudness",
                    required=True, InstanceID="0", Channel="Master",
                    DesiredLoudness="1" if enabled else "0",
                )
                await self._read_loudness()
            case "set_night_mode":
                await self._set_eq("NightMode", "1" if _as_bool(params.get("enabled")) else "0")
            case "set_speech_enhancement":
                await self._set_eq("DialogLevel", "1" if _as_bool(params.get("enabled")) else "0")
            case "set_play_mode":
                mode = str(params.get("mode", "NORMAL"))
                await self._soap(
                    "AVTransport", "SetPlayMode",
                    required=True, host=lead, InstanceID="0", NewPlayMode=mode,
                )
                await self._read_play_settings()
            case "set_crossfade":
                enabled = _as_bool(params.get("enabled", True))
                await self._soap(
                    "AVTransport", "SetCrossfadeMode",
                    required=True, host=lead, InstanceID="0",
                    CrossfadeMode="1" if enabled else "0",
                )
                await self._read_play_settings()
            case "set_sleep_timer":
                await self._soap(
                    "AVTransport", "ConfigureSleepTimer",
                    required=True, host=lead, InstanceID="0",
                    NewSleepTimerDuration=_hms(str(params.get("duration", ""))),
                )
                await self._read_sleep_timer()
            case "cancel_sleep_timer":
                await self._soap(
                    "AVTransport", "ConfigureSleepTimer",
                    required=True, host=lead, InstanceID="0", NewSleepTimerDuration="",
                )
                await self._read_sleep_timer()
            case "enable_alarm" | "disable_alarm":
                aid, alarm = await self._alarm(params.get("alarm"))
                await self._update_alarm(
                    aid, alarm, Enabled="1" if command == "enable_alarm" else "0",
                )
            case "delete_alarm":
                aid, _ = await self._alarm(params.get("alarm"))
                await self._soap("AlarmClock", "DestroyAlarm", required=True, ID=aid)
                await self._read_alarms()
            case "snooze_alarm":
                minutes = max(1, min(60, _as_int(params.get("minutes")) or 10))
                await self._soap(
                    "AVTransport", "SnoozeAlarm", required=True, host=lead,
                    # HH:MM:SS, the form a Sonos Amp took (00:05:00).
                    InstanceID="0", Duration=f"{minutes // 60:02d}:{minutes % 60:02d}:00",
                )
            case "set_status_light":
                await self._soap(
                    "DeviceProperties", "SetLEDState", required=True,
                    DesiredLEDState=_on_off(params.get("enabled", True)),
                )
                await self._read_speaker_controls()
            case "set_touch_controls_lock":
                await self._soap(
                    "DeviceProperties", "SetButtonLockState", required=True,
                    DesiredButtonLockState=_on_off(params.get("locked", False)),
                )
                await self._read_speaker_controls()
            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting.

        Each reads its value back from the speaker after the write.
        """
        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if key == "bass":
            await self.send_command("set_bass", {"level": int(value)})
        elif key == "treble":
            await self.send_command("set_treble", {"level": int(value)})
        elif key == "loudness":
            await self.send_command("set_loudness", {"enabled": _as_bool(value)})
        elif key == "play_mode":
            await self.send_command("set_play_mode", {"mode": str(value)})
        elif key == "crossfade":
            await self.send_command("set_crossfade", {"enabled": _as_bool(value)})
        elif key == "status_light":
            await self.send_command("set_status_light", {"enabled": _as_bool(value)})
        elif key == "touch_controls_locked":
            await self.send_command(
                "set_touch_controls_lock", {"locked": _as_bool(value)}
            )
        elif key == "trueplay":
            await self._soap(
                "RenderingControl", "SetRoomCalibrationStatus", required=True,
                InstanceID="0", RoomCalibrationEnabled="1" if _as_bool(value) else "0",
            )
            await self._read_trueplay()
        elif key == "surround_mode":
            mode = {v: k for k, v in _SURROUND_MODES.items()}.get(str(value))
            if mode is None:
                raise ValueError(f"Unknown surround playback: {value}")
            await self._set_eq("SurroundMode", mode)
        else:
            eq = next((eq for eq, state in _HT_EQ.items() if state == key), None)
            if eq is None:
                raise ValueError(f"Unknown device setting: {key}")
            if isinstance(value, bool) or eq in ("NightMode", "SubEnabled", "SurroundEnabled", "DialogLevel"):
                await self._set_eq(eq, "1" if _as_bool(value) else "0")
            else:
                await self._set_eq(eq, str(int(value)))

    async def poll(self) -> None:
        """Resync state, and read what Sonos never pushes.

        Push keeps transport state, volume, mute, tone controls, play mode,
        crossfade, track metadata, groups, favorites and alarms current within
        a second. This poll exists to (a) recover from a missed or filtered
        event and (b) read what is not evented: the track position, the sleep
        timer's time left, the status light and the touch-control lock.
        """
        if not self._client:
            return

        try:
            resp = await self._soap("AVTransport", "GetTransportInfo", InstanceID="0")
            if resp:
                raw_state = _parse_xml_value(resp, "CurrentTransportState")
                if raw_state:
                    state = _TRANSPORT_STATES.get(raw_state, "stopped")
                    old = self.get_state("transport_state")
                    self.set_state("transport_state", state)
                    if state != old:
                        log.info(f"[{self.device_id}] Transport: {state}")

            await self._read_volume()
            await self._read_mute()

            transport = self.get_state("transport_state")
            if transport in ("playing", "paused"):
                await self._read_position()
            elif transport == "stopped":
                # Clear track info when stopped (mirrors the push path). A
                # speaker between tracks reads TRANSITIONING; leave it alone.
                self.set_states({key: None for key in _TRACK_KEYS})

            if self.get_state("sleep_timer_remaining"):
                await self._read_sleep_timer()

            if self._polls % _RESYNC_EVERY_POLLS == 0:
                await self._resync()
            self._polls += 1

        except httpx.TransportError as exc:
            # Re-raise as ConnectionError so the BaseDriver poll-loop
            # watchdog counts this as a dry poll and eventually flips
            # device.<id>.connected to False.
            raise ConnectionError(
                f"Sonos at {self._base_url} not responding: {exc}"
            ) from exc

    async def _resync(self) -> None:
        """Read everything else the driver shows, once in a while."""
        await self._read_topology()
        await self._read_bass_treble()
        await self._read_loudness()
        await self._read_home_theater()
        await self._read_trueplay()
        await self._read_play_settings()
        await self._read_media_info()
        await self._read_sleep_timer()
        await self._read_speaker_controls()
        await self._read_group_volume()
        await self._read_favorites()
        await self._read_playlists()
        await self._read_alarms()

    # --- Reads (each sets state from the speaker's own answer) ---

    async def _read_transport(self) -> None:
        """What this room plays, read again (after it leaves a group)."""
        await self._read_play_settings()
        await self._read_media_info()
        await self._read_position()

    async def _read_volume(self) -> None:
        resp = await self._soap(
            "RenderingControl", "GetVolume", InstanceID="0", Channel="Master",
        )
        if resp:
            vol_str = _parse_xml_value(resp, "CurrentVolume")
            if vol_str and vol_str.isdigit():
                self.set_state("volume", int(vol_str))

    async def _read_mute(self) -> None:
        resp = await self._soap(
            "RenderingControl", "GetMute", InstanceID="0", Channel="Master",
        )
        if resp:
            mute_str = _parse_xml_value(resp, "CurrentMute")
            if mute_str is not None:
                self.set_state("mute", mute_str == "1")

    async def _read_group_volume(self) -> None:
        if "GroupRenderingControl" not in self._services:
            return
        lead = self._leader_host()
        updates: dict[str, Any] = {}
        resp = await self._soap("GroupRenderingControl", "GetGroupVolume", host=lead, InstanceID="0")
        level = _as_int(_parse_xml_value(resp or "", "CurrentVolume"))
        if level is not None:
            updates["group_volume"] = level
        resp = await self._soap("GroupRenderingControl", "GetGroupMute", host=lead, InstanceID="0")
        mute = _parse_xml_value(resp or "", "CurrentMute")
        if mute is not None:
            updates["group_mute"] = mute == "1"
        if updates:
            self.set_states(updates)

    async def _read_bass_treble(self) -> None:
        updates: dict[str, Any] = {}
        for action, tag, key in (
            ("GetBass", "CurrentBass", "bass"),
            ("GetTreble", "CurrentTreble", "treble"),
        ):
            resp = await self._soap("RenderingControl", action, InstanceID="0")
            if resp:
                level = _as_int(_parse_xml_value(resp, tag))
                if level is not None:
                    updates[key] = level
        if updates:
            self.set_states(updates)

    async def _read_loudness(self) -> None:
        resp = await self._soap(
            "RenderingControl", "GetLoudness", InstanceID="0", Channel="Master",
        )
        if resp:
            loud = _parse_xml_value(resp, "CurrentLoudness")
            if loud is not None:
                self.set_state("loudness", loud == "1")

    async def _read_home_theater(self) -> None:
        updates: dict[str, Any] = {}
        for eq in sorted(self._ht):
            resp = await self._soap("RenderingControl", "GetEQ", InstanceID="0", EQType=eq)
            value = _parse_xml_value(resp or "", "CurrentValue")
            if value is not None:
                updates[_HT_EQ[eq]] = self._ht_value(eq, value)
        if updates:
            self.set_states(updates)

    async def _read_trueplay(self) -> None:
        if not self._trueplay_available:
            return
        resp = await self._soap("RenderingControl", "GetRoomCalibrationStatus", InstanceID="0")
        enabled = _parse_xml_value(resp or "", "RoomCalibrationEnabled")
        if enabled is not None:
            self.set_state("trueplay", enabled == "1")

    async def _read_play_settings(self) -> None:
        """Play mode and crossfade (the coordinator's, for a follower)."""
        lead = self._leader_host()
        resp = await self._soap("AVTransport", "GetTransportSettings", host=lead, InstanceID="0")
        if resp:
            mode = _parse_xml_value(resp, "PlayMode")
            if mode in _PLAY_MODES:
                self.set_state("play_mode", mode)
        resp = await self._soap("AVTransport", "GetCrossfadeMode", host=lead, InstanceID="0")
        if resp:
            crossfade = _parse_xml_value(resp, "CrossfadeMode")
            if crossfade is not None:
                self.set_state("crossfade", crossfade == "1")

    async def _read_position(self) -> None:
        resp = await self._soap(
            "AVTransport", "GetPositionInfo", host=self._leader_host(), InstanceID="0",
        )
        if not resp:
            return
        updates: dict[str, Any] = {}
        duration = _parse_xml_value(resp, "TrackDuration")
        position = _parse_xml_value(resp, "RelTime")
        track = _as_int(_parse_xml_value(resp, "Track"))
        if duration and duration != "NOT_IMPLEMENTED":
            updates["track_duration"] = duration
        if position and position != "NOT_IMPLEMENTED":
            updates["track_position"] = position
        if track is not None:
            updates["track_number"] = track

        metadata = _parse_xml_value(resp, "TrackMetaData")
        if metadata:
            info = _parse_didl_metadata(metadata)
            if info["title"]:
                old_title = self.get_state("track_title")
                updates["track_title"] = info["title"]
                if info["title"] != old_title:
                    log.info(
                        f"[{self.device_id}] Now playing: "
                        f"{info['artist'] or 'Unknown'} - {info['title']}"
                    )
            if info["artist"]:
                updates["track_artist"] = info["artist"]
            if info["album"]:
                updates["track_album"] = info["album"]
            if info["album_art"]:
                updates["track_album_art"] = _absolute_url(
                    self._base_url, info["album_art"]
                )
        if updates:
            self.set_states(updates)

    async def _read_media_info(self) -> None:
        """The queue's length, the source and its name, what it allows."""
        lead = self._leader_host()
        resp = await self._soap("AVTransport", "GetMediaInfo", host=lead, InstanceID="0")
        if not resp:
            return
        updates: dict[str, Any] = {}
        tracks = _as_int(_parse_xml_value(resp, "NrTracks"))
        if tracks is not None:
            updates["queue_length"] = tracks
        uri = _parse_xml_value(resp, "CurrentURI")
        if uri is not None:
            updates["source"] = _source_of(_unescape(uri))
        metadata = _parse_xml_value(resp, "CurrentURIMetaData")
        if metadata is not None:
            updates["source_name"] = _parse_didl_metadata(metadata)["title"]
        resp = await self._soap(
            "AVTransport", "GetCurrentTransportActions", host=lead, InstanceID="0",
        )
        if resp:
            self._collect_variable(
                "CurrentTransportActions", _parse_xml_value(resp, "Actions") or "",
                updates,
            )
        if updates:
            self.set_states(updates)

    async def _read_sleep_timer(self) -> None:
        resp = await self._soap(
            "AVTransport", "GetRemainingSleepTimerDuration",
            host=self._leader_host(), InstanceID="0",
        )
        if not resp:
            return
        remaining = _parse_xml_value(resp, "RemainingSleepTimerDuration") or ""
        self.set_state("sleep_timer_remaining", _hms(remaining) if remaining else "")
        generation = _as_int(_parse_xml_value(resp, "CurrentSleepTimerGeneration"))
        if generation is not None:
            self._sleep_timer_generation = generation

    async def _read_speaker_controls(self) -> None:
        """Status light and touch-control lock (neither is evented)."""
        updates: dict[str, Any] = {}
        resp = await self._soap("DeviceProperties", "GetLEDState")
        if resp:
            led = _parse_xml_value(resp, "CurrentLEDState")
            if led in ("On", "Off"):
                updates["status_light"] = led == "On"
        resp = await self._soap("DeviceProperties", "GetButtonLockState")
        if resp:
            lock = _parse_xml_value(resp, "CurrentButtonLockState")
            if lock in ("On", "Off"):
                updates["touch_controls_locked"] = lock == "On"
        if updates:
            self.set_states(updates)

    async def _quietly(self, read: Any) -> None:
        """Run a read started from an event: the poll owns reporting a dead
        link, so a transport error here is dropped."""
        try:
            await read
        except httpx.TransportError:
            pass

    # --- Writes ---

    async def _set_volume(self, level: int) -> None:
        await self._soap(
            "RenderingControl", "SetVolume",
            required=True, InstanceID="0", Channel="Master",
            DesiredVolume=str(level),
        )
        await self._read_volume()

    async def _step_volume(self, adjustment: int) -> None:
        """Raise or lower the volume in one action, from where it is now.

        SetRelativeVolume works from the speaker's own level, so two quick
        presses each count, and answers with the new level (clamped to
        0-100 by the speaker).
        """
        resp = await self._soap(
            "RenderingControl", "SetRelativeVolume",
            required=True, InstanceID="0", Channel="Master",
            Adjustment=str(adjustment),
        )
        level = _as_int(_parse_xml_value(resp or "", "NewVolume"))
        if level is not None:
            self.set_state("volume", level)
        else:
            await self._read_volume()

    async def _ramp_to_volume(self, level: int, ramp_type: str) -> None:
        await self._soap(
            "RenderingControl", "RampToVolume",
            required=True, InstanceID="0", Channel="Master",
            RampType=ramp_type, DesiredVolume=str(level),
            ResetVolumeAfter="0", ProgramURI="",
        )

    async def _set_mute(self, muted: bool) -> None:
        await self._soap(
            "RenderingControl", "SetMute",
            required=True, InstanceID="0", Channel="Master",
            DesiredMute="1" if muted else "0",
        )
        await self._read_mute()

    async def _set_eq(self, eq: str, value: str) -> None:
        await self._soap(
            "RenderingControl", "SetEQ", required=True,
            InstanceID="0", EQType=eq, DesiredValue=value,
        )
        resp = await self._soap("RenderingControl", "GetEQ", InstanceID="0", EQType=eq)
        current = _parse_xml_value(resp or "", "CurrentValue")
        if current is not None and eq in _HT_EQ:
            self.set_state(_HT_EQ[eq], self._ht_value(eq, current))

    async def _play_queue_from(self, track: int) -> None:
        """Play the queue from a track.

        The speaker plays its queue only once its transport is pointed at it
        (x-rincon-queue:<owner>#0), and Seek's TRACK_NR then picks the track;
        a number outside the queue is refused (UPnP 711).
        """
        lead = self._leader_host()
        if self.get_state("source") != "queue":
            await self._soap(
                "AVTransport", "SetAVTransportURI", required=True, host=lead,
                InstanceID="0", CurrentURI=self._queue_uri(), CurrentURIMetaData="",
            )
        await self._soap(
            "AVTransport", "Seek", required=True, host=lead, InstanceID="0",
            Unit="TRACK_NR", Target=str(track),
        )
        await self._soap(
            "AVTransport", "Play", required=True, host=lead, InstanceID="0", Speed="1",
        )

    async def _set_source(self, uri: str, metadata: str, host: str | None) -> None:
        await self._soap(
            "AVTransport", "SetAVTransportURI", required=True, host=host,
            InstanceID="0", CurrentURI=uri, CurrentURIMetaData=metadata,
        )
        await self._soap(
            "AVTransport", "Play", required=True, host=host, InstanceID="0", Speed="1",
        )

    async def _play_favorite(self, value: Any) -> None:
        if not self._favorites:
            await self._read_favorites()
        try:
            _, favorite = self._pick(self._favorites, value, "favorite")
        except CommandRefusedError:
            await self._read_favorites()
            _, favorite = self._pick(self._favorites, value, "favorite")
        lead = self._leader_host()
        if "container" in favorite["class"]:
            # A collection plays through the queue, as a playlist does.
            await self._replace_queue(favorite["res"], favorite["meta"], lead)
        else:
            await self._set_source(favorite["res"], favorite["meta"], lead)

    async def _play_playlist(self, value: Any, replace: bool) -> None:
        if not self._playlists:
            await self._read_playlists()
        _, playlist = self._pick(self._playlists, value, "playlist")
        lead = self._leader_host()
        if replace:
            await self._replace_queue(playlist["res"], "", lead)
            return
        await self._soap(
            "AVTransport", "AddURIToQueue", required=True, host=lead, InstanceID="0",
            EnqueuedURI=playlist["res"], EnqueuedURIMetaData="",
            DesiredFirstTrackNumberEnqueued="0", EnqueueAsNext="0",
        )

    async def _replace_queue(self, uri: str, metadata: str, host: str | None) -> None:
        """Empty the queue, add a collection to it, and play it from the start.

        AddURIToQueue with a playlist's own res (file:///jffs/settings/
        savedqueues.rsq#<n>) adds every track of it (measured: three tracks
        from a three-track playlist, with or without its metadata).
        """
        await self._soap(
            "AVTransport", "RemoveAllTracksFromQueue", required=True, host=host,
            InstanceID="0",
        )
        await self._soap(
            "AVTransport", "AddURIToQueue", required=True, host=host, InstanceID="0",
            EnqueuedURI=uri, EnqueuedURIMetaData=metadata,
            DesiredFirstTrackNumberEnqueued="0", EnqueueAsNext="0",
        )
        await self._soap(
            "AVTransport", "SetAVTransportURI", required=True, host=host,
            InstanceID="0", CurrentURI=self._queue_uri(), CurrentURIMetaData="",
        )
        await self._soap(
            "AVTransport", "Play", required=True, host=host, InstanceID="0", Speed="1",
        )

    async def _soap(
        self,
        service: str,
        action: str,
        required: bool = False,
        host: str | None = None,
        **params: Any,
    ) -> str | None:
        """Send a SOAP request to one of the speaker's services.

        ``host`` sends it to another speaker of the household instead (the
        group's coordinator), at the same path: every Sonos player serves
        its services at the same places. Returns the response body text.
        ``required=True`` (every state-changing action) raises when the
        speaker rejects the call, so a command that did not take effect
        reports as failed instead of silently succeeding. Queries leave it
        False: a failed read just means this poll had nothing to say.
        """
        if not self._client:
            return None
        svc = self._services.get(service)
        if not svc or not svc.get("control"):
            if required:
                raise CommandRefusedError(
                    f"This speaker has no {service} service, so {action} "
                    f"cannot run on it"
                )
            return None

        body, soap_action = _build_soap(svc["type"], action, **params)
        url = (
            f"http://{host}:{self.config.get('port', 1400)}{svc['control']}"
            if host else svc["control"]
        )

        # Let httpx transport errors propagate: callers (connect, poll)
        # translate them to ConnectionError so the platform watchdog can flip
        # device.<id>.connected to False.
        log.debug(f"[{self.device_id}] SOAP {action}{f' @ {host}' if host else ''}")
        resp = await self._client.post(
            url,
            content=body.encode("utf-8"),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": soap_action,
            },
        )
        log.debug(f"[{self.device_id}] SOAP {action} -> {resp.status_code}")

        if resp.status_code == 200:
            return resp.text

        detail = _soap_fault_detail(resp.text)
        log.debug(
            f"[{self.device_id}] SOAP {action} failed: "
            f"HTTP {resp.status_code}{detail}"
        )
        if required:
            log.warning(f"[{self.device_id}] The speaker refused {action}{detail}")
            raise CommandRefusedError(f"The speaker refused {action}{detail}")
        return None

    async def _get_speaker_name(self) -> str | None:
        """Query the speaker name via DeviceProperties."""
        resp = await self._soap("DeviceProperties", "GetZoneAttributes")
        if resp:
            return _parse_xml_value(resp, "CurrentZoneName")
        return None
