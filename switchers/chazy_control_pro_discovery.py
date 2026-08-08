"""Discovery companion for the TurtleAV Chazy Control Pro controller.

The controller's telnet API (TCP 23) pushes a welcome banner on connect,
preceded by Telnet IAC option negotiation:

    <IAC negotiation bytes>
    ================================================================
    Welcome To TAV-CHAZY-CLTPRO Terminal Control System
    FW Version: 1.10.11
    Type "HELP" For More Information
    ================================================================
    CONTROLLER>

The driver declares a declarative ``tcp_probe`` that matches this banner: the
discovery probe runner accumulates TCP segments, so the IAC-first framing no
longer hides the model line, and an uninstalled Pro identifies straight from
the catalog. This companion is kept as a backup path that runs once the driver
is installed: it connects, accumulates a few short reads until the banner lands
(it does not need to answer the IAC negotiation; the controller sends the
banner regardless), reads the model token out of the welcome line, and emits an
active-probe fingerprint with ``manufacturer = "TurtleAV"``. That positively
identifies the chazy_control_pro driver and feeds the manufacturer-alias
narrowing path.

Pro vs. standard Control: the Pro banner reads ``TAV-CHAZY-CLTPRO``; the
standard Control reads ``CHAZY CONTROL``. This companion emits only for the
Pro model token (and chazy_control_discovery.py only for the standard one),
so the two drivers never both claim the same controller.

Why no mDNS hint: the controller advertises a single mDNS service,
``_netaudio-cmc._udp.local.`` — the generic Audinate Dante Conmon control
service every Dante device emits. Its TXT carries Dante fields only (no
vendor string), so it is not a Chazy fingerprint and is left for a future
generic Dante driver. The default hostname ``controller.local`` is declared
as a soft hint instead.

Validated against live hardware (FW 1.10.11) at controller.local / 192.168.4.188:23.

License: MIT (matches the OpenAVC drivers repo).
"""

from __future__ import annotations

import asyncio
import logging
import re

from openavc.discovery.companion import ProbeContext

# Telnet control API port. Module-level so tests can point the probe at a
# loopback server on an unprivileged port.
CHAZY_TELNET_PORT = 23

# Welcome-line model token that identifies *this* driver. The standard
# Control reports "CHAZY CONTROL"; matching the exact token keeps the two
# Chazy drivers from both claiming one controller.
PRO_MODEL_TOKEN = "TAV-CHAZY-CLTPRO"

# Manufacturer string lifted into the evidence response (reserved key). It
# normalizes to "turtleav", matching the driver's manufacturer_alias.
MANUFACTURER = "TurtleAV"

# Per-host connect / read budgets. A non-listener RSTs fast; the real cost
# is the read window for hosts that connect but aren't Chazy.
CONNECT_TIMEOUT = 1.5
READ_TIMEOUT = 1.5
# Cap total time accumulating banner reads per host so a chatty-but-silent
# telnet server can't stall the sweep.
BANNER_BUDGET = 3.0
# Cap concurrent connects so the sweep doesn't burst the network with SYNs.
MAX_CONCURRENT_TCP = 32

_BANNER_SENTINEL = "Terminal Control System"
_WELCOME_RE = re.compile(r"Welcome To\s+(.+?)\s+Terminal Control System")
_FW_RE = re.compile(r"FW Version:\s*([0-9][0-9A-Za-z.\-]*)")


def parse_welcome(text: str) -> tuple[str | None, str | None]:
    """Pull (model, firmware) out of a Chazy welcome banner.

    ``text`` is the connect banner decoded as latin-1 (Telnet IAC bytes, if
    present, are harmless noise the regex skips). Returns ``(None, None)``
    when the welcome line is absent — i.e. the host isn't a Chazy
    controller.
    """
    m = _WELCOME_RE.search(text)
    if m is None:
        return None, None
    model = m.group(1).strip()
    fw_match = _FW_RE.search(text)
    firmware = fw_match.group(1).strip() if fw_match else None
    return (model or None), firmware


def is_pro_token(model: str | None) -> bool:
    """True when a welcome-line model token identifies a Chazy Control *Pro*.

    Accepts the verified ``TAV-CHAZY-CLTPRO`` token and tolerates a firmware
    rebrand (any token bearing ``CLTPRO``, or both ``CHAZY`` and ``PRO``).
    Rejects the standard Control (``CHAZY CONTROL``) and the Darwin
    ``Controller(h)`` / ``DARWIN CONTROL`` tokens, so the three TAV companions
    stay mutually exclusive even across firmware brand changes.
    """
    if not model:
        return False
    low = model.lower()
    return "cltpro" in low or ("chazy" in low and "pro" in low)


async def _grab_banner(ip: str, source_ip: str, log: logging.Logger) -> str:
    """Connect to ``ip:CHAZY_TELNET_PORT`` and read the connect banner.

    The controller emits IAC negotiation in one segment and the banner in
    a later one, so accumulate short reads until the banner sentinel
    arrives or the budget elapses. Returns the latin-1-decoded stream
    (possibly empty on connect failure / silence).
    """
    local_addr = (source_ip, 0) if source_ip else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, CHAZY_TELNET_PORT, local_addr=local_addr),
            timeout=CONNECT_TIMEOUT,
        )
    except (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError, OSError) as exc:
        log.debug("chazy_control_pro companion: connect to %s failed: %s", ip, exc)
        return ""

    acc = b""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + BANNER_BUDGET
    try:
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(4096),
                    timeout=min(READ_TIMEOUT, max(0.1, deadline - loop.time())),
                )
            except (TimeoutError, asyncio.TimeoutError):
                break
            if not chunk:
                break  # peer closed
            acc += chunk
            # Don't stop at the sentinel alone: the FW Version line follows the
            # welcome line, so a banner fragmented across segments could exit
            # before it arrives and drop the firmware. Wait for both (or budget).
            if _BANNER_SENTINEL.encode("latin-1") in acc and _FW_RE.search(
                acc.decode("latin-1", errors="replace")
            ):
                break
    except (ConnectionResetError, BrokenPipeError, OSError) as exc:
        log.debug("chazy_control_pro companion: read from %s failed: %s", ip, exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass

    return acc.decode("latin-1", errors="replace")


async def probe(ctx: ProbeContext) -> None:
    """Banner-grab every host the engine saw listening on TCP 23.

    For each candidate, read the connect banner and emit an active-probe
    fingerprint only when the welcome line reports the Pro model token.
    Non-Chazy telnet hosts (and standard Control units) parse to a
    different / absent model and are skipped, so this companion never
    misidentifies them.
    """
    candidates = ctx.hosts_by_open_port.get(CHAZY_TELNET_PORT, ())
    if not candidates:
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT_TCP)

    async def _try(ip: str) -> None:
        async with sem:
            banner = await _grab_banner(ip, ctx.source_ip, ctx.log)
        if not banner:
            return
        model, firmware = parse_welcome(banner)
        if not is_pro_token(model):
            return
        response: dict[str, object] = {
            "ip": ip,
            "manufacturer": MANUFACTURER,  # reserved -> manufacturer_alias path
            "model": model,
            "category": "switcher",
            "protocols": ["chazy_telnet"],
        }
        if firmware:
            response["firmware"] = firmware
        await ctx.emit_active(
            host=ip,
            response=response,
            port=CHAZY_TELNET_PORT,
            matched_pattern=f"regex:Welcome To {model}",
        )
        ctx.log.info(
            "chazy_control_pro companion: %s identified as %s (FW %s)",
            ip, model, firmware or "?",
        )

    await asyncio.gather(*(_try(ip) for ip in candidates), return_exceptions=True)
