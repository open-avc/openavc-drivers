"""
TurtleAV Darwin Control — Simulator.

Stateful TCP simulator for the darwin_control driver. It reproduces the real
controller's telnet API (Controller(h) FW 1.50.02):

  - On connect: Telnet IAC option negotiation, a welcome banner, and the
    ``CONTROLLER> `` prompt.
  - Strictly request/response: every command is echoed, the response body is
    emitted, then ``CONTROLLER> `` is re-printed (the prompt is the
    end-of-response marker the driver frames on).
  - Status / detail banners are fixed-width column tables wrapped in 64-char
    ``====`` sentinel lines. The brand in every header is firmware-dependent
    (``Controller(h)`` on 1.50.02) — the driver keys off row layout, not the
    brand, so the sim renders the verified 1.50.02 brand.

Byte-exact ground truth is the real-hardware capture in
``tests/fixtures/darwin_control_banners.py`` (one TX + one RX enrolled, both
ID 001, online). The render functions below are asserted byte-for-byte against
those fixtures in ``tests/test_darwin_control_sim.py``.

Seeded by default with one encoder (001) and one decoder (001), both enrolled
and online — the state the captures were taken in. ``ADD DEV RESET`` wipes the
roster (back to the empty banner); ``SEARCH`` then re-finds the two physical
endpoints on the Video LAN, and ``ADD AUTO ALL`` re-enrols them.

Darwin is a strict simplification of the Chazy Control Pro, so this simulator
is materially simpler than ``chazy_control_pro_sim.py``: no Dante / media /
group / event / schedule / preset child types, no per-endpoint IO/relay/PHY
rows. It adds the Darwin RX detail blocks (``>>Fr`` / ``>>ASPECT`` / ``>>Video``)
and the Osp/HDCP column.

License: MIT.
"""

from __future__ import annotations

from openavc.simulator.tcp_simulator import TCPSimulator

# End-of-response marker re-printed after the connect banner and every reply.
PROMPT = b"CONTROLLER> "

# Banner header brand on the verified firmware (FW 1.50.02). Firmware-dependent
# (the 2.03.19 manual shows "DARWIN CONTROL"); the sim renders the captured one.
_BRAND = "Controller(h)"
_FIRMWARE = "1.50.02"

# Telnet IAC option negotiation sent at connect, matching the live Darwin unit:
# WILL SGA / WILL ECHO / DONT ECHO / DO BINARY. The fixtures record the stream
# *after* IAC stripping; the driver strips all IAC regardless, so this just
# exercises the driver's _filter_telnet path.
_IAC = 0xFF
_IAC_GREETING = bytes([
    _IAC, 0xFB, 0x03,   # IAC WILL SGA (suppress go-ahead)
    _IAC, 0xFB, 0x01,   # IAC WILL ECHO
    _IAC, 0xFE, 0x01,   # IAC DONT ECHO
    _IAC, 0xFD, 0x00,   # IAC DO BINARY
])

# Connect banner (post-IAC). Matches RAW_GREETING in the fixtures.
_GREETING_BODY = (
    "\r\n"
    "================================================================\r\n"
    "Welcome To Controller(h) Terminal Control System\r\n"
    "FW Version: 1.50.02\r\n"
    'Type "HELP" For More Information\r\n'
    "================================================================\r\n"
)

_SENTINEL = "=" * 64

# Verbatim column headers (copied from the byte-exact captures).
_STATUS_ENC_HEADER = "In      EDID    IP               NET/Sig  AudioFormat"
_STATUS_DEC_HEADER = "Out     FromIn  IP               NET/HDMI  Res  Mode  Osp"
_ENC_COL_HEADER = "In    Net    Sig   Ver     EDID   Aud   MCast   Name"
_DEC_COL_HEADER = "Out   Net    HPD   Ver     Mode   Res   Rotate  Name"
_SEARCH_HEADER = "Index   IP               MAC                Assign"

# Fixed controller network/config block of GET STATUS — constant in the
# captures (zero-padded on the wire). Kept verbatim for byte-exactness; the
# network setter commands are acked but not reflected here (sim fidelity).
_STATUS_TAIL = [
    "",
    "LAN     DHCP    IP              Gateway         SubnetMask",
    "01_POE  Off     169.254.008.100 169.254.008.001 255.255.000.000",
    "02_CTRL On      192.168.004.033 192.168.006.001 255.255.252.000",
    "        (static:192.168.006.100 192.168.006.001 255.255.255.000)",
    "",
    "Telnet  SSH2    HTTPS  WEB  LAN01 MAC               LAN02 MAC",
    "00023   Off     Off    On   18:66:96:11:11:48       18:66:96:11:11:49",
    "",
    "Domain Name",
    "Controller.local",
    _SENTINEL,
]

_LINE_ERR_CMD = "[ERROR]Command not found."
# Confirm wording for SET RESET*. Exact text not captured on hardware (a
# factory reset was never triggered); contains the "Yes"/"confirm" cue the
# driver's _send_with_confirm looks for. HELP shows: (Type "Yes" to confirm).
_LINE_RESET_QUESTION = 'Sure to RESET to default settings? (Type "Yes" to confirm, "No" to cancle)'


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


def _enc_mac(eid: int) -> str:
    return "18:66:96:11:0D:EB" if eid == 1 else f"18:66:96:11:0D:{(0xEB + eid - 1) % 256:02X}"


def _dec_mac(did: int) -> str:
    return "18:66:96:11:0D:1D" if did == 1 else f"18:66:96:11:0D:{(0x1D + did - 1) % 256:02X}"


class DarwinControlSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "darwin_control",
        "name": "TurtleAV Darwin Control Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 23,
        # Drivers send CRLF-terminated command lines; respond with CRLF too.
        "delimiter": "\r\n",
        "initial_state": {
            "firmware": _FIRMWARE,
            "power": True,
            "ir": True,
            "rs232_baud": "57600",
            "encoder_count": 1,
            "decoder_count": 1,
            "lan1_dhcp": False,
            "lan1_ip": "169.254.8.100",
            "lan1_gateway": "169.254.8.1",
            "lan1_subnet_mask": "255.255.0.0",
            "lan1_mac": "18:66:96:11:11:48",
            "lan2_dhcp": True,
            "lan2_ip": "192.168.4.33",
            "lan2_gateway": "192.168.6.1",
            "lan2_subnet_mask": "255.255.252.0",
            "lan2_mac": "18:66:96:11:11:49",
            "telnet_port": "23",
            "ssh": False,
            "https": False,
            "web": True,
            "hostname": "Controller.local",
            "gpio1_dir": "In", "gpio1_level": 1,
            "gpio2_dir": "In", "gpio2_level": 1,
            "gpio3_dir": "In", "gpio3_level": 1,
            "gpio4_dir": "In", "gpio4_level": 1,
        },
        "delays": {"command_response": 0.02},
        "error_modes": {
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
        self._walls: dict[int, dict] = {}
        # Physical TX/RX present on the Video LAN. assigned == 0 means the unit
        # is unassigned and shows up in SEARCH; otherwise it maps to the enc/dec
        # id it was enrolled as. search_mac is lowercase, as the SEARCH banner
        # reports it (the enrolled detail banner reports it uppercase).
        self._phys: list[dict] = [
            {"kind": "enc", "search_ip": "169.254.236.013",
             "search_mac": "18:66:96:11:0d:eb", "assigned": 1},
            {"kind": "dec", "search_ip": "169.254.030.013",
             "search_mac": "18:66:96:11:0d:1d", "assigned": 1},
        ]
        self._awaiting_reset = False
        self._sync_counts()

    # ── Endpoint factories (captured TX / RX profile) ──

    def _make_encoder(self, eid: int) -> dict:
        return {
            "id": eid, "net": True, "signal_present": False,
            "firmware": "3.12.02", "edid": "DF000", "audio_input": "HDMI",
            "audio_format": "PCM", "multicast": True, "name": f"Encoder {eid:03d}",
            "fpled": "9", "guest_sgen": "Off", "guest_baud": "9", "guest_framing": "8n1",
            "mac": _enc_mac(eid), "ip": f"169.254.010.{eid:03d}",
            "gateway": "169.254.008.001", "subnet_mask": "255.255.000.000",
        }

    def _make_decoder(self, did: int) -> dict:
        return {
            "id": did, "net": True, "hpd": False, "firmware": "3.12.01",
            "mode": "MX", "resolution": "01", "rotate": "0", "source": did,
            "name": f"Decoder {did:03d}",
            "lock_video": 0, "lock_ir": 0, "lock_rs232": 0, "lock_usb": 0,
            "multicast": True, "video_output": True, "video_mute": False,
            "pause": False, "auto": True, "video_lost_timeout": 0,
            "aspect": "Maintain", "hdcp": "SNK", "ir_enabled": True, "button": True,
            "fpled": "9", "guest_sgen": "Off", "guest_baud": "9", "guest_framing": "8n1",
            "mac": _dec_mac(did), "ip": f"169.254.020.{did:03d}",
            "gateway": "169.254.008.001", "subnet_mask": "255.255.000.000",
        }

    @staticmethod
    def _make_wall(wid: int, name: str = "", columns: int = 2, rows: int = 2) -> dict:
        return {"id": wid, "name": name, "columns": columns, "rows": rows, "cfgsel": 1}

    def _sync_counts(self) -> None:
        self.set_state("encoder_count", len(self._encoders))
        self.set_state("decoder_count", len(self._decoders))

    @staticmethod
    def _guest_str(dev: dict) -> str:
        return f"{dev['guest_sgen']} /{dev['guest_baud']}/{dev['guest_framing']}"

    @staticmethod
    def _next_free(store: dict) -> int:
        n = 1
        while n in store:
            n += 1
        return n

    @staticmethod
    def _select(store: dict, handle: int | None) -> list[dict]:
        if handle is None:
            return [store[k] for k in sorted(store)]
        return [store[handle]] if handle in store else []

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
                return "[SUCCESS]Reset to default settings."
            return "[SUCCESS]Reset process ignored."

        if up == "GET STATUS":
            return self._render_status()
        if up in ("SEARCH", "GET SEARCH STATUS"):
            return self._render_search()
        if up == "ADD AUTO ALL":
            return self._add_auto_all()
        if up == "ADD DEV RESET":
            return self._add_dev_reset()
        if up == "SEARCH RESET":
            return "[SUCCESS]Search result reset."
        if up == "EXITGUEST":
            return "[SUCCESS]Exit guest mode."

        toks = text.split()
        upt = up.split()

        if upt[:2] == ["GET", "GPIO"] and upt[-1:] == ["STATUS"]:
            return self._render_gpio()
        if upt[:2] == ["GET", "ENC"] and upt[-1:] == ["STATUS"]:
            return self._render_enc_detail(toks[2] if len(upt) == 4 else None)
        if upt[:2] == ["GET", "DEC"] and upt[-1:] == ["STATUS"]:
            return self._render_dec_detail(toks[2] if len(upt) == 4 else None)
        if upt[:2] == ["GET", "WALL"] and upt[-1:] == ["STATUS"]:
            handle = int(toks[2]) if (len(upt) == 4 and toks[2].isdigit()) else None
            return self._render_wall(handle)

        if up in ("SET RESET", "SET RESET NETWORK", "SET RESET ALL"):
            self._awaiting_reset = True
            return _LINE_RESET_QUESTION

        mutated = self._mutate(toks, upt)
        if mutated is not None:
            return mutated
        return _LINE_ERR_CMD

    # ── Banner title block ──

    def _title(self, label: str) -> list[str]:
        return [
            _SENTINEL,
            f"              {label}",
            f"              FW Version: {self.get_state('firmware', _FIRMWARE)}",
        ]

    # ── GET STATUS ──

    def _render_status(self) -> str:
        power = "On" if self.get_state("power", True) else "Off"
        ir = "On" if self.get_state("ir", True) else "Off"
        baud = self.get_state("rs232_baud", "57600")
        lines = self._title(f"{_BRAND} Status Info") + [
            "",
            "Power\tIR\tBaud",
            f"{power} \t{ir} \t{baud} ",
            "",
            _STATUS_ENC_HEADER,
        ]
        enc_ids = sorted(self._encoders)
        lines += [self._status_enc_row(self._encoders[i]) for i in enc_ids] or ["NONE"]
        lines += ["", _STATUS_DEC_HEADER]
        dec_ids = sorted(self._decoders)
        lines += [self._status_dec_row(self._decoders[i]) for i in dec_ids] or ["NONE"]
        lines += _STATUS_TAIL
        return "\n".join(lines)

    def _status_enc_row(self, e: dict) -> str:
        netsig = f"{'On ' if e['net'] else 'Off'}/{'On' if e['signal_present'] else 'Off'}"
        return _line((0, f"{e['id']:03d}"), (8, e["edid"]), (16, e["ip"]),
                     (33, netsig), (43, e["audio_format"]))

    def _status_dec_row(self, d: dict) -> str:
        netsig = f"{'On ' if d['net'] else 'Off'}/{'On' if d['hpd'] else 'Off'}"
        return _line((0, f"{d['id']:03d}"), (8, f"{d['source']:03d}"), (16, d["ip"]),
                     (33, netsig), (43, d["resolution"]), (48, d["mode"]), (54, d["hdcp"]))

    # ── GET ENC/DEC [n] STATUS ──

    def _render_enc_detail(self, raw_id: str | None) -> str:
        title = self._title(f"IP Control Box {_BRAND} Encoder Info")
        if raw_id is not None:
            if not raw_id.isdigit():
                return _LINE_ERR_CMD
            n = int(raw_id)
            e = self._encoders.get(n)
            if e is None:
                return f"[ERROR]Encoder {n:03d} not exist."
            encs = [e]
        else:
            encs = [self._encoders[k] for k in sorted(self._encoders)]
        if not encs:
            return "\n".join(title + [_SENTINEL])
        body = [_ENC_COL_HEADER]
        for e in encs:
            body += self._enc_block(e)
        return "\n".join(title + [""] + body + [_SENTINEL])

    def _enc_block(self, e: dict) -> list[str]:
        net = "On" if e["net"] else "Off"
        sig = "On" if e["signal_present"] else "Off"
        mcast = "On" if e["multicast"] else "Off"
        return [
            _line((0, f"{e['id']:03d}"), (6, net), (13, sig), (19, e["firmware"]),
                  (27, e["edid"]), (34, e["audio_input"]), (40, mcast), (48, e["name"])),
            "    >>LED    SGEn/Br/Bit",
            _line((6, e["fpled"]), (13, self._guest_str(e))),
            "    >>MAC",
            _line((6, e["mac"])),
            "    >>IP               GW               SM",
            _line((6, e["ip"]), (23, e["gateway"]), (40, e["subnet_mask"])),
        ]

    def _render_dec_detail(self, raw_id: str | None) -> str:
        title = self._title(f"IP Control Box {_BRAND} Decoder Info")
        if raw_id is not None:
            if not raw_id.isdigit():
                return _LINE_ERR_CMD
            n = int(raw_id)
            d = self._decoders.get(n)
            if d is None:
                return f"[ERROR]Decoder {n:03d} not exist."
            decs = [d]
        else:
            decs = [self._decoders[k] for k in sorted(self._decoders)]
        if not decs:
            return "\n".join(title + [_SENTINEL])
        body = [_DEC_COL_HEADER]
        for d in decs:
            body += self._dec_block(d)
        return "\n".join(title + [""] + body + [_SENTINEL])

    def _dec_block(self, d: dict) -> list[str]:
        net = "On" if d["net"] else "Off"
        hpd = "On" if d["hpd"] else "Off"
        ir = "On" if d["ir_enabled"] else "Off"
        btn = "On" if d["button"] else "Off"
        locks = (f"{d['lock_video']:03d}/{d['lock_ir']:03d}/"
                 f"{d['lock_rs232']:03d}/{d['lock_usb']:03d}")
        mc = "On " if d["multicast"] else "Off"
        vo = "On" if d["video_output"] else "Off"
        vm = "On" if d["video_mute"] else "Off"
        pause = "On" if d["pause"] else "Off"
        auto = "On" if d["auto"] else "Off"
        return [
            _line((0, f"{d['id']:03d}"), (6, net), (13, hpd), (19, d["firmware"]),
                  (27, d["mode"]), (34, d["resolution"]), (40, d["rotate"]), (48, d["name"])),
            "    >>Fr     Vid/IR_/Ser/USB      MCast",
            _line((6, f"{d['source']:03d}"), (13, locks), (34, mc)),
            "    >>ASPECT    OSP    IR    BTN     LED    SGEn/Br/Bit",
            _line((6, d["aspect"]), (16, d["hdcp"]), (23, ir), (29, btn),
                  (37, d["fpled"]), (44, self._guest_str(d))),
            "    >>Video  Mute  Pause   Auto   VideoLostTimeout",
            _line((6, vo), (13, vm), (19, pause), (27, auto),
                  (34, str(d["video_lost_timeout"]))),
            "    >>MAC",
            _line((6, d["mac"])),
            "    >>IP               GW               SM",
            _line((6, d["ip"]), (23, d["gateway"]), (40, d["subnet_mask"])),
        ]

    # ── GET GPIO 0 STATUS ──

    def _render_gpio(self) -> str:
        lines = self._title(f"{_BRAND} GPIO Info") + ["", "GPIO   DIR    Set   Get"]
        for n in range(1, 5):
            direction = self.get_state(f"gpio{n}_dir", "In")
            level = self.get_state(f"gpio{n}_level", 1)
            set_col = "-" if direction == "In" else str(level)
            lines.append(_line((0, f"{n:02d}"), (7, direction),
                               (14, set_col), (20, str(level))))
        lines.append(_SENTINEL)
        return "\n".join(lines)

    # ── GET WALL [hdl] STATUS ──

    def _render_wall(self, handle: int | None) -> str:
        title = self._title(f"{_BRAND} Video Wall Info")
        walls = self._select(self._walls, handle)
        if not walls:
            return "\n".join(title + ["", "No Video Wall", _SENTINEL])
        body: list[str] = []
        for i, w in enumerate(walls):
            if i:
                body.append("")
            body += self._wall_block(w)
        return "\n".join(title + [""] + body + [_SENTINEL])

    def _wall_block(self, w: dict) -> list[str]:
        name = w["name"] if w["name"] else "NULL"
        cells = w["columns"] * w["rows"]
        return [
            "VW  Col    Row    CfgSel  Name",
            _line((0, f"{w['id']:02d}"), (4, f"{w['columns']:02d}"),
                  (11, f"{w['rows']:02d}"), (18, f"{w['cfgsel']:02d}"), (26, name)),
            "    OutID",
            "    " + " ".join(["---"] * cells),
        ]

    # ── SEARCH / GET SEARCH STATUS ──

    def _render_search(self) -> str:
        lines = [
            "[SUCCESS]More device in network will take more time to finish scan, "
            "please wait...done.",
            _SENTINEL,
            "              Scan Device Result Info",
            "",
            "==New Encoder",
        ]
        lines += self._search_section("enc")
        lines += ["", "==New Decoder"]
        lines += self._search_section("dec")
        lines.append(_SENTINEL)
        return "\n".join(lines)

    def _search_section(self, kind: str) -> list[str]:
        new = [p for p in self._phys if p["kind"] == kind and p["assigned"] == 0]
        if not new:
            return ["None"]
        rows = [_SEARCH_HEADER]
        for idx, p in enumerate(new, start=1):
            rows.append(_line((0, f"{idx:03d}"), (8, p["search_ip"]),
                              (25, p["search_mac"]), (44, "No")))
        return rows

    # ── ADD … / device management ──

    def _add_auto_all(self) -> str:
        lines = []
        for p in self._phys:
            if p["assigned"] != 0:
                continue
            if p["kind"] == "enc":
                nid = self._next_free(self._encoders)
                self._encoders[nid] = self._make_encoder(nid)
                word = "encoder"
            else:
                nid = self._next_free(self._decoders)
                self._decoders[nid] = self._make_decoder(nid)
                word = "decoder"
            p["assigned"] = nid
            lines.append(
                f"[SUCCESS]Add dev search IP:{p['search_ip']}  device to {word}."
            )
        self._sync_counts()
        return "\n".join(lines) if lines else "[SUCCESS]No new device found."

    def _add_dev_reset(self) -> str:
        self._encoders.clear()
        self._decoders.clear()
        self._walls.clear()
        for p in self._phys:
            p["assigned"] = 0
        self._sync_counts()
        return "[SUCCESS]Reset All Encoder/Decoder/Videowall/Search Configuration."

    def _add_dev(self, toks: list[str], upt: list[str]) -> str:
        dev_idx = int(toks[2])
        kind = "enc" if upt[3] == "ENC" else "dec"
        target = int(toks[4]) if len(toks) >= 5 and toks[4].isdigit() else 0
        pool = [p for p in self._phys if p["kind"] == kind and p["assigned"] == 0]
        if dev_idx < 1 or dev_idx > len(pool):
            return f"[ERROR]Search index {dev_idx:03d} not exist."
        p = pool[dev_idx - 1]
        store = self._encoders if kind == "enc" else self._decoders
        nid = target if (target and target not in store) else self._next_free(store)
        if kind == "enc":
            self._encoders[nid] = self._make_encoder(nid)
            word = "encoder"
        else:
            self._decoders[nid] = self._make_decoder(nid)
            word = "decoder"
        p["assigned"] = nid
        self._sync_counts()
        return f"[SUCCESS]Add dev search IP:{p['search_ip']}  device to {word}."

    # ── Mutations (SET / CREATE / DELETE / ADD) ──

    def _mutate(self, toks: list[str], upt: list[str]) -> str | None:
        if not upt:
            return None
        # SET ENC/DEC <n> ... — endpoint-scoped, with existence check.
        if (upt[0] == "SET" and len(upt) >= 3 and upt[1] in ("ENC", "DEC")
                and toks[2].isdigit()):
            return self._mutate_endpoint(toks, upt)
        # Video-wall lifecycle + size/name tracking.
        w = self._mutate_wall(toks, upt)
        if w is not None:
            return w
        # ADD DEV <idx> ENC/DEC <id>.
        if (upt[:2] == ["ADD", "DEV"] and len(upt) >= 4 and toks[2].isdigit()
                and upt[3] in ("ENC", "DEC")):
            return self._add_dev(toks, upt)
        # Everything else valid acks with [SUCCESS]; the driver only treats a
        # leading [ERROR] as a rejection, so the exact text is immaterial.
        if upt[0] in ("SET", "CREATE", "DELETE", "APPLY", "ADD"):
            return "[SUCCESS]OK."
        return None

    def _mutate_endpoint(self, toks: list[str], upt: list[str]) -> str:
        kind = "enc" if upt[1] == "ENC" else "dec"
        n = int(toks[2])
        store = self._encoders if kind == "enc" else self._decoders
        label = "Encoder" if kind == "enc" else "Decoder"
        if n not in store:
            return f"[ERROR]{label} {n:03d} not exist."
        dev = store[n]
        rest = upt[3:]
        if rest[:1] == ["NAME"]:
            dev["name"] = " ".join(toks[4:])
            return f"[SUCCESS]Set {kind} {n:03d} name."
        if rest[:1] == ["DELETE"]:
            del store[n]
            self._free_phys(kind, n)
            self._sync_counts()
            return f"[SUCCESS]Delete {kind} {n:03d}."
        if rest[:1] == ["ID"] and len(toks) >= 5 and toks[4].isdigit():
            new = int(toks[4])
            if new != n and new not in store:
                dev["id"] = new
                store[new] = store.pop(n)
                self._reassign_phys(kind, n, new)
            return f"[SUCCESS]Set {kind} {n:03d} id {new:03d}."
        if kind == "dec":
            return self._mutate_decoder(dev, n, toks, rest)
        return "[SUCCESS]OK."

    def _mutate_decoder(self, dev: dict, n: int, toks: list[str], rest: list[str]) -> str:
        if rest[:1] == ["SWITCH"] and len(toks) >= 5 and toks[4].isdigit():
            src = int(toks[4])
            signal = rest[2] if len(rest) > 2 else "ALL"
            if signal in ("ALL", "VIDEO"):
                dev["source"] = src
            return f"[SUCCESS]Set dec {n:03d} switch."
        if rest[:2] == ["OUTPUT", "MUTE"] and len(rest) >= 3:
            dev["video_mute"] = rest[2] == "ON"
            return f"[SUCCESS]Set dec {n:03d} mute."
        if rest[:2] == ["OUTPUT", "PAUSE"] and len(rest) >= 3:
            dev["pause"] = rest[2] == "ON"
            return f"[SUCCESS]Set dec {n:03d} pause."
        if rest[:2] == ["OUTPUT", "AUTO"] and len(rest) >= 3:
            dev["auto"] = rest[2] == "ON"
            return f"[SUCCESS]Set dec {n:03d} auto."
        if rest[:2] == ["OUTPUT", "RESOLUTION"] and len(rest) >= 3:
            dev["resolution"] = rest[2].zfill(2)
            return f"[SUCCESS]Set dec {n:03d} resolution."
        if rest[:2] == ["OUTPUT", "ROTATE"] and len(rest) >= 3:
            dev["rotate"] = rest[2]
            return f"[SUCCESS]Set dec {n:03d} rotate."
        if rest[:2] == ["OUTPUT", "LOST"] and len(rest) >= 3 and rest[2].isdigit():
            dev["video_lost_timeout"] = int(rest[2])
            return f"[SUCCESS]Set dec {n:03d} video lost timeout."
        if rest[:1] == ["OUTPUT"] and len(rest) == 2:
            dev["video_output"] = rest[1] == "ON"
            return f"[SUCCESS]Set dec {n:03d} output."
        if rest[:1] == ["MODE"] and len(rest) >= 2:
            dev["mode"] = rest[1]
            return f"[SUCCESS]Set dec {n:03d} mode."
        if rest[:1] == ["OSP"] and len(rest) >= 2:
            dev["hdcp"] = rest[1]
            return f"[SUCCESS]Set dec {n:03d} osp."
        if rest[:1] == ["BUTTON"] and len(rest) >= 2:
            dev["button"] = rest[1] == "ON"
            return f"[SUCCESS]Set dec {n:03d} button."
        if rest[:1] == ["IR"] and len(rest) == 2:  # 'IR VOL 12V' is len 3, excluded
            dev["ir_enabled"] = rest[1] == "ON"
            return f"[SUCCESS]Set dec {n:03d} ir."
        if rest[:1] == ["FPLED"] and len(toks) >= 5:
            dev["fpled"] = toks[4]
            return f"[SUCCESS]Set dec {n:03d} fpled."
        if rest[:1] == ["STREAM"] and len(rest) >= 2:
            dev["multicast"] = rest[1] == "MULTICAST"
            return f"[SUCCESS]Set dec {n:03d} stream."
        return "[SUCCESS]OK."

    def _mutate_wall(self, toks: list[str], upt: list[str]) -> str | None:
        # CREATE/DELETE WALL HANDLE n
        if (upt[:2] in (["CREATE", "WALL"], ["DELETE", "WALL"])
                and upt[2:3] == ["HANDLE"] and len(toks) >= 4 and toks[3].isdigit()):
            n = int(toks[3])
            if upt[0] == "CREATE":
                self._walls.setdefault(n, self._make_wall(n))
                return f"[SUCCESS]Create wall handle {n:02d}."
            self._walls.pop(n, None)
            return f"[SUCCESS]Delete wall handle {n:02d}."
        # SET WALL n NAME ... / SET WALL n C c R r — tracked so GET WALL reflects.
        if upt[:2] == ["SET", "WALL"] and len(toks) >= 3 and toks[2].isdigit():
            n = int(toks[2])
            if n not in self._walls:
                return f"[ERROR]Video Wall {n:02d} not exist."
            rest = upt[3:]
            if rest[:1] == ["NAME"]:
                self._walls[n]["name"] = " ".join(toks[4:])
                return f"[SUCCESS]Set wall {n:02d} name."
            if (rest[:1] == ["C"] and upt[5:6] == ["R"] and len(toks) >= 7
                    and toks[4].isdigit() and toks[6].isdigit()):
                self._walls[n]["columns"] = int(toks[4])
                self._walls[n]["rows"] = int(toks[6])
                return f"[SUCCESS]Set wall {n:02d} size."
            return "[SUCCESS]OK."
        return None

    # ── Physical-device bookkeeping ──

    def _free_phys(self, kind: str, old_id: int) -> None:
        for p in self._phys:
            if p["kind"] == kind and p["assigned"] == old_id:
                p["assigned"] = 0

    def _reassign_phys(self, kind: str, old_id: int, new_id: int) -> None:
        for p in self._phys:
            if p["kind"] == kind and p["assigned"] == old_id:
                p["assigned"] = new_id
