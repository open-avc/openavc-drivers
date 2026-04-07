"""
BirdDog PTZ Camera — Simulator

Simulates a BirdDog PTZ camera REST API on port 8080. Handles all
HTTP endpoints used by the birddog_ptz driver: device info, PTZ presets,
exposure, white balance, encode/NDI settings, and tally control.

VISCA-over-UDP commands (pan/tilt/zoom/focus) are not simulated because
the driver sends those over a separate UDP socket, not HTTP.

Driver: birddog_ptz
Transport: http
"""

import json

from simulator.http_simulator import HTTPSimulator


class BirddogPtzSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "birddog_ptz",
        "name": "BirdDog PTZ Camera Simulator",
        "category": "camera",
        "transport": "http",
        "default_port": 8080,
        "initial_state": {
            "hostname": "BIRDDOG-SIM",
            "model": "P200 A2",
            "firmware": "6.0.1",
            "video_format": "1920x1080p60",
            "ndi_name": "BIRDDOG-SIM",
            "tally_mode": "Off",
            "exposure_mode": "FULL AUTO",
            "wb_mode": "AUTO",
            "preset": 1,
            # PTZ config state (birddogptzsetup)
            "pan_speed": "8",
            "tilt_speed": "8",
            "preset_speed": "24",
            # Picture settings (birddogpicsetup)
            "brightness": "128",
            "contrast": "128",
            "sharpness": "128",
        },
        "delays": {
            "command_response": 0.05,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Camera stops responding to HTTP requests",
                "behavior": "no_response",
            },
        },
        "controls": [
            {
                "type": "select",
                "key": "tally_mode",
                "label": "Tally",
                "options": ["Off", "Program", "Preview"],
            },
            {
                "type": "select",
                "key": "exposure_mode",
                "label": "Exposure Mode",
                "options": [
                    "FULL AUTO",
                    "MANUAL",
                    "SHUTTER Pri",
                    "IRIS Pri",
                    "BRIGHT",
                ],
            },
            {
                "type": "select",
                "key": "wb_mode",
                "label": "White Balance",
                "options": ["AUTO", "INDOOR", "OUTDOOR", "ONE PUSH", "MANUAL"],
            },
            {
                "type": "presets",
                "key": "preset",
                "label": "Presets",
                "count": 8,
            },
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
        ],
    }

    # Saved presets (preset number -> True means saved)
    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._saved_presets: set[int] = {1, 2, 3}

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        # Strip query string for matching
        clean_path = path.split("?")[0].rstrip("/")

        # Parse JSON body for POST requests
        body_data: dict = {}
        if body:
            try:
                body_data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                pass

        # ── /about — Device info ──
        if clean_path == "/about":
            if method == "POST" and body_data:
                if "HostName" in body_data:
                    self.set_state("hostname", body_data["HostName"])
                return 200, self._about_response()
            return 200, self._about_response()

        # ── /recall — Recall PTZ preset ──
        if clean_path == "/recall" and method == "POST":
            preset_str = body_data.get("Preset", "Preset-1")
            try:
                preset_num = int(preset_str.replace("Preset-", ""))
            except (ValueError, AttributeError):
                preset_num = 1
            self.set_state("preset", preset_num)
            return 200, {"Preset": preset_str}

        # ── /save — Save PTZ preset ──
        if clean_path == "/save" and method == "POST":
            preset_str = body_data.get("Preset", "Preset-1")
            try:
                preset_num = int(preset_str.replace("Preset-", ""))
            except (ValueError, AttributeError):
                preset_num = 1
            self._saved_presets.add(preset_num)
            return 200, {"Preset": preset_str}

        # ── /birddogptzsetup — PTZ configuration ──
        if clean_path == "/birddogptzsetup":
            if method == "POST" and body_data:
                for key in ("PanSpeed", "TiltSpeed", "PresetSpeed"):
                    if key in body_data:
                        state_key = {
                            "PanSpeed": "pan_speed",
                            "TiltSpeed": "tilt_speed",
                            "PresetSpeed": "preset_speed",
                        }[key]
                        self.set_state(state_key, str(body_data[key]))
            return 200, {
                "PanSpeed": self.get_state("pan_speed", "8"),
                "TiltSpeed": self.get_state("tilt_speed", "8"),
                "PresetSpeed": self.get_state("preset_speed", "24"),
            }

        # ── /birddogexpsetup — Exposure settings ──
        if clean_path == "/birddogexpsetup":
            if method == "POST" and body_data:
                if "ExpMode" in body_data:
                    self.set_state("exposure_mode", body_data["ExpMode"])
            return 200, {
                "ExpMode": self.get_state("exposure_mode", "FULL AUTO"),
            }

        # ── /birddogwbsetup — White balance settings ──
        if clean_path == "/birddogwbsetup":
            if method == "POST" and body_data:
                if "WBMode" in body_data:
                    self.set_state("wb_mode", body_data["WBMode"])
            return 200, {
                "WBMode": self.get_state("wb_mode", "AUTO"),
            }

        # ── /birddogpicsetup — Picture settings ──
        if clean_path == "/birddogpicsetup":
            if method == "POST" and body_data:
                for key in ("Brightness", "Contrast", "Sharpness"):
                    if key in body_data:
                        self.set_state(key.lower(), str(body_data[key]))
            return 200, {
                "Brightness": self.get_state("brightness", "128"),
                "Contrast": self.get_state("contrast", "128"),
                "Sharpness": self.get_state("sharpness", "128"),
            }

        # ── /encodesetup — NDI encode settings ──
        if clean_path == "/encodesetup":
            if method == "POST" and body_data:
                if "NDIName" in body_data:
                    self.set_state("ndi_name", body_data["NDIName"])
                if "VideoFormat" in body_data:
                    self.set_state("video_format", body_data["VideoFormat"])
                if "TallyMode" in body_data:
                    self.set_state("tally_mode", body_data["TallyMode"])
            return 200, {
                "NDIName": self.get_state("ndi_name", "BIRDDOG-SIM"),
                "VideoFormat": self.get_state("video_format", "1920x1080p60"),
                "TallyMode": self.get_state("tally_mode", "Off"),
            }

        # ── /tally — Tally light state ──
        if clean_path == "/tally":
            if method == "POST" and body_data:
                if "tally_state" in body_data:
                    self.set_state("tally_mode", body_data["tally_state"])
            return 200, {
                "tally_state": self.get_state("tally_mode", "Off"),
            }

        # ── /analogaudiosetup — Audio settings ──
        if clean_path == "/analogaudiosetup":
            return 200, {
                "AudioGain": "0",
                "AudioOutput": "Analog",
            }

        # ── /NDIDisServer — NDI discovery server ──
        if clean_path == "/NDIDisServer":
            return 200, {
                "NDIDisServer": "",
            }

        return 404, {"error": "not found"}

    def _about_response(self) -> dict:
        """Build the /about response matching BirdDog firmware format."""
        return {
            "HostName": self.get_state("hostname", "BIRDDOG-SIM"),
            "Format": self.get_state("model", "P200 A2"),
            "FirmwareVersion": self.get_state("firmware", "6.0.1"),
        }
