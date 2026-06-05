"""Unit tests for the roku_ecp driver.

Loads ``streaming/roku_ecp.py`` directly, stubbing the ``server.*`` imports it
needs (BaseDriver, HTTPClientTransport, get_logger) so the community repo's
test suite stays self-contained — mirrors test_darwin_control.py. Only the
module-level XML parsers and the command surface are exercised; the HTTP I/O
path is the platform's concern and is covered by the simulator.

The ECP XML samples below are synthesized from Roku's published External
Control Protocol responses.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "streaming" / "roku_ecp.py"


def _install_server_stubs() -> None:
    if "server.drivers.base" in sys.modules:
        return
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server", server)

    drivers = ModuleType("server.drivers")
    drivers.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.drivers", drivers)
    base = ModuleType("server.drivers.base")

    class _BaseDriver:  # minimal stand-in; we only test module-level code
        DRIVER_INFO: dict = {}

    base.BaseDriver = _BaseDriver
    sys.modules["server.drivers.base"] = base

    transport = ModuleType("server.transport")
    transport.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.transport", transport)
    http_client = ModuleType("server.transport.http_client")

    class _HTTPClientTransport:
        pass

    http_client.HTTPClientTransport = _HTTPClientTransport
    sys.modules["server.transport.http_client"] = http_client

    utils = ModuleType("server.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("server.utils", utils)
    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="roku": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_server_stubs()
drv = _load(DRIVER_PATH, "roku_ecp_under_test")
INFO = drv.RokuECPDriver.DRIVER_INFO

_SPECIAL_COMMANDS = {
    "launch_app", "install_app", "type_text", "keypress", "key_down", "key_up",
}

# A representative /query/device-info body (Roku Ultra, fully on).
_DEVICE_INFO = """<?xml version="1.0" encoding="UTF-8" ?>
<device-info>
  <udn>015e5108-9000-1046-8035-b0a737964dfb</udn>
  <serial-number>1GU48T017973</serial-number>
  <device-id>1GU48T017973</device-id>
  <model-name>Roku Ultra</model-name>
  <model-number>4800X</model-number>
  <friendly-device-name>Roku Ultra Living Room</friendly-device-name>
  <user-device-name>Living Room</user-device-name>
  <software-version>13.0.0</software-version>
  <software-build>4209</software-build>
  <power-mode>PowerOn</power-mode>
  <network-type>ethernet</network-type>
  <supports-tv-power-control>false</supports-tv-power-control>
  <is-tv>false</is-tv>
  <is-stick>false</is-stick>
</device-info>
"""

# A Roku TV in networked standby.
_DEVICE_INFO_TV_STANDBY = """<device-info>
  <model-name>TCL Roku TV</model-name>
  <power-mode>Ready</power-mode>
  <supports-tv-power-control>true</supports-tv-power-control>
  <is-tv>true</is-tv>
</device-info>
"""


# ── device-info parsing ──

def test_device_info_extracts_identity():
    info = drv._parse_device_info(_DEVICE_INFO)
    assert info["model_name"] == "Roku Ultra"
    assert info["serial_number"] == "1GU48T017973"
    assert info["software_version"] == "13.0.0"
    assert info["network_type"] == "ethernet"
    # user-device-name wins over friendly-device-name.
    assert info["device_name"] == "Living Room"


def test_device_info_power_and_booleans():
    info = drv._parse_device_info(_DEVICE_INFO)
    assert info["power_mode"] == "PowerOn"
    assert info["power"] == "on"
    assert info["is_tv"] is False
    assert info["supports_tv_power"] is False


def test_device_info_tv_standby():
    info = drv._parse_device_info(_DEVICE_INFO_TV_STANDBY)
    assert info["power"] == "standby"
    assert info["is_tv"] is True
    assert info["supports_tv_power"] is True
    # Absent elements are simply omitted, not defaulted.
    assert "serial_number" not in info


def test_power_mode_mapping():
    assert drv._POWER_MAP["PowerOn"] == "on"
    assert drv._POWER_MAP["Ready"] == "standby"
    assert drv._POWER_MAP["DisplayOff"] == "standby"
    assert drv._POWER_MAP["PowerOff"] == "off"
    # Unknown modes fall back to "off" in the parser.
    info = drv._parse_device_info("<device-info><power-mode>Surprise</power-mode></device-info>")
    assert info["power"] == "off"
    assert info["power_mode"] == "Surprise"


# ── active-app parsing ──

def test_active_app_running():
    xml = '<active-app><app id="12" type="appl" version="4.1.218">Netflix</app></active-app>'
    info = drv._parse_active_app(xml)
    assert info == {"active_app_id": "12", "active_app_name": "Netflix"}


def test_active_app_home_screen():
    # The home screen reports the app named "Roku" with no id.
    info = drv._parse_active_app("<active-app><app>Roku</app></active-app>")
    assert info == {"active_app_id": "", "active_app_name": "Home"}


def test_active_app_screensaver():
    xml = (
        "<active-app><app>Roku</app>"
        '<screensaver id="55545" type="ssvr" version="1.0">Default</screensaver>'
        "</active-app>"
    )
    info = drv._parse_active_app(xml)
    assert info["active_app_id"] == "55545"
    assert info["active_app_name"] == "Default"


# ── media-player parsing ──

def test_media_player_playing():
    xml = (
        '<player error="false" state="play">'
        "<position>6916 ms</position><duration>887999 ms</duration></player>"
    )
    info = drv._parse_media_player(xml)
    assert info["media_state"] == "play"
    assert info["media_position"] == 6916
    assert info["media_duration"] == 887999


def test_media_player_idle():
    info = drv._parse_media_player('<player error="false" state="none"></player>')
    assert info["media_state"] == "none"
    assert "media_position" not in info


def test_parsers_tolerate_garbage():
    assert drv._parse_device_info("not xml at all") == {}
    assert drv._parse_active_app("<broken") == {}
    assert drv._parse_media_player("") == {}
    assert drv._parse_tv_channel("nope") == {}


# ── tv-active-channel parsing ──

def test_tv_channel_active():
    xml = (
        "<tv-channel><channel>"
        "<number>14.3</number><name>getTV</name>"
        "<type>air-digital</type><program-title>Airwolf</program-title>"
        "</channel></tv-channel>"
    )
    info = drv._parse_tv_channel(xml)
    assert info["tv_channel"] == "14.3"
    assert info["tv_channel_name"] == "getTV"
    assert info["tv_program"] == "Airwolf"


def test_tv_channel_blank_when_not_tuned():
    info = drv._parse_tv_channel("<tv-channel><channel></channel></tv-channel>")
    assert info == {"tv_channel": "", "tv_channel_name": "", "tv_program": ""}


# ── command surface ──

def test_every_command_has_a_handler():
    for name in INFO["commands"]:
        assert name in drv._KEYPRESS or name in _SPECIAL_COMMANDS, (
            f"command {name!r} has no dispatch path"
        )


def test_keypress_table_matches_declared_commands():
    for name in drv._KEYPRESS_COMMANDS:
        assert name in INFO["commands"], f"{name} missing from commands"
        assert name in drv._KEYPRESS


def test_keypress_keys_are_nonempty_tokens():
    for name, key in drv._KEYPRESS.items():
        assert key and key.isalnum(), f"{name} maps to a bad ECP key {key!r}"


def test_every_command_declares_label_and_params():
    for name, spec in INFO["commands"].items():
        assert spec.get("label"), f"{name} missing label"
        assert "params" in spec, f"{name} missing params"


# ── discovery declaration sanity ──

def test_discovery_probe_matches_device_info():
    probe = INFO["discovery"]["tcp_probe"]
    assert probe["port"] == 8060
    # The declared substring matcher must hit a real device-info body.
    assert probe["expect"] in _DEVICE_INFO
    # And the model extract must pull a value.
    import re
    match = re.search(probe["extract"]["model"]["regex"], _DEVICE_INFO)
    assert match and match.group(1) == "Roku Ultra"
