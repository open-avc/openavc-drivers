"""
Philips Hue Bridge — Simulator (CLIP API v2)

Simulates a Hue Bridge's CLIP v2 API: a small install of five lights with
different capability sets (extended color, color, color temperature,
dimmable, on/off plug), two rooms + one zone, the whole-home bridge_home
group, and three scenes — each group with its grouped_light service, each
light with its owner device and zigbee_connectivity resource, exactly the
resource graph a real bridge serves from GET /clip/v2/resource.

Event stream: GET /eventstream/clip/v2 with Accept: text/event-stream is
held open, and every mutation (PUT handlers, Simulator UI toggles) emits
the real bridge's envelope format — a JSON array of
{creationtime, data: [changed resources], id, type: "update"} objects — so
the driver's push path is exercised end to end. Like the real bridge, the
stream sends no keepalive.

Pairing: POST /api succeeds only while the "Link Button Pressed" toggle is
on (otherwise Hue error 101), mirroring the physical link-button flow. The
toggle stays on until switched off (a real bridge's window auto-expires
after ~30 s; the sim keeps it manual so pairing is predictable).

Authentication: CLIP v2 endpoints answer 403 unless the
hue-application-key header carries a non-empty key other than the literal
"invalid" (the platform's designated bad-credential sentinel), matching
the real bridge's 403-on-bad-key behavior. The unauthenticated v1
GET /api/config discovery endpoint answers with the bridge-id subset like
a real bridge (it serves this on plain HTTP).

Driver: philips_hue
Transport: http
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any

from simulator.http_simulator import HTTPSimulator

_BAD_KEY = "invalid"

# Deterministic fake UUIDs — readable in tests and protocol logs.
L1 = "00000000-0000-0000-0000-00000000li01"  # Boardroom Front (extended color)
L2 = "00000000-0000-0000-0000-00000000li02"  # Boardroom Rear (color)
L3 = "00000000-0000-0000-0000-00000000li03"  # Lobby Downlight (color temperature)
L4 = "00000000-0000-0000-0000-00000000li04"  # Lobby Sconce (dimmable)
L5 = "00000000-0000-0000-0000-00000000li05"  # Rack Plug (on/off)
D1, D2, D3, D4, D5 = (
    "00000000-0000-0000-0000-00000000de01",
    "00000000-0000-0000-0000-00000000de02",
    "00000000-0000-0000-0000-00000000de03",
    "00000000-0000-0000-0000-00000000de04",
    "00000000-0000-0000-0000-00000000de05",
)
Z1, Z2, Z3, Z4, Z5 = (
    "00000000-0000-0000-0000-00000000zc01",
    "00000000-0000-0000-0000-00000000zc02",
    "00000000-0000-0000-0000-00000000zc03",
    "00000000-0000-0000-0000-00000000zc04",
    "00000000-0000-0000-0000-00000000zc05",
)
ROOM_BOARD = "00000000-0000-0000-0000-0000000ro01"
ROOM_LOBBY = "00000000-0000-0000-0000-0000000ro02"
ZONE_STAGE = "00000000-0000-0000-0000-0000000zo01"
HOME = "00000000-0000-0000-0000-0000000ho01"
GL_BOARD = "00000000-0000-0000-0000-0000000gl01"
GL_LOBBY = "00000000-0000-0000-0000-0000000gl02"
GL_STAGE = "00000000-0000-0000-0000-0000000gl03"
GL_HOME = "00000000-0000-0000-0000-0000000gl00"
SCENE_PRESENT = "00000000-0000-0000-0000-0000000sc01"
SCENE_RELAX = "00000000-0000-0000-0000-0000000sc02"
SCENE_WASH = "00000000-0000-0000-0000-0000000sc03"
BRIDGE = "00000000-0000-0000-0000-0000000br01"
BRIDGE_DEV = "00000000-0000-0000-0000-0000000bd01"

_MEMBER_LIGHTS = {
    GL_BOARD: [L1, L2],
    GL_LOBBY: [L3, L4],
    GL_STAGE: [L1, L3],
    GL_HOME: [L1, L2, L3, L4, L5],
}


def _light(
    lid: str, owner: str, name: str,
    on: bool = False,
    brightness: float | None = None,
    mirek: int | None = None,
    has_ct: bool = False,
    mirek_min: int = 153, mirek_max: int = 500,
    xy: tuple[float, float] | None = None,
    id_v1: str = "",
) -> dict[str, Any]:
    res: dict[str, Any] = {
        "id": lid,
        "id_v1": id_v1,
        "type": "light",
        "owner": {"rid": owner, "rtype": "device"},
        "metadata": {"name": name, "archetype": "sultan_bulb"},
        "mode": "normal",
        "on": {"on": on},
    }
    if brightness is not None:
        res["dimming"] = {"brightness": brightness, "min_dim_level": 0.2}
    if has_ct:
        res["color_temperature"] = {
            "mirek": mirek,
            "mirek_valid": mirek is not None,
            "mirek_schema": {
                "mirek_minimum": mirek_min,
                "mirek_maximum": mirek_max,
            },
        }
    if xy is not None:
        res["color"] = {
            "xy": {"x": xy[0], "y": xy[1]},
            "gamut_type": "C",
        }
    return res


def _device(rid: str, name: str, product: str, model: str,
            services: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "id": rid,
        "type": "device",
        "metadata": {"name": name, "archetype": "sultan_bulb"},
        "product_data": {
            "model_id": model,
            "manufacturer_name": "Signify Netherlands B.V.",
            "product_name": product,
            "product_archetype": "sultan_bulb",
            "certified": True,
            "software_version": "1.108.7",
        },
        "services": [{"rid": r, "rtype": t} for r, t in services],
    }


def _connectivity(rid: str, owner: str, status: str = "connected") -> dict[str, Any]:
    return {
        "id": rid,
        "type": "zigbee_connectivity",
        "owner": {"rid": owner, "rtype": "device"},
        "status": status,
    }


def _grouped_light(rid: str, owner: str, owner_type: str,
                   id_v1: str = "") -> dict[str, Any]:
    return {
        "id": rid,
        "id_v1": id_v1,
        "type": "grouped_light",
        "owner": {"rid": owner, "rtype": owner_type},
        "on": {"on": False},
        "dimming": {"brightness": 0.0},
    }


def _build_resources() -> dict[str, dict[str, Any]]:
    """The bridge's full resource graph, keyed by resource id."""
    resources: list[dict[str, Any]] = [
        _light(L1, D1, "Boardroom Front",
               on=True, brightness=78.74, mirek=366, has_ct=True,
               xy=(0.4573, 0.4100), id_v1="/lights/1"),
        _light(L2, D2, "Boardroom Rear",
               on=False, brightness=100.0, xy=(0.3127, 0.3290),
               id_v1="/lights/2"),
        _light(L3, D3, "Lobby Downlight",
               on=True, brightness=70.87, mirek=250, has_ct=True,
               mirek_min=153, mirek_max=454, id_v1="/lights/3"),
        _light(L4, D4, "Lobby Sconce",
               on=True, brightness=100.0, id_v1="/lights/4"),
        _light(L5, D5, "Rack Plug",
               on=False, id_v1="/lights/5"),
        _device(D1, "Boardroom Front", "Hue color lamp", "LCT015",
                [(L1, "light"), (Z1, "zigbee_connectivity")]),
        _device(D2, "Boardroom Rear", "Hue color lamp", "LCT015",
                [(L2, "light"), (Z2, "zigbee_connectivity")]),
        _device(D3, "Lobby Downlight", "Hue ambiance downlight", "LTW013",
                [(L3, "light"), (Z3, "zigbee_connectivity")]),
        _device(D4, "Lobby Sconce", "Hue white lamp", "LWB010",
                [(L4, "light"), (Z4, "zigbee_connectivity")]),
        _device(D5, "Rack Plug", "Hue Smart plug", "LOM001",
                [(L5, "light"), (Z5, "zigbee_connectivity")]),
        _connectivity(Z1, D1),
        _connectivity(Z2, D2),
        _connectivity(Z3, D3),
        _connectivity(Z4, D4),
        _connectivity(Z5, D5, status="connectivity_issue"),
        {
            "id": ROOM_BOARD, "id_v1": "/groups/1", "type": "room",
            "metadata": {"name": "Boardroom", "archetype": "office"},
            "children": [
                {"rid": D1, "rtype": "device"},
                {"rid": D2, "rtype": "device"},
            ],
            "services": [{"rid": GL_BOARD, "rtype": "grouped_light"}],
        },
        {
            "id": ROOM_LOBBY, "id_v1": "/groups/2", "type": "room",
            "metadata": {"name": "Lobby", "archetype": "lounge"},
            "children": [
                {"rid": D3, "rtype": "device"},
                {"rid": D4, "rtype": "device"},
            ],
            "services": [{"rid": GL_LOBBY, "rtype": "grouped_light"}],
        },
        {
            "id": ZONE_STAGE, "id_v1": "/groups/3", "type": "zone",
            "metadata": {"name": "Stage Wash", "archetype": "other"},
            "children": [
                {"rid": L1, "rtype": "light"},
                {"rid": L3, "rtype": "light"},
            ],
            "services": [{"rid": GL_STAGE, "rtype": "grouped_light"}],
        },
        {
            "id": HOME, "id_v1": "/groups/0", "type": "bridge_home",
            "children": [
                {"rid": d, "rtype": "device"} for d in (D1, D2, D3, D4, D5)
            ],
            "services": [{"rid": GL_HOME, "rtype": "grouped_light"}],
        },
        _grouped_light(GL_BOARD, ROOM_BOARD, "room", "/groups/1"),
        _grouped_light(GL_LOBBY, ROOM_LOBBY, "room", "/groups/2"),
        _grouped_light(GL_STAGE, ZONE_STAGE, "zone", "/groups/3"),
        _grouped_light(GL_HOME, HOME, "bridge_home", "/groups/0"),
        {
            "id": SCENE_PRESENT, "type": "scene",
            "metadata": {"name": "Presentation"},
            "group": {"rid": ROOM_BOARD, "rtype": "room"},
            "speed": 0.5,
            "actions": [
                {"target": {"rid": L1, "rtype": "light"},
                 "action": {"on": {"on": True},
                            "dimming": {"brightness": 100.0},
                            "color_temperature": {"mirek": 233}}},
                {"target": {"rid": L2, "rtype": "light"},
                 "action": {"on": {"on": False}}},
            ],
        },
        {
            "id": SCENE_RELAX, "type": "scene",
            "metadata": {"name": "Relax"},
            "group": {"rid": ROOM_LOBBY, "rtype": "room"},
            "speed": 0.5,
            "actions": [
                {"target": {"rid": L3, "rtype": "light"},
                 "action": {"on": {"on": True},
                            "dimming": {"brightness": 56.3},
                            "color_temperature": {"mirek": 447}}},
                {"target": {"rid": L4, "rtype": "light"},
                 "action": {"on": {"on": True},
                            "dimming": {"brightness": 40.0}}},
            ],
        },
        {
            "id": SCENE_WASH, "type": "scene",
            "metadata": {"name": "Wash Blue"},
            "group": {"rid": ZONE_STAGE, "rtype": "zone"},
            "speed": 0.5,
            "actions": [
                {"target": {"rid": L1, "rtype": "light"},
                 "action": {"on": {"on": True},
                            "color": {"xy": {"x": 0.153, "y": 0.048}}}},
                {"target": {"rid": L3, "rtype": "light"},
                 "action": {"on": {"on": True},
                            "dimming": {"brightness": 80.0}}},
            ],
        },
        {
            "id": BRIDGE, "type": "bridge",
            "bridge_id": "001788fffeabcdef",
            "owner": {"rid": BRIDGE_DEV, "rtype": "device"},
            "time_zone": {"time_zone": "America/New_York"},
        },
        _device(BRIDGE_DEV, "Sim Hue Bridge", "Hue Bridge", "BSB002",
                [(BRIDGE, "bridge")]),
    ]
    return {res["id"]: res for res in resources}


class PhilipsHueSimulator(HTTPSimulator):
    """Simulated Hue Bridge (CLIP v2 + eventstream + v1 pairing)."""

    SIMULATOR_INFO = {
        "driver_id": "philips_hue",
        "name": "Philips Hue Bridge Simulator",
        "category": "lighting",
        "transport": "http",
        "default_port": 443,
        "initial_state": {
            "bridge_name": "Sim Hue Bridge",
            "bridge_id": "001788FFFEABCDEF",
            "model_id": "BSB002",
            "sw_version": "1.66.1966155570",
            "link_button_pressed": False,
            "lights_on": 3,
            "light_1_on": True,
            "light_1_reachable": True,
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
                "description": "Bridge rejects the app key (HTTP 403)",
            },
        },
        "controls": [
            {
                "type": "toggle",
                "key": "link_button_pressed",
                "label": "Link Button Pressed",
            },
            {
                "type": "toggle",
                "key": "light_1_on",
                "label": "Boardroom Front On",
            },
            {
                "type": "toggle",
                "key": "light_1_reachable",
                "label": "Boardroom Front Reachable",
            },
            {"type": "indicator", "key": "bridge_name", "label": "Bridge Name"},
            {"type": "indicator", "key": "bridge_id", "label": "Bridge ID"},
            {"type": "indicator", "key": "model_id", "label": "Model ID"},
            {"type": "indicator", "key": "lights_on", "label": "Lights On"},
        ],
    }

    # The eventstream endpoint is served as SSE by the HTTPSimulator base.
    sse_paths = ["/eventstream/clip/v2"]

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        # Deep copies so mutations are isolated per instance.
        self.resources: dict[str, dict[str, Any]] = copy.deepcopy(
            _build_resources()
        )
        self._event_counter = 0
        self._recompute_groups(emit=False)
        self._refresh_lights_on()

    # ── State-change entry point (Simulator UI toggles) ──

    def set_state(self, key: str, value: Any) -> None:
        prev = self.state.get(key)
        super().set_state(key, value)
        if prev == value:
            return
        if key == "light_1_on":
            light = self.resources[L1]
            if light["on"]["on"] != bool(value):
                light["on"]["on"] = bool(value)
                changed = self._recompute_groups(emit=False)
                self._emit_update([self.resources[L1]] + changed)
                self._refresh_lights_on()
        elif key == "light_1_reachable":
            zc = self.resources[Z1]
            status = "connected" if value else "connectivity_issue"
            if zc["status"] != status:
                zc["status"] = status
                self._emit_update([zc])
        elif key == "bridge_name":
            bdev = self.resources[BRIDGE_DEV]
            if bdev["metadata"]["name"] != str(value):
                bdev["metadata"]["name"] = str(value)
                self._emit_update([bdev])

    # ── Event emission ──

    def _emit_update(self, resources: list[dict[str, Any]]) -> None:
        """Emit one SSE message carrying update envelopes for ``resources``
        (the real bridge batches concurrent changes the same way)."""
        if not resources:
            return
        self._event_counter += 1
        envelope = [{
            "creationtime": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "data": [copy.deepcopy(res) for res in resources],
            "id": f"00000000-0000-0000-0000-00000000ev{self._event_counter:02d}",
            "type": "update",
        }]
        self.push_sse_event(json.dumps(envelope))

    # ── Group state recomputation ──

    def _recompute_groups(self, emit: bool = True) -> list[dict[str, Any]]:
        """Recompute every grouped_light's on/brightness from its members.
        Returns (and optionally emits) the grouped_light resources that
        changed."""
        changed: list[dict[str, Any]] = []
        for gl_rid, members in _MEMBER_LIGHTS.items():
            gl = self.resources[gl_rid]
            lights = [self.resources[lid] for lid in members]
            any_on = any(li["on"]["on"] for li in lights)
            lit = [
                li["dimming"]["brightness"]
                for li in lights
                if li["on"]["on"] and "dimming" in li
            ]
            avg = round(sum(lit) / len(lit), 2) if lit else 0.0
            if gl["on"]["on"] != any_on or gl["dimming"]["brightness"] != avg:
                gl["on"]["on"] = any_on
                gl["dimming"]["brightness"] = avg
                changed.append(gl)
        if emit:
            self._emit_update(changed)
        return changed

    def _refresh_lights_on(self) -> None:
        count = sum(
            1 for res in self.resources.values()
            if res.get("type") == "light" and res["on"]["on"]
        )
        super().set_state("lights_on", count)
        super().set_state("light_1_on", self.resources[L1]["on"]["on"])

    # ── Request handling ──

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str,
    ) -> tuple[int, dict | str]:
        parts = [p for p in path.split("?")[0].split("/") if p]

        # v1 pairing surface (unauthenticated).
        if parts and parts[0] == "api":
            if len(parts) == 1 and method == "POST":
                return self._handle_pair(body)
            if len(parts) == 2 and parts[1] == "config" and method == "GET":
                return 200, {
                    "name": self.get_state("bridge_name", "Sim Hue Bridge"),
                    "datastoreversion": "166",
                    "swversion": self.get_state("sw_version", ""),
                    "apiversion": "1.66.0",
                    "mac": "00:17:88:ab:cd:ef",
                    "bridgeid": self.get_state("bridge_id", ""),
                    "factorynew": False,
                    "replacesbridgeid": None,
                    "modelid": self.get_state("model_id", "BSB002"),
                }
            return 404, json.dumps(
                [{"error": {"type": 3, "address": path,
                            "description": "resource not available"}}]
            )

        # CLIP v2 requires the application-key header.
        app_key = ""
        for hname, hval in headers.items():
            if hname.lower() == "hue-application-key":
                app_key = hval
                break
        if (
            "unauthorized" in self.active_errors
            or not app_key
            or app_key == _BAD_KEY
        ):
            return 403, json.dumps(
                {"errors": [{"description": "unauthorized user"}], "data": []}
            )

        if parts[:3] == ["clip", "v2", "resource"]:
            rest = parts[3:]
            if method == "GET":
                return self._handle_get(rest)
            if method == "PUT" and len(rest) == 2:
                return self._handle_put(rest[0], rest[1], body)
            return 405, json.dumps(
                {"errors": [{"description": "method not available"}],
                 "data": []}
            )

        return 404, json.dumps(
            {"errors": [{"description": "resource not available"}], "data": []}
        )

    def _handle_pair(self, body: str) -> tuple[int, Any]:
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict) or not payload.get("devicetype"):
            return 200, json.dumps(
                [{"error": {"type": 5, "address": "/",
                            "description": "invalid/missing parameters"}}]
            )
        if not self.get_state("link_button_pressed", False):
            return 200, json.dumps(
                [{"error": {"type": 101, "address": "",
                            "description": "link button not pressed"}}]
            )
        success: dict[str, Any] = {"username": "simulated-app-key-0001"}
        if payload.get("generateclientkey"):
            success["clientkey"] = "00112233445566778899AABBCCDDEEFF"
        return 200, json.dumps([{"success": success}])

    def _handle_get(self, rest: list[str]) -> tuple[int, Any]:
        if not rest:
            data = [copy.deepcopy(r) for r in self.resources.values()]
        elif len(rest) == 1:
            data = [
                copy.deepcopy(r) for r in self.resources.values()
                if r.get("type") == rest[0]
            ]
        else:
            res = self.resources.get(rest[1])
            if res is None or res.get("type") != rest[0]:
                return 404, json.dumps(
                    {"errors": [{"description": "resource not found"}],
                     "data": []}
                )
            data = [copy.deepcopy(res)]
        return 200, json.dumps({"errors": [], "data": data})

    def _handle_put(
        self, rtype: str, rid: str, body: str
    ) -> tuple[int, Any]:
        res = self.resources.get(rid)
        if res is None or res.get("type") != rtype:
            return 404, json.dumps(
                {"errors": [{"description": "resource not found"}], "data": []}
            )
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            return 400, json.dumps(
                {"errors": [{"description": "body is not valid JSON"}],
                 "data": []}
            )
        if not isinstance(payload, dict):
            return 400, json.dumps(
                {"errors": [{"description": "body must be an object"}],
                 "data": []}
            )

        if rtype == "light":
            self._apply_light_put(res, payload)
            changed = self._recompute_groups(emit=False)
            self._emit_update([res] + changed)
            self._refresh_lights_on()
        elif rtype == "grouped_light":
            self._apply_grouped_put(rid, payload)
        elif rtype == "scene":
            recall = payload.get("recall")
            if isinstance(recall, dict) and recall.get("action") in (
                "active", "static", "dynamic_palette"
            ):
                self._apply_scene_recall(res)
            else:
                return 400, json.dumps(
                    {"errors": [{"description": "unsupported scene action"}],
                     "data": []}
                )
        elif rtype == "device":
            meta = payload.get("metadata")
            if isinstance(meta, dict) and meta.get("name"):
                res["metadata"]["name"] = str(meta["name"])
                if rid == BRIDGE_DEV:
                    super().set_state("bridge_name", str(meta["name"]))
                self._emit_update([res])
            # identify: acknowledged, no state change (the real light just
            # breathes once).
        else:
            return 400, json.dumps(
                {"errors": [{"description": "resource is not writable"}],
                 "data": []}
            )

        return 200, json.dumps(
            {"errors": [], "data": [{"rid": rid, "rtype": rtype}]}
        )

    # ── PUT bodies ──

    @staticmethod
    def _apply_light_put(res: dict[str, Any], payload: dict[str, Any]) -> None:
        on = payload.get("on")
        if isinstance(on, dict) and "on" in on:
            res["on"]["on"] = bool(on["on"])
        dimming = payload.get("dimming")
        if (
            isinstance(dimming, dict)
            and "brightness" in dimming
            and "dimming" in res
        ):
            value = max(0.2, min(100.0, float(dimming["brightness"])))
            res["dimming"]["brightness"] = round(value, 2)
        ct = payload.get("color_temperature")
        if (
            isinstance(ct, dict)
            and ct.get("mirek") is not None
            and "color_temperature" in res
        ):
            schema = res["color_temperature"]["mirek_schema"]
            value = max(
                schema["mirek_minimum"],
                min(schema["mirek_maximum"], int(ct["mirek"])),
            )
            res["color_temperature"]["mirek"] = value
            res["color_temperature"]["mirek_valid"] = True
        color = payload.get("color")
        if (
            isinstance(color, dict)
            and isinstance(color.get("xy"), dict)
            and "color" in res
        ):
            res["color"]["xy"] = {
                "x": float(color["xy"].get("x", 0.0)),
                "y": float(color["xy"].get("y", 0.0)),
            }
            if "color_temperature" in res:
                res["color_temperature"]["mirek_valid"] = False

    def _apply_grouped_put(self, gl_rid: str, payload: dict[str, Any]) -> None:
        """Fan a grouped_light PUT out to member lights, then recompute and
        emit — one batch, like the real bridge."""
        members = _MEMBER_LIGHTS.get(gl_rid, [])
        changed_lights: list[dict[str, Any]] = []
        for lid in members:
            light = self.resources[lid]
            before = json.dumps(light, sort_keys=True)
            self._apply_light_put(light, payload)
            if json.dumps(light, sort_keys=True) != before:
                changed_lights.append(light)
        changed_groups = self._recompute_groups(emit=False)
        self._emit_update(changed_lights + changed_groups)
        self._refresh_lights_on()

    def _apply_scene_recall(self, scene: dict[str, Any]) -> None:
        changed: list[dict[str, Any]] = []
        for action in scene.get("actions", []):
            target = (action.get("target") or {}).get("rid")
            light = self.resources.get(target)
            if light is None:
                continue
            before = json.dumps(light, sort_keys=True)
            self._apply_light_put(light, action.get("action") or {})
            if json.dumps(light, sort_keys=True) != before:
                changed.append(light)
        changed += self._recompute_groups(emit=False)
        self._emit_update(changed)
        self._refresh_lights_on()
