"""Discovery companion for the TurtleAV Darwin Control controller.

The Darwin Control shares the Chazy Control's telnet API (TCP 23) and connect
framing — Telnet IAC negotiation in one TCP segment, then a welcome banner and
``CONTROLLER> `` prompt:

    <IAC negotiation bytes>
    ================================================================
    Welcome To Controller(h) Terminal Control System
    FW Version: 1.50.02
    Type "HELP" For More Information
    ================================================================
    CONTROLLER>

A declarative ``tcp_probe`` can't reach this banner (the IAC bytes arrive in
their own segment, so a single read returns only ~12 IAC bytes). This companion
connects, accumulates short reads until the banner lands (it does not answer
the IAC negotiation — the controller sends the banner regardless), reads the
model token from the welcome line, and emits an active-probe fingerprint with
``manufacturer = "TurtleAV"`` when that token identifies a Darwin Control.

**The discriminator is the welcome token, and it is firmware-dependent.** The
verified unit (FW 1.50.02) welcomes as ``Controller(h)`` — *not* "DARWIN"; the
manual's FW 2.03.19 build brands the same headers ``DARWIN CONTROL``. So this
companion accepts both the exact ``Controller(h)`` token (verified) and any
``DARWIN``-bearing token (the 2.03.19 variant), and it never claims a token
containing ``CHAZY`` — the Chazy Control (``CHAZY CONTROL``) and Chazy Control
Pro (``TAV-CHAZY-CLTPRO``) controllers, which share the default hostname
``controller.local`` and telnet port, have their own companions. Those three
companions are mutually exclusive: each emits only for its own token.

> ``Controller(h)`` is generic. A live verification step (a Darwin and a Chazy
> on the same subnet, plan §4) must confirm no Chazy *standard* Control welcomes
> with the same token; the standard-Control companion gates on ``CHAZY CONTROL``,
> which is distinct, so on the evidence to date there is no collision.

The ``Controller(h)`` welcome token is verified against live hardware
(FW 1.50.02) at controller.local / 192.168.4.33:23. The ``DARWIN CONTROL``
variant is documented (FW 2.03.19) but not captured on the wire.

License: MIT (matches the OpenAVC drivers repo).
"""

from __future__ import annotations

import asyncio
import logging
import re

from server.discovery.companion import ProbeContext

# Telnet control API port. Module-level so tests can point the probe at a
# loopback server on an unprivileged port.
DARWIN_TELNET_PORT = 23

# Manufacturer string lifted into the evidence response (reserved key). It
# normalizes to "turtleav", matching the driver's manufacturer_alias.
MANUFACTURER = "TurtleAV"

# Per-host connect / read budgets (see chazy_control_pro_discovery.py).
CONNECT_TIMEOUT = 1.5
READ_TIMEOUT = 1.5
BANNER_BUDGET = 3.0
MAX_CONCURRENT_TCP = 32

_BANNER_SENTINEL = "Terminal Control System"
_WELCOME_RE = re.compile(r"Welcome To\s+(.+?)\s+Terminal Control System")
_FW_RE = re.compile(r"FW Version:\s*([0-9][0-9A-Za-z.\-]*)")

# Verified welcome token (FW 1.50.02). Matched case-insensitively so a future
# build that capitalises it differently still identifies.
_CONTROLLER_H_RE = re.compile(r"^controller\(h\)$", re.IGNORECASE)


def parse_welcome(text: str) -> tuple[str | None, str | None]:
    """Pull (model, firmware) out of a controller welcome banner.

    ``text`` is the connect banner decoded as latin-1 (Telnet IAC bytes, if
    present, are harmless noise the regex skips). Returns ``(None, None)`` when
    the welcome line is absent — i.e. the host isn't a TAV-family controller.
    """
    m = _WELCOME_RE.search(text)
    if m is None:
        return None, None
    model = m.group(1).strip()
    fw_match = _FW_RE.search(text)
    firmware = fw_match.group(1).strip() if fw_match else None
    return (model or None), firmware


def is_darwin_token(model: str | None) -> bool:
    """True when a welcome-line model token identifies a Darwin Control.

    Accepts the verified ``Controller(h)`` token and any ``DARWIN``-bearing
    token (the FW 2.03.19 ``DARWIN CONTROL`` brand). Explicitly rejects any
    ``CHAZY`` token so this companion never claims a Chazy Control or Control
    Pro, which share Darwin's default hostname and telnet port.
    """
    if not model:
        return False
    m = model.strip()
    low = m.lower()
    if "chazy" in low:
        return False
    if "darwin" in low:
        return True
    return bool(_CONTROLLER_H_RE.match(m))


async def _grab_banner(ip: str, source_ip: str, log: logging.Logger) -> str:
    """Connect to ``ip:DARWIN_TELNET_PORT`` and read the connect banner.

    The controller emits IAC negotiation in one segment and the banner in a
    later one, so accumulate short reads until the banner sentinel arrives or
    the budget elapses. Returns the latin-1-decoded stream (possibly empty on
    connect failure / silence).
    """
    local_addr = (source_ip, 0) if source_ip else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, DARWIN_TELNET_PORT, local_addr=local_addr),
            timeout=CONNECT_TIMEOUT,
        )
    except (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError, OSError) as exc:
        log.debug("darwin_control companion: connect to %s failed: %s", ip, exc)
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
            # Wait for the FW Version line (it follows the welcome line), not
            # just the sentinel, so a fragmented banner doesn't drop firmware.
            if _BANNER_SENTINEL.encode("latin-1") in acc and _FW_RE.search(
                acc.decode("latin-1", errors="replace")
            ):
                break
    except (ConnectionResetError, BrokenPipeError, OSError) as exc:
        log.debug("darwin_control companion: read from %s failed: %s", ip, exc)
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
    fingerprint only when the welcome line carries a Darwin Control token.
    Chazy controllers (and unrelated telnet hosts) parse to a different /
    absent token and are skipped, so this companion never misidentifies them.
    """
    candidates = ctx.hosts_by_open_port.get(DARWIN_TELNET_PORT, ())
    if not candidates:
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT_TCP)

    async def _try(ip: str) -> None:
        async with sem:
            banner = await _grab_banner(ip, ctx.source_ip, ctx.log)
        if not banner:
            return
        model, firmware = parse_welcome(banner)
        if not is_darwin_token(model):
            return
        response: dict[str, object] = {
            "ip": ip,
            "manufacturer": MANUFACTURER,  # reserved -> manufacturer_alias path
            "model": model,
            "category": "switcher",
            "protocols": ["darwin_telnet"],
        }
        if firmware:
            response["firmware"] = firmware
        await ctx.emit_active(
            host=ip,
            response=response,
            port=DARWIN_TELNET_PORT,
            matched_pattern=f"regex:Welcome To {model}",
        )
        ctx.log.info(
            "darwin_control companion: %s identified as %s (FW %s)",
            ip, model, firmware or "?",
        )

    await asyncio.gather(*(_try(ip) for ip in candidates), return_exceptions=True)
