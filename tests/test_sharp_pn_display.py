"""Driver + simulator tests for sharp_pn_display (pre-merger Sharp
RS-232C / LAN protocol).

No Sharp PN hardware on hand, so correctness is proven two ways:
metadata / shape assertions on the driver, and **dual-proof round
trips** that wire the real driver to the real simulator over an
in-memory transport — the simulator renders the 4+4 ASCII grammar from
the PN-L603B operation manual, the driver parses it, and results are
asserted on both sides.

Covers the protocol essentials:
  - parameter-field formatting against the manual's worked examples
    (zero padding, the three-digit negative numeral);
  - the serialized request/response design that the echo-less bare
    value replies force, including WAIT interim responses, LOCKED,
    reply timeouts, and chain-ID reply suffixes ("OK 001" / "30 001");
  - the LAN login handshake (prompts arrive with no line ending):
    credential send, blank-credential send, rejection -> a clear
    ConnectionError, and prompt timeout;
  - every write mutating the simulator and every polled read landing
    in the right state variable (negative audio values, the inverted
    LED encoding, PC vs AV resolution reads, temperature/standby
    diagnostics);
  - device-setting writes with immediate read-back;
  - the documented standby restriction list (MUTE and friends answer
    ERR in standby while POWR and other reads keep working);
  - set-only functions fabricating no state.

Loads the driver + simulator with the ``server.*`` / ``simulator.*``
imports stubbed so the community CI stays self-contained (conftest.py
rolls the stubs back after this module is collected).
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
DRIVER_PATH = REPO_ROOT / "displays" / "sharp_pn_display.py"
SIM_PATH = REPO_ROOT / "displays" / "sharp_pn_display_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver:
    """Stand-in for the platform BaseDriver surface this driver uses."""

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
        return self.state.data.get(f"device.{self.device_id}.{key}", default)


_CURRENT_SIM: object | None = None


class _FakeTransport:
    """In-memory pipe: driver bytes -> sim -> reply lines -> driver,
    split on CRLF exactly like the platform's delimiter framing."""

    def __init__(self, on_data) -> None:
        self.on_data = on_data
        self.connected = True
        self.sent: list[bytes] = []
        self.silent = False

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        self.sent.append(bytes(data))
        if self.silent:
            return
        sim = _CURRENT_SIM
        reply = sim.handle_command(bytes(data)) if sim else None
        if reply:
            for frame in bytes(reply).split(b"\r\n"):
                if frame:
                    await self.on_data(frame)

    async def close(self) -> None:
        self.connected = False


class _FakeTCPSimulator:
    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

    @property
    def state(self) -> dict:
        # Mirrors BaseSimulator: a READ-ONLY COPY.
        return dict(self._state)

    def set_state(self, key, value) -> None:
        self._state[key] = value

    def get_state(self, key, default=None):
        return self._state.get(key, default)


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


DRV = _load("sharp_pn_display_under_test", DRIVER_PATH)
SIM = _load("sharp_pn_display_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM
    sim = SIM.SharpPnDisplaySimulator("sim1", sim_config or {})
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.62",
        "port": 10008,
        "username": "",
        "password": "",
        "poll_interval": 0,
        "inter_command_delay": 0.0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.SharpPnDisplayDriver("pn1", cfg, _FakeState(), _FakeEvents())
    driver.transport = _FakeTransport(driver.on_data_received)
    return driver, sim


def _dstate(driver, key):
    return driver.get_state(key)


def _run(coro):
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def pair():
    return _make_pair()


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_shape():
    info = DRV.SharpPnDisplayDriver.DRIVER_INFO
    assert info["version"] == "1.0.1"
    assert info["min_platform_version"] == "0.24.0"
    assert info["ports"] == [10008]
    assert info["transports"] == ["tcp", "serial"]
    for key, setting in info["device_settings"].items():
        assert setting["state_key"] in info["state_variables"], key
    for cid in info["quick_actions"]:
        assert cid in info["commands"], cid
    # LAN login credentials are declared config, password secret.
    assert info["config_schema"]["password"]["secret"] is True


def test_parameter_field_formatting():
    fmt = DRV.SharpPnDisplayDriver._fmt
    # Manual example: VOLM0030.
    assert fmt(30) == "0030"
    assert fmt(0) == "0000"
    # Manual example: AUTR-005 (three-digit negative numeral).
    assert fmt(-5) == "-005"
    assert fmt(-10) == "-010"


def test_probe_banner_matches_sim_login_prompt():
    """The banner probe's expect string must be the first thing the
    simulator's login gate writes."""
    probe = DRV.SharpPnDisplayDriver.DRIVER_INFO["discovery"]["tcp_probe"]
    assert probe["port"] == 10008
    assert "send_ascii" not in probe and "send_hex" not in probe

    written: list[bytes] = []

    class _W:
        def write(self, data):
            written.append(bytes(data))

        async def drain(self):
            pass

    class _R:
        async def readline(self):
            raise asyncio.TimeoutError

    sim = SIM.SharpPnDisplaySimulator("probe_sim", {})
    _run(sim.authenticate_client(_R(), _W(), "c1"))
    assert written and written[0] == probe["expect"].encode("ascii")


# ── LAN login handshake (driver side, scripted prompts) ─────────────────────

class _ScriptedTransport:
    """Feeds prompt bytes to the driver in response to its own sends."""

    def __init__(self, driver, script) -> None:
        # script: list of (expected_sent_or_None, bytes_to_feed)
        self.driver = driver
        self.script = list(script)
        self.connected = True
        self.sent: list[bytes] = []

    async def start(self):
        # Initial device banner (sent before the driver says anything).
        while self.script and self.script[0][0] is None:
            _, feed = self.script.pop(0)
            await self.driver.on_data_received(feed)

    async def send(self, data) -> None:
        self.sent.append(bytes(data))
        while self.script and self.script[0][0] is not None:
            expected, feed = self.script.pop(0)
            assert bytes(data) == expected, (data, expected)
            await self.driver.on_data_received(feed)
            break
        while self.script and self.script[0][0] is None:
            _, feed = self.script.pop(0)
            await self.driver.on_data_received(feed)

    async def close(self) -> None:
        self.connected = False


def _login_driver(script, **cfg_overrides):
    cfg = {
        "host": "10.0.0.62", "port": 10008,
        "username": "", "password": "", "poll_interval": 0,
    }
    cfg.update(cfg_overrides)
    driver = DRV.SharpPnDisplayDriver("pn1", cfg, _FakeState(), _FakeEvents())
    driver._auth_mode = True
    driver._auth_buffer = bytearray()
    driver._auth_event = asyncio.Event()
    driver.transport = _ScriptedTransport(driver, script)
    return driver


def test_login_sends_credentials():
    driver = _login_driver(
        [
            (None, b"Login:"),
            (b"integrator\r\n", b"Password:"),
            (b"s3cret\r\n", b"OK\r\n"),
        ],
        username="integrator", password="s3cret",
    )

    async def run():
        await driver.transport.start()
        await driver._perform_login()

    _run(run())
    assert driver.transport.sent == [b"integrator\r\n", b"s3cret\r\n"]


def test_login_blank_credentials_send_bare_lines():
    driver = _login_driver(
        [
            (None, b"Login:"),
            (b"\r\n", b"Password:"),
            (b"\r\n", b"OK\r\n"),
        ],
    )

    async def run():
        await driver.transport.start()
        await driver._perform_login()

    _run(run())
    assert driver.transport.sent == [b"\r\n", b"\r\n"]


def test_login_rejection_raises_auth_error():
    # A re-prompt after the password means the credentials were rejected.
    driver = _login_driver(
        [
            (None, b"Login:"),
            (b"integrator\r\n", b"Password:"),
            (b"wrong\r\n", b"Login:"),
        ],
        username="integrator", password="wrong",
    )

    async def run():
        await driver.transport.start()
        with pytest.raises(ConnectionError, match="[Aa]uthentication failed"):
            await driver._perform_login()

    _run(run())


def test_login_prompt_timeout_raises():
    driver = _login_driver([])
    monkey_timeout = 0.05

    async def run():
        import sharp_pn_display_under_test as mod
        saved = mod._PROMPT_TIMEOUT
        mod._PROMPT_TIMEOUT = monkey_timeout
        try:
            with pytest.raises(ConnectionError, match="No response"):
                await driver._perform_login()
        finally:
            mod._PROMPT_TIMEOUT = saved

    _run(run())


# ── Dual-proof round trips ──────────────────────────────────────────────────

def test_power_round_trip_with_wait(pair):
    driver, sim = pair

    async def run():
        # The sim answers WAIT before OK for POWR; the driver must absorb
        # the interim line and still report success.
        assert await driver.send_command("power_off") is True
        assert sim.state["power"] == 0
        await driver.poll()
        assert _dstate(driver, "power") == "standby"

        assert await driver.send_command("power_on") is True
        assert sim.state["power"] == 1
        await driver.poll()
        assert _dstate(driver, "power") == "on"

    _run(run())


def test_writes_mutate_sim_and_polls_read_back(pair):
    driver, sim = pair

    async def run():
        cases = [
            ("set_input", {"input": "hdmi2_pc"}, "input_code", 13),
            ("set_volume", {"level": 25}, "volume", 25),
            ("set_brightness", {"level": 18}, "brightness", 18),
            ("set_contrast", {"level": 41}, "contrast", 41),
            ("set_black_level", {"level": 42}, "black_level", 42),
            ("set_tint", {"level": 43}, "tint", 43),
            ("set_color", {"level": 44}, "color", 44),
            ("set_sharpness", {"level": 21}, "sharpness", 21),
            ("set_treble", {"level": -5}, "treble", -5),
            ("set_bass", {"level": 3}, "bass", 3),
            ("set_balance", {"level": -10}, "balance", -10),
            ("set_screen_size", {"size": "3"}, "screen_size", 3),
            ("set_pip_mode", {"mode": "pip"}, "pip_mode", 1),
            ("set_pip_source", {"input": "displayport"}, "pip_source", 14),
            ("set_pip_size", {"size": 20}, "pip_size", 20),
            ("set_pip_sound", {"from_window": "sub"}, "pip_sound", 2),
            ("mute_on", None, "mute", 1),
        ]
        for command, params, sim_key, expected in cases:
            assert await driver.send_command(command, params) is True, command
            assert sim.state[sim_key] == expected, command

        # Negative value on the wire uses the three-digit numeral.
        assert b"AUTR-005\r" in driver.transport.sent

        await driver.poll()
        expected_states = {
            "power": "on", "input": "hdmi2_pc", "volume": 25, "mute": True,
            "brightness": 18, "contrast": 41, "black_level": 42,
            "tint": 43, "color": 44, "sharpness": 21,
            "treble": -5, "bass": 3, "balance": -10,
            "screen_size": 3, "pip_mode": "pip", "pip_source": "displayport",
            "pip_sound": "sub", "touch_enabled": True,
            "standby_mode": "standard", "adjustment_lock": "off",
            "osd_display": "on1", "led_enabled": True,
            "temp_status": "normal", "temperature_c": 33,
            "last_standby_cause": "none",
            "input_resolution": "1920, 1080",
        }
        for key, value in expected_states.items():
            assert _dstate(driver, key) == value, key

    _run(run())


def test_av_input_reads_reso_resolution(pair):
    driver, sim = pair

    async def run():
        await driver.send_command("set_input", {"input": "hdmi1_av"})
        assert sim.state["input_code"] == 9
        await driver.poll()
        assert _dstate(driver, "input") == "hdmi1_av"
        # PXCK errs on AV input; RESO answers the signal format.
        assert _dstate(driver, "input_resolution") == "1080p"

    _run(run())


def test_device_settings_write_and_read_back(pair):
    driver, sim = pair

    async def run():
        cases = [
            ("brightness", 25, "brightness", 25, "brightness", 25),
            ("standby_mode", "low_power", "standby_mode", 1,
             "standby_mode", "low_power"),
            ("adjustment_lock", "on2", "adjustment_lock", 2,
             "adjustment_lock", "on2"),
            ("osd_display", "off", "osd_display", 1, "osd_display", "off"),
            # Inverted on the wire: OFLD 1 = LED off.
            ("led_enabled", False, "led_off", 1, "led_enabled", False),
            ("touch", False, "touch_enabled", 0, "touch_enabled", False),
        ]
        for key, value, sim_key, sim_expected, state_key, state_expected in cases:
            await driver.set_device_setting(key, value)
            assert sim.state[sim_key] == sim_expected, key
            # The write issues an immediate read-back.
            assert _dstate(driver, state_key) == state_expected, key

    _run(run())


def test_identity_reads_on_post_connect(pair):
    driver, sim = pair

    async def run():
        await driver._post_connect()
        assert _dstate(driver, "model_name") == "PN-L603B"
        assert _dstate(driver, "serial_number") == "8B0123456"

    _run(run())


def test_diagnostics_decode(pair):
    driver, sim = pair

    async def run():
        sim.set_state("temperature", -3)
        sim.set_state("temp_status", 3)
        sim.set_state("standby_cause", 8)
        await driver.poll()
        assert _dstate(driver, "temperature_c") == -3
        assert _dstate(driver, "temp_status") == "abnormal_dimmed"
        assert _dstate(driver, "last_standby_cause") == "schedule"

    _run(run())


def test_unknown_input_code_reads_as_token(pair):
    driver, sim = pair

    async def run():
        sim.set_state("input_code", 99)
        await driver.poll()
        assert _dstate(driver, "input") == "input_99"

    _run(run())


def test_standby_restriction_list(pair):
    driver, sim = pair

    async def run():
        await driver.poll()
        assert _dstate(driver, "mute") is False

        await driver.send_command("power_off")
        sim.set_state("volume", 7)
        sim.set_state("mute", 1)
        await driver.poll()
        assert _dstate(driver, "power") == "standby"
        # MUTE is on the documented standby-restriction list -> ERR, so the
        # driver's mute state holds; VOLM is not on the list and still reads.
        assert _dstate(driver, "mute") is False
        assert _dstate(driver, "volume") == 7
        # A blocked write is rejected, not applied.
        assert await driver.send_command("mute_off") is False
        assert sim.state["mute"] == 1

    _run(run())


def test_locked_responses_leave_state_untouched(pair):
    driver, sim = pair

    async def run():
        await driver.poll()
        assert _dstate(driver, "volume") == 15
        sim.set_state("locked", 1)
        assert await driver.send_command("set_volume", {"level": 5}) is False
        assert sim.state["volume"] == 15
        sim.set_state("volume", 9)
        await driver.poll()
        # Every read answered LOCKED: state holds.
        assert _dstate(driver, "volume") == 15

    _run(run())


def test_chain_id_suffix_is_stripped():
    driver, sim = _make_pair(sim_config={"monitor_id": 1})

    async def run():
        # Acks carry "OK 001".
        assert await driver.send_command("set_volume", {"level": 5}) is True
        assert sim.state["volume"] == 5
        # Values carry "5 001".
        await driver.poll()
        assert _dstate(driver, "volume") == 5
        assert _dstate(driver, "power") == "on"

    _run(run())


def test_reply_timeout_returns_none_and_state_holds(pair):
    driver, sim = pair

    async def run():
        await driver.poll()
        assert _dstate(driver, "volume") == 15
        driver.transport.silent = True
        line = await driver._request("VOLM", "????", timeout=0.05)
        assert line is None
        assert _dstate(driver, "volume") == 15

    _run(run())


def test_no_fabricated_state_for_set_only_functions(pair):
    driver, sim = pair

    async def run():
        await driver.poll()
        snapshot = dict(driver.state.data)
        await driver.send_command("screen_motion", {"pattern": "2"})
        assert sim.state["screen_motion"] == 2
        await driver.send_command("set_pip_size", {"size": 40})
        assert driver.state.data == snapshot

    _run(run())


def test_raw_command_read_routes_to_state(pair):
    driver, sim = pair

    async def run():
        line = await driver.send_command(
            "raw_command", {"command": "VOLM", "parameter": "????"},
        )
        assert line == "15"
        assert _dstate(driver, "volume") == 15
        # A raw write is passed through untouched.
        line = await driver.send_command(
            "raw_command", {"command": "VOLM", "parameter": "0022"},
        )
        assert line == "OK"
        assert sim.state["volume"] == 22

    _run(run())


def test_poll_raises_on_dead_transport(pair):
    driver, _ = pair

    async def run():
        await driver.transport.close()
        with pytest.raises(ConnectionError):
            await driver.poll()

    _run(run())
