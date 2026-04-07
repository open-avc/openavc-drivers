"""
Crestron DM NVX — Simulator

Simulates a Crestron DM NVX AV-over-IP encoder/decoder via the REST API
on port 443. Handles device status, AV I/O, stream routing, and the
cookie-based authentication flow.

Driver: crestron_nvx
Transport: http
"""

import json

from simulator.http_simulator import HTTPSimulator


class CrestronNvxSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "crestron_nvx",
        "name": "Crestron DM NVX Simulator",
        "category": "display",
        "transport": "http",
        "default_port": 443,
        "initial_state": {
            "device_mode": "Receiver",
            "device_ready": True,
            "video_source": "Stream",
            "audio_source": "Automatic",
            "active_video_source": "Stream",
            "active_audio_source": "Automatic",
            "stream_multicast": "239.1.2.3",
            "horizontal_resolution": 1920,
            "vertical_resolution": 1080,
            "sync_detected": True,
            "firmware": "6.0.12",
        },
        "delays": {
            "command_response": 0.05,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "NVX stops responding to requests",
                "behavior": "no_response",
            },
            "no_sync": {
                "description": "No video sync detected on input",
                "set_state": {"sync_detected": False},
            },
        },
        "controls": [
            {
                "type": "select",
                "key": "video_source",
                "label": "Video Source",
                "options": ["None", "Input1", "Input2", "Stream"],
            },
            {
                "type": "select",
                "key": "audio_source",
                "label": "Audio Source",
                "options": [
                    "Automatic", "Input1", "Input2", "Analog",
                    "PrimaryAudio", "SecondaryAudio",
                ],
            },
            {
                "type": "indicator",
                "key": "device_mode",
                "label": "Device Mode",
            },
            {
                "type": "indicator",
                "key": "firmware",
                "label": "Firmware",
            },
            {
                "type": "indicator",
                "key": "horizontal_resolution",
                "label": "H. Resolution",
            },
            {
                "type": "indicator",
                "key": "vertical_resolution",
                "label": "V. Resolution",
            },
            {
                "type": "indicator",
                "key": "sync_detected",
                "label": "Sync",
                "color_map": {
                    "true": "#22c55e",
                    "false": "#ef4444",
                },
            },
        ],
    }

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        # ── Authentication flow ──
        if path == "/userlogin.html":
            if method == "GET":
                return 200, "<html><body>Login</body></html>"
            if method == "POST":
                # Accept any credentials, return a fake auth cookie
                return 200, "<html><body>OK</body></html>"

        # ── Device Specific ──
        if path == "/Device/DeviceSpecific":
            if method == "GET":
                return 200, self._build_device_specific()
            if method == "POST":
                return self._handle_device_specific_post(body)

        # ── Audio Video Input Output ──
        if path == "/Device/AudioVideoInputOutput":
            if method == "GET":
                return 200, self._build_av_io()

        # ── Stream Receive ──
        if path == "/Device/StreamReceive":
            if method == "GET":
                return 200, self._build_stream_receive()
            if method == "POST":
                return self._handle_stream_receive_post(body)

        # ── Device Operations (reboot) ──
        if path == "/Device/DeviceOperations" and method == "POST":
            return 200, {"Device": {"DeviceOperations": {"Reboot": True}}}

        return 404, {"error": "Not Found"}

    # ── GET response builders ──

    def _build_device_specific(self) -> dict:
        return {
            "Device": {
                "DeviceSpecific": {
                    "DeviceMode": self.get_state("device_mode", "Receiver"),
                    "DeviceReady": self.get_state("device_ready", True),
                    "VideoSource": self.get_state("video_source", "Stream"),
                    "AudioSource": self.get_state("audio_source", "Automatic"),
                    "ActiveVideoSource": self.get_state("active_video_source", "Stream"),
                    "ActiveAudioSource": self.get_state("active_audio_source", "Automatic"),
                    "Version": self.get_state("firmware", "6.0.12"),
                }
            }
        }

    def _build_av_io(self) -> dict:
        return {
            "Device": {
                "AudioVideoInputOutput": {
                    "Inputs": [
                        {
                            "HorizontalResolution": self.get_state("horizontal_resolution", 1920),
                            "VerticalResolution": self.get_state("vertical_resolution", 1080),
                            "SyncDetected": self.get_state("sync_detected", True),
                        }
                    ]
                }
            }
        }

    def _build_stream_receive(self) -> dict:
        return {
            "Device": {
                "StreamReceive": {
                    "MulticastAddress": self.get_state("stream_multicast", "239.1.2.3"),
                }
            }
        }

    # ── POST handlers ──

    def _handle_device_specific_post(self, body: str) -> tuple[int, dict]:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return 400, {"error": "Invalid JSON"}

        ds = data.get("Device", {}).get("DeviceSpecific", {})

        if "VideoSource" in ds:
            self.set_state("video_source", ds["VideoSource"])
            self.set_state("active_video_source", ds["VideoSource"])

        if "AudioSource" in ds:
            self.set_state("audio_source", ds["AudioSource"])
            self.set_state("active_audio_source", ds["AudioSource"])

        if "LedsEnabled" in ds:
            pass  # Accept but no visible state change in sim

        if "DeviceName" in ds:
            pass  # Accept but not tracked as a state variable

        return 200, self._build_device_specific()

    def _handle_stream_receive_post(self, body: str) -> tuple[int, dict]:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return 400, {"error": "Invalid JSON"}

        sr = data.get("Device", {}).get("StreamReceive", {})

        if "MulticastAddress" in sr:
            self.set_state("stream_multicast", sr["MulticastAddress"])

        if "StreamUrl" in sr:
            pass  # Accept but not tracked separately

        return 200, self._build_stream_receive()
