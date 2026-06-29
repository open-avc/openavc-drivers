"""
Simulator for the hisense_vidaa driver.

Emulates a Hisense VIDAA TV's embedded MQTT broker (port 36669, TLS) closely
enough to exercise the driver end to end without a real set: the pairing
handshake, remote keys, volume, input switching, and state read-back.

Subclasses the platform ``MQTTSimulator`` (a tiny QoS-0 broker the driver dials
out to). The driver's command topics carry a per-connection client id; this sim
captures that id from CONNECT and builds the matching response topics, then
publishes the data/broadcast messages a real TV pushes back.

Response topics (cid = the driver's MQTT client id):
  - per-client:  /remoteapp/mobile/<cid>/<service>/data/<dtype>
  - broadcast:   /remoteapp/mobile/broadcast/<service>/...

Pairing here accepts any PIN and issues a token, so the integration flow is
"Request Pairing" then "Submit Pairing PIN" with any 4 digits.
"""

from __future__ import annotations

import json
import logging

from simulator.mqtt_simulator import MQTTSimulator

logger = logging.getLogger(__name__)

# sourceid -> display name. Mirrors the driver's SOURCE_MAP (name -> id) so a
# changesource the driver sends comes back as a recognizable source name.
_SOURCES = [
    {"sourceid": "0", "sourcename": "TV"},
    {"sourceid": "1", "sourcename": "HDMI 1"},
    {"sourceid": "2", "sourcename": "HDMI 2"},
    {"sourceid": "3", "sourcename": "HDMI 3"},
    {"sourceid": "4", "sourcename": "HDMI 4"},
    {"sourceid": "5", "sourcename": "AV"},
]
_SOURCE_BY_ID = {s["sourceid"]: s["sourcename"] for s in _SOURCES}

# Broadcast topics the driver subscribes to for unsolicited pushes.
_BCAST_STATE = "/remoteapp/mobile/broadcast/ui_service/state"
_BCAST_VOLUME = "/remoteapp/mobile/broadcast/platform_service/actions/volumechange"


class HisenseVidaaSimulator(MQTTSimulator):
    """Minimal VIDAA TV: pairing + remote keys + volume + input + state."""

    SIMULATOR_INFO = {
        "driver_id": "hisense_vidaa",
        "name": "Hisense VIDAA TV Simulator",
        "category": "display",
        "transport": "mqtt",
        "default_port": 36669,
        "tls": True,  # the driver always connects over TLS
        "initial_state": {
            "power": True,
            "volume": 20,
            "mute": False,
            "source": "TV",
            "authenticated": False,
        },
        "error_modes": {
            "no_response": {
                "description": "TV accepts publishes but never answers",
                "behavior": "no_response",
            },
        },
    }

    # ── Topic helpers ──

    def _cid(self, client_id: str, topic: str) -> str:
        """The driver's MQTT client id, from CONNECT (falling back to the topic).

        Command topics are /remoteapp/tv/<service>/<cid>/actions/<action>, so the
        client id is the 5th segment if the connect-time value is unavailable.
        """
        meta = self._client_meta.get(client_id) or {}
        cid = meta.get("mqtt_client_id")
        if cid:
            return cid
        parts = topic.split("/")
        if len(parts) >= 6 and parts[2] == "tv":
            return parts[4]
        return ""

    @staticmethod
    def _data_topic(cid: str, service: str, dtype: str) -> str:
        return f"/remoteapp/mobile/{cid}/{service}/data/{dtype}"

    # ── Command handling ──

    async def on_publish(self, client_id: str, topic: str, payload: bytes) -> None:
        if "/actions/" not in topic:
            return
        action = topic.rsplit("/actions/", 1)[-1]
        text = payload.decode("utf-8", "replace").strip() if payload else ""
        data = None
        if text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = text
        cid = self._cid(client_id, topic)

        if action == "vidaa_app_connect":
            await self._handle_app_connect(client_id, cid)
        elif action == "authenticationcode":
            await self._handle_auth_code(client_id, cid, data)
        elif action == "sendkey":
            await self._handle_sendkey(client_id, cid, text)
        elif action == "changevolume":
            await self._handle_changevolume(client_id, cid, text)
        elif action == "changesource":
            await self._handle_changesource(client_id, cid, data)
        elif action == "gettvstate":
            await self.publish_to(client_id, self._data_topic(cid, "ui_service", "state"),
                                  self._state_payload())
        elif action == "getvolume":
            await self._broadcast_volume()
        elif action == "sourcelist":
            await self.publish_to(
                client_id, self._data_topic(cid, "ui_service", "sourcelist"),
                json.dumps(_SOURCES))

    async def _handle_app_connect(self, client_id: str, cid: str) -> None:
        """A paired app gets current state; an unpaired one is asked to pair."""
        if self.get_state("authenticated"):
            await self.publish_to(
                client_id, self._data_topic(cid, "ui_service", "state"),
                self._state_payload())
        else:
            # Publishing to /authentication makes the TV show its PIN; the driver
            # flags pin_pending. Any PIN is accepted by _handle_auth_code below.
            await self.publish_to(
                client_id, self._data_topic(cid, "ui_service", "authentication"),
                json.dumps({"result": 1}))
            logger.info("%s: pairing requested (enter any PIN to pair)", self.name)

    async def _handle_auth_code(self, client_id: str, cid: str, data) -> None:
        pin = ""
        if isinstance(data, dict):
            pin = str(data.get("authNum", "")).strip()
        if not pin:
            return
        self.set_state("authenticated", True)
        token = {
            "accesstoken": "sim-access-token",
            "accesstoken_time": 0,
            "accesstoken_duration_day": 9999,
            "refreshtoken": "sim-refresh-token",
        }
        await self.publish_to(
            client_id, self._data_topic(cid, "ui_service", "tokenissuance"),
            json.dumps(token))
        # Close the on-screen PIN dialog, then push current state.
        await self.publish_to(
            client_id,
            self._data_topic(cid, "ui_service", "authenticationcodeclose"), "{}")
        await self.publish_to(
            client_id, self._data_topic(cid, "ui_service", "state"),
            self._state_payload())

    async def _handle_sendkey(self, client_id: str, cid: str, key: str) -> None:
        if key == "KEY_POWER":
            self.set_state("power", not self.get_state("power"))
            await self._broadcast_state()
        elif key == "KEY_VOLUMEUP":
            self.set_state("volume", min(100, int(self.get_state("volume") or 0) + 1))
            await self._broadcast_volume()
        elif key == "KEY_VOLUMEDOWN":
            self.set_state("volume", max(0, int(self.get_state("volume") or 0) - 1))
            await self._broadcast_volume()
        elif key == "KEY_MUTE":
            self.set_state("mute", not self.get_state("mute"))
            await self._broadcast_volume()
        # Other keys (navigation, channel, transport) are accepted with no
        # modeled state change.

    async def _handle_changevolume(self, client_id: str, cid: str, text: str) -> None:
        try:
            level = max(0, min(100, int(text)))
        except (TypeError, ValueError):
            return
        self.set_state("volume", level)
        self.set_state("mute", False)
        await self._broadcast_volume()

    async def _handle_changesource(self, client_id: str, cid: str, data) -> None:
        if not isinstance(data, dict):
            return
        source_id = str(data.get("sourceid", "")).strip()
        name = _SOURCE_BY_ID.get(source_id, source_id or self.get_state("source"))
        self.set_state("source", name)
        self.set_state("power", True)
        await self._broadcast_state()

    # ── State payloads + broadcasts ──

    def _state_payload(self) -> str:
        if not self.get_state("power"):
            return json.dumps({"statetype": "fake_sleep_0"})
        return json.dumps({
            "statetype": "sourceswitch",
            "sourcename": self.get_state("source") or "TV",
        })

    async def _broadcast_state(self) -> None:
        await self.broadcast(_BCAST_STATE, self._state_payload())

    async def _broadcast_volume(self) -> None:
        payload = json.dumps({
            "volume_type": 0,
            "volume_value": int(self.get_state("volume") or 0),
            "mute": bool(self.get_state("mute")),
        })
        await self.broadcast(_BCAST_VOLUME, payload)
