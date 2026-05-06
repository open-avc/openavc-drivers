"""
OpenAVC Poly Studio Driver.

Controls the Poly (HP) Studio X-series and G7500 video collaboration
bars over the public VideoOS REST API on HTTPS port 443. Devices ship
with a self-signed certificate so the driver disables verification by
default.

Models covered:
    Studio X30, Studio X50, Studio X70, Studio E70, Poly G7500. The
    REST surface is identical across the line — Poly publishes a
    single ``Poly VideoOS REST API Reference Guide`` that applies to
    all four chassis. Variation between models is what hardware is
    available (number of mics, integrated cameras, supported
    resolutions); the driver's command surface is the platform's
    common API.

Push vs poll:
    The VideoOS REST API is pull-only — there is no documented push /
    websocket / SSE channel for state changes, so polling is correct
    here. Documented choice. Default poll interval is 10 s; the
    devices handle ~10 polls/sec without issue per Poly's guidance.

Why Python (not YAML):
    Authentication is session-cookie based. The driver POSTs
    ``{user, password}`` to ``/rest/session``; the device responds
    with ``Set-Cookie: session=<id>`` which must accompany every
    subsequent request. ``ConfigurableDriver``'s declarative ``auth:``
    block today only knows ``type: telnet_login`` — there is no
    declarative cookie-session capture / replay. Per Principle 10,
    one-off custom auth is fine in Python; this is the first
    cookie-session driver, so we don't speculatively build a YAML
    extension yet. If a second driver in this shape lands (Yamaha
    DM-series, Bose ControlSpace, ClearOne Converge Pro), that's
    the trigger to ship a generic ``auth.type: post_login_cookie``
    extension.

Source:
    Poly VideoOS REST API Reference Guide (Software 3.7.0,
    September 2021), document 3725-86572-007A:
    https://kaas.hpcloud.hp.com/pdf-public/pdf_9122216_en-US-1.pdf
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.drivers.base import BaseDriver
from server.transport.http_client import HTTPClientTransport
from server.utils.logger import get_logger

log = get_logger(__name__)


# Camera move directions accepted by /rest/cameras/near/<id>.
CAMERA_DIRECTIONS = [
    "left",
    "right",
    "up",
    "down",
    "zoom_in",
    "zoom_out",
    "focus_near",
    "focus_far",
]

# Map from friendly direction name to the API's internal token.
_DIRECTION_TO_API = {
    "left": "MOVE_LEFT",
    "right": "MOVE_RIGHT",
    "up": "MOVE_UP",
    "down": "MOVE_DOWN",
    "zoom_in": "MOVE_ZOOMIN",
    "zoom_out": "MOVE_ZOOMOUT",
    "focus_near": "MOVE_FOCUSNEAR",
    "focus_far": "MOVE_FOCUSFAR",
}


class PolyStudioDriver(BaseDriver):
    """Poly Studio X / G7500 VideoOS driver."""

    DRIVER_INFO = {
        "id": "poly_studio",
        "name": "Poly Studio (VideoOS)",
        "manufacturer": "Poly",
        "category": "video",
        "version": "1.1.1",
        "author": "OpenAVC",
        "description": (
            "Controls Poly (HP) Studio X30, X50, X70, E70, and "
            "G7500 video collaboration bars via the public VideoOS "
            "REST API. Audio / video mute, volume, camera presets "
            "and direction nudges, hangup, reboot — everything "
            "needed to wire Poly bars into a touch panel."
        ),
        "source_url": "https://kaas.hpcloud.hp.com/pdf-public/pdf_9122216_en-US-1.pdf",
        "tags": ["poly", "hp", "videoconferencing", "studio", "x30", "x50", "x70", "g7500", "rest"],
        "verified": False,
        "simulated": True,
        "protocols": ["poly_videoos"],
        "ports": [443],
        "transport": "http",
        "discovery": {
            # Polycom / Poly room-system OUIs.
            "oui_prefixes": ["00:04:f2", "64:16:7f", "48:25:67"],
        },
        "compatible_models": [
            {
                "manufacturer": "Poly",
                "models": [
                    "Studio X30",
                    "Studio X50",
                    "Studio X70",
                    "Studio E70",
                    "G7500",
                ],
                "confidence": "untested",
                "notes": (
                    "VideoOS REST API is identical across the X-series "
                    "and G7500. Per-model differences (mic count, "
                    "integrated cameras, bundled mic pods) don't change "
                    "the command surface — they just gate which "
                    "features actually do anything when invoked."
                ),
            },
        ],
        "help": {
            "overview": (
                "Poly VideoOS exposes a documented REST API on "
                "HTTPS 443 for control of audio mute, volume, "
                "privacy / video mute, camera control and presets, "
                "active call hangup, and system reboot. The driver "
                "uses session-cookie authentication — log in once "
                "with the device's admin credentials and the "
                "session is reused for the lifetime of the "
                "connection."
            ),
            "setup": (
                "1. Connect the Poly bar to the network and assign "
                "a static IP.\n"
                "2. From the bar's web UI (https://<ip>) sign in "
                "as Admin and confirm the API is enabled. The "
                "factory default password is the device's serial "
                "number on first boot — change it immediately to a "
                "site-specific password.\n"
                "3. In OpenAVC, enter the bar's IP, the admin "
                "username (default ``admin``), and the password. "
                "Leave the port at 443."
            ),
        },
        "default_config": {
            "host": "",
            "port": 443,
            "username": "admin",
            "password": "",
            "verify_ssl": False,
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
            },
            "port": {
                "type": "integer",
                "default": 443,
                "label": "HTTPS Port",
            },
            "username": {
                "type": "string",
                "default": "admin",
                "label": "Admin Username",
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "Admin Password",
                "secret": True,
            },
            "verify_ssl": {
                "type": "boolean",
                "default": False,
                "label": "Verify SSL Certificate",
                "description": (
                    "Poly bars ship with a self-signed certificate. "
                    "Leave this off unless you've installed a "
                    "trusted certificate on the device."
                ),
            },
            "poll_interval": {
                "type": "integer",
                "default": 10,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "VideoOS has no push channel. Set to 0 to "
                    "disable polling."
                ),
            },
        },
        "state_variables": {
            "audio_mute": {
                "type": "boolean",
                "label": "Microphone Muted",
            },
            "video_mute": {
                "type": "boolean",
                "label": "Privacy / Video Mute",
            },
            "volume": {
                "type": "integer",
                "label": "Speaker Volume",
            },
            "in_call": {
                "type": "boolean",
                "label": "In Call",
            },
            "active_call_count": {
                "type": "integer",
                "label": "Active Call Count",
            },
            "system_name": {
                "type": "string",
                "label": "System Name",
            },
            "network_status": {
                "type": "string",
                "label": "Network Status",
            },
        },
        "commands": {
            "mute_audio": {
                "label": "Mute Microphones",
                "params": {},
            },
            "unmute_audio": {
                "label": "Unmute Microphones",
                "params": {},
            },
            "mute_video": {
                "label": "Privacy / Video Mute On",
                "params": {},
            },
            "unmute_video": {
                "label": "Privacy / Video Mute Off",
                "params": {},
            },
            "set_volume": {
                "label": "Set Volume",
                "params": {
                    "value": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 50,
                        "help": (
                            "Speaker volume on Poly's 0-50 scale."
                        ),
                    },
                },
            },
            "volume_up": {"label": "Volume Up", "params": {}},
            "volume_down": {"label": "Volume Down", "params": {}},
            "camera_preset_recall": {
                "label": "Recall Camera Preset",
                "params": {
                    "index": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 9,
                    },
                },
            },
            "camera_preset_save": {
                "label": "Save Camera Preset",
                "params": {
                    "index": {
                        "type": "integer",
                        "required": True,
                        "min": 0,
                        "max": 9,
                    },
                },
                "help": (
                    "Stores the current near-camera position into "
                    "the given preset slot. Includes a thumbnail."
                ),
            },
            "camera_move": {
                "label": "Nudge Camera",
                "params": {
                    "direction": {
                        "type": "enum",
                        "required": True,
                        "values": CAMERA_DIRECTIONS,
                    },
                    "duration_ms": {
                        "type": "integer",
                        "required": False,
                        "min": 50,
                        "max": 5000,
                        "help": (
                            "How long to hold the move before "
                            "stopping (50-5000 ms). Defaults to "
                            "300 ms."
                        ),
                    },
                },
                "help": (
                    "Issues a moveStart in the given direction, "
                    "waits, then moveStop. The driver handles the "
                    "stop so a forgotten button release won't run "
                    "the camera off the rails."
                ),
            },
            "hangup": {
                "label": "Hang Up Active Call",
                "params": {},
                "help": (
                    "Hangs up every active conference. No-op when "
                    "no call is active."
                ),
            },
            "reboot": {
                "label": "Reboot Device",
                "params": {},
                "help": "Restarts the Poly bar.",
            },
            "refresh": {
                "label": "Refresh Status",
                "params": {},
            },
        },
    }

    # Volume step for relative up/down in Poly's 0-50 scale.
    _VOLUME_STEP = 2

    def __init__(
        self,
        device_id: str,
        config: dict[str, Any],
        state,
        events,
    ) -> None:
        self._http: HTTPClientTransport | None = None
        self._authed = False
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ──

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 443))
        verify_ssl = bool(self.config.get("verify_ssl", False))
        # Newer firmware accepts http on the same port for some lab
        # configs, but the documented protocol is HTTPS — stick with it.
        scheme = "https" if port in (443, 8443) else "http"
        base_url = f"{scheme}://{host}:{port}"

        self._http = HTTPClientTransport(
            base_url=base_url,
            auth_type="none",
            verify_ssl=verify_ssl,
            timeout=8.0,
            name=self.device_id,
        )
        # The transport stores the httpx.AsyncClient; httpx's default
        # cookie jar is on, so the session cookie returned by
        # /rest/session is reused for every subsequent request without
        # any extra wiring on our side.
        await self._http.open()

        # Treat the transport as our connection surrogate so the base
        # driver's poll loop and disconnect handling work.
        self.transport = self._http

        try:
            await self._login()
        except Exception:
            await self._http.close()
            self._http = None
            self.transport = None
            raise

        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        log.info(
            f"[{self.device_id}] Connected to Poly Studio at "
            f"{host}:{port}"
        )

        # Initial status sweep.
        try:
            await self.poll()
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

        poll_interval = int(self.config.get("poll_interval", 10))
        if poll_interval > 0:
            await self.start_polling(poll_interval)

    async def disconnect(self) -> None:
        await self.stop_polling()
        # Politely close the session — Poly's /rest/session DELETE
        # ends it server-side too. Failure here is benign on a torn-
        # down link, so swallow.
        if self._authed and self._http:
            try:
                await self._http.delete("/rest/session")
            except Exception:  # noqa: BLE001
                pass
        if self._http:
            await self._http.close()
            self._http = None
        self.transport = None
        self._connected = False
        self._authed = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info(f"[{self.device_id}] Disconnected")

    async def _login(self) -> None:
        if self._http is None:
            raise ConnectionError("HTTP client not open")
        username = self.config.get("username", "admin") or "admin"
        password = self.config.get("password", "") or ""
        resp = await self._http.post(
            "/rest/session",
            body={"user": username, "password": password},
        )
        if not resp.ok:
            raise ConnectionError(
                f"[{self.device_id}] VideoOS login failed: "
                f"HTTP {resp.status_code}"
            )
        # Sanity-check the response shape — some firmware versions
        # return {success: false} with HTTP 200, so don't trust the
        # status code alone.
        data = resp.json_data or {}
        if data.get("success") is False:
            raise ConnectionError(
                f"[{self.device_id}] VideoOS login rejected — "
                "check the admin username and password"
            )
        self._authed = True

    # ── Sending ──

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        if self._http is None or not self._authed:
            log.warning(
                f"[{self.device_id}] Not authenticated — dropping "
                f"command {command}"
            )
            return
        params = params or {}

        if command == "mute_audio":
            await self._http.post("/rest/audio/muted", body=True)
            self.set_state("audio_mute", True)
        elif command == "unmute_audio":
            await self._http.post("/rest/audio/muted", body=False)
            self.set_state("audio_mute", False)
        elif command == "mute_video":
            await self._http.post("/rest/video/local/mute", body=True)
            self.set_state("video_mute", True)
        elif command == "unmute_video":
            await self._http.post("/rest/video/local/mute", body=False)
            self.set_state("video_mute", False)
        elif command == "set_volume":
            value = max(0, min(50, int(params.get("value", 0))))
            await self._http.post("/rest/audio/volume", body=value)
            self.set_state("volume", value)
        elif command == "volume_up":
            await self._adjust_volume(self._VOLUME_STEP)
        elif command == "volume_down":
            await self._adjust_volume(-self._VOLUME_STEP)
        elif command == "camera_preset_recall":
            index = int(params.get("index", 0))
            await self._http.post(
                f"/rest/cameras/near/presets/{index}",
                body={"action": "activate"},
            )
        elif command == "camera_preset_save":
            index = int(params.get("index", 0))
            await self._http.post(
                f"/rest/cameras/near/presets/{index}",
                body={"action": "store", "withImage": "Yes"},
            )
        elif command == "camera_move":
            await self._camera_move(
                str(params.get("direction", "")).strip().lower(),
                int(params.get("duration_ms", 300) or 300),
            )
        elif command == "hangup":
            await self._hangup_all()
        elif command == "reboot":
            await self._http.post(
                "/rest/system/reboot",
                body={"action": "reboot"},
            )
        elif command == "refresh":
            await self.poll()
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def _adjust_volume(self, delta: int) -> None:
        if self._http is None:
            return
        resp = await self._http.get("/rest/audio/volume")
        if not resp.ok:
            return
        try:
            current = int(resp.text.strip())
        except ValueError:
            current = self.get_state("volume") or 25
        target = max(0, min(50, current + delta))
        await self._http.post("/rest/audio/volume", body=target)
        self.set_state("volume", target)

    async def _camera_move(self, direction: str, duration_ms: int) -> None:
        if self._http is None:
            return
        api_direction = _DIRECTION_TO_API.get(direction)
        if api_direction is None:
            log.warning(
                f"[{self.device_id}] Unknown camera direction: "
                f"{direction!r}"
            )
            return
        # Use the SELECTED_PEOPLE source so the move applies to
        # whichever camera is currently active.
        path = "/rest/cameras/near/SELECTED_PEOPLE"
        await self._http.post(
            path,
            body={"action": "moveStart", "direction": api_direction},
        )
        try:
            await asyncio.sleep(max(0.05, duration_ms / 1000.0))
        finally:
            # Always issue the stop, even if cancelled / errored mid-move.
            try:
                await self._http.post(
                    path,
                    body={"action": "moveStop", "direction": api_direction},
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    f"[{self.device_id}] Failed to issue camera "
                    "moveStop — camera may keep moving"
                )

    async def _hangup_all(self) -> None:
        if self._http is None:
            return
        resp = await self._http.get("/rest/conferences")
        if not resp.ok:
            return
        items = resp.json_data
        ids: list[str] = []
        if isinstance(items, list):
            ids = [str(c.get("id")) for c in items if c.get("id")]
        elif isinstance(items, dict) and items.get("id"):
            ids = [str(items["id"])]
        if not ids:
            log.info(f"[{self.device_id}] No active call to hang up")
            return
        for conf_id in ids:
            await self._http.delete(f"/rest/conferences/{conf_id}")
        self.set_state("in_call", False)
        self.set_state("active_call_count", 0)

    # ── Polling ──

    async def poll(self) -> None:
        if self._http is None or not self._authed:
            return
        try:
            audio = await self._http.get("/rest/audio/muted")
            if audio.ok:
                self.set_state(
                    "audio_mute", _parse_bool(audio.text, audio.json_data)
                )

            volume = await self._http.get("/rest/audio/volume")
            if volume.ok:
                try:
                    self.set_state("volume", int(volume.text.strip()))
                except ValueError:
                    pass

            video = await self._http.get("/rest/video/local/mute")
            if video.ok:
                self.set_state(
                    "video_mute", _parse_bool(video.text, video.json_data)
                )

            confs = await self._http.get("/rest/conferences")
            if confs.ok:
                items = confs.json_data
                if isinstance(items, list):
                    count = len(items)
                elif isinstance(items, dict) and items:
                    count = 1
                else:
                    count = 0
                self.set_state("active_call_count", count)
                self.set_state("in_call", count > 0)

            status = await self._http.get("/rest/system/status")
            if status.ok and isinstance(status.json_data, list):
                for item in status.json_data:
                    if item.get("name") == "system.status.ipnetwork":
                        states = item.get("stateList") or []
                        if states:
                            self.set_state("network_status", states[0])
        except ConnectionError:
            log.warning(
                f"[{self.device_id}] Poll failed — not connected"
            )


def _parse_bool(text: str, json_data: Any) -> bool:
    """The /audio/muted and /video/local/mute endpoints return a bare
    JSON boolean. httpx parses it as json_data when the content type
    advertises JSON; some firmware versions return text/plain instead.
    Accept both."""
    if isinstance(json_data, bool):
        return json_data
    return text.strip().lower() == "true"
