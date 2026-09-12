"""APC Switched Rack PDU driver (PowerNet MIB, SNMP v2c).

A Switched Rack PDU is a rack strip whose outlets can be switched individually
over the network. APC's control surface for it is a MIB, not a command
protocol, so this driver is a set of OIDs rather than a set of strings.

**Two MIB trees, and they disagree about numbers.** APC ships two generations:

  ``rPDU2``  1.3.6.1.4.1.318.1.1.26 — Rack PDU 2G and later (AP84xx, AP86xx,
             AP88xx, AP89xx). Per-outlet metering, banks, phases, energy,
             a temperature/humidity sensor port.
  ``rPDU``   1.3.6.1.4.1.318.1.1.12 — the original generation (AP79xx).
             Outlet switching, phase/bank load, no per-outlet metering.

A 2G unit answers both (APC's scripting FAQ calls it backwards compatibility),
so the driver walks ``rPDU2`` first and falls back to ``rPDU`` — an AP7921 and
an AP8959 both come up, each with the surface it actually has.

The trap is that the two trees number the *same three facts* differently:

  outlet state       rPDU2 off=1 on=2      rPDU  on=1 off=2      (inverted)
  load state         rPDU2 low=1 normal=2  rPDU  normal=1 low=2  (swapped)
  all-outlet command rPDU2 allOn=1 ...     rPDU  noCommand=1 ... (shifted)

Reading one tree's numbers with the other's meaning reports every outlet
backwards, silently and plausibly. So every enum lives in its tree's own
``Tree`` profile, nothing is decoded without one, and
``tests/test_apc_rack_pdu.py`` pins all four mappings against the MIB.

**Polling, deliberately.** SNMP's push channel is the trap (an unsolicited
datagram to UDP 162), and the platform has no listener shape for a unicast port
shared across every agent on the network — see the pending-push tracker in the
driver roadmap, where ``snmp_v2c`` holds the same row. Nothing a PDU reports is
transient: an outlet changes only when commanded, bank load is a slow analog
reading, and a breaker trip or threshold crossing latches until cleared, so a
poll cannot look the wrong way at the wrong moment.

**Why Python, not YAML.** ``snmp`` is a Python-only transport: a request is a
list of OIDs, not a send string, so there is nothing for ``ConfigurableDriver``
to substitute into and no reply text to match. The roster is read off the
device with a walk, and the two-tree detection picks the OID set at connect.

Source:
  APC PowerNet MIB v4.6.0 (powernet460.mib), the objects' own DESCRIPTION
  clauses — https://www.se.com/us/en/download/document/APC_POWERNETMIB_EN/
  Scripting with APC Switched Rack PDUs (the rPDU2 outlet-control branch and
  the generation split) — https://www.se.com/us/en/faqs/FA156163/
  Switched Rack PDU AP89xx User Guide (delay ranges, reading units)
  https://download.schneider-electric.com/files?p_enDocType=User+guide&p_File_Name=JSAI-862KZR_R2_EN.pdf&p_Doc_Ref=SPD_JSAI-862KZR_EN
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.transport.snmp import SnmpError
from openavc.utils.logger import get_logger

log = get_logger(__name__)

# One request's worth of varbinds. Sixteen keeps a realistic PDU poll inside a
# 1500-byte datagram (an agent that overruns the path answers tooBig, or a
# middlebox drops it) while cutting a 24-outlet metered strip from 140 round
# trips to nine. Same number `snmp_v2c` settled on, for the same reason.
VARBINDS_PER_REQUEST = 16

# Ceiling on a column walk. The largest rPDU2 group is four PDUs of 24 outlets
# under one Network Port Sharing host, so 256 is generous; the limit exists so
# an agent that never leaves the subtree cannot walk forever.
WALK_LIMIT = 256

# The MIB-II system group (RFC 3418). Read without being declared, so the card
# shows what answered even on a PDU whose PowerNet branch is somehow empty.
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"

# APC's management arc — apcmgmt is 318.2, a sibling of products (318.1), so
# this OID does NOT sit under the Rack PDU branches. mcontrolRestartAgent's
# restartCurrentAgent reboots the Network Management Card's interface; the
# outlets keep their state and stay powered (the AP89xx guide's `reboot`
# command is the same action: "restart the interface of the Rack PDU").
MCONTROL_RESTART_AGENT = "1.3.6.1.4.1.318.2.2.1.0"
RESTART_CURRENT_AGENT = 1

RPDU2 = "1.3.6.1.4.1.318.1.1.26"
RPDU = "1.3.6.1.4.1.318.1.1.12"


# ── Enum vocabularies ────────────────────────────────────────────────────
#
# Every one of these is read straight off the MIB object's SYNTAX clause. The
# three marked DIFFERS are the ones the two generations number differently;
# they are the reason a `Tree` exists at all rather than one flat OID table.

# DIFFERS. rPDU2OutletSwitchedStatusState: off(1), on(2).
RPDU2_OUTLET_STATE = {1: False, 2: True}
# DIFFERS. rPDUOutletStatusOutletState: outletStatusOn(1), outletStatusOff(2).
RPDU_OUTLET_STATE = {1: True, 2: False}

# DIFFERS. rPDU2DeviceStatusLoadState / rPDU2BankStatusLoadState /
# rPDU2PhaseStatusLoadState: lowLoad(1), normal(2), nearOverload(3),
# overload(4) — and notsupported(5) on the device one only.
RPDU2_LOAD_STATE = {
    1: "low", 2: "normal", 3: "near_overload", 4: "overload",
    5: "not_supported",
}
# DIFFERS. rPDULoadStatusLoadState / rPDUStatusBankState: normal(1), low(2),
# nearOverload(3), overload(4).
RPDU_LOAD_STATE = {1: "normal", 2: "low", 3: "near_overload", 4: "overload"}

# DIFFERS. rPDU2DeviceControlCommand.
RPDU2_ALL_COMMAND = {
    "all_outlets_on": 1,
    "all_outlets_on_delayed": 2,
    "all_outlets_off": 3,
    "all_outlets_reboot": 4,
    "all_outlets_reboot_delayed": 5,
    "all_outlets_off_delayed": 7,
    "cancel_all_pending": 8,
}
# DIFFERS. rPDUOutletDevCommand — noCommandAll is 1 here, so every other value
# is one higher than its rPDU2 twin, and the two orders are not the same
# either (delayedAllOn is 5 here, 2 there).
RPDU_ALL_COMMAND = {
    "all_outlets_on": 2,
    "all_outlets_off": 3,
    "all_outlets_reboot": 4,
    "all_outlets_on_delayed": 5,
    "all_outlets_off_delayed": 6,
    "all_outlets_reboot_delayed": 7,
    "cancel_all_pending": 8,
}

# Same in both trees for the first three values, then they diverge: rPDU2
# spends 4 on outletUnknown and pushes the delayed set up by one.
RPDU2_OUTLET_COMMAND = {
    "outlet_on": 1,
    "outlet_off": 2,
    "outlet_reboot": 3,
    "outlet_on_delayed": 5,
    "outlet_off_delayed": 6,
    "outlet_reboot_delayed": 7,
    "outlet_cancel_pending": 8,
}
RPDU_OUTLET_COMMAND = {
    "outlet_on": 1,
    "outlet_off": 2,
    "outlet_reboot": 3,
    "outlet_on_delayed": 4,
    "outlet_off_delayed": 5,
    "outlet_reboot_delayed": 6,
    "outlet_cancel_pending": 7,
}

# Rpdu2OutletPhaseLayoutType, and rPDUOutletStatusOutletPhase which numbers
# the same six the same way.
PHASE_LAYOUT = {
    1: "L1-N", 2: "L2-N", 3: "L3-N", 4: "L1-L2", 5: "L2-L3", 6: "L3-L1",
}

COMMAND_PENDING = {1: True, 2: False, 3: None}  # 3 = commandPendingUnknown
PSU_STATUS = {1: "normal", 2: "alarm", 3: "not_installed"}
SENSOR_TYPE = {
    1: "temperature", 2: "temperature_humidity", 3: "comms_lost",
    4: "not_installed",
}
SENSOR_COMMS = {1: "not_installed", 2: "ok", 3: "lost"}
THRESHOLD_STATUS = {
    1: "not_present", 2: "below_min", 3: "below_low", 4: "normal",
    5: "above_high", 6: "above_max",
}

# rPDU2*ConfigOverloadRestriction — same numbering in both AdvBank and Phase.
OVERLOAD_RESTRICTION = {
    1: "always_allow", 2: "restrict_on_near_overload",
    3: "restrict_on_overload", 4: "not_supported",
}

NO_OPERATION, RESET = 1, 2  # the shared noOperation(1)/reset(2) action enum


@dataclass(frozen=True)
class Column:
    """One column of a MIB table, as this driver publishes it.

    ``oid`` is the column; a row's value lives at ``oid + "." + suffix``.
    ``prop`` is the child state variable it feeds. ``scale`` converts the MIB's
    integer to the unit the state variable is declared in, and ``enum`` maps an
    enumerated integer to the name a panel should show.
    """

    prop: str
    oid: str
    scale: float = 1.0
    enum: dict[int, Any] | None = None
    # -1 means "this model does not support this reading" across the whole
    # rPDU2 tree; publishing it as a number would put -1 W on a panel.
    minus_one_is_absent: bool = False


@dataclass(frozen=True)
class Table:
    """A MIB table this driver turns into child entities.

    ``index`` is walked to find the rows; each row's OID suffix addresses every
    other column. ``live`` is read on every poll, ``slow`` only at connect and
    on Refresh from Device — an outlet's name and its configured delays do not
    change between polls, and reading 24 of them every 30 seconds is most of
    the traffic for none of the value.
    """

    index: str
    live: tuple[Column, ...] = ()
    slow: tuple[Column, ...] = ()


@dataclass(frozen=True)
class Tree:
    """One generation's OID set. Nothing is read or decoded without one."""

    key: str
    label: str
    # Device-level objects. rPDU2 keeps them in tables indexed by Rack PDU
    # (".1" is the host, ".2".."4" are Network Port Sharing guests); the
    # legacy tree keeps them as plain scalars, which instance as ".0".
    suffix: str
    scalars: tuple[Column, ...]
    all_command_oid: str
    all_commands: dict[str, int]
    outlet_command_oid: str
    outlet_commands: dict[str, int]
    outlet_state_oid: str
    outlet_name_write_oid: str
    outlets: Table
    banks: Table | None = None
    phases: Table | None = None
    sensors: Table | None = None
    # Per-outlet writable config: state variable -> (column oid, min, max).
    outlet_config: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    # Device-level writable settings: setting key -> (oid, MIB type).
    settings: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Resettable peak/energy meters: command name -> oid.
    resets: dict[str, str] = field(default_factory=dict)


def _c(prop: str, oid: str, **kw: Any) -> Column:
    return Column(prop=prop, oid=oid, **kw)


# ── rPDU2 (Rack PDU 2G and later) ────────────────────────────────────────

_R2_IDENT = f"{RPDU2}.2.1"
_R2_CFG = f"{RPDU2}.4.1.1"
_R2_PROPS = f"{RPDU2}.4.2.1"
_R2_STATUS = f"{RPDU2}.4.3.1"
_R2_OUT_CFG = f"{RPDU2}.9.2.1.1"
_R2_OUT_PROPS = f"{RPDU2}.9.2.2.1"
_R2_OUT_STATUS = f"{RPDU2}.9.2.3.1"
_R2_MET_PROPS = f"{RPDU2}.9.4.2.1"
_R2_MET_STATUS = f"{RPDU2}.9.4.3.1"
_R2_BANK_CFG = f"{RPDU2}.8.1.1"
_R2_BANK_PROPS = f"{RPDU2}.8.2.1"
_R2_BANK_STATUS = f"{RPDU2}.8.3.1"
_R2_PH_CFG = f"{RPDU2}.6.1.1"
_R2_PH_STATUS = f"{RPDU2}.6.3.1"
_R2_SENSOR = f"{RPDU2}.10.2.2.1"

TREE_RPDU2 = Tree(
    key="rpdu2",
    label="Rack PDU 2G (rPDU2)",
    suffix="1",
    scalars=(
        _c("model", f"{_R2_IDENT}.8"),
        _c("serial_number", f"{_R2_IDENT}.9"),
        _c("firmware", f"{_R2_IDENT}.6"),
        _c("hardware_rev", f"{_R2_IDENT}.5"),
        _c("device_name", f"{_R2_CFG}.3"),
        _c("device_location", f"{_R2_CFG}.4"),
        _c("coldstart_delay", f"{_R2_CFG}.6"),
        _c("low_load_threshold_kw", f"{_R2_CFG}.7", scale=0.1),
        _c("near_overload_threshold_kw", f"{_R2_CFG}.8", scale=0.1),
        _c("overload_threshold_kw", f"{_R2_CFG}.9", scale=0.1),
        _c("outlet_count", f"{_R2_PROPS}.4"),
        _c("switched_outlet_count", f"{_R2_PROPS}.5"),
        _c("metered_outlet_count", f"{_R2_PROPS}.6"),
        _c("phase_count", f"{_R2_PROPS}.7"),
        _c("bank_count", f"{_R2_PROPS}.8"),
        _c("max_current_rating_a", f"{_R2_PROPS}.9"),
        _c("load_state", f"{_R2_STATUS}.4", enum=RPDU2_LOAD_STATE),
        _c("power_kw", f"{_R2_STATUS}.5", scale=0.01, minus_one_is_absent=True),
        _c("peak_power_kw", f"{_R2_STATUS}.6", scale=0.01,
           minus_one_is_absent=True),
        _c("energy_kwh", f"{_R2_STATUS}.9", scale=0.1,
           minus_one_is_absent=True),
        _c("command_pending", f"{_R2_STATUS}.11", enum=COMMAND_PENDING),
        _c("power_supply_alarm", f"{_R2_STATUS}.12", enum={1: False, 2: True}),
        _c("power_supply_1", f"{_R2_STATUS}.13", enum=PSU_STATUS),
        _c("power_supply_2", f"{_R2_STATUS}.14", enum=PSU_STATUS),
        _c("apparent_power_kva", f"{_R2_STATUS}.16", scale=0.01,
           minus_one_is_absent=True),
        _c("power_factor", f"{_R2_STATUS}.17", scale=0.01,
           minus_one_is_absent=True),
    ),
    all_command_oid=f"{RPDU2}.4.4.1.4",
    all_commands=RPDU2_ALL_COMMAND,
    outlet_command_oid=f"{RPDU2}.9.2.4.1.5",
    outlet_commands=RPDU2_OUTLET_COMMAND,
    outlet_state_oid=f"{_R2_OUT_STATUS}.5",
    outlet_name_write_oid=f"{_R2_OUT_CFG}.3",
    outlets=Table(
        index=f"{_R2_OUT_STATUS}.1",
        live=(
            _c("state", f"{_R2_OUT_STATUS}.5", enum=RPDU2_OUTLET_STATE),
            _c("command_pending", f"{_R2_OUT_STATUS}.6",
               enum=COMMAND_PENDING),
        ),
        slow=(
            _c("name", f"{_R2_OUT_STATUS}.3"),
            _c("number", f"{_R2_OUT_STATUS}.4"),
            _c("module", f"{_R2_OUT_STATUS}.2"),
            _c("bank", f"{_R2_OUT_PROPS}.6"),
            _c("phase", f"{_R2_OUT_PROPS}.5", enum=PHASE_LAYOUT),
            _c("power_on_delay", f"{_R2_OUT_CFG}.5"),
            _c("power_off_delay", f"{_R2_OUT_CFG}.6"),
            _c("reboot_duration", f"{_R2_OUT_CFG}.7"),
        ),
    ),
    banks=Table(
        index=f"{_R2_BANK_STATUS}.1",
        live=(
            _c("current_a", f"{_R2_BANK_STATUS}.5", scale=0.1),
            _c("peak_current_a", f"{_R2_BANK_STATUS}.6", scale=0.1),
            _c("load_state", f"{_R2_BANK_STATUS}.4", enum=RPDU2_LOAD_STATE),
        ),
        slow=(
            _c("number", f"{_R2_BANK_STATUS}.3"),
            _c("module", f"{_R2_BANK_STATUS}.2"),
            _c("breaker_rating_a", f"{_R2_BANK_PROPS}.5"),
            _c("low_load_threshold_a", f"{_R2_BANK_CFG}.5", scale=0.1),
            _c("near_overload_threshold_a", f"{_R2_BANK_CFG}.6", scale=0.1),
            _c("overload_threshold_a", f"{_R2_BANK_CFG}.7", scale=0.1),
        ),
    ),
    phases=Table(
        index=f"{_R2_PH_STATUS}.1",
        live=(
            _c("current_a", f"{_R2_PH_STATUS}.5", scale=0.1),
            _c("voltage_v", f"{_R2_PH_STATUS}.6", minus_one_is_absent=True),
            _c("power_kw", f"{_R2_PH_STATUS}.7", scale=0.01,
               minus_one_is_absent=True),
            _c("apparent_power_kva", f"{_R2_PH_STATUS}.8", scale=0.01,
               minus_one_is_absent=True),
            _c("power_factor", f"{_R2_PH_STATUS}.9", scale=0.01,
               minus_one_is_absent=True),
            _c("peak_current_a", f"{_R2_PH_STATUS}.10", scale=0.1),
            _c("load_state", f"{_R2_PH_STATUS}.4", enum=RPDU2_LOAD_STATE),
        ),
        slow=(
            _c("number", f"{_R2_PH_STATUS}.3"),
            _c("module", f"{_R2_PH_STATUS}.2"),
            _c("low_load_threshold_a", f"{_R2_PH_CFG}.5", scale=0.1),
            _c("near_overload_threshold_a", f"{_R2_PH_CFG}.6", scale=0.1),
            _c("overload_threshold_a", f"{_R2_PH_CFG}.7", scale=0.1),
            _c("overload_restriction", f"{_R2_PH_CFG}.4",
               enum=OVERLOAD_RESTRICTION),
        ),
    ),
    sensors=Table(
        index=f"{_R2_SENSOR}.1",
        live=(
            _c("temperature_c", f"{_R2_SENSOR}.8", scale=0.1),
            _c("temperature_f", f"{_R2_SENSOR}.7", scale=0.1),
            _c("humidity", f"{_R2_SENSOR}.10"),
            _c("temperature_status", f"{_R2_SENSOR}.9",
               enum=THRESHOLD_STATUS),
            _c("humidity_status", f"{_R2_SENSOR}.11", enum=THRESHOLD_STATUS),
            _c("comm_status", f"{_R2_SENSOR}.6", enum=SENSOR_COMMS),
            _c("peak_temperature_c", f"{_R2_SENSOR}.13", scale=0.1),
        ),
        slow=(
            _c("name", f"{_R2_SENSOR}.3"),
            _c("number", f"{_R2_SENSOR}.4"),
            _c("sensor_type", f"{_R2_SENSOR}.5", enum=SENSOR_TYPE),
        ),
    ),
    outlet_config={
        # Ranges are the MIB's own, not the CLI guide's: the SNMP surface
        # accepts -1 (never) and 0 (immediate) where the CLI starts at 1.
        "power_on_delay": (f"{_R2_OUT_CFG}.5", -1, 7200),
        "power_off_delay": (f"{_R2_OUT_CFG}.6", -1, 7200),
        "reboot_duration": (f"{_R2_OUT_CFG}.7", 5, 60),
    },
    settings={
        "device_name": (f"{_R2_CFG}.3", "string"),
        "device_location": (f"{_R2_CFG}.4", "string"),
        "coldstart_delay": (f"{_R2_CFG}.6", "integer"),
    },
    resets={
        "reset_peak_power": f"{_R2_CFG}.10",
        "reset_energy": f"{_R2_CFG}.11",
        "reset_outlet_energy": f"{_R2_CFG}.12",
        "reset_outlet_peak_load": f"{_R2_CFG}.13",
    },
)


# ── rPDU (the original generation, AP79xx) ───────────────────────────────

_R1_IDENT = f"{RPDU}.1"
_R1_LOAD_DEV = f"{RPDU}.2.1"
_R1_LOAD_STATUS = f"{RPDU}.2.3.1.1"
_R1_OUT_DEV = f"{RPDU}.3.1"
_R1_OUT_CTL = f"{RPDU}.3.3.1.1"
_R1_OUT_CFG = f"{RPDU}.3.4.1.1"
_R1_OUT_STATUS = f"{RPDU}.3.5.1.1"
_R1_PSU = f"{RPDU}.4.1"
_R1_BANK = f"{RPDU}.5.2.1"

TREE_RPDU = Tree(
    key="rpdu",
    label="Rack PDU, first generation (rPDU)",
    suffix="0",
    scalars=(
        _c("model", f"{_R1_IDENT}.5"),
        _c("serial_number", f"{_R1_IDENT}.6"),
        _c("firmware", f"{_R1_IDENT}.3"),
        _c("hardware_rev", f"{_R1_IDENT}.2"),
        _c("device_name", f"{_R1_IDENT}.1"),
        _c("max_current_rating_a", f"{_R1_IDENT}.7"),
        _c("outlet_count", f"{_R1_IDENT}.8"),
        _c("phase_count", f"{_R1_IDENT}.9"),
        _c("bank_count", f"{_R1_LOAD_DEV}.4"),
        _c("switched_outlet_count", f"{_R1_OUT_DEV}.3"),
        _c("coldstart_delay", f"{_R1_OUT_DEV}.2"),
        _c("power_supply_alarm", f"{_R1_PSU}.3", enum={1: False, 2: True}),
        _c("power_supply_1", f"{_R1_PSU}.1", enum=PSU_STATUS),
        _c("power_supply_2", f"{_R1_PSU}.2", enum=PSU_STATUS),
    ),
    all_command_oid=f"{_R1_OUT_DEV}.1",
    all_commands=RPDU_ALL_COMMAND,
    outlet_command_oid=f"{_R1_OUT_CTL}.4",
    outlet_commands=RPDU_OUTLET_COMMAND,
    outlet_state_oid=f"{_R1_OUT_STATUS}.4",
    outlet_name_write_oid=f"{_R1_OUT_CFG}.2",
    outlets=Table(
        index=f"{_R1_OUT_STATUS}.1",
        live=(_c("state", f"{_R1_OUT_STATUS}.4", enum=RPDU_OUTLET_STATE),),
        slow=(
            _c("name", f"{_R1_OUT_STATUS}.2"),
            _c("bank", f"{_R1_OUT_STATUS}.6"),
            _c("phase", f"{_R1_OUT_STATUS}.3", enum=PHASE_LAYOUT),
            _c("power_on_delay", f"{_R1_OUT_CFG}.4"),
            _c("power_off_delay", f"{_R1_OUT_CFG}.5"),
            _c("reboot_duration", f"{_R1_OUT_CFG}.6"),
        ),
    ),
    # Banks: state only. The first-generation tree reports per-bank CURRENT
    # only through rPDULoadStatusTable, which interleaves phases, banks and an
    # optional total in one table with no column saying which a row is — see
    # _sync_legacy_phases. rPDUStatusBankTable is indexed by bank and
    # unambiguous, so the bank's load state comes from there and its amps are
    # simply absent on this generation rather than guessed.
    banks=Table(
        index=f"{_R1_BANK}.1",
        live=(_c("load_state", f"{_R1_BANK}.3", enum=RPDU_LOAD_STATE),),
        slow=(_c("number", f"{_R1_BANK}.2"),),
    ),
    phases=Table(
        index=f"{_R1_LOAD_STATUS}.1",
        live=(
            _c("current_a", f"{_R1_LOAD_STATUS}.2", scale=0.1),
            _c("load_state", f"{_R1_LOAD_STATUS}.3", enum=RPDU_LOAD_STATE),
        ),
        slow=(_c("number", f"{_R1_LOAD_STATUS}.4"),),
    ),
    outlet_config={
        "power_on_delay": (f"{_R1_OUT_CFG}.4", -1, 7200),
        "power_off_delay": (f"{_R1_OUT_CFG}.5", -1, 7200),
        "reboot_duration": (f"{_R1_OUT_CFG}.6", 5, 60),
    },
    settings={
        "device_name": (f"{_R1_IDENT}.1", "string"),
        "coldstart_delay": (f"{_R1_OUT_DEV}.2", "integer"),
    },
)

TREES = (TREE_RPDU2, TREE_RPDU)

# The metered-outlet table only exists on rPDU2, and only on a
# metered-by-outlet model (AP89xx). It is keyed by the same outlet index as
# the switched table, so its columns attach to outlet children that already
# exist rather than making a child type of their own.
METERED_OUTLET_LIVE = (
    _c("current_a", f"{_R2_MET_STATUS}.6", scale=0.1),
    # The MIB states no unit for this object, unlike every other reading in
    # the tree. Its own thresholds (rPDU2OutletMeteredConfig*) are documented
    # "in Watts", so Watts is what this publishes — the one claim here that
    # the manufacturer's document does not settle outright. First thing to
    # check against a metered unit.
    _c("power_w", f"{_R2_MET_STATUS}.7"),
    _c("peak_power_w", f"{_R2_MET_STATUS}.8"),
    _c("energy_kwh", f"{_R2_MET_STATUS}.11", scale=0.1),
)
METERED_OUTLET_SLOW = (_c("bank", f"{_R2_MET_PROPS}.7"),)
METERED_OUTLET_INDEX = f"{_R2_MET_STATUS}.1"

# rPDU2Group — the Network Port Sharing roll-up across the host and its
# guests. Scalars, so they instance as ".0".
GROUP_DEVICE_COUNT = f"{RPDU2}.11.1.0"
GROUP_TOTAL_POWER = f"{RPDU2}.11.2.0"
GROUP_TOTAL_ENERGY = f"{RPDU2}.11.3.0"


# ── Child entities ───────────────────────────────────────────────────────
#
# Outlets, banks, phases and sensors are all real sub-units of the strip, each
# addressable and each with its own readings, so each is a child type rather
# than a run of flat outlet_1_* keys. Ids are INTEGERS: an rPDU table index is
# a plain integer in this MIB (rPDU2OutletSwitchedStatusIndex SYNTAX INTEGER),
# which is exactly what the generic `snmp_v2c` cannot assume and why it sorts
# outlet 10 before outlet 2. Unpadded, matching the switchers and the sibling
# `racklink_rlnk`; `wattbox_ip` pads to two, and either reads fine, but an
# unpadded id is what a user typing `device.pdu.outlet.3.state` expects.

CHILD_ENTITY_TYPES: dict[str, Any] = {
    "outlet": {
        "label": "Outlet",
        "label_plural": "Outlets",
        "id_format": {"type": "integer", "min": 1, "max": 96},
        "state_variables": {
            "state": {
                "type": "boolean", "label": "Powered",
                "control": True, "cloud_priority": "high",
            },
            "name": {"type": "string", "label": "Name",
                     "cloud_priority": "low"},
            "number": {"type": "integer", "label": "Outlet Number",
                       "cloud_priority": "low"},
            "module": {"type": "integer", "label": "Rack PDU",
                       "cloud_priority": "low"},
            "bank": {"type": "integer", "label": "Bank",
                     "cloud_priority": "low"},
            "phase": {
                "type": "enum", "label": "Phase",
                "values": ["L1-N", "L2-N", "L3-N", "L1-L2", "L2-L3", "L3-L1"],
                "cloud_priority": "low",
            },
            "command_pending": {
                "type": "boolean", "label": "Command Pending",
                "cloud_priority": "high",
            },
            "power_on_delay": {
                "type": "integer", "label": "Power-On Delay",
                "unit": "s", "min": -1, "max": 7200,
                "cloud_priority": "low",
            },
            "power_off_delay": {
                "type": "integer", "label": "Power-Off Delay",
                "unit": "s", "min": -1, "max": 7200,
                "cloud_priority": "low",
            },
            "reboot_duration": {
                "type": "integer", "label": "Reboot Duration",
                "unit": "s", "min": 5, "max": 60, "cloud_priority": "low",
            },
            # Metered-by-outlet models only (AP89xx). Absent elsewhere.
            "current_a": {
                "type": "number", "label": "Current", "unit": "A",
                "min": 0, "max": 32, "cloud_priority": "low",
            },
            "power_w": {
                "type": "number", "label": "Power", "unit": "W",
                "min": 0, "cloud_priority": "low",
            },
            "peak_power_w": {
                "type": "number", "label": "Peak Power", "unit": "W",
                "min": 0, "cloud_priority": "low",
            },
            "energy_kwh": {
                "type": "number", "label": "Energy", "unit": "kWh",
                "min": 0, "cloud_priority": "low",
            },
        },
        "summary_fields": ["name", "state", "power_w"],
        "label_field": "name",
    },
    "bank": {
        "label": "Bank",
        "label_plural": "Banks",
        "id_format": {"type": "integer", "min": 1, "max": 24},
        "state_variables": {
            "load_state": {
                "type": "enum", "label": "Load State",
                "values": ["low", "normal", "near_overload", "overload",
                           "not_supported"],
                "cloud_priority": "high",
            },
            "current_a": {
                "type": "number", "label": "Current", "unit": "A",
                "min": 0, "max": 64, "cloud_priority": "low",
            },
            "peak_current_a": {
                "type": "number", "label": "Peak Current", "unit": "A",
                "min": 0, "max": 64, "cloud_priority": "low",
            },
            "number": {"type": "integer", "label": "Bank Number",
                       "cloud_priority": "low"},
            "module": {"type": "integer", "label": "Rack PDU",
                       "cloud_priority": "low"},
            "breaker_rating_a": {
                "type": "integer", "label": "Breaker Rating", "unit": "A",
                "min": 0, "max": 64, "cloud_priority": "low",
            },
            "low_load_threshold_a": {
                "type": "number", "label": "Low-Load Threshold", "unit": "A",
                "min": 0, "max": 64, "cloud_priority": "low",
            },
            "near_overload_threshold_a": {
                "type": "number", "label": "Near-Overload Threshold",
                "unit": "A", "min": 0, "max": 64, "cloud_priority": "low",
            },
            "overload_threshold_a": {
                "type": "number", "label": "Overload Threshold", "unit": "A",
                "min": 0, "max": 64, "cloud_priority": "low",
            },
        },
        "summary_fields": ["number", "current_a", "load_state"],
    },
    "phase": {
        "label": "Phase",
        "label_plural": "Phases",
        "id_format": {"type": "integer", "min": 1, "max": 12},
        "state_variables": {
            "load_state": {
                "type": "enum", "label": "Load State",
                "values": ["low", "normal", "near_overload", "overload",
                           "not_supported"],
                "cloud_priority": "high",
            },
            "current_a": {
                "type": "number", "label": "Current", "unit": "A",
                "min": 0, "max": 100, "cloud_priority": "low",
            },
            "peak_current_a": {
                "type": "number", "label": "Peak Current", "unit": "A",
                "min": 0, "max": 100, "cloud_priority": "low",
            },
            "voltage_v": {
                "type": "number", "label": "Voltage", "unit": "V",
                "min": 0, "max": 480, "cloud_priority": "low",
            },
            "power_kw": {
                "type": "number", "label": "Power", "unit": "kW",
                "min": 0, "cloud_priority": "low",
            },
            "apparent_power_kva": {
                "type": "number", "label": "Apparent Power", "unit": "kVA",
                "min": 0, "cloud_priority": "low",
            },
            "power_factor": {
                "type": "number", "label": "Power Factor",
                "min": 0, "max": 1, "step": 0.01, "cloud_priority": "low",
            },
            "number": {"type": "integer", "label": "Phase Number",
                       "cloud_priority": "low"},
            "module": {"type": "integer", "label": "Rack PDU",
                       "cloud_priority": "low"},
            "low_load_threshold_a": {
                "type": "number", "label": "Low-Load Threshold", "unit": "A",
                "min": 0, "max": 100, "cloud_priority": "low",
            },
            "near_overload_threshold_a": {
                "type": "number", "label": "Near-Overload Threshold",
                "unit": "A", "min": 0, "max": 100, "cloud_priority": "low",
            },
            "overload_threshold_a": {
                "type": "number", "label": "Overload Threshold", "unit": "A",
                "min": 0, "max": 100, "cloud_priority": "low",
            },
            "overload_restriction": {
                "type": "enum", "label": "Overload Restriction",
                "values": ["always_allow", "restrict_on_near_overload",
                           "restrict_on_overload", "not_supported"],
                "cloud_priority": "low",
            },
        },
        "summary_fields": ["number", "current_a", "load_state"],
    },
    "sensor": {
        "label": "Sensor",
        "label_plural": "Sensors",
        "id_format": {"type": "integer", "min": 1, "max": 8},
        "state_variables": {
            "temperature_c": {
                "type": "number", "label": "Temperature", "unit": "°C",
                "min": -40, "max": 125, "step": 0.1,
                "cloud_priority": "high",
            },
            "temperature_f": {
                "type": "number", "label": "Temperature (F)", "unit": "°F",
                "min": -40, "max": 257, "step": 0.1,
                "cloud_priority": "low",
            },
            "humidity": {
                "type": "integer", "label": "Relative Humidity", "unit": "%",
                "min": 0, "max": 100, "cloud_priority": "high",
            },
            "temperature_status": {
                "type": "enum", "label": "Temperature Status",
                "values": ["not_present", "below_min", "below_low", "normal",
                           "above_high", "above_max"],
                "cloud_priority": "high",
            },
            "humidity_status": {
                "type": "enum", "label": "Humidity Status",
                "values": ["not_present", "below_min", "below_low", "normal",
                           "above_high", "above_max"],
                "cloud_priority": "high",
            },
            "comm_status": {
                "type": "enum", "label": "Sensor Communication",
                "values": ["not_installed", "ok", "lost"],
                "cloud_priority": "high",
            },
            "peak_temperature_c": {
                "type": "number", "label": "Peak Temperature", "unit": "°C",
                "min": -40, "max": 125, "step": 0.1,
                "cloud_priority": "low",
            },
            "name": {"type": "string", "label": "Name",
                     "cloud_priority": "low"},
            "number": {"type": "integer", "label": "Sensor Number",
                       "cloud_priority": "low"},
            "sensor_type": {
                "type": "enum", "label": "Sensor Type",
                "values": ["temperature", "temperature_humidity",
                           "comms_lost", "not_installed"],
                "cloud_priority": "low",
            },
        },
        "summary_fields": ["name", "temperature_c", "humidity"],
        "label_field": "name",
    },
}


class APCRackPDUDriver(BaseDriver):
    """APC Switched Rack PDU over SNMP v2c (PowerNet MIB)."""

    DRIVER_INFO = {
        "id": "apc_rack_pdu",
        "name": "APC Switched Rack PDU",
        "manufacturer": "APC",
        "category": "power",
        "version": "1.0.0",
        "author": "OpenAVC",
        # transport: snmp is the floor the contract computes. An older
        # platform answers "Unsupported transport type" and never connects.
        "min_platform_version": "0.34.0",
        "description": (
            "Controls APC Switched Rack PDUs over SNMP v2c. Outlets, banks, "
            "phases and the temperature/humidity sensor are addressable "
            "children read off the device, with named on/off/reboot commands, "
            "per-outlet delays, load thresholds and energy metering. Covers "
            "both the Rack PDU 2G MIB (AP84xx/86xx/88xx/89xx) and the "
            "first-generation one (AP79xx), detected on connect."
        ),
        "source_url": (
            "https://www.se.com/us/en/download/document/APC_POWERNETMIB_EN/"
        ),
        "tags": ["pdu", "power", "outlet-control", "rack", "snmp",
                 "metering", "energy"],
        "verified": False,
        "simulated": True,
        "protocols": ["snmp", "powernet-mib"],
        "ports": [161],
        "transport": "snmp",
        "discovery": {
            # sysObjectID on any APC device is 1.3.6.1.4.1.318.<...>, and the
            # discovery engine's SNMP scanner already reads it and extracts
            # the enterprise number. 318 is APC's, so this is an enrichment
            # hint rather than an identification: it narrows a scanned SNMP
            # host to APC and lets the card offer this driver. There is no
            # tcp/udp probe on 161 on purpose — the core scanner already
            # queries every SNMP host once, and a driver-declared probe would
            # re-query with a community string baked into its bytes.
            "snmp_pen": 318,
            "oui": ["00:c0:b7"],
            "manufacturer_alias": [
                "apc", "american power conversion", "schneider electric",
                "apc by schneider electric",
            ],
        },
        "compatible_models": [
            {
                "manufacturer": "APC",
                "models": [
                    "AP8941", "AP8953", "AP8958", "AP8959", "AP8961",
                    "AP8965", "AP8858", "AP8859", "AP8886", "AP8888",
                    "AP8432", "AP8453", "AP8458",
                ],
                "confidence": "untested",
                "notes": (
                    "Rack PDU 2G and later, on the rPDU2 MIB branch. "
                    "Switched models expose outlet control; Switched "
                    "Metered-by-Outlet models (AP89xx) add per-outlet "
                    "current, power and energy. Metered-only models connect "
                    "and report banks, phases and device load with no outlet "
                    "switching. The outlet, bank, phase and sensor rosters "
                    "are read off the device, so a 16-outlet and a 24-outlet "
                    "unit each surface exactly what they have."
                ),
            },
            {
                "manufacturer": "APC",
                "models": [
                    "AP7900", "AP7900B", "AP7901", "AP7902", "AP7911A",
                    "AP7920", "AP7921", "AP7921B", "AP7922", "AP7930",
                    "AP7931", "AP7932", "AP7940", "AP7941", "AP7960",
                    "AP7968", "AP7990", "AP7998",
                ],
                "confidence": "untested",
                "notes": (
                    "First-generation Switched Rack PDUs, on the legacy "
                    "rPDU MIB branch, detected automatically. Outlet "
                    "switching, per-outlet delays and naming, phase current "
                    "and bank load state all work. No per-outlet metering, "
                    "no device energy meter and no sensor port on this "
                    "generation, so those values stay blank."
                ),
            },
        ],
        "help": {
            "overview": (
                "APC Switched Rack PDUs are the metered power strips in most "
                "equipment racks. Each outlet is a child entity here, so a "
                "macro, schedule or panel button can switch one outlet, "
                "reboot a wedged device, or bring the whole rack up in "
                "sequence using the delays configured on the PDU itself. "
                "Bank and phase load, the breaker rating and the rack "
                "temperature sensor come through as live values you can "
                "alert on."
            ),
            "setup": (
                "1. Give the Rack PDU's Network Management Card an IP "
                "address and reach its web interface.\n"
                "2. Under Configuration > Network > SNMPv1, enable SNMPv1 "
                "access and note the community strings. Switching outlets "
                "needs a community with Write access — APC ships that as "
                "'private' on Access Control entry 2, and its Access Type "
                "must be Write or Write+ for a command to be accepted.\n"
                "3. Restrict the write community's NMS IP to the OpenAVC "
                "server's address, not 0.0.0.0, so only this server can "
                "switch outlets.\n"
                "4. Enter the PDU's IP and both community strings here, then "
                "run Test Connection — it reads the PDU's own model and "
                "serial back, which proves the address and the read "
                "community, and reports which MIB branch the unit answered "
                "on.\n"
                "5. Outlets appear as children once connected. If the count "
                "is wrong, use Refresh from Device."
            ),
        },
        "default_config": {
            "host": "",
            "port": 161,
            "community": "public",
            "write_community": "private",
            "timeout": 2.0,
            "retries": 1,
            "poll_interval": 30,
        },
        "config_schema": {
            "host": {"type": "string", "required": True,
                     "label": "IP Address",
                     "description": "The Rack PDU's IP address or hostname."},
            "port": {"type": "integer", "default": 161,
                     "label": "Port", "min": 1, "max": 65535,
                     "description": "SNMP port. 161 unless it was moved."},
            "community": {
                "type": "string", "default": "public", "secret": True,
                "label": "Read Community",
                "description": "The community string that may read. APC "
                               "ships this as 'public'.",
            },
            "write_community": {
                "type": "string", "default": "private", "secret": True,
                "label": "Write Community",
                "description": "The community string that may write, which "
                               "is what switching an outlet needs. APC ships "
                               "this as 'private' with Write access. Leave "
                               "blank to write with the read community.",
            },
            "timeout": {"type": "number", "default": 2.0,
                        "label": "Timeout (sec)", "min": 0.5, "max": 30},
            "retries": {
                "type": "integer", "default": 1, "label": "Retries",
                "min": 0, "max": 5,
                "description": "Extra attempts when a request goes "
                               "unanswered. SNMP runs over UDP, which drops "
                               "datagrams without saying so.",
            },
            "poll_interval": {
                "type": "integer", "default": 30,
                "label": "Poll Interval (sec)", "min": 0,
                "description": "How often to re-read outlet states and load "
                               "readings. 0 disables polling.",
            },
        },
        "state_variables": {
            "mib_branch": {
                "type": "enum", "label": "MIB Branch",
                "values": ["rpdu2", "rpdu"],
                "cloud_priority": "low",
            },
            "model": {"type": "string", "label": "Model"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "firmware": {"type": "string", "label": "Firmware Version"},
            "hardware_rev": {"type": "string", "label": "Hardware Revision"},
            "device_name": {"type": "string", "label": "Rack PDU Name"},
            "device_location": {"type": "string", "label": "Location"},
            "outlet_count": {"type": "integer", "label": "Outlets", "min": 0},
            "switched_outlet_count": {
                "type": "integer", "label": "Switched Outlets", "min": 0,
            },
            "metered_outlet_count": {
                "type": "integer", "label": "Metered Outlets", "min": 0,
            },
            "bank_count": {"type": "integer", "label": "Banks", "min": 0},
            "phase_count": {"type": "integer", "label": "Phases", "min": 0},
            "device_count": {
                "type": "integer", "label": "Rack PDUs in Group", "min": 0,
                "cloud_priority": "low",
            },
            "max_current_rating_a": {
                "type": "integer", "label": "Current Rating", "unit": "A",
                "min": 0, "max": 100,
            },
            "load_state": {
                "type": "enum", "label": "Load State",
                "values": ["low", "normal", "near_overload", "overload",
                           "not_supported"],
                "cloud_priority": "high",
            },
            "power_kw": {
                "type": "number", "label": "Load Power", "unit": "kW",
                "min": 0, "step": 0.01, "cloud_priority": "high",
            },
            "peak_power_kw": {
                "type": "number", "label": "Peak Power", "unit": "kW",
                "min": 0, "step": 0.01, "cloud_priority": "low",
            },
            "apparent_power_kva": {
                "type": "number", "label": "Apparent Power", "unit": "kVA",
                "min": 0, "step": 0.01, "cloud_priority": "low",
            },
            "power_factor": {
                "type": "number", "label": "Power Factor",
                "min": 0, "max": 1, "step": 0.01, "cloud_priority": "low",
            },
            "energy_kwh": {
                "type": "number", "label": "Energy", "unit": "kWh",
                "min": 0, "step": 0.1, "cloud_priority": "low",
            },
            "group_power_kw": {
                "type": "number", "label": "Group Power", "unit": "kW",
                "min": 0, "step": 0.01, "cloud_priority": "low",
            },
            "group_energy_kwh": {
                "type": "number", "label": "Group Energy", "unit": "kWh",
                "min": 0, "step": 0.1, "cloud_priority": "low",
            },
            "command_pending": {
                "type": "boolean", "label": "Command Pending",
                "cloud_priority": "high",
            },
            "power_supply_alarm": {
                "type": "boolean", "label": "Power Supply Alarm",
                "cloud_priority": "high",
            },
            "power_supply_1": {
                "type": "enum", "label": "Power Supply 1",
                "values": ["normal", "alarm", "not_installed"],
                "cloud_priority": "high",
            },
            "power_supply_2": {
                "type": "enum", "label": "Power Supply 2",
                "values": ["normal", "alarm", "not_installed"],
                "cloud_priority": "high",
            },
            "coldstart_delay": {
                "type": "integer", "label": "Cold Start Delay", "unit": "s",
                "min": -1, "max": 300, "cloud_priority": "low",
            },
            "low_load_threshold_kw": {
                "type": "number", "label": "Low-Load Threshold", "unit": "kW",
                "min": 0, "step": 0.1, "cloud_priority": "low",
            },
            "near_overload_threshold_kw": {
                "type": "number", "label": "Near-Overload Threshold",
                "unit": "kW", "min": 0, "step": 0.1, "cloud_priority": "low",
            },
            "overload_threshold_kw": {
                "type": "number", "label": "Overload Threshold", "unit": "kW",
                "min": 0, "step": 0.1, "cloud_priority": "low",
            },
            "sys_descr": {"type": "string", "label": "System Description",
                          "cloud_priority": "low"},
            "sys_name": {"type": "string", "label": "System Name",
                         "cloud_priority": "low"},
            "sys_uptime": {"type": "integer", "label": "Uptime", "unit": "s",
                           "min": 0, "cloud_priority": "low"},
        },
        "child_entity_types": CHILD_ENTITY_TYPES,
        "device_settings": {
            # A device setting's state_key names a DEVICE-level state
            # variable, so per-outlet delays cannot be settings even though
            # they read back perfectly; they are commands below, and their
            # current values are outlet child state.
            "coldstart_delay": {
                "type": "integer",
                "label": "Cold Start Delay (sec)",
                "help": (
                    "Added to each outlet's Power-On Delay when the Rack PDU "
                    "itself is powered up, so a rack does not inrush "
                    "everything at once. -1 never powers the outlets on "
                    "automatically; 0 is immediate; 1-300 is a delay in "
                    "seconds."
                ),
                "state_key": "coldstart_delay",
                "default": 0,
                "min": -1,
                "max": 300,
                "setup": False,
            },
            "device_name": {
                "type": "string",
                "label": "Rack PDU Name",
                "help": "The name the Rack PDU shows in its own web "
                        "interface and in its event log.",
                "state_key": "device_name",
                "default": "",
                "setup": False,
            },
            "device_location": {
                "type": "string",
                "label": "Location",
                "help": "The location string the Rack PDU reports. Not "
                        "available on first-generation (AP79xx) units.",
                "state_key": "device_location",
                "default": "",
                "setup": False,
            },
        },
        "quick_actions": [
            "outlet_on", "outlet_off", "outlet_reboot", "refresh",
        ],
        "actions": [
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection",
                "icon": "search",
                "availability": "always",
                "confirm": (
                    "Reads the Rack PDU's model, serial number and outlet "
                    "roster, and reports which MIB branch answered. Nothing "
                    "on the PDU changes and no outlet is switched."
                ),
            },
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "all_outlets_off",
                "kind": "command",
                "icon": "power-off",
                "confirm": (
                    "Switch off EVERY outlet on this Rack PDU immediately. "
                    "Everything in the rack loses power. Continue?"
                ),
            },
            {
                "id": "all_outlets_reboot",
                "kind": "command",
                "icon": "rotate-ccw",
                "confirm": (
                    "Power-cycle EVERY outlet on this Rack PDU. Everything "
                    "in the rack loses power and comes back. Continue?"
                ),
            },
            {
                "id": "restart_management_card",
                "kind": "command",
                "icon": "server",
                "confirm": (
                    "Restart the Rack PDU's network management card. The "
                    "outlets keep their power; only the network interface "
                    "goes away, for about a minute. Continue?"
                ),
            },
        ],
        "commands": {
            "refresh": {
                "label": "Refresh Status",
                "params": {},
                "help": "Re-read every value from the Rack PDU now.",
            },
            # Immediate and delayed are separate commands rather than one
            # command with a mode parameter: a panel button binds to a
            # command, and "turn this outlet off" and "turn it off after its
            # configured delay" are different buttons. Spelled out rather
            # than generated so the contract check can read every one.
            "outlet_on": {
                "label": "Turn Outlet On",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                },
                "help": "Switch an outlet on immediately.",
            },
            "outlet_off": {
                "label": "Turn Outlet Off",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                },
                "help": "Switch an outlet off immediately.",
            },
            "outlet_reboot": {
                "label": "Reboot Outlet",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                },
                "help": "Switch an outlet off, wait its Reboot Duration, "
                        "then switch it on again.",
            },
            "outlet_on_delayed": {
                "label": "Turn Outlet On (delayed)",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                },
                "help": "Switch an outlet on after its configured Power-On "
                        "Delay.",
            },
            "outlet_off_delayed": {
                "label": "Turn Outlet Off (delayed)",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                },
                "help": "Switch an outlet off after its configured "
                        "Power-Off Delay.",
            },
            "outlet_reboot_delayed": {
                "label": "Reboot Outlet (delayed)",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                },
                "help": "Reboot an outlet using its configured Power-Off "
                        "Delay, Reboot Duration and Power-On Delay.",
            },
            "outlet_cancel_pending": {
                "label": "Cancel Outlet's Pending Command",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                },
                "help": "Cancel a delayed command this outlet has not "
                        "carried out yet.",
            },
            "all_outlets_on": {
                "label": "Turn All Outlets On",
                "params": {},
                "help": "Switch every outlet on immediately.",
            },
            "all_outlets_off": {
                "label": "Turn All Outlets Off",
                "params": {},
                "help": "Switch every outlet off immediately.",
            },
            "all_outlets_reboot": {
                "label": "Reboot All Outlets",
                "params": {},
                "help": "Switch every outlet off, wait, then switch them "
                        "all on again.",
            },
            "all_outlets_on_delayed": {
                "label": "Turn All Outlets On (staggered)",
                "params": {},
                "help": "Switch the outlets on using each one's own "
                        "Power-On Delay, so gear comes up in sequence "
                        "instead of all at once.",
            },
            "all_outlets_off_delayed": {
                "label": "Turn All Outlets Off (staggered)",
                "params": {},
                "help": "Switch the outlets off using each one's own "
                        "Power-Off Delay.",
            },
            "all_outlets_reboot_delayed": {
                "label": "Reboot All Outlets (staggered)",
                "params": {},
                "help": "Reboot every outlet using each one's configured "
                        "delays.",
            },
            "cancel_all_pending": {
                "label": "Cancel All Pending Commands",
                "params": {},
                "help": "Cancel every delayed command the Rack PDU has not "
                        "carried out yet.",
            },
            "set_outlet_name": {
                "label": "Rename Outlet",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                    "name": {"type": "string", "required": True,
                             "label": "Name"},
                },
                "help": "Name an outlet so the PDU's own web interface and "
                        "this device page both show what is plugged in.",
            },
            "set_outlet_power_on_delay": {
                "label": "Set Outlet Power-On Delay",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                    "seconds": {
                        "type": "integer", "required": True, "label": "Delay",
                        "min": -1, "max": 7200,
                        "help": "-1 never powers on, 0 is immediate, "
                                "1-7200 delays that many seconds.",
                    },
                },
                "help": "Set the delay this outlet waits before switching on "
                        "for a delayed-on or staggered start-up.",
            },
            "set_outlet_power_off_delay": {
                "label": "Set Outlet Power-Off Delay",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                    "seconds": {
                        "type": "integer", "required": True, "label": "Delay",
                        "min": -1, "max": 7200,
                        "help": "-1 never powers off, 0 is immediate, "
                                "1-7200 delays that many seconds.",
                    },
                },
                "help": "Set the delay this outlet waits before switching "
                        "off for a delayed-off or staggered shutdown.",
            },
            "set_outlet_reboot_duration": {
                "label": "Set Outlet Reboot Duration",
                "params": {
                    "outlet": {"type": "child_id", "child_type": "outlet",
                               "required": True, "label": "Outlet"},
                    "seconds": {
                        "type": "integer", "required": True,
                        "label": "Duration", "min": 5, "max": 60,
                    },
                },
                "help": "How long this outlet stays off during a reboot "
                        "before power comes back (5-60 seconds).",
            },
            "reset_peak_power": {
                "label": "Reset Peak Power",
                "params": {},
                "help": "Replace the recorded peak load with the present "
                        "one and restart the measurement.",
            },
            "reset_energy": {
                "label": "Reset Energy Meter",
                "params": {},
                "help": "Zero the Rack PDU's energy meter and restart it.",
            },
            "reset_outlet_energy": {
                "label": "Reset Outlet Energy Meters",
                "params": {},
                "help": "Zero every per-outlet energy meter. "
                        "Metered-by-outlet models only.",
            },
            "reset_outlet_peak_load": {
                "label": "Reset Outlet Peak Loads",
                "params": {},
                "help": "Clear every per-outlet recorded peak load. "
                        "Metered-by-outlet models only.",
            },
            "restart_management_card": {
                "label": "Restart Management Card",
                "params": {},
                # The card is off the network while it comes back, so
                # without this the platform reports the PDU as faulted for
                # something we asked it to do. NOT measured -- no APC PDU has
                # been on the bench -- and deliberately generous, because too
                # short turns a deliberate restart into exactly the false
                # alarm this field exists to prevent.
                "restarts_device_for": 90,
                "help": "Restart the Rack PDU's network management card. "
                        "Outlets keep their power; the network interface is "
                        "away for about a minute.",
            },
        },
    }

    # Liveness watchdog. SNMP rides UDP, where a send always "succeeds", so an
    # awaited read is the only thing that ever notices the PDU has gone.
    HEALTH_INTERVAL_S = 60.0
    HEALTH_TIMEOUT_S = 10.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the Rack PDU stopped answering SNMP requests."
    )

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._tree: Tree | None = None
        # child type -> {local_id: OID index suffix}. The suffix is what
        # addresses a row; the local id is the child's number in the UI. They
        # are the same string for every rPDU table, but the walk is still the
        # authority on which rows exist.
        self._rows: dict[str, dict[int, str]] = {}
        # Outlet suffixes that also appear in the metered-outlet table.
        self._metered: dict[int, str] = {}
        self._absent: set[str] = set()
        super().__init__(device_id, config, state, events)

    # ── Connect ──────────────────────────────────────────────────────────

    async def _initial_sync(self) -> None:
        """Identify the PDU, pick the MIB branch, read the rosters, and take a
        first reading, so the card is populated before the first poll."""
        try:
            await self._read_system_group()
            self._tree = await self._detect_tree()
            if self._tree is None:
                log.warning(
                    "[%s] Answered SNMP but has neither PowerNet Rack PDU "
                    "branch (rPDU2 %s / rPDU %s). Is this an APC Rack PDU?",
                    self.device_id, RPDU2, RPDU,
                )
                return
            self.set_state("mib_branch", self._tree.key)
            log.info("[%s] APC Rack PDU on %s", self.device_id,
                     self._tree.label)
            await self._reconcile_children()
            await self.poll()
        except (ConnectionError, SnmpError, asyncio.TimeoutError) as e:
            log.warning("[%s] Initial sync failed: %s", self.device_id, e)

    async def _detect_tree(self) -> Tree | None:
        """Which generation is this?

        Asked by walking each branch's outlet-status index for one row. A 2G
        unit answers both branches (APC keeps the first-generation one for
        backwards compatibility), so rPDU2 is tried first and wins — it is the
        branch that carries per-outlet metering, banks and the sensor port.
        """
        for tree in TREES:
            try:
                rows = await self.transport.walk(tree.outlets.index, limit=1)
            except (SnmpError, asyncio.TimeoutError):
                continue
            if rows:
                return tree
        return None

    async def _read_system_group(self) -> None:
        answered = await self.transport.get(
            [SYS_DESCR, SYS_NAME, SYS_UPTIME, SYS_OBJECT_ID, SYS_LOCATION]
        )
        updates: dict[str, Any] = {}
        for name, oid in (("sys_descr", SYS_DESCR), ("sys_name", SYS_NAME)):
            varbind = answered.get(oid)
            if varbind is not None and not varbind.is_exception:
                updates[name] = str(varbind.value)
        uptime = answered.get(SYS_UPTIME)
        if uptime is not None and not uptime.is_exception:
            try:
                # TimeTicks are hundredths of a second; nobody wants that.
                updates["sys_uptime"] = int(uptime.value) // 100
            except (TypeError, ValueError):
                pass
        if updates:
            self.set_states(updates)

    # ── Children ─────────────────────────────────────────────────────────

    async def _reconcile_children(self) -> None:
        """Walk each table and register a child per row, then read the columns
        that do not change between polls.

        The walk is the roster and the PDU is the authority: an outlet that is
        gone (a guest Rack PDU unplugged from a Network Port Sharing group) is
        deregistered rather than left showing its last reading forever.
        """
        tree = self._tree
        if tree is None:
            return
        tables = {
            "outlet": tree.outlets,
            "bank": tree.banks,
            "phase": tree.phases,
            "sensor": tree.sensors,
        }
        for child_type, table in tables.items():
            if table is None:
                continue
            rows = await self._walk_rows(table.index)
            if child_type == "phase" and tree.key == "rpdu":
                rows = await self._filter_legacy_phases(rows)
            self._register(child_type, rows)

        if tree.key == "rpdu2":
            self._metered = await self._walk_rows(METERED_OUTLET_INDEX)

        await self._sync_slow_columns()

    async def _walk_rows(self, index_oid: str) -> dict[int, str]:
        """Row suffixes under a table's index column, keyed by local child id.

        A row's id is its index as an integer, which is what this MIB's table
        indexes are (``SYNTAX INTEGER`` on every one of them). A suffix that
        is not a single integer belongs to a table this driver does not model,
        and is skipped rather than coerced into something that sorts wrong.
        """
        try:
            rows = await self.transport.walk(index_oid, limit=WALK_LIMIT)
        except (SnmpError, asyncio.TimeoutError):
            return {}
        prefix = index_oid + "."
        found: dict[int, str] = {}
        for varbind in rows:
            suffix = varbind.oid[len(prefix):]
            if not suffix.isdigit():
                continue
            found[int(suffix)] = suffix
        return found

    async def _filter_legacy_phases(
        self, rows: dict[int, str],
    ) -> dict[int, str]:
        """Keep only the phase rows of the first generation's combined table.

        ``rPDULoadStatusTable`` holds phases AND banks AND, on some models, a
        device total, in one table with no column saying which a row is. The
        MIB states the count ("#phases + #banks") and the order ("all phase
        information shall precede the bank information", with a total before
        both when present) but nothing a reader can check per row.

        So this verifies rather than guesses: read the phase count, and accept
        the first N rows as phases only when each one's PhaseNumber column is
        its own position. On any other layout it publishes no phase children
        and says why, because a wrong split here would label a bank's amps as
        a phase's.

        The count is read from the device here rather than from
        ``phase_count`` state: the roster walk runs before the first scalar
        poll, so that state variable is still empty at this point.
        """
        phase_count = await self._get_int(f"{_R1_LOAD_DEV}.2.0")
        if phase_count is None or phase_count <= 0:
            return {}
        ordered = sorted(rows)[:phase_count]
        if len(ordered) < phase_count:
            return {}
        number_oid = f"{_R1_LOAD_STATUS}.4"
        answered = await self._get(
            [f"{number_oid}.{rows[i]}" for i in ordered]
        )
        for position, index in enumerate(ordered, start=1):
            varbind = answered.get(f"{number_oid}.{rows[index]}")
            if varbind is None or varbind.is_exception:
                return {}
            if int(varbind.value) != position:
                log.warning(
                    "[%s] rPDULoadStatusTable does not start with phases 1..%d "
                    "(row %d reports phase %s), so this Rack PDU's phase and "
                    "bank rows cannot be told apart. Phase children are not "
                    "published; bank load state still is.",
                    self.device_id, phase_count, index, varbind.value,
                )
                return {}
        return {i: rows[i] for i in ordered}

    def _register(self, child_type: str, rows: dict[int, str]) -> None:
        for local_id in rows:
            self.register_child(child_type, local_id,
                                initial_state={"online": True})
        known = self._rows.setdefault(child_type, {})
        for stale in set(known) - set(rows):
            self.deregister_child(child_type, stale)
        self._rows[child_type] = rows

    async def refresh_children(self) -> dict[str, Any]:
        """The IDE's Refresh from Device button."""
        await self._reconcile_children()
        return {
            child_type: len(rows)
            for child_type, rows in self._rows.items() if rows
        }

    # ── Reads ────────────────────────────────────────────────────────────

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if self._tree is None:
            # Connected to an agent that is not a Rack PDU, or the branch walk
            # failed at connect. Keep asking: an NMC that has just rebooted
            # answers the system group before its PowerNet branch is up.
            await self._read_system_group()
            self._tree = await self._detect_tree()
            if self._tree is not None:
                self.set_state("mib_branch", self._tree.key)
                await self._reconcile_children()
            return
        await self._read_scalars()
        await self._read_children(live_only=True)

    async def _read_scalars(self) -> None:
        tree = self._tree
        assert tree is not None
        by_oid = {
            f"{column.oid}.{tree.suffix}": column for column in tree.scalars
        }
        if tree.key == "rpdu2":
            by_oid[GROUP_DEVICE_COUNT] = _c("device_count", GROUP_DEVICE_COUNT)
            by_oid[GROUP_TOTAL_POWER] = _c(
                "group_power_kw", GROUP_TOTAL_POWER, scale=0.01,
                minus_one_is_absent=True,
            )
            by_oid[GROUP_TOTAL_ENERGY] = _c(
                "group_energy_kwh", GROUP_TOTAL_ENERGY, scale=0.1,
                minus_one_is_absent=True,
            )
        updates: dict[str, Any] = {}
        for chunk in _chunks(list(by_oid), VARBINDS_PER_REQUEST):
            for oid, varbind in (await self._get(chunk)).items():
                column = by_oid.get(oid)
                if column is None:
                    continue
                if varbind.is_exception:
                    self._report_absent(column.prop, varbind.type)
                    continue
                value = _decode(column, varbind.value)
                if value is not None:
                    updates[column.prop] = value
        if updates:
            self.set_states(updates)
        await self._read_system_group()

    async def _sync_slow_columns(self) -> None:
        """Names, numbers, bank/phase assignment, delays, ratings, thresholds.

        Read at connect and on Refresh from Device, not every poll: none of it
        changes on its own, and on a 24-outlet metered strip it is more
        varbinds than everything the poll actually needs.
        """
        await self._read_children(live_only=False)

    async def _read_children(self, *, live_only: bool) -> None:
        tree = self._tree
        if tree is None:
            return
        tables = {
            "outlet": tree.outlets,
            "bank": tree.banks,
            "phase": tree.phases,
            "sensor": tree.sensors,
        }
        wanted: list[str] = []
        owner: dict[str, tuple[str, int, Column]] = {}
        for child_type, table in tables.items():
            if table is None:
                continue
            columns = list(table.live)
            if not live_only:
                columns += list(table.slow)
            for local_id, suffix in self._rows.get(child_type, {}).items():
                for column in columns:
                    oid = f"{column.oid}.{suffix}"
                    wanted.append(oid)
                    owner[oid] = (child_type, local_id, column)

        metered = list(METERED_OUTLET_LIVE)
        if not live_only:
            metered += list(METERED_OUTLET_SLOW)
        for local_id, suffix in self._metered.items():
            for column in metered:
                oid = f"{column.oid}.{suffix}"
                wanted.append(oid)
                owner[oid] = ("outlet", local_id, column)

        per_child: dict[tuple[str, int], dict[str, Any]] = {}
        for chunk in _chunks(wanted, VARBINDS_PER_REQUEST):
            for oid, varbind in (await self._get(chunk)).items():
                entry = owner.get(oid)
                if entry is None or varbind.is_exception:
                    continue
                child_type, local_id, column = entry
                value = _decode(column, varbind.value)
                if value is None:
                    continue
                per_child.setdefault((child_type, local_id), {})[
                    column.prop
                ] = value
        if per_child:
            self.set_children_state_batch(
                [(ct, cid, props) for (ct, cid), props in per_child.items()]
            )

    async def _get(self, oids: list[str]) -> dict[str, Any]:
        """One GET, with silence turned into the platform's offline path.

        An ``SnmpError`` is the agent refusing the whole request — a community
        string that may not see one of these OIDs. That is a log line and a
        stale value, not an offline device.
        """
        if not oids:
            return {}
        try:
            return await self.transport.get(oids)
        except SnmpError as e:
            log.warning("[%s] SNMP read refused: %s", self.device_id, e)
            return {}
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"Rack PDU {self.config.get('host')} did not answer"
            ) from e

    async def _get_int(self, oid: str) -> int | None:
        varbind = (await self._get([oid])).get(oid)
        if varbind is None or varbind.is_exception:
            return None
        try:
            return int(varbind.value)
        except (TypeError, ValueError):
            return None

    def _report_absent(self, name: str, kind: str) -> None:
        if name in self._absent:
            return
        self._absent.add(name)
        log.info(
            "[%s] This Rack PDU does not report %r (%s); the value stays "
            "blank. Normal on models without that feature.",
            self.device_id, name, kind,
        )

    # ── Writes ───────────────────────────────────────────────────────────

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}
        if command == "refresh":
            await self._read_scalars()
            await self._read_children(live_only=False)
            return True

        if command == "restart_management_card":
            await self._set(MCONTROL_RESTART_AGENT, "integer",
                            RESTART_CURRENT_AGENT)
            return True

        tree = self._require_tree()

        if command in tree.outlet_commands:
            suffix = self._outlet_suffix(params)
            await self._set(
                f"{tree.outlet_command_oid}.{suffix}", "integer",
                tree.outlet_commands[command],
            )
            # The PDU applies switching asynchronously and the SET echoes the
            # value we sent, not the outlet's new state, so the reading comes
            # from the status column rather than from what we asked for.
            await self._read_outlet_state(suffix)
            return True

        if command in tree.all_commands:
            await self._set(
                f"{tree.all_command_oid}.{tree.suffix}", "integer",
                tree.all_commands[command],
            )
            await self._read_children(live_only=True)
            return True

        if command == "set_outlet_name":
            suffix = self._outlet_suffix(params)
            name = params.get("name")
            if name is None:
                raise ValueError("set_outlet_name requires a 'name'")
            confirmed = await self._set(
                f"{tree.outlet_name_write_oid}.{suffix}", "string", str(name)
            )
            self.set_child_state("outlet", int(suffix), "name",
                                 str(confirmed))
            return confirmed

        config_write = {
            "set_outlet_power_on_delay": "power_on_delay",
            "set_outlet_power_off_delay": "power_off_delay",
            "set_outlet_reboot_duration": "reboot_duration",
        }.get(command)
        if config_write is not None:
            suffix = self._outlet_suffix(params)
            seconds = params.get("seconds")
            if seconds is None:
                raise ValueError(f"{command} requires 'seconds'")
            oid, low, high = tree.outlet_config[config_write]
            value = int(seconds)
            if not low <= value <= high:
                raise ValueError(
                    f"{config_write} must be between {low} and {high} "
                    f"seconds on this Rack PDU; got {value}"
                )
            confirmed = await self._set(f"{oid}.{suffix}", "integer", value)
            self.set_child_state("outlet", int(suffix), config_write,
                                 int(confirmed))
            return confirmed

        if command in tree.resets:
            await self._set(
                f"{tree.resets[command]}.{tree.suffix}", "integer", RESET
            )
            await self._read_scalars()
            return True

        if command in self.DRIVER_INFO["commands"]:
            # Declared, reachable in the UI, and not supported by the branch
            # this PDU answered on. Saying so is the point: a command that
            # falls through silently answers success and does nothing.
            raise ValueError(
                f"{command} is not available on {tree.label}"
            )
        raise ValueError(f"Unknown command: {command}")

    async def _read_outlet_state(self, suffix: str) -> None:
        tree = self._require_tree()
        oid = f"{tree.outlet_state_oid}.{suffix}"
        answered = await self._get([oid])
        varbind = answered.get(oid)
        if varbind is None or varbind.is_exception:
            return
        column = tree.outlets.live[0]
        value = _decode(column, varbind.value)
        if value is not None:
            self.set_child_state("outlet", int(suffix), "state", value)

    def _outlet_suffix(self, params: dict[str, Any]) -> str:
        local_id = params.get("outlet", params.get("child_id"))
        if local_id is None:
            raise ValueError("This command requires an 'outlet'")
        rows = self._rows.get("outlet", {})
        try:
            key = int(local_id)
        except (TypeError, ValueError):
            raise ValueError(f"Unknown outlet: {local_id}") from None
        if key not in rows:
            raise ValueError(f"Unknown outlet: {local_id}")
        return rows[key]

    async def _set(self, oid: str, mib_type: str, value: Any) -> Any:
        """Write one OID and return what the agent confirmed.

        A SET answers with the varbinds it applied, so a value the PDU clamps
        (a reboot duration outside 5-60) comes back as the PDU's number rather
        than ours.
        """
        answered = await self.transport.set([(oid, mib_type, value)])
        varbind = answered.get(oid)
        if varbind is None or varbind.is_exception:
            return value
        return varbind.value

    async def set_device_setting(self, key: str, value: Any) -> Any:
        tree = self._require_tree()
        entry = tree.settings.get(key)
        if entry is None:
            raise ValueError(
                f"{key} is not settable on {tree.label}"
            )
        oid, mib_type = entry
        confirmed = await self._set(
            f"{oid}.{tree.suffix}", mib_type, value
        )
        if mib_type == "integer":
            confirmed = int(confirmed)
        else:
            confirmed = str(confirmed)
        self.set_state(key, confirmed)
        return confirmed

    def _require_tree(self) -> Tree:
        if self._tree is None:
            raise ConnectionError(
                "This device has not identified itself as an APC Rack PDU "
                "yet — no PowerNet Rack PDU branch answered."
            )
        return self._tree

    # ── Test Connection ──────────────────────────────────────────────────

    async def run_setup_action(
        self, action_id: str, params: dict[str, Any], progress: Any,
    ) -> dict[str, Any]:
        """Prove the address, the read community and the MIB branch.

        The three things that go wrong adding an SNMP PDU are the community
        string, the write community's access type, and pointing at something
        that is not a Rack PDU — and none announces itself. A wrong community
        is answered with silence, which is indistinguishable from an
        unreachable host.
        """
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")
        try:
            await self._read_system_group()
        except (ConnectionError, asyncio.TimeoutError):
            return {
                "success": False,
                "message": (
                    "No answer. Check the IP address, that SNMPv1 access is "
                    "enabled on the Rack PDU's management card, and that the "
                    "read community string matches — a Rack PDU ignores a "
                    "request whose community it does not recognise, so a "
                    "wrong community looks exactly like an unreachable "
                    "device."
                ),
            }
        except SnmpError as e:
            return {"success": False, "message": str(e)}

        tree = await self._detect_tree()
        if tree is None:
            descr = self.get_state("sys_descr") or "an SNMP device"
            return {
                "success": False,
                "message": (
                    f"Reached {descr}, but it does not answer on either "
                    f"PowerNet Rack PDU branch. This driver needs an APC "
                    f"Rack PDU; for other APC gear use the generic SNMP "
                    f"Device driver and declare its OIDs."
                ),
            }

        self._tree = tree
        self.set_state("mib_branch", tree.key)
        await self._reconcile_children()
        await self._read_scalars()

        model = self.get_state("model") or "an APC Rack PDU"
        serial = self.get_state("serial_number") or ""
        identity = f"{model} ({serial})" if serial else model
        counts = ", ".join(
            f"{len(rows)} {child_type}{'s' if len(rows) != 1 else ''}"
            for child_type, rows in self._rows.items() if rows
        )
        message = f"Connected to {identity} on {tree.label}."
        if counts:
            message += f" Found {counts}."
        if not self._rows.get("outlet"):
            message += (
                " No switchable outlets — this looks like a metered-only "
                "Rack PDU, so load readings work but outlet commands will "
                "not."
            )
        message += (
            " Switching an outlet also needs the write community to have "
            "Write access on the PDU; this test does not write anything."
        )
        return {"success": True, "message": message}

    # ── Liveness ─────────────────────────────────────────────────────────

    async def _liveness_probe(self) -> None:
        """Read sysDescr. Every agent implements it, so this works before the
        branch is known, and on a connectionless transport an awaited read is
        the only way to find out the PDU is gone."""
        await self.transport.get(SYS_DESCR)


def _decode(column: Column, raw: Any) -> Any:
    """Turn one varbind value into what the state variable is declared as."""
    if column.enum is not None:
        try:
            return column.enum.get(int(raw))
        except (TypeError, ValueError):
            return None
    if column.scale == 1.0 and not column.minus_one_is_absent:
        return raw if isinstance(raw, str) else _as_number(raw)
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    if column.minus_one_is_absent and number == -1:
        # The MIB's own convention across the rPDU2 tree: "Models that do not
        # support this feature will respond to this OID with a value of -1."
        return None
    if column.scale == 1.0:
        return number
    return round(number * column.scale, 4)


def _as_number(raw: Any) -> Any:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


DRIVER_CLASS = APCRackPDUDriver
