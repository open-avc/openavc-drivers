"""
Simulator for the LG webOS TV driver (lg_webos).

Speaks enough SSAP over WebSocket to exercise the driver end to end: the
pairing handshake, id-correlated request/response, subscriptions that deliver
an initial value and then push on every state change, and the pointer-input
socket used by the navigation buttons. Runs on the platform WebSocketSimulator
base (plain ws:// on localhost; the simulation redirect turns the driver's TLS
off to match).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from aiohttp import web

from openavc.simulator.websocket_simulator import WebSocketSimulator

# subscription id -> the state topic it tracks. Mirrors the driver's sub ids.
_SUB_TOPICS = {
    "sub_power": "power",
    "sub_volume": "volume",
    "sub_foreground": "foreground",
}

# Which state keys feed which subscription topic (a mute change pushes on the
# same volume subscription the real TV uses).
_KEY_TO_TOPIC = {
    "power": "power",
    "volume": "volume",
    "mute": "volume",
    "sound_output": "volume",
    "app_id": "foreground",
}

_INPUTS = [
    {"id": "HDMI_1", "label": "HDMI 1", "appId": "com.webos.app.hdmi1", "connected": False},
    {"id": "HDMI_2", "label": "HDMI 2", "appId": "com.webos.app.hdmi2", "connected": True},
    {"id": "AV_1", "label": "AV", "appId": "com.webos.app.externalinput.av1", "connected": False},
]

_APPS = [
    {"id": "com.webos.app.livetv", "title": "Live TV"},
    {"id": "netflix", "title": "Netflix"},
    {"id": "youtube.leanback.v4", "title": "YouTube"},
]


class LgWebosSimulator(WebSocketSimulator):
    """A minimal but faithful webOS SSAP TV."""

    SIMULATOR_INFO: dict[str, Any] = {
        "driver_id": "lg_webos",
        "name": "LG webOS TV Simulator",
        "category": "display",
        "transport": "tcp",
        "default_port": 3001,
        "initial_state": {
            "power": "Active",                 # SSAP power-state string
            "volume": 12,
            "mute": False,
            "sound_output": "tv_external_speaker",
            "app_id": "com.webos.app.hdmi2",
        },
        "controls": [
            {"type": "select", "key": "power", "label": "Power",
             "options": ["Active", "Suspend", "Screen Off"]},
            {"type": "slider", "key": "volume", "label": "Volume",
             "min": 0, "max": 100, "step": 1},
            {"type": "toggle", "key": "mute", "label": "Mute"},
            {"type": "select", "key": "app_id", "label": "Foreground",
             "options": ["com.webos.app.hdmi1", "com.webos.app.hdmi2",
                         "com.webos.app.livetv", "netflix"]},
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "TV stops answering",
                "behavior": "no_response",
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # client -> {topic: sub_id} for the connections that subscribed.
        self._subscriptions: dict[int, dict[str, str]] = {}
        self._clients_by_key: dict[int, web.WebSocketResponse] = {}
        # Push state topics when the UI (or a command) mutates the state.
        self.add_change_listener(self._on_state_change)

    async def on_client_connect(self, client: web.WebSocketResponse) -> None:
        self._clients_by_key[id(client)] = client
        self._subscriptions[id(client)] = {}

    async def on_client_disconnect(self, client: web.WebSocketResponse) -> None:
        self._clients_by_key.pop(id(client), None)
        self._subscriptions.pop(id(client), None)

    async def handle_message(self, client: web.WebSocketResponse, message: str) -> None:
        stripped = message.lstrip()
        # The pointer-input socket sends plain "type:button\nname:UP" frames.
        if stripped.startswith("type:button"):
            m = re.search(r"name:\s*(\S+)", message)
            if m:
                self.set_state("last_button", m.group(1))
            return

        try:
            msg = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msg, dict):
            return

        mtype = msg.get("type")
        mid = msg.get("id")
        uri = msg.get("uri", "")
        payload = msg.get("payload") or {}

        if mtype == "register":
            await self.send(client, json.dumps({
                "type": "registered", "id": mid,
                "payload": {"client-key": "openavc-sim-key"},
            }))
            return

        if mtype == "subscribe":
            topic = _SUB_TOPICS.get(mid)
            if topic:
                self._subscriptions.setdefault(id(client), {})[topic] = mid
                # Deliver the initial value the way the TV does.
                await self.send(client, json.dumps({
                    "type": "response", "id": mid,
                    "payload": {**self._topic_payload(topic), "subscribed": True},
                }))
            return

        if mtype == "request":
            result = await self._dispatch(uri, payload)
            await self.send(client, json.dumps({
                "type": "response", "id": mid, "payload": result,
            }))
            return

    # ── Request dispatch ───────────────────────────────────────────────────

    async def _dispatch(self, uri: str, payload: dict) -> dict:
        if uri.endswith("/getSystemInfo"):
            return {"returnValue": True, "modelName": "OpenAVC-SIM-TV",
                    "receiverType": "ATSC"}
        if uri.endswith("/power/getPowerState"):
            return self._power_payload()
        if uri.endswith("audio/getVolume"):
            return self._volume_payload()
        if uri.endswith("audio/getStatus"):
            vp = self._volume_payload()
            vp["volume"] = self.get_state("volume")
            vp["mute"] = self.get_state("mute")
            return vp
        if uri.endswith("applicationManager/getForegroundAppInfo"):
            return self._foreground_payload()
        if uri.endswith("tv/getExternalInputList"):
            return {"returnValue": True, "devices": _INPUTS}
        if uri.endswith("applicationManager/listLaunchPoints"):
            return {"returnValue": True, "launchPoints": _APPS}
        if uri.endswith("networkinput/getPointerInputSocket"):
            return {"returnValue": True,
                    "socketPath": f"ws://127.0.0.1:{self.port}/pointer"}

        if uri.endswith("audio/setVolume"):
            self.set_state("volume", int(payload.get("volume", self.get_state("volume"))))
            return {"returnValue": True, "volume": self.get_state("volume")}
        if uri.endswith("audio/volumeUp"):
            self.set_state("volume", min(100, int(self.get_state("volume", 0)) + 1))
            return {"returnValue": True, "volume": self.get_state("volume")}
        if uri.endswith("audio/volumeDown"):
            self.set_state("volume", max(0, int(self.get_state("volume", 0)) - 1))
            return {"returnValue": True, "volume": self.get_state("volume")}
        if uri.endswith("audio/setMute"):
            self.set_state("mute", bool(payload.get("mute")))
            return {"returnValue": True, "muteStatus": self.get_state("mute")}
        if uri.endswith("system.launcher/launch"):
            app_id = payload.get("id", "")
            if app_id:
                self.set_state("app_id", app_id)
            return {"returnValue": True, "id": app_id}
        if uri.endswith("tv/switchInput"):
            return {"returnValue": True}
        if uri.endswith("system/turnOff"):
            self.set_state("power", "Suspend")
            return {"returnValue": True}

        return {"returnValue": False, "errorText": f"unsupported: {uri}"}

    # ── Payload builders ───────────────────────────────────────────────────

    def _power_payload(self) -> dict:
        return {"returnValue": True, "state": self.get_state("power", "Active")}

    def _volume_payload(self) -> dict:
        return {
            "returnValue": True,
            "volumeStatus": {
                "volume": int(self.get_state("volume", 0)),
                "muteStatus": bool(self.get_state("mute", False)),
                "maxVolume": 100,
                "adjustVolume": True,
                "activeStatus": True,
                "soundOutput": self.get_state("sound_output", "tv_external_speaker"),
                "mode": "normal",
            },
        }

    def _foreground_payload(self) -> dict:
        return {"returnValue": True, "appId": self.get_state("app_id", "")}

    def _topic_payload(self, topic: str) -> dict:
        if topic == "power":
            return self._power_payload()
        if topic == "volume":
            return self._volume_payload()
        return self._foreground_payload()

    # ── Push on state change ───────────────────────────────────────────────

    def _on_state_change(self, change_type: str, data: dict) -> None:
        if change_type != "state":
            return
        topic = _KEY_TO_TOPIC.get(data.get("key"))
        if topic:
            # set_state runs synchronously; fan the push out on the loop.
            asyncio.ensure_future(self._push_topic(topic))

    async def _push_topic(self, topic: str) -> None:
        payload = {**self._topic_payload(topic), "subscribed": True}
        for key, subs in list(self._subscriptions.items()):
            sub_id = subs.get(topic)
            client = self._clients_by_key.get(key)
            if sub_id and client is not None:
                await self.send(client, json.dumps({
                    "type": "response", "id": sub_id, "payload": payload,
                }))
