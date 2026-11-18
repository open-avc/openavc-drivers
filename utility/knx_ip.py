"""KNX/IP: named building controls from ETS, with received-state feedback.

Protocol: KNX Specifications V3, 03/08 Core, Tunnelling and IP Secure;
03/03/07 Application Layer; 03/07/02 Datapoint Types. Project import follows
the KNX Association's Project Schema 23 and Group Address Export documents.
XKNX 4.1.1 (MIT) supplies protocol/security, not inferred physical state.
Python is needed for secure binary sessions and an installation-specific roster.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import time
import uuid
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from pathlib import PurePosixPath

import pyzipper
from defusedxml import ElementTree as SafeXML
from xknx import XKNX
from xknx.cemi.cemi_handler import CEMIHandler
from xknx.dpt import DPTBase, DPTArray, DPTBinary
from xknx.exceptions import CommunicationError, ConversionError, CouldNotParseTelegram, InvalidSecureConfiguration, IPSecureError
from xknx.io import ConnectionConfig, ConnectionType, SecureConfig
from xknx.io.gateway_scanner import GatewayScanner
from xknx.io.ip_secure import SecureSession
from xknx.io.knxip_interface import KNXIPInterface
from xknx.io.self_description import request_description
from xknx.io.transport import TCPTransport, UDPTransport
from xknx.io.tunnel import SecureTunnel, TCPTunnel, UDPTunnel
from xknx.secure.keyring import sync_load_keyring
from xknx.telegram import GroupAddress, Telegram, TelegramDirection
from xknx.telegram.apci import GroupValueRead, GroupValueResponse, GroupValueWrite

from openavc.core.connection_fault import ConnectionFaultError
from openavc.drivers.base import BaseDriver, CommandRefusedError
from openavc.system_config import get_data_dir

MAX_XML_BYTES = 32 * 1024 * 1024
MAX_IMPORT_BYTES = 128 * 1024 * 1024
MAX_GROUPS = 10000
MAX_CONTROLS = 512
PROJECT_XML = re.compile(r"P-[0-9A-Fa-f]+/\d+\.xml")
PROJECT_ZIP = re.compile(r"P-[0-9A-Fa-f]+\.zip")

# Roles are identified from the project's master data by their defined names.
# No translated label or address name is used to infer write/status direction.
LIGHT_ROLES = {
    "SwitchOnOff": ("on", "write", "1.001"),
    "InfoOnOff": ("on", "read", "1.001"),
    "DimmingValue": ("level", "write", "5.001"),
    "InfoDimmingValue": ("level", "read", "5.001"),
    "DimmingControl": ("dim", "write", "3.007"),
}
SHADE_ROLES = {
    "MoveUpDown": ("move", "write", "1.008"),
    "StopStepUpDown": ("step_stop", "write", "1.007"),
    "CurrentAbsolutePositionBlindsPercentage": ("position", "read", "5.001"),
    "CurrentAbsolutePositionSlatPercentage": ("tilt", "read", "5.001"),
}
SENSOR_ROLES = {
    "WindAlarm": "1.005", "RainAlarm": "1.005", "TempRoom": "9.001",
    "WindowStatus": "1.019",
}


def local_tag(element):
    return element.tag.rsplit("}", 1)[-1]


def address_text(value):
    address = GroupAddress(str(value))
    if address.raw == 0:
        raise ValueError("Group address 0 is a broadcast address, not a control.")
    return f"{address.raw >> 11}/{(address.raw >> 8) & 7}/{address.raw & 255}"


def dpt_text(value):
    """Normalize an explicit subtype; never guess a missing subtype."""
    value = str(value).strip()
    match = re.fullmatch(r"(?:DPST-)?(\d+)[.-](\d+)", value)
    if not match:
        return ""
    return f"{int(match[1])}.{int(match[2]):03d}"


def stable_id(*parts):
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()[:20]


class ProjectArchive:
    """Read selected XML entries in memory, with cumulative size limits."""

    def __init__(self, data, password=""):
        self.archive = pyzipper.AESZipFile(io.BytesIO(data))
        self.read_bytes = 0
        self.password = password
        if len(self.archive.infolist()) > 20000:
            raise ValueError("The project archive contains too many files.")

    def read(self, name, limit=MAX_XML_BYTES):
        info = self.archive.getinfo(name)
        if info.file_size > limit or self.read_bytes + info.file_size > MAX_IMPORT_BYTES:
            raise ValueError("The project's XML exceeds the import size limit.")
        with self.archive.open(info) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("The project's XML exceeds the import size limit.")
        self.read_bytes += len(data)
        return data

    def project(self):
        names = self.archive.namelist()
        documents = list(filter(PROJECT_XML.fullmatch, names))
        protected = list(filter(PROJECT_ZIP.fullmatch, names))
        if documents and protected:
            raise ValueError("The archive contains ambiguous project data.")
        if documents:
            return self.read(self.single_document(documents))
        if len(protected) != 1:
            raise ValueError("No single ETS project was found in this file.")
        return self._protected_project(protected[0])

    @staticmethod
    def single_document(matches):
        if len(matches) != 1:
            raise ValueError("Export one ETS installation at a time.")
        return matches[0]

    def _protected_project(self, name):
        if not self.password:
            raise ValueError("This ETS project needs its project password.")
        derived = base64.b64encode(hashlib.pbkdf2_hmac(
            "sha256", self.password.encode("utf-16le"), b"21.project.ets.knx.org", 65536, 32,
        ))
        with pyzipper.AESZipFile(io.BytesIO(self.read(name, MAX_IMPORT_BYTES))) as inner:
            inner.setpassword(derived)
            matches = [item for item in inner.namelist()
                       if re.fullmatch(r"(?:P-[0-9A-Fa-f]+/)?\d+\.xml", item)]
            document = self.single_document(matches)
            if inner.getinfo(document).file_size > MAX_XML_BYTES:
                raise ValueError("The project's XML exceeds the import size limit.")
            try:
                return inner.read(document)
            except (RuntimeError, pyzipper.BadZipFile) as exc:
                raise ValueError("The project password was refused, or the file is damaged.") from exc


def safe_xml(data):
    if len(data) > MAX_XML_BYTES:
        raise ValueError("The XML file exceeds the import size limit.")
    return SafeXML.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)


def group_rows(root):
    """Read names and explicit datapoint types from project or group-export XML."""
    result = {}
    seen = set()
    for element in root.iterfind(".//{*}GroupAddress"):
        row = group_row(element.attrib)
        key = element.get("Id", row["address"])
        identity = {("id", key), ("address", row["address"])}
        if identity.intersection(seen):
            raise ValueError("The project repeats a group address or identifier.")
        result[key] = row
        seen.update(identity)
        if len(result) > MAX_GROUPS:
            raise ValueError(f"Import at most {MAX_GROUPS} group addresses at a time.")
    if not result:
        raise ValueError("No group addresses were found in this file.")
    return result


def group_row(attrs):
    address = address_text(attrs.get("Address", ""))
    # The group export lists the directly assigned DPT last.
    types = attrs.get("DPTs", "").split(",")
    return {"address": address, "name": attrs.get("Name") or address,
            "dpt": dpt_text(attrs.get("DatapointType", types[-1])),
            "secure": attrs.get("Security") == "On" or "Key" in attrs,
            "description": attrs.get("Description", "")}


def project_roles(master):
    return {item.attrib["Id"]: item.attrib["Name"] for item in master.iter()
            if local_tag(item) == "DatapointRole" and "Id" in item.attrib and "Name" in item.attrib}


def function_bindings(function, context, room):
    groups, roles, project_id = context
    refs = defaultdict(list)
    for item in function.findall("{*}GroupAddressRef"):
        group = groups.get(item.get("RefId"))
        if group:
            role = item.get("Role", "")
            refs[roles.get(role, role)].append(group)
    bindings = []
    identity = stable_id(project_id, function.get("Puid") or function.get("Id"))
    # The declared function type distinguishes a socket from a light.
    function_types = function.get("Implements", "").split()
    is_light = bool(set(function_types) & {"FT-1", "FT-2", "FT-6"})
    is_shade = bool(set(function_types) & {"FT-3", "FT-7"})
    if is_light:
        bindings.extend(role_bindings(refs, LIGHT_ROLES, {
            "entity": identity, "kind": "light", "name": function.get("Name") or identity, "room": room}))
    if is_shade:
        bindings.extend(role_bindings(refs, SHADE_ROLES, {
            "entity": identity, "kind": "shade", "name": function.get("Name") or identity, "room": room}))
    bindings.extend(sensor_bindings(refs, identity, room))
    return bindings


def sensor_bindings(refs, identity, room):
    result = []
    for role, dpt in SENSOR_ROLES.items():
        matches = refs[role]
        if len(matches) != 1 or matches[0]["dpt"] not in ("", dpt):
            continue
        group = matches[0]
        result.append({"entity": stable_id(identity, role), "kind": "sensor", "name": group["name"],
                       "room": room, "property": "value", "dpt": dpt,
                       "read": group["address"], "write": "", "scene": 1})
    return result


def role_bindings(refs, definitions, common):
    values = {}
    for role, (prop, direction, dpt) in definitions.items():
        matches = refs[role]
        if len(matches) != 1 or matches[0]["dpt"] not in ("", dpt):
            continue
        row = values.setdefault(prop, {
            **common, "property": prop, "dpt": dpt, "read": "", "write": "", "scene": 1,
        })
        row[direction] = matches[0]["address"]
    return list(values.values())


def project_bindings(root, groups, roles):
    project = next((item for item in root.iter() if local_tag(item) == "Project"), root)
    project_id = project.get("Id", "")
    bindings = []

    for function, room in project_functions(project):
        bindings.extend(function_bindings(function, (groups, roles, project_id), room))
    if len({(row["kind"], row["entity"]) for row in bindings}) > MAX_CONTROLS:
        raise ValueError(f"Import at most {MAX_CONTROLS} controls per KNX connection.")
    return bindings


def project_functions(project):
    pending = [(project, (), 0)]
    while pending:
        element, rooms, depth = pending.pop()
        if depth > 64:
            raise ValueError("The project XML is nested too deeply.")
        name = element.get("Name", "")
        if local_tag(element) in ("Space", "BuildingPart") and name:
            rooms = (*rooms, name)
        if local_tag(element) == "Function":
            yield element, " / ".join(rooms)
        pending.extend((child, rooms, depth + 1) for child in reversed(element))


def import_project(data, name, password=""):
    """Read off the event loop. Passwords and project keys are never returned."""
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError("Choose a project smaller than 128 MB.")
    if PurePosixPath(name).suffix.lower() == ".xml":
        groups = group_rows(safe_xml(data))
        return {"addresses": list(groups.values()), "bindings": []}
    archive = ProjectArchive(data, password)
    try:
        root = safe_xml(archive.project())
        master = safe_xml(archive.read("knx_master.xml"))
        groups = group_rows(root)
        bindings = project_bindings(root, groups, project_roles(master))
        return {"addresses": list(groups.values()), "bindings": bindings}
    finally:
        archive.archive.close()


def read_keyring(data, password):
    safe_xml(data)
    # The library's signature-verified loader accepts paths. The temporary
    # file contains only the still-encrypted export and is removed at once.
    with tempfile.TemporaryDirectory(prefix="openavc-knx-") as folder:
        path = Path(folder) / "keys.knxkeys"
        path.write_bytes(data)
        path.chmod(0o600)
        return sync_load_keyring(path, password, validate_signature=True)


class RecordedTCP(TCPTransport):
    """Bind the selected adapter and record actual wire bytes, per instance."""
    owner = None

    async def connect(self):
        self._buffer = b""
        factory = self.TCPTransportFactory(self.data_received_callback, self._connection_lost)
        local = self.owner.local_address(self.remote_hpai.ip_addr)
        async with asyncio.timeout(10):
            self.transport, _ = await asyncio.get_running_loop().create_connection(
                lambda: factory, host=self.remote_hpai.ip_addr, port=self.remote_hpai.port,
                local_addr=(local, 0) if local else None,
            )

    def data_received_callback(self, raw):
        self.owner.record_traffic("rx", raw)
        super().data_received_callback(raw)

    def send(self, frame, addr=None):
        super().send(frame, addr)
        self.owner.record_traffic("tx", frame.to_knx())


class RecordedSecure(SecureSession, RecordedTCP):
    """Keep the library's secure handshake; bind and record its encrypted socket."""


class RecordedUDP(UDPTransport):
    owner = None

    def data_received_callback(self, raw, addr):
        self.owner.record_traffic("rx", raw)
        super().data_received_callback(raw, addr)

    def send(self, frame, addr=None):
        super().send(frame, addr)
        self.owner.record_traffic("tx", frame.to_knx())


class PlainTunnel(TCPTunnel):
    def _init_transport(self):
        self.transport = RecordedTCP((self.gateway_ip, self.gateway_port), self._connection_lost)


class EncryptedTunnel(SecureTunnel):
    def _init_transport(self):
        self.transport = RecordedSecure(
            remote_addr=(self.gateway_ip, self.gateway_port), user_id=self._user_id,
            user_password=self._user_password, device_authentication_password=self._device_authentication_password,
            connection_lost_cb=self._connection_lost,
        )


class DatagramTunnel(UDPTunnel):
    def _init_transport(self):
        self.transport = RecordedUDP(
            local_addr=(self.local_ip, self.local_port), remote_addr=(self.gateway_ip, self.gateway_port), multicast=False,
        )


class RecordedInterface(KNXIPInterface):
    """Small socket adapters around the pinned stack, without global patches."""
    owner = None

    def cemi_received(self, raw_cemi):
        self.owner._queue_cemi(raw_cemi)

    async def _open(self, tunnel):
        self._interface = tunnel
        tunnel.transport.owner = self.owner
        await tunnel.connect()

    def _args(self, gateway_ip, gateway_port):
        return {"xknx": self.xknx, "gateway_ip": gateway_ip, "gateway_port": gateway_port,
                "cemi_received_callback": self.cemi_received, "auto_reconnect": False}

    async def _start_tunnelling_tcp(self, gateway_ip, gateway_port):
        await self._open(PlainTunnel(**self._args(gateway_ip, gateway_port)))

    async def _start_secure_tunnelling_tcp(self, gateway_ip, gateway_port, gateway_descriptor=None, keyring=None):
        secure = self.connection_config.secure_config
        await self._open(EncryptedTunnel(
            **self._args(gateway_ip, gateway_port), user_id=secure.user_id,
            user_password=secure.user_password, device_authentication_password=secure.device_authentication_password,
        ))

    async def _start_tunnelling_udp(self, gateway_ip, gateway_port, local_ip):
        await self._open(DatagramTunnel(
            **self._args(gateway_ip, gateway_port), local_ip=local_ip or "0.0.0.0", route_back=True,
        ))


COMMON_STATE = {
    "name": {"type": "string", "label": "Name", "control": False},
    "room": {"type": "string", "label": "Room", "control": False},
    "feedback": {"type": "string", "label": "Feedback", "control": False},
    "last_received": {"type": "number", "label": "Last status received", "control": False,
                      "cloud_priority": "low", "help": "Unix time of the last valid status telegram."},
}
CONTROL_SCHEMAS = {
    "on": {"type": "boolean", "label": "On", "control": True},
    "level": {"type": "number", "label": "Brightness", "min": 0, "max": 100, "unit": "%", "control": True},
    "dim": {"type": "enum", "label": "Relative dimming", "values": ["brighter", "darker", "stop"], "control": True},
    "move": {"type": "enum", "label": "Move", "values": ["up", "down"], "control": True},
    "step_stop": {"type": "enum", "label": "Step / stop", "values": ["up", "down"], "control": True,
                  "help": "Stops a moving blind; steps its slats in this direction when stationary."},
    "position": {"type": "number", "label": "Position (0 = open)", "min": 0, "max": 100, "unit": "%", "control": True},
    "tilt": {"type": "number", "label": "Slat position", "min": 0, "max": 100, "unit": "%", "control": True},
}
SENSOR_TYPES = {
    "1.001": ("boolean", ""), "1.002": ("boolean", ""), "1.005": ("boolean", ""),
    "1.018": ("boolean", ""), "1.019": ("boolean", ""), "5.001": ("number", "%"),
    "7.001": ("integer", ""), "9.001": ("number", "°C"), "9.004": ("number", "lx"),
    "9.005": ("number", "m/s"), "9.006": ("number", "Pa"), "9.007": ("number", "%"),
    "9.008": ("number", "ppm"), "12.001": ("integer", ""), "13.010": ("integer", "Wh"),
}








class Bindings:
    TYPES = {"light": {"on": ("1.001",), "level": ("5.001",), "dim": ("3.007",)},
             "shade": {"move": ("1.008",), "step_stop": ("1.007",), "position": ("5.001",), "tilt": ("5.001",)},
             "sensor": {"value": SENSOR_TYPES}, "scene": {"recall": ("17.001", "18.001")}}

    def __init__(self, rows):
        if not isinstance(rows, list):
            raise ValueError("Control mappings must be a table.")
        self.rows = {}
        self.identities = {}
        for row in rows:
            self.add(self.normalize(row))
        if len(self.identities) > MAX_CONTROLS:
            raise ValueError(f"Use at most {MAX_CONTROLS} controls per KNX connection.")

    def normalize(self, original):
        row = dict(original)
        for field in ("kind", "entity", "property", "name", "room"):
            row[field] = str(row.get(field, "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row["entity"]):
            raise ValueError("Use a control ID with letters, digits, underscores or hyphens.")
        row["dpt"] = dpt_text(row.get("dpt"))
        allowed = self.TYPES.get(row["kind"], {}).get(row["property"], ())
        if row["dpt"] not in allowed:
            raise ValueError("The control kind, property and datapoint type do not match.")
        for direction in ("read", "write"):
            row[direction] = address_text(row[direction]) if row.get(direction) else ""
        self.check_directions(row)
        return row

    @staticmethod
    def check_directions(row):
        if not row["read"] and not row["write"]:
            raise ValueError("Each mapping needs a command or status address.")
        if row["property"] in ("recall", "dim", "move", "step_stop") and row["read"]:
            raise ValueError("Movement and scene commands have no measured state. Use a separate status address.")

    @staticmethod
    def check_kind(row):
        if row["kind"] == "sensor" and row["write"]:
            raise ValueError("Sensor mappings are read-only.")
        if row["kind"] == "scene":
            if not 1 <= int(row.get("scene", 0)) <= 64:
                raise ValueError("Scene numbers run from 1 to 64.")

    def add(self, row):
        self.check_kind(row)
        if not row["name"]:
            raise ValueError("Give each control a name.")
        identity = (row["name"], row["room"])
        target = (row["kind"], row["entity"])
        if self.identities.setdefault(target, identity) != identity:
            raise ValueError("All properties of a control must use the same name and room.")
        key = (*target, row["property"])
        if key in self.rows:
            raise ValueError(f"The control {row['name']} repeats property {row['property']}.")
        self.rows[key] = row


def validate_bindings(rows):
    return Bindings(rows).rows


def check_mapping_types(rows, addresses):
    imported = {address_text(row["address"]): dpt_text(row.get("dpt", "")) for row in addresses}
    if len(imported) != len(addresses):
        raise ValueError("The address table repeats a group address.")
    for row in rows.values():
        expected = row["dpt"]
        for address in (row["read"], row["write"]):
            actual = imported.get(address)
            if actual not in (None, "", expected):
                raise ValueError(f"The saved control uses {address} as {expected}, but the address table says {actual}. Update its mapping before connecting.")


def binding_schema(row):
    prop = row["property"]
    if prop == "recall":
        return {}
    if row["kind"] == "sensor":
        dtype, unit = SENSOR_TYPES[row["dpt"]]
        schema = {"type": dtype, "label": "Value", "control": False}
        if unit:
            schema["unit"] = unit
    else:
        schema = {**CONTROL_SCHEMAS[prop], "control": bool(row["write"])}
    result = {prop: schema}
    if prop not in ("dim", "move", "step_stop"):
        result[prop + "_valid"] = {"type": "boolean", "label": schema["label"] + " received", "control": False}
    return result


def decode_value(dpt, payload):
    if dpt.startswith("1."):
        if not isinstance(payload, DPTBinary) or payload.value not in (0, 1):
            raise ValueError("Invalid one-bit status.")
        return bool(payload.value)
    transcoder = DPTBase.parse_transcoder(dpt)
    if transcoder is None:
        raise ValueError("Unsupported status datatype.")
    return transcoder.from_knx(payload)


def encode_value(row, value):
    prop = row["property"]
    if prop == "recall":
        return DPTArray(int(row["scene"]) - 1)  # learn/store bit remains zero for DPT 18.001
    if prop == "on":
        return encode_boolean(value)
    if prop in ("move", "step_stop", "dim"):
        return encode_direction(prop, value)
    return encode_percentage(value)


def encode_boolean(value):
    # Dynamic command fields and macro arguments can arrive as text.
    if isinstance(value, str):
        value = {"true": True, "false": False}.get(value.lower())
    if not isinstance(value, bool):
        raise CommandRefusedError("On needs Yes or No.")
    return DPTBinary(int(value))


def encode_direction(prop, value):
    values = {"move": {"up": 0, "down": 1}, "step_stop": {"up": 0, "down": 1},
              "dim": {"brighter": 9, "darker": 1, "stop": 0}}
    if value not in values[prop]:
        raise CommandRefusedError("Choose one of the control's listed directions.")
    return DPTBinary(values[prop][value])


def encode_percentage(value):
    try:
        number = float(value)
        if isinstance(value, bool) or not 0 <= number <= 100:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise CommandRefusedError("Use a percentage from 0 to 100.") from exc
    return DPTBase.parse_transcoder("5.001").to_knx(number)


def authentication_failure(exc):
    visited = set()
    while exc is not None and id(exc) not in visited:
        visited.add(id(exc))
        if isinstance(exc, (IPSecureError, InvalidSecureConfiguration)):
            return True
        exc = exc.__cause__
    return False


class CounterJournal:
    """Durable Data Secure replay counters, never keys or passwords.

    Commit received counters before publishing a telegram. Reserve a range of
    sending counters before using any of it, so a crash cannot reuse a nonce.
    Entries are keyed by key-material hashes: re-exporting an unchanged key
    keeps its counters, and a key rotation does not erase other keys' history.
    SQLite transactions also serialize two processes using the same context.
    """
    def __init__(self, context, keyring, *, simulation=False):
        root = get_data_dir() / "driver-state" / "knx"
        if simulation:
            root = root / "simulation"
        # Audits create a temporary driver ID but use the same tunnel keys.
        # The imported security context owns their shared nonce/replay history.
        self.path = root / (stable_id(context) + ".sqlite3")
        keys = keyring.get_data_secure_group_keys()
        self.tokens = {group: hashlib.sha256(key).hexdigest() for group, key in keys.items()}
        self.by_sender = defaultdict(set)
        assignments = (entry for interface in keyring.interfaces for entry in interface.group_addresses.items())
        for group, senders in assignments:
            if group not in self.tokens:
                continue
            for sender in senders:
                self.by_sender[str(sender)].add(self.tokens[group])
        self.limit = 0
        self.received = {}

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=5)
        self.path.chmod(0o600)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS counters (direction TEXT, peer TEXT, token TEXT, value INTEGER, PRIMARY KEY(direction, peer, token))")
        return db

    def restore(self):
        """Return the highest committed counter still relevant to each sender."""
        received = {}
        with closing(self._open()) as db:
            for peer, token, value in db.execute("SELECT peer, token, value FROM counters WHERE direction='rx'"):
                if token in self._sender_tokens(peer):
                    received[peer] = max(received.get(peer, 0), value)
        self.received = received
        return received

    def _sender_tokens(self, peer):
        return self.by_sender.get(peer) or set(self.tokens.values())

    @staticmethod
    def _highest(db, direction, peer, tokens):
        values = [value for token, value in db.execute(
            "SELECT token, value FROM counters WHERE direction=? AND peer=?", (direction, peer),
        ) if token in tokens]
        return max(values, default=0)

    @staticmethod
    def _store(db, identity, tokens, value):
        direction, peer = identity
        db.executemany(
            "INSERT INTO counters VALUES(?,?,?,?) ON CONFLICT(direction,peer,token) DO UPDATE SET value=max(value,excluded.value)",
            [(direction, peer, token, value) for token in tokens],
        )

    def accept(self, peer, value):
        tokens = self._sender_tokens(peer)
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if value <= self._highest(db, "rx", peer, tokens):
                return False
            self._store(db, ("rx", peer), tokens, value)
        self.received[peer] = value
        return True

    def reserve(self, peer, next_value):
        tokens = set(self.tokens.values())
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            start = max(next_value, self._highest(db, "tx", peer, tokens) + 1)
            limit = start + 1024
            if limit >= 0xFFFFFFFFFFFF:
                raise ValueError("The Data Secure sending counter is exhausted. Recommission this secure interface in ETS.")
            self._store(db, ("tx", peer), tokens, limit)
        self.limit = limit
        return start


class DurableCEMI(CEMIHandler):
    """Hold received telegrams until their replay counters are on disk."""
    def __init__(self, knx, journal):
        super().__init__(knx)
        self.journal = journal
        self.pending = []

    def data_secure_init(self, keyring):
        super().data_secure_init(keyring)
        if self.data_secure and self.journal:
            table = self.data_secure._individual_address_table
            for sender in table:
                table[sender] = max(table[sender], self.journal.received.get(str(sender), 0))

    def telegram_received(self, telegram):
        self.pending.append(telegram)

    async def receive(self, raw):
        self.pending = []
        self.handle_raw_cemi(raw)
        for telegram in self.pending:
            if await self._persist(telegram):
                super().telegram_received(telegram)
        self.pending = []

    async def _persist(self, telegram):
        if not telegram.data_secure or self.journal is None:
            return True
        sender = telegram.source_address
        value = self.data_secure._individual_address_table[sender]
        return await asyncio.to_thread(self.journal.accept, str(sender), value)

    async def send_telegram(self, telegram):
        secure = self.data_secure
        if self.journal is None:
            return await super().send_telegram(telegram)
        if secure._sequence_number_sending >= self.journal.limit:
            secure._sequence_number_sending = await asyncio.to_thread(
                self.journal.reserve, str(self.xknx.current_address), secure._sequence_number_sending,
            )
        await super().send_telegram(telegram)


class KNXIPDriver(BaseDriver):
    DRIVER_INFO = {'id': 'knx_ip',
     'discovery': {'udp_probe': {
         'port': 3671, 'send_hex': '06 10 02 03 00 0e 08 01 00 00 00 00 00 00',
         'expect_regex': r'(?s)^\x06\x10\x02\x04..\x36\x01.{52}[\x04-\xfe]\x02(?:[\x02-\x09][\x01-\x02])*\x04[\x01-\x02].*$',
         'timeout_ms': 1500, 'cross_vendor': True}},
     'name': 'KNX/IP',
     'manufacturer': 'Generic',
     'category': 'utility',
     'version': '1.0.2',
     'author': 'OpenAVC',
     'transport': 'tcp',
     'ports': [3671],
     'protocols': ['knx-ip'],
     'min_platform_version': '0.40.1',
     'verified': False,
     'simulated': True,
     'tags': ['knx', 'lighting', 'shades', 'sensors', 'building-automation'],
     'source_url': 'https://support.knx.org/hc/en-us/articles/360000040999-KNX-Specifications',
     'description': 'Named KNX lights, shades, scenes and sensors, ETS project import and secure tunnelling.',
     'help': {'overview': 'Import the ETS project to use its room names, functions and explicit status mappings. '
                          'Controls expose actual received status; an accepted command never invents a light level or '
                          'shade position. KNX/IP Secure protects the interface connection, and KNX Data Secure '
                          'protects configured group traffic.',
              'setup': 'Add the KNX/IP interface by address, or use Find Interfaces and Select Interface. Import an '
                       'ETS .knxproj project or group-address XML export. Project functions create supported controls '
                       'automatically; use Add Light, Shade, Sensor or Scene for remaining mappings. For secure '
                       'installations, import the ETS .knxkeys export with its export password. The project password '
                       'and key-export password may differ. Choose the named controls in UI bindings, macros and '
                       'triggers. This driver uses tunnelling; it does not program KNX devices or change their ETS '
                       'configuration.',
              'connection': 'Allow KNXnet/IP on port 3671 between OpenAVC and the interface. A secure interface needs '
                            'a free tunnel slot and its matching credentials. Status objects must be configured in '
                            'ETS to transmit changes and answer group reads. A silent status address remains unknown '
                            'after reconnect.'},
     'default_config': {'port': 3671,
                        'mode': 'auto',
                        'user_id': 2,
                        'poll_interval': 0,
                        'reconcile_seconds': 300,
                        'addresses': [],
                        'bindings': [],
                        'keyring_data': '',
                        'keyring_password': '',
                        'user_password': '',
                        'device_password': '',
                        'security_context': ''},
     'config_schema': {'mode': {'type': 'enum',
                                'label': 'Connection',
                                'default': 'auto',
                                'values': [{'value': 'auto', 'label': 'Automatic'},
                                           {'value': 'secure', 'label': 'Secure TCP'},
                                           {'value': 'tcp', 'label': 'Plain TCP'},
                                           {'value': 'udp', 'label': 'Plain UDP'}],
                                'help': "Automatic reads the interface's requirements. Imported secure credentials "
                                        'always require secure TCP; failed authentication never falls back to plain '
                                        'traffic.'},
                       'user_id': {'type': 'integer',
                                   'label': 'Secure tunnel user',
                                   'default': 2,
                                   'min': 2,
                                   'max': 127,
                                   'advanced': True,
                                   'help': 'Used for manually entered credentials. The key import selects the '
                                           'matching ETS tunnel instead.'},
                       'user_password': {'type': 'string',
                                         'label': 'Tunnel password',
                                         'secret': True,
                                         'advanced': True},
                       'device_password': {'type': 'string',
                                           'label': 'Device authentication password',
                                           'secret': True,
                                           'advanced': True,
                                           'help': 'Required with manually entered secure credentials, so OpenAVC '
                                                   "verifies the interface's identity."},
                       'keyring_data': {'type': 'text',
                                        'label': 'Imported security keys',
                                        'secret': True,
                                        'advanced': True,
                                        'help': 'Managed by Import Security Keys. Leave blank when using manual '
                                                'tunnel credentials.'},
                       'keyring_password': {'type': 'string',
                                            'label': 'Key export password',
                                            'secret': True,
                                            'advanced': True},
                       'reconcile_seconds': {'type': 'integer',
                                             'label': 'Refresh status every (seconds)',
                                             'default': 300,
                                             'min': 60,
                                             'max': 3600,
                                             'advanced': True},
                       'addresses': {'type': 'table',
                                     'label': 'Imported addresses',
                                     'row_label': 'address',
                                     'advanced': True,
                                     'columns': {'address': {'type': 'string',
                                                             'label': 'Group address',
                                                             'required': True},
                                                 'name': {'type': 'string', 'label': 'Name'},
                                                 'dpt': {'type': 'string', 'label': 'Datapoint type'},
                                                 'secure': {'type': 'boolean',
                                                            'label': 'Data Secure required',
                                                            'default': False},
                                                 'description': {'type': 'string', 'label': 'Description'}}},
                       'bindings': {'type': 'table',
                                    'label': 'Control mappings',
                                    'row_label': 'mapping',
                                    'advanced': True,
                                    'help': 'Import and Add actions fill these mappings. One row is one property of a '
                                            'named control. A blank status address has no measured feedback.',
                                    'columns': {'entity': {'type': 'string', 'label': 'Control ID', 'required': True},
                                                'kind': {'type': 'enum',
                                                         'label': 'Kind',
                                                         'values': ['light', 'shade', 'sensor', 'scene']},
                                                'name': {'type': 'string', 'label': 'Name', 'required': True},
                                                'room': {'type': 'string', 'label': 'Room'},
                                                'property': {'type': 'string', 'label': 'Property', 'required': True},
                                                'dpt': {'type': 'string', 'label': 'Datapoint type', 'required': True},
                                                'write': {'type': 'string', 'label': 'Command address'},
                                                'read': {'type': 'string', 'label': 'Status address'},
                                                'scene': {'type': 'integer',
                                                          'label': 'Scene number',
                                                          'min': 1,
                                                          'max': 64,
                                                          'default': 1}}}},
     'state_variables': {'interface_name': {'type': 'string', 'label': 'Interface'},
                         'individual_address': {'type': 'string', 'label': 'Assigned KNX address'},
                         'connection_mode': {'type': 'string', 'label': 'Connection mode'},
                         'control_count': {'type': 'integer', 'label': 'Named controls'},
                         'address_count': {'type': 'integer', 'label': 'Imported addresses'},
                         'awaiting_status': {'type': 'integer', 'label': 'Status addresses awaiting a reply'},
                         'security_errors': {'type': 'integer', 'label': 'Unreadable secure telegrams'},
                         'interfaces': {'type': 'string', 'label': 'Discovered interfaces', 'cloud_priority': 'low'},
                         'addresses_1_001': {'type': 'string', 'label': 'Switch addresses', 'cloud_priority': 'low'},
                         'addresses_5_001': {'type': 'string',
                                             'label': 'Percentage addresses',
                                             'cloud_priority': 'low'},
                         'addresses_3_007': {'type': 'string',
                                             'label': 'Relative dimming addresses',
                                             'cloud_priority': 'low'},
                         'addresses_1_007': {'type': 'string',
                                             'label': 'Step / stop addresses',
                                             'cloud_priority': 'low'},
                         'addresses_1_008': {'type': 'string', 'label': 'Up/down addresses', 'cloud_priority': 'low'},
                         'sensor_addresses': {'type': 'string', 'label': 'Sensor addresses', 'cloud_priority': 'low'},
                         'scene_addresses': {'type': 'string', 'label': 'Scene addresses', 'cloud_priority': 'low'}},
     'child_entity_types': {'light': {'label': 'Light',
                                      'label_plural': 'Lights',
                                      'id_format': {'type': 'string'},
                                      'dynamic': True,
                                      'label_field': 'name',
                                      'summary_fields': ['room', 'on', 'level', 'feedback'],
                                      'state_variables': {'name': {'type': 'string',
                                                                   'label': 'Name',
                                                                   'control': False},
                                                          'room': {'type': 'string',
                                                                   'label': 'Room',
                                                                   'control': False},
                                                          'feedback': {'type': 'string',
                                                                       'label': 'Feedback',
                                                                       'control': False},
                                                          'last_received': {'type': 'number',
                                                                            'label': 'Last status received',
                                                                            'control': False,
                                                                            'cloud_priority': 'low',
                                                                            'help': 'Unix time of the last valid '
                                                                                    'status telegram.'}}},
                            'shade': {'label': 'Shade',
                                      'label_plural': 'Shades',
                                      'id_format': {'type': 'string'},
                                      'dynamic': True,
                                      'label_field': 'name',
                                      'summary_fields': ['room', 'position', 'feedback'],
                                      'state_variables': {'name': {'type': 'string',
                                                                   'label': 'Name',
                                                                   'control': False},
                                                          'room': {'type': 'string',
                                                                   'label': 'Room',
                                                                   'control': False},
                                                          'feedback': {'type': 'string',
                                                                       'label': 'Feedback',
                                                                       'control': False},
                                                          'last_received': {'type': 'number',
                                                                            'label': 'Last status received',
                                                                            'control': False,
                                                                            'cloud_priority': 'low',
                                                                            'help': 'Unix time of the last valid '
                                                                                    'status telegram.'}}},
                            'sensor': {'label': 'Sensor',
                                       'label_plural': 'Sensors',
                                       'id_format': {'type': 'string'},
                                       'dynamic': True,
                                       'label_field': 'name',
                                       'summary_fields': ['room', 'value', 'feedback'],
                                       'state_variables': {'name': {'type': 'string',
                                                                    'label': 'Name',
                                                                    'control': False},
                                                           'room': {'type': 'string',
                                                                    'label': 'Room',
                                                                    'control': False},
                                                           'feedback': {'type': 'string',
                                                                        'label': 'Feedback',
                                                                        'control': False},
                                                           'last_received': {'type': 'number',
                                                                             'label': 'Last status received',
                                                                             'control': False,
                                                                             'cloud_priority': 'low',
                                                                             'help': 'Unix time of the last valid '
                                                                                     'status telegram.'}}},
                            'scene': {'label': 'Scene',
                                      'label_plural': 'Scenes',
                                      'id_format': {'type': 'string'},
                                      'dynamic': True,
                                      'label_field': 'name',
                                      'summary_fields': ['room'],
                                      'state_variables': {'name': {'type': 'string',
                                                                   'label': 'Name',
                                                                   'control': False},
                                                          'room': {'type': 'string',
                                                                   'label': 'Room',
                                                                   'control': False},
                                                          'feedback': {'type': 'string',
                                                                       'label': 'Feedback',
                                                                       'control': False},
                                                          'last_received': {'type': 'number',
                                                                            'label': 'Last status received',
                                                                            'control': False,
                                                                            'cloud_priority': 'low',
                                                                            'help': 'Unix time of the last valid '
                                                                                    'status telegram.'}}}},
     'commands': {'set_light': {'label': 'Set Light',
                                'help': 'Send a configured command. Reported values change only on received status '
                                        'telegrams.',
                                'params': {'target': {'type': 'child_id',
                                                      'child_type': 'light',
                                                      'label': 'Light',
                                                      'required': True},
                                           'property': {'type': 'string',
                                                        'label': 'Control',
                                                        'required': True,
                                                        'options_from': {'param': 'target', 'source': 'child_schema'}},
                                           'value': {'type': 'string',
                                                     'label': 'Value',
                                                     'required': True,
                                                     'type_from': {'param': 'property'}}}},
                  'set_shade': {'label': 'Set Shade',
                                'help': 'Send a configured command. Reported values change only on received status '
                                        'telegrams.',
                                'params': {'target': {'type': 'child_id',
                                                      'child_type': 'shade',
                                                      'label': 'Shade',
                                                      'required': True},
                                           'property': {'type': 'string',
                                                        'label': 'Control',
                                                        'required': True,
                                                        'options_from': {'param': 'target', 'source': 'child_schema'}},
                                           'value': {'type': 'string',
                                                     'label': 'Value',
                                                     'required': True,
                                                     'type_from': {'param': 'property'}}}},
                  'recall_scene': {'label': 'Recall Scene',
                                   'help': 'Recall the named scene. KNX does not report whether every participant has '
                                           'reached it.',
                                   'params': {'scene': {'type': 'child_id',
                                                        'child_type': 'scene',
                                                        'label': 'Scene',
                                                        'required': True}}},
                  'refresh': {'label': 'Refresh Status',
                              'help': 'Read the configured feedback addresses at a bounded rate.',
                              'params': {}}},
     'quick_actions': ['set_light', 'set_shade', 'recall_scene', 'refresh'],
     'actions': [{'id': 'import_project',
                  'kind': 'setup',
                  'label': 'Import ETS Project',
                  'icon': 'upload',
                  'availability': 'always',
                  'confirm': 'Import names and supported function mappings from ETS. Existing controls and edits are '
                             'kept. The file is removed after import; its password is not saved.',
                  'params': {'project': {'type': 'file',
                                         'label': 'ETS project or group addresses',
                                         'required': True,
                                         'accept': ['.knxproj', '.xml'],
                                         'max_bytes': 134217728},
                             'password': {'type': 'string',
                                          'label': 'Project password (if protected)',
                                          'secret': True}}},
                 {'id': 'import_keys',
                  'kind': 'setup',
                  'label': 'Import Security Keys',
                  'icon': 'key-round',
                  'availability': 'always',
                  'confirm': "Import the encrypted ETS key export. Its password is stored using OpenAVC's secret "
                             'storage. A failed secure connection will never retry in plain mode.',
                  'params': {'keys': {'type': 'file',
                                      'label': 'ETS security key export',
                                      'required': True,
                                      'accept': ['.knxkeys'],
                                      'max_bytes': 2000000},
                             'password': {'type': 'string',
                                          'label': 'Key export password',
                                          'secret': True,
                                          'required': True}}},
                 {'id': 'find_interfaces',
                  'kind': 'setup',
                  'label': 'Find Interfaces',
                  'icon': 'search',
                  'availability': 'always',
                  'confirm': 'Find KNX/IP interfaces on the selected local network. Choose one with Select Interface '
                             'afterwards.',
                  'params': {}},
                 {'id': 'select_interface',
                  'kind': 'setup',
                  'label': 'Select Interface',
                  'availability': 'always',
                  'confirm': 'Connect this device to the selected interface.',
                  'params': {'interface': {'type': 'string',
                                           'label': 'Interface',
                                           'options_state': 'interfaces',
                                           'required': True}}},
                 {'id': 'add_light',
                  'kind': 'setup',
                  'label': 'Add or Update Light',
                  'availability': 'always',
                  'confirm': 'Choose separate command and status addresses. Only configured commands appear as '
                             'controls; missing status stays unknown.',
                  'params': {'name': {'type': 'string', 'label': 'Name', 'required': True},
                             'room': {'type': 'string', 'label': 'Room'},
                             'existing': {'type': 'child_id',
                                          'child_type': 'light',
                                          'label': 'Update existing (optional)',
                                          'help': 'Leave blank to add. When updating, blank address fields keep their '
                                                  'current mapping.'},
                             'on_write': {'type': 'string',
                                          'label': 'Switch command',
                                          'required': False,
                                          'options_state': 'addresses_1_001',
                                          'help': 'Choose an imported address by name, or enter the group address '
                                                  'supplied by the KNX programmer.'},
                             'on_read': {'type': 'string',
                                         'label': 'Switch status',
                                         'required': False,
                                         'options_state': 'addresses_1_001',
                                         'help': 'Choose an imported address by name, or enter the group address '
                                                 'supplied by the KNX programmer.'},
                             'level_write': {'type': 'string',
                                             'label': 'Brightness command',
                                             'required': False,
                                             'options_state': 'addresses_5_001',
                                             'help': 'Choose an imported address by name, or enter the group address '
                                                     'supplied by the KNX programmer.'},
                             'level_read': {'type': 'string',
                                            'label': 'Brightness status',
                                            'required': False,
                                            'options_state': 'addresses_5_001',
                                            'help': 'Choose an imported address by name, or enter the group address '
                                                    'supplied by the KNX programmer.'},
                             'dim_write': {'type': 'string',
                                           'label': 'Relative dimming command',
                                           'required': False,
                                           'options_state': 'addresses_3_007',
                                           'help': 'Choose an imported address by name, or enter the group address '
                                                   'supplied by the KNX programmer.'}}},
                 {'id': 'add_shade',
                  'kind': 'setup',
                  'label': 'Add or Update Shade',
                  'availability': 'always',
                  'confirm': "Choose the shade's command and status addresses. Position is KNX percentage: 0 fully "
                             'open, 100 fully closed. Movement is never estimated.',
                  'params': {'name': {'type': 'string', 'label': 'Name', 'required': True},
                             'room': {'type': 'string', 'label': 'Room'},
                             'existing': {'type': 'child_id',
                                          'child_type': 'shade',
                                          'label': 'Update existing (optional)',
                                          'help': 'Leave blank to add. When updating, blank address fields keep their '
                                                  'current mapping.'},
                             'move_write': {'type': 'string',
                                            'label': 'Up/down command',
                                            'required': False,
                                            'options_state': 'addresses_1_008',
                                            'help': 'Choose an imported address by name, or enter the group address '
                                                    'supplied by the KNX programmer.'},
                             'step_stop_write': {'type': 'string',
                                                 'label': 'Step/stop command',
                                                 'required': False,
                                                 'options_state': 'addresses_1_007',
                                                 'help': 'Choose an imported address by name, or enter the group '
                                                         'address supplied by the KNX programmer.'},
                             'position_write': {'type': 'string',
                                                'label': 'Position command',
                                                'required': False,
                                                'options_state': 'addresses_5_001',
                                                'help': 'Choose an imported address by name, or enter the group '
                                                        'address supplied by the KNX programmer.'},
                             'position_read': {'type': 'string',
                                               'label': 'Position status',
                                               'required': False,
                                               'options_state': 'addresses_5_001',
                                               'help': 'Choose an imported address by name, or enter the group '
                                                       'address supplied by the KNX programmer.'},
                             'tilt_write': {'type': 'string',
                                            'label': 'Slat position command',
                                            'required': False,
                                            'options_state': 'addresses_5_001',
                                            'help': 'Choose an imported address by name, or enter the group address '
                                                    'supplied by the KNX programmer.'},
                             'tilt_read': {'type': 'string',
                                           'label': 'Slat position status',
                                           'required': False,
                                           'options_state': 'addresses_5_001',
                                           'help': 'Choose an imported address by name, or enter the group address '
                                                   'supplied by the KNX programmer.'}}},
                 {'id': 'add_sensor',
                  'kind': 'setup',
                  'label': 'Add or Update Sensor',
                  'availability': 'always',
                  'confirm': "Add a read-only measurement or status. Its datatype supplies the value's units; missing "
                             'replies stay unknown.',
                  'params': {'name': {'type': 'string', 'label': 'Name', 'required': True},
                             'room': {'type': 'string', 'label': 'Room'},
                             'existing': {'type': 'child_id',
                                          'child_type': 'sensor',
                                          'label': 'Update existing (optional)',
                                          'help': 'Leave blank to add. When updating, blank address fields keep their '
                                                  'current mapping.'},
                             'address': {'type': 'string',
                                         'label': 'Status address',
                                         'options_state': 'sensor_addresses',
                                         'required': True},
                             'dpt': {'type': 'enum',
                                     'label': 'Datapoint type (only if not imported)',
                                     'values': ['1.001',
                                                '1.002',
                                                '1.005',
                                                '1.018',
                                                '1.019',
                                                '5.001',
                                                '7.001',
                                                '9.001',
                                                '9.004',
                                                '9.005',
                                                '9.006',
                                                '9.007',
                                                '9.008',
                                                '12.001',
                                                '13.010']}}},
                 {'id': 'add_scene',
                  'kind': 'setup',
                  'label': 'Add or Update Scene',
                  'availability': 'always',
                  'confirm': 'Name an existing KNX scene. Recall sends its scene number; it does not learn or '
                             'overwrite the scene.',
                  'params': {'name': {'type': 'string', 'label': 'Name', 'required': True},
                             'room': {'type': 'string', 'label': 'Room'},
                             'existing': {'type': 'child_id',
                                          'child_type': 'scene',
                                          'label': 'Update existing (optional)',
                                          'help': 'Leave blank to add. When updating, blank address fields keep their '
                                                  'current mapping.'},
                             'address': {'type': 'string',
                                         'label': 'Scene address',
                                         'options_state': 'scene_addresses',
                                         'required': True},
                             'scene': {'type': 'integer',
                                       'label': 'Scene number',
                                       'min': 1,
                                       'max': 64,
                                       'required': True},
                             'dpt': {'type': 'enum',
                                     'label': 'Datapoint type (only if not imported)',
                                     'values': ['17.001', '18.001']}}}]}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._knx = None
        self._keyring = None
        self._closing = False
        self._rows = {}
        self._feedback = defaultdict(list)
        self._seen = set()
        self._roster_ids = set()
        self._refresh_task = None
        self._send_lock = asyncio.Lock()
        self._cemi_queue = asyncio.Queue(maxsize=256)
        self._rebuild()

    def _rebuild(self):
        self._rows = validate_bindings(self.config.get("bindings", []))
        check_mapping_types(self._rows, self.config.get("addresses", []))
        self._feedback = defaultdict(list)
        for key, row in self._rows.items():
            if row["read"]:
                self._feedback[row["read"]].append(key)
        self._publish_roster()
        self._publish_options()

    async def _pre_connect(self):
        try:
            self._rebuild()
        except (ValueError, TypeError) as exc:
            raise ConnectionFaultError(str(exc), code="invalid_config") from exc
        self._closing = False

    async def _create_transport(self, transport_type):
        # The protocol library owns its socket; RecordedInterface supplies the
        # platform's adapter choice, wire recorder and reconnect ownership.
        pass

    async def _post_connect(self):
        host = str(self.config.get("host", ""))
        port = int(self.config.get("port", 3671))
        descriptor = await request_description(host, port, self.local_address(host), route_back=True)
        if not descriptor.supports_tunnelling:
            raise ConnectionFaultError("This KNX interface does not advertise tunnelling.", code="invalid_config")
        self._keyring = await self._load_keys()
        mode, secure = self._connection_settings(descriptor)
        config = ConnectionConfig(
            connection_type={"secure": ConnectionType.TUNNELING_TCP_SECURE,
                             "tcp": ConnectionType.TUNNELING_TCP, "udp": ConnectionType.TUNNELING}[mode],
            gateway_ip=host, gateway_port=port, local_ip=self.local_address(host),
            auto_reconnect=False, route_back=True, secure_config=secure,
        )
        self._knx = XKNX(
            connection_config=config, state_updater=False,
            telegram_received_cb=self._telegram, connection_state_changed_cb=self._connection_changed,
        )
        journal = await self._counter_journal()
        self._knx.cemi_handler = DurableCEMI(self._knx, journal)
        self._cemi_queue = asyncio.Queue(maxsize=256)
        self.spawn_task(self._receive_cemi())
        interface = RecordedInterface(self._knx, config)
        interface.owner = self
        self._knx.knxip_interface = interface
        try:
            await self._knx.start()
        except CommunicationError as exc:
            if authentication_failure(exc):
                raise ConnectionFaultError("KNX secure authentication failed. Check the tunnel credentials or import a current key export.", code="auth_failed") from exc
            raise
        self.set_state("interface_name", descriptor.name)
        self.set_state("individual_address", str(self._knx.current_address))
        self.set_state("connection_mode", mode)

    async def _counter_journal(self):
        if not self._keyring or not self._keyring.get_data_secure_group_keys():
            return None
        context = self.config.get("security_context")
        if not context:
            raise ConnectionFaultError("Import the ETS security keys to initialize their replay history.", code="invalid_config")
        simulation = bool(self.state.get("system.simulation_active"))
        journal = CounterJournal(context, self._keyring, simulation=simulation)
        if not simulation and not journal.path.is_file():
            raise ConnectionFaultError("The Data Secure history is missing. Restore this system's driver-state folder, or have the KNX programmer replace the group keys in ETS before importing them again.", code="invalid_config")
        await asyncio.to_thread(journal.restore)
        return journal

    def _queue_cemi(self, raw):
        try:
            self._cemi_queue.put_nowait(bytes(raw))
        except asyncio.QueueFull:
            self._force_disconnect(code="no_response", message="KNX traffic exceeded the receive queue. The connection was closed to keep feedback and security counters consistent.")

    async def _receive_cemi(self):
        while self._knx:
            raw = await self._cemi_queue.get()
            try:
                await self._knx.cemi_handler.receive(raw)
                self.set_state("security_errors", self._knx.connection_manager.undecoded_data_secure)
            except (OSError, sqlite3.Error, ValueError, CommunicationError, CouldNotParseTelegram):
                self._force_disconnect(code="invalid_config", message="KNX secure traffic could not be processed or its replay history saved. Check storage and the ETS key export.")
                return
            finally:
                self._cemi_queue.task_done()

    async def _load_keys(self):
        encoded = self.config.get("keyring_data", "")
        if not encoded:
            return None
        password = str(self.config.get("keyring_password", ""))
        self.redact_in_log(password)
        try:
            data = base64.b64decode(encoded, validate=True)
            return await asyncio.to_thread(read_keyring, data, password)
        except (ValueError, InvalidSecureConfiguration) as exc:
            raise ConnectionFaultError("The saved security keys could not be opened. Import the ETS key export with its password again.", code="auth_failed") from exc

    def _connection_settings(self, descriptor):
        mode = self.config.get("mode", "auto")
        password = str(self.config.get("user_password", ""))
        key_tunnels = self._keyring.get_tunnel_interfaces_by_host(descriptor.individual_address) if self._keyring else []
        secured = descriptor.tunnelling_requires_secure or bool(password) or bool(key_tunnels)
        if mode == "auto":
            mode = "secure" if secured else ("tcp" if descriptor.supports_tunnelling_tcp else "udp")
        if secured and mode != "secure":
            raise ConnectionFaultError("This interface or its saved credentials requires Secure TCP. Choose Automatic or Secure TCP.", code="invalid_config")
        secure = SecureConfig(keyring=self._keyring)
        if mode == "secure":
            self._fill_credentials(secure, descriptor, password)
        self._check_group_keys()
        return mode, secure

    def _fill_credentials(self, secure, descriptor, password):
        if self._keyring and not password:
            selected = self._select_tunnel(descriptor)
            secure.user_id = selected.user_id
            secure.user_password = selected.decrypted_password
            secure.device_authentication_password = selected.decrypted_authentication
        else:
            secure.user_id = int(self.config.get("user_id", 2))
            secure.user_password = password
            secure.device_authentication_password = self.config.get("device_password") or None
        for name, secret in (("tunnel", secure.user_password), ("device authentication", secure.device_authentication_password)):
            if not secret:
                raise ConnectionFaultError(f"The {name} password is missing. Import the ETS security keys or enter the password.", code="auth_failed")
            self.redact_in_log(secret)

    def _select_tunnel(self, descriptor):
        try:
            return KNXIPInterface._get_tunnel_interface_from_keyring(self._keyring, descriptor)
        except InvalidSecureConfiguration as exc:
            raise ConnectionFaultError("No available tunnel has matching keys. Check the interface's tunnel slots and export its keys from ETS.", code="invalid_config") from exc

    def _check_group_keys(self):
        keys = self._keyring.get_data_secure_group_keys() if self._keyring else {}
        required = {row["address"] for row in self.config.get("addresses", []) if row.get("secure")}
        used = {row[direction] for row in self._rows.values() for direction in ("read", "write") if row[direction]}
        missing = required.intersection(used) - {address_text(key) for key in keys}
        if missing:
            raise ConnectionFaultError("Some configured addresses require Data Secure keys. Import a current ETS key export before controlling them.", code="invalid_config")

    def _link_alive(self):
        return self._knx is not None and self._knx.connection_manager.connected.is_set()

    def _connection_changed(self, state):
        if self._closing or not self._connected:
            return
        if state.name == "DISCONNECTED":
            self._invalidate()
            self._force_disconnect(code="transport_disconnected", message="The KNX tunnel disconnected.")

    async def _initial_sync(self):
        self._invalidate()
        self.set_state("security_errors", self._knx.connection_manager.undecoded_data_secure)
        self._start_refresh()
        self.spawn_task(self._reconcile())

    async def _close_session(self):
        self._closing = True
        self._invalidate()
        self.set_state("security_errors", None)
        knx, self._knx = self._knx, None
        self._refresh_task = None
        if knx:
            await knx.stop()

    async def refresh_children(self):
        self._rebuild()
        if self.connected:
            self._start_refresh()

    def _publish_roster(self):
        grouped = defaultdict(list)
        for (kind, entity, _), row in self._rows.items():
            grouped[kind, entity].append(row)
        for kind, entity in self._roster_ids - set(grouped):
            self.deregister_child(kind, entity)
        for (kind, entity), rows in grouped.items():
            schema = dict(COMMON_STATE)
            for row in rows:
                schema.update(binding_schema(row))
            initial = {"name": rows[0]["name"], "room": rows[0].get("room", ""),
                       "feedback": "Awaiting status", "last_received": None}
            if self.is_child_registered(kind, entity):
                self.deregister_child(kind, entity)
            self.register_child(kind, entity, schema=schema, initial_state=initial)
        self._roster_ids = set(grouped)
        # These are configured functions, not discoverable physical slots.
        # Let the platform carry their availability with the tunnel; the
        # individual *_valid values separately say whether status arrived.
        self.set_state("connected", self.connected)
        self.set_state("control_count", len(grouped))
        self._invalidate()

    def _publish_options(self):
        groups = self.config.get("addresses", [])
        by_type = defaultdict(list)
        for row in groups:
            option = {"value": row["address"], "label": f"{row['name']} — {row['address']} ({row.get('dpt') or 'type not set'})"}
            by_type[row.get("dpt", "")].append(option)
        for dpt in ("1.001", "5.001", "1.007", "1.008", "3.007"):
            self.set_state("addresses_" + dpt.replace(".", "_"), json.dumps(by_type[dpt]))
        self.set_state("sensor_addresses", json.dumps([option for dpt in SENSOR_TYPES for option in by_type[dpt]]))
        self.set_state("scene_addresses", json.dumps(by_type["17.001"] + by_type["18.001"]))
        self.set_state("address_count", len(groups))

    def _invalidate(self):
        self._seen.clear()
        for (kind, entity, prop), row in self._rows.items():
            if prop in ("recall", "dim", "move", "step_stop"):
                continue
            self.set_child_state_batch(kind, entity, {prop: None, prop + "_valid": False})
        for kind, entity in self._roster_ids:
            self.set_child_state_batch(kind, entity, {"feedback": self._feedback_text(kind, entity), "last_received": None})
        self.set_state("awaiting_status", len(self._feedback))

    def _feedback_text(self, kind, entity):
        reads = {row["read"] for key, row in self._rows.items() if key[:2] == (kind, entity) and row["read"]}
        if not reads:
            return "No status address configured"
        count = len(reads - self._seen)
        return f"Awaiting {count} status address(es)" if count else "Status received"

    def _telegram(self, telegram):
        if telegram.direction is not TelegramDirection.INCOMING:
            return
        if telegram.source_address == self._knx.current_address:
            return  # own bus echo is never measured feedback
        if not isinstance(telegram.payload, (GroupValueWrite, GroupValueResponse)):
            return
        address = address_text(telegram.destination_address)
        for key in self._feedback.get(address, []):
            self._apply_feedback(key, telegram.payload.value)

    def _apply_feedback(self, key, payload):
        row = self._rows[key]
        try:
            value = decode_value(row["dpt"], payload)
        except (ValueError, ConversionError, CouldNotParseTelegram):
            return
        self._seen.add(row["read"])
        kind, entity, prop = key
        self.set_child_state_batch(kind, entity, {
            prop: value, prop + "_valid": True, "last_received": time.time(),
            "feedback": self._feedback_text(kind, entity),
        })
        self.set_state("awaiting_status", len(set(self._feedback) - self._seen))

    def _start_refresh(self):
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = self.spawn_task(self._refresh())

    async def _refresh(self):
        for address in self._feedback:
            if not self._link_alive():
                return
            await self._send(address, GroupValueRead())

    async def _reconcile(self):
        while self._link_alive():
            await asyncio.sleep(int(self.config.get("reconcile_seconds", 300)))
            self._start_refresh()
            self.set_state("security_errors", self._knx.connection_manager.undecoded_data_secure)

    async def _send(self, address, payload):
        async with self._send_lock:
            if not self._link_alive():
                raise CommandRefusedError("The KNX interface is offline.")
            try:
                await self._knx.cemi_handler.send_telegram(Telegram(GroupAddress(address), payload=payload))
            except CommunicationError:
                self._invalidate()
                self._force_disconnect(code="no_response", message="The KNX interface did not confirm transmission. Reconnecting before sending more commands.")
                raise
            except (OSError, sqlite3.Error):
                self._invalidate()
                self._force_disconnect(code="invalid_config", message="The Data Secure history could not be saved. Check this system's storage before reconnecting.")
                raise
            await asyncio.sleep(0.1)  # at most ten group telegrams per second

    async def send_command(self, command, params=None):
        params = params or {}
        if command == "refresh":
            self._start_refresh()
            return {"requested": len(self._feedback)}
        key = command_target(command, params)
        row = self._rows.get(key)
        if not row or not row["write"]:
            raise CommandRefusedError("This control has no command address configured.")
        payload = encode_value(row, params.get("value"))
        await self._send(row["write"], GroupValueWrite(payload))
        addresses = self._status_addresses(key[:2])
        for address in sorted(addresses):
            await self._send(address, GroupValueRead())
        return {"accepted": True, "feedback": "Reported separately from received status telegrams"}

    def _status_addresses(self, target):
        return {row["read"] for key, row in self._rows.items() if key[:2] == target and row["read"]}

    async def run_setup_action(self, action_id, params, progress):
        handlers = {
            "import_project": self._import_project, "import_keys": self._import_keys,
            "find_interfaces": self._find_interfaces, "select_interface": self._select_interface,
        }
        if action_id in handlers:
            return await handlers[action_id](params, progress)
        if action_id in ("add_light", "add_shade", "add_sensor", "add_scene"):
            return await self._add_control(action_id[4:], params, progress)
        raise ValueError("Unknown setup action.")

    async def _save_mappings(self, delta):
        rows = validate_bindings(delta.get("bindings", self.config.get("bindings", [])))
        check_mapping_types(rows, delta.get("addresses", self.config.get("addresses", [])))
        await self.request_config_update(delta)
        self._rebuild()
        if self._link_alive():
            await self.request_reconnect()

    async def _import_project(self, params, progress):
        file = params["project"]
        password = str(params.get("password", ""))
        self.redact_in_log(password)
        await progress("Reading the ETS project", 10)
        result = await asyncio.to_thread(import_project, await file.read(), file.name, password)
        current = validate_bindings(self.config.get("bindings", []))
        imported = validate_bindings(result["bindings"])
        merged = {**imported, **current}  # existing mappings and names win
        groups = {row["address"]: row for row in self.config.get("addresses", [])}
        groups.update({row["address"]: row for row in result["addresses"]})
        await progress("Saving names and explicit mappings", 70)
        await self._save_mappings({"addresses": list(groups.values()), "bindings": list(merged.values())})
        count = len({key[:2] for key in imported} - {key[:2] for key in current})
        return {"message": f"Imported {len(result['addresses'])} named addresses and added {count} controls. Existing controls were kept. Use Add or Update for addresses without explicit function mappings. Secure groups need the matching ETS security-key export."}

    async def _import_keys(self, params, progress):
        data = await params["keys"].read()
        password = str(params.get("password", ""))
        self.redact_in_log(password)
        await progress("Checking the key export and password", 15)
        try:
            keyring = await asyncio.to_thread(read_keyring, data, password)
        except InvalidSecureConfiguration as exc:
            raise ValueError("The key export or password was refused. Export current keys from ETS and try again.") from exc
        context = self.config.get("security_context") or uuid.uuid4().hex
        if keyring.get_data_secure_group_keys():
            await self._prepare_key_history(context, keyring)
        await self.request_config_update({"keyring_data": base64.b64encode(data).decode(), "keyring_password": password,
                                          "security_context": context})
        await progress("Security keys saved", 80)
        if self.config.get("host"):
            await self.request_reconnect()
        return {"message": f"Imported {len(keyring.interfaces)} interfaces and {len(keyring.get_data_secure_group_keys())} group keys. Secrets are stored on this OpenAVC system."}

    async def _prepare_key_history(self, context, keyring):
        simulation = bool(self.state.get("system.simulation_active"))
        journal = CounterJournal(context, keyring, simulation=simulation)
        missing_history = self.config.get("security_context") and not journal.path.is_file()
        if missing_history and not simulation:
            previous = await self._load_keys()
            previous_keys = previous.get_data_secure_group_keys() if previous else {}
            reused = set(previous_keys.values()).intersection(keyring.get_data_secure_group_keys().values())
            if reused:
                raise ValueError("These group keys still need their saved replay history. Restore the driver-state folder, or ask the KNX programmer to replace the group keys in ETS before exporting them.")
        await asyncio.to_thread(journal.restore)

    async def _find_interfaces(self, params, progress):
        await progress("Looking for KNX/IP interfaces", 10)
        if self.state.get("system.simulation_active"):
            raise ValueError("Network discovery is not used during simulation.")
        scanner = GatewayScanner(XKNX(), local_ip=self.local_address("224.0.23.12"), timeout_in_seconds=3)
        gateways = await scanner.scan()
        options = [{"value": f"{gateway.ip_addr}:{gateway.port}",
                    "label": f"{gateway.name} — {gateway.ip_addr} ({'secure' if gateway.tunnelling_requires_secure else 'tunnel'})"}
                   for gateway in gateways if gateway.supports_tunnelling]
        self.set_state("interfaces", json.dumps(options))
        return {"message": f"Found {len(options)} interfaces. Use Select Interface to choose one. If this network blocks multicast, enter the interface address in device settings."}

    async def _select_interface(self, params, progress):
        if self.state.get("system.simulation_active"):
            raise ValueError("Interface selection is not used during simulation.")
        value = str(params.get("interface", ""))
        options = json.loads(self.get_state("interfaces") or "[]")
        if value not in {option["value"] for option in options}:
            raise ValueError("Choose one of the interfaces returned by Find Interfaces.")
        host, port = value.rsplit(":", 1)
        await self.request_config_update({"host": host, "port": int(port)})
        await self.request_reconnect()
        return {"message": "Connected to the selected interface."}

    async def _add_control(self, kind, params, progress):
        rows = list(self._rows.values())
        entity = str(params.get("existing") or uuid.uuid4().hex)
        existing = [row for key, row in self._rows.items() if key[:2] == (kind, entity)]
        if params.get("existing") and not existing:
            raise ValueError("The selected control no longer exists. Choose it again.")
        common = {"kind": kind, "entity": entity, "name": str(params.get("name", "")).strip(),
                  "room": str(params.get("room", "")), "scene": 1}
        added = make_control_rows(common, params, existing, self.config.get("addresses", []))
        rows = [row for key, row in self._rows.items() if key[:2] != (kind, entity)] + added
        await progress("Saving the control mappings", 50)
        await self._save_mappings({"bindings": rows})
        return {"message": f"Saved {params['name']}. Only received status updates its displayed values."}


def command_target(command, params):
    if command == "recall_scene":
        return ("scene", str(params.get("scene", "")), "recall")
    if command in ("set_light", "set_shade"):
        return (command[4:], str(params.get("target", "")), str(params.get("property", "")))
    raise CommandRefusedError("Unknown KNX command.")


def make_control_rows(common, params, existing, addresses):
    rows = {row["property"]: {**row, **common} for row in existing}
    if common["kind"] in ("sensor", "scene"):
        return single_control_row(common["kind"], common, params, addresses)
    chosen = {key: value for key, value in params.items() if key.endswith(("_read", "_write"))}
    for param, value in chosen.items():
        apply_mapping(rows, common, (*param.rsplit("_", 1), value), addresses)
    if not rows:
        raise ValueError("Choose at least one command or status address.")
    return list(rows.values())


def apply_mapping(rows, common, mapping, addresses):
    prop, direction, value = mapping
    if not value:
        return
    dpt = Bindings.TYPES[common["kind"]].get(prop)
    if not dpt:
        raise ValueError("Unknown control mapping.")
    address = checked_address(value, dpt[0], addresses)
    row = rows.setdefault(prop, {**common, "property": prop, "dpt": dpt[0], "read": "", "write": ""})
    row[direction] = address


def checked_address(value, dpt, addresses):
    address = address_text(value)
    imported = next((row for row in addresses if row["address"] == address), None)
    if not imported or not imported.get("dpt"):
        return address
    if imported["dpt"] != dpt:
        raise ValueError(f"{imported['name']} uses datatype {imported['dpt']}, not {dpt}.")
    return address


def single_control_row(kind, common, params, addresses):
    address = address_text(params.get("address", ""))
    imported = next((row for row in addresses if row["address"] == address), {})
    dpt = imported.get("dpt") or str(params.get("dpt", ""))
    if not dpt:
        raise ValueError("Choose the datapoint type supplied by the KNX programmer.")
    return [{**common, "property": "value" if kind == "sensor" else "recall", "dpt": dpt,
             "read": address if kind == "sensor" else "", "write": address if kind == "scene" else "",
             "scene": int(params.get("scene", 1))}]
