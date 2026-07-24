"""
OpenAVC Barco Pulse Projector Driver.

Controls Barco Pulse-platform laser projectors over the Pulse API —
JSON-RPC 2.0 on TCP port 9090 (the same API is available over RS232,
not offered here). Requests are ``property.get`` / ``property.set`` /
``property.subscribe`` plus direct method invocations
(``system.poweron``, ``optics.zoom.stepforward``, …); messages are
UTF-8 JSON documents with no framing delimiter, so the driver splits
the byte stream on balanced top-level braces.

Why Python rather than YAML:
    Responses carry only the JSON-RPC ``id`` — a ``property.get``
    answer does not echo the property name, so replies must be
    correlated request-by-request, which regex response dispatch
    cannot do. On top of that: ``property.changed`` push notifications
    fan one message out into many state keys, the stream has no
    delimiter (needs a custom frame parser), and the source list is
    discovered at runtime to feed the input picker.

Push vs poll:
    Pulse is push-capable and the driver uses it: on connect it
    subscribes to every operational property (``property.subscribe``)
    and applies ``property.changed`` notifications as they arrive.
    Subscribing does not deliver the current value, so the driver
    primes state with reads at connect, and a slow poll backstops
    drift, refreshes the source list, and reads the environment
    temperatures (a method call, not a subscribable property).

Auth:
    A Pulse session may optionally start with an ``authenticate``
    request carrying a secret pass code. Normal end-user control
    (everything this driver does) works unauthenticated; the code is
    only needed when the projector's API access has been restricted
    or a higher access level is wanted. Leave the field empty unless
    the projector requires it. A rejected code raises a typed
    ``auth_failed`` fault. (JSON-RPC login is the ``jsonrpc_login``
    auth shape — this driver is Python for the correlation/push
    reasons above, so a YAML auth extension alone would not convert
    it.)

ECO mode caveat:
    In ECO state the network interface sleeps and the API session
    drops — Barco documents wake-up via Wake-on-LAN, the keypad, the
    IR remote, or an RS232 string. While the session is still up and
    the projector reports ``eco``, ``power_on`` sends a WoL magic
    packet (using the MAC read from the projector at connect) before
    ``system.poweron`` as a best effort. If the projector has already
    gone unreachable, pair it with the ``wake_on_lan`` utility driver
    or disable ECO (``system.eco.enable``).

Dynamic API:
    Parts of the Pulse API depend on model, mounted lens, and
    configuration (e.g. zoom/focus methods vanish without a motorized
    lens; ``environment.alarmstate`` is documented for the F80
    family). The driver tolerates per-property/method errors: absent
    features simply leave their state variables unset and their
    commands return the projector's error.

Models:
    The Pulse API is common to Barco's Pulse-platform projector
    families (per-family editions of the same "RS232 and Network
    Command Catalog for JSON RPC/Pulse Based Projectors" document).
    Verified against the F80 (End User, v1.7) and UDX (Power User,
    v1.7) catalogs plus the F90 protocol manual.

Source:
    Barco "RS232 and Network Command Catalog For JSON RPC/Pulse Based
    Projectors" (F80 edition v1.7, 2019; UDX edition v1.7, 2019) and
    "F90 Series Projector Control Protocol Manual" (601-0421, 2016):
    https://www.audiogeneral.com/barco/UDX%20Series/JSON_ReferenceGuide.pdf
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from server.drivers.base import BaseDriver, ConnectionFaultError
from server.transport.frame_parsers import FrameParser
from server.utils.logger import get_logger

log = get_logger(__name__)


DEFAULT_PORT = 9090
INITIAL_REQUEST_ID = 1000
REQUEST_TIMEOUT_S = 5.0

# system.state values (F80/UDX catalogs; the F90 manual predates
# "service"/"error"). Shown verbatim so macros match protocol truth.
POWER_STATES = [
    "boot",
    "eco",
    "standby",
    "ready",
    "conditioning",
    "on",
    "deconditioning",
    "service",
    "error",
]

ORIENTATIONS = [
    "DESKTOP_FRONT",
    "DESKTOP_REAR",
    "CEILING_FRONT",
    "CEILING_REAR",
]

# Operational properties: Pulse property -> driver state variable.
# Primed with reads at connect, subscribed for push, batch-polled as a
# drift backstop. Properties a given model lacks answer with an error
# at prime time and are simply left out of the working set.
PROPERTY_STATE_MAP = {
    "system.state": "power_state",
    "illumination.state": "illumination",
    "illumination.sources.laser.power": "laser_power",
    "image.window.main.source": "source",
    "optics.shutter.position": "shutter",
    "image.brightness": "brightness",
    "image.contrast": "contrast",
    "image.saturation": "saturation",
    "image.orientation": "orientation",
    "system.eco.enable": "eco_mode",
    "image.testpattern.show": "test_pattern",
    "image.testpattern.selected": "test_pattern_selected",
    "environment.alarmstate": "alarm_state",
}

# Identity properties read once per connect (all "MODELS All" in the
# catalog, so they are batched in one request).
IDENTITY_PROPERTIES = {
    "system.modelname": "model",
    "system.serialnumber": "serial_number",
    "system.firmwareversion": "firmware_version",
    "system.articlenumber": "article_number",
    "system.familyname": "family_name",
    "network.device.lan.hwaddress": "mac_address",
}

# Device settings: setting key -> (Pulse property, coercion).
SETTING_TO_PROPERTY = {
    "laser_power": ("illumination.sources.laser.power", float),
    "brightness": ("image.brightness", float),
    "contrast": ("image.contrast", float),
    "saturation": ("image.saturation", float),
    "orientation": ("image.orientation", str),
    "eco_mode": ("system.eco.enable", bool),
}


class PulseError(Exception):
    """JSON-RPC error returned by the projector."""

    def __init__(self, error: Any):
        self.error = error
        if isinstance(error, dict):
            message = str(error.get("message", error))
        else:
            message = str(error)
        super().__init__(message)


class _JsonStreamParser(FrameParser):
    """Split a raw TCP stream into complete top-level JSON objects.

    The Pulse API sends bare JSON documents with no delimiter, so the
    only reliable framing is brace balancing (quote- and escape-aware).
    Byte-level scanning is UTF-8 safe: no multi-byte sequence contains
    the ASCII codes for ``{``, ``}``, ``"`` or ``\\``. Bytes outside a
    top-level object (whitespace, garbage) are discarded.
    """

    MAX_BUFFER = 1_048_576

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        frames: list[bytes] = []
        while True:
            frame, rest = self._extract(self._buf)
            if frame is None:
                break
            frames.append(bytes(frame))
            self._buf = rest
        if len(self._buf) > self.MAX_BUFFER:
            self._buf.clear()
        return frames

    def reset(self) -> None:
        self._buf.clear()

    @staticmethod
    def _extract(buf: bytearray) -> tuple[bytearray | None, bytearray]:
        depth = 0
        in_string = False
        escaped = False
        start = -1
        for i, byte in enumerate(buf):
            if start < 0:
                if byte == 0x7B:  # {
                    start = i
                    depth = 1
                continue
            if in_string:
                if escaped:
                    escaped = False
                elif byte == 0x5C:  # backslash
                    escaped = True
                elif byte == 0x22:  # "
                    in_string = False
                continue
            if byte == 0x22:  # "
                in_string = True
            elif byte == 0x7B:  # {
                depth += 1
            elif byte == 0x7D:  # }
                depth -= 1
                if depth == 0:
                    return buf[start:i + 1], buf[i + 1:]
        # No complete object yet; drop any pre-object garbage.
        if start < 0:
            return None, bytearray()
        return None, buf[start:]


def _wol_packet(mac: str) -> bytes | None:
    """Build a Wake-on-LAN magic packet from a MAC string, or None."""
    digits = "".join(c for c in mac if c in "0123456789abcdefABCDEF")
    if len(digits) != 12:
        return None
    hw = bytes.fromhex(digits)
    return b"\xff" * 6 + hw * 16


def _parse_json_value(raw: str) -> Any:
    """Parse a user-supplied value: JSON if it parses, else the string."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


class BarcoPulseDriver(BaseDriver):
    """Barco Pulse projector driver (JSON-RPC 2.0 on TCP 9090)."""

    DRIVER_INFO = {
        "id": "barco_pulse",
        "name": "Barco Pulse Projector",
        "manufacturer": "Barco",
        "category": "projector",
        "version": "1.0.1",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "author": "OpenAVC",
        "description": (
            "Controls Barco Pulse-platform laser projectors (F70 / F80 "
            "/ F90, UDX, UDM, HDX 4K, I600, G62 / G100, XDL and other "
            "Pulse models) over the Pulse JSON-RPC API on TCP 9090. "
            "Live push updates for power, source, shutter, laser power "
            "and picture settings; source picker built from the "
            "projector's own input list; lens shift / zoom / focus; "
            "test patterns; environment temperatures; and a raw "
            "property/method escape hatch for the full Pulse API."
        ),
        "source_url": (
            "https://www.audiogeneral.com/barco/UDX%20Series/"
            "JSON_ReferenceGuide.pdf"
        ),
        "tags": [
            "projector", "barco", "pulse", "laser",
            "large-venue", "json-rpc",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["barco_pulse"],
        # Index-extracted fields must be literal data (build_index AST
        # rule) — 9090 is DEFAULT_PORT.
        "ports": [9090],
        "transport": "tcp",
        "discovery": {
            # property.get is side-effect free; a multi-property read
            # echoes the property names back in the result, so the
            # response positively identifies a Pulse device.
            "tcp_probe": {
                "port": 9090,
                "send_ascii": (
                    '{"jsonrpc":"2.0","method":"property.get","params":'
                    '{"property":["system.modelname","system.serialnumber"]}'
                    ',"id":1}'
                ),
                "expect_regex": r'"system\.modelname"\s*:\s*"',
                "extract_manufacturer": "Barco",
                "extract": {
                    "model": {
                        "regex": r'"system\.modelname"\s*:\s*"([^"]+)"',
                    },
                    "serial": {
                        "regex": r'"system\.serialnumber"\s*:\s*"([^"]+)"',
                    },
                },
            },
            "manufacturer_alias": ["Barco"],
        },
        "compatible_models": [
            {
                "manufacturer": "Barco",
                "models": [
                    "F80 series",
                    "F90 series",
                    "UDX series",
                ],
                "confidence": "untested",
                "notes": (
                    "These families' Pulse API catalogs were used to "
                    "build the driver (F80 End User v1.7, UDX Power "
                    "User v1.7, F90 protocol manual 601-0421). The "
                    "command surface is the shared Pulse API; "
                    "per-model availability of lens motors, ECO mode "
                    "and alarm state varies and the driver tolerates "
                    "absent features."
                ),
            },
            {
                "manufacturer": "Barco",
                "models": [
                    "F70 series",
                    "UDM series",
                    "HDX 4K series",
                    "I600 series",
                    "G62 series",
                    "G100 series",
                    "XDL series",
                ],
                "confidence": "untested",
                "notes": (
                    "Barco lists these as Pulse-platform projectors "
                    "(same JSON-RPC API, per-family catalog editions). "
                    "Expected to work identically; not verified "
                    "against hardware. The Pulse API self-describes — "
                    "use the Get Property / Invoke Method commands "
                    "with the projector's own catalog for "
                    "model-specific extras."
                ),
            },
        ],
        "help": {
            "overview": (
                "Barco Pulse projectors speak JSON-RPC 2.0 on TCP "
                "9090. The driver subscribes to the projector's "
                "change notifications, so power, source, shutter, "
                "laser power and picture values update live. The "
                "source picker offers the projector's own input list "
                "(read at connect). Lens shift / zoom / focus move by "
                "motor steps; models without a motorized lens answer "
                "those commands with an error, which is reported but "
                "harmless. The Get Property / Set Property / Invoke "
                "Method commands expose the full Pulse API for "
                "anything not surfaced as a named command."
            ),
            "setup": (
                "1. Connect the projector's LAN port and give it a "
                "fixed IP (OSD: System Settings > Communication > "
                "LAN).\n"
                "2. In OpenAVC, enter the IP. Leave port at 9090.\n"
                "3. Leave the API pass code empty — it is only needed "
                "when the projector's API access has been restricted "
                "with a code.\n"
                "4. ECO standby powers the network interface down, so "
                "a projector in ECO cannot be reached over the API. "
                "For fully controllable rooms leave ECO disabled (the "
                "Eco Standby device setting), or pair a Wake-on-LAN "
                "device to wake it."
            ),
        },
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "auth_code": "",
            "poll_interval": 30,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": DEFAULT_PORT,
                "label": "TCP Port",
                "description": "Pulse API port. Barco default is 9090.",
            },
            "auth_code": {
                "type": "string",
                "default": "",
                "label": "API Pass Code",
                "secret": True,
                "description": (
                    "Optional. Pulse authentication pass code — only "
                    "needed when the projector requires authentication "
                    "for API access. Normal control works with this "
                    "empty."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 30,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "State changes arrive by push; the poll is a "
                    "drift backstop and refreshes the environment "
                    "temperatures and source list. Set to 0 to "
                    "disable."
                ),
            },
        },
        "state_variables": {
            "power_state": {
                "type": "enum",
                "values": POWER_STATES,
                "label": "Power State",
                "control": True,
            },
            "illumination": {
                "type": "string",
                "label": "Light Source",
                "help": "Laser/lamp state as reported: On or Off.",
            },
            "laser_power": {
                "type": "number",
                "label": "Laser Power",
                "min": 0,
                "max": 100,
                "step": 1,
                "unit": "%",
                "control": True,
                "help": (
                    "Target light output in percent. The projector "
                    "may clamp to a model/lens-dependent minimum."
                ),
            },
            "source": {
                "type": "string",
                "label": "Active Source",
                "control": True,
                "help": (
                    "Momentarily empty while the projector switches "
                    "between sources."
                ),
            },
            "source_options": {
                "type": "string",
                "label": "Available Sources",
                "help": (
                    "JSON list of the projector's input sources, read "
                    "from the projector. Feeds the Select Source "
                    "picker."
                ),
            },
            "shutter": {
                "type": "string",
                "label": "Shutter",
                "control": True,
                "help": "Open or Closed.",
            },
            "brightness": {
                "type": "number",
                "label": "Brightness",
                "min": -1,
                "max": 1,
                "step": 0.01,
                "control": True,
            },
            "contrast": {
                "type": "number",
                "label": "Contrast",
                "min": 0,
                "max": 2,
                "step": 0.01,
                "control": True,
            },
            "saturation": {
                "type": "number",
                "label": "Saturation",
                "min": 0,
                "max": 2,
                "step": 0.01,
                "control": True,
            },
            "orientation": {
                "type": "string",
                "label": "Orientation",
            },
            "eco_mode": {
                "type": "boolean",
                "label": "Eco Standby Enabled",
            },
            "test_pattern": {
                "type": "boolean",
                "label": "Test Pattern Shown",
            },
            "test_pattern_selected": {
                "type": "string",
                "label": "Selected Test Pattern",
            },
            "alarm_state": {
                "type": "string",
                "label": "Alarm State",
                "help": (
                    "Ok / Warning / Alert / Error / Fatal. Documented "
                    "for the F80 family; stays unset on models that "
                    "do not expose environment.alarmstate."
                ),
            },
            "temperature_inlet": {
                "type": "number",
                "label": "Inlet Temperature",
                "unit": "°C",
            },
            "temperature_outlet": {
                "type": "number",
                "label": "Outlet Temperature",
                "unit": "°C",
            },
            "model": {"type": "string", "label": "Model"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "firmware_version": {
                "type": "string",
                "label": "Firmware Version",
            },
            "article_number": {"type": "string", "label": "Article Number"},
            "family_name": {"type": "string", "label": "Family"},
            "mac_address": {"type": "string", "label": "MAC Address"},
        },
        "commands": {
            "power_on": {
                "label": "Power On",
                "params": {},
                "help": (
                    "system.poweron. If the projector reports ECO "
                    "state, a Wake-on-LAN packet is sent first (best "
                    "effort — see the driver help about ECO)."
                ),
            },
            "power_off": {"label": "Power Off", "params": {}},
            "go_ready": {
                "label": "Go To Ready",
                "params": {},
                "help": (
                    "Light source off, electronics on — instant "
                    "restart of the image."
                ),
            },
            "go_eco": {
                "label": "Go To ECO Standby",
                "params": {},
                "help": (
                    "Deepest standby. The network interface sleeps in "
                    "ECO, so API control is lost until the projector "
                    "is woken (WoL, keypad, remote)."
                ),
            },
            "select_source": {
                "label": "Select Source",
                "params": {
                    "source": {
                        "type": "string",
                        "required": True,
                        "label": "Source",
                        "options_state": "source_options",
                        "help": (
                            "One of the projector's source names, "
                            "e.g. 'HDMI', 'DisplayPort 1', 'SDI'."
                        ),
                    },
                },
            },
            "shutter_open": {"label": "Shutter Open", "params": {}},
            "shutter_close": {"label": "Shutter Close", "params": {}},
            "shutter_toggle": {"label": "Shutter Toggle", "params": {}},
            "set_laser_power": {
                "label": "Set Laser Power",
                "params": {
                    "value": {
                        "type": "number",
                        "required": True,
                        "min": 0,
                        "max": 100,
                        "label": "Power (%)",
                    },
                },
            },
            "set_brightness": {
                "label": "Set Brightness",
                "params": {
                    "value": {
                        "type": "number",
                        "required": True,
                        "min": -1,
                        "max": 1,
                        "label": "Brightness",
                        "help": "Normalized: 0 is default, ±1 is ±100%.",
                    },
                },
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {
                    "value": {
                        "type": "number",
                        "required": True,
                        "min": 0,
                        "max": 2,
                        "label": "Contrast",
                        "help": "Normalized: 1 is default.",
                    },
                },
            },
            "set_saturation": {
                "label": "Set Saturation",
                "params": {
                    "value": {
                        "type": "number",
                        "required": True,
                        "min": 0,
                        "max": 2,
                        "label": "Saturation",
                        "help": "Normalized: 1 is default.",
                    },
                },
            },
            "set_orientation": {
                "label": "Set Orientation",
                "params": {
                    "orientation": {
                        "type": "enum",
                        "required": True,
                        "values": ORIENTATIONS,
                        "label": "Orientation",
                    },
                },
            },
            "test_pattern_on": {
                "label": "Show Test Pattern",
                "params": {},
            },
            "test_pattern_off": {
                "label": "Hide Test Pattern",
                "params": {},
            },
            "select_test_pattern": {
                "label": "Select Test Pattern",
                "params": {
                    "pattern": {
                        "type": "string",
                        "required": True,
                        "label": "Pattern",
                        "help": (
                            "Pattern id. Internal patterns use the "
                            "'internal:' prefix, e.g. "
                            "'internal:Cross Hatch', "
                            "'internal:Color Bars', 'internal:Focus' "
                            "(model-dependent)."
                        ),
                    },
                },
            },
            "lens_shift_up": {
                "label": "Lens Shift Up",
                "params": {
                    "steps": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "label": "Steps",
                    },
                },
            },
            "lens_shift_down": {
                "label": "Lens Shift Down",
                "params": {
                    "steps": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "label": "Steps",
                    },
                },
            },
            "lens_shift_left": {
                "label": "Lens Shift Left",
                "params": {
                    "steps": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "label": "Steps",
                    },
                },
            },
            "lens_shift_right": {
                "label": "Lens Shift Right",
                "params": {
                    "steps": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "label": "Steps",
                    },
                },
            },
            "lens_center": {
                "label": "Lens Shift To Center",
                "params": {},
            },
            "zoom_in": {
                "label": "Zoom In",
                "params": {
                    "steps": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "label": "Steps",
                    },
                },
                "help": (
                    "Motorized lens only — projectors without zoom "
                    "motors answer with an error."
                ),
            },
            "zoom_out": {
                "label": "Zoom Out",
                "params": {
                    "steps": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "label": "Steps",
                    },
                },
            },
            "focus_in": {
                "label": "Focus In",
                "params": {
                    "steps": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "label": "Steps",
                    },
                },
            },
            "focus_out": {
                "label": "Focus Out",
                "params": {
                    "steps": {
                        "type": "integer",
                        "required": True,
                        "min": 1,
                        "label": "Steps",
                    },
                },
            },
            "get_property": {
                "label": "Get Property (raw)",
                "params": {
                    "property": {
                        "type": "string",
                        "required": True,
                        "label": "Property",
                        "help": (
                            "Any Pulse property in dot notation, e.g. "
                            "'image.window.main.source'."
                        ),
                    },
                },
                "help": (
                    "Escape hatch: read any property from the Pulse "
                    "API catalog."
                ),
            },
            "set_property": {
                "label": "Set Property (raw)",
                "params": {
                    "property": {
                        "type": "string",
                        "required": True,
                        "label": "Property",
                    },
                    "value": {
                        "type": "string",
                        "required": True,
                        "label": "Value",
                        "help": (
                            "Parsed as JSON when possible ('59', "
                            "'true', '{\"x\":0,\"y\":0}'), otherwise "
                            "sent as a string."
                        ),
                    },
                },
                "help": (
                    "Escape hatch: write any writable property from "
                    "the Pulse API catalog."
                ),
            },
            "invoke_method": {
                "label": "Invoke Method (raw)",
                "params": {
                    "method": {
                        "type": "string",
                        "required": True,
                        "label": "Method",
                        "help": "e.g. 'image.source.list'.",
                    },
                    "params_json": {
                        "type": "string",
                        "required": False,
                        "label": "Params (JSON)",
                        "help": (
                            "Optional JSON object of named parameters, "
                            "e.g. '{\"steps\": 100}'."
                        ),
                    },
                },
                "help": (
                    "Escape hatch: invoke any Pulse API method, e.g. "
                    "introspection ('introspect') to see what this "
                    "projector exposes."
                ),
            },
            "refresh": {"label": "Refresh Status", "params": {}},
        },
        # Values the projector persists and reports back. Writes go
        # through property.set; the subscription echo (plus an
        # immediate read-back) is the state confirmation. Power /
        # source / shutter / test pattern are live operational actions
        # and stay commands only.
        "device_settings": {
            "laser_power": {
                "type": "number",
                "min": 0,
                "max": 100,
                "label": "Laser Power (%)",
                "help": (
                    "Target light output in percent. The projector "
                    "may clamp to a model/lens-dependent minimum."
                ),
                "state_key": "laser_power",
                "default": 100,
                "setup": False,
            },
            "brightness": {
                "type": "number",
                "min": -1,
                "max": 1,
                "label": "Brightness",
                "help": "Normalized image offset: 0 default, ±1 = ±100%.",
                "state_key": "brightness",
                "default": 0,
                "setup": False,
            },
            "contrast": {
                "type": "number",
                "min": 0,
                "max": 2,
                "label": "Contrast",
                "help": "Normalized image gain: 1 is default.",
                "state_key": "contrast",
                "default": 1,
                "setup": False,
            },
            "saturation": {
                "type": "number",
                "min": 0,
                "max": 2,
                "label": "Saturation",
                "help": "Normalized color saturation: 1 is default.",
                "state_key": "saturation",
                "default": 1,
                "setup": False,
            },
            "orientation": {
                "type": "enum",
                "values": ORIENTATIONS,
                "label": "Orientation",
                "help": "Mounting orientation (desktop/ceiling, front/rear).",
                "state_key": "orientation",
                "default": "DESKTOP_FRONT",
                "setup": False,
            },
            "eco_mode": {
                "type": "boolean",
                "label": "Eco Standby",
                "help": (
                    "Enable the deepest standby state. While in ECO "
                    "the network interface sleeps and API control is "
                    "lost until the projector is woken."
                ),
                "state_key": "eco_mode",
                "default": False,
                "setup": False,
            },
        },
        "actions": [
            {"id": "power_on", "kind": "command", "icon": "power"},
            {"id": "power_off", "kind": "command", "icon": "power-off"},
            {"id": "shutter_close", "kind": "command", "icon": "eye-off"},
            {"id": "shutter_open", "kind": "command", "icon": "eye"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {
                "id": "test_pulse",
                "kind": "setup",
                "label": "Test Connection",
                "icon": "plug-zap",
                "availability": "always",
                "params": {
                    "auth_code": {
                        "type": "password",
                        "secret": True,
                        "label": "API Pass Code (optional)",
                        "help": (
                            "Leave blank unless the projector "
                            "requires an authentication code for API "
                            "access."
                        ),
                    },
                    "save": {
                        "type": "boolean",
                        "default": True,
                        "label": "Save this code if it works",
                    },
                },
            },
        ],
    }

    # Liveness: an id-correlated property.get awaited every interval.
    # Push notifications carry no id and cannot satisfy it; a projector
    # that vanished without a FIN stops answering and the BaseDriver
    # watchdog tears the transport down with a typed no_response fault.
    HEALTH_FAULT_MESSAGE = (
        "Connected, but the projector stopped answering status probes."
    )

    def __init__(
        self, device_id: str, config: dict[str, Any], state, events,
    ) -> None:
        super().__init__(device_id, config, state, events)
        self._next_id = INITIAL_REQUEST_ID
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # Operational properties this projector actually answered at
        # prime time — the poll batch and subscriptions use this set.
        self._available: set[str] = set()

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ConnectionError(f"[{self.device_id}] No host configured")

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # Raw byte stream — the JSON-RPC frames are split by the stream
        # parser, not a line delimiter.
        kwargs["delimiter"] = None
        return kwargs

    def _create_frame_parser(self):
        return _JsonStreamParser()

    async def _post_connect(self) -> None:
        auth_code = str(self.config.get("auth_code", "")).strip()
        if auth_code:
            await self._authenticate(auth_code)
        await self._read_identification()

    async def _initial_sync(self) -> None:
        await self._prime_state()
        await self._refresh_source_list()
        await self._subscribe_available()
        await self._poll_environment()

    async def _close_session(self) -> None:
        # Runs on every teardown path: abort in-flight JSON-RPC requests and
        # forget which properties this unit answered, so a reconnect primes
        # from scratch.
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._available = set()

    # ── JSON-RPC plumbing ──

    def _allocate_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    async def _send_jsonrpc(
        self,
        method: str,
        params: Any | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> Any:
        """Send one request and await the id-correlated response.

        Raises PulseError on a JSON-RPC error response, TimeoutError on
        no reply, ConnectionError when the transport is down.
        """
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        rid = self._allocate_id()
        envelope: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": rid,
        }
        if params is not None:
            envelope["params"] = params
        fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.transport.send(json.dumps(envelope).encode("utf-8"))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"[{self.device_id}] Pulse {method} timed out after "
                f"{timeout}s"
            ) from None
        finally:
            self._pending.pop(rid, None)

    async def on_data_received(self, data: bytes) -> None:
        try:
            msg = json.loads(data.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            log.warning(f"[{self.device_id}] Bad JSON frame: {exc}")
            return
        if not isinstance(msg, dict):
            return

        rid = msg.get("id")
        if rid is not None and rid in self._pending:
            fut = self._pending.pop(rid)
            if msg.get("error") is not None:
                if not fut.done():
                    fut.set_exception(PulseError(msg["error"]))
            elif not fut.done():
                fut.set_result(msg.get("result"))
            return

        method = msg.get("method", "")
        params = msg.get("params", {})
        if method == "property.changed":
            self._handle_property_changed(params)
        elif method == "signal.callback":
            log.debug(f"[{self.device_id}] Ignoring signal callback")
        else:
            log.debug(f"[{self.device_id}] Unhandled Pulse frame: {msg!r}")

    def _handle_property_changed(self, params: Any) -> None:
        entries = (params or {}).get("property")
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for prop, value in entry.items():
                self._apply_property(prop, value)

    def _apply_property(self, prop: str, value: Any) -> None:
        state_key = PROPERTY_STATE_MAP.get(prop)
        if state_key is None:
            return
        if isinstance(value, (dict, list)):
            # Flat primitives only in the state store.
            value = json.dumps(value)
        self.set_state(state_key, value)

    # ── Connection-time setup ──

    async def _authenticate(self, auth_code: str) -> None:
        code: Any = int(auth_code) if auth_code.isdigit() else auth_code
        try:
            result = await self._send_jsonrpc("authenticate", {"code": code})
        except PulseError as exc:
            raise ConnectionFaultError(
                f"[{self.device_id}] Pulse authentication rejected the "
                f"configured pass code — {exc}",
                code="auth_failed",
            ) from exc
        if result is not True:
            raise ConnectionFaultError(
                f"[{self.device_id}] Pulse authentication rejected the "
                "configured pass code",
                code="auth_failed",
            )
        log.info(f"[{self.device_id}] Pulse authentication accepted")

    async def _read_identification(self) -> None:
        """Read the identity block; doubles as protocol verification."""
        try:
            result = await self._send_jsonrpc(
                "property.get",
                {"property": list(IDENTITY_PROPERTIES)},
            )
        except TimeoutError:
            raise ConnectionFaultError(
                f"[{self.device_id}] Port answered but no JSON-RPC "
                "response — is this a Barco Pulse projector?",
                code="no_response",
            ) from None
        except PulseError as exc:
            # Odd but non-fatal: the projector speaks JSON-RPC yet
            # rejected the identity read. Continue without identity.
            log.warning(f"[{self.device_id}] Identity read failed: {exc}")
            return
        if isinstance(result, dict):
            for prop, value in result.items():
                state_key = IDENTITY_PROPERTIES.get(prop)
                if state_key is not None and value is not None:
                    self.set_state(state_key, str(value))

    async def _prime_state(self) -> None:
        """Read each operational property once; record which exist."""
        available: set[str] = set()
        for prop in PROPERTY_STATE_MAP:
            try:
                value = await self._send_jsonrpc(
                    "property.get", {"property": prop},
                )
            except PulseError:
                continue  # not on this model/config — leave unset
            available.add(prop)
            self._apply_property(prop, value)
        self._available = available

    async def _subscribe_available(self) -> None:
        if not self._available:
            return
        try:
            await self._send_jsonrpc(
                "property.subscribe",
                {"property": sorted(self._available)},
            )
        except PulseError as exc:
            log.warning(f"[{self.device_id}] Subscribe failed: {exc}")

    async def _refresh_source_list(self) -> None:
        try:
            result = await self._send_jsonrpc("image.source.list")
        except PulseError:
            return
        if not isinstance(result, list):
            return
        options = [
            {"value": str(name), "label": str(name)} for name in result
        ]
        encoded = json.dumps(options)
        if self.get_state("source_options") != encoded:
            self.set_state("source_options", encoded)

    # ── Polling (drift backstop + environment) ──

    async def poll(self) -> None:
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        if self._available:
            try:
                result = await self._send_jsonrpc(
                    "property.get",
                    {"property": sorted(self._available)},
                )
                if isinstance(result, dict):
                    for prop, value in result.items():
                        self._apply_property(prop, value)
            except PulseError:
                # Availability shifted (lens change, config change) —
                # re-discover property by property.
                await self._prime_state()
        await self._poll_environment()
        await self._refresh_source_list()

    async def _poll_environment(self) -> None:
        try:
            result = await self._send_jsonrpc(
                "environment.getcontrolblocks",
                {"type": "Sensor", "valuetype": "Temperature"},
            )
        except PulseError:
            return
        if not isinstance(result, dict):
            return
        for name, value in result.items():
            if name == "environment.temperature.inlet":
                self.set_state("temperature_inlet", value)
            elif name == "environment.temperature.outlet":
                self.set_state("temperature_outlet", value)

    # ── Liveness ──

    async def _liveness_probe(self) -> None:
        value = await self._send_jsonrpc(
            "property.get", {"property": "system.state"}, timeout=4.0,
        )
        self._apply_property("system.state", value)

    # ── Commands ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}

        if command == "power_on":
            if self.get_state("power_state") == "eco":
                await self._send_wol()
            return await self._send_jsonrpc("system.poweron")
        if command == "power_off":
            return await self._send_jsonrpc("system.poweroff")
        if command == "go_ready":
            return await self._send_jsonrpc("system.gotoready")
        if command == "go_eco":
            return await self._send_jsonrpc("system.gotoeco")

        if command == "select_source":
            return await self._set_property(
                "image.window.main.source", str(params["source"]),
            )
        if command == "shutter_open":
            return await self._set_property("optics.shutter.target", "Open")
        if command == "shutter_close":
            return await self._set_property("optics.shutter.target", "Closed")
        if command == "shutter_toggle":
            return await self._send_jsonrpc("optics.shutter.toggle")

        if command == "set_laser_power":
            return await self._set_property(
                "illumination.sources.laser.power", float(params["value"]),
            )
        if command == "set_brightness":
            return await self._set_property(
                "image.brightness", float(params["value"]),
            )
        if command == "set_contrast":
            return await self._set_property(
                "image.contrast", float(params["value"]),
            )
        if command == "set_saturation":
            return await self._set_property(
                "image.saturation", float(params["value"]),
            )
        if command == "set_orientation":
            return await self._set_property(
                "image.orientation", str(params["orientation"]),
            )

        if command == "test_pattern_on":
            return await self._set_property("image.testpattern.show", True)
        if command == "test_pattern_off":
            return await self._set_property("image.testpattern.show", False)
        if command == "select_test_pattern":
            return await self._set_property(
                "image.testpattern.selected", str(params["pattern"]),
            )

        if command in _LENS_METHODS:
            method = _LENS_METHODS[command]
            call_params = (
                None if command == "lens_center"
                else {"steps": int(params["steps"])}
            )
            return await self._send_jsonrpc(method, call_params)

        if command == "get_property":
            prop = str(params["property"]).strip()
            value = await self._send_jsonrpc(
                "property.get", {"property": prop},
            )
            self._apply_property(prop, value)
            return value
        if command == "set_property":
            prop = str(params["property"]).strip()
            return await self._set_property(
                prop, _parse_json_value(str(params["value"])),
            )
        if command == "invoke_method":
            method = str(params["method"]).strip()
            raw = str(params.get("params_json", "") or "").strip()
            call_params = _parse_json_value(raw) if raw else None
            return await self._send_jsonrpc(method, call_params)

        if command == "refresh":
            await self.poll()
            return True

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    async def _set_property(self, prop: str, value: Any) -> Any:
        result = await self._send_jsonrpc(
            "property.set", {"property": prop, "value": value},
        )
        # Subscribed properties confirm via the change notification;
        # for everything else read the value back so state reflects
        # what the projector actually accepted.
        if prop in PROPERTY_STATE_MAP and prop not in self._available:
            try:
                current = await self._send_jsonrpc(
                    "property.get", {"property": prop},
                )
            except PulseError:
                return result
            self._available.add(prop)
            self._apply_property(prop, current)
        return result

    async def _send_wol(self) -> None:
        """Best-effort Wake-on-LAN using the MAC read at connect."""
        mac = str(self.get_state("mac_address") or "")
        packet = _wol_packet(mac)
        if packet is None:
            log.warning(
                f"[{self.device_id}] No MAC address known — cannot send "
                "Wake-on-LAN for ECO wake-up"
            )
            return
        from server.transport.udp import UDPTransport

        udp = UDPTransport(name=self.device_id)
        try:
            await udp.open(allow_broadcast=True)
            await udp.send(packet, "255.255.255.255", 9)
            log.info(f"[{self.device_id}] Sent Wake-on-LAN packet to {mac}")
        except OSError as exc:
            log.warning(f"[{self.device_id}] Wake-on-LAN send failed: {exc}")
        finally:
            try:
                udp.close()
            except Exception:
                pass

    # ── Device settings ──

    async def set_device_setting(self, key: str, value: Any) -> Any:
        entry = SETTING_TO_PROPERTY.get(key)
        if entry is None:
            raise ValueError(f"Unknown device setting: {key}")
        prop, coerce = entry
        await self._set_property(prop, coerce(value))
        return value

    # ── Setup wizard ──

    async def run_setup_action(self, action_id, params, progress):
        if action_id != "test_pulse":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", DEFAULT_PORT))
        auth_code = str(params.get("auth_code", "") or "").strip()
        save = bool(params.get("save", True))

        await progress(f"Connecting to {host}:{port}…", 10)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach {host}:{port} — {exc}"
            ) from exc

        parser = _JsonStreamParser()

        async def call(method: str, call_params: Any, rid: int) -> Any:
            envelope: dict[str, Any] = {
                "jsonrpc": "2.0", "method": method, "id": rid,
            }
            if call_params is not None:
                envelope["params"] = call_params
            writer.write(json.dumps(envelope).encode("utf-8"))
            await writer.drain()
            deadline = asyncio.get_event_loop().time() + 5.0
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise ConnectionError(
                        "The projector did not answer — is this a "
                        "Barco Pulse device?"
                    )
                chunk = await asyncio.wait_for(
                    reader.read(4096), timeout=remaining,
                )
                if not chunk:
                    raise ConnectionError("Connection closed by projector")
                for frame in parser.feed(chunk):
                    msg = json.loads(frame.decode("utf-8"))
                    if msg.get("id") != rid:
                        continue  # skip pushes / stale frames
                    if msg.get("error") is not None:
                        raise PulseError(msg["error"])
                    return msg.get("result")

        try:
            if auth_code:
                await progress("Testing the API pass code…", 35)
                code: Any = (
                    int(auth_code) if auth_code.isdigit() else auth_code
                )
                try:
                    result = await call("authenticate", {"code": code}, 1)
                except PulseError as exc:
                    raise ConnectionError(
                        f"The projector rejected the pass code — {exc}"
                    ) from exc
                if result is not True:
                    raise ConnectionError(
                        "The projector rejected the pass code"
                    )

            await progress("Reading projector identification…", 60)
            ident = await call(
                "property.get",
                {
                    "property": [
                        "system.modelname",
                        "system.serialnumber",
                        "system.firmwareversion",
                        "system.state",
                    ],
                },
                2,
            )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        summary = {
            "model": (ident or {}).get("system.modelname"),
            "serial": (ident or {}).get("system.serialnumber"),
            "firmware": (ident or {}).get("system.firmwareversion"),
            "state": (ident or {}).get("system.state"),
            "authenticated": bool(auth_code),
        }

        if auth_code and save:
            await progress("Saving the pass code…", 85)
            await self.request_config_update({"auth_code": auth_code})
            await progress("Reconnecting…", 95)
            await self.request_reconnect()

        return summary


# Lens motion commands -> Pulse methods, direction mapping per the F90
# protocol manual's Installation table (Up = vertical stepreverse,
# Down = vertical stepforward, Left = horizontal stepreverse, Right =
# horizontal stepforward; Zoom In / Focus In = stepforward). All take
# a "steps" parameter except lens_center. zoom/focus objects exist
# only with a motorized lens (dynamic API) — without one the projector
# answers "Method not found", which the command surfaces as an error.
_LENS_METHODS = {
    "lens_shift_up": "optics.lensshift.vertical.stepreverse",
    "lens_shift_down": "optics.lensshift.vertical.stepforward",
    "lens_shift_left": "optics.lensshift.horizontal.stepreverse",
    "lens_shift_right": "optics.lensshift.horizontal.stepforward",
    "lens_center": "optics.shifttocenter",
    "zoom_in": "optics.zoom.stepforward",
    "zoom_out": "optics.zoom.stepreverse",
    "focus_in": "optics.focus.stepforward",
    "focus_out": "optics.focus.stepreverse",
}
