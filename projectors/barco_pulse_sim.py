"""
Barco Pulse Projector — Simulator.

Implements the projector side of the Barco Pulse API: JSON-RPC 2.0
documents over TCP 9090 with no framing delimiter (messages are split
on balanced top-level braces, exactly like the driver does).

Behavior mirrored from the Pulse API reference catalogs:

- ``property.get`` (single property or array — array answers with a
  name-keyed dict), ``property.set`` (validated per property; setters
  answer ``true``), ``property.subscribe`` / ``property.unsubscribe``.
- Subscribed properties emit ``property.changed`` notifications (no
  ``id`` member) after every change — including changes made from the
  Simulator UI controls, so a connected driver sees live pushes.
- ``system.poweron`` / ``poweroff`` walk the documented state machine
  (standby → conditioning → on, on → deconditioning → standby) with a
  configurable transition delay; ``illumination.state`` follows.
  ``system.gotoready`` / ``system.gotoeco`` switch standby modes.
- ``authenticate`` checks the configured pass code (config
  ``auth_code``; empty = every code accepted, matching a projector
  with no restriction). Wrong code answers a JSON-RPC error.
- ``image.source.list`` returns the configured source list; setting
  an unknown source name answers an error (real projectors validate).
- Lens methods track step counters so tests can assert motion;
  ``motorized_lens: false`` makes zoom/focus answer "Method not
  found" — the dynamic-API case the driver must tolerate.
- ``environment.getcontrolblocks`` answers the documented
  temperature / fan-speed dictionaries.

Driver side: ``projectors/barco_pulse.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


DEFAULT_SOURCES = ["DVI 1", "DisplayPort 1", "HDBaseT", "HDMI", "SDI"]

POWER_STATES = [
    "boot", "eco", "standby", "ready",
    "conditioning", "on", "deconditioning", "service", "error",
]

ORIENTATIONS = [
    "DESKTOP_FRONT", "DESKTOP_REAR", "CEILING_FRONT", "CEILING_REAR",
]

# Pulse property name -> (sim state key, writable).
PROPERTIES: dict[str, tuple[str, bool]] = {
    "system.state": ("system_state", False),
    "illumination.state": ("illumination_state", False),
    "illumination.sources.laser.power": ("laser_power", True),
    "image.window.main.source": ("source", True),
    "optics.shutter.position": ("shutter_position", False),
    "optics.shutter.target": ("shutter_target", True),
    "image.brightness": ("brightness", True),
    "image.contrast": ("contrast", True),
    "image.saturation": ("saturation", True),
    "image.orientation": ("orientation", True),
    "system.eco.enable": ("eco_enable", True),
    "image.testpattern.show": ("testpattern_show", True),
    "image.testpattern.selected": ("testpattern_selected", True),
    "environment.alarmstate": ("alarm_state", False),
    "system.modelname": ("model", False),
    "system.serialnumber": ("serial_number", False),
    "system.firmwareversion": ("firmware_version", False),
    "system.articlenumber": ("article_number", False),
    "system.familyname": ("family_name", False),
    "network.device.lan.hwaddress": ("mac_address", False),
}

_STATE_TO_PROP = {key: prop for prop, (key, _w) in PROPERTIES.items()}

# Numeric write constraints (property -> (min, max)), per the catalog.
_NUMERIC_RANGES = {
    "illumination.sources.laser.power": (0.0, 100.0),
    "image.brightness": (-1.0, 1.0),
    "image.contrast": (0.0, 2.0),
    "image.saturation": (0.0, 2.0),
}

_LENS_COUNTERS = {
    "optics.lensshift.vertical.stepreverse": ("lensshift_v", 1),
    "optics.lensshift.vertical.stepforward": ("lensshift_v", -1),
    "optics.lensshift.horizontal.stepreverse": ("lensshift_h", -1),
    "optics.lensshift.horizontal.stepforward": ("lensshift_h", 1),
    "optics.zoom.stepforward": ("zoom_position", 1),
    "optics.zoom.stepreverse": ("zoom_position", -1),
    "optics.focus.stepforward": ("focus_position", 1),
    "optics.focus.stepreverse": ("focus_position", -1),
}


def _extract_json(buf: bytearray) -> tuple[bytes | None, bytearray]:
    """Pull one complete top-level JSON object off the buffer."""
    depth = 0
    in_string = False
    escaped = False
    start = -1
    for i, byte in enumerate(buf):
        if start < 0:
            if byte == 0x7B:
                start = i
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte == 0x7B:
            depth += 1
        elif byte == 0x7D:
            depth -= 1
            if depth == 0:
                return bytes(buf[start:i + 1]), buf[i + 1:]
    if start < 0:
        return None, bytearray()
    return None, buf[start:]


class BarcoPulseSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "barco_pulse",
        "name": "Barco Pulse Projector Simulator",
        "category": "projector",
        "transport": "tcp",
        "default_port": 9090,
        "initial_state": {
            "system_state": "standby",
            "illumination_state": "Off",
            "laser_power": 80.0,
            "source": "HDMI",
            "shutter_position": "Open",
            "shutter_target": "Open",
            "brightness": 0.0,
            "contrast": 1.0,
            "saturation": 1.0,
            "orientation": "DESKTOP_FRONT",
            "eco_enable": False,
            "testpattern_show": False,
            "testpattern_selected": "internal:Cross Hatch",
            "alarm_state": "Ok",
            "model": "F80-4K12",
            "serial_number": "2590123456",
            "firmware_version": "2.1.4",
            "article_number": "R9005947",
            "family_name": "F80",
            "mac_address": "00:0D:0A:01:64:39",
            "temperature_inlet": 25.5,
            "temperature_outlet": 29.4,
            "lensshift_h": 0,
            "lensshift_v": 0,
            "zoom_position": 0,
            "focus_position": 0,
        },
        "controls": [
            {
                "type": "select",
                "key": "system_state",
                "options": POWER_STATES,
                "label": "Power State",
            },
            {
                "type": "select",
                "key": "source",
                "options": DEFAULT_SOURCES,
                "label": "Source",
            },
            {
                "type": "select",
                "key": "shutter_position",
                "options": ["Open", "Closed"],
                "label": "Shutter",
            },
            {
                "type": "slider",
                "key": "laser_power",
                "min": 0,
                "max": 100,
                "step": 1,
                "label": "Laser Power (%)",
            },
            {
                "type": "slider",
                "key": "temperature_inlet",
                "min": 15,
                "max": 60,
                "step": 0.5,
                "label": "Inlet Temp (°C)",
            },
            {"type": "indicator", "key": "illumination_state",
             "label": "Light Source"},
            {"type": "indicator", "key": "alarm_state", "label": "Alarm"},
        ],
        "delays": {"command_response": 0.005},
        "error_modes": {
            "overtemp_alarm": {
                "description": (
                    "Report an Error alarm state (as after a cooling "
                    "fault) until cleared"
                ),
                "set_state": {"alarm_state": "Error"},
            },
        },
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Raw (no-delimiter) mode: JSON documents are brace-balanced.
        self._delimiter = None
        self._line_mode = False
        self._rx = bytearray()
        self._subscribed: set[str] = set()
        self._sources: list[str] = list(
            self.config.get("sources", DEFAULT_SOURCES)
        )
        # Pending timed power transition (real-socket mode).
        self._transition_task: asyncio.Task | None = None
        # Non-None only while a request is being handled: change
        # notifications triggered by the request are appended to the
        # response; outside a request they are pushed asynchronously.
        self._notifications: list[bytes] | None = None

    # ── Simulator UI integration ──

    def set_state(self, key: str, value: Any) -> None:
        """UI control changes push property.changed to subscribers."""
        changed = self.state.get(key) != value
        super().set_state(key, value)
        if not changed:
            return
        prop = _STATE_TO_PROP.get(key)
        if prop and prop in self._subscribed:
            self._push_notification({prop: value})
        if key == "system_state":
            illum = "On" if value == "on" else "Off"
            if self.state.get("illumination_state") != illum:
                super().set_state("illumination_state", illum)
                if "illumination.state" in self._subscribed:
                    self._push_notification({"illumination.state": illum})

    def _push_notification(self, pairs: dict[str, Any]) -> None:
        data = _notification(pairs)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.push(data))

    # ── Command handling ──

    def handle_command(self, data: bytes) -> bytes | None:
        self._rx.extend(data)
        out = bytearray()
        while True:
            frame, self._rx = _extract_json(self._rx)
            if frame is None:
                break
            reply = self._handle_message(frame)
            if reply:
                out.extend(reply)
        return bytes(out) if out else None

    def _handle_message(self, frame: bytes) -> bytes:
        try:
            msg = json.loads(frame.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(None, -32700, "Parse error")
        if not isinstance(msg, dict):
            return _error(None, -32600, "Invalid Request")

        rid = msg.get("id")
        method = str(msg.get("method", ""))
        params = msg.get("params") or {}

        # Notifications collected during this request are appended
        # after the response, matching real ordering (confirm first,
        # change notification after).
        self._notifications = []
        response = self._dispatch(method, params, rid)
        notifications = b"".join(self._notifications)
        self._notifications = None
        if rid is None:
            # Fire-and-forget request: no response message.
            return notifications
        return response + notifications

    def _dispatch(self, method: str, params: Any, rid: Any) -> bytes:
        if method == "authenticate":
            expected = str(self.config.get("auth_code", "") or "")
            supplied = str((params or {}).get("code", ""))
            if expected and supplied != expected:
                return _error(rid, -32000, "Authentication failed")
            return _result(rid, True)

        if method == "property.get":
            return self._prop_get(params, rid)
        if method == "property.set":
            return self._prop_set(params, rid)
        if method == "property.subscribe":
            return self._prop_subscribe(params, rid, subscribe=True)
        if method == "property.unsubscribe":
            return self._prop_subscribe(params, rid, subscribe=False)

        if method == "image.source.list":
            return _result(rid, list(self._sources))

        if method == "system.poweron":
            self._power_transition("conditioning", "on")
            return _result(rid, None)
        if method == "system.poweroff":
            self._power_transition("deconditioning", "standby")
            return _result(rid, None)
        if method == "system.gotoready":
            if self.state.get("system_state") in ("standby", "eco"):
                self._set_prop_value("system.state", "ready")
            return _result(rid, None)
        if method == "system.gotoeco":
            if self.state.get("system_state") in ("standby", "ready"):
                self._set_prop_value("system.state", "eco")
            return _result(rid, None)

        if method == "optics.shutter.toggle":
            new = ("Closed" if self.state.get("shutter_position") == "Open"
                   else "Open")
            self._set_prop_value("optics.shutter.target", new)
            self._set_prop_value("optics.shutter.position", new)
            return _result(rid, None)

        if method == "optics.shifttocenter":
            super().set_state("lensshift_h", 0)
            super().set_state("lensshift_v", 0)
            return _result(rid, None)

        if method in _LENS_COUNTERS:
            if (method.startswith(("optics.zoom", "optics.focus"))
                    and not self.config.get("motorized_lens", True)):
                return _error(rid, 32601, f"Method not found: {method}")
            key, sign = _LENS_COUNTERS[method]
            steps = int((params or {}).get("steps", 0) or 0)
            super().set_state(key, int(self.state.get(key, 0)) + sign * steps)
            return _result(rid, None)

        if method == "environment.getcontrolblocks":
            return self._control_blocks(params, rid)

        return _error(rid, 32601, f"Method not found: {method}")

    # ── property.* ──

    def _prop_get(self, params: Any, rid: Any) -> bytes:
        prop = (params or {}).get("property")
        if isinstance(prop, str):
            if prop not in PROPERTIES:
                return _error(rid, -32602, f"Property not found: {prop}")
            key, _writable = PROPERTIES[prop]
            return _result(rid, self.state.get(key))
        if isinstance(prop, list):
            values: dict[str, Any] = {}
            for name in prop:
                if name not in PROPERTIES:
                    return _error(rid, -32602, f"Property not found: {name}")
                key, _writable = PROPERTIES[name]
                values[name] = self.state.get(key)
            return _result(rid, values)
        return _error(rid, -32602, "Invalid params")

    def _prop_set(self, params: Any, rid: Any) -> bytes:
        prop = (params or {}).get("property")
        if not isinstance(prop, str) or "value" not in (params or {}):
            return _error(rid, -32602, "Invalid params")
        if prop not in PROPERTIES:
            return _error(rid, -32602, f"Property not found: {prop}")
        key, writable = PROPERTIES[prop]
        if not writable:
            return _error(rid, -32602, f"Property is read only: {prop}")
        value = params["value"]

        if prop in _NUMERIC_RANGES:
            lo, hi = _NUMERIC_RANGES[prop]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return _error(rid, -32602, "Invalid value type")
            if value < lo or value > hi:
                return _error(rid, -32602, "Value out of range")
            value = float(value)
        elif prop == "image.window.main.source":
            if value not in self._sources:
                return _error(rid, -32602, f"Unknown source: {value}")
        elif prop == "image.orientation":
            if value not in ORIENTATIONS:
                return _error(rid, -32602, f"Invalid orientation: {value}")
        elif prop in ("system.eco.enable", "image.testpattern.show"):
            if not isinstance(value, bool):
                return _error(rid, -32602, "Invalid value type")
        elif prop == "optics.shutter.target":
            if value not in ("Open", "Closed"):
                return _error(rid, -32602, f"Invalid target: {value}")

        self._set_prop_value(prop, value)
        if prop == "optics.shutter.target":
            # The motorized shutter follows its target immediately.
            self._set_prop_value("optics.shutter.position", value)
        return _result(rid, True)

    def _prop_subscribe(self, params: Any, rid: Any, subscribe: bool) -> bytes:
        prop = (params or {}).get("property")
        names = [prop] if isinstance(prop, str) else prop
        if not isinstance(names, list):
            return _error(rid, -32602, "Invalid params")
        for name in names:
            if name not in PROPERTIES:
                return _error(rid, -32602, f"Property not found: {name}")
        for name in names:
            if subscribe:
                self._subscribed.add(name)
            else:
                self._subscribed.discard(name)
        return _result(rid, True)

    def _set_prop_value(self, prop: str, value: Any) -> None:
        """Mutate state and queue a change notification if subscribed."""
        key, _writable = PROPERTIES[prop]
        changed = self.state.get(key) != value
        super().set_state(key, value)
        if changed and prop in self._subscribed:
            self._queue_notification({prop: value})
        if prop == "system.state":
            illum = "On" if value == "on" else "Off"
            if self.state.get("illumination_state") != illum:
                super().set_state("illumination_state", illum)
                if "illumination.state" in self._subscribed:
                    self._queue_notification({"illumination.state": illum})

    def _queue_notification(self, pairs: dict[str, Any]) -> None:
        if self._notifications is not None:
            self._notifications.append(_notification(pairs))
        else:
            self._push_notification(pairs)

    # ── Power state machine ──

    def _power_transition(self, via: str, target: str) -> None:
        current = self.state.get("system_state")
        if target == "on" and current not in ("standby", "ready"):
            return  # documented: ignored when on or in transition
        if target == "standby" and current not in ("on",):
            return
        delay = float(self.config.get("transition_delay", 1.0))
        self._set_prop_value("system.state", via)
        if delay <= 0:
            self._set_prop_value("system.state", target)
            return
        if self._transition_task and not self._transition_task.done():
            self._transition_task.cancel()

        async def _finish() -> None:
            await asyncio.sleep(delay)
            self._set_prop_value("system.state", target)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._set_prop_value("system.state", target)
            return
        self._transition_task = loop.create_task(_finish())

    # ── Environment ──

    def _control_blocks(self, params: Any, rid: Any) -> bytes:
        value_type = (params or {}).get("valuetype")
        if value_type == "Temperature":
            return _result(rid, {
                "environment.temperature.inlet":
                    self.state.get("temperature_inlet"),
                "environment.temperature.outlet":
                    self.state.get("temperature_outlet"),
                "environment.temperature.mainboard": 40.4,
            })
        if value_type == "Speed":
            return _result(rid, {
                "environment.fan.pcb.tacho": 1400,
                "environment.fan.psu.tacho": 1450,
            })
        return _result(rid, {})


def _result(rid: Any, result: Any) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": rid, "result": result}
    ).encode("utf-8")


def _error(rid: Any, code: int, message: str) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": rid,
         "error": {"code": code, "message": message}}
    ).encode("utf-8")


def _notification(pairs: dict[str, Any]) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0",
        "method": "property.changed",
        "params": {"property": [{k: v} for k, v in pairs.items()]},
    }).encode("utf-8")
