"""
Symetrix Cognio Driver.

Controls Symetrix Cognio smart processors (the C20) over the Cognio Control
Protocol: JSON-RPC 2.0 on TCP port 3012, every message ended by a null byte,
both ways. Cognio has no RS-232 control port and the protocol has no sign-in.
It is not the Composer protocol (TCP 48631, controller numbers); Composer
DSPs are `symetrix_composer`.

What it reaches
---------------
Only what the site's designer exposed in Cognio DesignOps: a Control Panel
Group (CPG) with Remote Enable on and a Remote Name, and each control in it
with Remote Enable on and a Remote Name; presets likewise. A control's full
name is ``<group>/<control>``.

The integrator lists the CPG remote names on the device page. On connect the
driver reads each group whole (``NamedControl.Get ["<group>/"]`` answers
every accessible control in it, with its text and its number) and lists the
group as a child entity whose state is its controls: ``<control>`` (the
number) and ``<control>__text`` (what the panel shows: "-6.0 dB", "muted",
a drop list's choice). Presets come from ``Device.GetPresets`` and feed the
Recall Preset picker.

Push vs poll
------------
Push. Every imported control is subscribed (``NamedControl.Subscribe``,
condition ``change``, ``sync`` 1), so the processor sends its value at once
and again whenever it changes, no more often than the configured rate. A
group subscription would cover only its first control (the document says so),
so each control gets its own. A preset recall is not announced, so
``Device.GetLastRecalledPreset`` is polled; the same request is the liveness
probe, because it is a device-level question every processor answers, a
"no preset recalled yet" error included.

Why Python (not a YAML .avcdriver)
----------------------------------
1) JSON-RPC 2.0 with id correlation, and an error the document says carries
   ``"id": null``: only one request is in flight at a time, so an error
   without an id is that request's.
2) The control set is the site's, read from the processor at connect: each
   group is a dynamic child whose schema is the controls the processor
   returned. YAML child types are static.
3) One subscription per control, whose repeated answers come back under the
   subscribing request's id and fan out into child state.

Source: Symetrix, "Using the Cognio Control Protocol"
https://docs.symetrix.co/control/external-control-of-cognio-with-json-rpc/control-protocol.html
(read 2026-10-29, Cognio DesignOps 1.1; archived in the workspace).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.utils.logger import get_logger

try:  # OpenAVC 0.37.1+; older releases refuse the same way with RuntimeError.
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # pragma: no cover - depends on the platform release
    CommandRefusedError = RuntimeError  # type: ignore[misc,assignment]

log = get_logger(__name__)


FRAME_TERMINATOR = b"\x00"
REQUEST_TIMEOUT_S = 5.0
DEFAULT_NOTIFY_RATE_MS = 200

# Prefix of the id a subscription's request is sent under. The processor
# answers every later change under the same id.
SUBSCRIPTION_ID_PREFIX = "sub:"

# The application error codes the document lists, and the JSON-RPC one the
# driver reads (a method this DesignOps release does not have).
ERR_PRESET_NOT_FOUND = -101
ERR_CONTROL_NOT_FOUND = -201
ERR_CONTROL_NOT_WRITABLE = -202
ERR_GROUP_NOT_FOUND = -301
ERR_CONTROL_NOT_READABLE = -401
ERR_METHOD_NOT_FOUND = -32601

# Device.GetPresets lists a group that is not itself remote-enabled under
# this name; it cannot be recalled by name.
UNNAMED_PRESET_GROUP = "<unnamed preset group>"

TEXT_SUFFIX = "__text"


def _safe_id(name: str) -> str:
    """A Control Panel Group name as a child local id (the platform takes
    ``[A-Za-z0-9_-]``). The real name stays in the child's ``name``."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "group"


def _safe_prop(name: str) -> str:
    """A control's remote name as a child state key."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "control"


def _unique(base: str, used: set[str]) -> str:
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def _walk_entries(obj: Any) -> list[dict[str, Any]]:
    """Every ``{"name": ...}`` entry in a result, nested arrays flattened (a
    group name in a Get answers as an array inside the result array)."""
    out: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        if "name" in obj:
            out.append(obj)
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_walk_entries(item))
    return out


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wire_number(value: Any) -> int | float:
    """A whole number goes out as an integer (the document writes a mute as
    0), anything else as a float."""
    number = float(value)
    return int(number) if number.is_integer() else number


def parse_group_rows(value: Any) -> list[str]:
    """The Control Panel Groups table: rows of ``{"name": ...}``. A plain
    string of names separated by commas or new lines is read too."""
    names: list[str] = []
    if isinstance(value, str):
        raw = re.split(r"[,\n]", value)
    elif isinstance(value, list):
        raw = [row.get("name") if isinstance(row, dict) else row for row in value]
    else:
        raw = []
    for item in raw:
        name = str(item or "").strip().strip("/")
        if name and name not in names:
            names.append(name)
    return names


class CognioError(Exception):
    """A JSON-RPC error answer from the processor."""

    def __init__(self, error: Any) -> None:
        if isinstance(error, dict):
            self.code = error.get("code")
            self.message = str(error.get("message") or "")
            self.data = error.get("data")
        else:
            self.code = None
            self.message = str(error)
            self.data = None
        super().__init__(f"{self.code}: {self.message}")


class SymetrixCognioDriver(BaseDriver):
    """Symetrix Cognio smart processor over the Cognio Control Protocol."""

    DRIVER_INFO = {
        "id": "symetrix_cognio",
        "name": "Symetrix Cognio",
        "manufacturer": "Symetrix",
        "category": "audio",
        "version": "1.0.0",
        "min_platform_version": "0.36.0",
        "author": "OpenAVC",
        "description": (
            "Controls Symetrix Cognio smart processors (C20) over the Cognio "
            "Control Protocol, JSON-RPC on TCP port 3012. Lists each Control "
            "Panel Group you name as a child with its live controls, updated "
            "as they change, and recalls and updates the site's presets."
        ),
        "source_url": (
            "https://docs.symetrix.co/control/external-control-of-cognio-with-"
            "json-rpc/control-protocol.html"
        ),
        "tags": ["dsp", "symetrix", "cognio", "json-rpc", "dante", "conferencing"],
        "verified": False,
        "simulated": True,
        "protocols": ["symetrix_cognio"],
        "ports": [3012],
        "transport": "tcp",
        "discovery": {
            # A side-effect-free question every processor answers: either the
            # last recalled preset (a remoteName) or error -101, Cognio's own
            # "Preset not found" code, when none has been recalled since it
            # started. Both are Cognio's alone; port 3012 is Cognio's default.
            "tcp_probe": {
                "port": 3012,
                "send_ascii": (
                    '{"jsonrpc":"2.0","method":"Device.GetLastRecalledPreset",'
                    '"id":"openavc-probe"}\x00'
                ),
                "expect_regex": r'"remoteName"\s*:|"code"\s*:\s*-101\b',
                "extract_manufacturer": "Symetrix",
            },
            # Symetrix's IEEE MA-L block, the same one symetrix_composer
            # declares, so a Symetrix MAC offers both drivers as possible.
            "oui": ["00:0c:d0"],
            "manufacturer_alias": ["symetrix"],
        },
        "compatible_models": [
            {
                "manufacturer": "Symetrix",
                "models": ["Cognio C20"],
                "confidence": "untested",
                "notes": (
                    "Cognio DesignOps 1.0 or later. Recall Preset's list of "
                    "presets needs DesignOps 1.1 (Device.GetPresets); on 1.0 "
                    "type the preset's remote name. Not the Composer DSPs "
                    "(Edge, Radius, Prism): those are Symetrix Composer."
                ),
            },
        ],
        "help": {
            "overview": (
                "Controls a Symetrix Cognio processor through the Cognio Control "
                "Protocol. Each Control Panel Group you list appears under the "
                "device with its controls: a control's number (a fader's level, "
                "a mute's 0 or 1, a drop list's position) and its text as the "
                "panel shows it. Values update as they change on the processor, "
                "from any panel or preset. Set Control, Set Control Text and "
                "Adjust Control change one; Recall Preset and Update Preset "
                "work on the site's presets."
            ),
            "setup": (
                "1. In Cognio DesignOps, open each Control Panel Group to control, "
                "turn on its Remote Enable and give it a Remote Name.\n"
                "2. Turn on Remote Enable for each control in it you need. "
                "DesignOps gives a control a default name (fader1, gain1) if you "
                "leave it blank. Names are case-sensitive.\n"
                "3. For presets, turn on Remote Enable in the Presets Manager and "
                "give each preset a Remote Name.\n"
                "4. Push the site to the processor.\n"
                "5. In OpenAVC, enter the IP address of the Cognio device that "
                "hosts the Control Panel Groups, and list each group's Remote "
                "Name under Control Panel Groups. The port is 3012 unless the "
                "site sets another.\n"
                "After a change to the site, use Refresh on the device's "
                "Control Panel Groups tab to read the controls again."
            ),
        },
        "quick_actions": ["recall_preset"],
        "child_entity_types": {
            "group": {
                "label": "Control Panel Group",
                "label_plural": "Control Panel Groups",
                "dynamic": True,
                "id_format": {"type": "string", "max_length": 128},
                # Each group's controls are its own schema, published at
                # register_child(schema=...).
                "state_variables": {
                    "name": {"type": "string", "label": "Remote Name"},
                    "control_count": {"type": "integer", "label": "Controls"},
                },
                "summary_fields": ["name", "control_count"],
                "label_field": "name",
            },
        },
        "default_config": {
            "host": "",
            "port": 3012,
            "control_groups": [],
            "notify_rate_ms": 200,
            "poll_interval": 10,
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {
                "type": "integer", "default": 3012, "min": 1,
                "max": 65535, "label": "Port",
                "description": (
                    "The processor's control port: 3012 unless the site sets "
                    "another."
                ),
            },
            "control_groups": {
                "type": "table", "label": "Control Panel Groups",
                "row_label": "group",
                "columns": {
                    "name": {
                        "type": "string", "label": "Remote Name",
                        "required": True,
                        "help": (
                            "The group's Remote Name in DesignOps, exactly, "
                            "case included."
                        ),
                    },
                },
                "help": (
                    "Each Control Panel Group to control. Its remote-enabled "
                    "controls are read from the processor when it connects."
                ),
            },
            "notify_rate_ms": {
                "type": "integer", "default": 200,
                "min": 20, "max": 60000, "label": "Update Rate (ms)",
                "description": (
                    "The fastest the processor sends a control's changes. 200 "
                    "ms keeps a fader smooth; raise it for a large site."
                ),
            },
            "poll_interval": {
                "type": "integer", "default": 10, "min": 0,
                "label": "Preset Read Interval (sec)",
                "description": (
                    "How often to read the last recalled preset. The processor "
                    "does not announce a recall."
                ),
            },
        },
        "state_variables": {
            "presets": {
                "type": "string", "label": "Presets",
                "help": (
                    "The remote-enabled presets and preset groups, as a list for "
                    "the Recall Preset picker. Empty before DesignOps 1.1."
                ),
            },
            "preset_count": {"type": "integer", "label": "Presets Available"},
            "last_preset": {
                "type": "string", "label": "Last Recalled Preset",
                "help": (
                    "The remote name of the preset most recently recalled on the "
                    "processor, from any source. Empty when none has been "
                    "recalled since it started."
                ),
            },
            "last_preset_name": {
                "type": "string", "label": "Last Recalled Preset (Name)",
                "help": "That preset's name as DesignOps shows it.",
            },
            "group_count": {"type": "integer", "label": "Control Panel Groups"},
            "control_count": {"type": "integer", "label": "Controls"},
            "controls_loaded": {
                "type": "boolean", "label": "Controls Read",
                "help": "True once the listed groups' controls have been read.",
            },
            "last_error": {
                "type": "string", "label": "Last Error",
                "help": "The processor's most recent refusal, in words.",
            },
        },
        "commands": {
            "set_control": {
                "label": "Set Control",
                "params": {
                    "group": {
                        "type": "child_id", "child_type": "group",
                        "required": True, "label": "Control Panel Group",
                    },
                    "control": {
                        "type": "string", "required": True, "label": "Control",
                        "options_from": {"param": "group", "source": "child_schema"},
                    },
                    "value": {
                        "type": "number", "required": True, "label": "Value",
                        "help": (
                            "The control's number: a fader's level as its text "
                            "shows it (dB for a gain), 1 or 0 for a button, a "
                            "drop list's position."
                        ),
                    },
                },
                "help": "Sets one control to a number, as if moved on its panel.",
            },
            "set_control_text": {
                "label": "Set Control Text",
                "params": {
                    "group": {
                        "type": "child_id", "child_type": "group",
                        "required": True, "label": "Control Panel Group",
                    },
                    "control": {
                        "type": "string", "required": True, "label": "Control",
                        "options_from": {"param": "group", "source": "child_schema"},
                    },
                    "text": {
                        "type": "string", "required": True, "label": "Text",
                        "help": "The value as the control's text reads it.",
                    },
                },
                "help": (
                    "Sets one control from text, the way its panel shows the "
                    "value (a drop list's choice, a label)."
                ),
            },
            "adjust_control": {
                "label": "Adjust Control",
                "params": {
                    "group": {
                        "type": "child_id", "child_type": "group",
                        "required": True, "label": "Control Panel Group",
                    },
                    "control": {
                        "type": "string", "required": True, "label": "Control",
                        "options_from": {"param": "group", "source": "child_schema"},
                    },
                    "offset": {
                        "type": "number", "required": True, "label": "Change By",
                        "help": "Added to the control's number; negative lowers it.",
                    },
                },
                "help": (
                    "Raises or lowers one control's number by the amount given "
                    "(a volume step)."
                ),
            },
            "set_named_control": {
                "label": "Set Control by Name",
                "params": {
                    "name": {
                        "type": "string", "required": True, "label": "Control Name",
                        "help": (
                            "<group>/<control>, or a control's name alone to set "
                            "that control in every Control Panel Group that has "
                            "one by that name."
                        ),
                    },
                    "value": {"type": "number", "required": True, "label": "Value"},
                },
                "help": (
                    "Sets a control by its remote name, including one in a "
                    "group not listed on this device. A name alone sets every "
                    "group's control of that name (every \"mute\", say)."
                ),
            },
            "set_named_control_text": {
                "label": "Set Control Text by Name",
                "params": {
                    "name": {
                        "type": "string", "required": True, "label": "Control Name",
                        "help": "<group>/<control>, or a control's name alone.",
                    },
                    "text": {"type": "string", "required": True, "label": "Text"},
                },
                "help": "Sets a control by its remote name, from text.",
            },
            "recall_preset": {
                "label": "Recall Preset",
                "params": {
                    "preset": {
                        "type": "string", "required": True, "label": "Preset",
                        "options_state": "presets",
                        "help": (
                            "A preset's or preset group's Remote Name. A group "
                            "recalls every preset in it."
                        ),
                    },
                },
                "help": (
                    "Recalls the preset across every processor in the site; its "
                    "controls take the preset's values."
                ),
            },
            "update_preset": {
                "label": "Update Preset",
                "params": {
                    "preset": {
                        "type": "string", "required": True, "label": "Preset",
                        "options_state": "presets",
                    },
                },
                "help": (
                    "Saves the current values of the preset's controls into the "
                    "preset, across every processor in the site."
                ),
                "confirm": (
                    "Replaces the values stored in this preset with the "
                    "controls' current values. Its old values are lost."
                ),
            },
            "refresh_controls": {
                "label": "Read Controls Again",
                "params": {},
                "help": (
                    "Reads the listed Control Panel Groups and the preset list "
                    "from the processor again, after a change to the site."
                ),
            },
        },
    }

    HEALTH_INTERVAL_S = 20.0
    HEALTH_FAULT_MESSAGE = "The processor stopped answering on its control port."

    def __init__(
        self, device_id: str, config: dict[str, Any], state: Any, events: Any,
    ) -> None:
        super().__init__(device_id, config, state, events)
        self._next_id = 1
        # One request in flight at a time: (id, future).
        self._request_lock = asyncio.Lock()
        self._inflight: tuple[Any, asyncio.Future[Any]] | None = None

        # Imported groups.
        #   _group_name:   child local id -> the group's remote name
        #   _group_schema: child local id -> the schema it was registered with
        #   _group_props:  child local id -> {state key: control remote name}
        #   _route:        "<group>/<control>" -> (child local id, state key)
        self._group_name: dict[str, str] = {}
        self._group_schema: dict[str, dict[str, Any]] = {}
        self._group_props: dict[str, dict[str, str]] = {}
        self._route: dict[str, tuple[str, str]] = {}
        self._subscription_ids: dict[str, str] = {}
        self._standing_error = ""

    # ── Lifecycle ──

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ConnectionError(f"[{self.device_id}] No host configured")

    def _transport_kwargs(
        self, transport_type: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        kwargs["delimiter"] = FRAME_TERMINATOR
        return kwargs

    async def _post_connect(self) -> None:
        # A port that opens proves only that something listens on it. Ask the
        # one question every processor answers before reporting connected.
        try:
            await self._ask_last_preset()
        except CognioError:
            pass  # an error answer is still the protocol answering
        except TimeoutError as exc:
            raise ConnectionFaultError(
                "The device accepted the connection on its control port but "
                "did not answer a Cognio Control Protocol request.",
                code="no_response",
            ) from exc

    async def _initial_sync(self) -> None:
        await self._read_presets()
        await self._import_groups()

    async def _close_session(self) -> None:
        if self._inflight is not None:
            _rid, fut = self._inflight
            if not fut.done():
                fut.cancel()
            self._inflight = None
        self._route.clear()
        self._subscription_ids.clear()
        self.set_state("controls_loaded", False)

    # ── JSON-RPC plumbing ──

    async def _call(
        self, method: str, params: Any = None, *, rid: Any = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> Any:
        """Send one request and await its answer. Raises CognioError on an
        error answer, TimeoutError on none, ConnectionError when down."""
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        async with self._request_lock:
            if rid is None:
                # A string id, as every example in Symetrix's document sends.
                rid = str(self._next_id)
                self._next_id += 1
            envelope: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                envelope["params"] = params
            envelope["id"] = rid
            fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._inflight = (rid, fut)
            try:
                await self.transport.send(
                    json.dumps(envelope).encode("utf-8") + FRAME_TERMINATOR
                )
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"[{self.device_id}] {method} got no answer in {timeout:g}s"
                ) from exc
            finally:
                self._inflight = None

    async def on_data_received(self, data: bytes) -> None:
        text = data.decode("utf-8", errors="replace").strip("\x00").strip()
        # "Multiple JSON requests may be combined within a single frame", so
        # read every value in the frame, and a batch array's every member.
        decoder = json.JSONDecoder()
        pos = 0
        while pos < len(text):
            try:
                msg, end = decoder.raw_decode(text, pos)
            except json.JSONDecodeError:
                log.warning(f"[{self.device_id}] Not JSON: {text[pos:pos + 200]!r}")
                return
            for item in msg if isinstance(msg, list) else [msg]:
                if isinstance(item, dict):
                    self._handle_message(item)
            pos = end
            while pos < len(text) and text[pos].isspace():
                pos += 1

    def _handle_message(self, msg: dict[str, Any]) -> None:
        rid = msg.get("id")
        is_error = msg.get("error") is not None
        inflight = self._inflight
        # The document gives an error answer "id": null in one place and the
        # request's id in its examples; with one request in flight, an error
        # without an id is that request's.
        if inflight is not None and (rid == inflight[0] or (rid is None and is_error)):
            fut = inflight[1]
            if not fut.done():
                if is_error:
                    fut.set_exception(CognioError(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
        if is_error:
            if inflight is None or (rid is not None and rid != inflight[0]):
                # A subscription the processor cancelled, or an answer to a
                # request that already timed out.
                err = CognioError(msg["error"])
                log.warning(f"[{self.device_id}] Unclaimed error answer: {err}")
            return
        # A subscription's answers (its first, and every change after it), a
        # Get, an Adjust: apply every control value in the result.
        self._apply_entries(msg.get("result"))

    def _apply_entries(self, result: Any) -> None:
        updates: dict[str, dict[str, Any]] = {}
        for entry in _walk_entries(result):
            route = self._route.get(str(entry.get("name")))
            if route is None:
                continue
            sid, prop = route
            batch = updates.setdefault(sid, {})
            if "value" in entry:
                number = _number(entry.get("value"))
                if number is not None:
                    batch[prop] = number
            if "text" in entry and entry.get("text") is not None:
                batch[prop + TEXT_SUFFIX] = str(entry.get("text"))
        for sid, batch in updates.items():
            if batch and self.is_child_registered("group", sid):
                self.set_child_state_batch("group", sid, batch)

    # ── Reads ──

    async def _ask_last_preset(self) -> None:
        try:
            result = await self._call("Device.GetLastRecalledPreset")
        except CognioError as exc:
            if exc.code == ERR_PRESET_NOT_FOUND:
                # No preset recalled since the processor started.
                self.set_state("last_preset", "")
                self.set_state("last_preset_name", "")
                return
            raise
        if isinstance(result, dict):
            self.set_state("last_preset", str(result.get("remoteName") or ""))
            self.set_state("last_preset_name", str(result.get("name") or ""))

    async def _read_presets(self) -> None:
        try:
            result = await self._call("Device.GetPresets")
        except CognioError as exc:
            if exc.code != ERR_METHOD_NOT_FOUND:
                log.warning(f"[{self.device_id}] Device.GetPresets refused: {exc}")
            # DesignOps 1.0 has no GetPresets; Recall Preset takes a typed name.
            self.set_state("presets", "[]")
            self.set_state("preset_count", 0)
            return
        options: list[dict[str, str]] = []

        def add(entries: Any) -> None:
            if not isinstance(entries, list):
                return
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "")
                is_group = bool(entry.get("group"))
                if name and name != UNNAMED_PRESET_GROUP:
                    label = f"{name} (preset group)" if is_group else name
                    if all(o["value"] != name for o in options):
                        options.append({"value": name, "label": label})
                if is_group:
                    add(entry.get("presets"))

        add(result.get("presets") if isinstance(result, dict) else None)
        self.set_state("presets", json.dumps(options))
        self.set_state("preset_count", len(options))

    async def _import_groups(self) -> None:
        """Read every listed group whole, register it as a child with its
        controls, and subscribe each control."""
        names = parse_group_rows(self.config.get("control_groups"))
        used_ids: set[str] = set()
        seen: set[str] = set()
        problems: list[str] = []
        self._route.clear()
        self._subscription_ids.clear()

        for group in names:
            sid = _unique(_safe_id(group), used_ids)
            try:
                result = await self._call("NamedControl.Get", [f"{group}/"])
            except CognioError as exc:
                problems.append(self._group_problem(group, exc))
                continue
            seen.add(sid)
            self._register_group(sid, group, _walk_entries(result))

        for sid in [s for s in self._group_name if s not in seen]:
            self.deregister_child("group", sid)
            self._group_name.pop(sid, None)
            self._group_schema.pop(sid, None)
            self._group_props.pop(sid, None)

        failed = 0
        rate = int(self.config.get("notify_rate_ms") or DEFAULT_NOTIFY_RATE_MS)
        for full_name in list(self._route):
            rid = SUBSCRIPTION_ID_PREFIX + full_name
            try:
                await self._call(
                    "NamedControl.Subscribe",
                    {"name": full_name, "rate": rate, "condition": "change",
                     "sync": 1},
                    rid=rid,
                )
                self._subscription_ids[rid] = full_name
            except CognioError as exc:
                failed += 1
                log.warning(
                    f"[{self.device_id}] Subscribe {full_name} refused: {exc}")
        if failed:
            problems.append(
                f"{failed} control(s) could not be subscribed and will not "
                f"update on their own."
            )

        self.set_state("group_count", len(seen))
        self.set_state("control_count", len(self._route))
        self.set_state("controls_loaded", True)
        self._standing_error = " ".join(problems)
        if self._standing_error:
            self.set_state("last_error", self._standing_error)
        log.info(
            f"[{self.device_id}] Read {len(seen)} Control Panel Group(s), "
            f"{len(self._route)} control(s)")

    @staticmethod
    def _group_problem(group: str, exc: CognioError) -> str:
        if exc.code == ERR_GROUP_NOT_FOUND:
            return (
                f"Control Panel Group \"{group}\" was not found, or its Remote "
                f"Enable is off."
            )
        return f"Control Panel Group \"{group}\" could not be read: {exc.message}."

    def _register_group(
        self, sid: str, group: str, entries: list[dict[str, Any]],
    ) -> None:
        schema: dict[str, dict[str, Any]] = {
            "name": {"type": "string", "label": "Remote Name"},
            "control_count": {"type": "integer", "label": "Controls"},
        }
        initial: dict[str, Any] = {"name": group}
        props: dict[str, str] = {}
        used = {"name", "control_count"}
        prefix = f"{group}/"
        for entry in entries:
            full = str(entry.get("name") or "")
            if not full.startswith(prefix):
                continue
            control = full[len(prefix):]
            if not control or full in self._route:
                continue
            prop = _unique(_safe_prop(control), used)
            used.add(prop + TEXT_SUFFIX)
            schema[prop] = {"type": "number", "label": control, "control": True}
            schema[prop + TEXT_SUFFIX] = {
                "type": "string", "label": f"{control} (text)",
            }
            props[prop] = control
            self._route[full] = (sid, prop)
            number = _number(entry.get("value"))
            if number is not None:
                initial[prop] = number
            if entry.get("text") is not None:
                initial[prop + TEXT_SUFFIX] = str(entry.get("text"))
        initial["control_count"] = len(props)

        if self.is_child_registered("group", sid) and (
            self._group_schema.get(sid) != schema
        ):
            self.deregister_child("group", sid)
        if self.is_child_registered("group", sid):
            self.set_child_state_batch("group", sid, initial)
        else:
            self.register_child("group", sid, initial_state=initial, schema=schema)
        self._group_name[sid] = group
        self._group_schema[sid] = schema
        self._group_props[sid] = props

    # ── Poll, liveness, refresh ──

    async def poll(self) -> None:
        try:
            await self._ask_last_preset()
        except CognioError as exc:
            # Answered, so reachable; a timeout or a dropped link still
            # raises and counts toward taking the device offline.
            log.warning(f"[{self.device_id}] Last recalled preset refused: {exc}")
        # A missing group is a standing condition: say it again each poll.
        if self._standing_error:
            self.set_state("last_error", self._standing_error)

    async def _liveness_probe(self) -> None:
        try:
            await self._call("Device.GetLastRecalledPreset")
        except CognioError:
            return  # an error answer is an answer

    async def refresh_children(self) -> dict[str, Any]:
        if self.transport is None or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self._read_presets()
        await self._import_groups()
        return {"groups": len(self._group_name), "controls": len(self._route)}

    # ── Commands ──

    def _control_name(self, params: dict[str, Any]) -> str:
        """``<group>/<control>`` from a group child id and a control's state
        key, either of which may also be typed as the real name."""
        sid = str(params.get("group") or "").strip()
        group = self._group_name.get(sid, sid)
        prop = str(params.get("control") or "").strip()
        control = self._group_props.get(sid, {}).get(prop, prop)
        if not group or not control:
            raise ValueError("A Control Panel Group and a control are both needed.")
        return f"{group}/{control}"

    def _refusal(self, exc: CognioError, subject: str) -> CommandRefusedError:
        if exc.code == ERR_CONTROL_NOT_FOUND:
            sentence = (
                f"Control \"{subject}\" was not found, or its Remote Enable is off."
            )
        elif exc.code == ERR_CONTROL_NOT_WRITABLE:
            sentence = f"Control \"{subject}\" is read-only."
        elif exc.code == ERR_CONTROL_NOT_READABLE:
            sentence = (
                f"Control \"{subject}\" is not assigned to anything in the "
                f"design, so it has no value."
            )
        elif exc.code == ERR_PRESET_NOT_FOUND:
            sentence = (
                f"Preset \"{subject}\" was not found, or its Remote Enable is off."
            )
        elif exc.code == ERR_GROUP_NOT_FOUND:
            sentence = (
                f"The Control Panel Group of \"{subject}\" was not found, or its "
                f"Remote Enable is off."
            )
        else:
            sentence = f"The processor refused it: {exc.message or exc.code}."
        self.set_state("last_error", sentence)
        return CommandRefusedError(sentence)

    async def _set(self, method: str, name: str, field: str, value: Any) -> bool:
        try:
            await self._call(method, {"name": name, field: value})
        except CognioError as exc:
            raise self._refusal(exc, name) from exc
        return True

    async def send_command(
        self, command: str, params: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}
        if command == "set_control":
            return await self._set(
                "NamedControl.SetValue", self._control_name(params), "value",
                _wire_number(params["value"]))
        if command == "set_control_text":
            return await self._set(
                "NamedControl.SetText", self._control_name(params), "text",
                str(params["text"]))
        if command == "adjust_control":
            name = self._control_name(params)
            try:
                result = await self._call(
                    "NamedControl.ModifyValue",
                    {"name": name, "offset": _wire_number(params["offset"])})
            except CognioError as exc:
                raise self._refusal(exc, name) from exc
            # The answer lists the new numbers; subscriptions bring the text.
            self._apply_entries(result)
            return True
        if command == "set_named_control":
            return await self._set(
                "NamedControl.SetValue", str(params["name"]).strip(), "value",
                _wire_number(params["value"]))
        if command == "set_named_control_text":
            return await self._set(
                "NamedControl.SetText", str(params["name"]).strip(), "text",
                str(params["text"]))
        if command in ("recall_preset", "update_preset"):
            preset = str(params.get("preset") or "").strip()
            if not preset:
                raise ValueError("A preset's Remote Name is needed.")
            method = (
                "Device.RecallPreset" if command == "recall_preset"
                else "Device.UpdatePreset"
            )
            try:
                # An array of remote names: the form both methods document.
                await self._call(method, [preset])
            except CognioError as exc:
                raise self._refusal(exc, preset) from exc
            if command == "recall_preset":
                # A recall is not announced; read it back now, not at the
                # next poll. The recall itself succeeded, whatever this says.
                try:
                    await self._ask_last_preset()
                except CognioError as exc:
                    log.warning(
                        f"[{self.device_id}] Last recalled preset refused: {exc}")
            return True
        if command == "refresh_controls":
            return await self.refresh_children()
        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None
