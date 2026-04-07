"""
BirdDog NDI Encoder/Decoder — Simulator

Simulates a BirdDog codec REST API on port 8080. Handles all HTTP
endpoints used by the birddog_codec driver: device info, operation mode,
NDI source list, source selection, refresh, reboot, and restart.

Note: /operationmode returns plain text (not JSON), matching real
BirdDog firmware behavior.

Driver: birddog_codec
Transport: http
"""

import json

from simulator.http_simulator import HTTPSimulator


# Default NDI sources visible on the simulated network
_DEFAULT_SOURCES = {
    "NDI Source 1": "192.168.1.100:5961",
    "NDI Source 2": "192.168.1.101:5961",
    "NDI Source 3": "192.168.1.102:5961",
}


class BirddogCodecSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "birddog_codec",
        "name": "BirdDog NDI Encoder/Decoder Simulator",
        "category": "video",
        "transport": "http",
        "default_port": 8080,
        "initial_state": {
            "hostname": "BIRDDOG-CODEC-SIM",
            "firmware": "5.0.2",
            "model": "Mini",
            "operation_mode": "Decode",
            "decode_source": "NDI Source 1",
            "source_count": 3,
        },
        "delays": {
            "command_response": 0.05,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Device stops responding to HTTP requests",
                "behavior": "no_response",
            },
            "no_sources": {
                "description": "No NDI sources available on the network",
                "set_state": {"source_count": 0},
            },
        },
        "controls": [
            {
                "type": "select",
                "key": "operation_mode",
                "label": "Mode",
                "options": ["Encode", "Decode"],
            },
            {
                "type": "select",
                "key": "decode_source",
                "label": "Decode Source",
                "options": ["NDI Source 1", "NDI Source 2", "NDI Source 3"],
            },
            {"type": "indicator", "key": "hostname", "label": "Hostname"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
            {"type": "indicator", "key": "model", "label": "Model"},
        ],
    }

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        # Strip query string for matching
        clean_path = path.split("?")[0].rstrip("/")

        # Parse JSON body for POST requests
        body_data: dict = {}
        if body:
            try:
                body_data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                pass

        # ── /about — Device info ──
        if clean_path == "/about":
            if method == "POST" and body_data:
                if "HostName" in body_data:
                    self.set_state("hostname", body_data["HostName"])
            return 200, {
                "HostName": self.get_state("hostname", "BIRDDOG-CODEC-SIM"),
                "Format": self.get_state("model", "Mini"),
                "FirmwareVersion": self.get_state("firmware", "5.0.2"),
            }

        # ── /operationmode — Current mode (plain text, not JSON) ──
        if clean_path == "/operationmode":
            if method == "POST" and body_data:
                if "mode" in body_data:
                    self.set_state("operation_mode", body_data["mode"])
            return 200, self.get_state("operation_mode", "Decode")

        # ── /List — Available NDI sources ──
        if clean_path == "/List":
            if "no_sources" in self.active_errors:
                return 200, {}
            return 200, dict(_DEFAULT_SOURCES)

        # ── /connectTo — Current/set decode source ──
        if clean_path == "/connectTo":
            if method == "POST" and body_data:
                source_name = body_data.get("sourceName", "")
                if source_name:
                    self.set_state("decode_source", source_name)
                return 200, {
                    "sourceName": self.get_state("decode_source", ""),
                }
            # GET
            return 200, {
                "sourceName": self.get_state("decode_source", ""),
            }

        # ── /refresh — Trigger NDI source list refresh ──
        if clean_path == "/refresh":
            return 200, {"status": "ok"}

        # ── /reboot — Reboot device ──
        if clean_path == "/reboot":
            return 200, {"status": "ok"}

        # ── /restart — Restart video engine ──
        if clean_path == "/restart":
            return 200, {"status": "ok"}

        # ── /encodesetup — NDI encode settings (used by set_device_setting) ──
        if clean_path == "/encodesetup":
            if method == "POST" and body_data:
                if "NDIName" in body_data:
                    self.set_state("hostname", body_data["NDIName"])
            return 200, {
                "NDIName": self.get_state("hostname", "BIRDDOG-CODEC-SIM"),
            }

        return 404, {"error": "not found"}
