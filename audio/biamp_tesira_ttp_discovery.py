"""Discovery companion for the Biamp Tesira TTP driver.

Tesira DSPs greet a TCP/23 connection with the distinctive banner
``Welcome to the Tesira Text Protocol Server`` — preceded by a handful
of Telnet IAC negotiation bytes (RFC 854/855) the driver itself filters
during connection setup. A safe read-only ``DEVICE get serialNumber``
query on the established session returns ``+OK "value:<serial>"``, so
the companion can pull the unit serial alongside the banner-based
identification.

The companion runs once per scan and consumes the engine's existing
port-scan map (``ctx.hosts_by_open_port``) — only hosts the engine
already saw answering on TCP/23 are queried. No subnet sweep, no
duplication of the engine's port scan.

License: MIT (matches the OpenAVC drivers repo).
"""

from __future__ import annotations

import asyncio
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

# Per-host budget. Connect timeouts are short because the engine has
# already confirmed these hosts answer on TCP/23 — failures here are
# rare. Reads get a slightly longer window so devices have time to
# push the banner after IAC negotiation.
CONNECT_TIMEOUT = 1.0
READ_TIMEOUT = 1.5

# Cap simultaneous queries — embedded DSPs handle a handful of
# concurrent telnet sessions but can melt under hundreds.
MAX_CONCURRENT_PROBES = 16


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
    """Query every host the engine saw answering on TCP/23 and emit evidence."""
    candidates = ctx.hosts_by_open_port.get(TESIRA_PORT, ())
    if not candidates:
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    async def bounded(ip: str) -> tuple[str, dict[str, Any] | None]:
        async with sem:
            return await _query_one(ip, ctx.source_ip, ctx.log)

    results = await asyncio.gather(*(bounded(ip) for ip in candidates))

    matches = 0
    for ip, response in results:
        if response is None:
            continue
        matches += 1
        await ctx.emit_active(host=ip, response=response, port=TESIRA_PORT)

    if matches:
        ctx.log.info(
            "biamp_tesira_ttp companion: identified %d Tesira device(s)",
            matches,
        )
