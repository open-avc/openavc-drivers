"""
vMix Video Production Software — Simulator

Models the vMix TCP API on port 8099:
  - VERSION greeting sent the moment a client connects, before anything is asked
  - FUNCTION command handling (Cut, Fade, PreviewInput, OverlayInput3In, ...)
  - XML state query with a length-prefixed response
  - XMLTEXT XPath lookups
  - TALLY subscription with push on every tally change
  - ACTS subscription with push on overlay, recording, fade-to-black and audio
  - Controls schema for the Simulator UI

Three details here are modelled the way vMix 29 actually behaves rather than the
way its documentation reads, because each one hid a bug in this driver:

1. version, edition, active and preview are child ELEMENTS of <vmix>. The root
   carries no attributes at all. A simulator that wrote them as attributes
   agreed with a driver that read them as attributes, and both were wrong.
2. An unknown FUNCTION name is answered "FUNCTION OK Completed", exactly like a
   real one. vMix never reports a bad function name over TCP, which is how six
   commands in this driver sat dead and green for months. The simulator keeps
   that behaviour on purpose — a simulator that rejected unknown names would
   make this driver's tests pass where the device would not. What guards the
   names instead is tests/test_vmix_driver.py, which checks every function the
   driver can emit against the vendor's published function list.
3. Only inputs that carry audio report any audio attributes. Input 1 here is a
   Colour input with none, which is the case that reads as "muted, volume 0" to
   anyone who assumes the attributes are always present.
"""

import asyncio
import xml.etree.ElementTree as ET
from urllib.parse import unquote_plus

from openavc.simulator.tcp_simulator import TCPSimulator

# vMix lists sixteen overlay channels in its XML but only addresses eight.
_XML_OVERLAY_SLOTS = 16
_ADDRESSABLE_OVERLAYS = 8

_TRANSITION_EFFECTS = {
    "Fade", "Zoom", "Wipe", "Slide", "Fly", "CrossZoom", "FlyRotate", "Cube",
    "CubeZoom", "VerticalWipe", "VerticalSlide", "Merge", "WipeReverse",
    "SlideReverse", "VerticalWipeReverse", "VerticalSlideReverse",
}


def fader_to_amplitude(fader: float) -> float:
    """What vMix reports for a fader position.

    vMix takes the fader position on every write and reports the resulting
    linear amplitude everywhere else. Measured against vMix 29 at
    0/10/25/50/75/90/100 and exact at each: writing 50 reads back 6.25.
    """
    fader = max(0.0, min(float(fader), 100.0))
    return round((fader / 100.0) ** 4 * 100.0, 6)


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
            "multicorder": False,
            "fullscreen": False,
            "playlist": False,
            "fade_to_black": False,
            "input_count": 4,
            "version": "29.0.0.49",
            "edition": "4K",
            "master_volume": 100,
            "master_muted": False,
            "headphones_volume": 100,
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
                "key": "active",
                "label": "Program Input",
                "options": ["1", "2", "3", "4"],
                "labels": {"1": "Input 1", "2": "Input 2", "3": "Input 3", "4": "Input 4"},
            },
            {
                "type": "select",
                "key": "preview",
                "label": "Preview Input",
                "options": ["1", "2", "3", "4"],
                "labels": {"1": "Input 1", "2": "Input 2", "3": "Input 3", "4": "Input 4"},
            },
            {"type": "toggle", "key": "recording", "label": "Recording"},
            {"type": "toggle", "key": "streaming", "label": "Streaming"},
            {"type": "toggle", "key": "external", "label": "External Output"},
            {"type": "toggle", "key": "fade_to_black", "label": "Fade to Black"},
            {"type": "toggle", "key": "master_muted", "label": "Master Muted"},
            {
                "type": "slider",
                "key": "master_volume",
                "label": "Master Volume",
                "min": 0,
                "max": 100,
                "unit": "%",
            },
        ],
    }

    # The simulated production. Input 1 deliberately carries no audio at all:
    # that is the case a driver gets wrong when it assumes every input reports
    # a volume and a mute.
    _INPUTS = [
        {"number": 1, "title": "Colour", "type": "Colour",
         "key": "3fa81c6c-3f46-4c4e-b8b2-d8555006ab1c", "audio": False},
        {"number": 2, "title": "Camera 2", "type": "Capture",
         "key": "be15de8a-8d3e-41a6-b82f-cfb54bee6f8f", "audio": True},
        {"number": 3, "title": "Slides", "type": "PowerPoint",
         "key": "4a627879-4363-41da-babe-1e6dc1062572", "audio": True},
        {"number": 4, "title": "Lower Third", "type": "Title",
         "key": "6169b34b-296f-4a84-8b32-edd590f8df9b", "audio": False,
         # A title carries its text fields as <text> children. "Headline"
         # deliberately sits beside a field called "title", which collides
         # with the input property of the same name — the case where the
         # picker has to drop one and say so.
         "texts": [("Headline", "Welcome"), ("Description", "Room 101"),
                   ("title", "collides with the input property")]},
    ]

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # A per-instance copy of the production. _INPUTS is a class attribute
        # and the simulator mutates it (renaming an input, retyping a title),
        # so without this one simulator's edits would follow the next one.
        self._INPUTS = [
            dict(entry, texts=list(entry.get("texts", ())))
            for entry in type(self)._INPUTS
        ]
        self._tally_subscribers: set[str] = set()
        self._acts_subscribers: set[str] = set()
        # Per-input audio. Volume is held as the FADER position, the scale
        # every write uses; the XML emits the amplitude vMix would report.
        self._input_audio: dict[int, dict] = {
            inp["number"]: {
                "muted": False, "volume": 100.0, "balance": 0.0, "gain_db": 0.0,
                "solo": False, "solo_pfl": False, "busses": "M",
            }
            for inp in self._INPUTS if inp["audio"]
        }
        self._overlays: dict[int, int] = {
            ch: 0 for ch in range(1, _ADDRESSABLE_OVERLAYS + 1)
        }
        # vMix's extra mixes, keyed by vMix's own number (the main mix is 1
        # and lives in the document's top-level active/preview, not here).
        # Two of them, so a test can tell "the right mix" from "a mix".
        self._mixes: dict[int, dict[str, int]] = {
            2: {"active": 0, "preview": 0},
            3: {"active": 0, "preview": 0},
        }

    async def on_client_connected(self, client_id: str) -> bytes | None:
        """vMix announces its version before the client asks anything."""
        version = self.state.get("version", "29.0.0.49")
        return f"VERSION OK {version}\r\n".encode("utf-8")

    def handle_command(self, data: bytes) -> bytes | None:
        """Parse a vMix TCP command and return the response."""
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return None

        if text == "XML":
            return self._build_xml_response()

        if text.startswith("XMLTEXT"):
            return self._handle_xmltext(text)

        if text.startswith("SUBSCRIBE"):
            return self._handle_subscribe(text)

        if text.startswith("UNSUBSCRIBE"):
            return self._handle_unsubscribe(text)

        if text.startswith("FUNCTION"):
            return self._handle_function(text)

        if text == "VERSION":
            version = self.state.get("version", "29.0.0.49")
            return f"VERSION OK {version}\r\n".encode("utf-8")

        if text == "QUIT":
            return b"QUIT OK\r\n"

        # vMix names the command back when it doesn't know it.
        command = text.split(" ", 1)[0]
        return f"{command} ER Unknown Command\r\n".encode("utf-8")

    # ── FUNCTION command handling ──

    def _handle_function(self, text: str) -> bytes:
        """
        Parse and execute a FUNCTION command.

        Format:   FUNCTION <FunctionName> [Param=Value&Param2=Value2]
        Response: FUNCTION OK Completed  or  FUNCTION ER <message>

        A function this simulator does not implement still answers OK, because
        that is what vMix does — see the module docstring.
        """
        parts = text.split(" ", 2)
        if len(parts) < 2:
            return b"FUNCTION ER Invalid command\r\n"

        func_name = parts[1]
        query_str = parts[2] if len(parts) > 2 else ""

        params = {}
        if query_str:
            for pair in query_str.split("&"):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    # The driver percent-encodes every value, as vMix requires.
                    params[key] = unquote_plus(value)

        result = self._execute_function(func_name, params)
        if isinstance(result, str):
            return f"FUNCTION ER {result}\r\n".encode("utf-8")
        return b"FUNCTION OK Completed\r\n"

    # The vendor's table names Mix on five functions; the transition effects
    # it omits were measured taking it too. CutDirect and QuickPlay look like
    # they should and do not.
    _MIX_AWARE = {"Cut", "CutDirect", "PreviewInput", "ActiveInput"} | _TRANSITION_EFFECTS
    _MIX_DEAF = {"CutDirect", "QuickPlay"}

    def _target_mix(self, params: dict) -> int | None:
        """Which mix a command acts on, as vMix's own number.

        The wire argument counts from zero off the main mix, so Mix=1 is
        vMix's mix 2. Returns None for the main mix.
        """
        raw = params.get("Mix")
        if raw in (None, ""):
            return None
        try:
            wire = int(raw)
        except (TypeError, ValueError):
            return None
        return None if wire <= 0 else wire + 1

    def _execute_function(self, func_name: str, params: dict) -> bool | str:
        """Apply a vMix function. Returns True, or an error message string."""
        input_num = self._resolve_input(params.get("Input"))
        value = params.get("Value")

        mix = self._target_mix(params)
        if mix is not None and func_name not in self._MIX_DEAF:
            if mix not in self._mixes:
                return f"Mix {mix} does not exist"
            if func_name in self._MIX_AWARE or func_name.startswith("Stinger"):
                if not self._input_exists(input_num):
                    return f"Input {input_num} does not exist"
                if func_name == "PreviewInput":
                    self._mixes[mix]["preview"] = input_num
                else:
                    self._mixes[mix]["active"] = input_num
                return True

        # ── Transitions ──
        if func_name in ("Cut", "CutDirect") or func_name in _TRANSITION_EFFECTS:
            target = input_num if input_num else self.state.get("preview", 1)
            if not self._input_exists(target):
                return f"Input {target} does not exist"
            old_active = self.state.get("active", 1)
            self.set_state("active", target)
            if func_name != "CutDirect":
                self.set_state("preview", old_active)
            self._push_tally()
            self._push_act("Input", target, 1)
            self._push_act("InputPreview", self.state.get("preview", 1), 1)
            return True

        if func_name.startswith("Transition") and func_name[10:].isdigit():
            return self._execute_function("Cut", params)

        if func_name.startswith("Stinger") and func_name[7:].isdigit():
            return self._execute_function("Cut", params)

        if func_name == "FadeToBlack":
            new = not self.state.get("fade_to_black", False)
            self.set_state("fade_to_black", new)
            self._push_act("FadeToBlack", None, 1 if new else 0)
            return True

        if func_name == "SetFader":
            return True

        # ── Input switching ──
        if func_name in ("PreviewInput", "QuickPlay", "ActiveInput"):
            if not self._input_exists(input_num):
                return f"Input {input_num} does not exist"
            if func_name == "PreviewInput":
                self.set_state("preview", input_num)
                self._push_act("InputPreview", input_num, 1)
            else:
                self.set_state("active", input_num)
                self._push_act("Input", input_num, 1)
            self._push_tally()
            return True

        if func_name in ("PreviewInputNext", "PreviewInputPrevious"):
            count = len(self._INPUTS)
            step = 1 if func_name.endswith("Next") else -1
            current = self.state.get("preview", 1)
            self.set_state("preview", ((current - 1 + step) % count) + 1)
            self._push_tally()
            return True

        # ── Overlays ──
        if func_name.startswith("OverlayInput"):
            return self._execute_overlay(func_name, input_num)

        # ── Audio ──
        if func_name in ("Audio", "AudioOn", "AudioOff"):
            audio = self._input_audio.get(input_num)
            if audio is None:
                return f"Input {input_num} has no audio"
            audio["muted"] = (
                not audio["muted"] if func_name == "Audio" else func_name == "AudioOff"
            )
            self._push_act("InputAudio", input_num, 0 if audio["muted"] else 1)
            return True

        if func_name in ("SetVolume", "SetVolumeFade"):
            audio = self._input_audio.get(input_num)
            if audio is None:
                return f"Input {input_num} has no audio"
            raw = (value or "").split(",")[0] if func_name == "SetVolumeFade" else value
            if func_name == "SetVolumeFade" and "," not in (value or ""):
                # vMix wants "volume,milliseconds" in one Value and rejects
                # anything else outright.
                return "Value must be Volume,Milliseconds"
            level = self._as_float(raw)
            if level is None:
                return "Invalid volume"
            audio["volume"] = max(0.0, min(level, 100.0))
            self._push_act("InputVolume", input_num, fader_to_amplitude(audio["volume"]) / 100.0)
            return True

        if func_name in ("SetGain", "SetBalance"):
            audio = self._input_audio.get(input_num)
            if audio is None:
                return f"Input {input_num} has no audio"
            level = self._as_float(value)
            if level is None:
                return "Invalid value"
            if func_name == "SetGain":
                audio["gain_db"] = max(0.0, min(level, 24.0))
            else:
                audio["balance"] = max(-1.0, min(level, 1.0))
            return True

        if func_name in ("Solo", "SoloOn", "SoloOff"):
            audio = self._input_audio.get(input_num)
            if audio is None:
                return f"Input {input_num} has no audio"
            audio["solo"] = (
                not audio["solo"] if func_name == "Solo" else func_name == "SoloOn"
            )
            self._push_act("InputSolo", input_num, 1 if audio["solo"] else 0)
            return True

        if func_name == "SoloAllOff":
            for audio in self._input_audio.values():
                audio["solo"] = False
            return True

        if func_name in ("AudioBusOn", "AudioBusOff"):
            audio = self._input_audio.get(input_num)
            if audio is None:
                return f"Input {input_num} has no audio"
            busses = [b for b in audio["busses"].split(",") if b]
            bus = (value or "").upper()
            if func_name == "AudioBusOn" and bus not in busses:
                busses.append(bus)
            elif func_name == "AudioBusOff" and bus in busses:
                busses.remove(bus)
            audio["busses"] = ",".join(busses)
            return True

        if func_name in ("MasterAudio", "MasterAudioOn", "MasterAudioOff"):
            muted = self.state.get("master_muted", False)
            new = not muted if func_name == "MasterAudio" else func_name == "MasterAudioOff"
            self.set_state("master_muted", new)
            self._push_act("MasterAudio", None, 0 if new else 1)
            return True

        if func_name in ("SetMasterVolume", "SetMasterVolumeFade"):
            raw = (value or "").split(",")[0]
            if func_name == "SetMasterVolumeFade" and "," not in (value or ""):
                return "Value must be Volume,Milliseconds"
            level = self._as_float(raw)
            if level is None:
                return "Invalid volume"
            level = max(0.0, min(level, 100.0))
            self.set_state("master_volume", level)
            self._push_act("MasterVolume", None, fader_to_amplitude(level) / 100.0)
            return True

        if func_name == "SetHeadphonesVolume":
            level = self._as_float(value)
            if level is None:
                return "Invalid volume"
            level = max(0.0, min(level, 100.0))
            self.set_state("headphones_volume", level)
            self._push_act("MasterHeadphones", None, fader_to_amplitude(level) / 100.0)
            return True

        # ── Recording / streaming / outputs ──
        for prefix, key, activator in (
            ("Recording", "recording", "Recording"),
            ("Streaming", "streaming", "Streaming"),
            ("External", "external", "External"),
            ("MultiCorder", "multicorder", "MultiCorder"),
        ):
            if func_name == f"Start{prefix}":
                self.set_state(key, True)
                self._push_act(activator, None, 1)
                return True
            if func_name == f"Stop{prefix}":
                self.set_state(key, False)
                self._push_act(activator, None, 0)
                return True

        # ── Titles ──
        if func_name == "SetText":
            entry = self._input_entry(input_num)
            if entry is None:
                return f"Input {input_num} does not exist"
            field = params.get("SelectedName")
            texts = entry.get("texts")
            if not texts:
                return f"Input {input_num} has no text fields"
            for i, (name, _old) in enumerate(texts):
                if name == field:
                    texts[i] = (name, value or "")
                    return True
            return f"No text field named {field}"

        if func_name == "SetInputName":
            entry = self._input_entry(input_num)
            if entry is None:
                return f"Input {input_num} does not exist"
            entry["title"] = value or ""
            return True

        # Everything else is accepted the way vMix accepts it, including
        # function names that do not exist. See the module docstring.
        return True

    def _execute_overlay(self, func_name: str, input_num: int | None) -> bool | str:
        """Handle the OverlayInput<N>[In|Out|Off|Zoom] family."""
        if func_name == "OverlayInputAllOff":
            for channel in self._overlays:
                if self._overlays[channel]:
                    self._overlays[channel] = 0
                    self._push_act("Overlay", channel, 0, extra=0)
            self._push_tally()
            return True

        suffix = func_name[len("OverlayInput"):]
        digits = ""
        for ch in suffix:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            return True
        channel = int(digits)
        if channel not in self._overlays:
            return True
        action = suffix[len(digits):]

        if action in ("Off", "Out"):
            showing = self._overlays[channel]
            self._overlays[channel] = 0
            self._push_act("Overlay", channel, 0, extra=showing or 1)
        elif action == "Zoom":
            return True
        else:  # "" (toggle) or "In"
            if input_num is None:
                return "No Input specified"
            if not self._input_exists(input_num):
                return f"Input {input_num} does not exist"
            if action == "" and self._overlays[channel] == input_num:
                self._overlays[channel] = 0
                self._push_act("Overlay", channel, 0, extra=input_num)
            else:
                self._overlays[channel] = input_num
                self._push_act("Overlay", channel, 1, extra=input_num)
        self._push_tally()
        return True

    # ── Subscriptions ──

    def _handle_subscribe(self, text: str) -> bytes:
        """Handle SUBSCRIBE commands."""
        parts = text.split()
        if len(parts) < 2:
            return b"SUBSCRIBE ER Invalid command\r\n"

        topic = parts[1].upper()

        if topic == "TALLY":
            self._tally_subscribers.add("active")
            tally_str = self._build_tally_string()
            return (
                f"SUBSCRIBE OK TALLY Subscribed\r\nTALLY OK {tally_str}\r\n"
            ).encode("utf-8")

        if topic == "ACTS":
            # vMix sends no snapshot on subscribe: events start at the next
            # change, which is why the driver still seeds itself from XML.
            self._acts_subscribers.add("active")
            return b"SUBSCRIBE OK ACTS Subscribed\r\n"

        return b"SUBSCRIBE ER Invalid Command\r\n"

    def _handle_unsubscribe(self, text: str) -> bytes:
        """Handle UNSUBSCRIBE commands."""
        parts = text.split()
        if len(parts) >= 2:
            topic = parts[1].upper()
            if topic == "TALLY":
                self._tally_subscribers.discard("active")
                return b"UNSUBSCRIBE OK TALLY\r\n"
            if topic == "ACTS":
                self._acts_subscribers.discard("active")
                return b"UNSUBSCRIBE OK ACTS\r\n"
        return b"UNSUBSCRIBE ER Invalid Command\r\n"

    # ── Tally ──

    def _build_tally_string(self) -> str:
        """
        Build a tally string: one digit per input.
        0 = safe (not in program or preview)
        1 = program (live)
        2 = preview

        An input showing on an overlay is live too, which is why the first "1"
        in this string is not reliably the program input.
        """
        active = self.state.get("active", 1)
        preview = self.state.get("preview", 2)
        on_overlay = {num for num in self._overlays.values() if num}
        chars = []
        for inp in self._INPUTS:
            i = inp["number"]
            if i == active or i in on_overlay:
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
        msg = f"TALLY OK {self._build_tally_string()}\r\n".encode("utf-8")
        asyncio.ensure_future(self.push(msg))

    def _push_act(
        self, name: str, input_num: int | None, value, *, extra: int | None = None
    ) -> None:
        """Push one activator event.

        Global activators are "ACTS OK <Name> <value>"; input-scoped ones carry
        the input number in the middle. Overlays are the odd one out: the middle
        token is the input assigned to the channel and the name carries the
        channel, so they pass ``extra``.
        """
        if not self._acts_subscribers:
            return
        if name == "Overlay":
            line = f"ACTS OK Overlay{input_num} {extra} {value}"
        elif input_num is None:
            line = f"ACTS OK {name} {value}"
        else:
            line = f"ACTS OK {name} {input_num} {value}"
        asyncio.ensure_future(self.push(f"{line}\r\n".encode("utf-8")))

    # ── XML state response ──

    def _build_xml_response(self) -> bytes:
        """
        Build the XML state response.

        Format: XML <length>\r\n<xml_body>

        vMix counts the trailing CRLF in the length, so the body it promises is
        two bytes longer than the document itself. A driver that frames on the
        length alone must tolerate that trailer.
        """
        xml_body = self._build_xml_body().encode("utf-8") + b"\r\n"
        header = f"XML {len(xml_body)}\r\n".encode("utf-8")
        return header + xml_body

    def _handle_xmltext(self, text: str) -> bytes:
        """Answer the handful of XPath lookups worth simulating."""
        parts = text.split(" ", 1)
        path = parts[1].strip() if len(parts) > 1 else ""
        answers = {
            "vmix/version": str(self.state.get("version", "29.0.0.49")),
            "vmix/edition": str(self.state.get("edition", "4K")),
            "vmix/active": str(self.state.get("active", 1)),
            "vmix/preview": str(self.state.get("preview", 2)),
        }
        if path in answers:
            return f"XMLTEXT OK {answers[path]}\r\n".encode("utf-8")
        return b"XMLTEXT ER XML Entry Not Found\r\n"

    def _build_xml_body(self) -> str:
        """
        Build the vMix XML state document.

        Shape matters here: version, edition, active, preview and the status
        booleans are child ELEMENTS. The <vmix> root has no attributes.
        """
        root = ET.Element("vmix")
        ET.SubElement(root, "version").text = str(self.state.get("version", "29.0.0.49"))
        ET.SubElement(root, "edition").text = str(self.state.get("edition", "4K"))

        inputs_el = ET.SubElement(root, "inputs")
        for entry in self._INPUTS:
            number = entry["number"]
            inp = ET.SubElement(inputs_el, "input")
            inp.set("key", entry["key"])
            inp.set("number", str(number))
            inp.set("type", entry["type"])
            inp.set("title", entry["title"])
            inp.set("shortTitle", entry["title"])
            inp.set("state", "Running" if number == self.state.get("active") else "Paused")
            inp.set("position", "0")
            inp.set("duration", "0")
            inp.set("loop", "False")
            audio = self._input_audio.get(number)
            if audio is not None:
                inp.set("muted", str(audio["muted"]))
                inp.set("volume", str(fader_to_amplitude(audio["volume"])))
                inp.set("balance", str(audio["balance"]))
                inp.set("solo", str(audio["solo"]))
                inp.set("soloPFL", str(audio["solo_pfl"]))
                inp.set("audiobusses", audio["busses"])
                inp.set("meterF1", "0")
                inp.set("meterF2", "0")
                inp.set("gainDb", str(audio["gain_db"]))
            for index, (name, value) in enumerate(entry.get("texts", ())):
                text_el = ET.SubElement(inp, "text")
                text_el.set("index", str(index))
                text_el.set("name", name)
                text_el.text = value
            if not entry.get("texts"):
                inp.text = entry["title"]

        overlays_el = ET.SubElement(root, "overlays")
        for channel in range(1, _XML_OVERLAY_SLOTS + 1):
            ov = ET.SubElement(overlays_el, "overlay")
            ov.set("number", str(channel))
            showing = self._overlays.get(channel, 0)
            if showing:
                ov.text = str(showing)

        ET.SubElement(root, "preview").text = str(self.state.get("preview", 2))
        ET.SubElement(root, "active").text = str(self.state.get("active", 1))
        ET.SubElement(root, "fadeToBlack").text = str(
            self.state.get("fade_to_black", False)
        )

        transitions_el = ET.SubElement(root, "transitions")
        for number, effect in enumerate(["Fade", "Merge", "Wipe", "CubeZoom"], start=1):
            trans = ET.SubElement(transitions_el, "transition")
            trans.set("number", str(number))
            trans.set("effect", effect)
            trans.set("duration", "500" if number == 1 else "1000")

        for element, key in (
            ("recording", "recording"),
            ("external", "external"),
            ("streaming", "streaming"),
            ("playList", "playlist"),
            ("multiCorder", "multicorder"),
            ("fullscreen", "fullscreen"),
        ):
            ET.SubElement(root, element).text = str(self.state.get(key, False))

        for number in sorted(self._mixes):
            mix_el = ET.SubElement(root, "mix")
            mix_el.set("number", str(number))
            ET.SubElement(mix_el, "preview").text = str(self._mixes[number]["preview"])
            ET.SubElement(mix_el, "active").text = str(self._mixes[number]["active"])

        audio_el = ET.SubElement(root, "audio")
        master = ET.SubElement(audio_el, "master")
        master.set("volume", str(fader_to_amplitude(self.state.get("master_volume", 100))))
        master.set("muted", str(self.state.get("master_muted", False)))
        master.set("meterF1", "0")
        master.set("meterF2", "0")
        master.set(
            "headphonesVolume",
            str(fader_to_amplitude(self.state.get("headphones_volume", 100))),
        )

        return ET.tostring(root, encoding="unicode")

    # ── Helpers ──

    def _input_entry(self, number: int | None) -> dict | None:
        for entry in self._INPUTS:
            if entry["number"] == number:
                return entry
        return None

    def _input_exists(self, number: int | None) -> bool:
        return self._input_entry(number) is not None

    def _resolve_input(self, value: str | None) -> int | None:
        """Convert an Input parameter to a number, resolving titles too."""
        if value is None or value == "":
            return None
        number = self._resolve_int(value)
        if number is not None:
            return number
        for entry in self._INPUTS:
            if entry["title"].lower() == value.lower() or entry["key"] == value:
                return entry["number"]
        return None

    @staticmethod
    def _resolve_int(value: str | None) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_float(value: str | None) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
