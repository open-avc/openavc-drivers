"""Analog Way Aquilon C-series, public AWJ guide 6.2 (June 2026).

JSON get/replace messages on TCP 10606, terminated by EOT (0x04). State is
read from the public object/property interface and followed with Subscriptions.
Writes can have no reply; read actual values after the documented xUpdate.
Python handles device-enumerated objects, subscription fan-out and the A/B
bank switch after TAKE. All networking uses the platform TCP transport.
A manufacturer simulation or this companion is not physical qualification.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import DelimiterFrameParser

try:
    from openavc.drivers.base import CommandRefusedError, CommandPartialError
except ImportError:  # These typed errors enhance reporting after the minimum release.
    CommandRefusedError = RuntimeError
    CommandPartialError = RuntimeError

ROOT = "DeviceObject/"
MODEL = ROOT + "system/$device/@items/1/@props/dev"
MODELS = {
    "NLC_CMINI": "Aquilon Cmini",
    "NLC_C": "Aquilon C",
    "NLC_CPLUS": "Aquilon C+",
    "NLC_CMAX": "Aquilon Cmax",
}
IDENTITY = {
    MODEL: "model",
    ROOT + "system/$device/@items/1/serial/@props/serialNumber": "serial_number",
    ROOT + "system/$device/@items/1/version/@props/updater": "firmware",
}
ROSTERS = [
    "preconfig/resources/new/$screen/@items",
    "$input/@items",
    "$still/@items",
    "$still/library/$bank/@items",
    "masterPresetBank/$bank/@items",
    "presetBank/$bank/@items",
    "$screenAuxGroup/@items",
    "$screen/@items",
    "$auxiliary/@items",
    "$monitoring/@items",
    "presetBank/status/presetId",
    "masterPresetBank/status/lastUsed",
    "preconfig/backgrounds/$screen/@items",
]


class AquilonRefused(CommandRefusedError):
    """An error received from Aquilon, distinct from a transport timeout."""


class AnalogwayAquilonDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "analogway_aquilon",
        "name": "Analog Way Aquilon",
        "manufacturer": "Analog Way",
        "category": "video",
        "version": "1.0.0",
        "min_platform_version": "0.27.0",
        "author": "OpenAVC",
        "description": "Controls Aquilon Cmini, C, C+ and Cmax presentation systems on firmware 6.2. "
        "Named screens, layers and sources, separate Preview and Program routing, "
        "preset recall, multiviewer control and subscribed status feedback.",
        "source_url": "https://dwn01.analogway.com/Site+Internet/Series/Aquilon/Common/AWJ+Protocol/AW_Aquilon_AWJ_ProgrammersGuide_V6.2.pdf",
        "verified": False,
        "simulated": True,
        "tags": ["presentation-switcher", "video-wall", "multiviewer", "livepremier"],
        "protocols": ["awj"],
        "ports": [10606],
        "transport": "tcp",
        "delimiter": "\x04",
        "compatible_models": [
            {
                "manufacturer": "Analog Way",
                "models": ["Aquilon Cmini", "Aquilon C", "Aquilon C+", "Aquilon Cmax"],
                "confidence": "untested",
                "notes": "Documented AWJ 6.2 API. Physical hardware qualification "
                "remains outstanding. Configure screens and outputs in WebRCS "
                "first.",
            }
        ],
        "help": {
            "overview": "Control a commissioned Aquilon system: recall named memories, prepare "
            "layers on Preview, TAKE a screen to Program, and select multiviewer "
            "widget sources. State follows changes from WebRCS and other "
            "controllers. Wall design, calibration, audio setup and power control "
            "remain in WebRCS.",
            "setup": "1. Commission the screens, layers and inputs in WebRCS on firmware 6.2.\n"
            "2. Enable the AWJ API under Remote Control in WebRCS security settings.\n"
            "3. Enter the processor IP address and AWJ port (normally 10606). No WebRCS "
            "password is sent to this API.\n"
            "4. Use Refresh from Device after changing the screen configuration. "
            "Preview and Program are separate routing planes; TAKE changes what is "
            "live.",
            "connection": "Check the processor address, AWJ enable setting and TCP port 10606. "
            "Aquilon supports up to five AWJ connections.",
        },
        "default_config": {"host": "", "port": 10606, "poll_interval": 15},
        "config_schema": {
            "host": {"type": "string", "label": "IP Address", "required": True},
            "port": {
                "type": "integer",
                "label": "AWJ Port",
                "default": 10606,
                "min": 1,
                "max": 65535,
            },
            "poll_interval": {
                "type": "integer",
                "label": "Status Check Interval (seconds)",
                "default": 15,
                "min": 1,
                "max": 120,
                "description": "Live changes arrive through "
                "subscriptions. Periodic reads check "
                "connectivity and refresh the reported "
                "objects.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "firmware": {"type": "string", "label": "Firmware"},
            "last_error": {
                "type": "string",
                "label": "Last Error",
                "cloud_priority": "high",
            },
            "master_program": {
                "type": "integer",
                "label": "Last Master Preset on Program",
            },
            "master_preview": {
                "type": "integer",
                "label": "Last Master Preset on Preview",
            },
            "multiviewer_source_options": {
                "type": "string",
                "label": "Multiviewer Source Choices",
                "cloud_priority": "low",
            },
        },
        "child_entity_types": {
            "screen": {
                "label": "Screen",
                "label_plural": "Screens",
                "id_format": {"type": "string", "max_length": 40},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "transition": {"type": "string", "label": "Transition"},
                    "program_preset": {
                        "type": "integer",
                        "label": "Last Program Preset",
                    },
                    "preview_preset": {
                        "type": "integer",
                        "label": "Last Preview Preset",
                    },
                },
                "summary_fields": ["transition", "program_preset", "preview_preset"],
            },
            "layer": {
                "label": "Layer",
                "label_plural": "Layers",
                "id_format": {"type": "string", "max_length": 40},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "screen": {"type": "string", "label": "Screen"},
                    "capability": {"type": "string", "label": "Capacity"},
                    "program_source": {
                        "type": "string",
                        "label": "Program Source",
                        "control": True,
                    },
                    "preview_source": {
                        "type": "string",
                        "label": "Preview Source",
                        "control": True,
                    },
                    "program_frozen": {"type": "boolean", "label": "Program Frozen"},
                    "preview_frozen": {"type": "boolean", "label": "Preview Frozen"},
                    "program_source_name": {
                        "type": "string",
                        "label": "Program Source Name",
                    },
                    "preview_source_name": {
                        "type": "string",
                        "label": "Preview Source Name",
                    },
                },
                "summary_fields": ["program_source_name", "preview_source_name"],
            },
            "source": {
                "label": "Source",
                "label_plural": "Sources",
                "id_format": {"type": "string", "max_length": 40},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "kind": {"type": "string", "label": "Source Type"},
                    "available": {"type": "boolean", "label": "Available"},
                    "frozen": {"type": "boolean", "label": "Frozen"},
                    "capability": {"type": "string", "label": "Capacity"},
                },
                "summary_fields": ["available", "frozen"],
            },
            "master_preset": {
                "label": "Master Preset",
                "label_plural": "Master Presets",
                "id_format": {"type": "string", "max_length": 40},
                "label_field": "name",
                "state_variables": {"name": {"type": "string", "label": "Name"}},
                "summary_fields": [],
            },
            "screen_preset": {
                "label": "Screen Preset",
                "label_plural": "Screen Presets",
                "id_format": {"type": "string", "max_length": 40},
                "label_field": "name",
                "state_variables": {"name": {"type": "string", "label": "Name"}},
                "summary_fields": [],
            },
            "multiviewer": {
                "label": "Multiviewer",
                "label_plural": "Multiviewers",
                "id_format": {"type": "string", "max_length": 40},
                "label_field": "name",
                "state_variables": {"name": {"type": "string", "label": "Name"}},
                "summary_fields": [],
            },
            "widget": {
                "label": "Multiviewer Widget",
                "label_plural": "Multiviewer Widgets",
                "id_format": {"type": "string", "max_length": 40},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "source": {"type": "string", "label": "Source", "control": True},
                },
                "summary_fields": ["source"],
            },
            "background_set": {
                "label": "Background Set",
                "label_plural": "Background Sets",
                "id_format": {"type": "string", "max_length": 40},
                "label_field": "name",
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "screen": {"type": "string", "label": "Screen"},
                    "content_valid": {"type": "boolean", "label": "Content Valid"},
                },
                "summary_fields": ["screen", "content_valid"],
            },
        },
        "commands": {
            "refresh": {
                "label": "Refresh Status and Objects",
                "help": "Re-read screens, layers, sources, memories and multiviewers "
                "from Aquilon.",
                "params": {},
                "query_for": "model",
            },
            "route_layer": {
                "label": "Select Layer Source",
                "help": "Change the selected layer on Preview or Program. "
                "Program changes are live immediately. Use TAKE to bring "
                "prepared Preview content to Program.",
                "params": {
                    "layer": {
                        "type": "child_id",
                        "child_type": "layer",
                        "label": "Layer",
                        "required": True,
                    },
                    "source": {
                        "type": "child_id",
                        "child_type": "source",
                        "label": "Source",
                        "required": True,
                    },
                    "target": {
                        "type": "enum",
                        "label": "Destination",
                        "values": [
                            {"value": "PREVIEW", "label": "Preview"},
                            {"value": "PROGRAM", "label": "Program"},
                        ],
                        "default": "PREVIEW",
                    },
                },
            },
            "clear_layer": {
                "label": "Clear Layer",
                "help": "Remove the layer source on the selected destination. "
                "Program changes are live.",
                "params": {
                    "layer": {
                        "type": "child_id",
                        "child_type": "layer",
                        "label": "Layer",
                        "required": True,
                    },
                    "target": {
                        "type": "enum",
                        "label": "Destination",
                        "values": [
                            {"value": "PREVIEW", "label": "Preview"},
                            {"value": "PROGRAM", "label": "Program"},
                        ],
                        "default": "PREVIEW",
                    },
                },
            },
            "take": {
                "label": "TAKE Screen",
                "help": "Transition this screen’s Preview content to Program using its "
                "configured transition.",
                "params": {
                    "screen": {
                        "type": "child_id",
                        "child_type": "screen",
                        "label": "Screen",
                        "required": True,
                    }
                },
            },
            "recall_master_preset": {
                "label": "Recall Master Preset",
                "help": "Recall a stored master preset to Preview or "
                "directly to Program. A Program recall changes "
                "the live presentation.",
                "params": {
                    "preset": {
                        "type": "child_id",
                        "child_type": "master_preset",
                        "label": "Master Preset",
                        "required": True,
                    },
                    "target": {
                        "type": "enum",
                        "label": "Destination",
                        "values": [
                            {"value": "PREVIEW", "label": "Preview"},
                            {"value": "PROGRAM", "label": "Program"},
                        ],
                        "default": "PREVIEW",
                    },
                },
            },
            "recall_screen_preset": {
                "label": "Recall Screen Preset",
                "help": "Recall a stored screen preset to the selected "
                "screen. A Program recall changes the live "
                "presentation.",
                "params": {
                    "screen": {
                        "type": "child_id",
                        "child_type": "screen",
                        "label": "Screen",
                        "required": True,
                    },
                    "preset": {
                        "type": "child_id",
                        "child_type": "screen_preset",
                        "label": "Screen Preset",
                        "required": True,
                    },
                    "target": {
                        "type": "enum",
                        "label": "Destination",
                        "values": [
                            {"value": "PREVIEW", "label": "Preview"},
                            {"value": "PROGRAM", "label": "Program"},
                        ],
                        "default": "PREVIEW",
                    },
                },
            },
            "set_widget_source": {
                "label": "Select Multiviewer Widget Source",
                "params": {
                    "widget": {
                        "type": "child_id",
                        "child_type": "widget",
                        "label": "Multiviewer Widget",
                        "required": True,
                    },
                    "source": {
                        "type": "string",
                        "label": "Source",
                        "required": True,
                        "options_state": "multiviewer_source_options",
                    },
                },
                "sets": {"source": "{source}"},
            },
            "recall_multiviewer_preset": {
                "label": "Recall Multiviewer Preset",
                "help": "Recall a multiviewer memory stored in "
                "WebRCS. Use the memory number shown "
                "there.",
                "params": {
                    "multiviewer": {
                        "type": "child_id",
                        "child_type": "multiviewer",
                        "label": "Multiviewer",
                        "required": True,
                    },
                    "preset": {
                        "type": "integer",
                        "label": "Memory Number",
                        "min": 1,
                        "required": True,
                    },
                },
            },
            "select_background_set": {
                "label": "Select Background Set",
                "help": "Select a configured background set on its "
                "screen. A Program change is live immediately.",
                "params": {
                    "background": {
                        "type": "child_id",
                        "child_type": "background_set",
                        "label": "Background Set",
                        "required": True,
                    },
                    "target": {
                        "type": "enum",
                        "label": "Destination",
                        "values": [
                            {"value": "PREVIEW", "label": "Preview"},
                            {"value": "PROGRAM", "label": "Program"},
                        ],
                        "default": "PREVIEW",
                    },
                },
            },
        },
        "quick_actions": ["recall_master_preset", "take", "refresh"],
        "discovery": {
            "tcp_probe": {
                "port": 10606,
                "send_ascii": '{"op":"get","path":"DeviceObject/system/$device/@items/1/@props/dev"}\x04',
                "expect_regex": '"value"\\s*:\\s*"NLC_(?:CMINI|C|CPLUS|CMAX)"',
                "extract": {
                    "model": {"regex": '"value"\\s*:\\s*"(NLC_(?:CMINI|C|CPLUS|CMAX))"'}
                },
            },
            "manufacturer_alias": ["Analog Way"],
        },
        "routing": {
            "destination_child_type": "layer",
            "source_child_type": "source",
            "command": "route_layer",
            "destination_param": "layer",
            "source_param": "source",
            "planes": [
                {
                    "label": "Preview",
                    "route_property": "preview_source",
                    "params": {"target": "PREVIEW"},
                },
                {
                    "label": "Program",
                    "route_property": "program_source",
                    "params": {"target": "PROGRAM"},
                },
            ],
        },
    }

    HEALTH_INTERVAL_S = 30
    HEALTH_TIMEOUT_S = 6
    HEALTH_MAX_FAILURES = 3
    HEALTH_FAULT_MESSAGE = "Aquilon stopped answering AWJ requests."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._exchange = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._waiter = None
        self._waiting_path = None
        self._write_errors = None
        self._values = {}
        self._bindings = {}
        self._layers = {}
        self._screens = {}
        self._widgets = {}
        self._backgrounds = {}
        self._watched = set()
        self._refresh_due = 0.0
        self._session_valid = False

    def _create_frame_parser(self):
        # Public AWJ object reads can be much larger than individual properties.
        return DelimiterFrameParser(b"\x04", max_buffer=16 * 1024 * 1024)

    async def _post_connect(self):
        self._session_valid = True
        self._values.clear()
        self._bindings.clear()
        self._refresh_due = 0.0
        async with self._exchange:
            model = await self._get(MODEL)
        if model not in MODELS:
            raise ConnectionError(
                "This driver requires an Aquilon Cmini, C, C+ or Cmax."
            )

    async def _initial_sync(self):
        await self.refresh_children()

    async def _close_session(self):
        self._session_valid = False
        if self._waiter and not self._waiter.done():
            self._waiter.set_exception(ConnectionError("The AWJ connection closed."))

    async def _liveness_probe(self):
        async with self._exchange:
            try:
                await self._get(MODEL)
            except AquilonRefused:
                pass  # A protocol error still proves that Aquilon answered.

    async def _send(self, op, path, value=None):
        if not self._session_valid or not self.transport:
            raise ConnectionError("Aquilon is not connected.")
        message = {"op": op, "path": path}
        if op == "replace":
            message["value"] = value
        await self.transport.send(
            (json.dumps(message, separators=(",", ":")) + "\x04").encode()
        )

    async def _get(self, path):
        self._waiting_path = path
        self._waiter = asyncio.get_running_loop().create_future()
        try:
            await self._send("get", path)
            return await asyncio.wait_for(self._waiter, 5)
        except asyncio.TimeoutError as exc:
            # No request IDs: a late reply must never satisfy a later read of
            # the same path. Reconnect before accepting another transaction.
            self._session_valid = False
            self._handle_transport_disconnect()
            raise ConnectionError("Aquilon did not answer the AWJ request.") from exc
        except asyncio.CancelledError:
            # A cancelled read can also leave a late, untagged response. The
            # next liveness check must reconnect rather than reusing this stream.
            self._session_valid = False
            raise
        finally:
            self._waiter = None
            self._waiting_path = None

    async def on_data_received(self, data):
        # Apply the whole message before yielding; transport callbacks are tasks.
        try:
            message = json.loads(data.rstrip(b"\x04"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(message, dict):
            return
        if "error" in message:
            self._receive_error(message["error"])
            return
        self._receive_value(message)

    def _receive_value(self, message):
        path = message.get("path")
        if not isinstance(path, str) or "value" not in message:
            return
        self._cache(path, message["value"])
        if path != self._waiting_path:
            return
        if self._waiter and not self._waiter.done():
            self._waiter.set_result(message["value"])

    def _receive_error(self, error):
        if not isinstance(error, dict):
            return
        detail = (
            f"{error.get('code', 'AWJ')}: {error.get('message', 'Request refused')}"
        )
        self.set_state("last_error", detail)
        if self._write_errors is not None:
            self._write_errors.append(detail)
        elif self._waiter and not self._waiter.done():
            self._waiter.set_exception(AquilonRefused(detail))

    def _cache(self, path, value):
        # A complete object read replaces its previous contents, including
        # properties/children removed since the last configuration.
        self._forget_branch(path)
        values = dict(_leaves(path, value))
        self._values.update(values)
        for address, item in values.items():
            self._publish_property(address, item)
        self._publish_banks()
        self._changed_objects(values)

    def _forget_branch(self, path):
        prefix = path + "/"
        for old in [p for p in self._values if p == path or p.startswith(prefix)]:
            del self._values[old]

    def _publish_property(self, address, item):
        if address in IDENTITY:
            key = IDENTITY[address]
            self.set_state(key, MODELS.get(item, item) if key == "model" else item)
        for kind, local_id, key in self._bindings.get(address, []):
            self.set_child_state(kind, local_id, key, item)

    def _changed_objects(self, values):
        if any(
            address.endswith(("/mode", "/isEnabled", "/isUsed")) for address in values
        ):
            self._refresh_due = 0.0
        if self._screens and any(address.endswith("/label") for address in values):
            self._multiviewer_choices()
            self._update_layer_names()

    def _update_layer_names(self):
        for local_id, (sid, layer) in self._layers.items():
            name = self.get_child_state("screen", sid).get("name") or sid
            label = "Background" if layer == "NATIVE" else f"Layer {layer}"
            self.set_child_state("layer", local_id, "name", f"{name} / {label}")

    def _at(self, path, default=None):
        return self._values.get(ROOT + path, default)

    def _ids(self, path):
        prefix = ROOT + path + "/"
        return sorted(
            {
                p[len(prefix) :].split("/", 1)[0]
                for p in self._values
                if p.startswith(prefix)
            },
            key=_natural,
        )

    def _bind(self, kind, local_id, key, path):
        full = ROOT + path
        self._bindings.setdefault(full, []).append((kind, local_id, key))
        self._watched.add(full)
        return self._values.get(full)

    def _child(self, kind, local_id, values, wanted):
        self.register_child(kind, local_id, values)
        self.set_child_state_batch(kind, local_id, values)
        wanted[kind].add(local_id)

    def _reconcile(self):
        self._bindings.clear()
        self._watched = set(IDENTITY)
        self._watched.update(
            path
            for path in self._values
            if path.endswith(("/mode", "/isEnabled", "/isUsed"))
        )
        self._layers.clear()
        self._screens.clear()
        self._widgets.clear()
        self._backgrounds.clear()
        wanted = {kind: set() for kind in self.DRIVER_INFO["child_entity_types"]}
        self._screen_rosters(wanted)
        self._background_roster(wanted)
        self._source_roster(wanted)
        self._preset_rosters(wanted)
        self._multiviewer_rosters(wanted)
        for kind, ids in wanted.items():
            for removed in set(self.list_children(kind)) - ids:
                self.deregister_child(kind, removed)
        self._publish_banks()
        self._multiviewer_choices()

    def _background_roster(self, wanted):
        for sid in self._screens:
            base = f"preconfig/backgrounds/$screen/@items/{sid}/$backgroundSet/@items"
            for slot in self._ids(base):
                if not re.fullmatch(r"[1-9]\d*", slot):
                    continue
                local_id = f"{sid}_{slot}"
                prefix = ROOT + f"{base}/{slot}/$output/@items/"
                validity = [
                    value
                    for path, value in self._values.items()
                    if path.startswith(prefix)
                    and path.endswith("/status/@props/isContentValid")
                ]
                self._backgrounds[local_id] = (sid, slot)
                name = self.get_child_state("screen", sid).get("name") or sid
                self._child(
                    "background_set",
                    local_id,
                    {
                        "name": f"{name} / Background Set {slot}",
                        "screen": sid,
                        "content_valid": any(validity) if validity else None,
                    },
                    wanted,
                )

    def _screen_rosters(self, wanted):
        for branch, pattern in (
            ("$screen", r"S[1-9]\d*"),
            ("$auxiliary", r"A[1-9]\d*"),
        ):
            for sid in self._ids(branch + "/@items"):
                if not re.fullmatch(pattern, sid):
                    continue
                if (
                    branch == "$screen"
                    and self._at(
                        f"preconfig/resources/new/$screen/@items/{sid}/control/@props/mode"
                    )
                    != "FREESTYLE"
                ):
                    continue
                self._add_screen(branch, sid, wanted)

    def _add_screen(self, branch, sid, wanted):
        base = f"{branch}/@items/{sid}"
        name = self._bind("screen", sid, "name", base + "/control/@props/label") or sid
        transition = f"$screenAuxGroup/@items/{sid}/status/@props/transition"
        state = {
            "name": name,
            "transition": self._bind("screen", sid, "transition", transition),
        }
        self._screens[sid] = base
        self._child("screen", sid, state, wanted)
        # Layer IDs are taken from the returned banks, not an assumed model size.
        layers = set(self._ids(base + "/$preset/@items/A/$layer/@items"))
        layers.update(self._ids(base + "/$preset/@items/B/$layer/@items"))
        for layer in sorted(layers, key=_natural):
            if layer == "NATIVE" or re.fullmatch(r"[1-9]\d*", layer):
                self._add_layer(sid, layer, name, wanted)
        for bank in ("A", "B"):
            self._watched.add(
                ROOT
                + f"presetBank/status/presetId/{branch}/@items/{sid}/$preset/@items/{bank}/@props/id"
            )

    def _add_layer(self, sid, layer, screen_name, wanted):
        capacity = self._at(
            f"preconfig/resources/new/$screen/@items/{sid}/$layer/@items/{layer}/control/@props/capability"
        )
        if capacity == "OFF":
            return
        base = self._screens[sid]
        local_id = f"{sid}_{layer}"
        self._layers[local_id] = (sid, layer)
        label = "Background" if layer == "NATIVE" else f"Layer {layer}"
        self._child(
            "layer",
            local_id,
            {"name": f"{screen_name} / {label}", "screen": sid, "capability": capacity},
            wanted,
        )
        for bank in ("A", "B"):
            self._watched.add(ROOT + _source_path(base, bank, layer))
        self._watched.add(ROOT + f"{base}/$layer/@items/{layer}/control/@props/freeze")

    def _source_roster(self, wanted):
        for wire, name in (("NONE", "No Source"), ("COLOR", "Layer Color")):
            self._child("source", wire, {"name": name, "kind": "Internal"}, wanted)
        self._live_sources(wanted)
        self._still_sources(wanted)
        self._screen_sources(wanted)

    def _live_sources(self, wanted):
        for iid in self._ids("$input/@items"):
            match = re.fullmatch(r"IN_([1-9]\d*)", iid)
            if (
                match
                and self._at(f"$input/@items/{iid}/status/@props/isEnabled") is True
            ):
                self._input_source(iid, "LIVE_" + match[1], wanted)

    def _still_sources(self, wanted):
        for slot in self._ids("$still/@items"):
            if (
                slot.isdigit()
                and self._at(f"$still/@items/{slot}/status/@props/isUsed") is True
            ):
                self._still_source(slot, wanted)

    def _screen_sources(self, wanted):
        for sid in self._screens:
            if sid.startswith("S"):
                name = self._at(self._screens[sid] + "/control/@props/label") or sid
                self._child(
                    "source",
                    "SCREEN_" + sid[1:],
                    {"name": f"Screen: {name}", "kind": "Screen"},
                    wanted,
                )

    def _input_source(self, iid, wire, wanted):
        base = f"$input/@items/{iid}"
        values = {"kind": "Live Input"}
        for key, suffix in (
            ("name", "control/@props/label"),
            ("available", "status/@props/isAvailable"),
            ("frozen", "control/@props/freeze"),
            ("capability", "status/@props/capability"),
        ):
            values[key] = self._bind("source", wire, key, f"{base}/{suffix}")
        values["name"] = values["name"] or f"Input {iid[3:]}"
        self._child("source", wire, values, wanted)

    def _still_source(self, slot, wanted):
        image = self._at(f"$still/@items/{slot}/control/@props/source")
        base = f"$still/@items/{slot}"
        wire = f"STILL_{slot}"
        name = (
            self._bind("source", wire, "name", base + "/control/@props/label")
            or f"Still {slot}"
        )
        available = self._bind(
            "source", wire, "available", base + "/status/@props/isAvailable"
        )
        capacity = self._at(
            f"$still/library/$bank/@items/{image}/status/@props/capability"
        )
        self._child(
            "source",
            wire,
            {
                "name": name,
                "kind": "Still",
                "available": available,
                "capability": capacity,
            },
            wanted,
        )

    def _preset_rosters(self, wanted):
        for kind, branch in (
            ("master_preset", "masterPresetBank"),
            ("screen_preset", "presetBank"),
        ):
            for slot in self._ids(branch + "/$bank/@items"):
                if not re.fullmatch(r"[1-9]\d*", slot):
                    continue
                name = self._bind(
                    kind,
                    slot,
                    "name",
                    f"{branch}/$bank/@items/{slot}/control/@props/label",
                )
                self._child(kind, slot, {"name": name or f"Preset {slot}"}, wanted)
        for target in ("PROGRAM", "PREVIEW"):
            self._watched.add(
                ROOT
                + f"masterPresetBank/status/lastUsed/$presetMode/@items/{target}/@props/memoryId"
            )

    def _multiviewer_rosters(self, wanted):
        for mid in self._ids("$monitoring/@items"):
            base = f"$monitoring/@items/{mid}"
            if (
                not mid.isdigit()
                or self._at(base + "/status/@props/isEnabled") is not True
            ):
                continue
            name = (
                self._bind("multiviewer", mid, "name", base + "/control/@props/label")
                or f"Multiviewer {mid}"
            )
            self._child("multiviewer", mid, {"name": name}, wanted)
            for widget in self._ids(base + "/layout/$widget/@items"):
                self._add_widget(mid, widget, name, wanted)

    def _add_widget(self, mid, widget, name, wanted):
        if not widget.isdigit() or not 0 <= int(widget) <= 63:
            return
        base = f"$monitoring/@items/{mid}/layout/$widget/@items/{widget}"
        if self._at(base + "/status/@props/isEnabled") is not True:
            return
        local_id = f"{mid}_{widget}"
        self._widgets[local_id] = base + "/control/@props/source"
        source = self._bind("widget", local_id, "source", self._widgets[local_id])
        self._child(
            "widget",
            local_id,
            {"name": f"{name} / Widget {int(widget) + 1}", "source": source},
            wanted,
        )

    def _multiviewer_choices(self):
        choices = [{"value": "NONE", "label": "No Source"}]
        choices.extend(self._multiviewer_input_choices())
        for sid in self._screens:
            choices.extend(self._multiviewer_screen_choices(sid))
        choices.extend(
            {"value": f"TIMER_{n}", "label": f"Timer {n}"} for n in range(1, 5)
        )
        self.set_state("multiviewer_source_options", json.dumps(choices))

    def _multiviewer_input_choices(self):
        for source in self.list_children("source"):
            if source.startswith(("LIVE_", "STILL_")):
                wire = source.replace("LIVE_", "IN_", 1)
                name = self.get_child_state("source", source).get("name") or source
                yield {"value": wire, "label": name}

    def _multiviewer_screen_choices(self, sid):
        name = self.get_child_state("screen", sid).get("name") or sid
        if sid.startswith("S"):
            return [
                {"value": f"{mode}_{sid}", "label": f"{name} {mode.title()}"}
                for mode in ("PROGRAM", "PREVIEW")
            ]
        return [{"value": "A_" + sid[1:], "label": name}]

    def _publish_banks(self):
        for sid, base in self._screens.items():
            transition = self._at(
                f"$screenAuxGroup/@items/{sid}/status/@props/transition"
            )
            bank = {"AT_DOWN": "A", "AT_UP": "B"}.get(transition)
            for target, selected in (("program", bank), ("preview", _opposite(bank))):
                branch = base.split("/", 1)[0]
                path = f"presetBank/status/presetId/{branch}/@items/{sid}/$preset/@items/{selected}/@props/id"
                self.set_child_state(
                    "screen",
                    sid,
                    target + "_preset",
                    self._at(path) if selected else None,
                )
        for local_id, (sid, layer) in self._layers.items():
            self._publish_layer(local_id, sid, layer)
        for mode in ("program", "preview"):
            self.set_state(
                "master_" + mode,
                self._at(
                    f"masterPresetBank/status/lastUsed/$presetMode/@items/{mode.upper()}/@props/memoryId"
                ),
            )

    def _publish_layer(self, local_id, sid, layer):
        base = self._screens[sid]
        transition = self._at(f"$screenAuxGroup/@items/{sid}/status/@props/transition")
        bank = {"AT_DOWN": "A", "AT_UP": "B"}.get(transition)
        freeze = self._at(f"{base}/$layer/@items/{layer}/control/@props/freeze")
        values = {}
        for mode, selected in (("program", bank), ("preview", _opposite(bank))):
            values[mode + "_source"] = (
                self._at(_source_path(base, selected, layer)) if selected else None
            )
            values[mode + "_source_name"] = self._source_name(
                sid, values[mode + "_source"]
            )
            values[mode + "_frozen"] = (
                (("DOWN" if selected == "A" else "UP") in freeze)
                if selected and isinstance(freeze, list)
                else None
            )
        self.set_child_state_batch("layer", local_id, values)

    def _source_name(self, sid, source):
        if source is None:
            return None
        if isinstance(source, str) and source.startswith("NATIVE_"):
            return (
                self.get_child_state("background_set", sid + "_" + source[7:]).get(
                    "name"
                )
                or source
            )
        return self.get_child_state("source", source).get("name") or source

    async def refresh_children(self):
        async with self._refresh_lock:
            for path in list(IDENTITY) + [ROOT + item for item in ROSTERS]:
                async with self._exchange:
                    value = await self._get(path)
                    if path not in IDENTITY and not isinstance(value, dict):
                        raise AquilonRefused(
                            "Aquilon returned an invalid object list. Check its firmware and AWJ configuration."
                        )
                await asyncio.sleep(0.02)
            self._reconcile()
            async with self._exchange:
                await self._write([("Subscriptions", sorted(self._watched))])
                subscribed = await self._get("Subscriptions")
                if not isinstance(subscribed, list) or not all(
                    any(p.startswith(prefix) for prefix in subscribed)
                    for p in self._watched
                ):
                    raise AquilonRefused(
                        "Aquilon did not enable the requested feedback subscriptions."
                    )
            self._refresh_due = time.monotonic() + 60
            self.set_state("last_error", "")

    async def poll(self):
        if time.monotonic() >= self._refresh_due:
            await self.refresh_children()
        else:
            await self._liveness_probe()

    async def _write(self, changes):
        # Every write may be silent. A subsequent identity GET is an ordered
        # barrier on the same connection; collect untagged write errors until it.
        self._write_errors = []
        try:
            for path, value in changes:
                await self._send("replace", path, value)
            await self._get(MODEL)
            if self._write_errors:
                raise AquilonRefused("; ".join(self._write_errors))
        finally:
            self._write_errors = None

    async def send_command(self, command, params=None):
        params = params or {}
        handlers = {
            "refresh": self._refresh_command,
            "route_layer": self._route,
            "clear_layer": self._clear,
            "take": self._take,
            "recall_master_preset": self._master,
            "recall_screen_preset": self._preset,
            "set_widget_source": self._widget,
            "recall_multiviewer_preset": self._mv_preset,
            "select_background_set": self._background,
        }
        if command not in handlers:
            raise ValueError(f"Unknown command: {command}")
        try:
            result = await handlers[command](params)
            self.set_state("last_error", "")
            return result
        except (AquilonRefused, CommandPartialError) as exc:
            self.set_state("last_error", str(exc))
            raise

    async def _refresh_command(self, params):
        await self.refresh_children()
        return "Status and objects refreshed."

    async def _clear(self, params):
        return await self._route(dict(params, source="NONE"))

    async def _route(self, params):
        local_id = str(params["layer"])
        source = str(params["source"])
        if local_id not in self._layers or source not in self.list_children("source"):
            raise AquilonRefused(
                "The layer or source is no longer available. Refresh from Device."
            )
        sid, layer = self._layers[local_id]
        return await self._route_source(
            sid, layer, source, params.get("target", "PREVIEW")
        )

    async def _background(self, params):
        local_id = str(params["background"])
        if local_id not in self._backgrounds:
            raise AquilonRefused(
                "This background set is no longer available. Refresh from Device."
            )
        sid, slot = self._backgrounds[local_id]
        if f"{sid}_NATIVE" not in self._layers:
            raise AquilonRefused(
                "This screen has no background layer. Check its configuration in WebRCS."
            )
        return await self._route_source(
            sid, "NATIVE", "NATIVE_" + slot, params.get("target", "PREVIEW")
        )

    async def _route_source(self, sid, layer, source, target):
        transition_path = (
            ROOT + f"$screenAuxGroup/@items/{sid}/status/@props/transition"
        )
        async with self._exchange:
            transition = await self._get(transition_path)
            bank = _bank(transition, target)
            path = ROOT + _source_path(self._screens[sid], bank, layer)
            await self._write(
                [
                    (path, source),
                    (ROOT + "$screenAuxGroup/control/@props/xUpdate", True),
                ]
            )
            actual = await self._get(path)
            after = await self._get(transition_path)
            if after != transition:
                raise CommandPartialError(
                    "The screen transitioned during the change. Check Program and Preview before continuing."
                )
            if actual != source:
                raise CommandPartialError(
                    "Aquilon has not reported the requested source. Refresh the layer before retrying."
                )
        return {"source": actual, "target": target}

    async def _take(self, params):
        sid = self._screen(params)
        path = ROOT + f"$screenAuxGroup/@items/{sid}"
        async with self._exchange:
            await self._write([(path + "/control/@props/xTake", True)])
            reported = await self._get(path + "/status/@props/transition")
        return {"requested": "TAKE", "reported_transition": reported}

    def _screen(self, params):
        sid = str(params["screen"])
        if sid not in self._screens:
            raise AquilonRefused(
                "This screen is no longer available. Refresh from Device."
            )
        return sid

    async def _master(self, params):
        slot = self._preset_id("master_preset", params)
        target = params.get("target", "PREVIEW")
        path = (
            ROOT
            + f"masterPresetBank/control/load/$slot/@items/{slot}/$preset/@items/{target}/@props/xRequest"
        )
        status = (
            ROOT
            + f"masterPresetBank/status/lastUsed/$presetMode/@items/{target}/@props/memoryId"
        )
        async with self._exchange:
            await self._write([(path, True)])
            actual = await self._get(status)
            if str(actual) != slot:
                raise CommandPartialError(
                    "Aquilon has not reported the requested master preset. Check the memory and screen locks in WebRCS."
                )
        return {"preset": actual, "target": target}

    def _preset_id(self, kind, params):
        slot = str(params["preset"])
        if slot not in self.list_children(kind):
            raise AquilonRefused(
                "This preset is no longer listed. Refresh from Device."
            )
        return slot

    async def _preset(self, params):
        sid = self._screen(params)
        slot = self._preset_id("screen_preset", params)
        target = params.get("target", "PREVIEW")
        branch = self._screens[sid].split("/", 1)[0]
        path = (
            ROOT
            + f"presetBank/control/load/$slot/@items/{slot}/{branch}/@items/{sid}/$preset/@items/{target}/@props/xRequest"
        )
        async with self._exchange:
            await self._write([(path, True)])
            transition = await self._get(
                ROOT + f"$screenAuxGroup/@items/{sid}/status/@props/transition"
            )
            bank = _bank(transition, target)
            actual = await self._get(
                ROOT
                + f"presetBank/status/presetId/{branch}/@items/{sid}/$preset/@items/{bank}/@props/id"
            )
            if str(actual) != slot:
                raise CommandPartialError(
                    "Aquilon has not reported the requested screen preset. Check the memory and screen lock in WebRCS."
                )
        return {"preset": actual, "target": target}

    async def _widget(self, params):
        widget = str(params["widget"])
        source = str(params["source"])
        options = json.loads(self.get_state("multiviewer_source_options") or "[]")
        if widget not in self._widgets or source not in {
            item["value"] for item in options
        }:
            raise AquilonRefused(
                "The widget or source is no longer available. Refresh from Device."
            )
        path = ROOT + self._widgets[widget]
        async with self._exchange:
            await self._write([(path, source)])
            actual = await self._get(path)
            if actual != source:
                raise CommandPartialError(
                    "Aquilon has not reported the requested widget source. Refresh before retrying."
                )
        return actual

    async def _mv_preset(self, params):
        mid = str(params["multiviewer"])
        if mid not in self.list_children("multiviewer"):
            raise AquilonRefused(
                "This multiviewer is no longer available. Refresh from Device."
            )
        slot = int(params["preset"])
        path = (
            ROOT
            + f"monitoringBank/control/load/$slot/@items/{slot}/$output/@items/{mid}/@props/xRequest"
        )
        async with self._exchange:
            await self._write([(path, True)])
            await self._get(ROOT + f"$monitoring/@items/{mid}")
        self._reconcile()
        return "Recall requested. Widget feedback has been refreshed."


def _leaves(path, value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(path + "/" + str(key), item)
    else:
        yield path, value


def _natural(value):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def _opposite(bank):
    return {"A": "B", "B": "A"}.get(bank)


def _source_path(base, bank, layer):
    return f"{base}/$preset/@items/{bank}/$layer/@items/{layer}/source/@props/inputNum"


def _bank(transition, target):
    bank = {"AT_DOWN": "A", "AT_UP": "B"}.get(transition)
    if not bank:
        raise AquilonRefused(
            "The screen is transitioning. Wait for it to finish before changing a layer or recalling a preset."
        )
    if target not in ("PROGRAM", "PREVIEW"):
        raise AquilonRefused("Choose Program or Preview.")
    return bank if target == "PROGRAM" else _opposite(bank)
