"""BACnet/IP point integration using BACpypes3 (MIT), ANSI/ASHRAE 135 Annex J.

Python supplies the binary protocol stack and per-point metadata that YAML
cannot express. The platform owns UDP, adapter selection, traffic recording,
reconnection and health. BACpypes3 supplies transaction matching, segmentation
and standard datatypes. This client does not advertise a BACnet device number.

Feedback is hybrid: finite COV subscriptions, renewed before expiry, with
periodic reconciliation; unsupported subscriptions fall back to polling.
Writes are opt-in and read back. A command at a lower priority can succeed
without changing Present_Value; priority feedback distinguishes that case.

Sources: https://bacpypes3.readthedocs.io/en/latest/gettingstarted/running.html
https://bacnet.org/wp-content/uploads/sites/4/2022/08/Add-135-2008w.pdf
Coverage: direct IPv4 endpoints, analog/binary/multistate input/output/value.
The catalog probe supplies local broadcast discovery. No BACnet/SC, MS/TP
routing, foreign-device registration, schedules or proprietary objects.
"""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import math
import time
from dataclasses import dataclass

from bacpypes3.app import Application
from bacpypes3.appservice import ABORTED, ApplicationServiceAccessPoint
from bacpypes3.apdu import AbortPDU, ErrorRejectAbortNack, SimpleAckPDU, SubscribeCOVRequest
from bacpypes3.basetypes import ErrorType, Segmentation
from bacpypes3.comm import Server, bind
from bacpypes3.ipv4.bvll import BVLLCodec
from bacpypes3.ipv4.service import BIPNormal
from bacpypes3.netservice import NetworkServiceAccessPoint, NetworkServiceElement
from bacpypes3.vendor import get_vendor_info
from bacpypes3.pdu import IPv4Address, PDU
from bacpypes3.primitivedata import Null, ObjectIdentifier

from openavc.core.connection_fault import ConnectionFaultError
from openavc.drivers.base import BaseDriver, CommandRefusedError

KINDS = ("analog-input", "analog-output", "analog-value", "binary-input",
         "binary-output", "binary-value", "multi-state-input", "multi-state-output",
         "multi-state-value")
MAX_POINTS = 128
COV_LIFETIME = 300
COV_RENEW = 210
RECONCILE = 300
COV_RETRY = 600
TIMEOUT = 4.0
IMPORT_BATCH_SIZE = 32
IMPORT_TIME_BUDGET = 20.0
IMPORT_OBJECT_BUDGET = 2048
CHILD_STATUS = {
    "object_id": {"type": "string", "label": "BACnet object", "control": False},
    "description": {"type": "string", "label": "Description", "control": False},
    "feedback": {"type": "enum", "label": "Feedback", "values": ["poll", "cov"], "control": False},
    "in_alarm": {"type": "boolean", "label": "In alarm", "control": False},
    "fault": {"type": "boolean", "label": "Fault", "control": False},
    "overridden": {"type": "boolean", "label": "Overridden", "control": False},
    "out_of_service": {"type": "boolean", "label": "Out of service", "control": False},
    "reliability": {"type": "string", "label": "Reliability", "control": False},
    "commandable": {"type": "boolean", "label": "Priority control supported", "control": False},
    "active_priority": {"type": "integer", "label": "Active priority", "min": 0, "max": 16, "control": False,
                        "help": "0 means all priorities are released. Empty means unavailable."},
    "priority_active": {"type": "boolean", "label": "Configured priority has a value", "control": False,
                        "help": "Read from the controller. This slot may also be used by another client."},
    "priority_overridden": {"type": "boolean", "label": "Higher priority in control", "control": False},
    "last_read": {"type": "number", "label": "Last value received", "control": False, "cloud_priority": "low",
                  "help": "Unix timestamp of the last valid present value."},
    "error": {"type": "string", "label": "Point error", "control": False},
    "value_valid": {"type": "boolean", "label": "Value in service", "control": False,
                    "help": "False when the controller reports a fault or an out-of-service point."},
    "inactive_text": {"type": "string", "label": "Inactive label", "control": False},
    "active_text": {"type": "string", "label": "Active label", "control": False},
}


def point_key(obj):
    return f"{int(obj[0])}_{obj[1]}"


def parse_point(row):
    if not isinstance(row, dict):
        raise ValueError("Each Points row must identify one BACnet object.")
    obj = ObjectIdentifier(str(row.get("object", "")).strip())
    if str(obj[0]) not in KINDS:
        raise ValueError(f"Unsupported BACnet point: {obj}")
    priority = row_priority(row)
    writable = writable_point(row, obj)
    return {"obj": obj, "row": row, "priority": priority,
            "writable": writable, "cov_due": 0.0, "read_due": 0.0,
            "cov_active": False, "cov_until": 0.0, "commandable": False,
            "metadata": {}, "metadata_ready": False}


def row_priority(row):
    priority = int(row.get("priority", 16))
    if priority not in set(range(1, 17)) - {6}:
        raise ValueError("Use priority 1 to 16, except reserved priority 6.")
    mode = row.get("write_mode", "priority")
    if mode not in ("priority", "direct"):
        raise ValueError("Write method must be Priority or Direct.")
    return priority


def writable_point(row, obj):
    writable = row.get("writable", False) is True
    if writable and str(obj[0]).endswith("-input"):
        raise ValueError(f"{obj} is an input. Leave it read-only.")
    return writable


def point_rows(rows):
    """Reject ambiguous maps rather than silently addressing a different point."""
    if not isinstance(rows, list):
        raise ValueError("Points must be a table.")
    if len(rows) > MAX_POINTS:
        raise ValueError(f"Use at most {MAX_POINTS} points per controller.")
    result = {}
    for row in rows:
        point = parse_point(row)
        key = point_key(point["obj"])
        if key in result:
            raise ValueError(f"The point {point['obj']} appears more than once.")
        result[key] = point
    return result


def unicast_address(host):
    address = ipaddress.IPv4Address(host)
    if address.is_multicast or address.is_unspecified:
        raise ValueError("Enter one controller's unicast IPv4 address.")
    if int(address) == 0xFFFFFFFF:
        raise ValueError("Enter one controller's unicast IPv4 address.")
    return address


def local_timeout(result):
    if not isinstance(result, AbortPDU) or result.apduSrv:
        return False
    return int(result.apduAbortRejectReason) in (64, 65)


def is_error(value):
    return isinstance(value, (ErrorRejectAbortNack, ErrorType, CommandRefusedError))


def error_text(value):
    code = getattr(value, "errorCode", None) or getattr(value, "apduAbortRejectReason", None)
    return str(code if code is not None else value)


@dataclass
class PointImport:
    """One resumable search; committed cursor and point config advance together.

    The cursor lives with this driver instance, including ordinary reconnects.
    A new instance starts a fresh search and preserves already imported rows.
    """
    target: tuple
    name_filter: str
    index: int = 1
    total: int = -1
    unreadable: int = 0

    @property
    def more(self):
        return self.total < 0 or self.index <= self.total

    def can_resume(self, target):
        return self.more and self.target == target

    def progress(self, added):
        return f"Checked {self.index - 1} of {self.total} objects; {added} new matching points found."

    def summary(self, added, configured):
        parts = [f"Imported {added} new read-only points. {configured} points configured.",
                 f"Checked {self.index - 1} of {max(0, self.total)} objects.",
                 "Existing points, display names and write permissions were kept."]
        if self.unreadable:
            parts.append(f"Skipped {self.unreadable} objects that could not be read.")
        if self.more and configured >= MAX_POINTS:
            parts.append("The 128-point limit was reached before the scan finished. Remove unused points or narrow the search before importing again.")
        elif self.more:
            parts.append("Choose Continue Import to resume the same search.")
        else:
            parts.append("Search complete.")
        return " ".join(parts)


class DatagramLink(Server):
    """Carry the library's BVLC frames on OpenAVC's already-open transport."""
    def __init__(self, transport):
        super().__init__()
        self.transport = transport

    async def indication(self, pdu):
        await self.transport.send(bytes(pdu.pduData))


class BACnetClient(Application):
    """Client-only stack: no Device object, I-Am announcement or extra socket."""
    def __init__(self, transport, address, notify):
        super().__init__()
        self.address = address
        self.notify = notify
        self.asap = ApplicationServiceAccessPoint(device_info_cache=self.device_info_cache)
        self.asap.segmentationSupported = Segmentation.segmentedBoth
        self.asap.maxSegmentsAccepted = 64
        self.asap.apduTimeout = 1500
        self.asap.numberOfApduRetries = 1
        self.nsap = NetworkServiceAccessPoint()
        self.nse = NetworkServiceElement()
        bind(self.nse, self.nsap)
        bind(self, self.asap, self.nsap)
        link = BIPNormal()
        self.wire = DatagramLink(transport)
        bind(link, BVLLCodec(), self.wire)
        self.nsap.bind(link, address=IPv4Address("0.0.0.0:0"))

    async def receive(self, data):
        await self.wire.response(PDU(data, source=self.address,
                                     destination=IPv4Address("0.0.0.0:0")))

    async def do_ConfirmedCOVNotificationRequest(self, apdu):
        self.notify(apdu)
        await self.response(SimpleAckPDU(context=apdu))

    async def do_UnconfirmedCOVNotificationRequest(self, apdu):
        self.notify(apdu)

    def close(self):
        # The library's close() only closes its own sockets. Here the platform
        # closes UDP; cancel protocol timers and pending futures as well.
        for transaction in list(self.asap.clientTransactions) + list(self.asap.serverTransactions):
            transaction.set_state(ABORTED)
        for pending in list(self._requests.values()):
            for _, future in pending:
                future.cancel()
        for future in list(getattr(self, "_who_is_futures", [])):
            future.future.cancel()


class BACnetIPDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "bacnet_ip", "name": "BACnet/IP", "manufacturer": "Generic",
        "category": "utility", "version": "1.1.2", "author": "OpenAVC",
        "transport": "udp", "ports": [47808], "protocols": ["bacnet-ip"],
        "simulated": True, "verified": False, "min_platform_version": "0.41.0",
        "description": "Named building automation points with live feedback, opt-in writes and priority release.",
        "source_url": "https://bacpypes3.readthedocs.io/en/latest/gettingstarted/running.html",
        "tags": ["bacnet", "bms", "hvac", "occupancy", "building-automation"],
        "discovery": {"udp_probe": {
            "port": 47808, "send_hex": "81 0b 00 08 01 00 10 08", "cross_vendor": True,
            "expect_regex": r"(?s)^\x81\x0a..\x01[\x00\x04]\x10\x00\xc4\x02.{3}(?:\x21.|\x22..|\x24....)\x91[\x00-\x03](?:\x21.|\x22..)$",
            "timeout_ms": 1500,
        }},
        "help": {
            "overview": "Read analog, binary and multistate BACnet/IP points. Import names from the controller, then bind point values and status to controls, macros or triggers. Writes are disabled until enabled per point. Values always come from the controller, including when a higher priority overrides a write.",
            "setup": "Enter the controller's IPv4 address and UDP port (usually 47808). Use Import Points to add read-only points by name, or enter the facilities team's object list in Points. Enable writes only for approved outputs or values and agree a priority with facilities. Priority 16 is the lowest; 6 is reserved. Release Point releases only the configured priority. Writes remain on the controller after OpenAVC disconnects until released or changed there. The controller must allow replies and COV notifications to OpenAVC's outgoing UDP port. Direct IPv4 only: no MS/TP router addressing, BBMD registration or BACnet/SC. BACnet/IP provides no encrypted login; use the building's approved controls network.",
        },
        "default_config": {"host": "", "port": 47808, "poll_interval": 30, "use_cov": True, "points": []},
        "config_schema": {
            "host": {"type": "string", "label": "Controller IPv4 address", "required": True},
            "port": {"type": "integer", "label": "UDP port", "default": 47808, "min": 1, "max": 65535},
            "poll_interval": {"type": "integer", "label": "Poll interval (seconds)", "default": 30, "min": 10, "max": 120, "advanced": True,
                              "help": "Points without COV are read at this interval. COV points are reconciled every five minutes."},
            "use_cov": {"type": "boolean", "label": "Use change notifications", "default": True, "advanced": True,
                        "help": "Subscribe to changes where supported. A refused subscription falls back to polling and is tried again later."},
            "points": {"type": "table", "label": "Points", "default": [], "row_label": "point",
                       "help": "Import Points fills this table. Up to 128 points per controller. Labels are optional; the controller's names are used otherwise.",
                       "columns": {
                           "object": {"type": "string", "label": "BACnet object", "required": True,
                                      "help": "Object type and instance, e.g. analog-value,1 or binary-input,5."},
                           "label": {"type": "string", "label": "Display name"},
                           "writable": {"type": "boolean", "label": "Allow writes", "default": False},
                           "write_mode": {"type": "enum", "label": "Write method", "default": "priority",
                                          "values": [{"value": "priority", "label": "Priority"}, {"value": "direct", "label": "Direct"}],
                                          "help": "Use Priority for commandable points. Select Direct only for points facilities confirms do not use a priority array."},
                           "priority": {"type": "integer", "label": "Write priority", "default": 16, "min": 1, "max": 16,
                                        "help": "1 is highest, 16 lowest. Do not use reserved priority 6. Agree the priority with facilities."},
                       }},
        },
        "state_variables": {
            "device_instance": {"type": "integer", "label": "BACnet device instance"},
            "device_name": {"type": "string", "label": "Controller name"},
            "manufacturer": {"type": "string", "label": "Manufacturer"},
            "model": {"type": "string", "label": "Model"},
            "firmware": {"type": "string", "label": "Firmware"},
            "point_count": {"type": "integer", "label": "Configured points"},
            "cov_count": {"type": "integer", "label": "Points using change notifications"},
            "import_pending": {"type": "boolean", "label": "Import can continue"},
            "import_filter": {"type": "string", "label": "Import name filter"},
            "import_status": {"type": "string", "label": "Last import result"},
        },
        "child_entity_types": {
            "point": {"label": "Point", "label_plural": "Points", "id_format": {"type": "string"},
                      "dynamic": True, "label_field": "label", "summary_fields": ["value", "feedback", "error"],
                      "state_variables": {}},
        },
        "commands": {
            "refresh": {"label": "Refresh Points", "help": "Read actual values, status and command priorities from the controller now.", "params": {}},
            "set_point": {"label": "Set Point", "help": "Write an enabled point at its configured priority, then read its actual value. A higher priority may keep control.",
                          "params": {
                              "point": {"type": "child_id", "child_type": "point", "label": "Point", "required": True},
                              "property": {"type": "string", "label": "Property", "default": "value",
                                           "options_from": {"param": "point", "source": "child_schema"}},
                              "value": {"type": "string", "label": "Value", "required": True, "type_from": {"param": "property"}},
                          }},
            "release_point": {"label": "Release Point", "help": "Release this point's configured priority and read the result. Lower priorities or the controller's default take over. Other priorities are unchanged.",
                              "params": {"point": {"type": "child_id", "child_type": "point", "label": "Point", "required": True}}},
        },
        "quick_actions": ["set_point", "release_point", "refresh"],
        "actions": [
            {"id": "import_points", "kind": "setup", "label": "Import Points", "icon": "search", "availability": "online",
             "confirm": "Search this controller and add matching read-only points. Existing points, names and permissions are kept. A new search replaces any unfinished scan.",
             "params": {"name_filter": {"type": "string", "label": "Name contains",
                        "help": "Optional room name or prefix. Empty includes all supported points, up to 128 configured points. Batches are handled automatically."}}},
            {"id": "continue_import", "kind": "setup", "label": "Continue Import", "icon": "search", "availability": "online",
             "confirm": "Continue the previous search using its remembered name filter. Existing points, names and permissions are kept.",
             "visible_when": {"key": "device.$id.import_pending", "operator": "truthy"}, "params": {}},
        ],
    }
    HEALTH_TIMEOUT_S = 5.0
    HEALTH_FAULT_MESSAGE = "The BACnet controller stopped answering device queries."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._app = None
        self._device = None
        self._points = {}
        self._rpm = True
        self._read_lock = asyncio.Lock()
        self._point_lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._subscriptions = {}
        self._priority_pending = set()
        self._import_scan = None

    async def _pre_connect(self):
        try:
            address = unicast_address(str(self.config.get("host", "")))
            self._peer = IPv4Address((str(address), int(self.config.get("port", 47808))))
            self._points = point_rows(self.config.get("points", []))
        except (ValueError, TypeError) as exc:
            raise ConnectionFaultError(str(exc), code="invalid_config") from exc
        self._subscriptions = {}
        self._priority_pending.clear()
        self._rpm = True

    async def _post_connect(self):
        self._app = BACnetClient(self.transport, self._peer, self._notification)
        replies = await self._app.who_is(address=self._peer, timeout=3)
        if not replies:
            raise ConnectionFaultError("The controller did not answer BACnet Who-Is.", code="no_response")
        self._device = replies[0].iAmDeviceIdentifier
        self.set_state("device_instance", self._device[1])

    async def on_data_received(self, data):
        if self._app:
            try:
                await self._app.receive(data)
            except (ValueError, TypeError, RuntimeError, ErrorRejectAbortNack):
                # Malformed/unrelated frames must not tear down the session.
                return

    async def _initial_sync(self):
        identity = {"object-name": "device_name", "vendor-name": "manufacturer",
                    "model-name": "model", "firmware-revision": "firmware"}
        data = await self._properties(self._device, list(identity))
        for prop, key in identity.items():
            if not is_error(data.get(prop)) and data.get(prop) is not None:
                self.set_state(key, str(data[prop]))
        for key, point in self._points.items():
            self._register_point(key, point)
        self.set_state("point_count", len(self._points))
        self._restore_import_status()
        # Identity has proved the connection. A large point list must not
        # consume the platform's connection deadline; readings populate next.
        self.spawn_task(self.poll())

    async def refresh_children(self):
        for key, point in self._points.items():
            async with self._point_lock:
                await self._prepare_point(key, point)
                await self._refresh_point(key, point)

    async def _close_session(self):
        app, self._app = self._app, None
        if app:
            app.close()
        self._subscriptions.clear()
        self._priority_pending.clear()

    async def _liveness_probe(self):
        try:
            await self._read(self._device, "object-identifier")
        except CommandRefusedError:
            pass  # An error reply is still proof that this controller answers.

    async def _request(self, operation, *args, **kwargs):
        async with self._read_lock:
            try:
                result = await asyncio.wait_for(operation(*args, **kwargs), TIMEOUT)
            except ErrorRejectAbortNack as exc:
                result = exc
        if local_timeout(result):
            raise asyncio.TimeoutError("The BACnet controller did not answer.")
        if is_error(result):
            raise CommandRefusedError(f"BACnet refused the request: {error_text(result)}")
        return result

    async def _read(self, obj, prop, index=None):
        if not self._app:
            raise ConnectionError("The BACnet connection is closed.")
        return await self._request(self._app.read_property, self._peer, obj, prop, index)

    async def _properties(self, obj, props):
        """A device may implement ReadProperty but not ReadPropertyMultiple."""
        if self._rpm:
            bulk = await self._bulk_properties(obj, props)
            if bulk is not None:
                return bulk
        return await self._single_properties(obj, props)

    async def _bulk_properties(self, obj, props):
        try:
            result = await self._request(self._app.read_property_multiple, self._peer, [(obj, props)])
        except CommandRefusedError:
            self._rpm = False
            return None
        if isinstance(result, list):
            return {str(prop): value for _, prop, _, value in result}
        self._rpm = False
        return None

    async def _single_properties(self, obj, props):
        result = {}
        for prop in props:
            try:
                result[prop] = await self._read(obj, prop)
            except CommandRefusedError as exc:
                result[prop] = exc  # Keep a failed present value visible; metadata is optional.
        return result

    async def _prepare_point(self, key, point):
        props = ["object-name", "description", "units", "number-of-states", "state-text",
                 "inactive-text", "active-text", "min-pres-value", "max-pres-value", "resolution"]
        point["metadata"] = await self._properties(point["obj"], props)
        point["metadata_ready"] = True
        self._register_point(key, point)

    def _register_point(self, key, point):
        schema = copy.deepcopy(CHILD_STATUS)
        schema["value"] = value_schema(point)
        if self.is_child_registered("point", key):
            self.deregister_child("point", key)
        meta = point["metadata"]
        name = point["row"].get("label") or usable(meta.get("object-name")) or str(point["obj"])
        self.register_child("point", key, schema=schema, initial_state={
            "label": str(name), "object_id": str(point["obj"]),
            "description": str(usable(meta.get("description")) or ""),
            "feedback": "cov" if point["cov_active"] else "poll", "error": "",
            "inactive_text": str(usable(meta.get("inactive-text")) or ""),
            "active_text": str(usable(meta.get("active-text")) or ""),
        })

    async def poll(self):
        if self._poll_lock.locked():
            return  # Initial fill and the periodic poll share one cycle.
        async with self._poll_lock:
            now = time.monotonic()
            for key, point in self._points.items():
                async with self._point_lock:
                    await self._poll_point(key, point, now)
            await self._renew_subscriptions()
            self.set_state("cov_count", sum(p["cov_active"] for p in self._points.values()))

    async def _poll_point(self, key, point, now):
        if not point["metadata_ready"]:
            await self._prepare_point(key, point)
        self._expire_subscription(key, point, now)
        if now >= point["read_due"]:
            await self._refresh_point(key, point)

    def _expire_subscription(self, key, point, now):
        if point["cov_active"] and now >= point["cov_until"]:
            point["cov_active"] = False
            point["read_due"] = 0
            self.set_child_state("point", key, "feedback", "poll")

    async def _renew_subscriptions(self):
        if not self.config.get("use_cov", True):
            return
        # Renew existing subscriptions first. A controller that ignores COV
        # must not keep initial sync waiting four seconds per configured point.
        deadline = time.monotonic() + 6
        ordered = sorted(self._points.items(), key=lambda pair: (not pair[1]["cov_active"], pair[1]["cov_due"]))
        for key, point in ordered:
            now = time.monotonic()
            if now >= deadline:
                break
            if now >= point["cov_due"]:
                async with self._point_lock:
                    await self._subscribe(key, point)

    async def _refresh_point(self, key, point):
        props = ["present-value", "status-flags", "reliability"]
        if point["writable"]:
            props.append("priority-array")
        values = await self._properties(point["obj"], props)
        self._apply_values(key, point, values)
        point["read_due"] = time.monotonic() + (RECONCILE if point["cov_active"] else 0)

    def _apply_values(self, key, point, values):
        value = values.get("present-value")
        if value is not None:
            self._apply_value(key, point, value)
        flags = usable(values.get("status-flags"))
        if flags is not None:
            for index, name in enumerate(("in_alarm", "fault", "overridden", "out_of_service")):
                self.set_child_state("point", key, name, bool(flags[index]))
            online = self.get_child_state("point", key).get("online", False)
            self.set_child_state("point", key, "value_valid", bool(online and not (flags[1] or flags[3])))
        reliability = usable(values.get("reliability"))
        if reliability is not None:
            self.set_child_state("point", key, "reliability", str(reliability))
        if "priority-array" in values:
            self._apply_priority(key, point, usable(values["priority-array"]))

    def _apply_value(self, key, point, value):
        try:
            if is_error(value):
                raise ValueError(error_text(value))
            kind = str(point["obj"][0])
            parsed = normalize_value(kind, value)
        except (ValueError, TypeError) as exc:
            self.set_child_state("point", key, "online", False)
            self.set_child_state("point", key, "offline_reason", "service_fault")
            self.set_child_state("point", key, "offline_detail", f"Cannot read this point: {exc}")
            self.set_child_state("point", key, "error", str(exc))
            self.set_child_state("point", key, "value_valid", False)
            return
        self.set_child_state("point", key, "value", parsed)
        self.set_child_state("point", key, "online", True)
        self.set_child_state("point", key, "offline_reason", None)
        self.set_child_state("point", key, "offline_detail", None)
        self.set_child_state("point", key, "error", "")
        self.set_child_state("point", key, "last_read", time.time())

    def _apply_priority(self, key, point, array):
        point["commandable"] = array is not None and len(array) == 16
        self.set_child_state("point", key, "commandable", point["commandable"])
        if not point["commandable"]:
            for field in ("active_priority", "priority_active", "priority_overridden"):
                self.set_child_state("point", key, field, None)
            return
        active = [i + 1 for i, item in enumerate(array) if item.null is None]
        chosen = point["priority"]
        self.set_child_state("point", key, "active_priority", min(active, default=0))
        self.set_child_state("point", key, "priority_active", chosen in active)
        self.set_child_state("point", key, "priority_overridden", bool(active and min(active) < chosen))

    async def _subscribe(self, key, point):
        process = int(point_key(point["obj"]).split("_")[0]) * 4194304 + point["obj"][1]
        self._subscriptions[process] = key
        request = SubscribeCOVRequest(subscriberProcessIdentifier=process,
                                      monitoredObjectIdentifier=point["obj"],
                                      issueConfirmedNotifications=True, lifetime=COV_LIFETIME,
                                      destination=self._peer)
        try:
            await self._request(self._app.request, request)
        except (CommandRefusedError, asyncio.TimeoutError):
            point["cov_active"] = False
            point["cov_due"] = time.monotonic() + COV_RETRY
            point["read_due"] = 0
            self._subscriptions.pop(process, None)
        else:
            point["cov_active"] = True
            point["cov_due"] = time.monotonic() + COV_RENEW
            point["cov_until"] = time.monotonic() + COV_LIFETIME
        self.set_child_state("point", key, "feedback", "cov" if point["cov_active"] else "poll")

    def _notification(self, apdu):
        key = self._subscriptions.get(int(apdu.subscriberProcessIdentifier))
        point = self._points.get(key)
        if not point:
            return
        if apdu.monitoredObjectIdentifier != point["obj"]:
            return
        if apdu.initiatingDeviceIdentifier != self._device:
            return
        values = notification_values(point, apdu)
        self._apply_values(key, point, values)
        if point["writable"] and key not in self._priority_pending:
            self._priority_pending.add(key)
            self.spawn_task(self._refresh_priority(key, point))

    async def _refresh_priority(self, key, point):
        try:
            async with self._point_lock:
                values = await self._properties(point["obj"], ["priority-array"])
                self._apply_priority(key, point, usable(values.get("priority-array")))
        finally:
            self._priority_pending.discard(key)

    async def send_command(self, command, params=None):
        params = params or {}
        if command == "refresh":
            for point in self._points.values():
                point["read_due"] = 0
            await self.poll()
            return
        if command not in ("set_point", "release_point"):
            raise ValueError(f"Unknown command: {command}")
        key = str(params.get("point", ""))
        point = self._points.get(key)
        require_writable(point)
        async with self._point_lock:
            await self._write_point(key, point, command, params)

    async def _write_point(self, key, point, command, params):
        release = command == "release_point"
        if (params.get("property") or "value") != "value":
            raise ValueError("Only the point's present value can be written.")
        if not point["metadata_ready"]:
            await self._prepare_point(key, point)
        await self._refresh_point(key, point)
        priority = write_priority(point, release)
        value = Null(()) if release else write_value(point, params.get("value"))
        await self._request(self._app.write_property, self._peer, point["obj"], "present-value", value, priority=priority)
        await self._refresh_point(key, point)

    async def run_setup_action(self, action_id, params, progress):
        if action_id not in ("import_points", "continue_import"):
            raise ValueError(f"Unknown setup action: {action_id}")
        if not self.connected:
            return {"success": False, "message": "Connect to the controller before importing points."}
        if len(self.config.get("points", [])) >= MAX_POINTS:
            return {"success": False, "message": "The 128-point table is full. Remove unused points before importing more."}
        scan = self._begin_import(action_id, params)
        rows, failure = await self._scan_import(scan, progress)
        added = await self._save_import(scan, rows)
        message = scan.summary(added, len(self.config.get("points", [])))
        if failure:
            message = f"{failure} {message}"
        self.set_state("import_status", message)
        if added:
            await self.request_reconnect()
        return {"success": not failure, "message": message}

    def _import_target(self):
        return (str(self.config.get("host", "")), int(self.config.get("port", 47808)), str(self._device))

    def _restore_import_status(self):
        scan = self._import_scan
        if scan and scan.target != self._import_target():
            self._import_scan = None
            self.set_state("import_status", "The controller changed. Start a new import.")
        self._publish_import_cursor()

    def _publish_import_cursor(self):
        scan = self._import_scan
        available = bool(scan and scan.more and len(self.config.get("points", [])) < MAX_POINTS)
        self.set_state("import_pending", available)
        self.set_state("import_filter", scan.name_filter if scan else "")

    def _begin_import(self, action_id, params):
        if action_id == "import_points":
            self._import_scan = PointImport(self._import_target(), str(params.get("name_filter", "")).strip())
        scan = self._import_scan
        if not scan or not scan.can_resume(self._import_target()):
            raise ValueError("No unfinished search is available for this controller. Choose Import Points to start a new search.")
        self._publish_import_cursor()
        # Do not advance the remembered cursor until new rows are saved. If
        # saving fails, Continue retries this slice instead of losing points.
        return copy.deepcopy(scan)

    async def _scan_import(self, scan, progress):
        rows = []
        known = set(point_rows(self.config.get("points", [])))
        try:
            await self._read_import_count(scan, progress)
            await self._read_import_batches(scan, known, rows, progress)
        except asyncio.TimeoutError:
            return rows, "The controller stopped answering during import."
        except (ConnectionError, CommandRefusedError) as exc:
            return rows, f"The scan stopped: {exc}."
        return rows, ""

    async def _read_import_count(self, scan, progress):
        count = int(await self._read(self._device, "object-list", 0))
        if count < 0:
            raise ValueError("The controller returned an invalid object count.")
        if scan.total >= 0 and scan.total != count:
            scan.index, scan.unreadable = 1, 0
            await progress("The controller's object list changed. Restarting the search; existing points are kept.")
        scan.total = count
        scope = f'Names containing "{scan.name_filter}"' if scan.name_filter else "All supported points"
        await progress(f"{scope}. {count} objects on this controller.", 0)

    async def _read_import_batches(self, scan, known, rows, progress):
        deadline = time.monotonic() + IMPORT_TIME_BUDGET
        end = min(scan.total + 1, scan.index + IMPORT_OBJECT_BUDGET)
        last_report = time.monotonic()
        while scan.index < end and len(known) < MAX_POINTS:
            if time.monotonic() >= deadline:
                break
            async with self._point_lock:
                await self._read_import_point(scan, known, rows)
            scan.index += 1
            if (scan.index - 1) % IMPORT_BATCH_SIZE == 0 or time.monotonic() - last_report >= 1:
                await progress(scan.progress(len(rows)), min(99, (scan.index - 1) * 100 // max(1, scan.total)))
                last_report = time.monotonic()
        await progress(scan.progress(len(rows)), min(99, (scan.index - 1) * 100 // max(1, scan.total)))

    async def _read_import_point(self, scan, known, rows):
        try:
            obj = await self._read(self._device, "object-list", scan.index)
            if not importable_point(obj, known):
                return
            name = str(await self._read(obj, "object-name"))
        except CommandRefusedError:
            scan.unreadable += 1
            return
        if scan.name_filter.casefold() not in name.casefold():
            return
        rows.append({"object": str(obj), "label": name, "writable": False,
                     "write_mode": "priority", "priority": 16})
        known.add(point_key(obj))

    async def _save_import(self, scan, found):
        if scan.target != self._import_target():
            raise ValueError("The controller changed during import. Start a new import for the selected controller.")
        # Merge against the current config, preserving edits made while this
        # scan was awaiting replies, rather than replacing an earlier copy.
        rows = copy.deepcopy(self.config.get("points", []))
        known = set(point_rows(rows))
        additions = [row for row in found if point_key(parse_point(row)["obj"]) not in known]
        if len(rows) + len(additions) > MAX_POINTS:
            raise ValueError("The Points table changed during import and is now full. Review it before continuing.")
        if additions:
            await self.request_config_update({"points": rows + additions})
        self._import_scan = scan
        self._publish_import_cursor()
        return len(additions)


def usable(value):
    return None if is_error(value) else value


def normalize_value(kind, value):
    if kind.startswith("binary-"):
        raw = int(value)
        if raw not in (0, 1):
            raise ValueError("Binary value must be inactive or active.")
        return bool(raw)
    if kind.startswith("multi-state-"):
        return str(int(value))
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("The controller returned a non-finite number.")
    return number


def enum_schema(meta):
    count = usable(meta.get("number-of-states"))
    if not count or not 0 < int(count) <= 4096:
        return {"type": "string", "help": "State number; the controller did not report its state names."}
    names = usable(meta.get("state-text")) or []
    values = []
    for i in range(1, int(count) + 1):
        label = str(names[i - 1]) if i <= len(names) else str(i)
        values.append({"value": str(i), "label": label})
    return {"type": "enum", "values": values}


def number_schema(meta):
    result = {"type": "number"}
    unit = usable(meta.get("units"))
    units = {"degrees-celsius": "°C", "degrees-fahrenheit": "°F", "percent": "%",
             "parts-per-million": "ppm", "no-units": "", "relative-humidity": "% RH"}
    if unit is not None:
        result["unit"] = units.get(str(unit), str(unit).replace("-", " "))
    for prop, field in (("min-pres-value", "min"), ("max-pres-value", "max"), ("resolution", "step")):
        val = usable(meta.get(prop))
        if val is None:
            continue
        if math.isfinite(float(val)):
            result[field] = float(val)
    return result


def value_schema(point):
    kind = str(point["obj"][0]).split("-")[0]
    builders = {"binary": lambda meta: {"type": "boolean"}, "multi": enum_schema, "analog": number_schema}
    priority = "low" if kind == "analog" and not point["writable"] else "high"
    return {"label": "Value", "control": point["writable"], "cloud_priority": priority,
            **builders[kind](point["metadata"])}


def binary_write(point, value):
    values = {"true": 1, "false": 0, "1": 1, "0": 0}
    text = str(value).strip().casefold()
    if text not in values:
        raise ValueError("Choose Yes or No for a binary point.")
    return values[text]


def enum_write(point, value):
    number = int(str(value))
    count = usable(point["metadata"].get("number-of-states"))
    maximum = int(count) if count is not None else 4294967295
    if not 1 <= number <= maximum:
        raise ValueError("Choose one of this point's reported states.")
    return number


def number_write(point, value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Enter a finite number.")
    spec = value_schema(point)
    if not spec.get("min", -math.inf) <= number <= spec.get("max", math.inf):
        raise ValueError("The value is outside this point's reported range.")
    return number


def write_value(point, value):
    converters = {"binary": binary_write, "multi": enum_write, "analog": number_write}
    return converters[str(point["obj"][0]).split("-")[0]](point, value)


def notification_values(point, apdu):
    values = {}
    for element in apdu.listOfValues:
        prop = str(element.propertyIdentifier)
        if prop not in ("present-value", "status-flags"):
            continue
        try:
            datatype = get_vendor_info(0).get_object_class(point["obj"][0]).get_property_type(element.propertyIdentifier)
            values[prop] = element.propertyValue.cast_out(datatype)
        except (TypeError, ValueError):
            continue
    return values


def require_writable(point):
    if not point or not point["writable"]:
        raise CommandRefusedError("Enable writes for this point in the Points table first.")


def write_priority(point, release):
    direct = point["row"].get("write_mode", "priority") == "direct"
    if not direct and not point["commandable"]:
        raise CommandRefusedError("This point has no readable priority array. If facilities confirms it uses direct writes, select Direct in its Points row.")
    if release and direct:
        raise CommandRefusedError("Direct-write points have no priority to release.")
    return None if direct else point["priority"]


def importable_point(obj, known):
    return str(obj[0]) in KINDS and point_key(obj) not in known
