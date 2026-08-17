"""Byte-exact CLI output captured from an AC-MXNET-SW8P.

Software V705R002C013, captured 2026-08-17. Column layout and spacing are
reproduced exactly as the switch prints them -- that alignment is what the
driver's table parsers key on, so hand-tidying this file would make the tests
stop testing anything.

The unit's own identifiers (serial number, base MAC, and the MAC addresses of
the endpoints attached to it) have been replaced with synthetic values of the
same shape. Nothing else is edited.
"""

# show version
SHOW_VERSION = """\
  AC-MXNET-SW8P Device, Compiled on Mar 11 13:11:45 2025
  sysLocation 2222 E 52ND STREET NORTH, SIOUX FALLS, SD 57104, United States
  CPU Mac 18:8a:6a:00:00:02
  Vlan MAC 18:8a:6a:00:00:01
  SoftWare Package Version V705R002C013
  BootRom Version 7.5.15
  HardWare Version 1.0.2
  CPLD Version 5.00
  Serial No.:SW100126030400001
  COPYRIGHT(C) AVPROGLOBAL HLDGS 2025
  All rights reserved
  Last reboot is cold reset.
  Uptime is 0 weeks, 0 days, 1 hours, 59 minutes
"""

# show interface ethernet status
SHOW_INTERFACE_STATUS = """\
Codes: A-Down - administratively down, E-Down - errdisable down, a - auto, f - force, G - Gigabit

Interface       Link/Protocol  Speed   Duplex  Vlan   Type            Alias Name
1/0/1           UP/UP          a-1G    a-FULL  1      G-TX            
1/0/2           DOWN/DOWN      auto    auto    1      G-TX            
1/0/3           UP/UP          a-1G    a-FULL  1      G-TX            
1/0/4           DOWN/DOWN      auto    auto    1      G-TX            
1/0/5           UP/UP          a-1G    a-FULL  1      G-TX            
1/0/6           DOWN/DOWN      auto    auto    1      G-TX            
1/0/7           DOWN/DOWN      auto    auto    1      G-TX            
1/0/8           DOWN/DOWN      auto    auto    1      G-TX            
1/0/9           DOWN/DOWN      auto    auto    1      SFP+            
1/0/10          DOWN/DOWN      auto    auto    1      SFP+            
1/0/11          DOWN/DOWN      auto    auto    1      SFP+            
1/0/12          DOWN/DOWN      auto    auto    1      SFP+            
"""

# show power inline interface
SHOW_POWER_INLINE_INTERFACE = """\

Interface       Status  Oper   Power(mW) Max-type Max(mW) Current(mA) Volt(V) Priority Class
--------------- ------- ------ --------- -------- ------- ----------- ------- -------- -----
Ethernet1/0/1    enable     on      3300    class   30000          62      52      low     3
Ethernet1/0/2    enable    off         0    class   30000           0       0      low     0
Ethernet1/0/3    enable     on      4200    class   30000          80      52      low     3
Ethernet1/0/4    enable    off         0    class   30000           0       0      low     0
Ethernet1/0/5    enable    off         0    class   30000           0       0      low     0
Ethernet1/0/6    enable    off         0    class   30000           0       0      low     0
Ethernet1/0/7    enable    off         0    class   30000           0       0      low     0
Ethernet1/0/8    enable    off         0    class   30000           0       0      low     0
Power inline is not supported on interface Ethernet1/0/9.
Power inline is not supported on interface Ethernet1/0/10.
Power inline is not supported on interface Ethernet1/0/11.
Power inline is not supported on interface Ethernet1/0/12.
"""

# show power inline
SHOW_POWER_INLINE = """\

Power Inline Status: On
Power Available: 125 W
Power Used: 7 W
Power Remaining: 118 W
Police: Off
Legacy: Off
Disconnect: Dc
Mode: Signal
Pse Type: RTL RSK
SW Version: 0.0.0.3
"""

# show power inline interface ethernet 1/0/3
SHOW_POWER_INLINE_PORT = """\

Interface       Status  Oper   Power(mW) Max-type Max(mW) Current(mA) Volt(V) Priority Class
--------------- ------- ------ --------- -------- ------- ----------- ------- -------- -----
Ethernet1/0/3    enable     on      4100    class   30000          79      52      low     3
"""

# show temperature
SHOW_TEMPERATURE = """\
Temperature: 45C/113F
"""

# show cpu usage
SHOW_CPU_USAGE = """\

Last  5 second CPU IDLE:  80%
Last 30 second CPU IDLE:  82%
Last  5 minute CPU IDLE:  82%
From  running  CPU IDLE:  81%
"""

# show memory usage
SHOW_MEMORY_USAGE = """\

The memory total 256 MB , free 104927232 bytes , usage is 60.91%
"""

# show ip igmp snooping
SHOW_IGMP_SNOOPING = """\
Global igmp snooping status   :Enabled
Igmp snooping is turned on for vlan 1(querier)
"""

# show ip igmp snooping vlan 1
SHOW_IGMP_SNOOPING_VLAN = """\
Igmp snooping information for vlan 1

Igmp snooping L3 multicasting                     :stopped
Igmp snooping L2 general querier                  :Yes(COULD_QUERY)
Igmp snooping query-interval                      :125(s)
Igmp snooping max response time                   :10(s)
Igmp snooping specific-query max response time    :1(s)
Igmp snooping robustness                          :2
Igmp snooping mrouter port keep-alive time        :255(s)
Igmp snooping query-suppression time              :255(s)

IGMP Snooping Connect Group Membership 
Note:*-All Source, (S)- Include Source, [S]-Exclude Source
Groups          Sources             Ports               Exptime  SrcMac              System Level
225.1.0.0       *                   Ethernet1/0/1       00:04:17 18:8A:6A:00:00:12   V2          
                                    Ethernet1/0/3       00:04:00 18:8A:6A:00:00:11   V2          
225.1.0.1       *                   Ethernet1/0/1       00:04:17 18:8A:6A:00:00:12   V2          
                                    Ethernet1/0/3       00:04:00 18:8A:6A:00:00:11   V2          
239.32.4.2      *                   Ethernet1/0/5       00:02:27 AA:BB:CC:00:00:21   V2          
225.2.0.20      *                   Ethernet1/0/1       00:03:38 18:8A:6A:00:00:12   V2          
                                    Ethernet1/0/3       00:04:16 18:8A:6A:00:00:11   V2          
225.3.0.20      *                   Ethernet1/0/1       00:03:38 18:8A:6A:00:00:12   V2          
                                    Ethernet1/0/3       00:04:16 18:8A:6A:00:00:11   V2          
225.4.0.20      *                   Ethernet1/0/1       00:03:38 18:8A:6A:00:00:12   V2          
                                    Ethernet1/0/3       00:04:16 18:8A:6A:00:00:11   V2          
225.7.0.20      *                   Ethernet1/0/1       00:03:38 18:8A:6A:00:00:12   V2          
                                    Ethernet1/0/3       00:04:16 18:8A:6A:00:00:11   V2          
239.255.255.250 *                   Ethernet1/0/5       00:02:22 AA:BB:CC:00:00:21   V2          
IGMP snooping vlan 1 current/limit groups :8/1000

Igmp snooping vlan 1 mrouter port
Note:"!"-static mrouter port
"""

# show mac-address-table
SHOW_MAC_ADDRESS_TABLE = """\
Read mac address table....
Vlan Mac Address                 Type    Creator   Ports
---- --------------------------- ------- -------------------------------------
1    18-8a-6a-00-00-11           DYNAMIC Hardware Ethernet1/0/3
1    18-8a-6a-00-00-12           DYNAMIC Hardware Ethernet1/0/1
1    18-8a-6a-00-00-01           STATIC  System   CPU
1    aa-bb-cc-00-00-21           DYNAMIC Hardware Ethernet1/0/5
"""

# show vlan
SHOW_VLAN = """\
VLAN Name         Type       Media     Ports
---- ------------ ---------- --------- ----------------------------------------
1    default      Static     ENET      Ethernet1/0/1       Ethernet1/0/2      
                                       Ethernet1/0/3       Ethernet1/0/4      
                                       Ethernet1/0/5       Ethernet1/0/6      
                                       Ethernet1/0/7       Ethernet1/0/8      
                                       Ethernet1/0/9       Ethernet1/0/10     
                                       Ethernet1/0/11      Ethernet1/0/12     
"""

# show transceiver
SHOW_TRANSCEIVER = """\
Interface            Temp(C)            Voltage(V)            Bias(mA)            RX Power(dBM)            TX Power(dBM)
---------            --------            ----------            --------            -------------            -------------
"""

# show interface ethernet 1/0/1-3
SHOW_INTERFACE_RANGE = """\
Interface brief:
  Ethernet1/0/1 is up, line protocol is up
  Ethernet1/0/1 is layer 2 port, alias name is (null), index is 1
  Hardware is Gigabit-TX, address is 18-8a-6a-00-00-02
  PVID is 1
  MTU 10218 bytes, BW 1000000 Kbit
  Time since last status change:0w-0d-0h-28m-20s  (1700 seconds)
  Encapsulation ARPA, Loopback not set
  Auto-duplex: Negotiation full-duplex, Auto-speed: Negotiation 1G bits
  FlowControl is off, MDI type is auto
Statistics:
  5 minute input rate 19202 bits/sec, 34 packets/sec
  5 minute output rate 17159 bits/sec, 32 packets/sec
  The last 5 second input rate 20099 bits/sec, 35 packets/sec
  The last 5 second output rate 18003 bits/sec, 34 packets/sec
  Input packets statistics:
    232696 input packets, 16486134 bytes, 0 no buffer
    7 unicast packets, 232665 multicast packets, 24 broadcast packets
    0 input errors, 0 CRC, 0 frame alignment, 0 overrun, 0 ignored,
    0 abort, 0 length error, 0 undersize 0 jabber, 0 fragments, 0 pause frame
  Output packets statistics:
    230897 output packets, 15778554 bytes, 0 underruns
    37 unicast packets, 220931 multicast packets, 9929 broadcast packets
    0 output errors, 0 collisions, 0 late collisions, 0 pause frame

Interface brief:
  Ethernet1/0/2 is down, line protocol is down
  Ethernet1/0/2 is layer 2 port, alias name is (null), index is 2
  Hardware is Gigabit-TX, address is 18-8a-6a-00-00-02
  PVID is 1
  MTU 10218 bytes, BW 10000 Kbit
  Time since last status change:0w-0d-2h-0m-2s  (7202 seconds)
  Encapsulation ARPA, Loopback not set
  Auto-duplex, Auto-speed
  FlowControl is off, MDI type is auto
Statistics:
  5 minute input rate 0 bits/sec, 0 packets/sec
  5 minute output rate 0 bits/sec, 0 packets/sec
  The last 5 second input rate 0 bits/sec, 0 packets/sec
  The last 5 second output rate 0 bits/sec, 0 packets/sec
  Input packets statistics:
    0 input packets, 0 bytes, 0 no buffer
    0 unicast packets, 0 multicast packets, 0 broadcast packets
    0 input errors, 0 CRC, 0 frame alignment, 0 overrun, 0 ignored,
    0 abort, 0 length error, 0 undersize 0 jabber, 0 fragments, 0 pause frame
  Output packets statistics:
    0 output packets, 0 bytes, 0 underruns
    0 unicast packets, 0 multicast packets, 0 broadcast packets
    0 output errors, 0 collisions, 0 late collisions, 0 pause frame

Interface brief:
  Ethernet1/0/3 is up, line protocol is up
  Ethernet1/0/3 is layer 2 port, alias name is (null), index is 3
  Hardware is Gigabit-TX, address is 18-8a-6a-00-00-02
  PVID is 1
  MTU 10218 bytes, BW 1000000 Kbit
  Time since last status change:0w-0d-0h-32m-36s  (1956 seconds)
  Encapsulation ARPA, Loopback not set
  Auto-duplex: Negotiation full-duplex, Auto-speed: Negotiation 1G bits
  FlowControl is off, MDI type is auto
Statistics:
  5 minute input rate 16951 bits/sec, 32 packets/sec
  5 minute output rate 19419 bits/sec, 34 packets/sec
  The last 5 second input rate 17680 bits/sec, 33 packets/sec
  The last 5 second output rate 20422 bits/sec, 36 packets/sec
  Input packets statistics:
    218798 input packets, 14473130 bytes, 0 no buffer
    32 unicast packets, 218741 multicast packets, 25 broadcast packets
    0 input errors, 0 CRC, 0 frame alignment, 0 overrun, 0 ignored,
    0 abort, 0 length error, 0 undersize 0 jabber, 0 fragments, 0 pause frame
  Output packets statistics:
    244324 output packets, 17746432 bytes, 0 underruns
    12 unicast packets, 234381 multicast packets, 9931 broadcast packets
    0 output errors, 0 collisions, 0 late collisions, 0 pause frame
"""
