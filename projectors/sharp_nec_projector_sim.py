"""
Sharp NEC Projector — Simulator

Full NEC binary control protocol simulator with:
  - Power state machine (off -> on, on -> cooling -> off)
  - Input source switching (computer, hdmi1, hdmi2, displayport, hdbaset, sdi)
  - Picture mute, sound mute, OSD mute
  - Freeze control
  - Shutter control
  - Volume, brightness, contrast adjustment
  - Eco mode
  - Lamp hours and filter hours tracking
  - Model name and serial number queries
  - Error status reporting
  - Basic info (305-3) query returning full state

Protocol reference:
  NEC Projector Control Command Reference Manual (BDT140013 Rev 7.1)

Packet format:
    Request:  [HEADER] [CMD] [0x00] [0x00] [LEN] [DATA...] [CHECKSUM]
    Response: [HEADER+0x20] [CMD] [0x00] [0x00] [LEN] [DATA...] [CHECKSUM]
    Checksum = sum of all preceding bytes & 0xFF

Header types:
    00h = Status queries   -> success: 20h, error: A0h
    01h = Freeze control   -> success: 21h, error: A1h
    02h = Control commands -> success: 22h, error: A2h
    03h = Adjust/query     -> success: 23h, error: A3h
"""

import asyncio

from simulator.tcp_simulator import TCPSimulator


# Command bytes (matching the driver)
CMD_POWER_ON = 0x00
CMD_POWER_OFF = 0x01
CMD_INPUT_SELECT = 0x03
CMD_PICTURE_MUTE_ON = 0x10
CMD_PICTURE_MUTE_OFF = 0x11
CMD_SOUND_MUTE_ON = 0x12
CMD_SOUND_MUTE_OFF = 0x13
CMD_OSD_MUTE_ON = 0x14
CMD_OSD_MUTE_OFF = 0x15
CMD_SHUTTER_CLOSE = 0x16
CMD_SHUTTER_OPEN = 0x17
CMD_FREEZE = 0x98
CMD_ERROR_STATUS = 0x88
CMD_STATUS_78 = 0x85
CMD_BASIC_INFO = 0xBF
CMD_GAIN_REQ = 0x05
CMD_ADJUST = 0x10
CMD_FILTER_INFO = 0x95
CMD_LAMP_INFO = 0x96
CMD_ECO_REQ = 0xB0
CMD_ECO_SET = 0xB1

# Input source codes (matching the driver)
INPUT_CODES = {
    "computer": 0x01,
    "hdmi1": 0xA1,
    "hdmi2": 0xA2,
    "displayport": 0xA6,
    "hdbaset": 0xBF,
    "sdi": 0xBE,
    "dvi-d": 0x20,
    "video": 0x06,
    "s-video": 0x0B,
    "component": 0x10,
    "usb_a": 0x1F,
    "lan": 0x20,
}
INPUT_REVERSE = {v: k for v, k in {
    0x01: "computer",
    0xA1: "hdmi1",
    0xA2: "hdmi2",
    0xA6: "displayport",
    0xBF: "hdbaset",
    0xBE: "sdi",
    0x20: "lan",
    0x06: "video",
    0x0B: "s-video",
    0x10: "component",
    0x1F: "usb_a",
}.items()}

# Power status codes for the 305-3 response
POWER_CODES = {
    "off": 0x00,
    "on": 0x04,
    "cooling": 0x05,
    "network_standby": 0x10,
}

# Signal type 2 mapping for BASIC INFO response
INPUT_TO_SIGNAL_TYPE_2 = {
    "computer": 0x01,
    "video": 0x02,
    "s-video": 0x03,
    "component": 0x04,
    "lan": 0x20,
    "dvi-d": 0x20,
    "hdmi1": 0x21,
    "hdmi2": 0x21,
    "displayport": 0x22,
    "usb_a": 0x23,
    "hdbaset": 0x21,
    "sdi": 0x21,
}

# Signal type 1 (input index) for BASIC INFO
INPUT_TO_SIGNAL_TYPE_1 = {
    "computer": 0x01,
    "hdmi1": 0x01,
    "hdmi2": 0x02,
    "displayport": 0x01,
    "hdbaset": 0x01,
    "sdi": 0x01,
    "dvi-d": 0x01,
    "video": 0x01,
    "s-video": 0x01,
    "component": 0x01,
    "usb_a": 0x01,
    "lan": 0x01,
}


def _checksum(data: bytes) -> int:
    """NEC checksum: sum of all bytes & 0xFF."""
    return sum(data) & 0xFF


class SharpNecProjectorSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "sharp_nec_projector",
        "name": "Sharp NEC Projector Simulator",
        "category": "projector",
        "transport": "tcp",
        "default_port": 7142,
        "initial_state": {
            "power": "off",
            "input": "hdmi1",
            "picture_mute": False,
            "sound_mute": False,
            "onscreen_mute": False,
            "freeze": False,
            "shutter": False,
            "volume": 50,
            "brightness": 50,
            "contrast": 50,
            "eco_mode": "0",
            "lamp_hours": 1250,
            "lamp_life_remaining": 87,
            "filter_hours": 650,
            "model_name": "NP-PX2201QL",
            "serial_number": "SIM00001",
            "error_status": "ok",
        },
        "delays": {
            "command_response": 0.03,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Projector stops responding to commands",
                "behavior": "no_response",
            },
            "corrupt_response": {
                "description": "Corrupted data on the wire",
                "behavior": "corrupt_response",
            },
            "lamp_warning": {
                "description": "Lamp approaching end of life",
                "set_state": {"lamp_hours": 19500, "lamp_life_remaining": 3},
            },
        },
        "controls": [
            {
                "type": "power",
                "key": "power",
            },
            {
                "type": "select",
                "key": "input",
                "label": "Input Source",
                "options": [
                    "hdmi1", "hdmi2", "computer", "displayport",
                    "hdbaset", "sdi",
                ],
                "labels": {
                    "hdmi1": "HDMI 1",
                    "hdmi2": "HDMI 2",
                    "computer": "Computer",
                    "displayport": "DisplayPort",
                    "hdbaset": "HDBaseT",
                    "sdi": "SDI",
                },
            },
            {
                "type": "slider",
                "key": "volume",
                "label": "Volume",
                "min": 0,
                "max": 63,
            },
            {
                "type": "group",
                "label": "Mute Controls",
                "controls": [
                    {"type": "toggle", "key": "picture_mute", "label": "Picture Mute"},
                    {"type": "toggle", "key": "sound_mute", "label": "Sound Mute"},
                    {"type": "toggle", "key": "onscreen_mute", "label": "OSD Mute"},
                ],
            },
            {
                "type": "group",
                "label": "Display Controls",
                "controls": [
                    {"type": "toggle", "key": "freeze", "label": "Freeze"},
                    {"type": "toggle", "key": "shutter", "label": "Shutter Closed"},
                ],
            },
            {
                "type": "slider",
                "key": "brightness",
                "label": "Brightness",
                "min": 0,
                "max": 100,
            },
            {
                "type": "slider",
                "key": "contrast",
                "label": "Contrast",
                "min": 0,
                "max": 100,
            },
            {
                "type": "group",
                "label": "Lamp / Filter",
                "controls": [
                    {
                        "type": "indicator",
                        "key": "lamp_hours",
                        "label": "Lamp Hours",
                    },
                    {
                        "type": "indicator",
                        "key": "lamp_life_remaining",
                        "label": "Lamp Life %",
                    },
                    {
                        "type": "indicator",
                        "key": "filter_hours",
                        "label": "Filter Hours",
                    },
                ],
            },
            {
                "type": "indicator",
                "key": "model_name",
                "label": "Model",
            },
            {
                "type": "indicator",
                "key": "serial_number",
                "label": "Serial",
            },
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._transition_task: asyncio.Task | None = None
        self._cooldown_time = 2.0

    def _build_response(
        self, req_header: int, cmd: int, data: bytes = b""
    ) -> bytes:
        """Build a NEC success response packet.

        Success header = request header + 0x20.
        Format: [HEADER+0x20] [CMD] [0x00] [0x00] [LEN] [DATA...] [CHECKSUM]
        """
        resp_header = req_header + 0x20
        body = bytes([resp_header, cmd, 0x00, 0x00, len(data)]) + data
        cs = _checksum(body)
        return body + bytes([cs])

    def _build_error(
        self, req_header: int, cmd: int, err1: int = 0x00, err2: int = 0x00
    ) -> bytes:
        """Build a NEC error response packet.

        Error header = request header + 0xA0.
        """
        err_header = req_header + 0xA0
        data = bytes([err1, err2])
        body = bytes([err_header, cmd, 0x00, 0x00, len(data)]) + data
        cs = _checksum(body)
        return body + bytes([cs])

    def handle_command(self, data: bytes) -> bytes | None:
        """Parse incoming NEC binary frames and return responses.

        Request format: [HEADER] [CMD] [0x00] [0x00] [LEN] [DATA...] [CS]
        Minimum packet size is 6 bytes (header + cmd + 2 reserved + len + cs).
        """
        response_buf = b""
        buf = data

        while len(buf) >= 6:
            # Valid request headers are 0x00, 0x01, 0x02, 0x03
            header = buf[0]
            if header not in (0x00, 0x01, 0x02, 0x03):
                buf = buf[1:]
                continue

            cmd = buf[1]
            # bytes [2] and [3] are reserved (0x00)
            data_len = buf[4]
            total_len = 5 + data_len + 1  # header+cmd+00+00+len + data + checksum

            if len(buf) < total_len:
                break

            payload = buf[5 : 5 + data_len]
            buf = buf[total_len:]

            resp = self._process_command(header, cmd, payload)
            if resp:
                response_buf += resp

        return response_buf if response_buf else None

    def _process_command(
        self, header: int, cmd: int, payload: bytes
    ) -> bytes | None:
        """Route a parsed command to the appropriate handler."""

        # Header 0x02 — Control commands
        if header == 0x02:
            return self._handle_control(cmd, payload)

        # Header 0x01 — Freeze control
        elif header == 0x01:
            return self._handle_freeze(cmd, payload)

        # Header 0x00 — Status queries
        elif header == 0x00:
            return self._handle_status(cmd, payload)

        # Header 0x03 — Adjust/query
        elif header == 0x03:
            return self._handle_adjust(cmd, payload)

        return None

    # ── Header 0x02: Control commands ──

    def _handle_control(self, cmd: int, payload: bytes) -> bytes | None:
        power = self.state.get("power", "off")

        # Power on
        if cmd == CMD_POWER_ON:
            if power in ("off", "network_standby"):
                self.set_state("power", "on")
            return self._build_response(0x02, cmd)

        # Power off
        elif cmd == CMD_POWER_OFF:
            if power == "on":
                self.set_state("power", "cooling")
                self._schedule_cooldown()
            return self._build_response(0x02, cmd)

        # Input select
        elif cmd == CMD_INPUT_SELECT:
            if power != "on":
                # ERR1=02 ERR2=0D: command rejected while power off
                return self._build_error(0x02, cmd, 0x02, 0x0D)
            if len(payload) >= 2:
                input_code = payload[1]
                input_name = INPUT_REVERSE.get(input_code)
                if input_name:
                    self.set_state("input", input_name)
                    return self._build_response(0x02, cmd, bytes([0x00]))
                else:
                    # Unsupported input: ERR1=01, ERR2=01
                    return self._build_error(0x02, cmd, 0x01, 0x01)
            return self._build_response(0x02, cmd)

        # Picture mute on/off
        elif cmd == CMD_PICTURE_MUTE_ON:
            if power != "on":
                return self._build_error(0x02, cmd, 0x02, 0x0D)
            self.set_state("picture_mute", True)
            return self._build_response(0x02, cmd)

        elif cmd == CMD_PICTURE_MUTE_OFF:
            if power != "on":
                return self._build_error(0x02, cmd, 0x02, 0x0D)
            self.set_state("picture_mute", False)
            return self._build_response(0x02, cmd)

        # Sound mute on/off
        elif cmd == CMD_SOUND_MUTE_ON:
            if power != "on":
                return self._build_error(0x02, cmd, 0x02, 0x0D)
            self.set_state("sound_mute", True)
            return self._build_response(0x02, cmd)

        elif cmd == CMD_SOUND_MUTE_OFF:
            if power != "on":
                return self._build_error(0x02, cmd, 0x02, 0x0D)
            self.set_state("sound_mute", False)
            return self._build_response(0x02, cmd)

        # OSD mute on/off
        elif cmd == CMD_OSD_MUTE_ON:
            if power != "on":
                return self._build_error(0x02, cmd, 0x02, 0x0D)
            self.set_state("onscreen_mute", True)
            return self._build_response(0x02, cmd)

        elif cmd == CMD_OSD_MUTE_OFF:
            if power != "on":
                return self._build_error(0x02, cmd, 0x02, 0x0D)
            self.set_state("onscreen_mute", False)
            return self._build_response(0x02, cmd)

        # Shutter close/open
        elif cmd == CMD_SHUTTER_CLOSE:
            if power != "on":
                return self._build_error(0x02, cmd, 0x02, 0x0D)
            self.set_state("shutter", True)
            return self._build_response(0x02, cmd)

        elif cmd == CMD_SHUTTER_OPEN:
            if power != "on":
                return self._build_error(0x02, cmd, 0x02, 0x0D)
            self.set_state("shutter", False)
            return self._build_response(0x02, cmd)

        # Lens control (acknowledge but no physical effect in sim)
        elif cmd in (0x18, 0x1C, 0x1D, 0x1E):
            return self._build_response(0x02, cmd)

        # Remote key (acknowledge)
        elif cmd == 0x0F:
            return self._build_response(0x02, cmd)

        return self._build_response(0x02, cmd)

    # ── Header 0x01: Freeze control ──

    def _handle_freeze(self, cmd: int, payload: bytes) -> bytes | None:
        power = self.state.get("power", "off")

        if cmd == CMD_FREEZE:
            if power != "on":
                return self._build_error(0x01, cmd, 0x02, 0x0D)
            if len(payload) >= 1:
                if payload[0] == 0x01:
                    self.set_state("freeze", True)
                elif payload[0] == 0x02:
                    self.set_state("freeze", False)
                return self._build_response(0x01, cmd, bytes([0x00]))
            return self._build_response(0x01, cmd)

        return self._build_response(0x01, cmd)

    # ── Header 0x00: Status queries ──

    def _handle_status(self, cmd: int, payload: bytes) -> bytes | None:

        # 305 — Basic info / serial
        if cmd == CMD_BASIC_INFO:
            if not payload:
                return self._build_response(0x00, cmd)

            sub = payload[0]

            # 305-3 BASIC INFORMATION (sub=0x02)
            if sub == 0x02:
                return self._build_basic_info_response()

            # 305-2 Serial number query (sub=0x01, content=0x06)
            elif sub == 0x01:
                serial = self.state.get("serial_number", "SIM00001")
                serial_bytes = serial.encode("ascii")
                # Response: [0x01, 0x06, ...serial bytes...]
                resp_data = bytes([0x01, 0x06]) + serial_bytes
                return self._build_response(0x00, cmd, resp_data)

            # 305-1 Base model type (sub=0x00)
            elif sub == 0x00:
                return self._build_response(0x00, cmd, bytes([0x00]))

            return self._build_response(0x00, cmd, payload[:1])

        # 078-5 Model name
        elif cmd == CMD_STATUS_78:
            if payload and payload[0] == 0x04:
                model = self.state.get("model_name", "NP-PX2201QL")
                # Model name as NUL-padded ASCII (32 bytes typical)
                model_bytes = model.encode("ascii").ljust(32, b"\x00")
                return self._build_response(0x00, cmd, model_bytes)
            return self._build_response(0x00, cmd)

        # 009 Error status
        elif cmd == CMD_ERROR_STATUS:
            # Return 12 bytes of error bit fields (all clear by default)
            error_data = bytearray(12)
            error_str = self.state.get("error_status", "ok")
            if error_str != "ok" and error_str:
                # Set bits based on active error descriptions
                issues = error_str.split(", ")
                for issue in issues:
                    if issue == "cover":
                        error_data[0] |= 0x01
                    elif issue == "temperature":
                        error_data[0] |= 0x02
                    elif issue == "fan":
                        error_data[0] |= 0x08
                    elif issue == "power":
                        error_data[0] |= 0x20
                    elif issue == "lamp_off":
                        error_data[0] |= 0x40
                    elif issue == "lamp_replace":
                        error_data[0] |= 0x80
                    elif issue == "lamp_hours_exceeded":
                        error_data[1] |= 0x01
            return self._build_response(0x00, cmd, bytes(error_data))

        return self._build_response(0x00, cmd)

    def _build_basic_info_response(self) -> bytes:
        """Build the 305-3 BASIC INFORMATION response.

        Response data layout:
          [0] = 0x02 (sub-request echo)
          [1] = Operation status (power)
          [2] = Content displayed
          [3] = Selection signal type 1
          [4] = Selection signal type 2
          [5] = Display signal type
          [6] = Video mute (00=off, 01=on)
          [7] = Sound mute (00=off, 01=on)
          [8] = Onscreen mute (00=off, 01=on)
          [9] = Freeze status (00=off, 01=on)
        """
        power = self.state.get("power", "off")
        input_name = self.state.get("input", "hdmi1")

        power_code = POWER_CODES.get(power, 0x00)
        sig_type_1 = INPUT_TO_SIGNAL_TYPE_1.get(input_name, 0x01)
        sig_type_2 = INPUT_TO_SIGNAL_TYPE_2.get(input_name, 0x21)

        # Content displayed: 0x01 when on and showing content, 0x00 when off
        content = 0x01 if power == "on" else 0x00
        # Display signal type mirrors sig_type_2 when on
        display_sig = sig_type_2 if power == "on" else 0xFF

        resp_data = bytes([
            0x02,                                              # sub-request echo
            power_code,                                        # operation status
            content,                                           # content displayed
            sig_type_1,                                        # signal type 1
            sig_type_2,                                        # signal type 2
            display_sig,                                       # display signal type
            0x01 if self.state.get("picture_mute") else 0x00,  # video mute
            0x01 if self.state.get("sound_mute") else 0x00,    # sound mute
            0x01 if self.state.get("onscreen_mute") else 0x00, # onscreen mute
            0x01 if self.state.get("freeze") else 0x00,        # freeze
        ])
        return self._build_response(0x00, CMD_BASIC_INFO, resp_data)

    # ── Header 0x03: Adjust/query ──

    def _handle_adjust(self, cmd: int, payload: bytes) -> bytes | None:

        # 037-4 Lamp info
        if cmd == CMD_LAMP_INFO:
            if len(payload) >= 2:
                content = payload[1]

                # Content 0x01: lamp usage time (seconds, 4 bytes LE)
                if content == 0x01:
                    hours = self.state.get("lamp_hours", 1250)
                    seconds = hours * 3600
                    time_bytes = seconds.to_bytes(4, "little")
                    # Response: [content_echo, content, ...4 bytes time...]
                    resp_data = bytes([payload[0], content]) + time_bytes
                    return self._build_response(0x03, cmd, resp_data)

                # Content 0x04: lamp remaining life (percentage)
                elif content == 0x04:
                    remaining = self.state.get("lamp_life_remaining", 87)
                    remaining_bytes = remaining.to_bytes(4, "little")
                    resp_data = bytes([payload[0], content]) + remaining_bytes
                    return self._build_response(0x03, cmd, resp_data)

            return self._build_response(0x03, cmd)

        # 037-3 Filter info (4 bytes LE seconds)
        elif cmd == CMD_FILTER_INFO:
            hours = self.state.get("filter_hours", 650)
            seconds = hours * 3600
            time_bytes = seconds.to_bytes(4, "little")
            return self._build_response(0x03, cmd, time_bytes)

        # 060-1 Gain request (brightness, contrast, volume read)
        elif cmd == CMD_GAIN_REQ:
            if len(payload) >= 3:
                target = payload[0]
                # target: 0x00=brightness, 0x01=contrast, 0x05=volume
                if target == 0x00:
                    value = self.state.get("brightness", 50)
                elif target == 0x01:
                    value = self.state.get("contrast", 50)
                elif target == 0x05:
                    value = self.state.get("volume", 50)
                else:
                    value = 0

                # Response: 9 bytes
                # [0] = status (0x00 = exists)
                # [1-2] = default value (LE)
                # [3-4] = min value (LE)
                # [5-6] = max value (LE)
                # [7-8] = current value (LE)
                max_val = 63 if target == 0x05 else 100
                resp_data = bytes([
                    0x00,                        # status: gain exists
                    50, 0x00,                    # default
                    0x00, 0x00,                  # min
                    max_val, 0x00,               # max
                    value & 0xFF, (value >> 8) & 0xFF,  # current
                ])
                return self._build_response(0x03, cmd, resp_data)

            return self._build_response(0x03, cmd)

        # 030-x Adjust commands (volume, brightness, contrast, aspect, etc.)
        elif cmd == CMD_ADJUST:
            if len(payload) >= 5:
                target = payload[0]
                mode = payload[2]  # 0x00=absolute, 0x01=relative
                value = payload[3] | (payload[4] << 8)

                if target == 0x05:
                    # Volume
                    level = max(0, min(63, value))
                    self.set_state("volume", level)
                elif target == 0x00:
                    # Brightness
                    level = max(0, min(100, value))
                    self.set_state("brightness", level)
                elif target == 0x01:
                    # Contrast
                    level = max(0, min(100, value))
                    self.set_state("contrast", level)
                # target 0x18 = aspect (no state to track in sim)

                # ACK: [0x00, 0x00] = success
                return self._build_response(0x03, cmd, bytes([0x00, 0x00]))

            return self._build_response(0x03, cmd, bytes([0x00, 0x00]))

        # 097-8 Eco mode read
        elif cmd == CMD_ECO_REQ:
            if payload and payload[0] == 0x07:
                eco = int(self.state.get("eco_mode", "0"))
                return self._build_response(
                    0x03, cmd, bytes([0x07, eco & 0xFF])
                )
            return self._build_response(0x03, cmd)

        # 098-8 Eco mode set
        elif cmd == CMD_ECO_SET:
            if len(payload) >= 2 and payload[0] == 0x07:
                self.set_state("eco_mode", str(payload[1]))
                return self._build_response(
                    0x03, cmd, bytes([0x07, 0x00])
                )
            return self._build_response(0x03, cmd)

        return self._build_response(0x03, cmd)

    # ── Power state machine ──

    def _schedule_cooldown(self) -> None:
        """Schedule transition from cooling -> off after cooldown period."""
        if self._transition_task and not self._transition_task.done():
            self._transition_task.cancel()
        self._transition_task = asyncio.ensure_future(
            self._do_cooldown()
        )

    async def _do_cooldown(self) -> None:
        """Wait for cooldown period, then set power to off."""
        await asyncio.sleep(self._cooldown_time)
        self.set_state("power", "off")
