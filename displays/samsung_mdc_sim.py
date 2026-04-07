"""
Samsung MDC Display — Simulator
Auto-generated skeleton. Fill in the handler method with protocol logic.

Driver: samsung_mdc
Transport: tcp
"""
from simulator.tcp_simulator import TCPSimulator


class SamsungMdcSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "samsung_mdc",
        "name": "Samsung MDC Display Simulator",
        "category": "display",
        "transport": "tcp",
        "default_port": 1515,
        "initial_state": {
            "power": "off",
            "volume": 0,
            "mute": False,
            "input": "off",
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
            volume               — Volume (params: mute: boolean, input: enum)
            mute                 — Mute (params: input: enum)
            input                — Input Source
            commands             — Power On (params: set_volume: integer)
            power_off            — Power Off (params: set_volume: integer)
            set_volume           — Set Volume (params: params: integer)
            mute_on              — Mute On (params: set_input: enum)
            mute_off             — Mute Off (params: set_input: enum)
            set_input            — Set Input (params: params: enum)

        State variables to maintain:
            power                (enum    ) — Power State
            volume               (integer ) — Volume
            mute                 (boolean ) — Mute
            input                (enum    ) — Input Source
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
