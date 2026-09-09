"""
Simulator for the OBS Studio driver (obs_studio).

Speaks enough of obs-websocket 5 to exercise the driver end to end: the
Hello / Identify handshake with the SHA-256 challenge, per-client event
subscriptions, id-correlated requests, request batches, and the events OBS
fires when something changes, whether the change came from a request or
from the Simulator UI. Runs on the platform WebSocketSimulator base (plain
ws:// on localhost).

The fixture is a small lecture-capture setup: three scenes (Camera, Slides,
Break), eight inputs of the kinds the driver treats specially (a text
source, a media source, a browser source, two audio-only inputs), a few
filters and the outputs OBS always has plus one NDI output.

Password: the simulator requires authentication only when its config
carries ``password`` (the tests set it). The ``authentication_failed`` error
mode rejects every password, which is what a wrong Server Password does.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
from typing import Any

from openavc.simulator.websocket_simulator import WebSocketSimulator

OBS_VERSION = "32.0.1"
WS_VERSION = "5.6.2"
RPC_VERSION = 1

OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_REIDENTIFY = 0, 1, 2, 3
OP_EVENT, OP_REQUEST, OP_REQUEST_RESPONSE, OP_REQUEST_BATCH, OP_REQUEST_BATCH_RESPONSE = 5, 6, 7, 8, 9

CLOSE_NOT_IDENTIFIED = 4007
CLOSE_AUTHENTICATION_FAILED = 4009
CLOSE_UNSUPPORTED_RPC_VERSION = 4010

# EventSubscription intents
SUB = {
    "General": 1 << 0, "Config": 1 << 1, "Scenes": 1 << 2, "Inputs": 1 << 3,
    "Transitions": 1 << 4, "Filters": 1 << 5, "Outputs": 1 << 6,
    "SceneItems": 1 << 7, "MediaInputs": 1 << 8, "Vendors": 1 << 9, "Ui": 1 << 10,
    "InputVolumeMeters": 1 << 16, "InputActiveStateChanged": 1 << 17,
    "InputShowStateChanged": 1 << 18,
}
EVENT_INTENT = {
    "ExitStarted": "General",
    "CurrentSceneCollectionChanging": "Config", "CurrentSceneCollectionChanged": "Config",
    "SceneCollectionListChanged": "Config", "CurrentProfileChanged": "Config",
    "ProfileListChanged": "Config",
    "SceneCreated": "Scenes", "SceneRemoved": "Scenes", "SceneNameChanged": "Scenes",
    "CurrentProgramSceneChanged": "Scenes", "CurrentPreviewSceneChanged": "Scenes",
    "SceneListChanged": "Scenes",
    "InputCreated": "Inputs", "InputRemoved": "Inputs", "InputNameChanged": "Inputs",
    "InputSettingsChanged": "Inputs", "InputMuteStateChanged": "Inputs",
    "InputVolumeChanged": "Inputs", "InputAudioBalanceChanged": "Inputs",
    "InputAudioSyncOffsetChanged": "Inputs", "InputAudioMonitorTypeChanged": "Inputs",
    "InputVolumeMeters": "InputVolumeMeters",
    "InputActiveStateChanged": "InputActiveStateChanged",
    "InputShowStateChanged": "InputShowStateChanged",
    "CurrentSceneTransitionChanged": "Transitions",
    "CurrentSceneTransitionDurationChanged": "Transitions",
    "SceneTransitionStarted": "Transitions", "SceneTransitionEnded": "Transitions",
    "SourceFilterCreated": "Filters", "SourceFilterRemoved": "Filters",
    "SourceFilterNameChanged": "Filters", "SourceFilterEnableStateChanged": "Filters",
    "StreamStateChanged": "Outputs", "RecordStateChanged": "Outputs",
    "RecordFileChanged": "Outputs", "ReplayBufferStateChanged": "Outputs",
    "VirtualcamStateChanged": "Outputs", "ReplayBufferSaved": "Outputs",
    "SceneItemCreated": "SceneItems", "SceneItemRemoved": "SceneItems",
    "SceneItemListReindexed": "SceneItems", "SceneItemEnableStateChanged": "SceneItems",
    "SceneItemLockStateChanged": "SceneItems",
    "MediaInputPlaybackStarted": "MediaInputs", "MediaInputPlaybackEnded": "MediaInputs",
    "MediaInputActionTriggered": "MediaInputs",
    "StudioModeStateChanged": "Ui",
}

# RequestStatus
OK = 100
UNKNOWN_REQUEST_TYPE = 204
MISSING_REQUEST_FIELD = 300
INVALID_REQUEST_FIELD = 400
REQUEST_FIELD_OUT_OF_RANGE = 402
OUTPUT_RUNNING = 500
OUTPUT_NOT_RUNNING = 501
OUTPUT_PAUSED = 502
OUTPUT_NOT_PAUSED = 503
STUDIO_MODE_NOT_ACTIVE = 506
RESOURCE_NOT_FOUND = 600
INVALID_RESOURCE_STATE = 604
RESOURCE_NOT_CONFIGURABLE = 606

MEDIA_KINDS = ("ffmpeg_source", "vlc_source", "slideshow")
TEXT_KINDS = ("text_gdiplus", "text_ft2_source")

# The fixture. Every name is what OBS would show; kinds are real OBS kinds
# (versioned as OBS reports them, with the unversioned form beside).
INPUTS: dict[str, dict[str, Any]] = {
    "Camera": {"kind": "av_capture_input_v2", "unversioned": "av_capture_input", "audio": False},
    "Mic/Aux": {"kind": "coreaudio_input_capture", "unversioned": "coreaudio_input_capture", "audio": True},
    "Desktop Audio": {"kind": "coreaudio_output_capture", "unversioned": "coreaudio_output_capture", "audio": True},
    "Slides Capture": {"kind": "screen_capture", "unversioned": "screen_capture", "audio": False},
    "Lower Third": {"kind": "text_ft2_source_v2", "unversioned": "text_ft2_source", "audio": False,
                    "settings": {"text": "Welcome", "font": {"face": "Helvetica", "size": 64}}},
    "Logo": {"kind": "image_source", "unversioned": "image_source", "audio": False},
    "Break Loop": {"kind": "ffmpeg_source", "unversioned": "ffmpeg_source", "audio": True,
                   "media": {"state": "OBS_MEDIA_STATE_STOPPED", "duration": 184000, "cursor": 0}},
    "Web Timer": {"kind": "browser_source", "unversioned": "browser_source", "audio": False},
}
# scene -> ordered items (top first): (source, enabled)
SCENES: dict[str, list[tuple[str, bool]]] = {
    "Camera": [("Lower Third", False), ("Logo", True), ("Camera", True)],
    "Slides": [("Camera", True), ("Slides Capture", True)],
    "Break": [("Logo", True), ("Web Timer", True), ("Break Loop", True)],
}
FILTERS: dict[str, list[dict[str, Any]]] = {
    "Camera": [{"name": "Color Correction", "kind": "color_filter_v2", "enabled": True}],
    "Mic/Aux": [{"name": "Noise Suppression", "kind": "noise_suppress_filter_v2", "enabled": True},
                {"name": "Compressor", "kind": "compressor_filter", "enabled": False}],
}
TRANSITIONS = [
    {"name": "Cut", "kind": "cut_transition", "fixed": True, "configurable": False},
    {"name": "Fade", "kind": "fade_transition", "fixed": False, "configurable": False},
    {"name": "Swipe", "kind": "swipe_transition", "fixed": False, "configurable": True},
]
OUTPUTS = [
    {"name": "simple_stream", "kind": "rtmp_output", "flags": {"OBS_OUTPUT_VIDEO": True, "OBS_OUTPUT_AUDIO": True, "OBS_OUTPUT_ENCODED": True, "OBS_OUTPUT_MULTI_TRACK": False, "OBS_OUTPUT_SERVICE": True}},
    {"name": "simple_file_output", "kind": "ffmpeg_muxer", "flags": {"OBS_OUTPUT_VIDEO": True, "OBS_OUTPUT_AUDIO": True, "OBS_OUTPUT_ENCODED": True, "OBS_OUTPUT_MULTI_TRACK": True, "OBS_OUTPUT_SERVICE": False}},
    {"name": "virtualcam_output", "kind": "virtualcam_output", "flags": {"OBS_OUTPUT_VIDEO": True, "OBS_OUTPUT_AUDIO": False, "OBS_OUTPUT_ENCODED": False, "OBS_OUTPUT_MULTI_TRACK": False, "OBS_OUTPUT_SERVICE": False}},
    {"name": "Program NDI", "kind": "ndi_output", "flags": {"OBS_OUTPUT_VIDEO": True, "OBS_OUTPUT_AUDIO": True, "OBS_OUTPUT_ENCODED": False, "OBS_OUTPUT_MULTI_TRACK": False, "OBS_OUTPUT_SERVICE": False}},
]
SCENE_COLLECTIONS = ["Lecture Hall", "Studio"]
PROFILES = ["Default", "Streaming"]
HOTKEYS = ["OBSBasic.StartStreaming", "OBSBasic.StopStreaming", "OBSBasic.StartRecording",
           "OBSBasic.StopRecording", "OBSBasic.PauseRecording", "OBSBasic.Screenshot",
           "OBSBasic.SelectScene", "libobs.mute", "libobs.unmute", "OBSBasic.Transition"]
MONITORS = [{"monitorName": "Built-in Display", "monitorIndex": 0, "monitorWidth": 1920,
             "monitorHeight": 1080, "monitorPositionX": 0, "monitorPositionY": 0}]
RECORD_DIRECTORY = "/Users/av/Movies"
PAUSE_LATENCY_S = 0.25

# Flat simulator state keys behind the fixture's controls, and the input
# each audio control drives.
CONTROL_INPUTS = {"mic_muted": "Mic/Aux", "mic_volume_db": "Mic/Aux",
                  "desktop_muted": "Desktop Audio"}


def _db_to_mul(db: float) -> float:
    return 0.0 if db <= -100 else round(10 ** (db / 20.0), 6)


def _mul_to_db(mul: float) -> float | None:
    return None if mul <= 0 else round(20.0 * math.log10(mul), 2)


def _timecode(ms: int) -> str:
    s, frac = divmod(int(ms), 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{frac:03d}"


class _Client:
    """One control session: where to send, whether it has identified, and
    which event intents it asked for."""

    def __init__(self, sender: Any) -> None:
        self.sender = sender
        self.identified = False
        self.subscriptions = 0
        self.salt = ""
        self.challenge = ""


class ObsStudioSimulator(WebSocketSimulator):
    """A small OBS Studio, as obs-websocket 5 presents it."""

    SIMULATOR_INFO: dict[str, Any] = {
        "driver_id": "obs_studio",
        "name": "OBS Studio Simulator",
        "category": "video",
        "transport": "tcp",
        "default_port": 4455,
        "initial_state": {
            "program_scene": "Camera",
            "preview_scene": "",
            "studio_mode": False,
            "streaming": False,
            "recording": False,
            "record_paused": False,
            "virtual_camera": False,
            "replay_buffer": False,
            "transition": "Fade",
            "transition_duration": 300,
            "scene_collection": "Lecture Hall",
            "profile": "Default",
            "mic_muted": False,
            "mic_volume_db": -6.0,
            "desktop_muted": True,
            "cpu_percent": 4.2,
            "active_fps": 30.0,
        },
        "controls": [
            {"type": "select", "key": "program_scene", "label": "Program Scene",
             "options": list(SCENES)},
            {"type": "toggle", "key": "studio_mode", "label": "Studio Mode"},
            {"type": "toggle", "key": "streaming", "label": "Streaming"},
            {"type": "toggle", "key": "recording", "label": "Recording"},
            {"type": "toggle", "key": "virtual_camera", "label": "Virtual Camera"},
            {"type": "toggle", "key": "mic_muted", "label": "Mic/Aux Muted"},
            {"type": "slider", "key": "mic_volume_db", "label": "Mic/Aux Volume (dB)",
             "min": -100, "max": 26, "step": 0.5},
            {"type": "toggle", "key": "desktop_muted", "label": "Desktop Audio Muted"},
            {"type": "slider", "key": "cpu_percent", "label": "CPU (%)", "min": 0, "max": 100, "step": 0.1},
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "OBS stops answering",
                "behavior": "no_response",
            },
            "authentication_failed": {
                "description": "OBS rejects every password (a wrong Server Password)",
                "behavior": "auth_reject",
            },
            "pause_unavailable": {
                "description": "PauseRecord is acknowledged and nothing pauses (the recording shares the stream encoder)",
                "behavior": "pause_noop",
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._password = str((self.config or {}).get("password", "") or "")
        self._sessions: dict[int, _Client] = {}
        self._applying = False
        self._program = "Camera"
        # Mutable copies of the fixture.
        self.inputs: dict[str, dict[str, Any]] = {}
        for name, spec in INPUTS.items():
            entry = {
                "kind": spec["kind"], "unversioned": spec["unversioned"], "audio": spec["audio"],
                "muted": False, "volume_db": 0.0, "balance": 0.5, "sync_offset": 0,
                "monitor": "OBS_MONITORING_TYPE_NONE",
                "settings": dict(spec.get("settings", {})),
                "media": dict(spec["media"]) if "media" in spec else None,
                "uuid": f"00000000-0000-4000-8000-{abs(hash(name)) % 10**12:012d}",
            }
            self.inputs[name] = entry
        self.inputs["Mic/Aux"]["volume_db"] = -6.0
        self.inputs["Desktop Audio"]["muted"] = True
        self.scenes: list[str] = list(SCENES)  # top first
        self.items: dict[str, list[dict[str, Any]]] = {}
        next_id = 1
        for scene, entries in SCENES.items():
            rows = []
            for position, (source, enabled) in enumerate(reversed(entries)):
                rows.append({"id": next_id, "source": source, "enabled": enabled,
                             "locked": False, "index": position})
                next_id += 1
            self.items[scene] = rows
        self.filters: dict[str, list[dict[str, Any]]] = {
            source: [dict(f) for f in rows] for source, rows in FILTERS.items()}
        self.outputs: dict[str, dict[str, Any]] = {
            o["name"]: {"kind": o["kind"], "flags": o["flags"], "active": False} for o in OUTPUTS}
        self.transition_settings: dict[str, Any] = {}
        self.record_bytes = 0
        self.stream_bytes = 0
        self.last_replay = ""
        self.record_file = ""
        self.custom_events: list[dict[str, Any]] = []
        self.screenshots: list[dict[str, Any]] = []
        self.projectors: list[dict[str, Any]] = []
        self.hotkeys_triggered: list[str] = []
        self.captions: list[str] = []
        self._meter_task: asyncio.Task | None = None
        self._transition_task: asyncio.Task | None = None
        self._output_timers: dict[str, float] = {}
        self.add_change_listener(self._on_state_change)

    # ── Sessions (aiohttp and the in-memory test harness share these) ──

    def ws_open(self, sender: Any) -> _Client:
        """Open a control session whose frames go to ``sender.send(text)``
        and whose close is ``sender.close(code, reason)``. Sends Hello."""
        client = _Client(sender)
        self._sessions[id(client)] = client
        asyncio.ensure_future(self._send_hello(client))
        return client

    def ws_close(self, client: _Client) -> None:
        self._sessions.pop(id(client), None)
        if not any(c.subscriptions & SUB["InputVolumeMeters"] for c in self._sessions.values()):
            self._stop_meters()

    async def ws_message(self, client: _Client, text: str) -> None:
        try:
            msg = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        op = msg.get("op")
        data = msg.get("d") or {}
        if not client.identified:
            if op != OP_IDENTIFY:
                await self._close(client, CLOSE_NOT_IDENTIFIED, "Not identified.")
                return
            await self._identify(client, data)
            return
        if op == OP_REIDENTIFY:
            client.subscriptions = int(data.get("eventSubscriptions", client.subscriptions))
            self._sync_meters()
            await client.sender.send(json.dumps({"op": OP_IDENTIFIED, "d": {"negotiatedRpcVersion": RPC_VERSION}}))
        elif op == OP_REQUEST:
            await client.sender.send(json.dumps({
                "op": OP_REQUEST_RESPONSE, "d": await self._run_request(data)}))
        elif op == OP_REQUEST_BATCH:
            results = []
            halt = bool(data.get("haltOnFailure", False))
            for entry in data.get("requests") or []:
                result = await self._run_request(entry)
                results.append(result)
                if halt and not result["requestStatus"]["result"]:
                    break
            await client.sender.send(json.dumps({
                "op": OP_REQUEST_BATCH_RESPONSE,
                "d": {"requestId": data.get("requestId", ""), "results": results}}))

    async def _send_hello(self, client: _Client) -> None:
        hello: dict[str, Any] = {
            "obsStudioVersion": OBS_VERSION, "obsWebSocketVersion": WS_VERSION, "rpcVersion": RPC_VERSION}
        if self._password or self.has_error_behavior("auth_reject"):
            client.salt = base64.b64encode(os.urandom(32)).decode("ascii")
            client.challenge = base64.b64encode(os.urandom(32)).decode("ascii")
            hello["authentication"] = {"challenge": client.challenge, "salt": client.salt}
        await client.sender.send(json.dumps({"op": OP_HELLO, "d": hello}))

    async def _identify(self, client: _Client, data: dict[str, Any]) -> None:
        if int(data.get("rpcVersion", 0)) != RPC_VERSION:
            await self._close(client, CLOSE_UNSUPPORTED_RPC_VERSION, "Unsupported RPC version.")
            return
        if self.has_error_behavior("auth_reject"):
            await self._close(client, CLOSE_AUTHENTICATION_FAILED, "Authentication failed.")
            return
        if self._password:
            secret = base64.b64encode(hashlib.sha256(
                (self._password + client.salt).encode("utf-8")).digest()).decode("ascii")
            expected = base64.b64encode(hashlib.sha256(
                (secret + client.challenge).encode("utf-8")).digest()).decode("ascii")
            if data.get("authentication") != expected:
                await self._close(client, CLOSE_AUTHENTICATION_FAILED, "Authentication failed.")
                return
        client.subscriptions = int(data.get("eventSubscriptions", 2047))
        client.identified = True
        await client.sender.send(json.dumps({"op": OP_IDENTIFIED, "d": {"negotiatedRpcVersion": RPC_VERSION}}))
        self._sync_meters()

    async def _close(self, client: _Client, code: int, reason: str) -> None:
        self._sessions.pop(id(client), None)
        try:
            await client.sender.close(code, reason)
        except Exception:
            pass

    # aiohttp plumbing: the platform base calls these.

    async def on_client_connect(self, client) -> None:
        setattr(client, "_obs_session", self.ws_open(_AiohttpSender(client)))

    async def on_client_disconnect(self, client) -> None:
        session = getattr(client, "_obs_session", None)
        if session is not None:
            self.ws_close(session)

    async def handle_message(self, client, message: str) -> None:
        session = getattr(client, "_obs_session", None)
        if session is not None:
            await self.ws_message(session, message)

    # ── Events ──

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        intent_name = EVENT_INTENT.get(event_type, "General")
        intent = SUB[intent_name]
        frame = json.dumps({"op": OP_EVENT, "d": {
            "eventType": event_type, "eventIntent": intent, "eventData": data or {}}})
        for client in list(self._sessions.values()):
            if client.identified and client.subscriptions & intent:
                try:
                    await client.sender.send(frame)
                except Exception:
                    pass

    def _emit_later(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        asyncio.ensure_future(self.emit(event_type, data))

    # ── State mutation paths (requests and the UI both land here) ──

    def _scene_uuid(self, name: str) -> str:
        return f"11111111-0000-4000-8000-{abs(hash('scene:' + name)) % 10**12:012d}"

    def _input_uuid(self, name: str) -> str:
        return self.inputs[name]["uuid"]

    def _sources_in(self, scene: str) -> set[str]:
        return {row["source"] for row in self.items.get(scene, []) if row["enabled"]}

    async def apply_program_scene(self, name: str) -> None:
        old = self._program
        if old == name:
            self._set("program_scene", name)
            return
        before = self._sources_in(old) if old else set()
        self._program = name
        self._set("program_scene", name)
        duration = int(self.get_state("transition_duration", 300))
        transition = self.get_state("transition", "Fade")
        fixed = next((t["fixed"] for t in TRANSITIONS if t["name"] == transition), False)
        payload = {"transitionName": transition, "transitionUuid": self._scene_uuid("t:" + transition)}
        await self.emit("SceneTransitionStarted", payload)
        await self.emit("CurrentProgramSceneChanged", {"sceneName": name, "sceneUuid": self._scene_uuid(name)})
        after = self._sources_in(name)
        for source in sorted(before ^ after):
            if source in self.inputs:
                await self.emit("InputActiveStateChanged", {
                    "inputName": source, "inputUuid": self._input_uuid(source),
                    "videoActive": source in after})
        if fixed or duration <= 0:
            await self.emit("SceneTransitionEnded", payload)
        else:
            if self._transition_task and not self._transition_task.done():
                self._transition_task.cancel()
            self._transition_task = asyncio.ensure_future(self._end_transition(payload, duration / 1000.0))

    async def _end_transition(self, payload: dict[str, Any], delay: float) -> None:
        await asyncio.sleep(delay)
        await self.emit("SceneTransitionEnded", payload)

    async def apply_preview_scene(self, name: str) -> None:
        self._set("preview_scene", name)
        await self.emit("CurrentPreviewSceneChanged", {"sceneName": name, "sceneUuid": self._scene_uuid(name)})

    async def apply_studio_mode(self, enabled: bool) -> None:
        self._set("studio_mode", enabled)
        await self.emit("StudioModeStateChanged", {"studioModeEnabled": enabled})
        if enabled:
            await self.apply_preview_scene(self.get_state("program_scene"))
        else:
            self._set("preview_scene", "")

    async def apply_output(self, key: str, active: bool) -> None:
        """Stream, record, virtual camera and replay buffer share one shape:
        a transient state event then the settled one."""
        event = {"streaming": "StreamStateChanged", "recording": "RecordStateChanged",
                 "virtual_camera": "VirtualcamStateChanged", "replay_buffer": "ReplayBufferStateChanged"}[key]
        output = {"streaming": "simple_stream", "recording": "simple_file_output",
                  "virtual_camera": "virtualcam_output"}.get(key)
        if bool(self.get_state(key)) == active:
            return
        self._set(key, active)
        if output:
            self.outputs[output]["active"] = active
        loop = asyncio.get_event_loop()
        if active:
            self._output_timers[key] = loop.time()
            await self.emit(event, {"outputActive": False, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTING"})
            await self.emit(event, {"outputActive": True, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"})
            if key == "recording":
                self._set("record_paused", False)
                self.record_file = f"{RECORD_DIRECTORY}/2026-09-08 10-00-00.mkv"
        else:
            self._output_timers.pop(key, None)
            # OBS reports the STOPPING event with outputActive already false,
            # though a Start is still refused until STOPPED.
            await self.emit(event, {"outputActive": False, "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPING"})
            data = {"outputActive": False, "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED"}
            if key == "recording":
                self._set("record_paused", False)
                data["outputPath"] = self.record_file
            await self.emit(event, data)

    async def apply_record_paused(self, paused: bool) -> None:
        if paused:
            # OBS acknowledges PauseRecord at once and pauses at the next
            # keyframe; a ResumeRecord before that is refused as not paused.
            await asyncio.sleep(PAUSE_LATENCY_S)
            if not self.get_state("recording"):
                return
        self._set("record_paused", paused)
        await self.emit("RecordStateChanged", {
            "outputActive": True,
            "outputState": "OBS_WEBSOCKET_OUTPUT_PAUSED" if paused else "OBS_WEBSOCKET_OUTPUT_RESUMED"})

    async def apply_mute(self, name: str, muted: bool) -> None:
        entry = self.inputs[name]
        entry["muted"] = muted
        for key, target in CONTROL_INPUTS.items():
            if target == name and key.endswith("_muted"):
                self._set(key, muted)
        await self.emit("InputMuteStateChanged", {
            "inputName": name, "inputUuid": entry["uuid"], "inputMuted": muted})

    async def apply_volume(self, name: str, db: float) -> None:
        entry = self.inputs[name]
        entry["volume_db"] = max(-100.0, min(26.0, float(db)))
        for key, target in CONTROL_INPUTS.items():
            if target == name and key.endswith("_volume_db"):
                self._set(key, entry["volume_db"])
        await self.emit("InputVolumeChanged", {
            "inputName": name, "inputUuid": entry["uuid"],
            "inputVolumeMul": _db_to_mul(entry["volume_db"]),
            "inputVolumeDb": _mul_to_db(_db_to_mul(entry["volume_db"]))})

    async def apply_transition(self, name: str) -> None:
        self._set("transition", name)
        await self.emit("CurrentSceneTransitionChanged", {
            "transitionName": name, "transitionUuid": self._scene_uuid("t:" + name)})

    async def apply_transition_duration(self, ms: int) -> None:
        self._set("transition_duration", int(ms))
        await self.emit("CurrentSceneTransitionDurationChanged", {"transitionDuration": int(ms)})

    async def apply_item_enabled(self, scene: str, item_id: int, enabled: bool) -> None:
        for row in self.items.get(scene, []):
            if row["id"] == item_id:
                row["enabled"] = enabled
        await self.emit("SceneItemEnableStateChanged", {
            "sceneName": scene, "sceneUuid": self._scene_uuid(scene),
            "sceneItemId": item_id, "sceneItemEnabled": enabled})
        if scene == self.get_state("program_scene"):
            source = next((r["source"] for r in self.items[scene] if r["id"] == item_id), None)
            if source in self.inputs:
                await self.emit("InputActiveStateChanged", {
                    "inputName": source, "inputUuid": self._input_uuid(source), "videoActive": enabled})

    async def apply_filter_enabled(self, source: str, filter_name: str, enabled: bool) -> None:
        for row in self.filters.get(source, []):
            if row["name"] == filter_name:
                row["enabled"] = enabled
        await self.emit("SourceFilterEnableStateChanged", {
            "sourceName": source, "filterName": filter_name, "filterEnabled": enabled})

    async def apply_media(self, name: str, state: str, cursor: int | None = None) -> None:
        media = self.inputs[name]["media"]
        media["state"] = state
        if cursor is not None:
            media["cursor"] = max(0, min(int(media["duration"]), int(cursor)))
        if state == "OBS_MEDIA_STATE_STOPPED":
            media["cursor"] = 0

    async def apply_text(self, name: str, text: str) -> None:
        entry = self.inputs[name]
        entry["settings"]["text"] = text
        await self.emit("InputSettingsChanged", {
            "inputName": name, "inputUuid": entry["uuid"], "inputSettings": dict(entry["settings"])})

    async def apply_scene_collection(self, name: str) -> None:
        await self.emit("CurrentSceneCollectionChanging", {"sceneCollectionName": self.get_state("scene_collection")})
        self._set("scene_collection", name)
        await self.emit("CurrentSceneCollectionChanged", {"sceneCollectionName": name})

    async def apply_profile(self, name: str) -> None:
        self._set("profile", name)
        await self.emit("CurrentProfileChanged", {"profileName": name})

    async def rename_input(self, old: str, new: str) -> None:
        """A rename made in OBS: the input, its scene items and its filters
        follow the new name; the events OBS fires are InputNameChanged only."""
        entry = self.inputs.pop(old)
        self.inputs[new] = entry
        for rows in self.items.values():
            for row in rows:
                if row["source"] == old:
                    row["source"] = new
        if old in self.filters:
            self.filters[new] = self.filters.pop(old)
        await self.emit("InputNameChanged", {"inputUuid": entry["uuid"], "oldInputName": old, "inputName": new})

    async def add_scene(self, name: str) -> None:
        self.scenes.insert(0, name)
        self.items[name] = []
        await self.emit("SceneCreated", {"sceneName": name, "sceneUuid": self._scene_uuid(name), "isGroup": False})
        await self.emit("SceneListChanged", {"scenes": self._scene_list()})

    async def remove_scene(self, name: str) -> None:
        self.scenes.remove(name)
        self.items.pop(name, None)
        await self.emit("SceneRemoved", {"sceneName": name, "sceneUuid": self._scene_uuid(name), "isGroup": False})
        await self.emit("SceneListChanged", {"scenes": self._scene_list()})

    def _set(self, key: str, value: Any) -> None:
        self._applying = True
        try:
            self.set_state(key, value)
        finally:
            self._applying = False

    # The Simulator UI moved a control: run the same path a request would.
    def _on_state_change(self, change_type: str, data: dict) -> None:
        if change_type != "state" or self._applying:
            return
        key = data.get("key")
        value = data.get("value")
        if key == "program_scene" and value in self.scenes:
            # set_state already stored it; re-run the event path from the old value.
            asyncio.ensure_future(self._ui_program_scene(str(value)))
        elif key == "studio_mode":
            asyncio.ensure_future(self.apply_studio_mode(bool(value)))
        elif key in ("streaming", "recording", "virtual_camera", "replay_buffer"):
            asyncio.ensure_future(self._ui_output(key, bool(value)))
        elif key in CONTROL_INPUTS and key.endswith("_muted"):
            asyncio.ensure_future(self.apply_mute(CONTROL_INPUTS[key], bool(value)))
        elif key in CONTROL_INPUTS and key.endswith("_volume_db"):
            asyncio.ensure_future(self.apply_volume(CONTROL_INPUTS[key], float(value)))

    async def _ui_program_scene(self, name: str) -> None:
        # The UI already wrote the new value; put the old one back silently
        # so apply_program_scene sees the change and fires the events.
        self._set("program_scene", self._program)
        await self.apply_program_scene(name)

    async def _ui_output(self, key: str, active: bool) -> None:
        # The UI already flipped the flat key; flip it back silently so
        # apply_output sees the change and fires the events.
        self._set(key, not active)
        await self.apply_output(key, active)

    # ── Meters ──

    def _sync_meters(self) -> None:
        wanted = any(c.identified and c.subscriptions & SUB["InputVolumeMeters"]
                     for c in self._sessions.values())
        if wanted and (self._meter_task is None or self._meter_task.done()):
            self._meter_task = asyncio.ensure_future(self._meter_loop())
        elif not wanted:
            self._stop_meters()

    def _stop_meters(self) -> None:
        if self._meter_task and not self._meter_task.done():
            self._meter_task.cancel()
        self._meter_task = None

    async def _meter_loop(self) -> None:
        tick = 0
        try:
            while True:
                await asyncio.sleep(0.1)
                tick += 1
                inputs = []
                for name, entry in self.inputs.items():
                    if not entry["audio"] or entry["muted"]:
                        continue
                    peak = 0.25 + 0.2 * math.sin(tick / 3.0)
                    inputs.append({"inputName": name, "inputUuid": entry["uuid"],
                                   "inputLevelsMul": [[peak * 0.8, peak, peak * 1.05]]})
                await self.emit("InputVolumeMeters", {"inputs": inputs})
        except asyncio.CancelledError:
            return

    # ── Requests ──

    async def _run_request(self, entry: dict[str, Any]) -> dict[str, Any]:
        rtype = str(entry.get("requestType", ""))
        rid = entry.get("requestId", "")
        data = entry.get("requestData") or {}
        handler = getattr(self, f"_rq_{rtype}", None)
        base = {"requestType": rtype, "requestId": rid}
        if handler is None:
            base["requestStatus"] = {"result": False, "code": UNKNOWN_REQUEST_TYPE}
            return base
        try:
            result = await handler(data)
        except _Fail as fail:
            base["requestStatus"] = {"result": False, "code": fail.code}
            if fail.comment:
                base["requestStatus"]["comment"] = fail.comment
            return base
        base["requestStatus"] = {"result": True, "code": OK}
        if result is not None:
            base["responseData"] = result
        return base

    # Lookups

    def _input(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        name = data.get("inputName")
        if name is None and data.get("inputUuid"):
            for candidate, entry in self.inputs.items():
                if entry["uuid"] == data["inputUuid"]:
                    name = candidate
        if name is None:
            raise _Fail(MISSING_REQUEST_FIELD, "Your request is missing the `inputName` field.")
        entry = self.inputs.get(str(name))
        if entry is None:
            raise _Fail(RESOURCE_NOT_FOUND, "No input was found by the name of `%s`." % name)
        return str(name), entry

    def _audio_input(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        name, entry = self._input(data)
        if not entry["audio"]:
            raise _Fail(INVALID_RESOURCE_STATE, "The specified input does not support audio.")
        return name, entry

    def _media_input(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        name, entry = self._input(data)
        if entry["media"] is None:
            raise _Fail(INVALID_RESOURCE_STATE, "The specified input is not a media input.")
        return name, entry

    def _scene(self, data: dict[str, Any], key: str = "sceneName") -> str:
        name = data.get(key)
        if name is None:
            raise _Fail(MISSING_REQUEST_FIELD, f"Your request is missing the `{key}` field.")
        if name not in self.scenes:
            raise _Fail(RESOURCE_NOT_FOUND, "No source was found by the name of `%s`." % name)
        return str(name)

    def _source(self, data: dict[str, Any], key: str = "sourceName") -> str:
        name = data.get(key)
        if name is None:
            raise _Fail(MISSING_REQUEST_FIELD, f"Your request is missing the `{key}` field.")
        if name not in self.scenes and name not in self.inputs:
            raise _Fail(RESOURCE_NOT_FOUND, "No source was found by the name of `%s`." % name)
        return str(name)

    def _need(self, data: dict[str, Any], key: str) -> Any:
        if key not in data:
            raise _Fail(MISSING_REQUEST_FIELD, f"Your request is missing the `{key}` field.")
        return data[key]

    def _scene_list(self) -> list[dict[str, Any]]:
        count = len(self.scenes)
        return [{"sceneName": name, "sceneUuid": self._scene_uuid(name), "sceneIndex": count - 1 - pos}
                for pos, name in enumerate(self.scenes)]

    def _output_status(self, key: str) -> dict[str, Any]:
        active = bool(self.get_state(key))
        started = self._output_timers.get(key)
        duration = int((asyncio.get_event_loop().time() - started) * 1000) if (active and started) else 0
        return {"outputActive": active, "outputReconnecting": False,
                "outputTimecode": _timecode(duration), "outputDuration": duration,
                "outputCongestion": 0.0, "outputBytes": duration * 625,
                "outputSkippedFrames": 0, "outputTotalFrames": int(duration * 0.03)}

    # General

    async def _rq_GetVersion(self, data):
        return {"obsVersion": OBS_VERSION, "obsWebSocketVersion": WS_VERSION, "rpcVersion": RPC_VERSION,
                "availableRequests": [n[4:] for n in dir(self) if n.startswith("_rq_")],
                "supportedImageFormats": ["bmp", "jpeg", "jpg", "png", "tif", "tiff"],
                "platform": "macos", "platformDescription": "macOS 15.5"}

    async def _rq_GetStats(self, data):
        return {"cpuUsage": float(self.get_state("cpu_percent", 4.2)), "memoryUsage": 812.4,
                "availableDiskSpace": 245760.0, "activeFps": float(self.get_state("active_fps", 30.0)),
                "averageFrameRenderTime": 1.234, "renderSkippedFrames": 3, "renderTotalFrames": 180000,
                "outputSkippedFrames": 0, "outputTotalFrames": 90000,
                "webSocketSessionIncomingMessages": 10, "webSocketSessionOutgoingMessages": 12}

    async def _rq_BroadcastCustomEvent(self, data):
        payload = self._need(data, "eventData")
        self.custom_events.append(payload)
        await self.emit("CustomEvent", payload)

    async def _rq_GetHotkeyList(self, data):
        return {"hotkeys": list(HOTKEYS)}

    async def _rq_TriggerHotkeyByName(self, data):
        name = self._need(data, "hotkeyName")
        if name not in HOTKEYS:
            raise _Fail(RESOURCE_NOT_FOUND, "No hotkeys were found by that name.")
        self.hotkeys_triggered.append(str(name))

    async def _rq_TriggerHotkeyByKeySequence(self, data):
        self.hotkeys_triggered.append(str(data.get("keyId", "")))

    # Config

    async def _rq_GetSceneCollectionList(self, data):
        return {"currentSceneCollectionName": self.get_state("scene_collection"),
                "sceneCollections": list(SCENE_COLLECTIONS)}

    async def _rq_SetCurrentSceneCollection(self, data):
        name = self._need(data, "sceneCollectionName")
        if name not in SCENE_COLLECTIONS:
            raise _Fail(RESOURCE_NOT_FOUND, "No scene collection was found by that name.")
        if name != self.get_state("scene_collection"):
            await self.apply_scene_collection(str(name))

    async def _rq_GetProfileList(self, data):
        return {"currentProfileName": self.get_state("profile"), "profiles": list(PROFILES)}

    async def _rq_SetCurrentProfile(self, data):
        name = self._need(data, "profileName")
        if name not in PROFILES:
            raise _Fail(RESOURCE_NOT_FOUND, "No profile was found by that name.")
        if self.get_state("streaming") or self.get_state("recording"):
            raise _Fail(OUTPUT_RUNNING, "The profile cannot be changed while an output is active.")
        if name != self.get_state("profile"):
            await self.apply_profile(str(name))

    async def _rq_GetVideoSettings(self, data):
        return {"fpsNumerator": 30, "fpsDenominator": 1, "baseWidth": 1920, "baseHeight": 1080,
                "outputWidth": 1280, "outputHeight": 720}

    async def _rq_GetRecordDirectory(self, data):
        return {"recordDirectory": RECORD_DIRECTORY}

    # Scenes

    async def _rq_GetSceneList(self, data):
        program = self.get_state("program_scene")
        preview = self.get_state("preview_scene") if self.get_state("studio_mode") else None
        return {"currentProgramSceneName": program, "currentProgramSceneUuid": self._scene_uuid(program),
                "currentPreviewSceneName": preview,
                "currentPreviewSceneUuid": self._scene_uuid(preview) if preview else None,
                "scenes": self._scene_list()}

    async def _rq_GetGroupList(self, data):
        return {"groups": []}

    async def _rq_GetCurrentProgramScene(self, data):
        name = self.get_state("program_scene")
        return {"sceneName": name, "sceneUuid": self._scene_uuid(name),
                "currentProgramSceneName": name, "currentProgramSceneUuid": self._scene_uuid(name)}

    async def _rq_SetCurrentProgramScene(self, data):
        await self.apply_program_scene(self._scene(data))

    async def _rq_GetCurrentPreviewScene(self, data):
        if not self.get_state("studio_mode"):
            raise _Fail(STUDIO_MODE_NOT_ACTIVE, "Studio mode is not active.")
        name = self.get_state("preview_scene")
        return {"sceneName": name, "sceneUuid": self._scene_uuid(name),
                "currentPreviewSceneName": name, "currentPreviewSceneUuid": self._scene_uuid(name)}

    async def _rq_SetCurrentPreviewScene(self, data):
        name = self._scene(data)
        if not self.get_state("studio_mode"):
            raise _Fail(STUDIO_MODE_NOT_ACTIVE, "Studio mode is not active.")
        await self.apply_preview_scene(name)

    # Scene items

    async def _rq_GetSceneItemList(self, data):
        scene = self._scene(data)
        rows = []
        for row in self.items[scene]:
            source = row["source"]
            entry = self.inputs.get(source)
            rows.append({
                "sceneItemId": row["id"], "sceneItemIndex": row["index"],
                "sourceName": source,
                "sourceUuid": entry["uuid"] if entry else self._scene_uuid(source),
                "sourceType": "OBS_SOURCE_TYPE_INPUT" if entry else "OBS_SOURCE_TYPE_SCENE",
                "inputKind": entry["kind"] if entry else None, "isGroup": None if entry else False,
                "sceneItemEnabled": row["enabled"], "sceneItemLocked": row["locked"],
                "sceneItemBlendMode": "OBS_BLEND_NORMAL",
                "sceneItemTransform": {"positionX": 0.0, "positionY": 0.0, "scaleX": 1.0, "scaleY": 1.0,
                                       "rotation": 0.0, "width": 1920.0, "height": 1080.0},
            })
        return {"sceneItems": rows}

    def _item(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        scene = self._scene(data)
        item_id = self._need(data, "sceneItemId")
        for row in self.items[scene]:
            if row["id"] == item_id:
                return scene, row
        raise _Fail(RESOURCE_NOT_FOUND, "No scene items were found in the specified scene by that id or offset.")

    async def _rq_GetSceneItemId(self, data):
        scene = self._scene(data)
        source = self._need(data, "sourceName")
        for row in self.items[scene]:
            if row["source"] == source:
                return {"sceneItemId": row["id"]}
        raise _Fail(RESOURCE_NOT_FOUND, "No scene items were found in the specified scene by that name or offset.")

    async def _rq_GetSceneItemEnabled(self, data):
        _scene, row = self._item(data)
        return {"sceneItemEnabled": row["enabled"]}

    async def _rq_SetSceneItemEnabled(self, data):
        scene, row = self._item(data)
        enabled = bool(self._need(data, "sceneItemEnabled"))
        if row["enabled"] != enabled:
            await self.apply_item_enabled(scene, row["id"], enabled)

    # Inputs

    async def _rq_GetInputList(self, data):
        kind = data.get("inputKind")
        return {"inputs": [
            {"inputName": name, "inputUuid": entry["uuid"], "inputKind": entry["kind"],
             "unversionedInputKind": entry["unversioned"]}
            for name, entry in self.inputs.items() if kind is None or entry["kind"] == kind]}

    async def _rq_GetSpecialInputs(self, data):
        return {"desktop1": "Desktop Audio", "desktop2": None, "mic1": "Mic/Aux",
                "mic2": None, "mic3": None, "mic4": None}

    async def _rq_GetInputSettings(self, data):
        _name, entry = self._input(data)
        return {"inputSettings": dict(entry["settings"]), "inputKind": entry["kind"]}

    async def _rq_SetInputSettings(self, data):
        name, entry = self._input(data)
        settings = self._need(data, "inputSettings")
        if not isinstance(settings, dict):
            raise _Fail(INVALID_REQUEST_FIELD, "The field `inputSettings` must be an object.")
        if not bool(data.get("overlay", True)):
            entry["settings"] = {}
        if "text" in settings and entry["unversioned"] in TEXT_KINDS:
            await self.apply_text(name, str(settings["text"]))
        else:
            entry["settings"].update(settings)
            await self.emit("InputSettingsChanged", {
                "inputName": name, "inputUuid": entry["uuid"], "inputSettings": dict(entry["settings"])})

    async def _rq_GetInputMute(self, data):
        _name, entry = self._audio_input(data)
        return {"inputMuted": entry["muted"]}

    async def _rq_SetInputMute(self, data):
        name, entry = self._audio_input(data)
        muted = bool(self._need(data, "inputMuted"))
        if entry["muted"] != muted:
            await self.apply_mute(name, muted)

    async def _rq_ToggleInputMute(self, data):
        name, entry = self._audio_input(data)
        await self.apply_mute(name, not entry["muted"])
        return {"inputMuted": entry["muted"]}

    async def _rq_GetInputVolume(self, data):
        _name, entry = self._audio_input(data)
        mul = _db_to_mul(entry["volume_db"])
        return {"inputVolumeMul": mul, "inputVolumeDb": _mul_to_db(mul)}

    async def _rq_SetInputVolume(self, data):
        name, _entry = self._audio_input(data)
        if "inputVolumeDb" in data:
            db = float(data["inputVolumeDb"])
            if db < -100 or db > 26:
                raise _Fail(REQUEST_FIELD_OUT_OF_RANGE, "The field `inputVolumeDb` is out of range (-100 to 26).")
        elif "inputVolumeMul" in data:
            mul = float(data["inputVolumeMul"])
            if mul < 0 or mul > 20:
                raise _Fail(REQUEST_FIELD_OUT_OF_RANGE, "The field `inputVolumeMul` is out of range (0 to 20).")
            db = _mul_to_db(mul)
            db = -100.0 if db is None else db
        else:
            raise _Fail(MISSING_REQUEST_FIELD, "Your request must contain either `inputVolumeMul` or `inputVolumeDb`.")
        await self.apply_volume(name, db)

    async def _rq_GetInputAudioBalance(self, data):
        _name, entry = self._audio_input(data)
        return {"inputAudioBalance": entry["balance"]}

    async def _rq_SetInputAudioBalance(self, data):
        name, entry = self._audio_input(data)
        balance = float(self._need(data, "inputAudioBalance"))
        if balance < 0 or balance > 1:
            raise _Fail(REQUEST_FIELD_OUT_OF_RANGE, "The field `inputAudioBalance` is out of range (0 to 1).")
        entry["balance"] = balance
        await self.emit("InputAudioBalanceChanged", {"inputName": name, "inputUuid": entry["uuid"], "inputAudioBalance": balance})

    async def _rq_GetInputAudioSyncOffset(self, data):
        _name, entry = self._audio_input(data)
        return {"inputAudioSyncOffset": entry["sync_offset"]}

    async def _rq_SetInputAudioSyncOffset(self, data):
        name, entry = self._audio_input(data)
        offset = int(self._need(data, "inputAudioSyncOffset"))
        if offset < -950 or offset > 20000:
            raise _Fail(REQUEST_FIELD_OUT_OF_RANGE, "The field `inputAudioSyncOffset` is out of range (-950 to 20000).")
        entry["sync_offset"] = offset
        await self.emit("InputAudioSyncOffsetChanged", {"inputName": name, "inputUuid": entry["uuid"], "inputAudioSyncOffset": offset})

    async def _rq_GetInputAudioMonitorType(self, data):
        _name, entry = self._audio_input(data)
        return {"monitorType": entry["monitor"]}

    async def _rq_SetInputAudioMonitorType(self, data):
        name, entry = self._audio_input(data)
        monitor = str(self._need(data, "monitorType"))
        if monitor not in ("OBS_MONITORING_TYPE_NONE", "OBS_MONITORING_TYPE_MONITOR_ONLY", "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"):
            raise _Fail(INVALID_REQUEST_FIELD, "The field `monitorType` has an invalid value.")
        entry["monitor"] = monitor
        await self.emit("InputAudioMonitorTypeChanged", {"inputName": name, "inputUuid": entry["uuid"], "monitorType": monitor})

    async def _rq_PressInputPropertiesButton(self, data):
        name, entry = self._input(data)
        prop = self._need(data, "propertyName")
        if entry["unversioned"] not in ("browser_source",) or prop != "refreshnocache":
            raise _Fail(RESOURCE_NOT_FOUND, "Unable to find a property by that name.")
        entry.setdefault("refreshes", 0)
        entry["refreshes"] += 1

    # Media inputs

    async def _rq_GetMediaInputStatus(self, data):
        _name, entry = self._media_input(data)
        media = entry["media"]
        playing = media["state"] in ("OBS_MEDIA_STATE_PLAYING", "OBS_MEDIA_STATE_PAUSED")
        return {"mediaState": media["state"],
                "mediaDuration": media["duration"] if playing else None,
                "mediaCursor": media["cursor"] if playing else None}

    async def _rq_TriggerMediaInputAction(self, data):
        name, entry = self._media_input(data)
        action = str(self._need(data, "mediaAction"))
        if not action.startswith("OBS_WEBSOCKET_MEDIA_INPUT_ACTION_"):
            raise _Fail(INVALID_REQUEST_FIELD, "The field `mediaAction` has an invalid value.")
        verb = action[len("OBS_WEBSOCKET_MEDIA_INPUT_ACTION_"):]
        media = entry["media"]
        if verb == "PLAY":
            await self.apply_media(name, "OBS_MEDIA_STATE_PLAYING")
            await self.emit("MediaInputPlaybackStarted", {"inputName": name, "inputUuid": entry["uuid"]})
        elif verb == "PAUSE":
            await self.apply_media(name, "OBS_MEDIA_STATE_PAUSED")
        elif verb == "STOP":
            await self.apply_media(name, "OBS_MEDIA_STATE_STOPPED")
            await self.emit("MediaInputPlaybackEnded", {"inputName": name, "inputUuid": entry["uuid"]})
        elif verb == "RESTART":
            await self.apply_media(name, "OBS_MEDIA_STATE_PLAYING", cursor=0)
            await self.emit("MediaInputPlaybackStarted", {"inputName": name, "inputUuid": entry["uuid"]})
        elif verb in ("NEXT", "PREVIOUS", "NONE"):
            media["cursor"] = 0
        else:
            raise _Fail(INVALID_REQUEST_FIELD, "The field `mediaAction` has an invalid value.")
        await self.emit("MediaInputActionTriggered", {"inputName": name, "inputUuid": entry["uuid"], "mediaAction": action})

    async def _rq_SetMediaInputCursor(self, data):
        name, entry = self._media_input(data)
        await self.apply_media(name, entry["media"]["state"], cursor=int(self._need(data, "mediaCursor")))

    async def _rq_OffsetMediaInputCursor(self, data):
        name, entry = self._media_input(data)
        offset = int(self._need(data, "mediaCursorOffset"))
        await self.apply_media(name, entry["media"]["state"], cursor=entry["media"]["cursor"] + offset)

    # Sources, filters

    async def _rq_GetSourceActive(self, data):
        source = self._source(data)
        program = self.get_state("program_scene")
        active = source == program or source in self._sources_in(program)
        preview = self.get_state("preview_scene") if self.get_state("studio_mode") else ""
        showing = active or (bool(preview) and (source == preview or source in self._sources_in(preview)))
        return {"videoActive": active, "videoShowing": showing}

    async def _rq_GetSourceFilterList(self, data):
        source = self._source(data)
        return {"filters": [
            {"filterName": row["name"], "filterKind": row["kind"], "filterIndex": index,
             "filterEnabled": row["enabled"], "filterSettings": {}}
            for index, row in enumerate(self.filters.get(source, []))]}

    async def _rq_SetSourceFilterEnabled(self, data):
        source = self._source(data)
        filter_name = self._need(data, "filterName")
        enabled = bool(self._need(data, "filterEnabled"))
        for row in self.filters.get(source, []):
            if row["name"] == filter_name:
                if row["enabled"] != enabled:
                    await self.apply_filter_enabled(source, str(filter_name), enabled)
                return
        raise _Fail(RESOURCE_NOT_FOUND, "No filter was found by the name of `%s`." % filter_name)

    async def _rq_SaveSourceScreenshot(self, data):
        source = self._source(data)
        fmt = self._need(data, "imageFormat")
        path = self._need(data, "imageFilePath")
        if fmt not in ("bmp", "jpeg", "jpg", "png", "tif", "tiff"):
            raise _Fail(INVALID_REQUEST_FIELD, "Your specified image format is invalid or not supported by this system.")
        self.screenshots.append({"source": source, "format": fmt, "path": path,
                                 "width": data.get("imageWidth"), "height": data.get("imageHeight")})
        return {"imageData": ""}

    # Transitions

    async def _rq_GetSceneTransitionList(self, data):
        current = self.get_state("transition")
        kind = next((t["kind"] for t in TRANSITIONS if t["name"] == current), None)
        return {"currentSceneTransitionName": current, "currentSceneTransitionUuid": self._scene_uuid("t:" + current),
                "currentSceneTransitionKind": kind,
                "transitions": [{"transitionName": t["name"], "transitionUuid": self._scene_uuid("t:" + t["name"]),
                                 "transitionKind": t["kind"], "transitionFixed": t["fixed"],
                                 "transitionConfigurable": t["configurable"]} for t in TRANSITIONS]}

    async def _rq_GetCurrentSceneTransition(self, data):
        current = self.get_state("transition")
        spec = next(t for t in TRANSITIONS if t["name"] == current)
        return {"transitionName": current, "transitionUuid": self._scene_uuid("t:" + current),
                "transitionKind": spec["kind"], "transitionFixed": spec["fixed"],
                "transitionDuration": None if spec["fixed"] else int(self.get_state("transition_duration", 300)),
                "transitionConfigurable": spec["configurable"],
                "transitionSettings": dict(self.transition_settings) if spec["configurable"] else None}

    async def _rq_SetCurrentSceneTransition(self, data):
        name = self._need(data, "transitionName")
        if not any(t["name"] == name for t in TRANSITIONS):
            raise _Fail(RESOURCE_NOT_FOUND, "No scene transition was found by that name.")
        if name != self.get_state("transition"):
            await self.apply_transition(str(name))

    async def _rq_SetCurrentSceneTransitionDuration(self, data):
        ms = int(self._need(data, "transitionDuration"))
        if ms < 50 or ms > 20000:
            raise _Fail(REQUEST_FIELD_OUT_OF_RANGE, "The field `transitionDuration` is out of range (50 to 20000).")
        # OBS accepts this on a fixed transition too (Cut): the value is the
        # global duration, kept for the next transition that uses one.
        await self.apply_transition_duration(ms)

    async def _rq_TriggerStudioModeTransition(self, data):
        if not self.get_state("studio_mode"):
            raise _Fail(STUDIO_MODE_NOT_ACTIVE, "Studio mode is not active.")
        preview = self.get_state("preview_scene")
        program = self.get_state("program_scene")
        await self.apply_program_scene(preview)
        await self.apply_preview_scene(program)

    async def _rq_SetTBarPosition(self, data):
        if not self.get_state("studio_mode"):
            raise _Fail(STUDIO_MODE_NOT_ACTIVE, "Studio mode is not active.")
        position = float(self._need(data, "position"))
        if position < 0 or position > 1:
            raise _Fail(REQUEST_FIELD_OUT_OF_RANGE, "The field `position` is out of range (0 to 1).")
        self.set_state("tbar_position", position)
        if position >= 1.0 and bool(data.get("release", True)):
            await self._rq_TriggerStudioModeTransition({})

    # UI

    async def _rq_GetStudioModeEnabled(self, data):
        return {"studioModeEnabled": bool(self.get_state("studio_mode"))}

    async def _rq_SetStudioModeEnabled(self, data):
        enabled = bool(self._need(data, "studioModeEnabled"))
        if enabled != bool(self.get_state("studio_mode")):
            await self.apply_studio_mode(enabled)

    async def _rq_GetMonitorList(self, data):
        return {"monitors": [dict(m) for m in MONITORS]}

    async def _rq_OpenVideoMixProjector(self, data):
        mix = self._need(data, "videoMixType")
        if not str(mix).startswith("OBS_WEBSOCKET_VIDEO_MIX_TYPE_"):
            raise _Fail(INVALID_REQUEST_FIELD, "The field `videoMixType` has an invalid value.")
        self.projectors.append({"mix": mix, "monitor": data.get("monitorIndex", -1)})

    async def _rq_OpenSourceProjector(self, data):
        source = self._source(data)
        self.projectors.append({"source": source, "monitor": data.get("monitorIndex", -1)})

    # Stream

    async def _rq_GetStreamStatus(self, data):
        return self._output_status("streaming")

    async def _rq_StartStream(self, data):
        if self.get_state("streaming"):
            raise _Fail(OUTPUT_RUNNING, "The stream output is already active.")
        await self.apply_output("streaming", True)

    async def _rq_StopStream(self, data):
        if not self.get_state("streaming"):
            raise _Fail(OUTPUT_NOT_RUNNING, "The stream output is not active.")
        await self.apply_output("streaming", False)

    async def _rq_ToggleStream(self, data):
        await self.apply_output("streaming", not self.get_state("streaming"))
        return {"outputActive": bool(self.get_state("streaming"))}

    async def _rq_SendStreamCaption(self, data):
        self.captions.append(str(self._need(data, "captionText")))

    # Record

    async def _rq_GetRecordStatus(self, data):
        status = self._output_status("recording")
        return {"outputActive": status["outputActive"], "outputPaused": bool(self.get_state("record_paused")),
                "outputTimecode": status["outputTimecode"], "outputDuration": status["outputDuration"],
                "outputBytes": status["outputBytes"]}

    async def _rq_StartRecord(self, data):
        if self.get_state("recording"):
            raise _Fail(OUTPUT_RUNNING, "The record output is already active.")
        await self.apply_output("recording", True)

    async def _rq_StopRecord(self, data):
        if not self.get_state("recording"):
            raise _Fail(OUTPUT_NOT_RUNNING, "The record output is not active.")
        await self.apply_output("recording", False)
        return {"outputPath": self.record_file}

    async def _rq_ToggleRecord(self, data):
        await self.apply_output("recording", not self.get_state("recording"))
        return {"outputActive": bool(self.get_state("recording"))}

    async def _rq_PauseRecord(self, data):
        if not self.get_state("recording"):
            raise _Fail(OUTPUT_NOT_RUNNING, "The record output is not active.")
        if self.get_state("record_paused"):
            raise _Fail(OUTPUT_PAUSED, "The record output is already paused.")
        if self.has_error_behavior("pause_noop"):
            return
        asyncio.ensure_future(self.apply_record_paused(True))

    async def _rq_ResumeRecord(self, data):
        if not self.get_state("recording"):
            raise _Fail(OUTPUT_NOT_RUNNING, "The record output is not active.")
        if not self.get_state("record_paused"):
            raise _Fail(OUTPUT_NOT_PAUSED, "The record output is not paused.")
        await self.apply_record_paused(False)

    async def _rq_ToggleRecordPause(self, data):
        if not self.get_state("recording"):
            raise _Fail(OUTPUT_NOT_RUNNING, "The record output is not active.")
        if self.get_state("record_paused"):
            await self.apply_record_paused(False)
        elif not self.has_error_behavior("pause_noop"):
            asyncio.ensure_future(self.apply_record_paused(True))

    async def _rq_SplitRecordFile(self, data):
        if not self.get_state("recording"):
            raise _Fail(OUTPUT_NOT_RUNNING, "The record output is not active.")
        self.record_file = f"{RECORD_DIRECTORY}/2026-09-08 10-00-00 (1).mkv"
        await self.emit("RecordFileChanged", {"newOutputPath": self.record_file})

    async def _rq_CreateRecordChapter(self, data):
        if not self.get_state("recording"):
            raise _Fail(OUTPUT_NOT_RUNNING, "The record output is not active.")
        self.chapters = getattr(self, "chapters", []) + [str(data.get("chapterName") or "")]

    # Outputs

    async def _rq_GetVirtualCamStatus(self, data):
        return {"outputActive": bool(self.get_state("virtual_camera"))}

    async def _rq_StartVirtualCam(self, data):
        if self.get_state("virtual_camera"):
            raise _Fail(OUTPUT_RUNNING, "The virtual camera output is already active.")
        await self.apply_output("virtual_camera", True)

    async def _rq_StopVirtualCam(self, data):
        if not self.get_state("virtual_camera"):
            raise _Fail(OUTPUT_NOT_RUNNING, "The virtual camera output is not active.")
        await self.apply_output("virtual_camera", False)

    async def _rq_ToggleVirtualCam(self, data):
        await self.apply_output("virtual_camera", not self.get_state("virtual_camera"))
        return {"outputActive": bool(self.get_state("virtual_camera"))}

    async def _rq_GetReplayBufferStatus(self, data):
        return {"outputActive": bool(self.get_state("replay_buffer"))}

    async def _rq_StartReplayBuffer(self, data):
        if self.get_state("replay_buffer"):
            raise _Fail(OUTPUT_RUNNING, "The replay buffer output is already active.")
        await self.apply_output("replay_buffer", True)

    async def _rq_StopReplayBuffer(self, data):
        if not self.get_state("replay_buffer"):
            raise _Fail(OUTPUT_NOT_RUNNING, "The replay buffer output is not active.")
        await self.apply_output("replay_buffer", False)

    async def _rq_ToggleReplayBuffer(self, data):
        await self.apply_output("replay_buffer", not self.get_state("replay_buffer"))
        return {"outputActive": bool(self.get_state("replay_buffer"))}

    async def _rq_SaveReplayBuffer(self, data):
        if not self.get_state("replay_buffer"):
            raise _Fail(OUTPUT_NOT_RUNNING, "The replay buffer output is not active.")
        self.last_replay = f"{RECORD_DIRECTORY}/Replay 2026-09-08 10-05-00.mkv"
        await self.emit("ReplayBufferSaved", {"savedReplayPath": self.last_replay})

    async def _rq_GetLastReplayBufferReplay(self, data):
        return {"savedReplayPath": self.last_replay}

    async def _rq_GetOutputList(self, data):
        return {"outputs": [
            {"outputName": name, "outputKind": spec["kind"], "outputWidth": 1280, "outputHeight": 720,
             "outputActive": spec["active"], "outputFlags": dict(spec["flags"])}
            for name, spec in self.outputs.items()]}

    def _output(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        name = self._need(data, "outputName")
        spec = self.outputs.get(str(name))
        if spec is None:
            raise _Fail(RESOURCE_NOT_FOUND, "No output was found by the name of `%s`." % name)
        return str(name), spec

    async def _rq_GetOutputStatus(self, data):
        name, spec = self._output(data)
        status = self._output_status(name)
        status["outputActive"] = spec["active"]
        return status

    async def _set_output(self, name: str, spec: dict[str, Any], active: bool) -> None:
        key = {"simple_stream": "streaming", "simple_file_output": "recording",
               "virtualcam_output": "virtual_camera"}.get(name)
        if key:
            await self.apply_output(key, active)
        else:
            spec["active"] = active

    async def _rq_StartOutput(self, data):
        name, spec = self._output(data)
        if spec["active"]:
            raise _Fail(OUTPUT_RUNNING, "The output is already active.")
        await self._set_output(name, spec, True)

    async def _rq_StopOutput(self, data):
        name, spec = self._output(data)
        if not spec["active"]:
            raise _Fail(OUTPUT_NOT_RUNNING, "The output is not active.")
        await self._set_output(name, spec, False)

    async def _rq_ToggleOutput(self, data):
        name, spec = self._output(data)
        await self._set_output(name, spec, not spec["active"])
        return {"outputActive": spec["active"]}


class _Fail(Exception):
    def __init__(self, code: int, comment: str = ""):
        super().__init__(comment)
        self.code = code
        self.comment = comment


class _AiohttpSender:
    """Adapts an aiohttp WebSocketResponse to the sender the sessions use."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, text: str) -> None:
        if not self._ws.closed:
            await self._ws.send_str(text)

    async def close(self, code: int, reason: str) -> None:
        await self._ws.close(code=code, message=reason.encode("utf-8"))
