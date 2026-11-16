"""SpinetiX iBX440 signage player, Direct RPC v6 (pp. 24–29, 38–43, 53–62).

Python joins videoConnectors and display by connector ID and reconciles arbitrary
named Web Storage keys after each read. The requests all go to the player's
authenticated /rpc endpoint. The separate RPC concentrator is site-configured.
"""

import asyncio
import hashlib
import re
from urllib.parse import urlsplit

from openavc.drivers.base import BaseDriver, ConnectionFaultError

try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:  # Older releases still deliver the refusal as a command failure.
    CommandRefusedError = RuntimeError


_FLAGS = {key: True for key in (
    "display", "videoConnectors", "license", "storage", "temperature", "errors", "reason"
)}


class RPCRefusal(CommandRefusedError):
    """The player answered the request but declined it."""


def _child_id(name):
    # Preserve common IDs. Hash everything else, including names in the reserved
    # hash namespace: punctuation, Unicode and arbitrarily long names remain distinct.
    if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name) and not name.startswith("key_"):
        return name
    return "key_" + hashlib.sha256(name.encode("utf-8")).hexdigest()


def _by_id(rows):
    return {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")}


def _licensed_features(info):
    features = (info.get("license") or {}).get("features")
    if not isinstance(features, list):
        return None
    return {entry.get("name") for entry in features
            if isinstance(entry, dict) and entry.get("valid") is True}


def _output_values(port, display):
    mode = display.get("mode") or {}
    hdr = mode.get("hdr") or {}
    region = mode.get("region") or {}
    area = None
    if all(k in region for k in ("x1", "y1", "x2", "y2")):
        area = f"{region['x1']},{region['y1']} to {region['x2']},{region['y2']}"
    return {
        "connector_id": port["id"], "connection": port.get("status") or "unknown",
        "enabled": port.get("enabled"), "display_power": display.get("power") or "unknown",
        "power_save": display.get("power_save") or "unknown", "width": mode.get("width"),
        "height": mode.get("height"), "refresh_hz": mode.get("refresh"),
        "color_space": mode.get("colorspace"), "hdr_transfer": hdr.get("transfer"),
        "canvas_region": area, "hdr_gamut": hdr.get("gamut"), "bits_per_color": hdr.get("bpc"),
    }


class SpinetixIBX440Driver(BaseDriver):
    DRIVER_INFO = {
        "id": "spinetix_ibx440", "name": "SpinetiX iBX440", "manufacturer": "SpinetiX",
        "category": "streaming", "version": "1.0.0", "author": "OpenAVC",
        "description": "Monitor four HDMI outputs and the signage player, request a content update, and control named project variables on SYSTEMS players.",
        "transport": "http", "ports": [443, 80], "protocols": ["http", "json-rpc"],
        "source_url": "https://support.spinetix.com/w/images/3/3a/SpinetiX_Tech_RPC_API.pdf",
        "min_platform_version": "0.36.0", "verified": False, "simulated": True, "tags": ["digital-signage", "video-wall", "hdmi", "dsos"],
        "compatible_models": [{"manufacturer": "SpinetiX", "models": ["iBX440"], "confidence": "untested"}],
        "discovery": {
            "mdns": [{"service": "_spx-hmp._tcp.local.", "txt": {"model": "iBX440"}}],
            "hostname": ["^spx-dsos-001d5023[0-9a-f]{4}([.]local[.]?)?$"],
            "manufacturer_alias": ["SpinetiX", "SpinetiX SA"],
        },
        "default_config": {"host": "", "port": 443, "ssl": True, "verify_ssl": True,
                           "username": "admin", "password": "", "auth_type": "basic", "poll_interval": 10},
        "config_schema": {
            "host": {"type": "string", "label": "Player Address", "required": True},
            "port": {"type": "integer", "label": "Port", "default": 443, "min": 1, "max": 65535},
            "ssl": {"type": "boolean", "label": "Use HTTPS", "default": True},
            "verify_ssl": {"type": "boolean", "label": "Verify TLS Certificate", "default": True,
                           "help": "Use a trusted certificate or turn verification off for a self-signed player."},
            "username": {"type": "string", "label": "Username", "default": "admin"},
            "password": {"type": "string", "label": "Password", "secret": True},
            "poll_interval": {"type": "integer", "label": "Feedback Interval (s)", "default": 10, "min": 1},
        },
        "help": {
            "overview": "Read the iBX440's HDMI connector and display reports, temperature and content errors. On a SYSTEMS player, bind a project variable to a control or a trigger.",
            "setup": "Enter the local player address and credentials from Control Center. A monitoring account can read player and output status. For that feedback plus variable control, use an account with the monitoring and content rights or an admin account. Content downloads and restart require admin rights.",
            "connection": "Use the player’s HTTPS service and certificate. For a player commissioned for plain HTTP, select port 80 and turn off Use HTTPS. Display Power can be unknown even with a connected cable. Gone means the display is missing or did not reply. A blank video mode means the player supplied no active mode. Shared Variable Access shows whether the player reported a valid SYSTEMS license and allowed the list to be read.",
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "device_name": {"type": "string", "label": "Player Name"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "firmware_version": {"type": "string", "label": "Firmware Version"},
            "operating_mode": {"type": "string", "label": "Operating Mode", "cloud_priority": "high"},
            "temperature": {"type": "number", "label": "Internal Temperature", "unit": "°C"},
            "temperature_alarm": {"type": "string", "label": "Temperature Alarm", "cloud_priority": "high"},
            "content_error": {"type": "string", "label": "Latest Content Error"},
            "last_boot_reason": {"type": "string", "label": "Last Restart Reason"},
            "content_free": {"type": "number", "label": "Content Storage Free"},
            "storage_unit": {"type": "string", "label": "Storage Unit"},
            "variable_access": {"type": "string", "label": "Shared Variable Access"},
            "last_error": {"type": "string", "label": "Last Error"},
        },
        "child_entity_types": {
            "output": {"label": "HDMI Output", "label_plural": "HDMI Outputs", "id_format": {"type": "string"},
                "summary_fields": ["connection", "display_power", "width", "height"],
                "state_variables": {
                    "connector_id": {"type": "string", "label": "Connector ID"},
                    "connection": {"type": "string", "label": "Cable Connection", "cloud_priority": "high"},
                    "enabled": {"type": "boolean", "label": "Output Enabled"},
                    "display_power": {"type": "string", "label": "Display Power", "cloud_priority": "high"},
                    "power_save": {"type": "string", "label": "Power Saving"},
                    "width": {"type": "integer", "label": "Active Width", "unit": "px"},
                    "height": {"type": "integer", "label": "Active Height", "unit": "px"},
                    "refresh_hz": {"type": "number", "label": "Refresh Rate", "unit": "Hz"},
                    "color_space": {"type": "string", "label": "Color Space"},
                    "hdr_transfer": {"type": "string", "label": "HDR Transfer Function"},
                    "hdr_gamut": {"type": "string", "label": "HDR Color Gamut"},
                    "bits_per_color": {"type": "integer", "label": "Bits per Color Component"},
                    "canvas_region": {"type": "string", "label": "Canvas Region", "help": "Top-left and bottom-right pixel coordinates assigned to this output."},
                }},
            "variable": {"label": "Shared Variable", "label_plural": "Shared Variables",
                "id_format": {"type": "string"}, "summary_fields": ["value"],
                "state_variables": {
                    "name": {"type": "string", "label": "Project Variable Name"},
                    "value": {"type": "string", "label": "Value", "control": True},
                    "timestamp": {"type": "string", "label": "Last Change (Unix Milliseconds)"},
                }},
        },
        "commands": {
            "refresh": {"label": "Refresh Player Status", "params": {}, "query_for": "operating_mode",
                        "help": "Read the current HDMI connections, display power and active video modes from the player."},
            "set_variable": {"label": "Set Shared Variable",
                "params": {"variable": {"type": "child_id", "child_type": "variable", "required": True, "label": "Project Variable"},
                           "value": {"type": "string", "required": True, "label": "New Value"}},
                "sets": {"value": "{value}"}, "help": "Change the selected project variable and read its value back. The signage project determines what changes on screen."},
            "publish_project": {"label": "Pull Signage Project", "params": {
                "url": {"type": "string", "required": True, "label": "Project Listing URL"}},
                "help": "Queue a content download from a published HTTP or HTTPS project listing. Requires Pull Mode. The returned handle confirms queuing.",
                "confirm": "Queue this project for publication on the player?"},
            "restart": {"label": "Restart Player", "params": {}, "restarts_device_for": 120,
                        "help": "Schedule a clean player restart. Signage playback is interrupted during the restart.",
                        "confirm": "Restart this signage player?"},
        },
        "quick_actions": ["refresh", "set_variable"],
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, {**self.DRIVER_INFO["default_config"], **config}, state, events)
        self._lock = asyncio.Lock()
        self._request_id = 0
        self._names = {}
        self._features = None
        self._offer_variables(False)

    def _offer_variables(self, enabled):
        base = type(self).DRIVER_INFO
        commands = dict(base["commands"])
        if not enabled:
            del commands["set_variable"]
        self.DRIVER_INFO = {**base, "commands": commands,
                            "quick_actions": [name for name in base["quick_actions"] if name in commands]}

    async def _rpc(self, method, params):
        self._request_id += 1
        request_id = self._request_id
        response = await self.transport.request("POST", "/rpc", json_body={
            "method": method, "params": params, "id": request_id})
        if response.status_code == 401 and method == "get_info":
            raise ConnectionFaultError("The player refused the monitoring login.", code="auth_failed")
        if response.status_code in (401, 403):
            raise RPCRefusal("This account does not have permission for " + method + ".")
        if not response.ok:
            raise ConnectionError(f"The player returned HTTP {response.status_code} for {method}.")
        reply = response.json_data
        if not isinstance(reply, dict) or reply.get("id") != request_id or "result" not in reply:
            raise ConnectionError("The player returned an incomplete or mismatched RPC reply.")
        if reply.get("error") is not None:
            raise RPCRefusal(f"The player refused {method}: {reply['error']}")
        return reply["result"]

    async def _info(self):
        info = await self._rpc("get_info", [_FLAGS])
        if not isinstance(info, dict) or str(info.get("model", "")).lower() != "ibx440":
            raise ConnectionFaultError("The address did not report a SpinetiX iBX440.", code="invalid_config")
        return info

    async def _post_connect(self):
        try:
            await self.poll()
        except RPCRefusal as exc:
            raise ConnectionFaultError(str(exc), code="auth_failed") from exc

    async def _close_session(self):
        self._clear_variables("unknown")
        for local_id in self.list_children("output"):
            self.set_child_state_batch("output", local_id, {
                "connection": "unknown", "enabled": None, "display_power": "unknown",
                "power_save": "unknown", "width": None, "height": None, "refresh_hz": None,
                "color_space": None, "hdr_transfer": None, "canvas_region": None,
                "hdr_gamut": None, "bits_per_color": None,
            })

    async def _liveness_probe(self):
        try:
            await self._rpc("get_info", [])
        except RPCRefusal:
            pass  # A refusal is still an answer.

    def _reconcile(self, kind, entries):
        for child_id, (name, values) in entries.items():
            self.register_child(kind, child_id, initial_state={"label": name})
            self.set_child_state_batch(kind, child_id, values)
        for stale in set(self.list_children(kind)) - entries.keys():
            self.deregister_child(kind, stale)

    def _apply_info(self, info):
        ports = _by_id(info.get("videoConnectors") or [])
        displays = _by_id(info.get("display") or [])
        outputs = {}
        for key, port in ports.items():
            if port.get("type") == "HDMI-A" or port.get("type") == "HDMI":
                label = port.get("name") or key
                jack = re.fullmatch(r"HDMI ([1-4])", label)
                local_id = "HDMI-" + jack[1] if jack else _child_id(key)
                outputs[local_id] = (label, _output_values(port, displays.get(key, {})))
        self._reconcile("output", outputs)
        self._features = _licensed_features(info)
        temperature = info.get("temperature") or {}
        content = next((x for x in info.get("storage") or [] if x.get("type") == "content"), {})
        errors = info.get("errors") or []
        self.set_states({
            "model": info["model"], "device_name": info.get("name"), "serial_number": info.get("serial"),
            "firmware_version": (info.get("firmware") or {}).get("version"), "operating_mode": info.get("mode"),
            "last_boot_reason": info.get("reason"), "temperature": temperature.get("C"),
            "temperature_alarm": temperature.get("alarm"), "storage_unit": content.get("unit"),
            "content_free": float(content["free"]) if content.get("free") is not None else None,
            "content_error": errors[-1].get("desc") if errors else "",
        })

    def _clear_variables(self, status):
        self._reconcile("variable", {})
        self._names.clear()
        self._offer_variables(False)
        self.set_state("variable_access", status)

    async def _read_variables(self):
        if self._features is not None and "_SYSTEMS" not in self._features:
            self._clear_variables("not licensed")
            return
        # On older firmware the license flag may not exist. A successful
        # SYSTEMS-only listing is proof of access, even when the project is empty.
        names = await self._rpc("webstorage_list", [])
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("Invalid Web Storage variable list.")
        entries = {}
        for start in range(0, len(names), 64):
            keys = names[start:start + 64]
            rows = await self._rpc("webstorage_get", [keys])
            if not isinstance(rows, list):
                raise ValueError("Invalid Web Storage value reply.")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Invalid Web Storage value reply.")
                name = row.get("name")
                if name in keys and row.get("success") is True and isinstance(row.get("value"), str):
                    entries[_child_id(name)] = (name, {"name": name, "value": row["value"],
                                                       "timestamp": str(row.get("timestamp", ""))})
        self._reconcile("variable", entries)
        self._names = {key: entry[0] for key, entry in entries.items()}
        self._offer_variables(True)
        self.set_state("variable_access", "available")

    async def _refresh_locked(self):
        info = await self._info()
        self._apply_info(info)
        try:
            await self._read_variables()
        except (RPCRefusal, ValueError, ConnectionError) as exc:
            self._clear_variables("unavailable")
            self.set_state("last_error", str(exc))
        else:
            self.set_state("last_error", "")

    async def refresh_children(self):
        async with self._lock:
            await self._refresh_locked()
            return {"outputs": len(self.list_children("output")), "variables": len(self._names)}

    async def poll(self):
        await self.refresh_children()

    async def send_command(self, command, params=None):
        if not self.connected:
            raise RPCRefusal("The signage player is offline.")
        params = params or {}
        if command == "refresh":
            return await self.refresh_children()
        async with self._lock:
            if command == "set_variable":
                return await self._write_variable(params)
            if command == "restart":
                result = await self._rpc("restart", [])
                if not isinstance(result, dict) or result.get("success") is not True:
                    raise RPCRefusal("The player did not accept the restart.")
                return
            if command == "publish_project":
                url = params.get("url", "")
                target = urlsplit(url)
                if target.scheme not in {"http", "https"} or not target.hostname:
                    raise ValueError("Enter an HTTP or HTTPS project listing URL.")
                handle = await self._rpc("add_pull_action", [{"type": "publish", "uri": url}])
                if not isinstance(handle, str) or not handle:
                    raise RPCRefusal("The player did not return a content download handle.")
                return handle
            raise ValueError(f"Unknown command: {command}")

    async def _write_variable(self, params):
        await self._refresh_locked()
        key = params.get("variable")
        if key not in self._names:
            raise RPCRefusal("This variable is unavailable on the player.")
        value = params.get("value")
        if not isinstance(value, str):
            raise ValueError("Enter a text value for the project variable.")
        name = self._names[key]
        expected = self.get_child_state("variable", key)["value"]
        # Do not recreate a key that has just been deleted or overwrite a
        # concurrent update from another controller.
        result = await self._rpc("webstorage_cmpxchg", [[{"name": name, "expect": expected, "value": value}]])
        await self._refresh_locked()
        if not isinstance(result, list) or not any(
            row.get("name") == name and row.get("success") is True for row in result
        ):
            raise RPCRefusal("The variable changed or the player refused the write. Check its current value.")
        if key not in self._names or self.get_child_state("variable", key).get("value") != value:
            raise RPCRefusal("The player did not report the requested variable value.")
