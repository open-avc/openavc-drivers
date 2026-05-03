"""
AVer PTZ310/PTZ330 — Simulator

Simulates the AVer Pro-AV HTTP CGI surface (port 80, ``/storks?cmd=...``).
Covers image-quality controls (shutter, iris, gain, gain limit, white
balance, color temperature, saturation, contrast, sharpness, noise
filter, mirror/flip), AI feature toggles (SmartShoot, SmartFraming),
RTMP streaming control, presets, freeze, and the bulk ``get_sys_stat``
query.

VISCA-over-IP commands are handled directly by the driver over a
separate UDP socket (the simulator doesn't echo them back). HTTP is
the surface where AVer-specific behavior lives, so HTTP is what we
verify here.

Driver: aver_ptz
Transport: http
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from simulator.http_simulator import HTTPSimulator


_SYS_STAT_KEYS = (
    "model_name", "fw_ver", "sn", "mac", "ipaddr",
    "expmode", "expval", "shutter", "iris", "gain", "gain_limit",
    "wb", "colortemp",
    "saturation", "contrast", "sharpness", "nf", "blc",
    "mirror_flip", "freq",
    "smartshot", "smartframing", "slow_shutter", "pt_slow",
    "osd_disp", "osd_status", "motion_mode", "power_saving", "web_lang",
)


class AverPtzSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "aver_ptz",
        "name": "AVer PTZ310/330 Simulator",
        "category": "camera",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "model_name": "PTZ330",
            "fw_ver": "0.0.0003.72",
            "sn": "5309112400007",
            "mac": "00:18:1A:04:AC:80",
            "ipaddr": "127.0.0.1",
            "expmode": 0,            # full auto
            "expval": 0,
            "shutter": 6,
            "iris": 7,
            "gain": 4,
            "gain_limit": 4,
            "wb": 0,                 # auto
            "colortemp": 5600,
            "saturation": 5,
            "contrast": 2,
            "sharpness": 1,
            "nf": 1,                 # low
            "blc": 0,                # off
            "mirror_flip": 0,        # off
            "freq": 2,               # 60 Hz
            "smartshot": 0,
            "smartframing": 0,
            "slow_shutter": "off",
            "pt_slow": "off",
            "osd_disp": 0,
            "osd_status": 1,
            "motion_mode": 0,
            "power_saving": 0,
            "web_lang": 0,
            "pan_speed": 12,
            "tilt_speed": 10,
            "zoom_speed": 4,
            "preset_speed": 4,
            "rtmp_running": False,
            "freeze": False,
        },
        "delays": {
            "command_response": 0.02,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Camera stops responding to HTTP requests",
                "behavior": "no_response",
            },
        },
        "controls": [
            {"type": "indicator", "key": "model_name", "label": "Model"},
            {"type": "indicator", "key": "fw_ver", "label": "Firmware"},
            {
                "type": "select",
                "key": "expmode",
                "label": "Exposure Mode",
                "options": [
                    {"value": 0, "label": "Full Auto"},
                    {"value": 1, "label": "Shutter Pri"},
                    {"value": 2, "label": "Iris Pri"},
                    {"value": 3, "label": "Manual"},
                ],
            },
            {
                "type": "select",
                "key": "wb",
                "label": "White Balance",
                "options": [
                    {"value": 0, "label": "Auto"},
                    {"value": 2, "label": "Indoor"},
                    {"value": 3, "label": "Outdoor"},
                    {"value": 4, "label": "One Push"},
                    {"value": 5, "label": "Manual"},
                ],
            },
            {
                "type": "slider",
                "key": "colortemp",
                "label": "Color Temperature (K)",
                "min": 2500,
                "max": 10000,
                "step": 100,
            },
            {
                "type": "toggle",
                "key": "smartshot",
                "label": "SmartShoot",
                "on_value": 1,
                "off_value": 0,
            },
            {
                "type": "toggle",
                "key": "smartframing",
                "label": "SmartFraming",
                "on_value": 1,
                "off_value": 0,
            },
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None) -> None:
        super().__init__(device_id, config)
        self._presets: dict[int, bool] = {}

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        parsed = urlparse(path)
        if parsed.path != "/storks":
            return 404, "not found"

        # Driver always sends "?cmd=<cmd>" as the only query key.
        cmd = (parse_qs(parsed.query).get("cmd") or [""])[0]
        if not cmd:
            return 400, "missing cmd"

        # Some commands carry a positional arg via colon: "set_shutter:6"
        if ":" in cmd:
            head, _, arg = cmd.partition(":")
        else:
            head, arg = cmd, ""

        return self._dispatch(head, arg, cmd, body)

    def _dispatch(
        self, head: str, arg: str, full_cmd: str, body: str
    ) -> tuple[int, str]:
        # Movement (HTTP — AVer's own *_start / *_end). Acknowledge with
        # "ok"; the driver doesn't expect any side effects to read back.
        if head.endswith("_start") or head.endswith("_end") or head == "go_home":
            return 200, "ok"
        if head in ("focus_trigger", "set_freeze"):
            return 200, "ok"

        # Presets
        if head == "set_preset":
            self._presets[self._int(arg, 1)] = True
            return 200, "ok"
        if head == "load_preset":
            return 200, "ok"

        # Image / exposure / WB / picture — colon-separated integer setters
        int_setters = {
            "set_pan_speed":   ("pan_speed", 1, 24),
            "set_tilt_speed":  ("tilt_speed", 1, 24),
            "set_zoom_speed":  ("zoom_speed", 0, 6),
            "set_preset_speed": ("preset_speed", 1, 6),
            "set_expmode":     ("expmode", 0, 3),
            "set_expval":      ("expval", -4, 4),
            "set_shutter":     ("shutter", 0, 15),
            "set_iris":        ("iris", 0, 13),
            "set_gain":        ("gain", 0, 16),
            "set_gain_limit":  ("gain_limit", 0, 8),
            "set_wb":          ("wb", 0, 5),
            "set_colortemp":   ("colortemp", 2500, 10000),
            "set_saturation":  ("saturation", 0, 10),
            "set_contrast":    ("contrast", 0, 4),
            "set_sharpness":   ("sharpness", 0, 3),
            "set_nf":          ("nf", 0, 3),
            "set_blc":         ("blc", 0, 2),
            "mirror_flip":     ("mirror_flip", 0, 3),
            "set_freq":        ("freq", 1, 3),
        }
        if head in int_setters:
            key, lo, hi = int_setters[head]
            v = max(lo, min(hi, self._int(arg, lo)))
            self.set_state(key, v)
            return 200, "ok"

        # On/off setters
        on_off_setters = {
            "set_slow_shutter": "slow_shutter",
            "set_pt_slow":      "pt_slow",
            "set_mirror":       "mirror",
            "set_flip":         "flip",
            "enable_af":        "enable_af",
        }
        if head in on_off_setters:
            self.set_state(on_off_setters[head], arg)
            return 200, "ok"

        # MF Near/Far
        if head == "mf":
            self.set_state("mf", arg)
            return 200, "ok"

        # 1/0 SmartShot / SmartFraming
        if head == "set_smartshot":
            self.set_state("smartshot", self._int(arg, 0))
            return 200, "ok"
        if head == "set_smartframing":
            self.set_state("smartframing", self._int(arg, 0))
            return 200, "ok"

        # White balance one-push
        if head == "set_one_push_wb":
            return 200, "ok"

        # Image defaults
        if head == "set_img_default":
            return 200, "ok"

        # RTMP control. Real cameras take a function-style arg
        # "set_rtmp_start(server=...,key=...)" — we accept any tail.
        if head == "set_rtmp_start" or full_cmd.startswith("set_rtmp_start"):
            self.set_state("rtmp_running", True)
            return 200, "ok"
        if head == "set_rtmp_stop":
            self.set_state("rtmp_running", False)
            return 200, "ok"

        # Reboot / factory default (would normally require Basic auth on
        # PTZ-S* SKUs; the simulator accepts unauthenticated calls).
        if head == "sys_reboot":
            return 200, "ok"
        if head == "set_factory_default":
            return 200, "ok"

        # Bulk status
        if head == "get_sys_stat":
            return 200, self._sys_stat_text()

        return 404, f"unknown cmd: {head}"

    def _sys_stat_text(self) -> str:
        parts: list[str] = []
        for key in _SYS_STAT_KEYS:
            value = self.get_state(key, "")
            parts.append(f"{key}={value}")
        return ";".join(parts) + ";"

    @staticmethod
    def _int(arg: str, default: int) -> int:
        try:
            return int(arg)
        except (TypeError, ValueError):
            return default
