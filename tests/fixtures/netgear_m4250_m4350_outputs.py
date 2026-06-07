"""Sample CLI outputs for the NETGEAR M4250/M4350 driver tests.

Dotted key/value outputs are byte-exact from the M4250/M4350 CLI Reference
Manual examples. Column tables are built with ``_tbl`` so the dashed separator
and the data columns are guaranteed to line up the way a real switch emits them
(fixed-width, single-space gaps). The driver parses column positions from the
separator, so this exercises the real parsing path. M4350 ``unit/slot/port``
naming is used throughout.
"""

from __future__ import annotations


def _tbl(cols: list[tuple[str, int]], rows: list[tuple]) -> str:
    """Render an aligned column table: header, dashed separator, data rows."""
    header = " ".join(name.ljust(w) for name, w in cols)
    sep = " ".join("-" * w for _, w in cols)
    out = [header, sep]
    for row in rows:
        out.append(" ".join(str(v).ljust(w) for (_, w), v in zip(cols, row)))
    return "\n".join(out) + "\n"


SHOW_VERSION = """\
System Description............................. M4350-24X4F ProAV Switch
Machine Model.................................. M4350-24X4F
Serial Number.................................. 7AB12C3D4E5F6
Burned In MAC Address.......................... BC:A5:11:22:33:44
Software Version............................... 14.0.6.17
Boot Code Version.............................. B1.0.0.6
System Name.................................... AV-CORE-SW1
System Up Time................................. 12 days 4 hrs 9 mins 51 secs
"""

SHOW_POE = """\
Firmware Version............................... 1.2.0.8
PSE Main Operational Status.................... ON
Total Power Available.......................... 720.0 Watts
Threshold Power................................ 648.0 Watts
Total Power Consumed........................... 62.5 Watts
Usage Threshold................................ 90
Power Management Mode.......................... Dynamic
Traps.......................................... Enable
"""

SHOW_ENVIRONMENT = """\
Fan Control Mode............................... Quiet
Temp (C)....................................... 41
Temperature traps range: 0 to 90 degrees (Celsius)

Temperature Sensors:

Unit Sensor Description      Temp (C) State    Max_Temp (C)
---- ------ ---------------- -------- -------- ------------
1    1      sensor-1         41       Normal   53

Fans:

Unit Fan Description    Type  Speed Duty       State
---- --- -------------- ----- ----- ---------- -----------
1    1   FAN-1          Fixed 3200  30%        Operational
1    2   FAN-2          Fixed 3100  28%        Operational

Power Modules:

Unit Power Description Type  State
---- ----- ----------- ----- -----------
1    1     PS-1        Fixed Operational
"""

SHOW_PROCESS_CPU = """\
Memory Utilization Report

status     bytes
------ ----------
free   268435456
alloc  805306368

CPU Utilization:

PID  Name               5 Secs 60 Secs 300 Secs
----------------------------------------------------
765  _interrupt_thread  0.00%  0.01%   0.02%
908  RMONTask           0.00%  0.11%   0.12%
----------------------------------------------------
Total CPU Utilization   3.20%  2.80%   2.50%
"""

SHOW_POE_PORT_INFO = _tbl(
    [("Intf", 8), ("High", 8), ("Max", 8), ("Class", 8), ("Power", 8),
     ("Current", 8), ("Voltage", 8), ("Status", 17), ("Fault", 11)],
    [
        ("1/0/1", "Yes", "32000", "Unknown", "0", "0", "0", "Searching", "No Error"),
        ("1/0/2", "Yes", "32000", "4", "4000", "74", "54", "Delivering Power", "No Error"),
        ("1/0/3", "Yes", "32000", "3", "3000", "56", "53", "Delivering Power", "No Error"),
        ("1/0/48", "No", "0", "Unknown", "0", "0", "0", "Disabled", "No Error"),
    ],
)

SHOW_POE_PORT_CONFIG = _tbl(
    [("Intf", 8), ("Admin", 8), ("Priority", 9), ("Limit", 8), ("LimitType", 14),
     ("HighPower", 10), ("Detection", 14), ("Timer", 9)],
    [
        ("1/0/1", "Enable", "Low", "N/A", "Class Based", "Dot3bt", "4Pt-Dot3af", "None"),
        ("1/0/2", "Enable", "High", "N/A", "Class Based", "Dot3bt", "4Pt-Dot3af", "None"),
        ("1/0/48", "Disable", "Low", "N/A", "Class Based", "Dot3bt", "4Pt-Dot3af", "None"),
    ],
)

SHOW_PORT_ALL = _tbl(
    [("Intf", 8), ("Type", 8), ("Admin", 8), ("PhysMode", 12), ("PhysStatus", 12),
     ("Link", 8), ("LinkTrap", 8), ("LACP", 8), ("Actor", 9)],
    [
        ("1/0/1", "", "Enable", "Auto", "1000 Full", "Up", "Enable", "Enable", "long"),
        ("1/0/2", "", "Enable", "Auto", "1000 Full", "Up", "Enable", "Enable", "long"),
        ("1/0/3", "", "Enable", "Auto", "", "Down", "Enable", "Enable", "long"),
        ("1/0/48", "", "Disable", "Auto", "", "Down", "Enable", "Enable", "long"),
        ("1/0/49", "", "Enable", "Auto", "10G Full", "Up", "Enable", "Enable", "long"),
    ],
)

SHOW_INTERFACES_STATUS = _tbl(
    [("Port", 8), ("Name", 14), ("Link", 8), ("PhysMode", 10), ("PhysStatus", 12),
     ("Media", 10), ("Flow", 10), ("VLAN", 6)],
    [
        ("1/0/1", "Camera 1", "Up", "Auto", "1000 Full", "RJ45", "Inactive", "10"),
        ("1/0/2", "Display Left", "Up", "Auto", "1000 Full", "RJ45", "Inactive", "20"),
        ("1/0/49", "Uplink", "Up", "Auto", "10G Full", "SFP+", "Inactive", "1"),
    ],
)

SHOW_IGMPSNOOPING = """\
Admin Mode..................................... Enabled
Multicast Control Frame Count.................. 142
Interfaces Enabled for IGMP Snooping.......... 1/0/1-1/0/48
VLANs enabled for IGMP snooping................ 10,20,30
"""

SHOW_IGMPSNOOPING_QUERIER = """\
Admin Mode..................................... Enabled
Admin Version.................................. 2
Querier Address................................ 10.20.0.1
Query Interval (secs).......................... 60
Querier Timeout (secs)......................... 120
"""

SHOW_IGMPSNOOPING_GROUP = _tbl(
    [("VLAN", 8), ("Subscriber", 31), ("MCGroup", 30), ("Interface", 10),
     ("Type", 7), ("Timeout", 13)],
    [
        ("10", "10.20.1.6/00:00:00:00:00:06", "224.1.1.6/01:00:5E:01:01:06",
         "1/0/16", "IGMPv2", "252"),
        ("20", "10.20.2.7/00:00:00:00:00:07", "239.1.1.7/01:00:5E:01:01:07",
         "1/0/2", "IGMPv2", "240"),
    ],
)

SHOW_LLDP_REMOTE = _tbl(
    [("Intf", 9), ("RemID", 7), ("ChassisID", 20), ("PortID", 20), ("Name", 20)],
    [
        ("1/0/2", "3", "B8:27:EB:11:22:33", "eth0", "Display-Left"),
        ("1/0/7", "2", "00:FC:E3:90:01:0F", "00:FC:E3:90:01:11", "Conf Room Cam"),
    ],
)

SHOW_VLAN = _tbl(
    [("VLANID", 8), ("Name", 20), ("Type", 12), ("Ports", 20)],
    [
        ("1", "default", "Default", ""),
        ("10", "Cameras", "Static", ""),
        ("20", "Displays", "Static", ""),
        ("30", "Dante", "Static", ""),
    ],
)

SHOW_POWER_REDUNDANCY = """\
N+1 configuration: ............................ Enable
N+1 Active: ................................... Yes
Number of PSU: ................................ 2
Effective Number of PSU: ...................... 1
"""

SHOW_RGB_LED = _tbl(
    [("Intf", 9), ("Color", 10), ("Profile", 14)],
    [
        ("1/0/1", "Blue", "Dante"),
        ("1/0/2", "Green", "NDI"),
    ],
)

CABLESTATUS = """\
Cable Status................................... Normal
Cable Length................................... 20m - 25m
"""
