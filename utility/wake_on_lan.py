"""
OpenAVC Wake-on-LAN Driver.

Sends WoL magic packets to wake devices on the network, through the
platform's send_udp. It doesn't maintain a persistent connection — it
sends a magic packet on demand.

WoL magic packet format:
    6 bytes of 0xFF followed by the target MAC address repeated 16 times.
"""

from __future__ import annotations

import re
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

try:
    from openavc.drivers.base import WakeAddressError
except ImportError:  # OpenAVC before 0.37.1 has no typed wake refusal
    WakeAddressError = ValueError

log = get_logger(__name__)

WOL_PORT = 9


def build_magic_packet(mac_address: str) -> bytes:
    """
    Build a Wake-on-LAN magic packet for the given MAC address.

    Args:
        mac_address: MAC address in any common format:
                     "AA:BB:CC:DD:EE:FF", "AA-BB-CC-DD-EE-FF",
                     or "AABBCCDDEEFF".

    Returns:
        102-byte magic packet (6 * 0xFF + 16 * MAC).

    Raises:
        WakeAddressError: If the MAC address is invalid (a ValueError).
    """
    # Strip separators and validate
    mac_clean = re.sub(r"[:\-.]", "", mac_address).upper()
    if len(mac_clean) != 12 or not all(c in "0123456789ABCDEF" for c in mac_clean):
        raise WakeAddressError(
            f"'{mac_address}' is not a MAC address (six pairs of hex digits, "
            f"like AA:BB:CC:DD:EE:FF). Correct it under Edit Device."
        )

    mac_bytes = bytes.fromhex(mac_clean)
    return b"\xFF" * 6 + mac_bytes * 16


class WakeOnLANDriver(BaseDriver):
    """Wake-on-LAN magic packet sender."""

    DRIVER_INFO = {
        "id": "wake_on_lan",
        "name": "Wake-on-LAN",
        "manufacturer": "Generic",
        "category": "utility",
        "version": "1.3.7",
        # send_udp sends a 255.255.255.255 broadcast through the adapter
        # Reach devices through chose, or through every connected adapter,
        # from 0.37.0.
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": (
            "Send Wake-on-LAN magic packets to wake devices on the network. "
            "No persistent connection required."
        ),
        "source_url": "https://en.wikipedia.org/wiki/Wake-on-LAN",
        "tags": ["wol", "magic-packet", "power-on", "generic"],
        "verified": False,
        "transport": "udp",
        "help": {
            "overview": (
                "Sends Wake-on-LAN magic packets to power on devices remotely. "
                "Works with any Ethernet device that supports WoL (most PCs, "
                "NUCs, some displays, and media players)."
            ),
            "setup": (
                "1. Enable Wake-on-LAN in the target device's BIOS/UEFI settings\n"
                "2. Enable WoL in the OS network adapter settings\n"
                "3. Enter the target device's MAC address (e.g. AA:BB:CC:DD:EE:FF)\n"
                "4. The broadcast address is usually 255.255.255.255 (default)"
            ),
        },
        # WoL is a one-shot send-only utility — there's no device
        # endpoint to discover. Always added manually, so no
        # ``discovery`` block.
        "default_config": {
            "mac_address": "",
            "broadcast_address": "255.255.255.255",
            "port": 9,
        },
        "config_schema": {
            "mac_address": {
                "type": "string",
                "required": True,
                "label": "MAC Address",
                "description": "Target device MAC (e.g., AA:BB:CC:DD:EE:FF)",
            },
            "broadcast_address": {
                "type": "string",
                "default": "255.255.255.255",
                "label": "Broadcast Address",
            },
            "port": {
                "type": "integer",
                "default": 9,
                "label": "WoL Port",
            },
        },
        "state_variables": {
            "last_wake": {"type": "string", "label": "Last Wake Sent"},
        },
        "commands": {
            "wake": {"label": "Send Wake Packet", "params": {}},
        },
    }

    async def _create_transport(self, transport_type: str) -> None:
        # Fire-and-forget protocol: each wake opens its own throwaway
        # socket (see send_command), so there is no persistent transport
        # to build. self.transport stays None.
        self.transport = None

    def _link_alive(self) -> bool:
        # No persistent link to test — a magic packet is one outbound
        # datagram and the target never answers, so the driver is ready
        # whenever it's active.
        return True

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a WoL magic packet."""
        if command != "wake":
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            return

        mac = self.config.get("mac_address", "")
        if not mac:
            raise WakeAddressError("No MAC address is set. Enter it under Edit Device.")

        broadcast = self.config.get("broadcast_address", "255.255.255.255")
        port = self.config.get("port", WOL_PORT)

        packet = build_magic_packet(mac)

        # The platform's side-send: a socket opened for it and closed after,
        # from the adapter Settings > Network > Reach devices through chose,
        # and a 255.255.255.255 broadcast through every connected adapter
        # when none is chosen. A wake that did not go out raises, so the
        # command fails with the reason instead of reading as sent: with the
        # chosen adapter not connected that is the platform's "Not sent"
        # sentence, which the panel and the Programmer show as it is.
        await self.send_udp(packet, host=broadcast, port=port)
        log.info(f"[{self.device_id}] Sent WoL magic packet to {mac}")

        import time
        self.set_state("last_wake", time.strftime("%Y-%m-%d %H:%M:%S"))
        return True
