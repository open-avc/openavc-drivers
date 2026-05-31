"""Discovery companion for the TurtleAV Chazy Control (standard) controller.

The standard Control shares the Pro's telnet API and connect-banner shape;
only the model token in the welcome line differs:

    Welcome To CHAZY CONTROL Terminal Control System
    FW Version: 1.00.17

As with the Pro, the banner arrives after Telnet IAC negotiation in a
later TCP segment. The driver declares a ``tcp_probe`` that matches it: the
discovery probe runner accumulates TCP segments, so a single read no longer
stops at the IAC bytes and an uninstalled standard Control identifies from
the catalog. This companion stays as a backup path that runs once the driver
is installed: it connects, accumulates short reads until the banner lands,
reads the model token, and emits an active-probe fingerprint with
``manufacturer = "TurtleAV"`` only when the token is the standard Control's.
The Pro companion emits only for ``TAV-CHAZY-CLTPRO``, so the two drivers
never both claim one controller.

The standard Control's banner format is taken from the Chazy Control API
reference (FW 1.00.17) — the same documentary basis as the rest of the
chazy_control driver (no standalone Control hardware is on hand). The Pro's
banner shape, which this mirrors, is verified against live hardware.

Why no mDNS hint: see chazy_control_pro_discovery.py — the controller
advertises only the generic Audinate Dante ``_netaudio-cmc._udp.local.``
service (no vendor string), so the default hostname ``controller.local`` is
declared as a soft hint instead.

License: MIT (matches the OpenAVC drivers repo).
"""

from __future__ import annotations

import asyncio
import logging
import re

from server.discovery.companion import ProbeContext

# Telnet control API port. Module-level so tests can point the probe at a
# loopback server on an unprivileged port.
CHAZY_TELNET_PORT = 23

# Welcome-line model token that identifies the standard Control. Matching
# the exact token keeps it distinct from the Pro's "TAV-CHAZY-CLTPRO".
CONTROL_MODEL_TOKEN = "CHAZY CONTROL"

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


def parse_welcome(text: str) -> tuple[str | None, str | None]:
    """Pull (model, firmware) out of a Chazy welcome banner.

    ``text`` is the connect banner decoded as latin-1 (Telnet IAC bytes, if
    present, are harmless noise the regex skips). Returns ``(None, None)``
    when the welcome line is absent.
    """
    m = _WELCOME_RE.search(text)
    if m is None:
        return None, None
    model = m.group(1).strip()
    fw_match = _FW_RE.search(text)
    firmware = fw_match.group(1).strip() if fw_match else None
    return (model or None), firmware


def is_standard_token(model: str | None) -> bool:
    """True when a welcome-line model token identifies the *standard* Chazy
    Control (not the Pro, not Darwin).

    Anchored on ``CHAZY`` so it can never claim the Darwin ``Controller(h)``
    token (whose substring ``control`` would otherwise match), and excludes the
    Pro (``CLTPRO`` / ``CHAZY ... PRO``). Tolerant of a firmware rebrand within
    the standard line. Keeps the three TAV companions mutually exclusive.
    """
    if not model:
        return False
    low = model.lower()
    if "chazy" not in low:
        return False
    if "cltpro" in low or "pro" in low:
        return False
    return "control" in low


async def _grab_banner(ip: str, source_ip: str, log: logging.Logger) -> str:
    """Connect to ``ip:CHAZY_TELNET_PORT`` and read the connect banner.

    Accumulates short reads until the banner sentinel arrives or the budget
    elapses (the controller sends IAC and the banner in separate segments).
    Returns the latin-1-decoded stream (empty on connect failure / silence).
    """
    local_addr = (source_ip, 0) if source_ip else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, CHAZY_TELNET_PORT, local_addr=local_addr),
            timeout=CONNECT_TIMEOUT,
        )
    except (TimeoutError, asyncio.TimeoutError, ConnectionRefusedError, OSError) as exc:
        log.debug("chazy_control companion: connect to %s failed: %s", ip, exc)
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
        log.debug("chazy_control companion: read from %s failed: %s", ip, exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass

    return acc.decode("latin-1", errors="replace")


async def probe(ctx: ProbeContext) -> None:
    """Banner-grab every host the engine saw listening on TCP 23.

    Emits an active-probe fingerprint only when the welcome line reports
    the standard Control's model token; Pro units, other Chazy variants,
    and unrelated telnet hosts are skipped.
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
        if not is_standard_token(model):
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
            "chazy_control companion: %s identified as %s (FW %s)",
            ip, model, firmware or "?",
        )

    await asyncio.gather(*(_try(ip) for ip in candidates), return_exceptions=True)
