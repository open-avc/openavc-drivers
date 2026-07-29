"""
Hisense VIDAA TV driver for OpenAVC.

Controls Hisense TVs running VIDAA OS — the same local control path the
RemoteNOW / VIDAA mobile app uses: an MQTT broker embedded in the TV on TCP
port 36669, MQTT v3.1.1 over TLS.

There is no published API; the protocol was reverse-engineered by the community
(see github.com/tombabolewski/vidaa-control, sehaas/ha_hisense_tv,
Krazy998/mqtt-hisensetv). Key facts this driver implements:

- TLS on 36669 with a self-signed broker cert (no verification). Newer models
  also require a CLIENT certificate (mutual TLS). OpenAVC does not ship Hisense's
  cert; if a TV needs one, paste the client cert + key into the device's
  Connection settings (see the help text). The driver tries without a cert
  first.
- Credentials: older TVs accept a static username/password; newer VIDAA TVs
  require dynamic, per-connection credentials derived from a device id +
  timestamp. The driver tries the dynamic scheme first, then the static one.
- Topics embed a per-connection client id, so they are built at connect time.
- Pairing: the TV shows a 4-digit PIN. Run "Request Pairing" (the TV displays a
  PIN), then "Submit Pairing PIN" with that number. The resulting tokens and the
  device id are persisted so the TV stays paired across restarts.

Power note: a VIDAA TV in deep standby drops its MQTT broker, so "Power On" over
the network is best-effort and often won't reach a fully-off set. Power Off and
all other controls work while the TV is on.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid as uuid_mod
from typing import Any

from server.drivers.base import BaseDriver
from server.system_config import get_system_config
from server.utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_PORT = 36669

# --- Credential generation (reverse-engineered from libmqttcrypt.so) --------
# Constants are protocol facts, not copyrightable expression.
_PATTERN = "38D65DC30F45109A369A86FCE866A85B"
_VALUE_SUFFIX_MODERN = "h!i@s#$v%i^d&a*a"   # protocol >= 3290
_VALUE_SUFFIX_LEGACY = "h*i&s%e!r^v0i1c9"   # protocol < 3290
_TIME_XOR = 0x5698_1477_2B03_A968
_BRAND = "his"

# Static credentials accepted by older TVs.
_STATIC_USERNAME = "hisenseservice"
_STATIC_PASSWORD = "multimqttservice"


def _md5_upper(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def generate_credentials(
    device_uuid: str,
    timestamp: int | None = None,
    *,
    brand: str = _BRAND,
    operation: str = "vidaacommon",
    modern: bool = True,
) -> tuple[str, str, str]:
    """Return (client_id, username, password) for a VIDAA TV connection.

    Mirrors the algorithm in the VIDAA app's native crypto library. ``modern``
    selects the value suffix (protocol >= 3290). ``device_uuid`` is a stable
    per-device identifier in MAC format.
    """
    if timestamp is None:
        timestamp = int(time.time())
    race_md5 = _md5_upper(f"{_PATTERN}${device_uuid}")[:6]
    client_id = f"{device_uuid}${brand}${race_md5}_{operation}_001"
    username = f"{brand}${timestamp ^ _TIME_XOR}"
    remainder = sum(int(d) for d in str(timestamp)) % 10
    suffix = _VALUE_SUFFIX_MODERN if modern else _VALUE_SUFFIX_LEGACY
    value_md5 = _md5_upper(f"{brand}{remainder}{suffix}")[:6]
    password = _md5_upper(f"{timestamp}${value_md5}")
    return client_id, username, password


# --- Remote key codes -------------------------------------------------------
KEY_MAP = {
    "power": "KEY_POWER",
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
    "ok": "KEY_OK", "back": "KEY_RETURNS", "menu": "KEY_MENU",
    "home": "KEY_HOME", "exit": "KEY_EXIT",
    "volume_up": "KEY_VOLUMEUP", "volume_down": "KEY_VOLUMEDOWN", "mute": "KEY_MUTE",
    "channel_up": "KEY_CHANNELUP", "channel_down": "KEY_CHANNELDOWN",
    "play": "KEY_PLAY", "pause": "KEY_PAUSE", "stop": "KEY_STOP",
    "fast_forward": "KEY_FORWARDS", "rewind": "KEY_BACK",
    "info": "KEY_INFO", "subtitle": "KEY_SUBTITLE",
    "red": "KEY_RED", "green": "KEY_GREEN", "yellow": "KEY_YELLOW", "blue": "KEY_BLUE",
    "0": "KEY_0", "1": "KEY_1", "2": "KEY_2", "3": "KEY_3", "4": "KEY_4",
    "5": "KEY_5", "6": "KEY_6", "7": "KEY_7", "8": "KEY_8", "9": "KEY_9",
}

# Common source-name -> sourceid fallbacks. The authoritative list comes from
# the TV's own sourcelist (populated into the `sources` state for the picker).
SOURCE_MAP = {
    "tv": "0", "hdmi1": "1", "hdmi2": "2", "hdmi3": "3", "hdmi4": "4", "av": "5",
}

# vidaa_app_connect payload that triggers the PIN dialog on the TV.
_APP_CONNECT = {"app_version": 2, "connect_result": 0, "device_type": "Mobile App"}


class HisenseVidaaDriver(BaseDriver):
    """Hisense VIDAA TV over the embedded MQTT broker (port 36669)."""

    DRIVER_INFO = {
        "id": "hisense_vidaa",
        "name": "Hisense VIDAA TV",
        "manufacturer": "Hisense",
        "category": "display",
        "version": "1.0.2",
        # The connection lifecycle hooks this driver overrides landed in 0.24.0.
        "min_platform_version": "0.24.0",
        "author": "OpenAVC",
        "description": (
            "Controls Hisense VIDAA OS TVs over the TV's embedded MQTT broker "
            "(the path the RemoteNOW/VIDAA app uses). Power, volume, input, key "
            "passthrough, and PIN pairing."
        ),
        "source_url": "https://github.com/tombabolewski/vidaa-control",
        "tags": ["tv", "display", "consumer", "vidaa", "hisense", "mqtt"],
        "verified": False,
        "simulated": True,
        "transport": "mqtt",
        "ports": [36669],  # literal: the catalog extractor requires literal index fields
        "compatible_models": [
            {
                "manufacturer": "Hisense",
                "models": ["VIDAA U6 (incl. 65U6KNZ)", "VIDAA OS TVs (2020+)"],
                "confidence": "untested",
            },
        ],
        "help": {
            "overview": (
                "Hisense VIDAA TVs are controlled over an MQTT broker built into "
                "the TV (port 36669), the same way the RemoteNOW phone app works. "
                "Enter the TV's IP address, then pair: run 'Request Pairing' (a "
                "4-digit PIN appears on the TV), then 'Submit Pairing PIN' with "
                "that number. The pairing is remembered across restarts.\n\n"
                "Power On over the network is best-effort: a TV in deep standby "
                "turns its broker off, so it may be unreachable until switched on "
                "with the physical remote. Power Off and everything else work "
                "while the TV is on."
            ),
            "setup": (
                "1. Make sure the TV and OpenAVC are on the same network.\n"
                "2. On the TV, enable mobile-app/remote control if it has such a "
                "setting (Settings > System > Mobile App / 'RemoteNOW').\n"
                "3. Enter the TV's IP address and save.\n"
                "4. Run 'Request Pairing', read the PIN on the TV, then run "
                "'Submit Pairing PIN'.\n\n"
                "Client certificate (only if pairing/connection fails): some "
                "newer models require a client certificate for the TLS "
                "connection. OpenAVC does not ship it. If the device stays "
                "offline with a TLS error, obtain the VIDAA app client "
                "certificate and key (the community publishes them, e.g. "
                "github.com/tombabolewski/vidaa-control) and paste them into the "
                "'Client Certificate (PEM)' and 'Client Key (PEM)' fields below."
            ),
        },
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "auth_mode": "auto",
            "client_cert": "",
            "client_key": "",
            "poll_interval": 30,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": DEFAULT_PORT, "label": "Port"},
            "auth_mode": {
                "type": "enum", "default": "auto", "label": "Authentication",
                "values": ["auto", "dynamic", "dynamic_legacy", "static"],
                "help": "Leave on 'auto' (tries the modern scheme, then the "
                        "legacy/static ones). Pick a specific scheme only if a "
                        "model needs it.",
            },
            "client_cert": {
                "type": "text", "default": "", "label": "Client Certificate (PEM)",
                "help": "Optional. Only needed for models that require a client "
                        "certificate. Paste the PEM certificate text here.",
            },
            "client_key": {
                "type": "text", "default": "", "label": "Client Key (PEM)",
                "secret": True,
                "help": "Optional. The private key matching the client "
                        "certificate above.",
            },
            "poll_interval": {
                "type": "integer", "default": 30, "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            "power": {"type": "boolean", "label": "Power"},
            "volume": {"type": "integer", "label": "Volume", "min": 0, "max": 100},
            "mute": {"type": "boolean", "label": "Muted"},
            "source": {"type": "string", "label": "Current Source"},
            "paired": {"type": "boolean", "label": "Paired"},
            "pin_pending": {"type": "boolean", "label": "Awaiting PIN"},
            "sources": {
                "type": "string", "label": "Available Sources",
                "help": "JSON list of input names discovered from the TV; "
                        "populates the Set Source picker.",
            },
            "auth_mode_active": {"type": "string", "label": "Auth Scheme In Use"},
        },
        "commands": {
            "power_on": {"label": "Power On", "params": {}},
            "power_off": {"label": "Power Off", "params": {}},
            "power_toggle": {"label": "Power (Toggle)", "params": {}},
            "volume_up": {"label": "Volume Up", "params": {}},
            "volume_down": {"label": "Volume Down", "params": {}},
            "set_volume": {
                "label": "Set Volume",
                "params": {"level": {"type": "integer", "required": True,
                                     "label": "Volume", "min": 0, "max": 100}},
            },
            "mute_toggle": {"label": "Mute (Toggle)", "params": {}},
            "set_source": {
                "label": "Set Source",
                "params": {"source": {
                    "type": "string", "required": True, "label": "Source",
                    "options_state": "sources",
                    "help": "Pick a discovered input, or type a source id."}},
            },
            "channel_up": {"label": "Channel Up", "params": {}},
            "channel_down": {"label": "Channel Down", "params": {}},
            "send_key": {
                "label": "Send Remote Key",
                "params": {"key": {
                    "type": "enum", "required": True, "label": "Key",
                    "values": sorted(KEY_MAP.keys()),
                    "help": "Remote button to send."}},
            },
            "request_pairing": {"label": "Request Pairing (show PIN)", "params": {}},
            "submit_pin": {
                "label": "Submit Pairing PIN",
                "params": {"pin": {"type": "string", "required": True,
                                   "label": "PIN from TV",
                                   "help": "The 4-digit number shown on the TV."}},
            },
            "refresh": {"label": "Refresh State", "params": {}},
        },
    }

    def __init__(self, device_id: str, config: dict[str, Any], *args: Any, **kwargs: Any):
        super().__init__(device_id, config, *args, **kwargs)
        self._client_id = ""
        self._auth_mode = ""
        self._uuid = ""
        self._tokens: dict[str, Any] = {}
        self._cert_path: str | None = None
        self._key_path: str | None = None

    # --- Persistence ----------------------------------------------------

    @property
    def _store_path(self) -> str:
        data_dir = get_system_config().data_dir
        return os.path.join(data_dir, f"hisense_vidaa_{self.device_id}.json")

    def _load_store(self) -> None:
        """Load the persisted device id, tokens, and pairing flag."""
        try:
            with open(self._store_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        self._uuid = data.get("uuid") or self._new_uuid()
        self._tokens = data.get("tokens") or {}
        if data.get("paired"):
            self.set_state("paired", True)
        if not data.get("uuid"):
            self._save_store()  # persist the freshly minted uuid

    def _save_store(self) -> None:
        data = {
            "uuid": self._uuid,
            "tokens": self._tokens,
            "paired": bool(self.get_state("paired")),
        }
        try:
            with open(self._store_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError as e:
            log.warning(f"[{self.device_id}] Could not persist VIDAA store: {e}")

    @staticmethod
    def _new_uuid() -> str:
        """A stable, random MAC-format identifier for this OpenAVC<->TV link."""
        b = uuid_mod.uuid4().bytes[:6]
        return ":".join(f"{x:02x}" for x in b)

    def _write_client_cert(self) -> None:
        """Write pasted PEM cert/key to data-dir files; set their paths."""
        cert = str(self.config.get("client_cert", "")).strip()
        key = str(self.config.get("client_key", "")).strip()
        self._cert_path = self._key_path = None
        if not cert or not key:
            return
        data_dir = get_system_config().data_dir
        cert_path = os.path.join(data_dir, f"hisense_vidaa_{self.device_id}_client.pem")
        key_path = os.path.join(data_dir, f"hisense_vidaa_{self.device_id}_client.key")
        try:
            with open(cert_path, "w", encoding="utf-8") as fh:
                fh.write(cert if cert.endswith("\n") else cert + "\n")
            with open(key_path, "w", encoding="utf-8") as fh:
                fh.write(key if key.endswith("\n") else key + "\n")
            os.chmod(key_path, 0o600)
            self._cert_path, self._key_path = cert_path, key_path
        except OSError as e:
            log.warning(f"[{self.device_id}] Could not write client cert: {e}")

    # --- Connection -----------------------------------------------------

    def _auth_candidates(self) -> list[str]:
        mode = str(self.config.get("auth_mode", "auto")).lower()
        if mode == "auto":
            return ["dynamic", "static", "dynamic_legacy"]
        return [mode]

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ConnectionError(f"[{self.device_id}] No host configured")
        self._load_store()
        self._write_client_cert()

    async def _create_transport(self, transport_type: str) -> None:
        # Wholesale override of the platform ladder: the TV's broker accepts
        # one of several credential schemes and we must retry transport
        # creation per candidate, which per-kwarg adjustment can't express.
        from server.transport.mqtt import MQTTTransport

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", DEFAULT_PORT) or DEFAULT_PORT)

        last_exc: Exception | None = None
        for mode in self._auth_candidates():
            client_id, username, password = self._build_auth(mode)
            try:
                self.transport = await MQTTTransport.create(
                    host, port,
                    client_id=client_id,
                    username=username,
                    password=password,
                    use_tls=True,
                    verify_ssl=False,
                    client_cert=self._cert_path,
                    client_key=self._key_path,
                    protocol_version="3.1.1",
                    on_message=self.on_mqtt_message,
                    on_disconnect=self._handle_transport_disconnect,
                    name=self.device_id,
                )
                self._client_id = client_id
                self._auth_mode = mode
                self.set_state("auth_mode_active", mode)
                log.info(f"[{self.device_id}] VIDAA connected (auth={mode})")
                return
            except ConnectionError as e:
                last_exc = e
                self._stash_transport_error()
                continue
        raise last_exc or ConnectionError(
            f"[{self.device_id}] Could not authenticate to the TV"
        )

    async def _initial_sync(self) -> None:
        await self._subscribe_topics()
        # Already-paired devices skip the PIN; ask the TV to confirm and refresh.
        await self._publish_action("ui_service", "vidaa_app_connect", _APP_CONNECT)
        await self._refresh_state()

    def _build_auth(self, mode: str) -> tuple[str, str, str]:
        if mode == "static":
            # Static scheme still needs a client id; reuse the dynamic format.
            client_id, _, _ = generate_credentials(self._uuid, modern=True)
            return client_id, _STATIC_USERNAME, _STATIC_PASSWORD
        return generate_credentials(self._uuid, modern=(mode != "dynamic_legacy"))

    # --- Topics ---------------------------------------------------------

    def _cmd_topic(self, service: str, action: str) -> str:
        return f"/remoteapp/tv/{service}/{self._client_id}/actions/{action}"

    def _resp_topic(self, service: str, dtype: str) -> str:
        return f"/remoteapp/mobile/{self._client_id}/{service}/data/{dtype}"

    async def _subscribe_topics(self) -> None:
        # Wildcards are denied by the TV — subscribe to specific topics only.
        topics = [
            self._resp_topic("ui_service", "state"),
            self._resp_topic("ui_service", "sourcelist"),
            self._resp_topic("ui_service", "authentication"),
            self._resp_topic("ui_service", "authenticationcodeclose"),
            self._resp_topic("ui_service", "tokenissuance"),
            self._resp_topic("platform_service", "tokenissuance"),
            "/remoteapp/mobile/broadcast/ui_service/state",
            "/remoteapp/mobile/broadcast/platform_service/actions/volumechange",
        ]
        for topic in topics:
            await self.transport.subscribe(topic)

    async def _publish_action(self, service: str, action: str, payload: Any = "") -> None:
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
        await self.transport.publish(self._cmd_topic(service, action), body)

    async def _refresh_state(self) -> None:
        await self._publish_action("ui_service", "gettvstate")
        await self._publish_action("platform_service", "getvolume")
        await self._publish_action("ui_service", "sourcelist")

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            return
        await self._refresh_state()

    # --- Inbound messages ----------------------------------------------

    async def on_mqtt_message(self, topic: str, payload: bytes) -> None:
        try:
            text = payload.decode("utf-8", errors="replace").strip()
        except Exception:
            return
        data: Any = None
        if text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = text

        if topic.endswith("/authentication"):
            self.set_state("pin_pending", True)
        elif topic.endswith("/tokenissuance"):
            self._handle_tokens(data)
        elif "volumechange" in topic or topic.endswith("/volume"):
            self._handle_volume(data)
        elif topic.endswith("/state"):
            self._handle_state(data)
        elif topic.endswith("/sourcelist"):
            self._handle_sourcelist(data)
        elif topic.endswith("/authenticationcodeclose"):
            self.set_state("pin_pending", False)

    def _handle_tokens(self, data: Any) -> None:
        if isinstance(data, dict) and data.get("accesstoken"):
            self._tokens = data
            self.set_state("paired", True)
            self.set_state("pin_pending", False)
            self._save_store()
            log.info(f"[{self.device_id}] VIDAA pairing complete")

    def _handle_volume(self, data: Any) -> None:
        if isinstance(data, dict):
            for field in ("volume_value", "volume", "value"):
                if field in data:
                    try:
                        self.set_state("volume", int(data[field]))
                    except (ValueError, TypeError):
                        pass
                    break
            if "mute" in data:
                self.set_state("mute", bool(data["mute"]))
        elif isinstance(data, str) and data.isdigit():
            self.set_state("volume", int(data))

    def _handle_state(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        st = str(data.get("statetype", "")).lower()
        # statetype "fake_sleep_0"/"sleep" => standby; "sourceswitch"/"app" => on.
        if st:
            self.set_state("power", "sleep" not in st)
        if "sourcename" in data:
            self.set_state("source", str(data["sourcename"]))
        elif "displayname" in data:
            self.set_state("source", str(data["displayname"]))

    def _handle_sourcelist(self, data: Any) -> None:
        names: list[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    name = item.get("sourcename") or item.get("displayname")
                    if name:
                        names.append(str(name))
        if names:
            self.set_state("sources", json.dumps(names))

    # --- Commands -------------------------------------------------------

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        if command == "power_on":
            if not self.get_state("power"):
                await self._send_key("power")
        elif command == "power_off":
            if self.get_state("power") is not False:
                await self._send_key("power")
        elif command == "power_toggle":
            await self._send_key("power")
        elif command == "volume_up":
            await self._send_key("volume_up")
        elif command == "volume_down":
            await self._send_key("volume_down")
        elif command == "mute_toggle":
            await self._send_key("mute")
        elif command == "channel_up":
            await self._send_key("channel_up")
        elif command == "channel_down":
            await self._send_key("channel_down")
        elif command == "set_volume":
            level = max(0, min(100, int(params.get("level", 0))))
            await self._publish_action("platform_service", "changevolume", str(level))
        elif command == "set_source":
            source = str(params.get("source", "")).strip()
            source_id = SOURCE_MAP.get(source.lower(), source)
            await self._publish_action(
                "ui_service", "changesource", {"sourceid": source_id})
        elif command == "send_key":
            key = str(params.get("key", "")).strip().lower()
            if key not in KEY_MAP:
                raise ValueError(f"Unknown key: {key}")
            await self._send_key(key)
        elif command == "request_pairing":
            await self._publish_action("ui_service", "vidaa_app_connect", _APP_CONNECT)
            self.set_state("pin_pending", True)
        elif command == "submit_pin":
            pin = str(params.get("pin", "")).strip()
            await self._publish_action(
                "ui_service", "authenticationcode", {"authNum": pin})
        elif command == "refresh":
            await self._refresh_state()
        else:
            raise ValueError(f"Unknown command: {command}")
        return True

    async def _send_key(self, key: str) -> None:
        await self._publish_action("remote_service", "sendkey", KEY_MAP[key])
