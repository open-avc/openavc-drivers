"""
Roku (ECP) — Simulator.

Emulates a Roku device's External Control Protocol over HTTP on port 8060:

  GET  /query/device-info     -> device + power-mode XML
  GET  /query/active-app      -> foreground app XML
  GET  /query/media-player    -> transport state XML
  GET  /query/apps            -> installed apps XML
  POST /keypress/<KEY>        -> 200 (updates state for Home/Power/Play/launch)
  POST /keydown|/keyup/<KEY>  -> 200
  POST /launch/<appID>        -> 200, sets the active app
  POST /install/<appID>       -> 200

The `mobile_control` toggle mirrors Roku OS 14.1+: when off, every POST returns
HTTP 403, which exercises the driver's "Control by mobile apps disabled"
detection (the `control_enabled` state variable).
"""

import json

from openavc.simulator.http_simulator import HTTPSimulator

# Channel ID -> display name for launch / active-app / apps responses.
_APPS = {
    "12": "Netflix",
    "13": "Prime Video",
    "837": "YouTube",
    "2285": "Hulu",
    "291097": "Disney+",
}


class RokuECPSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "roku_ecp",
        "name": "Roku (ECP) Simulator",
        "category": "streaming",
        "transport": "http",
        "default_port": 8060,
        "initial_state": {
            "power_mode": "PowerOn",
            "active_app_id": "",
            "active_app_name": "Home",
            "media_state": "none",
            "media_position": 0,
            "media_duration": 0,
            "model_name": "Roku Ultra",
            "serial_number": "SIM0123456789",
            "software_version": "13.0.0",
            "device_name": "Roku Simulator",
            "network_type": "ethernet",
            "is_tv": False,
            "supports_tv_power": False,
            "mobile_control": True,
        },
        "delays": {
            "command_response": 0.02,
        },
        "controls": [
            {
                "type": "select",
                "key": "power_mode",
                "label": "Power Mode",
                "options": ["PowerOn", "Ready", "DisplayOff", "PowerOff"],
                "labels": {
                    "PowerOn": "On",
                    "Ready": "Standby (Ready)",
                    "DisplayOff": "Display Off",
                    "PowerOff": "Off",
                },
            },
            {
                "type": "select",
                "key": "media_state",
                "label": "Media State",
                "options": ["none", "play", "pause", "stop"],
            },
            {
                "type": "toggle",
                "key": "mobile_control",
                "label": "Control by mobile apps",
            },
            {
                "type": "toggle",
                "key": "is_tv",
                "label": "Is Roku TV",
            },
            {
                "type": "toggle",
                "key": "supports_tv_power",
                "label": "Supports TV Power",
            },
            {
                "type": "indicator",
                "key": "active_app_name",
                "label": "Active App",
            },
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "Roku stops responding to HTTP requests",
                "behavior": "no_response",
            },
        },
    }

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        """Route an incoming ECP request."""
        clean = path.split("?")[0]
        parts = [p for p in clean.split("/") if p]

        if method in ("GET", "HEAD"):
            if not parts:
                return 200, ""  # root — lets the driver's reachability HEAD pass
            if parts[0] == "query" and len(parts) >= 2:
                return self._handle_query(parts[1])
            return 200, ""

        if method == "POST":
            # Roku OS 14.1+: control endpoints 403 when mobile control is off.
            if not self.state.get("mobile_control", True) and parts and parts[0] in (
                "keypress", "keydown", "keyup", "launch", "install",
            ):
                return 403, "Forbidden"
            if parts and parts[0] == "keypress" and len(parts) >= 2:
                self._handle_key(parts[1])
                return 200, ""
            if parts and parts[0] in ("keydown", "keyup"):
                return 200, ""
            if parts and parts[0] == "launch" and len(parts) >= 2:
                self._handle_launch(parts[1])
                return 200, ""
            if parts and parts[0] == "install":
                return 200, ""
            return 200, ""

        return 404, json.dumps({"error": "Not Found"})

    # ── Query responses ──

    def _handle_query(self, name: str) -> tuple[int, str]:
        if name == "device-info":
            return 200, self._device_info_xml()
        if name == "active-app":
            return 200, self._active_app_xml()
        if name == "media-player":
            return 200, self._media_player_xml()
        if name == "tv-active-channel":
            return 200, self._tv_channel_xml()
        if name == "apps":
            return 200, self._apps_xml()
        return 200, "<status>OK</status>"

    def _tv_channel_xml(self) -> str:
        # Only a Roku TV on the antenna input reports a channel; everything
        # else returns an empty <channel/>.
        if not self.state.get("is_tv"):
            return "<tv-channel>\n  <channel></channel>\n</tv-channel>\n"
        return (
            "<tv-channel>\n"
            "  <channel>\n"
            "    <number>14.3</number>\n"
            "    <name>getTV</name>\n"
            "    <type>air-digital</type>\n"
            "    <program-title>Airwolf</program-title>\n"
            "    <signal-strength>-75</signal-strength>\n"
            "  </channel>\n"
            "</tv-channel>\n"
        )

    def _device_info_xml(self) -> str:
        s = self.state

        def flag(key: str) -> str:
            return "true" if s.get(key) else "false"

        serial = s.get("serial_number", "SIM0123456789")
        return (
            '<?xml version="1.0" encoding="UTF-8" ?>\n'
            "<device-info>\n"
            f"  <udn>{serial}-0000-1046-8035-b0a737000000</udn>\n"
            f"  <serial-number>{serial}</serial-number>\n"
            f"  <device-id>{serial}</device-id>\n"
            f"  <model-name>{s.get('model_name', 'Roku Ultra')}</model-name>\n"
            "  <model-number>4800X</model-number>\n"
            f"  <friendly-device-name>{s.get('device_name', 'Roku')}</friendly-device-name>\n"
            f"  <user-device-name>{s.get('device_name', 'Roku')}</user-device-name>\n"
            f"  <software-version>{s.get('software_version', '13.0.0')}</software-version>\n"
            "  <software-build>4209</software-build>\n"
            f"  <power-mode>{s.get('power_mode', 'PowerOn')}</power-mode>\n"
            f"  <network-type>{s.get('network_type', 'ethernet')}</network-type>\n"
            f"  <supports-tv-power-control>{flag('supports_tv_power')}</supports-tv-power-control>\n"
            f"  <is-tv>{flag('is_tv')}</is-tv>\n"
            "  <is-stick>false</is-stick>\n"
            "  <supports-find-remote>true</supports-find-remote>\n"
            "</device-info>\n"
        )

    def _active_app_xml(self) -> str:
        app_id = self.state.get("active_app_id", "")
        name = self.state.get("active_app_name", "Home")
        if not app_id:
            # Home screen is reported as the app named "Roku" with no id.
            return "<active-app>\n  <app>Roku</app>\n</active-app>\n"
        return (
            "<active-app>\n"
            f'  <app id="{app_id}" type="appl" version="1.0.0">{name}</app>\n'
            "</active-app>\n"
        )

    def _media_player_xml(self) -> str:
        state = self.state.get("media_state", "none")
        if state in ("play", "pause"):
            pos = self.state.get("media_position", 0)
            dur = self.state.get("media_duration", 0)
            return (
                f'<player error="false" state="{state}">\n'
                f"  <position>{pos} ms</position>\n"
                f"  <duration>{dur} ms</duration>\n"
                "</player>\n"
            )
        return f'<player error="false" state="{state}"></player>\n'

    def _apps_xml(self) -> str:
        rows = "".join(
            f'  <app id="{app_id}" type="appl" version="1.0.0">{name}</app>\n'
            for app_id, name in _APPS.items()
        )
        return f"<apps>\n{rows}</apps>\n"

    # ── State transitions ──

    def _handle_key(self, key: str) -> None:
        if key == "Home":
            self.set_state("active_app_id", "")
            self.set_state("active_app_name", "Home")
            self.set_state("media_state", "none")
            self.set_state("media_position", 0)
            self.set_state("media_duration", 0)
        elif key == "PowerOff":
            self.set_state("power_mode", "PowerOff")
        elif key == "PowerOn":
            self.set_state("power_mode", "PowerOn")
        elif key == "Power":
            current = self.state.get("power_mode", "PowerOn")
            self.set_state(
                "power_mode", "PowerOff" if current == "PowerOn" else "PowerOn"
            )
        elif key == "Play":
            state = self.state.get("media_state", "none")
            if state == "play":
                self.set_state("media_state", "pause")
            elif state == "pause":
                self.set_state("media_state", "play")
        # All other keys (navigation, transport skip, volume, Lit_*) are no-ops
        # in the simulator — they don't change queryable state on a real Roku.

    def _handle_launch(self, app_id: str) -> None:
        self.set_state("active_app_id", app_id)
        self.set_state("active_app_name", _APPS.get(app_id, f"App {app_id}"))
        self.set_state("media_state", "play")
        self.set_state("media_position", 0)
        self.set_state("media_duration", 5400000)
