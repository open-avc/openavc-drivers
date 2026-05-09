"""Discovery companion for the Biamp Tesira TTP driver.

Tesira DSPs greet a TCP/23 connection with the distinctive banner
``Welcome to the Tesira Text Protocol Server`` — preceded by a handful
of Telnet IAC negotiation bytes (RFC 854/855) the driver itself filters
during connection setup. A safe read-only ``DEVICE get serialNumber``
query on the established session returns ``+OK "value:<serial>"``, so
the companion can pull the unit serial alongside the banner-based
identification.

The companion runs once per scan and iterates ``target_subnets`` to
try TCP/23 on each host. Hosts that don't answer drop quickly (RST or
timeout); matching ones get a banner read, query, and parse, with the
serial number lifted into the Tier 3 evidence response under the
reserved ``manufacturer`` key.

License: MIT (matches the OpenAVC drivers repo).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Any

from server.discovery.companion import ProbeContext


TESIRA_PORT = 23
TESIRA_QUERY = b"DEVICE get serialNumber\r\n"

# Identifies a Tesira DSP regardless of firmware variant. The other two
# substrings cover firmwares that emit the prompt-style banner instead
# of the welcome line.
_TESIRA_BANNER_RE = re.compile(
    r"Welcome to the Tesira|#Tesira|Tesira Text Protocol",
    re.IGNORECASE,
)
_TESIRA_RESPONSE_RE = re.compile(
    r'\+OK\s*"?value\s*:\s*(?P<serial>[A-Za-z0-9\-]+)"?',
    re.IGNORECASE,
)
_TESIRA_VERSION_RE = re.compile(r"version\s+(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE)

# Cap concurrent TCP connects so the companion doesn't burst the
# network with hundreds of simultaneous SYNs. 32 is comfortable for
# embedded AV gear; a /24 finishes in a few seconds at this setting.
MAX_CONCURRENT_PROBES = 32

# Per-host budget. Connect timeouts are short because failed hosts
# typically RST immediately; reads get a slightly longer window so
# devices have time to push the banner after IAC negotiation.
CONNECT_TIMEOUT = 1.0
READ_TIMEOUT = 1.5


def _expand_targets(subnets: tuple[str, ...] | list[str]) -> list[str]:
    """Return all unicast host IPs in the given subnets, deduped."""
    seen: set[str] = set()
    out: list[str] = []
    for cidr in subnets:
        try:
            net = ipaddress.IPv4Network(cidr, strict=False)
        except ValueError:
            continue
        if net.prefixlen >= 31:
            continue
        for host in net.hosts():
            ip = str(host)
            if ip not in seen:
                seen.add(ip)
                out.append(ip)
    return out


async def _query_one(
    ip: str, source_ip: str, log,
) -> tuple[str, dict[str, Any] | None]:
    """Connect to TCP/23, read banner, send the read-only query, parse."""
    local_addr = (source_ip, 0) if source_ip else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, TESIRA_PORT, local_addr=local_addr),
            timeout=CONNECT_TIMEOUT,
        )
    except (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return ip, None

    response: dict[str, Any] | None = None
    try:
        # Read the IAC negotiation + welcome banner.
        try:
            banner_raw = await asyncio.wait_for(
                reader.read(2048), timeout=READ_TIMEOUT,
            )
        except (TimeoutError, asyncio.TimeoutError):
            banner_raw = b""

        banner = banner_raw.decode("utf-8", errors="replace")
        if not _TESIRA_BANNER_RE.search(banner):
            return ip, None

        result: dict[str, Any] = {
            "ip": ip,
            "manufacturer": "Biamp",      # reserved — feeds vendor_string
            "category": "audio",
            "protocols": ["biamp_tesira"],
        }
        fw = _TESIRA_VERSION_RE.search(banner)
        if fw:
            result["firmware"] = fw.group(1)

        # Send the read-only serial query, read its reply.
        try:
            writer.write(TESIRA_QUERY)
            await asyncio.wait_for(writer.drain(), timeout=READ_TIMEOUT)
        except (ConnectionResetError, BrokenPipeError, OSError):
            return ip, result
        try:
            reply = await asyncio.wait_for(
                reader.read(1024), timeout=READ_TIMEOUT,
            )
        except (TimeoutError, asyncio.TimeoutError):
            reply = b""

        text = reply.decode("utf-8", errors="replace")
        m = _TESIRA_RESPONSE_RE.search(text)
        if m:
            result["serial_number"] = m.group("serial")

        response = result
    except (ConnectionResetError, BrokenPipeError, OSError) as exc:
        log.debug("tesira_ttp companion: %s read error: %s", ip, exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass

    return ip, response


async def probe(ctx: ProbeContext) -> None:
    """Sweep ``target_subnets`` for Tesira DSPs on TCP/23 and emit evidence."""
    targets = _expand_targets(ctx.target_subnets)
    if not targets:
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    async def bounded(ip: str) -> tuple[str, dict[str, Any] | None]:
        async with sem:
            return await _query_one(ip, ctx.source_ip, ctx.log)

    results = await asyncio.gather(*(bounded(ip) for ip in targets))

    matches = 0
    for ip, response in results:
        if response is None:
            continue
        matches += 1
        await ctx.emit_active(host=ip, response=response)

    if matches:
        ctx.log.info(
            "biamp_tesira_ttp companion: identified %d Tesira device(s)",
            matches,
        )
