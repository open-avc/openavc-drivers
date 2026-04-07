"""
Sonos Speaker — Simulator
Auto-generated skeleton. Fill in the handler method with protocol logic.

Driver: sonos
Transport: http
"""
from simulator.http_simulator import HTTPSimulator


class SonosSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "sonos",
        "name": "Sonos Speaker Simulator",
        "category": "audio",
        "transport": "http",
        "default_port": 1400,
        "initial_state": {
            "transport_state": "off",
            "volume": 0,
            "mute": False,
            "track_title": "",
            "track_artist": "",
            "track_album": "",
            "track_duration": "",
            "track_position": "",
            "speaker_name": "",
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
            play                 — Play
            pause                — Pause
            stop                 — Stop
            next_track           — Next Track
            previous_track       — Previous Track
            set_volume           — Set Volume (params: level: integer)
            volume_up            — Volume Up
            volume_down          — Volume Down
            mute_on              — Mute
            mute_off             — Unmute

        State variables to maintain:
            transport_state      (enum    ) — Transport State
            volume               (integer ) — Volume
            mute                 (boolean ) — Mute
            track_title          (string  ) — Track Title
            track_artist         (string  ) — Track Artist
            track_album          (string  ) — Track Album
            track_duration       (string  ) — Track Duration
            track_position       (string  ) — Track Position
            speaker_name         (string  ) — Speaker Name
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
