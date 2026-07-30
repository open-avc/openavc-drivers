"""Tests for the Wake-on-LAN driver.

Self-contained: the driver is loaded against lightweight ``server.*``
stubs installed in ``sys.modules`` (this repo's community CI has no
``openavc`` install; ``conftest.py`` brackets the stub install so it
can't leak into other modules).

Two layers:
  * the connection lifecycle — the driver is connectionless, so connect
    must declare it ready with no transport at all and the ``connected``
    property must read True while active;
  * the magic-packet builder — 6 x 0xFF followed by the MAC repeated 16
    times, with the common MAC separator formats accepted.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "utility" / "wake_on_lan.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver:
    """Functional stand-in for the platform BaseDriver: connect() and
    disconnect() run the hook-driven lifecycle in the platform's order —
    clean slate, _pre_connect, _create_transport, _post_connect, declare,
    _initial_sync, with _close_session on every teardown. ``connected``
    is the platform's _link_alive-backed property (the driver overrides
    _link_alive to True for its connectionless protocol)."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False

    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def get_state(self, key, default=None):
        return self.state.get(f"device.{self.device_id}.{key}", default)

    async def _pre_connect(self) -> None:
        pass

    async def _post_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    async def _create_transport(self, transport_type) -> None:
        raise NotImplementedError

    def _link_alive(self) -> bool:
        if self.transport is None:
            return False
        return bool(getattr(self.transport, "connected", False))

    @property
    def connected(self) -> bool:
        return self._connected and self._link_alive()

    async def connect(self) -> None:
        await self._close_session()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._pre_connect()
        await self._create_transport(self.DRIVER_INFO.get("transport", "tcp"))
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
        await self._initial_sync()

    async def disconnect(self) -> None:
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


class _FakeUDPTransport:
    """Stand-in for server.transport.udp.UDPTransport (the driver only
    builds throwaway instances inside send_command).

    The method signatures deliberately mirror the real transport exactly —
    ``send()`` takes only data, ad-hoc sends go through ``send_to()``, and
    ``close()`` is async — so a driver calling the API wrong fails here the
    same way it would against the platform.
    """

    instances: list["_FakeUDPTransport"] = []

    def __init__(self, host=None, port=None, name="", **kw) -> None:
        self.name = name
        self.sent_to: list[tuple[bytes, str, int]] = []
        self.closed = False
        _FakeUDPTransport.instances.append(self)

    async def open(self, allow_broadcast=True, local_addr=None) -> None:
        pass

    async def send(self, data) -> None:
        raise AssertionError(
            "send() has no destination - ad-hoc sends must use send_to()"
        )

    async def send_to(self, data, host, port) -> None:
        self.sent_to.append((bytes(data), host, port))

    async def close(self) -> None:
        self.closed = True


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

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_driver_mod = _load("_wake_on_lan_driver", DRIVER_PATH)
WakeOnLANDriver = _driver_mod.WakeOnLANDriver
build_magic_packet = _driver_mod.build_magic_packet


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Lifecycle (connectionless — no transport, ever) ─────────────────────────

def test_connect_declares_ready_with_no_transport():
    async def scenario():
        driver = WakeOnLANDriver(
            "wolX", {"mac_address": "AA:BB:CC:DD:EE:FF"},
            _FakeState(), _FakeEvents(),
        )
        await driver.connect()
        assert driver.transport is None
        assert driver.connected is True
        assert driver.state.get("device.wolX.connected") is True
        assert "device.connected.wolX" in driver.events.emitted

        await driver.disconnect()
        assert driver.connected is False
        assert driver.state.get("device.wolX.connected") is False
        assert "device.disconnected.wolX" in driver.events.emitted
    _run(scenario())


def test_wake_sends_magic_packet_and_closes_socket():
    async def scenario():
        _FakeUDPTransport.instances.clear()
        driver = WakeOnLANDriver(
            "wolX",
            {
                "mac_address": "AA:BB:CC:DD:EE:FF",
                "broadcast_address": "192.168.1.255",
                "port": 7,
            },
            _FakeState(), _FakeEvents(),
        )
        await driver.connect()
        result = await driver.send_command("wake")
        assert result is True

        assert len(_FakeUDPTransport.instances) == 1
        udp = _FakeUDPTransport.instances[0]
        assert udp.sent_to == [
            (build_magic_packet("AA:BB:CC:DD:EE:FF"), "192.168.1.255", 7)
        ]
        assert udp.closed is True
        assert driver.state.get("device.wolX.last_wake")
    _run(scenario())


# ── Magic packet builder ────────────────────────────────────────────────────

def test_magic_packet_layout():
    pkt = build_magic_packet("AA:BB:CC:DD:EE:FF")
    mac = bytes.fromhex("AABBCCDDEEFF")
    assert len(pkt) == 102
    assert pkt[:6] == b"\xFF" * 6
    assert pkt[6:] == mac * 16


@pytest.mark.parametrize("mac", [
    "AA:BB:CC:DD:EE:FF",
    "AA-BB-CC-DD-EE-FF",
    "aabbccddeeff",
])
def test_magic_packet_accepts_common_mac_formats(mac):
    assert build_magic_packet(mac) == build_magic_packet("AABBCCDDEEFF")


@pytest.mark.parametrize("mac", ["", "AA:BB:CC:DD:EE", "not-a-mac-addr", "GG:BB:CC:DD:EE:FF"])
def test_magic_packet_rejects_invalid_mac(mac):
    with pytest.raises(ValueError):
        build_magic_packet(mac)
