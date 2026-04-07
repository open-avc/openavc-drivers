"""
Sony Bravia Display — Simulator

Full-featured Sony Bravia JSON-RPC API simulator with:
  - Power control (setPowerStatus / getPowerStatus)
  - Volume control (setAudioVolume / getVolumeInformation / setAudioMute)
  - Input switching (setPlayContent / getPlayingContentInfo)
  - System info query (getSystemInformation)
  - IRCC SOAP remote control endpoint (accepts all codes)
  - PSK authentication check (X-Auth-PSK header)
  - Controls schema for Simulator UI

Protocol: HTTP JSON-RPC on port 80.
  Endpoints: /sony/system, /sony/audio, /sony/avContent, /sony/appControl, /sony/IRCC
  Request:  {"method": "<name>", "params": [...], "id": N, "version": "1.0"}
  Response: {"result": [...], "id": N}  or  {"error": [code, msg], "id": N}
"""

import json

from simulator.http_simulator import HTTPSimulator

# Map of input URIs to friendly display titles
_INPUT_TITLES = {
    "extInput:hdmi?port=1": "HDMI 1",
    "extInput:hdmi?port=2": "HDMI 2",
    "extInput:hdmi?port=3": "HDMI 3",
    "extInput:hdmi?port=4": "HDMI 4",
    "extInput:composite?port=1": "Composite",
    "extInput:component?port=1": "Component",
}

# Reverse map for source field
_INPUT_SOURCES = {
    "extInput:hdmi?port=1": "extInput:hdmi",
    "extInput:hdmi?port=2": "extInput:hdmi",
    "extInput:hdmi?port=3": "extInput:hdmi",
    "extInput:hdmi?port=4": "extInput:hdmi",
    "extInput:composite?port=1": "extInput:composite",
    "extInput:component?port=1": "extInput:component",
}


class SonyBraviaSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "sony_bravia",
        "name": "Sony Bravia Display Simulator",
        "category": "display",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "power": "off",
            "input": "extInput:hdmi?port=1",
            "volume": 25,
            "mute": False,
            "app": "",
            "model": "XBR-65X950G-SIM",
        },
        "delays": {
            "command_response": 0.05,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Display stops responding to HTTP requests",
                "behavior": "no_response",
            },
            "standby_mode": {
                "description": "Display enters standby (power off)",
                "set_state": {"power": "off"},
            },
        },
        "controls": [
            {
                "type": "select",
                "state_key": "power",
                "label": "Power",
                "options": [
                    {"label": "On", "value": "active"},
                    {"label": "Off", "value": "off"},
                ],
            },
            {
                "type": "select",
                "state_key": "input",
                "label": "Input",
                "options": [
                    {"label": "HDMI 1", "value": "extInput:hdmi?port=1"},
                    {"label": "HDMI 2", "value": "extInput:hdmi?port=2"},
                    {"label": "HDMI 3", "value": "extInput:hdmi?port=3"},
                    {"label": "HDMI 4", "value": "extInput:hdmi?port=4"},
                    {"label": "Component", "value": "extInput:component?port=1"},
                    {"label": "Composite", "value": "extInput:composite?port=1"},
                ],
            },
            {
                "type": "slider",
                "state_key": "volume",
                "label": "Volume",
                "min": 0,
                "max": 100,
            },
            {
                "type": "toggle",
                "state_key": "mute",
                "label": "Mute",
            },
        ],
    }

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        """Route incoming HTTP requests to the appropriate handler."""

        # Strip query string from path for routing
        clean_path = path.split("?")[0]

        # PSK authentication: if the driver sends X-Auth-PSK, just verify
        # the header exists (don't validate the actual key in simulation)
        # Real TVs accept any non-empty PSK that matches the configured value.

        # IRCC endpoint (SOAP XML) — accept anything and return 200
        if clean_path == "/sony/IRCC" and method == "POST":
            return self._handle_ircc(headers, body)

        # JSON-RPC endpoints
        if method == "POST" and clean_path.startswith("/sony/"):
            return self._handle_jsonrpc(clean_path, body)

        return 404, {"error": "Not Found"}

    # ── IRCC (Remote Control) ──

    def _handle_ircc(self, headers: dict[str, str], body: str) -> tuple[int, str]:
        """
        Handle IRCC SOAP requests for remote button emulation.
        The driver sends SOAP XML with an IRCCCode element.
        Just acknowledge all codes.
        """
        # Return a valid SOAP response envelope
        soap_response = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            '<u:X_SendIRCCResponse xmlns:u="urn:schemas-sony-com:service:IRCC:1">'
            "</u:X_SendIRCCResponse>"
            "</s:Body>"
            "</s:Envelope>"
        )
        return 200, soap_response

    # ── JSON-RPC Dispatcher ──

    def _handle_jsonrpc(self, path: str, body: str) -> tuple[int, dict]:
        """Parse JSON-RPC request and dispatch to the right service handler."""
        try:
            req = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return 400, {"error": [1, "Invalid JSON"]}

        rpc_method = req.get("method", "")
        params = req.get("params", [])
        req_id = req.get("id", 1)

        # Route by service path
        service = path.rsplit("/", 1)[-1]  # system, audio, avContent, appControl

        if service == "system":
            return self._handle_system(rpc_method, params, req_id)
        elif service == "audio":
            return self._handle_audio(rpc_method, params, req_id)
        elif service == "avContent":
            return self._handle_av_content(rpc_method, params, req_id)
        elif service == "appControl":
            return self._handle_app_control(rpc_method, params, req_id)

        return 404, {"error": [40400, f"Service not found: {service}"], "id": req_id}

    # ── /sony/system ──

    def _handle_system(self, method: str, params: list, req_id: int) -> tuple[int, dict]:
        """Handle system service JSON-RPC methods."""

        if method == "getPowerStatus":
            power = self.state.get("power", "off")
            # Sony reports "active" for on, "standby" for off
            status = "active" if power == "active" else "standby"
            return 200, {"result": [{"status": status}], "id": req_id}

        if method == "setPowerStatus":
            if params and isinstance(params[0], dict):
                status = params[0].get("status")
                if status is True:
                    self.set_state("power", "active")
                elif status is False:
                    self.set_state("power", "off")
            return 200, {"result": [], "id": req_id}

        if method == "getSystemInformation":
            model = self.state.get("model", "XBR-65X950G-SIM")
            return 200, {
                "result": [{
                    "product": "TV",
                    "region": "US",
                    "language": "en",
                    "model": model,
                    "serial": "SIM-00000001",
                    "macAddr": "00:00:00:00:00:00",
                    "name": "Bravia Simulator",
                    "generation": "3.0.0",
                }],
                "id": req_id,
            }

        if method == "getInterfaceInformation":
            return 200, {
                "result": [{
                    "productCategory": "tv",
                    "modelName": self.state.get("model", "XBR-65X950G-SIM"),
                    "productName": "BRAVIA",
                    "serverName": "Bravia Simulator",
                    "interfaceVersion": "4.0.0",
                }],
                "id": req_id,
            }

        return 200, {"error": [40400, f"Method not found: {method}"], "id": req_id}

    # ── /sony/audio ──

    def _handle_audio(self, method: str, params: list, req_id: int) -> tuple[int, dict]:
        """Handle audio service JSON-RPC methods."""

        # Audio queries return Illegal State (error 7) when TV is off
        power = self.state.get("power", "off")

        if method == "getVolumeInformation":
            if power != "active":
                return 200, {"error": [7, "Illegal State"], "id": req_id}
            volume = self.state.get("volume", 25)
            mute = self.state.get("mute", False)
            return 200, {
                "result": [[{
                    "target": "speaker",
                    "volume": volume,
                    "mute": mute,
                    "maxVolume": 100,
                    "minVolume": 0,
                }]],
                "id": req_id,
            }

        if method == "setAudioVolume":
            if power != "active":
                return 200, {"error": [7, "Illegal State"], "id": req_id}
            if params and isinstance(params[0], dict):
                vol_str = str(params[0].get("volume", "0"))
                current_vol = self.state.get("volume", 25)
                if vol_str.startswith("+"):
                    # Relative increase
                    try:
                        delta = int(vol_str)
                        new_vol = min(100, max(0, current_vol + delta))
                    except ValueError:
                        new_vol = current_vol
                elif vol_str.startswith("-"):
                    # Relative decrease
                    try:
                        delta = int(vol_str)
                        new_vol = min(100, max(0, current_vol + delta))
                    except ValueError:
                        new_vol = current_vol
                else:
                    # Absolute value
                    try:
                        new_vol = min(100, max(0, int(vol_str)))
                    except ValueError:
                        new_vol = current_vol
                self.set_state("volume", new_vol)
            return 200, {"result": [], "id": req_id}

        if method == "setAudioMute":
            if power != "active":
                return 200, {"error": [7, "Illegal State"], "id": req_id}
            if params and isinstance(params[0], dict):
                mute_status = params[0].get("status", False)
                self.set_state("mute", bool(mute_status))
            return 200, {"result": [], "id": req_id}

        return 200, {"error": [40400, f"Method not found: {method}"], "id": req_id}

    # ── /sony/avContent ──

    def _handle_av_content(self, method: str, params: list, req_id: int) -> tuple[int, dict]:
        """Handle avContent service JSON-RPC methods."""

        power = self.state.get("power", "off")

        if method == "getPlayingContentInfo":
            if power != "active":
                return 200, {"error": [7, "Illegal State"], "id": req_id}
            current_input = self.state.get("input", "extInput:hdmi?port=1")
            app = self.state.get("app", "")
            if app:
                # Currently in an app
                return 200, {
                    "result": [{
                        "uri": app,
                        "source": "app",
                        "title": app,
                    }],
                    "id": req_id,
                }
            title = _INPUT_TITLES.get(current_input, current_input)
            source = _INPUT_SOURCES.get(current_input, current_input)
            return 200, {
                "result": [{
                    "uri": current_input,
                    "source": source,
                    "title": title,
                }],
                "id": req_id,
            }

        if method == "setPlayContent":
            if power != "active":
                return 200, {"error": [7, "Illegal State"], "id": req_id}
            if params and isinstance(params[0], dict):
                uri = params[0].get("uri", "")
                if uri:
                    self.set_state("input", uri)
                    self.set_state("app", "")
            return 200, {"result": [], "id": req_id}

        if method == "getCurrentExternalInputsStatus":
            # Return available inputs
            inputs_list = []
            for uri, title in _INPUT_TITLES.items():
                inputs_list.append({
                    "uri": uri,
                    "title": title,
                    "connection": True,
                    "label": "",
                    "icon": "meta:hdmi" if "hdmi" in uri else "meta:composite",
                })
            return 200, {"result": [inputs_list], "id": req_id}

        return 200, {"error": [40400, f"Method not found: {method}"], "id": req_id}

    # ── /sony/appControl ──

    def _handle_app_control(self, method: str, params: list, req_id: int) -> tuple[int, dict]:
        """Handle appControl service JSON-RPC methods."""

        power = self.state.get("power", "off")

        if method == "setActiveApp":
            if power != "active":
                return 200, {"error": [7, "Illegal State"], "id": req_id}
            if params and isinstance(params[0], dict):
                uri = params[0].get("uri", "")
                if uri:
                    self.set_state("app", uri)
                    self.set_state("input", "app")
            return 200, {"result": [], "id": req_id}

        if method == "getApplicationList":
            # Return a few simulated apps
            return 200, {
                "result": [[
                    {"title": "Netflix", "uri": "com.sony.dtv.com.netflix.ninja"},
                    {"title": "YouTube", "uri": "com.sony.dtv.com.google.android.youtube.tv"},
                    {"title": "Prime Video", "uri": "com.sony.dtv.com.amazon.avod"},
                ]],
                "id": req_id,
            }

        return 200, {"error": [40400, f"Method not found: {method}"], "id": req_id}
