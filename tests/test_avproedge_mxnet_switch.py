"""Driver + simulator tests for avproedge_mxnet_switch (AVPro Edge MXnet switch).

Correctness is proven three ways:

1. **Against the hardware's own bytes.** Every CLI parser runs over
   ``fixtures/avproedge_mxnet_switch_outputs.py``, captured from a real
   AC-MXNET-SW8P. The switch's tables are column-aligned, so a parser that
   works on hand-written sample text can still fail on the device; these are
   the device's actual bytes.
2. **Command-surface consistency.** Every declared command has a branch in
   ``send_command`` -- a Python driver that silently falls through returns
   success indistinguishable from a command that worked, which no gate catches.
3. **A dual-proof round trip.** The real driver is wired to the real simulator
   through an in-memory pipe, so connect -> login -> poll -> command runs end
   to end and both sides are asserted.

One test exists purely to pin a hardware finding: ``power inline reset`` is
accepted by SW8P firmware V705R002C013 and never interrupts power, so the
driver must not use it. That is invisible in a code review and cheap to
"optimise" back in.

Loads the driver + simulator with the ``openavc.*`` imports stubbed so the
community CI stays self-contained (conftest.py rolls the stubs back after this
module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    CommandParamError,
    StubBaseDriver,
    StubEvents,
    StubState,
    install_stubs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "utility" / "avproedge_mxnet_switch.py"
SIM_PATH = REPO_ROOT / "utility" / "avproedge_mxnet_switch_sim.py"

sys.path.insert(0, str(REPO_ROOT / "tests" / "fixtures"))
import avproedge_mxnet_switch_outputs as fx  # noqa: E402


class _FakeBaseDriver(StubBaseDriver, LifecycleFake):
    """State + child registry from the shared stub; lifecycle from the shared
    lifecycle fake. This driver's own connect path is exercised through
    ``_post_connect``, which the round-trip test drives directly."""

    DRIVER_INFO: dict = {}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install_stubs(base_driver=_FakeBaseDriver)
driver_mod = _load("avproedge_mxnet_switch", DRIVER_PATH)
sim_mod = _load("avproedge_mxnet_switch_sim", SIM_PATH)

Driver = driver_mod.AVProEdgeMXnetSwitchDriver
Simulator = sim_mod.AVProEdgeMXnetSwitchSimulator


# ────────────────────────── parsers vs real hardware bytes ──────────────────

def test_version_parses_identity_off_the_device():
    info = driver_mod.parse_version(fx.SHOW_VERSION)
    assert info["model"] == "AC-MXNET-SW8P"
    assert info["firmware_version"] == "V705R002C013"
    assert info["boot_version"] == "7.5.15"
    assert info["hardware_version"] == "1.0.2"
    assert info["serial_number"] == "SW100126030400001"
    # The driver reports the VLAN MAC (the switch's L3 identity), not the CPU MAC.
    assert info["mac_address"] == "18:8a:6a:00:00:01"
    assert "weeks" in info["uptime"]


def test_interface_status_covers_every_port_including_the_sfp_cage():
    ports = driver_mod.parse_interface_status(fx.SHOW_INTERFACE_STATUS)
    assert len(ports) == 12
    assert ports["1/0/1"]["link_status"] == "up"
    assert ports["1/0/1"]["speed"] == "a-1G"
    assert ports["1/0/1"]["duplex"] == "a-FULL"
    assert ports["1/0/2"]["link_status"] == "down"
    # 1-8 are copper, 9-12 are the SFP+ cage.
    assert ports["1/0/8"]["media_type"] == "G-TX"
    assert ports["1/0/9"]["media_type"] == "SFP+"
    # Nothing is administratively down on a factory switch.
    assert {p["admin_status"] for p in ports.values()} == {"enabled"}


def test_an_administratively_down_port_reads_as_disabled():
    """The switch marks admin-down in the Link column ("A-Down"), not in a
    column of its own -- so this is easy to parse as merely 'down'."""
    text = (
        "Interface       Link/Protocol  Speed   Duplex  Vlan   Type      Alias Name\n"
        "1/0/4           A-Down/DOWN    auto    auto    1      G-TX            \n"
    )
    port = driver_mod.parse_interface_status(text)["1/0/4"]
    assert port["admin_status"] == "disabled"
    assert port["link_status"] == "down"


def test_poe_table_reads_watts_class_and_the_non_poe_ports():
    poe = driver_mod.parse_poe_ports(fx.SHOW_POWER_INLINE_INTERFACE)
    assert len(poe) == 12
    powered = poe["1/0/3"]
    assert powered["poe_capable"] is True
    assert powered["poe_admin"] == "enabled"
    assert powered["poe_status"] == "on"
    # The CLI prints milliwatts; the driver publishes watts.
    assert 3.0 <= powered["poe_power_w"] <= 5.0
    assert powered["poe_max_power_w"] == 30.0
    assert powered["poe_voltage_v"] == 52
    assert powered["poe_class"] == 3
    assert powered["poe_priority"] == "low"

    idle = poe["1/0/2"]
    assert idle["poe_status"] == "off"
    assert idle["poe_power_w"] == 0.0

    # The SFP+ ports answer with a sentence, not a row. They must still appear
    # as children, or the child roster stops matching the physical switch.
    for name in ("1/0/9", "1/0/10", "1/0/11", "1/0/12"):
        assert poe[name]["poe_capable"] is False
        assert poe[name]["poe_status"] == "n/a"


def test_poe_global_reads_the_power_budget():
    g = driver_mod.parse_poe_global(fx.SHOW_POWER_INLINE)
    assert g["poe_status"] == "on"
    assert g["poe_budget_w"] == 125.0
    assert g["poe_consumed_w"] + g["poe_remaining_w"] == g["poe_budget_w"]
    assert g["poe_police"] == "disabled"
    assert g["poe_legacy"] == "disabled"
    assert g["poe_pse_type"]


def test_single_port_poe_query_parses_like_the_full_table():
    one = driver_mod.parse_poe_ports(fx.SHOW_POWER_INLINE_PORT)
    assert list(one) == ["1/0/3"]
    assert one["1/0/3"]["poe_status"] == "on"


def test_health_readings():
    assert driver_mod.parse_temperature(fx.SHOW_TEMPERATURE)["temperature_c"] > 0
    cpu = driver_mod.parse_cpu(fx.SHOW_CPU_USAGE)
    # The switch reports IDLE; the driver publishes usage.
    assert 0 <= cpu["cpu_usage_5s"] <= 100
    assert cpu["cpu_usage_5s"] == pytest.approx(
        100 - int(_idle_from(fx.SHOW_CPU_USAGE)), abs=0.01)
    mem = driver_mod.parse_memory(fx.SHOW_MEMORY_USAGE)
    assert mem["memory_total_mb"] == 256
    assert 0 < mem["memory_usage_percent"] < 100


def _idle_from(text: str) -> str:
    import re
    return re.search(r"Last\s+5\s*second CPU IDLE:\s*(\d+)", text).group(1)


def test_igmp_snooping_global_state():
    ig = driver_mod.parse_igmp_snooping(fx.SHOW_IGMP_SNOOPING)
    assert ig["igmp_snooping"] is True
    assert ig["igmp_querier"] is True
    assert ig["igmp_snooping_vlans"] == "1"


def test_igmp_membership_inverts_the_group_major_table():
    """The membership table is group-major with continuation rows carrying no
    group of their own -- the group has to carry down, or every second endpoint
    is dropped."""
    rows = driver_mod.parse_igmp_groups(fx.SHOW_IGMP_SNOOPING_VLAN)
    assert rows, "no membership rows parsed"
    by_iface: dict[str, set[str]] = {}
    for row in rows:
        by_iface.setdefault(row["interface"], set()).add(row["group"])
    # The two MXnet endpoints are each joined to the same AV control groups,
    # and the continuation-row endpoint must not be lost.
    assert "1/0/1" in by_iface and "1/0/3" in by_iface
    assert by_iface["1/0/1"] == by_iface["1/0/3"]
    assert len(by_iface["1/0/1"]) >= 5
    assert all(g.count(".") == 3 for g in by_iface["1/0/1"])


def test_mac_table_maps_endpoints_to_ports():
    table = driver_mod.parse_mac_table(fx.SHOW_MAC_ADDRESS_TABLE)
    assert table["1/0/3"] == ["18:8a:6a:00:00:11"]
    assert table["1/0/1"] == ["18:8a:6a:00:00:12"]
    # The switch's own CPU entry is on "CPU", not on a port, and must not be
    # attributed to one.
    assert all(iface.count("/") == 2 for iface in table)


def test_endpoint_kind_only_claims_what_the_switch_can_know():
    # The switch sees MACs. It cannot tell an encoder from a decoder, and the
    # driver must not pretend otherwise.
    assert driver_mod._endpoint_kind("18:8a:6a:00:00:11") == "MXnet endpoint"
    assert driver_mod._endpoint_kind("aa:bb:cc:00:00:21") == "other"
    assert driver_mod._endpoint_kind("") == ""


def test_port_counters_include_errors_and_rates():
    ctr = driver_mod.parse_port_counters(fx.SHOW_INTERFACE_RANGE)
    assert set(ctr) == {"1/0/1", "1/0/2", "1/0/3"}
    live = ctr["1/0/1"]
    assert live["rx_packets"] > 0 and live["tx_packets"] > 0
    assert live["input_errors"] == 0 and live["crc_errors"] == 0
    # The rate lines sit ABOVE the Input/Output statistics headers in each
    # block; a parser that waits for those headers silently loses them.
    assert "rx_rate_bps" in live and "tx_rate_bps" in live


def test_vlan_and_transceiver_parsers():
    assert driver_mod.parse_vlan_count(fx.SHOW_VLAN) == 1
    # No SFP modules fitted -> a header with no rows, which must not throw.
    assert driver_mod.parse_transceivers(fx.SHOW_TRANSCEIVER) == {}


@pytest.mark.parametrize("iface,expected", [
    ("1/0/1", 10001),
    ("1/0/12", 10012),
    ("2/0/1", 20001),      # a stacked switch's second member
    ("Vlan1", None),
    ("port-channel1", None),
])
def test_child_ids_are_stable_and_reject_non_physical_interfaces(iface, expected):
    assert driver_mod._iface_to_id(iface) == expected


# ────────────────────────── command surface ──────────────────────────

def test_every_declared_command_has_a_dispatch_branch():
    """A declared command whose send_command falls through answers
    {"success": true, "result": null} -- byte-identical to one that worked. No
    gate catches it, so it is asserted here."""
    source = DRIVER_PATH.read_text(encoding="utf-8")
    handled = set(driver_mod._PORT_COMMANDS)
    for name in Driver.DRIVER_INFO["commands"]:
        if name in handled:
            continue
        assert f'command == "{name}"' in source, (
            f"command {name!r} is declared but send_command never branches on it")


def test_quick_actions_name_real_commands():
    commands = Driver.DRIVER_INFO["commands"]
    for action in Driver.DRIVER_INFO["quick_actions"]:
        assert action in commands, f"quick action {action!r} is not a command"


def test_every_port_command_targets_a_child_id():
    for name in driver_mod._PORT_COMMANDS:
        params = Driver.DRIVER_INFO["commands"][name]["params"]
        assert params["port"]["type"] == "child_id"
        assert params["port"]["child_type"] == "port"


def test_device_settings_are_backed_by_polled_state():
    """A device setting whose state_key is not a declared state variable can
    never read back, so the editor shows a value the device never confirmed."""
    declared = Driver.DRIVER_INFO["state_variables"]
    for key, spec in Driver.DRIVER_INFO["device_settings"].items():
        assert spec["state_key"] in declared, key


def test_child_summary_fields_exist_in_the_child_schema():
    port_type = Driver.DRIVER_INFO["child_entity_types"]["port"]
    schema = port_type["state_variables"]
    for field in port_type["summary_fields"]:
        assert field in schema, field
    assert port_type["label_field"] in schema


def test_the_driver_does_not_use_power_inline_reset():
    """Pinning a hardware finding.

    ``power inline reset`` is the obvious way to power-cycle a port and the
    firmware accepts it, but on an SW8P (V705R002C013) it never interrupts
    power -- measured against a live 4 W encoder, voltage and class held and
    the link never dropped. The driver cuts and restores power instead. If
    someone "simplifies" it back to the reset command, PoE recovery silently
    stops working in the field while every test still passes.
    """
    source = DRIVER_PATH.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    _docstrings_stripped = code.split('"""')
    executable = "".join(_docstrings_stripped[::2])
    assert "power inline reset" not in executable
    assert "no power inline enable" in executable


# ────────────────────────── driver <-> simulator round trip ──────────────────

class _PipeTransport:
    """In-memory pipe between the driver and the simulator.

    The driver writes whole command lines; the simulator answers with the bytes
    a real switch would put on the wire, and they are handed straight back to
    the driver's own byte-stream handler. That means the driver's prompt
    framing -- the part that is genuinely tricky -- is under test, not bypassed.
    """

    def __init__(self, sim, driver) -> None:
        self._sim = sim
        self._driver = driver
        self.connected = True
        self.last_error = ""

    async def send(self, data: bytes) -> None:
        text = data.decode("latin-1")
        for line in text.replace("\r\n", "\n").split("\n"):
            if not line and text.strip():
                continue
            reply = self._sim.handle_command(line.strip().encode("latin-1"))
            if reply:
                await self._driver.on_data_received(reply)

    async def close(self) -> None:
        self.connected = False


async def _connected_pair():
    state, events = StubState(), StubEvents()
    driver = Driver("sw1", {"host": "10.0.0.5", "username": "admin",
                            "password": "admin", "poe_cycle_seconds": 1},
                    state, events)
    sim = Simulator("sim1")
    transport = _PipeTransport(sim, driver)
    driver.transport = transport
    # The switch speaks first: its login prompt is the connect banner.
    banner = await sim.on_client_connected("c1")
    await driver.on_data_received(banner)
    await driver._post_connect()
    return driver, sim, state


@pytest.mark.asyncio
async def test_connect_logs_in_and_reads_identity():
    driver, _sim, state = await _connected_pair()
    assert state.data["device.sw1.model"] == "AC-MXNET-SW8P"
    assert state.data["device.sw1.firmware_version"] == "V705R002C013"


@pytest.mark.asyncio
async def test_bad_credentials_surface_as_a_connection_error():
    """The simulator rejects the credential 'invalid' the way the switch
    rejects a wrong password: it re-prompts. A driver that reads a re-prompt as
    success hangs on its first query instead of reporting an auth failure."""
    state, events = StubState(), StubEvents()
    driver = Driver("sw2", {"host": "10.0.0.5", "username": "admin",
                            "password": "invalid"}, state, events)
    sim = Simulator("sim2")
    driver.transport = _PipeTransport(sim, driver)
    await driver.on_data_received(await sim.on_client_connected("c1"))
    with pytest.raises(ConnectionError, match="Login failed"):
        await driver._post_connect()


@pytest.mark.asyncio
async def test_poll_registers_every_port_as_a_child():
    driver, _sim, state = await _connected_pair()
    await driver.poll()
    ports = driver.list_children("port")
    assert len(ports) == 12
    assert state.data["device.sw1.port_count"] == 12
    # 1/0/1 decoder, 1/0/3 encoder, 1/0/5 control PC, 1/0/7 uplink.
    assert state.data["device.sw1.ports_up"] == 4
    assert state.data["device.sw1.poe_ports_delivering"] == 2
    # PoE budget came from the global query in the same poll.
    assert state.data["device.sw1.poe_budget_w"] == 125.0


@pytest.mark.asyncio
async def test_slow_poll_fills_multicast_and_endpoint_mapping():
    driver, _sim, state = await _connected_pair()
    await driver._poll_fast()
    await driver._poll_slow()
    enc = driver.get_child_state("port", 10003)
    assert enc["connected_mac"] == "18:8a:6a:00:00:11"
    assert enc["connected_kind"] == "MXnet endpoint"
    assert enc["multicast_groups"] >= 5
    assert "225.1.0.0" in enc["multicast_group_list"]
    # The control PC is not MXnet gear and must not be counted as an endpoint.
    pc = driver.get_child_state("port", 10005)
    assert pc["connected_kind"] == "other"
    assert state.data["device.sw1.mxnet_endpoints"] == 2
    assert state.data["device.sw1.igmp_snooping"] is True
    assert enc["crc_errors"] == 0


@pytest.mark.asyncio
async def test_poe_power_cycle_actually_cuts_power_and_restores_it():
    driver, sim, _state = await _connected_pair()
    await driver._poll_fast()
    assert driver.get_child_state("port", 10003)["poe_status"] == "on"

    seen: list[int] = []
    original = sim._ports["1/0/3"]

    async def _watch():
        # Sample while power is off, before the driver restores it.
        await asyncio.sleep(0.3)
        seen.append(original["poe_mw"])

    watcher = asyncio.ensure_future(_watch())
    result = await driver.send_command("poe_cycle_port", {"port": 10003})
    await watcher

    assert seen == [0], "power was never actually cut during the cycle"
    assert "power-cycled" in result
    await driver._poll_fast()
    assert driver.get_child_state("port", 10003)["poe_status"] == "on"


@pytest.mark.asyncio
async def test_poe_enable_disable_round_trips_through_the_switch():
    driver, _sim, _state = await _connected_pair()
    await driver._poll_fast()
    await driver.send_command("poe_disable_port", {"port": 10003})
    await driver._poll_fast()
    assert driver.get_child_state("port", 10003)["poe_admin"] == "disabled"
    assert driver.get_child_state("port", 10003)["poe_power_w"] == 0.0
    await driver.send_command("poe_enable_port", {"port": 10003})
    await driver._poll_fast()
    assert driver.get_child_state("port", 10003)["poe_admin"] == "enabled"


@pytest.mark.asyncio
async def test_port_shutdown_and_description_reach_the_switch():
    driver, _sim, _state = await _connected_pair()
    await driver._poll_fast()
    await driver.send_command("port_disable", {"port": 10001})
    await driver._poll_fast()
    assert driver.get_child_state("port", 10001)["admin_status"] == "disabled"
    await driver.send_command("port_enable", {"port": 10001})

    await driver.send_command("set_port_description",
                              {"port": 10003, "description": "Encoder Rack A"})
    await driver._poll_fast()
    # Case must survive: it is a label a person typed.
    assert driver.get_child_state("port", 10003)["description"] == "Encoder Rack A"


@pytest.mark.asyncio
async def test_poe_max_power_converts_watts_to_the_milliwatts_the_cli_wants():
    driver, sim, _state = await _connected_pair()
    await driver._poll_fast()
    await driver.send_command("set_poe_max_power", {"port": 10003, "watts": 15})
    assert sim._ports["1/0/3"]["poe_max_mw"] == 15000
    await driver._poll_fast()
    assert driver.get_child_state("port", 10003)["poe_max_power_w"] == 15.0


@pytest.mark.asyncio
async def test_poe_priority_and_unknown_commands():
    driver, sim, _state = await _connected_pair()
    await driver._poll_fast()
    await driver.send_command("set_poe_priority",
                              {"port": 10003, "priority": "critical"})
    assert sim._ports["1/0/3"]["poe_priority"] == "critical"
    with pytest.raises(ValueError, match="Unknown command"):
        await driver.send_command("no_such_command", {})


@pytest.mark.asyncio
async def test_device_settings_write_through_and_read_back():
    driver, _sim, state = await _connected_pair()
    await driver.set_device_setting("poe_police", "enabled")
    assert state.data["device.sw1.poe_police"] == "enabled"
    await driver._poll_fast()          # confirmed by the device, not just set
    assert state.data["device.sw1.poe_police"] == "enabled"
    await driver.set_device_setting("poe_legacy", "enabled")
    await driver._poll_fast()
    assert state.data["device.sw1.poe_legacy"] == "enabled"
    with pytest.raises(ValueError, match="Unknown device setting"):
        await driver.set_device_setting("nope", "x")


@pytest.mark.asyncio
async def test_a_command_naming_an_unknown_port_is_refused():
    driver, _sim, _state = await _connected_pair()
    await driver._poll_fast()
    with pytest.raises(ValueError, match="not a known interface"):
        await driver.send_command("poe_enable_port", {"port": 19999})


@pytest.mark.asyncio
async def test_refresh_children_reconciles_the_roster():
    driver, _sim, _state = await _connected_pair()
    result = await driver.refresh_children()
    assert result == {"ports": 12}


@pytest.mark.asyncio
async def test_the_port_range_query_is_one_round_trip_for_every_port():
    driver, _sim, _state = await _connected_pair()
    await driver._poll_fast()
    assert driver._port_range() == "1/0/1-12"


@pytest.mark.asyncio
async def test_save_config_and_reboot_answer_their_prompts():
    driver, _sim, _state = await _connected_pair()
    assert "OK" in await driver.send_command("save_config", {})
    assert "Reboot requested" in await driver.send_command("reboot", {})


# ── cutting power to the port you are talking through ────────────────────────
#
# poe_cycle_port restores power from a finally block so a cancelled wait cannot
# leave an endpoint dark. That protects against task teardown, not against the
# case that actually happened on hardware: cut the port the control session
# runs over and the restore has nowhere to go. It left Ethernet1/0/1 disabled
# and reported a plain command failure while the endpoint visibly cycled.
#
# An MXnet endpoint is one device on one port, so several MACs behind a port
# means a switch, a control box or the building LAN is on the far side. On the
# bench the uplink carried 27 MACs against exactly one per endpoint port, so
# this is not a fine judgement.

@pytest.mark.asyncio
async def test_cycling_an_endpoint_port_is_not_obstructed():
    """The guard must stay out of the way of the thing people actually do."""
    driver, _sim, _state = await _connected_pair()
    await driver.poll()
    result = await driver.send_command("poe_cycle_port", {"port": 10003})
    assert "power-cycled" in result.lower()


@pytest.mark.asyncio
async def test_cycling_the_uplink_is_refused_and_says_what_is_behind_it():
    driver, _sim, _state = await _connected_pair()
    await driver.poll()
    with pytest.raises(CommandParamError) as caught:
        await driver.send_command("poe_cycle_port", {"port": 10007})
    message = str(caught.value)
    assert "4 devices" in message
    assert "uplink" in message.lower()
    # It has to name the way out, or it is just an obstacle.
    assert "force" in message.lower()


@pytest.mark.asyncio
async def test_the_uplink_can_still_be_cut_deliberately():
    driver, _sim, _state = await _connected_pair()
    await driver.poll()
    result = await driver.send_command(
        "poe_cycle_port", {"port": 10007, "force": True})
    assert "power-cycled" in result.lower()


@pytest.mark.asyncio
async def test_disabling_poe_on_the_uplink_is_refused_too():
    """poe_disable_port has the same hazard with no restore at all."""
    driver, _sim, _state = await _connected_pair()
    await driver.poll()
    with pytest.raises(CommandParamError):
        await driver.send_command("poe_disable_port", {"port": 10007})


@pytest.mark.asyncio
async def test_shutting_down_the_uplink_is_refused_too():
    """`shutdown` drops the link even where PoE never powered anything."""
    driver, _sim, _state = await _connected_pair()
    await driver.poll()
    with pytest.raises(CommandParamError):
        await driver.send_command("port_disable", {"port": 10007})


@pytest.mark.asyncio
async def test_an_unreadable_mac_table_does_not_block_the_command():
    """Fails OPEN. A driver that refuses whenever a parse looks unfamiliar is
    worse than the hazard: it breaks a working feature on every firmware whose
    table is formatted a little differently."""
    driver, _sim, _state = await _connected_pair()
    await driver.poll()

    async def _explode(_wire, *a, **k):
        raise RuntimeError("unfamiliar output")

    driver._send_request = _explode  # type: ignore[method-assign]
    # The guard swallows it and lets the command through; the read itself is
    # deliberately left to raise, so the swallow has exactly one home.
    await driver._refuse_if_uplink("1/0/7", {}, "Power-cycling PoE")


@pytest.mark.asyncio
async def test_an_undeliverable_restore_says_the_port_is_still_dark():
    """The message has to name the port and the state it was left in -- the
    operator has to go and re-enable it by hand."""
    driver, _sim, _state = await _connected_pair()
    await driver.poll()

    calls: list[list[str]] = []
    original = driver._interface_config

    async def _cut_then_die(iface, lines):
        calls.append(list(lines))
        if lines == ["power inline enable"]:
            raise ConnectionError("the path this ran over is gone")
        return await original(iface, lines)

    driver._interface_config = _cut_then_die  # type: ignore[method-assign]

    with pytest.raises(driver_mod.CommandPartialError) as caught:
        await driver.send_command("poe_cycle_port", {"port": 10003})

    message = str(caught.value)
    assert "1/0/3" in message
    assert "power inline enable" in message
    # The cut DID happen; saying "failed to send command" would be false.
    assert ["no power inline enable"] in calls
