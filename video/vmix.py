"""
OpenAVC vMix Driver.

Controls vMix video production software via its TCP API (port 8099).
Supports all major vMix functions: transitions, input switching, audio,
overlays, recording, streaming, titles, replay, and PTZ.

vMix TCP API reference:
https://www.vmix.com/help29/TCPAPI.html

Protocol overview:
- Commands: FUNCTION <name> <params>\r\n
- Responses: FUNCTION OK\r\n  or  FUNCTION <n> ER <msg>\r\n
- XML state: XML <length>\r\n<binary XML body>
- Subscriptions: SUBSCRIBE TALLY\r\n -> TALLY OK <data>\r\n (push)
- Subscriptions: SUBSCRIBE ACTS\r\n -> ACTS OK <data>\r\n (push)

Mixed framing: normal messages are CRLF-delimited text. XML responses
use a length-prefixed binary body after the "XML <length>" header line.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from typing import Any, Optional

from server.drivers.base import BaseDriver
from server.transport.frame_parsers import CallableFrameParser, FrameParser
from server.transport.tcp import TCPTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

# Sentinel prefix for XML body messages from the frame parser
_XML_BODY_PREFIX = b"XML_BODY "


def _parse_vmix_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """
    Parse vMix TCP frames from a byte buffer.

    Normal messages: delimited by CRLF (\\r\\n).
    XML responses: "XML <length>\\r\\n" header followed by <length> bytes of XML body.

    Returns (message, remaining) or (None, buffer) if incomplete.
    """
    # Need at least a CRLF to have any complete message
    crlf_pos = buffer.find(b"\r\n")
    if crlf_pos == -1:
        return None, buffer

    line = buffer[:crlf_pos]

    # Check if this is an XML length-prefixed response
    if line.startswith(b"XML "):
        try:
            xml_len = int(line[4:])
        except ValueError:
            # Not a valid XML length — treat as normal message
            remaining = buffer[crlf_pos + 2:]
            return line, remaining

        # Need the full XML body after the header line + CRLF
        body_start = crlf_pos + 2
        body_end = body_start + xml_len
        if len(buffer) < body_end:
            return None, buffer  # Wait for more data

        xml_body = buffer[body_start:body_end]
        remaining = buffer[body_end:]
        # Tag the XML body so the router can identify it
        return _XML_BODY_PREFIX + xml_body, remaining

    # Normal CRLF-delimited message
    remaining = buffer[crlf_pos + 2:]
    return line, remaining


class VMixDriver(BaseDriver):
    """vMix video production software driver via TCP API."""

    DRIVER_INFO = {
        "id": "vmix",
        "name": "vMix",
        "manufacturer": "StudioCoast",
        "category": "video",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls vMix video production software via the TCP API. "
            "Supports transitions, input switching, audio, overlays, "
            "recording, streaming, titles, replay, and PTZ."
        ),
        "transport": "tcp",
        "help": {
            "overview": (
                "Full control of vMix video production software. Supports "
                "input switching, transitions, audio mixing, overlays, "
                "recording/streaming, titles, replay, and PTZ cameras."
            ),
            "setup": (
                "1. Open vMix and go to Settings > Web Controller\n"
                "2. Ensure the TCP API is enabled (default port 8099)\n"
                "3. Enter the vMix PC's IP address and port below\n"
                "4. Tally subscription is enabled by default for real-time "
                "program/preview tracking"
            ),
        },
        "default_config": {
            "host": "",
            "port": 8099,
            "poll_interval": 30,
            "subscribe_tally": True,
            "subscribe_acts": False,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "IP address of the PC running vMix",
            },
            "port": {
                "type": "integer",
                "default": 8099,
                "label": "Port",
                "description": "vMix TCP API port (default 8099)",
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": "How often to request full XML state (0 to disable)",
            },
            "subscribe_tally": {
                "type": "boolean",
                "default": True,
                "label": "Subscribe to Tally",
                "description": "Real-time program/preview tracking via tally subscription",
            },
            "subscribe_acts": {
                "type": "boolean",
                "default": False,
                "label": "Subscribe to Activators",
                "description": "Real-time activator state push notifications",
            },
        },
        "state_variables": {
            "active": {"type": "integer", "label": "Program Input"},
            "preview": {"type": "integer", "label": "Preview Input"},
            "recording": {"type": "boolean", "label": "Recording"},
            "streaming": {"type": "boolean", "label": "Streaming"},
            "external": {"type": "boolean", "label": "External Output"},
            "fadeToBlack": {"type": "boolean", "label": "Fade to Black"},
            "input_count": {"type": "integer", "label": "Input Count"},
            "version": {"type": "string", "label": "vMix Version"},
        },
        "commands": {
            # --- Transitions ---
            "cut": {
                "label": "Cut",
                "params": {"input": {"type": "string", "help": "Input number or name (optional, omit for current preview)"}},
                "help": "Instant cut transition to the specified input or current preview.",
            },
            "fade": {
                "label": "Fade",
                "params": {
                    "input": {"type": "string", "help": "Input number or name (optional)"},
                    "duration": {"type": "integer", "help": "Fade duration in milliseconds"},
                },
                "help": "Fade transition to the specified input.",
            },
            "cut_direct": {
                "label": "Cut Direct",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Cut directly to an input without changing preview.",
            },
            "fade_to_black": {
                "label": "Fade to Black",
                "params": {},
                "help": "Toggle fade to black on program output.",
            },
            "transition": {
                "label": "Transition",
                "params": {
                    "input": {"type": "string", "help": "Input number or name"},
                    "effect": {"type": "string", "help": "Transition effect name (e.g., Fade, Zoom, Wipe)"},
                    "duration": {"type": "integer", "help": "Duration in milliseconds"},
                },
                "help": "Execute a named transition effect.",
            },
            "stinger": {
                "label": "Stinger",
                "params": {
                    "input": {"type": "string", "help": "Input number or name"},
                    "index": {"type": "integer", "help": "Stinger index (1-4)"},
                },
                "help": "Execute a stinger transition.",
            },
            "set_fader": {
                "label": "Set T-Bar",
                "params": {"position": {"type": "integer", "required": True, "help": "T-bar position 0-255"}},
                "help": "Set the transition T-bar position (0=full A, 255=full B).",
            },
            # --- Input Switching ---
            "preview_input": {
                "label": "Preview Input",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Send an input to preview.",
            },
            "active_input": {
                "label": "Active Input (Cut)",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Cut an input directly to program output.",
            },
            "preview_input_next": {
                "label": "Preview Next",
                "params": {},
                "help": "Advance preview to the next input.",
            },
            "preview_input_previous": {
                "label": "Preview Previous",
                "params": {},
                "help": "Move preview to the previous input.",
            },
            # --- Audio ---
            "audio": {
                "label": "Audio Toggle",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Toggle audio on/off for an input.",
            },
            "audio_on": {
                "label": "Audio On",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Enable audio for an input.",
            },
            "audio_off": {
                "label": "Audio Off",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Disable audio for an input.",
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "integer", "required": True, "help": "Volume 0-100"},
                },
                "help": "Set volume level for an input (0-100).",
            },
            "set_volume_fade": {
                "label": "Set Volume (Fade)",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "integer", "required": True, "help": "Target volume 0-100"},
                    "duration": {"type": "integer", "help": "Fade duration in ms"},
                },
                "help": "Fade volume to a target level over a duration.",
            },
            "set_gain": {
                "label": "Set Gain",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "integer", "required": True, "help": "Gain in dB (0-24)"},
                },
                "help": "Set audio gain for an input.",
            },
            "set_balance": {
                "label": "Set Balance",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "integer", "required": True, "help": "Balance -100 (left) to 100 (right)"},
                },
                "help": "Set audio balance/pan for an input.",
            },
            "solo": {
                "label": "Solo",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Toggle solo for an input in the audio mixer.",
            },
            "bus_audio": {
                "label": "Bus Audio Toggle",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "required": True, "help": "Bus letter (A, B, C, etc.)"},
                },
                "help": "Toggle an input's audio routing to a bus.",
            },
            "bus_audio_on": {
                "label": "Bus Audio On",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "required": True, "help": "Bus letter"},
                },
                "help": "Route an input's audio to a bus.",
            },
            "bus_audio_off": {
                "label": "Bus Audio Off",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "required": True, "help": "Bus letter"},
                },
                "help": "Remove an input's audio routing from a bus.",
            },
            "set_bus_volume": {
                "label": "Set Bus Volume",
                "params": {
                    "value": {"type": "string", "required": True, "help": "Bus letter (A, B, C, etc.)"},
                    "level": {"type": "integer", "required": True, "help": "Volume 0-100"},
                },
                "help": "Set the volume level of an audio bus.",
            },
            "master_audio": {
                "label": "Master Audio Toggle",
                "params": {},
                "help": "Toggle master audio on/off.",
            },
            "master_audio_on": {
                "label": "Master Audio On",
                "params": {},
                "help": "Enable master audio output.",
            },
            "master_audio_off": {
                "label": "Master Audio Off",
                "params": {},
                "help": "Disable master audio output.",
            },
            "set_master_volume": {
                "label": "Set Master Volume",
                "params": {"value": {"type": "integer", "required": True, "help": "Volume 0-100"}},
                "help": "Set the master output volume (0-100).",
            },
            # --- Overlays ---
            "overlay_input": {
                "label": "Overlay Toggle",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "integer", "required": True, "help": "Overlay channel 1-4"},
                },
                "help": "Toggle an overlay input on a channel.",
            },
            "overlay_input_in": {
                "label": "Overlay In",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "integer", "required": True, "help": "Overlay channel 1-4"},
                },
                "help": "Transition an overlay input in.",
            },
            "overlay_input_out": {
                "label": "Overlay Out",
                "params": {"value": {"type": "integer", "required": True, "help": "Overlay channel 1-4"}},
                "help": "Transition the current overlay out on a channel.",
            },
            "overlay_input_off": {
                "label": "Overlay Off",
                "params": {"value": {"type": "integer", "required": True, "help": "Overlay channel 1-4"}},
                "help": "Immediately turn off an overlay channel.",
            },
            "overlay_input_all_off": {
                "label": "All Overlays Off",
                "params": {},
                "help": "Turn off all overlay channels.",
            },
            # --- Recording / Streaming ---
            "start_recording": {
                "label": "Start Recording",
                "params": {},
                "help": "Start recording the program output.",
            },
            "stop_recording": {
                "label": "Stop Recording",
                "params": {},
                "help": "Stop the current recording.",
            },
            "start_streaming": {
                "label": "Start Streaming",
                "params": {"value": {"type": "integer", "help": "Stream channel (0-2, default 0)"}},
                "help": "Start streaming to the configured destination.",
            },
            "stop_streaming": {
                "label": "Stop Streaming",
                "params": {"value": {"type": "integer", "help": "Stream channel (0-2, default 0)"}},
                "help": "Stop the current stream.",
            },
            "start_external": {
                "label": "Start External",
                "params": {},
                "help": "Start external output (fullscreen, NDI, etc.).",
            },
            "stop_external": {
                "label": "Stop External",
                "params": {},
                "help": "Stop external output.",
            },
            "snapshot": {
                "label": "Snapshot",
                "params": {},
                "help": "Take a snapshot of the program output.",
            },
            "snapshot_input": {
                "label": "Snapshot Input",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Take a snapshot of a specific input.",
            },
            # --- Titles / Text ---
            "set_text": {
                "label": "Set Text",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "selectedName": {"type": "string", "required": True, "help": "Title field name"},
                    "value": {"type": "string", "required": True, "help": "Text value"},
                },
                "help": "Set a text field value in a title input.",
            },
            "set_image": {
                "label": "Set Image",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "selectedName": {"type": "string", "required": True, "help": "Image field name"},
                    "value": {"type": "string", "required": True, "help": "Image file path"},
                },
                "help": "Set an image field in a title input.",
            },
            "set_countdown": {
                "label": "Set Countdown",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "required": True, "help": "Time value (e.g., 00:05:00)"},
                },
                "help": "Set a countdown timer value.",
            },
            "start_countdown": {
                "label": "Start Countdown",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Start a countdown timer.",
            },
            "stop_countdown": {
                "label": "Stop Countdown",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Stop a countdown timer.",
            },
            # --- Input Control ---
            "play": {
                "label": "Play",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Start playback of a video/audio input.",
            },
            "pause": {
                "label": "Pause",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Pause playback of an input.",
            },
            "play_pause": {
                "label": "Play/Pause",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Toggle play/pause for an input.",
            },
            "restart": {
                "label": "Restart",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Restart playback from the beginning.",
            },
            "loop_on": {
                "label": "Loop On",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Enable loop mode for an input.",
            },
            "loop_off": {
                "label": "Loop Off",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Disable loop mode for an input.",
            },
            "set_position": {
                "label": "Set Position",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "integer", "required": True, "help": "Position in milliseconds"},
                },
                "help": "Set the playback position of an input.",
            },
            "set_rate": {
                "label": "Set Rate",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "required": True, "help": "Playback rate (e.g., 1, 0.5, 2)"},
                },
                "help": "Set the playback speed/rate of an input.",
            },
            # --- Replay ---
            "replay_play": {
                "label": "Replay Play",
                "params": {},
                "help": "Play the current replay event.",
            },
            "replay_pause": {
                "label": "Replay Pause",
                "params": {},
                "help": "Pause replay playback.",
            },
            "replay_mark_in": {
                "label": "Replay Mark In",
                "params": {},
                "help": "Set the replay in-point at the current position.",
            },
            "replay_mark_out": {
                "label": "Replay Mark Out",
                "params": {},
                "help": "Set the replay out-point at the current position.",
            },
            "replay_mark_in_out": {
                "label": "Replay Mark In/Out",
                "params": {},
                "help": "Set both in and out points for replay.",
            },
            "replay_live": {
                "label": "Replay Live",
                "params": {},
                "help": "Switch replay to live mode.",
            },
            "replay_recorded": {
                "label": "Replay Recorded",
                "params": {},
                "help": "Switch replay to recorded/playback mode.",
            },
            "replay_set_speed": {
                "label": "Replay Set Speed",
                "params": {"value": {"type": "string", "required": True, "help": "Speed (e.g., 1, 0.5, 0.25)"}},
                "help": "Set the replay playback speed.",
            },
            "replay_play_last_event": {
                "label": "Replay Last Event",
                "params": {},
                "help": "Play the most recently marked replay event.",
            },
            # --- PTZ ---
            "ptz_move_up": {
                "label": "PTZ Up",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "help": "Speed 0-1 (default 0.5)"},
                },
                "help": "Pan/tilt camera up.",
            },
            "ptz_move_down": {
                "label": "PTZ Down",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "help": "Speed 0-1"},
                },
                "help": "Pan/tilt camera down.",
            },
            "ptz_move_left": {
                "label": "PTZ Left",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "help": "Speed 0-1"},
                },
                "help": "Pan/tilt camera left.",
            },
            "ptz_move_right": {
                "label": "PTZ Right",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "help": "Speed 0-1"},
                },
                "help": "Pan/tilt camera right.",
            },
            "ptz_move_stop": {
                "label": "PTZ Stop",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Stop camera movement.",
            },
            "ptz_zoom_in": {
                "label": "PTZ Zoom In",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "help": "Speed 0-1"},
                },
                "help": "Zoom camera in.",
            },
            "ptz_zoom_out": {
                "label": "PTZ Zoom Out",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "help": "Speed 0-1"},
                },
                "help": "Zoom camera out.",
            },
            "ptz_zoom_stop": {
                "label": "PTZ Zoom Stop",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Stop camera zoom.",
            },
            "ptz_home": {
                "label": "PTZ Home",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Return camera to home position.",
            },
            "ptz_focus_auto": {
                "label": "PTZ Auto Focus",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Trigger auto focus on the camera.",
            },
            # --- Misc ---
            "add_input": {
                "label": "Add Input",
                "params": {"value": {"type": "string", "required": True, "help": "Input file path or URL"}},
                "help": "Add a new input to the production.",
            },
            "remove_input": {
                "label": "Remove Input",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Remove an input from the production.",
            },
            "set_input_name": {
                "label": "Set Input Name",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number"},
                    "value": {"type": "string", "required": True, "help": "New input name"},
                },
                "help": "Rename an input.",
            },
            "select_index": {
                "label": "Select Index",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "integer", "required": True, "help": "List item index"},
                },
                "help": "Select a specific item index within a list/playlist input.",
            },
            "next_item": {
                "label": "Next Item",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Advance to the next item in a list/playlist input.",
            },
            "previous_item": {
                "label": "Previous Item",
                "params": {"input": {"type": "string", "required": True, "help": "Input number or name"}},
                "help": "Go to the previous item in a list/playlist input.",
            },
            "browser_navigate": {
                "label": "Browser Navigate",
                "params": {
                    "input": {"type": "string", "required": True, "help": "Input number or name"},
                    "value": {"type": "string", "required": True, "help": "URL to navigate to"},
                },
                "help": "Navigate a browser input to a URL.",
            },
            "script_start": {
                "label": "Script Start",
                "params": {"value": {"type": "string", "required": True, "help": "Script name"}},
                "help": "Start a vMix script.",
            },
            "script_stop": {
                "label": "Script Stop",
                "params": {"value": {"type": "string", "required": True, "help": "Script name"}},
                "help": "Stop a running vMix script.",
            },
            # --- Generic ---
            "raw_function": {
                "label": "Raw Function",
                "params": {
                    "function": {"type": "string", "required": True, "help": "vMix function name (e.g., Cut, Fade, PreviewInput)"},
                    "query": {"type": "string", "help": "Additional query parameters (e.g., Input=1&Duration=1000)"},
                },
                "help": "Send any vMix API function with custom query parameters.",
            },
        },
    }

    # Map our command names to vMix function names
    _FUNCTION_MAP = {
        "cut": "Cut",
        "fade": "Fade",
        "cut_direct": "CutDirect",
        "fade_to_black": "FadeToBlack",
        "transition": "Transition",
        "stinger": "Stinger",
        "set_fader": "SetFader",
        "preview_input": "PreviewInput",
        "active_input": "ActiveInput",
        "preview_input_next": "PreviewInputNext",
        "preview_input_previous": "PreviewInputPrevious",
        "audio": "Audio",
        "audio_on": "AudioOn",
        "audio_off": "AudioOff",
        "set_volume": "SetVolume",
        "set_volume_fade": "SetVolumeFade",
        "set_gain": "SetGain",
        "set_balance": "SetBalance",
        "solo": "Solo",
        "bus_audio": "BusXAudio",
        "bus_audio_on": "BusXAudioOn",
        "bus_audio_off": "BusXAudioOff",
        "set_bus_volume": "SetBusXVolume",
        "master_audio": "MasterAudio",
        "master_audio_on": "MasterAudioOn",
        "master_audio_off": "MasterAudioOff",
        "set_master_volume": "SetMasterVolume",
        "overlay_input": "OverlayInput",
        "overlay_input_in": "OverlayInputIn",
        "overlay_input_out": "OverlayInputOut",
        "overlay_input_off": "OverlayInputOff",
        "overlay_input_all_off": "OverlayInputAllOff",
        "start_recording": "StartRecording",
        "stop_recording": "StopRecording",
        "start_streaming": "StartStreaming",
        "stop_streaming": "StopStreaming",
        "start_external": "StartExternal",
        "stop_external": "StopExternal",
        "snapshot": "Snapshot",
        "snapshot_input": "SnapshotInput",
        "set_text": "SetText",
        "set_image": "SetImage",
        "set_countdown": "SetCountdown",
        "start_countdown": "StartCountdown",
        "stop_countdown": "StopCountdown",
        "play": "Play",
        "pause": "Pause",
        "play_pause": "PlayPause",
        "restart": "Restart",
        "loop_on": "LoopOn",
        "loop_off": "LoopOff",
        "set_position": "SetPosition",
        "set_rate": "SetRate",
        "replay_play": "ReplayPlay",
        "replay_pause": "ReplayPause",
        "replay_mark_in": "ReplayMarkIn",
        "replay_mark_out": "ReplayMarkOut",
        "replay_mark_in_out": "ReplayMarkInOut",
        "replay_live": "ReplayLive",
        "replay_recorded": "ReplayRecorded",
        "replay_set_speed": "ReplaySetSpeed",
        "replay_play_last_event": "ReplayPlayLastEvent",
        "ptz_move_up": "PTZMoveUp",
        "ptz_move_down": "PTZMoveDown",
        "ptz_move_left": "PTZMoveLeft",
        "ptz_move_right": "PTZMoveRight",
        "ptz_move_stop": "PTZMoveStop",
        "ptz_zoom_in": "PTZZoomIn",
        "ptz_zoom_out": "PTZZoomOut",
        "ptz_zoom_stop": "PTZZoomStop",
        "ptz_home": "PTZHome",
        "ptz_focus_auto": "PTZFocusAuto",
        "add_input": "AddInput",
        "remove_input": "RemoveInput",
        "set_input_name": "SetInputName",
        "select_index": "SelectIndex",
        "next_item": "NextItem",
        "previous_item": "PreviousItem",
        "browser_navigate": "BrowserNavigate",
        "script_start": "ScriptStart",
        "script_stop": "ScriptStop",
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cmd_lock = asyncio.Lock()
        self._cmd_response: asyncio.Queue[str] = asyncio.Queue()
        self._tally_subscribed = False
        self._acts_subscribed = False

    def _create_frame_parser(self) -> Optional[FrameParser]:
        """Use callable parser for vMix mixed-mode framing."""
        return CallableFrameParser(_parse_vmix_frame)

    def _resolve_delimiter(self) -> Optional[bytes]:
        """vMix uses custom framing, not delimiter-based."""
        return None

    async def connect(self) -> None:
        """Connect to vMix TCP API."""
        host = self.config.get("host", "")
        port = self.config.get("port", 8099)
        frame_parser = self._create_frame_parser()

        self.transport = await TCPTransport.create(
            host=host,
            port=port,
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=None,
            frame_parser=frame_parser,
            name=self.device_id,
        )

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to vMix at {host}:{port}")

        # Subscribe to tally if configured
        if self.config.get("subscribe_tally", True):
            await self._subscribe_tally()

        # Subscribe to activators if configured
        if self.config.get("subscribe_acts", False):
            await self._subscribe_acts()

        # Start polling for full XML state
        poll_interval = self.config.get("poll_interval", 30)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        """Disconnect from vMix."""
        self._tally_subscribed = False
        self._acts_subscribed = False
        await self.stop_polling()
        if self.transport:
            # Unsubscribe before disconnecting
            try:
                if self._tally_subscribed:
                    await self.transport.send(b"UNSUBSCRIBE TALLY\r\n")
                if self._acts_subscribed:
                    await self.transport.send(b"UNSUBSCRIBE ACTS\r\n")
            except Exception:
                pass
            await self.transport.close()
            self.transport = None
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send a named command to vMix."""
        params = params or {}

        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        # Handle raw_function specially
        if command == "raw_function":
            func_name = params.get("function", "")
            query = params.get("query", "")
            if not func_name:
                log.warning(f"[{self.device_id}] raw_function: no function name")
                return None
            return await self._send_function(func_name, query)

        # Look up the vMix function name
        vmix_func = self._FUNCTION_MAP.get(command)
        if vmix_func is None:
            log.warning(f"[{self.device_id}] Unknown command: {command}")
            return None

        # Build query string from params
        query_parts = []

        # Bus commands need X replaced with the bus letter
        if command.startswith("bus_audio") or command == "set_bus_volume":
            bus = params.get("value", "A")
            vmix_func = vmix_func.replace("X", str(bus))
            # For bus_audio commands, input goes in Input= param
            if "input" in params:
                query_parts.append(f"Input={params['input']}")
            if command == "set_bus_volume":
                query_parts.append(f"Value={params.get('level', 0)}")
        else:
            # Standard parameter mapping
            if "input" in params:
                query_parts.append(f"Input={params['input']}")
            if "value" in params:
                query_parts.append(f"Value={params['value']}")
            if "duration" in params:
                query_parts.append(f"Duration={params['duration']}")
            if "selectedName" in params:
                query_parts.append(f"SelectedName={params['selectedName']}")
            if "effect" in params:
                query_parts.append(f"Value={params['effect']}")
            if "index" in params:
                query_parts.append(f"Value={params['index']}")
            if "position" in params:
                query_parts.append(f"Value={params['position']}")

        query = "&".join(query_parts) if query_parts else ""
        return await self._send_function(vmix_func, query)

    async def _send_function(self, function: str, query: str = "") -> str:
        """Send a FUNCTION command and wait for OK/ER response."""
        cmd = f"FUNCTION {function}"
        if query:
            cmd += f" {query}"
        cmd += "\r\n"

        async with self._cmd_lock:
            await self.transport.send(cmd.encode("utf-8"))
            log.debug(f"[{self.device_id}] Sent: {cmd.strip()}")

            # Wait for response with timeout
            try:
                response = await asyncio.wait_for(
                    self._cmd_response.get(), timeout=5.0
                )
                return response
            except asyncio.TimeoutError:
                log.warning(f"[{self.device_id}] Command timeout: {function}")
                return "TIMEOUT"

    async def _subscribe_tally(self) -> None:
        """Subscribe to tally state pushes."""
        if not self.transport or not self.transport.connected:
            return
        await self.transport.send(b"SUBSCRIBE TALLY\r\n")
        self._tally_subscribed = True
        log.debug(f"[{self.device_id}] Subscribed to TALLY")

    async def _subscribe_acts(self) -> None:
        """Subscribe to activator state pushes."""
        if not self.transport or not self.transport.connected:
            return
        await self.transport.send(b"SUBSCRIBE ACTS\r\n")
        self._acts_subscribed = True
        log.debug(f"[{self.device_id}] Subscribed to ACTS")

    async def on_data_received(self, data: bytes) -> None:
        """Route incoming messages by prefix."""
        # XML body (tagged by frame parser)
        if data.startswith(_XML_BODY_PREFIX):
            xml_body = data[len(_XML_BODY_PREFIX):]
            await self._handle_xml(xml_body)
            return

        # Decode text messages
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return

        # FUNCTION OK / FUNCTION <n> ER <msg>
        if text.startswith("FUNCTION"):
            await self._cmd_response.put(text)
            return

        # TALLY OK <data>
        if text.startswith("TALLY OK"):
            self._handle_tally(text)
            return

        # ACTS OK <data>
        if text.startswith("ACTS OK"):
            self._handle_acts(text)
            return

        # XMLTEXT OK <data>
        if text.startswith("XMLTEXT OK"):
            log.debug(f"[{self.device_id}] XMLTEXT: {text[:80]}")
            return

        # SUBSCRIBE OK
        if text.startswith("SUBSCRIBE OK"):
            log.debug(f"[{self.device_id}] Subscription confirmed")
            return

        # VERSION OK <version>
        if text.startswith("VERSION OK"):
            version = text.replace("VERSION OK ", "").strip()
            self.set_state("version", version)
            return

        log.debug(f"[{self.device_id}] Unhandled message: {text[:80]}")

    def _handle_tally(self, text: str) -> None:
        """
        Parse TALLY OK response and update state.

        Format: TALLY OK <tally_string>
        Each character is the tally state of the corresponding input:
            0 = safe (not in program or preview)
            1 = program (live)
            2 = preview
        """
        tally_data = text.replace("TALLY OK ", "").strip()

        active = None
        preview = None

        for i, ch in enumerate(tally_data):
            input_num = i + 1  # 1-based
            try:
                tally_val = int(ch)
            except ValueError:
                continue

            self.set_state(f"tally.{input_num}", tally_val)

            if tally_val == 1 and active is None:
                active = input_num
            elif tally_val == 2 and preview is None:
                preview = input_num

        if active is not None:
            self.set_state("active", active)
        if preview is not None:
            self.set_state("preview", preview)

    def _handle_acts(self, text: str) -> None:
        """Parse ACTS OK response (activator states)."""
        # ACTS OK <data> — each char is activator state
        log.debug(f"[{self.device_id}] ACTS: {text[:80]}")

    async def _handle_xml(self, xml_data: bytes) -> None:
        """Parse vMix XML state and flatten into state keys."""
        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            log.warning(f"[{self.device_id}] XML parse error: {e}")
            return

        # Version
        version = root.get("version", "")
        if version:
            self.set_state("version", version)

        # Recording / streaming / external / FTB
        recording = root.find("recording")
        if recording is not None:
            self.set_state("recording", recording.text == "True")

        streaming = root.find("streaming")
        if streaming is not None:
            self.set_state("streaming", streaming.text == "True")

        external = root.find("external")
        if external is not None:
            self.set_state("external", external.text == "True")

        ftb = root.find("fadeToBlack")
        if ftb is not None:
            self.set_state("fadeToBlack", ftb.text == "True")

        # Active / Preview from XML attributes
        active_input = root.get("active")
        if active_input:
            try:
                self.set_state("active", int(active_input))
            except ValueError:
                pass

        preview_input = root.get("preview")
        if preview_input:
            try:
                self.set_state("preview", int(preview_input))
            except ValueError:
                pass

        # Inputs
        inputs = root.find("inputs")
        if inputs is not None:
            input_count = 0
            for inp in inputs.findall("input"):
                input_count += 1
                num = inp.get("number", str(input_count))
                title = inp.get("title", "")
                inp_type = inp.get("type", "")
                state = inp.get("state", "")
                muted = inp.get("muted", "False")
                loop_val = inp.get("loop", "False")
                position = inp.get("position", "0")
                duration = inp.get("duration", "0")

                self.set_state(f"input.{num}.title", title)
                self.set_state(f"input.{num}.type", inp_type)
                self.set_state(f"input.{num}.state", state)
                self.set_state(f"input.{num}.muted", muted == "True")
                self.set_state(f"input.{num}.loop", loop_val == "True")
                try:
                    self.set_state(f"input.{num}.position", int(position))
                except ValueError:
                    pass
                try:
                    self.set_state(f"input.{num}.duration", int(duration))
                except ValueError:
                    pass

            self.set_state("input_count", input_count)

        # Overlays
        overlays = root.find("overlays")
        if overlays is not None:
            for overlay in overlays.findall("overlay"):
                overlay_num = overlay.get("number", "")
                overlay_input = overlay.text or ""
                if overlay_num:
                    try:
                        self.set_state(f"overlay.{overlay_num}", int(overlay_input) if overlay_input else 0)
                    except ValueError:
                        self.set_state(f"overlay.{overlay_num}", 0)

        # Transitions
        transitions = root.find("transitions")
        if transitions is not None:
            for trans in transitions.findall("transition"):
                trans_num = trans.get("number", "")
                effect = trans.get("effect", "")
                duration = trans.get("duration", "0")
                if trans_num:
                    self.set_state(f"transition.{trans_num}.effect", effect)
                    try:
                        self.set_state(f"transition.{trans_num}.duration", int(duration))
                    except ValueError:
                        pass

    async def poll(self) -> None:
        """Request full XML state from vMix."""
        if not self.transport or not self.transport.connected:
            return

        try:
            await self.transport.send(b"XML\r\n")
        except ConnectionError:
            log.warning(f"[{self.device_id}] Poll failed — not connected")
