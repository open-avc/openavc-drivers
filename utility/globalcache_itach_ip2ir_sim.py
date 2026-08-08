"""Global Cache iTach IP2IR — Simulator.

Implements the Unified TCP command API (port 4998) server side for the IP2IR's
three IR emitter ports, so an IR device and the learn flow can be exercised
without hardware:

  - ``getversion``            -> the firmware string (bare, CR-terminated).
  - ``getdevices``            -> the ETHERNET + "1,3 IR" module list.
  - ``get_IR,1:<n>``          -> ``IR,1:<n>,IR`` (connector mode).
  - ``set_IR,1:<n>,<mode>``   -> the mode echoed back.
  - ``sendir,<conn>,<id>,...`` -> ``completeir,<conn>,<id>`` on success,
                                  ``ERR_<conn>,010`` for a malformed pulse list.
  - ``get_IRL``               -> ``IR Learner Enabled`` plus one canned learned
                                  ``sendir`` capture, so the learn UI streams a
                                  code without a physical remote.
  - ``stop_IRL``              -> ``IR Learner Disabled``.
  - anything else             -> ``ERR_0:0,002`` (bad command / connector).
"""

from __future__ import annotations

import logging

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

IR_CONNECTOR_COUNT = 3
# Byte-exact getdevices reply for an IP2IR (ETHERNET module 0 + IR module 1).
GETDEVICES_REPLY = b"device,0,0 ETHERNET\rdevice,1,3 IR\rendlistdevices\r"
# A canned learned code the sim streams on get_IRL — a real captured NEC-style
# remote button, reported on the learner's internal connector 2:1 (the real unit
# returns the learner address, not the emit port).
CANNED_LEARNED = (
    b"sendir,2:1,1,37537,1,1,171,170,21,64,21,64,21,64,21,21,21,21,21,21,21,21,"
    b"21,21,21,64,21,64,21,64,21,21,21,21,21,21,21,21,21,21,21,64,21,64,21,21,"
    b"21,64,21,21,21,21,21,21,21,21,21,21,21,21,21,64,21,21,21,64,21,64,21,64,"
    b"21,64,21,1764\r"
)


class GlobalCacheItachIP2IRSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "globalcache_itach_ip2ir",
        "name": "Global Cache iTach IP2IR Simulator",
        "category": "utility",
        "transport": "tcp",
        "default_port": 4998,
        # Command lines are carriage-return terminated; the framework strips the
        # terminator before calling handle_command().
        "delimiter": "\r",
        "initial_state": {
            "firmware": "710-1005-05",
            "last_emit": "",
            "learner": False,
        },
        "controls": [
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
            {"type": "indicator", "key": "last_emit", "label": "Last IR Emit"},
            {"type": "indicator", "key": "learner", "label": "Learner Active"},
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
            return f"{self.state.get('firmware', '710-1005-05')}\r".encode()

        if line == "getdevices":
            return GETDEVICES_REPLY

        if line == "get_IRL":
            # Enable the learner and immediately stream one canned capture so the
            # learn UI works without a physical remote.
            self.set_state("learner", True)
            return b"IR Learner Enabled\r" + CANNED_LEARNED

        if line == "stop_IRL":
            self.set_state("learner", False)
            return b"IR Learner Disabled\r"

        parts = line.split(",")
        cmd = parts[0]

        if cmd == "get_IR" and len(parts) == 2:
            if self._connector_num(parts[1]) is None:
                return b"ERR_0:0,002\r"
            return f"IR,{parts[1]},IR\r".encode()

        if cmd == "set_IR" and len(parts) == 3:
            if self._connector_num(parts[1]) is None:
                return b"ERR_0:0,002\r"
            return f"IR,{parts[1]},{parts[2]}\r".encode()

        if cmd == "sendir" and len(parts) >= 7:
            connector = parts[1]
            ir_id = parts[2]
            if self._connector_num(connector) is None:
                return b"ERR_0:0,002\r"
            # The pulse list must be a non-empty, even count of positive ints.
            pulses = parts[6:]
            if not pulses or len(pulses) % 2 != 0 or not all(
                p.isdigit() and int(p) > 0 for p in pulses
            ):
                return f"ERR_{connector},010\r".encode()
            self.set_state("last_emit", f"{connector} id={ir_id}")
            return f"completeir,{connector},{ir_id}\r".encode()

        return b"ERR_0:0,002\r"

    @staticmethod
    def _connector_num(connector: str) -> int | None:
        """Map a ``module:connector`` token (``1:2``) to a connector 1..3."""
        _, _, conn = connector.partition(":")
        if not conn.isdigit():
            return None
        n = int(conn)
        return n if 1 <= n <= IR_CONNECTOR_COUNT else None
