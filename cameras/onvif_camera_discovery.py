"""Discovery companion for the ONVIF camera anchor driver.

ONVIF is an open multi-vendor standard for IP cameras and other AV
devices. Discovery uses WS-Discovery: a SOAP-over-UDP probe sent to
the multicast group 239.255.255.250:3702. Cameras answer with a
ProbeMatch envelope whose ``Scopes`` element carries one or more
``onvif://www.onvif.org/<category>/<value>`` URIs — the
``manufacturer`` and ``hardware`` categories are the load-bearing
fields for driver matching.

The wire format requires:

- A multicast send (the standard udp_probe: schema only does directed
  broadcast — multicast is a separate socket option set).
- A fresh ``MessageID`` UUID per envelope. Strict firmware rejects
  duplicates.
- SOAP / XML parsing on the reply to extract the scope URIs.

That mix is enough that the declarative ``udp_probe:`` block can't
express it, so this companion ships as the Python escape hatch.

Network safety: the socket binds to ``ctx.source_ip`` and the
multicast outbound interface is pinned to the same address, so on
multi-homed hosts the probe leaves through the right NIC and replies
route back the same way.

Reference for envelope shape: OASIS WS-Discovery 1.1 specification
(the wire format ONVIF requires). No third-party code is copied.

License: MIT (matches the OpenAVC drivers repo).
"""

from __future__ import annotations

import asyncio
import re
import socket
import struct
import uuid
from dataclasses import dataclass, field
from typing import Any

from defusedxml.ElementTree import fromstring as _safe_fromstring
from defusedxml.ElementTree import ParseError as _XMLParseError

from openavc.discovery.companion import ProbeContext


WSD_GROUP = "239.255.255.250"
WSD_PORT = 3702

# Filter the probe to NetworkVideoTransmitter (cameras + most ONVIF AV
# devices). ``dn:Device`` would also match printers and NAS — too
# noisy for AV discovery.
_PROBE_TYPES = "dn:NetworkVideoTransmitter"

# WS-Discovery + SOAP namespaces. Match URIs rather than prefixes
# because device firmware varies in its prefix choices.
_NS_ENVELOPE = "http://www.w3.org/2003/05/soap-envelope"
_NS_DISCOVERY = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
_NS_ADDRESSING = "http://schemas.xmlsoap.org/ws/2004/08/addressing"

# Match an ONVIF scope URI: onvif://www.onvif.org/<category>/<value>
_SCOPE_RE = re.compile(
    r"onvif://www\.onvif\.org/([A-Za-z][A-Za-z0-9]*)/(.+)",
    re.IGNORECASE,
)

# WS-Discovery spec recommends a 1-5 second listen window. Cameras
# with slow firmware may delay; 4 seconds is the sweet spot used by
# the original built-in scanner.
LISTEN_DURATION = 4.0

# TTL ceiling for the multicast send. Spec recommends 1; some routed
# networks need more, but 4 is a safe ceiling that won't escape the
# LAN.
MULTICAST_TTL = 4


@dataclass
class _ProbeMatch:
    """Parsed fields from one ProbeMatch element."""

    ip: str
    types: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    xaddrs: list[str] = field(default_factory=list)
    endpoint_reference: str = ""

    def scope_value(self, category: str) -> str | None:
        target = category.lower()
        for scope in self.scopes:
            match = _SCOPE_RE.match(scope.strip())
            if match and match.group(1).lower() == target:
                return match.group(2).strip()
        return None


def _build_probe_envelope() -> bytes:
    """Build a fresh WS-Discovery Probe envelope with a unique MessageID.

    Each call generates a new UUID per spec — duplicate MessageIDs
    are rejected by stricter firmware.
    """
    message_id = "urn:uuid:" + str(uuid.uuid4())
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<e:Envelope xmlns:e="{_NS_ENVELOPE}" '
        f'xmlns:w="{_NS_ADDRESSING}" '
        f'xmlns:d="{_NS_DISCOVERY}" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        '<e:Header>'
        f'<w:MessageID>{message_id}</w:MessageID>'
        '<w:To e:mustUnderstand="true">'
        'urn:schemas-xmlsoap-org:ws:2005:04:discovery'
        '</w:To>'
        '<w:Action e:mustUnderstand="true">'
        'http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe'
        '</w:Action>'
        '</e:Header>'
        '<e:Body>'
        '<d:Probe>'
        f'<d:Types>{_PROBE_TYPES}</d:Types>'
        '<d:Scopes/>'
        '</d:Probe>'
        '</e:Body>'
        '</e:Envelope>'
    )
    return envelope.encode("utf-8")


def _localname(tag: str) -> str:
    """Strip XML namespace from a tag: ``{ns}local`` -> ``local``."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _find_child(elem, localname: str):
    """Find the first child element by local name (any namespace)."""
    for child in elem:
        if _localname(child.tag) == localname:
            return child
    return None


def _parse_probe_match(data: bytes, sender_ip: str) -> _ProbeMatch | None:
    """Parse a WS-Discovery ProbeMatch reply. Returns ``None`` for
    non-ProbeMatch traffic on the multicast port."""
    try:
        text = data.decode("utf-8", errors="replace")
        root = _safe_fromstring(text)
    except (_XMLParseError, ValueError):
        return None

    if _localname(root.tag) != "Envelope":
        return None

    body = _find_child(root, "Body")
    if body is None:
        return None

    matches_root = _find_child(body, "ProbeMatches")
    if matches_root is None:
        # Some firmware emits a single ProbeMatch directly under Body.
        match_elem = _find_child(body, "ProbeMatch")
        if match_elem is None:
            return None
    else:
        match_elem = _find_child(matches_root, "ProbeMatch")
        if match_elem is None:
            return None

    result = _ProbeMatch(ip=sender_ip)

    types_elem = _find_child(match_elem, "Types")
    if types_elem is not None and types_elem.text:
        result.types = types_elem.text.split()

    scopes_elem = _find_child(match_elem, "Scopes")
    if scopes_elem is not None and scopes_elem.text:
        result.scopes = scopes_elem.text.split()

    xaddrs_elem = _find_child(match_elem, "XAddrs")
    if xaddrs_elem is not None and xaddrs_elem.text:
        result.xaddrs = xaddrs_elem.text.split()

    epr_elem = _find_child(match_elem, "EndpointReference")
    if epr_elem is not None:
        addr_elem = _find_child(epr_elem, "Address")
        if addr_elem is not None and addr_elem.text:
            result.endpoint_reference = addr_elem.text.strip()

    if not result.scopes and not result.xaddrs:
        return None
    return result


def _make_socket(source_ip: str):
    """Create a UDP socket pinned to the control interface for multicast."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((source_ip or "", 0))

    if source_ip:
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton(source_ip),
        )

    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_TTL,
        struct.pack("b", MULTICAST_TTL),
    )

    sock.setblocking(False)
    return sock


def _build_response(match: _ProbeMatch) -> dict[str, Any]:
    """Map a parsed ProbeMatch into the evidence response dict.

    ``manufacturer`` is reserved — the engine's vendor-string
    enrichment looks for it in the txt/response dict and feeds the
    matcher's manufacturer_alias path.
    """
    response: dict[str, Any] = {
        "ip": match.ip,
        "category": "camera",
        "protocols": ["onvif"],
    }
    if match.scopes:
        response["scopes"] = list(match.scopes)
    if match.xaddrs:
        response["xaddrs"] = list(match.xaddrs)
    if match.endpoint_reference:
        response["serial_number"] = match.endpoint_reference

    manufacturer = match.scope_value("manufacturer")
    if manufacturer:
        response["manufacturer"] = manufacturer

    hardware = match.scope_value("hardware")
    if hardware:
        response["model"] = hardware

    name = match.scope_value("name")
    if name:
        response["device_name"] = name

    return response


def _build_txt(match: _ProbeMatch) -> dict[str, str]:
    """Pack manufacturer + hardware into the evidence txt dict.

    The matcher disambiguates per-vendor peer drivers via a
    ``manufacturer`` filter on the broadcast SignalRule, so vendor-
    specific drivers can claim ONVIF with a TXT match while this
    cross-vendor anchor catches everything else.
    """
    txt: dict[str, str] = {}
    manufacturer = match.scope_value("manufacturer")
    if manufacturer:
        txt["manufacturer"] = manufacturer
    hardware = match.scope_value("hardware")
    if hardware:
        txt["hardware"] = hardware
    return txt


async def probe(ctx: ProbeContext) -> None:
    """Send the WS-Discovery Probe + collect ProbeMatch responses."""
    try:
        sock = _make_socket(ctx.source_ip)
    except OSError as exc:
        ctx.log.warning(
            "onvif_camera companion: could not create socket: %s", exc,
        )
        return

    loop = asyncio.get_event_loop()
    addr = (WSD_GROUP, WSD_PORT)

    try:
        envelope = _build_probe_envelope()
        try:
            await loop.run_in_executor(
                None, lambda: sock.sendto(envelope, addr),
            )
        except OSError as exc:
            ctx.log.debug(
                "onvif_camera companion: probe send failed: %s", exc,
            )
            return

        end = loop.time() + LISTEN_DURATION
        seen: set[str] = set()
        matches = 0

        while loop.time() < end:
            remaining = end - loop.time()
            if remaining <= 0:
                break
            try:
                sock.settimeout(min(remaining, 0.5))
                data, sender = await loop.run_in_executor(
                    None, lambda: sock.recvfrom(8192),
                )
            except (socket.timeout, TimeoutError):
                continue
            except OSError as exc:
                ctx.log.debug(
                    "onvif_camera companion: recv error: %s", exc,
                )
                break

            sender_ip = sender[0]
            if sender_ip in seen:
                continue

            match = _parse_probe_match(data, sender_ip)
            if match is None:
                continue
            seen.add(sender_ip)
            matches += 1

            await ctx.emit_broadcast(
                host=sender_ip,
                response=_build_response(match),
                txt=_build_txt(match) or None,
                port=WSD_PORT,
                matched_pattern="onvif:ProbeMatches",
            )

        if matches:
            ctx.log.info(
                "onvif_camera companion: identified %d ONVIF device(s)", matches,
            )
    finally:
        try:
            sock.close()
        except OSError:
            pass
