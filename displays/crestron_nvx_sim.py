"""
Crestron DM NVX — Simulator
Auto-generated skeleton. Fill in the handler method with protocol logic.

Driver: crestron_nvx
Transport: http
"""
from simulator.http_simulator import HTTPSimulator


class CrestronNvxSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "crestron_nvx",
        "name": "Crestron DM NVX Simulator",
        "category": "display",
        "transport": "http",
        "default_port": 443,
        "initial_state": {
            "device_mode": "off",
            "device_ready": False,
            "video_source": "",
            "audio_source": "",
            "active_video_source": "",
            "active_audio_source": "",
            "stream_multicast": "",
            "horizontal_resolution": 0,
            "vertical_resolution": 0,
            "sync_detected": False,
            "firmware": "",
        },
        "delays": {
            "command_response": 0.05,
        },
        "error_modes": {
            # Add error modes relevant to this device, e.g.:
            # "no_signal": {
            #     "description": "No input signal detected",
            # },
        },
    }

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        """
        Handle incoming HTTP request from the driver.
        Return (status_code, response_body).

        Available helpers:
            self.state              — dict of current state values
            self.set_state(k, v)    — update state (triggers UI refresh)
            self.active_errors      — set of currently active error mode names

        Driver commands to handle:
            set_video_source     — Set Video Source (params: source: enum)
            set_audio_source     — Set Audio Source (params: source: enum)
            route_stream         — Route Stream (Decoder) (params: multicast_address: string)
            set_stream_url       — Set Stream URL (Decoder) (params: url: string)
            enable_leds          — Enable Front Panel LEDs
            disable_leds         — Disable Front Panel LEDs
            reboot               — Reboot Device

        State variables to maintain:
            device_mode          (enum    ) — Device Mode
            device_ready         (boolean ) — Device Ready
            video_source         (string  ) — Video Source
            audio_source         (string  ) — Audio Source
            active_video_source  (string  ) — Active Video Source
            active_audio_source  (string  ) — Active Audio Source
            stream_multicast     (string  ) — Stream Multicast Address
            horizontal_resolution (integer ) — Horizontal Resolution
            vertical_resolution  (integer ) — Vertical Resolution
            sync_detected        (boolean ) — Sync Detected
            firmware             (string  ) — Firmware Version
        """
        # TODO: Implement API endpoint handlers.
        #
        # Example for a JSON API:
        #   import json
        #   if path == "/api/power" and method == "POST":
        #       req = json.loads(body)
        #       self.set_state("power", req.get("power", "off"))
        #       return 200, {"status": "ok"}
        #   if path == "/api/status" and method == "GET":
        #       return 200, self.state

        return 404, {"error": "not found"}
