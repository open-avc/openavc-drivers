"""Byte-exact Darwin Control telnet captures (Controller(h) FW 1.50.02).

Captured from real hardware (one TX + one RX enrolled, both ID 001). These are
the ground truth for the darwin_control driver's parser tests and for the
darwin_control simulator, whose banners must match byte-for-byte.

`RAW_*` values are exactly what arrives on the wire after Telnet IAC stripping
(command echo + response + trailing "CONTROLLER> " prompt). `BANNER_*` values
are the response body the driver's parser receives (echo + prompt removed,
CRLF normalised to LF, leading/trailing newlines stripped) — i.e. the exact
output of DarwinControlDriver._strip_echo, so the simulator's framed output can
be asserted against them directly.

Note the firmware brands every banner header "Controller(h)" (not "DARWIN
CONTROL", which the FW 2.03.19 manual shows). Parsers must key off row layout,
never the brand string.
"""

# Connect banner, after IAC negotiation (FF FB 03 / FF FB 01 / FF FE 01 /
# FF FD 00) is stripped, through the first prompt.
RAW_GREETING = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To Controller(h) Terminal Control System\r\n"
    "FW Version: 1.50.02\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
    "CONTROLLER> "
)

# GET STATUS — no encoders/decoders enrolled (In/Out NONE).
BANNER_STATUS_EMPTY = (
    "================================================================\n"
    "              Controller(h) Status Info\n"
    "              FW Version: 1.50.02\n"
    "\n"
    "Power\tIR\tBaud\n"
    "On \tOn \t57600 \n"
    "\n"
    "In      EDID    IP               NET/Sig  AudioFormat\n"
    "NONE\n"
    "\n"
    "Out     FromIn  IP               NET/HDMI  Res  Mode  Osp\n"
    "NONE\n"
    "\n"
    "LAN     DHCP    IP              Gateway         SubnetMask\n"
    "01_POE  Off     169.254.008.100 169.254.008.001 255.255.000.000\n"
    "02_CTRL On      192.168.004.033 192.168.006.001 255.255.252.000\n"
    "        (static:192.168.006.100 192.168.006.001 255.255.255.000)\n"
    "\n"
    "Telnet  SSH2    HTTPS  WEB  LAN01 MAC               LAN02 MAC\n"
    "00023   Off     Off    On   18:66:96:11:11:48       18:66:96:11:11:49\n"
    "\n"
    "Domain Name\n"
    "Controller.local\n"
    "================================================================"
)

# GET STATUS — one encoder (ID 001) + one decoder (ID 001) enrolled.
BANNER_STATUS_POPULATED = (
    "================================================================\n"
    "              Controller(h) Status Info\n"
    "              FW Version: 1.50.02\n"
    "\n"
    "Power\tIR\tBaud\n"
    "On \tOn \t57600 \n"
    "\n"
    "In      EDID    IP               NET/Sig  AudioFormat\n"
    "001     DF000   169.254.010.001  On /Off   PCM\n"
    "\n"
    "Out     FromIn  IP               NET/HDMI  Res  Mode  Osp\n"
    "001     001     169.254.020.001  On /Off   01   MX    SNK\n"
    "\n"
    "LAN     DHCP    IP              Gateway         SubnetMask\n"
    "01_POE  Off     169.254.008.100 169.254.008.001 255.255.000.000\n"
    "02_CTRL On      192.168.004.033 192.168.006.001 255.255.252.000\n"
    "        (static:192.168.006.100 192.168.006.001 255.255.255.000)\n"
    "\n"
    "Telnet  SSH2    HTTPS  WEB  LAN01 MAC               LAN02 MAC\n"
    "00023   Off     Off    On   18:66:96:11:11:48       18:66:96:11:11:49\n"
    "\n"
    "Domain Name\n"
    "Controller.local\n"
    "================================================================"
)

# GET ENC 1 STATUS — enrolled TX (endpoint FW 3.12.02).
BANNER_ENC = (
    "================================================================\n"
    "              IP Control Box Controller(h) Encoder Info\n"
    "              FW Version: 1.50.02\n"
    "\n"
    "In    Net    Sig   Ver     EDID   Aud   MCast   Name\n"
    "001   On     Off   3.12.02 DF000  HDMI  On      Encoder 001\n"
    "    >>LED    SGEn/Br/Bit\n"
    "      9      Off /9/8n1\n"
    "    >>MAC\n"
    "      18:66:96:11:0D:EB\n"
    "    >>IP               GW               SM\n"
    "      169.254.010.001  169.254.008.001  255.255.000.000\n"
    "================================================================"
)

# GET ENC STATUS — nothing enrolled (header only).
BANNER_ENC_EMPTY = (
    "================================================================\n"
    "              IP Control Box Controller(h) Encoder Info\n"
    "              FW Version: 1.50.02\n"
    "================================================================"
)

# GET ENC 1 STATUS — absent id.
BANNER_ENC_ABSENT = "[ERROR]Encoder 001 not exist."

# GET DEC 1 STATUS — enrolled RX (endpoint FW 3.12.01).
BANNER_DEC = (
    "================================================================\n"
    "              IP Control Box Controller(h) Decoder Info\n"
    "              FW Version: 1.50.02\n"
    "\n"
    "Out   Net    HPD   Ver     Mode   Res   Rotate  Name\n"
    "001   On     Off   3.12.01 MX     01    0       Decoder 001\n"
    "    >>Fr     Vid/IR_/Ser/USB      MCast\n"
    "      001    000/000/000/000      On \n"
    "    >>ASPECT    OSP    IR    BTN     LED    SGEn/Br/Bit\n"
    "      Maintain  SNK    On    On      9      Off /9/8n1\n"
    "    >>Video  Mute  Pause   Auto   VideoLostTimeout\n"
    "      On     Off   Off     On     0\n"
    "    >>MAC\n"
    "      18:66:96:11:0D:1D\n"
    "    >>IP               GW               SM\n"
    "      169.254.020.001  169.254.008.001  255.255.000.000\n"
    "================================================================"
)

# GET GPIO STATUS — all four ports input.
BANNER_GPIO = (
    "================================================================\n"
    "              Controller(h) GPIO Info\n"
    "              FW Version: 1.50.02\n"
    "\n"
    "GPIO   DIR    Set   Get\n"
    "01     In     -     1\n"
    "02     In     -     1\n"
    "03     In     -     1\n"
    "04     In     -     1\n"
    "================================================================"
)

# GET WALL STATUS — no walls.
BANNER_WALL_EMPTY = (
    "================================================================\n"
    "              Controller(h) Video Wall Info\n"
    "              FW Version: 1.50.02\n"
    "\n"
    "No Video Wall\n"
    "================================================================"
)

# SEARCH — one new encoder + one new decoder on the Video LAN (endpoint OUI
# 18:66:96, not the ASPEED 6C:DF:FB the manual shows; header column is
# "Assign", not "ID"; MAC reported lowercase in this banner).
BANNER_SEARCH = (
    "[SUCCESS]More device in network will take more time to finish scan, "
    "please wait...done.\n"
    "================================================================\n"
    "              Scan Device Result Info\n"
    "\n"
    "==New Encoder\n"
    "Index   IP               MAC                Assign\n"
    "001     169.254.236.013  18:66:96:11:0d:eb  No\n"
    "\n"
    "==New Decoder\n"
    "Index   IP               MAC                Assign\n"
    "001     169.254.030.013  18:66:96:11:0d:1d  No\n"
    "================================================================"
)
