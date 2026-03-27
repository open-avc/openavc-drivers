"""
OpenAVC Wake-on-LAN Driver.

Sends WoL magic packets to wake devices on the network. This driver
uses the UDP transport for broadcast. It doesn't maintain a persistent
connection — it sends a magic packet on demand.

WoL magic packet format:
    6 bytes of 0xFF followed by the target MAC address repeated 16 times.
"""

from __future__ import annotations

import re
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.udp import UDPTransport
from server.utils.logger import get_logger

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
        ValueError: If the MAC address is invalid.
    """
    # Strip separators and validate
    mac_clean = re.sub(r"[:\-.]", "", mac_address).upper()
    if len(mac_clean) != 12 or not all(c in "0123456789ABCDEF" for c in mac_clean):
        raise ValueError(f"Invalid MAC address: {mac_address}")

    mac_bytes = bytes.fromhex(mac_clean)
    return b"\xFF" * 6 + mac_bytes * 16


class WakeOnLANDriver(BaseDriver):
    """Wake-on-LAN magic packet sender."""

    DRIVER_INFO = {
        "id": "wake_on_lan",
        "name": "Wake-on-LAN",
        "manufacturer": "Generic",
        "category": "utility",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Send Wake-on-LAN magic packets to wake devices on the network. "
            "No persistent connection required."
        ),
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

    async def connect(self) -> None:
        """WoL doesn't need a persistent connection — just mark as ready."""
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Wake-on-LAN driver ready")

    async def disconnect(self) -> None:
        """Mark as disconnected."""
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Wake-on-LAN driver stopped")

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a WoL magic packet."""
        if command != "wake":
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            return

        mac = self.config.get("mac_address", "")
        if not mac:
            log.error(f"[{self.device_id}] No MAC address configured")
            return

        broadcast = self.config.get("broadcast_address", "255.255.255.255")
        port = self.config.get("port", WOL_PORT)

        try:
            packet = build_magic_packet(mac)
        except ValueError as e:
            log.error(f"[{self.device_id}] {e}")
            return

        # Create a temporary UDP transport, send, and close
        udp = UDPTransport(name=self.device_id)
        try:
            await udp.open(allow_broadcast=True)
            await udp.send(packet, broadcast, port)
            log.info(f"[{self.device_id}] Sent WoL magic packet to {mac}")

            import time
            self.set_state("last_wake", time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            log.error(f"[{self.device_id}] Failed to send WoL packet: {e}")
        finally:
            udp.close()

        return True
