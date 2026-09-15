"""
Simulator for the Vaddio ConferenceSHOT AV.

Modelled on a capture from a real ConferenceSHOT AV on firmware 1.7.2, not
on the manual, and deliberately reproduces the four things that make the
protocol awkward rather than an idealised version of it:

1. **Every reply ends with the `> ` prompt, not with the line delimiter.**
   That is what the driver frames on, so a simulator that ended replies at a
   newline would let a broken frame parser pass.
2. **Replies do not say what they answer.** All seven audio channels reply
   `volume: <n> dB` and `mute: <on|off>`, `video mute get` replies `mute:
   <on|off>` too, and the three single-axis position queries each reply with
   a bare number. A driver that routes by reply content instead of by
   request will read one channel's level onto another here, exactly as it
   would on the bench.
3. **Two failure shapes, neither of them `OK`.** An unknown or out-of-range
   command answers `Syntax error: Unknown or incomplete command` with NO
   `ERROR` token; a command the camera understood but cannot carry out
   answers a human sentence then `ERROR`. A driver that reports either as
   success is what this simulator exists to catch.
4. **Two commands answer with neither terminator.** `camera sensor get` and
   `system serial-number` reply with a bare value and then the prompt.
5. **Standby locks the audio mixer.** In standby the camera forces master
   mute on, refuses to clear it with a bare `ERROR` carrying no sentence at
   all, and refuses each microphone channel's mute with "Cannot modify while
   master mute is enabled." Waking it clears the lot. This is undocumented
   and is the single most likely reason a mute button appears dead in a real
   room, so it is modelled here rather than left to be rediscovered.
6. **Master mute is an overlay, not a state.** It covers the two EasyMics and
   the USB record stream; a channel's own mute survives underneath it.

It also keeps the commands the RoboSHOT family has and this camera does NOT
(`camera ccu scene ...`, `camera preset store N tri-sync N`) firmly absent,
so a driver that still sends them fails here instead of silently doing
nothing on real hardware.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

# Matches the hardware: -42.0 .. 6.0 dB in 1 dB steps on every channel.
VOLUME_MIN = -42.0
VOLUME_MAX = 6.0

# The camera's own message when an absolute zoom is out of range. 12x is the
# Super Wide ceiling this unit reports.
ZOOM_MIN = 1.0
ZOOM_MAX = 12.0

PAN_MIN, PAN_MAX = -160.0, 160.0
TILT_MIN, TILT_MAX = -30.0, 90.0

AUDIO_CHANNELS = (
    "master", "easy_mic_1", "easy_mic_2", "usb_playback",
    "line_out_1", "usb_record", "ip_stream",
)

# Master mute is an overlay on the near-end path only. Measured on firmware
# 1.7.2 from an all-unmuted baseline: `audio master mute on` makes exactly
# these three report muted and leaves usb_playback, line_out_1 and ip_stream
# untouched; turning it off restores each channel to its OWN setting, so a
# channel muted individually stays muted across a master on/off cycle.
MASTER_MUTE_COVERS = frozenset({"easy_mic_1", "easy_mic_2", "usb_record"})

# Channels the camera still lets you mute while it is asleep. Everything else
# on the near-end path is locked behind the standby-forced master mute, and
# line_out_1 is refused outright because the speaker output is down.
STANDBY_SETTABLE_MUTES = frozenset({"usb_playback"})

CCU_BOOLS = (
    "auto_iris", "auto_white_balance",
    "backlight_compensation", "wide_dynamic_range",
)
CCU_INTS = {
    "iris": (0, 11),
    "gain": (0, 11),
    "detail": (0, 15),
    "chroma": (0, 14),
    "gamma": (-16, 64),
    "red_gain": (0, 255),
    "blue_gain": (0, 255),
}

_SYNTAX = "Syntax error: Unknown or incomplete command"


def _fmt(value: float) -> str:
    """Render a number the way the shell does: 11, not 11.0; 103.47 kept."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


class VaddioConferenceShotAVSimulator(TCPSimulator):
    """Vaddio's interactive shell, prompt and all."""

    SIMULATOR_INFO: dict[str, Any] = {
        "driver_id": "vaddio_conferenceshot_av",
        "name": "Vaddio ConferenceSHOT AV Simulator",
        "delimiter": "\r\n",
        # Wire values, i.e. what the shell prints — not the names the driver
        # publishes after reading them.
        "initial_state": {
            "system_version": "ConferenceSHOT AV 1.7.2",
            "audio_version": "1.04",
            "usb_version": "01.02.012",
            "sensor_version": "06.00",
            "commit": "efff3b5223014dd2fc9e1dec811e7426fc948600",
            "sensor": "So7100",
            "serial_number": "",
            "standby": "off",
            "video_mute": "off",
            "auto_focus": "on",
            "ir_correction": "standard",
            "led": "on",
            "icr": "off",
            "pan": 0.0,
            "tilt": 0.0,
            "zoom": 1.0,
            "auto_iris": "on",
            "auto_white_balance": "on",
            "backlight_compensation": "off",
            "wide_dynamic_range": "off",
            "iris": 11,
            "gain": 1,
            "detail": 8,
            "chroma": 5,
            "gamma": -4,
            "red_gain": 0,
            "blue_gain": 0,
            "ip_enabled": "false",
            "ip_protocol": "RTSP",
            "ip_port": 554,
            "ip_url": "vaddio-conferenceshot-av-stream",
            "ip_preset_resolution": "720p",
            "ip_preset_quality": "Standard (Better)",
            "usb_active": "false",
            "usb_device": "ConferenceSHOT AV",
            "usb_resolution": "0x0",
            "usb_frame_rate": 0,
            "uvc_extensions": "true",
            "temperature_c": 56.84,
            "temperature_fault": "false",
            "factory_reset_sw": "off",
            "mac_address": "68:27:19:AA:BB:CC",
            "ip_address": "192.168.1.136",
            "gateway": "192.168.1.1",
            "netmask": "255.255.255.0",
            "hostname": "vaddio-conferenceshot-av-68-27-19-aa-bb-cc",
            "calibrating": "false",
        },
        "delays": {"command_response": 0.01},
        "controls": [
            {"type": "indicator", "key": "system_version", "label": "Model"},
            {"type": "toggle", "key": "standby", "label": "Standby"},
            {"type": "toggle", "key": "video_mute", "label": "Video Mute"},
            {"type": "toggle", "key": "auto_focus", "label": "Auto Focus"},
            {"type": "toggle", "key": "led", "label": "Indicator LED"},
            {"type": "toggle", "key": "icr", "label": "IR Cut Filter"},
            {"type": "toggle", "key": "ip_enabled", "label": "IP Streaming"},
            {"type": "indicator", "key": "pan", "label": "Pan"},
            {"type": "indicator", "key": "tilt", "label": "Tilt"},
            {"type": "indicator", "key": "zoom", "label": "Zoom"},
            {"type": "indicator", "key": "iris", "label": "Iris"},
            {"type": "indicator", "key": "gain", "label": "Gain"},
            {"type": "indicator", "key": "gamma", "label": "Gamma"},
            {"type": "indicator", "key": "temperature_c", "label": "Temperature"},
        ],
        "errors": {
            "no_response": {
                "label": "Stops answering",
                "description": (
                    "The shell accepts the connection and never replies — "
                    "what a wedged camera looks like to the liveness probe."
                ),
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Per-channel audio, seeded with the levels a factory unit reports.
        self._volumes: dict[str, float] = {
            "master": -9.0, "easy_mic_1": 6.0, "easy_mic_2": 6.0,
            "usb_playback": 0.0, "line_out_1": -9.0,
            "usb_record": -2.7890625, "ip_stream": 0.0,
        }
        self._mutes: dict[str, bool] = {c: False for c in AUDIO_CHANNELS}
        self._mutes["ip_stream"] = True

    # ── Telnet login ──────────────────────────────────────────────────────

    async def on_client_connected(self, client_id: str) -> bytes | None:
        # The real unit opens with IAC DO ECHO / DO NAWS / WILL ECHO /
        # WILL SGA before a byte of text. A driver that does not strip IAC
        # sees them glued to the login prompt, so they belong here.
        return b"\xff\xfd\x01\xff\xfd\x1f\xff\xfb\x01\xff\xfb\x03"

    async def authenticate_client(self, reader, writer, client_id) -> bool:
        host = self.get_state("hostname", "vaddio-conferenceshot-av")
        try:
            writer.write(
                b"\r\n\rLegrand|AV https://www.legrandav.com/vaddio\r\n"
                b"\x1b[1;31mWarning: This is an insecure connection\x1b[0m\r\n"
                + f"\r\n{host} login: ".encode()
            )
            await writer.drain()
            user = (await asyncio.wait_for(reader.readline(), timeout=10)).decode(
                "ascii", errors="replace").strip()
            writer.write(b"\r\nPassword: ")
            await writer.drain()
            password = (await asyncio.wait_for(reader.readline(), timeout=10)).decode(
                "ascii", errors="replace").strip()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return False

        if user == "invalid" or password == "invalid" or not user or not password:
            try:
                writer.write(f"\r\n{host} login: ".encode())
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            return False

        writer.write(
            b"\r\n\r\n********************************************\r\n"
            b"*         Vaddio Interactive Shell         *\r\n"
            b"********************************************\r\n"
            + f"Welcome {user}\r\n> ".encode()
        )
        await writer.drain()
        return True

    # ── Audio model ───────────────────────────────────────────────────────

    def _master_active(self) -> bool:
        """Master mute as the camera applies it — standby forces it on."""
        return self._mutes["master"] or self.get_state("standby") == "on"

    def _reported_mute(self, channel: str) -> str:
        """What `audio <ch> mute get` answers: the channel's own mute, or the
        master overlay where the overlay reaches."""
        if channel == "master":
            return "on" if self._master_active() else "off"
        muted = self._mutes[channel] or (
            self._master_active() and channel in MASTER_MUTE_COVERS
        )
        return "on" if muted else "off"

    # ── Reply helpers ─────────────────────────────────────────────────────

    def _reply(self, echo: str, body: list[str] | None, tail: str | None) -> bytes:
        """Assemble one reply exactly as the shell sends it.

        echo, then the body lines, then `OK` / `ERROR` / nothing, then the
        prompt with no trailing newline.
        """
        parts = [echo]
        parts.extend(body or [])
        if tail:
            parts.append(tail)
        return ("\r\n".join(parts) + "\r\n> ").encode()

    def _ok(self, echo: str, body: list[str] | None = None) -> bytes:
        return self._reply(echo, body, "OK")

    def _error(self, echo: str, why: str) -> bytes:
        """A command the camera understood but cannot do: sentence, then ERROR."""
        return self._reply(echo, [why], "ERROR")

    def _syntax(self, echo: str) -> bytes:
        """An unknown command or an out-of-range value: no ERROR token at all."""
        return self._reply(echo, [_SYNTAX, ""], None)

    def _bare(self, echo: str, value: str) -> bytes:
        """A reply with neither OK nor ERROR."""
        return self._reply(echo, [value], None)

    # ── Dispatch ──────────────────────────────────────────────────────────

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", "replace").strip()
        if not line:
            return b"> "
        echo = line

        for handler in (
            self._audio, self._camera, self._video, self._streaming,
            self._system, self._misc,
        ):
            out = handler(line, echo)
            if out is not None:
                return out
        return self._syntax(echo)

    # -- audio -------------------------------------------------------------

    def _audio(self, line: str, echo: str) -> bytes | None:
        m = re.match(r"^audio\s+(\w+)\s+(volume|mute)\s+(\S+)(?:\s+(\S+))?$", line)
        if not m:
            return None
        channel, kind, action, arg = m.group(1), m.group(2), m.group(3), m.group(4)
        if channel not in AUDIO_CHANNELS:
            return self._syntax(echo)

        if kind == "mute":
            if action == "get":
                # No channel name in the reply. This is the whole point.
                return self._ok(echo, [f"mute:   {self._reported_mute(channel)}"])

            if action not in ("on", "off", "toggle"):
                return self._syntax(echo)

            asleep = self.get_state("standby") == "on"

            if channel == "master":
                if asleep:
                    # Bare ERROR, no sentence — exactly what the camera sends.
                    return self._reply(echo, [], "ERROR")
                self._mutes["master"] = (
                    not self._mutes["master"] if action == "toggle"
                    else action == "on"
                )
                return self._ok(echo)

            if self._master_active() and channel in MASTER_MUTE_COVERS:
                return self._error(
                    echo, "Cannot modify while master mute is enabled."
                )
            if asleep and channel not in STANDBY_SETTABLE_MUTES:
                return self._reply(echo, [], "ERROR")

            self._mutes[channel] = (
                not self._mutes[channel] if action == "toggle"
                else action == "on"
            )
            return self._ok(echo)

        # volume
        if action == "get":
            # The camera prints the level at its own precision, not rounded:
            # a channel left where the DSP put it reads
            # `volume: -2.7890625 dB`, while a channel set from the API reads
            # `volume: 6.0 dB`. A driver that assumes one decimal is wrong.
            level = self._volumes[channel]
            text = f"{level:.1f}" if round(level, 1) == level else repr(level)
            return self._ok(echo, [f"volume: {text} dB"])
        if action in ("up", "down"):
            step = 1.0 if action == "up" else -1.0
            new = max(VOLUME_MIN, min(VOLUME_MAX, self._volumes[channel] + step))
            self._volumes[channel] = new
            return self._ok(echo)
        if action == "set":
            if arg is None:
                return self._syntax(echo)
            try:
                value = float(arg)
            except ValueError:
                return self._syntax(echo)
            if value < VOLUME_MIN or value > VOLUME_MAX:
                # Measured: the shell answers a bare ERROR for -43 and 7.
                return self._reply(echo, [], "ERROR")
            self._volumes[channel] = value
            return self._ok(echo)
        return self._syntax(echo)

    # -- camera ------------------------------------------------------------

    def _camera(self, line: str, echo: str) -> bytes | None:
        if not line.startswith("camera "):
            return None

        # Drive
        m = re.match(r"^camera (pan|tilt|zoom|focus) (left|right|up|down|in|out|near|far)(?:\s+(\d+))?$", line)
        if m:
            axis = m.group(1)
            if axis == "focus" and self.get_state("auto_focus") == "on":
                return self._error(
                    echo,
                    "cannot perform manual focus operation while auto focus enabled",
                )
            if axis in ("tilt", "zoom") and self.get_state("calibrating") == "true":
                return self._error(echo, "camera calibrating")
            return self._ok(echo)

        m = re.match(r"^camera (pan|tilt|zoom|focus) stop$", line)
        if m:
            if m.group(1) == "focus" and self.get_state("auto_focus") == "on":
                return self._error(
                    echo,
                    "cannot perform manual focus operation while auto focus enabled",
                )
            return self._ok(echo)

        # Single-axis position reads: a BARE number, no label.
        m = re.match(r"^camera (pan|tilt|zoom) get$", line)
        if m:
            return self._ok(echo, [_fmt(float(self.get_state(m.group(1))))])

        m = re.match(r"^camera (pan|tilt) set (-?[\d.]+)(?:\s+(\d+))?(?:\s+(no_wait))?$", line)
        if m:
            axis, value = m.group(1), float(m.group(2))
            lo, hi = (PAN_MIN, PAN_MAX) if axis == "pan" else (TILT_MIN, TILT_MAX)
            if value < lo or value > hi:
                return self._error(
                    echo, f"{axis} position {value} out of bounds ({lo}..{hi})"
                )
            self.set_state(axis, value)
            return self._ok(echo)

        m = re.match(r"^camera zoom set (-?[\d.]+)(?:\s+(\d+))?(?:\s+(no_wait))?$", line)
        if m:
            value = float(m.group(1))
            if value < ZOOM_MIN or value > ZOOM_MAX:
                # The exact sentence firmware 1.7.2 returns.
                return self._error(
                    echo,
                    f"zoom position {value} out of bounds ({ZOOM_MIN}..{ZOOM_MAX})",
                )
            self.set_state("zoom", value)
            return self._ok(echo)

        if line == "camera ptz-position get":
            return self._ok(echo, [
                f"pan: {_fmt(float(self.get_state('pan')))}",
                f"tilt: {_fmt(float(self.get_state('tilt')))}",
                f"zoom: {_fmt(float(self.get_state('zoom')))}",
            ])

        m = re.match(
            r"^camera ptz-position set(?:\s+pan\s+(-?[\d.]+))?"
            r"(?:\s+tilt\s+(-?[\d.]+))?(?:\s+zoom\s+(-?[\d.]+))?"
            r"(?:\s+(no_wait))?$",
            line,
        )
        if m:
            for axis, raw, lo, hi in (
                ("pan", m.group(1), PAN_MIN, PAN_MAX),
                ("tilt", m.group(2), TILT_MIN, TILT_MAX),
                ("zoom", m.group(3), ZOOM_MIN, ZOOM_MAX),
            ):
                if raw is None:
                    continue
                value = float(raw)
                if value < lo or value > hi:
                    return self._error(
                        echo, f"{axis} position {value} out of bounds ({lo}..{hi})"
                    )
                self.set_state(axis, value)
            return self._ok(echo)

        if line == "camera home":
            # Home is a position stored ON the camera, not the origin, and
            # the real unit acknowledges at once and keeps moving for several
            # seconds. Both are modelled: a driver that reads the position
            # straight back, or that assumes home means 0/0/1, is wrong here
            # for the same reason it is wrong on the bench. Measured home on
            # the reference unit was pan 103.47, tilt 40.26, zoom 1.
            self.set_state("pan", 103.47)
            self.set_state("tilt", 40.26)
            self.set_state("zoom", 1.0)
            return self._ok(echo)

        if line == "camera recalibrate":
            return self._ok(echo)

        # Focus mode
        m = re.match(r"^camera focus mode (get|auto|manual)$", line)
        if m:
            if m.group(1) == "get":
                return self._ok(echo, [f"auto_focus:     {self.get_state('auto_focus')}"])
            self.set_state("auto_focus", "on" if m.group(1) == "auto" else "off")
            return self._ok(echo)

        m = re.match(r"^camera focus ir-correction (get|standard|ir-light)$", line)
        if m:
            if m.group(1) == "get":
                return self._ok(
                    echo, [f"IR Correction:  {self.get_state('ir_correction')}"]
                )
            self.set_state("ir_correction", m.group(1))
            return self._ok(echo)

        # Presets. No tri-sync and no `camera ccu scene` on this model — both
        # fall through to the syntax error, which is what the hardware does.
        m = re.match(r"^camera preset (recall|store) (\d+)(\s+save-ccu)?$", line)
        if m:
            index = int(m.group(2))
            if not 1 <= index <= 16:
                return self._syntax(echo)
            return self._ok(echo)

        # CCU
        if line in ("camera ccu get all", "camera ccu get"):
            rows = []
            for key in sorted(CCU_BOOLS + tuple(CCU_INTS)):
                rows.append(f"{key:<24}{self.get_state(key)}")
            return self._ok(echo, rows)

        m = re.match(r"^camera ccu get (\w+)$", line)
        if m:
            key = m.group(1)
            if key in CCU_BOOLS or key in CCU_INTS:
                return self._ok(echo, [f"{key:<24}{self.get_state(key)}"])
            return self._syntax(echo)

        m = re.match(r"^camera ccu set (\w+) (-?\w+)$", line)
        if m:
            key, raw = m.group(1), m.group(2)
            if key in CCU_BOOLS:
                if raw not in ("on", "off"):
                    return self._syntax(echo)
                self.set_state(key, raw)
                return self._ok(echo)
            if key in CCU_INTS:
                try:
                    value = int(raw)
                except ValueError:
                    return self._syntax(echo)
                lo, hi = CCU_INTS[key]
                if not lo <= value <= hi:
                    # Out of range is a SYNTAX error here: the shell's
                    # grammar carries the bounds. Measured with `iris 99`.
                    return self._syntax(echo)
                self.set_state(key, value)
                return self._ok(echo)
            return self._syntax(echo)

        # LED — note the capital in the reply; the manual prints `led:`.
        m = re.match(r"^camera led (get|on|off)$", line)
        if m:
            if m.group(1) == "get":
                return self._ok(echo, [f"LED:    {self.get_state('led')}"])
            self.set_state("led", m.group(1))
            return self._ok(echo)

        # IR cut filter — leading spaces and the bracketed mechanical position.
        m = re.match(r"^camera icr (get|on|off)$", line)
        if m:
            if m.group(1) == "get":
                word = self.get_state("icr")
                where = "Out" if word == "on" else "In"
                return self._ok(echo, [f"  IR(Cut) filter {word}({where})"])
            self.set_state("icr", m.group(1))
            return self._ok(echo)

        # Standby
        m = re.match(r"^camera standby (get|on|off|toggle)$", line)
        if m:
            action = m.group(1)
            if action == "get":
                return self._ok(echo, [f"standby:        {self.get_state('standby')}"])
            if action == "toggle":
                self.set_state(
                    "standby", "off" if self.get_state("standby") == "on" else "on"
                )
            else:
                self.set_state("standby", action)
            return self._ok(echo)

        if line == "camera sensor get":
            # No OK, no ERROR — a bare quoted value then the prompt.
            return self._bare(echo, f'"{self.get_state("sensor")}"')

        return self._syntax(echo)

    # -- video -------------------------------------------------------------

    def _video(self, line: str, echo: str) -> bytes | None:
        m = re.match(r"^video mute (get|on|off|toggle)$", line)
        if not m:
            return None
        action = m.group(1)
        if action == "get":
            # Byte-identical in shape to an audio channel's mute reply.
            return self._ok(echo, [f"mute:   {self.get_state('video_mute')}"])
        if action == "toggle":
            self.set_state(
                "video_mute", "off" if self.get_state("video_mute") == "on" else "on"
            )
        else:
            self.set_state("video_mute", action)
        return self._ok(echo)

    # -- streaming ---------------------------------------------------------

    def _streaming(self, line: str, echo: str) -> bytes | None:
        if line == "streaming settings get":
            return self._ok(echo, [
                "IP Custom_Frame_Rate    15",
                "IP Custom_Resolution    1080p",
                f"IP Enabled              {self.get_state('ip_enabled')}",
                "IP MTU                  1400",
                f"IP Port                 {self.get_state('ip_port')}",
                f"IP Preset_Quality       {self.get_state('ip_preset_quality')}",
                f"IP Preset_Resolution    {self.get_state('ip_preset_resolution')}",
                f"IP Protocol             {self.get_state('ip_protocol')}",
                f"IP URL                  {self.get_state('ip_url')}",
                "IP Video_Mode           preset",
                f"USB Active              {self.get_state('usb_active')}",
                f"USB Device              {self.get_state('usb_device')}",
                f"USB Frame_Rate          {self.get_state('usb_frame_rate')}",
                f"USB Resolution          {self.get_state('usb_resolution')}",
                "USB Version             0",
                f"UVC Extensions_Enabled  {self.get_state('uvc_extensions')}",
            ])

        m = re.match(r"^streaming ip enable (get|on|off|toggle)$", line)
        if m:
            action = m.group(1)
            if action == "get":
                return self._ok(echo, [f"enabled: {self.get_state('ip_enabled')}"])
            if action == "toggle":
                self.set_state(
                    "ip_enabled",
                    "false" if self.get_state("ip_enabled") == "true" else "true",
                )
            else:
                self.set_state("ip_enabled", "true" if action == "on" else "false")
            return self._ok(echo)
        return None

    # -- system ------------------------------------------------------------

    def _system(self, line: str, echo: str) -> bytes | None:
        if line == "version":
            return self._ok(echo, [
                f"Audio           {self.get_state('audio_version')}",
                f"Commit          {self.get_state('commit')}",
                f"Sensor Version  {self.get_state('sensor_version')}",
                f"System Version  {self.get_state('system_version')}",
                f"USB             {self.get_state('usb_version')}",
            ])

        if line == "network settings get":
            return self._ok(echo, [
                "Name            eth0:WAN",
                f"MAC Address     {self.get_state('mac_address')}",
                f"IP Address      {self.get_state('ip_address')}",
                f"Netmask         {self.get_state('netmask')}",
                "VLAN            Disabled",
                f"Gateway         {self.get_state('gateway')}",
                f"Hostname        {self.get_state('hostname')}",
            ])

        if line == "temperature get":
            return self._ok(echo, [
                f"zynq_c  {self.get_state('temperature_c')} C  "
                f"fault?  {self.get_state('temperature_fault')}  6 seconds ago"
            ])

        m = re.match(r"^system factory-reset (get|on|off)$", line)
        if m:
            if m.group(1) in ("on", "off"):
                self.set_state("factory_reset_sw", m.group(1))
            return self._ok(echo, [
                f"factory-reset (software):       {self.get_state('factory_reset_sw')}",
                "factory-reset (hardware):       off",
            ])

        if line == "system serial-number":
            serial = self.get_state("serial_number")
            # No OK either way — a factory unit says so in a sentence.
            return self._bare(echo, serial or "Serial number not set")

        m = re.match(r"^system reboot(?:\s+(\d+))?$", line)
        if m:
            return self._ok(echo)
        return None

    # -- misc --------------------------------------------------------------

    def _misc(self, line: str, echo: str) -> bytes | None:
        m = re.match(r"^trigger (\d+) (on|off)$", line)
        if m:
            index = int(m.group(1))
            if index > 50:
                return self._syntax(echo)
            if index > 10:
                # Measured: index 50 parses but no such trigger is defined.
                return self._error(echo, "Unknown error occurred")
            return self._ok(echo)

        m = re.match(r"^sleep (\d+)$", line)
        if m:
            return self._ok(echo)
        return None
