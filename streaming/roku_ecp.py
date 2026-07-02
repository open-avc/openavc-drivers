"""
OpenAVC Roku Driver (External Control Protocol).

Controls Roku streaming players and Roku TVs over the local network using
Roku's External Control Protocol (ECP) — a RESTful HTTP API on port 8060.
No Roku account, PIN, or cloud connection is required; any device on the same
network can control the Roku once "Control by mobile apps" is enabled on it.

Protocol summary:
  POST /keypress/<KEY>        Press a remote key (Home, Up, Play, VolumeUp, ...)
  POST /keypress/Lit_<char>   Type one character into an on-screen keyboard
  POST /keydown/<KEY> /keyup  Press and hold / release (scrubbing)
  POST /launch/<appID>        Launch an app (optional deep-link query params)
  POST /install/<appID>       Open the channel-store page for an app
  GET  /query/device-info     Device + power state (XML)
  GET  /query/active-app      Currently foregrounded app (XML)
  GET  /query/media-player    Playback transport state (XML)

State is derived ONLY from the three query endpoints above — never synthesized
from the keys this driver sends, so the reported state always reflects what the
Roku actually did (the user's physical remote and other apps move it too).

Important: as of Roku OS 14.1, remote key presses require
Settings > System > Control by mobile apps > Network access set to "Default" or
"Permissive". When it is "Disabled", key presses return HTTP 403; this driver
surfaces that as the `control_enabled` state variable.

Reference: https://developer.roku.com/docs/developer-program/dev-tools/external-control-api.md
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

from server.drivers.base import BaseDriver
from server.transport.http_client import HTTPClientTransport
from server.utils.logger import get_logger

log = get_logger(__name__)

# Roku <power-mode> values mapped to a friendly three-state power enum.
# PowerOn = fully on; Ready = networked standby (Fast TV Start); DisplayOff =
# screen off but audio/network alive (treated as standby); the rest are off.
_POWER_MAP = {
    "PowerOn": "on",
    "Ready": "standby",
    "DisplayOff": "standby",
    "Headless": "off",
    "PowerOff": "off",
}

_MS_RE = re.compile(r"(\d+)")

# command name -> (ECP key, label, help). Each is a single POST /keypress/<key>.
# Power, volume, channel, and input keys only act on Roku TVs and the players
# that report supports-tv-power-control; they're harmless no-ops elsewhere.
_KEYPRESS_COMMANDS: dict[str, tuple[str, str, str]] = {
    "home": ("Home", "Home", "Go to the Roku home screen."),
    "up": ("Up", "Navigate Up", "D-pad up."),
    "down": ("Down", "Navigate Down", "D-pad down."),
    "left": ("Left", "Navigate Left", "D-pad left."),
    "right": ("Right", "Navigate Right", "D-pad right."),
    "select": ("Select", "OK / Select", "D-pad center (OK)."),
    "back": ("Back", "Back", "Return to the previous screen."),
    "instant_replay": ("InstantReplay", "Instant Replay", "Jump back a few seconds (the replay button)."),
    "info": ("Info", "Options / Info", "Open the options panel (the * button)."),
    "play_pause": ("Play", "Play / Pause", "Toggle play and pause."),
    "rewind": ("Rev", "Rewind", "Rewind / skip back."),
    "forward": ("Fwd", "Fast Forward", "Fast forward / skip ahead."),
    "enter": ("Enter", "Enter", "Confirm text entry (the Enter / OK on a keyboard)."),
    "backspace": ("Backspace", "Backspace", "Delete the last typed character."),
    "search": ("Search", "Search", "Open the on-screen search."),
    "find_remote": ("FindRemote", "Find Remote", "Chime the physical remote (if the model supports it)."),
    "volume_up": ("VolumeUp", "Volume Up", "Raise volume. Roku TV and supported players only."),
    "volume_down": ("VolumeDown", "Volume Down", "Lower volume. Roku TV and supported players only."),
    "volume_mute": ("VolumeMute", "Mute", "Toggle mute. Roku TV and supported players only."),
    "power_on": ("PowerOn", "Power On", "Turn on. Roku TV and supported players only."),
    "power_off": ("PowerOff", "Power Off", "Turn off (standby). Roku TV and supported players only."),
    "power_toggle": ("Power", "Power Toggle", "Toggle power. Roku TV and supported players only."),
    "channel_up": ("ChannelUp", "Channel Up", "Next antenna channel. Roku TV tuner only."),
    "channel_down": ("ChannelDown", "Channel Down", "Previous antenna channel. Roku TV tuner only."),
    "input_tuner": ("InputTuner", "Input: Antenna/Tuner", "Switch to the antenna tuner. Roku TV only."),
    "input_hdmi1": ("InputHDMI1", "Input: HDMI 1", "Switch to HDMI 1. Roku TV only."),
    "input_hdmi2": ("InputHDMI2", "Input: HDMI 2", "Switch to HDMI 2. Roku TV only."),
    "input_hdmi3": ("InputHDMI3", "Input: HDMI 3", "Switch to HDMI 3. Roku TV only."),
    "input_hdmi4": ("InputHDMI4", "Input: HDMI 4", "Switch to HDMI 4. Roku TV only."),
    "input_av1": ("InputAV1", "Input: AV", "Switch to the AV (composite) input. Roku TV only."),
}

# Runtime dispatch table: command name -> ECP key.
_KEYPRESS = {name: spec[0] for name, spec in _KEYPRESS_COMMANDS.items()}


def _child_text(root: ET.Element, tag: str) -> str | None:
    """Return the stripped text of a direct child element, or None."""
    el = root.find(tag)
    if el is not None and el.text:
        text = el.text.strip()
        return text or None
    return None


def _parse_device_info(xml_text: str) -> dict[str, Any]:
    """Parse a /query/device-info XML body into flat state values.

    Missing elements (e.g. is-tv on a non-TV player) are simply omitted from
    the result; only what the device reports is returned.
    """
    out: dict[str, Any] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out

    for tag, key in (
        ("model-name", "model_name"),
        ("serial-number", "serial_number"),
        ("software-version", "software_version"),
        ("network-type", "network_type"),
    ):
        value = _child_text(root, tag)
        if value is not None:
            out[key] = value

    name = _child_text(root, "user-device-name") or _child_text(
        root, "friendly-device-name"
    )
    if name is not None:
        out["device_name"] = name

    for tag, key in (
        ("is-tv", "is_tv"),
        ("supports-tv-power-control", "supports_tv_power"),
    ):
        value = _child_text(root, tag)
        if value is not None:
            out[key] = value.lower() == "true"

    mode = _child_text(root, "power-mode")
    if mode is not None:
        out["power_mode"] = mode
        out["power"] = _POWER_MAP.get(mode, "off")

    return out


def _parse_active_app(xml_text: str) -> dict[str, Any]:
    """Parse a /query/active-app XML body into active_app_id / active_app_name.

    Cases handled:
      <active-app><app id="12">Netflix</app></active-app>   -> an app
      <active-app><app>Roku</app></active-app>              -> the home screen
      <active-app>...<screensaver id="..">Name</screensaver> -> screensaver
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    saver = root.find("screensaver")
    if saver is not None:
        return {
            "active_app_id": saver.get("id", ""),
            "active_app_name": (saver.text or "").strip() or "Screensaver",
        }

    app = root.find("app")
    if app is None:
        return {}
    app_id = app.get("id", "")
    name = (app.text or "").strip()
    if not app_id:
        # No id => the Roku home screen (reported as the app named "Roku").
        return {"active_app_id": "", "active_app_name": "Home"}
    return {"active_app_id": app_id, "active_app_name": name}


def _parse_media_player(xml_text: str) -> dict[str, Any]:
    """Parse a /query/media-player XML body into transport state + ms times."""
    out: dict[str, Any] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out

    state = root.get("state")
    if state:
        out["media_state"] = state

    for tag, key in (("position", "media_position"), ("duration", "media_duration")):
        el = root.find(tag)
        if el is not None and el.text:
            match = _MS_RE.search(el.text)
            if match:
                out[key] = int(match.group(1))
    return out


def _parse_tv_channel(xml_text: str) -> dict[str, Any]:
    """Parse a /query/tv-active-channel XML body (Roku TV antenna tuner).

    Returns the current channel number / name / program, blanked when the TV
    isn't tuned to the antenna (the element is present but empty).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    channel = root.find("channel")
    if channel is None:
        return {}
    return {
        "tv_channel": _child_text(channel, "number") or "",
        "tv_channel_name": _child_text(channel, "name") or "",
        "tv_program": _child_text(channel, "program-title") or "",
    }


class RokuECPDriver(BaseDriver):
    """Roku player / TV control via the External Control Protocol (ECP)."""

    DRIVER_INFO = {
        "id": "roku_ecp",
        "name": "Roku (ECP)",
        "manufacturer": "Roku",
        "category": "streaming",
        "version": "1.0.2",
        "author": "OpenAVC",
        "description": (
            "Controls Roku streaming players and Roku TVs over the local "
            "network using the External Control Protocol (ECP). Full remote "
            "surface, text entry, app launch/install, and TV power, volume, "
            "input, and tuner control on supported models."
        ),
        "source_url": "https://developer.roku.com/docs/developer-program/dev-tools/external-control-api.md",
        "tags": ["roku", "streaming", "media-player", "source", "ecp"],
        "verified": False,
        "simulated": True,
        "ports": [8060],
        "protocols": ["roku_ecp"],
        "transport": "http",
        "compatible_models": [
            {
                "manufacturer": "Roku",
                "models": [
                    "Roku Ultra",
                    "Roku Express",
                    "Roku Express 4K",
                    "Roku Streaming Stick",
                    "Roku Streaming Stick 4K",
                    "Roku Premiere",
                    "Roku Streambar",
                    "Roku TV",
                ],
                "confidence": "untested",
                "notes": (
                    "Any Roku device with 'Control by mobile apps' enabled, "
                    "including third-party Roku TVs (TCL, Hisense, onn, etc.). "
                    "Power, volume, and input/tuner keys apply to models that "
                    "report supports-tv-power-control."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls Roku streaming players and Roku TVs over the local "
                "network using Roku's External Control Protocol (ECP). Provides "
                "the full remote-control surface (navigation, transport, search, "
                "and on-screen text entry), app launch and install, and TV "
                "power, volume, input, and tuner control on supported models. "
                "Reports power state, the currently active app, and media "
                "playback status. No Roku account, PIN, or cloud connection is "
                "required, and it works with third-party Roku TVs too."
            ),
            "setup": (
                "1. Connect the Roku to the same network and note its IP "
                "address (Settings > Network > About, or the Roku mobile app).\n"
                "2. On the Roku, set Settings > System > Control by mobile apps "
                "> Network access to 'Default' or 'Permissive' (NOT 'Disabled'). "
                "Roku OS 14.1 and later block remote key presses when this is "
                "off; the driver then reports Mobile Control Enabled = false and "
                "key presses return HTTP 403.\n"
                "3. Enter the IP address. The port is 8060 (do not change).\n"
                "4. No PIN, password, or account is needed.\n"
                "5. To launch an app, use Launch App with its channel ID. Find "
                "an installed app's ID by opening it and reading the Active App "
                "ID state, or browse http://<roku-ip>:8060/query/apps in a "
                "browser. Common IDs: Netflix 12, Amazon Prime Video 13."
            ),
        },
        "discovery": {
            # Authoritative fingerprint: every Roku answers GET
            # /query/device-info on 8060 with a <device-info> XML document.
            # Sent as HTTP/1.0 with NO Host header on purpose: Roku OS 14.1+
            # has DNS-rebinding protection that returns 403 (empty body) to any
            # request whose Host header doesn't match the device's own address.
            # A discovery probe only knows the IP, not the device's hostname, so
            # it must omit Host entirely. Do not add a Host header here.
            "tcp_probe": {
                "port": 8060,
                "send_ascii": "GET /query/device-info HTTP/1.0\r\n\r\n",
                "expect": "<device-info",
                "extract": {
                    "model": {"regex": "<model-name>([^<]+)</model-name>", "group": 1},
                },
            },
            # Roku answers an SSDP ssdp:all M-SEARCH with ST: roku:ecp.
            "ssdp": ["roku:ecp"],
            # Soft hints — narrow candidates when 8060 isn't seen directly.
            "oui": [
                "00:0d:4b", "08:05:81", "10:59:32", "20:ef:bd", "34:5e:08",
                "50:06:f5", "54:4e:f0", "60:92:c8", "7c:67:ab", "84:ea:ed",
                "88:de:a9", "8c:49:62", "9c:f1:d4", "a8:b5:7c", "ac:3a:7a",
                "ac:ae:19", "b0:a7:37", "b0:ee:7b", "b8:3e:59", "b8:a1:75",
                "bc:d7:d4", "c8:3a:6b", "cc:6d:a0", "d0:4d:2c", "d4:be:dc",
                "d4:e2:2f", "d8:31:34", "dc:3a:5e", "ec:9b:75", "f8:b2:2c",
            ],
            "hostname": ["^Roku"],
            "port_open": [8060],
            "manufacturer_alias": ["Roku"],
        },
        "default_config": {
            "host": "",
            "port": 8060,
            "poll_interval": 5,
            "timeout": 10.0,
            "text_entry_delay": 0.05,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "The Roku's IP address on the local network.",
            },
            "port": {
                "type": "integer",
                "default": 8060,
                "label": "Port",
                "min": 1,
                "max": 65535,
                "description": "ECP port. Always 8060 on Roku devices.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 5,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": "How often to refresh power, active app, and media state.",
            },
            "text_entry_delay": {
                "type": "number",
                "default": 0.05,
                "min": 0,
                "max": 2,
                "label": "Text Entry Delay (sec)",
                "description": "Pause between characters when typing text (Type Text command).",
            },
        },
        "state_variables": {
            "power": {
                "type": "enum",
                "values": ["on", "standby", "off"],
                "label": "Power State",
                "help": "Derived from the Roku's power-mode.",
            },
            "power_mode": {
                "type": "string",
                "label": "Power Mode (raw)",
                "help": "The raw Roku power-mode value (PowerOn, Ready, DisplayOff, ...).",
            },
            "active_app_id": {
                "type": "string",
                "label": "Active App ID",
                "help": "Channel ID of the foreground app (empty on the home screen).",
            },
            "active_app_name": {
                "type": "string",
                "label": "Active App",
                "help": "Name of the foreground app, 'Home', or 'Screensaver'.",
            },
            "media_state": {
                "type": "string",
                "label": "Media State",
                "help": "Playback transport state (play, pause, stop, none, ...).",
            },
            "media_position": {
                "type": "integer",
                "label": "Media Position (ms)",
                "help": "Current playback position in milliseconds.",
            },
            "media_duration": {
                "type": "integer",
                "label": "Media Duration (ms)",
                "help": "Total media duration in milliseconds.",
            },
            "model_name": {"type": "string", "label": "Model"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "software_version": {"type": "string", "label": "Software Version"},
            "device_name": {"type": "string", "label": "Device Name"},
            "network_type": {"type": "string", "label": "Network Type"},
            "is_tv": {"type": "boolean", "label": "Is Roku TV"},
            "supports_tv_power": {
                "type": "boolean",
                "label": "Supports TV Power Control",
                "help": "True when the device honors Power/Volume/Input keys.",
            },
            "tv_channel": {
                "type": "string",
                "label": "TV Channel",
                "help": "Antenna channel number (Roku TV on the tuner input).",
            },
            "tv_channel_name": {
                "type": "string",
                "label": "TV Channel Name",
                "help": "Antenna channel call sign (Roku TV tuner).",
            },
            "tv_program": {
                "type": "string",
                "label": "TV Program",
                "help": "Now-playing program title on the antenna tuner (Roku TV).",
            },
            "control_enabled": {
                "type": "boolean",
                "label": "Mobile Control Enabled",
                "help": (
                    "False if the Roku rejected a key press with HTTP 403 — "
                    "enable Settings > System > Control by mobile apps."
                ),
            },
        },
        "commands": {
            **{
                name: {"label": label, "params": {}, "help": help_text}
                for name, (_key, label, help_text) in _KEYPRESS_COMMANDS.items()
            },
            "launch_app": {
                "label": "Launch App",
                "help": (
                    "Launch an app by its Roku channel ID (e.g. 12 for Netflix). "
                    "Optionally deep-link with a content ID and media type."
                ),
                "params": {
                    "app_id": {
                        "type": "string",
                        "required": True,
                        "label": "App / Channel ID",
                        "help": "Roku channel ID. Find it via /query/apps or the Active App ID state.",
                    },
                    "content_id": {
                        "type": "string",
                        "required": False,
                        "label": "Content ID",
                        "help": "Optional deep-link content ID for the app.",
                    },
                    "media_type": {
                        "type": "string",
                        "required": False,
                        "label": "Media Type",
                        "help": "Optional deep-link media type (movie, episode, season, series, ...).",
                    },
                },
            },
            "install_app": {
                "label": "Install App",
                "help": "Open the channel-store page for an app by its channel ID.",
                "params": {
                    "app_id": {
                        "type": "string",
                        "required": True,
                        "label": "App / Channel ID",
                    },
                },
            },
            "type_text": {
                "label": "Type Text",
                "help": (
                    "Type a string into the on-screen keyboard, one character at "
                    "a time. Only works when a text field is focused on the Roku."
                ),
                "params": {
                    "text": {
                        "type": "string",
                        "required": True,
                        "label": "Text",
                        "trim": False,
                        "help": "The text to type. Sent verbatim - leading/trailing spaces are typed too.",
                    },
                },
            },
            "keypress": {
                "label": "Send Key (raw)",
                "help": "Send any ECP key by name (escape hatch, e.g. Lit_a or a custom key).",
                "params": {
                    "key": {
                        "type": "string",
                        "required": True,
                        "label": "ECP Key",
                    },
                },
            },
            "key_down": {
                "label": "Key Down (hold)",
                "help": "Press and hold a key without releasing (pair with Key Up for scrubbing).",
                "params": {
                    "key": {"type": "string", "required": True, "label": "ECP Key"},
                },
            },
            "key_up": {
                "label": "Key Up (release)",
                "help": "Release a key held with Key Down.",
                "params": {
                    "key": {"type": "string", "required": True, "label": "ECP Key"},
                },
            },
        },
    }

    async def connect(self) -> None:
        """Open the HTTP transport, verify reachability, and start polling."""
        host = self.config.get("host", "")
        port = self.config.get("port", 8060)
        base_url = f"http://{host}:{port}"

        self.transport = HTTPClientTransport(
            base_url=base_url,
            auth_type="none",
            verify_ssl=False,
            timeout=self.config.get("timeout", 10.0),
            name=self.device_id,
        )
        await self.transport.open()

        # Verify the Roku is actually answering before declaring connected,
        # so loading a project against an off/absent Roku fails fast instead
        # of sitting at connected=True until the missed-poll watchdog fires.
        verify_timeout = self.config.get("verify_timeout", 3.0)
        if verify_timeout > 0 and not await self.transport.verify(
            timeout=verify_timeout
        ):
            await self.transport.close()
            self.transport = None
            raise ConnectionError(f"Roku at {base_url} is not responding")

        self._connected = True
        self.set_state("connected", True)
        # Optimistic until a key press proves otherwise (the default Roku
        # setting permits same-network control).
        self.set_state("control_enabled", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to Roku at {base_url}")

        # Seed device info, then poll for ongoing state.
        await self._refresh_device_info()

        poll_interval = self.config.get("poll_interval", 5)
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Execute a named command on the Roku."""
        params = params or {}

        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        key = _KEYPRESS.get(command)
        if key is not None:
            await self._post_key(key)
            return

        match command:
            case "launch_app":
                await self._launch(params)
            case "install_app":
                app_id = str(params.get("app_id", "")).strip()
                if app_id:
                    await self.transport.request("POST", f"/install/{quote(app_id, safe='')}")
                else:
                    log.warning(f"[{self.device_id}] install_app missing app_id")
            case "type_text":
                await self._type_text(str(params.get("text", "")))
            case "keypress":
                user_key = str(params.get("key", "")).strip()
                if user_key:
                    await self._post_key(quote(user_key, safe=""))
            case "key_down":
                user_key = str(params.get("key", "")).strip()
                if user_key:
                    await self.transport.request("POST", f"/keydown/{quote(user_key, safe='')}")
            case "key_up":
                user_key = str(params.get("key", "")).strip()
                if user_key:
                    await self.transport.request("POST", f"/keyup/{quote(user_key, safe='')}")
            case _:
                log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def poll(self) -> None:
        """Refresh power/device info, the active app, and media transport."""
        if not self.transport or not self.transport.connected:
            return

        import httpx

        try:
            await self._refresh_device_info()

            resp = await self.transport.get("/query/active-app")
            if resp.ok:
                updates = _parse_active_app(resp.text)
                if updates:
                    self.set_states(updates)

            resp = await self.transport.get("/query/media-player")
            if resp.ok:
                media = _parse_media_player(resp.text)
                state = media.get("media_state", "none")
                updates = {"media_state": state}
                if state in ("play", "pause"):
                    updates["media_position"] = media.get("media_position", 0)
                    updates["media_duration"] = media.get("media_duration", 0)
                else:
                    # Don't carry a stale position/duration into a stopped state.
                    updates["media_position"] = 0
                    updates["media_duration"] = 0
                self.set_states(updates)

            # Antenna tuner state — Roku TVs only, and only meaningful on the
            # tuner input (blank otherwise). Skip the extra request on players.
            if self.get_state("is_tv"):
                resp = await self.transport.get("/query/tv-active-channel")
                if resp.ok:
                    tv = _parse_tv_channel(resp.text)
                    if tv:
                        self.set_states(tv)
        except httpx.TimeoutException as exc:
            # Re-raise as ConnectionError so the platform watchdog counts the
            # dry poll and eventually flips device.<id>.connected to False.
            raise ConnectionError(
                f"Roku at {self.config.get('host', '?')} not responding: {exc}"
            ) from exc

    # --- Internal helpers ---

    async def _refresh_device_info(self) -> None:
        """GET /query/device-info and map it into state."""
        resp = await self.transport.get("/query/device-info")
        if resp.ok:
            info = _parse_device_info(resp.text)
            if info:
                self.set_states(info)

    async def _launch(self, params: dict[str, Any]) -> None:
        """POST /launch/<appID> with optional deep-link query params."""
        app_id = str(params.get("app_id", "")).strip()
        if not app_id:
            log.warning(f"[{self.device_id}] launch_app missing app_id")
            return
        query: dict[str, str] = {}
        content_id = params.get("content_id")
        if content_id:
            query["contentID"] = str(content_id)
        media_type = params.get("media_type")
        if media_type:
            query["MediaType"] = str(media_type)
        await self.transport.request(
            "POST", f"/launch/{quote(app_id, safe='')}", params=query or None
        )

    async def _type_text(self, text: str) -> None:
        """Type a string as a sequence of Lit_ key presses (URL-encoded)."""
        if not text:
            return
        delay = self.config.get("text_entry_delay", 0.05)
        for char in text:
            await self._post_key("Lit_" + quote(char, safe=""))
            if delay > 0:
                await asyncio.sleep(delay)

    async def _post_key(self, segment: str) -> None:
        """POST /keypress/<segment> and track mobile-control authorization.

        ``segment`` must already be URL-safe (named keys are; literal
        characters are pre-encoded by the caller).
        """
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        resp = await self.transport.request("POST", f"/keypress/{segment}")
        self._note_keypress_status(resp.status_code)

    def _note_keypress_status(self, status_code: int) -> None:
        """Flip control_enabled based on the Roku's response to a key press.

        Roku OS 14.1+ returns 403 for key presses when "Control by mobile
        apps" is disabled — the single most common reason a Roku "doesn't
        respond". Surface it instead of failing silently.
        """
        if status_code == 403:
            if self.get_state("control_enabled"):
                log.warning(
                    f"[{self.device_id}] Roku rejected the key press (HTTP 403). "
                    "Enable Settings > System > Control by mobile apps > Network "
                    "access ('Default' or 'Permissive') on the Roku."
                )
            self.set_state("control_enabled", False)
        elif 200 <= status_code < 300:
            if not self.get_state("control_enabled"):
                log.info(f"[{self.device_id}] Roku mobile control is now enabled.")
            self.set_state("control_enabled", True)
