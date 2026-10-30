"""
Marshall PTZ Camera (Marshall CGI) — Simulator.

Simulates a Marshall CV620-BI2/WI2's HTTP CGI as Marshall's CGI Command V3
workbook describes it and as a CV620-WI2 (firmware VUG115_VUH109) answered it:

* ``/command/inquiry.cgi?inqjs=<group>`` answers ``var Name="value"`` lines
  for system, camera, imaging, ptzf, tally, presetposition, network, srt, rtmp,
  mpeg2ts, freedconfig and user (the values the bench unit reported, seeded
  below). A group the camera lacks answers 400 ``{"STATUS":"NG"}``.
* ``/command/<cgi>.cgi?<Name>=<value>`` answers ``{"STATUS":"OK"}``, 400
  ``{"STATUS":"NG"}`` for a parameter or value refused, and 403 for an unknown
  CGI.

What the bench unit did and the workbook does not say is modelled on purpose,
so the driver's handling of it can be seen: a set the camera acknowledges and
does not apply (shutter, iris and gain outside their exposure modes, red/blue
gain outside manual white balance, the limit angles while the limits are off,
an unknown tally word), a shutter or iris write landing in the read-only
priority field in that priority mode, PTZ Assist refused with HTTP 500 outside
manual focus, ImageMode taking only ``standard``, a camera name cut to 16
characters, SRT refused in caller mode with no destination and RTMP with no
server or stream key, and pan/tilt moves ignored in standby. The AbsolutePTZF
string carries pan and tilt as unpadded 32-bit hex (``ffffc653`` is -14765).

Sign-in: HTTP Digest, realm "Secure", MD5, qop auth, user ``admin`` (or the
configured ``username``). With a ``password`` configured only that password is
accepted; with none, any password but ``invalid`` is.

Motion follows what a CV620-WI2 measured on 2026-10-30, computed from when a
move started so positions advance between reads with no background task: pan,
tilt and zoom speed per speed step (far from linear: pan runs from about 700
counts/s at speed 0 to 110,000 at 23), the travel stops (pan +/-314,320 counts
for +/-170 degrees, tilt -42,668 to +128,000 for -30 to +90), the limit angles
while Pan/Tilt Limits On is on, and when the camera answers: an exact zoom once
it has arrived (7.3 s wide to full optical, 8.3 s to x12 digital), Home after the trip back (up to
about 1.2 s), a preset recall after about 0.75 s plus the trip at Preset Speed.
Power changes ``standby_delay`` seconds after Standby (default 13) and
``wake_delay`` seconds after Power On (default 16), as measured; both can be
set lower to test quickly. A reboot answers 503 for ``reboot_downtime`` seconds
(default 5).

Error modes: ``communication_timeout`` (answers nothing) and
``password_changed`` (every request is refused 401).

Driver: marshall_ptz
Transport: http
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

REALM = "Secure"

# Inquiry group -> the parameters it reports, in the bench unit's order.
GROUPS: dict[str, list[str]] = {
    "system": [
        "Power", "ModelName", "FWVersion", "Serial", "Uptime", "Time", "NtpService",
        "NtpServer", "NtpSynchronized", "TimeZoneIndex", "DstMode", "IRReceive",
        "ControlPort", "Protocol", "BaudRate", "ViscaAddress", "PelcoDAddress",
    ],
    "camera": [
        "ImageCodec1", "ImageSize1", "FrameRate1", "BitRate1", "CBR1", "IFrameRatio1",
        "ImageCodec2", "ImageSize2", "FrameRate2", "BitRate2", "CBR2", "IFrameRatio2",
        "FocusMode", "AFSensitivity", "AFFrame", "ZoomTracking", "PTZAssist", "PresetAF",
        "SmartAF", "PanTiltLimit", "PanRightLimit", "PanLeftLimit", "TiltUpLimit",
        "TiltDownLimit", "PanFlip", "TiltFlip", "PresetSpeed", "PTZSpeedComp",
        "PTZMotionSync", "DZoomLimit", "Mirror", "PictureEffect", "AudioIn",
        "MicLineSelect", "AudioInVolume", "AudioDelay", "AudioDelayTime", "AudInCodec",
        "InitPosition", "Resolution", "OutputSource", "HdmiColor", "OverlayTopLeftMode",
        "OverlayTopLeftText", "OverlayTopRightMode", "OverlayTopRightText", "Multicast",
    ],
    "imaging": [
        "ExposureMode", "ExposureCompensation", "ExposureExposureTimePri", "ExposureIrisPri",
        "ExposureGain", "ExposureExposureTime", "ExposureIris", "ExposureMaxGain",
        "VisibilityEnhancerLevel", "WhiteBalanceMode", "WhiteBalanceCrGain",
        "WhiteBalanceCbGain", "DetailLevel", "NoiseReduction2DLevel",
        "NoiseReduction3DLevel", "ImageMode", "DigitalBrightLevel", "ColorHue",
        "ColorSaturation", "GammaLevel",
    ],
    "ptzf": ["AbsolutePTZF"],
    "tally": ["TallyCMMDMode", "TallyControl", "TallyLevel"],
    "presetposition": ["CallMode"],
    "network": [
        "CameraName", "Location", "Dhcp", "Ip", "Subnetmask", "Gateway", "PrimaryDns",
        "CurrentIp", "CurrentSubnetmask", "CurrentGateway", "CurrentPrimaryDns",
        "MacAddress", "HttpPort", "HttpsPort", "RTSPPort1", "RTSPPort2",
    ],
    "srt": [
        "SRTEnable", "SRTConnectMode", "SRTUrl", "SRTPort", "SRTStreamID", "SRTLatency",
        "SRTEncryption", "SRTPassphraseUsed",
    ],
    "rtmp": [
        "RtmpEnable", "RtmpServerUrl1", "RtmpStreamKey1Used", "AuthenRtmp",
        "AuthenRtmpUserName", "AuthenRtmpPasswordUsed", "RtmpVideoInfo", "RtmpAudioInfo",
        "RtmpVideoResolution", "RtmpChunkSize", "RtmpStatus",
    ],
    "mpeg2ts": ["MPEG2TSEnable", "MPEG2TSIP", "MPEG2TSPort"],
    "freedconfig": ["FreeD", "FreeDOutput", "FreeDDestinationIP", "FreeDUdpPort"],
    "user": ["AuthenRTSP", "AdminInfo"],
}

_ONOFF = ("on", "off")
_INT = re.compile(r"^-?\d+$")
_LIMIT_ANGLES = ("PanLeftLimit", "PanRightLimit", "TiltUpLimit", "TiltDownLimit")


def _range(lo: int, hi: int):
    return lambda v: bool(_INT.match(v)) and lo <= int(v) <= hi


def _one_of(*words: str):
    return lambda v: v in words


# (cgi, parameter) -> what the camera accepts. Read-only parameters are absent.
WRITABLE: dict[tuple[str, str], Any] = {
    ("camera", "FocusMode"): _one_of("auto", "manual"),
    ("camera", "AFSensitivity"): _one_of("low", "middle", "high"),
    ("camera", "AFFrame"): _one_of("center", "full", "auto"),
    ("camera", "PTZAssist"): _one_of(*_ONOFF),
    ("camera", "PresetAF"): _one_of(*_ONOFF),
    ("camera", "SmartAF"): _one_of(*_ONOFF),
    ("camera", "PanTiltLimit"): _one_of(*_ONOFF),
    ("camera", "PanLeftLimit"): _range(-170, 0),
    ("camera", "PanRightLimit"): _range(0, 170),
    ("camera", "TiltUpLimit"): _range(0, 90),
    ("camera", "TiltDownLimit"): _range(-30, 0),
    ("camera", "PanFlip"): _one_of(*_ONOFF),
    ("camera", "TiltFlip"): _one_of(*_ONOFF),
    ("camera", "PresetSpeed"): _range(0, 7),
    ("camera", "PTZSpeedComp"): _one_of(*_ONOFF),
    ("camera", "PTZMotionSync"): _one_of(*_ONOFF),
    ("camera", "DZoomLimit"): _one_of(*(f"x{n}" for n in range(1, 13))),
    ("camera", "Mirror"): _one_of("off", "mirror", "flip", "mirror+flip"),
    ("camera", "PictureEffect"): _one_of("off", "neg", "bw"),
    ("camera", "AudioIn"): _one_of(*_ONOFF),
    ("camera", "MicLineSelect"): _one_of("mic", "line"),
    ("camera", "AudioInVolume"): _range(0, 10),
    ("camera", "InitPosition"): _one_of("lastmem", "1stpreset"),
    ("camera", "OverlayTopLeftText"): lambda v: len(v) <= 30,
    ("camera", "OverlayTopRightText"): lambda v: len(v) <= 30,
    ("imaging", "ExposureMode"): _one_of("auto", "shutter", "iris", "manual"),
    ("imaging", "ExposureCompensation"): _range(0, 10),
    ("imaging", "ExposureExposureTime"): _range(0, 21),
    ("imaging", "ExposureIris"): _range(0, 15),
    ("imaging", "ExposureGain"): _range(1, 16),
    ("imaging", "ExposureMaxGain"): _range(4, 16),
    ("imaging", "VisibilityEnhancerLevel"): _range(0, 3),
    ("imaging", "WhiteBalanceMode"): _one_of("auto", "atw", "indoor", "outdoor", "onepushwb", "sodiumlamp", "manual"),
    ("imaging", "WhiteBalanceCrGain"): _range(0, 128),
    ("imaging", "WhiteBalanceCbGain"): _range(0, 128),
    ("imaging", "WhiteBalanceOnePushTrg"): _one_of("on"),
    ("imaging", "DetailLevel"): _range(0, 14),
    ("imaging", "NoiseReduction2DLevel"): _range(0, 3),
    ("imaging", "NoiseReduction3DLevel"): _range(0, 3),
    # The bench unit reads "standard" and refuses the workbook's default/custom.
    ("imaging", "ImageMode"): _one_of("standard"),
    ("imaging", "DigitalBrightLevel"): _range(0, 15),
    ("imaging", "ColorHue"): _range(0, 15),
    ("imaging", "ColorSaturation"): _range(0, 15),
    ("imaging", "GammaLevel"): _range(0, 3),
    # Any word is acknowledged; only on/off change anything (bench: "maybe" -> OK).
    ("tally", "TallyControl"): lambda v: True,
    ("tally", "TallyLevel"): _one_of("off", "low", "high"),
    ("tally", "TallyCMMDMode"): _one_of("link", "normal"),
    ("presetposition", "CallMode"): _one_of("freeze", "normal"),
    ("system", "IRReceive"): _one_of(*_ONOFF),
    ("network", "CameraName"): lambda v: len(v) >= 1,
    ("srt", "SRTEnable"): _one_of(*_ONOFF),
    ("srt", "SRTConnectMode"): _one_of("caller", "listener"),
    ("srt", "SRTUrl"): lambda v: True,
    ("srt", "SRTPort"): _range(1024, 65534),
    ("srt", "SRTLatency"): _range(20, 8000),
    ("rtmp", "RtmpEnable"): _one_of(*_ONOFF),
    ("rtmp", "RtmpServerUrl1"): lambda v: True,
}

# Counts per second at each measured speed step (CV620-WI2, 0.8 s moves from
# home; speed 10 from six 0.6 s moves); speeds between are interpolated
# geometrically. Focus is not measured.
_RATES = {
    "pan": {0: 702, 3: 1747, 6: 4574, 10: 9200, 12: 18959, 18: 53966, 23: 110312},
    "tilt": {0: 492, 3: 1295, 6: 3490, 10: 6970, 12: 14861, 18: 40274, 23: 81927},
    "zoom": {0: 594, 2: 1798, 4: 2943, 7: 4649},
    "focus": {0: 20, 7: 160},
}
# Travel stops in counts (measured), and counts per degree for the limit angles.
_LIMIT = {"pan": (-314320, 314320), "tilt": (-42668, 128000), "zoom": (0, 0x4000), "focus": (0, 0x0FFF)}
_PAN_PER_DEGREE = 314320 / 170
_TILT_PER_DEGREE = 128000 / 90
# Seconds per zoom count when an exact zoom travels: the wide half of optical
# took 2.9 s, the tele half 4.35 s, the digital range (to x12) 1.0 s.
_ZOOM_S_WIDE = 2.9 / 8192
_ZOOM_S_TELE = 4.35 / 8192
_ZOOM_S_DIGITAL = 1.0 / (0x7AC0 - 0x4000)
# Pan/tilt speed of a preset recall, degrees per second, by Preset Speed (workbook).
_PRESET_DEG_S = {0: 5, 1: 25, 2: 50, 3: 80, 4: 120, 5: 160, 6: 200, 7: 300}


def _rate(axis: str, speed: int) -> float:
    table = _RATES[axis]
    steps = sorted(table)
    speed = max(steps[0], min(steps[-1], speed))
    for lo, hi in zip(steps, steps[1:]):
        if lo <= speed <= hi:
            return table[lo] * (table[hi] / table[lo]) ** ((speed - lo) / (hi - lo))
    return float(table[steps[-1]])


def _zoom_seconds(a: int, b: int) -> float:
    lo, hi = sorted((a, b))
    wide = max(0, min(hi, 8192) - lo)
    tele = max(0, min(hi, 0x4000) - max(lo, 8192))
    digital = max(0, hi - max(lo, 0x4000))
    return wide * _ZOOM_S_WIDE + tele * _ZOOM_S_TELE + digital * _ZOOM_S_DIGITAL


def _hex(value: int) -> str:
    """The camera's position form: unpadded-to-four 32-bit hex."""
    return f"{value & 0xFFFFFFFF:04x}"


class MarshallPTZSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "marshall_ptz",
        "name": "Marshall PTZ Camera Simulator",
        "category": "camera",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            # system
            "Power": "on", "ModelName": "CV620-BI2_WI2", "FWVersion": "VUG115_VUH109",
            "Serial": "VEGB02287", "Uptime": "19 min", "Time": "2201010018586",
            "NtpService": "off", "NtpServer": "pool.ntp.org", "NtpSynchronized": "0",
            "TimeZoneIndex": "20", "DstMode": "off", "IRReceive": "on",
            "ControlPort": "rs-232", "Protocol": "visca", "BaudRate": "9600",
            "ViscaAddress": "0", "PelcoDAddress": "1",
            # camera
            "ImageCodec1": "h264", "ImageSize1": "1920,1080", "FrameRate1": "60",
            "BitRate1": "7000", "CBR1": "on", "IFrameRatio1": "30",
            "ImageCodec2": "h264", "ImageSize2": "1280,720", "FrameRate2": "30",
            "BitRate2": "3000", "CBR2": "on", "IFrameRatio2": "30",
            "FocusMode": "auto", "AFSensitivity": "middle", "AFFrame": "auto",
            "ZoomTracking": "curvetracking", "PTZAssist": "off", "PresetAF": "on",
            "SmartAF": "off", "PanTiltLimit": "off", "PanRightLimit": "170",
            "PanLeftLimit": "-170", "TiltUpLimit": "90", "TiltDownLimit": "-30",
            "PanFlip": "off", "TiltFlip": "off", "PresetSpeed": "5", "PTZSpeedComp": "off",
            "PTZMotionSync": "off", "DZoomLimit": "x1", "Mirror": "off",
            "PictureEffect": "off", "AudioIn": "off", "MicLineSelect": "line",
            "AudioInVolume": "6", "AudioDelay": "off", "AudioDelayTime": "1",
            "AudInCodec": "aac_48k", "InitPosition": "lastmem", "Resolution": "1080p_60",
            "OutputSource": "hdmi+stream", "HdmiColor": "yuv422", "OverlayTopLeftMode": "off",
            "OverlayTopLeftText": "", "OverlayTopRightMode": "off", "OverlayTopRightText": "",
            "Multicast": "off",
            # imaging
            "ExposureMode": "auto", "ExposureCompensation": "5", "ExposureExposureTimePri": "6",
            "ExposureIrisPri": "11", "ExposureGain": "1", "ExposureExposureTime": "6",
            "ExposureIris": "11", "ExposureMaxGain": "16", "VisibilityEnhancerLevel": "0",
            "WhiteBalanceMode": "auto", "WhiteBalanceCrGain": "64", "WhiteBalanceCbGain": "64",
            "DetailLevel": "7", "NoiseReduction2DLevel": "1", "NoiseReduction3DLevel": "2",
            "ImageMode": "standard", "DigitalBrightLevel": "7", "ColorHue": "7",
            "ColorSaturation": "7", "GammaLevel": "3",
            # ptzf (composed into AbsolutePTZF)
            "Pan": 0, "Tilt": 0, "Zoom": 0, "Focus": 0x031B,
            # tally
            "TallyCMMDMode": "link", "TallyControl": "off", "TallyLevel": "off",
            # presetposition
            "CallMode": "normal",
            # network
            "CameraName": "CV620-BI2_WI2", "Location": "Default", "Dhcp": "on",
            "Ip": "192.168.1.67", "Subnetmask": "255.255.255.0", "Gateway": "192.168.1.1",
            "PrimaryDns": "192.168.1.1", "CurrentIp": "192.168.1.67",
            "CurrentSubnetmask": "255.255.255.0", "CurrentGateway": "192.168.1.1",
            "CurrentPrimaryDns": "192.168.1.1", "MacAddress": "00:23:a8:ff:81:cf",
            "HttpPort": "80", "HttpsPort": "81", "RTSPPort1": "8557", "RTSPPort2": "8556",
            # srt
            "SRTEnable": "off", "SRTConnectMode": "caller", "SRTUrl": "", "SRTPort": "1025",
            "SRTStreamID": "", "SRTLatency": "120", "SRTEncryption": "none",
            "SRTPassphraseUsed": "0",
            # rtmp
            "RtmpEnable": "off", "RtmpServerUrl1": "", "RtmpStreamKey1Used": "0",
            "AuthenRtmp": "off", "AuthenRtmpUserName": "", "AuthenRtmpPasswordUsed": "0",
            "RtmpVideoInfo": "H.264@7000kbps", "RtmpAudioInfo": "None",
            "RtmpVideoResolution": "1080p(60fps)", "RtmpChunkSize": "1412",
            "RtmpStatus": "RTMP disable",
            # mpeg2ts / freedconfig / user
            "MPEG2TSEnable": "off", "MPEG2TSIP": "", "MPEG2TSPort": "0",
            "FreeD": "off", "FreeDOutput": "ip", "FreeDDestinationIP": "0.0.0.0", "FreeDUdpPort": "0",
            "AuthenRTSP": "off", "AdminInfo": "admin",
            # simulator bookkeeping
            "last_preset": "",
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {"type": "select", "key": "Power", "label": "Power", "options": ["on", "standby"]},
            {"type": "select", "key": "TallyControl", "label": "Tally lamp", "options": ["on", "off"]},
            {"type": "select", "key": "FocusMode", "label": "Focus mode", "options": ["auto", "manual"]},
            {"type": "select", "key": "ExposureMode", "label": "Exposure mode",
             "options": ["auto", "shutter", "iris", "manual"]},
            {"type": "select", "key": "WhiteBalanceMode", "label": "White balance",
             "options": ["auto", "atw", "indoor", "outdoor", "onepushwb", "sodiumlamp", "manual"]},
            {"type": "select", "key": "AuthenRTSP", "label": "RTSP authentication", "options": ["off", "on"]},
            {"type": "select", "key": "RtmpStreamKey1Used", "label": "RTMP stream key set",
             "options": ["0", "1"]},
            {"type": "indicator", "key": "Pan", "label": "Pan"},
            {"type": "indicator", "key": "Tilt", "label": "Tilt"},
            {"type": "indicator", "key": "Zoom", "label": "Zoom"},
            {"type": "indicator", "key": "Focus", "label": "Focus"},
            {"type": "indicator", "key": "last_preset", "label": "Last preset recalled"},
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "Camera stops answering",
                "behavior": "no_response",
            },
            "password_changed": {
                "description": "The camera's password was changed: every request is refused (HTTP 401)",
                "behavior": "custom",
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        self._username = str(cfg.get("username", "") or "admin")
        self._password = str(cfg.get("password", "") or "")
        self._standby_delay = float(cfg.get("standby_delay", 13.0) or 0)
        self._wake_delay = float(cfg.get("wake_delay", 16.0) or 0)
        self._reboot_downtime = float(cfg.get("reboot_downtime", 5.0) or 0)
        self._nonces: set[str] = set()
        self._presets: dict[int, tuple[int, int, int, int]] = {}
        # axis -> (counts per second, start time, start position)
        self._moves: dict[str, tuple[float, float, int]] = {}
        self._power_change: tuple[str, float] | None = None
        self._down_until = 0.0
        # Seconds the current request's answer waits (the camera answers an
        # exact zoom, Home or a preset recall only once it has arrived).
        self._answer_delay = 0.0
        self.requests: list[str] = []

    # ── Positions and power, advanced lazily ──

    def _axis(self, axis: str) -> int:
        key = axis.capitalize()
        move = self._moves.get(axis)
        if move is None:
            return int(self.state.get(key, 0))
        rate, t0, p0 = move
        lo, hi = self._travel(axis)
        return int(max(lo, min(hi, p0 + rate * (time.monotonic() - t0))))

    def _travel(self, axis: str) -> tuple[int, int]:
        """Where the axis can go: the mechanical stops, the limit angles while
        they are on, the digital zoom limit."""
        lo, hi = _LIMIT[axis]
        if axis == "zoom":
            hi = self._zoom_max()
        elif axis in ("pan", "tilt") and self.state.get("PanTiltLimit") == "on":
            if axis == "pan":
                lo = max(lo, int(int(self.state.get("PanLeftLimit", -170)) * _PAN_PER_DEGREE))
                hi = min(hi, int(int(self.state.get("PanRightLimit", 170)) * _PAN_PER_DEGREE))
            else:
                lo = max(lo, int(int(self.state.get("TiltDownLimit", -30)) * _TILT_PER_DEGREE))
                hi = min(hi, int(int(self.state.get("TiltUpLimit", 90)) * _TILT_PER_DEGREE))
        return lo, hi

    def _stop(self, axis: str) -> None:
        if axis in self._moves:
            self.set_state(axis.capitalize(), self._axis(axis))
            del self._moves[axis]

    def _start(self, axis: str, direction: int, speed: int) -> None:
        self._stop(axis)
        self._moves[axis] = (direction * _rate(axis, speed), time.monotonic(), self._axis(axis))

    def _zoom_max(self) -> int:
        limit = str(self.state.get("DZoomLimit", "x1"))
        return 0x4000 if limit == "x1" else 0x7AC0

    def _settle_power(self) -> None:
        if self._power_change and time.monotonic() >= self._power_change[1]:
            self.set_state("Power", self._power_change[0])
            self._power_change = None

    def _value(self, name: str) -> str:
        if name == "AbsolutePTZF":
            positions = [self._axis(a) for a in ("pan", "tilt", "zoom", "focus")]
            # Keep the Simulator UI's indicators moving with the camera.
            for key, value in zip(("Pan", "Tilt", "Zoom", "Focus"), positions):
                if self.state.get(key) != value:
                    self.set_state(key, value)
            return ",".join(_hex(v) for v in positions) + ",0005,0005"
        value = self.state.get(name, "")
        if isinstance(value, bool):  # a Simulator UI toggle stores a real boolean
            return "on" if value else "off"
        return str(value)

    # ── Authentication ──

    def _challenge(self) -> tuple[int, str, dict[str, str]]:
        nonce = secrets.token_hex(8)
        self._nonces.add(nonce)
        return 401, "401 Authorization Required", {
            "WWW-Authenticate": f'Digest algorithm="MD5", qop="auth", realm="{REALM}", nonce="{nonce}"',
        }

    def _authorized(self, method: str, headers: dict[str, str]) -> bool:
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if not auth.startswith("Digest "):
            return False
        fields = {k.lower(): v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', auth[7:])}
        if fields.get("username") != self._username or fields.get("nonce") not in self._nonces:
            return False

        def expected(password: str) -> str:
            ha1 = hashlib.md5(f"{self._username}:{REALM}:{password}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{fields.get('uri', '')}".encode()).hexdigest()
            tail = f"{fields.get('nc', '')}:{fields.get('cnonce', '')}:{fields.get('qop', '')}:" if fields.get("qop") else ""
            return hashlib.md5(f"{ha1}:{fields['nonce']}:{tail}{ha2}".encode()).hexdigest()

        answer = fields.get("response", "")
        if self._password:
            return answer == expected(self._password)
        return answer != expected("invalid")

    # ── Dispatch ──

    async def respond_http(self, request, method, path, headers, body):
        """Answer as the camera does: an exact zoom, Home and a preset recall
        are answered once the camera has arrived."""
        self._answer_delay = 0.0
        response = await super().respond_http(request, method, path, headers, body)
        if self._answer_delay > 0:
            await asyncio.sleep(self._answer_delay)
        return response

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        if time.monotonic() < self._down_until:
            return 503, "Service Unavailable"
        if "password_changed" in self.active_errors or not self._authorized(method, headers):
            return self._challenge()
        parts = urlsplit(path)
        query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        self.requests.append(f"{method} {path}")
        self._settle_power()
        m = re.fullmatch(r"/command/([a-z0-9]+)\.cgi", parts.path)
        if method != "GET" or m is None:
            return 404, "404 Not Found"
        cgi = m.group(1)
        if cgi == "inquiry":
            group = query.get("inqjs", "")
            if group not in GROUPS:
                return 400, '{"STATUS":"NG"}'
            return 200, "".join(f'var {name}="{self._value(name)}"\r\n' for name in GROUPS[group])
        if cgi not in GROUPS and cgi not in ("main",):
            return 403, "403 Forbidden"
        if len(query) != 1:
            return 400, '{"STATUS":"NG"}'
        (param, value), = query.items()
        return self._apply(cgi, param, value)

    def _apply(self, cgi: str, param: str, value: str):
        ok, ng = (200, '{"STATUS":"OK"}'), (400, '{"STATUS":"NG"}')
        if cgi == "main":
            return self._main(param, value)
        if cgi == "ptzf":
            return self._ptzf(param, value)
        if cgi == "presetposition" and param != "CallMode":
            return self._preset(param, value)
        accepts = WRITABLE.get((cgi, param))
        if accepts is None or not accepts(value):
            return ng
        mode = self.state.get("ExposureMode")
        # Acknowledged and not applied, as on the bench unit.
        if param in ("ExposureExposureTime", "ExposureIris"):
            priority = {"ExposureExposureTime": "shutter", "ExposureIris": "iris"}[param]
            if mode == priority:
                self.set_state(f"{param}Pri", value)
            elif mode == "manual":
                self.set_state(param, value)
            return ok
        if param == "ExposureGain" and mode != "manual":
            return ok
        if param in ("WhiteBalanceCrGain", "WhiteBalanceCbGain") and self.state.get("WhiteBalanceMode") != "manual":
            return ok
        if param in _LIMIT_ANGLES and self.state.get("PanTiltLimit") != "on":
            return ok
        if param == "PTZAssist" and value != self.state.get("PTZAssist") and self.state.get("FocusMode") != "manual":
            return 500, '{"STATUS":"NG"}'  # any change outside manual focus, off included (bench)
        if param == "SmartAF" or param == "WhiteBalanceOnePushTrg":
            return ok
        if param == "CameraName":
            value = value[:16]
        if param == "TallyControl":
            if value not in _ONOFF:
                return ok
            if value == "on" and self.state.get("TallyCMMDMode") == "link":
                self.set_state("TallyLevel", "high")
        if param == "TallyLevel" and value in ("low", "high") and self.state.get("TallyCMMDMode") == "link":
            self.set_state("TallyControl", "on")
        if param == "SRTEnable" and value == "on":
            if self.state.get("SRTConnectMode") == "caller" and not self.state.get("SRTUrl"):
                return ng
        if param == "RtmpEnable" and value == "on":
            if not self.state.get("RtmpServerUrl1") or str(self.state.get("RtmpStreamKey1Used")) != "1":
                return ng
            self.set_state("RtmpStatus", "Connection success")
        if param == "RtmpEnable" and value == "off":
            self.set_state("RtmpStatus", "RTMP disable")
        self.set_state(param, value)
        return ok

    def _main(self, param: str, value: str):
        if param == "System" and value in ("on", "standby"):
            delay = self._wake_delay if value == "on" else self._standby_delay
            self._power_change = (value, time.monotonic() + delay)
            if delay <= 0:
                self._settle_power()
            return 200, '{"STATUS":"OK"}'
        if param == "System" and value == "reboot":
            self._down_until = time.monotonic() + self._reboot_downtime
            return 200, '{"STATUS":"OK"}'
        if param == "FactoryDefault" and value in ("soft", "hard"):
            keep = {k: self.state.get(k) for k in GROUPS["network"]} if value == "soft" else {}
            for key, initial in self.SIMULATOR_INFO["initial_state"].items():
                self.set_state(key, keep.get(key, initial))
            self._presets.clear()
            return 200, '{"STATUS":"OK"}'
        return 400, '{"STATUS":"NG"}'

    def _ptzf(self, param: str, value: str):
        ok, ng = (200, '{"STATUS":"OK"}'), (400, '{"STATUS":"NG"}')
        standby = self.state.get("Power") == "standby"
        if param == "AbsoluteZoom":
            try:
                position = int(value, 16)
            except ValueError:
                return ng
            if not 0 <= position <= self._zoom_max():
                return ng
            if not standby:
                self._stop("zoom")
                self._answer_delay = 0.1 + _zoom_seconds(self._axis("zoom"), position)
                self.set_state("Zoom", position)
            return ok
        if param != "Move" or "," not in value:
            return ng
        word, _, arg = value.partition(",")
        if word == "stop":
            axes = {"motor": ("pan", "tilt"), "zoom": ("zoom",), "focus": ("focus",)}.get(arg)
            if axes is None:
                return ng
            for axis in axes:
                self._stop(axis)
            return ok
        if word == "onepushaf":
            return ok
        if not _INT.match(arg):
            return ng
        speed = int(arg)
        pt = {
            "up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0),
            "up-left": (-1, 1), "up-right": (1, 1), "down-left": (-1, -1), "down-right": (1, -1),
        }
        if word in pt:
            if not 0 <= speed <= 23:
                return ng
            if standby:
                return ok  # acknowledged and ignored in standby (bench)
            pan, tilt = pt[word]
            for axis, direction in (("pan", pan), ("tilt", tilt)):
                if direction:
                    self._start(axis, direction, speed)
                else:
                    self._stop(axis)
            return ok
        zf = {"tele": ("zoom", 1), "wide": ("zoom", -1), "far": ("focus", 1), "near": ("focus", -1)}
        if word in zf and 0 <= speed <= 7:
            if not standby:
                self._start(zf[word][0], zf[word][1], speed)
            return ok
        return ng

    def _preset(self, param: str, value: str):
        ok, ng = (200, '{"STATUS":"OK"}'), (400, '{"STATUS":"NG"}')
        if param == "HomePos" and value == "recall":
            if self.state.get("Power") != "standby":
                for axis in ("pan", "tilt"):
                    self._stop(axis)
                # Measured: about 1.2 s back from the pan stop, 0.8 s from
                # the top of tilt, 0.2 s from close by.
                self._answer_delay = max(0.2, abs(self._axis("pan")) / 260000, abs(self._axis("tilt")) / 160000)
                self.set_state("Pan", 0)
                self.set_state("Tilt", 0)
            return ok
        if param not in ("PresetSet", "PresetCall", "PresetClear") or not _range(0, 255)(value):
            return ng
        n = int(value)
        if param == "PresetSet":
            self._presets[n] = tuple(self._axis(a) for a in ("pan", "tilt", "zoom", "focus"))
        elif param == "PresetClear":
            self._presets.pop(n, None)
        elif self.state.get("Power") != "standby":
            stored = self._presets.get(n)
            if stored is not None:
                for axis in ("pan", "tilt", "zoom", "focus"):
                    self._stop(axis)
                # Measured: about 0.75 s plus the trip at Preset Speed.
                deg_s = _PRESET_DEG_S.get(int(self.state.get("PresetSpeed", 5) or 5), 160)
                trip = max(abs(self._axis("pan") - stored[0]) / _PAN_PER_DEGREE,
                           abs(self._axis("tilt") - stored[1]) / _TILT_PER_DEGREE) / deg_s
                self._answer_delay = 0.75 + trip
                for key, position in zip(("Pan", "Tilt", "Zoom", "Focus"), stored):
                    self.set_state(key, position)
            self.set_state("last_preset", str(n))
        return ok
