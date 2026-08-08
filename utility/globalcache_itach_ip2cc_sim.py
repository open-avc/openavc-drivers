"""Global Cache iTach IP2CC — Simulator.

Implements the Unified TCP command API (port 4998) server side for the IP2CC's
three contact-closure relays:

  - ``getversion``           -> the firmware string (bare, CR-terminated).
  - ``getdevices``           -> the ETHERNET + "1,3 RELAY" module list.
  - ``getstate,1:<n>``       -> ``state,1:<n>,<0|1>`` for the relay's state.
  - ``setstate,1:<n>,<0|1>`` -> the command echoed back (matches real hardware,
    which echoes ``setstate,...`` rather than the spec's ``state,...``).
  - anything else            -> ``ERR_0:0,001`` (unknown-command error).

Relay states live in ``self.state`` (relay_1..relay_3) so the sim UI's toggle
controls flip them, letting a panel be tested against relay feedback without
hardware. All relays start open, mirroring the real unit's power-up state.
"""

from __future__ import annotations

import logging

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

RELAY_COUNT = 3
# Byte-exact getdevices reply for an IP2CC (ETHERNET module 0 + relay module 1).
GETDEVICES_REPLY = b"device,0,0 ETHERNET\rdevice,1,3 RELAY\rendlistdevices\r"


class GlobalCacheItachIP2CCSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "globalcache_itach_ip2cc",
        "name": "Global Cache iTach IP2CC Simulator",
        "category": "utility",
        "transport": "tcp",
        "default_port": 4998,
        # Command lines are carriage-return terminated; the framework strips the
        # terminator before calling handle_command().
        "delimiter": "\r",
        "initial_state": {
            "firmware": "710-1008-05",
            "relay_1": False,
            "relay_2": False,
            "relay_3": False,
        },
        "controls": [
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
            {"type": "toggle", "key": "relay_1", "label": "Relay 1"},
            {"type": "toggle", "key": "relay_2", "label": "Relay 2"},
            {"type": "toggle", "key": "relay_3", "label": "Relay 3"},
        ],
        "delays": {
            "command_response": 0.005,
        },
    }

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("ascii", errors="replace").strip()
        if not line:
            return None

        if line == "getversion":
            return f"{self.state.get('firmware', '710-1008-05')}\r".encode()

        if line == "getdevices":
            return GETDEVICES_REPLY

        parts = line.split(",")
        cmd = parts[0]

        if cmd == "getstate" and len(parts) == 2:
            relay = self._relay_from_connector(parts[1])
            if relay is None:
                return b"ERR_0:0,001\r"
            value = 1 if self.state.get(f"relay_{relay}") else 0
            return f"state,1:{relay},{value}\r".encode()

        if cmd == "setstate" and len(parts) == 3:
            relay = self._relay_from_connector(parts[1])
            if relay is None or parts[2] not in ("0", "1"):
                return b"ERR_0:0,001\r"
            self.set_state(f"relay_{relay}", parts[2] == "1")
            # Real hardware echoes the command verbatim.
            return f"setstate,1:{relay},{parts[2]}\r".encode()

        return b"ERR_0:0,001\r"

    @staticmethod
    def _relay_from_connector(connector: str) -> int | None:
        """Map a ``module:connector`` token (``1:2``) to a relay number 1..3."""
        _, _, conn = connector.partition(":")
        if not conn.isdigit():
            return None
        relay = int(conn)
        return relay if 1 <= relay <= RELAY_COUNT else None
