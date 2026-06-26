"""
QSC Q-SYS QRC — Simulator.

Implements the QRC protocol (JSON-RPC 2.0 with null-terminated frames)
on TCP port 1710. Behaves like a Q-SYS Core for everything the driver
under test exercises:

- Sends an ``EngineStatus`` event on connect (matches real Core
  behavior — the docs explicitly say the Core "automatically deploys
  EngineStatus to return the status of the Q-SYS Core whenever a
  client connects to the QRC port").
- Accepts JSON-RPC methods: Logon, NoOp, StatusGet, Control.Get,
  Control.Set, Component.Get, Component.Set, Component.GetControls,
  Component.GetComponents, ChangeGroup.AddControl,
  ChangeGroup.AddComponentControl, ChangeGroup.Remove,
  ChangeGroup.Poll, ChangeGroup.AutoPoll, ChangeGroup.Invalidate,
  ChangeGroup.Clear, ChangeGroup.Destroy, Mixer.* (per-input,
  per-output, crosspoint), Snapshot.Load, Snapshot.Save,
  LoopPlayer.Start / Stop / Cancel, PA.PageSubmit, PA.PageCancel.
- Maintains a flat (component, control) -> value state map seeded with
  a representative conferencing room. Set methods mutate state; Get
  methods return current values; the change-group polling loop pushes
  deltas back to subscribed clients on the configured AutoPoll rate.
- Tracks per-client change groups (separate from per-block driver
  state) — Q-SYS allows up to 4 change groups per connection, so the
  simulator enforces that limit too for fidelity.

Driver side: ``audio/qsc_qrc.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


# QRC framing: each request and response is null-terminated.
FRAME_TERMINATOR = b"\x00"

# Maximum change groups per connection (per QSC docs).
MAX_CHANGE_GROUPS = 4

# ── Topology ──
# A realistic Q-SYS design mirroring the shapes a real Core returns (verified
# against a live Core 24f): a Gain, a 4x4 Matrix Mixer, an Audio Router with
# boolean output.N.input.M.select crosspoints (NOT select.N integers — that was
# the old driver's wrong model), a Snapshot Controller, and a Loop Player, plus
# two Named Controls. Each control carries a real Type + Direction so the driver
# under test exercises Float/Integer/Boolean/Text/Time/State-Trigger/Trigger
# handling and Read Only vs Read/Write.
#
# COMPONENT_METADATA[name] = (qsys_type, [control specs]); a control spec is
# {Name, Type, Direction, ValueMin?, ValueMax?}. DEFAULT_STATE is derived from
# it (component, control) -> seed value.

_GAIN_RANGE = {"ValueMin": -100.0, "ValueMax": 20.0}
_XP_RANGE = {"ValueMin": -100.0, "ValueMax": 10.0}


def _build_topology() -> tuple[
    dict[str, tuple[str, list[dict[str, Any]]]],
    dict[tuple[str | None, str], Any],
]:
    meta: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    state: dict[tuple[str | None, str], Any] = {}

    def ctrl(name, qtype, direction="Read/Write", rng=None, seed=None):
        spec = {"Name": name, "Type": qtype, "Direction": direction}
        if rng:
            spec.update(rng)
        return spec, seed

    def add(comp, qtype, specs):
        controls = []
        for spec, seed in specs:
            controls.append(spec)
            if spec["Type"] != "Trigger":
                if seed is None:
                    seed = (False if spec["Type"] == "Boolean"
                            else 0 if spec["Type"] in ("Integer", "State Trigger")
                            else 0.0 if spec["Type"] in ("Float", "Time")
                            else "")
                state[(comp, spec["Name"])] = seed
        meta[comp] = (qtype, controls)

    # Gain
    add("PgmGain", "gain", [
        ctrl("gain", "Float", rng=_GAIN_RANGE, seed=-100.0),
        ctrl("mute", "Boolean"), ctrl("invert", "Boolean"),
        ctrl("bypass", "Boolean"),
    ])
    # 4x4 Matrix Mixer
    mix = []
    for i in range(1, 5):
        mix += [ctrl(f"input.{i}.gain", "Float", rng=_XP_RANGE, seed=0.0),
                ctrl(f"input.{i}.mute", "Boolean"),
                ctrl(f"input.{i}.label", "Text", seed=str(i))]
        for o in range(1, 5):
            mix.append(ctrl(f"input.{i}.output.{o}.gain", "Float", rng=_XP_RANGE,
                            seed=(10.0 if i == o else -100.0)))
    for o in range(1, 5):
        mix += [ctrl(f"output.{o}.gain", "Float", rng=_XP_RANGE, seed=0.0),
                ctrl(f"output.{o}.mute", "Boolean"),
                ctrl(f"output.{o}.label", "Text", seed=str(o))]
    add("PrgMix", "mixer", mix)
    # Audio Router (boolean crosspoints + select.N + mute.N)
    rt = []
    for o in range(1, 5):
        rt.append(ctrl(f"mute.{o}", "Boolean"))
    for o in range(1, 5):
        for i in range(1, 5):
            rt.append(ctrl(f"output.{o}.input.{i}.select", "Boolean",
                           seed=(i == o)))
    for o in range(1, 5):
        rt.append(ctrl(f"select.{o}", "Integer",
                       rng={"ValueMin": 1.0, "ValueMax": 4.0}, seed=o))
    add("PrgmRouter", "router_with_output", rt)
    # Snapshot Controller (8 snapshots)
    sn = []
    for n in range(1, 9):
        sn += [ctrl(f"last.{n}", "Boolean", direction="Read Only"),
               ctrl(f"load.{n}", "State Trigger", seed=0),
               ctrl(f"load.state.{n}", "Integer",
                    rng={"ValueMin": 0.0, "ValueMax": 2.0}, seed=0),
               ctrl(f"load.trigger.{n}", "Trigger"),
               ctrl(f"save.{n}", "Trigger")]
    sn += [ctrl("ramp.time", "Float", rng={"ValueMin": 0.0, "ValueMax": 60.0},
                seed=0.0),
           ctrl("load.next", "Trigger"), ctrl("load.prev", "Trigger")]
    add("RoomScenes", "snapshot_controller", sn)
    # Loop Player (4 outputs)
    lp = []
    for o in range(1, 5):
        lp += [ctrl(f"output.{o}.gain", "Float", rng=_GAIN_RANGE, seed=0.0),
               ctrl(f"output.{o}.elapsed", "Time", direction="Read Only"),
               ctrl(f"output.{o}.remaining", "Time", direction="Read Only"),
               ctrl(f"output.{o}.status", "Text", direction="Read Only"),
               ctrl(f"output.{o}.next", "Text", direction="Read Only")]
    add("BgmLoop", "loop_player", lp)

    # Named Controls (component=None).
    state[(None, "Volume")] = -10.0
    state[(None, "Mute")] = False
    return meta, state


# (qsys_type, [control specs]) per component, plus the seeded state.
COMPONENT_METADATA, DEFAULT_STATE = _build_topology()

# (component_or_None, control_name) -> Q-SYS control Type, so Get/poll
# responses can format the human String accurately (Time/Text/Boolean).
_CONTROL_TYPES: dict[tuple[str | None, str], str] = {}
for _comp, (_qt, _specs) in COMPONENT_METADATA.items():
    for _spec in _specs:
        _CONTROL_TYPES[(_comp, _spec["Name"])] = _spec["Type"]
_CONTROL_TYPES[(None, "Volume")] = "Float"
_CONTROL_TYPES[(None, "Mute")] = "Boolean"


def _coerce_qsys_value(value: Any, target_type: str = "auto") -> Any:
    """Normalize an inbound JSON value to a stored state value.

    target_type can be 'boolean', 'float', 'integer', 'string', or
    'auto' (best-guess from the value's runtime type).
    """
    if value is None:
        return None
    if target_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).lower() in ("true", "1", "on", "yes")
    if target_type == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0
    if target_type == "integer":
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return 0
    if target_type == "string":
        return str(value)
    return value


def _format_string(value: Any, qtype: str = "auto") -> str:
    """Build the 'String' field Q-SYS attaches to control values.

    Reasonable best effort keyed off the control's declared Type — the
    simulator imitates the formatting real cores use. Drivers under test
    should not rely on exact string content; the ``*__str`` state vars are
    an integrator visibility aid, not an automation surface.
    """
    if value is None:
        return ""
    if qtype == "Time":
        try:
            s = int(float(value))
            return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
        except (ValueError, TypeError):
            return "00:00:00"
    if isinstance(value, bool):
        return "true" if value else "false"
    if qtype == "Text" or isinstance(value, str):
        return str(value)
    if qtype in ("Float",) or isinstance(value, float):
        return f"{value:.1f}dB"
    return str(value)


def _parse_channel_spec(spec: str, max_n: int = 64) -> list[int]:
    """Parse Q-SYS mixer channel-spec strings: '*', '1 2 3', '1-6',
    '1-3 5-9', '1-8 !3', '* !3-5'.
    """
    if spec is None:
        return []
    s = str(spec).strip()
    if not s:
        return []
    s = s.replace(",", " ")

    include: set[int] = set()
    exclude: set[int] = set()

    for tok in s.split():
        tok = tok.strip()
        if not tok:
            continue
        target = exclude if tok.startswith("!") else include
        body = tok[1:] if tok.startswith("!") else tok
        if body == "*":
            target.update(range(1, max_n + 1))
            continue
        if "-" in body:
            try:
                a, b = body.split("-", 1)
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            target.update(range(lo, hi + 1))
        else:
            try:
                target.add(int(body))
            except ValueError:
                continue
    return sorted(include - exclude)


class QSCQRCSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "qsc_qrc",
        "name": "QSC Q-SYS QRC Simulator",
        "category": "audio",
        "transport": "tcp",
        "default_port": 1710,
        # QRC framing: each JSON-RPC frame ends with 0x00. Don't use
        # line-mode in the TCP simulator — set delimiter to the null
        # byte so the framework feeds us one complete frame per
        # handle_command call.
        "delimiter": "\x00",
        "initial_state": {
            "platform": "Core 110f",
            "design_name": "OpenAVC Sim Design",
            "design_code": "qSIM00000001",
            "engine_state": "Active",
            "is_redundant": False,
            "is_emulator": True,
        },
        "controls": [
            {"type": "indicator", "key": "platform", "label": "Platform"},
            {"type": "indicator", "key": "design_name", "label": "Design"},
            {"type": "indicator", "key": "engine_state", "label": "Engine State"},
            {"type": "slider", "key": "Volume_value", "label": "Volume",
             "min": -80, "max": 0, "step": 0.5},
            {"type": "toggle", "key": "Mute_value", "label": "Mute"},
            {"type": "slider", "key": "PgmGain_gain_value",
             "label": "PgmGain (dB)", "min": -100, "max": 20, "step": 0.5},
            {"type": "toggle", "key": "PgmGain_mute_value", "label": "PgmGain Mute"},
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Authoritative state — keyed by (component_or_None, control_name).
        self._qsys_state: dict[tuple[str | None, str], Any] = dict(DEFAULT_STATE)

        # Per-client state.
        # _client_groups[client_id][group_id] = {
        #     "controls": list[(component_or_None, control_name)],
        #     "rate": float | None,         # AutoPoll rate (None = manual poll)
        #     "task": asyncio.Task | None,
        #     "first_poll_done": bool,
        #     "dirty": set[(component, control)],
        # }
        self._client_groups: dict[str, dict[str, dict[str, Any]]] = {}
        # Per-client logon flag (always True in sim, but tracked for
        # surface fidelity if we ever wire up auth-required tests).
        self._client_logged_in: dict[str, bool] = {}

        # Bridge UI controls -> state map. When the operator nudges a
        # slider in the simulator UI, propagate to the QSC state and
        # mark the matching control dirty so the next AutoPoll tick
        # surfaces the change.
        self._ui_to_qsys: dict[str, tuple[str | None, str]] = {
            "Volume_value": (None, "Volume"),
            "Mute_value": (None, "Mute"),
            "PgmGain_gain_value": ("PgmGain", "gain"),
            "PgmGain_mute_value": ("PgmGain", "mute"),
        }

    # ── Connection ──

    async def on_client_connected(self, client_id: str) -> bytes | None:
        self._client_groups[client_id] = {}
        self._client_logged_in[client_id] = True

        # The Core auto-pushes EngineStatus on connect. The driver
        # should react by populating core_state, etc.
        engine_status = {
            "jsonrpc": "2.0",
            "method": "EngineStatus",
            "params": {
                "State": self.state.get("engine_state") or "Active",
                "DesignName": self.state.get("design_name") or "Sim",
                "DesignCode": self.state.get("design_code") or "qSIM",
                "IsRedundant": bool(self.state.get("is_redundant")),
                "IsEmulator": bool(self.state.get("is_emulator", True)),
            },
        }
        return self._frame(engine_status)

    # ── Command dispatch ──

    def handle_command(self, data: bytes) -> bytes | None:
        # `readuntil(b"\x00")` includes the terminator in the bytes it
        # hands us, and Python's json.loads rejects a trailing null —
        # strip control characters before decoding.
        try:
            text = data.decode("utf-8", errors="replace").strip("\x00").strip()
        except Exception:
            return None
        if not text:
            return None

        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return self._frame_error(None, -32700, "Parse error")
        if not isinstance(msg, dict):
            return self._frame_error(None, -32600, "Invalid request")

        method = msg.get("method", "")
        params = msg.get("params")
        rid = msg.get("id")
        client_id = self._latest_client_id()
        if client_id is None:
            return None

        try:
            handler = getattr(self, f"_method_{method.replace('.', '_')}", None)
        except Exception:
            handler = None

        if handler is None:
            return self._frame_error(rid, -32601, f"Method not found: {method}")

        try:
            result = handler(client_id, params)
        except QRCSimError as exc:
            return self._frame_error(rid, exc.code, exc.message)
        except Exception:
            logger.exception(f"{self.name}: error in {method}")
            return self._frame_error(rid, -32603, "Server error")

        # Methods that return None are fire-and-forget (no response
        # frame even if an id was supplied). Methods that return a
        # value get wrapped in a JSON-RPC result envelope.
        if result is None:
            return None
        return self._frame_result(rid, result)

    def _latest_client_id(self) -> str | None:
        if not self._clients:
            return None
        return next(reversed(self._clients))

    # ── Connection methods ──

    def _method_Logon(self, client_id: str, params: Any) -> Any:
        # Sim accepts any non-empty user.
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        user = str(params.get("User", "")).strip()
        if not user:
            raise QRCSimError(-32602, "Logon requires a User")
        self._client_logged_in[client_id] = True
        return True

    def _method_NoOp(self, client_id: str, params: Any) -> Any:
        return True

    # ── Status methods ──

    def _method_StatusGet(self, client_id: str, params: Any) -> Any:
        return {
            "Platform": self.state.get("platform") or "Core 110f",
            "State": self.state.get("engine_state") or "Active",
            "DesignName": self.state.get("design_name") or "Sim",
            "DesignCode": self.state.get("design_code") or "qSIM",
            "IsRedundant": bool(self.state.get("is_redundant")),
            "IsEmulator": bool(self.state.get("is_emulator", True)),
            "Status": {"Code": 0, "String": "OK"},
        }

    # ── Control methods (Named Controls) ──

    def _method_Control_Get(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, list):
            raise QRCSimError(-32602, "Control.Get expects an array of names")
        out: list[dict[str, Any]] = []
        for name in params:
            value = self._qsys_state.get((None, str(name)), 0)
            out.append({
                "Name": str(name),
                "Value": value,
                "String": _format_string(value),
            })
        return out

    def _method_Control_Set(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Control.Set expects an object")
        name = params.get("Name")
        if not isinstance(name, str):
            raise QRCSimError(-32602, "Control.Set requires Name")
        value = params.get("Value")
        # Auto-coerce: if the existing value is a bool/float/int, keep
        # the same type. Otherwise store as-is.
        existing = self._qsys_state.get((None, name))
        if isinstance(existing, bool):
            stored = _coerce_qsys_value(value, "boolean")
        elif isinstance(existing, float):
            stored = _coerce_qsys_value(value, "float")
        elif isinstance(existing, int):
            stored = _coerce_qsys_value(value, "integer")
        else:
            stored = value
        self._qsys_state[(None, name)] = stored
        self._mark_dirty(None, name)
        return True

    # ── Component methods ──

    def _method_Component_Get(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Component.Get expects an object")
        comp = params.get("Name")
        if not isinstance(comp, str):
            raise QRCSimError(-32602, "Component.Get requires Name")
        controls = params.get("Controls", [])
        out_controls: list[dict[str, Any]] = []
        for c in controls:
            if not isinstance(c, dict):
                continue
            cname = c.get("Name")
            if not cname:
                continue
            value = self._qsys_state.get((comp, cname), self._seed_default(comp, cname))
            out_controls.append({
                "Name": cname, "Value": value,
                "String": _format_string(value),
                "Position": 0.5,
            })
        return {"Name": comp, "Controls": out_controls}

    def _method_Component_Set(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Component.Set expects an object")
        comp = params.get("Name")
        if not isinstance(comp, str):
            raise QRCSimError(-32602, "Component.Set requires Name")
        controls = params.get("Controls", [])
        response_values = bool(params.get("ResponseValues", False))
        echoed: list[dict[str, Any]] = []
        for c in controls:
            if not isinstance(c, dict):
                continue
            cname = c.get("Name")
            if not cname:
                continue
            value = c.get("Value")
            existing = self._qsys_state.get((comp, cname))
            if isinstance(existing, bool):
                stored = _coerce_qsys_value(value, "boolean")
            elif isinstance(existing, float):
                stored = _coerce_qsys_value(value, "float")
            elif isinstance(existing, int):
                stored = _coerce_qsys_value(value, "integer")
            else:
                # No prior value — guess from the component metadata.
                _qt, specs = COMPONENT_METADATA.get(comp, (None, []))
                meta_type = next(
                    (m["Type"] for m in specs if m.get("Name") == cname),
                    None,
                )
                if meta_type == "Boolean":
                    stored = _coerce_qsys_value(value, "boolean")
                elif meta_type in ("Float", "Integer"):
                    stored = _coerce_qsys_value(
                        value, "integer" if meta_type == "Integer" else "float",
                    )
                else:
                    stored = value
            self._qsys_state[(comp, cname)] = stored
            self._mark_dirty(comp, cname)
            echoed.append({
                "Component": comp, "Name": cname,
                "Value": stored, "String": _format_string(stored),
                "Position": 0.5,
            })
        if response_values:
            return echoed
        return True

    def _method_Component_GetControls(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Component.GetControls expects an object")
        comp = params.get("Name")
        if not isinstance(comp, str):
            raise QRCSimError(-32602, "Component.GetControls requires Name")
        entry = COMPONENT_METADATA.get(comp)
        if entry is None:
            # Synthesize a minimal control list from any state keys we
            # already have for this component.
            keys = [k[1] for k in self._qsys_state if k[0] == comp]
            if not keys:
                raise QRCSimError(7, f"Unknown component: {comp}")
            specs = [{"Name": k, "Type": "Float", "Direction": "Read/Write"}
                     for k in keys]
        else:
            specs = entry[1]
        controls: list[dict[str, Any]] = []
        for spec in specs:
            cname = spec["Name"]
            qtype = spec.get("Type", "String")
            out: dict[str, Any] = {
                "Name": cname,
                "Type": qtype,
                "String": _format_string(
                    self._qsys_state.get((comp, cname)), qtype),
                "Direction": spec.get("Direction", "Read/Write"),
            }
            # Triggers carry no readable value; everything else does.
            if qtype != "Trigger":
                value = self._qsys_state.get(
                    (comp, cname), self._seed_default(comp, cname))
                out["Value"] = value
                out["String"] = _format_string(value, qtype)
                out["Position"] = 0.5
                if "ValueMin" in spec:
                    out["ValueMin"] = spec["ValueMin"]
                    out["ValueMax"] = spec["ValueMax"]
            controls.append(out)
        return {"Name": comp, "Controls": controls}

    def _method_Component_GetComponents(self, client_id: str, params: Any) -> Any:
        out: list[dict[str, Any]] = []
        for name, (qtype, _specs) in COMPONENT_METADATA.items():
            out.append({
                "ID": name, "Name": name, "Type": qtype,
                "Properties": [], "Controls": None, "ControlSource": 2,
            })
        return out

    # ── Change Group methods ──

    def _method_ChangeGroup_AddControl(
        self, client_id: str, params: Any,
    ) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        gid = params.get("Id")
        controls = params.get("Controls", [])
        if not isinstance(gid, str) or not isinstance(controls, list):
            raise QRCSimError(-32602, "AddControl requires Id and Controls")
        group = self._get_or_create_group(client_id, gid)
        existing = {c for c in group["controls"]}
        for name in controls:
            entry = (None, str(name))
            if entry in existing:
                continue
            group["controls"].append(entry)
            existing.add(entry)
        return True

    def _method_ChangeGroup_AddComponentControl(
        self, client_id: str, params: Any,
    ) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        gid = params.get("Id")
        component = params.get("Component", {})
        if (not isinstance(gid, str)
                or not isinstance(component, dict)):
            raise QRCSimError(-32602, "AddComponentControl needs Id, Component")
        comp = component.get("Name")
        controls = component.get("Controls", [])
        if not isinstance(comp, str) or not isinstance(controls, list):
            raise QRCSimError(-32602, "Component.Name and Controls required")
        group = self._get_or_create_group(client_id, gid)
        existing = {c for c in group["controls"]}
        for spec in controls:
            if not isinstance(spec, dict):
                continue
            cname = spec.get("Name")
            if not cname:
                continue
            entry = (comp, str(cname))
            if entry in existing:
                continue
            group["controls"].append(entry)
            existing.add(entry)
        return True

    def _method_ChangeGroup_Remove(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        gid = params.get("Id")
        controls = params.get("Controls", [])
        if not isinstance(gid, str):
            raise QRCSimError(-32602, "Id required")
        group = self._client_groups.get(client_id, {}).get(gid)
        if not group:
            raise QRCSimError(6, f"Unknown change group: {gid}")
        targets = {(None, str(n)) for n in controls}
        group["controls"] = [c for c in group["controls"] if c not in targets]
        return True

    def _method_ChangeGroup_Poll(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        gid = params.get("Id")
        if not isinstance(gid, str):
            raise QRCSimError(-32602, "Id required")
        group = self._client_groups.get(client_id, {}).get(gid)
        if not group:
            raise QRCSimError(6, f"Unknown change group: {gid}")
        return self._build_poll_response(gid, group)

    def _method_ChangeGroup_AutoPoll(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        gid = params.get("Id")
        rate = params.get("Rate")
        if not isinstance(gid, str):
            raise QRCSimError(-32602, "Id required")
        try:
            rate_f = float(rate)
        except (TypeError, ValueError):
            raise QRCSimError(-32602, "Rate must be numeric")
        group = self._get_or_create_group(client_id, gid)
        group["rate"] = max(0.05, rate_f)
        # Cancel any existing pump task and start a fresh one.
        existing_task = group.get("task")
        if existing_task is not None and not existing_task.done():
            existing_task.cancel()
        group["task"] = asyncio.create_task(
            self._autopoll_pump(client_id, gid),
        )
        # The first response carries every control's current value
        # (per QSC docs); the pump task picks up incremental deltas
        # from there.
        return self._build_poll_response(gid, group, force_initial=True)

    def _method_ChangeGroup_Invalidate(
        self, client_id: str, params: Any,
    ) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        gid = params.get("Id")
        group = self._client_groups.get(client_id, {}).get(gid)
        if not group:
            raise QRCSimError(6, f"Unknown change group: {gid}")
        group["first_poll_done"] = False
        # Mark every control dirty so the next poll resends.
        group["dirty"] = set(group["controls"])
        return True

    def _method_ChangeGroup_Clear(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        gid = params.get("Id")
        group = self._client_groups.get(client_id, {}).get(gid)
        if not group:
            raise QRCSimError(6, f"Unknown change group: {gid}")
        group["controls"] = []
        group["dirty"] = set()
        return True

    def _method_ChangeGroup_Destroy(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        gid = params.get("Id")
        groups = self._client_groups.get(client_id, {})
        group = groups.pop(gid, None)
        if group is None:
            return True  # Already gone — idempotent
        task = group.get("task")
        if task is not None and not task.done():
            task.cancel()
        return True

    # ── Mixer methods ──

    def _method_Mixer_SetCrossPointGain(
        self, client_id: str, params: Any,
    ) -> Any:
        return self._mixer_apply(
            params, ["Inputs", "Outputs"], "gain",
            cross=True, value_type="float",
        )

    def _method_Mixer_SetCrossPointMute(
        self, client_id: str, params: Any,
    ) -> Any:
        return self._mixer_apply(
            params, ["Inputs", "Outputs"], "mute",
            cross=True, value_type="boolean",
        )

    def _method_Mixer_SetCrossPointDelay(
        self, client_id: str, params: Any,
    ) -> Any:
        return self._mixer_apply(
            params, ["Inputs", "Outputs"], "delay",
            cross=True, value_type="float",
        )

    def _method_Mixer_SetInputGain(self, client_id: str, params: Any) -> Any:
        return self._mixer_apply(
            params, ["Inputs"], "gain", cross=False, value_type="float",
            channel_axis="input",
        )

    def _method_Mixer_SetInputMute(self, client_id: str, params: Any) -> Any:
        return self._mixer_apply(
            params, ["Inputs"], "mute", cross=False, value_type="boolean",
            channel_axis="input",
        )

    def _method_Mixer_SetOutputGain(self, client_id: str, params: Any) -> Any:
        return self._mixer_apply(
            params, ["Outputs"], "gain", cross=False, value_type="float",
            channel_axis="output",
        )

    def _method_Mixer_SetOutputMute(self, client_id: str, params: Any) -> Any:
        return self._mixer_apply(
            params, ["Outputs"], "mute", cross=False, value_type="boolean",
            channel_axis="output",
        )

    def _mixer_apply(
        self, params: Any, axes: list[str], suffix: str, *,
        cross: bool, value_type: str, channel_axis: str = "input",
    ) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        name = params.get("Name")
        value = params.get("Value")
        if not isinstance(name, str):
            raise QRCSimError(-32602, "Mixer.* requires Name")
        coerced = _coerce_qsys_value(value, value_type)

        if cross:
            inputs = _parse_channel_spec(str(params.get("Inputs", "*")))
            outputs = _parse_channel_spec(str(params.get("Outputs", "*")))
            if not inputs:
                inputs = list(range(1, 9))
            if not outputs:
                outputs = list(range(1, 5))
            for i in inputs:
                for o in outputs:
                    key = (name, f"input.{i}.output.{o}.{suffix}")
                    self._qsys_state[key] = coerced
                    self._mark_dirty(name, key[1])
            return True

        chans = _parse_channel_spec(str(params.get(axes[0], "*")))
        if not chans:
            chans = list(range(1, 9 if channel_axis == "input" else 5))
        for ch in chans:
            key = (name, f"{channel_axis}.{ch}.{suffix}")
            self._qsys_state[key] = coerced
            self._mark_dirty(name, key[1])
        return True

    # ── Snapshot methods ──

    def _method_Snapshot_Load(self, client_id: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise QRCSimError(-32602, "Invalid params")
        # No state mutation in the sim — we just ack.
        return True

    def _method_Snapshot_Save(self, client_id: str, params: Any) -> Any:
        return True

    # ── LoopPlayer methods ──

    def _method_LoopPlayer_Start(self, client_id: str, params: Any) -> Any:
        return True

    def _method_LoopPlayer_Stop(self, client_id: str, params: Any) -> Any:
        return True

    def _method_LoopPlayer_Cancel(self, client_id: str, params: Any) -> Any:
        return True

    # ── PA methods ──

    def _method_PA_PageSubmit(self, client_id: str, params: Any) -> Any:
        # Generate a synthetic PageID for visibility.
        return {"PageID": uuid.uuid4().hex[:8]}

    def _method_PA_PageCancel(self, client_id: str, params: Any) -> Any:
        return True

    # ── Internal helpers ──

    def _get_or_create_group(
        self, client_id: str, gid: str,
    ) -> dict[str, Any]:
        groups = self._client_groups.setdefault(client_id, {})
        if gid in groups:
            return groups[gid]
        if len(groups) >= MAX_CHANGE_GROUPS:
            raise QRCSimError(5, "Change groups exhausted")
        groups[gid] = {
            "controls": [],
            "rate": None,
            "task": None,
            "first_poll_done": False,
            "dirty": set(),
        }
        return groups[gid]

    def _build_poll_response(
        self, gid: str, group: dict[str, Any],
        force_initial: bool = False,
    ) -> dict[str, Any]:
        """Build a {Id, Changes:[...]} payload reflecting either every
        control (first poll / Invalidate / force_initial) or just the
        dirty subset (subsequent polls)."""
        first_poll = force_initial or not group.get("first_poll_done")
        if first_poll:
            keys = list(group["controls"])
            group["first_poll_done"] = True
        else:
            dirty = group.get("dirty", set())
            # Only emit dirty entries that are actually in the group's
            # subscribe list.
            sub = set(group["controls"])
            keys = [k for k in dirty if k in sub]
        group["dirty"] = set()

        changes: list[dict[str, Any]] = []
        for comp, cname in keys:
            value = self._qsys_state.get((comp, cname))
            if value is None:
                value = self._seed_default(comp, cname)
                self._qsys_state[(comp, cname)] = value
            qtype = _CONTROL_TYPES.get((comp, cname), "auto")
            entry: dict[str, Any] = {
                "Name": cname, "Value": value,
                "String": _format_string(value, qtype),
            }
            if comp is not None:
                entry["Component"] = comp
            changes.append(entry)
        return {"Id": gid, "Changes": changes}

    def _mark_dirty(self, comp: str | None, name: str) -> None:
        for groups in self._client_groups.values():
            for group in groups.values():
                if (comp, name) in group["controls"]:
                    group["dirty"].add((comp, name))

    @staticmethod
    def _seed_default(comp: str | None, cname: str) -> Any:
        """Pick a sensible default value for an unseen (component, control)
        pair so Component.Get / poll responses don't return None."""
        n = cname.lower()
        if "mute" in n or "bypass" in n or "invert" in n:
            return False
        if "select" in n or "input" in n.split(".")[0:1] or "source" in n:
            try:
                # routers: select.<n>
                if n.startswith("select."):
                    return 0
            except Exception:
                pass
        if "gain" in n or "level" in n or "delay" in n or "meter" in n:
            return -100.0
        return 0

    async def _autopoll_pump(self, client_id: str, gid: str) -> None:
        """Background task — fires periodic ChangeGroup.Poll deltas to
        the client at the configured AutoPoll rate."""
        try:
            while True:
                groups = self._client_groups.get(client_id, {})
                group = groups.get(gid)
                if group is None:
                    return
                rate = group.get("rate") or 0.25
                await asyncio.sleep(rate)
                # Group might have been destroyed during sleep.
                groups = self._client_groups.get(client_id, {})
                group = groups.get(gid)
                if group is None:
                    return
                # Only emit a frame if there's anything dirty (subsequent
                # polls) — saves wire if nothing changed.
                payload = self._build_poll_response(gid, group)
                if not payload["Changes"]:
                    continue
                # Wire format: top-level result envelope without an id —
                # the driver handles unsolicited Changes payloads.
                envelope = {
                    "jsonrpc": "2.0",
                    "method": "ChangeGroup.Poll",
                    "params": payload,
                }
                await self.push_to(client_id, self._frame(envelope))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(f"{self.name}: autopoll pump error")

    @staticmethod
    def _frame(obj: Any) -> bytes:
        return json.dumps(obj).encode("utf-8") + FRAME_TERMINATOR

    @staticmethod
    def _frame_result(rid: Any, result: Any) -> bytes:
        env = {"jsonrpc": "2.0", "result": result}
        if rid is not None:
            env["id"] = rid
        return QSCQRCSimulator._frame(env)

    @staticmethod
    def _frame_error(rid: Any, code: int, message: str) -> bytes:
        env: dict[str, Any] = {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
        }
        if rid is not None:
            env["id"] = rid
        return QSCQRCSimulator._frame(env)

    # ── Bridge UI controls to QSC state ──

    def set_state(self, key: str, value: Any) -> None:
        super().set_state(key, value)
        mapping = self._ui_to_qsys.get(key)
        if mapping is None:
            return
        comp, cname = mapping
        if isinstance(value, str):
            if value.lower() in ("true", "false"):
                value = value.lower() == "true"
            else:
                try:
                    value = float(value) if "." in value else int(value)
                except ValueError:
                    pass
        existing = self._qsys_state.get((comp, cname))
        if isinstance(existing, bool) and not isinstance(value, bool):
            value = bool(value)
        self._qsys_state[(comp, cname)] = value
        self._mark_dirty(comp, cname)

    # ── Test hooks ──

    def trigger_external_change(
        self, comp: str | None, control: str, value: Any,
    ) -> None:
        """Force a QSC state change as if from the front panel / another
        controller, and mark dirty so subscribed clients receive the
        update on the next AutoPoll tick."""
        self._qsys_state[(comp, control)] = value
        self._mark_dirty(comp, control)


class QRCSimError(Exception):
    """Raised inside method handlers to surface a JSON-RPC error
    response (instead of a generic -32603 server error)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
