"""Tests for the sACN / E1.31 DMX driver.

Self-contained: the driver and receiver simulator are loaded against
lightweight ``server.*`` / ``simulator.*`` stubs installed in
``sys.modules`` (this repo's community CI has no ``openavc`` install;
``conftest.py`` brackets the stub install so it can't leak into other
modules). The dual-proof round trip pipes the driver's outgoing bytes
straight into the real receiver simulator's decoder through an
in-memory fake transport — no socket needed for a transmitter.

Two layers:
  * pure packet-layout / addressing checks against ANSI E1.31-2018
    Table 4-1 and Section 9.3.1 (byte-correct packets are the driver's
    whole reason to exist);
  * a dual-proof round trip — the driver drives the real simulator's
    ``handle_command`` decoder, asserting the receiver sees exactly
    what each command sent.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
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


# ── Platform stubs ───────────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver: the driver supplies
    lifecycle hooks (_pre_connect / _create_transport / _initial_sync /
    _close_session) and connect()/disconnect() here run them in the
    platform's order — clean slate, _pre_connect, wholesale transport
    build, _post_connect (with failure teardown), declare, _initial_sync
    (with full teardown on failure), then polling. _close_session runs on
    every teardown path, mirroring base.py."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._last_transport_error = ""
        self._last_fault = None
        self._bg_tasks: set = set()

    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def get_state(self, key):
        return self.state.get(f"device.{self.device_id}.{key}")

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
        # overrides this wholesale (per-send destinations, broadcast open).
        raise NotImplementedError

    def _link_alive(self) -> bool:
        if self.transport is None:
            return False
        return bool(getattr(self.transport, "connected", False))

    @property
    def connected(self) -> bool:
        return self._connected and self._link_alive()

    def _handle_transport_disconnect(self) -> None:
        # Mirrors the platform: flip the flags synchronously, then schedule
        # the async teardown (close transport, _close_session, event).
        self._connected = False
        self.set_state("connected", False)
        task = asyncio.ensure_future(self._on_disconnect_cleanup())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _on_disconnect_cleanup(self) -> None:
        await self.stop_polling()
        transport = self.transport
        self.transport = None
        if transport is not None:
            await transport.close()
        await self._close_session()
        await self.events.emit(f"device.disconnected.{self.device_id}")

    # -- connection lifecycle (mirrors BaseDriver.connect/disconnect) --

    async def connect(self) -> None:
        # 1. Clean slate: reset fault classification, drop a previous
        #    attempt's driver session and stale transport.
        self._last_transport_error = ""
        self._last_fault = None
        await self._close_session()
        if self.transport:
            await self.transport.close()
            self.transport = None
        # 2-3. Establish: pre-connect hook, then the transport.
        await self._pre_connect()
        await self._create_transport(self.DRIVER_INFO.get("transport", "tcp"))
        # Transport verify stage skipped: the fake UDP transport, like the
        # real one, has no verify().
        # 4. Handshake: a raise here aborts the connection.
        try:
            await self._post_connect()
            self._connected = True
            self.set_state("connected", True)
            await self.events.emit(f"device.connected.{self.device_id}")
        except Exception:
            self._stash_transport_error()
            if self.transport:
                await self.transport.close()
                self.transport = None
            await self._close_session()
            self._connected = False
            raise
        # 5. Initial sync: a raise here tears the connection back down.
        try:
            await self._initial_sync()
        except Exception:
            self._stash_transport_error()
            transport = self.transport
            self.transport = None
            if transport is not None:
                await transport.close()
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise
        # 6. Polling (this driver never configures an interval).
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


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None


class _FakeUDPTransport:
    """Stand-in for server.transport.udp.UDPTransport — pipes every
    datagram straight into the paired simulator's decoder."""

    def __init__(self, host=None, port=None, on_data=None,
                 on_disconnect=None, name="", **kw) -> None:
        self.on_disconnect = on_disconnect
        self.name = name
        self._sim = _CURRENT_SIM
        self.sent: list = []
        self.closed = False
        self.connected = False

    async def open(self, allow_broadcast=True, local_addr=None) -> None:
        self.connected = True

    async def send_to(self, data, host, port) -> None:
        self.sent.append((bytes(data), host, port))
        if self._sim is not None:
            self._sim.handle_command(bytes(data))

    async def close(self) -> None:
        self.closed = True
        self.connected = False


class _FakeSimState:
    def __init__(self, initial) -> None:
        self.data = dict(initial)

    def get(self, key, default=None):
        return self.data.get(key, default)


class _FakeUDPSimulator:
    """Stand-in for simulator.udp_simulator.UDPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.driver_id = self.SIMULATOR_INFO.get("driver_id", "")
        self.name = self.SIMULATOR_INFO.get("name", device_id)
        self.state = _FakeSimState(self.SIMULATOR_INFO.get("initial_state", {}))

    def set_state(self, key, value) -> None:
        self.state.data[key] = value


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"server.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"server.{sub}"] = m
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["server.drivers.base"] = base
    udp = ModuleType("server.transport.udp")
    udp.UDPTransport = _FakeUDPTransport
    sys.modules["server.transport.udp"] = udp
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["simulator"] = sim_pkg
    sim_udp = ModuleType("simulator.udp_simulator")
    sim_udp.UDPSimulator = _FakeUDPSimulator
    sys.modules["simulator.udp_simulator"] = sim_udp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_driver_mod = _load("_sacn_e131_driver", REPO_ROOT / "lighting" / "sacn_e131.py")
_sim_mod = _load("_sacn_e131_sim", REPO_ROOT / "lighting" / "sacn_e131_sim.py")

SacnE131Driver = _driver_mod.SacnE131Driver
SacnE131Simulator = _sim_mod.SacnE131Simulator
build_packet = _driver_mod.build_e131_data_packet
universe_to_multicast = _driver_mod.universe_to_multicast
_derive_cid = _driver_mod._derive_cid


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_pair(overrides=None):
    global _CURRENT_SIM
    sim = SacnE131Simulator("simX")
    _CURRENT_SIM = sim
    cfg = {
        "mode": "unicast", "host": "127.0.0.1", "port": 5568,
        "universe": 1, "priority": 100, "source_name": "OpenAVC",
        "keepalive": False,
    }
    cfg.update(overrides or {})
    driver = SacnE131Driver("sacnX", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Packet layout (ANSI E1.31-2018, Table 4-1) ───────────────────────────────

def test_full_packet_byte_layout():
    cid = _derive_cid("dev-a")
    dmx = bytes(range(256)) * 2  # 512 bytes
    pkt = build_packet(
        cid=cid, source_name="OpenAVC", priority=100, sequence=7,
        universe=1, dmx_data=dmx,
    )
    assert len(pkt) == 638
    # Root layer
    assert pkt[0:2] == b"\x00\x10"                # preamble size
    assert pkt[2:4] == b"\x00\x00"                # post-amble size
    assert pkt[4:16] == b"ASC-E1.17\x00\x00\x00"  # ACN packet identifier
    assert pkt[16:18] == b"\x72\x6e"              # root flags/length
    assert pkt[18:22] == b"\x00\x00\x00\x04"      # VECTOR_ROOT_E131_DATA
    assert pkt[22:38] == cid
    # Framing layer
    assert pkt[38:40] == b"\x72\x58"              # framing flags/length
    assert pkt[40:44] == b"\x00\x00\x00\x02"      # VECTOR_E131_DATA_PACKET
    assert pkt[44:51] == b"OpenAVC" and pkt[51] == 0x00  # NUL-terminated name
    assert pkt[108] == 100                        # priority
    assert pkt[109:111] == b"\x00\x00"            # sync address
    assert pkt[111] == 7                          # sequence
    assert pkt[112] == 0x00                       # options
    assert pkt[113:115] == b"\x00\x01"            # universe (big-endian)
    # DMP layer
    assert pkt[115:117] == b"\x72\x0b"            # dmp flags/length
    assert pkt[117] == 0x02                       # VECTOR_DMP_SET_PROPERTY
    assert pkt[118] == 0xA1                       # address type & data type
    assert pkt[119:121] == b"\x00\x00"            # first property address
    assert pkt[121:123] == b"\x00\x01"            # address increment
    assert pkt[123:125] == b"\x02\x01"            # property value count = 513
    assert pkt[125] == 0x00                       # START code
    assert pkt[126:638] == dmx                    # 512 DMX slots verbatim


def test_flags_length_derived_for_short_payload():
    # A 4-slot payload: DMP len = 11+4=15, framing = 92, root = 114.
    pkt = build_packet(
        cid=_derive_cid("x"), source_name="x", priority=100, sequence=0,
        universe=1, dmx_data=bytes([1, 2, 3, 4]),
    )
    assert len(pkt) == 130
    assert pkt[16:18] == (0x7000 | 114).to_bytes(2, "big")
    assert pkt[38:40] == (0x7000 | 92).to_bytes(2, "big")
    assert pkt[115:117] == (0x7000 | 15).to_bytes(2, "big")
    assert pkt[123:125] == (5).to_bytes(2, "big")  # 1 START + 4 slots


def test_options_and_universe_encoding():
    pkt = build_packet(
        cid=_derive_cid("x"), source_name="x", priority=1, sequence=0,
        universe=7962, dmx_data=bytes(512),
        options=_driver_mod.OPT_STREAM_TERMINATED,
    )
    assert pkt[112] == 0x40                       # Stream_Terminated
    assert pkt[113:115] == (7962).to_bytes(2, "big")
    assert pkt[108] == 1


def test_source_name_truncated_and_terminated():
    pkt = build_packet(
        cid=_derive_cid("x"), source_name="N" * 100, priority=100,
        sequence=0, universe=1, dmx_data=bytes(512),
    )
    name_field = pkt[44:108]
    assert len(name_field) == 64
    assert name_field[63] == 0x00                 # always NUL-terminated
    assert name_field[:63] == b"N" * 63


# ── Multicast addressing (ANSI E1.31-2018, Section 9.3.1, Table 9-1) ──────────

@pytest.mark.parametrize("universe,addr", [
    (1, "239.255.0.1"),
    (256, "239.255.1.0"),
    (7962, "239.255.31.26"),   # spec Appendix B.2 worked example
    (63999, "239.255.249.255"),
])
def test_universe_to_multicast(universe, addr):
    assert universe_to_multicast(universe) == addr


# ── CID derivation ───────────────────────────────────────────────────────────

def test_cid_stable_and_per_device():
    a1 = _derive_cid("dev-a")
    a2 = _derive_cid("dev-a")
    b = _derive_cid("dev-b")
    assert len(a1) == 16
    assert a1 == a2          # stable across restarts (same device id)
    assert a1 != b           # distinct per device


# ── Driver surface ───────────────────────────────────────────────────────────

def test_actions_reference_real_commands():
    info = SacnE131Driver.DRIVER_INFO
    commands = info["commands"]
    actions = info["actions"]
    assert [a["id"] for a in actions] == ["blackout", "full_on"]
    for entry in actions:
        assert entry["id"] in commands, entry["id"]
        assert entry["kind"] == "command"
    full_on = next(a for a in actions if a["id"] == "full_on")
    assert full_on.get("confirm")  # destructive → must confirm


# ── Dual-proof: driver → real receiver-simulator decoder ─────────────────────

def test_connect_seeds_state():
    async def scenario():
        driver, _sim = _make_pair()
        await driver.connect()
        st = driver.state
        for key in ("target_universe", "priority", "destination",
                    "packets_sent", "blackout", "connected"):
            assert st.get(f"device.sacnX.{key}") is not None, key
        assert st.get("device.sacnX.blackout") is True
        assert st.get("device.sacnX.connected") is True
        assert st.get("device.sacnX.destination") == "127.0.0.1:5568"
    _run(scenario())


def test_commands_reach_receiver():
    async def scenario():
        driver, sim = _make_pair()
        await driver.connect()

        await driver.send_command("set_channel", {"channel": 1, "value": 200})
        assert sim.universes[1][0] == 200
        assert sim.state.get("last_priority") == 100
        assert sim.state.get("last_source_name") == "OpenAVC"
        assert sim.state.get("last_channel_count") == 512
        assert driver.state.get("device.sacnX.blackout") is False

        await driver.send_command(
            "set_channels", {"start_channel": 10, "values": "11,22,33"})
        assert sim.universes[1][9:12] == bytes([11, 22, 33])
        assert sim.universes[1][0] == 200  # buffer persists across commands

        await driver.send_command("full_on")
        assert sim.universes[1] == b"\xff" * 512

        await driver.send_command("blackout")
        assert sim.universes[1] == b"\x00" * 512
    _run(scenario())


def test_priority_and_universe_change():
    async def scenario():
        driver, sim = _make_pair()
        await driver.connect()

        await driver.send_command("set_priority", {"priority": 40})
        assert sim.state.get("last_priority") == 40
        assert driver.state.get("device.sacnX.priority") == 40

        await driver.send_command("set_universe", {"universe": 7962})
        assert sim.state.get("last_universe") == 7962
        assert 7962 in sim.universes
        assert driver.state.get("device.sacnX.target_universe") == 7962
    _run(scenario())


def test_multicast_destination_follows_universe():
    async def scenario():
        driver, _sim = _make_pair({"mode": "multicast", "host": ""})
        await driver.connect()
        assert driver.state.get("device.sacnX.destination") == "239.255.0.1:5568"
        await driver.send_command("set_universe", {"universe": 7962})
        assert driver.state.get("device.sacnX.destination") == "239.255.31.26:5568"
        # The fake transport records where each datagram was addressed.
        last_dest = driver.transport.sent[-1][1]
        assert last_dest == "239.255.31.26"
    _run(scenario())


def test_sequence_increments():
    async def scenario():
        driver, sim = _make_pair()
        await driver.connect()
        await driver.send_command("send_now")
        seq_a = sim.state.get("last_sequence")
        await driver.send_command("send_now")
        seq_b = sim.state.get("last_sequence")
        assert ((seq_b - seq_a) & 0xFF) == 1
    _run(scenario())


def test_out_of_range_inputs_are_ignored():
    async def scenario():
        driver, sim = _make_pair()
        await driver.connect()
        before = sim.state.get("frames_received")
        # Channel 0 / 513, value handled by runtime param gate normally; the
        # driver itself also guards and sends nothing for an out-of-range channel.
        await driver.send_command("set_channel", {"channel": 0, "value": 10})
        await driver.send_command("set_channel", {"channel": 999, "value": 10})
        await driver.send_command("set_universe", {"universe": 70000})
        await driver.send_command("set_priority", {"priority": 300})
        assert sim.state.get("frames_received") == before  # nothing sent
    _run(scenario())


def test_graceful_stream_termination_on_disconnect():
    async def scenario():
        driver, sim = _make_pair()
        await driver.connect()
        await driver.send_command("set_channel", {"channel": 1, "value": 5})
        assert sim.state.get("stream_terminated") is False
        frames_before = sim.state.get("frames_received")
        await driver.disconnect()
        # E1.31 6.2.6 / 12.2: three Stream_Terminated packets on shutdown.
        assert sim.state.get("frames_received") == frames_before + 3
        assert sim.state.get("stream_terminated") is True
        assert sim.state.get("last_options") == 0x40
        assert driver.transport is None
    _run(scenario())


def test_keepalive_task_lifecycle():
    async def scenario():
        driver_on, _s1 = _make_pair({"keepalive": True})
        await driver_on.connect()
        assert driver_on._keepalive_task is not None
        await driver_on.disconnect()
        assert driver_on._keepalive_task is None

        driver_off, _s2 = _make_pair({"keepalive": False})
        await driver_off.connect()
        assert driver_off._keepalive_task is None
        await driver_off.disconnect()
    _run(scenario())


def test_unicast_requires_host():
    async def scenario():
        driver, _sim = _make_pair({"mode": "unicast", "host": ""})
        with pytest.raises(ConnectionError):
            await driver.connect()
    _run(scenario())
