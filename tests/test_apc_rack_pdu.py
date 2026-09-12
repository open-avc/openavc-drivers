"""apc_rack_pdu — APC Switched Rack PDU over the PowerNet MIB.

The driver's transport is answered by the driver's OWN simulator MIB, so every
read and write here goes down the same path a real agent would serve: the walk
finds the rows the simulator declares, a SET lands in its MIB, and the next GET
sees the result.

The centre of gravity is the two MIB branches. APC numbers the same three
facts differently in each — an outlet is ``on`` at 2 in rPDU2 and at 1 in the
legacy tree, and the load states and all-outlet commands are shuffled too — so
a driver that mixed them up would report every outlet backwards, plausibly and
silently. The first block below pins all four vocabularies against the MIB's
own SYNTAX clauses, and the round-trip tests then run twice, once per branch,
against a simulator that serves each branch's real numbering.
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
    """Only this driver's connect ceremony. State handling and the child
    registry come from StubBaseDriver."""

    async def connect(self):
        self.transport = _FakeTransport(self._simulator)
        self.set_state("connected", True)


install_stubs(base_driver=_FakeBaseDriver)
DRV = load_module("apc_rack_pdu_under_test", REPO_ROOT / "power" / "apc_rack_pdu.py")
SIM = load_module("apc_rack_pdu_sim_under_test", REPO_ROOT / "power" / "apc_rack_pdu_sim.py")


class _VarBind:
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
        self.gets = 0
        self.read_oids: list[str] = []

    async def get(self, oids):
        if isinstance(oids, str):
            oids = [oids]
        self.gets += 1
        self.read_oids.extend(oids)
        out = {}
        for oid in oids:
            found = self.sim.read_oid(oid)
            out[oid] = (
                _VarBind(oid, "noSuchObject", None) if found is None
                else _VarBind(oid, found[0], found[1])
            )
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
                    error_name={4: "readOnly", 6: "noAccess",
                                7: "wrongType"}.get(status, str(status)),
                    error_index=1,
                    oid=oid,
                )
            found = self.sim.read_oid(oid)
            out[oid] = _VarBind(oid, found[0], found[1])
        return out


def _driver(generation: str = "rpdu2", *, metered: bool = True, sim=None):
    config = {
        "host": "127.0.0.1", "port": 161,
        "community": "public", "write_community": "private",
        "poll_interval": 30,
    }
    driver = DRV.APCRackPDUDriver("pdu-1", config, StubState(), StubEvents())
    driver._simulator = sim or SIM.APCRackPDUSimulator(
        "sim-1", {"generation": generation, "metered_outlets": metered}
    )
    return driver


async def _connected(generation: str = "rpdu2", *, metered: bool = True):
    driver = _driver(generation, metered=metered)
    await driver.connect()
    await driver._initial_sync()
    return driver


def _child(driver, child_type: str, local_id, prop: str):
    return driver.state.get(f"device.pdu-1.{child_type}.{local_id}.{prop}")


# ── The enum divergence, pinned to the MIB ──────────────────────────────────
#
# Each expectation below is the SYNTAX clause of the named object in PowerNet
# MIB v4.6.0. If APC ever renumbers one, this is where it surfaces — and until
# then these are the assertions that stop the two branches being conflated.


def test_outlet_state_is_numbered_the_opposite_way_in_the_two_branches():
    # rPDU2OutletSwitchedStatusState: off(1), on(2)
    assert DRV.RPDU2_OUTLET_STATE == {1: False, 2: True}
    # rPDUOutletStatusOutletState: outletStatusOn(1), outletStatusOff(2)
    assert DRV.RPDU_OUTLET_STATE == {1: True, 2: False}
    # The whole hazard in one line: the same number means opposite things.
    assert DRV.RPDU2_OUTLET_STATE[1] is not DRV.RPDU_OUTLET_STATE[1]


def test_load_state_swaps_low_and_normal_between_the_branches():
    # rPDU2*LoadState: lowLoad(1), normal(2), nearOverload(3), overload(4)
    assert DRV.RPDU2_LOAD_STATE[1] == "low"
    assert DRV.RPDU2_LOAD_STATE[2] == "normal"
    # rPDULoadStatusLoadState: phaseLoadNormal(1), phaseLoadLow(2), ...
    assert DRV.RPDU_LOAD_STATE[1] == "normal"
    assert DRV.RPDU_LOAD_STATE[2] == "low"
    # near-overload and overload happen to agree; low and normal do not.
    assert DRV.RPDU2_LOAD_STATE[3] == DRV.RPDU_LOAD_STATE[3] == "near_overload"
    assert DRV.RPDU2_LOAD_STATE[4] == DRV.RPDU_LOAD_STATE[4] == "overload"


def test_the_all_outlet_command_values_are_shifted_between_the_branches():
    # rPDU2DeviceControlCommand: immediateAllOn(1), delayedAllOn(2),
    # immediateAllOff(3), immediateAllReboot(4), delayedAllReboot(5),
    # noCommandAll(6), delayedAllOff(7), cancelAllPendingCommands(8)
    assert DRV.RPDU2_ALL_COMMAND["all_outlets_on"] == 1
    assert DRV.RPDU2_ALL_COMMAND["all_outlets_on_delayed"] == 2
    assert DRV.RPDU2_ALL_COMMAND["all_outlets_off"] == 3
    assert DRV.RPDU2_ALL_COMMAND["all_outlets_off_delayed"] == 7
    # rPDUOutletDevCommand: noCommandAll(1), immediateAllOn(2),
    # immediateAllOff(3), immediateAllReboot(4), delayedAllOn(5),
    # delayedAllOff(6), delayedAllReboot(7), cancelAllPendingCommands(8)
    assert DRV.RPDU_ALL_COMMAND["all_outlets_on"] == 2
    assert DRV.RPDU_ALL_COMMAND["all_outlets_on_delayed"] == 5
    assert DRV.RPDU_ALL_COMMAND["all_outlets_off"] == 3
    assert DRV.RPDU_ALL_COMMAND["all_outlets_off_delayed"] == 6
    # "Turn everything on" is 1 in one branch and 1 means "do nothing" in the
    # other, which is the quiet version of this bug.
    assert DRV.RPDU_ALL_COMMAND["all_outlets_on"] != 1


def test_the_delayed_outlet_commands_are_shifted_by_outlet_unknown():
    # Both branches agree on immediate on/off/reboot...
    for name in ("outlet_on", "outlet_off", "outlet_reboot"):
        assert DRV.RPDU2_OUTLET_COMMAND[name] == DRV.RPDU_OUTLET_COMMAND[name]
    # ...and diverge from there, because rPDU2 spends 4 on outletUnknown.
    assert DRV.RPDU2_OUTLET_COMMAND["outlet_on_delayed"] == 5
    assert DRV.RPDU_OUTLET_COMMAND["outlet_on_delayed"] == 4
    assert DRV.RPDU2_OUTLET_COMMAND["outlet_cancel_pending"] == 8
    assert DRV.RPDU_OUTLET_COMMAND["outlet_cancel_pending"] == 7


def test_every_tree_decodes_load_state_with_its_own_vocabulary():
    """No column reaches for the other branch's map."""
    for tree, expected in ((DRV.TREE_RPDU2, DRV.RPDU2_LOAD_STATE),
                           (DRV.TREE_RPDU, DRV.RPDU_LOAD_STATE)):
        tables = [t for t in (tree.outlets, tree.banks, tree.phases) if t]
        columns = [c for t in tables for c in (*t.live, *t.slow)]
        columns += list(tree.scalars)
        for column in columns:
            if column.prop == "load_state":
                assert column.enum is expected, (
                    f"{tree.key}: {column.oid} decodes load_state with the "
                    f"other branch's vocabulary"
                )


# ── Branch detection ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_generation_pdu_is_read_on_the_rpdu2_branch():
    driver = await _connected("rpdu2")

    assert driver.get_state("mib_branch") == "rpdu2"
    assert driver.get_state("model") == "AP8959"
    assert driver.get_state("serial_number") == "5A0000000000"


@pytest.mark.asyncio
async def test_a_first_generation_pdu_falls_back_to_the_legacy_branch():
    driver = await _connected("rpdu")

    assert driver.get_state("mib_branch") == "rpdu"
    assert driver.get_state("model") == "AP7921"
    assert len(driver.list_children("outlet")) == SIM.LEGACY_OUTLET_COUNT


@pytest.mark.asyncio
async def test_an_snmp_host_that_is_not_a_rack_pdu_connects_and_says_nothing_wrong(
    caplog,
):
    """A projector with an enterprise MIB, pointed at this driver by mistake:
    it must not crash, must not invent a roster, and must say why."""
    sim = SIM.APCRackPDUSimulator("sim-1")
    sim.oids = {SIM.SYS_DESCR: ("string", "Some other SNMP device")}
    driver = _driver(sim=sim)
    await driver.connect()
    await driver._initial_sync()

    assert driver.get_state("mib_branch") is None
    assert driver.list_children("outlet") == []
    assert "PowerNet Rack PDU branch" in caplog.text


# ── The outlet roster ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outlets_are_integer_ids_read_off_the_device():
    driver = await _connected("rpdu2")

    ids = driver.list_children("outlet")
    assert len(ids) == SIM.OUTLET_COUNT
    assert all(isinstance(i, int) for i in ids)
    # The reason integer ids are worth having: row 2 sorts before row 10.
    assert sorted(ids)[:3] == [1, 2, 3]
    assert sorted(ids)[-1] == SIM.OUTLET_COUNT


@pytest.mark.asyncio
async def test_the_roster_follows_the_device_when_a_row_disappears():
    driver = await _connected("rpdu2")
    assert 24 in driver.list_children("outlet")

    for column in (1, 2, 3, 4, 5, 6):
        driver._simulator.oids.pop(f"{SIM.R2_OUT_STATUS}.{column}.24", None)
    await driver.refresh_children()

    assert 24 not in driver.list_children("outlet")
    assert 23 in driver.list_children("outlet")


@pytest.mark.asyncio
async def test_banks_phases_and_the_sensor_are_their_own_children():
    driver = await _connected("rpdu2")

    assert len(driver.list_children("bank")) == SIM.BANK_COUNT
    assert len(driver.list_children("phase")) == 1
    assert len(driver.list_children("sensor")) == 1
    assert _child(driver, "sensor", 1, "temperature_c") == 22.4
    assert _child(driver, "sensor", 1, "humidity") == 41
    assert _child(driver, "sensor", 1, "comm_status") == "ok"
    assert _child(driver, "bank", 1, "breaker_rating_a") == 20


# ── Switching ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["rpdu2", "rpdu"])
async def test_switching_an_outlet_off_and_on_round_trips(generation):
    driver = await _connected(generation, metered=False)
    assert _child(driver, "outlet", 3, "state") is True

    await driver.send_command("outlet_off", {"outlet": 3})
    assert _child(driver, "outlet", 3, "state") is False

    await driver.send_command("outlet_on", {"outlet": 3})
    assert _child(driver, "outlet", 3, "state") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["rpdu2", "rpdu"])
async def test_the_state_comes_from_the_status_column_not_from_what_was_written(
    generation,
):
    """The control column and the status column use different numbers in
    rPDU2 (immediateOn is 1, on is 2), so a driver that echoed its own write
    would publish the wrong thing. Point the two at each other and check the
    driver believes the status column."""
    driver = await _connected(generation, metered=False)
    sim = driver._simulator

    await driver.send_command("outlet_off", {"outlet": 5})
    if generation == "rpdu2":
        assert sim.oids[f"{SIM.R2_OUT_STATUS}.5.5"][1] == SIM.R2_STATE_OFF
        assert sim.oids[f"{SIM.R2_OUT_CONTROL}.5.5"][1] == SIM.R2_CMD_OFF
        # ...and those two are different integers for the same fact.
        assert SIM.R2_STATE_OFF != SIM.R2_CMD_OFF
    else:
        assert sim.oids[f"{SIM.R1_OUT_STATUS}.4.5"][1] == SIM.R1_STATE_OFF
    assert _child(driver, "outlet", 5, "state") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["rpdu2", "rpdu"])
async def test_rebooting_an_outlet_leaves_it_powered(generation):
    driver = await _connected(generation, metered=False)
    await driver.send_command("outlet_off", {"outlet": 2})
    assert _child(driver, "outlet", 2, "state") is False

    await driver.send_command("outlet_reboot", {"outlet": 2})

    assert _child(driver, "outlet", 2, "state") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["rpdu2", "rpdu"])
async def test_all_outlets_off_then_on_moves_every_outlet(generation):
    driver = await _connected(generation, metered=False)

    await driver.send_command("all_outlets_off")
    states = [_child(driver, "outlet", i, "state")
              for i in driver.list_children("outlet")]
    assert states and not any(states)

    await driver.send_command("all_outlets_on")
    states = [_child(driver, "outlet", i, "state")
              for i in driver.list_children("outlet")]
    assert all(states)


@pytest.mark.asyncio
async def test_a_delayed_command_sends_its_own_branchs_value():
    """`all_outlets_on_delayed` is 2 in rPDU2 and 5 in the legacy branch; 2 in
    the legacy branch is plain immediateAllOn. Check the wire value, because
    the outcome after the simulator applies it looks the same either way."""
    driver = await _connected("rpdu", metered=False)
    sim = driver._simulator

    await driver.send_command("all_outlets_on_delayed")

    assert sim.oids[f"{SIM.R1_OUT_DEV}.1.0"][1] == 5


@pytest.mark.asyncio
async def test_an_unknown_outlet_is_refused_rather_than_written_somewhere():
    driver = await _connected("rpdu2")

    with pytest.raises(ValueError, match="Unknown outlet"):
        await driver.send_command("outlet_on", {"outlet": 99})


# ── Readings, units and the -1 convention ───────────────────────────────────


@pytest.mark.asyncio
async def test_readings_are_scaled_into_the_units_they_are_declared_in():
    driver = await _connected("rpdu2")

    # rPDU2PhaseStatusCurrent is tenths of an amp; the state variable is amps.
    tenths = driver._simulator._total_tenths()
    assert _child(driver, "phase", 1, "current_a") == round(tenths / 10, 4)
    # rPDU2PhaseStatusVoltage is volts already.
    assert _child(driver, "phase", 1, "voltage_v") == 120
    # rPDU2DeviceStatusEnergy is tenths of a kWh.
    assert driver.get_state("energy_kwh") == 482.1
    # rPDU2BankStatusPeakCurrent is tenths of an amp.
    assert _child(driver, "bank", 1, "peak_current_a") == 11.9


@pytest.mark.asyncio
async def test_a_reading_the_model_does_not_support_is_absent_not_minus_one():
    """The MIB's own convention: "models that do not support this feature will
    respond with -1". Publishing that verbatim puts -1 kVA on a panel."""
    driver = await _connected("rpdu2", metered=False)

    assert driver._simulator.oids[f"{SIM.R2_STATUS}.16.1"][1] == -1
    assert driver.get_state("apparent_power_kva") is None
    assert driver.get_state("power_factor") is None
    # ...while a supported reading on the same unit still comes through.
    assert driver.get_state("peak_power_kw") == 2.14


@pytest.mark.asyncio
async def test_per_outlet_metering_appears_only_on_a_metered_model():
    metered = await _connected("rpdu2", metered=True)
    assert _child(metered, "outlet", 1, "power_w") is not None
    assert _child(metered, "outlet", 1, "energy_kwh") == 3.1

    switched = await _connected("rpdu2", metered=False)
    assert _child(switched, "outlet", 1, "power_w") is None
    assert switched.get_state("metered_outlet_count") == 0


@pytest.mark.asyncio
async def test_switching_an_outlet_off_zeroes_its_metered_draw():
    """A driver that reported watts on a dead outlet would pass every
    round-trip assertion about the state alone."""
    driver = await _connected("rpdu2", metered=True)
    assert _child(driver, "outlet", 4, "power_w") > 0

    await driver.send_command("outlet_off", {"outlet": 4})
    await driver.poll()

    assert _child(driver, "outlet", 4, "power_w") == 0
    assert _child(driver, "outlet", 4, "current_a") == 0


@pytest.mark.asyncio
async def test_the_load_reading_follows_the_outlets():
    driver = await _connected("rpdu2", metered=True)
    before = _child(driver, "phase", 1, "current_a")

    await driver.send_command("all_outlets_off")
    await driver.poll()

    assert _child(driver, "phase", 1, "current_a") == 0
    assert before > 0


# ── The legacy combined phase/bank table ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_legacy_phase_rows_are_accepted_when_the_layout_is_plain():
    driver = await _connected("rpdu")

    assert len(driver.list_children("phase")) == 1
    assert _child(driver, "phase", 1, "number") == 1
    # And decoded with the legacy vocabulary: 1 is normal here, not low.
    assert _child(driver, "phase", 1, "load_state") == "normal"


@pytest.mark.asyncio
async def test_an_ambiguous_legacy_load_table_publishes_no_phases(caplog):
    """rPDULoadStatusTable interleaves phases, banks and sometimes a device
    total with no column saying which. When the first rows are not phases
    1..N, the driver must decline rather than label a bank's amps as a
    phase's."""
    sim = SIM.APCRackPDUSimulator("sim-1", {"generation": "rpdu"})
    # A device-total row first, the way the MIB says some models order it.
    sim.oids[f"{SIM.R1_LOAD_STATUS}.4.1"] = ("integer", 0)
    driver = _driver(sim=sim)
    await driver.connect()
    await driver._initial_sync()

    assert driver.list_children("phase") == []
    assert "cannot be told apart" in caplog.text
    # The outlets, which are unambiguous, still came up.
    assert len(driver.list_children("outlet")) == SIM.LEGACY_OUTLET_COUNT


# ── Writes other than switching ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_renaming_an_outlet_updates_the_name_the_pdu_reports():
    driver = await _connected("rpdu2")

    await driver.send_command("set_outlet_name",
                              {"outlet": 7, "name": "Amp Rack"})

    assert _child(driver, "outlet", 7, "name") == "Amp Rack"
    await driver.refresh_children()
    assert _child(driver, "outlet", 7, "name") == "Amp Rack"


@pytest.mark.asyncio
async def test_setting_an_outlet_delay_reads_back():
    driver = await _connected("rpdu2")

    await driver.send_command("set_outlet_power_on_delay",
                              {"outlet": 2, "seconds": 45})

    assert _child(driver, "outlet", 2, "power_on_delay") == 45


@pytest.mark.asyncio
async def test_a_reboot_duration_outside_the_mibs_range_is_refused_by_name():
    driver = await _connected("rpdu2")

    with pytest.raises(ValueError, match="between 5 and 60"):
        await driver.send_command("set_outlet_reboot_duration",
                                  {"outlet": 1, "seconds": 120})


@pytest.mark.asyncio
async def test_a_device_setting_writes_and_reads_back():
    driver = await _connected("rpdu2")

    result = await driver.set_device_setting("coldstart_delay", 30)

    assert result == 30
    assert driver.get_state("coldstart_delay") == 30
    await driver.poll()
    assert driver.get_state("coldstart_delay") == 30


@pytest.mark.asyncio
async def test_a_setting_the_legacy_branch_lacks_is_refused_by_name():
    """rPDU has no location OID. Silence would leave the field looking saved."""
    driver = await _connected("rpdu")

    with pytest.raises(ValueError, match="not settable"):
        await driver.set_device_setting("device_location", "Room 9")


@pytest.mark.asyncio
async def test_resetting_the_energy_meter_zeroes_it():
    driver = await _connected("rpdu2")
    assert driver.get_state("energy_kwh") == 482.1

    await driver.send_command("reset_energy")

    assert driver.get_state("energy_kwh") == 0


@pytest.mark.asyncio
async def test_a_command_the_answering_branch_lacks_raises_rather_than_succeeds():
    """A declared command with no branch in send_command answers
    {"success": true, "result": null} — byte-identical to one that worked."""
    driver = await _connected("rpdu")

    with pytest.raises(ValueError, match="not available"):
        await driver.send_command("reset_energy")


@pytest.mark.asyncio
async def test_restarting_the_management_card_writes_the_apcmgmt_oid():
    """apcmgmt is 318.2 — a sibling of products, not under either Rack PDU
    branch. Getting this wrong writes into an unrelated subtree."""
    driver = await _connected("rpdu2")

    await driver.send_command("restart_management_card")

    assert DRV.MCONTROL_RESTART_AGENT == "1.3.6.1.4.1.318.2.2.1.0"
    entry = driver._simulator.oids[DRV.MCONTROL_RESTART_AGENT]
    assert entry[1] == DRV.RESTART_CURRENT_AGENT


# ── Polling shape ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_poll_does_not_re_read_what_cannot_change():
    """Names, bank assignment and the configured delays are read at connect
    and on Refresh, not every poll. On a 24-outlet metered strip they are more
    varbinds than everything the poll actually needs."""
    driver = await _connected("rpdu2")
    transport = driver.transport
    transport.read_oids.clear()

    await driver.poll()

    read = set(transport.read_oids)
    assert any(o.startswith(f"{SIM.R2_OUT_STATUS}.5.") for o in read), (
        "the poll should read outlet state"
    )
    assert not any(o.startswith(f"{SIM.R2_OUT_CFG}.5.") for o in read), (
        "the poll should not re-read configured power-on delays"
    )
    assert not any(o.startswith(f"{SIM.R2_OUT_STATUS}.3.") for o in read), (
        "the poll should not re-read outlet names"
    )


@pytest.mark.asyncio
async def test_reads_are_batched_rather_than_one_oid_at_a_time():
    driver = await _connected("rpdu2", metered=True)
    transport = driver.transport
    transport.gets = 0
    transport.read_oids.clear()

    await driver.poll()

    assert len(transport.read_oids) > 60, "a 24-outlet strip has a lot to read"
    assert transport.gets <= 1 + len(transport.read_oids) // (
        DRV.VARBINDS_PER_REQUEST - 1
    )


# ── Test Connection ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_connection_reports_the_model_branch_and_roster():
    driver = _driver("rpdu2")
    await driver.connect()

    result = await driver.run_setup_action("test_connection", {}, None)

    assert result["success"] is True
    assert "AP8959" in result["message"]
    assert "rPDU2" in result["message"]
    assert "24 outlets" in result["message"]
    assert "write community" in result["message"]


@pytest.mark.asyncio
async def test_test_connection_names_the_problem_when_it_is_not_a_rack_pdu():
    sim = SIM.APCRackPDUSimulator("sim-1")
    sim.oids = {SIM.SYS_DESCR: ("string", "Some other SNMP device")}
    driver = _driver(sim=sim)
    await driver.connect()

    result = await driver.run_setup_action("test_connection", {}, None)

    assert result["success"] is False
    assert "Rack PDU branch" in result["message"]
    assert "generic SNMP Device driver" in result["message"]


# ── Contract ────────────────────────────────────────────────────────────────


def test_every_declared_command_is_reachable_in_send_command():
    """A Python command with no branch in send_command returns success and
    does nothing, forever, and no gate catches it."""
    declared = set(DRV.APCRackPDUDriver.DRIVER_INFO["commands"])
    handled = (
        {"refresh", "restart_management_card", "set_outlet_name",
         "set_outlet_power_on_delay", "set_outlet_power_off_delay",
         "set_outlet_reboot_duration"}
        | set(DRV.RPDU2_OUTLET_COMMAND) | set(DRV.RPDU_OUTLET_COMMAND)
        | set(DRV.RPDU2_ALL_COMMAND) | set(DRV.RPDU_ALL_COMMAND)
        | set(DRV.TREE_RPDU2.resets) | set(DRV.TREE_RPDU.resets)
    )
    assert declared - handled == set()


def test_every_column_writes_a_declared_child_state_variable():
    """Strict driver state turns an undeclared write into a failure at
    runtime; this says which column would do it, rather than which test."""
    declared = {
        child_type: set(spec["state_variables"])
        for child_type, spec in DRV.CHILD_ENTITY_TYPES.items()
    }
    for tree in DRV.TREES:
        for child_type, table in (("outlet", tree.outlets),
                                  ("bank", tree.banks),
                                  ("phase", tree.phases),
                                  ("sensor", tree.sensors)):
            if table is None:
                continue
            for column in (*table.live, *table.slow):
                assert column.prop in declared[child_type], (
                    f"{tree.key} {child_type} column {column.oid} writes "
                    f"undeclared {column.prop!r}"
                )
    for column in (*DRV.METERED_OUTLET_LIVE, *DRV.METERED_OUTLET_SLOW):
        assert column.prop in declared["outlet"]


def test_every_scalar_writes_a_declared_device_state_variable():
    declared = set(DRV.APCRackPDUDriver.DRIVER_INFO["state_variables"])
    for tree in DRV.TREES:
        for column in tree.scalars:
            assert column.prop in declared, (
                f"{tree.key} scalar {column.oid} writes undeclared "
                f"{column.prop!r}"
            )


def test_every_device_setting_points_at_a_declared_state_variable():
    """A setting's state_key must name a DEVICE-level state variable — the
    platform looks it up there, so a child key would never confirm."""
    info = DRV.APCRackPDUDriver.DRIVER_INFO
    for key, spec in info["device_settings"].items():
        assert spec["state_key"] in info["state_variables"]
        # ...and at least one branch can actually write it.
        assert any(key in tree.settings for tree in DRV.TREES), key


def test_every_enum_state_variable_lists_every_value_it_can_be_given():
    """A value missing from `values` is a state the panel cannot label."""
    info = DRV.APCRackPDUDriver.DRIVER_INFO
    pairs = [
        (info["state_variables"]["load_state"]["values"], DRV.RPDU2_LOAD_STATE),
        (info["state_variables"]["load_state"]["values"], DRV.RPDU_LOAD_STATE),
        (info["state_variables"]["power_supply_1"]["values"], DRV.PSU_STATUS),
        (
            DRV.CHILD_ENTITY_TYPES["outlet"]["state_variables"]["phase"]["values"],
            DRV.PHASE_LAYOUT,
        ),
        (
            DRV.CHILD_ENTITY_TYPES["sensor"]["state_variables"]["comm_status"]["values"],
            DRV.SENSOR_COMMS,
        ),
        (
            DRV.CHILD_ENTITY_TYPES["sensor"]["state_variables"]["sensor_type"]["values"],
            DRV.SENSOR_TYPE,
        ),
        (
            DRV.CHILD_ENTITY_TYPES["phase"]["state_variables"]["overload_restriction"]["values"],
            DRV.OVERLOAD_RESTRICTION,
        ),
    ]
    for values, mapping in pairs:
        assert set(mapping.values()) <= set(values), (
            f"{sorted(set(mapping.values()) - set(values))} missing"
        )


def test_the_outlet_child_id_range_covers_the_largest_rack_pdu_group():
    """Network Port Sharing puts up to four Rack PDUs behind one management
    card, and the outlet index runs across the whole group."""
    id_format = DRV.CHILD_ENTITY_TYPES["outlet"]["id_format"]
    assert id_format["type"] == "integer"
    assert id_format["max"] >= 4 * 24
