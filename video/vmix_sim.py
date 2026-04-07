"""
vMix Video Production Software — Simulator

Full-featured vMix TCP API simulator with:
  - FUNCTION command handling (Cut, Fade, PreviewInput, etc.)
  - XML state query with length-prefixed response
  - TALLY subscription with real-time push updates
  - ACTS subscription acknowledgment
  - Recording, streaming, external output, fade-to-black state
  - Input tracking (program, preview, per-input tally)
  - Overlay channel management
  - Audio state tracking
  - Controls schema for Simulator UI

Protocol: TCP text on port 8099.
  Commands:  FUNCTION <name> <params>\r\n
  Responses: FUNCTION OK\r\n  or  FUNCTION <n> ER <msg>\r\n
  XML query: XML\r\n -> XML <length>\r\n<xml_body>
  Tally sub: SUBSCRIBE TALLY\r\n -> TALLY OK <tally_string>\r\n (push)
"""

import asyncio
import xml.etree.ElementTree as ET

from simulator.tcp_simulator import TCPSimulator


class VmixSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "vmix",
        "name": "vMix Simulator",
        "category": "video",
        "transport": "tcp",
        "default_port": 8099,
        "delimiter": "\r\n",
        "initial_state": {
            "active": 1,
            "preview": 2,
            "recording": False,
            "streaming": False,
            "external": False,
            "fadeToBlack": False,
            "input_count": 4,
            "version": "27.0.0.48",
        },
        "delays": {
            "command_response": 0.02,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "vMix stops responding to commands",
                "behavior": "no_response",
            },
        },
        "controls": [
            {
                "type": "select",
                "state_key": "active",
                "label": "Program Input",
                "options": [
                    {"label": "Input 1", "value": 1},
                    {"label": "Input 2", "value": 2},
                    {"label": "Input 3", "value": 3},
                    {"label": "Input 4", "value": 4},
                ],
            },
            {
                "type": "select",
                "state_key": "preview",
                "label": "Preview Input",
                "options": [
                    {"label": "Input 1", "value": 1},
                    {"label": "Input 2", "value": 2},
                    {"label": "Input 3", "value": 3},
                    {"label": "Input 4", "value": 4},
                ],
            },
            {
                "type": "toggle",
                "state_key": "recording",
                "label": "Recording",
            },
            {
                "type": "toggle",
                "state_key": "streaming",
                "label": "Streaming",
            },
            {
                "type": "toggle",
                "state_key": "external",
                "label": "External Output",
            },
            {
                "type": "toggle",
                "state_key": "fadeToBlack",
                "label": "Fade to Black",
            },
        ],
    }

    # Input names for the simulated production
    _INPUT_NAMES = {
        1: "Camera 1",
        2: "Camera 2",
        3: "Slides",
        4: "Lower Third",
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._tally_subscribers: set[str] = set()
        self._acts_subscribers: set[str] = set()
        # Per-input audio state
        self._input_audio: dict[int, dict] = {}
        for i in range(1, 5):
            self._input_audio[i] = {
                "muted": False,
                "volume": 100,
                "solo": False,
            }
        self._master_audio = True
        self._master_volume = 100
        # Overlay state: channel -> input number (0 = off)
        self._overlays: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}
        # Error counter for generating error responses
        self._error_counter = 0

    def handle_command(self, data: bytes) -> bytes | None:
        """Parse a vMix TCP command and return the response."""
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return None

        # XML state request
        if text == "XML":
            return self._build_xml_response()

        # Subscription commands
        if text.startswith("SUBSCRIBE"):
            return self._handle_subscribe(text)
        if text.startswith("UNSUBSCRIBE"):
            return self._handle_unsubscribe(text)

        # FUNCTION commands
        if text.startswith("FUNCTION"):
            return self._handle_function(text)

        # VERSION query
        if text == "VERSION":
            version = self.state.get("version", "27.0.0.48")
            return f"VERSION OK {version}\r\n".encode("utf-8")

        return None

    # ── FUNCTION command handling ──

    def _handle_function(self, text: str) -> bytes:
        """
        Parse and execute a FUNCTION command.

        Format: FUNCTION <FunctionName> <Param1=Value1&Param2=Value2>
        Response: FUNCTION OK\r\n  or  FUNCTION 0 ER <message>\r\n
        """
        parts = text.split(" ", 2)
        if len(parts) < 2:
            return b"FUNCTION 0 ER Invalid command\r\n"

        func_name = parts[1]
        query_str = parts[2] if len(parts) > 2 else ""

        # Parse query parameters (Key=Value&Key2=Value2)
        params = {}
        if query_str:
            for pair in query_str.split("&"):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    params[key] = value

        result = self._execute_function(func_name, params)
        if result is True:
            return b"FUNCTION OK\r\n"
        elif isinstance(result, str):
            # Error message
            self._error_counter += 1
            return f"FUNCTION {self._error_counter} ER {result}\r\n".encode("utf-8")
        else:
            return b"FUNCTION OK\r\n"

    def _execute_function(self, func_name: str, params: dict) -> bool | str:
        """
        Execute a vMix function. Returns True on success, or an error message string.
        Updates simulator state as appropriate.
        """
        input_num = self._resolve_input(params.get("Input"))
        input_count = self.state.get("input_count", 4)

        # ── Transitions ──

        if func_name == "Cut":
            # Cut to specified input (or current preview if no input given)
            target = input_num if input_num else self.state.get("preview", 1)
            if target < 1 or target > input_count:
                return f"Input {target} does not exist"
            old_active = self.state.get("active", 1)
            self.set_state("active", target)
            # Move old program to preview
            self.set_state("preview", old_active)
            self._push_tally()
            return True

        if func_name == "Fade":
            target = input_num if input_num else self.state.get("preview", 1)
            if target < 1 or target > input_count:
                return f"Input {target} does not exist"
            old_active = self.state.get("active", 1)
            self.set_state("active", target)
            self.set_state("preview", old_active)
            self._push_tally()
            return True

        if func_name == "CutDirect":
            if not input_num:
                return "Input parameter required"
            if input_num < 1 or input_num > input_count:
                return f"Input {input_num} does not exist"
            self.set_state("active", input_num)
            self._push_tally()
            return True

        if func_name == "FadeToBlack":
            current = self.state.get("fadeToBlack", False)
            self.set_state("fadeToBlack", not current)
            return True

        if func_name in ("Transition", "Stinger"):
            target = input_num if input_num else self.state.get("preview", 1)
            if target < 1 or target > input_count:
                return f"Input {target} does not exist"
            old_active = self.state.get("active", 1)
            self.set_state("active", target)
            self.set_state("preview", old_active)
            self._push_tally()
            return True

        if func_name == "SetFader":
            # T-bar position — just acknowledge
            return True

        # ── Input Switching ──

        if func_name == "PreviewInput":
            if not input_num:
                return "Input parameter required"
            if input_num < 1 or input_num > input_count:
                return f"Input {input_num} does not exist"
            self.set_state("preview", input_num)
            self._push_tally()
            return True

        if func_name == "ActiveInput":
            if not input_num:
                return "Input parameter required"
            if input_num < 1 or input_num > input_count:
                return f"Input {input_num} does not exist"
            self.set_state("active", input_num)
            self._push_tally()
            return True

        if func_name == "PreviewInputNext":
            current = self.state.get("preview", 1)
            next_input = current + 1 if current < input_count else 1
            self.set_state("preview", next_input)
            self._push_tally()
            return True

        if func_name == "PreviewInputPrevious":
            current = self.state.get("preview", 1)
            prev_input = current - 1 if current > 1 else input_count
            self.set_state("preview", prev_input)
            self._push_tally()
            return True

        # ── Audio ──

        if func_name == "Audio":
            if input_num and input_num in self._input_audio:
                self._input_audio[input_num]["muted"] = not self._input_audio[input_num]["muted"]
            return True

        if func_name == "AudioOn":
            if input_num and input_num in self._input_audio:
                self._input_audio[input_num]["muted"] = False
            return True

        if func_name == "AudioOff":
            if input_num and input_num in self._input_audio:
                self._input_audio[input_num]["muted"] = True
            return True

        if func_name == "SetVolume":
            if input_num and input_num in self._input_audio:
                try:
                    vol = int(params.get("Value", 100))
                    self._input_audio[input_num]["volume"] = max(0, min(100, vol))
                except ValueError:
                    pass
            return True

        if func_name == "SetVolumeFade":
            # Fade to volume — just set it immediately in the simulator
            if input_num and input_num in self._input_audio:
                try:
                    vol = int(params.get("Value", 100))
                    self._input_audio[input_num]["volume"] = max(0, min(100, vol))
                except ValueError:
                    pass
            return True

        if func_name == "SetGain":
            # Acknowledge gain changes
            return True

        if func_name == "SetBalance":
            # Acknowledge balance changes
            return True

        if func_name == "Solo":
            if input_num and input_num in self._input_audio:
                self._input_audio[input_num]["solo"] = not self._input_audio[input_num]["solo"]
            return True

        # Bus audio commands (BusAAudio, BusBAudioOn, etc.)
        if func_name.startswith("Bus") and "Audio" in func_name:
            return True

        if func_name.startswith("SetBus") and "Volume" in func_name:
            return True

        if func_name == "MasterAudio":
            self._master_audio = not self._master_audio
            return True

        if func_name == "MasterAudioOn":
            self._master_audio = True
            return True

        if func_name == "MasterAudioOff":
            self._master_audio = False
            return True

        if func_name == "SetMasterVolume":
            try:
                vol = int(params.get("Value", 100))
                self._master_volume = max(0, min(100, vol))
            except ValueError:
                pass
            return True

        # ── Overlays ──

        if func_name == "OverlayInput":
            channel = self._resolve_int(params.get("Value"))
            if channel and 1 <= channel <= 4 and input_num:
                if self._overlays.get(channel) == input_num:
                    self._overlays[channel] = 0  # Toggle off
                else:
                    self._overlays[channel] = input_num
            return True

        if func_name == "OverlayInputIn":
            channel = self._resolve_int(params.get("Value"))
            if channel and 1 <= channel <= 4 and input_num:
                self._overlays[channel] = input_num
            return True

        if func_name == "OverlayInputOut":
            channel = self._resolve_int(params.get("Value"))
            if channel and 1 <= channel <= 4:
                self._overlays[channel] = 0
            return True

        if func_name == "OverlayInputOff":
            channel = self._resolve_int(params.get("Value"))
            if channel and 1 <= channel <= 4:
                self._overlays[channel] = 0
            return True

        if func_name == "OverlayInputAllOff":
            for ch in self._overlays:
                self._overlays[ch] = 0
            return True

        # ── Recording / Streaming / External ──

        if func_name == "StartRecording":
            self.set_state("recording", True)
            return True

        if func_name == "StopRecording":
            self.set_state("recording", False)
            return True

        if func_name == "StartStreaming":
            self.set_state("streaming", True)
            return True

        if func_name == "StopStreaming":
            self.set_state("streaming", False)
            return True

        if func_name == "StartExternal":
            self.set_state("external", True)
            return True

        if func_name == "StopExternal":
            self.set_state("external", False)
            return True

        # ── Snapshot / Titles / Countdown / Playback / Replay / PTZ ──
        # These are all fire-and-forget commands in vMix. Accept them silently.

        if func_name in (
            "Snapshot", "SnapshotInput",
            "SetText", "SetImage", "SetCountdown",
            "StartCountdown", "StopCountdown",
            "Play", "Pause", "PlayPause", "Restart",
            "LoopOn", "LoopOff", "SetPosition", "SetRate",
            "ReplayPlay", "ReplayPause",
            "ReplayMarkIn", "ReplayMarkOut", "ReplayMarkInOut",
            "ReplayLive", "ReplayRecorded", "ReplaySetSpeed",
            "ReplayPlayLastEvent",
            "PTZMoveUp", "PTZMoveDown", "PTZMoveLeft", "PTZMoveRight",
            "PTZMoveStop", "PTZZoomIn", "PTZZoomOut", "PTZZoomStop",
            "PTZHome", "PTZFocusAuto",
            "AddInput", "RemoveInput", "SetInputName",
            "SelectIndex", "NextItem", "PreviousItem",
            "BrowserNavigate", "ScriptStart", "ScriptStop",
        ):
            return True

        # Unknown function — still return OK (vMix is permissive)
        return True

    # ── Subscriptions ──

    def _handle_subscribe(self, text: str) -> bytes:
        """Handle SUBSCRIBE commands."""
        parts = text.split()
        if len(parts) < 2:
            return b"SUBSCRIBE OK\r\n"

        topic = parts[1].upper()

        if topic == "TALLY":
            # Store that this connection wants tally pushes.
            # Since TCPSimulator broadcasts via push(), we just
            # need to track that at least one client is subscribed.
            self._tally_subscribers.add("active")
            tally_str = self._build_tally_string()
            return f"SUBSCRIBE OK TALLY\r\nTALLY OK {tally_str}\r\n".encode("utf-8")

        if topic == "ACTS":
            self._acts_subscribers.add("active")
            return b"SUBSCRIBE OK ACTS\r\n"

        return b"SUBSCRIBE OK\r\n"

    def _handle_unsubscribe(self, text: str) -> bytes:
        """Handle UNSUBSCRIBE commands."""
        parts = text.split()
        if len(parts) >= 2:
            topic = parts[1].upper()
            if topic == "TALLY":
                self._tally_subscribers.discard("active")
            elif topic == "ACTS":
                self._acts_subscribers.discard("active")
        return b"UNSUBSCRIBE OK\r\n"

    # ── Tally ──

    def _build_tally_string(self) -> str:
        """
        Build a tally string: one digit per input.
        0 = safe (not in program or preview)
        1 = program (live)
        2 = preview
        """
        active = self.state.get("active", 1)
        preview = self.state.get("preview", 2)
        input_count = self.state.get("input_count", 4)
        chars = []
        for i in range(1, input_count + 1):
            if i == active:
                chars.append("1")
            elif i == preview:
                chars.append("2")
            else:
                chars.append("0")
        return "".join(chars)

    def _push_tally(self) -> None:
        """Push a tally update to all subscribed clients."""
        if not self._tally_subscribers:
            return
        tally_str = self._build_tally_string()
        msg = f"TALLY OK {tally_str}\r\n".encode("utf-8")
        asyncio.ensure_future(self.push(msg))

    # ── XML state response ──

    def _build_xml_response(self) -> bytes:
        """
        Build the XML state response.

        Format: XML <length>\r\n<xml_body>
        The driver parses this with a custom frame parser that reads
        the length header, then consumes that many bytes of XML body.
        """
        xml_body = self._build_xml_body()
        xml_bytes = xml_body.encode("utf-8")
        header = f"XML {len(xml_bytes)}\r\n".encode("utf-8")
        return header + xml_bytes

    def _build_xml_body(self) -> str:
        """
        Build the vMix XML state document.

        The driver parses these elements:
          - <vmix> root: version, active, preview attributes
          - <recording>, <streaming>, <external>, <fadeToBlack> text elements
          - <inputs> with <input> children (number, title, type, state, muted, loop, position, duration)
          - <overlays> with <overlay> children (number attribute, text = input number)
          - <transitions> with <transition> children (number, effect, duration)
        """
        active = self.state.get("active", 1)
        preview = self.state.get("preview", 2)
        version = self.state.get("version", "27.0.0.48")
        recording = self.state.get("recording", False)
        streaming = self.state.get("streaming", False)
        external = self.state.get("external", False)
        ftb = self.state.get("fadeToBlack", False)
        input_count = self.state.get("input_count", 4)

        root = ET.Element("vmix")
        root.set("version", version)
        root.set("active", str(active))
        root.set("preview", str(preview))

        # Recording / streaming / external / FTB
        ET.SubElement(root, "recording").text = str(recording)
        ET.SubElement(root, "streaming").text = str(streaming)
        ET.SubElement(root, "external").text = str(external)
        ET.SubElement(root, "fadeToBlack").text = str(ftb)

        # Inputs
        inputs_el = ET.SubElement(root, "inputs")
        for i in range(1, input_count + 1):
            inp = ET.SubElement(inputs_el, "input")
            inp.set("number", str(i))
            inp.set("title", self._INPUT_NAMES.get(i, f"Input {i}"))
            inp.set("type", "Capture" if i <= 2 else "Image")
            inp.set("state", "Running")
            audio = self._input_audio.get(i, {})
            inp.set("muted", str(audio.get("muted", False)))
            inp.set("loop", "False")
            inp.set("position", "0")
            inp.set("duration", "0")

        # Overlays
        overlays_el = ET.SubElement(root, "overlays")
        for ch in range(1, 5):
            ov = ET.SubElement(overlays_el, "overlay")
            ov.set("number", str(ch))
            ov_input = self._overlays.get(ch, 0)
            ov.text = str(ov_input) if ov_input else ""

        # Transitions (4 default slots)
        transitions_el = ET.SubElement(root, "transitions")
        for t_num, effect in enumerate(["Fade", "Merge", "Wipe", "CubeZoom"], start=1):
            trans = ET.SubElement(transitions_el, "transition")
            trans.set("number", str(t_num))
            trans.set("effect", effect)
            trans.set("duration", "1000")

        return ET.tostring(root, encoding="unicode", xml_declaration=True)

    # ── Helpers ──

    def _resolve_input(self, value: str | None) -> int | None:
        """Convert an input parameter to an integer, or None if not provided."""
        if value is None or value == "":
            return None
        try:
            return int(value)
        except ValueError:
            # Try matching by name
            for num, name in self._INPUT_NAMES.items():
                if name.lower() == value.lower():
                    return num
            return None

    @staticmethod
    def _resolve_int(value: str | None) -> int | None:
        """Convert a string to int, or None."""
        if value is None or value == "":
            return None
        try:
            return int(value)
        except ValueError:
            return None
