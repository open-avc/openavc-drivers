"""Integration tests for the NETGEAR M4250/M4350 driver against its simulator.

Loads the real driver (``utility/netgear_m4250_m4350.py``) and its simulator
(``utility/netgear_m4250_m4350_sim.py``) and exercises the real
connect -> enable -> poll -> child -> command path over a raw TCP/telnet
connection to the simulator.

This drives the actual platform runtime (StateStore, EventBus, BaseDriver,
TCPTransport) and the simulator subsystem, so it needs ``openavc`` (server +
simulator) importable. In this repo's isolated CI it skips cleanly; it runs in
the workspace where openavc is installed alongside.

Async work runs via ``asyncio.run()`` in sync tests, matching the other driver
integration tests in this repo.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    from server.core.event_bus import EventBus
    from server.core.state_store import StateStore
    # The production simulator imports the ``simulator`` package, a top-level
    # dir under openavc/ that isn't pip-installed (only ``server`` is). Add the
    # sibling openavc/ so it resolves when run from the workspace; in isolated
    # CI the ``server`` import above already fails first and the module skips.
    _OPENAVC = REPO_ROOT.parent / "openavc"
    if _OPENAVC.is_dir() and str(_OPENAVC) not in sys.path:
        sys.path.insert(0, str(_OPENAVC))
    _driver_mod = _load_module(
        "_netgear_driver", REPO_ROOT / "utility" / "netgear_m4250_m4350.py")
    _sim_mod = _load_module(
        "_netgear_sim", REPO_ROOT / "utility" / "netgear_m4250_m4350_sim.py")
except ModuleNotFoundError:
    pytest.skip(
        "NETGEAR integration test requires the openavc platform "
        "(run from the workspace with openavc installed)",
        allow_module_level=True,
    )

NetgearDriver = _driver_mod.NetgearM4250M4350Driver
NetgearSimulator = _sim_mod.NetgearM4250M4350Simulator

PORT = 18097
DEV = "netgear_test"


def _k(suffix: str) -> str:
    return f"device.{DEV}.{suffix}"


def _port_k(iface_id: int, prop: str) -> str:
    return f"device.{DEV}.port.{iface_id:05d}.{prop}"


async def _run_scenario(scenario):
    sim = NetgearSimulator("netgear_sim", {})
    await sim.start(PORT)
    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
    d = NetgearDriver(
        device_id=DEV,
        config={
            "host": "127.0.0.1",
            "port": PORT,
            "transport": "tcp",   # telnet path to the simulator
            "poll_interval": 0,   # drive poll() manually
            "verify_timeout": 0,
        },
        state=state,
        events=events,
    )
    await d.connect()
    try:
        return await scenario(d, state, sim)
    finally:
        try:
            await d.disconnect()
        except Exception:
            pass
        await sim.stop()


def _scenario(scenario):
    asyncio.run(_run_scenario(scenario))


# ── connect / identity ──

def test_connect_and_identity():
    async def s(d, state, sim):
        assert state.get(_k("connected")) is True
        assert state.get(_k("model")) == "M4350-24X4F"
        assert state.get(_k("series")) == "M4350"
        assert state.get(_k("firmware_version")) == "14.0.6.17"
    _scenario(s)


# ── fast poll: ports + PoE ──

def test_poll_registers_ports():
    async def s(d, state, sim):
        await d.poll()
        assert state.get(_k("port_count")) == 10
        # 1/0/1, 1/0/2, 1/0/3, 1/0/49 are up
        assert state.get(_k("ports_up")) == 4
        # PoE delivering on 1/0/1 and 1/0/2
        assert state.get(_k("poe_ports_delivering")) == 2
    _scenario(s)


def test_port_child_state():
    async def s(d, state, sim):
        await d.poll()
        cid = _driver_mod._iface_to_id("1/0/2")  # 10002
        assert state.get(_port_k(cid, "interface")) == "1/0/2"
        assert state.get(_port_k(cid, "link_status")) == "up"
        assert state.get(_port_k(cid, "speed")) == "1000 Full"
        assert state.get(_port_k(cid, "poe_status")) == "Delivering Power"
        assert state.get(_port_k(cid, "poe_power_w")) == 3.8
        assert state.get(_port_k(cid, "poe_admin")) == "enabled"
        assert state.get(_port_k(cid, "poe_priority")) == "high"
    _scenario(s)


def test_poe_global_state():
    async def s(d, state, sim):
        await d.poll()
        assert state.get(_k("poe_status")) == "ON"
        assert state.get(_k("poe_total_power_w")) == 720.0
        # consumed = 4.0 + 3.8 delivering ports
        assert state.get(_k("poe_consumed_power_w")) == 7.8
    _scenario(s)


# ── slow poll: health, multicast, neighbors, enrichment ──

def test_slow_poll_health_and_multicast():
    async def s(d, state, sim):
        await d.poll()
        await d._poll_slow()
        assert state.get(_k("temperature_c")) == 41
        assert state.get(_k("fan_status")) == "OK"
        assert state.get(_k("psu_status")) == "OK"
        assert state.get(_k("igmp_snooping")) is True
        assert state.get(_k("igmp_querier")) is True
        assert state.get(_k("igmp_querier_address")) == "10.20.0.1"
        assert state.get(_k("vlan_count")) == 4
        assert state.get(_k("multicast_group_count")) == 2
        assert state.get(_k("psu_redundancy")) == "Active"
    _scenario(s)


def test_slow_poll_enriches_ports():
    async def s(d, state, sim):
        await d.poll()
        await d._poll_slow()
        cid = _driver_mod._iface_to_id("1/0/2")
        assert state.get(_port_k(cid, "description")) == "Display Left"
        assert state.get(_port_k(cid, "media_type")) == "RJ45"
        assert state.get(_port_k(cid, "vlan")) == "20"
        assert state.get(_port_k(cid, "lldp_system_name")) == "Display-Left"
        assert state.get(_port_k(cid, "av_profile")) == "NDI"
        assert state.get(_port_k(cid, "multicast_groups")) == 1
    _scenario(s)


# ── commands ──

def test_poe_disable_then_poll_reflects():
    async def s(d, state, sim):
        await d.poll()
        cid = _driver_mod._iface_to_id("1/0/2")
        await d.send_command("poe_disable_port", {"port": str(cid)})
        # Optimistic state immediately.
        assert state.get(_port_k(cid, "poe_admin")) == "disabled"
        # And the next poll confirms it from the device.
        await d.poll()
        assert state.get(_port_k(cid, "poe_admin")) == "disabled"
        assert sim._ports["1/0/2"]["poe_admin"] == "Disable"
    _scenario(s)


def test_port_disable_then_poll_reflects():
    async def s(d, state, sim):
        await d.poll()
        cid = _driver_mod._iface_to_id("1/0/3")
        await d.send_command("port_disable", {"port": cid})
        await d.poll()
        assert state.get(_port_k(cid, "admin_status")) == "disabled"
        assert state.get(_port_k(cid, "link_status")) == "down"
    _scenario(s)


def test_poe_cycle_port_runs():
    async def s(d, state, sim):
        await d.poll()
        cid = _driver_mod._iface_to_id("1/0/1")
        # Should complete without raising (configure -> interface -> poe reset).
        await d.send_command("poe_cycle_port", {"port": cid})
    _scenario(s)


def test_set_poe_priority():
    async def s(d, state, sim):
        await d.poll()
        cid = _driver_mod._iface_to_id("1/0/1")
        await d.send_command("set_poe_priority", {"port": cid, "priority": "crit"})
        assert sim._ports["1/0/1"]["poe_priority"] == "Crit"
    _scenario(s)


def test_set_port_description_multiword():
    async def s(d, state, sim):
        await d.poll()
        cid = _driver_mod._iface_to_id("1/0/3")
        await d.send_command(
            "set_port_description", {"port": cid, "description": "Front Cam 2"})
        # Optimistic state shows the raw (unquoted) text immediately.
        assert state.get(_port_k(cid, "description")) == "Front Cam 2"
        # The driver sends it quoted, so the multi-word value is accepted and
        # the switch stores it without the surrounding quotes.
        assert sim._ports["1/0/3"]["description"] == "Front Cam 2"
        # The slow poll reads the description back unchanged from the device.
        await d.poll()
        await d._poll_slow()
        assert state.get(_port_k(cid, "description")) == "Front Cam 2"
    _scenario(s)


def test_reboot():
    async def s(d, state, sim):
        # reload prompts (y/n); the driver must detect the confirm and answer y.
        result = await d.send_command("reboot")
        assert "Reload" in result
        await asyncio.sleep(0.15)  # let the fire-and-forget 'y' round-trip
        assert sim._await_reload is False  # the 'y' confirm was answered
    _scenario(s)


def test_save_config():
    async def s(d, state, sim):
        save = await d.send_command("save_config")
        assert "startup-config" in save
    _scenario(s)


def test_cable_test():
    async def s(d, state, sim):
        await d.poll()
        cid = _driver_mod._iface_to_id("1/0/1")
        result = await d.send_command("cable_test", {"port": cid})
        assert result["cable_status"] == "Normal"
    _scenario(s)


def test_refresh_children():
    async def s(d, state, sim):
        summary = await d.refresh_children()
        assert summary["ports"] == 10
    _scenario(s)


def test_device_settings():
    async def s(d, state, sim):
        await d.set_device_setting("poe_usage_threshold", 80)
        assert state.get(_k("poe_usage_threshold")) == 80
        await d.set_device_setting("poe_power_mgmt_mode", "static")
        assert state.get(_k("poe_power_mgmt_mode")) == "Static"
    _scenario(s)
