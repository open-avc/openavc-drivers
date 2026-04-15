"""
LG WebOS Driver for OpenAVC
Author : Keaton Stacks
Version: 3.0.0

Protocol: SSAP over secure WebSocket (wss://<ip>:3001)
Each request opens a fresh connection, registers with the stored client-key,
sends the command, and returns the response payload.  No persistent socket is
kept; this matches WebOS behaviour and avoids subscription-management overhead.

"""

from __future__ import annotations

import json
import os
from server.system_config import get_system_config
import re
import socket
import ssl
import asyncio
from typing import Any

import websockets  # FIX: was imported inside _ssap_request on every call

from server.drivers.base import BaseDriver
from server.utils.logger import get_logger

log = get_logger(__name__)


class LgWebosDriver(BaseDriver):

    # ------------------------------------------------------------------
    # App / Input mapping
    # ------------------------------------------------------------------

    APP_MAP: dict[str, str] = {
        "HDMI_1":    "com.webos.app.hdmi1",
        "HDMI_2":    "com.webos.app.hdmi2",
        "HDMI_3":    "com.webos.app.hdmi3",
        "HDMI_4":    "com.webos.app.hdmi4",
        "TV":        "com.webos.app.livetv",
        "PEACOCK":   "com.peacock.tv",
        "NETFLIX":   "netflix",
        "YOUTUBE":   "youtube.leanback.v4",
        "YOUTUBE_TV":"youtube.leanback.ytv.v1",
        "PRIME":     "amazon",
        "HULU":      "hulu",
        "MAX":       "com.wbd.stream",
        "DISNEY":    "com.disney.disneyplus-prod",
        "PLEX":      "cdp-30",
        "APPLE_TV":  "com.apple.appletv",
        "PARAMOUNT": "com.cbs-all-access.webapp.prod",
    }
    def _display_name(cls, key: str) -> str:
        """Convert a programmatic APP_MAP key to a human-readable display label."""
        return cls.APP_DISPLAY_NAMES.get(key, key.replace("_", " ").title())
        
    APP_DISPLAY_NAMES: dict[str, str] = {
        "HDMI_1":     "HDMI 1",
        "HDMI_2":     "HDMI 2",
        "HDMI_3":     "HDMI 3",
        "HDMI_4":     "HDMI 4",
        "TV":         "Live TV",
        "PEACOCK":    "Peacock",
        "NETFLIX":    "Netflix",
        "YOUTUBE":    "YouTube",
        "YOUTUBE_TV": "YouTube TV",
        "PRIME":      "Prime Video",
        "HULU":       "Hulu",
        "MAX":        "Max",
        "DISNEY":     "Disney+",
        "PLEX":       "Plex",
        "APPLE_TV":   "Apple TV",
        "PARAMOUNT":  "Paramount+",
    }
    
    REVERSE_APP_MAP: dict[str, str] = {v: k for k, v in APP_MAP.items()}

    # ------------------------------------------------------------------
    # Driver metadata
    # ------------------------------------------------------------------

    DRIVER_INFO = {
        "id":           "lg_webos",
        "name":         "LG WebOS",
        "manufacturer": "LG",
        "category":     "display",
        "version":      "3.0.0",
        "author":       "Keaton Stacks",
        "description":  "Controls LG WebOS TVs (2016+) via the SSAP WebSocket protocol.",
        "transport":    "tcp",

        "discovery": {
            "ports": [3001],
        },

        "help": {
            "overview": (
                "Controls LG WebOS televisions using the SSAP protocol over a secure "
                "WebSocket connection on port 3001.  Supports power, volume, input "
                "switching, and app launching."
            ),
            "setup": (
                "Enable 'LG Connect Apps' or network control in the TV's General settings. "
                "A pairing prompt will appear on the TV screen on first connect. "
                "If the prompt does not appear, use the 'Force Pair' command."
            ),
        },

        "default_config": {
            "mac_address":   "",
            "host":    "",
            "poll_interval": 5,
        },

        "config_schema": {
            "host":    {"type": "string",  "required": True,  "label": "IP Address"},
            "mac_address":   {"type": "string",  "required": True,  "label": "MAC Address"},
            "poll_interval": {"type": "integer", "default": 5,      "label": "Poll Interval (s)"},
        },

        "state_variables": {
            "power":          {"type": "enum",    "values": ["off", "on"], "label": "Power State"},
            "volume":         {"type": "integer", "label": "Volume Level"},
            "mute":           {"type": "boolean", "label": "Mute State"},
            "sound_output":   {"type": "string",  "label": "Audio Output"},
            "max_volume":     {"type": "integer", "label": "Max Volume Limit"},
            "external_control":  {"type": "boolean", "label": "External CEC Control"},
            "can_adjust_volume": {"type": "boolean", "label": "Volume Adjustable"},
            "input":          {"type": "string",  "label": "Current Input / App"},
            "connected":      {"type": "boolean", "label": "API Connected"},
            "paired":         {"type": "boolean", "label": "Paired to TV"},
        },

        "commands": {
            "power":         {"label": "Power Toggle", "params": {"value": {"type": "boolean", "required": True}}},
            "power_on":      {"label": "Power On"},
            "power_off":     {"label": "Power Off"},
            "set_volume":    {"label": "Set Volume", "params": {"level": {"type": "integer", "min": 0, "max": 100}}},
            "volume_up":     {"label": "Volume Up"},
            "volume_down":   {"label": "Volume Down"},
            "mute":          {"label": "Set Mute", "params": {"value": {"type": "boolean"}}},
            "set_input":     {"label": "Set Input", "params": {"id": {"type": "string"}}},
            "launch_app":    {"label": "Launch App", "params": {"id": {"type": "string"}}},
            "list_apps":     {"label": "List Installed Apps"},
            "force_pair":    {"label": "Force Pairing Prompt"},
            "clear_pairing": {"label": "Clear Pairing Token"},
            "cursor_up":     {"label": "Up"},
            "cursor_down":   {"label": "Down"},
            "cursor_left":   {"label": "Left"},
            "cursor_right":  {"label": "Right"},
            "enter":         {"label": "Enter / OK"},
            "back":          {"label": "Back"},
            "home":          {"label": "Home / Dashboard"},
            "menu":          {"label": "Settings Menu"},
        },
    }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._poll_locked_until: float = 0.0
        data_dir = get_system_config().data_dir
        self._key_path = os.path.join(data_dir, f"lg_key_{self.device_id}.txt")
        self._volume_target: int | None = None

    async def connect(self) -> None:
        self._connected = True
        self._poll_locked_until = 0.0
        await self.events.emit(f"device.connected.{self.device_id}")
        self.set_state("input", "Syncing...")
        interval = self.config.get("poll_interval", 5)
        self.set_state("connected", True)
        await self.start_polling(interval=interval)
        log.info(f"[{self.device_id}] LG WebOS v{self.DRIVER_INFO['version']} Loaded")
        await self.poll()

    async def disconnect(self) -> None:
        await self.stop_polling()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    async def poll(self) -> None:
        if asyncio.get_running_loop().time() < self._poll_locked_until:
            return
        try:
            ip = self.config.get("host")
            is_reachable = await self._check_ssap_raw(ip) if ip else False
            self.set_state("connected", is_reachable)
            # --- OFFLINE STATE FLUSH ---
            if not ip or not await self._check_ssap_raw(ip):
                self.set_states({
                    "power":  "off",
                    "input":  "Power Off",
                    "volume": 0,
                    "mute":   False,
                    "paired": False,
                })
                return

            is_paired = os.path.exists(self._key_path)

            # --- STAGE 0: Power Check (Modern WebOS Endpoint) ---
            # Newer firmware restricts the legacy system endpoint; try tvpower first.
            pwr_data = await self._ssap_request(
                ip,
                "ssap://com.webos.service.tvpower/power/getPowerState",
                register=is_paired,
            )
            if not pwr_data:
                # Fallback for older TVs
                pwr_data = await self._ssap_request(
                    ip,
                    "ssap://system/getPowerState",
                    register=is_paired,
                )

            if not pwr_data:
                log.debug(
                    f"[{self.device_id}] Both power endpoints returned empty; "
                    "TV is reachable on port 3001 but not yet responding — treating as off."
                )
                self.set_state("power", "off")
                return

            # Handle QuickStart+ (Standby) states properly
            new_pwr = "off" if pwr_data.get("state") in ("Standby", "Active Standby") else "on"
            self.set_state("power", new_pwr)

            if new_pwr == "on" and is_paired:
                self.set_state("paired", True)

                # --- STAGE 1: Poll Audio (Advanced Status) ---
                try:
                    # getStatus provides better metadata (ARC vs Internal)
                    vol_data = await self._ssap_request(ip, "ssap://audio/getStatus")
                    if vol_data:
                        v_status = vol_data.get("volumeStatus", vol_data)

                        output_map = {
                            "external_arc":     "HDMI ARC",
                            "tv_speaker":       "TV Speakers",
                            "external_speaker": "Optical / Aux",
                            "bt_soundbar":      "Bluetooth",
                            "headphone":        "Headphones",
                        }
                        raw_output   = v_status.get("soundOutput", "unknown")
                        clean_output = output_map.get(
                            raw_output,
                            raw_output.replace("_", " ").title(),
                        )

                        self.set_states({
                            "volume":          v_status.get("volume", 0),
                            "mute":            bool(v_status.get("muteStatus", v_status.get("mute", False))),
                            "sound_output":    clean_output,
                            "max_volume":      v_status.get("maxVolume", 100),
                            "external_control":   bool(v_status.get("externalDeviceControl", False)),
                            "can_adjust_volume":  bool(v_status.get("adjustVolume", True)),
                        })
                except Exception as e:
                    log.debug(f"[{self.device_id}] Audio status sync skipped: {e}")

                # --- STAGE 2: Poll Input ---
                try:
                    app_info = await self._ssap_request(
                        ip, "ssap://com.webos.applicationManager/getForegroundAppInfo"
                    )
                    if app_info and "appId" in app_info:
                        active_id = app_info.get("appId", "")
                        raw_key = self.REVERSE_APP_MAP.get(active_id, active_id)
                        self.set_state("input", self._display_name(raw_key))
                    else:
                        if self.get_state("input") == "Syncing...":
                            self.set_state("input", "Live TV / Dashboard")
                except Exception:
                    pass

        except Exception as e:
            log.debug(f"[{self.device_id}] General Poll error: {e}")

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        mac    = self.config.get("mac_address", "").strip()
        ip     = self.config.get("host",  "").strip()
        params = params or {}

        def _parse_bool(val: Any) -> bool:
            if isinstance(val, str):
                return val.strip().lower() in ("true", "on", "1", "yes")
            return bool(val)

        # --- Diagnostic ---

        if command == "list_apps":
            resp = await self._ssap_request(
                ip, "ssap://com.webos.applicationManager/listLaunchPoints"
            )
            apps    = resp.get("launchPoints", [])
            summary = [{"id": a.get("id"), "title": a.get("title")} for a in apps]
            log.info(
                f"[{self.device_id}] Installed Apps ({len(summary)}):\n"
                + "\n".join(f"  {a['title']:30s}  {a['id']}" for a in summary)
            )
            return summary

        # --- Pairing ---

        if command == "force_pair":
            await self._ssap_request(ip, "ssap://system/getPowerState", register=True)
            return True

        if command == "clear_pairing":
            if os.path.exists(self._key_path):
                os.remove(self._key_path)
            self.set_state("paired", False)
            log.info(f"[{self.device_id}] Pairing token explicitly cleared via UI.")
            return True

        # --- Power ---

        if command == "power":
            is_on   = _parse_bool(params.get("value", True))
            command = "power_on" if is_on else "power_off"

        if command == "power_on":
            if await self._do_power_on(mac, ip):
                self._poll_locked_until = asyncio.get_running_loop().time() + 5
                self.set_state("power", "on")
                return True
            return False

        if command == "power_off":
            self._poll_locked_until = asyncio.get_running_loop().time() + 15
            await self._ssap_request(ip, "ssap://system/turnOff")
            self.set_state("power", "off")
            return True

        # --- Volume (shared guard) ---
        if command in ("set_volume", "volume_up", "volume_down"):
            can_adjust = self.get_state("can_adjust_volume")
            if can_adjust is False:
                log.warning(
                    f"[{self.device_id}] Volume command '{command}' blocked: "
                    "external device has volume control (can_adjust_volume=False)."
                )
                return False

        if command == "set_volume":
            try:
                raw_target = params.get("level") if params.get("level") is not None else params.get("value")
                if raw_target is None:
                    return False

                target_vol = int(raw_target)
                use_pulse = bool(self.get_state("external_control"))
                if not use_pulse:
                    # Direct set — instant, exact, no loop required
                    await self._ssap_request(ip, "ssap://audio/setVolume", {"volume": target_vol})
                    self.set_state("volume", target_vol)
                    return True

                # --- ARC/CEC pulse path ---
                current_vol = self.get_state("volume")
                if current_vol is None:
                    current_vol = 0
                diff = target_vol - current_vol
                if diff == 0:
                    return True

                self._volume_target = target_vol  # register intent; newer calls will update this
                log.debug(f"[{self.device_id}] ARC pulse: {current_vol} -> {target_vol}")
                pulse_uri = "ssap://audio/volumeUp" if diff > 0 else "ssap://audio/volumeDown"

                for _ in range(abs(diff)):
                    # If a newer set_volume arrived, this loop is stale — abort cleanly
                    if self._volume_target != target_vol:
                        log.debug(f"[{self.device_id}] Volume superseded at {target_vol}, aborting pulse")
                        return True
                    await self._ssap_request(ip, pulse_uri)
                    await asyncio.sleep(0.01)

                self.set_state("volume", target_vol)
                return True

            except Exception as e:
                log.error(f"[{self.device_id}] set_volume failure: {e}")
                return False

        if command == "volume_up":
            if await self._ssap_request(ip, "ssap://audio/volumeUp") is not None:
                return True

        if command == "volume_down":
            if await self._ssap_request(ip, "ssap://audio/volumeDown") is not None:
                return True

        # --- Audio ---

        if command == "mute":
            val = _parse_bool(params.get("value", False))
            if await self._ssap_request(ip, "ssap://audio/setMute", {"mute": val}) is not None:
                self.set_state("mute", val)
                return True
            return False

        # --- Navigation & Menu Control ---
        nav_buttons = {
            "cursor_up":    "UP", 
            "cursor_down":  "DOWN", 
            "cursor_left":  "LEFT", 
            "cursor_right": "RIGHT", 
            "enter":        "ENTER", 
            "back":         "BACK", 
            "home":         "HOME", 
            "menu":         "MENU"
        }
        
        if command in nav_buttons:
            return await self._send_pointer_button(ip, nav_buttons[command])
                
        # --- Input / App ---

        if command == "launch_app":
            app_id = str(params.get("id", "")).strip()
            if await self._ssap_request(
                ip, "ssap://system.launcher/launch", {"id": app_id}
            ) is not None:
                raw_key = self.REVERSE_APP_MAP.get(app_id, app_id)
                self.set_state("input", self._display_name(raw_key))
                return True
            return False

        if command == "set_input":
            raw_id    = str(params.get("id", "HDMI_1")).strip()
            inp       = raw_id.upper()
            target_id = self.APP_MAP.get(inp, raw_id)

            await self._ssap_request(
                ip, "ssap://system.launcher/launch", {"id": target_id}
            )
            if "HDMI" in inp:
                await asyncio.sleep(0.2)
                await self._ssap_request(ip, "ssap://tv/switchInput", {"inputId": inp})
            self.set_state("input", self._display_name(inp))
            return True

        log.warning(f"[{self.device_id}] Unhandled command: '{command}'")
        return False
    # ------------------------------------------------------------------
    # SSAP transport
    # ------------------------------------------------------------------
    async def _ssap_request(
        self,
        ip:       str,
        uri:      str,
        payload:  dict | None = None,
        register: bool        = True,
    ) -> dict:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = ssl.CERT_NONE

        try:
            async with websockets.connect(
                f"wss://{ip}:3001", ssl=ssl_ctx, open_timeout=2.0
            ) as ws:

                if register:
                    client_key = ""
                    if os.path.exists(self._key_path):
                        with open(self._key_path, "r") as fh:
                            client_key = fh.read().strip()

                    reg_pkt: dict = {
                        "type": "register",
                        "id":   "reg0",
                        "payload": {
                            "pairingType": "PROMPT",
                            "manifest": {
                                "permissions": [
                                    "CONTROL_POWER",
                                    "CONTROL_AUDIO",
                                    "CONTROL_INPUT_TV",
                                    "CONTROL_DISPLAY",
                                    "LAUNCH",
                                    "READ_INSTALLED_APPS",
                                    "READ_CURRENT_APP",
                                    "READ_RUNNING_APPS",
                                    "CONTROL_MOUSE_AND_KEYBOARD",
                                ],
                            },
                        },
                    }
                    if client_key:
                        reg_pkt["payload"]["client-key"] = client_key

                    await ws.send(json.dumps(reg_pkt))

                    loop         = asyncio.get_running_loop()
                    deadline     = loop.time() + (30.0 if not client_key else 5.0)
                    is_registered = False

                    while loop.time() < deadline:
                        timeout_left = deadline - loop.time()
                        if timeout_left <= 0:
                            break
                        reg_resp = json.loads(
                            await asyncio.wait_for(ws.recv(), timeout=timeout_left)
                        )
                        if reg_resp.get("type") == "error":
                            return {}
                        if reg_resp.get("type") == "registered":
                            new_key = reg_resp.get("payload", {}).get("client-key")
                            if new_key and new_key != client_key:
                                with open(self._key_path, "w") as fh:
                                    fh.write(new_key)
                            is_registered = True
                            break

                    if not is_registered:
                        return {}

                cmd_pkt: dict = {"type": "request", "id": "req_1", "uri": uri}
                if payload:
                    cmd_pkt["payload"] = payload
                await ws.send(json.dumps(cmd_pkt))

                loop     = asyncio.get_running_loop()
                deadline = loop.time() + 2.0
                while loop.time() < deadline:
                    timeout_left = deadline - loop.time()
                    if timeout_left <= 0:
                        break
                    resp = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=timeout_left)
                    )
                    if resp.get("type") == "error":
                        return {}
                    if resp.get("type") == "response":
                        return resp.get("payload", {})

                return {}

        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Helper to open a pointer socket and send a physical remote button press.
    # ------------------------------------------------------------------
    async def _send_pointer_button(self, ip: str, button_name: str) -> bool:
        """Emulates a physical remote button press via a background pointer socket."""
        endpoint = "ssap://com.webos.service.networkinput/getPointerInputSocket"
        res = await self._ssap_request(ip, endpoint)
        
        if not res or "socketPath" not in res:
            # Keep this error as it indicates a permission/pairing issue
            log.error(f"[{self.device_id}] Pointer access denied. Check 'Mouse & Keyboard' permissions.")
            return False
            
        try:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode    = ssl.CERT_NONE
            
            # Reduced timeout to 1.0s so UI doesn't hang if TV drops the connection
            async with websockets.connect(res["socketPath"], ssl=ssl_ctx, open_timeout=1.0) as ws:
                await ws.send(f"type:button\nname:{button_name}\n\n")
                return True
        except Exception:
            # Silent fail for network hiccups to keep logs clean
            return False

    # ------------------------------------------------------------------
    # Wake-on-LAN
    # ------------------------------------------------------------------
    async def _do_power_on(self, mac: str, ip: str) -> bool:
        try:
            m   = re.sub(r"[:\-.]", "", mac).upper()
            pkt = b"\xFF" * 6 + bytes.fromhex(m) * 16
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(pkt, ("255.255.255.255", 9))
                s.sendto(pkt, (ip, 9))
            return True
        except Exception: 
            return False

    # ------------------------------------------------------------------
    # Network probe
    # ------------------------------------------------------------------
    async def _check_ssap_raw(self, ip: str) -> bool:
        try:
            conn = asyncio.open_connection(ip, 3001)
            _, writer = await asyncio.wait_for(conn, timeout=1.5)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception: 
            return False