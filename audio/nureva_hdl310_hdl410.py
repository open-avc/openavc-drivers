"""
OpenAVC Nureva HDL310 / HDL410 driver.

Controls a Nureva HDL310 or HDL410 audio conferencing system through the
local API that runs in its connect module: REST over HTTPS on port 443 with
JSON bodies and a self-signed certificate, plus two Server-Sent-Event
streams. The HDL200, HDL300 and Dual HDL300 reach the same API only through
the Nureva App on a room computer, so they are not covered.

Protocol reference (the manufacturer's own): Nureva's local API docs,
https://developers-local.nureva.com (the getting-started guide, the local
control and sound location tutorials, and the API reference, whose pages each
embed the endpoint's OpenAPI 3.1 definition).

Requests:
  Every request carries two headers the device insists on (a 400 without
  them): Nureva-Client-Id and Nureva-Client-Version. The driver sends
  "OpenAVC" and its own version. POST /api/v1/auth/login with the ``general``
  account and its password returns ``authParameters``; every other request
  sends it as ``Authorization: Nureva <authParameters>``. A 401 later in the
  session (the password was changed) logs in once more on the same
  connection; a second refusal is ``auth_failed``. The device limits clients
  to 600 requests a minute (60 on the streams) and answers past that with
  "Too many requests"; that is never treated as a wrong password.

Push and polling:
  /api/v1/events streams configuration and status changes: calibration
  started and completed, the device information, the room layout and its
  zones, the zone-to-camera map, the camera switcher, the network
  configuration, the network and Console LEDs, USB and the speaker bars'
  connection. Audio settings (mute, volume, treble and bass, the rest) are
  not in that stream, so GET /api/v1/audio is polled every ``poll_interval``
  seconds, and re-read straight after every write. Everything else is read
  once on connect, again whenever the event stream reopens, and once a
  minute as a resync.

  /api/v1/data streams sound location every 200 ms (where the loudest sound
  is, its level, and which camera zone it falls in) and the room's
  background noise every 5 seconds. Sound location is throttled to one state
  write per ``sound_location_interval`` (the newest reading always lands) and
  relays to the cloud at low priority. Each camera zone configured in Nureva
  Console is a child entity with an ``active`` flag, which is what a macro or
  trigger recalling a camera preset keys off.

Volume is relative only: the API steps it up or down by about one step, and
``speaker_volume`` (0 to 20) is the device's own reading. There is no way to
set an absolute level, so the driver offers none.

Why Python (not YAML):
  Two things the declarative format cannot express. The login is a POST
  whose reply carries a token that must then go out under the device's own
  scheme word (``Authorization: Nureva <token>``); no YAML ``auth.type`` or
  HTTP ``auth_type`` produces that. And /api/v1/events multiplexes fourteen
  kinds of event on one stream and names each only in the SSE ``event:``
  line, while the YAML ``sse`` push hands a driver the data alone:
  calibration and USB both send ``{"status": ...}``, two events carry no
  body at all, and the LED event goes to different state depending on a
  field inside it. The fixed headers alone would have fitted YAML
  (``default_headers``).

The documents disagree with themselves in a few places, and the driver reads
both spellings where they do: a zone's ``id`` / ``label`` (reference schema,
tutorial) or ``zoneId`` / ``ZoneLabel`` (the HDL410 example);
``cameraSwitcherZoneInputMap`` or ``cameraSwitcherZoneInputMaps``; the USB
event's ``status`` (schema) or ``connected`` (tutorial); a triggered zone's
``type`` as a string or a list.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

log = get_logger(__name__)

# The two headers every request must carry (API client guide). The id names
# the integration; the version is the driver's own.
_CLIENT_ID = "OpenAVC"

# The account a third-party integration logs in as (Authorization guide).
_ACCOUNT = "general"

# The models this driver controls: the API runs in their connect module.
_MODELS = ("hdl310", "hdl410")

_LOGIN = "/api/v1/auth/login"
_AUDIO = "/api/v1/audio"
_VOLUME = "/api/v1/audio/volume/change"
_CALIBRATE = "/api/v1/audio/calibrate"
_IDENTIFY = "/api/v1/audio/identify"
_HARDWARE = "/api/v1/audio/hardware"
_STATUS = "/api/v1/status"
_LAYOUT = "/api/v1/room/layout"
_PROFILES = "/api/v1/room/profiles"
_SWITCHER = "/api/v1/integrations/camera-switcher"
_NETWORK = "/api/v1/network/configuration"
_EVENTS = "/api/v1/events"
_DATA = "/api/v1/data"

# The event stream carries no documented keepalive. After this much silence
# it is reopened, which also resyncs everything it would have reported.
_EVENTS_IDLE_REOPEN_S = 300.0

# The data stream sends background noise every 5 s even when sound location
# is off, so this much silence means the stream is dead.
_DATA_IDLE_REOPEN_S = 30.0

# Everything outside the audio settings is read again this often, in case an
# event was missed.
_RESYNC_INTERVAL_S = 60.0

# GET /api/v1/audio attribute -> state variable.
_AUDIO_FIELDS: dict[str, str] = {
    "microphoneMute": "microphone_mute",
    "audienceMute": "audience_mute",
    "speakerVolume": "speaker_volume",
    "speakerTrebleLevel": "speaker_treble",
    "speakerBassLevel": "speaker_bass",
    "microphoneGain": "microphone_gain",
    "echoReductionLevel": "echo_reduction",
    "noiseReductionLevel": "noise_reduction",
    "auxiliaryOutputState": "aux_output_mode",
    "voiceAmplificationEnabled": "voice_amplification",
    "voiceAmplificationLevel": "voice_amplification_level",
    "voiceAmplificationAuxInLevel": "voice_amplification_aux_in",
    "dynamicBoostEnabled": "dynamic_boost",
    "microphoneDuckingEnabled": "microphone_ducking",
    "voiceAmplificationGateThreshold": "voice_amp_gate_threshold",
    "voiceAmplificationUsbOutputGainLevel": "voice_amp_usb_gain",
}
_AUDIO_FIELD_BY_KEY = {key: field for field, key in _AUDIO_FIELDS.items()}

# Device settings written through the camera switching defaults, which the
# device takes only as a complete object.
_SWITCHING_DEFAULTS = {
    "default_camera_input": "defaultCameraInputPort",
    "zone_trigger_wait_ms": "zonesTriggerWaitTime",
    "switch_to_default_wait_ms": "switchToDefaultWaitTime",
}

_ZONE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "on", "yes")


def _first(obj: dict[str, Any], *names: str) -> Any:
    """The first of ``names`` present in ``obj`` (the docs spell some fields
    two ways)."""
    for name in names:
        if name in obj:
            return obj[name]
    return None


class NurevaHdl310Hdl410Driver(BaseDriver):
    """Nureva HDL310 / HDL410 over the connect module's local API."""

    DRIVER_INFO = {
        "id": "nureva_hdl310_hdl410",
        "name": "Nureva HDL310 / HDL410",
        "manufacturer": "Nureva",
        "category": "audio",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": (
            "Controls a Nureva HDL310 or HDL410 audio conferencing system "
            "over the local API in its connect module (HTTPS, port 443). "
            "Microphone mute, audience mute, volume up and down, treble and "
            "bass, echo and noise reduction, Adaptive Voice Amplification, "
            "dynamic boost, microphone ducking, the auxiliary output, "
            "calibration and identify, HDL410 room profiles, the camera "
            "switcher's defaults, and its status lights. Reports where the "
            "loudest sound in the space is and which camera zone it falls in, "
            "so a trigger can recall a camera preset when a zone becomes "
            "active. Status and configuration changes arrive as they happen; "
            "audio settings are polled. Does not control the HDL200, HDL300 "
            "or Dual HDL300."
        ),
        "source_url": "https://developers-local.nureva.com",
        "tags": [
            "microphone", "speaker-bar", "conferencing", "sound-location",
            "camera-tracking", "voice-amplification",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["nureva-local-api"],
        "ports": [443],
        "min_platform_version": "0.36.0",
        "compatible_models": [
            {
                "manufacturer": "Nureva",
                "models": ["HDL310", "HDL410"],
                "confidence": "untested",
                "notes": (
                    "Built from Nureva's local API documentation; not yet run "
                    "against a connect module. Room profiles are HDL410 only. "
                    "Some settings need newer firmware (dynamic boost and "
                    "microphone ducking 1.9, the voice amplification gate and "
                    "USB gain 2.0, microphone gain 1.3 to 1.x); the device "
                    "refuses them on older firmware and the device page shows "
                    "why."
                ),
            },
        ],
        "transport": "http",
        "help": {
            "overview": (
                "Controls a Nureva HDL310 or HDL410 conferencing system: mute "
                "the microphones, step the volume, adjust treble and bass and "
                "the audio processing, run a calibration, switch HDL410 room "
                "profiles, and watch the system's network, Console, USB and "
                "speaker bar status. The system reports where the loudest "
                "sound in the space is; each camera zone set up in Nureva "
                "Console appears under Zones with an Active flag, so a trigger "
                "can recall a camera preset when someone talks in that zone."
            ),
            "setup": (
                "1. Update the system's firmware and give the connect module "
                "a static IP address in the Nureva App.\n"
                "2. Enter that IP address here.\n"
                "3. Enter the password of the system's general account: the "
                "third-party password set in the Nureva App (Settings > Device "
                "password). A new or factory-reset system uses its enrollment "
                "code (XXX-XXX-XXX, on the bottom of the connect module); a "
                "system installed before Nureva introduced the enrollment-code "
                "default may still have a blank password, so leave the field "
                "empty for one of those.\n"
                "4. Camera zones and the camera switcher are set up in Nureva "
                "Console; they appear here once they exist."
            ),
            "connection": (
                "HTTPS on port 443 to the connect module, with the general "
                "account's password. The connect module's certificate is "
                "self-signed, so certificate verification is off unless you "
                "turn it on."
            ),
        },
        "default_config": {
            "host": "",
            "port": 443,
            "ssl": True,
            "verify_ssl": False,
            "password": "",
            "poll_interval": 2,
            "timeout": 5.0,
            "enable_sound_location": True,
            "sound_location_interval": 0.5,
        },
        "config_schema": {
            "host": {
                "type": "string", "required": True, "label": "IP Address",
                "help": "The connect module's IP address, as set in the Nureva App.",
            },
            "port": {
                "type": "integer", "default": 443, "label": "Port",
                "min": 1, "max": 65535, "advanced": True,
                "help": "The connect module's HTTPS port. 443 unless your network maps it elsewhere.",
            },
            "password": {
                "type": "string", "secret": True, "default": "",
                "label": "Password",
                "help": (
                    "The general account's password: the third-party password "
                    "set in the Nureva App, or the enrollment code on a new "
                    "system. Leave it empty for an older system whose password "
                    "was never set."
                ),
            },
            "verify_ssl": {
                "type": "boolean", "default": False, "label": "Verify TLS Certificate",
                "advanced": True,
                "help": "The connect module's certificate is self-signed, so leave this off unless you have installed a trusted one.",
            },
            "poll_interval": {
                "type": "integer", "default": 2, "label": "Audio Poll Interval (s)",
                "min": 0, "max": 60,
                "help": (
                    "How often mute, volume and the other audio settings are "
                    "read: how quickly a mute made on the room computer or "
                    "the system itself shows up here. 0 stops reading them."
                ),
            },
            "timeout": {
                "type": "number", "default": 5.0, "label": "Request Timeout (s)",
                "min": 1, "max": 30, "advanced": True,
                "help": "How long to wait for each answer from the connect module.",
            },
            "enable_sound_location": {
                "type": "boolean", "default": True, "label": "Report Sound Location",
                "help": (
                    "Read where the loudest sound in the space is and which "
                    "camera zone it falls in. Zone triggers need it. The "
                    "Sound Location On and Off commands turn it on and off "
                    "while the system is running."
                ),
            },
            "sound_location_interval": {
                "type": "number", "default": 0.5, "label": "Sound Location Update Interval (s)",
                "min": 0.2, "max": 10, "advanced": True,
                "help": (
                    "The fastest the sound location reading updates here. The "
                    "newest reading is never dropped."
                ),
            },
        },
        "state_variables": {
            # Identity (GET /api/v1/audio/hardware)
            "model": {"type": "enum", "values": ["hdl310", "hdl410"], "label": "Model"},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "device_version": {"type": "string", "label": "Device Version"},
            "hardware_id": {"type": "string", "label": "Hardware ID"},
            "bar_count": {
                "type": "integer", "label": "Speaker Bars", "min": 0, "max": 2,
                "help": "Microphone and speaker bars the connect module reports.",
            },
            "ip_address": {"type": "string", "label": "IP Address"},
            "mac_address": {"type": "string", "label": "MAC Address"},
            "enrollment_status": {
                "type": "enum", "values": ["enrolled", "unEnrolled"],
                "label": "Nureva Console Enrollment",
            },
            "device_status": {
                "type": "enum", "values": ["Ok", "CableUnplugged", "Disconnected"],
                "label": "System Status",
                "help": "Ok, CableUnplugged (a speaker bar cable is out) or Disconnected.",
            },
            # Room status (GET /api/v1/status, ledStateUpdated / usbConnection /
            # deviceComponentsConnection events)
            "network_led_colour": {
                "type": "enum", "values": ["none", "red", "yellow", "green"],
                "label": "Network Light Colour",
                "help": "Green: connected with an IP address. Yellow: direct connection, no gateway or DHCP address. Red: no IP address (blinking red: power cycle needed). None: no network.",
            },
            "network_led_state": {
                "type": "enum", "values": ["off", "solid", "pulsing", "blinking", "flashing"],
                "label": "Network Light State",
            },
            "console_led_colour": {
                "type": "enum", "values": ["none", "red", "yellow", "green"],
                "label": "Console Light Colour",
                "help": "Green: talking to Nureva Console. Yellow: internet but not Console. Red: neither. None: not connected.",
            },
            "console_led_state": {
                "type": "enum", "values": ["off", "solid", "pulsing", "blinking", "flashing"],
                "label": "Console Light State",
            },
            "usb_status": {
                "type": "enum", "values": ["connected", "disconnected", "unknown"],
                "label": "USB to Room Computer",
            },
            "components_status": {
                "type": "enum", "values": ["connected", "disconnected", "unknown"],
                "label": "Speaker Bars Connected",
                "help": "connected: every bar is connected. disconnected: one or more is not.",
            },
            # Audio settings (GET /api/v1/audio, polled)
            "microphone_mute": {
                "type": "boolean", "label": "Microphone Mute", "control": True,
                "cloud_priority": "high",
            },
            "audience_mute": {
                "type": "boolean", "label": "Audience Mute", "control": True,
                "help": "Mutes the system's own microphones so remote participants hear only the presenter's microphone. Needs Adaptive Voice Amplification on; returns to off when a conference ends.",
            },
            "speaker_volume": {
                "type": "integer", "label": "Speaker Volume", "min": 0, "max": 20,
                "step": 1, "control": True,
                "help": "The system's own reading, 0 (silent) to 20. Change it with Volume Up and Volume Down.",
            },
            "speaker_treble": {
                "type": "integer", "label": "Treble", "min": 0, "max": 100, "step": 1,
                "control": True,
            },
            "speaker_bass": {
                "type": "integer", "label": "Bass", "min": 0, "max": 100, "step": 1,
                "control": True,
            },
            "microphone_gain": {
                "type": "integer", "label": "Microphone Gain", "min": -6, "max": 12,
                "step": 1, "unit": "dB",
                "help": "Gain on the processed microphone signal sent to the room computer. Firmware 1.3 to 1.x only.",
            },
            "echo_reduction": {
                "type": "enum", "values": ["Low", "Medium", "High"], "label": "Echo Reduction",
            },
            "noise_reduction": {
                "type": "enum", "values": ["Low", "Medium", "High"], "label": "Noise Reduction",
            },
            "aux_output_mode": {
                "type": "enum",
                "values": ["MicLevel", "LineLevel", "SpeakerOut", "MixedSignal", "SpeakerRef"],
                "label": "Auxiliary Output Mode",
            },
            "voice_amplification": {
                "type": "boolean", "label": "Adaptive Voice Amplification", "control": True,
            },
            "voice_amplification_level": {
                "type": "integer", "label": "Voice Amplification Level", "min": 0, "max": 40,
                "step": 1, "control": True,
                "help": "0 to 40. Nureva recommends 10 to 30 (0 dB to 20 dB).",
            },
            "voice_amplification_aux_in": {
                "type": "enum", "values": ["Mic", "Line"], "label": "Voice Amplification Input Level",
            },
            "dynamic_boost": {"type": "boolean", "label": "Dynamic Boost"},
            "microphone_ducking": {"type": "boolean", "label": "Microphone Ducking"},
            "voice_amp_gate_threshold": {
                "type": "integer", "label": "External Mic Gate Threshold", "min": 0, "max": 256,
            },
            "voice_amp_usb_gain": {
                "type": "integer", "label": "External Mic USB Gain", "min": 0, "max": 40,
            },
            # Calibration (event stream)
            "calibrating": {
                "type": "boolean", "label": "Calibrating",
                "help": "True while a calibration runs (about 20 seconds of static from the speakers).",
            },
            # Sound location (data stream)
            "sound_location_feed": {
                "type": "boolean", "label": "Sound Location Reporting",
                "help": "Whether the driver is reading sound location (Report Sound Location, or the Sound Location On and Off commands).",
            },
            "sound_location_status": {
                "type": "string", "label": "Sound Location Status",
                "help": "Empty while readings arrive; otherwise the system's reason there are none (Microphone muted, Speaker bar disconnected, Unsupported device, Firmware update in progress).",
            },
            "sound_detected": {
                "type": "boolean", "label": "Sound Detected", "cloud_priority": "low",
                "help": "True when the latest reading has a sound level above 0 dB. At 0 dB the system detected nothing meaningful.",
            },
            "sound_power_db": {
                "type": "number", "label": "Sound Level", "unit": "dB",
                "cloud_priority": "low",
                "help": "Level of the loudest sound. Voices are usually above 40 dB.",
            },
            "sound_x_mm": {
                "type": "integer", "label": "Sound Position X", "unit": "mm",
                "cloud_priority": "low",
                "help": "Left (negative) or right of the centre back of the bar in port 1. Updated only while a sound is detected.",
            },
            "sound_y_mm": {
                "type": "integer", "label": "Sound Position Y", "unit": "mm",
                "cloud_priority": "low",
                "help": "Distance out from the bar in port 1. Updated only while a sound is detected.",
            },
            "active_zone_id": {
                "type": "string", "label": "Active Zone ID", "cloud_priority": "low",
                "help": "The camera zone the latest reading falls in, empty when none.",
            },
            "active_zone_label": {
                "type": "string", "label": "Active Zone", "cloud_priority": "low",
                "help": "The name of that zone, as set in Nureva Console. Empty when the sound is in no zone.",
            },
            "background_noise_db": {
                "type": "integer", "label": "Background Noise", "unit": "dB",
                "cloud_priority": "low",
                "help": "Background noise the system measures, every 5 seconds.",
            },
            "zone_count": {"type": "integer", "label": "Camera Zones", "min": 0, "max": 8},
            "sound_location_algorithm": {
                "type": "enum", "values": ["BP", "TDOA"], "label": "Sound Location Algorithm",
            },
            # Camera switcher (GET /api/v1/integrations/camera-switcher, GET
            # /api/v1/room/layout)
            "camera_switcher_enabled": {"type": "boolean", "label": "Camera Switcher Enabled"},
            "camera_switcher_model": {"type": "string", "label": "Camera Switcher Model"},
            "camera_switcher_address": {"type": "string", "label": "Camera Switcher Address"},
            "active_camera_input": {
                "type": "string", "label": "Active Camera Input",
                "help": "The camera switcher input in use (USB1, USB2 or HDMI), empty when no switcher reports one.",
            },
            "camera_switcher_error": {
                "type": "string", "label": "Camera Switcher Error",
                "help": "What the camera switcher integration reports wrong, empty when nothing.",
            },
            "default_camera_input": {
                "type": "enum", "values": ["HDMI", "USB1", "USB2"], "label": "Default Camera Input",
            },
            "zone_trigger_wait_ms": {
                "type": "integer", "label": "Zone Trigger Wait", "min": 0, "unit": "ms",
                "help": "How long sound must stay in a zone before the camera switcher switches to it.",
            },
            "switch_to_default_wait_ms": {
                "type": "integer", "label": "Switch to Default Wait", "min": 0, "unit": "ms",
                "help": "How long with no sound before the camera switcher goes back to the default camera.",
            },
            # Room profiles (HDL410)
            "room_profile": {
                "type": "string", "label": "Room Profile",
                "help": "The active room profile (HDL410). Empty on an HDL310.",
            },
            "room_profile_id": {"type": "string", "label": "Room Profile ID"},
            "room_profile_options": {
                "type": "string", "label": "Room Profiles",
                "help": "The room profiles the system lists, for the Activate Room Profile picker.",
            },
            # Network (GET /api/v1/network/configuration)
            "network_static": {"type": "boolean", "label": "Static IP Address"},
            "subnet_mask": {"type": "string", "label": "Subnet Mask"},
            "gateway": {"type": "string", "label": "Gateway"},
            "dns_servers": {"type": "string", "label": "DNS Servers"},
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": "The last thing the system refused, and why.",
            },
        },
        "child_entity_types": {
            "zone": {
                "label": "Zone",
                "label_plural": "Zones",
                "id_format": {"type": "string", "max_length": 128},
                "state_variables": {
                    "label": {"type": "string", "label": "Name"},
                    "active": {
                        "type": "boolean", "label": "Active", "cloud_priority": "low",
                        "help": "True while the latest sound location reading falls in this zone.",
                    },
                    "camera_input": {
                        "type": "string", "label": "Camera Input",
                        "help": "The camera switcher input mapped to this zone in Nureva Console (USB1, USB2 or HDMI), empty when none.",
                    },
                },
                "summary_fields": ["label", "active", "camera_input"],
                "label_field": "label",
            },
        },
        "commands": {
            "mute_microphone": {
                "label": "Mute Microphone",
                "help": "Mutes the system's microphones; remote participants hear nothing from the space.",
                "sets": {"microphone_mute": True},
            },
            "unmute_microphone": {
                "label": "Unmute Microphone",
                "help": "Unmutes the system's microphones.",
                "sets": {"microphone_mute": False},
            },
            "toggle_microphone_mute": {
                "label": "Toggle Microphone Mute",
                "help": "Mutes the microphones if they are on, unmutes them if they are muted.",
            },
            "volume_up": {
                "label": "Volume Up",
                "help": "Raises the speaker volume one step, as the remote control's volume button does.",
            },
            "volume_down": {
                "label": "Volume Down",
                "help": "Lowers the speaker volume one step, as the remote control's volume button does.",
            },
            "audience_mute_on": {
                "label": "Audience Mute On",
                "help": "Mutes the system's own microphones so remote participants hear only the presenter's microphone. Needs Adaptive Voice Amplification on.",
                "sets": {"audience_mute": True},
            },
            "audience_mute_off": {
                "label": "Audience Mute Off",
                "help": "Turns the system's own microphones back on for remote participants.",
                "sets": {"audience_mute": False},
            },
            "voice_amplification_on": {
                "label": "Voice Amplification On",
                "help": "Turns on Adaptive Voice Amplification: the presenter's wireless microphone plays through the speakers.",
                "sets": {"voice_amplification": True},
            },
            "voice_amplification_off": {
                "label": "Voice Amplification Off",
                "help": "Turns off Adaptive Voice Amplification.",
                "sets": {"voice_amplification": False},
            },
            "set_treble": {
                "label": "Set Treble",
                "help": "Sets the speakers' treble level.",
                "params": {
                    "level": {"type": "integer", "required": True, "min": 0, "max": 100, "label": "Level"},
                },
                "sets": {"speaker_treble": "{level}"},
            },
            "set_bass": {
                "label": "Set Bass",
                "help": "Sets the speakers' bass level.",
                "params": {
                    "level": {"type": "integer", "required": True, "min": 0, "max": 100, "label": "Level"},
                },
                "sets": {"speaker_bass": "{level}"},
            },
            "identify": {
                "label": "Identify",
                "help": "Lights LEDs on the speaker bars: one on the bar in port 1, two on the bar in port 2. Firmware 1.8 or newer.",
            },
            "activate_room_profile": {
                "label": "Activate Room Profile",
                "help": "Switches to another room profile, with its own camera zones and camera mapping (HDL410).",
                "params": {
                    "profile": {
                        "type": "string", "required": True, "label": "Room Profile",
                        "options_state": "room_profile_options",
                    },
                },
            },
            "sound_location_on": {
                "label": "Sound Location On",
                "help": "Starts reading where the loudest sound in the space is and which camera zone it falls in.",
                "sets": {"sound_location_feed": True},
            },
            "sound_location_off": {
                "label": "Sound Location Off",
                "help": "Stops reading sound location. Zones stop changing until it is turned on again.",
                "sets": {"sound_location_feed": False},
            },
            "calibrate": {
                "label": "Calibrate",
                "help": "Runs a manual calibration: the speakers play loud static for about 20 seconds. The space should be quiet.",
                "confirm": (
                    "The speakers play loud static for about 20 seconds, and "
                    "anyone on a call hears nothing from this space until it ends."
                ),
            },
        },
        "quick_actions": [
            "mute_microphone", "unmute_microphone", "volume_up", "volume_down",
        ],
        "device_settings": {
            "speaker_treble": {
                "type": "integer", "label": "Treble", "default": 50, "min": 0, "max": 100,
                "setup": False,
                "help": "Speaker treble, 0 to 100. Changing it can change the speaker preset chosen in the Nureva App.",
            },
            "speaker_bass": {
                "type": "integer", "label": "Bass", "default": 50, "min": 0, "max": 100,
                "setup": False,
                "help": "Speaker bass, 0 to 100. Changing it can change the speaker preset chosen in the Nureva App.",
            },
            "microphone_gain": {
                "type": "integer", "label": "Microphone Gain (dB)", "default": 0,
                "min": -6, "max": 12, "setup": False,
                "help": "Gain on the processed microphone signal sent to the room computer, -6 to +12 dB. Firmware 1.3 to 1.x only.",
            },
            "echo_reduction": {
                "type": "enum", "label": "Echo Reduction", "default": "Medium",
                "values": ["Low", "Medium", "High"], "setup": False,
                "help": "Lower it if remote participants hear voices from the space cutting out; raise it if they hear their own voices echo. Medium is recommended.",
            },
            "noise_reduction": {
                "type": "enum", "label": "Noise Reduction", "default": "Medium",
                "values": ["Low", "Medium", "High"], "setup": False,
                "help": "Lower it if remote participants hear voices cutting out; raise it if they hear fans or air handling. Medium is recommended.",
            },
            "aux_output_mode": {
                "type": "enum", "label": "Auxiliary Output Mode", "default": "LineLevel",
                "values": ["MicLevel", "LineLevel", "SpeakerOut", "MixedSignal", "SpeakerRef"],
                "setup": False,
                "help": "What the auxiliary output carries, for another audio system.",
            },
            "voice_amplification": {
                "type": "boolean", "label": "Adaptive Voice Amplification", "default": False,
                "setup": False,
                "help": "Plays a presenter's wireless microphone through the speakers.",
            },
            "voice_amplification_level": {
                "type": "integer", "label": "Voice Amplification Level", "default": 25,
                "min": 0, "max": 40, "setup": False,
                "help": "0 to 40. Nureva recommends 10 to 30, which is 0 dB to 20 dB.",
            },
            "voice_amplification_aux_in": {
                "type": "enum", "label": "Voice Amplification Input Level", "default": "Mic",
                "values": ["Mic", "Line"], "setup": False,
                "help": "Mic or Line, as the wireless microphone receiver's manual says.",
            },
            "dynamic_boost": {
                "type": "boolean", "label": "Dynamic Boost", "default": False, "setup": False,
                "help": "Boosts speaker output for speech in large or difficult spaces. Firmware 1.9 or newer.",
            },
            "microphone_ducking": {
                "type": "boolean", "label": "Microphone Ducking", "default": False, "setup": False,
                "help": "Stops remote participants hearing the presenter twice while Adaptive Voice Amplification is on. Firmware 1.9 or newer.",
            },
            "voice_amp_gate_threshold": {
                "type": "integer", "label": "External Mic Gate Threshold", "default": 65,
                "min": 0, "max": 256, "setup": False,
                "help": "Level that opens the external microphone's gate, 0 to 256. Firmware 2.0 or newer.",
            },
            "voice_amp_usb_gain": {
                "type": "integer", "label": "External Mic USB Gain", "default": 10,
                "min": 0, "max": 40, "setup": False,
                "help": "Gain of the external microphone in what remote participants and recordings hear, 0 to 40. Firmware 2.0 or newer.",
            },
            "camera_switcher_enabled": {
                "type": "boolean", "label": "Camera Switcher Enabled", "default": False,
                "setup": False,
                "help": "Turns the camera switcher integration set up in Nureva Console on or off.",
            },
            "default_camera_input": {
                "type": "enum", "label": "Default Camera Input", "default": "HDMI",
                "values": ["HDMI", "USB1", "USB2"], "setup": False,
                "help": "The camera the switcher goes back to when no sound is detected in a zone.",
            },
            "zone_trigger_wait_ms": {
                "type": "integer", "label": "Zone Trigger Wait (ms)", "default": 1000,
                "min": 0, "setup": False,
                "help": "How long sound must stay in a zone before the camera switcher switches to it.",
            },
            "switch_to_default_wait_ms": {
                "type": "integer", "label": "Switch to Default Wait (ms)", "default": 5000,
                "min": 0, "setup": False,
                "help": "How long with no sound before the camera switcher goes back to the default camera.",
            },
            "sound_location_algorithm": {
                "type": "enum", "label": "Sound Location Algorithm", "default": "BP",
                "values": ["BP", "TDOA"], "setup": False,
                "help": "BP is the default. TDOA needs the room dimensions and bar positions set accurately in the Nureva App first. Firmware 2.0 or newer.",
            },
        },
        "discovery": {
            # GET /api/v1 answers without credentials (the capabilities
            # endpoint needs no role) with the device's own OpenAPI
            # description, titled as every reference page's is. The two
            # headers the device insists on ride in the request; without them
            # it answers 400 naming the missing header. HTTPS only, with a
            # self-signed certificate, so the probe is a TLS GET.
            "tcp_probe": {
                "port": 443,
                "tls": True,
                "send_ascii": (
                    "GET /api/v1 HTTP/1.1\r\n"
                    "Host: nureva\r\n"
                    "Nureva-Client-Id: OpenAVC\r\n"
                    "Nureva-Client-Version: 1.0.0\r\n"
                    "Connection: close\r\n\r\n"
                ),
                "expect_regex": '"title"\\s*:\\s*"Nureva Developer Toolkit"',
                "extract_manufacturer": "Nureva",
                "timeout_ms": 3000,
            },
            # Nureva, Inc. (IEEE MA-L), its only block in the registry.
            "oui": ["e0:e7:bb"],
            "manufacturer_alias": ["Nureva"],
        },
    }

    # ── Construction ──

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._auth: str = ""
        self._events_task: asyncio.Task | None = None
        self._data_task: asyncio.Task | None = None
        self._resync_task: asyncio.Task | None = None
        # Reads started from the event stream, cancelled with the session.
        self._side_tasks: set[asyncio.Task] = set()
        # One login at a time; a caller whose token is already stale when it
        # gets the lock uses the new one instead of logging in again.
        self._login_lock = asyncio.Lock()
        # Endpoints the device answered "Unsupported device type" for.
        self._unsupported: set[str] = set()
        self._sound_location = True
        self._sound_location_refused = False
        self._last_reply = 0.0
        self._last_resync = 0.0
        # Sound location throttle: the newest reading, held until its window
        # ends.
        self._last_location_write = 0.0
        self._pending_location: dict[str, Any] | None = None
        self._location_timer: asyncio.TimerHandle | None = None
        # Raw zone id (as the device sends it) -> child local id.
        self._zone_ids: dict[str, str] = {}
        self._zone_inputs: dict[str, str] = {}
        self._active_zone = ""

    # ── Connection lifecycle ──

    def _base_url(self) -> str:
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 443) or 443)
        return f"https://{host}:{port}"

    def _client_headers(self) -> dict[str, str]:
        return {
            "Nureva-Client-Id": _CLIENT_ID,
            "Nureva-Client-Version": str(self.DRIVER_INFO["version"]),
        }

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ConnectionFaultError(
                "The connect module's IP address is required.", code="invalid_config",
            )
        self._sound_location = _as_bool(self.config.get("enable_sound_location", True))

    async def _create_transport(self, transport_type: str) -> None:
        """Driver-owned session: one httpx client for requests and both
        streams. ``self.transport`` stays None; _link_alive()/_close_session()
        report and retire the client instead."""
        timeout = float(self.config.get("timeout", 5.0) or 5.0)
        self._client = httpx.AsyncClient(
            base_url=self._base_url(),
            headers=self._client_headers(),
            verify=_as_bool(self.config.get("verify_ssl", False)),
            timeout=timeout,
        )
        self._auth = ""
        self._unsupported = set()
        self._sound_location_refused = False

    async def _post_connect(self) -> None:
        """Log in and prove the device is an HDL310 or HDL410 before
        `connected` is declared."""
        host = str(self.config.get("host", "")).strip()
        try:
            await self._login()
            hardware = await self._get_json(_HARDWARE)
        except httpx.ConnectError as exc:
            text = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
                raise ConnectionFaultError(
                    "The connect module's TLS certificate is not trusted. Turn "
                    "off \"Verify TLS Certificate\" for this device, or install "
                    "a trusted certificate on it.",
                    code="tls_cert_untrusted",
                ) from exc
            raise ConnectionError(
                f"Could not reach the connect module at {host}: {exc}"
            ) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(
                f"Could not reach the connect module at {host}: {exc}"
            ) from exc
        if hardware is None:
            raise ConnectionError(
                f"The device at {host} did not answer its device information request."
            )
        model = str(hardware.get("model", "")).strip().lower()
        if model not in _MODELS:
            raise ConnectionFaultError(
                f"The device at {host} reports model \"{model or 'unknown'}\". "
                f"This driver controls the Nureva HDL310 and HDL410; an HDL200, "
                f"HDL300 or Dual HDL300 is reached through the Nureva App on "
                f"the room computer instead.",
                code="invalid_config",
            )
        self._apply_hardware(hardware)
        self.set_state("last_error", None)
        log.info(f"[{self.device_id}] Connected to Nureva {model.upper()} at {host}")

    async def _initial_sync(self) -> None:
        await self._refresh_audio()
        await self._resync()
        self.set_state("sound_location_feed", self._sound_location)
        self._events_task = asyncio.create_task(self._events_loop())
        self._data_task = asyncio.create_task(self._data_loop())

    def _link_alive(self) -> bool:
        return self._client is not None

    async def _close_session(self) -> None:
        tasks = [self._events_task, self._data_task, self._resync_task, *self._side_tasks]
        self._events_task = self._data_task = self._resync_task = None
        self._side_tasks = set()
        for task in tasks:
            if task is None or task is asyncio.current_task():
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._location_timer is not None:
            self._location_timer.cancel()
            self._location_timer = None
        self._pending_location = None
        client, self._client = self._client, None
        self._auth = ""
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self) -> None:
        """Any answer from the connect module in the last interval counts: the
        audio poll and both streams already prove the link most of the time.
        Otherwise ask for the room status; any HTTP answer, a refusal
        included, means the device is there."""
        if time.monotonic() - self._last_reply < float(self.HEALTH_INTERVAL_S):
            return
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        headers = {"Authorization": f"Nureva {self._auth}"} if self._auth else {}
        await client.get(_STATUS, headers=headers)
        self._last_reply = time.monotonic()

    # ── Requests ──

    async def _login(self) -> None:
        """POST the general account's password; keep the authorization
        parameter the device returns. A blank password is valid on a system
        whose password was never set, so it is sent as it is."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        password = str(self.config.get("password", "") or "")
        response = await client.post(
            _LOGIN, json={"account": _ACCOUNT, "password": password},
        )
        self._last_reply = time.monotonic()
        if response.status_code == 401:
            raise ConnectionFaultError(
                "The connect module refused the password for its general "
                "account. Enter the third-party password set in the Nureva "
                "App (or the enrollment code on a new system).",
                code="auth_failed",
            )
        if self._rate_limited(response):
            raise ConnectionError(
                "The connect module is limiting requests (too many in the last "
                "minute); trying again shortly."
            )
        if not response.is_success:
            raise ConnectionError(
                f"The connect module refused the login: {self._refusal_reason(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectionError(
                "The connect module answered the login with something other than JSON."
            ) from exc
        token = str(payload.get("authParameters", "") or "") if isinstance(payload, dict) else ""
        if not token:
            raise ConnectionError(
                "The connect module's login reply carried no authorization parameter."
            )
        self.redact_in_log(token)
        self._auth = token

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Nureva {self._auth}"} if self._auth else {}

    async def _relogin(self, stale_token: str) -> None:
        """Log in again after a 401, once for everyone who saw it. A refused
        password drops the connection as ``auth_failed`` straight away, so
        the platform stops retrying instead of the poll loop sending the
        wrong password three more times."""
        async with self._login_lock:
            if self._auth and self._auth != stale_token:
                return  # another caller already logged in again
            log.info(f"[{self.device_id}] Authorization refused; logging in again")
            try:
                await self._login()
            except ConnectionFaultError as exc:
                self._lose_auth(str(exc))
                raise

    def _lose_auth(self, message: str) -> None:
        if getattr(self, "_connected", False):
            self._force_disconnect("auth_failed", message)

    async def _request(
        self, method: str, path: str, body: Any = None, *, relogin: bool = True,
    ) -> httpx.Response:
        """One authenticated request. Transport errors propagate (the poll
        contract). A 401 logs in again once on this connection; a second one
        is ``auth_failed``. Other statuses come back to the caller."""
        client = self._client
        if client is None:
            raise ConnectionError("Not connected")
        token = self._auth
        kwargs: dict[str, Any] = {"headers": self._auth_headers()}
        if body is not None:
            kwargs["json"] = body
        response = await client.request(method, path, **kwargs)
        self._last_reply = time.monotonic()
        if response.status_code == 401:
            message = (
                "The connect module refused the general account's password. "
                "Check the third-party password set in the Nureva App."
            )
            if relogin:
                await self._relogin(token)
                return await self._request(method, path, body, relogin=False)
            self._lose_auth(message)
            raise ConnectionFaultError(message, code="auth_failed")
        return response

    async def _get_json(self, path: str) -> dict[str, Any] | None:
        """GET a resource. None when the device says this model does not have
        it (remembered, so it is not asked again) or is limiting requests."""
        if path in self._unsupported:
            return None
        response = await self._request("GET", path)
        if response.is_success:
            try:
                payload = response.json()
            except ValueError as exc:
                raise ConnectionError(
                    f"The connect module answered {path} with something other than JSON."
                ) from exc
            return payload if isinstance(payload, dict) else {}
        if self._rate_limited(response):
            log.info(f"[{self.device_id}] Rate limited reading {path}; skipping this time")
            return None
        reason = self._refusal_reason(response)
        if response.status_code in (404, 409) and "too many" not in reason.lower():
            self._unsupported.add(path)
            log.info(f"[{self.device_id}] {path} is not available on this system ({reason})")
            return None
        raise ConnectionError(f"The connect module answered HTTP {response.status_code} for {path}: {reason}")

    async def _write(self, method: str, path: str, body: Any, what: str) -> None:
        """Send a change. A refusal names what and why, goes to last_error,
        and raises; the state is never assumed, it is read back."""
        response = await self._request(method, path, body)
        if response.is_success:
            return
        reason = self._refusal_reason(response)
        message = f"The system refused {what}: {reason}"
        self.set_state("last_error", message)
        raise ValueError(message)

    @staticmethod
    def _rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code == 409:
            # The API client guide says 409 for the rate limit; the reference
            # pages say 429 and use 409 for an unsupported model. The body
            # tells them apart.
            return "too many requests" in (response.text or "").lower()
        return False

    @staticmethod
    def _refusal_reason(response: httpx.Response) -> str:
        """The device's own words for a refusal: the ``errors`` list most
        endpoints use, or the problem-details shape some newer ones do."""
        code = response.status_code
        text = (response.text or "").strip()
        detail = ""
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                errors = payload.get("errors")
                if isinstance(errors, list):
                    detail = "; ".join(
                        str(e.get("message", "")) if isinstance(e, dict) else str(e)
                        for e in errors if e
                    )
                elif isinstance(errors, str):
                    detail = errors
                problems = payload.get("problems")
                if not detail and isinstance(problems, list):
                    detail = "; ".join(
                        str(p.get("details") or p.get("title") or "")
                        for p in problems if isinstance(p, dict)
                    )
                if not detail:
                    detail = str(payload.get("details") or payload.get("title") or payload.get("message") or "")
            elif not text.startswith("<"):
                detail = text[:200]
        return f"HTTP {code} ({detail})" if detail else f"HTTP {code}"

    # ── State mirroring ──

    def _apply_hardware(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        updates: dict[str, Any] = {}
        model = str(body.get("model", "")).strip().lower()
        if model in _MODELS:
            updates["model"] = model
        for field, key in (
            ("firmwareVersion", "firmware_version"),
            ("deviceVersion", "device_version"),
            ("hardwareId", "hardware_id"),
            ("ipAddress", "ip_address"),
            ("mac_address", "mac_address"),
            ("enrollmentStatus", "enrollment_status"),
            ("deviceStatus", "device_status"),
        ):
            if body.get(field) is not None:
                updates[key] = str(body[field])
        components = body.get("hardwareComponents")
        if isinstance(components, list):
            updates["bar_count"] = sum(
                1 for c in components
                if isinstance(c, dict) and str(c.get("model", "")).lower() == "bar"
            )
        if updates:
            self.set_states(updates)

    def _apply_audio(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        updates: dict[str, Any] = {}
        for field, key in _AUDIO_FIELDS.items():
            if field not in body or body[field] is None:
                continue
            updates[key] = self._coerce(key, body[field])
        if updates:
            self.set_states(updates)

    def _coerce(self, key: str, value: Any) -> Any:
        var_def = self.DRIVER_INFO["state_variables"].get(key, {})
        vtype = var_def.get("type")
        if vtype == "boolean":
            return _as_bool(value)
        if vtype == "integer" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(round(value))
        if vtype == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if value is None:
            return None
        return str(value)

    def _apply_led(self, led_type: str, body: Any) -> None:
        if not isinstance(body, dict):
            return
        if led_type == "networkA":
            prefix = "network_led"
        elif led_type == "console":
            prefix = "console_led"
        else:
            return  # networkB is the HDX's second port
        updates: dict[str, Any] = {}
        if body.get("colour") is not None:
            updates[f"{prefix}_colour"] = str(body["colour"])
        if body.get("state") is not None:
            updates[f"{prefix}_state"] = str(body["state"])
        if updates:
            self.set_states(updates)

    def _apply_status(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        leds = body.get("leds")
        if isinstance(leds, dict):
            for led_type in ("networkA", "console"):
                self._apply_led(led_type, leds.get(led_type))
        usb = body.get("usb")
        if isinstance(usb, dict) and usb.get("status") is not None:
            self.set_state("usb_status", str(usb["status"]))
        components = body.get("deviceComponents")
        if isinstance(components, dict) and components.get("status") is not None:
            self.set_state("components_status", str(components["status"]))

    def _apply_usb_event(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        if body.get("status") is not None:
            self.set_state("usb_status", str(body["status"]))
        elif "connected" in body:
            # The room status tutorial's example shape.
            self.set_state("usb_status", "connected" if _as_bool(body["connected"]) else "disconnected")

    def _apply_layout(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        if "zones" in body:
            self._apply_zones(body.get("zones"))
        if "cameraSwitcherZoneInputMap" in body or "cameraSwitcherZoneInputMaps" in body:
            self._apply_input_map(_first(body, "cameraSwitcherZoneInputMap", "cameraSwitcherZoneInputMaps"))
        defaults = body.get("cameraSwitcherDefaults")
        if isinstance(defaults, dict):
            updates: dict[str, Any] = {}
            if defaults.get("defaultCameraInputPort") is not None:
                updates["default_camera_input"] = str(defaults["defaultCameraInputPort"])
            for key in ("zone_trigger_wait_ms", "switch_to_default_wait_ms"):
                value = defaults.get(_SWITCHING_DEFAULTS[key])
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    updates[key] = int(round(value))
            if updates:
                self.set_states(updates)
        if body.get("soundLocationAlgorithm") is not None:
            self.set_state("sound_location_algorithm", str(body["soundLocationAlgorithm"]))

    @staticmethod
    def _zone_local_id(raw_id: str) -> str:
        if _ZONE_ID_RE.match(raw_id):
            return raw_id
        return re.sub(r"[^A-Za-z0-9_-]", "_", raw_id)[:128]

    def _apply_zones(self, zones: Any) -> None:
        """Reconcile the zone children with the camera zones the system
        lists. Zones the system no longer lists are removed."""
        if not isinstance(zones, list):
            return
        roster: dict[str, tuple[str, str]] = {}
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            raw_id = str(_first(zone, "id", "zoneId") or "").strip()
            if not raw_id:
                continue
            types = _first(zone, "type", "types")
            if isinstance(types, str):
                types = [types]
            if isinstance(types, list) and types and "Switching" not in types:
                continue  # an Active zone is an HDL300 thing
            label = _first(zone, "label", "ZoneLabel")
            local_id = self._zone_local_id(raw_id)
            roster[raw_id] = (local_id, str(label) if label is not None else raw_id)
        known = set(self.list_children("zone"))
        wanted = {local_id for local_id, _label in roster.values()}
        for stale in known - wanted:
            self.deregister_child("zone", stale)
        for raw_id, (local_id, label) in roster.items():
            camera_input = self._zone_inputs.get(raw_id, "")
            active = raw_id == self._active_zone
            if local_id in known:
                self.set_child_state_batch(
                    "zone", local_id,
                    {"label": label, "camera_input": camera_input, "active": active},
                )
            else:
                self.register_child(
                    "zone", local_id,
                    initial_state={"label": label, "camera_input": camera_input, "active": active},
                )
        self._zone_ids = {raw_id: local_id for raw_id, (local_id, _l) in roster.items()}
        self.set_state("zone_count", len(roster))

    def _apply_input_map(self, mapping: Any) -> None:
        if not isinstance(mapping, list):
            return
        inputs: dict[str, str] = {}
        for entry in mapping:
            if isinstance(entry, dict) and entry.get("zoneId") is not None:
                inputs[str(entry["zoneId"])] = str(entry.get("inputPort") or "")
        self._zone_inputs = inputs
        for raw_id, local_id in self._zone_ids.items():
            if self.is_child_registered("zone", local_id):
                self.set_child_state("zone", local_id, "camera_input", inputs.get(raw_id, ""))

    def _apply_switcher(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        updates: dict[str, Any] = {}
        if "enabled" in body:
            updates["camera_switcher_enabled"] = _as_bool(body["enabled"])
        if body.get("model") is not None:
            updates["camera_switcher_model"] = str(body["model"])
        if body.get("ipOrHostname") is not None:
            updates["camera_switcher_address"] = str(body["ipOrHostname"])
        if "activeCameraInput" in body:
            updates["active_camera_input"] = str(body.get("activeCameraInput") or "")
        errors = body.get("integrationErrors")
        if isinstance(errors, list):
            updates["camera_switcher_error"] = "; ".join(
                str(e.get("description") or e.get("code") or "") if isinstance(e, dict) else str(e)
                for e in errors
            )
        if updates:
            self.set_states(updates)

    def _apply_network(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        entries = body.get("networkConfiguration")
        if not isinstance(entries, list):
            return
        port_a = next(
            (e for e in entries if isinstance(e, dict) and str(e.get("port", "A")) == "A"),
            None,
        )
        if port_a is None:
            return
        updates: dict[str, Any] = {}
        if "isStatic" in port_a:
            updates["network_static"] = _as_bool(port_a["isStatic"])
        if port_a.get("subnetMask") is not None:
            updates["subnet_mask"] = str(port_a["subnetMask"])
        if port_a.get("gateway") is not None:
            updates["gateway"] = str(port_a["gateway"])
        dns = port_a.get("dns")
        if isinstance(dns, list):
            updates["dns_servers"] = ", ".join(str(d) for d in dns)
        if port_a.get("ip") is not None:
            updates["ip_address"] = str(port_a["ip"])
        if port_a.get("mac") is not None:
            updates["mac_address"] = str(port_a["mac"])
        if updates:
            self.set_states(updates)

    def _apply_profiles(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        profiles = body.get("profiles")
        if not isinstance(profiles, list):
            return
        options = []
        active_name, active_id = "", ""
        for profile in profiles:
            if not isinstance(profile, dict) or profile.get("profileId") is None:
                continue
            pid = str(profile["profileId"])
            name = str(profile.get("name") or pid)
            options.append({"value": pid, "label": name})
            if _as_bool(profile.get("active", False)):
                active_name, active_id = name, pid
        self.set_states({
            "room_profile_options": json.dumps(options),
            "room_profile": active_name,
            "room_profile_id": active_id,
        })

    # ── Sound location ──

    def _queue_location(self, body: dict[str, Any]) -> None:
        """At most one write per interval; a reading inside the window is
        held and written when it ends, so the newest one always lands."""
        interval = float(self.config.get("sound_location_interval", 0.5) or 0.5)
        interval = min(max(interval, 0.2), 10.0)
        now = time.monotonic()
        if self._pending_location is None and now - self._last_location_write >= interval:
            self._last_location_write = now
            self._apply_location(body)
            return
        first_hold = self._pending_location is None
        self._pending_location = body
        if first_hold:
            delay = max(0.0, interval - (now - self._last_location_write))
            self._location_timer = asyncio.get_running_loop().call_later(
                delay, self._flush_location,
            )

    def _flush_location(self) -> None:
        self._location_timer = None
        body, self._pending_location = self._pending_location, None
        if body is None or self._client is None:
            return
        self._last_location_write = time.monotonic()
        self._apply_location(body)

    def _apply_location(self, body: dict[str, Any]) -> None:
        updates: dict[str, Any] = {"sound_location_status": ""}
        power = body.get("powerLevel")
        if isinstance(power, (int, float)) and not isinstance(power, bool):
            updates["sound_power_db"] = float(power)
            updates["sound_detected"] = power > 0
            coords = body.get("coordinates")
            if power > 0 and isinstance(coords, dict):
                for axis, key in (("x", "sound_x_mm"), ("y", "sound_y_mm")):
                    value = coords.get(axis)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        updates[key] = int(round(value))
        zone_id, zone_label = "", ""
        triggered = body.get("triggeredZones")
        if isinstance(triggered, list):
            for zone in triggered:
                if not isinstance(zone, dict):
                    continue
                ztype = zone.get("type")
                types = [ztype] if isinstance(ztype, str) else (ztype if isinstance(ztype, list) else [])
                if types and "Switching" not in types:
                    continue
                zone_id = str(_first(zone, "id", "zoneId") or "")
                label = _first(zone, "label", "ZoneLabel")
                zone_label = str(label) if label is not None else ""
                break
        updates["active_zone_id"] = zone_id
        updates["active_zone_label"] = zone_label
        self.set_states(updates)
        self._set_active_zone(zone_id)

    def _set_active_zone(self, zone_id: str) -> None:
        previous, self._active_zone = self._active_zone, zone_id
        if previous == zone_id:
            return
        for raw_id in (previous, zone_id):
            local_id = self._zone_ids.get(raw_id)
            if local_id and self.is_child_registered("zone", local_id):
                self.set_child_state("zone", local_id, "active", raw_id == zone_id)

    def _clear_location(self, status: str) -> None:
        """No readings (the feed is off, or the system says why not): the
        zone is no longer known to be active."""
        self.set_states({
            "sound_location_status": status,
            "sound_detected": False,
            "active_zone_id": "",
            "active_zone_label": "",
        })
        self._set_active_zone("")

    # ── Polling ──

    async def _refresh_audio(self) -> None:
        body = await self._get_json(_AUDIO)
        if body is not None:
            self._apply_audio(body)

    async def poll(self) -> None:
        """Read the audio settings (not evented). Transport errors propagate
        so the platform's missed-poll watchdog sees them. Once a minute the
        evented resources are read again too, in case an event was missed."""
        if self._client is None:
            return
        await self._refresh_audio()
        if time.monotonic() - self._last_resync >= _RESYNC_INTERVAL_S:
            await self._resync()

    async def _resync(self) -> None:
        self._last_resync = time.monotonic()
        self._apply_hardware(await self._get_json(_HARDWARE))
        self._apply_status(await self._get_json(_STATUS))
        await self._read_layout()
        self._apply_switcher(await self._get_json(_SWITCHER))
        self._apply_network(await self._get_json(_NETWORK))
        await self._read_profiles()

    async def _read_layout(self) -> None:
        self._apply_layout(await self._get_json(_LAYOUT))

    async def _read_profiles(self) -> None:
        body = await self._get_json(_PROFILES)
        if body is not None:
            self._apply_profiles(body)
        elif _PROFILES in self._unsupported:
            self.set_states({
                "room_profile_options": "[]", "room_profile": "", "room_profile_id": "",
            })

    def _schedule_resync(self) -> None:
        """Re-read the evented resources from the event loop without
        blocking it."""
        if self._resync_task is not None and not self._resync_task.done():
            return
        self._resync_task = asyncio.create_task(self._resync_quietly())

    def _spawn(self, coro) -> None:
        """Run a read the event stream asked for without holding the stream
        up; the session's teardown cancels it."""
        task = asyncio.create_task(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    async def _resync_quietly(self) -> None:
        try:
            await self._resync()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug(f"[{self.device_id}] Resync after a stream event failed: {exc}")

    # ── Event streams ──

    async def _read_stream(self, path_fn, idle_s: float, on_open, on_event) -> None:
        """Hold one SSE stream open and hand each event to ``on_event``.
        ``path_fn`` gives the path on every (re)open. Silence longer than
        ``idle_s`` reopens it; failures back off and never take the device
        down, since polling remains the safety net."""
        attempts = 0
        warned = False
        while self._client is not None:
            client = self._client
            path = path_fn()
            timeout_s = float(self.config.get("timeout", 5.0) or 5.0)
            timeout = httpx.Timeout(connect=timeout_s, read=idle_s, write=timeout_s, pool=None)
            try:
                token = self._auth
                headers = {"Accept": "text/event-stream", **self._auth_headers()}
                async with client.stream("GET", path, headers=headers, timeout=timeout) as response:
                    self._last_reply = time.monotonic()
                    if response.status_code == 401:
                        await response.aread()
                        # The same once-only login a request does; a refused
                        # password drops the connection.
                        await self._relogin(token)
                        continue
                    if response.status_code != 200:
                        await response.aread()
                        raise ConnectionError(
                            f"stream rejected: {self._refusal_reason(response)}"
                        )
                    attempts = 0
                    warned = False
                    await on_open()
                    event_type = ""
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        self._last_reply = time.monotonic()
                        if line == "":
                            if data_lines or event_type:
                                await on_event(event_type, "\n".join(data_lines))
                            event_type = ""
                            data_lines = []
                            continue
                        if line.startswith(":"):
                            continue
                        field, _, value = line.partition(":")
                        if value.startswith(" "):
                            value = value[1:]
                        if field == "data":
                            data_lines.append(value)
                        elif field == "event":
                            event_type = value.strip()
                    if data_lines:
                        await on_event(event_type, "\n".join(data_lines))
                log.debug(f"[{self.device_id}] {path} stream ended; reopening")
            except asyncio.CancelledError:
                raise
            except httpx.ReadTimeout:
                log.debug(f"[{self.device_id}] {path} stream idle; reopening")
                continue
            except ConnectionFaultError:
                return  # auth_failed: the connection is already being dropped
            except Exception as exc:
                if self._client is None:
                    return
                attempts += 1
                msg = (
                    f"[{self.device_id}] {path} stream failed "
                    f"({str(exc) or type(exc).__name__}); retrying"
                )
                if warned:
                    log.debug(msg)
                else:
                    log.warning(msg)
                    warned = True
                await asyncio.sleep(min(2.0 * attempts, 30.0))

    async def _events_loop(self) -> None:
        opened = {"count": 0}

        async def on_open() -> None:
            # A reopen may have missed events: read everything again.
            opened["count"] += 1
            if opened["count"] > 1:
                self._schedule_resync()

        await self._read_stream(lambda: _EVENTS, _EVENTS_IDLE_REOPEN_S, on_open, self._handle_event)

    async def _handle_event(self, event: str, data: str) -> None:
        payload: Any = None
        if data.strip():
            try:
                payload = json.loads(data)
            except ValueError:
                log.debug(f"[{self.device_id}] Unparseable event {event}: {data[:120]!r}")
                return
        if event == "/api/v1/audio/calibrate":
            if isinstance(payload, dict) and payload.get("status") is not None:
                self.set_state("calibrating", str(payload["status"]) == "started")
        elif event == "/api/v1/audio/hardware":
            self._apply_hardware(payload)
        elif event == "/api/v1/integrations/camera-switcher":
            self._apply_switcher(payload)
        elif event in ("/api/v1/room/layout", "/api/v1/room/zones", "/api/v1/room/zone-input-maps"):
            if isinstance(payload, dict) and payload:
                self._apply_layout(payload)
                if event == "/api/v1/room/layout":
                    # A profile switch changes the layout.
                    self._spawn(self._read_profiles_quietly())
            else:
                self._spawn(self._read_layout_quietly())
        elif event == "/api/v1/network/configuration":
            # The event's body is not specified; read the configuration.
            self._spawn(self._read_quietly(_NETWORK, self._apply_network))
        elif event == "ledStateUpdated":
            if isinstance(payload, dict):
                self._apply_led(str(payload.get("ledType", "")), payload)
        elif event == "usbConnection":
            self._apply_usb_event(payload)
        elif event == "deviceComponentsConnection":
            if isinstance(payload, dict) and payload.get("overallStatus") is not None:
                self.set_state("components_status", str(payload["overallStatus"]))
        # Component layout, room devices, device settings and active zone
        # control carry nothing this driver shows on an HDL310 / HDL410.

    async def _read_quietly(self, path: str, apply) -> None:
        try:
            apply(await self._get_json(path))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug(f"[{self.device_id}] Reading {path} after an event failed: {exc}")

    async def _read_layout_quietly(self) -> None:
        await self._read_quietly(_LAYOUT, self._apply_layout)

    async def _read_profiles_quietly(self) -> None:
        try:
            await self._read_profiles()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug(f"[{self.device_id}] Reading room profiles after an event failed: {exc}")

    def _data_path(self) -> str:
        """The data stream's filter, read again on every reopen: background
        noise always, sound location while it is on and the system has not
        refused it."""
        events = ["deviceMetrics"]
        if self._sound_location and not self._sound_location_refused:
            events.insert(0, "soundLocation")
        return f"{_DATA}?events={','.join(events)}"

    async def _data_loop(self) -> None:
        if not self._sound_location:
            self._clear_location("")

        async def on_open() -> None:
            return None

        await self._read_stream(self._data_path, _DATA_IDLE_REOPEN_S, on_open, self._handle_data)

    async def _handle_data(self, event: str, data: str) -> None:
        try:
            payload = json.loads(data) if data.strip() else None
        except ValueError:
            return
        if event == "soundLocation":
            if self._sound_location and isinstance(payload, dict):
                self._queue_location(payload)
        elif event == "deviceMetrics":
            if isinstance(payload, dict):
                noise = payload.get("backgroundNoise")
                if isinstance(noise, (int, float)) and not isinstance(noise, bool):
                    self.set_state("background_noise_db", int(round(noise)))
        elif event.startswith("error"):
            self._handle_data_error(event, payload)

    def _handle_data_error(self, event: str, payload: Any) -> None:
        """``error (soundLocation)``: the system says why there are no
        readings (muted, a bar unplugged, an update running, unsupported).
        ``error (<name>)`` with 404: the filter named an event this system
        does not have."""
        if not isinstance(payload, dict):
            return
        messages: list[str] = []
        errors = payload.get("error")
        if isinstance(errors, list):
            messages = [str(e.get("message", "")) for e in errors if isinstance(e, dict) and e.get("message")]
        elif payload.get("message"):
            messages = [str(payload["message"])]
        reason = "; ".join(messages) or f"error {payload.get('statusCode', '')}".strip()
        if "soundLocation" not in event:
            log.info(f"[{self.device_id}] Data stream: {reason}")
            return
        if payload.get("statusCode") == 404 or any("unsupported device" in m.lower() for m in messages):
            self._sound_location_refused = True
        if self._pending_location is not None:
            self._pending_location = None
        self._clear_location(reason)

    async def _restart_data_stream(self) -> None:
        task, self._data_task = self._data_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            self._data_task = asyncio.create_task(self._data_loop())

    # ── Commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        if command in ("mute_microphone", "unmute_microphone"):
            await self._write_audio({"microphoneMute": command == "mute_microphone"}, "the microphone mute")
            return True
        if command == "toggle_microphone_mute":
            current = self.get_state("microphone_mute")
            if current is None:
                await self._refresh_audio()
                current = self.get_state("microphone_mute")
            await self._write_audio({"microphoneMute": not _as_bool(current)}, "the microphone mute")
            return True
        if command in ("volume_up", "volume_down"):
            operation = "increment" if command == "volume_up" else "decrement"
            await self._write("PUT", _VOLUME, {"operation": operation}, "the volume change")
            await self._refresh_audio()
            return True
        if command in ("audience_mute_on", "audience_mute_off"):
            await self._write_audio({"audienceMute": command == "audience_mute_on"}, "audience mute")
            return True
        if command in ("voice_amplification_on", "voice_amplification_off"):
            await self._write_audio(
                {"voiceAmplificationEnabled": command == "voice_amplification_on"},
                "Adaptive Voice Amplification",
            )
            return True
        if command in ("set_treble", "set_bass"):
            field = "speakerTrebleLevel" if command == "set_treble" else "speakerBassLevel"
            level = int(params.get("level"))
            await self._write_audio({field: level}, "the treble level" if command == "set_treble" else "the bass level")
            return True
        if command == "identify":
            # HDL310 / HDL410: no body (a port is the HDX's).
            response = await self._request("POST", _IDENTIFY)
            if not response.is_success:
                message = f"The system refused identify: {self._refusal_reason(response)}"
                self.set_state("last_error", message)
                raise ValueError(message)
            return True
        if command == "activate_room_profile":
            profile = self._profile_id(str(params.get("profile", "")).strip())
            if not profile:
                raise ValueError("Choose a room profile to activate.")
            await self._write(
                "POST", f"{_PROFILES}/{quote(profile, safe='')}/active", None, "the room profile",
            )
            await self._read_profiles()
            await self._read_layout()
            return True
        if command in ("sound_location_on", "sound_location_off"):
            enabled = command == "sound_location_on"
            self._sound_location = enabled
            if enabled:
                self._sound_location_refused = False
            self.set_state("sound_location_feed", enabled)
            if not enabled:
                self._pending_location = None
                self._clear_location("")
            await self._restart_data_stream()
            return True
        if command == "calibrate":
            await self._write("POST", _CALIBRATE, None, "calibration")
            return True
        raise ValueError(f"Unknown command: {command}")

    def _profile_id(self, value: str) -> str:
        """The picker sends a profile's id; a macro may name it instead."""
        try:
            options = json.loads(self.get_state("room_profile_options") or "[]")
        except ValueError:
            options = []
        for option in options if isinstance(options, list) else []:
            if isinstance(option, dict) and value in (option.get("value"), option.get("label")):
                return str(option.get("value"))
        return value

    async def _write_audio(self, body: dict[str, Any], what: str) -> None:
        await self._write("PATCH", _AUDIO, body, what)
        await self._refresh_audio()

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        settings = self.DRIVER_INFO["device_settings"]
        if key not in settings:
            raise ValueError(f"Unknown setting: {key}")
        if self._client is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        sdef = settings[key]
        value = self._coerce_setting(sdef, value)
        label = sdef.get("label", key)
        if key in _AUDIO_FIELD_BY_KEY:
            await self._write_audio({_AUDIO_FIELD_BY_KEY[key]: value}, label)
            return True
        if key == "camera_switcher_enabled":
            await self._write("PATCH", _SWITCHER, {"enabled": value}, label)
            self._apply_switcher(await self._get_json(_SWITCHER))
            return True
        if key in _SWITCHING_DEFAULTS:
            # The device takes the camera switching defaults only as a
            # complete object: the other two come from what it reported.
            defaults: dict[str, Any] = {}
            for setting, field in _SWITCHING_DEFAULTS.items():
                current = value if setting == key else self.get_state(setting)
                if current is None:
                    raise ValueError(
                        f"{label} cannot be written yet: the system has not "
                        f"reported its camera switching defaults."
                    )
                defaults[field] = current
            await self._write("PATCH", _LAYOUT, {"cameraSwitcherDefaults": defaults}, label)
            await self._read_layout()
            return True
        if key == "sound_location_algorithm":
            await self._write("PATCH", _LAYOUT, {"soundLocationAlgorithm": value}, label)
            await self._read_layout()
            return True
        raise ValueError(f"Unknown setting: {key}")

    @staticmethod
    def _coerce_setting(sdef: dict[str, Any], value: Any) -> Any:
        stype = sdef.get("type")
        if stype == "boolean":
            return _as_bool(value)
        if stype == "integer":
            number = int(round(float(value)))
            low, high = sdef.get("min"), sdef.get("max")
            if low is not None and number < low:
                raise ValueError(f"{sdef.get('label')} must be at least {low}.")
            if high is not None and number > high:
                raise ValueError(f"{sdef.get('label')} must be at most {high}.")
            return number
        if stype == "enum":
            text = str(value)
            if text not in sdef.get("values", []):
                raise ValueError(f"{sdef.get('label')} must be one of {', '.join(sdef.get('values', []))}.")
            return text
        return value

    # ── Child entities ──

    async def refresh_children(self) -> dict[str, Any]:
        await self._read_layout()
        return {"zones": len(self.list_children("zone"))}
