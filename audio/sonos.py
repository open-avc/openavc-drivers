"""
OpenAVC Sonos Speaker Driver.

Controls Sonos speakers via the local UPnP/SOAP API on port 1400.
Works with all Sonos models (S1 and S2 firmware). No authentication
required — any device on the same network can control the speaker.

Protocol: HTTP POST with SOAP/XML payloads to port 1400.
Services used:
  - AVTransport: play, pause, stop, next, previous, track info, play mode
  - RenderingControl: volume, mute, bass, treble, loudness
  - DeviceProperties: speaker name

Push (UPnP GENA):
  Sonos implements standard UPnP eventing. The driver SUBSCRIBEs to the
  AVTransport and RenderingControl services with a CALLBACK URL served by the
  platform's HTTP push listener, and the speaker NOTIFYs every state change to
  it — transport state, volume, mute, bass/treble/loudness, play mode and
  track metadata arrive within a second instead of waiting for a poll, no
  matter whether the change came from OpenAVC, the Sonos app, or the buttons
  on the speaker. Accepting a subscription also delivers a free initial event
  carrying the complete evented state, so the driver is in sync the moment it
  connects.

  Subscriptions expire: a renewal task refreshes each SID at half its granted
  lifetime, and one the speaker no longer recognizes (HTTP 412 after a reboot)
  is re-created from scratch. Polling stays on as the resync baseline and as
  the only source of track *position* — RelTime is not an evented variable
  (verified against a real speaker's event payload; see the shipped record).

Why Python (not YAML):
  SOAP envelopes with per-action SOAPAction headers, the GENA subscription
  lifecycle (SID tracking, renewal timers, UNSUBSCRIBE), and LastChange
  payloads that fan a single evented variable out into ten state keys through
  two layers of XML escaping. None of that fits ConfigurableDriver.

Reference:
  UPnP Device Architecture 2.0 §4 (eventing) and the AVTransport:1 /
  RenderingControl:1 service templates (the LastChange model), plus the
  speaker's own SCPD service descriptions for authoritative value ranges.
  Community service docs (https://sonos.svrooij.io/services/) cross-check the
  endpoint paths.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from openavc.drivers.base import BaseDriver
from openavc.transport import http_listener
from openavc.utils.logger import get_logger

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

# Service control endpoints
_AV_TRANSPORT = "/MediaRenderer/AVTransport/Control"
_RENDERING_CONTROL = "/MediaRenderer/RenderingControl/Control"
_DEVICE_PROPERTIES = "/DeviceProperties/Control"

# Service event endpoints (GENA SUBSCRIBE targets)
_AV_TRANSPORT_EVENT = "/MediaRenderer/AVTransport/Event"
_RENDERING_CONTROL_EVENT = "/MediaRenderer/RenderingControl/Event"

# Service type URNs
_AV_TRANSPORT_URN = "AVTransport"
_RENDERING_CONTROL_URN = "RenderingControl"
_DEVICE_PROPERTIES_URN = "DeviceProperties"

# GENA subscriptions: label -> event endpoint. The label becomes the last
# segment of the platform's callback path (/api/push/<device>/<label>), so each
# service NOTIFYs to its own URL and the protocol log stays readable.
_EVENT_SERVICES = {
    "avt": _AV_TRANSPORT_EVENT,
    "rc": _RENDERING_CONTROL_EVENT,
}

# Requested subscription lifetime. The UPnP spec says a publisher SHOULD grant
# at least 1800 s; Sonos grants exactly what it is asked for (verified on
# hardware), so ask for the spec's floor rather than something exotic.
_SUBSCRIBE_SECONDS = 1800
# Never renew less often than this, whatever the speaker granted.
_MIN_RENEW_SECONDS = 60

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
    "track_duration",
    "track_position",
)


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


def _local_name(tag: str) -> str:
    """Strip any XML namespace from a tag ('{urn:...}Volume' -> 'Volume').

    Vendors pick their own namespace prefixes, and Sonos's LastChange
    namespaces even carry a trailing slash the spec examples don't show — so
    every tag test in this driver is namespace-insensitive by construction.
    """
    return tag.split("}")[-1] if "}" in tag else tag


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

    Sonos returns track metadata as escaped XML — inside a SOAP response, and
    escaped a second time inside a LastChange event payload. Callers pass the
    already-once-unescaped string either way.
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
            tag = _local_name(item.tag)
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
    errorCode — that number is the difference between "the speaker is broken"
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


# The rejections an AV integrator can actually act on. Anything else surfaces
# with its bare code — the number is enough to search the UPnP spec for.
_UPNP_ERRORS = {
    "701": "the speaker is not in a state where that action applies",
    "710": "no queue / playlist is loaded",
    "711": "the transport is locked or restricted",
    "712": "the requested play mode is not supported by what is playing",
    "718": "invalid instance",
}


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes")


class SonosDriver(BaseDriver):
    """Sonos speaker control driver via local UPnP/SOAP API + GENA push."""

    DRIVER_INFO = {
        "id": "sonos",
        "name": "Sonos Speaker",
        "manufacturer": "Sonos",
        "category": "audio",
        "version": "2.0.2",
        "author": "OpenAVC",
        "description": (
            "Controls Sonos speakers via the local UPnP API. Play/pause, "
            "volume, mute, bass/treble, track info, and transport control. "
            "The speaker pushes state changes as they happen (UPnP eventing)."
        ),
        "source_url": "https://sonos.svrooij.io/services/",
        "tags": ["speaker", "wireless-audio", "background-music", "upnp"],
        "verified": True,
        "simulated": True,
        "ports": [1400],
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "compatible_models": [
            {
                "manufacturer": "Sonos",
                "models": [
                    "Sonos S1 platform speakers",
                    "Sonos S2 platform speakers",
                ],
                "confidence": "full",
                "notes": (
                    "UPnP API stable across all Sonos models. Control and "
                    "eventing verified on a Sonos Amp (S16) and a Sonos One "
                    "(S18), firmware 95.1 (S2)."
                ),
            },
        ],
        "transport": "http",
        # The speaker POSTs UPnP NOTIFY events to a callback URL the platform
        # assigns. This driver drives the GENA handshake itself (SUBSCRIBE /
        # renew / UNSUBSCRIBE) — see _start_push below.
        "push": {"type": "http_listener"},
        "help": {
            "overview": (
                "Controls any Sonos speaker on the local network via UPnP. "
                "Works with all Sonos models including One, Five, Beam, Arc, "
                "Era, Move, Roam, Port, Amp, and legacy Play/Connect models. "
                "No cloud account or API key required. The speaker pushes "
                "state changes as they happen, so transport state, volume, "
                "mute and now-playing info update on panels within about a "
                "second — whether the change came from OpenAVC, the Sonos "
                "app, or a button on the speaker itself."
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
                "values": _PLAY_MODES,
                "label": "Play Mode",
                "help": "Queue repeat / shuffle mode.",
            },
            "track_title": {"type": "string", "label": "Track Title"},
            "track_artist": {"type": "string", "label": "Track Artist"},
            "track_album": {"type": "string", "label": "Track Album"},
            "track_duration": {"type": "string", "label": "Track Duration"},
            "track_position": {
                "type": "string",
                "label": "Track Position",
                # Advances on every poll while playing — background telemetry,
                # not something the cloud needs at the high-priority cadence.
                "cloud_priority": "low",
            },
            "speaker_name": {"type": "string", "label": "Speaker Name"},
        },
        # Writable values the speaker persists and reports back (over the event
        # stream, and on poll) — the platform gives them an editable field plus
        # the offline pending queue. Ranges come from the speaker's own SCPD.
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
                "values": _PLAY_MODES,
                "label": "Play Mode",
                "state_key": "play_mode",
                "default": "NORMAL",
                "setup": False,
                "help": "Queue repeat / shuffle mode.",
            },
        },
        "quick_actions": ["play", "pause", "stop", "mute_toggle"],
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
            },
            "set_play_mode": {
                "label": "Set Play Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "values": _PLAY_MODES,
                        "required": True,
                        "help": "Queue repeat / shuffle mode.",
                    },
                },
                "help": "Set the queue's repeat / shuffle mode.",
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
        # GENA state per subscription label: event path, callback URL, SID,
        # granted timeout, last SEQ seen.
        self._gena: dict[str, dict[str, Any]] = {}
        self._renew_task: asyncio.Task | None = None

    async def _create_transport(self, transport_type: str) -> None:
        """Driver-owned session: SOAP over plain HTTP on port 1400.

        No platform transport — the httpx client is the connection, so
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
        try:
            name = await self._get_speaker_name()
        except Exception as e:
            raise ConnectionError(
                f"Failed to connect to Sonos at {host}:{port}: {e}"
            )
        if name:
            self.set_state("speaker_name", name)
            log.info(f"[{self.device_id}] Speaker name: {name}")

    async def _initial_sync(self) -> None:
        # Push is already subscribed (accepting a GENA subscription delivers
        # an initial event with the full evented state); this first poll fills
        # in track position, which Sonos does not push.
        await self.poll()

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    # --- Push: UPnP GENA subscriptions ---

    async def _start_push(self) -> None:
        """Subscribe to the speaker's AVTransport + RenderingControl events.

        Overrides BaseDriver's generic http_listener registration because GENA
        is a real handshake — SUBSCRIBE returns a SID with a lifetime that must
        be renewed — and one speaker needs a subscription per service. Never
        raises: a speaker whose events can't be subscribed still works at poll
        speed, and the gap is logged.
        """
        handles = []
        host = str(self.config.get("host", "") or "")
        for label, event_path in _EVENT_SERVICES.items():
            try:
                sub = await http_listener.subscribe(
                    device_id=self.device_id,
                    source_ip=host,
                    callback=self._handle_notify,
                    name=f"{self.device_id}:{label}",
                    label=label,
                )
            except Exception:
                log.warning(
                    f"[{self.device_id}] push: could not register the "
                    f"{label} callback",
                    exc_info=True,
                )
                continue
            handles.append(sub)
            self._gena[label] = {
                "event_path": event_path,
                "callback_url": http_listener.callback_url(host, sub.path),
                "sid": "",
                "timeout": _SUBSCRIBE_SECONDS,
                "seq": -1,
            }
            await self._gena_subscribe(label)

        if handles:
            self._push_subscription = handles
        if any(g["sid"] for g in self._gena.values()):
            self._renew_task = asyncio.create_task(self._renew_loop())

    async def _stop_push(self) -> None:
        """Cancel renewals, UNSUBSCRIBE from the speaker, drop the callbacks."""
        task = self._renew_task
        self._renew_task = None
        if task is not None:
            task.cancel()

        for label, gena in list(self._gena.items()):
            if not gena.get("sid"):
                continue
            try:
                await self._gena_request(
                    "UNSUBSCRIBE", gena["event_path"], {"SID": gena["sid"]}
                )
                log.debug(f"[{self.device_id}] push: unsubscribed {label}")
            except Exception:
                # A speaker expires forgotten subscriptions on its own, so a
                # failed goodbye is not worth surfacing.
                log.debug(
                    f"[{self.device_id}] push: UNSUBSCRIBE {label} failed",
                    exc_info=True,
                )
        self._gena.clear()

        # Drop the platform-side callback registrations.
        await super()._stop_push()

    async def _gena_request(
        self, method: str, path: str, headers: dict[str, str]
    ) -> httpx.Response | None:
        """Send one GENA request (SUBSCRIBE / UNSUBSCRIBE) to the speaker."""
        if not self._client:
            return None
        return await self._client.request(method, path, headers=headers)

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
            )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            log.warning(
                f"[{self.device_id}] push: SUBSCRIBE {label} failed: {e} — "
                f"state updates will arrive at poll speed"
            )
            return False
        if resp is None or resp.status_code != 200:
            log.warning(
                f"[{self.device_id}] push: SUBSCRIBE {label} rejected "
                f"(HTTP {resp.status_code if resp else '?'}) — state updates "
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

        A renewal the speaker no longer recognizes (412 — it rebooted, or the
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
                        )
                    except (httpx.ConnectError, httpx.TimeoutException):
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
                        f"{resp.status_code if resp else '?'}) — resubscribing"
                    )
                    gena["sid"] = ""
                    await self._gena_subscribe(label)
        except asyncio.CancelledError:
            return

    async def _handle_notify(self, request: Any) -> None:
        """Handle one inbound UPnP NOTIFY from the speaker.

        The body is a propertyset whose only evented property is LastChange: an
        escaped XML document naming every variable that changed. Which service
        sent it doesn't matter — the payload's own element names say what to
        update.
        """
        self._note_event_sequence(
            request.headers.get("sid", ""), request.headers.get("seq", "")
        )

        body = request.body.decode("utf-8", errors="replace")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            log.debug(f"[{self.device_id}] push: unparseable NOTIFY body")
            return

        for element in root.iter():
            if _local_name(element.tag) == "LastChange" and element.text:
                self._apply_last_change(element.text)

    def _note_event_sequence(self, sid: str, seq: str) -> None:
        """Track each subscription's event key so a gap is visible in the log.

        A gap means events were missed (a busy speaker, a dropped delivery).
        Nothing to repair — the next poll resyncs — but it explains a state
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

    def _apply_last_change(self, xml_text: str) -> None:
        """Apply one LastChange payload to state.

        The payload is `<Event><InstanceID val="0"><Var val="..."/>…`, and the
        spec says element order carries no meaning — so every changed variable
        is collected first and applied as one batch. Sonos is single-instance
        (InstanceID 0), and audio variables carry a `channel` attribute of
        which only Master is the room level (LF/RF are per-driver trims).
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

        if not updates:
            return

        # Stopped playback clears the now-playing fields, exactly as the poll
        # path does — otherwise push and the next poll would fight over them.
        if updates.get("transport_state") == "stopped":
            for key in _TRACK_KEYS:
                updates[key] = None

        for key, value in updates.items():
            self.set_state(key, value)
        log.debug(f"[{self.device_id}] push: applied {sorted(updates)}")

    def _collect_variable(
        self, name: str, value: str, updates: dict[str, Any]
    ) -> None:
        """Map one LastChange variable onto driver state."""
        if name == "TransportState":
            updates["transport_state"] = _TRANSPORT_STATES.get(value, "stopped")
        elif name == "CurrentPlayMode":
            updates["play_mode"] = value
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
            # An empty metadata blob means "no track" — clear rather than
            # ignore, or a finished stream leaves its last title on the panel.
            info = _parse_didl_metadata(value)
            updates["track_title"] = info["title"]
            updates["track_artist"] = info["artist"]
            updates["track_album"] = info["album"]

    # --- Commands ---

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
                    required=True, InstanceID="0", Speed="1",
                )
            case "pause":
                await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "Pause",
                    required=True, InstanceID="0",
                )
            case "stop":
                await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "Stop",
                    required=True, InstanceID="0",
                )
            case "next_track":
                await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "Next",
                    required=True, InstanceID="0",
                )
            case "previous_track":
                await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "Previous",
                    required=True, InstanceID="0",
                )
            case "set_volume":
                await self._set_volume(
                    max(0, min(100, int(params.get("level", 50))))
                )
            case "volume_up":
                current = self.get_state("volume") or 0
                await self._set_volume(min(100, current + 5))
            case "volume_down":
                current = self.get_state("volume") or 0
                await self._set_volume(max(0, current - 5))
            case "mute_on":
                await self._set_mute(True)
            case "mute_off":
                await self._set_mute(False)
            case "mute_toggle":
                await self._set_mute(not bool(self.get_state("mute")))
            case "set_bass":
                level = max(-10, min(10, int(params.get("level", 0))))
                await self._soap_action(
                    _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "SetBass",
                    required=True, InstanceID="0", DesiredBass=str(level),
                )
                self.set_state("bass", level)
            case "set_treble":
                level = max(-10, min(10, int(params.get("level", 0))))
                await self._soap_action(
                    _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "SetTreble",
                    required=True, InstanceID="0", DesiredTreble=str(level),
                )
                self.set_state("treble", level)
            case "set_loudness":
                enabled = _as_bool(params.get("enabled", True))
                await self._soap_action(
                    _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "SetLoudness",
                    required=True, InstanceID="0", Channel="Master",
                    DesiredLoudness="1" if enabled else "0",
                )
                self.set_state("loudness", enabled)
            case "set_play_mode":
                mode = str(params.get("mode", "NORMAL"))
                await self._soap_action(
                    _AV_TRANSPORT, _AV_TRANSPORT_URN, "SetPlayMode",
                    required=True, InstanceID="0", NewPlayMode=mode,
                )
                self.set_state("play_mode", mode)
            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting (bass / treble / loudness / play mode).

        Each routes through the matching command so the SOAP call lives in one
        place. The speaker reports the new value back over its event stream —
        that is the setting's read-back.
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
        else:
            raise ValueError(f"Unknown device setting: {key}")

    async def poll(self) -> None:
        """Resync state, and read the one value Sonos never pushes: position.

        Push keeps transport state, volume, mute, tone controls, play mode and
        track metadata current within a second. This poll exists to (a) recover
        from a missed or filtered event and (b) advance the track position,
        which is not an evented variable.
        """
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
                # Clear track info when stopped (mirrors the push path)
                for key in _TRACK_KEYS:
                    self.set_state(key, None)

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            # Re-raise as ConnectionError so the BaseDriver poll-loop
            # watchdog counts this as a dry poll and eventually flips
            # device.<id>.connected to False.
            raise ConnectionError(
                f"Sonos at {self._base_url} not responding: {exc}"
            ) from exc

    # --- Internal helpers ---

    async def _set_volume(self, level: int) -> None:
        await self._soap_action(
            _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "SetVolume",
            required=True, InstanceID="0", Channel="Master",
            DesiredVolume=str(level),
        )
        self.set_state("volume", level)

    async def _set_mute(self, muted: bool) -> None:
        await self._soap_action(
            _RENDERING_CONTROL, _RENDERING_CONTROL_URN, "SetMute",
            required=True, InstanceID="0", Channel="Master",
            DesiredMute="1" if muted else "0",
        )
        self.set_state("mute", muted)

    async def _soap_action(
        self,
        endpoint: str,
        service: str,
        action: str,
        required: bool = False,
        **params: str,
    ) -> str | None:
        """Send a SOAP request and return the response body text.

        ``required=True`` (every state-changing action) raises when the speaker
        rejects the call, so a command that did not take effect reports as
        failed instead of silently succeeding. Queries leave it False: a failed
        read just means this poll had nothing to say.
        """
        if not self._client:
            return None

        body, soap_action = _build_soap(service, action, **params)

        # Let httpx.ConnectError / TimeoutException propagate — callers
        # (connect, poll) translate them to ConnectionError so the platform
        # watchdog can flip device.<id>.connected to False.
        log.debug(f"[{self.device_id}] SOAP {action}")
        resp = await self._client.post(
            endpoint,
            content=body.encode("utf-8"),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": soap_action,
            },
        )
        log.debug(
            f"[{self.device_id}] SOAP {action} -> {resp.status_code}"
        )

        if resp.status_code == 200:
            return resp.text

        detail = _soap_fault_detail(resp.text)
        log.warning(
            f"[{self.device_id}] SOAP {action} failed: "
            f"HTTP {resp.status_code}{detail}"
        )
        if required:
            raise RuntimeError(f"Speaker rejected {action}{detail}")
        return None

    async def _get_speaker_name(self) -> str | None:
        """Query the speaker name via DeviceProperties."""
        resp = await self._soap_action(
            _DEVICE_PROPERTIES, _DEVICE_PROPERTIES_URN, "GetZoneAttributes",
        )
        if resp:
            return _parse_xml_value(resp, "CurrentZoneName")
        return None
