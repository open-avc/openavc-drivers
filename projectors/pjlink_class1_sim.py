"""
PJLink Class 1 Projector — Simulator

Full-featured PJLink Class 1 simulator with:
  - MD5 authentication (optional, via config["password"])
  - Power state machine (off → warming → on → cooling → off)
  - Input switching (rejects when power is off)
  - AV mute control
  - Lamp hours tracking
  - Error status reporting
  - Device info queries (name, manufacturer, product, class, inputs)

This is the reference implementation for Python TCP simulators.
"""

import asyncio
import hashlib
import secrets

from simulator.tcp_simulator import TCPSimulator


class PjlinkClass1Simulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "pjlink_class1",
        "name": "PJLink Class 1 Projector Simulator",
        "category": "projector",
        "transport": "tcp",
        "default_port": 4352,
        "delimiter": "\r",
        "initial_state": {
            "power": "off",
            # PJLink input codes: 1x=RGB, 2x=Video, 3x=Digital, 5x=Network
            "input": "31",       # 31 = HDMI 1
            "mute_video": False,
            "mute_audio": False,
            "lamp_hours": 450,
            "error_status": "000000",
            "projector_name": "PJLink Simulator",
            "manufacturer": "OpenAVC",
            "product_name": "Virtual Projector",
        },
        "controls": [
            {"type": "power", "key": "power"},
            {"type": "select", "key": "input",
             "options": ["11", "12", "31", "32", "51"],
             "labels": {"11": "VGA 1", "12": "VGA 2", "31": "HDMI 1", "32": "HDMI 2", "51": "Network"},
             "label": "Input"},
            {"type": "toggle", "key": "mute_video", "label": "Video Mute"},
            {"type": "toggle", "key": "mute_audio", "label": "Audio Mute"},
            {"type": "indicator", "key": "lamp_hours", "label": "Lamp Hours"},
            {"type": "indicator", "key": "error_status", "label": "Error Status"},
        ],
        "delays": {
            "command_response": 0.03,
        },
        "error_modes": {
            "lamp_warning": {
                "description": "Lamp approaching end of life",
                "set_state": {"lamp_hours": 19500},
            },
            "overtemp": {
                "description": "Projector overheating",
                "set_state": {"error_status": "002000"},
            },
            "lamp_failure": {
                "description": "Lamp has failed",
                "set_state": {"error_status": "020000"},
            },
            "filter_clogged": {
                "description": "Air filter needs cleaning",
                "set_state": {"error_status": "000020"},
            },
        },
    }

    # PJLink power states: 0=off, 1=on, 2=cooling, 3=warming
    _POWER_MAP = {"off": 0, "on": 1, "cooling": 2, "warming": 3}
    _POWER_REV = {0: "off", 1: "on", 2: "cooling", 3: "warming"}

    # Available inputs
    _AVAILABLE_INPUTS = ["11", "12", "31", "32", "51"]

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._auth_random = ""
        self._transition_task: asyncio.Task | None = None
        self._warmup_time = 3.0
        self._cooldown_time = 2.0

    async def on_client_connected(self, client_id: str) -> bytes | None:
        """Send PJLink greeting on connect."""
        password = self.config.get("password", "")
        if password:
            self._auth_random = secrets.token_hex(4)
            return f"PJLINK 1 {self._auth_random}\r".encode()
        return b"PJLINK 0\r"

    def handle_command(self, data: bytes) -> bytes | None:
        cmd = data.decode("ascii", errors="ignore").strip()
        if not cmd:
            return None

        # Handle authentication
        password = self.config.get("password", "")
        if password:
            expected = hashlib.md5(
                (self._auth_random + password).encode()
            ).hexdigest()
            if cmd.startswith(expected):
                cmd = cmd[len(expected):]
            else:
                return b"PJLINK ERRA\r"

        if not cmd.startswith("%1"):
            return None

        body = cmd[2:]
        if " " in body:
            code, param = body.split(" ", 1)
        else:
            code, param = body, ""

        code = code.upper()
        response = self._handle_code(code, param)

        if response:
            return response.encode("ascii") + b"\r"
        return None

    def _handle_code(self, code: str, param: str) -> str | None:
        power_int = self._POWER_MAP.get(str(self.state.get("power", "off")), 0)

        # ── Power ──
        if code == "POWR":
            if param == "?":
                return f"%1POWR={power_int}"
            elif param == "1":
                if power_int in (0, 2):  # off or cooling
                    self.set_state("power", "warming")
                    self._schedule_transition("on", self._warmup_time)
                return "%1POWR=OK"
            elif param == "0":
                if power_int in (1, 3):  # on or warming
                    self.set_state("power", "cooling")
                    self._schedule_transition("off", self._cooldown_time)
                return "%1POWR=OK"

        # ── Input (ERR2 when not on) ──
        elif code == "INPT":
            if power_int != 1:
                return "%1INPT=ERR2"
            if param == "?":
                return f"%1INPT={self.state.get('input', '31')}"
            elif param in self._AVAILABLE_INPUTS:
                self.set_state("input", param)
                return "%1INPT=OK"
            else:
                return "%1INPT=ERR2"

        # ── AV Mute (ERR2 when not on) ──
        elif code == "AVMT":
            if power_int != 1:
                return "%1AVMT=ERR2"
            if param == "?":
                mute_code = self._get_mute_code()
                return f"%1AVMT={mute_code}"
            elif param in ("10", "11", "20", "21", "30", "31"):
                self._set_mute_from_code(param)
                return "%1AVMT=OK"

        # ── Lamp ──
        elif code == "LAMP":
            if param == "?":
                lamp_on = 1 if power_int == 1 else 0
                hours = self.state.get("lamp_hours", 0)
                return f"%1LAMP={hours} {lamp_on}"

        # ── Error Status ──
        elif code == "ERST":
            if param == "?":
                return f"%1ERST={self.state.get('error_status', '000000')}"

        # ── Device Info ──
        elif code == "NAME":
            if param == "?":
                return f"%1NAME={self.state.get('projector_name', 'Simulator')}"
        elif code == "INF1":
            if param == "?":
                return f"%1INF1={self.state.get('manufacturer', 'OpenAVC')}"
        elif code == "INF2":
            if param == "?":
                return f"%1INF2={self.state.get('product_name', 'Virtual')}"
        elif code == "CLSS":
            if param == "?":
                return "%1CLSS=1"
        elif code == "INST":
            if param == "?":
                return f"%1INST={' '.join(self._AVAILABLE_INPUTS)}"

        return f"%1{code}=ERR1"

    def _get_mute_code(self) -> str:
        mv = self.state.get("mute_video", False)
        ma = self.state.get("mute_audio", False)
        if mv and ma:
            return "31"
        elif mv:
            return "11"
        elif ma:
            return "21"
        return "30"

    def _set_mute_from_code(self, code: str) -> None:
        if code == "11":
            self.set_state("mute_video", True)
        elif code == "10":
            self.set_state("mute_video", False)
        elif code == "21":
            self.set_state("mute_audio", True)
        elif code == "20":
            self.set_state("mute_audio", False)
        elif code == "31":
            self.set_state("mute_video", True)
            self.set_state("mute_audio", True)
        elif code == "30":
            self.set_state("mute_video", False)
            self.set_state("mute_audio", False)

    def _schedule_transition(self, target: str, delay: float) -> None:
        if self._transition_task and not self._transition_task.done():
            self._transition_task.cancel()
        self._transition_task = asyncio.ensure_future(
            self._do_transition(target, delay)
        )

    async def _do_transition(self, target: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self.set_state("power", target)
