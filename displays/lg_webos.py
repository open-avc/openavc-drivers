"""
LG webOS TV driver for OpenAVC.

Why Python (not YAML): webOS control is SSAP over a persistent secure
WebSocket (wss://<ip>:3001) — a transport the declarative YAML layer does not
speak — behind a pairing handshake, and the TV *pushes* power / volume / mute /
input changes over that one connection via subscriptions. The driver holds a
single connection, subscribes, and reflects only what the TV reports; it never
fabricates state from the commands it sends. Power-on from standby uses
Wake-on-LAN because the TV exposes no network power-on.

Protocol source of truth: verified against real hardware (LG 65UN6950ZUA,
webOS 4.x). LG publishes no standalone SSAP specification for consumer TVs, so
every command and reply shape here was confirmed live against the device rather
than taken from a third-party implementation.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import ssl
from typing import Any

import websockets

from server.drivers.base import BaseDriver, ConnectionFaultError
from server.system_config import get_system_config
from server.utils.logger import get_logger

log = get_logger(__name__)


# webOS pairing manifest. The permission set is broad on purpose: the TV grants
# exactly once (the on-screen prompt), and a missing permission silently
# disables a feature — CONTROL_MOUSE_AND_KEYBOARD, for instance, is what makes
# getPointerInputSocket return a socket for the navigation buttons.
_MANIFEST = {
    "manifestVersion": 1,
    "appVersion": "1.1",
    "signed": {
        "created": "20140509",
        "appId": "com.lge.test",
        "vendorId": "com.lge",
        "localizedAppNames": {"": "OpenAVC"},
        "localizedVendorNames": {"": "LG Electronics"},
        "permissions": [
            "TEST_SECURE", "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD",
            "READ_INSTALLED_APPS", "READ_LGE_SDX", "READ_NOTIFICATIONS",
            "SEARCH", "WRITE_SETTINGS", "WRITE_NOTIFICATION_ALERT",
            "CONTROL_POWER", "READ_CURRENT_CHANNEL", "READ_RUNNING_APPS",
            "READ_UPDATE_INFO", "UPDATE_FROM_REMOTE_APP",
            "READ_LGE_TV_INPUT_EVENTS", "READ_TV_CURRENT_TIME",
        ],
        "serial": "2f930e2d2cfe083771f68e4fe7bb07",
    },
    "permissions": [
        "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE", "TEST_OPEN",
        "TEST_PROTECTED", "CONTROL_AUDIO", "CONTROL_DISPLAY",
        # CONTROL_MOUSE_AND_KEYBOARD + CONTROL_INPUT_TEXT must be in this
        # effective-permissions list (not only signed.permissions), or
        # getPointerInputSocket answers 401 and the navigation buttons fail.
        "CONTROL_MOUSE_AND_KEYBOARD", "CONTROL_INPUT_TEXT",
        "CONTROL_INPUT_JOYSTICK", "CONTROL_INPUT_MEDIA_RECORDING",
        "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_TV", "CONTROL_POWER",
        "READ_APP_STATUS", "READ_CURRENT_CHANNEL", "READ_INPUT_DEVICE_LIST",
        "READ_NETWORK_STATE", "READ_RUNNING_APPS", "READ_TV_CHANNEL_LIST",
        "WRITE_NOTIFICATION_TOAST", "READ_POWER_STATE", "READ_COUNTRY_INFO",
        "READ_INSTALLED_APPS",
    ],
}

# Physical-remote buttons sent over the pointer input socket. Value is the
# webOS button name; key is the OpenAVC command.
_NAV_BUTTONS = {
    "cursor_up": "UP",
    "cursor_down": "DOWN",
    "cursor_left": "LEFT",
    "cursor_right": "RIGHT",
    "enter": "ENTER",
    "back": "BACK",
    "home": "HOME",
    "exit": "EXIT",
    "menu": "MENU",
    "info": "INFO",
}

# soundOutput code -> friendly label.
_SOUND_OUTPUTS = {
    "tv_speaker": "TV Speakers",
    "tv_external_speaker": "TV + Optical",
    "external_optical": "Optical",
    "external_arc": "HDMI ARC",
    "external_speaker": "Optical / Aux",
    "bt_soundbar": "Bluetooth Soundbar",
    "soundbar": "Soundbar",
    "lineout": "Line Out",
    "headphone": "Headphones",
}

_POWER_ON_STATES = {"Active"}
# Everything the TV still answers on the network but is not fully on.
_STANDBY_STATES = {"Active Standby", "Suspend", "Screen Off", "Power Off"}


class SSAPError(Exception):
    """A webOS SSAP request returned type: error."""


class LgWebosDriver(BaseDriver):

    # Liveness watchdog: a receive-mostly WebSocket can die without a FIN, so
    # probe the power endpoint periodically and let the platform reconnect
    # after repeated misses (see _liveness_probe).
    HEALTH_INTERVAL_S = 30.0
    HEALTH_TIMEOUT_S = 6.0
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = "Connected, but the TV stopped answering."

    DRIVER_INFO = {
        "id": "lg_webos",
        "name": "LG webOS TV",
        "manufacturer": "LG",
        "category": "display",
        "version": "4.1.0",
        "author": "OpenAVC",
        "description": "Controls LG webOS TVs over the SSAP WebSocket protocol "
                       "with live power/volume/input feedback.",
        "source_url": "https://webostv.developer.lge.com/develop/getting-started/introduction-to-webos-tv",
        "tags": ["tv", "display", "webos", "consumer"],
        "verified": True,
        # Self-managed secure WebSocket. 'tcp' gives the device a host/port in
        # the connection editor and lets the simulator redirect apply; the
        # driver overrides connect() and never uses a platform transport.
        "transport": "tcp",
        "ports": [3001],
        "min_platform_version": "0.24.0",

        "default_config": {
            "host": "",
            "port": 3001,
            "ssl": True,
            "mac_address": "",
            "poll_interval": 0,
        },

        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 3001, "label": "Port"},
            "mac_address": {
                "type": "string",
                "label": "MAC Address",
                "description": "Needed only for Wake-on-LAN power on. Discovery "
                               "fills this in; enable 'Mobile TV On' / "
                               "Wake-on-LAN in the TV's network settings.",
            },
        },

        "state_variables": {
            "power": {"type": "enum", "values": ["off", "standby", "on"], "label": "Power"},
            "volume": {"type": "integer", "label": "Volume", "min": 0, "max": 100,
                       "step": 1, "control": True},
            "mute": {"type": "boolean", "label": "Mute", "control": True},
            "sound_output": {"type": "string", "label": "Sound Output"},
            "can_set_volume": {"type": "boolean", "label": "Volume Adjustable"},
            "max_volume": {"type": "integer", "label": "Max Volume"},
            "input": {"type": "string", "label": "Current Input / App"},
            "app_id": {"type": "string", "label": "Foreground App ID"},
            "model": {"type": "string", "label": "Model"},
            "connected": {"type": "boolean", "label": "Connected"},
            "paired": {"type": "boolean", "label": "Paired"},
            "input_options": {"type": "string", "label": "Input Options (picker)",
                              "help": "JSON {value,label} list backing Set Input."},
            "app_options": {"type": "string", "label": "App Options (picker)",
                            "help": "JSON {value,label} list backing Launch App."},
        },

        "commands": {
            "power_on": {"label": "Power On", "available_offline": True,
                         "help": "Wake the TV via Wake-on-LAN (requires the MAC "
                         "address and Wake-on-LAN enabled). Runs while the TV is "
                         "off, so a macro, panel button, or schedule can power it on."},
            "power_off": {"label": "Power Off"},
            "power": {"label": "Power", "params": {
                "value": {"type": "boolean", "required": True, "label": "On"}}},
            "set_volume": {"label": "Set Volume", "params": {
                "level": {"type": "integer", "required": True, "min": 0, "max": 100,
                          "label": "Level"}}},
            "volume_up": {"label": "Volume Up"},
            "volume_down": {"label": "Volume Down"},
            "mute": {"label": "Set Mute", "params": {
                "value": {"type": "boolean", "required": True, "label": "Mute"}}},
            "set_input": {"label": "Set Input", "params": {
                "input": {"type": "string", "required": True, "label": "Input",
                          "options_state": "input_options",
                          "help": "Pick an input, or enter an app id "
                                  "(e.g. com.webos.app.hdmi1)."}}},
            "launch_app": {"label": "Launch App", "params": {
                "app": {"type": "string", "required": True, "label": "App",
                        "options_state": "app_options",
                        "help": "Pick an installed app, or enter its id."}}},
            "cursor_up": {"label": "Up"},
            "cursor_down": {"label": "Down"},
            "cursor_left": {"label": "Left"},
            "cursor_right": {"label": "Right"},
            "enter": {"label": "Enter / OK"},
            "back": {"label": "Back"},
            "home": {"label": "Home"},
            "exit": {"label": "Exit"},
            "menu": {"label": "Menu"},
            "info": {"label": "Info"},
            "clear_pairing": {"label": "Clear Pairing Key",
                              "help": "Forget the stored key and re-prompt on next connect."},
        },

        "quick_actions": ["power_on", "power_off", "mute"],
        "actions": [
            # Power On wakes the TV over Wake-on-LAN. A TV in standby closes its
            # control port and drops off the network, so power_on is declared
            # available_offline (see commands): the platform lets it run — and
            # keeps its button live — even while the TV is offline, and the
            # auto-reconnect brings the device back once it wakes. This works
            # from a macro, panel button, or schedule, unlike the offline setup
            # action it replaces.
            {"id": "power_on", "kind": "command", "icon": "power"},
            {"id": "power_off", "kind": "command", "icon": "power-off"},
            {"id": "mute", "kind": "command", "icon": "volume-x"},
        ],

        "help": {
            "overview": "Controls LG webOS TVs (2016 and later) over the SSAP "
                        "WebSocket protocol: power, volume, mute, input and app "
                        "switching, and on-screen navigation, with live "
                        "feedback pushed from the TV.",
            "setup": "1. Connect the TV to the network.\n"
                     "2. Add the TV here; on first connect a pairing prompt "
                     "appears on the TV screen — accept it with the remote.\n"
                     "3. For power-on from standby, set the MAC address and "
                     "enable Wake-on-LAN ('Mobile TV On') in the TV's network "
                     "settings.",
            "connection": "On first connection, accept the pairing prompt on the "
                          "TV screen. A TV in deep standby is unreachable until "
                          "woken with Wake-on-LAN.",
        },

        "discovery": {
            # Strong fingerprint: the TV's own TLS certificate on the SSAP port
            # names LG unmistakably, so a cert-only probe identifies it even
            # when SSDP is missed. (Platform >= 0.24.0.)
            "tcp_probe": {
                "port": 3001,
                "tls": True,
                "cert_subject": r"CN=LGE TV SSG",
                "timeout_ms": 3000,
            },
            # webOS TVs advertise this vendor URN over SSDP.
            "ssdp": ["urn:lge-com:service:webos-second-screen:1"],
            # LG spreads TVs across many OUI blocks (incl. wireless-module
            # vendors) — soft hints only; 60:8d:26 seen on a real webOS TV.
            "oui": [
                "60:8d:26", "00:05:c9", "00:e0:91", "10:68:3f", "2c:54:cf",
                "34:4d:f7", "38:8c:50", "58:a2:b5", "64:99:5d", "a8:23:fe",
                "bc:f1:71",
            ],
            "hostname": [r"^LGwebOSTV", r"^\[LG\]"],
            "manufacturer_alias": ["lg", "lg electronics", "lge"],
        },

        "compatible_models": [
            {
                "manufacturer": "LG",
                "models": ["webOS TVs (2016+)"],
                "confidence": "full",
                "notes": "Verified on a 65UN6950ZUA (webOS 4.x). SSAP is common "
                         "across webOS 3.0+ consumer TVs; navigation buttons and "
                         "app ids may vary by model/firmware.",
            },
        ],
    }

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ws: Any = None
        self._pointer_ws: Any = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._subs: dict[str, str] = {}          # subscription id -> handler name
        self._send_lock = asyncio.Lock()
        self._req_id = 0
        self._closing = False
        self._app_labels: dict[str, str] = {}    # appId -> friendly label
        data_dir = get_system_config().data_dir
        self._key_path = os.path.join(data_dir, f"lg_webos_key_{self.device_id}.txt")

    async def connect(self) -> None:
        self._last_transport_error = ""
        self._closing = False
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 3001))
        if not host:
            raise ConnectionFaultError(
                "No IP address is set for the TV.", code="invalid_config")

        # A TV in deep standby closes the control port — that is 'offline, wake
        # it', not a hard fault. A plain ConnectionError stays retryable.
        if not await self._verify_reachable(host, port, timeout=3.0):
            raise ConnectionError(
                f"TV at {host}:{port} is not reachable — it may be in standby "
                f"(use Wake-on-LAN to power it on).")

        scheme = "wss" if self.config.get("ssl", True) else "ws"
        url = f"{scheme}://{host}:{port}"
        try:
            self._ws = await websockets.connect(
                url,
                ssl=self._ssl_ctx() if scheme == "wss" else None,
                open_timeout=6.0,
                max_size=None,
                ping_interval=20,
                ping_timeout=10,
            )
            # Pair BEFORE the reader loop starts: the register handshake has its
            # own interim frames that the id-correlated dispatch shouldn't see.
            await self._register()
            self._reader_task = asyncio.create_task(self._read_loop())
        except Exception:
            await self._teardown_connection()
            raise

        self._connected = True
        self.set_state("connected", True)
        self.set_state("paired", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(f"[{self.device_id}] Connected to webOS TV at {host}:{port}")

        # One-time metadata + picker lists, then subscribe for live feedback.
        await self._load_metadata()
        await self._subscribe_all()
        self._start_health_loop()

    async def disconnect(self) -> None:
        self._closing = True
        self._stop_health_loop()
        await self._teardown_connection()
        self._connected = False
        self.set_state("connected", False)
        self.set_state("paired", False)
        self.set_state("power", "off")
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    def _handle_transport_disconnect(self) -> None:
        # The WebSocket dropped (reader loop ended). Cancel in-flight requests
        # and let BaseDriver flip state + schedule auto-reconnect.
        self._fail_pending()
        self._subs.clear()
        self._ws = None
        self._pointer_ws = None
        super()._handle_transport_disconnect()

    async def _teardown_connection(self) -> None:
        """Close both sockets and the reader task without touching driver state."""
        self._fail_pending()
        self._subs.clear()
        task = self._reader_task
        self._reader_task = None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for attr in ("_pointer_ws", "_ws"):
            sock = getattr(self, attr)
            if sock is not None:
                try:
                    await sock.close()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    # ── Pairing ────────────────────────────────────────────────────────────

    async def _register(self) -> None:
        """Run the SSAP pairing handshake on the freshly-opened socket.

        Sends the stored client-key when we have one (instant, no prompt);
        otherwise the TV shows an on-screen prompt and we wait for the user to
        accept. Reads directly — the general reader loop is not running yet.
        """
        key = ""
        if os.path.exists(self._key_path):
            try:
                with open(self._key_path, encoding="utf-8") as fh:
                    key = fh.read().strip()
            except OSError:
                key = ""

        payload: dict[str, Any] = {"pairingType": "PROMPT", "manifest": _MANIFEST}
        if key:
            payload["client-key"] = key
        await self._ws.send(json.dumps(
            {"type": "register", "id": "register_0", "payload": payload}))

        # A stored key registers immediately; a fresh pairing waits on the user.
        deadline = asyncio.get_event_loop().time() + (8.0 if key else 40.0)
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise ConnectionError(
                    "Pairing was not accepted — accept the prompt on the TV screen.")
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise ConnectionError(
                    "Pairing was not accepted — accept the prompt on the TV screen.")
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if msg.get("id") != "register_0":
                continue
            mtype = msg.get("type")
            if mtype == "registered":
                new_key = (msg.get("payload") or {}).get("client-key")
                if new_key and new_key != key:
                    self._store_key(new_key)
                return
            if mtype == "error":
                # A stored key the TV no longer honors: drop it so the next
                # attempt re-prompts instead of looping on a dead key.
                if key:
                    self._forget_key()
                    raise ConnectionError(
                        "Stored pairing key was rejected — re-pair on the next attempt.")
                raise ConnectionFaultError(
                    "The TV rejected pairing.", code="auth_failed")
            # Interim {"type": "response", "pairingType": "PROMPT"} — keep waiting.

    def _store_key(self, key: str) -> None:
        try:
            with open(self._key_path, "w", encoding="utf-8") as fh:
                fh.write(key)
        except OSError as exc:
            log.warning(f"[{self.device_id}] Could not store pairing key: {exc}")

    def _forget_key(self) -> None:
        try:
            os.remove(self._key_path)
        except OSError:
            pass

    # ── Reader loop + request/response ─────────────────────────────────────

    async def _read_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id")
                if mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                elif mid in self._subs:
                    self._apply_push(self._subs[mid], msg.get("payload") or {})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            # Graceful disconnect() closes the socket itself; only an
            # unsolicited drop should trigger the reconnect path.
            if not self._closing:
                self._handle_transport_disconnect()

    def _next_id(self) -> str:
        self._req_id += 1
        return f"req_{self._req_id}"

    async def _request(
        self, uri: str, payload: dict | None = None, *,
        typ: str = "request", timeout: float = 5.0,
    ) -> dict:
        """Send one SSAP request and await its id-correlated reply."""
        ws = self._ws
        if ws is None:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        rid = self._next_id()
        pkt: dict[str, Any] = {"type": typ, "id": rid, "uri": uri}
        if payload is not None:
            pkt["payload"] = payload
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        try:
            async with self._send_lock:
                await ws.send(json.dumps(pkt))
            msg = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"[{self.device_id}] SSAP {uri} timed out")
        finally:
            self._pending.pop(rid, None)
        if msg.get("type") == "error":
            raise SSAPError(str(msg.get("error", "unknown error")))
        return msg.get("payload") or {}

    # ── Subscriptions (push) ───────────────────────────────────────────────

    async def _subscribe_all(self) -> None:
        for name, uri in (
            ("power", "ssap://com.webos.service.tvpower/power/getPowerState"),
            ("volume", "ssap://audio/getVolume"),
            ("foreground", "ssap://com.webos.applicationManager/getForegroundAppInfo"),
        ):
            sid = f"sub_{name}"
            self._subs[sid] = name
            try:
                async with self._send_lock:
                    await self._ws.send(json.dumps(
                        {"type": "subscribe", "id": sid, "uri": uri}))
            except Exception as exc:
                log.debug(f"[{self.device_id}] subscribe {name} failed: {exc}")

    def _apply_push(self, name: str, payload: dict) -> None:
        if name == "power":
            self._apply_power(payload)
        elif name == "volume":
            self._apply_volume(payload)
        elif name == "foreground":
            self._apply_foreground(payload)

    def _apply_power(self, payload: dict) -> None:
        state = payload.get("state")
        if not state:
            return
        if state in _POWER_ON_STATES:
            self.set_state("power", "on")
        elif state in _STANDBY_STATES:
            self.set_state("power", "standby")
        else:
            # Unknown but reachable — treat conservatively as on.
            self.set_state("power", "on")

    def _apply_volume(self, payload: dict) -> None:
        vs = payload.get("volumeStatus", payload)
        updates: dict[str, Any] = {}
        if "volume" in vs:
            updates["volume"] = int(vs.get("volume") or 0)
        if "muteStatus" in vs:
            updates["mute"] = bool(vs.get("muteStatus"))
        elif "mute" in vs:
            updates["mute"] = bool(vs.get("mute"))
        if "maxVolume" in vs:
            updates["max_volume"] = int(vs.get("maxVolume") or 100)
        if "adjustVolume" in vs:
            updates["can_set_volume"] = bool(vs.get("adjustVolume"))
        raw_output = vs.get("soundOutput")
        if raw_output:
            updates["sound_output"] = _SOUND_OUTPUTS.get(
                raw_output, raw_output.replace("_", " ").title())
        if updates:
            self.set_states(updates)

    def _apply_foreground(self, payload: dict) -> None:
        app_id = payload.get("appId")
        if app_id is None:
            return
        self.set_state("app_id", app_id)
        self.set_state("input", self._app_labels.get(app_id) or _humanize_app(app_id))

    # ── One-time metadata / picker lists ───────────────────────────────────

    async def _load_metadata(self) -> None:
        try:
            info = await self._request("ssap://system/getSystemInfo", timeout=4.0)
            if info.get("modelName"):
                self.set_state("model", info["modelName"])
        except Exception as exc:
            log.debug(f"[{self.device_id}] getSystemInfo skipped: {exc}")

        # Inputs → picker; also builds the appId→label map for foreground display.
        try:
            data = await self._request("ssap://tv/getExternalInputList", timeout=4.0)
            options = []
            for dev in data.get("devices", []):
                app_id = dev.get("appId")
                label = dev.get("label") or app_id
                if not app_id:
                    continue
                self._app_labels[app_id] = label
                options.append({"value": app_id, "label": label})
            if options:
                self.set_state("input_options", json.dumps(options))
        except Exception as exc:
            log.debug(f"[{self.device_id}] getExternalInputList skipped: {exc}")

        # Installed apps → picker.
        try:
            data = await self._request(
                "ssap://com.webos.applicationManager/listLaunchPoints", timeout=5.0)
            options = []
            for lp in data.get("launchPoints", []):
                app_id = lp.get("id")
                title = lp.get("title") or app_id
                if not app_id:
                    continue
                self._app_labels.setdefault(app_id, title)
                options.append({"value": app_id, "label": title})
            if options:
                self.set_state("app_options", json.dumps(options))
        except Exception as exc:
            log.debug(f"[{self.device_id}] listLaunchPoints skipped: {exc}")

    # ── Liveness ───────────────────────────────────────────────────────────

    async def _liveness_probe(self) -> None:
        # Doubles as a keep-alive and a power-state refresh; a raise here counts
        # toward the watchdog and forces a reconnect.
        payload = await self._request(
            "ssap://com.webos.service.tvpower/power/getPowerState", timeout=5.0)
        self._apply_power(payload)

    # ── Commands ───────────────────────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        # power_on sends Wake-on-LAN and needs no live connection, which is why
        # the command is declared available_offline: the platform lets it run
        # whether the TV answers in network standby (a WoL nudge wakes the
        # screen) or is fully off (WoL wakes it from cold). The auto-reconnect
        # brings the device back once the TV reopens its control port.
        if command == "power_on":
            return self._wake_on_lan()

        if command == "power_off":
            await self._request("ssap://system/turnOff")
            return True

        if command == "power":
            return await self.send_command(
                "power_on" if _as_bool(params.get("value")) else "power_off")

        if command == "set_volume":
            level = int(params.get("level"))
            await self._request("ssap://audio/setVolume", {"volume": level})
            return True  # the subscription pushes the confirmed value

        if command == "volume_up":
            await self._request("ssap://audio/volumeUp")
            return True

        if command == "volume_down":
            await self._request("ssap://audio/volumeDown")
            return True

        if command == "mute":
            await self._request("ssap://audio/setMute", {"mute": _as_bool(params.get("value"))})
            return True

        if command == "set_input":
            app_id = str(params.get("input", "")).strip()
            if not app_id:
                return False
            await self._request("ssap://system.launcher/launch", {"id": app_id})
            return True

        if command == "launch_app":
            app_id = str(params.get("app", "")).strip()
            if not app_id:
                return False
            await self._request("ssap://system.launcher/launch", {"id": app_id})
            return True

        if command in _NAV_BUTTONS:
            return await self._send_button(_NAV_BUTTONS[command])

        if command == "clear_pairing":
            self._forget_key()
            self.set_state("paired", False)
            log.info(f"[{self.device_id}] Pairing key cleared")
            return True

        log.warning(f"[{self.device_id}] Unhandled command: {command}")
        return False

    # ── Navigation over the pointer input socket ───────────────────────────

    async def _send_button(self, name: str) -> bool:
        for attempt in (1, 2):
            ws = await self._pointer_socket()
            if ws is None:
                return False
            try:
                await ws.send(f"type:button\nname:{name}\n\n")
                return True
            except Exception:
                # Stale pointer socket — drop it and refetch once.
                self._pointer_ws = None
                if attempt == 2:
                    return False
        return False

    async def _pointer_socket(self) -> Any:
        ws = self._pointer_ws
        if ws is not None and _ws_is_open(ws):
            return ws
        try:
            res = await self._request(
                "ssap://com.webos.service.networkinput/getPointerInputSocket", timeout=4.0)
        except Exception as exc:
            log.debug(f"[{self.device_id}] getPointerInputSocket failed: {exc}")
            return None
        path = res.get("socketPath")
        if not path:
            return None
        use_tls = path.startswith("wss://")
        try:
            self._pointer_ws = await websockets.connect(
                path,
                ssl=self._ssl_ctx() if use_tls else None,
                open_timeout=3.0,
                max_size=None,
            )
        except Exception as exc:
            log.debug(f"[{self.device_id}] pointer socket connect failed: {exc}")
            self._pointer_ws = None
        return self._pointer_ws

    # ── Wake-on-LAN ────────────────────────────────────────────────────────

    def _wake_on_lan(self) -> bool:
        mac = str(self.config.get("mac_address", "")).strip()
        clean = re.sub(r"[^0-9A-Fa-f]", "", mac)
        if len(clean) != 12:
            log.warning(f"[{self.device_id}] Wake-on-LAN needs a valid MAC address")
            return False
        packet = b"\xff" * 6 + bytes.fromhex(clean) * 16
        host = str(self.config.get("host", "")).strip()
        sent = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(packet, ("255.255.255.255", 9))
                if host:
                    sock.sendto(packet, (host, 9))
            sent = True
        except OSError as exc:
            log.warning(f"[{self.device_id}] Wake-on-LAN send failed: {exc}")
        return sent

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _ssl_ctx() -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def _ws_is_open(ws: Any) -> bool:
    """True if a websockets connection is still open.

    websockets 16.x ClientConnection exposes ``state`` (an enum) rather than
    the legacy ``.closed`` flag, so consult it first and fall back for older
    releases.
    """
    state = getattr(ws, "state", None)
    if state is not None:
        return getattr(state, "name", "") == "OPEN"
    return not getattr(ws, "closed", False)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "on", "1", "yes")
    return bool(value)


def _humanize_app(app_id: str) -> str:
    """Best-effort friendly name for an appId with no catalog label."""
    known = {
        "com.webos.app.livetv": "Live TV",
        "com.webos.app.hdmi1": "HDMI 1",
        "com.webos.app.hdmi2": "HDMI 2",
        "com.webos.app.hdmi3": "HDMI 3",
        "com.webos.app.hdmi4": "HDMI 4",
    }
    if app_id in known:
        return known[app_id]
    tail = app_id.rsplit(".", 1)[-1]
    return tail.replace("_", " ").title()
