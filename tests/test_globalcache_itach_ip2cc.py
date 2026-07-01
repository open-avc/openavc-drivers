"""Unit tests for the globalcache_itach_ip2cc relay driver + simulator.

Self-contained / stdlib only: stubs the ``server.*`` and ``simulator.*`` modules
the driver and simulator import, so the test runs in the community repo's
isolated CI without an ``openavc`` install. Exercises the pure protocol helpers
and command/parse logic against byte-exact captures taken from a real iTach
IP2CC (fixtures/globalcache_itach_ip2cc/), and cross-checks that the bundled
simulator answers the same bytes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "utility" / "globalcache_itach_ip2cc.py"
SIM_PATH = REPO_ROOT / "utility" / "globalcache_itach_ip2cc_sim.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "globalcache_itach_ip2cc"


def _install_stubs() -> None:
    """Provide the minimal ``server.*`` / ``simulator.*`` surface the driver and
    simulator import at module load."""
    if "server.drivers.base" not in sys.modules:
        server = ModuleType("server")
        sys.modules.setdefault("server", server)
        drivers = ModuleType("server.drivers")
        sys.modules.setdefault("server.drivers", drivers)
        base = ModuleType("server.drivers.base")

        class _BaseDriver:
            DRIVER_INFO: dict = {}

            def __init__(self, device_id, config, state, events):
                self.device_id = device_id
                self.config = config
                self.state = state
                self.events = events
                self.transport = None
                self._published: dict = {}

            def set_state(self, key, value):
                self._published[key] = value

        base.BaseDriver = _BaseDriver
        sys.modules["server.drivers.base"] = base

        utils = ModuleType("server.utils")
        sys.modules.setdefault("server.utils", utils)
        logger_mod = ModuleType("server.utils.logger")
        logger_mod.get_logger = lambda name="gc": logging.getLogger(name)
        sys.modules["server.utils.logger"] = logger_mod

    if "simulator.tcp_simulator" not in sys.modules:
        sim_pkg = ModuleType("simulator")
        sys.modules.setdefault("simulator", sim_pkg)
        sim_tcp = ModuleType("simulator.tcp_simulator")

        class _TCPSimulator:
            SIMULATOR_INFO: dict = {}

            def __init__(self, device_id, config=None):
                self.device_id = device_id
                self.config = config or {}
                self.state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

            def set_state(self, key, value):
                self.state[key] = value

        sim_tcp.TCPSimulator = _TCPSimulator
        sys.modules["simulator.tcp_simulator"] = sim_tcp


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()
drv = _load(DRIVER_PATH, "globalcache_itach_ip2cc_under_test")
sim_mod = _load(SIM_PATH, "globalcache_itach_ip2cc_sim_under_test")


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class _FakeTransport:
    """Records the bytes the driver sends."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


def _make_driver() -> "drv.GlobalCacheItachIP2CCDriver":
    d = drv.GlobalCacheItachIP2CCDriver(
        "itach_cc", {"host": "192.168.4.97", "port": 4998}, None, None
    )
    d.transport = _FakeTransport()
    return d


# ---------------------------------------------------------------------------
# Pure protocol helpers vs. byte-exact hardware captures
# ---------------------------------------------------------------------------


def test_parse_version_from_capture():
    assert drv.parse_version(_fixture("getversion.response.txt")) == "710-1008-05"


def test_parse_version_rejects_non_version():
    assert drv.parse_version("ERR_0:0,001") == ""
    assert drv.parse_version("state,1:1,0") == ""


def test_parse_getdevices_from_capture():
    modules = drv.parse_getdevices(_fixture("getdevices.response.txt"))
    assert modules == [
        {"module": 0, "ports": 0, "type": "ETHERNET"},
        {"module": 1, "ports": 3, "type": "RELAY"},
    ]


def test_parse_getstate_line_from_capture():
    assert drv.parse_state_line(_fixture("getstate_1_1.response.txt")) == {
        "module": 1, "connector": 1, "relay": 1, "closed": False,
    }


def test_parse_setstate_echo_is_treated_as_state():
    """The IP2CC echoes ``setstate,...`` instead of the spec's ``state,...``;
    the parser accepts both so a set is confirmed from the echo."""
    assert drv.parse_state_line(_fixture("setstate_1_1_close.response.txt")) == {
        "module": 1, "connector": 1, "relay": 1, "closed": True,
    }
    assert drv.parse_state_line(_fixture("setstate_1_1_open.response.txt"))["closed"] is False


def test_parse_state_line_rejects_non_state():
    assert drv.parse_state_line(_fixture("unknowncommand.response.txt")) == {}
    assert drv.parse_state_line("710-1008-05") == {}


def test_parse_amx_beacon_from_capture():
    fields = drv.parse_amx_beacon(_fixture("amx_beacon.txt"))
    assert fields["Make"] == "GlobalCache"
    assert fields["Model"] == "iTachIP2CC"
    assert fields["Revision"] == "710-1008-05"
    # UUID embeds the MAC -> OUI 00:0C:1E
    assert fields["UUID"] == "GlobalCache_000C1E075EF1"
    assert fields["Config-URL"] == "http://192.168.4.97"


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


def test_format_setstate_is_cr_terminated():
    assert drv.format_setstate(2, True) == b"setstate,1:2,1\r"
    assert drv.format_setstate(3, False) == b"setstate,1:3,0\r"


def test_format_getstate_is_cr_terminated():
    assert drv.format_getstate(1) == b"getstate,1:1\r"


# ---------------------------------------------------------------------------
# Driver command behaviour (bytes on the wire + published state)
# ---------------------------------------------------------------------------


def test_close_and_open_relay():
    d = _make_driver()
    asyncio.run(d.send_command("close", {"relay": "2"}))
    assert d.transport.sent == [b"setstate,1:2,1\r"]
    assert d._published["relay_2"] is True

    asyncio.run(d.send_command("open", {"relay": "2"}))
    assert d.transport.sent[-1] == b"setstate,1:2,0\r"
    assert d._published["relay_2"] is False


def test_pulse_closes_then_opens():
    d = _make_driver()
    asyncio.run(d.send_command("pulse", {"relay": "1", "duration_ms": 50}))
    assert d.transport.sent == [b"setstate,1:1,1\r", b"setstate,1:1,0\r"]
    # Ends open.
    assert d._published["relay_1"] is False


def test_toggle_flips_last_known_state():
    d = _make_driver()
    # Unknown -> defaults to open -> toggling closes it.
    asyncio.run(d.send_command("toggle", {"relay": "3"}))
    assert d.transport.sent == [b"setstate,1:3,1\r"]
    assert d._relay_state[3] is True
    # Now closed -> toggling opens it.
    asyncio.run(d.send_command("toggle", {"relay": "3"}))
    assert d.transport.sent[-1] == b"setstate,1:3,0\r"
    assert d._relay_state[3] is False


def test_pulse_duration_is_clamped():
    d = _make_driver()
    assert d._coerce_duration({"duration_ms": 999999}) == 10000
    assert d._coerce_duration({"duration_ms": 1}) == 50
    assert d._coerce_duration({}) == 500


def test_invalid_relay_rejected():
    d = _make_driver()
    with pytest.raises(ValueError):
        asyncio.run(d.send_command("close", {"relay": "4"}))
    with pytest.raises(ValueError):
        asyncio.run(d.send_command("close", {}))


def test_unknown_command_rejected():
    d = _make_driver()
    with pytest.raises(ValueError):
        asyncio.run(d.send_command("explode", {}))


# ---------------------------------------------------------------------------
# on_data_received framing (multiple lines per chunk, split lines)
# ---------------------------------------------------------------------------


def test_on_data_received_parses_multiple_lines_in_one_chunk():
    d = _make_driver()
    asyncio.run(d.on_data_received(b"710-1008-05\rstate,1:1,1\rstate,1:2,0\r"))
    assert d._published["firmware_version"] == "710-1008-05"
    assert d._published["relay_1"] is True
    assert d._published["relay_2"] is False


def test_on_data_received_handles_delimiter_stripped_line():
    # The TCP transport frames on CR and delivers each line already stripped,
    # so a single call carries one bare line (no trailing CR).
    d = _make_driver()
    asyncio.run(d.on_data_received(b"710-1008-05"))
    assert d._published["firmware_version"] == "710-1008-05"
    asyncio.run(d.on_data_received(b"state,1:3,1"))
    assert d._published["relay_3"] is True


def test_on_data_received_ignores_error_line():
    d = _make_driver()
    asyncio.run(d.on_data_received(_fixture("unknowncommand.response.txt")))
    assert d._published == {}


# ---------------------------------------------------------------------------
# Catalog-facing DRIVER_INFO (commands + discovery hints)
# ---------------------------------------------------------------------------


def test_relay_commands_declared():
    cmds = drv.GlobalCacheItachIP2CCDriver.DRIVER_INFO["commands"]
    assert set(cmds) >= {"close", "open", "pulse", "toggle", "refresh"}
    assert cmds["close"]["params"]["relay"]["values"] == ["1", "2", "3"]
    assert cmds["pulse"]["params"]["duration_ms"]["default"] == 500


def test_state_variables_expose_three_relays():
    sv = drv.GlobalCacheItachIP2CCDriver.DRIVER_INFO["state_variables"]
    assert {"relay_1", "relay_2", "relay_3"} <= set(sv)


def test_discovery_hints_match_real_beacon():
    info = drv.GlobalCacheItachIP2CCDriver.DRIVER_INFO
    disc = info["discovery"]
    fields = drv.parse_amx_beacon(_fixture("amx_beacon.txt"))
    amx = disc["amx_ddp"][0]
    assert amx["make"] == fields["Make"]
    assert amx["model_pattern"] == fields["Model"]
    # 4998 getdevices probe positively identifies a relay unit.
    assert disc["tcp_probe"]["port"] == 4998
    assert disc["tcp_probe"]["expect"] == "RELAY"
    assert "00:0C:1E" in disc["oui"]


def test_min_platform_version_matches_discovery_runtime():
    # amx_ddp discovery hints ship in 0.19.0 (same floor as the IP2SL sibling).
    assert drv.GlobalCacheItachIP2CCDriver.DRIVER_INFO["min_platform_version"] == "0.19.0"


# ---------------------------------------------------------------------------
# Simulator answers the same bytes as the real unit
# ---------------------------------------------------------------------------


def _make_sim() -> "sim_mod.GlobalCacheItachIP2CCSimulator":
    return sim_mod.GlobalCacheItachIP2CCSimulator("sim_cc", {})


def test_sim_getversion_matches_capture():
    s = _make_sim()
    assert s.handle_command(b"getversion") == _fixture("getversion.response.txt")


def test_sim_getdevices_matches_capture():
    s = _make_sim()
    assert s.handle_command(b"getdevices") == _fixture("getdevices.response.txt")


def test_sim_getstate_defaults_open():
    s = _make_sim()
    assert s.handle_command(b"getstate,1:1") == _fixture("getstate_1_1.response.txt")


def test_sim_setstate_echoes_and_updates_state():
    s = _make_sim()
    assert s.handle_command(b"setstate,1:1,1") == _fixture("setstate_1_1_close.response.txt")
    assert s.state["relay_1"] is True
    # A follow-up getstate now reads closed.
    assert s.handle_command(b"getstate,1:1") == b"state,1:1,1\r"
    assert s.handle_command(b"setstate,1:1,0") == _fixture("setstate_1_1_open.response.txt")


def test_sim_unknown_command_matches_capture():
    s = _make_sim()
    assert s.handle_command(b"boguscmd") == _fixture("unknowncommand.response.txt")


def test_sim_rejects_bad_connector():
    s = _make_sim()
    assert s.handle_command(b"setstate,1:9,1") == b"ERR_0:0,001\r"
    assert s.handle_command(b"getstate,1:0") == b"ERR_0:0,001\r"
