"""
NETGEAR M4250 / M4350 AV Line switch — Simulator.

Stateful TCP simulator for the netgear_m4250_m4350 driver. It reproduces the
switch CLI closely enough to drive the whole driver without hardware: the
login banner and prompt, ``enable`` into Privileged EXEC, ``terminal length 0``,
the ``show`` commands the driver polls, and the config-mode sequences its
commands run (``configure`` / ``interface`` / ``poe`` / ``shutdown`` / ...).

It is reached over raw TCP, which is how the driver's ``transport: tcp``
(telnet) mode connects to it. The driver's CLI framing is identical over SSH
and TCP, so exercising it over TCP here validates the real connect -> poll ->
command path.

The switch is modelled as M4350 (``unit/slot/port`` naming) with eight PoE
ports and two SFP uplinks; two ports deliver power, mirroring a small AV-over-IP
room. Rendered tables use the same aligned-column layout the driver parses.

License: MIT.
"""

from __future__ import annotations

from simulator.tcp_simulator import TCPSimulator

_NAME = "M4350-AVSWITCH"


def _tbl(cols: list[tuple[str, int]], rows: list[tuple]) -> str:
    header = " ".join(name.ljust(w) for name, w in cols)
    sep = " ".join("-" * w for _, w in cols)
    out = [header, sep]
    for row in rows:
        out.append(" ".join(str(v).ljust(w) for (_, w), v in zip(cols, row)))
    return "\n".join(out)


def _seed_ports() -> dict[str, dict]:
    ports: dict[str, dict] = {}
    for n in range(1, 9):  # 1/0/1 .. 1/0/8 — PoE ports
        ports[f"1/0/{n}"] = {
            "admin": "Enable", "link": "Down", "speed": "",
            "poe_capable": True, "poe_admin": "Enable", "poe_status": "Searching",
            "poe_power_mw": 0, "poe_class": "Unknown", "poe_current": 0,
            "poe_voltage": 0, "poe_priority": "Low", "description": "",
            "media": "RJ45", "vlan": "1",
        }
    # 1/0/1: camera delivering power; 1/0/2: display delivering power.
    ports["1/0/1"].update(link="Up", speed="1000 Full", poe_status="Delivering Power",
                          poe_power_mw=4000, poe_class="4", poe_current=74,
                          poe_voltage=54, description="Camera 1", vlan="10")
    ports["1/0/2"].update(link="Up", speed="1000 Full", poe_status="Delivering Power",
                          poe_power_mw=3800, poe_class="4", poe_current=72,
                          poe_voltage=53, poe_priority="High",
                          description="Display Left", vlan="20")
    ports["1/0/3"].update(link="Up", speed="1000 Full", description="AP Lobby")
    # SFP uplinks (non-PoE).
    ports["1/0/49"] = {
        "admin": "Enable", "link": "Up", "speed": "10G Full",
        "poe_capable": False, "poe_admin": "Disable", "poe_status": "Disabled",
        "poe_power_mw": 0, "poe_class": "Unknown", "poe_current": 0,
        "poe_voltage": 0, "poe_priority": "Low", "description": "Uplink",
        "media": "SFP+", "vlan": "1",
    }
    ports["1/0/50"] = dict(ports["1/0/49"], link="Down", speed="",
                           description="", media="SFP+")
    return ports


class NetgearM4250M4350Simulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "netgear_m4250_m4350",
        "name": "NETGEAR M4250 / M4350 Simulator",
        "category": "utility",
        "transport": "tcp",
        "default_port": 23,
        "delimiter": "\r\n",
        # Every value the rendered CLI output carries lives here, rather than
        # inside the renderer that prints it. The switch's reason for being on
        # a control system is its telemetry — temperature, fans, PSU
        # redundancy, PoE budget — and a constant baked into a render function
        # cannot be moved, so none of it could be exercised: a macro reacting
        # to a failed fan or a PoE budget running out had nothing to react to.
        # Held in state, each one is drivable from the Simulator UI.
        "initial_state": {
            # Identity — show version
            "model": "M4350-24X4F",
            "firmware": "14.0.6.17",
            "boot_version": "B1.0.0.6",
            "serial_number": "7AB12C3D4E5F6",
            "mac_address": "BC:A5:11:22:33:44",
            "system_name": "AV-CORE-SW1",
            "uptime": "12 days 4 hrs 9 mins 51 secs",
            # Environment — show environment
            "temperature_c": 41,
            "temperature_state": "Normal",
            "fan_state": "Operational",
            "fan_speed": 3200,
            "fan_duty": 30,
            "psu_state": "Operational",
            "psu_count": 2,
            "psu_redundancy_enabled": True,
            "psu_redundancy_active": True,
            # Load — show process cpu
            "cpu_5s": 3.20,
            "cpu_60s": 2.80,
            "cpu_300s": 2.50,
            "mem_free_kb": 268435456,
            "mem_alloc_kb": 805306368,
            # PoE controller — show poe. Consumed power and the delivering
            # port count are summed from the port table rather than seeded, so
            # they stay honest when a port is switched off from the UI.
            "poe_main_status": "ON",
            "poe_total_power_w": 720.0,
            "poe_threshold_power_w": 648.0,
            "poe_usage_threshold": 90,
            "poe_power_mgmt_mode": "Dynamic",
            "poe_firmware": "1.2.0.8",
            # Multicast — show igmpsnooping / querier
            "igmp_snooping": True,
            "igmp_querier": True,
            "igmp_querier_address": "10.20.0.1",
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
            {"type": "indicator", "key": "uptime", "label": "Uptime"},
            {"type": "slider", "key": "temperature_c", "min": 0, "max": 90,
             "label": "Temperature (C)"},
            {"type": "select", "key": "temperature_state",
             "options": ["Normal", "Warning", "Critical"],
             "label": "Temperature State"},
            {"type": "select", "key": "fan_state",
             "options": ["Operational", "Failed", "Not Present"],
             "label": "Fan State"},
            {"type": "select", "key": "psu_state",
             "options": ["Operational", "Failed", "Not Present"],
             "label": "PSU State"},
            {"type": "toggle", "key": "psu_redundancy_active",
             "label": "PSU N+1 Active"},
            {"type": "slider", "key": "cpu_5s", "min": 0, "max": 100,
             "label": "CPU 5s (%)"},
            {"type": "select", "key": "poe_main_status",
             "options": ["ON", "OFF"], "label": "PoE Controller"},
            {"type": "slider", "key": "poe_total_power_w", "min": 0, "max": 1440,
             "label": "PoE Budget (W)"},
            {"type": "toggle", "key": "igmp_snooping", "label": "IGMP Snooping"},
            {"type": "toggle", "key": "igmp_querier", "label": "IGMP Querier"},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._ports = _seed_ports()
        self._mode = "user"   # user -> priv -> config -> interface
        self._cfg_iface = ""
        self._await_reload = False

    async def on_client_connected(self, client_id: str) -> bytes | None:
        banner = (
            "\r\n"
            "(NETGEAR Switch) AV Line Managed Switch\r\n"
            "\r\n"
            "User:admin logged in.\r\n"
        )
        self._mode = "user"
        return (banner + self._prompt()).encode("latin-1")

    def _prompt(self) -> str:
        if self._mode == "user":
            return f"\r\n({_NAME}) >"
        if self._mode == "config":
            return f"\r\n({_NAME}) (Config)#"
        if self._mode == "interface":
            return f"\r\n({_NAME}) (Interface {self._cfg_iface})#"
        return f"\r\n({_NAME}) #"

    # ── command dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("latin-1", errors="replace").strip()
        low = line.lower()

        if self._await_reload:
            self._await_reload = False
            return ("\r\n" + self._prompt().lstrip("\r\n")).encode("latin-1")

        if low == "reload":
            self._await_reload = True
            # The device waits at the confirmation with NO prompt printed after
            # it, so the driver frames on the (y/n) question, not a prompt.
            return b"\r\nAre you sure you want to reload the stack? (y/n)"

        body = self._dispatch(line, low)
        if body is None:
            body = ""
        text = (body.rstrip("\r\n") + "\r\n" if body else "") + self._prompt().lstrip("\r\n")
        # Prepend the leading CRLF the device prints before output / prompt.
        return ("\r\n" + text).encode("latin-1")

    def _dispatch(self, line: str, low: str) -> str | None:
        # Session setup
        if low == "enable":
            self._mode = "priv"
            return ""
        if low.startswith("terminal length"):
            return ""
        if low in ("exit", "logout", "quit", "end"):
            if self._mode == "interface":
                self._mode = "config"
            elif self._mode == "config":
                self._mode = "priv"
            return ""
        if low == "configure":
            self._mode = "config"
            return ""
        if low.startswith("interface "):
            self._cfg_iface = line.split(None, 1)[1].strip()
            self._mode = "interface"
            return ""

        # Config / interface mutations
        if self._mode in ("config", "interface"):
            mutated = self._mutate(line, low)
            if mutated is not None:
                return mutated

        # Privileged actions (reload is handled in handle_command)
        if low.startswith("write memory"):
            return "Config file 'startup-config' created successfully."
        if low.startswith("clear counters"):
            return ""
        if low.startswith("cablestatus"):
            return self._render_cablestatus(line)

        # show commands
        if low.startswith("show "):
            return self._render_show(low)

        return ""  # unknown — just re-prompt, like a lenient CLI

    def _mutate(self, line: str, low: str) -> str | None:
        iface = self._cfg_iface
        port = self._ports.get(iface)
        if low == "poe reset":
            if iface and port:
                pass  # transient; nothing persistent changes
            else:  # global poe reset
                for p in self._ports.values():
                    if p["poe_capable"]:
                        p.setdefault("poe_status", "Searching")
            return ""
        if not port:
            return None
        if low == "poe":
            port["poe_admin"] = "Enable"
            return ""
        if low == "no poe":
            port["poe_admin"] = "Disable"
            port["poe_status"] = "Disabled"
            port["poe_power_mw"] = 0
            return ""
        if low == "shutdown":
            port["admin"] = "Disable"
            port["link"] = "Down"
            port["speed"] = ""
            return ""
        if low == "no shutdown":
            port["admin"] = "Enable"
            return ""
        if low.startswith("poe priority"):
            port["poe_priority"] = line.split()[-1].capitalize()
            return ""
        if low.startswith("description"):
            raw = line.split(None, 1)[1].strip() if " " in line else ""
            # The driver sends the text quoted ('description "Foo Bar"'); the
            # switch stores it and reads it back unquoted in the status view.
            if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
                raw = raw[1:-1]
            port["description"] = raw
            return ""
        return None

    # ── renderers ──

    def _render_show(self, low: str) -> str:
        ports = self._ports
        poe_ports = {i: p for i, p in ports.items() if p["poe_capable"]}
        s = self.state
        if low == "show version" or low == "show hardware":
            model = s.get("model", "M4350-24X4F")
            return (
                f"System Description............................. {model} ProAV Switch\r\n"
                f"Machine Model.................................. {model}\r\n"
                f"Serial Number.................................. {s.get('serial_number', '')}\r\n"
                f"Burned In MAC Address.......................... {s.get('mac_address', '')}\r\n"
                f"Software Version............................... {s.get('firmware', '')}\r\n"
                f"Boot Code Version.............................. {s.get('boot_version', '')}\r\n"
                f"System Name.................................... {s.get('system_name', '')}\r\n"
                f"System Up Time................................. {s.get('uptime', '')}"
            )
        if low == "show poe":
            consumed = sum(p["poe_power_mw"] for p in poe_ports.values()) / 1000
            return (
                f"Firmware Version............................... {s.get('poe_firmware', '')}\r\n"
                f"PSE Main Operational Status.................... {s.get('poe_main_status', 'ON')}\r\n"
                f"Total Power Available.......................... {float(s.get('poe_total_power_w', 0)):.1f} Watts\r\n"
                f"Threshold Power................................ {float(s.get('poe_threshold_power_w', 0)):.1f} Watts\r\n"
                f"Total Power Consumed........................... {consumed:.1f} Watts\r\n"
                f"Usage Threshold................................ {s.get('poe_usage_threshold', 90)}\r\n"
                f"Power Management Mode.......................... {s.get('poe_power_mgmt_mode', 'Dynamic')}\r\n"
                "Traps.......................................... Enable"
            )
        if low.startswith("show poe port info"):
            rows = [
                (i, "Yes" if p["poe_capable"] else "No",
                 32000 if p["poe_capable"] else 0, p["poe_class"], p["poe_power_mw"],
                 p["poe_current"], p["poe_voltage"], p["poe_status"], "No Error")
                for i, p in poe_ports.items()
            ]
            return _tbl(
                [("Intf", 8), ("High", 8), ("Max", 8), ("Class", 8), ("Power", 8),
                 ("Current", 8), ("Voltage", 8), ("Status", 17), ("Fault", 11)], rows)
        if low.startswith("show poe port config"):
            rows = [
                (i, p["poe_admin"], p["poe_priority"], "N/A", "Class Based",
                 "Dot3bt", "4Pt-Dot3af", "None")
                for i, p in poe_ports.items()
            ]
            return _tbl(
                [("Intf", 8), ("Admin", 8), ("Priority", 9), ("Limit", 8),
                 ("LimitType", 14), ("HighPower", 10), ("Detection", 14),
                 ("Timer", 9)], rows)
        if low == "show port all":
            rows = [
                (i, "", p["admin"], "Auto", p["speed"], p["link"], "Enable",
                 "Enable", "long")
                for i, p in ports.items()
            ]
            return _tbl(
                [("Intf", 8), ("Type", 8), ("Admin", 8), ("PhysMode", 12),
                 ("PhysStatus", 12), ("Link", 8), ("LinkTrap", 8), ("LACP", 8),
                 ("Actor", 9)], rows)
        if low.startswith("show interfaces status"):
            rows = [
                (i, p["description"], p["link"], "Auto", p["speed"], p["media"],
                 "Inactive", p["vlan"])
                for i, p in ports.items()
            ]
            return _tbl(
                [("Port", 8), ("Name", 16), ("Link", 8), ("PhysMode", 10),
                 ("PhysStatus", 12), ("Media", 10), ("Flow", 10), ("VLAN", 6)], rows)
        if low == "show igmpsnooping":
            mode = "Enabled" if s.get("igmp_snooping", True) else "Disabled"
            return (
                f"Admin Mode..................................... {mode}\r\n"
                "Multicast Control Frame Count.................. 142\r\n"
                "Interfaces Enabled for IGMP Snooping.......... 1/0/1-1/0/48\r\n"
                "VLANs enabled for IGMP snooping................ 10,20,30"
            )
        if low.startswith("show igmpsnooping querier"):
            mode = "Enabled" if s.get("igmp_querier", True) else "Disabled"
            return (
                f"Admin Mode..................................... {mode}\r\n"
                "Admin Version.................................. 2\r\n"
                f"Querier Address................................ {s.get('igmp_querier_address', '')}\r\n"
                "Query Interval (secs).......................... 60\r\n"
                "Querier Timeout (secs)......................... 120"
            )
        if low.startswith("show igmpsnooping group"):
            return _tbl(
                [("VLAN", 8), ("Subscriber", 31), ("MCGroup", 30),
                 ("Interface", 10), ("Type", 7), ("Timeout", 13)],
                [("10", "10.20.1.6/00:00:00:00:00:06",
                  "224.1.1.6/01:00:5E:01:01:06", "1/0/16", "IGMPv2", "252"),
                 ("20", "10.20.2.7/00:00:00:00:00:07",
                  "239.1.1.7/01:00:5E:01:01:07", "1/0/2", "IGMPv2", "240")])
        if low == "show vlan":
            return _tbl(
                [("VLANID", 8), ("Name", 20), ("Type", 12), ("Ports", 16)],
                [("1", "default", "Default", ""), ("10", "Cameras", "Static", ""),
                 ("20", "Displays", "Static", ""), ("30", "Dante", "Static", "")])
        if low == "show environment":
            temp = s.get("temperature_c", 41)
            psu_rows = "\r\n".join(
                f"1    {n:<5} PS-{n:<9} Fixed {s.get('psu_state', 'Operational')}"
                for n in range(1, int(s.get("psu_count", 1)) + 1)
            )
            return (
                "Fan Control Mode............................... Quiet\r\n"
                f"Temp (C)....................................... {temp}\r\n"
                "Temperature traps range: 0 to 90 degrees (Celsius)\r\n"
                "\r\n"
                "Temperature Sensors:\r\n"
                "\r\n"
                "Unit Sensor Description      Temp (C) State    Max_Temp (C)\r\n"
                "---- ------ ---------------- -------- -------- ------------\r\n"
                f"1    1      sensor-1         {str(temp).ljust(8)} "
                f"{str(s.get('temperature_state', 'Normal')).ljust(8)} 53\r\n"
                "\r\n"
                "Fans:\r\n"
                "\r\n"
                "Unit Fan Description    Type  Speed Duty       State\r\n"
                "---- --- -------------- ----- ----- ---------- -----------\r\n"
                f"1    1   FAN-1          Fixed {str(s.get('fan_speed', 3200)).ljust(5)} "
                f"{(str(s.get('fan_duty', 30)) + '%').ljust(10)} {s.get('fan_state', 'Operational')}\r\n"
                "\r\n"
                "Power Modules:\r\n"
                "\r\n"
                "Unit Power Description Type  State\r\n"
                "---- ----- ----------- ----- -----------\r\n"
                f"{psu_rows}"
            )
        if low.startswith("show process cpu"):
            return (
                "Memory Utilization Report\r\n"
                "\r\nstatus     bytes\r\n------ ----------\r\n"
                f"free   {s.get('mem_free_kb', 0)}\r\nalloc  {s.get('mem_alloc_kb', 0)}\r\n"
                "\r\nCPU Utilization:\r\n"
                "\r\nPID  Name      5 Secs 60 Secs 300 Secs\r\n"
                "--------------------------------------------\r\n"
                "765  task1     0.00%  0.01%   0.02%\r\n"
                "--------------------------------------------\r\n"
                f"Total CPU Utilization   {float(s.get('cpu_5s', 0)):.2f}%  "
                f"{float(s.get('cpu_60s', 0)):.2f}%   {float(s.get('cpu_300s', 0)):.2f}%"
            )
        if low.startswith("show lldp remote-device"):
            return _tbl(
                [("Intf", 9), ("RemID", 7), ("ChassisID", 20), ("PortID", 20),
                 ("Name", 20)],
                [("1/0/2", "3", "B8:27:EB:11:22:33", "eth0", "Display-Left"),
                 ("1/0/1", "2", "00:FC:E3:90:01:0F", "00:FC:E3:90:01:11",
                  "Conf Room Cam")])
        if low == "show power redundancy":
            count = int(s.get("psu_count", 2))
            return (
                "N+1 configuration: ............................ "
                f"{'Enable' if s.get('psu_redundancy_enabled', True) else 'Disable'}\r\n"
                "N+1 Active: ................................... "
                f"{'Yes' if s.get('psu_redundancy_active', True) else 'No'}\r\n"
                f"Number of PSU: ................................ {count}\r\n"
                f"Effective Number of PSU: ...................... {max(count - 1, 1)}"
            )
        if low.startswith("show rgb-led"):
            return _tbl(
                [("Intf", 9), ("Color", 10), ("Profile", 14)],
                [("1/0/1", "Blue", "Dante"), ("1/0/2", "Green", "NDI")])
        return ""

    def _render_cablestatus(self, line: str) -> str:
        return (
            "Cable Status................................... Normal\r\n"
            "Cable Length................................... 20m - 25m"
        )
