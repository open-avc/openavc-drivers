"""Unit tests for the globalcache_itach_ip2ir IR-bridge driver + simulator.

Self-contained / stdlib only: stubs the ``openavc.*`` modules
the driver and simulator import (including a faithful minimal ``ir_codec`` that
mirrors the platform's Pronto <-> structure math), so the test runs in the
community repo's isolated CI without an ``openavc`` install. Exercises the pure
sendir wire helpers against byte-exact captures from a real iTach IP2IR
(fixtures/globalcache_itach_ip2ir/) -- including a round-trip on all 16 learned
codes -- the bridge emit/learn seams, and simulator parity.
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
DRIVER_PATH = REPO_ROOT / "utility" / "globalcache_itach_ip2ir.py"
SIM_PATH = REPO_ROOT / "utility" / "globalcache_itach_ip2ir_sim.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "globalcache_itach_ip2ir"


def _install_stubs() -> None:
    """Minimal ``openavc.*`` surface the driver + sim import."""
    if "openavc.drivers.base" not in sys.modules:
        server = ModuleType("openavc")
        sys.modules.setdefault("openavc", server)
        drivers = ModuleType("openavc.drivers")
        sys.modules.setdefault("openavc.drivers", drivers)
        base = ModuleType("openavc.drivers.base")

        import abc

        # Mirror the real BaseDriver's abstract contract: send_command is the one
        # @abstractmethod. Making the stub enforce it means _make_driver() fails
        # loudly if the driver forgets to implement send_command (as the real
        # platform does at instantiation) instead of the stub masking it.
        class _BaseDriver(abc.ABC):
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

            async def start_polling(self, interval):
                pass

            async def stop_polling(self):
                pass

            async def disconnect(self):
                pass

            @abc.abstractmethod
            async def send_command(self, command, params=None):
                ...

        base.BaseDriver = _BaseDriver
        sys.modules["openavc.drivers.base"] = base

        utils = ModuleType("openavc.utils")
        sys.modules.setdefault("openavc.utils", utils)
        logger_mod = ModuleType("openavc.utils.logger")
        logger_mod.get_logger = lambda name="gc": logging.getLogger(name)
        sys.modules["openavc.utils.logger"] = logger_mod

    if "openavc.transport.ir_codec" not in sys.modules:
        transport = ModuleType("openavc.transport")
        sys.modules.setdefault("openavc.transport", transport)
        ir_codec = ModuleType("openavc.transport.ir_codec")

        # Faithful mirror of the platform ir_codec (tested for real in the core
        # repo). Kept here so this driver test needs no openavc install.
        from typing import NamedTuple

        _CLOCK = 0.241246

        class IRCode(NamedTuple):
            frequency: int
            bursts: tuple
            repeat_offset: int = 0

        def parse_pronto(text: str) -> IRCode:
            words = [int(t, 16) for t in text.split()]
            once = words[2]
            bursts = words[4:]
            return IRCode(
                frequency=round(1_000_000 / (words[1] * _CLOCK)),
                bursts=tuple(bursts),
                repeat_offset=2 * once,
            )

        def build_pronto(code: IRCode) -> str:
            once = code.repeat_offset // 2
            rep = (len(code.bursts) - code.repeat_offset) // 2
            word1 = round(1_000_000 / (code.frequency * _CLOCK))
            words = [0x0000, word1, once, rep, *code.bursts]
            return " ".join(f"{w:04X}" for w in words)

        ir_codec.IRCode = IRCode
        ir_codec.parse_pronto = parse_pronto
        ir_codec.build_pronto = build_pronto
        sys.modules["openavc.transport.ir_codec"] = ir_codec

    if "openavc.simulator.tcp_simulator" not in sys.modules:
        sim_pkg = ModuleType("openavc.simulator")
        sys.modules.setdefault("openavc.simulator", sim_pkg)
        sim_tcp = ModuleType("openavc.simulator.tcp_simulator")

        class _TCPSimulator:
            SIMULATOR_INFO: dict = {}

            def __init__(self, device_id, config=None):
                self.device_id = device_id
                self.config = config or {}
                self.state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

            def set_state(self, key, value):
                self.state[key] = value

        sim_tcp.TCPSimulator = _TCPSimulator
        sys.modules["openavc.simulator.tcp_simulator"] = sim_tcp


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()
drv = _load(DRIVER_PATH, "globalcache_itach_ip2ir_under_test")
sim_mod = _load(SIM_PATH, "globalcache_itach_ip2ir_sim_under_test")


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.connected = True

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


def _make_driver() -> "drv.GlobalCacheItachIP2IRDriver":
    d = drv.GlobalCacheItachIP2IRDriver(
        "itach_ir", {"host": "192.168.4.98", "port": 4998, "poll_interval": 0},
        None, None,
    )
    d.transport = _FakeTransport()
    return d


# ---------------------------------------------------------------------------
# Pure protocol helpers vs. byte-exact hardware captures
# ---------------------------------------------------------------------------


def test_parse_version_from_capture():
    assert drv.parse_version(_fixture("getversion.response.txt")) == "710-1005-05"


def test_parse_getdevices_identifies_ir_module():
    modules = drv.parse_getdevices(_fixture("getdevices.response.txt"))
    assert modules == [
        {"module": 0, "ports": 0, "type": "ETHERNET"},
        {"module": 1, "ports": 3, "type": "IR"},
    ]


def test_parse_amx_beacon_from_capture():
    fields = drv.parse_amx_beacon(_fixture("amx_beacon.txt"))
    assert fields["Make"] == "GlobalCache"
    assert fields["Model"] == "iTachIP2IR"
    assert fields["Revision"] == "710-1005-05"
    assert fields["UUID"] == "GlobalCache_000C1E075B04"  # OUI 00:0C:1E


def test_parse_ir_mode_from_capture():
    assert drv.parse_ir_mode(_fixture("get_ir_1_1.response.txt")) == {
        "connector": "1:1", "mode": "IR",
    }


def test_parse_completeir_from_capture():
    assert drv.parse_completeir(_fixture("completeir_5555.response.txt")) == ("1:1", 5555)


def test_parse_err_shapes():
    assert drv.parse_err(_fixture("err_bad_pulses.response.txt")) == ("1:1", "010")
    assert drv.parse_err(_fixture("err_bad_connector.response.txt")) == ("0:0", "002")
    assert drv.parse_completeir("ERR_1:1,010") is None


# ---------------------------------------------------------------------------
# sendir wire layer: round-trip on all 16 real learned captures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", range(1, 17))
def test_learned_sendir_round_trips_byte_exact(n):
    """parse_sendir -> build_sendir reproduces every learned capture byte-for-byte
    (the learner sometimes prefixes a stray newline, which parse tolerates)."""
    raw = _fixture(f"learned_{n}.sendir.txt")
    expected = raw.lstrip(b"\n")
    d = drv.parse_sendir(raw)
    rebuilt = drv.build_sendir(
        d["connector"], d["id"], d["freq"], d["repeat"], d["offset"], d["bursts"],
    )
    assert rebuilt == expected


@pytest.mark.parametrize("n", range(1, 17))
def test_learned_sendir_pronto_round_trip_preserves_pulses(n):
    """sendir -> Pronto -> sendir keeps every pulse value (only the carrier freq
    may quantize to the Pronto grid). Retargets to the emit port on the way back,
    exactly as bridge_emit does."""
    raw = _fixture(f"learned_{n}.sendir.txt")
    src = drv.parse_sendir(raw)
    pronto = drv.sendir_to_pronto(raw)
    back = drv.pronto_to_sendir(pronto, "1:1", src["id"], src["repeat"])
    reparsed = drv.parse_sendir(back)
    assert reparsed["bursts"] == src["bursts"]
    assert reparsed["offset"] == src["offset"]
    assert reparsed["connector"] == "1:1"  # retargeted from the learner's 2:1


def test_build_sendir_is_cr_terminated():
    out = drv.build_sendir("1:2", 7, 38000, 3, 1, [96, 24, 48, 24])
    assert out == b"sendir,1:2,7,38000,3,1,96,24,48,24\r"


# ---------------------------------------------------------------------------
# Compressed sendir expansion (spec-derived; no compressed hardware fixture)
# ---------------------------------------------------------------------------


def test_decompress_plain_data_passes_through():
    assert drv.decompress_sendir_data(["96", "24", "48", "24"]) == [96, 24, 48, 24]


def test_decompress_compressed_example_from_spec():
    # The documented example "4,5A8,9ABB": A=(4,5), B=(8,9).
    assert drv.decompress_sendir_data(["4", "5A8", "9ABB"]) == [
        4, 5, 4, 5, 8, 9, 4, 5, 8, 9, 8, 9,
    ]


def test_decompress_rejects_odd_count():
    with pytest.raises(ValueError):
        drv.decompress_sendir_data(["96", "24", "48"])


# ---------------------------------------------------------------------------
# Bridge emit seam (Pronto payload -> sendir bytes -> completeir)
# ---------------------------------------------------------------------------


def test_bridge_emit_sends_sendir_and_resolves_on_completeir():
    d = _make_driver()

    async def run():
        pronto = drv.sendir_to_pronto(_fixture("learned_1.sendir.txt"))
        task = asyncio.create_task(
            d.bridge_emit("ir:1", "ir", {"pronto": pronto, "repeat": 2})
        )
        await asyncio.sleep(0)  # let it send
        sent = d.transport.sent[-1]
        parsed = drv.parse_sendir(sent)
        # Retargeted to the bound port; repeat comes from the payload.
        assert parsed["connector"] == "1:1"
        assert parsed["repeat"] == 2
        await d.on_data_received(
            f"completeir,1:1,{parsed['id']}\r".encode()
        )
        return await task

    result = asyncio.run(run())
    assert result["status"] == "ok"
    assert result["connector"] == "1:1"


def test_bridge_emit_raises_on_err():
    d = _make_driver()

    async def run():
        task = asyncio.create_task(
            d.bridge_emit("ir:1", "ir", {"pronto": "0000 006D 0000 0001 0060 0018"})
        )
        await asyncio.sleep(0)
        await d.on_data_received(b"ERR_1:1,010\r")
        return await task

    with pytest.raises(ConnectionError):
        asyncio.run(run())


def test_bridge_emit_rejects_non_ir_kind():
    d = _make_driver()
    with pytest.raises(ValueError):
        asyncio.run(d.bridge_emit("ir:1", "serial", {"pronto": "X"}))


def test_bridge_import_code_converts_typed_sendir_to_pronto():
    d = _make_driver()
    line = _fixture("learned_2.sendir.txt").decode().strip()
    pronto = asyncio.run(d.bridge_import_code(line))
    assert pronto.startswith("0000 ")
    # And it round-trips back to the same pulses.
    assert drv.parse_sendir(pronto and drv.pronto_to_sendir(pronto, "1:1", 1, 1))[
        "bursts"
    ] == drv.parse_sendir(line)["bursts"]


def test_bridge_import_code_rejects_garbage():
    d = _make_driver()
    with pytest.raises(ValueError):
        asyncio.run(d.bridge_import_code("not a sendir string"))


def test_driver_is_instantiable_and_send_command_is_implemented():
    # Regression: the class must implement the BaseDriver abstract send_command,
    # or the platform can't instantiate it ("Can't instantiate abstract class").
    # _make_driver() would raise TypeError here if it were missing.
    d = _make_driver()
    assert hasattr(d, "send_command")


def test_refresh_command_polls_and_unknown_rejects():
    d = _make_driver()
    asyncio.run(d.send_command("refresh"))
    assert d.transport.sent[-1] == b"getversion\r"
    with pytest.raises(ValueError):
        asyncio.run(d.send_command("bogus"))


def test_is_a_bridge_with_three_ir_ports():
    ports = drv.GlobalCacheItachIP2IRDriver.DRIVER_INFO["bridge"]["ports"]
    assert [p["id"] for p in ports] == ["ir:1", "ir:2", "ir:3"]
    assert all(p["kind"] == "ir" for p in ports)


def test_port_to_connector():
    assert drv.port_to_connector("ir:1") == "1:1"
    assert drv.port_to_connector("ir:3") == "1:3"


# ---------------------------------------------------------------------------
# Bridge learn seam (dedicated socket, get_IRL -> Pronto)
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def readuntil(self, sep):
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.sleep(3600)  # block until the loop is cancelled


class _FakeWriter:
    def __init__(self):
        self.written: list[bytes] = []

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


def test_learn_start_poll_stop(monkeypatch):
    d = _make_driver()
    reader = _FakeReader([
        b"IR Learner Enabled\r",
        b"sendir,2:1,1,37537,1,1,171,170,21,64,21,64\r",
    ])
    writer = _FakeWriter()

    async def fake_open(host, port):
        return reader, writer

    monkeypatch.setattr(drv.asyncio, "open_connection", fake_open)

    async def run():
        await d.bridge_learn_start()
        assert d._learning is True
        assert writer.written[0] == b"get_IRL\r"
        # First non-informational event is the captured code as Pronto.
        pronto = None
        for _ in range(5):
            pronto = await d.bridge_learn_poll(0.5)
            if pronto:
                break
        assert pronto and pronto.startswith("0000 ")
        await d.bridge_learn_stop()
        assert d._learning is False
        assert b"stop_IRL\r" in writer.written

    asyncio.run(run())


def test_emit_blocked_during_learn(monkeypatch):
    d = _make_driver()
    reader = _FakeReader([b"IR Learner Enabled\r"])
    writer = _FakeWriter()

    async def fake_open(host, port):
        return reader, writer

    monkeypatch.setattr(drv.asyncio, "open_connection", fake_open)

    async def run():
        await d.bridge_learn_start()
        with pytest.raises(ConnectionError):
            await d.bridge_emit("ir:1", "ir", {"pronto": "0000 006D 0000 0001 0060 0018"})
        await d.bridge_learn_stop()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# DRIVER_INFO catalog facing (discovery hints, gate)
# ---------------------------------------------------------------------------


def test_discovery_hints_match_real_beacon():
    info = drv.GlobalCacheItachIP2IRDriver.DRIVER_INFO
    disc = info["discovery"]
    fields = drv.parse_amx_beacon(_fixture("amx_beacon.txt"))
    amx = disc["amx_ddp"][0]
    assert amx["make"] == fields["Make"]
    assert amx["model_pattern"] == fields["Model"]
    assert disc["tcp_probe"]["port"] == 4998
    assert disc["tcp_probe"]["expect"] == "IR"
    assert "00:0C:1E" in disc["oui"]
    # The IR probe token must NOT match the sibling units' getdevices replies.
    assert "IR" not in "device,1,1 SERIAL"
    assert "IR" not in "device,1,3 RELAY"


def test_min_platform_version_gates_on_the_package_move():
    # The IR runtime ships in 0.22.0, but this file imports openavc.*, which
    # only exists from 0.25.0 — so that is the floor an older box has to refuse
    # the install on.
    assert drv.GlobalCacheItachIP2IRDriver.DRIVER_INFO["min_platform_version"] == "0.25.0"


# ---------------------------------------------------------------------------
# Simulator answers the same bytes as the real unit
# ---------------------------------------------------------------------------


def _make_sim() -> "sim_mod.GlobalCacheItachIP2IRSimulator":
    return sim_mod.GlobalCacheItachIP2IRSimulator("sim_ir", {})


def test_sim_getversion_matches_capture():
    s = _make_sim()
    assert s.handle_command(b"getversion") == _fixture("getversion.response.txt")


def test_sim_getdevices_matches_capture():
    s = _make_sim()
    assert s.handle_command(b"getdevices") == _fixture("getdevices.response.txt")


def test_sim_get_ir_matches_capture():
    s = _make_sim()
    assert s.handle_command(b"get_IR,1:1") == _fixture("get_ir_1_1.response.txt")


def test_sim_sendir_completes():
    s = _make_sim()
    reply = s.handle_command(b"sendir,1:1,5555,38000,1,1,96,24,48,24")
    assert reply == _fixture("completeir_5555.response.txt")


def test_sim_sendir_bad_pulses_errors():
    s = _make_sim()
    # Odd pulse count -> ERR_<conn>,010, matching the real unit.
    assert s.handle_command(b"sendir,1:1,5555,38000,1,1,96,24,48") == _fixture(
        "err_bad_pulses.response.txt"
    )


def test_sim_bad_connector_errors():
    s = _make_sim()
    assert s.handle_command(b"get_IR,1:9") == _fixture("err_bad_connector.response.txt")


def test_sim_learner_streams_a_capture():
    s = _make_sim()
    reply = s.handle_command(b"get_IRL")
    assert reply.startswith(b"IR Learner Enabled\r")
    assert b"sendir,2:1," in reply
    # The canned capture is a valid, Pronto-convertible sendir.
    line = reply.split(b"\r", 1)[1]
    pronto = drv.sendir_to_pronto(line)
    assert pronto.startswith("0000 ")
    assert s.handle_command(b"stop_IRL") == b"IR Learner Disabled\r"
