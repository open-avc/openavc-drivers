"""
BirdDog PTZ Camera — Simulator
Auto-generated skeleton. Fill in the handler method with protocol logic.

Driver: birddog_ptz
Transport: http
"""
from simulator.http_simulator import HTTPSimulator


class BirddogPtzSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "birddog_ptz",
        "name": "BirdDog PTZ Camera Simulator",
        "category": "camera",
        "transport": "http",
        "default_port": 8080,
        "initial_state": {
            "hostname": "",
            "model": "",
            "firmware": "",
            "video_format": "",
            "ndi_name": "",
            "tally_mode": "",
            "exposure_mode": "",
            "wb_mode": "",
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
            pt_up                — Pan/Tilt Up
            pt_down              — Pan/Tilt Down
            pt_left              — Pan/Tilt Left
            pt_right             — Pan/Tilt Right
            pt_up_left           — Pan/Tilt Up-Left
            pt_up_right          — Pan/Tilt Up-Right
            pt_down_left         — Pan/Tilt Down-Left
            pt_down_right        — Pan/Tilt Down-Right
            pt_stop              — Pan/Tilt Stop
            pt_home              — Pan/Tilt Home
            zoom_in              — Zoom In
            zoom_out             — Zoom Out
            zoom_stop            — Zoom Stop
            focus_auto           — Auto Focus
            focus_manual         — Manual Focus
            focus_near           — Focus Near
            focus_far            — Focus Far
            focus_stop           — Focus Stop
            focus_one_push       — One-Push Auto Focus
            recall_preset        — Recall Preset (params: preset: integer)
            save_preset          — Save Preset (params: preset: integer)
            set_exposure_mode    — Set Exposure Mode (params: mode: enum)
            set_wb_mode          — Set White Balance Mode (params: mode: enum)
            set_tally            — Set Tally (params: state: enum)
            power_on             — Power On
            standby              — Standby

        State variables to maintain:
            hostname             (string  ) — Hostname
            model                (string  ) — Model
            firmware             (string  ) — Firmware Version
            video_format         (string  ) — Video Format
            ndi_name             (string  ) — NDI Source Name
            tally_mode           (string  ) — Tally Mode
            exposure_mode        (string  ) — Exposure Mode
            wb_mode              (string  ) — White Balance Mode
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
