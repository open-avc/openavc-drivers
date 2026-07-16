"""
OpenAVC Crestron DM NVX Driver (AV-over-IP encoder / decoder).

Controls Crestron DM NVX endpoints over the CresNext REST API (HTTPS/JSON).
One role-adaptive driver covers the whole line: it reads ``DeviceMode`` on
connect and presents the encoder surface (DM-NVX-E20/E30/E760... — HDMI-in
capture, transmit stream + bitrate, RTSP preview) or the decoder surface
(DM-NVX-D20/D200/D30... — stream routing, scaler output, video wall). Combo
units (DM-NVX-350/360/384) that flip role via ``DeviceMode`` are covered by
the same card.

Why Python (not YAML): the driver is Python for several reasons no single
`.avcdriver` feature covers —
  * Role-adaptive surface: the command/state set is chosen at runtime from the
    device's reported ``DeviceMode`` (a transmitter has no StreamReceive; a
    receiver has no StreamTransmit).
  * Cookie-session auth: GET /userlogin.html for a TRACKID, then a
    form-urlencoded ``login=&passwd=`` POST; the session rides six HTTPOnly
    cookies (no `auth.type` fits — this is the `post_login_cookie` shape).
  * Nested-JSON fan-out: one GET of AudioVideoInputOutput / StreamTransmit /
    StreamReceive populates ~10 state keys out of nested arrays.
  * First-boot provisioning: modern firmware ships with NO login and forces a
    "create admin" step; the driver detects that and offers a setup wizard.
  * Every write is a partial nested POST to a single ``/Device`` endpoint.

Verified end-to-end on the bench 2026-07-15 against a live DM-NVX-E20
(encoder) + DM-NVX-D200 (decoder) on firmware 7.1.5259, including a real
E20 -> D200 multicast route.

Protocol doc (source of truth): https://sdkcon78221.crestron.com/sdk/DM_NVX_REST_API/
Product manual: DM-NVX-E20/D20/D200 series.
Authentication: cookie session (admin account mandatory on current firmware).
Transport: HTTPS REST (JSON), port 443.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from server.drivers.base import BaseDriver, ConnectionFaultError
from server.utils.logger import get_logger

log = get_logger(__name__)


# Video / audio source enums come straight from the DeviceSpecific object's
# documented allowed values (confirmed on hardware).
_VIDEO_SOURCES = ["None", "Input1", "Input2", "Stream"]
_AUDIO_SOURCES = [
    "AudioFollowsVideo", "Input1", "Input2",
    "AnalogAudio", "PrimaryStreamAudio", "SecondaryStreamAudio",
]


class CrestronNVXDriver(BaseDriver):
    """Crestron DM NVX AV-over-IP encoder/decoder (role-adaptive)."""

    # Typed-fault path + value hints need 0.22.0.
    DRIVER_INFO = {
        "id": "crestron_nvx",
        "name": "Crestron DM NVX",
        "manufacturer": "Crestron",
        "category": "switcher",
        "version": "2.0.0",
        "author": "OpenAVC",
        "min_platform_version": "0.22.0",
        "description": (
            "Crestron DM NVX AV-over-IP encoders and decoders over the CresNext "
            "REST API. One driver adapts to the endpoint's role: transmit stream "
            "+ bitrate + HDMI-in monitoring on encoders, stream routing + scaler "
            "output + video wall on decoders. Covers the E20/D20/D200, E30/D30, "
            "and combo 35x/36x lines."
        ),
        "source_url": "https://sdkcon78221.crestron.com/sdk/DM_NVX_REST_API/",
        "tags": ["av-over-ip", "encoder", "decoder", "rest-api", "streaming"],
        "verified": False,
        "simulated": True,
        "web_ui": True,          # exposes an HTTPS web interface (Open Web UI action)
        "protocols": ["cresnext"],
        "ports": [443],
        "discovery": {
            # NVX endpoints are identified pre-auth on 443: the TLS web server
            # answers with `Server: Crestron Webserver` and (fresh units) a
            # redirect to the first-boot Create-User page. That plus the
            # Crestron OUI is a strong signal — no credentials required, and it
            # does NOT depend on the CIP probe (NVX video endpoints don't answer
            # UDP 41794 / TCP 1688; verified on the E20 + D200).
            "tcp_probe": {
                "port": 443,
                "tls": True,
                "send_ascii": (
                    "GET / HTTP/1.1\r\nHost: openavc\r\n"
                    "Connection: close\r\n\r\n"
                ),
                "expect_regex": "Crestron Webserver",
                "timeout_ms": 2500,
            },
            # OUIs seen on modern NVX gear (c4:42:68 is the bench E20/D200 block).
            "oui": ["c4:42:68", "00:10:7f", "00:0e:80", "00:1f:5d"],
            "hostname": ["^DM-NVX-"],
            "manufacturer_alias": ["crestron", "crestron electronics"],
        },
        "compatible_models": [
            {
                "manufacturer": "Crestron",
                "models": [
                    "DM-NVX-E20", "DM-NVX-E20-2G", "DM-NVX-D20", "DM-NVX-D200",
                    "DM-NVX-E30", "DM-NVX-D30", "DM-NVX-350", "DM-NVX-360",
                    "DM-NVX-384", "DM NVX series (encoders and decoders)",
                ],
                "confidence": "full",
                "notes": (
                    "Verified on DM-NVX-E20 (encoder) + DM-NVX-D200 (decoder), "
                    "firmware 7.1.5259, 2026-07-15. The E20 is transmit-only and "
                    "the D200 receive-only; combo 35x/36x units flip via "
                    "DeviceMode and expose whichever surface is active. Current "
                    "firmware requires an admin account (use the Set Up NVX "
                    "wizard on a factory-fresh unit)."
                ),
            },
        ],
        "transport": "http",
        "default_config": {
            "host": "",
            "port": 443,
            "verify_ssl": False,
            "username": "admin",
            "password": "",
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "default": 443, "label": "Port"},
            "verify_ssl": {
                "type": "boolean",
                "default": False,
                "label": "Verify TLS Certificate",
                "description": "NVX endpoints ship a self-signed certificate; leave off unless you've installed a trusted one.",
            },
            "username": {
                "type": "string",
                "default": "admin",
                "label": "Username",
                "description": "Admin account created on the NVX (see the Set Up NVX wizard for a factory-fresh unit).",
            },
            "password": {
                "type": "string",
                "default": "",
                "label": "Password",
                "secret": True,
            },
            "poll_interval": {
                "type": "integer",
                "default": 10,
                "min": 0,
                "label": "Poll Interval (sec)",
            },
        },
        "state_variables": {
            # ── Common ──
            "device_mode": {
                "type": "enum",
                "values": ["Transmitter", "Receiver"],
                "label": "Device Mode",
            },
            "device_ready": {"type": "boolean", "label": "Device Ready"},
            "model": {"type": "string", "label": "Model"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "firmware": {"type": "string", "label": "Firmware Version"},
            "device_name": {"type": "string", "label": "Device Name"},
            "video_source": {"type": "enum", "values": _VIDEO_SOURCES, "label": "Video Source", "control": True},
            "audio_source": {"type": "enum", "values": _AUDIO_SOURCES, "label": "Audio Source", "control": True},
            "active_video_source": {"type": "string", "label": "Active Video Source"},
            "active_audio_source": {"type": "string", "label": "Active Audio Source"},
            "leds_enabled": {"type": "boolean", "label": "Front Panel LEDs"},
            "front_panel_locked": {"type": "boolean", "label": "Front Panel Locked"},
            # ── Stream (both roles; the primary stream) ──
            "stream_status": {"type": "string", "label": "Stream Status"},
            "stream_multicast": {"type": "string", "label": "Stream Multicast Address"},
            "stream_location": {"type": "string", "label": "Stream URL"},
            # ── Encoder-only ──
            "input_sync": {"type": "boolean", "label": "Input Sync Detected"},
            "input_resolution": {"type": "string", "label": "Input Resolution"},
            "input_hdcp": {"type": "string", "label": "Input HDCP State"},
            "bitrate": {"type": "integer", "label": "Bitrate", "unit": "Mbps", "min": 100, "max": 950, "control": True},
            "active_bitrate": {"type": "integer", "label": "Active Bitrate", "unit": "Mbps"},
            "bitrate_mode": {"type": "enum", "values": ["Fixed", "Adaptive"], "label": "Bitrate Mode"},
            "preview_url": {"type": "string", "label": "Preview URL"},
            "preview_format": {"type": "string", "label": "Preview Format"},
            # ── Decoder-only ──
            "output_connected": {"type": "boolean", "label": "Display Connected"},
            "output_resolution": {"type": "string", "label": "Output Resolution"},
            "output_hdcp": {"type": "string", "label": "Output HDCP State"},
            "scaler_resolution": {"type": "string", "label": "Scaler Output Resolution"},
            "video_wall_mode": {"type": "boolean", "label": "Video Wall Mode"},
            "rx_initiator": {"type": "string", "label": "Stream Source Address"},
        },
        "device_settings": {
            "leds": {
                "type": "boolean",
                "label": "Front Panel LEDs",
                "help": "Enable or disable the front-panel LED indicators.",
                "state_key": "leds_enabled",
                "default": True,
                "setup": False,
            },
            "front_panel_lock": {
                "type": "boolean",
                "label": "Front Panel Lockout",
                "help": "Lock the front-panel buttons so on-device changes can't override control.",
                "state_key": "front_panel_locked",
                "default": False,
                "setup": False,
            },
        },
        "commands": {
            "set_video_source": {
                "label": "Set Video Source",
                "params": {
                    "source": {"type": "enum", "values": _VIDEO_SOURCES, "required": True, "label": "Video Source"},
                },
            },
            "set_audio_source": {
                "label": "Set Audio Source",
                "params": {
                    "source": {"type": "enum", "values": _AUDIO_SOURCES, "required": True, "label": "Audio Source"},
                },
            },
            # ── Encoder ──
            "set_transmit_multicast": {
                "label": "Set Transmit Multicast",
                "params": {
                    "address": {
                        "type": "string", "required": True, "label": "Multicast Address",
                        "pattern": r"^(22[4-9]|23[0-9])(\.\d{1,3}){3}$",
                        "help": "IPv4 multicast group the encoder transmits on (224.0.0.0-239.255.255.255).",
                    },
                },
            },
            "set_bitrate": {
                "label": "Set Bitrate",
                "params": {
                    "mbps": {"type": "integer", "required": True, "label": "Bitrate (Mbps)", "min": 100, "max": 950},
                    "mode": {"type": "enum", "values": ["Fixed", "Adaptive"], "required": False, "label": "Mode", "default": "Fixed"},
                },
            },
            # ── Decoder ──
            "route_stream": {
                "label": "Route Stream",
                "params": {
                    "encoder": {
                        "type": "string", "required": True, "label": "Encoder (IP or stream URL)",
                        "help": (
                            "The source encoder to display on this decoder: its IP "
                            "address (e.g. 192.168.1.50) or a full rtsp:// stream URL."
                        ),
                    },
                },
            },
            "set_scaler_resolution": {
                "label": "Set Output Resolution",
                "params": {
                    "resolution": {
                        "type": "string", "required": True, "label": "Resolution",
                        "help": "Scaler output resolution, e.g. Auto, 1920x1080@60, 3840x2160@30.",
                    },
                },
            },
            # ── Both roles (routed by DeviceMode) ──
            "start_stream": {"label": "Start Stream", "params": {}},
            "stop_stream": {"label": "Stop Stream", "params": {}},
            "reboot": {"label": "Reboot Device", "params": {}},
        },
        "actions": [
            # Routing is the decoder operator's headline action.
            {
                "id": "route_stream", "kind": "command", "icon": "route",
                "visible_when": {"key": "device.$id.device_mode", "operator": "eq", "value": "Receiver"},
            },
            {
                "id": "set_video_source", "kind": "command", "icon": "monitor",
            },
            {
                "id": "set_bitrate", "kind": "command", "icon": "gauge",
                "visible_when": {"key": "device.$id.device_mode", "operator": "eq", "value": "Transmitter"},
            },
            {
                "id": "stop_stream", "kind": "command", "icon": "square",
            },
            {
                "id": "reboot", "kind": "command", "icon": "power",
                "confirm": "Reboot the NVX? It drops offline until it restarts.",
            },
            # Note: the "Open Web UI" button is added automatically by the
            # platform from `web_ui: True` above (opens https://<host>), so it's
            # not declared here.
            # First-boot provisioning: create the admin account a factory-fresh
            # NVX requires before OpenAVC can connect. Shows while offline.
            {
                "id": "set_up_nvx", "kind": "setup", "icon": "user-plus",
                "label": "Set Up NVX",
                "availability": "offline",
                "params": {
                    "new_username": {"type": "string", "required": True, "label": "Admin Username", "default": "admin"},
                    "new_password": {"type": "string", "required": True, "label": "Admin Password", "secret": True,
                                     "help": "Crestron requires a strong password (length, mixed case, number, symbol)."},
                },
            },
        ],
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client: httpx.AsyncClient | None = None
        self._base_url: str = ""
        self._mode: str = ""  # "Transmitter" | "Receiver"

    # ── Connection lifecycle ────────────────────────────────────────────────

    async def connect(self) -> None:
        host = self.config.get("host", "")
        port = self.config.get("port", 443)
        verify_ssl = self.config.get("verify_ssl", False)
        self._base_url = f"https://{host}:{port}"

        if not await self._verify_reachable(host, port, timeout=4.0):
            raise ConnectionError(f"NVX at {host}:{port} not responding")

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            verify=verify_ssl,
            timeout=10.0,
            follow_redirects=True,
            headers={"Referer": self._base_url},
        )

        try:
            await self._authenticate()

            info = await self._api_get("/Device/DeviceInfo")
            if not info or "DeviceInfo" not in info.get("Device", {}):
                raise ConnectionFaultError(
                    "Login rejected — check the NVX username and password.",
                    code="auth_failed",
                )
            self._parse_device_info(info)

            spec = await self._api_get("/Device/DeviceSpecific")
            if spec:
                self._parse_device_specific(spec)
            self._mode = self.get_state("device_mode") or "Receiver"

            self._connected = True
            self.set_state("connected", True)
            await self.events.emit(f"device.connected.{self.device_id}")
            log.info(
                "[%s] Connected to NVX %s at %s:%s (%s)",
                self.device_id, self.get_state("model"), host, port, self._mode,
            )

            # Prime the role-specific state before the first poll interval.
            await self._refresh_role_state()

            poll_interval = self.config.get("poll_interval", 10)
            if poll_interval > 0:
                await self.start_polling(poll_interval)
        except ConnectionFaultError:
            await self._close_client()
            raise
        except (httpx.RequestError, ConnectionError) as e:
            await self._close_client()
            raise ConnectionError(f"Failed to connect: {e}") from e

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self._client:
            try:
                await self._client.get("/logout")
            except Exception:
                pass
        await self._close_client()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")
        log.info("[%s] Disconnected", self.device_id)

    async def _close_client(self) -> None:
        if self._client:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def _authenticate(self) -> None:
        """Cookie-session login. Detects the factory 'create admin' state."""
        username = self.config.get("username", "admin")
        password = self.config.get("password", "")

        # A factory-fresh unit redirects everything to the Create-User page.
        r = await self._client.get("/userlogin.html")
        if "createuser" in str(r.url).lower():
            raise ConnectionFaultError(
                "This NVX has no admin account yet. Run the 'Set Up NVX' "
                "action to create one, then connect.",
                code="auth_failed",
            )

        # Form-urlencoded login; the httpx cookie jar carries the six session
        # cookies (TRACKID, userstr, userid, iv, tag, AuthByPasswd) afterward.
        await self._client.post(
            "/userlogin.html",
            content=f"login={username}&passwd={password}",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self._base_url,
                "Referer": f"{self._base_url}/userlogin.html",
            },
        )

    # ── Commands ────────────────────────────────────────────────────────────

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        match command:
            case "set_video_source":
                await self._set_device({"VideoSource": params.get("source", "None")})
            case "set_audio_source":
                await self._set_device({"AudioSource": params.get("source", "AudioFollowsVideo")})

            case "set_transmit_multicast":
                await self._set_stream("StreamTransmit", {"MulticastAddress": params["address"]})
            case "set_bitrate":
                fields: dict[str, Any] = {
                    "Bitrate": int(params["mbps"]),
                    "BitrateMode": params.get("mode", "Fixed"),
                }
                await self._set_stream("StreamTransmit", fields)

            case "route_stream":
                await self._route_receive(params["encoder"])
            case "set_scaler_resolution":
                await self._set_output({"Resolution": params["resolution"]})

            case "start_stream":
                await self._set_stream(self._stream_object(), {"Start": True})
            case "stop_stream":
                await self._set_stream(self._stream_object(), {"Stop": True})

            case "reboot":
                await self._post_device({"DeviceOperations": {"Reboot": True}})

            case _:
                log.warning("[%s] Unknown command: %s", self.device_id, command)

        log.debug("[%s] Sent command: %s %s", self.device_id, command, params)

    async def set_device_setting(self, key: str, value: Any) -> Any:
        if not self._client:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        match key:
            case "leds":
                await self._set_device({"LedsEnabled": bool(value)})
                self.set_state("leds_enabled", bool(value))
            case "front_panel_lock":
                await self._set_device({"IsFrontPanelLockoutEnabled": bool(value)})
                self.set_state("front_panel_locked", bool(value))
            case _:
                raise ValueError(f"Unknown device setting: {key}")

    def _stream_object(self) -> str:
        return "StreamTransmit" if self._mode == "Transmitter" else "StreamReceive"

    async def _set_device(self, fields: dict[str, Any]) -> None:
        await self._post_device({"DeviceSpecific": fields})

    async def _set_stream(self, obj: str, fields: dict[str, Any]) -> None:
        await self._post_device({obj: {"Streams": [fields]}})

    async def _set_output(self, port_fields: dict[str, Any]) -> None:
        await self._post_device(
            {"AudioVideoInputOutput": {"Outputs": [{"Ports": [port_fields]}]}}
        )

    async def _route_receive(self, encoder: str) -> None:
        """Point a decoder at an encoder's stream by its RTSP URL.

        The decoder's SessionInitiation is "Multicast via RTSP" (the NVX
        default): the RTSP URL (StreamLocation) is the control field — the
        device derives the multicast group from it. Setting StreamLocation +
        Start re-routes in one shot, no stop/start dance required. ``encoder``
        is an IP/hostname (we build the standard rtsp://<host>:554/live.sdp) or
        a full rtsp:// URL.
        """
        enc = encoder.strip()
        url = enc if enc.lower().startswith("rtsp://") else f"rtsp://{enc}:554/live.sdp"
        await self._post_device({"StreamReceive": {"Streams": [
            {"StreamLocation": url, "Start": True}
        ]}})
        await self._set_device({"VideoSource": "Stream"})

    # ── Setup wizard (first-boot admin creation) ───────────────────────────

    async def run_setup_action(self, action_id: str, params: dict[str, Any], progress) -> Any:
        if action_id != "set_up_nvx":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = self.config.get("host", "")
        port = self.config.get("port", 443)
        verify_ssl = self.config.get("verify_ssl", False)
        base = f"https://{host}:{port}"
        new_user = params["new_username"]
        new_pass = params["new_password"]

        await progress("Contacting the NVX…", 10)
        async with httpx.AsyncClient(
            base_url=base, verify=verify_ssl, timeout=15.0,
            follow_redirects=True, headers={"Referer": base},
        ) as client:
            # A factory-fresh unit redirects /userlogin.html (and /Device/*) to
            # the Create-User page; the root '/' returns 200 either way, so probe
            # a redirecting path.
            landing = await client.get("/userlogin.html")
            if "createuser" not in str(landing.url).lower():
                raise RuntimeError(
                    "This NVX already has an admin account — no setup needed. "
                    "Enter the existing credentials in the device settings."
                )

            await progress("Creating the admin account…", 50)
            body = {"Device": {"Authentication": {"AuthenticationState": {
                "AdminUsername": new_user,
                "AdminPassword": new_pass,
                "IsEnabled": True,
            }}}}
            resp = await client.post(
                "/Device/Authentication", json=body,
                headers={"Content-Type": "application/json"},
            )
            self._raise_on_action_error(resp, "Create admin account")

        await progress("Saving credentials…", 80)
        await self.request_config_update({"username": new_user, "password": new_pass})
        await progress("Connecting to the NVX…", 90)
        await self.request_reconnect()
        return {"provisioned": True, "username": new_user}

    # ── Polling ────────────────────────────────────────────────────────────

    async def poll(self) -> None:
        if not self._client:
            return
        try:
            spec = await self._api_get("/Device/DeviceSpecific")
            if spec:
                self._parse_device_specific(spec)
            await asyncio.sleep(0.15)
            await self._refresh_role_state()
        except ConnectionError:
            raise  # dead link -> watchdog flips offline
        except Exception:
            log.exception("[%s] Poll error", self.device_id)

    async def _refresh_role_state(self) -> None:
        """Query the role-specific objects (encoder vs decoder)."""
        avio = await self._api_get("/Device/AudioVideoInputOutput")
        if avio:
            self._parse_av_io(avio)
        await asyncio.sleep(0.15)
        if self._mode == "Transmitter":
            tx = await self._api_get("/Device/StreamTransmit")
            if tx:
                self._parse_stream_transmit(tx)
        else:
            rx = await self._api_get("/Device/StreamReceive")
            if rx:
                self._parse_stream_receive(rx)

    # ── Response parsing ────────────────────────────────────────────────────

    def _parse_device_info(self, data: dict) -> None:
        di = data.get("Device", {}).get("DeviceInfo", {})
        if not di:
            return
        self.set_states({
            "model": di.get("Model", ""),
            "serial_number": di.get("SerialNumber", ""),
            "firmware": di.get("PufVersion", di.get("DeviceVersion", "")),
            "device_name": di.get("Name", ""),
        })

    def _parse_device_specific(self, data: dict) -> None:
        ds = data.get("Device", {}).get("DeviceSpecific", {})
        if not ds:
            return
        updates: dict[str, Any] = {}
        if "DeviceMode" in ds:
            updates["device_mode"] = ds["DeviceMode"]
            self._mode = ds["DeviceMode"]
        if "DeviceReady" in ds:
            updates["device_ready"] = bool(ds["DeviceReady"])
        if "VideoSource" in ds:
            updates["video_source"] = ds["VideoSource"]
        if "AudioSource" in ds:
            updates["audio_source"] = ds["AudioSource"]
        if "ActiveVideoSource" in ds:
            updates["active_video_source"] = ds["ActiveVideoSource"]
        if "ActiveAudioSource" in ds:
            updates["active_audio_source"] = ds["ActiveAudioSource"]
        if "LedsEnabled" in ds:
            updates["leds_enabled"] = bool(ds["LedsEnabled"])
        if "IsFrontPanelLockoutEnabled" in ds:
            updates["front_panel_locked"] = bool(ds["IsFrontPanelLockoutEnabled"])
        if "VideoWallMode" in ds:
            updates["video_wall_mode"] = bool(ds["VideoWallMode"])
        if updates:
            self.set_states(updates)

    def _parse_av_io(self, data: dict) -> None:
        avio = data.get("Device", {}).get("AudioVideoInputOutput", {})
        if not avio:
            return
        if self._mode == "Transmitter":
            port = self._first_port(avio.get("Inputs", []))
            if port is None:
                return
            self.set_states({
                "input_sync": bool(port.get("IsSyncDetected", False)),
                "input_resolution": self._resolution(port),
                "input_hdcp": port.get("Hdmi", {}).get("HdcpState", ""),
            })
        else:
            port = self._first_port(avio.get("Outputs", []))
            if port is None:
                return
            self.set_states({
                "output_connected": bool(port.get("IsSinkConnected", False)),
                "output_resolution": self._resolution(port),
                "output_hdcp": port.get("Hdmi", {}).get("HdcpState", ""),
                "scaler_resolution": port.get("Resolution", ""),
            })

    def _parse_stream_transmit(self, data: dict) -> None:
        stream = self._first_stream(data, "StreamTransmit")
        if stream is None:
            return
        loc = stream.get("StreamLocation", "")
        self.set_states({
            "stream_status": stream.get("Status", ""),
            "stream_multicast": stream.get("MulticastAddress", ""),
            "stream_location": loc,
            "bitrate": stream.get("Bitrate", 0),
            "active_bitrate": stream.get("ActiveBitrate", 0),
            "bitrate_mode": stream.get("BitrateMode", ""),
            # Preview convention: the encoder's own RTSP stream is server-reachable.
            "preview_url": loc if loc.startswith("rtsp://") else "",
            "preview_format": "rtsp" if loc.startswith("rtsp://") else "",
        })

    def _parse_stream_receive(self, data: dict) -> None:
        stream = self._first_stream(data, "StreamReceive")
        if stream is None:
            return
        self.set_states({
            "stream_status": stream.get("Status", ""),
            "stream_multicast": stream.get("MulticastAddress", ""),
            "stream_location": stream.get("StreamLocation", ""),
            "rx_initiator": stream.get("InitiatorAddress", ""),
        })

    # ── Small helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _first_port(collection: list) -> dict | None:
        if not collection:
            return None
        ports = collection[0].get("Ports", [])
        return ports[0] if ports else None

    @staticmethod
    def _first_stream(data: dict, obj: str) -> dict | None:
        streams = data.get("Device", {}).get(obj, {})
        if not isinstance(streams, dict):
            return None  # "UNSUPPORTED PROPERTY" comes back as a string for the wrong role
        arr = streams.get("Streams", [])
        return arr[0] if arr else None

    @staticmethod
    def _resolution(port: dict) -> str:
        h = port.get("HorizontalResolution", 0)
        v = port.get("VerticalResolution", 0)
        if not h or not v:
            return "No Signal"
        fps = port.get("FramesPerSecond", 0)
        return f"{h}x{v}@{fps}" if fps else f"{h}x{v}"

    # ── HTTP helpers ─────────────────────────────────────────────────────────

    async def _api_get(self, path: str) -> dict | None:
        """GET + parse JSON. Transport failures raise ConnectionError (poll
        propagates them to the watchdog); HTTP errors return None."""
        try:
            resp = await self._client.get(path)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Session expired — re-auth once and retry.
                try:
                    await self._authenticate()
                    resp = await self._client.get(path)
                    resp.raise_for_status()
                    return resp.json()
                except Exception:
                    log.warning("[%s] Re-auth failed on GET %s", self.device_id, path)
            else:
                log.warning("[%s] GET %s -> HTTP %s", self.device_id, path, e.response.status_code)
            return None
        except httpx.TransportError as e:
            raise ConnectionError(f"NVX at {self._base_url} not responding: {e}") from e
        except Exception as e:
            log.warning("[%s] GET %s error: %s", self.device_id, path, e)
            return None

    async def _post_device(self, obj: dict) -> list[dict]:
        """POST a partial CresNext object to /Device and raise if the device
        rejected any field (negative StatusId)."""
        results = await self._post_device_results(obj)
        for r in results:
            if r.get("StatusId", 0) < 0:
                raise RuntimeError(
                    f"{r.get('Property', next(iter(obj)))} rejected: "
                    f"{r.get('StatusInfo', 'unknown error')}"
                )
        return results

    async def _post_device_results(self, obj: dict) -> list[dict]:
        """POST to /Device; return the per-property Results list (no raise).
        Re-auths once on a 401. Every write goes to /Device."""
        body = {"Device": obj}
        headers = {"Content-Type": "application/json"}
        try:
            resp = await self._client.post("/Device", json=body, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                await self._authenticate()
                resp = await self._client.post("/Device", json=body, headers=headers)
                resp.raise_for_status()
            else:
                log.warning("[%s] POST /Device -> HTTP %s", self.device_id, e.response.status_code)
                return []
        try:
            actions = resp.json().get("Actions", [])
        except Exception:
            return []
        return [r for a in actions for r in a.get("Results", [])]

    def _raise_on_action_error(self, resp: httpx.Response, what: str) -> None:
        """CresNext write results carry a StatusId; negative means rejected.
        Used by the setup wizard where the POST is made with a bare client."""
        try:
            actions = resp.json().get("Actions", [])
        except Exception:
            return
        for action in actions:
            for result in action.get("Results", []):
                if result.get("StatusId", 0) < 0:
                    raise RuntimeError(
                        f"{what} rejected: {result.get('StatusInfo', 'unknown error')}"
                    )
