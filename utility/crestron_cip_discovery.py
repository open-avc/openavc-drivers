"""Discovery companion for the Crestron CIP discovery anchor driver.

Crestron 3-Series control systems, DIN-AP processors, TSW touch
panels, MC4 / RMC4 control boxes, and DM NVX endpoints answer a
1-byte UDP probe on port 41794 with a fixed-format reply containing
hostname, model, and firmware. The wire format is well-documented
(Tenable PoC + Phenomite AMP-Research) and trivial to send/parse but
doesn't fit the declarative ``udp_probe`` block — the parser is
binary at fixed offsets, not ``response_match``-style.

This companion runs in two phases:

  1. UDP broadcast: send one ``\\x14`` byte to every subnet's directed
     broadcast address; parse ``\\x15``-magic replies for hostname,
     model, and firmware. Emits broadcast evidence under
     ``custom_crestron_cip_companion_udp``.
  2. TCP fallback (port 1688): modern Crestron firmware ignores the
     broadcast probe. The companion consumes the engine's port-scan
     map (``ctx.hosts_by_open_port``) and, for any host the engine
     already saw answering on TCP/1688 that the UDP phase missed,
     attempts a connect-only probe; data on connect confirms it as
     Crestron. Emits active evidence under
     ``custom_crestron_cip_companion_tcp``.

Both phases lift ``manufacturer = "Crestron"`` into their evidence
``response`` dict — the reserved key feeds vendor-string narrowing,
so vendor-specific Crestron drivers (crestron_nvx today, future
3-Series / TSW drivers) win the primary identification when they
declare matching ``manufacturer_alias`` values.

Spec references
---------------
Tenable PoC (BSD-style permissive): tenable/poc Crestron DGE-100
discover_and_hostname_change.py — describes the 0x14 / 0x15 protocol.
The TCP/1688 connect probe is the Crestron CIP control port; any
listener answer there is sufficient to identify Crestron gear.

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

from server.discovery.companion import ProbeContext


CRESTRON_CIP_PORT = 41794
CRESTRON_CIP_PROBE = b"\x14"

# TCP fallback port — modern firmware ignores the UDP broadcast.
CRESTRON_CIP_TCP_PORT = 1688

# Per-host TCP connect / read budgets. Failed hosts RST quickly so the
# main cost is the read window for ones that connect.
TCP_CONNECT_TIMEOUT = 1.0
TCP_READ_TIMEOUT = 1.5

# Cap concurrent TCP connects so the fallback sweep doesn't burst the
# network with hundreds of simultaneous SYNs.
MAX_CONCURRENT_TCP = 32

# Response constants (verified against Tenable PoC + Phenomite
# AMP-Research). Hostname is a fixed-width field at offset 10.
_CRESTRON_RESP_MAGIC = 0x15
_CRESTRON_HOSTNAME_OFFSET = 10
_CRESTRON_HOSTNAME_LEN = 16


@dataclass
class CrestronCIPReply:
    """Parsed CIP discovery reply."""

    ip: str
    hostname: str = ""
    model: str = ""
    firmware: str = ""
    raw_payload: bytes = b""
    fields: list[str] = field(default_factory=list)


def parse_crestron_cip(data: bytes, sender_ip: str) -> CrestronCIPReply | None:
    """Parse a Crestron CIP discovery reply. Returns None on bad data.

    Random UDP traffic on the listening port must not produce a false
    positive — the magic-byte check + fixed-offset hostname extraction
    + printable-ASCII filter on the tail fields keeps the parser
    deterministic.
    """
    if not data or data[0] != _CRESTRON_RESP_MAGIC:
        return None

    reply = CrestronCIPReply(ip=sender_ip, raw_payload=data)

    if len(data) >= _CRESTRON_HOSTNAME_OFFSET + _CRESTRON_HOSTNAME_LEN:
        hostname_bytes = data[
            _CRESTRON_HOSTNAME_OFFSET:
            _CRESTRON_HOSTNAME_OFFSET + _CRESTRON_HOSTNAME_LEN
        ]
        hostname = hostname_bytes.split(b"\x00", 1)[0].decode(
            "utf-8", errors="replace",
        ).strip()
        if hostname:
            reply.hostname = hostname

    # Tail: NUL-separated ASCII fields. Extract printable runs of >= 3
    # chars; first plausible model token + first plausible firmware
    # version land in their respective slots.
    tail = data[_CRESTRON_HOSTNAME_OFFSET + _CRESTRON_HOSTNAME_LEN:]
    fields = [
        f.decode("ascii", errors="replace").strip()
        for f in tail.split(b"\x00")
        if 3 <= len(f) <= 64 and all(0x20 <= b < 0x7f for b in f)
    ]
    reply.fields = fields

    for f in fields:
        if re.match(r"^[A-Z][A-Z0-9-]{2,}$", f):
            reply.model = f
            break

    for f in fields:
        if re.match(r"^[vV]?\d+\.\d+", f):
            reply.firmware = f.lstrip("v").lstrip("V")
            break

    return reply


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------


def _live_targets(subnets: tuple[str, ...] | list[str]) -> list[str]:
    """Return the directed broadcast address per CIDR.

    Modern Crestron firmware ignores broadcast probes — the platform
    feeds per-host targets via a wider engine pass when needed. For
    the companion we still send to the directed broadcast as a
    best-effort sweep; the responders that don't honor broadcast
    won't fire here, but the port_open / OUI hints on the anchor
    driver still surface them as `possible`.
    """
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
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((source_ip or "", 0))
        sock.setblocking(False)
        return sock
    except OSError:
        return None


async def _tcp_connect_probe(
    ip: str,
    source_ip: str,
    log: logging.Logger,
) -> bool:
    """Connect to TCP/1688 and return True if the device emits any data.

    Crestron CIP servers answer a connect with a banner / negotiation
    blob; non-Crestron hosts on 1688 (rare) typically don't. The
    original built-in probe used the same connect-only test.
    """
    local_addr = (source_ip, 0) if source_ip else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                ip, CRESTRON_CIP_TCP_PORT, local_addr=local_addr,
            ),
            timeout=TCP_CONNECT_TIMEOUT,
        )
    except (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return False
    try:
        try:
            data = await asyncio.wait_for(
                reader.read(1024), timeout=TCP_READ_TIMEOUT,
            )
            return bool(data)
        except (TimeoutError, asyncio.TimeoutError):
            return False
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass


# ---------------------------------------------------------------------------
# Companion entrypoint
# ---------------------------------------------------------------------------


async def probe(ctx: ProbeContext) -> None:
    """UDP broadcast 0x14 probe + TCP/1688 fallback sweep.

    The UDP phase emits broadcast evidence for every host that
    answers the probe; the TCP phase runs a connect-only probe on
    port 1688 against every subnet host that didn't already respond,
    and emits active evidence for any listener that emits data on
    connect.

    Both phases use the canonical synthetic IDs auto-registered for
    the crestron_cip anchor driver.
    """
    targets = _live_targets(ctx.target_subnets)
    seen: set[str] = set()
    udp_budget = min(ctx.timeout_seconds * 0.4, 4.0)

    # ── Phase 1: UDP broadcast ─────────────────────────────────────
    if targets:
        sock = _make_broadcast_socket(ctx.source_ip)
        if sock is None:
            ctx.log.warning(
                "crestron_cip companion: could not bind broadcast socket",
            )
        else:
            loop = asyncio.get_event_loop()
            try:
                for target in targets:
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda t=target: sock.sendto(
                                CRESTRON_CIP_PROBE, (t, CRESTRON_CIP_PORT),
                            ),
                        )
                        ctx.log.debug(
                            "crestron CIP probe -> %s:%d",
                            target, CRESTRON_CIP_PORT,
                        )
                    except OSError as exc:
                        ctx.log.debug(
                            "crestron CIP send to %s failed: %s",
                            target, exc,
                        )
                    await asyncio.sleep(0.02)

                end = loop.time() + udp_budget
                while loop.time() < end:
                    remaining = end - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        sock.settimeout(min(remaining, 0.5))
                        data, addr = await loop.run_in_executor(
                            None, lambda: sock.recvfrom(4096),
                        )
                    except (TimeoutError, socket.timeout):
                        continue
                    except OSError as exc:
                        ctx.log.debug("crestron CIP recv error: %s", exc)
                        break

                    sender_ip = addr[0]
                    if sender_ip in seen:
                        continue
                    reply = parse_crestron_cip(data, sender_ip)
                    if reply is None:
                        continue
                    seen.add(sender_ip)

                    response: dict[str, Any] = {
                        "ip": sender_ip,
                        "manufacturer": "Crestron",  # reserved
                        "category": "control",
                        "protocols": ["crestron_cip"],
                    }
                    if reply.hostname:
                        response["hostname"] = reply.hostname
                        response["device_name"] = reply.hostname
                    if reply.model:
                        response["model"] = reply.model
                    if reply.firmware:
                        response["firmware"] = reply.firmware

                    await ctx.emit_broadcast(
                        host=sender_ip,
                        response=response,
                        port=CRESTRON_CIP_PORT,
                        matched_pattern="crestron:device-info-reply",
                    )
                    ctx.log.info(
                        "crestron CIP reply from %s: hostname=%s model=%s",
                        sender_ip, reply.hostname, reply.model,
                    )
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

    # ── Phase 2: TCP/1688 fallback for hosts the UDP phase missed ──
    # Engine already port-scanned the subnet — only check hosts it saw
    # answering on 1688 that the UDP phase didn't already identify.
    tcp_candidates = ctx.hosts_by_open_port.get(CRESTRON_CIP_TCP_PORT, ())
    candidates = [ip for ip in tcp_candidates if ip not in seen]
    if not candidates:
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT_TCP)

    async def _try_tcp(ip: str) -> None:
        async with sem:
            ok = await _tcp_connect_probe(ip, ctx.source_ip, ctx.log)
        if not ok:
            return
        await ctx.emit_active(
            host=ip,
            response={
                "ip": ip,
                "manufacturer": "Crestron",  # reserved
                "category": "control",
                "protocols": ["crestron_cip"],
            },
            port=CRESTRON_CIP_TCP_PORT,
        )
        ctx.log.info("crestron CIP TCP/1688 listener at %s", ip)

    await asyncio.gather(
        *(_try_tcp(ip) for ip in candidates), return_exceptions=True,
    )
