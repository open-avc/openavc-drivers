"""SNMP v2c device — Simulator.

An SNMP agent for an invented eight-outlet rack widget, so the generic
driver's two declaration styles both have something real to point at:

  scalars    an enumerated switch (1=On, 2=Off), a scaled gauge reading, a
             writable string, and a counter parked past 2^31 — the range that
             reads back negative if anything on the path decodes it signed.
  a table    the outlet table at .2.1, with a label column, a writable state
             column and a power column, eight rows deep. That is what the
             driver's Tables field is for, and eight rows is enough to prove
             the walk finds them all and that a write lands on the right one.

The MIB-II system group comes from the base class's own defaults being
overridden here with this device's identity — the driver reads that group
without being told to, so a device identifies itself before the integrator
declares anything.

A generic agent has no fixed named controls (what each OID means lives in the
driver's OID map, not here), so the Simulator UI shows the handful of values
worth watching while a test drives the driver: the outlet states, the beacon,
and the load. Each can be changed there, as a person at the device would.

Traps. The widget sends a v2c trap to the driver's Trap Port whenever one of
those values changes, from a driver's SET or from the Simulator UI alike: an
outlet trap carries the outlet's state and power, a beacon trap the beacon,
and a load trap nothing at all, the way many real traps name an event and
leave the value to be read. All three go out from one place, ``set_state``,
which both paths reach, so a change is trapped once. A platform whose
simulator has no ``send_trap`` (before OpenAVC 0.40.0) sends none, and its
driver polls.

Driver side: ``utility/snmp_v2c.py``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from openavc.simulator.snmp_simulator import SNMPSimulator

logger = logging.getLogger(__name__)

# An invented enterprise arc — 99999 is not an assigned IANA number, so
# nothing here can collide with a real product's MIB.
WIDGET = "1.3.6.1.4.1.99999"

BEACON = f"{WIDGET}.1.1.0"          # enumerated: 1 = on, 2 = off
LOAD_TENTHS = f"{WIDGET}.1.2.0"     # gauge32, tenths of an amp
SITE_LABEL = f"{WIDGET}.1.3.0"      # writable string
PACKETS = f"{WIDGET}.1.4.0"         # counter32, deliberately past 2^31

OUTLET_LABEL = f"{WIDGET}.2.1.2"
OUTLET_STATE = f"{WIDGET}.2.1.3"    # 1 = on, 2 = off
OUTLET_WATTS = f"{WIDGET}.2.1.4"

# The widget's notifications (snmpTrapOID values).
TRAP_OUTLET = f"{WIDGET}.0.1"       # carries the outlet's state and watts
TRAP_BEACON = f"{WIDGET}.0.2"       # carries the beacon
TRAP_LOAD = f"{WIDGET}.0.3"         # carries nothing: read the load

SYS_UPTIME = "1.3.6.1.2.1.1.3.0"

OUTLET_COUNT = 8
DEFAULT_TRAP_PORT = 162

# Which OIDs the Simulator UI shows. Everything else lives in the MIB only.
MIRRORED: dict[str, str] = {
    BEACON: "beacon",
    LOAD_TENTHS: "load_tenths",
    **{f"{OUTLET_STATE}.{i}": f"outlet_{i}_state" for i in range(1, OUTLET_COUNT + 1)},
}
OID_OF: dict[str, str] = {key: oid for oid, key in MIRRORED.items()}

ON_OFF = {"options": [1, 2], "labels": {"1": "On", "2": "Off"}}


def _outlet_watts(index: int, on: bool) -> int:
    return 40 + index * 3 if on else 0


class SNMPv2cSimulator(SNMPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "snmp_v2c",
        "name": "SNMP Device Simulator",
        "category": "utility",
        "transport": "snmp",
        "default_port": 161,
        "initial_state": {
            "beacon": 1,
            "load_tenths": 34,
            **{f"outlet_{i}_state": (1 if i <= 6 else 2)
               for i in range(1, OUTLET_COUNT + 1)},
        },
        "controls": [
            {"type": "select", "key": "beacon", "label": "Beacon", **ON_OFF},
            {"type": "slider", "key": "load_tenths", "label": "Load (0.1 A)",
             "min": 0, "max": 200, "step": 1},
            *[
                {"type": "select", "key": f"outlet_{i}_state",
                 "label": f"Outlet {i}", **ON_OFF}
                for i in range(1, OUTLET_COUNT + 1)
            ],
        ],
        "delays": {"command_response": 0.002},
    }

    READ_COMMUNITY = "public"
    WRITE_COMMUNITY = "private"

    OIDS = {
        # MIB-II system group. Every agent has one, and the driver reads it
        # without being asked.
        "1.3.6.1.2.1.1.1.0": ("string", "Acme rack widget, 8 outlets, FW 2.4"),
        "1.3.6.1.2.1.1.2.0": ("oid", f"{WIDGET}.1"),
        SYS_UPTIME: ("timeticks", 0),
        "1.3.6.1.2.1.1.4.0": ("string", "facilities@example.invalid", True),
        "1.3.6.1.2.1.1.5.0": ("string", "rack-widget-1", True),
        "1.3.6.1.2.1.1.6.0": ("string", "Rack 4, Room 210", True),

        # Scalars.
        BEACON: ("integer", 1, True),
        LOAD_TENTHS: ("gauge32", 34),
        SITE_LABEL: ("string", "north wing", True),
        PACKETS: ("counter32", 3_100_000_000),

        # The outlet table: label, state, watts, eight rows.
        **{f"{OUTLET_LABEL}.{i}": ("string", f"outlet {i}")
           for i in range(1, OUTLET_COUNT + 1)},
        **{f"{OUTLET_STATE}.{i}": ("integer", 1 if i <= 6 else 2, True)
           for i in range(1, OUTLET_COUNT + 1)},
        **{f"{OUTLET_WATTS}.{i}": ("gauge32", _outlet_watts(i, i <= 6))
           for i in range(1, OUTLET_COUNT + 1)},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._booted = time.monotonic()

    # ── Computed values ──

    def read_oid(self, oid: str):
        """sysUpTime is a clock, not a stored value.

        A stored uptime that never moves is the one thing about a simulated
        agent that reads as broken at a glance, and it is two lines to get
        right. TimeTicks are hundredths of a second.
        """
        if oid == SYS_UPTIME:
            return "timeticks", int((time.monotonic() - self._booted) * 100)
        return super().read_oid(oid)

    # ── Writes ──

    def write_oid(self, oid: str, type_name: str, value):
        """Apply a write, then mirror the interesting ones into simulator
        state so the Simulator UI shows what the driver just did (and the
        trap goes out from there)."""
        status = super().write_oid(oid, type_name, value)
        if status != 0:
            return status
        self._side_effects(oid, value)
        if oid in MIRRORED:
            self.set_state(MIRRORED[oid], value)
        return status

    def _side_effects(self, oid: str, value) -> None:
        """Switching an outlet off also drops its power reading to zero — the
        kind of side effect a real device has and a test should be able to
        see, because a driver that reports watts on a dead outlet is wrong in
        a way no round-trip assertion would catch."""
        if oid.startswith(OUTLET_STATE + "."):
            index = oid.rsplit(".", 1)[-1]
            watts_oid = f"{OUTLET_WATTS}.{index}"
            if watts_oid in self.oids:
                current = self.oids[watts_oid]
                self.oids[watts_oid] = (
                    current[0], _outlet_watts(int(index), int(value) == 1),
                )

    # ── The Simulator UI, and the one place a change is trapped ──

    def set_state(self, key: str, value: Any) -> None:
        oid = OID_OF.get(key)
        if oid is None or not hasattr(self, "oids"):
            super().set_state(key, value)
            return
        try:
            # A select or slider can hand over text; the MIB holds a number.
            value = int(value)
        except (TypeError, ValueError):
            logger.debug("%s: %r is not a number for %s", self.device_id, value, key)
            return
        if self.get_state(key) == value:
            return
        entry = self.oids[oid]
        if entry[1] != value:
            # Changed in the Simulator UI: the MIB follows, side effects too.
            self.oids[oid] = (entry[0], value, *entry[2:])
            self._side_effects(oid, value)
        super().set_state(key, value)
        self._trap(oid, value)

    def _trap(self, oid: str, value: int) -> None:
        send = getattr(self, "send_trap", None)
        if send is None:
            # The simulator on a release before OpenAVC 0.40.0 has no trap
            # sender, and its driver has no listener: it polls.
            return
        try:
            port = int(self.config.get("trap_port", DEFAULT_TRAP_PORT) or 0)
        except (TypeError, ValueError):
            port = 0
        if port <= 0:
            return
        if oid.startswith(OUTLET_STATE + "."):
            index = oid.rsplit(".", 1)[-1]
            watts = self.oids[f"{OUTLET_WATTS}.{index}"][1]
            send(TRAP_OUTLET, [(oid, "integer", value),
                               (f"{OUTLET_WATTS}.{index}", "gauge32", watts)], port=port)
        elif oid == BEACON:
            send(TRAP_BEACON, [(oid, "integer", value)], port=port)
        elif oid == LOAD_TENTHS:
            send(TRAP_LOAD, [], port=port)
