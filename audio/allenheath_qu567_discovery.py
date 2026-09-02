"""Discovery companion for the Allen & Heath Qu-5/6/7 driver.

Why this is Python rather than a declarative ``tcp_probe:``
-----------------------------------------------------------
Identifying a Qu-5/6/7 needs a *negative* signal, and a declarative probe can
only express a positive one.

Every Allen & Heath console on TCP 51325 that speaks the current NRPN protocol
answers a Get for the LR master mute. That includes the SQ family, which shares
the protocol deliberately (the Qu-5/6/7 document says so). So a probe matching
only "answered the LR mute Get" identifies an SQ as a Qu with full confidence,
which is worse than the hint-only "possible" it replaced: a wrong answer stated
firmly costs more than no answer.

There is no address a Qu-5/6/7 has that an SQ does not -- the Qu's parameter
map is a subset of the SQ's. The discriminator only runs the other way, so it
has to be read as an absence:

  * ``00 44`` (LR master mute) -- every console of this protocol generation
    answers. Proves we are talking to one at all.
  * ``00 27`` (Ip40 mute) -- an SQ has 48 inputs and answers. A Qu-5/6/7 has
    32 mono inputs plus ST1/ST2/USB and is **silent** here.

Answered the first and silent on the second => Qu-5/6/7. Answered both => an
SQ, and this companion emits nothing so the SQ's own hints decide.

Both facts were measured on a real Qu-5 (2026-09-01) by sweeping the whole
address space and matching every reply by its echoed parameter number.

Notes
-----
The console accepts only one MIDI client at a time. If something already holds
the slot -- a running OpenAVC device, Qu-Pad, another control system -- this
probe's connection is accepted and then dropped, it reads nothing, and the
console simply does not identify on that scan. That is the right outcome: the
probe never disturbs the client that got there first (the console keeps the
first connection and drops the second, which was measured), and a console
already under control does not need discovering.

License: MIT (matches the OpenAVC drivers repo).
"""

from __future__ import annotations

import asyncio
from typing import Any

from openavc.discovery.companion import ProbeContext

QU_PORT = 51325

# NRPN Get: BN 63 <MSB> BN 62 <LSB> BN 60 7F, on MIDI channel 1. The console's
# MIDI channel is configurable, but channel 1 is the default and a scan is
# allowed to miss a console that has been moved off it -- the alternative is
# sixteen connections per host.
_CH = 0xB0


def _get(msb: int, lsb: int) -> bytes:
    return bytes([_CH, 0x63, msb, _CH, 0x62, lsb, _CH, 0x60, 0x7F])


# Present on every console of this protocol generation.
COMMON_ADDR = (0x00, 0x44)      # LR master mute
# Present on an SQ (Ip40), absent on a Qu-5/6/7.
SQ_ONLY_ADDR = (0x00, 0x27)

CONNECT_TIMEOUT = 1.5
READ_TIMEOUT = 1.0
# Consoles are single-client and embedded; a handful at a time is plenty and
# keeps a big site's scan from opening dozens of sockets at once.
MAX_CONCURRENT_PROBES = 8


def _reply_for(data: bytes, msb: int, lsb: int) -> bytes | None:
    """The 12-byte NRPN absolute answering (msb, lsb), if it is in ``data``.

    Matched by the echoed parameter number rather than by position: a Get
    answers with its own address echoed back, so this cannot mistake a reply
    to one question for the answer to another.
    """
    i = 0
    while i + 12 <= len(data):
        if (data[i] == _CH and data[i + 1] == 0x63 and data[i + 2] == msb
                and data[i + 4] == 0x62 and data[i + 5] == lsb
                and data[i + 7] == 0x06 and data[i + 10] == 0x26):
            return data[i:i + 12]
        i += 1
    return None


async def _read_for(reader: asyncio.StreamReader, seconds: float) -> bytes:
    """Collect whatever arrives within ``seconds``. Silence returns b""."""
    buf = bytearray()
    loop = asyncio.get_event_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        try:
            chunk = await asyncio.wait_for(reader.read(512),
                                           timeout=max(0.05, end - loop.time()))
        except (asyncio.TimeoutError, ConnectionError, OSError):
            break
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) >= 12:
            break
    return bytes(buf)


async def _probe_one(ip: str, source_ip: str, log) -> dict[str, Any] | None:
    """Return match details for a Qu-5/6/7, or None for anything else."""
    local_addr = (source_ip, 0) if source_ip else None
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, QU_PORT, local_addr=local_addr),
            timeout=CONNECT_TIMEOUT,
        )

        writer.write(_get(*COMMON_ADDR))
        await writer.drain()
        common = _reply_for(await _read_for(reader, READ_TIMEOUT), *COMMON_ADDR)
        if common is None:
            # Not this protocol generation at all — an original Qu, an
            # Avantis/dLive, or something else entirely on the port.
            return None

        writer.write(_get(*SQ_ONLY_ADDR))
        await writer.drain()
        sq_only = _reply_for(await _read_for(reader, READ_TIMEOUT), *SQ_ONLY_ADDR)
        if sq_only is not None:
            log.debug("%s answered the SQ-only address — not a Qu-5/6/7", ip)
            return None

        return {
            "protocol": "midi-over-tcp",
            "lr_mute_reply": common.hex(),
            "sq_only_address_silent": True,
        }
    except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
        log.debug("Qu-5/6/7 probe of %s failed: %s", ip, exc)
        return None
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def probe(ctx: ProbeContext) -> None:
    """Identify Qu-5/6/7 consoles among the hosts already seen on 51325."""
    hosts = ctx.hosts_by_open_port.get(QU_PORT, ())
    if not hosts:
        return
    ctx.log.debug("Qu-5/6/7 companion: %d host(s) open on %d",
                  len(hosts), QU_PORT)

    sem = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    async def one(ip: str) -> None:
        async with sem:
            found = await _probe_one(ip, ctx.source_ip, ctx.log)
        if found:
            await ctx.emit_active(
                ip,
                response=found,
                port=QU_PORT,
                matched_pattern="nrpn:LR-mute answered, Ip40 silent",
            )

    await asyncio.gather(*(one(ip) for ip in hosts), return_exceptions=True)
