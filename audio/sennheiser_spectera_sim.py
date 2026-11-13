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
        self.resources = {
            "/api/ssc/usagemark": {"usedBy": "", "timer": 5},
            "/api/audio/pendingactions": {"numPendingActions": 0},
            "/api/device/identity": {
                "product": "Spectera-Base-Station",
                "vendor": "Sennheiser electronic SE & Co. KG",
                "serial": "1534000001",
                "variant": {
                    "articleName": config.get("model", "Spectera Base Station")
                },
            },
            "/api/ssc/version": {
                "protocol": "2.3",
                "schema": config.get("api_version", "18.3"),
            },
            "/api/device/state": {"state": "Normal", "warnings": []},
            IDENTIFICATION: {"visual": False},
            "/api/device/site": {"deviceName": "Stage Left"},
            "/api/firmware/update/state": {
                "deviceVersion": config.get("firmware", "1.5.0")
            },
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

    def _authorized(self, headers):
        token = next(
            (value for key, value in headers.items() if key.lower() == "authorization"),
            "",
        )
        if "wrong_password" in self.active_errors or not token.startswith("Basic "):
            return False
        try:
            user, _, password = (
                base64.b64decode(token[6:], validate=True).decode().partition(":")
            )
        except (UnicodeError, ValueError):
            return False
        return (
            user == "controlSennheiser"
            and bool(password)
            and password != "invalid"
            and (not self._password or self._password == password)
        )

    @staticmethod
    def _frame(event, data):
        return (
            "event: "
            + event
            + "\ndata: "
            + json.dumps(data, separators=(",", ":"))
            + "\n\n"
        )

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
        for path in body:
            if path not in self.collections and path not in self.resources:
                return 400, {"path": path, "error": 404}
        session["paths"] = set(body)
        for path in body:
            if path == PAIRED:
                for uid, row in self.collections[path].items():
                    session["queue"].put_nowait(
                        self._frame("message", {f"{path}/{uid}": row})
                    )
            else:
                data = (
                    list(self.collections[path].values())
                    if path in self.collections
                    else self.resources[path]
                )
                session["queue"].put_nowait(self._frame("message", {path: data}))
        return 200, ""

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
                self._frame(
                    "open", {"sessionUUID": ident, "path": f"{path}/{ident}"}
                ).encode()
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
            if value:
                self.collections[PAIRED][uid] = self._sek
            else:
                self.collections[PAIRED].pop(uid, None)
        elif key == "sek_connected":
            self._sek["state"] = "Connected" if value else "Disconnected"
        elif key == "sek_button":
            if self._sek.get("commandBehavior") == "Latching":
                self._sek["commandState"] = "Latched" if value else "Unlatched"
            else:
                self._sek["commandState"] = "Pressed" if value else "Released"
        elif key == "sek_battery":
            self._sek.update(batteryFillLevel=value, batteryLow=0 <= value <= 10)
        else:
            wire = {
                "sek_minutes": "batteryRuntime",
                "sek_mic_quality": "micLqi",
                "sek_iem_quality": "iemLqi",
            }.get(key)
            if wire:
                self._sek[wire] = value
        self._notify(f"{PAIRED}/{uid}", self.collections[PAIRED].get(uid))

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
            mark = self.resources[path]
            if self._mark_expires and time.monotonic() >= self._mark_expires:
                mark["usedBy"] = ""
                self._mark_expires = 0
            if method == "PUT":
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("usedBy"), str)
                    or not 5 <= payload.get("timer", 0) <= 3600
                ):
                    return 400, {}
                mark.update(payload)
                self._mark_expires = time.monotonic() + payload["timer"]
                return 200, ""
        if path == "/api/audio/pendingactions" and self._pending:
            pending_path, row, patch = self._pending
            self._pending = None
            row.update(patch)
            self._notify(pending_path, row)
            return 200, {"numPendingActions": 1}
        if method == "GET":
            if path in self.collections:
                return 200, json.dumps(list(self.collections[path].values()))
            if path in self.resources:
                return 200, self.resources[path]
        if method == "PUT" and "refuse_write" in self.active_errors:
            return 409, {}
        if (
            path == IDENTIFICATION
            and method == "PUT"
            and isinstance(payload, dict)
            and type(payload.get("visual")) is bool
        ):
            self.resources[path].update(payload)
            self._notify(path, self.resources[path])
            return 200, self.resources[path]
        collection, _, suffix = path.rpartition("/")
        if collection not in self.collections:
            return 404, {}
        key = (
            suffix
            if collection == PORTS
            else int(suffix)
            if suffix.isdecimal()
            else None
        )
        row = self.collections[collection].get(key)
        if row is None:
            return 404, {}
        if method == "GET":
            return 200, row
        if method != "PUT" or not isinstance(payload, dict):
            return 405, {}
        id_field = {
            PAIRED: "mtUid",
            OUTPUTS: "outputId",
            CARRIERS: "rfChannelId",
            PORTS: "antennaPortId",
        }[collection]
        if payload.get(id_field) != key:
            return 400, {}
        if collection == PAIRED:
            if payload.get("type") != row["type"]:
                return 400, {}
            if row["state"] != "Connected":
                return 409, {}
        if not set(payload) <= set(row):
            return 400, {}
        if collection == OUTPUTS:
            self._pending = (path, row, payload)
            return 200, ""
        row.update(payload)
        if collection == CARRIERS:
            for device in self.collections[PAIRED].values():
                if device.get("rfChannelId") == key:
                    if payload.get("rfState") == "RfMuted":
                        device["state"] = "Disconnected"
                    elif device is self._sek and self.get_state("sek_connected"):
                        device["state"] = "Connected"
                    self._notify(f"{PAIRED}/{device['mtUid']}", device)
        self._notify(path, row)
        return 200, ""
