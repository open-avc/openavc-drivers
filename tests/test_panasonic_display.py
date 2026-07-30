"""Driver + simulator tests for panasonic_display (Protocol 1 / Protocol 2).

No Panasonic display on hand, so correctness is proven two ways: metadata /
shape assertions, and dual-proof round trips wiring the real driver to the real
simulator logic over a REAL localhost TCP server — the driver manages its own
per-request connections (the display family closes the control connection
between commands), so the harness runs actual sockets rather than an in-memory
transport. A ``close_after_response`` server mode exercises the documented
disconnect-after-every-response behaviour (VF1H / SF2 / EQ1 / SQ1 / VF2 / BQ1
units).

Covers: both protocols' framing and MD5 recipes (auto-detected from the
greeting), protected / non-protected / wrong-password paths, standby query
gating, every device-setting round trip, the input-code pass-through, poll
propagation on an unreachable display (never-offline guard), and the
"Test Admin Credentials" setup wizard end-to-end.

Loads the driver + simulator with the ``server.*`` / ``simulator.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "displays" / "panasonic_display.py"
SIM_PATH = REPO_ROOT / "displays" / "panasonic_display_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver: the driver supplies
    lifecycle hooks (_pre_connect / _create_transport / _post_connect /
    _initial_sync / _close_session / _link_alive) and connect()/disconnect()
    here run them in the platform's order — clean slate, _pre_connect,
    transport build, _post_connect (with failure teardown), declare,
    _initial_sync (with full teardown on failure), then polling.
    ``connected`` is the platform's _link_alive-backed property (the driver
    overrides _link_alive to True for its session-per-request protocol)."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self.config_updates: list[dict] = []
        self.reconnects = 0

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    async def request_config_update(self, delta) -> None:
        self.config_updates.append(delta)
        self.config.update(delta)

    async def request_reconnect(self) -> None:
        self.reconnects += 1

    # -- lifecycle hooks (drivers override; defaults are no-ops) --

    async def _pre_connect(self) -> None:
        pass

    async def _post_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    async def _create_transport(self, transport_type) -> None:
        # The platform ladder isn't modeled — the driver under test
        # overrides this wholesale (session-per-request, transport None).
        raise NotImplementedError

    def _link_alive(self) -> bool:
        if self.transport is None:
            return False
        return bool(getattr(self.transport, "connected", False))

    @property
    def connected(self) -> bool:
        return self._connected and self._link_alive()

    # -- connection lifecycle (mirrors BaseDriver.connect/disconnect) --

    async def connect(self) -> None:
        # Clean slate: drop any stale session/transport from a previous
        # attempt before the hooks run.
        await self._close_session()
        if self.transport:
            try:
                await self.transport.close()
            except Exception:
                pass
            self.transport = None

        await self._pre_connect()
        await self._create_transport("tcp")
        # Transport verify stage skipped: hasattr(None, "verify") is False
        # for this driver's always-None transport.

        try:
            await self._post_connect()
            self._connected = True
            self.set_state("connected", True)
            await self.events.emit(f"device.connected.{self.device_id}")
        except Exception:
            if self.transport:
                await self.transport.close()
                self.transport = None
            await self._close_session()
            self._connected = False
            raise

        try:
            await self._initial_sync()
        except Exception:
            transport = self.transport
            self.transport = None
            if transport is not None:
                try:
                    await transport.close()
                except Exception:
                    pass
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise

        if self.config.get("poll_interval", 0) > 0:
            await self.start_polling(self.config["poll_interval"])

    async def disconnect(self) -> None:
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


class _FakeTCPSimulator:
    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self._clients: dict = {}

    def set_state(self, key, value) -> None:
        self.state[key] = value

    def get_state(self, key, default=None):
        return self.state.get(key, default)


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server
    for sub in ("drivers", "utils"):
        m = ModuleType(f"server.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"server.{sub}"] = m
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["server.drivers.base"] = base
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["simulator"] = sim_pkg
    sim_tcp = ModuleType("simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    sys.modules["simulator.tcp_simulator"] = sim_tcp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("panasonic_display_under_test", DRIVER_PATH)
SIM = _load("panasonic_display_sim_under_test", SIM_PATH)


# ── Real-socket harness ─────────────────────────────────────────────────────

class _SimServer:
    """Real localhost TCP server around the simulator logic. With
    ``close_after_response`` it mimics the display families that drop the
    connection after every command response."""

    def __init__(self, sim, close_after_response: bool = False) -> None:
        self.sim = sim
        self.close_after_response = close_after_response
        self.received: list[str] = []
        self._server = None
        self._next_client = 0
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader, writer) -> None:
        self._next_client += 1
        client_id = f"c{self._next_client}"
        self.sim._clients[client_id] = object()
        try:
            greeting = await self.sim.on_client_connected(client_id)
            if greeting:
                writer.write(greeting)
                await writer.drain()
            while True:
                try:
                    raw = await reader.readuntil(b"\r")
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break
                self.received.append(
                    raw.decode("ascii", errors="ignore").strip("\r")
                )
                resp = self.sim.handle_command(raw)
                if resp:
                    writer.write(resp)
                    await writer.drain()
                if self.close_after_response:
                    break
        finally:
            self.sim._clients.pop(client_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionResetError):
                pass


async def _make_pair(
    sim_config=None,
    driver_overrides=None,
    power="on",
    close_after_response=False,
):
    sim = SIM.PanasonicDisplaySimulator("sim1", sim_config or {})
    sim.set_state("power", power)
    server = _SimServer(sim, close_after_response=close_after_response)
    await server.start()

    cfg = {
        "host": "127.0.0.1",
        "port": server.port,
        "poll_interval": 0,
        "username": "admin1",
        "password": "",
    }
    cfg.update(driver_overrides or {})
    driver = DRV.PanasonicDisplayDriver(
        "disp1", cfg, _FakeState(), _FakeEvents()
    )
    return driver, sim, server


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_shape():
    info = DRV.PanasonicDisplayDriver.DRIVER_INFO
    assert info["id"] == "panasonic_display"
    assert info["category"] == "display"
    assert info["version"] == "1.0.1"

    ds = info["device_settings"]
    assert set(ds) == {
        "input", "picture_mode", "backlight", "contrast",
        "black_level", "color", "tint", "sharpness",
    }
    state_vars = info["state_variables"]
    for key, spec in ds.items():
        assert spec["state_key"] in state_vars, key
        assert spec["setup"] is False
    for k in ("backlight", "contrast", "black_level", "color",
              "tint", "sharpness"):
        assert ds[k]["min"] == 0 and ds[k]["max"] == 100
        assert f"{k}_set" in info["commands"]

    cmds = set(info["commands"])
    setup = [a for a in info["actions"] if a["kind"] == "setup"]
    for a in info["actions"]:
        if a["kind"] == "command":
            assert a["id"] in cmds
    assert len(setup) == 1 and setup[0]["id"] == "test_credentials"
    assert {"username", "password", "save"} <= set(setup[0]["params"])


def test_control_hints_declared():
    state_vars = DRV.PanasonicDisplayDriver.DRIVER_INFO["state_variables"]
    for key in ("power", "input", "volume", "audio_mute", "video_mute"):
        assert state_vars[key].get("control") is True, key
    assert state_vars["volume"]["min"] == 0
    assert state_vars["volume"]["max"] == 100


def test_discovery_probe_matches_both_greetings():
    disc = DRV.PanasonicDisplayDriver.DRIVER_INFO["discovery"]
    probe = disc["tcp_probe"]
    assert probe["port"] == 1024
    # Connect-only banner read: no send declared.
    assert not any(k.startswith("send") for k in probe)
    pattern = re.compile(probe["expect_regex"])
    assert pattern.search("NTCONTROL 0\r")
    assert pattern.search("NTCONTROL 1 04563b31\r")
    assert pattern.search("PDPCONTROL 0\r")
    assert pattern.search("PDPCONTROL 1 0174735b\r")
    assert not pattern.search("SOMETHING ELSE\r")


# ── Connect populates state (both protocols, non-protected) ─────────────────

@pytest.mark.parametrize("lan_protocol", ["1", "2"])
def test_connect_populates_state(lan_protocol):
    async def go():
        driver, sim, server = await _make_pair(
            sim_config={"lan_protocol": lan_protocol}, power="on"
        )
        sim.state.update({
            "input": "HM2", "volume": 37, "audio_mute": True,
            "video_mute": False, "aspect": "NATV", "picture_mode": "NAT",
            "backlight": 66, "contrast": 44, "black_level": 55,
            "color": 33, "tint": 22, "sharpness": 11,
        })
        assert driver.connected is False
        await driver.connect()
        try:
            # _link_alive is overridden to True (no persistent transport),
            # so the connected property reports True while connected.
            assert driver.connected is True
            assert driver.get_state("connected") is True
            assert "device.connected.disp1" in driver.events.emitted
            expected_proto = (
                "protocol1" if lan_protocol == "1" else "protocol2"
            )
            assert driver.get_state("lan_protocol") == expected_proto
            assert driver.get_state("auth_required") is False
            assert driver.get_state("power") == "on"
            assert driver.get_state("input") == "hdmi2"
            assert driver.get_state("volume") == 37
            assert driver.get_state("audio_mute") is True
            assert driver.get_state("video_mute") is False
            assert driver.get_state("aspect") == "real"
            assert driver.get_state("picture_mode") == "natural"
            assert driver.get_state("backlight") == 66
            assert driver.get_state("contrast") == 44
            assert driver.get_state("black_level") == 55
            assert driver.get_state("color") == 33
            assert driver.get_state("tint") == 22
            assert driver.get_state("sharpness") == 11
        finally:
            await driver.disconnect()
            await server.stop()
        assert driver.connected is False
        assert driver.get_state("connected") is False
        assert driver.events.emitted[-1] == "device.disconnected.disp1"

    asyncio.run(go())


# ── Protected mode: MD5 recipes + wrong-password path ───────────────────────

@pytest.mark.parametrize("lan_protocol", ["1", "2"])
def test_connect_protected_accepts_correct_password(lan_protocol):
    async def go():
        driver, sim, server = await _make_pair(
            sim_config={"lan_protocol": lan_protocol, "password": "secret"},
            driver_overrides={"password": "secret"},
            power="on",
        )
        await driver.connect()
        try:
            assert driver.get_state("auth_required") is True
            assert driver.get_state("power") == "on"
            assert driver.get_state("volume") == 20
        finally:
            await driver.disconnect()
            await server.stop()

    asyncio.run(go())


@pytest.mark.parametrize("lan_protocol", ["1", "2"])
def test_connect_wrong_password_raises_auth_worded(lan_protocol):
    async def go():
        driver, sim, server = await _make_pair(
            sim_config={"lan_protocol": lan_protocol, "password": "secret"},
            driver_overrides={"password": "wrong"},
        )
        try:
            with pytest.raises(ConnectionError) as exc:
                await driver.connect()
            assert "authentication failed" in str(exc.value).lower()
            # _post_connect failed before the declare stage: never
            # connected, no connect event.
            assert driver.connected is False
            assert driver.get_state("connected") is not True
            assert "device.connected.disp1" not in driver.events.emitted
        finally:
            await server.stop()

    asyncio.run(go())


# ── Close-after-every-response family (VF1H / SF2 / EQ1 / SQ1 / VF2 / BQ1) ──

def test_close_after_every_response_still_populates():
    async def go():
        driver, sim, server = await _make_pair(
            sim_config={"password": "secret"},
            driver_overrides={"password": "secret"},
            power="on",
            close_after_response=True,
        )
        sim.state.update({"input": "UC1", "volume": 73})
        await driver.connect()
        try:
            assert driver.get_state("power") == "on"
            assert driver.get_state("input") == "usb_c"
            assert driver.get_state("volume") == 73
            assert driver.get_state("sharpness") == 50
            # Each command really did ride its own authed connection.
            assert server._next_client > 10
        finally:
            await driver.disconnect()
            await server.stop()

    asyncio.run(go())


# ── Standby gating ──────────────────────────────────────────────────────────

def test_standby_polls_only_standby_safe_queries():
    async def go():
        driver, sim, server = await _make_pair(power="off")
        sim.state.update({"volume": 12, "audio_mute": True})
        await driver.connect()
        try:
            assert driver.get_state("power") == "off"
            assert driver.get_state("volume") == 12
            assert driver.get_state("audio_mute") is True
            # Nothing else was asked while in standby.
            joined = "\n".join(server.received)
            assert "QMI" not in joined
            assert "QVM" not in joined
            assert "QAS" not in joined
            assert "QPC" not in joined
            assert driver.get_state("input") is None
            assert driver.get_state("backlight") is None
        finally:
            await driver.disconnect()
            await server.stop()

    asyncio.run(go())


# ── Device settings: write + read-back ──────────────────────────────────────

_DS_CASES = [
    ("input", "hdmi3", "input", "hdmi3", "HM3"),
    ("picture_mode", "graphic", "picture_mode", "graphic", "GRH"),
    ("backlight", 81, "backlight", 81, 81),
    ("contrast", 61, "contrast", 61, 61),
    ("black_level", 41, "black_level", 41, 41),
    ("color", 31, "color", 31, 31),
    ("tint", 21, "tint", 21, 21),
    ("sharpness", 91, "sharpness", 91, 91),
]


@pytest.mark.parametrize("key,value,state_key,expected,sim_value", _DS_CASES)
def test_device_setting_round_trip(key, value, state_key, expected, sim_value):
    async def go():
        driver, sim, server = await _make_pair(power="on")
        await driver.connect()
        try:
            await driver.set_device_setting(key, value)
            assert sim.state[state_key] == sim_value
            assert driver.get_state(state_key) == expected
            # Independent read-back through a fresh poll.
            driver.state.data.pop(state_key, None)
            await driver.poll()
            assert driver.get_state(state_key) == expected
        finally:
            await driver.disconnect()
            await server.stop()

    asyncio.run(go())


def test_unknown_device_setting_raises():
    async def go():
        driver, sim, server = await _make_pair()
        await driver.connect()
        try:
            with pytest.raises(ValueError):
                await driver.set_device_setting("nonsense", 1)
        finally:
            await driver.disconnect()
            await server.stop()

    asyncio.run(go())


# ── Commands ────────────────────────────────────────────────────────────────

def test_input_pass_through_and_rejection():
    async def go():
        driver, sim, server = await _make_pair(power="on")
        await driver.connect()
        try:
            # Friendly name maps to its code.
            await driver.send_command("set_input", {"input": "hdmi2"})
            assert sim.state["input"] == "HM2"
            # A free-typed protocol code passes through to the wire —
            # the sim (an SQ2H-class unit) rejects it with ERR2, which
            # is benign; state keeps the read-back value.
            await driver.send_command("set_input", {"input": "TV1"})
            assert any("IMS:TV1" in r for r in server.received)
            assert sim.state["input"] == "HM2"
            assert driver.get_state("input") == "hdmi2"
            # Junk that isn't a plausible code is never sent.
            before = len(server.received)
            await driver.send_command("set_input", {"input": "not a code!"})
            assert len(server.received) == before
        finally:
            await driver.disconnect()
            await server.stop()

    asyncio.run(go())


def test_volume_and_power_commands():
    async def go():
        driver, sim, server = await _make_pair(power="on")
        await driver.connect()
        try:
            await driver.send_command("set_volume", {"value": 42})
            assert sim.state["volume"] == 42
            assert driver.get_state("volume") == 42
            await driver.send_command("volume_up", {})
            assert driver.get_state("volume") == 43
            await driver.send_command("volume_down", {})
            assert driver.get_state("volume") == 42
            await driver.send_command("audio_mute_on", {})
            assert sim.state["audio_mute"] is True
            assert driver.get_state("audio_mute") is True
            await driver.send_command("power_off", {})
            assert sim.state["power"] == "off"
            assert driver.get_state("power") == "off"
            await driver.send_command("power_on", {})
            assert driver.get_state("power") == "on"
        finally:
            await driver.disconnect()
            await server.stop()

    asyncio.run(go())


def test_raw_command_returns_content():
    async def go():
        driver, sim, server = await _make_pair(power="on")
        await driver.connect()
        try:
            result = await driver.send_command(
                "raw_command", {"command": "QPW"}
            )
            assert result == "1"
        finally:
            await driver.disconnect()
            await server.stop()

    asyncio.run(go())


# ── Never-offline guard: poll propagates when the display goes away ─────────

def test_poll_propagates_when_unreachable():
    async def go():
        driver, sim, server = await _make_pair(power="on")
        await driver.connect()
        await server.stop()
        try:
            with pytest.raises(ConnectionError):
                await driver.poll()
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Setup wizard ────────────────────────────────────────────────────────────

async def _run_wizard(driver, typed_user, typed_pass, save=False):
    async def progress(step, pct=None):
        pass

    return await driver.run_setup_action(
        "test_credentials",
        {"username": typed_user, "password": typed_pass, "save": save},
        progress,
    )


@pytest.mark.parametrize("lan_protocol", ["1", "2"])
def test_setup_wizard_accepts_correct_credentials(lan_protocol):
    async def go():
        driver, sim, server = await _make_pair(
            sim_config={"lan_protocol": lan_protocol, "password": "secret"},
        )
        try:
            result = await _run_wizard(driver, "admin1", "secret", save=True)
            assert result["auth_enabled"] is True
            assert result["auth_ok"] is True
            assert result["saved"] is True
            assert result["protocol"] == f"protocol{lan_protocol}"
            assert driver.config["password"] == "secret"
            assert driver.reconnects == 1
        finally:
            await server.stop()

    asyncio.run(go())


def test_setup_wizard_wrong_password_raises():
    async def go():
        driver, sim, server = await _make_pair(
            sim_config={"password": "secret"},
        )
        try:
            with pytest.raises(ConnectionError) as exc:
                await _run_wizard(driver, "admin1", "wrong")
            assert "reject" in str(exc.value).lower()
        finally:
            await server.stop()

    asyncio.run(go())


def test_setup_wizard_non_protected():
    async def go():
        driver, sim, server = await _make_pair()
        try:
            result = await _run_wizard(driver, "admin1", "", save=False)
            assert result["auth_enabled"] is False
            assert result["auth_ok"] is True
            assert driver.reconnects == 0
        finally:
            await server.stop()

    asyncio.run(go())


def test_setup_wizard_unreachable_raises():
    async def go():
        driver, sim, server = await _make_pair()
        await server.stop()
        with pytest.raises(ConnectionError):
            await _run_wizard(driver, "admin1", "")

    asyncio.run(go())
