"""
ZeeVee ZyPer Management Platform Simulator.

Simulates a ZMP's Telnet API (API Release 4.1x) fronting a small mixed
system, so the driver's roster, routing, read-back and live updates can be
exercised without a platform on the bench:

  Laptop_1     ZyPer4K-XS encoder
  Laptop_2     ZyPerUHD60 encoder
  DocCam       ZyPer4K encoder (offline)
  Proj-Left    ZyPer4K-XS decoder
  Proj-Right   ZyPer4K-XS decoder
  Lobby        ZyPerUHD60 decoder
  Wall_TL      ZyPerUHD60 decoder  } video wall LobbyWall (1 x 2)
  Wall_TR      ZyPerUHD60 decoder  }

Reply fidelity follows the API manual:
  - the ``Zyper$ `` prompt with no line ending after every reply, and the
    command echoed back first (``echo`` config, on by default);
  - records under ``device(<mac>);`` headers, MACs printed without leading
    zeros the way the manual's examples print them;
  - lines longer than the CLI's wrap width broken mid-word (``line_wrap``
    config, 100 by default, the smallest width ``set server api lineWrap``
    takes);
  - ``Success`` after a reply, ``Error:(N) ...`` instead of it on a refusal
    (a genlocked join or a multiview on a ZyPerUHD60 decoder is refused);
  - ``show ... since N`` answering only units whose lastChangeId is N or
    more, with ``lastChangeIdMax(N);`` and ``lastDeleteIdMax(N);`` trailers.

Sign-in: none by default, as the ZMP ships. ``telnet_password`` set: a
``password:`` prompt. ``full_auth`` true: ``username:`` then ``password:``,
accepting any username with ``account_password`` (default ``admin``).

Events: a connection whose first command is ``events`` gets a DeviceStatus
event for every unit, then the ZMP User Manual's event lines as things
change (joins, the Simulator UI's cable and online toggles); any byte it
sends ends events mode, as the manual says.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

# name -> (MAC as the ZMP prints it, type, model, product code, IP)
UNITS: dict[str, tuple[str, str, str, str, str]] = {
    "Laptop_1": ("d8:80:39:9a:96:7", "encoder", "Zyper4KXS", "Z4KENCFXS", "192.168.0.31"),
    "Laptop_2": ("0:1c:d5:1:a:a3", "encoder", "ZyperUHD60", "ZUHD60-1E", "192.168.0.32"),
    "DocCam": ("d8:80:39:59:af:be", "encoder", "Zyper4K", "Z4KENCF", "192.168.0.33"),
    "Proj-Left": ("0:16:c0:4d:e3:12", "decoder", "Zyper4KXS", "Z4KDECFXS", "192.168.0.41"),
    "Proj-Right": ("0:16:c0:4d:e3:67", "decoder", "Zyper4KXS", "Z4KDECFXS", "192.168.0.42"),
    "Lobby": ("0:1c:d5:1:9:65", "decoder", "ZyperUHD60", "ZUHD60-1D", "192.168.0.51"),
    "Wall_TL": ("34:1b:22:81:f9:81", "decoder", "ZyperUHD60", "ZUHD60-1D", "192.168.0.52"),
    "Wall_TR": ("34:1b:22:81:f9:88", "decoder", "ZyperUHD60", "ZUHD60-1D", "192.168.0.53"),
}
ENCODERS = [n for n, u in UNITS.items() if u[1] == "encoder"]
DECODERS = [n for n, u in UNITS.items() if u[1] == "decoder"]
WALLS = {"LobbyWall": [["Wall_TL", "Wall_TR"]]}
MULTIVIEWS = {"Quad4K": 4}
ZONES = {"Classrooms": ["Proj-Left", "Proj-Right"], "Classrooms.Room2": []}
PRESETS = {
    "AllLaptop": [(d, "Laptop_1") for d in DECODERS],
    "Evening": [(d, "none") for d in DECODERS],
}
JOIN_MODES = ("fastSwitched", "genlocked", "genlockedScaled")
VIDEO_PORTS = (
    "auto", "hdmi", "hdmiOptionalIn", "usbc", "displayPort", "hdsdi",
    "12gsdi", "component", "composite", "s-video", "vga",
)

_PROMPT = b"Zyper$ "


def _norm_mac(mac: str) -> str:
    parts = mac.strip().lower().split(":")
    if len(parts) != 6:
        return mac.strip().lower()
    try:
        return ":".join(f"{int(p, 16):02x}" for p in parts)
    except ValueError:
        return mac.strip().lower()


def _tokens(line: str) -> list[str]:
    """Split a command line on spaces, keeping a quoted argument whole (the
    quotes and anything right after them stay with it)."""
    out: list[str] = []
    cur = ""
    quoted = False
    for ch in line:
        if ch == '"':
            quoted = not quoted
            cur += ch
        elif ch == " " and not quoted:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


class ZeeveeZmpSimulator(TCPSimulator):
    """ZyPer Management Platform simulator (Telnet API subset)."""

    SIMULATOR_INFO = {
        "driver_id": "zeevee_zmp",
        "name": "ZeeVee ZyPer Management Platform Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 23,
        "delimiter": "\r\n",
        "initial_state": {
            **{f"online_{n}": n != "DocCam" for n in UNITS},
            **{f"cable_{n}": n != "DocCam" for n in UNITS},
            "src_Proj-Left": "Laptop_1",
            "src_Proj-Right": "Laptop_2",
            "src_Lobby": "none",
            "src_Wall_TL": "Laptop_2",
            "src_Wall_TR": "Laptop_2",
            **{f"mode_{d}": "fastSwitched" for d in DECODERS},
            "mode_Proj-Left": "genlocked",
            "hdmi_Proj-Left": "Laptop_1",
            "hdmi_Proj-Right": "Laptop_2",
            "hdmi_Lobby": "none",
            "hdmi_Wall_TL": "none",
            "hdmi_Wall_TR": "none",
            **{f"analog_{d}": "none" for d in DECODERS},
            **{f"follow_{d}": d.startswith("Proj") for d in DECODERS},
            **{f"port_{e}": "auto" for e in ENCODERS},
            "wall_LobbyWall": "Laptop_2",
            "mv_Quad4K_1": "Laptop_1",
            "mv_Quad4K_2": "Laptop_2",
            "mv_Quad4K_3": "DocCam",
            "mv_Quad4K_4": "none",
            "last_command": "",
            "last_sent": "",
        },
        "controls": [
            {"type": "group", "label": "Encoder inputs", "controls": [
                {"type": "toggle", "key": f"cable_{e}", "label": f"{e} input"} for e in ENCODERS
            ]},
            {"type": "group", "label": "Online", "controls": [
                {"type": "toggle", "key": f"online_{n}", "label": n} for n in UNITS
            ]},
            {"type": "group", "label": "Routes", "controls": [
                {"type": "indicator", "key": f"src_{d}", "label": f"{d} video"} for d in DECODERS
            ] + [{"type": "indicator", "key": "wall_LobbyWall", "label": "LobbyWall"}]},
            {"type": "indicator", "key": "last_sent", "label": "Last RS-232 / IR / CEC sent"},
            {"type": "indicator", "key": "last_command", "label": "Last command"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._change_id = 1
        self._unit_change: dict[str, int] = {n: 1 for n in UNITS}
        self._event_writers: dict[str, asyncio.StreamWriter] = {}
        self._started = time.time()

    # ── State changes the ZMP announces ──

    def set_state(self, key, value):  # noqa: D401 - the base's own method
        old = self._state.get(key)
        super().set_state(key, value)
        if old == value:
            return
        name = key.split("_", 1)[1] if "_" in key else ""
        if name in UNITS:
            self._touch(name)
            if key.startswith("online_"):
                self._event(name, "DeviceStateChange", f"state={'up' if value else 'down'}")
                self._video_status_for(name)
            elif key.startswith("cable_"):
                self._event(name, "CableConnection",
                            f"cable={'connected' if value else 'disconnected'}")
                self._video_status_for(name)
            elif key.startswith("src_"):
                self._event(name, "VideoStatusChange", self._video_status(name))

    def _touch(self, name: str) -> None:
        self._change_id += 1
        self._unit_change[name] = self._change_id

    def _video_status_for(self, name: str) -> None:
        """A change at an encoder changes what its decoders receive."""
        if UNITS[name][1] == "decoder":
            self._event(name, "VideoStatusChange", self._video_status(name))
            return
        for dec in DECODERS:
            if self._state.get(f"src_{dec}") == name:
                self._event(dec, "VideoStatusChange", self._video_status(dec))

    def _video_status(self, dec: str) -> str:
        src = self._state.get(f"src_{dec}", "none")
        if not self._state.get(f"online_{dec}"):
            return "receivingVideoFromEncoder=no, reason=decoder down"
        if src == "none":
            return "receivingVideoFromEncoder=no, reason=decoder not joined"
        if src in MULTIVIEWS:
            return "receivingVideoFromEncoder=yes"
        if not self._state.get(f"online_{src}"):
            return "receivingVideoFromEncoder=no, reason=encoder down"
        if not self._state.get(f"cable_{src}"):
            return "receivingVideoFromEncoder=no, reason=encoder hdmi down"
        return "receivingVideoFromEncoder=yes"

    def _event(self, name: str, kind: str, message: str, change_id: int | None = None) -> None:
        if not self._event_writers:
            return
        mac = UNITS[name][0]
        stamp = time.strftime("%b-%d-%I:%M:%S:%p")
        cid = self._change_id if change_id is None else change_id
        line = self._wrap(f"Event::{kind}::{name}({mac})::{stamp}::{cid}::: {message}")
        data = (line + "\r\n").encode()
        for client_id, writer in list(self._event_writers.items()):
            try:
                writer.write(data)
                self.log_protocol("out", data, client_id)
            except (ConnectionError, OSError):
                self._event_writers.pop(client_id, None)

    def _device_status_event(self, name: str) -> str:
        if not self._state.get(f"online_{name}"):
            return "state=down"
        uptime = int(time.time() - self._started) + 3600
        if not self._state.get(f"cable_{name}"):
            msg = f"state=up, uptime={uptime}, cable=disconnected"
        else:
            msg = f"state=up, uptime={uptime}, cable=connected, sizeX=1920, sizeY=1080, fps=60"
        if UNITS[name][1] == "decoder":
            msg += ", " + self._video_status(name)
        return msg

    # ── Sign-in and events mode ──

    async def authenticate_client(self, reader, writer, client_id) -> bool:
        """Sign in, then read the first command here: one that is ``events``
        holds this connection in events mode, which the line handler cannot
        tell from another connection's commands."""

        async def emit(data: bytes) -> None:
            writer.write(data)
            await writer.drain()
            self.log_protocol("out", data, client_id)

        async def answer() -> str | None:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=60.0)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                return None
            if not raw:
                return None
            self.log_protocol("in", raw, client_id)
            return raw.decode("utf-8", errors="replace").strip("\r\n")

        if self.config.get("full_auth"):
            expected = str(self.config.get("account_password", "admin"))
            while True:
                await emit(b"username: ")
                if await answer() is None:
                    return False
                await emit(b"password: ")
                given = await answer()
                if given is None:
                    return False
                if given == expected:
                    break
        elif self.config.get("telnet_password"):
            expected = str(self.config.get("telnet_password"))
            while True:
                await emit(b"password: ")
                given = await answer()
                if given is None:
                    return False
                if given == expected:
                    break

        await emit(_PROMPT)
        first = await answer()
        while first is not None and not first.strip():
            await emit(_PROMPT)
            first = await answer()
        if first is None:
            return False
        if first.strip() == "events":
            if self.config.get("echo", True):
                await emit(b"events\r\n")
            self._event_writers[client_id] = writer
            for name in UNITS:
                self._event(name, "DeviceStatus", self._device_status_event(name), change_id=0)
            try:
                byte = await reader.read(1)
            except (ConnectionError, OSError):
                byte = b""
            finally:
                self._event_writers.pop(client_id, None)
            if not byte:
                return False
            await emit(_PROMPT)
            return True
        response = self.handle_command(first.encode())
        if response:
            await emit(response)
        return True

    # ── Commands ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", errors="replace").strip("\r\n").strip()
        if not line:
            return _PROMPT
        self.set_state("last_command", line)
        try:
            ok, lines = self._dispatch(line)
        except Exception:
            logger.exception("%s: error handling %r", self.name, line)
            ok, lines = False, ["Error:(1) Internal error"]
        out = []
        if self.config.get("echo", True):
            out.append(line)
        out.extend(self._wrap(ln) for ln in lines)
        if ok:
            out.append("Success")
        return ("\r\n".join(out) + "\r\n").encode() + _PROMPT

    def _wrap(self, line: str) -> str:
        width = int(self.config.get("line_wrap", 100) or 0)
        if width <= 0 or len(line) <= width:
            return line
        return "\r\n".join(line[i:i + width] for i in range(0, len(line), width))

    def _find(self, ident: str, kind: str | None = None) -> str | None:
        """A unit by MAC (either form) or name; a name may be given in part."""
        ident = ident.strip()
        for name, unit in UNITS.items():
            if kind and unit[1] != kind:
                continue
            if _norm_mac(unit[0]) == _norm_mac(ident) or name == ident:
                return name
        for name, unit in UNITS.items():
            if (not kind or unit[1] == kind) and ident and ident in name:
                return name
        return None

    def _dispatch(self, line: str) -> tuple[bool, list[str]]:
        t = _tokens(line)
        lower = [x.lower() for x in t]
        if lower[:3] == ["show", "server", "info"]:
            return True, self._server_info()
        if lower[:3] in (["show", "device", "config"], ["show", "device", "status"]) and len(t) >= 4:
            since = None
            if len(t) >= 6 and lower[4] == "since":
                since = int(t[5])
            return self._show_devices(lower[2], t[3], since)
        if lower[:3] == ["show", "device", "connections"]:
            return True, self._connections()
        if lower[:2] == ["show", "videowalls"]:
            return True, self._walls()
        if lower[:3] == ["show", "multiviews", "config"]:
            return True, self._multiviews()
        if lower[:2] == ["show", "zones"]:
            return True, [f"  {z}; {', '.join(m) if m else 'empty'}" for z, m in ZONES.items()]
        if lower[:1] == ["join"] and len(t) == 4:
            return self._join(t[1], t[2], t[3])
        if lower[:2] == ["set", "decoder"] and len(t) == 6 and lower[3] == "autoaudioconnections":
            dec = self._find(t[2], "decoder")
            if dec is None:
                return False, [f"Error:(12) Device {t[2]} not found"]
            self.set_state(f"follow_{dec}", lower[5] == "enabled")
            self._touch(dec)
            return True, []
        if lower[:2] == ["set", "device"] and len(t) == 5 and lower[3] == "videoport":
            enc = self._find(t[2], "encoder")
            if enc is None or t[4] not in VIDEO_PORTS:
                return False, [f"Error:(29) Device {t[2]} does not support or cannot change: videoPort with value {t[4]}."]
            self.set_state(f"port_{enc}", t[4])
            self._touch(enc)
            return True, []
        if lower[:2] == ["set", "multiview"] and len(t) == 7:
            mv, window, enc = t[2], int(t[4]), t[6]
            if mv not in MULTIVIEWS or not 1 <= window <= MULTIVIEWS[mv]:
                return False, [f"Error:(12) Multiview {mv} window {window} not found"]
            name = "none" if enc.lower() == "none" else self._find(enc, "encoder")
            if name is None:
                return False, [f"Error:(12) Device {enc} not found"]
            self.set_state(f"mv_{mv}_{window}", name)
            return True, []
        if lower[:1] == ["channel"] and len(t) == 3:
            return self._channel(lower[1], t[2])
        if lower[:2] == ["run", "preset"] and len(t) == 3:
            preset = t[2].strip('"')
            if preset not in PRESETS:
                return False, [f"Error:(12) Preset {preset} not found"]
            for dec, enc in PRESETS[preset]:
                self.set_state(f"src_{dec}", enc)
                self._touch(dec)
            return True, []
        if lower[:1] == ["send"] and len(t) >= 4:
            unit = self._find(t[1])
            if unit is None:
                return False, [f"Error:(12) Device {t[1]} not found"]
            text = " ".join(t[3:])
            if lower[2] == "rs232" and " " in text and not text.startswith('"'):
                return False, ["Error:(3) Bad syntax"]
            self.set_state("last_sent", f"{unit} {lower[2]} {text}")
            return True, []
        if lower[:1] == ["switch"] and len(t) == 4:
            if self._find(t[1]) is None:
                return False, [f"Error:(12) Device {t[1]} not found"]
            return True, []
        if lower[:1] == ["flashleds"] and len(t) == 2 or lower[:2] == ["restart", "device"] and len(t) == 3:
            if self._find(t[-1]) is None:
                return False, [f"Error:(12) Device {t[-1]} not found"]
            return True, []
        if lower[:1] in (["help"], ["?"]):
            return True, [" Help commands:", "    help", "    help all alphabetical"]
        return False, [f"Error:(2) Unknown command: {line}"]

    # ── Replies ──

    def _server_info(self) -> list[str]:
        up = sum(1 for n in UNITS if self._state.get(f"online_{n}"))
        z4k = sum(1 for u in UNITS.values() if u[2].startswith("Zyper4K"))
        return [
            "server(192.168.0.21);",
            "  server.gen; hostname=zyper.local, serverType=NUC-E, version=4.1.42133, "
            "previousVersion=4.0.41890, master=true, location=Rack A, contact=AV Team",
            "  server.gen; uptime=3d:0h:45m:32s, freeMem= 2.60GB, sdvoeVersion=3.6.0.1, "
            "bootCount=19, serialNumber=ZZM1MC00001A",
            "  server.gen; macAddress=00:0e:c6:8e:38:6f, managementMacAddress=54:b2:03:f0:ee:99",
            "  server.time; time=\"Mon Apr 8 09:38:06 2024\", timezone=America/New_York",
            "  server.license; productID=367d8850-f6f2-cd73-fbb1-54b203f0ee99, license=none",
            f"  server.license; Zyper4KLimit=24, Zyper4KDevices={z4k}, allDevices={len(UNITS)}, "
            f"allDevicesUp={up}, Zyper4KDevicesExceeded=0",
            "  server.deviceUpdates; active=0",
            f"lastChangeIdMax({self._change_id});",
        ]

    def _show_devices(self, what: str, ident: str, since: int | None) -> tuple[bool, list[str]]:
        key = ident.lower()
        if key == "all":
            names = list(UNITS)
        elif key in ("encoders", "decoders"):
            names = ENCODERS if key == "encoders" else DECODERS
        else:
            found = self._find(ident)
            if found is None:
                return False, [f"Error:(12) Device {ident} not found"]
            names = [found]
        if since is not None:
            names = [n for n in names if self._unit_change[n] >= since]
        lines: list[str] = []
        for name in names:
            lines.extend(self._config(name) if what == "config" else self._status(name))
        lines.append(f"lastChangeIdMax({self._change_id});")
        if what == "config":
            lines.append("lastDeleteIdMax(0);")
        return True, lines

    def _gen(self, name: str) -> str:
        mac, kind, model, _code, _ip = UNITS[name]
        state = "Up" if self._state.get(f"online_{name}") else "Down"
        return (f"  device.gen; model={model}, type={kind}, virtualType=none, name={name}, "
                f"state={state}, lastChangeId={self._unit_change[name]}")

    def _ref(self, name: str) -> tuple[str, str]:
        if name in UNITS:
            return UNITS[name][0], name
        return "none", name

    def _config(self, name: str) -> list[str]:
        mac, kind, model, code, ip = UNITS[name]
        lines = [
            f"device({mac});",
            self._gen(name),
            f"  device.gen; productCode={code}, productDescription=ZyPer {kind}",
            "  device.gen; firmware=2.1.0.0",
            f"  device.ip; mode=dhcp, address={ip}, mask=255.255.255.0, gateway=192.168.0.1",
        ]
        if kind == "encoder":
            lines.append(f"  device.ports; videoPort={self._state.get(f'port_{name}', 'auto')}")
            return lines
        src = self._state.get(f"src_{name}", "none")
        smac, sname = self._ref(src)
        mode = self._state.get(f"mode_{name}", "fastSwitched")
        if src in MULTIVIEWS:
            mode = "multiview"
        lines.append(f"  device.connectedEncoder; macAddr={smac}, name={sname}, connectionMode={mode}")
        hmac, hname = self._ref(self._state.get(f"hdmi_{name}", "none"))
        amac, aname = self._ref(self._state.get(f"analog_{name}", "none"))
        lines.append(f"  device.audioConnections; analogSourceMac={amac}, analogSourceName={aname}, "
                     f"hdmiAudioSourceMac={hmac}, hdmiAudioSourceName={hname}")
        follow = "true" if self._state.get(f"follow_{name}") else "false"
        lines.append(f"  device.autoAudioConnections; hdmiAudioFollowVideo={follow}")
        return lines

    def _status(self, name: str) -> list[str]:
        mac, kind, _model, _code, _ip = UNITS[name]
        lines = [f"device({mac});", self._gen(name) + ", uptime=0d:1h:0m:0s"]
        if kind == "encoder":
            if self._state.get(f"cable_{name}"):
                lines.append("  device.hdmiInput; cableConnected=connected, hdcp=inactive, "
                             "hdcpVersion=none, hdmi2.0=yes, horizontalSize=1920, "
                             "verticalSize=1080, fps=60.000, interlaced=no")
            else:
                lines.append("  device.hdmiInput; cableConnected=disconnected, hdcp=inactive, "
                             "hdcpVersion=none, hdmi2.0=no, horizontalSize=0, verticalSize=0, "
                             "fps=0.000, interlaced=no")
        return lines

    def _connections(self) -> list[str]:
        out = []
        for enc in ENCODERS:
            decs = [d for d in DECODERS if self._state.get(f"src_{d}") == enc]
            if decs:
                out.append(f"encoder.{enc}; {', '.join(decs)}")
        return out

    def _walls(self) -> list[str]:
        out = []
        for wall, rows in WALLS.items():
            src = self._state.get(f"wall_{wall}", "none")
            smac = UNITS[src][0] if src in UNITS else "none"
            out.append(f"videoWall({wall});")
            out.append(f"  videoWall.gen; videoSourceMac={smac}, numDisplayRows={len(rows)}, "
                       f"numDisplayCols={len(rows[0])}")
            out.append("  videoWall.bezel; top=0, bottom=0, left=0, right=0")
            for r, row in enumerate(rows, 1):
                cols = ", ".join(f"col{c}={d}" for c, d in enumerate(row, 1))
                out.append(f"  videoWall.decodersRow{r}; {cols}")
        return out

    def _multiviews(self) -> list[str]:
        out = []
        for mv, count in MULTIVIEWS.items():
            out.append(f"multiview({mv});")
            out.append("  multiview.audio; sourceWindow=1;")
            for w in range(1, count + 1):
                enc = self._state.get(f"mv_{mv}_{w}", "none")
                x, y = (w - 1) % 2 * 50, (w - 1) // 2 * 50
                out.append(f"  multiview.window{w}; encoder-name={enc}, percentPosX={x}, "
                           f"percentPosY={y}, percentSizeX=50, percentSizeY=50, layer=1;")
        return out

    def _join(self, src: str, dst: str, mode: str) -> tuple[bool, list[str]]:
        mode_l = mode.lower()
        if mode_l == "videowall":
            if dst not in WALLS:
                return False, [f"Error:(12) Video wall {dst} not found"]
            enc = "none" if src.lower() == "none" else self._find(src, "encoder")
            if enc is None:
                return False, [f"Error:(12) Device {src} not found"]
            self.set_state(f"wall_{dst}", enc)
            for row in WALLS[dst]:
                for dec in row:
                    self.set_state(f"src_{dec}", enc)
                    self._touch(dec)
            return True, []
        if mode_l == "multiview":
            dec = self._find(dst, "decoder")
            if dec is None or src not in MULTIVIEWS:
                return False, [f"Error:(12) {src} or {dst} not found"]
            if not UNITS[dec][2].startswith("Zyper4K"):
                return False, [f"Error:(29) Device {dec} does not support or cannot change: multiview with value {src}."]
            self.set_state(f"src_{dec}", src)
            self._touch(dec)
            return True, []
        if dst in ZONES:
            targets = ZONES[dst]
        else:
            dec = self._find(dst, "decoder")
            if dec is None:
                return False, [f"Error:(12) Device {dst} not found"]
            targets = [dec]
        enc = "none" if src.lower() == "none" else self._find(src, "encoder")
        if enc is None:
            return False, [f"Error:(12) Device {src} not found"]
        for dec in targets:
            if mode_l in ("genlocked", "genlockedscaled") and not UNITS[dec][2].startswith("Zyper4K"):
                return False, [f"Error:(29) Device {dec} does not support or cannot change: "
                               f"connectionMode with value {mode}."]
        for dec in targets:
            if mode_l in ("fastswitched", "genlocked", "genlockedscaled", "video"):
                if mode_l != "video":
                    self.set_state(f"mode_{dec}", mode if mode in JOIN_MODES else "fastSwitched")
                self.set_state(f"src_{dec}", enc)
                if self._state.get(f"follow_{dec}"):
                    self.set_state(f"hdmi_{dec}", enc)
            elif mode_l == "hdmiaudio":
                self.set_state(f"hdmi_{dec}", enc)
                self.set_state(f"follow_{dec}", False)
            elif mode_l == "analogaudio":
                self.set_state(f"analog_{dec}", enc)
            elif mode_l == "usb":
                pass
            else:
                return False, [f"Error:(3) Bad syntax: join mode {mode}"]
            self._touch(dec)
        return True, []

    def _channel(self, direction: str, ident: str) -> tuple[bool, list[str]]:
        dec = self._find(ident, "decoder")
        if dec is None:
            return False, [f"Error:(12) Device {ident} not found"]
        channels = sorted(
            (int(m.group(1)), n) for n in ENCODERS if (m := re.search(r"_(\d+)$", n)))
        if not channels:
            return False, ["Error:(40) No encoders with a channel suffix"]
        current = self._state.get(f"src_{dec}")
        names = [n for _c, n in channels]
        if current not in names:
            target = names[0]
        else:
            i = names.index(current) + (1 if direction == "up" else -1)
            target = names[i % len(names)]
        self.set_state(f"mode_{dec}", "fastSwitched")
        self.set_state(f"src_{dec}", target)
        self._touch(dec)
        return True, [f"Channel changed to {target}"]
