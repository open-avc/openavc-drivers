"""Simulator for an AVPro Edge MXNet USP control box (AC-MXNET-USP-CBOX).

Models a small USP system -- three encoders and three decoders -- behind the
control box's TCP 24 API, from IPCBox USP Commands V1.08: the device database,
per-endpoint AV status, each decoder's five routes, saved matrices, video walls,
multiview layouts, playlists and the Dante, AVPro Flow and serial settings.

The point of interest is that this API answers in many shapes and the simulator
emits each the way the document shows it, because a driver that only handles
one reads the others as garbage:

  * JSON with `cmd` and `code`, for most commands;
  * JSON whose `cmd` is an escaped JSON object and which has no `code`
    (`hdcprequest`, `play pl stop`, `play getstorage`); `hdcprequest` echoes
    its target by the endpoint's custom name, as the document's example does;
  * JSON with `info` and `cmd` and no `code` (`mvid layout rx`, `play pl start`,
    `play pl upload`);
  * a bare `OK` for the `vwid` writes;
  * a Lua table for the `vwid` queries AND for `config get device routes`, on
    one line, with lowercase MACs, `none` for an unrouted plane, and each
    renamed endpoint named by its custom name;
  * the malformed JSON the `vwid layout multiview` writes answer with;
  * a refusal that says `code: 0`: `sourceinput` on any encoder that is not an
    AC-MXNET-USP-PR. Which endpoint is which model is the simulator's own
    knowledge; the device list does not report it.

Also modelled on purpose: one encoder online with no source, sitting in
`s_attaching` (present, though anyone reading `state` as presence calls it
offline); one decoder with no heartbeat; and `matrix aset`, which is acked and
ignored when the destination's plane is disabled, so nobody routes with it.

The document shows no error reply at all. Where a command has to fail here
(an unknown endpoint, a bad value, Dante on an AC-MXNET-USP-ES) the simulator
answers `{"error": ..., "cmd": ..., "code": -1}`, the form the 1G document uses;
that is the simulator's guess for USP.

RS-232: the `serial_loopback` control wires an endpoint's serial TX back to its
RX, so sending data returns it as an unsolicited frame (empty `cmd`,
`source: "rs232"`), encoded the way the endpoint's `rs232responsetype` says
(Base64 by default, per the document).
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

# mac -> (encoder name, decoder name, mode, product, firmware, channel)
# A blank name means the endpoint was never renamed and is known by its MAC.
ENDPOINTS: dict[str, tuple[str, str, str, str, str, str]] = {
    "188A6ACE87DC": ("Cable-Box", "", "encoder", "AC-MXNET-USP-PL", "3.13", "0009"),
    "188A6A0F4485": ("", "", "encoder", "AC-MXNET-USP-ES", "3.11", "0002"),
    "188A6A2B11C0": ("Stage-PR", "", "encoder", "AC-MXNET-USP-PR", "3.13", "0005"),
    "188A6A45C4A5": ("", "Bar-Left", "decoder", "AC-MXNET-USP-PL", "3.13", "0000"),
    "188A6A45C4A6": ("", "", "decoder", "AC-MXNET-USP-ES", "3.13", "0000"),
    "188A6A1887E3": ("", "Boardroom", "decoder", "AC-MXNET-USP-PR", "3.12", "0000"),
}

# decoder mac -> encoder mac on its video and audio planes at start.
INITIAL_ROUTES = {
    "188A6A45C4A5": "188A6ACE87DC",
    "188A6A45C4A6": "188A6A0F4485",
}

MATRICES = {"Bar": "va", "AllHands": "z"}

# wall -> {layout -> [tile strings]}. A layout with no tiles is one that has been
# created and not yet populated, which the document's own example shows.
VIDEOWALLS: dict[str, dict[str, list[str]]] = {
    "BarWall": {
        "Full": [
            "1:1:Cable-Box:Bar-Left:1:2:1:1:1:3:102:100:100",
            "1:2:Cable-Box:188A6A45C4A6:1:2:1:2:1:3:102:100:100",
        ],
        "Split": [],
    },
    "Boardroom": {"Single": []},
}

MULTIVIEW_LAYOUTS: dict[str, dict[str, Any]] = {
    "Quad": {
        "windows": [
            {"host": "188a6ace87dc", "width": "960", "hposition": "0", "layer": 1,
             "vposition": "0", "height": "540", "index": 1},
            {"host": "Stage-PR", "width": "960", "hposition": "960", "layer": 2,
             "vposition": "0", "height": "540", "index": 2},
        ],
        "audiomode": 1,
        "audioselect": "188a6ace87dc",
        "resolution": 5,
        "client": "188a6a45c4a5",
    },
    "PiP": {
        "windows": [],
        "audiomode": 2,
        "audioselect": "",
        "resolution": 5,
        "client": "",
    },
}

PLAYLISTS: dict[str, list[dict[str, Any]]] = {
    "Lobby-Loop": [
        {"url": "1738737449099.jpg", "time": 10, "index": 1},
        {"url": "1738737888096.mp4", "time": 0, "index": 2},
    ],
    "Welcome": [{"url": "1738738470689.jpg", "time": 30, "index": 1}],
}

PLANES = ("video", "audio", "usb", "infrared", "serial")

PATH_PLANES = {
    "videopath": "video",
    "audiopath": "audio",
    "usbpath": "usb",
    "irpath": "infrared",
    "rs232path": "serial",
}

# The route reply's member for each plane, and the letter that asks for it in
# `config get device routes [v,a,u,r,s]`.
ROUTE_WIRE = {"audio": "audio", "infrared": "ir", "serial": "rs232", "usb": "usb", "video": "video"}
ROUTE_LETTERS = {"v": "video", "a": "audio", "u": "usb", "r": "infrared", "s": "serial"}

# `matrix aset` type letters, from the API document. USP has no analog-audio
# plane, so `l` moves nothing.
MATRIX_CODES = {
    "z": PLANES,
    "v": ("video",),
    "a": ("audio",),
    "l": (),
    "u": ("usb",),
    "r": ("infrared",),
    "s": ("serial",),
}

DANTE_PRODUCTS = ("AC-MXNET-USP-PL", "AC-MXNET-USP-PR")

_RE_PATH = re.compile(r"^config set device (\w+path)\s+(\S+)\s+(\S+)$", re.I)
_RE_PATH_OFF = re.compile(r"^config set device (\w+pathdisable)\s+(\S+)$", re.I)
_RE_SET_DEV = re.compile(r"^config set device (\S+)\s+(.+)$", re.I)
_RE_STATUS = re.compile(r"^config get device status(?:\s+(\S+))?$", re.I)
_RE_INFO = re.compile(r"^config get device info\s+(\S+)$", re.I)
_RE_ROUTES = re.compile(r"^config get device routes(?:\s+([vaurs,]+))?\s+(\S+)$", re.I)
_RE_DANTE_GET = re.compile(r"^config get device dante ip\s+(\S+)$", re.I)
_RE_MATRIX_ASET = re.compile(
    r"^matrix aset(?:\s+([A-Za-z0-9_-]*):([a-z]+))?\s+(\S+)\s+(.+)$", re.I
)


class AVProEdgeMXNetUSPSimulator(TCPSimulator):
    """MXNet USP control box."""

    SIMULATOR_INFO = {
        "driver_id": "avproedge_mxnet_usp",
        "name": "AVPro Edge MXNet USP Control Box Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 24,
        "delimiter": "\r\n",
        "initial_state": {
            # What `config get name` answers. AC-MXNET-CBOX-HA is the current
            # hardware in its USP mode; the other names are there to check
            # that the driver refuses a control box of another ecosystem.
            "cbox_name": "AC-MXNET-USP-CBOX",
            "firmware": "3.42.5",
            "timezone": "UTC+0",
            "serial_loopback": True,
            "last_command": "",
            "media_storage": "844M",
            "online_188A6ACE87DC": True,
            "online_188A6A0F4485": True,
            "online_188A6A2B11C0": True,
            "online_188A6A45C4A5": True,
            "online_188A6A45C4A6": True,
            "online_188A6A1887E3": False,
            "signal_188A6ACE87DC": True,
            # An online encoder with nothing plugged in: present, no signal,
            # and sitting in s_attaching forever.
            "signal_188A6A0F4485": False,
            "signal_188A6A2B11C0": True,
            "route_video_188A6A45C4A5": "Cable-Box",
            "route_video_188A6A45C4A6": "188A6A0F4485",
            "route_video_188A6A1887E3": "",
        },
        "controls": [
            {
                "type": "select",
                "key": "cbox_name",
                "label": "Control Box Name",
                "options": [
                    "AC-MXNET-USP-CBOX",
                    "AC-MXNET-CBOX-HA",
                    "AC-MXNET-10G-CBOX",
                    "AC-MXNET-DANTE-CBOX",
                    "AC-MXNET-CBOX-B",
                ],
            },
            {"type": "indicator", "key": "last_command", "label": "Last Command"},
            {"type": "toggle", "key": "signal_188A6ACE87DC", "label": "Cable-Box Signal"},
            {"type": "toggle", "key": "signal_188A6A0F4485", "label": "Unnamed ES Signal"},
            {"type": "toggle", "key": "signal_188A6A2B11C0", "label": "Stage-PR Signal"},
            {"type": "toggle", "key": "online_188A6A1887E3", "label": "Boardroom Online"},
            {"type": "toggle", "key": "serial_loopback", "label": "Serial Loopback"},
            {"type": "indicator", "key": "route_video_188A6A45C4A5", "label": "Bar-Left Source"},
            {"type": "indicator", "key": "route_video_188A6A45C4A6", "label": "Unnamed ES Source"},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._line_mode = True
        if self.config.get("cbox_name"):
            self.set_state("cbox_name", str(self.config["cbox_name"]))

        # Instance copies so a test can add or drop an endpoint and exercise
        # roster reconciliation.
        self._eps: dict[str, dict[str, Any]] = {}
        for mac, (txid, rxid, mode, product, firmware, channel) in ENDPOINTS.items():
            self._eps[mac] = {
                "txid": txid,
                "rxid": rxid,
                "host": mode == "encoder",
                "client": mode == "decoder",
                "product": product,
                "firmware": firmware,
                "channel": channel,
                "edid": "2",
                "hdcp": "3",
                "hdcprequest": "0",
                "hdrmode": "1",
                "stream": "on",
                "hdmi": "on",
                "osd": "on",
                "timing": "0 0 0",
                "serial_setting": "9600 8 0 1",
                # Base64 is the endpoint's default (rs232responsetype, V1.08).
                "responsetype": "1",
                "txdescription": "",
                "rxdescription": "",
                "exaudio_type": "1",
                "exaudio_volume": 100,
                "hdmioutaudio": "1",
                "sourceinput": "auto",
                "exstream": 0,
                "exstream_settings": "",
                "exstream_bridge": "off",
                "dante_id": "",
                "dante_description": "",
                "dante_ip": "DHCP/169.254.20.11/255.255.0.0/0.0.0.0",
                "playlist": "",
                "playing": "",
                "uploaded": set(),
            }

        # decoder mac -> {plane: encoder mac or ""}
        self._routes: dict[str, dict[str, str]] = {}
        for mac, ep in self._eps.items():
            if ep["client"]:
                src = INITIAL_ROUTES.get(mac, "")
                self._routes[mac] = {
                    p: (src if p in ("video", "audio") else "") for p in PLANES
                }

        self._matrices = dict(MATRICES)
        self._walls = {w: {ln: list(t) for ln, t in ls.items()} for w, ls in VIDEOWALLS.items()}
        self._mv = json.loads(json.dumps(MULTIVIEW_LAYOUTS))
        self._mv_active: str = ""
        self._lists = json.loads(json.dumps(PLAYLISTS))
        self._ntp = ["0.north-america.pool.ntp.org", "1.north-america.pool.ntp.org"]
        self._dns = ["8.8.8.8", "8.8.4.4"]
        self._date = "2026-11-06 09:15:00"

    # ── Helpers ──────────────────────────────────────────────────────

    def _current_id(self, mac: str) -> str:
        """The id the control box reports an endpoint by: its name, else its MAC."""
        ep = self._eps[mac]
        name = ep["txid"] if ep["host"] else ep["rxid"]
        return name or mac

    def _route_id(self, mac: str, as_decoder: bool) -> str:
        """How the route reply names an endpoint: custom name, else lowercase MAC."""
        ep = self._eps[mac]
        name = ep["rxid"] if as_decoder else ep["txid"]
        return name or mac.lower()

    def _by_name(self, token: str) -> str | None:
        """Resolve a MAC or either custom name to a MAC, the way the control box does."""
        token = token.strip()
        upper = token.upper()
        if upper in self._eps:
            return upper
        low = token.lower()
        for mac, ep in self._eps.items():
            if low and (ep["txid"].lower() == low or ep["rxid"].lower() == low):
                return mac
        return None

    def _targets(self, token: str) -> list[str]:
        """Expand a target token: MAC / name / colon-list / ALL / ALLTX / ALLRX."""
        token = token.strip()
        upper = token.upper()
        if upper == "ALL":
            return list(self._eps)
        if upper == "ALLTX":
            return [m for m, e in self._eps.items() if e["host"]]
        if upper in ("ALLRX", "ALL_RX"):
            return [m for m, e in self._eps.items() if e["client"]]
        macs = []
        for part in token.split(":"):
            mac = self._by_name(part)
            if mac:
                macs.append(mac)
        return macs

    def _online(self, mac: str) -> bool:
        return bool(self.state.get(f"online_{mac}", True))

    def _signal(self, mac: str) -> bool:
        return bool(self.state.get(f"signal_{mac}", False))

    def _heartbeat(self) -> int:
        """The control box's heartbeat clock.

        A box with no NTP counts from the epoch, which is what the API
        document's examples show (values around 14000-17500). This models that
        rather than a true Unix time, because a driver comparing heartbeats
        against ITS OWN clock would pass against a synced box and call every
        endpoint offline against this one.
        """
        return int(time.monotonic()) + 14000

    @staticmethod
    def _ip(mac: str) -> str:
        return f"169.254.{int(mac[-4:-2], 16) % 250}.{int(mac[-2:], 16) % 250}"

    @staticmethod
    def _frame(doc: dict[str, Any]) -> bytes:
        return (json.dumps(doc, separators=(",", ":")) + "\r\n").encode()

    def _ok(self, command: str, info: Any = "OK") -> bytes:
        return self._frame({"cmd": command, "info": info, "code": 0})

    def _done(self, command: str) -> bytes:
        """The document's reply to most sets: {"code":0,"cmd":...}."""
        return self._frame({"code": 0, "cmd": command})

    def _err(self, command: str, message: str) -> bytes:
        # The simulator's guess at a USP refusal (see the module docstring).
        return self._frame({"error": message, "cmd": command, "code": -1})

    def _codeless(self, command: str, info: str = "OK") -> bytes:
        """`{"info":"OK","cmd":"..."}`, which carries no code at all."""
        return self._frame({"info": info, "cmd": command})

    def _escaped(self, command: str, info: str | None = None) -> bytes:
        """`{"info":..,"cmd":"{\\"cmd\\":\\"...\\"}"}`: the echo is escaped JSON, no code."""
        doc: dict[str, Any] = {}
        if info is not None:
            doc["info"] = info
        doc["cmd"] = json.dumps({"cmd": command}, separators=(",", ":"))
        return self._frame(doc)

    @staticmethod
    def _bare(text: str) -> bytes:
        """A reply that is not JSON at all: a Lua table, or a bare OK."""
        return (text + "\r\n").encode()

    def _sync_route_state(self, mac: str) -> None:
        if f"route_video_{mac}" not in self.state:
            return
        src = self._routes[mac]["video"]
        self.set_state(f"route_video_{mac}", self._current_id(src) if src else "")

    # ── Reply builders ───────────────────────────────────────────────

    def _devicelist(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for mac, ep in self._eps.items():
            entry: dict[str, Any] = {
                "ch": ep["channel"],
                "id": self._current_id(mac),
                "ipmode": "dhcp",
                "rs232mode": "2",
                "version": ep["firmware"],
                "mac": mac,
                "dtype": "ast152x",
                "ip": self._ip(mac),
            }
            # Presence is the `online` heartbeat and NOTHING else. A real
            # control box omits the member entirely for an endpoint that is not
            # there; `state` is the streaming service state and is present
            # either way.
            if self._online(mac):
                entry["online"] = self._heartbeat()
                live = self._signal(mac)
                entry["state"] = "s_attaching" if ep["host"] and not live else "s_srv_on"
            if ep["host"]:
                entry["is_host"] = 1
                entry["edid"] = ep["edid"]
                # The document says `exstream set` "modifies the exstream
                # property of the device", so the simulator reports it here; on
                # a real box it may not be in this reply at all.
                entry["exstream"] = ep["exstream"]
            if ep["client"]:
                routes = self._routes[mac]
                # The channel members a decoder reports, as in the document's
                # example. An unrouted plane reports a channel no encoder hosts.
                members = {"ch_v": "video", "ch_a": "audio", "ch_u": "usb",
                           "ch_r": "infrared", "ch_s": "serial"}
                for member, plane in members.items():
                    src = routes[plane]
                    entry[member] = self._eps[src]["channel"] if src else "0001"
                entry["ch_p"] = "0000"
                entry["ch_c"] = "0000"
            # Keyed by the endpoint's CURRENT id, which is its MAC until it is
            # renamed and its custom name afterwards.
            out[self._current_id(mac)] = entry
        return out

    def _status(self, macs: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for mac in macs:
            ep = self._eps[mac]
            if ep["host"]:
                live = self._signal(mac)
                out[mac] = {
                    "version": ep["firmware"],
                    "colordepth": "8Bit" if live else "",
                    "speed": "3",
                    "hdcp": "HDCP1" if live else "HDCP0",
                    "audio": "PCM" if live else "",
                    "ch": ep["channel"],
                    "status": 0,
                    "hpd": "HPD1" if live else "HPD0",
                    "dtype": "ast152x",
                    "hdr": "HDR0",
                    "light": 0,
                    "is_host": 1,
                    "edid": ep["edid"],
                    "ip": self._ip(mac),
                    "video": " 3840X2160p/30Hz" if live else "",
                    "id": self._current_id(mac),
                }
            else:
                src = self._routes[mac]["video"]
                showing = bool(src) and self._signal(src) and ep["stream"] == "on"
                out[mac] = {
                    "version": ep["firmware"],
                    "colordepth": "8Bit" if showing else "",
                    "speed": "3",
                    # A decoder spells HDCP in words, where an encoder spells it
                    # as a token plus a digit (seen on the 1G control box).
                    "hdcp": "HDCP ON" if showing else "HDCP OFF",
                    "audio": "PCM" if showing else "",
                    "ch": "0000",
                    "status": 0,
                    "hpd": "HPD1",
                    "dtype": "ast152x",
                    "hdr": "HDR0",
                    "light": 0,
                    "ip": self._ip(mac),
                    "video": " 3840X2160p/30Hz" if showing else "",
                    "id": self._current_id(mac),
                }
            if self._online(mac):
                out[mac]["online"] = self._heartbeat()
        return out

    def _routes_table(self, macs: list[str], planes: tuple[str, ...]) -> str:
        """The Lua table `config get device routes` answers with, on one line.

        Spaced as irregularly as the document's example, which is the point:
        nothing reading it may depend on the spacing.
        """
        parts = []
        for i, mac in enumerate(macs):
            routes = self._routes[mac]
            fields = []
            for plane in sorted(planes, key=lambda p: ROUTE_WIRE[p]):
                src = routes[plane]
                value = self._route_id(src, as_decoder=False) if src else "none"
                fields.append(f'{ROUTE_WIRE[plane]} = "{value}"')
            key = self._route_id(mac, as_decoder=True)
            if i % 2:
                parts.append(f'["{key}"] = {{ ' + ", ".join(fields) + " }")
            else:
                parts.append(f'["{key}"] = {{' + ",".join(fields) + " }")
        return "{ " + ", ".join(parts) + " }"

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
                    lines.append(",\n".join(f'        "{tile}"' for tile in tiles))
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

        for handler in (self._system, self._queries, self._videowall, self._multiview,
                        self._media, self._matrix, self._routing, self._device):
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
            return self._done(line)

        m = re.match(r"^config set timezone (UTC[+-](?:\d|1[0-2]))$", line, re.I)
        if m:
            self.set_state("timezone", m.group(1).upper())
            return self._done(line)
        m = re.match(r"^config set ntp (.+)$", line, re.I)
        if m:
            servers = m.group(1).split()
            if len(servers) > 5:
                return self._err(line, "at most 5 NTP servers")
            self._ntp = servers
            return self._done(line)
        m = re.match(r"^config set dns (.+)$", line, re.I)
        if m:
            servers = m.group(1).split()
            if len(servers) > 2:
                return self._err(line, "at most 2 DNS servers")
            self._dns = servers
            return self._done(line)
        m = re.match(r"^config set date (\d+) (\d+) (\d+) (\d+) (\d+) (\d+)$", line, re.I)
        if m:
            y, mo, d, h, mi, s = (int(g) for g in m.groups())
            self._date = f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"
            return self._done(line)
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
            name = self._current_id(macs[0])
            return self._ok(line, self._devicelist()[name])

        m = _RE_ROUTES.match(line)
        if m:
            letters = (m.group(1) or "vaurs").replace(",", "").lower()
            planes = tuple(dict.fromkeys(ROUTE_LETTERS[c] for c in letters))
            macs = [mac for mac in self._targets(m.group(2)) if self._eps[mac]["client"]]
            if not macs:
                return self._err(line, f"decoder {m.group(2)} not found")
            # Not JSON: a Lua table with no cmd and no code (V1.08).
            return self._bare(self._routes_table(macs, planes))

        m = _RE_DANTE_GET.match(line)
        if m:
            mac = self._by_name(m.group(1))
            if mac is None:
                return self._err(line, f"device {m.group(1)} not found")
            if self._eps[mac]["product"] not in DANTE_PRODUCTS:
                # What an AC-MXNET-USP-ES answers is undocumented: the
                # simulator's guess.
                return self._err(line, "This device has no Dante module")
            return self._ok(line, self._eps[mac]["dante_ip"])
        return None

    def _videowall(self, line: str) -> bytes | None:
        """The `vwid` family, whose replies are mostly NOT JSON."""
        low = line.lower()
        if low == "vwid list":
            return self._bare(self._wall_table(self._walls))

        m = re.match(r"^vwid get (\S+)$", line, re.I)
        if m:
            wall = self._wall_of(m.group(1))
            if wall is None:
                return self._bare("ERROR: videowall not found")
            return self._bare(self._wall_table({wall: self._walls[wall]}))

        m = re.match(r"^vwid layout list (\S+)$", line, re.I)
        if m:
            wall = self._wall_of(m.group(1))
            if wall is None:
                return self._bare("ERROR: videowall not found")
            return self._bare(self._wall_table({wall: self._walls[wall]}))

        m = re.match(r"^vwid layout active (\S+)\s+(\S+)$", line, re.I)
        if m:
            wall = self._wall_of(m.group(1))
            if wall is None:
                return self._bare("ERROR: videowall not found")
            layout = self._layout_of(wall, m.group(2))
            if layout is None:
                return self._bare("ERROR: layout not found")
            # Activating a layout applies its tiles, which is a route.
            for tile in self._walls[wall][layout]:
                parts = tile.split(":")
                if len(parts) < 4:
                    continue
                src = self._by_name(parts[2])
                dst = self._by_name(parts[3])
                if src and dst and self._eps[dst]["client"]:
                    self._routes[dst]["video"] = src
                    self._sync_route_state(dst)
            return self._bare("OK")

        m = re.match(r"^vwid layout tx (\S+)\s+(\S+)\s+(\S+)\s+(\S+)$", line, re.I)
        if m:
            wall = self._wall_of(m.group(1))
            if wall is None:
                return self._err(line, "videowall not found")
            layout = self._layout_of(wall, m.group(2))
            if layout is None:
                return self._err(line, "layout not found")
            new = self._by_name(m.group(4))
            if new is None or not self._eps[new]["host"]:
                return self._err(line, f"encoder {m.group(4)} not found")
            where = m.group(3)
            old = None if re.match(r"^\d+:\d+$", where) or where.upper() == "ALL" else self._by_name(where)
            if old is None and not (re.match(r"^\d+:\d+$", where) or where.upper() == "ALL"):
                return self._err(line, f"{where} is not a position or an encoder")
            tiles = []
            for tile in self._walls[wall][layout]:
                parts = tile.split(":")
                hit = (
                    where.upper() == "ALL"
                    or (old is not None and self._by_name(parts[2]) == old)
                    or f"{parts[0]}:{parts[1]}" == where
                )
                if hit:
                    parts[2] = self._current_id(new)
                tiles.append(":".join(parts))
            self._walls[wall][layout] = tiles
            return self._ok(line, "OK")

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
            return self._bare('{"info":"OK","cmd":"{"cmd":"' + line + '"}"}')
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

    def _multiview(self, line: str) -> bytes | None:
        """The `mvid` family (multiview layouts on a decoder)."""
        m = re.match(r"^mvid get(?:\s+(layouts|templates))?$", line, re.I)
        if m:
            which = (m.group(1) or "").lower()
            if which == "layouts":
                return self._ok(line, self._mv)
            if which == "templates":
                return self._ok(line, {})
            return self._ok(line, {"templates": {}, "layouts": self._mv})

        m = re.match(r"^mvid layout active (\S+)$", line, re.I)
        if m:
            if m.group(1) not in self._mv:
                return self._err(line, f"layout {m.group(1)} not found")
            self._mv_active = m.group(1)
            return self._ok(line, "OK")

        m = re.match(r"^mvid layout rx (\S+)\s+(\S+)$", line, re.I)
        if m:
            if m.group(1) not in self._mv:
                return self._err(line, f"layout {m.group(1)} not found")
            macs = []
            for part in m.group(2).split(":"):
                mac = self._by_name(part)
                if mac is None or not self._eps[mac]["client"]:
                    return self._err(line, f"decoder {part} not found")
                macs.append(mac)
            self._mv[m.group(1)]["client"] = ":".join(mac.lower() for mac in macs)
            # Answered with no code at all, as the document shows.
            return self._codeless(line, "OK")

        m = re.match(r"^mvid layout tx (\S+)\s+(\d+)\s+(\S+)$", line, re.I)
        if m:
            src = self._by_name(m.group(3))
            if src is None or not self._eps[src]["host"]:
                return self._err(line, f"encoder {m.group(3)} not found")
            # The document: a layout or window that does not exist is created.
            layout = self._mv.setdefault(
                m.group(1),
                {"windows": [], "audiomode": 2, "audioselect": "", "resolution": 5, "client": ""},
            )
            index = int(m.group(2))
            for window in layout["windows"]:
                if window["index"] == index:
                    window["host"] = self._route_id(src, as_decoder=False)
                    break
            else:
                layout["windows"].append({
                    "host": self._route_id(src, as_decoder=False), "width": "960",
                    "hposition": "0", "layer": index, "vposition": "0",
                    "height": "540", "index": index,
                })
            return self._ok(line, "OK")

        m = re.match(r"^mvid layout audio (\S+)\s+([012])(?:\s+(\S+))?$", line, re.I)
        if m:
            if m.group(1) not in self._mv:
                return self._err(line, f"layout {m.group(1)} not found")
            mode = int(m.group(2))
            if mode in (0, 1) and not m.group(3):
                return self._err(line, "audio mode 0 and 1 need a window or a source")
            self._mv[m.group(1)]["audiomode"] = mode
            self._mv[m.group(1)]["audioselect"] = m.group(3) or ""
            return self._ok(line, "OK")
        return None

    def _media(self, line: str) -> bytes | None:
        """The media player (`play`)."""
        low = line.lower()
        if low == "play get":
            return self._ok(line, {"schedules": [], "playlist": self._lists})
        if low == "play getstorage":
            # Escaped echo, no code (V1.08).
            return self._escaped(line, str(self.state["media_storage"]))

        m = re.match(r"^play pl upload (\S+)\s+(\S+)$", line, re.I)
        if m:
            if m.group(1) not in self._lists:
                return self._err(line, f"playlist {m.group(1)} not found")
            macs = []
            for part in m.group(2).split(","):
                mac = self._by_name(part)
                if mac is None or not self._eps[mac]["client"]:
                    return self._err(line, f"decoder {part} not found")
                macs.append(mac)
            for mac in macs:
                self._eps[mac]["uploaded"].add(m.group(1))
            return self._codeless(line, "OK")

        m = re.match(r"^play pl apply (\S+)\s+(\S+)$", line, re.I)
        if m:
            mac = self._by_name(m.group(2))
            if m.group(1) not in self._lists:
                return self._err(line, f"playlist {m.group(1)} not found")
            if mac is None or not self._eps[mac]["client"]:
                return self._err(line, f"decoder {m.group(2)} not found")
            self._eps[mac]["playlist"] = m.group(1)
            return self._ok(line, "OK")

        m = re.match(r"^play pl start (\S+)\s+(\S+)$", line, re.I)
        if m:
            mac = self._by_name(m.group(2))
            if m.group(1) not in self._lists:
                return self._err(line, f"playlist {m.group(1)} not found")
            if mac is None or not self._eps[mac]["client"]:
                return self._err(line, f"decoder {m.group(2)} not found")
            if m.group(1) not in self._eps[mac]["uploaded"]:
                # The document says a playlist has to be uploaded first; what
                # the box answers otherwise is the simulator's guess.
                return self._err(line, "playlist has not been uploaded to this decoder")
            self._eps[mac]["playing"] = m.group(1)
            return self._codeless(line, "OK")

        m = re.match(r"^play pl stop (\S+)$", line, re.I)
        if m:
            mac = self._by_name(m.group(1))
            if mac is None or not self._eps[mac]["client"]:
                return self._err(line, f"decoder {m.group(1)} not found")
            self._eps[mac]["playing"] = ""
            # Escaped echo, and no info or code at all (V1.08).
            return self._escaped(line)
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
            # family, and the reason the driver routes with the per-plane
            # `*path` commands instead.
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
            if src is None or not self._eps[src]["host"]:
                return self._err(line, f"encoder {m.group(3)} not found")
            for token in m.group(4).split():
                dst = self._by_name(token)
                if dst is None or not self._eps[dst]["client"]:
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
                srcs[self._current_id(mac)] = self._current_id(src)
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
            if src is None or not self._eps[src]["host"]:
                return self._err(line, f"encoder {m.group(2)} not found")
            if dst is None or not self._eps[dst]["client"]:
                return self._err(line, f"decoder {m.group(3)} not found")
            refused = self._offline_route(line, src, dst)
            if refused is not None:
                return refused
            self._routes[dst][plane] = src
            self._sync_route_state(dst)
            return self._done(line)

        m = _RE_PATH_OFF.match(line)
        if m:
            key = m.group(1).lower().replace("disable", "")
            if key not in PATH_PLANES:
                return None
            dst = self._by_name(m.group(2))
            if dst is None or not self._eps[dst]["client"]:
                return self._err(line, f"decoder {m.group(2)} not found")
            refused = self._offline_route(line, dst)
            if refused is not None:
                return refused
            self._routes[dst][PATH_PLANES[key]] = ""
            self._sync_route_state(dst)
            return self._done(line)
        return None

    def _device(self, line: str) -> bytes | None:
        m = _RE_SET_DEV.match(line)
        if not m:
            return None
        verb = m.group(1).lower()
        rest = m.group(2).strip()

        # Verbs whose last token is the target and which carry one value:
        # verb -> (field, allowed values, side that takes it, success info).
        simple: dict[str, tuple[str, tuple[str, ...], str, Any]] = {
            "edid": ("edid", tuple(str(i) for i in range(0, 21)), "host", "Set EDID success"),
            "hdcp": ("hdcp", ("0", "1", "2", "3"), "client", "Set HDCP success"),
            "hdrmode": ("hdrmode", ("0", "1"), "client", None),
            "stream": ("stream", ("on", "off"), "client", None),
            "osd": ("osd", ("on", "off"), "any", "ok"),
            "hdmioutaudio": ("hdmioutaudio", ("1", "2"), "client", "OK"),
            "sourceinput": ("sourceinput", ("auto", "hdmi", "usb"), "host", "OK"),
        }
        if verb in simple:
            field, allowed, side, info = simple[verb]
            parts = rest.split()
            if len(parts) != 2:
                return self._err(line, f"{verb} needs a value and a target")
            value, target = parts[0].lower(), parts[1]
            if value not in allowed:
                return self._err(line, f"invalid {verb} value {parts[0]}")
            macs = self._targets(target)
            if not macs:
                return self._err(line, f"device {target} not found")
            if side != "any" and not all(self._eps[mac][side] for mac in macs):
                return self._err(line, f"{verb} is a {'TX' if side == 'host' else 'RX'} setting")
            if verb == "sourceinput" and any(
                self._eps[mac]["product"] != "AC-MXNET-USP-PR" for mac in macs
            ):
                # A refusal that says code 0, exactly as V1.08 shows it.
                return self._ok(line, "Only AC-MXNET-USP-PR supports this command")
            for mac in macs:
                self._eps[mac][field] = value
            return self._done(line) if info is None else self._ok(line, info)

        if verb == "hdcprequest":
            parts = rest.split()
            if len(parts) != 2 or parts[0] not in ("0", "1"):
                return self._err(line, "hdcprequest takes 0/1 and a target")
            mac = self._by_name(parts[1])
            if mac is None or not self._eps[mac]["host"]:
                return self._err(line, f"encoder {parts[1]} not found")
            self._eps[mac]["hdcprequest"] = parts[0]
            # V1.08's example: escaped echo, no code, and the target named by
            # the endpoint's custom name rather than the MAC it was sent.
            echo = f"config set device hdcprequest {parts[0]} {self._current_id(mac)}"
            return self._escaped(echo, "Set HDCP success")

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
            return self._done(line)

        if verb == "hdmi":
            # "hdmi 0 on|off <tx>": a USP encoder has one HDMI input, port 0.
            parts = rest.split()
            if len(parts) != 3 or parts[0] != "0" or parts[1].lower() not in ("on", "off"):
                return self._err(line, "hdmi takes 0, on/off and a target")
            macs = self._targets(parts[2])
            if not macs or not all(self._eps[mac]["host"] for mac in macs):
                return self._err(line, f"encoder {parts[2]} not found")
            for mac in macs:
                self._eps[mac]["hdmi"] = parts[1].lower()
            return self._ok(line, "")

        if verb == "exaudio":
            parts = rest.split()
            if len(parts) != 3 or parts[0] not in ("type", "volume"):
                return self._err(line, "bad exaudio command")
            macs = self._targets(parts[2])
            if not macs:
                return self._err(line, f"device {parts[2]} not found")
            if not all(self._eps[mac]["client"] for mac in macs):
                # exaudio is an RX setting; what an encoder answers is the
                # simulator's guess.
                return self._err(line, "exaudio is an RX setting")
            if parts[0] == "type":
                if parts[1] not in ("1", "2", "4"):
                    return self._err(line, "audio type is 1, 2 or 4")
                if parts[1] == "2" and any(
                    self._eps[mac]["product"] not in DANTE_PRODUCTS for mac in macs
                ):
                    return self._err(line, "This device has no Dante module")
                for mac in macs:
                    self._eps[mac]["exaudio_type"] = parts[1]
            else:
                if not parts[1].isdigit() or not 0 <= int(parts[1]) <= 100:
                    return self._err(line, "volume is 0-100")
                for mac in macs:
                    self._eps[mac]["exaudio_volume"] = int(parts[1])
            return self._ok(line, "OK")

        if verb == "copyedid":
            parts = rest.split()
            if len(parts) != 2:
                return self._err(line, "copyedid needs a decoder and an encoder")
            src = self._by_name(parts[0])
            dst = self._by_name(parts[1])
            if src is None or not self._eps[src]["client"]:
                return self._err(line, f"decoder {parts[0]} not found")
            if dst is None or not self._eps[dst]["host"]:
                return self._err(line, f"encoder {parts[1]} not found")
            return self._ok(line, "Copy success")

        if verb == "light":
            parts = rest.split()
            if len(parts) != 2 or parts[0].lower() not in ("on", "off", "flash"):
                return self._err(line, "light takes on, off or flash")
            if not self._targets(parts[1]):
                return self._err(line, f"device {parts[1]} not found")
            return self._ok(line, "OK")

        if verb == "reboot":
            if not self._targets(rest):
                return self._err(line, f"device {rest} not found")
            return self._ok(line, "")

        if verb == "hpdrst":
            parts = rest.split()
            if len(parts) != 2 or parts[0] not in ("1", "2"):
                return self._err(line, "hpdrst takes 1 (HDMI input) or 2 (HDMI output) and a target")
            if not self._targets(parts[1]):
                return self._err(line, f"device {parts[1]} not found")
            return self._done(line)

        if verb in ("txid", "rxid"):
            parts = rest.split()
            if len(parts) != 2:
                return self._err(line, f"{verb} needs a new name and a target")
            name = parts[0]
            if name.upper() in ("ALL", "ALLRX", "ALLTX") or ":" in name or "," in name:
                return self._err(line, "reserved id")
            mac = self._by_name(parts[1])
            if mac is None:
                return self._err(line, f"device {parts[1]} not found")
            self._eps[mac][verb] = name
            for dst in self._routes:
                self._sync_route_state(dst)
            return self._done(line)

        if verb in ("txdescription", "rxdescription", "danteid", "dantedes"):
            # The description may contain spaces, so the target is the LAST token.
            parts = rest.rsplit(" ", 1)
            if len(parts) != 2 or not parts[0].strip():
                return self._err(line, f"{verb} takes a value and a target")
            macs = self._targets(parts[1])
            if not macs:
                return self._err(line, f"device {parts[1]} not found")
            if verb in ("danteid", "dantedes") and any(
                self._eps[mac]["product"] not in DANTE_PRODUCTS for mac in macs
            ):
                return self._err(line, "This device has no Dante module")
            field = {
                "txdescription": "txdescription",
                "rxdescription": "rxdescription",
                "danteid": "dante_id",
                "dantedes": "dante_description",
            }[verb]
            for mac in macs:
                self._eps[mac][field] = parts[0]
            return self._ok(line, "OK")

        if verb == "dante":
            # "dante ip dhcp <dev>" / "dante ip static:<ip>:<mask>:<gw> <dev>"
            mm = re.match(r"^ip\s+(dhcp|static:([\d.]+):([\d.]+):([\d.]+))\s+(\S+)$", rest, re.I)
            if not mm:
                return self._err(line, "dante ip takes dhcp or static:ip:mask:gateway and a target")
            mac = self._by_name(mm.group(5))
            if mac is None:
                return self._err(line, f"device {mm.group(5)} not found")
            if self._eps[mac]["product"] not in DANTE_PRODUCTS:
                return self._err(line, "This device has no Dante module")
            if mm.group(1).lower() == "dhcp":
                self._eps[mac]["dante_ip"] = "DHCP/169.254.20.11/255.255.0.0/0.0.0.0"
            else:
                self._eps[mac]["dante_ip"] = (
                    f"STATIC/{mm.group(2)}/{mm.group(3)}/{mm.group(4)}"
                )
            return self._ok(
                line, "The IP setting was successful.The Dante device will be restarted."
            )

        if verb == "exstream":
            parts = rest.split()
            if not parts:
                return self._err(line, "bad exstream command")
            mac = self._by_name(parts[-1])
            if mac is None or not self._eps[mac]["host"]:
                return self._err(line, f"encoder {parts[-1]} not found")
            ep = self._eps[mac]
            # The heading spells it "exstream set on|off"; the document's
            # example drops "set". Both are accepted.
            if parts[0] == "set":
                parts = parts[1:]
            if len(parts) == 2 and parts[0] in ("on", "off"):
                ep["exstream"] = 1 if parts[0] == "on" else 0
                return self._ok(line, "OK")
            if parts[0] == "bridge" and len(parts) == 3 and parts[1] in ("on", "off"):
                ep["exstream_bridge"] = parts[1]
                return self._ok(line, "OK")
            if parts[0] == "save" and len(parts) == 9:
                encoding, profile, protocol, _key, bitrate, _method, gop = parts[1:8]
                if encoding not in ("h264", "h265") or protocol not in ("rtsp", "rtmp"):
                    return self._err(line, "bad encoding or protocol")
                ok_profiles = ("baseline", "main", "high") if encoding == "h264" else ("main", "main10")
                if profile not in ok_profiles or not bitrate.isdigit() or not gop.isdigit():
                    return self._err(line, "bad profile, bitrate or gop")
                ep["exstream_settings"] = " ".join(parts[1:8])
                return self._ok(line, "OK")
            return self._err(line, "bad exstream command")

        if verb == "devicemode":
            parts = rest.split()
            if (
                len(parts) != 3
                or parts[0].lower() not in ("host", "nohost")
                or parts[1].lower() not in ("client", "noclient")
            ):
                return self._err(line, "devicemode takes host/nohost client/noclient and a target")
            mac = self._by_name(parts[2])
            if mac is None:
                return self._err(line, f"device {parts[2]} not found")
            host = parts[0].lower() == "host"
            client = parts[1].lower() == "client"
            if not host and not client:
                return self._err(line, "a device must be an encoder, a decoder or both")
            ep = self._eps[mac]
            ep["host"], ep["client"] = host, client
            if client and mac not in self._routes:
                self._routes[mac] = {p: "" for p in PLANES}
            if not client:
                self._routes.pop(mac, None)
            if not host:
                for routes in self._routes.values():
                    for plane, src in routes.items():
                        if src == mac:
                            routes[plane] = ""
            return self._ok(line, "")

        if verb == "cec":
            parts = rest.rsplit(" ", 1)
            if len(parts) != 2:
                return self._err(line, "cec takes hex data and a target")
            payload = parts[0].strip()
            if payload.lower() not in ("poweron", "poweroff") and not re.match(
                r"^[0-9A-Fa-f]{2,}(?:,[0-9A-Fa-f]{2,})*$", payload
            ):
                return self._err(line, "cec takes hex blocks separated by commas, poweron or poweroff")
            if not self._targets(parts[1]):
                return self._err(line, f"device {parts[1]} not found")
            return self._ok(line, "OK")

        if verb == "ir":
            parts = rest.rsplit(" ", 1)
            if len(parts) != 2 or not parts[0].strip():
                return self._err(line, "ir takes a code and a target")
            if not self._targets(parts[1]):
                return self._err(line, f"device {parts[1]} not found")
            return self._done(line)

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

        if verb == "rs232responsetype":
            parts = rest.split()
            if len(parts) != 2 or parts[0] not in ("1", "2", "3"):
                return self._err(line, "rs232responsetype takes 1, 2 or 3 and a target")
            macs = self._targets(parts[1])
            if not macs:
                return self._err(line, f"device {parts[1]} not found")
            for mac in macs:
                self._eps[mac]["responsetype"] = parts[0]
            return self._ok(line, "")

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
                    ack += self._serial_frame(mac, self._looped(payload, parts[0]))
            return ack

        return None

    # ── Unsolicited frames ───────────────────────────────────────────

    @staticmethod
    def _looped(payload: str, data_type: str) -> str:
        """The bytes a looped-back serial port would receive for this send."""
        if data_type == "2":
            try:
                return bytes.fromhex(payload.replace(" ", "")).decode("latin-1")
            except ValueError:
                return ""
        return payload.replace("\\r", "\r").replace("\\n", "\n")

    def _serial_frame(self, mac: str, received: str) -> bytes:
        """An unsolicited serial frame (empty cmd, source=rs232), encoded per
        the endpoint's rs232responsetype (1 Base64, 2 ASCII, 3 HEX)."""
        kind = self._eps[mac]["responsetype"]
        raw = received.encode("latin-1", errors="replace")
        if kind == "1":
            info = base64.b64encode(raw).decode()
        elif kind == "3":
            info = raw.hex().upper()
        else:
            info = received.strip("\r\n")
        return self._frame(
            {
                "info": info,
                "id": self._current_id(mac),
                "source": "rs232",
                "cmd": "",
                "code": 0,
                "mac": mac,
            }
        )

    def _event_frame(self, mac: str, info: str) -> bytes:
        """An unsolicited AV event (empty cmd, source=mxnet).

        Undocumented for USP and observed on the 1G control box, which is the
        same firmware family. They are the reason a driver may not hand an
        empty-cmd frame to whichever request is in flight.
        """
        return self._frame(
            {
                "info": info,
                "id": self._current_id(mac),
                "source": "mxnet",
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

    async def emit_serial(self, endpoint: str, payload: str) -> None:
        """Push serial data as if a device had sent it into an endpoint's port."""
        mac = self._by_name(endpoint)
        if mac is None:
            return
        await self.push(self._serial_frame(mac, payload))
