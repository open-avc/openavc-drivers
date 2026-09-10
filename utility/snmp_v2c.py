"""SNMP v2c generic driver.

SNMP is how rack and infrastructure equipment exposes itself: switched PDUs,
UPSes, managed switches, environmental sensors, and the displays and projectors
that ship an enterprise MIB. There are no commands in the AV sense — the
device's control surface is a set of numbered values (OIDs), and which ones
matter is a property of the device, not of the protocol.

So this is a GENERIC. The integrator declares the OIDs from the manufacturer's
MIB in two ``type: table`` config fields and the driver builds its state
variables, commands, device settings and child entities per device at init:

  ``oid_map``    scalar OIDs — one OID, one value. Read rows become status
                 values, write rows become commands, read/write rows become
                 device settings with the offline pending queue.
  ``table_map``  columns of a MIB table — one row per column, grouped by
                 ``child_type``. The column marked ``index`` is walked to
                 discover the table's rows, and each row becomes a child
                 entity carrying every column as a property. This is what an
                 outlet strip, a switch's ports or a sensor bank actually is,
                 and it means the integrator declares eight outlets as one row
                 rather than eight.

Whatever the integrator declares, the MIB-II system group is read on connect
without being declared, so a device identifies itself the moment it is added.

Python, not YAML, for two reasons ConfigurableDriver cannot cover: SNMP is a
Python-only transport (a request is a list of OIDs, not a send string), and the
schema is derived from config rather than declared in the driver file.

**Polling, and why that is not a Principle-2 violation.** SNMP's push channel is
the trap (an unsolicited datagram to UDP 162). The platform has no listener
shape for a unicast UDP port shared across devices and demuxed by source
address, so traps are deferred rather than dropped silently — see the pending
push tracker in the driver roadmap. Everything here is polled.

**v2c only.** Version 3 adds USM authentication and privacy, which the platform
transport does not implement.

Source:
  RFC 3416 — Version 2 of the Protocol Operations for SNMP
    https://www.rfc-editor.org/rfc/rfc3416
  RFC 3418 — Management Information Base (MIB) for SNMP  (the system group)
    https://www.rfc-editor.org/rfc/rfc3418
"""

from __future__ import annotations

import asyncio
import copy
import re
from typing import Any

from openavc.drivers.base import BaseDriver
from openavc.transport.snmp import SnmpError
from openavc.utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_PORT = 161

# How many varbinds to put in one request. SNMP allows many, but a PDU that
# outgrows the path MTU is answered with tooBig (or dropped by a middlebox),
# and agents vary in what they accept. Sixteen is comfortably inside a 1500-byte
# datagram for realistic OID lengths and still cuts a 60-value poll to four
# round trips.
VARBINDS_PER_REQUEST = 16

# Ceiling on a column walk, so a malformed OID cannot poll forever. A table
# larger than this is a network switch's interface list, not AV equipment.
WALK_LIMIT = 512

# The MIB-II system group (RFC 3418). Every agent implements it, so it is read
# without being declared: a device that has just been added shows what it is
# before the integrator has typed a single OID.
SYSTEM_GROUP: dict[str, str] = {
    "sys_descr": "1.3.6.1.2.1.1.1.0",
    "sys_object_id": "1.3.6.1.2.1.1.2.0",
    "sys_uptime": "1.3.6.1.2.1.1.3.0",
    "sys_contact": "1.3.6.1.2.1.1.4.0",
    "sys_name": "1.3.6.1.2.1.1.5.0",
    "sys_location": "1.3.6.1.2.1.1.6.0",
}
SYS_DESCR_OID = SYSTEM_GROUP["sys_descr"]

# Value types an integrator may declare, in MIB spelling. These are the
# transport's own type names — the ones in the manufacturer's MIB file.
VALUE_TYPES = (
    "integer", "string", "gauge32", "counter32", "counter64",
    "timeticks", "ip_address", "oid",
)
NUMERIC_TYPES = frozenset({
    "integer", "gauge32", "counter32", "counter64", "timeticks",
})

OID_PATTERN = r"^\.?\d+(\.\d+)+$"

# Columns shared by both tables: what the value is and what may be done to it.
_VALUE_COLUMNS: dict[str, Any] = {
    "type": {
        "type": "enum", "label": "Type", "default": "integer",
        "values": list(VALUE_TYPES),
        "help": "The SNMP type from the MIB. Used as declared when writing; a "
                "read takes whatever the device answers with.",
    },
    "access": {
        "type": "enum", "label": "Access", "default": "r",
        "values": [
            {"value": "r", "label": "Read (status value)"},
            {"value": "w", "label": "Write (command)"},
            {"value": "rw", "label": "Read/Write (device setting)"},
        ],
    },
    "states": {
        "type": "string", "label": "States",
        "help": "Optional. Name the values of an enumerated integer, as "
                "1=On,2=Off. The value then reads as its name, and writing it "
                "offers a list instead of a number.",
    },
    "scale": {
        "type": "number", "label": "Scale", "default": 1,
        "help": "Engineering value = raw x scale + offset. A reading in "
                "tenths of an amp uses 0.1.",
    },
    "offset": {"type": "number", "label": "Offset", "default": 0},
    "unit": {
        "type": "string", "label": "Unit",
        "help": "Display unit, e.g. A, W, C, % (optional).",
    },
}

OID_MAP_COLUMNS: dict[str, Any] = {
    "name": {
        "type": "string", "label": "Name", "required": True,
        "help": "The status value / command / setting id this OID becomes.",
    },
    "oid": {
        "type": "string", "label": "OID", "required": True,
        "pattern": OID_PATTERN,
        "help": "Full dotted OID including the instance suffix — a scalar "
                "usually ends in .0, as in 1.3.6.1.2.1.1.5.0.",
    },
    **_VALUE_COLUMNS,
}

TABLE_MAP_COLUMNS: dict[str, Any] = {
    "child_type": {
        "type": "string", "label": "Group", "required": True,
        "help": "What one row of this table is, singular and lowercase: "
                "outlet, port, sensor, battery. Columns sharing a group "
                "become one set of child entities.",
    },
    "name": {
        "type": "string", "label": "Column", "required": True,
        "help": "The property name this column becomes. A column named "
                "'label' is used as the row's name in the interface.",
    },
    "oid": {
        "type": "string", "label": "Column OID", "required": True,
        "pattern": OID_PATTERN,
        "help": "The column's base OID, WITHOUT a row index — the driver "
                "appends each row's index to it.",
    },
    "index": {
        "type": "boolean", "label": "Index Column", "default": False,
        "help": "Tick one column per group. That column is walked to find out "
                "which rows exist; the rest are read at those rows.",
    },
    **_VALUE_COLUMNS,
}


def _sanitize(name: str, fallback: str) -> str:
    """Turn a user-typed name into a safe state / command / child key."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(name).strip()).strip("_")
    return cleaned or fallback


def _normalize_oid(oid: str) -> str:
    """Strip a leading dot; MIB browsers print one and the wire has none."""
    return str(oid).strip().lstrip(".")


def parse_states(spec: Any) -> dict[int, str]:
    """Parse a ``1=On,2=Off`` states column into {value: label}.

    Tolerant on purpose: this is hand- and AI-authored config, and one
    malformed pair should cost that pair, not the whole row.
    """
    out: dict[int, str] = {}
    if not spec:
        return out
    for pair in str(spec).split(","):
        if "=" not in pair:
            continue
        raw, label = pair.split("=", 1)
        label = label.strip()
        try:
            value = int(raw.strip())
        except ValueError:
            continue
        if label:
            out[value] = label
    return out


def _parse_common(raw: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The half of a row that both tables share. None if it can't be used."""
    oid = _normalize_oid(raw.get("oid", ""))
    if not re.match(OID_PATTERN, "." + oid if not oid.startswith(".") else oid):
        log.warning("snmp row %r: %r is not an OID, skipping", name, raw.get("oid"))
        return None
    vtype = str(raw.get("type", "integer")).strip().lower()
    if vtype not in VALUE_TYPES:
        log.warning("snmp row %r: unknown type %r, using integer", name, vtype)
        vtype = "integer"
    access = str(raw.get("access", "r")).strip().lower()
    if access not in ("r", "w", "rw"):
        access = "r"
    try:
        scale = float(raw.get("scale", 1) or 1)
    except (TypeError, ValueError):
        scale = 1.0
    try:
        offset = float(raw.get("offset", 0) or 0)
    except (TypeError, ValueError):
        offset = 0.0
    states = parse_states(raw.get("states"))
    if states and vtype not in NUMERIC_TYPES:
        log.warning("snmp row %r: states only apply to a numeric type, ignoring", name)
        states = {}
    return {
        "name": name,
        "oid": oid,
        "type": vtype,
        "access": access,
        "scale": scale,
        "offset": offset,
        "unit": str(raw.get("unit", "") or "").strip(),
        "states": states,
        "labels": {label: value for value, label in states.items()},
    }


def parse_oid_map(rows: Any) -> list[dict[str, Any]]:
    """Normalize the scalar OID table into validated specs."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(rows, list):
        return out
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        if not str(raw.get("name", "")).strip():
            log.warning("snmp OID row with no name skipped")
            continue
        name = _sanitize(raw.get("name"), "value")
        spec = _parse_common(raw, name)
        if spec is None:
            continue
        if name in seen:
            log.warning("snmp duplicate name %r: keeping the last", name)
            out = [r for r in out if r["name"] != name]
        seen.add(name)
        out.append(spec)
    return out


def parse_table_map(rows: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize the table-column rows into {child_type: [column specs]}.

    The index column is moved to the front of its group, so the walk column is
    always ``columns[0]`` and nothing downstream has to search for it. A group
    whose author ticked no index column uses its first column, which is the
    reading that makes a half-filled table work rather than fail.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(rows, list):
        return grouped
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        child_type = _sanitize(raw.get("child_type", ""), "").lower()
        if not child_type:
            log.warning("snmp table row with no group skipped")
            continue
        if not str(raw.get("name", "")).strip():
            log.warning("snmp table row in %r with no column name skipped", child_type)
            continue
        name = _sanitize(raw.get("name"), "value")
        spec = _parse_common(raw, name)
        if spec is None:
            continue
        spec["index"] = bool(raw.get("index"))
        columns = grouped.setdefault(child_type, [])
        if any(c["name"] == name for c in columns):
            log.warning("snmp duplicate column %r in %r: keeping the last",
                        name, child_type)
            columns[:] = [c for c in columns if c["name"] != name]
        columns.append(spec)

    for child_type, columns in grouped.items():
        indexes = [c for c in columns if c["index"]]
        if len(indexes) > 1:
            log.warning("snmp group %r marks %d index columns: using %r",
                        child_type, len(indexes), indexes[0]["name"])
        chosen = indexes[0] if indexes else columns[0]
        columns.remove(chosen)
        columns.insert(0, chosen)
    return grouped


def _state_type(spec: dict[str, Any]) -> str:
    """The state-variable type a column or scalar publishes."""
    if spec["states"]:
        return "string"
    if spec["type"] not in NUMERIC_TYPES:
        return "string"
    if spec["scale"] != 1 or spec["offset"] != 0:
        return "number"
    return "integer"


class SNMPv2cDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "snmp_v2c",
        "name": "SNMP Device (v2c)",
        "manufacturer": "Generic",
        "category": "utility",
        "version": "1.0.0",
        "author": "OpenAVC",
        "description": "Read and control any SNMP v2c device by declaring the "
                       "OIDs from its MIB.",
        "source_url": "https://www.rfc-editor.org/rfc/rfc3416",
        "simulated": True,
        # transport: snmp is the floor the contract computes — an older
        # platform answers "Unsupported transport type" and the device never
        # connects.
        "min_platform_version": "0.34.0",
        "transport": "snmp",
        "protocols": ["snmp"],
        "default_config": {
            "host": "",
            "port": DEFAULT_PORT,
            "community": "public",
            "write_community": "",
            "timeout": 2.0,
            "retries": 1,
            "poll_interval": 30,
            "oid_map": [],
            "table_map": [],
        },
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address",
                     "description": "The device's IP address or hostname."},
            "port": {"type": "integer", "default": DEFAULT_PORT, "label": "Port",
                     "min": 1, "max": 65535,
                     "description": "SNMP port. 161 unless the device was moved."},
            "community": {"type": "string", "default": "public",
                          "label": "Read Community", "secret": True,
                          "description": "The community string that may read. "
                                         "Devices ship with 'public'."},
            "write_community": {"type": "string", "label": "Write Community",
                                "secret": True,
                                "description": "The community string that may write, "
                                               "often 'private'. Leave blank to write "
                                               "with the read community."},
            "timeout": {"type": "number", "default": 2.0, "label": "Timeout (sec)",
                        "min": 0.5, "max": 30},
            "retries": {"type": "integer", "default": 1, "label": "Retries",
                        "min": 0, "max": 5,
                        "description": "Extra attempts when a request goes "
                                       "unanswered. SNMP runs over UDP, which "
                                       "drops datagrams without saying so."},
            "poll_interval": {"type": "integer", "default": 30,
                              "label": "Poll Interval (sec)", "min": 0,
                              "description": "How often to read the declared OIDs. "
                                             "0 disables polling."},
            "oid_map": {
                "type": "table",
                "label": "Values",
                "row_label": "OID",
                "help": "Declare each OID to read or write. Read rows become "
                        "status values, write rows become commands, read/write "
                        "rows become device settings.",
                "columns": OID_MAP_COLUMNS,
            },
            "table_map": {
                "type": "table",
                "label": "Tables",
                "row_label": "column",
                "help": "For a MIB table — outlets, ports, sensors. Declare one "
                        "row per column and give the columns of one table the "
                        "same group name. Every row of the table becomes a "
                        "child entity.",
                "columns": TABLE_MAP_COLUMNS,
            },
        },
        "state_variables": {
            "sys_descr": {"type": "string", "label": "Description"},
            "sys_object_id": {"type": "string", "label": "Object ID"},
            "sys_name": {"type": "string", "label": "System Name"},
            "sys_location": {"type": "string", "label": "Location"},
            "sys_contact": {"type": "string", "label": "Contact"},
            "sys_uptime": {"type": "integer", "label": "Uptime (sec)",
                           "unit": "s", "cloud_priority": "low"},
        },
        "commands": {},
        "device_settings": {},
        "actions": [
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection",
                "icon": "search",
                "availability": "always",
                "confirm": (
                    "Reads the device's own description and every value you "
                    "have declared, and reports what answered. Nothing on the "
                    "device changes."
                ),
            },
        ],
        "help": {
            "overview": "Controls any device that speaks SNMP v2c — switched "
                        "PDUs, UPSes, managed switches, environmental sensors, "
                        "and displays or projectors with a MIB. You declare the "
                        "OIDs from the manufacturer's MIB on the device page; "
                        "the driver turns read OIDs into live status values, "
                        "write OIDs into commands, and read/write OIDs into "
                        "device settings. A MIB table becomes one child entity "
                        "per row.",
            "setup": "Enter the IP address and the read community string "
                     "(devices ship with 'public'), then run Test Connection — "
                     "it reads the device's own description back, which "
                     "confirms both the address and the community string. Add "
                     "the write community only if you need to control the "
                     "device; it is usually a different string from the read "
                     "one. Then add the OIDs from the manufacturer's MIB.",
        },
    }

    # Liveness watchdog. SNMP runs over UDP: a send always "succeeds", so an
    # awaited read is the only thing that notices a device has gone.
    HEALTH_INTERVAL_S = 60.0
    HEALTH_TIMEOUT_S = 10.0
    HEALTH_MAX_FAILURES = 2

    def __init__(self, device_id: str, config: dict[str, Any], state, events):
        self._scalars = parse_oid_map(config.get("oid_map"))
        self._by_name = {s["name"]: s for s in self._scalars}
        self._columns = parse_table_map(config.get("table_map"))
        # command name -> the scalar it writes / (child_type, column) it writes.
        self._scalar_commands: dict[str, dict[str, Any]] = {}
        self._child_commands: dict[str, tuple[str, dict[str, Any]]] = {}
        # child_type -> {local_id: the raw OID index suffix for that row}
        self._child_index: dict[str, dict[Any, str]] = {}
        # Names already reported absent, so a device that lacks a declared OID
        # says so once rather than on every poll forever.
        self._absent: set[str] = set()
        self.DRIVER_INFO = self._build_driver_info()
        super().__init__(device_id, config, state, events)

    # ── Per-instance schema ──

    def _build_driver_info(self) -> dict[str, Any]:
        info = copy.deepcopy(type(self).DRIVER_INFO)
        state_vars = dict(info["state_variables"])
        commands: dict[str, Any] = {}
        settings: dict[str, Any] = {}

        for spec in self._scalars:
            name = spec["name"]
            if name in SYSTEM_GROUP:
                log.warning("snmp row %r shadows a system-group value, skipping", name)
                continue
            label = name.replace("_", " ").title()
            if spec["access"] in ("r", "rw"):
                state_vars[name] = self._state_var(spec, label)
            if spec["access"] == "w":
                cmd = f"set_{name}"
                commands[cmd] = {
                    "label": f"Set {label}",
                    "params": {"value": self._value_param(spec)},
                }
                self._scalar_commands[cmd] = spec
            if spec["access"] == "rw":
                settings[name] = self._setting(spec, label)

        child_types: dict[str, Any] = {}
        for child_type, columns in self._columns.items():
            props: dict[str, Any] = {}
            for column in columns:
                if column["access"] in ("r", "rw"):
                    props[column["name"]] = self._state_var(
                        column, column["name"].replace("_", " ").title()
                    )
                if column["access"] in ("w", "rw"):
                    cmd = f"set_{child_type}_{column['name']}"
                    commands[cmd] = {
                        "label": f"Set {child_type.title()} "
                                 f"{column['name'].replace('_', ' ').title()}",
                        "params": {
                            "child_id": {
                                "type": "child_id", "required": True,
                                "child_type": child_type,
                                "label": child_type.title(),
                            },
                            "value": self._value_param(column),
                        },
                    }
                    self._child_commands[cmd] = (child_type, column)
            label = child_type.replace("_", " ").title()
            declared = {
                "label": label,
                "label_plural": f"{label}s",
                "id_format": {"type": "string", "max_length": 64},
                "state_variables": props,
                "summary_fields": [n for n in list(props)[:3]],
            }
            if "label" in props:
                declared["label_field"] = "label"
            child_types[child_type] = declared

        info["state_variables"] = state_vars
        info["commands"] = commands
        info["device_settings"] = settings
        if child_types:
            info["child_entity_types"] = child_types
        return info

    @staticmethod
    def _state_var(spec: dict[str, Any], label: str) -> dict[str, Any]:
        var: dict[str, Any] = {
            "type": _state_type(spec), "label": label, "control": True,
        }
        if spec["unit"]:
            var["unit"] = spec["unit"]
        if spec["type"] in ("counter32", "counter64"):
            # A counter climbs forever and nobody watches it live.
            var["cloud_priority"] = "low"
        return var

    @staticmethod
    def _value_param(spec: dict[str, Any]) -> dict[str, Any]:
        """The parameter a write command takes. An enumerated OID offers its
        names, so nobody has to remember that 2 means off."""
        if spec["states"]:
            return {
                "type": "enum", "required": True, "label": "Value",
                "values": [
                    {"value": label, "label": label}
                    for label in spec["states"].values()
                ],
            }
        if spec["type"] in NUMERIC_TYPES:
            ptype = "number" if (spec["scale"] != 1 or spec["offset"] != 0) else "integer"
            param: dict[str, Any] = {"type": ptype, "required": True, "label": "Value"}
            if spec["unit"]:
                param["label"] = f"Value ({spec['unit']})"
            return param
        return {"type": "string", "required": True, "label": "Value"}

    @staticmethod
    def _setting(spec: dict[str, Any], label: str) -> dict[str, Any]:
        setting: dict[str, Any] = {
            "label": label, "state_key": spec["name"], "setup": False,
        }
        if spec["states"]:
            setting["type"] = "enum"
            setting["values"] = [
                {"value": name, "label": name} for name in spec["states"].values()
            ]
            setting["default"] = next(iter(spec["states"].values()))
        elif _state_type(spec) == "integer":
            setting["type"] = "integer"
            setting["default"] = 0
        elif _state_type(spec) == "number":
            setting["type"] = "number"
            setting["default"] = 0
        else:
            setting["type"] = "string"
            setting["default"] = ""
        if spec["unit"]:
            setting["help"] = f"Value in {spec['unit']}."
        return setting

    # ── Value conversion ──

    def _to_state(self, spec: dict[str, Any], value: Any) -> Any:
        """Turn what the agent answered into what the driver publishes."""
        if spec["states"]:
            try:
                return spec["states"].get(int(value), str(value))
            except (TypeError, ValueError):
                return str(value)
        if spec["type"] not in NUMERIC_TYPES:
            return str(value)
        try:
            scaled = float(value) * spec["scale"] + spec["offset"]
        except (TypeError, ValueError):
            return str(value)
        return int(scaled) if _state_type(spec) == "integer" else scaled

    def _to_wire(self, spec: dict[str, Any], value: Any) -> Any:
        """Turn what the user asked for into what goes on the wire."""
        if spec["states"]:
            if value in spec["labels"]:
                return spec["labels"][value]
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{value!r} is not one of: "
                    f"{', '.join(spec['states'].values())}"
                ) from exc
        if spec["type"] not in NUMERIC_TYPES:
            return str(value)
        raw = (float(value) - spec["offset"]) / spec["scale"]
        return int(round(raw))

    # ── Connection lifecycle ──

    async def _initial_sync(self) -> None:
        """Identify the device, discover its tables, and take a first reading,
        so the card is populated before the first poll comes round."""
        try:
            await self._read_system_group()
            await self._reconcile_children()
            await self.poll()
        except (ConnectionError, SnmpError, asyncio.TimeoutError) as e:
            log.warning("[%s] Initial SNMP sync failed: %s", self.device_id, e)

    async def _read_system_group(self) -> None:
        answered = await self.transport.get(list(SYSTEM_GROUP.values()))
        updates: dict[str, Any] = {}
        for name, oid in SYSTEM_GROUP.items():
            varbind = answered.get(oid)
            if varbind is None or varbind.is_exception:
                continue
            if name == "sys_uptime":
                # TimeTicks are hundredths of a second; nobody wants that unit.
                try:
                    updates[name] = int(varbind.value) // 100
                except (TypeError, ValueError):
                    continue
            else:
                updates[name] = str(varbind.value)
        if updates:
            self.set_states(updates)

    # ── Children (MIB tables) ──

    async def _reconcile_children(self) -> None:
        """Walk each declared table's index column and register a child per row.

        A row that has gone (an outlet on a card somebody pulled) is
        deregistered; the walk is the roster and the device is the authority.
        """
        for child_type, columns in self._columns.items():
            index_column = columns[0]
            rows = await self.transport.walk(index_column["oid"], limit=WALK_LIMIT)
            prefix = index_column["oid"] + "."
            seen: dict[Any, str] = {}
            for varbind in rows:
                suffix = varbind.oid[len(prefix):]
                if not suffix:
                    continue
                local_id = self._local_id(suffix)
                seen[local_id] = suffix
                initial: dict[str, Any] = {"online": True}
                if index_column["access"] in ("r", "rw"):
                    initial[index_column["name"]] = self._to_state(
                        index_column, varbind.value
                    )
                self.register_child(child_type, local_id, initial_state=initial)
            known = self._child_index.setdefault(child_type, {})
            for stale in set(known) - set(seen):
                self.deregister_child(child_type, stale)
            known.clear()
            known.update(seen)

    @staticmethod
    def _local_id(suffix: str) -> str:
        """A table row's OID suffix as a child id.

        Always a string, even when it looks like a number. A MIB index is not
        an integer in general — entPhysical rows, an ipAddrTable keyed by
        address, and any table with a composite index all carry several
        components — and a type declared at init cannot be narrowed once the
        walk finds one. A numeric index still reads as itself
        (``outlet.3.state``); the cost is that the IDE lists row 10 before
        row 2, which is the trade for a driver that does not simply refuse
        half the tables in a real MIB.
        """
        return suffix.replace(".", "_")

    async def refresh_children(self) -> dict[str, Any]:
        """The IDE's Refresh from Device button."""
        await self._reconcile_children()
        return {
            child_type: len(self.list_children(child_type))
            for child_type in self._columns
        }

    # ── Polling ──

    async def poll(self) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self._poll_scalars()
        await self._poll_children()

    async def _poll_scalars(self) -> None:
        readable = [s for s in self._scalars if s["access"] in ("r", "rw")]
        if not readable:
            # Nothing declared yet: keep uptime moving so the card is alive and
            # the link is proven by something.
            await self._read_system_group()
            return
        by_oid = {s["oid"]: s for s in readable}
        updates: dict[str, Any] = {}
        for chunk in _chunks(list(by_oid), VARBINDS_PER_REQUEST):
            answered = await self._get(chunk)
            for oid, varbind in answered.items():
                spec = by_oid.get(oid)
                if spec is None:
                    continue
                if varbind.is_exception:
                    self._report_absent(spec["name"], varbind.type)
                    continue
                updates[spec["name"]] = self._to_state(spec, varbind.value)
        if updates:
            self.set_states(updates)
        await self._read_system_group()

    async def _poll_children(self) -> None:
        batch: list[tuple[str, Any, dict[str, Any]]] = []
        for child_type, columns in self._columns.items():
            readable = [c for c in columns if c["access"] in ("r", "rw")]
            if not readable:
                continue
            index = self._child_index.get(child_type, {})
            # One request carries several children's several columns, so a
            # 24-outlet strip with four columns is six round trips, not 96.
            wanted: list[str] = []
            owner: dict[str, tuple[Any, dict[str, Any]]] = {}
            for local_id, suffix in index.items():
                for column in readable:
                    oid = f"{column['oid']}.{suffix}"
                    wanted.append(oid)
                    owner[oid] = (local_id, column)
            per_child: dict[Any, dict[str, Any]] = {}
            for chunk in _chunks(wanted, VARBINDS_PER_REQUEST):
                answered = await self._get(chunk)
                for oid, varbind in answered.items():
                    if oid not in owner:
                        continue
                    local_id, column = owner[oid]
                    if varbind.is_exception:
                        continue
                    per_child.setdefault(local_id, {})[column["name"]] = (
                        self._to_state(column, varbind.value)
                    )
            for local_id, props in per_child.items():
                batch.append((child_type, local_id, props))
        if batch:
            self.set_children_state_batch(batch)

    async def _get(self, oids: list[str]) -> dict[str, Any]:
        """One GET, with a timeout turned into the platform's offline path.

        An `SnmpError` here is the agent refusing the whole request — a
        community string that may not see one of these OIDs. That is worth a
        log line and a stale value, not an offline device.
        """
        try:
            return await self.transport.get(oids)
        except SnmpError as e:
            log.warning("[%s] SNMP read refused: %s", self.device_id, e)
            return {}
        except asyncio.TimeoutError as e:
            raise ConnectionError(
                f"SNMP device {self.config.get('host')} did not answer"
            ) from e

    def _report_absent(self, name: str, kind: str) -> None:
        if name in self._absent:
            return
        self._absent.add(name)
        log.warning(
            "[%s] %r: the device has no such OID (%s). Check it against the "
            "manufacturer's MIB — the value will stay blank.",
            self.device_id, name, kind,
        )

    # ── Writes ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        spec = self._scalar_commands.get(command)
        if spec is not None:
            if "value" not in params:
                raise ValueError(f"{command} requires a 'value' parameter")
            confirmed = await self._write(spec, spec["oid"], params["value"])
            if spec["name"] in self.DRIVER_INFO["state_variables"]:
                self.set_state(spec["name"], confirmed)
            return confirmed

        child = self._child_commands.get(command)
        if child is not None:
            child_type, column = child
            local_id = params.get("child_id")
            if local_id is None:
                raise ValueError(f"{command} requires a 'child_id' parameter")
            if "value" not in params:
                raise ValueError(f"{command} requires a 'value' parameter")
            suffix = self._suffix_for(child_type, local_id)
            confirmed = await self._write(
                column, f"{column['oid']}.{suffix}", params["value"]
            )
            if column["access"] in ("r", "rw"):
                self.set_child_state(
                    child_type, self._local_id(suffix), column["name"], confirmed
                )
            return confirmed

        raise ValueError(f"Unknown command: {command}")

    def _suffix_for(self, child_type: str, local_id: Any) -> str:
        index = self._child_index.get(child_type, {})
        if local_id in index:
            return index[local_id]
        # A child id the platform coerced on its way through the picker.
        for known, suffix in index.items():
            if str(known) == str(local_id):
                return suffix
        raise ValueError(f"Unknown {child_type}: {local_id}")

    async def _write(self, spec: dict[str, Any], oid: str, value: Any) -> Any:
        """Write one OID and return the value the agent confirmed.

        A SET answers with the varbinds it applied, so the confirmation is the
        device's word rather than ours — which matters on a value the device
        rounds or clamps.
        """
        wire = self._to_wire(spec, value)
        answered = await self.transport.set([(oid, spec["type"], wire)])
        varbind = answered.get(oid)
        if varbind is None or varbind.is_exception:
            return self._to_state(spec, wire)
        return self._to_state(spec, varbind.value)

    async def set_device_setting(self, key: str, value: Any) -> Any:
        spec = self._by_name.get(key)
        if spec is None or spec["access"] != "rw":
            raise ValueError(f"Unknown device setting: {key}")
        confirmed = await self._write(spec, spec["oid"], value)
        self.set_state(key, confirmed)
        return confirmed

    # ── Test Connection ──

    async def run_setup_action(
        self, action_id: str, params: dict[str, Any], progress: Any,
    ) -> dict[str, Any]:
        """Read the identity and every declared OID, and say what answered.

        The two things that go wrong when adding an SNMP device are the
        community string and a mistyped OID, and neither announces itself: a
        wrong community is answered with silence, and a wrong OID reads blank
        forever. This names both.
        """
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")
        try:
            await self._read_system_group()
        except (ConnectionError, asyncio.TimeoutError):
            return {
                "success": False,
                "message": "No answer. Check the IP address, that SNMP is "
                           "enabled on the device, and that the read community "
                           "string is correct — a device ignores a request "
                           "whose community it does not recognise.",
            }
        except SnmpError as e:
            return {"success": False, "message": str(e)}

        descr = self.get_state("sys_descr") or ""
        name = self.get_state("sys_name") or ""
        identity = " — ".join(part for part in (name, descr) if part) or "an SNMP device"

        declared = [s for s in self._scalars if s["access"] in ("r", "rw")]
        missing: list[str] = []
        if declared:
            by_oid = {s["oid"]: s for s in declared}
            for chunk in _chunks(list(by_oid), VARBINDS_PER_REQUEST):
                answered = await self._get(chunk)
                for oid, varbind in answered.items():
                    if varbind.is_exception:
                        missing.append(by_oid[oid]["name"])

        rows = {
            child_type: len(self.list_children(child_type))
            for child_type in self._columns
        }

        message = f"Connected to {identity}."
        if declared:
            message += (f" {len(declared) - len(missing)} of {len(declared)} "
                        f"declared values answered.")
        if missing:
            message += (" The device has no OID for: "
                        + ", ".join(sorted(missing)[:8])
                        + ("…" if len(missing) > 8 else "") + ".")
        for child_type, count in rows.items():
            message += f" Found {count} {child_type}(s)."

        return {"success": not missing, "message": message}

    # ── Liveness ──

    async def _liveness_probe(self) -> None:
        """Read sysDescr. Every agent implements it, so this works on a device
        whose map is empty, and an awaited read is the only way a connectionless
        transport finds out the host is gone."""
        await self.transport.get(SYS_DESCR_OID)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


DRIVER_CLASS = SNMPv2cDriver
