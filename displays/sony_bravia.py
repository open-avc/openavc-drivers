"""
OpenAVC Sony Bravia Display Driver.

Controls Sony Bravia TVs and professional displays via the JSON-RPC REST API
over HTTP (port 80). Covers Android TV, Google TV, and Pro Bravia models from
~2013 onwards (W, X, Z, A, XR series).

Authentication uses a Pre-Shared Key (PSK) sent in the X-Auth-PSK HTTP header.
To configure PSK on the TV:
    Settings > Network > Home Network Setup > IP Control
        - Authentication: Normal and Pre-Shared Key
        - Pre-Shared Key: (set your key, e.g., "1234")
        - Simple IP Control: On (also enables Remote Start for power-on)

API overview:
    POST /sony/system     - Power, system info, LED indicator, remote codes
    POST /sony/audio      - Volume, mute
    POST /sony/avContent  - Input selection, playing content info
    POST /sony/appControl - Application launch
    POST /sony/IRCC       - SOAP-based IR remote code emulation (navigation,
                            media transport, app shortcuts, etc.)

Each JSON-RPC request:
    {"method": "<name>", "params": [<args>], "id": <n>, "version": "1.0"}

Protocol reference: https://pro-bravia.sony.net/develop/integrate/rest-api/spec/
"""

from __future__ import annotations

from typing import Any

from server.drivers.base import BaseDriver
from server.transport.http_client import HTTPClientTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

# Map friendly input names to Sony URI format
INPUT_URI_MAP = {
    "hdmi1": "extInput:hdmi?port=1",
    "hdmi2": "extInput:hdmi?port=2",
    "hdmi3": "extInput:hdmi?port=3",
    "hdmi4": "extInput:hdmi?port=4",
    "composite": "extInput:composite?port=1",
    "component": "extInput:component?port=1",
}
# Reverse: URI to friendly name
URI_INPUT_MAP = {v: k for k, v in INPUT_URI_MAP.items()}

# IRCC (IR Compatible Control over IP) codes for remote button emulation.
# These are base64-encoded command codes sent via SOAP to /sony/IRCC.
# Codes are standardized across Sony Bravia models.
IRCC_CODES = {
    # Navigation
    "up": "AAAAAQAAAAEAAAB0Aw==",
    "down": "AAAAAQAAAAEAAAB1Aw==",
    "left": "AAAAAQAAAAEAAAB2Aw==",
    "right": "AAAAAQAAAAEAAAB3Aw==",
    "confirm": "AAAAAQAAAAEAAABlAw==",
    "back": "AAAAAgAAAJcAAAAjAw==",
    "home": "AAAAAQAAAAEAAABgAw==",
    # Media transport
    "play": "AAAAAgAAAJcAAAAaAw==",
    "pause": "AAAAAgAAAJcAAAAZAw==",
    "stop": "AAAAAgAAAJcAAAAYAw==",
    "rewind": "AAAAAgAAAJcAAAAbAw==",
    "forward": "AAAAAgAAAJcAAAAcAw==",
    # Channel
    "channel_up": "AAAAAQAAAAEAAAAQAw==",
    "channel_down": "AAAAAQAAAAEAAAARAw==",
    # App shortcuts
    "netflix": "AAAAAgAAABoAAAB8Aw==",
    # Display
    "info": "AAAAAQAAAAEAAAB/Aw==",
    "input_toggle": "AAAAAQAAAAEAAAAlAw==",
    "pic_off": "AAAAAQAAAAEAAAARAA==",
    # Number pad
    "num_0": "AAAAAQAAAAEAAAAJAw==",
    "num_1": "AAAAAQAAAAEAAAAAAw==",
    "num_2": "AAAAAQAAAAEAAAABAw==",
    "num_3": "AAAAAQAAAAEAAAACAw==",
    "num_4": "AAAAAQAAAAEAAAADAw==",
    "num_5": "AAAAAQAAAAEAAAAEAw==",
    "num_6": "AAAAAQAAAAEAAAAFAw==",
    "num_7": "AAAAAQAAAAEAAAAGAw==",
    "num_8": "AAAAAQAAAAEAAAAHAw==",
    "num_9": "AAAAAQAAAAEAAAAIAw==",
}

# Build IRCC command entries for DRIVER_INFO
_IRCC_COMMANDS = {
    # Navigation
    "nav_up": {"label": "Navigate Up", "params": {}, "help": "D-pad up."},
    "nav_down": {"label": "Navigate Down", "params": {}, "help": "D-pad down."},
    "nav_left": {"label": "Navigate Left", "params": {}, "help": "D-pad left."},
    "nav_right": {"label": "Navigate Right", "params": {}, "help": "D-pad right."},
    "nav_select": {"label": "Select / Confirm", "params": {}, "help": "D-pad center (OK/Enter)."},
    "nav_back": {"label": "Back", "params": {}, "help": "Return to previous screen."},
    "nav_home": {"label": "Home", "params": {}, "help": "Go to the home screen."},
    # Media transport
    "media_play": {"label": "Play", "params": {}, "help": "Start or resume playback."},
    "media_pause": {"label": "Pause", "params": {}, "help": "Pause playback."},
    "media_stop": {"label": "Stop", "params": {}, "help": "Stop playback."},
    "media_rewind": {"label": "Rewind", "params": {}, "help": "Rewind."},
    "media_forward": {"label": "Fast Forward", "params": {}, "help": "Fast forward."},
    # Channel
    "channel_up": {"label": "Channel Up", "params": {}, "help": "Next channel."},
    "channel_down": {"label": "Channel Down", "params": {}, "help": "Previous channel."},
    # Apps
    "launch_netflix": {"label": "Netflix", "params": {}, "help": "Launch the Netflix app."},
    "launch_app": {
        "label": "Launch App",
        "params": {
            "uri": {
                "type": "string",
                "required": True,
                "help": "Application URI (use the get_apps command to find URIs)",
            },
        },
        "help": "Launch an app by URI.",
    },
    # Display
    "info_display": {"label": "Info / Display", "params": {}, "help": "Toggle on-screen info overlay."},
    "input_toggle": {"label": "Input Toggle", "params": {}, "help": "Cycle through inputs (same as the Input button on the remote)."},
    "pic_off": {"label": "Picture Off", "params": {}, "help": "Turn off the screen (audio keeps playing). Press any key to restore."},
    # IRCC passthrough
    "send_ircc": {
        "label": "Send IRCC Code",
        "params": {
            "code": {
                "type": "string",
                "required": True,
                "help": "Base64-encoded IRCC code to send",
            },
        },
        "help": "Send a raw IRCC remote code (for buttons not covered by other commands).",
    },
}

# Map command names to IRCC code keys
_CMD_TO_IRCC = {
    "nav_up": "up",
    "nav_down": "down",
    "nav_left": "left",
    "nav_right": "right",
    "nav_select": "confirm",
    "nav_back": "back",
    "nav_home": "home",
    "media_play": "play",
    "media_pause": "pause",
    "media_stop": "stop",
    "media_rewind": "rewind",
    "media_forward": "forward",
    "channel_up": "channel_up",
    "channel_down": "channel_down",
    "launch_netflix": "netflix",
    "info_display": "info",
    "input_toggle": "input_toggle",
    "pic_off": "pic_off",
}


class SonyBraviaDriver(BaseDriver):
    """Sony Bravia JSON-RPC REST API driver."""

    DRIVER_INFO = {
        "id": "sony_bravia",
        "name": "Sony Bravia Display",
        "manufacturer": "Sony",
        "category": "display",
        "version": "1.5.1",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "author": "OpenAVC",
        "description": (
            "Controls Sony Bravia TVs and professional displays via the "
            "JSON-RPC REST API and IRCC remote emulation. Power, input, "
            "volume, mute, navigation, media transport, app launch. "
            "Covers Android TV, Google TV, and Pro Bravia models."
        ),
        "source_url": "https://pro-bravia.sony.net/develop/integrate/rest-api/spec/",
        "tags": ["tv", "display", "professional", "bravia"],
        "verified": True,
        "simulated": True,
        "protocols": ["sony_bravia_rest"],
        "ports": [80],
        "compatible_models": [
            {
                "manufacturer": "Sony",
                "models": [
                    "Bravia Android TV",
                    "Bravia Google TV",
                    "Bravia Professional Display",
                ],
                "confidence": "full",
                "notes": "Verified on production hardware.",
            },
        ],
        "transport": "http",
        "help": {
            "overview": (
                "Controls Sony Bravia displays using the built-in REST API "
                "and IRCC remote emulation. Works with Android TV, Google TV, "
                "and Pro Bravia series from ~2013 onwards (W, X, Z, A, XR models)."
            ),
            "setup": (
                "1. Connect the TV to the network.\n"
                "2. On the TV, go to Settings > Network > Home Network Setup > IP Control.\n"
                "3. Set Authentication to 'Normal and Pre-Shared Key'.\n"
                "4. Set a Pre-Shared Key (e.g., '1234').\n"
                "5. Set Simple IP Control to 'On' (enables power-on over network).\n"
                "6. Enter the TV's IP address and PSK in the driver config."
            ),
        },
        "discovery": {
            "ssdp": [
                "urn:schemas-sony-com:service:ScalarWebAPI:1",
            ],
            "oui": [
                "00:01:4a",
                "00:0a:d9",
                "00:0e:07",
                "00:13:a9",
                "00:1a:80",
                "04:5d:4b",
                "40:b8:9a",
                "54:42:49",
                "a8:93:4a",
                "ac:9b:0a",
                "fc:f1:52",
            ],
        },
        "default_config": {
            "host": "",
            "port": 80,
            "psk": "",
            "poll_interval": 15,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 80,
                "label": "Port",
            },
            "psk": {
                "type": "string",
                "required": True,
                "label": "Pre-Shared Key",
                "description": (
                    "The PSK configured on the TV under Settings > Network > "
                    "Home Network Setup > IP Control > Pre-Shared Key."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 15,
                "min": 0,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["off", "on"],
                "label": "Power State",
            },
            "input": {
                "type": "string",
                "label": "Input Source",
            },
            "volume": {
                "type": "integer",
                "label": "Volume",
            },
            "mute": {
                "type": "boolean",
                "label": "Audio Mute",
            },
            "app": {
                "type": "string",
                "label": "Current App",
            },
            "model": {
                "type": "string",
                "label": "Model Name",
            },
            "led_indicator": {
                "type": "string",
                "label": "LED Indicator Mode",
            },
            "picture_mode": {
                "type": "string",
                "label": "Picture Mode",
            },
            "brightness": {
                "type": "integer",
                "label": "Brightness",
            },
            "contrast": {
                "type": "integer",
                "label": "Contrast",
            },
            "color": {
                "type": "integer",
                "label": "Color",
            },
            "sharpness": {
                "type": "integer",
                "label": "Sharpness",
            },
        },
        "device_settings": {
            "led_indicator": {
                "type": "enum",
                "values": [
                    "Demo",
                    "AutoBrightnessAdjust",
                    "Dark",
                    "SimpleResponse",
                    "Off",
                ],
                "label": "LED Indicator Mode",
                "help": (
                    "Front LED behaviour. AutoBrightnessAdjust dims it to the "
                    "room; Dark keeps it dim; SimpleResponse blinks on remote "
                    "input; Off disables it."
                ),
                "state_key": "led_indicator",
                "default": "Dark",
                "setup": False,
            },
            "picture_mode": {
                "type": "string",
                "label": "Picture Mode",
                "help": (
                    "Picture preset. The available names are model-specific "
                    "(e.g. Vivid, Standard, Cinema, Custom, Game, Graphics) — "
                    "enter one your TV lists."
                ),
                "state_key": "picture_mode",
                "default": "Standard",
                "setup": False,
            },
            "brightness": {
                "type": "integer",
                "label": "Brightness",
                "help": "Picture brightness (0-100).",
                "state_key": "brightness",
                "default": 50,
                "min": 0,
                "max": 100,
                "setup": False,
            },
            "contrast": {
                "type": "integer",
                "label": "Contrast",
                "help": "Picture contrast (0-100).",
                "state_key": "contrast",
                "default": 90,
                "min": 0,
                "max": 100,
                "setup": False,
            },
            "color": {
                "type": "integer",
                "label": "Color",
                "help": "Color saturation (0-100).",
                "state_key": "color",
                "default": 50,
                "min": 0,
                "max": 100,
                "setup": False,
            },
            "sharpness": {
                "type": "integer",
                "label": "Sharpness",
                "help": "Picture sharpness (0-100).",
                "state_key": "sharpness",
                "default": 50,
                "min": 0,
                "max": 100,
                "setup": False,
            },
        },
        "commands": {
            # Power
            "power_on": {
                "label": "Power On",
                "params": {},
                "help": "Turn on the display. Requires Simple IP Control enabled on the TV.",
            },
            "power_off": {
                "label": "Power Off",
                "params": {},
                "help": "Turn off the display (standby).",
            },
            # Volume
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "level": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Volume level 0-100",
                    },
                },
                "help": "Set the speaker volume.",
            },
            "volume_up": {
                "label": "Volume Up",
                "params": {},
                "help": "Increase volume by 1.",
            },
            "volume_down": {
                "label": "Volume Down",
                "params": {},
                "help": "Decrease volume by 1.",
            },
            "mute_on": {
                "label": "Mute On",
                "params": {},
                "help": "Mute the audio.",
            },
            "mute_off": {
                "label": "Mute Off",
                "params": {},
                "help": "Unmute the audio.",
            },
            # Input
            "set_input": {
                "label": "Set Input",
                "params": {
                    "input": {
                        "type": "enum",
                        "values": list(INPUT_URI_MAP.keys()),
                        "required": True,
                        "help": "Input source to switch to",
                    },
                },
                "help": "Switch the display input source.",
            },
            # Picture / indicator settings (also exposed as device_settings).
            "set_led_indicator": {
                "label": "Set LED Indicator Mode",
                "params": {
                    "mode": {
                        "type": "enum",
                        "values": [
                            "Demo",
                            "AutoBrightnessAdjust",
                            "Dark",
                            "SimpleResponse",
                            "Off",
                        ],
                        "required": True,
                        "help": "Front LED indicator behaviour.",
                    },
                },
                "help": "Set the front LED indicator mode.",
            },
            "set_picture_mode": {
                "label": "Set Picture Mode",
                "params": {
                    "value": {
                        "type": "string",
                        "required": True,
                        "help": (
                            "Picture preset name (model-specific, e.g. "
                            "Standard, Vivid, Cinema, Custom, Game)."
                        ),
                    },
                },
                "help": "Set the picture-mode preset.",
            },
            "set_brightness": {
                "label": "Set Brightness",
                "params": {
                    "value": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Brightness 0-100.",
                    },
                },
                "help": "Set picture brightness.",
            },
            "set_contrast": {
                "label": "Set Contrast",
                "params": {
                    "value": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Contrast 0-100.",
                    },
                },
                "help": "Set picture contrast.",
            },
            "set_color": {
                "label": "Set Color",
                "params": {
                    "value": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Color saturation 0-100.",
                    },
                },
                "help": "Set color saturation.",
            },
            "set_sharpness": {
                "label": "Set Sharpness",
                "params": {
                    "value": {
                        "type": "integer",
                        "min": 0,
                        "max": 100,
                        "required": True,
                        "help": "Sharpness 0-100.",
                    },
                },
                "help": "Set picture sharpness.",
            },
            # IRCC commands (navigation, media, apps, etc.)
            **_IRCC_COMMANDS,
        },
        # Quick Action strip: high-use room controls + a setup wizard that
        # tests (and optionally saves) the Pre-Shared Key out-of-band, useful
        # when the TV is offline on a wrong PSK.
        "actions": [
            {"id": "power_on", "kind": "command", "icon": "power"},
            {"id": "power_off", "kind": "command", "icon": "power-off"},
            {"id": "mute_on", "kind": "command", "icon": "volume-x"},
            {"id": "mute_off", "kind": "command", "icon": "volume-2"},
            {"id": "input_toggle", "kind": "command", "icon": "monitor"},
            {
                "id": "test_psk",
                "kind": "setup",
                "label": "Test Pre-Shared Key",
                "icon": "key-round",
                "availability": "always",
                "params": {
                    "psk": {
                        "type": "password",
                        "secret": True,
                        "label": "Pre-Shared Key",
                        "help": (
                            "The PSK set on the TV under Settings > Network > "
                            "Home Network Setup > IP Control."
                        ),
                    },
                    "save": {
                        "type": "boolean",
                        "default": True,
                        "label": "Save this key if it works",
                    },
                },
            },
        ],
    }

    _request_id: int = 1

    # Picture-quality settings: state-var / DS key -> Bravia API target name.
    _PICTURE_TARGETS = {
        "picture_mode": "pictureMode",
        "brightness": "brightness",
        "contrast": "contrast",
        "color": "color",
        "sharpness": "sharpness",
    }
    _NUMERIC_PICTURE = {"brightness", "contrast", "color", "sharpness"}

    # device_setting key -> (command, param name) the setting writes through.
    _DS_COMMANDS = {
        "led_indicator": ("set_led_indicator", "mode"),
        "picture_mode": ("set_picture_mode", "value"),
        "brightness": ("set_brightness", "value"),
        "contrast": ("set_contrast", "value"),
        "color": ("set_color", "value"),
        "sharpness": ("set_sharpness", "value"),
    }

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        # Bravia authenticates every /sony/* request with a Pre-Shared Key
        # in the X-Auth-PSK header; the key lives in the driver's `psk`
        # config field. The API is plain HTTP, so there is no certificate
        # to check.
        kwargs["auth_type"] = "api_key"
        kwargs["credentials"] = {
            "header": "X-Auth-PSK",
            "key": str(self.config.get("psk", "")),
        }
        kwargs["verify_ssl"] = False
        return kwargs

    async def _post_connect(self) -> None:
        # Authenticated probe before the device is declared connected:
        # getSystemInformation returns HTTP 403 when the Pre-Shared Key is
        # wrong. The reachability check only HEADs "/", which isn't
        # PSK-gated, so a bad key would otherwise connect and then fail
        # every poll with no reason (the fault surfaced as "not
        # responding"). Classify it as auth here. Also caches the model
        # name on success.
        probe = await self._fetch_system_info()
        if probe is not None and probe.status_code in (401, 403):
            raise ConnectionError(
                "Sony Bravia authentication failed - check the "
                "Pre-Shared Key (PSK)"
            )

    # --- JSON-RPC helper ---

    async def _jsonrpc(
        self,
        service: str,
        method: str,
        params: list | None = None,
        version: str = "1.0",
    ) -> dict | None:
        """
        Send a JSON-RPC request and return the parsed result.

        Args:
            service: API service name (system, audio, avContent, appControl).
            method: RPC method name (e.g., getPowerStatus).
            params: Method parameters (default: empty list).
            version: API version (default: "1.0").

        Returns:
            The "result" field from the response, or None on error.
        """
        if not self.transport or not self.transport.connected:
            return None

        self._request_id += 1
        body = {
            "method": method,
            "params": params or [],
            "id": self._request_id,
            "version": version,
        }

        # Transport-level errors (ConnectError, Timeout) propagate so the
        # platform watchdog can flip device.<id>.connected to False.
        # Only suppress protocol-level errors that indicate the TV is
        # reachable but in an expected non-queryable state.
        response = await self.transport.post(f"/sony/{service}", body=body)
        if not response.ok:
            log.warning(
                f"[{self.device_id}] {service}/{method} HTTP {response.status_code}"
            )
            return None
        data = response.json_data
        if data and "result" in data:
            return data["result"]
        if data and "error" in data:
            err = data["error"]
            # Error code 7 = "Illegal State" (TV in app or standby).
            # Error code 40400 = method not found on this model.
            # Don't spam logs for expected transient errors.
            err_code = err[0] if isinstance(err, list) and err else None
            if err_code not in (7, 40400):
                log.warning(
                    f"[{self.device_id}] {service}/{method} error: {err}"
                )
            return None
        return data

    # --- IRCC (IR remote emulation via SOAP) ---

    async def _send_ircc(self, code: str) -> None:
        """Send an IRCC remote code via SOAP to /sony/IRCC."""
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        soap_body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            '<u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1">'
            f"<IRCCCode>{code}</IRCCCode>"
            "</u:X_SendIRCC>"
            "</s:Body>"
            "</s:Envelope>"
        )
        try:
            await self.transport.request(
                "POST",
                "/sony/IRCC",
                content=soap_body.encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=UTF-8",
                    "SOAPACTION": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"',
                },
            )
        except Exception as e:
            log.warning(f"[{self.device_id}] IRCC send failed: {e}")

    # --- System info ---

    async def _fetch_system_info(self):
        """POST getSystemInformation, cache the model, and return the raw
        HTTPResponse. Doubles as the connect-time PSK check — a wrong key
        returns HTTP 403 — so the connection handshake can classify auth
        failures."""
        if not self.transport or not self.transport.connected:
            return None
        self._request_id += 1
        response = await self.transport.post(
            "/sony/system",
            body={
                "method": "getSystemInformation",
                "params": [],
                "id": self._request_id,
                "version": "1.0",
            },
        )
        if response.ok:
            data = response.json_data or {}
            result = data.get("result")
            if isinstance(result, list) and result and isinstance(result[0], dict):
                model = result[0].get("model", "")
                if model:
                    self.set_state("model", model)
                    log.info(f"[{self.device_id}] Model: {model}")
        return response

    # --- Picture / LED read-back ---

    async def _read_led_indicator(self) -> None:
        """Poll the LED indicator mode (a system setting, readable in standby)."""
        result = await self._jsonrpc("system", "getLEDIndicatorStatus")
        if result and isinstance(result, list) and result:
            mode = result[0].get("mode")
            if mode:
                self.set_state("led_indicator", mode)

    async def _read_picture_settings(self) -> None:
        """Poll every picture-quality setting in one call and fan out."""
        result = await self._jsonrpc(
            "video", "getPictureQualitySettings", [{"target": ""}]
        )
        self._apply_picture_settings(result)

    def _apply_picture_settings(self, result: Any) -> None:
        """Map a getPictureQualitySettings result to the picture state vars.

        The result is ``[[{target, currentValue, ...}, ...]]`` (Sony wraps the
        list one level); some firmware returns a flat ``[{...}]``. Handle both.
        """
        if not result or not isinstance(result, list):
            return
        items = result[0] if result and isinstance(result[0], list) else result
        rev = {api: key for key, api in self._PICTURE_TARGETS.items()}
        for item in items:
            if not isinstance(item, dict):
                continue
            key = rev.get(item.get("target"))
            value = item.get("currentValue")
            if not key or value is None:
                continue
            if key in self._NUMERIC_PICTURE:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            self.set_state(key, value)

    # --- Commands ---

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Execute a named command on the Sony Bravia display."""
        params = params or {}

        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        # Check if this is an IRCC-mapped command
        ircc_key = _CMD_TO_IRCC.get(command)
        if ircc_key:
            code = IRCC_CODES[ircc_key]
            await self._send_ircc(code)
            log.debug(f"[{self.device_id}] IRCC: {command} -> {ircc_key}")
            return

        match command:
            case "power_on":
                await self._jsonrpc(
                    "system", "setPowerStatus", [{"status": True}]
                )
            case "power_off":
                await self._jsonrpc(
                    "system", "setPowerStatus", [{"status": False}]
                )
            case "set_volume":
                level = str(int(params.get("level", 0)))
                await self._jsonrpc(
                    "audio",
                    "setAudioVolume",
                    [{"target": "speaker", "volume": level}],
                )
            case "volume_up":
                await self._jsonrpc(
                    "audio",
                    "setAudioVolume",
                    [{"target": "speaker", "volume": "+1"}],
                )
            case "volume_down":
                await self._jsonrpc(
                    "audio",
                    "setAudioVolume",
                    [{"target": "speaker", "volume": "-1"}],
                )
            case "mute_on":
                await self._jsonrpc(
                    "audio", "setAudioMute", [{"status": True}]
                )
            case "mute_off":
                await self._jsonrpc(
                    "audio", "setAudioMute", [{"status": False}]
                )
            case "set_input":
                input_name = params.get("input", "")
                uri = INPUT_URI_MAP.get(input_name)
                if uri:
                    await self._jsonrpc(
                        "avContent", "setPlayContent", [{"uri": uri}]
                    )
                else:
                    log.warning(
                        f"[{self.device_id}] Unknown input: {input_name}"
                    )
            case "launch_app":
                uri = params.get("uri", "")
                if uri:
                    await self._jsonrpc(
                        "appControl", "setActiveApp", [{"uri": uri}]
                    )
            case "set_led_indicator":
                mode = str(params.get("mode", ""))
                if mode:
                    # setLEDIndicatorStatus is a v1.1 method; status may be null.
                    await self._jsonrpc(
                        "system",
                        "setLEDIndicatorStatus",
                        [{"mode": mode, "status": None}],
                        version="1.1",
                    )
                    self.set_state("led_indicator", mode)
            case "set_picture_mode":
                await self._set_picture("pictureMode", str(params.get("value", "")))
            case "set_brightness":
                await self._set_picture("brightness", int(params.get("value", 0)))
            case "set_contrast":
                await self._set_picture("contrast", int(params.get("value", 0)))
            case "set_color":
                await self._set_picture("color", int(params.get("value", 0)))
            case "set_sharpness":
                await self._set_picture("sharpness", int(params.get("value", 0)))
            case "send_ircc":
                code = params.get("code", "")
                if code:
                    await self._send_ircc(code)
            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

        log.debug(f"[{self.device_id}] Sent command: {command} {params}")

    async def _set_picture(self, target: str, value: Any) -> None:
        """Write one picture-quality setting. Values go on the wire as strings
        (Sony's setPictureQualitySettings takes string values for every
        target). Updates the matching state var optimistically."""
        await self._jsonrpc(
            "video",
            "setPictureQualitySettings",
            [{"settings": [{"target": target, "value": str(value)}]}],
        )
        key = {api: k for k, api in self._PICTURE_TARGETS.items()}.get(target)
        if key:
            self.set_state(
                key, int(value) if key in self._NUMERIC_PICTURE else value
            )

    async def set_device_setting(self, key: str, value: Any) -> Any:
        """Write a device setting through its backing command, then read back
        so the settings editor reflects the device without waiting for a poll."""
        entry = self._DS_COMMANDS.get(key)
        if entry is None:
            raise ValueError(f"Unknown device setting: {key}")
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        command, param = entry
        await self.send_command(command, {param: value})
        if key == "led_indicator":
            await self._read_led_indicator()
        else:
            await self._read_picture_settings()
        return self.get_state(key)

    # --- Polling ---

    async def poll(self) -> None:
        """Query the TV for current power, volume, and input status."""
        if not self.transport or not self.transport.connected:
            return

        # Power status
        result = await self._jsonrpc("system", "getPowerStatus")
        powered_on = True
        if result and isinstance(result, list) and len(result) > 0:
            status = result[0].get("status", "")
            powered_on = status == "active"
            self.set_state("power", "on" if powered_on else "off")

        # LED indicator is a system setting — readable even in standby.
        await self._read_led_indicator()

        # Volume / input / picture only make sense when the TV is on.
        if not powered_on:
            return

        # Volume and mute
        result = await self._jsonrpc("audio", "getVolumeInformation")
        if result and isinstance(result, list):
            for item_list in result:
                if isinstance(item_list, list):
                    for item in item_list:
                        if isinstance(item, dict) and item.get("target") == "speaker":
                            self.set_state("volume", item.get("volume", 0))
                            self.set_state("mute", bool(item.get("mute", False)))
                            break
                elif isinstance(item_list, dict) and item_list.get("target") == "speaker":
                    self.set_state("volume", item_list.get("volume", 0))
                    self.set_state("mute", bool(item_list.get("mute", False)))
                    break

        # Current input / app (may return Illegal State error code 7 if the
        # TV is in an internal app rather than an external input, which is
        # expected and silenced in _jsonrpc).
        result = await self._jsonrpc("avContent", "getPlayingContentInfo")
        if result and isinstance(result, list) and len(result) > 0:
            info = result[0]
            uri = info.get("uri", "")
            title = info.get("title", "")

            input_name = URI_INPUT_MAP.get(uri)
            if input_name:
                self.set_state("input", input_name)
                self.set_state("app", "")
            else:
                # In an app or internal source
                self.set_state("input", "app")
                self.set_state("app", title or uri)

        # Picture-quality settings — read-back for the device_settings surface.
        await self._read_picture_settings()

    # --- Setup wizard ---

    async def run_setup_action(
        self,
        action_id: str,
        params: dict[str, Any],
        progress: Any,
    ) -> dict[str, Any]:
        """Test the Pre-Shared Key over an out-of-band HTTP request.

        The device's normal transport may be down (wrong PSK), so this opens
        its own client, POSTs getSystemInformation, and reports whether the TV
        accepts the key (HTTP 403 = rejected). On success, optionally persists
        the key + reconnects.
        """
        if action_id != "test_psk":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 80))
        psk = str(params.get("psk", "") or "")
        save = bool(params.get("save", True))
        if not host:
            raise ValueError("No IP address configured")

        base_url = f"http://{host}:{port}"
        http = HTTPClientTransport(
            base_url=base_url,
            auth_type="api_key",
            credentials={"header": "X-Auth-PSK", "key": psk},
            verify_ssl=False,
            timeout=8.0,
            name=f"{self.device_id}-setup",
        )

        await progress(f"Connecting to {host}:{port}…", 20)
        await http.open()
        try:
            await progress("Checking the Pre-Shared Key…", 60)
            try:
                resp = await http.post(
                    "/sony/system",
                    body={
                        "method": "getSystemInformation",
                        "params": [],
                        "id": 1,
                        "version": "1.0",
                    },
                )
            except ConnectionError as exc:
                raise ConnectionError(
                    f"Could not reach the TV on {host}:{port} ({exc}). Check "
                    "the IP address and that IP Control is enabled."
                ) from exc
            if resp.status_code in (401, 403):
                raise ConnectionError(
                    "The TV rejected this Pre-Shared Key. Check the key under "
                    "Settings > Network > Home Network Setup > IP Control."
                )
            if not resp.ok:
                raise ConnectionError(
                    f"Unexpected response from the TV (HTTP {resp.status_code})."
                )
            model = ""
            data = resp.json_data or {}
            result = data.get("result")
            if isinstance(result, list) and result and isinstance(result[0], dict):
                model = result[0].get("model", "")
            await progress("Pre-Shared Key accepted", 90)
        finally:
            await http.close()

        saved = False
        if save:
            await self.request_config_update({"psk": psk})
            saved = True
            await progress("Saved. Reconnecting…", 95)
            await self.request_reconnect()

        return {
            "reachable": True,
            "auth_ok": True,
            "saved": saved,
            "model": model,
            "message": f"Pre-Shared Key accepted ({model})."
            if model
            else "Pre-Shared Key accepted.",
        }
