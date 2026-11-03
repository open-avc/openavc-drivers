"""
Crestron Automate VX (Automate VX API) — Simulator.

Simulates the Automate VX server's REST API as Crestron's "API for 1 Beyond
Automate VX Series" describes it. ``POST /get-token`` takes the user name and
password base64-encoded in a bare ``Authorization`` header and answers
``{"status": "OK", "token": ...}``; every ``POST /api/<Call>`` carries that
token as its bare ``Authorization`` header and a JSON body (empty when the
call takes no parameters), and is answered ``{"status": "OK", ...}`` or
``{"status": "Error", "err": ...}`` with HTTP 200. StopPT and StopZ refuse
with ``"status": "error"`` and ``"message"``, as their topics show.

Reply shapes follow the document, both where it disagrees with itself: each
call asked on its own answers in its own topic's form (LayoutStatus's
``layout`` and RoomConfigStatus's ``roomConfigs`` as one-element lists, a
camera's ``name`` and ``ip``, recording space and ids as strings), and
GetAllStatus nests each status as a JSON string in the form of its example
(``layout`` and ``roomConfig`` as objects, a camera's ``model`` with no
address, numbers as numbers, the ISORecordStatus and CopyStatus calls that
have no topic of their own). Numeric body values are taken as strings or
numbers.

Serves HTTPS with a throwaway certificate unless the device's ``ssl`` setting
is off (firmware before 6.4.2 served the API over HTTP).

What the document does not say and this simulator chooses: a refused sign-in
and a refused call are answered HTTP 200 with the error body, an unknown token
401 with one; ChangeRoomConfiguration refuses while AutoSwitch runs
(ForceChangeRoomConfig exists for that case); a room configuration change puts
its first camera live; Sleep turns everything off and Wake turns output back
on (the unit's sleep and wake actions are its own); Restart Wirecast leaves
output and streaming off; a copy takes 3 s; Restart answers nothing for
``reboot_downtime`` seconds (default 5); ShotStatus names the scenario last
called as ``shotname`` and -1 after a camera was chosen by hand; the health
report's camera ids are each camera's number in the configuration it is in,
and a camera it cannot reach makes the device health and the overall status
"Unhealthy". The room configurations, cameras, layouts and scenarios are the
simulator's own.

Sign-in: user ``admin`` (or the configured ``username``). With a ``password``
configured only that password is accepted; with none, any password but
``invalid`` is.

Error modes: ``communication_timeout`` (answers nothing), ``password_changed``
(the sign-in is refused and every call answers that the credentials are not
valid), ``no_get_all_status`` (GetAllStatus answers 404, as on firmware
before 5.8) and ``health_refused`` (the health report answers an error).

Driver: crestron_automate_vx
Transport: http
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

ROOM_CONFIGS = {1: "Default", 2: "Lecture Hall"}
# configuration -> [(camera number, model, address)]
CAMERAS = {
    1: [(1, "IV-CAM-I12", "192.168.1.31"), (2, "IV-CAM-P20", "192.168.1.32"), (3, "IV-CAM-I20", "192.168.1.33")],
    2: [(1, "IV-CAM-I12", "192.168.1.31"), (2, "IV-CAM-I12D-B", "192.168.1.34")],
}
LAYOUTS = {"A": "Layout A", "B": "Side by Side", "C": "Picture in Picture"}
# scenario -> (name, cameras on its shot)
SCENARIOS = {1: ("Home", [1]), 2: ("Lectern", [2]), 3: ("Panel", [1, 3])}
MICS = ["192.168.1.40"]
COPY_S = 3.0


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _ok(message: str, **extra: Any) -> tuple[int, dict]:
    return 200, {"status": "OK", "message": message, **extra}


def _error(err: str) -> tuple[int, dict]:
    return 200, {"status": "Error", "err": err}


class CrestronAutomateVXSimulator(HTTPSimulator):
    """Simulates a Crestron Automate VX server's API."""

    SIMULATOR_INFO = {
        "driver_id": "crestron_automate_vx",
        "name": "Crestron Automate VX Simulator",
        "category": "video",
        "transport": "http",
        "default_port": 4443,
        "initial_state": {
            "autoswitch": False,
            "recording": False,
            "recording_paused": False,
            "iso_recording": False,
            "streaming": False,
            "output": True,
            "sleeping": False,
            "room_config": "1",
            "layout": "A",
            "stored_layout": "A",
            "live_camera": "1",
            "second_camera": -1,
            "scenario": 1,
            "shot_scenario": "",
            "recording_space_gb": 207,
            "recording_space_total_gb": 232,
            "cpu_utilization": "25.33%",
            "storage_available": "316.98 GB",
            "camera_2_reachable": True,
            "camera_3_needs_update": False,
            # simulator bookkeeping
            "last_preset": "",
            "last_move": "",
            "last_copy": "",
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {"type": "toggle", "key": "autoswitch", "label": "AutoSwitch"},
            {"type": "toggle", "key": "recording", "label": "Recording"},
            {"type": "toggle", "key": "iso_recording", "label": "ISO recording"},
            {"type": "toggle", "key": "streaming", "label": "Streaming"},
            {"type": "toggle", "key": "output", "label": "Output"},
            {"type": "select", "key": "room_config", "label": "Room configuration", "options": ["1", "2"]},
            {"type": "select", "key": "layout", "label": "Layout", "options": ["A", "B", "C"]},
            {"type": "select", "key": "live_camera", "label": "Live camera", "options": ["1", "2", "3"]},
            {"type": "toggle", "key": "camera_2_reachable", "label": "Camera 2 reachable"},
            {"type": "toggle", "key": "camera_3_needs_update", "label": "Camera 3 needs firmware"},
            {"type": "indicator", "key": "sleeping", "label": "Asleep"},
            {"type": "indicator", "key": "last_preset", "label": "Last preset call"},
            {"type": "indicator", "key": "last_move", "label": "Last pan/tilt/zoom"},
            {"type": "indicator", "key": "last_copy", "label": "Last copy"},
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "The server stops answering",
                "behavior": "no_response",
            },
            "password_changed": {
                "description": "The password was changed: the sign-in is refused and every call says the credentials are not valid",
                "behavior": "custom",
            },
            "no_get_all_status": {
                "description": "GetAllStatus answers 404 (firmware before 5.8)",
                "behavior": "custom",
            },
            "health_refused": {
                "description": "The health report answers an error",
                "behavior": "custom",
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        cfg = dict(config or {})
        # HTTPS unless the device is set to the older HTTP API.
        ssl = cfg.get("ssl", True)
        cfg.setdefault("tls", ssl not in (False, "false", "False", 0, "0"))
        super().__init__(device_id, cfg)
        self._username = str(cfg.get("username", "") or "admin")
        self._password = str(cfg.get("password", "") or "")
        self._reboot_downtime = float(cfg.get("reboot_downtime", 5.0) or 0)
        self._tokens: set[str] = set()
        self._presets: dict[tuple[int, int], int] = {}
        self._copy_until = 0.0
        self._down_until = 0.0
        self.requests: list[str] = []

    # ── State helpers ──

    def _config_id(self) -> int:
        cid = _int(self.state.get("room_config"))
        return cid if cid in ROOM_CONFIGS else 1

    def _cameras(self) -> list[tuple[int, str, str]]:
        return CAMERAS[self._config_id()]

    def _camera_ids(self) -> set[int]:
        return {cam for cam, _model, _ip in self._cameras()}

    def _live(self) -> list[int]:
        first = _int(self.state.get("live_camera"))
        second = _int(self.state.get("second_camera"))
        return [c for c in (first, second) if c is not None and c > 0]

    def _put_live(self, cameras: list[int]) -> None:
        self.set_state("live_camera", str(cameras[0]) if cameras else "-1")
        self.set_state("second_camera", cameras[1] if len(cameras) > 1 else -1)

    def _reachable(self, ip: str) -> bool:
        if ip == "192.168.1.32":
            return bool(self.state.get("camera_2_reachable", True))
        return True

    # ── Dispatch ──

    def handle_request(self, method: str, path: str, headers: dict[str, str], body: str):
        if time.monotonic() < self._down_until:
            return 503, "Service Unavailable"
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "").strip()
        self.requests.append(f"{method} {path}" + (f" {body}" if body else ""))
        del self.requests[:-200]
        route = urlsplit(path).path
        if method != "POST":
            return 405, "Method Not Allowed"
        if route == "/get-token":
            return self._get_token(auth)
        if not route.startswith("/api/"):
            return 404, "Not Found"
        if "password_changed" in self.active_errors:
            return _error("Credentials are not valid")
        if auth not in self._tokens:
            return 401, {"status": "Error", "err": "Credentials are not valid"}
        payload: Any = {}
        if body.strip():
            try:
                payload = json.loads(body)
            except ValueError:
                return _error("Request body is not valid JSON")
        if not isinstance(payload, dict):
            return _error("Request body must be a JSON object")
        handler = self._CALLS.get(route[len("/api/"):])
        if handler is None:
            return 404, "Not Found"
        return handler(self, payload)

    def _get_token(self, auth: str):
        try:
            user, _, password = base64.b64decode(auth, validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return _error("Incorrect Username or Password")
        accepted = user == self._username and (
            password == self._password if self._password else password != "invalid"
        )
        if not accepted or "password_changed" in self.active_errors:
            return _error("Incorrect Username or Password")
        token = secrets.token_hex(16)
        self._tokens.add(token)
        return 200, {"status": "OK", "token": token}

    # ── Status, as each call's own topic shows it ──

    def _flag(self, key: str, on: str, off: str):
        value = bool(self.state.get(key))
        return 200, {"status": "OK", "results": value, "message": on if value else off}

    def _autoswitch_status(self, payload: dict):
        return self._flag("autoswitch", "AutoSwitching in Progress", "No AutoSwitching in Progress")

    def _record_status(self, payload: dict):
        return self._flag("recording", "Record in Progress", "No Record in Progress")

    def _stream_status(self, payload: dict):
        return self._flag("streaming", "Stream in Progress", "No Stream in Progress")

    def _output_status(self, payload: dict):
        return self._flag("output", "Output in Progress", "No Output in Progress")

    def _recording_space(self, payload: dict):
        return _ok(
            "Available recording storage queried successfully",
            available_gigabytes=str(self.state.get("recording_space_gb")),
            total_gigabytes=str(self.state.get("recording_space_total_gb")),
        )

    def _get_cameras(self, payload: dict):
        return _ok("cameras loaded successfully", cameras=[
            {"id": str(cam), "name": model, "ip": ip} for cam, model, ip in self._cameras()
        ])

    def _camera_status(self, payload: dict):
        live = self._live()
        return _ok("Live camera address queried successfully", address=str(live[0]) if live else "-1")

    def _get_layouts(self, payload: dict):
        return _ok("Layouts loaded successfully", layouts=[
            {"id": lid, "name": name} for lid, name in LAYOUTS.items()
        ])

    def _layout_status(self, payload: dict):
        lid = str(self.state.get("layout") or "A")
        return _ok("Current layout queried successfully", layout=[{"id": lid, "name": LAYOUTS.get(lid, lid)}])

    def _get_room_configs(self, payload: dict):
        return _ok("Room configs loaded successfully", roomConfigs=[
            {"id": str(cid), "name": name} for cid, name in ROOM_CONFIGS.items()
        ])

    def _room_config_status(self, payload: dict):
        cid = self._config_id()
        return _ok("Current room configuration queried successfully",
                   roomConfigs=[{"id": str(cid), "name": ROOM_CONFIGS[cid]}])

    def _get_scenarios(self, payload: dict):
        return _ok("Scenarios loaded successfully", scenarios=[
            {"id": sid, "name": name} for sid, (name, _cams) in SCENARIOS.items()
        ])

    def _scenario_status(self, payload: dict):
        sid = _int(self.state.get("scenario")) or 1
        name = SCENARIOS.get(sid, ("Home", []))[0]
        return _ok("Successfully read selected scenario", scenario={"id": sid, "name": name})

    def _shot_status(self, payload: dict):
        live = self._live()
        return _ok(
            "Shot status queried successfully.",
            shotname=self.state.get("shot_scenario") or -1,
            layout=str(self.state.get("layout") or "A"),
            storedLayout=str(self.state.get("stored_layout") or "A"),
            cam1=live[0] if live else -1,
            cam2=live[1] if len(live) > 1 else -1,
        )

    def _all_status(self, payload: dict):
        """GetAllStatus, in the form of the document's example."""
        if "no_get_all_status" in self.active_errors:
            return 404, "Not Found"
        cid = self._config_id()
        lid = str(self.state.get("layout") or "A")
        live = self._live()
        copying = time.monotonic() < self._copy_until
        parts = [
            ("AutoSwitchStatus", self._autoswitch_status({})[1]),
            ("StreamStatus", self._stream_status({})[1]),
            ("OutputStatus", self._output_status({})[1]),
            ("ISORecordStatus", {
                "status": "OK", "results": bool(self.state.get("iso_recording")),
                "message": "Recording ISO in progress" if self.state.get("iso_recording") else "Recording ISO not in progress",
            }),
            ("RecordStatus", self._record_status({})[1]),
            ("RecordingSpaceAvail", {
                "status": "OK", "message": "Available recording space queried successfully.",
                "available_gigabytes": int(self.state.get("recording_space_gb") or 0),
                "total_gigabytes": int(self.state.get("recording_space_total_gb") or 0),
            }),
            ("CopyStatus", {
                "status": "OK", "message": "Copying." if copying else "Not copying.", "copy_underway": copying,
            }),
            ("GetCameras", {
                "status": "OK", "message": "cameras loaded successfully",
                "cameras": [{"id": cam, "model": model} for cam, model, _ip in self._cameras()],
            }),
            ("CameraStatus", {
                "status": "OK", "message": "Live camera address queried successfully.",
                "address": live[0] if live else -1, "addresses": live,
            }),
            ("GetLayouts", self._get_layouts({})[1]),
            ("LayoutStatus", {
                "status": "OK", "message": "Current layout queried successfully",
                "layout": {"id": lid, "name": LAYOUTS.get(lid, lid)},
            }),
            ("GetRoomConfigs", {
                "status": "OK", "message": "Room configs loaded successfully",
                "roomConfigs": [{"id": c, "name": n} for c, n in ROOM_CONFIGS.items()],
            }),
            ("RoomConfigStatus", {
                "status": "OK", "message": "Current room configuration queried successfully",
                "roomConfig": {"id": cid, "name": ROOM_CONFIGS[cid]},
            }),
        ]
        answer: dict[str, Any] = {"status": "OK"}
        for n, (call, inner) in enumerate(parts):
            answer[f"api_call_{n}"] = {
                "request_api": call, "response_code": 200, "response_body": json.dumps(inner),
            }
        return 200, answer

    def _health(self, payload: dict):
        if "health_refused" in self.active_errors:
            return _error("Failed to gather health information")
        cid = self._config_id()
        by_ip: dict[str, list[dict[str, int]]] = {}
        for config, cams in CAMERAS.items():
            for cam, _model, ip in cams:
                by_ip.setdefault(ip, []).append({"configID": config, "deviceID": cam})
        cameras = [{"address": ip, "pingable": self._reachable(ip), "configs": configs}
                   for ip, configs in by_ip.items()]
        unhealthy = any(not c["pingable"] for c in cameras)
        firmware = {}
        for ip, configs in by_ip.items():
            current = next((c for c in configs if c["configID"] == cid), configs[0])
            needs = ip == "192.168.1.33" and bool(self.state.get("camera_3_needs_update"))
            firmware[ip] = {"id": current["deviceID"], "requiresUpdate": needs}
        status = "Unhealthy" if unhealthy else "Healthy"
        errors = [f"Camera at {c['address']} is not reachable." for c in cameras if not c["pingable"]]
        return 200, {
            "status": status,
            "message": "",
            "autoSwitchHealth": {
                "status": "Running" if self.state.get("autoswitch") else "None",
                "running": bool(self.state.get("autoswitch")),
            },
            "deviceHealth": {
                "status": status,
                "cameras": cameras,
                "mics": [{"address": ip, "pingable": True,
                          "configs": [{"configID": c, "deviceID": 1} for c in ROOM_CONFIGS]} for ip in MICS],
                "vps": [],
            },
            "configHealth": {
                "status": "Healthy",
                "currentConfig": str(cid),
                "configs": {str(c): {"name": n} for c, n in ROOM_CONFIGS.items()},
            },
            "cameraFirmware": {
                "status": "Healthy" if not any(f["requiresUpdate"] for f in firmware.values()) else "Warning",
                "cameras": firmware,
            },
            "errors": {"status": "Errors" if errors else "None", "errors": " ".join(errors) or "None to report."},
            "hardwareHealth": {
                "status": "Healthy",
                "hardwareData": {
                    "cpu": {"status": "Healthy", "utilization": str(self.state.get("cpu_utilization"))},
                    "dDrive": {"status": "Healthy", "available_storage": str(self.state.get("storage_available"))},
                },
            },
        }

    # ── Actions ──

    def _start_autoswitch(self, payload: dict):
        self.set_state("autoswitch", True)
        return _ok("AutoSwitching Started Successfully")

    def _stop_autoswitch(self, payload: dict):
        self.set_state("autoswitch", False)
        return _ok("AutoSwitching Stopped Successfully")

    def _start_record(self, payload: dict):
        self.set_state("recording", True)
        self.set_state("recording_paused", False)
        return _ok("Record Started Successfully")

    def _pause_record(self, payload: dict):
        if not self.state.get("recording"):
            return _error("No recording in progress")
        self.set_state("recording_paused", True)
        return _ok("Record Paused Successfully")

    def _stop_record(self, payload: dict):
        self.set_state("recording", False)
        self.set_state("recording_paused", False)
        return _ok("Record Stopped Successfully")

    def _start_stream(self, payload: dict):
        self.set_state("streaming", True)
        return _ok("Stream Started Successfully")

    def _stop_stream(self, payload: dict):
        self.set_state("streaming", False)
        return _ok("Stream Stopped Successfully")

    def _start_output(self, payload: dict):
        if self.state.get("output"):
            return _ok("Output is in progress")
        self.set_state("output", True)
        return _ok("Started Output")

    def _stop_output(self, payload: dict):
        if not self.state.get("output"):
            return _ok("No output in progress")
        self.set_state("output", False)
        return _ok("Output Stopped")

    def _sleep(self, payload: dict):
        for key in ("output", "autoswitch", "recording", "streaming"):
            self.set_state(key, False)
        self.set_state("sleeping", True)
        return _ok("VX went to sleep successfully")

    def _wake(self, payload: dict):
        self.set_state("sleeping", False)
        self.set_state("output", True)
        return _ok("VX woke up successfully")

    def _change_layout(self, payload: dict):
        lid = str(payload.get("id", "")).strip()
        if lid not in LAYOUTS:
            return _error(f"Layout {lid or '(none)'} does not exist")
        if lid == self.state.get("layout"):
            return _ok(f"Already on Layout {lid}")
        self.set_state("stored_layout", self.state.get("layout"))
        self.set_state("layout", lid)
        return _ok(f"Changed to Layout {lid}")

    def _switch_config(self, cid: int):
        self.set_state("room_config", str(cid))
        self._put_live([CAMERAS[cid][0][0]])

    def _change_room_config(self, payload: dict):
        cid = _int(payload.get("id"))
        if cid not in ROOM_CONFIGS:
            return _error(f"Room configuration {payload.get('id')} does not exist")
        if cid == self._config_id():
            return _ok(f"Already on Room Configuration {cid}")
        if self.state.get("autoswitch"):
            return _error("AutoSwitching is in progress")
        self._switch_config(cid)
        return _ok(f"Changed to Room Configuration {cid}")

    def _force_change_room_config(self, payload: dict):
        cid = _int(payload.get("id"))
        if cid not in ROOM_CONFIGS:
            return _error(f"Room configuration {payload.get('id')} does not exist")
        if cid == self._config_id():
            return _ok(f"Already on Room Configuration {cid}")
        self._switch_config(cid)
        return _ok(f"Changed to Room Configuration {cid}")

    def _go_to_scenario(self, payload: dict):
        sid = _int(payload.get("id"))
        if sid not in SCENARIOS:
            return _error(f"Could not find scenario: {payload.get('id')}")
        name, cams = SCENARIOS[sid]
        cams = [c for c in cams if c in self._camera_ids()] or [self._cameras()[0][0]]
        self.set_state("scenario", sid)
        self.set_state("shot_scenario", name)
        self._put_live(cams)
        return _ok(f"Successfully called scenario {sid}", cameras=cams)

    def _go_home(self, payload: dict):
        self.set_state("scenario", 1)
        self.set_state("shot_scenario", SCENARIOS[1][0])
        self._put_live([SCENARIOS[1][1][0]])
        return _ok("Successfully called Home Shot and preset")

    def _camera_arg(self, payload: dict, field: str = "cam") -> int | None:
        cam = _int(payload.get(field))
        return cam if cam in self._camera_ids() else None

    def _manual_switch(self, payload: dict):
        cam = self._camera_arg(payload, "address")
        if cam is None:
            return _error(f"Camera address {payload.get('address')} is invalid")
        self.set_state("shot_scenario", "")
        self._put_live([cam])
        return _ok(f"Successfully switched shot to {cam}")

    def _preset(self, payload: dict, save: bool):
        cam = self._camera_arg(payload)
        pre = _int(payload.get("pre"))
        if cam is None or pre is None or not 1 <= pre <= 255:
            return _error(f"Camera preset {payload.get('pre')} is invalid")
        if save:
            self._presets[(cam, pre)] = int(time.time())
            self.set_state("last_preset", f"saved camera {cam} preset {pre}")
            return _ok(f"Successfully saved preset {pre} for camera {cam}")
        self.set_state("last_preset", f"called camera {cam} preset {pre}")
        return _ok(f"Successfully called preset {pre} for camera {cam}")

    def _call_preset(self, payload: dict):
        return self._preset(payload, save=False)

    def _save_preset(self, payload: dict):
        return self._preset(payload, save=True)

    def _start_pt(self, payload: dict):
        cam = self._camera_arg(payload)
        direction = _int(payload.get("ptDir"))
        if cam is None or direction is None or not 0 <= direction <= 7:
            return _error(f"Could not move camera {payload.get('cam')}")
        self.set_state("last_move", f"camera {cam} pan/tilt direction {direction}")
        return _ok(f"Successfully called started movement for camera {cam}")

    def _start_z(self, payload: dict):
        cam = self._camera_arg(payload)
        direction = _int(payload.get("zDir"))
        if cam is None or direction not in (0, 1):
            return _error(f"Could not move camera {payload.get('cam')}")
        self.set_state("last_move", f"camera {cam} zoom {'in' if direction == 0 else 'out'}")
        return _ok("Zoom started")

    def _stop_motion(self, payload: dict):
        cam = self._camera_arg(payload)
        if cam is None:
            return 200, {"status": "error", "message": f"Camera address {payload.get('cam')} is invalid"}
        self.set_state("last_move", f"camera {cam} stopped")
        return _ok(f"Successfully called stop camera {cam}")

    def _export_presets(self, payload: dict):
        return 200, {"status": "OK", "message": "Camera presets exported successfully"}

    def _import_presets(self, payload: dict):
        return 200, {"status": "OK", "message": "Camera presets imported successfully"}

    def _copy_files(self, payload: dict):
        destination = payload.get("destination")
        if not isinstance(destination, str) or not destination.strip():
            return _error("A destination is required")
        if not isinstance(payload.get("deleteSource"), bool):
            return _error("deleteSource must be true or false")
        self._copy_until = time.monotonic() + COPY_S
        what = "moved" if payload["deleteSource"] else "copied"
        self.set_state("last_copy", f"{what} to {destination}")
        return _ok("Successfully backed up information")

    def _close_wirecast(self, payload: dict):
        self.set_state("output", False)
        self.set_state("streaming", False)
        return _ok("Wirecast closed successfully")

    def _restart(self, payload: dict):
        self._down_until = time.monotonic() + self._reboot_downtime
        self._tokens.clear()
        return _ok("Restart command initiated")

    _CALLS = {
        "GetAllStatus": _all_status,
        "AutoSwitchStatus": _autoswitch_status,
        "RecordStatus": _record_status,
        "StreamStatus": _stream_status,
        "OutputStatus": _output_status,
        "RecordingSpaceAvail": _recording_space,
        "GetCameras": _get_cameras,
        "CameraStatus": _camera_status,
        "GetLayouts": _get_layouts,
        "LayoutStatus": _layout_status,
        "GetRoomConfigs": _get_room_configs,
        "RoomConfigStatus": _room_config_status,
        "GetScenarios": _get_scenarios,
        "ScenarioStatus": _scenario_status,
        "ShotStatus": _shot_status,
        "HealthStatus": _health,
        "StartAutoSwitch": _start_autoswitch,
        "StopAutoSwitch": _stop_autoswitch,
        "StartRecord": _start_record,
        "PauseRecord": _pause_record,
        "StopRecord": _stop_record,
        "StartStream": _start_stream,
        "StopStream": _stop_stream,
        "StartOutput": _start_output,
        "StopOutput": _stop_output,
        "Sleep": _sleep,
        "Wake": _wake,
        "ChangeLayout": _change_layout,
        "ChangeRoomConfiguration": _change_room_config,
        "ForceChangeRoomConfig": _force_change_room_config,
        "GoToScenario": _go_to_scenario,
        "GoHome": _go_home,
        "ManualSwitchCamera": _manual_switch,
        "CallCameraPreset": _call_preset,
        "SaveCameraPreset": _save_preset,
        "StartPT": _start_pt,
        "StopPT": _stop_motion,
        "StartZ": _start_z,
        "StopZ": _stop_motion,
        "ExportCameraPresets": _export_presets,
        "ImportCameraPresets": _import_presets,
        "CopyFiles": _copy_files,
        "CloseWirecast": _close_wirecast,
        "Restart": _restart,
    }
