"""Unit tests for the globalcache_itach_ip2sl bridge driver.

Self-contained / stdlib only: stubs the ``server.*`` modules the driver imports
so the test runs in the community repo's isolated CI without an ``openavc``
install. Exercises the pure protocol helpers against byte-exact captures taken
from a real iTach IP2SL (fixtures/globalcache_itach_ip2sl/), and checks the
catalog-facing DRIVER_INFO (bridge port + discovery hints).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "utility" / "globalcache_itach_ip2sl.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "globalcache_itach_ip2sl"


def _install_server_stubs() -> None:
    """Provide the minimal ``server.*`` surface the driver imports at module load."""
    if "server.drivers.base" in sys.modules:
        return
    server = ModuleType("server")
    sys.modules.setdefault("server", server)

    drivers = ModuleType("server.drivers")
    sys.modules.setdefault("server.drivers", drivers)
    base = ModuleType("server.drivers.base")

    class _BaseDriver:
        DRIVER_INFO: dict = {}

    base.BaseDriver = _BaseDriver
    sys.modules["server.drivers.base"] = base

    utils = ModuleType("server.utils")
    sys.modules.setdefault("server.utils", utils)
    logger_mod = ModuleType("server.utils.logger")
    logger_mod.get_logger = lambda name="gc": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger_mod


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_server_stubs()
drv = _load(DRIVER_PATH, "globalcache_itach_ip2sl_under_test")


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Pure protocol helpers vs. byte-exact hardware captures
# ---------------------------------------------------------------------------


def test_parse_version_from_capture():
    assert drv.parse_version(_fixture("getversion.response.txt")) == "710-1009-05"


def test_parse_version_rejects_non_version():
    assert drv.parse_version("ERR_01") == ""
    assert drv.parse_version("SERIAL,1:1,19200,FLOW_NONE,PARITY_NO") == ""


def test_parse_getdevices_from_capture():
    modules = drv.parse_getdevices(_fixture("getdevices.response.txt"))
    assert modules == [
        {"module": 0, "ports": 0, "type": "ETHERNET"},
        {"module": 1, "ports": 1, "type": "SERIAL"},
    ]


def test_parse_serial_response_from_capture():
    parsed = drv.parse_serial_response(_fixture("get_serial.response.txt"))
    assert parsed == {
        "port": "1:1",
        "baudrate": 19200,
        "flow": "FLOW_NONE",
        "parity": "PARITY_NO",
    }


def test_parse_set_serial_echo_from_capture():
    """The captured set_SERIAL echo (a 9600 change) round-trips through the
    same parser the prepare_bridge_port confirmation uses."""
    parsed = drv.parse_serial_response(_fixture("set_serial.echo.txt"))
    assert parsed["baudrate"] == 9600
    assert parsed["parity"] == "PARITY_NO"


def test_parse_amx_beacon_from_capture():
    fields = drv.parse_amx_beacon(_fixture("amx_beacon.txt"))
    assert fields["Make"] == "GlobalCache"
    assert fields["Model"] == "iTachIP2SL"
    assert fields["Revision"] == "710-1009-05"
    # UUID embeds the MAC -> OUI 00:0C:1E
    assert fields["UUID"] == "GlobalCache_000C1E075C67"
    assert fields["Config-URL"] == "http://192.168.4.54"


# ---------------------------------------------------------------------------
# Request building / param mapping
# ---------------------------------------------------------------------------


def test_format_set_serial_is_cr_terminated():
    req = drv.format_set_serial("1:1", 9600, "FLOW_NONE", "PARITY_NO")
    assert req == b"set_SERIAL,1:1,9600,FLOW_NONE,PARITY_NO\r"


def test_bridge_port_to_modaddr():
    assert drv.bridge_port_to_modaddr("serial:1") == "1:1"


def test_bridge_port_to_modaddr_rejects_non_serial():
    import pytest

    with pytest.raises(ValueError):
        drv.bridge_port_to_modaddr("ir:1")


def test_openavc_serial_to_gc_defaults():
    assert drv.openavc_serial_to_gc({"baudrate": 9600, "parity": "N"}) == (
        9600, "FLOW_NONE", "PARITY_NO",
    )


def test_openavc_serial_to_gc_even_parity_and_hardware_flow():
    assert drv.openavc_serial_to_gc(
        {"baudrate": 19200, "parity": "E", "flow_control": "hardware"}
    ) == (19200, "FLOW_HARDWARE", "PARITY_EVEN")


def test_prepare_request_matches_set_serial_echo_baud():
    """The request prepare_bridge_port would send for a 9600/N device carries
    the same baud the captured set_SERIAL echo confirms — end-to-end coherence
    between what we send and what the hardware echoed."""
    baudrate, flow, parity = drv.openavc_serial_to_gc({"baudrate": 9600, "parity": "N"})
    req = drv.format_set_serial(drv.bridge_port_to_modaddr("serial:1"), baudrate, flow, parity)
    assert req == b"set_SERIAL,1:1,9600,FLOW_NONE,PARITY_NO\r"
    echo = drv.parse_serial_response(_fixture("set_serial.echo.txt"))
    assert echo["baudrate"] == baudrate


# ---------------------------------------------------------------------------
# Catalog-facing DRIVER_INFO (bridge declaration + discovery hints)
# ---------------------------------------------------------------------------


def test_bridge_port_declaration():
    ports = drv.GlobalCacheItachIP2SLDriver.DRIVER_INFO["bridge"]["ports"]
    assert len(ports) == 1
    port = ports[0]
    assert port["id"] == "serial:1"
    assert port["kind"] == "serial"
    assert port["passthrough_port"] == 4999


def test_discovery_hints_match_real_beacon():
    info = drv.GlobalCacheItachIP2SLDriver.DRIVER_INFO
    disc = info["discovery"]
    # Beacon fingerprint matches the captured Make/Model.
    fields = drv.parse_amx_beacon(_fixture("amx_beacon.txt"))
    amx = disc["amx_ddp"][0]
    assert amx["make"] == fields["Make"]
    assert amx["model_pattern"] == fields["Model"]
    # 4998 getdevices probe + OUI.
    assert disc["tcp_probe"]["port"] == 4998
    assert disc["tcp_probe"]["expect"] == "endlistdevices"
    assert "00:0C:1E" in disc["oui"]


def test_min_platform_version_gates_on_bridge_runtime():
    # Bridge runtime (resolver + prepare_bridge_port) ships in 0.19.0.
    assert drv.GlobalCacheItachIP2SLDriver.DRIVER_INFO["min_platform_version"] == "0.19.0"
