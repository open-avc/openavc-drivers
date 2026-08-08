"""Discovery companion for the PJLink Class 1 driver.

PJLink is a JBMIA cross-vendor projector control standard. Class 1
adds basic control (TCP 4352); Class 2 adds the SRCH broadcast
discovery (UDP 4352). Most modern PJLink projectors implement both.

This companion implements both halves of PJLink discovery:

  1. Class 2 SRCH (UDP 4352) — broadcast a single ``%2SRCH\\r`` packet
     to each scan subnet's directed broadcast address; collect
     ``%2ACKN=<MAC>\\r`` replies.
  2. Class 1 INFO query (TCP 4352) — for every ACKN responder, open a
     TCP connection, read the PJLINK greeting, then send the five
     read-only INFO queries (CLSS / INF1 / INF2 / NAME / LAMP) and
     parse the responses.

Evidence is emitted under the canonical synthetic IDs:

  ``custom_pjlink_class1_companion_udp`` (broadcast)  — Class 2 SRCH ack
  ``custom_pjlink_class1_companion_tcp`` (active)     — Class 1 INF reply

The pjlink_class1 driver declares ``discovery.python`` with
``cross_vendor: true``, so the matcher's best-driver-first logic demotes
pjlink_class1 to an alternative when a vendor-specific projector driver
(``sharp_nec_projector``, ``epson_escvp``, etc.) matches the device via
``manufacturer_alias`` lifted from the INF1 manufacturer string, or via OUI.

Spec references
---------------
PJLink Class 2: https://pjlink.jbmia.or.jp/english/data_cl2/PJLink_5-1.pdf

License: MIT (matches the OpenAVC drivers repo).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any

# Resolved at runtime via the openavc package; the companion runs in
# the platform's Python environment.
from openavc.discovery.companion import ProbeContext


PJLINK_PORT = 4352
PJLINK_SRCH_REQUEST = b"%2SRCH\r"

# Spec response: %2ACKN=<12 hex chars>\r — case-insensitive, some
# firmware emits trailing whitespace.
_PJLINK_ACKN_RE = re.compile(rb"%2ACKN=([0-9a-fA-F]{12})\b")

# Class 1 INFO queries we send per ACKN responder. Order matters for
# parse_pjlink_info_responses — keep CLSS / INF1 / INF2 / NAME / LAMP.
_PJLINK_INFO_QUERIES: tuple[bytes, ...] = (
    b"%1CLSS ?\r",
    b"%1INF1 ?\r",
    b"%1INF2 ?\r",
    b"%1NAME ?\r",
    b"%1LAMP ?\r",
)


@dataclass
class PJLinkClass2Reply:
    """A Class 2 SRCH ACKN response."""

    ip: str
    mac: str  # 12 hex chars, lowercase, no separators


@dataclass
class PJLinkInfoResult:
    """Parsed INFO-query responses from a PJLink projector."""

    ip: str
    pjlink_class: str | None = None
    manufacturer: str | None = None
    product_name: str | None = None
    device_name: str | None = None
    lamp_hours: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def parse_pjlink_ackn(data: bytes, sender_ip: str) -> PJLinkClass2Reply | None:
    """Parse a ``%2ACKN=<MAC>\\r`` Class 2 SRCH reply.

    Returns ``None`` for malformed input — random UDP traffic on the
    listening port must not produce a false positive.
    """
    match = _PJLINK_ACKN_RE.search(data)
    if not match:
        return None
    mac_hex = match.group(1).decode("ascii", errors="replace").lower()
    if len(mac_hex) != 12:
        return None
    return PJLinkClass2Reply(ip=sender_ip, mac=mac_hex)


def format_mac(mac_hex: str) -> str:
    """Return ``001122aabbcc`` formatted as ``00:11:22:aa:bb:cc``."""
    if len(mac_hex) != 12:
        return mac_hex
    return ":".join(mac_hex[i:i + 2] for i in range(0, 12, 2))


def _parse_pjlink_response(data: bytes | None, prefix: str) -> str | None:
    """Pull the value out of a ``%1<CMD>=<value>\\r`` response."""
    if not data:
        return None
    text = data.decode("utf-8", errors="replace").strip()
    if text.startswith(prefix + "="):
        val = text[len(prefix) + 1:].strip()
        if val and val != "ERR" and not val.startswith("ERR"):
            return val
    return None


def parse_pjlink_info_responses(
    ip: str, responses: list[bytes | None],
) -> PJLinkInfoResult | None:
    """Parse the response list from the canonical 5-query exchange.

    ``responses[0]`` is the PJLINK greeting; entries 1..5 are CLSS,
    INF1, INF2, NAME, LAMP in order. Returns ``None`` if the greeting
    isn't present or doesn't start with ``PJLINK`` (random TCP traffic
    on 4352).
    """
    if not responses:
        return None
    greeting_raw = responses[0]
    if not greeting_raw:
        return None
    greeting = greeting_raw.decode("utf-8", errors="replace").strip()
    if not greeting.startswith("PJLINK"):
        return None

    result = PJLinkInfoResult(ip=ip)

    if len(responses) > 1:
        cls = _parse_pjlink_response(responses[1], "%1CLSS")
        if cls:
            result.pjlink_class = cls
            result.extra["pjlink_class"] = cls

    if len(responses) > 2:
        mfg = _parse_pjlink_response(responses[2], "%1INF1")
        if mfg:
            result.manufacturer = mfg

    if len(responses) > 3:
        product = _parse_pjlink_response(responses[3], "%1INF2")
        if product:
            result.product_name = product

    if len(responses) > 4:
        name = _parse_pjlink_response(responses[4], "%1NAME")
        if name:
            result.device_name = name

    if len(responses) > 5:
        lamp_raw = _parse_pjlink_response(responses[5], "%1LAMP")
        if lamp_raw:
            parts = lamp_raw.split()
            if parts and parts[0].isdigit():
                result.lamp_hours = int(parts[0])
                result.extra["lamp_hours"] = result.lamp_hours

    return result


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------


def _broadcast_addresses_for(subnets: tuple[str, ...] | list[str]) -> list[str]:
    """Return the directed broadcast address for each CIDR. Skips /31 / /32."""
    out: list[str] = []
    for cidr in subnets:
        try:
            net = ipaddress.IPv4Network(cidr, strict=False)
        except ValueError:
            continue
        if net.prefixlen >= 31:
            continue
        out.append(str(net.broadcast_address))
    return out


def _make_broadcast_socket(source_ip: str) -> socket.socket | None:
    """UDP socket with broadcast on, bound to ``source_ip``."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((source_ip or "", 0))
        sock.setblocking(False)
        return sock
    except OSError:
        return None


async def _srch_class2(
    source_ip: str,
    subnets: tuple[str, ...],
    duration: float,
    log: logging.Logger,
) -> dict[str, PJLinkClass2Reply]:
    """Broadcast %2SRCH and collect ACKN replies. Keyed by responder IP."""
    targets = _broadcast_addresses_for(subnets)
    if not targets:
        return {}

    sock = _make_broadcast_socket(source_ip)
    if sock is None:
        log.warning("pjlink_class1 companion: could not bind broadcast socket")
        return {}

    results: dict[str, PJLinkClass2Reply] = {}
    loop = asyncio.get_event_loop()

    try:
        for bcast in targets:
            try:
                await loop.run_in_executor(
                    None,
                    lambda b=bcast: sock.sendto(
                        PJLINK_SRCH_REQUEST, (b, PJLINK_PORT),
                    ),
                )
                log.debug("pjlink SRCH -> %s:%d", bcast, PJLINK_PORT)
            except OSError as exc:
                log.debug("pjlink SRCH send to %s failed: %s", bcast, exc)

        end = loop.time() + duration
        while loop.time() < end:
            remaining = end - loop.time()
            if remaining <= 0:
                break
            try:
                sock.settimeout(min(remaining, 0.5))
                data, addr = await loop.run_in_executor(
                    None, lambda: sock.recvfrom(2048),
                )
            except (TimeoutError, socket.timeout):
                continue
            except OSError as exc:
                log.debug("pjlink SRCH recv error: %s", exc)
                break
            reply = parse_pjlink_ackn(data, addr[0])
            if reply:
                # First ACKN per IP wins; some firmware re-emits.
                results.setdefault(reply.ip, reply)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    return results


async def _query_class1_info(
    ip: str,
    source_ip: str,
    timeout: float,
    log: logging.Logger,
) -> PJLinkInfoResult | None:
    """Open TCP/4352, read greeting, send 5 INFO queries, parse responses."""
    local_addr = (source_ip, 0) if source_ip else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, PJLINK_PORT, local_addr=local_addr),
            timeout=timeout,
        )
    except (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError, OSError) as exc:
        log.debug("pjlink TCP connect to %s failed: %s", ip, exc)
        return None

    responses: list[bytes | None] = []
    try:
        # Greeting first.
        try:
            data = await asyncio.wait_for(reader.read(1024), timeout=timeout)
            responses.append(data)
        except (TimeoutError, asyncio.TimeoutError):
            responses.append(None)

        # Then the 5 INFO queries, one at a time so each response stays
        # on its own read.
        for cmd in _PJLINK_INFO_QUERIES:
            try:
                writer.write(cmd)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                responses.append(None)
                continue
            await asyncio.sleep(0.15)
            try:
                data = await asyncio.wait_for(reader.read(1024), timeout=timeout)
                responses.append(data)
            except (TimeoutError, asyncio.TimeoutError):
                responses.append(None)
    except (ConnectionResetError, BrokenPipeError, OSError) as exc:
        log.debug("pjlink TCP read from %s failed: %s", ip, exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass

    return parse_pjlink_info_responses(ip, responses)


# ---------------------------------------------------------------------------
# Companion entrypoint
# ---------------------------------------------------------------------------


async def probe(ctx: ProbeContext) -> None:
    """Run PJLink Class 2 SRCH + per-responder Class 1 INFO query.

    Emits broadcast evidence for every ACKN responder, plus active
    evidence enriched with manufacturer / product / lamp_hours from the
    per-host TCP exchange. The reserved ``manufacturer`` key in the
    active evidence response feeds the manufacturer-alias hint path —
    vendor-specific drivers with matching ``manufacturer_alias`` win
    primary identification over the generic ``pjlink_class1`` driver.
    """
    # Class 2 listen window. The spec recommends 30s for full coverage
    # (responders use a randomized delay), but the engine's overall scan
    # budget governs the upper bound. Use the smaller of half the
    # context budget and 8s — keeps bursty SRCH polite without making
    # the scan crawl.
    listen_window = min(ctx.timeout_seconds * 0.5, 8.0)

    ackn_replies = await _srch_class2(
        source_ip=ctx.source_ip,
        subnets=ctx.target_subnets,
        duration=listen_window,
        log=ctx.log,
    )
    if not ackn_replies:
        ctx.log.debug("pjlink_class1 companion: no Class 2 responders")
        return

    ctx.log.info(
        "pjlink_class1 companion: %d Class 2 responder(s)", len(ackn_replies),
    )

    # Emit broadcast evidence for each ACKN responder. The matcher binds
    # this to the canonical companion synthetic ID auto-registered by
    # the driver's ``discovery.python`` declaration.
    for ip, reply in ackn_replies.items():
        await ctx.emit_broadcast(
            host=ip,
            response={"mac": reply.mac, "ip": ip, "protocols": ["pjlink"]},
            txt={"mac": format_mac(reply.mac)},
            port=PJLINK_PORT,
            matched_pattern="ascii:%2ACKN=",
        )

    # Per-responder TCP/4352 INFO query. Run in parallel with a tcp
    # timeout below the remaining budget; the runner caps the whole
    # companion at ctx.timeout_seconds anyway.
    tcp_timeout = max(1.0, min(3.0, ctx.timeout_seconds * 0.25))

    async def query_one(ip: str) -> tuple[str, PJLinkInfoResult | None]:
        info = await _query_class1_info(
            ip=ip,
            source_ip=ctx.source_ip,
            timeout=tcp_timeout,
            log=ctx.log,
        )
        return ip, info

    tasks = [query_one(ip) for ip in ackn_replies]
    for result in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(result, BaseException):
            ctx.log.debug("pjlink TCP info gather error: %r", result)
            continue
        ip, info = result
        if info is None:
            continue

        response: dict[str, Any] = {"ip": ip, "category": "projector"}
        if info.manufacturer:
            response["manufacturer"] = info.manufacturer  # reserved key
        if info.product_name:
            response["model"] = info.product_name
        if info.device_name:
            response["device_name"] = info.device_name
        if info.extra:
            response["extra"] = dict(info.extra)
        await ctx.emit_active(host=ip, response=response, port=PJLINK_PORT)
