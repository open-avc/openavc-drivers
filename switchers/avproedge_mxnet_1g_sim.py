"""Simulator for the AVPro Edge MXNet 1G control box (AC-MXNET-CBOX).

Models a small MXNet system — three encoders and four decoders — behind the
CBOX's TCP 24 API: the device database, per-endpoint AV status, the five
per-stream routes each decoder holds, and the named preset / scene / video wall
/ KVM lists.

Replies are JSON objects in the CBOX's own shape ({"cmd", "info", "code"}, and
{"error", "cmd", "code": -1} when a command is rejected).

RS-232: the API doc's own feedback example wires an endpoint's serial TX pin
back to its RX pin, so sending data returns it. The `serial_loopback` control
reproduces that, which is what exercises the driver's unsolicited-frame path
(a serial frame carries an empty `cmd` and "source":"rs232").
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from simulator.tcp_simulator import TCPSimulator

# mac -> (name, kind, model, firmware, channel)
ENDPOINTS: dict[str, tuple[str, str, str, str, str]] = {
    "188A6A0067A2": ("Apple-TV", "encoder", "ast152x", "3.39", "0007"),
    "188A6A0F4485": ("Laptop-HDMI", "encoder", "ast152x", "3.39", "0002"),
    "188A6ACE87DC": ("Cable-Box", "encoder", "ast152x", "3.38", "0009"),
    "188A6A0102A8": ("Lobby-Display", "decoder", "ast152x", "4.22", "0000"),
    "188A6A45C4A5": ("Bar-Left", "decoder", "ast152x", "4.22", "0000"),
    "188A6A45C4A6": ("Bar-Right", "decoder", "ast152x", "4.22", "0000"),
    "188A6A1887E3": ("Boardroom", "decoder", "ast152x", "4.21", "0000"),
}

# The displays each decoder has plugged into it.
DISPLAYS = {
    "188A6A0102A8": "65Q825",
    "188A6A45C4A5": "SAMSUNG",
    "188A6A45C4A6": "SAMSUNG",
    "188A6A1887E3": "LG-OLED",
}

PRESETS = ["AllHands", "Lunch"]
SCENES = ["Evening"]
VIDEOWALLS = ["BarWall"]
KVM_LAYOUTS = ["BoardroomKVM"]

STREAMS = ("video", "audio", "usb", "ir", "rs232")

# `matrix aset :<code>` selectors, from the API doc.
ASET_CODES = {
    "z": STREAMS,
    "v": ("video",),
    "a": ("audio",),
    "u": ("usb",),
    "r": ("ir",),
    "s": ("rs232",),
}

PATH_STREAMS = {
    "videopath": "video",
    "audiopath": "audio",
    "usbpath": "usb",
    "irpath": "ir",
    "rs232path": "rs232",
}

_RE_ASET = re.compile(r"^matrix aset(?:\s+([A-Za-z0-9_-]*):([a-z]+))?\s+(\S+)\s+(.+)$", re.I)
_RE_PATH = re.compile(r"^config set device (\w+path)\s+(\S+)\s+(\S+)$", re.I)
_RE_PATH_OFF = re.compile(r"^config set device (\w+pathdisable)\s+(\S+)$", re.I)
_RE_SET_DEV = re.compile(r"^config set device (\S+)\s+(.+)$", re.I)
_RE_ROUTES = re.compile(r"^config get device routes\s+([vaurs]+)\s+(\S+)$", re.I)
_RE_STATUS = re.compile(r"^config get device status(?:\s+(\S+))?$", re.I)
_RE_ACTIVE = re.compile(
    r"^(matrix preset|scene|vw|kvm)\s+active\s+(\S+)(?:\s+(force))?$", re.I
)


class AVProEdgeMXNet1GSimulator(TCPSimulator):
    """AC-MXNET-CBOX control box."""

    SIMULATOR_INFO = {
        "driver_id": "avproedge_mxnet_1g",
        "name": "AVPro Edge MXNet CBOX Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 24,
        "delimiter": "\r\n",
        "initial_state": {
            "cbox_name": "AC-MXNET-CBOX",
            "firmware": "2.28",
            "previews": 1,
            "rs232_alias": "on",
            "timezone": "UTC+0",
            "serial_loopback": True,
            "last_command": "",
            # Per-endpoint UI state. Signal is an encoder concept, HPD a decoder one.
            "online_188A6A0067A2": True,
            "online_188A6A0F4485": True,
            "online_188A6ACE87DC": True,
            "online_188A6A0102A8": True,
            "online_188A6A45C4A5": True,
            "online_188A6A45C4A6": True,
            "online_188A6A1887E3": False,
            "signal_188A6A0067A2": True,
            "signal_188A6A0F4485": True,
            "signal_188A6ACE87DC": False,
            "route_video_188A6A0102A8": "Apple-TV",
            "route_video_188A6A45C4A5": "Laptop-HDMI",
            "route_video_188A6A45C4A6": "Laptop-HDMI",
            "route_video_188A6A1887E3": "",
        },
        "controls": [
            {"type": "indicator", "key": "cbox_name", "label": "Control Box"},
            {"type": "indicator", "key": "last_command", "label": "Last Command"},
            {"type": "toggle", "key": "signal_188A6A0067A2", "label": "Apple-TV Signal"},
            {"type": "toggle", "key": "signal_188A6ACE87DC", "label": "Cable-Box Signal"},
            {"type": "toggle", "key": "online_188A6A1887E3", "label": "Boardroom Online"},
            {"type": "toggle", "key": "serial_loopback", "label": "Serial Loopback"},
            {"type": "indicator", "key": "route_video_188A6A0102A8", "label": "Lobby Video Source"},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._line_mode = True

        # Instance copies so a test can add or drop an endpoint and exercise
        # roster reconciliation.
        self._eps: dict[str, dict[str, Any]] = {}
        for mac, (name, kind, model, firmware, channel) in ENDPOINTS.items():
            self._eps[mac] = {
                "name": name,
                "kind": kind,
                "model": model,
                "firmware": firmware,
                "channel": channel,
                "edid": "2",
                "volume": 100,
                "stream": "on",
                "blackout": "off",
                "rotate": "0",
                "stretch": "2",
                "pattern": "0",
                "hdcp": "0",
                "serial_type": "1",
                "serial_setting": "9600 8 0 1 0",
            }

        # decoder mac -> {stream: encoder mac or ""}
        self._routes: dict[str, dict[str, str]] = {}
        for mac, ep in self._eps.items():
            if ep["kind"] != "decoder":
                continue
            source = self.state.get(f"route_video_{mac}", "")
            src_mac = self._by_name(source) if source else ""
            self._routes[mac] = {s: (src_mac or "") for s in STREAMS}

        self._presets = list(PRESETS)
        self._scenes = list(SCENES)
        self._videowalls = list(VIDEOWALLS)
        self._kvm = list(KVM_LAYOUTS)
        self._ntp = [
            "0.north-america.pool.ntp.org",
            "1.north-america.pool.ntp.org",
        ]

    # ── Helpers ──────────────────────────────────────────────────────

    def _by_name(self, token: str) -> str | None:
        """Resolve a MAC or custom name to a MAC, the way the CBOX does."""
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

    def _ok(self, command: str, info: Any = "") -> bytes:
        return self._frame({"cmd": command, "info": info, "code": 0})

    def _err(self, command: str, message: str) -> bytes:
        return self._frame({"error": message, "cmd": command, "code": -1})

    @staticmethod
    def _frame(doc: dict[str, Any]) -> bytes:
        return (json.dumps(doc) + "\r\n").encode()

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
                "dtype": ep["model"],
                "version": ep["firmware"],
                "ipmode": "autoip",
                "rs232mode": "2",
                "online": 14147,
                "state": "s_srv_on" if self._online(mac) else "s_attaching",
                "ch": ep["channel"],
            }
            if ep["kind"] == "encoder":
                entry["is_host"] = 1
                entry["edid"] = ep["edid"]
                entry["exaudiovolume"] = str(ep["volume"])
            else:
                routes = self._routes[mac]
                entry["stream"] = ep["stream"]
                entry["blackout"] = ep["blackout"]
                entry["rotate"] = ep["rotate"]
                entry["stretch"] = ep["stretch"]
                entry["pattern"] = ep["pattern"]
                for wire, stream in (
                    ("ch_v", "video"),
                    ("ch_a", "audio"),
                    ("ch_u", "usb"),
                    ("ch_r", "ir"),
                    ("ch_s", "rs232"),
                ):
                    src = routes[stream]
                    entry[wire] = self._eps[src]["channel"] if src else "0000"
            out[mac] = entry
        return out

    def _status(self, macs: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for mac in macs:
            ep = self._eps[mac]
            if ep["kind"] == "encoder":
                live = bool(self.state.get(f"signal_{mac}", False))
                out[mac] = {
                    "id": ep["name"],
                    "video": "3840X2160p/60Hz" if live else "",
                    "audio": "PCM" if live else "",
                    "connectedname": "AppleTV" if live else "",
                    "hpd": "HPD1" if live else "HPD0",
                    "hdr": "HDR1" if live else "HDR0",
                    "hdcp": "HDCP2" if live else "",
                    "chroma": "YUV422" if live else "",
                    "colordepth": "8Bit" if live else "",
                    "speed": "1G",
                    "light": 0,
                    "profile": 0,
                    "switchip": "192.168.1.50",
                    "switchport": "4",
                }
            else:
                src = self._routes[mac]["video"]
                live = bool(src) and bool(self.state.get(f"signal_{src}", False))
                showing = live and ep["stream"] == "on"
                out[mac] = {
                    "id": ep["name"],
                    "video": "3840X2160p/60Hz" if showing else "",
                    "audio": "PCM" if showing else "",
                    "connectedname": DISPLAYS.get(mac, ""),
                    "hpd": "HPD1",
                    "hdr": "HDR1" if showing else "HDR0",
                    "hdcp": "HDCP2" if showing else "",
                    "chroma": "YUV422" if showing else "",
                    "colordepth": "8Bit" if showing else "",
                    "speed": "1G",
                    "light": 0,
                    "profile": 0,
                    "switchip": "192.168.1.50",
                    "switchport": "9",
                }
        return out

    def _routes_reply(self, selectors: str, macs: list[str]) -> dict[str, Any]:
        wanted = {
            "v": "video",
            "a": "audio",
            "u": "usb",
            "r": "ir",
            "s": "rs232",
        }
        out: dict[str, Any] = {}
        for mac in macs:
            if self._eps[mac]["kind"] != "decoder":
                continue
            entry = {}
            for sel in selectors:
                stream = wanted[sel]
                src = self._routes[mac][stream]
                entry[stream] = self._eps[src]["name"] if src else "none"
            out[self._eps[mac]["name"]] = entry
        return out

    # ── Dispatch ─────────────────────────────────────────────────────

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", errors="replace").strip("\r\n").strip()
        if not line:
            return None
        self.set_state("last_command", line)

        reply = self._system(line)
        if reply is None:
            reply = self._queries(line)
        if reply is None:
            reply = self._routing(line)
        if reply is None:
            reply = self._device(line)
        if reply is None:
            return self._err(line, "unknown command")
        return reply

    def _system(self, line: str) -> bytes | None:
        low = line.lower()
        if low == "config get name":
            return self._ok(line, self.state["cbox_name"])
        if low == "config get version":
            return self._ok(line, self.state["firmware"])
        if low == "config get ipsetting":
            return self._ok(line, "autoip/169.254.4.198/255.255.0.0/192.168.1.1")
        if low == "config get ipsetting2":
            return self._ok(line, "dhcp/192.168.1.239/255.255.255.0/192.168.1.1")
        if low == "config get previews":
            return self._ok(line, self.state["previews"])
        if low == "config get rs-232 alias":
            return self._ok(line, self.state["rs232_alias"])
        if low == "config get timezone":
            return self._ok(line, self.state["timezone"])
        if low == "config get ntp":
            return self._ok(line, "/".join(self._ntp))
        if low == "config set reboot":
            return self._ok(line, "OK")

        m = re.match(r"^config set previews (on|off)$", line, re.I)
        if m:
            self.set_state("previews", 1 if m.group(1).lower() == "on" else 0)
            return self._frame({"code": 0, "cmd": line})
        m = re.match(r"^config set rs-232 alias (on|off)$", line, re.I)
        if m:
            self.set_state("rs232_alias", m.group(1).lower())
            return self._ok(line, "OK")
        m = re.match(r"^config set timezone (UTC[+-](?:\d|1[0-2]))$", line, re.I)
        if m:
            self.set_state("timezone", m.group(1).upper())
            return self._ok(line, "OK")
        m = re.match(r"^config set ntp (.+)$", line, re.I)
        if m:
            servers = m.group(1).split()
            if len(servers) > 5:
                return self._err(line, "at most 5 NTP servers")
            self._ntp = servers
            return self._ok(line, "OK")
        return None

    def _queries(self, line: str) -> bytes | None:
        low = line.lower()
        if low == "config get devicelist":
            return self._ok(line, self._devicelist())

        m = _RE_STATUS.match(line)
        if m:
            token = m.group(1) or "ALL"
            macs = self._targets(token)
            if not macs:
                return self._err(line, f"device {token} not found")
            return self._ok(line, self._status(macs))

        m = _RE_ROUTES.match(line)
        if m:
            macs = self._targets(m.group(2))
            if not macs:
                return self._err(line, f"device {m.group(2)} not found")
            return self._frame(
                {"info": self._routes_reply(m.group(1).lower(), macs), "cmd": line, "code": 0}
            )

        lists = {
            "matrix preset list": self._presets,
            "scene list": self._scenes,
            "vw list": self._videowalls,
            "kvm list": self._kvm,
        }
        if low in lists:
            # The CBOX answers these as a name -> detail map.
            return self._ok(line, {name: {} for name in lists[low]})
        return None

    def _routing(self, line: str) -> bytes | None:
        m = _RE_ASET.match(line)
        if m:
            code = (m.group(2) or "v").lower()
            # The doc's long forms map onto the same selectors.
            long_form = {
                "video": "v",
                "audio": "a",
                "usb": "u",
                "infrared": "r",
                "serial": "s",
                "all": "z",
            }
            code = long_form.get(code, code)
            streams = ASET_CODES.get(code)
            if streams is None:
                return self._err(line, f"unknown matrix type {code}")
            src = self._by_name(m.group(3))
            if src is None or self._eps[src]["kind"] != "encoder":
                return self._err(line, f"encoder {m.group(3)} not found")
            for token in m.group(4).split():
                dst = self._by_name(token)
                if dst is None or self._eps[dst]["kind"] != "decoder":
                    return self._err(line, f"decoder {token} not found")
                for stream in streams:
                    self._routes[dst][stream] = src
                self._sync_route_state(dst)
            return self._ok(line, "OK")

        m = _RE_PATH.match(line)
        if m and m.group(1).lower() in PATH_STREAMS:
            stream = PATH_STREAMS[m.group(1).lower()]
            src = self._by_name(m.group(2))
            dst = self._by_name(m.group(3))
            if src is None or self._eps[src]["kind"] != "encoder":
                return self._err(line, f"encoder {m.group(2)} not found")
            if dst is None or self._eps[dst]["kind"] != "decoder":
                return self._err(line, f"decoder {m.group(3)} not found")
            self._routes[dst][stream] = src
            self._sync_route_state(dst)
            return self._frame({"code": 0, "cmd": line})

        m = _RE_PATH_OFF.match(line)
        if m:
            key = m.group(1).lower().replace("disable", "")
            if key not in PATH_STREAMS:
                return None
            dst = self._by_name(m.group(2))
            if dst is None or self._eps[dst]["kind"] != "decoder":
                return self._err(line, f"decoder {m.group(2)} not found")
            self._routes[dst][PATH_STREAMS[key]] = ""
            self._sync_route_state(dst)
            return self._frame({"code": 0, "cmd": line})

        m = _RE_ACTIVE.match(line)
        if m:
            kind = m.group(1).lower()
            name = m.group(2)
            pool = {
                "matrix preset": self._presets,
                "scene": self._scenes,
                "vw": self._videowalls,
                "kvm": self._kvm,
            }[kind]
            if name not in pool:
                return self._err(line, f"{kind} {name} not found")
            return self._ok(line, "OK")
        return None

    def _device(self, line: str) -> bytes | None:
        m = _RE_SET_DEV.match(line)
        if not m:
            return None
        verb = m.group(1).lower()
        rest = m.group(2).strip()

        # Verbs whose last token is the target and which carry one value.
        simple: dict[str, tuple[str, tuple[str, ...]]] = {
            "stream": ("stream", ("on", "off")),
            "blackout": ("blackout", ("on", "off")),
            "rotate": ("rotate", ("0", "3", "5", "6", "90", "180", "270")),
            "stretch": ("stretch", ("1", "2")),
            "pattern": ("pattern", ("0", "1", "2")),
            "hdcp": ("hdcp", ("2", "3", "4")),
            "edid": ("edid", tuple(str(i) for i in range(1, 16))),
            "osd": ("osd", ("on", "off")),
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
                if field in self._eps[mac]:
                    self._eps[mac][field] = value
            return self._frame({"code": 0, "cmd": line})

        if verb == "audiomute":
            parts = rest.split()
            if len(parts) != 2 or not parts[0].isdigit() or not 0 <= int(parts[0]) <= 10000:
                return self._err(line, "audiomute takes 0-10000 ms")
            if not self._targets(parts[1]):
                return self._err(line, f"device {parts[1]} not found")
            return self._ok(line, "")

        if verb == "audio":
            # "audio volume <n> <rx>" and "audio input type <src> <tx>"
            parts = rest.split()
            if parts[:1] == ["volume"] and len(parts) == 3:
                if not self._targets(parts[2]):
                    return self._err(line, f"device {parts[2]} not found")
                return self._ok(line, "")
            if parts[:2] == ["input", "type"] and len(parts) == 4:
                if parts[2].lower() not in ("hdmi", "analog", "auto"):
                    return self._err(line, f"invalid audio input {parts[2]}")
                if not self._targets(parts[3]):
                    return self._err(line, f"device {parts[3]} not found")
                return self._ok(line, "OK")
            return self._err(line, "bad audio command")

        if verb == "exaudio":
            parts = rest.split()
            if parts[:1] != ["volume"] or len(parts) != 3:
                return self._err(line, "bad exaudio command")
            macs = self._targets(parts[2])
            if not macs:
                return self._err(line, f"device {parts[2]} not found")
            for mac in macs:
                self._eps[mac]["volume"] = int(parts[1])
            return self._ok(line, "OK")

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
            self._eps[dst]["edid"] = "14"
            return self._ok(line, "OK")

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
            return self._ok(line, "OK")

        if verb == "id":
            parts = rest.split()
            if len(parts) != 2:
                return self._err(line, "id needs a new name and a target")
            mac = self._by_name(parts[1])
            if mac is None:
                return self._err(line, f"device {parts[1]} not found")
            self._eps[mac]["name"] = parts[0]
            for dst in self._routes:
                self._sync_route_state(dst)
            return self._ok(line, "OK")

        if verb == "cec":
            parts = rest.rsplit(" ", 1)
            if len(parts) != 2 or not re.match(r"^[0-9A-Fa-f,]+$", parts[0]):
                return self._err(line, "cec takes hex data and a target")
            if not self._targets(parts[1]):
                return self._err(line, f"device {parts[1]} not found")
            return self._ok(line, "OK")

        if verb == "ir":
            parts = rest.rsplit(" ", 1)
            if len(parts) != 2 or not parts[0].strip():
                return self._err(line, "ir takes a code and a target")
            if not self._targets(parts[1]):
                return self._err(line, f"device {parts[1]} not found")
            return self._ok(line, "OK")

        if verb == "rs232setting":
            parts = rest.split()
            if len(parts) != 6:
                return self._err(line, "rs232setting takes 5 values and a target")
            macs = self._targets(parts[5])
            if not macs:
                return self._err(line, f"device {parts[5]} not found")
            for mac in macs:
                self._eps[mac]["serial_setting"] = " ".join(parts[:5])
            return self._ok(line, "OK")

        if verb == "rs232responsetype":
            parts = rest.split()
            if len(parts) != 2 or parts[0] not in ("1", "2", "3"):
                return self._err(line, "rs232responsetype takes 1, 2 or 3 and a target")
            macs = self._targets(parts[1])
            if not macs:
                return self._err(line, f"device {parts[1]} not found")
            for mac in macs:
                self._eps[mac]["serial_type"] = parts[0]
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
                # comes straight back as an unsolicited frame — exactly the
                # setup the API doc uses to demonstrate serial feedback.
                for mac in macs:
                    ack += self._serial_frame(mac, payload)
            return ack

        return None

    # ── Unsolicited RS-232 feedback ──────────────────────────────────

    def _serial_frame(self, mac: str, payload: str) -> bytes:
        """Build the CBOX's unsolicited serial frame (empty cmd, source=rs232)."""
        ep = self._eps[mac]
        # The endpoint's own responsetype decides the encapsulation.
        fmt = ep["serial_type"]
        text = payload.replace("\\r", "").replace("\\n", "")
        if fmt == "1":
            info = base64.b64encode(text.encode()).decode()
        elif fmt == "3":
            info = text.encode().hex().upper()
        else:
            info = text
        return self._frame(
            {
                "info": info,
                "id": ep["name"],
                "source": "rs232",
                "cmd": "",
                "code": 0,
                "mac": mac,
            }
        )

    async def emit_serial(self, endpoint: str, payload: str) -> None:
        """Push serial data as if a device had sent it into an endpoint's port."""
        mac = self._by_name(endpoint)
        if mac is None:
            return
        await self.push(self._serial_frame(mac, payload))
