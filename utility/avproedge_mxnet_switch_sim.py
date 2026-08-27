"""
AVPro Edge MXnet network switch — Simulator.

Stateful TCP simulator for the ``avproedge_mxnet_switch`` driver. It reproduces
the switch CLI closely enough to drive the whole driver without hardware: the
``login:`` / ``Password:`` handshake, the ``switch#`` prompt, ``terminal length
0``, every ``show`` the driver polls, and the config-mode sequences its commands
run (``config`` / ``interface ethernet`` / ``power inline`` / ``shutdown`` / …).

It is reached over raw TCP, which is how the driver's ``transport: tcp``
(telnet) mode connects. The driver's CLI framing is identical over SSH and TCP,
so exercising it over TCP here validates the real connect -> poll -> command
path.

Modelled on the bench AC-MXNET-SW8P: 8 copper PoE ports plus 4 SFP+ uplinks,
with an MXnet decoder on 1/0/1 and an encoder on 1/0/3, both drawing ~4 W and
joined to the MXnet control-plane multicast groups.

**Two awkward behaviours are modelled deliberately, because they are the ones
that mislead.**

1. ``power inline reset`` is accepted and does *nothing* — which is what the
   real firmware (V705R002C013) does. A simulator that helpfully power-cycled
   on that command would make the driver look correct while the field behaviour
   was broken, which is exactly the failure this catalog has been bitten by
   before. The driver cuts and restores power instead, and the simulator
   rewards only that.
2. The four SFP+ ports answer ``Power inline is not supported on interface
   EthernetX`` rather than a table row, so the driver's handling of a
   non-PoE port on a PoE switch is exercised rather than assumed.

Login-failure fidelity: any credentials are accepted **except** a username or
password of ``invalid``, which re-prompts — matching the sentinel the platform's
YAML auto-simulator uses, so the driver's auth-rejection path is testable.

License: MIT.
"""

from __future__ import annotations

from openavc.simulator.tcp_simulator import TCPSimulator

_HOSTNAME = "switch"
# Synthetic endpoint identifiers, so the MAC table and the IGMP membership
# table agree with each other the way they do on real hardware.
_DECODER_MAC = "18-8a-6a-00-00-12"
_ENCODER_MAC = "18-8a-6a-00-00-11"
_LAPTOP_MAC = "aa-bb-cc-00-00-21"
# Whatever the switch is uplinked to. A real AV switch is rarely an island, and
# the port carrying the rest of the building learns every MAC on it -- which is
# how a "power-cycle this port" lands on the switch's own path back to OpenAVC.
_UPLINK_MACS = ["aa-bb-cc-00-00-31", "aa-bb-cc-00-00-32",
                "aa-bb-cc-00-00-33", "aa-bb-cc-00-00-34"]
_AV_GROUPS = ["225.1.0.0", "225.1.0.1", "225.2.0.20", "225.3.0.20",
              "225.4.0.20", "225.7.0.20"]


def _seed_ports() -> dict[str, dict]:
    ports: dict[str, dict] = {}
    for n in range(1, 9):        # 1/0/1 .. 1/0/8 — copper, PoE-capable
        ports[f"1/0/{n}"] = {
            "admin_up": True, "link": False, "speed": "auto", "duplex": "auto",
            "media": "G-TX", "vlan": "1", "alias": "",
            "poe_capable": True, "poe_admin": True, "poe_on": False,
            "poe_mw": 0, "poe_max_mw": 30000, "poe_ma": 0, "poe_v": 0,
            "poe_priority": "low", "poe_class": 0,
            "rx_packets": 0, "rx_bytes": 0, "tx_packets": 0, "tx_bytes": 0,
            "rx_rate": 0, "tx_rate": 0,
            "input_errors": 0, "crc_errors": 0, "output_errors": 0,
            "macs": [], "groups": [],
        }
    for n in range(9, 13):       # 1/0/9 .. 1/0/12 — SFP+, no PoE
        ports[f"1/0/{n}"] = dict(
            ports["1/0/1"], media="SFP+", poe_capable=False, poe_admin=False,
            macs=[], groups=[])
    # An MXnet decoder and encoder, powered and streaming.
    ports["1/0/1"].update(
        link=True, speed="a-1G", duplex="a-FULL", poe_on=True, poe_mw=3600,
        poe_ma=70, poe_v=52, poe_class=3, macs=[_DECODER_MAC],
        groups=list(_AV_GROUPS), rx_packets=218925, rx_bytes=15512798,
        tx_packets=217822, tx_bytes=14908456, rx_rate=17754, tx_rate=15891)
    ports["1/0/3"].update(
        link=True, speed="a-1G", duplex="a-FULL", poe_on=True, poe_mw=4200,
        poe_ma=81, poe_v=52, poe_class=3, macs=[_ENCODER_MAC],
        groups=list(_AV_GROUPS), rx_packets=205796, rx_bytes=13614066,
        tx_packets=230481, tx_bytes=16762126, rx_rate=15568, tx_rate=18077)
    # A control PC, so not everything on the switch is MXnet gear.
    ports["1/0/5"].update(
        link=True, speed="a-1G", duplex="a-FULL", macs=[_LAPTOP_MAC],
        groups=["239.255.255.250"], rx_packets=3546, rx_bytes=443035,
        tx_packets=844, tx_bytes=148159, rx_rate=900, tx_rate=400)
    # The uplink: linked, drawing no PoE (the far end powers itself), and
    # carrying everything that is not on this switch.
    ports["1/0/7"].update(
        link=True, speed="a-1G", duplex="a-FULL", macs=list(_UPLINK_MACS),
        alias="uplink", rx_packets=891244, rx_bytes=402118755,
        tx_packets=774310, tx_bytes=98221043, rx_rate=44120, tx_rate=21008)
    return ports


class AVProEdgeMXnetSwitchSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "avproedge_mxnet_switch",
        "name": "AVPro Edge MXnet Switch Simulator",
        "category": "utility",
        "transport": "tcp",
        "default_port": 23,
        "delimiter": "\r\n",
        # Seeds are the values that go ON THE WIRE, which is a different
        # namespace from the driver's published state variables (the driver
        # derives CPU usage from idle, watts from milliwatts, and so on).
        "initial_state": {
            "model": "AC-MXNET-SW8P",
            "firmware": "V705R002C013",
            "bootrom": "7.5.15",
            "hardware": "1.0.2",
            "cpld": "5.00",
            "serial": "SW100126030400001",
            "base_mac": "18:8a:6a:00:00:01",
            "cpu_mac": "18:8a:6a:00:00:02",
            "uptime": "0 weeks, 0 days, 1 hours, 52 minutes",
            "temperature_c": 44,
            # The CLI reports CPU IDLE, not usage — seed what it prints.
            "cpu_idle_5s": 82,
            "cpu_idle_30s": 83,
            "cpu_idle_5m": 82,
            "memory_total_mb": 256,
            "memory_usage_pct": 60.87,
            # PoE controller. Consumed/remaining are summed from the port table
            # rather than seeded, so they stay honest when a port is switched
            # off from the simulator UI.
            "poe_main_status": "On",
            "poe_budget_w": 125,
            "poe_police": False,
            "poe_legacy": False,
            "poe_pse_type": "RTL RSK",
            "poe_sw_version": "0.0.0.3",
            "igmp_snooping": True,
            "igmp_querier": True,
            "igmp_vlan": 1,
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
            {"type": "indicator", "key": "uptime", "label": "Uptime"},
            {"type": "slider", "key": "temperature_c", "min": 0, "max": 90,
             "label": "Temperature (C)"},
            {"type": "slider", "key": "cpu_idle_5s", "min": 0, "max": 100,
             "label": "CPU Idle 5s (%)"},
            {"type": "slider", "key": "memory_usage_pct", "min": 0, "max": 100,
             "label": "Memory Used (%)"},
            {"type": "select", "key": "poe_main_status", "options": ["On", "Off"],
             "label": "PoE Controller"},
            {"type": "slider", "key": "poe_budget_w", "min": 0, "max": 1000,
             "label": "PoE Budget (W)"},
            {"type": "toggle", "key": "poe_police", "label": "PoE Policing"},
            {"type": "toggle", "key": "poe_legacy", "label": "PoE Legacy PDs"},
            {"type": "toggle", "key": "igmp_snooping", "label": "IGMP Snooping"},
            {"type": "toggle", "key": "igmp_querier", "label": "IGMP Querier"},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._ports = _seed_ports()
        self._stage = "login"      # login -> password -> ready
        self._mode = "priv"        # priv -> config -> interface
        self._cfg_iface = ""
        self._await_reload = False

    # ── session ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        self._stage = "login"
        self._mode = "priv"
        self._cfg_iface = ""
        return b"\r\nlogin:"

    def _prompt(self) -> str:
        if self._mode == "config":
            return f"\r\n{_HOSTNAME}(config)#"
        if self._mode == "interface":
            return f"\r\n{_HOSTNAME}(config-if-ethernet{self._cfg_iface})#"
        return f"\r\n{_HOSTNAME}#"

    # ── command dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("latin-1", errors="replace").strip()
        low = line.lower()

        # Login handshake. "invalid" is the designated bad credential so the
        # driver's rejection path is reachable in simulation.
        if self._stage == "login":
            if low == "invalid":
                return b"\r\nlogin:"
            self._stage = "password"
            return b"\r\nPassword:"
        if self._stage == "password":
            if low == "invalid":
                self._stage = "login"
                return b"\r\nLogin incorrect\r\nlogin:"
            self._stage = "ready"
            return ("\r\n*****" + self._prompt()).encode("latin-1")

        if self._await_reload:
            self._await_reload = False
            return ("\r\n" + self._prompt().lstrip("\r\n")).encode("latin-1")
        if low == "reload":
            self._await_reload = True
            # The device pauses at the confirmation with no prompt after it, so
            # the driver frames on the question rather than on a prompt.
            return b"\r\nProcess with reboot? [Y/N]"

        body = self._dispatch(line, low)
        if body is None:
            body = "\r\n% Unknown command."
        text = (body.rstrip("\r\n") + "\r\n" if body.strip() else "") \
            + self._prompt().lstrip("\r\n")
        return ("\r\n" + text).encode("latin-1")

    def _dispatch(self, line: str, low: str) -> str | None:
        if low.startswith("terminal length") or low.startswith("terminal no length"):
            return ""
        if low in ("exit", "quit", "end"):
            if self._mode == "interface":
                self._mode = "config"
            elif self._mode == "config":
                self._mode = "priv"
            return ""
        if low == "config" or low.startswith("config "):
            self._mode = "config"
            return ""
        if self._mode in ("config", "interface") and low.startswith("interface ethernet"):
            iface = line.split()[-1].strip()
            if iface in self._ports:
                self._cfg_iface = iface
                self._mode = "interface"
                return ""
            return "\r\n% Invalid input."
        if low == "write" or low.startswith("write "):
            return "\r\nSaving current configuration...\r\nOK!"
        if low.startswith("clear counters"):
            return self._clear_counters(line)
        if low.startswith("virtual-cable-test"):
            return self._cable_test(line)
        if self._mode == "interface":
            return self._interface_command(line, low)
        if self._mode == "config":
            return self._config_command(low)
        if low.startswith("show "):
            return self._render_show(low)
        return None

    # ── mutation ──

    def _config_command(self, low: str) -> str | None:
        if low in ("power inline police enable", "no power inline police enable"):
            self.set_state("poe_police", not low.startswith("no"))
            return ""
        if low in ("power inline legacy enable", "no power inline legacy enable"):
            self.set_state("poe_legacy", not low.startswith("no"))
            return ""
        if low in ("power inline enable", "no power inline enable"):
            return ""   # global PoE toggle; not modelled beyond acceptance
        return None

    def _interface_command(self, line: str, low: str) -> str | None:
        """``line`` keeps the original casing — a port description is text the
        user typed, so it must not come back lowercased."""
        port = self._ports.get(self._cfg_iface)
        if port is None:
            return "\r\n% Invalid input."

        if low == "power inline reset":
            # DELIBERATE NO-OP — see the module docstring. The real firmware
            # accepts this and never interrupts power, so a driver that relies
            # on it must fail here too.
            return ""
        if low in ("power inline enable", "no power inline enable"):
            if not port["poe_capable"]:
                return (f"\r\nPower inline is not supported on interface "
                        f"Ethernet{self._cfg_iface}.")
            enable = not low.startswith("no")
            port["poe_admin"] = enable
            if enable:
                # Power comes back only if something is actually attached.
                powered = bool(port["macs"])
                port.update(poe_on=powered, poe_mw=3800 if powered else 0,
                            poe_ma=72 if powered else 0,
                            poe_v=52 if powered else 0,
                            poe_class=3 if powered else 0)
            else:
                port.update(poe_on=False, poe_mw=0, poe_ma=0, poe_v=0,
                            poe_class=0)
            return ""
        if low.startswith("power inline priority "):
            value = low.rsplit(" ", 1)[-1]
            if value not in ("critical", "high", "low"):
                return "\r\n% Invalid input."
            port["poe_priority"] = value
            return ""
        if low.startswith("power inline max "):
            try:
                port["poe_max_mw"] = int(low.rsplit(" ", 1)[-1])
            except ValueError:
                return "\r\n% Invalid input."
            return ""
        if low in ("shutdown", "no shutdown"):
            up = low.startswith("no")
            port["admin_up"] = up
            if not up:
                port.update(link=False, speed="auto", duplex="auto")
            elif port["macs"]:
                port.update(link=True, speed="a-1G", duplex="a-FULL")
            return ""
        if low.startswith("no description"):
            port["alias"] = ""
            return ""
        if low.startswith("description"):
            port["alias"] = line.partition("description")[2].strip().strip('"')
            return ""
        return None

    def _clear_counters(self, line: str) -> str:
        iface = line.split()[-1].strip()
        targets = [iface] if iface in self._ports else list(self._ports)
        for name in targets:
            self._ports[name].update(
                rx_packets=0, rx_bytes=0, tx_packets=0, tx_bytes=0,
                input_errors=0, crc_errors=0, output_errors=0)
        return ""

    def _cable_test(self, line: str) -> str:
        iface = line.split()[-1].strip()
        if iface not in self._ports:
            return "\r\n% Invalid input."
        port = self._ports[iface]
        status = "Normal" if port["link"] else "Open"
        length = "3" if port["link"] else "0"
        return (f"\r\nInterface Ethernet{iface} :\r\n"
                f"        Pair A: {status}, Length {length}m\r\n"
                f"        Pair B: {status}, Length {length}m")

    # ── rendering ──

    def _poe_used_w(self) -> int:
        return round(sum(p["poe_mw"] for p in self._ports.values()) / 1000)

    def _render_show(self, low: str) -> str | None:
        if low.startswith("show version"):
            return self._show_version()
        if low.startswith("show interface ethernet status"):
            return self._show_iface_status()
        if low.startswith("show power inline interface power"):
            return self._show_poe_power()
        if low.startswith("show power inline interface"):
            return self._show_poe_ports(low)
        if low.startswith("show power inline"):
            return self._show_poe_global()
        if low.startswith("show temperature"):
            return f"\r\nTemperature: {self.state.get('temperature_c', 44)}C/111F"
        if low.startswith("show cpu usage"):
            s = self.state
            return ("\r\n\r\n"
                    f"Last  5 second CPU IDLE:  {s.get('cpu_idle_5s', 82)}%\r\n"
                    f"Last 30 second CPU IDLE:  {s.get('cpu_idle_30s', 83)}%\r\n"
                    f"Last  5 minute CPU IDLE:  {s.get('cpu_idle_5m', 82)}%\r\n"
                    f"From  running  CPU IDLE:  {s.get('cpu_idle_5m', 82)}%")
        if low.startswith("show memory usage"):
            total = self.state.get("memory_total_mb", 256)
            pct = self.state.get("memory_usage_pct", 60.87)
            free = int(total * 1024 * 1024 * (100 - float(pct)) / 100)
            return (f"\r\n\r\nThe memory total {total} MB , free {free} bytes , "
                    f"usage is {pct}%")
        if low.startswith("show ip igmp snooping vlan"):
            return self._show_igmp_vlan()
        if low.startswith("show ip igmp snooping"):
            return self._show_igmp_global()
        if low.startswith("show mac-address-table"):
            return self._show_mac_table()
        if low.startswith("show vlan"):
            return self._show_vlan()
        if low.startswith("show transceiver"):
            return ("\r\nInterface            Temp(C)            Voltage(V)"
                    "            Bias(mA)            RX Power(dBM)            "
                    "TX Power(dBM)\r\n---------            --------            "
                    "----------            --------            -------------"
                    "            -------------")
        if low.startswith("show arp"):
            return ("\r\nARP Unicast Items: 0, Valid: 0, Matched: 0\r\n"
                    "Address          Hardware Addr      Interface     Port")
        if low.startswith("show interface ethernet"):
            return self._show_iface_detail(low)
        if low.startswith("show running-config"):
            return "\r\n!\r\nhostname switch\r\n!\r\nend"
        return None

    def _show_version(self) -> str:
        s = self.state
        return (
            f"\r\n  {s.get('model')} Device, Compiled on Mar 11 13:11:45 2025\r\n"
            "  sysLocation 2222 E 52ND STREET NORTH, SIOUX FALLS, SD 57104, "
            "United States\r\n"
            f"  CPU Mac {s.get('cpu_mac')}\r\n"
            f"  Vlan MAC {s.get('base_mac')}\r\n"
            f"  SoftWare Package Version {s.get('firmware')}\r\n"
            f"  BootRom Version {s.get('bootrom')}\r\n"
            f"  HardWare Version {s.get('hardware')}\r\n"
            f"  CPLD Version {s.get('cpld')}\r\n"
            f"  Serial No.:{s.get('serial')}\r\n"
            "  COPYRIGHT(C) AVPROGLOBAL HLDGS 2025\r\n"
            "  All rights reserved\r\n"
            "  Last reboot is cold reset.\r\n"
            f"  Uptime is {s.get('uptime')}"
        )

    def _show_iface_status(self) -> str:
        out = ["\r\nCodes: A-Down - administratively down, E-Down - errdisable "
               "down, a - auto, f - force, G - Gigabit",
               "",
               "Interface       Link/Protocol  Speed   Duplex  Vlan   Type"
               "            Alias Name"]
        for name, p in self._ports.items():
            if not p["admin_up"]:
                link = "A-Down/DOWN"
            elif p["link"]:
                link = "UP/UP"
            else:
                link = "DOWN/DOWN"
            out.append(
                f"{name:<16}{link:<15}{p['speed']:<8}{p['duplex']:<8}"
                f"{p['vlan']:<7}{p['media']:<16}{p['alias']}".rstrip())
        return "\r\n".join(out)

    def _show_poe_ports(self, low: str) -> str:
        # An explicit interface argument narrows the table to one row.
        wanted = list(self._ports)
        tail = low.replace("show power inline interface", "").strip()
        tail = tail.replace("ethernet", "").strip()
        if tail:
            wanted = [tail] if tail in self._ports else []
            if not wanted:
                return "\r\n% Invalid input."
        out = ["", "Interface       Status  Oper   Power(mW) Max-type Max(mW) "
               "Current(mA) Volt(V) Priority Class",
               "--------------- ------- ------ --------- -------- ------- "
               "----------- ------- -------- -----"]
        trailer = []
        for name in wanted:
            p = self._ports[name]
            if not p["poe_capable"]:
                trailer.append(
                    f"Power inline is not supported on interface Ethernet{name}.")
                continue
            status = "enable" if p["poe_admin"] else "disable"
            oper = "on" if p["poe_on"] else "off"
            out.append(
                f"Ethernet{name:<8}{status:>8}{oper:>7}{p['poe_mw']:>10}"
                f"{'class':>9}{p['poe_max_mw']:>8}{p['poe_ma']:>12}"
                f"{p['poe_v']:>8}{p['poe_priority']:>9}{p['poe_class']:>6}")
        return "\r\n".join(out + trailer)

    def _show_poe_power(self) -> str:
        used = self._poe_used_w()
        budget = int(self.state.get("poe_budget_w", 125))
        out = ["", "member 1:--------------------------", "",
               f"Available:{budget}W\t Used:{used}W\t "
               f"Remaining:{budget - used}W", "",
               "Interface       Power(mW)        Interface       Power(mW)",
               "--------------- ---------       ---------------  ---------"]
        poe_ports = [(n, p) for n, p in self._ports.items() if p["poe_capable"]]
        for i in range(0, len(poe_ports), 2):
            pair = poe_ports[i:i + 2]
            row = ""
            for name, p in pair:
                row += f"Ethernet{name:<12}{p['poe_mw']:>9}       "
            out.append(row.rstrip())
        out += [f"Power inline is not supported on interface Ethernet{n}."
                for n, p in self._ports.items() if not p["poe_capable"]]
        return "\r\n".join(out)

    def _show_poe_global(self) -> str:
        used = self._poe_used_w()
        budget = int(self.state.get("poe_budget_w", 125))
        s = self.state
        return (
            f"\r\n\r\nPower Inline Status: {s.get('poe_main_status', 'On')}\r\n"
            f"Power Available: {budget} W\r\n"
            f"Power Used: {used} W\r\n"
            f"Power Remaining: {budget - used} W\r\n"
            f"Police: {'On' if s.get('poe_police') else 'Off'}\r\n"
            f"Legacy: {'On' if s.get('poe_legacy') else 'Off'}\r\n"
            "Disconnect: Dc\r\n"
            "Mode: Signal\r\n"
            f"Pse Type: {s.get('poe_pse_type', 'RTL RSK')}\r\n"
            f"SW Version: {s.get('poe_sw_version', '0.0.0.3')}"
        )

    def _show_igmp_global(self) -> str:
        on = self.state.get("igmp_snooping", True)
        vlan = self.state.get("igmp_vlan", 1)
        querier = " (querier)" if self.state.get("igmp_querier", True) else ""
        line = (f"Igmp snooping is turned on for vlan {vlan}"
                f"{'(querier)' if querier else ''}")
        return (f"\r\nGlobal igmp snooping status   :"
                f"{'Enabled' if on else 'Disabled'}\r\n"
                + (line if on else ""))

    def _show_igmp_vlan(self) -> str:
        vlan = self.state.get("igmp_vlan", 1)
        out = [f"\r\nIgmp snooping information for vlan {vlan}", "",
               "Igmp snooping L3 multicasting                     :stopped",
               "Igmp snooping L2 general querier                  "
               ":Yes(COULD_QUERY)",
               "Igmp snooping query-interval                      :125(s)", "",
               "IGMP Snooping Connect Group Membership ",
               "Note:*-All Source, (S)- Include Source, [S]-Exclude Source",
               "Groups          Sources             Ports               "
               "Exptime  SrcMac              System Level"]
        # Group-major with continuation rows, exactly as the switch prints it.
        by_group: dict[str, list[tuple[str, str]]] = {}
        for name, p in self._ports.items():
            for g in p["groups"]:
                mac = p["macs"][0].replace("-", ":").upper() if p["macs"] else ""
                by_group.setdefault(g, []).append((name, mac))
        count = 0
        for group, members in by_group.items():
            count += 1
            for idx, (name, mac) in enumerate(members):
                head = f"{group:<16}*{'':<19}" if idx == 0 else " " * 36
                out.append(f"{head}Ethernet{name:<12}00:04:01 {mac:<19} V2")
        out += ["", f"IGMP snooping vlan {vlan} current/limit groups "
                    f":{count}/1000", "",
                f"Igmp snooping vlan {vlan} mrouter port",
                'Note:"!"-static mrouter port', ""]
        return "\r\n".join(out)

    def _show_mac_table(self) -> str:
        out = ["\r\nRead mac address table....",
               "Vlan Mac Address                 Type    Creator   Ports",
               "---- --------------------------- ------- "
               "-------------------------------------"]
        for name, p in self._ports.items():
            for mac in p["macs"]:
                out.append(f"1    {mac:<27} DYNAMIC Hardware Ethernet{name}")
        base = str(self.state.get("base_mac", "")).replace(":", "-")
        out.append(f"1    {base:<27} STATIC  System   CPU")
        return "\r\n".join(out)

    def _show_vlan(self) -> str:
        names = list(self._ports)
        out = ["\r\nVLAN Name         Type       Media     Ports",
               "---- ------------ ---------- --------- "
               "----------------------------------------"]
        first = True
        for i in range(0, len(names), 2):
            pair = "".join(f"Ethernet{n:<12}" for n in names[i:i + 2])
            prefix = "1    default      Static     ENET      " if first \
                else " " * 39
            out.append(prefix + pair.rstrip())
            first = False
        return "\r\n".join(out)

    def _show_iface_detail(self, low: str) -> str:
        """``show interface ethernet <name|range>`` — one block per interface."""
        arg = low.replace("show interface ethernet", "").strip()
        names = self._expand_range(arg)
        if not names:
            return "\r\ninterface error!"
        out = ["\r\nInterface brief:"]
        for name in names:
            p = self._ports[name]
            state = "up" if p["link"] else "down"
            idx = int(name.split("/")[-1])
            alias = p["alias"] or "(null)"
            out += [
                f"  Ethernet{name} is {state}, line protocol is {state}",
                f"  Ethernet{name} is layer 2 port, alias name is {alias}, "
                f"index is {idx}",
                f"  Hardware is Gigabit-TX, address is "
                f"{str(self.state.get('cpu_mac', '')).replace(':', '-')}",
                f"  PVID is {p['vlan']}",
                "  MTU 10218 bytes, BW 1000000 Kbit",
                "  Encapsulation ARPA, Loopback not set",
                "Statistics:",
                f"  5 minute input rate {p['rx_rate']} bits/sec, 32 packets/sec",
                f"  5 minute output rate {p['tx_rate']} bits/sec, 34 packets/sec",
                f"  The last 5 second input rate {p['rx_rate']} bits/sec, "
                f"31 packets/sec",
                f"  The last 5 second output rate {p['tx_rate']} bits/sec, "
                f"34 packets/sec",
                "  Input packets statistics:",
                f"    {p['rx_packets']} input packets, {p['rx_bytes']} bytes, "
                f"0 no buffer",
                f"    {p['input_errors']} input errors, {p['crc_errors']} CRC, "
                "0 frame alignment, 0 overrun, 0 ignored,",
                "  Output packets statistics:",
                f"    {p['tx_packets']} output packets, {p['tx_bytes']} bytes, "
                f"0 underruns",
                f"    {p['output_errors']} output errors, 0 collisions, "
                "0 late collisions, 0 pause frame",
            ]
        return "\r\n".join(out)

    def _expand_range(self, arg: str) -> list[str]:
        """'1/0/3' or '1/0/1-12' -> the interface names it covers."""
        arg = arg.strip()
        if not arg:
            return []
        if arg in self._ports:
            return [arg]
        parts = arg.split("/")
        if len(parts) != 3 or "-" not in parts[2]:
            return []
        lo, _, hi = parts[2].partition("-")
        if not (lo.isdigit() and hi.isdigit()):
            return []
        names = [f"{parts[0]}/{parts[1]}/{n}" for n in range(int(lo), int(hi) + 1)]
        return [n for n in names if n in self._ports]
