"""
Philips Hue Bridge — Simulator

Simulates a Hue Bridge's local REST API V1 on port 80: a small install of
five lights with different capability sets (extended color, color
temperature, dimmable, on/off plug), two rooms + one zone, three scenes,
and the special all-lights group 0 (excluded from GET /groups, served at
/groups/0, exactly like a real bridge).

Pairing: POST /api succeeds only while the "Link Button Pressed" toggle is
on (otherwise Hue error 101), mirroring the physical link-button flow. The
toggle stays on until switched off (a real bridge's window auto-expires
after ~30 s; the sim keeps it manual so pairing is predictable).

Authentication: any non-empty app key is accepted EXCEPT the literal
"invalid" (the platform's designated bad-credential sentinel), which —
like a missing key — draws Hue error type 1 (unauthorized user), so the
driver's auth-failure path is testable. The unauthenticated GET
/api/config discovery endpoint answers with the bridge-id subset like a
real bridge.

Group actions fan out to member lights (group 0 = all lights) and the
group's any_on/all_on state is recomputed from members. Scene recall
applies the scene's stored light states. Writing a dimming/color field to
a light that is off (without turning it on in the same request) draws Hue
error 201, mirroring the real "parameter not modifiable" behavior.

Driver: philips_hue
Transport: http
"""

from __future__ import annotations

import json
from typing import Any

from simulator.http_simulator import HTTPSimulator

_BAD_KEY = "invalid"

# Per-light V1 documents. `state` carries exactly the fields the light's
# capability set supports — the driver derives each child's schema from
# what is present.
_LIGHTS: dict[str, dict[str, Any]] = {
    "1": {
        "name": "Boardroom Front",
        "type": "Extended color light",
        "modelid": "LCT015",
        "manufacturername": "Signify Netherlands B.V.",
        "uniqueid": "00:17:88:01:aa:00:01:01-0b",
        "swversion": "1.108.7",
        "state": {
            "on": True, "bri": 200, "hue": 8402, "sat": 140,
            "xy": [0.4573, 0.4100], "ct": 366, "alert": "none",
            "effect": "none", "colormode": "ct", "reachable": True,
        },
    },
    "2": {
        "name": "Boardroom Rear",
        "type": "Extended color light",
        "modelid": "LCT015",
        "manufacturername": "Signify Netherlands B.V.",
        "uniqueid": "00:17:88:01:aa:00:01:02-0b",
        "swversion": "1.108.7",
        "state": {
            "on": False, "bri": 254, "hue": 0, "sat": 0,
            "xy": [0.3127, 0.3290], "ct": 300, "alert": "none",
            "effect": "none", "colormode": "xy", "reachable": True,
        },
    },
    "3": {
        "name": "Lobby Downlight",
        "type": "Color temperature light",
        "modelid": "LTW013",
        "manufacturername": "Signify Netherlands B.V.",
        "uniqueid": "00:17:88:01:aa:00:01:03-0b",
        "swversion": "1.108.7",
        "state": {
            "on": True, "bri": 180, "ct": 250, "alert": "none",
            "colormode": "ct", "reachable": True,
        },
    },
    "4": {
        "name": "Stage Wash",
        "type": "Dimmable light",
        "modelid": "LWB010",
        "manufacturername": "Signify Netherlands B.V.",
        "uniqueid": "00:17:88:01:aa:00:01:04-0b",
        "swversion": "1.108.7",
        "state": {"on": False, "bri": 254, "alert": "none", "reachable": True},
    },
    "5": {
        "name": "Signage Power",
        "type": "On/Off plug-in unit",
        "modelid": "LOM001",
        "manufacturername": "Signify Netherlands B.V.",
        "uniqueid": "00:17:88:01:aa:00:01:05-0b",
        "swversion": "1.108.7",
        "state": {"on": True, "alert": "none", "reachable": True},
    },
}

_GROUPS: dict[str, dict[str, Any]] = {
    "1": {
        "name": "Boardroom",
        "type": "Room",
        "class": "Meeting",
        "lights": ["1", "2"],
        "sensors": [],
        "action": {"on": True, "bri": 200, "colormode": "ct", "ct": 366},
    },
    "2": {
        "name": "Lobby",
        "type": "Room",
        "class": "Lobby",
        "lights": ["3"],
        "sensors": [],
        "action": {"on": True, "bri": 180, "colormode": "ct", "ct": 250},
    },
    "3": {
        "name": "Stage",
        "type": "Zone",
        "class": "Other",
        "lights": ["4"],
        "sensors": [],
        "action": {"on": False, "bri": 254},
    },
}

# Scene documents + the light states each recall applies. GroupScenes carry
# their group id; the LightScene has no group (recalled via group 0).
_SCENES: dict[str, dict[str, Any]] = {
    "AB34ef5-presnt1": {
        "name": "Presentation",
        "type": "GroupScene",
        "group": "1",
        "lights": ["1", "2"],
        "owner": "sim",
        "recycle": False,
        "locked": False,
        "lightstates": {
            "1": {"on": True, "bri": 60, "ct": 400},
            "2": {"on": False},
        },
    },
    "CD78gh9-bright2": {
        "name": "Bright",
        "type": "GroupScene",
        "group": "1",
        "lights": ["1", "2"],
        "owner": "sim",
        "recycle": False,
        "locked": False,
        "lightstates": {
            "1": {"on": True, "bri": 254, "ct": 250},
            "2": {"on": True, "bri": 254, "ct": 250},
        },
    },
    "EF12ij3-energz": {
        "name": "Energize",
        "type": "LightScene",
        "lights": ["3"],
        "owner": "sim",
        "recycle": False,
        "locked": False,
        "lightstates": {
            "3": {"on": True, "bri": 254, "ct": 156},
        },
    },
}

# Fields a light must be ON to accept (Hue error 201 otherwise).
_DIMMING_FIELDS = ("bri", "hue", "sat", "xy", "ct", "effect")


def _error(etype: int, address: str, description: str) -> dict[str, Any]:
    return {"error": {"type": etype, "address": address, "description": description}}


class PhilipsHueSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "philips_hue",
        "name": "Philips Hue Bridge Simulator",
        "category": "lighting",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "bridge_name": "Sim Hue Bridge",
            "bridge_id": "001788FFFEABCDEF",
            "model_id": "BSB002",
            "api_version": "1.65.0",
            "sw_version": "1965055050",
            "mac_address": "00:17:88:ab:cd:ef",
            "zigbee_channel": 25,
            "link_button_pressed": False,
            "lights_on": 3,
        },
        "delays": {
            "command_response": 0.02,
        },
        "error_modes": {
            "communication_timeout": {
                "description": "Bridge stops responding to requests",
                "behavior": "no_response",
            },
            "unauthorized": {
                "description": "Bridge rejects the app key (Hue error 1)",
            },
        },
        "controls": [
            {
                "type": "toggle",
                "key": "link_button_pressed",
                "label": "Link Button Pressed",
            },
            {"type": "indicator", "key": "bridge_name", "label": "Bridge Name"},
            {"type": "indicator", "key": "bridge_id", "label": "Bridge ID"},
            {"type": "indicator", "key": "model_id", "label": "Model ID"},
            {"type": "indicator", "key": "lights_on", "label": "Lights On"},
            {"type": "indicator", "key": "zigbee_channel", "label": "Zigbee Channel"},
        ],
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Deep copies so mutations are isolated per instance.
        self._lights: dict[str, dict[str, Any]] = json.loads(json.dumps(_LIGHTS))
        self._groups: dict[str, dict[str, Any]] = json.loads(json.dumps(_GROUPS))
        self._scenes: dict[str, dict[str, Any]] = json.loads(json.dumps(_SCENES))
        self._pair_counter = 0

    # ── Request routing ──

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        parts = [p for p in path.split("?")[0].split("/") if p]
        if not parts or parts[0] != "api":
            return 404, _json([_error(3, path, "resource not available")])

        # POST /api — pairing (unauthenticated).
        if len(parts) == 1:
            if method != "POST":
                return 200, _json([_error(4, "/api", "method not available")])
            return self._handle_pair(body)

        # GET /api/config — unauthenticated discovery subset.
        if len(parts) == 2 and parts[1] == "config" and method == "GET":
            return 200, {
                "name": self.get_state("bridge_name", "Sim Hue Bridge"),
                "datastoreversion": "103",
                "swversion": self.get_state("sw_version", ""),
                "apiversion": self.get_state("api_version", ""),
                "mac": self.get_state("mac_address", ""),
                "bridgeid": self.get_state("bridge_id", ""),
                "factorynew": False,
                "replacesbridgeid": None,
                "modelid": self.get_state("model_id", "BSB002"),
            }

        # Everything below requires a valid app key.
        app_key = parts[1]
        if "unauthorized" in self.active_errors or not app_key or app_key == _BAD_KEY:
            return 200, _json([_error(1, "/", "unauthorized user")])

        rest = parts[2:]
        if not rest:
            if method == "GET":
                return 200, self._full_state()
            return 200, _json([_error(4, "/", "method not available")])

        resource = rest[0]
        if resource == "config":
            return self._handle_config(method, body)
        if resource == "lights":
            return self._handle_lights(method, rest[1:], body)
        if resource == "groups":
            return self._handle_groups(method, rest[1:], body)
        if resource == "scenes":
            return self._handle_scenes(method, rest[1:])
        return 200, _json([_error(3, "/" + "/".join(rest), "resource not available")])

    # ── Pairing ──

    def _handle_pair(self, body: str) -> tuple[int, dict | str]:
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict) or not payload.get("devicetype"):
            return 200, _json([_error(5, "/", "invalid/missing parameters in body")])
        if not self.get_state("link_button_pressed", False):
            return 200, _json([_error(101, "/", "link button not pressed")])
        self._pair_counter += 1
        username = f"simhueappkey{self._pair_counter:04d}"
        return 200, _json([{"success": {"username": username}}])

    # ── Config ──

    def _config_document(self) -> dict[str, Any]:
        return {
            "name": self.get_state("bridge_name", "Sim Hue Bridge"),
            "bridgeid": self.get_state("bridge_id", ""),
            "modelid": self.get_state("model_id", "BSB002"),
            "apiversion": self.get_state("api_version", ""),
            "swversion": self.get_state("sw_version", ""),
            "mac": self.get_state("mac_address", ""),
            "zigbeechannel": self.get_state("zigbee_channel", 25),
            "datastoreversion": "103",
            "factorynew": False,
        }

    def _handle_config(self, method: str, body: str) -> tuple[int, dict | str]:
        if method == "GET":
            return 200, self._config_document()
        if method == "PUT":
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {}
            results = []
            if isinstance(payload, dict) and "name" in payload:
                self.set_state("bridge_name", str(payload["name"]))
                results.append({"success": {"/config/name": str(payload["name"])}})
            if not results:
                results.append(_error(6, "/config", "parameter not available"))
            return 200, _json(results)
        return 200, _json([_error(4, "/config", "method not available")])

    # ── Lights ──

    def _refresh_lights_on(self) -> None:
        self.set_state(
            "lights_on",
            sum(1 for li in self._lights.values() if li["state"].get("on")),
        )

    def _handle_lights(
        self, method: str, rest: list[str], body: str
    ) -> tuple[int, dict | str]:
        if not rest:
            if method == "GET":
                return 200, self._lights
            return 200, _json([_error(4, "/lights", "method not available")])
        lid = rest[0]
        light = self._lights.get(lid)
        if light is None:
            return 200, _json(
                [_error(3, f"/lights/{lid}", "resource not available")]
            )
        if len(rest) == 1 and method == "GET":
            return 200, light
        if len(rest) == 2 and rest[1] == "state" and method == "PUT":
            return self._apply_light_state(lid, body)
        return 200, _json(
            [_error(4, f"/lights/{lid}", "method not available")]
        )

    def _apply_light_state(self, lid: str, body: str) -> tuple[int, dict | str]:
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            return 200, _json([_error(5, f"/lights/{lid}/state", "invalid body")])
        state = self._lights[lid]["state"]
        results: list[dict[str, Any]] = []
        turning_on = bool(payload.get("on", False))
        for field, value in payload.items():
            address = f"/lights/{lid}/state/{field}"
            if field == "on":
                state["on"] = bool(value)
                results.append({"success": {address: bool(value)}})
                continue
            if field in ("alert",):
                # Transient — acknowledged, not stored.
                results.append({"success": {address: value}})
                continue
            if field not in state:
                results.append(
                    _error(6, address, f"parameter, {field}, not available")
                )
                continue
            if field in _DIMMING_FIELDS and not (state.get("on") or turning_on):
                # Real-bridge behavior: dimming/color writes to an off
                # light are rejected per-field with error 201.
                results.append(
                    _error(
                        201, address,
                        f"parameter, {field}, is not modifiable. "
                        f"Device is set to off.",
                    )
                )
                continue
            state[field] = value
            if field in ("hue", "sat"):
                state["colormode"] = "hs"
            elif field == "xy":
                state["colormode"] = "xy"
            elif field == "ct":
                state["colormode"] = "ct"
            results.append({"success": {address: value}})
        self._refresh_group_states()
        self._refresh_lights_on()
        return 200, _json(results)

    # ── Groups ──

    def _group_members(self, gid: str) -> list[str]:
        if gid == "0":
            return list(self._lights.keys())
        group = self._groups.get(gid)
        return list(group.get("lights", [])) if group else []

    def _group_document(self, gid: str) -> dict[str, Any] | None:
        if gid == "0":
            members = self._group_members("0")
            doc = {
                "name": "Group 0",
                "type": "LightGroup",
                "lights": members,
                "sensors": [],
                "action": {"on": any(
                    self._lights[m]["state"].get("on") for m in members
                )},
            }
        else:
            doc = self._groups.get(gid)
            if doc is None:
                return None
            members = self._group_members(gid)
        states = [bool(self._lights[m]["state"].get("on"))
                  for m in members if m in self._lights]
        doc["state"] = {
            "any_on": any(states) if states else False,
            "all_on": all(states) if states else False,
        }
        return doc

    def _refresh_group_states(self) -> None:
        for gid in self._groups:
            self._group_document(gid)

    def _handle_groups(
        self, method: str, rest: list[str], body: str
    ) -> tuple[int, dict | str]:
        if not rest:
            if method == "GET":
                # Group 0 is excluded from the listing, like a real bridge.
                return 200, {
                    gid: self._group_document(gid) for gid in self._groups
                }
            return 200, _json([_error(4, "/groups", "method not available")])
        gid = rest[0]
        doc = self._group_document(gid)
        if doc is None:
            return 200, _json(
                [_error(3, f"/groups/{gid}", "resource not available")]
            )
        if len(rest) == 1 and method == "GET":
            return 200, doc
        if len(rest) == 2 and rest[1] == "action" and method == "PUT":
            return self._apply_group_action(gid, body)
        return 200, _json([_error(4, f"/groups/{gid}", "method not available")])

    def _apply_group_action(self, gid: str, body: str) -> tuple[int, dict | str]:
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            return 200, _json([_error(5, f"/groups/{gid}/action", "invalid body")])
        members = self._group_members(gid)
        results: list[dict[str, Any]] = []
        for field, value in payload.items():
            address = f"/groups/{gid}/action/{field}"
            if field == "scene":
                scene = self._scenes.get(str(value))
                if scene is None:
                    results.append(_error(7, address, "invalid value for scene"))
                    continue
                targets = set(scene.get("lights", []))
                if gid != "0":
                    targets &= set(members)
                for lid in targets:
                    light = self._lights.get(lid)
                    if light is None:
                        continue
                    for f, v in scene.get("lightstates", {}).get(lid, {}).items():
                        if f == "on" or f in light["state"]:
                            light["state"][f] = v
                results.append({"success": {address: value}})
                continue
            # on/bri/hue/sat/xy/ct/effect/alert fan out to member lights
            # that support the field.
            for lid in members:
                light = self._lights.get(lid)
                if light is None:
                    continue
                if field == "on":
                    light["state"]["on"] = bool(value)
                elif field in light["state"]:
                    light["state"][field] = value
                    if field in ("hue", "sat"):
                        light["state"]["colormode"] = "hs"
                    elif field == "xy":
                        light["state"]["colormode"] = "xy"
                    elif field == "ct":
                        light["state"]["colormode"] = "ct"
            if gid != "0" and field != "alert":
                self._groups[gid].setdefault("action", {})[field] = value
            results.append({"success": {address: value}})
        self._refresh_group_states()
        self._refresh_lights_on()
        return 200, _json(results)

    # ── Scenes ──

    def _handle_scenes(self, method: str, rest: list[str]) -> tuple[int, dict | str]:
        if method != "GET":
            return 200, _json([_error(4, "/scenes", "method not available")])
        if not rest:
            # The listing omits per-scene lightstates, like a real bridge.
            return 200, {
                sid: {k: v for k, v in doc.items() if k != "lightstates"}
                for sid, doc in self._scenes.items()
            }
        sid = rest[0]
        doc = self._scenes.get(sid)
        if doc is None:
            return 200, _json(
                [_error(3, f"/scenes/{sid}", "resource not available")]
            )
        return 200, doc

    # ── Full state ──

    def _full_state(self) -> dict[str, Any]:
        return {
            "config": self._config_document(),
            "lights": self._lights,
            "groups": {gid: self._group_document(gid) for gid in self._groups},
            "scenes": {
                sid: {k: v for k, v in doc.items() if k != "lightstates"}
                for sid, doc in self._scenes.items()
            },
            "sensors": {},
        }


def _json(obj: Any) -> str:
    """Serialize a top-level list response (HTTPSimulator auto-serializes
    dicts only)."""
    return json.dumps(obj)
