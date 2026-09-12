"""Simulator for the AVPro Edge MXNet 10G control box (AC-MXNET-10G-CBOX).

Models a small 10G system — two encoders and three decoders — behind the control
box's TCP 24 API: the device database, per-endpoint AV status, the six per-plane
channel subscriptions each decoder holds, the saved matrices and the video walls.

The point of interest is that this API answers in three different shapes and the
simulator emits all three the way the document shows them, because a driver that
only handles JSON reads two of them as garbage:

  * a JSON object, for every `config`, `matrix` and multiview command;
  * a Lua-style table, for the `vwid list` / `vwid get` / `vwid layout list` /
    `vwid layout get` queries;
  * a bare `OK`, for the remaining `vwid` commands.

Routes are not a query here. A decoder reports the channel it subscribes to on
each plane (`ch_v`, `ch_a`, `ch_l`, `ch_u`, `ch_r`, `ch_s`) and an encoder
reports the channel it hosts (`ch`), so this simulator keeps routes as endpoint
references and renders them as channel numbers — which is what makes the
driver's join the thing under test rather than a shared assumption.

RS-232: the `serial_loopback` control wires an endpoint's serial TX back to its
RX, so sending data returns it as an unsolicited frame (empty `cmd`,
`source: "rs232"`) — the path a driver must not mistake for a reply.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

# mac -> (name, kind, product, firmware, channel)
ENDPOINTS: dict[str, tuple[str, str, str, str, str]] = {
    "188A6ACE87DC": ("Cable-Box", "encoder", "AC-MXNET-10G-E", "3.13", "0009"),
    "188A6A0F4485": ("Laptop-HDMI", "encoder", "AC-MXNET-10G-AVDM-E", "3.11", "0002"),
    "188A6A45C4A5": ("Bar-Left", "decoder", "AC-MXNET-10G-D", "3.13", "0000"),
    "188A6A45C4A6": ("Bar-Right", "decoder", "AC-MXNET-10G-D", "3.13", "0000"),
    "188A6A1887E3": ("Boardroom", "decoder", "AC-MXNET-10G-D", "3.12", "0000"),
}

MATRICES = {"Bar": "va", "AllHands": "z"}

# wall -> {layout -> [tile strings]}. A layout with no tiles is one that has been
# created and not yet populated, which the document's own example shows.
VIDEOWALLS: dict[str, dict[str, list[str]]] = {
    "BarWall": {
        "Full": [
            "1:1:Cable-Box:Bar-Left:1:2:1:1:1:3:102:100:100",
            "1:2:Cable-Box:Bar-Right:1:2:1:2:1:3:102:100:100",
        ],
        "Split": [],
    },
    "Boardroom": {"Single": []},
}

PLANES = ("video", "audio", "analogaudio", "usb", "infrared", "serial")

# The devicelist member each plane's subscription is reported on, and the
# `*path` command that sets it.
PLANE_MEMBERS = {
    "video": "ch_v",
    "audio": "ch_a",
    "analogaudio": "ch_l",
    "usb": "ch_u",
    "infrared": "ch_r",
    "serial": "ch_s",
}

PATH_PLANES = {
    "videopath": "video",
    "audiopath": "audio",
    "analogaudiopath": "analogaudio",
    "usbpath": "usb",
    "irpath": "infrared",
    "rs232path": "serial",
}

# `matrix add` / `matrix aset` type letters, from the API document.
MATRIX_CODES = {
    "z": PLANES,
    "v": ("video",),
    "a": ("audio",),
    "l": ("analogaudio",),
    "u": ("usb",),
    "r": ("infrared",),
    "s": ("serial",),
}

_RE_PATH = re.compile(r"^config set device (\w+path)\s+(\S+)\s+(\S+)$", re.I)
_RE_PATH_OFF = re.compile(r"^config set device (\w+pathdisable)\s+(\S+)$", re.I)
_RE_SET_DEV = re.compile(r"^config set device (\S+)\s+(.+)$", re.I)
_RE_STATUS = re.compile(r"^config get device status(?:\s+(\S+))?$", re.I)
_RE_INFO = re.compile(r"^config get device info\s+(\S+)$", re.I)
_RE_MATRIX_ASET = re.compile(
    r"^matrix aset(?:\s+([A-Za-z0-9_-]*):([a-z]+))?\s+(\S+)\s+(.+)$", re.I
)


class AVProEdgeMXNet10GSimulator(TCPSimulator):
    """AC-MXNET-10G-CBOX control box."""

    SIMULATOR_INFO = {
        "driver_id": "avproedge_mxnet_10g",
        "name": "AVPro Edge MXNet 10G CBOX Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 24,
        "delimiter": "\r\n",
        "initial_state": {
            "cbox_name": "AC-MXNET-10G-CBOX",
            "firmware": "3.01",
            "timezone": "UTC+0",
            "serial_loopback": True,
            "last_command": "",
            # Per-endpoint UI state. Signal is an encoder concept, HPD a
            # decoder one; presence applies to both.
            "online_188A6ACE87DC": True,
            "online_188A6A0F4485": True,
            "online_188A6A45C4A5": True,
            "online_188A6A45C4A6": True,
            "online_188A6A1887E3": False,
            "signal_188A6ACE87DC": True,
            # An online encoder with nothing plugged in: present, no signal,
            # and sitting in s_attaching forever. It is the case that reads as
            # "offline" to anyone who mistakes `state` for presence.
            "signal_188A6A0F4485": False,
            "route_video_188A6A45C4A5": "Cable-Box",
            "route_video_188A6A45C4A6": "Cable-Box",
            "route_video_188A6A1887E3": "",
        },
        "controls": [
            {"type": "indicator", "key": "cbox_name", "label": "Control Box"},
            {"type": "indicator", "key": "last_command", "label": "Last Command"},
            {"type": "toggle", "key": "signal_188A6ACE87DC", "label": "Cable-Box Signal"},
            {"type": "toggle", "key": "signal_188A6A0F4485", "label": "Laptop Signal"},
            {"type": "toggle", "key": "online_188A6A1887E3", "label": "Boardroom Online"},
            {"type": "toggle", "key": "serial_loopback", "label": "Serial Loopback"},
            {"type": "indicator", "key": "route_video_188A6A45C4A5", "label": "Bar-Left Source"},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._line_mode = True

        # Instance copies so a test can add or drop an endpoint and exercise
        # roster reconciliation.
        self._eps: dict[str, dict[str, Any]] = {}
        for mac, (name, kind, product, firmware, channel) in ENDPOINTS.items():
            self._eps[mac] = {
                "name": name,
                "kind": kind,
                "product": product,
                "firmware": firmware,
                "channel": channel,
                "service_state": "s_srv_on",
                "edid": "2",
                "volume": 100,
                "hdcp": "1",
                "hdrmode": "1",
                "stream": "on",
                "timing": "0 0 0",
                "serial_setting": "9600 8 0 1",
                "description": "",
                "avdm_id": "",
                "avdm_description": "",
                "downmix": "1",
                "hdmi": {"0": "on", "1": "on"},
            }

        # decoder mac -> {plane: encoder mac or ""}
        self._routes: dict[str, dict[str, str]] = {}
        for mac, ep in self._eps.items():
            if ep["kind"] != "decoder":
                continue
            source = self.state.get(f"route_video_{mac}", "")
            src_mac = self._by_name(source) if source else ""
            self._routes[mac] = {p: (src_mac or "") for p in PLANES}

        self._matrices = dict(MATRICES)
        self._walls = {w: {ln: list(t) for ln, t in ls.items()} for w, ls in VIDEOWALLS.items()}
        self._ntp = ["0.north-america.pool.ntp.org", "1.north-america.pool.ntp.org"]
        self._dns = ["8.8.8.8", "8.8.4.4"]
        self._date = "2026-09-12 09:15:00"

    # ── Helpers ──────────────────────────────────────────────────────

    def _by_name(self, token: str) -> str | None:
        """Resolve a MAC or custom name to a MAC, the way the control box does."""
        token = token.strip()
        upper = token.upper()
        if upper in self._eps:
            return upper
        for mac, ep in self._eps.items():
            if ep["name"].lower() == token.lower():
                return mac
        return None

    def _targets(self, token: str) -> list[str]:
        """Expand a target token: MAC / name / colon-list / ALL / ALLTX / ALLRX."""
        token = token.strip()
        upper = token.upper()
        if upper == "ALL":
            return list(self._eps)
        if upper == "ALLTX":
            return [m for m, e in self._eps.items() if e["kind"] == "encoder"]
        if upper == "ALLRX":
            return [m for m, e in self._eps.items() if e["kind"] == "decoder"]
        macs = []
        for part in token.split(":"):
            mac = self._by_name(part)
            if mac:
                macs.append(mac)
        return macs

    def _online(self, mac: str) -> bool:
        return bool(self.state.get(f"online_{mac}", True))

    def _heartbeat(self) -> int:
        """The control box's heartbeat clock.

        A box with no NTP counts from the epoch, which is what the API
        document's examples show (values around 14000-17500). This models that
        rather than a true Unix time, because a driver comparing heartbeats
        against ITS OWN clock would pass against a synced box and call every
        endpoint offline against this one.
        """
        return int(time.monotonic()) + 14000

    def _ok(self, command: str, info: Any = "") -> bytes:
        return self._frame({"cmd": command, "info": info, "code": 0})

    def _err(self, command: str, message: str) -> bytes:
        return self._frame({"error": message, "cmd": command, "code": -1})

    @staticmethod
    def _frame(doc: dict[str, Any]) -> bytes:
        return (json.dumps(doc) + "\r\n").encode()

    @staticmethod
    def _raw(text: str) -> bytes:
        """A reply that is not JSON at all — a Lua table, or a bare OK."""
        return (text + "\r\n").encode()

    def _sync_route_state(self, mac: str) -> None:
        src = self._routes[mac]["video"]
        self.set_state(f"route_video_{mac}", self._eps[src]["name"] if src else "")

    # ── Reply builders ───────────────────────────────────────────────

    def _devicelist(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for mac, ep in self._eps.items():
            entry: dict[str, Any] = {
                "mac": mac,
                "id": ep["name"],
                "ip": f"169.254.{int(mac[-4:-2], 16) % 250}.{int(mac[-2:], 16) % 250}",
                "dtype": "ast152x",
                "version": ep["firmware"],
                "ipmode": "dhcp",
                "rs232mode": "2",
                "ch": ep["channel"],
            }
            # Presence is the `online` heartbeat and NOTHING else. A real
            # control box omits the member entirely for an endpoint that is not
            # there, and keeps a live counter for one that is; `state` is the
            # streaming service state and is present either way (a perfectly
            # reachable encoder with no source sits in `s_attaching` forever).
            if self._online(mac):
                entry["online"] = self._heartbeat()
                live = bool(self.state.get(f"signal_{mac}", False))
                entry["state"] = (
                    "s_attaching" if ep["kind"] == "encoder" and not live else "s_srv_on"
                )
            if ep["kind"] == "encoder":
                entry["is_host"] = 1
                entry["edid"] = ep["edid"]
                entry["exaudiovolume"] = str(ep["volume"])
            else:
                routes = self._routes[mac]
                entry["ch_p"] = "0000"
                entry["ch_c"] = "0000"
                for plane, member in PLANE_MEMBERS.items():
                    src = routes[plane]
                    # An unrouted plane reports a channel no encoder hosts.
                    entry[member] = self._eps[src]["channel"] if src else "0001"
            # Keyed by the endpoint's CURRENT id, which is its MAC until it is
            # renamed and its custom name afterwards. The `mac` member stays
            # put, which is the only reason a rename does not re-identify the
            # child and orphan every binding pointing at it.
            out[ep["name"]] = entry
        return out

    def _status(self, macs: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for mac in macs:
            ep = self._eps[mac]
            if ep["kind"] == "encoder":
                live = bool(self.state.get(f"signal_{mac}", False))
                out[mac] = {
                    "id": ep["name"],
                    "version": ep["firmware"],
                    "video": " 3840X2160p/30Hz" if live else "",
                    "audio": "PCM" if live else "",
                    "hpd": "HPD1" if live else "HPD0",
                    "hdr": "HDR1" if live else "HDR0",
                    # An encoder spells HDCP as a token plus a digit...
                    "hdcp": "HDCP1" if live else "HDCP0",
                    "colordepth": "8Bit" if live else "",
                    "speed": "3",
                    "ch": ep["channel"],
                    "status": 0,
                    "light": 0,
                    "is_host": 1,
                    "edid": ep["edid"],
                    "dtype": "ast152x",
                    "ip": f"169.254.{int(mac[-4:-2], 16) % 250}.{int(mac[-2:], 16) % 250}",
                }
            else:
                src = self._routes[mac]["video"]
                showing = bool(src) and bool(self.state.get(f"signal_{src}", False))
                out[mac] = {
                    "id": ep["name"],
                    "version": ep["firmware"],
                    "video": " 3840X2160p/30Hz" if showing else "",
                    "audio": "PCM" if showing else "",
                    "hpd": "HPD1",
                    "hdr": "HDR1" if showing else "HDR0",
                    # ...while a decoder spells the same thing in words, on the
                    # same firmware. Both are real; the driver normalises them
                    # so two endpoint cards agree.
                    "hdcp": "HDCP ON" if showing else "HDCP OFF",
                    "colordepth": "8Bit" if showing else "",
                    "speed": "3",
                    "ch": "0000",
                    "status": 0,
                    "light": 0,
                    "dtype": "ast152x",
                    "ip": f"169.254.{int(mac[-4:-2], 16) % 250}.{int(mac[-2:], 16) % 250}",
                }
            if self._online(mac):
                out[mac]["online"] = self._heartbeat()
        return out

    def _device_info(self, mac: str) -> dict[str, Any]:
        """`config get device info` — the roster entry for one endpoint.

        A decoder's entry carries its output timing as an OBJECT here, where the
        status reply carries a string for the same fact. Both shapes are in the
        document and a driver has to read either.
        """
        entry = dict(self._devicelist()[self._eps[mac]["name"]])
        if self._eps[mac]["kind"] == "decoder":
            src = self._routes[mac]["video"]
            if src and self.state.get(f"signal_{src}", False):
                entry["video"] = {
                    "frames_per_second": "30",
                    "height": "2160",
                    "width": "3840",
                }
        return entry

    def _wall_table(self, walls: dict[str, dict[str, list[str]]]) -> str:
        """Render the Lua-style table the video-wall queries answer with."""
        lines = ["{"]
        for wall in sorted(walls):
            layouts = walls[wall]
            lines.append(f"    {wall} = {{")
            lines.append("     cols = 2,")
            lines.append("     layouts = {")
            for layout in sorted(layouts):
                tiles = layouts[layout]
                lines.append(f"      {layout} = {{")
                lines.append("       cols = 2,")
                if tiles:
                    lines.append("       layout = {")
                    lines.append(
                        ",\n".join(f'        "{tile}"' for tile in tiles)
                    )
                    lines.append("       },")
                else:
                    lines.append("       layout = {},")
                lines.append("       rows = 2")
                lines.append("      },")
            lines.append("     },")
            lines.append("     rows = 2")
            lines.append("    },")
        lines.append("}")
        return "\n".join(lines)

    # ── Dispatch ─────────────────────────────────────────────────────

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", errors="replace").strip("\r\n").strip()
        if not line:
            return None
        self.set_state("last_command", line)

        for handler in (self._system, self._queries, self._videowall, self._matrix,
                        self._routing, self._device):
            reply = handler(line)
            if reply is not None:
                return reply
        return self._err(line, "unknown command")

    def _system(self, line: str) -> bytes | None:
        low = line.lower()
        if low == "config get name":
            return self._ok(line, self.state["cbox_name"])
        if low == "config get version":
            return self._ok(line, self.state["firmware"])
        if low == "config get ipsetting":
            return self._ok(line, "autoip")
        if low == "config get ipsetting2":
            return self._ok(line, "static/192.168.1.239/255.255.255.0")
        if low == "config get timezone":
            return self._ok(line, self.state["timezone"])
        if low == "config get ntp":
            return self._ok(line, "/".join(self._ntp))
        if low == "config get dns":
            return self._ok(line, " ".join(self._dns))
        if low == "config get date":
            return self._ok(line, self._date)
        if low == "config set reboot":
            return self._frame({"code": 0, "cmd": line})

        m = re.match(r"^config set timezone (UTC[+-](?:\d|1[0-2]))$", line, re.I)
        if m:
            self.set_state("timezone", m.group(1).upper())
            return self._frame({"code": 0, "cmd": line})
        m = re.match(r"^config set ntp (.+)$", line, re.I)
        if m:
            servers = m.group(1).split()
            if len(servers) > 5:
                return self._err(line, "at most 5 NTP servers")
            self._ntp = servers
            return self._frame({"code": 0, "cmd": line})
        m = re.match(r"^config set dns (.+)$", line, re.I)
        if m:
            servers = m.group(1).split()
            if len(servers) > 2:
                return self._err(line, "at most 2 DNS servers")
            self._dns = servers
            return self._frame({"code": 0, "cmd": line})
        m = re.match(r"^config set date (\d+) (\d+) (\d+) (\d+) (\d+) (\d+)$", line, re.I)
        if m:
            y, mo, d, h, mi, s = (int(g) for g in m.groups())
            self._date = f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"
            return self._frame({"code": 0, "cmd": line})
        return None

    def _queries(self, line: str) -> bytes | None:
        if line.lower() == "config get devicelist":
            return self._ok(line, self._devicelist())

        m = _RE_STATUS.match(line)
        if m:
            token = m.group(1) or "ALL"
            macs = self._targets(token)
            if not macs:
                return self._err(line, f"device {token} not found")
            return self._ok(line, self._status(macs))

        m = _RE_INFO.match(line)
        if m:
            macs = self._targets(m.group(1))
            if len(macs) != 1:
                return self._err(line, f"device {m.group(1)} not found")
            return self._ok(line, self._device_info(macs[0]))
        return None

    def _videowall(self, line: str) -> bytes | None:
        """The `vwid` family, whose replies are NOT JSON."""
        low = line.lower()
        if low == "vwid list":
            return self._raw(self._wall_table(self._walls))

        m = re.match(r"^vwid get (\S+)$", line, re.I)
        if m:
            wall = self._wall_of(m.group(1))
            if wall is None:
                return self._raw("ERROR: videowall not found")
            return self._raw(self._wall_table({wall: self._walls[wall]}))

        m = re.match(r"^vwid layout list (\S+)$", line, re.I)
        if m:
            wall = self._wall_of(m.group(1))
            if wall is None:
                return self._raw("ERROR: videowall not found")
            return self._raw(self._wall_table({wall: self._walls[wall]}))

        m = re.match(r"^vwid layout active (\S+)\s+(\S+)$", line, re.I)
        if m:
            wall = self._wall_of(m.group(1))
            if wall is None:
                return self._raw("ERROR: videowall not found")
            layout = self._layout_of(wall, m.group(2))
            if layout is None:
                return self._raw("ERROR: layout not found")
            # Activating a layout applies its tiles, which is a route.
            for tile in self._walls[wall][layout]:
                parts = tile.split(":")
                if len(parts) < 4:
                    continue
                src = self._by_name(parts[2])
                dst = self._by_name(parts[3])
                if src and dst and self._eps[dst]["kind"] == "decoder":
                    self._routes[dst]["video"] = src
                    self._sync_route_state(dst)
            return self._raw("OK")

        m = re.match(r"^vwid layout multiview active (\S+)\s+(\S+)\s+(\S+)$", line, re.I)
        if m:
            wall = self._wall_of(m.group(1))
            if wall is None or self._layout_of(wall, m.group(2)) is None:
                return self._err(line, "layout not found")
            if not re.match(r"^\d+:\d+$", m.group(3)):
                return self._err(line, "indexid must be row:col")
            # The document shows these answering with JSON whose `cmd` member
            # contains unescaped quotes, which no parser can read. Reproduced
            # exactly, because a driver has to survive it.
            return self._raw('{"info":"OK","cmd":"{"cmd":"' + line + '"}"}')
        return None

    def _wall_of(self, token: str) -> str | None:
        for wall in self._walls:
            if wall.lower() == token.strip().lower():
                return wall
        return None

    def _layout_of(self, wall: str, token: str) -> str | None:
        for layout in self._walls[wall]:
            if layout.lower() == token.strip().lower():
                return layout
        return None

    def _matrix(self, line: str) -> bytes | None:
        low = line.lower()
        if low == "matrix list":
            return self._ok(line, {name: self._matrix_body(name) for name in self._matrices})

        m = re.match(r"^matrix get (\S+)$", line, re.I)
        if m:
            name = self._matrix_of(m.group(1))
            if name is None:
                return self._err(line, f"matrix {m.group(1)} not found")
            return self._ok(line, self._matrix_body(name))

        m = re.match(r"^matrix active (\S+)(?:\s+(force))?$", line, re.I)
        if m:
            name = self._matrix_of(m.group(1))
            if name is None:
                return self._err(line, f"matrix {m.group(1)} not found")
            return self._ok(line, "OK")

        m = _RE_MATRIX_ASET.match(line)
        if m:
            # Accepted, and deliberately NOT applied when a destination's plane
            # is disabled -- the behaviour measured on the 1G box in this
            # family, and the reason this driver routes with the per-plane
            # `*path` commands instead. Pinned by a test so nobody "simplifies"
            # routing back onto the one-liner that reads better and drops routes.
            code = (m.group(2) or "v").lower()
            long_form = {
                "video": "v", "audio": "a", "analogaudio": "l",
                "usb": "u", "infrared": "r", "serial": "s", "all": "z",
            }
            code = long_form.get(code, code)
            planes = MATRIX_CODES.get(code)
            if planes is None:
                return self._err(line, f"unknown matrix type {code}")
            src = self._by_name(m.group(3))
            if src is None or self._eps[src]["kind"] != "encoder":
                return self._err(line, f"encoder {m.group(3)} not found")
            for token in m.group(4).split():
                dst = self._by_name(token)
                if dst is None or self._eps[dst]["kind"] != "decoder":
                    return self._err(line, f"decoder {token} not found")
                for plane in planes:
                    if self._routes[dst][plane]:
                        self._routes[dst][plane] = src
                self._sync_route_state(dst)
            return self._ok(line, "OK")
        return None

    def _matrix_of(self, token: str) -> str | None:
        for name in self._matrices:
            if name.lower() == token.strip().lower():
                return name
        return None

    def _matrix_body(self, name: str) -> dict[str, Any]:
        srcs = {}
        for mac, routes in self._routes.items():
            src = routes["video"]
            if src:
                srcs[self._eps[mac]["name"]] = self._eps[src]["name"]
        return {"type": self._matrices[name], "srcs": srcs}

    def _offline_route(self, line: str, *macs: str) -> bytes | None:
        """The control box's refusal for a route touching an endpoint it cannot reach."""
        for mac in macs:
            if mac and not self._online(mac):
                return self._err(line, "Device not online")
        return None

    def _routing(self, line: str) -> bytes | None:
        m = _RE_PATH.match(line)
        if m and m.group(1).lower() in PATH_PLANES:
            plane = PATH_PLANES[m.group(1).lower()]
            src = self._by_name(m.group(2))
            dst = self._by_name(m.group(3))
            if src is None or self._eps[src]["kind"] != "encoder":
                return self._err(line, f"encoder {m.group(2)} not found")
            if dst is None or self._eps[dst]["kind"] != "decoder":
                return self._err(line, f"decoder {m.group(3)} not found")
            refused = self._offline_route(line, src, dst)
            if refused is not None:
                return refused
            self._routes[dst][plane] = src
            self._sync_route_state(dst)
            return self._frame({"code": 0, "cmd": line})

        m = _RE_PATH_OFF.match(line)
        if m:
            key = m.group(1).lower().replace("disable", "")
            if key not in PATH_PLANES:
                return None
            dst = self._by_name(m.group(2))
            if dst is None or self._eps[dst]["kind"] != "decoder":
                return self._err(line, f"decoder {m.group(2)} not found")
            refused = self._offline_route(line, dst)
            if refused is not None:
                return refused
            self._routes[dst][PATH_PLANES[key]] = ""
            self._sync_route_state(dst)
            return self._frame({"code": 0, "cmd": line})
        return None

    def _device(self, line: str) -> bytes | None:
        m = _RE_SET_DEV.match(line)
        if not m:
            return None
        verb = m.group(1).lower()
        rest = m.group(2).strip()

        # Verbs whose last token is the target and which carry one value.
        simple: dict[str, tuple[str, tuple[str, ...]]] = {
            "edid": ("edid", tuple(str(i) for i in range(0, 21))),
            "hdcp": ("hdcp", ("0", "1", "2")),
            "hdrmode": ("hdrmode", ("0", "1")),
            "stream": ("stream", ("on", "off")),
            "exmxmode": ("downmix", tuple(str(i) for i in range(1, 8))),
        }
        if verb in simple:
            field, allowed = simple[verb]
            parts = rest.split()
            if len(parts) != 2:
                return self._err(line, f"{verb} needs a value and a target")
            value, target = parts[0].lower(), parts[1]
            if value not in allowed:
                return self._err(line, f"invalid {verb} value {parts[0]}")
            macs = self._targets(target)
            if not macs:
                return self._err(line, f"device {target} not found")
            for mac in macs:
                self._eps[mac][field] = value
            return self._frame({"code": 0, "cmd": line})

        if verb == "video":
            # "video <width> <height> <fps> <rx>"
            parts = rest.split()
            if len(parts) != 4 or not all(p.isdigit() for p in parts[:3]):
                return self._err(line, "video takes width height fps and a target")
            macs = self._targets(parts[3])
            if not macs:
                return self._err(line, f"device {parts[3]} not found")
            for mac in macs:
                self._eps[mac]["timing"] = " ".join(parts[:3])
            return self._frame({"code": 0, "cmd": line})

        if verb == "hdmi":
            # "hdmi 0|1 on|off <tx>"
            parts = rest.split()
            if len(parts) != 3 or parts[0] not in ("0", "1") or parts[1].lower() not in ("on", "off"):
                return self._err(line, "hdmi takes 0/1, on/off and a target")
            macs = self._targets(parts[2])
            if not macs:
                return self._err(line, f"device {parts[2]} not found")
            for mac in macs:
                self._eps[mac]["hdmi"][parts[0]] = parts[1].lower()
            return self._ok(line, "")

        if verb == "exaudio":
            parts = rest.split()
            if parts[:1] != ["volume"] or len(parts) != 3 or not parts[1].isdigit():
                return self._err(line, "bad exaudio command")
            if not 0 <= int(parts[1]) <= 100:
                return self._err(line, "volume is 0-100")
            macs = self._targets(parts[2])
            if not macs:
                return self._err(line, f"device {parts[2]} not found")
            for mac in macs:
                self._eps[mac]["volume"] = int(parts[1])
            return self._ok(line, "")

        if verb == "copyedid":
            parts = rest.split()
            if len(parts) != 2:
                return self._err(line, "copyedid needs a decoder and an encoder")
            src = self._by_name(parts[0])
            dst = self._by_name(parts[1])
            if src is None or self._eps[src]["kind"] != "decoder":
                return self._err(line, f"decoder {parts[0]} not found")
            if dst is None or self._eps[dst]["kind"] != "encoder":
                return self._err(line, f"encoder {parts[1]} not found")
            self._eps[dst]["edid"] = "20"
            return self._ok(line, "Copy success")

        if verb == "light":
            parts = rest.split()
            if len(parts) != 2 or parts[0].lower() not in ("on", "off", "flash"):
                return self._err(line, "light takes on, off or flash")
            if not self._targets(parts[1]):
                return self._err(line, f"device {parts[1]} not found")
            return self._ok(line, "OK")

        if verb in ("reboot", "hpdrst"):
            if not self._targets(rest):
                return self._err(line, f"device {rest} not found")
            return self._ok(line, "")

        if verb == "id":
            parts = rest.split()
            if len(parts) != 2:
                return self._err(line, "id needs a new name and a target")
            if parts[0].upper() in ("ALL", "ALLRX", "ALLTX") or "," in parts[0]:
                return self._err(line, "reserved id")
            mac = self._by_name(parts[1])
            if mac is None:
                return self._err(line, f"device {parts[1]} not found")
            self._eps[mac]["name"] = parts[0]
            for dst in self._routes:
                self._sync_route_state(dst)
            return self._frame({"code": 0, "cmd": line})

        if verb in ("description", "avdmid", "avdmdes"):
            # The description may contain spaces, so the target is the LAST token.
            parts = rest.rsplit(" ", 1)
            if len(parts) != 2 or not parts[0].strip():
                return self._err(line, f"{verb} takes a value and a target")
            macs = self._targets(parts[1])
            if not macs:
                return self._err(line, f"device {parts[1]} not found")
            field = {
                "description": "description",
                "avdmid": "avdm_id",
                "avdmdes": "avdm_description",
            }[verb]
            for mac in macs:
                if verb != "description" and self._eps[mac]["kind"] != "encoder":
                    return self._err(line, "only a TX has an AVDM daughter-card")
                self._eps[mac][field] = parts[0]
            return self._frame({"code": 0, "cmd": line})

        if verb == "cec":
            parts = rest.rsplit(" ", 1)
            if len(parts) != 2:
                return self._err(line, "cec takes hex data and a target")
            payload = parts[0].strip()
            if payload.lower() not in ("poweron", "poweroff") and not re.match(
                r"^[0-9A-Fa-f:]+$", payload
            ):
                return self._err(line, "cec takes hex data, poweron or poweroff")
            if not self._targets(parts[1]):
                return self._err(line, f"device {parts[1]} not found")
            return self._ok(line, "OK")

        if verb == "ir":
            parts = rest.rsplit(" ", 1)
            if len(parts) != 2 or not parts[0].strip():
                return self._err(line, "ir takes a code and a target")
            if not self._targets(parts[1]):
                return self._err(line, f"device {parts[1]} not found")
            return self._frame({"code": 0, "cmd": line})

        if verb == "rs232setting":
            parts = rest.split()
            if len(parts) != 5:
                return self._err(line, "rs232setting takes baud, bits, parity, stop and a target")
            baud, bits, parity, stop = parts[:4]
            if not baud.isdigit() or not 300 <= int(baud) <= 115200:
                return self._err(line, "baud is 300-115200")
            if bits not in ("6", "7", "8") or parity not in ("0", "1", "2") or stop not in ("1", "2"):
                return self._err(line, "bad serial setting")
            macs = self._targets(parts[4])
            if not macs:
                return self._err(line, f"device {parts[4]} not found")
            for mac in macs:
                self._eps[mac]["serial_setting"] = " ".join(parts[:4])
            return self._ok(line, "OK")

        if verb == "rs232":
            # "rs232 <dataType> <payload...> <target>"
            parts = rest.split(" ")
            if len(parts) < 3 or parts[0] not in ("1", "2"):
                return self._err(line, "rs232 takes a data type, data and a target")
            target = parts[-1]
            payload = " ".join(parts[1:-1])
            macs = self._targets(target)
            if not macs:
                return self._err(line, f"device {target} not found")
            ack = self._ok(line, "OK")
            if self.state.get("serial_loopback"):
                # The endpoint's serial TX is looped to its RX, so the data
                # comes straight back as an unsolicited frame.
                for mac in macs:
                    ack += self._serial_frame(mac, payload)
            return ack

        return None

    # ── Unsolicited frames ───────────────────────────────────────────

    def _event_frame(self, mac: str, info: str) -> bytes:
        """An unsolicited AV event (empty cmd, source=mxnet).

        Undocumented for the 10G and observed on the 1G control box, which is
        the same firmware family. They are the reason a driver may not treat
        "empty cmd" as "this must be serial data" — or, worse, hand the frame to
        whichever request is in flight.
        """
        ep = self._eps[mac]
        return self._frame(
            {"info": info, "id": ep["name"], "source": "mxnet", "cmd": "", "code": 0, "mac": mac}
        )

    def _serial_frame(self, mac: str, payload: str) -> bytes:
        """An unsolicited serial frame (empty cmd, source=rs232)."""
        ep = self._eps[mac]
        text = payload.replace("\\r", "").replace("\\n", "")
        return self._frame(
            {
                "info": text,
                "id": ep["name"],
                "source": "rs232",
                "cmd": "",
                "code": 0,
                "mac": mac,
            }
        )

    async def emit_event(self, endpoint: str, info: str) -> None:
        """Push an AV event as the control box would on a hot-plug or format change."""
        mac = self._by_name(endpoint)
        if mac is None:
            return
        await self.push(self._event_frame(mac, info))

    async def emit_serial(self, endpoint: str, payload: str, encoding: str = "ascii") -> None:
        """Push serial data as if a device had sent it into an endpoint's port."""
        mac = self._by_name(endpoint)
        if mac is None:
            return
        if encoding == "base64":
            payload = base64.b64encode(payload.encode()).decode()
        elif encoding == "hex":
            payload = payload.encode().hex().upper()
        await self.push(self._serial_frame(mac, payload))
