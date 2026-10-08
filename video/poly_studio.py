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
    Poly VideoOS REST API Reference Guide (VideoOS 4.4.0, last update
    August 2025):
    https://kaas.hpcloud.hp.com/pdf-public/pdf_11545198_en-US-1.pdf
"""

from __future__ import annotations

import asyncio
from typing import Any

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.transport.http_client import HTTPClientTransport
from openavc.utils.logger import get_logger

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

# A request the bar refuses for want of a valid session. The guide gives 403
# as "Forbidden, authentication denied" (Table 3-1) and documents no 401; the
# simulator answers 401. Either one is read the same way.
_SESSION_REFUSED = (401, 403)

_LOGIN_REFUSED = (
    "The Poly bar refused the admin username and password. Check them in the "
    "device settings, or try them with Test Admin Login."
)
_SESSION_REFUSED_AFTER_LOGIN = (
    "The Poly bar accepted the admin login and then refused the session. "
    "Check the admin username and password, then press Reconnect."
)


class PolyStudioDriver(BaseDriver):
    """Poly Studio X / G7500 VideoOS driver."""

    DRIVER_INFO = {
        "id": "poly_studio",
        "name": "Poly Studio (VideoOS)",
        "manufacturer": "Poly",
        "category": "video",
        "version": "1.4.2",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.25.0",
        "author": "OpenAVC",
        "description": (
            "Controls Poly (HP) Studio X30, X50, X70, E70, and "
            "G7500 video collaboration bars via the public VideoOS "
            "REST API. Audio / video mute, volume, camera presets "
            "and direction nudges, hangup, reboot — everything "
            "needed to wire Poly bars into a touch panel."
        ),
        "source_url": "https://kaas.hpcloud.hp.com/pdf-public/pdf_11545198_en-US-1.pdf",
        "tags": ["poly", "hp", "videoconferencing", "studio", "x30", "x50", "x70", "g7500", "rest"],
        "verified": False,
        "simulated": True,
        "protocols": ["poly_videoos"],
        "ports": [443],
        "transport": "http",
        "discovery": {
            # Polycom / Poly room-system OUIs. 00:04:f2 is the original
            # Polycom MA-L (registered 2001, exhausted ~2020). 64:16:7f
            # and 9c:ad:ef are the post-2020 Polycom voice/video blocks
            # used on current Studio + G7500 / G62 hardware. 00:e0:db
            # and 00:90:27 cover legacy Polycom video gear. SSDP / mDNS
            # service strings are not publicly documented for VideoOS;
            # the candidate `urn:polycom:device:VideoOSEndpoint:1` did
            # not turn up in any vendor doc, integrator module, or
            # public PCAP — left unset until a real capture lands.
            # Polycom's and Poly's IEEE blocks, and two held by companies
            # Polycom acquired (Obihai, ViaVideo).
            "oui": [
                "00:04:f2", "64:16:7f", "48:25:67",
                "9c:ad:ef", "00:e0:db",
            ],
            "manufacturer_alias": ["poly", "polycom", "hp", "plantronics"],
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
                        "max": 100,
                        "help": (
                            "Speaker volume. The REST API takes a bare "
                            "integer and the reference guide states no "
                            "range; its own /rest/audio example reports 62."
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
        # Quick Action strip: the one-tap controls an operator reaches for on a
        # room panel, plus a setup wizard that tests (and optionally saves) the
        # admin login out-of-band — useful when the bar is offline on a bad
        # password.
        "actions": [
            {"id": "mute_audio", "kind": "command", "icon": "mic-off"},
            {"id": "unmute_audio", "kind": "command", "icon": "mic"},
            {"id": "mute_video", "kind": "command", "icon": "video-off"},
            {"id": "unmute_video", "kind": "command", "icon": "video"},
            {
                "id": "hangup",
                "kind": "command",
                "icon": "phone-off",
                "confirm": "Hang up all active calls on this Poly bar?",
            },
            {
                "id": "reboot",
                "kind": "command",
                "icon": "rotate-ccw",
                "confirm": (
                    "Reboot the Poly bar? It drops offline until it restarts."
                ),
            },
            {
                "id": "test_login",
                "kind": "setup",
                "label": "Test Admin Login",
                "icon": "key-round",
                "availability": "always",
                "params": {
                    "username": {
                        "type": "string",
                        "default": "admin",
                        "label": "Admin Username",
                    },
                    "password": {
                        "type": "password",
                        "secret": True,
                        "label": "Admin Password",
                        "help": (
                            "The bar's admin password. On first boot this is "
                            "the device serial number until it's changed."
                        ),
                    },
                    "save": {
                        "type": "boolean",
                        "default": True,
                        "label": "Save these credentials if they work",
                    },
                },
            },
        ],
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
        # One login again at a time; the count tells a request whose session
        # was refused whether someone has already signed in again since.
        self._login_lock = asyncio.Lock()
        self._logins = 0
        # Set once the bar refuses the credential on this connection; every
        # later request reports it without asking the bar again.
        self._refused = ""
        super().__init__(device_id, config, state, events)

    # ── Connection lifecycle hooks ──

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        host = self.config.get("host", "")
        port = int(self.config.get("port", 443))
        # Newer firmware accepts http on the same port for some lab
        # configs, but the documented protocol is HTTPS — stick with it.
        scheme = "https" if port in (443, 8443) else "http"
        kwargs["base_url"] = f"{scheme}://{host}:{port}"
        # Session-cookie auth, not a static header: the transport stores the
        # httpx.AsyncClient, and httpx's default cookie jar is on, so the
        # session cookie returned by /rest/session is reused for every
        # subsequent request without any extra wiring on our side.
        kwargs["auth_type"] = "none"
        kwargs["credentials"] = {}
        kwargs["verify_ssl"] = bool(self.config.get("verify_ssl", False))
        kwargs["timeout"] = 8.0
        return kwargs

    async def _post_connect(self) -> None:
        # The platform-built HTTP transport is the session client.
        self._http = self.transport
        await self._login()
        log.info(
            f"[{self.device_id}] Connected to Poly Studio at "
            f"{self.config.get('host', '')}:{int(self.config.get('port', 443))}"
        )

    async def _initial_sync(self) -> None:
        # Initial status sweep.
        try:
            await self.poll()
        except ConnectionFaultError:
            raise
        except (ConnectionError, OSError):
            log.warning(f"[{self.device_id}] Initial poll failed")

    async def disconnect(self) -> None:
        # Politely end the session. NOT IN the 4.4.0 reference guide: its
        # Session section documents POST only, so this is best-effort, kept
        # because absence from the guide is not absence from the device.
        # Must happen while the link is still open; failure here is benign on
        # a torn-down link, so swallow.
        if self._authed and self._http:
            try:
                await self._http.delete("/rest/session")
            except Exception:  # noqa: BLE001
                pass
        await super().disconnect()

    async def _close_session(self) -> None:
        # The transport itself is closed by the platform; drop the alias and
        # the session flag.
        self._http = None
        self._authed = False
        self._refused = ""

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
            # 403 LOG-IN ATTEMPT FAILED (Table 2-74), and a 401, mean the
            # credentials were refused. Anything else is a login failure that
            # says nothing about them.
            if resp.status_code in (401, 403):
                raise ConnectionFaultError(_LOGIN_REFUSED, code="auth_failed")
            raise ConnectionError(
                f"[{self.device_id}] VideoOS login failed: "
                f"HTTP {resp.status_code}"
            )
        # Sanity-check the response shape — some firmware versions
        # return {success: false} with HTTP 200, so don't trust the
        # status code alone.
        data = resp.json_data or {}
        if data.get("success") is False:
            raise ConnectionFaultError(_LOGIN_REFUSED, code="auth_failed")
        self._authed = True
        self._logins += 1

    async def _login_again(self, logins_seen: int) -> None:
        """Sign in again after the bar refused a session, once for every
        request that saw the refusal. A refused login is ``auth_failed``."""
        async with self._login_lock:
            if self._refused:
                raise ConnectionFaultError(self._refused, code="auth_failed")
            if self._logins != logins_seen:
                return  # another request has already signed in again
            log.info(f"[{self.device_id}] Session refused; signing in again")
            try:
                await self._login()
            except ConnectionFaultError as exc:
                self._refused = str(exc)
                raise

    async def _send(self, method: str, path: str, body: Any = None) -> Any:
        if self._http is None:
            raise ConnectionError("HTTP client not open")
        if method == "GET":
            return await self._http.get(path)
        if method == "DELETE":
            return await self._http.delete(path)
        return await self._http.post(path, body=body)

    async def _request(self, method: str, path: str, body: Any = None) -> Any:
        """One request on the session. A refused session signs in again once
        on this connection and the request is sent again; a refused login, or
        a session refused straight after a fresh one, raises ``auth_failed``.
        Transport failures propagate, and other statuses come back to the
        caller."""
        if self._refused:
            raise ConnectionFaultError(self._refused, code="auth_failed")
        logins_seen = self._logins
        resp = await self._send(method, path, body)
        if resp.status_code not in _SESSION_REFUSED:
            return resp
        await self._login_again(logins_seen)
        resp = await self._send(method, path, body)
        if resp.status_code in _SESSION_REFUSED:
            self._refused = _SESSION_REFUSED_AFTER_LOGIN
            raise ConnectionFaultError(
                _SESSION_REFUSED_AFTER_LOGIN, code="auth_failed"
            )
        return resp

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
            await self._request("POST", "/rest/audio/muted", True)
            self.set_state("audio_mute", True)
        elif command == "unmute_audio":
            await self._request("POST", "/rest/audio/muted", False)
            self.set_state("audio_mute", False)
        elif command == "mute_video":
            await self._request(
                "POST", "/rest/video/local/mute", {"mute": True}
            )
            self.set_state("video_mute", True)
        elif command == "unmute_video":
            await self._request(
                "POST", "/rest/video/local/mute", {"mute": False}
            )
            self.set_state("video_mute", False)
        elif command == "set_volume":
            value = max(0, min(100, int(params.get("value", 0))))
            await self._request("POST", "/rest/audio/volume", value)
            self.set_state("volume", value)
        elif command == "volume_up":
            await self._adjust_volume(self._VOLUME_STEP)
        elif command == "volume_down":
            await self._adjust_volume(-self._VOLUME_STEP)
        elif command == "camera_preset_recall":
            index = int(params.get("index", 0))
            await self._request(
                "POST",
                f"/rest/cameras/near/presets/{index}",
                {"action": "activate"},
            )
        elif command == "camera_preset_save":
            index = int(params.get("index", 0))
            await self._request(
                "POST",
                f"/rest/cameras/near/presets/{index}",
                {"action": "store", "withImage": "Yes"},
            )
        elif command == "camera_move":
            await self._camera_move(
                str(params.get("direction", "")).strip().lower(),
                int(params.get("duration_ms", 300) or 300),
            )
        elif command == "hangup":
            await self._hangup_all()
        elif command == "reboot":
            await self._request(
                "POST", "/rest/system/reboot", {"action": "reboot"}
            )
        elif command == "refresh":
            await self.poll()
        else:
            log.warning(f"[{self.device_id}] Unknown command: {command}")

    async def _adjust_volume(self, delta: int) -> None:
        if self._http is None:
            return
        resp = await self._request("GET", "/rest/audio/volume")
        if not resp.ok:
            return
        try:
            current = int(resp.text.strip())
        except ValueError:
            current = self.get_state("volume") or 25
        target = max(0, min(100, current + delta))
        await self._request("POST", "/rest/audio/volume", target)
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
        # The reference guide gives this operation its own endpoint --
        # "performs the move operation for the selected near people camera
        # source" -- so the move applies to whichever camera is active. The
        # uppercase SELECTED_PEOPLE token is a sourceID, and is documented
        # only for /rest/cameras/near/position/<sourceID>.
        path = "/rest/cameras/near/selectedpeople"
        await self._request(
            "POST", path, {"action": "moveStart", "direction": api_direction}
        )
        try:
            await asyncio.sleep(max(0.05, duration_ms / 1000.0))
        finally:
            # Always issue the stop, even if cancelled / errored mid-move.
            try:
                await self._request(
                    "POST",
                    path,
                    {"action": "moveStop", "direction": api_direction},
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    f"[{self.device_id}] Failed to issue camera "
                    "moveStop — camera may keep moving"
                )

    async def _hangup_all(self) -> None:
        if self._http is None:
            return
        resp = await self._request("GET", "/rest/conferences")
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
            await self._request("DELETE", f"/rest/conferences/{conf_id}")
        self.set_state("in_call", False)
        self.set_state("active_call_count", 0)

    # ── Polling ──

    async def poll(self) -> None:
        if self._http is None or not self._authed:
            return
        # A transport failure (host went away, session dropped) must
        # propagate: BaseDriver._poll_loop counts consecutive raises and flips
        # the device offline after ``max_missed_polls``. Swallowing
        # ConnectionError here (the old behaviour) meant an unplugged bar stayed
        # shown online forever. Only the inner value-parse guards below are
        # caught — a malformed number is a protocol quirk, not a dead link.
        audio = await self._request("GET", "/rest/audio/muted")
        if audio.ok:
            self.set_state(
                "audio_mute", _parse_bool(audio.text, audio.json_data)
            )

        volume = await self._request("GET", "/rest/audio/volume")
        if volume.ok:
            try:
                self.set_state("volume", int(volume.text.strip()))
            except ValueError:
                pass

        # Unlike /rest/audio/muted, this one answers with an OBJECT --
        # {"result": <boolean>} -- so it does NOT go through _parse_bool.
        # Reading it as a bare boolean made video_mute permanently False:
        # json_data is a dict, so the fallback compared the whole JSON text
        # against "true" and never matched.
        video = await self._request("GET", "/rest/video/local/mute")
        if video.ok and isinstance(video.json_data, dict):
            self.set_state("video_mute", bool(video.json_data.get("result")))

        confs = await self._request("GET", "/rest/conferences")
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

        # System info: GET /rest/system returns the device envelope
        # ({systemName, model, serialNumber, softwareVersion, ...}), which is
        # what fills the system_name state var.
        # NOT IN the VideoOS 4.4.0 reference guide -- it lists no bare
        # /rest/system table and the word "systemName" appears nowhere in it.
        # Kept because absence from the guide is not absence from the device,
        # and this is best-effort: a 404 leaves the name unset and nothing
        # else changes. Do not build anything on it that has to work.
        system = await self._request("GET", "/rest/system")
        if system.ok and isinstance(system.json_data, dict):
            name = system.json_data.get("systemName")
            if name:
                self.set_state("system_name", str(name))

        status = await self._request("GET", "/rest/system/status")
        if status.ok and isinstance(status.json_data, list):
            for item in status.json_data:
                if item.get("name") == "system.status.ipnetwork":
                    states = item.get("stateList") or []
                    if states:
                        self.set_state("network_status", states[0])

    # ── Setup wizard ──

    async def run_setup_action(
        self,
        action_id: str,
        params: dict[str, Any],
        progress: Any,
    ) -> dict[str, Any]:
        """Test the admin login over an out-of-band HTTPS session.

        Opens its own HTTP client (the device's normal transport may be down
        because the password is wrong), POSTs the credentials to
        ``/rest/session``, and reports whether the bar accepts them (403 =
        rejected). On success, optionally persists the credentials + reconnects.
        """
        if action_id != "test_login":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 443))
        verify_ssl = bool(self.config.get("verify_ssl", False))
        username = str(params.get("username", "admin") or "admin")
        password = str(params.get("password", "") or "")
        save = bool(params.get("save", True))
        if not host:
            raise ValueError("No IP address configured")

        scheme = "https" if port in (443, 8443) else "http"
        base_url = f"{scheme}://{host}:{port}"
        http = HTTPClientTransport(
            base_url=base_url,
            auth_type="none",
            verify_ssl=verify_ssl,
            timeout=8.0,
            name=f"{self.device_id}-setup",
        )

        await progress(f"Connecting to {host}:{port}…", 20)
        await http.open()
        try:
            await progress("Signing in…", 60)
            try:
                resp = await http.post(
                    "/rest/session",
                    body={"user": username, "password": password},
                )
            except ConnectionError as exc:
                raise ConnectionError(
                    f"Could not reach the Poly bar on {host}:{port} ({exc}). "
                    "Check the IP address and that HTTPS is reachable."
                ) from exc

            data = resp.json_data or {}
            auth_ok = resp.ok and data.get("success") is not False
            if not auth_ok:
                # 403 LOG-IN ATTEMPT FAILED, or {success:false} with 200.
                raise ConnectionError(
                    "Login rejected — check the admin username and password "
                    f"(HTTP {resp.status_code})."
                )
            # Politely end the throwaway session so we don't burn a session slot.
            try:
                await http.delete("/rest/session")
            except Exception:  # noqa: BLE001
                pass
            await progress("Credentials accepted", 90)
        finally:
            await http.close()

        saved = False
        if save:
            await self.request_config_update(
                {"username": username, "password": password}
            )
            saved = True
            await progress("Saved. Reconnecting…", 95)
            await self.request_reconnect()

        return {
            "reachable": True,
            "auth_ok": True,
            "saved": saved,
            "message": "Admin login accepted.",
        }


def _parse_bool(text: str, json_data: Any) -> bool:
    """The /audio/muted endpoint returns a bare JSON boolean. httpx parses it
    as json_data when the content type advertises JSON; some firmware versions
    return text/plain instead. Accept both.

    NOT for /video/local/mute, which answers {"result": <boolean>}: a dict
    fails the isinstance check here and the text fallback then compares the
    whole JSON body against "true", so it would read False forever."""
    if isinstance(json_data, bool):
        return json_data
    return text.strip().lower() == "true"
