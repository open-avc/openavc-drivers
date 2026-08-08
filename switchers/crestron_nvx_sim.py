"""
Crestron DM NVX — Simulator

Role-aware simulation of a DM NVX endpoint over the CresNext REST API. The
simulated ``device_mode`` (Transmitter or Receiver) decides which stream
object answers and which the driver sees as "UNSUPPORTED PROPERTY" — matching
real hardware, where an encoder has no StreamReceive and a decoder no
StreamTransmit. Default is Receiver; set ``device_mode`` in the launch config
to simulate an encoder.

Driver: crestron_nvx
Transport: http (HTTPS on 443)

The NVX REST API is HTTPS-only and the unit ships a self-signed certificate, so
the driver has an https:// base URL and leaves verification off. The simulator
serves TLS to match (``"tls": True``), which lets the driver connect here the
same way it connects to hardware.
"""

import json

from openavc.simulator.http_simulator import HTTPSimulator


class CrestronNvxSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "crestron_nvx",
        "name": "Crestron DM NVX Simulator",
        "category": "switcher",
        "transport": "http",
        "default_port": 443,
        # HTTPS-only device: serve TLS with an ephemeral self-signed cert.
        "tls": True,
        "initial_state": {
            "device_mode": "Receiver",     # "Transmitter" | "Receiver"
            "model": "DM-NVX-D200",
            "serial_number": "SIM00000001",
            "firmware": "7.1.5259.00090",
            "device_name": "DM-NVX-D200-SIM",
            "device_ready": True,
            "video_source": "Stream",
            "audio_source": "AudioFollowsVideo",
            "active_video_source": "Stream",
            "active_audio_source": "AudioFollowsVideo",
            "leds_enabled": True,
            "front_panel_locked": False,
            # Stream (primary)
            "stream_status": "Stream stopped",
            "stream_multicast": "",
            "stream_location": "",
            "bitrate": 700,
            "active_bitrate": 0,
            "bitrate_mode": "Fixed",
            # AV I/O
            "input_sync": False,
            "output_connected": False,
            "h_res": 0,
            "v_res": 0,
            "fps": 0,
            "scaler_resolution": "Auto",
            "video_wall_mode": False,
        },
        "delays": {"command_response": 0.03},
        "error_modes": {
            "communication_timeout": {
                "description": "NVX stops responding to requests",
                "behavior": "no_response",
            },
            "no_sync": {
                "description": "No sync on the HDMI input/output",
                "set_state": {"input_sync": False, "h_res": 0, "v_res": 0},
            },
        },
        "controls": [
            {"type": "indicator", "key": "device_mode", "label": "Mode"},
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "select", "key": "video_source", "label": "Video Source",
             "options": ["None", "Input1", "Input2", "Stream"]},
            {"type": "select", "key": "audio_source", "label": "Audio Source",
             "options": ["AudioFollowsVideo", "Input1", "Input2", "AnalogAudio",
                         "PrimaryStreamAudio", "SecondaryStreamAudio"]},
            {"type": "toggle", "key": "leds_enabled", "label": "Front LEDs"},
            {"type": "indicator", "key": "stream_status", "label": "Stream"},
            {"type": "slider", "key": "bitrate", "label": "Bitrate", "min": 100, "max": 950, "step": 10},
            {"type": "toggle", "key": "input_sync", "label": "Input Sync",
             "color_map": {"true": "#22c55e", "false": "#6b7280"}},
        ],
    }

    def _is_tx(self) -> bool:
        return self.get_state("device_mode", "Receiver") == "Transmitter"

    def handle_request(self, method, path, headers, body):
        # ── Auth ──
        if path == "/userlogin.html":
            return 200, "<html><body>OK</body></html>"
        if path == "/logout":
            return 200, "<html><body>bye</body></html>"
        if path == "/Device/Authentication" and method == "POST":
            # First-boot create-admin (wizard). Accept any well-formed request.
            try:
                data = json.loads(body)
                auth = data["Device"]["Authentication"]["AuthenticationState"]
                if auth.get("AdminUsername") and auth.get("AdminPassword"):
                    return 200, {"Actions": [{"Operation": "SetPartial", "Results": [
                        {"Path": "Device.Authentication", "Property": "AuthenticationState",
                         "StatusId": 0, "StatusInfo": "OK"}]}]}
            except Exception:
                pass
            return 200, {"Actions": [{"Operation": "SetPartial", "Results": [
                {"StatusId": -4, "StatusInfo": "Invalid createuser"}]}]}

        # ── GET object tree ──
        if method == "GET" and path.startswith("/Device/"):
            obj = path[len("/Device/"):]
            return 200, self._get_object(obj)

        # ── POST (writes) ──
        if path == "/Device" and method == "POST":
            return self._handle_post(body)

        return 404, {"error": "Not Found"}

    # ── GET builders ──

    def _get_object(self, obj: str) -> dict:
        if obj == "DeviceInfo":
            return {"Device": {"DeviceInfo": {
                "Model": self.get_state("model", "DM-NVX-D200"),
                "Manufacturer": "Crestron",
                "Category": "DM",
                "SerialNumber": self.get_state("serial_number", ""),
                "Name": self.get_state("device_name", ""),
                "PufVersion": self.get_state("firmware", ""),
                "DeviceVersion": self.get_state("firmware", ""),
                "MacAddress": "c4.42.68.00.00.01",
            }}}
        if obj == "DeviceSpecific":
            return {"Device": {"DeviceSpecific": {
                "DeviceMode": self.get_state("device_mode", "Receiver"),
                "DeviceReady": 1 if self.get_state("device_ready", True) else 0,
                "VideoSource": self.get_state("video_source", "Stream"),
                "AudioSource": self.get_state("audio_source", "AudioFollowsVideo"),
                "ActiveVideoSource": self.get_state("active_video_source", "Stream"),
                "ActiveAudioSource": self.get_state("active_audio_source", "AudioFollowsVideo"),
                "LedsEnabled": bool(self.get_state("leds_enabled", True)),
                "IsFrontPanelLockoutEnabled": bool(self.get_state("front_panel_locked", False)),
                **({"VideoWallMode": 1 if self.get_state("video_wall_mode") else 0}
                   if not self._is_tx() else {}),
                "Version": "2.1.0",
            }}}
        if obj == "AudioVideoInputOutput":
            return self._build_av_io()
        if obj == "StreamTransmit":
            if not self._is_tx():
                return {"Device": {"StreamTransmit": "UNSUPPORTED PROPERTY, CHECK REST API!!"}}
            return {"Device": {"StreamTransmit": {"Streams": [self._build_stream(tx=True)], "Version": "2.1.7"}}}
        if obj == "StreamReceive":
            if self._is_tx():
                return {"Device": {"StreamReceive": "UNSUPPORTED PROPERTY, CHECK REST API!!"}}
            return {"Device": {"StreamReceive": {"Streams": [self._build_stream(tx=False)], "Version": "2.0.1"}}}
        if obj == "DeviceOperations":
            return {"Device": {"DeviceOperations": {"UpgradeStatus": "Puf Upgrade Not Initiated", "Version": "2.3.0"}}}
        return {"Device": {obj: "UNSUPPORTED PROPERTY, CHECK REST API!!"}}

    def _build_av_io(self) -> dict:
        h, v, fps = self.get_state("h_res", 0), self.get_state("v_res", 0), self.get_state("fps", 0)
        port_common = {
            "HorizontalResolution": h, "VerticalResolution": v, "FramesPerSecond": fps,
            "IsInterlacedDetected": False, "Hdmi": {"HdcpState": "Non-HDCPSource"},
        }
        if self._is_tx():
            inp = {**port_common, "IsSyncDetected": bool(self.get_state("input_sync", False)),
                   "IsSourceDetected": bool(self.get_state("input_sync", False)), "PortType": "Hdmi"}
            return {"Device": {"AudioVideoInputOutput": {
                "Inputs": [{"Name": "input0", "Ports": [inp], "VideoPortTypeSelect": "Hdmi"}],
                "Outputs": [], "Version": "2.4.10"}}}
        out = {**port_common, "IsSinkConnected": bool(self.get_state("output_connected", False)),
               "Resolution": self.get_state("scaler_resolution", "Auto"), "PortType": "Hdmi"}
        return {"Device": {"AudioVideoInputOutput": {
            "Inputs": [], "Outputs": [{"Name": "output0", "Ports": [out], "VideoPortTypeSelect": "Hdmi"}],
            "Version": "2.4.10"}}}

    def _build_stream(self, *, tx: bool) -> dict:
        s = {
            "Status": self.get_state("stream_status", "Stream stopped"),
            "MulticastAddress": self.get_state("stream_multicast", ""),
            "StreamLocation": self.get_state("stream_location", ""),
            "SessionInitiation": "Multicast via RTSP",
            "StreamType": "Primary",
            "Start": self.get_state("stream_status") == "Stream started",
            "Stop": self.get_state("stream_status") != "Stream started",
        }
        if tx:
            s.update({"Bitrate": self.get_state("bitrate", 700),
                      "ActiveBitrate": self.get_state("active_bitrate", 0),
                      "BitrateMode": self.get_state("bitrate_mode", "Fixed")})
        else:
            s.update({"InitiatorAddress": "", "Bitrate": self.get_state("active_bitrate", 0),
                      "StreamProfile": "High"})
        return s

    # ── POST handling ──

    def _handle_post(self, body: str):
        try:
            data = json.loads(body)
            dev = data["Device"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return 400, {"error": "Invalid JSON"}

        results = []

        def ok(path, prop):
            results.append({"Path": path, "Property": prop, "StatusId": 0, "StatusInfo": "OK"})

        def err(path, prop, info="Generic error"):
            results.append({"Path": path, "Property": prop, "StatusId": -4, "StatusInfo": info})

        if "DeviceSpecific" in dev:
            for prop, val in dev["DeviceSpecific"].items():
                key = {"VideoSource": "video_source", "AudioSource": "audio_source",
                       "LedsEnabled": "leds_enabled",
                       "IsFrontPanelLockoutEnabled": "front_panel_locked"}.get(prop)
                if key:
                    self.set_state(key, val)
                    if prop == "VideoSource":
                        self.set_state("active_video_source", val)
                    if prop == "AudioSource":
                        self.set_state("active_audio_source", val)
                ok("Device.DeviceSpecific." + prop, prop)

        for obj in ("StreamTransmit", "StreamReceive"):
            if obj in dev and isinstance(dev[obj], dict):
                streams = dev[obj].get("Streams", [])
                if streams:
                    self._apply_stream(obj, streams[0], ok, err)

        if "DeviceOperations" in dev:
            for prop in dev["DeviceOperations"]:
                ok("Device.DeviceOperations." + prop, prop)

        if "AudioVideoInputOutput" in dev:
            outs = dev["AudioVideoInputOutput"].get("Outputs", [])
            if outs and outs[0].get("Ports"):
                res = outs[0]["Ports"][0].get("Resolution")
                if res is not None:
                    self.set_state("scaler_resolution", res)
                ok("Device.AudioVideoInputOutput.Outputs[0]", "Resolution")

        return 200, {"Actions": [{"Operation": "SetPartial", "Results": results}]}

    def _apply_stream(self, obj: str, fields: dict, ok, err) -> None:
        base = f"Device.{obj}.Streams[0]"
        running = self.get_state("stream_status") == "Stream started"
        for prop, val in fields.items():
            if prop == "MulticastAddress":
                # Real NVX rejects a multicast change while running (RTSP mode).
                if running:
                    err(base, prop)
                    continue
                self.set_state("stream_multicast", val)
                ok(base, prop)
            elif prop == "StreamLocation":
                self.set_state("stream_location", val)
                ok(base, prop)
            elif prop == "Bitrate":
                self.set_state("bitrate", val)
                self.set_state("active_bitrate", max(0, int(val) - 60))
                ok(base, prop)
            elif prop == "BitrateMode":
                self.set_state("bitrate_mode", val)
                ok(base, prop)
            elif prop == "Start" and val:
                self.set_state("stream_status", "Stream started")
                if obj == "StreamTransmit" and not self.get_state("stream_location"):
                    # Encoder auto-derives its RTSP URL when it starts.
                    self.set_state("stream_location", "rtsp://127.0.0.1:554/live.sdp")
                ok(base, prop)
            elif prop == "Stop" and val:
                self.set_state("stream_status", "Stream stopped")
                ok(base, prop)
            else:
                ok(base, prop)
