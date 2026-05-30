"""
TurtleAV Chazy Control (standard, non-Pro) — Simulator.

Stateful TCP simulator for the chazy_control driver. The standard Control's
telnet API is a documented strict subset of the Control Pro, so this simulator
reuses the Control Pro simulator's hardware-validated rendering format (banner
column layout, Telnet IAC + ``CONTROLLER> `` prompt framing, blank-when-offline
columns — all proven byte-exact against real Control Pro hardware) with two
deltas that define the standard Control:

  - **Identity.** The welcome and status banners identify as ``CHAZY CONTROL``
    (FW 1.00.17, per the Chazy Control API reference) rather than the Pro's
    ``TAV-CHAZY-CLTPRO``.
  - **Pro-only modules rejected.** Media Player, decoder Groups, Events,
    Scheduler, Configuration Presets, Dante Presets and Date/Time/NTP commands
    return ``[ERROR]Command not found.`` — as a standard Control does, since
    those subsystems do not exist on it.

There is no standalone Control hardware to capture from, so (unlike the Pro
simulator) these banners are not asserted byte-exact against a hardware fixture.
Instead the sim tests prove the simulator and the driver are exact duals: every
rendered banner round-trips cleanly through the driver's own parsers.

Seeded by default with one encoder (001, a TAV-CHAZY4K-TX) and one decoder
(001, a TAV-CHAZY4K-RX), both online. Inject the ``endpoints_offline`` error
mode to reproduce the just-added (Net Off, blank-column) state.

License: MIT.
"""

from __future__ import annotations

from simulator.tcp_simulator import TCPSimulator

# End-of-response marker re-printed after the connect banner and every reply.
PROMPT = b"CONTROLLER> "

# Telnet IAC option negotiation sent at connect (standard server-echo
# negotiation; the driver strips all IAC regardless).
_IAC = 0xFF
_IAC_GREETING = bytes([
    _IAC, 0xFB, 0x03,   # IAC WILL SGA (suppress go-ahead)
    _IAC, 0xFB, 0x01,   # IAC WILL ECHO
    _IAC, 0xFD, 0x00,   # IAC DO BINARY
])

# Connect banner (post-IAC). Standard Control identity.
_GREETING_BODY = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To CHAZY CONTROL Terminal Control System\r\n"
    "FW Version: 1.00.17\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
)

_SENTINEL = "=" * 64

# Fixed system blocks of GET STATUS (controller network/config; zero-padded on
# the wire). Column layout reused verbatim from the hardware-validated Control
# Pro simulator; identity line reads CHAZY CONTROL.
_STATUS_HEAD = [
    _SENTINEL,
    "              CHAZY CONTROL Status Info",
    "              FW Version: 1.00.17",
    "",
    "Power\tIR\tBaud",
    "On \tOn \t57600 ",
    "",
]
_STATUS_ENC_HEADER = "ENC     Type                EDID    IP               NET/Sig"
_STATUS_DEC_HEADER = (
    "DEC     Type                From    IP               NET/HDMI  Res  Mode"
)
_STATUS_TAIL = [
    "",
    "LAN     DHCP    IP              Gateway         SubnetMask",
    "01_POE  Off     169.254.008.100 169.254.008.001 255.255.000.000",
    "02_CTRL On      192.168.006.100 192.168.006.001 255.255.255.000",
    "        (static:192.168.006.100 192.168.006.001 255.255.255.000)",
    "",
    "Telnet  SSH     HTTPS   LAN01 MAC           LAN02 MAC",
    "0023    Off     Off     6C:DF:FB:00:01:2D   6C:DF:FB:00:01:21",
    "",
    # FW 1.00.17 §2.2 GET STATUS has no DNS-server block, only Domain Name.
    "Domain Name",
    "controller.local",
    _SENTINEL,
]

# Single-line acks.
_LINE_ERR_CMD = "[ERROR]Command not found."
_LINE_RESET_QUESTION = (
    'Sure to RESET system to default settings? Type "Yes" after next prompt to confirm...'
)
_LINE_RESET_CANCELLED = "[SUCCESS]RESET process ignored."

# Pro-only command prefixes the standard Control does not implement. A command
# whose uppercased text starts with any of these is rejected with
# [ERROR]Command not found, exactly as a standard Control would. "SET DANTE
# PRESET" is listed (Pro-only) while "SET DANTE DEV ..." routing is not, so
# Dante routing still works.
_PRO_ONLY_PREFIXES = (
    "GET DATE", "GET NTP", "SET DATE", "SET NTP",
    "ADD MEDIA", "SET MEDIA", "DELETE MEDIA",
    "CREATE GROUP", "SET GROUP", "ADD GROUP", "DELETE GROUP",
    "CREATE EVENT", "SET EVENT", "DELETE EVENT",
    "CREATE SCHEDULE", "SET SCHEDULE", "DELETE SCHEDULE",
    "SAVE CONFIG PRESET", "DELETE CONFIG PRESET", "APPLY CONFIG PRESET",
    "CREATE DANTE PRESET", "DELETE DANTE PRESET", "SET DANTE PRESET",
    "APPLY DANTE PRESET",
)


def _line(*parts: tuple[int, str], width: int | None = None) -> str:
    """Build a fixed-column line by placing (start_index, text) pairs into a
    space buffer. Without ``width`` the line ends at the last placed char (no
    trailing pad); with ``width`` it is padded/truncated to that exact length.
    """
    end = max((i + len(t) for i, t in parts), default=0)
    size = end if width is None else width
    buf = [" "] * size
    for i, t in parts:
        for k, ch in enumerate(t):
            if 0 <= i + k < size:
                buf[i + k] = ch
    return "".join(buf)


def _io_default() -> dict:
    return {"iovol": 12, "iodir": "Out", "iodat": 0, "irvol": 12,
            "relay": "Open", "phy": "Copper"}


def _io2_default() -> dict:
    return {"iovol": 12, "iodir": "Out", "iodat": 0, "relay": "Open"}


def _enc_mac(eid: int) -> str:
    return "18:66:96:11:0A:27" if eid == 1 else f"18:66:96:11:0A:{eid % 256:02X}"


def _dec_mac(did: int) -> str:
    return "18:66:96:11:08:C0" if did == 1 else f"18:66:96:11:08:{did % 256:02X}"


class ChazyControlSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "chazy_control",
        "name": "TurtleAV Chazy Control Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 23,
        # Drivers send CRLF-terminated command lines; respond with CRLF too.
        "delimiter": "\r\n",
        "initial_state": {
            "firmware": "1.00.17",
            "power": True,
            "ir": True,
            "rs232_baud": "57600",
            "encoder_count": 1,
            "decoder_count": 1,
            "lan1_dhcp": False,
            "lan1_ip": "169.254.8.100",
            "lan1_gateway": "169.254.8.1",
            "lan1_subnet_mask": "255.255.0.0",
            "lan1_mac": "6C:DF:FB:00:01:2D",
            "lan2_dhcp": True,
            "lan2_ip": "192.168.6.100",
            "lan2_gateway": "192.168.6.1",
            "lan2_subnet_mask": "255.255.255.0",
            "lan2_mac": "6C:DF:FB:00:01:21",
            "telnet_port": "23",
            "ssh": False,
            "https": False,
            "hostname": "controller.local",
            "gpio1_dir": "In", "gpio1_level": 1,
            "gpio2_dir": "In", "gpio2_level": 1,
            "gpio3_dir": "In", "gpio3_level": 1,
            "gpio4_dir": "In", "gpio4_level": 1,
        },
        "delays": {"command_response": 0.02},
        "error_modes": {
            "endpoints_offline": {
                "description": "All encoders/decoders report Net Off (unlinked)",
                "behavior": "endpoints_offline",
            },
            "no_response": {
                "description": "Controller stops responding to commands",
                "behavior": "no_response",
            },
        },
        "controls": [
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
            {"type": "indicator", "key": "encoder_count", "label": "Encoders"},
            {"type": "indicator", "key": "decoder_count", "label": "Decoders"},
            {"type": "indicator", "key": "hostname", "label": "Hostname"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._encoders: dict[int, dict] = {1: self._make_encoder(1)}
        self._decoders: dict[int, dict] = {1: self._make_decoder(1)}
        # Video walls — the standard Control's one queryable config child type.
        # Empty by default (a fresh controller); populated by CREATE/SET WALL.
        self._walls: dict[int, dict] = {}
        # Physical TX/RX present on the Video LAN. assigned == 0 means the unit
        # is unassigned and will show up in SEARCH; otherwise it maps to the
        # enc/dec id it was added as.
        self._phys: list[dict] = [
            {"kind": "enc", "mac": "18:66:96:11:0A:27",
             "search_ip": "169.254.002.177", "assigned": 1},
            {"kind": "dec", "mac": "18:66:96:11:08:C0",
             "search_ip": "169.254.005.166", "assigned": 1},
        ]
        self._awaiting_reset = False
        self._sync_counts()

    # ── Endpoint factories (TX / RX profile, same physical units as the Pro) ──

    def _make_encoder(self, eid: int, online: bool = True) -> dict:
        return {
            "id": eid, "online": online,
            "model": "TAV-CHAZY4K-TX", "firmware": "1.10.03",
            "edid": "DF000", "signal_present": False,
            "audio_input": "HDMI", "multicast": True,
            "name": f"Encoder {eid:03d}",
            "arc_fix": 0, "arc_source": 0,
            "sac": "ARC", "guest_sgen": "Off", "guest_baud": "9",
            "guest_framing": "8n1",
            "io1": _io_default(), "io2": _io2_default(),
            "ip_mode": "Static", "mac": _enc_mac(eid),
            "ip": f"169.254.010.{eid:03d}",
            "gateway": "169.254.001.001", "subnet_mask": "255.255.000.000",
            "lan2_num": "1", "lan2_im": "DHCP",
        }

    def _make_decoder(self, did: int, online: bool = True) -> dict:
        return {
            "id": did, "online": online,
            "model": "TAV-CHAZY4K-RX", "firmware": "1.10.03",
            "hpd": False, "mode": "MX", "resolution": "02", "rotate": "0",
            "name": f"Decoder {did:03d}",
            "source_video": 1, "source_audio": 1, "source_ir": 1,
            "source_rs232": 1, "source_usb": 1, "source_cec": 1,
            "fix_video": 0, "fix_audio": 0, "fix_ir": 0,
            "fix_rs232": 0, "fix_usb": 0, "fix_cec": 0,
            "multicast": True, "video_output": True, "video_mute": False,
            "sac": "ARC", "osp": 4, "guest_sgen": "Off", "guest_baud": "9",
            "guest_framing": "8n1",
            "io1": _io_default(), "io2": _io2_default(),
            "ip_mode": "Static", "mac": _dec_mac(did),
            "ip": f"169.254.020.{did:03d}",
            "gateway": "169.254.001.001", "subnet_mask": "255.255.000.000",
            "lan2_num": "1", "lan2_im": "DHCP",
        }

    @staticmethod
    def _make_wall(wid: int, name: str = "", columns: int = 2, rows: int = 2) -> dict:
        # A configured 2x2 wall with one preset whose class A is sourced from
        # encoder 001 across all four screens (same shape as the Pro family).
        return {
            "id": wid, "name": name, "columns": columns, "rows": rows,
            "cfgsel": 1,
            "presets": [
                {"id": 1, "name": "Preset 1", "classes": [{"cls": "A", "source": 1}]},
            ],
        }

    def _sync_counts(self) -> None:
        self.set_state("encoder_count", len(self._encoders))
        self.set_state("decoder_count", len(self._decoders))

    def _online(self, dev: dict) -> bool:
        return dev.get("online", True) and not self.has_error_behavior(
            "endpoints_offline"
        )

    # ── Connection ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        return _IAC_GREETING + _GREETING_BODY.encode("latin-1") + PROMPT

    # ── Command dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("latin-1", errors="replace").strip()
        body = self._respond(text)
        return self._frame(data, body)

    def _frame(self, echo: bytes, body: str) -> bytes:
        """Echo the command line, emit the response, re-print the prompt."""
        body_crlf = body.replace("\n", "\r\n")
        return echo + b"\r\n" + body_crlf.encode("latin-1") + b"\r\n" + PROMPT

    def _respond(self, text: str) -> str:
        up = text.upper()

        # Reset confirmation follow-up (driver sends "Yes" to proceed).
        if self._awaiting_reset:
            self._awaiting_reset = False
            if up == "YES":
                return "[SUCCESS]System reset to default settings."
            return _LINE_RESET_CANCELLED

        # Pro-only modules are absent on the standard Control.
        if any(up.startswith(p) for p in _PRO_ONLY_PREFIXES):
            return _LINE_ERR_CMD

        if up == "GET STATUS":
            return self._render_status()
        if up == "SEARCH" or up == "GET SEARCH STATUS":
            return self._render_search()
        if up == "ADD AUTO ALL":
            return self._add_auto_all()

        toks = text.split()
        upt = up.split()

        if upt[:2] == ["GET", "GPIO"] and upt[-1:] == ["STATUS"]:
            return self._render_gpio()
        if upt[:2] == ["GET", "ENC"] and upt[-1:] == ["STATUS"] and len(upt) == 4:
            return self._render_enc_detail(toks[2])
        if upt[:2] == ["GET", "DEC"] and upt[-1:] == ["STATUS"] and len(upt) == 4:
            return self._render_dec_detail(toks[2])
        if upt[:2] == ["GET", "WALL"] and upt[-1:] == ["STATUS"]:
            return self._render_wall(self._handle_arg(toks, upt, 2))

        if up in ("SET RESET", "SET RESET NETWORK", "SET RESET ALL"):
            self._awaiting_reset = True
            return self._reset_question(up)

        mutated = self._mutate(toks, upt)
        if mutated is not None:
            return mutated

        return _LINE_ERR_CMD

    def _reset_question(self, up: str) -> str:
        # Only "SET RESET" wording is reused for NETWORK/ALL (the driver only
        # checks for the 'Yes' cue).
        return _LINE_RESET_QUESTION

    # ── GET STATUS ──

    def _render_status(self) -> str:
        lines = list(_STATUS_HEAD)
        lines.append(_STATUS_ENC_HEADER)
        enc_ids = sorted(self._encoders)
        if enc_ids:
            lines += [self._status_enc_row(self._encoders[i]) for i in enc_ids]
        else:
            lines.append("NONE")
        lines.append("")
        lines.append(_STATUS_DEC_HEADER)
        dec_ids = sorted(self._decoders)
        if dec_ids:
            lines += [self._status_dec_row(self._decoders[i]) for i in dec_ids]
        else:
            lines.append("NONE")
        lines += _STATUS_TAIL
        return "\n".join(lines)

    def _status_enc_row(self, e: dict) -> str:
        online = self._online(e)
        eid = f"{e['id']:03d}"
        model = e["model"] if online else ""
        net = "On" if online else "Off"
        sig = "On" if e["signal_present"] else "Off"
        return f"{eid:<8}{model:<20}{e['edid']:<8}{e['ip']:<17}{net:<3}/{sig}"

    def _status_dec_row(self, d: dict) -> str:
        online = self._online(d)
        did = f"{d['id']:03d}"
        model = d["model"] if online else ""
        frm = f"{d['source_video']:03d}"
        net = "On" if online else "Off"
        hpd = "On" if d["hpd"] else "Off"
        nethdmi = f"{net:<3}/{hpd}"
        return (
            f"{did:<8}{model:<20}{frm:<8}{d['ip']:<17}"
            f"{nethdmi:<10}{d['resolution']:<5}{d['mode']}"
        )

    # ── GET GPIO 0 STATUS ──

    def _render_gpio(self) -> str:
        lines = [
            _SENTINEL,
            "              CHAZY CONTROL GPIO Info",
            "              FW Version: 1.00.17",
            "",
            "GPIO   DIR    Set   Get",
        ]
        for n in range(1, 5):
            direction = self.get_state(f"gpio{n}_dir", "In")
            level = self.get_state(f"gpio{n}_level", 1)
            set_col = "-" if direction == "In" else str(level)
            lines.append(_line((0, f"{n:02d}"), (7, direction),
                               (14, set_col), (20, str(level))))
        lines.append(_SENTINEL)
        return "\n".join(lines)

    # ── GET ENC/DEC [n] STATUS ──

    def _render_enc_detail(self, raw_id: str) -> str:
        if not raw_id.isdigit():
            return _LINE_ERR_CMD
        n = int(raw_id)
        e = self._encoders.get(n)
        if e is None:
            return f"[ERROR]Encoder {n:03d} does not exist."
        online = self._online(e)
        lines = [
            _SENTINEL,
            "              CHAZY CONTROL Encoder Info",
            "              FW Version: 1.00.17",
            "",
            "ID      Type                Net    Sig   Ver      "
            "EDID   Aud   MCast   Name",
            self._enc_header_row(e, online),
        ]
        lines += self._enc_body(e, online)
        lines.append(_SENTINEL)
        return "\n".join(lines)

    def _enc_header_row(self, e: dict, online: bool) -> str:
        eid = f"{e['id']:03d}"
        model = e["model"] if online else ""
        net = "On" if online else "Off"
        sig = "On" if e["signal_present"] else "Off"
        ver = e["firmware"] if online else ""
        mcast = "On" if e["multicast"] else "Off"
        name = e["name"] if online else ""
        return (
            f"{eid:<8}{model:<20}{net:<7}{sig:<6}{ver:<9}"
            f"{e['edid']:<7}{e['audio_input']:<6}{mcast:<8}{name}"
        )

    def _enc_body(self, e: dict, online: bool) -> list[str]:
        lines = ["      >>Fix    Arc "]
        lines.append(
            _line((15, f"{e['arc_fix']:03d}"), width=19) if online
            else _line((13, "NA"))
        )
        lines.append("      >>Sel    Arc ")
        lines.append(
            _line((15, f"{e['arc_source']:03d}"), width=19) if online
            else _line((15, "NA"))
        )
        lines.append("      >>SAC    SGEn/Br/Bit")
        lines.append(_line(
            (8, e["sac"]),
            (15, f"{e['guest_sgen']} /{e['guest_baud']} /{e['guest_framing']}"),
        ))
        lines.append("      >>Pin    IOVOL/IODIR/IODAT IRVOL RLY   PHY")
        lines += self._pin_rows(e) if online else [_line((15, "NA"))]
        lines.append("      >>IM     MAC")
        lines.append(_line((8, e["ip_mode"]), (15, e["mac"])))
        lines.append("      >>IP               GW               SM")
        lines.append(_line((8, e["ip"]), (25, e["gateway"]),
                           (42, e["subnet_mask"])))
        if online:
            lines.append("      >>LAN2 Mode  IM")
            lines.append(_line((13, e["lan2_num"]), (19, e["lan2_im"]), width=25))
        return lines

    def _render_dec_detail(self, raw_id: str) -> str:
        if not raw_id.isdigit():
            return _LINE_ERR_CMD
        n = int(raw_id)
        d = self._decoders.get(n)
        if d is None:
            return f"[ERROR]Decoder {n:03d} does not exist."
        online = self._online(d)
        lines = [
            _SENTINEL,
            "              CHAZY CONTROL Decoder Info",
            "              FW Version: 1.00.17",
            "",
            "ID      Type                Net    HPD   Ver      "
            "Mode   Res   Rotate  Name",
            self._dec_header_row(d, online),
        ]
        lines += self._dec_body(d, online)
        lines.append(_SENTINEL)
        return "\n".join(lines)

    def _dec_header_row(self, d: dict, online: bool) -> str:
        did = f"{d['id']:03d}"
        model = d["model"] if online else ""
        net = "On" if online else "Off"
        hpd = "On" if d["hpd"] else "Off"
        ver = d["firmware"] if online else ""
        name = d["name"] if online else ""
        return (
            f"{did:<8}{model:<20}{net:<7}{hpd:<6}{ver:<9}"
            f"{d['mode']:<7}{d['resolution']:<6}{d['rotate']:<8}{name}"
        )

    def _dec_body(self, d: dict, online: bool) -> list[str]:
        mc = "On" if d["multicast"] else "Off"
        vo = "On" if d["video_output"] else "Off"
        vm = "On" if d["video_mute"] else "Off"
        lines = [
            "      >>Fix    Vid      /Aud     /IR      /Ser     "
            "/USB     /CEC     MCast Video Mute",
            _line((15, f"{d['fix_video']:03d}"), (24, f"/{d['fix_audio']:03d}"),
                  (33, f"/{d['fix_ir']:03d}"), (42, f"/{d['fix_rs232']:03d}"),
                  (51, f"/{d['fix_usb']:03d}"), (60, f"/{d['fix_cec']:03d}"),
                  (69, mc), (75, vo), (81, vm)),
            "      >>Sel    Vid      /Aud     /IR      /Ser     /USB     /CEC",
            _line((15, f"{d['source_video']:03d}"),
                  (24, f"/{d['source_audio']:03d}"),
                  (33, f"/{d['source_ir']:03d}"),
                  (42, f"/{d['source_rs232']:03d}"),
                  (51, f"/{d['source_usb']:03d}"),
                  (60, f"/{d['source_cec']:03d}"), width=69),
            "      >>SAC    OSP   SGEn/Br/Bit",
            _line((8, d["sac"]), (15, str(d["osp"])),
                  (21, f"{d['guest_sgen']} /{d['guest_baud']} /{d['guest_framing']}")),
            "      >>Pin    IOVOL/IODIR/IODAT IRVOL RLY   PHY",
        ]
        lines += self._pin_rows(d) if online else [_line((15, "NA"))]
        lines.append("      >>IM     MAC")
        lines.append(_line((8, d["ip_mode"]), (15, d["mac"])))
        lines.append("      >>IP               GW               SM")
        lines.append(_line((8, d["ip"]), (25, d["gateway"]),
                           (42, d["subnet_mask"])))
        if online:
            lines.append("      >>LAN2 Mode  IM")
            lines.append(_line((13, d["lan2_num"]), (19, d["lan2_im"]), width=25))
        return lines

    def _pin_rows(self, dev: dict) -> list[str]:
        io1, io2 = dev["io1"], dev["io2"]
        return [
            _line((8, "(1)"), (15, str(io1["iovol"])), (21, io1["iodir"]),
                  (27, str(io1["iodat"])), (33, str(io1["irvol"])),
                  (39, io1["relay"]), (45, io1["phy"])),
            _line((8, "(2)"), (15, str(io2["iovol"])), (21, io2["iodir"]),
                  (27, str(io2["iodat"])), (39, io2["relay"])),
        ]

    # ── SEARCH / GET SEARCH STATUS ──

    def _render_search(self) -> str:
        lines = [_SENTINEL, "              Search Device Result Info", ""]
        lines.append("==New Encoder")
        lines += self._search_section("enc")
        lines.append("")
        lines.append("==New Decoder")
        lines += self._search_section("dec")
        lines.append(_SENTINEL)
        return "\n".join(lines)

    def _search_section(self, kind: str) -> list[str]:
        new = [p for p in self._phys if p["kind"] == kind and p["assigned"] == 0]
        if not new:
            return ["None"]
        rows = ["Index   IP               MAC                Assign"]
        for idx, p in enumerate(new, start=1):
            rows.append(_line((0, f"{idx:03d}"), (8, p["search_ip"]),
                              (25, p["mac"]), (44, f"ID:{p['assigned']:03d}")))
        return rows

    def _add_auto_all(self) -> str:
        lines = []
        for p in self._phys:
            if p["assigned"] != 0:
                continue
            if p["kind"] == "enc":
                nid = self._next_free(self._encoders)
                self._encoders[nid] = self._make_encoder(nid, online=False)
                word = "encoder"
            else:
                nid = self._next_free(self._decoders)
                self._decoders[nid] = self._make_decoder(nid, online=False)
                word = "decoder"
            p["assigned"] = nid
            lines.append(
                f"[SUCCESS]Add scan index {nid:03d} device to {word} {nid:03d}."
            )
        self._sync_counts()
        return "\n".join(lines) if lines else "[SUCCESS]No new device found."

    @staticmethod
    def _next_free(store: dict) -> int:
        n = 1
        while n in store:
            n += 1
        return n

    # ── GET WALL [n] STATUS ──
    #
    # Video walls are the standard Control's one queryable config child type.
    # No standalone Control hardware exists to capture from, so this banner is
    # not asserted byte-exact; instead the sim tests round-trip it through the
    # driver's _parse_wall_status to prove sim↔driver duality. Layout matches
    # the Pro family; the identity line reads CHAZY CONTROL.

    @staticmethod
    def _handle_arg(toks: list[str], upt: list[str], handle_idx: int) -> int | None:
        """Optional numeric handle between the type word and STATUS. None means
        the all-form (``GET WALL STATUS``)."""
        if len(upt) == handle_idx + 1:
            return None
        if len(upt) == handle_idx + 2 and toks[handle_idx].isdigit():
            return int(toks[handle_idx])
        return None

    @staticmethod
    def _select(store: dict, handle: int | None) -> list[dict]:
        if handle is None:
            return [store[k] for k in sorted(store)]
        return [store[handle]] if handle in store else []

    def _render_wall(self, handle: int | None) -> str:
        # Real hardware separates multiple wall blocks with one blank line
        # (confirmed by a live two-wall query on the Pro); single wall has none.
        blocks: list[list[str]] = []
        for w in self._select(self._walls, handle):
            name = w["name"] if w["name"] else "NULL"
            wlines = [
                "VW  Col    Row    CfgSel  Name",
                _line((0, f"{w['id']:02d}"), (4, f"{w['columns']:02d}"),
                      (11, f"{w['rows']:02d}"), (18, f"{w['cfgsel']:02d}"),
                      (26, name)),
                "    OutID",
                "    " + " ".join(["---"] * (w["columns"] * w["rows"])),
                "    Cfg    Name",
            ]
            screens = " ".join(
                f"H{col:02d}V{row:02d}"
                for row in range(1, w["rows"] + 1)
                for col in range(1, w["columns"] + 1)
            )
            for p in w["presets"]:
                wlines.append(_line((4, f"{p['id']:02d}"), (11, p["name"])))
                wlines.append("           Class  From    Screen")
                for c in p["classes"]:
                    wlines.append(_line((11, c["cls"]), (18, f"{c['source']:03d}"),
                                        (26, screens)))
            blocks.append(wlines)
        body: list[str] = []
        for i, blk in enumerate(blocks):
            if i:
                body.append("")
            body += blk
        lines = [
            _SENTINEL,
            "              CHAZY CONTROL Video Wall Info",
            "              FW Version: 1.00.17",
            "",
        ]
        lines += body if body else ["No Video Wall"]
        lines.append(_SENTINEL)
        return "\n".join(lines)

    # ── Mutations (SET / CREATE / DELETE / ADD) ──

    def _mutate(self, toks: list[str], upt: list[str]) -> str | None:
        if not upt:
            return None

        # SET ENC/DEC <n> ... — endpoint-scoped, with existence check.
        if (upt[0] == "SET" and len(upt) >= 3 and upt[1] in ("ENC", "DEC")
                and toks[2].isdigit()):
            return self._mutate_endpoint(toks, upt)

        # Video-wall lifecycle — mutate tracked state so a later GET WALL
        # STATUS reflects the change.
        wall = self._mutate_wall(toks, upt)
        if wall is not None:
            return wall

        # Everything else valid acks with [SUCCESS]; the driver only treats a
        # leading [ERROR] as a rejection, so the exact text is immaterial.
        # Pro-only verbs were already rejected in _respond, so what reaches here
        # is the standard Control surface.
        if upt[0] in ("SET", "CREATE", "DELETE", "ADD", "APPLY",
                      "DANTE", "EXITGUEST", "SEARCH"):
            return "[SUCCESS]OK."
        return None

    def _mutate_wall(self, toks: list[str], upt: list[str]) -> str | None:
        """Handle CREATE/DELETE WALL HANDLE n and SET WALL n NAME .... Returns
        a reply string if handled, else None (caller falls back)."""
        if upt[:1] not in (["CREATE"], ["DELETE"], ["SET"]) or upt[1:2] != ["WALL"]:
            return None
        verb = upt[0]
        if (verb in ("CREATE", "DELETE") and upt[2:3] == ["HANDLE"]
                and len(toks) >= 4 and toks[3].isdigit()):
            n = int(toks[3])
            if verb == "CREATE":
                self._walls.setdefault(n, self._make_wall(n))
                return f"[SUCCESS]Create wall {n:03d}."
            self._walls.pop(n, None)
            return f"[SUCCESS]Delete wall {n:03d}."
        if verb == "SET" and len(toks) >= 3 and toks[2].isdigit():
            n = int(toks[2])
            if n not in self._walls:
                return f"[ERROR]Wall {n:03d} does not exist."
            if upt[3:4] == ["NAME"]:
                self._walls[n]["name"] = " ".join(toks[4:])
                return f"[SUCCESS]Set wall {n:03d} name."
            return "[SUCCESS]OK."
        return None

    def _mutate_endpoint(self, toks: list[str], upt: list[str]) -> str | None:
        kind = "enc" if upt[1] == "ENC" else "dec"
        n = int(toks[2])
        store = self._encoders if kind == "enc" else self._decoders
        label = "Encoder" if kind == "enc" else "Decoder"
        if n not in store:
            return f"[ERROR]{label} {n:03d} does not exist."
        dev = store[n]
        rest = upt[3:]

        if rest[:1] == ["NAME"]:
            dev["name"] = " ".join(toks[4:])
            return f"[SUCCESS]Set {kind} {n:03d} name."
        if rest[:1] == ["DELETE"]:
            del store[n]
            self._reassign_phys(kind, n, 0)
            self._sync_counts()
            return f"[SUCCESS]Delete {kind} {n:03d}."
        if rest[:1] == ["ID"] and len(toks) >= 5 and toks[4].isdigit():
            new = int(toks[4])
            if new != n and new not in store:
                dev["id"] = new
                store[new] = store.pop(n)
                self._reassign_phys(kind, n, new)
            return f"[SUCCESS]Set {kind} {n:03d} id {new:03d}."
        if rest[:1] == ["MULTICAST"] and len(rest) >= 2:
            dev["multicast"] = rest[1] == "ON"
            return f"[SUCCESS]Set {kind} {n:03d} multicast."
        if kind == "dec" and rest[:1] == ["SWITCH"] and len(toks) >= 5 \
                and toks[4].isdigit():
            self._route_decoder(dev, int(toks[4]), rest[2] if len(rest) > 2 else "ALL")
            return f"[SUCCESS]Set dec {n:03d} switch."
        if kind == "dec" and rest[:2] == ["OUTPUT", "MUTE"] and len(rest) >= 3:
            dev["video_mute"] = rest[2] == "ON"
            return f"[SUCCESS]Set dec {n:03d} mute."
        if kind == "dec" and rest[:1] == ["OUTPUT"] and len(rest) == 2:
            dev["video_output"] = rest[1] == "ON"
            return f"[SUCCESS]Set dec {n:03d} output."
        return "[SUCCESS]OK."

    @staticmethod
    def _route_decoder(dev: dict, src: int, signal: str) -> None:
        targets = {
            "VIDEO": ["source_video"], "AUDIO": ["source_audio"],
            "IR": ["source_ir"], "RS232": ["source_rs232"],
            "USB": ["source_usb"], "CEC": ["source_cec"],
            "ALL": ["source_video", "source_audio", "source_ir",
                    "source_rs232", "source_usb", "source_cec"],
        }
        for key in targets.get(signal, []):
            dev[key] = src

    def _reassign_phys(self, kind: str, old_id: int, new_id: int) -> None:
        for p in self._phys:
            if p["kind"] == kind and p["assigned"] == old_id:
                p["assigned"] = new_id
