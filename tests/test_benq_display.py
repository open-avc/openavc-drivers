"""Driver + simulator tests for benq_display (BenQ RS232 & LAN protocol).

No BenQ display hardware on hand, so correctness is proven two ways:
metadata / shape assertions on the driver, and **dual-proof round trips**
that wire the real driver to the real simulator over an in-memory
transport — the simulator renders the framed len/ID/type/code/value
packets from the RM6503 RS232 & LAN guide, the driver parses them, and
results are asserted on both sides.

Covers the protocol essentials:
  - packet build against the manual's worked examples (length byte,
    ID field, three- and five-digit value fields, binary selector
    payloads);
  - every set command mutating the simulator and every polled get
    landing in the right state variable (including the high-bit
    command codes that forced this driver to Python);
  - device-setting writes with immediate read-back, including the
    inverted IR/keypad lock encoding (wire 000 = locked);
  - identity decode (model / firmware / serial NUL-padded ASCII, MAC
    hex bytes) and the five-digit operation-time field;
  - Monitor-ID addressing: frames for another chain ID get no reply
    and foreign replies are ignored;
  - the standby gate (only the power get answers) and reject ('-')
    handling leaving state untouched;
  - the Wake-on-LAN setup action's magic packet and MAC resolution
    order (param > learned state > config).

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

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "displays" / "benq_display.py"
SIM_PATH = REPO_ROOT / "displays" / "benq_display_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value


class _FakeEvents:
    async def emit(self, name, *args, **kwargs):
        pass


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


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None


class _FakeTransport:
    """In-memory pipe: driver bytes -> sim -> reply frames -> driver,
    split on CR exactly like the platform's delimiter framing."""

    def __init__(self, on_data) -> None:
        self.on_data = on_data
        self.connected = True
        self.sent: list[bytes] = []

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        self.sent.append(bytes(data))
        sim = _CURRENT_SIM
        reply = sim.handle_command(bytes(data)) if sim else None
        if reply:
            for frame in bytes(reply).split(b"\r"):
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
        # Mirrors BaseSimulator: a READ-ONLY COPY. Sim code must write
        # through set_state; tests read through this copy.
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


DRV = _load("benq_display_under_test", DRIVER_PATH)
SIM = _load("benq_display_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM
    sim = SIM.BenqDisplaySimulator("sim1", sim_config or {})
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.60",
        "port": 4660,
        "monitor_id": 1,
        "poll_interval": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.BenqDisplayDriver("ifp1", cfg, _FakeState(), _FakeEvents())
    driver.transport = _FakeTransport(driver.on_data_received)
    return driver, sim


def _dstate(driver, key):
    return driver.get_state(key)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture()
def pair():
    return _make_pair()


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_shape():
    info = DRV.BenqDisplayDriver.DRIVER_INFO
    assert info["version"] == "1.0.0"
    assert info["min_platform_version"] == "0.23.0"
    assert info["ports"] == [4660]
    assert info["transports"] == ["tcp", "serial"]
    # Every device setting reads back through a declared state variable.
    for key, setting in info["device_settings"].items():
        assert setting["state_key"] in info["state_variables"], key
    # Quick actions promote declared commands.
    for cid in info["quick_actions"]:
        assert cid in info["commands"], cid
    # The wake action is a setup action available while offline.
    wake = next(a for a in info["actions"] if a["id"] == "wake_display")
    assert wake["kind"] == "setup"
    assert wake["availability"] == "offline"


def test_setting_enums_match_state_enums():
    info = DRV.BenqDisplayDriver.DRIVER_INFO
    for key in ("picture_mode", "color_temp", "sound_mode", "power_save",
                "switch_on_status"):
        setting = info["device_settings"][key]
        state_var = info["state_variables"][setting["state_key"]]
        setting_values = {v["value"] for v in setting["values"]}
        assert setting_values == set(state_var["values"]), key


def test_packet_build_matches_manual_examples():
    driver, _ = _make_pair()
    # Manual example: Set Brightness 76 for ID 01 ->
    # 38 30 31 73 24 30 37 36 0D
    assert driver._packet("s", 0x24, b"076") == b"801s\x24076\r"
    # Get with a five-digit value field is a 10-byte packet: length ':'.
    assert driver._packet("g", 0x76, b"00000") == b":01g\x7600000\r"
    # Model-info get carries a binary selector + NUL padding: length 'D' (20).
    packet = driver._packet("g", 0x20, b"\x02" + b"\x00" * 14)
    assert packet[0:1] == b"D" and len(packet) == 21 and packet.endswith(b"\r")


def test_probe_answer_coherence():
    """The declared tcp_probe bytes must be a packet the simulator answers,
    and the reply must contain the declared expect substring."""
    probe = DRV.BenqDisplayDriver.DRIVER_INFO["discovery"]["tcp_probe"]
    assert probe["port"] == 4660
    sim = SIM.BenqDisplaySimulator("probe_sim", {})
    answer = sim.handle_command(probe["send_ascii"].encode("ascii"))
    assert answer is not None
    assert probe["expect"].encode("ascii") in answer


# ── Dual-proof round trips ──────────────────────────────────────────────────

def test_power_transitions(pair):
    driver, sim = pair

    async def run():
        await driver.send_command("screen_off")
        assert sim.state["power_code"] == "000"
        await driver.poll()
        assert _dstate(driver, "power") == "screen_off"

        await driver.send_command("power_on")
        assert sim.state["power_code"] == "001"
        await driver.poll()
        assert _dstate(driver, "power") == "on"

    _run(run())


def test_sets_mutate_sim_and_polls_read_back(pair):
    driver, sim = pair

    async def run():
        cases = [
            ("set_source", {"source": "hdmi2"}, "source_code", "021"),
            ("set_volume", {"level": 42}, "volume", 42),
            ("set_contrast", {"level": 61}, "contrast", 61),
            ("set_brightness", {"level": 62}, "brightness", 62),
            ("set_sharpness", {"level": 63}, "sharpness", 63),
            ("set_saturation", {"level": 64}, "saturation", 64),
            ("set_hue", {"level": 65}, "hue", 65),
            ("set_backlight", {"level": 66}, "backlight", 66),
            ("set_treble", {"level": 67}, "treble", 67),
            ("set_bass", {"level": 68}, "bass", 68),
            ("set_balance", {"level": 69}, "balance", 69),
            ("set_picture_mode", {"mode": "eco"}, "picture_mode_code", "003"),
            ("set_sound_mode", {"mode": "meeting"}, "sound_mode_code", "004"),
            ("set_color_temp", {"temp": "warm"}, "color_temp_code", "002"),
            ("set_aspect", {"aspect": "ptp"}, "aspect_code", "002"),
            ("mute_on", None, "mute_code", "001"),
        ]
        for command, params, sim_key, expected in cases:
            await driver.send_command(command, params)
            assert sim.state[sim_key] == expected, command

        await driver.poll()
        expected_states = {
            "source": "hdmi2", "volume": 42, "contrast": 61,
            "brightness": 62, "sharpness": 63, "saturation": 64,
            "hue": 65, "backlight": 66, "treble": 67, "bass": 68,
            "balance": 69, "picture_mode": "eco", "sound_mode": "meeting",
            "color_temp": "warm", "aspect": "ptp", "mute": True,
            "power": "on", "signal_stable": True,
            "ir_lock": "unlocked", "keypad_lock": "unlocked",
            "power_save": "off", "switch_on_status": "last_status",
            "wol_enabled": True, "operation_hours": 1786,
        }
        for key, value in expected_states.items():
            assert _dstate(driver, key) == value, key

    _run(run())


def test_device_settings_write_and_read_back(pair):
    driver, sim = pair

    async def run():
        cases = [
            ("backlight", 55, "backlight", 55, "backlight", 55),
            ("picture_mode", "custom1", "picture_mode_code", "005",
             "picture_mode", "custom1"),
            ("color_temp", "cool", "color_temp_code", "000",
             "color_temp", "cool"),
            ("sound_mode", "class", "sound_mode_code", "003",
             "sound_mode", "class"),
            ("power_save", "high", "power_save_code", "002",
             "power_save", "high"),
            ("switch_on_status", "force_on", "switch_on_code", "001",
             "switch_on_status", "force_on"),
            ("wol", False, "wol_code", "000", "wol_enabled", False),
            # Inverted on the wire: 000 = Disable(d) = locked.
            ("ir_lock", "locked", "ir_lock_code", "000", "ir_lock", "locked"),
            ("keypad_lock", "locked", "keypad_lock_code", "000",
             "keypad_lock", "locked"),
        ]
        for key, value, sim_key, sim_expected, state_key, state_expected in cases:
            await driver.set_device_setting(key, value)
            assert sim.state[sim_key] == sim_expected, key
            # The write issues an immediate read-back get.
            assert _dstate(driver, state_key) == state_expected, key

    _run(run())


def test_identity_and_mac_decode(pair):
    driver, sim = pair

    async def run():
        await driver._post_connect()
        assert _dstate(driver, "model_name") == "RM6503"
        assert _dstate(driver, "firmware_version") == "1.02"
        assert _dstate(driver, "serial_number") == "ETC1M00001SL0"
        assert _dstate(driver, "mac_address") == "80:65:e9:12:34:56"

    _run(run())


def test_remote_keys_and_toggles(pair):
    driver, sim = pair

    async def run():
        before = sim.state["volume"]
        await driver.send_command("remote_key", {"key": "vol_up"})
        assert sim.state["volume"] == before + 1
        await driver.send_command("volume_down")
        assert sim.state["volume"] == before

        # Blank / freeze have no read-back: the sim ACKs, the driver must
        # not fabricate any state for them.
        snapshot = dict(driver.state.data)
        await driver.send_command("blank_toggle")
        await driver.send_command("freeze_toggle")
        assert driver.state.data == snapshot

    _run(run())


def test_monitor_id_addressing():
    # Driver addresses ID 2; the sim is ID 1 -> chain silence, no state.
    driver, sim = _make_pair(driver_overrides={"monitor_id": 2})

    async def run():
        await driver.poll()
        assert _dstate(driver, "power") is None
        # A foreign reply frame is ignored by the driver's ID filter.
        await driver.on_data_received(b"801rl001")
        assert _dstate(driver, "power") is None

    _run(run())


def test_rejects_leave_state_untouched(pair):
    driver, sim = pair

    async def run():
        await driver.poll()
        assert _dstate(driver, "volume") == 30
        # Out-of-range raw set: the sim answers '-', nothing changes.
        await driver.send_command(
            "raw_command", {"cmd_type": "s", "code": "35", "value": "176"},
        )
        assert sim.state["volume"] == 30
        await driver.poll()
        assert _dstate(driver, "volume") == 30

    _run(run())


def test_standby_gate_holds_state(pair):
    driver, sim = pair

    async def run():
        await driver.poll()
        assert _dstate(driver, "volume") == 30

        await driver.send_command("standby")
        assert sim.state["power_code"] == "002"
        # Nudge the sim's held values; in standby only the power get
        # answers, so the driver's other state must hold.
        sim.set_state("volume", 77)
        await driver.poll()
        assert _dstate(driver, "power") == "standby"
        assert _dstate(driver, "volume") == 30

    _run(run())


def test_poll_raises_on_dead_transport(pair):
    driver, _ = pair

    async def run():
        await driver.transport.close()
        with pytest.raises(ConnectionError):
            await driver.poll()

    _run(run())


# ── Wake-on-LAN setup action ────────────────────────────────────────────────

class _FakeSocket:
    sent: list[tuple[bytes, tuple]] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def setsockopt(self, *args) -> None:
        pass

    def sendto(self, data, addr) -> None:
        _FakeSocket.sent.append((bytes(data), addr))


class _FakeSocketModule:
    """Replaces the driver module's ``socket`` attribute only — patching
    the real module would break asyncio's own socketpair plumbing."""

    AF_INET = 2
    SOCK_DGRAM = 2
    SOL_SOCKET = 1
    SO_BROADCAST = 6
    socket = _FakeSocket


def test_wake_display_sends_magic_packet(pair, monkeypatch):
    driver, _ = pair
    monkeypatch.setattr(DRV, "socket", _FakeSocketModule)
    _FakeSocket.sent = []
    driver.set_state("mac_address", "80:65:e9:12:34:56")
    progress_lines: list[str] = []

    async def progress(step, pct=None):
        progress_lines.append(step)

    async def run():
        result = await driver.run_setup_action("wake_display", {}, progress)
        assert result == {"mac": "80:65:e9:12:34:56"}

    _run(run())
    mac_bytes = bytes([0x80, 0x65, 0xE9, 0x12, 0x34, 0x56])
    magic = b"\xff" * 6 + mac_bytes * 16
    # Broadcast plus a direct copy at the configured host.
    assert (magic, ("255.255.255.255", 9)) in _FakeSocket.sent
    assert (magic, ("10.0.0.60", 9)) in _FakeSocket.sent
    assert progress_lines


def test_wake_display_mac_resolution_and_validation(pair, monkeypatch):
    driver, _ = pair
    monkeypatch.setattr(DRV, "socket", _FakeSocketModule)
    _FakeSocket.sent = []

    async def progress(step, pct=None):
        pass

    async def run():
        # No MAC anywhere -> a clear error.
        with pytest.raises(ValueError, match="No MAC address"):
            await driver.run_setup_action("wake_display", {}, progress)
        # Malformed MAC -> a clear error.
        with pytest.raises(ValueError, match="not a valid MAC"):
            await driver.run_setup_action(
                "wake_display", {"mac": "not-a-mac"}, progress,
            )
        # An explicit param wins over config.
        driver.config["mac_address"] = "11:22:33:44:55:66"
        result = await driver.run_setup_action(
            "wake_display", {"mac": "aa:bb:cc:dd:ee:ff"}, progress,
        )
        assert result == {"mac": "aa:bb:cc:dd:ee:ff"}

    _run(run())
