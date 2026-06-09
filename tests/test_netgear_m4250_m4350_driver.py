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


# ── telnet login fix + Enable SSH setup action ──
#
# These don't use the simulator (which skips login). They drive the driver's
# CLI framing against a scripted fake "switch" that replays the real User: /
# Password: login and the captured enable-SSH command sequence
# (Netgear/captures/after-setup, factory/00-forced-password-set).


def _new_driver():
    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
    d = NetgearDriver(
        device_id=DEV,
        config={"host": "10.0.0.9", "port": 23, "transport": "tcp",
                "username": "admin", "password": "pw", "poll_interval": 0},
        state=state, events=events,
    )
    return d, state


class _ScriptedSwitch:
    """Fake transport replaying a CLI session. Each ``send()`` delivers the next
    scripted chunk back through the driver's ``on_data_received``. The login
    banner is delivered once, lazily, on the first send if not pre-delivered.
    """

    def __init__(self, on_data, script):
        self.connected = True
        self.last_error = ""
        self._on_data = on_data
        self._script = list(script)
        self.sent: list[bytes] = []

    async def send(self, data):
        self.sent.append(data)
        # A telnet IAC refusal (starts with 0xFF) isn't a command, so it must
        # not consume a scripted response — only real command lines advance.
        if data[:1] == b"\xff":
            return
        if self._script:
            await self._on_data(self._script.pop(0))

    async def close(self):
        self.connected = False


class _RecordingCtx:
    """Stands in for the platform's setup context (request_config_update /
    request_reconnect) so the handler runs outside the full runner."""

    def __init__(self):
        self.delta = None
        self.reconnected = False

    async def apply_config_update(self, delta):
        self.delta = dict(delta)

    async def reconnect(self):
        self.reconnected = True


def test_login_re_frames_user_and_password_prompts():
    async def run():
        d, _state = _new_driver()
        # A real switch's "User:" prompt frames as a "login" boundary...
        await d.on_data_received(b"\r\r\nUser: ")
        _text, kind = d._responses.get_nowait()
        assert kind == "login"
        # ...and "Password:" as a "password" boundary.
        await d.on_data_received(b"\r\nPassword: ")
        _text, kind = d._responses.get_nowait()
        assert kind == "password"
    asyncio.run(run())


def test_telnet_iac_negotiation_is_stripped_and_declined():
    async def run():
        d, _state = _new_driver()
        sent = []

        class _Rec:
            connected = True
            last_error = ""

            async def send(self, data):
                sent.append(data)

            async def close(self):
                self.connected = False

        d.transport = _Rec()
        IAC, DO, WILL, WONT, DONT, ECHO, SGA = 255, 253, 251, 252, 254, 1, 3
        # A real switch leads with IAC option negotiation, then the login prompt.
        await d.on_data_received(
            bytes([IAC, DO, ECHO, IAC, WILL, SGA]) + b"\r\r\nUser: ")
        # The prompt frames cleanly as "login" with no IAC bytes leaking in.
        text, kind = d._responses.get_nowait()
        assert kind == "login"
        assert "\xff" not in text
        # We declined both negotiated options.
        joined = b"".join(sent)
        assert bytes([IAC, WONT, ECHO]) in joined
        assert bytes([IAC, DONT, SGA]) in joined
    asyncio.run(run())


def test_telnet_iac_split_across_chunks_is_buffered():
    async def run():
        d, _state = _new_driver()

        class _Rec:
            connected = True
            last_error = ""

            async def send(self, data):
                pass

            async def close(self):
                pass

        d.transport = _Rec()
        IAC, DO, ECHO = 255, 253, 1
        # IAC DO ECHO split mid-sequence across two reads, then the prompt.
        await d.on_data_received(bytes([IAC, DO]))      # partial — buffered
        await d.on_data_received(bytes([ECHO]) + b"\r\nUser: ")
        text, kind = d._responses.get_nowait()
        assert kind == "login"
        assert "\xff" not in text
    asyncio.run(run())


def test_post_connect_drives_user_then_password_login():
    async def run():
        d, state = _new_driver()
        # After username -> Password:, after password -> CLI prompt, then
        # enable -> #, terminal length 0 -> #, show version -> identity + #.
        fake = _ScriptedSwitch(d.on_data_received, [
            b"\r\nPassword: ",
            b"\r\n(M4250-40G8XF-PoE+) >",
            b"\r\n(M4250-40G8XF-PoE+) #",
            b"\r\n(M4250-40G8XF-PoE+) #",
            b"\r\nMachine Model... M4250-40G8XF-PoE+\r\n(M4250-40G8XF-PoE+) #",
        ])
        d.transport = fake
        # Banner arrives before _post_connect starts waiting (no _clear there).
        await d.on_data_received(b"\r\r\nUser: ")
        await asyncio.wait_for(d._post_connect(), timeout=5.0)
        assert fake.sent[0].strip() == b"admin"   # username answered first
        assert fake.sent[1].strip() == b"pw"       # then the password
    asyncio.run(run())


def test_post_connect_rejects_bad_credentials():
    async def run():
        d, _state = _new_driver()
        # The switch re-prompts "User:" instead of dropping to the CLI -> auth
        # failure, surfaced as ConnectionError (not a 15s hang).
        fake = _ScriptedSwitch(d.on_data_received, [
            b"\r\nPassword: ",
            b"\r\nLogin incorrect\r\nUser: ",
        ])
        d.transport = fake
        await d.on_data_received(b"\r\r\nUser: ")
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(d._post_connect(), timeout=5.0)
    asyncio.run(run())


def _patch_create(monkeypatch_target, switches):
    """Replace TCPTransport.create with one that hands out scripted switches in
    order, scheduling each one's banner so it lands after the handler's _clear.
    Returns a restore callable."""
    original = monkeypatch_target.create
    seq = list(switches)

    async def fake_create(**kwargs):
        sw = seq.pop(0)
        sw._on_data = kwargs["on_data"]
        # Defer the banner to the next yield (after the handler's _clear()).
        # Lead with real telnet IAC negotiation (IAC DO ECHO, IAC WILL SGA) so
        # the wizard is exercised against the same option negotiation a real
        # M4250 sends before the "User:" prompt.
        async def _banner():
            iac = bytes([255, 253, 1, 255, 251, 3])  # IAC DO ECHO, IAC WILL SGA
            await sw._on_data(iac + b"\r\r\nUser: ")
        asyncio.create_task(_banner())
        return sw

    monkeypatch_target.create = staticmethod(fake_create)
    return lambda: setattr(monkeypatch_target, "create", original)


def test_enable_ssh_action_enables_and_switches_to_ssh():
    async def run():
        d, _state = _new_driver()
        # Login (User:/Password:) -> CLI '>', then enable -> '#', then the
        # captured enable-SSH sequence, then write memory.
        sw = _ScriptedSwitch(None, [
            b"\r\nPassword: ",                                  # after username
            b"\r\n(M4250-40G8XF-PoE+) >",                       # after password
            b"\r\n(M4250-40G8XF-PoE+) #",                       # after enable
            b"\r\n(M4250-40G8XF-PoE+) (Config)#",               # after configure
            b"\r\nRSA key generation complete.\r\n(M4250-40G8XF-PoE+) (Config)#",
            b"\r\nECDSA key generation complete.\r\n(M4250-40G8XF-PoE+) (Config)#",
            b"\r\n(M4250-40G8XF-PoE+) #",                       # after exit
            b"\r\n(M4250-40G8XF-PoE+) #",                       # after ip ssh server enable
            b"\r\n(M4250-40G8XF-PoE+) #",                       # after write memory
        ])
        restore = _patch_create(_driver_mod.TCPTransport, [sw])
        ctx = _RecordingCtx()
        d._set_setup_context(ctx)
        steps = []

        async def progress(step, pct=None):
            steps.append(step)

        try:
            result = await asyncio.wait_for(
                d.run_setup_action("enable_ssh",
                                   {"username": "admin", "password": "pw"},
                                   progress),
                timeout=10.0)
        finally:
            restore()

        assert result["ssh_enabled"] is True
        # Skip telnet IAC refusals (0xFF…); keep the command lines.
        sent = [b.strip().decode() for b in sw.sent if not b.startswith(b"\xff")]
        assert "ip ssh server enable" in sent
        assert "crypto key generate rsa 2048" in sent
        assert "write memory confirm" in sent
        # Flipped to SSH password auth and reconnected.
        assert ctx.delta["transport"] == "ssh"
        assert ctx.delta["port"] == 22
        assert ctx.delta["ssh_auth_method"] == "password"
        assert ctx.delta["password"] == "pw"
        assert ctx.reconnected is True
        assert steps  # progress was reported
    asyncio.run(run())


def test_enable_ssh_action_handles_forced_password_change():
    async def run():
        d, _state = _new_driver()
        # First session: blank/factory password triggers a forced change, then
        # the switch logs us out.
        sw1 = _ScriptedSwitch(None, [
            b"\r\nPassword: ",                                  # after username
            # after default password: forced change
            b"\r\nDefault password authentication successful.\r\n"
            b"Change the default password for user 'admin'.\r\nNew password: ",
            b"\r\nRe-enter new password: ",                    # after new password
            # after confirm: success, then the switch re-presents the login.
            b"\r\nPassword change is successful.\r\nLog in again.\r\nUser: ",
        ])
        # Second session: log in with the new password, then enable SSH.
        sw2 = _ScriptedSwitch(None, [
            b"\r\nPassword: ",                                  # after username
            b"\r\n(M4250) >",                                   # after new password
            b"\r\n(M4250) #",                                   # after enable
            b"\r\n(M4250) (Config)#",                           # configure
            b"\r\nRSA key generation complete.\r\n(M4250) (Config)#",
            b"\r\nECDSA key generation complete.\r\n(M4250) (Config)#",
            b"\r\n(M4250) #",                                   # exit
            b"\r\n(M4250) #",                                   # ip ssh server enable
            b"\r\n(M4250) #",                                   # write memory
        ])
        restore = _patch_create(_driver_mod.TCPTransport, [sw1, sw2])
        ctx = _RecordingCtx()
        d._set_setup_context(ctx)

        async def progress(step, pct=None):
            return None

        try:
            result = await asyncio.wait_for(
                d.run_setup_action(
                    "enable_ssh",
                    {"username": "admin", "password": "", "new_password": "NewPass99"},
                    progress),
                timeout=10.0)
        finally:
            restore()

        assert result["ssh_enabled"] is True
        # The new password was set (sent twice on session 1) and used to flip SSH.
        assert sw1.sent.count(b"NewPass99\r\n") == 2
        assert ctx.delta["password"] == "NewPass99"
        assert ctx.reconnected is True
    asyncio.run(run())


def test_enable_ssh_forced_change_without_new_password_errors():
    async def run():
        d, _state = _new_driver()
        sw = _ScriptedSwitch(None, [
            b"\r\nPassword: ",
            b"\r\nChange the default password for user 'admin'.\r\nNew password: ",
        ])
        restore = _patch_create(_driver_mod.TCPTransport, [sw])
        d._set_setup_context(_RecordingCtx())

        async def progress(step, pct=None):
            return None

        try:
            with pytest.raises(Exception) as exc:
                await asyncio.wait_for(
                    d.run_setup_action(
                        "enable_ssh",
                        {"username": "admin", "password": ""},  # no new_password
                        progress),
                    timeout=10.0)
            assert "new admin password" in str(exc.value).lower()
        finally:
            restore()
    asyncio.run(run())
