"""
WyreStorm NetworkHD Simulator.

Simulates an NHD-000-CTL / NHD-CTL-PRO controller speaking the NetworkHD
API v6.5 subset the driver exercises, fronting a small mixed-series system:

  source1   NHD-600-TX   (online)
  source2   NHD-400-TX   (online)
  source3   NHD-140-TX   (offline — exercises the roster's online flag)
  display1  NHD-600-RX   (online)
  display2  NHD-400-RX   (online)
  display3  NHD-220-RX   (online, multiview — has preset layouts)

Reply fidelity follows the API document:
  - every reply line ends CRLF;
  - JSON documents are pretty-printed across many lines with tab
    indentation and ``"key" : value`` spacing, exactly like the document's
    Appendix A examples (this is what proves the driver's block
    accumulator);
  - `config get device info` / `device status` render per-series field
    sets (600 vs 400 vs 100/200 series);
  - matrix queries answer the documented header + `<TX|NULL> <RX>` lines;
  - scene / wscene2 / mscene commands answer their documented
    success/failure acks;
  - anything unrecognized answers `unknown command`.

Notification helpers build documented `notify` lines (endpoint online/
offline, video lost/found, cecinfo, irinfo, serialinfo) — `emit_*`
coroutines push them to connected clients; the `build_*` functions return
the raw bytes so tests can inject them directly.
"""

from __future__ import annotations

import json
import logging
import re

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

# alias -> (hostname, series, kind)
ROSTER: dict[str, tuple[str, int, str]] = {
    "source1": ("NHD-600-TX-D88039E5E401", 600, "tx"),
    "source2": ("NHD-400-TX-E4CE02106E9E", 400, "tx"),
    "source3": ("NHD-140-TX-E4CE02102EE1", 100, "tx"),
    "display1": ("NHD-600-RX-D88039E5E525", 600, "rx"),
    "display2": ("NHD-400-RX-E4CE02102EF0", 400, "rx"),
    "display3": ("NHD-220-RX-E4CE02107132", 200, "rx"),
}

SCENES = ["OfficeVW-Splitmode", "OfficeVW-Combined"]
WSCENES = ["OfficeVW-windows"]
MSCENE_LAYOUTS = {"display3": ["gridlayout", "piplayout"]}
VW_ENTRIES = [("OfficeVW-Combined_TopTwo", "source1", ["display1", "display2"])]

_RE_MATRIX_SET = re.compile(
    r"^matrix(?: (video|audio2|audio3|audio|usb|infrared2|serial2|infrared|serial))?"
    r" set\s+(.+)$"
)
_RE_MATRIX_GET = re.compile(
    r"^matrix(?: (video|audio2|audio3|audio|usb|infrared2|serial2|infrared|serial))?"
    r" get\s*(.*)$"
)


def build_notify_endpoint(alias: str, online: bool) -> bytes:
    return f"notify endpoint {'+' if online else '-'} {alias}\r\n".encode()


def build_notify_video(event: str, name: str, tx: str | None = None) -> bytes:
    tail = f" {tx}" if tx else ""
    return f"notify video {event} {name}{tail}\r\n".encode()


def build_notify_cec(name: str, data: str) -> bytes:
    return f'notify cecinfo {name} "{data}"\r\n'.encode()


def build_notify_ir(name: str, data: str) -> bytes:
    return f'notify irinfo {name} "{data}"\r\n'.encode()


def build_notify_serial(name: str, fmt: str, data: str) -> bytes:
    return f"notify serialinfo {name} {fmt} {len(data)}:\r\n{data}\r\n".encode()


def _pretty(doc) -> str:
    """Render JSON the way the CTL does: tab indentation, ' : ' pair
    separators, CRLF line endings."""
    text = json.dumps(doc, indent="\t", separators=(",", " : "), sort_keys=True)
    return text.replace("\n", "\r\n")


class WyrestormNetworkHDSimulator(TCPSimulator):
    """NHD-CTL controller simulator (NetworkHD API v6.5 subset)."""

    SIMULATOR_INFO = {
        "driver_id": "wyrestorm_networkhd",
        "name": "WyreStorm NetworkHD Controller Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 23,
        "delimiter": "\r\n",
        "initial_state": {
            # Endpoint online flags.
            "online_source1": True,
            "online_source2": True,
            "online_source3": False,
            "online_display1": True,
            "online_display2": True,
            "online_display3": True,
            # Matrix assignment per RX (all-media view + breakaway views).
            "matrix_display1": "source1",
            "matrix_display2": "source2",
            "matrix_display3": "",
            "matrix_video_display1": "source1",
            "matrix_video_display2": "source2",
            "matrix_video_display3": "",
            "matrix_audio_display1": "source1",
            "matrix_audio_display2": "source2",
            "matrix_audio_display3": "",
            "matrix_usb_display1": "",
            "matrix_usb_display2": "source2",
            "matrix_usb_display3": "",
            # Encoder input signal presence (feeds device status).
            "signal_source1": True,
            "signal_source2": True,
            "signal_source3": False,
            "last_command": "",
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)

    # ── Command dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", errors="replace").strip("\r\n").strip()
        if not line:
            return None
        self.set_state("last_command", line)

        if line.startswith("config set session alias"):
            return self._reply(line)
        if line == "config get version":
            return self._reply("API version: v6.5", "System version: v8.3.1(v3.6.0)")
        if line == "config get devicelist":
            online = [a for a in ROSTER if self.state.get(f"online_{a}")]
            return self._reply("devicelist is " + " ".join(online))
        if line == "config get devicejsonstring":
            return self._device_json_string()
        if line.startswith("config get device info"):
            return self._device_info(line.removeprefix("config get device info").split())
        if line.startswith("config get device status"):
            return self._device_status(line.removeprefix("config get device status").split())

        m = _RE_MATRIX_SET.match(line)
        if m:
            return self._matrix_set(m.group(1) or "", m.group(2), line)
        m = _RE_MATRIX_GET.match(line)
        if m:
            return self._matrix_get(m.group(1) or "", m.group(2).split())

        if line == "scene get":
            return self._reply("scene list:", " ".join(SCENES))
        m = re.match(r"^scene active (\S+)$", line)
        if m:
            verdict = "success" if m.group(1) in SCENES else "failure"
            return self._reply(f"scene {m.group(1)} active {verdict}")
        m = re.match(r"^scene set (\S+) (\d+) (\d+) (\S+)$", line)
        if m:
            return self._reply(
                f"scene {m.group(1)}'s source in [{m.group(2)},{m.group(3)}] "
                f"change to {m.group(4)}")
        if line == "vw get":
            lines = ["Video wall information:"]
            for ref, tx, rxs in VW_ENTRIES:
                lines.append(f"{ref} {tx}")
                lines.append("Row 1: " + " ".join(rxs))
            return self._reply(*lines)
        m = re.match(r"^vw change (\S+) (\S+)$", line)
        if m:
            return self._reply(f"videowall change {m.group(1)} tx connect to {m.group(2)}")

        if line == "wscene2 get":
            return self._reply("wscene2 list:", " ".join(WSCENES))
        m = re.match(r"^wscene2 active (\S+)$", line)
        if m:
            verdict = "success" if m.group(1) in WSCENES else "failure"
            return self._reply(f"wscene2 active {m.group(1)} {verdict}")
        if line.startswith("wscene2 window "):
            return self._reply(f"{line} success")

        m = re.match(r"^mscene get\s*(\S*)$", line)
        if m:
            lines = ["mscene list:"]
            for rx, layouts in MSCENE_LAYOUTS.items():
                if not m.group(1) or self._resolve(m.group(1)) == rx:
                    lines.append(f"{rx} " + " ".join(layouts))
            return self._reply(*lines)
        m = re.match(r"^mscene active (\S+) (\S+)$", line)
        if m:
            known = m.group(2) in MSCENE_LAYOUTS.get(self._resolve(m.group(1)), [])
            return self._reply(f"{line} {'success' if known else 'failure'}")
        if line.startswith(("mscene change ", "mscene set audio ")):
            return self._reply(f"{line} success")
        if line.startswith("mview set") or line == "mview get":
            if line == "mview get":
                return self._reply("mview information:",
                                   "display3 tile source1:0_0_960_540:fit")
            return self._reply(f"{line} success")

        if re.match(r"^config set device (videosource|audiosource|audio2source) \S+ \S+$", line):
            return self._reply(line)
        if line.startswith("config set device audio input type "):
            return self._reply(line)
        if line.startswith("config set device info dante.audio_input="):
            return self._reply(line)
        if line.startswith("config set device info km_over_ip_enable="):
            return self._reply(line)
        if re.match(r"^config set device sinkpower (on|off) \S+", line):
            return self._reply(line)
        if line.startswith("config set device cec notify "):
            return self._reply(line)
        if re.match(r"^config set device cec (onetouchplay|standby) \S+", line):
            return self._reply(line)
        if line.startswith("config set device audio volume "):
            return self._reply(line)
        if line.startswith("config set device osd "):
            return self._reply(f"receive: {line}")
        if re.match(r'^cec ".*" \S+$', line):
            return self._reply(line)
        if re.match(r'^infrared ".*" \S+$', line):
            return self._reply(line)
        if re.match(r'^serial -b .+ ".*" \S+$', line):
            return self._reply(line)

        m = re.match(r"^config set device reboot (.+)$", line)
        if m:
            return self._reply("the following device will reboot now:", *m.group(1).split())
        m = re.match(r"^config set device restorefactory (.+)$", line)
        if m:
            return self._reply("the following device will restore now:", *m.group(1).split())
        if line == "config set reboot":
            return self._reply("system will reboot now")

        return self._reply("unknown command")

    def _reply(self, *lines: str) -> bytes:
        return ("\r\n".join(lines) + "\r\n").encode()

    # ── Matrix ──

    def _matrix_set(self, stream: str, args: str, line: str) -> bytes:
        parts = args.split()
        if stream in ("infrared2", "serial2"):
            return self._reply(line)  # mode assignment — mirror only
        if stream == "audio3":
            return self._reply(line)  # RX TX order — mirror only
        if len(parts) < 2:
            return self._reply("unknown command")
        tx, rxs = parts[0], parts[1:]
        if tx.lower() != "null" and not self._is_endpoint(tx):
            return self._reply("unknown command")
        value = "" if tx.lower() == "null" else self._resolve(tx)
        prefixes = {"": ["matrix_", "matrix_video_", "matrix_audio_", "matrix_usb_"],
                    "video": ["matrix_video_"],
                    "audio": ["matrix_audio_"],
                    "usb": ["matrix_usb_"]}.get(stream)
        if prefixes:
            for rx in rxs:
                alias = self._resolve(rx)
                if alias and ROSTER.get(alias, ("", 0, ""))[2] == "rx":
                    for prefix in prefixes:
                        self.set_state(f"{prefix}{alias}", value)
        return self._reply(line)

    def _matrix_get(self, stream: str, rxs: list[str]) -> bytes:
        headers = {"": "matrix information:",
                   "video": "matrix video information:",
                   "audio": "matrix audio information:",
                   "audio2": "matrix audio2 information:",
                   "audio3": "matrix audio3 information:",
                   "usb": "matrix usb information:",
                   "infrared": "matrix infrared information:",
                   "serial": "matrix serial information:",
                   "infrared2": "matrix infrared2 information:",
                   "serial2": "matrix serial2 information:"}
        header = headers[stream]
        if stream in ("audio2", "audio3", "infrared", "serial", "infrared2", "serial2"):
            return self._reply(header)
        prefix = {"": "matrix_", "video": "matrix_video_",
                  "audio": "matrix_audio_", "usb": "matrix_usb_"}[stream]
        targets = [self._resolve(r) for r in rxs] if rxs else [
            a for a, (_h, _s, kind) in ROSTER.items() if kind == "rx"]
        lines = [header]
        for alias in targets:
            if not alias or ROSTER.get(alias, ("", 0, ""))[2] != "rx":
                continue
            tx = self.state.get(f"{prefix}{alias}") or "NULL"
            lines.append(f"{tx} {alias}")
        return self._reply(*lines)

    # ── JSON documents ──

    def _device_json_string(self) -> bytes:
        entries = []
        # The doc notes RX devices are returned before TX devices.
        for kind in ("rx", "tx"):
            seq = 1
            for alias, (hostname, _series, k) in ROSTER.items():
                if k != kind:
                    continue
                entry = {
                    "aliasName": alias.upper(),
                    "deviceType": "Transmitter" if kind == "tx" else "Receiver",
                    "group": [{"name": "ungrouped", "sequence": 1}],
                    "ip": f"169.254.10.{seq}",
                    "online": bool(self.state.get(f"online_{alias}")),
                    "sequence": seq,
                    "trueName": hostname,
                }
                if kind == "rx":
                    assigned = self.state.get(f"matrix_{alias}")
                    if assigned:
                        entry["txName"] = assigned
                entries.append(entry)
                seq += 1
        return ("device json string:" + _pretty(entries) + "\r\n").encode()

    def _selected(self, names: list[str]) -> list[str]:
        if not names:
            return list(ROSTER)
        return [a for a in (self._resolve(n) for n in names) if a]

    def _device_info(self, names: list[str]) -> bytes:
        devices = []
        for alias in self._selected(names):
            hostname, series, kind = ROSTER[alias]
            entry = {
                "aliasname": alias.upper(),
                "gateway": "0.0.0.0",
                "ip4addr": f"169.254.20.{list(ROSTER).index(alias) + 1}",
                "ip_mode": "dhcp",
                "mac": "d8:80:39:00:00:0%d" % (list(ROSTER).index(alias) + 1),
                "name": hostname,
                "netmask": "255.255.0.0",
                "version": {600: "3.6.0.0", 400: "v0.10.1", 200: "v2.12.2",
                            100: "v1.0.6"}[series],
            }
            if series == 600:
                entry["serial_param"] = "57600-8n1"
                entry["temperature"] = 42
                if kind == "tx":
                    entry.update({
                        "analog_audio_direction": "INPUT",
                        "hdcp14_enable": True,
                        "hdcp22_enable": True,
                        "video_input": bool(self.state.get(f"signal_{alias}")),
                        "video_source": "hdmi",
                        "video_timing": "1920x1080P@60",
                    })
                else:
                    entry.update({
                        "analog_audio_source": "analog",
                        "hdmi_audio_source": "hdmi",
                        "video_mode": "fast_switch",
                        "video_stretch_type": "none",
                        "video_timing": "1080P@50",
                    })
            elif series == 400:
                entry["edid"] = "null"
                entry["km_over_ip_enable"] = "true"
                entry["videodetection"] = "lost"
                if kind == "tx":
                    entry["audio_input_type"] = "auto"
            else:
                entry["edid"] = "null"
                entry["sourcein"] = self.state.get(f"matrix_{alias}") or "unknown"
                if kind == "rx":
                    entry["audio"] = [{"mute": False, "name": "lineout1"}]
            devices.append(entry)
        return ("devices json info:\r\n" + _pretty({"devices": devices}) + "\r\n").encode()

    def _device_status(self, names: list[str]) -> bytes:
        devices = []
        for alias in self._selected(names):
            hostname, series, kind = ROSTER[alias]
            entry: dict = {"aliasname": alias.upper(), "name": hostname}
            signal = bool(self.state.get(f"signal_{alias}"))
            if kind == "tx":
                entry.update({
                    "hdmi in active": "true" if signal else "false",
                    "hdmi in frame rate": "60",
                    "resolution": "1920x1080",
                })
                if series == 400:
                    entry.update({"audio bitrate": "3072000",
                                  "audio input format": "lpcm", "hdcp": "hdcp22"})
                elif series == 600:
                    entry["hdcp"] = "hdcp14"
                else:
                    entry.update({
                        "audio stream ip address": "224.48.46.225",
                        "encoding enable": "true",
                        "line out audio enable": "false",
                        "stream frame rate": "60",
                        "stream resolution": "1920x1080",
                        "video stream ip address": "224.16.46.225",
                    })
            else:
                active = bool(self.state.get(f"matrix_{alias}"))
                entry.update({
                    "hdmi out active": "true" if active else "false",
                    "hdmi out frame rate": "60",
                    "hdmi out resolution": "1920x1080",
                })
                if series == 400:
                    entry.update({"audio bitrate": "3072000",
                                  "audio output format": "lpcm", "hdcp": "hdcp22"})
                elif series == 600:
                    entry["hdcp"] = "hdcp14"
                else:
                    entry.update({
                        "audio bitrate": "1536000",
                        "audio input format": "lpcm",
                        "hdcp status": "hdcp14",
                        "hdmi out audio enable": "true",
                        "line out audio enable": "true",
                        "stream error count": "0",
                        "stream frame rate": "60",
                        "stream resolution": "1920x1080",
                    })
            devices.append(entry)
        return ("devices status info:\r\n"
                + _pretty({"devices status": devices}) + "\r\n").encode()

    # ── Endpoint name handling ──

    def _resolve(self, name: str) -> str:
        """Alias (any case) or hostname -> roster alias; '' if unknown."""
        low = name.strip().lower()
        for alias, (hostname, _s, _k) in ROSTER.items():
            if low == alias.lower() or low == hostname.lower():
                return alias
        return ""

    def _is_endpoint(self, name: str) -> bool:
        return bool(self._resolve(name))

    # ── Notification emitters (for the real-platform smoke / Sim UI) ──

    async def emit_endpoint_online(self, alias: str, online: bool) -> None:
        self.set_state(f"online_{alias}", online)
        await self.push(build_notify_endpoint(alias, online))

    async def emit_video(self, event: str, name: str, tx: str | None = None) -> None:
        await self.push(build_notify_video(event, name, tx))

    async def emit_serial(self, name: str, fmt: str, data: str) -> None:
        await self.push(build_notify_serial(name, fmt, data))
