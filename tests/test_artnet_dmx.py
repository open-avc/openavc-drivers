"""Tests for the Art-Net DMX driver's declared surface and its socket.

``artnet_dmx.py`` imports ``openavc.*`` at module load, so it is loaded
against the shared stand-ins in ``_platform_stubs`` (this repo's isolated CI
has no openavc install; ``conftest.py`` rolls the stubs back after
collection). The UDP transport is a fake that records how the driver opened it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from _platform_stubs import (
    StubBaseDriver,
    StubEvents,
    StubState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeUDPTransport:
    """Stand-in for openavc.transport.udp.UDPTransport: records the keyword
    arguments the driver opened the socket with."""

    def __init__(self, host=None, port=None, on_data=None,
                 on_disconnect=None, name="", **kw) -> None:
        self.host = host
        self.port = port
        self.open_kwargs: dict | None = None

    async def open(self, allow_broadcast=True, local_addr=None) -> None:
        self.open_kwargs = {
            "allow_broadcast": allow_broadcast, "local_addr": local_addr,
        }

    async def close(self) -> None:
        pass


class _FakeBaseDriver(StubBaseDriver):
    def _handle_transport_disconnect(self) -> None:
        pass


install_stubs(
    {"openavc.transport.udp": {"UDPTransport": _FakeUDPTransport}},
    base_driver=_FakeBaseDriver,
)
_driver_mod = load_module(
    "_artnet_dmx_driver", REPO_ROOT / "lighting" / "artnet_dmx.py"
)

ArtNetDmxDriver = next(
    obj
    for obj in vars(_driver_mod).values()
    if isinstance(obj, type)
    and getattr(obj, "DRIVER_INFO", {}).get("id") == "artnet_dmx"
)


def test_actions_reference_real_commands():
    """Every quick action promotes an existing command (a dangling id
    would silently drop the button — the resolver skips it)."""
    info = ArtNetDmxDriver.DRIVER_INFO
    commands = info["commands"]
    actions = info["actions"]
    assert [a["id"] for a in actions] == ["blackout", "full_on"]
    for entry in actions:
        assert entry["id"] in commands, entry["id"]
        assert entry["kind"] == "command"
    # full_on is the destructive one — it must carry a confirm
    full_on = next(a for a in actions if a["id"] == "full_on")
    assert full_on.get("confirm")


# ── Reach devices through: the socket the driver opens itself ──

def _open_socket(host: str, pinned: str | None) -> dict:
    driver = ArtNetDmxDriver(
        "artnet1", {"host": host, "universe": 0}, StubState(), StubEvents(),
    )
    driver.reach_devices_address = pinned
    asyncio.run(driver._create_transport("udp"))
    return driver.transport.open_kwargs


def test_the_socket_leaves_from_the_chosen_adapter():
    # A subnet broadcast target binds too, and broadcast stays allowed.
    assert _open_socket("10.1.2.255", "10.20.0.5") == {
        "allow_broadcast": True, "local_addr": "10.20.0.5",
    }


@pytest.mark.parametrize("pinned, host", [
    (None, "10.1.2.255"),        # no adapter chosen
    ("10.20.0.5", "127.0.0.1"),  # a node on this computer (a simulator)
])
def test_nothing_is_bound_with_no_adapter_chosen_or_a_node_on_this_computer(
    pinned, host,
):
    assert _open_socket(host, pinned) == {
        "allow_broadcast": True, "local_addr": None,
    }
