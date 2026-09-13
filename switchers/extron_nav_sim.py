"""
Extron NAV Pro AV-over-IP (NAVigator System Manager) — Simulator.

Stateful simulator for the ``extron_nav`` driver: the SIS grammar the NAVigator
serves on its SSH/SIS port, reproduced closely enough to drive the whole driver
without hardware — the echo and verbose-mode session settings, ties, the
inventory and tie reports, WindoWall and KVM presets, alarms, encapsulated
endpoint commands, and the unsolicited endpoint notices the NAVigator pushes on
the control connection.

It is reached over raw TCP, which is the driver's ``transport: tcp`` mode. The
driver's framing is identical over SSH and TCP (both are raw byte pipes), so
exercising it here validates the real connect -> poll -> command path.

Identity values are the ones printed in the system report in the NAVigator User
Guide (68-2740-01 Rev. F): model ``NAVigator``, part ``60-1534-01``, serial
``A1PC690``, firmware ``1.01.0000-b088``, MAC ``00-05-A6-13-9C-32``.

**Four awkward cases are modelled deliberately, because they are the ones that
mislead:**

1. **Echo is ON at connect**, as it is on real hardware. A driver that does not
   turn it off sees a copy of every command interleaved with its replies. The
   echo of the very command that disables it is emitted too, so the driver's
   connect ceremony has to survive its own echo.
2. **Verbose mode starts at 0**, so the endpoint notices are UNTAGGED until the
   driver sets verbose 3. A simulator that tagged them from the start would
   hide the reason the driver sets verbose at all.
3. **The endpoint roster is sparse and non-contiguous** — encoders at 1, 2, 3,
   17 and 101, decoders at 1, 2, 3, 4, 201 and 202. A driver that assumed
   1..N would pass against a dense roster and mis-map every endpoint here.
4. **One decoder has its audio broken away** from its video (decoder 3 takes
   video from 1 and audio from 17), and one endpoint is "present but not
   connected" rather than simply offline — the two states a real NAV system
   distinguishes and a boolean would flatten.

License: MIT.
"""

from __future__ import annotations

import re

from openavc.simulator.tcp_simulator import TCPSimulator

ESC = "\x1b"
CR = "\r"
CRLF = "\r\n"

# Inventory digits: 0 unassigned, 1 online, 2 offline, 3 present-not-connected.
# Sparse on purpose (see the module docstring).
_ENCODERS = {1: "1", 2: "1", 3: "1", 17: "2", 101: "3"}
_DECODERS = {1: "1", 2: "1", 3: "1", 4: "1", 201: "1", 202: "2"}

_ENCODER_NAMES = {
    1: "NAV-E-Podium-PC",
    2: "NAV-E-Laptop-HDMI",
    3: "NAV-E-Doc-Cam",
    17: "NAV-E-Overflow",
    101: "NAV-E-Lecture-Capture",
}
_DECODER_NAMES = {
    1: "NAV-SD-Main-Projector",
    2: "NAV-SD-Confidence",
    3: "NAV-SD-Lobby",
    4: "NAV-SD-Overflow-1",
    201: "NAV-SD-Atrium-Left",
    202: "NAV-SD-Atrium-Right",
}

# {output: [video input, audio input]}. 0 is untied; decoder 3 has its audio
# broken away from its video.
_TIES = {1: [1, 1], 2: [3, 3], 3: [1, 17], 4: [0, 0], 201: [101, 101],
         202: [0, 0]}

# {usb device endpoint: usb host endpoint}
_USB_TIES = {"1o": "17i"}

_ALARMS = [
    ("17i", "device_offline", "warning", "2026-09-12T14:02:11Z"),
    ("202o", "video_loss", "info", "2026-09-12T14:03:40Z"),
]


class ExtronNavSimulator(TCPSimulator):
    """Simulated Extron NAVigator System Manager."""

    SIMULATOR_INFO = {
        "driver_id": "extron_nav",
        "name": "Extron NAVigator System Manager Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 22023,
        # No delimiter: the simulator frames incoming bytes itself, because an
        # encapsulated command carries an embedded CR inside its braces and a
        # CR-framed reader would split it in half.
        "initial_state": {
            # Seeds are what goes ON THE WIRE, which is a different namespace
            # from the driver's published state variables.
            "model": "NAVigator",
            "model_description": "NAV System Manager",
            "part_number": "60-1534-01",
            "serial": "A1PC690",
            "firmware": "1.01.0000-b088",
            "unit_name": "NAVigator-13-9C-31",
            "mac": "00-05-A6-13-9C-32",
            "temperature_f": 103,
            "temperature_c": 39,
            "connected_users": 2,
            "igmp_querier": "192.168.1.1",
            "oob_ip": "192.168.253.254",
            "oob_prefix": 24,
            "oob_gateway": "0.0.0.0",
            "nav_ip": "192.168.1.10",
            "nav_prefix": 24,
            "nav_gateway": "192.168.1.1",
            "dns": "192.168.1.1",
            "licensed_endpoints": 48,
            # Session settings, at their factory values: echo ON, verbose 0.
            "echo": 1,
            "verbose": 0,
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
            {"type": "indicator", "key": "unit_name", "label": "Name"},
            {"type": "slider", "key": "temperature_c", "min": 0, "max": 90,
             "label": "Temperature (C)"},
            {"type": "slider", "key": "connected_users", "min": 0, "max": 15,
             "label": "Connected Users"},
            {"type": "select", "key": "licensed_endpoints",
             "options": [16, 48, 96, 240], "label": "Licensed Endpoints"},
            {"type": "toggle", "key": "echo", "label": "Echo"},
            {"type": "slider", "key": "verbose", "min": 0, "max": 3,
             "label": "Verbose Mode"},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Own framing (see SIMULATOR_INFO): raw byte mode.
        self._delimiter = None
        self._line_mode = False
        self._buf = ""
        self._encoders = dict(_ENCODERS)
        self._decoders = dict(_DECODERS)
        self._ties = {k: list(v) for k, v in _TIES.items()}
        self._usb = dict(_USB_TIES)
        self._alarms = list(_ALARMS)
        self._window_mute: dict[tuple[int, int], int] = {}
        self._window_input: dict[tuple[int, int], int] = {}
        self._canvas_preset: dict[int, int] = {}
        self._workstation_preset: dict[int, int] = {}

    # ── session ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        # A fresh SSH/SIS session starts at the factory session settings.
        self.set_state("echo", 1)
        self.set_state("verbose", 0)
        self._buf = ""
        # The SSH identification string a raw socket meets on port 22023, which
        # is what the driver's discovery probe fingerprints. A real driver never
        # sees it -- the SSH transport consumes the banner before the SIS
        # session starts -- but this simulator IS the raw socket, so serving it
        # here is what lets the probe be verified against the driver's own
        # simulator rather than a hand-typed expectation. Deliberately a plain
        # OpenSSH ident with no vendor string: Extron's real banner is unknown
        # without hardware, and inventing one would invite matching on it.
        return b"SSH-2.0-OpenSSH_9.6\r\n"

    # ── framing ──

    def handle_command(self, data: bytes) -> bytes | None:
        """Frame raw bytes into SIS commands and answer each one.

        A plain command ends at a CR. An encapsulated command starts with '{'
        and ends at '}' followed by a CR — its inner command carries its own CR,
        which is exactly why this cannot frame on CR alone.
        """
        self._buf += data.decode("latin-1", errors="replace")
        out: list[str] = []
        while self._buf:
            if self._buf[0] == "{":
                end = self._buf.find("}")
                if end == -1:
                    break                       # brace still open
                after = self._buf[end + 1:]
                if not after:
                    break                       # waiting for the trailing CR
                if after[0] not in "\r\n":
                    # '}' that is not followed by a terminator: keep scanning
                    # for a later one rather than answering half a command.
                    nxt = self._buf.find("}", end + 1)
                    if nxt == -1:
                        break
                    end = nxt
                    after = self._buf[end + 1:]
                    if not after:
                        break
                cmd = self._buf[: end + 1]
                self._buf = after[1:] if after[0] in "\r\n" else after
                out.append(self._answer(cmd))
                continue

            m = re.search(r"[\r\n]", self._buf)
            if not m:
                break
            cmd = self._buf[: m.start()]
            self._buf = self._buf[m.end():]
            if cmd:
                out.append(self._answer(cmd))

        body = "".join(o for o in out if o)
        return body.encode("latin-1") if body else None

    def _answer(self, command: str) -> str:
        echo = command + CRLF if self.state.get("echo") else ""
        try:
            reply = self._dispatch(command)
        except _SisError as e:
            reply = e.code
        if reply is None:
            reply = "E10"
        return echo + (reply + CRLF if reply else "")

    # ── helpers ──

    def _verbose(self) -> int:
        try:
            return int(self.state.get("verbose") or 0)
        except (TypeError, ValueError):
            return 0

    def _tagged(self) -> bool:
        """Verbose 2 and 3 tag query answers with the constant string."""
        return self._verbose() >= 2

    def _roster(self, kind: str) -> dict[int, str]:
        return self._encoders if kind == "i" else self._decoders

    def _inventory(self, kind: str) -> str:
        roster = self._roster(kind)
        if not roster:
            return ""
        # One digit per endpoint number, up to the highest assigned one. A real
        # NAVigator sends all 4096; trimming keeps the simulator's frames small
        # without changing what the driver has to parse (position = number).
        top = max(roster)
        return "".join(roster.get(n, "0") for n in range(1, top + 1))

    def _name_of(self, number: int, kind: str) -> str | None:
        table = _ENCODER_NAMES if kind == "i" else _DECODER_NAMES
        if number not in self._roster(kind):
            return None
        return table.get(number, f"NAV-{number}{kind}")

    def _require_endpoint(self, ref: str) -> tuple[int, str]:
        m = re.match(r"^(\d{1,4})([ioIO])$", ref.strip())
        if not m:
            raise _SisError("E13")
        number, kind = int(m.group(1)), m.group(2).lower()
        if number not in self._roster(kind):
            raise _SisError("E25")
        return number, kind

    def _sync_tie_state(self) -> None:
        self.set_state("tie_count",
                       sum(1 for v in self._ties.values() if v[0] or v[1]))

    # ── dispatch ──

    def _dispatch(self, command: str) -> str | None:
        # Encapsulated command to an endpoint.
        if command.startswith("{"):
            return self._encapsulated(command)

        # ── ESC-prefixed commands ──
        if command.startswith(ESC):
            return self._esc_command(command[1:].rstrip(CR))

        body = command.rstrip(CR)

        # WindoWall video mute — no ESC, terminated by the trailing B.
        m = re.match(r"^(\d+)\*(\d+)\*([01])B$", body)
        if m:
            canvas, window, value = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if not (1 <= canvas <= 8 and 1 <= window <= 64):
                raise _SisError("E13")
            self._window_mute[(canvas, window)] = value
            return f"Vmt{canvas}*{window}*{value}"
        m = re.match(r"^(\d+)\*(\d+)B$", body)
        if m:
            canvas, window = int(m.group(1)), int(m.group(2))
            value = self._window_mute.get((canvas, window), 0)
            return (f"Vmt{canvas}*{window}*{value}" if self._tagged()
                    else str(value))

        # Information requests.
        if body == "I":
            enc, dec = len(self._encoders), len(self._decoders)
            return f"V{enc}X{dec} A{enc}X{dec}"
        if body == "1I":
            return str(self.state.get("model"))
        if body == "2I":
            return str(self.state.get("model_description"))
        if body == "10I":
            return str(self.state.get("connected_users"))
        if body == "50I":
            return str(self.state.get("igmp_querier"))
        if body == "55I":
            return str(len(self._alarms))
        if body == "98I":
            return str(self.state.get("serial"))
        if body == "N":
            return str(self.state.get("part_number"))

        # Firmware.
        fw = str(self.state.get("firmware") or "")
        if body == "Q":
            return ".".join(fw.split("-")[0].split(".")[:2])
        if body == "*Q":
            return fw.split("-")[0]
        if body == "20Q":
            return fw

        return None

    def _esc_command(self, body: str) -> str | None:
        # ── session settings ──
        m = re.match(r"^([01])ECHO$", body)
        if m:
            self.set_state("echo", int(m.group(1)))
            return f"Echo{m.group(1)}"
        if body == "ECHO":
            value = int(self.state.get("echo") or 0)
            return f"Echo{value}" if self._tagged() else str(value)

        m = re.match(r"^([0-3])CV$", body)
        if m:
            self.set_state("verbose", int(m.group(1)))
            return f"Vrb{m.group(1)}"
        if body == "CV":
            value = self._verbose()
            return f"Vrb{value}" if self._tagged() else str(value)

        # ── device name ──
        if body == "CN":
            name = str(self.state.get("unit_name"))
            return f"Ipn {name}" if self._tagged() else name
        m = re.match(r"^(.+)CN$", body)
        if m:
            name = m.group(1).strip()
            if not name or not re.match(r"^[A-Za-z0-9\-]{1,63}$", name):
                raise _SisError("E13")
            self.set_state("unit_name", name)
            return f"Ipn {name}"

        if body == "CH":
            mac = str(self.state.get("mac"))
            return f"Iph {mac}" if self._tagged() else mac

        if body == "20STAT":
            return f"{self.state.get('temperature_f')}F {self.state.get('temperature_c')}C"

        if body == "LELIC":
            count = self.state.get("licensed_endpoints")
            return (
                '{"licensedFeature":[{"name":"NAVigator Endpoints",'
                f'"description":"Link License {count} Endpoints","status":true,'
                '"serialNumber":"A1PC690","expirationDate":0,"data":"1",'
                '"part_number":"79-3084-01"},'
                '{"name":"Third Party Control","description":"Link License '
                'Third Party Control","status":true,"serialNumber":"A1PC690",'
                '"expirationDate":0,"data":"1","part_number":"79-3085-01"}]}'
            )

        # ── network ──
        m = re.match(r"^([12])\*CISG$", body)
        if m:
            pre = "oob" if m.group(1) == "1" else "nav"
            return (f"{self.state.get(pre + '_ip')}/"
                    f"{self.state.get(pre + '_prefix')}*"
                    f"{self.state.get(pre + '_gateway')}")
        m = re.match(r"^([12])DNSS$", body)
        if m:
            dns = str(self.state.get("dns"))
            return f"Dnss{m.group(1)}*{dns}" if self._tagged() else dns

        # ── reports ──
        m = re.match(r"^Inventory\*([IO])\*RPRT$", body)
        if m:
            kind = m.group(1).lower()
            digits = self._inventory(kind)
            if self._tagged():
                return f"Rprt*Inventory*{m.group(1)}*{digits}"
            return digits

        if body == "Ties*A*RPRT":
            lines = ["Rprt ties", "Output\tInVid\tInAud"]
            for out in sorted(self._ties):
                vid, aud = self._ties[out]
                lines.append(f"{out}\t{vid or '---'}\t{aud or '---'}")
            lines.append("")          # blank line terminates the report
            return CRLF.join(lines)

        if body == "Ties*U*RPRT":
            lines = ["Rprt*ties*U", "Device\tHost"]
            for dev in sorted(self._usb):
                lines.append(f"{dev}\t{self._usb[dev]}")
            lines.append("")
            return CRLF.join(lines)

        # ── alarms ──
        m = re.match(r"^V(\d{1,2})ALRM$", body)
        if m:
            wanted = int(m.group(1))
            rows = self._alarms if wanted == 0 else self._alarms[:wanted]
            lines = [
                f"I/O:{io},Event:{ev},Severity:{sev},Time: {ts}"
                for io, ev, sev, ts in rows
            ]
            lines.append("")
            return CRLF.join(lines) if lines else ""
        m = re.match(r"^C(\d{1,2})ALRM$", body)
        if m:
            wanted = int(m.group(1))
            if wanted == 0:
                self._alarms = []
            else:
                self._alarms = self._alarms[wanted:]
            return f"AlrmC{wanted}"

        # ── endpoint status ──
        m = re.match(r"^([ACP])\*(\d{1,4})([ioIO])DEVP$", body)
        if m:
            what, number, kind = m.group(1), int(m.group(2)), m.group(3).lower()
            roster = self._roster(kind)
            if number not in roster:
                value = 0
            elif what == "A":
                value = 1
            elif what == "C":
                value = 1 if roster[number] == "1" else 0
            else:
                value = 1 if roster[number] == "1" else 0
            if self._tagged():
                return f"Devp{what}*{number}{kind}*{value}"
            return str(value)

        # ── WindoWall / KVM presets ──
        m = re.match(r"^R1\*(\d+)\*(\d+)PRST$", body)
        if m:
            canvas, preset = int(m.group(1)), int(m.group(2))
            if not (1 <= canvas <= 8 and 1 <= preset <= 8):
                raise _SisError("E13")
            self._canvas_preset[canvas] = preset
            return f"PrstR1*{canvas}*{preset}"
        m = re.match(r"^L1\*(\d+)PRST$", body)
        if m:
            canvas = int(m.group(1))
            preset = self._canvas_preset.get(canvas, 0)
            return (f"PrstL1*{canvas}*{preset}" if self._tagged()
                    else str(preset))
        m = re.match(r"^R3\*(\d+)\*(\d+)PRST$", body)
        if m:
            ws, preset = int(m.group(1)), int(m.group(2))
            if not (1 <= ws <= 30 and 1 <= preset <= 30):
                raise _SisError("E13")
            self._workstation_preset[ws] = preset
            return f"PrstR3*{ws}*{preset}"
        m = re.match(r"^L3\*(\d+)PRST$", body)
        if m:
            ws = int(m.group(1))
            preset = self._workstation_preset.get(ws, 0)
            return f"PrstL3*{ws}*{preset}" if self._tagged() else str(preset)

        # Select WindoWall input.
        m = re.match(r"^(\d+)\*(\d+)\*(\d+)!X$", body)
        if m:
            canvas, window, inp = (int(m.group(1)), int(m.group(2)),
                                   int(m.group(3)))
            if not (1 <= canvas <= 8 and 1 <= window <= 64):
                raise _SisError("E13")
            if inp not in self._encoders:
                raise _SisError("E25")
            self._window_input[(canvas, window)] = inp
            return f"Grp{canvas}*{window}*{inp}"
        m = re.match(r"^(\d+)\*(\d+)!X$", body)
        if m:
            canvas, window = int(m.group(1)), int(m.group(2))
            return str(self._window_input.get((canvas, window), 0))

        # ── system reset ──
        if body == "ZQQQ":
            self._encoders, self._decoders = {}, {}
            self._ties, self._usb, self._alarms = {}, {}, []
            self.set_state("unit_name", "NAVigator-13-9C-31")
            return "Zpq"

        # ── ties ──
        return self._tie_command(body)

    def _tie_command(self, body: str) -> str | None:
        # Clear every AV tie / every USB tie.
        if body == "0*!":
            for out in self._ties:
                self._ties[out] = [0, 0]
            self._sync_tie_state()
            return "0 All"
        if body == "0i*^":
            self._usb.clear()
            return "0 Usb"

        # USB tie: <host><i|o>*<device><i|o>^
        m = re.match(r"^(\d{1,4}[ioIO])\*(\d{1,4}[ioIO])\^$", body)
        if m:
            host_n, host_k = self._require_endpoint(m.group(1))
            dev_n, dev_k = self._require_endpoint(m.group(2))
            host, dev = f"{host_n}{host_k}", f"{dev_n}{dev_k}"
            self._usb[dev] = host
            return f"Out{dev} In{host} Usb"

        # Tie one input to all outputs.
        m = re.match(r"^(\d{1,4})\*([!%$])$", body)
        if m:
            inp, kind = int(m.group(1)), m.group(2)
            if inp and inp not in self._encoders:
                raise _SisError("E25")
            for out in self._ties:
                if kind in "!%":
                    self._ties[out][0] = inp
                if kind in "!$":
                    self._ties[out][1] = inp
            self._sync_tie_state()
            return f"{inp} " + {"!": "All", "%": "Vid", "$": "Aud"}[kind]

        # Tie one input to one output.
        m = re.match(r"^(\d{1,4})\*(\d{1,4})([!%$])$", body)
        if m:
            inp, out, kind = int(m.group(1)), int(m.group(2)), m.group(3)
            if inp and inp not in self._encoders:
                raise _SisError("E25")
            if out and out not in self._decoders:
                raise _SisError("E25")
            if out == 0:
                # Untie this input from every output it feeds.
                for o in self._ties:
                    if kind in "!%" and self._ties[o][0] == inp:
                        self._ties[o][0] = 0
                    if kind in "!$" and self._ties[o][1] == inp:
                        self._ties[o][1] = 0
                self._sync_tie_state()
                return f"Out00*In{inp}*All"
            if inp == 0:
                if kind in "!%":
                    self._ties[out][0] = 0
                if kind in "!$":
                    self._ties[out][1] = 0
                self._sync_tie_state()
                return f"Out{out}*In00*All"
            if kind in "!%":
                self._ties[out][0] = inp
            if kind in "!$":
                self._ties[out][1] = inp
            self._sync_tie_state()
            tag = {"!": "All", "%": "Vid", "$": "Aud"}[kind]
            return f"Out{out} In{inp} {tag}"

        # View a tie.
        m = re.match(r"^(\d{1,4})([!%$])$", body)
        if m:
            out, kind = int(m.group(1)), m.group(2)
            if out not in self._ties:
                raise _SisError("E25")
            vid, aud = self._ties[out]
            if kind == "%":
                return str(vid)
            if kind == "$":
                return str(aud)
            if vid != aud:
                # Audio is broken away, so there is no single AV tie to report.
                raise _SisError("E13")
            return str(vid)

        return None

    # ── encapsulation ──

    def _encapsulated(self, command: str) -> str:
        """`{<endpoint>:<inner SIS command>}` -> `{<endpoint>}<reply>`."""
        m = re.match(r"^\{([^:]+):(.*)\}$", command, re.DOTALL)
        if not m:
            return "E10"
        ref, inner = m.group(1).strip(), m.group(2)
        try:
            number, kind = self._require_endpoint(ref)
        except _SisError as e:
            # The NAVigator echoes the identifier it was given even when it
            # cannot resolve it.
            return "{" + ref.lower() + "}" + e.code
        tag = "{" + f"{number}{kind}" + "}"
        inner = inner.rstrip(CR)

        if inner == ESC + "CN":
            name = self._name_of(number, kind) or ""
            return tag + (f"Ipn {name}" if self._tagged() else name)
        mute = re.match(r"^([01])B$", inner)
        if mute:
            return tag + f"Vmt{mute.group(1)}"
        if inner == "Q":
            return tag + "1.01"
        if inner in ("1I", "2I"):
            model = ("NAV E 101" if kind == "i" else "NAV SD 101")
            desc = ("NAV Gigabit Encoder" if kind == "i"
                    else "NAV Gigabit Scaling Decoder")
            return tag + (model if inner == "1I" else desc)
        return tag + "E10"

    # ── unsolicited notices ──

    async def notify_endpoint(self, number: int, kind: str, what: str,
                              value: int) -> None:
        """Push an endpoint-state notice the way the NAVigator does.

        ``what`` is A (assigned), C (connected) or P (online). Untagged below
        verbose 2, which is what makes the driver's verbose-3 ceremony load
        bearing rather than cosmetic.
        """
        roster = self._roster(kind)
        if what == "P":
            roster[number] = "1" if value else "2"
        elif what == "C" and number in roster:
            roster[number] = "1" if value else "3"
        elif what == "A":
            if value:
                roster.setdefault(number, "1")
            else:
                roster.pop(number, None)
        frame = (f"Devp{what}*{number}{kind}*{value}" if self._tagged()
                 else str(value))
        await self.push((frame + CRLF).encode("latin-1"))

    async def notify_hotkey(self, number: int, kind: str,
                            combo: str = "P") -> None:
        """Push a KVM hot-key notice (`HkdmP` / `HkdmK`)."""
        await self.push(
            (f"Hkdm{combo}*{number}{kind}" + CRLF).encode("latin-1"))


class _SisError(Exception):
    """An SIS error code the NAVigator answers with (E10, E13, E25, ...)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
