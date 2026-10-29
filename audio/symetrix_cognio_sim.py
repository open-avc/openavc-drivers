"""
Symetrix Cognio — Simulator.

Serves the Cognio Control Protocol (JSON-RPC 2.0, each message ended by a
null byte) on TCP 3012 for a small site: two Control Panel Groups, "Room"
(a fader, a mute, a source drop list, a read-only meter) and "Lobby" (a
background music level and mute), three remote-enabled presets and a preset
group holding two of them.

Methods, as Symetrix's "Using the Cognio Control Protocol" describes them:
NamedControl.Get / GetText / GetValue (a name ending "/" answers the whole
group as an array inside the result), Set / SetText / SetValue (a name alone
sets that control in every group), ModifyValue, Subscribe / SubscribeText /
SubscribeValue (rate, condition, sync; rate 0 cancels), and
Device.RecallPreset / UpdatePreset / GetLastRecalledPreset / GetPresets.
Errors use the document's codes (-101, -201, -202, -301, and the JSON-RPC
ones). A request with no id is a notification and gets no answer.

Not modelled, because the document does not say: what a Get of a missing
control answers (this answers -201, the code the document gives for a set),
what cancelling a subscription answers (this answers 1), and whether a group
Get lists an unassigned control.

Set the device config's ``designops`` to "1.0" to simulate a processor
without Device.GetPresets (added in DesignOps 1.1).

Driver side: ``audio/symetrix_cognio.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

FRAME_TERMINATOR = b"\x00"
PUMP_TICK_S = 0.05

SOURCE_OPTIONS = ["Laptop", "Wall Plate", "Wireless"]


def _site() -> dict[str, dict[str, Any]]:
    """Control remote name -> control. Kinds: gain (dB), button (0 / 1),
    list (1-based position), meter (read-only dB)."""
    return {
        "Room/fader1": {"kind": "gain", "value": -12.0, "min": -100.0, "max": 12.0},
        "Room/mute1": {"kind": "button", "value": 0},
        "Room/source": {"kind": "list", "value": 1, "options": SOURCE_OPTIONS},
        "Room/meter1": {"kind": "meter", "value": -40.0},
        "Lobby/bgm_level": {"kind": "gain", "value": -20.0, "min": -100.0, "max": 12.0},
        "Lobby/bgm_mute": {"kind": "button", "value": 0},
    }


def _presets() -> dict[str, dict[str, Any]]:
    return {
        "p1": {"name": "Presentation",
               "values": {"Room/fader1": -6.0, "Room/mute1": 0, "Room/source": 1}},
        "p2": {"name": "Video Call",
               "values": {"Room/fader1": -10.0, "Room/source": 2}},
        "p3": {"name": "Shutdown",
               "values": {"Room/mute1": 1, "Lobby/bgm_mute": 1}},
    }


PRESET_GROUPS = {"scenes": ["p2", "p3"]}

# Simulator UI key -> control remote name.
UI_KEYS = {
    "room_fader1": "Room/fader1",
    "room_mute1": "Room/mute1",
    "room_source": "Room/source",
    "room_meter1": "Room/meter1",
    "lobby_bgm_level": "Lobby/bgm_level",
    "lobby_bgm_mute": "Lobby/bgm_mute",
}


class CognioSimError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SymetrixCognioSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "symetrix_cognio",
        "name": "Symetrix Cognio Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 3012,
        "delimiter": "\x00",
        "initial_state": {
            "room_fader1": -12.0,
            "room_mute1": False,
            "room_source": 1,
            "room_meter1": -40.0,
            "lobby_bgm_level": -20.0,
            "lobby_bgm_mute": False,
            "last_preset": "",
        },
        "controls": [
            {"type": "slider", "key": "room_fader1", "label": "Room fader1 (dB)",
             "min": -100, "max": 12, "step": 0.5},
            {"type": "toggle", "key": "room_mute1", "label": "Room mute1"},
            {"type": "slider", "key": "room_source", "label": "Room source",
             "min": 1, "max": 3, "step": 1},
            {"type": "slider", "key": "room_meter1", "label": "Room meter1 (dB)",
             "min": -100, "max": 0, "step": 1},
            {"type": "slider", "key": "lobby_bgm_level", "label": "Lobby bgm_level (dB)",
             "min": -100, "max": 12, "step": 0.5},
            {"type": "toggle", "key": "lobby_bgm_mute", "label": "Lobby bgm_mute"},
            {"type": "indicator", "key": "last_preset", "label": "Last preset"},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._controls = _site()
        self._preset_table = _presets()
        self._last_recalled: str | None = None
        # client id -> subscription key -> subscription
        self._subs: dict[str, dict[str, dict[str, Any]]] = {}
        self._pumps: dict[str, asyncio.Task] = {}
        self._writing_ui = False

    # ── Connection ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        self._subs[client_id] = {}
        self._pumps[client_id] = asyncio.ensure_future(self._pump(client_id))
        return None

    async def stop(self) -> None:
        for task in self._pumps.values():
            task.cancel()
        self._pumps.clear()
        await super().stop()

    def _latest_client_id(self) -> str | None:
        if not self._clients:
            return None
        return next(reversed(self._clients))

    # ── Dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("utf-8", errors="replace").strip("\x00").strip()
        if not text:
            return None
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return self._frame(self._error(None, -32700, "Parse error"))
        if isinstance(msg, list):
            replies = [r for r in (self._dispatch(m) for m in msg) if r is not None]
            return self._frame(replies) if replies else None
        reply = self._dispatch(msg)
        return self._frame(reply) if reply is not None else None

    def _dispatch(self, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._error(
                msg.get("id") if isinstance(msg, dict) else None,
                -32600, "Invalid Request")
        rid = msg.get("id")
        method = msg.get("method")
        if not isinstance(method, str):
            return self._error(rid, -32600, "Invalid Request",
                               "Missing required property: method")
        handler = getattr(self, "_m_" + method.replace(".", "_"), None)
        if handler is None:
            if rid is None:
                return None
            return self._error(rid, -32601, "Method not found")
        try:
            result = handler(msg.get("params"), rid)
        except CognioSimError as exc:
            return None if rid is None else self._error(rid, exc.code, exc.message)
        except Exception:
            logger.exception("%s: error in %s", self.name, method)
            return None if rid is None else self._error(rid, -32603, "Internal error")
        if rid is None or result is _NO_REPLY:
            return None
        return {"jsonrpc": "2.0", "result": result, "id": rid}

    # ── Name resolution ──

    def _groups(self) -> list[str]:
        out: list[str] = []
        for full in self._controls:
            group = full.split("/", 1)[0]
            if group not in out:
                out.append(group)
        return out

    def _resolve(self, name: Any, *, for_set: bool = False) -> list[str] | list[list[str]]:
        """Control remote names a request names. A group ("Room/") is one
        list inside the result; a bare name matches every group."""
        if not isinstance(name, str) or not name:
            raise CognioSimError(-32602, "Invalid params")
        if name.endswith("/"):
            group = name[:-1]
            if group not in self._groups():
                raise CognioSimError(-301, "Control panel group not found")
            if for_set:
                raise CognioSimError(-32602, "Invalid params")
            return [[f for f in self._controls if f.startswith(name)]]
        if "/" in name:
            group = name.split("/", 1)[0]
            if group not in self._groups():
                raise CognioSimError(-301, "Control panel group not found")
            if name not in self._controls:
                raise CognioSimError(-201, "Control not found")
            return [name]
        matches = [f for f in self._controls if f.split("/", 1)[1] == name]
        if not matches:
            raise CognioSimError(-201, "Control not found")
        return matches

    def _entry(self, full: str, fields: str = "both") -> dict[str, Any]:
        ctrl = self._controls[full]
        entry: dict[str, Any] = {"name": full}
        if fields in ("both", "text"):
            entry["text"] = self._text(ctrl)
        if fields in ("both", "value"):
            entry["value"] = ctrl["value"]
        return entry

    @staticmethod
    def _text(ctrl: dict[str, Any]) -> str:
        kind = ctrl["kind"]
        value = ctrl["value"]
        if kind in ("gain", "meter"):
            return f"{float(value):.1f} dB"
        if kind == "button":
            return "muted" if value else "unmuted"
        if kind == "list":
            options = ctrl["options"]
            idx = int(value) - 1
            return options[idx] if 0 <= idx < len(options) else str(value)
        return str(value)

    # ── NamedControl ──

    def _get(self, params: Any, fields: str) -> list[Any]:
        if not isinstance(params, list) or not params:
            raise CognioSimError(-32602, "Invalid params")
        result: list[Any] = []
        for name in params:
            for item in self._resolve(name):
                if isinstance(item, list):
                    result.append([self._entry(f, fields) for f in item])
                else:
                    result.append(self._entry(item, fields))
        return result

    def _m_NamedControl_Get(self, params: Any, rid: Any) -> Any:
        return self._get(params, "both")

    def _m_NamedControl_GetText(self, params: Any, rid: Any) -> Any:
        return self._get(params, "text")

    def _m_NamedControl_GetValue(self, params: Any, rid: Any) -> Any:
        return self._get(params, "value")

    def _set(self, params: Any, mode: str) -> Any:
        if not isinstance(params, dict):
            raise CognioSimError(-32602, "Invalid params")
        targets = self._resolve(params.get("name"), for_set=True)
        # "If both value and text are provided, value takes precedence."
        use_value = mode == "value" or (mode == "either" and "value" in params)
        raw = params.get("value") if use_value else params.get("text")
        if raw is None:
            raise CognioSimError(-32602, "Invalid params")
        for full in targets:
            ctrl = self._controls[full]
            if ctrl["kind"] == "meter":
                raise CognioSimError(-202, "Control not writable")
        for full in targets:
            ctrl = self._controls[full]
            number = self._number_from(ctrl, raw) if not use_value else raw
            if not isinstance(number, (int, float)) or isinstance(number, bool):
                try:
                    number = float(number)
                except (TypeError, ValueError):
                    raise CognioSimError(-32602, "Invalid params") from None
            self._write(full, number)
        return 1

    @staticmethod
    def _number_from(ctrl: dict[str, Any], text: Any) -> Any:
        text = str(text).strip()
        if ctrl["kind"] == "list" and text in ctrl["options"]:
            return ctrl["options"].index(text) + 1
        if ctrl["kind"] == "button" and text.lower() in ("muted", "unmuted", "on", "off"):
            return 1 if text.lower() in ("muted", "on") else 0
        match = re.match(r"\s*([-+]?\d+(?:\.\d+)?)", text)
        return float(match.group(1)) if match else text

    def _m_NamedControl_Set(self, params: Any, rid: Any) -> Any:
        return self._set(params, "either")

    def _m_NamedControl_SetValue(self, params: Any, rid: Any) -> Any:
        return self._set(params, "value")

    def _m_NamedControl_SetText(self, params: Any, rid: Any) -> Any:
        return self._set(params, "text")

    def _m_NamedControl_ModifyValue(self, params: Any, rid: Any) -> Any:
        if not isinstance(params, dict) or not isinstance(
            params.get("offset"), (int, float)
        ):
            raise CognioSimError(-32602, "Invalid params")
        targets = self._resolve(params.get("name"), for_set=True)
        for full in targets:
            if self._controls[full]["kind"] == "meter":
                raise CognioSimError(-202, "Control not writable")
        out = []
        for full in targets:
            self._write(full, self._controls[full]["value"] + params["offset"])
            out.append({"name": full, "value": self._controls[full]["value"]})
        return out

    def _subscribe(self, params: Any, rid: Any, fields: str) -> Any:
        if not isinstance(params, dict):
            raise CognioSimError(-32602, "Invalid params")
        client_id = self._latest_client_id()
        name = params.get("name")
        rate = params.get("rate")
        if not isinstance(rate, (int, float)) or rate < 0:
            raise CognioSimError(-32602, "Invalid params")
        resolved = self._resolve(name)
        # "Only the first control in a control panel group will be subscribed
        # to."
        first = resolved[0]
        targets = [first[0]] if isinstance(first, list) and first else (
            [first] if isinstance(first, str) else [])
        if not targets:
            return []
        key = f"{rid}|{name}"
        subs = self._subs.setdefault(client_id or "", {})
        if rate == 0:
            subs.pop(key, None)
            return 1
        condition = params.get("condition", "change")
        sync = int(params.get("sync", 0) or 0)
        subs[key] = {
            "id": rid, "targets": targets, "fields": fields,
            "rate": float(rate) / 1000.0, "condition": condition,
            "sync": sync, "last_sent": time.monotonic(), "dirty": False,
            "skipped": 0,
        }
        if condition == "change" and sync != 1:
            # Nothing is sent until the value changes.
            return _NO_REPLY
        return [self._entry(f, fields) for f in targets]

    def _m_NamedControl_Subscribe(self, params: Any, rid: Any) -> Any:
        return self._subscribe(params, rid, "both")

    def _m_NamedControl_SubscribeText(self, params: Any, rid: Any) -> Any:
        return self._subscribe(params, rid, "text")

    def _m_NamedControl_SubscribeValue(self, params: Any, rid: Any) -> Any:
        return self._subscribe(params, rid, "value")

    # ── Device ──

    def _preset_names(self, params: Any) -> list[str]:
        if isinstance(params, str):
            return [params]
        if isinstance(params, list) and all(isinstance(p, str) for p in params):
            return list(params)
        raise CognioSimError(-32602, "Invalid params")

    def _m_Device_RecallPreset(self, params: Any, rid: Any) -> Any:
        names = self._preset_names(params)
        found = 0
        missing = False
        for name in names:
            members = PRESET_GROUPS.get(name, [name] if name in self._preset_table else [])
            if not members:
                missing = True
                continue
            for member in members:
                for full, value in self._preset_table[member]["values"].items():
                    self._write(full, value)
                    found += 1
                self._last_recalled = member
        if self._last_recalled:
            self._set_ui("last_preset", self._last_recalled)
        if missing:
            raise CognioSimError(-101, "Preset not found")
        return {"recalledControls": found}

    def _m_Device_UpdatePreset(self, params: Any, rid: Any) -> Any:
        names = self._preset_names(params)
        updated = 0
        missing = False
        for name in names:
            members = PRESET_GROUPS.get(name, [name] if name in self._preset_table else [])
            if not members:
                missing = True
                continue
            for member in members:
                values = self._preset_table[member]["values"]
                for full in values:
                    values[full] = self._controls[full]["value"]
                    updated += 1
        if missing:
            raise CognioSimError(-101, "Preset not found")
        return {"updatedControls": updated}

    def _m_Device_GetLastRecalledPreset(self, params: Any, rid: Any) -> Any:
        if self._last_recalled is None:
            raise CognioSimError(-101, "Preset not found")
        return {
            "remoteName": self._last_recalled,
            "name": self._preset_table[self._last_recalled]["name"],
        }

    def _m_Device_GetPresets(self, params: Any, rid: Any) -> Any:
        if str((self.config or {}).get("designops", "")) == "1.0":
            raise CognioSimError(-32601, "Method not found")
        if params is None:
            wanted = None
        else:
            wanted = set(self._preset_names(params))
            unknown = wanted - set(self._preset_table) - set(PRESET_GROUPS)
            if unknown:
                raise CognioSimError(-101, "Preset not found")
        grouped = {m for members in PRESET_GROUPS.values() for m in members}
        out: list[dict[str, Any]] = []
        for name in self._preset_table:
            if name not in grouped and (wanted is None or name in wanted):
                out.append({"name": name, "group": False})
        for gname, members in PRESET_GROUPS.items():
            if wanted is None or gname in wanted:
                out.append({
                    "name": gname, "group": True,
                    "presets": [{"name": m, "group": False} for m in members],
                })
        return {"presets": out}

    # ── Changes and subscriptions ──

    def _write(self, full: str, value: Any) -> None:
        ctrl = self._controls[full]
        if ctrl["kind"] == "gain":
            value = max(ctrl["min"], min(ctrl["max"], float(value)))
        elif ctrl["kind"] == "button":
            value = 1 if float(value) else 0
        elif ctrl["kind"] == "list":
            value = max(1, min(len(ctrl["options"]), int(round(float(value)))))
        if ctrl["value"] == value:
            return
        ctrl["value"] = value
        for subs in self._subs.values():
            for sub in subs.values():
                if full in sub["targets"]:
                    sub["dirty"] = True
        for key, mapped in UI_KEYS.items():
            if mapped == full:
                shown = bool(value) if ctrl["kind"] == "button" else value
                self._set_ui(key, shown)

    def _set_ui(self, key: str, value: Any) -> None:
        self._writing_ui = True
        try:
            super().set_state(key, value)
        finally:
            self._writing_ui = False

    def set_state(self, key: str, value: Any) -> None:
        super().set_state(key, value)
        if self._writing_ui:
            return
        full = UI_KEYS.get(key)
        if full is None:
            return
        # A Simulator UI toggle stores a real boolean.
        if isinstance(value, bool):
            value = int(value)
        try:
            self._write(full, float(value))
        except (TypeError, ValueError):
            pass

    async def _pump(self, client_id: str) -> None:
        try:
            while client_id in self._clients:
                await asyncio.sleep(PUMP_TICK_S)
                now = time.monotonic()
                for sub in list(self._subs.get(client_id, {}).values()):
                    if now - sub["last_sent"] < sub["rate"]:
                        continue
                    due = sub["dirty"] or sub["condition"] == "time"
                    if not due and sub["condition"] == "change" and sub["sync"] > 1:
                        # "skips that number of minimum update periods between
                        # sending notifications if there is no change"
                        sub["skipped"] += 1
                        due = sub["skipped"] >= sub["sync"]
                    if not due:
                        continue
                    sub["dirty"] = False
                    sub["skipped"] = 0
                    sub["last_sent"] = now
                    reply = {
                        "jsonrpc": "2.0",
                        "result": [self._entry(f, sub["fields"]) for f in sub["targets"]],
                        "id": sub["id"],
                    }
                    await self.push_to(client_id, self._frame(reply))
        except asyncio.CancelledError:
            return
        finally:
            self._subs.pop(client_id, None)
            self._pumps.pop(client_id, None)

    # ── Framing ──

    @staticmethod
    def _frame(obj: Any) -> bytes:
        return json.dumps(obj).encode("utf-8") + FRAME_TERMINATOR

    @staticmethod
    def _error(rid: Any, code: int, message: str, details: str | None = None) -> dict[str, Any]:
        err: dict[str, Any] = {"code": code, "message": message}
        if details:
            err["data"] = {"details": details}
        return {"jsonrpc": "2.0", "error": err, "id": rid}


_NO_REPLY = object()
