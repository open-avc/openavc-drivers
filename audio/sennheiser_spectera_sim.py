"""Spectera SSCv2 HTTPS simulator. The station owns the paired list and individual subscription sessions."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid

from aiohttp import web
from openavc.simulator.http_simulator import HTTPSimulator

PAIRED = "/api/mts/paired/all"
OUTPUTS = "/api/audio/outputs"
CARRIERS = "/api/rf/channels"
PORTS = "/api/rf/antennas"
SESSIONS = "/api/ssc/state/subscriptions"
IDENTIFICATION = "/api/device/identification"
PUBLIC = {"/api/device/identity", "/api/ssc/version", IDENTIFICATION}


def mobile(uid, model, name, link):
    row = dict(
        mtUid=uid,
        type=model,
        name=name,
        serial=str(1534000000 + link),
        version="v1.5.0",
        state="Connected",
        rfChannelId=0,
        batteryFillLevel=82,
        batteryRuntime=225,
        batteryLow=False,
        micAudiolinkId=link,
        micAudiolinkActive=True,
        micLqi=4,
        dominantAntenna="a",
        interference={"severity": "None"},
        rssi=-57.5,
        identify=False,
        reverseIdentify=False,
        micPreampGain=12,
        commandBehavior="Momentary",
        commandState="Released",
    )
    if model == "SEK":
        row.update(
            iemAudiolinkId=31,
            iemAudiolinkActive=True,
            iemLqi=4,
            headphoneVolume=-20,
            headphoneVolumeMin=-100,
            headphoneVolumeMax=0,
        )
    return row


def output(index):
    row = {"outputId": index, "micAudiolinkId": index + 8 if index < 2 else -1}
    for key, enabled in [("aoIp", "Mute"), ("madi1", "Talk"), ("madi2", "On")]:
        row[key + "EnableIfCommandIsEnabled"] = enabled
        row[key + "EnableIfCommandIsDisabled"] = "On"
    return row


def valid_mark(payload):
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("usedBy"), str):
        return False
    return type(payload.get("timer")) is int and 5 <= payload["timer"] <= 3600


def write_status(collection, key, row, payload):
    id_field = {
        PAIRED: "mtUid",
        OUTPUTS: "outputId",
        CARRIERS: "rfChannelId",
        PORTS: "antennaPortId",
    }[collection]
    if payload.get(id_field) != key or not set(payload) <= set(row):
        return 400
    if collection != PAIRED:
        return 200
    if payload.get("type") != row["type"]:
        return 400
    return 200 if row["state"] == "Connected" else 409


def station_resources(config):
    return {
        "/api/ssc/usagemark": {"usedBy": "", "timer": 5},
        "/api/audio/pendingactions": {"numPendingActions": 0},
        "/api/device/identity": {
            "product": "Spectera-Base-Station",
            "vendor": "Sennheiser electronic SE & Co. KG",
            "serial": "1534000001",
            "variant": {"articleName": config.get("model", "Spectera Base Station")},
        },
        "/api/ssc/version": {
            "protocol": "2.3",
            "schema": config.get("api_version", "18.3"),
        },
        "/api/device/state": {"state": "Normal", "warnings": []},
        IDENTIFICATION: {"visual": False},
        "/api/device/site": {"deviceName": "Stage Left"},
        "/api/firmware/update/state": {"deviceVersion": config.get("firmware", "1.5.0")},
        "/api/health/psu": {"psu1": "connected", "psu2": "unconnected"},
        "/api/health/tempstateoverall": {"value": "normal"},
        "/api/audio/interface/aoip/status": {
            "aoIpType": "Dante",
            "status": "Locked",
            "mute": False,
        },
        **{
            f"/api/audio/interface/madi{i}/status": {
                "inputStatus": {"mute": False},
                "outputStatus": {"mute": False},
            }
            for i in (1, 2)
        },
    }


class SennheiserSpecteraSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "sennheiser_spectera",
        "name": "Sennheiser Spectera Simulator",
        "category": "audio",
        "transport": "http",
        "default_port": 443,
        "tls": True,
        "initial_state": {
            "sek_paired": True,
            "sek_connected": True,
            "sek_battery": 82,
            "sek_minutes": 225,
            "sek_mic_quality": 4,
            "sek_iem_quality": 4,
            "sek_button": False,
            "aoip_muted": False,
        },
        "controls": [
            {"type": "toggle", "key": "sek_paired", "label": "SEK Paired"},
            {"type": "toggle", "key": "sek_connected", "label": "SEK Connected"},
            {
                "type": "slider",
                "key": "sek_battery",
                "label": "SEK Battery (%)",
                "min": -1,
                "max": 100,
            },
            {
                "type": "slider",
                "key": "sek_minutes",
                "label": "SEK Battery (min)",
                "min": -1,
                "max": 600,
            },
            {
                "type": "slider",
                "key": "sek_mic_quality",
                "label": "SEK Microphone Quality",
                "min": 0,
                "max": 4,
            },
            {
                "type": "slider",
                "key": "sek_iem_quality",
                "label": "SEK IEM Quality",
                "min": 0,
                "max": 4,
            },
            {
                "type": "toggle",
                "key": "sek_button",
                "label": "SEK Command Button Active",
            },
            {"type": "toggle", "key": "aoip_muted", "label": "AoIP Interface Muted"},
        ],
        "error_modes": {
            "communication_timeout": {
                "description": "Station stops answering",
                "behavior": "no_response",
            },
            "wrong_password": {
                "description": "Device credentials refused",
                "behavior": "custom",
            },
            "refuse_write": {
                "description": "Station refuses a control change (409)",
                "behavior": "custom",
            },
            "stream_closed": {
                "description": "Interrupt live notifications; HTTPS reads continue",
                "behavior": "custom",
            },
        },
    }
    sse_paths = [SESSIONS]

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        config = config or {}
        self._password = config.get("password")
        self._mark_expires = 0.0
        self._pending = None
        self.sessions = {}
        self._sek = mobile(196620, "SEK", "Presenter", 8)
        self.collections = {
            PAIRED: {196620: self._sek, 196621: mobile(196621, "SKM", "Handheld", 9)},
            OUTPUTS: {i: output(i) for i in range(32)},
            CARRIERS: {
                i: {
                    "rfChannelId": i,
                    "rfState": "RfActive",
                    "rfRestrictionViolation": False,
                    "frequency": 474000 + i * 8000,
                    "txPower": 50,
                }
                for i in range(2)
            },
            PORTS: {
                port: {
                    "antennaPortId": port,
                    "state": "Initialized" if port == "a" else "Unconnected",
                    "type": "UHF" if port == "a" else "NA",
                    "temperature": 30,
                    "identify": False,
                    "warningHighTemperature": False,
                    "warningPacketError": False,
                    "errorStateDetails": "NA",
                    "serial": "1534000020",
                    "version": "v1.5.0",
                }
                for port in "abcd"
            },
        }
        self.resources = station_resources(config)

    def _authorized(self, headers):
        token = next(
            (value for key, value in headers.items() if key.lower() == "authorization"),
            "",
        )
        if "wrong_password" in self.active_errors or not token.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(token[6:], validate=True).decode().partition(":")
        except (UnicodeError, ValueError):
            return False
        if user != "controlSennheiser" or password in ("", "invalid"):
            return False
        return self._password in (None, "", password)

    @staticmethod
    def _frame(event, data):
        return "event: " + event + "\ndata: " + json.dumps(data, separators=(",", ":")) + "\n\n"

    def _notify(self, path, data):
        parent = path.rpartition("/")[0]
        for session in self.sessions.values():
            if path in session["paths"] or parent in session["paths"]:
                session["queue"].put_nowait(self._frame("message", {path: data}))

    def _session_request(self, method, path, body):
        ident = path.removeprefix(SESSIONS + "/")
        session = self.sessions.get(ident)
        if session is None:
            return 422, {}
        if method == "DELETE":
            session["queue"].put_nowait(self._frame("close", {"sessionUUID": ident}))
            session["queue"].put_nowait(None)
            return 200, ""
        if method != "PUT" or not isinstance(body, list):
            return 400, {}
        known = self.collections.keys() | self.resources.keys()
        unsupported = next((p for p in body if not isinstance(p, str) or p not in known), None)
        if unsupported is not None:
            return 400, {"path": unsupported, "error": 404}
        session["paths"] = set(body)
        for path in body:
            self._initial_notifications(session["queue"], path)
        return 200, ""

    def _initial_notifications(self, queue, path):
        if path == PAIRED:
            for uid, row in self.collections[path].items():
                queue.put_nowait(self._frame("message", {f"{path}/{uid}": row}))
            return
        value = (
            list(self.collections[path].values())
            if path in self.collections
            else self.resources[path]
        )
        queue.put_nowait(self._frame("message", {path: value}))

    async def _serve_sse(self, request, path):
        if not self._authorized(request.headers):
            return web.Response(status=401)
        if "stream_closed" in self.active_errors:
            return web.Response(status=503)
        ident = str(uuid.uuid4())
        queue = asyncio.Queue()
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Content-Location": ident}
        )
        await response.prepare(request)
        self.sessions[ident] = {"queue": queue, "paths": set()}
        self._sse_clients.add(queue)
        try:
            await response.write(
                self._frame("open", {"sessionUUID": ident, "path": f"{path}/{ident}"}).encode()
            )
            while self.running:
                msg = await queue.get()
                if msg is None:
                    break
                await response.write(msg.encode())
        except ConnectionError:
            pass
        finally:
            self.sessions.pop(ident, None)
            self._sse_clients.discard(queue)
        return response

    def set_state(self, key, value):
        super().set_state(key, value)
        if key == "aoip_muted":
            path = "/api/audio/interface/aoip/status"
            self.resources[path]["mute"] = value
            self._notify(path, self.resources[path])
            return
        if not key.startswith("sek_"):
            return
        uid = self._sek["mtUid"]
        if key == "sek_paired":
            self._set_paired(uid, value)
        else:
            self._sek.update(self._sek_change(key, value))
        self._notify(f"{PAIRED}/{uid}", self.collections[PAIRED].get(uid))

    def _set_paired(self, uid, paired):
        if paired:
            self.collections[PAIRED][uid] = self._sek
        else:
            self.collections[PAIRED].pop(uid, None)

    def _sek_change(self, key, value):
        if key == "sek_connected":
            return {"state": "Connected" if value else "Disconnected"}
        if key == "sek_battery":
            return {"batteryFillLevel": value, "batteryLow": 0 <= value <= 10}
        if key == "sek_button":
            states = (
                ("Unlatched", "Latched")
                if self._sek.get("commandBehavior") == "Latching"
                else ("Released", "Pressed")
            )
            return {"commandState": states[bool(value)]}
        wire = {
            "sek_minutes": "batteryRuntime",
            "sek_mic_quality": "micLqi",
            "sek_iem_quality": "iemLqi",
        }.get(key)
        return {wire: value} if wire else {}

    def inject_error(self, mode):
        super().inject_error(mode)
        if mode in ("stream_closed", "wrong_password"):
            self.close_sse_sessions("close")

    def handle_request(self, method, path, headers, body):
        if path not in PUBLIC and not self._authorized(headers):
            return 401, {}
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            return 400, {}
        if path.startswith(SESSIONS + "/"):
            return self._session_request(method, path, payload)
        if path == "/api/ssc/usagemark":
            return self._mark(method, payload)
        if path == "/api/audio/pendingactions" and self._pending:
            return self._apply_pending()
        if method == "GET":
            return self._read(path)
        if method != "PUT":
            return 405, {}
        if "refuse_write" in self.active_errors:
            return 409, {}
        return self._write(path, payload)

    def _mark(self, method, payload):
        mark = self.resources["/api/ssc/usagemark"]
        if self._mark_expires and time.monotonic() >= self._mark_expires:
            mark["usedBy"] = ""
            self._mark_expires = 0
        if method == "GET":
            return 200, mark
        if method != "PUT" or not valid_mark(payload):
            return 400, {}
        mark.update(payload)
        self._mark_expires = time.monotonic() + payload["timer"]
        return 200, ""

    def _apply_pending(self):
        path, row, patch = self._pending
        self._pending = None
        row.update(patch)
        self._notify(path, row)
        return 200, {"numPendingActions": 1}

    def _resource_row(self, path):
        collection, _, suffix = path.rpartition("/")
        rows = self.collections.get(collection, {})
        key = suffix if collection == PORTS else int(suffix) if suffix.isdecimal() else None
        return collection, key, rows.get(key)

    def _read(self, path):
        if path in self.collections:
            return 200, json.dumps(list(self.collections[path].values()))
        if path in self.resources:
            return 200, self.resources[path]
        _, _, row = self._resource_row(path)
        return (200, row) if row is not None else (404, {})

    def _write(self, path, payload):
        if not isinstance(payload, dict):
            return 400, {}
        if path == IDENTIFICATION:
            if type(payload.get("visual")) is not bool:
                return 400, {}
            self.resources[path].update(payload)
            self._notify(path, self.resources[path])
            return 200, self.resources[path]
        coll, key, row = self._resource_row(path)
        if row is None:
            return 404, {}
        status = write_status(coll, key, row, payload)
        if status != 200:
            return status, {}
        if coll == OUTPUTS:
            self._pending = (path, row, payload)
            return 200, ""
        row.update(payload)
        if coll == CARRIERS:
            self._carrier_change(key, payload)
        self._notify(path, row)
        return 200, ""

    def _carrier_change(self, key, payload):
        attached = [r for r in self.collections[PAIRED].values() if r.get("rfChannelId") == key]
        for row in attached:
            if payload.get("rfState") == "RfMuted":
                row["state"] = "Disconnected"
            elif row is not self._sek or self.get_state("sek_connected"):
                row["state"] = "Connected"
            self._notify(f"{PAIRED}/{row['mtUid']}", row)
