"""APC Switched Rack PDU — Simulator.

An SNMP agent serving the PowerNet MIB's Rack PDU branch, in the three shapes
the driver has to tell apart:

  ``rpdu2`` + metered   a Switched Metered-by-Outlet Rack PDU 2G (the AP8959
                        shape): 24 outlets, 2 banks, 1 phase, per-outlet
                        current / power / energy, a temperature+humidity
                        sensor.
  ``rpdu2``             a Switched Rack PDU 2G with no per-outlet metering
                        (the AP8941 shape). The metered-outlet table is
                        absent entirely, and the MIB's "models that do not
                        support this feature" values answer -1 — which is the
                        case that puts "-1 kVA" on a panel if the driver
                        publishes it as a number.
  ``rpdu``              a first-generation Switched Rack PDU (the AP7921
                        shape) on the legacy branch, whose outlet-state and
                        load-state enums are numbered the OTHER WAY ROUND.

That last one is the point of simulating both. The driver's whole hazard is
that ``on`` is 2 in one branch and 1 in the other, so an agent that serves
only the modern numbering would let an inverted legacy path pass every test.
Set ``generation`` in the device config to pick; the default is the metered
2G unit.

The interesting behaviours are the ones a round-trip assertion alone would
miss, so they are modelled explicitly:

  - Writing the CONTROL column changes the STATUS column, and in rPDU2 those
    two use different numbers for the same fact (control immediateOn is 1,
    status on is 2). A driver that echoes what it wrote instead of reading
    the status column back looks correct here and is wrong on hardware.
  - Switching an outlet off drops its metered current, power and energy rate
    to zero. A driver reporting watts on a dead outlet is wrong in a way no
    state assertion would catch.
  - A reboot ends with the outlet ON regardless of where it started, because
    that is what a power cycle does.

Driver side: ``power/apc_rack_pdu.py``.
"""

from __future__ import annotations

import logging
import time

from openavc.simulator.snmp_simulator import SNMPSimulator

logger = logging.getLogger(__name__)

RPDU2 = "1.3.6.1.4.1.318.1.1.26"
RPDU = "1.3.6.1.4.1.318.1.1.12"

# MIB-II.
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_CONTACT = "1.3.6.1.2.1.1.4.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"

# apcmgmt.mcontrol.mcontrolRestartAgent — 318.2, not under products.
MCONTROL_RESTART_AGENT = "1.3.6.1.4.1.318.2.2.1.0"

# rPDU2 columns.
R2_IDENT = f"{RPDU2}.2.1"
R2_CFG = f"{RPDU2}.4.1.1"
R2_PROPS = f"{RPDU2}.4.2.1"
R2_STATUS = f"{RPDU2}.4.3.1"
R2_CONTROL = f"{RPDU2}.4.4.1"
R2_OUT_CFG = f"{RPDU2}.9.2.1.1"
R2_OUT_PROPS = f"{RPDU2}.9.2.2.1"
R2_OUT_STATUS = f"{RPDU2}.9.2.3.1"
R2_OUT_CONTROL = f"{RPDU2}.9.2.4.1"
R2_MET_PROPS = f"{RPDU2}.9.4.2.1"
R2_MET_STATUS = f"{RPDU2}.9.4.3.1"
R2_BANK_CFG = f"{RPDU2}.8.1.1"
R2_BANK_PROPS = f"{RPDU2}.8.2.1"
R2_BANK_STATUS = f"{RPDU2}.8.3.1"
R2_PH_CFG = f"{RPDU2}.6.1.1"
R2_PH_STATUS = f"{RPDU2}.6.3.1"
R2_SENSOR = f"{RPDU2}.10.2.2.1"
R2_GROUP = f"{RPDU2}.11"

# Legacy rPDU columns.
R1_IDENT = f"{RPDU}.1"
R1_LOAD_DEV = f"{RPDU}.2.1"
R1_LOAD_STATUS = f"{RPDU}.2.3.1.1"
R1_OUT_DEV = f"{RPDU}.3.1"
R1_OUT_CTL = f"{RPDU}.3.3.1.1"
R1_OUT_CFG = f"{RPDU}.3.4.1.1"
R1_OUT_STATUS = f"{RPDU}.3.5.1.1"
R1_PSU = f"{RPDU}.4.1"
R1_BANK = f"{RPDU}.5.2.1"

# rPDU2 control-column values (rPDU2OutletSwitchedControlCommand).
R2_CMD_ON, R2_CMD_OFF, R2_CMD_REBOOT = 1, 2, 3
R2_CMD_DELAYED_ON, R2_CMD_DELAYED_OFF = 5, 6
R2_CMD_DELAYED_REBOOT, R2_CMD_CANCEL = 7, 8
# rPDU2 status-column values (rPDU2OutletSwitchedStatusState). Note that on
# and off are the other way round from the control column above.
R2_STATE_OFF, R2_STATE_ON = 1, 2

# Legacy control column (rPDUOutletControlOutletCommand) — the delayed set
# sits one lower than rPDU2's because there is no outletUnknown value.
R1_CMD_ON, R1_CMD_OFF, R1_CMD_REBOOT = 1, 2, 3
R1_CMD_DELAYED_ON, R1_CMD_DELAYED_OFF = 4, 5
R1_CMD_DELAYED_REBOOT, R1_CMD_CANCEL = 6, 7
# Legacy status column (rPDUOutletStatusOutletState) — on is 1 here.
R1_STATE_ON, R1_STATE_OFF = 1, 2

# Device-wide control. rPDU2DeviceControlCommand vs rPDUOutletDevCommand.
R2_ALL_ON, R2_ALL_OFF, R2_ALL_REBOOT = 1, 3, 4
R1_ALL_ON, R1_ALL_OFF, R1_ALL_REBOOT = 2, 3, 4

OUTLET_COUNT = 24
BANK_COUNT = 2
LEGACY_OUTLET_COUNT = 8

# Which outlets start on, and roughly what each draws in tenths of an amp
# when it is. Deliberately uneven so a test that reads the wrong row notices.
_ON_AT_START = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}
_DRAW_TENTHS = {i: (2 + (i * 3) % 17) for i in range(1, OUTLET_COUNT + 1)}


def _watts(tenths_amp: int) -> int:
    """A plausible wattage for a current draw at 120 V, to one decimal amp."""
    return int(round(tenths_amp / 10 * 120))


class APCRackPDUSimulator(SNMPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "apc_rack_pdu",
        "name": "APC Switched Rack PDU Simulator",
        "category": "power",
        "transport": "snmp",
        "default_port": 161,
        "initial_state": {
            "generation": "rpdu2",
            "load_amps": 0.0,
            **{f"outlet_{i}": (i in _ON_AT_START)
               for i in range(1, OUTLET_COUNT + 1)},
        },
        "controls": [
            {"type": "indicator", "key": "generation", "label": "MIB Branch"},
            {"type": "indicator", "key": "load_amps", "label": "Load (A)"},
            *[
                {"type": "indicator", "key": f"outlet_{i}",
                 "label": f"Outlet {i}"}
                for i in range(1, OUTLET_COUNT + 1)
            ],
        ],
        "delays": {"command_response": 0.002},
    }

    READ_COMMUNITY = "public"
    WRITE_COMMUNITY = "private"

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = config or {}
        self.generation = str(cfg.get("generation", "rpdu2")).lower()
        if self.generation not in ("rpdu2", "rpdu"):
            self.generation = "rpdu2"
        # A Switched Metered-by-Outlet unit (AP8959) vs a plain Switched one
        # (AP8941). Meaningless on the legacy branch, which never metered
        # outlets.
        self.metered = bool(cfg.get("metered_outlets", True))
        if self.generation == "rpdu":
            self.metered = False
        self.outlet_count = (
            OUTLET_COUNT if self.generation == "rpdu2" else LEGACY_OUTLET_COUNT
        )
        self._booted = time.monotonic()
        self.oids = self._build_mib()
        self.set_state("generation", self.generation)
        self._publish_outlets()

    # ── The MIB ──────────────────────────────────────────────────────────

    def _build_mib(self) -> dict[str, tuple]:
        oids: dict[str, tuple] = {
            SYS_DESCR: ("string", self._sys_descr()),
            # The MasterSwitch / Rack PDU family arc. What matters to
            # discovery is the enterprise number in it, 318, which is APC's.
            SYS_OBJECT_ID: ("oid", "1.3.6.1.4.1.318.1.3.4.6"),
            SYS_UPTIME: ("timeticks", 0),
            SYS_CONTACT: ("string", "facilities@example.invalid", True),
            SYS_NAME: ("string", "rack-pdu-1", True),
            SYS_LOCATION: ("string", "Rack 4, Room 210", True),
            MCONTROL_RESTART_AGENT: ("integer", 2, True),
        }
        builder = (
            self._build_rpdu2 if self.generation == "rpdu2"
            else self._build_rpdu
        )
        oids.update(builder())
        return oids

    def _sys_descr(self) -> str:
        if self.generation == "rpdu":
            return ("APC Web/SNMP Management Card (MB:v3.9.2 PF:v3.9.2 "
                    "PN:apc_hw02_aos_392.bin AF1:v3.9.2 MN:AP7921 "
                    "HR:B2 SN: ZA0000000000 MD:01/01/2011)")
        return ("APC Web/SNMP Management Card (MB:v4.1.0 PF:v6.8.2 "
                "PN:apc_hw05_aos_682.bin AF1:v6.8.2 MN:AP8959 "
                "HR:05 SN: 5A0000000000 MD:01/01/2020)")

    def _build_rpdu2(self) -> dict[str, tuple]:
        model = "AP8959" if self.metered else "AP8941"
        # A Switched-only Rack PDU answers -1 for the readings it does not
        # take. The MIB says so on every one of them, and it is the value a
        # driver must drop rather than publish.
        absent = -1
        oids: dict[str, tuple] = {
            f"{R2_IDENT}.1.1": ("integer", 1),
            f"{R2_IDENT}.3.1": ("string", "Rack PDU", True),
            f"{R2_IDENT}.5.1": ("string", "05"),
            f"{R2_IDENT}.6.1": ("string", "v6.8.2"),
            f"{R2_IDENT}.8.1": ("string", model),
            f"{R2_IDENT}.9.1": ("string", "5A0000000000"),

            f"{R2_CFG}.1.1": ("integer", 1),
            f"{R2_CFG}.3.1": ("string", "Equipment Rack 4", True),
            f"{R2_CFG}.4.1": ("string", "Room 210", True),
            f"{R2_CFG}.6.1": ("integer", 5, True),      # coldstart delay
            f"{R2_CFG}.7.1": ("integer", 2, True),      # low load, 0.1 kW
            f"{R2_CFG}.8.1": ("integer", 30, True),     # near overload
            f"{R2_CFG}.9.1": ("integer", 36, True),     # overload
            f"{R2_CFG}.10.1": ("integer", 1, True),     # peak power reset
            f"{R2_CFG}.11.1": ("integer", 1, True),     # energy reset
            f"{R2_CFG}.12.1": ("integer", 1, True),     # outlet energy reset
            f"{R2_CFG}.13.1": ("integer", 1, True),     # outlet peak reset

            f"{R2_PROPS}.1.1": ("integer", 1),
            f"{R2_PROPS}.4.1": ("integer", OUTLET_COUNT),
            f"{R2_PROPS}.5.1": ("integer", OUTLET_COUNT),
            f"{R2_PROPS}.6.1": ("integer", OUTLET_COUNT if self.metered else 0),
            f"{R2_PROPS}.7.1": ("integer", 1),
            f"{R2_PROPS}.8.1": ("integer", BANK_COUNT),
            f"{R2_PROPS}.9.1": ("integer", 30),

            f"{R2_STATUS}.1.1": ("integer", 1),
            f"{R2_STATUS}.4.1": ("integer", 2),         # load state: normal
            f"{R2_STATUS}.5.1": ("integer", 0),         # power, 0.01 kW
            f"{R2_STATUS}.6.1": ("integer", 214),       # peak power
            f"{R2_STATUS}.9.1": ("integer", 4821),      # energy, 0.1 kWh
            f"{R2_STATUS}.11.1": ("integer", 2),        # no command pending
            f"{R2_STATUS}.12.1": ("integer", 1),        # psu alarm: normal
            f"{R2_STATUS}.13.1": ("integer", 1),        # psu 1: normal
            f"{R2_STATUS}.14.1": ("integer", 3),        # psu 2: not installed
            f"{R2_STATUS}.16.1": (
                "integer", 198 if self.metered else absent),
            f"{R2_STATUS}.17.1": (
                "integer", 98 if self.metered else absent),

            f"{R2_CONTROL}.1.1": ("integer", 1),
            f"{R2_CONTROL}.4.1": ("integer", 6, True),  # noCommandAll

            f"{R2_GROUP}.1.0": ("integer", 1),
            f"{R2_GROUP}.2.0": ("integer", 0),
            f"{R2_GROUP}.3.0": ("integer", 4821),

            # One phase.
            f"{R2_PH_STATUS}.1.1": ("integer", 1),
            f"{R2_PH_STATUS}.2.1": ("integer", 1),
            f"{R2_PH_STATUS}.3.1": ("integer", 1),
            f"{R2_PH_STATUS}.4.1": ("integer", 2),      # normal
            f"{R2_PH_STATUS}.5.1": ("integer", 0),      # current, 0.1 A
            f"{R2_PH_STATUS}.6.1": ("integer", 120),
            f"{R2_PH_STATUS}.7.1": ("integer", 0),
            f"{R2_PH_STATUS}.8.1": (
                "integer", 198 if self.metered else absent),
            f"{R2_PH_STATUS}.9.1": (
                "integer", 98 if self.metered else absent),
            f"{R2_PH_STATUS}.10.1": ("integer", 231),   # peak current
            f"{R2_PH_CFG}.1.1": ("integer", 1),
            f"{R2_PH_CFG}.4.1": ("integer", 1, True),   # always allow turn on
            f"{R2_PH_CFG}.5.1": ("integer", 10, True),
            f"{R2_PH_CFG}.6.1": ("integer", 240, True),
            f"{R2_PH_CFG}.7.1": ("integer", 300, True),
        }

        for bank in range(1, BANK_COUNT + 1):
            oids.update({
                f"{R2_BANK_STATUS}.1.{bank}": ("integer", bank),
                f"{R2_BANK_STATUS}.2.{bank}": ("integer", 1),
                f"{R2_BANK_STATUS}.3.{bank}": ("integer", bank),
                f"{R2_BANK_STATUS}.4.{bank}": ("integer", 2),
                f"{R2_BANK_STATUS}.5.{bank}": ("integer", 0),
                f"{R2_BANK_STATUS}.6.{bank}": ("integer", 118 + bank),
                f"{R2_BANK_PROPS}.1.{bank}": ("integer", bank),
                f"{R2_BANK_PROPS}.5.{bank}": ("integer", 20),
                f"{R2_BANK_CFG}.1.{bank}": ("integer", bank),
                f"{R2_BANK_CFG}.5.{bank}": ("integer", 5, True),
                f"{R2_BANK_CFG}.6.{bank}": ("integer", 160, True),
                f"{R2_BANK_CFG}.7.{bank}": ("integer", 200, True),
            })

        for i in range(1, OUTLET_COUNT + 1):
            on = i in _ON_AT_START
            bank = 1 if i <= OUTLET_COUNT // 2 else 2
            oids.update({
                f"{R2_OUT_STATUS}.1.{i}": ("integer", i),
                f"{R2_OUT_STATUS}.2.{i}": ("integer", 1),
                f"{R2_OUT_STATUS}.3.{i}": ("string", f"Outlet {i}"),
                f"{R2_OUT_STATUS}.4.{i}": ("integer", i),
                f"{R2_OUT_STATUS}.5.{i}": (
                    "integer", R2_STATE_ON if on else R2_STATE_OFF),
                f"{R2_OUT_STATUS}.6.{i}": ("integer", 2),
                f"{R2_OUT_CONTROL}.1.{i}": ("integer", i),
                f"{R2_OUT_CONTROL}.5.{i}": (
                    "integer", R2_CMD_ON if on else R2_CMD_OFF, True),
                f"{R2_OUT_CFG}.1.{i}": ("integer", i),
                f"{R2_OUT_CFG}.3.{i}": ("string", f"Outlet {i}", True),
                f"{R2_OUT_CFG}.4.{i}": ("integer", i),
                f"{R2_OUT_CFG}.5.{i}": ("integer", 0, True),
                f"{R2_OUT_CFG}.6.{i}": ("integer", 0, True),
                f"{R2_OUT_CFG}.7.{i}": ("integer", 5, True),
                f"{R2_OUT_PROPS}.1.{i}": ("integer", i),
                f"{R2_OUT_PROPS}.5.{i}": ("integer", 1),   # L1-N
                f"{R2_OUT_PROPS}.6.{i}": ("integer", bank),
            })
            if self.metered:
                draw = _DRAW_TENTHS[i] if on else 0
                oids.update({
                    f"{R2_MET_STATUS}.1.{i}": ("integer", i),
                    f"{R2_MET_STATUS}.3.{i}": ("string", f"Outlet {i}"),
                    f"{R2_MET_STATUS}.5.{i}": ("integer", 2),
                    f"{R2_MET_STATUS}.6.{i}": ("integer", draw),
                    f"{R2_MET_STATUS}.7.{i}": ("integer", _watts(draw)),
                    f"{R2_MET_STATUS}.8.{i}": (
                        "integer", _watts(_DRAW_TENTHS[i])),
                    f"{R2_MET_STATUS}.11.{i}": ("integer", 30 + i),
                    f"{R2_MET_PROPS}.1.{i}": ("integer", i),
                    f"{R2_MET_PROPS}.7.{i}": ("integer", bank),
                })

        # One temperature + humidity sensor on the PDU's sensor port.
        oids.update({
            f"{R2_SENSOR}.1.1": ("integer", 1),
            f"{R2_SENSOR}.3.1": ("string", "Rack 4 Inlet"),
            f"{R2_SENSOR}.4.1": ("integer", 1),
            f"{R2_SENSOR}.5.1": ("integer", 2),     # temperature + humidity
            f"{R2_SENSOR}.6.1": ("integer", 2),     # comms OK
            f"{R2_SENSOR}.7.1": ("integer", 724),   # 72.4 F
            f"{R2_SENSOR}.8.1": ("integer", 224),   # 22.4 C
            f"{R2_SENSOR}.9.1": ("integer", 4),     # normal
            f"{R2_SENSOR}.10.1": ("integer", 41),   # 41 %RH
            f"{R2_SENSOR}.11.1": ("integer", 4),    # normal
            f"{R2_SENSOR}.13.1": ("integer", 268),  # peak 26.8 C
        })
        return oids

    def _build_rpdu(self) -> dict[str, tuple]:
        """The first generation. Scalars instance as .0, and the outlet-state
        and load-state enums are numbered the other way round."""
        oids: dict[str, tuple] = {
            f"{R1_IDENT}.1.0": ("string", "Equipment Rack 4", True),
            f"{R1_IDENT}.2.0": ("string", "B2"),
            f"{R1_IDENT}.3.0": ("string", "v3.9.2"),
            f"{R1_IDENT}.5.0": ("string", "AP7921"),
            f"{R1_IDENT}.6.0": ("string", "ZA0000000000"),
            f"{R1_IDENT}.7.0": ("integer", 16),          # device rating, A
            f"{R1_IDENT}.8.0": ("integer", LEGACY_OUTLET_COUNT),
            f"{R1_IDENT}.9.0": ("integer", 1),           # phases
            f"{R1_IDENT}.10.0": ("integer", 0),          # breakers
            f"{R1_LOAD_DEV}.1.0": ("integer", 16),
            f"{R1_LOAD_DEV}.2.0": ("integer", 1),        # num phases
            f"{R1_LOAD_DEV}.4.0": ("integer", 0),        # num banks
            f"{R1_OUT_DEV}.1.0": ("integer", 1, True),   # noCommandAll
            f"{R1_OUT_DEV}.2.0": ("integer", 3, True),   # coldstart delay
            f"{R1_OUT_DEV}.3.0": ("integer", LEGACY_OUTLET_COUNT),
            f"{R1_PSU}.1.0": ("integer", 1),
            f"{R1_PSU}.2.0": ("integer", 3),
            f"{R1_PSU}.3.0": ("integer", 1),
            # The combined phase/bank load table. This unit has one phase and
            # no banks, so the whole table is that one phase — the layout the
            # driver's phase filter accepts.
            f"{R1_LOAD_STATUS}.1.1": ("integer", 1),
            f"{R1_LOAD_STATUS}.2.1": ("integer", 0),     # load, 0.1 A
            f"{R1_LOAD_STATUS}.3.1": ("integer", 1),     # normal (1 here!)
            f"{R1_LOAD_STATUS}.4.1": ("integer", 1),     # phase number
        }
        for i in range(1, LEGACY_OUTLET_COUNT + 1):
            on = i in _ON_AT_START
            oids.update({
                f"{R1_OUT_STATUS}.1.{i}": ("integer", i),
                f"{R1_OUT_STATUS}.2.{i}": ("string", f"Outlet {i}"),
                f"{R1_OUT_STATUS}.3.{i}": ("integer", 1),   # phase1
                f"{R1_OUT_STATUS}.4.{i}": (
                    "integer", R1_STATE_ON if on else R1_STATE_OFF),
                f"{R1_OUT_STATUS}.6.{i}": ("integer", 1),
                f"{R1_OUT_CTL}.1.{i}": ("integer", i),
                f"{R1_OUT_CTL}.2.{i}": ("string", f"Outlet {i}", True),
                f"{R1_OUT_CTL}.4.{i}": (
                    "integer", R1_CMD_ON if on else R1_CMD_OFF, True),
                f"{R1_OUT_CFG}.1.{i}": ("integer", i),
                f"{R1_OUT_CFG}.2.{i}": ("string", f"Outlet {i}", True),
                f"{R1_OUT_CFG}.4.{i}": ("integer", 0, True),
                f"{R1_OUT_CFG}.5.{i}": ("integer", 0, True),
                f"{R1_OUT_CFG}.6.{i}": ("integer", 5, True),
            })
        return oids

    # ── Computed values ──────────────────────────────────────────────────

    def read_oid(self, oid: str):
        """sysUpTime is a clock, and the load readings follow the outlets.

        A stored uptime that never moves, and a phase current that does not
        change when half the rack is switched off, are the two things about a
        simulated PDU that read as broken at a glance.
        """
        if oid == SYS_UPTIME:
            return "timeticks", int((time.monotonic() - self._booted) * 100)
        if self.generation == "rpdu2":
            if oid == f"{R2_PH_STATUS}.5.1":
                return "integer", self._total_tenths()
            if oid == f"{R2_STATUS}.5.1":
                return "integer", self._total_hundredth_kw()
            if oid == f"{R2_GROUP}.2.0":
                return "integer", self._total_hundredth_kw()
            if oid.startswith(f"{R2_BANK_STATUS}.5."):
                bank = oid.rsplit(".", 1)[-1]
                if bank.isdigit():
                    return "integer", self._bank_tenths(int(bank))
        elif oid == f"{R1_LOAD_STATUS}.2.1":
            return "integer", self._total_tenths()
        return super().read_oid(oid)

    def _outlet_on(self, index: int) -> bool:
        if self.generation == "rpdu2":
            entry = self.oids.get(f"{R2_OUT_STATUS}.5.{index}")
            return bool(entry and entry[1] == R2_STATE_ON)
        entry = self.oids.get(f"{R1_OUT_STATUS}.4.{index}")
        return bool(entry and entry[1] == R1_STATE_ON)

    def _total_tenths(self) -> int:
        return sum(
            _DRAW_TENTHS[i]
            for i in range(1, self.outlet_count + 1)
            if self._outlet_on(i)
        )

    def _bank_tenths(self, bank: int) -> int:
        half = OUTLET_COUNT // 2
        rng = range(1, half + 1) if bank == 1 else range(half + 1,
                                                         OUTLET_COUNT + 1)
        return sum(_DRAW_TENTHS[i] for i in rng if self._outlet_on(i))

    # ── Writes ───────────────────────────────────────────────────────────

    def write_oid(self, oid: str, type_name: str, value):
        status = super().write_oid(oid, type_name, value)
        if status != 0:
            return status
        if self.generation == "rpdu2":
            if oid.startswith(f"{R2_OUT_CONTROL}.5."):
                self._apply_outlet(oid.rsplit(".", 1)[-1], int(value),
                                   R2_CMD_ON, R2_CMD_OFF, R2_CMD_REBOOT,
                                   R2_CMD_DELAYED_ON, R2_CMD_DELAYED_OFF,
                                   R2_CMD_DELAYED_REBOOT)
            elif oid == f"{R2_CONTROL}.4.1":
                self._apply_all(int(value), R2_ALL_ON, R2_ALL_OFF,
                                R2_ALL_REBOOT)
            elif oid.startswith(f"{R2_OUT_CFG}.3."):
                # The config name and the status name are the same name.
                index = oid.rsplit(".", 1)[-1]
                self._store(f"{R2_OUT_STATUS}.3.{index}", "string", value)
                if self.metered:
                    self._store(f"{R2_MET_STATUS}.3.{index}", "string", value)
            elif oid == f"{R2_CFG}.11.1" and int(value) == 2:
                self._store(f"{R2_STATUS}.9.1", "integer", 0)
            elif oid == f"{R2_CFG}.10.1" and int(value) == 2:
                self._store(f"{R2_STATUS}.6.1", "integer",
                            self._total_hundredth_kw())
        else:
            if oid.startswith(f"{R1_OUT_CTL}.4."):
                self._apply_outlet(oid.rsplit(".", 1)[-1], int(value),
                                   R1_CMD_ON, R1_CMD_OFF, R1_CMD_REBOOT,
                                   R1_CMD_DELAYED_ON, R1_CMD_DELAYED_OFF,
                                   R1_CMD_DELAYED_REBOOT)
            elif oid == f"{R1_OUT_DEV}.1.0":
                self._apply_all(int(value), R1_ALL_ON, R1_ALL_OFF,
                                R1_ALL_REBOOT)
            elif oid.startswith(f"{R1_OUT_CFG}.2."):
                index = oid.rsplit(".", 1)[-1]
                self._store(f"{R1_OUT_STATUS}.2.{index}", "string", value)
                self._store(f"{R1_OUT_CTL}.2.{index}", "string", value)
        return status

    def _apply_outlet(
        self, index: str, command: int, on: int, off: int, reboot: int,
        delayed_on: int, delayed_off: int, delayed_reboot: int,
    ) -> None:
        """Turn a control-column write into the status column's own value.

        The delayed forms land in the same place as the immediate ones here:
        what the driver has to get right is which state results, not how long
        the PDU waited, and a simulator that sat on a timer would make every
        test sleep.
        """
        if not index.isdigit():
            return
        if command in (on, delayed_on, reboot, delayed_reboot):
            powered = True
        elif command in (off, delayed_off):
            powered = False
        else:
            # cancelPendingCommand, or a value this branch does not define.
            return
        self._set_outlet(int(index), powered)

    def _apply_all(self, command: int, on: int, off: int,
                   reboot: int) -> None:
        if command == on or command == reboot:
            powered = True
        elif command == off:
            powered = False
        else:
            return
        for i in range(1, self.outlet_count + 1):
            self._set_outlet(i, powered)

    def _set_outlet(self, index: int, powered: bool) -> None:
        if self.generation == "rpdu2":
            self._store(f"{R2_OUT_STATUS}.5.{index}", "integer",
                        R2_STATE_ON if powered else R2_STATE_OFF)
            self._store(f"{R2_OUT_CONTROL}.5.{index}", "integer",
                        R2_CMD_ON if powered else R2_CMD_OFF, writable=True)
            if self.metered:
                draw = _DRAW_TENTHS[index] if powered else 0
                self._store(f"{R2_MET_STATUS}.6.{index}", "integer", draw)
                self._store(f"{R2_MET_STATUS}.7.{index}", "integer",
                            _watts(draw))
        else:
            self._store(f"{R1_OUT_STATUS}.4.{index}", "integer",
                        R1_STATE_ON if powered else R1_STATE_OFF)
            self._store(f"{R1_OUT_CTL}.4.{index}", "integer",
                        R1_CMD_ON if powered else R1_CMD_OFF, writable=True)
        self.set_state(f"outlet_{index}", powered)
        self.set_state("load_amps", round(self._total_tenths() / 10, 1))

    def _total_hundredth_kw(self) -> int:
        return int(round(_watts(self._total_tenths()) / 10))

    def _store(self, oid: str, type_name: str, value,
               writable: bool = False) -> None:
        existing = self.oids.get(oid)
        if existing is not None and len(existing) > 2:
            writable = existing[2]
        self.oids[oid] = (
            (type_name, value, True) if writable else (type_name, value)
        )

    def _publish_outlets(self) -> None:
        for i in range(1, self.outlet_count + 1):
            self.set_state(f"outlet_{i}", self._outlet_on(i))
        self.set_state("load_amps", round(self._total_tenths() / 10, 1))
