"""
vMix — Simulator
Auto-generated skeleton. Fill in the handler method with protocol logic.

Driver: vmix
Transport: tcp
"""
from simulator.tcp_simulator import TCPSimulator


class VmixSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "vmix",
        "name": "vMix Simulator",
        "category": "video",
        "transport": "tcp",
        "default_port": 8099,
        "initial_state": {
            "active": 0,
            "preview": 0,
            "recording": False,
            "streaming": False,
            "external": False,
            "fadeToBlack": False,
            "input_count": 0,
            "version": "",
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

    def handle_command(self, data: bytes) -> bytes | None:
        """
        Parse incoming bytes from the driver, return response bytes.

        Available helpers:
            self.state              — dict of current state values
            self.set_state(k, v)    — update state (triggers UI refresh)
            self.active_errors      — set of currently active error mode names

        Driver commands to handle:
            cut                  — Cut (params: input: string)
            fade                 — Fade (params: input: string, duration: integer)
            cut_direct           — Cut Direct (params: input: string)
            fade_to_black        — Fade to Black
            transition           — Transition (params: input: string, effect: string, duration: integer)
            stinger              — Stinger (params: input: string, index: integer)
            set_fader            — Set T-Bar (params: position: integer)
            preview_input        — Preview Input (params: input: string)
            active_input         — Active Input (Cut) (params: input: string)
            preview_input_next   — Preview Next
            preview_input_previous — Preview Previous
            audio                — Audio Toggle (params: input: string)
            audio_on             — Audio On (params: input: string)
            audio_off            — Audio Off (params: input: string)
            set_volume           — Set Volume (params: input: string, value: integer)
            set_volume_fade      — Set Volume (Fade) (params: input: string, value: integer, duration: integer)
            set_gain             — Set Gain (params: input: string, value: integer)
            set_balance          — Set Balance (params: input: string, value: integer)
            solo                 — Solo (params: input: string)
            bus_audio            — Bus Audio Toggle (params: input: string, value: string)
            bus_audio_on         — Bus Audio On (params: input: string, value: string)
            bus_audio_off        — Bus Audio Off (params: input: string, value: string)
            set_bus_volume       — Set Bus Volume (params: value: string, level: integer)
            master_audio         — Master Audio Toggle
            master_audio_on      — Master Audio On
            master_audio_off     — Master Audio Off
            set_master_volume    — Set Master Volume (params: value: integer)
            overlay_input        — Overlay Toggle (params: input: string, value: integer)
            overlay_input_in     — Overlay In (params: input: string, value: integer)
            overlay_input_out    — Overlay Out (params: value: integer)
            overlay_input_off    — Overlay Off (params: value: integer)
            overlay_input_all_off — All Overlays Off
            start_recording      — Start Recording
            stop_recording       — Stop Recording
            start_streaming      — Start Streaming (params: value: integer)
            stop_streaming       — Stop Streaming (params: value: integer)
            start_external       — Start External
            stop_external        — Stop External
            snapshot             — Snapshot
            snapshot_input       — Snapshot Input (params: input: string)
            set_text             — Set Text (params: input: string, selectedName: string, value: string)
            set_image            — Set Image (params: input: string, selectedName: string, value: string)
            set_countdown        — Set Countdown (params: input: string, value: string)
            start_countdown      — Start Countdown (params: input: string)
            stop_countdown       — Stop Countdown (params: input: string)
            play                 — Play (params: input: string)
            pause                — Pause (params: input: string)
            play_pause           — Play/Pause (params: input: string)
            restart              — Restart (params: input: string)
            loop_on              — Loop On (params: input: string)
            loop_off             — Loop Off (params: input: string)
            set_position         — Set Position (params: input: string, value: integer)
            set_rate             — Set Rate (params: input: string, value: string)
            replay_play          — Replay Play
            replay_pause         — Replay Pause
            replay_mark_in       — Replay Mark In
            replay_mark_out      — Replay Mark Out
            replay_mark_in_out   — Replay Mark In/Out
            replay_live          — Replay Live
            replay_recorded      — Replay Recorded
            replay_set_speed     — Replay Set Speed (params: value: string)
            replay_play_last_event — Replay Last Event
            ptz_move_up          — PTZ Up (params: input: string, value: string)
            ptz_move_down        — PTZ Down (params: input: string, value: string)
            ptz_move_left        — PTZ Left (params: input: string, value: string)
            ptz_move_right       — PTZ Right (params: input: string, value: string)
            ptz_move_stop        — PTZ Stop (params: input: string)
            ptz_zoom_in          — PTZ Zoom In (params: input: string, value: string)
            ptz_zoom_out         — PTZ Zoom Out (params: input: string, value: string)
            ptz_zoom_stop        — PTZ Zoom Stop (params: input: string)
            ptz_home             — PTZ Home (params: input: string)
            ptz_focus_auto       — PTZ Auto Focus (params: input: string)
            add_input            — Add Input (params: value: string)
            remove_input         — Remove Input (params: input: string)
            set_input_name       — Set Input Name (params: input: string, value: string)
            select_index         — Select Index (params: input: string, value: integer)
            next_item            — Next Item (params: input: string)
            previous_item        — Previous Item (params: input: string)
            browser_navigate     — Browser Navigate (params: input: string, value: string)
            script_start         — Script Start (params: value: string)
            script_stop          — Script Stop (params: value: string)
            raw_function         — Raw Function (params: function: string, query: string)

        State variables to maintain:
            active               (integer ) — Program Input
            preview              (integer ) — Preview Input
            recording            (boolean ) — Recording
            streaming            (boolean ) — Streaming
            external             (boolean ) — External Output
            fadeToBlack          (boolean ) — Fade to Black
            input_count          (integer ) — Input Count
            version              (string  ) — vMix Version
        """
        # TODO: Implement protocol parsing and response generation.
        #
        # Example for a text protocol:
        #   text = data.decode().strip()
        #   if text == "POWER ON":
        #       self.set_state("power", "on")
        #       return b"OK\r\n"
        #
        # Example for a binary protocol:
        #   if len(data) >= 4 and data[0] == 0xAA:
        #       cmd = data[1]
        #       if cmd == 0x11:  # Power query
        #           payload = [0x01 if self.state["power"] == "on" else 0x00]
        #           return self._build_response(cmd, payload)

        return None
