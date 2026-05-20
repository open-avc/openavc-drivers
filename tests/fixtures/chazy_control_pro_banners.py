"""Byte-exact Chazy Control Pro telnet captures (TAV-CHAZY-CLTPRO FW 1.10.11).

Captured from real hardware (one TX + one RX added, both ID 001). These are
the ground truth for the chazy_control_pro driver's parser tests and for the
chazy_control_pro simulator (D2), whose banners must match byte-for-byte.

`RAW_*` values are exactly what arrives on the wire after Telnet IAC stripping
(command echo + response + trailing "CONTROLLER> " prompt). `BANNER_*` values
are the response body the driver's parser receives (echo + prompt removed,
CRLF normalised to LF) — i.e. the output of ChazyControlProDriver._strip_echo.
"""

# Connect banner (after IAC negotiation 0xFF... is stripped), through the
# first prompt.
RAW_GREETING = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To TAV-CHAZY-CLTPRO Terminal Control System\r\n"
    "FW Version: 1.10.11\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
    "CONTROLLER> "
)

# GET STATUS — no encoders/decoders configured.
BANNER_STATUS_EMPTY = (
    "================================================================\n"
    "              TAV-CHAZY-CLTPRO Status Info\n"
    "              FW Version: 1.10.11\n"
    "\n"
    "Power\tIR\tBaud\n"
    "On \tOn \t57600 \n"
    "\n"
    "ENC     Type                EDID    IP               NET/Sig\n"
    "NONE\n"
    "\n"
    "DEC     Type                From    IP               NET/HDMI  Res  Mode\n"
    "NONE\n"
    "\n"
    "LAN     DHCP    IP              Gateway         SubnetMask\n"
    "01_POE  Off     169.254.008.100 169.254.008.001 255.255.000.000\n"
    "02_CTRL On      192.168.004.188 192.168.004.001 255.255.252.000\n"
    "        (static:192.168.006.100 192.168.006.001 255.255.255.000)\n"
    "\n"
    "Telnet  SSH     HTTPS   LAN01 MAC           LAN02 MAC\n"
    "0023    Off     Off     18:66:96:11:11:E0   18:66:96:11:11:E1\n"
    "\n"
    "DNS     Mode    LAN     Preferred           Alternate\n"
    "        Auto    02_CTRL 192.168.4.1         160.40.198.190\n"
    "\n"
    "Domain Name\n"
    "controller.local\n"
    "================================================================"
)

# GET STATUS — one encoder (001) + one decoder (001) added, both offline
# (Net Off) right after add. Note the blank Type column.
BANNER_STATUS_POPULATED = (
    "================================================================\n"
    "              TAV-CHAZY-CLTPRO Status Info\n"
    "              FW Version: 1.10.11\n"
    "\n"
    "Power\tIR\tBaud\n"
    "On \tOn \t57600 \n"
    "\n"
    "ENC     Type                EDID    IP               NET/Sig\n"
    "001                         DF000   169.254.010.001  Off/Off\n"
    "\n"
    "DEC     Type                From    IP               NET/HDMI  Res  Mode\n"
    "001                         001     169.254.020.001  Off/Off   02   MX\n"
    "\n"
    "LAN     DHCP    IP              Gateway         SubnetMask\n"
    "01_POE  Off     169.254.008.100 169.254.008.001 255.255.000.000\n"
    "02_CTRL On      192.168.004.188 192.168.004.001 255.255.252.000\n"
    "        (static:192.168.006.100 192.168.006.001 255.255.255.000)\n"
    "\n"
    "Telnet  SSH     HTTPS   LAN01 MAC           LAN02 MAC\n"
    "0023    Off     Off     18:66:96:11:11:E0   18:66:96:11:11:E1\n"
    "\n"
    "DNS     Mode    LAN     Preferred           Alternate\n"
    "        Auto    02_CTRL 192.168.4.1         160.40.198.190\n"
    "\n"
    "Domain Name\n"
    "controller.local\n"
    "================================================================"
)

# GET ENC 1 STATUS — encoder 001, offline (Net Off), no name set.
BANNER_ENC_DETAIL = (
    "================================================================\n"
    "              TAV-CHAZY-CLTPRO Encoder Info\n"
    "              FW Version: 1.10.11\n"
    "\n"
    "ID      Type                Net    Sig   Ver      EDID   Aud   MCast   Name\n"
    "001                         Off    Off            DF000  HDMI  On      \n"
    "      >>Fix    Arc \n"
    "             NA\n"
    "      >>Sel    Arc \n"
    "               NA\n"
    "      >>SAC    SGEn/Br/Bit\n"
    "        ARC    Off /9 /8n1\n"
    "      >>Pin    IOVOL/IODIR/IODAT IRVOL RLY   PHY\n"
    "               NA\n"
    "      >>IM     MAC\n"
    "        Static 18:66:96:11:0A:27\n"
    "      >>IP               GW               SM\n"
    "        169.254.010.001  169.254.001.001  255.255.000.000\n"
    "================================================================"
)

# GET DEC 1 STATUS — decoder 001, offline, all signals resolved from enc 001.
BANNER_DEC_DETAIL = (
    "================================================================\n"
    "              TAV-CHAZY-CLTPRO Decoder Info\n"
    "              FW Version: 1.10.11\n"
    "\n"
    "ID      Type                Net    HPD   Ver      Mode   Res   Rotate  Name\n"
    "001                         Off    Off            MX     02    0       \n"
    "      >>Fix    Vid      /Aud     /IR      /Ser     /USB     /CEC     MCast Video Mute\n"
    "               000      /000     /000     /000     /000     /000     On    On    Off\n"
    "      >>Sel    Vid      /Aud     /IR      /Ser     /USB     /CEC\n"
    "               001      /001     /001     /001     /001     /001     \n"
    "      >>SAC    OSP   SGEn/Br/Bit\n"
    "        ARC    4     Off /9 /8n1\n"
    "      >>Pin    IOVOL/IODIR/IODAT IRVOL RLY   PHY\n"
    "               NA\n"
    "      >>IM     MAC\n"
    "        Static 18:66:96:11:08:C0\n"
    "      >>IP               GW               SM\n"
    "        169.254.020.001  169.254.001.001  255.255.000.000\n"
    "================================================================"
)

# GET ENC 1 STATUS for a non-existent encoder.
BANNER_ENC_NOT_FOUND = "[ERROR]Encoder 002 does not exist."

# GET GPIO 0 STATUS — all four controller GPIO ports.
BANNER_GPIO = (
    "================================================================\n"
    "              TAV-CHAZY-CLTPRO GPIO Info\n"
    "              FW Version: 1.10.11\n"
    "\n"
    "GPIO   DIR    Set   Get\n"
    "01     In     -     1\n"
    "02     In     -     1\n"
    "03     In     -     1\n"
    "04     In     -     1\n"
    "================================================================"
)

# SEARCH / GET SEARCH STATUS — one new TX + one new RX found, not yet added.
BANNER_SEARCH_FOUND = (
    "================================================================\n"
    "              Search Device Result Info\n"
    "\n"
    "==New Encoder\n"
    "Index   IP               MAC                Assign\n"
    "001     169.254.002.177  18:66:96:11:0A:27  ID:000\n"
    "\n"
    "==New Decoder\n"
    "Index   IP               MAC                Assign\n"
    "001     169.254.005.166  18:66:96:11:08:C0  ID:000\n"
    "================================================================"
)

# SEARCH with nothing new on the network.
BANNER_SEARCH_EMPTY = (
    "================================================================\n"
    "              Search Device Result Info\n"
    "\n"
    "==New Encoder\n"
    "None\n"
    "\n"
    "==New Decoder\n"
    "None\n"
    "================================================================"
)

# Single-line acks.
LINE_GET_DATE = "[SUCCESS]2026-05-20 12:17:01 (Australia/Sydney)."
LINE_GET_NTP = "[SUCCESS]time.nist.gov."
LINE_ADD_AUTO_ENC = "[SUCCESS]Add scan index 001 device to encoder 001."
LINE_ADD_AUTO_DEC = "[SUCCESS]Add scan index 001 device to decoder 001."
LINE_ERR_NOT_FOUND = "[ERROR]Decoder 001 does not exist."
LINE_ERR_CMD = "[ERROR]Command not found."

# Factory-reset confirm flow (captured by answering 'No' to cancel safely).
LINE_RESET_QUESTION = (
    'Sure to RESET system to default settings? Type "Yes" after next prompt to confirm...'
)
LINE_RESET_CANCELLED = "[SUCCESS]RESET process ignored."

# ── Online captures (TX + RX linked: Net On, Type=model, Ver populated, Pin
#    rows present, plus the >>LAN2 sub-block that only appears when online) ──

BANNER_STATUS_ONLINE = (
    "================================================================\n"
    "              TAV-CHAZY-CLTPRO Status Info\n"
    "              FW Version: 1.10.11\n"
    "\n"
    "Power\tIR\tBaud\n"
    "On \tOn \t57600 \n"
    "\n"
    "ENC     Type                EDID    IP               NET/Sig\n"
    "001     TAV-CHAZY4K-TX      DF000   169.254.010.001  On /Off\n"
    "\n"
    "DEC     Type                From    IP               NET/HDMI  Res  Mode\n"
    "001     TAV-CHAZY4K-RX      001     169.254.020.001  On /Off   02   MX\n"
    "\n"
    "LAN     DHCP    IP              Gateway         SubnetMask\n"
    "01_POE  Off     169.254.008.100 169.254.008.001 255.255.000.000\n"
    "02_CTRL On      192.168.004.188 192.168.004.001 255.255.252.000\n"
    "        (static:192.168.006.100 192.168.006.001 255.255.255.000)\n"
    "\n"
    "Telnet  SSH     HTTPS   LAN01 MAC           LAN02 MAC\n"
    "0023    Off     Off     18:66:96:11:11:E0   18:66:96:11:11:E1\n"
    "\n"
    "DNS     Mode    LAN     Preferred           Alternate\n"
    "        Auto    02_CTRL 192.168.4.1         160.40.198.190\n"
    "\n"
    "Domain Name\n"
    "controller.local\n"
    "================================================================"
)

BANNER_ENC_DETAIL_ONLINE = (
    "================================================================\n"
    "              TAV-CHAZY-CLTPRO Encoder Info\n"
    "              FW Version: 1.10.11\n"
    "\n"
    "ID      Type                Net    Sig   Ver      EDID   Aud   MCast   Name\n"
    "001     TAV-CHAZY4K-TX      On     Off   1.10.03  DF000  HDMI  On      Encoder 001\n"
    "      >>Fix    Arc \n"
    "               000 \n"
    "      >>Sel    Arc \n"
    "               000 \n"
    "      >>SAC    SGEn/Br/Bit\n"
    "        ARC    Off /9 /8n1\n"
    "      >>Pin    IOVOL/IODIR/IODAT IRVOL RLY   PHY\n"
    "        (1)    12    Out   0     12    Open  Copper\n"
    "        (2)    12    Out   0           Open\n"
    "      >>IM     MAC\n"
    "        Static 18:66:96:11:0A:27\n"
    "      >>IP               GW               SM\n"
    "        169.254.010.001  169.254.001.001  255.255.000.000\n"
    "      >>LAN2 Mode  IM\n"
    "             1     DHCP  \n"
    "================================================================"
)

BANNER_DEC_DETAIL_ONLINE = (
    "================================================================\n"
    "              TAV-CHAZY-CLTPRO Decoder Info\n"
    "              FW Version: 1.10.11\n"
    "\n"
    "ID      Type                Net    HPD   Ver      Mode   Res   Rotate  Name\n"
    "001     TAV-CHAZY4K-RX      On     Off   1.10.03  MX     02    0       Decoder 001\n"
    "      >>Fix    Vid      /Aud     /IR      /Ser     /USB     /CEC     MCast Video Mute\n"
    "               000      /000     /000     /000     /000     /000     On    On    Off\n"
    "      >>Sel    Vid      /Aud     /IR      /Ser     /USB     /CEC\n"
    "               001      /001     /001     /001     /001     /001     \n"
    "      >>SAC    OSP   SGEn/Br/Bit\n"
    "        ARC    4     Off /9 /8n1\n"
    "      >>Pin    IOVOL/IODIR/IODAT IRVOL RLY   PHY\n"
    "        (1)    12    Out   0     12    Open  Copper\n"
    "        (2)    12    Out   0           Open\n"
    "      >>IM     MAC\n"
    "        Static 18:66:96:11:08:C0\n"
    "      >>IP               GW               SM\n"
    "        169.254.020.001  169.254.001.001  255.255.000.000\n"
    "      >>LAN2 Mode  IM\n"
    "             1     DHCP  \n"
    "================================================================"
)
