"""
BSS Soundweb OMNI — Simulator.

Serves HARMAN HControl on TCP 4197, one JSON message per line, as the
"HControl Protocol Integration Guide" 1.23.2.0 describes it: get, set,
subscribe, unsubscribe and exec, each answered by an "@" reply in order (the
guide's lockstep), the five error replies it lists, and the four formats
(norm, number, string, variant).

Subscriptions follow the guide's message flow: the subscribe reply carries
the current value, and every later change goes out as a ``publish`` message.
The next ``publish`` waits until the client has answered the last one with
``@publish`` ("Failure to do so will halt further publish messages"); changes
made meanwhile queue, the newest value per control. A change is published to
every subscribed connection, the one that made it included.

The design it serves:
    /Device/Input1/Gain                 float -100..12 dB
    /Device/Input1/Mute                 boolean
    /Device/Input1/Meter                float -100..0 dB, read-only
    /Device/Output1/Gain                float -100..12 dB
    /Device/Output1/Mute                boolean
    /Device/Source/Select               enum Laptop / Wall Plate / Wireless
    /Device/Channel4PEQ/Band_1_Frequency  float 20..20000 Hz (the guide's example)
plus every path in the device's Controls table, as the kind its row gives,
and the device-level parameters under /configuration/device and /version.
A get, subscribe or unsubscribe on an object (/Device/Input1) covers every
parameter under it; a get on one answers an array.

Invented, because the guide does not say: what an OMNI reports as its
manufacturer, model and devicestate (this answers "BSS", the device config's
``model`` or "OMNI 512p", and "running"); the text a number renders as in
string format (the value and its unit, "100Hz" as the guide shows); the
refusal of a write to a read-only parameter ("Parameter is read only"); and
the refusal of a command the guide does not have ("Unknown command"). A
value beyond a parameter's range is set to the limit and that value
returned, as p.8 says, not refused as p.9's table shows. An exec is
acknowledged and does nothing.

Subscriptions are kept per connection; a message is read as coming from the
newest one (the simulator base hands a command no connection id), which is
exact for one controller.

Driver side: ``audio/bss_soundweb_omni.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from openavc.simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)

LINE_END = b"\n"
FORMATS = ("norm", "number", "string", "variant")
MESSAGE_RE = re.compile(r"^(@?)([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$", re.S)
NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

SOURCE_OPTIONS = ["Laptop", "Wall Plate", "Wireless"]

# Simulator UI key -> path.
UI_KEYS = {
    "input1_gain": "/Device/Input1/Gain",
    "input1_mute": "/Device/Input1/Mute",
    "input1_meter": "/Device/Input1/Meter",
    "output1_gain": "/Device/Output1/Gain",
    "output1_mute": "/Device/Output1/Mute",
    "source_select": "/Device/Source/Select",
    "peq_band1_frequency": "/Device/Channel4PEQ/Band_1_Frequency",
}
PATH_UI_KEYS = {path: key for key, path in UI_KEYS.items()}


def _param(kind: str, value: Any, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "value": value, **extra}


def _design() -> dict[str, dict[str, Any]]:
    return {
        "/Device/Input1/Gain": _param("float", -12.0, min=-100.0, max=12.0, unit="dB"),
        "/Device/Input1/Mute": _param("boolean", False),
        "/Device/Input1/Meter": _param("float", -40.0, min=-100.0, max=0.0, unit="dB", ro=True),
        "/Device/Output1/Gain": _param("float", -6.0, min=-100.0, max=12.0, unit="dB"),
        "/Device/Output1/Mute": _param("boolean", False),
        "/Device/Source/Select": _param("enum", 0, options=list(SOURCE_OPTIONS)),
        "/Device/Channel4PEQ/Band_1_Frequency": _param(
            "float", 100.0, min=20.0, max=20000.0, unit="Hz"),
    }


def _read_path_cell(text: str) -> tuple[str, str]:
    """The path and format from a Controls row's Path cell: a bare path, or a
    whole HControl message copied from AVX Architect."""
    text = (text or "").strip()
    if text.startswith("/"):
        return text, ""
    m = MESSAGE_RE.match(text)
    if m and m.group(3).strip().startswith("{"):
        try:
            body = json.loads(m.group(3))
        except json.JSONDecodeError:
            return "", ""
        if isinstance(body, dict) and isinstance(body.get("path"), str):
            fmt = body.get("format")
            return body["path"].strip(), fmt if isinstance(fmt, str) else ""
    return "", ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class BssSoundwebOmniSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "bss_soundweb_omni",
        "name": "BSS Soundweb OMNI Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 4197,
        "delimiter": "\n",
        "initial_state": {
            "input1_gain": -12.0,
            "input1_mute": False,
            "input1_meter": -40.0,
            "output1_gain": -6.0,
            "output1_mute": False,
            "source_select": 0,
            "peq_band1_frequency": 100.0,
        },
        "controls": [
            {"type": "slider", "key": "input1_gain", "label": "Input1 Gain (dB)",
             "min": -100, "max": 12, "step": 0.5},
            {"type": "toggle", "key": "input1_mute", "label": "Input1 Mute"},
            {"type": "slider", "key": "input1_meter", "label": "Input1 Meter (dB)",
             "min": -100, "max": 0, "step": 1},
            {"type": "slider", "key": "output1_gain", "label": "Output1 Gain (dB)",
             "min": -100, "max": 12, "step": 0.5},
            {"type": "toggle", "key": "output1_mute", "label": "Output1 Mute"},
            {"type": "slider", "key": "source_select",
             "label": "Source (0 Laptop, 1 Wall Plate, 2 Wireless)",
             "min": 0, "max": 2, "step": 1},
            {"type": "slider", "key": "peq_band1_frequency",
             "label": "Channel4PEQ Band 1 (Hz)", "min": 20, "max": 20000, "step": 10},
        ],
        "delays": {"command_response": 0.002},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        cfg = self.config or {}
        self._params = _design()
        self._params.update({
            "/configuration/device/manufacturer": _param("string", "BSS", ro=True),
            "/configuration/device/model": _param(
                "string", str(cfg.get("model") or "OMNI 512p"), ro=True),
            "/configuration/device/name": _param("string", "OMNI Processor"),
            "/configuration/device/devicestate": _param("string", "running", ro=True),
            "/version": _param("string", "1.0.0", ro=True),
        })
        self._add_declared(cfg.get("controls"))
        # client id -> subscribed (path, format) keys
        self._subs: dict[str, set[tuple[str, str]]] = {}
        # client id -> changes waiting to be published, newest value per key
        self._pending: dict[str, dict[tuple[str, str], None]] = {}
        # client ids whose last publish has not been acknowledged
        self._awaiting_ack: set[str] = set()
        self._writing_ui = False

    def _add_declared(self, rows: Any) -> None:
        """Every path in the device's Controls table, so a simulated project
        answers for the controls it lists."""
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            path, fmt = _read_path_cell(str(row.get("path") or ""))
            if not path.startswith("/") or path in self._params:
                continue
            kind = str(row.get("type") or "").strip().lower() or {
                "norm": "level", "string": "text"}.get(fmt, "number")
            low = _number(row.get("min"))
            high = _number(row.get("max"))
            unit = str(row.get("unit") or "")
            if kind == "boolean":
                self._params[path] = _param("boolean", False)
            elif kind == "text":
                self._params[path] = _param("string", path.rsplit("/", 1)[-1])
            elif kind == "meter":
                self._params[path] = _param(
                    "float", -40.0, min=-100.0, max=0.0, unit=unit or "dB", ro=True)
            elif kind == "level":
                self._params[path] = _param(
                    "float", -20.0, min=-100.0, max=12.0, unit=unit or "dB")
            else:
                lo = low if low is not None else -100.0
                hi = high if high is not None else 100.0
                self._params[path] = _param(
                    "float", min(max(0.0, lo), hi), min=lo, max=hi, unit=unit)

    # ── Connection ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        self._subs[client_id] = set()
        self._pending[client_id] = {}
        self._awaiting_ack.discard(client_id)
        return None

    def _latest_client_id(self) -> str | None:
        if not self._clients:
            return None
        return next(reversed(self._clients))

    # ── Dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        m = MESSAGE_RE.match(text)
        if m is None:
            return None
        is_reply, word, rest = m.group(1) == "@", m.group(2), m.group(3).strip()
        client = self._latest_client_id()
        if is_reply:
            if word == "publish" and client is not None:
                self._awaiting_ack.discard(client)
                self._schedule_drain(client)
            return None
        body: Any = None
        if rest:
            try:
                body = json.loads(rest)
            except json.JSONDecodeError:
                return self._line(word, {"error": "Invalid JSON"})
        handler = getattr(self, "_c_" + word, None)
        if handler is None:
            return self._line(word, {"error": "Unknown command"})
        try:
            reply = handler(body, client)
        except Exception:
            logger.exception("%s: error in %s", self.name, word)
            return self._line(word, {"error": "Internal error"})
        return self._line(word, reply)

    @staticmethod
    def _line(word: str, body: Any) -> bytes:
        if body is None:
            return f"@{word}".encode() + LINE_END
        return f"@{word} {json.dumps(body, separators=(',', ':'))}".encode() + LINE_END

    # ── Paths and formats ──

    def _under(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return [p for p in self._params if p.startswith(prefix)]

    def _render(self, param: dict[str, Any], fmt: str) -> Any:
        kind, value = param["kind"], param["value"]
        if kind == "boolean":
            if fmt == "string":
                return "true" if value else "false"
            return 1 if value else 0
        if kind == "enum":
            options = param["options"]
            if fmt == "string":
                return options[value]
            if fmt == "norm":
                return round(value / (len(options) - 1), 6) if len(options) > 1 else 0
            return value
        if kind == "string":
            return value
        # float
        if fmt == "norm":
            span = param["max"] - param["min"]
            return round((value - param["min"]) / span, 6) if span else 0
        if fmt == "string":
            return f"{value:g}{param.get('unit', '')}"
        return value

    def _parse(self, param: dict[str, Any], fmt: str, raw: Any) -> Any:
        """The value a set applies, clamped to the parameter's range (p.8).
        Raises ValueError("Invalid value type") when it cannot be read."""
        kind = param["kind"]
        if kind == "string":
            return str(raw)[:64]
        if kind == "boolean":
            if fmt == "string":
                text = str(raw).strip().lower()
                if text in ("true", "false"):
                    return text == "true"
                raise ValueError("Invalid value type")
            if isinstance(raw, bool):
                return raw
            number = _number(raw)
            if number is None:
                raise ValueError("Invalid value type")
            return number != 0
        if kind == "enum":
            options = param["options"]
            if fmt == "string":
                if raw in options:
                    return options.index(raw)
                raise ValueError("Invalid value type")
            number = _number(raw)
            if number is None:
                raise ValueError("Invalid value type")
            if fmt == "norm":
                number = min(1.0, max(0.0, number)) * (len(options) - 1)
            return int(min(len(options) - 1, max(0, round(number))))
        # float
        if fmt == "string":
            found = NUMBER_RE.search(str(raw))
            if found is None:
                raise ValueError("Invalid value type")
            number = float(found.group(0))
        else:
            number = _number(raw)
            if number is None:
                raise ValueError("Invalid value type")
            if fmt == "norm":
                number = param["min"] + min(1.0, max(0.0, number)) * (param["max"] - param["min"])
        return round(min(param["max"], max(param["min"], number)), 6)

    def _item(self, path: str, fmt: str) -> dict[str, Any]:
        return {"format": fmt, "path": path, "value": self._render(self._params[path], fmt)}

    @staticmethod
    def _format_of(body: dict[str, Any]) -> str | None:
        fmt = body.get("format", "variant")
        return fmt if fmt in FORMATS else None

    # ── Commands ──

    def _c_get(self, body: Any, client: str | None) -> Any:
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            return {"error": "Missing required parameter"}
        path = body["path"]
        fmt = self._format_of(body)
        if fmt is None:
            return {"path": path, "error": "Invalid format specified"}
        if path in self._params:
            return self._item(path, fmt)
        below = self._under(path)
        if below:
            return [self._item(p, fmt) for p in below]
        return {"path": path, "error": "Parameter not found"}

    def _c_set(self, body: Any, client: str | None) -> Any:
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            return {"error": "Missing required parameter"}
        path = body["path"]
        fmt = self._format_of(body)
        if fmt is None:
            return {"path": path, "error": "Invalid format specified"}
        param = self._params.get(path)
        if param is None:
            return {"path": path, "error": "Parameter not found"}
        if "value" not in body:
            return {"path": path, "error": "Missing required parameter"}
        if param.get("ro"):
            return {"path": path, "error": "Parameter is read only"}
        try:
            value = self._parse(param, fmt, body["value"])
        except ValueError as exc:
            return {"path": path, "error": str(exc)}
        self._write(path, value)
        return self._item(path, fmt)

    def _c_subscribe(self, body: Any, client: str | None) -> Any:
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            return {"error": "Missing required parameter"}
        path = body["path"]
        fmt = self._format_of(body)
        if fmt is None:
            return {"path": path, "error": "Invalid format specified"}
        paths = [path] if path in self._params else self._under(path)
        if not paths:
            return {"path": path, "error": "Parameter not found"}
        subs = self._subs.setdefault(client or "", set())
        for p in paths:
            subs.add((p, fmt))
        if path in self._params:
            return self._item(path, fmt)
        return [self._item(p, fmt) for p in paths]

    def _c_unsubscribe(self, body: Any, client: str | None) -> Any:
        subs = self._subs.setdefault(client or "", set())
        pending = self._pending.setdefault(client or "", {})
        if body is None:
            subs.clear()
            pending.clear()
            return None
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            return {"error": "Missing required parameter"}
        path = body["path"]
        prefix = path.rstrip("/") + "/"
        gone = {k for k in subs if k[0] == path or k[0].startswith(prefix)}
        subs.difference_update(gone)
        for key in gone:
            pending.pop(key, None)
        return {"path": path}

    def _c_exec(self, body: Any, client: str | None) -> Any:
        if not isinstance(body, dict) or not body.get("path") or not body.get("command"):
            return {"error": "Missing required parameter"}
        logger.info("%s: exec %s %s %s", self.name, body.get("path"),
                    body.get("command"), body.get("arguments"))
        return None

    # ── Changes and publishing ──

    def _write(self, path: str, value: Any) -> None:
        param = self._params[path]
        if param["value"] == value:
            return
        param["value"] = value
        key = PATH_UI_KEYS.get(path)
        if key is not None:
            self._set_ui(key, value)
        self._changed(path)

    def _changed(self, path: str) -> None:
        for client, subs in self._subs.items():
            if client not in self._clients:
                continue
            queued = self._pending.setdefault(client, {})
            for key in subs:
                if key[0] == path:
                    queued.pop(key, None)
                    queued[key] = None
            self._schedule_drain(client)

    def _schedule_drain(self, client: str) -> None:
        if client in self._awaiting_ack or not self._pending.get(client):
            return
        try:
            asyncio.get_running_loop().create_task(self._drain(client))
        except RuntimeError:
            pass  # no loop: nothing is connected to publish to

    async def _drain(self, client: str) -> None:
        queued = self._pending.get(client)
        if client in self._awaiting_ack or not queued or client not in self._clients:
            return
        key = next(iter(queued))
        queued.pop(key)
        path, fmt = key
        if path not in self._params:
            return
        self._awaiting_ack.add(client)
        line = f"publish {json.dumps(self._item(path, fmt), separators=(',', ':'))}"
        await self.push_to(client, line.encode() + LINE_END)

    # ── Simulator UI ──

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
        path = UI_KEYS.get(key)
        if path is None:
            return
        param = self._params[path]
        try:
            if param["kind"] == "boolean":
                new: Any = bool(value)
            elif param["kind"] == "enum":
                new = int(min(len(param["options"]) - 1, max(0, round(float(value)))))
            else:
                new = round(min(param["max"], max(param["min"], float(value))), 6)
        except (TypeError, ValueError):
            return
        if param["value"] != new:
            param["value"] = new
            self._changed(path)
