"""
BirdDog NDI Encoder/Decoder — Simulator
Auto-generated skeleton. Fill in the handler method with protocol logic.

Driver: birddog_codec
Transport: http
"""
from simulator.http_simulator import HTTPSimulator


class BirddogCodecSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "birddog_codec",
        "name": "BirdDog NDI Encoder/Decoder Simulator",
        "category": "video",
        "transport": "http",
        "default_port": 8080,
        "initial_state": {
            "hostname": "",
            "firmware": "",
            "model": "",
            "operation_mode": "off",
            "decode_source": "",
            "source_count": 0,
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
            select_source        — Select NDI Source (params: source_name: string)
            next_source          — Next NDI Source
            previous_source      — Previous NDI Source
            refresh_sources      — Refresh NDI Sources
            reboot               — Reboot Device
            restart_video        — Restart Video

        State variables to maintain:
            hostname             (string  ) — Hostname
            firmware             (string  ) — Firmware Version
            model                (string  ) — Model
            operation_mode       (enum    ) — Operation Mode
            decode_source        (string  ) — Current NDI Source
            source_count         (integer ) — Available NDI Sources
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
