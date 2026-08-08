"""
AtlasIED Atmosphere AZM4 / AZM8 — Simulator.

Implements the third-party JSON-RPC 2.0 protocol described in
ATS006993-B-AZM4-AZM8-3rd-Party-Control. Newline-delimited JSON over
TCP port 5321.

  - Accepts "set", "bmp", "sub", "unsub", "get".
  - Pushes "update" messages to all subscribed clients on state change.
  - Replies to "get" with "getResp".
  - Replies to "id"-bearing messages with {"jsonrpc":"2.0","result":"OK","id":N}.
  - Models a configurable count of sources / zones / mixes / groups /
    messages / routines / scenes / gpo_presets / gpos / bell_schedules,
    matching an AZM8 default deployment.

Read-only "Name" parameters can be set by the simulator config and are
returned via subscribe / get; client "set" attempts on Name params are
ignored, matching the device behavior. GpoState and ZoneGrouped are
likewise read-only for third-party clients (ATS006993 §6 parameter
table) — GPO pins are driven by recalling GPO presets.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


DEFAULT_NUM_SOURCES = 8
DEFAULT_NUM_ZONES = 8
DEFAULT_NUM_MIXES = 8
DEFAULT_NUM_GROUPS = 4
DEFAULT_NUM_MESSAGES = 8
DEFAULT_NUM_ROUTINES = 8
DEFAULT_NUM_SCENES = 8
DEFAULT_NUM_GPO_PRESETS = 8
DEFAULT_NUM_GPOS = 4
DEFAULT_NUM_BELL_SCHEDULES = 4

NAME_PARAMS = {
    "SourceName",
    "ZoneName",
    "MixName",
    "GroupName",
    "MessageName",
    "RoutineName",
    "SceneName",
    "GpoPresetName",
    "BellScheduleName",
    "FirmwareVersion",
    "KeepAlive",
}

# GpoState and ZoneGrouped can be subscribed but not set by third-party
# clients (ATS006993 §6 parameter table) — GPOs change via RecallGpoPreset.
READ_ONLY_PARAMS = NAME_PARAMS | {"GpoState", "ZoneGrouped"}


class AtlasIEDAtmosphereSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "atlasied_atmosphere",
        "name": "AtlasIED Atmosphere Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 5321,
        "delimiter": "\n",
        "initial_state": {
            "firmware_version": "1.7.0",
            "todays_bell_schedule": 0,
        },
        "controls": [
            {
                "type": "indicator",
                "key": "firmware_version",
                "label": "Firmware",
            },
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)

        cfg = self.config
        self._num_sources = int(cfg.get("num_sources", DEFAULT_NUM_SOURCES))
        self._num_zones = int(cfg.get("num_zones", DEFAULT_NUM_ZONES))
        self._num_mixes = int(cfg.get("num_mixes", DEFAULT_NUM_MIXES))
        self._num_groups = int(cfg.get("num_groups", DEFAULT_NUM_GROUPS))
        self._num_messages = int(cfg.get("num_messages", DEFAULT_NUM_MESSAGES))
        self._num_routines = int(cfg.get("num_routines", DEFAULT_NUM_ROUTINES))
        self._num_scenes = int(cfg.get("num_scenes", DEFAULT_NUM_SCENES))
        self._num_gpo_presets = int(cfg.get("num_gpo_presets", DEFAULT_NUM_GPO_PRESETS))
        self._num_gpos = int(cfg.get("num_gpos", DEFAULT_NUM_GPOS))
        self._num_bell_schedules = int(
            cfg.get("num_bell_schedules", DEFAULT_NUM_BELL_SCHEDULES)
        )

        # Per-param value store. Defaults: gains -10 dB, mutes off,
        # sources select source 0, groups inactive.
        self._values: dict[str, Any] = {}
        self._init_values()

        # Subscriptions: {param -> set of subscribed fmts}.
        # Simulator-wide rather than per-client; the test harness uses one
        # client and that's the only realistic case.
        self._subscriptions: dict[str, set[str]] = {}

    def _init_values(self) -> None:
        for n in range(self._num_sources):
            self._values[f"SourceGain_{n}"] = -10.0
            self._values[f"SourceMute_{n}"] = 0
            self._values[f"SourceName_{n}"] = f"Source {n + 1}"
        for n in range(self._num_zones):
            self._values[f"ZoneGain_{n}"] = -10.0
            self._values[f"ZoneMute_{n}"] = 0
            self._values[f"ZoneName_{n}"] = f"Zone {n + 1}"
            self._values[f"ZoneSource_{n}"] = 0
            self._values[f"ZoneGrouped_{n}"] = 0
        for n in range(self._num_mixes):
            self._values[f"MixGain_{n}"] = -10.0
            self._values[f"MixMute_{n}"] = 0
            self._values[f"MixName_{n}"] = f"Mix {n + 1}"
        for n in range(self._num_groups):
            self._values[f"GroupGain_{n}"] = -10.0
            self._values[f"GroupMute_{n}"] = 0
            self._values[f"GroupName_{n}"] = f"Group {n + 1}"
            self._values[f"GroupSource_{n}"] = 0
            self._values[f"GroupActive_{n}"] = 0
        for n in range(self._num_messages):
            self._values[f"MessageName_{n}"] = f"Message {n + 1}"
        for n in range(self._num_routines):
            self._values[f"RoutineName_{n}"] = f"Routine {n + 1}"
        for n in range(self._num_scenes):
            self._values[f"SceneName_{n}"] = f"Scene {n + 1}"
        for n in range(self._num_gpo_presets):
            self._values[f"GpoPresetName_{n}"] = f"GPO Preset {n + 1}"
        for n in range(self._num_gpos):
            self._values[f"GpoState_{n}"] = 0
        for n in range(self._num_bell_schedules):
            self._values[f"BellScheduleName_{n}"] = f"Schedule {n + 1}"

        self._values["TodaysBellSchedule"] = 0
        self._values["FirmwareVersion"] = self.state.get("firmware_version", "1.7.0")

    # ── Per-line handling ──

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", errors="replace").strip()
        if not line:
            return None

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return None

        if not isinstance(msg, dict):
            return None

        method = msg.get("method")
        params = msg.get("params")
        msg_id = msg.get("id")

        # Normalize params to a list of dicts.
        if isinstance(params, dict):
            param_list = [params]
        elif isinstance(params, list):
            param_list = [p for p in params if isinstance(p, dict)]
        else:
            param_list = []

        responses: list[dict[str, Any]] = []

        if method == "set":
            for p in param_list:
                self._handle_set(p, responses)
        elif method == "bmp":
            for p in param_list:
                self._handle_bump(p, responses)
        elif method == "sub":
            for p in param_list:
                self._handle_subscribe(p, responses)
        elif method == "unsub":
            for p in param_list:
                self._handle_unsubscribe(p)
        elif method == "get":
            for p in param_list:
                self._handle_get(p, responses)
        else:
            return None

        out = b""

        # Group "update" / "getResp" by method type for compactness; the
        # protocol allows arrays of params under one method.
        updates = [r for r in responses if r["__method"] == "update"]
        get_resps = [r for r in responses if r["__method"] == "getResp"]

        if updates:
            out += self._encode("update", updates)
        if get_resps:
            out += self._encode("getResp", get_resps)

        # Per the protocol: messages with an "id" always get a final
        # {"jsonrpc":"2.0","result":"OK","id":N} ack regardless of method.
        if msg_id is not None:
            out += (
                json.dumps(
                    {"jsonrpc": "2.0", "result": "OK", "id": msg_id},
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )

        return out or None

    def _encode(self, method: str, items: list[dict[str, Any]]) -> bytes:
        clean = [
            {k: v for k, v in item.items() if not k.startswith("__")}
            for item in items
        ]
        msg = {"jsonrpc": "2.0", "method": method, "params": clean}
        return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"

    # ── Method handlers ──

    def _handle_set(self, p: dict[str, Any], responses: list[dict[str, Any]]) -> None:
        param = p.get("param", "")
        if not param or param not in self._values:
            return

        prefix = param.rsplit("_", 1)[0]
        # Read-only params (Names, FirmwareVersion, GpoState, ZoneGrouped)
        # can't be set by clients.
        if prefix in READ_ONLY_PARAMS:
            return

        # Action params (PlayMessage / RecallRoutine / etc.) just trigger.
        # We treat any "set" on an action param as a successful trigger
        # and push an "update" so subscribers see it fired (matches the
        # spec note in §4.0).
        if prefix in ("PlayMessage", "RecallRoutine", "RecallScene", "RecallGpoPreset"):
            if param in self._subscriptions:
                fmt = next(iter(self._subscriptions[param]), "val")
                responses.append(self._build_update(param, fmt, 1))
            return

        new_val = self._coerce_set_value(p, prefix)
        if new_val is None:
            return
        self._values[param] = new_val

        # Push update if the param is subscribed.
        for fmt in self._subscriptions.get(param, set()):
            responses.append(self._build_update(param, fmt, new_val))

    def _handle_bump(self, p: dict[str, Any], responses: list[dict[str, Any]]) -> None:
        param = p.get("param", "")
        if not param or param not in self._values:
            return

        prefix = param.rsplit("_", 1)[0]
        if prefix in READ_ONLY_PARAMS:
            return

        # Bump on action params triggers them.
        if prefix in ("PlayMessage", "RecallRoutine", "RecallScene", "RecallGpoPreset"):
            if param in self._subscriptions:
                fmt = next(iter(self._subscriptions[param]), "val")
                responses.append(self._build_update(param, fmt, 1))
            return

        delta = p.get("val")
        if delta is None:
            return
        try:
            delta_f = float(delta)
        except (TypeError, ValueError):
            return

        current = self._values[param]
        try:
            new_val: Any = float(current) + delta_f
        except (TypeError, ValueError):
            return
        # Clamp gains to the AZM range [-80, 0] dB. Other val params (e.g.
        # ZoneSource) the spec doesn't bump, so we leave them unclamped
        # if the client tries.
        if "Gain_" in param:
            new_val = max(-80.0, min(0.0, new_val))
        self._values[param] = new_val

        for fmt in self._subscriptions.get(param, set()):
            responses.append(self._build_update(param, fmt, new_val))

    def _handle_subscribe(
        self, p: dict[str, Any], responses: list[dict[str, Any]]
    ) -> None:
        param = p.get("param", "")
        fmt = p.get("fmt", "val")
        if not param or param not in self._values:
            return
        self._subscriptions.setdefault(param, set()).add(fmt)
        # Send an immediate update with the current value so the client
        # has something to display without polling.
        responses.append(self._build_update(param, fmt, self._values[param]))

    def _handle_unsubscribe(self, p: dict[str, Any]) -> None:
        param = p.get("param", "")
        fmt = p.get("fmt")
        subs = self._subscriptions.get(param)
        if not subs:
            return
        if fmt and fmt in subs:
            subs.discard(fmt)
        if not subs:
            self._subscriptions.pop(param, None)

    def _handle_get(self, p: dict[str, Any], responses: list[dict[str, Any]]) -> None:
        param = p.get("param", "")
        fmt = p.get("fmt", "val")
        if param == "KeepAlive":
            responses.append({
                "__method": "getResp",
                "param": "KeepAlive",
                "str": "OK",
            })
            return
        if not param or param not in self._values:
            return
        responses.append({
            "__method": "getResp",
            "param": param,
            **self._format_value(fmt, self._values[param]),
        })

    # ── Helpers ──

    def _build_update(self, param: str, fmt: str, value: Any) -> dict[str, Any]:
        return {"__method": "update", "param": param, **self._format_value(fmt, value)}

    def _format_value(self, fmt: str, value: Any) -> dict[str, Any]:
        if fmt == "str":
            return {"str": str(value)}
        if fmt == "pct":
            # Map -80..0 dB to 0..100. Other val params return as-is.
            try:
                f = float(value)
                pct = max(0, min(100, int(round((f + 80) / 80 * 100))))
                return {"pct": pct}
            except (TypeError, ValueError):
                return {"pct": 0}
        # default 'val'
        if isinstance(value, bool):
            return {"val": 1 if value else 0}
        return {"val": value}

    def _coerce_set_value(self, p: dict[str, Any], prefix: str) -> Any:
        # Pick whichever value field the client sent.
        if "val" in p:
            v = p["val"]
        elif "pct" in p:
            try:
                pct = float(p["pct"])
                # Map 0..100 pct to -80..0 dB for gain params.
                if "Gain" in prefix:
                    return max(-80.0, min(0.0, (pct / 100.0) * 80.0 - 80.0))
                return pct
            except (TypeError, ValueError):
                return None
        elif "str" in p:
            v = p["str"]
        else:
            return None

        # Mute / GroupActive are 0/1 ints.
        if prefix in ("SourceMute", "ZoneMute", "MixMute", "GroupMute", "GroupActive"):
            try:
                return 1 if int(v) != 0 else 0
            except (TypeError, ValueError):
                return None
        # Gains are floats clamped to AZM range.
        if "Gain" in prefix:
            try:
                f = float(v)
                return max(-80.0, min(0.0, f))
            except (TypeError, ValueError):
                return None
        # ZoneSource / GroupSource / TodaysBellSchedule are ints.
        if prefix in ("ZoneSource", "GroupSource") or p.get("param") == "TodaysBellSchedule":
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        return v
