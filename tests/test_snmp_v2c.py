"""snmp_v2c — the generic SNMP driver.

The driver's transport is backed by the driver's OWN simulator MIB, so every
read and write here goes through the same declarations a user would meet: the
walk finds the rows the simulator serves, a write lands in the simulator's MIB
and the next read sees it. A driver and a simulator written in one sitting can
still agree with each other and both be wrong about SNMP — that is what the
platform-side tests against the real transport are for — but they cannot
silently disagree with each other, which is what this catches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _platform_stubs import (
    SnmpError,
    StubBaseDriver,
    StubEvents,
    StubState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeBaseDriver(StubBaseDriver):
    """Only this driver's connect ceremony. Everything about state and the
    child registry comes from StubBaseDriver."""

    async def connect(self):
        self.transport = _FakeTransport(self._simulator)
        self.set_state("connected", True)


install_stubs(base_driver=_FakeBaseDriver)
DRV = load_module("snmp_v2c_under_test", REPO_ROOT / "utility" / "snmp_v2c.py")
SIM = load_module("snmp_v2c_sim_under_test", REPO_ROOT / "utility" / "snmp_v2c_sim.py")


class _VarBind:
    """What the transport hands back: an OID, its SNMP type, its value."""

    def __init__(self, oid: str, type_name: str, value):
        self.oid = oid
        self.type = type_name
        self.value = value

    @property
    def is_exception(self) -> bool:
        return self.type in ("noSuchObject", "noSuchInstance", "endOfMibView")


class _FakeTransport:
    """The SNMP transport, answered by the driver's own simulator."""

    def __init__(self, simulator):
        self.sim = simulator
        self.connected = True
        self.reads = 0
        self.refuse_writes = False

    async def get(self, oids):
        if isinstance(oids, str):
            oids = [oids]
        self.reads += 1
        out = {}
        for oid in oids:
            found = self.sim.read_oid(oid)
            if found is None:
                out[oid] = _VarBind(oid, "noSuchObject", None)
            else:
                out[oid] = _VarBind(oid, found[0], found[1])
        return out

    async def get_value(self, oid):
        varbind = (await self.get(oid)).get(oid)
        return None if varbind is None or varbind.is_exception else varbind.value

    async def walk(self, root_oid, limit=512):
        rows = []
        prefix = root_oid + "."
        current = root_oid
        for _ in range(limit):
            nxt = self.sim.next_oid(current)
            if nxt is None or not nxt.startswith(prefix):
                break
            found = self.sim.read_oid(nxt)
            rows.append(_VarBind(nxt, found[0], found[1]))
            current = nxt
        return rows

    async def set(self, bindings):
        out = {}
        for oid, type_name, value in bindings:
            status = self.sim.write_oid(oid, type_name, value)
            if status != 0:
                raise SnmpError(
                    "The device refused the write.",
                    error_status=status,
                    error_name={4: "readOnly", 6: "noAccess", 7: "wrongType"}.get(
                        status, str(status)
                    ),
                    error_index=1,
                    oid=oid,
                )
            found = self.sim.read_oid(oid)
            out[oid] = _VarBind(oid, found[0], found[1])
        return out


WIDGET = SIM.WIDGET
OUTLET_LABEL = SIM.OUTLET_LABEL
OUTLET_STATE = SIM.OUTLET_STATE
OUTLET_WATTS = SIM.OUTLET_WATTS


def _driver(oid_map=None, table_map=None):
    config = {
        "host": "127.0.0.1", "port": 161,
        "community": "public", "write_community": "private",
        "poll_interval": 30,
        "oid_map": oid_map or [],
        "table_map": table_map or [],
    }
    driver = DRV.SNMPv2cDriver("snmp-1", config, StubState(), StubEvents())
    driver._simulator = SIM.SNMPv2cSimulator("sim-1")
    return driver


# ── Parsing the declarations ────────────────────────────────────────────────


def test_a_read_row_becomes_a_state_variable_and_a_write_row_a_command():
    driver = _driver([
        {"name": "load", "oid": SIM.LOAD_TENTHS, "type": "gauge32",
         "access": "r", "scale": 0.1, "unit": "A"},
        {"name": "beacon", "oid": SIM.BEACON, "type": "integer", "access": "w"},
        {"name": "site", "oid": SIM.SITE_LABEL, "type": "string", "access": "rw"},
    ])

    info = driver.DRIVER_INFO
    assert info["state_variables"]["load"]["type"] == "number"   # scaled
    assert info["state_variables"]["load"]["unit"] == "A"
    assert "set_beacon" in info["commands"]
    assert "beacon" not in info["state_variables"]               # write-only
    assert info["device_settings"]["site"]["state_key"] == "site"
    assert info["state_variables"]["site"]["type"] == "string"


def test_states_turn_a_number_into_a_name_and_a_picker():
    driver = _driver([
        {"name": "beacon", "oid": SIM.BEACON, "type": "integer",
         "access": "rw", "states": "1=On,2=Off"},
    ])

    spec = driver._by_name["beacon"]
    assert spec["states"] == {1: "On", 2: "Off"}
    # The state reads as the name...
    assert driver.DRIVER_INFO["state_variables"]["beacon"]["type"] == "string"
    # ...and the setting offers the names rather than asking for a number.
    setting = driver.DRIVER_INFO["device_settings"]["beacon"]
    assert setting["type"] == "enum"
    assert [v["value"] for v in setting["values"]] == ["On", "Off"]


def test_a_malformed_row_is_skipped_and_the_rest_survive(caplog):
    driver = _driver([
        {"name": "good", "oid": SIM.LOAD_TENTHS, "type": "gauge32"},
        {"name": "bad_oid", "oid": "not an oid", "type": "integer"},
        {"name": "", "oid": SIM.BEACON},
        {"name": "odd_type", "oid": SIM.PACKETS, "type": "float128"},
    ])

    names = [s["name"] for s in driver._scalars]
    assert names == ["good", "odd_type"]
    assert driver._by_name["odd_type"]["type"] == "integer"   # corrected, not fatal


def test_a_duplicate_name_keeps_the_last_row():
    driver = _driver([
        {"name": "load", "oid": SIM.LOAD_TENTHS, "type": "gauge32"},
        {"name": "load", "oid": SIM.PACKETS, "type": "counter32"},
    ])

    assert len(driver._scalars) == 1
    assert driver._by_name["load"]["oid"] == SIM.PACKETS


def test_states_are_ignored_on_a_type_that_cannot_carry_them():
    driver = _driver([
        {"name": "site", "oid": SIM.SITE_LABEL, "type": "string",
         "access": "rw", "states": "1=On,2=Off"},
    ])

    assert driver._by_name["site"]["states"] == {}


def test_the_index_column_leads_its_group_however_it_was_entered():
    driver = _driver(table_map=[
        {"child_type": "outlet", "name": "watts", "oid": OUTLET_WATTS,
         "type": "gauge32"},
        {"child_type": "outlet", "name": "label", "oid": OUTLET_LABEL,
         "type": "string", "index": True},
        {"child_type": "outlet", "name": "state", "oid": OUTLET_STATE,
         "type": "integer", "access": "rw", "states": "1=On,2=Off"},
    ])

    assert [c["name"] for c in driver._columns["outlet"]][0] == "label"


def test_a_group_with_no_index_column_walks_its_first():
    driver = _driver(table_map=[
        {"child_type": "outlet", "name": "label", "oid": OUTLET_LABEL,
         "type": "string"},
        {"child_type": "outlet", "name": "watts", "oid": OUTLET_WATTS,
         "type": "gauge32"},
    ])

    assert driver._columns["outlet"][0]["name"] == "label"


def test_a_table_becomes_a_child_type_named_by_its_label_column():
    driver = _driver(table_map=[
        {"child_type": "outlet", "name": "label", "oid": OUTLET_LABEL,
         "type": "string", "index": True},
        {"child_type": "outlet", "name": "state", "oid": OUTLET_STATE,
         "type": "integer", "access": "rw", "states": "1=On,2=Off"},
    ])

    declared = driver.DRIVER_INFO["child_entity_types"]["outlet"]
    assert declared["label_field"] == "label"
    assert set(declared["state_variables"]) == {"label", "state"}
    assert "set_outlet_state" in driver.DRIVER_INFO["commands"]
    param = driver.DRIVER_INFO["commands"]["set_outlet_state"]["params"]
    assert param["child_id"]["child_type"] == "outlet"
    assert [v["value"] for v in param["value"]["values"]] == ["On", "Off"]


# ── Against the simulator ───────────────────────────────────────────────────


async def _widget():
    """The driver as an integrator would declare this device: three scalars
    (one of them mistyped, on purpose) and the outlet table."""
    driver = _driver(
        oid_map=[
            {"name": "load", "oid": SIM.LOAD_TENTHS, "type": "gauge32",
             "access": "r", "scale": 0.1, "unit": "A"},
            {"name": "packets", "oid": SIM.PACKETS, "type": "counter32"},
            {"name": "beacon", "oid": SIM.BEACON, "type": "integer",
             "access": "rw", "states": "1=On,2=Off"},
            {"name": "absent", "oid": f"{WIDGET}.9.9.9", "type": "integer"},
        ],
        table_map=[
            {"child_type": "outlet", "name": "label", "oid": OUTLET_LABEL,
             "type": "string", "index": True},
            {"child_type": "outlet", "name": "state", "oid": OUTLET_STATE,
             "type": "integer", "access": "rw", "states": "1=On,2=Off"},
            {"child_type": "outlet", "name": "watts", "oid": OUTLET_WATTS,
             "type": "gauge32", "unit": "W"},
        ],
    )
    await driver.connect()
    await driver._initial_sync()
    return driver


@pytest.mark.asyncio
async def test_the_device_identifies_itself_without_being_asked():
    """The MIB-II system group is read on connect, so a device that has just
    been added shows what it is before a single OID has been declared."""
    widget = await _widget()
    assert "Acme rack widget" in widget.get_state("sys_descr")
    assert widget.get_state("sys_name") == "rack-widget-1"
    assert widget.get_state("sys_location") == "Rack 4, Room 210"
    assert isinstance(widget.get_state("sys_uptime"), int)


@pytest.mark.asyncio
async def test_a_scaled_reading_and_a_big_counter_come_back_right():
    widget = await _widget()
    # 34 tenths of an amp, scaled by 0.1.
    assert widget.get_state("load") == pytest.approx(3.4)
    # Past 2^31 — the range a signed decoder turns negative.
    assert widget.get_state("packets") == 3_100_000_000


@pytest.mark.asyncio
async def test_an_enumerated_value_reads_as_its_name():
    widget = await _widget()
    assert widget.get_state("beacon") == "On"


@pytest.mark.asyncio
async def test_the_walk_finds_every_row_of_the_table():
    widget = await _widget()
    ids = sorted(widget.list_children("outlet"))

    # String ids: a MIB index is not an integer in general.
    assert ids == [str(i) for i in range(1, SIM.OUTLET_COUNT + 1)]
    assert widget.get_state("outlet.1.label") == "outlet 1"
    assert widget.get_state("outlet.1.state") == "On"
    assert widget.get_state("outlet.8.state") == "Off"
    assert widget.get_state("outlet.2.watts") == 46


@pytest.mark.asyncio
async def test_writing_a_child_column_lands_on_that_row():
    widget = await _widget()
    await widget.send_command("set_outlet_state", {"child_id": "3", "value": "Off"})

    # The simulator's MIB moved...
    assert widget._simulator.oids[f"{OUTLET_STATE}.3"][1] == 2
    # ...only that row moved...
    assert widget._simulator.oids[f"{OUTLET_STATE}.4"][1] == 1
    # ...and the driver reports the new value by name.
    assert widget.get_state("outlet.3.state") == "Off"


@pytest.mark.asyncio
async def test_a_side_effect_of_the_write_is_seen_on_the_next_poll():
    """Switching an outlet off drops its power reading. The driver must read
    that back rather than assume the rest of the row is unchanged."""
    widget = await _widget()
    await widget.send_command("set_outlet_state", {"child_id": "2", "value": "Off"})
    await widget.poll()

    assert widget.get_state("outlet.2.watts") == 0


@pytest.mark.asyncio
async def test_a_scalar_write_takes_the_name_and_sends_the_number():
    widget = await _widget()
    await widget.set_device_setting("beacon", "Off")

    assert widget._simulator.oids[SIM.BEACON][1] == 2
    assert widget.get_state("beacon") == "Off"


@pytest.mark.asyncio
async def test_a_value_outside_the_declared_states_is_refused():
    widget = await _widget()
    with pytest.raises(ValueError, match="not one of"):
        await widget.set_device_setting("beacon", "Blinking")


@pytest.mark.asyncio
async def test_writing_a_read_only_oid_surfaces_the_agents_refusal():
    driver = _driver([
        {"name": "packets", "oid": SIM.PACKETS, "type": "counter32", "access": "w"},
    ])
    await driver.connect()

    with pytest.raises(SnmpError) as excinfo:
        await driver.send_command("set_packets", {"value": 5})

    assert excinfo.value.error_name == "readOnly"


@pytest.mark.asyncio
async def test_an_oid_the_device_does_not_have_is_reported_once(caplog):
    """A mistyped OID reads blank forever, so it has to say so — and a device
    that lacks one must not log it on every poll for the rest of its life."""
    widget = await _widget()
    caplog.clear()
    with caplog.at_level("WARNING"):
        await widget.poll()
        await widget.poll()

    absent = [r for r in caplog.records if "no such OID" in r.getMessage()]
    assert absent == []          # the first poll, during _initial_sync, said it
    assert "absent" in widget._absent
    assert widget.get_state("absent") is None


@pytest.mark.asyncio
async def test_a_row_that_disappears_is_deregistered():
    widget = await _widget()
    for oid in list(widget._simulator.oids):
        if oid.startswith(f"{OUTLET_LABEL}.7") or oid.startswith(f"{OUTLET_LABEL}.8"):
            del widget._simulator.oids[oid]

    counts = await widget.refresh_children()

    assert counts == {"outlet": 6}
    assert sorted(widget.list_children("outlet")) == ["1", "2", "3", "4", "5", "6"]


@pytest.mark.asyncio
async def test_a_poll_batches_rather_than_asking_one_oid_at_a_time():
    """Three columns across eight outlets plus three scalars is 27 values. One
    request each would be 27 round trips on a protocol with no pipelining."""
    widget = await _widget()
    widget.transport.reads = 0

    await widget.poll()

    assert widget.transport.reads <= 6


@pytest.mark.asyncio
async def test_test_connection_names_the_oids_the_device_does_not_have():
    widget = await _widget()
    result = await widget.run_setup_action("test_connection", {}, None)

    assert result["success"] is False
    assert "rack-widget-1" in result["message"]
    assert "absent" in result["message"]
    assert "8 outlet(s)" in result["message"]


@pytest.mark.asyncio
async def test_test_connection_passes_when_every_declared_oid_answers():
    driver = _driver([
        {"name": "load", "oid": SIM.LOAD_TENTHS, "type": "gauge32"},
    ])
    await driver.connect()

    result = await driver.run_setup_action("test_connection", {}, None)

    assert result["success"] is True
    assert "1 of 1" in result["message"]


@pytest.mark.asyncio
async def test_every_declared_command_has_somewhere_to_go():
    """A Python command with no branch in send_command answers success and does
    nothing, which nothing else in the toolchain can see."""
    widget = await _widget()
    for name in widget.DRIVER_INFO["commands"]:
        assert (name in widget._scalar_commands) or (name in widget._child_commands)


@pytest.mark.asyncio
async def test_the_liveness_probe_reads_something_every_agent_has():
    """A device whose map is empty still has to be watched — sysDescr is the
    one OID that is always there."""
    widget = await _widget()
    await widget._liveness_probe()


@pytest.mark.asyncio
async def test_a_driver_with_nothing_declared_still_polls_and_stays_alive():
    driver = _driver()
    await driver.connect()
    await driver._initial_sync()

    await driver.poll()

    assert driver.get_state("sys_name") == "rack-widget-1"
