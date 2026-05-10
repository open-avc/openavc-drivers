"""Discovery companion for the generic VISCA-over-IP camera driver.

VISCA cameras (Sony SRG/BRC, AVer, Lumens, Marshall, Panasonic AW-HE
when configured for VISCA mode, and any other camera that adopts
Sony's wire format) accept the standard ``CAM_VersionInq`` packet
``81 09 00 02 FF`` on TCP port 10500 and reply with
``90 50 VV VV MM MM ... FF``, where ``VV VV`` is a 16-bit vendor code
and ``MM MM`` is the model code.

The vendor code uniquely identifies the manufacturer (Sony 0x0020,
Panasonic 0x0001 are documented; others may follow). The matcher then
narrows to a vendor-specific driver (sony_visca, panasonic_awhe, etc.)
via the ``manufacturer_alias`` hint when the companion's evidence
carries the reserved ``manufacturer`` key.

The companion is declared on the generic ``visca_ip`` driver with
``cross_vendor: true`` so vendor-specific peers win primary
identification when their ``manufacturer_alias`` matches. It consumes
the engine's port-scan map (``ctx.hosts_by_open_port``) — only hosts
already seen answering on TCP/10500 are queried.

License: MIT (matches the OpenAVC drivers repo).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from server.discovery.companion import ProbeContext


VISCA_PORT = 10500
VISCA_VERSION_INQ = b"\x81\x09\x00\x02\xFF"

# Known VISCA vendor codes. Codes outside the table still emit
# evidence (the probe success + port_open hint anchor enough), but
# without a manufacturer_alias narrowing path.
_VENDOR_CODES: dict[int, str] = {
    0x0001: "Panasonic",
    0x0020: "Sony",
}

CONNECT_TIMEOUT = 1.0
READ_TIMEOUT = 1.5
MAX_CONCURRENT_PROBES = 16


@dataclass
class ViscaReply:
    ip: str
    vendor_code: int
    model_code: int
    manufacturer: str | None


def parse_visca_response(data: bytes, ip: str) -> ViscaReply | None:
    """Parse a VISCA ``CAM_VersionInq`` reply. Returns ``None`` on bad data.

    The header bytes ``90 50`` and the 7-byte minimum length are the
    only two checks the original built-in probe applied; we keep them
    so noisy hosts answering on TCP/10500 don't masquerade as cameras.
    """
    if not data or len(data) < 7:
        return None
    if data[0] != 0x90 or data[1] != 0x50:
        return None
    vendor_code = (data[2] << 8) | data[3]
    model_code = (data[4] << 8) | data[5]
    return ViscaReply(
        ip=ip,
        vendor_code=vendor_code,
        model_code=model_code,
        manufacturer=_VENDOR_CODES.get(vendor_code),
    )


async def _query_one(
    ip: str, source_ip: str, log,
) -> tuple[str, dict[str, Any] | None]:
    local_addr = (source_ip, 0) if source_ip else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, VISCA_PORT, local_addr=local_addr),
            timeout=CONNECT_TIMEOUT,
        )
    except (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return ip, None

    try:
        try:
            writer.write(VISCA_VERSION_INQ)
            await asyncio.wait_for(writer.drain(), timeout=READ_TIMEOUT)
        except (ConnectionResetError, BrokenPipeError, OSError):
            return ip, None
        try:
            data = await asyncio.wait_for(reader.read(64), timeout=READ_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            return ip, None

        reply = parse_visca_response(data, ip)
        if reply is None:
            return ip, None

        result: dict[str, Any] = {
            "ip": ip,
            "category": "camera",
            "protocols": ["visca"],
            "vendor_code": f"0x{reply.vendor_code:04X}",
            "model_code": f"0x{reply.model_code:04X}",
        }
        if reply.manufacturer:
            # Reserved key — feeds vendor_string narrowing.
            result["manufacturer"] = reply.manufacturer
        return ip, result
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass


async def probe(ctx: ProbeContext) -> None:
    """Query every host the engine saw answering on TCP/10500."""
    candidates = ctx.hosts_by_open_port.get(VISCA_PORT, ())
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
        await ctx.emit_active(host=ip, response=response, port=VISCA_PORT)

    if matches:
        ctx.log.info(
            "visca_ip companion: identified %d VISCA camera(s)", matches,
        )
