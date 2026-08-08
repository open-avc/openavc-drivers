"""
Poly Studio (VideoOS) — REST API Simulator.

Models the device side of Poly's documented VideoOS REST API on
HTTPS-equivalent (the simulator runs plain HTTP — TLS termination is
the OS layer's problem, not the protocol's). Implements:

  - ``POST /rest/session`` cookie-based admin login. Returns
    ``Set-Cookie: session=<id>`` and the device's ``loginStatus``
    envelope. Bad credentials get HTTP 403.
  - ``DELETE /rest/session`` to log out.
  - All other paths require the session cookie (or a recent login)
    or return HTTP 401.
  - ``GET / POST /rest/audio/muted`` — bare boolean toggling the
    microphone mute state.
  - ``GET / POST /rest/audio/volume`` — bare integer in Poly's 0-50
    speaker scale.
  - ``GET / POST /rest/video/local/mute`` — bare boolean privacy
    mute.
  - ``GET /rest/cameras/near/all`` — minimal payload reporting one
    near camera.
  - ``POST /rest/cameras/near/SELECTED_PEOPLE`` — accepts moveStart
    / moveStop with a direction.
  - ``POST /rest/cameras/near/presets/<index>`` — accepts ``store``
    and ``activate`` actions.
  - ``GET /rest/conferences`` and
    ``DELETE /rest/conferences/<confID>`` — track an in-memory list
    of active calls; the simulator's error_modes table can inject a
    fake call so the hangup path can be exercised.
  - ``POST /rest/system/reboot`` — flips a transient
    ``rebooting`` state flag and clears it after a short delay so
    long-running tests can still poll.
  - ``GET /rest/system/status`` — minimal status list.

Driver side: ``video/poly_studio.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any

from aiohttp import web

from openavc.simulator.http_simulator import HTTPSimulator

logger = logging.getLogger(__name__)


VALID_DIRECTIONS = {
    "MOVE_LEFT",
    "MOVE_RIGHT",
    "MOVE_UP",
    "MOVE_DOWN",
    "MOVE_ZOOMIN",
    "MOVE_ZOOMOUT",
    "MOVE_FOCUSNEAR",
    "MOVE_FOCUSFAR",
}


class PolyStudioSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "poly_studio",
        "name": "Poly Studio Simulator",
        "category": "video",
        "transport": "http",
        "default_port": 443,
        "initial_state": {
            "audio_mute": False,
            "video_mute": False,
            "volume": 25,
            "system_name": "Studio X50 (sim)",
            "rebooting": False,
        },
        "controls": [
            {
                "type": "indicator",
                "key": "audio_mute",
                "label": "Mic Mute",
            },
            {
                "type": "indicator",
                "key": "video_mute",
                "label": "Privacy Mute",
            },
            {
                "type": "indicator",
                "key": "volume",
                "label": "Volume",
            },
            {
                "type": "indicator",
                "key": "in_call",
                "label": "In Call",
            },
        ],
        "delays": {"command_response": 0.005},
        "error_modes": {
            "auth_fail": {
                "description": "Reject /rest/session login attempts",
                "set_state": {"force_auth_fail": True},
            },
            "incoming_call": {
                "description": (
                    "Inject a fake active call so hangup can be "
                    "exercised against the simulator"
                ),
                "set_state": {"force_call_active": True},
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Session id -> issued-at monotonic time. A real Poly bar
        # supports a handful of concurrent sessions; the sim caps at 8.
        self._sessions: dict[str, float] = {}
        # Camera preset slots populated on-demand by ``store``.
        self._presets: dict[int, dict[str, Any]] = {}
        # Active call list. ``incoming_call`` error mode injects one.
        self._calls: list[dict[str, Any]] = []

    # ── Configuration helpers ──

    def _username(self) -> str:
        return str(self.config.get("username", "admin") or "admin")

    def _password(self) -> str:
        return str(self.config.get("password", "") or "")

    # ── Override the framework dispatcher to handle cookies ──

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.method
        path = "/" + request.match_info.get("path", "")
        body_text = await request.text()
        self.log_protocol("in", f"{method} {path} | {body_text[:200]}")

        delay = self._delays.get("command_response") or 0
        if delay > 0:
            await asyncio.sleep(delay)

        # Reflect ``incoming_call`` error-mode -> active-call list.
        if (
            self.state.get("force_call_active")
            and not self._calls
        ):
            self._calls = [
                {
                    "id": "0",
                    "isActive": True,
                    "startTime": int(time.time() * 1000),
                }
            ]

        # /rest/session is the only path that can be hit without a
        # session cookie — it's how you GET one in the first place.
        if path == "/rest/session":
            return self._handle_session(method, body_text)

        if not self._is_authenticated(request):
            return web.json_response(
                {"error": "Not authenticated"}, status=401
            )

        return await self._handle_authenticated(method, path, body_text)

    # ── Session endpoint ──

    def _handle_session(self, method: str, body_text: str) -> web.Response:
        if method == "POST":
            if self.state.get("force_auth_fail"):
                return web.json_response(
                    {"success": False, "loginStatus": {}}, status=403
                )
            try:
                payload = json.loads(body_text) if body_text else {}
            except json.JSONDecodeError:
                return web.json_response(
                    {"error": "Invalid JSON"}, status=400
                )
            if (
                str(payload.get("user", "")) != self._username()
                or str(payload.get("password", "")) != self._password()
            ):
                return web.json_response(
                    {"success": False, "loginStatus": {}}, status=403
                )
            session_id = secrets.token_urlsafe(24)
            # Cap at 8 concurrent sessions like the real device.
            if len(self._sessions) >= 8:
                # Drop the oldest.
                oldest = min(self._sessions, key=self._sessions.get)
                self._sessions.pop(oldest, None)
            self._sessions[session_id] = time.monotonic()
            resp = web.json_response(
                {
                    "success": True,
                    "loginStatus": {
                        "loginResult": "NOLOCKOUT",
                        "failedLogins": 0,
                    },
                    "session": {
                        "sessionId": session_id,
                        "role": "ADMIN",
                        "userId": self._username(),
                        "isAuthenticated": True,
                        "isNew": True,
                    },
                },
                status=200,
            )
            resp.set_cookie("session", session_id, httponly=True)
            return resp

        if method == "DELETE":
            # Best-effort logout — drop whatever the request presents.
            return web.json_response({"success": True}, status=200)

        return web.json_response(
            {"error": "Method Not Allowed"}, status=405
        )

    # ── Auth gate ──

    def _is_authenticated(self, request: web.Request) -> bool:
        cookie = request.cookies.get("session", "")
        if cookie and cookie in self._sessions:
            return True
        return False

    # ── Authenticated dispatch ──

    async def _handle_authenticated(
        self, method: str, path: str, body_text: str
    ) -> web.Response:
        # Audio mute.
        if path == "/rest/audio/muted":
            if method == "GET":
                return _bool_response(
                    bool(self.state.get("audio_mute", False))
                )
            if method == "POST":
                self.set_state(
                    "audio_mute", _parse_bool_body(body_text)
                )
                return web.Response(status=200)

        # Audio volume.
        if path == "/rest/audio/volume":
            if method == "GET":
                return web.json_response(
                    int(self.state.get("volume", 0)), status=200
                )
            if method == "POST":
                try:
                    value = int(json.loads(body_text or "0"))
                except (ValueError, json.JSONDecodeError):
                    return web.json_response(
                        {"error": "Bad value"}, status=400
                    )
                self.set_state("volume", max(0, min(50, value)))
                return web.Response(status=200)

        # Video / privacy mute.
        if path == "/rest/video/local/mute":
            if method == "GET":
                return _bool_response(
                    bool(self.state.get("video_mute", False))
                )
            if method == "POST":
                self.set_state(
                    "video_mute", _parse_bool_body(body_text)
                )
                return web.Response(status=200)

        # Cameras.
        if path == "/rest/cameras/near/all" and method == "GET":
            return web.json_response(
                [
                    {
                        "cameraIndex": 1,
                        "name": "Main",
                        "model": "STUDIO_X50_INTERNAL",
                        "sourceType": "SRC_PEOPLE",
                        "ptzcapable": True,
                        "selected": True,
                        "trackable": True,
                        "connected": True,
                        "nearCamera": True,
                    }
                ],
                status=200,
            )

        if (
            path.startswith("/rest/cameras/near/")
            and not path.startswith("/rest/cameras/near/presets/")
            and method == "POST"
        ):
            return self._handle_camera_move(body_text)

        if path.startswith("/rest/cameras/near/presets/"):
            try:
                index = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return web.json_response(
                    {"error": "Bad preset index"}, status=400
                )
            return self._handle_preset(method, index, body_text)

        # Conferences.
        if path == "/rest/conferences" and method == "GET":
            return web.json_response(self._calls, status=200)

        if path.startswith("/rest/conferences/") and method == "DELETE":
            conf_id = path.rsplit("/", 1)[-1]
            self._calls = [c for c in self._calls if c.get("id") != conf_id]
            if not self._calls:
                # Clear the error-mode latch so subsequent tests start clean.
                self.set_state("force_call_active", False)
            return web.Response(status=200)

        # System.
        if path == "/rest/system" and method == "GET":
            return web.json_response(
                {
                    "systemName": str(
                        self.state.get("system_name", "Studio X50")
                    ),
                    "model": "Studio X50",
                    "serialNumber": "SIMPOLY0001",
                    "softwareVersion": "3.7.0-sim",
                    "hardwareVersion": "1",
                    "build": "sim",
                    "state": "READY",
                    "uptime": 1000,
                },
                status=200,
            )
        if path == "/rest/system/status" and method == "GET":
            return web.json_response(
                [
                    {
                        "name": "system.status.ipnetwork",
                        "stateList": ["up"],
                    },
                    {
                        "name": "system.status.microphones",
                        "stateList": ["up"],
                    },
                ],
                status=200,
            )
        if path == "/rest/system/reboot" and method == "POST":
            self.set_state("rebooting", True)
            asyncio.create_task(self._clear_rebooting())
            return web.Response(status=200)

        return web.json_response(
            {"error": f"Path not handled by simulator: {path}"},
            status=404,
        )

    # ── Helpers ──

    def _handle_camera_move(self, body_text: str) -> web.Response:
        try:
            payload = json.loads(body_text) if body_text else {}
        except json.JSONDecodeError:
            return web.json_response({"error": "Bad JSON"}, status=400)
        action = str(payload.get("action", ""))
        direction = str(payload.get("direction", ""))
        if action not in {"moveStart", "moveStop"}:
            return web.json_response({"error": "Bad action"}, status=400)
        if direction and direction not in VALID_DIRECTIONS:
            return web.json_response({"error": "Bad direction"}, status=400)
        # Real camera state changes are a deep rabbit hole; for the
        # simulator we just record the most recent move so tests can
        # observe it.
        self.set_state("last_camera_action", action)
        self.set_state("last_camera_direction", direction)
        return web.Response(status=200)

    def _handle_preset(
        self, method: str, index: int, body_text: str
    ) -> web.Response:
        if method == "GET":
            preset = self._presets.get(index)
            if not preset:
                return web.json_response(
                    {"error": "Preset not stored"}, status=404
                )
            return web.json_response(preset, status=200)
        if method == "POST":
            try:
                payload = json.loads(body_text) if body_text else {}
            except json.JSONDecodeError:
                return web.json_response(
                    {"error": "Bad JSON"}, status=400
                )
            action = str(payload.get("action", "")).lower()
            if action == "store":
                self._presets[index] = {
                    "imageLocation": f"/preset/{index}.jpg",
                    "index": index,
                    "near": True,
                    "stored": True,
                }
                self.set_state("last_preset_stored", index)
                return web.Response(status=200)
            if action == "activate":
                if index not in self._presets:
                    return web.json_response(
                        {"error": "Preset empty"}, status=404
                    )
                self.set_state("last_preset_activated", index)
                return web.Response(status=200)
            if action in ("clear",):
                self._presets.pop(index, None)
                return web.Response(status=200)
            return web.json_response(
                {"error": f"Bad preset action: {action}"}, status=400
            )
        return web.json_response(
            {"error": "Method Not Allowed"}, status=405
        )

    async def _clear_rebooting(self) -> None:
        await asyncio.sleep(0.5)
        self.set_state("rebooting", False)

    # The framework's abstract handle_request must exist; everything
    # is dispatched through the overridden _handle above.
    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        return (404, {"error": "Should be unreachable"})


def _bool_response(value: bool) -> web.Response:
    # The Poly devices return a bare JSON boolean for these endpoints.
    return web.Response(
        status=200,
        text=("true" if value else "false"),
        content_type="application/json",
    )


def _parse_bool_body(body_text: str) -> bool:
    text = body_text.strip().lower()
    if text in ("true", "1"):
        return True
    if text in ("false", "0", ""):
        return False
    try:
        return bool(json.loads(body_text))
    except json.JSONDecodeError:
        return False
